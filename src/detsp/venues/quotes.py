"""公开行情只读价格源（paper 模式的价格驱动，H6）。

paper 模拟场所的价格优先来自各交易所的**公开只读接口**（不需要密钥）：
- HL 家族（Hyperliquid 本体 / Entropy" io" / trade.xyz" xyz"）：
  POST api.hyperliquid.xyz/info {"type":"allMids","dex":...}；
- Aster：GET /fapi/v1/ticker/bookTicker?symbol=BTCUSDT；
- Lighter：orderBooks（symbol→market_id）+ orderBookOrders 取买卖一。

Arcus 公开行情端点名未在本项目核实，返回 None 走随机游走兜底，
代码与文档均如实标注。任何失败（断网、限流、symbol 不存在）都返回
None，由 PaperVenue 回退到内置随机游走，绝不因行情失败让 paper 崩。
本模块只读公开数据，不签名、不下单。
"""

from __future__ import annotations

import time

HL_INFO_URL = "https://api.hyperliquid.xyz/info"
ASTER_BASE = "https://fapi.asterdex.com"
LIGHTER_BASE = "https://mainnet.zklighter.elliot.ai/api/v1"


class PublicQuoteProvider:
    """按 (venue_id, symbol) 取公开中间价，带短缓存（默认 5 秒）。"""

    def __init__(self, cache_seconds: float = 5.0, timeout: float = 4.0,
                 http=None) -> None:
        self._cache_seconds = cache_seconds
        self._timeout = timeout
        self._http = http  # 测试可注入假 httpx.Client
        self._cache: dict[tuple[str, str], tuple[float, float]] = {}
        # market_id 映射必须按部署分开缓存：主网与 Robinhood 链的
        # 同一 symbol 的 market_id 不同（如 SOL 主网 2 / RH 3），
        # 共用一份会串到别的币的价格（实测 RH SOL 取到 HYPE 的价）
        self._lighter_markets: dict[str, dict[str, int]] = {}

    def mid(self, venue_id: str, symbol: str) -> float | None:
        key = (venue_id, symbol.upper())
        now = time.time()
        hit = self._cache.get(key)
        if hit and now - hit[1] < self._cache_seconds:
            return hit[0]
        try:
            price = self._fetch(venue_id, symbol.upper())
        except Exception:  # noqa: BLE001 — 公共行情失败一律回退
            price = None
        if price is not None and price > 0:
            self._cache[key] = (price, now)
            return price
        return None

    # ----- 内部 -----
    def _client(self):
        if self._http is not None:
            return self._http
        import httpx

        self._http = httpx.Client(timeout=self._timeout)
        return self._http

    def _fetch(self, venue_id: str, symbol: str) -> float | None:
        if venue_id in ("hyperliquid", "entropy", "tradexyz"):
            dex = {"hyperliquid": "", "entropy": "io",
                   "tradexyz": "xyz"}[venue_id]
            r = self._client().post(HL_INFO_URL,
                                    json={"type": "allMids", "dex": dex})
            r.raise_for_status()
            mids = r.json() or {}
            raw = mids.get(f"{dex}:{symbol}" if dex else symbol) \
                or mids.get(symbol)
            return float(raw) if raw else None
        if venue_id == "aster":
            r = self._client().get(
                f"{ASTER_BASE}/fapi/v1/ticker/bookTicker",
                params={"symbol": f"{symbol}USDT"})
            r.raise_for_status()
            d = r.json() or {}
            bid, ask = float(d.get("bidPrice") or 0), float(d.get("askPrice") or 0)
            if bid > 0 and ask > 0:
                return (bid + ask) / 2
            return bid or ask or None
        if venue_id in ("lighter", "lighter_rh"):
            base = LIGHTER_BASE if venue_id == "lighter" else \
                "https://api.rh.lighter.xyz/api/v1"
            if venue_id not in self._lighter_markets:
                r = self._client().get(f"{base}/orderBooks")
                r.raise_for_status()
                self._lighter_markets[venue_id] = {
                    str(b.get("symbol", "")).upper(): int(b["market_id"])
                    for b in (r.json() or {}).get("order_books", [])
                    if b.get("status") == "active"}
            mid_id = self._lighter_markets[venue_id].get(symbol)
            if mid_id is None:
                return None
            r = self._client().get(f"{base}/orderBookOrders",
                                   params={"market_id": mid_id, "limit": 1})
            r.raise_for_status()
            d = r.json() or {}
            bids, asks = d.get("bids") or [], d.get("asks") or []
            if bids and asks:
                return (float(bids[0]["price"]) + float(asks[0]["price"])) / 2
            return None
        return None  # arcus 等未核实公开端点的场所：走随机游走兜底
