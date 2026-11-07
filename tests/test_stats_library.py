"""费率常量 + 中枢库 / 费差库测试：手造快照与 CSV，数值全部手算对账。"""

import csv
import time
from pathlib import Path

import pytest

from detsp.core.config import VENUE_IDS
from detsp.core.fee_table import FeeTable, get_fees
from detsp.core.stats_library import LibraryService, StatsLibrary
from detsp.core.venue import FundingInfo
from detsp.tools.analyze import analyze_rows

PREMIUM_FIELDS = [
    "minute_ts", "premium_mean_bps", "sell_edge_mean_bps",
    "sell_edge_max_bps", "buy_edge_mean_bps", "buy_edge_max_bps",
]
FUNDING_FIELDS = ["hour_ts", "rate_a_bps_h", "rate_b_bps_h", "net_bps_h"]


def _cell(price=None, funding=None):
    return {"price": price, "funding": funding, "vol": None,
            "max_leverage": None, "margin_mode": "unknown"}


def _snap(ts, data):
    return {"ts": ts, "data": data, "listings": {}, "errors": {}}


def _read_rows(path: Path):
    with path.open(newline="", encoding="utf-8") as fh:
        return [{k: float(v) for k, v in r.items()}
                for r in csv.DictReader(fh)]


def _write_premium_csv(path: Path, premiums, sell=40.0, buy=20.0):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=PREMIUM_FIELDS)
        w.writeheader()
        for i, p in enumerate(premiums):
            w.writerow({
                "minute_ts": i * 60, "premium_mean_bps": p,
                "sell_edge_mean_bps": sell, "sell_edge_max_bps": sell,
                "buy_edge_mean_bps": buy, "buy_edge_max_bps": buy})


def _write_funding_csv(path: Path, nets, start_hour=0):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=FUNDING_FIELDS)
        w.writeheader()
        for i, net in enumerate(nets):
            w.writerow({
                "hour_ts": (start_hour + i) * 3600,
                "rate_a_bps_h": net, "rate_b_bps_h": 0.0,
                "net_bps_h": net})


# ---------- 费率常量（第十二轮：可编辑费率表已撤） ----------

def test_fee_constants_uniform_defaults(tmp_path):
    """全所统一 taker 5 / maker 2 bps（代码内常量，用户定版
    「按普遍默认」）；FeeTable 退化为只读适配：构造路径被忽略、
    不读不写文件、没有 set。"""
    ft = FeeTable(tmp_path / "fees.json")
    for vid in VENUE_IDS:
        assert ft.get(vid) == (5.0, 2.0)
        assert get_fees(vid) == (5.0, 2.0)
    assert not hasattr(ft, "set")
    assert not (tmp_path / "fees.json").exists()
    with pytest.raises(ValueError):
        ft.get("nope")
    with pytest.raises(ValueError):
        get_fees("nope")


def test_fee_table_ignores_stale_fees_json(tmp_path):
    """已有落盘的旧费率 JSON 不再读取：里面写了自定义值也按
    常量走（文件留着不管，代码不读）。"""
    import json

    path = tmp_path / "fees.json"
    path.write_text(json.dumps(
        {"arcus": {"taker_bps": 99.0, "maker_bps": 88.0}}),
        encoding="utf-8")
    ft = FeeTable(path)
    assert ft.get("arcus") == (5.0, 2.0)
    assert get_fees("arcus") == (5.0, 2.0)


# ---------- 溢价分钟聚合 ----------

def test_record_snapshot_minute_aggregation(tmp_path):
    lib = StatsLibrary(tmp_path, FeeTable(tmp_path / "fees.json"))
    # 第 0 分钟两个样本：premium 200 与 100 bps
    lib.record_snapshot(_snap(0.0, {
        "arcus": {"BTC": _cell(price=102.0)},
        "lighter": {"BTC": _cell(price=100.0)},
    }), ts=0.0)
    lib.record_snapshot(_snap(30.0, {
        "arcus": {"BTC": _cell(price=101.0)},
        "lighter": {"BTC": _cell(price=100.0)},
    }), ts=30.0)
    path = tmp_path / "premium_minutes" / "arcus__lighter__BTC.csv"
    assert not path.exists()  # 还没跨分钟，不落盘
    # 第 1 分钟第一笔样本触发上一分钟落盘
    lib.record_snapshot(_snap(61.0, {
        "arcus": {"BTC": _cell(price=100.0)},
        "lighter": {"BTC": _cell(price=100.0)},
    }), ts=61.0)
    rows = _read_rows(path)
    assert len(rows) == 1
    row = rows[0]
    assert row["minute_ts"] == 0
    # 样本 [200, 100]：mean 150、max 200；buy 取 -premium：mean -150、max -100
    assert row["premium_mean_bps"] == pytest.approx(150.0)
    assert row["sell_edge_mean_bps"] == pytest.approx(150.0)
    assert row["sell_edge_max_bps"] == pytest.approx(200.0)
    assert row["buy_edge_mean_bps"] == pytest.approx(-150.0)
    assert row["buy_edge_max_bps"] == pytest.approx(-100.0)
    # flush 把第 1 分钟（premium 0）也落盘
    lib.flush()
    rows = _read_rows(path)
    assert len(rows) == 2
    assert rows[1]["minute_ts"] == 60
    assert rows[1]["premium_mean_bps"] == pytest.approx(0.0)


