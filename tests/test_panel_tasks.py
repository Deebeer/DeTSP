"""面板可交互操作测试：参数规格一致性、任务增删改、持久化合并、
手动平仓。fixture 与 test_web 同口径：真实 HTTP + 命令泵线程。"""

import dataclasses
import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from detsp.core.config import build_config
from detsp.core.engine import Engine
from detsp.core.ledger import Ledger
from detsp.core.panel_tasks import load_panel_tasks, save_panel_tasks
from detsp.core.venue import OrderRequest, Side
from detsp.strategies.specs import (
    PARAMS_CLASSES, strategy_specs, validate_params)
from detsp.venues.paper import PaperVenue
from detsp.web.server import PanelState, serve

ROT_PARAMS = {"symbol": "BTC", "venue_a": "arcus", "venue_b": "lighter",
              "notional_usd": 100.0, "leverage_a": 1, "leverage_b": 1}

CFG_YAML = {"tasks": [{
    "name": "cfg-task", "strategy": "hedged_grid",
    "params": {"grid_venue": "arcus", "hedge_venue": "lighter",
               "symbol": "BTC", "lower_price": 55_000,
               "upper_price": 65_000, "grid_count": 10,
               "qty_per_grid": 0.01}}]}


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


def _make_state(tmp_path, yaml_data=None):
    cfg = build_config(env={}, yaml_data=yaml_data or {})
    venues = {"arcus": PaperVenue(venue_id="arcus", prices={"BTC": 60_000.0}),
              "lighter": PaperVenue(venue_id="lighter",
                                     prices={"BTC": 60_000.0})}
    ledger = Ledger()
    engine = Engine.from_config(cfg, venues, ledger=ledger)
    task_cfgs = {str(t.get("name")): t for t in cfg.tasks}
    store = tmp_path / "panel_tasks.yaml"
    state = PanelState(engine=engine, venues=venues, ledger=ledger,
                       app_config=cfg, task_cfgs=task_cfgs,
                       panel_store_path=store)
    return state, venues, store


@pytest.fixture()
def panel(tmp_path):
    state, venues, store = _make_state(tmp_path)
    server = serve(state, "127.0.0.1", 0)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    stop_flag = threading.Event()

    def _pump():
        while not stop_flag.is_set():
            state.engine.process_commands()
            time.sleep(0.01)

    pump = threading.Thread(target=_pump, daemon=True)
    pump.start()
    yield base, state, venues, store
    stop_flag.set()
    server.shutdown()


# ---------- 规格一致性（防漂移） ----------

def test_specs_match_params_classes():
    specs = {s["strategy"]: s for s in strategy_specs()}
    assert set(specs) == set(PARAMS_CLASSES)
    for stype, cls in PARAMS_CLASSES.items():
        fields = {f.name: f for f in dataclasses.fields(cls)}
        spec_fields = {f["name"]: f for f in specs[stype]["fields"]}
        assert set(spec_fields) == set(fields), stype
        for name, f in fields.items():
            sf = spec_fields[name]
            has_default = f.default is not dataclasses.MISSING or \
                f.default_factory is not dataclasses.MISSING
            assert sf["required"] == (not has_default), (stype, name)
            if has_default and f.default is not dataclasses.MISSING:
                assert sf["default"] == f.default, (stype, name)


def test_validate_params_rules():
    out = validate_params("rotation_hedge",
                          {**ROT_PARAMS, "hold_seconds": 60.0})
    assert out["hold_seconds"] == 60  # float 整数值归一为 int 字段外
    # 的 float 字段保持 float；grid_count 的 int 归一单独验
    out = validate_params("hedged_grid", {
        "grid_venue": "arcus", "hedge_venue": "lighter", "symbol": "BTC",
        "lower_price": 1, "upper_price": 2, "grid_count": 10.0,
        "qty_per_grid": 0.01})
    assert out["grid_count"] == 10 and isinstance(out["grid_count"], int)
    with pytest.raises(ValueError, match="未知参数"):
        validate_params("rotation_hedge", {**ROT_PARAMS, "nope": 1})
    with pytest.raises(ValueError, match="必填"):
        validate_params("rotation_hedge", {"symbol": "BTC"})
    with pytest.raises(ValueError, match="必须是"):
        validate_params("hedged_grid", {
            "grid_venue": "arcus", "hedge_venue": "lighter", "symbol": "BTC",
            "lower_price": 1, "upper_price": 2, "grid_count": 10,
            "qty_per_grid": 0.01, "mode": "sideways"})
    with pytest.raises(ValueError, match="合法场所"):
        validate_params("funding_carry", {
            "venue_a": "nope", "venue_b": "lighter", "symbol": "BTC",
            "notional_usd": 100})


# ---------- 规格接口 ----------

def test_strategy_specs_endpoint(panel):
    base, _, _, _ = panel
    status, body = _req(base, "/api/strategy-specs")
    assert status == 200
    assert {s["strategy"] for s in body["strategies"]} == set(PARAMS_CLASSES)
    assert "arcus" in body["venues"]


