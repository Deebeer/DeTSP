"""审查修复回归测试（2026-11-04 三批全修）。

覆盖：费率闸门/口径（H1/H2）、费率周期自检、caps 策略层双腿合计
（M5）、削腿滑点保护（对照 M2）、市价单成交发现补记账（M2）、
引擎停用收尾（G2）与停机异常不计错（L3）、结算宽限期（G1）、
PremiumParams 校验（M4）、网格 skipBand/保证金预检/价格 sanity/
增量记账（M3）/撤单风暴、Arcus fills 逐单匹配（对照 M5）、
analyze 触发计数 max 口径（对照 L4）、预检资金费专项。
"""

import time

import pytest

from detsp.core.config import VENUE_IDS, build_config
from detsp.core.engine import Engine, Task
from detsp.core.execution import Caps, Executor
from detsp.core.ledger import Ledger
from detsp.core.preflight import run_preflight
from detsp.core.registry import OwnershipRegistry
from detsp.core.risk import (
    KillSwitch,
    RiskHaltError,
    detect_period_anomaly,
    evaluate_funding_pair,
)
from detsp.core.venue import FundingInfo, Order, OrderRequest, Side
from detsp.strategies.funding_carry import CarryParams, FundingCarryStrategy
from detsp.strategies.hedged_grid import (
    HedgedGridParams,
    HedgedGridStrategy,
    PlannedOrder,
    counter_order,
    plan_initial_orders,
)
from detsp.strategies.premium_arb import PremiumParams
from detsp.strategies.rotation_hedge import (
    RotationHedgeStrategy,
    RotationParams,
)
from detsp.tools.analyze import analyze_rows
from detsp.venues import _arcus_sign as ax
from detsp.venues.arcus import ArcusVenue
from detsp.venues.paper import PaperVenue

BTC = {"BTC": 60_000.0}


def _executor(venues, **kw):
    return Executor(venues=venues, registry=OwnershipRegistry(),
                    ledger=Ledger(), kill_switch=KillSwitch(), **kw)


# ═══════════ 费率闸门（H1/H2） ═══════════

def _info(rate, kind="predicted", interval=3600.0):
    return FundingInfo(symbol="BTC", rate=rate,
                       interval_seconds=interval, rate_kind=kind)


def test_funding_gate_rejects_suspect_rate():
    ok, reason, _, _ = evaluate_funding_pair(_info(0.06), _info(0.001))
    assert not ok and "5%" in reason
    # 归一到每小时再大也不等于单期异常：8 小时 1% 是正常量级
    ok, _, ha, hb = evaluate_funding_pair(
        _info(0.01, interval=28800.0), _info(0.0001))
    assert ok and ha == pytest.approx(0.01 / 8)


def test_funding_gate_rejects_mismatched_kinds():
    ok, reason, _, _ = evaluate_funding_pair(
        _info(0.001, kind="predicted"), _info(0.001, kind="last_settled"))
    assert not ok and "口径不一致" in reason
    ok, _, _, _ = evaluate_funding_pair(
        _info(0.001, kind="current"), _info(0.002, kind="current"))
    assert ok


def test_period_anomaly_detector():
    floor = [FundingInfo(symbol=f"S{i}", rate=0.0001,
                         interval_seconds=3600.0) for i in range(10)]
    assert detect_period_anomaly(floor) is not None  # 地板年化 87%>15%
    varied = [FundingInfo(symbol=f"S{i}", rate=0.001 * (i + 1),
                          interval_seconds=3600.0) for i in range(10)]
    assert detect_period_anomaly(varied) is None
    assert detect_period_anomaly(floor[:5]) is None   # 样本不足不下结论


def _carry_venues(fa=0.0001, fb=0.0005, kind_b="predicted"):
    a = PaperVenue(venue_id="arcus", prices=dict(BTC), leverage=5.0)
    b = PaperVenue(venue_id="lighter", prices=dict(BTC), leverage=5.0)
    a.set_funding("BTC", fa)
    b.set_funding("BTC", fb, rate_kind=kind_b)
    return {"arcus": a, "lighter": b}


