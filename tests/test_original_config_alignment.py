"""原仓配置对齐测试（第三轮）。

四个策略的默认值逐项对齐原仓（Parallax 两包 / WGALL /
entropy-arb / perp-dex-tools，见仓库根目录《原仓配置对照表.md》）；
本文件锁住对齐后的默认值与本轮补进的 premium 参数行为
（执行冷却、单笔名义下限、库存阶梯参数化），防止再漂移。
"""

import random

import pytest

from detsp.core.config import build_config
from detsp.core.engine import Engine
from detsp.core.ledger import Ledger
from detsp.strategies.funding_carry import CarryParams
from detsp.strategies.hedged_grid import HedgedGridParams
from detsp.strategies.premium_arb import (PremiumParams,
                                          inventory_surcharge_bps)
from detsp.strategies.rotation_hedge import (RotationParams,
                                             draw_hold_seconds)
from detsp.strategies.specs import strategy_specs
from detsp.venues.paper import PaperVenue


# ── 默认值对齐 ──────────────────────────────────────────────

def test_rotation_defaults_match_parallax():
    p = RotationParams(symbol="BTC", venue_a="arcus", venue_b="lighter",
                       notional_usd=300)
    assert p.leverage_a == 6 and p.leverage_b == 6      # 表单默认杠杆
    assert p.hold_min_seconds == 3600                   # 轮换最短 1h
    assert p.hold_max_seconds == 7200                   # 轮换最长 2h
    assert p.cooldown_seconds == 30                     # TaskState 默认
    assert p.arcus_maker is True                        # 表单默认限价
    assert p.maker_wait_seconds == 120                  # MAKER_WAIT_SECONDS
    # 默认即区间随机（原仓语义），抽签落在 [1h, 2h]
    drawn = draw_hold_seconds(p, random.Random(7))
    assert 3600 <= drawn <= 7200


def test_carry_defaults_match_parallax_funding():
    p = CarryParams(venue_a="arcus", venue_b="lighter", symbol="BTC",
                    notional_usd=300)
    assert p.leverage == 6
    assert p.check_seconds == 30        # FUNDING_REFRESH_SECONDS


def test_grid_defaults_match_wgall():
    p = HedgedGridParams(grid_venue="arcus", hedge_venue="lighter",
                         symbol="BTC", lower_price=90, upper_price=110,
                         qty_per_grid=0.1)
    assert p.grid_count == 20
    assert p.leverage_grid == 3 and p.leverage_hedge == 3
    assert p.skip_band_frac == 0.25
    assert p.fee_bps_per_side == 5
    assert p.mode == "long_short" and p.out_of_range_action == "close"


def test_premium_defaults_match_entropy_arb_and_perp_tools():
    p = PremiumParams(midline_bps=0, upper_bps=4, lower_bps=4)
    assert p.max_position_usd == 1000
    assert p.max_order_notional_usd == 500
    assert p.min_order_notional_usd == 10       # sizing.min_order_notional
    assert p.take_fraction == 0.5
    assert p.persist_seconds == 0.3             # 原仓代码兜底（example 写 0）
    assert p.cooldown_seconds == 0              # execution.cooldown_sec
    assert p.inventory_scale_bps == 10          # inventory.scale_bps
    assert p.inventory_floor_frac == 0.5        # 原仓代码兜底（example 写 0）
    assert p.reconcile_seconds == 15
    assert p.maker_offset_ticks == 1            # perp-tools BBO 内侧 1 tick
    assert p.maker_timeout_seconds == 5         # perp-tools --fill-timeout


def test_retained_differences_have_help_notes():
    """保留与原仓差异的字段必须在 specs 里带 help 说明原因。"""
    specs = {s["strategy"]: {f["name"]: f for f in s["fields"]}
             for s in strategy_specs()}
    for st in ("rotation_hedge", "premium_arb", "funding_carry"):
        assert "原仓" in specs[st]["max_slippage_bps"]["help"]
    assert "原仓" in specs["premium_arb"]["settle_grace_seconds"]["help"]
    assert "原仓" in specs["rotation_hedge"]["notional_min_usd"]["help"]
    assert "实测" in specs["premium_arb"]["midline_bps"]["help"]
    # 本轮新补字段都有中文 label
    for name in ("min_order_notional_usd", "cooldown_seconds",
                 "inventory_scale_bps", "inventory_floor_frac"):
        assert specs["premium_arb"][name]["label"] != name


# ── premium 新参数行为 ──────────────────────────────────────

def _venues():
    base = PaperVenue(venue_id="entropy", prices={"SNDK": 100.0})
    hedge = PaperVenue(venue_id="lighter_rh", prices={"SNDK": 99.0})
    return {"entropy": base, "lighter_rh": hedge}


def _build(venues, **params):
    base = {"symbol": "SNDK", "base_venue": "entropy",
            "hedge_venue": "lighter_rh", "midline_bps": 0,
            "upper_bps": 50, "lower_bps": 50, "persist_seconds": 0,
            "max_position_usd": 1000, "max_order_notional_usd": 500,
            "execution_mode": "taker"}
    base.update(params)
    # 第十轮夹具调整：冷却/再开是原生循环语义，钉 semi_fixed
    # （缺省 manual 只打一笔、不再 fire）
    y = {"tasks": [{"name": "arb", "strategy": "premium_arb",
                    "run_mode": "semi_fixed", "params": base}]}
    cfg = build_config(env={}, yaml_data=y)
    engine = Engine.from_config(cfg, venues, ledger=Ledger())
    assert "arb" not in engine.errors, engine.errors
    return engine


def _legs(engine):
    pb = engine.executor.position("entropy", "SNDK")
    ph = engine.executor.position("lighter_rh", "SNDK")
    return (pb.qty if pb else 0.0), (ph.qty if ph else 0.0)


def test_premium_cooldown_blocks_refire_until_interval():
    engine = _build(_venues(), cooldown_seconds=100)
    engine.tick(now=0)                       # 信号触发，首笔 -5/+5
    assert _legs(engine) == (pytest.approx(-5), pytest.approx(5))
    engine.tick(now=50)                      # 冷却中：不再加仓
    assert _legs(engine) == (pytest.approx(-5), pytest.approx(5))
    engine.tick(now=101)                     # 冷却满：第二笔到仓位上限
    assert _legs(engine) == (pytest.approx(-10), pytest.approx(10))


def test_premium_min_order_notional_skips_dust():
    engine = _build(_venues(), max_order_notional_usd=5)
    engine.tick(now=0)                       # 切片名义 5 < 下限 10：不下单
    assert _legs(engine) == (0.0, 0.0)


def test_inventory_surcharge_reads_params():
    p = PremiumParams(midline_bps=0, upper_bps=4, lower_bps=4)
    # 默认 floor 0.5 / scale 10：仓位 75% 时附加 (0.75-0.5)/0.5*10 = 5
    assert inventory_surcharge_bps(750.0, p) == pytest.approx(5.0)
    p2 = PremiumParams(midline_bps=0, upper_bps=4, lower_bps=4,
                       inventory_floor_frac=0.0, inventory_scale_bps=20.0)
    assert inventory_surcharge_bps(750.0, p2) == pytest.approx(15.0)
    # 显式传参仍可覆盖（旧调用兼容）
    assert inventory_surcharge_bps(750.0, p, scale_bps=0.0) == 0.0
