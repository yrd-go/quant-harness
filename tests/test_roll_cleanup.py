#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""测试: 滚动清理(rolling_cleanup) —— 幂等性与 cutoff 边界。

背景:
    数据库要保持"只有最近 N 年"。每次写入后删除 trade_date < cutoff 的行。
    这段逻辑最容易出错的地方是:
      1. cutoff 算错(比如用交易日数量而不是日历减 N 年)
      2. 不幂等(跑两次删的数据不一样)
      3. 误删窗口内的数据
    所以用真实数据库做"跑完再跑一次"的验证。
"""

import sqlite3
from datetime import date, datetime, timedelta

from _runner import test
import data_center as dc
from config import DB_FILE


def _real_conn() -> sqlite3.Connection:
    return dc.connect(str(DB_FILE))


@test("返回 (cutoff字符串, daily删除数, fund删除数) 三元组")
def t_return_shape():
    conn = _real_conn()
    try:
        r = dc.rolling_cleanup(conn, 5)
        assert isinstance(r, tuple) and len(r) == 3, f"期望三元组, 实际 {r!r}"
        cutoff, d1, d2 = r
        assert isinstance(cutoff, str), f"cutoff 应为 str, 实际 {type(cutoff).__name__}"
        assert isinstance(d1, int) and isinstance(d2, int), "删除数应为 int"
        # cutoff 应能被解析成日期
        datetime.strptime(cutoff, "%Y-%m-%d")
    finally:
        conn.close()


@test("cutoff = 当前北京时间 - N 年(日历减, 不是交易日数量)")
def t_cutoff_value():
    conn = _real_conn()
    try:
        cutoff, _, _ = dc.rolling_cleanup(conn, 5)
        now = dc.now_bj()
        expect = now.replace(year=now.year - 5).strftime("%Y-%m-%d")
        assert cutoff == expect, f"期望 cutoff={expect}, 实际 {cutoff}"
    finally:
        conn.close()


@test("history_years 参数生效: 3 年的 cutoff 晚于 5 年")
def t_years():
    conn = _real_conn()
    try:
        c3, _, _ = dc.rolling_cleanup(conn, 3)
        c5, _, _ = dc.rolling_cleanup(conn, 5)
        assert c3 > c5, f"3 年 cutoff({c3}) 应晚于 5 年({c5})"
    finally:
        conn.close()


@test("★ 幂等性: 连续清理两次, 第二次必须删除 0 行")
def t_idempotent():
    """这是最关键的一条。

    如果第二次还删掉数据, 说明 cutoff 不稳定或有其他 bug ——
    那会导致"每次跑增量都在删数据", 数据库永远填不满。
    """
    conn = _real_conn()
    try:
        dc.rolling_cleanup(conn, 5)                 # 第一次: 可能删掉窗口外的
        _, d1, d2 = dc.rolling_cleanup(conn, 5)     # 第二次: 必须什么都删不掉
        assert d1 == 0, f"第二次清理仍删了 {d1} 行 daily_price, 不幂等"
        assert d2 == 0, f"第二次清理仍删了 {d2} 行 fundamentals, 不幂等"
    finally:
        conn.close()


@test("★ 清理后库里不再有 cutoff 之前的数据")
def t_no_old_data():
    conn = _real_conn()
    try:
        cutoff, _, _ = dc.rolling_cleanup(conn, 5)
        for tbl in ("daily_price", "fundamentals"):
            n = conn.execute(
                f"SELECT COUNT(*) FROM {tbl} WHERE trade_date < ?",
                (cutoff,)).fetchone()[0]
            assert n == 0, f"{tbl} 里仍有 {n} 行早于 cutoff {cutoff} 的数据"
    finally:
        conn.close()


@test("★ 清理不会误删窗口内的数据")
def t_keeps_recent():
    """窗口内(>= cutoff)的数据必须完好。"""
    conn = _real_conn()
    try:
        cutoff, _, _ = dc.rolling_cleanup(conn, 5)
        n = conn.execute(
            "SELECT COUNT(*) FROM daily_price WHERE trade_date >= ?",
            (cutoff,)).fetchone()[0]
        assert n > 0, f"cutoff {cutoff} 之后一行数据都没有了 —— 可能误删了"
        # 5 年窗口大约 1200 个交易日/标的, 库里有多个标的
        assert n >= 1000, f"窗口内只剩 {n} 行, 明显偏少, 疑似误删"
    finally:
        conn.close()


@test("空表也能安全清理(不抛异常)")
def t_empty_db():
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute(dc.DDL_DAILY_PRICE)
        conn.execute(dc.DDL_FUNDAMENTALS)
        cutoff, d1, d2 = dc.rolling_cleanup(conn, 5)
        assert d1 == 0 and d2 == 0, "空表不该删掉任何行"
        datetime.strptime(cutoff, "%Y-%m-%d")
    finally:
        conn.close()


@test("表不存在时不抛异常(返回 0 删除数)")
def t_no_tables():
    conn = sqlite3.connect(":memory:")      # 什么表都没建
    try:
        cutoff, d1, d2 = dc.rolling_cleanup(conn, 5)
        assert d1 == 0 and d2 == 0
    finally:
        conn.close()
