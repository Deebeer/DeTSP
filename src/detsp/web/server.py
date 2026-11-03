"""本地网页面板后端（标准库 http.server，无新依赖）。

部署形态：与引擎**同进程**，共享同一批对象（Engine / venues / Ledger），
不另开进程读状态文件——状态即内存对象，不存在不一致窗口（见 DESIGN）。
引擎调度循环跑在后台线程，本模块的 HTTP 服务跑在调用线程或独立线程。

安全红线：面板**没有任何登录鉴权**。因此强制只绑回环地址
（127.0.0.1 / localhost / ::1），非回环绑定直接拒绝启动；
不要做端口映射，不要改绑公网——任何能打开面板的人都能下单和平仓。
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from datetime import datetime, time as dtime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from ..core.engine import Engine, build_task
from ..core.ledger import Ledger
from .page import PAGE_HTML

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8320
_LOOPBACK = ("127.0.0.1", "localhost", "::1")


def web_settings(env: dict) -> tuple[str, int]:
    """从 .env 取面板绑定：WEB_HOST/WEB_PORT 优先，其次 HOST/PORT。"""
    host = env.get("WEB_HOST") or env.get("HOST") or DEFAULT_HOST
    port = int(env.get("WEB_PORT") or env.get("PORT") or DEFAULT_PORT)
    if host not in _LOOPBACK:
        raise ValueError(
            f"面板无鉴权，禁止绑非回环地址：{host}（只允许 127.0.0.1）")
    return host, port


@dataclass
class PanelState:
    """面板共享状态：引擎、场所、账本与任务的原始配置（重启任务用）。"""

    engine: Engine
    venues: dict
    ledger: Ledger
    app_config: object = None
    task_cfgs: dict = field(default_factory=dict)   # 任务名 -> 原始 task dict
    stopped_by_user: set = field(default_factory=set)

    # ----- 任务视图 -----
    def _task_status(self, task) -> str:
        if task.name in self.stopped_by_user:
            return "stopped"
        if task.disabled:
            return "disabled"   # 连续错误被引擎停用
        return "running"

    def tasks_view(self) -> list[dict]:
        out = []
        seen = set()
        for t in self.engine.tasks:
            seen.add(t.name)
            cfg = self.task_cfgs.get(t.name, {})
            out.append({
                "id": t.name, "name": t.name, "strategy": t.strategy_type,
                "status": self._task_status(t),
                "last_error": t.last_error or self.engine.errors.get(t.name, ""),
                "params": {k: v for k, v in cfg.items() if k != "name"},
            })
        for name, cfg in self.task_cfgs.items():
            if name not in seen:   # 组装失败的任务也要在面板可见
                out.append({
                    "id": name, "name": name,
                    "strategy": cfg.get("strategy", ""),
                    "status": "error",
                    "last_error": self.engine.errors.get(name, "组装失败"),
                    "params": {k: v for k, v in cfg.items() if k != "name"},
                })
        return out

    # ----- 启停 -----
    def stop_task(self, name: str) -> dict:
        for t in self.engine.tasks:
            if t.name == name:
                t.disabled = True
                self.stopped_by_user.add(name)
                note = "已停止调度"
                stop = getattr(t.runtime, "stop", None)
                if callable(stop):
                    try:
                        stop()   # 网格：撤自有挂单并平仓、释放归属
                        note = "已停止：自有挂单已撤、持仓已平"
                    except Exception as e:  # 停止动作失败也要如实回报
                        note = f"已停止调度，但收尾动作失败：{e}"
                else:
                    note = "已停止调度（该策略无收尾动作，持仓未自动平仓，请人工确认）"
                return {"ok": True, "task": name, "note": note}
        return {"ok": False, "error": f"任务不存在：{name}"}

    def start_task(self, name: str) -> dict:
        cfg = self.task_cfgs.get(name)
        if cfg is None or self.engine.executor is None or self.app_config is None:
            return {"ok": False, "error": f"任务不存在或引擎未就绪：{name}"}
        try:
            new_task = build_task(cfg, self.engine.executor,
                                  self.app_config.risk)
        except Exception as e:
            return {"ok": False, "error": f"启动失败：{e}"}
        for i, t in enumerate(self.engine.tasks):
            if t.name == name:
                self.engine.tasks[i] = new_task
                break
        else:
            self.engine.tasks.append(new_task)
        self.stopped_by_user.discard(name)
        self.engine.errors.pop(name, None)
        return {"ok": True, "task": name}

    # ----- 总览 -----
    def overview(self) -> dict:
        venues_out = []
        upnl = 0.0
        for vid, v in self.venues.items():
            item = {"venue_id": vid, "mode": getattr(v, "mode", ""),
                    "equity": None, "available": None, "positions": [],
                    "open_orders": 0, "error": ""}
            try:
                bal = v.balance()
                item["equity"] = bal.get("equity")
                item["available"] = bal.get("available")
                for p in v.positions():
                    item["positions"].append({
                        "symbol": p.symbol, "qty": p.qty,
                        "entry_price": p.entry_price,
                        "leverage": p.leverage})
                    try:
                        mid = v.book(p.symbol).mid
                        upnl += p.qty * (mid - p.entry_price)
                    except Exception:
                        pass
                item["open_orders"] = len(v.open_orders())
            except Exception as e:  # 单场所故障不拖垮总览
                item["error"] = str(e)
            venues_out.append(item)
        today0 = datetime.combine(datetime.now().date(), dtime.min).timestamp()
        stats = self.ledger.stats_since(today0)
        ks = self.engine.kill_switch
        return {
            "dry_run": bool(getattr(self.app_config, "dry_run", True)),
            "killswitch": {"halted": ks.halted, "reason": ks.reason},
            "today": stats,
            "unrealized_pnl": round(upnl, 4),
            "venues": venues_out,
        }


def make_handler(state: PanelState):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # 静默，避免面板日志刷屏
            pass

        def _json(self, obj, code=200):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            u = urlparse(self.path)
            if u.path == "/":
                body = PAGE_HTML.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif u.path == "/api/overview":
                self._json(state.overview())
            elif u.path == "/api/tasks":
                self._json({"tasks": state.tasks_view()})
            elif u.path == "/api/fills":
                q = parse_qs(u.query)
                limit = int(q.get("limit", ["50"])[0])
                self._json({"fills": state.ledger.recent_fills(limit=limit)})
            else:
                self._json({"error": "not found"}, 404)

        def do_POST(self):
            u = urlparse(self.path)
            parts = [unquote(p) for p in u.path.split("/") if p]
            if parts == ["api", "killswitch"]:
                state.engine.kill_switch.halt("面板手动触发全局停机")
                self._json({"ok": True, "halted": True})
            elif parts == ["api", "killswitch", "reset"]:
                state.engine.kill_switch.reset()
                self._json({"ok": True, "halted": False})
            elif (len(parts) == 4 and parts[0] == "api" and parts[1] == "tasks"
                    and parts[3] in ("start", "stop")):
                fn = state.start_task if parts[3] == "start" else state.stop_task
                res = fn(parts[2])
                self._json(res, 200 if res.get("ok") else 400)
            else:
                self._json({"error": "not found"}, 404)

    return Handler


def serve(state: PanelState, host: str = DEFAULT_HOST,
          port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    """创建并启动面板服务（独立线程），返回 server 对象。"""
    if host not in _LOOPBACK:
        raise ValueError(f"面板无鉴权，禁止绑非回环地址：{host}")
    server = ThreadingHTTPServer((host, port), make_handler(state))
    th = threading.Thread(target=server.serve_forever, daemon=True,
                          name="detsp-web")
    th.start()
    return server
