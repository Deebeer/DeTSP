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
    # PremiumArbStrategy / FundingCarryStrategy / AutoCycleRuntime
    # 运行模式：manual=自主（单笔，平仓后任务结束）/ auto=全自动
    # （监督器自动选标循环，见 core.auto_cycle）
    run_mode: str = "manual"
    errors: ErrorCounter = field(default_factory=ErrorCounter)
    last_error: str = ""
    disabled: bool = False


def task_occupied_keys(task_cfg: dict) -> set[tuple[str, str]]:
    """一条 task 占用的 (venue, symbol) 集合（口径与 core.config 的
    启动冲突校验一致：双腿策略占两个场所）。字段可能写在 task
    顶层或 params 里，两处都看（与 build_task 的合并口径一致）。"""
    def _tf(key: str) -> str:
        if task_cfg.get(key):
            return str(task_cfg[key])
        return str((task_cfg.get("params") or {}).get(key) or "")

    strat = task_cfg.get("strategy")
    # 全自动任务不绑单个标的（监督器动态选标）：占用口径用通配
    # "*"——该任务的两个场所不许再被任何其他任务占用
    sym = "*" if task_cfg.get("run_mode") == "auto" else _tf("symbol")
    used: set[tuple[str, str]] = set()
    if strat == "hedged_grid":
        used.add((_tf("grid_venue"), sym))
        used.add((_tf("hedge_venue"), sym))
    elif strat in ("rotation_hedge", "funding_carry"):
        used.add((_tf("venue_a"), sym))
        used.add((_tf("venue_b"), sym))
    elif strat == "premium_arb":
        used.add((_tf("base_venue"), sym))
        used.add((_tf("hedge_venue"), sym))
    return used


def build_runtime(strategy: str, clean_params: dict, executor: Executor,
                  risk_cfg, owner: str):
    """按策略用已清洗参数构造运行时并 start() 后返回。

    build_task（单笔/自主）与全自动监督器（core.auto_cycle 每轮
    选标后组装内层单笔运行时）共用同一条构造路径。
    """
    from ..strategies.funding_carry import CarryParams, FundingCarryStrategy
    from ..strategies.hedged_grid import HedgedGridParams, HedgedGridStrategy
    from ..strategies.premium_arb import PremiumArbStrategy, PremiumParams
    from ..strategies.rotation_hedge import RotationHedgeStrategy, RotationParams

    available = "hedged_grid / premium_arb / rotation_hedge / funding_carry"
    if strategy == "hedged_grid":
        runtime = HedgedGridStrategy(
            HedgedGridParams(**clean_params),
            executor=executor, owner=owner)
    elif strategy == "rotation_hedge":
        runtime = RotationHedgeStrategy(
            RotationParams(**clean_params),
            executor=executor, risk_cfg=risk_cfg, owner=owner)
    elif strategy == "premium_arb":
        runtime = PremiumArbStrategy(
            PremiumParams(**clean_params),
            executor=executor, owner=owner)
    elif strategy == "funding_carry":
        runtime = FundingCarryStrategy(
            CarryParams(**clean_params),
            executor=executor, risk_cfg=risk_cfg, owner=owner)
    else:
        raise ValueError(
            f"未知策略类型：{strategy}，可用策略：{available}")
    runtime.start()
    return runtime


