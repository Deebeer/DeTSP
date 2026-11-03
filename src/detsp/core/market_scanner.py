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

两类组合只纳入「两腿都 ok」的格：状态非 ok（未上市/数据异常）
或缺价格/缺费率的格一律不参与，避免拿半残数据算出假机会。

**每格三态（status）**——把「没上市」与「取数失败」分开（用户
实测痛点：SNDK 在 Hyperliquid 本体根本未上市，却与故障一样
显示「无数据」）：
- ``ok``：该所上市此标的且有价（费率可缺，缺了单标费率 -）；
- ``not_listed``：该所的市场清单里没有此标的（未上市）；
- ``error``：接口异常或上市却取不到价，reason 给中文短句。
上市清单来自各所自己的市场列表（HL 家族各 dex 的 meta
universe、Lighter orderBooks、Aster exchangeInfo、
Arcus /v1/markets），随扫描一并缓存；K 线接口也先查它，未
上市直接回中文短句、不打上游（上游对未上市 coin 会回 500，
该英文原文绝不能甩给用户）。

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

# 面向用户的中文短句：上游异常原文（英文/堆栈）只进内部，绝不外露
ERR_VENUE_DOWN = "行情接口暂时异常"
ERR_NO_PRICE = "价格暂时取不到"
ERR_NO_DATA = "数据暂时取不到"
ERR_NO_CANDLES = "K线数据暂时取不到，请稍后重试"
ERR_NOT_LISTED = "该平台未上市此标的"


