#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
factor_score.py — 多因子打分选股

读取 quant_data.db 中【最新交易日】的行情与基本面, 对个股做价值/质量/动量三因子打分排名。
ETF(510300) 没有 PE/PB/ROE, 因此只算它的 20 日动量, 作为大盘基准单独打印,
不参与个股的三因子综合排名。

因子定义
--------
价值因子 (越低越好):
    价值排名 = (PE(TTM) 升序排名 + PB 升序排名) / 2
质量因子 (越高越好):
    质量排名 = ROE 降序排名
动量因子 (越高越好):
    动量排名 = 20 日收益率降序排名
综合得分 (越低越靠前):
    综合得分 = 价值排名*40% + 质量排名*30% + 动量排名*30%

关于 20 日动量: 20 个"交易日收益率"需要 21 个价格点(首尾相减),
所以窗口取 MOMENTUM_WINDOW + 1 个收盘价, 请不要把常量 20 误解成"取 20 根K线"。

口径说明(重要)
--------------
1. 动量的基准日取 daily_price 的全局最大交易日; 若某些标的缺失该日, 会给出警告并
   退回到各自最近可用的交易日。
2. ROE 直接取数据库里的值, 是报告期累计口径(如 2026-06-30 是半年累计, 不是年化)。
   同一截面上 4 只个股的报告期一致时可比; 若不一致会打印警告。
3. ETF 若有 adjust 为空(未复权)的记录, 跑之前会提示, 以免把未复权价格当复权价用。

用法
----
    python factor_score.py
    python factor_score.py --db quant_data.db --window 20
    python factor_score.py --quiet

依赖: pandas (sqlite3 为标准库)

只读: 本脚本只执行 SELECT, 不会创建、修改或删除任何表。
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
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

from config import DB_FILE, FACTOR_LOG  # noqa: E402  (必须在 sys.path 调整之后导入)

# --------------------------------------------------------------------------- #
# 配置区
# --------------------------------------------------------------------------- #
ETF_SYMBOL = "510300"          # 只做基准, 不参与个股综合排名
MOMENTUM_WINDOW = 20           # 20 个交易日收益率

W_VALUE = 0.40                 # 价值因子权重
W_QUALITY = 0.30               # 质量因子权重
W_MOMENTUM = 0.30              # 动量因子权重

# 显示时用 ●/○ 标注"该因子的分项名次"(加权前的原始名次), 方便定位谁拖了后腿
MARK_GOOD = "●"
MARK_BAD = "○"

PRICE_TABLE = "daily_price"
FUND_TABLE = "fundamentals"

log = logging.getLogger("factor_score")


class DataError(Exception):
    """数据层可预期的问题(缺库/缺表/缺字段/数据不足), 用于给出友好提示。"""


# --------------------------------------------------------------------------- #
# 日志
# --------------------------------------------------------------------------- #
def setup_logging(verbose: bool = True) -> None:
    log.setLevel(logging.DEBUG)
    log.handlers.clear()

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S")

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    console.setFormatter(fmt)
    log.addHandler(console)

    try:
        fh = logging.FileHandler(FACTOR_LOG, mode="w", encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)
        log.addHandler(fh)
    except Exception as exc:
        log.warning("无法写入日志文件 %s: %s", FACTOR_LOG, exc)


# --------------------------------------------------------------------------- #
# 读数据库 (只读)
# --------------------------------------------------------------------------- #
def connect_readonly(db_path: str) -> sqlite3.Connection:
    """以只读用途打开数据库。

    这里用普通 connect + 绝对路径, 而不是 "file:...?mode=ro" 这种 URI 形式:
    一是 Windows 盘符(C:/...)在 URI 模式下对路径写法比较挑剔, 二是库里存的是
    WAL 模式, 纯 ro 打开在部分环境会因读不到 WAL 文件而报错。
    本脚本内部【只执行 SELECT】, 不会创建、修改或删除任何表。
    """
    path = Path(db_path)
    if not path.exists():
        raise DataError(
            f"找不到数据库文件: {path.resolve()}\n"
            f"       请先运行 data_center.py 生成 {DB_FILE.name}, 或确认当前工作目录是否正确。"
        )
    try:
        conn = sqlite3.connect(str(path.resolve()))
    except sqlite3.Error as exc:
        raise DataError(f"无法打开数据库 {path}: {exc}") from exc
    return conn


