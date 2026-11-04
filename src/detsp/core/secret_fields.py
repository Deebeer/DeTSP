"""面板「设置」页的密钥字段目录与 .env 读写。

安全口径：
- 状态接口只回答「已填 / 未填」，**绝不返回任何密钥值**（连
  长度、掩码都不给，避免侧信道）；
- 保存时只接受目录内的字段名（白名单），空值 = 不修改，未知
  字段直接报错；
- 写 .env 时保留原文件的注释、空行与其他键，只替换目标键的
  值行；原文件没有的键追加到文件末尾。原子写（临时文件 +
  rename），避免写一半损坏密钥文件。

必填口径与 core.preflight.REQUIRED_SECRETS 一致：组内单字段 =
必填；组为「任选其一」（私钥值与私钥文件路径）时标 one_of。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from .config import load_env_file

# required: True=必填；"one_of"=与同组另一字段二选一必填；False=可选
SECRET_GROUPS: list[dict] = [
    {
        "key": "hyperliquid",
        "title": "Hyperliquid 家族（本体 / Entropy / trade.xyz 共用）",
        "fields": [
            {"name": "HL_ACCOUNT_ADDRESS", "label": "主账户地址（你的钱包地址）",
             "required": True, "secret": False},
            {"name": "HL_PRIVATE_KEY", "label": "API 钱包私钥（agent 钱包的私钥，不是主钱包的）",
             "required": True, "secret": True},
            {"name": "HL_ACCOUNT_ADDRESS_XYZ",
             "label": "trade.xyz 专用主账户地址（留空就与本体共用）",
             "required": False, "secret": False},
            {"name": "HL_PRIVATE_KEY_XYZ",
             "label": "trade.xyz 专用 API 钱包私钥（留空就共用）",
             "required": False, "secret": True},
        ],
    },
    {
        "key": "arcus",
        "title": "Arcus",
        "fields": [
            {"name": "ARCUS_ADDRESS", "label": "钱包地址（Arcus 账户的钱包地址）",
             "required": True, "secret": False},
            {"name": "ARCUS_ACCOUNT_INDEX", "label": "子账户序号（主账户一般是 0）",
             "required": True, "secret": False},
            {"name": "ARCUS_API_KEY", "label": "API 公钥（创建 API 时给的公钥）",
             "required": True, "secret": False},
            {"name": "ARCUS_API_PRIVATE_KEY", "label": "API 私钥（只显示一次，丢了要重建）",
             "required": "one_of", "secret": True},
            {"name": "ARCUS_API_PRIVATE_KEY_FILE",
             "label": "API 私钥文件路径（把私钥存成文件时填路径，与私钥二选一）",
             "required": "one_of", "secret": False},
        ],
    },
    {
        "key": "lighter",
        "title": "Lighter 主网",
        "fields": [
            {"name": "LIGHTER_ACCOUNT_INDEX", "label": "账户序号（平台给的账户编号）",
             "required": True, "secret": False},
            {"name": "LIGHTER_API_KEY_INDEX", "label": "API Key 序号（这把钥匙的编号）",
             "required": True, "secret": False},
            {"name": "LIGHTER_API_PRIVATE_KEY", "label": "API 私钥（只显示一次，丢了要重建）",
             "required": "one_of", "secret": True},
            {"name": "LIGHTER_API_PRIVATE_KEY_FILE",
             "label": "API 私钥文件路径（把私钥存成文件时填路径，与私钥二选一）",
             "required": "one_of", "secret": False},
        ],
    },
    {
        "key": "lighter_rh",
        "title": "Lighter Robinhood 链（与主网是独立账户）",
        "fields": [
            {"name": "LIGHTER_RH_ACCOUNT_INDEX", "label": "账户序号（平台给的账户编号）",
             "required": True, "secret": False},
            {"name": "LIGHTER_RH_API_KEY_INDEX", "label": "API Key 序号（这把钥匙的编号）",
             "required": True, "secret": False},
            {"name": "LIGHTER_RH_API_PRIVATE_KEY", "label": "API 私钥（只显示一次，丢了要重建）",
             "required": "one_of", "secret": True},
            {"name": "LIGHTER_RH_API_PRIVATE_KEY_FILE",
             "label": "API 私钥文件路径（把私钥存成文件时填路径，与私钥二选一）",
             "required": "one_of", "secret": False},
        ],
    },
    {
        "key": "aster",
        "title": "Aster",
        "fields": [
            {"name": "ASTER_API_KEY", "label": "API Key（创建 API 时给的钥匙编号串）",
             "required": True, "secret": False},
            {"name": "ASTER_SECRET_KEY", "label": "Secret Key（只显示一次的密钥串）",
             "required": True, "secret": True},
        ],
    },
]

ALL_FIELDS: dict[str, dict] = {
    f["name"]: f for g in SECRET_GROUPS for f in g["fields"]}


_LABEL_RE = re.compile(r"^(.*?)（(.*)）$")


def _split_label(raw: str) -> tuple[str, str]:
    """「字段名（简短说明）」拆成 (名, 说明)，面板按三段式呈现。"""
    m = _LABEL_RE.match(raw or "")
    if m:
        return m.group(1), m.group(2)
    return raw or "", ""


def secrets_status(env_path) -> list[dict]:
    """逐组逐字段的填写状态（只有已填/未填，绝不含值）。"""
    values = load_env_file(env_path)
    out = []
    for g in SECRET_GROUPS:
        fields = []
        for f in g["fields"]:
            label, desc = _split_label(f["label"])
            fields.append({
                "name": f["name"],
                "label": label,
                "desc": desc,
                "required": f["required"],
                "secret": bool(f["secret"]),
                "filled": bool(str(values.get(f["name"]) or "").strip()),
            })
        out.append({"key": g["key"], "title": g["title"], "fields": fields})
    return out


_KEY_LINE = re.compile(r"^(\s*)([A-Za-z_][A-Za-z0-9_]*)\s*=")


def save_secrets(env_path, updates: dict) -> list[str]:
    """把非空值写回 .env，返回实际写入的字段名列表。

    - updates 里出现目录外字段名 → ValueError（调用方回 400）；
    - 空/纯空白值视为「不修改」，跳过；
    - 值含换行 → ValueError（会破坏 .env 结构）；
    - 原文件里的注释、空行、其他键原样保留；目标键只改值行，
      文件里还没有的键追加到末尾；
    - 原子写：同目录临时文件 + os.replace。
    """
    unknown = [k for k in updates if k not in ALL_FIELDS]
    if unknown:
        raise ValueError(f"未知密钥字段：{', '.join(sorted(unknown))}")
    pending: dict[str, str] = {}
    for k, v in updates.items():
        val = str(v if v is not None else "").strip()
        if not val:
            continue
        if "\n" in val or "\r" in val:
            raise ValueError(f"字段 {k} 的值不能含换行")
        pending[k] = val
    if not pending:
        return []

    p = Path(env_path)
    lines = p.read_text(encoding="utf-8").splitlines() if p.exists() else []
    done: set[str] = set()
    out_lines: list[str] = []
    for line in lines:
        m = _KEY_LINE.match(line)
        if m and m.group(2) in pending:
            key = m.group(2)
            out_lines.append(f"{key}={pending[key]}")
            done.add(key)
        else:
            out_lines.append(line)
    rest = [k for k in pending if k not in done]
    if rest:
        if out_lines and out_lines[-1].strip():
            out_lines.append("")
        out_lines.append("# ===== 面板设置页补充 =====")
        for k in rest:
            out_lines.append(f"{k}={pending[k]}")
    text = "\n".join(out_lines) + "\n"

    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, p)
    return sorted(pending)
