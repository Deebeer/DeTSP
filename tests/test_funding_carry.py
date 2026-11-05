"""资金费 carry 测试：纯函数优势/选向 + paper 端到端（入场→
持有→费差跌破最低费差线退出/冷却重开）+ 启动对账接管与修正 +
孤腿与停机守卫 + 第十一轮五项新语义（最大开仓价差闸门、数量
偏差两段式、强平保护取参数、老任务迁移、entry_gate 复检）。"""

import pytest

from detsp.core.config import build_config
from detsp.core.engine import Engine
from detsp.core.ledger import Ledger
from detsp.core.venue import OrderRequest, Side
from detsp.strategies.funding_carry import (
    CarryParams,
    best_carry_direction,
    carry_edge_bps_per_hour,
    current_edge_bps_per_hour,
)
from detsp.venues.paper import PaperVenue

BTC = {"BTC": 60_000.0}


# ── 纯函数 ──

def test_edge_math():
    # 多腿费率 0.0001/h、空腿 0.0005/h → 净收 0.0004 = 4 bps/小时
    assert carry_edge_bps_per_hour(0.0001, 0.0005) == pytest.approx(4.0)
    # 费率反转时优势为负
    assert carry_edge_bps_per_hour(0.0005, 0.0001) == pytest.approx(-4.0)


def test_best_direction_picks_low_long_high_short():
    leg, edge = best_carry_direction(0.0001, 0.0005)
    assert leg == "a" and edge == pytest.approx(4.0)     # 多低费率 a
    leg, edge = best_carry_direction(0.0005, 0.0001)
    assert leg == "b" and edge == pytest.approx(4.0)     # 多低费率 b
    leg, edge = best_carry_direction(0.0003, 0.0003)
    assert leg == "a" and edge == pytest.approx(0.0)     # 相等默认多 a


def test_current_edge_follows_held_direction():
    assert current_edge_bps_per_hour("a", 0.0001, 0.0005) == \
        pytest.approx(4.0)
    assert current_edge_bps_per_hour("b", 0.0001, 0.0005) == \
        pytest.approx(-4.0)


def test_invalid_params():
    base = dict(venue_a="arcus", venue_b="lighter", symbol="BTC",
                notional_usd=300, min_edge_bps_per_hour=1.0)
    with pytest.raises(ValueError):
        CarryParams(**{**base, "venue_b": "arcus"})
    with pytest.raises(ValueError):
        CarryParams(**{**base, "min_edge_bps_per_hour": 0})
    with pytest.raises(ValueError):
        CarryParams(**{**base, "max_open_spread_bps": 0})
    with pytest.raises(ValueError):
        CarryParams(**{**base, "close_corridor_pct": 0})
    with pytest.raises(ValueError):
        CarryParams(**{**base, "qty_dev_close_pct": -1})
    with pytest.raises(ValueError):
        CarryParams(**{**base, "exit_confirm_checks": 0})


def test_new_param_defaults():
    # 第十一轮新字段与默认值（出处见任务定案）
    p = CarryParams(venue_a="arcus", venue_b="lighter", symbol="BTC",
                    notional_usd=300)
    assert p.leverage == 6.0
    assert p.min_edge_bps_per_hour == 1.0
    assert p.max_open_spread_bps == 300
    assert p.close_corridor_pct == 1.5
    assert p.qty_dev_close_pct == 0.0
    assert p.reopen is True
    # 系统值（不进面板）
    assert p.exit_confirm_checks == 3
    assert p.check_seconds == 30.0
    assert p.cooldown_seconds == 60.0
    assert p.settle_grace_seconds == 10.0
    assert p.max_slippage_bps == 20.0
    # 已删除字段不得存在
    for gone in ("min_entry_edge_bps_per_hour",
                 "exit_edge_bps_per_hour", "max_hold_seconds"):
        assert not hasattr(p, gone)


# ── 端到端 ──

def _venues(fa=0.0001, fb=0.0005):
    a = PaperVenue(venue_id="arcus", prices=dict(BTC), leverage=5.0)
    b = PaperVenue(venue_id="lighter", prices=dict(BTC), leverage=5.0)
    a.set_funding("BTC", fa)   # 每小时口径
    b.set_funding("BTC", fb)
    return {"arcus": a, "lighter": b}


