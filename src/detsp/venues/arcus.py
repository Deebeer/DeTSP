"""Arcus 适配（实盘）。

移植自 Parallax Hedge（MIT）：签名/精度/强平价在 _arcus_sign.py，
本文件是 REST 客户端与下单执行：
- 只读：GET /v1/markets、/v1/bbo、/v1/l2OrderBook、/v1/account、
  /v1/positions、/v1/openOrders（只要公开地址，不签名）
- 写：POST /v1/placeOrder、/v1/cancelOrder、/v1/setLeverage，
  Ed25519 签名 + 请求头 X-API-Key / X-Timestamp / X-Signature，只发一次不重试
- 强平价交易所不返回，按 _arcus_sign 的公式用账户权益复算（全仓最坏口径）

下单约定：price=None → 以对手价加 1% 滑点保护的 IOC 限价单模拟市价；
post_only=True → ALO；其余限价 → GTT。
place_order 的 order_id 优先交易所 orderId，没有时为 "cid:<clientId>"；
IOC 的成交量以 positions() 对账为准（202 回执不带成交量）。
"""

from __future__ import annotations

import json
import time
from decimal import Decimal
from typing import Any, Callable

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
from . import _arcus_sign as ax
from ._skeleton import SkeletonVenue

DEFAULT_API_URL = "https://api.arcus.xyz"
MARKET_SLIPPAGE = 0.01  # 市价模拟的滑点保护 1%


def parse_market_fields(raw: dict[str, Any]) -> dict[str, Any] | None:
    """把 /v1/markets 一行整理成内部字段；不能交易的返回 None。

    保证金参数按当前时段取：休市时段（isOutsideRth）用更高的
    offHoursInitialMarginFraction，最大杠杆随之下降；维持保证金率不变。
    """
    if not isinstance(raw, dict):
        return None
    if str(raw.get("type") or "PERPETUAL").upper() != "PERPETUAL":
        return None
    if str(raw.get("status") or "").upper() != "ONLINE":
        return None
    try:
        market_id = int(raw.get("marketId"))
    except (TypeError, ValueError):
        return None
    name = str(raw.get("marketDisplayName") or "")
    base = str(raw.get("baseAsset") or "").upper()
    step = str(raw.get("stepSize") or "")
    tick = str(raw.get("tickSize") or "")
    if not name or not base or not step or not tick:
        return None
    try:
        decimals = ax.step_decimals(step)
    except ValueError:
        decimals = None
    if decimals is None:
        return None  # 步长不是 10 的整数次幂：精度口径装不下，宁可不列
    off_hours = bool(raw.get("isOutsideRth"))
    imf = _f(raw.get("offHoursInitialMarginFraction") if off_hours else None) \
        or _f(raw.get("initialMarginFraction"))
    return {
        "asset": base,
        "arcus_symbol": name,
        "arcus_market_id": market_id,
        "arcus_step_size": step,
        "arcus_tick_size": tick,
        "arcus_tick_tiers": [
            {"upToPrice": t.get("upToPrice"), "tick": t.get("tick")}
            for t in raw.get("tickTiers") or [] if isinstance(t, dict)
        ],
        "arcus_size_decimals": decimals,
        "arcus_min_order_size": _f(raw.get("minOrderSize")) or 0.0,
        "arcus_min_notional": _f(raw.get("minOrderNotional")) or 0.0,
        "arcus_max_order_size": _f(raw.get("maxOrderSize")) or 0.0,
        "arcus_imf": imf,
        "arcus_mmf": _f(raw.get("maintenanceMarginFraction")),
        "max_leverage": max(1, int(1.0 / imf + 1e-9)) if imf and imf > 0 else 1,
    }


