#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""检查并更新 libra_schema.etf_daily 的表结构与数据情况。

为什么需要这个脚本
------------------
在 Workbench / Navicat 里把多条 SQL 一起粘贴执行，客户端只会按分号切分语句；
少一个分号，`SHOW CREATE TABLE` 就会吞掉后面的 `SELECT`，报 1064 语法错误。
这个脚本用 pymysql 逐条执行，不依赖客户端的语句切分规则。

用法
----
    python db_check.py                       # 读环境变量 / 交互输入密码
    python db_check.py --migrate             # 检查并补齐缺失列（幂等）
    python db_check.py --mysql-password xxx
    python db_check.py --mysql-db libra_schema --mysql-table etf_daily --symbol 513520

连接参数优先级：命令行 > 环境变量(MYSQL_HOST/PORT/USER/PASSWORD/DB) > 默认值。
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys

try:
    import pymysql
except ImportError:  # pragma: no cover
    print("缺少 pymysql，请先执行：python -m pip install pymysql", file=sys.stderr)
    raise SystemExit(1)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="检查 MySQL 表结构与数据（绕过 SQL 客户端的语句切分问题）",
    )
    parser.add_argument("--mysql-host", help="默认 127.0.0.1，或 MYSQL_HOST")
    parser.add_argument("--mysql-port", type=int, help="默认 3306，或 MYSQL_PORT")
    parser.add_argument("--mysql-user", help="默认 root，或 MYSQL_USER")
    parser.add_argument("--mysql-password", help="建议改用环境变量 MYSQL_PASSWORD")
    parser.add_argument("--mysql-db", help="默认 libra_schema，或 MYSQL_DB")
    parser.add_argument("--mysql-table", default="etf_daily", help="默认 etf_daily")
    parser.add_argument("--symbol", default="513520", help="统计用的 ETF 代码，默认 513520")
    parser.add_argument("--migrate", action="store_true",
                        help="对比预期结构，补齐缺失列（幂等，已存在则跳过）")
    return parser


def connect(args: argparse.Namespace, password: str):
    return pymysql.connect(
        host=args.mysql_host or os.environ.get("MYSQL_HOST") or "127.0.0.1",
        port=int(args.mysql_port or os.environ.get("MYSQL_PORT") or 3306),
        user=args.mysql_user or os.environ.get("MYSQL_USER") or "root",
        password=password,
        charset="utf8mb4",
        connect_timeout=8,
        read_timeout=15,
    )


def q(conn, sql: str, params: tuple = ()):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def run(conn, sql: str) -> None:
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()


# 预期结构（与 etf_daily.py 的 MYSQL_DDL 对齐）：缺失列 -> 补齐 DDL
MIGRATIONS = [
    (
        "updated_at",
        "ALTER TABLE `{db}`.`{table}` ADD COLUMN updated_at TIMESTAMP NOT NULL "
        "DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP AFTER created_at",
    ),
    (
        "ma5",
        "ALTER TABLE `{db}`.`{table}` ADD COLUMN ma5 DECIMAL(10,3) NULL "
        "COMMENT '5日均线' AFTER `close`",
    ),
    (
        "ma20",
        "ALTER TABLE `{db}`.`{table}` ADD COLUMN ma20 DECIMAL(10,3) NULL "
        "COMMENT '20日均线' AFTER `ma5`",
    ),
    (
        "ma60",
        "ALTER TABLE `{db}`.`{table}` ADD COLUMN ma60 DECIMAL(10,3) NULL "
        "COMMENT '60日均线' AFTER `ma20`",
    ),
]


