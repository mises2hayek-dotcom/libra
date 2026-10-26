#!/usr/bin/env python
"""ETF 日频行情抓取工具。多数据源自动降级：东财(curl_cffi) -> 东财(requests) -> 腾讯 -> 新浪。

用法示例
--------
    python etf_daily.py                                 # 默认抓 513520
    python etf_daily.py --adjust qfq                     # 513520 前复权
    python etf_daily.py 510300 159915 --adjust qfq       # 指定标的
    python etf_daily.py --source sina                    # 指定源
    python etf_daily.py --search 沪深300                  # 按名称查代码

数据源对照
----------
| 源       | 复权 | 成交额/振幅/换手率 | 备注                         |
|----------|------|--------------------|------------------------------|
| em-cffi  | 支持 | 齐全               | 东财 + 浏览器 TLS 指纹       |
| em       | 支持 | 齐全               | 东财 + 普通 requests         |
| tencent  | 支持 | 无                 | 单次最多 640 根              |
| sina     | 不支持 | 无               | 成交量原为股，已折算为手     |
| akshare  | 支持 | 齐全               | 原库，东财裸请求             |

输出：<out>/<代码>_<复权>_<开始>_<结束>.csv，UTF-8 BOM 编码，Excel 双击可看。
列：date, open, close, high, low, volume, amount, amplitude, pct_change, change, turnover
　　volume 统一为「手」（1 手 = 100 份），各源差异在入库前已折算。
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import akshare as ak
import pandas as pd
import requests

EM_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
EM_UT = "7eea3edcaed734bea9cbfc24409ed989"
TX_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
HEADERS = {"User-Agent": UA, "Referer": "https://quote.eastmoney.com/"}

COLUMN_MAP = {
    "日期": "date",
    "开盘": "open",
    "收盘": "close",
    "最高": "high",
    "最低": "low",
    "成交量": "volume",
    "成交额": "amount",
    "振幅": "amplitude",
    "涨跌幅": "pct_change",
    "涨跌额": "change",
    "换手率": "turnover",
}
COLUMN_ORDER = [
    "date",
    "open",
    "close",
    "high",
    "low",
    "volume",
    "amount",
    "amplitude",
    "pct_change",
    "change",
    "turnover",
]
ADJUST_CHOICES = {"none": "", "qfq": "qfq", "hfq": "hfq"}
ADJUST_CODE = {"": "0", "qfq": "1", "hfq": "2"}
TX_ADJUST_KEY = {"": "day", "qfq": "qfqday", "hfq": "hfqday"}
SOURCES = ["auto", "em-cffi", "em", "tencent", "sina", "akshare"]
AUTO_ORDER = ["em-cffi", "em", "tencent", "sina"]
TX_MAX_BARS = 640
DEFAULT_SYMBOL = "513520"  # 当前只关注日经 ETF，省略代码时用它

# 各源成交量原始单位 -> 手的换算系数（1 手 = 100 份）
VOLUME_TO_LOT = {
    "em-cffi": 1.0,  # 东财已是手
    "em": 1.0,
    "akshare": 1.0,  # akshare 走东财，已是手
    "tencent": 1.0,  # 腾讯已是手
    "sina": 0.01,  # 新浪为股，除以 100
}


def use_direct_connection() -> None:
    """绕过系统代理直连。国内行情站点走代理反而常被中断。"""
    for key in ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        os.environ.pop(key, None)
    os.environ["no_proxy"] = "*"
    os.environ["NO_PROXY"] = "*"


def use_proxy(url: str) -> None:
    """让本次请求走指定代理，如 http://127.0.0.1:7897（Clash 默认端口）。"""
    if not url.startswith(("http://", "https://", "socks5://")):
        url = f"http://{url}"
    for key in ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        os.environ[key] = url
    os.environ.pop("no_proxy", None)
    os.environ.pop("NO_PROXY", None)


def normalize_symbol(raw: str) -> str:
    """把 sh510300 / 510300.SH / 510300 统一成 6 位数字代码。"""
    match = re.search(r"\d{6}", raw)
    if not match:
        raise ValueError(f"无法识别的 ETF 代码：{raw}（需要 6 位数字，如 510300）")
    return match.group(0)


def em_secid(symbol: str, mid: int) -> str:
    return f"{mid}.{symbol}"


