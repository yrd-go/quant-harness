"""

quant_demo.py — A股日线数据抓取与收盘价可视化示例

功能:
    1. 日期不再写死: 用 datetime 取"今天", 往前推 60 个自然日作为起点
    2. 自适应补足交易日: 若拉到的交易日不足 40 个(遇长假/周末偏多),
       自动把窗口放宽到 90 天、120 天重试(最多 2 次), 保证能拿到 40 个交易日
    3. 截取最近 40 个交易日: 清洗后用 .tail(40)
    4. 打印这 40 行的前 5 行数据
    5. 用这 40 个交易日画收盘价折线图, 保存为 price.png
    6. 全程 try-except 异常捕获, 网络失败时给出友好提示
       (东方财富 + 腾讯双源容灾、中文字体缓存、tqdm 屏蔽等逻辑均保留)

为什么是按自然日推, 而不是直接按交易日推:
    akshare 的行情接口只接受日期区间, 不提供"最近 N 个交易日"这种参数,
    所以只能先用自然日放宽窗口, 拉到数据后再按行数截取。
    因为拉取范围始终 >= 40 个交易日, .tail(40) 的结果恒等于
    "最近 40 个交易日", 先放宽再截取不会引入偏差。

运行:
    python quant_demo.py
    python quant_demo.py --start 2026-08-01 --end 2026-10-01
    python quant_demo.py --days 40 --lookback 90

依赖:
    pip install akshare pandas matplotlib

"""

from __future__ import annotations

import argparse
import contextlib
import os
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

# 统一路径配置(集中放在项目根目录的 config.py 里)。
# 这里显式把 config.py 所在目录(项目根)加入 sys.path, 避免裸 import 依赖
# "当前工作目录", 同时也兼容这些脚本以后被挪进子目录的情况。
_CONFIG_DIR = Path(__file__).resolve().parent
while not (_CONFIG_DIR / "config.py").exists() and _CONFIG_DIR != _CONFIG_DIR.parent:
    _CONFIG_DIR = _CONFIG_DIR.parent
if str(_CONFIG_DIR) not in sys.path:
    sys.path.insert(0, str(_CONFIG_DIR))

from config import PRICE_PNG  # noqa: E402  (必须在 sys.path 调整之后导入)


# --------------------------------------------------------------------------- #
# 配置区: 想换股票 / 换时间, 只改这里
# --------------------------------------------------------------------------- #
SYMBOL = "000001"           # 平安银行
SYMBOL_NAME = "平安银行"
ADJUST = "qfq"              # qfq = 前复权, hfq = 后复权, "" = 不复权

# ---- 日期与窗口(不再写死具体日期, 由 datetime 动态计算) ----
LOOKBACK_CALENDAR_DAYS = 60   # 初始: 从今天往前推 60 个自然日
KEEP_TRADING_DAYS = 40        # 最终要保留的交易日数(.tail)
WIDEN_STEP_DAYS = 30          # 每次放宽的自然日步长 -> 60 / 90 / 120
MAX_WIDEN_TIMES = 2           # 最多放宽 2 次, 合计 3 次尝试

# 若某天 akshare 改了接口日期格式, 把这个开关打开即可切换成 "YYYY-MM-DD"
USE_DASHED_DATE = False


def to_dashed(date_str: str) -> str:
    """'20220101' -> '2022-01-01'"""
    return f"{date_str[0:4]}-{date_str[4:6]}-{date_str[6:8]}"


def format_date(value: "date | str") -> str:
    """统一把 date / 'YYYYMMDD' / 'YYYY-MM-DD' / Timestamp 显示成 YYYY-MM-DD"""
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")
    ts = pd.to_datetime(value, errors="coerce")
    if ts is None or pd.isna(ts):
        return str(value)
    return ts.strftime("%Y-%m-%d")


def parse_cli_date(value: str) -> date:
    """解析命令行传入的日期, 支持 '2026-08-01' 和 '20260801'。"""
    ts = pd.to_datetime(value, errors="coerce")
    if ts is None or pd.isna(ts):
        raise argparse.ArgumentTypeError(
            f"日期格式无法识别: {value!r}, 请使用 2026-08-01 或 20260801"
        )
    return ts.date()


