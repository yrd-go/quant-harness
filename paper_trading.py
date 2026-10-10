#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""paper_trading.py — 纸面跟踪(Paper Trading): 模拟真实成交, 积累"策略行不行"的证据

为什么需要它
------------
三层漏斗 + 多智能体目前只能"给出建议", 但【没有任何机制检验建议对不对】。
这个脚本把每日选股结果变成模拟订单, 用真实开盘价撮合, 逐日记录净值,
最终回答一个问题: 这套策略到底有没有 alpha?

工作流(严格对齐 A 股 T+1)
--------------------------
    T 日收盘后   python paper_trading.py --daily
                 -> 读 config/target_pool.json, 生成"明天开盘"要执行的订单
    T+1 日收盘后 python paper_trading.py --daily
                 -> 先用【T+1 开盘价】撮合昨日挂单(含手续费+滑点)
                 -> 再按 T+1 收盘价 mark-to-market 记净值
                 -> 最后根据最新目标池生成"T+2 开盘"的订单

口径(与 src/backtest.py 一致, 便于横向对比)
-------------------------------------------
    手续费 0.03%, 最低 5 元   |   滑点 0.1%(买入向上、卖出向下)
    买入 100 股整手           |   当日买入不可卖(T+1)

用法
----
    python paper_trading.py --init --cash 100000     # 建账
    python paper_trading.py --daily                  # 每日例行(收盘后跑)
    python paper_trading.py --report                 # 出绩效报告
    python paper_trading.py --daily --dry-run        # 只算不写
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path

# --------------------------------------------------------------------------- #
# 路径引导
# --------------------------------------------------------------------------- #
_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent if (_HERE.parent / "config.py").exists() else _HERE
for p in (str(_ROOT), str(_ROOT / "src")):
    if p not in sys.path:
        sys.path.insert(0, p)

from config import (  # noqa: E402
    CONFIG_DATA_DIR, DB_FILE, LOGS_DIR, now_bj,
)
import paper_broker as pb  # noqa: E402

# --------------------------------------------------------------------------- #
# 路径
# --------------------------------------------------------------------------- #
DATA_DIR = _ROOT / "data"
ACCOUNT_FILE = DATA_DIR / "paper_account.json"
PENDING_FILE = DATA_DIR / "paper_pending.json"
TRADES_CSV = DATA_DIR / "paper_trades.csv"
EQUITY_CSV = DATA_DIR / "paper_equity.csv"
TARGET_POOL = CONFIG_DATA_DIR / "target_pool.json"
LOG_FILE = LOGS_DIR / "paper_trading.log"

BENCHMARK = "510300"          # 基准: 沪深300 ETF
POSITION_POLICY = "equal_weight"

log = logging.getLogger("paper_trading")


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
# 数据读取
# --------------------------------------------------------------------------- #
def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_FILE))
    conn.row_factory = sqlite3.Row
    return conn


def latest_complete_date(conn: sqlite3.Connection) -> str | None:
    """取库里【最新的、已经有收盘价】的交易日。

    注意: 这里取的是全库最大 trade_date。因为更新脚本保证所有标的同步到最新,
    所以它就是"最近一个已完成交易日"。
    """
    r = conn.execute("SELECT MAX(trade_date) FROM daily_price").fetchone()
    return r[0] if r and r[0] else None


def load_prices(conn: sqlite3.Connection, symbols: list[str],
                as_of: str, column: str = "close") -> dict[str, float]:
    """取每个标的在 <= as_of 的【最近一次】价格。

    为什么不是严格等于 as_of: 停牌/新股会导致某些标的当日没有数据,
    直接取不到就该沿用上一个有效价格(否则持仓会被错误地估成 0)。
    """
    out: dict[str, float] = {}
    for sym in symbols:
        r = conn.execute(
            f"SELECT {column} FROM daily_price WHERE symbol=? AND trade_date<=? "
            f"AND {column} IS NOT NULL ORDER BY trade_date DESC LIMIT 1",
            (sym, as_of)).fetchone()
        if r and r[0]:
            out[sym] = float(r[0])
    return out


