#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
backtest.py — 基于 backtrader 的月度动量轮动回测 (含三重风控优化 + 参数化)

策略一句话说明
--------------
每月第一个交易日调仓: 用过去 N 个交易日的收益率(动量)给 4 只个股排序,
买入最强的 top_n 只, 等权持有。整个回测期间尽量满仓, 但叠加三层保护:

    模块一 大盘择时"保险丝" : 基准(510300)走弱 -> 当月空仓避险。支持两种口径:
                              单均线: 前一日收盘 < MA          (--ma)
                              双均线: 前一日短均线 < 前一日长均线 (--short_ma/--long_ma)
    模块二 缓冲带降换手     : 只有跌出动量前 sell_threshold 名才卖, 避免来回打脸
    模块三 个股止损         : 单只亏损达 stop_loss 无条件清仓, 且当月禁止再买回

所有核心参数都可以从命令行覆盖, 详见 `python backtest.py --help`。
这是为参数敏感性测试(parameter sensitivity)和样本外测试(OOS)准备的:
同一套逻辑跑不同参数, 看结论是否稳健, 而不是只挑一组好看的参数。

标的池
------
交易标的(4 只个股) : 600519 贵州茅台 / 600036 招商银行 / 300750 宁德时代 / 601318 中国平安
基准标的(不交易)   : 510300 沪深300ETF, 既用来画对比净值, 也用来做大盘择时信号

关于「前视偏差」——本脚本最重要的正确性保证
------------------------------------------
前视偏差指的是: 用了当时还不可能知道的信息去做交易决策。衡量标准只有一条 ——
**决策用到的数据, 在决策发生的那一刻是否已经真实可得**。

本脚本的做法是:
  1. 在每根 K 线的 next() 里, 只用【当前及更早】的数据做判断, 不碰任何未来数据;
  2. 算完后提交市价单, backtrader 的市价单在【下一根 K 线的开盘】成交。

第 2 点不是我的假设, 而是实测结论。实测记录(用 6 根构造K线做的对照实验):
    在 2024-01-02 那根K线(close=11.0)里调用 buy()
    -> 成交发生在 2024-01-03, 成交价 = 11.5 = 2024-01-03 的 open
也就是说, 信号来自 t 日收盘, 成交发生在 t+1 日开盘, 中间隔了一整根K线,
"用收盘信号按同一根收盘价成交"这种典型的前视偏差在框架层面就不可能发生。

具体到三个模块:
  - 动量: close[0] / close[-window] - 1, 全部是当前与历史收盘价;
  - 大盘择时: 一律用【前一交易日】的均线值, 即下标 [-1]:
              单均线 -> close[-1] vs market_ma[-1]
              双均线 -> short_ma[-1] vs long_ma[-1]
              用前一交易日而不是当日, 是因为本月调仓决策发生在当日收盘时,
              而当日均线要用当日收盘价才算得出来 —— 用 [0] 就等于用"还没确认"的数据。
              注意边界处理: 均线要 period 根K线才能成形, 再往前取一天,
              所以门槛是 len >= period + 1; 数据不足时视为"安全、不避险"(需求明确要求);
  - 止损: 用当前收盘价对比持仓成本价(position.price, backtrader 记录的成交均价),
    判断后下单, 成交同样发生在次日开盘。所以止损实际成交价可能比设定值更差,
    这一点在报告里会如实说明 —— 这是真实交易的必然结果, 不是 bug。

关于滑点(slippage)
------------------
滑点是在"理论成交价"上人为加的一个不利偏移, 用来模拟真实交易中的冲击成本与买卖价差。
要求是千分之一(0.1%), 所以用 set_slippage_perc(0.001)。

有一个 backtrader 的坑必须显式处理: 对市价单(market order), 滑点默认【不作用于开盘价】
(slip_open=False)。原因是 backtrader 认为市价单按开盘价成交已经不差了, 不想再额外惩罚。
但这会让"千分之一滑点"实际上只作用于 high/low 区间, 等于滑点被悄悄削弱。
所以这里显式传 slip_open=True, 强制滑点也作用在开盘价上, 让 0.1% 真实生效。

佣金: 万分之三(0.0003), 买卖双向都收, 用 setcommission(commission=0.0003)。

关于基准对比
------------
基准 = 510300 的"买入并持有": 把期初资金按第一天收盘价全额买入, 一直拿到最后一天。
基准【不含】手续费与滑点, 因为被动持有不产生交易。所以基准略微"占便宜",
超额收益的解读要留这个余量。

关于夏普比率的计算口径(明确写出来, 免得日后误解)
------------------------------------------------
    日收益率 r_t = 净值_t / 净值_{t-1} - 1
    夏普 = (mean(r) - 无风险日利率) / std(r) * sqrt(252)
其中:
  - 无风险利率设为 0(题目要求), 所以分子就是 mean(r);
  - sqrt(252) 是年化因子, 252 是A股一年的标准交易日数;
  - std 用的是样本标准差(pandas 默认 ddof=1);
  - 这里【没有】扣掉无风险利率, 也【没有】做超额收益的再投资处理 —— 即标准教科书口径。
最大回撤则基于每日净值序列: 对每个点算 (净值/历史最高净值 - 1), 取最小值。

关于输出图片的「防覆盖」
------------------------
图片文件名带上了关键参数后缀, 避免多次运行互相覆盖:
    单均线: backtest_result_ma20_stop15_th4.png
    双均线: backtest_result_sma10_lma30_stop15_th4.png
双均线用 sma/lma 前缀而不是复用 ma, 一来两个周期都能进文件名、一眼看清是哪组,
二来单均线模式的历史文件名保持不变(不会让你已有的那批图突然对不上)。
后缀包含 择时参数 / stop_loss / sell_threshold 三项; 若你要对比的是 top_n 或
momentum_window, 用 --name 加个自定义标签区分(见下)。

运行
----
    python backtest.py                          # 默认参数(单均线 MA20)
    python backtest.py --quiet                  # 只打印报告
    # ---- 单均线模式(向后兼容) ----
    python backtest.py --ma 15                  # 大盘均线改成 15 日
    # ---- 双均线交叉模式(传了 short/long 就自动启用) ----
    python backtest.py --short_ma 10 --long_ma 30
    python backtest.py --short_ma 5 --long_ma 20
    # ---- 其它 ----
    python backtest.py --stop_loss 0.10         # 止损改成 10%
    python backtest.py --sell_threshold 2       # 缓冲带关掉(退回"跌出前2名就卖")
    python backtest.py --no-market-filter       # 关掉大盘择时
    python backtest.py --no-stop-loss           # 关掉个股止损
    python backtest.py --start 2021-01-01 --end 2023-12-31   # 样本内
    python backtest.py --start 2024-01-01 --end 2026-09-30   # 样本外(OOS)
