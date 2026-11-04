"""左侧边栏 + 每策略一页测试（第五轮：用户定版结构）。

覆盖：边栏入口与四个策略页容器存在且顺序为用户定版（总览 /
中性网格 / 溢价套利 / 资金费套利 / 轮换对冲刷量 / 设置，共 6 项，
无账本、无「数据」入口、无通用新建任务按钮）；策略页定版流程
的区块标记（运作方式 / 订单腿 / 金额（自由输入、无 chips）/
选市场（pairs 下拉）/ 配置（预设四态）/ 高级选项 / 交易预览 /
运行中仓位，无热门机会表）；总览策略记录两段与最佳机会卡片；
极客风主题关键样式；「资金费套利」改名一致性；specs 的 page
元数据自洽（primary / venue_fields / amount / market_value /
how_it_works / presets）；/api/strategy-specs 把 page 元数据
带给前端。
"""

import json
import threading
import time
import urllib.request

import pytest

from detsp.core.config import build_config
from detsp.core.engine import Engine
from detsp.core.ledger import Ledger
from detsp.strategies.specs import (PAGE_META, PARAMS_CLASSES, PRESETS,
                                    PRESET_LABELS, STRATEGY_TITLES,
                                    strategy_specs)
from detsp.venues.paper import PaperVenue
from detsp.web.page import PAGE_HTML
from detsp.web.server import PanelState, serve

SIDEBAR_PAGES = ["overview", "strat-hedged_grid", "strat-premium_arb",
                 "strat-funding_carry", "strat-rotation_hedge",
                 "settings"]


# ── 边栏与策略页容器 ────────────────────────────────────────

def test_sidebar_entries_and_order():
    start = PAGE_HTML.index('id="sidebar"')
    end = PAGE_HTML.index("</nav>")
    sidebar = PAGE_HTML[start:end]
    pos = [sidebar.index(f'data-page="{p}"') for p in SIDEBAR_PAGES]
    assert pos == sorted(pos), \
        "边栏顺序必须是用户定版：总览/网格/套利/资金费/轮换/设置"
    for label in ("总览", "中性网格", "溢价套利", "资金费套利",
                  "轮换对冲刷量", "设置"):
        assert label in sidebar
    # 账本已按定版从边栏移除
    assert "账本" not in sidebar
    assert 'data-page="fills"' not in PAGE_HTML


def test_sidebar_slimmed_no_market_no_newtask():
    # 边栏没有「数据」入口、没有通用新建任务按钮，数据页与账本页
    # 整体删除（/api/market、/api/fills 接口保留供策略页使用）
    assert 'data-page="market"' not in PAGE_HTML
    assert 'id="page-market"' not in PAGE_HTML
    assert 'id="page-fills"' not in PAGE_HTML
    assert 'id="newBtn"' not in PAGE_HTML
    assert 'id="spreadTbl"' not in PAGE_HTML


def test_geek_theme_tokens():
    # 极客终端风：近黑底、面板面、终端绿强调、等宽字体、直角
    assert "#0b0e11" in PAGE_HTML
    assert "#11161c" in PAGE_HTML
    assert "#3fb950" in PAGE_HTML
    assert "ui-monospace" in PAGE_HTML
    assert "border-radius:0" in PAGE_HTML
    # 旧蓝圆角主题不得残留
    assert "#2456a6" not in PAGE_HTML
    assert "border-radius:4px" not in PAGE_HTML
    # 状态标签：圆点 + 大写英文
    assert "RUNNING" in PAGE_HTML and "statusHtml" in PAGE_HTML


def test_strategy_page_containers_exist():
    for p in SIDEBAR_PAGES:
        assert f'id="page-{p}"' in PAGE_HTML
    # 策略页容器是空壳，由前端通用渲染器按 specs 填充
    for st in PARAMS_CLASSES:
        assert f'<section class="page" id="page-strat-{st}"></section>' \
            in PAGE_HTML


def test_strategy_page_flow_blocks():
    # 第五轮流程（用户定版）：页头 → 订单腿 → 金额 → 选市场 →
    # 配置（预设）→ 高级选项 → 交易预览 → 运行 → 运行中仓位；
    # 区块标题由渲染器 JS 生成，在渲染器源码范围内断言顺序
    renderer = PAGE_HTML[PAGE_HTML.index("function buildStrategyPage"):]
    flow = ["运作方式</summary>", "<h3>订单腿</h3>", "<h3>金额</h3>",
            "<h3>选市场</h3>", "<h3>配置</h3>", "高级选项（其余参数",
            "<h3>交易预览</h3>", "<h3>运行中仓位</h3>"]
    pos = [renderer.index(t) for t in flow]
    assert pos == sorted(pos), "策略页区块顺序必须是定版流程"
    assert "runBtn" in PAGE_HTML and "运行" in PAGE_HTML
    # 旧区块标题不得残留（按完整标题字面断言，避免误伤「所需保证金」等词）
    for gone in ("<h3>交易平台</h3>", "<h3>保证金</h3>",
                 "<h3>本策略任务</h3>", "<h3>策略机会"):
        assert gone not in PAGE_HTML
    # 热门机会表已按定版从策略页移除（移去总览做最佳机会卡片）
    assert "热门机会" not in PAGE_HTML


