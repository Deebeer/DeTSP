"""第五轮测试：全量 pairs 接口、跨所 best、配置预设、策略记录落库。

- pairs：两所共同上市交集、按策略排序、混口径靠后、预计年化
  换算、疑似同名不同资产（价差 >50%）标 suspect 并沉底；
- best：跨场所对聚合，suspect 不进最佳；
- presets：三档齐全、字段真实、中性档与 Params 默认值一致
  （有默认值的字段）、三档关键字段单调差异化；
- records：仓位出现 → 运行仓位；平掉 → 落 closed_positions，
  平仓价口径 fills 优先、mark 兜底；接口 /api/records、
  /api/pairs、/api/best 实服可用。
全部离线（假 HTTP + PaperVenue）。
"""

import json
import threading
import time
import urllib.request

import pytest

from detsp.core.config import build_config
from detsp.core.engine import Engine
from detsp.core.ledger import Fill, Ledger
from detsp.core.market_scanner import MarketScanner
from detsp.core.venue import OrderRequest, Side
from detsp.strategies.specs import PRESETS, strategy_specs
from detsp.venues.paper import PaperVenue
from detsp.web.server import PanelState, serve


class _Resp:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


class FakeFullHttp:
    """全量假数据：HL 本体 BTC/ETH/ZZZ（BTC 被 predictedFundings
    升级为 predicted、ETH 保持 current）；io/xyz 股票；arcus 全
    predicted；aster last_settled 且 ZZZ 价格与 HL 差 100 倍
    （疑似同名不同资产）；lighter 主网 BTC/ETH/AAPL、RH 只有 BTC。"""

    def post(self, url, json=None, params=None):
        assert "hyperliquid" in url
        body = json or {}
        t = body.get("type")
        if t == "metaAndAssetCtxs":
            dex = body.get("dex", "")
            if dex == "":
                return _Resp([
                    {"universe": [{"name": "BTC"}, {"name": "ETH"},
                                  {"name": "ZZZ"}]},
                    [{"markPx": "60000", "prevDayPx": "57000",
                      "funding": "0.0001"},
                     {"markPx": "3000", "prevDayPx": "2700",
                      "funding": "0.0005"},
                     {"markPx": "100", "prevDayPx": "99",
                      "funding": "0.00001"}]])
            if dex == "io":
                return _Resp([{"universe": [{"name": "io:AAPL"}]},
                              [{"markPx": "200", "prevDayPx": "190",
                                "funding": "0.00003"}]])
            if dex == "xyz":
                return _Resp([
                    {"universe": [{"name": "xyz:AAPL"},
                                  {"name": "xyz:SNDK"}]},
                    [{"markPx": "201", "prevDayPx": "195",
                      "funding": "0.00005"},
                     {"markPx": "250", "prevDayPx": "240",
                      "funding": "0.00001"}]])
        if t == "predictedFundings":
            return _Resp([["BTC", [["HlPerp", {
                "fundingRate": "0.0002",
                "nextFundingTime": 1_900_000_000_000}]]]])
        raise AssertionError(f"未预期的 POST {body}")

    def get(self, url, params=None):
        if "arcus" in url and "/v1/markets" in url:
            return _Resp({"markets": [
                {"baseAsset": "BTC", "marketDisplayName": "BTC-PERP",
                 "nextFundingRate": "0.00036", "markPrice": "60050",
                 "priceChange24h": "0.05"},
                {"baseAsset": "ETH", "marketDisplayName": "ETH-PERP",
                 "nextFundingRate": "0.0001", "markPrice": "3010",
                 "priceChange24h": "-0.02"}]})
        if "arcus" in url and "/v1/bbo/" in url:
            return _Resp({"bestBid": {"price": "60040"},
                          "bestAsk": {"price": "60060"}})
        if "asterdex" in url and "premiumIndex" in url:
            return _Resp([
                {"symbol": "BTCUSDT", "markPrice": "60100",
                 "lastFundingRate": "0.0008"},
                {"symbol": "ZZZUSDT", "markPrice": "1",
                 "lastFundingRate": "0.0008"}])
        if "asterdex" in url and "exchangeInfo" in url:
            return _Resp({"symbols": [
                {"symbol": "BTCUSDT", "status": "TRADING"},
                {"symbol": "ZZZUSDT", "status": "TRADING"}]})
        if "asterdex" in url and "ticker/24hr" in url:
            return _Resp([
                {"symbol": "BTCUSDT", "priceChangePercent": "2.5"},
                {"symbol": "ZZZUSDT", "priceChangePercent": "0.5"}])
        if "zklighter" in url and "orderBooks" in url:
            return _Resp({"order_books": [
                {"symbol": "BTC", "market_id": 1, "status": "active",
                 "market_type": "perp"},
                {"symbol": "ETH", "market_id": 2, "status": "active",
                 "market_type": "perp"},
                {"symbol": "AAPL", "market_id": 3, "status": "active",
                 "market_type": "perp"}]})
        if "zklighter" in url and "funding-rates" in url:
            return _Resp({"funding_rates": [
                {"market_id": 1, "exchange": "lighter", "rate": 0.00016},
                {"market_id": 2, "exchange": "lighter", "rate": 0.0008},
                {"market_id": 3, "exchange": "lighter", "rate": 0.00024}]})
        if "zklighter" in url and "exchangeStats" in url:
            return _Resp({"order_book_stats": [
                {"symbol": "BTC", "last_trade_price": 59999,
                 "daily_price_change": 1.5},
                {"symbol": "ETH", "last_trade_price": 3005,
                 "daily_price_change": -2.0}]})
        if "zklighter" in url and "orderBookOrders" in url:
            if (params or {}).get("market_id") == 2:
                return _Resp({"bids": [{"price": "3004"}],
                              "asks": [{"price": "3006"}]})
            return _Resp({"bids": [{"price": "60012"}],
                          "asks": [{"price": "60018"}]})
        if "rh.lighter" in url and "orderBooks" in url:
            return _Resp({"order_books": [
                {"symbol": "BTC", "market_id": 1, "status": "active",
                 "market_type": "perp"}]})
        if "rh.lighter" in url and "funding-rates" in url:
            return _Resp({"funding_rates": [
                {"market_id": 1, "exchange": "lighter", "rate": 0.00016}]})
        if "rh.lighter" in url and "exchangeStats" in url:
            return _Resp({"order_book_stats": [
                {"symbol": "BTC", "last_trade_price": 60020,
                 "daily_price_change": 1.0}]})
        if "rh.lighter" in url and "orderBookOrders" in url:
            return _Resp({"bids": [{"price": "60018"}],
                          "asks": [{"price": "60022"}]})
        raise AssertionError(f"未预期的 GET {url}")


