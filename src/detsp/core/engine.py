"""策略调度引擎骨架。

每轮：快照 → 风控裁决（停机优先）→ 策略决策 → 归属校验 → 交执行层。
本批只做调度骨架：决策动作以 Action 记录/分发，不直接连实盘执行。
单任务异常只隔离该任务，不拖停其他任务。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from .registry import Key, OwnershipRegistry
from .risk import KillSwitch


@dataclass
class Action:
    """策略产出的意图动作（执行层在后续批次落地）。"""

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
class Engine:
    registry: OwnershipRegistry = field(default_factory=OwnershipRegistry)
    kill_switch: KillSwitch = field(default_factory=KillSwitch)
    strategies: list[Strategy] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)

    def add_strategy(self, strategy: Strategy) -> None:
        self.strategies.append(strategy)

    def run_once(self, ctx: Context) -> list[Action]:
        """跑一轮。全局停机时不开新仓：直接返回空动作。"""
        if self.kill_switch.halted:
            return []
        actions: list[Action] = []
        for s in self.strategies:
            try:
                acts = s.decide(ctx)
            except Exception as e:  # 单任务隔离
                self.errors[s.name] = str(e)
                continue
            # 归属校验：动作涉及的键必须可被该策略占用
            for a in acts:
                self.registry.claim(a.key, s.name)
                actions.append(a)
        return actions
