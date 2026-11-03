"""DeTSP 主入口：python -m detsp

加载 .env + config.yaml → 组装场所 → 组装引擎 → 主线程跑调度循环，
网页面板在同进程独立线程提供服务（共享同一批状态对象）。

参数：
  --config PATH  策略配置（默认 config.yaml，不存在时用空配置=全 paper 无任务）
  --env PATH     密钥文件（默认 .env）
  --paper        强制全部场所 paper（无视 config 的 live 设置）
  --no-web       不启动网页面板
  --db PATH      账本 SQLite 路径（默认 data/detsp.db）
  --reset-killswitch  清除落盘的停机状态后退出（人工恢复用，M3）

live 启动门（M3）：配置里有 live 场所、且非 DRY_RUN、未 --paper 时，
必须存在有效的预检通过标记（data/preflight-ok.json，指纹与当前
config+密钥一致），否则拒绝启动并提示先跑预检。caps（H1）经
Engine.from_config 从 config 读入，不再需要单独传参。
"""

from __future__ import annotations

import argparse
import os
import time
from pathlib import Path

from .core.config import VENUE_IDS, build_config, load_env_file, load_yaml_config
from .core.engine import Engine
from .core.ledger import Ledger
from .core.panel_tasks import PANEL_TASKS_NAME, load_panel_tasks
from .core.preflight import PREFLIGHT_MARK_NAME, check_preflight_mark
from .venues.factory import build_venues
from .web.server import PanelState, serve, web_settings


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="DeTSP 统一永续合约交易系统")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--env", default=".env")
    ap.add_argument("--paper", action="store_true", help="强制全 paper")
    ap.add_argument("--no-web", action="store_true", help="不启动网页面板")
    ap.add_argument("--db", default="data/detsp.db")
    ap.add_argument("--reset-killswitch", action="store_true",
                    help="清除落盘停机状态后退出（人工恢复，M3）")
    args = ap.parse_args(argv)

    env = dict(os.environ)
    for k, v in load_env_file(args.env).items():
        env.setdefault(k, v)   # 真实环境变量优先于 .env 文件
    cfg_path = Path(args.config)
    yaml_data = load_yaml_config(cfg_path) if cfg_path.exists() else {}
    app_config = build_config(env=env, yaml_data=yaml_data)

    state_dir = Path(args.db).parent
    if args.reset_killswitch:
        # 人工恢复：删停机落盘文件即解除（KillSwitch 以文件存在为准）
        f = state_dir / "killswitch.json"
        if f.exists():
            f.unlink()
            print(f"[DeTSP] 已清除停机状态：{f}", flush=True)
        else:
            print("[DeTSP] 当前没有落盘的停机状态", flush=True)
        return 0

    # M3 live 启动门：真金白银模式（有 live 场所 + 非 DRY_RUN + 未强制
    # paper）必须先过预检且标记指纹与当前 config/密钥一致
    has_live = any(app_config.venue_mode(v) == "live" for v in VENUE_IDS)
    if has_live and not app_config.dry_run and not args.paper:
        ok, why = check_preflight_mark(
            app_config, state_dir / PREFLIGHT_MARK_NAME)
        if not ok:
            print(f"[DeTSP] 拒绝 live 启动：{why}\n"
                  f"        先运行：python -m detsp.preflight "
                  f"--config {args.config} --env {args.env}", flush=True)
            return 2

    venues = build_venues(app_config, force_paper=args.paper,
                          dry_run=app_config.dry_run)
    ledger = Ledger(args.db)
    engine = Engine.from_config(app_config, venues, ledger=ledger,
                                state_dir=state_dir)
    for note in engine.events:
        print(f"[DeTSP] {note}", flush=True)
    for name, err in engine.errors.items():
        print(f"[DeTSP] 任务 {name} 启动失败（已隔离）：{err}", flush=True)

    task_cfgs = {}
    for t in app_config.tasks:
        task_cfgs[str(t.get("name") or t.get("strategy"))] = t
    # 面板任务（data/panel_tasks.yaml）：与 config 任务合并加载；
    # 文件坏了只跳过面板任务并提示，不拖垮主程序
    panel_path = state_dir / PANEL_TASKS_NAME
    try:
        panel_cfgs_list = load_panel_tasks(panel_path)
    except Exception as e:  # noqa: BLE001 — 面板任务文件损坏降级处理
        print(f"[DeTSP] 面板任务文件读取失败（已忽略）：{e}", flush=True)
        panel_cfgs_list = []
    state = PanelState(engine=engine, venues=venues, ledger=ledger,
                       app_config=app_config, task_cfgs=task_cfgs,
                       panel_store_path=panel_path, env_path=args.env)
    for name, err in state.attach_panel_tasks(panel_cfgs_list).items():
        print(f"[DeTSP] 面板任务 {name} 加载失败（已隔离）：{err}", flush=True)

    server = None
    if not args.no_web:
        host, port = web_settings(env)
        server = serve(state, host, port)
        print(f"[DeTSP] 面板：http://{host}:{port}/?token={state.api_token}"
              f"（仅本机，POST 需 token，勿外传此地址）", flush=True)

    mode_note = "paper" if args.paper else (
        "DRY_RUN（只决策不下单）" if app_config.dry_run else "live")
    print(f"[DeTSP] 引擎启动：{len(engine.tasks)} 个任务，"
          f"周期 {app_config.cycle_seconds}s，模式 {mode_note}", flush=True)
    try:
        while True:
            engine.tick()
            time.sleep(app_config.cycle_seconds)
    except KeyboardInterrupt:
        print("\n[DeTSP] 收到中断，退出（交易所已有挂单/持仓不会自动处理，"
              "请在面板或交易所确认）")
    finally:
        if server is not None:
            server.shutdown()
        ledger.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
