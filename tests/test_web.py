"""网页面板测试：本地起服务打真实 HTTP 接口（不联网外呼）。

H2/M4 后：POST 必须带 X-API-Token；启停任务经引擎命令队列执行，
fixture 起一个泵线程模拟引擎主循环（循环 process_commands）。
"""

import json
import threading
import time
import urllib.request

import pytest

from detsp.core.config import build_config
from detsp.core.engine import Engine
from detsp.core.ledger import Fill, Ledger
from detsp.venues.paper import PaperVenue
from detsp.web.server import PanelState, serve, web_settings

GRID_YAML = {"tasks": [{
    "name": "hgrid-btc", "strategy": "hedged_grid",
    "params": {"grid_venue": "arcus", "hedge_venue": "lighter",
               "symbol": "BTC", "lower_price": 55_000,
               "upper_price": 65_000, "grid_count": 10,
               "qty_per_grid": 0.01, "mode": "long_short",
               "out_of_range_action": "close"}}]}


def _req(base, path, method="GET", token=None):
    req = urllib.request.Request(base + path, method=method)
    if token:
        req.add_header("X-API-Token", token)
    with urllib.request.urlopen(req, timeout=5) as r:
        body = r.read().decode("utf-8")
        return r.status, body


def _req_json(base, path, payload, token):
    import urllib.error

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(base + path, method="POST", data=data)
    req.add_header("X-API-Token", token)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


class _FakeLibrary:
    """StatsLibrary 契约的假实现（premium_entry/funding_*）。"""

    def premium_entries(self):
        return {"arcus__lighter__BTC": {
            "midline_bps": 12.0, "upper_bps": 8.0, "lower_bps": 9.0,
            "sample_hours": 5.0, "updated_at": 1_700_000_000.0}}

    def premium_entry(self, base, hedge, symbol):
        return self.premium_entries().get(f"{base}__{hedge}__{symbol}")

    def funding_stats(self, va, vb, symbol, min_line, now):
        return {"median_duration_h": 1.0, "above_ratio": 0.1,
                "avg_net_bps_per_hour": 0.5, "sample_hours": 10}

    def breakeven_hours(self, cost, net):
        return cost / net if net and net > 0 else None

    def funding_gate(self, va, vb, symbol, min_line, cost, net, now):
        return {"ok": False, "reason": "持续性不足：中位持续 1 小时"}


class _FakeLibraryService:
    def __init__(self):
        self.library = _FakeLibrary()
        self.fee_table = None

    def status(self):
        return {"running": True, "last_record_ts": 1_700_000_000.0,
                "last_recompute_ts": 1_700_000_000.0,
                "premium_entries": 1, "last_error": ""}


@pytest.fixture()
def panel():
    cfg = build_config(env={}, yaml_data=GRID_YAML)
    venue = PaperVenue(venue_id="arcus", prices={"BTC": 60_000.0})
    hedge = PaperVenue(venue_id="lighter", prices={"BTC": 60_000.0})
    venues = {"arcus": venue, "lighter": hedge}
    ledger = Ledger()
    engine = Engine.from_config(cfg, venues, ledger=ledger)
    state = PanelState(engine=engine, venues=venues, ledger=ledger,
                       app_config=cfg,
                       task_cfgs={"hgrid-btc": GRID_YAML["tasks"][0]})
    server = serve(state, "127.0.0.1", 0)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    # 命令泵：模拟引擎主循环串行执行面板命令（M4）
    stop_flag = threading.Event()

    def _pump():
        while not stop_flag.is_set():
            engine.process_commands()
            time.sleep(0.01)

    pump = threading.Thread(target=_pump, daemon=True)
    pump.start()
    yield base, state, ledger
    stop_flag.set()
    server.shutdown()


def test_index_page(panel):
    base, state, _ = panel
    status, body = _req(base, "/")
    assert status == 200 and "DeTSP" in body and "不等于登录鉴权" in body
    assert state.api_token and state.api_token in body  # token 已注入页面


def test_overview(panel):
    base, _, _ = panel
    _, body = _req(base, "/api/overview")
    ov = json.loads(body)
    assert ov["killswitch"]["halted"] is False
    assert ov["dry_run"] is True
    v = ov["venues"][0]
    assert v["venue_id"] == "arcus" and v["equity"] == 10_000.0
    assert v["open_orders"] > 0        # 网格已铺单
    assert ov["today"]["fills"] == 0


