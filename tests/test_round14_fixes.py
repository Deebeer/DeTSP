"""第十四轮全仓梳理修复的回归测试。

覆盖：统计库注入监督器（F2）、risk.max_consecutive_errors 接线
（F1）、费差持续时长去重与断段（F3）、recorder 坏盘口分钟不崩
（B-F1）、资金费参数校验补齐（B-F2）、溢价 taker 被拒不设宽限
（B-F3）、溢价 manual 平仓带不生效（B-F4）、analyze 分位数口径
（S2）。
"""

import pytest

from detsp.core.config import build_config
from detsp.core.engine import Engine, build_task
from detsp.core.fee_table import FeeTable
from detsp.core.ledger import Ledger
from detsp.core.stats_library import StatsLibrary
from detsp.core.venue import Order
from detsp.strategies.funding_carry import CarryParams
from detsp.strategies.premium_arb import PremiumArbStrategy, PremiumParams
from detsp.tools.analyze import analyze_rows
from detsp.tools.recorder import MinuteAggregator
from detsp.venues.paper import PaperVenue


def _venues():
    return {"arcus": PaperVenue(venue_id="arcus", prices={"BTC": 100.0}),
            "lighter": PaperVenue(venue_id="lighter", prices={"BTC": 100.0})}


# ── F1：risk.max_consecutive_errors 真正到达任务 ──

class _StubExec:
    """build_task 组装期只需 claim/position 两个口。"""

    def claim(self, owner, venue, symbol):
        pass

    def position(self, venue, symbol):
        return None


def test_build_task_threads_max_consecutive_errors():
    cfg = {"name": "c", "strategy": "funding_carry",
           "params": {"venue_a": "arcus", "venue_b": "lighter",
                      "symbol": "BTC", "notional_usd": 100}}
    t = build_task(cfg, _StubExec(), None, max_consecutive_errors=7)
    assert t.errors.max_consecutive == 7
    t2 = build_task(cfg, _StubExec(), None)
    assert t2.errors.max_consecutive == 3   # 不传保持 ErrorCounter 默认


def test_config_risk_max_errors_reaches_task():
    y = {"risk": {"max_consecutive_errors": 1},
         "tasks": [{"name": "c", "strategy": "funding_carry",
                    "run_mode": "semi_fixed",
                    "params": {"venue_a": "arcus", "venue_b": "lighter",
                               "symbol": "BTC", "notional_usd": 100}}]}
    cfg = build_config(env={}, yaml_data=y)
    engine = Engine.from_config(cfg, _venues(), ledger=Ledger())
    assert engine.tasks[0].errors.max_consecutive == 1


# ── F2：统计库注入监督器（生产路径） ──

def test_build_task_threads_library_into_supervisor():
    lib = object()
    cfg = {"name": "a", "strategy": "premium_arb", "run_mode": "semi_auto",
           "params": {"base_venue": "arcus", "hedge_venue": "lighter",
                      "symbol": "BTC", "midline_bps": 0, "upper_bps": 10,
                      "lower_bps": 10}}
    t = build_task(cfg, None, None, library=lib)
    assert t.runtime.library is lib


def test_from_config_library_kwarg_reaches_supervisor():
    lib = object()
    y = {"tasks": [{"name": "a", "strategy": "premium_arb",
                    "run_mode": "semi_auto",
                    "params": {"base_venue": "arcus",
                               "hedge_venue": "lighter", "symbol": "BTC",
                               "midline_bps": 0, "upper_bps": 10,
                               "lower_bps": 10}}]}
    cfg = build_config(env={}, yaml_data=y)
    engine = Engine.from_config(cfg, _venues(), ledger=Ledger(),
                                library=lib)
    assert engine.tasks[0].runtime.library is lib


# ── F3：费差库去重 + 按真实时间断段 ──

def _write_funding_csv(tmp_path, rows):
    d = tmp_path / "funding_hours"
    d.mkdir(parents=True, exist_ok=True)
    path = d / "arcus__lighter__BTC.csv"
    with path.open("w", encoding="utf-8") as fh:
        fh.write("hour_ts,rate_a_bps_h,rate_bps_h,net_bps_h\n")
        for ts, net in rows:
            fh.write(f"{ts},0,0,{net}\n")
    return path


