"""溢价套利策略（骨架）：参数 + 纯函数溢价带计算。

移植自 entropy-arb（MIT）：溢价中枢带、扣费后可成交价门槛、
信号持续门、库存阶梯。本批只做纯函数与决策骨架，不接实盘。

约定：premium 以基准腿（base，如 Entropy）相对对冲腿（hedge）计：
premium_bps = (base 价 / hedge 价 − 1) × 10000。
"""

from __future__ import annotations

from dataclasses import dataclass


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

    def __post_init__(self) -> None:
        if self.upper_bps <= 0 or self.lower_bps <= 0:
            raise ValueError("upper_bps / lower_bps 必须大于 0")
        if not 0 < self.take_fraction <= 1:
            raise ValueError("take_fraction 须在 (0, 1]")

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


class PremiumArbStrategy:
    name = "premium_arb"

    def __init__(self, params: PremiumParams) -> None:
        self.params = params

    def decide(self, ctx) -> list:
        return []
