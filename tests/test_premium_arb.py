"""溢价带（premium band）计算测试。"""

import pytest

from detsp.core.venue import Side
from detsp.strategies.premium_arb import (
    PremiumParams,
    band_signal,
    buy_threshold_bps,
    executable_signal,
    inventory_surcharge_bps,
    maker_open_price,
    premium_bps,
    round_trip_floor_bps,
    sell_threshold_bps,
    slice_notional,
)


def make(**kw) -> PremiumParams:
    base = dict(midline_bps=5.0, upper_bps=10.0, lower_bps=10.0)
    base.update(kw)
    return PremiumParams(**base)


def test_premium_bps():
    assert premium_bps(101, 100) == pytest.approx(100.0)
    assert premium_bps(100, 100) == 0.0
    with pytest.raises(ValueError):
        premium_bps(0, 100)


def test_thresholds():
    p = make()
    assert sell_threshold_bps(p) == 15.0
    # 中枢 5、下带 10 → 买入门槛 -5，可为负属预期设计
    assert buy_threshold_bps(p) == -5.0


def test_band_signal():
    p = make()
    assert band_signal(16, p) == "sell_base"
    assert band_signal(-6, p) == "buy_base"
    assert band_signal(0, p) is None
    assert band_signal(14.9, p) is None


def test_round_trip_floor():
    assert round_trip_floor_bps(make()) == 20.0


def test_executable_signal_with_fees():
    # 无费：base bid 相对 hedge ask 溢价 20bps ≥ 门槛 15 → 卖 base
    p = make()
    assert executable_signal(100.2, 100.3, 99.9, 100.0, p) == "sell_base"
    # 双边共 30bps 费：20bps 溢价扣费后不够门槛 → 无信号
    pf = make(base_taker_fee_bps=15.0, hedge_taker_fee_bps=15.0)
    assert executable_signal(100.2, 100.3, 99.9, 100.0, pf) is None
    # 买 base：hedge bid 相对 base ask 折价 60bps ≥ 门槛 lower-midline=5bps
    assert executable_signal(99.9, 100.0, 100.6, 100.7, p) == "buy_base"


def test_slice_notional():
    p = make(max_order_notional_usd=500.0, take_fraction=0.5)
    assert slice_notional(800, p) == 400.0     # 深度 800 × 0.5
    assert slice_notional(4000, p) == 500.0    # 被单笔上限截断
    assert slice_notional(0, p) == 0.0


def test_inventory_surcharge():
    p = make(max_position_usd=1000.0)
    assert inventory_surcharge_bps(400, p) == 0.0            # 未过半仓线
    assert inventory_surcharge_bps(1000, p) == pytest.approx(10.0)  # 满仓满阶梯
    assert 0 < inventory_surcharge_bps(750, p) < 10.0


def test_invalid_params():
    with pytest.raises(ValueError):
        make(upper_bps=0)
    with pytest.raises(ValueError):
        make(take_fraction=0)
    with pytest.raises(ValueError):
        make(execution_mode="iceberg")   # 未知执行模式
    with pytest.raises(ValueError):
        make(maker_timeout_seconds=0)


def test_maker_open_price_inside_book():
    # 默认贴盘口内侧：买贴 bid、卖贴 ask
    assert maker_open_price(99.9, 100.1, Side.BUY, 0.1) == pytest.approx(99.9)
    assert maker_open_price(99.9, 100.1, Side.SELL, 0.1) == pytest.approx(100.1)
    # offset 向内侧推进 1 tick
    assert maker_open_price(99.9, 100.1, Side.BUY, 0.1, 1) == \
        pytest.approx(100.0)
    assert maker_open_price(99.9, 100.1, Side.SELL, 0.1, 1) == \
        pytest.approx(100.0)
    # offset 过大也不穿价：买价封顶 ask−tick、卖价封底 bid+tick
    assert maker_open_price(99.9, 100.1, Side.BUY, 0.1, 9) == \
        pytest.approx(100.0)
    assert maker_open_price(99.9, 100.1, Side.SELL, 0.1, 9) == \
        pytest.approx(100.0)
    # tick<=0 时直接贴盘口
    assert maker_open_price(99.9, 100.1, Side.BUY, 0.0, 3) == \
        pytest.approx(99.9)
