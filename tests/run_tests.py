#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""tests/run_tests.py — 运行 quant-harness 的全部测试

    python tests/run_tests.py           # 全部
    python tests/run_tests.py -v        # 显示每个用例通过
    python tests/run_tests.py vacuum    # 只跑名字含 vacuum 的

退出码: 0 = 全部通过, 1 = 有失败(可直接用于 CI)
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _runner  # noqa: E402

# 导入即注册用例(顺序无所谓)
import test_roe_available  # noqa: E402,F401
import test_fetch_start  # noqa: E402,F401
import test_roll_cleanup  # noqa: E402,F401
import test_vacuum_neutral  # noqa: E402,F401
import test_paper_broker  # noqa: E402,F401


def main(argv: list[str]) -> int:
    verbose = True
    pattern = None
    for a in argv:
        if a in ("-v", "--verbose"):
            verbose = True
        elif a in ("-q", "--quiet"):
            verbose = False
        elif not a.startswith("-"):
            pattern = a
    return _runner.run(pattern=pattern, verbose=verbose)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
