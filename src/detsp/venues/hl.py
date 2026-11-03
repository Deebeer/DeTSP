"""Hyperliquid 家族适配（骨架）：本体 / Entropy / trade.xyz 共用参数化实现。

三者共用 Hyperliquid 的 API、SDK 与 API 钱包（agent）签名，仅 dex 不同：
- hyperliquid：dex=""（主 dex，本体永续市场；四仓原本没有，本项目新增）
- entropy：dex="io"，builder 子所，只支持逐仓
- tradexyz：dex="xyz"，builder 子所

移植来源：entropy-arb（MIT）的 venue_hl.py。
本批仅骨架：paper 可用，live 抛 NotImplementedError。
逐仓场所的杠杆上限处理（守强平距离门槛）在策略/风控层做，不在此处。
"""

from __future__ import annotations

from ._skeleton import SkeletonVenue


class HyperliquidVenue(SkeletonVenue):
    source = "待移植自 entropy-arb 的 Hyperliquid 适配（dex 参数化）"

    def __init__(self, dex: str = "", venue_id: str = "hyperliquid", **kw) -> None:
        self.dex = dex
        self.venue_id = venue_id
        super().__init__(**kw)


def hyperliquid(**kw) -> HyperliquidVenue:
    return HyperliquidVenue(dex="", venue_id="hyperliquid", **kw)


def entropy(**kw) -> HyperliquidVenue:
    return HyperliquidVenue(dex="io", venue_id="entropy", **kw)


def tradexyz(**kw) -> HyperliquidVenue:
    return HyperliquidVenue(dex="xyz", venue_id="tradexyz", **kw)