def resolve_window(start_arg: str | None, end_arg: str | None,
                   lookback: int) -> tuple[date, date, date]:
    """计算拉取窗口。

    返回 (窗口起始日, 窗口结束日, 今天)。
    - 未指定 --start/--end 时: 结束日 = 今天, 起始日 = 今天 - lookback 个自然日
    - 指定了 --start/--end 时: 完全按用户给的来, 不做自适应放宽
    """
    today = date.today()

    end = parse_cli_date(end_arg) if end_arg else today
    start = parse_cli_date(start_arg) if start_arg else (end - timedelta(days=lookback))

    if start > end:
        raise ValueError(f"起始日期 {format_date(start)} 晚于结束日期 {format_date(end)}")

    return start, end, today


def fetch_hist_adaptive(ak, symbol: str, base_start: date, end: date,
                        adjust: str, keep: int, max_widen: int,
                        step_days: int, allow_widen: bool = True
                        ) -> tuple[pd.DataFrame, date, int]:
    """带自适应放宽的行情拉取。

    先用 base_start ~ end 拉一次; 若拿到的交易日不足 keep 个(长假、周末偏多时会这样),
    就把起点再往前推 step_days 天重拉, 最多放宽 max_widen 次。

    返回 (数据, 实际使用的起始日, 放宽次数)。
    """
    attempt_dates: list[tuple[date, bool]] = [(base_start, False)]
    for i in range(1, max_widen + 1):
        attempt_dates.append((base_start - timedelta(days=step_days * i), True))

    if not allow_widen:
        attempt_dates = attempt_dates[:1]

    last_df: pd.DataFrame | None = None
    last_start: date = base_start
    widen_used = 0

    for idx, (start, is_widened) in enumerate(attempt_dates):
        if is_widened:
            print(f"[提示] 交易日不足 {keep} 个, 自动把窗口放宽到 "
                  f"{(end - start).days} 个自然日 ({format_date(start)} 起) 重试...")
            widen_used = idx

        start_str = to_dashed(format_date(start)) if USE_DASHED_DATE else \
            format_date(start).replace("-", "")
        end_str = to_dashed(format_date(end)) if USE_DASHED_DATE else \
            format_date(end).replace("-", "")

        try:
            df = fetch_hist(ak, symbol, start_str, end_str, adjust)
        except Exception:
            # 拉取异常完全交给上层统一给出中文排查建议, 这里不吞掉
            raise

        df = df.copy()
        df["日期"] = pd.to_datetime(df["日期"], errors="coerce")
        df = df.dropna(subset=["日期"]).sort_values("日期").reset_index(drop=True)

        last_df, last_start = df, start
        if len(df) >= keep:
            return df, start, widen_used

        print(f"[提示] 本次只取到 {len(df)} 个交易日, 还不够 {keep} 个。")

    assert last_df is not None
    print(f"[警告] 已尝试 {len(attempt_dates)} 次, 最多只拿到 {len(last_df)} 个交易日。")
    print(f"       可能原因: 该标的上市时间较短, 或数据源本身历史数据不全。")
    print(f"       将继续用现有数据出图, 但行数少于 {keep} 行。")
    return last_df, last_start, widen_used


def print_head(df: pd.DataFrame, symbol_name: str, n: int = 5) -> None:
    """打印前 n 行数据"""
    span_start = format_date(df["日期"].min())
    span_end = format_date(df["日期"].max())
    print("=" * 72)
    print(f"{symbol_name} 最近 {len(df)} 个交易日中的前 {n} 行 "
          f"(区间 {span_start} ~ {span_end})")
    print("=" * 72)

    # 关掉 pandas 的列宽截断, 让终端里也能看全
    with pd.option_context(
        "display.max_columns", None,
        "display.width", 200,
        "display.unicode.east_asian_width", True,
    ):
        print(df.head(n).to_string(index=False))
    print()


def _ensure_mpl_cache_dir() -> None:
    """确保 matplotlib 的字体缓存目录可写。

    若用户目录下的 ~/.matplotlib 不可写(受限环境/沙箱), matplotlib 会打印
    'Could not save font_manager cache ... Permission denied' 警告。
    这里提前把 MPLCONFIGDIR 指向一个确认可写的目录, 必须先于 import matplotlib。
    """
    if os.environ.get("MPLCONFIGDIR"):
        return

    default_dir = Path.home() / ".matplotlib"
    try:
        default_dir.mkdir(parents=True, exist_ok=True)
        probe = default_dir / ".write_probe"
        probe.touch()
        probe.unlink()
        return  # 默认目录可用, 不必折腾
    except Exception:
        pass

    for candidate in (Path.cwd() / ".mplconfig", Path(tempfile.gettempdir()) / "mplconfig"):
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            probe = candidate / ".write_probe"
            probe.touch()
            probe.unlink()
            os.environ["MPLCONFIGDIR"] = str(candidate)
            print(f"[提示] 默认 matplotlib 缓存目录不可写, 已改用: {candidate}")
            return
        except Exception:
            continue


