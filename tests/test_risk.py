"""风控纯函数测试。"""

import pytest

from detsp.core.risk import (
    ErrorCounter,
    KillSwitch,
    RiskHaltError,
    basis_ok,
    corridor_ok,
    detect_single_leg,
    liquidation_corridor_pct,
    net_delta,
    should_force_close,
    within_cap,
    worst_corridor_pct,
)
from detsp.core.venue import Side


def test_corridor_long():
    # 多头：现价 100，强平 90 → 距离 10%
    assert liquidation_corridor_pct(100, 90, Side.BUY) == pytest.approx(10.0)


def test_corridor_short():
    assert liquidation_corridor_pct(100, 110, Side.SELL) == pytest.approx(10.0)


def test_corridor_invalid_price():
    with pytest.raises(ValueError):
        liquidation_corridor_pct(0, 90, Side.BUY)


def test_worst_corridor():
    assert worst_corridor_pct([8.0, 3.5, 12.0]) == 3.5
    assert worst_corridor_pct([]) is None


def test_corridor_ok():
    assert corridor_ok(3.0, 3.0)
    assert not corridor_ok(2.9, 3.0)


def test_force_close_lines():
    assert should_force_close(1.4, 10.0, 1.5)          # 跌破平仓线
    assert should_force_close(4.9, 10.0, 1.5)          # 只剩开仓时一半
    assert not should_force_close(6.0, 10.0, 1.5)


def test_basis_gate():
    assert basis_ok(100, 102, 3.0)
    assert not basis_ok(100, 104, 3.0)
    assert not basis_ok(100, 0, 3.0)


def test_single_leg():
    assert detect_single_leg({"arcus": 1.0, "lighter": 0.0})
    assert not detect_single_leg({"arcus": 1.0, "lighter": -1.0})
    assert not detect_single_leg({"arcus": 0.0, "lighter": 0.0})
    # 数量明显不平也算不平衡
    assert detect_single_leg({"arcus": 1.0, "lighter": -0.5})


def test_net_delta():
    assert net_delta({"a": 1.0, "b": -1.0}) == 0.0
    assert net_delta({"a": 1.0, "b": -0.6}) == pytest.approx(0.4)


def test_caps():
    assert within_cap(100, 100)
    assert not within_cap(101, 100)
    assert within_cap(10**9, 0)  # cap<=0 不限


def test_kill_switch():
    ks = KillSwitch()
    ks.assert_allowed()
    ks.halt("测试")
    assert ks.halted
    with pytest.raises(RiskHaltError):
        ks.assert_allowed()
    ks.reset()
    ks.assert_allowed()


def test_error_counter():
    ec = ErrorCounter(max_consecutive=3)
    assert not ec.record_error()
    assert not ec.record_error()
    ec.record_success()
    assert ec.count == 0
    assert not ec.record_error()
    assert not ec.record_error()
    assert ec.record_error()
