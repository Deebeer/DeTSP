"""Lighter 适配（实盘）：主网与 Robinhood 链共用参数化实现。

两部署的差异只有端点、chain id、账户与密钥：
- mainnet：https://mainnet.zklighter.elliot.ai，chain id 304，USDC 计价
- robinhood：https://api.rh.lighter.xyz，chain id 466324，USDG 计价

分工：只读接口走公开 REST（httpx 同步），签名下单走官方 lighter-sdk
（钉死 1.1.2：只有该版本带当前 Windows x64 签名器与 Robinhood 端点配置，
见 pyproject 的 live 依赖组）。SignerClient 的下单方法是 async，
本文件用小工具在同步接口里跑协程。

移植来源：entropy-arb（MIT）的 venue_lighter.py 与 Parallax Hedge（MIT）
的 Lighter 读接口口径。

注意（与接口的偏差，调用方需知）：
- 市价单是异步结算：place_order 返回时成交未知，status="open"、
  filled_qty=0，成交量以 positions()/open_orders() 对账为准（与 Parallax
  按仓位判定成交的口径一致）。
- place_order 返回的 order_id 形如 "coi:<client_order_index>"；
  open_orders() 返回的 order_id 是交易所 order_index（字符串）。
  cancel_order 两种都认：coi 形式会先查活跃单换算成 order_index。
- Lighter 资金费 /api/v1/funding-rates 返回的是每 8 小时费率
  （该接口混有别家费率，必须按 exchange=="lighter" 过滤）。
"""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass

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

FUNDING_INTERVAL_SEC = 8 * 3600  # Lighter 费率周期 8 小时（见模块说明）
MARKET_SLIPPAGE_BPS = 100.0      # 市价单平均成交价保护：默认 1%


@dataclass(frozen=True)
class LighterProfile:
    api_url: str
    ws_url: str
    chain_id: int


PROFILES = {
    "mainnet": LighterProfile(
        "https://mainnet.zklighter.elliot.ai",
        "wss://mainnet.zklighter.elliot.ai/stream", 304),
    "robinhood": LighterProfile(
        "https://api.rh.lighter.xyz",
        "wss://api.rh.lighter.xyz/stream", 466324),
}


def _run(coro):
    """在同步接口里跑 SDK 的 async 方法：无事件循环时直接 asyncio.run，
    已在事件循环中时另起线程跑，避免 'loop already running'。"""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    box: dict = {}

    def _target():
        try:
            box["value"] = asyncio.run(coro)
        except BaseException as exc:  # noqa: BLE001
            box["error"] = exc

    t = threading.Thread(target=_target, daemon=True)
    t.start()
    t.join()
    if "error" in box:
        raise box["error"]
    return box.get("value")


