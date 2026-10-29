#!/usr/bin/env python3
"""Buy-in 5-day dip strategy checker for A-share ETFs.

Rules
-----
Let day1..day5 be the last 5 trading days (day5 = today / the as-of date).

1. cumulative dip: from the close of the day before day1 (close0) down to the
   LOW of day5 must fall by at least 5%:
   (low5 - close0) / close0 <= -0.05
   Using low5 (not close5) captures the intraday flush even if the close
   recovers - this is also how a gap-up open on day1 is ignored.
   - If not -> "not qualified for buying in 5 day dip strategy."
2. day5 close >= day5 open AND day5 close <= day4 close
   (close back above its own open, but not above yesterday's close).
   - If not -> not qualified.
   - Yes -> "qualified for buying in 5 day dip strategy"
   - No  -> "not qualified for buying in 5 day dip strategy."

Data source: Eastmoney public daily K-line endpoint (no auth, stdlib only).

Usage
-----
    python buy_in_5_day_dip.py                 # 513520, latest trading day
    python buy_in_5_day_dip.py 510300          # another ETF
    python buy_in_5_day_dip.py 513520 --date 2026-10-29   # as-of a past date
    python buy_in_5_day_dip.py 513520 --scan 2026-01-01   # list all qualified dates
    python buy_in_5_day_dip.py 513520 --scan 2026-01-01 --end 2026-06-30
    python buy_in_5_day_dip.py 513520 --after 2026-01-01 --days 3  # signal performance
    python buy_in_5_day_dip.py 513520 --csv bars.csv      # offline / from file
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Iterable, Sequence

DEFAULT_CODE = "513520"
LOOKBACK = 6           # 5 days of the pattern + 1 bar for the dip baseline
DIP_THRESHOLD = -0.05  # rule 1: (low5 - close0) / close0

QUALIFIED_MSG = "qualified for buying in 5 day dip strategy"
NOT_QUALIFIED_MSG = "not qualified for buying in 5 day dip strategy."


@dataclass(frozen=True)
class Bar:
    date: str
    open: float
    close: float
    high: float = float("nan")
    low: float = float("nan")
    volume: float = float("nan")


# --------------------------------------------------------------------------- #
# data access
# --------------------------------------------------------------------------- #
def _eastmoney_secid(code: str) -> str:
    """Market prefix used by Eastmoney: 1 = Shanghai, 0 = Shenzhen."""
    code = code.strip()
    if code[0] in "569" or code.startswith("11"):
        return f"1.{code}"
    return f"0.{code}"


def _eastmoney_symbol(code: str) -> str:
    """Tencent-style symbol, e.g. sh513520 / sz159915."""
    prefix = "sh" if _eastmoney_secid(code).startswith("1.") else "sz"
    return f"{prefix}{code.strip()}"


def _http_json(url: str, timeout: float) -> dict:
    req = urllib.request.Request(url, headers={
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"),
        "Referer": "https://quote.eastmoney.com/",
    })
    last_err: Exception | None = None
    for _ in range(3):                     # these endpoints drop connections sporadically
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:           # noqa: BLE001
            last_err = exc
    raise RuntimeError(f"request failed: {last_err}") from last_err


def _fetch_eastmoney(code: str, limit: int, end_date: str | None,
                     timeout: float) -> list[Bar]:
    params = {
        "secid": _eastmoney_secid(code),
        "fields1": "f1,f2,f3,f4,f5",
        "fields2": "f51,f52,f53,f54,f55,f56,f57",
        "klt": "101",          # daily
        "fqt": "1",            # forward-adjusted
        "end": (end_date or "20500101").replace("-", ""),
        "lmt": str(limit),
    }
    # Eastmoney rejects percent-encoded commas inside fields1/fields2 -> safe=","
    url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?"
           + urllib.parse.urlencode(params, safe=","))
    payload = _http_json(url, timeout)
    klines = (payload.get("data") or {}).get("klines") or []
    if not klines:
        raise RuntimeError("no klines in Eastmoney response")

    bars: list[Bar] = []
    for line in klines:
        f = line.split(",")
        bars.append(Bar(date=f[0], open=float(f[1]), close=float(f[2]),
                        high=float(f[3]), low=float(f[4]), volume=float(f[5])))
    return bars


def _fetch_tencent(code: str, limit: int, end_date: str | None,
                   timeout: float) -> list[Bar]:
    param = f"{_eastmoney_symbol(code)},day,,{end_date or ''},{limit},qfq"
    url = ("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param="
           + urllib.parse.quote(param, safe=",:")
           + "&_var=&r=0.1")
    payload = _http_json(url, timeout)
    node = (payload.get("data") or {}).get(_eastmoney_symbol(code)) or {}
    rows = node.get("qfqday") or node.get("day") or []
    if not rows:
        raise RuntimeError("no klines in Tencent response")

    bars = [Bar(date=r[0], open=float(r[1]), close=float(r[2]),
                high=float(r[3]), low=float(r[4]), volume=float(r[5]))
            for r in rows]
    return bars


def fetch_bars(code: str = DEFAULT_CODE, limit: int = LOOKBACK,
               end_date: str | None = None, timeout: float = 10.0) -> list[Bar]:
    """Fetch the last `limit` daily bars, oldest first (last element = as-of day).

    Tries Eastmoney first, then Tencent as a fallback. Both return forward-adjusted
    (qfq) prices, so splits/dividends don't create fake gaps.
    """
    errors: list[str] = []
    for provider in (_fetch_eastmoney, _fetch_tencent):
        try:
            bars = provider(code, limit, end_date, timeout)
        except Exception as exc:           # noqa: BLE001
            errors.append(f"{provider.__name__}: {exc}")
            continue
        bars.sort(key=lambda b: b.date)
        if end_date:
            bars = [b for b in bars if b.date <= end_date] or bars
        return bars[-limit:]
    raise RuntimeError(f"all data providers failed for {code} -> " + "; ".join(errors))


def load_bars_csv(path: str) -> list[Bar]:
    """Load bars from CSV with columns date,open,close[,high,low,volume]."""
    bars: list[Bar] = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            bars.append(Bar(date=row["date"].strip(),
                            open=float(row["open"]), close=float(row["close"]),
                            high=float(row.get("high") or "nan"),
                            low=float(row.get("low") or "nan"),
                            volume=float(row.get("volume") or "nan")))
    bars.sort(key=lambda b: b.date)
    return bars


def save_bars_csv(bars: Iterable[Bar], path: str) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["date", "open", "close", "high", "low", "volume"])
        for b in bars:
            w.writerow([b.date, b.open, b.close, b.high, b.low, b.volume])


# --------------------------------------------------------------------------- #
# strategy
# --------------------------------------------------------------------------- #
@dataclass
class Result:
    qualified: bool
    message: str
    reason: str
    window: list[Bar]
    dip_return: float | None = None


def evaluate(bars: Sequence[Bar], threshold: float = DIP_THRESHOLD) -> Result:
    """Apply the 5-day dip strategy to the last 5 bars (oldest first).

    Rule 1 needs one extra bar before the window (its close as the dip
    baseline), so `bars` must contain at least 6 bars.
    """
    if len(bars) < 6:
        raise ValueError(f"need at least 6 daily bars, got {len(bars)}")

    window = list(bars[-5:])
    day4, day5 = window[-2], window[-1]
    close0 = bars[-6].close

    # 1. cumulative dip: close0 -> day5 low must be at least `threshold` down
    low5 = day5.low if day5.low == day5.low else day5.close  # NaN -> fall back
    dip = (low5 - close0) / close0
    if dip > threshold:
        return Result(False, NOT_QUALIFIED_MSG,
                      f"cumulative dip {dip:.2%} (close0 {close0:.3f} -> day5 low "
                      f"{low5:.3f}) is not below {threshold:.1%}",
                      window, dip)

    # 2. day5: close >= own open (reversal) AND close <= day4 close (no breakout)
    if day5.close < day5.open:
        return Result(False, NOT_QUALIFIED_MSG,
                      f"dip {dip:.2%} qualifies, but today closed at "
                      f"{day5.close:.3f} < open {day5.open:.3f} (no reversal candle)",
                      window, dip)
    if day5.close > day4.close:
        return Result(False, NOT_QUALIFIED_MSG,
                      f"dip {dip:.2%} qualifies and today closed >= open "
                      f"({day5.close:.3f} >= {day5.open:.3f}), but day5 close is "
                      f"above day4 close {day4.close:.3f}",
                      window, dip)

    return Result(True, QUALIFIED_MSG,
                  f"dip {dip:.2%} to day5 low {low5:.3f} (close0 {close0:.3f}), "
                  f"today closed {day5.close:.3f} >= open {day5.open:.3f} "
                  f"and <= day4 close {day4.close:.3f}",
                  window, dip)


# --------------------------------------------------------------------------- #
# scan: find every qualified trading date in a range
# --------------------------------------------------------------------------- #
def scan_qualified_dates(code: str = DEFAULT_CODE, start_date: str = "",
                         end_date: str | None = None,
                         threshold: float = DIP_THRESHOLD,
                         timeout: float = 10.0) -> tuple[list[Result], int]:
    """Evaluate the 5-day dip strategy on every trading day in the range.

    A day D "qualifies" if the 5-bar window ending at D passes all rules.
    Returns (qualified_results, total_days_scanned).

    start_date: "YYYY-MM-DD"; empty string = from the earliest available bar.
    end_date:   "YYYY-MM-DD"; None = latest trading day (today).
    """
    # 800 bars ~= 3+ years of trading days; enough context before start_date
    bars = fetch_bars(code, limit=800, end_date=end_date, timeout=timeout)
    qualified: list[Result] = []
    scanned = 0
    for i in range(5, len(bars)):
        day = bars[i].date
        if start_date and day < start_date:
            continue
        if end_date and day > end_date:
            break
        scanned += 1
        res = evaluate(bars[:i + 1], threshold=threshold)
        if res.qualified:
            qualified.append(res)
    return qualified, scanned


def format_scan_report(code: str, results: Sequence[Result],
                       start_date: str, end_date: str | None,
                       scanned: int) -> str:
    span = f"{start_date or 'earliest available'} ~ {end_date or 'today'}"
    lines = [f"ETF {code} - 5-day dip scan [{span}]",
             f"trading days scanned: {scanned}, qualified: {len(results)}", ""]
    if not results:
        lines.append("no qualified dates found")
        return "\n".join(lines)
    lines.append(f"{'signal date':<14}{'5-day drop':>12}{'detail'}")
    for r in results:
        drop = "n/a" if r.dip_return is None else f"{r.dip_return:+.2%}"
        lines.append(f"{r.window[-1].date:<14}{drop:>12}  {r.reason}")
    return "\n".join(lines)


def performance_report(code: str, start_date: str, days: int = 3,
                       end_date: str | None = None,
                       threshold: float = DIP_THRESHOLD,
                       timeout: float = 10.0) -> tuple[str, int]:
    """Report per-day performance of every signal after start_date.

    Buy price = signal-day close. Returns are close-to-close for the next
    `days` trading days, plus win-rate / average stats per horizon.
    Returns (report_text, number_of_signals).
    """
    # 800 bars ~= 3+ years of trading days; enough context before start_date
    bars = fetch_bars(code, limit=800, end_date=end_date, timeout=timeout)
    signals = [i for i in range(5, len(bars))
               if bars[i].date >= start_date
               and evaluate(bars[:i + 1], threshold=threshold).qualified]

    span = f"{start_date} ~ {end_date or 'today'}"
    lines = [f"ETF {code} - signal performance [{span}]",
             f"buy at signal-day close, hold {days} trading day(s)", ""]
    if not signals:
        lines.append("no qualified signals found in range")
        return "\n".join(lines), 0

    per_horizon: list[list[float]] = [[] for _ in range(days)]
    for idx in signals:
        sig = bars[idx]
        lines.append(f"signal {sig.date} (buy @ {sig.close:.3f})")
        lines.append(f"  {'date':<12}{'open':>8}{'close':>8}{'day%':>9}{'cum%':>9}")
        for k in range(1, days + 1):
            j = idx + k
            if j >= len(bars):
                lines.append(f"  {'(incomplete)':<12}")
                break
            b = bars[j]
            day_ret = b.close / bars[j - 1].close - 1
            cum_ret = b.close / sig.close - 1
            per_horizon[k - 1].append(cum_ret)
            lines.append(f"  {b.date:<12}{b.open:>8.3f}{b.close:>8.3f}"
                         f"{day_ret:>+8.2%}{cum_ret:>+8.2%}")
        lines.append("")

    lines.append("summary (close-to-close, buy @ signal-day close)")
    for k in range(days):
        rets = per_horizon[k]
        if not rets:
            lines.append(f"  day {k + 1}: no completed samples")
            continue
        wins = sum(r > 0 for r in rets)
        lines.append(f"  day {k + 1}: samples={len(rets)}  "
                     f"win-rate={wins}/{len(rets)} ({wins / len(rets):.0%})  "
                     f"avg={sum(rets) / len(rets):+.2%}  "
                     f"best={max(rets):+.2%}  worst={min(rets):+.2%}")
    return "\n".join(lines), len(signals)


def format_report(code: str, res: Result) -> str:
    lines = [f"ETF {code} - last 5 trading days"]
    lines.append(f"{'date':<12}{'open':>8}{'close':>8}{'chg%':>9}")
    prev = None
    for b in res.window:
        chg = (b.close / prev - 1) * 100 if prev else float("nan")
        chg_s = "   n/a" if prev is None else f"{chg:+.2f}%"
        lines.append(f"{b.date:<12}{b.open:>8.3f}{b.close:>8.3f}{chg_s:>9}")
        prev = b.close
    if res.dip_return is not None:
        lines.append(f"dip vs close0 (to day5 low): {res.dip_return:.2%}")
    lines.append(f"reason: {res.reason}")
    lines.append("")
    lines.append(f"RESULT: {res.message}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="5-day dip buy-in checker")
    ap.add_argument("code", nargs="?", default=DEFAULT_CODE,
                    help=f"ETF code (default {DEFAULT_CODE})")
    ap.add_argument("--date", help="as-of date YYYY-MM-DD (default: latest bar)")
    ap.add_argument("--csv", help="read bars from this CSV instead of the network")
    ap.add_argument("--save-csv", help="cache fetched bars to this CSV")
    ap.add_argument("--threshold", type=float, default=DIP_THRESHOLD,
                    help="rule 1 threshold: (low5 - close0) / close0 "
                         "(default -0.05)")
    ap.add_argument("--scan", metavar="START_DATE",
                    help="scan from START_DATE (YYYY-MM-DD) to --end/--today "
                         "and list every qualified trading date")
    ap.add_argument("--after", metavar="START_DATE",
                    help="like --scan, but also report the next --days trading "
                         "days after each signal and win-rate stats")
    ap.add_argument("--days", type=int, default=3,
                    help="holding horizon in trading days for --after (default 3)")
    ap.add_argument("--end", help="scan end date YYYY-MM-DD (default: today)")
    ap.add_argument("--json", action="store_true", help="print JSON result")
    args = ap.parse_args(argv)

    try:
        if args.scan is not None:
            results, scanned = scan_qualified_dates(
                args.code, start_date=args.scan, end_date=args.end,
                threshold=args.threshold)
        elif args.after is not None:
            report, n_signals = performance_report(
                args.code, start_date=args.after, days=args.days,
                end_date=args.end, threshold=args.threshold)
        else:
            if args.csv:
                bars = load_bars_csv(args.csv)
            else:
                bars = fetch_bars(args.code, limit=LOOKBACK, end_date=args.date)
                if args.save_csv:
                    save_bars_csv(bars, args.save_csv)
            results, scanned = [evaluate(bars, threshold=args.threshold)], 1
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps({
            "code": args.code,
            "scan_start": args.scan,
            "scan_end": args.end,
            "days_scanned": scanned,
            "qualified_dates": [r.window[-1].date for r in results],
            "results": [{
                "signal_date": r.window[-1].date,
                "qualified": r.qualified,
                "message": r.message,
                "reason": r.reason,
                "dip_return": None if r.dip_return is None
                else round(r.dip_return, 6),
                "window": [b.__dict__ for b in r.window],
            } for r in results],
        }, ensure_ascii=False, indent=2))
    elif args.after is not None:
        print(report)
    elif args.scan is not None:
        print(format_scan_report(args.code, results, args.scan, args.end, scanned))
    else:
        print(format_report(args.code, results[0]))
    if args.after is not None:
        return 0 if n_signals else 1
    return 0 if results else 1


if __name__ == "__main__":
    sys.exit(main())
