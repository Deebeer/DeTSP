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

_YAML_TOP_KEYS = {"engine", "risk", "venues", "tasks", "caps"}
_YAML_RISK_KEYS = {"min_corridor_pct", "close_corridor_pct", "max_consecutive_errors"}
_YAML_CAPS_KEYS = {"global_usd", "venues", "strategies"}
_TASK_KEYS = {"name", "strategy", "venue", "symbol", "venue_a", "venue_b",
              "base_venue", "hedge_venue", "params"}
_TASK_STRATEGIES = ("grid", "rotation_hedge", "premium_arb")


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
class CapsConfig:
    """三层名义金额上限（USD，H1）：<=0/缺省 = 该层不限。

    strategies 的键是任务名（config 里 task 的 name，与 H4 任务级
    归属的 owner 口径一致）。
    """
    global_usd: float = 0.0
    venues: dict[str, float] = field(default_factory=dict)
    strategies: dict[str, float] = field(default_factory=dict)


@dataclass
class AppConfig:
    dry_run: bool = True
    cycle_seconds: float = 20.0
    venues: dict[str, VenueConfig] = field(default_factory=dict)
    risk: RiskConfig = field(default_factory=RiskConfig)
    caps: CapsConfig = field(default_factory=CapsConfig)
    tasks: list[dict] = field(default_factory=list)
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

    caps_raw = yaml_data.get("caps") or {}
    if not isinstance(caps_raw, dict):
        raise ConfigError("caps 必须是映射")
    unknown_caps = set(caps_raw) - _YAML_CAPS_KEYS
    if unknown_caps:
        raise ConfigError(f"caps 含未知键：{sorted(unknown_caps)}")
    caps_venues = {str(k): float(v)
                   for k, v in (caps_raw.get("venues") or {}).items()}
    for vid in caps_venues:
        validate_venue_id(vid)
    caps = CapsConfig(
        global_usd=float(caps_raw.get("global_usd") or 0.0),
        venues=caps_venues,
        strategies={str(k): float(v)
                    for k, v in (caps_raw.get("strategies") or {}).items()})

    tasks: list[dict] = []
    seen_names: set[str] = set()
    seen_keys: set[tuple[str, str]] = set()
    for i, t in enumerate(yaml_data.get("tasks") or []):
        if not isinstance(t, dict):
            raise ConfigError(f"tasks[{i}] 必须是映射")
        unknown = set(t) - _TASK_KEYS
        if unknown:
            raise ConfigError(f"tasks[{i}] 含未知键：{sorted(unknown)}")
        if t.get("strategy") not in _TASK_STRATEGIES:
            raise ConfigError(
                f"tasks[{i}].strategy 非法：{t.get('strategy')}，"
                f"合法值：{_TASK_STRATEGIES}")
        if "params" in t and not isinstance(t["params"], dict):
            raise ConfigError(f"tasks[{i}].params 必须是映射")
        # H4：任务名必填且唯一（它就是归属 owner 与账本 strategy 口径）
        name = str(t.get("name") or "")
        if not name:
            raise ConfigError(f"tasks[{i}] 缺少 name（任务名必填，用作归属标识）")
        if name in seen_names:
            raise ConfigError(f"任务名重复：{name}")
        seen_names.add(name)
        # H4：同一 (venue, symbol) 不许被两个任务占用——account 在本
        # 系统里由 venue 唯一决定（每场所一个账户），故 (venue,symbol)
        # 即 (venue,account,symbol) 口径。字段可能写在 task 顶层或
        # params 里（与 engine.build_task 的合并口径一致），两处都看
        def _tf(key: str) -> str:
            if t.get(key):
                return str(t[key])
            return str((t.get("params") or {}).get(key) or "")

        used: set[tuple[str, str]] = set()
        strat = t["strategy"]
        if strat == "grid":
            used.add((_tf("venue"), _tf("symbol")))
        elif strat == "rotation_hedge":
            sym = _tf("symbol")
            used.add((_tf("venue_a"), sym))
            used.add((_tf("venue_b"), sym))
        elif strat == "premium_arb":
            sym = _tf("symbol")
            used.add((_tf("base_venue"), sym))
            used.add((_tf("hedge_venue"), sym))
        dup = used & seen_keys
        if dup:
            raise ConfigError(
                f"tasks[{i}]（{name}）与已有任务占用同一 (venue,symbol)："
                f"{sorted(dup)}，归属冲突，拒绝启动")
        seen_keys |= used
        tasks.append(dict(t))

    secrets = {k: v for k, v in env.items() if v}
    return AppConfig(dry_run=dry_run, cycle_seconds=cycle, venues=venues,
                     risk=risk, caps=caps, tasks=tasks, secrets=secrets)
