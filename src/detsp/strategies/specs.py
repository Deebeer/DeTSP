"""策略参数规格（单一事实源）。

三个策略的 Params dataclass 是字段与默认值的唯一定义；本模块在其
之上叠加展示/校验元数据（中文说明、分组、枚举选项），生成：

- 面板动态表单（GET /api/strategy-specs 直接返回本模块的产物）；
- 参数校验 validate_params()：config 组装（core.engine.build_task）
  与面板新建/编辑任务共用同一份校验，杜绝两处口径漂移。

字段名、必填性（= dataclass 无默认值的字段）、默认值全部由
dataclass 反射得到，不在本模块重复维护；tests 里有专门的一致性
测试，Params 改字段时规格自动跟随、元数据缺失会让测试失败。
"""

from __future__ import annotations

import dataclasses
import re

from ..core.config import VENUE_IDS
from .funding_carry import CarryParams
from .hedged_grid import HedgedGridParams
from .premium_arb import PremiumParams

PARAMS_CLASSES = {
    "hedged_grid": HedgedGridParams,
    "premium_arb": PremiumParams,
    "funding_carry": CarryParams,
}

STRATEGY_TITLES = {
    "hedged_grid": "中性网格",
    "premium_arb": "溢价套利",
    "funding_carry": "资金费套利",
}

STRATEGY_BLURBS = {
    "hedged_grid": "网格腿铺单吃震荡，对冲腿在另一所把净仓位调平，不赌方向。",
    "premium_arb": "盯两所价差，偏离中枢开仓、回归平仓，赚价差回归的钱。",
    "funding_carry": "两所一多一空长期持有，专门收两所资金费率的差额。",
}

# 字段展示元数据：label 以「字段名（简短说明）」书写（单一事实
# 源），strategy_specs() 输出时拆成 label（名）与 desc（说明）
# 两项，面板按三段式呈现（名一行、说明一行暗灰小字、输入框
# 一行）；group（分组）/ type 覆盖 / options（枚举）。不在此列
# 的字段按 dataclass 类型推断。
_LABEL_RE = re.compile(r"^(.*?)（(.*)）$")


def _split_label(raw: str) -> tuple[str, str]:
    m = _LABEL_RE.match(raw or "")
    if m:
        return m.group(1), m.group(2)
    return raw or "", ""
