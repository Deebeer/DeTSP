"""Arcus 适配（骨架）。

移植来源：Parallax Hedge（MIT）的 arcus.py / arcus_ws.py——
Ed25519 API 密钥签名、强平价复算、订单推送。
本批仅骨架：paper 可用，live 抛 NotImplementedError。
"""

from __future__ import annotations

from ._skeleton import SkeletonVenue


class ArcusVenue(SkeletonVenue):
    venue_id = "arcus"
    source = "待移植自 Parallax Hedge 的 Arcus 签名与下单实现"
