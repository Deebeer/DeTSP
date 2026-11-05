"""统计库：中枢库（溢价）+ 费差库（资金费），由全量快照批量聚合。

数据来源是 MarketScanner.full_snapshot() 的批量快照（逐所一次
拿到全市场价格 + 资金费），本地算全部组合，不逐组合调接口：

- 溢价分钟聚合：全部有序场所对 × 共同标的，每快照一个样本，
  按分钟聚合落盘 premium_minutes/<base>__<hedge>__<symbol>.csv。
  注意：快照只有中间价、无买卖盘口，这是中价近似——sell_edge
  样本取 premium 本身、buy_edge 样本取 -premium；真实可成交
  edge 还要扣盘口价差，库里的触发次数因此是上限估计。中枢
  计算口径不重写，直接复用 tools.analyze.analyze_rows。
- 费差小时聚合：全部无序场所对 × 共同标的，费率先过
  core.risk.evaluate_funding_pair 闸门（单期费率合理性 + 口径
  一致性）并统一换算到每小时，再按小时聚合落盘
  funding_hours/<va>__<vb>__<symbol>.csv。
- recompute_premium：最近 24 小时、样本 ≥180 分钟才准入，
  每小时滚动重算 premium_library.json。
- funding_stats / funding_gate：最近 7 天小时行算持续性
  （中位持续时长 / 达标占比 / 平均净费差），持续性是开仓硬关：
  中位持续时长 ≥ 回本时间 × 2 才放行。

LibraryService 是后台录制服务：定时 record + 定时 recompute，
单次异常只记 last_error、不退出循环。
"""

from __future__ import annotations

import csv
import json
import math
import os
import statistics
import tempfile
import threading
import time
from pathlib import Path

from ..tools.analyze import analyze_rows
from .config import VENUE_IDS
from .fee_table import FeeTable
from .risk import evaluate_funding_pair

PREMIUM_FIELDS = [
    "minute_ts", "premium_mean_bps", "sell_edge_mean_bps",
    "sell_edge_max_bps", "buy_edge_mean_bps", "buy_edge_max_bps",
]
FUNDING_FIELDS = ["hour_ts", "rate_a_bps_h", "rate_b_bps_h", "net_bps_h"]

_MIN_PREMIUM_MINUTES = 180        # 中枢库准入：至少 3 小时分钟行
_PREMIUM_WINDOW_SECONDS = 86400   # 中枢库窗口：最近 24 小时
_FUNDING_WINDOW_SECONDS = 7 * 86400  # 费差库窗口：最近 7 天
_MIN_FUNDING_SAMPLE_HOURS = 24    # 费差硬关：至少 24 小时样本
_SAFETY_MULTIPLE = 2.0            # 持续性安全倍数：中位时长 ≥ 回本 × 2


