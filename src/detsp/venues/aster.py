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
import time
from typing import Callable
from urllib.parse import urlencode

import httpx

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
        # lastFundingRate 是上一期已结算费率（官方无当期预测字段）
        return FundingInfo(symbol=symbol,
                           rate=float(data.get("lastFundingRate") or 0),
                           next_ts=float(data.get("nextFundingTime") or 0) / 1000)

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
        return [self._to_order(o) for o in rows or []]

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
    def place_order(self, req: OrderRequest) -> Order:
        if self._paper is not None:
            return super().place_order(req)
        params: dict = {
            "symbol": to_pair(req.symbol),
            "side": "BUY" if req.side is Side.BUY else "SELL",
            "quantity": _fmt_qty(req.qty),
        }
        if req.price is None:
            params["type"] = "MARKET"
        else:
            params["type"] = "LIMIT"
            params["price"] = _fmt_qty(req.price)
            # 接口层映射：post_only → GTX（只做 maker）；其余限价 → GTC 长挂
            params["timeInForce"] = "GTX" if req.post_only else "GTC"
        if req.reduce_only:
            params["reduceOnly"] = "true"
        data = self._request("POST", "/fapi/v1/order", params, signed=True)
        return self._to_order(data)

    def cancel_order(self, order_id: str) -> bool:
        if self._paper is not None:
            return super().cancel_order(order_id)
        # DELETE /fapi/v1/order 必须带 symbol：先从挂单列表反查
        for o in self.open_orders():
            if o.order_id == str(order_id):
                try:
                    self._request("DELETE", "/fapi/v1/order",
                                  {"symbol": to_pair(o.symbol),
                                   "orderId": int(order_id)}, signed=True)
                    return True
                except RuntimeError:
                    return False
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
