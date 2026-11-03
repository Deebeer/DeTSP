"""左侧边栏 + 每策略一页改版测试。

覆盖：边栏入口与四个策略页容器存在且顺序正确；「资金费套利」
改名一致性（specs 标题与页面文案无旧名残留，键名 funding_carry
不变）；specs 的 page 元数据自洽（primary / venue_fields 都是
真实字段、互不重叠、字段 primary 标记与 page.primary 列表一致）；
/api/strategy-specs 把 page 元数据带给前端。
"""

import json
import threading
import time
import urllib.request

import pytest

from detsp.core.config import build_config
from detsp.core.engine import Engine
from detsp.core.ledger import Ledger
from detsp.strategies.specs import (PAGE_META, PARAMS_CLASSES,
                                    STRATEGY_TITLES, strategy_specs)
from detsp.venues.paper import PaperVenue
from detsp.web.page import PAGE_HTML
from detsp.web.server import PanelState, serve

SIDEBAR_PAGES = ["overview", "strat-hedged_grid", "strat-rotation_hedge",
                 "strat-premium_arb", "strat-funding_carry",
                 "market", "settings", "fills"]


# ── 边栏与策略页容器 ────────────────────────────────────────

def test_sidebar_entries_and_order():
    start = PAGE_HTML.index('id="sidebar"')
    end = PAGE_HTML.index("</nav>")
    sidebar = PAGE_HTML[start:end]
    pos = [sidebar.index(f'data-page="{p}"') for p in SIDEBAR_PAGES]
    assert pos == sorted(pos), "边栏顺序必须是 总览/四策略/数据/设置/账本"
    for label in ("总览", "中性网格", "轮换对冲刷量", "溢价套利",
                  "资金费套利", "数据", "设置", "账本"):
        assert label in sidebar


def test_strategy_page_containers_exist():
    for p in SIDEBAR_PAGES:
        assert f'id="page-{p}"' in PAGE_HTML
    # 策略页容器是空壳，由前端通用渲染器按 specs 填充
    for st in PARAMS_CLASSES:
        assert f'<section class="page" id="page-strat-{st}"></section>' \
            in PAGE_HTML


def test_page_has_six_block_titles_for_renderer():
    # 六区块标题由渲染器 JS 生成，断言 JS 模板里都在
    for title in ("交易平台", "策略机会", "保证金", "开仓", "高级配置",
                  "本策略任务"):
        assert title in PAGE_HTML
    assert "runBtn" in PAGE_HTML and "运行" in PAGE_HTML


# ── 改名一致性 ──────────────────────────────────────────────

def test_funding_carry_renamed_everywhere_visible():
    assert STRATEGY_TITLES["funding_carry"] == "资金费套利"
    specs_json = json.dumps(strategy_specs(), ensure_ascii=False)
    assert "资金费 carry" not in specs_json
    assert "资金费套利" in specs_json
    assert "资金费 carry" not in PAGE_HTML
    assert "资金费套利" in PAGE_HTML
    # 键名不变（config/任务引用都靠它）
    assert "funding_carry" in PARAMS_CLASSES
    assert 'data-page="strat-funding_carry"' in PAGE_HTML


# ── specs page 元数据自洽 ───────────────────────────────────

def test_page_meta_consistency():
    for spec in strategy_specs():
        st = spec["strategy"]
        fields = {f["name"]: f for f in spec["fields"]}
        page = spec["page"]
        assert page == {**PAGE_META[st],
                        "venue_fields": list(PAGE_META[st]["venue_fields"]),
                        "venue_labels": list(PAGE_META[st]["venue_labels"]),
                        "primary": list(PAGE_META[st]["primary"])}
        vf = page["venue_fields"]
        assert len(vf) == 2 and len(page["venue_labels"]) == 2
        for name in vf:
            assert name in fields and fields[name]["type"] == "venue"
        primary = page["primary"]
        assert "symbol" in primary
        # primary 与两腿字段都是真实字段且互不重叠
        assert set(primary) <= set(fields)
        assert not set(primary) & set(vf)
        # 字段上的 primary 标记与 page.primary 列表严格一致
        flagged = {f["name"] for f in spec["fields"] if f["primary"]}
        assert flagged == set(primary)
        assert page["opportunity"] in ("funding", "spread", "grid")
        # primary + 高级（其余非两腿字段）恰好覆盖全部非两腿字段
        rest = {n for n, f in fields.items()
                if n not in vf and n not in primary}
        assert set(primary) | rest | set(vf) == set(fields)


def test_primary_fields_per_strategy():
    by = {s["strategy"]: set(s["page"]["primary"])
          for s in strategy_specs()}
    assert "hold_seconds" in by["rotation_hedge"]
    assert "notional_usd" in by["rotation_hedge"]
    assert "notional_usd" in by["funding_carry"]
    assert "min_entry_edge_bps_per_hour" in by["funding_carry"]
    assert "exit_edge_bps_per_hour" in by["funding_carry"]
    assert {"execution_mode", "midline_bps", "upper_bps", "lower_bps",
            "max_position_usd"} <= by["premium_arb"]
    assert {"lower_price", "upper_price", "grid_count", "qty_per_grid"} \
        <= by["hedged_grid"]


# ── 接口把 page 元数据带给前端 ──────────────────────────────

@pytest.fixture()
def panel_base():
    cfg = build_config(env={}, yaml_data={"tasks": []})
    venues = {"arcus": PaperVenue(venue_id="arcus", prices={"BTC": 60_000.0}),
              "lighter": PaperVenue(venue_id="lighter",
                                    prices={"BTC": 60_000.0})}
    ledger = Ledger()
    engine = Engine.from_config(cfg, venues, ledger=ledger)
    state = PanelState(engine=engine, venues=venues, ledger=ledger,
                       app_config=cfg, task_cfgs={})
    server = serve(state, "127.0.0.1", 0)
    base = f"http://127.0.0.1:{server.server_address[1]}"
    stop_flag = threading.Event()

    def _pump():
        while not stop_flag.is_set():
            engine.process_commands()
            time.sleep(0.01)

    threading.Thread(target=_pump, daemon=True).start()
    yield base
    stop_flag.set()
    server.shutdown()


def test_strategy_specs_endpoint_has_page_meta(panel_base):
    with urllib.request.urlopen(panel_base + "/api/strategy-specs",
                                timeout=5) as r:
        d = json.loads(r.read().decode("utf-8"))
    by = {s["strategy"]: s for s in d["strategies"]}
    assert by["funding_carry"]["title"] == "资金费套利"
    for s in d["strategies"]:
        assert s["page"]["venue_fields"] and s["page"]["primary"]
        assert any(f["primary"] for f in s["fields"])


def test_index_page_serves_sidebar_layout(panel_base):
    with urllib.request.urlopen(panel_base + "/", timeout=5) as r:
        body = r.read().decode("utf-8")
    assert 'id="sidebar"' in body
    assert 'data-page="strat-funding_carry"' in body
    assert "资金费套利" in body and "资金费 carry" not in body
