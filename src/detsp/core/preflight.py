"""实盘预检：逐场所逐项检查，任一关键项失败退出码非 0。

检查项（live 场所全跑；paper 场所只跑离线项，其余标 skip）：
1. 凭据齐全：只查字段是否存在，绝不打印值。
2. 签名自检：能签不能发——用各场所已有的本地签名能力构造一次
   签名（Arcus Ed25519 / HL L1 动作 / Aster HMAC / Lighter 签名器构造），
   不向交易所发送任何签名请求。
3. 时钟偏差：取场所 HTTP 响应的 Date 头与本机 UTC 比较
   （Arcus 拒收偏差 >30 秒的签名，阈值统一 30 秒）。
4. 公开行情连通：markets() 能返回市场。
5. 账户快照：live 才查 balance() 可读。
6. 精度与最小量：市场规则含 min_qty / min_notional。

命令行：python -m detsp.preflight [--config config.yaml] [--env .env]
        [--paper]（--paper 强制全 paper，只跑离线项）
"""

from __future__ import annotations

import argparse
import os
import time
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from pathlib import Path

from .config import VENUE_IDS, AppConfig, build_config, load_env_file, load_yaml_config

PASS, FAIL, SKIP = "通过", "失败", "跳过"
CLOCK_TOLERANCE_SEC = 30.0

# 每所必填凭据：每组为「任选其一」的字段组
REQUIRED_SECRETS: dict[str, list[tuple[str, ...]]] = {
    "arcus": [("ARCUS_ADDRESS",), ("ARCUS_ACCOUNT_INDEX",), ("ARCUS_API_KEY",),
              ("ARCUS_API_PRIVATE_KEY", "ARCUS_API_PRIVATE_KEY_FILE")],
    "lighter": [("LIGHTER_ACCOUNT_INDEX",), ("LIGHTER_API_KEY_INDEX",),
                ("LIGHTER_API_PRIVATE_KEY", "LIGHTER_API_PRIVATE_KEY_FILE")],
    "lighter_rh": [("LIGHTER_RH_ACCOUNT_INDEX",), ("LIGHTER_RH_API_KEY_INDEX",),
                   ("LIGHTER_RH_API_PRIVATE_KEY",
                    "LIGHTER_RH_API_PRIVATE_KEY_FILE")],
    "hyperliquid": [("HL_ACCOUNT_ADDRESS",), ("HL_PRIVATE_KEY",)],
    "entropy": [("HL_ACCOUNT_ADDRESS",), ("HL_PRIVATE_KEY",)],
    "tradexyz": [("HL_ACCOUNT_ADDRESS_XYZ", "HL_ACCOUNT_ADDRESS"),
                 ("HL_PRIVATE_KEY_XYZ", "HL_PRIVATE_KEY")],
    "aster": [("ASTER_API_KEY",), ("ASTER_SECRET_KEY",)],
}


@dataclass
class CheckItem:
    venue: str
    item: str
    status: str          # 通过 / 失败 / 跳过
    detail: str = ""


@dataclass
class PreflightReport:
    items: list[CheckItem] = field(default_factory=list)

    def add(self, venue: str, item: str, status: str, detail: str = "") -> None:
        self.items.append(CheckItem(venue, item, status, detail))

    @property
    def ok(self) -> bool:
        """任一检查失败即整体不通过（skip 不算失败）。"""
        return all(i.status != FAIL for i in self.items)

    def render(self) -> str:
        lines = []
        for i in self.items:
            lines.append(f"[{i.status}] {i.venue} · {i.item}"
                         + (f"：{i.detail}" if i.detail else ""))
        lines.append("预检结论：" + ("全部通过（或跳过）" if self.ok
                                    else "存在失败项，禁止切实盘"))
        return "\n".join(lines)


# ---------- 单项检查 ----------

def _check_credentials(rep: PreflightReport, venue_id: str, venue,
                       secrets: dict) -> bool:
    if venue.mode != "live":
        rep.add(venue_id, "凭据齐全", SKIP, "paper 模式不需要凭据")
        return True
    missing = []
    for group in REQUIRED_SECRETS.get(venue_id, []):
        if not any(secrets.get(k) for k in group):
            missing.append("/".join(group))
    if missing:
        rep.add(venue_id, "凭据齐全", FAIL, f"缺字段：{', '.join(missing)}")
        return False
    rep.add(venue_id, "凭据齐全", PASS, "必填字段均已填写（未显示值）")
    return True


