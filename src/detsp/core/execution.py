"""统一执行层：所有策略下单的唯一通道。

硬规则（任何策略不得绕过）：
- 下单前：registry.claim 校验归属（冲突直接抛 ConflictError）；
  开仓动作过 KillSwitch、强平距离门槛与三层仓位上限（场所/策略/全局）。
- 下单后：成交写统一账本（ledger），订单登记归属（registry）。
- 结果未知（超时/连接中断/适配器抛 UnknownOutcomeError）：**绝不重发**，
  立即对账（查该场所持仓与挂单），把对账结果随 ExecResult 返回，
  由策略按真实持仓决策。
- 部分成交：按实际 filled_qty 记账与处理，剩余数量不自动补下。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .ledger import Fill, Ledger
from .registry import Key, OwnershipRegistry
from .risk import KillSwitch, within_cap
from .venue import Order, OrderRequest, Position, Side, VenueAdapter


class UnknownOutcomeError(RuntimeError):
    """下单结果未知（超时/断连/回执丢失）：禁止重发，必须先对账。"""


@dataclass
class Caps:
    """三层名义金额上限（<=0 表示该层不限）。"""

    venue: dict[str, float] = field(default_factory=dict)
    strategy: dict[str, float] = field(default_factory=dict)
    global_: float = 0.0


@dataclass
class ExecResult:
    status: str  # filled / open / partial / rejected / unknown
    order: Order | None = None
    filled_qty: float = 0.0
    fill_price: float = 0.0
    detail: str = ""
    position: Position | None = None  # status=unknown 时附对账持仓


class Executor:
    def __init__(
        self,
        venues: dict[str, VenueAdapter],
        registry: OwnershipRegistry | None = None,
        ledger: Ledger | None = None,
        kill_switch: KillSwitch | None = None,
        caps: Caps | None = None,
        min_corridor_pct: float = 3.0,
    ) -> None:
        self.venues = dict(venues)
        self.registry = registry or OwnershipRegistry()
        self.ledger = ledger or Ledger()
        self.kill_switch = kill_switch or KillSwitch()
        self.caps = caps or Caps()
        self.min_corridor_pct = min_corridor_pct

    # ----- 基础查询 -----
    def venue(self, venue_id: str) -> VenueAdapter:
        return self.venues[venue_id]

    def key(self, venue_id: str, symbol: str) -> Key:
        return Key(venue=venue_id, account=self.venues[venue_id].account,
                   symbol=symbol)

    def claim(self, strategy: str, venue_id: str, symbol: str) -> Key:
        key = self.key(venue_id, symbol)
        self.registry.claim(key, strategy)
        return key

    def release(self, strategy: str, venue_id: str, symbol: str) -> None:
        self.registry.release(self.key(venue_id, symbol), strategy)

    def position(self, venue_id: str, symbol: str) -> Position | None:
        for p in self.venues[venue_id].positions():
            if p.symbol == symbol:
                return p
        return None

    def reconcile(self, venue_id: str, symbol: str) -> Position | None:
        """对账：以场所真实持仓为准（下单结果未知/定期核对时用）。"""
        return self.position(venue_id, symbol)

    def _total_notional(self) -> float:
        total = 0.0
        for v in self.venues.values():
            for p in v.positions():
                total += p.notional
        return total

    def record_fill(self, strategy: str, venue_id: str, symbol: str,
                    side: Side, qty: float, price: float,
                    order_id: str = "") -> None:
        """补记成交：挂单在下单之后才成交时（如限价单被行情穿价），
        由策略在轮询发现成交后调用，保证账本不漏记。"""
        adapter = self.venues[venue_id]
        self.ledger.record(Fill(
            venue=venue_id, account=adapter.account, symbol=symbol,
            strategy=strategy, side=side.value, qty=qty, price=price,
            fee=0.0, order_id=order_id))

    # ----- 下单 -----
    def place(self, strategy: str, venue_id: str, req: OrderRequest, *,
              intent: str = "open",
              corridor_pct: float | None = None) -> ExecResult:
        """统一下单入口。

        intent: open=开/加仓（过全部风控）；close=平/减仓（安全动作，
        停机时也允许，不查上限与强平距离）。
        corridor_pct: 调用方估算的开仓后强平距离，低于门槛直接拒。
        """
        adapter = self.venues[venue_id]
        key = self.key(venue_id, req.symbol)
        # 归属校验：被其他策略占用直接抛 ConflictError
        self.registry.claim(key, strategy)

        book = adapter.book(req.symbol)
        ref_price = req.price if req.price is not None else book.mid
        cur = self.position(venue_id, req.symbol)
        cur_qty = cur.qty if cur else 0.0

        if intent == "open":
            self.kill_switch.assert_allowed()
            if corridor_pct is not None and corridor_pct < self.min_corridor_pct:
                return ExecResult(status="rejected", detail=(
                    f"强平距离 {corridor_pct:.2f}% 低于门槛 "
                    f"{self.min_corridor_pct}%"))
            # 仓位上限：只对「敞口变大」的下单生效
            new_qty = cur_qty + req.qty * req.side.sign
            if abs(new_qty) > abs(cur_qty):
                new_notional = abs(new_qty) * ref_price
                if not within_cap(new_notional, self.caps.venue.get(venue_id, 0.0)):
                    return ExecResult(status="rejected",
                                      detail=f"超过场所仓位上限 {venue_id}")
                if not within_cap(new_notional,
                                  self.caps.strategy.get(strategy, 0.0)):
                    return ExecResult(status="rejected",
                                      detail=f"超过策略仓位上限 {strategy}")
                if self.caps.global_ > 0:
                    after = self._total_notional() + \
                        (new_notional - abs(cur_qty) * ref_price)
                    if not within_cap(after, self.caps.global_):
                        return ExecResult(status="rejected",
                                          detail="超过全局仓位上限")

        try:
            order = adapter.place_order(req)
        except (UnknownOutcomeError, TimeoutError, ConnectionError) as e:
            # 结果未知：不重发，先对账，把真实持仓交回调用方
            pos = self.reconcile(venue_id, req.symbol)
            return ExecResult(status="unknown", detail=str(e), position=pos)

        if order.order_id:
            self.registry.register_order(order.order_id, key, strategy)
        filled = order.filled_qty
        fill_price = 0.0
        if filled > 0:
            if order.price is not None:
                fill_price = order.price
            elif req.price is not None:
                fill_price = req.price
            else:
                fill_price = book.ask if req.side is Side.BUY else book.bid
            self.ledger.record(Fill(
                venue=venue_id, account=adapter.account, symbol=req.symbol,
                strategy=strategy, side=req.side.value, qty=filled,
                price=fill_price, fee=0.0, order_id=order.order_id))
        if order.status in ("filled", "canceled", "rejected"):
            self.registry.unregister_order(order.order_id)
            status = order.status if filled <= 0 else (
                "filled" if filled >= req.qty else "partial")
        elif filled >= req.qty:
            status = "filled"
        elif filled > 0:
            status = "partial"
        else:
            status = "open"
        return ExecResult(status=status, order=order, filled_qty=filled,
                          fill_price=fill_price)

    # ----- 撤单（只能撤自己的） -----
    def cancel(self, strategy: str, venue_id: str, order_id: str) -> bool:
        if not self.registry.owns_order(order_id, strategy):
            from .registry import ConflictError
            raise ConflictError(f"订单 {order_id} 不属于策略 {strategy}，拒绝撤销")
        ok = self.venues[venue_id].cancel_order(order_id)
        if ok:
            self.registry.unregister_order(order_id)
        return ok

    def cancel_all_own(self, strategy: str, venue_id: str, symbol: str) -> int:
        n = 0
        for o in list(self.venues[venue_id].open_orders(symbol)):
            if self.registry.owns_order(o.order_id, strategy):
                if self.cancel(strategy, venue_id, o.order_id):
                    n += 1
        return n

    # ----- 平仓（安全动作） -----
    def close_position(self, strategy: str, venue_id: str, symbol: str,
                       qty: float | None = None) -> ExecResult | None:
        """市价只减仓平掉自有持仓（可指定部分数量）。无持仓返回 None。"""
        pos = self.position(venue_id, symbol)
        if pos is None or pos.qty == 0:
            return None
        close_qty = abs(pos.qty) if qty is None else min(abs(pos.qty), qty)
        if close_qty <= 0:
            return None
        side = Side.SELL if pos.qty > 0 else Side.BUY
        req = OrderRequest(symbol=symbol, side=side, qty=close_qty,
                           price=None, reduce_only=True)
        return self.place(strategy, venue_id, req, intent="close")
