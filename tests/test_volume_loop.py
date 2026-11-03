"""单所刷量策略（volume_loop）测试：纯函数 + paper 端到端。

策略语义源自 perp-dex-tools 的 trading_bot.py（非商业许可，
个人自用），按 DeTSP 架构重组后在此锁定行为。
"""

import pytest

from detsp.core.config import ConfigError, build_config
from detsp.core.engine import Engine
from detsp.core.execution import Caps, Executor
from detsp.core.ledger import Ledger
from detsp.strategies.volume_loop import (
    VolumeLoopStrategy,
    VolumeParams,
    effective_wait,
    grid_step_ok,
    maker_open_price,
    price_trigger,
    take_profit_price,
)
from detsp.venues.paper import PaperVenue
from detsp.web.server import PanelState

BTC = {"BTC": 60_000.0}


# ---------------- 纯函数 ----------------

def test_take_profit_price():
    assert take_profit_price(100.0, "buy", 0.5) == pytest.approx(100.5)
    assert take_profit_price(100.0, "sell", 0.5) == pytest.approx(99.5)


def test_maker_open_price():
    # 买单贴 ask 下一 tick、卖单贴 bid 上一 tick（不穿价保 maker）
    assert maker_open_price(99.0, 101.0, "buy", 0.5) == pytest.approx(100.5)
    assert maker_open_price(99.0, 101.0, "sell", 0.5) == pytest.approx(99.5)


def test_effective_wait_tiers():
    assert effective_wait(40, 0, 6) == 10      # 占比 <1/6 → 1/4
    assert effective_wait(40, 1, 6) == 20      # 占比 ≥1/6 → 1/2
    assert effective_wait(40, 2, 6) == 40      # 占比 ≥1/3 → 1 倍
    assert effective_wait(40, 4, 6) == 80      # 占比 ≥2/3 → 2 倍


def test_grid_step_ok():
    # buy：已有止盈卖单最低 101，候选 100.4 → 比值不足 1% 不放行
    assert grid_step_ok([101.0], 100.4, "buy", 1.0) is False
    assert grid_step_ok([101.0], 99.9, "buy", 1.0) is True
    # sell：已有止盈买单最高 99，候选 99.6 → 间距不足
    assert grid_step_ok([99.0], 99.6, "sell", 1.0) is False
    assert grid_step_ok([99.0], 100.1, "sell", 1.0) is True
    # 无已有单 / step=0 恒放行
    assert grid_step_ok([], 100.0, "buy", 1.0) is True
    assert grid_step_ok([101.0], 100.4, "buy", 0.0) is True


def test_price_trigger():
    assert price_trigger(99.0, 101.0, "buy", 100.5) is True   # ask 上穿
    assert price_trigger(99.0, 101.0, "buy", 102.0) is False
    assert price_trigger(99.0, 101.0, "sell", 99.5) is True   # bid 下穿
    assert price_trigger(99.0, 101.0, "sell", 98.0) is False
    assert price_trigger(99.0, 101.0, "buy", 0.0) is False    # 0=禁用


def test_params_validation():
    with pytest.raises(ValueError, match="无止损"):
        VolumeParams(qty=0.01, take_profit_pct=0)
    with pytest.raises(ValueError, match="方向"):
        VolumeParams(qty=0.01, take_profit_pct=1.0, direction="both")
    with pytest.raises(ValueError, match="max_orders"):
        VolumeParams(qty=0.01, take_profit_pct=1.0, max_orders=0)


# ---------------- 引擎集成（paper） ----------------

def vol_yaml(**over):
    params = {"qty": 0.01, "take_profit_pct": 1.0, "direction": "buy",
              "max_orders": 3, "wait_seconds": 0}
    params.update(over)
    return {"tasks": [{"name": "vol-btc", "strategy": "volume_loop",
                       "venue": "lighter", "symbol": "BTC",
                       "params": params}]}


def build(yaml_data, venue):
    cfg = build_config(env={}, yaml_data=yaml_data)
    ledger = Ledger()
    engine = Engine.from_config(cfg, {"lighter": venue}, ledger=ledger)
    assert "vol-btc" not in engine.errors, engine.errors
    return engine, ledger


def test_volume_round_trip_open_fill_take_profit():
    v = PaperVenue(venue_id="lighter", prices=dict(BTC))
    engine, ledger = build(vol_yaml(), v)
    engine.tick(now=100)
    opens = v.open_orders("BTC")
    assert len(opens) == 1 and opens[0].side.value == "buy"
    open_price = opens[0].price

    v.set_price("BTC", 59_900)          # 跌穿开仓买单 → 成交
    engine.tick(now=101)
    pos = engine.executor.position("lighter", "BTC")
    assert pos is not None and pos.qty == pytest.approx(0.01)
    # 止盈卖单已挂：reduce-only、价 = 成交价 ×1.01
    opens = v.open_orders("BTC")
    tps = [o for o in opens if o.reduce_only]
    assert len(tps) == 1 and tps[0].side.value == "sell"
    assert tps[0].price == pytest.approx(open_price * 1.01)

    v.set_price("BTC", 60_700)          # 涨穿止盈价 → 平仓
    engine.tick(now=102)
    assert engine.executor.position("lighter", "BTC") is None
    assert ledger.count("vol-btc") >= 2  # 开仓 + 止盈各一笔


def test_volume_max_orders_blocks_new_open():
    v = PaperVenue(venue_id="lighter", prices=dict(BTC))
    engine, _ = build(vol_yaml(max_orders=1), v)
    engine.tick(now=0)
    v.set_price("BTC", 59_900)
    engine.tick(now=1)                   # 成交 + 止盈挂上（未平批次=1=上限）
    assert engine.executor.position("lighter", "BTC") is not None
    engine.tick(now=2)
    opens = v.open_orders("BTC")
    assert len(opens) == 1 and opens[0].reduce_only  # 只有止盈单，无新开仓


