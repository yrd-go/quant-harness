#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""run_daily.py — 每日流程编排: 一条命令跑完整个流水线

为什么需要它
------------
纸面跟踪的全部价值在于【连续】。手动跑三条命令, 几天后必然断,
一断净值曲线就有洞、盈亏比统计就失真。

本脚本把流水线串起来, 并且决定"今天要不要刷新目标池"。

刷新节奏(方案 B: 周频刷新)
---------------------------
    平时(周二~周五):  ① 增量更新行情  → ② 刷新目标池  → ③ 纸面跟踪
    周一(或候选池过期): 额外先跑 ①b 全市场海选(约 24 分钟)

为什么目标池不全市场海选每天重跑:
    三层漏斗用的是 20 日动量, 一天之内排名几乎不变 —— 每天重跑 24 分钟
    基本是在重复昨天的结果。而且频繁换仓会被【最低 5 元手续费】反复摩擦:
    10 只持仓每天微调一次 = 50 元/天 ≈ 全年 1.2 万, 占 10 万本金的 12%。

用法
----
    python run_daily.py                    # 按"候选池年龄"自动决定是否海选
    python run_daily.py --force-screen     # 强制重跑海选
    python run_daily.py --skip-screen      # 强制跳过海选(即使过期)
    python run_daily.py --dry-run          # 每一步都不写库/不写文件
    python run_daily.py --max-age-days 14  # 候选池超过 14 天算过期(默认 7)

退出码: 0 = 全部成功; 1 = 有步骤失败(详见日志)
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# --------------------------------------------------------------------------- #
# 路径引导
# --------------------------------------------------------------------------- #
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from config import CONFIG_DATA_DIR, LOGS_DIR, now_bj  # noqa: E402

CANDIDATES = CONFIG_DATA_DIR / "candidates.json"
TARGET_POOL = CONFIG_DATA_DIR / "target_pool.json"
LOG_FILE = LOGS_DIR / "run_daily.log"

PY = sys.executable
DATA_CENTER = _HERE / "src" / "data_center.py"
SCREENER = _HERE / "src" / "market_screener.py"
NEWS_AGENT = _HERE / "src" / "news_agent.py"
PAPER = _HERE / "paper_trading.py"

log = logging.getLogger("run_daily")


def setup_logging(verbose: bool = True) -> None:
    log.setLevel(logging.DEBUG)
    log.handlers.clear()
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S")
    c = logging.StreamHandler(sys.stdout)
    c.setLevel(logging.DEBUG if verbose else logging.INFO)
    c.setFormatter(fmt)
    log.addHandler(c)
    try:
        fh = logging.FileHandler(LOG_FILE, mode="a", encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)
        log.addHandler(fh)
    except Exception as exc:
        print(f"[警告] 无法写日志文件: {exc}")


# --------------------------------------------------------------------------- #
# 步骤执行
# --------------------------------------------------------------------------- #
def run_step(name: str, script: Path, extra_args: list[str],
             timeout_min: float = 60.0) -> tuple[bool, str]:
    """跑一个流水线步骤。返回 (是否成功, 输出尾部)。

    关键设计: 【不用管道捕获输出】——
    某些受限环境禁止创建匿名管道(实测 subprocess 用 PIPE 会 WinError 5),
    所以把 stdout/stderr 重定向到项目内 .tmp/ 的文件, 再读回来。
    """
    if not script.exists():
        return False, f"脚本不存在: {script}"

    tmp = _HERE / ".tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    out_file = tmp / f"{script.stem}.log"

    log.info("-" * 68)
    log.info("▶ %s", name)
    log.info("  命令: %s %s %s", script.name, " ".join(extra_args),
             "(输出重定向到 .tmp/)" if extra_args or True else "")

    # 子进程强制 UTF-8, 否则 Windows 下中文按 GBK 输出会被读成乱码
    import os
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"

    t0 = time.time()
    try:
        with open(out_file, "w+", encoding="utf-8", errors="replace") as f:
            p = subprocess.run([PY, str(script), *extra_args],
                               cwd=str(_HERE), env=env,
                               stdout=f, stderr=subprocess.STDOUT,
                               text=True, timeout=timeout_min * 60)
        rc = p.returncode
    except subprocess.TimeoutExpired:
        return False, f"超时(>{timeout_min:.0f} 分钟)"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"

    dt = time.time() - t0
    tail = ""
    try:
        text = out_file.read_text(encoding="utf-8-sig", errors="replace")
        lines = [l for l in text.splitlines() if l.strip()]
        tail = "\n".join(lines[-6:])
    except Exception:
        pass

    if rc == 0:
        log.info("  ✅ 完成 (%.0f 秒)", dt)
        return True, tail
    log.error("  ❌ 失败 (退出码 %d, %.0f 秒)", rc, dt)
    if tail:
        log.error("  输出尾部:")
        for line in tail.splitlines():
            log.error("    %s", line)
    return False, tail


