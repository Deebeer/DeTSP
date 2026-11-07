"""第十轮：配置五段 + 四模式（面板与 server）。

覆盖：策略页产物含四模式文案与大白话说明、五段标题与段序、
总结句容器（secSummary）与模板替换、全自动只读参数区、
manual 隐藏开仓条件与平仓后两段、平仓条件显示，与网格
out_of_range_action 及资金费 min_edge_bps_per_hour 豁免标记、
specs 无 section 时的兜底分组渲染分支；server 创建 full_auto
任务后 /api/tasks 带 run_mode=full_auto 且落盘、amount_usd
（保证金）透传（网格可不带 qty_per_grid）、创建传 auto 落盘
归一为 semi_auto；第十二轮预设全砍：run_preset 不再有按钮与
提交字段、老请求带了容忍并忽略、落盘丢弃，非法 run_mode
仍被拒、编辑不带模式时保持原值；specs 的
section/section_summaries 与字段自洽。
"""

import json
import re
import threading
import time
import urllib.error
import urllib.request

import pytest

from detsp.core.config import build_config
from detsp.core.engine import Engine
from detsp.core.ledger import Ledger
from detsp.core.panel_tasks import load_panel_tasks
from detsp.strategies.specs import strategy_specs
from detsp.venues.paper import PaperVenue
from detsp.web.page import PAGE_HTML
from detsp.web.server import (PanelState, _normalize_run_mode,
                              _validate_params_for_mode, serve)

CARRY_PARAMS = {"symbol": "BTC", "venue_a": "arcus", "venue_b": "lighter",
                "notional_usd": 100.0, "leverage": 1}

GRID_PARAMS_NO_QTY = {"grid_venue": "arcus", "hedge_venue": "lighter",
                      "symbol": "BTC", "lower_price": 55_000,
                      "upper_price": 65_000, "grid_count": 10}


# ── 页面静态产物：四模式 ────────────────────────────────────

def test_page_has_four_run_modes_and_plain_descriptions():
    for mode in ("manual", "semi_fixed", "semi_auto", "full_auto"):
        assert f'data-mode="{mode}"' in PAGE_HTML
    for label in ("自主", "半自动·固定标的", "半自动·自动选标", "全自动"):
        assert label in PAGE_HTML
    # 每模式一行大白话（MODE_DESCS 原文关键句）
    assert "点「开仓」立即开仓、不等开仓条件；平仓条件照常生效" in PAGE_HTML
    assert "平仓条件到达自动平仓，也可手动平仓" in PAGE_HTML
    assert "平仓你手动点" not in PAGE_HTML
    assert "在这个标的上按条件开仓→平仓→再开仓" in PAGE_HTML
    assert "每轮系统重新选标" in PAGE_HTML
    assert "只选策略和保证金，两腿/标的/参数系统全定" in PAGE_HTML
    # 网格 manual 例外说明
    assert "网格在区间内自动铺格交易，开/停由你手动" in PAGE_HTML
    # 旧两段切换不得残留
    assert 'data-mode="auto"' not in PAGE_HTML
    # 运行按钮文案随模式切换：manual=开仓，其余=运行
    assert '"▶ 开仓"' in PAGE_HTML and '"▶ 运行"' in PAGE_HTML
    # 旧值 auto 在前端也归一 semi_auto
    assert "normalizeModeClient" in PAGE_HTML
    assert 'if(mode==="auto") return "semi_auto"' in PAGE_HTML


def test_page_mode_visibility_rules():
    # manual 隐藏开仓条件与平仓后两段、平仓条件显示（JS 按
    # data-section 处理，不刷新页面）
    assert 'hideSecs=["开仓条件","平仓后怎么办"]' in PAGE_HTML
    assert "applySectionVisibility" in PAGE_HTML
    # 网格例外最终做法：out_of_range_action 打 data-manual-keep
    # 标记，隐藏其所在段时只豁免显示它一个字段
    assert "data-manual-keep" in PAGE_HTML
    assert "out_of_range_action" in PAGE_HTML
    # 资金费 min_edge_bps_per_hour 是进出共用一条线、manual 下作
    # 退出线：同样打 data-manual-keep 标记（configFieldHtml 与
    # zoneFieldHtml 两处渲染入口都带关联写法）
    assert 'f.name==="min_edge_bps_per_hour"' in PAGE_HTML
    assert PAGE_HTML.count('f.name==="min_edge_bps_per_hour"') >= 2
    assert 'f.name==="out_of_range_action"||f.name==="min_edge_bps_per_hour"' in PAGE_HTML
    # semi_auto：币种下拉禁用 + 自动选标注明
    assert 'S.runMode==="semi_auto"' in PAGE_HTML
    assert "半自动·自动选标：标的由系统自动选" in PAGE_HTML
    # full_auto：隐藏订单腿/选市场、占位说明、只留保证金+
    # 总结句+只读参数（第十二轮：风险档随预设全砍删除）
    assert "两腿和标的都由系统选" in PAGE_HTML
    assert "fullAutoCfg" in PAGE_HTML and "fullAutoNote" in PAGE_HTML
    assert "看系统用的参数" in PAGE_HTML
    assert "要改参数请切到半自动" in PAGE_HTML
    assert "data-run-preset" not in PAGE_HTML
    assert "风险档" not in PAGE_HTML
    # 提交组装：保证金走顶层 amount_usd（全模式统一），不再带
    # run_preset
    assert "body.run_preset" not in PAGE_HTML
    assert "body.amount_usd" in PAGE_HTML