def _build(venues, _run_mode=None, _scanner=None, **params):
    # 第十轮夹具调整：本文件验的是 carry 原生语义（门槛入场/
    # 迟滞退出/重开），缺省自主（manual）已改为立即开仓且不
    # 自动退出，故缺省钉 semi_fixed（原生循环）；显式传模式
    # 的用例（全自动重开）不受影响
    # 系统值（exit_confirm_checks=3、check_seconds=30、
    # cooldown_seconds=60）是 hidden 字段：写进 params 会被
    # validate 静默丢弃、走 dataclass 默认，故夹具不写它们，
    # 用例时序按系统默认排
    base = {"symbol": "BTC", "venue_a": "arcus", "venue_b": "lighter",
            "notional_usd": 300, "leverage": 5,
            "min_edge_bps_per_hour": 2.0}
    base.update(params)
    task = {"name": "carry", "strategy": "funding_carry", "params": base}
    task["run_mode"] = _run_mode or "semi_fixed"
    y = {"tasks": [task]}
    cfg = build_config(env={}, yaml_data=y)
    engine = Engine.from_config(cfg, venues, ledger=Ledger(),
                                scanner=_scanner)
    assert "carry" not in engine.errors, engine.errors
    return engine


class _FakeScanner:
    """全自动模式测试用假扫描器：BTC 费差恒 4 bps/小时（≥ 门槛）。"""

    def pairs(self, venue_a, venue_b, strategy):
        return {"rows": [{"symbol": "BTC", "price_a": 60_000.0,
                          "price_b": 60_000.0, "edge_bps_h": 4.0,
                          "has_price": True, "suspect": False}]}

    def candles(self, venue_id, symbol):
        return {"ok": False, "error": "假扫描器无 K 线"}


def _legs(engine):
    pa = engine.executor.position("arcus", "BTC")
    pb = engine.executor.position("lighter", "BTC")
    return (pa.qty if pa else 0.0), (pb.qty if pb else 0.0)


def test_entry_hold_and_edge_exit_below_min_line():
    venues = _venues()   # 优势 4 bps/h ≥ 最低费差线 2
    engine = _build(venues)
    rt = engine.tasks[0].runtime
    engine.tick(now=1000)
    # 多低费率 arcus、空高费率 lighter，等量 300/60000=0.005
    assert _legs(engine) == (pytest.approx(0.005), pytest.approx(-0.005))
    assert rt.phase == "open" and rt.long_leg == "a"

    # 优势跌到 0.2 bps/h（< 最低费差线 2，进出同一条线）：连续
    # 确认 exit_confirm_checks=3 次（系统值）才平
    venues["lighter"].set_funding("BTC", 0.00012)
    engine.tick(now=1031)
    assert _legs(engine)[0] == pytest.approx(0.005)
    assert rt._below_exit_count == 1
    engine.tick(now=1062)
    assert _legs(engine)[0] == pytest.approx(0.005)
    assert rt._below_exit_count == 2
    engine.tick(now=1093)   # 第 3 次确认 → 双平进冷却
    assert _legs(engine) == (0.0, 0.0)
    assert rt.phase == "cooldown"


def test_no_entry_below_threshold():
    venues = _venues(fa=0.0004, fb=0.0005)   # 优势只有 1 bps/h < 2
    engine = _build(venues)
    engine.tick(now=1000)
    engine.tick(now=1011)
    assert _legs(engine) == (0.0, 0.0)
    assert engine.tasks[0].runtime.phase == "flat"


def test_hourly_conversion_across_intervals():
    # lighter 是 8 小时费率 0.004 → 每小时 0.0005，与直给等价
    venues = _venues()
    venues["lighter"].set_funding("BTC", 0.004, interval_seconds=28800)
    engine = _build(venues)
    engine.tick(now=1000)
    assert _legs(engine) == (pytest.approx(0.005), pytest.approx(-0.005))


