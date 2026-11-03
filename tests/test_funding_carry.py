"""资金费 carry 测试：纯函数优势/选向 + paper 端到端（入场→
持有→优势消失退出/最长持有/冷却重开）+ 启动对账接管与修正 +
孤腿与停机守卫。"""

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
                notional_usd=300, min_entry_edge_bps_per_hour=1.0)
    with pytest.raises(ValueError):
        CarryParams(**{**base, "venue_b": "arcus"})
    with pytest.raises(ValueError):
        # 退出门槛必须小于入场门槛（迟滞）
        CarryParams(**{**base, "exit_edge_bps_per_hour": 1.0})
    with pytest.raises(ValueError):
        CarryParams(**{**base, "exit_confirm_checks": 0})


# ── 端到端 ──

def _venues(fa=0.0001, fb=0.0005):
    a = PaperVenue(venue_id="arcus", prices=dict(BTC), leverage=5.0)
    b = PaperVenue(venue_id="lighter", prices=dict(BTC), leverage=5.0)
    a.set_funding("BTC", fa)   # 每小时口径
    b.set_funding("BTC", fb)
    return {"arcus": a, "lighter": b}


def _build(venues, **params):
    base = {"symbol": "BTC", "venue_a": "arcus", "venue_b": "lighter",
            "notional_usd": 300, "leverage": 5,
            "min_entry_edge_bps_per_hour": 2.0,
            "exit_edge_bps_per_hour": 1.0, "exit_confirm_checks": 2,
            "check_seconds": 10, "cooldown_seconds": 30}
    base.update(params)
    y = {"tasks": [{"name": "carry", "strategy": "funding_carry",
                    "params": base}]}
    cfg = build_config(env={}, yaml_data=y)
    engine = Engine.from_config(cfg, venues, ledger=Ledger())
    assert "carry" not in engine.errors, engine.errors
    return engine


def _legs(engine):
    pa = engine.executor.position("arcus", "BTC")
    pb = engine.executor.position("lighter", "BTC")
    return (pa.qty if pa else 0.0), (pb.qty if pb else 0.0)


def test_entry_hold_and_edge_exit_with_hysteresis():
    venues = _venues()   # 优势 4 bps/h ≥ 入场 2
    engine = _build(venues)
    rt = engine.tasks[0].runtime
    engine.tick(now=1000)
    # 多低费率 arcus、空高费率 lighter，等量 300/60000=0.005
    assert _legs(engine) == (pytest.approx(0.005), pytest.approx(-0.005))
    assert rt.phase == "open" and rt.long_leg == "a"

    # 优势跌到 0.2 bps/h（< 退出 1.0）：第 1 次检查不平（需连续 2 次）
    venues["lighter"].set_funding("BTC", 0.00012)
    engine.tick(now=1011)
    assert _legs(engine)[0] == pytest.approx(0.005)
    assert rt._below_exit_count == 1
    engine.tick(now=1022)   # 第 2 次确认 → 双平进冷却
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


def test_max_hold_and_reopen_after_cooldown():
    venues = _venues()
    engine = _build(venues, max_hold_seconds=50.0)
    engine.tick(now=1000)
    assert _legs(engine)[0] == pytest.approx(0.005)
    engine.tick(now=1051)   # 超过最长持有 → 平仓进冷却（30s）
    assert _legs(engine) == (0.0, 0.0)
    engine.tick(now=1070)   # 冷却未完（到 1081）：不开
    assert _legs(engine) == (0.0, 0.0)
    engine.tick(now=1082)   # 冷却完 + 检查周期到 + 优势还在 → 重开
    assert _legs(engine)[0] == pytest.approx(0.005)


def test_reopen_disabled_stays_flat():
    venues = _venues()
    engine = _build(venues, max_hold_seconds=50.0, reopen=False)
    engine.tick(now=1000)
    assert _legs(engine)[0] == pytest.approx(0.005)
    engine.tick(now=1051)
    assert _legs(engine) == (0.0, 0.0)
    engine.tick(now=1200)   # 冷却早过、优势还在，但 reopen=False
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
