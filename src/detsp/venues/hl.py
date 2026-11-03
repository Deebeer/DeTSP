"""Hyperliquid 家族适配（实盘）：本体 / Entropy / trade.xyz 共用参数化实现。

三者共用 Hyperliquid 的 API、官方 SDK（hyperliquid-python-sdk）与
API 钱包（agent）签名，仅 dex 不同：
- hyperliquid：dex=""（主 dex，本体永续市场；四仓原本没有，本项目新增）
- entropy：dex="io"，builder 子所，只支持逐仓
- tradexyz：dex="xyz"，builder 子所

同步接口说明：官方 SDK 的 Info / Exchange 是同步（requests）实现，
与 core.venue 的同步 VenueAdapter 接口直接对接。
移植来源：entropy-arb（MIT）的 venue_hl.py（其为自研 REST+签名实现；
本文件改用官方 SDK 当前版本 0.24 的 Info/Exchange，两者端点与语义一致）。
逐仓场所的杠杆上限处理在策略/风控层做，不在此处。

下单约定（接口层映射）：
- price=None → 市价单（SDK market_open，IOC 性质，默认 1% 滑点保护）
- price 有值 + post_only=True → ALO（只做 maker）
- price 有值 + post_only=False → GTC 限价（网格需要挂着的单）
"""

from __future__ import annotations

import math
import time

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

MAINNET_API_URL = "https://api.hyperliquid.xyz"
MIN_NOTIONAL_USD = 10.0  # Hyperliquid 最小下单名义金额（官方规则）
MARKET_SLIPPAGE = 0.01   # 市价单默认滑点保护 1%


