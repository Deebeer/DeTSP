"""集成测试：paper 场所端到端跑策略（不联网、不用真密钥）。"""

import pytest

from detsp.core.config import build_config
from detsp.core.engine import Engine
from detsp.core.ledger import Ledger
from detsp.venues.paper import PaperVenue

BTC = {"BTC": 60_000.0}


def hedged_grid_yaml(action="close"):
    return {"tasks": [{
        "name": "hgrid-btc", "strategy": "hedged_grid",
        "params": {"grid_venue": "arcus", "hedge_venue": "lighter",
                   "symbol": "BTC", "lower_price": 55_000,
                   "upper_price": 65_000, "grid_count": 10,
                   "qty_per_grid": 0.01, "mode": "long_short",
                   "out_of_range_action": action}}]}


def carry_yaml():
    # 资金费套利：占 arcus/lighter 的 BTC（冲突/停机等引擎级
    # 用例借它当第二个任务）
    return {"tasks": [{
        "name": "hedge", "strategy": "funding_carry",
        "run_mode": "semi_fixed",
        "params": {"symbol": "BTC", "venue_a": "arcus",
                   "venue_b": "lighter", "notional_usd": 300,
                   "leverage": 5}}]}


def arb_yaml():
    return {"tasks": [{
        "name": "arb", "strategy": "premium_arb",
        "run_mode": "semi_fixed",
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


def grid_venues():
    return {"arcus": PaperVenue(venue_id="arcus", prices=dict(BTC)),
            "lighter": PaperVenue(venue_id="lighter", prices=dict(BTC))}


# ---------------- 中性网格 ----------------

def test_hedged_grid_lifecycle_fill_counter_and_hedge():
    venues = grid_venues()
    engine, ledger = build(hedged_grid_yaml(), venues)
    v = venues["arcus"]
    assert len(engine.tasks) == 1
    assert len(v.open_orders("BTC")) == 10  # 下方 5 买 + 上方 5 卖

    v.set_price("BTC", 58_900)   # 跌穿 59000 买单
    engine.tick(now=1)
    pos = engine.executor.position("arcus", "BTC")
    assert pos is not None and pos.qty == pytest.approx(0.01)
    # 对冲腿同步卖出等量：组合净 delta 为 0
    hedge = engine.executor.position("lighter", "BTC")
    assert hedge is not None and hedge.qty == pytest.approx(-0.01)
    assert len(v.open_orders("BTC")) == 10  # 成交 1 单 + 反向卖单补回

    v.set_price("BTC", 60_100)   # 涨穿 60000 反向卖单
    engine.tick(now=2)
    assert engine.executor.position("arcus", "BTC") is None
    assert engine.executor.position("lighter", "BTC") is None
    assert ledger.count("hgrid-btc") >= 4   # 网格两笔 + 对冲两笔


def test_hedged_grid_out_of_range_close_flattens_both_legs():
    venues = grid_venues()
    engine, _ = build(hedged_grid_yaml("close"), venues)
    v = venues["arcus"]
    v.set_price("BTC", 58_900)
    engine.tick(now=1)
    assert engine.executor.position("arcus", "BTC") is not None
    assert engine.executor.position("lighter", "BTC") is not None
    v.set_price("BTC", 66_000)   # 冲破上界
    engine.tick(now=2)
    assert engine.executor.position("arcus", "BTC") is None
    assert engine.executor.position("lighter", "BTC") is None
    assert v.open_orders("BTC") == []
    assert engine.tasks[0].runtime.stopped is True


def test_hedged_grid_out_of_range_reduce_only_recovery():
    venues = grid_venues()
    engine, _ = build(hedged_grid_yaml("reduce_only"), venues)
    v = venues["arcus"]
    v.set_price("BTC", 58_900)
    engine.tick(now=1)
    assert engine.executor.position("arcus", "BTC") is not None

    v.set_price("BTC", 54_000)   # 跌破下界 → 网格腿只减仓回收
    engine.tick(now=2)
    rt = engine.tasks[0].runtime
    assert rt.recovering is True
    opens = v.open_orders("BTC")
    assert opens and all(o.reduce_only for o in opens)
    # 回收阶梯成交后回区间：双腿仓位回收完毕且网格恢复
    v.set_price("BTC", 60_000)
    engine.tick(now=3)
    assert engine.executor.position("arcus", "BTC") is None
    assert engine.executor.position("lighter", "BTC") is None
    assert rt.recovering is False
    assert len(v.open_orders("BTC")) == 10


def test_hedged_grid_refill_only_on_manual_request():
    venues = grid_venues()
    engine, _ = build(hedged_grid_yaml(), venues)
    v = venues["arcus"]
    rt = engine.tasks[0].runtime
    oid = v.open_orders("BTC")[0].order_id
    v.cancel_order(oid)  # 模拟外部撤单造成空缺
    engine.tick(now=1)
    assert len(v.open_orders("BTC")) == 9   # 不自动补格
    rt.request_refill()
    engine.tick(now=2)
    assert len(v.open_orders("BTC")) == 10  # 人工触发后补回


# ---------------- 溢价套利（taker 默认模式） ----------------

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
    """同一 (venue,symbol) 被两个任务占用，config 校验层直接报错。"""
    from detsp.core.config import ConfigError

    y = hedged_grid_yaml()
    y["tasks"].append(carry_yaml()["tasks"][0])  # 同样占用 arcus BTC
    with pytest.raises(ConfigError):
        build_config(env={}, yaml_data=y)


def test_removed_strategy_names_rejected_with_guidance():
    """已移除的旧策略名（grid / volume_loop / rotation_hedge）：
    报错必须写明已移除并列出可用策略，config 层与引擎组装层口径一致。"""
    from detsp.core.config import ConfigError
    from detsp.core.engine import build_task

    for old in ("grid", "volume_loop", "rotation_hedge"):
        y = {"tasks": [{"name": "t", "strategy": old, "params": {}}]}
        with pytest.raises(ConfigError, match="已移除"):
            build_config(env={}, yaml_data=y)
        with pytest.raises(ValueError, match="已移除.*hedged_grid"):
            build_task(y["tasks"][0], None, None)


def test_conflict_task_isolated_at_start():
    """绕过 config 校验直接组装时，registry claim 仍在引擎层隔离冲突任务。"""
    from detsp.core.config import AppConfig

    venues = grid_venues()
    y = hedged_grid_yaml()
    y["tasks"].append(carry_yaml()["tasks"][0])  # 同样占用 arcus BTC
    cfg = AppConfig(tasks=y["tasks"])
    ledger = Ledger()
    engine = Engine.from_config(cfg, venues, ledger=ledger)
    assert len(engine.tasks) == 1                    # 中性网格照常启动
    assert "hedge" in engine.errors                  # 对冲任务被拦截并记录
    assert len(venues["arcus"].open_orders("BTC")) == 10


def test_kill_switch_flattens_everything():
    venues = two_btc_venues()
    engine, ledger = build(carry_yaml(), venues)
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
    venues = grid_venues()
    engine, _ = build(hedged_grid_yaml(), venues)
    rt = engine.tasks[0].runtime

    def boom(now, allow_open=True):
        raise RuntimeError("模拟故障")

    rt.tick = boom
    for i in range(3):
        engine.tick(now=i)
    assert engine.tasks[0].disabled is True
    assert engine.kill_switch.halted is True
