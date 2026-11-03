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