def _atomic_write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _append_csv(path: Path, fields: list[str], row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    new_file = not path.exists() or path.stat().st_size == 0
    with path.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        if new_file:
            writer.writeheader()
        writer.writerow(row)


def _read_csv_rows(path: Path) -> list[dict]:
    """读 CSV 并把每列转 float；坏行跳过。"""
    rows: list[dict] = []
    if not path.exists():
        return rows
    with path.open(newline="", encoding="utf-8") as fh:
        for raw in csv.DictReader(fh):
            try:
                rows.append({k: float(v) for k, v in raw.items()
                             if v is not None and v != ""})
            except (TypeError, ValueError):
                continue
    return rows


class StatsLibrary:
    """中枢库 + 费差库：快照聚合落盘、滚动重算、持续性统计。"""

    def __init__(self, data_dir, fee_table: FeeTable) -> None:
        self._data_dir = Path(data_dir)
        self._fee_table = fee_table
        self._lock = threading.Lock()
        # (base, hedge, symbol) -> {"minute_ts": int, "premiums": [float]}
        self._premium_acc: dict[tuple[str, str, str], dict] = {}
        # (va, vb, symbol) -> {"hour_ts": int,
        #   "samples": [(rate_a_bps_h, rate_b_bps_h, net_bps_h)]}
        self._funding_acc: dict[tuple[str, str, str], dict] = {}
        self._lib_cache: dict | None = None
        self._lib_cache_mtime: float | None = None

    @property
    def data_dir(self) -> Path:
        return self._data_dir

    @property
    def fee_table(self) -> FeeTable:
        return self._fee_table

    # ----- 录制 -----

    def record_snapshot(self, snap: dict, ts: float | None = None) -> None:
        """把一张全量快照喂进分钟 / 小时聚合器（跨界时落盘旧桶）。"""
        if ts is None:
            ts = snap.get("ts") if isinstance(snap, dict) else None
        now = float(ts) if ts is not None else time.time()
        data = (snap or {}).get("data") or {}
        minute_ts = int(now // 60) * 60
        hour_ts = int(now // 3600) * 3600
        with self._lock:
            self._record_premium_locked(data, minute_ts)
            self._record_funding_locked(data, hour_ts)

    def _record_premium_locked(self, data: dict, minute_ts: int) -> None:
        for base in VENUE_IDS:
            base_map = data.get(base) or {}
            if not base_map:
                continue
            for hedge in VENUE_IDS:
                if hedge == base:
                    continue
                hedge_map = data.get(hedge) or {}
                if not hedge_map:
                    continue
                for symbol in sorted(set(base_map) & set(hedge_map)):
                    pb = (base_map.get(symbol) or {}).get("price")
                    ph = (hedge_map.get(symbol) or {}).get("price")
                    if pb is None or ph is None:
                        continue
                    try:
                        pb_f, ph_f = float(pb), float(ph)
                    except (TypeError, ValueError):
                        continue
                    if pb_f <= 0 or ph_f <= 0:
                        continue
                    premium = (pb_f / ph_f - 1) * 10_000
                    key = (base, hedge, symbol)
                    acc = self._premium_acc.get(key)
                    if acc is not None and acc["minute_ts"] != minute_ts:
                        self._write_premium_locked(key, acc)
                        acc = None
                    if acc is None:
                        acc = {"minute_ts": minute_ts, "premiums": []}
                        self._premium_acc[key] = acc
                    acc["premiums"].append(premium)

    def _record_funding_locked(self, data: dict, hour_ts: int) -> None:
        for i, va in enumerate(VENUE_IDS):
            va_map = data.get(va) or {}
            if not va_map:
                continue
            for vb in VENUE_IDS[i + 1:]:
                vb_map = data.get(vb) or {}
                if not vb_map:
                    continue
                for symbol in sorted(set(va_map) & set(vb_map)):
                    fa = (va_map.get(symbol) or {}).get("funding")
                    fb = (vb_map.get(symbol) or {}).get("funding")
                    if fa is None or fb is None:
                        continue
                    ok, _reason, ha, hb = evaluate_funding_pair(fa, fb)
                    if not ok:
                        continue
                    sample = (ha * 10_000, hb * 10_000,
                              abs(ha - hb) * 10_000)
                    key = (va, vb, symbol)
                    acc = self._funding_acc.get(key)
                    if acc is not None and acc["hour_ts"] != hour_ts:
                        self._write_funding_locked(key, acc)
                        acc = None
                    if acc is None:
                        acc = {"hour_ts": hour_ts, "samples": []}
                        self._funding_acc[key] = acc
                    acc["samples"].append(sample)

    # ----- 落盘 -----

    def _premium_path(self, key: tuple[str, str, str]) -> Path:
        base, hedge, symbol = key
        return (self._data_dir / "premium_minutes"
                / f"{base}__{hedge}__{symbol}.csv")

    def _funding_path(self, key: tuple[str, str, str]) -> Path:
        va, vb, symbol = key
        return (self._data_dir / "funding_hours"
                / f"{va}__{vb}__{symbol}.csv")

    def _write_premium_locked(self, key, acc: dict) -> None:
        premiums: list[float] = acc["premiums"]
        if not premiums:
            return
        buy_samples = [-p for p in premiums]
        _append_csv(self._premium_path(key), PREMIUM_FIELDS, {
            "minute_ts": acc["minute_ts"],
            "premium_mean_bps": sum(premiums) / len(premiums),
            "sell_edge_mean_bps": sum(premiums) / len(premiums),
            "sell_edge_max_bps": max(premiums),
            "buy_edge_mean_bps": sum(buy_samples) / len(buy_samples),
            "buy_edge_max_bps": max(buy_samples),
        })

    def _write_funding_locked(self, key, acc: dict) -> None:
        samples: list[tuple[float, float, float]] = acc["samples"]
        if not samples:
            return
        n = len(samples)
        _append_csv(self._funding_path(key), FUNDING_FIELDS, {
            "hour_ts": acc["hour_ts"],
            "rate_a_bps_h": sum(s[0] for s in samples) / n,
            "rate_b_bps_h": sum(s[1] for s in samples) / n,
            "net_bps_h": sum(s[2] for s in samples) / n,
        })

    def flush(self) -> None:
        """强制落盘当前未满的分钟 / 小时桶（样本为空不写）。"""
        with self._lock:
            for key, acc in self._premium_acc.items():
                self._write_premium_locked(key, acc)
            self._premium_acc.clear()
            for key, acc in self._funding_acc.items():
                self._write_funding_locked(key, acc)
            self._funding_acc.clear()

    # ----- 中枢库 -----

    def recompute_premium(self, now: float) -> dict:
        """最近 24 小时重算中枢库，原子写 premium_library.json。"""
        now = float(now)
        cutoff = now - _PREMIUM_WINDOW_SECONDS
        result: dict[str, dict] = {}
        minutes_dir = self._data_dir / "premium_minutes"
        if minutes_dir.exists():
            for path in sorted(minutes_dir.glob("*.csv")):
                parts = path.stem.split("__")
                if len(parts) != 3:
                    continue
                base, hedge, symbol = parts
                rows = [r for r in _read_csv_rows(path)
                        if r.get("minute_ts", 0) >= cutoff]
                if len(rows) < _MIN_PREMIUM_MINUTES:
                    continue
                try:
                    taker_base, _ = self._fee_table.get(base)
                    taker_hedge, _ = self._fee_table.get(hedge)
                except ValueError:
                    continue
                try:
                    res = analyze_rows(
                        rows, fees_bps=taker_base + taker_hedge,
                        min_fires=10)
                except ValueError:
                    continue
                suggested = res["suggested"]
                upper, lower = suggested["upper_bps"], suggested["lower_bps"]
                table = {t["band"]: t for t in res["table"]}
                result[f"{base}__{hedge}__{symbol}"] = {
                    "midline_bps": suggested["midline_bps"],
                    "upper_bps": upper,
                    "lower_bps": lower,
                    "sell_fires": table[upper]["sell_fires"]
                    if upper in table else 0,
                    "buy_fires": table[lower]["buy_fires"]
                    if lower in table else 0,
                    "minutes": len(rows),
                    "sample_hours": round(len(rows) / 60, 2),
                    "updated_at": now,
                }
        lib_path = self._data_dir / "premium_library.json"
        _atomic_write_json(lib_path, result)
        with self._lock:
            self._lib_cache = {k: dict(v) for k, v in result.items()}
            try:
                self._lib_cache_mtime = lib_path.stat().st_mtime
            except OSError:
                self._lib_cache_mtime = None
        return result

    def _load_library(self) -> dict:
        lib_path = self._data_dir / "premium_library.json"
        if not lib_path.exists():
            with self._lock:
                self._lib_cache = {}
                self._lib_cache_mtime = None
            return {}
        try:
            mtime = lib_path.stat().st_mtime
        except OSError:
            return {}
        with self._lock:
            if self._lib_cache is not None \
                    and self._lib_cache_mtime == mtime:
                return {k: dict(v) for k, v in self._lib_cache.items()}
        try:
            raw = json.loads(lib_path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return {}
        if not isinstance(raw, dict):
            return {}
        with self._lock:
            self._lib_cache = {k: dict(v) for k, v in raw.items()
                               if isinstance(v, dict)}
            self._lib_cache_mtime = mtime
            return {k: dict(v) for k, v in self._lib_cache.items()}

    def premium_entries(self) -> dict:
        """中枢库全部条目：{"base__hedge__symbol": entry}。"""
        return self._load_library()

    def premium_entry(self, base: str, hedge: str,
                      symbol: str) -> dict | None:
        """单条中枢（附带 base/hedge/symbol 字段）；不存在返回 None。"""
        entry = self._load_library().get(f"{base}__{hedge}__{symbol}")
        if entry is None:
            return None
        return {**entry, "base": base, "hedge": hedge, "symbol": symbol}

    # ----- 费差库 -----

    @staticmethod
    def _ordered_pair(va: str, vb: str) -> tuple[str, str]:
        if va not in VENUE_IDS:
            raise ValueError(f"未知 venue_id：{va}，合法值：{VENUE_IDS}")
        if vb not in VENUE_IDS:
            raise ValueError(f"未知 venue_id：{vb}，合法值：{VENUE_IDS}")
        return (va, vb) if VENUE_IDS.index(va) < VENUE_IDS.index(vb) \
            else (vb, va)

    def funding_stats(self, va: str, vb: str, symbol: str,
                      min_line_bps: float, now: float) -> dict | None:
        """最近 7 天费差持续性统计；无样本返回 None。"""
        ova, ovb = self._ordered_pair(va, vb)
        if ova == ovb:
            return None
        rows = _read_csv_rows(self._funding_path((ova, ovb, symbol)))
        cutoff = float(now) - _FUNDING_WINDOW_SECONDS
        rows = sorted(
            (r for r in rows if r.get("hour_ts", 0) >= cutoff
             and "net_bps_h" in r and "hour_ts" in r),
            key=lambda r: r["hour_ts"])
        if not rows:
            return None
        nets = [r["net_bps_h"] for r in rows]
        segments: list[int] = []
        run = 0
        above_count = 0
        for net in nets:
            if net >= min_line_bps:
                run += 1
                above_count += 1
            elif run:
                segments.append(run)
                run = 0
        if run:
            segments.append(run)
        return {
            "median_duration_h": float(statistics.median(segments))
            if segments else 0,
            "above_ratio": above_count / len(rows),
            "avg_net_bps_per_hour": sum(nets) / len(nets),
            "sample_hours": len(rows),
            "updated_at": rows[-1]["hour_ts"],
        }

    @staticmethod
    def breakeven_hours(roundtrip_cost_bps: float,
                        current_net_bps: float) -> float | None:
        """回本时间（小时）= 开平总成本 ÷ 当前净费差；费差不为正返回 None。"""
        if current_net_bps is None or current_net_bps <= 0:
            return None
        return float(roundtrip_cost_bps) / float(current_net_bps)

    def funding_gate(self, va: str, vb: str, symbol: str,
                     min_line_bps: float, roundtrip_cost_bps: float,
                     current_net_bps: float, now: float) -> dict:
        """持续性硬关：样本 ≥24h 且中位持续时长 ≥ 回本时间 × 2 才放行。"""
        stats = self.funding_stats(va, vb, symbol, min_line_bps, now)
        be = self.breakeven_hours(roundtrip_cost_bps, current_net_bps)
        if stats is None:
            ok, reason = False, "样本不足：暂无费差历史数据"
        elif be is None:
            ok, reason = False, "无当前费差：当前净费差不为正，无法计算回本时间"
        elif stats["sample_hours"] < _MIN_FUNDING_SAMPLE_HOURS:
            ok, reason = False, (
                f"样本不足：只有 {stats['sample_hours']} 小时，"
                f"需要至少 {_MIN_FUNDING_SAMPLE_HOURS} 小时")
        elif stats["median_duration_h"] < be * _SAFETY_MULTIPLE:
            ok, reason = False, (
                f"持续性不足：中位持续 {stats['median_duration_h']} 小时，"
                f"需 ≥ 回本 {be:.2f} 小时 × {_SAFETY_MULTIPLE:g}")
        else:
            ok, reason = True, "通过：持续性达标"
        return {"ok": ok, "reason": reason, "stats": stats,
                "breakeven_hours": be}


class LibraryService:
    """后台录制服务：定时 record 快照 + 定时 recompute 中枢库。"""

    def __init__(self, scanner, data_dir, fee_table: FeeTable | None = None,
                 record_interval: float = 60.0,
                 recompute_interval: float = 3600.0) -> None:
        self._scanner = scanner
        self._data_dir = Path(data_dir)
        self.fee_table = fee_table if fee_table is not None else FeeTable(
            self._data_dir / "fees.json")
        self.library = StatsLibrary(self._data_dir, self.fee_table)
        self._record_interval = max(float(record_interval), 0.01)
        self._recompute_interval = max(float(recompute_interval), 0.01)
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_record_ts: float | None = None
        self._last_recompute_ts: float | None = None
        self._last_error = ""

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, name="library-service", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=5.0)
        self._thread = None
        try:
            self.library.flush()
        except Exception as e:  # noqa: BLE001 — 停机落盘失败只记错
            self._last_error = f"{type(e).__name__}: {e}"

    def status(self) -> dict:
        try:
            entries = len(self.library.premium_entries())
        except Exception:  # noqa: BLE001 — 状态查询不因读库失败崩
            entries = 0
        return {
            "running": self._thread is not None and self._thread.is_alive(),
            "last_record_ts": self._last_record_ts,
            "last_recompute_ts": self._last_recompute_ts,
            "premium_entries": entries,
            "last_error": self._last_error,
        }

    def _run(self) -> None:
        # 双到期调度：record 与 recompute 各按各的周期，互不拖延
        next_record = time.time()  # 首轮立即 record
        next_recompute = next_record + self._recompute_interval
        while not self._stop_event.is_set():
            now = time.time()
            wait = max(min(next_record, next_recompute) - now, 0.0)
            if wait > 0 and self._stop_event.wait(timeout=wait):
                break
            now = time.time()
            if now >= next_record:
                next_record = now + self._record_interval
                try:
                    snap = self._scanner.full_snapshot()
                    self.library.record_snapshot(snap)
                    ts = snap.get("ts") if isinstance(snap, dict) else None
                    self._last_record_ts = float(ts) if ts is not None \
                        else time.time()
                except Exception as e:  # noqa: BLE001 — 单次异常不退出
                    self._last_error = f"{type(e).__name__}: {e}"
            now = time.time()
            if now >= next_recompute:
                next_recompute = now + self._recompute_interval
                try:
                    self.library.recompute_premium(now)
                    self._last_recompute_ts = now
                except Exception as e:  # noqa: BLE001
                    self._last_error = f"{type(e).__name__}: {e}"