def build_task(task_cfg: dict, executor: Executor, risk_cfg,
               scanner=None) -> Task:
    """由 config 的一条 task dict 组装策略运行时。

    只认四个现行策略：hedged_grid / premium_arb / rotation_hedge /
    funding_carry。已移除的旧策略名（grid / volume_loop）与未知
    名字一样报错，并明确列出可用策略，避免用户对着旧配置猜。

    运行模式（task 顶层 run_mode，不进 params）：manual=自主
    （缺省）——运行时设 single_cycle，一笔平仓后引擎停用任务；
    auto=全自动——运行时是 AutoCycleRuntime 监督器，由它自动
    选标、逐笔组装内层单笔运行时循环运行（scanner 为行情扫描
    器，缺省时监督器内部惰性自建）。
    """
    from ..strategies.specs import validate_params

    available = "hedged_grid / premium_arb / rotation_hedge / funding_carry"
    name = str(task_cfg.get("name") or task_cfg.get("strategy") or "task")
    stype = task_cfg.get("strategy")
    run_mode = task_cfg.get("run_mode") or "manual"
    if run_mode not in ("manual", "auto"):
        raise ValueError(
            f"任务 {name} 的 run_mode 非法：{run_mode}，"
            f"只许 manual（自主）/ auto（全自动）")
    params_raw = dict(task_cfg.get("params") or {})
    # venue/symbol 也可写在 task 顶层，合并进参数（params 内优先）
    for k in ("venue", "symbol", "venue_a", "venue_b",
              "base_venue", "hedge_venue", "grid_venue"):
        if k in task_cfg:
            params_raw.setdefault(k, task_cfg[k])

    if stype in ("grid", "volume_loop"):
        raise ValueError(
            f"策略 {stype} 已移除，可用策略：{available}")
    # 全自动任务不写 symbol（监督器逐轮选标）、中性网格不写区间
    # 上下限（监督器逐轮按 K 线/价格定）：这两类「监督器供给
    # 字段」在校验前填占位让必填检查通过，校验后从 clean 摘除，
    # 用户实填字段仍走全量校验，口径不放松
    auto_supplied: set[str] = set()
    if run_mode == "auto":
        supplied = {"symbol": "AUTO"}
        if stype == "hedged_grid":
            supplied.update({"lower_price": 1.0, "upper_price": 2.0})
        for k, v in supplied.items():
            if k not in params_raw:
                params_raw[k] = v
                auto_supplied.add(k)
    # 参数校验走 strategies.specs 的单一事实源（与面板表单同一份
    # 规格）：未知字段/缺必填/类型不符都在这里逐项报错
    clean = validate_params(str(stype), params_raw) \
        if stype in ("hedged_grid", "premium_arb", "rotation_hedge",
                     "funding_carry") else None
    if clean is None:
        raise ValueError(
            f"未知策略类型：{stype}，可用策略：{available}")
    for k in auto_supplied:
        clean.pop(k, None)
    if run_mode == "auto":
        # 延迟导入避免与 core.auto_cycle 循环依赖
        from .auto_cycle import AutoCycleRuntime

        runtime = AutoCycleRuntime(
            strategy_type=stype, params=clean, executor=executor,
            risk_cfg=risk_cfg, owner=name, scanner=scanner)
    else:
        runtime = build_runtime(stype, clean, executor, risk_cfg, name)
        # 自主模式：只跑一笔，平仓后策略置 cycle_done，引擎随即
        # 停用任务（见 Engine.tick），不自动重开
        runtime.single_cycle = True
        if stype == "funding_carry":
            runtime.recovery_notes.append(
                "自主模式：本笔平仓后任务结束，不按 reopen 参数自动重开")
    return Task(name=name, strategy_type=str(stype), runtime=runtime,
                run_mode=run_mode)