def require_columns(conn: sqlite3.Connection, table: str,
                    needed: list[str]) -> None:
    """检查表和字段是否存在, 缺失时给出可操作的中文提示。"""
    exists = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()[0]
    if not exists:
        raise DataError(
            f"数据库缺少表 '{table}'。\n"
            f"       请先运行: python data_center.py"
        )

    have = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    missing = [c for c in needed if c not in have]
    if missing:
        raise DataError(
            f"表 '{table}' 缺少字段 {missing}。\n"
            f"       实际字段: {sorted(have)}\n"
            f"       可能是数据库结构版本不匹配, 建议重新运行 data_center.py 重建。"
        )


def load_price_window(conn: sqlite3.Connection, window: int
                      ) -> tuple[pd.DataFrame, str | None, list[str]]:
    """取每个标的最近 window+1 个交易日的收盘价。

    返回 (价格长表, 全局基准日, 警告列表)。
    价格表列: symbol, name, asset_type, trade_date, close, adjust
    """
    max_date = conn.execute(
        f"SELECT MAX(trade_date) FROM {PRICE_TABLE}"
    ).fetchone()[0]
    if max_date is None:
        raise DataError(
            f"表 '{PRICE_TABLE}' 是空的, 没有行情数据。\n"
            f"       请先运行: python data_center.py"
        )

    warnings: list[str] = []
    symbols = [r[0] for r in conn.execute(
        f"SELECT DISTINCT symbol FROM {PRICE_TABLE} ORDER BY symbol")]

    need = window + 1  # 20 日收益率 = 21 个价格点首尾相减
    frames: list[pd.DataFrame] = []

    for sym in symbols:
        rows = pd.read_sql(
            f"SELECT symbol, name, asset_type, trade_date, close, adjust "
            f"FROM {PRICE_TABLE} WHERE symbol=? AND trade_date<=? "
            f"ORDER BY trade_date DESC LIMIT ?",
            conn, params=(sym, max_date, need),
        )
        if rows.empty:
            warnings.append(f"{sym}: 基准日 {max_date} 之前没有任何行情数据, 已跳过")
            continue

        latest = rows["trade_date"].iloc[0]
        if latest != max_date:
            warnings.append(
                f"{sym}: 缺少基准日 {max_date} 的数据, 实际最新为 {latest}, "
                f"其动量窗口与其它标的不可比"
            )
        frames.append(rows)

    if not frames:
        raise DataError(
            f"没有取到任何行情数据(基准日 {max_date})。\n"
            f"       请检查 {PRICE_TABLE} 是否正常。"
        )

    prices = pd.concat(frames, ignore_index=True)
    prices["trade_date"] = prices["trade_date"].astype(str)
    return prices, max_date, warnings


def load_momentum(conn: sqlite3.Connection, window: int
                  ) -> tuple[pd.DataFrame, str | None, list[str]]:
    """计算每个标的的 N 日收益率。

    返回 (动量表, 全局基准日, 警告列表)。
    动量表列: symbol, name, asset_type, base_date, start_date, end_date, close, mom
    """
    prices, max_date, warnings = load_price_window(conn, window)
    need = window + 1

    records: list[dict] = []
    for sym, grp in prices.groupby("symbol", sort=True):
        grp = grp.sort_values("trade_date")  # 已按 DESC 取出, 这里翻成升序
        if len(grp) < need:
            warnings.append(
                f"{sym}: 只有 {len(grp)} 个交易日数据, 不足 {need} 个, "
                f"无法计算 {window} 日动量, 已从排名中剔除"
            )
            continue

        start_px = grp["close"].iloc[0]
        end_px = grp["close"].iloc[-1]
        if pd.isna(start_px) or pd.isna(end_px):
            warnings.append(f"{sym}: 窗口内收盘价存在空值, 无法计算动量, 已剔除")
            continue
        if start_px == 0:
            warnings.append(f"{sym}: 窗口起始收盘价为 0, 无法计算收益率, 已剔除")
            continue

        records.append({
            "symbol": sym,
            "name": grp["name"].iloc[-1],
            "asset_type": grp["asset_type"].iloc[-1],
            "base_date": max_date,
            "start_date": grp["trade_date"].iloc[0],
            "end_date": grp["trade_date"].iloc[-1],
            "close": float(end_px),
            "adjust": grp["adjust"].iloc[-1],
            "mom": (float(end_px) / float(start_px) - 1.0) * 100.0,
        })

    if not records:
        raise DataError(
            f"所有标的都无法计算 {window} 日动量。\n"
            f"       通常是历史数据不足 {need} 个交易日所致。"
        )

    return pd.DataFrame(records), max_date, warnings


