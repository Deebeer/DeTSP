"""通用模拟适配（paper）：所有场所在 paper 模式下共用。

用配置给定的合成价格生成盘口，下单立即按盘口价全额成交，
持仓/权益在本地维护。用于跑通策略与风控，不连任何真实接口。
"""

from __future__ import annotations

import itertools

from ..core.venue import (
    Book,
    FundingInfo,
    Market,
    Order,
    OrderRequest,
    Position,
    Side,
    VenueAdapter,
)


class PaperVenue(VenueAdapter):
    def __init__(
        self,
        venue_id: str = "paper",
        prices: dict[str, float] | None = None,
        balance_usd: float = 10_000.0,
        spread_bps: float = 2.0,
        account: str = "default",
    ) -> None:
        self.venue_id = venue_id
        self.mode = "paper"
        self.account = account
        self._prices = dict(prices or {"BTC": 60_000.0, "ETH": 3_000.0})
        self._spread = spread_bps / 10_000
        self._equity = balance_usd
        self._positions: dict[str, Position] = {}
        self._orders: dict[str, Order] = {}
        self._seq = itertools.count(1)

    # 供测试/录制行情回放改价
    def set_price(self, symbol: str, price: float) -> None:
        self._prices[symbol] = price

    def markets(self) -> list[Market]:
        return [
            Market(symbol=s, price_step=0.1, qty_step=0.001, min_qty=0.001,
                   min_notional=10.0, max_leverage=20.0)
            for s in self._prices
        ]

    def book(self, symbol: str) -> Book:
        mid = self._prices[symbol]
        half = mid * self._spread / 2
        return Book(symbol=symbol, bid=mid - half, ask=mid + half)

    def place_order(self, req: OrderRequest) -> Order:
        book = self.book(req.symbol)
        oid = f"paper-{next(self._seq)}"
        if req.post_only:
            # 挂单模式：先挂不成交，等后续批次撮合；本批记为 open
            order = Order(order_id=oid, symbol=req.symbol, side=req.side,
                          qty=req.qty, price=req.price, status="open")
            self._orders[oid] = order
            return order
        fill_price = book.ask if req.side is Side.BUY else book.bid
        signed = req.qty * req.side.sign
        pos = self._positions.get(req.symbol)
        if pos is None:
            self._positions[req.symbol] = Position(
                symbol=req.symbol, qty=signed, entry_price=fill_price)
        else:
            new_qty = pos.qty + signed
            if new_qty == 0:
                del self._positions[req.symbol]
            else:
                # 同向加仓更新均价；反向减仓不动均价（简化模拟）
                if pos.qty * signed > 0:
                    pos.entry_price = (
                        pos.entry_price * abs(pos.qty) + fill_price * req.qty
                    ) / abs(new_qty)
                pos.qty = new_qty
        return Order(order_id=oid, symbol=req.symbol, side=req.side,
                     qty=req.qty, price=req.price, filled_qty=req.qty,
                     status="filled")

    def cancel_order(self, order_id: str) -> bool:
        o = self._orders.pop(order_id, None)
        if o is None:
            return False
        o.status = "canceled"
        return True

    def open_orders(self, symbol: str | None = None) -> list[Order]:
        return [o for o in self._orders.values()
                if symbol is None or o.symbol == symbol]

    def positions(self) -> list[Position]:
        return list(self._positions.values())

    def balance(self) -> dict:
        used = sum(p.notional / max(p.leverage, 1) for p in self._positions.values())
        return {"equity": self._equity, "available": max(self._equity - used, 0.0)}

    def funding(self, symbol: str) -> FundingInfo:
        return FundingInfo(symbol=symbol, rate=0.0)
