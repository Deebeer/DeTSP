"""轮换对冲刷量策略（骨架）：参数 + 纯函数决策。

移植自 Parallax Hedge（MIT）：双腿反向等量、定时轮换、
按资金费选向、强平距离风控、孤腿抢救。
本批只做纯函数与决策骨架，不接实盘。
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from ..core.risk import (
    basis_ok,
    detect_single_leg,
    position_corridor_pct,
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

    def __post_init__(self) -> None:
        if self.venue_a == self.venue_b:
            raise ValueError("对冲两腿不能是同一场所")
        if self.notional_usd <= 0:
            raise ValueError("notional_usd 必须大于 0")


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

    def __init__(self, params: RotationParams, executor=None, risk_cfg=None,
                 rng: random.Random | None = None) -> None:
        self.params = params
        self.executor = executor
        self.rng = rng
        self.min_corridor_pct = getattr(risk_cfg, "min_corridor_pct", 3.0)
        self.close_corridor_pct = getattr(risk_cfg, "close_corridor_pct", 1.5)
        self.max_basis_pct = 3.0
        self.phase = "idle"          # idle / opening / open / cooldown
        self.opened_at = 0.0
        self.closed_at = -1e18
        self.hold_drawn = params.hold_seconds
        self.open_corridor: float | None = None
        self._claimed = False
        self._maker_oid: str | None = None
        self._maker_leg: str = ""    # "a" / "b"
        self._maker_qty = 0.0
        self._maker_started = 0.0
        self.last_error: str = ""

    # ----- 基础 -----
    def _venue_id(self, leg: str) -> str:
        return self.params.venue_a if leg == "a" else self.params.venue_b

    def _leverage(self, leg: str) -> float:
        return self.params.leverage_a if leg == "a" else self.params.leverage_b

    def _ensure_claims(self) -> None:
        if not self._claimed:
            self.executor.claim(self.name, self.params.venue_a, self.params.symbol)
            self.executor.claim(self.name, self.params.venue_b, self.params.symbol)
            self._claimed = True

    def start(self, now: float = 0.0) -> None:
        if self.executor is None:
            raise RuntimeError("RotationHedgeStrategy 未注入 executor（骨架模式）")
        self._ensure_claims()

    def _legs_qty(self) -> dict[str, float]:
        out = {}
        for leg in ("a", "b"):
            pos = self.executor.position(self._venue_id(leg), self.params.symbol)
            out[leg] = pos.qty if pos else 0.0
        return out

    def _close_leg(self, leg: str, qty: float | None = None) -> None:
        self.executor.close_position(self.name, self._venue_id(leg),
                                     self.params.symbol, qty=qty)

    def _finish_round(self, now: float) -> None:
        self.phase = "cooldown"
        self.closed_at = now
        self.open_corridor = None
        self._maker_oid = None

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
        # 强平距离监控
        corridors = []
        for leg in ("a", "b"):
            pos = self.executor.position(self._venue_id(leg), self.params.symbol)
            if pos is None or pos.qty == 0:
                continue
            mid = self.executor.venue(self._venue_id(leg)).book(
                self.params.symbol).mid
            c = position_corridor_pct(mid, pos)
            if c is not None:
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
        """开仓前估算强平距离（%）：按杠杆近似 100/杠杆。"""
        return 100.0 / max(self._leverage(leg), 1e-9)

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
        fa = ex.venue(p.venue_a).funding(p.symbol).rate
        fb = ex.venue(p.venue_b).funding(p.symbol).rate
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
            res = ex.place(self.name, self._venue_id(leg), req,
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
        req = OrderRequest(symbol=p.symbol,
                           side=Side.BUY if is_long else Side.SELL,
                           qty=qty, price=price, post_only=True)
        res = self.executor.place(self.name, venue_id, req, intent="open",
                                  corridor_pct=self._est_corridor(maker_leg))
        if res.order is not None and res.status == "open":
            self.phase = "opening"
            self._maker_oid = res.order.order_id
            self._maker_leg = maker_leg
            self._maker_qty = qty
            self._maker_started = now
        elif res.status == "filled":
            self._hedge_after_maker(maker_leg, long_leg, qty, now)

    def _hedge_after_maker(self, maker_leg: str, long_leg: str, qty: float,
                           now: float) -> None:
        other = "b" if maker_leg == "a" else "a"
        side = Side.BUY if other == long_leg else Side.SELL
        req = OrderRequest(symbol=self.params.symbol, side=side, qty=qty,
                           price=None)
        self.executor.place(self.name, self._venue_id(other), req,
                            intent="open",
                            corridor_pct=self._est_corridor(other))
        legs_now = self._legs_qty()
        if detect_single_leg(legs_now):
            # 对冲腿没跟上：平掉 maker 腿，不留裸腿
            self._close_both(now)
            return
        self._maker_oid = None
        self._post_open(now)

    def _post_open(self, now: float) -> None:
        """开仓后复核：用真实持仓重算强平距离，不合格立即双平。"""
        corridors = []
        for leg in ("a", "b"):
            pos = self.executor.position(self._venue_id(leg), self.params.symbol)
            if pos is None or pos.qty == 0:
                continue
            mid = self.executor.venue(self._venue_id(leg)).book(
                self.params.symbol).mid
            c = position_corridor_pct(mid, pos)
            if c is not None:
                corridors.append(c)
        worst = min(corridors) if corridors else None
        if worst is not None and worst < self.min_corridor_pct:
            self._close_both(now)
            return
        self.open_corridor = worst
        self.opened_at = now
        self.hold_drawn = draw_hold_seconds(self.params, self.rng)
        self.phase = "open"

    def _tick_opening(self, now: float) -> None:
        """maker 挂单等待阶段：成交→立即 taker 对冲；超时→撤单回 idle。"""
        venue = self.executor.venue(self._venue_id(self._maker_leg))
        o = venue.poll_order(self._maker_oid)
        if o is not None and (o.status == "filled" or o.filled_qty > 0):
            filled = o.filled_qty if o.filled_qty > 0 else self._maker_qty
            # maker 成交在下单后发生，补记账本
            self.executor.record_fill(
                self.name, self._venue_id(self._maker_leg), self.params.symbol,
                o.side, filled,
                o.price if o.price is not None else 0.0, order_id=self._maker_oid)
            # maker 腿的持仓方向决定哪条腿是多头
            pos = self.executor.position(self._venue_id(self._maker_leg),
                                         self.params.symbol)
            if pos is not None and pos.qty > 0:
                long_leg = self._maker_leg
            else:
                long_leg = "b" if self._maker_leg == "a" else "a"
            self._maker_oid = None
            self._hedge_after_maker(self._maker_leg, long_leg, filled, now)
            return
        if o is not None and o.status in ("canceled", "rejected"):
            self._maker_oid = None
            self.phase = "idle"
            return
        if now - self._maker_started > self.params.maker_wait_seconds:
            self.executor.cancel(self.name, self._venue_id(self._maker_leg),
                                 self._maker_oid)
            self._maker_oid = None
            self.phase = "idle"

    # ----- 主循环 -----
    def tick(self, now: float = 0.0, allow_open: bool = True) -> None:
        if self.executor is None:
            return
        self._ensure_claims()

        # maker 等成交阶段先于孤腿检查：maker 刚成交、对冲腿未下时
        # 本来就是单腿，若先跑安全检查会把 maker 腿误平掉
        if self.phase == "opening" and self._maker_oid is not None:
            if not allow_open:
                self.executor.cancel(self.name, self._venue_id(self._maker_leg),
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

        if self.phase == "cooldown" and in_cooldown(
                self.closed_at, now, self.params.cooldown_seconds):
            return
        if not has_position:
            self.phase = "idle"
            self._open_round(now)

    def decide(self, ctx) -> list:
        return []
