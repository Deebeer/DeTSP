"""DRY_RUN 代理场所：真实行情/账户读 + 本地模拟下单。

语义（DESIGN 第 9 节）：DRY_RUN=true 时走全部真实决策，但不向交易所
发送任何订单。本代理把读接口（markets/book/funding/balance）透传给
实盘场所，把下单与持仓状态交给内置的 PaperVenue 模拟——下单前用实盘
盘口中价同步模拟价格，使成交/持仓/对账在本地自洽，账本记录的是
模拟成交（与实盘账本同表，靠 venue 模式区分）。
"""

from __future__ import annotations

from ..core.venue import (
    Book,
    FundingInfo,
    Market,
    Order,
    OrderRequest,
    Position,
    VenueAdapter,
)
from .paper import PaperVenue


class DryRunVenue(VenueAdapter):
    def __init__(self, live_venue: VenueAdapter,
                 balance_usd: float = 10_000.0) -> None:
        self._live = live_venue
        self.venue_id = live_venue.venue_id
        self.account = live_venue.account
        self.mode = "live"          # 读口径按实盘；下单被代理拦截
        self.dry_run = True
        self._paper = PaperVenue(venue_id=self.venue_id,
                                 account=self.account,
                                 balance_usd=balance_usd)

    def _sync_price(self, symbol: str) -> None:
        """用实盘盘口中价同步模拟撮合价格。"""
        try:
            mid = self._live.book(symbol).mid
        except Exception:
            return
        if mid > 0:
            self._paper.set_price(symbol, mid)

    # ----- 读：透传实盘 -----
    def markets(self) -> list[Market]:
        return self._live.markets()

    def book(self, symbol: str) -> Book:
        return self._live.book(symbol)

    def funding(self, symbol: str) -> FundingInfo:
        return self._live.funding(symbol)

    def funding_snapshot(self) -> list[FundingInfo]:
        return self._live.funding_snapshot()

    def balance(self) -> dict:
        # 权益读实盘账户（只读不下单）；实盘读失败时回退模拟权益
        try:
            return self._live.balance()
        except Exception:
            return self._paper.balance()

    # ----- 写：本地模拟，绝不发送 -----
    def place_order(self, req: OrderRequest) -> Order:
        self._sync_price(req.symbol)
        return self._paper.place_order(req)

    def cancel_order(self, order_id: str) -> bool:
        return self._paper.cancel_order(order_id)

    def open_orders(self, symbol: str | None = None) -> list[Order]:
        return self._paper.open_orders(symbol)

    def poll_order(self, order_id: str) -> Order | None:
        return self._paper.poll_order(order_id)

    def positions(self) -> list[Position]:
        return self._paper.positions()
