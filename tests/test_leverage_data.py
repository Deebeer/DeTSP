"""第七轮测试：杠杆与保证金模式数据维度（活接口核实后实现）。

数据来源口径（2026-11-04 逐所活接口核实）：
- HL 家族：meta universe 的 maxLeverage 直给；onlyIsolated /
  marginMode=noCross → isolated_only，其余 cross；
- Lighter：orderBookDetails 的 min_initial_margin_fraction
  （万分比，10000=100%）→ 最高杠杆 = 10000/它；
  market_config.market_margin_mode 0=cross / 1=isolated_only；
- Aster：exchangeInfo 的 requiredMarginPercent（百分数）→
  最高杠杆 = 100/它；保证金模式无公开字段 → unknown；
- Arcus：/v1/markets 的 initialMarginFraction（小数）→
  最高杠杆 = 1/它；保证金模式无公开字段 → unknown。
pairs 行须带两腿各自的 max_leverage / margin_mode；某所的
杠杆数据源失败只丢该维度（未知），不拖垮价格/资金费。
全部离线（假 HTTP）。
"""

import pytest

from detsp.core.market_scanner import (MarketScanner, _lev_direct,
                                       _lev_from_fraction)


class _Resp:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


class FakeLevHttp:
    """带杠杆字段的全量假数据（字段形状照活接口实测）。

    details_broken=True 时 Lighter 的 orderBookDetails 抛错，
    模拟杠杆数据源单独失败。
    """

    def __init__(self, details_broken: bool = False):
        self.details_broken = details_broken

    def post(self, url, json=None, params=None):
        assert "hyperliquid" in url
        body = json or {}
        t = body.get("type")
        if t == "metaAndAssetCtxs":
            dex = body.get("dex", "")
            if dex == "":
                return _Resp([
                    {"universe": [
                        {"name": "BTC", "maxLeverage": 40},
                        {"name": "ETH", "maxLeverage": 25}]},
                    [{"markPx": "60000", "prevDayPx": "57000",
                      "funding": "0.0001"},
                     {"markPx": "3000", "prevDayPx": "2700",
                      "funding": "0.0005"}]])
            if dex == "io":
                return _Resp([
                    {"universe": [
                        {"name": "io:AAPL", "maxLeverage": 10,
                         "onlyIsolated": True,
                         "marginMode": "noCross"}]},
                    [{"markPx": "200", "prevDayPx": "190",
                      "funding": "0.00003"}]])
            if dex == "xyz":
                return _Resp([
                    {"universe": [
                        {"name": "xyz:SNDK", "maxLeverage": 5}]},
                    [{"markPx": "250", "prevDayPx": "240",
                      "funding": "0.00001"}]])
        if t == "predictedFundings":
            return _Resp([])
        raise AssertionError(f"未预期的 POST {body}")

    def get(self, url, params=None):
        if "arcus" in url and "/v1/markets" in url:
            return _Resp({"markets": [
                {"baseAsset": "BTC", "marketDisplayName": "BTC-USD",
                 "nextFundingRate": "0.00036", "markPrice": "60050",
                 "priceChange24h": "0.05",
                 "initialMarginFraction": "0.025"},
                {"baseAsset": "AAPL", "marketDisplayName": "AAPL-USD",
                 "nextFundingRate": "0.0001", "markPrice": "200",
                 "priceChange24h": "0.01",
                 "initialMarginFraction": "0.05"}]})
        if "arcus" in url and "/v1/bbo/" in url:
            return _Resp({"bestBid": {"price": "60040"},
                          "bestAsk": {"price": "60060"}})
        if "asterdex" in url and "premiumIndex" in url:
            return _Resp([
                {"symbol": "BTCUSDT", "markPrice": "60100",
                 "lastFundingRate": "0.0008"}])
        if "asterdex" in url and "exchangeInfo" in url:
            return _Resp({"symbols": [
                {"symbol": "BTCUSDT", "status": "TRADING",
                 "requiredMarginPercent": "5.0000"}]})
        if "asterdex" in url and "ticker/24hr" in url:
            return _Resp([
                {"symbol": "BTCUSDT", "priceChangePercent": "1.0"}])
        if "zklighter" in url and "orderBookDetails" in url:
            if self.details_broken:
                raise RuntimeError("details down")
            return _Resp({"order_book_details": [
                {"symbol": "BTC", "market_id": 1, "market_type": "perp",
                 "min_initial_margin_fraction": 200,
                 "market_config": {"market_margin_mode": 0}},
                {"symbol": "AAPL", "market_id": 3, "market_type": "perp",
                 "min_initial_margin_fraction": 500,
                 "market_config": {"market_margin_mode": 1}}]})
        if "zklighter" in url and "orderBooks" in url:
            return _Resp({"order_books": [
                {"symbol": "BTC", "market_id": 1, "status": "active",
                 "market_type": "perp"},
                {"symbol": "AAPL", "market_id": 3, "status": "active",
                 "market_type": "perp"}]})
        if "zklighter" in url and "funding-rates" in url:
            return _Resp({"funding_rates": [
                {"market_id": 1, "exchange": "lighter", "rate": 0.00016},
                {"market_id": 3, "exchange": "lighter", "rate": 0.00024}]})
        if "zklighter" in url and "exchangeStats" in url:
            return _Resp({"order_book_stats": [
                {"symbol": "BTC", "last_trade_price": 59999,
                 "daily_price_change": 1.5},
                {"symbol": "AAPL", "last_trade_price": 199,
                 "daily_price_change": 0.5}]})
        if "zklighter" in url and "orderBookOrders" in url:
            return _Resp({"bids": [{"price": "60012"}],
                          "asks": [{"price": "60018"}]})
        if "rh.lighter" in url and "orderBookDetails" in url:
            if self.details_broken:
                raise RuntimeError("details down")
            return _Resp({"order_book_details": [
                {"symbol": "BTC", "market_id": 1, "market_type": "perp",
                 "min_initial_margin_fraction": 250,
                 "market_config": {"market_margin_mode": 0}}]})
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
    return MarketScanner(http=FakeLevHttp(), watchlist=["BTC", "AAPL"])


