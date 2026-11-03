"""集成测试：paper 场所端到端跑三策略（不联网、不用真密钥）。"""

import pytest

from detsp.core.config import build_config
from detsp.core.engine import Engine
from detsp.core.ledger import Ledger
from detsp.venues.paper import PaperVenue

BTC = {"BTC": 60_000.0}


def grid_yaml(action="close"):
    return {"tasks": [{
        "name": "grid-btc", "strategy": "grid", "venue": "arcus",
        "symbol": "BTC",
        "params": {"lower": 55_000, "upper": 65_000, "grid_count": 10,
                   "qty_per_grid": 0.01, "mode": "neutral",
                   "out_of_range_action": action}}]}


def rotation_yaml(maker=False):
    return {"tasks": [{
        "name": "hedge", "strategy": "rotation_hedge",
        "params": {"symbol": "BTC", "venue_a": "arcus", "venue_b": "lighter",
                   "notional_usd": 300, "hold_seconds": 60,
                   "cooldown_seconds": 60, "leverage_a": 5, "leverage_b": 5,
                   "arcus_maker": maker}}]}


def arb_yaml():
    return {"tasks": [{
        "name": "arb", "strategy": "premium_arb",
        "params": {"symbol": "SNDK", "base_venue": "entropy",
                   "hedge_venue": "lighter_rh", "midline_bps": 0,
                   "upper_bps": 50, "lower_bps": 50, "persist_seconds": 0,
                   "max_position_usd": 1000, "max_order_notional_usd": 500,
                   "reconcile_seconds": 15}}]}


def build(yaml_data, venues):
    cfg = build_config(env={}, yaml_data=yaml_data)
    ledger = Ledger()
    engine = Engine.from_config(cfg, venues, ledger=ledger)
    return engine, ledger


def two_btc_venues(lev=5.0):
    a = PaperVenue(venue_id="arcus", prices=dict(BTC), leverage=lev)
    b = PaperVenue(venue_id="lighter", prices=dict(BTC), leverage=lev)
    a.set_funding("BTC", 0.0001)   # 低费率 → 做多
    b.set_funding("BTC", 0.0005)   # 高费率 → 做空
    return {"arcus": a, "lighter": b}


# ---------------- 网格 ----------------

def test_grid_lifecycle_fill_and_counter():
    v = PaperVenue(venue_id="arcus", prices=dict(BTC))
    engine, ledger = build(grid_yaml(), {"arcus": v})
    assert len(engine.tasks) == 1
    assert len(v.open_orders("BTC")) == 10  # 下方 5 买 + 上方 5 卖

    v.set_price("BTC", 58_900)   # 跌穿 59000 买单
    engine.tick(now=1)
    pos = engine.executor.position("arcus", "BTC")
    assert pos is not None and pos.qty == pytest.approx(0.01)
    assert len(v.open_orders("BTC")) == 10  # 成交 1 单 + 反向卖单补回

    v.set_price("BTC", 60_100)   # 涨穿 60000 反向卖单
    engine.tick(now=2)
    assert engine.executor.position("arcus", "BTC") is None
    assert ledger.count("grid-btc") >= 2


def test_grid_out_of_range_close():
    v = PaperVenue(venue_id="arcus", prices=dict(BTC))
    engine, _ = build(grid_yaml("close"), {"arcus": v})
    v.set_price("BTC", 58_900)
    engine.tick(now=1)
    assert engine.executor.position("arcus", "BTC") is not None
    v.set_price("BTC", 66_000)   # 冲破上界
    engine.tick(now=2)
    assert engine.executor.position("arcus", "BTC") is None
    assert v.open_orders("BTC") == []
    assert engine.tasks[0].runtime.stopped is True


def test_grid_out_of_range_recover_only_reduce():
    v = PaperVenue(venue_id="arcus", prices=dict(BTC))
    engine, _ = build(grid_yaml("recover"), {"arcus": v})
    v.set_price("BTC", 58_900)
    engine.tick(now=1)
    assert engine.executor.position("arcus", "BTC") is not None

    v.set_price("BTC", 54_000)   # 跌破下界 → 只减仓回收
    engine.tick(now=2)
    rt = engine.tasks[0].runtime
    assert rt.recovering is True
    opens = v.open_orders("BTC")
    assert opens and all(o.reduce_only for o in opens)
    # 回收阶梯成交后回区间：仓位回收完毕且网格恢复
    v.set_price("BTC", 60_000)
    engine.tick(now=3)
    assert engine.executor.position("arcus", "BTC") is None
    assert rt.recovering is False
    assert len(v.open_orders("BTC")) == 10


