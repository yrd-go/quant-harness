#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
plot_heatmap.py — 参数敏感性热力图(通用)

读取 run_sensitivity.py 产出的 sensitivity_summary.csv(或任何同结构 CSV),
把两个参数作为 X/Y 轴、某个绩效指标作为颜色, 画成二维热力图。
用途: 直观找"参数平原"(一片连续的好区域) 而不是"参数尖峰"(孤立的单点最优)。
参数平原通常意味着策略稳健; 孤立尖峰通常意味着过拟合。

基本用法
--------
    # 双均线: 短均线 vs 长均线, 颜色=总收益, 格子里标最大回撤
    python src/plot_heatmap.py --x short_ma --y long_ma --color total_return --annot max_drawdown

    # 想看夏普的分布
    python src/plot_heatmap.py --x short_ma --y long_ma --color sharpe --annot max_drawdown

    # 单均线 vs 止损(先跑一次: run_sensitivity.py --ma 15,20,25 --stop_loss 0.08,0.15,0.25)
    python src/plot_heatmap.py --x ma --y stop_loss --color total_return --annot max_drawdown

    # 指定输入输出
    python src/plot_heatmap.py --csv output/sensitivity_summary.csv \
        --out output/heatmap_short_long.png

说明与注意
----------
1. CSV 里非数值的单元格(空串 / "N/A" / "nan")会被统一转成 NaN。
   热力图上的 NaN 格子会留白并标注 "--", 这通常代表"该参数组合被跳过"
   (例如双均线要求 短均线 < 长均线, 所以 (20,20)、(30,15) 这类组合不存在)。
   这些空白不是错误, 看热力图时不要把留白误读成"表现差"。
2. 颜色的方向是按指标语义定的:
     收益类(total_return / annual_return / sharpe / calmar): 越大越绿, 且在 0 处居中;
     回撤类(max_drawdown): 越接近 0 越绿(即亏得越少越好), 最小值处最红。
3. 若 X 或 Y 只有一个取值(即只有一个参数在变), 会自动退化成一维折线图, 而不是报错。
4. seaborn 是可选依赖:
     装了 -> 用 seaborn.heatmap(配色与标注更省事);
     没装 -> 自动回退到 matplotlib 手绘(视觉效果基本一致, 只是实现不同)。
   两条路径都会跑通, 不会因为缺包直接崩。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

# --------------------------------------------------------------------------- #
# 默认路径: 锚定到"项目根目录", 而不是当前工作目录
# 本文件在 src/ 下, 所以项目根 = 本文件所在目录的上一级。
# --------------------------------------------------------------------------- #
BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CSV = BASE_DIR / "output" / "sensitivity_summary.csv"
DEFAULT_OUT = BASE_DIR / "output" / "sensitivity_heatmap.png"

# 回撤类指标: 数值是负数, 越接近 0 越好。用来决定配色的方向。
DRAWDOWN_METRICS = {"max_drawdown", "max_dd", "drawdown"}
# 会被强制转成数值的列(存在才转, 不存在的忽略)
NUMERIC_METRICS = ["total_return", "annual_return", "max_drawdown",
                   "sharpe", "calmar", "final_value", "avoid_count", "stop_count"]


# --------------------------------------------------------------------------- #
# 中文字体
# --------------------------------------------------------------------------- #
def setup_chinese_font() -> str:
    """配置 matplotlib 中文字体, 返回实际使用的字体名。

    不设置的话中文会显示成方框(豆腐块)。做法是逐个探测系统里真实存在的字体,
    用 findfont(..., fallback_to_default=False) 抛异常来判断"有没有这个字体",
    比硬编码一个名字更可靠(不同机器装的字体不一样)。
    """
    import matplotlib
    from matplotlib import font_manager

    for name in ("Microsoft YaHei", "SimHei", "SimSun",
                 "Noto Sans CJK SC", "PingFang SC", "DejaVu Sans"):
        try:
            font_manager.findfont(name, fallback_to_default=False)
            matplotlib.rcParams["font.sans-serif"] = [name]
            matplotlib.rcParams["axes.unicode_minus"] = False  # 负号正常显示
            return name
        except Exception:
            continue
    return "(未找到中文字体, 中文可能显示为方框)"


