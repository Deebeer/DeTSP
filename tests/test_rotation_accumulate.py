"""轮换对冲逐步建仓（max_position_qty）测试。

语义源自 perp-dex-tools 的 hedge --max-position：逐档建仓到上限，
达到后转入平仓循环。DeTSP 口径：首轮按资金费选向开仓，之后在
open 阶段按间隔同向双腿加仓，持有期仍从首轮开仓起算。
"""

import pytest

from detsp.core.config import build_config
from detsp.core.engine import Engine
from detsp.core.ledger import Ledger
from detsp.strategies.rotation_hedge import accumulation_step_qty
from detsp.venues.paper import PaperVenue

BTC = {"BTC": 60_000.0}


def test_accumulation_step_qty():
    assert accumulation_step_qty(0.0, 0.005, 0.0) == 0.005    # 禁用原样
    assert accumulation_step_qty(0.005, 0.005, 0.012) == 0.005
    assert accumulation_step_qty(0.010, 0.005, 0.012) == pytest.approx(0.002)
    assert accumulation_step_qty(0.012, 0.005, 0.012) == 0.0  # 已达上限
    assert accumulation_step_qty(0.020, 0.005, 0.012) == 0.0  # 超过上限


def _venues():
    a = PaperVenue(venue_id="arcus", prices=dict(BTC), leverage=5.0)
    b = PaperVenue(venue_id="lighter", prices=dict(BTC), leverage=5.0)
    a.set_funding("BTC", 0.0001)   # 低费率 → 做多
    b.set_funding("BTC", 0.0005)   # 高费率 → 做空
    return {"arcus": a, "lighter": b}


def _build(max_position_qty):
    y = {"tasks": [{
        "name": "hedge", "strategy": "rotation_hedge",
        "params": {"symbol": "BTC", "venue_a": "arcus", "venue_b": "lighter",
                   "notional_usd": 300, "hold_seconds": 60,
                   "cooldown_seconds": 60, "leverage_a": 5, "leverage_b": 5,
                   "max_position_qty": max_position_qty,
                   "accumulate_interval_seconds": 10}}]}
    cfg = build_config(env={}, yaml_data=y)
    engine = Engine.from_config(cfg, _venues(), ledger=Ledger())
    assert "hedge" not in engine.errors, engine.errors
    return engine


def _legs(engine):
    pa = engine.executor.position("arcus", "BTC")
    pb = engine.executor.position("lighter", "BTC")
    return (pa.qty if pa else 0.0), (pb.qty if pb else 0.0)


def test_accumulates_to_max_then_rotates():
    engine = _build(0.012)
    engine.tick(now=1000)                       # 首轮：0.005 / −0.005
    assert _legs(engine) == (pytest.approx(0.005), pytest.approx(-0.005))
    engine.tick(now=1005)                       # 间隔未到（5<10）：不加仓
    assert _legs(engine)[0] == pytest.approx(0.005)
    engine.tick(now=1011)                       # 加一档 → 0.010
    assert _legs(engine) == (pytest.approx(0.010), pytest.approx(-0.010))
    engine.tick(now=1022)                       # 末档截断到上限 0.012
    assert _legs(engine) == (pytest.approx(0.012), pytest.approx(-0.012))
    engine.tick(now=1033)                       # 已达上限：不再加仓
    assert _legs(engine)[0] == pytest.approx(0.012)
    engine.tick(now=1061)                       # 持有到期（自首轮起 60s）双平
    assert _legs(engine) == (0.0, 0.0)
    assert engine.tasks[0].runtime.phase == "cooldown"


def test_disabled_keeps_original_behavior():
    engine = _build(0.0)                        # 0=禁用：一轮开满即持有
    engine.tick(now=1000)
    assert _legs(engine)[0] == pytest.approx(0.005)
    engine.tick(now=1011)                       # 不会加仓
    assert _legs(engine)[0] == pytest.approx(0.005)
    engine.tick(now=1061)
    assert _legs(engine) == (0.0, 0.0)
