#!/usr/bin/env python
"""ETF 日频行情抓取工具。多数据源自动降级：东财(curl_cffi) -> 东财(requests) -> 腾讯 -> 新浪。

用法示例
--------
    python etf_daily.py                                 # 默认抓 513520，2024-01-01 起
    python etf_daily.py --adjust qfq                     # 513520 前复权
    python etf_daily.py 510300 159915 --adjust qfq       # 指定标的
    python etf_daily.py --source sina                    # 指定源
    python etf_daily.py --search 沪深300                  # 按名称查代码
    python etf_daily.py --to-mysql                       # 抓完直接 upsert 进 MySQL
    python etf_daily.py --to-mysql --mysql-db libra_schema --mysql-user root

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
列：date, open, close, ma5, ma20, ma60, bb_upper, bb_mid, bb_lower,
　　dist_to_lower, band_pos, high, low, volume, amount, amplitude,
　　pct_change, change, turnover
　　volume 统一为「手」（1 手 = 100 份），各源差异在入库前已折算。
　　ma5/ma20/ma60 为 close 的移动平均；布林带 bb_mid = MA20，bb_upper/bb_lower
　　= 中线 ± 2 倍总体标准差（与 TA-Lib 口径一致）；dist_to_lower = close - 下轨，
　　band_pos = (close - 下轨) / 通道宽度，0=贴下轨 1=贴上轨。
　　为保证区间头部的指标值准确，实际抓取会向前多取 130 个日历日做预热，
　　输出时再裁剪回请求区间。
　　pct_change(涨跌幅 %)/change(涨跌额) 优先用源数据；源未提供（如新浪）
　　时按相邻交易日 close 补算，因此所有源的这两列都不为空。

MySQL 入库（--to-mysql）
-----------------------
依赖 pymysql。连接参数优先级：命令行 > 环境变量(MYSQL_HOST/PORT/USER/PASSWORD/DB) > 默认值。
密码建议走环境变量 MYSQL_PASSWORD，别写进命令行历史。
表不存在时自动 CREATE TABLE IF NOT EXISTS；已存在则不改结构。
写入为 INSERT ... ON DUPLICATE KEY UPDATE，主键 (symbol, trade_date, adjust)，重复抓取幂等。
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
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
# 输出列顺序（CSV 与入库对齐用）：均线紧跟 close，派生列放最后
OUTPUT_ORDER = [
    "date",
    "open",
    "close",
    "ma5",
    "ma20",
    "ma60",
    "bb_upper",
    "bb_mid",
    "bb_lower",
    "dist_to_lower",
    "band_pos",
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
AUTO_ORDER = ["em-cffi", "em", "sina", "tencent"]
TX_MAX_BARS = 640
DEFAULT_SYMBOL = "513520"  # 当前只关注日经 ETF，省略代码时用它
DEFAULT_START = date(2024, 1, 1)  # 默认抓取起点（--start 未指定时生效）

# 各源成交量原始单位 -> 手的换算系数（1 手 = 100 份）
VOLUME_TO_LOT = {
    "em-cffi": 1.0,  # 东财已是手
    "em": 1.0,
    "akshare": 1.0,  # akshare 走东财，已是手
    "tencent": 1.0,  # 腾讯已是手
    "sina": 0.01,  # 新浪为股，除以 100
}

# 移动平均线：列名 -> 窗口（交易日）
MA_WINDOWS = {"ma5": 5, "ma20": 20, "ma60": 60}
# 计算均线用的预热抓取量（日历日）：60 个交易日 ≈ 88 个日历日，留足节假日余量
MA_WARMUP_DAYS = 130

# 布林带（BOLL）：中线 = N 日均线，上/下线 = 中线 ± K 倍标准差（总体标准差，与 TA-Lib 一致）
BB_WINDOW = 20
BB_NUM_STD = 2.0
BB_COLS = ["bb_upper", "bb_mid", "bb_lower"]


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


def _decimals(series: pd.Series) -> list[Decimal]:
    """按 DECIMAL(10,3) 的语义归一化，保证 Python 与库内用的是同一批数值。"""
    quantum = Decimal("0.001")
    return [Decimal(str(float(v))).quantize(quantum, ROUND_HALF_UP) for v in series]


def _round_half_up(value: float, digits: int = 3) -> float:
    """十进制四舍五入，与 MySQL ROUND 一致。

    pandas 的 Series.round 用的是 half-even（1.5465 -> 1.546），
    MySQL 的 ROUND 是 half-up（1.5465 -> 1.547）；不统一就会稳定差 0.001。
    """
    if value is None or pd.isna(value):
        return float("nan")
    quantum = Decimal(1).scaleb(-digits)
    return float(Decimal(repr(float(value))).quantize(quantum, ROUND_HALF_UP))


def _rolling_mean_decimals(vals: list[Decimal], window: int) -> list[Decimal | None]:
    """十进制滚动均值，窗口不足返回 None（对齐 pandas min_periods=window）。"""
    out: list[Decimal | None] = [None] * len(vals)
    total = Decimal(0)
    for i, value in enumerate(vals):
        total += value
        if i >= window:
            total -= vals[i - window]
        if i >= window - 1:
            out[i] = total / window
    return out


def add_moving_averages(df: pd.DataFrame) -> pd.DataFrame:
    """基于 close 计算 MA5/MA20/MA60，窗口不足的位置为 NaN（入库转 NULL）。"""
    if df.empty or "close" not in df.columns:
        return df
    df = df.copy()
    vals = _decimals(df["close"])
    quantum = Decimal("0.001")
    for col, window in MA_WINDOWS.items():
        means = _rolling_mean_decimals(vals, window)
        df[col] = [
            float(m.quantize(quantum, ROUND_HALF_UP)) if m is not None else float("nan")
            for m in means
        ]
    return df


def add_bollinger(df: pd.DataFrame) -> pd.DataFrame:
    """计算布林带：中线 = BB_WINDOW 日均线，上/下线 = 中线 ± BB_NUM_STD 倍标准差。

    标准差用总体标准差（ddof=0），与 TA-Lib/主流行情软件及 MySQL STDDEV_POP 一致；
    窗口不足 20 日的位置为 NaN（入库转 NULL）。
    """
    if df.empty or "close" not in df.columns:
        return df
    df = df.copy()
    vals = _decimals(df["close"])
    closes = pd.Series([float(v) for v in vals], index=df.index)
    means = _rolling_mean_decimals(vals, BB_WINDOW)
    mid = pd.Series(
        [float(m) if m is not None else float("nan") for m in means], index=df.index
    )
    std = closes.rolling(BB_WINDOW, min_periods=BB_WINDOW).std(ddof=0)

    quantum = Decimal("0.001")
    df["bb_mid"] = [
        float(m.quantize(quantum, ROUND_HALF_UP)) if m is not None else float("nan")
        for m in means
    ]
    df["bb_upper"] = (mid + BB_NUM_STD * std).map(lambda v: _round_half_up(v, 3))
    df["bb_lower"] = (mid - BB_NUM_STD * std).map(lambda v: _round_half_up(v, 3))
    return df


def add_band_metrics(df: pd.DataFrame) -> pd.DataFrame:
    """布林带派生指标：

    dist_to_lower = close - bb_lower，收盘价距下轨的距离（元），负值表示跌破下轨；
    band_pos = (close - bb_lower) / (bb_upper - bb_lower)，通道相对位置，
               0 = 贴下轨，1 = 贴上轨，可小于 0 或大于 1（破位/超强）。
    通道宽度为 0（价格长期不变）时 band_pos 无定义，置 NaN（入库 NULL）。
    """
    if df.empty or "bb_lower" not in df.columns:
        return df
    df = df.copy()
    closes = pd.Series([float(v) for v in _decimals(df["close"])], index=df.index)
    df["dist_to_lower"] = (closes - df["bb_lower"]).map(lambda v: _round_half_up(v, 3))
    width = df["bb_upper"] - df["bb_lower"]
    df["band_pos"] = ((closes - df["bb_lower"]) / width.where(width != 0)).map(
        lambda v: _round_half_up(v, 4)
    )
    return df


def add_price_change(df: pd.DataFrame) -> pd.DataFrame:
    """计算涨跌幅(%)与涨跌额。源已提供的以其为准，缺的用 close 推导。

    新浪源不带 pct_change/change，这里按相邻交易日收盘价补算：
        pct_change = (close - 前一日 close) / 前一日 close * 100
        change     = close - 前一日 close
    第一行没有前值，留 NaN（入库为 NULL）。
    """
    if df.empty or "close" not in df.columns:
        return df
    df = df.copy()
    closes = pd.Series([float(v) for v in _decimals(df["close"])], index=df.index)
    derived_pct = (closes.pct_change() * 100).map(lambda v: _round_half_up(v, 3))
    derived_chg = closes.diff().map(lambda v: _round_half_up(v, 3))
    if "pct_change" not in df.columns:
        df["pct_change"] = derived_pct
    else:
        df["pct_change"] = pd.to_numeric(df["pct_change"], errors="coerce").fillna(derived_pct)
    if "change" not in df.columns:
        df["change"] = derived_chg
    else:
        df["change"] = pd.to_numeric(df["change"], errors="coerce").fillna(derived_chg)
    return df


def order_columns(df: pd.DataFrame) -> pd.DataFrame:
    """按 OUTPUT_ORDER 重排，保证 CSV 与入库列序稳定。"""
    return df[[c for c in OUTPUT_ORDER if c in df.columns]]


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


def _year_ranges(start: str, end: str) -> list[tuple[str, str]]:
    """把 [start, end] 按自然年切段，绕开腾讯单次 640 根上限。"""
    s = datetime.strptime(start, "%Y%m%d").date()
    e = datetime.strptime(end, "%Y%m%d").date()
    ranges: list[tuple[str, str]] = []
    cur = s
    while cur <= e:
        seg_end = min(date(cur.year, 12, 31), e)
        ranges.append((cur.strftime("%Y%m%d"), seg_end.strftime("%Y%m%d")))
        cur = date(cur.year + 1, 1, 1)
    return ranges


def _fetch_tencent_once(symbol: str, start: str, end: str, adjust: str) -> pd.DataFrame:
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
    return _standardize(df)


def fetch_tencent(symbol: str, start: str, end: str, adjust: str = "") -> pd.DataFrame:
    """腾讯行情，支持复权；缺点是无成交额/振幅/换手率。

    按自然年分段抓取再拼接，单段最多约 250 根，永远不会撞上 640 根上限。
    """
    frames: list[pd.DataFrame] = []
    for seg_start, seg_end in _year_ranges(start, end):
        try:
            frames.append(_fetch_tencent_once(symbol, seg_start, seg_end, adjust))
        except ValueError:
            continue  # 该段无数据（未上市、长期停牌），跳过
    if not frames:
        raise ValueError(f"{symbol} 在 {start}-{end} 区间没有数据")
    df = pd.concat(frames, ignore_index=True)
    return df.drop_duplicates("date").sort_values("date").reset_index(drop=True)


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


def top_up_tail(
    df: pd.DataFrame, symbol: str, end: str, adjust: str, used: str
) -> tuple[pd.DataFrame, str]:
    """用腾讯源补齐尾部缺口。

    新浪的 ETF 历史接口是 T+1 更新——当天收盘后仍拿不到当日 K 线，
    而腾讯当天收盘后就有。所以历史主体用新浪（字段全、无根数限制），
    最后缺的这几天用腾讯补上，避免永远缺当日数据。
    """
    if df.empty or used == "tencent":
        return df, used
    last = df["date"].max().date()
    end_date = datetime.strptime(dashed(end), "%Y-%m-%d").date()
    if last >= end_date:
        return df, used

    gap_start = (last + timedelta(days=1)).strftime("%Y%m%d")
    try:
        tail = fetch_tencent(symbol, gap_start, end, adjust=adjust)
    except Exception as exc:  # noqa: BLE001
        print(f"  - 尾部补取失败（{exc}），当前仍止于 {last}", file=sys.stderr)
        return df, used

    tail = tail.loc[tail["date"].dt.date > last]
    if tail.empty:
        return df, used

    merged = pd.concat([df, tail], ignore_index=True)
    merged = merged.sort_values("date").drop_duplicates("date").reset_index(drop=True)
    print(
        f"  + 尾部 {len(tail)} 行由 tencent 补齐（{tail['date'].min().date()} ~ "
        f"{tail['date'].max().date()}），{used} 自身最新只到 {last}",
        file=sys.stderr,
    )
    return merged, f"{used}+tencent"


def fetch_daily(
    symbol: str,
    start: str,
    end: str,
    adjust: str = "",
    source: str = "auto",
    retries: int = 3,
) -> tuple[pd.DataFrame, str]:
    """抓取日线，返回 (DataFrame, 实际使用的数据源)。

    auto 模式下校验数据完整性：若某源返回的第一根 K 线晚于请求起点，
    视为不完整，继续尝试下一源；源更新滞后导致尾部缺当日数据时，
    再用腾讯补齐尾部。
    """
    if source != "auto":
        df = fetch_with_retry(source, symbol, start, end, adjust, retries)
        return top_up_tail(df, symbol, end, adjust, source)

    errors: list[str] = []
    fallback: tuple[pd.DataFrame, str] | None = None
    lo = pd.to_datetime(dashed(start))
    for candidate in AUTO_ORDER:
        if adjust and candidate == "sina":
            print("  - sina 不支持复权，跳过", file=sys.stderr)
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
            f"  ! 所有可用源均不完整，采用 {used}（最早 {df['date'].iloc[0].date()}）；"
            f"建议换 --source 或缩短区间",
            file=sys.stderr,
        )
        return top_up_tail(df, symbol, end, adjust, used)
    raise RuntimeError("所有数据源均失败：\n    " + "\n    ".join(errors))


def warn_if_stale(df: pd.DataFrame, used: str) -> None:
    """收盘后仍缺当日 K 线时提醒，避免把滞后数据当成最新数据用。"""
    if df.empty:
        return
    last = df["date"].max().date()
    now = datetime.now()
    if now.weekday() < 5 and now.hour >= 16 and last < now.date():
        print(
            f"  ! 数据止于 {last}，未含今日 {now.date()}（源更新滞后）；"
            f"稍后重跑或用 --source tencent",
            file=sys.stderr,
        )


def save_csv(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False, encoding="utf-8-sig")


MYSQL_COLUMNS = [
    "symbol",
    "trade_date",
    "open",
    "close",
    "ma5",
    "ma20",
    "ma60",
    "bb_upper",
    "bb_mid",
    "bb_lower",
    "dist_to_lower",
    "band_pos",
    "high",
    "low",
    "volume",
    "amount",
    "amplitude",
    "pct_change",
    "price_change",
    "turnover",
    "adjust",
]
MYSQL_UPDATE = [c for c in MYSQL_COLUMNS if c not in {"symbol", "trade_date", "adjust"}]
MYSQL_DDL = """
CREATE TABLE IF NOT EXISTS `{table}` (
  symbol       VARCHAR(6)    NOT NULL COMMENT 'ETF 代码',
  trade_date   DATE          NOT NULL COMMENT '交易日',
  open         DECIMAL(10,3) NOT NULL,
  close        DECIMAL(10,3) NOT NULL,
  ma5          DECIMAL(10,3) NULL COMMENT '5日均线',
  ma20         DECIMAL(10,3) NULL COMMENT '20日均线',
  ma60         DECIMAL(10,3) NULL COMMENT '60日均线',
  bb_upper     DECIMAL(10,3) NULL COMMENT '布林上线（MA20+2σ）',
  bb_mid       DECIMAL(10,3) NULL COMMENT '布林中线（MA20）',
  bb_lower     DECIMAL(10,3) NULL COMMENT '布林下线（MA20-2σ）',
  dist_to_lower DECIMAL(10,3) NULL COMMENT '收盘价距布林下轨距离（元）',
  band_pos     DECIMAL(8,4)  NULL COMMENT '通道相对位置 0=下轨 1=上轨',
  high         DECIMAL(10,3) NOT NULL,
  low          DECIMAL(10,3) NOT NULL,
  volume       BIGINT        NOT NULL COMMENT '成交量（手）',
  amount       DECIMAL(20,2) NULL COMMENT '成交额（元）',
  amplitude    DECIMAL(8,3)  NULL COMMENT '振幅 %',
  pct_change   DECIMAL(8,3)  NULL COMMENT '涨跌幅 %',
  price_change DECIMAL(10,3) NULL COMMENT '涨跌额',
  turnover     DECIMAL(8,3)  NULL COMMENT '换手率 %',
  adjust       VARCHAR(4)    NOT NULL DEFAULT 'none' COMMENT 'none/qfq/hfq',
  created_at   TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at   TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (symbol, trade_date, adjust),
  KEY idx_trade_date (trade_date)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""

# 库内指标计算：存储过程定义放在 sql/sp_etf_indicators.sql
SQL_DIR = Path(__file__).resolve().parent / "sql"
PROC_NAME = "sp_etf_indicators"


def load_env_file(path: Path | None = None) -> None:
    """加载脚本同目录的 .env（KEY=VALUE，已 gitignore），不覆盖已有环境变量。

    这样密码可以只放在本地 .env 里，既不用交互输入，也不用写进命令行历史。
    """
    env_path = path or Path(__file__).resolve().with_name(".env")
    if not env_path.is_file():
        return
    for raw in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


def mysql_config(args: argparse.Namespace) -> dict:
    """命令行 > .env / 环境变量 > 交互输入 > 默认值。

    注意空字符串要当成「明确的空密码」，不能当成「没提供」，
    否则空密码的实例（本地开发常见）会一直卡在 getpass 提示上。
    """
    import getpass

    password = args.mysql_password
    if password is None:
        password = os.environ.get("MYSQL_PASSWORD")
    if password is None:
        password = getpass.getpass("MySQL 密码（直接回车表示空密码）: ")
    password = password or ""
    db = args.mysql_db or os.environ.get("MYSQL_DB") or "libra_schema"
    table = args.mysql_table or "etf_daily"
    if not re.fullmatch(r"[A-Za-z0-9_]+", table):
        raise ValueError(f"表名只能包含字母、数字、下划线：{table}")
    return {
        "host": args.mysql_host or os.environ.get("MYSQL_HOST") or "127.0.0.1",
        "port": int(args.mysql_port or os.environ.get("MYSQL_PORT") or 3306),
        "user": args.mysql_user or os.environ.get("MYSQL_USER") or "root",
        "password": password,
        "database": db,
        "table": table,
    }


def _cell(value: float, digits: int) -> float | None:
    return None if pd.isna(value) else round(float(value), digits)


def upsert_to_mysql(df: pd.DataFrame, symbol: str, adjust: str, cfg: dict) -> tuple[int, int]:
    """把 DataFrame upsert 进 MySQL，返回 (写入行数, 影响行数)。"""
    try:
        import pymysql
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("未安装 pymysql，请执行 pip install pymysql") from exc

    table = cfg["table"]
    placeholders = ", ".join(["%s"] * len(MYSQL_COLUMNS))
    updates = ", ".join(f"{c}=VALUES({c})" for c in MYSQL_UPDATE)
    sql = (
        f"INSERT INTO `{table}` ({', '.join(MYSQL_COLUMNS)}) VALUES ({placeholders}) "
        f"ON DUPLICATE KEY UPDATE {updates}"
    )

    # 新浪等源缺列，reindex 补成 NaN，保证属性访问一致
    frame = df.reindex(
        columns=[
            "date",
            "open",
            "close",
            "ma5",
            "ma20",
            "ma60",
            "bb_upper",
            "bb_mid",
            "bb_lower",
            "dist_to_lower",
            "band_pos",
            "high",
            "low",
            "volume",
            "amount",
            "amplitude",
            "pct_change",
            "change",
            "turnover",
        ]
    )

    rows = [
        (
            symbol,
            pd.to_datetime(r.date).date(),
            _cell(r.open, 3),
            _cell(r.close, 3),
            _cell(r.ma5, 3),
            _cell(r.ma20, 3),
            _cell(r.ma60, 3),
            _cell(r.bb_upper, 3),
            _cell(r.bb_mid, 3),
            _cell(r.bb_lower, 3),
            _cell(r.dist_to_lower, 3),
            _cell(r.band_pos, 4),
            _cell(r.high, 3),
            _cell(r.low, 3),
            0 if pd.isna(r.volume) else int(r.volume),
            _cell(r.amount, 2),
            _cell(r.amplitude, 3),
            _cell(r.pct_change, 3),
            _cell(r.change, 3),  # CSV 列 change -> MySQL 列 price_change
            _cell(r.turnover, 3),
            adjust or "none",
        )
        for r in frame.itertuples(index=False)
    ]

    conn = pymysql.connect(
        host=cfg["host"],
        port=cfg["port"],
        user=cfg["user"],
        password=cfg["password"],
        database=cfg["database"],
        charset="utf8mb4",
        autocommit=False,
    )
    try:
        with conn.cursor() as cur:
            cur.execute(MYSQL_DDL.format(table=table))
            affected = cur.executemany(sql, rows)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return len(rows), affected


def _connect(cfg: dict, autocommit: bool = False):
    """按 cfg 建立连接（只在需要时 import pymysql）。"""
    import pymysql

    return pymysql.connect(
        host=cfg["host"],
        port=cfg["port"],
        user=cfg["user"],
        password=cfg["password"],
        database=cfg["database"],
        charset="utf8mb4",
        autocommit=autocommit,
    )


def _procedure_body(sql_text: str) -> str:
    """去掉 SQL 文件里的 -- 注释行与结尾分号，交给服务端整条执行。

    pymysql 走的是 COM_QUERY 单语句协议，不需要客户端的 DELIMITER，
    但也不能把多条语句拼在一次 execute 里。
    """
    lines = [ln for ln in sql_text.splitlines() if not ln.lstrip().startswith("--")]
    return "\n".join(lines).strip().rstrip(";")


def ensure_indicators_proc(cfg: dict, force: bool = False) -> bool:
    """确保 sp_etf_indicators 存在；force=True 时先 DROP 再建。返回是否本次创建。"""
    conn = _connect(cfg, autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM information_schema.routines "
                "WHERE routine_schema = %s AND routine_name = %s "
                "AND routine_type = 'PROCEDURE'",
                (cfg["database"], PROC_NAME),
            )
            exists = cur.fetchone()[0] > 0
            if exists and not force:
                return False
            sql_path = SQL_DIR / f"{PROC_NAME}.sql"
            if not sql_path.is_file():
                raise RuntimeError(f"缺少存储过程定义文件：{sql_path}")
            cur.execute(f"DROP PROCEDURE IF EXISTS `{PROC_NAME}`")
            cur.execute(_procedure_body(sql_path.read_text(encoding="utf-8")))
    finally:
        conn.close()
    return True


def apply_indicators(
    cfg: dict,
    symbol: str | None = None,
    adjust: str | None = None,
    fill_pct: bool = True,
) -> dict:
    """调用存储过程在库内重算指标，返回过程回执。"""
    conn = _connect(cfg, autocommit=True)
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"CALL `{PROC_NAME}`(%s, %s, %s)",
                (symbol, adjust, 1 if fill_pct else 0),
            )
            row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        return {}
    keys = ("symbol", "adjust", "rows_total", "rows_ma60", "first_date", "last_date")
    return dict(zip(keys, row))


