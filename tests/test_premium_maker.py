"""溢价套利 maker 执行模式测试（paper）。

maker 语义（源自 perp-dex-tools 的 hedge 主腿执行）：开仓时
base 腿先 post-only 挂单等成交，全成后对冲腿 taker 跟上；
部分成交先对冲已成部分再撤余单；超时未成撤单放弃本轮。
平仓/反向减仓仍走双腿 taker（不在本文件范围，见集成测试的
taker 往返；maker 模式的反向信号同样走 taker，见最后一个用例）。
"""

import pytest

from detsp.core.config import build_config
from detsp.core.engine import Engine
from detsp.core.ledger import Ledger
from detsp.core.venue import Order, OrderRequest, Side
from detsp.venues.paper import PaperVenue


def _venues(base_price=100.0, hedge_price=99.0):
    base = PaperVenue(venue_id="entropy", prices={"SNDK": base_price})
    hedge = PaperVenue(venue_id="lighter_rh", prices={"SNDK": hedge_price})
    return {"entropy": base, "lighter_rh": hedge}


def _build(venues, **params):
    base = {"symbol": "SNDK", "base_venue": "entropy",
            "hedge_venue": "lighter_rh", "midline_bps": 0,
            "upper_bps": 50, "lower_bps": 50, "persist_seconds": 0,
            "max_position_usd": 1000, "max_order_notional_usd": 500,
            "reconcile_seconds": 15, "execution_mode": "maker",
            "maker_timeout_seconds": 10}
    base.update(params)
    y = {"tasks": [{"name": "arb", "strategy": "premium_arb",
                    "params": base}]}
    cfg = build_config(env={}, yaml_data=y)
    engine = Engine.from_config(cfg, venues, ledger=Ledger())
    assert "arb" not in engine.errors, engine.errors
    return engine


def _legs(engine):
    pb = engine.executor.position("entropy", "SNDK")
    ph = engine.executor.position("lighter_rh", "SNDK")
    return (pb.qty if pb else 0.0), (ph.qty if ph else 0.0)


def test_maker_full_fill_then_hedge_follows():
    venues = _venues()   # 溢价约 +101bps ≥ 50 → sell_base 信号
    engine = _build(venues)
    rt = engine.tasks[0].runtime
    engine.tick(now=0)
    # base 腿只挂了 maker 卖单（贴 ask），对冲腿还没动
    opens = venues["entropy"].open_orders("SNDK")
    assert len(opens) == 1 and opens[0].side is Side.SELL
    assert rt._maker is not None
    assert _legs(engine) == (0.0, 0.0)

    venues["entropy"].set_price("SNDK", 101.0)   # 涨穿 maker 卖单
    engine.tick(now=1)
    # 主腿全成 5（500/100），对冲腿 taker 买入等量跟上
    assert _legs(engine) == (pytest.approx(-5), pytest.approx(5))
    assert rt._maker is None


def test_maker_timeout_no_fill_abandons_round():
    venues = _venues()
    engine = _build(venues)
    rt = engine.tasks[0].runtime
    engine.tick(now=0)
    assert rt._maker is not None
    engine.tick(now=5)    # 未超时、未成交：继续等
    assert rt._maker is not None
    engine.tick(now=11)   # 超过 maker_timeout_seconds=10：撤单放弃
    assert rt._maker is None
    assert venues["entropy"].open_orders("SNDK") == []
    assert _legs(engine) == (0.0, 0.0)     # 没开成任何仓位


class _PartialBaseVenue(PaperVenue):
    """base 腿 maker 单先部分成交挂着，released 后变终态（已撤）。"""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.released = False
        self._req = None
        self._partial = 0.0

    def place_order(self, req):
        if req.post_only and self._req is None:
            self._req = req
            self._partial = req.qty * 0.4
            self._apply_fill(req.symbol, req.side, self._partial, req.price)
            return Order(order_id="mk-1", symbol=req.symbol, side=req.side,
                         qty=req.qty, price=req.price, filled_qty=0.0,
                         status="open")
        return super().place_order(req)

    def poll_order(self, order_id):
        if order_id == "mk-1" and self._req is not None:
            status = "canceled" if self.released else "open"
            return Order(order_id="mk-1", symbol=self._req.symbol,
                         side=self._req.side, qty=self._req.qty,
                         price=self._req.price,
                         filled_qty=self._partial, status=status)
        return super().poll_order(order_id)


def test_maker_partial_fill_hedges_final_qty_on_timeout():
    base = _PartialBaseVenue(venue_id="entropy", prices={"SNDK": 100.0})
    hedge = PaperVenue(venue_id="lighter_rh", prices={"SNDK": 99.0})
    venues = {"entropy": base, "lighter_rh": hedge}
    # 仓位上限压到 600：部分对冲后（已用 200）第二轮切片（500）
    # 会超限不开新轮，方便断言本轮已收尾
    engine = _build(venues, max_position_usd=600)
    rt = engine.tasks[0].runtime
    engine.tick(now=0)    # 挂 maker（场所侧已部分成交 40%）
    assert rt._maker is not None
    engine.tick(now=5)    # 部分成交中：保留跟踪，不提前对冲
    assert rt._maker is not None
    assert _legs(engine)[1] == 0.0
    engine.tick(now=11)   # 超时：撤单（终态 canceled 带部分量）
    base.released = True
    engine.tick(now=12)   # 终态确认：按最终量 2（5×40%）对冲
    qb, qh = _legs(engine)
    assert qb == pytest.approx(-2)
    assert qh == pytest.approx(2)
    assert rt._maker is None


def test_maker_mode_closing_still_taker():
    # 先用 maker 开成仓位，再让溢价反转：反向减仓必须双腿 taker
    # 立即完成（同一 tick 内双腿同步削减、不留 maker 挂单等待），
    # 剩余只是切片按新价折算的零头且两腿仍等量对冲
    venues = _venues()
    engine = _build(venues)
    engine.tick(now=0)
    venues["entropy"].set_price("SNDK", 100.05)   # 轻微涨穿 maker 卖单
    engine.tick(now=1)
    assert _legs(engine) == (pytest.approx(-5), pytest.approx(5))
    venues["lighter_rh"].set_price("SNDK", 101.5)  # 溢价大幅转负
    engine.tick(now=2)
    qb, qh = _legs(engine)
    assert engine.tasks[0].runtime._maker is None      # 没走 maker 等成交
    assert venues["entropy"].open_orders("SNDK") == []  # 无 maker 挂单
    assert qb + qh == pytest.approx(0.0)               # 双腿同步削减
    assert abs(qb) < 0.01                              # 只剩折算零头
