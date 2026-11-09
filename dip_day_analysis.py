#!/usr/bin/env python3
"""T0 大跌 -> T+H 表现分析（默认 H=1，即 T+1）。

规则
----
1. 遍历某标的全部日线，挑出「当日跌幅 >= 阈值」的交易日作为信号日 T0
   （默认 -2%，即 pct_change <= -2.00%）。
2. 取信号日之后第 H 个交易日（默认第 1 个，即 T+1）的行情数据。
3. 比较 T0 的**开盘价**与 TH 的**收盘价**，输出涨跌额与涨跌幅%。

数据来源：本地 MySQL（表 stock_daily / etf_daily，连接参数从 .env 读取）。

用法
-----
    python dip_day_analysis.py                       # 600276，跌 2%+，看 T+1
    python dip_day_analysis.py 300750                # 另一只
    python dip_day_analysis.py 600276 --threshold -3   # 跌 3%+ 才算信号
    python dip_day_analysis.py 600276 --horizon 3      # 看 T+3（原默认值）
    python dip_day_analysis.py 600276 --start 2026-01-01 --end 2026-10-31
    python dip_day_analysis.py 600276 --csv out.csv    # 导出明细
    python dip_day_analysis.py 600276 --json           # JSON 输出
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path

DEFAULT_SYMBOL = "600276"
DEFAULT_THRESHOLD = -2.0   # 当日跌幅阈值（%），<= 此值即信号
DEFAULT_HORIZON = 1        # 向后看第 H 个交易日（1 = 次日 T+1）
DEFAULT_TABLE = "stock_daily"


# --------------------------------------------------------------------------- #
# 配置与连接
# --------------------------------------------------------------------------- #
def load_env_file(path: Path | None = None) -> None:
    """加载脚本同目录 .env（KEY=VALUE），不覆盖已有环境变量。"""
    env_path = path or Path(__file__).resolve().with_name(".env")
    if not env_path.is_file():
        return
    for raw in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


def connect(args: argparse.Namespace):
    import pymysql

    load_env_file()
    return pymysql.connect(
        host=args.mysql_host or os.environ.get("MYSQL_HOST", "127.0.0.1"),
        port=int(args.mysql_port or os.environ.get("MYSQL_PORT", "3306")),
        user=args.mysql_user or os.environ.get("MYSQL_USER", "root"),
        password=os.environ.get("MYSQL_PASSWORD", ""),
        database=args.mysql_db or os.environ.get("MYSQL_DB", "libra_schema"),
        charset="utf8mb4",
    )


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #
@dataclass
class Bar:
    date: str
    open: float
    high: float
    low: float
    close: float
    pct: float | None
    volume: float
    amount: float | None
    adj_close: float | None = None   # 未复权/复权前收盘，用于补算 pct


@dataclass
class Signal:
    t0: Bar
    tn: Bar
    diff: float
    pct: float


def _f(v) -> float:
    return float("nan") if v is None else float(v)


def fetch_bars(conn, symbol: str, table: str,
               start: str | None, end: str | None) -> list[Bar]:
    sql = (f"SELECT trade_date, open, high, low, close, pct_change, volume, amount "
           f"FROM `{table}` WHERE symbol = %s AND adjust = 'none'")
    params: list = [symbol]
    if start:
        sql += " AND trade_date >= %s"
        params.append(start)
    if end:
        sql += " AND trade_date <= %s"
        params.append(end)
    sql += " ORDER BY trade_date"
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    return [Bar(date=str(r[0]), open=_f(r[1]), high=_f(r[2]), low=_f(r[3]),
                close=_f(r[4]), pct=None if r[5] is None else float(r[5]),
                volume=_f(r[6]), amount=None if r[7] is None else float(r[7]))
            for r in rows]


def fill_missing_pct(all_bars: list[Bar]) -> None:
    """pct_change 缺失时用相邻收盘补算（首行无法补）。"""
    prev = None
    for b in all_bars:
        if b.pct is None and prev is not None and prev > 0:
            b.pct = (b.close - prev) / prev * 100
        prev = b.close


# --------------------------------------------------------------------------- #
# 核心逻辑
# --------------------------------------------------------------------------- #
def find_signals(all_bars: list[Bar], threshold: float, horizon: int,
                 start: str | None, end: str | None) -> tuple[list[Signal], int]:
    """返回 (信号列表, 因样本不足而跳过的信号数)。"""
    signals: list[Signal] = []
    skipped = 0
    for i, b in enumerate(all_bars):
        if b.pct is None or b.pct > threshold:
            continue
        if start and b.date < start:
            continue
        if end and b.date > end:
            continue
        j = i + horizon
        if j >= len(all_bars):
            skipped += 1          # 尾部信号没有足够的后续交易日
            continue
        tn = all_bars[j]
        diff = tn.close - b.open
        signals.append(Signal(t0=b, tn=tn, diff=diff, pct=diff / b.open * 100))
    return signals, skipped


# --------------------------------------------------------------------------- #
# 输出
# --------------------------------------------------------------------------- #
def _dw(text: str) -> int:
    """字符串的显示宽度（中日韩字符算 2 列）。"""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def _pad(text: str, width: int, align: str = "left") -> str:
    fill = " " * max(0, width - _dw(text))
    return text + fill if align == "left" else fill + text


def format_report(symbol: str, signals: list[Signal], threshold: float,
                  horizon: int, skipped: int, total: int) -> str:
    lines = [
        f"{symbol}  T0 大跌 -> T+{horizon} 表现",
        f"信号条件：当日涨跌幅 <= {threshold:.2f}%（共扫描 {total} 个交易日）",
        f"对比口径：T0 开盘价  ->  T+{horizon} 收盘价",
        "",
    ]
    if not signals:
        lines.append("没有符合条件的交易日")
        return "\n".join(lines)

    spec = [("T0 日期", 12, "l"), ("T0 开", 9, "r"), ("T0 跌%", 8, "r"),
            (f"T+{horizon} 日期", 13, "l"), (f"T+{horizon} 收", 10, "r"),
            ("涨跌额", 9, "r"), ("涨跌%", 9, "r")]
    head = "".join(_pad(t, w, a) for t, w, a in spec)
    lines.append(head)
    lines.append("-" * _dw(head))
    for s in signals:
        cells = [s.t0.date, f"{s.t0.open:.3f}", f"{s.t0.pct:.2f}",
                 s.tn.date, f"{s.tn.close:.3f}",
                 f"{s.diff:+.3f}", f"{s.pct:+.2f}%"]
        lines.append("".join(_pad(c, w, a) for c, (_, w, a) in zip(cells, spec)))

    rets = [s.pct for s in signals]
    wins = sum(r > 0 for r in rets)
    lines += [
        "",
        f"样本 {len(rets)} 个"
        + (f"（另有 {skipped} 个信号因后续交易日不足 {horizon} 天被跳过）" if skipped else ""),
        f"上涨 {wins} / 下跌 {len(rets) - wins}   胜率 {wins / len(rets):.1%}",
        f"平均 {sum(rets) / len(rets):+.2f}%   中位数 {sorted(rets)[len(rets) // 2]:+.2f}%   "
        f"最佳 {max(rets):+.2f}%   最差 {min(rets):+.2f}%",
    ]
    return "\n".join(lines)


def save_csv(signals: list[Signal], horizon: int, path: str) -> None:
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["t0_date", "t0_open", "t0_high", "t0_low", "t0_close", "t0_pct",
                    f"t{horizon}_date", f"t{horizon}_open", f"t{horizon}_high",
                    f"t{horizon}_low", f"t{horizon}_close",
                    "diff", "pct"])
        for s in signals:
            w.writerow([s.t0.date, f"{s.t0.open:.3f}", f"{s.t0.high:.3f}",
                        f"{s.t0.low:.3f}", f"{s.t0.close:.3f}",
                        "" if s.t0.pct is None else f"{s.t0.pct:.3f}",
                        s.tn.date, f"{s.tn.open:.3f}", f"{s.tn.high:.3f}",
                        f"{s.tn.low:.3f}", f"{s.tn.close:.3f}",
                        f"{s.diff:+.3f}", f"{s.pct:+.4f}"])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="T0 大跌 -> T+H 表现分析")
    ap.add_argument("symbol", nargs="?", default=DEFAULT_SYMBOL,
                    help=f"标的代码（默认 {DEFAULT_SYMBOL}）")
    ap.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD,
                    help=f"当日跌幅阈值（%%，默认 {DEFAULT_THRESHOLD}）")
    ap.add_argument("--horizon", type=int, default=DEFAULT_HORIZON,
                    help=f"向后看第几个交易日（默认 {DEFAULT_HORIZON}）")
    ap.add_argument("--table", default=DEFAULT_TABLE,
                    help=f"数据表（默认 {DEFAULT_TABLE}；ETF 用 etf_daily）")
    ap.add_argument("--start", help="信号日下限 YYYY-MM-DD")
    ap.add_argument("--end", help="信号日上限 YYYY-MM-DD")
    ap.add_argument("--csv", help="把明细导出到 CSV")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("--mysql-host")
    ap.add_argument("--mysql-port")
    ap.add_argument("--mysql-user")
    ap.add_argument("--mysql-db")
    args = ap.parse_args(argv)

    if args.horizon < 1:
        print("--horizon 必须 >= 1", file=sys.stderr)
        return 2

    try:
        conn = connect(args)
    except Exception as exc:  # noqa: BLE001
        print(f"数据库连接失败：{exc}", file=sys.stderr)
        return 2

    try:
        all_bars = fetch_bars(conn, args.symbol, args.table, args.start, args.end)
    finally:
        conn.close()

    if not all_bars:
        print(f"{args.table} 里没有 {args.symbol} 的数据（adjust='none'）", file=sys.stderr)
        return 1

    fill_missing_pct(all_bars)
    signals, skipped = find_signals(all_bars, args.threshold, args.horizon,
                                    args.start, args.end)

    if args.json:
        print(json.dumps({
            "symbol": args.symbol,
            "table": args.table,
            "threshold": args.threshold,
            "horizon": args.horizon,
            "scanned": len(all_bars),
            "skipped": skipped,
            "signals": [{
                "t0_date": s.t0.date, "t0_open": s.t0.open, "t0_pct": s.t0.pct,
                f"t{args.horizon}_date": s.tn.date,
                f"t{args.horizon}_close": s.tn.close,
                "diff": round(s.diff, 3), "pct": round(s.pct, 4),
            } for s in signals],
        }, ensure_ascii=False, indent=2))
    else:
        print(format_report(args.symbol, signals, args.threshold, args.horizon,
                            skipped, len(all_bars)))
        if args.csv:
            save_csv(signals, args.horizon, args.csv)
            print(f"\n明细已导出：{args.csv}")
    return 0 if signals else 1


if __name__ == "__main__":
    sys.exit(main())
