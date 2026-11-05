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

from copy import deepcopy

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


def fees_dict() -> dict[str, dict[str, float]]:
    """全部场所的费率（深拷贝）：{venue_id: {taker_bps, maker_bps}}。"""
    return {
        vid: {"taker_bps": TAKER_FEE_BPS, "maker_bps": MAKER_FEE_BPS}
        for vid in VENUE_IDS
    }


class FeeTable:
    """只读适配层：返回内置通用默认费率，不读不写任何文件。

    第十一轮的可编辑费率表（JSON 持久化 + set()）已按第十二轮
    定版删除；保留这个类只是让既有调用方（stats_library 的
    LibraryService / StatsLibrary 构造参数）不用改签名。构造时
    传入的 path 一律忽略——历史 fees.json 不再被读取。
    """

    def __init__(self, path=None) -> None:
        # path 仅为签名兼容，刻意不读不写
        self._path = path

    @property
    def path(self):
        return self._path

    def get(self, venue_id: str) -> tuple[float, float]:
        """返回 (taker_bps, maker_bps)；未知场所抛 ValueError。"""
        return get_fees(venue_id)

    def as_dict(self) -> dict[str, dict[str, float]]:
        """全部费率的深拷贝：{venue_id: {"taker_bps":…, "maker_bps":…}}。"""
        return deepcopy(fees_dict())
