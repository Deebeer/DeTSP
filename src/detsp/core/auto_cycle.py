"""全自动监督器（run_mode=auto 任务的运行时）。

职责：只定两所/金额/门槛，标的由系统自动选——
扫描（scanning）→ 按策略口径从两所共同上市标的里选第一个合格者
→ 组装一台单笔运行时（single_cycle=True，构造走 engine.build_runtime
同一条路径）运行（running）→ 内层一笔平仓（cycle_done）后收尾、
释放该标的两腿归属、计数 → 冷却（cooldown）→ 再选标循环，
直到 stop() 为止，自身永不置 cycle_done。

异常口径：tick 绝不抛出。扫描/选标/组装异常写 last_error 中文
短句并停在 scanning，下一 tick 自动重试；内层 tick 异常同样只
记 last_error、内层保留继续管仓。
"""

from __future__ import annotations


class AutoCycleRuntime:
    """全自动监督器：接口与策略运行时对齐（tick(now, allow_open) /
    stop()），引擎可无差别调度。"""

    # 各策略的两腿场所字段（与 engine.task_occupied_keys 同口径）
    _VENUE_FIELDS = {
        "hedged_grid": ("grid_venue", "hedge_venue"),
        "rotation_hedge": ("venue_a", "venue_b"),
        "funding_carry": ("venue_a", "venue_b"),
        "premium_arb": ("base_venue", "hedge_venue"),
    }

    def __init__(self, strategy_type, params: dict, executor,
                 risk_cfg=None, owner: str = "", scanner=None) -> None:
        self.strategy_type = strategy_type
        # 原始（已清洗）参数替身：内层每笔按它组装，供序列化读
        self.params: dict = dict(params)
        self.executor = executor
        self.risk_cfg = risk_cfg
        self.owner = owner
        self.scanner = scanner
        fa, fb = self._VENUE_FIELDS.get(strategy_type, ("venue_a", "venue_b"))
        self.venue_a = self.params.get(fa) or ""
        self.venue_b = self.params.get(fb) or ""
        # 监督器状态（面板展示用）
        self.auto_phase = "scanning"     # scanning / running / cooldown / stopped
        self.current_symbol: str | None = None
        self.cycles_completed = 0
        self.last_error = ""
        self.recovery_notes: list[str] = []
        self.stopped = False
        # 监督器自身不是单笔：恒 False，引擎不会因它结束 auto 任务
        self.single_cycle = False
        self.cycle_done = False
        self.stop_note = "已停止：双腿挂单已撤、双腿持仓已平"
        self._inner = None               # 当前单笔内层运行时
        self._cooldown_until = -1e18

    # ----- 行情扫描 -----
    def _get_scanner(self):
        if self.scanner is None:
            from .market_scanner import MarketScanner

            self.scanner = MarketScanner()
        return self.scanner

    def _scan_rows(self) -> list[dict]:
        """两所共同上市标的（按策略口径已排序）；兼容 dict/list 返回。"""
        res = self._get_scanner().pairs(
            self.venue_a, self.venue_b, self.strategy_type)
        if isinstance(res, dict):
            return list(res.get("rows") or [])
        if isinstance(res, list):
            return res
        return []

    def _pick(self, rows: list[dict]) -> dict | None:
        """按策略口径取第一行合格者；suspect（疑似同名坏数据）一律跳过。"""
        usable = [r for r in rows
                  if isinstance(r, dict) and not r.get("suspect")]
        if self.strategy_type == "funding_carry":
            threshold = self.params.get("min_entry_edge_bps_per_hour")
            threshold = 1.0 if threshold is None else float(threshold)
            for row in usable:
                edge = row.get("edge_bps_h")
                if edge is not None and edge >= threshold:
                    return row
            return None
        if self.strategy_type == "rotation_hedge":
            for row in usable:
                edge = row.get("edge_bps_h")
                if edge is not None and edge > 0:
                    return row
            return None
        if self.strategy_type == "premium_arb":
            for row in usable:
                if "has_price" in row:
                    if row.get("has_price"):
                        return row
                elif row.get("price_a") is not None \
                        and row.get("price_b") is not None:
                    return row
            return None
        if self.strategy_type == "hedged_grid":
            # 有网格腿价格的优先；全无价时也接受第一行（内层启动
            # 时若仍无价会自报错隔离，不在选标期一票否决）
            for row in usable:
                if row.get("price_a") is not None:
                    return row
            return usable[0] if usable else None
        return usable[0] if usable else None

    # ----- 内层组装 -----
    def _grid_bounds(self, symbol: str, row: dict) -> tuple[float, float] | None:
        """中性网格区间：优先 K 线建议区间（近 24h 高低各扩 2%），
        拿不到用选中行价格 ±10%；两者都拿不到返回 None（本轮跳过）。"""
        try:
            c = self._get_scanner().candles(self.venue_a, symbol)
            if isinstance(c, dict) and c.get("ok") \
                    and c.get("suggested_lower") is not None \
                    and c.get("suggested_upper") is not None:
                return float(c["suggested_lower"]), float(c["suggested_upper"])
        except Exception:  # noqa: BLE001 — K 线失败走价格兜底
            pass
        price = row.get("price_a") or row.get("price_b")
        if price:
            p = float(price)
            return round(p * 0.9, 6), round(p * 1.1, 6)
        return None

    def _build_inner(self, symbol: str, row: dict):
        """组装一台单笔内层运行时（参数经 specs 校验清洗）。"""
        from ..strategies.specs import validate_params
        from .engine import build_runtime  # 延迟导入避免循环

        inner_params = dict(self.params)
        inner_params["symbol"] = symbol
        if self.strategy_type == "hedged_grid":
            bounds = self._grid_bounds(symbol, row)
            if bounds is None:
                return None
            inner_params["lower_price"], inner_params["upper_price"] = bounds
        clean = validate_params(self.strategy_type, inner_params)
        inner = build_runtime(self.strategy_type, clean, self.executor,
                              self.risk_cfg, self.owner)
        inner.single_cycle = True
        return inner

    # ----- 内层收尾 -----
    def _inner_flat(self) -> bool:
        """内层两腿仓位是否都为零（停机收尾期释放内层前判）。"""
        if self._inner is None or self.current_symbol is None:
            return False
        if getattr(self._inner, "cycle_done", False):
            return True
        pos_fn = getattr(self.executor, "position", None) \
            if self.executor is not None else None
        if not callable(pos_fn):
            return False
        try:
            for venue in (self.venue_a, self.venue_b):
                pos = pos_fn(venue, self.current_symbol)
                if pos is not None and float(getattr(pos, "qty", 0.0) or 0.0) != 0.0:
                    return False
        except Exception:  # noqa: BLE001 — 查不到就不释放，保守保留内层
            return False
        return True

    def _release_inner(self) -> None:
        """释放当前标的两腿归属并摘掉内层（current_symbol 保留，
        供面板显示刚完成的标的，下次选标时更新）。"""
        if self.current_symbol:
            for venue in (self.venue_a, self.venue_b):
                try:
                    self.executor.release(self.owner, venue,
                                          self.current_symbol)
                except Exception:  # noqa: BLE001 — 释放失败不挡循环
                    pass
        self._inner = None

    def _after_inner_tick(self, now: float) -> None:
        """内层 tick 后检查单笔完成：收尾、释放、计数、进冷却。"""
        if self._inner is None:
            return
        if not getattr(self._inner, "cycle_done", False):
            return
        try:
            self._inner.stop()   # 已空仓，幂等收尾
        except Exception:  # noqa: BLE001 — 收尾失败不挡释放与循环
            pass
        self._release_inner()
        self.cycles_completed += 1
        cooldown = self.params.get("cooldown_seconds")
        self._cooldown_until = now + (float(cooldown) if cooldown else 30.0)
        self.auto_phase = "cooldown"

    def _forward_inner(self, now: float, allow_open: bool) -> None:
        try:
            self._inner.tick(now, allow_open=allow_open)
        except Exception:  # noqa: BLE001 — 内层异常不外抛，保留内层继续管仓
            self.last_error = "本笔运行暂时异常，机器人会继续尝试处理"
            return
        self._after_inner_tick(now)

    # ----- 调度 -----
    def tick(self, now: float, allow_open: bool = True) -> None:
        if self.stopped:
            return
        try:
            self._tick(now, allow_open)
        except Exception:  # noqa: BLE001 — 监督器异常绝不外抛拖垮引擎
            if not self.last_error:
                self.last_error = "全自动运行暂时异常，稍后自动重试"

    def _tick(self, now: float, allow_open: bool) -> None:
        if not allow_open:
            # 停机期：只转发收尾，不选新标、不开新仓；双腿空仓后
            # 释放内层，等恢复后重新选标
            if self._inner is not None:
                try:
                    self._inner.tick(now, allow_open=False)
                except Exception:  # noqa: BLE001
                    self.last_error = "本笔运行暂时异常，机器人会继续尝试处理"
                    return
                if self._inner_flat():
                    self._release_inner()
            return
        if self._inner is not None:
            self.auto_phase = "running"
            self._forward_inner(now, allow_open=True)
            return
        if now < self._cooldown_until:
            self.auto_phase = "cooldown"
            return
        self.auto_phase = "scanning"
        try:
            row = self._pick(self._scan_rows())
        except Exception:  # noqa: BLE001 — 扫描异常停在 scanning 下轮重试
            self.last_error = "行情扫描暂时异常，稍后自动重试"
            return
        if row is None or not row.get("symbol"):
            self.last_error = "暂时没有符合条件的标的，稍后自动重试"
            return
        symbol = str(row["symbol"])
        try:
            inner = self._build_inner(symbol, row)
        except Exception:  # noqa: BLE001 — 构造异常停在 scanning 下轮重试
            self.last_error = "候选标的启动暂时异常，稍后自动重试"
            return
        if inner is None:
            # 中性网格区间定不下来：本轮跳过，下轮重试
            self.last_error = "网格区间暂时定不下来，稍后自动重试"
            return
        self._inner = inner
        self.current_symbol = symbol
        self.auto_phase = "running"
        self.last_error = ""
        self._forward_inner(now, allow_open=True)

    def stop(self) -> None:
        self.stopped = True
        self.auto_phase = "stopped"
        if self._inner is not None:
            try:
                self._inner.stop()
            except Exception:  # noqa: BLE001 — 停止动作尽力而为
                pass
