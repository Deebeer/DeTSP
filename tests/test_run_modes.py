"""第九轮：两种运行模式（自主 manual / 全自动 auto）。

覆盖：run_mode 校验与缺省；自主模式下 carry 平仓后 cycle_done
且不重开（params 里 reopen=True 也一样）、rotation 只跑一轮、
premium 只做一笔；AutoCycle 监督器用假 scanner 跑通
「选标 A → 开 → 平 → 冷却 → 选标 B → 开」且轮数递增、stop 后
内层仓位已平；面板页含模式切换、server 创建 auto 任务后
/api/tasks 带 run_mode=auto 并落盘。
"""

import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from detsp.core.config import ConfigError, build_config
from detsp.core.engine import Engine, build_task, task_occupied_keys
from detsp.core.ledger import Ledger
from detsp.core.panel_tasks import load_panel_tasks
from detsp.strategies.funding_carry import CarryParams, FundingCarryStrategy
from detsp.venues.paper import PaperVenue
from detsp.web.page import PAGE_HTML
from detsp.web.server import PanelState, serve

PRICES = {"BTC": 60_000.0, "ETH": 3_000.0}


# ── 假扫描器：行序可翻转，模拟「下一轮最佳标的变了」 ──

def _row(symbol, edge=4.0, price=60_000.0):
    return {"symbol": symbol, "price_a": price, "price_b": price,
            "edge_bps_h": edge, "spread_bps": 10.0,
            "volatility_pct": 2.0, "has_price": True,
            "has_funding": True, "suspect": False, "mixed": False}


class FakeScanner:
    def __init__(self, rows):
        self.rows = list(rows)
        self.pairs_calls = 0

    def pairs(self, venue_a, venue_b, strategy):
        self.pairs_calls += 1
        return {"venue_a": venue_a, "venue_b": venue_b,
                "strategy": strategy, "rows": list(self.rows),
                "common": len(self.rows), "errors": {}}

    def candles(self, venue_id, symbol):
        return {"ok": False, "error": "假扫描器无 K 线"}


def _venues():
    a = PaperVenue(venue_id="arcus", prices=dict(PRICES), leverage=5.0)
    b = PaperVenue(venue_id="lighter", prices=dict(PRICES), leverage=5.0)
    for v in (a, b):
        pass
    a.set_funding("BTC", 0.0001)
    b.set_funding("BTC", 0.0005)
    a.set_funding("ETH", 0.0001)
    b.set_funding("ETH", 0.0005)
    return {"arcus": a, "lighter": b}


def _legs(engine, symbol="BTC"):
    pa = engine.executor.position("arcus", symbol)
    pb = engine.executor.position("lighter", symbol)
    return (pa.qty if pa else 0.0), (pb.qty if pb else 0.0)


CARRY_PARAMS = {"symbol": "BTC", "venue_a": "arcus", "venue_b": "lighter",
                "notional_usd": 300, "leverage": 5,
                "min_entry_edge_bps_per_hour": 2.0,
                "exit_edge_bps_per_hour": 1.0, "exit_confirm_checks": 2,
                "check_seconds": 10, "cooldown_seconds": 30}


def _engine(venues, task, scanner=None):
    cfg = build_config(env={}, yaml_data={"tasks": [task]})
    engine = Engine.from_config(cfg, venues, ledger=Ledger(),
                                scanner=scanner)
    name = task["name"]
    assert name not in engine.errors, engine.errors
    return engine


# ── 校验与缺省 ──

def test_run_mode_default_is_manual_and_single_cycle():
    engine = _engine(_venues(), {"name": "carry", "strategy": "funding_carry",
                                  "params": dict(CARRY_PARAMS)})
    task = engine.tasks[0]
    assert task.run_mode == "manual"
    assert task.runtime.single_cycle is True
    assert task.runtime.cycle_done is False


def test_direct_strategy_defaults_not_single_cycle():
    rt = FundingCarryStrategy(CarryParams(
        venue_a="arcus", venue_b="lighter", symbol="BTC",
        notional_usd=300))
    assert rt.single_cycle is False and rt.cycle_done is False