def load_opens_at(conn: sqlite3.Connection, symbols: list[str],
                  day: str) -> dict[str, float]:
    """取指定交易日的开盘价(撮合价)。没有该日数据的标的不会出现在结果里。"""
    out: dict[str, float] = {}
    for sym in symbols:
        r = conn.execute(
            "SELECT open FROM daily_price WHERE symbol=? AND trade_date=? "
            "AND open IS NOT NULL", (sym, day)).fetchone()
        if r and r[0]:
            out[sym] = float(r[0])
    return out


def load_target_pool() -> list[dict]:
    if not TARGET_POOL.exists():
        raise FileNotFoundError(
            f"找不到 {TARGET_POOL.name}。请先跑第二层:\n"
            f"       python src/news_agent.py")
    payload = json.loads(TARGET_POOL.read_text(encoding="utf-8"))
    pool = payload.get("pool") or []
    if not pool:
        raise ValueError(f"{TARGET_POOL.name} 里没有标的")
    return pool


# --------------------------------------------------------------------------- #
# 持久化
# --------------------------------------------------------------------------- #
def _append_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with open(path, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        if new:
            w.writeheader()
        for r in rows:
            w.writerow(r)


def read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, "r", newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def save_pending(orders: list[dict], signal_day: str) -> None:
    PENDING_FILE.parent.mkdir(parents=True, exist_ok=True)
    PENDING_FILE.write_text(json.dumps(
        {"signal_day": signal_day, "orders": orders},
        ensure_ascii=False, indent=2), encoding="utf-8")


def load_pending() -> dict:
    if not PENDING_FILE.exists():
        return {"signal_day": None, "orders": []}
    return json.loads(PENDING_FILE.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# --init
# --------------------------------------------------------------------------- #
def cmd_init(cash: float) -> int:
    if ACCOUNT_FILE.exists():
        log.warning("账户已存在: %s", ACCOUNT_FILE)
        log.warning("如需重置, 请先手动删除该文件(以及 data/paper_*.csv)")
        return 1
    acct = pb.PaperAccount(initial_cash=cash, cash=cash,
                           created_at=now_bj().strftime("%Y-%m-%d %H:%M:%S"))
    acct.save(ACCOUNT_FILE)
    log.info("=" * 68)
    log.info("纸面账户已创建")
    log.info("  初始资金: %s 元", f"{cash:,.0f}")
    log.info("  账户文件: %s", ACCOUNT_FILE)
    log.info("=" * 68)
    log.info("下一步: 收盘后运行  python paper_trading.py --daily")
    return 0


# --------------------------------------------------------------------------- #
# --daily
# --------------------------------------------------------------------------- #
def cmd_daily(dry_run: bool = False) -> int:
    acct = pb.PaperAccount.load(ACCOUNT_FILE)
    conn = _connect()
    try:
        today = latest_complete_date(conn)
        if not today:
            log.error("数据库里没有任何行情, 请先跑 data_center.py")
            return 1

        log.info("=" * 68)
        log.info("纸面跟踪 | 最近交易日 %s | 当前北京时间 %s", today,
                 now_bj().strftime("%Y-%m-%d %H:%M:%S"))
        log.info("=" * 68)

        pending = load_pending()
        all_syms = list({p["symbol"] for p in (pending.get("orders") or [])} |
                        set(acct.positions))

        # ---------------- ① 先撮合【上一次挂的】订单 ----------------
        if pending.get("orders"):
            sig = pending.get("signal_day")
            if sig == today:
                log.info("挂单信号日 = %s (就是今天), 需等到下一交易日开盘才能成交, 跳过撮合",
                         sig)
            else:
                fills = load_opens_at(conn, all_syms, today)
                runnable, skipped = [], []
                for o in pending["orders"]:
                    if o["symbol"] in fills:
                        o = dict(o, price=fills[o["symbol"]])
                        runnable.append(o)
                    else:
                        skipped.append(o)

                log.info("撮合 %s 的开盘单: 可执行 %d 笔, 跳过 %d 笔(当日无开盘价)",
                         today, len(runnable), len(skipped))
                for o in skipped:
                    log.warning("  跳过 %s (%s): 当日无开盘价(可能停牌)",
                                o["symbol"], o.get("reason", ""))

                trades = pb.execute_orders(acct, runnable, today)
                for t in trades:
                    if t["shares"] == 0:
                        log.warning("  %s %s 未成交: %s", t["symbol"], t["side"],
                                    t.get("note", ""))
                        continue
                    pnl = f" 已实现盈亏 {t['pnl']:+.2f}" if t["pnl"] is not None else ""
                    log.info("  %s %s %s %d股 @ %.4f  手续费 %.2f%s",
                             t["date"], t["symbol"], t["side"], t["shares"],
                             t["price"], t["fee"], pnl)

                if trades and not dry_run:
                    _append_csv(TRADES_CSV, trades, [
                        "date", "symbol", "side", "shares", "price",
                        "gross", "fee", "pnl", "note"])

                # 挂单已处理完(无论成交与否), 清空
                save_pending([], pending.get("signal_day"))

        # ---------------- ② 按今日收盘价 mark-to-market ----------------
        hold_syms = list(acct.positions)
        prices = load_prices(conn, hold_syms, today, "close") if hold_syms else {}
        equity = acct.equity(prices)
        mv = acct.market_value(prices)

        log.info("-" * 68)
        log.info("账户估值(收盘价 %s): 总资产 %.2f = 现金 %.2f + 持仓 %.2f",
                 today, equity, acct.cash, mv)
        log.info("累计收益: %+.2f%%  (初始 %.0f)",
                 (equity / acct.initial_cash - 1) * 100, acct.initial_cash)
        for sym, p in acct.positions.items():
            px = prices.get(sym, float(p.get("avg_cost", 0)))
            cost = float(p.get("avg_cost", 0)) * int(p["shares"])
            val = int(p["shares"]) * px
            log.info("  %s %5d股 成本 %.4f 现价 %.4f 市值 %.2f 浮动 %+.2f (%+.2f%%)",
                     sym, int(p["shares"]), float(p.get("avg_cost", 0)), px, val,
                     val - cost, (val / cost - 1) * 100 if cost else 0.0)
        log.info("-" * 68)

        # ---------------- ③ 生成下一交易日的订单 ----------------
        pool = load_target_pool()
        weights = pb.target_weights(pool, POSITION_POLICY)
        excluded = len(pool) - len(weights)
        log.info("目标池 %d 只 -> 目标权重 %d 只 (排除 %d 只: cautioned/信息真空期)",
                 len(pool), len(weights), excluded)

        # 生成订单要用"今日收盘价"估算, 但成交在"次日开盘"
        order_syms = list(set(weights) | set(acct.positions))
        close_px = load_prices(conn, order_syms, today, "close")

        # ---- 一手门槛检查: 本金不够时, 有些标的根本买不进来 ----
        # 不做这一步的话, 它们会被 plan_orders 静默丢弃, 你以为在建 15 只组合,
        # 实际只建了 10 只 —— 而且丢的全是高价股, 组合会系统性偏向低价股。
        info = pb.min_capital_required(weights, close_px)
        if info["n"]:
            per_budget = equity / info["n"]
            unaff = [s for s in weights
                     if close_px.get(s, 0) * pb.LOT_SIZE > per_budget]
            if unaff:
                log.warning("-" * 68)
                log.warning("⚠ 一手门槛: 有 %d 只标的【买不起一手】, 本次不会建仓",
                            len(unaff))
                for s in sorted(unaff, key=lambda x: -close_px.get(x, 0)):
                    lot = close_px.get(s, 0) * pb.LOT_SIZE
                    log.warning("    %s  股价 %.2f  一手 %.0f 元  超出每只预算 %.0f 元 %.0f%%",
                                s, close_px.get(s, 0), lot, per_budget,
                                (lot / per_budget - 1) * 100)
                log.warning("  当前本金 %.0f 元, 等权每只预算 %.0f 元", equity, per_budget)
                log.warning("  要让这 %d 只都买得起, 建议本金 >= %.0f 元 (%.1f 万)",
                            info["n"], info["recommended"], info["recommended"] / 10000)
                log.warning("  否则组合只会有 %d 只, 且系统性偏向低价股",
                            info["n"] - len(unaff))
                log.warning("-" * 68)

        orders = pb.plan_orders(acct, weights, close_px, equity)
        real = [o for o in orders if o["side"] in ("buy", "sell")]

        if real:
            log.info("生成 %s 开盘要执行的订单 %d 笔:", today, len(real))
            for o in real:
                est = o["shares"] * o["price"]
                log.info("  %s %s %d股 约 %.2f 元 (%s)",
                         o["symbol"], o["side"].upper(), o["shares"], est,
                         o.get("reason", ""))
        else:
            log.info("无需调仓(与目标权重一致, 或差额低于最小交易额)")

        for o in orders:
            if o["side"] == "skip":
                log.warning("  跳过 %s: %s", o["symbol"], o.get("reason", ""))

        # ---------------- ④ 落盘 ----------------
        if dry_run:
            log.info("dry-run: 不写任何文件")
        else:
            acct.last_run = now_bj().strftime("%Y-%m-%d %H:%M:%S")
            acct.save(ACCOUNT_FILE)
            save_pending(real, today)
            _append_csv(EQUITY_CSV, [{
                "date": today, "equity": round(equity, 2),
                "cash": round(acct.cash, 2), "market_value": round(mv, 2),
                "positions": len(acct.positions),
            }], ["date", "equity", "cash", "market_value", "positions"])
            log.info("已保存账户 / 净值 / 挂单")

    finally:
        conn.close()

    log.info("=" * 68)
    return 0


# --------------------------------------------------------------------------- #
# --report
# --------------------------------------------------------------------------- #
def _fmt_pct(v: float | None, nd: int = 2) -> str:
    return "N/A" if v is None else f"{v * 100:+.{nd}f}%"


def cmd_report() -> int:
    curve = read_csv(EQUITY_CSV)
    trades = read_csv(TRADES_CSV)

    # 净空曲线里可能有重复日期(同一天跑多次), 只保留每个日期最后一条
    dedup: dict[str, dict] = {}
    for r in curve:
        dedup[r["date"]] = r
    curve = [dedup[d] for d in sorted(dedup)]

    if not curve:
        log.error("还没有净值记录。先跑 --init 和 --daily")
        return 1

    perf = pb.performance(curve)
    closed = [t for t in trades if t.get("side") == "sell" and t.get("pnl") not in ("", None)]
    for t in closed:
        t["pnl"] = float(t["pnl"])
    tstat = pb.trade_stats(closed)

    # 基准对比
    bench_ret = None
    try:
        conn = _connect()
        try:
            first_day, last_day = curve[0]["date"], curve[-1]["date"]
            b0 = conn.execute(
                "SELECT close FROM daily_price WHERE symbol=? AND trade_date>=? "
                "ORDER BY trade_date LIMIT 1", (BENCHMARK, first_day)).fetchone()
            b1 = conn.execute(
                "SELECT close FROM daily_price WHERE symbol=? AND trade_date<=? "
                "ORDER BY trade_date DESC LIMIT 1", (BENCHMARK, last_day)).fetchone()
            if b0 and b1 and b0[0]:
                bench_ret = float(b1[0]) / float(b0[0]) - 1.0
        finally:
            conn.close()
    except Exception as exc:
        log.warning("基准对比计算失败: %s", exc)

    print()
    print("=" * 72)
    print(f"纸面跟踪绩效报告  |  {curve[0]['date']} ~ {curve[-1]['date']}")
    print("=" * 72)
    print(f"  交易日数        : {perf['days']}")
    print(f"  期初 / 期末资产 : {perf['start_equity']:,.2f} -> {perf['end_equity']:,.2f}")
    print()
    print(f"  累计收益        : {_fmt_pct(perf['total_return'])}")
    print(f"  年化收益        : {_fmt_pct(perf['annual_return'])}")
    print(f"  最大回撤        : {_fmt_pct(perf['max_drawdown'])}")
    print(f"  年化波动率      : {_fmt_pct(perf['volatility'])}")
    print(f"  夏普比率        : {perf['sharpe']:.3f}")
    print()
    print(f"  按【日】统计(参考):")
    print(f"    上涨 {perf['win_days']} 天 / 下跌 {perf['loss_days']} 天"
          f"  ->  日胜率 {_fmt_pct(perf['win_rate'], 1)}")
    print(f"    平均涨 {_fmt_pct(perf['avg_win'])}  平均跌 {_fmt_pct(perf['avg_loss'])}")
    print(f"    ★ 日盈亏比 {perf['profit_loss_ratio']:.3f}")
    print()
    if tstat.get("trades"):
        print(f"  按【笔】统计(更重要):")
        print(f"    平仓 {tstat['trades']} 笔 (盈 {tstat['win_trades']} / 亏 {tstat['loss_trades']})")
        print(f"    胜率 {_fmt_pct(tstat['win_rate'], 1)}")
        print(f"    平均盈利 {tstat['avg_win']:+,.2f}  平均亏损 {-tstat['avg_loss']:+,.2f}")
        print(f"    ★ 盈亏比 {tstat['profit_loss_ratio']:.3f}   ( >1 才说明赚的比亏的多 )")
        print(f"    合计已实现盈亏 {tstat['total_pnl']:+,.2f}"
              f"  (最好 {tstat['best']:+,.2f} / 最差 {tstat['worst']:+,.2f})")
    else:
        print("  按【笔】统计: 还没有平仓交易(策略只有卖出后才结算一笔)")
    print()
    if bench_ret is not None:
        print(f"  基准 {BENCHMARK}(沪深300ETF) 同期: {_fmt_pct(bench_ret)}")
        print(f"  ★ 超额收益: {_fmt_pct(perf['total_return'] - bench_ret)}")
    print()
    print("=" * 72)
    print("说明: 这不是投资建议。样本天数太少时, 任何指标都不具备统计意义 ——")
    print("      至少要覆盖一轮完整涨跌周期(建议 ≥250 个交易日)才可以下结论。")
    print("=" * 72)
    return 0


# --------------------------------------------------------------------------- #
# --capital-hint
# --------------------------------------------------------------------------- #
def cmd_capital_hint() -> int:
    """只算"要装下目标池需要多少本金", 不动账户。"""
    conn = _connect()
    try:
        today = latest_complete_date(conn)
        pool = load_target_pool()
        weights = pb.target_weights(pool, POSITION_POLICY)
        syms = list(weights)
        close_px = load_prices(conn, syms, today, "close")
        info = pb.min_capital_required(weights, close_px)
    finally:
        conn.close()

    if not info["n"]:
        log.error("目标池里没有可建仓的标的(可能全被 cautioned/真空期排除)")
        return 1

    print()
    print("=" * 74)
    print(f"一手门槛测算  |  可建仓标的 {info['n']} 只  |  行情日 {today}")
    print("=" * 74)
    print(f"  {'代码':<9}{'股价':>9}{'一手成本':>12}{'占最低本金预算':>16}")
    print("-" * 74)
    per_min = info["min_capital"] / info["n"] if info["n"] else 0
    for s, c in sorted(info["lot_costs"].items(), key=lambda kv: -kv[1]):
        pct = (c / per_min * 100) if per_min else 0
        flag = "" if c <= per_min else "  <- 买不起"
        print(f"  {s:<9}{c / pb.LOT_SIZE:>9.2f}{c:>12,.0f}{pct:>15.0f}%{flag}")
    print("-" * 74)
    print(f"  最贵一手: {info['max_lot_symbol']}  {info['max_lot_cost']:,.0f} 元")
    print()
    print(f"  ★ 理论最低本金: {info['min_capital']:>10,.0f} 元 "
          f"({info['min_capital'] / 10000:.1f} 万)")
    print(f"       = 最贵一手 x 只数 = {info['max_lot_cost']:,.0f} x {info['n']}")
    print(f"       ({info['n']} 只等权时, 每只预算才够买最贵那只的一手)")
    print()
    print(f"  ★ 实操建议本金: {info['recommended']:>10,.0f} 元 "
          f"({info['recommended'] / 10000:.1f} 万)")
    print(f"       = 理论下限 x 1.5 (留余量: 股数不会刚好一手 + 最低 5 元手续费)")

    # 反推: 给定本金最多能同时持有几只
    #
    # 注意: 不要自己另写一套"每只预算 = 本金/只数"的近似公式 ——
    # 那个模型是错的(它假设只数 k 变大时每只预算会缩小, 但如果我们只挑
    # 【最便宜的 k 只】, 总成本是固定的, 不该随 k 缩水)。
    # 正确做法是【直接用 plan_orders 那套分配逻辑去实测】。
    if info["lot_costs"]:
        costs = sorted(info["lot_costs"].items(), key=lambda kv: kv[1])  # 便宜优先

        def max_holdings(cap: float) -> tuple[int, float, float]:
            """用与真实下单一致的逻辑, 贪心试出能持有几只。

            按"便宜优先"逐个尝试买入一手: 每次把剩余资金在【已选 + 当前】之间等权,
            若当前这只买得起就纳入。返回 (只数, 每只预算, 最贵一手)。
            """
            chosen: list[float] = []
            for _sym, lot in costs:
                trial = chosen + [lot]
                k = len(trial)
                if cap / k >= max(trial):      # 等权预算够最贵那手
                    chosen = trial
            if not chosen:
                return 0, 0.0, 0.0
            return len(chosen), cap / len(chosen), max(chosen)

        print()
        print("  反推: 给定本金最多能同时等权持有几只")
        print("        (按'便宜优先'逐个试买一手, 与真实下单同一套逻辑)")
        print(f"    {'本金':>12}{'可持有':>10}{'每只预算':>12}{'其中最贵一手':>14}")
        print("    " + "-" * 48)
        for cap in (30_000, 50_000, 100_000, 200_000, 250_000, 500_000):
            k, per, mx = max_holdings(cap)
            if k == 0:
                print(f"    {cap:>12,}{'0':>10}{'-':>12}{'-':>14}")
            else:
                print(f"    {cap:>12,}{k:>8} 只{per:>12,.0f}{mx:>14,.0f}")
        print()
        print(f"    注: 目标池 {info['n']} 只要【全部】持有, 本金需 >= "
              f"{info['min_capital']:,.0f} 元")
        print(f"        本金不足时, 系统只会买入其中一部分(便宜优先), ")
        print(f"        被跳过的多是高价股 —— 这会让组合偏向低价股, 需注意。")

    print("=" * 74)
    return 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="纸面跟踪: 用真实行情模拟成交, 积累策略验证证据",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--init", action="store_true", help="创建纸面账户")
    p.add_argument("--daily", action="store_true", help="每日例行(收盘后跑)")
    p.add_argument("--report", action="store_true", help="输出绩效报告")
    p.add_argument("--capital-hint", action="store_true",
                   help="测算装下目标池需要多少本金(不动账户)")
    p.add_argument("--cash", type=float, default=100_000.0,
                   help="初始资金(仅 --init 用)")
    p.add_argument("--dry-run", action="store_true", help="只计算不写文件")
    p.add_argument("--quiet", action="store_true", help="只输出 INFO 及以上")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    setup_logging(verbose=not args.quiet)

    n = sum([args.init, args.daily, args.report, args.capital_hint])
    if n == 0:
        log.error("请指定 --init / --daily / --report / --capital-hint 之一")
        return 2
    if n > 1:
        log.error("--init / --daily / --report / --capital-hint 只能选一个")
        return 2

    if args.cash <= 0:
        log.error("--cash 必须为正数")
        return 2

    if args.capital_hint:
        return cmd_capital_hint()
    if args.init:
        return cmd_init(args.cash)
    if args.daily:
        return cmd_daily(dry_run=args.dry_run)
    return cmd_report()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
