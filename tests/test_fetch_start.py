#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""测试: 逐标的基准(compute_fetch_start) —— 修过的第二个重要 bug。

背景:
    原增量更新用【一个全局】SELECT MAX(trade_date) 当所有标的的起算点。
    后果: 新加入的标的在库里查不到, 会错误沿用【别的标的】的最后日期,
    于是只拉 4 天数据, 而不是它需要的 5 年历史。

    这段逻辑现在被固化成测试: 有记录 -> 真增量; 没记录 -> 回补 N 年。
"""

from datetime import date, timedelta

from _runner import test
import data_center as dc
import sqlite3


def _empty_conn() -> sqlite3.Connection:
    """一个内存库, 建好表结构但没有数据。用于测"新标的"分支。"""
    conn = sqlite3.connect(":memory:")
    conn.execute(dc.DDL_DAILY_PRICE)
    conn.execute(dc.DDL_FUNDAMENTALS)
    return conn


@test("老标的: 从'它自己的最后交易日 + 1 天'开始(真增量)")
def t_existing():
    conn = _empty_conn()
    last = date(2026, 9, 30)
    start, is_new = dc.compute_fetch_start(
        conn, "daily_price", "600519", 5, date(2026, 10, 10),
        existing_last=last, symbol_label="600519")
    assert start == date(2026, 10, 1), f"期望 2026-10-01, 实际 {start}"
    assert is_new is False, "老标的不该被判定为新增"


@test("老标的: 起算点必须【严格晚于】它自己的最后交易日(否则会重复拉)")
def t_strictly_after():
    conn = _empty_conn()
    last = date(2026, 9, 30)
    start, _ = dc.compute_fetch_start(
        conn, "daily_price", "600519", 5, date(2026, 10, 10),
        existing_last=last)
    assert start > last, f"起算点 {start} 未晚于最后交易日 {last}"


@test("新标的: 库里没有 -> 回补最近 N 年(不是从全局最后日期续)")
def t_new_symbol():
    conn = _empty_conn()
    today = date(2026, 10, 10)
    start, is_new = dc.compute_fetch_start(
        conn, "daily_price", "999999", 5, today,
        existing_last=None, symbol_label="TEST")
    assert is_new is True, "库里没有的标的必须判定为新增"
    # 应回溯约 5 年(允许 ±3 天: 闰年/月份长度差异)
    expected = date(2021, 10, 10)
    delta = abs((start - expected).days)
    assert delta <= 3, f"新标的起算点 {start} 偏离预期的 {expected} 达 {delta} 天"
    assert start < today, "新标的起算点必须早于今天"


@test("★ 关键: 新标的的起算点远早于老标的, 不会只拉到几天数据")
def t_new_vs_existing():
    """这是那个静默 bug 的核心断言。

    如果实现退回成"全局基准", 新标的的起算点会变成 2026-10-01(只拉几天),
    这个断言就会失败 —— 从而把 bug 拦在合入之前。
    """
    conn = _empty_conn()
    today = date(2026, 10, 10)
    global_last = date(2026, 9, 30)      # 别的标的的最后日期

    new_start, is_new = dc.compute_fetch_start(
        conn, "daily_price", "999999", 5, today, existing_last=None)

    assert is_new is True
    assert new_start != global_last + timedelta(days=1), \
        "新标的错误地沿用了全局最后日期 -> 只拉 1 天数据, 这就是那个 bug"
    assert (today - new_start).days > 1000, \
        f"新标的应回溯约 5 年(>1000 天), 实际只回溯 {(today - new_start).days} 天"


@test("返回类型是 (date, bool) 二元组")
def t_return_type():
    conn = _empty_conn()
    r = dc.compute_fetch_start(conn, "daily_price", "X", 5, date(2026, 10, 10),
                               existing_last=date(2026, 9, 30))
    assert isinstance(r, tuple) and len(r) == 2, f"期望二元组, 实际 {r!r}"
    start, is_new = r
    assert type(start) is date, f"第 1 个元素应为 date, 实际 {type(start).__name__}"
    assert type(is_new) is bool, f"第 2 个元素应为 bool, 实际 {type(is_new).__name__}"


@test("history_years 参数真的生效(改年份, 起算点跟着变)")
def t_history_years():
    conn = _empty_conn()
    today = date(2026, 10, 10)
    s3, _ = dc.compute_fetch_start(conn, "daily_price", "X", 3, today,
                                   existing_last=None)
    s7, _ = dc.compute_fetch_start(conn, "daily_price", "X", 7, today,
                                   existing_last=None)
    assert s7 < s3, f"7 年({s7}) 应早于 3 年({s3})"
    assert (s3 - s7).days > 1300, "3 年与 7 年应相差约 4 年(1460 天左右)"
