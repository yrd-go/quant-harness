#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
quant_agent.py — 基于 LangGraph 的多智能体投研决策系统(投资委员会)

设计思路
--------
把"投研决策"拆成四个角色, 前三个【并行】工作, 第四个汇总:

                ┌──────────────────┐
                │ 基本面分析师      │ fetch_fundamentals
                ├──────────────────┤
   START ──────▶│ 技术面分析师      │ fetch_technicals     ──────▶ 投资组合经理 ──▶ END
                ├──────────────────┤                              (汇总出最终建议)
                │ 风险管理员        │ fetch_market_risk
                └──────────────────┘

重要: 这是【决策辅助工具】, 不接任何真实交易接口, 也不会自动下单。

关于"并行"在 LangGraph 里怎么实现(以及为什么能安全并行)
------------------------------------------------------
从 START 拉三条边指向三个分析师节点, 它们就会在同一轮 superstep 里并行执行。
并行安全的关键在于【状态字段不能冲突】:
  - fundamental_analysis / technical_analysis / risk_analysis 三个字段各写各的,
    互不重叠, 所以可以直接并行;
  - messages 是三个节点都想追加的【共享字段】—— 如果不做处理, LangGraph 会认为
    并发写入冲突而报错。解决办法是给它加 reducer:
        messages: Annotated[list, add_messages]
    这样并发写入会被"合并"而不是"覆盖"。
  - symbol 是只读的(没人改它), 所以安全。

三个分析师全部完成后, 才轮到投资组合经理。这是因为我们给三条边都指向了同一个
下游节点, LangGraph 会自动等待这一轮全部结束(barrier)再执行下游。
组合经理除了读三份报告, 还会调用 run_backtest 工具拿该标的的历史风险收益指标。

数据工具一共四个
----------------
    fetch_fundamentals(symbol)  PE/PB/ROE
    fetch_technicals(symbol)    20 日动量等技术指标
    fetch_market_risk()         大盘是否处于避险期(510300 vs MA20)
    run_backtest(symbol)        过去 N 年买入并持有的最大回撤/夏普/总收益
其中 run_backtest 的年限由用户决定(前端下拉框或 --years), 不是模型自己挑 ——
避免每次跑出来的年限都不一样、结果不可复现。

关于工具调用(tool calling)
--------------------------
每个分析师都是标准的 LangChain 工具调用循环:
    绑定工具 -> 模型可能返回 tool_calls -> 我们执行工具 -> 把结果作为 ToolMessage
    回灌 -> 模型基于真实数据写出中文报告 -> 结束。
这样"分析"是基于数据库里真实数字的, 而不是模型凭空编。

关于大模型配置
--------------
默认走 DeepSeek(OpenAI 兼容接口)。API Key 从环境变量读, 优先级:
    1) DEEPSEEK_API_KEY
    2) OPENAI_API_KEY (兼容其它供应商)
    3) 项目根目录下的 .env 文件里的同名变量
base_url 可用 DEEPSEEK_BASE_URL / OPENAI_BASE_URL 覆盖, 模型名用 LLM_MODEL 覆盖。
【不要】把 Key 写死在代码里。

运行前准备
----------
    pip install langgraph langchain langchain-openai streamlit python-dotenv
    # 然后在 .env 里写一行(或设成环境变量):
    # DEEPSEEK_API_KEY=sk-xxxxxxxx

怎么用
------
    # 命令行单标的跑一次(默认回测 1 年)
    python quant_agent.py --symbol 600519

    # 指定回测年限
    python quant_agent.py --symbol 600519 --years 5

    # 只测数据工具, 不调大模型(不需要 API Key, 用来排查数据库问题)
    python quant_agent.py --symbol 600519 --tools-only --years 3

    # 启动前端
    streamlit run app.py
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path
from typing import Annotated, Any, TypedDict

# --------------------------------------------------------------------------- #
# 路径引导: 从当前目录逐级向上找 config.py, 找到后加入 sys.path。
# 这样无论从项目根、还是从 src/ 启动, 都能导入到同一份 config。
# --------------------------------------------------------------------------- #
_CONFIG_DIR = Path(__file__).resolve().parent
while not (_CONFIG_DIR / "config.py").exists() and _CONFIG_DIR != _CONFIG_DIR.parent:
    _CONFIG_DIR = _CONFIG_DIR.parent
if str(_CONFIG_DIR) not in sys.path:
    sys.path.insert(0, str(_CONFIG_DIR))

from config import DB_FILE  # noqa: E402  (必须在 sys.path 调整之后导入)

import pandas as pd  # noqa: E402

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #
BENCHMARK = "510300"          # 沪深300ETF, 作为大盘风险信号源
MOMENTUM_WINDOW = 20          # 动量窗口(交易日)
MA_WINDOW = 20                # 大盘均线窗口
DEFAULT_SYMBOL = "600519"
DEFAULT_BACKTEST_YEARS = 1    # 回测工具默认年限
MAX_TOOL_ROUNDS = 4           # 工具调用循环上限, 防止模型反复调工具陷入死循环
TRADING_DAYS_PER_YEAR = 252   # 年化因子, 与 backtest.py 保持一致

DEFAULT_DEEPSEEK_BASE = "https://api.deepseek.com/v1"
DEFAULT_DEEPSEEK_MODEL = "deepseek-chat"


# =========================================================================== #
# 第一部分: 数据工具
#
# 这三个函数是"纯数据函数", 不依赖大模型, 可以单独测试。
# 它们同时被用作 LangChain 工具(通过 @tool 装饰), 所以在 docstring 里写清楚
# 用途和参数 —— 这段 docstring 会被当作工具描述喂给模型, 直接影响模型是否会用。
# =========================================================================== #
def _connect() -> sqlite3.Connection:
    """打开数据库连接。路径来自 config.py, 不写死。"""
    if not Path(DB_FILE).exists():
        raise FileNotFoundError(
            f"找不到数据库: {DB_FILE}\n"
            f"       请先运行 data_center.py 生成数据, 或检查 config.py。"
        )
    return sqlite3.connect(str(DB_FILE))


