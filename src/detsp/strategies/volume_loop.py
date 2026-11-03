"""单所刷量策略：参数 + 纯函数决策 + 执行状态机。

策略语义源自 perp-dex-tools（your-quantguy，非商业许可，本项目为
个人自用、非商业用途，依其许可使用并保留署名，见 LICENSES/ 与
NOTICE）的 trading_bot.py，按 DeTSP 架构重组：
- 单场所、单方向（buy/sell 固定）：在现价附近挂 maker 开仓单；
- 开仓成交后，立即按 take_profit_pct 在成交价上方/下方挂
  reduce-only 止盈平仓单，该批次的退出只靠这张止盈单；
- 未平批次（未成交止盈单）数量达 max_orders 上限后不再开新仓；
- 两笔开仓的间隔随未平批次占比自适应放大（pdt 的 wait 分档）；
- 新开仓前检查 grid_step_pct：候选止盈价与最近一张已有止盈单的
  间距不足则不开（防止盈单挤成一堆）；
- stop_price 触发 → 撤开仓单并退出运行（止盈单与持仓保留在
  交易所，与 pdt 的「停止交易」语义一致）；pause_price 触发 →
  只暂停开新仓，止盈单照常等待成交。

⚠ 无止损（原始语义，务必知晓）：本策略**没有止损**——价格反向
走远时，仓位只能靠止盈单在价格回到止盈位时退出，或人工/全局
停机处理。唯一的自动保护是全局强平距离监控（跌破门槛撤单平仓
停机）与 caps 上限。因此 live 模式默认强制要求至少配置一层
caps（全局/场所/策略任一），未配置时 start() 直接拒绝启动。

执行约束与其它策略一致：全部下单走统一执行层（Executor），
归属 owner = 任务名；启动先对账（撤遗留挂单、已有持仓未托管）；
全局停机（allow_open=False）时撤单 + 市价平仓（安全动作优先）。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..core.venue import OrderRequest, Side


@dataclass
class VolumeParams:
    qty: float                          # 每笔开仓数量（币）
    take_profit_pct: float              # 止盈幅度（%）：买入批次在
    # 成交价 ×(1+tp/100) 挂卖单止盈，卖出批次在 ×(1−tp/100) 挂买单
    direction: str = "buy"              # buy / sell：固定单方向
    max_orders: int = 5                 # 未平批次上限（未成交止盈单数）
    wait_seconds: float = 30.0          # 开仓基础间隔（秒），按占比分档
    grid_step_pct: float = 0.0          # 止盈单最小间距（%），0=不限制
    stop_price: float = 0.0             # 退出价，0=禁用：buy 看 ask、
    # sell 看 bid，触及后撤开仓单并退出（止盈单/持仓保留）
    pause_price: float = 0.0            # 暂停价，0=禁用：触及只暂停开仓
    leverage: float = 1.0
    open_timeout_seconds: float = 60.0  # 开仓 maker 单超时撤单（部分
    # 成交按最终量挂止盈单），<=0 表示不超时

    def __post_init__(self) -> None:
        if self.qty <= 0:
            raise ValueError("qty 必须大于 0")
        if self.take_profit_pct <= 0:
            raise ValueError("take_profit_pct 必须大于 0（本策略无止损，"
                             "止盈是唯一的自动退出）")
        if self.direction not in ("buy", "sell"):
            raise ValueError(f"未知方向：{self.direction}（只允许 buy/sell）")
        if self.max_orders < 1:
            raise ValueError("max_orders 至少为 1")
        if self.wait_seconds < 0:
            raise ValueError("wait_seconds 不能为负")
        if self.grid_step_pct < 0:
            raise ValueError("grid_step_pct 不能为负")
        if self.stop_price < 0 or self.pause_price < 0:
            raise ValueError("stop_price / pause_price 不能为负（0=禁用）")


def take_profit_price(fill_price: float, direction: str,
                      take_profit_pct: float) -> float:
    """止盈价（纯函数）：买入批次向上、卖出批次向下偏移 tp%。"""
    if direction == "buy":
        return fill_price * (1 + take_profit_pct / 100)
    return fill_price * (1 - take_profit_pct / 100)


def maker_open_price(bid: float, ask: float, direction: str,
                     tick: float) -> float:
    """开仓 maker 价（纯函数，pdt 口径）：买单挂 ask − 1 tick、
    卖单挂 bid + 1 tick——贴近对手价但不穿价，保证 maker 身份。"""
    if direction == "buy":
        return ask - tick
    return bid + tick


def effective_wait(wait_seconds: float, active_closes: int,
                   max_orders: int) -> float:
    """自适应开仓间隔（纯函数，pdt 分档）：未平批次占比越高，
    间隔越长——≥2/3 时 2 倍、≥1/3 时 1 倍、≥1/6 时 1/2、否则 1/4。"""
    if max_orders <= 0:
        return wait_seconds
    ratio = active_closes / max_orders
    if ratio >= 2 / 3:
        return wait_seconds * 2
    if ratio >= 1 / 3:
        return wait_seconds
    if ratio >= 1 / 6:
        return wait_seconds / 2
    return wait_seconds / 4


def grid_step_ok(close_prices: list[float], candidate: float,
                 direction: str, grid_step_pct: float) -> bool:
    """止盈间距检查（纯函数，pdt 口径）：候选止盈价与最近一张已有
    止盈单的价格比须超过 1 + step/100 才允许开新仓。
    buy 方向取已有止盈卖单的最低价比对，sell 方向取最高价。"""
    if grid_step_pct <= 0 or not close_prices or candidate <= 0:
        return True
    if direction == "buy":
        nearest = min(close_prices)
        return nearest / candidate > 1 + grid_step_pct / 100
    nearest = max(close_prices)
    return candidate / nearest > 1 + grid_step_pct / 100


def price_trigger(book_bid: float, book_ask: float, direction: str,
                  price: float) -> bool:
    """stop/pause 价格触发（纯函数，pdt 口径）：buy 看 ask 上穿、
    sell 看 bid 下穿；price<=0 表示禁用恒不触发。"""
    if price <= 0:
        return False
    if direction == "buy":
        return book_ask >= price
    return book_bid <= price


@dataclass
class _CloseTrack:
    """一张止盈单的跟踪状态。"""

    price: float
    qty: float
    filled: float = 0.0

    @property
    def remaining(self) -> float:
        return max(self.qty - self.filled, 0.0)


@dataclass
class _OpenTrack:
    """当前开仓 maker 单的跟踪状态。"""

    order_id: str
    price: float
    qty: float
    started: float
    baseline: float          # 下单前持仓量（S2 对账基线）
    recorded: float = 0.0    # 已记账的成交量（下单即成部分）


class VolumeLoopStrategy:
    """单所刷量执行状态机。

    - start：caps 硬校验 + live 无 caps 拒绝启动 → claim → 撤遗留
      挂单 → 已有持仓登记为未托管（不接管、不新开仓，与网格一致）
    - tick：先处理成交（开仓单/止盈单，poll + 对账式发现）→ 强平
      距离监控 → stop/pause 价格检查 → 间隔/上限/间距满足时挂下一笔
    - stop（面板停止）：撤自有全部挂单，**持仓不自动平**（见
      stop_note），归属在持仓清空前不释放
    """

    name = "volume_loop"
    # 面板停止时如实标注（web/server.py 读取）：撤单但不平仓
    stop_note = ("已停止：自有挂单已撤，持仓未自动平仓"
                 "（本策略无止损，请人工确认处理）")

    def __init__(self, params: VolumeParams, executor=None, venue_id: str = "",
                 symbol: str = "BTC", owner: str | None = None) -> None:
        self.params = params
        self.executor = executor
        self.venue_id = venue_id
        self.symbol = symbol
        # 归属 owner = 任务名（config 的 name）；未给时退回类型名
        self.owner = owner or self.name
        self.started = False
        self.stopped = False
        self.exited = False            # stop_price 触发退出
        self.unmanaged_position = False
        self.last_error: str = ""
        self.recovery_notes: list[str] = []
        self._open: _OpenTrack | None = None
        self._closes: dict[str, _CloseTrack] = {}
        self._last_open_at: float = -1e18

    # ----- 基础 -----
    @property
    def _open_side(self) -> Side:
        return Side.BUY if self.params.direction == "buy" else Side.SELL

    def _signed_pos(self) -> float:
        pos = self.executor.position(self.venue_id, self.symbol)
        return pos.qty if pos else 0.0

    def _tick_size(self) -> float:
        for m in self.executor.venue(self.venue_id).markets():
            if m.symbol == self.symbol and m.price_step > 0:
                return m.price_step
        book = self.executor.venue(self.venue_id).book(self.symbol)
        return max((book.ask - book.bid) / 2, 1e-9)

    # ----- 生命周期 -----
    def start(self, now: float = 0.0) -> None:
        if self.executor is None:
            raise RuntimeError("VolumeLoopStrategy 未注入 executor（骨架模式）")
        p = self.params
        caps = self.executor.caps
        venue = self.executor.venue(self.venue_id)
        # live 强制 caps：无止损策略在真实资金上必须先有金额上限兜底
        has_cap = caps.global_ > 0 or caps.venue.get(self.venue_id, 0.0) > 0 \
            or caps.strategy.get(self.owner, 0.0) > 0
        if venue.mode == "live" and not has_cap:
            raise RuntimeError(
                "单所刷量无止损：live 模式必须至少配置一层 caps"
                "（全局/场所/策略上限），拒绝启动")
        # caps 硬校验：最大敞口估算 = max_orders × qty × 现价
        book = venue.book(self.symbol)
        est = p.max_orders * p.qty * book.mid
        for label, cap in (("全局", caps.global_),
                           (f"场所 {self.venue_id}",
                            caps.venue.get(self.venue_id, 0.0)),
                           (f"策略 {self.owner}",
                            caps.strategy.get(self.owner, 0.0))):
            if cap > 0 and est > cap:
                raise RuntimeError(
                    f"单所刷量最大敞口估算 {est:,.0f} USD 超过{label}上限 "
                    f"{cap:,.0f} USD，拒绝启动")
        self.executor.claim(self.owner, self.venue_id, self.symbol)
        # 启动对账：先撤遗留挂单（非本进程登记的），防新旧单叠加
        cancelled = self.executor.cancel_unregistered(self.venue_id, self.symbol)
        if cancelled:
            self.recovery_notes.append(
                f"启动对账：撤销遗留挂单 {cancelled} 张")
        pos = self.executor.position(self.venue_id, self.symbol)
        if pos is not None and pos.qty != 0:
            # 已有持仓无法确知批次成本，不自动接管（与网格口径一致）
            self.unmanaged_position = True
            self.recovery_notes.append(
                f"启动对账：发现未托管持仓 {pos.qty}（均价 "
                f"{pos.entry_price}），无法确知批次成本，未自动接管，"
                f"策略暂停开仓，请人工处理")
        self.started = True
        self.stopped = False

    def stop(self) -> None:
        """面板停止：撤自有全部挂单，持仓不自动平（无止损，如实标注）。"""
        if self.executor is None:
            return
        self.executor.cancel_all_own(self.owner, self.venue_id, self.symbol)
        self._open = None
        self._closes.clear()
        self.stopped = True
        if self.executor.position(self.venue_id, self.symbol) is None:
            self.executor.release(self.owner, self.venue_id, self.symbol)

    # ----- 止盈单 -----
    def _place_take_profit(self, fill_price: float, qty: float) -> None:
        if qty <= 1e-12:
            return
        p = self.params
        price = take_profit_price(fill_price, p.direction, p.take_profit_pct)
        req = OrderRequest(symbol=self.symbol, side=self._open_side.opposite,
                           qty=qty, price=price, reduce_only=True,
                           post_only=True)
        # 止盈是平仓动作：走 close 意图，停机时也允许
        res = self.executor.place(self.owner, self.venue_id, req, intent="close")
        if res.order is None:
            self.last_error = (f"止盈单下单失败：{res.detail or res.status}"
                               f"（仓位 {qty} 暂无止盈覆盖，覆盖检查会重试）")
            return
        if res.status == "filled":
            return  # 挂单即成（穿价），仓位已平，无需跟踪
        self._closes[res.order.order_id] = _CloseTrack(
            price=price, qty=qty, filled=res.filled_qty)

    def _ensure_tp_coverage(self) -> None:
        """止盈覆盖检查（安全网）：持仓量超过已挂止盈单覆盖量时，
        按持仓均价补挂差额止盈单——覆盖止盈单被外部撤销、开仓单
        部分成交对账推断等场景，防仓位裸奔无退出。"""
        pos = self.executor.position(self.venue_id, self.symbol)
        if pos is None or pos.qty == 0:
            return
        covered = sum(t.remaining for t in self._closes.values())
        uncovered = abs(pos.qty) - covered
        if uncovered > 1e-9:
            self._place_take_profit(pos.entry_price, uncovered)

    # ----- 开仓单 -----
    def _place_open(self, now: float) -> None:
        p = self.params
        venue = self.executor.venue(self.venue_id)
        book = venue.book(self.symbol)
        price = maker_open_price(book.bid, book.ask, p.direction,
                                 self._tick_size())
        baseline = self._signed_pos()
        corridor = self.executor.estimate_open_corridor(
            p.leverage, self.executor.market_mmr(self.venue_id, self.symbol))
        req = OrderRequest(symbol=self.symbol, side=self._open_side,
                           qty=p.qty, price=price, post_only=True)
        res = self.executor.place(self.owner, self.venue_id, req,
                                  intent="open", corridor_pct=corridor)
        self._last_open_at = now
        if res.order is None:
            self.last_error = res.detail or res.status
            return
        if res.status == "filled":
            self._place_take_profit(res.fill_price or price, res.filled_qty)
            return
        self._open = _OpenTrack(order_id=res.order.order_id, price=price,
                                qty=p.qty, started=now, baseline=baseline,
                                recorded=res.filled_qty)
        if res.filled_qty > 0:
            # 下单即部分成交：先为已成部分挂止盈，剩余继续跟踪
            self._place_take_profit(res.fill_price or price, res.filled_qty)

    def _finalize_open(self, filled_total: float,
                       fill_price: float) -> None:
        """开仓单终局：补记未记账的成交量并为其挂止盈单。"""
        tr = self._open
        self._open = None
        if tr is None:
            return
        inc = filled_total - tr.recorded
        if inc > 1e-9:
            self.executor.record_fill(
                self.owner, self.venue_id, self.symbol, self._open_side,
                inc, fill_price, order_id=tr.order_id)
            self._place_take_profit(fill_price, inc)

    def _reconcile_open_missing(self) -> None:
        """poll 不可得时的对账式发现（与网格同一口径）：开仓单不在
        挂单列表 → 用持仓差推断成交量；无变化视为已撤/失效。"""
        tr = self._open
        if tr is None:
            return
        venue = self.executor.venue(self.venue_id)
        try:
            open_ids = {o.order_id for o in venue.open_orders(self.symbol)}
        except Exception:  # noqa: BLE001 — 查询失败：保持跟踪下轮再试
            return
        if tr.order_id in open_ids:
            return
        delta = (self._signed_pos() - tr.baseline) * self._open_side.sign
        filled = min(max(delta, 0.0), tr.qty) if delta > 1e-9 else 0.0
        self._finalize_open(filled, tr.price)

    def _process_open(self, now: float) -> None:
        tr = self._open
        if tr is None:
            return
        venue = self.executor.venue(self.venue_id)
        o = venue.poll_order(tr.order_id)
        if o is None:
            self._reconcile_open_missing()
            return
        if o.status == "filled":
            self._finalize_open(
                o.filled_qty if o.filled_qty > 0 else tr.qty,
                o.price if o.price is not None else tr.price)
            return
        if o.status in ("canceled", "rejected"):
            self._finalize_open(o.filled_qty, tr.price)
            return
        # 仍在挂单中：超时撤单，撤单后按最终成交量收尾
        if self.params.open_timeout_seconds > 0 and \
                now - tr.started > self.params.open_timeout_seconds:
            self.executor.cancel(self.owner, self.venue_id, tr.order_id)
            o2 = venue.poll_order(tr.order_id)
            if o2 is not None and o2.status in ("canceled", "rejected",
                                                "filled"):
                filled = o2.filled_qty
                if o2.status == "filled" and filled <= 0:
                    filled = tr.qty
                self._finalize_open(
                    filled, o2.price if o2.price is not None else tr.price)
            else:
                self._reconcile_open_missing()

    def _process_closes(self) -> None:
        venue = self.executor.venue(self.venue_id)
        for oid, tr in list(self._closes.items()):
            o = venue.poll_order(oid)
            if o is None:
                # 对账式发现：止盈单不在挂单列表且持仓已减少对应量
                # → 视为已成交平仓（补记账本）；无法确认则保持跟踪
                try:
                    open_ids = {x.order_id for x in venue.open_orders(self.symbol)}
                except Exception:  # noqa: BLE001
                    continue
                if oid in open_ids:
                    continue
                covered = sum(t.remaining for k, t in self._closes.items()
                              if k != oid)
                pos_qty = abs(self._signed_pos())
                if pos_qty <= covered + 1e-9:
                    self.executor.record_fill(
                        self.owner, self.venue_id, self.symbol,
                        self._open_side.opposite, tr.remaining, tr.price,
                        order_id=oid)
                    del self._closes[oid]
                continue
            if o.status == "filled":
                inc = (o.filled_qty if o.filled_qty > 0 else tr.qty) - tr.filled
                if inc > 1e-9:
                    self.executor.record_fill(
                        self.owner, self.venue_id, self.symbol,
                        self._open_side.opposite, inc,
                        o.price if o.price is not None else tr.price,
                        order_id=oid)
                del self._closes[oid]
            elif o.status in ("canceled", "rejected"):
                inc = o.filled_qty - tr.filled
                if inc > 1e-9:
                    self.executor.record_fill(
                        self.owner, self.venue_id, self.symbol,
                        self._open_side.opposite, inc, tr.price, order_id=oid)
                del self._closes[oid]
                if o.filled_qty < tr.qty - 1e-9:
                    self.last_error = ("止盈单被撤销/拒绝，未平部分由覆盖"
                                       "检查按持仓均价补挂")
            elif o.filled_qty > tr.filled:
                tr.filled = o.filled_qty  # 部分成交：只更新已成量

    # ----- 主循环 -----
    def tick(self, now: float = 0.0, allow_open: bool = True) -> None:
        if self.executor is None or not self.started or self.stopped \
                or self.exited:
            return
        if not allow_open:
            # 全局停机（安全优先，与其它策略一致）：撤单 + 市价平仓
            self.executor.cancel_all_own(self.owner, self.venue_id, self.symbol)
            self._open = None
            self._closes.clear()
            self.executor.close_position(self.owner, self.venue_id, self.symbol)
            self.stopped = True
            return
        if self.unmanaged_position:
            return  # 未托管持仓处理前不做任何自动动作
        self._process_closes()
        self._process_open(now)
        self._ensure_tp_coverage()

        book = self.executor.venue(self.venue_id).book(self.symbol)
        # 强平距离监控（公共口径）：跌破门槛撤单平仓停机；
        # 不可评估时只暂停开新仓（block_new）
        block_new = False
        has_pos, corridor = self.executor.position_corridor(
            self.venue_id, self.symbol, book.mid)
        if has_pos:
            if corridor is None:
                block_new = True
                self.last_error = ("强平距离不可评估（无强平价且杠杆未知），"
                                   "暂停新开仓")
            elif corridor < self.executor.min_corridor_pct:
                self.last_error = (f"强平距离 {corridor:.2f}% 跌破门槛 "
                                   f"{self.executor.min_corridor_pct}%，停机平仓")
                self.executor.cancel_all_own(self.owner, self.venue_id,
                                             self.symbol)
                self._open = None
                self._closes.clear()
                self.executor.close_position(self.owner, self.venue_id,
                                             self.symbol)
                self.stopped = True
                return

        # stop_price：撤开仓单并退出运行；止盈单与持仓保留（pdt 语义）
        p = self.params
        if price_trigger(book.bid, book.ask, p.direction, p.stop_price):
            if self._open is not None:
                self.executor.cancel(self.owner, self.venue_id,
                                     self._open.order_id)
                self._open = None
            self.exited = True
            self.last_error = (f"价格触及 stop_price {p.stop_price}，"
                               f"已停止开仓并退出（止盈单与持仓保留）")
            self.recovery_notes.append(self.last_error)
            return
        # pause_price：只暂停开新仓
        if price_trigger(book.bid, book.ask, p.direction, p.pause_price):
            return
        if block_new or self._open is not None:
            return
        if len(self._closes) >= p.max_orders:
            return  # 未平批次达上限
        wait = effective_wait(p.wait_seconds, len(self._closes), p.max_orders)
        if now - self._last_open_at < wait:
            return
        # 止盈间距：候选止盈价取当前对手价口径（与 pdt 一致）
        ref = book.ask if p.direction == "buy" else book.bid
        candidate = take_profit_price(ref, p.direction, p.take_profit_pct)
        if not grid_step_ok([t.price for t in self._closes.values()],
                            candidate, p.direction, p.grid_step_pct):
            return
        self._place_open(now)

    def decide(self, ctx) -> list:
        return []
