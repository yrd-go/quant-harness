#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
data_center.py — 量化交易本地数据层

把「股票池」的日线行情 + 核心基本面指标落地到本地 SQLite 数据库 quant_data.db。

设计要点
--------
1. 股票池: 1 只 ETF(510300, 走 fund_etf_hist_em) + 4 只个股。
2. 日期全部动态: 起始日来自 config.START_DATE, 结束日 = config.now_bj()(北京时间)。
   不再写死 END_DATE。
3. 两种运行模式:
   - 全量建库(默认): 从 config.START_DATE 拉到当前北京时间, DROP 重建两张表;
   - 增量更新(--update): 只拉"库内最后交易日 + 1"到当前北京时间, 用
     INSERT OR REPLACE 幂等写入, 然后执行滚动清理。
     非交易日(周末/节假日/今日未收盘)会优雅退出并 return 0, 不算错误。
4. 滚动清理: 每次写入后删除 trade_date < (当前北京时间 - HISTORY_YEARS 年) 的数据,
   保证数据库永远只有最近 N 年(默认 5 年 ≈ 1250 个交易日)。
5. 基本面(仅个股): PE(TTM)、PE(静)、PB、ROE, 存成【日频】。
   - PE/PB 来自 stock_value_em, 本身就是每日一个值;
   - ROE 来自 stock_financial_abstract, 是每季度披露的, 这里按报告期
     向前填充(forward fill)到每个交易日。这样回测时可直接按日期 join,
     且不会用到"当天还没披露"的财报数据, 避免前视偏差。
   - 增量模式下会额外往前多取 FUND_LOOKBACK_DAYS 天作为"前导期", 否则
     merge_asof 找不到窗口之前的报告期, 前几行的 ROE 会为空。
6. 存储: sqlite3, 两张表 daily_price / fundamentals。
7. 容灾: 每个数据源都带重试 + 多源兜底; 单只标的失败不影响其它标的;
   全流程日志同时输出到终端和 logs/data_center.log, 并且屏蔽第三方进度条刷屏。

用法
----
    python src/data_center.py                          # 全量建库(拉 START_DATE ~ 今天)
    python src/data_center.py --update                 # 增量更新 + 滚动清理(日常用这个)
    python src/data_center.py --update --dry-run       # 增量试跑, 不写库
    python src/data_center.py --update --history-years 3   # 临时改成保留 3 年
    python src/data_center.py --start 20240101         # 自定义全量起始日

依赖: akshare pandas
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

# 统一路径配置(集中放在项目根目录的 config.py 里)。
# 注意: 不能写成裸的 `from config import ...` —— 那个搜索的是"当前工作目录"
# (sys.path[0] 是脚本所在目录, 从别处启动时找不到 config.py)。
# 这里显式把 config.py 所在目录(项目根)加入 sys.path, 于是无论从哪个目录
# 启动、以及这些脚本以后被挪进几级子目录, 都能稳定找到 config。
_CONFIG_DIR = Path(__file__).resolve().parent
while not (_CONFIG_DIR / "config.py").exists() and _CONFIG_DIR != _CONFIG_DIR.parent:
    _CONFIG_DIR = _CONFIG_DIR.parent
if str(_CONFIG_DIR) not in sys.path:
    sys.path.insert(0, str(_CONFIG_DIR))

# noqa: E402 —— 必须在 sys.path 调整之后才能导入 config
from config import (
    CONFIG_DATA_DIR, DB_FILE, HISTORY_YEARS, LOGS_DIR, now_bj, today_bj_str,
)
from config import START_DATE as CFG_START_DATE

# --------------------------------------------------------------------------- #
# 配置区
# --------------------------------------------------------------------------- #
# START_DATE / HISTORY_YEARS / 时区 一律来自 config.py, 不再在这里写死。
# (data_center.py 以前自己定义了 START_DATE/END_DATE, 已删除以避免两处不一致)
ADJUST = "qfq"
DATA_LOG = LOGS_DIR / "data_center.log"      # 日志统一到 logs/ 目录

# 股票池: (代码, 名称, 类型)
#   type = "stock" -> stock_zh_a_hist / stock_zh_a_hist_tx
#   type = "etf"   -> fund_etf_hist_em / fund_etf_hist_sina
UNIVERSE: list[tuple[str, str, str]] = [
    ("510300", "沪深300ETF华泰柏瑞", "etf"),
    ("600519", "贵州茅台", "stock"),
    ("600036", "招商银行", "stock"),
    ("300750", "宁德时代", "stock"),
    ("601318", "中国平安", "stock"),
]

# 行情表需要的字段(其余列一律丢掉, 保持表结构稳定)
PRICE_COLUMNS = ["symbol", "name", "asset_type", "trade_date",
                 "open", "close", "high", "low", "volume", "amount",
                 "adjust", "source", "fetched_at"]

# 每次请求之间的间隔(秒)。东方财富对高频请求会直接断连, 这里主动限速。
REQUEST_INTERVAL = 1.0
RETRY_TIMES = 3
RETRY_BACKOFF = 3.0

# 增量取基本面时的"前导期"(天)。
# 原因: get_fundamentals 内部用 merge_asof(direction="backward") 往前找 ROE 报告期,
# 若只取很窄的增量窗口, 窗口之前的报告期带不进来, 前几行的 ROE 会是空的。
# 多取约 400 天(>1 年)足以覆盖最近 4 个季度报告期。
FUND_LOOKBACK_DAYS = 400

log = logging.getLogger("data_center")


# --------------------------------------------------------------------------- #
# 日志
# --------------------------------------------------------------------------- #
def setup_logging(verbose: bool = True) -> None:
    """终端 + 文件双写日志。禁止一切库往 stderr 刷屏。"""
    log.setLevel(logging.DEBUG)
    log.handlers.clear()

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S")

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(fmt)
    log.addHandler(console)

    try:
        # 用追加模式: data_center 是长期运行的滚动脚本, 每次覆盖会丢掉历史排查线索
        fh = logging.FileHandler(DATA_LOG, mode="a", encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)
        log.addHandler(fh)
    except Exception as exc:  # 日志文件写不了也不该让主流程挂掉
        log.warning("无法写入日志文件 %s: %s", DATA_LOG, exc)

    # 让 akshare/urllib3 的噪音日志闭嘴
    for noisy in ("urllib3", "akshare", "matplotlib", "chardet"):
        logging.getLogger(noisy).setLevel(logging.ERROR)


