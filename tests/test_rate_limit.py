"""执行层下单频率上限测试：滑动 60 秒、只限开仓类、按腿角色分预算。

定案口径：
- 只限 place(intent="open", opening=True)：base 120 笔 / hedge 30 笔
  （60 秒滑动窗口）。
- 对冲跟单（opening=False）、intent="close"、trim_excess、
  close_position 绝不限。
- 超限返回 rejected + 记录 rate_limit_events，适配器收不到该单；
  被拒订单不计入窗口。
"""

from detsp.core.execution import Executor
from detsp.core.venue import OrderRequest, Side
from detsp.venues.paper import PaperVenue

STRATEGY = "grid"
VENUE = "arcus"
SYMBOL = "BTC"
QTY = 0.001  # 0.001 * 60000 = 60 USD，无仓位上限干扰


class CountingVenue(PaperVenue):
    """记录适配器实际收到的下单次数。"""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.place_calls = 0

    def place_order(self, req):
        self.place_calls += 1
        return super().place_order(req)


class FakeClock:
    def __init__(self, start: float = 1_000_000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_executor(clock=None, **kw):
    venue = CountingVenue(venue_id=VENUE, prices={SYMBOL: 60_000.0})
    ex = Executor(venues={VENUE: venue}, clock=clock, **kw)
    return ex, venue


def open_order(ex, **kw):
    return ex.place(STRATEGY, VENUE,
                    OrderRequest(SYMBOL, Side.BUY, QTY),
                    intent="open", **kw)


def fill_base(ex, n=120):
    for _ in range(n):
        res = open_order(ex, leg_role="base")
        assert res.status == "filled"


# ---------- base 角色：120 通过 / 121 拒 ----------

def test_base_role_120_pass_121st_rejected():
    ex, venue = make_executor()
    fill_base(ex, 120)
    assert venue.place_calls == 120

    res = open_order(ex, leg_role="base")
    assert res.status == "rejected"
    assert "频率" in res.detail
    assert "base" in res.detail and "120" in res.detail
    # 适配器没有收到第 121 笔
    assert venue.place_calls == 120
    # 事件记录完整
    assert len(ex.rate_limit_events) == 1
    ev = ex.rate_limit_events[0]
    assert ev["strategy"] == STRATEGY
    assert ev["venue_id"] == VENUE
    assert ev["symbol"] == SYMBOL
    assert ev["leg_role"] == "base"
    assert isinstance(ev["ts"], float)


def test_default_leg_role_is_base():
    # 旧调用不传 leg_role/opening：按基准腿开仓计入预算
    ex, venue = make_executor(rate_limits={"base": 2, "hedge": 30})
    for _ in range(2):
        res = ex.place(STRATEGY, VENUE,
                       OrderRequest(SYMBOL, Side.BUY, QTY), intent="open")
        assert res.status == "filled"
    res = ex.place(STRATEGY, VENUE,
                   OrderRequest(SYMBOL, Side.BUY, QTY), intent="open")
    assert res.status == "rejected"
    assert venue.place_calls == 2


# ---------- hedge 角色：30 通过 / 31 拒，与 base 互不影响 ----------

def test_hedge_role_30_pass_31st_rejected_and_independent_from_base():
    ex, venue = make_executor()
    for _ in range(30):
        res = open_order(ex, leg_role="hedge")
        assert res.status == "filled"
    res = open_order(ex, leg_role="hedge")
    assert res.status == "rejected"
    assert "频率" in res.detail
    assert "hedge" in res.detail and "30" in res.detail
    assert venue.place_calls == 30
    assert [e["leg_role"] for e in ex.rate_limit_events] == ["hedge"]

    # hedge 打满不影响 base：base 仍可下满 120
    fill_base(ex, 120)
    assert venue.place_calls == 150
    res = open_order(ex, leg_role="base")
    assert res.status == "rejected"


def test_unknown_leg_role_unlimited():
    ex, venue = make_executor(rate_limits={"base": 1, "hedge": 1})
    for _ in range(5):
        res = open_order(ex, leg_role="other")
        assert res.status == "filled"
    assert venue.place_calls == 5
    assert ex.rate_limit_events == []


# ---------- 不受限路径 ----------

def test_opening_false_open_intent_unlimited():
    ex, venue = make_executor()
    fill_base(ex, 120)  # 先把 base 窗口打满
    assert open_order(ex, leg_role="base").status == "rejected"
    calls_before = venue.place_calls
    # 对冲跟单：intent=open 但 opening=False，下 200 笔不限
    for _ in range(200):
        res = open_order(ex, leg_role="base", opening=False)
        assert res.status == "filled"
    # opening=False 的对冲腿角色同样不限
    for _ in range(50):
        res = open_order(ex, leg_role="hedge", opening=False)
        assert res.status == "filled"
    assert venue.place_calls == calls_before + 250


def test_close_intent_unlimited():
    ex, venue = make_executor()
    fill_base(ex, 120)
    assert open_order(ex, leg_role="base").status == "rejected"
    calls_before = venue.place_calls
    for _ in range(200):
        res = ex.place(STRATEGY, VENUE,
                       OrderRequest(SYMBOL, Side.SELL, QTY),
                       intent="close")
        assert res.status == "filled"
    assert venue.place_calls == calls_before + 200


def test_trim_and_close_position_still_work_after_window_full():
    ex, venue = make_executor()
    fill_base(ex, 120)
    assert open_order(ex, leg_role="base").status == "rejected"
    calls_before = venue.place_calls

    # 削腿修平：trim_excess 经 place(intent="close") 进入
    res = ex.trim_excess(STRATEGY, VENUE, SYMBOL, Side.SELL, QTY,
                         max_slippage_bps=20.0)
    assert res.status == "filled"
    assert venue.place_calls == calls_before + 1

    # 保护性平仓：close_position 经 place(intent="close") 进入
    res = ex.close_position(STRATEGY, VENUE, SYMBOL)
    assert res is not None and res.status == "filled"
    assert venue.place_calls == calls_before + 2
    assert venue.positions() == []


# ---------- 滑动窗口 ----------

def test_window_slides_after_61_seconds():
    clock = FakeClock()
    ex, venue = make_executor(clock=clock)
    fill_base(ex, 120)
    assert open_order(ex, leg_role="base").status == "rejected"

    clock.advance(61.0)
    res = open_order(ex, leg_role="base")
    assert res.status == "filled"
    assert venue.place_calls == 121


def test_rejected_order_not_counted_in_window():
    clock = FakeClock()
    ex, venue = make_executor(clock=clock, rate_limits={"base": 2})
    # t0：打满 2 笔
    assert open_order(ex, leg_role="base").status == "filled"
    assert open_order(ex, leg_role="base").status == "filled"
    # 立刻再试仍拒，事件逐笔记录
    assert open_order(ex, leg_role="base").status == "rejected"
    assert open_order(ex, leg_role="base").status == "rejected"
    assert len(ex.rate_limit_events) == 2
    assert venue.place_calls == 2

    # t0+50：在窗口内被拒一次（若被拒单计入窗口，其时间戳在 t0+50）
    clock.advance(50.0)
    assert open_order(ex, leg_role="base").status == "rejected"
    assert len(ex.rate_limit_events) == 3

    # t0+61：原始 2 笔滑出窗口。若 t0+50 的被拒单曾计入，此时窗口
    # 内应有 1 笔残留、只能再下 1 笔；实际被拒不计，应可连下 2 笔
    clock.advance(11.0)
    assert open_order(ex, leg_role="base").status == "filled"
    assert open_order(ex, leg_role="base").status == "filled"
    assert open_order(ex, leg_role="base").status == "rejected"
    assert venue.place_calls == 4


def test_rejected_within_window_stays_full_after_small_advance():
    clock = FakeClock()
    ex, _venue = make_executor(clock=clock)
    fill_base(ex, 120)
    assert open_order(ex, leg_role="base").status == "rejected"
    clock.advance(10.0)  # 仍在原始 120 笔的 60 秒窗口内
    assert open_order(ex, leg_role="base").status == "rejected"
    assert len(ex.rate_limit_events) == 2