def _fetch_fundamentals_impl(symbol: str) -> dict[str, Any]:
    """fetch_fundamentals 的纯实现(便于不经 LangChain 直接调用/测试)。"""
    conn = _connect()
    try:
        # 取该标的【最新交易日】的基本面快照。
        # 注意 ROE 是报告期累计值(如 2026-06-30 是半年累计), 不是年化, 报告里要说明。
        row = conn.execute(
            """
            SELECT f.symbol, f.name, f.trade_date,
                   f.pe_ttm, f.pe_static, f.pb, f.roe,
                   f.roe_report_period, f.total_mv
            FROM fundamentals f
            JOIN (SELECT symbol, MAX(trade_date) AS m
                  FROM fundamentals WHERE symbol = ? GROUP BY symbol) t
              ON f.symbol = t.symbol AND f.trade_date = t.m
            """,
            (symbol,),
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        return {"ok": False, "symbol": symbol,
                "error": f"数据库里没有 {symbol} 的基本面数据(可能是 ETF, 或代码不存在)"}

    (sym, name, trade_date, pe_ttm, pe_static, pb, roe,
     roe_period, total_mv) = row
    return {
        "ok": True,
        "symbol": sym,
        "name": name,
        "trade_date": trade_date,
        "pe_ttm": pe_ttm,
        "pe_static": pe_static,
        "pb": pb,
        "roe": roe,
        "roe_report_period": roe_period,
        # 总市值单位是元, 转成亿元更好读
        "total_mv_yi": round(total_mv / 1e8, 2) if total_mv else None,
        "note": "ROE 为报告期累计值(非年化); PE_TTM 为滚动市盈率",
    }


def _fetch_technicals_impl(symbol: str, window: int = MOMENTUM_WINDOW) -> dict[str, Any]:
    """fetch_technicals 的纯实现。

    动量口径与 backtest.py 的 _momentum 完全一致:
        动量 = close[最新] / close[window 个交易日之前] - 1
    需要 window+1 个收盘价(首尾相减), 所以取最近 window+1 行。
    """
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT trade_date, close FROM daily_price WHERE symbol = ? "
            "ORDER BY trade_date DESC LIMIT ?",
            (symbol, window + 1),
        ).fetchall()
        name_row = conn.execute(
            "SELECT name, asset_type FROM daily_price WHERE symbol = ? LIMIT 1",
            (symbol,)).fetchone()
    finally:
        conn.close()

    if not rows:
        return {"ok": False, "symbol": symbol, "error": f"数据库里没有 {symbol} 的行情数据"}
    if len(rows) < window + 1:
        return {"ok": False, "symbol": symbol,
                "error": f"{symbol} 只有 {len(rows)} 个交易日数据, 不足 {window + 1} 个, "
                         f"无法计算 {window} 日动量"}

    newest_date, newest_close = rows[0]
    oldest_date, oldest_close = rows[-1]
    closes = [r[1] for r in rows]

    if not oldest_close or oldest_close <= 0:
        return {"ok": False, "symbol": symbol, "error": f"{symbol} 起始收盘价异常"}

    momentum = (newest_close / oldest_close - 1.0) * 100.0
    # 附带几个简单的技术观察值, 让分析师有更多依据(都用已有数据算, 不额外查库)
    high = max(closes)
    low = min(closes)
    avg = sum(closes) / len(closes)
    return {
        "ok": True,
        "symbol": symbol,
        "name": name_row[0] if name_row else symbol,
        "asset_type": name_row[1] if name_row else None,
        "start_date": oldest_date,
        "end_date": newest_date,
        "start_close": round(oldest_close, 3),
        "latest_close": round(newest_close, 3),
        "momentum_pct": round(momentum, 2),
        "window_high": round(high, 3),
        "window_low": round(low, 3),
        "window_avg": round(avg, 3),
        "window_days": window,
        "position_in_range": round((newest_close - low) / (high - low) * 100, 1)
        if high > low else 50.0,
    }


def _fetch_market_risk_impl() -> dict[str, Any]:
    """fetch_market_risk 的纯实现: 用 510300 的收盘价与 MA20 判断大盘状态。

    口径说明(重要):
      backtest.py 里为防止前视偏差, 用的是【前一交易日】的 close[-1] 与 ma[-1];
      而这里作为"当前时点的风险快照", 用的是【最新收盘价】与最新 20 日均线。
      两者在实盘中等价(都是用已经收盘确认的数据), 差别只在回测里避免用到当日未完成K线。
    """
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT trade_date, close FROM daily_price WHERE symbol = ? "
            "ORDER BY trade_date DESC LIMIT ?",
            (BENCHMARK, MA_WINDOW),
        ).fetchall()
    finally:
        conn.close()

    if len(rows) < MA_WINDOW:
        return {"ok": False, "benchmark": BENCHMARK,
                "error": f"基准 {BENCHMARK} 数据不足 {MA_WINDOW} 个交易日"}

    latest_date, latest_close = rows[0]
    ma = sum(r[1] for r in rows) / float(len(rows))
    avoid = latest_close < ma
    gap_pct = (latest_close / ma - 1.0) * 100.0
    return {
        "ok": True,
        "benchmark": BENCHMARK,
        "trade_date": latest_date,
        "close": round(latest_close, 3),
        "ma20": round(ma, 3),
        "avoid_mode": bool(avoid),
        "gap_pct": round(gap_pct, 2),
        "signal": ("避险期: 基准收盘价跌破 MA20, 历史上该状态下策略应空仓"
                   if avoid else
                   "正常期: 基准收盘价在 MA20 之上, 可以正常持仓"),
    }


