"""面板数据页的公开行情扫描（只读、不用任何密钥）。

给四个策略「看数据选标的」用：对观察列表里的每个币种，一次扫
7 个场所的公开价格与资金费（归一到每小时 bps，并如实标注取值
口径 rate_kind，与 venues 适配的标注口径一致），再算两个组合：

- 最大价差组合（哪所买、哪所卖、价差 bps）——溢价套利看；
- 最优资金费组合（多哪所、空哪所、净费差 bps/小时）——资金费
  carry 与轮换对冲看。口径（rate_kind）不同的两腿相减没有意义
  （审查 H2，策略选向也会拒绝），组合优先在同口径场所里选；
  只有混口径组合时才返回并标 mixed=True，前端据此不打
  「当前最优」、只作参考。

数据源（全部公开接口，与 venues/quotes.py 同源）：
- HL 家族（hyperliquid/entropy/tradexyz）：POST /info
  metaAndAssetCtxs（标记价 + 当期费率），另以 predictedFundings
  把能拿到的条目升级为下期预测口径（与 HL 适配 funding() 的
  主口径一致）；
- Aster：GET /fapi/v1/premiumIndex（标记价 + 上期已结算费率，
  8 小时周期）；
- Lighter 主网 / Robinhood 链：orderBooks 定 market_id，
  funding-rates 取当期费率（8 小时周期）；价格走
  PublicQuoteProvider 的 orderBookOrders 买卖一中间价；
- Arcus：GET /v1/markets（下期预测费率为主）+ /v1/bbo 取中间价。

单所失败隔离：每个场所的抓取独立 try，失败只把该所标进
venue_errors、该所数据缺失，不拖垮整张表。整表带缓存（默认
20 秒），面板自动刷新不会把公开接口打爆。
"""

from __future__ import annotations

import time

from .risk import funding_hourly_rate
from .venue import FundingInfo

DEFAULT_WATCHLIST = ["BTC", "ETH", "SOL", "HYPE", "AAPL", "SNDK"]

HL_INFO_URL = "https://api.hyperliquid.xyz/info"
ASTER_BASE = "https://fapi.asterdex.com"
ARCUS_BASE = "https://api.arcus.xyz"
LIGHTER_BASES = {
    "lighter": "https://mainnet.zklighter.elliot.ai/api/v1",
    "lighter_rh": "https://api.rh.lighter.xyz/api/v1",
}
HL_DEX = {"hyperliquid": "", "entropy": "io", "tradexyz": "xyz"}
_LIGHTER_FUNDING_INTERVAL = 28800.0   # Lighter 费率周期 8 小时
_ASTER_FUNDING_INTERVAL = 28800.0     # Aster 结算周期 8 小时


