"""统一执行层：所有策略下单的唯一通道。

硬规则（任何策略不得绕过）：
- 下单前：registry.claim 校验归属（冲突直接抛 ConflictError）；
  开仓动作过 KillSwitch、强平距离门槛与三层仓位上限（场所/策略/全局）。
- 下单后：成交写统一账本（ledger），订单登记归属（registry）。
- 结果未知（超时/连接中断/适配器抛 UnknownOutcomeError）：**绝不重发**，
  立即对账（查该场所持仓与挂单），把对账结果随 ExecResult 返回，
  由策略按真实持仓决策。
- 部分成交：按实际 filled_qty 记账与处理，剩余数量不自动补下。

S1（审计修复）：第三方库的超时/断连异常（requests / httpx / aiohttp /
SDK 自有异常）**不是** Python 内置 TimeoutError/ConnectionError 的子类，
只捕获内置类会让保护失效。各 live venue 的 place_order/cancel_order
用 unknown_outcome_guard 把这类异常统一翻译为 UnknownOutcomeError；
Executor 再兜底：place 过程中任何未被明确分类的异常先对账——
持仓发生变化或对账本身失败 → 按 unknown 返回；对账确认持仓未变
且无新增挂单 → 按 rejected 返回（订单确定未生效）。明确拒单
（venue 抛 RuntimeError 的业务拒单）走同一兜底路径被对账确认。
"""

from __future__ import annotations

import collections
import contextlib
import time
from dataclasses import dataclass, field

from .ledger import Fill, Ledger
from .registry import Key, OwnershipRegistry
from .risk import DEFAULT_MMR, KillSwitch, position_corridor_pct, within_cap
from .venue import Order, OrderRequest, Position, Side, VenueAdapter


class UnknownOutcomeError(RuntimeError):
    """下单结果未知（超时/断连/回执丢失）：禁止重发，必须先对账。"""


def _collect_unknown_types() -> tuple[type, ...]:
    """收集各网络库的超时/断连异常类（装了才收，缺失跳过）。"""
    types: list[type] = [TimeoutError, ConnectionError]
    try:
        import requests.exceptions as rq

        types += [rq.Timeout, rq.ConnectionError, rq.ConnectTimeout,
                  rq.ReadTimeout]
    except ImportError:
        pass
    try:
        import httpx

        types += [httpx.TimeoutException, httpx.ConnectError,
                  httpx.NetworkError, httpx.RemoteProtocolError]
    except ImportError:
        pass
    try:
        import aiohttp

        types += [aiohttp.ClientConnectionError, aiohttp.ServerTimeoutError,
                  aiohttp.ClientPayloadError]
    except ImportError:
        pass
    try:
        import asyncio

        types.append(asyncio.TimeoutError)
    except ImportError:
        pass
    return tuple(dict.fromkeys(types))


UNKNOWN_OUTCOME_TYPES = _collect_unknown_types()

# 类名启发式：SDK 自有异常（如 lighter/hyperliquid 内部包装的超时）
_UNKNOWN_NAME_HINTS = ("timeout", "timedout", "connecterror",
                       "connectionerror", "networkerror", "disconnect",
                       "unreachable", "temporarily")


def is_unknown_outcome_error(exc: BaseException) -> bool:
    """判断异常是否属于「结果未知」类（超时/断连/回执丢失）。"""
    if isinstance(exc, UnknownOutcomeError):
        return True
    if isinstance(exc, UNKNOWN_OUTCOME_TYPES):
        return True
    name = type(exc).__name__.lower()
    return any(h in name for h in _UNKNOWN_NAME_HINTS)


@contextlib.contextmanager
def unknown_outcome_guard():
    """venue 写接口用：把超时/断连类异常翻译为 UnknownOutcomeError。

    业务拒单（RuntimeError 等明确错误）原样穿透，由 Executor 兜底对账。
    """
    try:
        yield
    except UnknownOutcomeError:
        raise
    except Exception as exc:  # noqa: BLE001
        if is_unknown_outcome_error(exc):
            raise UnknownOutcomeError(
                f"{type(exc).__name__}: {exc}") from exc
        raise


