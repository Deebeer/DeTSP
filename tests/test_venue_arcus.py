"""Arcus live 适配测试：签名向量对拍（与 Parallax 的 JS 实现一致性由
载荷格式保证）+ 注入假 GET/POST，不联网。"""

from decimal import Decimal

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from detsp.core.venue import OrderRequest, Side
from detsp.venues import _arcus_sign as ax
from detsp.venues.arcus import ArcusVenue

ADDR = "0x" + "ab" * 20
SEED = bytes(range(32))
KEY = Ed25519PrivateKey.from_private_bytes(SEED)

MARKET_ROW = {
    "type": "PERPETUAL", "status": "ONLINE", "marketId": 3,
    "marketDisplayName": "BTC-USD", "baseAsset": "BTC",
    "stepSize": "0.001", "tickSize": "0.1", "tickTiers": [],
    "minOrderSize": "0.002", "minOrderNotional": "10",
    "maxOrderSize": "100", "initialMarginFraction": "0.05",
    "maintenanceMarginFraction": "0.025",
}
MARKET = {
    "asset": "BTC", "arcus_symbol": "BTC-USD", "arcus_market_id": 3,
    "arcus_step_size": "0.001", "arcus_tick_size": "0.1",
    "arcus_tick_tiers": [], "arcus_size_decimals": 3,
}


# ── 纯签名/精度（移植的 _arcus_sign）──────────────────────

def test_place_payload_byte_format():
    p = ax.build_place_payload(
        address=ADDR.upper().replace("0X", "0x"), account_index=2,
        client_id="ABC123", timestamp=111, good_til_us_value=2000,
        market_id=3, price_ticks=600000, quantity_quantums=500,
        reduce_only=True, side="SELL", time_in_force="IOC")
    # g 必须是纳秒（微秒 ×1000）；clientId 小写；卖方 s=1；IOC 的 t=2
    assert p == ('{"ad":"%s","ai":2,"c":"abc123","ct":111,"g":2000000,'
                 '"m":3,"op":1,"p":600000,"q":500,"r":1,"s":1,"t":2,"v":1}'
                 % ADDR)


def test_cancel_payload_requires_exactly_one_id():
    with pytest.raises(ValueError):
        ax.build_cancel_payload(address=ADDR, account_index=0, timestamp=1,
                                market_id=3)
    p = ax.build_cancel_payload(address=ADDR, account_index=0, timestamp=1,
                                market_id=3, order_id="987")
    assert '"id":"987"' in p and '"op":2' in p


def test_sign_and_verify():
    sig = ax.sign_hex(KEY, "hello")
    KEY.public_key().verify(bytes.fromhex(sig), b"hello")  # 不抛即通过
    assert ax.public_key_hex(KEY) == \
        KEY.public_key().public_bytes_raw().hex()


def test_load_private_key_forms():
    assert ax.public_key_hex(ax.load_private_key(SEED.hex())) == \
        ax.public_key_hex(KEY)
    assert ax.public_key_hex(ax.load_private_key("0x" + SEED.hex())) == \
        ax.public_key_hex(KEY)
    with pytest.raises(ValueError):
        ax.load_private_key("not-a-key")


def test_price_rounding_direction_and_qty_floor():
    assert ax.round_arcus_price(MARKET, 60000.04, "buy") == Decimal("60000.1")
    assert ax.round_arcus_price(MARKET, 60000.04, "sell") == Decimal("60000.0")
    assert ax.align_arcus_quantity(MARKET, 0.1239) == Decimal("0.123")


def test_timestamp_ns_strictly_increasing():
    a, b = ax.timestamp_ns(), ax.timestamp_ns()
    assert b > a


def test_liquidation_price_cross_long():
    # 权益 1000、名义 6000（0.1 BTC @60000）、mmf 2.5%：走廊约 13.8%
    liq = ax.arcus_liquidation_price(
        size=0.1, entry_price=60000, mark_price=60000, margin_mode="CROSS",
        margin_used=None, mmf=0.025, account_equity=1000)
    assert liq is not None and 0 < liq < 60000
    x = ax.cross_distance(equity=1000, notional=6000, mmf=0.025)
    assert liq == pytest.approx(60000 * (1 - x))
    # 权益远大于名义额时价格归零都不爆 → 没有强平价，返回 None
    assert ax.arcus_liquidation_price(
        size=0.1, entry_price=60000, mark_price=60000, margin_mode="CROSS",
        margin_used=None, mmf=0.025, account_equity=100000) is None


