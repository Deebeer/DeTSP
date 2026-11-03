"""网格层级计算测试。"""

import pytest

from detsp.core.venue import Side
from detsp.strategies.grid import (
    GridParams,
    counter_order,
    estimated_margin,
    estimated_notional,
    grid_levels,
    grid_step,
    out_of_range,
    plan_initial_orders,
    rung_gross_profit,
)


def make(**kw) -> GridParams:
    base = dict(lower=90.0, upper=110.0, grid_count=10, qty_per_grid=0.1)
    base.update(kw)
    return GridParams(**base)


def test_levels_and_step():
    p = make()
    levels = grid_levels(p)
    assert len(levels) == 11
    assert levels[0] == 90.0 and levels[-1] == 110.0
    assert grid_step(p) == pytest.approx(2.0)


def test_rung_profit_and_notional():
    p = make()
    assert rung_gross_profit(p) == pytest.approx(0.2)       # 2.0 × 0.1
    assert estimated_notional(p) == pytest.approx(100.0)    # 10 × 0.1 × 100
    assert estimated_margin(make(leverage=2.0)) == pytest.approx(50.0)


def test_plan_neutral():
    p = make(mode="neutral")
    orders = plan_initial_orders(p, current_price=100.0)
    buys = [o for o in orders if o.side is Side.BUY]
    sells = [o for o in orders if o.side is Side.SELL]
    assert buys and sells
    assert all(o.price < 100 for o in buys)
    assert all(o.price > 100 for o in sells)


def test_plan_long_only_buys():
    orders = plan_initial_orders(make(mode="long"), current_price=100.0)
    assert orders and all(o.side is Side.BUY for o in orders)


def test_plan_short_only_sells():
    orders = plan_initial_orders(make(mode="short"), current_price=100.0)
    assert orders and all(o.side is Side.SELL for o in orders)


def test_counter_order():
    p = make()
    up = counter_order(p, 100.0, Side.BUY)
    assert up.side is Side.SELL and up.price == pytest.approx(102.0)
    down = counter_order(p, 100.0, Side.SELL)
    assert down.side is Side.BUY and down.price == pytest.approx(98.0)
    # 做多网格的卖单是止盈，只减仓
    assert counter_order(make(mode="long"), 100.0, Side.BUY).reduce_only


def test_out_of_range():
    p = make()
    assert out_of_range(p, 111)
    assert out_of_range(p, 89)
    assert not out_of_range(p, 100)


def test_invalid_params():
    with pytest.raises(ValueError):
        make(lower=110, upper=90)
    with pytest.raises(ValueError):
        make(grid_count=1)
    with pytest.raises(ValueError):
        make(mode="weird")
