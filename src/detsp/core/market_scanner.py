"""面板公开行情扫描（只读、不用任何密钥）。

两层数据：

1. **全量快照**（full_snapshot）：逐所一次批量拿到全部上市标的
   的价格 + 资金费（归一到每小时 bps，口径 rate_kind 如实标注）
   + 日波动（24h 涨跌幅绝对值 %）。数据源全是各所的批量公开
   接口：HL 家族 metaAndAssetCtxs 本就是全量（prevDayPx 算日
   波动）；Aster premiumIndex 全量 + ticker/24hr 给日涨跌幅；
   Arcus /v1/markets 自带 markPrice 与 priceChange24h；
   Lighter orderBooks/funding-rates 全量 + exchangeStats 批量给
   最近成交价与日涨跌幅（逐市场拉 BBO 要 200+ 次请求、实测
   一分钟以上，不可行；watchlist 内的币仍用 BBO 中间价覆盖，
   保持矩阵口径不变）。单所失败隔离，整表带缓存（默认 20 秒）。

2. **观察列表矩阵**（scan）：从全量快照派生 watchlist 逐格三态
   （ok / not_listed / error），语义与旧版一致——把「没上市」
   与「取数失败」分开（用户实测痛点：SNDK 在 Hyperliquid 本体
   根本未上市，却与故障一样显示「无数据」）。

pairs(venue_a, venue_b, strategy)：两所**共同上市**的全部标的，
带双边价格/资金费、价差、费差与预计年化，按策略口径排序；
best(strategy)：跨全部场所对聚合每策略当前最佳标的（总览
卡片用）。K 线（candles）先查上市清单，未上市直接回中文
短句、不打上游（上游对未上市 coin 回 500，英文原文绝不外露）。
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
VENUE_ORDER = ("arcus", "lighter", "lighter_rh", "hyperliquid",
               "entropy", "tradexyz", "aster")
ALL_VENUES = frozenset(VENUE_ORDER)
_LIGHTER_FUNDING_INTERVAL = 28800.0   # Lighter 费率周期 8 小时
_ASTER_FUNDING_INTERVAL = 28800.0     # Aster 结算周期 8 小时

# 面向用户的中文短句：上游异常原文（英文/堆栈）只进内部，绝不外露
ERR_VENUE_DOWN = "行情接口暂时异常"
ERR_NO_PRICE = "价格暂时取不到"
ERR_NO_DATA = "数据暂时取不到"
ERR_NO_CANDLES = "K线数据暂时取不到，请稍后重试"
ERR_NOT_LISTED = "该平台未上市此标的"

# 各所全量价格的口径（pairs/best 输出给前端标注用）
PRICE_KINDS = {
    "hyperliquid": "标记价", "entropy": "标记价", "tradexyz": "标记价",
    "aster": "标记价", "arcus": "标记价",
    "lighter": "最近成交价", "lighter_rh": "最近成交价",
}


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
    """公开行情扫描器：全量快照 + watchlist 矩阵 + pairs/best。"""

    def __init__(self, http=None, cache_seconds: float = 20.0,
                 timeout: float = 6.0, watchlist: list[str] | None = None,
                 quotes=None) -> None:
        self._http = http            # 测试可注入假 httpx.Client
        self._cache_seconds = max(10.0, float(cache_seconds))
        self._timeout = timeout
        self.watchlist = [str(s).upper() for s in
                          (watchlist or DEFAULT_WATCHLIST)]
        self._quotes = quotes        # PublicQuoteProvider（lighter 取价）
        self._snap: dict | None = None
        self._scan_cache: tuple[float, dict] | None = None
        self._lighter_maps: dict[str, dict[str, int]] = {}
        # 逐所上市清单 {venue_id: set(symbol)}，随快照一并刷新；
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

    # ----- 全量抓取：每所返回 {symbol: cell}，cell = -----
    # ----- {"price":…, "funding": FundingInfo|None, "vol":…} -----
    def _full_hl_family(self, errors: dict[str, str],
                        listings: dict[str, set[str]]) -> dict[str, dict]:
        out: dict[str, dict] = {v: {} for v in HL_DEX}
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
            listings[vid] = {
                _plain(str(a.get("name") or "")).upper()
                for a in meta.get("universe", [])
                if isinstance(a, dict) and not a.get("isDelisted")}
            listings[vid].discard("")
            for a, ctx in zip(meta.get("universe", []), ctxs or []):
                if not isinstance(a, dict) or a.get("isDelisted"):
                    continue
                sym = _plain(str(a.get("name") or "")).upper()
                if not sym:
                    continue
                ctx = ctx or {}
                price = _f(ctx.get("markPx"))
                prev = _f(ctx.get("prevDayPx"))
                vol = (abs(price - prev) / prev * 100
                       if price and prev else None)
                rate = _f(ctx.get("funding"))
                funding = FundingInfo(
                    symbol=sym, rate=rate, interval_seconds=3600,
                    rate_kind="current") if rate is not None else None
                out[vid][sym] = {"price": price, "funding": funding,
                                 "vol": vol}
        # 下期预测：能对上的条目把口径升级为 predicted（与 HL 适配
        # funding() 的主口径一致）；接口失败不影响当期口径结果。
        # 注意：predictedFundings 实测不含 io/xyz 子所条目，子所
        # 因此保持 current 口径（与 venues 适配的实测结论一致）。
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
                if vid is None:
                    continue
                for vname, payload in venues or []:
                    # 活接口场所名实测为 "HlPerp"（与 hl.py 同一坑）
                    if str(vname).lower() not in ("hyperliquid", "hlperp"):
                        continue
                    rate = _f((payload or {}).get("fundingRate"))
                    if rate is None:
                        continue
                    cell = out.setdefault(vid, {}).setdefault(
                        sym, {"price": None, "funding": None, "vol": None})
                    nts = _f((payload or {}).get("nextFundingTime")) or 0
                    cell["funding"] = FundingInfo(
                        symbol=sym, rate=rate,
                        next_ts=nts / 1000 if nts > 1e10 else nts,
                        interval_seconds=3600, rate_kind="predicted")
        except Exception:  # noqa: BLE001 — 预测升级失败保留当期口径
            pass
        return out

    def _full_aster(self, listings: dict[str, set[str]]) -> dict[str, dict]:
        r = self._client().get(f"{ASTER_BASE}/fapi/v1/premiumIndex")
        r.raise_for_status()
        out: dict[str, dict] = {}
        for row in r.json() or []:
            if not isinstance(row, dict):
                continue
            raw = str(row.get("symbol") or "")
            sym = raw[:-4].upper() if raw.upper().endswith("USDT") else ""
            if not sym:
                continue
            rate = _f(row.get("lastFundingRate"))
            out[sym] = {
                "price": _f(row.get("markPrice")),
                "funding": FundingInfo(
                    symbol=sym, rate=rate,
                    interval_seconds=_ASTER_FUNDING_INTERVAL,
                    rate_kind="last_settled") if rate is not None else None,
                "vol": None,
            }
        # 日涨跌幅走 ticker/24hr（单独失败只丢波动，不拖垮主数据）
        try:
            rt = self._client().get(f"{ASTER_BASE}/fapi/v1/ticker/24hr")
            rt.raise_for_status()
            for row in rt.json() or []:
                if not isinstance(row, dict):
                    continue
                raw = str(row.get("symbol") or "")
                sym = raw[:-4].upper() if raw.upper().endswith("USDT") else ""
                chg = _f(row.get("priceChangePercent"))
                if sym in out and chg is not None:
                    out[sym]["vol"] = abs(chg)
        except Exception as e:  # noqa: BLE001 — 波动是排序辅助，可缺
            _log(f"aster ticker/24hr 失败：{type(e).__name__}: {e}")
        # 上市清单走 exchangeInfo（status=TRADING 的 USDT 永续）；
        # 该接口单独失败时退回「premiumIndex 有条目即上市」
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

    def _full_lighter(self, vid: str,
                      listings: dict[str, set[str]]) -> dict[str, dict]:
        base = LIGHTER_BASES[vid]
        if vid not in self._lighter_maps:
            r = self._client().get(f"{base}/orderBooks")
            r.raise_for_status()
            self._lighter_maps[vid] = {
                str(b.get("symbol", "")).upper(): int(b["market_id"])
                for b in (r.json() or {}).get("order_books", [])
                if b.get("status") == "active"
                and b.get("market_type") in (None, "perp")
                and b.get("market_id") is not None}
        # 上市清单 = orderBooks 里 active 的永续市场全集
        listings[vid] = set(self._lighter_maps[vid])
        id2sym = {i: s for s, i in self._lighter_maps[vid].items()}
        # 全量价格与日涨跌幅：exchangeStats 一次批量给最近成交价
        # （逐市场 BBO 要 200+ 次请求、实测一分钟以上，不可行）；
        # 该接口失败只丢全量价格，watchlist 币仍有 BBO 兜底
        stats: dict[str, dict] = {}
        try:
            rs = self._client().get(f"{base}/exchangeStats")
            rs.raise_for_status()
            for row in (rs.json() or {}).get("order_book_stats") or []:
                if isinstance(row, dict) and row.get("symbol"):
                    stats[str(row["symbol"]).upper()] = row
        except Exception as e:  # noqa: BLE001 — 批量价是增强项，可缺
            _log(f"{vid} exchangeStats 失败：{type(e).__name__}: {e}")
        out: dict[str, dict] = {}
        for sym in self._lighter_maps[vid]:
            srow = stats.get(sym) or {}
            price = _f(srow.get("last_trade_price"))
            chg = _f(srow.get("daily_price_change"))
            out[sym] = {"price": price if price and price > 0 else None,
                        "funding": None,
                        "vol": abs(chg) if chg is not None else None}
        r = self._client().get(f"{base}/funding-rates")
        r.raise_for_status()
        for row in (r.json() or {}).get("funding_rates") or []:
            if str(row.get("exchange") or "").lower() != "lighter":
                continue
            try:
                sym = id2sym.get(int(row.get("market_id")))
            except (TypeError, ValueError):
                continue
            rate = _f(row.get("rate"))
            if sym is None or rate is None:
                continue
            out.setdefault(sym, {"price": None, "funding": None,
                                 "vol": None})
            out[sym]["funding"] = FundingInfo(
                symbol=sym, rate=rate,
                interval_seconds=_LIGHTER_FUNDING_INTERVAL,
                rate_kind="current")
        # watchlist 内的币用 BBO 中间价覆盖批量最近成交价，
        # 保持观察列表矩阵的价格口径与旧版一致
        for sym in self.watchlist:
            if sym in self._lighter_maps[vid]:
                price = self._quote_mid(vid, sym)
                if price is not None:
                    out[sym]["price"] = price
        return out

    def _full_arcus(self, listings: dict[str, set[str]]) -> dict[str, dict]:
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
            if not sym:
                continue
            rate = _f(raw.get("nextFundingRate"))
            kind = "predicted"
            if rate is None:
                rate = _f(raw.get("fundingRate"))
                kind = "current"
            chg = _f(raw.get("priceChange24h"))
            cell = {"price": _f(raw.get("markPrice")), "funding": None,
                    # priceChange24h 是小数口径（0.0123 = 1.23%）
                    "vol": abs(chg) * 100 if chg is not None else None,
                    "_arcus_symbol": str(raw.get("marketDisplayName") or "")}
            if rate is not None:
                cell["funding"] = FundingInfo(
                    symbol=sym, rate=rate, interval_seconds=3600,
                    rate_kind=kind)
            out[sym] = cell
        # watchlist 内的币仍走公开 BBO 中间价（与旧版矩阵口径
        # 一致）；BBO 失败时保留 markets 的 markPrice 兜底
        for sym, cell in out.items():
            arcus_symbol = cell.pop("_arcus_symbol", "")
            if sym not in self.watchlist or not arcus_symbol:
                continue
            try:
                rb = self._client().get(f"{ARCUS_BASE}/v1/bbo/{arcus_symbol}")
                rb.raise_for_status()
                body = rb.json() or {}
                bid = _f(_side(body.get("bestBid")))
                ask = _f(_side(body.get("bestAsk")))
                if bid and ask:
                    cell["price"] = (bid + ask) / 2
                elif bid or ask:
                    cell["price"] = bid or ask
            except Exception:  # noqa: BLE001 — BBO 失败保留 markPrice
                pass
        return out

    # ----- 全量快照（带缓存） -----
    def full_snapshot(self, force: bool = False) -> dict:
        now = time.time()
        if not force and self._snap and \
                now - self._snap["ts"] < self._cache_seconds:
            return self._snap
        data: dict[str, dict[str, dict]] = {}
        errors: dict[str, str] = {}
        listings: dict[str, set[str]] = {}

        def _grab(vid: str, fn) -> None:
            try:
                data[vid] = fn()
            except Exception as e:  # noqa: BLE001 — 单所失败隔离
                data[vid] = {}
                errors[vid] = ERR_VENUE_DOWN
                _log(f"{vid} 抓取失败：{type(e).__name__}: {e}")

        def _hl_job() -> None:
            hl_errors: dict[str, str] = {}
            hl = self._full_hl_family(hl_errors, listings)
            for vid in HL_DEX:
                data[vid] = hl.get(vid, {})
            errors.update(hl_errors)

        # 五组抓取互相独立（各写各的 venue 键），并发跑缩短首屏
        # 等待；httpx.Client 本身线程安全
        from concurrent.futures import ThreadPoolExecutor

        jobs = [_hl_job,
                lambda: _grab("aster", lambda: self._full_aster(listings)),
                lambda: _grab("arcus", lambda: self._full_arcus(listings)),
                lambda: _grab("lighter",
                              lambda: self._full_lighter("lighter",
                                                          listings)),
                lambda: _grab("lighter_rh",
                              lambda: self._full_lighter("lighter_rh",
                                                          listings))]
        with ThreadPoolExecutor(max_workers=5) as ex:
            list(ex.map(lambda fn: fn(), jobs))
        self._listings = listings
        self._snap = {"ts": now, "data": data, "listings": listings,
                      "errors": errors}
        self._scan_cache = None
        return self._snap

    # ----- 观察列表矩阵（从全量快照派生，语义与旧版一致） -----
    def scan(self, force: bool = False) -> dict:
        snap = self.full_snapshot(force=force)
        if self._scan_cache and self._scan_cache[0] == snap["ts"]:
            return self._scan_cache[1]
        per_venue, errors, listings = \
            snap["data"], snap["errors"], snap["listings"]
        symbols_out = []
        spread_ranking, funding_ranking = [], []
        for sym in self.watchlist:
            venues_cell: dict[str, dict] = {}
            prices: dict[str, float | None] = {}
            rates: dict[str, tuple[float, str]] = {}
            listed_venues: list[str] = []
            for vid in VENUE_ORDER:
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
            "updated_at": snap["ts"],
            "watchlist": list(self.watchlist),
            "symbols": symbols_out,
            "spread_ranking": spread_ranking,
            "funding_ranking": funding_ranking,
            "venue_errors": errors,
        }
        self._scan_cache = (snap["ts"], result)
        return result

    # ----- 两所共同标的（pairs）与跨所最佳（best） -----
    @staticmethod
    def _cell_bps(cell: dict | None) -> float | None:
        finfo = cell.get("funding") if cell else None
        if finfo is None:
            return None
        return round(_bps_per_hour(finfo.rate, finfo.interval_seconds), 4)

    def _pair_row(self, sym: str, va: str, vb: str,
                  ca: dict | None, cb: dict | None) -> dict:
        pa = ca.get("price") if ca else None
        pb = cb.get("price") if cb else None
        signed = spread = None
        if pa and pb:
            mid = (pa + pb) / 2
            signed = (pa - pb) / mid * 10_000
            spread = abs(signed)
        ra, rb = self._cell_bps(ca), self._cell_bps(cb)
        ka = ca["funding"].rate_kind if ca and ca.get("funding") else None
        kb = cb["funding"].rate_kind if cb and cb.get("funding") else None
        edge = apr = None
        long_v = short_v = None
        mixed = False
        if ra is not None and rb is not None:
            edge = abs(rb - ra)
            apr = edge * 87.6          # bps/小时 → 预计年化 %（×24×365）
            long_v, short_v = (va, vb) if ra <= rb else (vb, va)
            mixed = ka != kb
        vol, vol_from = None, None
        for src, cell in (("a", ca), ("b", cb)):
            if cell and cell.get("vol") is not None:
                vol, vol_from = cell["vol"], src
                break
        # 疑似坏数据：同名标的在两所价差超过 50%，几乎必然是
        # 不同资产同名（如股票代码与同名代币）或一边市场已死，
        # 不是可套利的价差。标 suspect，排序时沉底、best 里剔除
        suspect = spread is not None and spread > 5000
        return {"symbol": sym,
                "price_a": pa, "price_b": pb,
                "rate_a_bps_h": ra, "rate_b_bps_h": rb,
                "rate_kind_a": ka, "rate_kind_b": kb,
                "spread_bps": spread, "signed_spread_bps": signed,
                "edge_bps_h": edge, "apr_pct": apr,
                "mixed": mixed, "suspect": suspect,
                "long_venue": long_v, "short_venue": short_v,
                "volatility_pct": vol, "volatility_from": vol_from,
                "has_price": bool(pa and pb),
                "has_funding": ra is not None and rb is not None}

    def _pair_rows(self, snap: dict, va: str, vb: str) -> list[dict]:
        la = snap["listings"].get(va)
        lb = snap["listings"].get(vb)
        if la is None or lb is None:
            return []
        da, db = snap["data"].get(va) or {}, snap["data"].get(vb) or {}
        return [self._pair_row(sym, va, vb, da.get(sym), db.get(sym))
                for sym in sorted(la & lb)]

    @staticmethod
    def _sort_strategy(rows: list[dict], strategy: str) -> list[dict]:
        """按策略口径过滤 + 排序（pairs 与 best 共用）。"""
        if strategy in ("funding_carry", "rotation_hedge"):
            rows = [r for r in rows if r["has_price"] and r["has_funding"]]
            # 疑似坏数据沉底；同口径优先（混口径靠后），组内按费差降序
            rows.sort(key=lambda r: (r["suspect"], r["mixed"],
                                     -r["edge_bps_h"]))
        elif strategy == "premium_arb":
            rows = [r for r in rows if r["has_price"]]
            rows.sort(key=lambda r: (r["suspect"], -r["spread_bps"]))
        elif strategy == "hedged_grid":
            rows = [r for r in rows if r["price_a"]]
            # 日波动降序，无波动数据的排末尾
            rows.sort(key=lambda r: (r["volatility_pct"] is None,
                                     -(r["volatility_pct"] or 0.0)))
        return rows

    def pairs(self, venue_a: str, venue_b: str, strategy: str) -> dict:
        """两所共同上市的全部标的，按策略口径排序。"""
        snap = self.full_snapshot()
        base = {"venue_a": venue_a, "venue_b": venue_b,
                "strategy": strategy, "updated_at": snap["ts"],
                "listed_a": 0, "listed_b": 0, "common": 0,
                "price_kinds": {venue_a: PRICE_KINDS.get(venue_a, ""),
                                venue_b: PRICE_KINDS.get(venue_b, "")},
                "errors": {}, "rows": []}
        for v in (venue_a, venue_b):
            if v not in ALL_VENUES:
                return {**base, "ok": False,
                        "error": f"未知场所：{v}"}
            if v in snap["errors"]:
                base["errors"][v] = snap["errors"][v]
        la = snap["listings"].get(venue_a)
        lb = snap["listings"].get(venue_b)
        base["listed_a"] = len(la) if la is not None else 0
        base["listed_b"] = len(lb) if lb is not None else 0
        if la is None or lb is None:
            return base          # 某所故障/清单未知：rows 空、errors 说明
        base["common"] = len(la & lb)
        base["rows"] = self._sort_strategy(
            self._pair_rows(snap, venue_a, venue_b), strategy)
        return base

    def best(self, strategy: str, limit: int = 5) -> dict:
        """跨全部场所对聚合：每策略当前最佳标的（总览卡片用）。"""
        snap = self.full_snapshot()
        out = {"strategy": strategy, "updated_at": snap["ts"], "rows": []}
        if strategy == "hedged_grid":
            best_by_sym: dict[str, dict] = {}
            for vid in VENUE_ORDER:
                if vid in snap["errors"]:
                    continue
                for sym, cell in (snap["data"].get(vid) or {}).items():
                    if not cell.get("price") or cell.get("vol") is None:
                        continue
                    cur = best_by_sym.get(sym)
                    if cur is None or cell["vol"] > cur["volatility_pct"]:
                        best_by_sym[sym] = {
                            "symbol": sym, "venue": vid,
                            "venue_a": vid, "venue_b": None,
                            "price": cell["price"],
                            "volatility_pct": cell["vol"]}
            rows = sorted(best_by_sym.values(),
                          key=lambda r: -r["volatility_pct"])
            # 对冲腿候选：另一所有价的场所（前端预填用）
            for row in rows[:limit]:
                for vid in VENUE_ORDER:
                    if vid == row["venue"]:
                        continue
                    cell = (snap["data"].get(vid) or {}).get(row["symbol"])
                    if cell and cell.get("price"):
                        row["venue_b"] = vid
                        break
            out["rows"] = rows[:limit]
            return out
        metric = ("edge_bps_h" if strategy in ("funding_carry",
                                               "rotation_hedge")
                  else "spread_bps")
        best_by_sym = {}
        venues = [v for v in VENUE_ORDER if v not in snap["errors"]]
        for i, va in enumerate(venues):
            for vb in venues[i + 1:]:
                for row in self._sort_strategy(
                        self._pair_rows(snap, va, vb), strategy):
                    if row["suspect"]:
                        continue   # 疑似同名不同资产/坏数据，不进最佳卡片
                    cur = best_by_sym.get(row["symbol"])
                    if cur is None:
                        best_by_sym[row["symbol"]] = {**row, "venue_a": va,
                                                      "venue_b": vb}
                        continue
                    if strategy == "premium_arb":
                        better = row[metric] > cur[metric]
                    else:
                        # 资金费类：同口径优先，其次费差大
                        better = (not row["mixed"], row[metric]) > \
                            (not cur["mixed"], cur[metric])
                    if better:
                        best_by_sym[row["symbol"]] = {**row, "venue_a": va,
                                                      "venue_b": vb}
        rows = list(best_by_sym.values())
        if strategy == "premium_arb":
            rows.sort(key=lambda r: -r["spread_bps"])
        else:
            rows.sort(key=lambda r: (r["mixed"], -r["edge_bps_h"]))
        out["rows"] = rows[:limit]
        return out

    # ----- 24 小时高低（中性网格定区间用）-----
    def _ensure_listings(self) -> None:
        """K 线判定上市前先保证上市清单可用（过期就顺带扫一次）。"""
        now = time.time()
        if self._snap and now - self._snap["ts"] < self._cache_seconds \
                and self._listings:
            return
        try:
            self.full_snapshot()
        except Exception as e:  # noqa: BLE001 — 扫描失败则清单未知，保守放行
            _log(f"candles 前置扫描失败：{type(e).__name__}: {e}")

    def candles(self, venue_id: str, symbol: str) -> dict:
        """近 24 小时 1 小时 K 线的高低与建议区间（高低各扩 2%）。

        只有 HL 家族有已核实的官方公开 K 线接口（candleSnapshot）；
        其他场所如实返回不可用。先查上市清单：未上市直接回中文
        短句、不打上游（上游对未上市 coin 会回 500，原文绝不外露）；
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