def _log(msg: str) -> None:
    """内部细节只进标准错误日志，不进任何 API 输出。"""
    import sys

    print(f"[market_scanner] {msg}", file=sys.stderr)


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
        # 逐所上市清单 {venue_id: set(symbol)}，随 scan 一并刷新；
        # 某所抓取失败时该所不写入（=清单未知，不当未上市处理）
        self._listings: dict[str, set[str]] = {}

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
    def _fetch_hl_family(self, errors: dict[str, str],
                         listings: dict[str, set[str]]) -> dict[str, dict[str, dict]]:
        out: dict[str, dict[str, dict]] = {v: {} for v in HL_DEX}
        for vid, dex in HL_DEX.items():
            try:
                r = self._client().post(
                    HL_INFO_URL, json={"type": "metaAndAssetCtxs", "dex": dex})
                r.raise_for_status()
                meta, ctxs = r.json()
            except Exception as e:  # noqa: BLE001 — 子所级失败隔离
                errors[vid] = ERR_VENUE_DOWN
                _log(f"{vid} metaAndAssetCtxs 失败：{type(e).__name__}: {e}")
                continue
            # 上市清单 = 该 dex 的 universe 全集（下架标记的除外）
            listings[vid] = {
                _plain(str(a.get("name") or "")).upper()
                for a in meta.get("universe", [])
                if isinstance(a, dict) and not a.get("isDelisted")}
            listings[vid].discard("")
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

    def _fetch_aster(self, listings: dict[str, set[str]]) -> dict[str, dict]:
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
        # 上市清单走 exchangeInfo（status=TRADING 的 USDT 永续）；
        # 该接口单独失败时退回「premiumIndex 有条目即上市」，不拖垮主数据
        listed: set[str] | None = None
        try:
            re_ = self._client().get(f"{ASTER_BASE}/fapi/v1/exchangeInfo")
            re_.raise_for_status()
            listed = set()
            for row in re_.json().get("symbols", []) or []:
                if not isinstance(row, dict):
                    continue
                raw = str(row.get("symbol") or "")
                status = row.get("status")
                if raw.upper().endswith("USDT") and \
                        (status is None or str(status).upper() == "TRADING"):
                    listed.add(raw[:-4].upper())
        except Exception as e:  # noqa: BLE001 — 清单失败只降级判定口径
            _log(f"aster exchangeInfo 失败：{type(e).__name__}: {e}")
        listings["aster"] = listed if listed is not None else set(out)
        return out

    def _fetch_lighter(self, vid: str,
                       listings: dict[str, set[str]]) -> dict[str, dict]:
        base = LIGHTER_BASES[vid]
        if vid not in self._lighter_maps:
            r = self._client().get(f"{base}/orderBooks")
            r.raise_for_status()
            self._lighter_maps[vid] = {
                str(b.get("symbol", "")).upper(): int(b["market_id"])
                for b in (r.json() or {}).get("order_books", [])
                if b.get("status") == "active" and b.get("market_id") is not None}
        # 上市清单 = orderBooks 里 active 的市场全集
        listings[vid] = set(self._lighter_maps[vid])
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

    def _fetch_arcus(self, listings: dict[str, set[str]]) -> dict[str, dict]:
        r = self._client().get(f"{ARCUS_BASE}/v1/markets")
        r.raise_for_status()
        payload = r.json()
        rows = payload.get("markets") if isinstance(payload, dict) else payload
        # 上市清单 = /v1/markets 的 baseAsset 全集
        listings["arcus"] = {
            str(raw.get("baseAsset") or "").upper()
            for raw in rows or [] if isinstance(raw, dict)}
        listings["arcus"].discard("")
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
        listings: dict[str, set[str]] = {}

        def _grab(vid: str, fn) -> None:
            try:
                per_venue[vid] = fn()
            except Exception as e:  # noqa: BLE001 — 单所失败隔离
                per_venue[vid] = {}
                errors[vid] = ERR_VENUE_DOWN
                _log(f"{vid} 抓取失败：{type(e).__name__}: {e}")

        hl_errors: dict[str, str] = {}
        hl = self._fetch_hl_family(hl_errors, listings)
        for vid in HL_DEX:
            per_venue[vid] = hl.get(vid, {})
        errors.update(hl_errors)
        _grab("aster", lambda: self._fetch_aster(listings))
        _grab("arcus", lambda: self._fetch_arcus(listings))
        for vid in LIGHTER_BASES:
            _grab(vid, lambda v=vid: self._fetch_lighter(v, listings))
        self._listings = listings

        symbols_out = []
        spread_ranking, funding_ranking = [], []
        for sym in self.watchlist:
            venues_cell: dict[str, dict] = {}
            prices: dict[str, float | None] = {}
            rates: dict[str, tuple[float, str]] = {}
            listed_venues: list[str] = []
            for vid in ("arcus", "lighter", "lighter_rh", "hyperliquid",
                        "entropy", "tradexyz", "aster"):
                cell = (per_venue.get(vid) or {}).get(sym)
                finfo: FundingInfo | None = cell.get("funding") if cell else None
                bps_h = None
                if finfo is not None:
                    bps_h = round(_bps_per_hour(finfo.rate,
                                                finfo.interval_seconds), 4)
                listed_set = listings.get(vid)
                is_listed = listed_set is not None and sym in listed_set
                if is_listed:
                    listed_venues.append(vid)
                # 三态判定：场所故障 → 未上市 → 上市却无价/无数据 → ok
                if vid in errors:
                    venues_cell[vid] = {
                        "status": "error", "reason": errors[vid],
                        "price": None, "funding_bps_h": None,
                        "rate_kind": None}
                    continue
                if listed_set is not None and not is_listed:
                    venues_cell[vid] = {
                        "status": "not_listed",
                        "price": None, "funding_bps_h": None,
                        "rate_kind": None}
                    continue
                if cell is None or (cell.get("price") is None
                                    and finfo is None):
                    venues_cell[vid] = {
                        "status": "error",
                        "reason": ERR_NO_PRICE if is_listed else ERR_NO_DATA,
                        "price": None, "funding_bps_h": bps_h,
                        "rate_kind": finfo.rate_kind if finfo else None}
                    continue
                if cell.get("price") is None:
                    # 上市、有费率但取不到价：半残格，标异常且不进排行
                    venues_cell[vid] = {
                        "status": "error", "reason": ERR_NO_PRICE,
                        "price": None, "funding_bps_h": bps_h,
                        "rate_kind": finfo.rate_kind if finfo else None}
                    continue
                venues_cell[vid] = {
                    "status": "ok",
                    "price": cell.get("price"),
                    "funding_bps_h": bps_h,
                    "rate_kind": finfo.rate_kind if finfo else None,
                }
                # 排行只纳入 ok 的格：价差要两腿都有价、费差还要有费率
                prices[vid] = cell.get("price")
                if bps_h is not None and finfo is not None:
                    rates[vid] = (bps_h, finfo.rate_kind)
            spread = best_spread(prices)
            funding = best_funding_pair(rates)
            if spread:
                spread_ranking.append({"symbol": sym, **spread})
            if funding:
                funding_ranking.append({"symbol": sym, **funding})
            symbols_out.append({"symbol": sym, "venues": venues_cell,
                                "listed_venues": listed_venues,
                                "listed_count": len(listed_venues),
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
    def _ensure_listings(self) -> None:
        """K 线判定上市前先保证上市清单可用（过期就顺带扫一次）。"""
        now = time.time()
        if self._cache and now - self._cache[0] < self._cache_seconds \
                and self._listings:
            return
        try:
            self.scan()
        except Exception as e:  # noqa: BLE001 — 扫描失败则清单未知，保守放行
            _log(f"candles 前置扫描失败：{type(e).__name__}: {e}")

    def candles(self, venue_id: str, symbol: str) -> dict:
        """近 24 小时 1 小时 K 线的高低与建议区间（高低各扩 2%）。

        只有 HL 家族有已核实的官方公开 K 线接口（candleSnapshot）；
        其他场所如实返回不可用。先查上市清单：未上市直接回中文
        短句、不打上游（上游对未上市 coin 回 500，原文绝不外露）；
        子所 coin 名带 dex 前缀（xyz:SNDK / io:AAPL，活接口实测）。
        """
        symbol = str(symbol or "").upper()
        base = {"venue": venue_id, "symbol": symbol}
        if venue_id not in HL_DEX:
            return {**base, "ok": False,
                    "error": "该场所没有已核实的公开 K 线接口，"
                             "请把网格腿切到 Hyperliquid 家族"
                             "（hyperliquid / entropy / tradexyz）看区间"}
        self._ensure_listings()
        listed = self._listings.get(venue_id)
        if listed is not None and symbol not in listed:
            return {**base, "ok": False, "not_listed": True,
                    "error": ERR_NOT_LISTED}
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
        except Exception as e:  # noqa: BLE001 — 原文只进日志
            _log(f"candles {venue_id}/{symbol} 失败："
                 f"{type(e).__name__}: {e}")
            return {**base, "ok": False, "error": ERR_NO_CANDLES}
        highs = [h for h in (_f(row.get("h")) for row in rows
                             if isinstance(row, dict)) if h]
        lows = [lo for lo in (_f(row.get("l")) for row in rows
                              if isinstance(row, dict)) if lo]
        if not highs or not lows:
            return {**base, "ok": False, "error": ERR_NO_CANDLES}
        high, low = max(highs), min(lows)
        return {**base, "ok": True, "hours": 24, "candles": len(rows),
                "high": high, "low": low,
                "suggested_lower": round(low * 0.98, 6),
                "suggested_upper": round(high * 1.02, 6)}


def _side(side) -> object:
    return side.get("price") if isinstance(side, dict) else side