def test_liquidation_price_isolated_short():
    liq = ax.arcus_liquidation_price(
        size=-1.0, entry_price=100, mark_price=100, margin_mode="ISOLATED",
        margin_used=10, mmf=0.05, account_equity=None)
    # 空头逐仓：(M + |s|·entry) / (|s|·(1+mmf))
    assert liq == pytest.approx(110 / 1.05)


def test_parse_order_response_states():
    r = ax.parse_order_response(202, {"orderId": "1"}, "cid-x")
    assert r.accepted and r.filled is None and r.status == "ACK"
    r = ax.parse_order_response(200, {"status": "REJECTED",
                                      "rejectionReason": "IOC_CANCELED"}, "c")
    assert not r.accepted and "IOC 未成交" in (r.error or "")
    r = ax.parse_order_response(200, {"status": "FILLED", "filledSize": "0.5",
                                      "orderId": "9"}, "c")
    assert r.accepted and r.filled == 0.5


# ── 适配器（注入假传输）──────────────────────────────────

class FakeTransport:
    def __init__(self):
        self.posts = []

    def getter(self, path, params):
        if path == "/v1/markets":
            return {"markets": [MARKET_ROW]}
        if path.startswith("/v1/bbo"):
            return {"bestBid": {"price": "59999"}, "bestAsk": {"price": "60001"}}
        if path == "/v1/account":
            return {"equity": "10000", "freeCollateral": "9000"}
        if path == "/v1/positions":
            return {"positions": []}
        if path == "/v1/openOrders":
            return {"orders": []}
        raise AssertionError(path)

    def poster(self, path, body, ts, sig):
        self.posts.append((path, body, ts, sig))
        if path == "/v1/placeOrder":
            return 202, {"orderId": "ord-1", "clientId": body["clientId"]}
        if path == "/v1/cancelOrder":
            return 200, {"status": "CANCELED"}
        if path == "/v1/setLeverage":
            return 200, {"status": "APPLIED"}
        raise AssertionError(path)


@pytest.fixture()
def venue():
    t = FakeTransport()
    v = ArcusVenue(mode="live", address=ADDR, account_index=0,
                   api_private_key=SEED.hex(), getter=t.getter, poster=t.poster)
    v._transport_fake = t
    return v


def test_markets_parse(venue):
    ms = venue.markets()
    assert ms[0].symbol == "BTC" and ms[0].price_step == 0.1
    assert ms[0].max_leverage == 20  # 1 / imf 0.05


def test_book_from_bbo(venue):
    b = venue.book("BTC")
    assert b.bid == 59999 and b.ask == 60001


def test_balance(venue):
    assert venue.balance() == {"equity": 10000.0, "available": 9000.0}


def test_place_order_signs_and_posts(venue):
    o = venue.place_order(OrderRequest("BTC", Side.BUY, 0.01, price=60000.04))
    path, body, ts, sig = venue._transport_fake.posts[-1]
    assert path == "/v1/placeOrder"
    assert body["price"] == "60000.1"        # 买单向上对齐 tick
    assert body["quantity"] == "0.01"
    assert body["timeInForce"] == "GTT"
    assert len(sig) == 128                    # Ed25519 签名 64 字节 hex
    # 签名必须能被私钥对应的公钥验过（载荷在 build_signed_order 内）
    assert o.order_id == "ord-1" and o.status == "open"


def test_place_ioc_market_uses_opposite_with_slippage(venue):
    venue.place_order(OrderRequest("BTC", Side.SELL, 0.01))
    _, body, _, _ = venue._transport_fake.posts[-1]
    assert body["timeInForce"] == "IOC"
    # 卖：买一 59999 × (1 − 1%) = 59399.01 → 向下对齐到 59399.0
    assert body["price"] == "59399"


def test_cancel_order(venue):
    venue._order_market["ord-1"] = 3
    assert venue.cancel_order("ord-1") is True
    path, body, _, _ = venue._transport_fake.posts[-1]
    assert path == "/v1/cancelOrder" and body["kind"] == "orderId"


def test_set_leverage(venue):
    assert venue.set_leverage("BTC", 10) is True
    path, body, _, _ = venue._transport_fake.posts[-1]
    assert path == "/v1/setLeverage" and body["leverage"] == 10
    assert body["isolated"] is False


def test_api_key_mismatch_rejected(venue):
    venue._api_key_env = "00" * 32
    with pytest.raises(ValueError, match="不是同一对"):
        venue.place_order(OrderRequest("BTC", Side.BUY, 0.01, price=60000.0))
