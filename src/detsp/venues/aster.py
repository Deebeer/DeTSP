"""Aster 适配（实盘）：本项目依据 Aster 官方文档自写。

未使用 perp-dex-tools 的任何代码（其为非商业许可）。

依据：Aster 官方 API 文档（asterdex/api-docs 与 docs.futures 的
Futures API (Legacy) V1 文档，2026-11 查阅）：
- 基础地址 https://fapi.asterdex.com，路径前缀 /fapi/v1（永续）
- 签名：HMAC-SHA256，secretKey 作密钥、对 totalParams（query string 与
  request body 拼接）签名，signature 放在参数末尾；请求头
  X-MBX-APIKEY 带 API key；签名接口必带 timestamp（毫秒），可带 recvWindow
- 端点：GET /fapi/v1/exchangeInfo、/fapi/v1/depth、
  /fapi/v1/premiumIndex（标记价与 lastFundingRate）、
  /fapi/v1/ticker/bookTicker、POST /fapi/v1/order、
  DELETE /fapi/v1/order、DELETE /fapi/v1/allOpenOrders、
  GET /fapi/v1/openOrders、GET /fapi/v2/balance、
  GET /fapi/v2/positionRisk

注意（官方现状，写在代码里提醒后来者）：Aster 官方现主推 V3（Pro API，
EIP-712 签名），V1 为 Legacy。本文件按既定方案实现 V1 HMAC；
据官方文档仓的变更说明，V1 密钥的可用性以 Aster 平台当前政策为准，
若平台停发/停用 V1 密钥，需在下一批迁移 V3。

符号约定：接口内部用基础符号（BTC），Aster 用交易对名（BTCUSDT）；
本适配默认拼 USDT 后缀，已带报价后缀的原样使用。持仓返回时剥掉后缀。
资金费：premiumIndex 只给「上一期」lastFundingRate 及 nextFundingTime，
无当期预测值，funding() 返回的就是它（注释口径）。
"""

from __future__ import annotations

import hashlib
import hmac as hmac_mod
import math
import time
from typing import Callable
from urllib.parse import urlencode

import httpx

from ..core.execution import unknown_outcome_guard
from ..core.venue import (
    Book,
    FundingInfo,
    Market,
    Order,
    OrderRequest,
    Position,
    Side,
)
from ._skeleton import SkeletonVenue

BASE_URL = "https://fapi.asterdex.com"
QUOTE_SUFFIXES = ("USDT", "USD1", "USDC")
STATUS_MAP = {
    "NEW": "open", "PARTIALLY_FILLED": "open", "FILLED": "filled",
    "CANCELED": "canceled", "EXPIRED": "canceled", "REJECTED": "rejected",
}


def sign_params(secret_key: str, params: dict) -> str:
    """HMAC-SHA256 签名：对 urlencode 后的参数串签名，返回带 signature
    的完整参数串（signature 在末尾）。纯函数，可单测。"""
    total = urlencode(params)
    sig = hmac_mod.new(secret_key.encode(), total.encode(),
                       hashlib.sha256).hexdigest()
    return f"{total}&signature={sig}"


def to_pair(symbol: str) -> str:
    """BTC → BTCUSDT；已带报价后缀的原样返回。"""
    s = symbol.upper()
    if any(s.endswith(q) and len(s) > len(q) for q in QUOTE_SUFFIXES):
        return s
    return s + "USDT"


def to_base(pair: str) -> str:
    s = pair.upper()
    for q in QUOTE_SUFFIXES:
        if s.endswith(q) and len(s) > len(q):
            return s[: -len(q)]
    return s