def _sign_check_arcus(secrets: dict) -> tuple[str, str]:
    from ..venues._arcus_sign import load_private_key, public_key_hex, sign_hex
    key = load_private_key(secrets.get("ARCUS_API_PRIVATE_KEY", ""),
                           secrets.get("ARCUS_API_PRIVATE_KEY_FILE", ""))
    pub = public_key_hex(key)
    expect = (secrets.get("ARCUS_API_KEY") or "").lower().removeprefix("0x")
    if expect and pub.lower().removeprefix("0x") != expect:
        return FAIL, "由私钥推导的公钥与 ARCUS_API_KEY 不一致"
    sig = sign_hex(key, "detsp-preflight")
    return (PASS, "Ed25519 本地签名成功且公钥与配置一致（未发送）") \
        if sig else (FAIL, "签名结果为空")


def _sign_check_hl(secrets: dict, venue_id: str) -> tuple[str, str]:
    suffix = "_XYZ" if venue_id == "tradexyz" else ""
    raw = secrets.get(f"HL_PRIVATE_KEY{suffix}") or secrets.get("HL_PRIVATE_KEY", "")
    from eth_account import Account
    acct = Account.from_key(raw)
    try:
        from hyperliquid.utils import signing as hl_signing
    except ImportError:
        return PASS, f"API 钱包私钥格式合法（签名地址 {acct.address[:10]}…；SDK 未安装，仅验格式）"
    action = {"type": "cancel", "cancels": [{"a": 0, "o": 0}]}
    sig = hl_signing.sign_l1_action(acct, action, None, int(time.time() * 1000),
                                    None, True)
    if not sig or "r" not in sig:
        return FAIL, "L1 动作签名结果异常"
    return PASS, "L1 动作本地签名成功（只签不发）"


def _sign_check_lighter(secrets: dict, venue_id: str) -> tuple[str, str]:
    prefix = "LIGHTER" if venue_id == "lighter" else "LIGHTER_RH"
    key = secrets.get(f"{prefix}_API_PRIVATE_KEY", "")
    kfile = secrets.get(f"{prefix}_API_PRIVATE_KEY_FILE", "")
    if kfile and not key:
        p = Path(kfile)
        if not p.exists():
            return FAIL, f"私钥文件不存在：{kfile}"
        key = p.read_text(encoding="utf-8").strip().splitlines()[0].strip()
    try:
        import lighter
    except ImportError:
        return SKIP, "lighter-sdk 未安装（pip install -e \".[live]\"），跳过签名器构造"
    from ..venues.lighter import PROFILES
    deployment = "mainnet" if venue_id == "lighter" else "robinhood"
    prof = PROFILES[deployment]
    signer = lighter.SignerClient(
        url=prof.api_url,
        account_index=int(secrets[f"{prefix}_ACCOUNT_INDEX"]),
        api_private_keys={int(secrets[f"{prefix}_API_KEY_INDEX"]): key},
        chain_id=prof.chain_id)
    # 只构造不调 check_client（那会联网），构造成功即证明密钥可被签名器加载
    _ = signer
    return PASS, "签名器本地构造成功（未联网校验，留待小额验证）"


def _sign_check_aster(secrets: dict) -> tuple[str, str]:
    from ..venues.aster import sign_params
    out = sign_params(secrets["ASTER_SECRET_KEY"],
                      {"timestamp": int(time.time() * 1000)})
    sig = out.rsplit("signature=", 1)[-1]
    if len(sig) != 64:
        return FAIL, "HMAC 签名长度异常"
    return PASS, "HMAC-SHA256 本地签名成功（未发送）"


def _check_sign(rep: PreflightReport, venue_id: str, venue,
                secrets: dict, creds_ok: bool) -> None:
    if venue.mode != "live":
        rep.add(venue_id, "签名自检", SKIP, "paper 模式跳过")
        return
    if not creds_ok:
        rep.add(venue_id, "签名自检", SKIP, "凭据不齐，无法验签")
        return
    try:
        if venue_id == "arcus":
            status, detail = _sign_check_arcus(secrets)
        elif venue_id in ("hyperliquid", "entropy", "tradexyz"):
            status, detail = _sign_check_hl(secrets, venue_id)
        elif venue_id in ("lighter", "lighter_rh"):
            status, detail = _sign_check_lighter(secrets, venue_id)
        elif venue_id == "aster":
            status, detail = _sign_check_aster(secrets)
        else:
            status, detail = SKIP, "该场所无签名自检实现"
    except Exception as e:
        status, detail = FAIL, f"签名构造失败：{e}"
    rep.add(venue_id, "签名自检", status, detail)


