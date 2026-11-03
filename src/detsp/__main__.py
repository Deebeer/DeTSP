"""DeTSP 主入口：python -m detsp

加载 .env + config.yaml → 组装场所 → 组装引擎 → 主线程跑调度循环，
网页面板在同进程独立线程提供服务（共享同一批状态对象）。

参数：
  --config PATH  策略配置（默认 config.yaml，不存在时用空配置=全 paper 无任务）
  --env PATH     密钥文件（默认 .env）
  --paper        强制全部场所 paper（无视 config 的 live 设置）
  --no-web       不启动网页面板
  --db PATH      账本 SQLite 路径（默认 data/detsp.db）
"""

from __future__ import annotations

import argparse
import os
import time
from pathlib import Path

from .core.config import build_config, load_env_file, load_yaml_config
from .core.engine import Engine
from .core.ledger import Ledger
from .venues.factory import build_venues
from .web.server import PanelState, serve, web_settings


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="DeTSP 统一永续合约交易系统")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--env", default=".env")
    ap.add_argument("--paper", action="store_true", help="强制全 paper")
    ap.add_argument("--no-web", action="store_true", help="不启动网页面板")
    ap.add_argument("--db", default="data/detsp.db")
    args = ap.parse_args(argv)

    env = dict(os.environ)
    for k, v in load_env_file(args.env).items():
        env.setdefault(k, v)   # 真实环境变量优先于 .env 文件
    cfg_path = Path(args.config)
    yaml_data = load_yaml_config(cfg_path) if cfg_path.exists() else {}
    app_config = build_config(env=env, yaml_data=yaml_data)

    venues = build_venues(app_config, force_paper=args.paper,
                          dry_run=app_config.dry_run)
    ledger = Ledger(args.db)
    engine = Engine.from_config(app_config, venues, ledger=ledger)
    for name, err in engine.errors.items():
        print(f"[DeTSP] 任务 {name} 启动失败（已隔离）：{err}", flush=True)

    task_cfgs = {}
    for t in app_config.tasks:
        task_cfgs[str(t.get("name") or t.get("strategy"))] = t
    state = PanelState(engine=engine, venues=venues, ledger=ledger,
                       app_config=app_config, task_cfgs=task_cfgs)

    server = None
    if not args.no_web:
        host, port = web_settings(env)
        server = serve(state, host, port)
        print(f"[DeTSP] 面板：http://{host}:{port}/（无鉴权，仅本机）", flush=True)

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
