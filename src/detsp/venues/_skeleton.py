"""实盘场所骨架基类（内部）：paper 委托模拟适配，live 暂未移植。

本批不写实盘签名代码：mode=live 时所有接口抛 NotImplementedError，
并在子类 docstring 里注明移植来源，后续批次逐所落地。
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

SOURCE_NOTE = "实盘未移植"


class SkeletonVenue(VenueAdapter):
    source: str = ""  # 移植来源说明（子类填写）

    def __init__(self, mode: str = "paper", account: str = "default",
                 prices: dict[str, float] | None = None) -> None:
        self.mode = mode
        self.account = account
        self._paper = PaperVenue(venue_id=self.venue_id, account=account,
                                 prices=prices) if mode == "paper" else None

    def _live(self):
        raise NotImplementedError(f"{self.venue_id} {SOURCE_NOTE}：{self.source}")

    def _delegate(self) -> PaperVenue:
        if self._paper is None:
            self._live()
        return self._paper  # type: ignore[return-value]

    def markets(self) -> list[Market]:
        return self._delegate().markets()

    def book(self, symbol: str) -> Book:
        return self._delegate().book(symbol)

    def place_order(self, req: OrderRequest) -> Order:
        return self._delegate().place_order(req)

    def cancel_order(self, order_id: str) -> bool:
        return self._delegate().cancel_order(order_id)

    def open_orders(self, symbol: str | None = None) -> list[Order]:
        return self._delegate().open_orders(symbol)

    def positions(self) -> list[Position]:
        return self._delegate().positions()

    def balance(self) -> dict:
        return self._delegate().balance()

    def funding(self, symbol: str) -> FundingInfo:
        return self._delegate().funding(symbol)