_VENUE = {"type": "venue"}
_FIELD_META: dict[str, dict[str, dict]] = {
    "hedged_grid": {
        "grid_venue": {"label": "网格腿场所（铺网格单的一所）",
                       "group": "场所与标的", **_VENUE},
        "hedge_venue": {"label": "对冲腿场所（对冲网格仓位的一所）",
                        "group": "场所与标的", **_VENUE},
        "symbol": {"label": "币种（要交易的标的）", "group": "场所与标的"},
        "lower_price": {"label": "网格区间下限（网格铺单的最低价）",
                        "group": "网格参数"},
        "upper_price": {"label": "网格区间上限（网格铺单的最高价）",
                        "group": "网格参数"},
        "grid_count": {"label": "格数（区间切成多少格，至少 2）",
                       "group": "网格参数"},
        "qty_per_grid": {"label": "每格数量（每格下单的币数）",
                         "group": "网格参数"},
        "mode": {"label": "方向模式（网格铺单方向：只挂买单、只挂卖单，还是两边都挂）",
                 "group": "网格参数", "type": "enum",
                 "options": [("long", "只做多"), ("short", "只做空"),
                             ("long_short", "双向")]},
        "out_of_range_action": {
            "label": "冲出区间后的动作（价格冲出区间时机器人怎么做：平掉双腿停机，或只减仓回收）",
            "group": "网格参数", "type": "enum",
            "options": [("close", "平双腿并停机"),
                        ("reduce_only", "只减仓回收")]},
        "leverage_grid": {"label": "网格腿杠杆（填账户里实际设的杠杆，只对账校验，程序不自动改）",
                          "group": "对冲与风控"},
        "leverage_hedge": {"label": "对冲腿杠杆（填账户里实际设的杠杆，只对账校验，程序不自动改）",
                           "group": "对冲与风控"},
        "hedge_deadband_qty": {"label": "对冲死区（两腿仓位差小于这个币数时先不补对冲，差大了才补）",
                               "group": "对冲与风控"},
        "max_unhedged_seconds": {"label": "裸露超时（一腿先成交、另一腿没对上，超过这个秒数就强制市价对冲，0=关）",
                                 "group": "对冲与风控"},
        "max_residual_seconds": {"label": "小残差超时（对冲剩下的小尾巴仓位留超过这个秒数，机器人会强制补平，0=关）",
                                 "group": "对冲与风控"},
        "skip_band_frac": {"label": "贴价跳档带宽（离现价太近的格子先不挂单，跳过范围占一个格距的比例）",
                           "group": "对冲与风控"},
        "fee_bps_per_side": {"label": "单边手续费估算（启动时按这个费率算账：每格利润不够付双边手续费就拒绝启动）",
                             "group": "对冲与风控"},
        "takeover_position": {"label": "接管已有持仓（启动时把网格腿已有的仓位接过来继续管，默认关）",
                              "group": "对冲与风控"},
    },
    "premium_arb": {
        "base_venue": {"label": "基准腿场所（盯价差定方向的一所）",
                       "group": "场所与标的", **_VENUE},
        "hedge_venue": {"label": "对冲腿场所（跟随对冲的一所）",
                        "group": "场所与标的", **_VENUE},
        "symbol": {"label": "币种（要交易的标的）", "group": "场所与标的"},
        "midline_bps": {"label": "溢价中枢（两所价差的中间线，单位 bps 即万分之一）",
                        "group": "溢价带",
                        "help": "中枢必须用录制+analyze 实测，不能照抄"
                                "示例值（原仓 example 的 0 只是占位）。"},
        "upper_bps": {"label": "上方入场带（价差高出中枢多少 bps 开仓）",
                      "group": "溢价带"},
        "lower_bps": {"label": "下方入场带（价差低于中枢多少 bps 开仓）",
                      "group": "溢价带"},
        "persist_seconds": {"label": "信号确认（价差越过入场带并持续这么多秒，机器人才开仓，防假信号）",
                            "group": "溢价带"},
        "cooldown_seconds": {"label": "执行冷却（开完一仓后至少等这么多秒才许再开，0=不限）",
                             "group": "溢价带"},
        "close_band_bps": {
            "label": "回归平仓带（价差回到中枢上下这个 bps 内就双腿全平，0=关闭）",
            "group": "溢价带",
            "help": "DeTSP 新增字段（非原仓），默认 0=关：不设时"
                    "平仓只靠反向越带信号，行为与原来完全一致。"},
        "max_position_usd": {"label": "最大持仓（两腿最多持有多少美元）",
                             "group": "风控"},
        "max_order_notional_usd": {"label": "单笔上限（一笔订单最多下这么多美元，更大的量会拆成多笔下）",
                                   "group": "风控",
                                   "help": "第十二轮起面板不填此值：单笔上限"
                                           "由「单笔上限占最大持仓 %」在组装时"
                                           "派生；手写配置里显式填了本字段且没填"
                                           "百分比时，仍以本字段为准。"},
        "order_cap_pct": {"label": "单笔上限占最大持仓 %（单笔上限 = 最大持仓 × 这个百分比，组装时自动换算）",
                          "group": "风控"},
        "min_order_notional_usd": {"label": "单笔下限（算出的下单金额低于这个数就不下，跳过这次机会）",
                                   "group": "风控"},
        "inventory_scale_bps": {"label": "库存阶梯（持仓越接近上限，开仓门槛额外抬高，最多加这么多 bps）",
                                "group": "风控"},
        "inventory_floor_frac": {"label": "库存阶梯起点（持仓达到最大持仓的这个比例后，才开始抬门槛）",
                                 "group": "风控"},
        "take_fraction": {"label": "吃单深度比例（每笔单最多吃掉对手盘挂单量的这个比例）",
                          "group": "风控"},
        "base_taker_fee_bps": {"label": "基准腿吃单费率（只用来估算收益：吃单一次按这个 bps 扣费）",
                               "group": "执行"},
        "hedge_taker_fee_bps": {"label": "对冲腿吃单费率（只用来估算收益：吃单一次按这个 bps 扣费）",
                                "group": "执行"},
        "execution_mode": {"label": "执行模式（挂单等成交还是直接吃单）",
                           "group": "执行", "type": "enum",
                           "options": [("taker", "吃单（双腿立即成交）"),
                                       ("maker", "挂单（主腿先挂单等成交）")]},
        "maker_offset_ticks": {"label": "挂单偏移（maker 挂单时，挂价离盘口几个最小跳动单位 tick）",
                               "group": "执行"},
        "maker_timeout_seconds": {"label": "挂单超时（主腿挂单等这么多秒没成交就撤单，这次不开仓）",
                                  "group": "执行"},
        "reconcile_seconds": {"label": "对账周期（每隔这么多秒核对一次两腿仓位，对不平就自动修）",
                              "group": "执行"},
        "leverage_base": {"label": "基准腿杠杆（填账户里实际设的杠杆，只对账校验，程序不自动改）",
                          "group": "风控"},
        "leverage_hedge": {"label": "对冲腿杠杆（填账户里实际设的杠杆，只对账校验，程序不自动改）",
                           "group": "风控"},
        "settle_grace_seconds": {"label": "结算宽限（开仓后的这段秒数内先不修仓，等交易所把成交结算完，防误判）",
                                 "group": "执行",
                                 "help": "与原仓差异：entropy-arb 的 "
                                         "settle_timeout 是 5~10 秒的等成交"
                                         "确认；本字段是防异步结算误削腿的"
                                         "状态机宽限，保留 20 秒（安全差异）。"},
        "max_slippage_bps": {"label": "削腿滑点上限（两腿数量对不平时修仓，限价最多偏离现价这么多 bps，超了不下单）",
                             "group": "执行",
                             "help": "与原仓 hedge_slippage_bps=20 一致；"
                                     "原仓开仓腿滑点保护 leg_slippage_bps=50 "
                                     "未单独移植（DeTSP 开仓走市价单+未知"
                                     "结果对账口径）。"},
    },
    "funding_carry": {
        "venue_a": {"label": "场所 A（A 腿用的交易所）",
                    "group": "场所与标的", **_VENUE},
        "venue_b": {"label": "场所 B（B 腿用的交易所）",
                    "group": "场所与标的", **_VENUE},
        "symbol": {"label": "币种（要交易的标的）", "group": "场所与标的"},
        "notional_usd": {"label": "单边名义（仓位名义：一边开多少美元；面板任务由保证金 × 杠杆自动换算，手写配置时也可直接填）",
                         "group": "场所与标的"},
        "leverage": {"label": "两腿杠杆（填账户里实际设的杠杆，只对账校验，程序不自动改）",
                     "group": "场所与标的"},
        "max_open_spread_bps": {"label": "最大开仓价差（开仓时两所价差超过这个 bps 就不开，防同名不同标的）",
                                "group": "开仓条件"},
        "min_edge_bps_per_hour": {"label": "最低资金费率差（净费差达到这个数才开仓、跌破并连续确认后平仓，进出共用一条线，单位 bps/小时）",
                                  "group": "开仓条件"},
        "close_corridor_pct": {"label": "强平保护（任一腿距强平价近于这个百分比就双腿平仓）",
                               "group": "平仓条件"},
        "qty_dev_close_pct": {"label": "数量偏差自动平仓（两腿数量差超过这个百分比、按较大腿算，就双腿平仓；小偏差先削腿修平，0=关）",
                              "group": "平仓条件"},
        "reopen": {"label": "自动重开（平仓后费差又达标时，机器人自动重新开仓）",
                   "group": "平仓后怎么办"},
        "exit_confirm_checks": {"label": "退出确认次数（系统值，面板不露）",
                                "group": "系统值"},
        "check_seconds": {"label": "检查周期（系统值，面板不露）",
                          "group": "系统值"},
        "cooldown_seconds": {"label": "重开冷却（系统值，面板不露）",
                             "group": "系统值"},
        "settle_grace_seconds": {"label": "结算宽限（开仓后的这段秒数内先不修仓，等交易所把成交结算完，防误判）",
                                 "group": "执行与风控"},
        "max_slippage_bps": {"label": "削腿滑点上限（两腿数量对不平时修仓，限价最多偏离现价这么多 bps，超了不下单）",
                             "group": "执行与风控",
                             "help": "与原仓差异：Parallax 下单滑点上限"
                                     "为 10 bps；本字段只管削腿修正单，"
                                     "保留 20 给薄盘口成交余量（安全差异）。"},
    },
}