# ---- 惰性注册 LangChain 工具 -------------------------------------------------- #
# ---- 历史回测工具 ------------------------------------------------------------ #
def _metrics_from_values(values: list[float]) -> dict[str, Any]:
    """由每日净值序列算总收益 / 年化 / 最大回撤 / 夏普 / 波动率。

    口径与 backtest.py 的 compute_metrics 保持一致, 避免"回测脚本一个数、
    Agent 报另一个数"这种自相矛盾:
      日收益率 r_t = 净值_t / 净值_{t-1} - 1
      总收益率     = 末值 / 首值 - 1
      年化收益率   = (末值/首值)^(252/交易日数) - 1     (几何年化)
      最大回撤     = min(净值 / 历史最高净值 - 1)
      夏普         = mean(r) / std(r) * sqrt(252)       (无风险利率取 0)
      年化波动率   = std(r) * sqrt(252)
    其中 std 用样本标准差(pandas 默认 ddof=1)。
    """
    s = pd.Series(values, dtype="float64")
    if len(s) < 2:
        return {}

    def f(v) -> float | None:
        """把 numpy 标量转成原生 Python float。

        这一步【必须做】, 不能省: round()/算术运算作用在 numpy.float64 上,
        返回的仍是 np.float64; 而 str(np.float64(-8.46)) 会打印成
        "np.float64(-8.46)" —— 那是个函数调用语法, 会导致两个后果:
          1) 解析工具返回值时 ast.literal_eval 报 "malformed node ... Call object";
          2) 大模型读工具返回值时会看到一堆 np.float64(...) 噪音, 可能理解错。
        所以统一转成原生 float(NaN 时给 None, 保证 JSON 可序列化)。
        """
        try:
            x = float(v)
        except (TypeError, ValueError):
            return None
        return None if x != x else x          # x != x 用于判断 NaN

    daily_ret = s.pct_change().dropna()
    total_return = f(s.iloc[-1] / s.iloc[0] - 1.0)
    n_days = len(s)
    years = n_days / TRADING_DAYS_PER_YEAR
    annual_return = f((s.iloc[-1] / s.iloc[0]) ** (1.0 / years) - 1.0) if years > 0 else None

    running_max = s.cummax()
    drawdown = s / running_max - 1.0
    max_dd = f(drawdown.min())
    dd_end_pos = int(drawdown.values.argmin())
    dd_start_pos = int(s.iloc[:dd_end_pos + 1].values.argmax())

    std = f(daily_ret.std())
    sharpe = (f(float(daily_ret.mean()) / std * (TRADING_DAYS_PER_YEAR ** 0.5))
              if std and std > 0 else None)
    volatility = f(std * (TRADING_DAYS_PER_YEAR ** 0.5)) if std is not None else None

    return {
        "total_return_pct": None if total_return is None else round(total_return * 100, 2),
        "annual_return_pct": None if annual_return is None else round(annual_return * 100, 2),
        "max_drawdown_pct": None if max_dd is None else round(max_dd * 100, 2),
        "sharpe": None if sharpe is None else round(sharpe, 3),
        "annual_volatility_pct": None if volatility is None else round(volatility * 100, 2),
        "trading_days": int(n_days),
        "years": round(float(years), 2),
        "_dd_start_pos": dd_start_pos,
        "_dd_end_pos": dd_end_pos,
    }


def _run_backtest_impl(symbol: str, years: int = 1) -> dict[str, Any]:
    """run_backtest 的纯实现: 该标的"买入并持有"过去 N 年的表现。

    口径说明(重要):
      这是【单标的买入并持有】的历史表现, 不是 backtest.py 里那套
      "月度动量轮动 + 择时 + 止损"策略的回测结果。两者用途不同:
        - 本工具: 回答"这只票自己过去N年表现如何"(最大回撤/夏普/总收益)
        - backtest.py: 回答"整套策略历史上表现如何"
      之所以这么设计, 是因为这个工具是按代码查的, 而策略回测需要多标的协作。

    另: ETF(如 510300)没有基本面数据, 但有行情, 所以本工具对它同样有效。
    """
    try:
        years = int(years)
    except (TypeError, ValueError):
        years = 1
    if years < 1:
        years = 1
    if years > 20:
        years = 20

    conn = _connect()
    try:
        row = conn.execute(
            "SELECT MAX(trade_date) FROM daily_price WHERE symbol = ?", (symbol,)
        ).fetchone()
        latest = row[0] if row else None
        if not latest:
            return {"ok": False, "symbol": symbol,
                    "error": f"数据库里没有 {symbol} 的行情数据"}

        start_date = (pd.Timestamp(latest) - pd.DateOffset(years=years)).strftime("%Y-%m-%d")
        earliest = conn.execute(
            "SELECT MIN(trade_date) FROM daily_price WHERE symbol = ?", (symbol,)
        ).fetchone()[0]

        df = pd.read_sql(
            "SELECT trade_date, close FROM daily_price "
            "WHERE symbol = ? AND trade_date >= ? ORDER BY trade_date",
            conn, params=(symbol, start_date),
        )
        name_row = conn.execute(
            "SELECT name FROM daily_price WHERE symbol = ? LIMIT 1", (symbol,)).fetchone()
    finally:
        conn.close()

    if df.empty:
        return {"ok": False, "symbol": symbol,
                "error": f"{symbol} 在 {start_date} 之后没有行情数据"}

    # 价格列强制转数值并丢空值(和 backtest.py 的清洗口径一致)
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df = df.dropna(subset=["close"])
    df = df[df["close"] > 0]
    if len(df) < 2:
        return {"ok": False, "symbol": symbol, "error": f"{symbol} 有效数据不足 2 个交易日"}

    metrics = _metrics_from_values(df["close"].tolist())
    if not metrics:
        return {"ok": False, "symbol": symbol, "error": "指标计算失败(数据点太少)"}

    dates = df["trade_date"].tolist()
    dd_start = dates[metrics.pop("_dd_start_pos")]
    dd_end = dates[metrics.pop("_dd_end_pos")]

    # 若请求的年限超出了数据库覆盖范围, 如实告知, 避免用户误以为回测了那么久
    truncated = bool(earliest and start_date < earliest)
    note = "单标的买入并持有口径(不含手续费/滑点, 未做任何择时或止损)"
    if truncated:
        note += (f"; 数据库最早只有 {earliest}, 实际只回测了 "
                 f"{df['trade_date'].iloc[0]} 起的 {metrics['years']} 年, "
                 f"不足请求的 {years} 年")

    return {
        "ok": True,
        "symbol": symbol,
        "name": name_row[0] if name_row else symbol,
        "requested_years": years,
        "start_date": str(df["trade_date"].iloc[0]),
        "end_date": str(df["trade_date"].iloc[-1]),
        "start_close": round(float(df["close"].iloc[0]), 3),
        "end_close": round(float(df["close"].iloc[-1]), 3),
        "total_return_pct": metrics["total_return_pct"],
        "annual_return_pct": metrics["annual_return_pct"],
        "max_drawdown_pct": metrics["max_drawdown_pct"],
        "max_drawdown_from": str(dd_start),
        "max_drawdown_to": str(dd_end),
        "sharpe": metrics["sharpe"],
        "annual_volatility_pct": metrics["annual_volatility_pct"],
        "trading_days": metrics["trading_days"],
        "actual_years": metrics["years"],
        "note": note,
    }


