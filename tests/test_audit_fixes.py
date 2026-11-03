"""审计修复回归测试（对应 AUDIT.md 编号 S1/S2/S3/H1–H7/M2/M3/M4）。

原则：S1 必须用真实库异常类（requests / httpx 的超时类不是内置
TimeoutError 子类，这正是原 bug 所在）；S2 用 mock 终态；
其余各项用 paper 端到端或单元级断言锁住修复语义。
"""

import json
import threading
import time
import urllib.error
import urllib.request

import httpx
import pytest
import requests

from detsp.core.config import AppConfig, build_config
from detsp.core.engine import Engine
from detsp.core.execution import Caps, Executor, UnknownOutcomeError
from detsp.core.ledger import Ledger
from detsp.core.registry import OwnershipRegistry
from detsp.core.risk import KillSwitch, funding_hourly_rate
from detsp.core.venue import OrderRequest, Side
from detsp.strategies.grid import GridParams, GridStrategy
from detsp.venues.hl import HyperliquidVenue
from detsp.venues.paper import PaperVenue

BTC = {"BTC": 60_000.0}


def _executor(venue, caps=None):
    return Executor(venues={"v": venue}, registry=OwnershipRegistry(),
                    ledger=Ledger(), kill_switch=KillSwitch(), caps=caps)


# ═══════════ S1：异常翻译与 Executor 兜底 ═══════════

class _ReadTimeoutVenue(PaperVenue):
    """place_order 直接抛真实库异常（模拟 venue 未翻译的漏网情况）。"""

    def __init__(self, exc, **kw):
        super().__init__(**kw)
        self._exc = exc
        self.place_calls = 0

    def place_order(self, req):
        self.place_calls += 1
        raise self._exc


def test_s1_executor_httpx_timeout_is_unknown_and_not_retried():
    v = _ReadTimeoutVenue(httpx.ReadTimeout("read timed out"),
                          venue_id="v", prices=dict(BTC))
    ex = _executor(v)
    res = ex.place("t1", "v", OrderRequest("BTC", Side.BUY, 0.1, price=100.0))
    assert res.status == "unknown"
    assert v.place_calls == 1  # 绝不重发


def test_s1_executor_requests_timeout_is_unknown():
    v = _ReadTimeoutVenue(requests.exceptions.ReadTimeout("slow"),
                          venue_id="v", prices=dict(BTC))
    ex = _executor(v)
    res = ex.place("t1", "v", OrderRequest("BTC", Side.BUY, 0.1, price=100.0))
    assert res.status == "unknown" and v.place_calls == 1


def test_s1_executor_plain_reject_confirmed_by_reconcile():
    v = _ReadTimeoutVenue(RuntimeError("保证金不足"),
                          venue_id="v", prices=dict(BTC))
    ex = _executor(v)
    res = ex.place("t1", "v", OrderRequest("BTC", Side.BUY, 0.1, price=100.0))
    # 对账确认持仓未变、无残留挂单 → 确定未生效，按 rejected 返回
    assert res.status == "rejected"


def test_s1_executor_raise_after_fill_is_unknown():
    class FillThenRaise(PaperVenue):
        def place_order(self, req):
            super().place_order(req)          # 实际已成交
            raise RuntimeError("回执在半路丢失")  # 未被翻译的异常

    v = FillThenRaise(venue_id="v", prices=dict(BTC))
    ex = _executor(v)
    res = ex.place("t1", "v", OrderRequest("BTC", Side.BUY, 0.1))
    # 持仓已变化 → 必须按 unknown 返回，调用方据对账持仓决策
    assert res.status == "unknown"
    assert res.position is not None and res.position.qty == pytest.approx(0.1)


