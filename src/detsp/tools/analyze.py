"""溢价分析：读 recorder 的 minutes.csv，输出溢价分布与建议阈值。

方法（移植自 entropy-arb 的 analyzer 思路）：
- 中枢 midline 取录制溢价（分钟均值）的中位数——跨所溢价很少以 0 为
  中心，中枢测错就是系统性亏钱，所以必须实测且定期重测。
- 对每个候选入场带 band，统计历史上会触发的分钟数：
  卖 base：sell_edge_max − 双边费 > midline + band
  买 base：buy_edge_max − 双边费 > band − midline
  （触发计数用分钟内**最大值**列，与原仓 analyzer 一致：实盘
  策略是 tick 级判断的，一分钟内只要有一刻越线就会触发，用
  分钟均值统计会系统性低估触发次数、把带定得过窄。）
- 建议值 = 能达到最少触发次数（--min-fires）的最小 band；
  都达不到则取最大候选并提示数据不足。

输出的建议块可直接填进 config.yaml 中 premium_arb 任务的 params。
"""

from __future__ import annotations

import argparse
import csv
import statistics
from pathlib import Path

DEFAULT_CANDIDATES = [5, 10, 15, 20, 25, 30, 40, 50, 75, 100]


def load_rows(path: str | Path, hours: float | None = None) -> list[dict]:
    rows: list[dict] = []
    with Path(path).open(newline="", encoding="utf-8") as fh:
        for raw in csv.DictReader(fh):
            try:
                rows.append({k: float(v) for k, v in raw.items()
                             if k != "time_utc"})
            except (TypeError, ValueError):
                continue  # 跳过坏行
    if hours is not None and rows:
        cutoff = max(r["minute_ts"] for r in rows) - hours * 3600
        rows = [r for r in rows if r["minute_ts"] >= cutoff]
    return rows


def _pick(candidates: list[int], fires: list[int], min_fires: int) -> int:
    for band, n in zip(candidates, fires):
        if n >= min_fires:
            return band
    return candidates[-1]


def analyze_rows(rows: list[dict], fees_bps: float = 0.0,
                 min_fires: int = 10,
                 candidates: list[int] | None = None) -> dict:
    if not rows:
        raise ValueError("没有可分析的数据行（先用 recorder 录制）")
    candidates = candidates or DEFAULT_CANDIDATES
    premiums = [r["premium_mean_bps"] for r in rows]
    midline = statistics.median(premiums)
    sell_fires, buy_fires = [], []
    for band in candidates:
        # 触发计数用分钟最大值列（与原仓一致）：策略是 tick 级
        # 触发的，均值列会低估触发次数。旧录制文件若只有 mean
        # 列，按 mean 兜底（新录制恒有 max 列）
        sell_fires.append(sum(
            1 for r in rows
            if r.get("sell_edge_max_bps", r["sell_edge_mean_bps"])
            - fees_bps > midline + band))
        buy_fires.append(sum(
            1 for r in rows
            if r.get("buy_edge_max_bps", r["buy_edge_mean_bps"])
            - fees_bps > band - midline))
    table = [{"band": b, "sell_fires": s, "buy_fires": y}
             for b, s, y in zip(candidates, sell_fires, buy_fires)]
    sorted_p = sorted(premiums)
    return {
        "minutes": len(rows),
        "midline_bps": round(midline, 2),
        "premium_mean_bps": round(statistics.fmean(premiums), 2),
        "premium_std_bps": round(statistics.pstdev(premiums), 2)
        if len(premiums) > 1 else 0.0,
        "premium_p10_bps": round(sorted_p[int(len(sorted_p) * 0.10)], 2),
        "premium_p90_bps": round(sorted_p[int(len(sorted_p) * 0.90) - 1], 2),
        "fees_bps": fees_bps,
        "table": table,
        "suggested": {
            "midline_bps": round(midline, 1),
            "upper_bps": _pick(candidates, sell_fires, min_fires),
            "lower_bps": _pick(candidates, buy_fires, min_fires),
        },
    }


def format_report(res: dict) -> str:
    s = res["suggested"]
    lines = [
        f"样本：{res['minutes']} 分钟　扣费口径：{res['fees_bps']} bps",
        f"溢价中枢（中位数）：{res['midline_bps']} bps　"
        f"均值 {res['premium_mean_bps']}　标准差 {res['premium_std_bps']}　"
        f"P10 {res['premium_p10_bps']}　P90 {res['premium_p90_bps']}",
        "候选带触发次数（分钟数）：",
        "  band | sell | buy",
    ]
    lines += [f"  {t['band']:>4} | {t['sell_fires']:>4} | {t['buy_fires']:>4}"
              for t in res["table"]]
    lines += [
        "建议参数（填入 premium_arb 任务的 params）：",
        f"  midline_bps: {s['midline_bps']}",
        f"  upper_bps: {s['upper_bps']}",
        f"  lower_bps: {s['lower_bps']}",
        "注意：溢价中枢会漂移，定期重录重测；触发太少先多录几天数据。",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="DeTSP 溢价分析（定套利阈值）")
    ap.add_argument("--csv", default="logs/minutes.csv")
    ap.add_argument("--fees-bps", type=float, default=0.0,
                    help="双边 taker 手续费合计（bps）")
    ap.add_argument("--hours", type=float, default=None,
                    help="只看最近 N 小时（默认全部）")
    ap.add_argument("--min-fires", type=int, default=10)
    args = ap.parse_args(argv)
    rows = load_rows(args.csv, hours=args.hours)
    res = analyze_rows(rows, fees_bps=args.fees_bps, min_fires=args.min_fires)
    print(format_report(res))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