# ---- 惰性注册 LangChain 工具 -------------------------------------------------- #
# 为什么要惰性: 这几个工具函数是纯数据函数, 即使没装 langchain 也应该能用来查数据
# (--tools-only 模式)。所以不在模块顶层 import langchain, 而是在需要时才构建工具对象。
_TOOLS_CACHE: dict[str, Any] = {}


def build_tools(backtest_years: int = DEFAULT_BACKTEST_YEARS):
    """把四个数据函数包装成 LangChain 工具。

    返回 (fetch_fundamentals, fetch_technicals, fetch_market_risk, run_backtest)。

    注意 run_backtest 是【用年限闭包出来的】: 工具签名只暴露 symbol,
    年限在构建时就固定了。这样做的原因:
      - 让"回测几年"由前端下拉框(或命令行)决定, 而不是让模型每次自己挑一个年限
        (模型挑年限会引入不确定性, 也让结果不可复现);
      - 工具描述里会写明实际年限, 模型据此知道该按多少年解读。
    """
    cache_key = f"tools_{backtest_years}"
    if cache_key in _TOOLS_CACHE:
        return _TOOLS_CACHE[cache_key]
    try:
        from langchain_core.tools import tool
    except ImportError as exc:
        raise ImportError(
            "缺少 langchain-core, 请先安装:\n"
            "    pip install langgraph langchain langchain-openai"
        ) from exc

    @tool
    def fetch_fundamentals(symbol: str) -> dict:
        """查询指定股票代码的最新基本面数据(PE_TTM、PE_静、PB、ROE、总市值)。

        参数 symbol: 6 位股票代码, 例如 "600519"(贵州茅台)。
        返回最新交易日的估值与盈利能力指标。若该代码没有基本面数据会返回 ok=False。
        """
        return _fetch_fundamentals_impl(symbol)

    @tool
    def fetch_technicals(symbol: str) -> dict:
        """查询指定股票代码的技术面数据, 核心是 20 日动量(区间收益率)。

        参数 symbol: 6 位股票代码, 例如 "600519"。
        返回过去 20 个交易日的收益率百分比、区间最高/最低/均价, 以及最新收盘价。
        动量口径 = 最新收盘价 / 20 个交易日前的收盘价 - 1。
        """
        return _fetch_technicals_impl(symbol)

    @tool
    def fetch_market_risk() -> dict:
        """查询当前大盘风险状态。无需参数。

        用沪深300ETF(510300)的最新收盘价与 20 日均线比较:
        收盘价低于 MA20 视为"避险期"(历史上此状态应减仓或空仓), 否则为"正常期"。
        """
        return _fetch_market_risk_impl()

    # 注意: 这个 docstring 必须是【普通字符串】, 不能用 f-string。
    # 因为 f-string 里的 {backtest_years} 会被 Python 当成集合字面量而不是占位符,
    # 于是 functools.wraps 复制到包装函数上的 __doc__ 变成 None, @tool 就会报
    # "Function must have a docstring"。年限改用 str.format 在运行时注入。
    @tool
    def run_backtest(symbol: str) -> dict:
        """对指定股票做历史回测, 返回最大回撤、夏普比率和总收益。

        参数 symbol: 6 位股票代码, 例如 "600519"。
        回测区间: 从最新交易日往前推 {years} 年(年限由用户在界面上选择)。
        回测口径: 该标的【买入并持有】, 不含手续费与滑点, 未做任何择时或止损。

        返回主要指标:
          - max_drawdown_pct: 最大回撤(百分比, 负数, 越接近 0 越好)
          - sharpe: 夏普比率(无风险利率取 0, 按日收益年化)
          - total_return_pct: 区间总收益率(百分比)
          - annual_return_pct: 年化收益率(百分比)
          - annual_volatility_pct: 年化波动率(百分比)
        另外返回区间起止日期、起止价、实际回测年数。
        若请求年限超过数据库覆盖范围, note 字段会说明实际只回测了多久。
        """
        return _run_backtest_impl(symbol, backtest_years)

    # 把年限注入到工具描述里, 让模型知道本次回测是按多少年算的
    run_backtest.description = run_backtest.description.format(years=backtest_years)

    tools = (fetch_fundamentals, fetch_technicals, fetch_market_risk, run_backtest)
    _TOOLS_CACHE[cache_key] = tools
    return tools


# =========================================================================== #
# 第二部分: 共享状态 (State)
# =========================================================================== #
try:
    from langgraph.graph.message import add_messages
except ImportError:      # 还没装 langgraph 时, 保证本文件仍能被 import 去看工具
    def add_messages(left, right):       # type: ignore[misc]
        """langgraph 未安装时的占位实现: 简单地把新消息追加到列表尾部。"""
        if left is None:
            left = []
        if right is None:
            return list(left)
        if not isinstance(right, list):
            right = [right]
        return list(left) + list(right)


