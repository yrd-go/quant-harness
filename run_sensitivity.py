#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
run_sensitivity.py — 参数敏感性 / 样本外测试的批量跑批工具

作用: 一次跑多组参数, 自动收集"总收益 / 最大回撤 / 夏普 / 卡玛", 最后打一张对比表。
避免手点十几次 + 手工抄数字抄错。

用法
----
    # 跑 MA = 15 / 20 / 30 三组(默认就是这三组)
    python run_sensitivity.py

    # 自定义要对比的参数
    python run_sensitivity.py --ma 10,15,20,30,60
    python run_sensitivity.py --stop_loss 0.08,0.15,0.25
    python run_sensitivity.py --sell_threshold 2,3,4,6

    # 样本外测试: 把区间切成样本内 / 样本外分别跑
    python run_sensitivity.py --ma 15,20,30 --start 2021-01-01 --end 2023-12-31
    python run_sensitivity.py --ma 15,20,30 --start 2024-01-01 --end 2026-09-30

    # 逗号分隔的多个维度会做"笛卡尔积", 注意组合数量会相乘
    python run_sensitivity.py --ma 15,20 --stop_loss 0.10,0.15

说明
----
* 本脚本只是个"调度器", 不重新实现回测逻辑; 每一组都是真的去跑一次 src/backtest.py,
  所以结果和你手动敲命令完全一致。
