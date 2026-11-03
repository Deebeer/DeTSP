"""执行层单元测试：归属/上限/停机/结果未知/部分成交。"""

import pytest

from detsp.core.execution import Caps, Executor, UnknownOutcomeError
from detsp.core.registry import ConflictError
from detsp.core.risk import KillSwitch
from detsp.core.venue import Order, OrderRequest, Side
from detsp.venues.paper import PaperVenue


def make_executor(**kw):
    venue = PaperVenue(venue_id="arcus", prices={"BTC": 60_000.0})
    return Executor(venues={"arcus": venue}, **kw), venue


def test_place_claims_and_conflict_blocks_second_strategy():
    ex, _ = make_executor()
    res = ex.place("grid", "arcus",
                   OrderRequest("BTC", Side.BUY, 0.01), intent="open")
    assert res.status == "filled"
    # 同一 (venue, account, symbol) 已被 grid 占用，其他策略下单被拦截
    with pytest.raises(ConflictError):
        ex.place("premium_arb", "arcus",
                 OrderRequest("BTC", Side.BUY, 0.01), intent="open")


def test_venue_cap_rejects_open():
    ex, venue = make_executor(caps=Caps(venue={"arcus": 100.0}))
    res = ex.place("grid", "arcus",
                   OrderRequest("BTC", Side.BUY, 0.01), intent="open")
    assert res.status == "rejected"  # 0.01*60000=600 > 100
    assert venue.positions() == []


def test_corridor_gate_rejects_open():
    ex, venue = make_executor(min_corridor_pct=3.0)
    res = ex.place("rotation_hedge", "arcus",
                   OrderRequest("BTC", Side.BUY, 0.01),
                   intent="open", corridor_pct=2.0)
    assert res.status == "rejected"
    assert venue.positions() == []


def test_kill_switch_blocks_open_but_allows_close():
    ks = KillSwitch()
    ex, venue = make_executor(kill_switch=ks)
    ex.place("grid", "arcus", OrderRequest("BTC", Side.BUY, 0.01), intent="open")
    ks.halt("测试")
    with pytest.raises(Exception):
        ex.place("grid", "arcus",
                 OrderRequest("BTC", Side.BUY, 0.01), intent="open")
    # 平仓是安全动作，停机时照常执行
    res = ex.close_position("grid", "arcus", "BTC")
    assert res is not None and res.status == "filled"
    assert venue.positions() == []


class FlakyVenue(PaperVenue):
    """第一次下单抛结果未知，之后恢复正常；记录调用次数。"""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.calls = 0

    def place_order(self, req):
        self.calls += 1
        if self.calls == 1:
            raise UnknownOutcomeError("连接超时，回执丢失")
        return super().place_order(req)


def test_unknown_outcome_never_retries_and_reconciles():
    venue = FlakyVenue(venue_id="arcus", prices={"BTC": 60_000.0})
    ex = Executor(venues={"arcus": venue})
    res = ex.place("grid", "arcus",
                   OrderRequest("BTC", Side.BUY, 0.01), intent="open")
    assert res.status == "unknown"
    assert venue.calls == 1  # 绝不重发
    # 对账结果随返回值给出：该单实际未成交，持仓为空
    assert res.position is None


def test_partial_fill_recorded_by_actual_qty():
    ex, venue = make_executor()
    orig = venue.place_order

    def half(req):
        o = orig(req)
        o.filled_qty = req.qty / 2
        return o

    venue.place_order = half
    res = ex.place("grid", "arcus",
                   OrderRequest("BTC", Side.BUY, 0.1), intent="open")
    assert res.status == "partial"
    assert res.filled_qty == pytest.approx(0.05)
    assert ex.ledger.count("grid") == 1
    assert ex.ledger.total_volume("grid") == pytest.approx(0.05 * 60_000 * 1.0001,
                                                          rel=1e-3)


def test_cancel_only_own_orders():
    ex, venue = make_executor()
    res = ex.place("grid", "arcus",
                   OrderRequest("BTC", Side.BUY, 0.01, price=59_000.0,
                                post_only=True), intent="open")
    assert res.status == "open"
    with pytest.raises(ConflictError):
        ex.cancel("premium_arb", "arcus", res.order.order_id)
    assert ex.cancel("grid", "arcus", res.order.order_id) is True
    assert venue.open_orders("BTC") == []
