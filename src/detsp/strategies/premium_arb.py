"""溢价套利策略：参数 + 纯函数溢价带计算 + 执行状态机。

来源与重组关系：
- 价差带逻辑（中枢/上下带、可成交价扣费门槛、信号持续门、库存
  阶梯、定期对账净 delta 修正）来自 entropy-arb（your-quantguy，
  MIT）；
- maker 主腿执行来自 perp-dex-tools（your-quantguy，非商业许可，
  本项目个人自用，见 LICENSES/ 与 NOTICE）的 hedge 模式：
  主腿先 maker 挂单等成交、成交后对冲腿 taker 跟上。

约定：premium 以基准腿（base）相对对冲腿（hedge）计：
premium_bps = (base 价 / hedge 价 − 1) × 10000。

执行模式（execution_mode）：
- taker（默认）：信号触发后双腿 taker 立即开仓，与旧版一致；
- maker：**开仓**时 base 腿先以 post-only 限价挂在盘口内侧等
  成交，全成后 hedge 腿 taker 跟上；部分成交先对冲已成部分、
  再撤余单结束本轮；超时未成撤单放弃本轮（信号需重新持续
  persist_seconds 才再触发，相当于冷却）。**平仓/反向减仓仍走
  双腿 taker**，保证退出确定性，不用 maker 平仓。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..core.risk import net_delta
from ..core.venue import OrderRequest, Side


@dataclass
class PremiumParams:
    midline_bps: float          # 溢价中枢：必须实测，不假设为 0
    upper_bps: float            # 中枢上方入场带（>0）
    lower_bps: float            # 中枢下方入场带（>0）
    base_taker_fee_bps: float = 0.0
    hedge_taker_fee_bps: float = 0.0
    max_position_usd: float = 1000.0
    max_order_notional_usd: float = 500.0
    take_fraction: float = 0.5  # 吃对手盘深度的比例
    persist_seconds: float = 0.3
    # 执行模式（maker 主腿来自 perp-dex-tools 的 hedge 执行方式）
    execution_mode: str = "taker"   # taker / maker
    maker_offset_ticks: int = 0     # maker 挂价向盘口内侧偏移的 tick 数
    maker_timeout_seconds: float = 30.0  # maker 开仓单最长等待
    # 执行层字段（默认值不影响纯函数测试）
    symbol: str = "BTC"
    base_venue: str = ""        # 基准腿 venue_id（如 entropy）
    hedge_venue: str = ""       # 对冲腿 venue_id（如 lighter）
    reconcile_seconds: float = 15.0  # 与场所持仓对账周期
    leverage_base: float = 1.0  # 两腿杠杆（开仓强平距离估算用；
    leverage_hedge: float = 1.0  # 交易所不由本程序设置杠杆，须与账户实际一致）

    def __post_init__(self) -> None:
        if self.upper_bps <= 0 or self.lower_bps <= 0:
            raise ValueError("upper_bps / lower_bps 必须大于 0")
        if not 0 < self.take_fraction <= 1:
            raise ValueError("take_fraction 须在 (0, 1]")
        if self.execution_mode not in ("taker", "maker"):
            raise ValueError(f"未知执行模式：{self.execution_mode}")
        if self.maker_offset_ticks < 0:
            raise ValueError("maker_offset_ticks 不能为负")
        if self.maker_timeout_seconds <= 0:
            raise ValueError("maker_timeout_seconds 必须大于 0")

    @property
    def total_fee_bps(self) -> float:
        return self.base_taker_fee_bps + self.hedge_taker_fee_bps


def premium_bps(base_price: float, hedge_price: float) -> float:
    if base_price <= 0 or hedge_price <= 0:
        raise ValueError("价格必须大于 0")
    return (base_price / hedge_price - 1) * 10_000


def sell_threshold_bps(p: PremiumParams) -> float:
    """卖 base 买 hedge 的溢价门槛（未扣费）。"""
    return p.midline_bps + p.upper_bps


def buy_threshold_bps(p: PremiumParams) -> float:
    """买 base 卖 hedge 的溢价门槛（未扣费，可为负，属预期设计）。"""
    return p.midline_bps - p.lower_bps


def band_signal(premium: float, p: PremiumParams) -> str | None:
    """按中枢价溢价判断信号（不含可成交价/费用修正）。

    返回 "sell_base" / "buy_base" / None。
    """
    if premium >= sell_threshold_bps(p):
        return "sell_base"
    if premium <= buy_threshold_bps(p):
        return "buy_base"
    return None


def executable_sell_edge_bps(base_bid: float, hedge_ask: float, p: PremiumParams) -> float:
    """卖 base 的可成交溢价（扣双边 taker 费后）。"""
    return premium_bps(base_bid, hedge_ask) - p.total_fee_bps


def executable_buy_edge_bps(base_ask: float, hedge_bid: float, p: PremiumParams) -> float:
    """买 base 的可成交优势（扣费后）：对冲腿 bid 相对 base ask 的折价。"""
    return premium_bps(hedge_bid, base_ask) - p.total_fee_bps


def executable_signal(base_bid: float, base_ask: float,
                      hedge_bid: float, hedge_ask: float,
                      p: PremiumParams) -> str | None:
    """实盘触发判断：用可成交价且扣费。

    - 卖 base：可成交溢价 ≥ midline + upper
    - 买 base：可成交折价 ≥ lower − midline（中枢为正时该门槛可为负，
      因为在中枢下方买入本身就是相对均衡的便宜价，属预期设计）
    """
    if executable_sell_edge_bps(base_bid, hedge_ask, p) >= sell_threshold_bps(p):
        return "sell_base"
    if executable_buy_edge_bps(base_ask, hedge_bid, p) >= p.lower_bps - p.midline_bps:
        return "buy_base"
    return None


def round_trip_floor_bps(p: PremiumParams) -> float:
    """一轮往返（开+反向平）扣费后的收益下限：upper + lower。"""
    return p.upper_bps + p.lower_bps


def slice_notional(depth_notional: float, p: PremiumParams) -> float:
    """单笔切片金额：对手深度 × take_fraction，再按单笔上限截断。"""
    return max(0.0, min(depth_notional * p.take_fraction, p.max_order_notional_usd))


def inventory_surcharge_bps(position_usd: float, p: PremiumParams,
                            scale_bps: float = 10.0, floor_frac: float = 0.5) -> float:
    """库存阶梯：仓位超过上限的 floor_frac 后，额外要求的溢价线性增加。"""
    if p.max_position_usd <= 0:
        return 0.0
    frac = abs(position_usd) / p.max_position_usd
    if frac <= floor_frac:
        return 0.0
    span = max(1 - floor_frac, 1e-9)
    return min((frac - floor_frac) / span, 1.0) * scale_bps


def maker_open_price(bid: float, ask: float, side: Side, tick: float,
                     offset_ticks: int = 0) -> float:
    """maker 开仓挂价（纯函数）：挂在盘口内侧——买贴 bid、卖贴 ask，
    再向内侧偏移 offset_ticks 个 tick，但不穿价（买价封顶 ask−tick、
    卖价封底 bid+tick），保证仍是 maker 单。tick<=0 时直接贴盘口。"""
    if tick <= 0:
        return bid if side is Side.BUY else ask
    off = max(offset_ticks, 0) * tick
    if side is Side.BUY:
        return max(bid, min(bid + off, ask - tick))
    return min(ask, max(ask - off, bid + tick))


@dataclass
class _MakerOpen:
    """一笔 maker 开仓单的跟踪状态（base 腿）。"""

    order_id: str
    signal: str          # sell_base / buy_base
    side: Side           # base 腿方向
    qty: float           # 目标数量（整笔切片）
    price: float
    started: float
    baseline: float      # 挂单前 base 腿持仓（对账式发现基线）
    recorded: float = 0.0   # 下单时已记账的成交量
    partial: float = 0.0    # 已发现的累计成交量


class PremiumArbStrategy:
    """溢价套利执行状态机。

    - 信号：executable_signal（可成交价、扣双边费）+ 库存阶梯附加
      门槛；信号须持续 persist_seconds 才触发（防单 tick 幻影溢价）
    - taker 模式：双腿 taker 先后紧贴发出；maker 模式：开仓走
      maker 主腿（见模块头注），减仓/平仓仍双腿 taker
    - 仓位：单笔切片 max_order_notional_usd，总仓 max_position_usd
    - 对账：每 reconcile_seconds 与场所真实持仓核对；净 delta 不平
      立即以 reduce-only 市价单修正（只减多出的那条腿）
    - 停机信号（allow_open=False）/ 面板 stop：撤自有挂单 + 双腿全平
    """

    name = "premium_arb"
    stop_note = "已停止：双腿挂单已撤、双腿持仓已平"

    def __init__(self, params: PremiumParams, executor=None,
                 owner: str | None = None) -> None:
        self.params = params
        self.executor = executor
        # 归属 owner = 任务名（config 的 name）；未给时退回类型名
        self.owner = owner or self.name
        self._claimed = False
        self._armed: dict[str, float] = {}   # 信号 -> 首次出现时间
        self._last_reconcile = -1e18
        self._maker: _MakerOpen | None = None
        self.stopped = False
        self.last_error: str = ""
        self.recovery_notes: list[str] = []  # 启动对账事件

    def start(self, now: float = 0.0) -> None:
        if self.executor is None:
            raise RuntimeError("PremiumArbStrategy 未注入 executor（骨架模式）")
        self._ensure_claims()
        self.stopped = False
        # 启动恢复对账：已有持仓不假设是本进程开的——记录事件，
        # 净 delta 由 tick 的定期对账（_fix_delta）自动修正，
        # 强平距离由 tick 监控接管
        qb = self._pos_qty(self.params.base_venue)
        qh = self._pos_qty(self.params.hedge_venue)
        if qb != 0 or qh != 0:
            self.recovery_notes.append(
                f"启动对账：发现已有持仓 base={qb} hedge={qh}，"
                f"由定期对账与强平监控接管")

    def _flatten(self) -> None:
        """撤自有挂单（含 maker 开仓单）+ 双腿全平（停机与停止共用）。"""
        p = self.params
        self._maker = None
        self._armed.clear()
        self.executor.cancel_all_own(self.owner, p.base_venue, p.symbol)
        self.executor.cancel_all_own(self.owner, p.hedge_venue, p.symbol)
        self.executor.close_position(self.owner, p.base_venue, p.symbol)
        self.executor.close_position(self.owner, p.hedge_venue, p.symbol)

    def stop(self) -> None:
        """面板停止：撤单 + 双腿全平，并停机（不再 tick 动作）。"""
        if self.executor is None:
            return
        self._flatten()
        self.stopped = True

    def _ensure_claims(self) -> None:
        if not self._claimed:
            p = self.params
            self.executor.claim(self.owner, p.base_venue, p.symbol)
            self.executor.claim(self.owner, p.hedge_venue, p.symbol)
            self._claimed = True

    def _pos_qty(self, venue_id: str) -> float:
        pos = self.executor.position(venue_id, self.params.symbol)
        return pos.qty if pos else 0.0

    def _fix_delta(self) -> None:
        """净 delta 修正：多出的腿用 reduce-only 市价单削回与另一腿等量。"""
        p = self.params
        legs = {p.base_venue: self._pos_qty(p.base_venue),
                p.hedge_venue: self._pos_qty(p.hedge_venue)}
        delta = net_delta(legs)
        if delta == 0:
            return
        # 净多头过剩：卖出多出的量；净空头过剩：买回。落在绝对值较大的腿上
        big = max(legs, key=lambda v: abs(legs[v]))
        excess = abs(legs[big]) - abs(legs[p.hedge_venue if big == p.base_venue
                                       else p.base_venue])
        if excess <= 1e-12:
            return
        side = Side.SELL if legs[big] > 0 else Side.BUY
        req = OrderRequest(symbol=p.symbol, side=side, qty=excess,
                           price=None, reduce_only=True)
        self.executor.place(self.owner, big, req, intent="close")

    def _signal_with_surcharge(self, base_book, hedge_book) -> str | None:
        """可成交信号 + 库存阶梯：同向加仓时门槛上浮 surcharge。"""
        p = self.params
        base_pos_usd = self._pos_qty(p.base_venue) * base_book.mid
        sell_edge = executable_sell_edge_bps(base_book.bid, hedge_book.ask, p)
        if sell_edge >= sell_threshold_bps(p) + (
                inventory_surcharge_bps(base_pos_usd, p)
                if base_pos_usd < 0 else 0.0):
            return "sell_base"
        buy_edge = executable_buy_edge_bps(base_book.ask, hedge_book.bid, p)
        if buy_edge >= (p.lower_bps - p.midline_bps) + (
                inventory_surcharge_bps(base_pos_usd, p)
                if base_pos_usd > 0 else 0.0):
            return "buy_base"
        return None

    # ----- 开仓量与双腿下单 -----
    def _slice_qty(self) -> tuple[float, float]:
        """本轮切片数量与参考名义：Book 无深度数据，用单笔上限作
        为切片名义（见 DESIGN 的已知限制）。返回 (qty, notional)。"""
        p = self.params
        base_book = self.executor.venue(p.base_venue).book(p.symbol)
        notional = slice_notional(
            p.max_order_notional_usd / max(p.take_fraction, 1e-9), p)
        return notional / base_book.mid, notional

    def _corridors(self) -> tuple[float, float]:
        ex = self.executor
        p = self.params
        return (
            ex.estimate_open_corridor(
                p.leverage_base, ex.market_mmr(p.base_venue, p.symbol)),
            ex.estimate_open_corridor(
                p.leverage_hedge, ex.market_mmr(p.hedge_venue, p.symbol)),
        )

    def _fire_taker(self, signal: str, qty: float) -> None:
        """双腿 taker 开平（taker 模式的开仓，以及任何模式的减仓/
        反向平仓——退出必须确定成交，不用 maker）。"""
        p = self.params
        ex = self.executor
        if signal == "sell_base":
            base_req = OrderRequest(p.symbol, Side.SELL, qty, price=None)
            hedge_req = OrderRequest(p.symbol, Side.BUY, qty, price=None)
        else:
            base_req = OrderRequest(p.symbol, Side.BUY, qty, price=None)
            hedge_req = OrderRequest(p.symbol, Side.SELL, qty, price=None)
        corr_base, corr_hedge = self._corridors()
        ex.place(self.owner, p.base_venue, base_req, intent="open",
                 corridor_pct=corr_base)
        ex.place(self.owner, p.hedge_venue, hedge_req, intent="open",
                 corridor_pct=corr_hedge)
        # 双腿成交量可能不等（部分成交）：立即按对账结果修正净 delta
        self._fix_delta()
        self._last_reconcile = -1e18  # 下一 tick 先对账确认

    def _fire(self, signal: str, now: float) -> None:
        p = self.params
        qty, notional = self._slice_qty()
        if qty <= 0:
            return
        base_qty = self._pos_qty(p.base_venue)
        base_book = self.executor.venue(p.base_venue).book(p.symbol)
        # 仓位上限：加仓后 base 腿名义不超过 max_position_usd（减仓不受限）
        adds = (base_qty <= 0) if signal == "sell_base" else (base_qty >= 0)
        if adds and abs(base_qty * base_book.mid) + notional > p.max_position_usd:
            return
        # 与现有 base 持仓反向的交易 = 减仓/平仓：任何模式都走 taker
        closing = (base_qty > 0 and signal == "sell_base") or \
            (base_qty < 0 and signal == "buy_base")
        if p.execution_mode == "maker" and not closing:
            self._place_maker_open(signal, qty, now)
        else:
            self._fire_taker(signal, qty)

    # ----- maker 主腿开仓 -----
    def _base_tick(self) -> float:
        for m in self.executor.venue(self.params.base_venue).markets():
            if m.symbol == self.params.symbol and m.price_step > 0:
                return m.price_step
        book = self.executor.venue(self.params.base_venue).book(self.params.symbol)
        return max((book.ask - book.bid) / 2, 1e-9)

    def _place_maker_open(self, signal: str, qty: float, now: float) -> None:
        p = self.params
        ex = self.executor
        side = Side.SELL if signal == "sell_base" else Side.BUY
        book = ex.venue(p.base_venue).book(p.symbol)
        price = maker_open_price(book.bid, book.ask, side,
                                 self._base_tick(), p.maker_offset_ticks)
        baseline = self._pos_qty(p.base_venue)
        corr_base, _ = self._corridors()
        req = OrderRequest(symbol=p.symbol, side=side, qty=qty,
                           price=price, post_only=True)
        res = ex.place(self.owner, p.base_venue, req, intent="open",
                       corridor_pct=corr_base)
        if res.order is None:
            self.last_error = f"maker 开仓单下单失败：{res.detail or res.status}"
            return
        if res.status == "filled":
            filled = res.filled_qty if res.filled_qty > 0 else qty
            self._hedge_after_maker(signal, filled)
            return
        # open / partial：登记归属后跟踪，等终态或超时按最终量收尾
        self._maker = _MakerOpen(
            order_id=res.order.order_id, signal=signal, side=side,
            qty=qty, price=price, started=now, baseline=baseline,
            recorded=res.filled_qty, partial=res.filled_qty)

    def _hedge_after_maker(self, signal: str, filled_qty: float) -> None:
        """maker 主腿已成交 filled_qty：对冲腿 taker 立即跟上等量。"""
        if filled_qty <= 1e-12:
            return
        p = self.params
        hedge_side = Side.BUY if signal == "sell_base" else Side.SELL
        _, corr_hedge = self._corridors()
        self.executor.place(
            self.owner, p.hedge_venue,
            OrderRequest(p.symbol, hedge_side, filled_qty, price=None),
            intent="open", corridor_pct=corr_hedge)
        self._fix_delta()
        self._last_reconcile = -1e18

    def _maker_final_filled(self, mk: _MakerOpen) -> float | None:
        """maker 单的最终成交量：优先 poll 终态；poll 不可得时用
        「持仓 − 挂单前基线」对账推断。订单仍在挂单中返回 None。"""
        p = self.params
        venue = self.executor.venue(p.base_venue)
        o = venue.poll_order(mk.order_id)
        if o is not None:
            if o.status == "open":
                return None
            return o.filled_qty
        try:
            open_ids = {x.order_id for x in venue.open_orders(p.symbol)}
        except Exception:  # noqa: BLE001 — 查询失败：保持跟踪
            return None
        if mk.order_id in open_ids:
            return None
        cur = self._pos_qty(p.base_venue)
        delta = (cur - mk.baseline) * mk.side.sign
        return min(max(delta, 0.0), mk.qty)

    def _finalize_maker(self, mk: _MakerOpen, filled: float) -> None:
        """maker 终局：补记未记账成交量，再按最终量对冲收尾。

        filled=0（超时未成/被拒）= 放弃本轮，不下任何对冲单；
        armed 已在 _fire 前清空，信号须重新持续 persist_seconds
        才再触发，相当于进冷却。
        """
        self._maker = None
        inc = filled - mk.recorded
        if inc > 1e-9:
            self.executor.record_fill(
                self.owner, self.params.base_venue, self.params.symbol,
                mk.side, inc, mk.price, order_id=mk.order_id)
        if filled > 1e-9:
            self._hedge_after_maker(mk.signal, filled)

    def _tick_maker(self, now: float) -> None:
        """maker 开仓单跟踪：全成→对冲；终态带部分量→按最终量
        对冲；超时→先撤单再按最终量收尾（0 则放弃本轮）。"""
        mk = self._maker
        if mk is None:
            return
        p = self.params
        venue = self.executor.venue(p.base_venue)
        o = venue.poll_order(mk.order_id)
        if o is not None:
            if o.status == "open":
                if o.filled_qty > mk.partial:
                    mk.partial = o.filled_qty
            elif o.status == "filled":
                self._finalize_maker(
                    mk, o.filled_qty if o.filled_qty > 0 else mk.qty)
                return
            elif o.status in ("canceled", "rejected"):
                self._finalize_maker(mk, o.filled_qty)
                return
        else:
            final = self._maker_final_filled(mk)
            if final is not None:
                self._finalize_maker(mk, final)
                return
        if now - mk.started >= p.maker_timeout_seconds:
            self.executor.cancel(self.owner, p.base_venue, mk.order_id)
            final = self._maker_final_filled(mk)
            if final is None:
                # 撤单后仍确认不了终局：用已发现的部分成交量兜底，
                # 再不行留到下一 tick（订单已撤，终局必现）
                final = mk.partial if mk.partial > 0 else None
            if final is not None:
                self._finalize_maker(mk, final)

    # ----- 主循环 -----
    def tick(self, now: float = 0.0, allow_open: bool = True) -> None:
        if self.executor is None or self.stopped:
            return
        self._ensure_claims()
        p = self.params
        ex = self.executor

        if not allow_open:
            # 停机：撤自有挂单（含 maker 开仓单）+ 双腿全平；
            # 停机解除后可继续运行（与本策略历史语义一致）
            self._flatten()
            return

        # maker 开仓单未收尾前先于信号处理：它收尾时会对冲腿跟上，
        # 期间净 delta 暂时不平是预期状态，不让定期对账误削主腿
        if self._maker is not None:
            self._tick_maker(now)
            return

        # 定期对账：净 delta 不平立即修正（安全动作，先于开仓信号）
        if now - self._last_reconcile >= p.reconcile_seconds:
            self._last_reconcile = now
            self._fix_delta()

        base_book = ex.venue(p.base_venue).book(p.symbol)
        hedge_book = ex.venue(p.hedge_venue).book(p.symbol)

        # 持仓强平距离监控（公共口径）：任一腿跌破门槛 → 双平；
        # 不可评估 → 本 tick 不开新仓（不放行）
        block_fire = False
        for vid, bk in ((p.base_venue, base_book), (p.hedge_venue, hedge_book)):
            has, corr = ex.position_corridor(vid, p.symbol, bk.mid)
            if not has:
                continue
            if corr is None:
                block_fire = True
                self.last_error = (f"{vid} 强平距离不可评估"
                                   f"（无强平价且杠杆未知），暂停开仓")
            elif corr < ex.min_corridor_pct:
                self.last_error = (f"{vid} 强平距离 {corr:.2f}% 跌破门槛 "
                                   f"{ex.min_corridor_pct}%，双腿平仓")
                ex.close_position(self.owner, p.base_venue, p.symbol)
                ex.close_position(self.owner, p.hedge_venue, p.symbol)
                self._armed.clear()
                return

        signal = self._signal_with_surcharge(base_book, hedge_book)
        if signal is None or block_fire:
            self._armed.clear()
            return
        since = self._armed.setdefault(signal, now)
        if now - since >= p.persist_seconds:
            self._armed.clear()
            self._fire(signal, now)

    def decide(self, ctx) -> list:
        return []
