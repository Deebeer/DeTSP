"""轮换对冲刷量策略：参数 + 纯函数决策 + 执行状态机。

移植自 Parallax Hedge（MIT）：双腿反向等量、定时轮换、
按资金费选向、强平距离风控、孤腿抢救。
可选参数 max_position_qty 的「逐步建仓到上限」语义源自
perp-dex-tools（非商业许可，个人自用，见 NOTICE）的 hedge
--max-position，按本策略的双腿口径重组。
已接统一执行层（core.execution.Executor），归属 owner = 任务名（H4）。
审计修复（2026-11-03）：资金费统一换算每小时口径再选向（H3）；
强平距离改用 Executor 公共口径带 mmr、不可评估按危险处理（H7/M5）；
maker 部分成交保留跟踪、终局按最终量对冲（M2）；poll 不可得时
「挂单消失+持仓差」对账式发现（S2）；启动按已有持仓对账初始化（S3）。
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from ..core.risk import (
    basis_ok,
    detect_single_leg,
    funding_hourly_rate,
    should_force_close,
)
from ..core.venue import OrderRequest, Side


@dataclass
class RotationParams:
    symbol: str
    venue_a: str
    venue_b: str
    notional_usd: float                 # 单边开仓金额
    hold_seconds: float = 3600.0        # 固定持有时长（与区间二选一）
    hold_min_seconds: float = 0.0       # 区间持有：>0 时启用随机抽取
    hold_max_seconds: float = 0.0
    cooldown_seconds: float = 60.0
    notional_min_usd: float = 0.0       # 金额区间：>0 时启用随机抽取
    notional_max_usd: float = 0.0
    arcus_maker: bool = False           # Arcus 腿走 maker 挂单
    leverage_a: float = 1.0
    leverage_b: float = 1.0
    maker_wait_seconds: float = 120.0   # maker 挂单最长等待，超时撤单
    max_position_qty: float = 0.0       # 逐步建仓上限（币，两腿绝对值）：
    # 0=禁用（原行为：一轮开满 notional 后持有到期轮换）；>0 时首轮
    # 开仓后在 open 阶段按 accumulate_interval_seconds 间隔逐档同向
    # 加仓（每档仍为 notional_usd 口径），两腿达到该数量后停止加仓、
    # 转入正常持有→到期平仓循环。语义源自 perp-dex-tools 的 hedge
    # --max-position（主所 maker 逐笔建仓到上限），此处按 DeTSP 的
    # 双腿 taker 口径重组（maker 首轮之后的加仓档统一走 taker）
    accumulate_interval_seconds: float = 60.0  # 逐步建仓的加仓间隔

    def __post_init__(self) -> None:
        if self.venue_a == self.venue_b:
            raise ValueError("对冲两腿不能是同一场所")
        if self.notional_usd <= 0:
            raise ValueError("notional_usd 必须大于 0")
        if self.max_position_qty < 0:
            raise ValueError("max_position_qty 不能为负（0=禁用）")


def choose_direction(funding_a: float, funding_b: float) -> tuple[str, str]:
    """按资金费选向：空高费率所、多低费率所，净收资金费。

    返回 (多头场所, 空头场所)，取值为 "a" / "b"。
    费率相等时默认多 a 空 b。
    """
    if funding_a >= funding_b:
        return ("b", "a")
    return ("a", "b")


def net_funding_edge(funding_a: float, funding_b: float) -> float:
    """选向后每期可收的资金费差（空腿费率 − 多腿费率）。"""
    return abs(funding_a - funding_b)


def draw_hold_seconds(p: RotationParams, rng: random.Random | None = None) -> float:
    """持有时长：区间模式在区间内随机抽，否则用固定值。"""
    if p.hold_min_seconds > 0 and p.hold_max_seconds > p.hold_min_seconds:
        r = rng or random
        return r.uniform(p.hold_min_seconds, p.hold_max_seconds)
    return p.hold_seconds


def draw_notional(p: RotationParams, rng: random.Random | None = None) -> float:
    if p.notional_min_usd > 0 and p.notional_max_usd > p.notional_min_usd:
        r = rng or random
        return r.uniform(p.notional_min_usd, p.notional_max_usd)
    return p.notional_usd


def should_rotate(opened_at: float, now: float, hold_seconds: float) -> bool:
    return now - opened_at >= hold_seconds


def accumulation_step_qty(current_qty: float, step_qty: float,
                          max_position_qty: float) -> float:
    """逐步建仓的单步数量（纯函数）：不超过 max_position_qty 的
    剩余额度；max<=0（禁用）时原样返回 step_qty；已达上限返回 0。"""
    if max_position_qty <= 0:
        return step_qty
    remaining = max_position_qty - abs(current_qty)
    if remaining <= 0:
        return 0.0
    return min(step_qty, remaining)


def in_cooldown(closed_at: float, now: float, cooldown_seconds: float) -> bool:
    return now - closed_at < cooldown_seconds


def position_size(balance_a: float, balance_b: float, leverage_a: float,
                  leverage_b: float, p: RotationParams) -> float:
    """仓位大小：两边可用保证金×杠杆取小，再按任务金额截断。"""
    capacity = min(balance_a * leverage_a, balance_b * leverage_b)
    return min(capacity, draw_notional(p))


class RotationHedgeStrategy:
    """轮换对冲执行状态机（移植 Parallax execution/risk 的流程）。

    阶段：idle →（双 taker 或 maker 等成交）→ open → 到期/风控 → cooldown。
    安全动作永远优先：每 tick 先查孤腿/不平衡/强平距离，再谈开仓。
    - 孤腿（只剩一条腿）：无条件市价平掉剩余腿
    - 双腿数量不平：只减仓削掉多出的一截；同向（方向错乱）双腿全平
    - 开仓后复核：实际持仓强平距离低于门槛 → 立即双平
    - 停机信号（allow_open=False）：撤 maker 挂单并双腿全平
    """

    name = "rotation_hedge"
    # 面板停止语义：四策略统一为撤单 + 平仓型
    stop_note = "已停止：双腿挂单已撤、双腿持仓已平"

    def __init__(self, params: RotationParams, executor=None, risk_cfg=None,
                 rng: random.Random | None = None,
                 owner: str | None = None) -> None:
        self.params = params
        self.executor = executor
        self.rng = rng
        # H4：归属 owner = 任务名（config 的 name）；未给时退回类型名
        self.owner = owner or self.name
        self.min_corridor_pct = getattr(risk_cfg, "min_corridor_pct", 3.0)
        self.close_corridor_pct = getattr(risk_cfg, "close_corridor_pct", 1.5)
        self.max_basis_pct = 3.0
        self.phase = "idle"          # idle / opening / open / cooldown
        self.stopped = False         # 面板 stop 后置位，tick 不再动作
        self.opened_at = 0.0
        self.closed_at = -1e18
        self.hold_drawn = params.hold_seconds
        self.open_corridor: float | None = None
        self._claimed = False
        self._maker_oid: str | None = None
        self._maker_leg: str = ""    # "a" / "b"
        self._maker_qty = 0.0
        self._maker_started = 0.0
        self._maker_partial = 0.0    # M2：已发现的部分成交量（累计口径）
        self._maker_recorded = 0.0   # 下单时 Executor 已记账的量
        self._maker_pos_before = 0.0  # S2：挂单前该腿持仓（对账基线）
        self._maker_sign = 1.0       # maker 腿方向符号（多 +1 / 空 -1）
        self._round_qty = 0.0        # 本轮单档数量（逐步建仓的步长）
        self._last_add_at = -1e18    # 最近一次加仓时间（逐步建仓间隔）
        self.last_error: str = ""
        self.recovery_notes: list[str] = []  # S3 启动对账事件

    # ----- 基础 -----
    def _venue_id(self, leg: str) -> str:
        return self.params.venue_a if leg == "a" else self.params.venue_b

    def _leverage(self, leg: str) -> float:
        return self.params.leverage_a if leg == "a" else self.params.leverage_b

    def _ensure_claims(self) -> None:
        if not self._claimed:
            self.executor.claim(self.owner, self.params.venue_a, self.params.symbol)
            self.executor.claim(self.owner, self.params.venue_b, self.params.symbol)
            self._claimed = True

    def start(self, now: float = 0.0) -> None:
        if self.executor is None:
            raise RuntimeError("RotationHedgeStrategy 未注入 executor（骨架模式）")
        self._ensure_claims()
        self.stopped = False
        # S3 启动恢复对账：已有持仓（双腿对冲或单腿残留）按 open 阶段
        # 初始化，opened_at 重置为现在，安全检查从下一 tick 起接管
        # （孤腿会被平、双腿到期轮换），不假设是本进程开的仓
        legs = self._legs_qty()
        if legs["a"] != 0 or legs["b"] != 0:
            self.phase = "open"
            self.opened_at = now
            self.hold_drawn = draw_hold_seconds(self.params, self.rng)
            self.recovery_notes.append(
                f"启动对账：发现已有持仓 a={legs['a']} b={legs['b']}，"
                f"按 open 阶段接管，安全检查接管处理")

    def stop(self) -> None:
        """面板停止：撤自有挂单（含 maker 开仓单）+ 双腿全平。"""
        if self.executor is None:
            return
        self._ensure_claims()
        if self._maker_oid is not None:
            try:
                self.executor.cancel(self.owner,
                                     self._venue_id(self._maker_leg),
                                     self._maker_oid)
            except Exception:  # noqa: BLE001 — 撤单失败不挡平仓
                pass
            self._maker_oid = None
        for leg in ("a", "b"):
            self.executor.cancel_all_own(self.owner, self._venue_id(leg),
                                         self.params.symbol)
        self._close_both(0.0)
        self.stopped = True

    def _legs_qty(self) -> dict[str, float]:
        out = {}
        for leg in ("a", "b"):
            pos = self.executor.position(self._venue_id(leg), self.params.symbol)
            out[leg] = pos.qty if pos else 0.0
        return out

    def _close_leg(self, leg: str, qty: float | None = None) -> None:
        self.executor.close_position(self.owner, self._venue_id(leg),
                                     self.params.symbol, qty=qty)

    def _finish_round(self, now: float) -> None:
        self.phase = "cooldown"
        self.closed_at = now
        self.open_corridor = None
        self._maker_oid = None
        self._maker_partial = 0.0
        self._maker_recorded = 0.0

    def _close_both(self, now: float) -> None:
        self._close_leg("a")
        self._close_leg("b")
        self._finish_round(now)

    # ----- 安全检查（每 tick 最先执行） -----
    def _safety(self, now: float) -> bool:
        """返回 True 表示已处理安全事件，本 tick 不再做其他事。"""
        legs = self._legs_qty()
        qa, qb = legs["a"], legs["b"]
        if qa == 0 and qb == 0:
            return False
        if detect_single_leg({"a": qa, "b": qb}):
            if qa != 0 and qb != 0 and qa * qb < 0:
                # 双腿反向但数量不平：削掉较大腿多出的部分
                big, small = ("a", "b") if abs(qa) > abs(qb) else ("b", "a")
                self._close_leg(big, qty=abs(legs[big]) - abs(legs[small]))
                return True
            if qa != 0 and qb != 0:
                # 双腿同向 = 方向错乱，全平
                self._close_both(now)
                return True
            # 孤腿：无条件市价平掉剩余腿
            self._close_leg("a" if qa != 0 else "b")
            self._finish_round(now)
            return True
        # 强平距离监控（H7/M5 公共口径：带 mmr；不可评估按危险处理）
        corridors = []
        for leg in ("a", "b"):
            vid = self._venue_id(leg)
            mid = self.executor.venue(vid).book(self.params.symbol).mid
            has, c = self.executor.position_corridor(
                vid, self.params.symbol, mid)
            if not has:
                continue
            if c is None:
                self.last_error = (f"{vid} 强平距离不可评估"
                                   f"（无强平价且杠杆未知），双平")
                self._close_both(now)
                return True
            corridors.append(c)
        if corridors:
            worst = min(corridors)
            ref = self.open_corridor if self.open_corridor is not None else worst
            if worst < self.close_corridor_pct or \
                    should_force_close(worst, ref, self.close_corridor_pct):
                self._close_both(now)
                return True
        return False

    # ----- 开仓 -----
    def _est_corridor(self, leg: str) -> float:
        """开仓前估算强平距离（%）：Executor 公共口径（带 mmr，M5）。"""
        return self.executor.estimate_open_corridor(
            self._leverage(leg),
            self.executor.market_mmr(self._venue_id(leg), self.params.symbol))

    def _open_round(self, now: float) -> None:
        p = self.params
        ex = self.executor
        book_a = ex.venue(p.venue_a).book(p.symbol)
        book_b = ex.venue(p.venue_b).book(p.symbol)
        if not basis_ok(book_a.mid, book_b.mid, self.max_basis_pct):
            return  # 价差闸门：两所价格差过大不开（防同名不同标的）
        worst_est = min(self._est_corridor("a"), self._est_corridor("b"))
        if worst_est < self.min_corridor_pct:
            return  # 杠杆太高，估算强平距离不足
        # H3：资金费先统一换算到每小时口径再选向/算 edge——
        # HL/Arcus 是每小时费率、Lighter 是 8 小时费率，直接比原始
        # rate 会系统性选错方向
        info_a = ex.venue(p.venue_a).funding(p.symbol)
        info_b = ex.venue(p.venue_b).funding(p.symbol)
        fa = funding_hourly_rate(info_a.rate, info_a.interval_seconds)
        fb = funding_hourly_rate(info_b.rate, info_b.interval_seconds)
        long_leg, short_leg = choose_direction(fa, fb)
        bal = {leg: ex.venue(self._venue_id(leg)).balance().get("available", 0.0)
               for leg in ("a", "b")}
        notional = position_size(bal["a"], bal["b"], p.leverage_a,
                                 p.leverage_b, p)
        if notional <= 0:
            return
        long_mid = book_a.mid if long_leg == "a" else book_b.mid
        qty = notional / long_mid
        if qty <= 0:
            return
        if p.max_position_qty > 0:
            qty = min(qty, p.max_position_qty)  # 首轮也不超过建仓上限
        self._round_qty = qty

        maker_leg = ""
        if p.arcus_maker:
            if p.venue_a == "arcus":
                maker_leg = "a"
            elif p.venue_b == "arcus":
                maker_leg = "b"
        if maker_leg:
            self._place_maker(maker_leg, long_leg, qty, now)
            return

        # 双 taker：先下一腿，失败/未知立即按对账结果处理，不留裸腿
        legs_order = [long_leg, short_leg]
        results = {}
        for leg in legs_order:
            side = Side.BUY if leg == long_leg else Side.SELL
            req = OrderRequest(symbol=p.symbol, side=side, qty=qty, price=None)
            res = ex.place(self.owner, self._venue_id(leg), req,
                           intent="open", corridor_pct=worst_est)
            results[leg] = res
            if res.status in ("rejected", "unknown"):
                break
        legs_now = self._legs_qty()
        if any(abs(q) > 0 for q in legs_now.values()):
            if detect_single_leg(legs_now) or \
                    results.get(legs_order[-1]) is None or \
                    results[legs_order[-1]].status in ("rejected", "unknown"):
                # 只成一腿或第二腿结果不明：按真实持仓把已成腿平掉
                self._close_both(now)
                return
            self._post_open(now)
        # 两腿都没成：留在 idle，下一 tick 再看

    def _place_maker(self, maker_leg: str, long_leg: str, qty: float,
                     now: float) -> None:
        p = self.params
        venue_id = self._venue_id(maker_leg)
        book = self.executor.venue(venue_id).book(p.symbol)
        is_long = maker_leg == long_leg
        price = book.bid if is_long else book.ask
        # S2 对账基线：挂单前该腿持仓量，poll 不可得时用持仓差推断成交
        pos = self.executor.position(venue_id, p.symbol)
        self._maker_pos_before = pos.qty if pos else 0.0
        self._maker_sign = 1.0 if is_long else -1.0
        req = OrderRequest(symbol=p.symbol,
                           side=Side.BUY if is_long else Side.SELL,
                           qty=qty, price=price, post_only=True)
        res = self.executor.place(self.owner, venue_id, req, intent="open",
                                  corridor_pct=self._est_corridor(maker_leg))
        if res.order is not None and res.status in ("open", "partial"):
            # M2：部分成交也进 opening 跟踪，剩余继续等/超时按最终量对冲
            self.phase = "opening"
            self._maker_oid = res.order.order_id
            self._maker_leg = maker_leg
            self._maker_qty = qty
            self._maker_started = now
            self._maker_partial = res.filled_qty
            self._maker_recorded = res.filled_qty
        elif res.status == "filled":
            self._hedge_after_maker(maker_leg, long_leg, qty, now)

    def _hedge_after_maker(self, maker_leg: str, long_leg: str, qty: float,
                           now: float) -> None:
        other = "b" if maker_leg == "a" else "a"
        side = Side.BUY if other == long_leg else Side.SELL
        req = OrderRequest(symbol=self.params.symbol, side=side, qty=qty,
                           price=None)
        self.executor.place(self.owner, self._venue_id(other), req,
                            intent="open",
                            corridor_pct=self._est_corridor(other))
        legs_now = self._legs_qty()
        if detect_single_leg(legs_now):
            # 对冲腿没跟上：平掉 maker 腿，不留裸腿
            self._close_both(now)
            return
        self._maker_oid = None
        self._maker_partial = 0.0
        self._maker_recorded = 0.0
        self._post_open(now)

    def _post_open(self, now: float, reset_timer: bool = True) -> None:
        """开仓后复核：用真实持仓重算强平距离，不合格立即双平。

        H7/M5：走 Executor 公共口径（带 mmr）；任一腿不可评估
        （无强平价且杠杆未知）按不合格处理，双平。
        reset_timer=False 用于逐步建仓的加仓档：只复核、不重置
        本轮的 opened_at / hold_drawn（持有期从首轮开仓起算）。
        """
        corridors = []
        for leg in ("a", "b"):
            vid = self._venue_id(leg)
            mid = self.executor.venue(vid).book(self.params.symbol).mid
            has, c = self.executor.position_corridor(
                vid, self.params.symbol, mid)
            if not has:
                continue
            if c is None:
                self.last_error = f"{vid} 开仓后强平距离不可评估，双平"
                self._close_both(now)
                return
            corridors.append(c)
        worst = min(corridors) if corridors else None
        if worst is not None and worst < self.min_corridor_pct:
            self._close_both(now)
            return
        self.open_corridor = worst
        if reset_timer:
            self.opened_at = now
            self.hold_drawn = draw_hold_seconds(self.params, self.rng)
        self._last_add_at = now
        self.phase = "open"

    def _add_step(self, now: float) -> bool:
        """逐步建仓加一档（max_position_qty>0 时）：沿现有持仓方向
        双腿 taker 同量加仓，单档量经 accumulation_step_qty 截断到
        上限剩余额度。返回 True 表示本 tick 已动作。
        方向由真实持仓符号决定（多腿为正、空腿为负），不再按资金费
        重新选向——建仓中途翻向会破坏对冲结构。"""
        p = self.params
        legs = self._legs_qty()
        qa, qb = legs["a"], legs["b"]
        if qa == 0 or qb == 0 or qa * qb > 0:
            return False  # 无仓/单腿/同向：交给安全检查处理
        long_leg = "a" if qa > 0 else "b"
        short_leg = "b" if long_leg == "a" else "a"
        cur = min(abs(qa), abs(qb))
        ex = self.executor
        long_mid = ex.venue(self._venue_id(long_leg)).book(p.symbol).mid
        step_base = self._round_qty if self._round_qty > 0 else \
            draw_notional(p) / long_mid
        step = accumulation_step_qty(cur, step_base, p.max_position_qty)
        if step <= 1e-12:
            return False
        book_a = ex.venue(p.venue_a).book(p.symbol)
        book_b = ex.venue(p.venue_b).book(p.symbol)
        if not basis_ok(book_a.mid, book_b.mid, self.max_basis_pct):
            return True  # 价差闸门不过：本 tick 不加仓（已决策）
        worst_est = min(self._est_corridor("a"), self._est_corridor("b"))
        if worst_est < self.min_corridor_pct:
            return True  # 强平距离估算不足：不加仓
        for leg in (long_leg, short_leg):
            side = Side.BUY if leg == long_leg else Side.SELL
            req = OrderRequest(symbol=p.symbol, side=side, qty=step,
                               price=None)
            res = ex.place(self.owner, self._venue_id(leg), req,
                           intent="open", corridor_pct=worst_est)
            if res.status in ("rejected", "unknown"):
                break
        legs_now = self._legs_qty()
        if detect_single_leg(legs_now):
            # 加仓只成一腿：按真实持仓双平，不留裸腿
            self._close_both(now)
            return True
        self._post_open(now, reset_timer=False)
        return True

    def _maker_final_filled(self) -> float | None:
        """maker 单的最终成交量：优先 poll 终态；poll 不可得时用
        「持仓 − 挂单前基线」对账推断（S2）。订单仍在挂单中返回 None。"""
        venue_id = self._venue_id(self._maker_leg)
        venue = self.executor.venue(venue_id)
        o = venue.poll_order(self._maker_oid)
        if o is not None:
            if o.status == "open":
                return None
            return o.filled_qty
        try:
            open_ids = {x.order_id for x in venue.open_orders(self.params.symbol)}
        except Exception:  # noqa: BLE001 — 查询失败：保持跟踪
            return None
        if self._maker_oid in open_ids:
            return None
        pos = self.executor.position(venue_id, self.params.symbol)
        cur = pos.qty if pos else 0.0
        delta = (cur - self._maker_pos_before) * self._maker_sign
        return min(max(delta, 0.0), self._maker_qty)

    def _finalize_maker(self, filled: float, now: float) -> None:
        """maker 终局：按最终成交量补记账本并对冲（M2）。"""
        p = self.params
        venue_id = self._venue_id(self._maker_leg)
        oid = self._maker_oid
        inc = filled - self._maker_recorded
        if inc > 1e-9:
            venue = self.executor.venue(venue_id)
            try:
                px = venue.book(p.symbol).mid
            except Exception:  # noqa: BLE001
                px = 0.0
            self.executor.record_fill(
                self.owner, venue_id, p.symbol,
                Side.BUY if self._maker_sign > 0 else Side.SELL,
                inc, px, order_id=oid or "")
        self._maker_oid = None
        self._maker_partial = 0.0
        self._maker_recorded = 0.0
        if filled <= 1e-9:
            self.phase = "idle"
            return
        pos = self.executor.position(venue_id, p.symbol)
        if pos is not None and pos.qty > 0:
            long_leg = self._maker_leg
        else:
            long_leg = "b" if self._maker_leg == "a" else "a"
        self._hedge_after_maker(self._maker_leg, long_leg, filled, now)

    def _tick_opening(self, now: float) -> None:
        """maker 挂单等待阶段（M2 重做）：

        - 部分成交（status=open 且 filled>0）：只更新已成交量，**保留
          跟踪**，绝不提前对冲——等终态或超时撤单确认后按最终量对冲；
        - 终态（filled / canceled 带部分量 / 对账推断终局）：按最终
          成交量一次性对冲；
        - 超时：先撤单，再复查最终成交量，有量按最终量对冲、无量回 idle。
        """
        venue_id = self._venue_id(self._maker_leg)
        venue = self.executor.venue(venue_id)
        o = venue.poll_order(self._maker_oid)
        if o is not None:
            if o.status == "open":
                if o.filled_qty > self._maker_partial:
                    self._maker_partial = o.filled_qty
            elif o.status == "filled":
                self._finalize_maker(
                    o.filled_qty if o.filled_qty > 0 else self._maker_qty, now)
                return
            elif o.status in ("canceled", "rejected"):
                self._finalize_maker(o.filled_qty, now)
                return
        else:
            final = self._maker_final_filled()
            if final is not None:
                self._finalize_maker(final, now)
                return
        if now - self._maker_started > self.params.maker_wait_seconds:
            self.executor.cancel(self.owner, venue_id, self._maker_oid)
            final = self._maker_final_filled()
            if final is None:
                # 撤单未确认终局：用已发现的部分成交量兜底，再不行等下轮
                final = self._maker_partial if self._maker_partial > 0 else None
            if final is not None:
                self._finalize_maker(final, now)

    # ----- 主循环 -----
    def tick(self, now: float = 0.0, allow_open: bool = True) -> None:
        if self.executor is None or self.stopped:
            return
        self._ensure_claims()

        # maker 等成交阶段先于孤腿检查：maker 刚成交、对冲腿未下时
        # 本来就是单腿，若先跑安全检查会把 maker 腿误平掉
        if self.phase == "opening" and self._maker_oid is not None:
            if not allow_open:
                self.executor.cancel(self.owner, self._venue_id(self._maker_leg),
                                     self._maker_oid)
                self._maker_oid = None
                self.phase = "idle"
                if self._legs_qty()[self._maker_leg] != 0:
                    self._close_leg(self._maker_leg)
                    self._finish_round(now)
                return
            self._tick_opening(now)
            return

        if self._safety(now):
            return
        legs = self._legs_qty()
        has_position = legs["a"] != 0 or legs["b"] != 0

        if not allow_open:
            # 停机：双腿全平
            if has_position:
                self._close_both(now)
            return

        if self.phase == "open":
            if not has_position:
                self._finish_round(now)
                return
            if should_rotate(self.opened_at, now, self.hold_drawn):
                self._close_both(now)
                return
            # 逐步建仓：未达 max_position_qty 且间隔已到 → 加一档
            if self.params.max_position_qty > 0 and \
                    now - self._last_add_at >= \
                    self.params.accumulate_interval_seconds:
                self._add_step(now)
            return

        if self.phase == "cooldown" and in_cooldown(
                self.closed_at, now, self.params.cooldown_seconds):
            return
        if not has_position:
            self.phase = "idle"
            self._open_round(now)

    def decide(self, ctx) -> list:
        return []
