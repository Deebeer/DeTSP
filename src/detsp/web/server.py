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

try:
    from ..core.engine import normalize_run_mode
except ImportError:  # 后端归一函数未就绪时的兜底（语义与契约一致）
    def normalize_run_mode(value):
        v = str(value or "manual")
        if v == "auto":
            return "semi_auto"
        return v

from ..core.config import VENUE_IDS
from ..core.ledger import Ledger
from ..core.panel_tasks import save_panel_tasks
from ..core.secret_fields import save_secrets, secrets_status
from ..strategies.specs import PARAMS_CLASSES, strategy_specs, validate_params
from .page import PAGE_HTML

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8320
_LOOPBACK = ("127.0.0.1", "localhost", "::1")

# 运行模式四值（第十轮）与全自动风险档三值；旧值 auto 归一 semi_auto
RUN_MODES = ("manual", "semi_fixed", "semi_auto", "full_auto")
RUN_PRESETS = ("conservative", "neutral", "aggressive")
_RUN_MODE_ERROR = ("运行模式不合法：{v}，只许 manual（自主）/ "
                   "semi_fixed（半自动·固定标的）/ semi_auto"
                   "（半自动·自动选标）/ full_auto（全自动）；"
                   "旧值 auto 按 semi_auto 处理")


def _normalize_run_mode(value) -> str:
    """归一 + 校验：合法返回四值之一，非法抛 ValueError。"""
    v = normalize_run_mode(value or "manual")
    if v not in RUN_MODES:
        raise ValueError(_RUN_MODE_ERROR.format(v=value))
    return v


def _normalize_run_mode_or(value, default: str = "manual") -> str:
    """展示口径的归一：坏值不抛错，回退缺省（列表不能因旧数据挂掉）。"""
    try:
        return _normalize_run_mode(value)
    except Exception:  # noqa: BLE001 — 展示路径容错
        return default


def _validate_run_preset(value) -> str:
    v = str(value or "neutral")
    if v not in RUN_PRESETS:
        raise ValueError(
            f"风险档不合法：{value}，只许 conservative（保守）/ "
            f"neutral（中性）/ aggressive（激进）")
    return v


def _validate_amount_usd(value):
    """amount_usd 透传校验：None 原样返回，给了必须是正数。"""
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"amount_usd 必须是数字：{value}")
    if v <= 0:
        raise ValueError(f"amount_usd 必须大于 0：{value}")
    return v