def test_volume_wait_interval_between_opens():
    v = PaperVenue(venue_id="lighter", prices=dict(BTC))
    engine, _ = build(vol_yaml(wait_seconds=100), v)
    engine.tick(now=100)                 # 第一笔开仓
    v.set_price("BTC", 59_900)
    engine.tick(now=101)                 # 成交+止盈；距上次开仓 1s < 100s
    assert len(v.open_orders("BTC")) == 1
    engine.tick(now=150)                 # 50s 仍不足
    assert len(v.open_orders("BTC")) == 1
    engine.tick(now=201)                 # 101s ≥ 有效间隔 → 第二笔开仓
    assert len(v.open_orders("BTC")) == 2


def test_volume_stop_price_exits_and_keeps_take_profit():
    v = PaperVenue(venue_id="lighter", prices=dict(BTC))
    engine, _ = build(vol_yaml(stop_price=60_500), v)
    rt = engine.tasks[0].runtime
    engine.tick(now=0)                   # 开仓单 @60005.9
    v.set_price("BTC", 59_900)
    engine.tick(now=1)                   # 成交、止盈 @60606 挂上、第二笔开仓
    assert len(v.open_orders("BTC")) == 2
    v.set_price("BTC", 60_550)           # ask≈60556 触及 stop 60500，
    engine.tick(now=2)                   # 但 bid 未到止盈价，止盈不成交
    assert rt.exited is True
    opens = v.open_orders("BTC")
    assert len(opens) == 1 and opens[0].reduce_only  # 开仓单撤、止盈保留
    assert engine.executor.position("lighter", "BTC") is not None
    engine.tick(now=3)                   # 退出后不再有任何动作
    assert len(v.open_orders("BTC")) == 1


def test_volume_pause_price_blocks_open_only():
    v = PaperVenue(venue_id="lighter", prices=dict(BTC))
    engine, _ = build(vol_yaml(pause_price=60_500), v)
    v.set_price("BTC", 60_600)           # ask≈60606 ≥ pause → 暂停开仓
    engine.tick(now=0)
    assert v.open_orders("BTC") == []
    v.set_price("BTC", 60_000)           # 回到暂停价下方 → 恢复开仓
    engine.tick(now=1)
    assert len(v.open_orders("BTC")) == 1


def test_volume_kill_switch_flattens():
    v = PaperVenue(venue_id="lighter", prices=dict(BTC))
    engine, _ = build(vol_yaml(), v)
    engine.tick(now=0)
    v.set_price("BTC", 59_900)
    engine.tick(now=1)
    assert engine.executor.position("lighter", "BTC") is not None
    engine.kill_switch.halt("测试停机")
    engine.tick(now=2)                   # 停机：撤单 + 平仓（安全优先）
    assert engine.executor.position("lighter", "BTC") is None
    assert v.open_orders("BTC") == []


def test_volume_panel_stop_keeps_position():
    v = PaperVenue(venue_id="lighter", prices=dict(BTC))
    y = vol_yaml()
    cfg = build_config(env={}, yaml_data=y)
    ledger = Ledger()
    engine = Engine.from_config(cfg, {"lighter": v}, ledger=ledger)
    state = PanelState(engine=engine, venues={"lighter": v}, ledger=ledger,
                       app_config=cfg, task_cfgs={"vol-btc": y["tasks"][0]})
    engine.tick(now=0)
    v.set_price("BTC", 59_900)
    engine.tick(now=1)
    assert engine.executor.position("lighter", "BTC") is not None
    res = state._do_stop_task("vol-btc")
    assert res["ok"] is True
    assert "未自动平仓" in res["note"]            # 如实标注停止语义
    assert v.open_orders("BTC") == []             # 自有挂单全撤
    assert engine.executor.position("lighter", "BTC") is not None  # 持仓保留


def test_volume_conflict_with_grid_rejected():
    y = vol_yaml()
    y["tasks"].append({
        "name": "grid-btc", "strategy": "grid", "venue": "lighter",
        "symbol": "BTC",
        "params": {"lower": 55_000, "upper": 65_000, "grid_count": 10,
                   "qty_per_grid": 0.01}})
    with pytest.raises(ConfigError):
        build_config(env={}, yaml_data=y)


# ---------------- live 的 caps 强制 ----------------

def _direct(mode, caps=None, **param_over):
    v = PaperVenue(venue_id="lighter", prices=dict(BTC))
    v.mode = mode
    ex = Executor(venues={"lighter": v}, caps=caps)
    params = {"qty": 0.01, "take_profit_pct": 1.0}
    params.update(param_over)
    return VolumeLoopStrategy(VolumeParams(**params), executor=ex,
                              venue_id="lighter", symbol="BTC", owner="vol")


def test_live_without_caps_refused():
    rt = _direct("live")
    with pytest.raises(RuntimeError, match="caps"):
        rt.start()


def test_live_with_caps_starts():
    rt = _direct("live", caps=Caps(global_=100_000))
    rt.start()
    assert rt.started is True


def test_paper_without_caps_starts():
    rt = _direct("paper")
    rt.start()
    assert rt.started is True


def test_estimated_exposure_over_cap_refused():
    # 最大敞口估算 = max_orders(3) × 0.01 × 60000 = 1800 > 策略上限 100
    rt = _direct("paper", caps=Caps(strategy={"vol": 100.0}), max_orders=3)
    with pytest.raises(RuntimeError, match="超过策略"):
        rt.start()
