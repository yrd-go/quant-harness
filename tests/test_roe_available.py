#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""测试: ROE 的"法定披露可用日"映射。

背景(这是本项目修复过的一个重要 bug):
    ROE 的报告期末日 != 数据可用日。A 股年报报告期是 12-31, 但实际披露要到
    次年 3~4 月。原实现直接拿报告期末日当"可用日", 导致回测在 1 月就"知道"了
    12-31 的年报 ROE —— 而那份财报当时根本还没公布。这是静默的前视偏差。

这些用例把"披露滞后"这条规则固化下来, 防止以后被人改回去。
"""

from datetime import date

from _runner import test
import data_center as dc


@test("ROE 一季报: 报告期 03-31 -> 当年 04-30 (滞后 30 天)")
def t_q1():
    assert dc.roe_available_from(date(2025, 3, 31)) == date(2025, 4, 30)


@test("ROE 中报: 报告期 06-30 -> 当年 08-31 (滞后 62 天)")
def t_h1():
    assert dc.roe_available_from(date(2025, 6, 30)) == date(2025, 8, 31)


@test("ROE 三季报: 报告期 09-30 -> 当年 10-31 (滞后 31 天)")
def t_q3():
    assert dc.roe_available_from(date(2025, 9, 30)) == date(2025, 10, 31)


@test("ROE 年报: 报告期 12-31 -> 次年 04-30 (滞后 120 天) ★核心")
def t_annual():
    # 这是前视偏差的主要来源: 滞后整整 120 天
    assert dc.roe_available_from(date(2024, 12, 31)) == date(2025, 4, 30)
    assert dc.roe_available_from(date(2025, 12, 31)) == date(2026, 4, 30)
    # 跨年必须正确进位
    assert dc.roe_available_from(date(2024, 12, 31)).year == 2025


@test("可用日必须【恒晚于或等于】报告期末(绝不提前, 否则就是前视)")
def t_never_early():
    for y in (2023, 2024, 2025, 2026):
        for m in (3, 6, 9, 12):
            rp = date(y, m, 31 if m in (3, 12) else 30)
            av = dc.roe_available_from(rp)
            assert av >= rp, f"{rp} 的可用日 {av} 早于报告期末, 这是前视偏差"


@test("四个报告期的滞后天数符合 A 股法定披露规则")
def t_lag_days():
    expect = {
        date(2025, 3, 31): 30,    # 一季报 -> 4-30
        date(2025, 6, 30): 62,    # 中报   -> 8-31
        date(2025, 9, 30): 31,    # 三季报 -> 10-31
        date(2025, 12, 31): 120,  # 年报   -> 次年 4-30
    }
    for rp, days in expect.items():
        got = (dc.roe_available_from(rp) - rp).days
        assert got == days, f"{rp}: 期望滞后 {days} 天, 实际 {got} 天"


@test("非标准报告期末也要返回一个日期且不提前(兜底分支)")
def t_fallback():
    # 例如误传了一个 2025-01-31
    rp = date(2025, 1, 31)
    av = dc.roe_available_from(rp)
    assert isinstance(av, date)
    assert av >= rp, "兜底分支也必须保证不提前"


@test("返回类型必须是 date 而不是 datetime(下游按 date 比较)")
def t_type():
    av = dc.roe_available_from(date(2025, 12, 31))
    assert type(av) is date, f"期望 date, 实际 {type(av).__name__}"