def _carry(venues, **kw):
    base = dict(venue_a="arcus", venue_b="lighter", symbol="BTC",
                notional_usd=300, leverage=5.0,
                min_entry_edge_bps_per_hour=2.0,
                exit_edge_bps_per_hour=1.0, check_seconds=10)
    base.update(kw)
    ex = _executor(venues)
    s = FundingCarryStrategy(CarryParams(**base), executor=ex, owner="carry")
    s.start(now=0)
    return s, ex


def test_carry_gate_blocks_suspect_and_mismatched():
    s, ex = _carry(_carry_venues(fb=0.06))     # 单期 6% → 数据错误
    s.tick(now=1000)
    assert s.phase == "flat" and "费率" in s.last_error
    assert ex.position("arcus", "BTC") is None

    s2, ex2 = _carry(_carry_venues(kind_b="last_settled"))  # 口径不一致
    s2.tick(now=1000)
    assert s2.phase == "flat" and "口径不一致" in s2.last_error
    assert ex2.position("arcus", "BTC") is None


def test_rotation_gate_blocks_suspect():
    venues = _carry_venues(fb=0.06)
    ex = _executor(venues)
    p = RotationParams(symbol="BTC", venue_a="arcus", venue_b="lighter",
                       notional_usd=300, leverage_a=5, leverage_b=5)
    s = RotationHedgeStrategy(p, executor=ex, owner="rot")
    s.start(now=0)
    s.tick(now=0)
    assert s.phase == "idle" and "费率" in s.last_error
    assert ex.position("arcus", "BTC") is None


# ═══════════ caps 策略层双腿合计（M5） ═══════════

def test_strategy_cap_counts_both_legs():
    venues = {"arcus": PaperVenue(venue_id="arcus", prices=dict(BTC)),
              "lighter": PaperVenue(venue_id="lighter", prices=dict(BTC))}
    ex = _executor(venues, caps=Caps(strategy={"s": 90_000.0}))
    ex.claim("s", "arcus", "BTC")
    ex.claim("s", "lighter", "BTC")
    # 第一腿 1.0 BTC = 60000 ≤ 90000 放行
    r1 = ex.place("s", "arcus", OrderRequest("BTC", Side.BUY, 1.0),
                  intent="open")
    assert r1.status == "filled"
    # 第二腿再 60000：合计 120000 > 90000 → 拒绝（旧口径只看单腿会放行）
    r2 = ex.place("s", "lighter", OrderRequest("BTC", Side.SELL, 1.0),
                  intent="open")
    assert r2.status == "rejected"
    assert ex.position("lighter", "BTC") is None


# ═══════════ 削腿滑点保护（对照 M2） ═══════════

def test_trim_excess_limit_and_inflight_skip():
    v = PaperVenue(venue_id="arcus", prices=dict(BTC))
    venues = {"arcus": v}
    ex = _executor(venues)
    ex.claim("s", "arcus", "BTC")
    ex.place("s", "arcus", OrderRequest("BTC", Side.BUY, 0.01), intent="open")
    # 削 0.005：带滑点上限的限价卖单，paper 盘口内立即成交
    r = ex.trim_excess("s", "arcus", "BTC", Side.SELL, 0.005,
                       max_slippage_bps=20.0)
    assert r.order is not None and r.order.reduce_only
    assert ex.position("arcus", "BTC").qty == pytest.approx(0.005)
    # 已有本策略在途削腿单（挂在远价的 reduce-only 单）→ 跳过不叠加
    ex.place("s", "arcus",
             OrderRequest("BTC", Side.SELL, 0.005, price=999_999.0,
                          reduce_only=True), intent="close")
    before = len(v.open_orders("BTC"))
    r2 = ex.trim_excess("s", "arcus", "BTC", Side.SELL, 0.004,
                        max_slippage_bps=20.0)
    assert r2.status == "open" and len(v.open_orders("BTC")) == before


# ═══════════ 市价单成交发现补记账（M2） ═══════════