def test_tasks_start_stop(panel):
    base, state, _ = panel
    _, body = _req(base, "/api/tasks")
    tasks = json.loads(body)["tasks"]
    assert tasks[0]["name"] == "hgrid-btc" and tasks[0]["status"] == "running"
    assert tasks[0]["params"]["params"]["grid_count"] == 10

    _, body = _req(base, "/api/tasks/hgrid-btc/stop", "POST",
                   token=state.api_token)
    res = json.loads(body)
    assert res["ok"] is True
    # 四策略停止语义统一：撤单 + 平仓，note 必须如实说明已平仓
    assert "持仓已平" in res["note"]
    _, body = _req(base, "/api/tasks")
    assert json.loads(body)["tasks"][0]["status"] == "stopped"

    _, body = _req(base, "/api/tasks/hgrid-btc/start", "POST",
                   token=state.api_token)
    assert json.loads(body)["ok"] is True
    _, body = _req(base, "/api/tasks")
    assert json.loads(body)["tasks"][0]["status"] == "running"


def test_killswitch(panel):
    base, state, _ = panel
    _req(base, "/api/killswitch", "POST", token=state.api_token)
    _, body = _req(base, "/api/overview")
    ks = json.loads(body)["killswitch"]
    assert ks["halted"] is True and "手动" in ks["reason"]
    _req(base, "/api/killswitch/reset", "POST", token=state.api_token)
    _, body = _req(base, "/api/overview")
    assert json.loads(body)["killswitch"]["halted"] is False


def test_fills_api(panel):
    base, _, ledger = panel
    ledger.record(Fill(venue="arcus", account="default", symbol="BTC",
                       strategy="hedged_grid", side="buy", qty=0.01,
                       price=60_000.0, fee=0.1, order_id="paper-1"))
    _, body = _req(base, "/api/fills")
    fills = json.loads(body)["fills"]
    assert len(fills) == 1 and fills[0]["order_id"] == "paper-1"
    _, body = _req(base, "/api/overview")
    assert json.loads(body)["today"]["fills"] == 1


def test_web_settings_rejects_public_bind():
    with pytest.raises(ValueError):
        web_settings({"WEB_HOST": "0.0.0.0"})
    assert web_settings({}) == ("127.0.0.1", 8320)
    assert web_settings({"WEB_PORT": "9001"}) == ("127.0.0.1", 9001)


# ── 第十一轮：费率表 / 库接口 / 资金费警告 ──────────────────

def test_libraries_degrades_without_service(panel):
    base, state, _ = panel
    assert state.library_service is None
    _, body = _req(base, "/api/libraries")
    assert json.loads(body) == {"service": None, "premium": {}}
    _, body = _req(base, "/api/libraries/funding?va=arcus&vb=lighter"
                         "&symbol=BTC&line=1")
    d = json.loads(body)
    assert d["service"] is None and d["funding"] is None


def test_fees_endpoint_removed(panel):
    # 第十二轮：场所费率表撤掉——/api/settings/fees 的读与写都
    # 不再存在（404），PanelState 也不再有费率表与编辑方法；
    # 费率走 core.fee_table 内置常量（全所 taker 5 / maker 2）
    base, state, _ = panel
    import urllib.error
    try:
        _req(base, "/api/settings/fees")
        raise AssertionError("GET /api/settings/fees 应返回 404")
    except urllib.error.HTTPError as e:
        assert e.code == 404
        assert json.loads(e.read().decode("utf-8")) == \
            {"error": "not found"}
    status, res = _req_json(base, "/api/settings/fees",
                            {"venue_id": "arcus", "taker_bps": 7.5,
                             "maker_bps": 1.5}, state.api_token)
    assert status == 404 and res == {"error": "not found"}
    assert not hasattr(state, "fee_table")
    assert not hasattr(state, "fees_view")
    assert not hasattr(state, "save_fees_view")
    # 回本成本改读常量：taker 两所合计 10 bps
    assert state._taker_bps("arcus") == 5.0
    assert state._taker_bps("nope") == 0.0