* 每组的价格曲线图会由 backtest.py 自己按参数命名保存到 output/, 不会互相覆盖。
* 输出的表格同时会存一份 CSV 到 output/sensitivity_summary.csv, 方便贴到 Excel 里画图。
"""

from __future__ import annotations

import argparse
import csv
import itertools
import os
import re
import subprocess
import sys
from pathlib import Path

# 项目根目录(本文件所在目录)
BASE_DIR = Path(__file__).resolve().parent


def _find_backtest() -> Path:
    """定位 backtest.py: 优先 src/, 找不到再找项目根。"""
    for candidate in (BASE_DIR / "src" / "backtest.py", BASE_DIR / "backtest.py"):
        if candidate.exists():
            return candidate
    raise FileNotFoundError(
        "找不到 backtest.py, 请确认它在 src/ 或项目根目录下。"
    )


def _find_python() -> str:
    """优先用项目自带的虚拟环境解释器, 保证依赖一致。"""
    for rel in (".venv-quant/Scripts/python.exe", ".venv-quant/bin/python",
                "venv/Scripts/python.exe", "venv/bin/python"):
        p = BASE_DIR / rel
        if p.exists():
            return str(p)
    return sys.executable       # 退回当前解释器


def parse_float_list(text: str, cast=float) -> list:
    """把 '0.10,0.15' 解析成 [0.1, 0.15]; 解析失败给出明确提示。"""
    out = []
    for chunk in str(text).split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            out.append(cast(chunk))
        except ValueError as exc:
            raise ValueError(f"无法解析参数值 {chunk!r}: {exc}") from exc
    if not out:
        raise ValueError(f"参数列表为空: {text!r}")
    return out


# 从 backtest.py 的输出里抓关键指标。
# 用正则而不是 import 后取返回值, 是为了保证"跑批看到的数字 == 手动跑看到的数字"。
PATTERNS = {
    "total_return": re.compile(r"总收益率\s*:\s*(-?[\d.]+)%"),
    "max_drawdown": re.compile(r"最大回撤\s*:\s*(-?[\d.]+)%"),
    "sharpe": re.compile(r"夏普比率\s*:\s*(-?[\d.]+)"),
    "calmar": re.compile(r"卡玛比率\s*:\s*(-?[\d.]+)"),
    "annual_return": re.compile(r"年化收益率\s*:\s*(-?[\d.]+)%"),
    "final_value": re.compile(r"期末净值\s*:\s*([\d,]+\.?\d*)"),
    "avoid_count": re.compile(r"触发避险次数\s*:\s*(\d+)"),
    "stop_count": re.compile(r"止损触发次数\s*:\s*(\d+)"),
}


def parse_metrics(text: str) -> dict:
    """从一次运行的 stdout 里提取指标; 缺哪个就留 None。"""
    m: dict = {}
    for key, pat in PATTERNS.items():
        hit = pat.search(text)
        if not hit:
            m[key] = None
            continue
        raw = hit.group(1).replace(",", "")
        try:
            m[key] = float(raw)
        except ValueError:
            m[key] = None
    return m


def _read_text_auto(path: Path) -> str:
    """读回子进程输出, 自动吃掉 BOM 并容错编码。

    为什么不能简单 read_text(encoding="utf-8"):
      子进程若没被强制成 UTF-8, 在 Windows 中文环境下会按 GBK(cp936) 写文件。
      那种字节流用 UTF-8 硬解会产生大量 U+FFFD 替换字符, 而且【不会抛异常】——
      结果是正则一个都匹配不上, 但程序假装一切正常, 非常难排查。

    所以这里的策略是:
      1. 先用 utf-8-sig 解(BOM 自动吃掉);
      2. 统计替换字符 U+FFFD 的占比, 超过 1% 就说明大概率是 GBK 被当 UTF-8 解了,
         退回用 gbk 重解一次;
      3. 两次都留着 errors="replace" 兜底, 保证任何脏字节都不会让整个跑批崩掉。
    """
    raw = path.read_bytes()
    if not raw:
        return ""
    text = raw.decode("utf-8-sig", errors="replace")
    if text.count("\ufffd") / max(len(text), 1) > 0.01:
        # 疑似 GBK: 用 cp936 重解, 能救回来大部分中文
        text = raw.decode("gbk", errors="replace")
    return text


def run_one(python: str, script: Path, extra_args: list[str]) -> tuple[dict, str]:
    """跑一组参数, 返回 (指标, 原始输出)。

    实现说明(三个刻意的工程选择):
      1. 【不用 subprocess 管道】(不写 capture_output=True / PIPE), 而是把子进程的
         stdout/stderr 重定向到文件, 跑完再读回来。原因: 某些受限环境(沙箱/企业安全策略)
         禁止进程创建匿名管道, 用 PIPE 会直接抛 PermissionError: [WinError 5];
         重定向到文件则通行无阻, 输出内容完全一样。
      2. 临时文件放在【项目内的 .tmp/】而不是系统 %TEMP%。原因: 部分受限环境下
         系统临时目录不可写; 放在工作区里最稳, 且跑完就删。
      3. 【必须给子进程传 env, 强制 PYTHONIOENCODING=utf-8】——
         在 src/backtest.py 里写 os.environ[...] 是没用的: 那是给"当前进程"设的,
         子进程是独立进程, 环境变量必须在它启动时就注入(env=...)。
         Windows 中文环境下子进程默认按 GBK(cp936) 输出, 而中文指标名("总收益率"等)
         一旦编码不匹配就会变成乱码, 导致正则匹配全部失败、抓不到任何指标。
    """
    cmd = [python, str(script), "--quiet"] + extra_args

    # ---- 关键修复: 构造子进程环境, 强制 UTF-8 输出 ----
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"    # 子进程的 stdout/stderr 一律用 UTF-8
    env["PYTHONUTF8"] = "1"              # 双保险: 让子进程整体走 UTF-8 模式
    env.setdefault("PYTHONUNBUFFERED", "1")  # 不缓冲, 避免异常退出时丢最后一段输出

    tmp_dir = BASE_DIR / ".tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    out_path = tmp_dir / "sens_stdout.txt"
    err_path = tmp_dir / "sens_stderr.txt"
    proc = None
    try:
        # 注意: 文件句柄这里必须也用 utf-8 打开, 才能和子进程的 UTF-8 输出对齐。
        # (如果这里写 utf-8 而子进程吐 GBK, 会在【写入时】就把中文替换掉,
        #  属于不可逆损坏, 读回时再改编码也救不回来 —— 所以 env 那一手才是根治。)
        with open(out_path, "w+", encoding="utf-8", errors="replace") as f_out, \
                open(err_path, "w+", encoding="utf-8", errors="replace") as f_err:
            proc = subprocess.run(cmd, cwd=str(BASE_DIR), env=env,
                                  stdout=f_out, stderr=f_err, text=True)
        output = _read_text_auto(out_path)
        output += _read_text_auto(err_path)
    except Exception as exc:
        # 子进程起不来(解释器路径错、权限被拒等)也不该让整批跑批中断
        output = f"[run_one 内部错误] {type(exc).__name__}: {exc}"
        metrics = {"exit_code": -1}
        metrics.update(parse_metrics(output))
        return metrics, output
    finally:
        # 尽力清理; 删不掉也不该让整个跑批失败
        for p in (out_path, err_path):
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass

    metrics = parse_metrics(output)
    metrics["exit_code"] = proc.returncode if proc is not None else -1
    return metrics, output


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="批量跑多组参数并汇总对比(参数敏感性 / 样本外测试)")
    p.add_argument("--ma", default="15,20,30",
                   help="【单均线模式】要对比的均线周期, 逗号分隔(默认 15,20,30)。"
                        "注意: 一旦传了 --short_ma/--long_ma, backtest.py 会切换到"
                        "双均线模式, 此时本参数完全不参与计算")
    p.add_argument("--short_ma", default="",
                   help="【双均线模式】要对比的短均线周期, 逗号分隔(留空=走单均线模式)")
    p.add_argument("--long_ma", default="",
                   help="【双均线模式】要对比的长均线周期, 逗号分隔。"
                        "与 --short_ma 组成笛卡尔积, 注意组合数会相乘")
    p.add_argument("--stop_loss", default="",
                   help="要对比的止损比例, 逗号分隔(留空=只用默认值)")
    p.add_argument("--sell_threshold", default="",
                   help="要对比的缓冲带阈值, 逗号分隔(留空=只用默认值)")
    p.add_argument("--top_n", default="",
                   help="要对比的持仓数量, 逗号分隔(留空=只用默认值)")
    p.add_argument("--momentum_window", default="",
                   help="要对比的动量窗口, 逗号分隔(留空=只用默认值)")
    p.add_argument("--start", default=None, help="回测开始日期(样本外测试用)")
    p.add_argument("--end", default=None, help="回测结束日期(样本外测试用)")
    p.add_argument("--name", default=None,
                   help="给本批运行加标签, 会拼到图片文件名末尾"
                        "(例如 --name oos, 用来区分样本内/样本外)")
    p.add_argument("--show-log", action="store_true",
                   help="把每组运行的完整输出也打出来(默认只看汇总表)")
    args = p.parse_args(argv)

    try:
        script = _find_backtest()
        sma_list = parse_float_list(args.short_ma, int) if args.short_ma else [None]
        lma_list = parse_float_list(args.long_ma, int) if args.long_ma else [None]
        is_dual = bool(args.short_ma or args.long_ma)
        # 【关键】双均线模式下 --ma 完全不参与计算, 所以不能让它的默认值("15,20,30")
        # 还去展开笛卡尔积 —— 否则 9 组会变成 27 组, 同一套参数被重复跑 3 遍。
        # 早期版本就踩了这个坑: 输出里出现 3 行一模一样的参数组合。
        ma_list = [None] if is_dual else parse_float_list(args.ma, int)
        sl_list = parse_float_list(args.stop_loss, float) if args.stop_loss else [None]
        st_list = parse_float_list(args.sell_threshold, int) if args.sell_threshold else [None]
        tn_list = parse_float_list(args.top_n, int) if args.top_n else [None]
        mw_list = parse_float_list(args.momentum_window, int) if args.momentum_window else [None]
    except (ValueError, FileNotFoundError) as exc:
        print(f"[错误] {exc}")
        return 2

    # 双均线模式下, 逐组校验"短 < 长"; 不合法的组合直接跳过, 而不是等 backtest.py 报错。
    # 这样一次跑 15 组时不会因为其中 3 组参数反了就把整批搞出一堆错误输出。
    def _combo_ok(sma, lma) -> tuple[bool, str]:
        if not is_dual:
            return True, ""
        s = sma if sma is not None else 10      # 与 backtest.py 的默认值保持一致
        l = lma if lma is not None else 30
        if s >= l:
            return False, f"短均线 {s} >= 长均线 {l}, 金叉方向会反, 已跳过"
        return True, ""

    python = _find_python()
    combos = list(itertools.product(ma_list, sma_list, lma_list,
                                   sl_list, st_list, tn_list, mw_list))

    print("=" * 92)
    print("批量参数测试")
    print("=" * 92)
    print(f"  backtest.py : {script}")
    print(f"  解释器      : {python}")
    print(f"  区间        : {args.start or '(默认)'} ~ {args.end or '(默认)'}")
    print(f"  择时模式    : {'双均线交叉' if is_dual else '单均线'}")
    print(f"  组合数量    : {len(combos)} 组")
    print("=" * 92)

    rows: list[dict] = []
    for idx, (ma, sma, lma, sl, st, tn, mw) in enumerate(combos, 1):
        ok, why = _combo_ok(sma, lma)
        if not ok:
            print(f"\n[{idx}/{len(combos)}] 跳过: 短均线{sma} / 长均线{lma} —— {why}")
            continue

        if is_dual:
            # 双均线模式: 不传 --ma, 只传短/长均线, 避免 backtest.py 报"两套参数都给了"的警告
            extra = []
            label_parts = []
            if sma is not None:
                extra += ["--short_ma", str(sma)]
                label_parts.append(f"短{sma}")
            if lma is not None:
                extra += ["--long_ma", str(lma)]
                label_parts.append(f"长{lma}")
            if not label_parts:
                label_parts.append("双均线(默认10/30)")
        else:
            extra = ["--ma", str(ma)]
            label_parts = [f"MA{ma}"]

        if sl is not None:
            extra += ["--stop_loss", str(sl)]
            label_parts.append(f"止损{sl:.0%}")
        if st is not None:
            extra += ["--sell_threshold", str(st)]
            label_parts.append(f"缓冲{st}")
        if tn is not None:
            extra += ["--top_n", str(tn)]
            label_parts.append(f"持仓{tn}")
        if mw is not None:
            extra += ["--momentum_window", str(mw)]
            label_parts.append(f"动量{mw}")
        if args.start:
            extra += ["--start", args.start]
        if args.end:
            extra += ["--end", args.end]
        if args.name:
            extra += ["--name", args.name]

        label = " ".join(label_parts)
        print(f"\n[{idx}/{len(combos)}] 正在跑: {label} ...")

        metrics, output = run_one(python, script, extra)
        if args.show_log:
            print(output)

        if metrics.get("total_return") is None:
            print(f"  !! 这一组没抓到指标(exit={metrics.get('exit_code')}), 可能参数非法或数据不足。")
            # 把最后的报错行打出来, 方便定位
            for line in output.strip().splitlines()[-8:]:
                print("     | " + line)

        rows.append({"label": label, "mode": "dual" if is_dual else "single",
                     "ma": ma, "short_ma": sma, "long_ma": lma,
                     "stop_loss": sl, "sell_threshold": st,
                     "top_n": tn, "momentum_window": mw,
                     **metrics})
        if metrics.get("total_return") is not None:
            print(f"  总收益 {metrics['total_return']:+.2f}%  |  "
                  f"最大回撤 {metrics['max_drawdown']:.2f}%  |  "
                  f"夏普 {metrics['sharpe']:.3f}  |  卡玛 {metrics['calmar']:.3f}")

    # ---------------- 汇总表 ----------------
    print("\n" + "=" * 92)
    print("汇总对比")
    print("=" * 92)
    header = (f"{'参数组合':<28}{'总收益':>10}{'年化':>9}{'最大回撤':>11}"
              f"{'夏普':>9}{'卡玛':>9}{'避险':>6}{'止损':>6}")
    print(header)
    print("-" * 92)

    def _cell(v, fmt, suffix=""):
        return "  N/A".rjust(len(fmt % 0) + 1) if v is None else (fmt % v) + suffix

    for r in rows:
        if r.get("total_return") is None:
            print(f"{r['label']:<28}{'  抓取失败':>10}")
            continue
        print(f"{r['label']:<28}"
              f"{r['total_return']:>9.2f}%"
              f"{r['annual_return']:>8.2f}%"
              f"{r['max_drawdown']:>10.2f}%"
              f"{r['sharpe']:>9.3f}"
              f"{r['calmar']:>9.3f}"
              f"{(int(r['avoid_count']) if r['avoid_count'] is not None else 0):>6}"
              f"{(int(r['stop_count']) if r['stop_count'] is not None else 0):>6}")
    print("-" * 92)

    # 简单挑出两个极端, 方便快速判断稳健性
    ok = [r for r in rows if r.get("total_return") is not None]
    if ok:
        best_ret = max(ok, key=lambda r: r["total_return"])
        best_dd = min(ok, key=lambda r: r["max_drawdown"])       # 回撤是负数, 越大越接近0
        best_calmar = max(ok, key=lambda r: (r["calmar"] if r["calmar"] is not None else -9e9))
        print(f"  收益最高 : {best_ret['label']}  ({best_ret['total_return']:+.2f}%)")
        print(f"  回撤最小 : {best_dd['label']}  ({best_dd['max_drawdown']:.2f}%)")
        print(f"  卡玛最优 : {best_calmar['label']}  ({best_calmar['calmar']:.3f})")
        spreads = [r["total_return"] for r in ok]
        if len(spreads) > 1:
            print(f"  收益极差 : {max(spreads) - min(spreads):.2f} 个百分点"
                  f"   <- 极差越大说明策略对参数越敏感(越可能过拟合)")
    print("=" * 92)

    # ---------------- 落盘 ----------------
    out_csv = BASE_DIR / "output" / "sensitivity_summary.csv"
    try:
        if not rows:
            print("[警告] 没有任何一组成功执行(可能是参数组合全部非法), 不写汇总 CSV。")
        else:
            # 用所有行的键的并集做表头: 直接取 rows[0].keys() 在"某些行字段更少"时会漏列
            fieldnames: list[str] = []
            for r in rows:
                for k in r.keys():
                    if k not in fieldnames:
                        fieldnames.append(k)
            out_csv.parent.mkdir(parents=True, exist_ok=True)
            with open(out_csv, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames, restval="")
                writer.writeheader()
                writer.writerows(rows)
            print(f"对比表已保存: {out_csv}")
    except Exception as exc:
        print(f"[警告] 汇总 CSV 写入失败(不影响上面的结果): {exc}")

    print("价格曲线图在 output/ 下, 文件名带参数后缀, 可直接并排对比。")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