class _AsyncVenue(PaperVenue):
    """回执未终结（open/0），成交只反映在持仓里；poll 不可得。"""

    poll_available = False

    def place_order(self, req):
        o = super().place_order(req)
        if req.price is None and o.status == "filled":
            return Order(order_id=o.order_id, symbol=o.symbol, side=o.side,
                         qty=o.qty, price=o.price, filled_qty=0.0,
                         status="open")
        return o

    def poll_order(self, order_id):
        if not self.poll_available:
            return None
        return super().poll_order(order_id)


def test_pending_fill_discovered_by_position_delta():
    v = _AsyncVenue(venue_id="lighter", prices=dict(BTC))
    ex = _executor({"lighter": v})
    res = ex.place("s", "lighter", OrderRequest("BTC", Side.BUY, 0.01),
                   intent="open")
    assert res.status == "open"          # 回执未终结，进待发现
    assert ex.ledger.count("s") == 0     # 尚未记账
    # 截止期后 poll 仍不可得、订单不在挂单中 → 持仓差定案补记
    ex.poll_pending_fills(now=time.time() + 1000)
    assert ex.ledger.count("s") == 1
    # 记账量为请求量、参考价为下单时对手价（ask≈60006），量级对即可
    assert ex.ledger.total_volume("s") == pytest.approx(600.0, rel=0.01)
    assert not ex.has_pending_fill("lighter", "BTC")


def test_pending_fill_discovered_by_poll():
    v = _AsyncVenue(venue_id="lighter", prices=dict(BTC))
    v.poll_available = True
    ex = _executor({"lighter": v})
    ex.place("s", "lighter", OrderRequest("BTC", Side.BUY, 0.01),
             intent="open")
    ex.poll_pending_fills()              # poll 终态即可定案，不等截止
    assert ex.ledger.count("s") == 1


# ═══════════ 引擎：停用收尾（G2）/ 停机异常不计错（L3） ═══════════

class _StubRuntime:
    def __init__(self, exc=None):
        self.exc = exc
        self.stopped_called = False

    def tick(self, now, allow_open=True):
        if self.exc is not None:
            raise self.exc

    def stop(self):
        self.stopped_called = True


def _engine_with(runtime):
    eng = Engine(tasks=[Task(name="t", strategy_type="stub",
                             runtime=runtime)])
    return eng


def test_engine_disables_and_stops_after_error_threshold():
    rt = _StubRuntime(RuntimeError("boom"))
    eng = _engine_with(rt)
    for i in range(3):
        eng.tick(now=float(i))
    task = eng.tasks[0]
    assert task.disabled and rt.stopped_called
    assert eng.kill_switch.halted
    assert any("stop() 收尾" in e for e in eng.events)


def test_engine_halt_error_not_counted():
    rt = _StubRuntime(RiskHaltError("全局停机中"))
    eng = _engine_with(rt)
    for i in range(5):
        eng.tick(now=float(i))
    task = eng.tasks[0]
    assert not task.disabled and task.errors.count == 0
    assert not eng.kill_switch.halted


# ═══════════ 结算宽限期（G1） ═══════════

class _DelayedVenue(PaperVenue):
    """成交即时发生，但持仓延迟可见（模拟 Lighter 异步结算）。"""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.hide_positions = True

    def positions(self):
        return [] if self.hide_positions else super().positions()


def test_rotation_grace_waits_for_async_leg():
    a = PaperVenue(venue_id="arcus", prices=dict(BTC), leverage=5.0)
    b = _DelayedVenue(venue_id="lighter", prices=dict(BTC), leverage=5.0)
    a.set_funding("BTC", 0.0001)
    b.set_funding("BTC", 0.0005)
    ex = _executor({"arcus": a, "lighter": b})
    p = RotationParams(symbol="BTC", venue_a="arcus", venue_b="lighter",
                       notional_usd=300, leverage_a=5, leverage_b=5)
    s = RotationHedgeStrategy(p, executor=ex, owner="rot")
    s.start(now=0)
    s.tick(now=0)
    # lighter 腿已成交但持仓不可见：宽限期内不按孤腿平掉 arcus 腿
    assert s.phase == "opening"
    assert ex.position("arcus", "BTC") is not None
    b.hide_positions = False
    s.tick(now=5)                        # 宽限内补齐 → 定案 open
    assert s.phase == "open"
    assert ex.position("lighter", "BTC") is not None