def test_funding_stats_gap_breaks_duration(tmp_path):
    # 0/1 时与 3/4 时两段达标，中间停机 1 小时空洞：持续中位
    # 必须是 2，不是把四行算成连续 4 小时
    _write_funding_csv(tmp_path, [(0, 5.0), (3600, 5.0),
                                  (10800, 5.0), (14400, 5.0)])
    lib = StatsLibrary(tmp_path, FeeTable())
    st = lib.funding_stats("arcus", "lighter", "BTC",
                           min_line_bps=1.0, now=20000)
    assert st is not None
    assert st["sample_hours"] == 4
    assert st["median_duration_h"] == 2.0


def test_funding_stats_duplicate_hour_rows_deduped(tmp_path):
    # 同一小时重复落盘（重启 flush）不虚增样本与持续时长
    _write_funding_csv(tmp_path, [(0, 5.0), (3600, 5.0), (3600, 5.0),
                                  (7200, 5.0)])
    lib = StatsLibrary(tmp_path, FeeTable())
    st = lib.funding_stats("arcus", "lighter", "BTC",
                           min_line_bps=1.0, now=20000)
    assert st is not None
    assert st["sample_hours"] == 3
    assert st["median_duration_h"] == 3.0


# ── B-F1：recorder 整分钟坏盘口不崩 ──

def test_aggregator_bad_book_minute_then_good_sample():
    agg = MinuteAggregator()
    # 第 0 分钟整分钟坏盘口（价格全 0，无有效样本）
    assert agg.add(0, 0.0, 0.0, 0.0, 0.0) is None
    assert agg.add(30, 0.0, 0.0, 0.0, 0.0) is None
    # 第 1 分钟来正常样本：此前对空样本 _bar 解包直接抛错
    assert agg.add(61, 100.0, 101.0, 100.0, 101.0) is None
    bar = agg.flush()
    assert bar is not None and bar["minute_ts"] == 60


# ── B-F2：资金费参数校验补齐 ──

def test_carry_params_rejects_bad_leverage_and_symbol():
    with pytest.raises(ValueError):
        CarryParams(venue_a="arcus", venue_b="lighter", symbol="BTC",
                    notional_usd=100, leverage=0)
    with pytest.raises(ValueError):
        CarryParams(venue_a="arcus", venue_b="lighter", symbol="",
                    notional_usd=100)


# ── B-F3：溢价 taker 任一腿被拒不设结算宽限 ──

class _StubExecutor:
    def __init__(self, reject_venue=None):
        self.reject_venue = reject_venue

    def estimate_open_corridor(self, leverage, mmr):
        return 10.0

    def market_mmr(self, venue, symbol):
        return 0.001

    def place(self, owner, venue, req, intent=None, corridor_pct=None,
              **kw):
        status = "rejected" if venue == self.reject_venue else "filled"
        return Order(order_id="x1", symbol=req.symbol, side=req.side,
                     qty=req.qty, price=None, status=status)


def _premium_strategy(reject_venue=None):
    p = PremiumParams(base_venue="arcus", hedge_venue="lighter",
                      symbol="BTC", midline_bps=0.0, upper_bps=10.0,
                      lower_bps=10.0, settle_grace_seconds=20.0)
    return PremiumArbStrategy(p, executor=_StubExecutor(reject_venue),
                              owner="t")


def test_fire_taker_rejection_skips_settle_grace():
    s = _premium_strategy(reject_venue="lighter")
    s._fire_taker("sell_base", 1.0, now=100.0)
    # 被拒不设宽限：_settle_until == now，对账下一 tick 立即介入
    assert s._settle_until == 100.0


def test_fire_taker_normal_sets_settle_grace():
    s = _premium_strategy()
    s._fire_taker("sell_base", 1.0, now=100.0)
    assert s._settle_until == 120.0


# ── B-F4（溢价 manual 平仓带）：现行行为有专门测试锁定
# （test_run_modes.test_premium_close_band_sets_cycle_done_in_manual），
# 与定案表述的张力已列待用户定，本轮不改、不另加测试 ──


# ── S2：analyze 分位数两端同口径 ──

def test_analyze_percentiles_single_row():
    res = analyze_rows([{"minute_ts": 0, "premium_mean_bps": 42.0,
                         "sell_edge_mean_bps": 42.0,
                         "sell_edge_max_bps": 42.0,
                         "buy_edge_mean_bps": -42.0,
                         "buy_edge_max_bps": -42.0}],
                       fees_bps=10.0)
    assert res["premium_p10_bps"] == 42.0
    assert res["premium_p90_bps"] == 42.0