def test_grid_refill_only_on_manual_request():
    v = PaperVenue(venue_id="arcus", prices=dict(BTC))
    engine, _ = build(grid_yaml(), {"arcus": v})
    rt = engine.tasks[0].runtime
    oid = v.open_orders("BTC")[0].order_id
    v.cancel_order(oid)  # 模拟外部撤单造成空缺
    engine.tick(now=1)
    assert len(v.open_orders("BTC")) == 9   # 不自动补格
    rt.request_refill()
    engine.tick(now=2)
    assert len(v.open_orders("BTC")) == 10  # 人工触发后补回


# ---------------- 轮换对冲 ----------------

def test_rotation_full_round_trip():
    venues = two_btc_venues()
    engine, ledger = build(rotation_yaml(), venues)
    engine.tick(now=1000)
    pa = engine.executor.position("arcus", "BTC")
    pb = engine.executor.position("lighter", "BTC")
    assert pa.qty == pytest.approx(0.005)    # 低费率所做多
    assert pb.qty == pytest.approx(-0.005)   # 高费率所做空
    engine.tick(now=1030)
    assert engine.executor.position("arcus", "BTC") is not None
    engine.tick(now=1061)                    # 到期轮换双平
    assert engine.executor.position("arcus", "BTC") is None
    assert engine.executor.position("lighter", "BTC") is None
    assert ledger.count("hedge") == 4


def test_rotation_maker_leg_then_hedge():
    venues = two_btc_venues()
    venues["arcus"].set_funding("BTC", 0.0005)  # 改成 arcus 高费率 → arcus 做空腿
    venues["lighter"].set_funding("BTC", 0.0001)
    engine, _ = build(rotation_yaml(maker=True), venues)
    engine.tick(now=1000)
    rt = engine.tasks[0].runtime
    assert rt.phase == "opening"             # arcus maker 挂单中
    assert len(venues["arcus"].open_orders("BTC")) == 1
    venues["arcus"].set_price("BTC", 60_100)  # 涨穿 maker 卖单
    engine.tick(now=1001)
    assert rt.phase == "open"
    assert engine.executor.position("arcus", "BTC").qty == pytest.approx(-0.005)
    assert engine.executor.position("lighter", "BTC").qty == pytest.approx(0.005)


def test_rotation_orphan_leg_closed():
    venues = two_btc_venues()
    engine, _ = build(rotation_yaml(), venues)
    engine.tick(now=1000)
    # 外部把 lighter 腿平掉（模拟单腿故障）
    from detsp.core.venue import OrderRequest, Side
    venues["lighter"].place_order(OrderRequest("BTC", Side.BUY, 0.005))
    assert engine.executor.position("lighter", "BTC") is None
    engine.tick(now=1001)
    # 孤腿（arcus 多头）被无条件平掉
    assert engine.executor.position("arcus", "BTC") is None


def test_rotation_post_open_review_closes_when_actual_leverage_higher():
    # 参数杠杆 5 → 开仓前估算距离 20% 放行；但场所实际持仓杠杆 100
    # → 复核距离约 1% < 3%，开仓后应立即双平
    venues = two_btc_venues(lev=100.0)
    engine, _ = build(rotation_yaml(), venues)
    engine.tick(now=1000)
    assert engine.executor.position("arcus", "BTC") is None
    assert engine.executor.position("lighter", "BTC") is None
    assert engine.tasks[0].runtime.phase == "cooldown"


def test_rotation_preopen_estimate_blocks_extreme_leverage():
    # 杠杆 100 → 开仓后强平距离约 1% < 门槛 3%，复核应立即双平
    venues = two_btc_venues(lev=100.0)
    y = rotation_yaml()
    y["tasks"][0]["params"]["leverage_a"] = 100
    y["tasks"][0]["params"]["leverage_b"] = 100
    engine, _ = build(y, venues)
    # 开仓前估算同样会拦下：100/100=1% < 3%，根本不开仓
    engine.tick(now=1000)
    assert engine.executor.position("arcus", "BTC") is None
    assert engine.executor.position("lighter", "BTC") is None