def test_carry_grace_waits_for_async_leg():
    venues = _carry_venues()
    venues["lighter"] = _DelayedVenue(venue_id="lighter", prices=dict(BTC),
                                      leverage=5.0)
    venues["lighter"].set_funding("BTC", 0.0005)
    s, ex = _carry(venues)
    s.tick(now=1000)
    assert s.phase == "opening"          # 不立即按孤腿平掉
    assert ex.position("arcus", "BTC") is not None
    venues["lighter"].hide_positions = False
    s.tick(now=1001)
    assert s.phase == "open"


def test_rotation_adopts_position_found_in_idle():
    venues = _carry_venues()
    ex = _executor(venues)
    p = RotationParams(symbol="BTC", venue_a="arcus", venue_b="lighter",
                       notional_usd=300, leverage_a=5, leverage_b=5)
    s = RotationHedgeStrategy(p, executor=ex, owner="rot")
    s.start(now=0)
    # 外部在两腿各开一份对冲仓（idle 阶段出现仓位）
    venues["arcus"].place_order(OrderRequest("BTC", Side.BUY, 0.005))
    venues["lighter"].place_order(OrderRequest("BTC", Side.SELL, 0.005))
    s.tick(now=50)
    assert s.phase == "open" and s.opened_at == 50
    assert any("接管" in n for n in s.recovery_notes)


# ═══════════ PremiumParams 校验（M4） ═══════════

def test_premium_params_validation():
    base = dict(midline_bps=5.0, upper_bps=10.0, lower_bps=10.0,
                base_venue="entropy", hedge_venue="lighter")
    PremiumParams(**base)                # 正常构造不抛
    with pytest.raises(ValueError):
        PremiumParams(**{**base, "hedge_venue": "entropy"})   # 两腿同所
    with pytest.raises(ValueError):
        PremiumParams(**{**base, "leverage_base": 0})
    with pytest.raises(ValueError):
        PremiumParams(**{**base, "base_taker_fee_bps": -1})
    with pytest.raises(ValueError):
        PremiumParams(**{**base, "persist_seconds": -0.1})
    with pytest.raises(ValueError):
        PremiumParams(**{**base, "max_position_usd": 0})
    with pytest.raises(ValueError):
        PremiumParams(**{**base, "hedge_venue": ""})          # 只给一腿


# ═══════════ 中性网格：skipBand / 预检 / 增量记账 / 撤单风暴 ═══════════

def _grid_params(**kw):
    base = dict(grid_venue="arcus", hedge_venue="lighter", symbol="BTC",
                lower_price=90.0, upper_price=110.0, grid_count=10,
                qty_per_grid=0.1)
    base.update(kw)
    return HedgedGridParams(**base)


def test_skip_band_and_counter_qty():
    p = _grid_params()                   # 格距 2，带宽 0.5
    prices = [o.price for o in plan_initial_orders(p, 100.4)]
    assert 100.0 not in prices           # 距现价 0.4 < 0.5 → 跳档
    assert 102.0 in prices and 98.0 in prices
    p0 = _grid_params(skip_band_frac=0.0)
    assert 100.0 in [o.price for o in plan_initial_orders(p0, 100.4)]
    # 反向单按实际成交量镜像
    assert counter_order(p, 100.0, Side.BUY, qty=0.03).qty == 0.03
    assert counter_order(p, 100.0, Side.BUY).qty == 0.1   # 默认整格量


def _grid_strategy(balance=10_000.0, price=100.0, **kw):
    a = PaperVenue(venue_id="arcus", prices={"BTC": price},
                   balance_usd=balance)
    b = PaperVenue(venue_id="lighter", prices={"BTC": price},
                   balance_usd=balance)
    ex = _executor({"arcus": a, "lighter": b})
    s = HedgedGridStrategy(_grid_params(**kw), executor=ex, owner="hg")
    return s, ex, a, b