def test_no_chips_but_presets_and_pairs():
    # 金额改为纯自由输入：快捷额 chips 整体删除
    assert "AMT_CHIPS" not in PAGE_HTML
    assert "chipAmt" not in PAGE_HTML
    assert "setAmountChip" not in PAGE_HTML
    # 配置预设四态：按钮渲染 + 整组填入 + 改动转自定义
    assert "data-preset" in PAGE_HTML
    assert "applyPreset" in PAGE_HTML and "markCustom" in PAGE_HTML
    for label in ("保守", "中性", "激进", "自定义"):
        assert label in PAGE_HTML
    # 选市场走 pairs 接口（两所共同上市全量），指标用 apr_pct 等行字段
    assert "/api/pairs" in PAGE_HTML and "loadPairs" in PAGE_HTML
    assert "apr_pct" in PAGE_HTML and "共同" in PAGE_HTML
    # 网格金额 = 目标总名义自由输入，反推每格量
    assert "gridTotalInp" in PAGE_HTML and "目标总名义" in PAGE_HTML
    assert "gridTotalToQty" in PAGE_HTML


def test_preview_block_markers():
    # 交易预览：按平台分列 + 入场条件指示（前端用 market/overview 算）
    for marker in ("标记价", "预估执行价", "所需保证金", "总保证金",
                   "renderPreview", "pairRates"):
        assert marker in PAGE_HTML
    assert "已达入场门槛" in PAGE_HTML
    assert "未达门槛" in PAGE_HTML and "启动后等待机会" in PAGE_HTML
    assert "已进入入场带" in PAGE_HTML
    assert "每格间距" in PAGE_HTML
    # 订单腿：方向徽标与数量联动
    assert "做多" in PAGE_HTML and "做空" in PAGE_HTML
    assert "网格双向" in PAGE_HTML and "反向对冲" in PAGE_HTML
    assert "renderLegs" in PAGE_HTML


def test_overview_records_and_best_cards():
    # 旧「运行中仓位（全部策略）」表已按定版移除，改为策略记录两段
    assert 'id="allPos"' not in PAGE_HTML
    assert "运行中仓位（全部策略）" not in PAGE_HTML
    assert "策略记录" in PAGE_HTML
    assert 'id="recOpen"' in PAGE_HTML and 'id="recClosed"' in PAGE_HTML
    assert "运行仓位" in PAGE_HTML and "仓位历史" in PAGE_HTML
    assert "renderRecords" in PAGE_HTML and "/api/records" in PAGE_HTML
    # 四策略最佳机会卡片：数据走 /api/best，点行跳策略页带入
    assert 'id="bestCards"' in PAGE_HTML
    assert "renderBestCards" in PAGE_HTML and "/api/best" in PAGE_HTML
    assert "data-goto" in PAGE_HTML and "gotoStrategy" in PAGE_HTML


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
                        "primary": list(PAGE_META[st]["primary"]),
                        "presets": PRESETS[st],
                        "preset_labels": PRESET_LABELS}
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


def test_page_meta_mob_flow_fields():
    # 第四轮新增元数据：运作方式文案、下拉机会值类型、主金额字段
    for spec in strategy_specs():
        st = spec["strategy"]
        page = spec["page"]
        steps = page["how_it_works"]
        assert 3 <= len(steps) <= 5, f"{st} 运作方式应为 3~5 步"
        assert all(isinstance(s, str) and s.strip() for s in steps)
        assert page["market_value"] in ("funding_apr", "spread_bps",
                                        "price")
        fields = {f["name"]: f for f in spec["fields"]}
        if st == "hedged_grid":
            # 网格无单一金额字段：页面用每格量×格数折算总名义
            assert page["amount"] is None
            assert page["market_value"] == "price"
        else:
            # 主金额必须是真实字段，且在 primary 里（金额区渲染）
            assert page["amount"] in fields
            assert page["amount"] in page["primary"]
        if page["opportunity"] == "funding":
            assert page["market_value"] == "funding_apr"
        if page["opportunity"] == "spread":
            assert page["market_value"] == "spread_bps"


def test_primary_fields_per_strategy():
    by = {s["strategy"]: set(s["page"]["primary"])
          for s in strategy_specs()}
    assert {"hold_min_seconds", "hold_max_seconds"} <= \
        by["rotation_hedge"]
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
        assert s["page"]["how_it_works"]
        assert s["page"]["market_value"]
        assert "amount" in s["page"]
        assert any(f["primary"] for f in s["fields"])


def test_index_page_serves_sidebar_layout(panel_base):
    with urllib.request.urlopen(panel_base + "/", timeout=5) as r:
        body = r.read().decode("utf-8")
    assert 'id="sidebar"' in body
    assert 'data-page="strat-funding_carry"' in body
    assert "资金费套利" in body and "资金费 carry" not in body