@pytest.fixture()
def scanner():
    return MarketScanner(http=FakeFullHttp(), watchlist=["BTC", "ETH"])


# ── pairs ──────────────────────────────────────────────────

def test_pairs_common_intersection_and_counts(scanner):
    d = scanner.pairs("hyperliquid", "lighter", "funding_carry")
    assert d["listed_a"] == 3 and d["listed_b"] == 3
    assert d["common"] == 2          # BTC、ETH（AAPL 不在 HL、ZZZ 不在 Lighter）
    assert {r["symbol"] for r in d["rows"]} == {"BTC", "ETH"}
    assert d["errors"] == {}


def test_pairs_funding_sort_same_kind_first_then_edge(scanner):
    d = scanner.pairs("hyperliquid", "lighter", "funding_carry")
    rows = d["rows"]
    # ETH：HL current 5.0 vs Lighter current 1.0，同口径，费差 4.0
    # BTC：HL predicted 2.0 vs Lighter current 0.2，混口径，费差 1.8
    # 同口径优先 → ETH 在前（哪怕只看费差 ETH 也更大，双保险）
    assert [r["symbol"] for r in rows] == ["ETH", "BTC"]
    eth, btc = rows
    assert eth["mixed"] is False and eth["edge_bps_h"] == pytest.approx(4.0)
    assert eth["long_venue"] == "lighter"
    assert eth["short_venue"] == "hyperliquid"
    assert eth["apr_pct"] == pytest.approx(4.0 * 87.6)
    assert btc["mixed"] is True and btc["edge_bps_h"] == pytest.approx(1.8)
    # BTC 的 Lighter 价应是 watchlist BBO 覆盖值 60015，不是批量 59999
    assert btc["price_b"] == pytest.approx(60015.0)


