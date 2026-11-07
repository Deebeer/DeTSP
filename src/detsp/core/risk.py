"""全局风控：纯函数优先。

安全动作永远压过交易动作。本模块不碰网络、不读配置全局变量，
所有判断都可单测。
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from .venue import Position, Side

# 交易所未提供维持保证金率时的保守默认（5%）：强平距离估算统一用它，
# 宁可低估距离早拦截，不可按 mmr=0 乐观放行（M5）
DEFAULT_MMR = 0.05


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
    liq_price 缺失且杠杆未知（leverage_known=False，如 Lighter/Arcus
    持仓接口不给杠杆）时返回 None = 不可评估——调用方必须按
    「不可评估就阻止开仓/触发安全动作」处理，禁止当作安全放行（M5）。
    """
    if pos.qty == 0:
        return None
    side = Side.BUY if pos.qty > 0 else Side.SELL
    liq = pos.liq_price
    if liq is None:
        if not pos.leverage_known or pos.entry_price <= 0:
            return None
        liq = estimate_liq_price(pos.entry_price, pos.leverage, side,
                                 maint_margin_rate)
    return liquidation_corridor_pct(current_price, liq, side)


def funding_hourly_rate(rate: float, interval_seconds: float) -> float:
    """资金费率统一换算到每小时口径（H3）：rate 是 interval_seconds
    周期内的费率。HL/Arcus 每小时结算（interval=3600，原样），
    Lighter/Aster 每 8 小时（interval=28800，须除以 8）。"""
    if interval_seconds <= 0:
        return rate
    return rate * 3600.0 / interval_seconds


# ── 资金费数据防呆（移植 Parallax funding.py 的三道防护）──────
#
# Parallax 文件头自述：费率是「最容易出错、也最致命的一块」。
# 审查 H1/H2/L5 对照确认 DeTSP 此前只有归一换算一道，这里补齐：
# 1. 合理性闸门：单期费率超 5% 几乎必然是单位解析错误 → 判数据
#    错误、拒绝按它开仓（「宁可不开，不能按错的方向开」）；
# 2. 取值口径一致性：下期预测 / 当期 / 上期已结算不是同一时点的
#    量，混着相减会在变盘点选反——口径不一致时不选向；
# 3. 周期自检（detect_period_anomaly）：用全市场地板价年化反查
#    结算周期假设，防「8 小时当 1 小时」这类整倍数错误。

# 单期费率合理上限（小数）：Lighter 协议硬顶 4%/8h、HL 每小时
# 也远达不到 5%，超过即判单位解析错误（Parallax 同口径）
SANE_RATE_PER_INTERVAL = 0.05

# 地板价至少占这么多市场，才认为它确实是基准利率而非巧合
_FLOOR_SHARE_THRESHOLD = 0.30
# 地板价隐含年化超过这个数（%）就报警：真实基准利率通常 <10%
_FLOOR_APR_WARN_PCT = 15.0
_SECONDS_PER_YEAR = 365 * 24 * 3600


def funding_rate_suspect(rate: float) -> bool:
    """单期费率是否超出合理区间（疑似单位/解析错误）。"""
    return abs(rate) > SANE_RATE_PER_INTERVAL


def evaluate_funding_pair(info_a, info_b) -> tuple[bool, str, float, float]:
    """开仓选向前的一对费率评估（funding_carry 选向用）。

    返回 (可否选向, 原因, a 的每小时费率, b 的每小时费率)。
    任一腿触发合理性闸门、或两腿取值口径（rate_kind）不一致，
    都返回不可选向——调用方应拒绝开仓并记录原因，而不是拿
    可疑/异口径数据选方向。
    """
    ha = funding_hourly_rate(info_a.rate, info_a.interval_seconds)
    hb = funding_hourly_rate(info_b.rate, info_b.interval_seconds)
    for label, info in (("a", info_a), ("b", info_b)):
        if funding_rate_suspect(info.rate):
            return False, (
                f"{label} 腿单期费率 {info.rate * 100:.3f}% 超出合理区间 "
                f"（>{SANE_RATE_PER_INTERVAL * 100:.0f}%），疑似单位解析"
                f"有误，拒绝开仓"), ha, hb
    ka = getattr(info_a, "rate_kind", "unknown") or "unknown"
    kb = getattr(info_b, "rate_kind", "unknown") or "unknown"
    if ka != kb:
        return False, (
            f"两腿费率取值口径不一致（a={ka}，b={kb}），"
            f"不同时点的费率不能相减选向，拒绝开仓"), ha, hb
    return True, "", ha, hb