class _HLFakeInfo:
    def meta(self, dex=""):
        return {"universe": [{"name": "BTC", "szDecimals": 5,
                              "maxLeverage": 40}]}

    def l2_snapshot(self, coin):
        return {"levels": [[{"px": "59999"}], [{"px": "60001"}]],
                "time": 1700000000000}

    def user_state(self, address, dex=""):
        return {"marginSummary": {"accountValue": "1000"}, "withdrawable": "900",
                "assetPositions": []}

    def open_orders(self, address, dex=""):
        return []

    def meta_and_asset_ctxs(self, dex=""):
        return self.meta(dex), [{"funding": "0.0001"}]

    def query_order_by_oid(self, address, oid):
        return {"status": "order", "order": {
            "order": {"coin": "BTC", "side": "B", "origSz": "0.5",
                      "sz": "0.0", "limitPx": "59000"},
            "status": "filled"}}


class _HLTimeoutExchange:
    def order(self, *a, **kw):
        raise requests.exceptions.ReadTimeout("sdk read timeout")

    def market_open(self, *a, **kw):
        raise requests.exceptions.ConnectTimeout("sdk connect timeout")


def test_s1_hl_place_translates_requests_timeout():
    v = HyperliquidVenue(mode="live", account_address="0xabc",
                         info=_HLFakeInfo(), exchange=_HLTimeoutExchange())
    with pytest.raises(UnknownOutcomeError):
        v.place_order(OrderRequest("BTC", Side.BUY, 0.1, price=59000.0))
    with pytest.raises(UnknownOutcomeError):
        v.place_order(OrderRequest("BTC", Side.BUY, 0.1))  # 市价路径同样


def test_s1_rotation_first_leg_timeout_no_duplicate():
    class Flaky(PaperVenue):
        def __init__(self, **kw):
            super().__init__(**kw)
            self.calls = 0

        def place_order(self, req):
            self.calls += 1
            raise requests.exceptions.ReadTimeout("leg timeout")

    a = Flaky(venue_id="arcus", prices=dict(BTC))
    b = PaperVenue(venue_id="lighter", prices=dict(BTC))
    a.set_funding("BTC", 0.0001)
    b.set_funding("BTC", 0.0005)
    y = {"tasks": [{"name": "hedge", "strategy": "rotation_hedge",
                    "params": {"symbol": "BTC", "venue_a": "arcus",
                               "venue_b": "lighter", "notional_usd": 300,
                               "hold_seconds": 60, "cooldown_seconds": 60,
                               "leverage_a": 5, "leverage_b": 5}}]}
    cfg = build_config(env={}, yaml_data=y)
    engine = Engine.from_config(cfg, {"arcus": a, "lighter": b},
                                ledger=Ledger())
    engine.tick(now=1000)
    assert a.calls == 1                      # 只下过一次，没有重发
    assert engine.executor.position("arcus", "BTC") is None
    assert engine.executor.position("lighter", "BTC") is None


# ═══════════ S2：live 终态查询与对账式发现 ═══════════

def test_s2_hl_poll_order_terminal_filled():
    v = HyperliquidVenue(mode="live", account_address="0xabc",
                         info=_HLFakeInfo(), exchange=_HLTimeoutExchange())
    o = v.poll_order("777")
    assert o is not None and o.status == "filled"
    assert o.filled_qty == pytest.approx(0.5)