def adjust_label(adjust: str) -> str:
    """把复权口径翻译成中文, 避免标题说谎。"""
    mapping = {"qfq": "前复权", "hfq": "后复权", "": "不复权"}
    return mapping.get(str(adjust).strip().lower()
                       if str(adjust).strip() else "", "复权口径未知")


def plot_close(df: pd.DataFrame, symbol_name: str, out_png: str) -> None:
    """画收盘价折线图并保存为图片。

    传入的 df 应该已经是"最近 N 个交易日"的数据, 标题按实际区间显示。
    """
    _ensure_mpl_cache_dir()

    import matplotlib
    matplotlib.use("Agg")  # 无界面环境也能存图, 必须在 pyplot 之前设置
    import matplotlib.pyplot as plt
    from matplotlib import font_manager

    # 中文字体: Windows 上优先用微软雅黑, 找不到就退回黑体
    for font_name in ("Microsoft YaHei", "SimHei", "DejaVu Sans"):
        try:
            font_manager.findfont(font_name, fallback_to_default=False)
            plt.rcParams["font.sans-serif"] = [font_name]
            break
        except Exception:
            continue
    plt.rcParams["axes.unicode_minus"] = False  # 负号正常显示

    # x 轴用日期, y 轴用收盘价
    x = pd.to_datetime(df["日期"])
    y = pd.to_numeric(df["收盘"], errors="coerce")

    fig, ax = plt.subplots(figsize=(14, 6))
    ax.plot(x, y, color="#1f77b4", linewidth=1.4, label="收盘价")
    ax.scatter(x, y, color="#1f77b4", s=12, zorder=4)  # 标出每个交易日

    # 标注区间最高 / 最低点
    if y.notna().any():
        i_max, i_min = y.idxmax(), y.idxmin()
        ax.scatter([x[i_max], x[i_min]], [y[i_max], y[i_min]],
                   color=["#d62728", "#2ca02c"], zorder=5, s=36)
        ax.annotate(f"最高 {y[i_max]:.2f}", (x[i_max], y[i_max]),
                    textcoords="offset points", xytext=(0, 10),
                    ha="center", color="#d62728", fontsize=10)
        ax.annotate(f"最低 {y[i_min]:.2f}", (x[i_min], y[i_min]),
                    textcoords="offset points", xytext=(0, -18),
                    ha="center", color="#2ca02c", fontsize=10)

    # 复权口径按数据实际情况显示, 不写死
    adjust_text = adjust_label(df["adjust"].iloc[0] if "adjust" in df.columns else ADJUST)
    ax.set_title(
        f"{symbol_name} 最近 {len(df)} 个交易日收盘价走势 "
        f"({format_date(df['日期'].min())} ~ {format_date(df['日期'].max())}, {adjust_text})",
        fontsize=14, pad=12,
    )
    ax.set_xlabel("日期")
    ax.set_ylabel("收盘价 (元)")
    ax.grid(True, linestyle="--", alpha=0.35)
    ax.legend(loc="best")
    fig.autofmt_xdate()          # 日期标签倾斜, 防止重叠
    fig.tight_layout()

    try:
        fig.savefig(out_png, dpi=150)
    finally:
        plt.close(fig)            # 无论如何都释放画布内存

    print(f"图表已保存: {out_png}")


def _with_market_prefix(symbol: str) -> str:
    """'000001' -> 'sz000001' (6 开头是沪市, 其余按深市处理)"""
    return ("sh" if symbol.startswith(("6", "9")) else "sz") + symbol


