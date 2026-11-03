"""统一成交账本（SQLite）：三策略共用一本账。

fills 表记录每笔成交，盈亏/磨损/交易量统一从这里聚合，
不从各策略的本地状态各算各的。
"""

from __future__ import annotations

import sqlite3
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
        self._conn = sqlite3.connect(str(path))
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
        return int(self._conn.execute(sql, args).fetchone()[0])

    def total_volume(self, strategy: str | None = None) -> float:
        """成交名义金额合计（交易量口径）。"""
        sql = "SELECT COALESCE(SUM(ABS(qty) * price), 0) FROM fills"
        args: tuple = ()
        if strategy:
            sql += " WHERE strategy = ?"
            args = (strategy,)
        return float(self._conn.execute(sql, args).fetchone()[0])

    def total_fees(self, strategy: str | None = None) -> float:
        sql = "SELECT COALESCE(SUM(fee), 0) FROM fills"
        args: tuple = ()
        if strategy:
            sql += " WHERE strategy = ?"
            args = (strategy,)
        return float(self._conn.execute(sql, args).fetchone()[0])

    def volume_by_strategy(self) -> dict[str, float]:
        rows = self._conn.execute(
            "SELECT strategy, COALESCE(SUM(ABS(qty) * price), 0) FROM fills GROUP BY strategy"
        ).fetchall()
        return {r[0]: float(r[1]) for r in rows}

    def close(self) -> None:
        self._conn.close()
