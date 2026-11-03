"""Aster 适配（骨架）：本项目自写。

说明：perp-dex-tools 里有 Aster 适配，但其为非商业许可，
本文件不使用其任何代码，仅参考「统一适配接口」的设计思想，
实盘部分后续依据 Aster 官方文档自行实现（API key/secret 签名）。
本批仅骨架：paper 可用，live 抛 NotImplementedError。
"""

from __future__ import annotations

from ._skeleton import SkeletonVenue


class AsterVenue(SkeletonVenue):
    venue_id = "aster"
    source = "待依据 Aster 官方文档自写（未使用 perp-dex-tools 代码）"
