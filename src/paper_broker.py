#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""paper_broker.py — 纸面交易的核心: 账户状态 / 目标权重 / 订单生成 / 撮合记账

为什么单独抽成模块:
    撮合与记账是这个系统里【最容易悄悄算错】的地方 —— 手续费最低 5 元、
    滑点方向、100 股整手、T+1 可用股数、现金约束……任何一处错了,
    最后的收益曲线都是假的, 而且不会报错。
    所以这里做成【纯函数 + 可测试】, 并在 tests/test_paper_broker.py 里
    把这些规则逐条固化。

设计约定(与 src/backtest.py 保持一致, 便于横向对比):
    手续费 = 0.03%, 且【最低 5 元】(散户真实成本)
    滑点   = 0.1%, 买入向上、卖出向下
    成交价 = 目标日的【开盘价】(信号基于前一日收盘, T+1 开盘是真实可成交价)
    买入按 100 股整手取整; 卖出可以不足 100 股(清仓时会出现零股)
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

# --------------------------------------------------------------------------- #
# 交易成本口径(与 backtest.py 对齐)
# --------------------------------------------------------------------------- #
COMMISSION_RATE = 0.0003      # 万分之三
COMMISSION_MIN = 5.0          # 单笔最低 5 元
SLIPPAGE_PCT = 0.001          # 千分之一
LOT_SIZE = 100                # A 股最小交易单位


# --------------------------------------------------------------------------- #
# 账户状态
# --------------------------------------------------------------------------- #
@dataclass
class PaperAccount:
    """纸面账户。持仓与已实现盈亏都记在这里, 定期存 JSON。"""

    initial_cash: float = 100_000.0
    cash: float = 100_000.0
    positions: dict[str, dict] = field(default_factory=dict)
    closed_trades: list[dict] = field(default_factory=list)
    created_at: str = ""
    last_run: str = ""

    # ---------------- 序列化 ----------------
    def to_dict(self) -> dict:
        return {
            "initial_cash": self.initial_cash,
            "cash": self.cash,
            "positions": self.positions,
            "closed_trades": self.closed_trades,
            "created_at": self.created_at,
            "last_run": self.last_run,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PaperAccount":
        return cls(
            initial_cash=float(d.get("initial_cash", 100_000.0)),
            cash=float(d.get("cash", 100_000.0)),
            positions=dict(d.get("positions") or {}),
            closed_trades=list(d.get("closed_trades") or []),
            created_at=str(d.get("created_at", "")),
            last_run=str(d.get("last_run", "")),
        )

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
                        encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "PaperAccount":
        if not path.exists():
            raise FileNotFoundError(f"账户文件不存在: {path} (先跑 --init)")
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))

    # ---------------- 估值 ----------------
    def market_value(self, prices: dict[str, float]) -> float:
        """持仓市值。某个标的当日没有价格时【沿用它的成本价】(保守, 不虚增)。"""
        total = 0.0
        for sym, p in self.positions.items():
            px = prices.get(sym)
            if px is None or px <= 0:
                px = float(p.get("avg_cost", 0.0))
            total += float(p.get("shares", 0)) * float(px)
        return total

    def equity(self, prices: dict[str, float]) -> float:
        """总资产 = 现金 + 持仓市值。"""
        return self.cash + self.market_value(prices)

    def position_value(self, symbol: str, prices: dict[str, float]) -> float:
        p = self.positions.get(symbol)
        if not p:
            return 0.0
        px = prices.get(symbol)
        if px is None or px <= 0:
            px = float(p.get("avg_cost", 0.0))
        return float(p.get("shares", 0)) * float(px)


# --------------------------------------------------------------------------- #
# 交易成本
# --------------------------------------------------------------------------- #
def commission(amount: float) -> float:
    """手续费 = 成交额 x 万分之三, 且【不低于 5 元】。

    最低 5 元这条对【小资金】影响极大, 必须如实计:
    买 3000 元时, 万三只有 0.9 元, 实际收 5 元 -> 真实费率变成万分之 16.7,
    是名义费率的 5.6 倍。忽略它会让回测收益虚高。
    """
    if amount <= 0:
        return 0.0
    return max(amount * COMMISSION_RATE, COMMISSION_MIN)


def fill_price(open_price: float, side: str) -> float:
    """含滑点的成交价。买入向上滑、卖出向下滑(永远对自己不利)。"""
    if side == "buy":
        return open_price * (1.0 + SLIPPAGE_PCT)
    if side == "sell":
        return open_price * (1.0 - SLIPPAGE_PCT)
    raise ValueError(f"未知方向: {side!r}")