def detect_period_anomaly(rows: list) -> str | None:
    """费率周期自检（移植 Parallax detect_period_anomaly）。

    rows 是同一场所的全市场 FundingInfo 列表。经验事实：一个所
    大多数市场在溢价接近零时会卡在同一个基准利率（地板价）上，
    地板价的隐含年化必然温和；若算出几十个百分点，多半是结算
    周期假设错了。返回告警文案，没问题返回 None。
    """
    usable = [r for r in rows if r.rate is not None]
    if len(usable) < 10:
        return None
    counts: dict[float, int] = {}
    for r in usable:
        counts[r.rate] = counts.get(r.rate, 0) + 1
    floor_rate, hits = max(counts.items(), key=lambda kv: kv[1])
    if hits / len(usable) < _FLOOR_SHARE_THRESHOLD:
        return None
    if floor_rate == 0:
        return None
    sample = next(r for r in usable if r.rate == floor_rate)
    if sample.interval_seconds <= 0:
        return None
    apr_pct = abs(floor_rate * (_SECONDS_PER_YEAR
                                / sample.interval_seconds) * 100)
    if apr_pct <= _FLOOR_APR_WARN_PCT:
        return None
    hours = sample.interval_seconds / 3600
    return (
        f"{hits}/{len(usable)} 个市场卡在同一个费率 {floor_rate:g} 上"
        f"（疑似基准利率），按 {hours:g} 小时周期换算隐含年化 "
        f"{apr_pct:.1f}%，高得不合常理——多半是结算周期假设错了，"
        f"请核对该所的费率周期")


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


class KillSwitch:
    """全局停机：触发后只允许撤单/平仓，拒绝一切新开仓。

    传入 persist_path 时状态落盘（M3）：halt 写 JSON、reset 删文件，
    进程重启后从文件恢复停机状态与原因，必须人工 reset 才解除——
    避免「连续错误触发的停机一重启就无痕复活实盘」。
    """

    def __init__(self, persist_path: str | Path | None = None) -> None:
        self._halted = False
        self._reason = ""
        self._lock = threading.Lock()
        self._persist_path = Path(persist_path) if persist_path else None
        if self._persist_path is not None and self._persist_path.exists():
            try:
                data = json.loads(self._persist_path.read_text(encoding="utf-8"))
                if data.get("halted"):
                    self._halted = True
                    self._reason = str(data.get("reason") or "重启前已停机")
            except (ValueError, OSError):
                # 文件损坏按未停机处理，但不覆盖原文件（留待人工查看）
                pass

    @property
    def halted(self) -> bool:
        return self._halted

    @property
    def reason(self) -> str:
        return self._reason

    def _save(self) -> None:
        if self._persist_path is None:
            return
        try:
            self._persist_path.parent.mkdir(parents=True, exist_ok=True)
            if self._halted:
                self._persist_path.write_text(json.dumps(
                    {"halted": True, "reason": self._reason},
                    ensure_ascii=False), encoding="utf-8")
            else:
                self._persist_path.unlink(missing_ok=True)
        except OSError:
            pass  # 落盘失败不阻塞停机动作本身

    def halt(self, reason: str = "") -> None:
        with self._lock:
            self._halted = True
            self._reason = reason
            self._save()

    def reset(self) -> None:
        with self._lock:
            self._halted = False
            self._reason = ""
            self._save()

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