@dataclass
class Caps:
    """三层名义金额上限（<=0 表示该层不限）。"""

    venue: dict[str, float] = field(default_factory=dict)
    strategy: dict[str, float] = field(default_factory=dict)
    global_: float = 0.0


@dataclass
class ExecResult:
    status: str  # filled / open / partial / rejected / unknown
    order: Order | None = None
    filled_qty: float = 0.0
    fill_price: float = 0.0
    detail: str = ""
    position: Position | None = None  # status=unknown 时附对账持仓


# 市价单成交发现的最长等待（秒）：异步结算场所（Lighter）的成交
# 通常数秒内落地，180 秒足够覆盖慢结算；超时按持仓差定案
PENDING_FILL_TTL_SECONDS = 180.0


@dataclass
class _PendingFill:
    """一笔回执未终结的市价单，等待成交发现补记账（审查 M2）。"""

    order_id: str
    strategy: str
    venue_id: str
    symbol: str
    side: Side
    qty: float            # 请求量
    recorded: float       # 已记账量（place 时回执已记的部分）
    baseline_qty: float   # 下单前该 (venue,symbol) 的带符号持仓量
    ref_price: float      # 记账参考价（下单时的对手价）
    deadline: float       # 定案截止（epoch 秒）


class Executor:
    # 开仓频率闸门滑动窗口（秒）
    RATE_LIMIT_WINDOW_SECONDS = 60.0

    def __init__(
        self,
        venues: dict[str, VenueAdapter],
        registry: OwnershipRegistry | None = None,
        ledger: Ledger | None = None,
        kill_switch: KillSwitch | None = None,
        caps: Caps | None = None,
        min_corridor_pct: float = 3.0,
        rate_limits: dict | None = None,
        clock=None,
    ) -> None:
        self.venues = dict(venues)
        self.registry = registry or OwnershipRegistry()
        self.ledger = ledger or Ledger()
        self.kill_switch = kill_switch or KillSwitch()
        self.caps = caps or Caps()
        self.min_corridor_pct = min_corridor_pct
        self._pending_fills: dict[str, _PendingFill] = {}
        # 开仓频率预算：{leg_role: 60 秒内最多笔数}；缺省基准腿
        # 120、对冲腿 30。不在表中的 leg_role 不限、不计数。
        self.rate_limits: dict[str, int] = (
            {"base": 120, "hedge": 30} if rate_limits is None
            else dict(rate_limits))
        self._rate_windows: dict[str, collections.deque] = {
            role: collections.deque() for role in self.rate_limits}
        # 频率超限事件（供面板/测试读）：每条含 ts/strategy/
        # venue_id/symbol/leg_role
        self.rate_limit_events: list[dict] = []
        # 时钟注入：缺省动态调用 time.time()（可 monkeypatch
        # detsp.core.execution.time.time）；测试也可传 clock 可调用对象
        self._clock = clock if clock is not None else (lambda: time.time())

    def _rate_gate(self, strategy: str, venue_id: str, symbol: str,
                   leg_role: str) -> tuple[ExecResult | None, float | None]:
        """开仓频率闸门检查：超限返回 (rejected 结果, None)，放行
        返回 (None, 本次时间戳)——时间戳由调用方在真正下单前计入
        窗口，被闸门拒的单不计入，被其他风控拒/抛错的单也不计入。"""
        limit = self.rate_limits[leg_role]
        now = float(self._clock())
        dq = self._rate_windows.setdefault(leg_role, collections.deque())
        cutoff = now - self.RATE_LIMIT_WINDOW_SECONDS
        while dq and dq[0] <= cutoff:
            dq.popleft()
        if len(dq) >= limit:
            self.rate_limit_events.append({
                "ts": now, "strategy": strategy, "venue_id": venue_id,
                "symbol": symbol, "leg_role": leg_role})
            return ExecResult(status="rejected", detail=(
                f"下单频率超限：{leg_role} 腿 60 秒内最多 {limit} 笔"
                f"开仓单，已达上限，本单未下")), None
        return None, now

    # ----- 基础查询 -----
    def venue(self, venue_id: str) -> VenueAdapter:
        return self.venues[venue_id]

    def key(self, venue_id: str, symbol: str) -> Key:
        return Key(venue=venue_id, account=self.venues[venue_id].account,
                   symbol=symbol)

    def claim(self, strategy: str, venue_id: str, symbol: str) -> Key:
        key = self.key(venue_id, symbol)
        self.registry.claim(key, strategy)
        return key

    def release(self, strategy: str, venue_id: str, symbol: str) -> None:
        self.registry.release(self.key(venue_id, symbol), strategy)

    def position(self, venue_id: str, symbol: str) -> Position | None:
        for p in self.venues[venue_id].positions():
            if p.symbol == symbol:
                return p
        return None

    def reconcile(self, venue_id: str, symbol: str) -> Position | None:
        """对账：以场所真实持仓为准（下单结果未知/定期核对时用）。"""
        return self.position(venue_id, symbol)

    # ----- 强平距离公共函数（H7/M5：各策略共用同一套口径）-----
    def market_mmr(self, venue_id: str, symbol: str) -> float:
        """该市场的维持保证金率：交易所给得出就用，未知用保守默认。"""
        for m in self.venues[venue_id].markets():
            if m.symbol == symbol:
                return m.maint_margin_rate or DEFAULT_MMR
        return DEFAULT_MMR

    def position_corridor(self, venue_id: str, symbol: str,
                          price: float | None = None) -> tuple[bool, float | None]:
        """已有持仓的强平距离：(有持仓, 距离%)；距离 None=不可评估。

        mmr 统一取市场维持保证金率（缺失保守默认）；交易所未给
        liq_price 又无法估算（如杠杆未知）→ 不可评估。
        """
        pos = self.position(venue_id, symbol)
        if pos is None or pos.qty == 0:
            return False, None
        if price is None:
            price = self.venues[venue_id].book(symbol).mid
        return True, position_corridor_pct(price, pos,
                                           self.market_mmr(venue_id, symbol))

    @staticmethod
    def estimate_open_corridor(leverage: float,
                               mmr: float = DEFAULT_MMR) -> float:
        """新开仓的强平距离上限估算（%）：100/杠杆 − mmr×100。

        实际距离只会比这更近（估算乐观界），低于门槛即拒开仓；
        mmr 不再默认 0（M5）。"""
        lev = max(float(leverage), 1.0)
        return 100.0 / lev - mmr * 100.0

    def cancel_unregistered(self, venue_id: str, symbol: str) -> int:
        """撤销该 (venue,symbol) 下 registry 没有登记的挂单（S3 启动对账）。

        只撤「不是本进程任何策略下过」的单；数量返回给调用方记事件。
        """
        n = 0
        for o in list(self.venues[venue_id].open_orders(symbol)):
            if self.registry.order_strategy(o.order_id) is None:
                try:
                    if self.venues[venue_id].cancel_order(o.order_id):
                        n += 1
                except Exception:  # noqa: BLE001 — 单个撤单失败不阻塞对账
                    pass
        return n

    def _total_notional(self) -> float:
        total = 0.0
        for v in self.venues.values():
            for p in v.positions():
                total += p.notional
        return total

    def record_fill(self, strategy: str, venue_id: str, symbol: str,
                    side: Side, qty: float, price: float,
                    order_id: str = "") -> None:
        """补记成交：挂单在下单之后才成交时（如限价单被行情穿价），
        由策略在轮询发现成交后调用，保证账本不漏记。"""
        adapter = self.venues[venue_id]
        self.ledger.record(Fill(
            venue=venue_id, account=adapter.account, symbol=symbol,
            strategy=strategy, side=side.value, qty=qty, price=price,
            fee=0.0, order_id=order_id))

    # ----- 市价单成交发现（审查 M2）-----
    def has_pending_fill(self, venue_id: str, symbol: str) -> bool:
        """该 (场所, 币种) 是否有市价单尚待成交发现定案——对冲
        调平在上一笔调整未结算时不叠加新调整（防异步结算下按
        旧持仓重复调平）。"""
        return any(pf.venue_id == venue_id and pf.symbol == symbol
                   for pf in self._pending_fills.values())

    def poll_pending_fills(self, now: float | None = None) -> list[str]:
        """推进待发现的市价单：能 poll 到终态就按终态量补记；
        poll 不可得且订单已不在挂单中时，用「持仓 − 下单前基线」
        的差额推断成交量（按请求量封顶）；定案后注销订单登记。

        引擎每 tick 调用一次。返回本轮定案的订单号列表。
        归因口径：同一 (venue,symbol) 同时只有归属任务自己的单，
        多笔待发现按登记顺序逐笔以各自基线推断、各自封顶。
        """
        now = time.time() if now is None else now
        settled: list[str] = []
        for oid, pf in list(self._pending_fills.items()):
            adapter = self.venues[pf.venue_id]
            order = None
            try:
                order = adapter.poll_order(oid)
            except Exception:  # noqa: BLE001 — 查询失败按不可得处理
                order = None
            total: float | None = None
            if order is not None:
                if order.status == "open":
                    if order.filled_qty > pf.recorded:
                        self._record_pending_inc(pf, order.filled_qty)
                    if now < pf.deadline:
                        continue
                    total = pf.recorded  # 超时仍 open：按已知量定案
                else:
                    total = order.filled_qty
            if total is None:
                try:
                    open_ids = {o.order_id
                                for o in adapter.open_orders(pf.symbol)}
                except Exception:  # noqa: BLE001 — 挂单查询失败：继续等
                    continue
                if oid in open_ids and now < pf.deadline:
                    continue
                pos = self.position(pf.venue_id, pf.symbol)
                cur = pos.qty if pos else 0.0
                delta = (cur - pf.baseline_qty) * pf.side.sign
                inferred = min(max(delta, 0.0), pf.qty)
                if inferred <= pf.recorded and now < pf.deadline \
                        and oid not in open_ids:
                    # 已不在挂单中又无成交证据：可能只是结算延迟，
                    # 宽限期内继续等，过期再按推断量定案
                    continue
                total = inferred
            if total > pf.recorded:
                self._record_pending_inc(pf, total)
            self.registry.unregister_order(oid)
            del self._pending_fills[oid]
            settled.append(oid)
        return settled

    def _record_pending_inc(self, pf: _PendingFill, total_filled: float) -> None:
        inc = total_filled - pf.recorded
        if inc <= 1e-12:
            return
        adapter = self.venues[pf.venue_id]
        self.ledger.record(Fill(
            venue=pf.venue_id, account=adapter.account, symbol=pf.symbol,
            strategy=pf.strategy, side=pf.side.value, qty=inc,
            price=pf.ref_price, fee=0.0, order_id=pf.order_id))
        pf.recorded = total_filled

    # ----- 削腿：带滑点保护的限价只减仓单（对照审查 M2）-----
    def trim_excess(self, strategy: str, venue_id: str, symbol: str,
                    side: Side, qty: float,
                    max_slippage_bps: float) -> ExecResult:
        """削掉多余的一截仓位（delta 修正专用通道）。

        对照 entropy-arb 的 _hedge 口径：不用无保护市价单，改用
        reduce-only 限价单——卖单限价 = bid×(1−滑点上限)、买单 =
        ask×(1+滑点上限)，盘口在保护范围内立即成交，跳变时宁可
        本轮不成交（挂单留待后续对账再处理），不吃异常滑点。
        已有本策略的在途削腿单（同 venue/symbol 的 reduce-only
        挂单，或同向且量不小于本次的挂单）时不重复下单。
        """
        if qty <= 0:
            return ExecResult(status="rejected", detail="削腿量为 0")
        adapter = self.venues[venue_id]
        try:
            for o in adapter.open_orders(symbol):
                if not self.registry.owns_order(o.order_id, strategy):
                    continue
                if o.reduce_only or (o.side is side and o.qty >= qty * 0.999):
                    return ExecResult(status="open", order=o,
                                      detail="已有在途削腿单，本轮跳过")
        except Exception:  # noqa: BLE001 — 挂单查询失败不挡削腿本身
            pass
        book = adapter.book(symbol)
        slip = max(max_slippage_bps, 0.0) / 10_000
        if side is Side.SELL:
            price = book.bid * (1 - slip)
        else:
            price = book.ask * (1 + slip)
        req = OrderRequest(symbol=symbol, side=side, qty=qty, price=price,
                           reduce_only=True)
        return self.place(strategy, venue_id, req, intent="close")

    # ----- 下单 -----
    def place(self, strategy: str, venue_id: str, req: OrderRequest, *,
              intent: str = "open",
              corridor_pct: float | None = None,
              leg_role: str = "base",
              opening: bool = True) -> ExecResult:
        """统一下单入口。

        intent: open=开/加仓（过全部风控）；close=平/减仓（安全动作，
        停机时也允许，不查上限与强平距离）。
        corridor_pct: 调用方估算的开仓后强平距离，低于门槛直接拒。
        leg_role: 腿角色，base=基准腿 / hedge=对冲腿，仅用于开仓
        频率预算区分；不在 rate_limits 表中的值不限、不计数。
        opening: 是否为真正开仓。频率闸门只挂 intent="open" 且
        opening=True 的路径：对冲跟单以 opening=False 进入、
        trim_excess / close_position 以 intent="close" 进入，
        三者都不受频率限制。
        """
        # 频率闸门：开仓路径最前面，在归属/盘口/其他风控/适配器
        # 之前快速返回，被拒不消耗任何其他状态、不计入窗口
        _rate_ts: float | None = None
        if intent == "open" and opening and leg_role in self.rate_limits:
            rejected, _rate_ts = self._rate_gate(
                strategy, venue_id, req.symbol, leg_role)
            if rejected is not None:
                return rejected
        adapter = self.venues[venue_id]
        key = self.key(venue_id, req.symbol)
        # 归属校验：被其他策略占用直接抛 ConflictError
        self.registry.claim(key, strategy)

        book = adapter.book(req.symbol)
        ref_price = req.price if req.price is not None else book.mid
        cur = self.position(venue_id, req.symbol)
        cur_qty = cur.qty if cur else 0.0

        if intent == "open":
            self.kill_switch.assert_allowed()
            if corridor_pct is not None and corridor_pct < self.min_corridor_pct:
                return ExecResult(status="rejected", detail=(
                    f"强平距离 {corridor_pct:.2f}% 低于门槛 "
                    f"{self.min_corridor_pct}%"))
            # 仓位上限：只对「敞口变大」的下单生效
            new_qty = cur_qty + req.qty * req.side.sign
            if abs(new_qty) > abs(cur_qty):
                new_notional = abs(new_qty) * ref_price
                if not within_cap(new_notional, self.caps.venue.get(venue_id, 0.0)):
                    return ExecResult(status="rejected",
                                      detail=f"超过场所仓位上限 {venue_id}")
                # 策略层口径（审查 M5 统一）：该任务全部腿的持仓
                # 名义之和——双腿策略不再是「单腿口径可达 2 倍」。
                # 其余腿名义按持仓均价计（position.notional 口径）。
                strat_cap = self.caps.strategy.get(strategy, 0.0)
                if strat_cap > 0:
                    others = 0.0
                    for k in self.registry.keys_of(strategy):
                        if k == key:
                            continue
                        p = self.position(k.venue, k.symbol)
                        if p is not None:
                            others += p.notional
                    if not within_cap(others + new_notional, strat_cap):
                        return ExecResult(
                            status="rejected",
                            detail=f"超过策略仓位上限 {strategy}（全部腿合计口径）")
                if self.caps.global_ > 0:
                    after = self._total_notional() + \
                        (new_notional - abs(cur_qty) * ref_price)
                    if not within_cap(after, self.caps.global_):
                        return ExecResult(status="rejected",
                                          detail="超过全局仓位上限")

        if _rate_ts is not None:
            # 只有通过闸门且通过其余开仓风控、真正要下到适配器的
            # 开仓单才计入窗口
            self._rate_windows[leg_role].append(_rate_ts)
        try:
            order = adapter.place_order(req)
        except UnknownOutcomeError as e:
            # 结果未知：不重发，先对账，把真实持仓交回调用方
            pos = self.reconcile(venue_id, req.symbol)
            return ExecResult(status="unknown", detail=str(e), position=pos)
        except Exception as e:  # noqa: BLE001 — S1 兜底：未分类异常先对账
            # venue 理论上应把超时/断连翻译成 UnknownOutcomeError；
            # 仍有未翻译的到达这里：一律先对账，按证据定性——
            # 持仓变了或对账失败 → unknown（可能已生效，绝不重发）；
            # 持仓未变且无新增挂单 → rejected（确定未生效）。
            if is_unknown_outcome_error(e):
                pos = self.reconcile(venue_id, req.symbol)
                return ExecResult(status="unknown", detail=str(e), position=pos)
            try:
                pos = self.reconcile(venue_id, req.symbol)
                after_qty = pos.qty if pos else 0.0
                stray = any(
                    self.registry.order_strategy(o.order_id) is None
                    for o in adapter.open_orders(req.symbol))
            except Exception:  # noqa: BLE001 — 对账本身都失败 → 保守 unknown
                return ExecResult(status="unknown", detail=str(e),
                                  position=None)
            if abs(after_qty - cur_qty) > 1e-12 or stray:
                return ExecResult(status="unknown", detail=str(e), position=pos)
            return ExecResult(status="rejected",
                              detail=f"下单被拒：{type(e).__name__}: {e}")

        if order.order_id:
            self.registry.register_order(order.order_id, key, strategy)
        filled = order.filled_qty
        fill_price = 0.0
        if filled > 0:
            if order.price is not None:
                fill_price = order.price
            elif req.price is not None:
                fill_price = req.price
            else:
                fill_price = book.ask if req.side is Side.BUY else book.bid
            self.ledger.record(Fill(
                venue=venue_id, account=adapter.account, symbol=req.symbol,
                strategy=strategy, side=req.side.value, qty=filled,
                price=fill_price, fee=0.0, order_id=order.order_id))
        if order.status in ("filled", "canceled", "rejected"):
            self.registry.unregister_order(order.order_id)
            status = order.status if filled <= 0 else (
                "filled" if filled >= req.qty else "partial")
        elif filled >= req.qty:
            status = "filled"
        elif filled > 0:
            status = "partial"
        else:
            status = "open"
        if req.price is None and status == "open" and order.order_id:
            # 市价单回执未终结（如 Lighter 异步结算：回执恒 open /
            # filled=0）：登记待发现，成交由 poll_pending_fills 经
            # poll 终态或持仓差对账补记账本（审查 M2）；在此之前
            # 订单登记保留，定案后才注销，避免登记只增不减。
            self._pending_fills[order.order_id] = _PendingFill(
                order_id=order.order_id, strategy=strategy,
                venue_id=venue_id, symbol=req.symbol, side=req.side,
                qty=req.qty, recorded=filled, baseline_qty=cur_qty,
                ref_price=fill_price if filled > 0 else
                (book.ask if req.side is Side.BUY else book.bid),
                deadline=time.time() + PENDING_FILL_TTL_SECONDS)
        return ExecResult(status=status, order=order, filled_qty=filled,
                          fill_price=fill_price)

    # ----- 撤单（只能撤自己的） -----
    def cancel(self, strategy: str, venue_id: str, order_id: str) -> bool:
        if not self.registry.owns_order(order_id, strategy):
            from .registry import ConflictError
            raise ConflictError(f"订单 {order_id} 不属于策略 {strategy}，拒绝撤销")
        try:
            ok = self.venues[venue_id].cancel_order(order_id)
        except UnknownOutcomeError:
            # 撤单结果未知：按未撤成处理，保留登记，下轮重试
            return False
        if ok:
            self.registry.unregister_order(order_id)
        return ok

    def cancel_all_own(self, strategy: str, venue_id: str, symbol: str) -> int:
        n = 0
        for o in list(self.venues[venue_id].open_orders(symbol)):
            if self.registry.owns_order(o.order_id, strategy):
                if self.cancel(strategy, venue_id, o.order_id):
                    n += 1
        return n

    # ----- 平仓（安全动作） -----
    def close_position(self, strategy: str, venue_id: str, symbol: str,
                       qty: float | None = None) -> ExecResult | None:
        """市价只减仓平掉自有持仓（可指定部分数量）。无持仓返回 None。"""
        pos = self.position(venue_id, symbol)
        if pos is None or pos.qty == 0:
            return None
        close_qty = abs(pos.qty) if qty is None else min(abs(pos.qty), qty)
        if close_qty <= 0:
            return None
        side = Side.SELL if pos.qty > 0 else Side.BUY
        req = OrderRequest(symbol=symbol, side=side, qty=close_qty,
                           price=None, reduce_only=True)
        return self.place(strategy, venue_id, req, intent="close")
