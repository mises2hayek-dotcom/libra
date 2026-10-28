#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""管理 ETF 指标的库内计算：创建 / 应用 / 校验存储过程 sp_etf_indicators。

为什么要这个脚本
----------------
指标从 Python 搬进 MySQL 后，必须能证明「库内算的和原来 pandas 算的一模一样」。
`--verify` 会把库里存的 8 个指标列，与用同一份行情在 pandas 里重算的结果逐列比对，
输出不一致行数和最大绝对偏差。差值为 0 才算迁移成功。

用法
----
    python db_indicators.py                    # 建过程 + 重算 + 校验（默认三连）
    python db_indicators.py --create           # 只建/更新存储过程（已存在则跳过）
    python db_indicators.py --create --force   # 先 DROP 再重建（改了口径后用）
    python db_indicators.py --apply            # 只让库里重算（可指定标的）
    python db_indicators.py --verify           # 只校验
    python db_indicators.py --drop             # 删除存储过程
    python db_indicators.py --apply --symbol 513520 --adjust none

连接参数优先级：命令行 > .env > 环境变量 > 默认值。
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

try:
    import pandas as pd
    import pymysql
except ImportError as exc:  # pragma: no cover
    print(f"缺少依赖：{exc}，请先执行 python -m pip install pymysql pandas", file=sys.stderr)
    raise SystemExit(1)

SQL_DIR = Path(__file__).resolve().parent / "sql"
PROC_NAME = "sp_etf_indicators"

# 校验列：(列名, 允许偏差) —— 3 位小数取整用 0.001，band_pos 4 位用 0.0002
CHECK_COLUMNS = [
    ("ma5", 0.001),
    ("ma20", 0.001),
    ("ma60", 0.001),
    ("bb_upper", 0.001),
    ("bb_mid", 0.001),
    ("bb_lower", 0.001),
    ("dist_to_lower", 0.001),
    ("band_pos", 0.0002),
]


def load_env_file() -> None:
    """加载脚本同目录的 .env（KEY=VALUE，已 gitignore），不覆盖已有环境变量。"""
    env_path = Path(__file__).resolve().with_name(".env")
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="创建/应用/校验库内指标计算（sp_etf_indicators）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    action = parser.add_argument_group("动作（省略时依次执行 建过程 → 重算 → 校验）")
    action.add_argument("--create", action="store_true", help="创建存储过程（已存在则跳过）")
    action.add_argument("--force", action="store_true", help="配合 --create，先 DROP 再建")
    action.add_argument("--apply", action="store_true", help="调用存储过程重算指标")
    action.add_argument("--verify", action="store_true", help="库内结果 vs pandas 重算，逐列比对")
    action.add_argument("--drop", action="store_true", help="删除存储过程")

    target = parser.add_argument_group("目标")
    target.add_argument("--symbol", help="ETF 代码；省略表示全部标的")
    target.add_argument("--adjust", help="复权类型 none/qfq/hfq；省略表示全部")
    target.add_argument("--no-fill-pct", action="store_true",
                        help="不补齐缺失的涨跌额/涨跌幅")

    db = parser.add_argument_group("MySQL")
    db.add_argument("--mysql-host", help="默认 127.0.0.1，或 MYSQL_HOST")
    db.add_argument("--mysql-port", type=int, help="默认 3306，或 MYSQL_PORT")
    db.add_argument("--mysql-user", help="默认 root，或 MYSQL_USER")
    db.add_argument("--mysql-password", help="建议改用 .env 里的 MYSQL_PASSWORD")
    db.add_argument("--mysql-db", help="默认 libra_schema，或 MYSQL_DB")
    db.add_argument("--mysql-table", default="etf_daily", help="默认 etf_daily")
    return parser


def mysql_config(args: argparse.Namespace) -> dict:
    password = args.mysql_password
    if password is None:
        password = os.environ.get("MYSQL_PASSWORD")
    if password is None:
        password = getpass.getpass(f"MySQL 密码（用户 {args.mysql_user or 'root'}）：")
    password = password or ""
    return {
        "host": args.mysql_host or os.environ.get("MYSQL_HOST") or "127.0.0.1",
        "port": int(args.mysql_port or os.environ.get("MYSQL_PORT") or 3306),
        "user": args.mysql_user or os.environ.get("MYSQL_USER") or "root",
        "password": password,
        "database": args.mysql_db or os.environ.get("MYSQL_DB") or "libra_schema",
        "table": args.mysql_table,
    }


