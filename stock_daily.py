#!/usr/bin/env python
"""A 股个股日频行情抓取工具。与 etf_daily.py 同一套功能，标的换成个股。

用法示例
--------
    python stock_daily.py                          # 默认抓 600276 + 300750，2024-01-01 起
    python stock_daily.py 600276 --adjust qfq      # 前复权
    python stock_daily.py 600276 300750 --source sina
    python stock_daily.py --to-mysql --calc-db     # 抓取入库，指标由存储过程计算
    python stock_daily.py --search 恒瑞            # 按名称查代码

数据源对照
----------
| 源       | 复权 | 成交额/振幅/换手率        | 备注                       |
|----------|------|---------------------------|----------------------------|
| em-cffi  | 支持 | 齐全（首选）              | 东财 + 浏览器 TLS 指纹     |
| em       | 支持 | 齐全                      | 东财 + 普通 requests       |
| tencent  | 支持 | 无成交额/振幅/换手率      | 按自然年分段抓取           |
| sina     | 支持 | 有成交额/换手率，无振幅   | 成交量原为股，折算为手；   |
|          |      |                           | turnover 为小数，已 ×100   |
| akshare  | 支持 | 齐全                      | 原库，东财裸请求           |

输出：<out>/<代码>_<复权>_<开始>_<结束>.csv，列与 etf_daily 完全一致：
date, open, close, ma5, ma20, ma60, bb_upper, bb_mid, bb_lower,
dist_to_lower, band_pos, high, low, volume, amount, amplitude,
pct_change, change, turnover（volume 统一为「手」，1 手 = 100 股）。

MySQL 入库（--to-mysql）
-----------------------
表 stock_daily（结构与 etf_daily 相同），主键 (symbol, trade_date, adjust)，幂等 upsert。
--calc-db 时指标由存储过程 sp_stock_indicators 在库内计算（口径与 sp_etf_indicators 一致），
Python 只入库原始行情（含 130 天预热行）。
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import akshare as ak
import pandas as pd

import etf_daily as etf
from etf_daily import (
    ADJUST_CHOICES,
    add_band_metrics,
    add_bollinger,
    add_moving_averages,
    add_price_change,
    dashed,
    describe,
    fetch_from_mysql,
    fetch_tencent,
    load_env_file,
    mysql_config,
    normalize_symbol,
    order_columns,
    parse_date,
    save_csv,
    top_up_tail,
    upsert_to_mysql,
    use_direct_connection,
    use_proxy,
    warn_if_stale,
)

PROC_NAME = "sp_stock_indicators"  # 存储过程定义：sql/sp_stock_indicators.sql
DEFAULT_SYMBOLS = ["600276", "300750"]  # 恒瑞医药、宁德时代
DEFAULT_START = date(2024, 1, 1)
SOURCES = ["auto", "em-cffi", "em", "tencent", "sina", "akshare"]
AUTO_ORDER = ["em-cffi", "em", "sina", "tencent"]

# 各源成交量原始单位 -> 手（A 股 1 手 = 100 股），与 etf_daily 的换算系数一致
STOCK_VOLUME_TO_LOT = {
    "em-cffi": 1.0,
    "em": 1.0,
    "akshare": 1.0,
    "tencent": 1.0,
    "sina": 0.01,  # 新浪为股
}


def _to_lots(df: pd.DataFrame, source: str) -> pd.DataFrame:
    factor = STOCK_VOLUME_TO_LOT.get(source, 1.0)
    if factor == 1.0 or "volume" not in df.columns:
        return df
    df = df.copy()
    df["volume"] = (df["volume"] * factor).round(0).astype("Int64")
    return df


def fetch_sina(symbol: str, start: str, end: str, adjust: str = "") -> pd.DataFrame:
    """新浪个股日线：自带 amount 与 turnover（小数，需 ×100）。"""
    df = ak.stock_zh_a_daily(symbol=etf.prefixed(symbol), adjust=adjust or "")
    if df is None or df.empty:
        raise ValueError(f"{symbol} 在新浪源没有数据")
    df = etf._standardize(df)
    if "turnover" in df.columns:
        df["turnover"] = df["turnover"] * 100  # 新浪给的是 0.0074 这种比例，转成 %
    lo, hi = pd.to_datetime(dashed(start)), pd.to_datetime(dashed(end))
    df = df.loc[(df["date"] >= lo) & (df["date"] <= hi)].reset_index(drop=True)
    if df.empty:
        raise ValueError(f"{symbol} 在 {start}-{end} 区间没有数据")
    return df


def fetch_akshare(symbol: str, start: str, end: str, adjust: str = "") -> pd.DataFrame:
    """akshare 走东财的个股日线，字段齐全。"""
    df = ak.stock_zh_a_hist(
        symbol=symbol, period="daily", start_date=start, end_date=end, adjust=adjust or ""
    )
    if df is None or df.empty:
        raise ValueError(f"{symbol} 在 {start}-{end} 区间没有数据")
    return etf._standardize(df)


FETCHERS = {
    "em-cffi": etf.fetch_em_cffi,  # em_market_id 已在 etf_daily 中泛化，个股直接可用
    "em": etf.fetch_em_http,
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
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt < retries:
                wait = backoff * attempt
                print(f"  ! [{source}] 第 {attempt} 次失败（{exc}），{wait:.0f}s 后重试",
                      file=sys.stderr)
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
    """抓取个股日线，返回 (DataFrame, 实际使用的数据源)。逻辑与 etf_daily.fetch_daily 一致。"""
    if source != "auto":
        df = fetch_with_retry(source, symbol, start, end, adjust, retries)
        return top_up_tail(df, symbol, end, adjust, source)

    errors: list[str] = []
    fallback: tuple[pd.DataFrame, str] | None = None
    lo = pd.to_datetime(dashed(start))
    for candidate in AUTO_ORDER:
        if adjust and candidate == "sina":
            print("  - auto 模式下跳过 sina（复权口径不一致），指定 --source sina 可用新浪复权",
                  file=sys.stderr)
            continue
        try:
            df = fetch_with_retry(candidate, symbol, start, end, adjust, retries=1)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{candidate}: {exc}")
            print(f"  - {candidate} 不可用（{exc}）", file=sys.stderr)
            continue
        if df["date"].iloc[0] <= lo:
            return top_up_tail(df, symbol, end, adjust, candidate)
        print(
            f"  - {candidate} 数据不完整（最早 {df['date'].iloc[0].date()}，"
            f"晚于请求起点 {dashed(start)}），尝试下一源",
            file=sys.stderr,
        )
        if fallback is None or df["date"].iloc[0] < fallback[0]["date"].iloc[0]:
            fallback = (df, candidate)
    if fallback:
        df, used = fallback
        print(
            f"  ! 所有可用源均不完整，采用 {used}（最早 {df['date'].iloc[0].date()}）",
            file=sys.stderr,
        )
        return top_up_tail(df, symbol, end, adjust, used)
    raise RuntimeError("所有数据源均失败：\n    " + "\n    ".join(errors))


def search_stock(keyword: str, limit: int = 20) -> int:
    """按名称/代码关键词查找 A 股代码。

    优先静态列表（交易所官网，需直连），失败时退回东财实时快照。
    """
    spot = None
    use_direct_connection()  # 静态列表走交易所官网，走代理常被断
    try:
        spot = ak.stock_info_a_code_name()
    except Exception as exc:  # noqa: BLE001
        print(f"  - 静态列表不可用（{exc}），改用东财实时快照", file=sys.stderr)
        try:
            spot = ak.stock_zh_a_spot_em()
        except Exception as exc2:  # noqa: BLE001
            print(f"查询 A 股列表失败：{exc2}", file=sys.stderr)
            return 1
    spot = spot.rename(columns={"code": "代码", "name": "名称"})
    if not {"代码", "名称"}.issubset(spot.columns):
        print(f"返回列不符合预期：{list(spot.columns)}", file=sys.stderr)
        return 1
    keyword = keyword.strip()
    mask = spot["代码"].astype(str).str.contains(keyword, na=False) | spot[
        "名称"
    ].astype(str).str.contains(keyword, na=False)
    hits = spot.loc[mask, ["代码", "名称"]].head(limit)
    if hits.empty:
        print(f"没有匹配「{keyword}」的 A 股")
        return 1
    for row in hits.itertuples(index=False):
        print(f"{row.代码}\t{row.名称}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="抓取 A 股个股日频行情（东财 / 腾讯 / 新浪，自动降级）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "symbols", nargs="*",
        help=f"6 位代码，省略时默认 {' + '.join(DEFAULT_SYMBOLS)}（可传多个）",
    )
    parser.add_argument("--start", help="开始日期，默认 2024-01-01（20240101 或 2024-01-01）")
    parser.add_argument("--end", help="结束日期，默认今天")
    parser.add_argument(
        "--adjust", choices=sorted(ADJUST_CHOICES), default="none",
        help="复权方式：none(默认) / qfq(前复权) / hfq(后复权)",
    )
    parser.add_argument(
        "--source", choices=SOURCES, default="auto",
        help="数据源，默认 auto（em-cffi -> em -> tencent -> sina 依次尝试）",
    )
    parser.add_argument("--out", default="data/stock", help="输出目录，默认 data/stock")
    parser.add_argument("--retries", type=int, default=3, help="指定源时的重试次数，默认 3")
    parser.add_argument("--sleep", type=float, default=1.0, help="批量抓取间隔秒数，默认 1.0")
    parser.add_argument("--direct", action="store_true", help="忽略系统代理，直连行情站点")
    parser.add_argument("--proxy", metavar="URL", help="指定代理，如 http://127.0.0.1:7897")
    parser.add_argument("--search", metavar="关键词", help="按名称查代码，如 --search 恒瑞")

    db = parser.add_argument_group("MySQL 入库（--to-mysql）")
    db.add_argument("--to-mysql", action="store_true", help="抓取后 upsert 进 MySQL，需 pymysql")
    db.add_argument(
        "--calc-db", action="store_true",
        help="指标（MA/布林带/派生列）由库内存储过程计算，Python 只入库原始行情",
    )
    db.add_argument("--mysql-host", help="默认 127.0.0.1，或环境变量 MYSQL_HOST")
    db.add_argument("--mysql-port", type=int, help="默认 3306，或 MYSQL_PORT")
    db.add_argument("--mysql-user", help="默认 root，或 MYSQL_USER")
    db.add_argument("--mysql-password", help="建议改用环境变量 MYSQL_PASSWORD，避免留痕")
    db.add_argument("--mysql-db", help="默认 libra_schema，或 MYSQL_DB")
    db.add_argument("--mysql-table", default="stock_daily", help="默认 stock_daily")
    return parser


def main(argv: list[str] | None = None) -> int:
    load_env_file()
    args = build_parser().parse_args(argv)

    if args.direct:
        use_direct_connection()
    elif args.proxy:
        use_proxy(args.proxy)

    if args.search:
        return search_stock(args.search)

    symbols = args.symbols or list(DEFAULT_SYMBOLS)

    start = parse_date(args.start, DEFAULT_START)
    end = parse_date(args.end, date.today())
    if start > end:
        print(f"开始日期 {start} 晚于结束日期 {end}", file=sys.stderr)
        return 2

    adjust = ADJUST_CHOICES[args.adjust]
    adjust_label = args.adjust if adjust else "none"
    out_dir = Path(args.out)
    failures: list[str] = []

    # 预热：向前多抓 130 个日历日，保证区间头部 MA60/布林带有值
    warm_start = (
        datetime.strptime(start, "%Y%m%d") - timedelta(days=etf.MA_WARMUP_DAYS)
    ).strftime("%Y%m%d")

    cfg: dict | None = None
    if args.calc_db and not args.to_mysql:
        print("--calc-db 需要与 --to-mysql 一起使用（指标在库里算）", file=sys.stderr)
        return 2
    if args.to_mysql:
        try:
            cfg = mysql_config(args)
        except Exception as exc:  # noqa: BLE001
            print(f"! MySQL 配置有误：{exc}", file=sys.stderr)
            return 2
        if args.calc_db:
            try:
                if etf.ensure_indicators_proc(cfg, proc_name=PROC_NAME):
                    print(f"已创建存储过程 {PROC_NAME}（库 {cfg['database']}）")
            except Exception as exc:  # noqa: BLE001
                print(f"! 存储过程准备失败：{exc}", file=sys.stderr)
                return 2

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
                symbol, warm_start, end, adjust, source=args.source, retries=args.retries
            )
        except Exception as exc:  # noqa: BLE001
            print(f"  ! {exc}", file=sys.stderr)
            failures.append(symbol)
            continue

        lo, hi = pd.to_datetime(dashed(start)), pd.to_datetime(dashed(end))
        db_calc = cfg is not None and args.calc_db

        if db_calc:
            try:
                written, _ = upsert_to_mysql(df, symbol, adjust, cfg)
                print(
                    f"  MySQL {cfg['database']}.{cfg['table']}: 写入 {written} 行"
                    f"（含 {etf.MA_WARMUP_DAYS} 天预热）"
                )
                receipt = etf.apply_indicators(
                    cfg, symbol, adjust or "none", fill_pct=True, proc_name=PROC_NAME
                )
                if receipt.get("rows_total"):
                    print(
                        f"  {PROC_NAME}: 重算 {receipt['rows_total']} 行，"
                        f"其中 {receipt['rows_ma60']} 行 MA60 有值"
                        f"（{receipt['first_date']} ~ {receipt['last_date']}）"
                    )
                df = fetch_from_mysql(cfg, symbol, adjust or "none", start, end)
            except Exception as exc:  # noqa: BLE001
                print(f"  ! MySQL 入库/库内计算失败：{exc}", file=sys.stderr)
                failures.append(f"{symbol}[mysql]")
                continue
        else:
            df = add_moving_averages(df)
            df = add_bollinger(df)
            df = add_band_metrics(df)
            df = add_price_change(df)
            df = df.loc[(df["date"] >= lo) & (df["date"] <= hi)].reset_index(drop=True)

        df = order_columns(df)
        if df.empty:
            print(f"  ! {symbol} 在 {start}-{end} 区间没有数据", file=sys.stderr)
            failures.append(symbol)
            continue

        path = out_dir / f"{symbol}_{adjust_label}_{start}_{end}.csv"
        save_csv(df, path)
        print(describe(symbol, df))
        print(f"  数据源 {used}  -> {path}")
        warn_if_stale(df, used)

        if cfg is not None and not db_calc:
            try:
                written, affected = upsert_to_mysql(df, symbol, adjust, cfg)
                print(
                    f"  MySQL {cfg['database']}.{cfg['table']}: 提交 {written} 行，"
                    f"影响 {affected} 行（MySQL 中更新一行计 2）"
                )
            except Exception as exc:  # noqa: BLE001
                print(f"  ! MySQL 写入失败：{exc}", file=sys.stderr)
                failures.append(f"{symbol}[mysql]")

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