def _f(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out else None


def _plain(name: str) -> str:
    return name.split(":", 1)[1] if ":" in name else name


def _bps_per_hour(rate: float, interval_seconds: float) -> float:
    """原始费率 → 每小时 bps（统一口径，与 risk 的换算一致）。"""
    return funding_hourly_rate(rate, interval_seconds) * 10_000


def best_spread(prices: dict[str, float | None]) -> dict | None:
    """最大价差组合：在最便宜的所买、在最贵的所卖。

    prices 是 {venue_id: 中间价}，None/缺失视为无数据。价差 bps
    以两所中间价的均值为基准。不足两所返回 None。
    """
    usable = {v: p for v, p in prices.items() if p is not None and p > 0}
    if len(usable) < 2:
        return None
    buy = min(usable, key=lambda v: usable[v])
    sell = max(usable, key=lambda v: usable[v])
    if buy == sell:
        return None
    mid = (usable[buy] + usable[sell]) / 2
    return {"buy_venue": buy, "sell_venue": sell,
            "spread_bps": (usable[sell] - usable[buy]) / mid * 10_000}


def best_funding_pair(
        rates: dict[str, tuple[float, str]]) -> dict | None:
    """最优资金费组合：多腿放费率低的所、空腿放费率高的所。

    rates 是 {venue_id: (每小时 bps, rate_kind)}。约定费率为正 =
    多头付费：多 L 空 S 的组合每小时净收 = bps[S] - bps[L]。
    口径不同的两腿不能相减选向（H2）：先只在同口径组里选最优；
    没有任何同口径组合时才返回混口径最优并标 mixed=True。
    """
    venues = list(rates)
    if len(venues) < 2:
        return None

    def _edge(long_v: str, short_v: str) -> float:
        return rates[short_v][0] - rates[long_v][0]

    def _best(pairs) -> dict | None:
        best = None
        for long_v, short_v in pairs:
            e = _edge(long_v, short_v)
            if best is None or e > best["edge_bps_h"]:
                best = {"long_venue": long_v, "short_venue": short_v,
                        "edge_bps_h": e,
                        "rate_kind": rates[long_v][1],
                        "mixed": rates[long_v][1] != rates[short_v][1]}
        return best

    all_pairs = [(a, b) for a in venues for b in venues if a != b]
    same = [(a, b) for a, b in all_pairs
            if rates[a][1] == rates[b][1]]
    if same:
        return _best(same)
    return _best(all_pairs)


class MarketScanner:
    """公开行情扫描器：scan() 出整表（带缓存），candles() 出 24h 高低。"""

    def __init__(self, http=None, cache_seconds: float = 20.0,
                 timeout: float = 6.0, watchlist: list[str] | None = None,
                 quotes=None) -> None:
        self._http = http            # 测试可注入假 httpx.Client
        self._cache_seconds = max(10.0, float(cache_seconds))
        self._timeout = timeout
        self.watchlist = [str(s).upper() for s in
                          (watchlist or DEFAULT_WATCHLIST)]
        self._quotes = quotes        # PublicQuoteProvider（lighter 取价）
        self._cache: tuple[float, dict] | None = None
        self._lighter_maps: dict[str, dict[str, int]] = {}

    # ----- HTTP -----
    def _client(self):
        if self._http is not None:
            return self._http
        import httpx

        self._http = httpx.Client(timeout=self._timeout)
        return self._http

    def _quote_mid(self, venue_id: str, symbol: str) -> float | None:
        if self._quotes is None:
            from ..venues.quotes import PublicQuoteProvider

            self._quotes = PublicQuoteProvider(http=self._http)
        try:
            return self._quotes.mid(venue_id, symbol)
        except Exception:  # noqa: BLE001 — 取价失败即无数据
            return None

    # ----- 各所抓取：返回 {symbol: {"price":…, "funding": FundingInfo}} -----
    def _fetch_hl_family(self, errors: dict[str, str]) -> dict[str, dict[str, dict]]:
        out: dict[str, dict[str, dict]] = {v: {} for v in HL_DEX}
        for vid, dex in HL_DEX.items():
            try:
                r = self._client().post(
                    HL_INFO_URL, json={"type": "metaAndAssetCtxs", "dex": dex})
                r.raise_for_status()
                meta, ctxs = r.json()
            except Exception as e:  # noqa: BLE001 — 子所级失败隔离
                errors[vid] = f"{type(e).__name__}: {e}"[:200]
                continue
            for a, ctx in zip(meta.get("universe", []), ctxs or []):
                sym = _plain(str(a.get("name") or "")).upper()
                if sym not in self.watchlist:
                    continue
                price = _f((ctx or {}).get("markPx"))
                rate = _f((ctx or {}).get("funding"))
                funding = FundingInfo(
                    symbol=sym, rate=rate or 0.0, interval_seconds=3600,
                    rate_kind="current") if rate is not None else None
                out[vid][sym] = {"price": price, "funding": funding}
        # 下期预测：能对上的条目把口径升级为 predicted（与 HL 适配
        # funding() 的主口径一致）；接口失败不影响当期口径结果
        try:
            r = self._client().post(
                HL_INFO_URL, json={"type": "predictedFundings"})
            r.raise_for_status()
            for entry in r.json() or []:
                if not isinstance(entry, (list, tuple)) or len(entry) != 2:
                    continue
                coin, venues = entry
                coin = str(coin)
                if ":" in coin:
                    prefix, sym = coin.split(":", 1)
                    vid = {"io": "entropy", "xyz": "tradexyz"}.get(prefix)
                else:
                    vid, sym = "hyperliquid", coin
                sym = sym.upper()
                if vid is None or sym not in self.watchlist:
                    continue
                for vname, payload in venues or []:
                    # 活接口场所名实测为 "HlPerp"（与 hl.py 同一坑）
                    if str(vname).lower() not in ("hyperliquid", "hlperp"):
                        continue
                    rate = _f((payload or {}).get("fundingRate"))
                    if rate is None:
                        continue
                    cell = out.setdefault(vid, {}).setdefault(
                        sym, {"price": None, "funding": None})
                    nts = _f((payload or {}).get("nextFundingTime")) or 0
                    cell["funding"] = FundingInfo(
                        symbol=sym, rate=rate,
                        next_ts=nts / 1000 if nts > 1e10 else nts,
                        interval_seconds=3600, rate_kind="predicted")
        except Exception:  # noqa: BLE001 — 预测升级失败保留当期口径
            pass
        return out

    def _fetch_aster(self) -> dict[str, dict]:
        r = self._client().get(f"{ASTER_BASE}/fapi/v1/premiumIndex")
        r.raise_for_status()
        out: dict[str, dict] = {}
        for row in r.json() or []:
            if not isinstance(row, dict):
                continue
            raw = str(row.get("symbol") or "")
            sym = raw[:-4].upper() if raw.upper().endswith("USDT") else ""
            if sym not in self.watchlist:
                continue
            rate = _f(row.get("lastFundingRate"))
            out[sym] = {
                "price": _f(row.get("markPrice")),
                "funding": FundingInfo(
                    symbol=sym, rate=rate or 0.0,
                    interval_seconds=_ASTER_FUNDING_INTERVAL,
                    rate_kind="last_settled") if rate is not None else None,
            }
        return out

    def _fetch_lighter(self, vid: str) -> dict[str, dict]:
        base = LIGHTER_BASES[vid]
        if vid not in self._lighter_maps:
            r = self._client().get(f"{base}/orderBooks")
            r.raise_for_status()
            self._lighter_maps[vid] = {
                str(b.get("symbol", "")).upper(): int(b["market_id"])
                for b in (r.json() or {}).get("order_books", [])
                if b.get("status") == "active" and b.get("market_id") is not None}
        id2sym = {i: s for s, i in self._lighter_maps[vid].items()}
        r = self._client().get(f"{base}/funding-rates")
        r.raise_for_status()
        out: dict[str, dict] = {}
        for row in (r.json() or {}).get("funding_rates") or []:
            if str(row.get("exchange") or "").lower() != "lighter":
                continue
            try:
                sym = id2sym.get(int(row.get("market_id")))
            except (TypeError, ValueError):
                continue
            rate = _f(row.get("rate"))
            if sym is None or sym not in self.watchlist or rate is None:
                continue
            out[sym] = {"price": None, "funding": FundingInfo(
                symbol=sym, rate=rate,
                interval_seconds=_LIGHTER_FUNDING_INTERVAL,
                rate_kind="current")}
        for sym in self.watchlist:
            if sym in self._lighter_maps[vid]:
                price = self._quote_mid(vid, sym)
                cell = out.setdefault(sym, {"price": None, "funding": None})
                cell["price"] = price
        return out

    def _fetch_arcus(self) -> dict[str, dict]:
        r = self._client().get(f"{ARCUS_BASE}/v1/markets")
        r.raise_for_status()
        payload = r.json()
        rows = payload.get("markets") if isinstance(payload, dict) else payload
        out: dict[str, dict] = {}
        for raw in rows or []:
            if not isinstance(raw, dict):
                continue
            sym = str(raw.get("baseAsset") or "").upper()
            if sym not in self.watchlist:
                continue
            rate = _f(raw.get("nextFundingRate"))
            kind = "predicted"
            if rate is None:
                rate = _f(raw.get("fundingRate"))
                kind = "current"
            cell = {"price": None, "funding": None,
                    "_arcus_symbol": str(raw.get("marketDisplayName") or "")}
            if rate is not None:
                cell["funding"] = FundingInfo(
                    symbol=sym, rate=rate, interval_seconds=3600,
                    rate_kind=kind)
            out[sym] = cell
        # 价格走公开 BBO（未签名的只读端点，与 arcus 适配 book() 同源）
        for sym, cell in out.items():
            arcus_symbol = cell.pop("_arcus_symbol", "")
            if not arcus_symbol:
                continue
            try:
                rb = self._client().get(f"{ARCUS_BASE}/v1/bbo/{arcus_symbol}")
                rb.raise_for_status()
                body = rb.json() or {}
                bid = _f(_side(body.get("bestBid")))
                ask = _f(_side(body.get("bestAsk")))
                if bid and ask:
                    cell["price"] = (bid + ask) / 2
                else:
                    cell["price"] = bid or ask
            except Exception:  # noqa: BLE001 — BBO 失败只丢价格
                cell["price"] = None
        return out

    # ----- 整表 -----
    def scan(self, force: bool = False) -> dict:
        now = time.time()
        if not force and self._cache and \
                now - self._cache[0] < self._cache_seconds:
            return self._cache[1]
        per_venue: dict[str, dict[str, dict]] = {}
        errors: dict[str, str] = {}

        def _grab(vid: str, fn) -> None:
            try:
                per_venue[vid] = fn()
            except Exception as e:  # noqa: BLE001 — 单所失败隔离
                per_venue[vid] = {}
                errors[vid] = f"{type(e).__name__}: {e}"[:200]

        hl_errors: dict[str, str] = {}
        hl = self._fetch_hl_family(hl_errors)
        for vid in HL_DEX:
            per_venue[vid] = hl.get(vid, {})
        errors.update(hl_errors)
        _grab("aster", self._fetch_aster)
        _grab("arcus", self._fetch_arcus)
        for vid in LIGHTER_BASES:
            _grab(vid, lambda v=vid: self._fetch_lighter(v))

        symbols_out = []
        spread_ranking, funding_ranking = [], []
        for sym in self.watchlist:
            venues_cell: dict[str, dict | None] = {}
            prices: dict[str, float | None] = {}
            rates: dict[str, tuple[float, str]] = {}
            for vid in ("arcus", "lighter", "lighter_rh", "hyperliquid",
                        "entropy", "tradexyz", "aster"):
                cell = (per_venue.get(vid) or {}).get(sym)
                if not cell or (cell.get("price") is None
                                and cell.get("funding") is None):
                    venues_cell[vid] = None
                    continue
                finfo: FundingInfo | None = cell.get("funding")
                bps_h = None
                if finfo is not None:
                    bps_h = round(_bps_per_hour(finfo.rate,
                                                finfo.interval_seconds), 4)
                    rates[vid] = (bps_h, finfo.rate_kind)
                venues_cell[vid] = {
                    "price": cell.get("price"),
                    "funding_bps_h": bps_h,
                    "rate_kind": finfo.rate_kind if finfo else None,
                }
                prices[vid] = cell.get("price")
            spread = best_spread(prices)
            funding = best_funding_pair(rates)
            if spread:
                spread_ranking.append({"symbol": sym, **spread})
            if funding:
                funding_ranking.append({"symbol": sym, **funding})
            symbols_out.append({"symbol": sym, "venues": venues_cell,
                                "best_spread": spread,
                                "best_funding": funding})
        spread_ranking.sort(key=lambda x: -x["spread_bps"])
        funding_ranking.sort(key=lambda x: -x["edge_bps_h"])
        result = {
            "updated_at": now,
            "watchlist": list(self.watchlist),
            "symbols": symbols_out,
            "spread_ranking": spread_ranking,
            "funding_ranking": funding_ranking,
            "venue_errors": errors,
        }
        self._cache = (now, result)
        return result

    # ----- 24 小时高低（中性网格定区间用）-----
    def candles(self, venue_id: str, symbol: str) -> dict:
        """近 24 小时 1 小时 K 线的高低与建议区间（高低各扩 2%）。

        只有 HL 家族有已核实的官方公开 K 线接口（candleSnapshot）；
        其他场所如实返回不可用。
        """
        symbol = str(symbol or "").upper()
        base = {"venue": venue_id, "symbol": symbol}
        if venue_id not in HL_DEX:
            return {**base, "ok": False,
                    "error": "该场所没有已核实的公开 K 线接口，"
                             "请用 Hyperliquid 家族场所看区间"}
        dex = HL_DEX[venue_id]
        coin = f"{dex}:{symbol}" if dex else symbol
        end = int(time.time() * 1000)
        start = end - 24 * 3600 * 1000
        try:
            r = self._client().post(HL_INFO_URL, json={
                "type": "candleSnapshot",
                "req": {"coin": coin, "interval": "1h",
                        "startTime": start, "endTime": end}})
            r.raise_for_status()
            rows = r.json() or []
        except Exception as e:  # noqa: BLE001
            return {**base, "ok": False, "error": f"K 线获取失败：{e}"}
        highs = [h for h in (_f(row.get("h")) for row in rows
                             if isinstance(row, dict)) if h]
        lows = [lo for lo in (_f(row.get("l")) for row in rows
                              if isinstance(row, dict)) if lo]
        if not highs or not lows:
            return {**base, "ok": False,
                    "error": f"{venue_id} 没有 {symbol} 的 K 线（可能未上市）"}
        high, low = max(highs), min(lows)
        return {**base, "ok": True, "hours": 24, "candles": len(rows),
                "high": high, "low": low,
                "suggested_lower": round(low * 0.98, 6),
                "suggested_upper": round(high * 1.02, 6)}


def _side(side) -> object:
    return side.get("price") if isinstance(side, dict) else side