def test_pairs_mixed_last_even_with_bigger_edge(scanner):
    # arcus 全 predicted：BTC = predicted×predicted 同口径（费差
    # |3.6-2.0|=1.6）；ETH = current×predicted 混口径且费差
    # |1.0-5.0|=4.0 更大——同口径优先，BTC 仍排第一
    d = scanner.pairs("hyperliquid", "arcus", "funding_carry")
    assert [r["symbol"] for r in d["rows"]] == ["BTC", "ETH"]
    assert d["rows"][0]["mixed"] is False
    assert d["rows"][1]["mixed"] is True
    assert d["rows"][1]["edge_bps_h"] > d["rows"][0]["edge_bps_h"]


def test_pairs_spread_sort_and_grid_vol_sort(scanner):
    d = scanner.pairs("hyperliquid", "lighter", "premium_arb")
    # ETH 价差（3000 vs 3005 ≈ 16.6bps）> BTC（60000 vs 60015 ≈ 2.5bps）
    assert [r["symbol"] for r in d["rows"]] == ["ETH", "BTC"]
    assert d["rows"][0]["signed_spread_bps"] == pytest.approx(-16.65, abs=0.1)
    g = scanner.pairs("hyperliquid", "lighter", "hedged_grid")
    # 日波动取 A 腿（HL prevDayPx）：ETH 11.1% > BTC 5.3%
    assert [r["symbol"] for r in g["rows"]] == ["ETH", "BTC"]
    assert g["rows"][0]["volatility_pct"] == pytest.approx(
        abs(3000 - 2700) / 2700 * 100, rel=1e-6)


def test_pairs_suspect_flagged_and_sunk(scanner):
    d = scanner.pairs("hyperliquid", "aster", "premium_arb")
    assert [r["symbol"] for r in d["rows"]] == ["BTC", "ZZZ"]
    zzz = d["rows"][1]
    assert zzz["suspect"] is True and zzz["spread_bps"] > 5000
    assert d["rows"][0]["suspect"] is False


def test_pairs_unknown_venue(scanner):
    d = scanner.pairs("hyperliquid", "nope", "funding_carry")
    assert d["ok"] is False and "未知场所" in d["error"]


def test_best_aggregates_and_excludes_suspect(scanner):
    b = scanner.best("funding_carry", limit=5)
    assert b["rows"], "应有跨所最佳行"
    for r in b["rows"]:
        assert r["venue_a"] and r["venue_b"]
        assert r["long_venue"] in (r["venue_a"], r["venue_b"])
    keys = [(r["mixed"], -r["edge_bps_h"]) for r in b["rows"]]
    assert keys == sorted(keys)
    p = scanner.best("premium_arb", limit=5)
    assert all(not r["suspect"] for r in p["rows"])
    assert "ZZZ" not in {r["symbol"] for r in p["rows"]}
    g = scanner.best("hedged_grid", limit=5)
    assert g["rows"] and all(r["volatility_pct"] is not None
                             for r in g["rows"])
    vols = [r["volatility_pct"] for r in g["rows"]]
    assert vols == sorted(vols, reverse=True)


# ── presets ────────────────────────────────────────────────

def test_presets_shape_and_neutral_equals_defaults():
    for spec in strategy_specs():
        st = spec["strategy"]
        presets = spec["page"]["presets"]
        assert set(presets) == {"conservative", "neutral", "aggressive"}
        fields = {f["name"]: f for f in spec["fields"]}
        for tier, vals in presets.items():
            assert vals, f"{st}/{tier} 预设不能为空"
            for k, v in vals.items():
                if k == "_total_usd":
                    assert st == "hedged_grid"  # 伪字段只许网格用
                    continue
                assert k in fields, f"{st}/{tier} 含未知字段 {k}"
                if tier == "neutral" and fields[k]["default"] is not None:
                    assert v == fields[k]["default"], \
                        f"{st} 中性档 {k}={v} 与默认值 " \
                        f"{fields[k]['default']} 漂移"


def test_presets_tiers_differ_on_key_fields():
    p = PRESETS
    # 杠杆/规模单调：保守 < 中性 < 激进（网格看格数与杠杆）
    assert p["funding_carry"]["conservative"]["leverage"] < \
        p["funding_carry"]["neutral"]["leverage"] < \
        p["funding_carry"]["aggressive"]["leverage"]
    assert p["funding_carry"]["conservative"][
        "min_entry_edge_bps_per_hour"] > \
        p["funding_carry"]["aggressive"]["min_entry_edge_bps_per_hour"]
    assert p["rotation_hedge"]["conservative"]["leverage_a"] < \
        p["rotation_hedge"]["aggressive"]["leverage_a"]
    assert p["premium_arb"]["conservative"]["max_position_usd"] < \
        p["premium_arb"]["neutral"]["max_position_usd"] < \
        p["premium_arb"]["aggressive"]["max_position_usd"]
    assert p["hedged_grid"]["conservative"]["grid_count"] < \
        p["hedged_grid"]["neutral"]["grid_count"] < \
        p["hedged_grid"]["aggressive"]["grid_count"]


