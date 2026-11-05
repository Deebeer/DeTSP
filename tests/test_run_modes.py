"""第九轮两种模式 + 第十轮三模式四值（manual / semi_fixed /
semi_auto / full_auto，旧值 auto 归一 semi_auto）。

覆盖：run_mode 四值校验、缺省与归一；第十轮 manual 改为立即
开仓、人工平仓（自动平仓条件不生效）；semi_fixed 原生同标的
循环；AutoCycle 监督器（pair 固定两腿选标 / all_pairs 全自动
跨所选组合）跑通选标→开→平→冷却→再选标且轮数递增、stop 后
内层仓位已平；premium 回归平仓带；引擎拒绝已删除的
rotation_hedge 并列出三策略清单；specs 五段映射全覆盖；面板
页含四模式切换、server 创建任务传 run_mode=auto 落盘归一为
semi_auto。
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
                "min_edge_bps_per_hour": 2.0}


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
    # 第十二轮：预设全砍，Task 不再有 run_preset
    assert not hasattr(task, "run_preset")
    assert task.runtime.single_cycle is True
    # 第十轮：自主模式同时设 manual_immediate（立即开仓语义）
    assert task.runtime.manual_immediate is True
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


# ── 自主模式：carry 立即开仓且不自动退出（第十轮语义大改） ──
# 第九轮原用例 test_carry_manual_cycle_done_and_no_reopen 断言
# 「达门槛开仓、最长持有到期自动平仓置 cycle_done」，与第十轮
# manual 语义（不看门槛立即开、退出检查不触发、人工 stop 平仓）
# 冲突，已按新语义改写为下面两个用例；原生循环行为移交
# semi_fixed 用例覆盖（见本文件后部）。

def test_carry_manual_immediate_opens_below_threshold_and_never_auto_exits():
    venues = _venues()
    # 费差只有 1 bps/小时（a 0.0001 / b 0.0002），低于入场门槛 2
    venues["lighter"].set_funding("BTC", 0.0002)
    engine = _engine(venues, {
        "name": "carry", "strategy": "funding_carry",
        "params": {**CARRY_PARAMS, "reopen": True}})
    rt = engine.tasks[0].runtime
    engine.tick(now=1000)   # 不看门槛：首 tick 立即按最优方向开仓
    assert _legs(engine) == (pytest.approx(0.005), pytest.approx(-0.005))
    assert rt.phase == "open"
    # 费差跌破最低费差线（多腿 a 费率反超）：manual 不自动平
    venues["arcus"].set_funding("BTC", 0.0009)
    engine.tick(now=1051)
    engine.tick(now=1100)
    engine.tick(now=1200)
    assert _legs(engine) == (pytest.approx(0.005), pytest.approx(-0.005))
    assert rt.cycle_done is False
    assert engine.tasks[0].disabled is False
    # 人工 stop 照常全平
    rt.stop()
    assert _legs(engine) == (0.0, 0.0)


# ── 已删除策略：rotation_hedge 被引擎与 config 双层拒绝 ──

def test_rotation_hedge_removed_and_rejected():
    bad = {"name": "hedge", "strategy": "rotation_hedge",
           "params": {"symbol": "BTC", "venue_a": "arcus",
                      "venue_b": "lighter", "notional_usd": 300}}
    # config 层：已移除名单，报错列出三策略清单
    with pytest.raises(ConfigError, match="已移除.*hedged_grid / premium_arb / funding_carry"):
        build_config(env={}, yaml_data={"tasks": [bad]})
    # 引擎组装层：口径一致
    venues = _venues()
    cfg = build_config(env={}, yaml_data={"tasks": []})
    eng = Engine.from_config(cfg, venues, ledger=Ledger())
    with pytest.raises(ValueError, match="已移除.*hedged_grid / premium_arb / funding_carry"):
        build_task(bad, eng.executor, eng.risk_cfg)


# ── 自主模式：premium 首 tick 打一笔、之后不 fire、人工 stop 平 ──
# 第九轮原用例断言「溢价反转自动反向平仓」，与第十轮 manual
# 语义（一笔后不再 fire、平仓靠人工 stop）冲突，已按新语义改写。

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
    engine.tick(now=0)      # 首 tick 立即打一笔配对切片 -5/+5
    pa = engine.executor.position("entropy", "SNDK")
    assert pa is not None and pa.qty == pytest.approx(-5)
    hedge.set_price("SNDK", 101.0)   # 溢价反转：manual 不再 fire
    engine.tick(now=1)
    pa = engine.executor.position("entropy", "SNDK")
    assert pa is not None and pa.qty == pytest.approx(-5)
    assert rt.cycle_done is False
    assert engine.tasks[0].disabled is False
    rt.stop()               # 人工 stop 全平，本笔结束
    assert engine.executor.position("entropy", "SNDK") is None
    assert engine.executor.position("lighter_rh", "SNDK") is None
    assert rt.cycle_done is True
    engine.tick(now=2)      # 引擎据 cycle_done 停用，绝不再开
    assert engine.tasks[0].disabled is True
    assert engine.executor.position("entropy", "SNDK") is None


# ── 全自动：监督器选标→开→平→冷却→换标再开 ──

def test_auto_cycle_scans_opens_cools_rescans():
    venues = _venues()
    scanner = FakeScanner([_row("BTC"), _row("ETH", price=3_000.0)])
    engine = _engine(venues, {
        "name": "carry-auto", "strategy": "funding_carry",
        "run_mode": "auto",
        "params": dict(CARRY_PARAMS)}, scanner=scanner)
    rt = engine.tasks[0].runtime
    # 第十轮：旧名 auto 归一为 semi_auto（半自动·自动选标）
    assert engine.tasks[0].run_mode == "semi_auto"
    assert rt.selection_scope == "pair"
    assert rt.current_venues == ["arcus", "lighter"]
    assert rt.auto_phase in ("scanning", "running")

    engine.tick(now=1000)   # 选标 BTC → 开仓
    assert rt.current_symbol == "BTC"
    assert rt.auto_phase == "running"
    assert _legs(engine, "BTC") == (pytest.approx(0.005),
                                    pytest.approx(-0.005))

    # 费差跌破最低费差线：连续确认 3 次（系统值）后内层平仓
    venues["lighter"].set_funding("BTC", 0.00012)
    engine.tick(now=1031)
    engine.tick(now=1062)
    assert _legs(engine, "BTC")[0] == pytest.approx(0.005)
    engine.tick(now=1093)   # 第 3 次确认 → 平仓，监督器记一轮
    assert _legs(engine, "BTC") == (0.0, 0.0)
    assert rt.cycles_completed == 1
    assert rt.auto_phase == "cooldown"
    # claims 已释放（BTC 两腿无归属）
    ex = engine.executor
    assert ex.registry.owner_of(ex.key("arcus", "BTC")) is None
    assert ex.registry.owner_of(ex.key("lighter", "BTC")) is None

    # 下一轮最佳标的变成 ETH
    scanner.rows = [_row("ETH", price=3_000.0), _row("BTC")]
    engine.tick(now=1110)   # 冷却未完（30 秒兜底）：不开
    assert _legs(engine, "ETH") == (0.0, 0.0)
    engine.tick(now=1124)   # 冷却完 → 重新选标 ETH → 开仓
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
    # 第十轮：四值三模式切换（旧值 auto 已取消，归一为 semi_auto）
    for m in ("manual", "semi_fixed", "semi_auto", "full_auto"):
        assert f'data-mode="{m}"' in PAGE_HTML
    assert "半自动·固定标的" in PAGE_HTML and "半自动·自动选标" in PAGE_HTML
    assert "立即开仓" in PAGE_HTML            # 自主说明大白话
    assert "循环到你停止" in PAGE_HTML        # 全自动说明大白话
    assert "标的由系统自动选" in PAGE_HTML    # 选市场区的自动选标注明
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


CARRY_PANEL_PARAMS = {"symbol": "BTC", "venue_a": "arcus",
                      "venue_b": "lighter", "notional_usd": 100.0,
                      "leverage": 1}


def test_panel_create_auto_task_persists_run_mode(panel):
    base, state, store = panel
    status, res = _req(base, "/api/tasks/create", "POST",
                       token=state.api_token,
                       body={"name": "auto-1", "strategy": "funding_carry",
                             "params": CARRY_PANEL_PARAMS, "run_mode": "auto"})
    assert status == 200 and res["ok"] is True
    _, body = _req(base, "/api/tasks")
    t = next(x for x in body["tasks"] if x["name"] == "auto-1")
    # 第十轮：创建时传旧值 auto，落盘与输出一律归一为 semi_auto
    assert t["run_mode"] == "semi_auto"
    assert t["auto_phase"] == "scanning"
    assert t["current_symbol"] is None
    assert t["cycles_completed"] == 0
    saved = load_panel_tasks(store)
    assert saved[0]["run_mode"] == "semi_auto"
    # 非法模式被拒
    status, res = _req(base, "/api/tasks/create", "POST",
                       token=state.api_token,
                       body={"name": "auto-2", "strategy": "funding_carry",
                             "params": CARRY_PANEL_PARAMS, "run_mode": "turbo"})
    assert status == 400 and "run_mode 非法" in res["error"]


def test_panel_create_manual_task_default_mode(panel):
    base, state, store = panel
    status, res = _req(base, "/api/tasks/create", "POST",
                       token=state.api_token,
                       body={"name": "man-1", "strategy": "funding_carry",
                             "params": CARRY_PANEL_PARAMS})
    assert status == 200 and res["ok"] is True
    _, body = _req(base, "/api/tasks")
    t = next(x for x in body["tasks"] if x["name"] == "man-1")
    assert t["run_mode"] == "manual"
    assert t["auto_phase"] == "" and t["cycles_completed"] == 0
    assert load_panel_tasks(store)[0]["run_mode"] == "manual"


# ═══════════ 第十轮：三模式后端 ═══════════

from detsp.core.engine import normalize_run_mode


def test_normalize_run_mode_four_values_and_auto_alias():
    assert normalize_run_mode(None) == "manual"
    assert normalize_run_mode("") == "manual"
    assert normalize_run_mode("manual") == "manual"
    assert normalize_run_mode("semi_fixed") == "semi_fixed"
    assert normalize_run_mode("semi_auto") == "semi_auto"
    assert normalize_run_mode("full_auto") == "full_auto"
    assert normalize_run_mode("auto") == "semi_auto"
    with pytest.raises(ValueError, match="manual"):
        normalize_run_mode("turbo")
    # 第十二轮：预设全砍，normalize_run_preset 不再存在
    import detsp.core.engine as _eng
    assert not hasattr(_eng, "normalize_run_preset")


def test_modes_normalized_in_config_and_build_task():
    # config 入口：auto 归一 semi_auto 并写回，amount 校验；
    # run_preset（第十二轮预设全砍）老任务带了容忍并忽略、
    # 写回丢弃——连非法值也不再报错
    cfg = build_config(env={}, yaml_data={"tasks": [{
        "name": "c1", "strategy": "funding_carry", "run_mode": "auto",
        "run_preset": "wild", "amount_usd": 500,
        "params": dict(CARRY_PARAMS)}]})
    t0 = cfg.tasks[0]
    assert t0["run_mode"] == "semi_auto"
    assert "run_preset" not in t0
    assert t0["amount_usd"] == 500.0
    for bad in ({"run_mode": "turbo"},
                {"amount_usd": -1}, {"amount_usd": "x"}):
        with pytest.raises(ConfigError):
            build_config(env={}, yaml_data={"tasks": [{
                "name": "c1", "strategy": "funding_carry",
                "params": dict(CARRY_PARAMS), **bad}]})
    # build_task 入口：四值各归其位（空任务引擎的 executor 上组装；
    # 直接构造的两个模式用不同标的避开归属 claim 冲突）
    venues = _venues()
    cfg2 = build_config(env={}, yaml_data={"tasks": []})
    eng = Engine.from_config(cfg2, venues, ledger=Ledger())
    for mode, is_sup, sym in (("manual", False, "BTC"),
                              ("semi_fixed", False, "ETH"),
                              ("semi_auto", True, "BTC"),
                              ("full_auto", True, "ETH")):
        task = build_task({"name": f"t-{mode}", "strategy": "funding_carry",
                           "run_mode": mode,
                           "params": {**CARRY_PARAMS, "symbol": sym}},
                          eng.executor, eng.risk_cfg,
                          scanner=FakeScanner([_row("BTC")]))
        assert task.run_mode == mode
        assert not hasattr(task, "run_preset")
        if is_sup:
            assert task.runtime.selection_scope == \
                ("pair" if mode == "semi_auto" else "all_pairs")
            task.runtime.stop()
        else:
            assert task.runtime.single_cycle is (mode == "manual")
            assert task.runtime.manual_immediate is (mode == "manual")
    # 老任务带 run_preset（任意值）容忍并忽略，正常组装
    # （另起一个干净引擎，避开上面任务的归属占用）
    eng2 = Engine.from_config(cfg2, _venues(), ledger=Ledger())
    legacy = build_task({"name": "legacy", "strategy": "funding_carry",
                         "run_preset": "wild",
                         "params": {**CARRY_PARAMS, "symbol": "ETH"}},
                        eng2.executor, eng2.risk_cfg)
    assert legacy.run_mode == "manual"
    assert not hasattr(legacy, "run_preset")


def test_full_auto_occupied_keys_and_config_conflict():
    full = {"name": "f1", "strategy": "funding_carry",
            "run_mode": "full_auto", "params": dict(CARRY_PARAMS)}
    assert task_occupied_keys(full) == {("*", "*")}
    other = {"name": "m1", "strategy": "funding_carry",
             "params": {**CARRY_PARAMS, "symbol": "ETH"}}
    with pytest.raises(ConfigError):
        build_config(env={}, yaml_data={"tasks": [full, other]})
    with pytest.raises(ConfigError):
        build_config(env={}, yaml_data={"tasks": [other, full]})


# ── semi_fixed：carry 原生循环（reopen 第二轮不被截断） ──

def test_semi_fixed_carry_reopen_cycles_again():
    venues = _venues()
    engine = _engine(venues, {
        "name": "carry", "strategy": "funding_carry",
        "run_mode": "semi_fixed",
        "params": {**CARRY_PARAMS, "reopen": True}})
    rt = engine.tasks[0].runtime
    assert rt.single_cycle is False and rt.manual_immediate is False
    engine.tick(now=1000)
    assert _legs(engine) == (pytest.approx(0.005), pytest.approx(-0.005))
    venues["lighter"].set_funding("BTC", 0.00012)   # 费差跌破线
    engine.tick(now=1031)
    engine.tick(now=1062)
    engine.tick(now=1093)   # 连续确认 3 次 → 平仓进冷却（原生退出）
    assert _legs(engine) == (0.0, 0.0)
    assert rt.cycle_done is False
    assert engine.tasks[0].disabled is False
    venues["lighter"].set_funding("BTC", 0.0005)    # 优势恢复
    engine.tick(now=1200)   # 冷却（60 秒系统值）已过 → 原生第二轮
    assert _legs(engine) == (pytest.approx(0.005), pytest.approx(-0.005))
    assert engine.tasks[0].disabled is False


# ── premium 回归平仓带（新字段） ──

def _premium_engine(close_band=0.0, run_mode="semi_fixed"):
    base = PaperVenue(venue_id="entropy", prices={"SNDK": 100.0})
    hedge = PaperVenue(venue_id="lighter_rh", prices={"SNDK": 99.0})
    venues = {"entropy": base, "lighter_rh": hedge}
    engine = _engine(venues, {
        "name": "arb", "strategy": "premium_arb", "run_mode": run_mode,
        "params": {"symbol": "SNDK", "base_venue": "entropy",
                   "hedge_venue": "lighter_rh", "midline_bps": 0,
                   "upper_bps": 50, "lower_bps": 50, "persist_seconds": 0,
                   "max_position_usd": 1000,
                   "max_order_notional_usd": 500,
                   "reconcile_seconds": 15,
                   "close_band_bps": close_band}})
    return engine, hedge


def _premium_legs(engine):
    pb = engine.executor.position("entropy", "SNDK")
    ph = engine.executor.position("lighter_rh", "SNDK")
    return (pb.qty if pb else 0.0), (ph.qty if ph else 0.0)


def test_premium_close_band_flattens_on_regression():
    engine, hedge = _premium_engine(close_band=20.0)
    rt = engine.tasks[0].runtime
    engine.tick(now=0)      # 越带开仓 -5/+5
    assert _premium_legs(engine) == (pytest.approx(-5), pytest.approx(5))
    hedge.set_price("SNDK", 100.05)   # 价差回归到 ≈-5 bps，进带内
    engine.tick(now=1)
    assert _premium_legs(engine) == (0.0, 0.0)
    assert rt._last_fire == 1   # 复用执行冷却：平仓刷新 _last_fire


def test_premium_close_band_sets_cycle_done_in_manual():
    engine, hedge = _premium_engine(close_band=20.0, run_mode="manual")
    rt = engine.tasks[0].runtime
    engine.tick(now=0)      # manual 首笔 -5/+5
    assert _premium_legs(engine) == (pytest.approx(-5), pytest.approx(5))
    hedge.set_price("SNDK", 100.05)
    engine.tick(now=1)      # 回归平仓 + single_cycle 置 cycle_done
    assert _premium_legs(engine) == (0.0, 0.0)
    assert rt.cycle_done is True
    assert rt._last_fire == 1


def test_premium_close_band_zero_never_triggers():
    engine, hedge = _premium_engine(close_band=0.0)
    engine.tick(now=0)
    assert _premium_legs(engine) == (pytest.approx(-5), pytest.approx(5))
    hedge.set_price("SNDK", 100.05)   # 同样回归，带宽 0=关：不平
    engine.tick(now=1)
    assert _premium_legs(engine) == (pytest.approx(-5), pytest.approx(5))


# ── specs 五段映射 ──

def test_section_map_covers_every_field_exactly_once():
    import dataclasses
    import string as _string

    from detsp.strategies.specs import (
        PAGE_META, PARAMS_CLASSES, SECTION_MAP, SECTION_ORDER,
        SYSTEM_KEYS, strategy_specs)

    assert SECTION_ORDER == ("交易对象", "开仓条件", "平仓条件",
                             "平仓后怎么办", "保护")
    specs = {s["strategy"]: s for s in strategy_specs()}
    for stype, cls in PARAMS_CLASSES.items():
        names = [f.name for f in dataclasses.fields(cls)]
        mapping = SECTION_MAP[stype]
        # 每字段恰好一段：可见字段都在映射内；hidden 系统值可登记
        # 在映射内（如 funding）也可不在（如 premium 已删键）——
        # 映射 ∪ 系统值键须恰好覆盖全部字段，段值合法
        assert set(mapping) | set(SYSTEM_KEYS[stype]) == set(names), stype
        assert set(mapping) <= set(names), stype
        assert set(mapping.values()) <= set(SECTION_ORDER), stype
        # specs 输出的 section 与映射一致（hidden 不输出）
        for f in specs[stype]["fields"]:
            assert f["section"] == mapping[f["name"]]
        # 段总结：键是段名（grid 无平仓后段）、占位符都是真字段
        summaries = specs[stype]["page"]["section_summaries"]
        assert summaries == PAGE_META[stype]["section_summaries"]
        expect_keys = {"开仓条件", "平仓条件", "保护"}
        if stype != "hedged_grid":
            expect_keys.add("平仓后怎么办")
        assert set(summaries) == expect_keys, stype
        for seg, tpl in summaries.items():
            holders = [fn for _, fn, _, _ in
                       _string.Formatter().parse(tpl) if fn]
            assert holders, (stype, seg)
            assert set(holders) <= set(names), (stype, seg, holders)


# ── full_auto：跨所选组合 + 内层组装 ──

class FakeBestScanner:
    """full_auto 假扫描器：best 第一行即选中组合。"""

    def __init__(self, rows):
        self.rows = list(rows)

    def best(self, strategy, limit=5):
        return {"strategy": strategy, "rows": list(self.rows)}

    def pairs(self, venue_a, venue_b, strategy):
        return {"rows": []}

    def candles(self, venue_id, symbol):
        return {"ok": False, "error": "假扫描器无 K 线"}


def test_full_auto_premium_assembles_from_best_row():
    base = PaperVenue(venue_id="entropy", prices={"SNDK": 100.0})
    hedge = PaperVenue(venue_id="lighter_rh", prices={"SNDK": 99.0})
    venues = {"entropy": base, "lighter_rh": hedge}
    scanner = FakeBestScanner([{
        "symbol": "SNDK", "venue_a": "entropy", "venue_b": "lighter_rh",
        "signed_spread_bps": 123.456, "spread_bps": 123.456,
        "price_a": 100.0, "price_b": 99.0, "has_price": True,
        "suspect": False}])
    engine = _engine(venues, {
        "name": "arb-full", "strategy": "premium_arb",
        "run_mode": "full_auto",
        "params": {"max_position_usd": 1000,
                   "max_order_notional_usd": 500}}, scanner=scanner)
    task = engine.tasks[0]
    assert task.run_mode == "full_auto"
    assert not hasattr(task, "run_preset")
    rt = task.runtime
    engine.tick(now=0)
    assert rt.auto_phase == "running"
    assert rt.current_symbol == "SNDK"
    assert rt.current_venues == ["entropy", "lighter_rh"]
    inner = rt._inner
    assert inner is not None and inner.single_cycle is True
    assert inner.params.base_venue == "entropy"
    assert inner.params.hedge_venue == "lighter_rh"
    assert inner.params.symbol == "SNDK"
    # 中枢 = 快照带符号价差取整到 0.1；上下带没给 → 系统默认 10/10
    assert inner.params.midline_bps == pytest.approx(123.5)
    assert inner.params.upper_bps == 10.0
    assert inner.params.lower_bps == 10.0
    rt.stop()


def test_full_auto_grid_qty_from_amount_usd():
    venues = _venues()
    scanner = FakeBestScanner([{
        "symbol": "BTC", "venue": "arcus", "venue_b": "lighter",
        "price": 60_000.0, "volatility_pct": 3.0, "suspect": False}])
    engine = _engine(venues, {
        "name": "grid-full", "strategy": "hedged_grid",
        "run_mode": "full_auto", "amount_usd": 6_000.0,
        "params": {"grid_count": 10}}, scanner=scanner)
    rt = engine.tasks[0].runtime
    engine.tick(now=1000)
    assert rt.auto_phase == "running"
    assert rt.current_venues == ["arcus", "lighter"]
    inner = rt._inner
    assert inner is not None
    # 每格量 = 总名义（保证金 6000 × 网格腿杠杆 3，内置中性
    # 档）/（格数 10 × 现价 60000）= 0.03
    assert inner.params.qty_per_grid == pytest.approx(0.03)
    assert inner.params.lower_price == pytest.approx(54_000.0)
    assert inner.params.upper_price == pytest.approx(66_000.0)
    rt.stop()


def test_full_auto_grid_without_amount_or_qty_stays_scanning():
    venues = _venues()
    scanner = FakeBestScanner([{
        "symbol": "BTC", "venue": "arcus", "venue_b": "lighter",
        "price": 60_000.0, "volatility_pct": 3.0, "suspect": False}])
    engine = _engine(venues, {
        "name": "grid-full", "strategy": "hedged_grid",
        "run_mode": "full_auto", "params": {}}, scanner=scanner)
    rt = engine.tasks[0].runtime
    engine.tick(now=1000)
    assert rt.auto_phase == "scanning"
    assert rt._inner is None
    assert "缺少每格数量" in rt.last_error


def test_full_auto_no_candidate_stays_scanning():
    venues = _venues()
    scanner = FakeBestScanner([])
    engine = _engine(venues, {
        "name": "carry-full", "strategy": "funding_carry",
        "run_mode": "full_auto",
        "params": {"notional_usd": 300}}, scanner=scanner)
    rt = engine.tasks[0].runtime
    engine.tick(now=1000)
    assert rt.auto_phase == "scanning"
    assert rt.current_symbol is None
    assert rt.last_error


# ── 第十一轮：AutoCycle 接库选标 ──

class _FakeFeeTable:
    def get(self, venue_id):
        return (5.0, 0.0)


class _FakeLibrary:
    """假统计库：premium 两标的都有 entry（ETH 触发次数更高）；
    funding 两标的样本充足、ETH 分数更高；gate 可切换放行。"""

    def __init__(self, gate_ok=True):
        self.gate_ok = gate_ok
        self.gate_calls = []

    def premium_entry(self, base, hedge, symbol):
        entries = {
            "BTC": {"midline_bps": 1.0, "upper_bps": 9.0,
                    "lower_bps": 9.0, "sell_fires": 1, "buy_fires": 8,
                    "sample_hours": 30.0, "updated_at": 0},
            "ETH": {"midline_bps": 12.5, "upper_bps": 30.0,
                    "lower_bps": 25.0, "sell_fires": 5, "buy_fires": 7,
                    "sample_hours": 30.0, "updated_at": 0},
        }
        return entries.get(symbol)

    def funding_stats(self, va, vb, symbol, min_line, now):
        stats = {
            "BTC": {"median_duration_h": 10.0, "above_ratio": 0.5,
                    "avg_net_bps_per_hour": 2.0, "sample_hours": 100.0},
            "ETH": {"median_duration_h": 10.0, "above_ratio": 0.5,
                    "avg_net_bps_per_hour": 3.0, "sample_hours": 100.0},
        }
        return stats.get(symbol)

    def funding_gate(self, va, vb, symbol, min_line, cost, current, now):
        self.gate_calls.append((va, vb, symbol, min_line, cost, current))
        if self.gate_ok:
            return {"ok": True, "reason": "通过"}
        return {"ok": False, "reason": "持续性不足"}


def test_auto_premium_library_ranks_by_fires_and_overrides_bands():
    venues = _venues()
    scanner = FakeScanner([_row("BTC"), _row("ETH", price=3_000.0)])
    engine = _engine(venues, {
        "name": "arb-auto", "strategy": "premium_arb",
        "run_mode": "semi_auto",
        "params": {"base_venue": "arcus", "hedge_venue": "lighter",
                   "max_position_usd": 1000,
                   "max_order_notional_usd": 500,
                   # 占位三带：接库后内层组装时被库值覆盖
                   "midline_bps": 999.0, "upper_bps": 99.0,
                   "lower_bps": 99.0}}, scanner=scanner)
    rt = engine.tasks[0].runtime
    rt.library = _FakeLibrary()
    engine.tick(now=1000)
    # BTC 行在前但 min(触发)=1 < ETH 的 5 → 选 ETH，三带用库值
    assert rt.current_symbol == "ETH"
    inner = rt._inner
    assert inner is not None
    assert inner.params.midline_bps == pytest.approx(12.5)
    assert inner.params.upper_bps == pytest.approx(30.0)
    assert inner.params.lower_bps == pytest.approx(25.0)
    rt.stop()


def test_auto_funding_library_gate_score_and_entry_gate():
    venues = _venues()
    scanner = FakeScanner([_row("BTC", edge=4.0),
                           _row("ETH", edge=6.0, price=3_000.0)])
    engine = _engine(venues, {
        "name": "carry-auto", "strategy": "funding_carry",
        "run_mode": "semi_auto",
        "params": dict(CARRY_PARAMS)}, scanner=scanner)
    rt = engine.tasks[0].runtime
    lib = _FakeLibrary()
    rt.library = lib
    rt.fee_table = _FakeFeeTable()
    engine.tick(now=1000)
    # score: ETH 3.0×10 > BTC 2.0×10 → 选 ETH；往返成本口径
    # (5+5)×2 + 300（默认最大开仓价差）= 320 已传给 gate
    assert rt.current_symbol == "ETH"
    assert any(c[2] == "ETH" and c[4] == pytest.approx(320.0)
               for c in lib.gate_calls)
    inner = rt._inner
    assert inner is not None and inner.entry_gate is not None
    # 内层开仓时 entry_gate 复检通过 → ETH 已开仓
    assert _legs(engine, "ETH") == (pytest.approx(0.1),
                                    pytest.approx(-0.1))
    rt.stop()


def test_auto_funding_library_gate_blocks_all():
    venues = _venues()
    scanner = FakeScanner([_row("BTC", edge=4.0)])
    engine = _engine(venues, {
        "name": "carry-auto", "strategy": "funding_carry",
        "run_mode": "semi_auto",
        "params": dict(CARRY_PARAMS)}, scanner=scanner)
    rt = engine.tasks[0].runtime
    rt.library = _FakeLibrary(gate_ok=False)
    engine.tick(now=1000)
    engine.tick(now=2000)
    assert rt.auto_phase == "scanning"
    assert rt.current_symbol is None
    assert _legs(engine) == (0.0, 0.0)