def test_run_mode_invalid_rejected():
    bad = {"name": "carry", "strategy": "funding_carry",
           "run_mode": "turbo", "params": dict(CARRY_PARAMS)}
    with pytest.raises(ConfigError):
        build_config(env={}, yaml_data={"tasks": [bad]})
    venues = _venues()
    cfg = build_config(env={}, yaml_data={"tasks": [{
        "name": "carry", "strategy": "funding_carry",
        "params": dict(CARRY_PARAMS)}]})
    engine = Engine.from_config(cfg, venues, ledger=Ledger())
    with pytest.raises(ValueError, match="manual"):
        build_task(bad, engine.executor, engine.risk_cfg)


def test_auto_task_occupied_keys_wildcard():
    cfg = {"name": "a1", "strategy": "funding_carry", "run_mode": "auto",
           "params": dict(CARRY_PARAMS)}
    assert task_occupied_keys(cfg) == {("arcus", "*"), ("lighter", "*")}
    manual = {"name": "a2", "strategy": "funding_carry",
              "params": dict(CARRY_PARAMS)}
    assert task_occupied_keys(manual) == {("arcus", "BTC"),
                                          ("lighter", "BTC")}


def test_auto_task_conflicts_with_any_symbol_same_venue():
    auto = {"name": "a1", "strategy": "funding_carry", "run_mode": "auto",
            "params": dict(CARRY_PARAMS)}
    other = {"name": "a2", "strategy": "funding_carry",
             "params": {**CARRY_PARAMS, "symbol": "ETH",
                        "venue_b": "hyperliquid"}}
    # other 的 arcus/ETH 与 auto 的 arcus/* 通配冲突
    with pytest.raises(ConfigError):
        build_config(env={}, yaml_data={"tasks": [auto, other]})


# ── 自主模式：carry 只跑一笔（reopen=True 也不重开） ──

def test_carry_manual_cycle_done_and_no_reopen():
    venues = _venues()
    engine = _engine(venues, {
        "name": "carry", "strategy": "funding_carry",
        "params": {**CARRY_PARAMS, "max_hold_seconds": 50.0,
                   "reopen": True, "cooldown_seconds": 30}})
    rt = engine.tasks[0].runtime
    engine.tick(now=1000)
    assert _legs(engine) == (pytest.approx(0.005), pytest.approx(-0.005))
    engine.tick(now=1051)   # 最长持有到 → 平仓
    assert _legs(engine) == (0.0, 0.0)
    assert rt.cycle_done is True
    # params 原值没被改（运行时语义，不是改配置）
    assert rt.params.reopen is True
    # 引擎已停用该任务并记中文事件
    assert engine.tasks[0].disabled is True
    assert any("自主模式：本笔已平仓，任务结束" in e
               for e in engine.events)
    engine.tick(now=1200)   # 冷却早过、优势还在，也绝不重开
    assert _legs(engine) == (0.0, 0.0)


# ── 自主模式：rotation 只跑一轮 ──

def test_rotation_manual_only_one_round():
    venues = _venues()
    engine = _engine(venues, {
        "name": "hedge", "strategy": "rotation_hedge",
        "params": {"symbol": "BTC", "venue_a": "arcus",
                   "venue_b": "lighter", "notional_usd": 300,
                   "hold_seconds": 60, "hold_min_seconds": 0,
                   "hold_max_seconds": 0, "cooldown_seconds": 30,
                   "leverage_a": 5, "leverage_b": 5,
                   "arcus_maker": False}})
    rt = engine.tasks[0].runtime
    engine.tick(now=1000)
    assert _legs(engine) == (pytest.approx(0.005), pytest.approx(-0.005))
    engine.tick(now=1061)   # 到期平仓，第一轮结束
    assert _legs(engine) == (0.0, 0.0)
    assert rt.cycle_done is True
    assert engine.tasks[0].disabled is True
    engine.tick(now=1200)   # 冷却早过，不进第二轮
    assert _legs(engine) == (0.0, 0.0)
    assert rt.phase != "open"


# ── 自主模式：premium 只做一笔 ──