# ---------- 费差小时聚合 ----------

def test_record_snapshot_funding_hourly_conversion(tmp_path):
    lib = StatsLibrary(tmp_path, FeeTable(tmp_path / "fees.json"))
    # arcus 每小时结算 0.08% → 8 bps/h；lighter 每 8 小时 0.16%
    # → 0.02%/h = 2 bps/h；净费差 6 bps/h
    fa = FundingInfo(symbol="BTC", rate=0.0008, interval_seconds=3600,
                     rate_kind="current")
    fb = FundingInfo(symbol="BTC", rate=0.0016, interval_seconds=28800,
                     rate_kind="current")
    lib.record_snapshot(_snap(0.0, {
        "arcus": {"BTC": _cell(price=100.0, funding=fa)},
        "lighter": {"BTC": _cell(price=100.0, funding=fb)},
    }), ts=0.0)
    lib.flush()
    path = tmp_path / "funding_hours" / "arcus__lighter__BTC.csv"
    rows = _read_rows(path)
    assert len(rows) == 1
    assert rows[0]["hour_ts"] == 0
    assert rows[0]["rate_a_bps_h"] == pytest.approx(8.0)
    assert rows[0]["rate_b_bps_h"] == pytest.approx(2.0)
    assert rows[0]["net_bps_h"] == pytest.approx(6.0)


def test_record_snapshot_funding_gate_skips_suspect(tmp_path):
    lib = StatsLibrary(tmp_path, FeeTable(tmp_path / "fees.json"))
    # 单期费率 6% > 5% 闸门（risk.SANE_RATE_PER_INTERVAL）→ 跳过
    fa = FundingInfo(symbol="BTC", rate=0.06, interval_seconds=3600,
                     rate_kind="current")
    fb = FundingInfo(symbol="BTC", rate=0.001, interval_seconds=3600,
                     rate_kind="current")
    lib.record_snapshot(_snap(0.0, {
        "arcus": {"BTC": _cell(price=100.0, funding=fa)},
        "lighter": {"BTC": _cell(price=100.0, funding=fb)},
    }), ts=0.0)
    lib.flush()
    path = tmp_path / "funding_hours" / "arcus__lighter__BTC.csv"
    assert not path.exists()


# ---------- 中枢库重算 ----------

def test_recompute_premium_admission_and_midline(tmp_path):
    fee_table = FeeTable(tmp_path / "fees.json")
    lib = StatsLibrary(tmp_path, fee_table)
    # BTC：200 分钟行（≥180 准入）；ETH：100 分钟行（不准入）
    _write_premium_csv(
        tmp_path / "premium_minutes" / "arcus__lighter__BTC.csv",
        [5.0] * 200)
    _write_premium_csv(
        tmp_path / "premium_minutes" / "arcus__lighter__ETH.csv",
        [5.0] * 100)
    now = 200 * 60.0
    result = lib.recompute_premium(now)
    assert "arcus__lighter__BTC" in result
    assert "arcus__lighter__ETH" not in result

    rows = _read_rows(
        tmp_path / "premium_minutes" / "arcus__lighter__BTC.csv")
    rows = [r for r in rows if r["minute_ts"] >= now - 86400]
    expected = analyze_rows(rows, fees_bps=10.0, min_fires=10)
    entry = result["arcus__lighter__BTC"]
    assert entry["midline_bps"] == expected["suggested"]["midline_bps"]
    assert entry["upper_bps"] == expected["suggested"]["upper_bps"]
    assert entry["lower_bps"] == expected["suggested"]["lower_bps"]
    table = {t["band"]: t for t in expected["table"]}
    assert entry["sell_fires"] == table[entry["upper_bps"]]["sell_fires"]
    assert entry["buy_fires"] == table[entry["lower_bps"]]["buy_fires"]
    assert entry["minutes"] == 200
    assert entry["sample_hours"] == pytest.approx(round(200 / 60, 2))
    assert entry["updated_at"] == now
    # premium_entry 附带三元组字段；不准入返回 None
    got = lib.premium_entry("arcus", "lighter", "BTC")
    assert got is not None
    assert got["midline_bps"] == entry["midline_bps"]
    assert (got["base"], got["hedge"], got["symbol"]) == \
        ("arcus", "lighter", "BTC")
    assert lib.premium_entry("arcus", "lighter", "ETH") is None
    assert set(lib.premium_entries()) == {"arcus__lighter__BTC"}