def _row(d, sym):
    rows = {r["symbol"]: r for r in d["rows"]}
    assert sym in rows, f"pairs 应含 {sym}：{list(rows)}"
    return rows[sym]


# ── 换算口径 ────────────────────────────────────────────────

def test_lev_conversion_units():
    # Lighter 万分比：200 = 2% → 50x；3333 ≈ 33.33% → 3x
    assert _lev_from_fraction(200, 10_000.0) == 50
    assert _lev_from_fraction(3333, 10_000.0) == 3
    assert _lev_from_fraction(500, 10_000.0) == 20
    # Aster 百分数：5.0 → 20x；33.33 → 3x
    assert _lev_from_fraction("5.0000", 100.0) == 20
    assert _lev_from_fraction("33.3300", 100.0) == 3
    # Arcus 小数：0.025 → 40x
    assert _lev_from_fraction("0.025", 1.0) == 40
    # 缺失/非法 → 未知，不猜
    assert _lev_from_fraction(None, 100.0) is None
    assert _lev_from_fraction(0, 100.0) is None
    assert _lev_from_fraction("abc", 100.0) is None
    assert _lev_direct(40) == 40
    assert _lev_direct(None) is None


# ── pairs 行带两腿杠杆/保证金模式 ───────────────────────────

def test_pairs_hl_lighter_leverage(scanner):
    d = scanner.pairs("hyperliquid", "lighter", "funding_carry")
    btc = _row(d, "BTC")
    assert btc["max_leverage_a"] == 40          # HL maxLeverage 直给
    assert btc["margin_mode_a"] == "cross"      # 无 onlyIsolated
    assert btc["max_leverage_b"] == 50          # 10000 / 200
    assert btc["margin_mode_b"] == "cross"      # market_margin_mode 0


def test_pairs_isolated_only_both_legs(scanner):
    d = scanner.pairs("entropy", "lighter", "funding_carry")
    aapl = _row(d, "AAPL")
    # io 子所：onlyIsolated + marginMode=noCross → 仅逐仓，10x
    assert aapl["max_leverage_a"] == 10
    assert aapl["margin_mode_a"] == "isolated_only"
    # Lighter AAPL：10000 / 500 = 20x，market_margin_mode 1 → 仅逐仓
    assert aapl["max_leverage_b"] == 20
    assert aapl["margin_mode_b"] == "isolated_only"


def test_pairs_arcus_aster_leverage_mode_unknown(scanner):
    d = scanner.pairs("arcus", "aster", "premium_arb")
    btc = _row(d, "BTC")
    assert btc["max_leverage_a"] == 40          # 1 / 0.025
    assert btc["margin_mode_a"] == "unknown"    # Arcus 无公开字段
    assert btc["max_leverage_b"] == 20          # 100 / 5.0
    assert btc["margin_mode_b"] == "unknown"    # Aster 无公开字段


def test_pairs_lighter_rh_leverage(scanner):
    d = scanner.pairs("lighter_rh", "hyperliquid", "funding_carry")
    btc = _row(d, "BTC")
    assert btc["max_leverage_a"] == 40          # RH: 10000 / 250
    assert btc["margin_mode_a"] == "cross"
    assert btc["max_leverage_b"] == 40


def test_lighter_details_failure_degrades_to_unknown():
    # orderBookDetails 单独失败：杠杆维度未知，价格/资金费不受影响
    sc = MarketScanner(http=FakeLevHttp(details_broken=True),
                       watchlist=["BTC"])
    d = sc.pairs("hyperliquid", "lighter", "funding_carry")
    btc = _row(d, "BTC")
    assert btc["max_leverage_b"] is None
    assert btc["margin_mode_b"] == "unknown"
    assert btc["has_price"] and btc["has_funding"]
    assert btc["max_leverage_a"] == 40          # 另一所不受影响