def load_fundamentals(conn: sqlite3.Connection) -> pd.DataFrame:
    """取每个个股最新交易日的 PE(TTM) / PB / ROE 及报告期。"""
    df = pd.read_sql(
        f"""
        SELECT f.symbol, f.name, f.trade_date, f.pe_ttm, f.pb, f.roe, f.roe_report_period
        FROM {FUND_TABLE} f
        JOIN (
            SELECT symbol, MAX(trade_date) AS m
            FROM {FUND_TABLE} GROUP BY symbol
        ) t ON f.symbol = t.symbol AND f.trade_date = t.m
        ORDER BY f.symbol
        """,
        conn,
    )
    if df.empty:
        raise DataError(
            f"表 '{FUND_TABLE}' 里没有任何基本面数据, 无法进行价值/质量打分。\n"
            f"       请先运行: python data_center.py"
        )
    return df


# --------------------------------------------------------------------------- #
# 打分
# --------------------------------------------------------------------------- #
def assign_ranks(df: pd.DataFrame, window: int) -> pd.DataFrame:
    """计算各因子名次与综合得分。

    只在【个股】上排名: ETF 没有基本面, 让它参与综合排名会拿"单项分"比"三项分"。
    所有 rank 都用默认的 average 处理并列, 名次从 1 开始, 越小越好。
    """
    out = df.copy()

    # 价值: PE 与 PB 分别升序排名(越低越好), 再取平均
    out["pe_rank"] = out["pe_ttm"].rank(method="average", ascending=True)
    out["pb_rank"] = out["pb"].rank(method="average", ascending=True)
    out["value_rank"] = (out["pe_rank"] + out["pb_rank"]) / 2.0

    # 质量: ROE 降序(越高越好)
    out["quality_rank"] = out["roe"].rank(method="average", ascending=False)

    # 动量: N 日收益率降序(越高越好)
    out["momo_rank"] = out["mom"].rank(method="average", ascending=False)

    out["score"] = (
        out["value_rank"] * W_VALUE
        + out["quality_rank"] * W_QUALITY
        + out["momo_rank"] * W_MOMENTUM
    )
    # 得分越低越靠前
    out = out.sort_values(["score", "symbol"], ascending=[True, True]).reset_index(drop=True)
    out["composite_rank"] = range(1, len(out) + 1)
    out["window"] = window
    return out


# --------------------------------------------------------------------------- #
# 展示
# --------------------------------------------------------------------------- #
def _mark(rank: float, n: int) -> str:
    """名次在前一半标 ●(相对好), 后一半标 ○(相对差)。"""
    if n <= 1:
        return ""
    return MARK_GOOD if rank <= (n + 1) / 2.0 else MARK_BAD


def _fmt_pct(v: float) -> str:
    return "N/A" if pd.isna(v) else f"{v:+.2f}%"