# 自动化配置分组（面板「配置」区内「自动化配置」子区的小分组
# 标题）：策略都是自动运行，这些字段是机器人自动开/平仓
# 行为的旋钮，按语义分 开仓 / 平仓 / 保护 / 其他 四组。只有非
# primary、非两腿场所字段（即进自动化配置的那些）需要登记；
# 未登记者归「其他」。
ADV_GROUPS: dict[str, dict[str, str]] = {
    "hedged_grid": {
        "mode": "开仓", "skip_band_frac": "开仓",
        "out_of_range_action": "平仓",
        "hedge_deadband_qty": "保护", "max_unhedged_seconds": "保护",
        "max_residual_seconds": "保护", "fee_bps_per_side": "保护",
        "takeover_position": "其他",
    },
    "premium_arb": {
        "persist_seconds": "开仓", "cooldown_seconds": "开仓",
        "max_order_notional_usd": "开仓",
        "min_order_notional_usd": "开仓",
        "inventory_scale_bps": "开仓", "inventory_floor_frac": "开仓",
        "take_fraction": "开仓", "maker_offset_ticks": "开仓",
        "maker_timeout_seconds": "开仓",
        "close_band_bps": "平仓",
        "settle_grace_seconds": "保护",
        "max_slippage_bps": "保护",
    },
    "funding_carry": {
        "reopen": "开仓",
        "max_slippage_bps": "保护",
    },
}


