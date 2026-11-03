"""通用模拟适配（paper）：所有场所在 paper 模式下共用。

合成价格生成盘口；市价单立即按盘口价成交，限价单（含 post_only）
在盘口未触及时挂单等待，set_price 改价时按穿价规则撮合成交
（买单：ask <= 限价；卖单：bid >= 限价，成交价取限价）。
持仓/权益在本地维护。用于跑通策略与风控，不连任何真实接口。

价格驱动（H6 修复）：原实现只有写死的 BTC/ETH 静态价——网格永不
成交、套利永无信号、未知 symbol 直接 KeyError。现在 book() 取价顺序：
1. 注入的 quote_provider（公开行情只读接口，如 venues.quotes 的
   PublicQuoteProvider，失败返回 None 自动回退）；
2. walk=True 时的内置随机游走（可设种子复现，每次 book 按 ±5bps
   游走一步并触发挂单撮合）；
3. 静态价（默认行为，既有测试/手动 set_price 驱动不受影响）。
未知 symbol 不再报错：以 DEFAULT_WALK_START 兜底价起步。
"""

from __future__ import annotations

import itertools
import random

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

# paper 内置起始价：覆盖 config 示例与常用标的
DEFAULT_PRICES = {"BTC": 60_000.0, "ETH": 3_000.0, "SNDK": 250.0,
                  "SOL": 150.0, "HYPE": 40.0}
DEFAULT_WALK_START = 100.0  # 未知 symbol 的兜底起始价


class PaperVenue(VenueAdapter):
    def __init__(
        self,
        venue_id: str = "paper",
        prices: dict[str, float] | None = None,
        balance_usd: float = 10_000.0,
        spread_bps: float = 2.0,
        account: str = "default",
        leverage: float = 1.0,
        quote_provider=None,
        walk: bool = False,
        seed: int | None = None,
    ) -> None:
        self.venue_id = venue_id
        self.mode = "paper"
        self.account = account
        self.leverage = leverage  # 盖到新建持仓上，供强平距离估算
        self._prices = dict(prices or DEFAULT_PRICES)
        self._spread = spread_bps / 10_000
        self._equity = balance_usd
        self._positions: dict[str, Position] = {}
        self._orders: dict[str, Order] = {}       # 挂单中
        self._done: dict[str, Order] = {}         # 终态（filled/canceled）
        self._funding: dict[str, tuple[float, float]] = {}
        self._seq = itertools.count(1)
        # 价格驱动（H6）：公开行情 provider + 随机游走兜底
        self._quote_provider = quote_provider
        self._walk = walk
        self._rng = random.Random(seed)

    # 供测试/录制行情回放改价；改价后立即撮合被穿价的挂单
    def set_price(self, symbol: str, price: float) -> None:
        self._prices[symbol] = price
        self._match(symbol)

    def set_funding(self, symbol: str, rate: float,
                    interval_seconds: float = 3600.0) -> None:
        self._funding[symbol] = (rate, interval_seconds)

    def markets(self) -> list[Market]:
        return [
            Market(symbol=s, price_step=0.1, qty_step=0.001, min_qty=0.001,
                   min_notional=10.0, max_leverage=50.0)
            for s in self._prices
        ]

    def _drive_price(self, symbol: str) -> float:
        """book() 的取价：公开行情 → 随机游走 → 静态价（H6）。"""
        if self._quote_provider is not None:
            q = self._quote_provider.mid(self.venue_id, symbol)
            if q is not None and q > 0:
                self._prices[symbol] = q
                return q
        cur = self._prices.get(symbol)
        if cur is None:
            cur = DEFAULT_WALK_START
            self._prices[symbol] = cur
        if self._walk:
            cur = cur * (1 + self._rng.uniform(-0.0005, 0.0005))
            self._prices[symbol] = cur
            self._match(symbol)
        return cur

    def book(self, symbol: str) -> Book:
        mid = self._drive_price(symbol)
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
        """撮合该币种被当前盘口穿价的挂单，成交价取限价。

        直接用当前静态价算盘口，不走 _drive_price（避免游走递归）。
        """
        mid = self._prices[symbol]
        half = mid * self._spread / 2
        book = Book(symbol=symbol, bid=mid - half, ask=mid + half)
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
        rate, interval = self._funding.get(symbol, (0.0, 3600.0))
        return FundingInfo(symbol=symbol, rate=rate, interval_seconds=interval)