"""

from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path

# --------------------------------------------------------------------------- #
# 统一路径引导: 从当前目录逐级向上找 config.py, 找到后加入 sys.path。
# 这样无论从项目根目录、还是从 src/ 目录启动, 都能导入到同一份 config。
# --------------------------------------------------------------------------- #
_CONFIG_DIR = Path(__file__).resolve().parent
while not (_CONFIG_DIR / "config.py").exists() and _CONFIG_DIR != _CONFIG_DIR.parent:
    _CONFIG_DIR = _CONFIG_DIR.parent
if str(_CONFIG_DIR) not in sys.path:
    sys.path.insert(0, str(_CONFIG_DIR))

from config import DB_FILE, BACKTEST_PNG  # noqa: E402  (必须在 sys.path 调整之后导入)

import backtrader as bt  # noqa: E402
import pandas as pd  # noqa: E402

log = logging.getLogger("backtest")


# --------------------------------------------------------------------------- #
# 默认参数(集中在这里; 命令行没给的值就用这些)
# --------------------------------------------------------------------------- #
STOCK_POOL = ["600519", "600036", "300750", "601318"]   # 参与交易的 4 只个股
BENCHMARK = "510300"                                     # 基准 + 大盘择时信号源

DEFAULT_START = "2021-01-01"      # 回测默认起点(数据库实际从 2021-01-04 开始)
DEFAULT_END = "2026-09-30"        # 回测默认终点
DEFAULT_MA = 20                   # 单均线模式的择时均线周期(兼容旧用法)
DEFAULT_SHORT_MA = 10             # 双均线模式: 短均线周期
DEFAULT_LONG_MA = 30              # 双均线模式: 长均线周期
DEFAULT_STOP_LOSS = 0.15          # 个股止损线: 亏损 15% 清仓
DEFAULT_SELL_THRESHOLD = 4        # 缓冲带: 跌出前 4 名才卖
DEFAULT_MOMENTUM_WINDOW = 20      # 动量窗口
DEFAULT_TOP_N = 2                 # 每月买入动量最强的 2 只

INITIAL_CASH = 100_000.0      # 初始资金 10 万元
COMMISSION = 0.0003           # 手续费万分之三
SLIPPAGE_PCT = 0.001          # 滑点千分之一
CASH_BUFFER = 0.02            # 买入时保留 2% 现金做手续费缓冲
LOT_SIZE = 100                # A股一手 = 100 股, 买入数量向下取整到 100 的整数倍
TRADING_DAYS_PER_YEAR = 252   # 年化因子


# --------------------------------------------------------------------------- #
# 日志
# --------------------------------------------------------------------------- #
def setup_logging(verbose: bool) -> None:
    """verbose=False 时只保留 WARNING 以上, 避免每笔订单刷屏。"""
    log.setLevel(logging.DEBUG)
    log.handlers.clear()
    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(logging.DEBUG if verbose else logging.WARNING)
    handler.setFormatter(logging.Formatter("%(message)s"))
    log.addHandler(handler)


# --------------------------------------------------------------------------- #
# 参数校验与输出文件名
# --------------------------------------------------------------------------- #
def validate_params(args: argparse.Namespace) -> tuple[list[str], list[str]]:
    """集中校验命令行参数, 返回 (错误列表, 警告列表)。

    把校验集中在一个函数里, 好处是: 一次把所有问题都告诉用户, 而不是改一个报一个。
    """
    errors: list[str] = []
    warnings: list[str] = []

    # ---- 日期 ----
    start_dt = end_dt = None
    try:
        start_dt = pd.to_datetime(args.start, errors="coerce")
        if start_dt is None or pd.isna(start_dt):
            errors.append(f"--start 日期格式无法识别: {args.start!r} (正确写法如 2021-01-01)")
    except Exception:
        errors.append(f"--start 日期格式无法识别: {args.start!r} (正确写法如 2021-01-01)")

    try:
        end_dt = pd.to_datetime(args.end, errors="coerce")
        if end_dt is None or pd.isna(end_dt):
            errors.append(f"--end 日期格式无法识别: {args.end!r} (正确写法如 2026-09-30)")
    except Exception:
        errors.append(f"--end 日期格式无法识别: {args.end!r} (正确写法如 2026-09-30)")

    if start_dt is not None and end_dt is not None and not pd.isna(start_dt) \
            and not pd.isna(end_dt) and start_dt >= end_dt:
        errors.append(f"--start({args.start}) 必须早于 --end({args.end})")

    # ---- 数值范围 ----
    if not (0 < args.stop_loss < 1):
        errors.append(
            f"--stop_loss 必须在 0 和 1 之间(不含端点), 当前 {args.stop_loss}。\n"
            f"           例如: 想设 15% 就写 --stop_loss 0.15; 设 10% 写 --stop_loss 0.10。"
            f"\n           常见错误: 写成 15(倍) 或 1.5(超过100%)。"
        )
    if args.momentum_window < 1:
        errors.append(f"--momentum_window 必须 >= 1, 当前 {args.momentum_window}")
    if args.top_n < 1:
        errors.append(f"--top_n 必须 >= 1, 当前 {args.top_n}")
    if args.sell_threshold < 1:
        errors.append(f"--sell_threshold 必须 >= 1, 当前 {args.sell_threshold}")
    if args.cash <= 0:
        errors.append(f"--cash 必须 > 0, 当前 {args.cash}")

    # ---- 择时模式: 单均线 or 双均线 ----
    # 判定规则: 只要用户显式给了 --short_ma 或 --long_ma 任意一个, 就进双均线模式。
    # 为什么用"任意一个"而不是"两个都给": 若要求两个都给, 用户只传 --short_ma 时会
    # 静默退回单均线模式, 参数看起来没生效却毫无提示 —— 这种坑最难排查。
    is_dual = bool(args.short_ma is not None or args.long_ma is not None)
    short_ma = args.short_ma if args.short_ma is not None else DEFAULT_SHORT_MA
    long_ma = args.long_ma if args.long_ma is not None else DEFAULT_LONG_MA

    if is_dual:
        # 未显式给出的那个用默认值补齐, 并提示一声, 避免"以为生效了其实用的默认值"
        if args.short_ma is None:
            warnings.append(f"只给了 --long_ma, --short_ma 取默认值 {DEFAULT_SHORT_MA}")
        if args.long_ma is None:
            warnings.append(f"只给了 --short_ma, --long_ma 取默认值 {DEFAULT_LONG_MA}")

        if short_ma < 2:
            errors.append(f"--short_ma 必须 >= 2, 当前 {short_ma}")
        if long_ma < 2:
            errors.append(f"--long_ma 必须 >= 2, 当前 {long_ma}")
        if short_ma >= long_ma:
            errors.append(
                f"--short_ma({short_ma}) 必须小于 --long_ma({long_ma}), "
                f"否则金叉/死叉的方向就反了。\n"
                f"           常见写法: --short_ma 10 --long_ma 30"
            )
        if long_ma > 250:
            warnings.append(
                f"--long_ma({long_ma}) 超过一年交易日数, 回测前期会长期处于"
                f"'长均线数据不足'状态(按规则此时视为安全、不避险)"
            )
        if args.ma is not None:
            warnings.append(
                f"同时传了 --ma({args.ma}) 和双均线参数, 按规则【以双均线为准】, "
                f"--ma 不参与本次计算"
            )
    else:
        eff_ma = args.ma if args.ma is not None else DEFAULT_MA
        if eff_ma < 2:
            errors.append(f"--ma 必须 >= 2, 当前 {eff_ma}")

    # ---- 组合逻辑(不致命, 但会让人误解结果) ----
    if args.top_n > len(STOCK_POOL):
        warnings.append(
            f"--top_n({args.top_n}) 大于股票池规模({len(STOCK_POOL)}), "
            f"实际最多只能买入 {len(STOCK_POOL)} 只"
        )
    if args.sell_threshold < args.top_n:
        warnings.append(
            f"--sell_threshold({args.sell_threshold}) 小于 --top_n({args.top_n}), "
            f"缓冲带实际不生效(等于跌出前 {args.top_n} 名就卖)"
        )
    if args.momentum_window + 1 > 250:
        warnings.append(
            f"--momentum_window({args.momentum_window}) 很大(约 "
            f"{(args.momentum_window + 1) / 21:.0f} 个月), 回测前期会被大量跳过"
        )

    return errors, warnings


def _sanitize(text: str) -> str:
    """把字符串处理成适合放进文件名的形式(去掉空格/冒号/横线等)。"""
    return "".join(ch if (ch.isalnum() or ch in "._-") else "_" for ch in str(text))


def format_stop_tag(stop_loss: float) -> str:
    """把止损比例转成文件名友好的短标签。

    0.15 -> '15' ; 0.1 -> '10' ; 0.075 -> '7.5'
    注意不能直接写 int(stop_loss * 1000)(浮点误差会把 0.15 变成 150),
    所以先用 round 消掉浮点噪声再决定要不要保留小数。
    """
    pct = round(stop_loss * 100, 4)
    if abs(pct - round(pct)) < 1e-9:
        return str(int(round(pct)))
    return f"{pct:g}".replace(".", "p")


def resolve_timing(args: argparse.Namespace) -> tuple[str, int, int | None, int | None]:
    """把命令行参数解析成具体的择时模式。

    返回 (mode, ma, short_ma, long_ma), 其中 mode ∈ {"single", "dual"}。
    双均线模式下 ma 仍会返回单均线的值(仅用于显示), 但策略里不会使用它。
    """
    is_dual = bool(args.short_ma is not None or args.long_ma is not None)
    short_ma = args.short_ma if args.short_ma is not None else DEFAULT_SHORT_MA
    long_ma = args.long_ma if args.long_ma is not None else DEFAULT_LONG_MA
    # --ma 的实际生效值: 没传就用默认 20
    eff_ma = args.ma if args.ma is not None else DEFAULT_MA
    if is_dual:
        return "dual", eff_ma, short_ma, long_ma
    return "single", eff_ma, None, None


def timing_label(args: argparse.Namespace) -> str:
    """生成择时模式的可读标签, 供终端与图片标题共用, 避免两处写法不一致。"""
    mode, ma, short_ma, long_ma = resolve_timing(args)
    if mode == "dual":
        return f"双均线 MA{short_ma}/MA{long_ma}"
    return f"单均线 MA{ma}"


def build_output_path(args: argparse.Namespace) -> Path:
    """在 config.BACKTEST_PNG 的基础上拼接动态后缀, 避免多次运行互相覆盖。

    config.BACKTEST_PNG 形如 <项目根>/output/backtest_result.png
    本函数只替换文件名部分, 目录仍然沿用 output/, 例如:
        单均线: <项目根>/output/backtest_result_ma20_stop15_th4.png
        双均线: <项目根>/output/backtest_result_sma10_lma30_stop15_th4.png

    为什么双均线要单独用 sma/lma 前缀、而不是复用 ma: 这样单均线模式的历史文件名
    保持不变(不会让已有的一批图突然对不上), 同时双均线的两个周期都能进文件名,
    一眼就能看出是哪组参数, 不会和单均线混淆。
    """
    base = Path(BACKTEST_PNG)
    stem = base.stem                      # backtest_result
    suffix = base.suffix or ".png"        # .png
    mode, ma, short_ma, long_ma = resolve_timing(args)

    if mode == "dual":
        tag = (f"_sma{_sanitize(short_ma)}_lma{_sanitize(long_ma)}"
               f"_stop{format_stop_tag(args.stop_loss)}"
               f"_th{_sanitize(args.sell_threshold)}")
    else:
        tag = (f"_ma{_sanitize(ma)}"
               f"_stop{format_stop_tag(args.stop_loss)}"
               f"_th{_sanitize(args.sell_threshold)}")

    if getattr(args, "name", None):
        tag += f"_{_sanitize(args.name)}"
    return base.with_name(f"{stem}{tag}{suffix}")


def print_run_header(args: argparse.Namespace, out_png: Path) -> None:
    """打印本次运行的全部参数。

    多组参数对比时最容易犯的错就是"忘了这张图是哪组参数跑的", 所以把参数
    强制打到报告最前面, 和图表文件名一一对应。
    """
    is_on = (lambda flag: "开启" if not flag else "关闭")
    mode, ma, short_ma, long_ma = resolve_timing(args)
    print("=" * 78)
    print("月度动量轮动策略回测 (backtrader) — 参数化版本")
    print("=" * 78)
    print("  【本次运行参数】")
    print(f"    回测区间        : {args.start} ~ {args.end}")
    print(f"    大盘择时        : {timing_label(args)}   [{is_on(args.no_market_filter)}]")
    if mode == "dual":
        print(f"                      金叉(前一日 MA{short_ma} > MA{long_ma})持仓, "
              f"死叉则当月空仓避险")
    else:
        print(f"                      前一日收盘 < MA{ma} 则当月空仓避险")
    print(f"    个股止损        : {args.stop_loss:.2%}   [{is_on(args.no_stop_loss)}]")
    print(f"    卖出缓冲带      : 跌出前 {args.sell_threshold} 名才卖")
    print(f"    动量窗口        : {args.momentum_window} 个交易日")
    print(f"    持仓数量        : top_n = {args.top_n} 只 (等权)")
    print(f"    初始资金        : {args.cash:,.0f} 元")
    print(f"    手续费 / 滑点   : {COMMISSION:.4%} / {SLIPPAGE_PCT:.2%} "
          f"(slip_open=True, 滑点作用于开盘价)")
    print(f"    图片输出        : {out_png.name}")
    print("=" * 78)


# --------------------------------------------------------------------------- #
# 从 SQLite 读取数据
# --------------------------------------------------------------------------- #
def load_price_frame(symbol: str, start: str | None = None,
                     end: str | None = None) -> pd.DataFrame:
    """从 daily_price 表读取单个标的的日线, 返回 backtrader 能吃的 DataFrame。

    返回的 DataFrame 以 trade_date 为索引, 列固定为
    open / high / low / close / volume / openinterest。

    这里做了三件事:
      1. 把 open/high/low/close/volume 强制转成 float (SQLite 里可能是字符串);
      2. 丢掉任何 OHLC 有空值的行 —— 缺价格的K线喂给 backtrader 会导致净值算错;
      3. 按日期升序排列并去掉重复日期(重复会让 backtrader 报错)。
    """
    conn = sqlite3.connect(str(DB_FILE))
    try:
        sql = ("SELECT trade_date, open, high, low, close, volume "
               "FROM daily_price WHERE symbol = ?")
        params: list[object] = [symbol]
        if start:
            sql += " AND trade_date >= ?"
            params.append(start)
        if end:
            sql += " AND trade_date <= ?"
            params.append(end)
        sql += " ORDER BY trade_date"

        df = pd.read_sql(sql, conn, params=params)
    finally:
        conn.close()

    if df.empty:
        return df

    # 类型转换: 用 to_numeric + errors='coerce', 非数字会变成 NaN, 随后被丢掉
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")

    before = len(df)
    df = df.dropna(subset=["open", "high", "low", "close"])
    dropped = before - len(df)
    if dropped:
        log.warning("  %s: 丢弃了 %d 行 OHLC 有空值的记录", symbol, dropped)

    df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce")
    df = df.dropna(subset=["trade_date"])
    df = df.sort_values("trade_date").drop_duplicates("trade_date", keep="last")

    df = df.set_index("trade_date")
    df["openinterest"] = 0.0          # backtrader 需要这一列, A股没有持仓量概念
    return df[["open", "high", "low", "close", "volume", "openinterest"]]


def load_all_frames(start: str | None, end: str | None) -> dict[str, pd.DataFrame]:
    """读取 4 只个股 + 1 只基准, 缺任何一个都直接报错(而不是悄悄少跑)。"""
    frames: dict[str, pd.DataFrame] = {}
    for symbol in STOCK_POOL + [BENCHMARK]:
        df = load_price_frame(symbol, start, end)
        if df.empty:
            raise ValueError(
                f"标的 {symbol} 在 {start} ~ {end} 区间内没有任何行情数据。\n"
                f"       请确认 data/quant_data.db 里有该标的, 或放宽 --start/--end。"
            )
        log.info("  已载入 %s: %d 个交易日 (%s ~ %s)", symbol, len(df),
                 df.index.min().date(), df.index.max().date())
        frames[symbol] = df
    return frames


def load_symbol_names() -> dict[str, str]:
    """从数据库取标的中文名, 用于日志和图表; 取不到就退回用代码当名字。"""
    names: dict[str, str] = {}
    try:
        conn = sqlite3.connect(str(DB_FILE))
        try:
            rows = conn.execute(
                "SELECT symbol, name FROM daily_price "
                "WHERE symbol IN (%s) GROUP BY symbol"
                % ",".join("?" * (len(STOCK_POOL) + 1)),
                tuple(STOCK_POOL + [BENCHMARK]),
            ).fetchall()
            names = {s: (n or s) for s, n in rows}
        finally:
            conn.close()
    except sqlite3.Error as exc:
        log.warning("读取标的中文名失败, 将使用代码代替: %s", exc)
    return names


# --------------------------------------------------------------------------- #
# 净值记录器: 每根K线结束后记一下账户总资产
# --------------------------------------------------------------------------- #
class ValueRecorder(bt.Analyzer):
    """把每根K线的账户净值存成序列, 用于画资金曲线 / 算回撤和夏普。

    为什么不用 bt.analyzers.TimeReturn: 那个按时间粒度聚合, 想拿到"每根K线的净值"
    还得反推; 直接记 getvalue() 最直观, 也不依赖 backtrader 的内部实现。
    """

    def start(self):
        self.dates: list[date] = []
        self.values: list[float] = []

    def next(self):
        self.dates.append(self.strategy.datas[0].datetime.date(0))
        self.values.append(self.strategy.broker.getvalue())

    def get_analysis(self):
        return {"dates": self.dates, "values": self.values}


# --------------------------------------------------------------------------- #
# 现价快照(资金腾挪判定用)
# 为什么需要它: 市价单要到下一根开盘才成交, 所以"刚提交的卖单"此刻还没回笼现金,
# 直接看 broker.getcash() 会低估可用资金, 从而误判为"钱不够"、多卖一笔缓冲带持仓。
# 这个 analyzer 每根K线记下各标的收盘价, 用来估算待成交卖单能回笼多少钱。
# --------------------------------------------------------------------------- #
class PendingValueSnapshot(bt.Analyzer):
    """记录当前K线各标的的收盘价, 供策略估算待成交订单的金额。"""

    def start(self):
        self.map: dict = {}

    def next(self):
        self.map = {d._name: float(d.close[0]) for d in self.strategy.datas}

    def get_analysis(self):
        return self.map


# --------------------------------------------------------------------------- #
# 策略本体
# --------------------------------------------------------------------------- #
class MonthlyMomentumStrategy(bt.Strategy):
    """每月初买入动量最强的前 top_n 只, 等权持有, 叠加三层风控。"""

    params = (
        ("top_n", DEFAULT_TOP_N),
        ("momentum_window", DEFAULT_MOMENTUM_WINDOW),
        ("cash_buffer", CASH_BUFFER),
        ("lot_size", LOT_SIZE),
        ("names", None),                 # symbol -> 中文名, 仅用于日志与图表
        # ---- 模块一: 大盘择时 ----
        ("use_market_filter", True),     # 是否启用大盘择时保险丝
        # 择时模式: "single" = 单均线(收盘价 vs MA), "dual" = 双均线交叉(MA短 vs MA长)
        ("timing_mode", "single"),
        ("market_ma_period", DEFAULT_MA),          # 单均线模式用
        ("short_ma_period", DEFAULT_SHORT_MA),     # 双均线模式用
        ("long_ma_period", DEFAULT_LONG_MA),       # 双均线模式用
        # ---- 模块二: 缓冲带 ----
        ("sell_threshold", DEFAULT_SELL_THRESHOLD),
        # ---- 模块三: 个股止损 ----
        ("use_stop_loss", True),
        ("stop_loss_pct", DEFAULT_STOP_LOSS),
    )

    def __init__(self):
        # 只对 4 只个股建"可交易"映射; 基准虽然也在 cerebro 里, 但不进这个字典,
        # 所以它永远不会被下单(notify_order 里还有一道断言兜底)。
        self.stock_data = {d._name: d for d in self.datas if d._name in STOCK_POOL}
        self.names = self.p.names or {}

        if not self.stock_data:
            raise ValueError("没有任何个股数据被正确载入, 请检查股票池与数据库。")

        # ---- [模块一] 给基准数据挂均线 ----
        # 实测注意: 在 len == period 那根K线上, ma[-1] 仍是 NaN, 要到 len == period+1 才有效。
        # 所以下面用 len >= period + 1 做守卫, 否则会拿 NaN 去比较(结果恒为 False, 静默失效)。
        self.benchmark = self.getdatabyname(BENCHMARK)
        if self.p.timing_mode == "dual":
            # 双均线: 需要两条均线, 金叉/死叉由它们的大小关系决定
            self.short_ma = bt.indicators.SimpleMovingAverage(
                self.benchmark, period=self.p.short_ma_period)
            self.long_ma = bt.indicators.SimpleMovingAverage(
                self.benchmark, period=self.p.long_ma_period)
            self.market_ma = None            # 单均线模式下才用, 这里显式置空避免误引用
        else:
            # 单均线: 只挂一条
            self.market_ma = bt.indicators.SimpleMovingAverage(
                self.benchmark, period=self.p.market_ma_period)
            self.short_ma = None
            self.long_ma = None

        # ---- 统计变量 ----
        self.rebalance_count = 0             # 实际发生调仓的次数
        self.skipped_no_momentum = 0         # 因数据不足跳过调仓的次数
        self.last_month: tuple[int, int] | None = None
        self.rebalance_log: list[str] = []   # 调仓明细(打印用)

        # 模块一统计
        self.avoid_count = 0                 # 触发避险的次数
        self.avoid_periods: list[dict] = []  # 每次避险的明细(起止日、清仓只数)
        self.avoid_start: date | None = None # 当前避险段的起始日

        # 模块二统计
        self.buffer_hold_count = 0           # 缓冲带生效(继续持有)的次数
        self.sell_outside_count = 0          # 卖出标的的次数

        # 模块三统计
        self.stop_loss_events: list[dict] = []      # 每次止损的明细
        self.stopped_this_month: set[str] = set()   # 本月已止损、禁止再买回的标的
        self.pending_stop_sell: set[str] = set()    # 已提交止损卖单、等待成交的标的
        self._pending_entry: dict[str, dict] = {}   # 止损单的触发信息(成交时补齐)
        self._sold_this_bar: set[str] = set()       # 本根K线刚提交卖单的标的

    # ---------------- 工具方法 ---------------- #

    def _advance_month(self) -> bool:
        """推进"当前月份"并返回本次是否是当月第一个交易日。

        【必须无条件调用】: 这个函数负责把 self.last_month 更新到当前K线的月份。
        早期版本把它写在"只在真的要调仓时才更新"的位置, 结果避险分支提前 return 后
        last_month 一直没更新 -> 同一个月里每根K线都被判定成"月初", 避险区间被反复重开,
        统计出来的"避险天数"远超样本总天数。所以现在把它与决策逻辑彻底解耦:
        next() 每天调用一次, 早退也不会漏掉月份推进。

        注意这跟"日历上的1号"不是一回事 —— 比如 2021-05-01 是假期,
        当月第一个交易日是 2021-05-06, 这里认的正是 05-06。
        """
        d = self.datas[0].datetime.date(0)
        cur = (d.year, d.month)
        if cur != self.last_month:
            self.last_month = cur
            return True
        return False

    def _momentum(self, data) -> float | None:
        """过去 momentum_window 个交易日的收益率。

        用【最近 window+1 个收盘价】的头尾相除, 所以需要 window+1 根K线。
        取值全部来自 [0] 及更早(即当前与历史), 不含任何未来数据。
        数据不足时返回 None, 由调用方决定跳过。
        """
        need = self.p.momentum_window
        if len(data) < need + 1:
            return None
        newest = data.close[0]
        oldest = data.close[-need]
        if not oldest or oldest <= 0 or newest is None:
            return None
        return float(newest) / float(oldest) - 1.0

    @staticmethod
    def _is_nan(v) -> bool:
        """NaN 判断。用 v != v 而不是 math.isnan, 这样非 float 类型也不会抛异常。"""
        try:
            return v is None or v != v
        except TypeError:
            return False

    def _is_market_bearish(self) -> bool | None:
        """[模块一] 大盘避险信号(单均线 / 双均线两种口径)。

        统一约定: 一律使用【前一交易日】的数据, 即下标 [-1]。
        用前一交易日而不是当日, 是为了让信号完全基于已经收盘确认的数据 ——
        当日K线还没走完时就用它的值做本月决策, 就是典型的前视偏差。

        单均线(single): 前一日收盘价 < 前一日均线  -> 看空
        双均线(dual)  : 前一日短均线 < 前一日长均线 -> 看空(死叉/空头排列)
                        前一日短均线 > 前一日长均线 -> 看多(金叉/多头排列)

        返回值: True=需要避险, False=安全, None=数据不足(本月不做择时干预)。

        边界处理: 均线需要 period 根K线才能算出来, 且 [-1] 还要再往前一天,
        所以判定门槛是 len >= period + 1。长均线数据不足时返回 None,
        由调用方按"不触发避险、视为安全"处理(需求里明确要求的默认值)。
        """
        if self.p.timing_mode == "dual":
            need = self.p.long_ma_period          # 长均线是瓶颈
            if len(self.benchmark) < need + 1:
                return None                       # 长均线数据不足 -> 视为安全
            prev_short = self.short_ma[-1]
            prev_long = self.long_ma[-1]
            if self._is_nan(prev_short) or self._is_nan(prev_long):
                return None
            return float(prev_short) < float(prev_long)

        # ---- 单均线模式(保持与旧版完全一致的口径) ----
        period = self.p.market_ma_period
        if len(self.benchmark) < period + 1:
            return None
        prev_close = self.benchmark.close[-1]
        prev_ma = self.market_ma[-1]
        if self._is_nan(prev_close) or self._is_nan(prev_ma):
            return None
        return float(prev_close) < float(prev_ma)

    def _timing_snapshot(self) -> str:
        """把当前择时指标拼成一句可读的说明, 供避险日志使用。"""
        if self.p.timing_mode == "dual":
            prev_short = self.short_ma[-1]
            prev_long = self.long_ma[-1]
            cross = "死叉/空头" if float(prev_short) < float(prev_long) else "金叉/多头"
            return (f"MA{self.p.short_ma_period}={float(prev_short):.3f} vs "
                    f"MA{self.p.long_ma_period}={float(prev_long):.3f} ({cross})")
        return (f"收盘 {float(self.benchmark.close[-1]):.3f} vs "
                f"MA{self.p.market_ma_period}={float(self.market_ma[-1]):.3f}")

    def _buy_to_target(self, data, target_value: float) -> None:
        """把某只标的的持仓补到目标市值(只补不减, 避免无谓的双边手续费)。

        这里要同时满足三个约束:
          1. 等权: 目标市值 = 总资产 / 计划持有的只数;
          2. 手续费缓冲: 买入时最多只用"可用现金的 98%", 留 2% 出来付手续费,
             否则满仓买入后可能因为手续费不够而被券商拒单;
          3. 整手: A股最小交易单位是 100 股, 所以数量向下取整到 100 的整数倍。
        """
        price = float(data.close[0])
        if price <= 0:
            return

        current_size = self.getposition(data).size
        current_value = current_size * price
        delta_value = target_value - current_value

        if delta_value <= 0:
            return                            # 已达标或超配 -> 不买也不卖

        available_cash = self.broker.getcash() * (1.0 - self.p.cash_buffer)
        budget = min(delta_value, available_cash)

        size = int(budget / price / self.p.lot_size) * self.p.lot_size
        if size < self.p.lot_size:
            return                            # 连一手都买不起(常见于高价股)

        self.buy(data=data, size=size)
        log.debug("        买入 %s %d 股 @约%.2f (预算 %.0f 元)",
                  self.names.get(data._name, data._name), size, price, budget)

    def _pending_sell_proceeds(self) -> float:
        """估算"已提交但还没成交"的卖单能回笼多少现金。

        市价单要到下一根开盘才成交, 所以刚提交的卖单此刻还没进 getcash()。
        这里用当前收盘价(analyzer 快照)乘持仓数量近似估算, 目的是避免误判资金不足。
        """
        snapshot = self.analyzers.pending.get_analysis()
        total = 0.0
        waiting = set(self.pending_stop_sell) | set(self._sold_this_bar)
        for symbol, data in self.stock_data.items():
            if symbol in waiting:
                price = snapshot.get(symbol)
                if price:
                    total += self.getposition(data).size * float(price)
        return total

    # ---------------- 模块三: 个股止损 ---------------- #

    def _check_stop_loss(self) -> None:
        """[模块三] 每个交易日检查一次持仓盈亏, 触及 -stop_loss_pct 就清仓。

        成本价用 backtrader 自己的 position.price(成交均价), 不自己维护,
        避免加仓/减仓后成本算错。

        注意: 这里判断用的是当日收盘价, 但卖单要到次日开盘才成交,
        所以实际成交价可能比设定止损线更差(跳空低开时尤其明显)。
        这是真实交易必然存在的情况, 报告里会如实体现, 不做粉饰。
        """
        for symbol, data in self.stock_data.items():
            pos = self.getposition(data)
            if pos.size <= 0:
                continue
            entry_price = float(pos.price)
            if entry_price <= 0:
                continue
            current_price = float(data.close[0])
            pnl_pct = current_price / entry_price - 1.0

            if pnl_pct <= -self.p.stop_loss_pct and symbol not in self.pending_stop_sell:
                self.pending_stop_sell.add(symbol)
                self.stopped_this_month.add(symbol)   # 封杀到月底
                self.sell(data=data, size=pos.size)
                self._sold_this_bar.add(symbol)
                log.info("        [止损] %s 亏损 %.2f%% (成本 %.2f -> 现价 %.2f), "
                         "清仓 %d 股, 本月禁止再买入",
                         self.names.get(symbol, symbol), pnl_pct * 100.0,
                         entry_price, current_price, pos.size)
                self._pending_entry[symbol] = {
                    "trigger_date": self.datas[0].datetime.date(0),
                    "entry_price": entry_price,
                    "trigger_price": current_price,
                    "size": pos.size,
                }

    # ---------------- 模块一 + 二: 月度调仓 ---------------- #

    def _do_rebalance(self) -> None:
        """月度调仓主逻辑: 算动量排名, 套用大盘择时与缓冲带, 先卖后买。"""
        today = self.datas[0].datetime.date(0)

        # 新的一个月 -> 解除上个月的止损禁令(纪律约束只在本月内有效)
        self.stopped_this_month = set()
        self._sold_this_bar = set()

        # ---- 1. 算动量并排名 ----
        scores: dict[str, float] = {}
        for symbol, data in self.stock_data.items():
            mom = self._momentum(data)
            if mom is not None:
                scores[symbol] = mom

        if len(scores) < self.p.top_n:
            self.skipped_no_momentum += 1
            log.debug("%s 可用动量数据不足(%d/%d), 本月跳过调仓",
                      today, len(scores), self.p.top_n)
            return

        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        rank_of = {sym: i + 1 for i, (sym, _) in enumerate(ranked)}
        buy_picks = [sym for sym, _ in ranked[:self.p.top_n]]

        self.rebalance_count += 1
        detail = ", ".join(f"{self.names.get(s, s)}({m:+.2%})" for s, m in ranked)
        log.info("[%s] 调仓 #%d | 动量排名: %s", today, self.rebalance_count, detail)

        # ---- 2. [模块一] 大盘择时保险丝 ----
        bearish = self._is_market_bearish() if self.p.use_market_filter else False

        # 不变式: self.avoid_start 永远等于 avoid_periods[-1]["start"],
        # 即"当前仍然敞开的那一段避险"。每次重新判断前先闭合上一段,
        # 免得多个月连续避险时出现嵌套/永不闭合的区间(早期版本就踩了这个坑:
        # avoid_start 只在为 None 时赋值, 导致第二段起就没有正确地开启/闭合)。
        if self.avoid_start is not None:
            self.avoid_periods[-1]["end"] = today
            self.avoid_start = None

        if bearish:
            self.avoid_count += 1
            held = [s for s, d in self.stock_data.items() if self.getposition(d).size > 0]
            log.info("        [避险] %s 前一日 %s -> 本月空仓避险, 清空 %d 个持仓",
                     BENCHMARK, self._timing_snapshot(), len(held))
            self.avoid_start = today
            self.avoid_periods.append({"start": today, "end": None, "cleared": len(held)})
            self.rebalance_log.append(f"{today}  [避险] 空仓 (清空 {len(held)} 个持仓)")

            for symbol, data in self.stock_data.items():
                pos = self.getposition(data)
                if pos.size > 0:
                    log.debug("        避险清仓 %s", self.names.get(symbol, symbol))
                    self.sell(data=data, size=pos.size)
                    self._sold_this_bar.add(symbol)
            return

        # 走到这里说明本月是安全的; 若刚闭合掉一段避险, 提示一句
        if self.avoid_periods and self.avoid_periods[-1].get("end") == today:
            log.info("        [避险结束] 基准回到均线上方, 本月恢复正常调仓")

        # ---- 3. [模块二] 先卖: 只卖跌出前 sell_threshold 名的 ----
        to_sell: list[str] = []
        for symbol, data in self.stock_data.items():
            if self.getposition(data).size <= 0:
                continue
            # 排名掉出前 sell_threshold 名, 或已经算不出动量 -> 卖出
            if symbol not in rank_of or rank_of[symbol] > self.p.sell_threshold:
                to_sell.append(symbol)

        # 缓冲带生效的: 有持仓、排名在 top_n 之外、但仍在 sell_threshold 之内
        for symbol, data in self.stock_data.items():
            if self.getposition(data).size <= 0:
                continue
            if symbol in rank_of and self.p.top_n < rank_of[symbol] <= self.p.sell_threshold:
                self.buffer_hold_count += 1
                log.info("        [继续持有] %s 动量排名第 %d 名, 仍在缓冲带(前 %d 名)内, "
                         "不卖出", self.names.get(symbol, symbol), rank_of[symbol],
                         self.p.sell_threshold)

        for symbol in to_sell:
            data = self.stock_data[symbol]
            size = self.getposition(data).size
            self.sell_outside_count += 1
            log.info("        [卖出] %s 动量排名第 %s 名, 已跌出前 %d 名, 清仓 %d 股",
                     self.names.get(symbol, symbol), rank_of.get(symbol, "N/A"),
                     self.p.sell_threshold, size)
            self.sell(data=data, size=size)
            self._sold_this_bar.add(symbol)

        # ---- 4. 确定要买什么 ----
        # 被止损封杀的标的即使回到前 top_n 名也不买(模块三的纪律约束)
        buyable = [s for s in buy_picks if s not in self.stopped_this_month]
        blocked = [s for s in buy_picks if s in self.stopped_this_month]
        for symbol in blocked:
            log.info("        [跳过] %s 已回到前 %d 名, 但本月已被止损, 禁止再买入",
                     self.names.get(symbol, symbol), self.p.top_n)

        # 缓冲带里继续持有、且不参与本轮买入的标的
        keep_names = [
            s for s in self.stock_data
            if self.getposition(self.stock_data[s]).size > 0
            and s not in to_sell and s not in buyable and s not in blocked
        ]

        # 计划持有只数 = 本轮要买的 + 缓冲带继续持有的; 等权分摊到每一只
        target_count = max(1, len(buyable) + len(keep_names))
        target_value = self.broker.getvalue() / float(target_count)

        # ---- 5. 资金不足时, 卖出缓冲带持仓腾挪资金 ----
        needed = 0.0
        for symbol in buyable:
            if symbol in to_sell:
                continue                      # 本轮刚卖掉, 视为空仓
            held_value = self.getposition(self.stock_data[symbol]).size * \
                float(self.stock_data[symbol].close[0])
            needed += max(0.0, target_value - held_value)

        available = self.broker.getcash() + self._pending_sell_proceeds()

        if needed > available * (1.0 - self.p.cash_buffer):
            shortfall = needed - available * (1.0 - self.p.cash_buffer)
            log.info("        [资金不足] 距买满前 %d 名还差约 %.0f 元, "
                     "卖出缓冲带持仓腾挪资金", self.p.top_n, shortfall)
            for symbol in list(keep_names):
                if shortfall <= 0:
                    break
                data = self.stock_data[symbol]
                size = self.getposition(data).size
                if size <= 0:
                    continue
                freed = size * float(data.close[0])
                log.info("        [卖出腾挪] %s (动量排名第 %s 名, 在缓冲带内但不属于前 %d 名), "
                         "清仓 %d 股 约 %.0f 元",
                         self.names.get(symbol, symbol), rank_of.get(symbol, "N/A"),
                         self.p.top_n, size, freed)
                self.sell(data=data, size=size)
                self.sell_outside_count += 1
                self._sold_this_bar.add(symbol)
                shortfall -= freed
                available += freed

        # ---- 6. 补仓到目标比例 ----
        for symbol in buyable:
            self._buy_to_target(self.stock_data[symbol], target_value)

        summary = "/".join(self.names.get(s, s) for s in buyable) or "无"
        self.rebalance_log.append(
            f"{today}  买入 {summary}"
            + (f"  (跳过被封杀: {'/'.join(self.names.get(s, s) for s in blocked)})"
               if blocked else ""))
        log.info("         本月买入: %s | 缓冲带继续持有: %s",
                 ", ".join(self.names.get(s, s) for s in buyable) or "无",
                 ", ".join(self.names.get(s, s) for s in keep_names) or "无")

    # ---------------- 主循环 ---------------- #

    def next(self):
        if self.broker.getvalue() <= 0:
            log.error("账户净值跌破 0, 回测终止。请检查参数设置。")
            self.env.runstop()
            return

        # 【关键】先无条件推进月份标记, 再决定做什么。
        # 放在最前面是为了保证: 即使下面因为避险/数据不足提前 return,
        # "本月已经处理过"这件事也已经记录下来了(详见 _advance_month 的说明)。
        is_month_start = self._advance_month()

        # [模块三] 每个交易日都先检查止损(不必等到调仓日)
        if self.p.use_stop_loss:
            self._check_stop_loss()

        # 只有每月第一个交易日才做调仓决策
        if is_month_start:
            self._do_rebalance()

    # ---------------- 订单回调 ---------------- #

    def notify_order(self, order):
        symbol = order.data._name

        if order.status == order.Completed:
            # 保险: 基准永远不该被交易, 一旦发生就是逻辑 bug, 必须留下痕迹
            if symbol == BENCHMARK:
                log.error("[BUG] 基准标的上出现了成交! 请检查数据源映射。")

            # 止损单成交 -> 记录真实成交价与已实现盈亏
            if symbol in self.pending_stop_sell and not order.isbuy():
                info = self._pending_entry.get(symbol, {})
                fill_price = float(order.executed.price)
                size = abs(float(order.executed.size))
                entry = float(info.get("entry_price", 0.0))
                realized = (fill_price - entry) * size if entry > 0 else float("nan")
                trigger = float(info.get("trigger_price", fill_price)) or fill_price
                self.stop_loss_events.append({
                    "symbol": symbol,
                    "name": self.names.get(symbol, symbol),
                    "trigger_date": info.get("trigger_date"),
                    "fill_date": self.datas[0].datetime.date(0),
                    "entry_price": entry,
                    "trigger_price": trigger,
                    "fill_price": fill_price,
                    "size": size,
                    "realized_pnl": realized,
                    # 次日开盘成交, 实际亏损通常比触发时更狠, 这个差值能量化"滑点+跳空"
                    "slippage_vs_trigger": fill_price / trigger - 1.0 if trigger else float("nan"),
                })
                self.pending_stop_sell.discard(symbol)

            log.debug("        [成交] %s %s %d 股 @ %.3f",
                      "买入" if order.isbuy() else "卖出",
                      self.names.get(symbol, symbol),
                      abs(int(order.executed.size)), float(order.executed.price))

        elif order.status in (order.Canceled, order.Margin, order.Rejected):
            reason = {order.Canceled: "已取消", order.Margin: "保证金/现金不足",
                      order.Rejected: "被拒绝"}.get(order.status, "未知")
            log.warning("        [订单未成交] %s %s (原因: %s)",
                        self.names.get(symbol, symbol),
                        "买入" if order.isbuy() else "卖出", reason)
            self.pending_stop_sell.discard(symbol)

    def stop(self):
        # 收尾: 最后一段避险区间若还没闭合, 用最后一个交易日闭合
        if self.avoid_start is not None:
            self.avoid_periods[-1]["end"] = self.datas[0].datetime.date(0)
            self.avoid_start = None

        log.info("")
        log.info("策略结束: 调仓 %d 次 | 避险 %d 次 | 缓冲带继续持有 %d 次 | "
                 "止损 %d 次 | 因数据不足跳过 %d 次",
                 self.rebalance_count, self.avoid_count, self.buffer_hold_count,
                 len(self.stop_loss_events), self.skipped_no_momentum)


# --------------------------------------------------------------------------- #
# 绩效指标计算 (全部基于每日净值序列)
# --------------------------------------------------------------------------- #
def compute_metrics(values: list[float], dates: list[date]) -> dict:
    """由每日净值序列计算总收益率 / 最大回撤 / 夏普比率等指标。

    口径说明(与文件顶部注释一致):
      - 日收益率 = 今日净值 / 昨日净值 - 1;
      - 夏普 = mean(日收益率) / std(日收益率) * sqrt(252), 无风险利率取 0;
      - 最大回撤 = min(净值 / 历史最高净值 - 1), 并记录发生回撤的日期区间。
    """
    if len(values) < 2:
        return {}

    s = pd.Series(values, index=pd.to_datetime(dates), dtype="float64")
    daily_ret = s.pct_change().dropna()

    total_return = s.iloc[-1] / s.iloc[0] - 1.0
    n_days = len(s)

    # 年化收益率: 按交易日数量折算(几何年化), 避免用日历年头数造成误差
    years = n_days / TRADING_DAYS_PER_YEAR
    annual_return = (s.iloc[-1] / s.iloc[0]) ** (1.0 / years) - 1.0 if years > 0 else float("nan")

    # 最大回撤
    running_max = s.cummax()
    drawdown = s / running_max - 1.0
    max_dd = float(drawdown.min())
    dd_end = drawdown.idxmin()
    dd_start = s.loc[:dd_end].idxmax()      # 回撤起点 = 该点之前的历史最高净值

    # 夏普比率(无风险利率 = 0)
    std = float(daily_ret.std())
    sharpe = float(daily_ret.mean()) / std * (TRADING_DAYS_PER_YEAR ** 0.5) if std > 0 else float("nan")

    # 年化波动率
    volatility = std * (TRADING_DAYS_PER_YEAR ** 0.5)

    # 卡玛比率 = 年化收益 / 最大回撤绝对值: 衡量每承担 1 单位回撤换来多少收益
    calmar = annual_return / abs(max_dd) if max_dd < 0 else float("nan")

    return {
        "final_value": float(s.iloc[-1]),
        "total_return": float(total_return),
        "annual_return": float(annual_return),
        "max_drawdown": max_dd,
        "dd_start": dd_start.date(),
        "dd_end": dd_end.date(),
        "sharpe": float(sharpe),
        "volatility": float(volatility),
        "daily_std": std,
        "calmar": float(calmar),
        "n_days": n_days,
        "years": years,
        "series": s,
    }


def compute_benchmark(bench_frame: pd.DataFrame, initial_cash: float,
                      date_index) -> pd.Series:
    """基准净值曲线: 把沪深300ETF当成"期初满仓买入并持有"。

    做法: 用收盘价序列除以第一天的收盘价, 再乘以初始资金。
    注意: 这样得到的净值与 ETF 的实际价差无关, 只反映它的涨跌幅;
    另外它【不含】手续费和滑点(基准就是被动持有, 不产生交易)。
    """
    close = bench_frame["close"].astype("float64")
    # 对齐到策略的净值日期(避免日期错位)
    close = close.reindex(pd.to_datetime(date_index)).ffill()
    base = close.iloc[0]
    if not base or base <= 0:
        raise ValueError("基准标的起始收盘价异常, 无法构建基准净值曲线。")
    return close / base * initial_cash


# --------------------------------------------------------------------------- #
# 模块效果分析
# --------------------------------------------------------------------------- #
def analyze_avoidance(strategy, strat_series: pd.Series,
                      bench_frame: pd.DataFrame) -> dict:
    """[模块一] 量化避险效果: 每段避险期内基准涨跌多少, 相当于踏空/避开了多少。

    做法: 对每一段避险区间, 取基准在同一区间的涨跌幅。
      - 基准在避险期下跌 -> 这段是"避免的亏损"(对我们有利);
      - 基准在避险期上涨 -> 这段是"踏空的收益"(对我们不利)。
    金额影响按该区间起始时的账户规模折算, 便于和净值曲线对上。
    """
    close = bench_frame["close"].astype("float64")
    rows: list[dict] = []
    total_effect = 0.0

    for period in strategy.avoid_periods:
        start = pd.to_datetime(period["start"])
        end = pd.to_datetime(period["end"] or strat_series.index[-1])
        seg = close.loc[(close.index >= start) & (close.index <= end)]
        if len(seg) < 2:
            continue
        bench_ret = float(seg.iloc[-1] / seg.iloc[0] - 1.0)
        try:
            scale = float(strat_series.loc[strat_series.index >= start].iloc[0])
        except Exception:
            scale = INITIAL_CASH
        # 空仓 -> 基准跌多少我们就躲过多少, 所以取负号
        effect = -bench_ret * scale
        total_effect += effect
        rows.append({
            "start": period["start"], "end": end.date(),
            "days": len(seg), "cleared": period.get("cleared", 0),
            "bench_ret": bench_ret, "effect": effect,
        })

    return {"rows": rows, "total_effect": total_effect}


def analyze_stop_loss(strategy, frames: dict) -> dict:
    """[模块三] 汇总止损: 已实现盈亏, 以及止损后到月底的后续表现。

    '躲过的跌幅' 口径: 止损成交后到当月最后一个交易日, 该股若继续下跌,
    那部分跌幅就是我们成功避开的; 若反弹了, 就是被止损打脸(计为负值)。
    """
    events = strategy.stop_loss_events
    if not events:
        return {"events": [], "realized": 0.0, "avoided": 0.0,
                "hit_rate": float("nan"), "n_hit": 0, "n_valid": 0}

    def _ok(v) -> bool:
        return v == v      # NaN 判断

    realized = sum(e["realized_pnl"] for e in events if _ok(e["realized_pnl"]))
    avoided_total = 0.0

    for e in events:
        frame = frames.get(e["symbol"])
        if frame is None or e["fill_date"] is None:
            e["after_ret"] = float("nan")
            continue
        close = frame["close"].astype("float64")
        fill_day = pd.to_datetime(e["fill_date"])
        month_end = fill_day + pd.offsets.MonthEnd(0)
        seg = close.loc[(close.index >= fill_day) & (close.index <= month_end)]
        if len(seg) < 2:
            e["after_ret"] = float("nan")
            continue
        after = float(seg.iloc[-1] / seg.iloc[0] - 1.0)
        e["after_ret"] = after
        # 已离场 -> 它跌了就是我们躲过的损失
        avoided_total += -after * e["size"] * e["fill_price"]

    valid = [e for e in events if _ok(e.get("after_ret"))]
    hit = [e for e in valid if e["after_ret"] < 0]
    return {
        "events": events,
        "realized": realized,
        "avoided": avoided_total,
        "hit_rate": (len(hit) / len(valid)) if valid else float("nan"),
        "n_hit": len(hit),
        "n_valid": len(valid),
    }


# --------------------------------------------------------------------------- #
# 打印报告
# --------------------------------------------------------------------------- #
def _fmt_pct(v: float) -> str:
    return "N/A" if v != v else f"{v * 100:.2f}%"      # v != v 用于判断 NaN


def print_report(m: dict, bench: pd.Series, strat: "MonthlyMomentumStrategy",
                 trade_stats: dict, avoid: dict, stops: dict,
                 args: argparse.Namespace) -> None:
    """打印回测结果报告(含三个优化模块的效果统计)。"""
    print()
    print("=" * 78)
    print("回测结果报告")
    print("=" * 78)

    # 头部再列一次关键参数: 多组对比时最容易搞混结果对应哪组参数
    print(f"  本次参数        : {timing_label(args)} | 止损 {args.stop_loss:.0%} | "
          f"缓冲带前{args.sell_threshold} | 动量{args.momentum_window}日 | "
          f"持仓{args.top_n}只")
    print(f"  标的池          : {', '.join(STOCK_POOL)}")
    print(f"  基准            : {BENCHMARK} (沪深300ETF, 不参与交易, 兼作大盘择时信号源)")
    print(f"  回测区间        : {m['series'].index[0].date()} ~ {m['series'].index[-1].date()}"
          f"  ({m['n_days']} 个交易日, 约 {m['years']:.2f} 年)")
    print(f"  初始资金        : {args.cash:,.0f} 元")
    print(f"  手续费 / 滑点   : {COMMISSION:.4%} / {SLIPPAGE_PCT:.2%} "
          f"(slip_open=True, 滑点作用于开盘价)")

    print("-" * 78)
    print("  【策略表现】")
    print(f"    期末净值        : {m['final_value']:,.2f} 元")
    print(f"    总收益率        : {_fmt_pct(m['total_return'])}")
    print(f"    年化收益率      : {_fmt_pct(m['annual_return'])}")
    print(f"    最大回撤        : {_fmt_pct(m['max_drawdown'])}"
          f"   ({m['dd_start']} 高点 -> {m['dd_end']} 低点)")
    print(f"    夏普比率        : {m['sharpe']:.3f}   (无风险利率=0, 按日收益年化 ×√252)")
    print(f"    卡玛比率        : {m['calmar']:.3f}   (年化收益 / 最大回撤绝对值)")
    print(f"    年化波动率      : {_fmt_pct(m['volatility'])}")
    print(f"    日收益标准差    : {m['daily_std']:.5f}")

    # ---- 基准对比 ----
    bench_total = bench.iloc[-1] / bench.iloc[0] - 1.0
    bench_dd = float((bench / bench.cummax() - 1.0).min())
    excess = m["total_return"] - bench_total

    print("-" * 78)
    print("  【基准对比】(沪深300ETF 买入持有, 不含费用)")
    print(f"    基准期末净值    : {bench.iloc[-1]:,.2f} 元")
    print(f"    基准总收益率    : {_fmt_pct(bench_total)}")
    print(f"    基准最大回撤    : {_fmt_pct(bench_dd)}")
    print(f"    超额收益        : {_fmt_pct(excess)}"
          f"   ({'跑赢' if excess > 0 else '跑输'}基准)")

    # ---- 模块一统计 ----
    print("-" * 78)
    print("  【模块一: 大盘择时保险丝】")
    print(f"    择时方式        : {timing_label(args)}"
          + ("   (金叉持仓 / 死叉避险)" if resolve_timing(args)[0] == "dual" else ""))
    print(f"    触发避险次数    : {strat.avoid_count} 次")
    if avoid["rows"]:
        total_days = sum(r["days"] for r in avoid["rows"])
        print(f"    避险累计天数    : {total_days} 个交易日")
        print(f"    净影响(估算)    : {avoid['total_effect']:+,.0f} 元"
              f"   ({'成功避免了亏损' if avoid['total_effect'] > 0 else '代价是踏空了收益'})")
        print("    明细(基准跌=躲过下跌, 基准涨=踏空):")
        for r in avoid["rows"]:
            tag = "避开下跌" if r["bench_ret"] < 0 else "踏空上涨"
            print(f"      {r['start']} ~ {r['end']}  {r['days']:>3}个交易日  "
                  f"清仓 {r['cleared']} 只  基准 {r['bench_ret']:+.2%}  [{tag}]")
    else:
        print("    本次回测未触发避险(或模块已关闭)。")

    # ---- 模块二统计 ----
    print("-" * 78)
    print("  【模块二: 缓冲带降换手】")
    print(f"    继续持有次数    : {strat.buffer_hold_count} 次"
          f"   (排名跌出前 {strat.p.top_n} 名但仍在缓冲带内, 免于卖出)")
    print(f"    卖出标的次数    : {strat.sell_outside_count} 次"
          f"   (跌出前 {strat.p.sell_threshold} 名, 或为腾挪资金卖出)")
    est_saved = strat.buffer_hold_count * 2 * COMMISSION
    print(f"    估计省下的摩擦  : 约 {est_saved * 100:.2f}%"
          f"   (按每次免于一卖一买、双边 {COMMISSION:.4%} 估算, 未计滑点)")

    # ---- 模块三统计 ----
    print("-" * 78)
    print("  【模块三: 个股止损】")
    print(f"    止损触发次数    : {len(stops['events'])} 次")
    if stops["events"]:
        print(f"    止损已实现盈亏  : {stops['realized']:+,.0f} 元")
        print(f"    止损后躲过的跌幅: {stops['avoided']:+,.0f} 元"
              f"   (止损后到月底继续下跌的部分)")
        print(f"    止损正确率      : {_fmt_pct(stops['hit_rate'])}"
              f"   ({stops['n_hit']}/{stops['n_valid']} 次止损后确实继续下跌)")
        print("    明细:")
        for e in stops["events"]:
            print(f"      {e['trigger_date']} 触发 -> {e['fill_date']} 成交  "
                  f"{e['name']:<6} 成本 {e['entry_price']:.2f} -> 成交 {e['fill_price']:.2f}  "
                  f"({e['realized_pnl']:+,.0f} 元)  "
                  f"成交价较触发价 {e['slippage_vs_trigger']:+.2%}  "
                  f"此后到月底 {_fmt_pct(e.get('after_ret', float('nan')))}")
    else:
        print("    本次回测未触发止损(或模块已关闭)。")

    # ---- 交易统计 ----
    print("-" * 78)
    print("  【交易统计】")
    print(f"    调仓次数        : {strat.rebalance_count} 次"
          f"   (数据不足跳过 {strat.skipped_no_momentum} 次)")
    if trade_stats:
        print(f"    总成交笔数      : {trade_stats.get('total_closed', 0)} 笔平仓交易")
        print(f"    盈利 / 亏损     : {trade_stats.get('won', 0)} / {trade_stats.get('lost', 0)}")
        print(f"    胜率            : {_fmt_pct(trade_stats.get('win_rate', float('nan')))}")
        print(f"    平均盈利 / 亏损 : {trade_stats.get('avg_win', 0):,.0f} / "
              f"{trade_stats.get('avg_loss', 0):,.0f} 元")

    if strat.rebalance_log:
        print("-" * 78)
        print("  【调仓明细】")
        for line in strat.rebalance_log:
            print("    " + line)

    print("=" * 78)
    print("  提示: 用 --no-market-filter / --no-stop-loss / --sell-threshold 2 分别关闭各模块,")
    print("        即可对比出每个优化各自贡献了多少收益、压掉了多少回撤。")
    print("=" * 78)


# --------------------------------------------------------------------------- #
# 画图
# --------------------------------------------------------------------------- #
def plot_result(strat_series: pd.Series, bench_series: pd.Series,
                avoid_periods: list[dict], stop_events: list[dict],
                out_png: Path, args: argparse.Namespace) -> None:
    """画策略净值 vs 基准净值对比图, 并标出大盘避险期与个股止损点。"""
    import matplotlib
    matplotlib.use("Agg")            # 无界面环境也能出图, 必须在 pyplot 之前设置
    import matplotlib.pyplot as plt
    from matplotlib import font_manager

    # ---- 中文字体: 不设置的话中文会显示成方框(豆腐块) ----
    # 逐个探测系统中真实存在的字体, 找到第一个可用的就用它。
    for font_name in ("Microsoft YaHei", "SimHei", "SimSun", "DejaVu Sans"):
        try:
            font_manager.findfont(font_name, fallback_to_default=False)
            plt.rcParams["font.sans-serif"] = [font_name]
            break
        except Exception:
            continue
    plt.rcParams["axes.unicode_minus"] = False   # 负号正常显示, 否则是个方框

    fig, (ax1, ax2) = plt.subplots(
        2, 1, figsize=(14, 9), sharex=True,
        gridspec_kw={"height_ratios": [3, 1]},
    )

    # ---- [模块一] 用灰色竖带标出"大盘避险期" ----
    for i, period in enumerate(avoid_periods):
        x0 = pd.to_datetime(period["start"])
        x1 = pd.to_datetime(period["end"] or strat_series.index[-1])
        for ax in (ax1, ax2):
            ax.axvspan(x0, x1, color="#999999", alpha=0.18, zorder=0,
                       label="大盘避险期(空仓)" if (i == 0 and ax is ax1) else None)

    # ---- 净值曲线 ----
    ax1.plot(strat_series.index, strat_series.values,
             color="#d62728", linewidth=1.8, label="策略净值", zorder=3)
    ax1.plot(bench_series.index, bench_series.values,
             color="#1f77b4", linewidth=1.5, linestyle="--", zorder=3,
             label=f"基准净值 ({BENCHMARK} 沪深300ETF)")
    ax1.axhline(args.cash, color="#bbbbbb", linewidth=1.0,
                linestyle=":", zorder=2, label=f"初始资金 {args.cash:,.0f}")

    # ---- [模块三] 在策略净值曲线上用红三角标出止损点 ----
    plotted_stop_label = False
    for e in stop_events:
        d = pd.to_datetime(e["fill_date"])
        try:
            y = float(strat_series.loc[strat_series.index >= d].iloc[0])
        except Exception:
            continue
        ax1.scatter([d], [y], marker="v", s=70, color="#8B0000",
                    edgecolor="white", linewidth=0.8, zorder=6,
                    label="个股止损点" if not plotted_stop_label else None)
        plotted_stop_label = True

    # 标题里带上本次参数, 图片单独拿出去看也不会忘记是哪组参数跑的
    ax1.set_title(
        "月度动量轮动策略  资金曲线 vs 沪深300ETF 基准\n"
        f"参数: {timing_label(args)} | 止损 {args.stop_loss:.0%} | "
        f"缓冲带前{args.sell_threshold} | 动量{args.momentum_window}日 | "
        f"持仓{args.top_n}只\n"
        "(灰色竖带 = 大盘避险期空仓 ; 红色三角 = 个股止损点)",
        fontsize=13, pad=12)
    ax1.set_ylabel("账户净值 (元)", fontsize=12)
    ax1.grid(True, linestyle="--", alpha=0.35)
    ax1.legend(loc="best", fontsize=10)

    # 标注最终收益, 便于一眼看出跑赢还是跑输。
    # 两个标注都锚在"曲线末端左侧", 并给文字加白底, 避免压在曲线上看不清;
    # 同时左右留出空白, 防止文字被坐标轴裁掉。
    for series, color, tag, y_off in ((strat_series, "#d62728", "策略", -16),
                                      (bench_series, "#1f77b4", "基准", 8)):
        final_ret = series.iloc[-1] / series.iloc[0] - 1.0
        ax1.annotate(
            f"{tag} {final_ret:+.2%}",
            xy=(series.index[-1], series.iloc[-1]),
            xytext=(-14, y_off), textcoords="offset points",
            ha="right", va="center",
            color=color, fontsize=11, fontweight="bold",
            bbox=dict(boxstyle="round,pad=0.28", facecolor="white",
                      edgecolor=color, alpha=0.85, linewidth=0.8),
        )

    ax1.margins(x=0.06)

    # ---- 下图: 策略回撤 ----
    dd = strat_series / strat_series.cummax() - 1.0
    ax2.fill_between(dd.index, dd.values * 100.0, 0,
                     color="#d62728", alpha=0.3, zorder=2)
    ax2.plot(dd.index, dd.values * 100.0, color="#d62728", linewidth=1.0, zorder=3)
    ax2.set_ylabel("策略回撤 (%)", fontsize=12)
    ax2.set_xlabel("日期", fontsize=12)
    ax2.grid(True, linestyle="--", alpha=0.35)
    ax2.annotate(f"最大回撤 {dd.min():.2%}",
                 xy=(dd.idxmin(), dd.min() * 100.0),
                 xytext=(0, -4), textcoords="offset points",
                 ha="center", va="top",
                 color="#d62728", fontsize=11, fontweight="bold",
                 bbox=dict(boxstyle="round,pad=0.28", facecolor="white",
                           edgecolor="#d62728", alpha=0.85, linewidth=0.8))
    ax2.margins(x=0.06)

    fig.autofmt_xdate()
    fig.tight_layout()

    out_png.parent.mkdir(parents=True, exist_ok=True)
    try:
        fig.savefig(out_png, dpi=150)
    finally:
        plt.close(fig)
    print(f"\n资金曲线图已保存: {out_png}")
    print("  图例: 灰色竖带 = 大盘避险期(空仓) | 红色三角 = 个股止损点")


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="月度动量轮动策略回测 (backtrader, 含三重风控, 支持参数敏感性测试)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    # ---- 区间与资金 ----
    p.add_argument("--start", default=DEFAULT_START,
                   help="回测开始日期")
    p.add_argument("--end", default=DEFAULT_END,
                   help="回测结束日期")
    p.add_argument("--cash", type=float, default=INITIAL_CASH,
                   help="初始资金(元)")

    # ---- 策略核心参数(裸名 + 兼容旧名) ----
    # 注意 --ma 的默认值写成 None 而不是 DEFAULT_MA:
    # 否则双均线模式下会误报"你同时传了 --ma 和双均线参数"——
    # 用户其实一个 --ma 都没传, 只是默认值在捣乱。真实使用值在 resolve_timing() 里补。
    p.add_argument("--ma", "--market-ma", dest="ma", type=int, default=None,
                   help=f"【单均线模式】择时均线周期(交易日, 默认 {DEFAULT_MA})。"
                        f"只传它时走单均线; 一旦传了 --short_ma/--long_ma 就自动切双均线")
    p.add_argument("--short_ma", "--short-ma", dest="short_ma", type=int, default=None,
                   help=f"【双均线模式】短均线周期(默认 {DEFAULT_SHORT_MA})。"
                        f"传入本参数即启用双均线交叉择时")
    p.add_argument("--long_ma", "--long-ma", dest="long_ma", type=int, default=None,
                   help=f"【双均线模式】长均线周期(默认 {DEFAULT_LONG_MA})。"
                        f"必须大于 --short_ma")
    p.add_argument("--stop_loss", "--stop-loss-pct", dest="stop_loss", type=float,
                   default=DEFAULT_STOP_LOSS,
                   help="个股止损比例, 小数形式(0.15 = 15%%)")
    p.add_argument("--sell_threshold", "--sell-threshold", dest="sell_threshold",
                   type=int, default=DEFAULT_SELL_THRESHOLD,
                   help="缓冲带: 跌出动量前几名才卖出")
    p.add_argument("--momentum_window", dest="momentum_window", type=int,
                   default=DEFAULT_MOMENTUM_WINDOW,
                   help="动量窗口(交易日)")
    p.add_argument("--top_n", dest="top_n", type=int, default=DEFAULT_TOP_N,
                   help="每月买入动量最强的几只")

    # ---- 模块开关 ----
    p.add_argument("--no-market-filter", action="store_true",
                   help="关闭【模块一】大盘择时保险丝")
    p.add_argument("--no-stop-loss", action="store_true",
                   help="关闭【模块三】个股止损")

    # ---- 其他 ----
    p.add_argument("--name", default=None,
                   help="给本次运行加一个自定义标签, 会拼到图片文件名末尾(便于区分同参数不同实验)")
    p.add_argument("--quiet", action="store_true",
                   help="安静模式: 不打印每笔买入明细")

    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    setup_logging(verbose=not args.quiet)

    # ---- 1. 参数校验(一次把所有问题都列出来) ----
    errors, warnings = validate_params(args)
    if errors:
        print("=" * 78)
        print("[错误] 参数不合法, 回测未启动:")
        for e in errors:
            print(f"  - {e}")
        print("\n  用 `python backtest.py --help` 可以查看所有参数说明。")
        print("=" * 78)
        return 2
    for w in warnings:
        print(f"[警告] {w}")

    # ---- 2. 打印本次运行参数 ----
    out_png = build_output_path(args)
    print_run_header(args, out_png)

    # ---- 3. 前置检查: 数据库 ----
    if not Path(DB_FILE).exists():
        print(f"[错误] 找不到数据库文件: {DB_FILE}")
        print("       请先运行 data_center.py 生成数据, 或确认 config.py 的路径配置。")
        return 1

    # ---- 4. 读数据 ----
    try:
        print(f"\n[1/4] 正在从数据库读取行情: {DB_FILE}")
        frames = load_all_frames(args.start, args.end)
        names = load_symbol_names()
    except ValueError as exc:
        print(f"\n[错误] 数据不足或缺失: {exc}")
        return 1
    except sqlite3.Error as exc:
        print(f"\n[错误] 数据库读取失败: {exc}")
        print("       请确认 quant_data.db 结构完整(表 daily_price 存在)。")
        return 1
    except Exception as exc:
        print(f"\n[错误] 读取数据时发生意外错误: {type(exc).__name__}: {exc}")
        return 1

    # ---- 5. 组装 cerebro 并运行 ----
    try:
        print(f"\n[2/4] 正在配置回测引擎...")
        cerebro = bt.Cerebro()

        # 个股数据: 参与交易
        for symbol in STOCK_POOL:
            cerebro.adddata(bt.feeds.PandasData(dataname=frames[symbol]), name=symbol)

        # 基准数据: 加进来有两个用途 ——
        #   1) 做大盘择时的信号源(策略里会读它的均线);
        #   2) 让基准与策略共享同一根时间轴, 便于逐日对齐比较。
        # 它不会被下单: 策略只对 self.stock_data 里的标的交易,
        # 且 notify_order 里还有一道断言会在真出问题时报警。
        cerebro.adddata(bt.feeds.PandasData(dataname=frames[BENCHMARK]), name=BENCHMARK)

        # 所有策略参数都来自命令行, 不再写死
        timing_mode, ma_val, short_ma_val, long_ma_val = resolve_timing(args)
        cerebro.addstrategy(
            MonthlyMomentumStrategy,
            names=names,
            top_n=args.top_n,
            momentum_window=args.momentum_window,
            use_market_filter=not args.no_market_filter,
            timing_mode=timing_mode,
            market_ma_period=ma_val,
            short_ma_period=short_ma_val if short_ma_val is not None else DEFAULT_SHORT_MA,
            long_ma_period=long_ma_val if long_ma_val is not None else DEFAULT_LONG_MA,
            sell_threshold=args.sell_threshold,
            use_stop_loss=not args.no_stop_loss,
            stop_loss_pct=args.stop_loss,
        )

        cerebro.broker.setcash(args.cash)
        cerebro.broker.setcommission(commission=COMMISSION)

        # 滑点: 千分之一。必须显式打开 slip_open, 否则市价单的成交价(开盘价)
        # 不会被施加滑点, 等于滑点被悄悄削弱 —— 详见文件顶部的说明。
        cerebro.broker.set_slippage_perc(
            perc=SLIPPAGE_PCT,
            slip_open=True,      # 关键: 让滑点也作用在开盘价上
            slip_limit=True,
            slip_match=True,
        )

        cerebro.addanalyzer(ValueRecorder, _name="recorder")
        cerebro.addanalyzer(PendingValueSnapshot, _name="pending")
        cerebro.addanalyzer(bt.analyzers.TradeAnalyzer, _name="trades")

        print(f"       初始资金 {args.cash:,.0f} 元 | 手续费 {COMMISSION:.4%} "
              f"| 滑点 {SLIPPAGE_PCT:.2%} | 持有 {args.top_n} 只等权")
        print(f"       模块一 大盘择时: {'开' if not args.no_market_filter else '关'}"
              f" ({timing_label(args)})"
              f" | 模块二 缓冲带: 跌出前 {args.sell_threshold} 名才卖"
              f" | 模块三 止损: {'开' if not args.no_stop_loss else '关'}"
              f" ({args.stop_loss:.0%})")

        print(f"\n[3/4] 正在运行回测...")
        results = cerebro.run()
        strat = results[0]
    except Exception as exc:
        print(f"\n[错误] 回测运行失败: {type(exc).__name__}: {exc}")
        print("       常见原因: 数据里存在重复日期或空值, 或 backtrader 版本不兼容。")
        import traceback
        traceback.print_exc()
        return 1

    # ---- 6. 取净值序列并算指标 ----
    try:
        rec = strat.analyzers.recorder.get_analysis()
        if not rec["values"]:
            print("\n[错误] 没有采集到任何净值数据, 回测可能一根K线都没跑。")
            print("       可能是回测区间太短, 或动量窗口太大导致所有月份都被跳过。")
            return 1

        metrics = compute_metrics(rec["values"], rec["dates"])
        if not metrics:
            print("\n[错误] 净值数据不足 2 个点, 无法计算绩效指标。")
            return 1

        strat_series = metrics["series"]
        bench_series = compute_benchmark(
            frames[BENCHMARK], args.cash, strat_series.index)

        # 从 backtrader 的交易分析器里取胜率等统计
        trade_stats: dict = {}
        try:
            ta = strat.analyzers.trades.get_analysis()
            total_closed = ta.get("total", {}).get("closed", 0)
            won = ta.get("won", {}).get("total", 0)
            lost = ta.get("lost", {}).get("total", 0)
            trade_stats = {
                "total_closed": total_closed,
                "won": won,
                "lost": lost,
                "win_rate": (won / total_closed) if total_closed else float("nan"),
                "avg_win": ta.get("won", {}).get("pnl", {}).get("average", 0.0),
                "avg_loss": ta.get("lost", {}).get("pnl", {}).get("average", 0.0),
            }
        except Exception as exc:
            log.warning("交易统计解析失败(不影响主结果): %s", exc)

        # 三个模块的效果分析
        avoid = analyze_avoidance(strat, strat_series, frames[BENCHMARK])
        stops = analyze_stop_loss(strat, frames)

        print(f"\n[4/4] 回测完成, 正在生成报告...")
        print_report(metrics, bench_series, strat, trade_stats, avoid, stops, args)

        # ---- 7. 画图(文件名带参数后缀, 不会覆盖之前的运行) ----
        try:
            plot_result(strat_series, bench_series, strat.avoid_periods,
                        strat.stop_loss_events, out_png, args)
        except Exception as exc:
            print(f"\n[警告] 绘图失败(回测结果已正常输出): "
                  f"{type(exc).__name__}: {exc}")

    except Exception as exc:
        print(f"\n[错误] 计算绩效指标时出错: {type(exc).__name__}: {exc}")
        import traceback
        traceback.print_exc()
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