def test_recompute_premium_empty(tmp_path):
    lib = StatsLibrary(tmp_path, FeeTable(tmp_path / "fees.json"))
    assert lib.recompute_premium(1000.0) == {}
    assert lib.premium_entries() == {}
    assert lib.premium_entry("arcus", "lighter", "BTC") is None


# ---------- 费差统计与硬关 ----------

def test_funding_stats(tmp_path):
    lib = StatsLibrary(tmp_path, FeeTable(tmp_path / "fees.json"))
    # 9 小时：net 序列的达标段（≥5）长 3 / 2 / 1，中位数 2
    nets = [10.0, 10.0, 10.0, 2.0, 10.0, 10.0, 2.0, 2.0, 10.0]
    _write_funding_csv(
        tmp_path / "funding_hours" / "arcus__lighter__BTC.csv", nets)
    now = 8 * 3600.0
    # va/vb 顺序任意
    stats = lib.funding_stats("lighter", "arcus", "BTC", 5.0, now)
    assert stats is not None
    assert stats["median_duration_h"] == pytest.approx(2.0)
    assert stats["above_ratio"] == pytest.approx(6 / 9)
    assert stats["avg_net_bps_per_hour"] == pytest.approx(66 / 9)
    assert stats["sample_hours"] == 9
    assert stats["updated_at"] == pytest.approx(8 * 3600.0)
    assert lib.funding_stats("arcus", "lighter", "ETH", 5.0, now) is None


def test_breakeven_hours():
    assert StatsLibrary.breakeven_hours(20.0, 5.0) == pytest.approx(4.0)
    assert StatsLibrary.breakeven_hours(20.0, 0.0) is None
    assert StatsLibrary.breakeven_hours(20.0, -1.0) is None


def _gate_lib(tmp_path, name, nets):
    data_dir = tmp_path / name
    lib = StatsLibrary(data_dir, FeeTable(data_dir / "fees.json"))
    _write_funding_csv(
        data_dir / "funding_hours" / "arcus__lighter__BTC.csv", nets)
    return lib, (len(nets) - 1) * 3600.0


def test_funding_gate_pass(tmp_path):
    lib, now = _gate_lib(tmp_path, "pass", [10.0] * 30)
    gate = lib.funding_gate("arcus", "lighter", "BTC", 5.0, 20.0, 5.0, now)
    assert gate["ok"] is True
    assert "通过" in gate["reason"]
    assert gate["breakeven_hours"] == pytest.approx(4.0)
    assert gate["stats"]["sample_hours"] == 30


def test_funding_gate_sample_insufficient(tmp_path):
    lib, now = _gate_lib(tmp_path, "short", [10.0] * 10)
    gate = lib.funding_gate("arcus", "lighter", "BTC", 5.0, 20.0, 5.0, now)
    assert gate["ok"] is False
    assert "样本不足" in gate["reason"]


def test_funding_gate_persistence_insufficient(tmp_path):
    # 30 小时样本够，但达标段全长 1 小时，中位 1 < 回本 4 × 2
    lib, now = _gate_lib(tmp_path, "fragile", [10.0, 1.0] * 15)
    gate = lib.funding_gate("arcus", "lighter", "BTC", 5.0, 20.0, 5.0, now)
    assert gate["ok"] is False
    assert "持续性不足" in gate["reason"]


def test_funding_gate_no_current_edge(tmp_path):
    lib, now = _gate_lib(tmp_path, "noedge", [10.0] * 30)
    gate = lib.funding_gate("arcus", "lighter", "BTC", 5.0, 20.0, 0.0, now)
    assert gate["ok"] is False
    assert gate["breakeven_hours"] is None
    assert "无当前费差" in gate["reason"]


# ---------- LibraryService ----------

class _FakeScanner:
    def __init__(self, snap):
        self._snap = snap
        self.calls = 0

    def full_snapshot(self):
        self.calls += 1
        return self._snap


def test_library_service_lifecycle(tmp_path):
    snap = _snap(1_700_000_000.0, {
        "arcus": {"BTC": _cell(price=101.0)},
        "lighter": {"BTC": _cell(price=100.0)},
    })
    svc = LibraryService(_FakeScanner(snap), tmp_path,
                         record_interval=0.05, recompute_interval=0.05)
    assert isinstance(svc.library, StatsLibrary)
    assert isinstance(svc.fee_table, FeeTable)
    svc.start()
    try:
        deadline = time.time() + 5.0
        while time.time() < deadline:
            if svc.status()["last_record_ts"] is not None:
                break
            time.sleep(0.02)
        status = svc.status()
        assert status["running"] is True
        assert status["last_record_ts"] == pytest.approx(1_700_000_000.0)
        assert status["last_error"] == ""
        assert status["premium_entries"] == 0
    finally:
        svc.stop()
    assert svc.status()["running"] is False
    # stop 已 flush：分钟桶落盘一行
    path = tmp_path / "premium_minutes" / "arcus__lighter__BTC.csv"
    assert path.exists()
    assert len(_read_rows(path)) == 1