class _AsterTransport:
    def __init__(self):
        self.calls = []

    def __call__(self, method, path, query, headers):
        self.calls.append((method, path, query))
        if path == "/fapi/v1/exchangeInfo":
            return 200, {"symbols": [{
                "symbol": "BTCUSDT", "contractType": "PERPETUAL",
                "status": "TRADING",
                "filters": [
                    {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
                    {"filterType": "LOT_SIZE", "stepSize": "0.001",
                     "minQty": "0.002"},
                    {"filterType": "MIN_NOTIONAL", "notional": "5"}]}]}
        if path == "/fapi/v1/order" and method == "POST":
            return 200, {"orderId": 42, "symbol": "BTCUSDT", "side": "BUY",
                         "origQty": "0.123", "executedQty": "0",
                         "price": "59000", "status": "NEW"}
        if path == "/fapi/v1/order" and method == "GET":
            return 200, {"orderId": 42, "symbol": "BTCUSDT", "side": "BUY",
                         "origQty": "0.123", "executedQty": "0.123",
                         "price": "59000", "status": "FILLED"}
        if path == "/fapi/v1/openOrders":
            return 200, []
        raise AssertionError(f"{method} {path}")


def _aster():
    from detsp.venues.aster import AsterVenue

    t = _AsterTransport()
    v = AsterVenue(mode="live", api_key="k", secret_key="s", transport=t)
    return v, t


def test_s2_aster_poll_order_terminal():
    v, _t = _aster()
    o = v.place_order(OrderRequest("BTC", Side.BUY, 0.123, price=59000.0))
    got = v.poll_order(o.order_id)
    assert got is not None and got.status == "filled"
    assert got.filled_qty == pytest.approx(0.123)


def test_h5_aster_precision_alignment():
    v, t = _aster()
    from urllib.parse import parse_qsl

    v.place_order(OrderRequest("BTC", Side.BUY, 0.1234, price=59000.05))
    params = dict(parse_qsl(t.calls[-1][2]))
    assert params["quantity"] == "0.123"      # 数量向下对齐 step 0.001
    assert params["price"] == "59000"         # 买单价格向下到 tick 0.10
    v.place_order(OrderRequest("BTC", Side.SELL, 0.1, price=59000.05))
    params = dict(parse_qsl(t.calls[-1][2]))
    assert params["price"] == "59000.1"       # 卖单价格向上到 tick


def test_s2_lighter_poll_order_from_inactive():
    import httpx as _httpx

    from detsp.venues.lighter import LighterVenue

    books = {"order_books": [{"symbol": "BTC", "status": "active",
                              "market_id": 1, "supported_price_decimals": 2,
                              "supported_size_decimals": 5,
                              "min_base_amount": "0.0002",
                              "min_quote_amount": "10"}]}
    inactive = {"orders": [{"order_index": 888, "client_order_index": 0,
                            "market_index": 1, "is_ask": False,
                            "initial_base_amount": "0.1",
                            "filled_base_amount": "0.1", "price": "59000",
                            "status": "filled"}]}

    def handler(request):
        path = request.url.path
        if path == "/api/v1/orderBooks":
            return _httpx.Response(200, json=books)
        if path == "/api/v1/orderBookOrders":
            return _httpx.Response(200, json={
                "bids": [{"price": "59999"}], "asks": [{"price": "60001"}]})
        if path == "/api/v1/accountActiveOrders":
            return _httpx.Response(200, json={"orders": []})
        if path == "/api/v1/accountInactiveOrders":
            return _httpx.Response(200, json=inactive)
        return _httpx.Response(404, json={})

    class Resp:
        code = 200
        message = ""

    class Signer:
        def check_client(self):
            return None

        def create_auth_token_with_expiry(self):
            return "tok", None

        async def create_order(self, **kw):
            inactive["orders"][0]["client_order_index"] = \
                kw["client_order_index"]
            return object(), Resp(), None

    http = _httpx.Client(base_url="https://example.invalid",
                         transport=_httpx.MockTransport(handler))
    v = LighterVenue(deployment="mainnet", mode="live",
                     secrets={"LIGHTER_ACCOUNT_INDEX": "7",
                              "LIGHTER_API_KEY_INDEX": "4"},
                     http=http, signer=Signer())
    o = v.place_order(OrderRequest("BTC", Side.BUY, 0.1, price=59000.0))
    got = v.poll_order(o.order_id)   # "coi:<n>" 形式
    assert got is not None and got.status == "filled"
    assert got.filled_qty == pytest.approx(0.1)


class _NoPollVenue(PaperVenue):
    """模拟 Arcus：没有终态查询，poll 永远 None。"""

    def poll_order(self, order_id):
        return None


def _grid_yaml(**params):
    base = {"lower": 55_000, "upper": 65_000, "grid_count": 10,
            "qty_per_grid": 0.01, "mode": "neutral",
            "out_of_range_action": "close"}
    base.update(params)
    return {"tasks": [{"name": "grid-btc", "strategy": "grid",
                       "venue": "arcus", "symbol": "BTC", "params": base}]}


def test_s2_grid_reconcile_fill_via_position_delta():
    v = _NoPollVenue(venue_id="arcus", prices=dict(BTC))
    cfg = build_config(env={}, yaml_data=_grid_yaml())
    ledger = Ledger()
    engine = Engine.from_config(cfg, {"arcus": v}, ledger=ledger)
    assert len(v.open_orders("BTC")) == 10
    v.set_price("BTC", 58_900)   # 穿 59000 买单（paper 内部已成交）
    engine.tick(now=1)
    pos = engine.executor.position("arcus", "BTC")
    assert pos is not None and pos.qty == pytest.approx(0.01)
    # 对账式发现成交 → 补记账本 + 挂出反向单（总数回到 10）
    assert ledger.count("grid-btc") == 1
    assert len(v.open_orders("BTC")) == 10


# ═══════════ S3：启动恢复对账 ═══════════

def test_s3_grid_cancels_leftover_orders():
    v = PaperVenue(venue_id="arcus", prices=dict(BTC))
    # 外部遗留挂单（不经 Executor，无归属登记）
    v.place_order(OrderRequest("BTC", Side.BUY, 0.02, price=50_000.0))
    assert len(v.open_orders("BTC")) == 1
    cfg = build_config(env={}, yaml_data=_grid_yaml())
    engine = Engine.from_config(cfg, {"arcus": v}, ledger=Ledger())
    # 遗留单已被撤销，只剩网格自己的 10 张
    assert len(v.open_orders("BTC")) == 10
    assert any("撤销遗留挂单" in e for e in engine.events)


def test_s3_grid_unmanaged_position_blocks():
    v = PaperVenue(venue_id="arcus", prices=dict(BTC))
    v.place_order(OrderRequest("BTC", Side.BUY, 0.05))  # 已有持仓
    cfg = build_config(env={}, yaml_data=_grid_yaml())
    engine = Engine.from_config(cfg, {"arcus": v}, ledger=Ledger())
    rt = engine.tasks[0].runtime
    assert rt.unmanaged_position is True
    assert len(v.open_orders("BTC")) == 0     # 不铺单
    engine.tick(now=1)
    assert len(v.open_orders("BTC")) == 0     # tick 也不动作


def test_s3_grid_takeover_position():
    v = PaperVenue(venue_id="arcus", prices=dict(BTC))
    v.place_order(OrderRequest("BTC", Side.BUY, 0.05))
    cfg = build_config(env={}, yaml_data=_grid_yaml(takeover_position=True))
    engine = Engine.from_config(cfg, {"arcus": v}, ledger=Ledger())
    rt = engine.tasks[0].runtime
    assert rt.unmanaged_position is False
    assert len(v.open_orders("BTC")) == 10    # 接管后正常铺单


def test_s3_rotation_initializes_from_existing_positions():
    a = PaperVenue(venue_id="arcus", prices=dict(BTC), leverage=5)
    b = PaperVenue(venue_id="lighter", prices=dict(BTC), leverage=5)
    a.place_order(OrderRequest("BTC", Side.BUY, 0.005))
    b.place_order(OrderRequest("BTC", Side.SELL, 0.005))
    y = {"tasks": [{"name": "hedge", "strategy": "rotation_hedge",
                    "params": {"symbol": "BTC", "venue_a": "arcus",
                               "venue_b": "lighter", "notional_usd": 300,
                               "hold_seconds": 60, "cooldown_seconds": 60,
                               "leverage_a": 5, "leverage_b": 5}}]}
    cfg = build_config(env={}, yaml_data=y)
    engine = Engine.from_config(cfg, {"arcus": a, "lighter": b},
                                ledger=Ledger())
    rt = engine.tasks[0].runtime
    assert rt.phase == "open"
    assert any("启动对账" in e for e in engine.events)


# ═══════════ H1：caps 接线 ═══════════

def test_h1_caps_from_config_reject_grid_start():
    y = _grid_yaml()
    y["caps"] = {"global_usd": 100}     # 网格估算名义 6000，远超上限
    cfg = build_config(env={}, yaml_data=y)
    v = PaperVenue(venue_id="arcus", prices=dict(BTC))
    engine = Engine.from_config(cfg, {"arcus": v}, ledger=Ledger())
    assert engine.executor.caps.global_ == 100   # config 已接线进 Executor
    assert engine.tasks == []                     # 网格被拒绝启动
    assert "grid-btc" in engine.errors
    assert "caps" in engine.errors["grid-btc"]


def test_h1_grid_start_caps_hard_check():
    v = PaperVenue(venue_id="v", prices=dict(BTC))
    ex = _executor(v, caps=Caps(strategy={"g1": 100.0}))
    g = GridStrategy(GridParams(lower=55_000, upper=65_000, grid_count=10,
                                qty_per_grid=0.01),
                     executor=ex, venue_id="v", symbol="BTC", owner="g1")
    with pytest.raises(RuntimeError, match="caps"):
        g.start()


# ═══════════ H3：资金费口径 ═══════════

def test_h3_funding_hourly_conversion():
    assert funding_hourly_rate(0.0001, 3600) == pytest.approx(0.0001)
    assert funding_hourly_rate(0.0008, 28800) == pytest.approx(0.0001)
    f = PaperVenue(venue_id="x")
    f.set_funding("BTC", 0.0005, interval_seconds=28800)
    assert f.funding("BTC").interval_seconds == 28800


def test_h3_rotation_direction_uses_hourly_rates():
    a = PaperVenue(venue_id="arcus", prices=dict(BTC), leverage=5)
    b = PaperVenue(venue_id="lighter", prices=dict(BTC), leverage=5)
    a.set_funding("BTC", 0.0001)                            # 每小时 0.0001
    b.set_funding("BTC", 0.0005, interval_seconds=28800)    # 每小时 0.0000625
    # 原始 rate 直比会选「空 a 多 b」；换算后 a 的小时费率更高 → 空 a 多 b
    # 恰好相反：应空 arcus、多 lighter
    y = {"tasks": [{"name": "hedge", "strategy": "rotation_hedge",
                    "params": {"symbol": "BTC", "venue_a": "arcus",
                               "venue_b": "lighter", "notional_usd": 300,
                               "hold_seconds": 60, "cooldown_seconds": 60,
                               "leverage_a": 5, "leverage_b": 5}}]}
    cfg = build_config(env={}, yaml_data=y)
    engine = Engine.from_config(cfg, {"arcus": a, "lighter": b},
                                ledger=Ledger())
    engine.tick(now=1000)
    pa = engine.executor.position("arcus", "BTC")
    pb = engine.executor.position("lighter", "BTC")
    assert pa is not None and pa.qty < 0    # 高（小时）费率所做空
    assert pb is not None and pb.qty > 0


# ═══════════ H4：任务级归属 ═══════════

def test_h4_claim_mutex_between_tasks():
    from detsp.core.registry import ConflictError

    v = PaperVenue(venue_id="v", prices=dict(BTC))
    ex = _executor(v)
    p = GridParams(lower=55_000, upper=65_000, grid_count=10,
                   qty_per_grid=0.01)
    g1 = GridStrategy(p, executor=ex, venue_id="v", symbol="BTC",
                      owner="task-1")
    g2 = GridStrategy(p, executor=ex, venue_id="v", symbol="BTC",
                      owner="task-2")
    g1.start()
    with pytest.raises(ConflictError):
        g2.start()   # 同 (venue,symbol) 第二个任务真互斥


# ═══════════ H7 / M5：强平监控与 mmr ═══════════

def test_m5_estimate_open_corridor_includes_mmr():
    # 10x 杠杆：100/10 − 5%（默认 mmr）= 5%，而不是旧口径的 10%
    assert Executor.estimate_open_corridor(10) == pytest.approx(5.0)
    assert Executor.estimate_open_corridor(10, 0.0) == pytest.approx(10.0)


def test_h7_position_corridor_unevaluable():
    class Stub:
        venue_id = "v"
        account = "default"
        mode = "live"

        def positions(self):
            from detsp.core.venue import Position
            return [Position(symbol="BTC", qty=0.1, entry_price=100.0,
                             liq_price=None, leverage_known=False)]

        def markets(self):
            return []

        def book(self, symbol):
            from detsp.core.venue import Book
            return Book(symbol=symbol, bid=99.0, ask=101.0)

    ex = _executor(Stub())
    has, corr = ex.position_corridor("v", "BTC")
    assert has is True and corr is None     # 不可评估，禁止当作安全


def test_h7_grid_closes_when_corridor_collapses():
    v = PaperVenue(venue_id="arcus", prices=dict(BTC))
    cfg = build_config(env={}, yaml_data=_grid_yaml())
    engine = Engine.from_config(cfg, {"arcus": v}, ledger=Ledger())
    v.set_price("BTC", 58_900)
    engine.tick(now=1)
    pos = engine.executor.position("arcus", "BTC")
    assert pos is not None and pos.qty == pytest.approx(0.01)
    v.set_price("BTC", 60_000)
    pos.liq_price = 59_900.0    # 强平距离只剩约 0.17% < 3% 门槛
    engine.tick(now=2)
    rt = engine.tasks[0].runtime
    assert rt.stopped is True
    assert engine.executor.position("arcus", "BTC") is None


def test_h7_premium_blocks_when_unevaluable():
    ea = PaperVenue(venue_id="entropy", prices={"SNDK": 100.0})
    lh = PaperVenue(venue_id="lighter_rh", prices={"SNDK": 99.0})
    y = {"tasks": [{"name": "arb", "strategy": "premium_arb",
                    "params": {"symbol": "SNDK", "base_venue": "entropy",
                               "hedge_venue": "lighter_rh", "midline_bps": 0,
                               "upper_bps": 50, "lower_bps": 50,
                               "persist_seconds": 0, "max_position_usd": 1000,
                               "max_order_notional_usd": 500,
                               "reconcile_seconds": 15}}]}
    cfg = build_config(env={}, yaml_data=y)
    ledger = Ledger()
    engine = Engine.from_config(cfg, {"entropy": ea, "lighter_rh": lh},
                                ledger=ledger)
    engine.tick(now=0)   # 溢价信号 → 双腿开仓
    assert engine.executor.position("entropy", "SNDK").qty == \
        pytest.approx(-5)
    n0 = ledger.count()
    # 把 base 腿持仓改成「无强平价 + 杠杆未知」= 不可评估
    pos = ea.positions()[0]
    pos.leverage_known = False
    lh.set_price("SNDK", 101.0)   # 出现反向信号也不许再开仓
    engine.tick(now=1)
    engine.tick(now=2)
    assert ledger.count() == n0
    assert "不可评估" in engine.tasks[0].runtime.last_error


# ═══════════ H6：paper 行情 ═══════════

def test_h6_paper_unknown_symbol_no_keyerror():
    v = PaperVenue(venue_id="paper")
    b = v.book("DOGE")
    assert b.mid == pytest.approx(100.0)   # 兜底价，不再 KeyError
    assert v.book("SNDK").mid > 0          # 示例 symbol 有内置价


def test_h6_paper_provider_priority_and_walk():
    class FakeProvider:
        def mid(self, venue_id, symbol):
            return 123.0 if symbol == "BTC" else None

    v = PaperVenue(venue_id="paper", quote_provider=FakeProvider())
    assert v.book("BTC").mid == pytest.approx(123.0)

    w = PaperVenue(venue_id="paper", walk=True, seed=42)
    p0 = w.book("BTC").mid
    moved = any(w.book("BTC").mid != p0 for _ in range(50))
    assert moved                            # 随机游走确实在动


def test_h6_factory_injects_price_driver():
    from detsp.venues.factory import build_venues

    cfg = build_config(env={}, yaml_data={})
    venues = build_venues(cfg, force_paper=True)
    arcus = venues["arcus"]
    assert arcus._paper is not None and arcus._paper._walk is True
    # arcus 无公开端点（provider 返回 None）→ 走随机游走，未知 symbol 不崩
    assert arcus.book("SNDK").mid > 0


# ═══════════ M2：maker 部分成交 ═══════════

def test_m2_maker_partial_fill_hedges_final_qty_only():
    from detsp.core.venue import Order

    class PartialVenue(PaperVenue):
        """maker 单先部分成交挂着，released 后变终态（已撤，带部分量）。"""

        def __init__(self, **kw):
            super().__init__(**kw)
            self.released = False
            self._maker_req = None
            self._partial = 0.0

        def place_order(self, req):
            if req.post_only and self._maker_req is None:
                self._maker_req = req
                self._partial = req.qty * 0.4
                self._apply_fill(req.symbol, req.side, self._partial,
                                 req.price)
                return Order(order_id="mk-1", symbol=req.symbol,
                             side=req.side, qty=req.qty, price=req.price,
                             filled_qty=0.0, status="open")
            return super().place_order(req)

        def poll_order(self, order_id):
            if order_id == "mk-1":
                if self.released:
                    return Order(order_id="mk-1", symbol="BTC",
                                 side=self._maker_req.side,
                                 qty=self._maker_req.qty,
                                 price=self._maker_req.price,
                                 filled_qty=self._partial, status="canceled")
                return Order(order_id="mk-1", symbol="BTC",
                             side=self._maker_req.side,
                             qty=self._maker_req.qty,
                             price=self._maker_req.price,
                             filled_qty=self._partial, status="open")
            return super().poll_order(order_id)

    a = PartialVenue(venue_id="arcus", prices=dict(BTC), leverage=5)
    b = PaperVenue(venue_id="lighter", prices=dict(BTC), leverage=5)
    a.set_funding("BTC", 0.0001)
    b.set_funding("BTC", 0.0005)
    y = {"tasks": [{"name": "hedge", "strategy": "rotation_hedge",
                    "params": {"symbol": "BTC", "venue_a": "arcus",
                               "venue_b": "lighter", "notional_usd": 300,
                               "hold_seconds": 60, "cooldown_seconds": 60,
                               "leverage_a": 5, "leverage_b": 5,
                               "arcus_maker": True,
                               "maker_wait_seconds": 120}}]}
    cfg = build_config(env={}, yaml_data=y)
    engine = Engine.from_config(cfg, {"arcus": a, "lighter": b},
                                ledger=Ledger())
    rt = engine.tasks[0].runtime
    engine.tick(now=1000)   # 挂 maker（部分成交 40% 在场所侧已发生）
    assert rt.phase == "opening"
    engine.tick(now=1010)   # 部分成交中：保留跟踪，不提前对冲
    assert rt.phase == "opening"
    assert engine.executor.position("lighter", "BTC") is None
    a.released = True       # 订单终局（撤单确认，最终量 = 部分量）
    engine.tick(now=1020)
    pa = engine.executor.position("arcus", "BTC")
    pb = engine.executor.position("lighter", "BTC")
    assert pa is not None and pb is not None
    assert abs(pa.qty) == pytest.approx(abs(pb.qty))   # 按最终量对冲
    assert abs(pa.qty) == pytest.approx(0.4 * 300 / 60_000, rel=0.05)


# ═══════════ M3：停机落盘与预检门 ═══════════

def test_m3_killswitch_persists_across_restart(tmp_path):
    p = tmp_path / "killswitch.json"
    ks = KillSwitch(persist_path=p)
    ks.halt("连续错误")
    assert p.exists()
    ks2 = KillSwitch(persist_path=p)      # 模拟重启
    assert ks2.halted is True and ks2.reason == "连续错误"
    ks2.reset()                            # 人工 reset 才解除
    assert not p.exists()
    assert KillSwitch(persist_path=p).halted is False


def test_m3_engine_restores_halt_from_disk(tmp_path):
    (tmp_path / "killswitch.json").write_text(
        json.dumps({"halted": True, "reason": "上次停机"}), encoding="utf-8")
    cfg = AppConfig(tasks=[])
    engine = Engine.from_config(cfg, {}, ledger=Ledger(), state_dir=tmp_path)
    assert engine.kill_switch.halted is True
    assert any("落盘恢复" in e for e in engine.events)


def test_m3_preflight_mark_fingerprint(tmp_path):
    from detsp.core.preflight import check_preflight_mark, write_preflight_mark

    y = {"venues": {"arcus": {"mode": "live"}}, "tasks": []}
    cfg = build_config(env={"DRY_RUN": "false"}, yaml_data=y)
    mark = tmp_path / "preflight-ok.json"
    ok, _ = check_preflight_mark(cfg, mark)
    assert ok is False                     # 无标记 → 不通过
    write_preflight_mark(cfg, mark)
    ok, _ = check_preflight_mark(cfg, mark)
    assert ok is True
    y2 = {"venues": {"arcus": {"mode": "live"}},
          "tasks": [{"name": "t", "strategy": "grid", "venue": "arcus",
                     "symbol": "BTC", "params": {}}]}
    cfg2 = build_config(env={"DRY_RUN": "false"}, yaml_data=y2)
    ok, why = check_preflight_mark(cfg2, mark)
    assert ok is False and "重跑预检" in why   # 指纹变了必须重跑


# ═══════════ H2 / M4：面板安全与命令队列 ═══════════

def _panel_server():
    from detsp.web.server import PanelState, serve

    cfg = build_config(env={}, yaml_data={"tasks": []})
    engine = Engine.from_config(cfg, {}, ledger=Ledger())
    state = PanelState(engine=engine, venues={}, ledger=Ledger(),
                       app_config=cfg, task_cfgs={})
    server = serve(state, "127.0.0.1", 0)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    return base, state, server


def _post(base, path, headers=None):
    req = urllib.request.Request(base + path, method="POST")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def test_h2_post_requires_token_and_host_check():
    base, state, server = _panel_server()
    try:
        assert _post(base, "/api/killswitch") == 403            # 无 token
        assert _post(base, "/api/killswitch",
                     {"X-API-Token": "wrong"}) == 403          # 错 token
        assert _post(base, "/api/killswitch",
                     {"X-API-Token": state.api_token}) == 200  # 正确 token
        assert _post(base, "/api/killswitch",
                     {"X-API-Token": state.api_token,
                      "Origin": "http://evil.example"}) == 403  # 跨源 Origin
        # 坏 Host：连 GET 都拒（DNS rebinding 防线）
        req = urllib.request.Request(base + "/")
        req.add_header("Host", "evil.example")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                code = r.status
        except urllib.error.HTTPError as e:
            code = e.code
        assert code == 403
    finally:
        server.shutdown()


def test_m4_command_runs_on_engine_thread():
    engine = Engine()
    main_ident = threading.get_ident()
    result = {}

    def caller():
        result["ident"] = engine.run_command(
            lambda: threading.get_ident(), timeout=5)

    t = threading.Thread(target=caller)
    t.start()
    deadline = time.time() + 5
    while t.is_alive() and time.time() < deadline:
        engine.process_commands()   # 主线程充当引擎主循环
        time.sleep(0.005)
    t.join(5)
    assert result["ident"] == main_ident   # 命令在引擎线程串行执行