def fetch_from_mysql(
    cfg: dict, symbol: str, adjust: str, start: str, end: str
) -> pd.DataFrame:
    """从库里回读区间数据（含库内算好的指标列），列名与 CSV 保持一致。"""
    sql = (
        "SELECT trade_date AS date, open, close, ma5, ma20, ma60, "
        "bb_upper, bb_mid, bb_lower, dist_to_lower, band_pos, "
        "high, low, volume, amount, amplitude, pct_change, "
        "price_change AS `change`, turnover "
        f"FROM `{cfg['table']}` "
        "WHERE symbol = %s AND adjust = %s AND trade_date BETWEEN %s AND %s "
        "ORDER BY trade_date"
    )
    conn = _connect(cfg)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, (symbol, adjust or "none", dashed(start), dashed(end)))
            columns = [d[0] for d in cur.description]
            rows = cur.fetchall()
    finally:
        conn.close()

    df = pd.DataFrame(rows, columns=columns)
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"])
    for col in columns:
        if col != "date":
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df["volume"] = df["volume"].fillna(0).astype("Int64")
    return df


def describe(symbol: str, df: pd.DataFrame) -> str:
    first, last = df["date"].iloc[0].date(), df["date"].iloc[-1].date()
    window_return = (df["close"].iloc[-1] / df["close"].iloc[0] - 1) * 100
    volume = df["volume"].iloc[-1] if "volume" in df.columns else float("nan")
    ma_part = "  ".join(
        f"{col.upper()} {df[col].iloc[-1]:.3f}"
        for col in MA_WINDOWS
        if col in df.columns and not pd.isna(df[col].iloc[-1])
    )
    bb_part = ""
    if all(c in df.columns for c in BB_COLS) and not pd.isna(df["bb_upper"].iloc[-1]):
        bb_part = (f"  BOLL {df['bb_lower'].iloc[-1]:.3f} / "
                   f"{df['bb_mid'].iloc[-1]:.3f} / {df['bb_upper'].iloc[-1]:.3f}")
    pos_part = ""
    if "band_pos" in df.columns and not pd.isna(df["band_pos"].iloc[-1]):
        pos_part = f"  通道位置 {df['band_pos'].iloc[-1]:.2f}"
    pct = df["pct_change"].iloc[-1] if "pct_change" in df.columns else float("nan")
    pct_part = "" if pd.isna(pct) else f"  当日涨跌 {pct:+.2f}%"
    return (
        f"  {symbol}: {len(df)} 行  {first} ~ {last}  "
        f"最新收盘 {df['close'].iloc[-1]:.3f}{pct_part}  "
        f"成交量 {volume:,.0f} 手  "
        f"区间涨跌 {window_return:+.2f}%"
        + (f"  {ma_part}" if ma_part else "")
        + bb_part
        + pos_part
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
    parser.add_argument("--start", help="开始日期，默认 2024-01-01（20240101 或 2024-01-01）")
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

    db = parser.add_argument_group("MySQL 入库（--to-mysql）")
    db.add_argument("--to-mysql", action="store_true", help="抓取后 upsert 进 MySQL，需 pymysql")
    db.add_argument(
        "--calc-db",
        action="store_true",
        help="指标（MA/布林带/派生列）改由库内存储过程计算，Python 只入库原始行情",
    )
    db.add_argument("--mysql-host", help="默认 127.0.0.1，或环境变量 MYSQL_HOST")
    db.add_argument("--mysql-port", type=int, help="默认 3306，或 MYSQL_PORT")
    db.add_argument("--mysql-user", help="默认 root，或 MYSQL_USER")
    db.add_argument("--mysql-password", help="建议改用环境变量 MYSQL_PASSWORD，避免留痕")
    db.add_argument("--mysql-db", help="默认 libra_schema，或 MYSQL_DB")
    db.add_argument("--mysql-table", help="默认 etf_daily")
    return parser


def main(argv: list[str] | None = None) -> int:
    load_env_file()
    args = build_parser().parse_args(argv)

    if args.direct:
        use_direct_connection()
    elif args.proxy:
        use_proxy(args.proxy)

    if args.search:
        return search_etf(args.search)

    symbols: list[str] = args.symbols or [DEFAULT_SYMBOL]

    today = date.today()
    start = parse_date(args.start, DEFAULT_START)
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

    # 均线预热：向前多抓 130 个日历日，保证请求区间头部的 MA20/MA60 也有值
    warm_start = (
        datetime.strptime(start, "%Y%m%d") - timedelta(days=MA_WARMUP_DAYS)
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
                if ensure_indicators_proc(cfg):
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
            # 指标交给存储过程。预热行必须一起入库，否则区间头部的 MA60/布林带无法成立。
            try:
                written, affected = upsert_to_mysql(df, symbol, adjust, cfg)
                print(
                    f"  MySQL {cfg['database']}.{cfg['table']}: 写入 {written} 行"
                    f"（含 {MA_WARMUP_DAYS} 天预热）"
                )
                receipt = apply_indicators(cfg, symbol, adjust or "none", fill_pct=True)
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
            # 本地计算：用预热数据算指标，再裁剪回用户请求的区间
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