def em_market_id(symbol: str) -> int:
    """东财 secid 的市场号：1=上交所，0=深交所。"""
    return 1 if symbol[0] in "56" else 0


def prefixed(symbol: str) -> str:
    """新浪/腾讯需要 sh/sz 前缀。"""
    return ("sh" if symbol[0] in "56" else "sz") + symbol


def parse_date(value: str | None, default: date) -> str:
    """接受 20240101 / 2024-01-01 / 2024/01/01，返回 YYYYMMDD。"""
    if not value:
        return default.strftime("%Y%m%d")
    digits = re.sub(r"\D", "", value)
    if len(digits) != 8:
        raise ValueError(f"日期格式不正确：{value}（应为 20240101 或 2024-01-01）")
    try:
        datetime.strptime(digits, "%Y%m%d")
    except ValueError as exc:
        raise ValueError(f"日期不存在：{value}") from exc
    return digits


def dashed(yyyymmdd: str) -> str:
    return f"{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:]}"


def _standardize(df: pd.DataFrame) -> pd.DataFrame:
    """统一列名、类型、排序，并补齐缺失列。"""
    df = df.rename(columns=COLUMN_MAP)
    if "date" not in df.columns:
        raise ValueError(f"返回列不符合预期：{list(df.columns)}")

    df = df[[c for c in COLUMN_ORDER if c in df.columns]].copy()
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    for col in COLUMN_ORDER[1:]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["date"]).sort_values("date").drop_duplicates("date")
    df = df.reset_index(drop=True)
    if "volume" in df.columns:
        df["volume"] = df["volume"].round(0).astype("Int64")
    return df


def _to_lots(df: pd.DataFrame, source: str) -> pd.DataFrame:
    """把成交量统一折算为手（1 手 = 100 份）。"""
    factor = VOLUME_TO_LOT.get(source, 1.0)
    if factor == 1.0 or "volume" not in df.columns:
        return df
    df = df.copy()
    df["volume"] = (df["volume"] * factor).round(0).astype("Int64")
    return df


def _em_params(symbol: str, start: str, end: str, adjust: str, mid: int) -> dict:
    return {
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f116",
        "ut": EM_UT,
        "klt": "101",
        "fqt": ADJUST_CODE[adjust],
        "beg": start,
        "end": end,
        "secid": em_secid(symbol, mid),
    }


def _parse_em(payload: dict) -> pd.DataFrame:
    data = payload.get("data") or {}
    klines = data.get("klines") or []
    if not klines:
        return pd.DataFrame()
    rows = [line.split(",") for line in klines]
    return _standardize(pd.DataFrame(rows, columns=list(COLUMN_MAP)))


def fetch_em_http(symbol: str, start: str, end: str, adjust: str = "") -> pd.DataFrame:
    """东财接口 + 普通 requests（带浏览器头）。"""
    for mid in (em_market_id(symbol), 1 - em_market_id(symbol)):
        resp = requests.get(
            EM_URL, params=_em_params(symbol, start, end, adjust, mid), headers=HEADERS, timeout=15
        )
        resp.raise_for_status()
        df = _parse_em(resp.json())
        if not df.empty:
            return df
    raise ValueError(f"{symbol} 在 {start}-{end} 区间没有数据")


def fetch_em_cffi(symbol: str, start: str, end: str, adjust: str = "") -> pd.DataFrame:
    """东财接口 + curl_cffi 模拟 Chrome TLS 指纹（绕过指纹级拦截）。"""
    try:
        from curl_cffi import requests as cffi_requests
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("未安装 curl_cffi，请执行 pip install curl_cffi") from exc

    for mid in (em_market_id(symbol), 1 - em_market_id(symbol)):
        resp = cffi_requests.get(
            EM_URL,
            params=_em_params(symbol, start, end, adjust, mid),
            headers=HEADERS,
            impersonate="chrome",
            timeout=20,
        )
        resp.raise_for_status()
        df = _parse_em(resp.json())
        if not df.empty:
            return df
    raise ValueError(f"{symbol} 在 {start}-{end} 区间没有数据")


