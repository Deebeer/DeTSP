"""本地网页面板后端（标准库 http.server，无新依赖）。

部署形态：与引擎**同进程**，共享同一批对象（Engine / venues / Ledger），
不另开进程读状态文件——状态即内存对象，不存在不一致窗口（见 DESIGN）。
引擎调度循环跑在后台线程，本模块的 HTTP 服务跑在调用线程或独立线程。

安全（H2 修复）：面板除只绑回环外，启动时生成随机 token——所有
POST 必须带 X-API-Token 头（页面 JS 从页面内嵌值自动带上），
Host 头必须是回环字面值（防 DNS rebinding），POST 带 Origin 时
必须同源（防跨站 POST 操控交易）。即便如此也不要做端口映射、
不要改绑公网——token 只防「本机浏览器被恶意网页借道」这一类攻击，
不等于登录鉴权。
"""

from __future__ import annotations

import json
import secrets as _secrets
import threading
from dataclasses import dataclass, field
from datetime import datetime, time as dtime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from ..core.engine import Engine, build_task, task_occupied_keys
from ..core.ledger import Ledger
from ..core.panel_tasks import save_panel_tasks
from ..core.secret_fields import save_secrets, secrets_status
from ..strategies.specs import PARAMS_CLASSES, strategy_specs, validate_params
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
            f"面板只绑回环且 POST 需 token，禁止绑非回环地址：{host}（只允许 127.0.0.1）")
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
    api_token: str = ""   # H2：serve() 时生成；POST 必须带此 token
    # 面板任务（可在面板增删改）：原始 cfg 与落盘路径（panel_tasks.yaml）
    panel_cfgs: dict = field(default_factory=dict)  # 任务名 -> 原始 task dict
    panel_store_path: object = None
    # 与 config 任务重名被跳过的面板任务的错误（面板列表里标 error 用）
    panel_skip_errors: dict = field(default_factory=dict)
    # 设置页：.env 文件路径（保存密钥写回此文件；None = 不可用）
    env_path: object = None
    # 数据页：公开行情扫描器（测试可注入假对象）
    market_scanner: object = None

    def _scanner(self):
        """惰性创建扫描器：观察列表取 config 的 watchlist。"""
        if self.market_scanner is None:
            from ..core.market_scanner import MarketScanner

            wl = getattr(self.app_config, "watchlist", None) or None
            self.market_scanner = MarketScanner(watchlist=wl)
        return self.market_scanner

    # ----- 数据页（公开行情，只读）-----
    def market_view(self, force: bool = False) -> dict:
        return self._scanner().scan(force=force)

    def market_candles(self, venue_id: str, symbol: str) -> dict:
        return self._scanner().candles(venue_id, symbol)

    # ----- 设置页（密钥：只报状态、只写不读值）-----
    def secrets_view(self) -> dict:
        if not self.env_path:
            return {"groups": [], "error": "未配置 .env 路径，设置页不可用"}
        return {"groups": secrets_status(self.env_path)}

    def save_secrets_view(self, updates) -> dict:
        if not self.env_path:
            return {"ok": False, "error": "未配置 .env 路径，无法保存"}
        if not isinstance(updates, dict):
            return {"ok": False, "error": "请求体必须是 {字段名: 值} 对象"}
        try:
            changed = save_secrets(self.env_path, updates)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        if changed:
            # 系统事件只记字段名，绝不记值
            self._event(f"设置页保存密钥字段：{'、'.join(changed)}"
                        f"（需重启程序生效）")
        return {"ok": True, "changed": changed,
                "note": "已保存到 .env，需重启程序生效"}

    # ----- 来源与持久化 -----
    def _source(self, name: str) -> str:
        """任务来源：panel=面板任务（可删改），config=配置文件任务。

        以引擎的 panel_task_names 为准（合并加载/动态新建时登记）：
        与 config 任务重名被跳过的面板任务不会登记到引擎里，
        config 任务不会被误标成面板任务。
        """
        return "panel" if name in self.engine.panel_task_names else "config"

    def _persist_panel(self) -> None:
        """面板任务全量落盘（原子写）；未配置路径时只存内存。"""
        if self.panel_store_path:
            save_panel_tasks(self.panel_store_path,
                             list(self.panel_cfgs.values()))

    def _event(self, msg: str) -> None:
        """面板写操作记入系统事件（总览可见，带时分秒）。"""
        self.engine.events.append(
            f"[{datetime.now():%H:%M:%S}] 面板操作：{msg}")

    def attach_panel_tasks(self, cfgs: list[dict]) -> dict[str, str]:
        """启动时把面板任务文件的内容并入面板与引擎。

        与 config 任务重名的面板任务：只登记在面板侧并标 error
        （用户可在面板编辑/删除它），不进引擎、不许顶掉 config
        任务的任何状态；其余走 engine.load_panel_tasks 合并组装
        （占用冲突等失败同样只跳过该条并记事件）。
        返回 {任务名: 错误} 供启动日志打印。
        """
        errs: dict[str, str] = {}
        config_names = {n for n in self.task_cfgs
                        if n not in self.panel_cfgs}
        loadable = []
        for cfg in cfgs or []:
            name = str(cfg.get("name") or "")
            if not name:
                continue
            self.panel_cfgs[name] = dict(cfg)
            if name in config_names:
                msg = f"与配置文件任务重名（{name}），面板任务已跳过"
                self.panel_skip_errors[name] = msg
                errs[name] = msg
                self._event(f"面板任务「{name}」与配置文件任务重名，已跳过")
                continue
            self.task_cfgs[name] = dict(cfg)
            loadable.append(cfg)
        errs.update(self.engine.load_panel_tasks(loadable))
        return errs

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
                "source": self._source(t.name),
                "last_error": t.last_error or self.engine.errors.get(t.name, ""),
                "params": {k: v for k, v in cfg.items() if k != "name"},
            })
        for name, cfg in self.task_cfgs.items():
            if name not in seen:   # 组装失败的任务也要在面板可见
                seen.add(name)
                out.append({
                    "id": name, "name": name,
                    "strategy": cfg.get("strategy", ""),
                    "status": "error",
                    "source": self._source(name),
                    "last_error": self.engine.errors.get(name, "组装失败"),
                    "params": {k: v for k, v in cfg.items() if k != "name"},
                })
        for name, cfg in self.panel_cfgs.items():
            if name in self.panel_skip_errors:
                # 与 config 任务重名被跳过的面板任务：与 config 的
                # 同名条目并存展示（来源 panel、状态 error）
                out.append({
                    "id": name, "name": name,
                    "strategy": cfg.get("strategy", ""),
                    "status": "error",
                    "source": "panel",
                    "last_error": self.panel_skip_errors.get(
                        name, "未加载"),
                    "params": {k: v for k, v in cfg.items() if k != "name"},
                })
        return out

    # ----- 启停（M4：经引擎命令队列串行执行，不在 HTTP 线程下单）-----
    def stop_task(self, name: str) -> dict:
        try:
            return self.engine.run_command(lambda: self._do_stop_task(name))
        except TimeoutError:
            return {"ok": False, "error": "引擎主循环繁忙，停止命令超时"}

    def _do_stop_task(self, name: str) -> dict:
        for t in self.engine.tasks:
            if t.name == name:
                t.disabled = True
                self.stopped_by_user.add(name)
                note = "已停止调度"
                stop = getattr(t.runtime, "stop", None)
                if callable(stop):
                    try:
                        stop()   # 四个策略的停止语义统一：撤自有
                        # 挂单 + 平掉全部持仓（见各 runtime 的
                        # stop_note），note 如实回报实际动作
                        note = getattr(
                            t.runtime, "stop_note",
                            "已停止：自有挂单已撤、持仓已平")
                    except Exception as e:  # 停止动作失败也要如实回报
                        note = f"已停止调度，但收尾动作失败：{e}"
                else:
                    note = "已停止调度（该策略无收尾动作，持仓未自动平仓，请人工确认）"
                return {"ok": True, "task": name, "note": note}
        return {"ok": False, "error": f"任务不存在：{name}"}

    def start_task(self, name: str) -> dict:
        try:
            return self.engine.run_command(lambda: self._do_start_task(name))
        except TimeoutError:
            return {"ok": False, "error": "引擎主循环繁忙，启动命令超时"}

    def _do_start_task(self, name: str) -> dict:
        cfg = self.task_cfgs.get(name)
        if cfg is None or self.engine.executor is None or self.app_config is None:
            return {"ok": False, "error": f"任务不存在或引擎未就绪：{name}"}
        try:
            new_task = build_task(cfg, self.engine.executor,
                                  self.app_config.risk)
        except Exception as e:
            return {"ok": False, "error": f"启动失败：{e}"}
        for note in getattr(new_task.runtime, "recovery_notes", []) or []:
            self.engine.events.append(f"[{name}] {note}")
        for i, t in enumerate(self.engine.tasks):
            if t.name == name:
                self.engine.tasks[i] = new_task
                break
        else:
            self.engine.tasks.append(new_task)
        self.stopped_by_user.discard(name)
        self.engine.errors.pop(name, None)
        return {"ok": True, "task": name}

    # ----- 面板任务增删改（经命令队列串行执行，与启停同口径）-----
    def _run(self, fn) -> dict:
        try:
            return self.engine.run_command(fn)
        except TimeoutError:
            return {"ok": False, "error": "引擎主循环繁忙，命令超时，请重试"}

    def create_task(self, name, strategy, params) -> dict:
        return self._run(lambda: self._do_create_task(name, strategy, params))

    def _do_create_task(self, name, strategy, params) -> dict:
        name = str(name or "").strip()
        if not name:
            return {"ok": False, "error": "任务名必填"}
        if strategy not in PARAMS_CLASSES:
            return {"ok": False, "error":
                    f"未知策略：{strategy}，可用策略："
                    f"{' / '.join(PARAMS_CLASSES)}"}
        if name in self.task_cfgs or name in self.panel_cfgs or \
                any(t.name == name for t in self.engine.tasks):
            return {"ok": False, "error": f"任务名已存在：{name}"}
        try:
            clean = validate_params(strategy, params or {})
        except ValueError as e:
            return {"ok": False, "error": f"参数不合法：{e}"}
        cfg = {"name": name, "strategy": strategy, "params": clean}
        try:
            self.engine.add_task_dynamic(cfg)
        except Exception as e:  # 占用冲突/caps/启动对账失败：原样说清
            return {"ok": False, "error": f"创建失败：{e}"}
        self.panel_cfgs[name] = cfg
        self.task_cfgs[name] = cfg
        self._persist_panel()
        self._event(f"创建任务「{name}」（{strategy}）")
        return {"ok": True, "task": name}

    def update_task(self, name, params) -> dict:
        return self._run(lambda: self._do_update_task(name, params))

    def _do_update_task(self, name, params) -> dict:
        name = str(name or "").strip()
        if name not in self.panel_cfgs:
            if name in self.task_cfgs:
                return {"ok": False, "error":
                        f"任务「{name}」来自配置文件，面板不能改，"
                        f"请改 config.yaml 后重启"}
            return {"ok": False, "error": f"任务不存在：{name}"}
        old_cfg = self.panel_cfgs[name]
        if name in self.panel_skip_errors:
            return {"ok": False, "error":
                    f"任务「{name}」与配置文件任务重名被跳过，面板改不了，"
                    f"请删除后换个名字重新新建"}
        strategy = old_cfg["strategy"]
        try:
            clean = validate_params(strategy, params or {})
        except ValueError as e:
            return {"ok": False, "error": f"参数不合法：{e}"}
        new_cfg = {"name": name, "strategy": strategy, "params": clean}
        # 运行中改参数 = 先停（撤单+平仓）再按新参数重组
        was_stopped = name in self.stopped_by_user
        in_engine = any(t.name == name for t in self.engine.tasks)
        if in_engine and not was_stopped:
            res = self._do_stop_task(name)
            if not res.get("ok"):
                return res
        self.engine.remove_task_dynamic(name)
        self.stopped_by_user.discard(name)
        try:
            self.engine.add_task_dynamic(new_cfg)
        except Exception as e:
            # 重组失败：不保存新参数，旧参数仍在面板里，点「启动」
            # 即可按旧参数恢复（任务当前为未组装的 error 状态）
            self.engine.errors[name] = str(e)
            return {"ok": False, "error":
                    f"按新参数重组失败：{e}。任务已停止且未保存新参数，"
                    f"可点启动按原参数恢复，或修正参数后重试"}
        self.panel_cfgs[name] = new_cfg
        self.task_cfgs[name] = new_cfg
        self._persist_panel()
        if was_stopped:
            # 原本是用户手动停的：重组后保持停止状态
            self._do_stop_task(name)
        self._event(f"修改任务「{name}」参数（{strategy}）")
        return {"ok": True, "task": name}

    def delete_task(self, name) -> dict:
        return self._run(lambda: self._do_delete_task(name))

    def _do_delete_task(self, name) -> dict:
        name = str(name or "").strip()
        if name not in self.panel_cfgs:
            if name in self.task_cfgs:
                return {"ok": False, "error":
                        f"任务「{name}」来自配置文件，面板不能删，"
                        f"请改 config.yaml 后重启"}
            return {"ok": False, "error": f"任务不存在：{name}"}
        cfg = self.panel_cfgs[name]
        t_obj = next((t for t in self.engine.tasks if t.name == name), None)
        if t_obj is not None and not t_obj.disabled and \
                name not in self.stopped_by_user:
            return {"ok": False, "error":
                    f"任务「{name}」仍在运行，请先停止（停止会平仓）再删除"}
        # 有持仓拒绝删：先停止平仓或手动平仓后再删
        ex = self.engine.executor
        if ex is not None:
            for venue_id, symbol in sorted(task_occupied_keys(cfg)):
                if not venue_id or not symbol:
                    continue
                pos = ex.position(venue_id, symbol)
                if pos is not None and pos.qty != 0:
                    return {"ok": False, "error":
                            f"任务「{name}」在 {venue_id} 的 {symbol} "
                            f"仍有持仓 {pos.qty}，请先停止平仓后再删除"}
        self.engine.remove_task_dynamic(name)
        self.panel_cfgs.pop(name, None)
        if name not in self.panel_skip_errors:
            self.task_cfgs.pop(name, None)
        self.panel_skip_errors.pop(name, None)
        self.stopped_by_user.discard(name)
        self._persist_panel()
        self._event(f"删除任务「{name}」")
        return {"ok": True, "task": name}

    # ----- 手动平仓（总览持仓行） -----
    def close_position(self, venue_id, symbol) -> dict:
        return self._run(lambda: self._do_close_position(venue_id, symbol))

    def _do_close_position(self, venue_id, symbol) -> dict:
        venue_id = str(venue_id or "")
        symbol = str(symbol or "").strip()
        ex = self.engine.executor
        if ex is None or venue_id not in self.venues:
            return {"ok": False, "error": f"场所不存在：{venue_id}"}
        pos = ex.position(venue_id, symbol)
        if pos is None or pos.qty == 0:
            return {"ok": False, "error": f"{venue_id} 的 {symbol} 无持仓"}
        owner = None
        try:
            owner = ex.registry.owner_of(ex.key(venue_id, symbol))
        except Exception:
            owner = None
        if owner:
            res = ex.close_position(owner, venue_id, symbol)
            if res is None:
                return {"ok": False, "error": "平仓未执行（持仓已变化）"}
            if res.status == "rejected":
                return {"ok": False, "error": f"平仓被拒：{res.detail}"}
            running = any(t.name == owner and t.name not in
                          self.stopped_by_user for t in self.engine.tasks)
            note = f"已平 {venue_id} {symbol}（任务「{owner}」名下持仓）"
            if running:
                note += "；注意：该任务仍在运行，可能按自身逻辑重新开仓或对账处理"
            self._event(f"平仓 {venue_id} {symbol}（任务 {owner} 名下）")
            return {"ok": True, "owner": owner, "note": note}
        # 无归属：手动操作，平掉该场所该币种的全部持仓
        try:
            res = ex.close_position("panel-manual", venue_id, symbol)
        finally:
            try:
                ex.release("panel-manual", venue_id, symbol)
            except Exception:
                pass
        if res is None:
            return {"ok": False, "error": "平仓未执行（持仓已变化）"}
        if res.status == "rejected":
            return {"ok": False, "error": f"平仓被拒：{res.detail}"}
        self._event(f"手动平仓 {venue_id} {symbol}（无任务归属，全部持仓）")
        return {"ok": True, "owner": None,
                "note": f"已手动平掉 {venue_id} {symbol} 的全部持仓（无任务归属）"}

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
                    owner = None
                    ex = self.engine.executor
                    if ex is not None:
                        try:
                            owner = ex.registry.owner_of(
                                ex.key(vid, p.symbol))
                        except Exception:
                            owner = None
                    item["positions"].append({
                        "symbol": p.symbol, "qty": p.qty,
                        "entry_price": p.entry_price,
                        "leverage": p.leverage,
                        "owner": owner})
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
            # S3：启动恢复对账等事件（最近 20 条，面板可见）
            "events": list(self.engine.events[-20:]),
        }


