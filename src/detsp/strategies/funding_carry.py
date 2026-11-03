"""资金费 carry 策略：参数 + 纯函数资金费优势计算 + 执行状态机。

来源：Parallax Hedge（ZAIJIN88/Arcus-Entropy-Lighter-，MIT）的
资金费选向模块独立成策略——轮换对冲里它只负责「开仓时选哪边
做多」，这里把它抽出来：不刷量、不轮换，专门选两所资金费差
最大的一组方向，等量双腿长期持有，靠每小时净收的资金费差
赚钱，优势消失（连续多次低于退出门槛）才平仓。

资金费口径：费率先按各自结算周期统一换算到每小时（HL/Arcus
每小时、Lighter/Aster 每 8 小时），再组合：
组合每小时净收（小数）= 空腿小时费率 − 多腿小时费率
（约定费率为正时多头付费给空头：做空高费率所、做多低费率所，
净收为正）。优势以 bps/小时 计。

启动对账口径（本实现的定案）：已有双腿先做净 delta 修正——
双腿反向但不等量时用 reduce-only 削掉多余的一截、只有单腿时
平掉孤腿——修正到等量双腿（或空仓）后接管：方向按真实持仓
推定，之后只继续监控（优势方向与当前方向不一致也不强制平仓，
退出仍只看本组合的净收优势与迟滞规则）。

执行约束与其他双腿策略一致：全部下单走 Executor，归属 =
任务名；孤腿立即平；强平距离任一腿告警走全局风控口径双平；
KillSwitch / 面板 stop 均为撤单 + 双腿全平。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..core.risk import basis_ok, detect_single_leg, funding_hourly_rate
from ..core.venue import OrderRequest, Side

_EPS = 1e-12


@dataclass
class CarryParams:
    venue_a: str
    venue_b: str
    symbol: str
    notional_usd: float                 # 单边开仓金额
    leverage: float = 1.0               # 两腿杠杆（与账户实际一致）
    min_entry_edge_bps_per_hour: float = 1.0   # 入场门槛：组合净收
    # 资金费优势 ≥ 此值（bps/小时）才开仓
    exit_edge_bps_per_hour: float = 0.0        # 退出门槛（< 入场
    # 门槛，构成迟滞，防止在门槛附近反复开平）
    exit_confirm_checks: int = 3        # 优势连续低于退出门槛的
    # 检查次数达到此值才平仓（防单次抖动误平）
    check_seconds: float = 60.0         # 资金费检查周期
    max_hold_seconds: float = 0.0       # 最长持有（秒），0=不限
    reopen: bool = True                 # 平仓后优势恢复是否自动重开
    cooldown_seconds: float = 300.0     # 平仓后的重开冷却

    def __post_init__(self) -> None:
        if self.venue_a == self.venue_b:
            raise ValueError("carry 两腿不能是同一场所")
        if not self.venue_a or not self.venue_b:
            raise ValueError("venue_a / venue_b 必填")
        if self.notional_usd <= 0:
            raise ValueError("notional_usd 必须大于 0")
        if self.min_entry_edge_bps_per_hour <= 0:
            raise ValueError("min_entry_edge_bps_per_hour 必须大于 0")
        if self.exit_edge_bps_per_hour >= self.min_entry_edge_bps_per_hour:
            raise ValueError("exit_edge_bps_per_hour 必须小于入场门槛（迟滞）")
        if self.exit_confirm_checks < 1:
            raise ValueError("exit_confirm_checks 至少为 1")
        if self.check_seconds <= 0:
            raise ValueError("check_seconds 必须大于 0")
        if self.max_hold_seconds < 0 or self.cooldown_seconds < 0:
            raise ValueError("max_hold_seconds / cooldown_seconds 不能为负")


# ═══════════ 纯函数：资金费优势 ═══════════

def carry_edge_bps_per_hour(long_hourly_rate: float,
                            short_hourly_rate: float) -> float:
    """一组方向的组合净收资金费优势（bps/小时，纯函数）。

    入参均为已换算到每小时口径的费率（小数）：净收 =
    空腿费率 − 多腿费率（正费率时空头收、多头付）。
    """
    return (short_hourly_rate - long_hourly_rate) * 10_000


def best_carry_direction(hourly_a: float,
                         hourly_b: float) -> tuple[str, float]:
    """在两个方向里取净收优势更大的一组（纯函数）。

    返回 (多腿, 优势 bps/小时)：多腿取值为 "a" / "b"——
    多腿永远在费率低的一侧、空腿在费率高的一侧；两边相等时
    默认多 a 空 b，优势为 0。
    """
    edge_a_long = carry_edge_bps_per_hour(hourly_a, hourly_b)   # 多 a 空 b
    edge_b_long = carry_edge_bps_per_hour(hourly_b, hourly_a)   # 多 b 空 a
    if edge_b_long > edge_a_long:
        return "b", edge_b_long
    return "a", edge_a_long


def current_edge_bps_per_hour(long_leg: str, hourly_a: float,
                              hourly_b: float) -> float:
    """已持方向的当前净收优势（bps/小时）：long_leg 为 "a" 时
    多 a 空 b，为 "b" 时多 b 空 a；费率反转时该值为负。"""
    if long_leg == "a":
        return carry_edge_bps_per_hour(hourly_a, hourly_b)
    return carry_edge_bps_per_hour(hourly_b, hourly_a)


class FundingCarryStrategy:
    """资金费 carry 执行状态机。

    阶段：flat（空仓等入场）→ open（双腿持有吃费）→ 平仓后
    cooldown（冷却结束且 reopen=True 才允许再入场）。
    - 检查节奏：资金费判断只在 check_seconds 周期到时做；安全
      检查（孤腿/强平距离）每 tick 都做，安全永远优先
    - 开仓：双腿 taker 等量，先下一腿、失败或结果未知立即按
      真实持仓把已成腿平掉，不留裸腿（与轮换对冲同一口径）
    - 退出：优势连续 exit_confirm_checks 次低于退出门槛才双平；
      max_hold_seconds 到期也双平；平仓后进冷却
    """

    name = "funding_carry"
    stop_note = "已停止：双腿挂单已撤、双腿持仓已平"

    def __init__(self, params: CarryParams, executor=None, risk_cfg=None,
                 owner: str | None = None) -> None:
        self.params = params
        self.executor = executor
        # 归属 owner = 任务名（config 的 name）；未给时退回类型名
        self.owner = owner or self.name
        self.min_corridor_pct = getattr(risk_cfg, "min_corridor_pct", 3.0)
        self.close_corridor_pct = getattr(risk_cfg, "close_corridor_pct", 1.5)
        self.max_basis_pct = 3.0
        self.phase = "flat"          # flat / open / cooldown
        self.long_leg: str = ""      # 当前持仓的多腿："a" / "b"
        self.opened_at = 0.0
        self.cooldown_until = -1e18
        self.stopped = False
        self._claimed = False
        self._last_check = -1e18
        self._below_exit_count = 0
        self._opened_once = False    # reopen=False 时用过即止的标记
        self.last_error: str = ""
        self.recovery_notes: list[str] = []  # 启动对账事件

    # ----- 基础 -----
    def _venue_id(self, leg: str) -> str:
        return self.params.venue_a if leg == "a" else self.params.venue_b

    def _ensure_claims(self) -> None:
        if not self._claimed:
            self.executor.claim(self.owner, self.params.venue_a,
                                self.params.symbol)
            self.executor.claim(self.owner, self.params.venue_b,
                                self.params.symbol)
            self._claimed = True

    def _legs_qty(self) -> dict[str, float]:
        out = {}
        for leg in ("a", "b"):
            pos = self.executor.position(self._venue_id(leg),
                                         self.params.symbol)
            out[leg] = pos.qty if pos else 0.0
        return out

    def _hourly_rates(self) -> tuple[float, float]:
        """两腿资金费率，统一换算到每小时口径。"""
        ex = self.executor
        out = []
        for leg in ("a", "b"):
            info = ex.venue(self._venue_id(leg)).funding(self.params.symbol)
            out.append(funding_hourly_rate(info.rate, info.interval_seconds))
        return out[0], out[1]

    def _close_both(self) -> None:
        for leg in ("a", "b"):
            self.executor.close_position(self.owner, self._venue_id(leg),
                                         self.params.symbol)
        self.long_leg = ""
        self._below_exit_count = 0

    def _enter_cooldown(self, now: float) -> None:
        self.phase = "cooldown"
        self.cooldown_until = now + self.params.cooldown_seconds
        self._last_check = now

    # ----- 生命周期 -----
    def start(self, now: float = 0.0) -> None:
        if self.executor is None:
            raise RuntimeError("FundingCarryStrategy 未注入 executor（骨架模式）")
        self._ensure_claims()
        self.stopped = False
        legs = self._legs_qty()
        qa, qb = legs["a"], legs["b"]
        if qa == 0 and qb == 0:
            return
        # 启动对账（定案口径）：先净 delta 修正再接管——
        # 孤腿平掉；双腿反向不等量削掉多余的一截
        if qa != 0 and qb != 0 and qa * qb < 0 and abs(abs(qa) - abs(qb)) > _EPS:
            big = "a" if abs(qa) > abs(qb) else "b"
            excess = abs(legs[big]) - abs(legs["b" if big == "a" else "a"])
            self.executor.close_position(
                self.owner, self._venue_id(big), self.params.symbol,
                qty=excess)
            self.recovery_notes.append(
                f"启动对账：双腿不等量（a={qa} b={qb}），已削掉 "
                f"{big} 腿多余的 {excess} 后接管")
            legs = self._legs_qty()
            qa, qb = legs["a"], legs["b"]
        elif qa == 0 or qb == 0 or qa * qb > 0:
            # 孤腿或双腿同向（方向错乱）：平掉残留回空仓，不接管裸腿
            self._close_both()
            self.recovery_notes.append(
                f"启动对账：发现孤腿/同向持仓（a={qa} b={qb}），"
                f"已平掉残留，回空仓等待入场")
            return
        if qa != 0 and qb != 0:
            # 等量双腿接管：方向按真实持仓推定，只继续监控，
            # 不因当前优势方向不同而强制平仓
            self.long_leg = "a" if qa > 0 else "b"
            self.phase = "open"
            self.opened_at = now
            self._opened_once = True
            ha, hb = self._hourly_rates()
            edge_now = current_edge_bps_per_hour(self.long_leg, ha, hb)
            best_leg, best_edge = best_carry_direction(ha, hb)
            note = (f"启动对账：接管已有双腿 a={qa} b={qb}（多 "
                    f"{self.long_leg} 腿），当前净收优势 "
                    f"{edge_now:.2f} bps/小时，继续监控")
            if best_leg != self.long_leg:
                note += (f"；注意当前最优方向为多 {best_leg} 腿"
                         f"（{best_edge:.2f} bps/小时），与持仓方向相反，"
                         f"按退出迟滞规则处理，不强制平仓")
            self.recovery_notes.append(note)

    def stop(self) -> None:
        """面板停止：撤自有挂单 + 双腿全平。"""
        if self.executor is None:
            return
        self._ensure_claims()
        for leg in ("a", "b"):
            self.executor.cancel_all_own(self.owner, self._venue_id(leg),
                                         self.params.symbol)
        self._close_both()
        self.phase = "flat"
        self.stopped = True

    # ----- 安全检查（每 tick 最先执行） -----
    def _safety(self, now: float) -> bool:
        """返回 True 表示已处理安全事件，本 tick 不再做其他事。

        只在持有期有意义：孤腿立即平剩余腿回冷却；任一腿强平
        距离跌破平仓线双平；不可评估时不平仓（无从判断），但
        置 last_error 并阻止本 tick 的新开仓（由返回值语义外
        的 block 标记传递——这里直接返回 True 跳过本 tick）。
        """
        legs = self._legs_qty()
        qa, qb = legs["a"], legs["b"]
        if qa == 0 and qb == 0:
            if self.phase == "open":
                # 双腿已被外部平掉：回冷却，不再假设持有
                self.phase = "cooldown"
                self.cooldown_until = now + self.params.cooldown_seconds
                self.long_leg = ""
            return False
        if detect_single_leg(legs):
            if qa != 0 and qb != 0 and qa * qb < 0:
                big = "a" if abs(qa) > abs(qb) else "b"
                small = "b" if big == "a" else "a"
                self.executor.close_position(
                    self.owner, self._venue_id(big), self.params.symbol,
                    qty=abs(legs[big]) - abs(legs[small]))
                return True
            # 孤腿或同向错乱：平掉残留，回冷却
            self._close_both()
            self._enter_cooldown(now)
            self.last_error = "检测到孤腿/同向持仓，已平掉残留腿"
            return True
        for leg in ("a", "b"):
            vid = self._venue_id(leg)
            mid = self.executor.venue(vid).book(self.params.symbol).mid
            has, corr = self.executor.position_corridor(
                vid, self.params.symbol, mid)
            if not has:
                continue
            if corr is None:
                self.last_error = (f"{vid} 强平距离不可评估"
                                   f"（无强平价且杠杆未知），本 tick 不动作")
                return True
            if corr < self.close_corridor_pct:
                self.last_error = (f"{vid} 强平距离 {corr:.2f}% 跌破平仓线 "
                                   f"{self.close_corridor_pct}%，双腿平仓")
                self._close_both()
                self._enter_cooldown(now)
                return True
        return False

    # ----- 开仓 -----
    def _est_corridor(self, leg: str) -> float:
        return self.executor.estimate_open_corridor(
            self.params.leverage,
            self.executor.market_mmr(self._venue_id(leg), self.params.symbol))

    def _open_pair(self, long_leg: str, now: float) -> None:
        p = self.params
        ex = self.executor
        short_leg = "b" if long_leg == "a" else "a"
        book_a = ex.venue(p.venue_a).book(p.symbol)
        book_b = ex.venue(p.venue_b).book(p.symbol)
        if not basis_ok(book_a.mid, book_b.mid, self.max_basis_pct):
            return  # 价差闸门：两所价格差过大不开（防同名不同标的）
        worst_est = min(self._est_corridor("a"), self._est_corridor("b"))
        if worst_est < self.min_corridor_pct:
            return  # 杠杆太高，估算强平距离不足
        long_mid = book_a.mid if long_leg == "a" else book_b.mid
        qty = p.notional_usd / long_mid
        if qty <= 0:
            return
        # 双 taker 等量：先下一腿，失败/未知立即停手，按真实持仓收尾
        results = {}
        for leg in (long_leg, short_leg):
            side = Side.BUY if leg == long_leg else Side.SELL
            req = OrderRequest(symbol=p.symbol, side=side, qty=qty,
                               price=None)
            res = ex.place(self.owner, self._venue_id(leg), req,
                           intent="open", corridor_pct=worst_est)
            results[leg] = res
            if res.status in ("rejected", "unknown"):
                break
        legs_now = self._legs_qty()
        if any(abs(q) > 0 for q in legs_now.values()):
            if detect_single_leg(legs_now) or \
                    results.get(short_leg) is None or \
                    results[short_leg].status in ("rejected", "unknown"):
                # 只成一腿或第二腿结果不明：按真实持仓双平，不留裸腿
                self._close_both()
                self.last_error = "开仓只成一腿，已按真实持仓平掉"
                return
            self.long_leg = long_leg
            self.opened_at = now
            self._below_exit_count = 0
            self._opened_once = True
            self.phase = "open"
        # 两腿都没成：留在 flat，下一检查周期再看

    # ----- 主循环 -----
    def tick(self, now: float = 0.0, allow_open: bool = True) -> None:
        if self.executor is None or self.stopped:
            return
        self._ensure_claims()
        p = self.params

        if not allow_open:
            # 全局停机：双腿全平（安全动作优先）
            legs = self._legs_qty()
            if legs["a"] != 0 or legs["b"] != 0:
                self._close_both()
                self._enter_cooldown(now)
            return

        if self._safety(now):
            return

        if self.phase == "open":
            if p.max_hold_seconds > 0 and \
                    now - self.opened_at >= p.max_hold_seconds:
                self._close_both()
                self._enter_cooldown(now)
                return
            if now - self._last_check < p.check_seconds:
                return
            self._last_check = now
            ha, hb = self._hourly_rates()
            edge = current_edge_bps_per_hour(self.long_leg, ha, hb)
            if edge < p.exit_edge_bps_per_hour:
                self._below_exit_count += 1
            else:
                self._below_exit_count = 0
            if self._below_exit_count >= p.exit_confirm_checks:
                self._close_both()
                self._enter_cooldown(now)
            return

        if self.phase == "cooldown":
            if now < self.cooldown_until:
                return
            self.phase = "flat"

        # flat：等入场检查周期
        if self._opened_once and not p.reopen:
            return  # reopen=False：平过一次就不再自动重开
        if now - self._last_check < p.check_seconds:
            return
        self._last_check = now
        ha, hb = self._hourly_rates()
        long_leg, edge = best_carry_direction(ha, hb)
        if edge >= p.min_entry_edge_bps_per_hour:
            self._open_pair(long_leg, now)

    def decide(self, ctx) -> list:
        return []
