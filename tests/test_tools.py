"""盘口录制 / 溢价分析测试：造数据验聚合与建议阈值。"""

import csv

from detsp.tools.analyze import analyze_rows, load_rows
from detsp.tools.recorder import CsvRecorder, MinuteAggregator


def test_aggregator_minute_bar():
    agg = MinuteAggregator()
    # 第 0 分钟：base 与 hedge 同价，溢价 0
    assert agg.add(0, 100.0, 101.0, 100.0, 101.0) is None
    assert agg.add(20, 100.0, 101.0, 100.0, 101.0) is None
    # 第 1 分钟第一笔样本触发上一分钟成 bar
    bar = agg.add(61, 101.0, 102.0, 100.0, 101.0)
    assert bar is not None
    assert bar["minute_ts"] == 0 and bar["samples"] == 2
    assert bar["premium_mean_bps"] == 0.0
    # 第 1 分钟：base 中价 101.5 / hedge 100.5 → 溢价约 99.5 bps
    bar2 = agg.flush()
    assert bar2["samples"] == 1
    assert 99.0 < bar2["premium_close_bps"] < 100.0
    assert bar2["sell_edge_mean_bps"] == 0.0  # base_bid 101 / hedge_ask 101


def test_recorder_writes_csv(tmp_path):
    path = tmp_path / "minutes.csv"
    rec = CsvRecorder(path)
    for ts in (0, 30, 59):
        rec.add_sample(ts, 100.0, 101.0, 100.0, 101.0)
    for ts in (60, 90):
        rec.add_sample(ts, 102.0, 103.0, 100.0, 101.0)
    rec.close()
    with path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert len(rows) == 2
    assert rows[0]["samples"] == "3" and rows[1]["samples"] == "2"
    assert float(rows[1]["premium_mean_bps"]) > 100.0


def _rows(n=60, premium=5.0, sell_edge=40.0, buy_edge=20.0):
    return [{
        "minute_ts": float(i * 60), "base_bid": 100.0, "base_ask": 101.0,
        "hedge_bid": 100.0, "hedge_ask": 101.0,
        "premium_open_bps": premium, "premium_high_bps": premium,
        "premium_low_bps": premium, "premium_close_bps": premium,
        "premium_mean_bps": premium,
        "sell_edge_mean_bps": sell_edge, "sell_edge_max_bps": sell_edge,
        "buy_edge_mean_bps": buy_edge, "buy_edge_max_bps": buy_edge,
        "samples": 60.0,
    } for i in range(n)]


def test_analyze_suggests_midline_and_bands():
    res = analyze_rows(_rows(), fees_bps=0.0, min_fires=10)
    assert res["midline_bps"] == 5.0
    # sell_edge 40 > 中枢 5 + band：band<=30 时每分钟都触发 → 取最小达标带
    assert res["suggested"]["upper_bps"] == 5
    # buy_edge 20 > band - 5：band=5 时 20>0 每分钟触发 → lower 取 5
    assert res["suggested"]["lower_bps"] == 5
    table = {t["band"]: t for t in res["table"]}
    assert table[30]["sell_fires"] == 60       # 40 > 5+30
    assert table[40]["sell_fires"] == 0        # 40 > 5+40 不成立


def test_analyze_fees_reduce_fires():
    res = analyze_rows(_rows(), fees_bps=36.0, min_fires=10)
    table = {t["band"]: t for t in res["table"]}
    assert table[5]["sell_fires"] == 0         # 40-36=4 > 5+5 不成立


def test_load_rows_roundtrip(tmp_path):
    path = tmp_path / "m.csv"
    rec = CsvRecorder(path)
    rec.add_sample(0, 100.0, 101.0, 100.0, 101.0)
    rec.add_sample(60, 100.0, 101.0, 100.0, 101.0)
    rec.close()
    rows = load_rows(path)
    assert len(rows) == 2
    res = analyze_rows(rows, min_fires=1)
    assert res["minutes"] == 2 and res["midline_bps"] == 0.0
