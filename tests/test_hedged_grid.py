"""中性网格测试：纯函数（档位/铺单/对冲计划）+ 守卫（caps/
未托管/接管/冲突）+ 对冲死区与裸露超时强制对冲。"""

import pytest

from detsp.core.execution import Caps, Executor
from detsp.core.ledger import Ledger
from detsp.core.registry import ConflictError, OwnershipRegistry
from detsp.core.risk import KillSwitch
from detsp.core.venue import Side
from detsp.strategies.hedged_grid import (
    HedgedGridParams,
    HedgedGridStrategy,
    counter_order,
    estimated_notional,
    estimated_total_notional,
    grid_levels,
    grid_step,
    hedge_residual,
    hedge_target,
    out_of_range,
    plan_hedge_adjustment,
    plan_initial_orders,
    rung_gross_profit,
)
from detsp.venues.paper import PaperVenue

BTC = {"BTC": 60_000.0}


def make(**kw) -> HedgedGridParams:
    base = dict(grid_venue="arcus", hedge_venue="lighter", symbol="BTC",
                lower_price=90.0, upper_price=110.0, grid_count=10,
                qty_per_grid=0.1)
    base.update(kw)
    return HedgedGridParams(**base)


# ── 纯函数 ──

def test_levels_step_profit_notional():
    p = make()
    levels = grid_levels(p)
    assert len(levels) == 11
    assert levels[0] == 90.0 and levels[-1] == 110.0
    assert grid_step(p) == pytest.approx(2.0)
    assert rung_gross_profit(p) == pytest.approx(0.2)
    assert estimated_notional(p) == pytest.approx(100.0)
    # caps 口径：双腿总名义 = 单腿 × 2
    assert estimated_total_notional(p) == pytest.approx(200.0)


def test_plan_modes():
    both = plan_initial_orders(make(mode="long_short"), 100.0)
    assert any(o.side is Side.BUY for o in both)
    assert any(o.side is Side.SELL for o in both)
    longs = plan_initial_orders(make(mode="long"), 100.0)
    assert longs and all(o.side is Side.BUY for o in longs)
    shorts = plan_initial_orders(make(mode="short"), 100.0)
    assert shorts and all(o.side is Side.SELL for o in shorts)


def test_counter_order_reduce_only_by_mode():
    up = counter_order(make(mode="long_short"), 100.0, Side.BUY)
    assert up.side is Side.SELL and up.price == pytest.approx(102.0)
    assert not up.reduce_only
    # long 模式的卖单是止盈，只减仓；short 模式的买单同理
    assert counter_order(make(mode="long"), 100.0, Side.BUY).reduce_only
    assert counter_order(make(mode="short"), 100.0, Side.SELL).reduce_only
    assert out_of_range(make(), 111) and out_of_range(make(), 89)
    assert not out_of_range(make(), 100)


def test_hedge_pure_functions():
    assert hedge_target(0.3) == pytest.approx(-0.3)
    assert hedge_residual(0.3, -0.25) == pytest.approx(0.05)
    # 死区内不对冲
    assert plan_hedge_adjustment(0.01, 0.0, deadband_qty=0.02,
                                 min_qty=0.0) == 0.0
    # 残差达不到最小量：等累积；force 时绕过
    assert plan_hedge_adjustment(0.01, 0.0, deadband_qty=0.0,
                                 min_qty=0.02) == 0.0
    assert plan_hedge_adjustment(0.01, 0.0, deadband_qty=0.0,
                                 min_qty=0.02,
                                 force=True) == pytest.approx(-0.01)
    # 正常调平：目标 −0.01，当前 0 → 卖 0.01
    assert plan_hedge_adjustment(0.01, 0.0, 0.0, 0.001) == \
        pytest.approx(-0.01)
    # 过度对冲时反向买回多余部分
    assert plan_hedge_adjustment(0.01, -0.03, 0.0, 0.001) == \
        pytest.approx(0.02)


def test_invalid_params():
    with pytest.raises(ValueError):
        make(lower_price=110, upper_price=90)
    with pytest.raises(ValueError):
        make(grid_count=1)
    with pytest.raises(ValueError):
        make(mode="neutral")           # 旧模式名不再接受
    with pytest.raises(ValueError):
        make(out_of_range_action="recover")   # 旧动作名不再接受
    with pytest.raises(ValueError):
        make(hedge_venue="arcus")      # 两腿同所


# ── 守卫 ──

def _executor(v1, v2, caps=None):
    return Executor(venues={"arcus": v1, "lighter": v2},
                    registry=OwnershipRegistry(), ledger=Ledger(),
                    kill_switch=KillSwitch(), caps=caps)


def _big_params(**kw):
    base = dict(lower_price=55_000, upper_price=65_000, grid_count=10,
                qty_per_grid=0.01)
    base.update(kw)
    return make(**base)


def test_caps_reject_start_by_total_notional():
    # 单腿估算 6000、双腿 12000；策略层上限 10000 → 拒绝启动
    ex = _executor(PaperVenue(venue_id="arcus", prices=dict(BTC)),
                   PaperVenue(venue_id="lighter", prices=dict(BTC)),
                   caps=Caps(strategy={"hg": 10_000.0}))
    g = HedgedGridStrategy(_big_params(), executor=ex, owner="hg")
    with pytest.raises(RuntimeError, match="caps"):
        g.start()
    # 上限放到双腿总名义之上 → 正常启动
    ex2 = _executor(PaperVenue(venue_id="arcus", prices=dict(BTC)),
                    PaperVenue(venue_id="lighter", prices=dict(BTC)),
                    caps=Caps(strategy={"hg": 13_000.0}))
    g2 = HedgedGridStrategy(_big_params(), executor=ex2, owner="hg")
    g2.start()
    assert g2.started and not g2.stopped