class ArcusVenue(SkeletonVenue):
    venue_id = "arcus"
    source = "移植自 Parallax Hedge（MIT）的 Arcus 签名、精度、强平价与 REST 实现"

    def __init__(self, secrets: dict[str, str] | None = None,
                 address: str | None = None,
                 account_index: int | None = None,
                 api_key: str | None = None,
                 api_private_key: str | None = None,
                 api_private_key_file: str | None = None,
                 api_url: str | None = None,
                 good_til_days: float = 40.0,
                 http: httpx.Client | None = None,
                 getter: Callable | None = None,
                 poster: Callable | None = None, **kw) -> None:
        super().__init__(**kw)
        secrets = secrets or {}
        self._address = (address or secrets.get("ARCUS_ADDRESS") or "").lower()
        self._account_index = account_index if account_index is not None else \
            int(secrets.get("ARCUS_ACCOUNT_INDEX") or 0)
        self._api_key_env = api_key or secrets.get("ARCUS_API_KEY") or ""
        self._priv_value = api_private_key or secrets.get("ARCUS_API_PRIVATE_KEY") or ""
        self._priv_file = api_private_key_file or \
            secrets.get("ARCUS_API_PRIVATE_KEY_FILE") or ""
        self._api_url = (api_url or secrets.get("ARCUS_API_URL")
                         or DEFAULT_API_URL).rstrip("/")
        self._good_til_days = good_til_days
        self._http = http
        self._getter = getter    # 可注入（测试用）：(path, params) -> Any
        self._poster = poster    # 可注入（测试用）：(path, body, ts, sig) -> (status, Any)
        self._key = None
        self._api_key = ""
        self._markets_cache: dict[str, dict] = {}
        self._markets_ts = 0.0
        self._order_market: dict[str, int] = {}

    # ── 基础 ────────────────────────────────────────────────
    def _get_http(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(
                base_url=self._api_url, timeout=15.0,
                headers={"User-Agent": "DeTSP/0.2", "Accept": "application/json"})
        return self._http

    def _get(self, path: str, params: dict | None = None):
        if self._getter is not None:
            return self._getter(path, params or {})
        r = self._get_http().get(path, params=params)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()

    def _get_key(self):
        """(Ed25519 私钥, API 公钥 hex)。填了 ARCUS_API_KEY 必须与私钥成对。"""
        if self._key is None:
            key = ax.load_private_key(self._priv_value, self._priv_file)
            derived = ax.public_key_hex(key)
            configured = self._api_key_env.lower().removeprefix("0x")
            if configured and configured != derived.lower():
                raise ValueError(
                    "ARCUS_API_KEY 与 ARCUS_API_PRIVATE_KEY 不是同一对密钥，已拒绝下单")
            self._key, self._api_key = key, derived
        return self._key, self._api_key

    def _post_signed(self, path: str, body: dict, timestamp: int,
                     signature: str) -> tuple[int, Any]:
        """签过名的 POST，只发一次，绝不重试（结果未知交给仓位判定）。"""
        if self._poster is not None:
            return self._poster(path, body, timestamp, signature)
        _, api_key = self._get_key()
        r = self._get_http().post(
            path, params={"address": self._address},
            content=json.dumps(body, separators=(",", ":")),
            headers={"X-API-Key": api_key, "X-Timestamp": str(timestamp),
                     "X-Signature": signature,
                     "Content-Type": "application/json"})
        try:
            data = r.json()
        except ValueError:
            data = {"error": r.text[:300]}
        return r.status_code, data

    def _account_params(self) -> dict:
        return {"address": self._address,
                "accountIndex": str(self._account_index)}

    # ── 市场 ────────────────────────────────────────────────
    def _load_markets(self) -> dict[str, dict]:
        now = time.monotonic()
        if not self._markets_cache or now - self._markets_ts > 8.0:
            payload = self._get("/v1/markets")
            rows = payload.get("markets") if isinstance(payload, dict) else payload
            cache: dict[str, dict] = {}
            for raw in rows or []:
                m = parse_market_fields(raw)
                if m is not None:
                    cache[m["asset"]] = m
            if not cache:
                raise RuntimeError("Arcus /v1/markets 没有返回可用市场")
            self._markets_cache, self._markets_ts = cache, now
        return self._markets_cache

    def _market(self, symbol: str) -> dict:
        m = self._load_markets().get(symbol.upper())
        if m is None:
            raise RuntimeError(f"Arcus 找不到市场 {symbol}")
        return m

    def markets(self) -> list[Market]:
        if self._paper is not None:
            return super().markets()
        return [Market(symbol=m["asset"],
                       price_step=float(m["arcus_tick_size"]),
                       qty_step=float(m["arcus_step_size"]),
                       min_qty=m["arcus_min_order_size"],
                       min_notional=m["arcus_min_notional"],
                       max_leverage=float(m["max_leverage"]),
                       # 维持保证金率来自 markets 的 maintenanceMarginFraction
                       maint_margin_rate=m["arcus_mmf"])
                for m in self._load_markets().values()]

    def book(self, symbol: str) -> Book:
        if self._paper is not None:
            return super().book(symbol)
        m = self._market(symbol)
        payload = self._get(f"/v1/bbo/{m['arcus_symbol']}")
        body = payload if isinstance(payload, dict) else {}
        bid = _f(_side_price(body.get("bestBid")))
        ask = _f(_side_price(body.get("bestAsk")))
        if not bid or not ask:
            raise RuntimeError(f"Arcus {symbol} 盘口为空")
        return Book(symbol=symbol, bid=bid, ask=ask, ts=time.time())

    def funding(self, symbol: str) -> FundingInfo:
        if self._paper is not None:
            return super().funding(symbol)
        payload = self._get("/v1/markets")
        rows = payload.get("markets") if isinstance(payload, dict) else payload
        for raw in rows or []:
            if str(raw.get("baseAsset") or "").upper() != symbol.upper():
                continue
            rate = _f(raw.get("nextFundingRate"))
            if rate is None:
                rate = _f(raw.get("fundingRate")) or 0.0
            # Arcus 资金费每小时结算，rate 本身就是每小时费率
            return FundingInfo(symbol=symbol, rate=rate,
                               next_ts=time.time() + 3600,
                               interval_seconds=3600)
        raise RuntimeError(f"Arcus 找不到市场 {symbol}")

    # ── 账户 ────────────────────────────────────────────────
    def _account_snapshot(self) -> dict:
        account = self._get("/v1/account", self._account_params())
        positions = self._get("/v1/positions", self._account_params())
        if account is None:
            account = {"equity": "0", "freeCollateral": "0"}
        if isinstance(account.get("account"), dict):
            account = account["account"]
        result = dict(account)
        rows = (positions or {}).get("positions") if isinstance(positions, dict) else None
        if rows is None:
            rows = account.get("positions")
        result["_positions"] = _position_rows(rows, self._account_index)
        return result

    def balance(self) -> dict:
        if self._paper is not None:
            return super().balance()
        snap = self._account_snapshot()
        return {"equity": _f(snap.get("equity")) or 0.0,
                "available": _f(snap.get("freeCollateral")) or 0.0}

    def positions(self) -> list[Position]:
        if self._paper is not None:
            return super().positions()
        snap = self._account_snapshot()
        markets = self._load_markets()
        by_id = {m["arcus_market_id"]: m for m in markets.values()}
        mmf_by_id = {mid: (m["arcus_mmf"] or 0.1) for mid, m in by_id.items()}
        equity = _f(snap.get("equity"))
        out: list[Position] = []
        for row in snap["_positions"]:
            try:
                market_id = int(row.get("marketId"))
            except (TypeError, ValueError):
                continue
            size = _f(row.get("size")) or 0.0
            if str(row.get("side") or "").upper() == "SHORT" and size > 0:
                size = -size
            if size == 0:
                continue
            m = by_id.get(market_id)
            _, other_notional, other_maint = ax.account_exposure(
                snap, mmf_by_id, exclude_market=market_id)
            liq = ax.arcus_liquidation_price(
                size=size, entry_price=_f(row.get("averageEntryPrice")),
                mark_price=_f(row.get("markPx")),
                margin_mode=row.get("marginMode"),
                margin_used=_f(row.get("marginUsed")),
                mmf=m["arcus_mmf"] if m else None,
                account_equity=equity,
                other_maintenance=other_maint, other_notional=other_notional)
            out.append(Position(
                symbol=m["asset"] if m else str(market_id), qty=size,
                entry_price=_f(row.get("averageEntryPrice")) or 0.0,
                liq_price=liq,
                # 持仓接口不给杠杆：liq 靠公式复算得出时可用，
                # 复算不出（liq=None）时强平距离不可评估（M5）
                leverage_known=False))
        return out

    def open_orders(self, symbol: str | None = None) -> list[Order]:
        if self._paper is not None:
            return super().open_orders(symbol)
        targets = [self._market(symbol)] if symbol else list(self._load_markets().values())
        out: list[Order] = []
        for m in targets:
            payload = self._get("/v1/openOrders", {
                **self._account_params(), "market": str(m["arcus_market_id"]),
                "status": "OPEN"})
            rows = payload.get("orders") if isinstance(payload, dict) else payload
            for o in rows or []:
                if not isinstance(o, dict):
                    continue
                oid = str(o.get("orderId") or "")
                cid = str(o.get("clientId") or "")
                if oid:
                    self._order_market[oid] = m["arcus_market_id"]
                if cid:
                    self._order_market[cid] = m["arcus_market_id"]
                qty_total = abs(_f(o.get("quantity") or o.get("size")) or 0.0)
                filled = abs(_f(o.get("filledSize")) or 0.0)
                out.append(Order(
                    order_id=oid or f"cid:{o.get('clientId')}",
                    symbol=m["asset"],
                    side=Side.BUY if str(o.get("side") or o.get("orderSide") or "")
                    .upper().startswith("B") else Side.SELL,
                    qty=qty_total, price=_f(o.get("price")),
                    filled_qty=filled, status="open"))
        return out

    # ── 交易 ────────────────────────────────────────────────
    def build_signed_order(self, market: dict, side: str, quantity: float,
                           price: float, reduce_only: bool = False,
                           time_in_force: str = "IOC") -> dict:
        """对齐精度 + 签名 + 组装请求体，但不发送（实盘预检可只签不发）。"""
        key, _ = self._get_key()
        if time_in_force == "ALO":
            limit = ax.D(price)  # 挂单价已由调用方对齐，不再二次取整
            ax.to_units(limit, market["arcus_tick_size"])
        else:
            limit = ax.round_arcus_price(market, price, side)
        qty = ax.align_arcus_quantity(market, quantity)
        if qty <= 0:
            raise ValueError(
                f"数量 {quantity:g} 对齐到步长 {market['arcus_step_size']} 后为 0")
        max_size = market.get("arcus_max_order_size") or 0
        if max_size and float(qty) > max_size:
            raise ValueError(f"数量 {qty} 超过 Arcus 单笔上限 {max_size:g}")
        ts = ax.timestamp_ns()
        gtt = ax.good_til_us(self._good_til_days)
        client_id = ax.new_client_id()
        order_side = "SELL" if side == "sell" else "BUY"
        payload = ax.build_place_payload(
            address=self._address, account_index=self._account_index,
            client_id=client_id, timestamp=ts, good_til_us_value=gtt,
            market_id=int(market["arcus_market_id"]),
            price_ticks=ax.to_units(limit, market["arcus_tick_size"]),
            quantity_quantums=ax.to_units(qty, market["arcus_step_size"]),
            reduce_only=reduce_only, side=order_side, time_in_force=time_in_force)
        body = {
            "address": self._address,
            "accountIndex": int(self._account_index),
            "marketId": int(market["arcus_market_id"]),
            "orderSide": order_side,
            "orderType": "LIMIT",
            "quantity": ax.fmt_decimal(qty),
            "price": ax.fmt_decimal(limit),
            "timeInForce": time_in_force,
            "goodTilTime": str(gtt),  # body 是微秒字符串；签名载荷里是纳秒
            "reduceOnly": bool(reduce_only),
            "clientId": client_id,
            "timestamp": ts,
        }
        return {"body": body, "payload": payload, "timestamp": ts,
                "signature": ax.sign_hex(key, payload), "client_id": client_id,
                "quantity": float(qty), "price": float(limit)}

    def place_order(self, req: OrderRequest) -> Order:
        if self._paper is not None:
            return super().place_order(req)
        m = self._market(req.symbol)
        side = "buy" if req.side is Side.BUY else "sell"
        if req.price is None:
            book = self.book(req.symbol)
            ref = book.ask if req.side is Side.BUY else book.bid
            price = ref * (1 + MARKET_SLIPPAGE * req.side.sign)
            tif = "IOC"
        else:
            price = req.price
            tif = "ALO" if req.post_only else "GTT"
        signed = self.build_signed_order(m, side, req.qty, price,
                                         req.reduce_only, tif)
        # S1：httpx 超时/断连统一翻译为 UnknownOutcomeError（只发一次不重试）
        with unknown_outcome_guard():
            status, data = self._post_signed(
                "/v1/placeOrder", signed["body"],
                signed["timestamp"], signed["signature"])
        receipt = ax.parse_order_response(status, data, signed["client_id"])
        ref = ax.order_ref(receipt.order_id, receipt.client_id) or ""
        if ref and not ref.startswith("cid:"):
            self._order_market[ref] = m["arcus_market_id"]
        if not receipt.accepted:
            if receipt.error and "IOC 未成交" in receipt.error:
                return Order(order_id=ref, symbol=req.symbol, side=req.side,
                             qty=signed["quantity"], price=signed["price"],
                             status="canceled")
            raise RuntimeError(receipt.error or "Arcus 下单被拒")
        filled = receipt.filled or 0.0
        return Order(order_id=ref, symbol=req.symbol, side=req.side,
                     qty=signed["quantity"], price=signed["price"],
                     filled_qty=filled,
                     status="filled" if filled >= signed["quantity"] else "open")

    def cancel_order(self, order_id: str) -> bool:
        if self._paper is not None:
            return super().cancel_order(order_id)
        by_client = str(order_id).startswith("cid:")
        plain_id = str(order_id)[4:] if by_client else str(order_id)
        market_id = self._order_market.get(plain_id)
        if market_id is None:
            self.open_orders()  # 刷新 order→market 映射后再找一次
            market_id = self._order_market.get(plain_id)
        if market_id is None and not by_client:
            return False
        if market_id is None:
            # 只剩 clientId 且不知道市场：无法构造撤单签名载荷（必须带 marketId）
            return False
        key, _ = self._get_key()
        ts = ax.timestamp_ns()
        payload = ax.build_cancel_payload(
            address=self._address, account_index=self._account_index,
            timestamp=ts, market_id=market_id,
            order_id=None if by_client else plain_id,
            client_id=plain_id if by_client else None)
        if by_client:
            # 未核实：按 clientId 撤单的 HTTP body 形状未在 Parallax 代码中出现过
            # （其只用 orderId 撤单）；签名载荷部分来自 arcus.py 已验证实现。
            # body 按 orderId 版本的对称形式组装，上实盘前需小额验证。
            body = {"kind": "clientId", "clientId": plain_id,
                    "address": self._address, "marketId": market_id,
                    "accountIndex": int(self._account_index), "timestamp": ts}
        else:
            body = {"kind": "orderId", "orderId": plain_id,
                    "address": self._address, "marketId": market_id,
                    "accountIndex": int(self._account_index), "timestamp": ts}
        status, _data = None, None
        with unknown_outcome_guard():
            status, _data = self._post_signed(
                "/v1/cancelOrder", body, ts, ax.sign_hex(key, payload))
        return status in (200, 202)

    def poll_order(self, order_id: str) -> Order | None:
        """S2：Arcus 在 Parallax 参考实现里没有单订单终态查询接口
        （只有 /v1/openOrders 按市场列 OPEN 单），故这里只能返回
        挂单中的订单；不在挂单中返回 None，成交发现由策略层走
        「挂单消失 + 持仓差」对账式口径（见 grid/rotation 的实现），
        不臆造不存在的终态接口。"""
        if self._paper is not None:
            return self._delegate().poll_order(order_id)
        for o in self.open_orders():
            if o.order_id == str(order_id):
                return o
        return None

    def set_leverage(self, symbol: str, leverage: int) -> bool:
        """设置某市场杠杆为全仓（旧式签名）。接口外附加能力，供预检/策略用。"""
        if self._paper is not None:
            return True
        m = self._market(symbol)
        key, _ = self._get_key()
        body = {"address": self._address,
                "accountIndex": int(self._account_index),
                "marketId": int(m["arcus_market_id"]),
                "leverage": int(leverage), "isolated": False}
        ts = ax.timestamp_ns()
        sig = ax.sign_hex(key, ax.legacy_message(ts, "setLeverage", body))
        status, data = self._post_signed("/v1/setLeverage", body, ts, sig)
        state = str((data or {}).get("status") or "").upper() \
            if isinstance(data, dict) else ""
        return status == 200 and state in ("APPLIED", "")


def _f(value) -> float | None:
    if value is None or value == "":
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result else None


def _side_price(side) -> Any:
    return side.get("price") if isinstance(side, dict) else side


def _position_rows(rows, account_index: int) -> list[dict]:
    """positions 可能是 {marketId: {...}} 也可能是数组，统一成数组，
    只留本子账户的。"""
    if isinstance(rows, dict):
        items = list(rows.values())
    elif isinstance(rows, list):
        items = rows
    else:
        return []
    out = []
    for row in items:
        if not isinstance(row, dict):
            continue
        if row.get("accountIndex") is not None:
            try:
                if int(row["accountIndex"]) != int(account_index):
                    continue
            except (TypeError, ValueError):
                continue
        out.append(row)
    return out