# 配置五段（第十轮定版，单一事实源）：每策略每个字段恰好属于
# 一段，面板配置区按段组织、按模式显隐。段顺序固定为
# SECTION_ORDER；字段名以各 Params dataclass 为准（含新增字段）。
SECTION_ORDER = ("交易对象", "开仓条件", "平仓条件", "平仓后怎么办", "保护")
# 兼容别名：五段顺序常量
SECTIONS = SECTION_ORDER

SECTION_MAP: dict[str, dict[str, str]] = {
    "funding_carry": {
        "venue_a": "交易对象", "venue_b": "交易对象",
        "symbol": "交易对象", "notional_usd": "交易对象",
        "leverage": "交易对象",
        "max_open_spread_bps": "开仓条件",
        "min_edge_bps_per_hour": "开仓条件",
        "close_corridor_pct": "平仓条件",
        "qty_dev_close_pct": "平仓条件",
        "reopen": "平仓后怎么办",
        "max_slippage_bps": "保护",
        # 系统值（hidden，不在面板输出）：仅为完整性登记
        "exit_confirm_checks": "平仓条件",
        "check_seconds": "开仓条件",
        "cooldown_seconds": "平仓后怎么办",
        "settle_grace_seconds": "保护",
    },
    "premium_arb": {
        "base_venue": "交易对象", "hedge_venue": "交易对象",
        "symbol": "交易对象", "max_position_usd": "交易对象",
        "leverage_base": "交易对象", "leverage_hedge": "交易对象",
        "midline_bps": "开仓条件", "upper_bps": "开仓条件",
        "lower_bps": "开仓条件", "persist_seconds": "开仓条件",
        "inventory_scale_bps": "开仓条件",
        "inventory_floor_frac": "开仓条件",
        "execution_mode": "开仓条件", "maker_offset_ticks": "开仓条件",
        "maker_timeout_seconds": "开仓条件",
        "max_order_notional_usd": "开仓条件", "order_cap_pct": "开仓条件",
        "min_order_notional_usd": "开仓条件",
        "take_fraction": "开仓条件",
        "close_band_bps": "平仓条件",
        "cooldown_seconds": "平仓后怎么办",
        "settle_grace_seconds": "保护",
        "max_slippage_bps": "保护",
    },
    "hedged_grid": {
        "grid_venue": "交易对象", "hedge_venue": "交易对象",
        "symbol": "交易对象", "lower_price": "交易对象",
        "upper_price": "交易对象", "grid_count": "交易对象",
        "qty_per_grid": "交易对象", "mode": "交易对象",
        "leverage_grid": "交易对象", "leverage_hedge": "交易对象",
        "skip_band_frac": "开仓条件",
        "out_of_range_action": "平仓条件",
        "hedge_deadband_qty": "保护", "max_unhedged_seconds": "保护",
        "max_residual_seconds": "保护", "fee_bps_per_side": "保护",
        "takeover_position": "保护",
    },
}