def candidates_age_days() -> float | None:
    """候选池文件距今多少天(None = 文件不存在)。"""
    if not CANDIDATES.exists():
        return None
    mtime = datetime.fromtimestamp(CANDIDATES.stat().st_mtime)
    return (datetime.now() - mtime).total_seconds() / 86400.0


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="每日流程编排: 行情更新 → 目标池刷新 → 纸面跟踪",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    ap.add_argument("--force-screen", action="store_true",
                    help="强制重跑全市场海选(即使候选池没过期)")
    ap.add_argument("--skip-screen", action="store_true",
                    help="强制跳过海选(即使候选池过期)")
    ap.add_argument("--max-age-days", type=float, default=7.0,
                    help="候选池超过多少天算过期, 需要重新海选")
    ap.add_argument("--dry-run", action="store_true",
                    help="每一步都加 --dry-run(不写库/不写文件)")
    ap.add_argument("--quiet", action="store_true", help="只输出 INFO 及以上")
    args = ap.parse_args(argv)

    setup_logging(verbose=not args.quiet)
    dry = ["--dry-run"] if args.dry_run else []

    log.info("=" * 68)
    log.info("量化流水线 | 当前北京时间 %s", now_bj().strftime("%Y-%m-%d %H:%M:%S"))
    log.info("  周几: %s", ["一", "二", "三", "四", "五", "六", "日"][now_bj().weekday()])
    log.info("  模式: %s", "dry-run(不写任何东西)" if args.dry_run else "正式运行")
    log.info("=" * 68)

    results: list[tuple[str, bool]] = []

    # ---------------- 决定今天要不要海选 ----------------
    age = candidates_age_days()
    if args.force_screen:
        need_screen, why = True, "--force-screen 指定"
    elif args.skip_screen:
        need_screen, why = False, "--skip-screen 指定"
    elif age is None:
        need_screen, why = True, "候选池不存在(首次运行)"
    elif age > args.max_age_days:
        need_screen, why = True, f"候选池已 {age:.1f} 天未更新(> {args.max_age_days} 天)"
    else:
        need_screen, why = False, f"候选池 {age:.1f} 天前刚更新过"

    log.info("")
    log.info("① 全市场海选: %s  (%s)", "执行" if need_screen else "跳过", why)
    if need_screen:
        log.info("   注意: 预计约 24 分钟(300 只 x 腾讯源 3 秒 + 主动限速)")
    log.info("")

    # ---------------- 步骤 1: 增量更新行情 ----------------
    ok, _ = run_step("② 增量更新行情(data_center --update)",
                     DATA_CENTER, ["--update", *dry], timeout_min=30)
    results.append(("行情更新", ok))
    if not ok:
        log.error("行情更新失败 —— 后续步骤依赖它, 中止")
        return 1

    # ---------------- 步骤 2: 海选(按需) ----------------
    if need_screen:
        # 小规模试跑容易漏股票; 用默认的 300/100 才能体现漏斗
        ok, _ = run_step("① 全市场海选(market_screener)",
                         SCREENER, ["--quiet", *dry], timeout_min=90)
        results.append(("全市场海选", ok))
        if not ok:
            log.warning("海选失败, 但继续用【已有候选池】跑后面的步骤")
    else:
        results.append(("全市场海选", True))

    # ---------------- 步骤 3: 刷新目标池 ----------------
    if not CANDIDATES.exists():
        log.error("候选池不存在(%s), 无法生成目标池", CANDIDATES.name)
        log.error("请先手动跑一次: python src/market_screener.py")
        return 1
    ok, _ = run_step("③ 宏观+财报过滤(news_agent)",
                     NEWS_AGENT, ["--quiet", *dry], timeout_min=20)
    results.append(("目标池刷新", ok))
    if not ok:
        log.warning("目标池刷新失败, 但继续用【已有目标池】跑纸面跟踪")

    # ---------------- 步骤 3.5: 让数据库跟上新的候选池 ★ ----------------
    # 为什么必须有这一步:
    #   海选每次可能选出【全新一批】股票, 而数据库里没有它们的行情。
    #   不补的话, 纸面跟踪会因为"无当日价格"而只能操作少数几只,
    #   "等权分散"就落空了 —— 这个坑实测踩过: 15 只目标池里库里只有 3 只。
    #
    # 为什么用 candidates.json(100 只) 而不是 target_pool.json(15 只):
    #   候选池是每周海选的结果, 覆盖它就等于覆盖了整周可能进入目标池的股票。
    #   而且数据是复用的 —— 今天补过, 明天 --update 就只拉增量, 很快。
    #   代价: 首次运行要补齐 100 只的历史(约 20-30 分钟), 之后每天只需增量。
    if CANDIDATES.exists():
        ok, _ = run_step("③b 按候选池补齐行情(data_center --pool candidates)",
                         DATA_CENTER, ["--pool", str(CANDIDATES), *dry],
                         timeout_min=60)
        results.append(("候选池补数据", ok))
        if not ok:
            log.warning("候选池补数据失败, 继续跑(纸面跟踪可能跳过部分标的)")
    else:
        results.append(("候选池补数据", True))

    # ---------------- 步骤 4: 纸面跟踪 ----------------
    if not (TARGET_POOL.exists()):
        log.error("目标池不存在, 无法纸面跟踪")
        return 1
    ok, _ = run_step("④ 纸面跟踪(paper_trading --daily)",
                     PAPER, ["--daily", *dry], timeout_min=10)
    results.append(("纸面跟踪", ok))

    # ---------------- 汇总 ----------------
    log.info("")
    log.info("=" * 68)
    log.info("流水线完成")
    for name, ok in results:
        log.info("  %s %s", "✅" if ok else "❌", name)
    failed = [n for n, ok in results if not ok]
    if failed:
        log.error("  失败步骤: %s", ", ".join(failed))
    log.info("=" * 68)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