def print_etf_benchmark(mom: pd.DataFrame) -> None:
    """打印 ETF 基准的 20 日动量。"""
    etf = mom[mom["symbol"] == ETF_SYMBOL]
    if etf.empty:
        log.warning("基准标的 %s 不在动量结果中, 无法给出大盘基准。", ETF_SYMBOL)
        return

    r = etf.iloc[0]
    direction = "上涨" if r["mom"] > 0 else ("下跌" if r["mom"] < 0 else "持平")
    arrow = "↑" if r["mom"] > 0 else ("↓" if r["mom"] < 0 else "→")

    print()
    print("=" * 92)
    print("大盘基准(仅动量, 不参与个股综合排名)")
    print("=" * 92)
    print(f"  {r['symbol']} {r['name']}")
    print(f"  {r['window'] if 'window' in r else MOMENTUM_WINDOW} 日动量: "
          f"{_fmt_pct(r['mom'])} {arrow}  [{direction}]")
    print(f"  窗口: {r['start_date']} ~ {r['end_date']}   "
          f"收盘: {r['close']:.3f}")
    if str(r.get("adjust", "")).strip() == "":
        print("  注意: 该 ETF 记录为【未复权】口径(见 daily_price.adjust), "
              "与个股的前复权价格不可直接跨品种比较涨跌幅。")
    print()


def print_score_table(scored: pd.DataFrame) -> None:
    """打印个股多因子打分表。"""
    n = len(scored)

    header = [
        ("代码", 8), ("名称", 22), ("PE(TTM)", 10), ("PB", 8), ("ROE", 9),
        (f"{MOMENTUM_WINDOW}日动量", 11), ("价值排名", 9), ("质量排名", 9),
        ("动量排名", 9), ("综合得分", 10), ("综合排名", 9),
    ]
    line = "  ".join(f"{t:^{w}}" for t, w in header)
    sep = "-" * len(line)

    print("=" * 92)
    print(f"个股多因子打分  |  综合得分 = 价值排名×{W_VALUE:.0%} + "
          f"质量排名×{W_QUALITY:.0%} + 动量排名×{W_MOMENTUM:.0%}  (得分越低越靠前)")
    print("=" * 92)
    print(line)
    print(sep)

    for _, r in scored.iterrows():
        cells = [
            f"{r['symbol']:^{8}}",
            f"{str(r['name']):^{22}}",
            f"{r['pe_ttm']:>10.2f}",
            f"{r['pb']:>8.2f}",
            f"{r['roe']:>9.2f}",
            f"{_fmt_pct(r['mom']):>11}",
            f"{r['value_rank']:>6.2f} {_mark(r['value_rank'], n)}",
            f"{r['quality_rank']:>6.2f} {_mark(r['quality_rank'], n)}",
            f"{r['momo_rank']:>6.2f} {_mark(r['momo_rank'], n)}",
            f"{r['score']:>10.2f}",
            f"{int(r['composite_rank']):^{9}}",
        ]
        print("  ".join(cells))

    print(sep)
    print(f"说明: 价值排名 =(PE 名次 + PB 名次)/2; "
          f"{MARK_GOOD} = 该因子名次位于前一半, {MARK_BAD} = 后一半。")
    print("      名次在 1~%d 之间, 1 最优。综合得分为加权名次, 越小越好。" % n)
    print()