def connect(cfg: dict, autocommit: bool = True):
    return pymysql.connect(
        host=cfg["host"],
        port=cfg["port"],
        user=cfg["user"],
        password=cfg["password"],
        database=cfg["database"],
        charset="utf8mb4",
        autocommit=autocommit,
        connect_timeout=8,
    )


def procedure_body(sql_text: str) -> str:
    """去掉 -- 注释行与结尾分号：pymysql 一次 execute 只能带一条语句。"""
    lines = [ln for ln in sql_text.splitlines() if not ln.lstrip().startswith("--")]
    return "\n".join(lines).strip().rstrip(";")


def proc_exists(conn, database: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM information_schema.routines "
            "WHERE routine_schema = %s AND routine_name = %s AND routine_type = 'PROCEDURE'",
            (database, PROC_NAME),
        )
        return cur.fetchone()[0] > 0


def cmd_create(conn, cfg: dict, force: bool) -> int:
    if proc_exists(conn, cfg["database"]) and not force:
        print(f"[跳过] {cfg['database']}.{PROC_NAME} 已存在；要重建请加 --force")
        return 0
    sql_path = SQL_DIR / f"{PROC_NAME}.sql"
    if not sql_path.is_file():
        print(f"[失败] 找不到定义文件 {sql_path}", file=sys.stderr)
        return 1
    with conn.cursor() as cur:
        cur.execute(f"DROP PROCEDURE IF EXISTS `{PROC_NAME}`")
        cur.execute(procedure_body(sql_path.read_text(encoding="utf-8")))
    print(f"[建过程] {cfg['database']}.{PROC_NAME} 创建成功")
    return 0


def cmd_drop(conn, cfg: dict) -> int:
    with conn.cursor() as cur:
        cur.execute(f"DROP PROCEDURE IF EXISTS `{PROC_NAME}`")
    print(f"[删除] {cfg['database']}.{PROC_NAME}")
    return 0


def cmd_apply(conn, cfg: dict, args: argparse.Namespace) -> int:
    if not proc_exists(conn, cfg["database"]):
        print("[失败] 存储过程不存在，先跑 --create", file=sys.stderr)
        return 1
    symbol = args.symbol
    adjust = args.adjust
    print(f"[重算] symbol={symbol or '全部'}  adjust={adjust or '全部'}  "
          f"fill_pct={0 if args.no_fill_pct else 1}")
    with conn.cursor() as cur:
        cur.execute(
            f"CALL `{PROC_NAME}`(%s, %s, %s)",
            (symbol, adjust, 0 if args.no_fill_pct else 1),
        )
        row = cur.fetchone()
    if row:
        print(f"  回执：{row[2]} 行参与计算，其中 {row[3]} 行 MA60 有值"
              f"（{row[4]} ~ {row[5]}）")
    return 0


def read_frame(conn, cfg: dict, symbol: str, adjust: str) -> pd.DataFrame:
    sql = (
        "SELECT trade_date AS date, open, high, low, close, volume, pct_change, "
        "price_change, ma5, ma20, ma60, bb_upper, bb_mid, bb_lower, "
        "dist_to_lower, band_pos "
        f"FROM `{cfg['table']}` WHERE symbol = %s AND adjust = %s ORDER BY trade_date"
    )
    with conn.cursor() as cur:
        cur.execute(sql, (symbol, adjust))
        columns = [d[0] for d in cur.description]
        rows = cur.fetchall()
    df = pd.DataFrame(rows, columns=columns)
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"])
    for col in columns:
        if col != "date":
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def reference_frame(df: pd.DataFrame) -> pd.DataFrame:
    """用 etf_daily.py 里同一套函数在 pandas 侧重算，作为比对基准。"""
    import etf_daily as etf

    out = etf.add_moving_averages(df.copy())
    out = etf.add_bollinger(out)
    out = etf.add_band_metrics(out)
    return out