class HyperliquidVenue(SkeletonVenue):
    source = "实盘基于 hyperliquid-python-sdk（官方）；设计参考 entropy-arb（MIT）"

    def __init__(self, dex: str = "", venue_id: str = "hyperliquid",
                 secrets: dict[str, str] | None = None,
                 account_address: str | None = None,
                 private_key: str | None = None,
                 base_url: str = MAINNET_API_URL,
                 info=None, exchange=None, **kw) -> None:
        self.dex = dex
        self.venue_id = venue_id
        super().__init__(**kw)
        secrets = secrets or {}
        # trade.xyz 可用独立账户（HL_*_XYZ），留空则与本体共用同一 API 钱包
        suffix = "_XYZ" if dex == "xyz" else ""
        self._account_address = (
            account_address or secrets.get(f"HL_ACCOUNT_ADDRESS{suffix}")
            or secrets.get("HL_ACCOUNT_ADDRESS") or ""
        ).lower()
        self._private_key = (
            private_key or secrets.get(f"HL_PRIVATE_KEY{suffix}")
            or secrets.get("HL_PRIVATE_KEY") or ""
        )
        self._base_url = base_url
        self._info = info            # 可注入（测试用）
        self._exchange = exchange    # 可注入（测试用）
        self._sz_decimals: dict[str, int] = {}
        self._oid_coin: dict[str, str] = {}

    # ── 客户端（懒加载，需要时才 import SDK）──────────────────
    def _get_info(self):
        if self._info is None:
            from hyperliquid.info import Info

            self._info = Info(self._base_url, skip_ws=True,
                              perp_dexs=["", "io", "xyz"])
        return self._info

    def _get_exchange(self):
        if self._exchange is None:
            if not self._private_key:
                raise RuntimeError(
                    f"{self.venue_id} 实盘下单缺少 API 钱包私钥（HL_PRIVATE_KEY）")
            from eth_account import Account
            from hyperliquid.exchange import Exchange

            wallet = Account.from_key(self._private_key)
            self._exchange = Exchange(
                wallet, self._base_url,
                account_address=self._account_address or None,
                perp_dexs=["", "io", "xyz"])
            if not self._account_address:
                self._account_address = wallet.address.lower()
        return self._exchange

    def _address(self) -> str:
        if self._account_address:
            return self._account_address
        if self._private_key:
            from eth_account import Account

            self._account_address = Account.from_key(self._private_key).address.lower()
            return self._account_address
        raise RuntimeError(f"{self.venue_id} 缺少账户地址（HL_ACCOUNT_ADDRESS）")

    # ── 命名与精度 ──────────────────────────────────────────
    def coin(self, symbol: str) -> str:
        """内部符号（BTC）→ HL 币名：子所带前缀，如 io:BTC。"""
        if ":" in symbol:
            return symbol
        return f"{self.dex}:{symbol}" if self.dex else symbol

    def _plain(self, coin: str) -> str:
        return coin.split(":", 1)[1] if ":" in coin else coin

    def _sz_decimals_for(self, symbol: str) -> int:
        coin = self.coin(symbol)
        if coin not in self._sz_decimals:
            self.markets()
        return self._sz_decimals.get(coin, 2)

    @staticmethod
    def _round_px(px: float, sz_decimals: int) -> float:
        """HL 价格规则：最多 5 位有效数字，且小数位 ≤ 6 − szDecimals。"""
        if px <= 0:
            return px
        max_dec = max(0, 6 - sz_decimals)
        sig_dec = 4 - math.floor(math.log10(px))
        dec = max(0, min(max_dec, sig_dec))
        return round(px, dec)

    def _round_qty(self, symbol: str, qty: float) -> float:
        dec = self._sz_decimals_for(symbol)
        f = 10 ** dec
        return math.floor(qty * f + 1e-9) / f

    # ── 只读接口 ────────────────────────────────────────────
    def markets(self) -> list[Market]:
        if self._paper is not None:
            return super().markets()
        meta = self._get_info().meta(dex=self.dex)
        out: list[Market] = []
        for a in meta.get("universe", []):
            if a.get("isDelisted"):
                continue
            sz_dec = int(a["szDecimals"])
            self._sz_decimals[a["name"]] = sz_dec
            out.append(Market(
                symbol=self._plain(a["name"]),
                # HL 无统一 tick：用官方允许的最细小数位近似（见 _round_px）
                price_step=10 ** (-(max(0, 6 - sz_dec))),
                qty_step=10 ** (-sz_dec),
                min_qty=10 ** (-sz_dec),
                min_notional=MIN_NOTIONAL_USD,
                max_leverage=float(a.get("maxLeverage") or 1),
            ))
        return out

    def book(self, symbol: str) -> Book:
        if self._paper is not None:
            return super().book(symbol)
        snap = self._get_info().l2_snapshot(self.coin(symbol))
        levels = snap.get("levels") or [[], []]
        bids, asks = levels[0], levels[1]
        if not bids or not asks:
            raise RuntimeError(f"{self.venue_id} {symbol} 盘口为空")
        return Book(symbol=symbol, bid=float(bids[0]["px"]),
                    ask=float(asks[0]["px"]),
                    ts=float(snap.get("time") or 0) / 1000)

    def _predicted_funding(self, coin: str) -> tuple[float, float] | None:
        """下期预测费率：POST /info type=predictedFundings。

        返回 (rate, next_ts)；接口不可用/无该币条目时返回 None，
        调用方回退当期口径并如实标注 rate_kind（审查 H2）。
        """
        try:
            data = self._get_info().post("/info",
                                         {"type": "predictedFundings"})
        except Exception:  # noqa: BLE001 — 预测接口失败不挡当期回退
            return None
        for entry in data or []:
            if not isinstance(entry, (list, tuple)) or len(entry) != 2:
                continue
            cname, venues = entry
            if cname != coin:
                continue
            for vname, payload in venues or []:
                if str(vname).lower() != "hyperliquid":
                    continue
                r = (payload or {}).get("fundingRate")
                if r is None:
                    return None
                nts = float((payload or {}).get("nextFundingTime") or 0)
                return float(r), nts / 1000 if nts > 1e10 else nts
            return None
        return None

    def funding(self, symbol: str) -> FundingInfo:
        if self._paper is not None:
            return super().funding(symbol)
        coin = self.coin(symbol)
        pred = self._predicted_funding(coin)
        if pred is not None:
            rate, next_ts = pred
            # HL 资金费按小时收取，预测值即下一期（每小时）费率
            return FundingInfo(symbol=symbol, rate=rate,
                               next_ts=next_ts or time.time() + 3600,
                               interval_seconds=3600,
                               rate_kind="predicted")
        meta, ctxs = self._get_info().meta_and_asset_ctxs(dex=self.dex)
        for a, ctx in zip(meta.get("universe", []), ctxs):
            if a["name"] == coin:
                # 回退口径：ctx.funding 是当期费率（非预测），如实标注
                return FundingInfo(symbol=symbol,
                                   rate=float(ctx.get("funding") or 0),
                                   next_ts=time.time() + 3600,
                                   interval_seconds=3600,
                                   rate_kind="current")
        raise RuntimeError(f"{self.venue_id} 找不到市场 {symbol}")

    def funding_snapshot(self) -> list[FundingInfo]:
        if self._paper is not None:
            return super().funding_snapshot()
        meta, ctxs = self._get_info().meta_and_asset_ctxs(dex=self.dex)
        out: list[FundingInfo] = []
        for a, ctx in zip(meta.get("universe", []), ctxs):
            out.append(FundingInfo(
                symbol=self._plain(str(a.get("name") or "")),
                rate=float(ctx.get("funding") or 0),
                interval_seconds=3600, rate_kind="current"))
        return out

    def balance(self) -> dict:
        if self._paper is not None:
            return super().balance()
        st = self._get_info().user_state(self._address(), dex=self.dex)
        ms = st.get("marginSummary") or {}
        return {"equity": float(ms.get("accountValue") or 0),
                "available": float(st.get("withdrawable") or 0)}

    def positions(self) -> list[Position]:
        if self._paper is not None:
            return super().positions()
        st = self._get_info().user_state(self._address(), dex=self.dex)
        out: list[Position] = []
        for ap in st.get("assetPositions") or []:
            p = ap.get("position") or {}
            qty = float(p.get("szi") or 0)
            if qty == 0:
                continue
            lev = p.get("leverage") or {}
            liq = p.get("liquidationPx")
            out.append(Position(
                symbol=self._plain(p["coin"]), qty=qty,
                entry_price=float(p.get("entryPx") or 0),
                leverage=float(lev.get("value") or 1),
                liq_price=float(liq) if liq else None))
        return out

    def open_orders(self, symbol: str | None = None) -> list[Order]:
        if self._paper is not None:
            return super().open_orders(symbol)
        rows = self._get_info().open_orders(self._address(), dex=self.dex)
        out: list[Order] = []
        for o in rows:
            plain = self._plain(o["coin"])
            if symbol is not None and plain != symbol:
                continue
            oid = str(o["oid"])
            self._oid_coin[oid] = o["coin"]
            orig = float(o.get("origSz") or o.get("sz") or 0)
            remaining = float(o.get("sz") or 0)
            out.append(Order(
                order_id=oid, symbol=plain,
                side=Side.BUY if o.get("side") == "B" else Side.SELL,
                qty=orig, price=float(o.get("limitPx") or 0) or None,
                filled_qty=max(orig - remaining, 0.0), status="open"))
        return out

    # ── 交易接口 ────────────────────────────────────────────
    def place_order(self, req: OrderRequest) -> Order:
        if self._paper is not None:
            return super().place_order(req)
        # S1：SDK 基于 requests，超时/断连统一翻译为 UnknownOutcomeError
        with unknown_outcome_guard():
            ex = self._get_exchange()
            coin = self.coin(req.symbol)
            qty = self._round_qty(req.symbol, req.qty)
            if qty <= 0:
                raise ValueError(f"{self.venue_id} 数量 {req.qty} 对齐精度后为 0")
            is_buy = req.side is Side.BUY
            if req.price is None:
                resp = ex.market_open(coin, is_buy, qty, px=None,
                                      slippage=MARKET_SLIPPAGE)
                limit_px = None
            else:
                px = self._round_px(req.price, self._sz_decimals_for(req.symbol))
                tif = "Alo" if req.post_only else "Gtc"
                resp = ex.order(coin, is_buy, qty, px,
                                {"limit": {"tif": tif}},
                                reduce_only=req.reduce_only)
                limit_px = px
            order = self._parse_order_response(resp, req, qty, limit_px)
            if order.order_id:
                self._oid_coin[order.order_id] = coin
            return order

    def _parse_order_response(self, resp, req: OrderRequest,
                              qty: float, limit_px: float | None) -> Order:
        if not isinstance(resp, dict) or resp.get("status") != "ok":
            raise RuntimeError(f"{self.venue_id} 下单失败：{str(resp)[:200]}")
        try:
            st = resp["response"]["data"]["statuses"][0]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"{self.venue_id} 下单回执异常：{str(resp)[:200]}") from exc
        if "filled" in st:
            f = st["filled"]
            return Order(order_id=str(f["oid"]), symbol=req.symbol, side=req.side,
                         qty=qty, price=limit_px,
                         filled_qty=float(f.get("totalSz") or qty), status="filled")
        if "resting" in st:
            return Order(order_id=str(st["resting"]["oid"]), symbol=req.symbol,
                         side=req.side, qty=qty, price=limit_px, status="open")
        if "error" in st:
            msg = str(st["error"])
            if "could not immediately match" in msg.lower():
                # IOC 吃不到对手价：未成交，不是故障
                return Order(order_id="", symbol=req.symbol, side=req.side,
                             qty=qty, price=limit_px, status="canceled")
            raise RuntimeError(f"{self.venue_id} 拒单：{msg}")
        raise RuntimeError(f"{self.venue_id} 未知下单回执：{str(st)[:150]}")

    def cancel_order(self, order_id: str) -> bool:
        if self._paper is not None:
            return super().cancel_order(order_id)
        coin = self._oid_coin.get(str(order_id))
        if coin is None:
            for o in self.open_orders():
                if o.order_id == str(order_id):
                    coin = self._oid_coin.get(o.order_id)
                    break
        if coin is None:
            return False
        with unknown_outcome_guard():
            resp = self._get_exchange().cancel(coin, int(order_id))
        return isinstance(resp, dict) and resp.get("status") == "ok"

    def poll_order(self, order_id: str) -> Order | None:
        """S2：用 Info.query_order_by_oid 查真实终态（限价单成交后不在
        挂单列表里，默认实现会误判）。查不到/接口异常时退回挂单列表。"""
        if self._paper is not None:
            return self._delegate().poll_order(order_id)
        try:
            oid = int(order_id)
        except (TypeError, ValueError):
            return None
        try:
            resp = self._get_info().query_order_by_oid(self._address(), oid)
        except Exception:  # noqa: BLE001 — 查询失败退回挂单列表口径
            resp = None
        if isinstance(resp, dict) and resp.get("status") == "order":
            o = (resp.get("order") or {}).get("order") or {}
            st = (resp.get("order") or {}).get("status") or ""
            coin = o.get("coin") or self._oid_coin.get(str(order_id), "")
            orig = float(o.get("origSz") or o.get("sz") or 0)
            remaining = float(o.get("sz") or 0)
            filled = max(orig - remaining, 0.0)
            status = {"open": "open", "filled": "filled",
                      "canceled": "canceled", "rejected": "rejected",
                      "marginCanceled": "canceled"}.get(st, st or "open")
            if status == "filled":
                filled = orig
            return Order(order_id=str(order_id),
                         symbol=self._plain(coin) if coin else "",
                         side=Side.BUY if o.get("side") == "B" else Side.SELL,
                         qty=orig, price=float(o.get("limitPx") or 0) or None,
                         filled_qty=filled, status=status)
        # unknownOid 等：退回挂单列表口径（不在挂单中 → None）
        for o in self.open_orders():
            if o.order_id == str(order_id):
                return o
        return None


def hyperliquid(**kw) -> HyperliquidVenue:
    return HyperliquidVenue(dex="", venue_id="hyperliquid", **kw)


def entropy(**kw) -> HyperliquidVenue:
    return HyperliquidVenue(dex="io", venue_id="entropy", **kw)


def tradexyz(**kw) -> HyperliquidVenue:
    return HyperliquidVenue(dex="xyz", venue_id="tradexyz", **kw)