# ── 策略记录（运行仓位 + 历史落库） ────────────────────────

@pytest.fixture()
def state():
    cfg = build_config(env={}, yaml_data={"tasks": []})
    venues = {"arcus": PaperVenue(venue_id="arcus",
                                   prices={"BTC": 60_000.0}),
              "lighter": PaperVenue(venue_id="lighter",
                                    prices={"BTC": 60_000.0})}
    ledger = Ledger()
    engine = Engine.from_config(cfg, venues, ledger=ledger)
    st = PanelState(engine=engine, venues=venues, ledger=ledger,
                    app_config=cfg,
                    task_cfgs={"t1": {"name": "t1",
                                      "strategy": "funding_carry",
                                      "params": {}}})
    st.market_scanner = MarketScanner(http=FakeFullHttp(),
                                      watchlist=["BTC", "ETH"])
    return st


def _records(st):
    st._pos_synced_at = 0.0     # 绕过 3 秒同步节流，立即重抓
    return st.records_view()


def test_records_open_then_closed_with_fills_price(state):
    v = state.venues["arcus"]
    state.engine.executor.claim("t1", "arcus", "BTC")
    v.place_order(OrderRequest(symbol="BTC", side=Side.BUY, qty=0.5))
    rec = _records(state)
    assert len(rec["open"]) == 1
    row = rec["open"][0]
    assert row["task"] == "t1" and row["strategy"] == "funding_carry"
    assert row["side"] == "long" and row["qty"] == pytest.approx(0.5)
    assert rec["closed"] == []
    # 平仓成交先落账（fills 口径），再平掉仓位
    state.ledger.record(Fill(venue="arcus", account="default",
                             symbol="BTC", strategy="t1", side="sell",
                             qty=0.5, price=61_000.0))
    v.place_order(OrderRequest(symbol="BTC", side=Side.SELL, qty=0.5))
    rec = _records(state)
    assert rec["open"] == []
    assert len(rec["closed"]) == 1
    closed = rec["closed"][0]
    assert closed["side"] == "long"
    assert closed["close_price"] == pytest.approx(61_000.0)
    assert closed["close_price_source"] == "fills"
    assert closed["closed_ts"] >= closed["opened_ts"] > 0


def test_records_close_price_falls_back_to_mark(state):
    v = state.venues["lighter"]
    v.place_order(OrderRequest(symbol="BTC", side=Side.SELL, qty=0.25))
    rec = _records(state)
    assert len(rec["open"]) == 1 and rec["open"][0]["side"] == "short"
    v.place_order(OrderRequest(symbol="BTC", side=Side.BUY, qty=0.25))
    rec = _records(state)
    assert rec["open"] == [] and len(rec["closed"]) == 1
    closed = rec["closed"][0]
    # 无归属、无 fills → 盘口中间价口径
    assert closed["task"] == "" and closed["strategy"] == ""
    assert closed["close_price_source"] == "mark"
    assert closed["close_price"] > 0


def test_records_endpoints_over_http(state):
    server = serve(state, "127.0.0.1", 0)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    stop_flag = threading.Event()

    def _pump():
        while not stop_flag.is_set():
            state.engine.process_commands()
            time.sleep(0.01)

    threading.Thread(target=_pump, daemon=True).start()
    try:
        with urllib.request.urlopen(
                base + "/api/pairs?venue_a=hyperliquid&venue_b=lighter"
                       "&strategy=funding_carry", timeout=5) as r:
            d = json.loads(r.read().decode("utf-8"))
        assert d["common"] == 2 and len(d["rows"]) == 2
        with urllib.request.urlopen(
                base + "/api/best?strategy=hedged_grid", timeout=5) as r:
            b = json.loads(r.read().decode("utf-8"))
        assert b["rows"]
        with urllib.request.urlopen(base + "/api/records",
                                    timeout=5) as r:
            rec = json.loads(r.read().decode("utf-8"))
        assert rec == {"open": [], "closed": []}
    finally:
        stop_flag.set()
        server.shutdown()