@dataclass
class Engine:
    registry: OwnershipRegistry = field(default_factory=OwnershipRegistry)
    kill_switch: KillSwitch = field(default_factory=KillSwitch)
    errors: dict[str, str] = field(default_factory=dict)
    executor: Executor | None = None
    tasks: list[Task] = field(default_factory=list)
    max_consecutive_errors: int = 3
    # 组装动态任务时用的风控配置（from_config 时存入；rotation /
    # carry 的运行时需要它，面板新建任务也走同一份）
    risk_cfg: object = None
    # 行情扫描器（from_config 时存入）：全自动任务的监督器选标用，
    # 组装任务时透传给 build_task
    market_scanner: object = None
    # 面板任务名集合（与 config 任务区分：只有它们能在面板删改）
    panel_task_names: set[str] = field(default_factory=set)
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
                    caps=None, state_dir: str | Path | None = None,
                    scanner=None) -> "Engine":
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
                     executor=executor, risk_cfg=app_config.risk,
                     market_scanner=scanner,
                     max_consecutive_errors=app_config.risk.max_consecutive_errors)
        if kill_switch.halted:
            engine.events.append(
                f"停机状态由落盘恢复（{kill_switch.reason}），"
                f"须人工 reset 才恢复开仓")
        for task_cfg in getattr(app_config, "tasks", []) or []:
            try:
                task = build_task(task_cfg, executor, app_config.risk,
                                  scanner=scanner)
                engine.tasks.append(task)
                for note in getattr(task.runtime, "recovery_notes", []) or []:
                    engine.events.append(f"[{task.name}] {note}")
            except Exception as e:  # 启动期隔离：冲突/参数错误只废该任务
                tname = str(task_cfg.get("name") or task_cfg.get("strategy"))
                engine.errors[tname] = str(e)
        return engine

    # ----- 运行时动态任务（面板增删改） -----
    def add_task_dynamic(self, task_cfg: dict) -> Task:
        """运行时组装并加入一个新任务（面板新建/编辑重组用）。

        与启动组装走同一条 build_task 路径：参数规格校验、归属
        冲突、caps 与启动对账语义完全一致。失败抛 ValueError，
        消息逐项说清（占用冲突/参数非法/上限超限），调用方原样
        回给面板，不吞不改。任务名与现有任务或已失败任务重名
        直接拒绝（先删/改名再建）。
        """
        if self.executor is None:
            raise ValueError("引擎未就绪（executor 未组装）")
        name = str(task_cfg.get("name") or "")
        if not name:
            raise ValueError("任务名必填")
        if any(t.name == name for t in self.tasks) or name in self.errors:
            raise ValueError(f"任务名已存在：{name}（重名任务请先删除或改名）")
        task = build_task(task_cfg, self.executor, self.risk_cfg,
                          scanner=self.market_scanner)
        self.tasks.append(task)
        self.panel_task_names.add(name)
        for note in getattr(task.runtime, "recovery_notes", []) or []:
            self.events.append(f"[{name}] {note}")
        return task

    def load_panel_tasks(self, cfgs: list[dict]) -> dict[str, str]:
        """启动时合并加载面板任务（data/panel_tasks.yaml）。

        与 config 任务同路径组装；单个面板任务失败（参数坏了、
        与 config 任务占用冲突等）只记 errors + 系统事件并跳过，
        绝不拖垮 config 任务。返回 {任务名: 错误} 供启动日志打印。
        名字先登记进 panel_task_names——组装失败的面板任务也要
        能在面板里编辑修正或删除。
        """
        errs: dict[str, str] = {}
        for cfg in cfgs or []:
            name = str(cfg.get("name") or "")
            if not name:
                continue
            self.panel_task_names.add(name)
            if any(t.name == name for t in self.tasks) or name in errs \
                    or name in self.errors:
                # 与 config 任务（或先加载的面板任务）重名：跳过，
                # 不许顶掉任何在跑的任务
                msg = f"与已有任务重名（{name}），面板任务已跳过"
                self.errors[name] = msg
                errs[name] = msg
                self.events.append(f"[面板任务 {name}] 加载失败已跳过：{msg}")
                continue
            try:
                task = build_task(cfg, self.executor, self.risk_cfg,
                                  scanner=self.market_scanner)
                self.tasks.append(task)
                for note in getattr(task.runtime, "recovery_notes", []) or []:
                    self.events.append(f"[{name}] {note}")
            except Exception as e:  # 合并加载隔离：只废这一个面板任务
                self.errors[name] = str(e)
                errs[name] = str(e)
                self.events.append(f"[面板任务 {name}] 加载失败已跳过：{e}")
        return errs

    def remove_task_dynamic(self, name: str) -> bool:
        """从调度列表移除任务（调用方须先停止并确认无持仓）。

        同步清掉错误记录、面板任务登记与该任务的全部归属 claim
        （停止动作未必释放 claim，如 rotation 保留 claim 以便重启
        续管；任务既已删除，claim 必须释放，否则该 (venue,symbol)
        会被一个不存在的任务永久占用）。返回是否曾在列表中。
        """
        before = len(self.tasks)
        self.tasks = [t for t in self.tasks if t.name != name]
        self.errors.pop(name, None)
        self.panel_task_names.discard(name)
        if self.executor is not None:
            registry = self.executor.registry
            for k in registry.keys_of(name):
                registry.release(k, name)
        return len(self.tasks) != before

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
                if task.run_mode == "manual" \
                        and getattr(task.runtime, "cycle_done", False) \
                        and not task.disabled:
                    # 自主模式单笔已平仓：任务到此结束（不调
                    # runtime.stop()——仓位已平、无单可撤，策略
                    # 本身已收尾；停用只是不再调度）
                    task.disabled = True
                    self.events.append(
                        f"[{task.name}] 自主模式：本笔已平仓，"
                        f"任务结束（不自动重开）")
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
