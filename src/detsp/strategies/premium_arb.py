"""溢价套利策略：参数 + 纯函数溢价带计算 + 执行状态机。

移植自 entropy-arb（MIT）：溢价中枢带、扣费后可成交价门槛、
信号持续门、库存阶梯。已接统一执行层（core.execution.Executor），
归属 owner = 任务名（H4）。审计修复（2026-11-03）：tick 增加持仓
强平距离监控、开仓双腿带 corridor 估算（H7，公共口径带 mmr）；
启动按已有持仓对账初始化（S3）。

约定：premium 以基准腿（base，如 Entropy）相对对冲腿（hedge）计：
premium_bps = (base 价 / hedge 价 − 1) × 10000。
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
    # 执行层字段（默认值不影响纯函数测试）
    symbol: str = "BTC"
    base_venue: str = ""        # 基准腿 venue_id（如 entropy）
    hedge_venue: str = ""       # 对冲腿 venue_id（如 lighter）
    reconcile_seconds: float = 15.0  # 与场所持仓对账周期
    leverage_base: float = 1.0  # 两腿杠杆（H7：开仓 corridor 估算用；
    leverage_hedge: float = 1.0  # 交易所不由本程序设置杠杆，须与账户实际一致）

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
    """溢价套利执行状态机（移植 entropy-arb engine 的执行规则）。

    - 信号：executable_signal（可成交价、扣双边费）+ 库存阶梯附加门槛；
      信号须持续 persist_seconds 才触发（防单 tick 幻影溢价）
    - 执行：双腿 taker 先后紧贴发出（同步引擎内等价于并发），
      单笔切片按 max_order_notional_usd，仓位受 max_position_usd 约束
    - 对账：每 reconcile_seconds 与场所真实持仓核对；净 delta 不平
      立即以 reduce-only 市价单修正（只减多出的那条腿）
    - 反向穿越溢价带即反向成交：净仓位自然被削减/翻转，完成往返
    - 停机信号（allow_open=False）：双腿全平停机
    """

    name = "premium_arb"

    def __init__(self, params: PremiumParams, executor=None,
                 owner: str | None = None) -> None:
        self.params = params
        self.executor = executor
        # H4：归属 owner = 任务名（config 的 name）；未给时退回类型名
        self.owner = owner or self.name
        self._claimed = False
        self._armed: dict[str, float] = {}   # 信号 -> 首次出现时间
        self._last_reconcile = -1e18
        self.last_error: str = ""
        self.recovery_notes: list[str] = []  # S3 启动对账事件

    def start(self, now: float = 0.0) -> None:
        if self.executor is None:
            raise RuntimeError("PremiumArbStrategy 未注入 executor（骨架模式）")
        self._ensure_claims()
        # S3 启动恢复对账：已有持仓不假设是本进程开的——记录事件，
        # 净 delta 由 tick 的定期对账（_fix_delta）自动修正，
        # 强平距离由 tick 监控接管
        qb = self._pos_qty(self.params.base_venue)
        qh = self._pos_qty(self.params.hedge_venue)
        if qb != 0 or qh != 0:
            self.recovery_notes.append(
                f"启动对账：发现已有持仓 base={qb} hedge={qh}，"
                f"由定期对账与强平监控接管")

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

    def _fire(self, signal: str) -> None:
        p = self.params
        ex = self.executor
        base_book = ex.venue(p.base_venue).book(p.symbol)
        # 切片：Book 无深度数据，用单笔上限作为切片名义（见 DESIGN 限制）
        notional = slice_notional(p.max_order_notional_usd / max(p.take_fraction, 1e-9), p)
        qty = notional / base_book.mid
        # 仓位上限：加仓后 base 腿名义不超过 max_position_usd（减仓不受限）
        base_qty = self._pos_qty(p.base_venue)
        if signal == "sell_base":
            base_req = OrderRequest(p.symbol, Side.SELL, qty, price=None)
            hedge_req = OrderRequest(p.symbol, Side.BUY, qty, price=None)
            adds = base_qty <= 0
        else:
            base_req = OrderRequest(p.symbol, Side.BUY, qty, price=None)
            hedge_req = OrderRequest(p.symbol, Side.SELL, qty, price=None)
            adds = base_qty >= 0
        if adds and abs(base_qty * base_book.mid) + notional > p.max_position_usd:
            return
        # H7：双腿开仓都带 corridor 估算（公共口径带 mmr），
        # 任一腿估算低于门槛会被 Executor 拒绝，事后 _fix_delta 修正
        corr_base = ex.estimate_open_corridor(
            p.leverage_base, ex.market_mmr(p.base_venue, p.symbol))
        corr_hedge = ex.estimate_open_corridor(
            p.leverage_hedge, ex.market_mmr(p.hedge_venue, p.symbol))
        ex.place(self.owner, p.base_venue, base_req, intent="open",
                 corridor_pct=corr_base)
        ex.place(self.owner, p.hedge_venue, hedge_req, intent="open",
                 corridor_pct=corr_hedge)
        # 双腿成交量可能不等（部分成交）：立即按对账结果修正净 delta
        self._fix_delta()
        self._last_reconcile = -1e18  # 下一 tick 先对账确认

    def tick(self, now: float = 0.0, allow_open: bool = True) -> None:
        if self.executor is None:
            return
        self._ensure_claims()
        p = self.params
        ex = self.executor

        if not allow_open:
            # 停机：双腿全平
            ex.close_position(self.owner, p.base_venue, p.symbol)
            ex.close_position(self.owner, p.hedge_venue, p.symbol)
            self._armed.clear()
            return

        # 定期对账：净 delta 不平立即修正（安全动作，先于开仓信号）
        if now - self._last_reconcile >= p.reconcile_seconds:
            self._last_reconcile = now
            self._fix_delta()

        base_book = ex.venue(p.base_venue).book(p.symbol)
        hedge_book = ex.venue(p.hedge_venue).book(p.symbol)

        # H7：持仓强平距离监控（公共口径）：任一腿跌破门槛 → 双平；
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
            self._fire(signal)

    def decide(self, ctx) -> list:
        return []
