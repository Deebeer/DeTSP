"""第十二轮：保证金语义 + 预设移除后的组装口径。

覆盖：amount_usd 一律按保证金解释（仓位名义 = 保证金 ×
基准腿杠杆）——资金费换算 notional、溢价用保证金封顶最大
持仓、溢价单笔上限由显式字段 order_cap_pct 派生（默认
50%）；老任务无任何新语义标记也直接按保证金加载（用户定
版：不加提示事件/标记）；full_auto 组装顺序 = 用户实填 >
内置中性档（NEUTRAL_DEFAULTS）> dataclass 默认。
"""

import pytest

from detsp.core.auto_cycle import AutoCycleRuntime
from detsp.core.config import build_config
from detsp.core.engine import Engine, build_task
from detsp.core.ledger import Ledger
from detsp.strategies.premium_arb import PremiumParams
from detsp.venues.paper import PaperVenue

PRICES = {"BTC": 60_000.0, "ETH": 3_000.0}


def _venues():
    a = PaperVenue(venue_id="arcus", prices=dict(PRICES), leverage=5.0)
    b = PaperVenue(venue_id="lighter", prices=dict(PRICES), leverage=5.0)
    a.set_funding("BTC", 0.0001)
    b.set_funding("BTC", 0.0005)
    return {"arcus": a, "lighter": b}


def _engine(task):
    cfg = build_config(env={}, yaml_data={"tasks": [task]})
    engine = Engine.from_config(cfg, _venues(), ledger=Ledger())
    assert task["name"] not in engine.errors, engine.errors
    return engine


# ── 资金费：保证金 × 杠杆 = 单边名义 ───────────────────────

def test_funding_margin_converts_to_notional():
    engine = _engine({
        "name": "f-margin", "strategy": "funding_carry",
        "amount_usd": 100,
        "params": {"venue_a": "arcus", "venue_b": "lighter",
                   "symbol": "BTC", "leverage": 6}})
    assert engine.tasks[0].runtime.params.notional_usd == 600.0


def test_funding_explicit_notional_wins_over_margin():
    engine = _engine({
        "name": "f-explicit", "strategy": "funding_carry",
        "amount_usd": 100,
        "params": {"venue_a": "arcus", "venue_b": "lighter",
                   "symbol": "BTC", "leverage": 6,
                   "notional_usd": 250}})
    assert engine.tasks[0].runtime.params.notional_usd == 250.0


def test_old_task_without_marker_loads_as_margin():
    """老任务（无 amount_semantics 之类标记、还带着已删除的
    run_preset）直接按保证金解释加载，不报错、不需要任何
    额外字段（用户定版：不加提示与标记逻辑）。"""
    engine = _engine({
        "name": "f-legacy", "strategy": "funding_carry",
        "run_preset": "aggressive", "amount_usd": 50,
        "params": {"venue_a": "arcus", "venue_b": "lighter",
                   "symbol": "BTC", "leverage": 2}})
    task = engine.tasks[0]
    assert task.runtime.params.notional_usd == 100.0
    assert not hasattr(task, "run_preset")


# ── 溢价：保证金封顶最大持仓 + 单笔上限百分比派生 ─────────

_PREMIUM_BASE = {"base_venue": "arcus", "hedge_venue": "lighter",
                 "symbol": "BTC", "midline_bps": 0,
                 "upper_bps": 10, "lower_bps": 10}


def test_premium_margin_caps_position_and_derives_order_cap():
    engine = _engine({
        "name": "p-margin", "strategy": "premium_arb",
        "amount_usd": 100,
        "params": {**_PREMIUM_BASE, "leverage_base": 1.0,
                   "order_cap_pct": 50}})
    p = engine.tasks[0].runtime.params
    # 最大持仓被保证金名义（100 × 1）封顶到 100（默认 1000）
    assert p.max_position_usd == 100.0
    # 单笔上限 = 生效最大持仓 × 50%
    assert p.max_order_notional_usd == 50.0
    # 费率改内置常量：两腿 taker 都是 5 bps
    assert p.base_taker_fee_bps == 5.0
    assert p.hedge_taker_fee_bps == 5.0


def test_premium_order_cap_pct_quarter():
    engine = _engine({
        "name": "p-pct25", "strategy": "premium_arb",
        "params": {**_PREMIUM_BASE, "max_position_usd": 1000,
                   "order_cap_pct": 25}})
    p = engine.tasks[0].runtime.params
    assert p.max_order_notional_usd == 250.0


def test_premium_legacy_explicit_order_cap_preserved():
    """老配置显式写了单笔上限、没写百分比：不派生、原值保留
    （百分比只在任务参数里带了 order_cap_pct 时才生效）。"""
    engine = _engine({
        "name": "p-legacy", "strategy": "premium_arb",
        "params": {**_PREMIUM_BASE, "max_position_usd": 1000,
                   "max_order_notional_usd": 123}})
    p = engine.tasks[0].runtime.params
    assert p.max_order_notional_usd == 123.0


def test_premium_order_cap_pct_defaults_and_validation():
    base = {"midline_bps": 0.0, "upper_bps": 10.0, "lower_bps": 10.0}
    assert PremiumParams(**base).order_cap_pct == 50.0
    with pytest.raises(ValueError):
        PremiumParams(order_cap_pct=0, **base)
    with pytest.raises(ValueError):
        PremiumParams(order_cap_pct=100.5, **base)


# ── full_auto 组装：用户实填 > 内置中性档 > 默认 ───────────

def test_full_auto_merges_neutral_defaults():
    rt = AutoCycleRuntime("funding_carry", {}, executor=None,
                          selection_scope="all_pairs")
    merged = rt._merged_full_auto(
        "BTC", {"venue_a": "arcus", "venue_b": "lighter"})
    assert merged["leverage"] == 6.0            # 内置中性档
    assert merged["min_edge_bps_per_hour"] == 1.0
    assert merged["venue_a"] == "arcus" and merged["symbol"] == "BTC"
    # 用户实填优先于中性档
    rt2 = AutoCycleRuntime("funding_carry", {"leverage": 2.0},
                           executor=None, selection_scope="all_pairs")
    merged2 = rt2._merged_full_auto(
        "BTC", {"venue_a": "arcus", "venue_b": "lighter"})
    assert merged2["leverage"] == 2.0
    # 网格中性档：格数 20 / 杠杆 3,3 / mode long_short
    rt3 = AutoCycleRuntime("hedged_grid",
                           {"lower_price": 50_000.0,
                            "upper_price": 70_000.0},
                           executor=None, selection_scope="all_pairs",
                           amount_usd=6_000.0)
    merged3 = rt3._merged_full_auto(
        "BTC", {"venue": "arcus", "venue_b": "lighter",
                "price": 60_000.0})
    assert merged3["grid_count"] == 20
    assert merged3["leverage_grid"] == 3.0
    assert merged3["mode"] == "long_short"