def test_grid_margin_precheck_and_price_sanity():
    s, *_ = _grid_strategy(balance=50.0)  # 单腿名义 100 > 可用 50
    with pytest.raises(RuntimeError, match="保证金预检"):
        s.start()
    s2, *_ = _grid_strategy(price=300.0)  # 现价 > upper×2=220
    with pytest.raises(RuntimeError, match="sanity"):
        s2.start()


def test_grid_incremental_recording_no_double_count():
    s, ex, a, _ = _grid_strategy()
    s.start(now=0)
    oid, po = next((k, v) for k, v in s._orders.items()
                   if v.side is Side.BUY)
    s._recorded[oid] = 0.04              # place 回执已记部分成交
    orig_poll = a.poll_order
    a.poll_order = lambda q: Order(
        order_id=oid, symbol="BTC", side=po.side, qty=po.qty,
        price=po.price, filled_qty=0.1, status="filled") if q == oid \
        else orig_poll(q)
    s._process_fills(now=1)
    # 账本只应记增量 0.06（0.1 − 已记 0.04），不是全量 0.1
    fills = [f for f in ex.ledger.recent_fills(100) if f["strategy"] == "hg"]
    assert sum(f["qty"] for f in fills) == pytest.approx(0.06)


def test_grid_canceled_partial_records_and_counters():
    s, ex, a, _ = _grid_strategy()
    s.start(now=0)
    oid, po = next((k, v) for k, v in s._orders.items()
                   if v.side is Side.SELL)
    orig_poll = a.poll_order
    a.poll_order = lambda q: Order(
        order_id=oid, symbol="BTC", side=po.side, qty=po.qty,
        price=po.price, filled_qty=0.03, status="canceled") if q == oid \
        else orig_poll(q)
    s._process_fills(now=1)
    fills = [f for f in ex.ledger.recent_fills(100) if f["strategy"] == "hg"]
    assert sum(f["qty"] for f in fills) == pytest.approx(0.03)
    # 已成部分挂了按量镜像的反向单（卖成 0.03 → 下一格买 0.03）
    counters = [o for o in a.open_orders("BTC")
                if o.side is Side.BUY and o.qty == pytest.approx(0.03)]
    assert counters


def test_grid_cancel_storm_pauses_placement():
    s, ex, a, _ = _grid_strategy()
    s.start(now=0)
    targets = list(s._orders.items())[:5]
    orig_poll = a.poll_order
    bad = {k for k, _ in targets}
    a.poll_order = lambda q: Order(
        order_id=q, symbol="BTC", side=s._orders[q].side, qty=0.1,
        price=100.0, filled_qty=0.0, status="canceled") if q in bad \
        else orig_poll(q)
    s._process_fills(now=1)              # 60 秒内 5 次被撤 → 暂停
    assert s._pause_until >= 61
    n0 = len(a.open_orders("BTC"))
    s._place(PlannedOrder(price=95.0, side=Side.BUY, qty=0.1), now=10)
    assert len(a.open_orders("BTC")) == n0      # 开仓类铺单被暂停拦住
    s._place(PlannedOrder(price=95.0, side=Side.SELL, qty=0.1,
                          reduce_only=True), now=10)
    assert len(a.open_orders("BTC")) == n0 + 1  # 只减仓单不拦


# ═══════════ Arcus fills 逐单匹配（对照 M5） ═══════════

