"""场所费率（内置通用默认，第十二轮定版）。

第十二轮起不再有可编辑的场所费率表：全所统一一组内置默认值
（taker 5 bps / maker 2 bps，宁高勿低——中枢库算带、溢价开仓
扣费、费差库算回本时把费估高一点更安全），写死在本模块常量
里，不进面板、不落盘。

兼容说明：
- 历史上落盘的 data/fees.json **不再读取**（文件留着不管即可，
  本模块任何路径都不打开它）；
- FeeTable 类保留为只读适配层（构造参数 path 被忽略），让
  stats_library 等旧调用方无需改签名即可拿到同一组常量；
  它没有 set()、不写任何文件。
"""

from __future__ import annotations

from .config import VENUE_IDS

# 内置通用默认费率（bps）：全所统一
TAKER_FEE_BPS = 5.0
MAKER_FEE_BPS = 2.0


def get_fees(venue_id: str) -> tuple[float, float]:
    """返回某场所 (taker_bps, maker_bps)；未知场所抛 ValueError。"""
    if venue_id not in VENUE_IDS:
        raise ValueError(
            f"未知 venue_id：{venue_id}，合法值：{VENUE_IDS}")
    return TAKER_FEE_BPS, MAKER_FEE_BPS


def taker_bps(venue_id) -> float:
    """某场所 taker 费率（bps）；为空/非法返回 0（不挡选标）。

    选标与回本成本的统一入口（auto_cycle 与 web/server 共用，
    此前两处各写一份同口径实现，第十四轮收敛到这里）。"""
    if not venue_id:
        return 0.0
    try:
        taker, _maker = get_fees(str(venue_id))
        return float(taker)
    except Exception:  # noqa: BLE001 — 非法场所按 0
        return 0.0


def roundtrip_cost_bps(venue_a, venue_b,
                       max_open_spread_bps=None) -> float:
    """双腿开平总成本（bps）= (两腿 taker 之和) × 2 + 最大开仓价差。

    最大开仓价差缺省 300（资金费五项配置的默认值）。费差库回本
    时间 = 本成本 ÷ 当前净费差，口径全系统一处。"""
    spread = 300.0 if max_open_spread_bps is None \
        else float(max_open_spread_bps)
    return (taker_bps(venue_a) + taker_bps(venue_b)) * 2 + spread


class FeeTable:
    """只读适配层：返回内置通用默认费率，不读不写任何文件。

    第十一轮的可编辑费率表（JSON 持久化 + set()）已按第十二轮
    定版删除；保留这个类只是让既有调用方（stats_library 的
    LibraryService / StatsLibrary 构造参数）不用改签名。构造时
    传入的 path 一律忽略——历史 fees.json 不再被读取。
    """

    def __init__(self, path=None) -> None:
        # path 仅为签名兼容，刻意不读不写、不保存
        pass

    def get(self, venue_id: str) -> tuple[float, float]:
        """返回 (taker_bps, maker_bps)；未知场所抛 ValueError。"""
        return get_fees(venue_id)
