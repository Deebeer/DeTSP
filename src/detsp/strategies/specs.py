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
    "funding_carry": "资金费 carry",
}

STRATEGY_BLURBS = {
    "hedged_grid": "网格腿铺单吃震荡，对冲腿在另一所把净仓位调平，不赌方向。",
    "rotation_hedge": "两所一多一空对锁，按资金费选向、持有到期轮换，刷量兼吃费差。",
    "premium_arb": "盯两所价差，偏离中枢开仓、回归平仓，赚价差回归的钱。",
    "funding_carry": "两所一多一空长期持有，专门收两所资金费率的差额。",
}

# 字段展示元数据：label（中文说明）/ group（分组）/ type 覆盖 /
# options（枚举）。不在此列的字段按 dataclass 类型推断。
_VENUE = {"type": "venue"}
_FIELD_META: dict[str, dict[str, dict]] = {
    "hedged_grid": {
        "grid_venue": {"label": "网格腿场所", "group": "场所与标的", **_VENUE},
        "hedge_venue": {"label": "对冲腿场所", "group": "场所与标的", **_VENUE},
        "symbol": {"label": "币种（如 BTC）", "group": "场所与标的"},
        "lower_price": {"label": "网格区间下限", "group": "网格参数"},
        "upper_price": {"label": "网格区间上限", "group": "网格参数"},
        "grid_count": {"label": "格数（至少 2）", "group": "网格参数"},
        "qty_per_grid": {"label": "每格数量（币）", "group": "网格参数"},
        "mode": {"label": "方向模式", "group": "网格参数", "type": "enum",
                 "options": [("long", "只做多"), ("short", "只做空"),
                             ("long_short", "双向")]},
        "out_of_range_action": {
            "label": "冲出区间后的动作", "group": "网格参数", "type": "enum",
            "options": [("close", "平双腿并停机"),
                        ("reduce_only", "只减仓回收")]},
        "leverage_grid": {"label": "网格腿杠杆（与账户实际一致）",
                          "group": "对冲与风控"},
        "leverage_hedge": {"label": "对冲腿杠杆（与账户实际一致）",
                           "group": "对冲与风控"},
        "hedge_deadband_qty": {"label": "对冲死区（币，残差小于它不对冲）",
                               "group": "对冲与风控"},
        "max_unhedged_seconds": {"label": "裸露超时强制对冲（秒，0=禁用）",
                                 "group": "对冲与风控"},
        "max_residual_seconds": {"label": "小残差强制对冲时限（秒，0=禁用）",
                                 "group": "对冲与风控"},
        "skip_band_frac": {"label": "贴价跳档带宽（格距比例）",
                           "group": "对冲与风控"},
        "fee_bps_per_side": {"label": "单边手续费估算 bps（经济性检查用）",
                             "group": "对冲与风控"},
        "takeover_position": {"label": "启动时接管网格腿已有持仓",
                              "group": "对冲与风控"},
    },
    "rotation_hedge": {
        "symbol": {"label": "币种（如 BTC）", "group": "场所与标的"},
        "venue_a": {"label": "场所 A", "group": "场所与标的", **_VENUE},
        "venue_b": {"label": "场所 B", "group": "场所与标的", **_VENUE},
        "notional_usd": {"label": "单边开仓金额（USD）", "group": "场所与标的"},
        "hold_seconds": {"label": "固定持有时长（秒）", "group": "轮换节奏"},
        "hold_min_seconds": {"label": "持有时长下限（秒，>0 时与上限间随机）",
                             "group": "轮换节奏"},
        "hold_max_seconds": {"label": "持有时长上限（秒）", "group": "轮换节奏"},
        "cooldown_seconds": {"label": "平仓后冷却（秒）", "group": "轮换节奏"},
        "notional_min_usd": {"label": "金额下限（USD，>0 时与上限间随机）",
                             "group": "轮换节奏"},
        "notional_max_usd": {"label": "金额上限（USD）", "group": "轮换节奏"},
        "max_position_qty": {"label": "逐步建仓上限（币，0=禁用）",
                             "group": "轮换节奏"},
        "accumulate_interval_seconds": {"label": "逐步建仓加仓间隔（秒）",
                                        "group": "轮换节奏"},
        "arcus_maker": {"label": "Arcus 腿走 maker 挂单开仓", "group": "执行与风控"},
        "maker_wait_seconds": {"label": "maker 挂单最长等待（秒）",
                               "group": "执行与风控"},
        "leverage_a": {"label": "A 腿杠杆（与账户实际一致）", "group": "执行与风控"},
        "leverage_b": {"label": "B 腿杠杆（与账户实际一致）", "group": "执行与风控"},
        "settle_grace_seconds": {"label": "开仓结算宽限期（秒）",
                                 "group": "执行与风控"},
        "max_slippage_bps": {"label": "削腿滑点保护上限（bps）",
                             "group": "执行与风控"},
    },
    "premium_arb": {
        "base_venue": {"label": "基准腿场所", "group": "场所与标的", **_VENUE},
        "hedge_venue": {"label": "对冲腿场所", "group": "场所与标的", **_VENUE},
        "symbol": {"label": "币种（如 BTC）", "group": "场所与标的"},
        "midline_bps": {"label": "溢价中枢（bps，必须实测）", "group": "溢价带"},
        "upper_bps": {"label": "中枢上方入场带（bps）", "group": "溢价带"},
        "lower_bps": {"label": "中枢下方入场带（bps）", "group": "溢价带"},
        "persist_seconds": {"label": "信号持续确认（秒）", "group": "溢价带"},
        "max_position_usd": {"label": "最大持仓名义（USD）", "group": "风控"},
        "max_order_notional_usd": {"label": "单笔名义上限（USD）", "group": "风控"},
        "take_fraction": {"label": "吃对手盘深度比例（0~1）", "group": "风控"},
        "base_taker_fee_bps": {"label": "基准腿 taker 费率（bps）", "group": "执行"},
        "hedge_taker_fee_bps": {"label": "对冲腿 taker 费率（bps）", "group": "执行"},
        "execution_mode": {"label": "执行模式", "group": "执行", "type": "enum",
                           "options": [("taker", "吃单（双腿立即成交）"),
                                       ("maker", "挂单（主腿先挂单等成交）")]},
        "maker_offset_ticks": {"label": "maker 挂价偏移（tick）", "group": "执行"},
        "maker_timeout_seconds": {"label": "maker 挂单最长等待（秒）", "group": "执行"},
        "reconcile_seconds": {"label": "持仓对账周期（秒）", "group": "执行"},
        "leverage_base": {"label": "基准腿杠杆（与账户实际一致）", "group": "风控"},
        "leverage_hedge": {"label": "对冲腿杠杆（与账户实际一致）", "group": "风控"},
        "settle_grace_seconds": {"label": "开仓结算宽限期（秒）", "group": "执行"},
        "max_slippage_bps": {"label": "削腿滑点保护上限（bps）", "group": "执行"},
    },
    "funding_carry": {
        "venue_a": {"label": "场所 A", "group": "场所与标的", **_VENUE},
        "venue_b": {"label": "场所 B", "group": "场所与标的", **_VENUE},
        "symbol": {"label": "币种（如 BTC）", "group": "场所与标的"},
        "notional_usd": {"label": "单边开仓金额（USD）", "group": "场所与标的"},
        "leverage": {"label": "两腿杠杆（与账户实际一致）", "group": "场所与标的"},
        "min_entry_edge_bps_per_hour": {"label": "入场门槛：净收资金费优势（bps/小时）",
                                        "group": "入场与退出"},
        "exit_edge_bps_per_hour": {"label": "退出门槛（bps/小时，须小于入场门槛）",
                                   "group": "入场与退出"},
        "exit_confirm_checks": {"label": "退出确认次数（连续低于退出门槛）",
                                "group": "入场与退出"},
        "check_seconds": {"label": "资金费检查周期（秒）", "group": "入场与退出"},
        "max_hold_seconds": {"label": "最长持有（秒，0=不限）", "group": "入场与退出"},
        "reopen": {"label": "平仓后优势恢复自动重开", "group": "入场与退出"},
        "cooldown_seconds": {"label": "平仓后重开冷却（秒）", "group": "入场与退出"},
        "settle_grace_seconds": {"label": "开仓结算宽限期（秒）",
                                 "group": "执行与风控"},
        "max_slippage_bps": {"label": "削腿滑点保护上限（bps）",
                             "group": "执行与风控"},
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
        fields = []
        for f in dataclasses.fields(cls):
            meta = meta_all.get(f.name, {})
            required = f.default is dataclasses.MISSING and \
                f.default_factory is dataclasses.MISSING
            item = {
                "name": f.name,
                "label": meta.get("label", f.name),
                "type": _infer_type(f, meta),
                "required": required,
                "default": None if required else f.default,
                "group": meta.get("group", "参数"),
            }
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