@contextlib.contextmanager
def suppress_tqdm():
    """屏蔽 akshare 内部的 tqdm 进度条。

    akshare 里写的是 ``from akshare.utils.tqdm import get_tqdm`` 再 ``tqdm = get_tqdm()``,
    所以必须改【调用方模块】命名空间里的引用, 且替换品仍要是"返回迭代器"的工厂函数。
    进度条纯属观感问题, 这里任何失败都不应影响主流程。
    """
    patched: list[tuple[object, str, object]] = []
    try:
        from akshare.utils import tqdm as ak_tqdm
        from akshare.stock_feature import stock_hist_tx

        def _disabled_get_tqdm(enable: bool = True):
            _ = enable  # 无论调用方传什么, 一律关掉
            return lambda iterable, *args, **kwargs: iterable

        for mod in (stock_hist_tx, ak_tqdm):
            if hasattr(mod, "get_tqdm"):
                patched.append((mod, "get_tqdm", getattr(mod, "get_tqdm")))
                setattr(mod, "get_tqdm", _disabled_get_tqdm)
    except Exception:
        patched = []

    try:
        yield
    finally:
        for mod, attr, original in patched:
            try:
                setattr(mod, attr, original)
            except Exception:
                pass


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def normalize_date(value) -> date | None:
    """把 akshare 返回的各种日期形态统一成 datetime.date。

    实测 stock_value_em 的 '数据日期' 是 datetime.date 对象而不是字符串,
    同时另一些接口返回 str / Timestamp, 所以这里统一兜住。
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    ts = pd.to_datetime(value, errors="coerce")
    if ts is None or pd.isna(ts):
        return None
    return ts.date()


def date_window(start: str, end: str) -> tuple[date, date]:
    """'20210101' / '2021-01-01' -> (date(2021,1,1), date(...))"""
    s = normalize_date(start)
    e = normalize_date(end)
    if s is None or e is None:
        raise ValueError(f"日期格式无法解析: start={start!r} end={end!r}")
    if s > e:
        raise ValueError(f"开始日期晚于结束日期: {s} > {e}")
    return s, e


def with_retry(func, *args, what: str = "", **kwargs):
    """带指数退避的重试包装。第 1 次失败等 3s, 第 2 次等 6s..."""
    last_exc: Exception | None = None
    for attempt in range(1, RETRY_TIMES + 1):
        try:
            return func(*args, **kwargs)
        except Exception as exc:
            last_exc = exc
            if attempt < RETRY_TIMES:
                wait = RETRY_BACKOFF * attempt
                log.debug("%s 第 %d/%d 次失败(%s), %.0fs 后重试",
                          what or getattr(func, "__name__", "?"),
                          attempt, RETRY_TIMES, type(exc).__name__, wait)
                time.sleep(wait)
    assert last_exc is not None
    raise last_exc


def market_prefix(symbol: str) -> str:
    """'600519' -> 'sh600519'; '000001' -> 'sz000001'; '510300' -> 'sh510300'

    注意: ETF 代码以 5 开头(沪市), 不能只用 6/9 判断沪市 —— 早期版本这里
    把 510300 判成了 sz510300, 结果新浪接口返回空数据。
    """
    return ("sh" if symbol.startswith(("5", "6", "9")) else "sz") + symbol


# --------------------------------------------------------------------------- #
# 行情抓取
# --------------------------------------------------------------------------- #
def fetch_price_eastmoney(ak, symbol: str, asset_type: str,
                          start: date, end: date) -> pd.DataFrame:
    """【当前未启用】东方财富行情源, 保留以便该接口恢复后快速切回。

    2026-10 实测: stock_zh_a_hist / fund_etf_hist_em 已稳定不可用
    (跨多标的、多日期范围约 10 次尝试全部 RemoteDisconnected)。
    get_price_history 因此不再调用本函数, 个股走腾讯、ETF 走新浪。
    若将来东财恢复, 只需在 get_price_history 的 sources 里把它加回去。
    """
    s, e = start.strftime("%Y%m%d"), end.strftime("%Y%m%d")
    if asset_type == "etf":
        return ak.fund_etf_hist_em(symbol=symbol, period="daily",
                                   start_date=s, end_date=e, adjust=ADJUST)
    return ak.stock_zh_a_hist(symbol=symbol, period="daily",
                              start_date=s, end_date=e, adjust=ADJUST)


def fetch_price_stock_tx(ak, symbol: str, start: date, end: date) -> pd.DataFrame:
    """数据源二(个股): 腾讯。字段英文, 量纲与东财一致。"""
    with suppress_tqdm():
        df = ak.stock_zh_a_hist_tx(symbol=market_prefix(symbol),
                                   start_date=start.strftime("%Y%m%d"),
                                   end_date=end.strftime("%Y%m%d"),
                                   adjust=ADJUST)
    return df.rename(columns={
        "date": "日期", "open": "开盘", "close": "收盘",
        "high": "最高", "low": "最低", "volume": "成交量", "amount": "成交额",
    })


def fetch_price_etf_sina(ak, symbol: str, start: date, end: date) -> pd.DataFrame:
    """数据源二(ETF): 新浪。

    注意: 新浪接口【不吃日期参数, 也不做复权】, 返回的是上市以来全部未复权数据。
    所以这里拉全量后本地按日期裁剪, 并把 adjust 标成 "" 以如实记录口径。
    """
    with suppress_tqdm():
        df = ak.fund_etf_hist_sina(symbol=market_prefix(symbol))
    df = df.rename(columns={"date": "日期", "open": "开盘", "close": "收盘",
                            "high": "最高", "low": "最低",
                            "volume": "成交量", "amount": "成交额"})
    df["_unadjusted"] = True
    return df


def normalize_price(df: pd.DataFrame, symbol: str, name: str, asset_type: str,
                    source: str, start: date, end: date) -> pd.DataFrame:
    """把任意数据源的结果整理成统一的行情表结构。"""
    if df is None or len(df) == 0:
        raise ValueError(f"{symbol} 在数据源 {source} 返回空数据")

    out = pd.DataFrame()
    out["trade_date"] = df["日期"].map(normalize_date)
    for col in ("开盘", "收盘", "最高", "最低", "成交量", "成交额"):
        out[col] = pd.to_numeric(df.get(col), errors="coerce")

    out = out.rename(columns={"开盘": "open", "收盘": "close", "最高": "high",
                             "最低": "low", "成交量": "volume", "成交额": "amount"})
    out = out.dropna(subset=["trade_date", "close"])
    out = out[(out["trade_date"] >= start) & (out["trade_date"] <= end)]

    if out.empty:
        raise ValueError(f"{symbol} 在数据源 {source} 裁剪到 {start}~{end} 后无数据")

    # 新浪 ETF 是未复权的, 如实记录口径, 不要假装成前复权
    unadjusted = bool(df["_unadjusted"].iloc[0]) if "_unadjusted" in df.columns else False
    out["symbol"] = symbol
    out["name"] = name
    out["asset_type"] = asset_type
    out["adjust"] = "" if unadjusted else ADJUST
    out["source"] = source
    out["fetched_at"] = now_bj().strftime("%Y-%m-%d %H:%M:%S")

    out = out.sort_values("trade_date").drop_duplicates("trade_date", keep="last")
    return out[PRICE_COLUMNS].reset_index(drop=True)


def get_price_history(ak, symbol: str, name: str, asset_type: str,
                      start: date, end: date) -> tuple[pd.DataFrame | None, str | None]:
    """抓取行情, 返回 (数据, 错误信息)。

    数据源选择(2026-10 实测后确定):
        个股 -> 腾讯 stock_zh_a_hist_tx
        ETF  -> 新浪 fund_etf_hist_sina
    为什么不再尝试东方财富:
        实测东财的行情接口(stock_zh_a_hist / fund_etf_hist_em)已稳定不可用 ——
        跨多个标的、多个日期范围共约 10 次尝试全部返回 RemoteDisconnected。
        每只标的在它上面白等约 3 秒(重试更久), 对日常增量更新纯属浪费, 因此彻底移除。
    重试仍然保留: 腾讯/新浪偶发抽风时, with_retry 会带退避重试。
    """
    if asset_type == "etf":
        src_name, func = "sina", lambda: fetch_price_etf_sina(ak, symbol, start, end)
    else:
        src_name, func = "tencent", lambda: fetch_price_stock_tx(ak, symbol, start, end)

    try:
        raw = with_retry(func, what=f"{symbol}@{src_name}")
        df = normalize_price(raw, symbol, name, asset_type, src_name, start, end)
        if df["adjust"].iloc[0] == "" and asset_type == "etf":
            log.warning("  %s 使用 %s, 该源不支持复权, 数据为未复权口径",
                        symbol, src_name)
        return df, None
    except Exception as exc:
        log.debug("  %s 数据源 %s 失败: %s", symbol, src_name, type(exc).__name__)
        return None, f"{src_name}: {type(exc).__name__}: {exc}"


# --------------------------------------------------------------------------- #
# 基本面抓取
# --------------------------------------------------------------------------- #
def fetch_valuation(ak, symbol: str, start: date, end: date) -> pd.DataFrame:
    """东方财富估值: 每日 PE(TTM) / PE(静) / PB / 市值。"""
    raw = with_retry(ak.stock_value_em, symbol=symbol, what=f"{symbol} value_em")
    if raw is None or raw.empty:
        raise ValueError("stock_value_em 返回空数据")

    df = pd.DataFrame()
    df["trade_date"] = raw["数据日期"].map(normalize_date)
    df["pe_ttm"] = pd.to_numeric(raw.get("PE(TTM)"), errors="coerce")
    df["pe_static"] = pd.to_numeric(raw.get("PE(静)"), errors="coerce")
    df["pb"] = pd.to_numeric(raw.get("市净率"), errors="coerce")
    df["total_mv"] = pd.to_numeric(raw.get("总市值"), errors="coerce")

    df = df.dropna(subset=["trade_date"])
    df = df[(df["trade_date"] >= start) & (df["trade_date"] <= end)]
    if df.empty:
        raise ValueError(f"{symbol} 估值数据在 {start}~{end} 内为空")
    return df.sort_values("trade_date").drop_duplicates("trade_date", keep="last")


def fetch_roe(ak, symbol: str, start: date, end: date) -> pd.DataFrame:
    """同花顺财务摘要: 取 ROE 序列(宽表 -> 长表)。

    返回列为 report_period(报告期, date) 和 roe。只保留报告期 <= 区间末的数据。
    """
    raw = with_retry(ak.stock_financial_abstract, symbol=symbol, what=f"{symbol} financial_abstract")
    if raw is None or raw.empty:
        raise ValueError("stock_financial_abstract 返回空数据")
    if "指标" not in raw.columns or "选项" not in raw.columns:
        raise ValueError(f"stock_financial_abstract 字段异常: {list(raw.columns)[:8]}")

    # 指标名在不同报告里可能出现在不同分组, 优先"常用指标"分组
    mask = raw["指标"].astype(str).str.strip() == "净资产收益率(ROE)"
    hit = raw[mask]
    if hit.empty:
        raise ValueError("未找到 '净资产收益率(ROE)' 指标")

    preferred = hit[hit["选项"].astype(str).str.strip() == "常用指标"]
    if not preferred.empty:
        hit = preferred
    hit = hit.drop_duplicates(subset=["指标"], keep="first")

    # 除 选项/指标 外的列名都是报告期, 如 '20251231'
    period_cols = [c for c in hit.columns if str(c).strip().isdigit()]
    if not period_cols:
        raise ValueError("未在财务摘要中找到报告期列")

    long = hit.melt(id_vars=["选项", "指标"], value_vars=period_cols,
                    var_name="report_period", value_name="roe")
    long["report_period"] = long["report_period"].map(normalize_date)
    long["roe"] = pd.to_numeric(long["roe"], errors="coerce")
    long = long.dropna(subset=["report_period", "roe"])
    long = long[long["report_period"] <= end]

    if long.empty:
        raise ValueError(f"{symbol} 在 {end} 之前没有可用的 ROE 数据")
    return long[["report_period", "roe"]].sort_values("report_period").reset_index(drop=True)


def get_fundamentals(ak, symbol: str, name: str,
                     start: date, end: date) -> tuple[pd.DataFrame | None, str | None]:
    """日频基本面: 每日 PE/PB + 最近一期已披露的 ROE。"""
    errors: list[str] = []

    try:
        val = fetch_valuation(ak, symbol, start, end)
    except Exception as exc:
        return None, f"估值(PE/PB)失败: {type(exc).__name__}: {exc}"

    try:
        roe = fetch_roe(ak, symbol, start, end)
    except Exception as exc:
        # ROE 挂了但 PE/PB 拿到了, 仍然入库, ROE 留空并记录警告
        log.warning("  %s 的 ROE 获取失败, 该列将留空: %s", symbol, type(exc).__name__)
        errors.append(f"ROE失败: {type(exc).__name__}: {exc}")
        roe = pd.DataFrame(columns=["report_period", "roe"])

    df = val.sort_values("trade_date").copy()

    if not roe.empty:
        # 关键: 用 merge_asof 做"按报告期向后填充", 且只取 <= 当前交易日的报告期。
        # report_period 是报告期末(如 2025-12-31), 用它当日即可用的近似;
        # 更严格的可用日应考虑披露滞后, 这里保持简单并把这个口径写进注释。
        #
        # 注意: 本地日期是 datetime.date 对象, dtype 为 object, 而 pandas 3.x 的
        # merge_asof 要求两侧都是 datetime64, 所以这里必须先显式转换。
        left = df.sort_values("trade_date").copy()
        right = roe.sort_values("report_period").copy()
        left["_d"] = pd.to_datetime(left["trade_date"])
        right["_d"] = pd.to_datetime(right["report_period"])
        df = pd.merge_asof(left, right, on="_d", direction="backward")
        df = df.drop(columns=["_d"])
        df = df.rename(columns={"report_period": "roe_report_period"})
    else:
        df["roe"] = pd.NA
        df["roe_report_period"] = pd.NaT

    df["symbol"] = symbol
    df["name"] = name
    df["asset_type"] = "stock"
    df["pe_ttm"] = pd.to_numeric(df["pe_ttm"], errors="coerce")
    df["pe_static"] = pd.to_numeric(df["pe_static"], errors="coerce")
    df["pb"] = pd.to_numeric(df["pb"], errors="coerce")
    df["roe"] = pd.to_numeric(df["roe"], errors="coerce")
    df["fetched_at"] = now_bj().strftime("%Y-%m-%d %H:%M:%S")

    cols = ["symbol", "name", "asset_type", "trade_date", "pe_ttm", "pe_static",
            "pb", "roe", "roe_report_period", "total_mv", "fetched_at"]
    df = df.dropna(subset=["trade_date"]).sort_values("trade_date")
    df = df.drop_duplicates("trade_date", keep="last")
    return df[cols].reset_index(drop=True), (" | ".join(errors) if errors else None)


# --------------------------------------------------------------------------- #
# 数据库
# --------------------------------------------------------------------------- #
DDL_DAILY_PRICE = """
CREATE TABLE daily_price (
    symbol       TEXT    NOT NULL,
    name         TEXT,
    asset_type   TEXT,
    trade_date   DATE    NOT NULL,
    open         REAL,
    close        REAL,
    high         REAL,
    low          REAL,
    volume       REAL,
    amount       REAL,
    adjust       TEXT,
    source       TEXT,
    fetched_at   TEXT,
    PRIMARY KEY (symbol, trade_date)
)
"""

DDL_FUNDAMENTALS = """
CREATE TABLE fundamentals (
    symbol            TEXT NOT NULL,
    name              TEXT,
    asset_type        TEXT,
    trade_date        DATE NOT NULL,
    pe_ttm            REAL,
    pe_static         REAL,
    pb                REAL,
    roe               REAL,
    roe_report_period DATE,
    total_mv          REAL,
    fetched_at        TEXT,
    PRIMARY KEY (symbol, trade_date)
)
"""


def connect(db_path: str = DB_FILE) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def rebuild_tables(conn: sqlite3.Connection) -> None:
    """DROP 后重建两张表, 保证每次运行都是干净状态(可重复运行)。"""
    cur = conn.cursor()
    cur.execute("DROP TABLE IF EXISTS daily_price")
    cur.execute("DROP TABLE IF EXISTS fundamentals")
    cur.execute(DDL_DAILY_PRICE)
    cur.execute(DDL_FUNDAMENTALS)
    cur.execute("CREATE INDEX idx_price_symbol ON daily_price(symbol)")
    cur.execute("CREATE INDEX idx_fund_symbol ON fundamentals(symbol)")
    conn.commit()
    log.info("已重建表: daily_price, fundamentals")


def write_price(conn: sqlite3.Connection, df: pd.DataFrame) -> int:
    if df is None or df.empty:
        return 0
    df.to_sql("daily_price", conn, if_exists="append", index=False)
    conn.commit()
    return len(df)


def write_fundamentals(conn: sqlite3.Connection, df: pd.DataFrame) -> int:
    if df is None or df.empty:
        return 0
    # sqlite3 不认识 NaT, 统一转成 None, 否则写入会报错
    out = df.copy()
    out["roe_report_period"] = out["roe_report_period"].map(
        lambda v: v.strftime("%Y-%m-%d") if isinstance(v, (date, datetime)) and not pd.isna(v) else None
    )
    out.to_sql("fundamentals", conn, if_exists="append", index=False)
    conn.commit()
    return len(out)


# --------------------------------------------------------------------------- #
# 增量更新所需的辅助函数
# --------------------------------------------------------------------------- #
def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return bool(conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?",
        (name,)).fetchone()[0])


def last_stored_date(conn: sqlite3.Connection, table: str) -> date | None:
    """取某张表里已入库的最大交易日。

    只统计有实际行情数据的行。返回 datetime.date 或 None(表为空/没有数据)。
    """
    if not table_exists(conn, table):
        return None
    row = conn.execute(f"SELECT MAX(trade_date) FROM {table}").fetchone()
    if not row or row[0] is None:
        return None
    parsed = pd.to_datetime(row[0], errors="coerce")
    if parsed is None or pd.isna(parsed):
        return None
    return parsed.date()


def replace_price_rows(conn: sqlite3.Connection, df: pd.DataFrame) -> int:
    """增量写入行情: 用 INSERT OR REPLACE 保证重复日期不报错(幂等)。

    为什么不用 to_sql(if_exists="append"): 表有 PRIMARY KEY(symbol, trade_date),
    重复插入会直接抛 IntegrityError。增量模式下"最后一天"被重复拉到很常见,
    所以必须显式处理冲突。
    """
    if df is None or df.empty:
        return 0
    sql = """
    INSERT OR REPLACE INTO daily_price
      (symbol, name, asset_type, trade_date, open, close, high, low,
       volume, amount, adjust, source, fetched_at)
    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
    """
    rows = [
        (r.symbol, r.name, r.asset_type,
         r.trade_date.strftime("%Y-%m-%d") if isinstance(r.trade_date, (date, datetime))
         else str(r.trade_date),
         r.open, r.close, r.high, r.low, r.volume, r.amount,
         r.adjust, r.source, r.fetched_at)
        for r in df.itertuples(index=False)
    ]
    conn.executemany(sql, rows)
    conn.commit()
    return len(rows)


def replace_fundamental_rows_in(conn: sqlite3.Connection, df: pd.DataFrame) -> int:
    """增量写入基本面: 同样用 INSERT OR REPLACE 保证幂等。"""
    if df is None or df.empty:
        return 0
    out = df.copy()
    # sqlite3 不认识 NaT/date, 统一转字符串或 None
    out["roe_report_period"] = out["roe_report_period"].map(
        lambda v: v.strftime("%Y-%m-%d")
        if isinstance(v, (date, datetime)) and not pd.isna(v) else None)
    out["trade_date"] = out["trade_date"].map(
        lambda v: v.strftime("%Y-%m-%d")
        if isinstance(v, (date, datetime)) and not pd.isna(v) else None)
    sql = """
    INSERT OR REPLACE INTO fundamentals
      (symbol, name, asset_type, trade_date, pe_ttm, pe_static, pb, roe,
       roe_report_period, total_mv, fetched_at)
    VALUES (?,?,?,?,?,?,?,?,?,?,?)
    """
    rows = [
        (r.symbol, r.name, r.asset_type, r.trade_date, r.pe_ttm, r.pe_static,
         r.pb, r.roe, r.roe_report_period, r.total_mv, r.fetched_at)
        for r in out.itertuples(index=False)
    ]
    conn.executemany(sql, rows)
    conn.commit()
    return len(rows)


def last_stored_date_for_symbol(conn: sqlite3.Connection, table: str,
                                symbol: str) -> date | None:
    """取【指定标的】在库里的最后交易日。

    为什么要单独有这个函数: 原 last_stored_date() 是 SELECT MAX(trade_date)
    (全表全局)。用它当基准会让"新加入的标的"错误地沿用别的标的的最后日期,
    于是只拉到几天数据而不是完整的 5 年历史。逐标的基准是第四步的核心修复。
    """
    if not table_exists(conn, table):
        return None
    row = conn.execute(
        f"SELECT MAX(trade_date) FROM {table} WHERE symbol = ?", (symbol,)).fetchone()
    if not row or row[0] is None:
        return None
    parsed = pd.to_datetime(row[0], errors="coerce")
    if parsed is None or pd.isna(parsed):
        return None
    return parsed.date()


def compute_fetch_start(conn: sqlite3.Connection, table: str, symbol: str,
                        history_years: int, today: date,
                        existing_last: date | None = None,
                        symbol_label: str = "") -> tuple[date, bool]:
    """算某个标的应该从哪一天开始拉取。

    返回 (fetch_start, is_new)。

    规则:
      库里有该标的 -> fetch_start = 该标的最后交易日 + 1 天      (真正的增量)
      库里没有       -> fetch_start = 今天 - history_years 年     (新标的, 全量补齐)

    为什么"库里没有"要回退到 N 年前, 而不是从 START_DATE 开始:
      需求要求"缺失的最近 5 年数据" —— 滚动窗口本来也只保留 5 年,
      拉更早的数据会被随后的滚动清理立刻删掉, 纯属浪费时间。
    """
    last = existing_last if existing_last is not None else \
        last_stored_date_for_symbol(conn, table, symbol)

    if last is not None:
        return last + timedelta(days=1), False

    now = now_bj()
    try:
        start = (now - pd.DateOffset(years=history_years)).date()
    except Exception:
        start = today - timedelta(days=365 * history_years)
    log.info("  新增标的 %s, 库中无历史 -> 首次入库, 从 %s 起拉取最近 %d 年",
             symbol_label or symbol, start, history_years)
    return start, True


def resolve_universe(args: argparse.Namespace) -> tuple[list[tuple[str, str, str]], str]:
    """根据命令行参数决定本次要处理哪些标的。

    优先级: --symbol > --pool > 内置 UNIVERSE
    返回 (标的列表, 来源说明)。标的元素为 (代码, 名称, 类型) 三元组。

    注意: --pool 必须【显式】传入。不传时保持原 UNIVERSE 兜底, 绝不默认去读
    target_pool.json —— 否则 `python src/data_center.py` 会从"全量建库 5 只"
    静默变成"补齐 15 只新标的", 行为突变且耗时从几秒变成几十分钟。
    """
    # ---- 1) --symbol: 临时只装这一只 ----
    if args.symbol:
        sym = str(args.symbol).strip().zfill(6)
        kind = "etf" if sym.startswith(("5", "1")) else "stock"
        log.info("--symbol 指定单只标的: %s (类型推断为 %s)", sym, kind)
        return [(sym, sym, kind)], f"--symbol {sym}"

    # ---- 2) --pool: 从 JSON 读 ----
    if args.pool is not None:
        pool_path = Path(args.pool) if args.pool else (CONFIG_DATA_DIR / "target_pool.json")
        if not pool_path.is_absolute():
            pool_path = Path.cwd() / pool_path
        if not pool_path.exists():
            raise FileNotFoundError(
                f"找不到选股池文件: {pool_path}\n"
                f"       请先运行前两层生成它: "
                f"python src/market_screener.py && python src/news_agent.py\n"
                f"       或直接指定路径: --pool <文件路径>"
            )
        payload = json.loads(pool_path.read_text(encoding="utf-8"))

        # 兼容三种结构: {"pool":[...]} / {"candidates":[...]} / 裸列表 [...]
        if isinstance(payload, list):
            items = payload
        else:
            items = payload.get("pool") or payload.get("candidates") or []
        if not items:
            raise ValueError(f"{pool_path.name} 里没有任何标的(pool/candidates 都为空)")

        universe: list[tuple[str, str, str]] = []
        seen: set[str] = set()
        for it in items:
            sym = str(it.get("symbol") or "").strip().zfill(6)
            if not sym or sym in seen:
                continue
            seen.add(sym)
            name = it.get("name") or sym      # 新标的可能没名字, 先用代码占位
            kind = "etf" if sym.startswith(("5", "1")) else "stock"
            universe.append((sym, name, kind))
        if not universe:
            raise ValueError(f"{pool_path.name} 里没解析出任何有效代码")
        return universe, f"{pool_path.name}({len(universe)} 只)"

    # ---- 3) 兜底: 原内置 UNIVERSE ----
    return list(UNIVERSE), f"内置 UNIVERSE({len(UNIVERSE)} 只)"


def rolling_cleanup(conn: sqlite3.Connection, history_years: int,
                    now=None) -> tuple[str, int, int]:
    """滚动清理: 删除早于 (当前北京时间 - N 年) 的数据。

    返回 (截止日期字符串, daily_price 删除行数, fundamentals 删除行数)。

    为什么用"日历减 N 年"而不是"交易日数量": 需求明确要求"最近5年(约1250个交易日)",
    日历减 5 年 ≈ 1250 个交易日, 两者等价且日历法可复现、不依赖交易日历。
    """
    now = now or now_bj()
    try:
        cutoff = now.replace(year=now.year - history_years)
    except ValueError:
        # 2月29日 减去整年会越界, 退到 2月28日
        cutoff = now.replace(year=now.year - history_years, day=28)
    cutoff_str = cutoff.strftime("%Y-%m-%d")

    deleted: list[int] = []
    for tbl in ("daily_price", "fundamentals"):
        if not table_exists(conn, tbl):
            deleted.append(0)
            continue
        cur = conn.execute(f"DELETE FROM {tbl} WHERE trade_date < ?", (cutoff_str,))
        deleted.append(cur.rowcount if cur.rowcount >= 0 else 0)
    conn.commit()
    return cutoff_str, deleted[0], deleted[1]


# --------------------------------------------------------------------------- #
# --update 增量模式
# --------------------------------------------------------------------------- #
def _backfill_universe(ak, conn: sqlite3.Connection, targets, history_years: int,
                       dry_run: bool = False, mode_label: str = "增量更新",
                       require_new_rows: bool = True,
                       require_existing_db: bool = True) -> int:
    """逐标的补齐内核: 按【每个标的自己的缺口】拉数据, 再写入并滚动清理。

    这是第四步的核心。关键修复: 基准必须【逐标的】而不是全局。
    用全局基准 SELECT MAX(trade_date) 会让新加入的标的错误地沿用别的标的的最后日期,
    于是只拉到几天数据, 而不是完整的 N 年历史。

    两种调用方:
      --update      : 目标是内置池, 期望"今天没有新数据"时优雅退出(非交易日)
      --pool/--symbol: 目标是动态池, 新标的会从 N 年前开始补齐

    返回进程退出码。
    """
    today = now_bj().date()

    if require_existing_db and not table_exists(conn, "daily_price"):
        log.warning("daily_price 表不存在, %s 无从进行。", mode_label)
        log.warning("请先执行一次全量建库: python src/data_center.py")
        return 0

    # ---- 逐标的算缺口 ----
    # plan 元素: (symbol, name, asset_type, fetch_start, is_new, existing_last)
    plan = []
    for (symbol, name, asset_type) in targets:
        last = last_stored_date_for_symbol(conn, "daily_price", symbol)
        if last is not None and last >= today:
            # 该标的已是最新 -> 无需拉取(非交易日/今天还没收盘都属于这种)
            plan.append((symbol, name, asset_type, None, False, last))
            continue
        fetch_start, is_new = compute_fetch_start(
            conn, "daily_price", symbol, history_years, today,
            existing_last=last, symbol_label=name or symbol)
        plan.append((symbol, name, asset_type, fetch_start, is_new, last))

    todo = [p for p in plan if p[3] is not None]
    skipped = [p for p in plan if p[3] is None]
    new_syms = [p[0] for p in plan if p[4]]

    log.info("=" * 68)
    log.info("%s | 当前北京时间 %s | 保留 %d 年", mode_label,
             now_bj().strftime("%Y-%m-%d %H:%M:%S"), history_years)
    log.info("标的 %d 个: 待拉取 %d, 已是最新 %d, 其中新增标的 %d 个",
             len(plan), len(todo), len(skipped), len(new_syms))
    if new_syms:
        log.info("  新增标的(库里没有, 将首次补齐 %d 年): %s",
                 history_years, ", ".join(new_syms))
    # 时间预估: 让用户能判断"还要等多久", 而不是以为卡死了
    if todo:
        est_sec = len(todo) * 3.0 + len(todo) * 62 * REQUEST_INTERVAL
        log.info("  预计耗时约 %.1f 分钟 (每只约 %.1f 分钟: 62 次K线请求 + 限速 %.1fs)",
                 est_sec / 60, (3.0 + 62 * REQUEST_INTERVAL) / 60, REQUEST_INTERVAL)
    log.info("=" * 68)

    if not todo and require_new_rows:
        log.info("-" * 68)
        log.info("无新数据可更新, 属于非交易日(周末/节假日/今日尚未收盘)。")
        if skipped:
            log.info("  例: 库内最后交易日 = %s, 当前北京日期 = %s",
                     skipped[0][5], today)
        log.info("  这是正常情况, 不视为错误。")
        log.info("-" * 68)
        if not dry_run:
            cutoff_str, d1, d2 = rolling_cleanup(conn, history_years)
            log.info("滚动清理: 删除 trade_date < %s 的数据 "
                     "(daily_price %d 行, fundamentals %d 行)", cutoff_str, d1, d2)
        return 0

    price_frames: list[pd.DataFrame] = []
    fund_frames: list[pd.DataFrame] = []
    failures: list[str] = []
    new_price_rows = 0

    # ---- 逐标的拉取(失败隔离: 单只失败不影响其它) ----
    for idx, (symbol, name, asset_type, fetch_start, is_new, last) in enumerate(
            [(p[0], p[1], p[2], p[3], p[4], p[5]) for p in todo], 1):
        log.info("[%d/%d] %s %s (%s)%s", idx, len(todo), symbol, name, asset_type,
                 "  <- 新增标的, 首次入库" if is_new else "")

        # 行情
        try:
            df, err = get_price_history(ak, symbol, name, asset_type, fetch_start, today)
        except Exception as exc:
            df, err = None, f"{type(exc).__name__}: {exc}"

        sym_new = 0
        if err or df is None:
            log.error("  行情获取失败: %s", err)
            failures.append(f"{symbol} 行情: {err}")
        else:
            # 裁掉已入库的部分(数据源可能返回比请求更早的行)
            before = len(df)
            if last is not None:
                df = df[df["trade_date"] > last].reset_index(drop=True)
            if df.empty:
                log.info("  行情: 暂无新交易日数据(可能停牌)")
            else:
                sym_new = len(df)
                new_price_rows += sym_new
                if is_new:
                    log.info("  新增标的 %s, 首次入库 %d 行  (%s ~ %s)",
                             symbol, sym_new, df["trade_date"].min(),
                             df["trade_date"].max())
                else:
                    log.info("  行情 OK  %d 行  %s ~ %s  (source=%s)",
                             sym_new, df["trade_date"].min(), df["trade_date"].max(),
                             df["source"].iloc[0])
                if not dry_run:
                    price_frames.append(df)

        # 基本面(仅个股)。注意条件用 (is_new or sym_new>0):
        # 新标的即使行情为空也要尝试拉基本面, 否则会缺 PE/PB/ROE。
        if asset_type == "stock" and (is_new or sym_new > 0):
            time.sleep(REQUEST_INTERVAL)
            # 往前多取 FUND_LOOKBACK_DAYS 天作为"前导期":
            # get_fundamentals 内部用 merge_asof(direction="backward") 向前找 ROE 报告期,
            # 窗口太窄会导致前几行 ROE 为空。
            lead_start = fetch_start - timedelta(days=FUND_LOOKBACK_DAYS)
            try:
                fdf, ferr = get_fundamentals(ak, symbol, name, lead_start, today)
            except Exception as exc:
                fdf, ferr = None, f"{type(exc).__name__}: {exc}"
            if fdf is None:
                log.error("  基本面获取失败: %s", ferr)
                failures.append(f"{symbol} 基本面: {ferr}")
            else:
                n_all = len(fdf)
                if last is not None:
                    fdf = fdf[fdf["trade_date"] > last].reset_index(drop=True)
                n_roe = int(fdf["roe"].notna().sum()) if not fdf.empty else 0
                n_pe = int(fdf["pe_ttm"].notna().sum()) if not fdf.empty else 0
                log.info("  基本面 OK  %d 行(前导 %d 行用于 ROE 回填)  PE/PB 非空 %d, ROE 非空 %d",
                         len(fdf), max(n_all - len(fdf), 0), n_pe, n_roe)
                if not dry_run and not fdf.empty:
                    fund_frames.append(fdf)
        elif asset_type == "stock":
            log.info("  基本面 跳过(该标的无新行情)")
        else:
            log.info("  基本面 跳过 (ETF 无 PE/PB/ROE)")

        time.sleep(REQUEST_INTERVAL)

    # ---- --update 模式: 所有标的都没新数据 -> 非交易日, 优雅退出 ----
    if require_new_rows and new_price_rows == 0:
        log.info("-" * 68)
        log.info("无新数据可更新, 属于非交易日(周末/节假日/今日尚未收盘)。")
        log.info("  所有标的都未返回新行, 不视为错误。")
        log.info("-" * 68)
        if not dry_run:
            cutoff_str, d1, d2 = rolling_cleanup(conn, history_years)
            log.info("滚动清理: 删除 trade_date < %s 的数据 "
                     "(daily_price %d 行, fundamentals %d 行)", cutoff_str, d1, d2)
        return 0

    # ---- 写入 ----
    written_price = written_fund = 0
    if dry_run:
        log.info("dry-run: 不写库(本应写入 行情 %d 行, 基本面 %d 标的)",
                 sum(len(d) for d in price_frames), len(fund_frames))
    else:
        try:
            for df in price_frames:
                written_price += replace_price_rows(conn, df)
            for fdf in fund_frames:
                written_fund += replace_fundamental_rows_in(conn, fdf)
            log.info("写入完成: daily_price %d 行, fundamentals %d 行",
                     written_price, written_fund)
        except sqlite3.Error as exc:
            log.error("写库失败: %s", exc)
            return 1

        cutoff_str, d1, d2 = rolling_cleanup(conn, history_years)
        log.info("滚动清理: 删除 trade_date < %s 的数据 "
                 "(daily_price %d 行, fundamentals %d 行)", cutoff_str, d1, d2)

    # ---- 汇总 ----
    log.info("=" * 68)
    log.info("%s完成: 行情 +%d 行 | 基本面 +%d 行 | 成功标的 %d/%d",
             mode_label, written_price, written_fund,
             len(todo) - len(failures), len(todo))
    for f in failures:
        log.error("失败: %s", f)
    log.info("库内最新交易日 = %s", last_stored_date(conn, "daily_price"))
    log.info("=" * 68)

    if not dry_run:
        try:
            verify_db(DB_FILE)
        except Exception as exc:
            log.warning("自检失败(不影响数据入库): %s", exc)

    # 只要写了数据(或 dry-run)就算成功; 部分标的失败不算整体失败
    return 0 if (written_price or dry_run) else 1


def run_update(ak, conn: sqlite3.Connection, history_years: int,
               dry_run: bool = False) -> int:
    """--update: 对内置 UNIVERSE 做增量更新 + 滚动清理。

    非交易日/无新数据时优雅退出(return 0), 这是需求里的硬约束。
    实际逻辑已抽到 _backfill_universe, 与 --pool/--symbol 共用同一套逐标的基准。
    """
    return _backfill_universe(
        ak, conn, list(UNIVERSE), history_years, dry_run=dry_run,
        mode_label="增量更新", require_new_rows=True, require_existing_db=True)


def run_backfill(ak, conn: sqlite3.Connection, targets, history_years: int,
                 source_label: str, dry_run: bool = False) -> int:
    """--pool / --symbol: 按需补齐动态股票池的数据。

    与 --update 的区别:
      - 用 --pool/--symbol 指定的标的(而不是内置 UNIVERSE);
      - 新标的会自动从 HISTORY_YEARS 年前开始补齐(由 compute_fetch_start 决定);
      - 不做"非交易日整体退出"的判断: 因为新标的即使今天没有新K线,
        它需要的 5 年历史依然要拉, 不能因为"今天没新数据"就整体跳过。
    """
    return _backfill_universe(
        ak, conn, targets, history_years, dry_run=dry_run,
        mode_label=f"补齐模式({source_label})",
        require_new_rows=False, require_existing_db=False)

def verify_db(db_path: str = DB_FILE) -> None:
    """回读数据库做个自检, 把概要和口径打到日志里。"""
    conn = connect(db_path)
    try:
        log.info("-" * 68)
        log.info("数据库自检: %s", Path(db_path).resolve())

        for table, date_col in (("daily_price", "trade_date"),
                                ("fundamentals", "trade_date")):
            total = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            log.info("[%s] 总行数 = %d", table, total)
            rows = conn.execute(
                f"SELECT symbol, COUNT(*) n, MIN({date_col}) a, MAX({date_col}) b "
                f"FROM {table} GROUP BY symbol ORDER BY symbol"
            ).fetchall()
            for sym, n, a, b in rows:
                log.info("   %-8s %5d 行  %s ~ %s", sym, n, a, b)
            if total == 0:
                log.warning("   (空表)")

        # 复权口径必须可见, 否则回测结果没法解释
        log.info("复权口径检查:")
        for sym, adj, src, n in conn.execute(
                "SELECT symbol, adjust, source, COUNT(*) FROM daily_price "
                "GROUP BY symbol, adjust, source ORDER BY symbol").fetchall():
            flag = "前复权" if adj == ADJUST else "!! 未复权/其他"
            log.info("   %-8s adjust=%-4s source=%-10s %5d 行  [%s]",
                     sym, repr(adj), src, n, flag)

        roe_ok = conn.execute(
            "SELECT COUNT(*) FROM fundamentals WHERE roe IS NOT NULL").fetchone()[0]
        fund_total = conn.execute("SELECT COUNT(*) FROM fundamentals").fetchone()[0]
        log.info("ROE 覆盖: %d/%d 行非空", roe_ok, fund_total)
        log.info("-" * 68)
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="量化数据层: 抓取行情+基本面并落地 SQLite",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--db", default=DB_FILE, help="数据库路径")

    # ---- 模式 ----
    p.add_argument("--update", action="store_true",
                   help="增量更新模式: 只拉'库内最后交易日+1'到当前北京时间, 然后滚动清理")

    # ---- 动态股票池(第四步新增) ----
    p.add_argument("--pool", nargs="?", const="", default=None,
                   help="从 JSON 选股池拉数据(不带值则用 config/target_pool.json)。"
                        "传入本参数会进入【补齐模式】: 新标的自动补齐 HISTORY_YEARS 年, "
                        "绝不 DROP 重建")
    p.add_argument("--symbol", default=None,
                   help="只拉取指定的单只股票, 例如 --symbol 600418(同样走补齐模式)")

    # ---- 全量模式的日期(不传 --update 时生效) ----
    p.add_argument("--start", default=CFG_START_DATE,
                   help=f"全量建库的起始日期(默认 config.START_DATE={CFG_START_DATE})")
    p.add_argument("--end", default=None,
                   help="结束日期; 默认 = 当前北京时间(动态, 见 config.now_bj())")

    # ---- 滚动窗口 ----
    p.add_argument("--history-years", type=int, default=HISTORY_YEARS,
                   help="数据保留年限, 早于此窗口的数据会被滚动删除")

    p.add_argument("--dry-run", action="store_true", help="只抓取不写库")
    p.add_argument("--quiet", action="store_true", help="只输出 INFO 及以上日志")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    setup_logging(verbose=not args.quiet)

    if args.history_years < 1:
        log.error("--history-years 必须 >= 1, 当前 %d", args.history_years)
        return 2

    # ---- 决定本次处理哪些标的(第四步: 支持 --symbol / --pool) ----
    # 注意: 传了 --pool/--symbol 就隐含【补齐模式】, 绝不能落到下面的全量 DROP 重建,
    # 否则会把库里已有的标的全部删掉。
    is_backfill = bool(args.symbol) or (args.pool is not None)
    if is_backfill:
        try:
            targets, universe_label = resolve_universe(args)
        except (FileNotFoundError, ValueError) as exc:
            log.error("%s", exc)
            return 2
    else:
        targets, universe_label = list(UNIVERSE), f"内置 UNIVERSE({len(UNIVERSE)} 只)"

    # ---- 结束日期动态取当前北京时间(不再写死 20260930) ----
    effective_end = args.end if args.end else today_bj_str()
    try:
        start, end = date_window(args.start, effective_end)
    except ValueError as exc:
        log.error("日期参数有误: %s", exc)
        return 2

    mode_name = ("补齐模式(--symbol/--pool)" if is_backfill
                 else ("增量更新(--update)" if args.update else "全量建库"))

    log.info("=" * 68)
    log.info("量化数据层启动 | 模式 %s | 当前北京时间 %s", mode_name,
             now_bj().strftime("%Y-%m-%d %H:%M:%S"))
    log.info("标的来源: %s", universe_label)
    if not args.update and not is_backfill:
        log.info("全量区间 %s ~ %s | 前复权 | 股票池 %d 个标的",
                 start, end, len(targets))
    log.info("=" * 68)

    try:
        import akshare as ak
    except ImportError:
        log.error("未安装 akshare, 请执行: pip install akshare")
        return 1

    # ---- 补齐模式(--symbol / --pool) ----
    if is_backfill:
        try:
            conn = connect(args.db)
        except sqlite3.Error as exc:
            log.error("无法打开数据库 %s: %s", args.db, exc)
            return 1
        try:
            return run_backfill(ak, conn, targets, args.history_years,
                                universe_label, dry_run=args.dry_run)
        except sqlite3.Error as exc:
            log.error("补齐时数据库出错: %s", exc)
            return 1
        except Exception as exc:
            log.error("补齐失败: %s: %s", type(exc).__name__, exc)
            import traceback
            traceback.print_exc()
            return 1
        finally:
            conn.close()

    # ---- 增量模式: 走完全不同的分支 ----
    if args.update:
        try:
            conn = connect(args.db)
        except sqlite3.Error as exc:
            log.error("无法打开数据库 %s: %s", args.db, exc)
            return 1
        try:
            return run_update(ak, conn, args.history_years, dry_run=args.dry_run)
        except sqlite3.Error as exc:
            log.error("增量更新时数据库出错: %s", exc)
            return 1
        except Exception as exc:
            log.error("增量更新失败: %s: %s", type(exc).__name__, exc)
            import traceback
            traceback.print_exc()
            return 1
        finally:
            conn.close()

    price_frames: list[pd.DataFrame] = []
    fund_frames: list[pd.DataFrame] = []
    failures: list[str] = []
    warnings: list[str] = []

    for idx, (symbol, name, asset_type) in enumerate(targets, 1):
        log.info("[%d/%d] %s %s (%s)", idx, len(targets), symbol, name, asset_type)

        # ---- 行情 ----
        df, err = get_price_history(ak, symbol, name, asset_type, start, end)
        if err:
            log.error("  行情获取失败: %s", err)
            failures.append(f"{symbol} 行情: {err}")
        else:
            assert df is not None
            log.info("  行情 OK  %d 行  %s ~ %s  (source=%s, adjust=%s)",
                     len(df), df["trade_date"].min(), df["trade_date"].max(),
                     df["source"].iloc[0], df["adjust"].iloc[0] or "未复权")
            price_frames.append(df)

        # ---- 基本面(仅个股; ETF 没有 PE/PB/ROE, 按约定跳过) ----
        if asset_type == "stock":
            time.sleep(REQUEST_INTERVAL)
            fdf, ferr = get_fundamentals(ak, symbol, name, start, end)
            if fdf is None:
                log.error("  基本面获取失败: %s", ferr)
                failures.append(f"{symbol} 基本面: {ferr}")
            else:
                n_roe = int(fdf["roe"].notna().sum())
                log.info("  基本面 OK  %d 行  PE/PB 非空 %d, ROE 非空 %d",
                         len(fdf), int(fdf["pe_ttm"].notna().sum()), n_roe)
                fund_frames.append(fdf)
                if ferr:
                    warnings.append(f"{symbol}: {ferr}")
        else:
            log.info("  基本面 跳过 (ETF 无 PE/PB/ROE)")

        time.sleep(REQUEST_INTERVAL)

    # ---- 落库 ----
    total_price = total_fund = 0
    if args.dry_run:
        log.info("dry-run 模式: 不写数据库")
        total_price = sum(len(d) for d in price_frames)
        total_fund = sum(len(d) for d in fund_frames)
    else:
        try:
            conn = connect(args.db)
        except sqlite3.Error as exc:
            log.error("无法打开数据库 %s: %s", args.db, exc)
            return 1
        try:
            rebuild_tables(conn)
            for df in price_frames:
                total_price += write_price(conn, df)
            for df in fund_frames:
                total_fund += write_fundamentals(conn, df)
            log.info("写入完成: daily_price %d 行, fundamentals %d 行", total_price, total_fund)

            # 全量重建后也做一次滚动清理: 若 START_DATE 早于保留窗口, 这里会把
            # 超出窗口的旧数据删掉, 保证"数据库永远只有最近 N 年"这条不变式成立。
            cutoff_str, d1, d2 = rolling_cleanup(conn, args.history_years)
            if d1 or d2:
                log.info("滚动清理: 删除 trade_date < %s 的数据 "
                         "(daily_price %d 行, fundamentals %d 行)", cutoff_str, d1, d2)
            else:
                log.info("滚动清理: cutoff=%s, 无需删除(库内数据都在窗口内)", cutoff_str)
        except sqlite3.Error as exc:
            log.error("写库失败: %s", exc)
            return 1
        finally:
            conn.close()

    # ---- 汇总 ----
    log.info("=" * 68)
    log.info("完成: 行情 %d 行 | 基本面 %d 行 | 成功标的 %d/%d",
             total_price, total_fund, len(price_frames), len(targets))
    for w in warnings:
        log.warning("警告: %s", w)
    for f in failures:
        log.error("失败: %s", f)
    if failures:
        log.error("有 %d 项失败, 详见上方日志。其余数据已正常入库。", len(failures))
    log.info("=" * 68)

    if not args.dry_run:
        try:
            verify_db(args.db)
        except Exception as exc:
            log.warning("自检失败(不影响数据入库): %s", exc)

    # 只要行情有数据就算基本成功; 全失败才返回非 0
    return 0 if price_frames else 1


if __name__ == "__main__":
    sys.exit(main())