def test_edge_exit_and_reopen_after_cooldown():
    # 全自动（run_mode: auto + 假扫描器选标）：费差跌破最低费差
    # 线连续确认平仓 → 监督器冷却 → 费差恢复后重开下一轮
    venues = _venues()
    engine = _build(venues, _run_mode="auto", _scanner=_FakeScanner())
    engine.tick(now=1000)
    assert _legs(engine)[0] == pytest.approx(0.005)
    venues["lighter"].set_funding("BTC", 0.00012)   # 优势跌到 0.2
    engine.tick(now=1031)
    engine.tick(now=1062)
    assert _legs(engine)[0] == pytest.approx(0.005)   # 只确认 2 次
    engine.tick(now=1093)   # 第 3 次确认 → 内层平仓，监督器进冷却
    assert _legs(engine) == (0.0, 0.0)
    venues["lighter"].set_funding("BTC", 0.0005)    # 优势恢复
    engine.tick(now=1110)   # 监督器冷却未完：不开
    assert _legs(engine) == (0.0, 0.0)
    engine.tick(now=1124)   # 冷却完 → 重新选标 → 重开
    assert _legs(engine)[0] == pytest.approx(0.005)


def test_reopen_disabled_stays_flat():
    venues = _venues()
    engine = _build(venues, reopen=False)
    engine.tick(now=1000)
    assert _legs(engine)[0] == pytest.approx(0.005)
    venues["lighter"].set_funding("BTC", 0.00012)
    engine.tick(now=1031)
    engine.tick(now=1062)
    engine.tick(now=1093)   # 连续确认平仓
    assert _legs(engine) == (0.0, 0.0)
    venues["lighter"].set_funding("BTC", 0.0005)    # 优势恢复
    engine.tick(now=1300)   # 冷却早过、优势还在，但 reopen=False
    assert _legs(engine) == (0.0, 0.0)


def test_killswitch_flattens():
    venues = _venues()
    engine = _build(venues)
    engine.tick(now=1000)
    assert _legs(engine)[0] == pytest.approx(0.005)
    engine.kill_switch.halt("测试停机")
    engine.tick(now=1001)
    assert _legs(engine) == (0.0, 0.0)


def test_orphan_leg_closed_immediately():
    venues = _venues()
    engine = _build(venues)
    engine.tick(now=1000)
    # 外部平掉 lighter 腿 → 孤腿 arcus 立即被平
    venues["lighter"].place_order(OrderRequest("BTC", Side.BUY, 0.005))
    assert _legs(engine)[1] == 0.0
    engine.tick(now=1001)
    assert _legs(engine) == (0.0, 0.0)


# ── 启动对账 ──

def test_start_adopts_equal_legs():
    venues = _venues()
    venues["arcus"].place_order(OrderRequest("BTC", Side.BUY, 0.005))
    venues["lighter"].place_order(OrderRequest("BTC", Side.SELL, 0.005))
    engine = _build(venues)
    rt = engine.tasks[0].runtime
    assert rt.phase == "open" and rt.long_leg == "a"
    assert any("接管已有双腿" in e for e in engine.events)


def test_start_fixes_unequal_legs_by_delta():
    venues = _venues()
    venues["arcus"].place_order(OrderRequest("BTC", Side.BUY, 0.01))
    venues["lighter"].place_order(OrderRequest("BTC", Side.SELL, 0.005))
    engine = _build(venues)
    rt = engine.tasks[0].runtime
    # 净 delta 修正：arcus 多余的 0.005 被削掉，接管等量双腿
    assert _legs(engine) == (pytest.approx(0.005), pytest.approx(-0.005))
    assert rt.phase == "open"
    assert any("不等量" in e for e in engine.events)


def test_start_flattens_orphan_instead_of_adopting():
    venues = _venues()
    venues["arcus"].place_order(OrderRequest("BTC", Side.BUY, 0.005))
    engine = _build(venues)
    rt = engine.tasks[0].runtime
    assert _legs(engine) == (0.0, 0.0)     # 孤腿被平，回空仓
    assert rt.phase == "flat"
    engine.tick(now=1000)                  # 优势达标后正常入场
    assert _legs(engine)[0] == pytest.approx(0.005)


# ── 第十一轮五项新语义 ──

