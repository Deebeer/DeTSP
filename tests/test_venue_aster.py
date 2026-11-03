"""Aster live 适配测试：官方文档示例签名向量对拍 + 假传输，不联网。

签名向量来自 Aster 官方 Futures V1 文档 general-info 的示例：
secret 与参数串固定时，HMAC-SHA256 结果是确定的公开值。
"""

import hashlib
import hmac as hmac_mod
from urllib.parse import parse_qsl, urlencode

import pytest

from detsp.core.venue import OrderRequest, Side
from detsp.venues.aster import AsterVenue, sign_params, to_base, to_pair

API_KEY = "test-api-key"
SECRET = "test-secret-key"

EXCHANGE_INFO = {"symbols": [{
    "symbol": "BTCUSDT", "contractType": "PERPETUAL", "status": "TRADING",
    "filters": [
        {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
        {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.002"},
        {"filterType": "MIN_NOTIONAL", "notional": "5"},
    ]}, {
    "symbol": "ETHUSDT", "contractType": "PERPETUAL", "status": "SUSPENDED",
    "filters": []}]}
BOOK_TICKER = {"bidPrice": "59999.0", "askPrice": "60001.0"}
PREMIUM = {"lastFundingRate": "0.0001", "nextFundingTime": 1700003600000}
BALANCE = [{"asset": "USDT", "balance": "8000", "availableBalance": "7500"}]
POSITION_RISK = [{"symbol": "BTCUSDT", "positionAmt": "-0.25",
                  "entryPrice": "61000", "leverage": "10",
                  "liquidationPrice": "90000"}]
OPEN_ORDERS = [{"orderId": 42, "symbol": "BTCUSDT", "side": "BUY",
                "origQty": "0.1", "executedQty": "0.0", "price": "59000",
                "status": "NEW"}]


class FakeTransport:
    def __init__(self):
        self.calls = []

    def __call__(self, method, path, query, headers):
        self.calls.append((method, path, query, headers))
        if path == "/fapi/v1/exchangeInfo":
            return 200, EXCHANGE_INFO
        if path == "/fapi/v1/ticker/bookTicker":
            return 200, BOOK_TICKER
        if path == "/fapi/v1/premiumIndex":
            return 200, PREMIUM
        if path == "/fapi/v2/balance":
            return 200, BALANCE
        if path == "/fapi/v2/positionRisk":
            return 200, POSITION_RISK
        if path == "/fapi/v1/openOrders":
            return 200, OPEN_ORDERS
        if path == "/fapi/v1/order" and method == "POST":
            return 200, {**OPEN_ORDERS[0], "status": "NEW"}
        if path == "/fapi/v1/order" and method == "DELETE":
            return 200, {**OPEN_ORDERS[0], "status": "CANCELED"}
        if path == "/fapi/v1/allOpenOrders":
            return 200, {"code": 200}
        raise AssertionError(f"{method} {path}")


@pytest.fixture()
def venue():
    t = FakeTransport()
    v = AsterVenue(mode="live", api_key=API_KEY, secret_key=SECRET,
                   transport=t)
    v._fake = t
    return v


def _verify_signature(query: str) -> dict:
    """把 query 拆开，用 secret 重算签名校验末尾的 signature 是否正确。"""
    pairs = parse_qsl(query)
    assert pairs[-1][0] == "signature"  # signature 必须在末尾
    total = urlencode(pairs[:-1])
    expect = hmac_mod.new(SECRET.encode(), total.encode(),
                          hashlib.sha256).hexdigest()
    assert pairs[-1][1] == expect
    return dict(pairs)


def test_sign_params_official_example():
    """Aster 官方文档 general-info 的 HMAC 示例向量（参数与密钥为文档原值）。"""
    secret = ("2b5eb11e18796d12d88f13dc27dbbd02c2cc51ff7059765ed9821957d82bb4d9")
    params = {"symbol": "BTCUSDT", "side": "BUY", "type": "LIMIT",
              "quantity": 1, "price": 9000, "timeInForce": "GTC",
              "recvWindow": 5000, "timestamp": 1591702613943}
    signed = sign_params(secret, params)
    assert signed.endswith(
        "&signature=3c661234138461fcc7a7d8746c6558c9842d4e10870d2ecbedf7777cad694af9")


def test_symbol_pair_mapping():
    assert to_pair("BTC") == "BTCUSDT"
    assert to_pair("ETHUSDT") == "ETHUSDT"
    assert to_base("BTCUSDT") == "BTC"


def test_markets_filters_and_status(venue):
    ms = venue.markets()
    assert [m.symbol for m in ms] == ["BTC"]  # SUSPENDED 的被过滤
    assert ms[0].price_step == 0.10 and ms[0].min_qty == 0.002
    assert ms[0].min_notional == 5.0


def test_book_and_funding(venue):
    b = venue.book("BTC")
    assert b.bid == 59999.0 and b.ask == 60001.0
    f = venue.funding("BTC")
    assert f.rate == 0.0001 and f.next_ts == 1700003600.0


def test_signed_request_shape(venue):
    venue.balance()
    method, path, query, headers = venue._fake.calls[-1]
    assert headers["X-MBX-APIKEY"] == API_KEY
    params = _verify_signature(query)  # 同时验证签名有效
    assert "timestamp" in params and params["recvWindow"] == "5000"


def test_balance_and_positions(venue):
    assert venue.balance() == {"equity": 8000.0, "available": 7500.0}
    ps = venue.positions()
    assert ps[0].symbol == "BTC" and ps[0].qty == -0.25
    assert ps[0].liq_price == 90000.0 and ps[0].leverage == 10


def test_place_limit_order(venue):
    o = venue.place_order(OrderRequest("BTC", Side.BUY, 0.1, price=59000.0))
    method, path, query, _ = venue._fake.calls[-1]
    assert method == "POST" and path == "/fapi/v1/order"
    params = _verify_signature(query)
    assert params["symbol"] == "BTCUSDT" and params["type"] == "LIMIT"
    assert params["timeInForce"] == "GTC" and params["quantity"] == "0.1"
    assert o.order_id == "42" and o.status == "open"


def test_place_market_and_post_only(venue):
    venue.place_order(OrderRequest("BTC", Side.SELL, 0.05, reduce_only=True))
    params = _verify_signature(venue._fake.calls[-1][2])
    assert params["type"] == "MARKET" and params["reduceOnly"] == "true"
    assert "price" not in params
    venue.place_order(OrderRequest("BTC", Side.BUY, 0.05, price=58000.0,
                                   post_only=True))
    params = _verify_signature(venue._fake.calls[-1][2])
    assert params["timeInForce"] == "GTX"


def test_cancel_order_looks_up_symbol(venue):
    assert venue.cancel_order("42") is True
    method, path, query, _ = venue._fake.calls[-1]
    assert method == "DELETE" and path == "/fapi/v1/order"
    params = _verify_signature(query)
    assert params["symbol"] == "BTCUSDT" and params["orderId"] == "42"
    assert venue.cancel_order("不存在") is False


def test_cancel_all(venue):
    assert venue.cancel_all("BTC") == 1
    assert venue._fake.calls[-1][1] == "/fapi/v1/allOpenOrders"


def test_missing_keys_raise(venue):
    v = AsterVenue(mode="live", transport=FakeTransport())
    with pytest.raises(RuntimeError, match="ASTER_API_KEY"):
        v.balance()