class AsterVenue(SkeletonVenue):
    venue_id = "aster"
    source = "依据 Aster 官方 API 文档（V1 Legacy，HMAC-SHA256）自写"

    def __init__(self, secrets: dict[str, str] | None = None,
                 api_key: str | None = None,
                 secret_key: str | None = None,
                 base_url: str = BASE_URL,
                 recv_window: int = 5000,
                 http: httpx.Client | None = None,
                 transport: Callable | None = None, **kw) -> None:
        super().__init__(**kw)
        secrets = secrets or {}
        self._api_key = api_key or secrets.get("ASTER_API_KEY") or ""
        self._secret_key = secret_key or secrets.get("ASTER_SECRET_KEY") or ""
        self._base_url = base_url.rstrip("/")
        self._recv_window = recv_window
        self._http = http
        # 可注入（测试用）：(method, path, query_string, headers) -> (status, json)
        self._transport = transport
        self._oid_symbol: dict[str, str] = {}  # order_id → 基础符号（查单/撤单用）
        self._markets_cache: list[Market] | None = None

    # ── 请求层 ──────────────────────────────────────────────
    def _get_http(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(base_url=self._base_url, timeout=15.0)
        return self._http

    def _request(self, method: str, path: str, params: dict | None = None,
                 signed: bool = False):
        params = dict(params or {})
        headers: dict[str, str] = {}
        if signed:
            if not self._api_key or not self._secret_key:
                raise RuntimeError("Aster 实盘缺少 ASTER_API_KEY / ASTER_SECRET_KEY")
            params.setdefault("recvWindow", self._recv_window)
            params["timestamp"] = int(time.time() * 1000)
            query = sign_params(self._secret_key, params)
            headers["X-MBX-APIKEY"] = self._api_key
        else:
            query = urlencode(params)
        if self._transport is not None:
            status, data = self._transport(method, path, query, headers)
        else:
            url = path + (f"?{query}" if query else "")
            r = self._get_http().request(method, url, headers=headers)
            status = r.status_code
            try:
                data = r.json()
            except ValueError:
                data = {"msg": r.text[:300]}
        if status >= 400:
            msg = data.get("msg") if isinstance(data, dict) else data
            raise RuntimeError(f"Aster {method} {path} HTTP {status}：{msg}")
        return data

    # ── 只读接口 ────────────────────────────────────────────
    def markets(self) -> list[Market]:
        if self._paper is not None:
            return super().markets()
        if self._markets_cache is not None:
            return self._markets_cache
        data = self._request("GET", "/fapi/v1/exchangeInfo")
        out: list[Market] = []
        for s in data.get("symbols") or []:
            if s.get("contractType") != "PERPETUAL" or s.get("status") != "TRADING":
                continue
            filters = {f["filterType"]: f for f in s.get("filters") or []}
            price_f = filters.get("PRICE_FILTER") or {}
            lot = filters.get("LOT_SIZE") or {}
            notional_f = filters.get("MIN_NOTIONAL") or {}
            out.append(Market(
                symbol=to_base(s["symbol"]),
                price_step=float(price_f.get("tickSize") or 0),
                qty_step=float(lot.get("stepSize") or 0),
                min_qty=float(lot.get("minQty") or 0),
                min_notional=float(notional_f.get("notional") or 0),
                max_leverage=0.0))  # 未核实：exchangeInfo 不含最大杠杆；
            # 官方 leverageBracket 需签名且口径未在本批核对，先记 0=未知
        self._markets_cache = out
        return out

    def book(self, symbol: str) -> Book:
        if self._paper is not None:
            return super().book(symbol)
        data = self._request("GET", "/fapi/v1/ticker/bookTicker",
                             {"symbol": to_pair(symbol)})
        return Book(symbol=symbol, bid=float(data["bidPrice"]),
                    ask=float(data["askPrice"]), ts=time.time())

    def funding(self, symbol: str) -> FundingInfo:
        if self._paper is not None:
            return super().funding(symbol)
        data = self._request("GET", "/fapi/v1/premiumIndex",
                             {"symbol": to_pair(symbol)})
        # lastFundingRate 是上一期已结算费率（官方无当期预测字段）；
        # Aster 结算周期 8 小时（H3 口径标注）
        return FundingInfo(symbol=symbol,
                           rate=float(data.get("lastFundingRate") or 0),
                           next_ts=float(data.get("nextFundingTime") or 0) / 1000,
                           interval_seconds=28800)

    def balance(self) -> dict:
        if self._paper is not None:
            return super().balance()
        rows = self._request("GET", "/fapi/v2/balance", signed=True)
        equity = available = 0.0
        for row in rows or []:
            # 多资产模式下可能多行：汇总全部资产的余额口径（以 USDT 行为主时即其本身）
            equity += float(row.get("balance") or 0)
            available += float(row.get("availableBalance") or 0)
        return {"equity": equity, "available": available}

    def positions(self) -> list[Position]:
        if self._paper is not None:
            return super().positions()
        rows = self._request("GET", "/fapi/v2/positionRisk", signed=True)
        out: list[Position] = []
        for p in rows or []:
            qty = float(p.get("positionAmt") or 0)
            if qty == 0:
                continue
            liq = float(p.get("liquidationPrice") or 0)
            out.append(Position(
                symbol=to_base(p["symbol"]), qty=qty,
                entry_price=float(p.get("entryPrice") or 0),
                leverage=float(p.get("leverage") or 1),
                liq_price=liq or None))
        return out

    def open_orders(self, symbol: str | None = None) -> list[Order]:
        if self._paper is not None:
            return super().open_orders(symbol)
        params = {"symbol": to_pair(symbol)} if symbol else {}
        rows = self._request("GET", "/fapi/v1/openOrders", params, signed=True)
        out = [self._to_order(o) for o in rows or []]
        for o in out:
            self._oid_symbol[o.order_id] = o.symbol
        return out

    @staticmethod
    def _to_order(o: dict) -> Order:
        orig = float(o.get("origQty") or 0)
        executed = float(o.get("executedQty") or 0)
        price = float(o.get("price") or 0)
        return Order(
            order_id=str(o.get("orderId")), symbol=to_base(o.get("symbol") or ""),
            side=Side.BUY if o.get("side") == "BUY" else Side.SELL,
            qty=orig, price=price or None, filled_qty=executed,
            status=STATUS_MAP.get(str(o.get("status") or ""), "open"))

    # ── 交易接口 ────────────────────────────────────────────
    def _market_for(self, symbol: str) -> Market | None:
        for m in self.markets():
            if m.symbol == symbol:
                return m
        return None

    def place_order(self, req: OrderRequest) -> Order:
        if self._paper is not None:
            return super().place_order(req)
        # H5：按市场精度对齐后再发单（数量向下、价格按方向就近 tick）
        mkt = self._market_for(req.symbol)
        qty = req.qty
        price = req.price
        if mkt is not None:
            if mkt.qty_step > 0:
                qty = _floor_step(qty, mkt.qty_step)
            if price is not None and mkt.price_step > 0:
                price = _align_price(price, mkt.price_step, req.side)
        if qty <= 0:
            raise ValueError(f"aster 数量 {req.qty} 对齐精度后为 0")
        params: dict = {
            "symbol": to_pair(req.symbol),
            "side": "BUY" if req.side is Side.BUY else "SELL",
            "quantity": _fmt_qty(qty),
        }
        if price is None:
            params["type"] = "MARKET"
        else:
            params["type"] = "LIMIT"
            params["price"] = _fmt_qty(price)
            # 接口层映射：post_only → GTX（只做 maker）；其余限价 → GTC 长挂
            params["timeInForce"] = "GTX" if req.post_only else "GTC"
        if req.reduce_only:
            params["reduceOnly"] = "true"
        # S1：httpx 超时/断连统一翻译为 UnknownOutcomeError
        with unknown_outcome_guard():
            data = self._request("POST", "/fapi/v1/order", params, signed=True)
        order = self._to_order(data)
        if order.order_id:
            self._oid_symbol[order.order_id] = req.symbol
        return order

    def poll_order(self, order_id: str) -> Order | None:
        """S2：GET /fapi/v1/order 查真实终态（需 symbol：用下单/挂单时
        记录的映射，映射缺失时退回挂单列表口径）。"""
        if self._paper is not None:
            return self._delegate().poll_order(order_id)
        symbol = self._oid_symbol.get(str(order_id))
        if symbol is None:
            for o in self.open_orders():
                if o.order_id == str(order_id):
                    return o
            return None
        try:
            data = self._request("GET", "/fapi/v1/order",
                                 {"symbol": to_pair(symbol),
                                  "orderId": int(order_id)}, signed=True)
        except RuntimeError:
            return None
        return self._to_order(data)

    def cancel_order(self, order_id: str) -> bool:
        if self._paper is not None:
            return super().cancel_order(order_id)
        # DELETE /fapi/v1/order 必须带 symbol：优先用记录的映射，
        # 没有再从挂单列表反查
        symbol = self._oid_symbol.get(str(order_id))
        if symbol is None:
            for o in self.open_orders():
                if o.order_id == str(order_id):
                    symbol = o.symbol
                    break
        if symbol is None:
            return False
        try:
            with unknown_outcome_guard():
                self._request("DELETE", "/fapi/v1/order",
                              {"symbol": to_pair(symbol),
                               "orderId": int(order_id)}, signed=True)
            return True
        except RuntimeError:
            return False

    def cancel_all(self, symbol: str) -> int:
        if self._paper is not None:
            return super().cancel_all(symbol)
        before = len(self.open_orders(symbol))
        self._request("DELETE", "/fapi/v1/allOpenOrders",
                      {"symbol": to_pair(symbol)}, signed=True)
        return before


def _fmt_qty(v: float) -> str:
    """数量/价格格式化：去掉浮点尾巴，避免 0.30000000000000004 这种串。"""
    return f"{v:.10f}".rstrip("0").rstrip(".") or "0"


def _floor_step(v: float, step: float) -> float:
    """数量向下对齐到 step 的整数倍（H5）。"""
    return math.floor(v / step + 1e-9) * step


def _align_price(price: float, step: float, side: Side) -> float:
    """价格按方向就近到 tick（H5）：买单向下、卖单向上，
    保证对齐后不比原价更激进（不会意外穿价）。"""
    units = price / step
    if side is Side.BUY:
        return math.floor(units + 1e-9) * step
    return math.ceil(units - 1e-9) * step
