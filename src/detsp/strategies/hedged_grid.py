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
    max_residual_seconds: float = 0.0  # 残差强制对冲时限（秒）：残差
    # 超死区但低于对冲所最小下单量时，持续超过此时长也强制对冲
    # （自查 L1）；与 max_unhedged_seconds 的区别是它只针对
    # 「量不够最小下单量」这一类残差，0=禁用（保持等累积）
    skip_band_frac: float = 0.25    # 贴价跳档带宽（格距的比例）：
    # 初始铺单跳过距现价小于该比例×格距的档位（WGALL skipBand，
    # 防止现价附近的档位被行情噪声反复触发磨损）
    fee_bps_per_side: float = 5.0   # 单边手续费估算（bps）：启动
    # 经济性检查用——格距毛利 ≤ 往返手续费时告警（WGALL 口径）
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
        if self.max_residual_seconds < 0:
            raise ValueError("max_residual_seconds 不能为负")
        if not 0 <= self.skip_band_frac < 1:
            raise ValueError("skip_band_frac 须在 [0, 1)")
        if self.fee_bps_per_side < 0:
            raise ValueError("fee_bps_per_side 不能为负")
        if self.leverage_grid <= 0 or self.leverage_hedge <= 0:
            raise ValueError("leverage_grid / leverage_hedge 必须大于 0")


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
    - 贴价跳档（WGALL skipBand）：距现价 < skip_band_frac×格距
      的档位不挂——现价附近的档位最容易被噪声反复触发，先跳过，
      等成交后的反向单机制自然把档位铺开
    """
    levels = grid_levels(p)
    band = p.skip_band_frac * grid_step(p)
    out: list[PlannedOrder] = []
    for lv in levels:
        if abs(lv - current_price) < band:
            continue  # 贴价跳档（含现价恰在档位上：原逻辑本就不挂）
        if lv < current_price and p.mode in ("long_short", "long"):
            out.append(PlannedOrder(price=lv, side=Side.BUY,
                                    qty=p.qty_per_grid))
        elif lv > current_price and p.mode in ("long_short", "short"):
            out.append(PlannedOrder(price=lv, side=Side.SELL,
                                    qty=p.qty_per_grid))
    return out


def counter_order(p: HedgedGridParams, filled_price: float,
                  filled_side: Side, qty: float | None = None) -> PlannedOrder:
    """成交后相邻格的反向单：买成交→上一格挂卖，卖成交→下一格挂买。

    数量按实际成交量镜像（WGALL 口径）：部分成交只挂已成部分
    的反向单，qty=None 时用整格量（保持纯函数旧调用兼容）。
    long 模式的卖单、short 模式的买单是止盈，只减仓（reduce_only）。
    """
    step = grid_step(p)
    q = p.qty_per_grid if qty is None else qty
    if filled_side is Side.BUY:
        return PlannedOrder(price=filled_price + step, side=Side.SELL,
                            qty=q,
                            reduce_only=(p.mode == "long"))
    return PlannedOrder(price=filled_price - step, side=Side.BUY,
                        qty=q,
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
        self._recorded: dict[str, float] = {}        # 每单已记账量
        # （增量记账口径，自查 M3：place 回执/poll 中间态/终态/对账
        # 四路都只记增量，互不重复）
        self._cancel_events: list[float] = []        # 撤单风暴统计
        # （WGALL 口径：60 秒内自有单被撤/被拒 ≥5 次 → 暂停补单）
        self._pause_until = -1e18                    # 暂停铺单截止时刻
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
        # 启动行情与保证金预检（WGALL 口径，对照补齐）：
        price = self.executor.venue(p.grid_venue).book(p.symbol).mid
        if not (p.lower_price * 0.5 <= price <= p.upper_price * 2.0):
            raise RuntimeError(
                f"启动价格 sanity 失败：现价 {price} 远离网格区间 "
                f"[{p.lower_price}, {p.upper_price}]（允许范围 "
                f"[{p.lower_price * 0.5}, {p.upper_price * 2.0}]），"
                f"拒绝启动——多半是标的或区间配错")
        for vid, lev in ((p.grid_venue, p.leverage_grid),
                         (p.hedge_venue, p.leverage_hedge)):
            need = single / lev
            avail = float(self.executor.venue(vid).balance().get(
                "available", 0.0))
            if need > avail:
                raise RuntimeError(
                    f"保证金预检失败：{vid} 单腿估算名义 {single:,.0f} "
                    f"USD ÷ 杠杆 {lev} 需保证金 {need:,.0f}，超过可用 "
                    f"{avail:,.0f}，拒绝启动")
            if avail > 0 and need > 0.8 * avail:
                self.recovery_notes.append(
                    f"保证金预检告警：{vid} 所需保证金 {need:,.0f} 已超"
                    f"过可用 {avail:,.0f} 的 80%，成交推进后可能不足")
        mid = (p.lower_price + p.upper_price) / 2
        if grid_step(p) <= mid * 2 * p.fee_bps_per_side / 10_000:
            self.recovery_notes.append(
                f"经济性告警：格距 {grid_step(p)} 不大于往返手续费估算 "
                f"{mid * 2 * p.fee_bps_per_side / 10_000:.4f}"
                f"（单边 {p.fee_bps_per_side} bps），每格毛利将被手续"
                f"费吃掉，请调大区间/格数或核对 fee_bps_per_side")
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
        for po in plan_initial_orders(p, price):
            self._place(po, now=now)

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
        self._recorded.clear()
        if close:
            self.executor.close_position(self.owner, p.grid_venue, p.symbol)
            self.executor.close_position(self.owner, p.hedge_venue, p.symbol)
        self.stopped = True
        if close:
            # 归属显式释放（自查 L4）：不依赖 position() 恰好返回
            # None——某些场所平仓后仍返回零量持仓行，旧逻辑会
            # 因此漏释放、归属被永久占用。平仓已执行即释放
            for vid in (p.grid_venue, p.hedge_venue):
                try:
                    self.executor.release(self.owner, vid, p.symbol)
                except Exception:  # noqa: BLE001 — 未 claim 时不挡停机
                    pass

    def request_refill(self) -> None:
        """人工补格触发：下一 tick 只补一次空缺层级。"""
        self._refill_requested = True

    # ----- 网格腿下单/成交处理 -----
    def _level_taken(self, price: float) -> bool:
        return any(abs(po.price - price) < 1e-9 for po in self._orders.values())

    def _note_cancel_storm(self, now: float) -> None:
        """撤单风暴统计（WGALL 口径）：60 秒内自有单被撤/被拒
        ≥5 次 → 暂停开仓类铺单 60 秒（reduce-only 回收单不拦，
        它是降风险动作）。"""
        self._cancel_events = [t for t in self._cancel_events
                               if now - t <= 60.0]
        self._cancel_events.append(now)
        if len(self._cancel_events) >= 5:
            self._pause_until = now + 60.0
            self.last_error = ("撤单风暴保护：60 秒内自有单被撤/被拒 "
                               "≥5 次，暂停铺单 60 秒")

    def _place(self, po: PlannedOrder, recovery: bool = False,
               now: float = 0.0) -> None:
        p = self.params
        if not po.reduce_only and now < self._pause_until:
            return  # 撤单风暴暂停期内不开新仓类单
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
            if res.status == "unknown":
                # 下单结果未知（自查 M1）：订单可能已挂上。用合成
                # 号登记待确认单，_process_fills 先在挂单列表里按
                # 方向/价格/数量认领真实号，认领不到再走对账式
                # 发现——不因没拿到回执就当没下过（会漏成交）或
                # 重复补单（会双倍敞口）
                oid = f"unknown:{p.grid_venue}:{po.price}:{po.side.value}"
                self._orders[oid] = po
                self._pos_baseline[oid] = baseline
                self._recorded[oid] = 0.0
                if recovery:
                    self._recovery_orders.add(oid)
                self.last_error = ("网格下单结果未知，已登记待确认单"
                                   "等待认领/对账定案")
            else:
                self.last_error = res.detail or res.status
            return
        if res.status == "filled":
            self._on_fill(po, res.filled_qty or po.qty, now)
            return
        self._orders[res.order.order_id] = po
        self._pos_baseline[res.order.order_id] = baseline
        # place 回执已记账的量（如即时部分成交）登记为已记，
        # 后续 poll/对账只记增量（自查 M3）
        self._recorded[res.order.order_id] = res.filled_qty
        if recovery:
            self._recovery_orders.add(res.order.order_id)

    def _on_fill(self, po: PlannedOrder, filled_qty: float,
                 now: float = 0.0) -> None:
        if self.recovering:
            return  # 回收单成交：阶梯维护在 tick 里做
        # 反向单数量按实际成交量镜像（WGALL 口径）：部分成交
        # 只挂已成部分的反向单，不按整格量超挂
        counter = counter_order(self.params, po.price, po.side,
                                qty=filled_qty)
        if not (self.params.lower_price - 1e-9 <= counter.price
                <= self.params.upper_price + 1e-9):
            return  # 反向单越出区间不再挂
        if self._level_taken(counter.price):
            return
        self._place(counter, now=now)

    def _record_inc(self, oid: str, po: PlannedOrder, total: float,
                    price: float) -> None:
        """增量记账（自查 M3）：total 为该单累计成交量，只记
        超过已记账的部分——place 回执、poll 中间/终态、对账推断
        三路共用同一口径，互不重复计数。"""
        inc = total - self._recorded.get(oid, 0.0)
        if inc > 1e-9:
            self.executor.record_fill(
                self.owner, self.params.grid_venue, self.params.symbol,
                po.side, inc, price, order_id=oid)
            self._recorded[oid] = self._recorded.get(oid, 0.0) + inc

    def _drop_order(self, oid: str) -> None:
        self._orders.pop(oid, None)
        self._recovery_orders.discard(oid)
        self._pos_baseline.pop(oid, None)
        self._recorded.pop(oid, None)

    def _adopt_unknown(self, oid: str, po: PlannedOrder) -> str | None:
        """待确认单认领：在挂单列表里按方向/价格/数量找本策略
        刚挂的那张单，找到就把跟踪键换成真实订单号。"""
        venue = self.executor.venue(self.params.grid_venue)
        try:
            orders = venue.open_orders(self.params.symbol)
        except Exception:  # noqa: BLE001 — 查询失败下轮再认领
            return None
        for o in orders:
            if o.side != po.side:
                continue
            if o.price is None or abs(o.price - po.price) > 1e-9:
                continue
            if abs(o.qty - po.qty) > max(1e-9, po.qty * 1e-6):
                continue
            self._orders[o.order_id] = self._orders.pop(oid)
            if oid in self._pos_baseline:
                self._pos_baseline[o.order_id] = \
                    self._pos_baseline.pop(oid)
            if oid in self._recorded:
                self._recorded[o.order_id] = self._recorded.pop(oid)
            if oid in self._recovery_orders:
                self._recovery_orders.discard(oid)
                self._recovery_orders.add(o.order_id)
            return o.order_id
        return None

    def _process_fills(self, now: float = 0.0) -> None:
        venue = self.executor.venue(self.params.grid_venue)
        for oid, po in list(self._orders.items()):
            if oid.startswith("unknown:"):
                adopted = self._adopt_unknown(oid, po)
                if adopted is not None:
                    oid = adopted
                # 认领不到也继续走 poll/对账：poll 对合成号返回
                # None → 对账式按持仓差定案（没挂上则差为 0 丢弃）
            o = venue.poll_order(oid)
            if o is None:
                # 对账式发现：场所给不出终态时，「挂单消失 + 持仓差」
                # 推断成交（与原网格同一口径）
                self._reconcile_missing(oid, po, now)
                continue
            px = o.price if o.price is not None else po.price
            if o.status == "open":
                # 中间态部分成交：只记增量，不提前挂反向单
                if o.filled_qty > self._recorded.get(oid, 0.0):
                    self._record_inc(oid, po, o.filled_qty, px)
            elif o.status == "filled":
                final = o.filled_qty if o.filled_qty > 0 else po.qty
                self._record_inc(oid, po, final, px)
                self._drop_order(oid)
                self._on_fill(po, final, now)
            elif o.status in ("canceled", "rejected"):
                # 带部分成交的撤单：已成部分照样记账 + 挂反向单
                if o.filled_qty > 1e-9:
                    self._record_inc(oid, po, o.filled_qty, px)
                    self._drop_order(oid)
                    self._on_fill(po, o.filled_qty, now)
                else:
                    self._drop_order(oid)
                self._note_cancel_storm(now)

    def _reconcile_missing(self, oid: str, po: PlannedOrder,
                           now: float = 0.0) -> None:
        venue = self.executor.venue(self.params.grid_venue)
        try:
            open_ids = {o.order_id for o in venue.open_orders(self.params.symbol)}
        except Exception:  # noqa: BLE001 — 挂单查询失败：保持跟踪
            return
        if oid in open_ids:
            return
        baseline = self._pos_baseline.get(oid)
        if baseline is None:
            self._drop_order(oid)
            return
        delta = (self._grid_pos() - baseline) * po.side.sign
        filled_total = min(max(delta, 0.0), po.qty)
        if filled_total > 1e-9:
            self._record_inc(oid, po, filled_total, po.price)
            self._drop_order(oid)
            self._on_fill(po, filled_total, now)
        else:
            self._drop_order(oid)  # 无成交痕迹：订单已撤/失效

    # ----- 对冲腿调平 -----
    def _sync_hedge(self, now: float) -> None:
        """对冲腿目标 = −网格净持仓；差额超死区时市价调平。

        残差达不到对冲所最小下单量时等累积；裸露持续超过
        max_unhedged_seconds 时报警并强制对冲（绕过最小量）；
        残差低于最小量但持续超过 max_residual_seconds 时同样
        强制对冲（自查 L1，独立开关）。
        两所价格偏离 >3% 时本 tick 跳过（疑似行情异常或同名
        不同标的，不拿坏价格调平）；上一笔调平单尚待结算定案
        时不叠加新调平（防按旧持仓重复下单）。
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
        if delta == 0.0 and p.max_residual_seconds > 0 and \
                now - self._unhedged_since >= p.max_residual_seconds:
            # 残差超死区但低于最小下单量、持续超时：强制对冲
            delta = plan_hedge_adjustment(grid_qty, hedge_qty,
                                          p.hedge_deadband_qty,
                                          self._hedge_min_qty(), force=True)
            if delta != 0.0:
                force = True
        if delta == 0.0:
            return  # 未达最小量且未到强制时限：等残差累积
        if self.executor.has_pending_fill(p.hedge_venue, p.symbol):
            return  # 上一笔调平单未结算定案：不按旧持仓叠加下单
        try:
            g_mid = self.executor.venue(p.grid_venue).book(p.symbol).mid
            h_mid = self.executor.venue(p.hedge_venue).book(p.symbol).mid
        except Exception:  # noqa: BLE001 — 行情不可得：本 tick 不调平
            return
        if g_mid > 0 and h_mid > 0 and abs(g_mid / h_mid - 1.0) > 0.03:
            self.last_error = (
                f"对冲调平跳过：两所价格偏离 "
                f"{abs(g_mid / h_mid - 1.0):.2%} 超过 3%"
                f"（疑似行情异常/同名不同标的）")
            return
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
                    self._recorded.pop(oid, None)
                    self._pos_baseline.pop(oid, None)

    def _recovery_rungs(self, price: float) -> list[float]:
        """回收阶梯落点（WGALL `_placeRecoveryLadder` 口径）：

        现价在区间外时，档位先落在「现价 ↔ 被破边界」之间
        （区间外、被破边界外侧一格起），价格小幅反弹/回落、
        尚未回到区间内就开始分批减仓；再沿价格返回方向逐格
        延伸，直到能覆盖全部剩余持仓（DeTSP 的总量封顶口径，
        REVIEW2 对照中记为改进项）。档位与网格同格距、同
        层级对齐（以 lower_price 为原点延伸）。
        """
        import math
        p = self.params
        step = grid_step(p)
        pos_qty = self._grid_pos()
        rungs: list[float] = []
        cap = p.grid_count * 2 + 2
        if pos_qty > 0:
            # 多头卖出回收：档位自下而上。自现价上方第一格起；
            # 现价冲出上界时先铺上边界↔现价之间的档位（回落
            # 必经之路），再向上延伸
            if price > p.upper_price:
                lv = p.upper_price
                while lv < price and len(rungs) < cap:
                    rungs.append(round(lv, 10))
                    lv += step
                start = lv
            else:
                start = p.lower_price + (
                    math.floor((price - p.lower_price) / step) + 1) * step
            lv = start
            while len(rungs) < cap:
                rungs.append(round(lv, 10))
                lv += step
        elif pos_qty < 0:
            # 空头买入回收：与多头对称，自上而下
            if price < p.lower_price:
                lv = p.lower_price
                while lv > price and len(rungs) < cap:
                    rungs.append(round(lv, 10))
                    lv -= step
                start = lv
            else:
                start = p.lower_price + (
                    math.ceil((price - p.lower_price) / step) - 1) * step
            lv = start
            while len(rungs) < cap:
                rungs.append(round(lv, 10))
                lv -= step
        return rungs

    def _maintain_recovery(self, price: float, now: float = 0.0) -> None:
        p = self.params
        pos_qty = self._grid_pos()
        if pos_qty == 0:
            self.executor.cancel_all_own(self.owner, p.grid_venue, p.symbol)
            self._orders.clear()
            self._recovery_orders.clear()
            self.recovering = False
            self.stopped = True
            if self._pos(p.hedge_venue) == 0:
                # 按数量判零而不是 position() 返回 None（自查 L4
                # 同一口径：零量持仓行不算还有持仓）
                self.executor.release(self.owner, p.grid_venue, p.symbol)
                self.executor.release(self.owner, p.hedge_venue, p.symbol)
            return
        open_recovery_qty = sum(
            po.qty for oid, po in self._orders.items()
            if oid in self._recovery_orders)
        remaining = abs(pos_qty) - open_recovery_qty
        if remaining <= 1e-12:
            return
        # 阶梯落在现价↔边界之间（_recovery_rungs）；方向只由
        # 持仓符号决定：多头挂卖回收、空头挂买回收
        side = Side.SELL if pos_qty > 0 else Side.BUY
        for lv in self._recovery_rungs(price):
            if remaining <= 1e-12:
                break
            if self._level_taken(lv):
                continue
            q = min(p.qty_per_grid, remaining)
            self._place(PlannedOrder(price=lv, side=side, qty=q,
                                     reduce_only=True), recovery=True,
                        now=now)
            remaining -= q

    def _exit_recovery(self, price: float, now: float = 0.0) -> None:
        for oid in list(self._recovery_orders):
            if self.executor.cancel(self.owner, self.params.grid_venue, oid):
                self._orders.pop(oid, None)
                self._recorded.pop(oid, None)
                self._pos_baseline.pop(oid, None)
        self._recovery_orders.clear()
        self.recovering = False
        for po in plan_initial_orders(self.params, price):
            if not self._level_taken(po.price):
                self._place(po, now=now)

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
        self._process_fills(now)
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
            self._maintain_recovery(price, now)
            return
        if self.recovering:
            self._exit_recovery(price, now)
            return
        if self._refill_requested:
            self._refill_requested = False
            if block_new:
                return
            for po in plan_initial_orders(p, price):
                if not self._level_taken(po.price):
                    self._place(po, now=now)