def fetch_tencent(symbol: str, start: str, end: str, adjust: str = "") -> pd.DataFrame:
    """腾讯行情，支持复权；缺点是无成交额/振幅/换手率，单次上限 640 根。"""
    code = prefixed(symbol)
    params = {"param": f"{code},day,{dashed(start)},{dashed(end)},{TX_MAX_BARS},{adjust}"}
    resp = requests.get(
        TX_URL, params=params, headers={"User-Agent": UA, "Referer": "https://gu.qq.com/"}, timeout=15
    )
    resp.raise_for_status()
    payload = resp.json()
    node = (payload.get("data") or {}).get(code) or {}
    rows = node.get(TX_ADJUST_KEY[adjust]) or node.get("day") or []
    if not rows:
        raise ValueError(f"{symbol} 在 {start}-{end} 区间没有数据")

    df = pd.DataFrame(
        [row[:6] for row in rows],
        columns=["date", "open", "close", "high", "low", "volume"],
    )
    df = _standardize(df)
    if len(df) >= TX_MAX_BARS:
        print(
            f"  ! 腾讯源单次上限 {TX_MAX_BARS} 根，区间过长可能截断，建议缩短 --start",
            file=sys.stderr,
        )
    return df


def fetch_sina(symbol: str, start: str, end: str) -> pd.DataFrame:
    """新浪备用源：6 列，不支持复权。"""
    df = ak.fund_etf_hist_sina(symbol=prefixed(symbol))
    if df is None or df.empty:
        raise ValueError(f"{symbol} 在新浪源没有数据")
    df = _standardize(df)
    lo, hi = pd.to_datetime(dashed(start)), pd.to_datetime(dashed(end))
    df = df.loc[(df["date"] >= lo) & (df["date"] <= hi)].reset_index(drop=True)
    if df.empty:
        raise ValueError(f"{symbol} 在 {start}-{end} 区间没有数据")
    return df


def fetch_akshare(symbol: str, start: str, end: str, adjust: str = "") -> pd.DataFrame:
    df = ak.fund_etf_hist_em(
        symbol=symbol, period="daily", start_date=start, end_date=end, adjust=adjust
    )
    if df is None or df.empty:
        raise ValueError(f"{symbol} 在 {start}-{end} 区间没有数据")
    return _standardize(df)


FETCHERS = {
    "em-cffi": fetch_em_cffi,
    "em": fetch_em_http,
    "tencent": fetch_tencent,
    "sina": fetch_sina,
    "akshare": fetch_akshare,
}


def fetch_with_retry(
    source: str,
    symbol: str,
    start: str,
    end: str,
    adjust: str,
    retries: int,
    backoff: float = 2.0,
) -> pd.DataFrame:
    fetch = FETCHERS[source]
    kwargs = {} if source == "sina" else {"adjust": adjust}
    last_error: Exception | None = None

    for attempt in range(1, retries + 1):
        try:
            return _to_lots(fetch(symbol, start, end, **kwargs), source)
        except Exception as exc:  # noqa: BLE001 - 网络异常种类多，统一重试
            last_error = exc
            if attempt < retries:
                wait = backoff * attempt
                print(f"  ! [{source}] 第 {attempt} 次失败（{exc}），{wait:.0f}s 后重试", file=sys.stderr)
                time.sleep(wait)
    raise RuntimeError(str(last_error)) from last_error


def fetch_daily(
    symbol: str,
    start: str,
    end: str,
    adjust: str = "",
    source: str = "auto",
    retries: int = 3,
) -> tuple[pd.DataFrame, str]:
    """抓取日线，返回 (DataFrame, 实际使用的数据源)。"""
    if source != "auto":
        return fetch_with_retry(source, symbol, start, end, adjust, retries), source

    errors: list[str] = []
    for candidate in AUTO_ORDER:
        try:
            df = fetch_with_retry(candidate, symbol, start, end, adjust, retries=1)
            return df, candidate
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{candidate}: {exc}")
            print(f"  - {candidate} 不可用（{exc}）", file=sys.stderr)
    raise RuntimeError("所有数据源均失败：\n    " + "\n    ".join(errors))


def save_csv(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False, encoding="utf-8-sig")


def describe(symbol: str, df: pd.DataFrame) -> str:
    first, last = df["date"].iloc[0].date(), df["date"].iloc[-1].date()
    window_return = (df["close"].iloc[-1] / df["close"].iloc[0] - 1) * 100
    volume = df["volume"].iloc[-1] if "volume" in df.columns else float("nan")
    return (
        f"  {symbol}: {len(df)} 行  {first} ~ {last}  "
        f"最新收盘 {df['close'].iloc[-1]:.3f}  "
        f"成交量 {volume:,.0f} 手  "
        f"区间涨跌 {window_return:+.2f}%"
    )


