"""数据层三态与根因修复的测试：
- 上市三态判定（ok / not_listed / error），mock 各所市场列表；
- 未上市不打 candles 上游、coin 前缀构造、上游异常转中文短句；
- 排行只纳入两腿都 ok 的组合（半残格不参与）；
- Lighter 两部署 market_id 不串所（quotes 按部署分缓存）。
全部离线（假 HTTP）。"""

import json

from detsp.core.market_scanner import MarketScanner
from detsp.venues.quotes import PublicQuoteProvider


class _Resp:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


class _Boom:
    """模拟上游 500：raise_for_status 抛带英文原文的异常。"""

    def raise_for_status(self):
        raise RuntimeError(
            "Server error '500 Internal Server Error' for url "
            "'https://api.hyperliquid.xyz/info'")

    def json(self):  # pragma: no cover
        return None


class FakeHttp2:
    """全所假数据：HL 本体只有 BTC；io 有 SNDK；xyz 有 SNDK；
    aster 清单含 BTC/SNDK；lighter 主网 BTC 有价、SNDK 有费率无价
    （半残格）；arcus 接口整体故障。记录 candleSnapshot 的 coin。"""

    def __init__(self):
        self.candle_coins: list[str] = []
        self.candle_boom = False

    def post(self, url, json=None, params=None):
        assert "hyperliquid" in url
        body = json or {}
        t = body.get("type")
        if t == "metaAndAssetCtxs":
            dex = body.get("dex", "")
            if dex == "":
                return _Resp([{"universe": [{"name": "BTC"}]},
                              [{"markPx": "60000", "funding": "0.00001"}]])
            if dex == "io":
                return _Resp([{"universe": [{"name": "io:SNDK"}]},
                              [{"markPx": "250", "funding": "0.00002"}]])
            if dex == "xyz":
                return _Resp([{"universe": [{"name": "xyz:SNDK"}]},
                              [{"markPx": "251", "funding": "0.00004"}]])
        if t == "predictedFundings":
            return _Resp([])
        if t == "candleSnapshot":
            coin = (body.get("req") or {}).get("coin")
            self.candle_coins.append(coin)
            if self.candle_boom:
                return _Boom()
            return _Resp([{"h": "110", "l": "90"}, {"h": "120", "l": "95"}])
        raise AssertionError(f"未预期的 POST {body}")

    def get(self, url, params=None):
        if "arcus" in url:
            raise RuntimeError("arcus down")
        if "asterdex" in url and "premiumIndex" in url:
            return _Resp([
                {"symbol": "BTCUSDT", "markPrice": "60100",
                 "lastFundingRate": "0.0008"},
                {"symbol": "SNDKUSDT", "markPrice": "252",
                 "lastFundingRate": "0.0008"}])
        if "asterdex" in url and "exchangeInfo" in url:
            return _Resp({"symbols": [
                {"symbol": "BTCUSDT", "status": "TRADING"},
                {"symbol": "SNDKUSDT", "status": "TRADING"},
                {"symbol": "OLDUSDT", "status": "BREAK"}]})
        if "zklighter" in url and "orderBooks" in url:
            return _Resp({"order_books": [
                {"symbol": "BTC", "market_id": 1, "status": "active"},
                {"symbol": "SNDK", "market_id": 2, "status": "active"}]})
        if "zklighter" in url and "funding-rates" in url:
            return _Resp({"funding_rates": [
                {"market_id": 1, "exchange": "lighter", "rate": 0.00016},
                {"market_id": 2, "exchange": "lighter", "rate": 0.00024}]})
        if "zklighter" in url and "orderBookOrders" in url:
            # BTC 有盘口；SNDK 盘口为空 → 有费率无价格（半残格）
            if (params or {}).get("market_id") == 1:
                return _Resp({"bids": [{"price": "60010"}],
                              "asks": [{"price": "60020"}]})
            return _Resp({"bids": [], "asks": []})
        if "rh.lighter" in url:
            return _Resp({"order_books": [], "funding_rates": []})
        raise AssertionError(f"未预期的 GET {url}")


def _scanner():
    return MarketScanner(http=FakeHttp2(), watchlist=["BTC", "SNDK"])


# ── 三态判定 ───────────────────────────────────────────────

def test_three_states():
    out = _scanner().scan()
    by = {s["symbol"]: s for s in out["symbols"]}
    # 未上市：SNDK 不在 HL 本体 universe、BTC 不在 io/xyz
    assert by["SNDK"]["venues"]["hyperliquid"]["status"] == "not_listed"
    assert by["BTC"]["venues"]["entropy"]["status"] == "not_listed"
    assert by["BTC"]["venues"]["tradexyz"]["status"] == "not_listed"
    # 故障：arcus 整体挂 → error + 中文短句，不带英文原文
    arc = by["BTC"]["venues"]["arcus"]
    assert arc["status"] == "error"
    assert arc["reason"] == "行情接口暂时异常"
    blob = json.dumps(out, ensure_ascii=False)
    assert "Internal Server Error" not in blob
    assert "RuntimeError" not in blob and "arcus down" not in blob
    # ok：有价的格
    assert by["BTC"]["venues"]["hyperliquid"]["status"] == "ok"
    assert by["SNDK"]["venues"]["entropy"]["status"] == "ok"
    assert by["SNDK"]["venues"]["aster"]["status"] == "ok"
    # 半残格：lighter 的 SNDK 有费率无价格 → error（价格暂时取不到）
    half = by["SNDK"]["venues"]["lighter"]
    assert half["status"] == "error" and half["reason"] == "价格暂时取不到"
    assert half["funding_bps_h"] is not None  # 费率仍如实附带
    # 上市计数：SNDK 在 io/xyz/aster/lighter 上市（HL 未上市、arcus 故障不计）
    assert by["SNDK"]["listed_count"] == 4
    assert by["BTC"]["listed_count"] == 3  # HL/lighter/aster