# ---------------- 溢价套利 ----------------

def arb_venues():
    base = PaperVenue(venue_id="entropy", prices={"SNDK": 100.0})
    hedge = PaperVenue(venue_id="lighter_rh", prices={"SNDK": 99.0})
    return {"entropy": base, "lighter_rh": hedge}


def test_premium_arb_round_trip():
    venues = arb_venues()
    engine, ledger = build(arb_yaml(), venues)
    engine.tick(now=0)   # 溢价约 +99bps ≥ 50：卖 base 买 hedge
    assert engine.executor.position("entropy", "SNDK").qty == pytest.approx(-5)
    assert engine.executor.position("lighter_rh", "SNDK").qty == pytest.approx(5)
    venues["lighter_rh"].set_price("SNDK", 101.0)  # 溢价反转为负 → 反向往返
    engine.tick(now=1)
    assert engine.executor.position("entropy", "SNDK") is None
    assert engine.executor.position("lighter_rh", "SNDK") is None
    assert ledger.count("arb") == 4


def test_premium_arb_delta_correction_on_reconcile():
    venues = arb_venues()
    engine, ledger = build(arb_yaml(), venues)
    engine.tick(now=0)
    # 外部在 base 腿加仓造成净 delta 不平
    from detsp.core.venue import OrderRequest, Side
    venues["entropy"].place_order(OrderRequest("SNDK", Side.BUY, 2.0))
    engine.tick(now=20)  # 对账周期到：修正 + 信号可能再开仓，但净 delta 必须为 0
    qa = engine.executor.position("entropy", "SNDK").qty
    qb = engine.executor.position("lighter_rh", "SNDK").qty
    assert qa + qb == pytest.approx(0.0)


# ---------------- 引擎级 ----------------

def test_conflict_task_rejected_at_config_level():
    """H4：同 (venue,symbol) 被两个任务占用，config 校验层直接报错。"""
    from detsp.core.config import ConfigError

    y = grid_yaml()
    y["tasks"].append(rotation_yaml()["tasks"][0])  # 同样占用 arcus BTC
    with pytest.raises(ConfigError):
        build_config(env={}, yaml_data=y)


def test_conflict_task_isolated_at_start():
    """绕过 config 校验直接组装时，registry claim 仍在引擎层隔离冲突任务。"""
    from detsp.core.config import AppConfig

    venues = two_btc_venues()
    y = grid_yaml()
    y["tasks"].append(rotation_yaml()["tasks"][0])  # 同样占用 arcus BTC
    cfg = AppConfig(tasks=y["tasks"])
    ledger = Ledger()
    engine = Engine.from_config(cfg, venues, ledger=ledger)
    assert len(engine.tasks) == 1                    # 网格照常启动
    assert "hedge" in engine.errors                  # 对冲任务被拦截并记录
    assert len(venues["arcus"].open_orders("BTC")) == 10


def test_kill_switch_flattens_everything():
    venues = two_btc_venues()
    engine, ledger = build(rotation_yaml(), venues)
    engine.tick(now=1000)
    assert engine.executor.position("arcus", "BTC") is not None
    engine.kill_switch.halt("测试停机")
    engine.tick(now=1001)
    assert engine.executor.position("arcus", "BTC") is None
    assert engine.executor.position("lighter", "BTC") is None
    n = ledger.count()
    engine.tick(now=1002)                            # 停机后不再有任何动作
    assert ledger.count() == n


def test_repeated_task_errors_disable_and_halt():
    v = PaperVenue(venue_id="arcus", prices=dict(BTC))
    engine, _ = build(grid_yaml(), {"arcus": v})
    rt = engine.tasks[0].runtime

    def boom(now, allow_open=True):
        raise RuntimeError("模拟故障")

    rt.tick = boom
    for i in range(3):
        engine.tick(now=i)
    assert engine.tasks[0].disabled is True
    assert engine.kill_switch.halted is True
