"""统一成交账本（SQLite）：三策略共用一本账。

fills 表记录每笔成交，盈亏/磨损/交易量统一从这里聚合，
不从各策略的本地状态各算各的。

线程安全：引擎线程写账、面板线程读账，连接允许跨线程，
全部访问用锁串行化。
"""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Fill:
    venue: str
    account: str
    symbol: str
    strategy: str
    side: str            # buy / sell
    qty: float
    price: float
    fee: float = 0.0
    order_id: str = ""
    ts: float = 0.0      # 0 = 记账时自动填当前时间

    @property
    def notional(self) -> float:
        return abs(self.qty) * self.price


class Ledger:
    def __init__(self, path: str | Path = ":memory:") -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        with self._lock:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS fills (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts REAL NOT NULL,
                    venue TEXT NOT NULL,
                    account TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    strategy TEXT NOT NULL,
                    side TEXT NOT NULL,
                    qty REAL NOT NULL,
                    price REAL NOT NULL,
                    fee REAL NOT NULL DEFAULT 0,
                    order_id TEXT DEFAULT ''
                )
                """
            )
            self._conn.commit()

    def record(self, fill: Fill) -> int:
        ts = fill.ts if fill.ts > 0 else time.time()
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO fills (ts, venue, account, symbol, strategy, side, qty, price, fee, order_id)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (ts, fill.venue, fill.account, fill.symbol, fill.strategy,
                 fill.side, fill.qty, fill.price, fill.fee, fill.order_id),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def count(self, strategy: str | None = None) -> int:
        sql = "SELECT COUNT(*) FROM fills"
        args: tuple = ()
        if strategy:
            sql += " WHERE strategy = ?"
            args = (strategy,)
        with self._lock:
            return int(self._conn.execute(sql, args).fetchone()[0])

    def total_volume(self, strategy: str | None = None) -> float:
        """成交名义金额合计（交易量口径）。"""
        sql = "SELECT COALESCE(SUM(ABS(qty) * price), 0) FROM fills"
        args: tuple = ()
        if strategy:
            sql += " WHERE strategy = ?"
            args = (strategy,)
        with self._lock:
            return float(self._conn.execute(sql, args).fetchone()[0])

    def total_fees(self, strategy: str | None = None) -> float:
        sql = "SELECT COALESCE(SUM(fee), 0) FROM fills"
        args: tuple = ()
        if strategy:
            sql += " WHERE strategy = ?"
            args = (strategy,)
        with self._lock:
            return float(self._conn.execute(sql, args).fetchone()[0])

    def volume_by_strategy(self) -> dict[str, float]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT strategy, COALESCE(SUM(ABS(qty) * price), 0) FROM fills GROUP BY strategy"
            ).fetchall()
        return {r[0]: float(r[1]) for r in rows}

    def recent_fills(self, limit: int = 50, since: float | None = None) -> list[dict]:
        """最近成交（倒序），供面板账本页展示。since 为起始时间戳（秒）。"""
        sql = ("SELECT ts, venue, account, symbol, strategy, side, qty, price,"
               " fee, order_id FROM fills")
        args: tuple = ()
        if since is not None:
            sql += " WHERE ts >= ?"
            args = (since,)
        sql += " ORDER BY id DESC LIMIT ?"
        args = args + (int(limit),)
        cols = ("ts", "venue", "account", "symbol", "strategy", "side",
                "qty", "price", "fee", "order_id")
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [dict(zip(cols, r)) for r in rows]

    def stats_since(self, since: float) -> dict:
        """since 之后的成交笔数 / 成交量 / 手续费合计（面板总览用）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(ABS(qty) * price), 0),"
                " COALESCE(SUM(fee), 0) FROM fills WHERE ts >= ?",
                (since,),
            ).fetchone()
        return {"fills": int(row[0]), "volume": float(row[1]),
                "fees": float(row[2])}

    def close(self) -> None:
        with self._lock:
            self._conn.close()