def test_premium_manual_only_one_cycle():
    base = PaperVenue(venue_id="entropy", prices={"SNDK": 100.0})
    hedge = PaperVenue(venue_id="lighter_rh", prices={"SNDK": 99.0})
    venues = {"entropy": base, "lighter_rh": hedge}
    engine = _engine(venues, {
        "name": "arb", "strategy": "premium_arb",
        "params": {"symbol": "SNDK", "base_venue": "entropy",
                   "hedge_venue": "lighter_rh", "midline_bps": 0,
                   "upper_bps": 50, "lower_bps": 50, "persist_seconds": 0,
                   "max_position_usd": 1000,
                   "max_order_notional_usd": 500,
                   "reconcile_seconds": 15}})
    rt = engine.tasks[0].runtime
    engine.tick(now=0)      # 溢价信号 → 开仓 -5/+5
    pa = engine.executor.position("entropy", "SNDK")
    assert pa is not None and pa.qty == pytest.approx(-5)
    hedge.set_price("SNDK", 101.0)   # 溢价反转 → 反向一笔平掉
    engine.tick(now=1)
    assert engine.executor.position("entropy", "SNDK") is None
    assert engine.executor.position("lighter_rh", "SNDK") is None
    assert rt.cycle_done is True
    assert engine.tasks[0].disabled is True
    engine.tick(now=2)      # 信号还在，也不许再开新仓
    assert engine.executor.position("entropy", "SNDK") is None


# ── 全自动：监督器选标→开→平→冷却→换标再开 ──

def test_auto_cycle_scans_opens_cools_rescans():
    venues = _venues()
    scanner = FakeScanner([_row("BTC"), _row("ETH", price=3_000.0)])
    engine = _engine(venues, {
        "name": "carry-auto", "strategy": "funding_carry",
        "run_mode": "auto",
        "params": {**CARRY_PARAMS, "max_hold_seconds": 50.0,
                   "cooldown_seconds": 5}}, scanner=scanner)
    rt = engine.tasks[0].runtime
    assert engine.tasks[0].run_mode == "auto"
    assert rt.auto_phase in ("scanning", "running")

    engine.tick(now=1000)   # 选标 BTC → 开仓
    assert rt.current_symbol == "BTC"
    assert rt.auto_phase == "running"
    assert _legs(engine, "BTC") == (pytest.approx(0.005),
                                    pytest.approx(-0.005))

    engine.tick(now=1051)   # 最长持有到 → 内层平仓，监督器记一轮
    assert _legs(engine, "BTC") == (0.0, 0.0)
    assert rt.cycles_completed == 1
    assert rt.auto_phase == "cooldown"
    # claims 已释放（BTC 两腿无归属）
    ex = engine.executor
    assert ex.registry.owner_of(ex.key("arcus", "BTC")) is None
    assert ex.registry.owner_of(ex.key("lighter", "BTC")) is None

    # 下一轮最佳标的变成 ETH
    scanner.rows = [_row("ETH", price=3_000.0), _row("BTC")]
    engine.tick(now=1055)   # 冷却未完（到 1056）：不开
    assert _legs(engine, "ETH") == (0.0, 0.0)
    engine.tick(now=1057)   # 冷却完 → 重新选标 ETH → 开仓
    assert rt.current_symbol == "ETH"
    assert rt.auto_phase == "running"
    assert _legs(engine, "ETH") == (pytest.approx(0.1),
                                    pytest.approx(-0.1))
    assert rt.cycles_completed == 1

    rt.stop()               # 停止：内层仓位已平
    assert _legs(engine, "ETH") == (0.0, 0.0)
    assert rt.auto_phase == "stopped"


def test_auto_cycle_no_candidate_stays_scanning():
    venues = _venues()
    scanner = FakeScanner([_row("BTC", edge=0.5)])   # 低于入场门槛
    engine = _engine(venues, {
        "name": "carry-auto", "strategy": "funding_carry",
        "run_mode": "auto", "params": dict(CARRY_PARAMS)},
        scanner=scanner)
    rt = engine.tasks[0].runtime
    engine.tick(now=1000)
    engine.tick(now=2000)
    assert rt.auto_phase == "scanning"
    assert rt.current_symbol is None
    assert rt.last_error   # 中文短句说明选不到
    assert _legs(engine) == (0.0, 0.0)


