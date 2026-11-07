"""盘口录制器：每秒采样一对场所的盘口，每分钟写一行 CSV。

思路移植自 entropy-arb（MIT）的 recorder：溢价中枢不能拍脑袋，
必须先录制真实盘口再分析。本模块只读盘口、不下单、不需要凭据
（live 场所的 book() 是公开行情；paper 场所则录合成数据）。

CSV 字段（每分钟一行）：
  minute_ts, time_utc            分钟起始（epoch 秒 / ISO UTC）
  base_bid/ask, hedge_bid/ask    该分钟最后一个新鲜样本的盘口
  premium_open/high/low/close/mean_bps  两所中价溢价 (base/hedge-1)*1e4
  sell_edge_mean/max_bps         卖 base 的可成交溢价 (base_bid/hedge_ask-1)*1e4（费前）
  buy_edge_mean/max_bps          买 base 的可成交溢价 (hedge_bid/base_ask-1)*1e4（费前）
  samples                        该分钟有效样本数
"""

from __future__ import annotations

import argparse
import csv
import os
import time
from datetime import datetime, timezone
from pathlib import Path

FIELDS = [
    "minute_ts", "time_utc", "base_bid", "base_ask", "hedge_bid", "hedge_ask",
    "premium_open_bps", "premium_high_bps", "premium_low_bps",
    "premium_close_bps", "premium_mean_bps",
    "sell_edge_mean_bps", "sell_edge_max_bps",
    "buy_edge_mean_bps", "buy_edge_max_bps", "samples",
]


def _bps(ratio_minus_one: float) -> float:
    return ratio_minus_one * 10_000


class MinuteAggregator:
    """把秒级样本聚合成分钟 bar；跨分钟时 add() 返回完成的一行。"""

    def __init__(self) -> None:
        self._minute: int | None = None
        self._reset()

    def _reset(self) -> None:
        self._premiums: list[float] = []
        self._sell_edges: list[float] = []
        self._buy_edges: list[float] = []
        self._last: tuple[float, float, float, float] | None = None

    def add(self, ts: float, base_bid: float, base_ask: float,
            hedge_bid: float, hedge_ask: float) -> dict | None:
        minute = int(ts // 60) * 60
        done = None
        if self._minute is not None and minute != self._minute:
            # 上一分钟可能整分钟都是坏盘口（无有效样本）：_bar 对
            # 空样本会崩，只在有样本时才出 bar（与 flush 同口径）
            if self._premiums:
                done = self._bar()
            self._reset()
        self._minute = minute
        base_mid = (base_bid + base_ask) / 2
        hedge_mid = (hedge_bid + hedge_ask) / 2
        if hedge_mid > 0 and base_ask > 0 and hedge_ask > 0:
            self._premiums.append(_bps(base_mid / hedge_mid - 1))
            self._sell_edges.append(_bps(base_bid / hedge_ask - 1))
            self._buy_edges.append(_bps(hedge_bid / base_ask - 1))
            self._last = (base_bid, base_ask, hedge_bid, hedge_ask)
        return done

    def flush(self) -> dict | None:
        """收尾时输出未满的一分钟（无样本则 None）。"""
        if self._minute is None or not self._premiums:
            return None
        bar = self._bar()
        self._minute = None
        self._reset()
        return bar

    def _bar(self) -> dict:
        p = self._premiums
        bb, ba, hb, ha = self._last  # type: ignore[misc]
        return {
            "minute_ts": self._minute,
            "time_utc": datetime.fromtimestamp(
                self._minute, tz=timezone.utc).isoformat(),
            "base_bid": bb, "base_ask": ba, "hedge_bid": hb, "hedge_ask": ha,
            "premium_open_bps": round(p[0], 3),
            "premium_high_bps": round(max(p), 3),
            "premium_low_bps": round(min(p), 3),
            "premium_close_bps": round(p[-1], 3),
            "premium_mean_bps": round(sum(p) / len(p), 3),
            "sell_edge_mean_bps": round(
                sum(self._sell_edges) / len(self._sell_edges), 3),
            "sell_edge_max_bps": round(max(self._sell_edges), 3),
            "buy_edge_mean_bps": round(
                sum(self._buy_edges) / len(self._buy_edges), 3),
            "buy_edge_max_bps": round(max(self._buy_edges), 3),
            "samples": len(p),
        }


class CsvRecorder:
    """聚合器 + CSV 落盘（追加模式，首次写表头）。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._agg = MinuteAggregator()
        new_file = not self.path.exists() or self.path.stat().st_size == 0
        self._fh = self.path.open("a", newline="", encoding="utf-8")
        self._writer = csv.DictWriter(self._fh, fieldnames=FIELDS)
        if new_file:
            self._writer.writeheader()
            self._fh.flush()
        self.bars_written = 0

    def add_sample(self, ts: float, base_bid: float, base_ask: float,
                   hedge_bid: float, hedge_ask: float) -> None:
        bar = self._agg.add(ts, base_bid, base_ask, hedge_bid, hedge_ask)
        if bar is not None:
            self._write(bar)

    def _write(self, bar: dict) -> None:
        self._writer.writerow(bar)
        self._fh.flush()
        self.bars_written += 1

    def close(self) -> None:
        bar = self._agg.flush()
        if bar is not None:
            self._write(bar)
        self._fh.close()


def record_pair(base_venue, hedge_venue, symbol: str, csv_path,
                interval: float = 1.0, duration: float | None = None,
                now_fn=time.time, sleep_fn=time.sleep) -> int:
    """采样主循环：返回采样次数。duration=None 表示一直跑到被中断。"""
    rec = CsvRecorder(csv_path)
    started = now_fn()
    n = 0
    try:
        while True:
            now = now_fn()
            if duration is not None and now - started >= duration:
                break
            try:
                bb = base_venue.book(symbol)
                hb = hedge_venue.book(symbol)
                rec.add_sample(now, bb.bid, bb.ask, hb.bid, hb.ask)
                n += 1
            except Exception:
                pass  # 单场所行情抖动跳过这一秒，不中断录制
            sleep_fn(interval)
    finally:
        rec.close()
    return n


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="DeTSP 盘口录制（只读，不下单）")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--env", default=".env")
    ap.add_argument("--base-venue", required=True)
    ap.add_argument("--hedge-venue", required=True)
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--csv", default="logs/minutes.csv")
    ap.add_argument("--interval", type=float, default=1.0)
    ap.add_argument("--duration", type=float, default=None,
                    help="录制秒数（默认一直跑，Ctrl+C 停止）")
    args = ap.parse_args(argv)

    from ..core.config import build_config, load_env_file, load_yaml_config
    from ..venues.factory import build_venues

    env = dict(os.environ)
    for k, v in load_env_file(args.env).items():
        env.setdefault(k, v)
    cfg_path = Path(args.config)
    yaml_data = load_yaml_config(cfg_path) if cfg_path.exists() else {}
    app_config = build_config(env=env, yaml_data=yaml_data)
    venues = build_venues(app_config)
    print(f"[recorder] {args.base_venue} vs {args.hedge_venue} {args.symbol}"
          f" → {args.csv}（只读盘口，不下单）")
    try:
        n = record_pair(venues[args.base_venue], venues[args.hedge_venue],
                        args.symbol, args.csv, interval=args.interval,
                        duration=args.duration)
    except KeyboardInterrupt:
        n = -1
        print("\n[recorder] 已停止")
    if n >= 0:
        print(f"[recorder] 完成：{n} 个样本")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