# ---------- 新建 + 落盘 + 冲突 ----------

def _create(base, token, name="rot-1", params=None):
    return _req(base, "/api/tasks/create", "POST", token=token,
                body={"name": name, "strategy": "rotation_hedge",
                      "params": params or ROT_PARAMS})


def test_create_task_and_persist(panel):
    base, state, _, store = panel
    status, res = _create(base, state.api_token)
    assert status == 200 and res["ok"] is True
    _, body = _req(base, "/api/tasks")
    t = body["tasks"][0]
    assert t["name"] == "rot-1" and t["status"] == "running"
    assert t["source"] == "panel"
    saved = load_panel_tasks(store)
    assert len(saved) == 1 and saved[0]["name"] == "rot-1"
    assert saved[0]["params"]["notional_usd"] == 100.0
    _, ov = _req(base, "/api/overview")
    assert any("创建任务" in e for e in ov["events"])


def test_create_conflicts_and_bad_input(panel):
    base, state, _, _ = panel
    assert _create(base, state.api_token)[1]["ok"] is True
    # 重名
    status, res = _create(base, state.api_token)
    assert status == 400 and "已存在" in res["error"]
    # (venue,symbol) 占用冲突：中性网格抢 arcus 的 BTC
    status, res = _req(base, "/api/tasks/create", "POST",
                       token=state.api_token,
                       body={"name": "hg-1", "strategy": "hedged_grid",
                             "params": {"grid_venue": "arcus",
                                        "hedge_venue": "lighter",
                                        "symbol": "BTC", "lower_price": 55_000,
                                        "upper_price": 65_000, "grid_count": 5,
                                        "qty_per_grid": 0.01}})
    assert status == 400 and "占用" in res["error"]
    # 参数非法：缺必填 / 未知策略，都回明确错误而非 500
    status, res = _create(base, state.api_token, name="bad-1",
                          params={"symbol": "BTC"})
    assert status == 400 and ("必填" in res["error"]
                              or "缺少" in res["error"])
    status, res = _req(base, "/api/tasks/create", "POST",
                       token=state.api_token,
                       body={"name": "x", "strategy": "grid", "params": {}})
    assert status == 400 and "未知策略" in res["error"]


# ---------- 重启合并加载 ----------

def test_attach_reload_panel_tasks(tmp_path):
    # 先把面板任务文件写好（新建+落盘由 HTTP 测试覆盖），再模拟
    # 重启：新引擎 + 新 PanelState 从同一文件 attach 合并加载
    state1, _, store = _make_state(tmp_path)
    save_panel_tasks(store, [{"name": "rot-1",
                              "strategy": "rotation_hedge",
                              "params": dict(ROT_PARAMS)}])
    state2, _, _ = _make_state(tmp_path)
    errs = state2.attach_panel_tasks(load_panel_tasks(store))
    assert errs == {}
    names = [t.name for t in state2.engine.tasks]
    assert names == ["rot-1"]
    view = {t["name"]: t for t in state2.tasks_view()}
    assert view["rot-1"]["source"] == "panel"
    assert view["rot-1"]["status"] == "running"


def test_attach_name_collision_with_config(tmp_path):
    state, venues, store = _make_state(tmp_path, CFG_YAML)
    assert [t.name for t in state.engine.tasks] == ["cfg-task"]
    save_panel_tasks(store, [{"name": "cfg-task",
                              "strategy": "rotation_hedge",
                              "params": dict(ROT_PARAMS)}])
    errs = state.attach_panel_tasks(load_panel_tasks(store))
    assert "cfg-task" in errs and "重名" in errs["cfg-task"]
    # config 任务不受影响，仍在跑；面板侧有一条同名 error 记录
    assert [t.name for t in state.engine.tasks] == ["cfg-task"]
    view = [t for t in state.tasks_view() if t["name"] == "cfg-task"]
    assert {t["source"] for t in view} == {"config", "panel"}
    assert any(t["status"] == "running" for t in view)
    assert any(t["status"] == "error" for t in view)


def test_attach_occupancy_conflict_skipped(tmp_path):
    state, venues, store = _make_state(tmp_path, CFG_YAML)
    # 面板任务与 config 的中性网格抢 (arcus, BTC)：合并时跳过并报错
    save_panel_tasks(store, [{"name": "panel-rot",
                              "strategy": "rotation_hedge",
                              "params": dict(ROT_PARAMS)}])
    errs = state.attach_panel_tasks(load_panel_tasks(store))
    assert "panel-rot" in errs and "占用" in errs["panel-rot"]
    assert [t.name for t in state.engine.tasks] == ["cfg-task"]


# ---------- 编辑 ----------