def _host_is_loopback(host_header: str | None) -> bool:
    """Host 头必须是回环字面值（可带端口）：防 DNS rebinding（H2）。"""
    if not host_header:
        return False
    h = host_header.strip().lower()
    if h.startswith("["):
        return h.startswith("[::1]")
    return h.split(":")[0] in ("127.0.0.1", "localhost")


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

        def _check_host(self) -> bool:
            if _host_is_loopback(self.headers.get("Host")):
                return True
            self._json({"error": "Host 头不被接受"}, 403)
            return False

        def _check_post_auth(self) -> bool:
            """POST 安全校验（H2）：token 必带；Origin 存在时必须同源。"""
            token = self.headers.get("X-API-Token") or ""
            if not state.api_token or not _secrets.compare_digest(
                    token, state.api_token):
                self._json({"error": "缺少或错误的 X-API-Token"}, 403)
                return False
            origin = self.headers.get("Origin")
            if origin:
                from urllib.parse import urlparse as _up
                if _up(origin).netloc.lower() != \
                        (self.headers.get("Host") or "").lower():
                    self._json({"error": "Origin 与 Host 不同源"}, 403)
                    return False
            return True

        def do_GET(self):
            if not self._check_host():
                return
            u = urlparse(self.path)
            if u.path == "/":
                # token 注入页面：页面 JS 读取后自动带在 POST 头里
                html = PAGE_HTML.replace("__TOKEN__", state.api_token)
                body = html.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif u.path == "/api/overview":
                self._json(state.overview())
            elif u.path == "/api/tasks":
                self._json({"tasks": state.tasks_view()})
            elif u.path == "/api/strategy-specs":
                self._json({"strategies": strategy_specs(),
                            "venues": list(state.venues)})
            elif u.path == "/api/fills":
                q = parse_qs(u.query)
                limit = int(q.get("limit", ["50"])[0])
                self._json({"fills": state.ledger.recent_fills(limit=limit)})
            elif u.path == "/api/market":
                q = parse_qs(u.query)
                force = (q.get("force", ["0"])[0] == "1")
                self._json(state.market_view(force=force))
            elif u.path == "/api/market/candles":
                q = parse_qs(u.query)
                self._json(state.market_candles(
                    q.get("venue", [""])[0], q.get("symbol", [""])[0]))
            elif u.path == "/api/settings/secrets":
                # 只返回已填/未填，绝不返回任何密钥值
                self._json(state.secrets_view())
            else:
                self._json({"error": "not found"}, 404)

        def _json_body(self) -> dict | None:
            """读 POST 的 JSON body；坏格式返回 None 并已回 400。"""
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = 0
            raw = self.rfile.read(n) if n > 0 else b""
            try:
                obj = json.loads(raw.decode("utf-8")) if raw else {}
            except Exception:
                self._json({"ok": False, "error": "请求体不是合法 JSON"}, 400)
                return None
            if not isinstance(obj, dict):
                self._json({"ok": False, "error": "请求体必须是 JSON 对象"}, 400)
                return None
            return obj

        def do_POST(self):
            if not self._check_host():
                return
            if not self._check_post_auth():
                return
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
            elif parts == ["api", "tasks", "create"]:
                body = self._json_body()
                if body is None:
                    return
                res = state.create_task(body.get("name"),
                                        body.get("strategy"),
                                        body.get("params") or {})
                self._json(res, 200 if res.get("ok") else 400)
            elif parts == ["api", "tasks", "update"]:
                body = self._json_body()
                if body is None:
                    return
                res = state.update_task(body.get("name"),
                                        body.get("params") or {})
                self._json(res, 200 if res.get("ok") else 400)
            elif parts == ["api", "tasks", "delete"]:
                body = self._json_body()
                if body is None:
                    return
                res = state.delete_task(body.get("name"))
                self._json(res, 200 if res.get("ok") else 400)
            elif parts == ["api", "positions", "close"]:
                body = self._json_body()
                if body is None:
                    return
                res = state.close_position(body.get("venue_id"),
                                           body.get("symbol"))
                self._json(res, 200 if res.get("ok") else 400)
            elif parts == ["api", "settings", "secrets"]:
                body = self._json_body()
                if body is None:
                    return
                res = state.save_secrets_view(body)
                self._json(res, 200 if res.get("ok") else 400)
            else:
                self._json({"error": "not found"}, 404)

    return Handler


def serve(state: PanelState, host: str = DEFAULT_HOST,
          port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    """创建并启动面板服务（独立线程），返回 server 对象。

    启动时生成随机 token（H2）：调用方应把带 token 的地址打印到
    启动日志；页面本身会从内嵌值自动带 token，地址栏 token 只是
    方便用户确认打开的是本进程面板。
    """
    if host not in _LOOPBACK:
        raise ValueError(f"面板只绑回环且 POST 需 token，禁止绑非回环地址：{host}")
    if not state.api_token:
        state.api_token = _secrets.token_urlsafe(24)
    server = ThreadingHTTPServer((host, port), make_handler(state))
    th = threading.Thread(target=server.serve_forever, daemon=True,
                          name="detsp-web")
    th.start()
    return server
