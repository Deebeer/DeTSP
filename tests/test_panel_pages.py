"""左侧边栏 + 每策略一页测试（第六轮：用户定版结构）。

覆盖：边栏入口与四个策略页容器存在且顺序为用户定版（总览 /
中性网格 / 溢价套利 / 资金费套利 / 轮换对冲刷量 / 策略记录 /
设置，共 7 项，无账本、无「数据」入口、无通用新建任务按钮）；
策略页定版流程的区块标记（运作方式 / 订单腿 / 金额（自由输入、
无 chips）/ 选市场（pairs 下拉）/ 配置（预设四态）/ 高级选项 /
交易预览 / 运行中仓位，无热门机会表）；总览两行统计（今日 +
累计，含盈亏估算口径）与最佳机会混合列表；策略记录独立页
两段；字段说明三段式（label 只剩名 + desc 一句话说明，页面
fname/fdesc 两行呈现）；颜色只用一套变量、
无杂色残留；极客风主题关键样式；「资金费套利」改名一致性；
实时性与杠杆维度（20 秒刷新、更新戳与失败保留、选市场统计/
价格行、订单腿杠杆行、预览超限警告）；specs 的 page 元数据自洽（primary / venue_fields / amount /
market_value / how_it_works / presets / amount_ratios）；
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
from detsp.strategies.specs import (AMOUNT_RATIOS, PAGE_META,
                                    PARAMS_CLASSES, PRESETS,
                                    PRESET_LABELS, STRATEGY_TITLES,
                                    strategy_specs)
from detsp.venues.paper import PaperVenue
from detsp.web.page import PAGE_HTML
from detsp.web.server import PanelState, serve

SIDEBAR_PAGES = ["overview", "strat-hedged_grid", "strat-premium_arb",
                 "strat-funding_carry", "strat-rotation_hedge",
                 "records", "settings"]


# ── 边栏与策略页容器 ────────────────────────────────────────

def test_sidebar_entries_and_order():
    start = PAGE_HTML.index('id="sidebar"')
    end = PAGE_HTML.index("</nav>")
    sidebar = PAGE_HTML[start:end]
    pos = [sidebar.index(f'data-page="{p}"') for p in SIDEBAR_PAGES]
    assert pos == sorted(pos), \
        "边栏顺序必须是用户定版：总览/网格/套利/资金费/轮换/策略记录/设置"
    for label in ("总览", "中性网格", "溢价套利", "资金费套利",
                  "轮换对冲刷量", "策略记录", "设置"):
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


def test_color_palette_single_source_no_junk_colors():
    # 颜色只用一套变量：说明暗灰 --mut、正文 --txt、强调 --acc、
    # 负/空 --red、正/多 --acc、警告 --yel；杂色一律不得残留
    assert "--mut:#8b949e" in PAGE_HTML
    assert "--txt:#c9d1d9" in PAGE_HTML
    assert "--acc:#3fb950" in PAGE_HTML
    assert "--red:#f85149" in PAGE_HTML
    assert "--yel:#d29922" in PAGE_HTML
    for junk in ("#ff9d97", "#58a6ff", "#1f6feb", "#bc8cff",
                 "#a371f7", "#79c0ff", "background:#445", "#161b22"):
        assert junk not in PAGE_HTML, f"杂色残留：{junk}"
    # 说明文字（.muted/.fhelp）统一走 --mut，不许各写各的灰
    assert ".muted{color:var(--mut)}" in PAGE_HTML
    assert ".fhelp{" in PAGE_HTML and "color:var(--mut)" in PAGE_HTML


def test_field_labels_have_short_explanations():
    # 全站字段三段式：specs 输出把「字段名（简短说明）」拆成
    # label（只有名、无括号）+ desc（一句话说明，大白话、扫一眼懂）
    for spec in strategy_specs():
        for f in spec["fields"]:
            label, desc = f["label"], f.get("desc")
            assert "（" not in label and "）" not in label, \
                f"{spec['strategy']}.{f['name']} label 不应再带括号：{label}"
            assert desc, \
                f"{spec['strategy']}.{f['name']} 缺 desc 说明"
            # 第七轮：高级字段 desc 改「机器人会怎么做」的动作
            # 语言，长度上限放宽为 desc ≤ 60 字（仍须简短）
            assert len(desc) <= 60, \
                f"{spec['strategy']}.{f['name']} 说明太长：{desc}"
    # 用户点名的例子
    by = {s["strategy"]: {f["name"]: f for f in s["fields"]}
          for s in strategy_specs()}
    assert by["hedged_grid"]["lower_price"]["label"] == "网格区间下限"
    assert by["hedged_grid"]["lower_price"]["desc"] == "网格铺单的最低价"
    # 金额区说明按用户例子：名下直接写「自由填写（USD，每边）」
    pages = {s["strategy"]: s["page"] for s in strategy_specs()}
    assert pages["rotation_hedge"]["amount_desc"] == "自由填写（USD，每边）"
    assert pages["funding_carry"]["amount_desc"] == "自由填写（USD，每边）"


def test_sixth_round_layout_markers():
    # 三段式渲染：fname（名）/ fdesc（说明）两行 + 输入框
    assert 'class="fname"' in PAGE_HTML and 'class="fdesc"' in PAGE_HTML
    assert "fldHead" in PAGE_HTML
    # 选市场定版版式：统计行 + 币种下拉 + 价格行（实际价格带口径）
    assert "pairsStats" in PAGE_HTML and "pairPrices" in PAGE_HTML
    assert '"共同 "' in PAGE_HTML and '" 上市 "' in PAGE_HTML
    assert '"价格："' in PAGE_HTML and "renderPairPrices" in PAGE_HTML
    # 数据区块更新戳：时间取接口 updated_at，失败标黄保留旧数据
    for marker in ('id="ovUpd"', 'id="bestUpd"', 'id="recUpd"',
                   "pairsUpd", "prevUpd", "setUpd", "更新 ",
                   "刷新失败，显示上次数据"):
        assert marker in PAGE_HTML, marker
    # 实时节奏：pairs/最佳机会 20 秒（与扫描缓存对齐），旧 30 秒不得残留
    assert "},20000);" in PAGE_HTML
    assert "},30000);" not in PAGE_HTML
    # 订单腿杠杆行 + 预览超限警告（只展示与警告，不改执行行为）
    assert "data-leglev" in PAGE_HTML and "legLevText" in PAGE_HTML
    assert "杠杆上限未知" in PAGE_HTML and "逐仓 only" in PAGE_HTML
    assert "实际执行可能被该所拒绝或按上限成交" in PAGE_HTML
    assert "所需保证金按各腿所填杠杆计算" in PAGE_HTML


def test_leg_rows_show_available_balance():
    # 订单腿每行显示该所可用余额（权益），paper 标模拟，取不到
    # 显示「可用 —」，随 refresh 的 renderLegs 更新
    assert 'data-legbal="' in PAGE_HTML
    assert "legBal" in PAGE_HTML
    assert "可用 $" in PAGE_HTML and "（权益 $" in PAGE_HTML
    assert "可用 —" in PAGE_HTML and "模拟余额" in PAGE_HTML


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


def test_overview_stats_best_list_and_records_page():
    # 总览两行统计：今日一行 + 累计一行，第四项是盈亏（估算）
    for marker in ('id="tvol"', 'id="tfee"', 'id="tcnt"', 'id="tpnl"',
                   'id="avol"', 'id="afee"', 'id="acnt"', 'id="apnl"',
                   "今日盈亏（估算）", "累计盈亏（估算）",
                   "累计成交量", "累计手续费", "累计成交笔数",
                   'id="pnlNote"'):
        assert marker in PAGE_HTML
    # 旧单行口径不得残留
    assert "持仓未实现盈亏（估算）" not in PAGE_HTML
    assert 'id="upnl"' not in PAGE_HTML
    # 最佳机会改单一混合列表（四策略合并、按机会值排序），卡片删除
    assert 'id="bestTbl"' in PAGE_HTML
    assert 'id="bestCards"' not in PAGE_HTML
    assert "renderBestCards" not in PAGE_HTML
    assert "renderBest" in PAGE_HTML and 'fetch("/api/best")' in PAGE_HTML
    assert "score_pct" in PAGE_HTML
    assert "data-goto" in PAGE_HTML and "gotoStrategy" in PAGE_HTML
    # 策略记录独立成页：两段表格在记录页容器内，不在总览里
    ov = PAGE_HTML[PAGE_HTML.index('id="page-overview"'):
                   PAGE_HTML.index('id="page-records"')]
    assert 'id="recOpen"' not in ov and "策略记录" not in ov
    rec = PAGE_HTML[PAGE_HTML.index('id="page-records"'):
                    PAGE_HTML.index('id="page-strat-hedged_grid"')]
    assert 'id="recOpen"' in rec and 'id="recClosed"' in rec
    assert "运行仓位" in rec and "仓位历史" in rec
    assert "renderRecords" in PAGE_HTML and "/api/records" in PAGE_HTML
    assert 'id="allPos"' not in PAGE_HTML
    assert "运行中仓位（全部策略）" not in PAGE_HTML


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
                        "preset_labels": PRESET_LABELS,
                        "amount_ratios": AMOUNT_RATIOS.get(st, {})}
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


# ── 第七轮：总览顺序 / 去拥挤 / 配置说明 / 高级大白话 ──────

def test_overview_block_order():
    # 总览主体顺序定版：今日统计 → 累计统计 → 盈亏口径小字 →
    # 场所表 → 最佳机会表 → 安全警告 / 停机 / 系统事件
    ov = PAGE_HTML[PAGE_HTML.index('id="page-overview"'):
                   PAGE_HTML.index('id="page-records"')]
    flow = ["今日成交量", "累计成交量", 'id="pnlNote"', 'id="venues"',
            "最佳机会", 'id="bestTbl"', 'class="warn"',
            'id="killbox"', 'id="events"']
    pos = [ov.index(t) for t in flow]
    assert pos == sorted(pos), "总览区块顺序必须是第七轮定版"
    # 场所表列定版：场所/模式/权益/可用/持仓/挂单数
    venues = ov[ov.index('id="venues"'):ov.index("最佳机会")]
    for col in ("场所", "模式", "权益", "可用", "持仓", "挂单数"):
        assert f"<th>{col}</th>" in venues


def test_config_note_and_advanced_plain_language():
    # 配置区说明行：用户定版原文，位于「配置」标题正下方、预设
    # 按钮行之上
    note = "先在「金额」区填好金额，再点预设：只套结构参数" \
           "（杠杆/门槛/格数等），金额不改（中性 = 原仓对齐" \
           "默认值）；改动任何字段自动切到「自定义」。"
    assert note in PAGE_HTML
    renderer = PAGE_HTML[PAGE_HTML.index("function buildStrategyPage"):]
    assert renderer.index("<h3>配置</h3>") < renderer.index(note) \
        < renderer.index("presetRow")
    # 高级选项总说明 + 按开仓/平仓/保护/其他语义分组渲染
    assert "以下参数控制机器人自动开仓、平仓的行为细节，" \
           "保持默认也能正常运行。" in PAGE_HTML
    assert "advFieldsHtml" in PAGE_HTML
    # 四策略全部高级字段（非 primary、非两腿）都有语义分组
    for spec in strategy_specs():
        vf = set(spec["page"]["venue_fields"])
        groups = set()
        for f in spec["fields"]:
            if f["primary"] or f["name"] in vf:
                continue
            assert f["adv_group"] in ("开仓", "平仓", "保护", "其他"), \
                f"{spec['strategy']}.{f['name']} 缺高级分组"
            groups.add(f["adv_group"])
        # 每策略至少有开仓/保护两组；溢价套利的平仓由主配置区
        # 的入场带决定、高级区无平仓组，其余三策略三组齐全
        assert {"开仓", "保护"} <= groups, \
            f"{spec['strategy']} 高级分组应至少覆盖开仓/保护"
        if spec["strategy"] != "premium_arb":
            assert "平仓" in groups, \
                f"{spec['strategy']} 高级分组应有平仓组"


def test_symbol_dropdown_two_segments_and_stats_line():
    # 选市场去拥挤：下拉选项只剩「币种 · 关键指标」两段，现价
    # 与混口径都不进下拉（混口径移到价格行末尾小字）
    seg = PAGE_HTML[PAGE_HTML.index("function fillPairSymbols"):
                    PAGE_HTML.index("function renderPairPrices")]
    assert "现价" not in seg and "混口径" not in seg
    assert "疑似同名不同资产" not in seg
    assert "混口径，仅供参考" in PAGE_HTML
    # 统计行精简：删括号长尾，过滤后数量不同才短注「可交易 M」
    assert "（按策略排序后可交易" not in PAGE_HTML
    assert '" · 可交易 "' in PAGE_HTML
    assert '"共同 "' in PAGE_HTML and '" 上市 "' in PAGE_HTML
