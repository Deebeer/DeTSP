"""策略调度引擎。

两种用法：
- 骨架模式（第一批）：run_once(ctx) 收集各策略 decide() 的 Action，
  只做归属校验，不执行。
- 执行模式（第三批）：Engine.from_config(...) 按 config.yaml 的 tasks
  组装 Executor 与三策略运行时，tick(now) 逐任务调度。

执行模式规则：
- 安全动作优先：KillSwitch 触发时任务以 allow_open=False 运行
  （撤单/平仓/孤腿处理照常，开仓一律禁止）。
- 单任务异常只隔离该任务并计数；连续错误达阈值触发全局停机。
- 归属冲突（ConflictError）在任务启动 claim 时就会暴露，
  该任务被隔离，其他任务不受影响。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Protocol

from .execution import Executor
from .ledger import Ledger
from .registry import Key, OwnershipRegistry
from .risk import ErrorCounter, KillSwitch


@dataclass
class Action:
    """策略产出的意图动作（骨架模式）。"""

    kind: str            # place / cancel / close 等
    key: Key
    payload: dict = field(default_factory=dict)


@dataclass
class Context:
    """一轮决策的上下文快照（行情/持仓由调度方填充）。"""

    ts: float = 0.0
    books: dict = field(default_factory=dict)      # {(venue, symbol): Book}
    positions: dict = field(default_factory=dict)  # {Key: Position}


class Strategy(Protocol):
    name: str

    def decide(self, ctx: Context) -> list[Action]:
        ...


@dataclass
class Task:
    """一个可执行任务：策略运行时 + 独立错误计数。"""

    name: str
    strategy_type: str
    runtime: object  # GridStrategy / RotationHedgeStrategy / PremiumArbStrategy
    errors: ErrorCounter = field(default_factory=ErrorCounter)
    last_error: str = ""
    disabled: bool = False


def _filter_params(cls, raw: dict) -> dict:
    """只把 dataclass 认识的字段传给参数类，未知键报错（严格校验）。"""
    import dataclasses
    known = {f.name for f in dataclasses.fields(cls)}
    unknown = set(raw) - known
    if unknown:
        raise ValueError(f"{cls.__name__} 含未知参数：{sorted(unknown)}")
    return {k: v for k, v in raw.items() if k in known}


def build_task(task_cfg: dict, executor: Executor, risk_cfg) -> Task:
    """由 config 的一条 task dict 组装策略运行时。"""
    from ..strategies.grid import GridParams, GridStrategy
    from ..strategies.premium_arb import PremiumArbStrategy, PremiumParams
    from ..strategies.rotation_hedge import RotationHedgeStrategy, RotationParams

    name = str(task_cfg.get("name") or task_cfg.get("strategy") or "task")
    stype = task_cfg.get("strategy")
    params_raw = dict(task_cfg.get("params") or {})
    # venue/symbol 也可写在 task 顶层，合并进参数（params 内优先）
    for k in ("venue", "symbol", "venue_a", "venue_b",
              "base_venue", "hedge_venue"):
        if k in task_cfg:
            params_raw.setdefault(k, task_cfg[k])

    if stype == "grid":
        venue_id = params_raw.pop("venue", task_cfg.get("venue", ""))
        symbol = params_raw.pop("symbol", task_cfg.get("symbol", "BTC"))
        runtime = GridStrategy(GridParams(**_filter_params(GridParams, params_raw)),
                               executor=executor, venue_id=venue_id,
                               symbol=symbol)
        runtime.start()
    elif stype == "rotation_hedge":
        runtime = RotationHedgeStrategy(
            RotationParams(**_filter_params(RotationParams, params_raw)),
            executor=executor, risk_cfg=risk_cfg)
        runtime.start()
    elif stype == "premium_arb":
        runtime = PremiumArbStrategy(
            PremiumParams(**_filter_params(PremiumParams, params_raw)),
            executor=executor)
        runtime.start()
    else:
        raise ValueError(f"未知策略类型：{stype}")
    return Task(name=name, strategy_type=str(stype), runtime=runtime)


@dataclass
class Engine:
    registry: OwnershipRegistry = field(default_factory=OwnershipRegistry)
    kill_switch: KillSwitch = field(default_factory=KillSwitch)
    strategies: list[Strategy] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)
    executor: Executor | None = None
    tasks: list[Task] = field(default_factory=list)
    max_consecutive_errors: int = 3

    # ----- 骨架模式（第一批兼容） -----
    def add_strategy(self, strategy: Strategy) -> None:
        self.strategies.append(strategy)

    def run_once(self, ctx: Context) -> list[Action]:
        """跑一轮骨架决策。全局停机时不开新仓：直接返回空动作。"""
        if self.kill_switch.halted:
            return []
        actions: list[Action] = []
        for s in self.strategies:
            try:
                acts = s.decide(ctx)
            except Exception as e:  # 单任务隔离
                self.errors[s.name] = str(e)
                continue
            for a in acts:
                self.registry.claim(a.key, s.name)
                actions.append(a)
        return actions

    # ----- 执行模式 -----
    @classmethod
    def from_config(cls, app_config, venues: dict, ledger: Ledger | None = None,
                    caps=None) -> "Engine":
        """按 AppConfig.tasks 组装引擎。

        单个 task 组装失败（如归属冲突）不拖垮全局：该任务记入
        errors 并跳过，其余任务照常启动。
        """
        registry = OwnershipRegistry()
        kill_switch = KillSwitch()
        executor = Executor(venues=venues, registry=registry,
                            ledger=ledger or Ledger(),
                            kill_switch=kill_switch, caps=caps,
                            min_corridor_pct=app_config.risk.min_corridor_pct)
        engine = cls(registry=registry, kill_switch=kill_switch,
                     executor=executor,
                     max_consecutive_errors=app_config.risk.max_consecutive_errors)
        for task_cfg in getattr(app_config, "tasks", []) or []:
            try:
                engine.tasks.append(build_task(task_cfg, executor,
                                               app_config.risk))
            except Exception as e:  # 启动期隔离：冲突/参数错误只废该任务
                tname = str(task_cfg.get("name") or task_cfg.get("strategy"))
                engine.errors[tname] = str(e)
        return engine

    def tick(self, now: float | None = None) -> None:
        """跑一轮执行调度：逐任务 tick，异常隔离 + 错误计数 + 停机联动。"""
        now = time.time() if now is None else now
        allow_open = not self.kill_switch.halted
        for task in self.tasks:
            if task.disabled:
                continue
            try:
                task.runtime.tick(now, allow_open=allow_open)
                task.errors.record_success()
            except Exception as e:  # 单任务隔离
                task.last_error = str(e)
                self.errors[task.name] = str(e)
                if task.errors.record_error():
                    # 同一任务连续错误达阈值：停用该任务并触发全局停机
                    task.disabled = True
                    self.kill_switch.halt(
                        f"任务 {task.name} 连续错误达阈值：{e}")
