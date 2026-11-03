"""Lighter live 适配测试：假 httpx 传输 + 假签名器，不联网。"""

import httpx
import pytest

from detsp.core.venue import OrderRequest, Side
from detsp.venues.lighter import LighterVenue, lighter_robinhood

ORDER_BOOKS = {"order_books": [
    {"symbol": "BTC", "status": "active", "market_id": 1,
     "supported_price_decimals": 2, "supported_size_decimals": 5,
     "min_base_amount": "0.0002", "min_quote_amount": "10"},
    {"symbol": "ETH", "status": "inactive", "market_id": 2,
     "supported_price_decimals": 2, "supported_size_decimals": 4,
     "min_base_amount": "0.005", "min_quote_amount": "10"},
]}
BOOK = {"bids": [{"price": "59999.0"}], "asks": [{"price": "60001.0"}]}
ACCOUNT = {"accounts": [{
    "total_asset_value": "5000", "available_balance": "4000",
    "positions": [{"market_id": 1, "sign": -1, "position": "0.25",
                   "avg_entry_price": "61000", "liquidation_price": "90000"}],
}]}
FUNDING = {"funding_rates": [
    {"exchange": "binance", "market_id": 1, "rate": 0.0001},
    {"exchange": "lighter", "market_id": 1, "rate": -0.0002},
]}
ACTIVE_ORDERS = {"orders": [
    {"order_index": 777, "client_order_index": 4242, "market_index": 1,
     "is_ask": False, "initial_base_amount": "0.1",
     "filled_base_amount": "0.04", "price": "59000"},
]}


def _handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/api/v1/orderBooks":
        return httpx.Response(200, json=ORDER_BOOKS)
    if path == "/api/v1/orderBookOrders":
        return httpx.Response(200, json=BOOK)
    if path == "/api/v1/account":
        return httpx.Response(200, json=ACCOUNT)
    if path == "/api/v1/funding-rates":
        return httpx.Response(200, json=FUNDING)
    if path == "/api/v1/accountActiveOrders":
        assert request.headers.get("authorization") == "tok"
        return httpx.Response(200, json=ACTIVE_ORDERS)
    return httpx.Response(404, json={})


class FakeResp:
    code = 200
    message = ""


class FakeSigner:
    """模拟 lighter-sdk 的 SignerClient（下单方法是 async）。"""

    def __init__(self):
        self.orders = []
        self.cancels = []

    def check_client(self):
        return None

    def create_auth_token_with_expiry(self):
        return "tok", None

    async def create_order(self, **kw):
        self.orders.append(kw)
        return object(), FakeResp(), None

    async def cancel_order(self, **kw):
        self.cancels.append(kw)
        return object(), FakeResp(), None


@pytest.fixture()
def venue():
    http = httpx.Client(base_url="https://example.invalid",
                        transport=httpx.MockTransport(_handler))
    return LighterVenue(deployment="mainnet", mode="live",
                        secrets={"LIGHTER_ACCOUNT_INDEX": "7",
                                 "LIGHTER_API_KEY_INDEX": "4"},
                        http=http, signer=FakeSigner())


def test_profiles():
    rh = lighter_robinhood(mode="paper")
    assert rh.profile.chain_id == 466324
    assert rh.profile.api_url == "https://api.rh.lighter.xyz"
    assert rh.venue_id == "lighter_rh"


def test_markets_only_active(venue):
    ms = venue.markets()
    assert [m.symbol for m in ms] == ["BTC"]
    assert ms[0].qty_step == 10 ** -5 and ms[0].min_notional == 10.0


def test_book(venue):
    b = venue.book("BTC")
    assert b.bid == 59999.0 and b.ask == 60001.0


def test_funding_filters_other_exchanges(venue):
    # funding-rates 混有别家费率，必须只取 exchange=lighter 的（8 小时口径）
    f = venue.funding("BTC")
    assert f.rate == -0.0002


def test_balance_and_positions(venue):
    assert venue.balance() == {"equity": 5000.0, "available": 4000.0}
    ps = venue.positions()
    assert len(ps) == 1
    assert ps[0].symbol == "BTC" and ps[0].qty == -0.25  # sign=-1 → 空头
    assert ps[0].liq_price == 90000.0


def test_open_orders(venue):
    os_ = venue.open_orders()
    assert os_[0].order_id == "777" and os_[0].side is Side.BUY
    assert os_[0].filled_qty == 0.04


def test_place_limit_order_units(venue):
    o = venue.place_order(OrderRequest("BTC", Side.BUY, 0.1, price=59000.0))
    kw = venue._signer.orders[-1]
    assert kw["market_index"] == 1
    assert kw["base_amount"] == 10000        # 0.1 × 10^5
    assert kw["price"] == 5900000            # 59000 × 10^2
    assert kw["is_ask"] is False
    assert kw["order_type"] == 0             # LIMIT
    assert o.order_id.startswith("coi:") and o.status == "open"


def test_place_market_order_protection(venue):
    venue.place_order(OrderRequest("BTC", Side.SELL, 0.05))
    kw = venue._signer.orders[-1]
    assert kw["order_type"] == 1             # MARKET
    # 卖单保护价 = 买一 × (1 − 1%) 再换整数
    assert kw["price"] == int(round(59999.0 * 0.99 * 100))


def test_place_post_only_tif(venue):
    from lighter import SignerClient

    venue.place_order(OrderRequest("BTC", Side.BUY, 0.1, price=58000.0,
                                   post_only=True))
    kw = venue._signer.orders[-1]
    assert kw["time_in_force"] == SignerClient.ORDER_TIME_IN_FORCE_POST_ONLY


def test_cancel_by_coi_resolves_order_index(venue):
    venue.place_order(OrderRequest("BTC", Side.BUY, 0.1, price=59000.0))
    coi = venue._signer.orders[-1]["client_order_index"]
    # 让活跃单里的 client_order_index 与刚下的单对上
    ACTIVE_ORDERS["orders"][0]["client_order_index"] = coi
    assert venue.cancel_order(f"coi:{coi}") is True
    kw = venue._signer.cancels[-1]
    assert kw["order_index"] == 777 and kw["market_index"] == 1


def test_place_rejected_raises(venue):
    class BadSigner(FakeSigner):
        async def create_order(self, **kw):
            return None, None, "insufficient funds"

    venue._signer = BadSigner()
    with pytest.raises(RuntimeError, match="被拒"):
        venue.place_order(OrderRequest("BTC", Side.BUY, 0.1, price=59000.0))
