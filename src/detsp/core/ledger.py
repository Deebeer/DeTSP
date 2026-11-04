"""统一成交账本（SQLite）：四策略共用一本账。

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

            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS closed_positions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    strategy TEXT NOT NULL DEFAULT '',
                    task TEXT NOT NULL DEFAULT '',
                    venue TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    qty REAL NOT NULL,
                    entry_price REAL NOT NULL DEFAULT 0,
                    close_price REAL,
                    close_price_source TEXT NOT NULL DEFAULT 'unknown',
                    opened_ts REAL NOT NULL DEFAULT 0,
                    closed_ts REAL NOT NULL
                )
                """
            )
            self._conn.commit()

    def record_closed_position(self, rec: dict) -> int:
        """落一条已平仓位记录（面板「策略记录 · 仓位历史」的数据源）。

        rec 字段：strategy（策略类型，可空）、task（任务名/归属，
        可空）、venue、symbol、side（long/short）、qty（绝对值）、
        entry_price、close_price（可 None）、close_price_source
        （fills=平仓成交均价 / mark=发现平仓时的标记价 /
        unknown=两者都拿不到）、opened_ts、closed_ts。
        """
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO closed_positions (strategy, task, venue,"
                " symbol, side, qty, entry_price, close_price,"
                " close_price_source, opened_ts, closed_ts)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (str(rec.get("strategy") or ""),
                 str(rec.get("task") or ""),
                 str(rec.get("venue") or ""),
                 str(rec.get("symbol") or ""),
                 str(rec.get("side") or ""),
                 float(rec.get("qty") or 0.0),
                 float(rec.get("entry_price") or 0.0),
                 rec.get("close_price"),
                 str(rec.get("close_price_source") or "unknown"),
                 float(rec.get("opened_ts") or 0.0),
                 float(rec.get("closed_ts") or time.time())),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def recent_closed_positions(self, limit: int = 50) -> list[dict]:
        """最近已平仓位（按平仓时间倒序），供面板策略记录展示。"""
        cols = ("id", "strategy", "task", "venue", "symbol", "side",
                "qty", "entry_price", "close_price",
                "close_price_source", "opened_ts", "closed_ts")
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, strategy, task, venue, symbol, side, qty,"
                " entry_price, close_price, close_price_source,"
                " opened_ts, closed_ts FROM closed_positions"
                " ORDER BY closed_ts DESC, id DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [dict(zip(cols, r)) for r in rows]

    def count_closed_positions(self) -> int:
        with self._lock:
            return int(self._conn.execute(
                "SELECT COUNT(*) FROM closed_positions").fetchone()[0])

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
