"""网格策略（骨架）：参数 + 纯函数层级/铺单计划。

逻辑移植自 WGALL（MIT）的 grid.js/bot.js，用 Python 重写。
本批只做纯函数与决策骨架，不接实盘。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..core.venue import OrderRequest, Side


@dataclass
class GridParams:
    lower: float
    upper: float
    grid_count: int            # 格数 >= 2
    qty_per_grid: float        # 每格数量（币）
    mode: str = "neutral"      # neutral / long / short
    leverage: float = 1.0
    out_of_range_action: str = "close"  # close / recover

    def __post_init__(self) -> None:
        if not self.lower < self.upper:
            raise ValueError("lower 必须小于 upper")
        if self.grid_count < 2:
            raise ValueError("grid_count 至少为 2")
        if self.qty_per_grid <= 0:
            raise ValueError("qty_per_grid 必须大于 0")
        if self.mode not in ("neutral", "long", "short"):
            raise ValueError(f"未知网格模式：{self.mode}")
        if self.out_of_range_action not in ("close", "recover"):
            raise ValueError(f"未知区间外动作：{self.out_of_range_action}")


def grid_step(p: GridParams) -> float:
    """单格价差。"""
    return (p.upper - p.lower) / p.grid_count


def grid_levels(p: GridParams) -> list[float]:
    """全部价格层级，共 grid_count + 1 个。"""
    step = grid_step(p)
    return [p.lower + step * i for i in range(p.grid_count + 1)]


def rung_gross_profit(p: GridParams) -> float:
    """单格理论毛利（不含手续费/资金费/滑点）。"""
    return grid_step(p) * p.qty_per_grid


def estimated_notional(p: GridParams) -> float:
    """估算名义敞口：格数 × 每格量 × 区间中点。"""
    return p.grid_count * p.qty_per_grid * (p.lower + p.upper) / 2


def estimated_margin(p: GridParams) -> float:
    return estimated_notional(p) / max(p.leverage, 1e-9)


@dataclass
class PlannedOrder:
    price: float
    side: Side
    qty: float
    reduce_only: bool = False


def plan_initial_orders(p: GridParams, current_price: float) -> list[PlannedOrder]:
    """初始铺单计划（纯函数）。

    - neutral：现价下方挂买、上方挂卖，均为开仓单
    - long：只在现价下方铺买（开仓）；止盈卖单在成交后动态挂
    - short：只在现价上方铺卖（开仓）；止盈买单在成交后动态挂
    """
    levels = grid_levels(p)
    out: list[PlannedOrder] = []
    for lv in levels:
        if lv < current_price and p.mode in ("neutral", "long"):
            out.append(PlannedOrder(price=lv, side=Side.BUY, qty=p.qty_per_grid))
        elif lv > current_price and p.mode in ("neutral", "short"):
            out.append(PlannedOrder(price=lv, side=Side.SELL, qty=p.qty_per_grid))
    return out


def counter_order(p: GridParams, filled_price: float, filled_side: Side) -> PlannedOrder:
    """成交后相邻格的反向单：买成交→上一格挂卖（止盈），卖成交→下一格挂买。"""
    step = grid_step(p)
    if filled_side is Side.BUY:
        return PlannedOrder(price=filled_price + step, side=Side.SELL,
                            qty=p.qty_per_grid, reduce_only=(p.mode == "long"))
    return PlannedOrder(price=filled_price - step, side=Side.BUY,
                        qty=p.qty_per_grid, reduce_only=(p.mode == "short"))


def out_of_range(p: GridParams, price: float) -> bool:
    return price < p.lower or price > p.upper


class GridStrategy:
    """网格执行状态机（移植 WGALL bot.js 的状态机规则）。

    - start：claim (venue, symbol) 后按 plan_initial_orders 铺限价单
    - tick：轮询自有订单成交 → 按 counter_order 在相邻格挂反向单
    - 区间外：close=撤自有单+市价平仓+停机；recover=撤开仓单，
      只挂 reduce-only 回收阶梯，不加仓、不是止损
    - 补格：只补空缺层级，且必须先 request_refill() 人工触发，
      绝不自动无限补（防单边持续加仓）
    - 停机信号（allow_open=False）：撤单+平仓+停机（安全动作优先）
    """

    name = "grid"

    def __init__(self, params: GridParams, executor=None, venue_id: str = "",
                 symbol: str = "BTC") -> None:
        self.params = params
        self.executor = executor
        self.venue_id = venue_id
        self.symbol = symbol
        self.started = False
        self.stopped = False
        self.recovering = False
        self._orders: dict[str, PlannedOrder] = {}   # 自有挂单 oid -> 计划
        self._recovery_orders: set[str] = set()
        self._refill_requested = False
        self.last_error: str = ""

    # ----- 生命周期 -----
    def start(self, now: float = 0.0) -> None:
        if self.executor is None:
            raise RuntimeError("GridStrategy 未注入 executor（骨架模式）")
        self.executor.claim(self.name, self.venue_id, self.symbol)
        self.started = True
        self.stopped = False
        price = self.executor.venue(self.venue_id).book(self.symbol).mid
        for po in plan_initial_orders(self.params, price):
            self._place(po)

    def stop(self, close: bool = True) -> None:
        if self.executor is None:
            return
        self.executor.cancel_all_own(self.name, self.venue_id, self.symbol)
        self._orders.clear()
        self._recovery_orders.clear()
        if close:
            self.executor.close_position(self.name, self.venue_id, self.symbol)
        self.stopped = True
        if self.executor.position(self.venue_id, self.symbol) is None:
            self.executor.release(self.name, self.venue_id, self.symbol)

    def request_refill(self) -> None:
        """人工补格触发：下一 tick 只补一次空缺层级。"""
        self._refill_requested = True

    # ----- 下单/成交处理 -----
    def _level_taken(self, price: float) -> bool:
        return any(abs(po.price - price) < 1e-9 for po in self._orders.values())

    def _place(self, po: PlannedOrder, recovery: bool = False) -> None:
        req = OrderRequest(symbol=self.symbol, side=po.side, qty=po.qty,
                           price=po.price, reduce_only=po.reduce_only,
                           post_only=True)
        res = self.executor.place(self.name, self.venue_id, req, intent="open")
        if res.order is None:
            self.last_error = res.detail or res.status
            return
        if res.status == "filled":
            self._on_fill(po)
            return
        self._orders[res.order.order_id] = po
        if recovery:
            self._recovery_orders.add(res.order.order_id)

    def _on_fill(self, po: PlannedOrder) -> None:
        if self.recovering:
            # 回收单成交：仓位已减，阶梯维护在 tick 里做
            return
        counter = counter_order(self.params, po.price, po.side)
        if not (self.params.lower - 1e-9 <= counter.price
                <= self.params.upper + 1e-9):
            return  # 反向单越出区间不再挂
        if self._level_taken(counter.price):
            return
        self._place(counter)

    def _process_fills(self) -> None:
        venue = self.executor.venue(self.venue_id)
        for oid, po in list(self._orders.items()):
            o = venue.poll_order(oid)
            if o is None:
                continue
            if o.status == "filled":
                del self._orders[oid]
                self._recovery_orders.discard(oid)
                # 挂单后成交的量在下单时未记账，这里补记（按实际成交量）
                self.executor.record_fill(
                    self.name, self.venue_id, self.symbol, po.side,
                    o.filled_qty, o.price if o.price is not None else po.price,
                    order_id=oid)
                self._on_fill(po)
            elif o.status in ("canceled", "rejected"):
                del self._orders[oid]
                self._recovery_orders.discard(oid)

    # ----- 区间外 -----
    def _enter_recovery(self) -> None:
        self.recovering = True
        # 撤掉全部开仓挂单，只保留/新挂 reduce-only 回收阶梯
        for oid, po in list(self._orders.items()):
            if not po.reduce_only:
                if self.executor.cancel(self.name, self.venue_id, oid):
                    del self._orders[oid]

    def _maintain_recovery(self, price: float) -> None:
        pos = self.executor.position(self.venue_id, self.symbol)
        if pos is None or pos.qty == 0:
            # 已回收完毕：撤残余阶梯并停机释放
            self.executor.cancel_all_own(self.name, self.venue_id, self.symbol)
            self._orders.clear()
            self._recovery_orders.clear()
            self.recovering = False
            self.stopped = True
            self.executor.release(self.name, self.venue_id, self.symbol)
            return
        # 已挂回收量之外，按层级补足 reduce-only 阶梯
        open_recovery_qty = sum(
            po.qty for oid, po in self._orders.items()
            if oid in self._recovery_orders)
        remaining = abs(pos.qty) - open_recovery_qty
        if remaining <= 1e-12:
            return
        levels = grid_levels(self.params)
        if pos.qty > 0:  # 多头：上方层级挂卖回收
            cands = [lv for lv in levels if lv > price]
            side = Side.SELL
        else:            # 空头：下方层级挂买回收
            cands = [lv for lv in levels if lv < price]
            side = Side.BUY
        for lv in cands:
            if remaining <= 1e-12:
                break
            if self._level_taken(lv):
                continue
            q = min(self.params.qty_per_grid, remaining)
            self._place(PlannedOrder(price=lv, side=side, qty=q,
                                     reduce_only=True), recovery=True)
            remaining -= q

    def _exit_recovery(self, price: float) -> None:
        for oid in list(self._recovery_orders):
            if self.executor.cancel(self.name, self.venue_id, oid):
                self._orders.pop(oid, None)
        self._recovery_orders.clear()
        self.recovering = False
        # 回到区间：补回缺失的初始层级
        for po in plan_initial_orders(self.params, price):
            if not self._level_taken(po.price):
                self._place(po)

    # ----- 主循环 -----
    def tick(self, now: float = 0.0, allow_open: bool = True) -> None:
        if self.executor is None or not self.started or self.stopped:
            return
        if not allow_open:
            self.stop(close=True)
            return
        self._process_fills()
        if self.stopped:
            return
        price = self.executor.venue(self.venue_id).book(self.symbol).mid
        if out_of_range(self.params, price):
            if self.params.out_of_range_action == "close":
                self.stop(close=True)
                return
            if not self.recovering:
                self._enter_recovery()
            self._maintain_recovery(price)
            return
        if self.recovering:
            self._exit_recovery(price)
            return
        if self._refill_requested:
            self._refill_requested = False
            for po in plan_initial_orders(self.params, price):
                if not self._level_taken(po.price):
                    self._place(po)

    def decide(self, ctx) -> list:
        return []