# 字段分区（第十一轮定版，与 SECTION_MAP 并列的单一事实源）：
# 面板按 zone 组织字段——legs=选择两腿、position=仓位设置、
# config=策略配置（正面）、more=更多配置（折叠）、hidden=系统值
# （不进面板输出）。zone=hidden 的字段即 SYSTEM_KEYS：validate_params
# 容忍并直接丢弃（系统值/旧任务文件兼容），不报「未知参数」。
ZONE_MAP: dict[str, dict[str, str]] = {
    "hedged_grid": {
        "grid_venue": "legs", "hedge_venue": "legs",
        "symbol": "position", "leverage_grid": "position",
        "leverage_hedge": "position",
        "lower_price": "config", "upper_price": "config",
        "grid_count": "config", "qty_per_grid": "config",
        "mode": "config", "out_of_range_action": "config",
        "hedge_deadband_qty": "more", "max_unhedged_seconds": "more",
        "max_residual_seconds": "more", "takeover_position": "more",
        "skip_band_frac": "hidden", "fee_bps_per_side": "hidden",
    },
    "premium_arb": {
        "base_venue": "legs", "hedge_venue": "legs",
        "symbol": "position", "max_position_usd": "position",
        "leverage_base": "position", "leverage_hedge": "position",
        "execution_mode": "config", "close_band_bps": "config",
        "midline_bps": "config", "upper_bps": "config",
        "lower_bps": "config",
        "persist_seconds": "more", "cooldown_seconds": "more",
        "inventory_scale_bps": "more", "inventory_floor_frac": "more",
        "min_order_notional_usd": "more",
        "settle_grace_seconds": "more", "max_slippage_bps": "more",
        "maker_offset_ticks": "more", "maker_timeout_seconds": "more",
        "max_order_notional_usd": "more", "order_cap_pct": "more",
        "take_fraction": "more",
        "reconcile_seconds": "hidden", "base_taker_fee_bps": "hidden",
        "hedge_taker_fee_bps": "hidden",
    },
    "funding_carry": {
        "venue_a": "legs", "venue_b": "legs",
        "symbol": "position", "notional_usd": "position",
        "leverage": "position",
        "max_open_spread_bps": "config",
        "min_edge_bps_per_hour": "config",
        "close_corridor_pct": "config", "qty_dev_close_pct": "config",
        "reopen": "config",
        "max_slippage_bps": "more",
        "exit_confirm_checks": "hidden", "check_seconds": "hidden",
        "cooldown_seconds": "hidden", "settle_grace_seconds": "hidden",
    },
}

# 系统值键（= 各策略 zone=hidden 的字段）：校验时容忍并丢弃
SYSTEM_KEYS: dict[str, frozenset] = {
    stype: frozenset(name for name, zone in zones.items()
                     if zone == "hidden")
    for stype, zones in ZONE_MAP.items()
}


# 内置中性档（第十二轮定版，取代原三档预设）：预设已全砍，
# 不再有档位概念。自动选标类模式（固腿优标 / 优腿优标）组装
# 内层参数时，用户没填的字段按这里的中性值补（优先级：用户
# 实填 > 本表 > Params dataclass 默认）。数值 = 原 PRESETS
# 各策略 neutral 档原值（tests 锁死与 Params 默认一致，防漂移）。
NEUTRAL_DEFAULTS: dict[str, dict] = {
    "hedged_grid": {"grid_count": 20,
                    "leverage_grid": 3.0, "leverage_hedge": 3.0,
                    "skip_band_frac": 0.25, "mode": "long_short"},
    "premium_arb": {"leverage_base": 1.0, "leverage_hedge": 1.0,
                    "execution_mode": "taker"},
    "funding_carry": {"leverage": 6.0,
                      "min_edge_bps_per_hour": 1.0},
}