def test_claim_conflict_between_tasks():
    ex = _executor(PaperVenue(venue_id="arcus", prices=dict(BTC)),
                   PaperVenue(venue_id="lighter", prices=dict(BTC)))
    p = _big_params()
    HedgedGridStrategy(p, executor=ex, owner="task-1").start()
    with pytest.raises(ConflictError):
        HedgedGridStrategy(p, executor=ex, owner="task-2").start()


def test_unmanaged_grid_position_blocks_and_takeover():
    from detsp.core.venue import OrderRequest

    v1 = PaperVenue(venue_id="arcus", prices=dict(BTC))
    v2 = PaperVenue(venue_id="lighter", prices=dict(BTC))
    v1.place_order(OrderRequest("BTC", Side.BUY, 0.05))   # 网格腿已有持仓
    ex = _executor(v1, v2)
    g = HedgedGridStrategy(_big_params(), executor=ex, owner="hg")
    g.start()
    assert g.unmanaged_position is True
    assert v1.open_orders("BTC") == []        # 不铺单
    g.tick(now=1)
    assert v1.open_orders("BTC") == []        # tick 也不动作
    assert ex.position("lighter", "BTC") is None   # 对冲腿不动

    v1b = PaperVenue(venue_id="arcus", prices=dict(BTC))
    v2b = PaperVenue(venue_id="lighter", prices=dict(BTC))
    v1b.place_order(OrderRequest("BTC", Side.BUY, 0.05))
    ex2 = _executor(v1b, v2b)
    g2 = HedgedGridStrategy(_big_params(takeover_position=True),
                            executor=ex2, owner="hg")
    g2.start()
    assert g2.unmanaged_position is False
    assert len(v1b.open_orders("BTC")) == 10  # 接管后正常铺单
    g2.tick(now=1)                            # 对冲腿调平到 −0.05
    pos_h = ex2.position("lighter", "BTC")
    assert pos_h is not None and pos_h.qty == pytest.approx(-0.05)


# ── 对冲死区 / 裸露超时 ──

def test_deadband_blocks_small_hedge():
    from detsp.core.config import build_config
    from detsp.core.engine import Engine

    y = {"tasks": [{"name": "hg", "strategy": "hedged_grid",
                    "params": {"grid_venue": "arcus",
                               "hedge_venue": "lighter", "symbol": "BTC",
                               "lower_price": 55_000, "upper_price": 65_000,
                               "grid_count": 10, "qty_per_grid": 0.01,
                               "mode": "long_short",
                               "out_of_range_action": "close",
                               "hedge_deadband_qty": 0.02}}]}
    cfg = build_config(env={}, yaml_data=y)
    v1 = PaperVenue(venue_id="arcus", prices=dict(BTC))
    v2 = PaperVenue(venue_id="lighter", prices=dict(BTC))
    engine = Engine.from_config(cfg, {"arcus": v1, "lighter": v2},
                                ledger=Ledger())
    v1.set_price("BTC", 58_900)   # 网格买成 0.01 < 死区 0.02
    engine.tick(now=1)
    assert engine.executor.position("arcus", "BTC").qty == \
        pytest.approx(0.01)
    assert engine.executor.position("lighter", "BTC") is None


class _BigMinQtyVenue(PaperVenue):
    """对冲所最小下单量很大：小残差永远达不到，只能等强制对冲。"""

    def markets(self):
        from detsp.core.venue import Market
        return [Market(symbol=s, price_step=0.1, qty_step=0.001,
                       min_qty=0.02, min_notional=10.0, max_leverage=50.0)
                for s in ("BTC",)]


def test_unhedged_timeout_forces_hedge():
    from detsp.core.config import build_config
    from detsp.core.engine import Engine

    y = {"tasks": [{"name": "hg", "strategy": "hedged_grid",
                    "params": {"grid_venue": "arcus",
                               "hedge_venue": "lighter", "symbol": "BTC",
                               "lower_price": 55_000, "upper_price": 65_000,
                               "grid_count": 10, "qty_per_grid": 0.01,
                               "mode": "long_short",
                               "out_of_range_action": "close",
                               "max_unhedged_seconds": 5}}]}
    cfg = build_config(env={}, yaml_data=y)
    v1 = PaperVenue(venue_id="arcus", prices=dict(BTC))
    v2 = _BigMinQtyVenue(venue_id="lighter", prices=dict(BTC))
    engine = Engine.from_config(cfg, {"arcus": v1, "lighter": v2},
                                ledger=Ledger())
    rt = engine.tasks[0].runtime
    v1.set_price("BTC", 58_900)   # 网格买成 0.01，残差达不到 min 0.02
    engine.tick(now=100)
    assert engine.executor.position("lighter", "BTC") is None
    engine.tick(now=104)          # 裸露 4s < 5s：仍等
    assert engine.executor.position("lighter", "BTC") is None
    engine.tick(now=106)          # 裸露超时：强制对冲（报警）
    pos_h = engine.executor.position("lighter", "BTC")
    assert pos_h is not None and pos_h.qty == pytest.approx(-0.01)
    assert rt.hedge_forced_count == 1
    assert "裸露超时" in rt.last_error