def migrate(conn, db: str, table: str) -> list[str]:
    """按 MIGRATIONS 逐项检查，缺失才执行，返回本次补齐的列名。"""
    existing = {
        r[0]
        for r in q(conn, "SELECT column_name FROM information_schema.columns "
                         "WHERE table_schema = %s AND table_name = %s", (db, table))
    }
    applied = []
    for col, ddl in MIGRATIONS:
        if col in existing:
            print(f"  = {col} 已存在，跳过")
            continue
        run(conn, ddl.format(db=db, table=table))
        applied.append(col)
        print(f"  + 已添加列 {col}")
    return applied


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    db = args.mysql_db or os.environ.get("MYSQL_DB") or "libra_schema"
    table = args.mysql_table

    password = (
        args.mysql_password
        or os.environ.get("MYSQL_PASSWORD")
        or getpass.getpass(f"MySQL 密码（用户 {args.mysql_user or 'root'}）：")
    )

    try:
        conn = connect(args, password)
    except pymysql.err.OperationalError as exc:
        code = exc.args[0] if exc.args else "?"
        msg = str(exc.args[-1]) if exc.args else str(exc)
        print(f"[连接失败] {code}: {msg}")
        if code == 1045:
            print("  → 账号或密码不对。确认用户名，或改用 --mysql-user 指定其他账号。")
        elif code == 2003:
            print("  → 连不上端口，检查 MySQL 服务是否在运行、端口是否为 3306。")
        return 1

    print(f"[连接成功] {args.mysql_host or '127.0.0.1'}:{args.mysql_port or 3306} "
          f"MySQL {q(conn, 'SELECT VERSION()')[0][0]}")

    # 1) 库是否存在
    rows = q(conn, "SELECT schema_name FROM information_schema.schemata "
                   "WHERE schema_name = %s", (db,))
    if not rows:
        dbs = [r[0] for r in q(conn, "SELECT schema_name FROM information_schema.schemata")]
        print(f"[库不存在] {db}  → 现有库：{', '.join(dbs)}")
        conn.close()
        return 1
    print(f"[库] {db} 存在")

    # 2) 表是否存在
    rows = q(conn, "SELECT table_name, engine, table_rows, table_collation "
                   "FROM information_schema.tables "
                   "WHERE table_schema = %s AND table_name = %s", (db, table))
    if not rows:
        print(f"[表不存在] {db}.{table}")
        print("  → 还没建表。先跑一次：python etf_daily.py --to-mysql")
        print(f"  → 或者手工建：CREATE TABLE {db}.{table} (...) 见 etf_daily.py 里的 MYSQL_DDL")
        conn.close()
        return 1
    _, engine, est_rows, collation = rows[0]
    print(f"[表] {db}.{table}  引擎 {engine}  估算行数 {est_rows}  排序规则 {collation}")

    # 3) 结构更新（--migrate 时执行）
    if args.migrate:
        print("\n--- 结构更新（--migrate）---")
        applied = migrate(conn, db, table)
        if applied:
            print(f"本次共补齐 {len(applied)} 列：{', '.join(applied)}")
        else:
            print("结构与预期一致，无需变更。")

    # 4) 建表语句
    create_sql = q(conn, f"SHOW CREATE TABLE `{db}`.`{table}`")[0][1]
    print("\n--- SHOW CREATE TABLE ---")
    print(create_sql)

    # 4) 唯一键检查（幂等的前提）
    idx = q(conn, "SELECT index_name, GROUP_CONCAT(column_name ORDER BY seq_in_index) AS cols, "
                  "MIN(non_unique) AS non_unique "
                  "FROM information_schema.statistics "
                  "WHERE table_schema = %s AND table_name = %s "
                  "GROUP BY index_name", (db, table))
    print("\n--- 索引 ---")
    has_uk = False
    for name, cols, non_unique in idx:
        kind = "UNIQUE" if non_unique == 0 else "INDEX "
        print(f"  {kind} {name}: {cols}")
        if non_unique == 0 and {c.strip() for c in cols.split(",")} >= {"symbol", "trade_date", "adjust"}:
            has_uk = True
    if not has_uk:
        print("  ! 缺少 (symbol, trade_date, adjust) 唯一键，重复抓取会产生重复行。")
        print("    修复：ALTER TABLE `%s`.`%s` ADD UNIQUE KEY uk_symbol_date_adjust (symbol, trade_date, adjust);" % (db, table))

    # 5) 缺列提醒（未加 --migrate 时）
    if not args.migrate:
        existing = {
            r[0]
            for r in q(conn, "SELECT column_name FROM information_schema.columns "
                             "WHERE table_schema = %s AND table_name = %s", (db, table))
        }
        missing = [col for col, _ in MIGRATIONS if col not in existing]
        if missing:
            print(f"\n  ! 表缺少预期的列：{', '.join(missing)}")
            print("    补齐：python db_check.py --migrate")

    # 6) 数据概览
    print("\n--- 数据概览 ---")
    total = q(conn, f"SELECT COUNT(*) FROM `{db}`.`{table}`")[0][0]
    print(f"  总行数：{total}")
    if total:
        rows = q(conn, f"SELECT adjust, COUNT(*) AS cnt, MIN(trade_date) AS min_d, "
                       f"MAX(trade_date) AS max_d, MIN(close) AS min_c, MAX(close) AS max_c "
                       f"FROM `{db}`.`{table}` GROUP BY adjust ORDER BY adjust")
        for adjust, cnt, min_d, max_d, min_c, max_c in rows:
            print(f"  adjust={adjust:<5} {cnt:>5} 行  {min_d} ~ {max_d}  收盘 {min_c} ~ {max_c}")

    rows = q(conn, f"SELECT COUNT(*) AS cnt, MIN(trade_date) AS min_d, MAX(trade_date) AS max_d "
                   f"FROM `{db}`.`{table}` WHERE symbol = %s", (args.symbol,))
    cnt, min_d, max_d = rows[0][0], rows[0][1], rows[0][2]
    print(f"  symbol={args.symbol}：{cnt} 行  {min_d} ~ {max_d}")
    if cnt == 0:
        print("  ! 这个代码没有数据。若抓取时用的是别的代码，用 --symbol 指定。")

    # 7) 最近 3 行
    if cnt:
        rows = q(conn, f"SELECT trade_date, open, close, high, low, volume, pct_change "
                       f"FROM `{db}`.`{table}` WHERE symbol = %s "
                       f"ORDER BY trade_date DESC LIMIT 3", (args.symbol,))
        print("\n--- 最近 3 个交易日 ---")
        print(f"  {'日期':<12}{'开':>8}{'收':>8}{'高':>8}{'低':>8}{'量(手)':>12}{'涨跌%':>9}")
        for d, o, c, h, l, v, p in rows:
            print(f"  {str(d):<12}{o:>8}{c:>8}{h:>8}{l:>8}{(v or 0):>12}{('' if p is None else p):>9}")

    conn.close()
    print("\n检查完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
