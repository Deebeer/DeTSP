"""场所基类（内部）：paper 委托模拟适配；live 由各子类覆盖全部接口。

本类只负责 paper 委托与默认行为。各实盘场所（hl / lighter / arcus /
aster）已覆盖全部接口走真实实现；未覆盖的方法在 live 下抛
NotImplementedError，子类 docstring 注明实现/移植来源。
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

SOURCE_NOTE = "该方法尚未实现 live"


class SkeletonVenue(VenueAdapter):
    source: str = ""  # 移植来源说明（子类填写）

    def __init__(self, mode: str = "paper", account: str = "default",
                 prices: dict[str, float] | None = None,
                 quote_provider=None, walk: bool = False,
                 seed: int | None = None) -> None:
        self.mode = mode
        self.account = account
        self._paper = PaperVenue(venue_id=self.venue_id, account=account,
                                 prices=prices, quote_provider=quote_provider,
                                 walk=walk, seed=seed) \
            if mode == "paper" else None

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