# ── 页面静态产物：五段与总结句 ──────────────────────────────

def test_page_five_sections_and_summaries():
    assert 'SECTION_ORDER=["交易对象","开仓条件","平仓条件",' \
           '"平仓后怎么办","保护"]' in PAGE_HTML
    # 段渲染按 field.section 分组，总结句容器 class=secSummary
    assert "sectionsHtml" in PAGE_HTML and "f.section" in PAGE_HTML
    assert "secSummary" in PAGE_HTML
    assert "data-sec-summary" in PAGE_HTML
    # 模板来自 page.section_summaries，输入变化即更新
    assert "section_summaries" in PAGE_HTML
    assert "fillSummaryTemplate" in PAGE_HTML
    assert "updateSectionSummaries" in PAGE_HTML
    # 保护段默认折叠（details），其余段常显（div）
    assert '<details class="cfgSec"' in PAGE_HTML
    # 布尔显示开/关的通用格式化在模板替换函数里
    assert "formatSummaryValue" in PAGE_HTML
    # 字段仍三段式
    assert 'class="fname"' in PAGE_HTML and 'class="fdesc"' in PAGE_HTML


def test_page_section_fallback_when_specs_lack_section():
    # specs 任一字段都没带 section → sectionsHtml 返回 null →
    # 回退 legacyConfigHtml 旧分组渲染，中间状态不白屏
    assert "if(!configFields.some(f=>f.section)) return null" in PAGE_HTML
    assert "legacyConfigHtml" in PAGE_HTML
    assert "advFieldsHtml(advanced)" in PAGE_HTML


def test_mode_badge_labels():
    # 任务徽标四值文案（第十二轮：不再带档位后缀）
    assert "半自动·固定标的" in PAGE_HTML
    assert "MODE_BADGE_LABELS" in PAGE_HTML
    assert "RUN_PRESET_LABELS" not in PAGE_HTML
    # 全自动任务列表显示当前两腿
    assert "current_venues" in PAGE_HTML


# ── specs 段数据自洽（后端已落地时的强断言） ────────────────

def test_specs_sections_and_summaries_consistent():
    order = ["交易对象", "开仓条件", "平仓条件", "平仓后怎么办", "保护"]
    for spec in strategy_specs():
        fields = {f["name"]: f for f in spec["fields"]}
        vf = set(spec["page"]["venue_fields"])
        amt = spec["page"]["amount"]
        for f in spec["fields"]:
            if f["name"] in vf or f["name"] == "symbol" \
                    or f["name"] == amt:
                continue
            assert f.get("section") in order, \
                f"{spec['strategy']}.{f['name']} 缺合法 section"
        sums = spec["page"].get("section_summaries") or {}
        assert set(sums) <= {"开仓条件", "平仓条件", "平仓后怎么办",
                             "保护"}
        for sec, tpl in sums.items():
            for name in re.findall(r"\{([a-zA-Z0-9_]+)\}", tpl):
                assert name in fields, \
                    f"{spec['strategy']} 总结模板占位符 {name} 不是字段"


# ── server 归一与校验（不经 HTTP 的直测） ───────────────────

def test_server_normalize_and_mode_validate():
    assert _normalize_run_mode("auto") == "semi_auto"
    assert _normalize_run_mode("full_auto") == "full_auto"
    assert _normalize_run_mode(None) == "manual"
    with pytest.raises(ValueError):
        _normalize_run_mode("turbo")
    # full_auto 网格可不带 qty_per_grid：占位校验后摘除，不进存盘
    clean = _validate_params_for_mode(
        "hedged_grid", dict(GRID_PARAMS_NO_QTY), "full_auto")
    assert "qty_per_grid" not in clean
    assert clean["grid_count"] == 10
    # manual 网格仍必填 qty_per_grid（口径不放松）
    with pytest.raises(ValueError):
        _validate_params_for_mode(
            "hedged_grid", dict(GRID_PARAMS_NO_QTY), "manual")


# ── server HTTP：创建/落盘/输出 ─────────────────────────────

def _row(symbol, edge=4.0, price=60_000.0):
    return {"symbol": symbol, "price_a": price, "price_b": price,
            "edge_bps_h": edge, "spread_bps": 10.0,
            "volatility_pct": 2.0, "has_price": True,
            "has_funding": True, "suspect": False, "mixed": False}


class FakeScanner:
    def pairs(self, venue_a, venue_b, strategy):
        return {"venue_a": venue_a, "venue_b": venue_b,
                "strategy": strategy, "rows": [_row("BTC")],
                "common": 1, "errors": {}}

    def candles(self, venue_id, symbol):
        return {"ok": False, "error": "假扫描器无 K 线"}


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
    scanner = FakeScanner()
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

    threading.Thread(target=_pump, daemon=True).start()
    yield base, state, store
    stop_flag.set()
    server.shutdown()