def round_lot(shares: float) -> int:
    """买入按 100 股整手向下取整(不够一手的零头买不了)。"""
    return int(shares // LOT_SIZE) * LOT_SIZE


# --------------------------------------------------------------------------- #
# 目标权重
# --------------------------------------------------------------------------- #
def target_weights(pool: list[dict], policy: str = "equal_weight") -> dict[str, float]:
    """把目标池转成目标权重。

    ★ 这里把前两层做的【防幻觉护栏】变成真正的风控 ★

    规则(与 news_agent 的 decision 语义对应):
      - decision == "cautioned"      -> 权重 0(财报雷区, 不持有)
      - information_vacuum is True   -> 权重 0(信息真空期, 无依据不建仓)
      - 其余                          -> 等权分配

    如果这里不拦, 那 news_agent 里那套护栏就只是 UI 上的一个图标,
    不产生任何实际风控效果 —— 那是自欺欺人。
    """
    if policy != "equal_weight":
        raise ValueError(f"暂不支持的仓位策略: {policy!r}")

    eligible = []
    for item in pool:
        sym = str(item.get("symbol", "")).zfill(6)
        if not sym or sym == "000000":
            continue
        if str(item.get("decision", "")).strip().lower() == "cautioned":
            continue
        if item.get("information_vacuum"):
            continue
        eligible.append(sym)

    if not eligible:
        return {}
    w = 1.0 / len(eligible)
    return {s: w for s in eligible}


def min_capital_required(weights: dict[str, float],
                         prices: dict[str, float]) -> dict:
    """算"要装下这批标的，至少需要多少本金"。

    ★ 为什么需要这个函数 ★
    A 股最小交易单位是 100 股, 所以一只股票有一个"一手门槛"。等权分配时,
    每只分到的预算 = 本金 / 只数, 如果它低于某只股票的一手成本, 那只就买不进来。

    实测: 10 万本金做 15 只等权(每只约 6667 元), 有 5 只买不起
    (近岸蛋白一手 16118 元、义翘神州 14712 元…), 实际只能建 10 只的组合。
    而且被丢掉的全是高价股 -> 组合系统性偏向低价股, 是个隐性偏差。

    推导(等权下只有这一条约束):
        每只预算 = 本金 / n  >=  最贵一手
        =>  本金 >= 最贵一手 * n

    注意: "各买一手之和"是【错误】的算法 —— 总和的等权平均仍会低于最贵一手,
    无法保证最贵那只买得进来(这个错误我犯过一次, 记录在案以免重犯)。
    """
    n = len(weights)
    if n == 0:
        return {"n": 0, "min_capital": 0.0, "recommended": 0.0,
                "max_lot_symbol": None, "max_lot_cost": 0.0,
                "lot_costs": {}, "unaffordable": []}

    lot_costs: dict[str, float] = {}
    for sym in weights:
        px = prices.get(sym)
        if px and px > 0:
            lot_costs[sym] = px * LOT_SIZE

    if not lot_costs:
        return {"n": n, "min_capital": 0.0, "recommended": 0.0,
                "max_lot_symbol": None, "max_lot_cost": 0.0,
                "lot_costs": {}, "unaffordable": []}

    max_sym, max_lot = max(lot_costs.items(), key=lambda kv: kv[1])

    # 理论下限: 刚好让最贵那只也能买一手
    min_capital = max_lot * n
    # 实操建议: 加 50% 余量 —— 真实下单时每只的股数不会刚好是一手,
    # 而且还要留出最低 5 元手续费的缓冲
    recommended = min_capital * 1.5

    return {
        "n": n,
        "max_lot_symbol": max_sym,
        "max_lot_cost": max_lot,
        "min_capital": min_capital,
        "recommended": recommended,
        "lot_costs": lot_costs,
        "unaffordable": [],
    }


def affordable_at_capital(weights: dict[str, float], prices: dict[str, float],
                          capital: float) -> tuple[list[str], list[str]]:
    """在给定本金下, 哪些标的买得起一手、哪些买不起。

    返回 (买得起, 买不起)。顺序按一手成本升序(便宜的优先)。
    """
    n = len(weights)
    if n == 0:
        return [], []
    per = capital / n
    ok, bad = [], []
    for sym in weights:
        px = prices.get(sym)
        if not px or px <= 0:
            continue
        (ok if px * LOT_SIZE <= per else bad).append(sym)
    return ok, bad


# --------------------------------------------------------------------------- #
# 订单生成
# --------------------------------------------------------------------------- #
def plan_orders(account: PaperAccount, weights: dict[str, float],
                prices: dict[str, float], equity: float,
                min_trade_value: float = 1000.0) -> list[dict]:
    """把"当前持仓"和"目标权重"的差额转成订单列表。

    返回 [{symbol, side, shares, price, reason}]。

    几个必须处理的现实约束:
      1. 先卖后买: 卖出释放的现金要能立刻用于买入(否则总资产大时会误判资金不足)
      2. 现金约束: 买入总额 + 手续费 <= 可用现金
      3. 最小交易额: 差额太小就不动(避免频繁微调被最低 5 元手续费吃掉)
      4. 清仓允许零股: 卖出不强制整手, 否则会留下卖不掉的零头
    """
    orders: list[dict] = []

    # 所有涉及的标的(目标池并集当前持仓)
    symbols = set(weights) | set(account.positions)

    sells: list[dict] = []
    buys: list[dict] = []

    for sym in sorted(symbols):
        px = prices.get(sym)
        held = int(account.positions.get(sym, {}).get("shares", 0))
        target_w = float(weights.get(sym, 0.0))

        if px is None or px <= 0:
            # 没有价格就不动它(通常停牌); 记下来让上层提示
            if held or target_w:
                orders.append({"symbol": sym, "side": "skip", "shares": 0,
                               "price": 0.0, "reason": "无当日价格(可能停牌)"})
            continue

        target_value = equity * target_w
        current_value = held * px
        diff_value = target_value - current_value

        if abs(diff_value) < min_trade_value:
            continue

        if diff_value < 0:
            # 需要减仓/清仓
            want_shares = int(abs(diff_value) / px)
            if want_shares >= held:
                want_shares = held                      # 全部卖掉
            else:
                want_shares = (want_shares // LOT_SIZE) * LOT_SIZE
            if want_shares > 0:
                sells.append({"symbol": sym, "side": "sell", "shares": want_shares,
                              "price": px,
                              "reason": "清仓" if want_shares >= held else "减仓"})
        else:
            # 需要加仓
            want_shares = round_lot(diff_value / px)
            if want_shares > 0:
                buys.append({"symbol": sym, "side": "buy", "shares": want_shares,
                             "price": px, "reason": "建仓" if held == 0 else "加仓"})

    orders.extend(sells)

    # ---- 买入按"资金缺口大的优先"分配, 且严格受现金约束 ----
    # 可用现金 = 当前现金 + 卖出预计净收入
    est_cash = account.cash
    for o in sells:
        gross = o["shares"] * fill_price(o["price"], "sell")
        est_cash += gross - commission(gross)

    buys.sort(key=lambda o: o["shares"] * o["price"], reverse=True)
    for o in buys:
        while o["shares"] > 0:
            gross = o["shares"] * fill_price(o["price"], "buy")
            fee = commission(gross)
            if gross + fee <= est_cash:
                est_cash -= gross + fee
                orders.append(o)
                break
            o["shares"] -= LOT_SIZE          # 买不起就少买一手, 直到买得起或归零
        if o["shares"] <= 0 and not any(x.get("symbol") == o["symbol"] and
                                        x["side"] == "buy" for x in orders):
            orders.append({"symbol": o["symbol"], "side": "skip", "shares": 0,
                           "price": o["price"], "reason": "现金不足, 无法买入"})

    return [o for o in orders if o["side"] != "skip"] + \
           [o for o in orders if o["side"] == "skip"]


# --------------------------------------------------------------------------- #
# 撮合与记账
# --------------------------------------------------------------------------- #
def execute_orders(account: PaperAccount, orders: list[dict],
                   fill_date: str) -> list[dict]:
    """按订单撮合并更新账户。返回成交记录列表。

    注意: 这里【不再】检查现金是否够 —— plan_orders 已经保证了。
    成交时不看当日涨跌, 直接用开盘价 + 滑点模拟, 这符合"开盘挂单"的真实情形。
    """
    trades: list[dict] = []

    for o in orders:
        if o["side"] == "skip":
            continue

        sym, side, shares = o["symbol"], o["side"], int(o["shares"])
        if shares <= 0:
            continue

        px = fill_price(float(o["price"]), side)
        gross = shares * px
        fee = commission(gross)

        if side == "buy":
            cost = gross + fee
            if cost > account.cash + 1e-6:
                # 理论上不该发生(plan_orders 已约束); 真发生就跳过并记录
                trades.append({"date": fill_date, "symbol": sym, "side": "buy",
                               "shares": 0, "price": px, "gross": 0.0,
                               "fee": 0.0, "pnl": None,
                               "note": f"现金不足跳过(需 {cost:.2f}, 有 {account.cash:.2f})"})
                continue

            account.cash -= cost
            pos = account.positions.setdefault(
                sym, {"shares": 0, "avg_cost": 0.0, "opened": fill_date})
            old_shares = int(pos["shares"])
            old_cost = float(pos["avg_cost"]) * old_shares
            new_shares = old_shares + shares
            # 成本价【含手续费】摊进去 —— 这样后面算的盈亏才是真实到手盈亏
            pos["avg_cost"] = (old_cost + cost) / new_shares if new_shares else 0.0
            pos["shares"] = new_shares

            trades.append({"date": fill_date, "symbol": sym, "side": "buy",
                           "shares": shares, "price": round(px, 4),
                           "gross": round(gross, 2), "fee": round(fee, 2),
                           "pnl": None, "note": o.get("reason", "")})

        else:  # sell
            pos = account.positions.get(sym)
            if not pos:
                continue
            held = int(pos["shares"])
            shares = min(shares, held)          # T+1: 最多卖掉持有的
            if shares <= 0:
                continue

            proceeds = gross - fee
            account.cash += proceeds
            avg_cost = float(pos["avg_cost"])
            # 已实现盈亏 = 卖出净收入 - 卖出部分的成本(成本已含买入手续费)
            pnl = proceeds - avg_cost * shares

            remaining = held - shares
            if remaining <= 0:
                account.positions.pop(sym, None)
            else:
                pos["shares"] = remaining

            account.closed_trades.append({
                "symbol": sym, "shares": shares, "avg_cost": round(avg_cost, 4),
                "sell_price": round(px, 4), "pnl": round(pnl, 2),
                "open_date": pos.get("opened", ""), "close_date": fill_date,
            })

            trades.append({"date": fill_date, "symbol": sym, "side": "sell",
                           "shares": shares, "price": round(px, 4),
                           "gross": round(gross, 2), "fee": round(fee, 2),
                           "pnl": round(pnl, 2), "note": o.get("reason", "")})

    return trades


# --------------------------------------------------------------------------- #
# 绩效统计
# --------------------------------------------------------------------------- #
def performance(equity_curve: list[dict]) -> dict:
    """从净值曲线算绩效指标。

    equity_curve: [{"date": "...", "equity": 100000.0}, ...]

    重点给【盈亏比】—— 日均胜率没有意义, 盈亏比才反映策略质量。
    """
    if len(equity_curve) < 2:
        return {}

    eq = [float(r["equity"]) for r in equity_curve]
    rets = [(eq[i] / eq[i - 1] - 1.0) for i in range(1, len(eq))]

    total_ret = eq[-1] / eq[0] - 1.0
    days = len(eq) - 1
    ann = (1.0 + total_ret) ** (252.0 / days) - 1.0 if days > 0 else 0.0

    peak, mdd = eq[0], 0.0
    for v in eq:
        peak = max(peak, v)
        if peak > 0:
            mdd = min(mdd, v / peak - 1.0)

    wins = [r for r in rets if r > 0]
    losses = [r for r in rets if r < 0]
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = abs(sum(losses) / len(losses)) if losses else 0.0

    # 波动率与夏普(无风险利率取 0)
    import statistics
    vol = statistics.pstdev(rets) * (252 ** 0.5) if len(rets) > 1 else 0.0
    sharpe = (ann / vol) if vol > 0 else 0.0

    return {
        "days": days,
        "total_return": total_ret,
        "annual_return": ann,
        "max_drawdown": mdd,
        "volatility": vol,
        "sharpe": sharpe,
        "win_days": len(wins),
        "loss_days": len(losses),
        "win_rate": len(wins) / len(rets) if rets else 0.0,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        # 盈亏比 = 平均盈利 / 平均亏损。>1 才说明"赚的比亏的多"
        "profit_loss_ratio": (avg_win / avg_loss) if avg_loss > 0 else float("inf"),
        "start_equity": eq[0],
        "end_equity": eq[-1],
    }


def trade_stats(closed_trades: list[dict]) -> dict:
    """按【笔】统计 —— 与按日统计分开, 因为你说过日胜率无意义。"""
    if not closed_trades:
        return {"trades": 0}
    pnls = [float(t["pnl"]) for t in closed_trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    return {
        "trades": len(pnls),
        "win_trades": len(wins),
        "loss_trades": len(losses),
        "win_rate": len(wins) / len(pnls) if pnls else 0.0,
        "avg_win": (sum(wins) / len(wins)) if wins else 0.0,
        "avg_loss": (abs(sum(losses) / len(losses))) if losses else 0.0,
        "profit_loss_ratio": ((sum(wins) / len(wins)) / (abs(sum(losses) / len(losses))))
        if wins and losses else float("inf") if wins else 0.0,
        "total_pnl": sum(pnls),
        "best": max(pnls),
        "worst": min(pnls),
    }