def test_update_task(panel):
    base, state, _, store = panel
    assert _create(base, state.api_token)[1]["ok"] is True
    status, res = _req(base, "/api/tasks/update", "POST",
                       token=state.api_token,
                       body={"name": "rot-1",
                             "params": {**ROT_PARAMS, "notional_usd": 250.0,
                                        "hold_seconds": 30}})
    assert status == 200 and res["ok"] is True
    _, body = _req(base, "/api/tasks")
    t = body["tasks"][0]
    assert t["params"]["params"]["notional_usd"] == 250.0
    assert t["status"] == "running"       # 原在运行，重组后仍运行
    saved = load_panel_tasks(store)
    assert saved[0]["params"]["notional_usd"] == 250.0
    # 改坏参数：重组失败、不落盘、旧参数还在
    status, res = _req(base, "/api/tasks/update", "POST",
                       token=state.api_token,
                       body={"name": "rot-1",
                             "params": {**ROT_PARAMS, "notional_usd": -5}})
    assert status == 400 and "重组失败" in res["error"]
    assert load_panel_tasks(store)[0]["params"]["notional_usd"] == 250.0


def test_update_config_task_rejected(panel):
    base, state, _, _ = panel
    status, res = _req(base, "/api/tasks/update", "POST",
                       token=state.api_token,
                       body={"name": "nope", "params": {}})
    assert status == 400 and "不存在" in res["error"]


# ---------- 删除与平仓 ----------

def test_delete_flow_and_manual_close(panel):
    base, state, venues, store = panel
    assert _create(base, state.api_token)[1]["ok"] is True
    # 运行中不许删
    status, res = _req(base, "/api/tasks/delete", "POST",
                       token=state.api_token, body={"name": "rot-1"})
    assert status == 400 and "仍在运行" in res["error"]
    # 停掉后，在场所直接留一笔持仓（归属仍登记在已停的 rot-1
    # 名下）→ 有持仓仍不许删
    _req(base, "/api/tasks/rot-1/stop", "POST", token=state.api_token)
    venues["arcus"].place_order(OrderRequest(
        symbol="BTC", side=Side.BUY, qty=0.01, price=None))
    status, res = _req(base, "/api/tasks/delete", "POST",
                       token=state.api_token, body={"name": "rot-1"})
    assert status == 400 and "仍有持仓" in res["error"]
    # 面板平仓：按归属走 rot-1 名下平仓
    status, res = _req(base, "/api/positions/close", "POST",
                       token=state.api_token,
                       body={"venue_id": "arcus", "symbol": "BTC"})
    assert status == 200 and res["ok"] is True
    assert res["owner"] == "rot-1"
    assert state.engine.executor.position("arcus", "BTC") is None or \
        state.engine.executor.position("arcus", "BTC").qty == 0
    # 平完可删，文件同步清空
    status, res = _req(base, "/api/tasks/delete", "POST",
                       token=state.api_token, body={"name": "rot-1"})
    assert status == 200 and res["ok"] is True
    assert load_panel_tasks(store) == []
    _, body = _req(base, "/api/tasks")
    assert body["tasks"] == []


def test_manual_close_unowned(panel):
    base, state, venues, _ = panel
    # 没有任何任务时直接在场所留仓 → 无归属，面板手动平全部
    venues["lighter"].place_order(OrderRequest(
        symbol="BTC", side=Side.SELL, qty=0.02, price=None))
    _, ov = _req(base, "/api/overview")
    lighter = next(v for v in ov["venues"] if v["venue_id"] == "lighter")
    assert lighter["positions"][0]["owner"] is None
    status, res = _req(base, "/api/positions/close", "POST",
                       token=state.api_token,
                       body={"venue_id": "lighter", "symbol": "BTC"})
    assert status == 200 and res["ok"] is True and res["owner"] is None
    assert "全部持仓" in res["note"] and "手动" in res["note"]
    pos = state.engine.executor.position("lighter", "BTC")
    assert pos is None or pos.qty == 0


def test_close_owned_position(panel):
    base, state, venues, _ = panel
    assert _create(base, state.api_token)[1]["ok"] is True
    ex = state.engine.executor
    # 以任务名义真实开一笔（走 Executor，归属登记在 rot-1 名下）
    res = ex.place("rot-1", "arcus", OrderRequest(
        symbol="BTC", side=Side.BUY, qty=0.01, price=None), intent="open")
    assert res.status in ("filled", "partial")
    _, ov = _req(base, "/api/overview")
    arcus = next(v for v in ov["venues"] if v["venue_id"] == "arcus")
    assert arcus["positions"][0]["owner"] == "rot-1"
    status, res = _req(base, "/api/positions/close", "POST",
                       token=state.api_token,
                       body={"venue_id": "arcus", "symbol": "BTC"})
    assert status == 200 and res["ok"] is True
    assert res["owner"] == "rot-1" and "rot-1" in res["note"]
    pos = ex.position("arcus", "BTC")
    assert pos is None or pos.qty == 0
    # 无持仓再平 → 明确报错
    status, res = _req(base, "/api/positions/close", "POST",
                       token=state.api_token,
                       body={"venue_id": "arcus", "symbol": "BTC"})
    assert status == 400 and "无持仓" in res["error"]