def test_rankings_only_ok_legs():
    out = _scanner().scan()
    # SNDK 费差：可用腿只有 io/xyz/aster（lighter 半残、HL 未上市、
    # arcus 故障）——组合里绝不能出现 lighter / hyperliquid / arcus
    sndk_f = [r for r in out["funding_ranking"] if r["symbol"] == "SNDK"]
    assert sndk_f, "SNDK 应仍有合法费差组合（io/xyz/aster 之间）"
    for r in sndk_f:
        assert r["long_venue"] not in ("lighter", "hyperliquid", "arcus")
        assert r["short_venue"] not in ("lighter", "hyperliquid", "arcus")
    # SNDK 价差同理：lighter 无价不得参与
    sndk_s = [r for r in out["spread_ranking"] if r["symbol"] == "SNDK"]
    for r in sndk_s:
        assert "lighter" not in (r["buy_venue"], r["sell_venue"])
        assert "hyperliquid" not in (r["buy_venue"], r["sell_venue"])


# ── candles：未上市不打上游 / 前缀 / 异常转中文 ─────────────

def test_candles_not_listed_never_hits_upstream():
    http = FakeHttp2()
    sc = MarketScanner(http=http, watchlist=["BTC", "SNDK"])
    out = sc.candles("hyperliquid", "SNDK")
    assert out["ok"] is False
    assert out["error"] == "该平台未上市此标的"
    assert out.get("not_listed") is True
    assert http.candle_coins == []  # 一次都没打上游


def test_candles_coin_prefix_for_sub_dex():
    http = FakeHttp2()
    sc = MarketScanner(http=http, watchlist=["BTC", "SNDK"])
    out = sc.candles("tradexyz", "SNDK")
    assert out["ok"] is True
    assert http.candle_coins == ["xyz:SNDK"]
    out = sc.candles("entropy", "SNDK")
    assert out["ok"] is True
    assert http.candle_coins[-1] == "io:SNDK"
    out = sc.candles("hyperliquid", "BTC")
    assert out["ok"] is True
    assert http.candle_coins[-1] == "BTC"


def test_candles_upstream_error_becomes_chinese():
    http = FakeHttp2()
    http.candle_boom = True
    sc = MarketScanner(http=http, watchlist=["BTC", "SNDK"])
    out = sc.candles("hyperliquid", "BTC")
    assert out["ok"] is False
    blob = json.dumps(out, ensure_ascii=False)
    assert "Internal Server Error" not in blob
    assert "500" not in blob and "hyperliquid.xyz" not in blob
    assert out["error"] == "K线数据暂时取不到，请稍后重试"


# ── quotes：Lighter 两部署不串所 ────────────────────────────

class FakeQuoteHttp:
    """主网 SOL 的 market_id=2、RH 的 SOL 的 market_id=3；
    两部署各 id 的盘口价不同，串用立刻露馅。"""

    def get(self, url, params=None):
        rh = "rh.lighter" in url
        if "orderBooks" in url:
            if rh:
                return _Resp({"order_books": [
                    {"symbol": "SOL", "market_id": 3, "status": "active"}]})
            return _Resp({"order_books": [
                {"symbol": "SOL", "market_id": 2, "status": "active"}]})
        if "orderBookOrders" in url:
            mid_id = (params or {}).get("market_id")
            book = {(False, 2): ("100", "102"),   # 主网 SOL
                    (True, 3): ("200", "202"),    # RH SOL（本所 id）
                    (True, 2): ("999", "999")}    # RH 串用主网 id 的别家盘口
            bid, ask = book[(rh, mid_id)]
            return _Resp({"bids": [{"price": bid}],
                          "asks": [{"price": ask}]})
        raise AssertionError(f"未预期的 GET {url}")

    def post(self, url, json=None, params=None):  # pragma: no cover
        raise AssertionError("quotes 测试不应 POST")


def test_lighter_quotes_no_cross_deployment():
    q = PublicQuoteProvider(http=FakeQuoteHttp(), cache_seconds=0)
    # 先问 RH（旧实现会把先到者的映射毒化给另一部署）
    assert q.mid("lighter_rh", "SOL") == 201.0
    assert q.mid("lighter", "SOL") == 101.0
    # 反序再验一遍（新实例、先主网后 RH）
    q2 = PublicQuoteProvider(http=FakeQuoteHttp(), cache_seconds=0)
    assert q2.mid("lighter", "SOL") == 101.0
    assert q2.mid("lighter_rh", "SOL") == 201.0
