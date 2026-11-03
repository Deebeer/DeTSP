"""配置加载与校验：.env 放密钥，config.yaml 放策略/风控参数。

校验原则（借鉴 entropy-arb 的严格配置思路）：未知 venue、未知键、
非法 mode 在启动时直接报错，避免带着错误配置上实盘。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# 7 个场所的 venue_id（与 DESIGN.md 一致）
VENUE_IDS = (
    "arcus",
    "lighter",
    "lighter_rh",
    "hyperliquid",
    "entropy",
    "tradexyz",
    "aster",
)

# Hyperliquid 家族共用 hl.py（dex 参数区分）
HL_DEX = {"hyperliquid": "", "entropy": "io", "tradexyz": "xyz"}
# Lighter 两部署共用 lighter.py（deployment 参数区分）
LIGHTER_DEPLOYMENT = {"lighter": "mainnet", "lighter_rh": "robinhood"}

MODES = ("paper", "live")

_YAML_TOP_KEYS = {"engine", "risk", "venues"}
_YAML_RISK_KEYS = {"min_corridor_pct", "close_corridor_pct", "max_consecutive_errors"}


class ConfigError(ValueError):
    """配置不合法。"""


def validate_venue_id(venue_id: str) -> str:
    if venue_id not in VENUE_IDS:
        raise ConfigError(f"未知 venue_id：{venue_id}，合法值：{VENUE_IDS}")
    return venue_id


def validate_mode(mode: str) -> str:
    if mode not in MODES:
        raise ConfigError(f"非法 mode：{mode}，合法值：{MODES}")
    return mode


@dataclass
class VenueConfig:
    venue_id: str
    mode: str = "paper"

    def __post_init__(self) -> None:
        validate_venue_id(self.venue_id)
        validate_mode(self.mode)


@dataclass
class RiskConfig:
    min_corridor_pct: float = 3.0
    close_corridor_pct: float = 1.5
    max_consecutive_errors: int = 3


@dataclass
class AppConfig:
    dry_run: bool = True
    cycle_seconds: float = 20.0
    venues: dict[str, VenueConfig] = field(default_factory=dict)
    risk: RiskConfig = field(default_factory=RiskConfig)
    secrets: dict[str, str] = field(default_factory=dict, repr=False)

    def venue_mode(self, venue_id: str) -> str:
        cfg = self.venues.get(venue_id)
        return cfg.mode if cfg else "paper"


def load_env_file(path: str | Path = ".env") -> dict[str, str]:
    """读取 .env（不打印任何值）。文件不存在时返回空 dict。"""
    p = Path(path)
    out: dict[str, str] = {}
    if not p.exists():
        return out
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip("'\"")
    return out


def load_yaml_config(path: str | Path) -> dict:
    """读取并校验 config.yaml 的顶层结构。"""
    import yaml  # 延迟导入，纯逻辑测试可不依赖

    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ConfigError("config.yaml 顶层必须是映射")
    unknown = set(data) - _YAML_TOP_KEYS
    if unknown:
        raise ConfigError(f"config.yaml 含未知顶层键：{sorted(unknown)}")
    return data


def build_config(env: dict[str, str] | None = None, yaml_data: dict | None = None) -> AppConfig:
    """由 .env dict 与 yaml dict 组装 AppConfig，并完成全部校验。"""
    env = env if env is not None else dict(os.environ)
    yaml_data = yaml_data or {}

    dry_run = str(env.get("DRY_RUN", "true")).lower() not in ("false", "0", "no")

    venues: dict[str, VenueConfig] = {}
    for vid, vcfg in (yaml_data.get("venues") or {}).items():
        validate_venue_id(vid)
        mode = (vcfg or {}).get("mode", "paper")
        venues[vid] = VenueConfig(venue_id=vid, mode=mode)
    # 未在 yaml 出现的场所默认 paper
    for vid in VENUE_IDS:
        venues.setdefault(vid, VenueConfig(venue_id=vid))

    risk_raw = yaml_data.get("risk") or {}
    unknown_risk = set(risk_raw) - _YAML_RISK_KEYS
    if unknown_risk:
        raise ConfigError(f"risk 含未知键：{sorted(unknown_risk)}")
    risk = RiskConfig(**{k: risk_raw[k] for k in _YAML_RISK_KEYS & set(risk_raw)})

    engine_raw = yaml_data.get("engine") or {}
    cycle = float(engine_raw.get("cycle_seconds", 20.0))
    if cycle <= 0:
        raise ConfigError("engine.cycle_seconds 必须大于 0")

    secrets = {k: v for k, v in env.items() if v}
    return AppConfig(dry_run=dry_run, cycle_seconds=cycle, venues=venues, risk=risk, secrets=secrets)