class InvestState(TypedDict):
    """投资委员会共享状态。

    字段设计要点:
      - messages 用 Annotated[..., add_messages] 加 reducer。三个分析师会【并行】
        往 messages 里追加内容, 没有 reducer 的话 LangGraph 会判定并发写入冲突。
      - 三个 *_analysis 字段各自独立, 并行写入不同字段是安全的。
      - symbol 是输入参数, 全程只读。
      - 其余字段是给前端展示/传递中间结果用的。
    """

    symbol: str                                   # 待分析的股票代码(只读)
    messages: Annotated[list, add_messages]       # 对话/工作记录(需要 reducer)
    fundamental_analysis: str                     # 基本面分析师报告
    technical_analysis: str                       # 技术面分析师报告
    risk_analysis: str                            # 风险管理员报告
    backtest_analysis: str                        # 历史回测结果(组合经理调工具时写入)
    final_decision: str                           # 投资组合经理最终建议
    error: str                                    # 出错信息(任一步失败时写入)


def new_state(symbol: str) -> InvestState:
    """构造一份初始状态(前端和命令行都用它)。"""
    return InvestState(
        symbol=symbol,
        messages=[],
        fundamental_analysis="",
        technical_analysis="",
        risk_analysis="",
        backtest_analysis="",
        final_decision="",
        error="",
    )


# =========================================================================== #
# 第三部分: 大模型
# =========================================================================== #
def load_env_file() -> None:
    """尝试从项目根目录的 .env 读取环境变量(没装 python-dotenv 就静默跳过)。"""
    env_path = _CONFIG_DIR / ".env"
    if not env_path.exists():
        return
    try:
        from dotenv import load_dotenv
        load_dotenv(env_path, override=False)
    except ImportError:
        # 没装 dotenv: 手工解析一个极简的 KEY=VALUE 格式, 免得用户白配了 .env
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def get_api_config() -> tuple[str, str, str]:
    """读取 (api_key, base_url, model)。取不到 key 就抛异常并给出中文指引。"""
    load_env_file()
    api_key = os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("OPENAI_API_KEY") or ""
    base_url = (os.environ.get("DEEPSEEK_BASE_URL")
                or os.environ.get("OPENAI_BASE_URL")
                or os.environ.get("OPENAI_API_BASE")
                or DEFAULT_DEEPSEEK_BASE)
    model = os.environ.get("LLM_MODEL") or DEFAULT_DEEPSEEK_MODEL

    if not api_key:
        raise RuntimeError(
            "没有找到大模型 API Key。请任选一种方式配置:\n"
            "   方式一(推荐): 在项目根目录建一个 .env 文件, 写入一行:\n"
            "       DEEPSEEK_API_KEY=sk-你的key\n"
            "   方式二: 设为环境变量\n"
            '       PowerShell:  $env:DEEPSEEK_API_KEY="sk-你的key"\n'
            '       CMD:         set DEEPSEEK_API_KEY=sk-你的key\n'
            "   如果你用的是 OpenAI 或其它兼容服务, 可以改用 OPENAI_API_KEY,\n"
            "   并用 OPENAI_BASE_URL 指定接口地址。"
        )
    return api_key, base_url, model


def build_llm(temperature: float = 0.2):
    """创建 ChatOpenAI 实例(DeepSeek 走 OpenAI 兼容协议)。"""
    try:
        from langchain_openai import ChatOpenAI
    except ImportError as exc:
        raise ImportError(
            "缺少 langchain-openai, 请先安装:\n"
            "    pip install langchain-openai"
        ) from exc

    api_key, base_url, model = get_api_config()
    return ChatOpenAI(
        model=model,
        api_key=api_key,
        base_url=base_url,
        temperature=temperature,
        timeout=90,
        max_retries=2,
    )