def test_libraries_with_fake_service(panel):
    base, state, _ = panel
    state.library_service = _FakeLibraryService()
    _, body = _req(base, "/api/libraries")
    d = json.loads(body)
    assert d["service"]["running"] is True
    assert d["service"]["premium_entries"] == 1
    entry = d["premium"]["arcus__lighter__BTC"]
    assert entry["midline_bps"] == 12.0 and entry["upper_bps"] == 8.0
    # 带三元组查询参数也能精确取到一条
    _, body = _req(base, "/api/libraries?venue_a=arcus&venue_b=lighter"
                         "&symbol=BTC")
    assert "arcus__lighter__BTC" in json.loads(body)["premium"]
    # 费差库信息：回本 = ((taker 和)×2 + 最大开仓价差) / 当前净费差
    # （taker 取内置常量 5 bps/所）
    _, body = _req(base, "/api/libraries/funding?va=arcus&vb=lighter"
                         "&symbol=BTC&line=1&max_open_spread=300"
                         "&current_net=2")
    d = json.loads(body)
    f = d["funding"]
    assert f["median_duration_h"] == 1.0 and f["sample_hours"] == 10
    # 内置费率常量：往返成本 = (5+5)×2 + 最大开仓价差 300
    assert f["roundtrip_cost_bps"] == 320.0
    assert f["breakeven_hours"] == 160.0


def test_funding_start_warnings_force_start(panel):
    base, state, _ = panel
    state.library_service = _FakeLibraryService()
    # 用 ETH 避开 fixture 里网格任务对 arcus/lighter BTC 的占用
    for vid, rate in (("arcus", 0.0001), ("lighter", 0.0003)):
        state.venues[vid].set_price("ETH", 3_000.0)
        state.venues[vid].set_funding("ETH", rate)
    cfg = {"name": "carry-1", "strategy": "funding_carry",
           "run_mode": "semi_fixed",
           "params": {"venue_a": "arcus", "venue_b": "lighter",
                      "symbol": "ETH", "notional_usd": 100.0,
                      "leverage": 6.0, "min_edge_bps_per_hour": 1.0,
                      "max_open_spread_bps": 300}}
    # 直查：不达标给警告且含「样本不足」；自动选标不走此路径
    warns = state._funding_gate_warnings(cfg)
    assert any("样本不足" in w for w in warns), warns
    assert any("持续性不足" in w for w in warns), warns
    assert state._funding_gate_warnings(
        {**cfg, "run_mode": "semi_auto"}) == []
    assert state._funding_gate_warnings(
        {**cfg, "strategy": "hedged_grid"}) == []
    # 启动路径：任务照常启动成功，响应带 warnings（强行口径）
    state.task_cfgs["carry-1"] = cfg
    res = state.start_task("carry-1")
    assert res["ok"] is True, res
    assert any("样本不足" in w for w in res.get("warnings", [])), res


def test_panel_state_state_dir_wires_library_service(tmp_path):
    pytest.importorskip("detsp.core.fee_table")
    pytest.importorskip("detsp.core.stats_library")
    cfg = build_config(env={}, yaml_data={"tasks": []})
    venues = {"arcus": PaperVenue(venue_id="arcus",
                                  prices={"BTC": 60_000.0})}
    ledger = Ledger()
    engine = Engine.from_config(cfg, venues, ledger=ledger)
    fake_scanner = object()
    state = PanelState(engine=engine, venues=venues, ledger=ledger,
                       app_config=cfg, task_cfgs={},
                       scanner=fake_scanner, state_dir=tmp_path)
    # scanner 别名与 market_scanner 同步；数据服务按 state_dir 起
    assert state.market_scanner is fake_scanner
    # 第十二轮：PanelState 不再持有可编辑费率表；库服务内部的
    # 费率一律是内置常量，且不再落盘 fees.json
    assert not hasattr(state, "fee_table")
    assert state.library_service is not None
    assert state.library_service.fee_table.get("arcus") == (5.0, 2.0)
    assert state.library_service.fee_table.get("aster") == (5.0, 2.0)
    assert not (tmp_path / "fees.json").exists()


def test_main_wires_scanner_and_state_dir():
    import pathlib

    src = pathlib.Path(__file__).resolve().parent.parent.joinpath(
        "src/detsp/__main__.py").read_text(encoding="utf-8")
    assert "scanner=scanner" in src and "state_dir=state_dir" in src
