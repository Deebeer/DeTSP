"""场所组装工厂：按 AppConfig 的 venues 配置构建 7 所适配器。

- mode=paper（或 force_paper=True）：各场所类以 paper 模式构造，
  内部委托通用模拟适配，不连任何真实接口、不需要密钥。
- mode=live：以 live 模式构造，密钥从 AppConfig.secrets（.env）读取；
  传入 dry_run=True 时 live 场所被 DryRunVenue 代理（只读实盘、不下单）。
"""

from __future__ import annotations

from ..core.config import HL_DEX, LIGHTER_DEPLOYMENT, VENUE_IDS, AppConfig
from ..core.venue import VenueAdapter


def build_venues(app_config: AppConfig, force_paper: bool = False,
                 dry_run: bool = False) -> dict[str, VenueAdapter]:
    """构建全部 7 个场所的适配器，键为 venue_id。

    dry_run=True 时，mode=live 的场所会被 DryRunVenue 代理：
    行情/账户走实盘读，下单走本地模拟（不发送任何真实订单）。
    """
    # 延迟导入：各实盘适配模块顶部只依赖 httpx 等轻量库，官方 SDK 均懒加载
    from .arcus import ArcusVenue
    from .aster import AsterVenue
    from .dryrun import DryRunVenue
    from .hl import HyperliquidVenue
    from .lighter import LighterVenue

    secrets = app_config.secrets or {}
    venues: dict[str, VenueAdapter] = {}
    for vid in VENUE_IDS:
        mode = "paper" if force_paper else app_config.venue_mode(vid)
        if vid == "arcus":
            venues[vid] = ArcusVenue(secrets=secrets, mode=mode)
        elif vid in LIGHTER_DEPLOYMENT:
            venues[vid] = LighterVenue(deployment=LIGHTER_DEPLOYMENT[vid],
                                        secrets=secrets, mode=mode)
        elif vid in HL_DEX:
            venues[vid] = HyperliquidVenue(dex=HL_DEX[vid], venue_id=vid,
                                           secrets=secrets, mode=mode)
        elif vid == "aster":
            venues[vid] = AsterVenue(secrets=secrets, mode=mode)
        if dry_run and mode == "live":
            venues[vid] = DryRunVenue(venues[vid])
    return venues
