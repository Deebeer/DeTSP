"""Lighter 适配（骨架）：主网与 Robinhood 链共用参数化实现。

两部署的差异只有端点、chain id、账户与密钥：
- mainnet：Robinhood 链之外的 Lighter 主网，USDC 计价
- robinhood：Robinhood Chain 上的 Lighter，USDG 计价（chain id 466324）

移植来源：主网参考 entropy-arb（MIT）的 venue_lighter.py；
Robinhood 链参考 Parallax Hedge（MIT）。
本批仅骨架：paper 可用，live 抛 NotImplementedError。
"""

from __future__ import annotations

from ._skeleton import SkeletonVenue


class LighterVenue(SkeletonVenue):
    venue_id = "lighter"
    source = "待移植：主网自 entropy-arb、Robinhood 链自 Parallax Hedge"

    def __init__(self, deployment: str = "mainnet", **kw) -> None:
        if deployment not in ("mainnet", "robinhood"):
            raise ValueError(f"未知 Lighter 部署：{deployment}")
        self.deployment = deployment
        self.venue_id = "lighter" if deployment == "mainnet" else "lighter_rh"
        super().__init__(**kw)


def lighter_mainnet(**kw) -> LighterVenue:
    return LighterVenue(deployment="mainnet", **kw)


def lighter_robinhood(**kw) -> LighterVenue:
    return LighterVenue(deployment="robinhood", **kw)
