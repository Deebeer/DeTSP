"""统一交易所适配接口。

策略层只依赖本模块定义的 VenueAdapter，不感知任何交易所的签名细节。
所有场所（含 paper 模拟）都实现同一接口：市场 / 盘口 / 下单 / 撤单 /
持仓 / 资金费。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"

    @property
    def opposite(self) -> "Side":
        return Side.SELL if self is Side.BUY else Side.BUY

    @property
    def sign(self) -> int:
        return 1 if self is Side.BUY else -1


@dataclass
class Market:
    symbol: str
    price_step: float          # 价格精度（最小变动）
    qty_step: float            # 数量精度
    min_qty: float             # 最小下单量（币）
    min_notional: float = 0.0  # 最小名义金额
    max_leverage: float = 1.0


@dataclass
class Book:
    symbol: str
    bid: float
    ask: float
    ts: float = 0.0            # 盘口时间戳（秒）

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2


@dataclass
class OrderRequest:
    symbol: str
    side: Side
    qty: float                 # 币数量，正数
    price: float | None = None  # None = 市价
    reduce_only: bool = False
    post_only: bool = False    # True = 只做 maker（挂单）


@dataclass
class Order:
    order_id: str
    symbol: str
    side: Side
    qty: float
    price: float | None
    filled_qty: float = 0.0
    status: str = "open"       # open / filled / canceled / rejected


@dataclass
class Position:
    symbol: str
    qty: float                 # 带符号：多为正，空为负
    entry_price: float = 0.0
    leverage: float = 1.0
    liq_price: float | None = None  # 交易所给出的强平价（可得时）

    @property
    def notional(self) -> float:
        return abs(self.qty) * self.entry_price


@dataclass
class FundingInfo:
    symbol: str
    rate: float                # 当期资金费率（多头支付为正的约定）
    next_ts: float = 0.0


class VenueAdapter(ABC):
    """统一适配抽象接口。venue_id 见 DESIGN.md 的 7 所清单。"""

    venue_id: str = ""
    account: str = "default"   # 账户标识，用于归属登记的键
    mode: str = "paper"        # paper / live

    @abstractmethod
    def markets(self) -> list[Market]:
        """市场列表与规则。"""

    @abstractmethod
    def book(self, symbol: str) -> Book:
        """当前盘口。"""

    @abstractmethod
    def place_order(self, req: OrderRequest) -> Order:
        """下单。"""

    @abstractmethod
    def cancel_order(self, order_id: str) -> bool:
        """撤销单个订单。"""

    def cancel_all(self, symbol: str) -> int:
        """撤销该币种全部挂单，返回撤销数量（默认实现逐个撤）。"""
        n = 0
        for o in self.open_orders(symbol):
            if self.cancel_order(o.order_id):
                n += 1
        return n

    @abstractmethod
    def open_orders(self, symbol: str | None = None) -> list[Order]:
        """未成交挂单。"""

    @abstractmethod
    def positions(self) -> list[Position]:
        """当前持仓。"""

    @abstractmethod
    def balance(self) -> dict:
        """权益与可用：{"equity": float, "available": float}。"""

    @abstractmethod
    def funding(self, symbol: str) -> FundingInfo:
        """资金费信息。"""