# =========================================================================== #
# 第四部分: 工具调用循环
#
# 这是每个分析师的"干活"逻辑: 让模型自己决定要不要调工具, 调了就执行并把真实结果
# 回灌, 直到模型给出最终文字报告。
# =========================================================================== #
def run_tool_agent(llm, tools: list, system_prompt: str, user_prompt: str,
                   log_prefix: str = "") -> tuple[str, list]:
    """跑一个"带工具的分析师", 返回 (最终文字报告, 新增消息列表)。

    流程:
      1. 把工具绑到模型上(bind_tools), 并把 system + user 提示发过去;
      2. 若模型返回 tool_calls -> 逐个执行工具, 把结果包成 ToolMessage 回灌, 再问一次;
      3. 若模型直接给出文本(没有 tool_calls) -> 这就是报告, 结束;
      4. 最多循环 MAX_TOOL_ROUNDS 轮, 防止模型陷入"反复调工具"的死循环。
    """
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

    tool_map = {t.name: t for t in tools}
    llm_with_tools = llm.bind_tools(tools)

    messages: list = [SystemMessage(content=system_prompt),
                      HumanMessage(content=user_prompt)]
    emitted: list = []          # 这一轮新增的消息(用于写回 state["messages"])
    emitted.append(HumanMessage(content=f"[{log_prefix}] 任务: {user_prompt}"))

    for _ in range(MAX_TOOL_ROUNDS):
        ai_msg = llm_with_tools.invoke(messages)
        messages.append(ai_msg)
        emitted.append(ai_msg)

        tool_calls = getattr(ai_msg, "tool_calls", None) or []
        if not tool_calls:
            # 没有工具调用了 -> 模型已给出最终文本
            text = ai_msg.content if isinstance(ai_msg.content, str) else str(ai_msg.content)
            return text, emitted

        # 执行模型要求调用的每个工具
        for call in tool_calls:
            name = call.get("name")
            args = call.get("args") or {}
            call_id = call.get("id") or name
            func = tool_map.get(name)
            try:
                if func is None:
                    result = {"ok": False, "error": f"未知工具 {name}"}
                else:
                    result = func.invoke(args)
                content = str(result)
            except Exception as exc:      # 工具内部异常不该让整个图崩掉
                content = str({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
            tool_msg = ToolMessage(content=content, tool_call_id=call_id)
            messages.append(tool_msg)
            emitted.append(tool_msg)

    # 循环用尽仍未收敛: 退回最后一条 AI 文本, 并如实说明
    last_ai = next((m for m in reversed(messages) if isinstance(m, AIMessage)), None)
    fallback = (last_ai.content if last_ai and isinstance(last_ai.content, str)
                else "(模型未返回内容)")
    return (f"[注意] 工具调用轮数达到上限 {MAX_TOOL_ROUNDS}, 以下为最后一次模型输出:\n"
            f"{fallback}"), emitted


# =========================================================================== #
# 第五部分: 四个 Agent 节点
# =========================================================================== #
FUNDAMENTAL_SYSTEM = """你是一位严谨的基本面分析师, 服务于A股投资委员会。
你会调用工具获取真实的财务数据, 然后基于【工具返回的真实数字】写一份简短的中文分析报告。

要求:
1. 必须先调用工具拿数据, 不允许凭记忆编造任何数字。
2. 报告控制在 200 字以内, 结构清晰。
3. 必须包含: 估值水平(PE_TTM、PB 处于什么区间)、盈利能力(ROE)、以及一句结论。
4. 提醒口径: ROE 是报告期累计值(非年化), PE 是滚动市盈率。
5. 只做分析不做最终买卖决定, 买卖决定由投资组合经理负责。
6. 若工具返回 ok=False, 直接说明"数据缺失"并停止分析, 不要编造。"""

TECHNICAL_SYSTEM = """你是一位技术面分析师, 服务于A股投资委员会。
你会调用工具获取真实的行情数据, 然后基于【工具返回的真实数字】写一份简短的中文分析报告。

要求:
1. 必须先调用工具拿数据, 不允许凭记忆编造任何数字。
2. 报告控制在 200 字以内。
3. 必须包含: 20 日动量是多少、动量是正还是负、最新价处于区间高低位的什么位置、一句结论。
4. 说明动量为正代表趋势向上, 为负代表趋势向下; 这是趋势跟踪指标, 不代表估值高低。
5. 只做分析不做最终买卖决定。"""

RISK_SYSTEM = """你是一位风险管理员, 服务于A股投资委员会。你的职责是给出保守的风险提示。
你会调用工具获取当前大盘状态, 然后基于【工具返回的真实数字】写一份简短的中文风险报告。

要求:
1. 必须先调用工具拿数据, 不允许凭记忆编造任何数字。
2. 报告控制在 200 字以内。
3. 必须包含: 大盘当前处于避险期还是正常期、判断依据(收盘价与 MA20 的关系)、以及对个股持仓的风险提示。
4. 你的立场要偏保守: 处于避险期时必须明确建议降低仓位或空仓。
5. 只做分析不做最终买卖决定。"""

PM_SYSTEM = """你是投资组合经理, 负责综合三位分析师的意见, 给出最终决策。
你会拿到基本面分析师、技术面分析师、风险管理员的报告。
你还有一个回测工具 run_backtest, 可以查询该标的【过去若干年买入并持有】的历史表现
(最大回撤、夏普比率、总收益)。

要求:
1. 必须调用 run_backtest 工具获取该标的的历史回测数据 —— 最大回撤和夏普比率是
   判断"这笔投资的风险收益比"的关键依据, 不看它就不许下结论。
2. 除了回测工具的结果, 不要引入其它新数据; 基本面/技术面/风险的意见以前三份报告为准。
3. 输出格式(中文, 350 字以内):
   【最终建议】买入 / 持有 / 卖出 (三选一, 必须明确)
   【历史回测】引用回测工具返回的最大回撤、夏普比率、总收益(要写出具体数字)
   【决策理由】把三份报告 + 回测数据的关键点串起来, 并指出它们之间是否一致
   【风险提示】一句话, 必须提及止损纪律
   【建议仓位】给出一个大致比例(如 0%-10% / 20%-30%)
4. 若历史最大回撤很深(例如超过 30%), 或夏普比率接近/低于 0, 必须据此收紧仓位建议。
5. 若三份报告意见冲突, 必须说明你更看重哪一个, 以及为什么。
6. 风险管理员处于避险期时, 仓位建议不得高于 20%。
7. 这是决策辅助, 不是投资建议, 结尾用一句话提醒读者自行判断风险。"""


def _fmt_report(title: str, body: str) -> str:
    return f"### {title}\n{body}"


def _log(state: InvestState, text: str) -> list:
    """往 messages 里追加一条系统记录(用于前端展示工作流程)。"""
    from langchain_core.messages import SystemMessage
    return [SystemMessage(content=text)]


def _extract_tool_payload(msgs: list, marker: str = "max_drawdown_pct") -> dict | None:
    """从消息流里把最后一个工具返回的 dict 解析出来(供前端展示原始数字)。

    为什么靠【特征键】而不是工具名来识别:
      实测 ToolMessage.name 是 None(content 里也不含工具名), 所以按名字判断会全部落空。
      这里改用"返回值里是否含 max_drawdown_pct 这种独有键"来判断, 更可靠。

    解析用 ast.literal_eval 而不是 json.loads: 工具返回的是 str(dict), 键是单引号,
    json 解不了; literal_eval 能正确处理单引号/None/True。
    (前提是 dict 里不能有 np.float64 这类非字面量 —— 这一点已在 _metrics_from_values
     里通过强制转原生 float 保证了。)
    """
    import ast
    for m in reversed(msgs):
        if type(m).__name__ != "ToolMessage":
            continue
        content = str(getattr(m, "content", "") or "")
        if marker not in content:
            continue
        try:
            parsed = ast.literal_eval(content)
        except (ValueError, SyntaxError) as exc:
            # 解析失败不要静默吞掉: 把原因带出去, 便于定位(比如又出现了 numpy 标量)
            return {"_parse_error": f"{type(exc).__name__}: {exc}",
                    "_raw": content[:200]}
        if isinstance(parsed, dict):
            return parsed
    return None


def _format_backtest_md(data: dict) -> str:
    """把回测工具返回的 dict 渲染成前端用的 Markdown。"""
    if not data:
        return ""
    if "_parse_error" in data:
        return (f"### 历史回测\n- ⚠️ 解析工具返回失败: {data['_parse_error']}\n"
                f"- 原始内容片段: `{data.get('_raw', '')}`")
    return (
        f"### 历史回测({data.get('actual_years')} 年, 买入并持有)\n"
        f"- 区间: {data.get('start_date')} ~ {data.get('end_date')}"
        f"({data.get('trading_days')} 个交易日)\n"
        f"- 价格: {data.get('start_close')} → {data.get('end_close')}\n"
        f"- 最大回撤: **{data.get('max_drawdown_pct')}%**"
        f"({data.get('max_drawdown_from')} → {data.get('max_drawdown_to')})\n"
        f"- 夏普比率: **{data.get('sharpe')}**\n"
        f"- 总收益: **{data.get('total_return_pct')}%**"
        f"(年化 {data.get('annual_return_pct')}%)\n"
        f"- 年化波动率: {data.get('annual_volatility_pct')}%\n"
        f"- 说明: {data.get('note')}"
    )


def make_nodes(llm, backtest_years: int = DEFAULT_BACKTEST_YEARS):
    """基于给定的 llm 生成四个节点函数。

    做成闭包是为了: llm 只创建一次, 四个节点共享; 也方便测试时注入假 llm。
    backtest_years 会传给 run_backtest 工具(决定回测年限, 由前端下拉框/命令行控制)。
    """
    tools = build_tools(backtest_years)
    fetch_fundamentals, fetch_technicals, fetch_market_risk, run_backtest = tools

    def fundamental_analyst(state: InvestState) -> dict:
        """节点1: 基本面分析师(调用 fetch_fundamentals)。"""
        symbol = state["symbol"]
        try:
            text, msgs = run_tool_agent(
                llm, [fetch_fundamentals], FUNDAMENTAL_SYSTEM,
                f"请分析股票 {symbol} 的基本面, 先用工具获取它的 PE/PB/ROE 数据。",
                log_prefix="基本面分析师",
            )
            return {"fundamental_analysis": _fmt_report("基本面分析", text),
                    "messages": msgs}
        except Exception as exc:
            err = f"基本面分析师执行失败: {type(exc).__name__}: {exc}"
            return {"fundamental_analysis": _fmt_report("基本面分析", f"(失败) {err}"),
                    "messages": _log(state, err), "error": err}

    def technical_analyst(state: InvestState) -> dict:
        """节点2: 技术面分析师(调用 fetch_technicals)。"""
        symbol = state["symbol"]
        try:
            text, msgs = run_tool_agent(
                llm, [fetch_technicals], TECHNICAL_SYSTEM,
                f"请分析股票 {symbol} 的技术面, 先用工具获取它最近 {MOMENTUM_WINDOW} "
                f"个交易日的动量数据。",
                log_prefix="技术面分析师",
            )
            return {"technical_analysis": _fmt_report("技术面分析", text),
                    "messages": msgs}
        except Exception as exc:
            err = f"技术面分析师执行失败: {type(exc).__name__}: {exc}"
            return {"technical_analysis": _fmt_report("技术面分析", f"(失败) {err}"),
                    "messages": _log(state, err), "error": err}

    def risk_manager(state: InvestState) -> dict:
        """节点3: 风险管理员(调用 fetch_market_risk)。"""
        try:
            text, msgs = run_tool_agent(
                llm, [fetch_market_risk], RISK_SYSTEM,
                "请评估当前A股大盘风险状态, 先用工具获取沪深300ETF 的收盘价与 MA20。",
                log_prefix="风险管理员",
            )
            return {"risk_analysis": _fmt_report("风险预警", text), "messages": msgs}
        except Exception as exc:
            err = f"风险管理员执行失败: {type(exc).__name__}: {exc}"
            return {"risk_analysis": _fmt_report("风险预警", f"(失败) {err}"),
                    "messages": _log(state, err), "error": err}

    def portfolio_manager(state: InvestState) -> dict:
        """节点4: 投资组合经理(汇总前三份报告 + 用回测工具查历史表现)。

        与前三个分析师不同, 这里【只绑定 run_backtest 一个工具】:
        它不该绕过分析师去重抓基本面/技术面数据, 但需要自己拿到历史风险收益指标。
        回测工具的年限在 build_tools 里就被固定成用户选择的值, 模型不能自己改。
        """
        from langchain_core.messages import SystemMessage

        symbol = state["symbol"]
        payload = (
            f"股票代码: {symbol}\n\n"
            f"{state.get('fundamental_analysis') or '(基本面报告缺失)'}\n\n"
            f"{state.get('technical_analysis') or '(技术面报告缺失)'}\n\n"
            f"{state.get('risk_analysis') or '(风险报告缺失)'}"
        )
        try:
            text, msgs = run_tool_agent(
                llm, [run_backtest], PM_SYSTEM, payload, log_prefix="投资组合经理",
            )
            result: dict = {
                "final_decision": text,
                "messages": [SystemMessage(
                    content="[投资组合经理] 已汇总三份报告并给出决策")] + msgs,
            }
            # 把回测结果的原始数字单独存一份, 供前端"回测数据"栏展示,
            # 免得用户只能从决策文字里猜模型看了什么数。
            data = _extract_tool_payload(msgs)
            md = _format_backtest_md(data) if data else ""
            if md:
                result["backtest_analysis"] = md
            # 模型没调工具(或返回里没有回测数据)时如实说明, 而不是留空让人猜
            if data and "max_drawdown_pct" not in data and "_parse_error" not in data:
                result["backtest_analysis"] = (
                    "### 历史回测\n- 组合经理调用了回测工具但未取得有效数据: "
                    f"`{str(data)[:160]}`")
            return result
        except Exception as exc:
            err = f"投资组合经理执行失败: {type(exc).__name__}: {exc}"
            return {"final_decision": f"(失败) {err}",
                    "messages": _log(state, err), "error": err}

    return fundamental_analyst, technical_analyst, risk_manager, portfolio_manager


# =========================================================================== #
# 第六部分: LangGraph 编排
# =========================================================================== #
def build_graph(llm=None, backtest_years: int = DEFAULT_BACKTEST_YEARS):
    """构建并编译投资委员会的 StateGraph。

    拓扑:
        START ─┬─▶ fundamental_analyst ─┐
               ├─▶ technical_analyst   ─┼─▶ portfolio_manager ─▶ END
               └─▶ risk_manager        ─┘

    并行说明: 从 START 同时拉三条边, 三个节点会在同一个 superstep 里并行跑;
    它们都指向 portfolio_manager, LangGraph 会自动等三个都完成(barrier)才执行它。

    backtest_years: 传给组合经理的回测工具, 决定回测年限(前端下拉框/命令行控制)。
    """
    try:
        from langgraph.graph import END, START, StateGraph
    except ImportError as exc:
        raise ImportError(
            "缺少 langgraph, 请先安装:\n"
            "    pip install langgraph"
        ) from exc

    llm = llm or build_llm()
    fundamental_analyst, technical_analyst, risk_manager, portfolio_manager = \
        make_nodes(llm, backtest_years)

    graph = StateGraph(InvestState)
    graph.add_node("fundamental_analyst", fundamental_analyst)
    graph.add_node("technical_analyst", technical_analyst)
    graph.add_node("risk_manager", risk_manager)
    graph.add_node("portfolio_manager", portfolio_manager)

    # ---- 并行分支: START 同时指向三个分析师 ----
    graph.add_edge(START, "fundamental_analyst")
    graph.add_edge(START, "technical_analyst")
    graph.add_edge(START, "risk_manager")

    # ---- 汇聚: 三个分析师都指向组合经理(自动等待全部完成) ----
    graph.add_edge("fundamental_analyst", "portfolio_manager")
    graph.add_edge("technical_analyst", "portfolio_manager")
    graph.add_edge("risk_manager", "portfolio_manager")

    graph.add_edge("portfolio_manager", END)
    return graph.compile()


# =========================================================================== #
# 第七部分: 命令行入口
# =========================================================================== #
def check_tools(symbol: str, backtest_years: int = DEFAULT_BACKTEST_YEARS) -> int:
    """--tools-only: 只验证四个数据工具能否正常工作(不需要 API Key)。"""
    print("=" * 74)
    print("数据工具自检(不调用大模型)")
    print("=" * 74)
    print(f"  数据库: {DB_FILE}")
    print(f"  标的  : {symbol}")
    print(f"  回测年限: {backtest_years} 年")
    print("-" * 74)

    ok = True
    for label, func in (("fetch_fundamentals", lambda: _fetch_fundamentals_impl(symbol)),
                        ("fetch_technicals", lambda: _fetch_technicals_impl(symbol)),
                        ("fetch_market_risk", _fetch_market_risk_impl),
                        ("run_backtest",
                         lambda: _run_backtest_impl(symbol, backtest_years))):
        try:
            result = func()
            flag = "OK  " if result.get("ok") else "失败"
            print(f"  [{flag}] {label}")
            for k, v in result.items():
                print(f"          {k} = {v}")
            if not result.get("ok"):
                ok = False
        except Exception as exc:
            ok = False
            print(f"  [失败] {label}: {type(exc).__name__}: {exc}")
        print()

    print("=" * 74)
    print("工具自检结论:", "全部可用" if ok else "存在失败项, 请先排查数据库")
    if ok:
        print("接下来配置 API Key 后即可运行完整流程: python quant_agent.py --symbol", symbol)
    print("=" * 74)
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="基于 LangGraph 的多智能体投研决策系统")
    p.add_argument("--symbol", default=DEFAULT_SYMBOL, help=f"股票代码(默认 {DEFAULT_SYMBOL})")
    p.add_argument("--years", type=int, default=DEFAULT_BACKTEST_YEARS,
                   help=f"回测年限(默认 {DEFAULT_BACKTEST_YEARS}; 可用 1/3/5)")
    p.add_argument("--tools-only", action="store_true",
                   help="只自检数据工具, 不调用大模型(不需要 API Key)")
    args = p.parse_args(argv)

    if args.years < 1:
        print(f"[错误] --years 必须 >= 1, 当前 {args.years}")
        return 2

    if args.tools_only:
        return check_tools(args.symbol, args.years)

    # ---- 构建图(这里会校验 API Key) ----
    try:
        app = build_graph(backtest_years=args.years)
    except ImportError as exc:
        print(f"[错误] {exc}")
        return 1
    except RuntimeError as exc:
        print(f"[错误] {exc}")
        return 1
    except Exception as exc:
        print(f"[错误] 初始化失败: {type(exc).__name__}: {exc}")
        return 1

    print("=" * 74)
    print(f"投资委员会开始审议: {args.symbol}   (回测年限 {args.years} 年)")
    print("=" * 74)

    try:
        final_state = app.invoke(new_state(args.symbol))
    except Exception as exc:
        print(f"\n[错误] 执行失败: {type(exc).__name__}: {exc}")
        print("       常见原因: API Key 无效 / 网络不通 / 模型不支持工具调用。")
        return 1

    for field, title in (("fundamental_analysis", "基本面分析师"),
                         ("technical_analysis", "技术面分析师"),
                         ("risk_analysis", "风险管理员")):
        print()
        print("-" * 74)
        print(f"【{title}】")
        print("-" * 74)
        print(final_state.get(field) or "(无内容)")

    if final_state.get("backtest_analysis"):
        print()
        print("-" * 74)
        print("【历史回测数据(组合经理实际看到的数字)】")
        print("-" * 74)
        print(final_state["backtest_analysis"])

    print()
    print("=" * 74)
    print("【投资组合经理 · 最终决策】")
    print("=" * 74)
    print(final_state.get("final_decision") or "(无内容)")

    if final_state.get("error"):
        print()
        print(f"[注意] 过程中有错误: {final_state['error']}")

    print()
    print("=" * 74)
    print("以上为决策辅助输出, 不构成投资建议; 请自行判断风险。")
    print("=" * 74)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
