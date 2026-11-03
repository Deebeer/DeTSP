"""策略调度引擎（执行模式）。

Engine.from_config(...) 按 config.yaml 的 tasks 组装 Executor 与
四策略运行时，tick(now) 逐任务调度：先串行执行面板命令队列，
再推进市价单成交发现，最后逐任务 tick。

执行模式规则：
- 安全动作优先：KillSwitch 触发时任务以 allow_open=False 运行
  （撤单/平仓/孤腿处理照常，开仓一律禁止）。
- 单任务异常只隔离该任务并计数；连续错误达阈值触发全局停机，
  且引擎立即调用该任务的 stop() 收尾（撤单 + 平仓），不让它的
  持仓脱离自动管理（审查 G2；原仓 Parallax 调度器同口径）。
- 停机信号在 tick 中途触发导致的 RiskHaltError 是安全动作的
  正常结果，不计入任务错误计数（审查 L3）。
- 归属冲突（ConflictError）在任务启动 claim 时就会暴露，
  该任务被隔离，其他任务不受影响。

（旧「骨架模式」run_once/decide 已按审查 R1 删除：无任何调用方，
其描述的快照式流程与实际执行路径不符，只会误导读者。）
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .execution import Caps, Executor
from .ledger import Ledger
from .registry import OwnershipRegistry
from .risk import ErrorCounter, KillSwitch, RiskHaltError


@dataclass
class Task:
    """一个可执行任务：策略运行时 + 独立错误计数。"""

    name: str
    strategy_type: str
    runtime: object  # HedgedGridStrategy / RotationHedgeStrategy /
    # PremiumArbStrategy / FundingCarryStrategy
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
    """由 config 的一条 task dict 组装策略运行时。

    只认四个现行策略：hedged_grid / premium_arb / rotation_hedge /
    funding_carry。已移除的旧策略名（grid / volume_loop）与未知
    名字一样报错，并明确列出可用策略，避免用户对着旧配置猜。
    """
    from ..strategies.funding_carry import CarryParams, FundingCarryStrategy
    from ..strategies.hedged_grid import HedgedGridParams, HedgedGridStrategy
    from ..strategies.premium_arb import PremiumArbStrategy, PremiumParams
    from ..strategies.rotation_hedge import RotationHedgeStrategy, RotationParams

    available = "hedged_grid / premium_arb / rotation_hedge / funding_carry"
    name = str(task_cfg.get("name") or task_cfg.get("strategy") or "task")
    stype = task_cfg.get("strategy")
    params_raw = dict(task_cfg.get("params") or {})
    # venue/symbol 也可写在 task 顶层，合并进参数（params 内优先）
    for k in ("venue", "symbol", "venue_a", "venue_b",
              "base_venue", "hedge_venue", "grid_venue"):
        if k in task_cfg:
            params_raw.setdefault(k, task_cfg[k])

    if stype in ("grid", "volume_loop"):
        raise ValueError(
            f"策略 {stype} 已移除，可用策略：{available}")
    if stype == "hedged_grid":
        runtime = HedgedGridStrategy(
            HedgedGridParams(**_filter_params(HedgedGridParams, params_raw)),
            executor=executor, owner=name)
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
    elif stype == "funding_carry":
        runtime = FundingCarryStrategy(
            CarryParams(**_filter_params(CarryParams, params_raw)),
            executor=executor, risk_cfg=risk_cfg, owner=name)
        runtime.start()
    else:
        raise ValueError(
            f"未知策略类型：{stype}，可用策略：{available}")
    return Task(name=name, strategy_type=str(stype), runtime=runtime)


@dataclass
class Engine:
    registry: OwnershipRegistry = field(default_factory=OwnershipRegistry)
    kill_switch: KillSwitch = field(default_factory=KillSwitch)
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
        """跑一轮执行调度：面板命令队列 → 市价单成交发现 → 逐任务
        tick，异常隔离 + 错误计数 + 停机联动。"""
        self.process_commands()
        if self.executor is not None:
            # 异步结算场所的市价单成交补记账本（审查 M2）
            try:
                self.executor.poll_pending_fills()
            except Exception:  # noqa: BLE001 — 发现失败不挡任务调度
                pass
        now = time.time() if now is None else now
        allow_open = not self.kill_switch.halted
        for task in self.tasks:
            if task.disabled:
                continue
            try:
                task.runtime.tick(now, allow_open=allow_open)
                task.errors.record_success()
            except RiskHaltError as e:
                # 停机在 tick 中途被触发（如面板线程直接 halt）时，
                # 开仓被风控拒绝是安全机制的正常结果，不是任务
                # 出错，不计错误数（审查 L3）
                task.last_error = f"停机拦截（不计错误）：{e}"
            except Exception as e:  # 单任务隔离
                task.last_error = str(e)
                self.errors[task.name] = str(e)
                if task.errors.record_error():
                    # 同一任务连续错误达阈值：停用该任务并触发全局
                    # 停机；同时立即调用其 stop() 收尾（撤单+平仓），
                    # 不让持仓脱离自动管理（审查 G2）
                    task.disabled = True
                    self.kill_switch.halt(
                        f"任务 {task.name} 连续错误达阈值：{e}")
                    try:
                        task.runtime.stop()
                        self.events.append(
                            f"[{task.name}] 连续错误达阈值被停用，"
                            f"引擎已调用 stop() 收尾（撤单 + 平仓）")
                    except Exception as stop_err:  # noqa: BLE001
                        self.events.append(
                            f"[{task.name}] 被停用后 stop() 收尾失败："
                            f"{stop_err}，请立即人工处理其持仓")
