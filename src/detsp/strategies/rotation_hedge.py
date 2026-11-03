"""轮换对冲刷量策略（骨架）：参数 + 纯函数决策。

移植自 Parallax Hedge（MIT）：双腿反向等量、定时轮换、
按资金费选向、强平距离风控、孤腿抢救。
本批只做纯函数与决策骨架，不接实盘。
"""

from __future__ import annotations

import random
from dataclasses import dataclass


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
    name = "rotation_hedge"

    def __init__(self, params: RotationParams) -> None:
        self.params = params

    def decide(self, ctx) -> list:
        return []