def _validate_params_for_mode(strategy, params, run_mode):
    """按运行模式校验参数（与 core.engine.build_task 同口径）。

    自动选标类（semi_auto/full_auto）有些必填字段由监督器逐轮
    供给（symbol、网格区间；full_auto 另含两腿场所、网格每格量、
    溢价带）：校验前只给缺失的这类字段填占位让必填检查通过，
    校验后从结果摘除——用户实填字段仍走全量校验，存盘不带占位。
    其余模式直接走 validate_params。
    """
    raw = dict(params or {})
    if run_mode not in ("semi_auto", "full_auto"):
        return validate_params(strategy, raw)
    supplied: dict = {"symbol": "AUTO"}
    if strategy == "hedged_grid":
        supplied.update({"lower_price": 1.0, "upper_price": 2.0})
    if run_mode == "full_auto":
        # 两腿场所占位只填本策略自己的字段（多了会被判未知参数）
        supplied.update({
            "hedged_grid": {"grid_venue": "arcus",
                            "hedge_venue": "lighter"},
            "funding_carry": {"venue_a": "arcus", "venue_b": "lighter"},
            "premium_arb": {"base_venue": "arcus",
                            "hedge_venue": "lighter"},
        }.get(strategy, {}))
        if strategy == "hedged_grid":
            supplied.update({"qty_per_grid": 0.01})
        if strategy == "premium_arb":
            supplied.update({"midline_bps": 0.0, "upper_bps": 10.0,
                             "lower_bps": 10.0})
    filled = dict(supplied)
    filled.update(raw)
    clean = validate_params(strategy, filled)
    for k in supplied:
        if k not in raw:
            clean.pop(k, None)
    return clean


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
    # 第十一轮接线：__main__ 把共享 scanner 与 state_dir 传进来
    # （scanner 是 market_scanner 的别名参数，两者同步）；缺省
    # None 时维持现行懒构造 scanner 的行为，库服务不启用。
    scanner: object = None
    state_dir: object = None
    # 场所费率表与中枢/费差库服务（stats_library 工作流提供）；
    # 模块缺失或构造失败时保持 None，面板相关功能降级隐藏。
    fee_table: object = None
    library_service: object = None

    def __post_init__(self):
        if self.scanner is not None and self.market_scanner is None:
            self.market_scanner = self.scanner
        elif self.market_scanner is not None and self.scanner is None:
            self.scanner = self.market_scanner
        self._init_library_services()

    def _init_library_services(self) -> None:
        """按 state_dir 初始化费率表与库服务（容错降级）。

        只在给了 state_dir 时尝试：fee_table = FeeTable(
        state_dir/"fees.json")；library_service = LibraryService(
        scanner, state_dir, fee_table)。任一模块 ImportError 或
        构造异常都只让对应对象保持 None，面板其余功能不受影响。
        已注入的对象（测试/外部）不覆盖；库服务自带的 fee_table
        反过来补给 self.fee_table。
        """
        from pathlib import Path

        svc = self.library_service
        if svc is not None and self.fee_table is None:
            self.fee_table = getattr(svc, "fee_table", None)
        if self.state_dir is None:
            return
        if self.fee_table is None:
            try:
                from ..core.fee_table import FeeTable

                self.fee_table = FeeTable(Path(self.state_dir) / "fees.json")
            except Exception:  # noqa: BLE001 — 缺模块/坏文件降级
                self.fee_table = None
        if self.library_service is None:
            try:
                from ..core.stats_library import LibraryService

                self.library_service = LibraryService(
                    self._scanner(), self.state_dir, self.fee_table)
            except Exception:  # noqa: BLE001 — 缺模块降级
                self.library_service = None
            if self.library_service is not None and self.fee_table is None:
                self.fee_table = getattr(
                    self.library_service, "fee_table", None)

    def _fee_table(self):
        """有效费率表：自身优先，其次库服务自带的。"""
        if self.fee_table is not None:
            return self.fee_table
        if self.library_service is not None:
            return getattr(self.library_service, "fee_table", None)
        return None

    def _scanner(self):
        """惰性创建扫描器：观察列表取 config 的 watchlist。"""
        if self.market_scanner is None:
            from ..core.market_scanner import MarketScanner

            wl = getattr(self.app_config, "watchlist", None) or None
            self.market_scanner = MarketScanner(watchlist=wl)
            self.scanner = self.market_scanner
        return self.market_scanner

    # ----- 数据页（公开行情，只读）-----
    def market_view(self, force: bool = False) -> dict:
        return self._scanner().scan(force=force)

    def market_candles(self, venue_id: str, symbol: str) -> dict:
        return self._scanner().candles(venue_id, symbol)

    def pairs_view(self, venue_a: str, venue_b: str,
                   strategy: str) -> dict:
        return self._scanner().pairs(venue_a, venue_b, strategy)

    def best_view(self, strategy: str) -> dict:
        """strategy 给了合法策略名 → 该策略最佳（策略卡时代的
        接口，保留兼容）；没给/不合法 → 三策略合并排序
        （总览「最佳机会」单列表用）。"""
        from ..strategies.specs import PARAMS_CLASSES

        if strategy in PARAMS_CLASSES:
            return self._scanner().best(strategy, limit=5)
        return self._scanner().best_all(per_strategy=5)

    # ----- 策略记录（运行仓位 + 仓位历史）-----
    # 运行仓位没有交易所侧的「开仓时间」可用：追踪器以面板首次
    # 观测到该仓位的时间为 opened_ts（程序启动前就存在的仓位
    # 同此口径）。仓位从有到无（平仓/任务停止收尾/手动平）时，
    # 把一条记录落进账本 closed_positions：平仓价优先取该任务
    # 在 opened_ts 之后的平仓方向成交均价（fills 口径），取不
    # 到时用发现平仓当刻的盘口中间价（mark 口径），再取不到
    # 记 unknown、close_price 为 None——口径随记录如实标注。
    _pos_seen: dict = None          # (venue, symbol) -> 跟踪中的仓位
    _pos_synced_at: float = 0.0

    def _strategy_of(self, task_name: str) -> str:
        cfg = self.task_cfgs.get(task_name) or {}
        return str(cfg.get("strategy") or "")

    def _close_price_from_fills(self, seen: dict,
                                need_qty: float) -> float | None:
        """平仓价（fills 口径）：opened_ts 之后、平仓方向的成交，
        从最近往前累计到 need_qty 的量加权均价；无成交返回 None。"""
        if not seen.get("task"):
            return None
        close_side = "sell" if seen["qty"] > 0 else "buy"
        fills = self.ledger.recent_fills(limit=500,
                                         since=seen["opened_ts"] - 1.0)
        got, wsum = 0.0, 0.0
        for f in fills:
            if f["venue"] != seen["venue"] or \
                    f["symbol"] != seen["symbol"] or \
                    f["strategy"] != seen["task"] or \
                    f["side"] != close_side:
                continue
            take = min(abs(f["qty"]), need_qty - got)
            if take <= 0:
                break
            got += take
            wsum += take * f["price"]
            if got >= need_qty - 1e-12:
                break
        if got <= 0:
            return None
        return wsum / got

    def _update_pos_tracker(self, flat: list[dict],
                            known_venues: set | None = None) -> None:
        """flat = [{venue, symbol, qty, entry_price, owner}]（当前
        全部持仓）。与上次快照 diff：消失/反向的仓位落历史表。
        known_venues = 本轮成功抓到持仓的场所集合：不在其中的
        场所视为「本轮未知」，其仓位不参与 diff（防接口抖动把
        还在的仓位误记成已平）；None = 全部可信。"""
        import time as _time

        if self._pos_seen is None:
            self._pos_seen = {}
        now = _time.time()
        current: dict[tuple, dict] = {}
        for p in flat:
            if not p["qty"]:
                continue
            current[(p["venue"], p["symbol"])] = p
        for key, seen in list(self._pos_seen.items()):
            if known_venues is not None and \
                    seen["venue"] not in known_venues:
                continue          # 本轮没抓到这所：不下结论
            cur = current.get(key)
            if cur is not None and \
                    (cur["qty"] > 0) == (seen["qty"] > 0):
                # 同向仍在：更新数量/开仓价（加仓会变），不落历史
                seen["qty"] = cur["qty"]
                seen["entry_price"] = cur["entry_price"]
                continue
            # 消失或方向翻转：旧仓位视为已平，落一条历史记录
            close_price = self._close_price_from_fills(
                seen, abs(seen["qty"]))
            source = "fills"
            if close_price is None:
                source = "unknown"
                venue = self.venues.get(seen["venue"])
                if venue is not None:
                    try:
                        close_price = venue.book(seen["symbol"]).mid
                        source = "mark"
                    except Exception:  # noqa: BLE001 — 盘口取不到就 unknown
                        close_price = None
            self.ledger.record_closed_position({
                "strategy": seen["strategy"], "task": seen["task"],
                "venue": seen["venue"], "symbol": seen["symbol"],
                "side": "long" if seen["qty"] > 0 else "short",
                "qty": abs(seen["qty"]),
                "entry_price": seen["entry_price"],
                "close_price": close_price,
                "close_price_source": source,
                "opened_ts": seen["opened_ts"], "closed_ts": now})
            del self._pos_seen[key]
        for key, p in current.items():
            if key not in self._pos_seen:
                self._pos_seen[key] = {
                    "venue": p["venue"], "symbol": p["symbol"],
                    "qty": p["qty"], "entry_price": p["entry_price"],
                    "task": p.get("owner") or "",
                    "strategy": self._strategy_of(p.get("owner") or ""),
                    "opened_ts": now}
        self._pos_synced_at = now

    def _flat_positions(self) -> tuple[list[dict], set]:
        """轻量抓取全部场所当前持仓（含归属），供记录追踪用。
        返回 (持仓列表, 成功抓到的场所集合)。"""
        flat = []
        known = set()
        ex = self.engine.executor
        for vid, v in self.venues.items():
            try:
                poss = v.positions()
            except Exception:  # noqa: BLE001 — 单所故障不拖垮记录
                continue
            known.add(vid)
            for p in poss:
                owner = None
                if ex is not None:
                    try:
                        owner = ex.registry.owner_of(ex.key(vid, p.symbol))
                    except Exception:  # noqa: BLE001
                        owner = None
                flat.append({"venue": vid, "symbol": p.symbol,
                             "qty": p.qty, "entry_price": p.entry_price,
                             "owner": owner})
        return flat, known

    def records_view(self, limit: int = 50) -> dict:
        """策略记录：运行仓位（开始时间倒序）+ 仓位历史（平仓
        时间倒序）。追踪快照过期（>3 秒）时先轻量同步一次。"""
        import time as _time

        if _time.time() - self._pos_synced_at > 3.0:
            flat, known = self._flat_positions()
            self._update_pos_tracker(flat, known)
        seen = self._pos_seen or {}
        open_rows = [{
            "strategy": s["strategy"], "task": s["task"],
            "venue": s["venue"], "symbol": s["symbol"],
            "side": "long" if s["qty"] > 0 else "short",
            "qty": abs(s["qty"]), "qty_signed": s["qty"],
            "entry_price": s["entry_price"], "opened_ts": s["opened_ts"],
        } for s in seen.values()]
        open_rows.sort(key=lambda r: -r["opened_ts"])
        return {"updated_at": _time.time(),
                "open": open_rows,
                "closed": self.ledger.recent_closed_positions(limit)}

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

    # ----- 设置页（场所费率表：core.fee_table，单位 bps）-----
    def fees_view(self) -> dict:
        ft = self._fee_table()
        if ft is None:
            return {"ok": False, "fees": {},
                    "error": "费率表不可用（费率表服务未就绪）"}
        try:
            return {"ok": True, "fees": ft.as_dict()}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "fees": {}, "error": str(e)}

    def save_fees_view(self, body) -> dict:
        """POST /api/settings/fees {venue_id, taker_bps, maker_bps}。"""
        ft = self._fee_table()
        if ft is None:
            return {"ok": False, "error": "费率表不可用（费率表服务未就绪）"}
        if not isinstance(body, dict):
            return {"ok": False, "error": "请求体必须是 JSON 对象"}
        venue_id = body.get("venue_id") or body.get("venue")
        if venue_id not in VENUE_IDS:
            return {"ok": False,
                    "error": f"未知场所：{venue_id}，合法值：{VENUE_IDS}"}
        try:
            taker = float(body.get("taker_bps"))
            maker = float(body.get("maker_bps"))
        except (TypeError, ValueError):
            return {"ok": False,
                    "error": "taker_bps / maker_bps 必须是数字（单位 bps）"}
        try:
            ft.set(venue_id, taker, maker)
        except (ValueError, TypeError) as e:
            return {"ok": False, "error": str(e)}
        self._event(f"设置页保存场所费率：{venue_id} "
                    f"taker {taker} bps / maker {maker} bps")
        return {"ok": True, "fees": ft.as_dict()}

    # ----- 中枢库 / 费差库（core.stats_library，缺失时降级）-----
    def _library(self):
        svc = self.library_service
        return getattr(svc, "library", None) if svc is not None else None

    def _library_status(self):
        svc = self.library_service
        if svc is None:
            return None
        try:
            st = svc.status()
            return st if isinstance(st, dict) else {"raw": st}
        except Exception as e:  # noqa: BLE001 — 状态查询不崩面板
            return {"running": False, "last_error": str(e)}

    @staticmethod
    def _entry_dict(entry) -> dict:
        if isinstance(entry, dict):
            return dict(entry)
        if hasattr(entry, "__dict__"):
            return {k: v for k, v in vars(entry).items()
                    if not k.startswith("_")}
        return {"value": entry}

    def libraries_view(self, venue_a=None, venue_b=None,
                       symbol=None) -> dict:
        """GET /api/libraries：{service: status|null, premium: {...}}。

        premium 的键为 "base__hedge__symbol"（与 StatsLibrary.
        premium_entries 的键一致）。带 venue_a/venue_b/symbol 查询
        参数时按三元组精确取一条并入（枚举不可用时也能取到）。
        库服务不存在时 service=None、premium={}（面板降级隐藏）。
        """
        premium: dict = {}
        lib = self._library()
        if lib is not None:
            entries = getattr(lib, "premium_entries", None)
            if callable(entries):
                try:
                    all_entries = entries()
                    if isinstance(all_entries, dict):
                        for k, v in all_entries.items():
                            premium[str(k)] = self._entry_dict(v)
                except Exception:  # noqa: BLE001 — 枚举失败不拖垮接口
                    pass
            if venue_a and venue_b and symbol:
                fn = getattr(lib, "premium_entry", None)
                if callable(fn):
                    try:
                        entry = fn(venue_a, venue_b, symbol)
                    except Exception:  # noqa: BLE001
                        entry = None
                    if entry is not None:
                        premium[f"{venue_a}__{venue_b}__{symbol}"] = \
                            self._entry_dict(entry)
        return {"service": self._library_status(), "premium": premium}

    def _taker_bps(self, venue_id) -> float:
        """某所 taker 费率（bps）；费率表不可用/场所非法时 0。"""
        ft = self._fee_table()
        if ft is None or venue_id not in VENUE_IDS:
            return 0.0
        try:
            taker, _maker = ft.get(venue_id)
            return float(taker)
        except Exception:  # noqa: BLE001
            return 0.0

    def _funding_cost_bps(self, va, vb, params: dict) -> float:
        """开平总成本（bps）= (两所 taker 之和) × 2 + 最大开仓价差。

        最大开仓价差取任务/表单的 max_open_spread_bps（资金费五项
        配置之一），缺失时按其默认值 300 bps。
        """
        mos = params.get("max_open_spread_bps")
        try:
            mos = float(mos) if mos is not None else 300.0
        except (TypeError, ValueError):
            mos = 300.0
        return (self._taker_bps(va) + self._taker_bps(vb)) * 2 + mos

    def libraries_funding_view(self, va=None, vb=None, symbol=None,
                               line=None, max_open_spread=None,
                               current_net=None) -> dict:
        """GET /api/libraries/funding：单组合费差库信息 + 预计回本。

        回本 = 开平总成本 ÷ 当前净费差；当前净费差由页面传当前
        pairs 行的费差（current_net），没传时用 7 天平均净费差。
        """
        import time as _time

        out = {"service": self._library_status(), "funding": None}
        lib = self._library()
        if lib is None or not va or not vb or not symbol:
            return out
        try:
            min_line = float(line) if line is not None else 0.0
        except (TypeError, ValueError):
            min_line = 0.0
        fn = getattr(lib, "funding_stats", None)
        if not callable(fn):
            return out
        try:
            stats = fn(va, vb, symbol, min_line, _time.time())
        except Exception as e:  # noqa: BLE001
            out["error"] = str(e)
            return out
        if not isinstance(stats, dict):
            return out
        stats = dict(stats)
        params = {}
        if max_open_spread is not None:
            params["max_open_spread_bps"] = max_open_spread
        cost = self._funding_cost_bps(va, vb, params)
        net = current_net
        if net is None:
            net = stats.get("avg_net_bps_per_hour")
        be = None
        be_fn = getattr(lib, "breakeven_hours", None)
        if callable(be_fn):
            try:
                be = be_fn(cost, net)
            except Exception:  # noqa: BLE001
                be = None
        if be is None and net:
            try:
                be = cost / float(net) if float(net) > 0 else None
            except (TypeError, ValueError, ZeroDivisionError):
                be = None
        stats["roundtrip_cost_bps"] = cost
        stats["breakeven_hours"] = be
        stats["breakeven_h"] = be
        out["funding"] = stats
        return out

    # ----- 资金费启动警告（固腿固标/自主可强行）-----
    def _funding_gate_warnings(self, cfg: dict) -> list[str]:
        """资金费任务启动前的持续性警告（不拦启动，只提醒）。

        只对 funding_carry 且 run_mode ∈ manual/semi_fixed 的任务：
        查费差库 funding_gate，不达标把原因（中文）收进 warnings；
        样本不足 24 小时时 warnings 必含「样本不足」。库服务不
        存在时返回空列表（降级：不警告、不拦截）。semi_auto /
        full_auto 不走这里（自动选标在监督器内硬过滤）。
        """
        if not isinstance(cfg, dict) or \
                cfg.get("strategy") != "funding_carry":
            return []
        mode = _normalize_run_mode_or(cfg.get("run_mode") or "manual")
        if mode not in ("manual", "semi_fixed"):
            return []
        lib = self._library()
        if lib is None:
            return []
        import time as _time

        params = cfg.get("params") or {}
        if not isinstance(params, dict):
            params = {}
        va = params.get("venue_a") or cfg.get("venue_a")
        vb = params.get("venue_b") or cfg.get("venue_b")
        symbol = params.get("symbol") or cfg.get("symbol")
        if not va or not vb or not symbol:
            return []
        # 最低资金费率差（进出共用线）：现行字段名优先，旧名兼容，
        # 再兜底找 min_*费差类字段，避免 specs 改名后静默失效
        min_line = None
        for key in ("min_edge_bps_per_hour",
                    "min_entry_edge_bps_per_hour",
                    "min_funding_diff_bps_per_hour",
                    "min_net_bps_per_hour", "min_line_bps"):
            if params.get(key) is not None:
                min_line = params[key]
                break
        if min_line is None:
            for k, v in params.items():
                if k.startswith("min") and isinstance(v, (int, float)):
                    min_line = v
                    break
        try:
            min_line = float(min_line) if min_line is not None else 0.0
        except (TypeError, ValueError):
            min_line = 0.0
        now = _time.time()
        warnings: list[str] = []
        stats = None
        stats_fn = getattr(lib, "funding_stats", None)
        if callable(stats_fn):
            try:
                stats = stats_fn(va, vb, symbol, min_line, now)
            except Exception:  # noqa: BLE001
                stats = None
        if isinstance(stats, dict):
            sample = stats.get("sample_hours")
            if sample is not None and float(sample) < 24:
                warnings.append(
                    f"样本不足：费差库样本仅 {sample} 小时，不足 24 小时")
        elif stats is None:
            warnings.append("样本不足：费差库暂无此组合数据")
        # 当前净费差：优先扫描器 pairs 的实时费差，其次 7 天平均
        current_net = None
        if self.market_scanner is not None:
            try:
                pairs = self._scanner().pairs(va, vb, "funding_carry")
                for row in (pairs or {}).get("rows") or []:
                    if row.get("symbol") == symbol:
                        current_net = row.get("edge_bps_h")
                        break
            except Exception:  # noqa: BLE001 — 取不到就用平均值兜底
                current_net = None
        if current_net is None and isinstance(stats, dict):
            current_net = stats.get("avg_net_bps_per_hour")
        gate_fn = getattr(lib, "funding_gate", None)
        if callable(gate_fn):
            cost = self._funding_cost_bps(va, vb, params)
            try:
                gate = gate_fn(va, vb, symbol, min_line, cost,
                               current_net, now)
            except Exception:  # noqa: BLE001 — gate 异常不拦启动
                gate = None
            if isinstance(gate, dict) and not gate.get("ok", True):
                reason = str(gate.get("reason") or "费差持续性未达标")
                if not any(reason in w or w in reason for w in warnings):
                    warnings.append(reason)
        return warnings

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
            run_mode = _normalize_run_mode_or(
                getattr(t, "run_mode", None) or cfg.get("run_mode")
                or "manual")
            rt = t.runtime
            # 自动选标类（semi_auto/full_auto，含旧值 auto 归一后）
            # 的当前标的/阶段/轮数取自监督器运行时；其余为空/0
            is_cycle = run_mode in ("semi_auto", "full_auto")
            venues_rt = getattr(rt, "current_venues", None)
            out.append({
                "id": t.name, "name": t.name, "strategy": t.strategy_type,
                "status": self._task_status(t),
                "source": self._source(t.name),
                "last_error": t.last_error or self.engine.errors.get(t.name, ""),
                "params": {k: v for k, v in cfg.items() if k != "name"},
                # 运行模式（第十轮四值，归一后输出）+ 风险档 +
                # 当前两腿（从 runtime 读，没有则空 list）
                "run_mode": run_mode,
                "run_preset": cfg.get("run_preset")
                or getattr(t, "run_preset", None) or "neutral",
                "auto_phase": getattr(rt, "auto_phase", "")
                if is_cycle else "",
                "current_symbol": getattr(rt, "current_symbol", None)
                if is_cycle else None,
                "current_venues": list(venues_rt) if venues_rt else [],
                "cycles_completed": int(
                    getattr(rt, "cycles_completed", 0) or 0)
                if is_cycle else 0,
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
                    "run_mode": _normalize_run_mode_or(
                        cfg.get("run_mode") or "manual"),
                    "run_preset": cfg.get("run_preset") or "neutral",
                    "auto_phase": "", "current_symbol": None,
                    "current_venues": [],
                    "cycles_completed": 0,
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
                    "run_mode": _normalize_run_mode_or(
                        cfg.get("run_mode") or "manual"),
                    "run_preset": cfg.get("run_preset") or "neutral",
                    "auto_phase": "", "current_symbol": None,
                    "current_venues": [],
                    "cycles_completed": 0,
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
            new_task = build_task(
                cfg, self.engine.executor, self.app_config.risk,
                scanner=getattr(self.engine, "market_scanner", None)
                or self.market_scanner)
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
        res = {"ok": True, "task": name}
        # 资金费固腿固标/自主：持续性不达标只警告、照常启动（强行）
        warnings = self._funding_gate_warnings(cfg)
        if warnings:
            res["warnings"] = warnings
        return res

    # ----- 面板任务增删改（经命令队列串行执行，与启停同口径）-----
    def _run(self, fn) -> dict:
        try:
            return self.engine.run_command(fn)
        except TimeoutError:
            return {"ok": False, "error": "引擎主循环繁忙，命令超时，请重试"}

    def create_task(self, name, strategy, params,
                    run_mode="manual", run_preset=None,
                    amount_usd=None) -> dict:
        return self._run(lambda: self._do_create_task(
            name, strategy, params, run_mode, run_preset, amount_usd))

    def _do_create_task(self, name, strategy, params,
                        run_mode="manual", run_preset=None,
                        amount_usd=None) -> dict:
        name = str(name or "").strip()
        if not name:
            return {"ok": False, "error": "任务名必填"}
        if strategy not in PARAMS_CLASSES:
            return {"ok": False, "error":
                    f"未知策略：{strategy}，可用策略："
                    f"{' / '.join(PARAMS_CLASSES)}"}
        try:
            # 归一后再落盘：新写入一律新值（auto → semi_auto）
            run_mode = _normalize_run_mode(run_mode)
            run_preset = _validate_run_preset(run_preset)
            amount_usd = _validate_amount_usd(amount_usd)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        if name in self.task_cfgs or name in self.panel_cfgs or \
                any(t.name == name for t in self.engine.tasks):
            return {"ok": False, "error": f"任务名已存在：{name}"}
        try:
            clean = _validate_params_for_mode(strategy, params,
                                              run_mode)
        except ValueError as e:
            return {"ok": False, "error": f"参数不合法：{e}"}
        cfg = {"name": name, "strategy": strategy, "params": clean,
               "run_mode": run_mode, "run_preset": run_preset}
        if amount_usd is not None:
            cfg["amount_usd"] = amount_usd
        try:
            self.engine.add_task_dynamic(cfg)
        except Exception as e:  # 占用冲突/caps/启动对账失败：原样说清
            return {"ok": False, "error": f"创建失败：{e}"}
        self.panel_cfgs[name] = cfg
        self.task_cfgs[name] = cfg
        self._persist_panel()
        self._event(f"创建任务「{name}」（{strategy}）")
        res = {"ok": True, "task": name}
        # 一键运行即启动：资金费警告口径与 _do_start_task 一致
        warnings = self._funding_gate_warnings(cfg)
        if warnings:
            res["warnings"] = warnings
        return res

    def update_task(self, name, params, run_mode=None, run_preset=None,
                    amount_usd=None) -> dict:
        return self._run(lambda: self._do_update_task(
            name, params, run_mode, run_preset, amount_usd))

    def _do_update_task(self, name, params, run_mode=None,
                        run_preset=None, amount_usd=None) -> dict:
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
        if run_mode is None:
            # 编辑弹窗不带模式时保持原模式，不许悄悄改语义
            run_mode = old_cfg.get("run_mode") or "manual"
        if run_preset is None:
            # 同理：弹窗不带风险档时保持原档（缺省 neutral）
            run_preset = old_cfg.get("run_preset") or "neutral"
        if amount_usd is None:
            amount_usd = old_cfg.get("amount_usd")
        try:
            # 归一后再落盘：旧值 auto 写回时也变 semi_auto
            run_mode = _normalize_run_mode(run_mode)
            run_preset = _validate_run_preset(run_preset)
            amount_usd = _validate_amount_usd(amount_usd)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        try:
            clean = _validate_params_for_mode(strategy, params,
                                              run_mode)
        except ValueError as e:
            return {"ok": False, "error": f"参数不合法：{e}"}
        new_cfg = {"name": name, "strategy": strategy, "params": clean,
                   "run_mode": run_mode, "run_preset": run_preset}
        if amount_usd is not None:
            new_cfg["amount_usd"] = amount_usd
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
            occupied = set(task_occupied_keys(cfg))
            if _normalize_run_mode_or(cfg.get("run_mode") or "manual") \
                    in ("semi_auto", "full_auto") and t_obj is not None:
                # 自动选标任务的占用键是通配 "*"，真实持仓在监督器
                # 当前标的上：把当前标的的两腿也纳入持仓检查
                cur = getattr(t_obj.runtime, "current_symbol", None)
                if cur:
                    for venue_id, _ in list(occupied):
                        occupied.add((venue_id, cur))
            for venue_id, symbol in sorted(occupied):
                if not venue_id or not symbol or symbol == "*":
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
        import time as _time

        venues_out = []
        pos_ok_venues: set = set()
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
                pos_ok_venues.add(vid)   # 持仓已成功抓到，可参与记录 diff
                item["open_orders"] = len(v.open_orders())
            except Exception as e:  # 单场所故障不拖垮总览
                item["error"] = str(e)
            venues_out.append(item)
        # 策略记录追踪：用总览已抓到的持仓 diff，消失的落历史表
        self._update_pos_tracker([
            {"venue": item["venue_id"], "symbol": p["symbol"],
             "qty": p["qty"], "entry_price": p["entry_price"],
             "owner": p.get("owner")}
            for item in venues_out for p in item["positions"]],
            pos_ok_venues)
        today0 = datetime.combine(datetime.now().date(), dtime.min).timestamp()
        stats = self.ledger.stats_since(today0)
        all_stats = self.ledger.stats_since(0.0)
        # 盈亏（估算）口径：已平仓实现盈亏（closed_positions 按
        # (平仓价-开仓价)×量×方向；平仓价 unknown 的行无法估算、
        # 不计入只计 excluded 数）+ 当前持仓未实现盈亏。未实现
        # 盈亏是「当前」快照、没有时间维度，今日与累计都全额
        # 计入（页面小字注明）。
        rp_today = self.ledger.realized_pnl_since(today0)
        rp_all = self.ledger.realized_pnl_since(0.0)
        stats = {**stats,
                 "realized_pnl": round(rp_today["realized_pnl"], 4),
                 "pnl_excluded": rp_today["excluded"],
                 "pnl": round(rp_today["realized_pnl"] + upnl, 4)}
        all_stats = {**all_stats,
                     "realized_pnl": round(rp_all["realized_pnl"], 4),
                     "pnl_excluded": rp_all["excluded"],
                     "pnl": round(rp_all["realized_pnl"] + upnl, 4)}
        ks = self.engine.kill_switch
        return {
            # 数据时间（本接口生成时刻）：面板各区块右上角的
            # 「更新 HH:MM:SS」小字取自这里，不由前端瞎填
            "updated_at": _time.time(),
            "dry_run": bool(getattr(self.app_config, "dry_run", True)),
            "killswitch": {"halted": ks.halted, "reason": ks.reason},
            "today": stats,
            "all_time": all_stats,
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
            elif u.path == "/api/pairs":
                q = parse_qs(u.query)
                self._json(state.pairs_view(
                    q.get("venue_a", [""])[0], q.get("venue_b", [""])[0],
                    q.get("strategy", [""])[0]))
            elif u.path == "/api/best":
                q = parse_qs(u.query)
                self._json(state.best_view(q.get("strategy", [""])[0]))
            elif u.path == "/api/records":
                q = parse_qs(u.query)
                try:
                    limit = int(q.get("limit", ["50"])[0])
                except ValueError:
                    limit = 50
                self._json(state.records_view(limit=limit))
            elif u.path == "/api/settings/secrets":
                # 只返回已填/未填，绝不返回任何密钥值
                self._json(state.secrets_view())
            elif u.path == "/api/settings/fees":
                self._json(state.fees_view())
            elif u.path == "/api/libraries":
                q = parse_qs(u.query)
                self._json(state.libraries_view(
                    q.get("venue_a", q.get("va", q.get("base", [""])))[0]
                    or None,
                    q.get("venue_b", q.get("vb", q.get("hedge", [""])))[0]
                    or None,
                    q.get("symbol", [""])[0] or None))
            elif u.path == "/api/libraries/funding":
                q = parse_qs(u.query)

                def _fnum(key):
                    try:
                        return float(q.get(key, [""])[0])
                    except (TypeError, ValueError):
                        return None

                self._json(state.libraries_funding_view(
                    q.get("va", q.get("venue_a", [""]))[0] or None,
                    q.get("vb", q.get("venue_b", [""]))[0] or None,
                    q.get("symbol", [""])[0] or None,
                    line=_fnum("line"), max_open_spread=_fnum("max_open_spread"),
                    current_net=_fnum("current_net")))
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
                                        body.get("params") or {},
                                        body.get("run_mode") or "manual",
                                        body.get("run_preset"),
                                        body.get("amount_usd"))
                self._json(res, 200 if res.get("ok") else 400)
            elif parts == ["api", "tasks", "update"]:
                body = self._json_body()
                if body is None:
                    return
                res = state.update_task(body.get("name"),
                                        body.get("params") or {},
                                        body.get("run_mode"),
                                        body.get("run_preset"),
                                        body.get("amount_usd"))
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
            elif parts == ["api", "settings", "fees"]:
                body = self._json_body()
                if body is None:
                    return
                res = state.save_fees_view(body)
                self._json(res, 200 if res.get("ok") else 400)
            else:
                self._json({"error": "not found"}, 404)

    return Handler


def _stop_library_service(svc) -> None:
    try:
        svc.stop()
    except Exception:  # noqa: BLE001 — 退出路径尽力而为
        pass


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
    # 中枢/费差库录制服务随面板启动（只读公开行情，paper/live
    # 都跑）；进程退出时经 atexit 停（其线程本身也是 daemon）。
    svc = getattr(state, "library_service", None)
    if svc is not None and not getattr(svc, "_detsp_started", False):
        try:
            svc.start()
            svc._detsp_started = True
            import atexit

            atexit.register(_stop_library_service, svc)
        except Exception:  # noqa: BLE001 — 库服务起不来不拖垮面板
            pass
    server = ThreadingHTTPServer((host, port), make_handler(state))
    th = threading.Thread(target=server.serve_forever, daemon=True,
                          name="detsp-web")
    th.start()
    return server