def _venue_base_url(venue) -> str | None:
    for attr in ("_api_url", "_base_url"):
        v = getattr(venue, attr, None)
        if v:
            return str(v)
    prof = getattr(venue, "profile", None)
    return getattr(prof, "api_url", None)


def _check_clock(rep: PreflightReport, venue_id: str, venue) -> None:
    if venue.mode != "live":
        rep.add(venue_id, "时钟偏差", SKIP, "paper 模式只查本机时钟（正常）")
        return
    base = _venue_base_url(venue)
    if not base:
        rep.add(venue_id, "时钟偏差", SKIP, "无已知端点，跳过")
        return
    try:
        import httpx
        r = httpx.get(base, timeout=10.0)
        date_hdr = r.headers.get("date")
        if not date_hdr:
            rep.add(venue_id, "时钟偏差", SKIP, "响应无 Date 头")
            return
        server_ts = parsedate_to_datetime(date_hdr).timestamp()
        skew = abs(time.time() - server_ts)
        if skew > CLOCK_TOLERANCE_SEC:
            rep.add(venue_id, "时钟偏差", FAIL,
                    f"本机与服务器相差 {skew:.1f} 秒（>{CLOCK_TOLERANCE_SEC:.0f} 秒会被拒签）")
        else:
            rep.add(venue_id, "时钟偏差", PASS, f"偏差 {skew:.1f} 秒")
    except Exception as e:
        rep.add(venue_id, "时钟偏差", SKIP, f"读取服务器时间失败：{e}")


def _check_markets_and_rules(rep: PreflightReport, venue_id: str, venue) -> None:
    """行情连通 + 精度/最小量：paper 时 markets 为本地合成数据（离线项）。"""
    try:
        markets = venue.markets()
    except Exception as e:
        rep.add(venue_id, "公开行情连通", FAIL, f"markets() 失败：{e}")
        rep.add(venue_id, "精度与最小量", SKIP, "无市场数据")
        return
    if venue.mode == "live":
        rep.add(venue_id, "公开行情连通", PASS, f"取得 {len(markets)} 个市场")
    else:
        rep.add(venue_id, "公开行情连通", SKIP, "paper 模式为本地数据，不验连通")
    if not markets:
        rep.add(venue_id, "精度与最小量", FAIL, "市场列表为空")
        return
    bad = [m.symbol for m in markets if m.min_qty <= 0 or m.price_step <= 0]
    if bad:
        rep.add(venue_id, "精度与最小量", FAIL, f"规则缺失：{bad[:3]}")
    else:
        rep.add(venue_id, "精度与最小量", PASS,
                f"{len(markets)} 个市场精度/最小量齐全")


def _check_account(rep: PreflightReport, venue_id: str, venue) -> None:
    if venue.mode != "live":
        rep.add(venue_id, "账户快照", SKIP, "paper 模式跳过")
        return
    try:
        bal = venue.balance()
        eq = bal.get("equity")
        if eq is None:
            rep.add(venue_id, "账户快照", FAIL, "balance() 未返回 equity")
        else:
            rep.add(venue_id, "账户快照", PASS, f"权益可读（{float(eq):.2f}）")
    except Exception as e:
        rep.add(venue_id, "账户快照", FAIL, f"balance() 失败：{e}")


def run_preflight(app_config: AppConfig, venues: dict) -> PreflightReport:
    rep = PreflightReport()
    secrets = app_config.secrets or {}
    for vid in VENUE_IDS:
        venue = venues.get(vid)
        if venue is None:
            rep.add(vid, "场所组装", FAIL, "venues 中缺失")
            continue
        creds_ok = _check_credentials(rep, vid, venue, secrets)
        _check_sign(rep, vid, venue, secrets, creds_ok)
        _check_clock(rep, vid, venue)
        _check_markets_and_rules(rep, vid, venue)
        _check_account(rep, vid, venue)
    return rep


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="DeTSP 实盘预检")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--env", default=".env")
    ap.add_argument("--paper", action="store_true", help="强制全 paper，只跑离线项")
    args = ap.parse_args(argv)

    env = dict(os.environ)
    for k, v in load_env_file(args.env).items():
        env.setdefault(k, v)
    cfg_path = Path(args.config)
    yaml_data = load_yaml_config(cfg_path) if cfg_path.exists() else {}
    app_config = build_config(env=env, yaml_data=yaml_data)

    from ..venues.factory import build_venues
    venues = build_venues(app_config, force_paper=args.paper)
    rep = run_preflight(app_config, venues)
    print(rep.render())
    return 0 if rep.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