def test_auto_grid_uses_price_band_when_no_candles():
    venues = _venues()
    scanner = FakeScanner([_row("BTC")])
    engine = _engine(venues, {
        "name": "grid-auto", "strategy": "hedged_grid", "run_mode": "auto",
        "params": {"grid_venue": "arcus", "hedge_venue": "lighter",
                   "symbol": "BTC", "lower_price": 1.0,
                   "upper_price": 2.0, "grid_count": 10,
                   "qty_per_grid": 0.01}}, scanner=scanner)
    rt = engine.tasks[0].runtime
    engine.tick(now=1000)
    assert rt.current_symbol == "BTC"
    assert rt.auto_phase == "running"
    # 无 K 线建议区间时用现价 ±10% 定区间：54000 ~ 66000
    inner = rt._inner
    assert inner is not None
    assert inner.params.lower_price == pytest.approx(54_000.0)
    assert inner.params.upper_price == pytest.approx(66_000.0)
    assert len(venues["arcus"].open_orders("BTC")) > 0
    rt.stop()


# ── 面板：模式切换文案 + 创建 auto 任务落盘 ──

def test_page_has_run_mode_switch_and_descriptions():
    assert 'data-mode="manual"' in PAGE_HTML
    assert 'data-mode="auto"' in PAGE_HTML
    assert "自主模式" in PAGE_HTML and "全自动模式" in PAGE_HTML
    assert "不自动重开" in PAGE_HTML          # 自主说明大白话
    assert "一直循环，点停止才停" in PAGE_HTML  # 全自动说明大白话
    assert "由系统自动选" in PAGE_HTML        # 选市场区的自动选标注明
    assert "run_mode" in PAGE_HTML            # 提交带模式


def _req(base, path, method="GET", token=None, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, method=method, data=data)
    if token:
        req.add_header("X-API-Token", token)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


@pytest.fixture()
def panel(tmp_path):
    cfg = build_config(env={}, yaml_data={})
    venues = {"arcus": PaperVenue(venue_id="arcus",
                                   prices={"BTC": 60_000.0}),
              "lighter": PaperVenue(venue_id="lighter",
                                     prices={"BTC": 60_000.0})}
    ledger = Ledger()
    scanner = FakeScanner([_row("BTC")])
    engine = Engine.from_config(cfg, venues, ledger=ledger,
                                scanner=scanner)
    store = tmp_path / "panel_tasks.yaml"
    state = PanelState(engine=engine, venues=venues, ledger=ledger,
                       app_config=cfg, task_cfgs={},
                       panel_store_path=store, market_scanner=scanner)
    server = serve(state, "127.0.0.1", 0)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    stop_flag = threading.Event()

    def _pump():
        while not stop_flag.is_set():
            state.engine.process_commands()
            time.sleep(0.01)

    pump = threading.Thread(target=_pump, daemon=True)
    pump.start()
    yield base, state, store
    stop_flag.set()
    server.shutdown()


ROT_PARAMS = {"symbol": "BTC", "venue_a": "arcus", "venue_b": "lighter",
              "notional_usd": 100.0, "leverage_a": 1, "leverage_b": 1}


def test_panel_create_auto_task_persists_run_mode(panel):
    base, state, store = panel
    status, res = _req(base, "/api/tasks/create", "POST",
                       token=state.api_token,
                       body={"name": "auto-1", "strategy": "rotation_hedge",
                             "params": ROT_PARAMS, "run_mode": "auto"})
    assert status == 200 and res["ok"] is True
    _, body = _req(base, "/api/tasks")
    t = next(x for x in body["tasks"] if x["name"] == "auto-1")
    assert t["run_mode"] == "auto"
    assert t["auto_phase"] == "scanning"
    assert t["current_symbol"] is None
    assert t["cycles_completed"] == 0
    saved = load_panel_tasks(store)
    assert saved[0]["run_mode"] == "auto"
    # 非法模式被拒
    status, res = _req(base, "/api/tasks/create", "POST",
                       token=state.api_token,
                       body={"name": "auto-2", "strategy": "rotation_hedge",
                             "params": ROT_PARAMS, "run_mode": "turbo"})
    assert status == 400 and "运行模式" in res["error"]


def test_panel_create_manual_task_default_mode(panel):
    base, state, store = panel
    status, res = _req(base, "/api/tasks/create", "POST",
                       token=state.api_token,
                       body={"name": "man-1", "strategy": "rotation_hedge",
                             "params": ROT_PARAMS})
    assert status == 200 and res["ok"] is True
    _, body = _req(base, "/api/tasks")
    t = next(x for x in body["tasks"] if x["name"] == "man-1")
    assert t["run_mode"] == "manual"
    assert t["auto_phase"] == "" and t["cycles_completed"] == 0
    assert load_panel_tasks(store)[0]["run_mode"] == "manual"
