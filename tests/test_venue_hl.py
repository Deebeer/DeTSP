"""Hyperliquid 家族 live 适配测试：注入假 Info/Exchange，不联网。"""

import pytest

from detsp.core.venue import OrderRequest, Side
from detsp.venues.hl import HyperliquidVenue, entropy, hyperliquid, tradexyz

META = {"universe": [
    {"name": "BTC", "szDecimals": 5, "maxLeverage": 40},
    {"name": "io:BTC", "szDecimals": 3, "maxLeverage": 20, "onlyIsolated": True},
    {"name": "xyz:BTC", "szDecimals": 4, "maxLeverage": 10},
]}


class FakeInfo:
    def __init__(self):
        self.state = {
            "marginSummary": {"accountValue": "1234.5"},
            "withdrawable": "1000.0",
            "assetPositions": [{"position": {
                "coin": "BTC", "szi": "0.5", "entryPx": "60000",
                "leverage": {"value": 10}, "liquidationPx": "40000"}}],
        }

    def meta(self, dex=""):
        if dex == "":
            return {"universe": [META["universe"][0]]}
        return {"universe": [a for a in META["universe"]
                             if a["name"].startswith(dex + ":")]}

    def l2_snapshot(self, coin):
        return {"levels": [[{"px": "59999", "sz": "1"}],
                           [{"px": "60001", "sz": "1"}]], "time": 1700000000000}

    def user_state(self, address, dex=""):
        return self.state

    def open_orders(self, address, dex=""):
        return [{"coin": "BTC", "oid": 123, "side": "B", "sz": "0.3",
                 "origSz": "0.5", "limitPx": "59000"}]

    def meta_and_asset_ctxs(self, dex=""):
        meta = self.meta(dex)
        return meta, [{"funding": "0.0001"} for _ in meta["universe"]]


class FakeExchange:
    def __init__(self):
        self.calls = []
        self.resp = {"status": "ok", "response": {"data": {"statuses": [
            {"filled": {"totalSz": "0.5", "avgPx": "60000", "oid": 999}}]}}}

    def order(self, name, is_buy, sz, limit_px, order_type, reduce_only=False):
        self.calls.append(("order", name, is_buy, sz, limit_px, order_type,
                           reduce_only))
        return self.resp

    def market_open(self, name, is_buy, sz, px=None, slippage=0.05, cloid=None,
                    builder=None):
        self.calls.append(("market_open", name, is_buy, sz, slippage))
        return self.resp

    def cancel(self, name, oid):
        self.calls.append(("cancel", name, oid))
        return {"status": "ok"}


@pytest.fixture()
def venue():
    return HyperliquidVenue(dex="", venue_id="hyperliquid", mode="live",
                            account_address="0xabc", info=FakeInfo(),
                            exchange=FakeExchange())


def test_coin_prefix_by_dex():
    assert hyperliquid(mode="paper").coin("BTC") == "BTC"
    assert entropy(mode="paper").coin("BTC") == "io:BTC"
    assert tradexyz(mode="paper").coin("BTC") == "xyz:BTC"


def test_markets_main_dex(venue):
    ms = venue.markets()
    assert [m.symbol for m in ms] == ["BTC"]
    assert ms[0].qty_step == 10 ** -5
    assert ms[0].max_leverage == 40
    assert ms[0].min_notional == 10.0


def test_markets_sub_dex_strips_prefix():
    v = HyperliquidVenue(dex="io", venue_id="entropy", mode="live",
                         account_address="0xabc", info=FakeInfo(),
                         exchange=FakeExchange())
    ms = v.markets()
    assert [m.symbol for m in ms] == ["BTC"]  # io:BTC 剥前缀
    assert ms[0].max_leverage == 20


def test_book(venue):
    b = venue.book("BTC")
    assert b.bid == 59999 and b.ask == 60001 and b.mid == 60000


def test_balance_and_positions(venue):
    assert venue.balance() == {"equity": 1234.5, "available": 1000.0}
    ps = venue.positions()
    assert len(ps) == 1
    assert ps[0].qty == 0.5 and ps[0].entry_price == 60000
    assert ps[0].liq_price == 40000 and ps[0].leverage == 10


def test_funding_hourly_rate(venue):
    f = venue.funding("BTC")
    assert f.rate == 0.0001


def test_open_orders_mapping(venue):
    os_ = venue.open_orders()
    assert os_[0].order_id == "123" and os_[0].side is Side.BUY
    assert os_[0].filled_qty == pytest.approx(0.2)


def test_place_limit_order_gtc_and_filled(venue):
    o = venue.place_order(OrderRequest("BTC", Side.BUY, 0.5, price=60000.0))
    kind, name, is_buy, sz, px, otype, ro = venue._exchange.calls[-1]
    assert kind == "order" and name == "BTC" and is_buy and sz == 0.5
    assert otype == {"limit": {"tif": "Gtc"}}
    assert o.status == "filled" and o.order_id == "999" and o.filled_qty == 0.5


def test_place_post_only_alo(venue):
    venue._exchange.resp = {"status": "ok", "response": {"data": {"statuses": [
        {"resting": {"oid": 555}}]}}}
    o = venue.place_order(OrderRequest("BTC", Side.SELL, 0.1, price=61000.0,
                                       post_only=True))
    assert venue._exchange.calls[-1][5] == {"limit": {"tif": "Alo"}}
    assert o.status == "open" and o.order_id == "555"


def test_place_market_uses_market_open(venue):
    venue.place_order(OrderRequest("BTC", Side.BUY, 0.1))
    assert venue._exchange.calls[-1][0] == "market_open"


def test_place_ioc_no_match_is_canceled_not_error():
    ex = FakeExchange()
    ex.resp = {"status": "ok", "response": {"data": {"statuses": [
        {"error": "Order could not immediately match against any resting orders."}]}}}
    v = HyperliquidVenue(mode="live", account_address="0xabc",
                         info=FakeInfo(), exchange=ex)
    o = v.place_order(OrderRequest("BTC", Side.BUY, 0.1))
    assert o.status == "canceled"


def test_place_rejected_raises():
    ex = FakeExchange()
    ex.resp = {"status": "ok", "response": {"data": {"statuses": [
        {"error": "Insufficient margin"}]}}}
    v = HyperliquidVenue(mode="live", account_address="0xabc",
                         info=FakeInfo(), exchange=ex)
    with pytest.raises(RuntimeError, match="拒单"):
        v.place_order(OrderRequest("BTC", Side.BUY, 0.1))


def test_cancel_uses_cached_coin(venue):
    venue.open_orders()  # 填充 oid→coin 缓存
    assert venue.cancel_order("123") is True
    assert venue._exchange.calls[-1] == ("cancel", "BTC", 123)


def test_px_rounding_rules():
    # HL：最多 5 位有效数字、小数位 ≤ 6 − szDecimals
    assert HyperliquidVenue._round_px(60000.123456, 5) == 60000.0
    assert HyperliquidVenue._round_px(0.123456789, 2) == 0.1235
    assert HyperliquidVenue._round_px(1234.5678, 3) == 1234.6


def test_live_without_key_cannot_trade():
    v = HyperliquidVenue(mode="live", account_address="0xabc", info=FakeInfo())
    with pytest.raises(RuntimeError, match="HL_PRIVATE_KEY"):
        v.place_order(OrderRequest("BTC", Side.BUY, 0.1))