def cmd_verify(conn, cfg: dict, args: argparse.Namespace) -> int:
    targets: list[tuple[str, str]]
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT DISTINCT symbol, adjust FROM `{cfg['table']}` "
            "WHERE (%s IS NULL OR symbol = %s) AND (%s IS NULL OR adjust = %s) "
            "ORDER BY symbol, adjust",
            (args.symbol, args.symbol, args.adjust, args.adjust),
        )
        targets = [(r[0], r[1]) for r in cur.fetchall()]
    if not targets:
        print("[校验] 没有可校验的数据", file=sys.stderr)
        return 1

    total_bad = 0
    for symbol, adjust in targets:
        df = read_frame(conn, cfg, symbol, adjust)
        if df.empty:
            continue
        ref = reference_frame(df)
        print(f"\n[校验] {symbol} / {adjust}  {len(df)} 行  "
              f"{df['date'].iloc[0].date()} ~ {df['date'].iloc[-1].date()}")

        bad_any = 0
        print(f"  {'列名':<15}{'库内非空':>9}{'pandas非空':>11}"
              f"{'不一致':>8}{'最大绝对差':>12}  判定")
        for col, tol in CHECK_COLUMNS:
            if col not in ref.columns:
                continue
            db_series = df[col]
            py_series = pd.to_numeric(ref[col], errors="coerce")
            both = db_series.notna() & py_series.notna()
            only_one = (db_series.notna() ^ py_series.notna()).sum()
            diff = (db_series[both] - py_series[both]).abs()
            max_diff = float(diff.max()) if not diff.empty else 0.0
            mismatch = int((diff > tol).sum()) + int(only_one)
            verdict = "OK" if mismatch == 0 else f"差异 {mismatch} 行"
            if mismatch:
                bad_any += mismatch
                print(f"  {col:<15}{int(db_series.notna().sum()):>9}"
                      f"{int(py_series.notna().sum()):>11}{mismatch:>8}"
                      f"{max_diff:>12.6f}  {verdict}")
            else:
                print(f"  {col:<15}{int(db_series.notna().sum()):>9}"
                      f"{int(py_series.notna().sum()):>11}{0:>8}"
                      f"{max_diff:>12.6f}  {verdict}")

        total_bad += bad_any
        if bad_any == 0:
            print("  → 全部列一致")
        else:
            first = ref.loc[
                (df["ma60"].notna() != ref["ma60"].notna())
                | ((df["ma60"] - ref["ma60"]).abs() > 0.001)
            ]
            if not first.empty:
                print(f"  → 首个偏差出现在 {first['date'].iloc[0].date()}")

    print("\n" + ("校验通过：库内结果与 pandas 完全一致。" if total_bad == 0
                  else f"校验未通过：共 {total_bad} 处不一致，先看首部窗口是否缺预热行。"))
    return 0 if total_bad == 0 else 1


def main(argv: list[str] | None = None) -> int:
    load_env_file()
    args = build_parser().parse_args(argv)
    cfg = mysql_config(args)

    try:
        conn = connect(cfg)
    except pymysql.err.OperationalError as exc:
        code = exc.args[0] if exc.args else "?"
        msg = str(exc.args[-1]) if exc.args else str(exc)
        print(f"[连接失败] {code}: {msg}", file=sys.stderr)
        if code == 1045:
            print("  → 账号或密码不对；把密码写进 .env 的 MYSQL_PASSWORD 更省事。",
                  file=sys.stderr)
        return 1

    try:
        print(f"[连接成功] {cfg['host']}:{cfg['port']}  库 {cfg['database']}  "
              f"表 {cfg['table']}")

        if args.drop:
            return cmd_drop(conn, cfg)

        actions = {
            "create": args.create,
            "apply": args.apply,
            "verify": args.verify,
        }
        if not any(actions.values()):  # 无参数 = 建过程 → 重算 → 校验
            actions = dict.fromkeys(actions, True)

        if actions["create"]:
            rc = cmd_create(conn, cfg, args.force)
            if rc:
                return rc
        if actions["apply"]:
            rc = cmd_apply(conn, cfg, args)
            if rc:
                return rc
        if actions["verify"]:
            return cmd_verify(conn, cfg, args)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
