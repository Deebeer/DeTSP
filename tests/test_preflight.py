"""实盘预检测试：mock 场所，不联网、不用真密钥（密钥为现造的假值）。"""

from detsp.core.config import VENUE_IDS, build_config
from detsp.core.preflight import main as preflight_main
from detsp.core.preflight import run_preflight
from detsp.core.venue import Market


class MockVenue:
    """最小 mock：只实现预检会碰的接口。"""

    def __init__(self, mode="paper"):
        self.mode = mode
        self.venue_id = ""

    def markets(self):
        return [Market(symbol="BTC", price_step=0.1, qty_step=0.001,
                       min_qty=0.001, min_notional=10.0, max_leverage=50.0)]

    def balance(self):
        return {"equity": 1234.5, "available": 1000.0}


def _venues(mode="paper"):
    return {vid: MockVenue(mode=mode) for vid in VENUE_IDS}


def _items(rep, venue, item):
    return [i for i in rep.items if i.venue == venue and i.item == item]


def test_paper_all_offline_ok():
    cfg = build_config(env={}, yaml_data={})
    rep = run_preflight(cfg, _venues("paper"))
    assert rep.ok
    assert _items(rep, "arcus", "凭据齐全")[0].status == "跳过"
    assert _items(rep, "arcus", "精度与最小量")[0].status == "通过"
    assert _items(rep, "aster", "账户快照")[0].status == "跳过"


def test_live_missing_credentials_fails():
    cfg = build_config(env={}, yaml_data={"venues": {"arcus": {"mode": "live"}}})
    venues = _venues("paper")
    venues["arcus"] = MockVenue(mode="live")
    rep = run_preflight(cfg, venues)
    assert not rep.ok
    item = _items(rep, "arcus", "凭据齐全")[0]
    assert item.status == "失败" and "ARCUS_ADDRESS" in item.detail
    # 报告里不出现任何密钥值（本来就没有），且签名项被跳过
    assert _items(rep, "arcus", "签名自检")[0].status == "跳过"


def test_live_aster_sign_check_passes():
    secrets = {"ASTER_API_KEY": "fake-key", "ASTER_SECRET_KEY": "fake-secret"}
    cfg = build_config(env=secrets,
                       yaml_data={"venues": {"aster": {"mode": "live"}}})
    venues = _venues("paper")
    venues["aster"] = MockVenue(mode="live")
    rep = run_preflight(cfg, venues)
    assert _items(rep, "aster", "凭据齐全")[0].status == "通过"
    assert _items(rep, "aster", "签名自检")[0].status == "通过"
    assert _items(rep, "aster", "账户快照")[0].status == "通过"
    assert rep.ok


def test_live_arcus_sign_check_with_generated_key():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
    )
    from detsp.venues._arcus_sign import load_private_key, public_key_hex

    priv = Ed25519PrivateKey.generate()
    from cryptography.hazmat.primitives import serialization
    seed_hex = priv.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
        serialization.NoEncryption()).hex()
    pub = public_key_hex(load_private_key(seed_hex, ""))
    secrets = {
        "ARCUS_ADDRESS": "0x" + "11" * 20,
        "ARCUS_ACCOUNT_INDEX": "0",
        "ARCUS_API_KEY": pub,
        "ARCUS_API_PRIVATE_KEY": seed_hex,
    }
    cfg = build_config(env=secrets,
                       yaml_data={"venues": {"arcus": {"mode": "live"}}})
    venues = _venues("paper")
    venues["arcus"] = MockVenue(mode="live")
    rep = run_preflight(cfg, venues)
    assert _items(rep, "arcus", "签名自检")[0].status == "通过"
    assert rep.ok
    # 假密钥值不得出现在报告文本里
    assert seed_hex not in rep.render()


def test_live_hl_sign_check_with_generated_key():
    from eth_account import Account
    acct = Account.create()
    secrets = {"HL_ACCOUNT_ADDRESS": acct.address,
               "HL_PRIVATE_KEY": acct.key.hex()}
    cfg = build_config(env=secrets,
                       yaml_data={"venues": {"entropy": {"mode": "live"}}})
    venues = _venues("paper")
    venues["entropy"] = MockVenue(mode="live")
    rep = run_preflight(cfg, venues)
    assert _items(rep, "entropy", "签名自检")[0].status == "通过"


def test_main_paper_exit_zero(tmp_path):
    code = preflight_main(["--paper",
                           "--config", str(tmp_path / "none.yaml"),
                           "--env", str(tmp_path / "none.env")])
    assert code == 0