# 策略页（左侧边栏布局）元数据：每策略一页，页面由通用渲染器按
# 本表生成。venue_fields/venue_labels 是「选择两腿」区的两腿场所
# 下拉；primary 是常用字段集合（弹窗/回退渲染用）；opportunity
# 决定 pairs 排序与总览最佳机会的口径；amount 是被仓位设置
# 「保证金」输入框取代的参数字段名（资金费的 notional_usd：
# 页面不单独渲染它、由保证金换算；其余策略为 None——第十二轮
# 起金额统一走顶层 amount_usd 保证金语义）；derived_fields 是
# 组装时派生的字段（面板不渲染输入框，见 core.engine 的派生
# 规则）；market_value 是币种下拉里每个币后面带的机会值类型；
# how_it_works 是页头「运作方式」折叠里的步骤短句（按各策略
# 真实机制写）；name_prefix 是面板一键运行的自动任务名前缀。
PAGE_META: dict[str, dict] = {
    "hedged_grid": {
        "venue_fields": ("grid_venue", "hedge_venue"),
        "venue_labels": ("网格腿", "对冲腿"),
        "opportunity": "grid",
        "amount": None,
        "amount_desc": None,
        "market_value": "price",
        "name_prefix": "grid",
        "run_modes": [{"value": "manual", "label": "自主"}],
        "how_it_works": [
            "网格腿在区间内按等距格子铺单，成交后在相邻格补反向单，吃震荡价差。",
            "网格腿每成交一格，对冲腿就在另一所下反向单，把两腿净仓位拉平。",
            "净仓位残差小于对冲死区时不动；裸露超过设定秒数会强制对冲。",
            "价格冲出区间时按设置处置：平双腿停机，或只减仓回收。",
        ],
        "primary": ("symbol", "lower_price", "upper_price", "grid_count",
                    "qty_per_grid", "leverage_grid", "leverage_hedge"),
        "section_summaries": {
            "开仓条件": "价格在区间内就按 {grid_count} 格铺格开仓；离现价太近的格子按系统设定的跳档比例跳过，先不挂单防噪声磨损。",
            "平仓条件": "价格冲出网格区间时按 {out_of_range_action} 处置：平掉双腿停机，或只减仓回收。",
            "保护": "两腿仓位差小于 {hedge_deadband_qty} 时先不补对冲；裸露超过 {max_unhedged_seconds} 秒、残差超过 {max_residual_seconds} 秒会强制对冲；启动时按系统费率估算校验每格利润，接管已有持仓的开关是 {takeover_position}。",
        },
        "note": "区间上下限可用仓位设置下方的建议区间（近 24 小时"
                "高低各扩 2%）一键填入；仓位设置里的金额是保证金，"
                "网格总名义 = 保证金 × 网格腿杠杆，用作网格资金上限；"
                "杠杆只用于校验与显示，须与账户里实际设置的一致"
                "（程序不自动改杠杆）。",
    },
    "premium_arb": {
        "venue_fields": ("base_venue", "hedge_venue"),
        "venue_labels": ("基准腿", "对冲腿"),
        "opportunity": "spread",
        "amount": None,
        "amount_desc": None,
        "derived_fields": ("max_order_notional_usd",),
        "market_value": "spread_bps",
        "name_prefix": "arb",
        "run_modes": [{"value": "semi_fixed", "label": "固腿固标"},
                      {"value": "semi_auto", "label": "固腿优标"},
                      {"value": "full_auto", "label": "优腿优标"}],
        "how_it_works": [
            "持续比较基准腿与对冲腿的价格，价差换算成相对中枢的溢价（bps）。",
            "溢价偏离中枢超过入场带、并持续确认一段时间后，便宜的一腿买、贵的一腿卖，等量对锁。",
            "溢价回归中枢附近即双腿平仓，赚价差回归；平仓恒走 taker 立即成交。",
            "主腿可选 maker 先挂单（成交后对冲腿跟上）或 taker 双腿立即吃单。",
        ],
        "primary": ("symbol", "max_position_usd", "execution_mode",
                    "midline_bps", "upper_bps", "lower_bps",
                    "leverage_base", "leverage_hedge"),
        "section_summaries": {
            "开仓条件": "价差偏离中枢 {midline_bps} 超过上方 {upper_bps} 或下方 {lower_bps} bps、并持续 {persist_seconds} 秒才开仓；执行方式是 {execution_mode}，挂单偏移 {maker_offset_ticks} 个跳动、最多等 {maker_timeout_seconds} 秒。",
            "平仓条件": "价差回归到中枢上下 {close_band_bps} bps 内就双腿全平（0=关闭回归平仓，靠反向越带信号平仓）。",
            "平仓后怎么办": "平仓后先冷却 {cooldown_seconds} 秒，才允许再开下一笔。",
            "保护": "对账发现两腿不平时自动削腿修平；开仓后 {settle_grace_seconds} 秒内先等结算不削腿，削腿最多偏离现价 {max_slippage_bps} bps。",
        },
        "note": "仓位设置里的金额是保证金：仓位名义 = 保证金 × "
                "基准腿杠杆，对冲腿开相同名义；实际仓位以最大持仓"
                "为上限（名义超过最大持仓时预览会警告）。单笔上限"
                "不用填，在「更多配置」里按最大持仓的百分比定"
                "（单笔上限占最大持仓 %，组装时自动换算）。两腿"
                "费率不用填：全所统一内置默认（taker 5 / maker 2 "
                "bps）。溢价带（中枢/上带/下带）必须先实测再填："
                "用录制器采集盘口后跑 analyze 得建议值（见使用说明"
                "第 6 节），不要凭感觉填。执行模式 maker = 主腿先"
                "挂单、成交后对冲腿跟上；taker = 双腿立即吃单；"
                "平仓恒为 taker。",
    },
    "funding_carry": {
        "venue_fields": ("venue_a", "venue_b"),
        "venue_labels": ("A腿", "B腿"),
        "opportunity": "funding",
        "amount": "notional_usd",
        "amount_desc": "自由填写（USD，每边）",
        "market_value": "funding_apr",
        "name_prefix": "carry",
        "run_modes": [{"value": "semi_fixed", "label": "固腿固标"},
                      {"value": "semi_auto", "label": "固腿优标"},
                      {"value": "full_auto", "label": "优腿优标"}],
        "how_it_works": [
            "比较两所同一币的资金费率，方向自动定：多费率低的一所、空费率高的一所。",
            "净费差（空腿费率减多腿费率）达到最低资金费率差、且开仓时两所价差不超过最大开仓价差，才双腿等额开仓。",
            "持有期间复核费差，跌破最低资金费率差（与入场同一条线）并连续确认多次后平仓；任一腿距强平过近或两腿数量偏差过大也双腿平仓。",
            "开启自动重开时，平仓后费差恢复到最低资金费率差之上会自动再开仓。",
        ],
        "primary": ("symbol", "notional_usd", "leverage",
                    "min_edge_bps_per_hour"),
        "section_summaries": {
            "开仓条件": "两所价差不超过 {max_open_spread_bps} bps、且费差达到 {min_edge_bps_per_hour} bps/小时就开仓，方向自动：多费率低的一所、空费率高的。",
            "平仓条件": "费差跌破 {min_edge_bps_per_hour} bps/小时（与入场同一条线）并连续确认后平仓；任一腿距强平近于 {close_corridor_pct}% 或两腿数量偏差超过 {qty_dev_close_pct}%（按较大腿算，0=关）也双腿平仓。",
            "平仓后怎么办": "自动重开是 {reopen} 时，费差再达标会自动再开一笔。",
            "保护": "两腿数量不平时先削腿修平，削腿最多偏离现价 {max_slippage_bps} bps，超了不下单。",
        },
        "note": "方向由策略按资金费自动选（多费率低的一所、空费率"
                "高的一所）；仓位设置里的金额是保证金：仓位名义 = "
                "保证金 × 杠杆，两腿开相同名义；最低资金费率差是"
                "进出共用的一条线：达到才开仓，跌破并连续确认后"
                "平仓。",
    },
}


