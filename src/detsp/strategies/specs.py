"""策略参数规格（单一事实源）。

四个策略的 Params dataclass 是字段与默认值的唯一定义；本模块在其
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
from .rotation_hedge import RotationParams

PARAMS_CLASSES = {
    "hedged_grid": HedgedGridParams,
    "rotation_hedge": RotationParams,
    "premium_arb": PremiumParams,
    "funding_carry": CarryParams,
}

STRATEGY_TITLES = {
    "hedged_grid": "中性网格",
    "rotation_hedge": "轮换对冲刷量",
    "premium_arb": "溢价套利",
    "funding_carry": "资金费套利",
}

STRATEGY_BLURBS = {
    "hedged_grid": "网格腿铺单吃震荡，对冲腿在另一所把净仓位调平，不赌方向。",
    "rotation_hedge": "两所一多一空对锁，按资金费选向、持有到期轮换，刷量兼吃费差。",
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
    "rotation_hedge": {
        "symbol": {"label": "币种（要交易的标的）", "group": "场所与标的"},
        "venue_a": {"label": "场所 A（A 腿用的交易所）",
                    "group": "场所与标的", **_VENUE},
        "venue_b": {"label": "场所 B（B 腿用的交易所）",
                    "group": "场所与标的", **_VENUE},
        "notional_usd": {"label": "单边开仓金额（一边开多少美元）",
                         "group": "场所与标的"},
        "hold_seconds": {"label": "固定持有时长（关掉随机时长时，每轮开仓后持有这么多秒就自动平仓）",
                         "group": "轮换节奏"},
        "hold_min_seconds": {"label": "持有时长下限（每轮随机时长的最短，单位秒）",
                             "group": "轮换节奏"},
        "hold_max_seconds": {"label": "持有时长上限（每轮随机时长的最长，单位秒）",
                             "group": "轮换节奏"},
        "cooldown_seconds": {"label": "冷却（一轮平仓后，等这么多秒才开下一轮）",
                             "group": "轮换节奏"},
        "notional_min_usd": {"label": "随机金额下限（金额随机模式开时，每轮金额的下限，与上限都填才生效）",
                             "group": "轮换节奏",
                             "help": "与原仓差异：Parallax 默认金额留空="
                                     "按两所可用容量开满（安全系数 0.95）；"
                                     "DeTSP 任务必须给固定 notional_usd，"
                                     "不做按容量开满（安全差异）。"},
        "notional_max_usd": {"label": "随机金额上限（金额随机模式开时，每轮金额的上限）",
                             "group": "轮换节奏"},
        "max_position_qty": {"label": "逐步建仓上限（分批建仓时最多加到这么多币，0=一次开满不分批）",
                             "group": "轮换节奏"},
        "accumulate_interval_seconds": {"label": "加仓间隔（分批建仓时，每加一批仓位等这么多秒）",
                                        "group": "轮换节奏"},
        "arcus_maker": {"label": "Arcus 挂单开仓（Arcus 腿用挂单等成交开仓，手续费低；关则市价吃单）",
                        "group": "执行与风控"},
        "maker_wait_seconds": {"label": "挂单等待（Arcus 挂单等这么多秒还没成交，机器人就撤掉这张单）",
                               "group": "执行与风控"},
        "leverage_a": {"label": "A 腿杠杆（填账户里实际设的杠杆，只对账校验，程序不自动改）",
                       "group": "执行与风控"},
        "leverage_b": {"label": "B 腿杠杆（填账户里实际设的杠杆，只对账校验，程序不自动改）",
                       "group": "执行与风控"},
        "settle_grace_seconds": {"label": "结算宽限（开仓后的这段秒数内先不修仓，等交易所把成交结算完，防误判）",
                                 "group": "执行与风控"},
        "max_slippage_bps": {"label": "削腿滑点上限（两腿数量对不平时修仓，限价最多偏离现价这么多 bps，超了不下单）",
                             "group": "执行与风控",
                             "help": "与原仓差异：Parallax 下单滑点上限"
                                     " ORDER_SLIPPAGE_BPS=10；本字段只管"
                                     "双腿不平时的削腿修正单，保留 20 "
                                     "给薄盘口成交余量（安全差异）。"},
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
        "max_position_usd": {"label": "最大持仓（两腿最多持有多少美元）",
                             "group": "风控"},
        "max_order_notional_usd": {"label": "单笔上限（一笔订单最多下这么多美元，更大的量会拆成多笔下）",
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
        "notional_usd": {"label": "单边开仓金额（一边开多少美元）",
                         "group": "场所与标的"},
        "leverage": {"label": "两腿杠杆（填账户里实际设的杠杆，只对账校验，程序不自动改）",
                     "group": "场所与标的"},
        "min_entry_edge_bps_per_hour": {"label": "入场门槛（费差达到这个数才开仓，单位 bps/小时）",
                                        "group": "入场与退出"},
        "exit_edge_bps_per_hour": {"label": "退出门槛（费差跌破这个数并多次确认后平仓，单位 bps/小时）",
                                   "group": "入场与退出"},
        "exit_confirm_checks": {"label": "退出确认次数（费差连续这么多次检查都低于退出门槛，机器人才平仓）",
                                "group": "入场与退出"},
        "check_seconds": {"label": "检查周期（每隔这么多秒查一次两所资金费，判断该开仓还是平仓）",
                          "group": "入场与退出"},
        "max_hold_seconds": {"label": "最长持有（持仓超过这么多秒就强制平仓，0=不限）",
                             "group": "入场与退出"},
        "reopen": {"label": "自动重开（平仓后费差又达标时，机器人自动重新开仓）",
                   "group": "入场与退出"},
        "cooldown_seconds": {"label": "重开冷却（平仓后等这么多秒，才允许重新开仓）",
                             "group": "入场与退出"},
        "settle_grace_seconds": {"label": "结算宽限（开仓后的这段秒数内先不修仓，等交易所把成交结算完，防误判）",
                                 "group": "执行与风控"},
        "max_slippage_bps": {"label": "削腿滑点上限（两腿数量对不平时修仓，限价最多偏离现价这么多 bps，超了不下单）",
                             "group": "执行与风控",
                             "help": "与原仓差异：Parallax 下单滑点上限"
                                     "为 10 bps；本字段只管削腿修正单，"
                                     "保留 20 给薄盘口成交余量（安全差异）。"},
    },
}

# 高级选项分组（面板「高级选项」折叠区内的小分组标题）：四策略
# 都是全自动运行，高级字段是机器人自动开/平仓行为的旋钮，按
# 语义分 开仓 / 平仓 / 保护 / 其他 四组。只有非 primary、非两腿
# 场所字段（即进高级选项的那些）需要登记；未登记者归「其他」。
ADV_GROUPS: dict[str, dict[str, str]] = {
    "hedged_grid": {
        "mode": "开仓", "skip_band_frac": "开仓",
        "out_of_range_action": "平仓",
        "hedge_deadband_qty": "保护", "max_unhedged_seconds": "保护",
        "max_residual_seconds": "保护", "fee_bps_per_side": "保护",
        "takeover_position": "其他",
    },
    "rotation_hedge": {
        "cooldown_seconds": "开仓", "notional_min_usd": "开仓",
        "notional_max_usd": "开仓", "max_position_qty": "开仓",
        "accumulate_interval_seconds": "开仓", "arcus_maker": "开仓",
        "maker_wait_seconds": "开仓",
        "hold_seconds": "平仓",
        "settle_grace_seconds": "保护", "max_slippage_bps": "保护",
    },
    "premium_arb": {
        "persist_seconds": "开仓", "cooldown_seconds": "开仓",
        "max_order_notional_usd": "开仓",
        "min_order_notional_usd": "开仓",
        "inventory_scale_bps": "开仓", "inventory_floor_frac": "开仓",
        "take_fraction": "开仓", "maker_offset_ticks": "开仓",
        "maker_timeout_seconds": "开仓",
        "reconcile_seconds": "保护", "settle_grace_seconds": "保护",
        "max_slippage_bps": "保护",
        "base_taker_fee_bps": "其他", "hedge_taker_fee_bps": "其他",
    },
    "funding_carry": {
        "reopen": "开仓", "cooldown_seconds": "开仓",
        "exit_confirm_checks": "平仓", "max_hold_seconds": "平仓",
        "settle_grace_seconds": "保护", "max_slippage_bps": "保护",
        "check_seconds": "其他",
    },
}


# 配置预设（页面「配置」区的档位按钮）：保守 / 中性 / 激进 三组
# 字段值，点一下整组填入表单；用户再改任何字段即变「自定义」。
# 用户定版语义（第五轮修正）：预设只套**结构参数**（杠杆、门槛、
# 格数、持有时长、冷却、执行方式等），**绝不含金额类字段**——
# 金额由用户先在「金额」区填好，点预设时金额输入框一个字不动。
# 中性 = 现行原仓对齐默认值：凡结构字段在 Params 里有默认值，
# 中性档的值必须与默认值相等（tests 锁死，防漂移）。金额本来
# 就不是原仓默认语义的一部分（原仓金额是表单必填项），故不锁。
# 原先靠金额分档的量改为比例换算（见 AMOUNT_RATIOS）：溢价套利
# 的单笔上限 = 用户填的最大持仓 × 档位比例，点预设时前端换算
# 填入。中性网格的每格量继续由用户填的目标总名义反推（点预设
# 只改格数/杠杆/跳档，总名义输入框不动）。
PRESETS: dict[str, dict[str, dict]] = {
    "hedged_grid": {
        "conservative": {"grid_count": 10,
                         "leverage_grid": 2.0, "leverage_hedge": 2.0,
                         "skip_band_frac": 0.35},
        "neutral": {"grid_count": 20,
                    "leverage_grid": 3.0, "leverage_hedge": 3.0,
                    "skip_band_frac": 0.25, "mode": "long_short"},
        "aggressive": {"grid_count": 40,
                       "leverage_grid": 5.0, "leverage_hedge": 5.0,
                       "skip_band_frac": 0.15},
    },
    "rotation_hedge": {
        "conservative": {"leverage_a": 3.0,
                         "leverage_b": 3.0, "hold_min_seconds": 3600.0,
                         "hold_max_seconds": 5400.0,
                         "cooldown_seconds": 60.0, "arcus_maker": True},
        "neutral": {"leverage_a": 6.0,
                    "leverage_b": 6.0, "hold_min_seconds": 3600.0,
                    "hold_max_seconds": 7200.0,
                    "cooldown_seconds": 30.0, "arcus_maker": True},
        "aggressive": {"leverage_a": 10.0,
                       "leverage_b": 10.0, "hold_min_seconds": 1800.0,
                       "hold_max_seconds": 3600.0,
                       "cooldown_seconds": 15.0, "arcus_maker": False},
    },
    "premium_arb": {
        "conservative": {"leverage_base": 1.0, "leverage_hedge": 1.0,
                         "execution_mode": "maker"},
        "neutral": {"leverage_base": 1.0, "leverage_hedge": 1.0,
                    "execution_mode": "taker"},
        "aggressive": {"leverage_base": 3.0, "leverage_hedge": 3.0,
                       "execution_mode": "taker"},
    },
    "funding_carry": {
        "conservative": {"leverage": 3.0,
                         "min_entry_edge_bps_per_hour": 2.0,
                         "exit_edge_bps_per_hour": 0.5,
                         "check_seconds": 30.0},
        "neutral": {"leverage": 6.0,
                    "min_entry_edge_bps_per_hour": 1.0,
                    "exit_edge_bps_per_hour": 0.0,
                    "check_seconds": 30.0},
        "aggressive": {"leverage": 10.0,
                       "min_entry_edge_bps_per_hour": 0.5,
                       "exit_edge_bps_per_hour": 0.0,
                       "check_seconds": 15.0},
    },
}

# 金额派生字段的档位比例：字段值 = 页面主金额（用户已填）× 比例。
# 只有溢价套利有这种字段：单笔上限原先按最大持仓分档写死绝对值
# （250/500/1500 对应最大持仓 500/1000/3000）；现按用户定版改
# 比例 25% / 50% / 100%——保守/中性与原档在参考金额下等值，
# 激进原为 50%（1500/3000），按定版语义改 100%（单笔可打满）。
AMOUNT_RATIOS: dict[str, dict[str, dict[str, float]]] = {
    "premium_arb": {
        "max_order_notional_usd": {"conservative": 0.25,
                                   "neutral": 0.5,
                                   "aggressive": 1.0},
    },
}

PRESET_LABELS = {"conservative": "保守", "neutral": "中性",
                 "aggressive": "激进", "custom": "自定义"}


# 策略页（左侧边栏布局）元数据：每策略一页，页面由通用渲染器按
# 本表生成。venue_fields/venue_labels 是「订单腿」区的两腿场所
# 下拉；primary 是「金额」区的常用字段（其余字段进折叠的「高级
# 选项」）；opportunity 决定 pairs 排序与总览最佳机会的口径；amount 是
# 页面顶部大输入框的主金额字段（中性网格无单一金额，为 None，
# 用每格量×格数×现价折算总名义）；market_value 是「选市场」下拉
# 里每个币后面带的机会值类型；how_it_works 是页头「运作方式」
# 折叠里的步骤短句（按各策略真实机制写）；name_prefix 是面板
# 一键运行的自动任务名前缀。
PAGE_META: dict[str, dict] = {
    "hedged_grid": {
        "venue_fields": ("grid_venue", "hedge_venue"),
        "venue_labels": ("网格腿", "对冲腿"),
        "opportunity": "grid",
        "amount": None,
        "amount_desc": None,
        "market_value": "price",
        "name_prefix": "grid",
        "how_it_works": [
            "网格腿在区间内按等距格子铺单，成交后在相邻格补反向单，吃震荡价差。",
            "网格腿每成交一格，对冲腿就在另一所下反向单，把两腿净仓位拉平。",
            "净仓位残差小于对冲死区时不动；裸露超过设定秒数会强制对冲。",
            "价格冲出区间时按设置处置：平双腿停机，或只减仓回收。",
        ],
        "primary": ("symbol", "lower_price", "upper_price", "grid_count",
                    "qty_per_grid", "leverage_grid", "leverage_hedge"),
        "note": "区间上下限可在「选市场」下方用建议区间（近 24 小时"
                "高低各扩 2%）一键填入；杠杆只用于校验与显示，须与"
                "账户里实际设置的一致（程序不自动改杠杆）。",
    },
    "rotation_hedge": {
        "venue_fields": ("venue_a", "venue_b"),
        "venue_labels": ("A腿", "B腿"),
        "opportunity": "funding",
        "amount": "notional_usd",
        "amount_desc": "自由填写（USD，每边）",
        "market_value": "funding_apr",
        "name_prefix": "rotation",
        "how_it_works": [
            "两所一多一空等额对锁，方向按当下资金费选：多费率低的一所、空费率高的一所。",
            "持有一段时长（默认每轮在 1~2 小时之间随机）刷成交量，顺带吃两所费差。",
            "到期双腿平仓，冷却后按最新资金费重新选向，再开下一轮。",
            "Arcus 腿可走 maker 挂单开仓，降低手续费（高级选项里开关）。",
        ],
        "primary": ("symbol", "notional_usd", "leverage_a", "leverage_b",
                    "hold_min_seconds", "hold_max_seconds"),
        "note": "方向由策略按资金费自动选（多费率低的一所、空费率"
                "高的一所）；持有时长默认按 Parallax 原仓表单在最短"
                " 1 小时 ~ 最长 2 小时之间每轮随机抽取，到点自动平仓"
                "轮换（最长 ≤ 最短或最短填 0 时关闭随机，改用高级"
                "选项里的固定 hold_seconds）。",
    },
    "premium_arb": {
        "venue_fields": ("base_venue", "hedge_venue"),
        "venue_labels": ("基准腿", "对冲腿"),
        "opportunity": "spread",
        "amount": "max_position_usd",
        "amount_desc": "自由填写（USD）",
        "market_value": "spread_bps",
        "name_prefix": "arb",
        "how_it_works": [
            "持续比较基准腿与对冲腿的价格，价差换算成相对中枢的溢价（bps）。",
            "溢价偏离中枢超过入场带、并持续确认一段时间后，便宜的一腿买、贵的一腿卖，等量对锁。",
            "溢价回归中枢附近即双腿平仓，赚价差回归；平仓恒走 taker 立即成交。",
            "主腿可选 maker 先挂单（成交后对冲腿跟上）或 taker 双腿立即吃单。",
        ],
        "primary": ("symbol", "max_position_usd", "execution_mode",
                    "midline_bps", "upper_bps", "lower_bps",
                    "leverage_base", "leverage_hedge"),
        "note": "顶部金额是最大持仓名义（不是单笔）；单笔上限在高级"
                "选项里，点预设时按最大持仓的比例换算（保守 25% / "
                "中性 50% / 激进 100%），改金额不会被预设覆盖。"
                "溢价带（中枢/上带/下带）必须先实测再填：用录制器"
                "采集盘口后跑 analyze 得建议值（见使用说明第 6 节），"
                "不要凭感觉填。执行模式 maker = 主腿先挂单、成交后"
                "对冲腿跟上；taker = 双腿立即吃单；平仓恒为 taker。",
    },
    "funding_carry": {
        "venue_fields": ("venue_a", "venue_b"),
        "venue_labels": ("A腿", "B腿"),
        "opportunity": "funding",
        "amount": "notional_usd",
        "amount_desc": "自由填写（USD，每边）",
        "market_value": "funding_apr",
        "name_prefix": "carry",
        "how_it_works": [
            "比较两所同一币的资金费率，方向自动定：多费率低的一所、空费率高的一所。",
            "净费差（空腿费率减多腿费率）达到入场门槛，才双腿等额开仓。",
            "持有期间按检查周期复核费差，跌破退出门槛并连续确认多次后平仓。",
            "开启自动重开时，平仓后费差恢复到门槛之上会自动再开仓。",
        ],
        "primary": ("symbol", "notional_usd", "leverage",
                    "min_entry_edge_bps_per_hour",
                    "exit_edge_bps_per_hour"),
        "note": "方向由策略按资金费自动选（多费率低的一所、空费率"
                "高的一所）；净费差达到入场门槛才开仓，跌破退出"
                "门槛并连续确认后平仓。",
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
    """四策略的参数规格（JSON 可序列化）：面板表单与校验的共同来源。"""
    out = []
    for stype, cls in PARAMS_CLASSES.items():
        meta_all = _FIELD_META.get(stype, {})
        page = PAGE_META[stype]
        primary = set(page["primary"])
        fields = []
        for f in dataclasses.fields(cls):
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
                "primary": f.name in primary,
                # 高级选项折叠区内的语义分组（开仓/平仓/保护/
                # 其他）；primary 与两腿字段不进高级区，为 None
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
                "how_it_works": list(page["how_it_works"]),
                "primary": list(page["primary"]),
                "note": page["note"],
                "presets": PRESETS[stype],
                "preset_labels": PRESET_LABELS,
                "amount_ratios": AMOUNT_RATIOS.get(stype, {}),
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
    - 数值字符串不接受（前端表单发 JSON 数字），bool 不当 int 用；
    - 返回只含已提供字段的归一化 dict（缺省字段由 Params 默认值补）。
    """
    spec = spec_for(strategy)
    if not isinstance(raw, dict):
        raise ValueError("params 必须是对象（字段名 → 值）")
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