def test_open_spread_gate_blocks_wide_basis():
    # 两所价格差 70000/60000 ≈ 1667 bps > 默认最大开仓价差 300：
    # 费差达标也不开（防同名不同标的）
    a = PaperVenue(venue_id="arcus", prices={"BTC": 60_000.0},
                   leverage=5.0)
    b = PaperVenue(venue_id="lighter", prices={"BTC": 70_000.0},
                   leverage=5.0)
    a.set_funding("BTC", 0.0001)
    b.set_funding("BTC", 0.0005)
    engine = _build({"arcus": a, "lighter": b})
    engine.tick(now=1000)
    engine.tick(now=1031)
    assert _legs(engine) == (0.0, 0.0)
    # 放宽闸门到 2000 bps 后可开
    engine2 = _build({"arcus": a, "lighter": b},
                     max_open_spread_bps=2000)
    engine2.tick(now=1000)
    assert _legs(engine2)[0] != 0.0


def test_qty_dev_above_threshold_closes_both():
    venues = _venues()
    engine = _build(venues, qty_dev_close_pct=10.0)
    rt = engine.tasks[0].runtime
    engine.tick(now=1000)
    assert _legs(engine) == (pytest.approx(0.005), pytest.approx(-0.005))
    # 外部给 lighter 再加空 0.001：偏差 |0.005-0.006|/0.006≈16.7%
    venues["lighter"].place_order(OrderRequest("BTC", Side.SELL, 0.001))
    engine.tick(now=1001)
    assert _legs(engine) == (0.0, 0.0)     # 超阈值直接双平
    assert rt.phase == "cooldown"
    assert "数量偏差" in rt.last_error


def test_qty_dev_below_threshold_trims_to_match():
    venues = _venues()
    engine = _build(venues, qty_dev_close_pct=50.0)
    rt = engine.tasks[0].runtime
    engine.tick(now=1000)
    venues["lighter"].place_order(OrderRequest("BTC", Side.SELL, 0.001))
    engine.tick(now=1001)
    # 偏差 16.7% < 50%：走削腿修平，两腿回到等量、继续持有
    assert _legs(engine) == (pytest.approx(0.005), pytest.approx(-0.005))
    assert rt.phase == "open"


def test_close_corridor_uses_params_not_risk_cfg():
    # 强平保护平仓线取 params.close_corridor_pct（默认 risk 口径
    # 1.5 不会触发的距离，在参数线 99% 下立即双平）
    venues = _venues()
    engine = _build(venues, close_corridor_pct=99.0)
    rt = engine.tasks[0].runtime
    engine.tick(now=1000)
    assert _legs(engine)[0] == pytest.approx(0.005)
    engine.tick(now=1001)
    assert _legs(engine) == (0.0, 0.0)
    assert "强平距离" in rt.last_error


def test_entry_gate_blocks_then_allows():
    venues = _venues()
    engine = _build(venues)
    rt = engine.tasks[0].runtime
    rt.entry_gate = lambda va, vb, sym, edge: (False, "费差持续性不足")
    engine.tick(now=1000)
    assert _legs(engine) == (0.0, 0.0)
    assert rt.last_error == "费差持续性不足"
    rt.entry_gate = lambda va, vb, sym, edge: (True, "")
    engine.tick(now=1031)   # 过检查周期后复检通过 → 开仓
    assert _legs(engine)[0] == pytest.approx(0.005)


def test_old_task_params_migrated():
    # 老任务文件：min_entry → min_edge；exit/max_hold 静默丢弃
    venues = _venues()
    task = {"name": "carry-old", "strategy": "funding_carry",
            "run_mode": "semi_fixed",
            "params": {"symbol": "BTC", "venue_a": "arcus",
                       "venue_b": "lighter", "notional_usd": 300,
                       "leverage": 5,
                       "min_entry_edge_bps_per_hour": 2.0,
                       "exit_edge_bps_per_hour": 1.0,
                       "max_hold_seconds": 50.0}}
    cfg = build_config(env={}, yaml_data={"tasks": [task]})
    engine = Engine.from_config(cfg, venues, ledger=Ledger())
    assert "carry-old" not in engine.errors, engine.errors
    p = engine.tasks[0].runtime.params
    assert p.min_edge_bps_per_hour == 2.0
    engine.tick(now=1000)   # 迁移后按新语义正常开仓
    assert _legs(engine)[0] == pytest.approx(0.005)
