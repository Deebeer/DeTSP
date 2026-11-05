"""场所费率表：每所 taker（吃单）/ maker（挂单）费率（bps），JSON 落盘。

设置页的场所费率表与中枢库 / 费差库的扣费口径共用这一份数据：
- 文件不存在时用默认值、不报错（唯 tradexyz taker=1.0 bps，
  其余全 0——与现行 premium_arb 参数默认 0 的口径一致，tradexyz
  的 1.0 是第十一轮定版值，仓库此前没有统一费率表）；
- set() 立即原子落盘（写临时文件再 replace），重启不丢；
- 未知场所一律 ValueError，不静默吞掉笔误。
"""

from __future__ import annotations

import json
import math
import os
import tempfile
from copy import deepcopy
from pathlib import Path

from .config import VENUE_IDS

# 默认费率（bps）：tradexyz taker 1.0，其余 0
_DEFAULT_TAKER: dict[str, float] = {"tradexyz": 1.0}


def _defaults() -> dict[str, dict[str, float]]:
    return {
        vid: {
            "taker_bps": float(_DEFAULT_TAKER.get(vid, 0.0)),
            "maker_bps": 0.0,
        }
        for vid in VENUE_IDS
    }


def _check_fee(value, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} 必须是数字")
    v = float(value)
    if not math.isfinite(v) or v < 0:
        raise ValueError(f"{name} 必须是非负数字")
    return v


class FeeTable:
    """场所费率表（JSON 文件持久化）。"""

    def __init__(self, path) -> None:
        self._path = Path(path)
        self._data: dict[str, dict[str, float]] = _defaults()
        if self._path.exists():
            try:
                raw = json.loads(self._path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                raw = None
            if isinstance(raw, dict):
                for vid in VENUE_IDS:
                    entry = raw.get(vid)
                    if not isinstance(entry, dict):
                        continue
                    try:
                        taker = _check_fee(entry.get("taker_bps", 0.0),
                                           "taker_bps")
                        maker = _check_fee(entry.get("maker_bps", 0.0),
                                           "maker_bps")
                    except ValueError:
                        continue
                    self._data[vid] = {"taker_bps": taker,
                                       "maker_bps": maker}

    @property
    def path(self) -> Path:
        return self._path

    def get(self, venue_id: str) -> tuple[float, float]:
        """返回 (taker_bps, maker_bps)；未知场所抛 ValueError。"""
        if venue_id not in VENUE_IDS:
            raise ValueError(
                f"未知 venue_id：{venue_id}，合法值：{VENUE_IDS}")
        entry = self._data[venue_id]
        return float(entry["taker_bps"]), float(entry["maker_bps"])

    def set(self, venue_id: str, taker_bps, maker_bps) -> None:
        """更新一所费率并立即原子落盘。"""
        if venue_id not in VENUE_IDS:
            raise ValueError(
                f"未知 venue_id：{venue_id}，合法值：{VENUE_IDS}")
        taker = _check_fee(taker_bps, "taker_bps")
        maker = _check_fee(maker_bps, "maker_bps")
        self._data[venue_id] = {"taker_bps": taker, "maker_bps": maker}
        self._save()

    def as_dict(self) -> dict[str, dict[str, float]]:
        """全部费率的深拷贝：{venue_id: {"taker_bps":…, "maker_bps":…}}。"""
        return deepcopy(self._data)

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(self._path.parent),
            prefix=self._path.name + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh, ensure_ascii=False, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, self._path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