@contextlib.contextmanager
def _quiet_tqdm():
    """临时屏蔽 tqdm 进度条。

    akshare 内部写的是 ``from akshare.utils.tqdm import get_tqdm``, 再 ``tqdm = get_tqdm()``,
    所以只改 akshare.utils.tqdm 没用 —— 必须改【调用方模块】命名空间里那个引用,
    而且替换品必须仍是 "返回迭代器" 的工厂函数, 不能直接塞一个迭代器。
    进度条只是观感问题, 这里的任何失败都不应该影响主流程。
    """
    patched: list[tuple[object, str, object]] = []
    try:
        from akshare.utils import tqdm as _ak_tqdm
        from akshare.stock_feature import stock_hist_tx as _mod

        def _get_tqdm_disabled(enable: bool = True):
            """替换掉 akshare 的 get_tqdm(), 返回一个不做任何显示的恒等迭代器。"""
            _ = enable  # 无论调用方传什么, 一律关掉
            return lambda iterable, *args, **kwargs: iterable

        targets = [
            (_mod, "get_tqdm"),
            (_ak_tqdm, "get_tqdm"),
        ]
        for mod, attr in targets:
            if hasattr(mod, attr):
                original = getattr(mod, attr)
                setattr(mod, attr, _get_tqdm_disabled)
                patched.append((mod, attr, original))
    except Exception:
        patched = []  # 环境不同就算了, 进度条纯属观感问题

    try:
        yield
    finally:
        for mod, attr, original in patched:
            try:
                setattr(mod, attr, original)
            except Exception:
                pass


def fetch_from_eastmoney(ak, symbol: str, start: str, end: str, adjust: str) -> pd.DataFrame:
    """数据源一: 东方财富(字段为中文, 数据最全)"""
    return ak.stock_zh_a_hist(
        symbol=symbol,
        period="daily",
        start_date=start,
        end_date=end,
        adjust=adjust,
    )


def fetch_from_tencent(ak, symbol: str, start: str, end: str, adjust: str) -> pd.DataFrame:
    """数据源二: 腾讯(备用)。字段是英文, 统一改回中文列名以便下游共用。

    注意: 腾讯的 turnover 是小数(0.0060 = 0.60%), 东方财富的 "换手率" 是百分数,
    两个源的量纲不同, 所以这里保留原列名 turnover 不做映射, 免得误导。
    """
    with _quiet_tqdm():
        df = ak.stock_zh_a_hist_tx(
            symbol=_with_market_prefix(symbol),
            start_date=start,
            end_date=end,
            adjust=adjust,
        )
    return df.rename(columns={
        "date": "日期", "open": "开盘", "close": "收盘",
        "high": "最高", "low": "最低", "volume": "成交量", "amount": "成交额",
    })