def test_create_full_auto_persists_and_tasks_view(panel):
    base, state, store = panel
    status, res = _req(base, "/api/tasks/create", "POST",
                       token=state.api_token,
                       body={"name": "fa-1", "strategy": "funding_carry",
                             "params": CARRY_PARAMS,
                             "run_mode": "full_auto",
                             "run_preset": "aggressive"})
    assert status == 200 and res["ok"] is True, res
    saved = load_panel_tasks(store)
    assert saved[0]["run_mode"] == "full_auto"
    # 第十二轮：老请求带的 run_preset 容忍并忽略、落盘丢弃
    assert "run_preset" not in saved[0]
    _, body = _req(base, "/api/tasks")
    t = next(x for x in body["tasks"] if x["name"] == "fa-1")
    assert t["run_mode"] == "full_auto"
    assert "run_preset" not in t
    assert isinstance(t["current_venues"], list)


def test_create_full_auto_grid_amount_usd_without_qty(panel):
    base, state, store = panel
    status, res = _req(base, "/api/tasks/create", "POST",
                       token=state.api_token,
                       body={"name": "grid-fa", "strategy": "hedged_grid",
                             "params": GRID_PARAMS_NO_QTY,
                             "run_mode": "full_auto",
                             "run_preset": "conservative",
                             "amount_usd": 1000})
    assert status == 200 and res["ok"] is True, res
    saved = load_panel_tasks(store)
    assert saved[0]["run_mode"] == "full_auto"
    assert "run_preset" not in saved[0]
    assert saved[0]["amount_usd"] == 1000.0
    assert "qty_per_grid" not in saved[0]["params"]


def test_create_auto_normalizes_to_semi_auto(panel):
    base, state, store = panel
    status, res = _req(base, "/api/tasks/create", "POST",
                       token=state.api_token,
                       body={"name": "sa-1", "strategy": "funding_carry",
                             "params": CARRY_PARAMS, "run_mode": "auto"})
    assert status == 200 and res["ok"] is True, res
    saved = load_panel_tasks(store)
    assert saved[0]["run_mode"] == "semi_auto"   # 新写入一律新值
    _, body = _req(base, "/api/tasks")
    t = next(x for x in body["tasks"] if x["name"] == "sa-1")
    assert t["run_mode"] == "semi_auto"


def test_create_legacy_run_preset_tolerated_mode_rejected(panel):
    base, state, _ = panel
    status, res = _req(base, "/api/tasks/create", "POST",
                       token=state.api_token,
                       body={"name": "ok-1", "strategy": "funding_carry",
                             "params": CARRY_PARAMS,
                             "run_mode": "full_auto",
                             "run_preset": "turbo"})
    # 第十二轮：run_preset 任意值都容忍并忽略，不再报错
    assert status == 200 and res["ok"] is True, res
    status, res = _req(base, "/api/tasks/create", "POST",
                       token=state.api_token,
                       body={"name": "bad-2", "strategy": "funding_carry",
                             "params": CARRY_PARAMS, "run_mode": "turbo"})
    assert status == 400 and "非法" in res["error"]


def test_update_without_mode_fields_preserves_them(panel):
    base, state, store = panel
    status, res = _req(base, "/api/tasks/create", "POST",
                       token=state.api_token,
                       body={"name": "sf-1", "strategy": "funding_carry",
                             "params": CARRY_PARAMS,
                             "run_mode": "semi_fixed",
                             "run_preset": "conservative"})
    assert status == 200 and res["ok"] is True, res
    # 编辑弹窗口径：只带 params、不带 run_mode → 保持原模式；
    # run_preset 创建时已丢弃、编辑后仍不出现
    status, res = _req(base, "/api/tasks/update", "POST",
                       token=state.api_token,
                       body={"name": "sf-1",
                             "params": {**CARRY_PARAMS,
                                        "notional_usd": 250.0}})
    assert status == 200 and res["ok"] is True, res
    saved = load_panel_tasks(store)
    assert saved[0]["run_mode"] == "semi_fixed"
    assert "run_preset" not in saved[0]
    assert saved[0]["params"]["notional_usd"] == 250.0


def test_create_funding_with_margin_only_derives_notional(panel):
    """第十二轮保证金语义（server 路径）：资金费任务只给
    amount_usd（保证金）+ 杠杆、不给 notional_usd，也能创建；
    落盘 params 里的 notional_usd = 保证金 × 杠杆。"""
    base, state, store = panel
    params = {"symbol": "BTC", "venue_a": "arcus",
              "venue_b": "lighter", "leverage": 6}
    status, res = _req(base, "/api/tasks/create", "POST",
                       token=state.api_token,
                       body={"name": "fm-1", "strategy": "funding_carry",
                             "params": params, "run_mode": "semi_fixed",
                             "amount_usd": 100})
    assert status == 200 and res["ok"] is True, res
    saved = load_panel_tasks(store)
    assert saved[0]["amount_usd"] == 100.0
    assert saved[0]["params"]["notional_usd"] == 600.0
