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

import queue
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from .execution import Caps, Executor
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
                               symbol=symbol, owner=name)
        runtime.start()
    elif stype == "rotation_hedge":
        runtime = RotationHedgeStrategy(
            RotationParams(**_filter_params(RotationParams, params_raw)),
            executor=executor, risk_cfg=risk_cfg, owner=name)
        runtime.start()
    elif stype == "premium_arb":
        runtime = PremiumArbStrategy(
            PremiumParams(**_filter_params(PremiumParams, params_raw)),
            executor=executor, owner=name)
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
    # S3：启动恢复对账等事件（面板可见）
    events: list[str] = field(default_factory=list)
    # M4：面板命令队列——HTTP 线程只投递，引擎主循环串行执行
    _cmd_queue: queue.Queue = field(default_factory=queue.Queue, repr=False)

    # ----- 命令队列（M4）-----
    def run_command(self, fn, timeout: float = 10.0):
        """在引擎主循环线程执行 fn 并等待结果（其他线程调用）。

        主循环每 tick 先 drain 队列；调用方超时返回 TimeoutError，
        命令本身的异常原样抛回调用方。
        """
        done = threading.Event()
        box: dict = {}

        def _wrap():
            try:
                box["value"] = fn()
            except Exception as e:  # noqa: BLE001 — 原样回传调用方
                box["error"] = e
            finally:
                done.set()

        self._cmd_queue.put(_wrap)
        if not done.wait(timeout):
            raise TimeoutError("引擎主循环未在超时内执行命令")
        if "error" in box:
            raise box["error"]
        return box.get("value")

    def process_commands(self) -> int:
        """主循环调用：串行执行队列中的全部待处理命令，返回条数。"""
        n = 0
        while True:
            try:
                fn = self._cmd_queue.get_nowait()
            except queue.Empty:
                return n
            fn()
            n += 1

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
                    caps=None, state_dir: str | Path | None = None) -> "Engine":
        """按 AppConfig.tasks 组装引擎。

        - caps：显式传入优先；缺省时从 app_config.caps（H1 的 config
          接线）换算，两处都没有则不限。
        - state_dir：停机状态落盘目录（M3），KillSwitch 持久化到
          <state_dir>/killswitch.json，重启保持停机。
        - 单个 task 组装失败（如归属冲突）不拖垮全局：该任务记入
          errors 并跳过，其余任务照常启动。各任务的 S3 启动对账
          事件汇总进 engine.events（面板可见）。
        """
        registry = OwnershipRegistry()
        ks_path = Path(state_dir) / "killswitch.json" if state_dir else None
        kill_switch = KillSwitch(persist_path=ks_path)
        if caps is None:
            cc = getattr(app_config, "caps", None)
            if cc is not None:
                caps = Caps(venue=dict(cc.venues),
                            strategy=dict(cc.strategies),
                            global_=float(cc.global_usd))
        executor = Executor(venues=venues, registry=registry,
                            ledger=ledger or Ledger(),
                            kill_switch=kill_switch, caps=caps,
                            min_corridor_pct=app_config.risk.min_corridor_pct)
        engine = cls(registry=registry, kill_switch=kill_switch,
                     executor=executor,
                     max_consecutive_errors=app_config.risk.max_consecutive_errors)
        if kill_switch.halted:
            engine.events.append(
                f"停机状态由落盘恢复（{kill_switch.reason}），"
                f"须人工 reset 才恢复开仓")
        for task_cfg in getattr(app_config, "tasks", []) or []:
            try:
                task = build_task(task_cfg, executor, app_config.risk)
                engine.tasks.append(task)
                for note in getattr(task.runtime, "recovery_notes", []) or []:
                    engine.events.append(f"[{task.name}] {note}")
            except Exception as e:  # 启动期隔离：冲突/参数错误只废该任务
                tname = str(task_cfg.get("name") or task_cfg.get("strategy"))
                engine.errors[tname] = str(e)
        return engine

    def tick(self, now: float | None = None) -> None:
        """跑一轮执行调度：先执行面板命令队列（M4），再逐任务 tick，
        异常隔离 + 错误计数 + 停机联动。"""
        self.process_commands()
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
