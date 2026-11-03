"""通用模拟适配（paper）：所有场所在 paper 模式下共用。

合成价格生成盘口；市价单立即按盘口价成交，限价单（含 post_only）
在盘口未触及时挂单等待，set_price 改价时按穿价规则撮合成交
（买单：ask <= 限价；卖单：bid >= 限价，成交价取限价）。
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
        leverage: float = 1.0,
    ) -> None:
        self.venue_id = venue_id
        self.mode = "paper"
        self.account = account
        self.leverage = leverage  # 盖到新建持仓上，供强平距离估算
        self._prices = dict(prices or {"BTC": 60_000.0, "ETH": 3_000.0})
        self._spread = spread_bps / 10_000
        self._equity = balance_usd
        self._positions: dict[str, Position] = {}
        self._orders: dict[str, Order] = {}       # 挂单中
        self._done: dict[str, Order] = {}         # 终态（filled/canceled）
        self._funding: dict[str, float] = {}
        self._seq = itertools.count(1)

    # 供测试/录制行情回放改价；改价后立即撮合被穿价的挂单
    def set_price(self, symbol: str, price: float) -> None:
        self._prices[symbol] = price
        self._match(symbol)

    def set_funding(self, symbol: str, rate: float) -> None:
        self._funding[symbol] = rate

    def markets(self) -> list[Market]:
        return [
            Market(symbol=s, price_step=0.1, qty_step=0.001, min_qty=0.001,
                   min_notional=10.0, max_leverage=50.0)
            for s in self._prices
        ]

    def book(self, symbol: str) -> Book:
        mid = self._prices[symbol]
        half = mid * self._spread / 2
        return Book(symbol=symbol, bid=mid - half, ask=mid + half)

    # ----- 撮合 -----
    def _apply_fill(self, symbol: str, side: Side, qty: float, price: float) -> None:
        signed = qty * side.sign
        pos = self._positions.get(symbol)
        if pos is None:
            self._positions[symbol] = Position(
                symbol=symbol, qty=signed, entry_price=price,
                leverage=self.leverage)
            return
        new_qty = pos.qty + signed
        if new_qty == 0:
            del self._positions[symbol]
            return
        # 同向加仓更新均价；反向减仓不动均价（简化模拟）
        if pos.qty * signed > 0:
            pos.entry_price = (
                pos.entry_price * abs(pos.qty) + price * qty
            ) / abs(new_qty)
        pos.qty = new_qty

    def _match(self, symbol: str) -> None:
        """撮合该币种被当前盘口穿价的挂单，成交价取限价。"""
        book = self.book(symbol)
        for oid, o in list(self._orders.items()):
            if o.symbol != symbol:
                continue
            crossed = (o.side is Side.BUY and book.ask <= (o.price or 0)) or \
                      (o.side is Side.SELL and book.bid >= (o.price or 0))
            if not crossed:
                continue
            o.status = "filled"
            o.filled_qty = o.qty
            self._apply_fill(symbol, o.side, o.qty, o.price or book.mid)
            del self._orders[oid]
            self._done[oid] = o

    def place_order(self, req: OrderRequest) -> Order:
        book = self.book(req.symbol)
        oid = f"paper-{next(self._seq)}"
        if req.price is not None:
            # 限价单：未被盘口穿价（或 post_only）则挂单等待
            marketable = (req.side is Side.BUY and req.price >= book.ask) or \
                         (req.side is Side.SELL and req.price <= book.bid)
            if req.post_only or not marketable:
                order = Order(order_id=oid, symbol=req.symbol, side=req.side,
                              qty=req.qty, price=req.price, status="open",
                              reduce_only=req.reduce_only)
                self._orders[oid] = order
                return order
            fill_price = book.ask if req.side is Side.BUY else book.bid
        else:
            fill_price = book.ask if req.side is Side.BUY else book.bid
        self._apply_fill(req.symbol, req.side, req.qty, fill_price)
        order = Order(order_id=oid, symbol=req.symbol, side=req.side,
                      qty=req.qty, price=req.price, filled_qty=req.qty,
                      status="filled", reduce_only=req.reduce_only)
        self._done[oid] = order
        return order

    def cancel_order(self, order_id: str) -> bool:
        o = self._orders.pop(order_id, None)
        if o is None:
            return False
        o.status = "canceled"
        self._done[order_id] = o
        return True

    def open_orders(self, symbol: str | None = None) -> list[Order]:
        return [o for o in self._orders.values()
                if symbol is None or o.symbol == symbol]

    def poll_order(self, order_id: str) -> Order | None:
        if order_id in self._orders:
            return self._orders[order_id]
        return self._done.get(order_id)

    def positions(self) -> list[Position]:
        return list(self._positions.values())

    def balance(self) -> dict:
        used = sum(p.notional / max(p.leverage, 1) for p in self._positions.values())
        return {"equity": self._equity, "available": max(self._equity - used, 0.0)}

    def funding(self, symbol: str) -> FundingInfo:
        return FundingInfo(symbol=symbol, rate=self._funding.get(symbol, 0.0))