def print_summary(scored: pd.DataFrame) -> None:
    """给出结论性摘要与口径提醒。"""
    if scored.empty:
        return

    top = scored.iloc[0]
    print("-" * 92)
    print(f"综合排名第一: {top['symbol']} {top['name']}  "
          f"(得分 {top['score']:.2f}, PE {top['pe_ttm']:.2f}, PB {top['pb']:.2f}, "
          f"ROE {top['roe']:.2f}, {MOMENTUM_WINDOW}日动量 {_fmt_pct(top['mom'])})")

    periods = sorted({str(p) for p in scored["roe_report_period"].dropna().unique()})
    if len(periods) > 1:
        log.warning("各标的 ROE 报告期不一致 %s, 质量因子横向可比性下降。", periods)
    elif periods:
        print(f"ROE 口径: 报告期 {periods[0]} 的累计值(非年化), 各标的报告期一致, 横向可比。")

    # 得分差距很小时提示"不要太当真"
    if len(scored) >= 2:
        gap = scored["score"].iloc[1] - scored["score"].iloc[0]
        if gap < 0.05:
            print("提示: 前两名综合得分差距极小(<0.05), 名次差异基本可视为噪音。")
    print("-" * 92)


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="多因子打分选股(只读 quant_data.db)")
    p.add_argument("--db", default=DB_FILE, help=f"数据库路径(默认 {DB_FILE.name})")
    p.add_argument("--window", type=int, default=MOMENTUM_WINDOW,
                   help=f"动量窗口交易日数(默认 {MOMENTUM_WINDOW})")
    p.add_argument("--quiet", action="store_true", help="只输出 INFO 及以上日志")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    setup_logging(verbose=not args.quiet)

    if args.window < 1:
        log.error("--window 必须 >= 1, 当前为 %s", args.window)
        return 2

    conn: sqlite3.Connection | None = None
    try:
        conn = connect_readonly(args.db)
        require_columns(conn, PRICE_TABLE,
                        ["symbol", "name", "asset_type", "trade_date", "close", "adjust"])
        require_columns(conn, FUND_TABLE,
                        ["symbol", "name", "trade_date", "pe_ttm", "pb", "roe",
                         "roe_report_period"])

        # ---- 1. 动量(全部标的, 含 ETF) ----
        mom, max_date, warnings = load_momentum(conn, args.window)
        for w in warnings:
            log.warning(w)
        log.info("基准交易日: %s | 动量窗口: %d 个交易日 | 标的数: %d",
                 max_date, args.window, len(mom))

        # ---- 2. 基本面(仅个股) ----
        fund = load_fundamentals(conn)
        if ETF_SYMBOL in set(fund["symbol"]):
            log.warning("%s 意外出现在 %s 表中, 已剔除(ETF 不参与基本面打分)。",
                        ETF_SYMBOL, FUND_TABLE)
            fund = fund[fund["symbol"] != ETF_SYMBOL]

        # ---- 3. 合并: 只有既有基本面又有动量的标的才能进综合排名 ----
        merged = fund.merge(
            mom[["symbol", "mom", "close", "start_date", "end_date", "adjust"]],
            on="symbol", how="inner",
        )

        missing_mom = sorted(set(fund["symbol"]) - set(merged["symbol"]))
        for s in missing_mom:
            log.warning("%s 有基本面但算不出动量, 已排除出综合排名。", s)

        stocks_only = merged[~merged["symbol"].isin([ETF_SYMBOL])].copy()
        dropped_etf = set(merged["symbol"]) & {ETF_SYMBOL}
        if dropped_etf:
            log.debug("%s 已排除出综合排名(仅作基准展示)。", ETF_SYMBOL)

        if stocks_only.empty:
            log.error("没有任何个股同时具备基本面与动量数据, 无法打分。")
            return 1

        # 价值/质量因子所需字段的空值检查
        for col, label in (("pe_ttm", "PE(TTM)"), ("pb", "PB"), ("roe", "ROE")):
            bad = stocks_only[stocks_only[col].isna()]
            if not bad.empty:
                log.warning("%s 在 %s 上为空, 该因子将按缺失处理(名次可能失真): %s",
                            label, ", ".join(bad["symbol"]), label)
                stocks_only = stocks_only.dropna(subset=[col])

        if stocks_only.empty:
            log.error("剔除空值后没有可用于打分的个股。")
            return 1
        if len(stocks_only) < 2:
            log.warning("可用于打分的个股只有 %d 只, 名次没有区分度, 结果仅供演示。",
                        len(stocks_only))

        # ---- 4. 打印 ----
        print_etf_benchmark(mom)
        scored = assign_ranks(stocks_only, args.window)
        print_score_table(scored)
        print_summary(scored)

        log.info("完成: 个股 %d 只参与综合打分, 基准日 %s。",
                 len(scored), max_date)
        return 0

    except DataError as exc:
        log.error("数据问题: %s", exc)
        return 1
    except sqlite3.Error as exc:
        log.error("数据库读取失败: %s", exc)
        print("       建议: 确认 quant_data.db 未被其它程序独占, 然后重试。")
        return 1
    except Exception as exc:  # 兜底, 避免把原始 traceback 甩给用户
        log.error("运行出错: %s: %s", type(exc).__name__, exc)
        print("       如果反复出现, 请把上面的完整报错发给我排查。")
        return 1
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    sys.exit(main())