def search_etf(keyword: str, limit: int = 20) -> int:
    """按名称/代码关键词模糊查找 ETF。"""
    try:
        spot = ak.fund_etf_spot_em()
    except Exception as exc:  # noqa: BLE001
        print(f"查询 ETF 列表失败：{exc}", file=sys.stderr)
        return 1

    keyword = keyword.strip()
    mask = spot["代码"].astype(str).str.contains(keyword, na=False) | spot["名称"].astype(
        str
    ).str.contains(keyword, na=False)
    hits = spot.loc[mask, ["代码", "名称"]].head(limit)
    if hits.empty:
        print(f"没有匹配「{keyword}」的 ETF")
        return 1
    for row in hits.itertuples(index=False):
        print(f"{row.代码}\t{row.名称}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="抓取 ETF 日频行情（东财 / 腾讯 / 新浪，自动降级）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "symbols", nargs="*", help=f"ETF 代码，省略时默认 {DEFAULT_SYMBOL}（可传多个）"
    )
    parser.add_argument("--start", help="开始日期，默认一年前（20240101 或 2024-01-01）")
    parser.add_argument("--end", help="结束日期，默认今天")
    parser.add_argument(
        "--adjust",
        choices=sorted(ADJUST_CHOICES),
        default="none",
        help="复权方式：none(默认) / qfq(前复权) / hfq(后复权)",
    )
    parser.add_argument(
        "--source",
        choices=SOURCES,
        default="auto",
        help="数据源，默认 auto（em-cffi -> em -> tencent -> sina 依次尝试）",
    )
    parser.add_argument("--out", default="data/etf", help="输出目录，默认 data/etf")
    parser.add_argument("--retries", type=int, default=3, help="指定源时的重试次数，默认 3")
    parser.add_argument("--sleep", type=float, default=1.0, help="批量抓取间隔秒数，默认 1.0")
    parser.add_argument("--direct", action="store_true", help="忽略系统代理，直连行情站点")
    parser.add_argument("--proxy", metavar="URL", help="指定代理，如 http://127.0.0.1:7897")
    parser.add_argument("--search", metavar="关键词", help="按名称查代码，如 --search 沪深300")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.direct:
        use_direct_connection()
    elif args.proxy:
        use_proxy(args.proxy)

    if args.search:
        return search_etf(args.search)

    symbols: list[str] = args.symbols or [DEFAULT_SYMBOL]

    today = date.today()
    start = parse_date(args.start, today - timedelta(days=365))
    end = parse_date(args.end, today)
    if start > end:
        print(f"开始日期 {start} 晚于结束日期 {end}", file=sys.stderr)
        return 2

    adjust = ADJUST_CHOICES[args.adjust]
    if adjust and args.source in {"sina"}:
        print("! 新浪源不支持复权，已按不复权处理", file=sys.stderr)
        adjust = ""

    out_dir = Path(args.out)
    failures: list[str] = []
    adjust_label = args.adjust if adjust else "none"

    print(
        f"区间 {start} ~ {end}  复权 {adjust_label}  数据源 {args.source}  "
        f"输出到 {out_dir.resolve()}"
    )
    for index, raw in enumerate(symbols):
        try:
            symbol = normalize_symbol(raw)
        except ValueError as exc:
            print(f"! {exc}", file=sys.stderr)
            failures.append(raw)
            continue

        print(f"[{index + 1}/{len(symbols)}] {symbol} 抓取中 ...")
        try:
            df, used = fetch_daily(
                symbol, start, end, adjust, source=args.source, retries=args.retries
            )
        except Exception as exc:  # noqa: BLE001
            print(f"  ! {exc}", file=sys.stderr)
            failures.append(symbol)
            continue

        path = out_dir / f"{symbol}_{adjust_label}_{start}_{end}.csv"
        save_csv(df, path)
        print(describe(symbol, df))
        print(f"  数据源 {used}  -> {path}")
        if index < len(symbols) - 1:
            time.sleep(args.sleep)

    if failures:
        print(f"完成，{len(failures)} 个失败：{', '.join(failures)}", file=sys.stderr)
        return 1

    print(f"完成，共 {len(symbols)} 个标的。")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        raise SystemExit(130) from None
