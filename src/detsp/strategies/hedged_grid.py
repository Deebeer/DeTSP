"""中性网格策略：参数 + 纯函数层级/铺单/对冲计划 + 执行状态机。

来源与重组关系：
- 网格腿的铺单/补单/回收语义来自 WGALL-ZJ005（ZAIJIN88，MIT）的
  网格逻辑（原普通网格 grid.py 的已验证语义在此重组沿用）；
- 对冲腿来自 Parallax Hedge（ZAIJIN88/Arcus-Entropy-Lighter-，
  MIT）的双腿对锁：每个 tick 读网格腿真实净持仓，对冲腿目标 =
  −网格净持仓，差额超死区时用 taker 市价调平，使组合方向中性。

执行约束与其他策略一致：全部下单走统一执行层（Executor），
归属 owner = 任务名；双腿都做强平距离监控；启动先对账。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..core.venue import OrderRequest, Side


@dataclass
class HedgedGridParams:
    grid_venue: str                 # 网格腿场所
    hedge_venue: str                # 对冲腿场所
    symbol: str
    lower_price: float              # 网格区间下限
    upper_price: float              # 网格区间上限
    grid_count: int                 # 格数 >= 2
    qty_per_grid: float             # 每格数量（币）
    mode: str = "long_short"        # long / short / long_short（双向）
    out_of_range_action: str = "close"  # close 平双腿停机 /
    # reduce_only 只挂只减仓回收阶梯（不加仓、不是止损）
    leverage_grid: float = 1.0      # 网格腿杠杆（与账户实际设置一致）
    leverage_hedge: float = 1.0     # 对冲腿杠杆（同上）
    hedge_deadband_qty: float = 0.0  # 对冲死区（币）：净仓位绝对值
    # 小于它时不对冲，避免为碎量频繁下单磨损
    max_unhedged_seconds: float = 0.0  # 裸露超时（秒）：残差超死区
    # 持续超过此时长 → 报警并绕过对冲所最小下单量强制对冲；0=禁用
    takeover_position: bool = False    # 启动发现网格腿已有持仓时是否
    # 接管；默认 False = 登记为未托管、不铺单，等人工处理

    def __post_init__(self) -> None:
        if self.grid_venue == self.hedge_venue:
            raise ValueError("网格腿与对冲腿不能是同一场所")
        if not self.grid_venue or not self.hedge_venue:
            raise ValueError("grid_venue / hedge_venue 必填")
        if not self.lower_price < self.upper_price:
            raise ValueError("lower_price 必须小于 upper_price")
        if self.grid_count < 2:
            raise ValueError("grid_count 至少为 2")
        if self.qty_per_grid <= 0:
            raise ValueError("qty_per_grid 必须大于 0")
        if self.mode not in ("long", "short", "long_short"):
            raise ValueError(f"未知网格模式：{self.mode}")
        if self.out_of_range_action not in ("close", "reduce_only"):
            raise ValueError(
                f"未知区间外动作：{self.out_of_range_action}")
        if self.hedge_deadband_qty < 0:
            raise ValueError("hedge_deadband_qty 不能为负")
        if self.max_unhedged_seconds < 0:
            raise ValueError("max_unhedged_seconds 不能为负")


# ═══════════ 网格纯函数（档位/铺单计划，沿用已验证的网格语义） ═══════════

def grid_step(p: HedgedGridParams) -> float:
    """单格价差。"""
    return (p.upper_price - p.lower_price) / p.grid_count


def grid_levels(p: HedgedGridParams) -> list[float]:
    """全部价格层级，共 grid_count + 1 个。"""
    step = grid_step(p)
    return [p.lower_price + step * i for i in range(p.grid_count + 1)]


def rung_gross_profit(p: HedgedGridParams) -> float:
    """单格理论毛利（不含手续费/资金费/滑点）。"""
    return grid_step(p) * p.qty_per_grid


def estimated_notional(p: HedgedGridParams) -> float:
    """单腿估算名义敞口：格数 × 每格量 × 区间中点。"""
    return p.grid_count * p.qty_per_grid * (p.lower_price + p.upper_price) / 2


def estimated_total_notional(p: HedgedGridParams) -> float:
    """双腿总名义估算（caps 硬校验口径）：两条腿各一份单腿名义。"""
    return estimated_notional(p) * 2


@dataclass
class PlannedOrder:
    price: float
    side: Side
    qty: float
    reduce_only: bool = False


def plan_initial_orders(p: HedgedGridParams,
                        current_price: float) -> list[PlannedOrder]:
    """初始铺单计划（纯函数）。

    - long_short：现价下方挂买、上方挂卖，均为开仓单
    - long：只在现价下方铺买（开仓）；止盈卖单在成交后动态挂
    - short：只在现价上方铺卖（开仓）；止盈买单在成交后动态挂
    """
    levels = grid_levels(p)
    out: list[PlannedOrder] = []
    for lv in levels:
        if lv < current_price and p.mode in ("long_short", "long"):
            out.append(PlannedOrder(price=lv, side=Side.BUY,
                                    qty=p.qty_per_grid))
        elif lv > current_price and p.mode in ("long_short", "short"):
            out.append(PlannedOrder(price=lv, side=Side.SELL,
                                    qty=p.qty_per_grid))
    return out


def counter_order(p: HedgedGridParams, filled_price: float,
                  filled_side: Side) -> PlannedOrder:
    """成交后相邻格的反向单：买成交→上一格挂卖，卖成交→下一格挂买。

    long 模式的卖单、short 模式的买单是止盈，只减仓（reduce_only）。
    """
    step = grid_step(p)
    if filled_side is Side.BUY:
        return PlannedOrder(price=filled_price + step, side=Side.SELL,
                            qty=p.qty_per_grid,
                            reduce_only=(p.mode == "long"))
    return PlannedOrder(price=filled_price - step, side=Side.BUY,
                        qty=p.qty_per_grid,
                        reduce_only=(p.mode == "short"))


def out_of_range(p: HedgedGridParams, price: float) -> bool:
    return price < p.lower_price or price > p.upper_price


# ═══════════ 对冲纯函数 ═══════════

def hedge_target(grid_qty: float) -> float:
    """对冲腿目标持仓 = −网格腿净持仓（组合净 delta 为 0）。"""
    return -grid_qty


def hedge_residual(grid_qty: float, hedge_qty: float) -> float:
    """未对冲残差（带符号）：两腿数量之和，理想为 0。"""
    return grid_qty + hedge_qty


def plan_hedge_adjustment(grid_qty: float, hedge_qty: float,
                          deadband_qty: float, min_qty: float,
                          force: bool = False) -> float:
    """对冲腿本 tick 应交易的带符号数量（纯函数，正=买、负=卖）。

    - 残差绝对值 ≤ 死区 → 0（不对冲）
    - 残差达不到对冲所最小下单量 → 0（等累积），force=True 时绕过
      最小量强制对冲（裸露超时用；场所仍可能拒单，调用方记录）
    """
    residual = hedge_residual(grid_qty, hedge_qty)
    if abs(residual) <= max(deadband_qty, 0.0):
        return 0.0
    delta = -residual
    if not force and min_qty > 0 and abs(delta) < min_qty:
        return 0.0
    return delta


class HedgedGridStrategy:
    """中性网格执行状态机。

    - start：双腿总名义 caps 硬校验 → claim 双腿 → 先撤网格腿该
      币种遗留挂单 → 读双腿持仓对账（见 start 内注释）→ 铺网格单
    - tick：处理网格腿成交（poll + 对账式发现）→ 相邻格挂反向单
      → 对冲腿按净持仓调平 → 双腿强平监控 → 区间外动作
    - 区间外：close=撤双腿单+平双腿停机；reduce_only=网格腿撤开仓
      单、只挂只减仓回收阶梯，对冲腿继续跟随调平
    - 补格：只补空缺层级，必须 request_refill() 人工触发
    - 停机信号（allow_open=False）/ 面板 stop：撤双腿挂单 + 平双腿
    """

    name = "hedged_grid"
    # 面板停止语义：四策略统一为撤单 + 平仓型
    stop_note = "已停止：双腿挂单已撤、双腿持仓已平"

    def __init__(self, params: HedgedGridParams, executor=None,
                 owner: str | None = None) -> None:
        self.params = params
        self.executor = executor
        # 归属 owner = 任务名（config 的 name）；未给时退回类型名
        self.owner = owner or self.name
        self.started = False
        self.stopped = False
        self.recovering = False
        self.unmanaged_position = False
        self._orders: dict[str, PlannedOrder] = {}   # 网格腿自有挂单
        self._recovery_orders: set[str] = set()
        self._pos_baseline: dict[str, float] = {}    # 对账式成交发现基线
        self._refill_requested = False
        self._unhedged_since: float | None = None    # 残差超死区起始时刻
        self.hedge_forced_count = 0                  # 裸露超时强制对冲次数
        self.last_error: str = ""
        self.recovery_notes: list[str] = []          # 启动对账事件

    # ----- 基础 -----
    @property
    def symbol(self) -> str:
        return self.params.symbol

    def _pos(self, venue_id: str) -> float:
        pos = self.executor.position(venue_id, self.params.symbol)
        return pos.qty if pos else 0.0

    def _grid_pos(self) -> float:
        return self._pos(self.params.grid_venue)

    def _hedge_pos(self) -> float:
        return self._pos(self.params.hedge_venue)

    def _hedge_min_qty(self) -> float:
        for m in self.executor.venue(self.params.hedge_venue).markets():
            if m.symbol == self.params.symbol:
                return m.min_qty
        return 0.0

    # ----- 生命周期 -----
    def start(self, now: float = 0.0) -> None:
        if self.executor is None:
            raise RuntimeError("HedgedGridStrategy 未注入 executor（骨架模式）")
        p = self.params
        # caps 硬校验：双腿总名义 vs 全局/策略层；单腿名义 vs 各场所层
        caps = self.executor.caps
        total = estimated_total_notional(p)
        single = estimated_notional(p)
        checks = (("全局", caps.global_, total),
                  (f"策略 {self.owner}", caps.strategy.get(self.owner, 0.0),
                   total),
                  (f"场所 {p.grid_venue}", caps.venue.get(p.grid_venue, 0.0),
                   single),
                  (f"场所 {p.hedge_venue}", caps.venue.get(p.hedge_venue, 0.0),
                   single))
        for label, cap, est in checks:
            if cap > 0 and est > cap:
                raise RuntimeError(
                    f"中性网格名义估算 {est:,.0f} USD 超过{label}上限 "
                    f"{cap:,.0f} USD，拒绝启动（caps 硬校验）")
        self.executor.claim(self.owner, p.grid_venue, p.symbol)
        self.executor.claim(self.owner, p.hedge_venue, p.symbol)
        # 启动对账：先撤双腿该币种的遗留挂单（非本进程登记的），
        # 防重启后新旧两套单叠加（对冲腿正常只有市价单，顺带清理）
        for vid in (p.grid_venue, p.hedge_venue):
            cancelled = self.executor.cancel_unregistered(vid, p.symbol)
            if cancelled:
                self.recovery_notes.append(
                    f"启动对账：{vid} 撤销遗留挂单 {cancelled} 张")
        grid_qty = self._grid_pos()
        hedge_qty = self._hedge_pos()
        if grid_qty != 0 and not p.takeover_position:
            # 网格腿已有持仓登记为未托管：不铺单、不动对冲腿，
            # 等人工处理或开启 takeover_position 后重启接管
            self.unmanaged_position = True
            self.recovery_notes.append(
                f"启动对账：网格腿发现未托管持仓 {grid_qty}，未开启接管，"
                f"暂停铺单（对冲腿持仓 {hedge_qty} 一并保留不动）")
            self.started = True
            self.stopped = False
            return
        if grid_qty != 0:
            self.recovery_notes.append(
                f"启动对账：接管网格腿已有持仓 {grid_qty}，对冲腿"
                f"（现 {hedge_qty}）将按目标 {hedge_target(grid_qty)} "
                f"在 tick 中自动调平")
        elif hedge_qty != 0:
            self.recovery_notes.append(
                f"启动对账：网格腿空仓但对冲腿残留 {hedge_qty}，"
                f"将在 tick 中调平到 0")
        self.started = True
        self.stopped = False
        price = self.executor.venue(p.grid_venue).book(p.symbol).mid
        for po in plan_initial_orders(p, price):
            self._place(po)

    def stop(self, close: bool = True) -> None:
        """停止：撤双腿自有挂单 + 平双腿持仓（面板停止与停机共用）。"""
        if self.executor is None:
            return
        p = self.params
        self.executor.cancel_all_own(self.owner, p.grid_venue, p.symbol)
        self.executor.cancel_all_own(self.owner, p.hedge_venue, p.symbol)
        self._orders.clear()
        self._recovery_orders.clear()
        self._pos_baseline.clear()
        if close:
            self.executor.close_position(self.owner, p.grid_venue, p.symbol)
            self.executor.close_position(self.owner, p.hedge_venue, p.symbol)
        self.stopped = True
        if self.executor.position(p.grid_venue, p.symbol) is None and \
                self.executor.position(p.hedge_venue, p.symbol) is None:
            self.executor.release(self.owner, p.grid_venue, p.symbol)
            self.executor.release(self.owner, p.hedge_venue, p.symbol)

    def request_refill(self) -> None:
        """人工补格触发：下一 tick 只补一次空缺层级。"""
        self._refill_requested = True

    # ----- 网格腿下单/成交处理 -----
    def _level_taken(self, price: float) -> bool:
        return any(abs(po.price - price) < 1e-9 for po in self._orders.values())

    def _place(self, po: PlannedOrder, recovery: bool = False) -> None:
        p = self.params
        req = OrderRequest(symbol=p.symbol, side=po.side, qty=po.qty,
                           price=po.price, reduce_only=po.reduce_only,
                           post_only=True)
        baseline = self._grid_pos()
        if po.reduce_only:
            # 减仓单走 close 意图：停机时也允许，不查 caps/强平距离
            res = self.executor.place(self.owner, p.grid_venue, req,
                                      intent="close")
        else:
            corridor = self.executor.estimate_open_corridor(
                p.leverage_grid,
                self.executor.market_mmr(p.grid_venue, p.symbol))
            res = self.executor.place(self.owner, p.grid_venue, req,
                                      intent="open", corridor_pct=corridor)
        if res.order is None:
            self.last_error = res.detail or res.status
            return
        if res.status == "filled":
            self._on_fill(po)
            return
        self._orders[res.order.order_id] = po
        self._pos_baseline[res.order.order_id] = baseline
        if recovery:
            self._recovery_orders.add(res.order.order_id)

    def _on_fill(self, po: PlannedOrder) -> None:
        if self.recovering:
            return  # 回收单成交：阶梯维护在 tick 里做
        counter = counter_order(self.params, po.price, po.side)
        if not (self.params.lower_price - 1e-9 <= counter.price
                <= self.params.upper_price + 1e-9):
            return  # 反向单越出区间不再挂
        if self._level_taken(counter.price):
            return
        self._place(counter)

    def _process_fills(self) -> None:
        venue = self.executor.venue(self.params.grid_venue)
        for oid, po in list(self._orders.items()):
            o = venue.poll_order(oid)
            if o is None:
                # 对账式发现：场所给不出终态时，「挂单消失 + 持仓差」
                # 推断成交（与原网格同一口径）
                self._reconcile_missing(oid, po)
                continue
            if o.status == "filled":
                del self._orders[oid]
                self._recovery_orders.discard(oid)
                self._pos_baseline.pop(oid, None)
                self.executor.record_fill(
                    self.owner, self.params.grid_venue, self.params.symbol,
                    po.side, o.filled_qty,
                    o.price if o.price is not None else po.price,
                    order_id=oid)
                self._on_fill(po)
            elif o.status in ("canceled", "rejected"):
                del self._orders[oid]
                self._recovery_orders.discard(oid)
                self._pos_baseline.pop(oid, None)

    def _reconcile_missing(self, oid: str, po: PlannedOrder) -> None:
        venue = self.executor.venue(self.params.grid_venue)
        try:
            open_ids = {o.order_id for o in venue.open_orders(self.params.symbol)}
        except Exception:  # noqa: BLE001 — 挂单查询失败：保持跟踪
            return
        if oid in open_ids:
            return
        baseline = self._pos_baseline.get(oid)
        del self._orders[oid]
        self._recovery_orders.discard(oid)
        self._pos_baseline.pop(oid, None)
        if baseline is None:
            return
        delta = (self._grid_pos() - baseline) * po.side.sign
        if delta <= 1e-9:
            return  # 无成交痕迹：订单已撤/失效
        filled = min(delta, po.qty)
        self.executor.record_fill(
            self.owner, self.params.grid_venue, self.params.symbol,
            po.side, filled, po.price, order_id=oid)
        self._on_fill(po)

    # ----- 对冲腿调平 -----
    def _sync_hedge(self, now: float) -> None:
        """对冲腿目标 = −网格净持仓；差额超死区时市价调平。

        残差达不到对冲所最小下单量时等累积；裸露持续超过
        max_unhedged_seconds 时报警并强制对冲（绕过最小量）。
        """
        p = self.params
        grid_qty = self._grid_pos()
        hedge_qty = self._hedge_pos()
        residual = hedge_residual(grid_qty, hedge_qty)
        if abs(residual) <= max(p.hedge_deadband_qty, 0.0):
            self._unhedged_since = None
            return
        if self._unhedged_since is None:
            self._unhedged_since = now
        force = p.max_unhedged_seconds > 0 and \
            now - self._unhedged_since >= p.max_unhedged_seconds
        delta = plan_hedge_adjustment(grid_qty, hedge_qty,
                                      p.hedge_deadband_qty,
                                      self._hedge_min_qty(), force=force)
        if delta == 0.0:
            return  # 未达最小量且未到强制时限：等残差累积
        side = Side.BUY if delta > 0 else Side.SELL
        resulting = hedge_qty + delta
        if abs(resulting) > abs(hedge_qty):
            # 对冲敞口变大：开仓意图，带强平距离估算
            corridor = self.executor.estimate_open_corridor(
                p.leverage_hedge,
                self.executor.market_mmr(p.hedge_venue, p.symbol))
            res = self.executor.place(
                self.owner, p.hedge_venue,
                OrderRequest(symbol=p.symbol, side=side, qty=abs(delta),
                             price=None),
                intent="open", corridor_pct=corridor)
        else:
            # 向目标收敛（减仓）：安全动作，reduce-only
            res = self.executor.place(
                self.owner, p.hedge_venue,
                OrderRequest(symbol=p.symbol, side=side, qty=abs(delta),
                             price=None, reduce_only=True),
                intent="close")
        if force:
            self.hedge_forced_count += 1
            self.last_error = (
                f"对冲裸露超时（残差 {residual}，已持续 "
                f"{now - self._unhedged_since:.0f}s），强制对冲 "
                f"{delta}，结果 {res.status}")
            self._unhedged_since = None
        if res.status in ("rejected", "unknown"):
            self.last_error = (f"对冲调平失败：{res.detail or res.status}"
                               f"（残差 {residual} 未消除）")

    # ----- 区间外（reduce_only 回收） -----
    def _enter_recovery(self) -> None:
        self.recovering = True
        for oid, po in list(self._orders.items()):
            if not po.reduce_only:
                if self.executor.cancel(self.owner, self.params.grid_venue, oid):
                    del self._orders[oid]

    def _maintain_recovery(self, price: float) -> None:
        p = self.params
        pos_qty = self._grid_pos()
        if pos_qty == 0:
            self.executor.cancel_all_own(self.owner, p.grid_venue, p.symbol)
            self._orders.clear()
            self._recovery_orders.clear()
            self.recovering = False
            self.stopped = True
            if self.executor.position(p.hedge_venue, p.symbol) is None:
                self.executor.release(self.owner, p.grid_venue, p.symbol)
                self.executor.release(self.owner, p.hedge_venue, p.symbol)
            return
        open_recovery_qty = sum(
            po.qty for oid, po in self._orders.items()
            if oid in self._recovery_orders)
        remaining = abs(pos_qty) - open_recovery_qty
        if remaining <= 1e-12:
            return
        levels = grid_levels(p)
        if pos_qty > 0:  # 多头：上方层级挂卖回收
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
            q = min(p.qty_per_grid, remaining)
            self._place(PlannedOrder(price=lv, side=side, qty=q,
                                     reduce_only=True), recovery=True)
            remaining -= q

    def _exit_recovery(self, price: float) -> None:
        for oid in list(self._recovery_orders):
            if self.executor.cancel(self.owner, self.params.grid_venue, oid):
                self._orders.pop(oid, None)
        self._recovery_orders.clear()
        self.recovering = False
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
        if self.unmanaged_position:
            # 未托管持仓被接管前，网格腿与对冲腿都不自动动作
            return
        p = self.params
        self._process_fills()
        if self.stopped:
            return
        # 对冲调平紧跟成交处理：先把方向残差消掉再谈其他
        self._sync_hedge(now)

        price = self.executor.venue(p.grid_venue).book(p.symbol).mid
        # 双腿强平监控（Executor 公共口径）：任一腿距离跌破门槛 →
        # 双腿全平停机；不可评估 → 暂停网格新开仓（对冲调平照常，
        # 它是降风险动作，已在上面执行完）
        block_new = False
        for vid in (p.grid_venue, p.hedge_venue):
            has_pos, corridor = self.executor.position_corridor(
                vid, p.symbol,
                self.executor.venue(vid).book(p.symbol).mid)
            if not has_pos:
                continue
            if corridor is None:
                block_new = True
                self.last_error = (f"{vid} 强平距离不可评估"
                                   f"（无强平价且杠杆未知），暂停网格新开仓")
            elif corridor < self.executor.min_corridor_pct:
                self.last_error = (f"{vid} 强平距离 {corridor:.2f}% 跌破门槛 "
                                   f"{self.executor.min_corridor_pct}%，双腿平仓停机")
                self.stop(close=True)
                return
        if out_of_range(p, price):
            if p.out_of_range_action == "close":
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
            if block_new:
                return
            for po in plan_initial_orders(p, price):
                if not self._level_taken(po.price):
                    self._place(po)

    def decide(self, ctx) -> list:
        return []