def fetch_hist(ak, symbol: str, start: str, end: str, adjust: str) -> pd.DataFrame:
    """按顺序尝试多个数据源, 前一个失败就换下一个。

    东方财富的行情接口偶发 RemoteDisconnected(限流/抽风), 所以准备了腾讯做兜底。
    """
    sources = [
        ("东方财富", fetch_from_eastmoney),
        ("腾讯", fetch_from_tencent),
    ]
    errors: list[str] = []

    for name, func in sources:
        try:
            df = func(ak, symbol, start, end, adjust)
        except Exception as exc:
            msg = f"{name}: {type(exc).__name__}: {exc}"
            errors.append(msg)
            print(f"[警告] 数据源 {name} 拉取失败, 尝试下一个... ({type(exc).__name__})")
            continue

        if df is not None and not df.empty:
            if name != "东方财富":
                print(f"[提示] 已自动切换到备用数据源: {name}")
            return df
        errors.append(f"{name}: 返回空数据")

    raise RuntimeError("所有数据源均失败 -> " + " | ".join(errors))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="抓取A股最近N个交易日行情并画出收盘价曲线")
    p.add_argument("--start", default=None,
                   help="起始日期(如 2026-08-01)。默认 = 结束日往前推 --lookback 个自然日")
    p.add_argument("--end", default=None,
                   help="结束日期(如 2026-10-01)。默认 = 今天")
    p.add_argument("--days", type=int, default=KEEP_TRADING_DAYS,
                   help=f"要保留的交易日数(默认 {KEEP_TRADING_DAYS})")
    p.add_argument("--lookback", type=int, default=LOOKBACK_CALENDAR_DAYS,
                   help=f"初始自然日窗口(默认 {LOOKBACK_CALENDAR_DAYS})")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if args.days < 1:
        print(f"[错误] --days 必须 >= 1, 当前为 {args.days}")
        return 2
    if args.lookback < 1:
        print(f"[错误] --lookback 必须 >= 1, 当前为 {args.lookback}")
        return 2

    # ---- 1. 导入 akshare(单独捕获, 提示如何安装) ----
    try:
        import akshare as ak
    except ImportError:
        print("[错误] 未安装 akshare, 请先执行:  pip install akshare")
        return 1

    # ---- 2. 动态计算日期窗口 ----
    try:
        start, end, today = resolve_window(args.start, args.end, args.lookback)
    except (ValueError, argparse.ArgumentTypeError) as exc:
        print(f"[错误] 日期参数有误: {exc}")
        return 2

    print("=" * 72)
    print(f"今天: {format_date(today)}")
    print(f"目标: {SYMBOL_NAME}({SYMBOL}) 最近 {args.days} 个交易日 "
          f"({adjust_label(ADJUST)})")
    print(f"初始窗口: {format_date(start)} ~ {format_date(end)} "
          f"({(end - start).days} 个自然日)")
    print("=" * 72)

    # ---- 3. 拉取数据(不足则自适应放宽窗口) ----
    df: pd.DataFrame | None = None
    try:
        df, used_start, widen_used = fetch_hist_adaptive(
            ak, SYMBOL, start, end, ADJUST,
            keep=args.days,
            max_widen=MAX_WIDEN_TIMES,
            step_days=WIDEN_STEP_DAYS,
            allow_widen=(args.start is None),  # 用户自己指定了起点就不擅自放宽
        )
    except Exception as exc:  # 网络 / 接口变更 / 日期格式等任何异常
        print("\n[错误] 获取行情数据失败。可能的原因和排查建议:")
        print(f"       具体报错: {type(exc).__name__}: {exc}")
        print("       1) 网络问题: 检查能否访问 push2his.eastmoney.com, 公司代理/防火墙是否拦截")
        print("       2) 接口或日期格式变更: 把脚本顶部 USE_DASHED_DATE 改成 True 再试")
        print("       3) akshare 版本过旧: 执行  pip install -U akshare")
        print("       4) 数据源临时抽风: 稍等几分钟后重跑")
        return 1

    # ---- 4. 校验数据 ----
    if df is None or df.empty:
        print(f"\n[提示] 接口未返回任何数据。请确认代码 {SYMBOL} 是否正确, "
              f"以及 {format_date(start)} ~ {format_date(end)} 是否落在有效交易日区间内。")
        return 1

    missing = [c for c in ("日期", "收盘") if c not in df.columns]
    if missing:
        print(f"\n[错误] 返回数据缺少字段 {missing}, 实际字段为: {list(df.columns)}")
        print("       可能是 akshare 接口字段改名, 请升级 akshare 后重试。")
        return 1

    # 数据已在 fetch_hist_adaptive 里清洗过, 这里只做兜底检查
    if df["日期"].isna().all():
        print("\n[提示] 清洗后没有可用数据。")
        return 1

    fetched_rows = len(df)
    if widen_used:
        print(f"[提示] 自适应放宽生效: 实际使用窗口 {format_date(used_start)} ~ "
              f"{format_date(end)} (放宽 {widen_used} 次)。")
    print(f"[信息] 实际拉取到 {fetched_rows} 个交易日 "
          f"({format_date(df['日期'].min())} ~ {format_date(df['日期'].max())})。")

    # ---- 5. 只保留最近 N 个交易日 ----
    recent = df.tail(args.days).reset_index(drop=True)
    if len(recent) < args.days:
        print(f"[警告] 只保留到 {len(recent)} 行, 少于目标的 {args.days} 行, "
              f"下图按现有数据绘制。")

    print(f"[信息] 已保留最近 {len(recent)} 个交易日: "
          f"{format_date(recent['日期'].min())} ~ {format_date(recent['日期'].max())}")
    print()

    # ---- 6. 打印前 5 行 ----
    print_head(recent, SYMBOL_NAME, n=5)

    # ---- 7. 画图 ----
    try:
        plot_close(recent, SYMBOL_NAME, PRICE_PNG)
    except Exception as exc:
        print(f"\n[错误] 绘图失败: {type(exc).__name__}: {exc}")
        print("       数据已成功获取并打印, 仅出图环节出错, 可单独排查 matplotlib。")
        return 1

    print(f"\n完成。共 {len(recent)} 个交易日, 区间 "
          f"{format_date(recent['日期'].min())} ~ {format_date(recent['日期'].max())}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