def _int_or_none(v) -> int | None:
    try:
        return int(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


class LighterVenue(SkeletonVenue):
    source = "实盘基于 lighter-sdk 1.1.2（官方）；读接口移植自 Parallax/entropy-arb（MIT）"

    def __init__(self, deployment: str = "mainnet",
                 secrets: dict[str, str] | None = None,
                 account_index: int | None = None,
                 api_key_index: int | None = None,
                 api_private_key: str | None = None,
                 api_private_key_file: str | None = None,
                 slippage_bps: float = MARKET_SLIPPAGE_BPS,
                 http: httpx.Client | None = None, signer=None, **kw) -> None:
        if deployment not in PROFILES:
            raise ValueError(f"未知 Lighter 部署：{deployment}")
        self.deployment = deployment
        self.profile = PROFILES[deployment]
        self.venue_id = "lighter" if deployment == "mainnet" else "lighter_rh"
        super().__init__(**kw)
        secrets = secrets or {}
        prefix = "LIGHTER" if deployment == "mainnet" else "LIGHTER_RH"
        self._account_index = account_index if account_index is not None else \
            _int_or_none(secrets.get(f"{prefix}_ACCOUNT_INDEX"))
        self._api_key_index = api_key_index if api_key_index is not None else \
            _int_or_none(secrets.get(f"{prefix}_API_KEY_INDEX"))
        self._api_private_key = api_private_key or secrets.get(
            f"{prefix}_API_PRIVATE_KEY") or ""
        self._api_private_key_file = api_private_key_file or secrets.get(
            f"{prefix}_API_PRIVATE_KEY_FILE") or ""
        self._slippage_bps = slippage_bps
        self._http = http        # 可注入（测试用）
        self._signer = signer    # 可注入（测试用）
        self._markets_cache: dict[str, dict] = {}
        self._coi = int(time.time() * 1000)

    # ── 客户端 ──────────────────────────────────────────────
    def _get_http(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(base_url=self.profile.api_url, timeout=15.0)
        return self._http

    def _get(self, path: str, params: dict | None = None,
             headers: dict | None = None) -> dict:
        r = self._get_http().get(path, params=params, headers=headers)
        r.raise_for_status()
        return r.json()

    def _load_private_key(self) -> str:
        if self._api_private_key:
            return self._api_private_key.strip()
        if self._api_private_key_file:
            from pathlib import Path

            return Path(self._api_private_key_file).read_text(encoding="utf-8").strip()
        return ""

    def _get_signer(self):
        if self._signer is None:
            if self._account_index is None or self._api_key_index is None:
                raise RuntimeError(
                    f"{self.venue_id} 实盘缺少 account_index / api_key_index")
            key = self._load_private_key()
            if not key:
                raise RuntimeError(f"{self.venue_id} 实盘缺少 API 私钥")
            from lighter import SignerClient

            self._signer = SignerClient(
                url=self.profile.api_url,
                account_index=self._account_index,
                api_private_keys={self._api_key_index: key},
                chain_id=self.profile.chain_id)
            err = self._signer.check_client()
            if err is not None:
                raise RuntimeError(f"{self.venue_id} API 密钥校验失败：{err}")
        return self._signer

    def _auth_token(self) -> str:
        token, err = self._get_signer().create_auth_token_with_expiry()
        if err is not None:
            raise RuntimeError(f"{self.venue_id} 生成认证令牌失败：{err}")
        return token

    # ── 市场缓存 ────────────────────────────────────────────
    def _load_markets(self) -> dict[str, dict]:
        if not self._markets_cache:
            data = self._get("/api/v1/orderBooks")
            for ob in data.get("order_books") or []:
                if str(ob.get("status")) != "active":
                    continue
                self._markets_cache[str(ob["symbol"])] = {
                    "market_id": int(ob["market_id"]),
                    "price_decimals": int(ob["supported_price_decimals"]),
                    "size_decimals": int(ob["supported_size_decimals"]),
                    "min_base": float(ob["min_base_amount"]),
                    "min_quote": float(ob["min_quote_amount"]),
                }
        return self._markets_cache

    def _market(self, symbol: str) -> dict:
        m = self._load_markets().get(symbol)
        if m is None:
            raise RuntimeError(f"{self.venue_id} 找不到市场 {symbol}")
        return m

    def _symbol_of(self, market_id: int) -> str:
        for sym, m in self._load_markets().items():
            if m["market_id"] == market_id:
                return sym
        return str(market_id)

    # ── 只读接口 ────────────────────────────────────────────
    def markets(self) -> list[Market]:
        if self._paper is not None:
            return super().markets()
        return [Market(symbol=sym,
                       price_step=10 ** (-m["price_decimals"]),
                       qty_step=10 ** (-m["size_decimals"]),
                       min_qty=m["min_base"], min_notional=m["min_quote"],
                       max_leverage=0.0)  # orderBooks 不含杠杆上限，0 表示未知
                for sym, m in self._load_markets().items()]

    def book(self, symbol: str) -> Book:
        if self._paper is not None:
            return super().book(symbol)
        m = self._market(symbol)
        data = self._get("/api/v1/orderBookOrders",
                         params={"market_id": m["market_id"], "limit": 5})
        bids, asks = data.get("bids") or [], data.get("asks") or []
        if not bids or not asks:
            raise RuntimeError(f"{self.venue_id} {symbol} 盘口为空")
        return Book(symbol=symbol, bid=float(bids[0]["price"]),
                    ask=float(asks[0]["price"]), ts=time.time())

    def funding(self, symbol: str) -> FundingInfo:
        if self._paper is not None:
            return super().funding(symbol)
        m = self._market(symbol)
        data = self._get("/api/v1/funding-rates")
        for row in data.get("funding_rates") or []:
            if str(row.get("exchange") or "").lower() != "lighter":
                continue
            if int(row.get("market_id")) == m["market_id"]:
                return FundingInfo(symbol=symbol, rate=float(row["rate"]),
                                   next_ts=time.time() + FUNDING_INTERVAL_SEC,
                                   interval_seconds=FUNDING_INTERVAL_SEC)
        raise RuntimeError(f"{self.venue_id} 找不到 {symbol} 的资金费率")

    def _account_data(self) -> dict:
        if self._account_index is None:
            raise RuntimeError(f"{self.venue_id} 缺少 account_index")
        data = self._get("/api/v1/account",
                         params={"by": "index", "value": str(self._account_index)})
        accounts = data.get("accounts") or []
        if not accounts:
            raise RuntimeError(f"{self.venue_id} 找不到账户 {self._account_index}")
        return accounts[0]

    def balance(self) -> dict:
        if self._paper is not None:
            return super().balance()
        acct = self._account_data()
        return {"equity": float(acct.get("total_asset_value") or 0),
                "available": float(acct.get("available_balance") or 0)}

    def positions(self) -> list[Position]:
        if self._paper is not None:
            return super().positions()
        acct = self._account_data()
        out: list[Position] = []
        for p in acct.get("positions") or []:
            qty = float(p.get("sign") or 1) * float(p.get("position") or 0)
            if qty == 0:
                continue
            liq = p.get("liquidation_price")
            out.append(Position(
                symbol=self._symbol_of(int(p["market_id"])), qty=qty,
                entry_price=float(p.get("avg_entry_price") or 0),
                liq_price=float(liq) if liq not in (None, "", "0") else None,
                # 账户持仓接口不给杠杆：liq 缺失时强平距离不可评估（M5）
                leverage_known=False))
        return out

    def _active_orders_raw(self) -> list[dict]:
        """活跃挂单（带认证令牌）：含 order_index 与 client_order_index 对照。"""
        data = self._get("/api/v1/accountActiveOrders",
                         params={"account_index": self._account_index},
                         headers={"authorization": self._auth_token()})
        return data.get("orders") or []

    def open_orders(self, symbol: str | None = None) -> list[Order]:
        if self._paper is not None:
            return super().open_orders(symbol)
        out: list[Order] = []
        for o in self._active_orders_raw():
            sym = self._symbol_of(int(o["market_index"]))
            if symbol is not None and sym != symbol:
                continue
            total = float(o.get("initial_base_amount") or 0)
            filled = float(o.get("filled_base_amount") or 0)
            out.append(Order(
                order_id=str(o["order_index"]), symbol=sym,
                side=Side.SELL if o.get("is_ask") else Side.BUY,
                qty=total, price=float(o.get("price") or 0) or None,
                filled_qty=filled, status="open"))
        return out

    # ── 交易接口 ────────────────────────────────────────────
    def _next_coi(self) -> int:
        self._coi += 1
        return self._coi

    def place_order(self, req: OrderRequest) -> Order:
        if self._paper is not None:
            return super().place_order(req)
        from lighter import SignerClient

        signer = self._get_signer()
        book = self.book(req.symbol)
        if req.price is None:
            # 市价：参考价取对手价再加滑点保护（平均成交价口径）
            ref = book.ask if req.side is Side.BUY else book.bid
            limit_px = ref * (1 + self._slippage_bps / 10_000 * req.side.sign)
            order_type = SignerClient.ORDER_TYPE_MARKET
            tif = SignerClient.ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL
            expiry = SignerClient.DEFAULT_IOC_EXPIRY
        else:
            limit_px = req.price
            order_type = SignerClient.ORDER_TYPE_LIMIT
            tif = (SignerClient.ORDER_TIME_IN_FORCE_POST_ONLY if req.post_only
                   else SignerClient.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME)
            expiry = SignerClient.DEFAULT_28_DAY_ORDER_EXPIRY
        m = self._market(req.symbol)
        base = int(round(req.qty * 10 ** m["size_decimals"]))
        px = int(round(limit_px * 10 ** m["price_decimals"]))
        if base <= 0:
            raise ValueError(f"{self.venue_id} 数量 {req.qty} 对齐精度后为 0")
        coi = self._next_coi()
        # S1：SDK/网络超时统一翻译为 UnknownOutcomeError（禁止上层重发）
        with unknown_outcome_guard():
            _tx, resp, err = _run(signer.create_order(
                market_index=m["market_id"], client_order_index=coi,
                base_amount=base, price=px, is_ask=req.side is Side.SELL,
                order_type=order_type, time_in_force=tif,
                reduce_only=req.reduce_only, order_expiry=expiry))
        if err is not None or (getattr(resp, "code", 200) or 200) != 200:
            msg = str(err) if err is not None else \
                f"code={resp.code} msg={getattr(resp, 'message', None)}"
            raise RuntimeError(f"{self.venue_id} 下单被拒：{msg}")
        return Order(order_id=f"coi:{coi}", symbol=req.symbol, side=req.side,
                     qty=req.qty, price=req.price, status="open")

    def cancel_order(self, order_id: str) -> bool:
        if self._paper is not None:
            return super().cancel_order(order_id)
        signer = self._get_signer()
        target_index: int | None = None
        target_market: int | None = None
        if str(order_id).startswith("coi:"):
            coi = int(str(order_id)[4:])
            for o in self._active_orders_raw():
                if int(o.get("client_order_index") or -1) == coi:
                    target_index = int(o["order_index"])
                    target_market = int(o["market_index"])
                    break
            if target_index is None:
                return False  # 已不在活跃单里（已成交/已撤）
        else:
            target_index = int(order_id)
            for o in self._active_orders_raw():
                if int(o.get("order_index") or -1) == target_index:
                    target_market = int(o["market_index"])
                    break
            if target_market is None:
                return False
        _tx, resp, err = None, None, None
        with unknown_outcome_guard():
            _tx, resp, err = _run(signer.cancel_order(
                market_index=target_market, order_index=target_index))
        if err is not None:
            raise RuntimeError(f"{self.venue_id} 撤单失败：{err}")
        return (getattr(resp, "code", 200) or 200) == 200

    # ── 订单终态查询（S2）────────────────────────────────────
    def _row_to_order(self, o: dict, status: str) -> Order:
        total = float(o.get("initial_base_amount") or 0)
        filled = float(o.get("filled_base_amount") or 0)
        return Order(
            order_id=str(o.get("order_index")), symbol=self._symbol_of(
                int(o.get("market_index") or 0)),
            side=Side.SELL if o.get("is_ask") else Side.BUY,
            qty=total, price=float(o.get("price") or 0) or None,
            filled_qty=filled, status=status)

    def _inactive_orders_raw(self) -> list[dict]:
        """历史订单（终态）：/api/v1/accountInactiveOrders。

        注：端点名按 Lighter REST 命名规律（active 的对应面）采用，
        未在参考实现中实证——调用失败时 poll_order 退回对账式口径，
        不影响正确性，只损失终态细节。
        """
        data = self._get("/api/v1/accountInactiveOrders",
                         params={"account_index": self._account_index},
                         headers={"authorization": self._auth_token()})
        return data.get("orders") or []

    def poll_order(self, order_id: str) -> Order | None:
        """S2：先查活跃单（open/部分成交），再查历史单（终态）；
        历史接口不可用时返回 None，由策略层走「挂单消失+持仓差」
        对账式发现。order_id 认 "coi:<n>" 与 order_index 两种形式。"""
        if self._paper is not None:
            return self._delegate().poll_order(order_id)
        want_coi = int(str(order_id)[4:]) if str(order_id).startswith("coi:") \
            else None
        want_idx = None if want_coi is not None else _int_or_none(order_id)

        def _match(o: dict) -> bool:
            if want_coi is not None:
                return int(o.get("client_order_index") or -1) == want_coi
            return int(o.get("order_index") or -1) == want_idx

        for o in self._active_orders_raw():
            if _match(o):
                return self._row_to_order(o, "open")
        try:
            rows = self._inactive_orders_raw()
        except Exception:  # noqa: BLE001 — 历史接口不可用 → 对账式兜底
            return None
        for o in rows:
            if _match(o):
                total = float(o.get("initial_base_amount") or 0)
                filled = float(o.get("filled_base_amount") or 0)
                st = str(o.get("status") or "").lower()
                if filled >= total and total > 0:
                    status = "filled"
                elif "cancel" in st:
                    status = "canceled"
                elif filled > 0:
                    status = "canceled"  # 部分成交后终结
                else:
                    status = "canceled"
                return self._row_to_order(o, status)
        return None


def lighter_mainnet(**kw) -> LighterVenue:
    return LighterVenue(deployment="mainnet", **kw)


def lighter_robinhood(**kw) -> LighterVenue:
    return LighterVenue(deployment="robinhood", **kw)