def _infer_type(f: "dataclasses.Field", meta: dict) -> str:
    if "type" in meta:
        return meta["type"]
    t = f.type
    if t is bool or t == "bool":
        return "bool"
    if t is int or t == "int":
        return "int"
    if t is float or t == "float":
        return "float"
    return "str"


def strategy_specs() -> list[dict]:
    """三策略的参数规格（JSON 可序列化）：面板表单与校验的共同来源。

    zone=hidden 的系统值字段不出现在 fields 输出里（见 ZONE_MAP）。
    """
    out = []
    for stype, cls in PARAMS_CLASSES.items():
        meta_all = _FIELD_META.get(stype, {})
        page = PAGE_META[stype]
        primary = set(page["primary"])
        fields = []
        for f in dataclasses.fields(cls):
            zone = ZONE_MAP.get(stype, {}).get(f.name)
            if zone == "hidden":
                continue   # 系统值：不进面板字段输出
            meta = meta_all.get(f.name, {})
            required = f.default is dataclasses.MISSING and \
                f.default_factory is dataclasses.MISSING
            label, desc = _split_label(meta.get("label", f.name))
            item = {
                "name": f.name,
                "label": label,
                "desc": desc,
                "type": _infer_type(f, meta),
                "required": required,
                "default": None if required else f.default,
                "group": meta.get("group", "参数"),
                "section": SECTION_MAP.get(stype, {}).get(f.name),
                "zone": zone,
                "primary": f.name in primary,
                # 自动化配置子区内的语义分组（开仓/平仓/保护/
                # 其他）；primary 与两腿字段不进该子区，为 None
                "adv_group": ADV_GROUPS.get(stype, {}).get(f.name),
            }
            if "help" in meta:
                item["help"] = meta["help"]
            if item["type"] == "enum":
                item["options"] = [
                    {"value": v, "label": lbl}
                    for v, lbl in meta.get("options", [])]
            fields.append(item)
        out.append({
            "strategy": stype,
            "title": STRATEGY_TITLES[stype],
            "blurb": STRATEGY_BLURBS[stype],
            "fields": fields,
            "page": {
                "venue_fields": list(page["venue_fields"]),
                "venue_labels": list(page["venue_labels"]),
                "opportunity": page["opportunity"],
                "amount": page["amount"],
                "amount_desc": page.get("amount_desc"),
                "market_value": page["market_value"],
                "name_prefix": page["name_prefix"],
                "run_modes": [dict(m) for m in page.get("run_modes", [])],
                "how_it_works": list(page["how_it_works"]),
                "primary": list(page["primary"]),
                "note": page["note"],
                "section_summaries": dict(page.get("section_summaries", {})),
                "neutral_defaults": dict(NEUTRAL_DEFAULTS.get(stype, {})),
                "derived_fields": list(page.get("derived_fields", ())),
            },
        })
    return out


