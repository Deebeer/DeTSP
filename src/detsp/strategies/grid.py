"""网格策略（骨架）：参数 + 纯函数层级/铺单计划。

逻辑移植自 WGALL（MIT）的 grid.js/bot.js，用 Python 重写。
本批只做纯函数与决策骨架，不接实盘。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..core.venue import Side


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
    """引擎调度用的策略壳：决策细节在后续批次补齐。"""

    name = "grid"

    def __init__(self, params: GridParams) -> None:
        self.params = params

    def decide(self, ctx) -> list:
        return []
