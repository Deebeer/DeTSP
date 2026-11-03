"""面板任务的持久化存储（data/panel_tasks.yaml）。

面板上新建的任务与 config.yaml 里的任务分开存放：config 是用户
手写的主配置，面板不回写它；面板任务单独一个文件，引擎启动时
与 config 任务合并加载（合并规则见 DESIGN 第 13 节）。

文件格式与 config.yaml 的 tasks 段一致：顶层 `tasks:` 列表，
每条 {name, strategy, params}。写入用「临时文件 + os.replace」
原子替换，进程中途崩溃也不会留下半截文件。
"""

from __future__ import annotations

import os
from pathlib import Path

PANEL_TASKS_NAME = "panel_tasks.yaml"


def load_panel_tasks(path: str | Path) -> list[dict]:
    """读取面板任务文件；不存在返回空列表，格式坏抛 ValueError。"""
    p = Path(path)
    if not p.exists():
        return []
    import yaml

    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict) or not isinstance(
            data.get("tasks", []), list):
        raise ValueError(f"面板任务文件格式不对：{p}（顶层应为 tasks 列表）")
    out = []
    for i, t in enumerate(data.get("tasks") or []):
        if not isinstance(t, dict) or not t.get("name") \
                or not t.get("strategy"):
            raise ValueError(f"面板任务文件第 {i} 条缺 name/strategy：{p}")
        out.append(dict(t))
    return out


def save_panel_tasks(path: str | Path, tasks: list[dict]) -> None:
    """原子写：先写同目录临时文件，再 os.replace 覆盖目标。"""
    import yaml

    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    body = yaml.safe_dump({"tasks": [dict(t) for t in tasks]},
                          allow_unicode=True, sort_keys=False)
    tmp.write_text(body, encoding="utf-8")
    os.replace(tmp, p)