def spec_for(strategy: str) -> dict:
    for s in strategy_specs():
        if s["strategy"] == strategy:
            return s
    raise ValueError(
        f"未知策略类型：{strategy}，可用策略："
        f"{' / '.join(PARAMS_CLASSES)}")


def validate_params(strategy: str, raw: dict) -> dict:
    """按规格校验并归一化参数（面板与 config 组装共用）。

    - 未知字段、缺必填、类型不符、枚举/场所非法 → ValueError，
      消息逐项说清是哪个字段、什么问题；
    - 系统值键（SYSTEM_KEYS，zone=hidden）容忍并直接丢弃：它们是
      程序内定值或旧任务文件遗留键，不进归一化结果、不报错；
    - 数值字符串不接受（前端表单发 JSON 数字），bool 不当 int 用；
    - 返回只含已提供字段的归一化 dict（缺省字段由 Params 默认值补）。
    """
    spec = spec_for(strategy)
    if not isinstance(raw, dict):
        raise ValueError("params 必须是对象（字段名 → 值）")
    raw = {k: v for k, v in raw.items()
           if k not in SYSTEM_KEYS.get(strategy, frozenset())}
    by_name = {f["name"]: f for f in spec["fields"]}
    unknown = set(raw) - set(by_name)
    if unknown:
        raise ValueError(f"{strategy} 含未知参数：{sorted(unknown)}")
    out: dict = {}
    for name, val in raw.items():
        f = by_name[name]
        t = f["type"]
        if t == "bool":
            if not isinstance(val, bool):
                raise ValueError(f"参数 {name} 必须是开关（true/false）")
            out[name] = val
        elif t == "int":
            if isinstance(val, bool) or not isinstance(val, (int, float)) \
                    or int(val) != val:
                raise ValueError(f"参数 {name} 必须是整数")
            out[name] = int(val)
        elif t == "float":
            if isinstance(val, bool) or not isinstance(val, (int, float)):
                raise ValueError(f"参数 {name} 必须是数字")
            out[name] = float(val)
        elif t == "enum":
            vals = [o["value"] for o in f.get("options", [])]
            if val not in vals:
                raise ValueError(f"参数 {name} 必须是 {' / '.join(vals)} 之一")
            out[name] = val
        elif t == "venue":
            if val not in VENUE_IDS:
                raise ValueError(
                    f"参数 {name} 不是合法场所：{val}，合法值：{VENUE_IDS}")
            out[name] = val
        else:  # str
            if not isinstance(val, str) or not val.strip():
                raise ValueError(f"参数 {name} 必须是非空文本")
            out[name] = val.strip()
    missing = [f["name"] for f in spec["fields"]
               if f["required"] and f["name"] not in out]
    if missing:
        raise ValueError(f"{strategy} 缺少必填参数：{missing}")
    return out