def test_arcus_match_fills_pure():
    rows = [
        {"orderId": "9", "tradeId": "t1", "price": "100", "size": "0.5",
         "fee": "0.1", "closedPnl": "0", "side": "BUY"},
        {"orderId": "9", "tradeId": "t2", "price": "102", "size": "0.5",
         "fee": "0.1", "closedPnl": "1.0", "side": "BUY"},
        {"orderId": "9", "tradeId": "t2", "price": "102", "size": "0.5",
         "fee": "0.1", "closedPnl": "1.0", "side": "BUY"},   # 重复行去重
        {"orderId": "8", "tradeId": "t3", "price": "50", "size": "1",
         "fee": "0", "closedPnl": "0", "side": "SELL"},
    ]
    qty, avg, fee, pnl, side, n = ax.match_fills(rows, "9")
    assert qty == pytest.approx(1.0) and avg == pytest.approx(101.0)
    assert fee == pytest.approx(0.2) and n == 2 and side == "buy"
    # closedPnl 已扣费，返回的盈亏加回手续费避免双扣
    assert pnl == pytest.approx(1.2)
    assert ax.match_fills(rows, "不存在") is None
    rows_c = [{"clientId": "c1", "tradeId": "x", "price": "10",
               "size": "2", "fee": "0", "closedPnl": "0", "side": "SELL"}]
    assert ax.match_fills(rows_c, "cid:c1")[0] == pytest.approx(2.0)


def test_arcus_poll_order_uses_fills():
    market_row = {
        "type": "PERPETUAL", "status": "ONLINE", "marketId": 3,
        "marketDisplayName": "BTC-USD", "baseAsset": "BTC",
        "stepSize": "0.001", "tickSize": "0.1", "tickTiers": [],
        "minOrderSize": "0.002", "minOrderNotional": "10",
        "maxOrderSize": "100", "initialMarginFraction": "0.05",
        "maintenanceMarginFraction": "0.025",
    }
    fills = [{"orderId": "ord-9", "tradeId": "t1", "price": "60000",
              "size": "0.01", "fee": "0.03", "closedPnl": "0",
              "side": "BUY"}]

    def getter(path, params):
        if path == "/v1/markets":
            return {"markets": [market_row]}
        if path == "/v1/openOrders":
            return {"orders": []}
        if path == "/v1/fills":
            return {"fills": fills}
        raise AssertionError(path)

    v = ArcusVenue(mode="live", address="0x" + "ab" * 20, account_index=0,
                   api_private_key=bytes(range(32)).hex(), getter=getter,
                   poster=lambda *a: (200, {}))
    o = v.poll_order("ord-9")            # 不在挂单中 → fills 匹配定案
    assert o is not None and o.status == "filled"
    assert o.filled_qty == pytest.approx(0.01)
    assert v.poll_order("ord-不存在") is None


# ═══════════ analyze max 口径（对照 L4） ═══════════

def test_analyze_counts_fires_by_minute_max():
    row = {"minute_ts": 0.0, "premium_mean_bps": 0.0,
           "sell_edge_mean_bps": 5.0, "sell_edge_max_bps": 100.0,
           "buy_edge_mean_bps": 5.0, "buy_edge_max_bps": 100.0}
    res = analyze_rows([dict(row, minute_ts=float(i)) for i in range(3)],
                       fees_bps=0.0, min_fires=1)
    band50 = next(t for t in res["table"] if t["band"] == 50)
    # mean 口径下 5 < 50 不触发；max 口径下 100 > 50 每分钟都触发
    assert band50["sell_fires"] == 3 and band50["buy_fires"] == 3


# ═══════════ 预检资金费专项 ═══════════

class _MockVenue:
    def __init__(self, mode="paper"):
        self.mode = mode
        self.venue_id = ""

    def markets(self):
        from detsp.core.venue import Market
        return [Market(symbol="BTC", price_step=0.1, qty_step=0.001,
                       min_qty=0.001, min_notional=10.0, max_leverage=50.0)]

    def balance(self):
        return {"equity": 100.0, "available": 100.0}


def test_preflight_has_funding_items():
    cfg = build_config(env={}, yaml_data={})
    venues = {vid: _MockVenue("paper") for vid in VENUE_IDS}
    rep = run_preflight(cfg, venues)
    items = {(i.venue, i.item): i.status for i in rep.items}
    assert items[("arcus", "费率周期自检")] == "跳过"
    assert items[("arcus", "费率符号抽查")] == "跳过"
