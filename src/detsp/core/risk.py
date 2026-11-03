"""全局风控：纯函数优先。

安全动作永远压过交易动作。本模块不碰网络、不读配置全局变量，
所有判断都可单测。
"""

from __future__ import annotations

from .venue import Position, Side


class RiskHaltError(RuntimeError):
    """全局停机已触发，拒绝新开仓。"""


def liquidation_corridor_pct(price: float, liq_price: float, side: Side) -> float:
    """强平距离（%）：当前价离强平价还有多少。

    多头：(price - liq) / price；空头：(liq - price) / price。
    """
    if price <= 0:
        raise ValueError("price 必须大于 0")
    if side is Side.BUY:
        return (price - liq_price) / price * 100
    return (liq_price - price) / price * 100


def worst_corridor_pct(corridors: list[float]) -> float | None:
    """多条腿取最近（最小）的强平距离；无腿时返回 None。"""
    return min(corridors) if corridors else None


def corridor_ok(corridor_pct: float, min_pct: float) -> bool:
    """开仓门槛：强平距离不低于 min_pct 才允许开仓。"""
    return corridor_pct >= min_pct


def should_force_close(corridor_pct: float, open_corridor_pct: float, close_pct: float) -> bool:
    """持仓监控平仓线：低于 close_pct，或只剩开仓时一半，触发双腿同平。"""
    return corridor_pct < close_pct or corridor_pct < open_corridor_pct / 2


def estimate_liq_price(entry_price: float, leverage: float, side: Side,
                       maint_margin_rate: float = 0.0) -> float:
    """按杠杆估算强平价（交易所不给 liq_price 时用）。

    强平距离比例 ≈ 1/杠杆 − 维持保证金率；多头强平价在入场价下方，
    空头在上方。杠杆<=1 时多头强平价<=0（不会强平）。
    """
    if entry_price <= 0:
        raise ValueError("entry_price 必须大于 0")
    lev = max(leverage, 1e-9)
    dist = max(1.0 / lev - maint_margin_rate, 0.0)
    if side is Side.BUY:
        return entry_price * (1 - dist)
    return entry_price * (1 + dist)


def position_corridor_pct(current_price: float, pos: Position,
                          maint_margin_rate: float = 0.0) -> float | None:
    """持仓的强平距离（%）：优先用交易所给的 liq_price，否则按杠杆估算。

    空仓返回 None。方向由持仓数量符号决定（多头看跌破，空头看涨破）。
    """
    if pos.qty == 0:
        return None
    side = Side.BUY if pos.qty > 0 else Side.SELL
    liq = pos.liq_price
    if liq is None:
        if pos.entry_price <= 0:
            return None
        liq = estimate_liq_price(pos.entry_price, pos.leverage, side,
                                 maint_margin_rate)
    return liquidation_corridor_pct(current_price, liq, side)


def basis_ok(price_a: float, price_b: float, max_diff_pct: float = 3.0) -> bool:
    """价差闸门：两所中价相差超过 max_diff_pct 不开仓（防同名不同标的）。"""
    if price_a <= 0 or price_b <= 0:
        return False
    return abs(price_a / price_b - 1) * 100 <= max_diff_pct


def detect_single_leg(legs: dict[str, float]) -> bool:
    """单腿检测：对冲仓位里恰好只有一条腿有持仓。

    legs：{场所: 带符号数量}。两条都空/都有不算单腿；
    数量明显不平（差超过较小腿的 10%）也视为不平衡需处理。
    """
    nonzero = {v: q for v, q in legs.items() if abs(q) > 0}
    if len(nonzero) == 1 and len(legs) >= 2:
        return True
    if len(nonzero) >= 2:
        qtys = [abs(q) for q in nonzero.values()]
        if max(qtys) - min(qtys) > min(qtys) * 0.1:
            return True
    return False


def net_delta(legs: dict[str, float]) -> float:
    """对冲组合的净 delta（带符号数量之和，理想为 0）。"""
    return sum(legs.values())


def within_cap(notional: float, cap: float) -> bool:
    """仓位上限检查；cap<=0 表示不限。"""
    return cap <= 0 or notional <= cap


def check_caps(position: Position, venue_cap: float, strategy_cap: float) -> bool:
    n = position.notional
    return within_cap(n, venue_cap) and within_cap(n, strategy_cap)


class KillSwitch:
    """全局停机：触发后只允许撤单/平仓，拒绝一切新开仓。"""

    def __init__(self) -> None:
        self._halted = False
        self._reason = ""

    @property
    def halted(self) -> bool:
        return self._halted

    @property
    def reason(self) -> str:
        return self._reason

    def halt(self, reason: str = "") -> None:
        self._halted = True
        self._reason = reason

    def reset(self) -> None:
        self._halted = False
        self._reason = ""

    def assert_allowed(self) -> None:
        if self._halted:
            raise RiskHaltError(f"全局停机中：{self._reason}")


class ErrorCounter:
    """连续错误计数：达阈值触发停机（借鉴 entropy-arb 的失败收容）。"""

    def __init__(self, max_consecutive: int = 3) -> None:
        self.max_consecutive = max_consecutive
        self._count = 0

    def record_error(self) -> bool:
        """记录一次错误，返回是否已达停机阈值。"""
        self._count += 1
        return self._count >= self.max_consecutive

    def record_success(self) -> None:
        self._count = 0

    @property
    def count(self) -> int:
        return self._count