# --------------------------------------------------------------------------- #
# 读数据
# --------------------------------------------------------------------------- #
def load_csv(csv_path: Path) -> pd.DataFrame:
    """读取 CSV 并做基础清洗。

    用 utf-8-sig 解码: run_sensitivity.py 写 CSV 时用的就是 utf-8-sig(带 BOM),
    这个编码读带 BOM 和不带 BOM 的文件都能正确处理。
    """
    if not csv_path.exists():
        raise FileNotFoundError(
            f"找不到 CSV: {csv_path}\n"
            f"       请先跑一次参数扫描, 例如: python run_sensitivity.py --ma 15,20,30"
        )
    df = pd.read_csv(csv_path, encoding="utf-8-sig")
    if df.empty:
        raise ValueError(f"CSV 是空的(没有任何数据行): {csv_path}")
    return df


def coerce_numeric(df: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """把指标列强制转成数值, 非数值(空串/"N/A"/"nan")变成 NaN。

    这是需求里明确要求的一步: CSV 里可能出现 "N/A" 或空值,
    必须转成 NaN, 否则 seaborn/matplotlib 画图会直接崩。
    返回 (处理后的 df, 被转成 NaN 的单元格数)。
    """
    out = df.copy()
    converted = 0
    for col in NUMERIC_METRICS:
        if col not in out.columns:
            continue
        before_na = out[col].isna().sum()
        # errors="coerce": 无法解析的一律变 NaN, 而不是抛异常
        out[col] = pd.to_numeric(out[col], errors="coerce")
        converted += int(out[col].isna().sum() - before_na)
    return out, converted


def check_columns(df: pd.DataFrame, required: list[str]) -> list[str]:
    """检查必需列是否存在, 返回缺失列名列表。"""
    return [c for c in required if c not in df.columns]


def build_matrix(df: pd.DataFrame, x: str, y: str, metric: str
                 ) -> tuple[pd.DataFrame, int]:
    """把长表透视成 矩阵[Y][X] = metric。

    处理要点:
      - aggfunc="mean": 若同一 (x,y) 组合出现多次(比如同参数跑了多遍),
        取平均而不是报错;
      - 索引/列都升序排序(需求要求), 用 sort_index 保证是数值序而不是字符串序
        (否则会出现 10, 100, 20 这种排序);
      - 返回 (矩阵, 被丢掉的省略行数)。
    """
    sub = df[[x, y, metric]].dropna(subset=[x, y])
    dropped = int(len(df) - len(sub))
    if sub.empty:
        return pd.DataFrame(), dropped

    # pivot_table 会自动丢弃 x/y 有 NaN 的行
    matrix = sub.pivot_table(index=y, columns=x, values=metric, aggfunc="mean")
    # 升序排列。若列是数字, sort_index 会按数值排; 混入字符串也不会抛异常。
    try:
        matrix = matrix.sort_index(axis=0).sort_index(axis=1)
    except TypeError:
        matrix = matrix.sort_index(axis=0, key=lambda s: s.astype(str)) \
                       .sort_index(axis=1, key=lambda s: s.astype(str))
    return matrix, dropped


# --------------------------------------------------------------------------- #
# 配色方向
# --------------------------------------------------------------------------- #
def resolve_color_scale(matrix: pd.DataFrame, metric: str,
                        center_arg: float | None) -> tuple[float | None, str]:
    """决定颜色映射的范围与说明文字。

    返回 (center, 说明), center=None 表示不居中(普通顺序色标)。

    规则(按指标语义, 避免"颜色好看但结论读反"):
      - max_drawdown 这类回撤指标: 越接近 0 越好。用 vmin=最小值, vmax=0,
        这样最深回撤是红的、零回撤是绿的。
      - 收益/风险调整类指标: 用 vmin=-M, vmax=+M 关于 0 对称,
        这样 0 永远落在色标正中, 正收益绿、负收益红, 不会因为数据范围偏移而误读。
    """
    vals = matrix.to_numpy(dtype="float64")
    finite = vals[np.isfinite(vals)]
    if finite.size == 0:
        return None, "全部为空值, 无法确定色标范围"

    is_dd = metric.lower() in DRAWDOWN_METRICS
    if center_arg is not None:
        return center_arg, f"色标以 {center_arg:g} 为中心(手动指定)"

    if is_dd:
        # 回撤: 上界固定为 0(代表没有回撤)
        return 0.0, "回撤指标: 越接近 0(越浅)越绿; 最深回撤最红"

    # 收益类: 关于 0 对称
    m = float(np.nanmax(np.abs(finite)))
    if m == 0:
        return 0.0, "所有取值都是 0, 色标无区分度"
    return 0.0, f"色标关于 0 对称(±{m:.2f}), 绿=好, 红=差"


def fmt_cell(v: float | None, ndigits: int = 2) -> str:
    """单元格标注文本。NaN 显示 "--" (代表该组合不存在/被跳过), 而不是显示 nan。"""
    if v is None or not np.isfinite(v):
        return "--"
    return f"{v:.{ndigits}f}"


# --------------------------------------------------------------------------- #
# 画图
# --------------------------------------------------------------------------- #
def draw_heatmap(matrix: pd.DataFrame, x: str, y: str, color: str, annot: str,
                 annot_matrix: pd.DataFrame | None, out_png: Path,
                 center: float | None, scale_note: str,
                 font_name: str, use_seaborn: bool) -> None:
    """画二维热力图并存盘。"""
    import matplotlib
    matplotlib.use("Agg")           # 无界面环境也能出图
    import matplotlib.pyplot as plt

    n_y, n_x = matrix.shape
    # 图幅按格子数量自适应, 免得格子太挤或太空
    fig_w = max(8.0, 1.1 * n_x + 4.0)
    fig_h = max(6.0, 0.7 * n_y + 3.0)
    fig, ax = plt.subplots(figsize=(fig_w, fig_h))

    # ---- 单元格标注文本矩阵 ----
    ann_text = None
    if annot_matrix is not None:
        ann_text = annot_matrix.reindex(index=matrix.index, columns=matrix.columns)

    # ---- 统一的色标范围(两条绘制路径共用, 保证配色语义一致) ----
    masked = np.ma.masked_invalid(matrix.to_numpy(dtype="float64"))
    if center is not None:
        # 以 center 为中心做对称范围: 取"离 center 最远"的那个距离, 左右各留这么多。
        # 注意不能写成 nanmax(abs(values)) —— 数据全为负时那会取到绝对值最大的负数,
        # 导致范围算错(例如 -40 会得到 span=40, 而正确值是 |−40−0| = 40, 恰好巧合;
        # 但若 center≠0 或数据跨零, 就会明显错位)。所以统一按"到 center 的距离"算。
        if masked.count() == 0:
            span = 1.0
        else:
            dist = np.abs(np.asarray(masked.compressed(), dtype="float64") - center)
            span = float(np.max(dist)) if dist.size else 1.0
            if not np.isfinite(span) or span == 0:
                span = 1.0
        vmin, vmax = center - span, center + span
    else:
        if masked.count() == 0:
            vmin, vmax = -1.0, 1.0
        else:
            vmin = float(masked.min())
            vmax = float(masked.max())
            if vmin == vmax:          # 所有格子数值相同 -> 给个最小跨度, 免得色标退化
                vmin, vmax = vmin - 1.0, vmax + 1.0

    if use_seaborn:
        # ---------------- 路径一: seaborn.heatmap ----------------
        import seaborn as sns
        sns.heatmap(
            matrix, ax=ax,
            cmap="RdYlGn",
            center=center,
            vmin=vmin, vmax=vmax,
            annot=ann_text if ann_text is not None else True,
            fmt="",                      # 因为 ann_text 已是字符串; 若为 None 则用 .2f
            linewidths=0.5, linecolor="white",
            cbar_kws={"label": color},
            mask=None,
        )
        # 若没有单独的 annot 矩阵, 就退回用数值本身并以 .2f 格式化重画一遍标注
        if ann_text is None:
            for i in range(n_y):
                for j in range(n_x):
                    v = matrix.iat[i, j]
                    ax.text(j + 0.5, i + 0.5, fmt_cell(v), ha="center", va="center",
                            fontsize=10, color="black")
    else:
        # ---------------- 路径二: matplotlib 手绘(无 seaborn 时的回退) ----------------
        cmap = plt.get_cmap("RdYlGn").copy()
        cmap.set_bad("#e8e8e8")          # NaN 格子用浅灰, 和"数值差"的红色区分开
        im = ax.imshow(masked, cmap=cmap, vmin=vmin, vmax=vmax, aspect="auto")

        ax.set_xticks(range(n_x))
        ax.set_xticklabels([str(v) for v in matrix.columns])
        ax.set_yticks(range(n_y))
        ax.set_yticklabels([str(v) for v in matrix.index])
        ax.set_xticks(np.arange(-0.5, n_x, 1), minor=True)
        ax.set_yticks(np.arange(-0.5, n_y, 1), minor=True)
        ax.grid(which="minor", color="white", linewidth=0.8)
        ax.tick_params(which="minor", length=0)

        # 标注: 逐格写文本(和 seaborn 的 annot 等价)
        for i in range(n_y):
            for j in range(n_x):
                v = ann_text.iat[i, j] if ann_text is not None else matrix.iat[i, j]
                txt = fmt_cell(None if v is None else float(v))
                ax.text(j, i, txt, ha="center", va="center",
                        fontsize=10, color="black")

        cbar = fig.colorbar(im, ax=ax)
        cbar.set_label(color)

    # ---- 轴标签与标题 ----
    ax.set_xlabel(x, fontsize=12)
    ax.set_ylabel(y, fontsize=12)
    ax.set_title(
        f"参数敏感性热力图\n颜色 = {color}   格子内标注 = {annot}\n{scale_note}",
        fontsize=13, pad=14,
    )
    fig.text(0.5, 0.005,
             f"灰色/空白格 = 该参数组合无数据(通常是被跳过, 例如 短均线须<长均线)。中文字体: {font_name}",
             ha="center", fontsize=9, color="#666666")

    out_png.parent.mkdir(parents=True, exist_ok=True)
    try:
        fig.tight_layout(rect=(0, 0.03, 1, 1))
        fig.savefig(out_png, dpi=150)
    finally:
        plt.close(fig)


def draw_line_fallback(df: pd.DataFrame, vary: str, fixed: str | None,
                       metric: str, out_png: Path, font_name: str) -> None:
    """退化路径: 只有一个参数在变时, 画一维折线图(而不是让热力图报错)。

    需求明确要求: 维度不足时不要报错, 要友好提示或退化为一维折线图。
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    sub = df[[vary, metric]].dropna()
    sub = sub.groupby(vary, as_index=False)[metric].mean().sort_values(vary)

    fig, ax = plt.subplots(figsize=(max(8.0, 0.8 * len(sub) + 4.0), 5.5))
    ax.plot(sub[vary].astype(str), sub[metric], marker="o", color="#1f77b4", linewidth=1.8)
    for xi, yi in zip(sub[vary].astype(str), sub[metric]):
        ax.annotate(f"{yi:.2f}", (xi, yi), textcoords="offset points",
                    xytext=(0, 8), ha="center", fontsize=10)
    ax.axhline(0, color="#bbbbbb", linewidth=1.0, linestyle="--")
    ax.set_xlabel(vary, fontsize=12)
    ax.set_ylabel(metric, fontsize=12)
    title_extra = f"(另一轴 {fixed} 没有变化, 已退化为一维折线图)" if fixed else \
        "(只有一个维度在变化, 已退化为一维折线图)"
    ax.set_title(f"参数敏感性(一维)  指标 = {metric}\n{title_extra}", fontsize=13, pad=12)
    ax.grid(True, linestyle="--", alpha=0.35)

    out_png.parent.mkdir(parents=True, exist_ok=True)
    try:
        fig.tight_layout()
        fig.savefig(out_png, dpi=150)
    finally:
        plt.close(fig)


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="参数敏感性热力图(读取 run_sensitivity.py 产出的 CSV)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--csv", default=str(DEFAULT_CSV),
                   help="输入的 CSV 路径")
    p.add_argument("--x", default="short_ma", help="X 轴字段名(参数)")
    p.add_argument("--y", default="long_ma", help="Y 轴字段名(参数)")
    p.add_argument("--color", default="total_return",
                   help="热力图颜色代表的指标(如 total_return/max_drawdown/sharpe/calmar)")
    p.add_argument("--annot", default="max_drawdown",
                   help="单元格内标注的指标(如 max_drawdown/total_return/sharpe)")
    p.add_argument("--out", default=str(DEFAULT_OUT), help="输出图片路径")
    p.add_argument("--center", type=float, default=None,
                   help="色标中心值(默认自动: 回撤类以 0 为上限, 收益类关于 0 对称)")
    p.add_argument("--no-seaborn", action="store_true",
                   help="强制不用 seaborn, 走 matplotlib 手绘路径")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    try:
        import matplotlib
        matplotlib.use("Agg")
        font_name = setup_chinese_font()
    except ImportError:
        print("[错误] 未安装 matplotlib, 请执行: pip install matplotlib")
        return 1

    csv_path = Path(args.csv)
    out_png = Path(args.out)

    # ---- 1. 读数据 ----
    try:
        df = load_csv(csv_path)
        df, na_count = coerce_numeric(df)
    except (FileNotFoundError, ValueError) as exc:
        print(f"[错误] {exc}")
        return 1
    except Exception as exc:
        print(f"[错误] 读取 CSV 失败: {type(exc).__name__}: {exc}")
        return 1

    print("=" * 74)
    print("参数敏感性热力图")
    print("=" * 74)
    print(f"  输入 CSV   : {csv_path}")
    print(f"  数据规模   : {len(df)} 行, {len(df.columns)} 列")
    if na_count:
        print(f"  空值处理   : 有 {na_count} 个非数值单元格被转为 NaN(不会影响绘图)")
    print(f"  轴与指标   : X={args.x}  Y={args.y}  颜色={args.color}  标注={args.annot}")

    # ---- 2. 列检查: 一次把缺的列都告诉用户, 并列出实际可用的列 ----
    missing = check_columns(df, [args.x, args.y, args.color, args.annot])
    if missing:
        print(f"\n[错误] CSV 中缺少这些列: {missing}")
        print(f"       实际可用列: {list(df.columns)}")
        # 给出可操作的建议: 判断是不是"双均线跑出来却想画单均线的轴"
        if args.x in ("ma",) and "short_ma" in df.columns:
            blank = df["ma"].isna().all() if "ma" in df.columns else True
            if blank:
                print("       提示: 当前 CSV 是【双均线】扫描的结果, ma 列为空;")
                print("             双均线请用 --x short_ma --y long_ma")
        if args.x in ("short_ma", "long_ma") and "short_ma" in df.columns:
            if df["short_ma"].isna().all():
                print("       提示: 当前 CSV 是【单均线】扫描的结果, short_ma 列为空;")
                print("             单均线请用 --x ma --y stop_loss 之类")
        return 1

    print(f"  中文字体   : {font_name}")

    # ---- 3. 透视成矩阵 ----
    matrix, dropped = build_matrix(df, args.x, args.y, args.color)
    if dropped:
        print(f"  [提示] 有 {dropped} 行因为 {args.x}/{args.y} 为空被跳过(通常是另一模式的空列)")
    if matrix.empty:
        print(f"\n[错误] 按 X={args.x} / Y={args.y} 透视后没有任何有效数据。")
        # 给出针对性诊断: 这两列是否存在、是否整列为空、CSV 属于哪种模式
        for col in (args.x, args.y):
            if col not in df.columns:
                print(f"       - 列 {col!r} 在 CSV 里不存在")
            elif df[col].isna().all():
                print(f"       - 列 {col!r} 存在但【整列为空】")
            else:
                print(f"       - 列 {col!r} 有 {int(df[col].notna().sum())} 个非空值")
        modes = [str(m) for m in df.get("mode", pd.Series(dtype=str)).dropna().unique()]
        if modes:
            print(f"       本 CSV 的模式(mode 列) = {modes}")
            if set(modes) == {"dual"}:
                print("       诊断: 这是【双均线】扫描的结果, 请用 --x short_ma --y long_ma;")
                print("             若想画 ma x stop_loss, 需要先用单均线模式重跑:")
                print("             python run_sensitivity.py --ma 15,20,25 --stop_loss 0.08,0.15,0.25")
            elif set(modes) == {"single"}:
                print("       诊断: 这是【单均线】扫描的结果, 请用 --x ma / --y stop_loss;")
                print("             双均线请用 --x short_ma --y long_ma")
        return 1

    annot_matrix = None
    if args.annot != args.color:
        annot_matrix, _ = build_matrix(df, args.x, args.y, args.annot)
    else:
        annot_matrix = matrix      # 颜色和标注同一个指标时, 直接复用

    n_x = matrix.shape[1]
    n_y = matrix.shape[0]
    print(f"  矩阵规模   : {n_y} 行(Y={args.y}) × {n_x} 列(X={args.x})")

    # ---- 4. 维度不足 -> 退化为一维折线图 ----
    # 判断谁在变、谁不变:
    #   X 只有 1 个取值 -> X 是不变的那个, 变化轴是 Y  -> 横轴画 Y
    #   Y 只有 1 个取值 -> Y 是不变的那个, 变化轴是 X  -> 横轴画 X
    # (早期版本这里把两个变量写反了, 导致提示里的"只有4个取值"和"横轴=mode"
    #  自相矛盾 —— 明明 short_ma 有 4 个值才对吧。)
    if n_x < 2 or n_y < 2:
        if n_x < 2:
            vary, fixed = args.y, args.x
            fixed_vals = list(matrix.columns)      # X 的取值(不变的)
            vary_vals = list(matrix.index)         # Y 的取值(在变的)
        else:
            vary, fixed = args.x, args.y
            fixed_vals = list(matrix.index)        # Y 的取值(不变的)
            vary_vals = list(matrix.columns)       # X 的取值(在变的)

        print(f"\n  [提示] {fixed} 只有 {len(fixed_vals)} 个取值 {fixed_vals}, "
              f"构不成二维网格; {vary} 有 {len(vary_vals)} 个取值 {vary_vals}。")
        print(f"         已退化为一维折线图, 横轴 = {vary}。")
        if args.color != args.annot:
            print(f"         (一维图只画颜色指标 {args.color}; "
                  f"标注指标 {args.annot} 本图不显示)")
        try:
            draw_line_fallback(df, vary, fixed, args.color, out_png, font_name)
        except Exception as exc:
            print(f"\n[错误] 绘图失败: {type(exc).__name__}: {exc}")
            return 1
        print(f"\n图片已保存: {out_png}")
        print("=" * 74)
        return 0

    # ---- 5. 画二维热力图 ----
    center, scale_note = resolve_color_scale(matrix, args.color, args.center)
    print(f"  配色方向   : {scale_note}")

    use_seaborn = not args.no_seaborn
    if use_seaborn:
        try:
            import seaborn  # noqa: F401
            print(f"  绘图后端   : seaborn {seaborn.__version__}")
        except ImportError:
            use_seaborn = False
            print("  绘图后端   : matplotlib 手绘(未安装 seaborn; 想用 seaborn 可执行")
            print("               pip install seaborn, 两条路径视觉效果基本一致)")

    # 打印矩阵, 方便在终端里也能直接看数
    print("-" * 74)
    print(f"矩阵内容(行={args.y}, 列={args.x}, 值={args.color}):")
    with pd.option_context("display.width", 200, "display.max_columns", None):
        print(matrix.round(2).to_string())
    print("-" * 74)

    try:
        draw_heatmap(matrix, args.x, args.y, args.color, args.annot,
                     annot_matrix, out_png, center, scale_note,
                     font_name, use_seaborn)
    except Exception as exc:
        print(f"\n[错误] 绘图失败: {type(exc).__name__}: {exc}")
        import traceback
        traceback.print_exc()
        return 1

    # ---- 6. 稳健性判断: 真·区分"参数平原"还是"参数尖峰" ----
    #
    # 这里刻意【不用】"好格子占前 40% 的比例"这种指标 —— 那是按分位数数出来的,
    # 结果恒等于 40%, 无论数据是平原还是尖峰都会给出同一个结论, 属于没有信息量的假指标。
    #
    # 真正能区分的判据是【邻域一致性】:
    #   最优格子周围紧邻的格子, 是否也表现不错?
    #     - 邻域都不错  -> 参数平原(好结果连片, 换参数不容易翻车)
    #     - 邻域明显变差 -> 参数尖峰(孤立最优, 高度可疑, 很可能是过拟合)
    # 同时报告"全网格极差"和"非负格子占比", 作为辅助参考。
    vals = matrix.to_numpy(dtype="float64")
    finite = vals[np.isfinite(vals)]
    if finite.size:
        is_dd = args.color.lower() in DRAWDOWN_METRICS
        # 回撤是负数, 越接近 0 越好 -> 取最大; 收益类也是取最大。
        # (两者方向一致: 都是"数值越大越好", 因为回撤 -5% > -30%。)
        best_pos = np.unravel_index(np.nanargmax(vals), vals.shape)
        best_val = float(vals[best_pos])

        print(f"  最优格子   : {args.y}={matrix.index[best_pos[0]]}, "
              f"{args.x}={matrix.columns[best_pos[1]]} "
              f"-> {args.color} = {best_val:.2f}"
              f"   ({'回撤越接近0越好' if is_dd else '越大越好'})")

        # ---- 邻域一致性: 好东西是否"连片" ----
        bi, bj = int(best_pos[0]), int(best_pos[1])
        neighbors = []
        for di, dj in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            ni, nj = bi + di, bj + dj
            if 0 <= ni < vals.shape[0] and 0 <= nj < vals.shape[1]:
                v = vals[ni, nj]
                if np.isfinite(v):
                    neighbors.append(float(v))

        if neighbors:
            # 邻域容差: 用整体极差的 25% 作为"算不错"的门槛
            spread_all = float(np.nanmax(vals) - np.nanmin(vals))
            tol = max(0.25 * spread_all, 1e-9)
            if is_dd:
                ok = [v for v in neighbors if v >= best_val - tol]
            else:
                ok = [v for v in neighbors if v <= best_val + tol]
            ratio = len(ok) / len(neighbors)
            if ratio >= 0.6:
                verdict = "参数平原: 最优格子周围普遍也不错, 换参数的代价较小(相对稳健)"
            elif ratio >= 0.3:
                verdict = "过渡形态: 邻域有好有坏, 稳健性一般, 建议结合样本外结果判断"
            else:
                verdict = "参数尖峰: 最优格子周围明显变差, 高度可疑(很可能过拟合)"
            print(f"  稳健性判断 : {verdict}")
            print(f"               最优格子的 {len(neighbors)} 个邻居里, {len(ok)} 个也算不错"
                  f" (邻域一致率 {ratio:.0%}, 容差 ±{tol:.2f})")
        else:
            print("  稳健性判断 : 最优格子没有有效邻居, 无法判断(网格太小)")

        # ---- 辅助参考 ----
        spread = float(finite.max() - finite.min())
        nonneg = int(np.sum(finite >= 0))
        print(f"  辅助参考   : 全网格极差 {spread:.2f} "
              f"(越大说明越依赖参数选择); {args.color}>=0 的格子 {nonneg}/{finite.size}")
        if np.isnan(vals).any():
            print(f"  [注意] 矩阵里有 {int(np.isnan(vals).sum())} 个空格子"
                  f"(代表该参数组合没有数据, 不是表现差)")

    print(f"\n图片已保存: {out_png}")
    print("=" * 74)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
