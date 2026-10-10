#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""tests/_runner.py — 零依赖迷你测试框架

为什么不用 pytest:
    本项目部署环境网络受限, PyPI 下载常常超时(实测清华源也慢)。
    测试要能在【任何环境、零额外依赖】下跑起来, 所以自己实现一个极简 runner。
    功能上够用: 用例注册 / 断言 / 异常捕获 / 汇总报告 / 非零退出码。

用法:
    python tests/run_tests.py            # 跑全部
    python tests/run_tests.py -v         # 显示每个用例
    python tests/run_tests.py vacuum     # 只跑名字含 vacuum 的
"""

from __future__ import annotations

import sys
import traceback
from pathlib import Path

# ---- 路径引导: 让测试能 import 到项目根与 src/ ----
_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
for p in (str(_ROOT), str(_ROOT / "src")):
    if p not in sys.path:
        sys.path.insert(0, p)

_TESTS: list[tuple[str, callable]] = []


def test(name: str):
    """把函数注册成一个用例。"""
    def deco(fn):
        _TESTS.append((name, fn))
        return fn
    return deco


def run(pattern: str | None = None, verbose: bool = True) -> int:
    """跑全部(或匹配 pattern 的)用例, 返回失败数。"""
    selected = [(n, f) for n, f in _TESTS if not pattern or pattern in n]
    if not selected:
        print(f"没有匹配 {pattern!r} 的用例")
        return 1

    passed, failed = 0, []
    print("=" * 74)
    print(f"运行 {len(selected)} 个用例" + (f" (筛选: {pattern})" if pattern else ""))
    print("=" * 74)

    for name, fn in selected:
        try:
            fn()
            passed += 1
            if verbose:
                print(f"  [PASS] {name}")
        except AssertionError as exc:
            failed.append((name, f"断言失败: {exc}", traceback.format_exc()))
            print(f"  [FAIL] {name}")
            print(f"         {exc}")
        except Exception as exc:
            failed.append((name, f"{type(exc).__name__}: {exc}",
                           traceback.format_exc()))
            print(f"  [ERROR] {name}")
            print(f"          {type(exc).__name__}: {exc}")

    print()
    print("=" * 74)
    print(f"结果: 通过 {passed} / 失败 {len(failed)} / 共 {len(selected)}")
    if failed:
        print()
        print("失败详情:")
        for name, msg, tb in failed:
            print(f"\n--- {name} ---")
            print(tb.rstrip())
    print("=" * 74)
    return len(failed)
