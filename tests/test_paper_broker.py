#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""测试: 纸面交易的撮合与记账(paper_broker) —— 最容易悄悄算错的地方。

为什么这些测试重要:
    手续费最低 5 元、滑点方向、100 股整手、现金约束……任何一处错了,
    最后的收益曲线都是假的, 而且【不会报错】。
    所以把每条规则都固化成断言。
"""

from datetime import date

from _runner import test
import paper_broker as pb


def _mk_acct(cash=100_000.0, positions=None):
    a = pb.PaperAccount(initial_cash=cash, cash=cash)
    if positions:
        a.positions = positions
    return a


# --------------------------------------------------------------------------- #
# 手续费
# --------------------------------------------------------------------------- #
@test("手续费: 大额按万三计算")
def t_commission_rate():
    # 10 万 x 0.0003 = 30 元 > 5 元最低, 应按 30 收
    assert abs(pb.commission(100_000) - 30.0) < 1e-6


@test("★ 手续费: 小额触发【最低 5 元】(忽略它会让收益虚高)")
def t_commission_min():
    # 3000 元 x 0.0003 = 0.9 元, 但实际收 5 元
    assert pb.commission(3000) == 5.0
    # 这正是小资金最痛的地方: 真实费率变成万分之 16.7, 是名义的 5.6 倍
    assert abs(pb.commission(3000) / 3000 - 0.0003) > 0.0001


@test("手续费: 0 或负数成交额返回 0")
def t_commission_zero():
    assert pb.commission(0) == 0.0
    assert pb.commission(-100) == 0.0


@test("手续费: 刚好等于最低额的边界(16666.67 元 -> 5 元)")
def t_commission_boundary():
    # 5 / 0.0003 = 16666.67, 低于它按 5 元, 高于它按万三
    assert pb.commission(16_000) == 5.0
    assert abs(pb.commission(20_000) - 6.0) < 1e-6


# --------------------------------------------------------------------------- #
# 滑点
# --------------------------------------------------------------------------- #
@test("★ 滑点方向: 买入向上(更贵)、卖出向下(更便宜), 永远对自己不利")
def t_slippage_direction():
    buy = pb.fill_price(10.0, "buy")
    sell = pb.fill_price(10.0, "sell")
    assert buy > 10.0, f"买入成交价应高于开盘价, 实际 {buy}"
    assert sell < 10.0, f"卖出成交价应低于开盘价, 实际 {sell}"
    assert abs(buy - 10.0 * 1.001) < 1e-9
    assert abs(sell - 10.0 * 0.999) < 1e-9


@test("滑点: 未知方向抛异常(不静默返回错误价格)")
def t_slippage_bad_side():
    try:
        pb.fill_price(10.0, "hold")
        assert False, "应该抛 ValueError"
    except ValueError:
        pass


# --------------------------------------------------------------------------- #
# 整手
# --------------------------------------------------------------------------- #
@test("整手: 买入向下取整到 100 股")
def t_round_lot():
    assert pb.round_lot(150) == 100
    assert pb.round_lot(99) == 0
    assert pb.round_lot(100) == 100
    assert pb.round_lot(1999) == 1900
    assert pb.round_lot(0) == 0


# --------------------------------------------------------------------------- #
# 目标权重: 护栏是否真的影响仓位
# --------------------------------------------------------------------------- #
@test("★ 目标权重: cautioned(财报雷区) 必须被排除 -> 权重 0")
def t_weight_excludes_cautioned():
    pool = [
        {"symbol": "600519", "decision": "favored"},
        {"symbol": "600036", "decision": "cautioned"},
    ]
    w = pb.target_weights(pool)
    assert "600036" not in w, "cautioned 不该出现在持仓目标里 —— 否则护栏形同虚设"
    assert "600519" in w
    assert abs(w["600519"] - 1.0) < 1e-9, "只剩一只时应拿满仓"


@test("★ 目标权重: 信息真空期必须被排除")
def t_weight_excludes_vacuum():
    pool = [
        {"symbol": "600519", "decision": "neutral"},
        {"symbol": "600036", "decision": "neutral", "information_vacuum": True},
    ]
    w = pb.target_weights(pool)
    assert "600036" not in w, "信息真空期不该建仓(无依据不买)"
    assert "600519" in w


@test("目标权重: 剩下的等权分配, 权重和为 1")
def t_weight_equal():
    pool = [{"symbol": f"60000{i}", "decision": "neutral"} for i in range(4)]
    for p in pool:
        p["symbol"] = p["symbol"].zfill(6)
    w = pb.target_weights(pool)
    assert len(w) == 4
    assert abs(sum(w.values()) - 1.0) < 1e-9
    for v in w.values():
        assert abs(v - 0.25) < 1e-9


@test("目标权重: 全部被排除时返回空字典(空仓, 而不是乱买)")
def t_weight_all_excluded():
    pool = [{"symbol": "600519", "decision": "cautioned"},
            {"symbol": "600036", "decision": "cautioned"}]
    assert pb.target_weights(pool) == {}


@test("目标权重: symbol 会补零到 6 位")
def t_weight_pad():
    w = pb.target_weights([{"symbol": "1", "decision": "neutral"}])
    assert "000001" in w, f"应补零成 000001, 实际 {list(w)}"


# --------------------------------------------------------------------------- #
# 订单生成
# --------------------------------------------------------------------------- #
@test("订单: 空仓 + 目标权重 -> 生成买入单, 且按整手")
def t_plan_buy():
    acct = _mk_acct(100_000)
    prices = {"600519": 100.0}
    orders = pb.plan_orders(acct, {"600519": 0.5}, prices, 100_000)
    buys = [o for o in orders if o["side"] == "buy"]
    assert len(buys) == 1
    assert buys[0]["shares"] % 100 == 0, "买入必须是整手"


@test("★ 订单: 现金约束 —— 买入总额 + 手续费不得超过可用现金")
def t_plan_cash_constraint():
    acct = _mk_acct(10_000)          # 钱很少
    prices = {"600519": 100.0}
    orders = pb.plan_orders(acct, {"600519": 1.0}, prices, 10_000)
    buys = [o for o in orders if o["side"] == "buy"]
    if buys:
        need = sum(o["shares"] * pb.fill_price(o["price"], "buy") +
                   pb.commission(o["shares"] * pb.fill_price(o["price"], "buy"))
                   for o in buys)
        assert need <= acct.cash + 1e-6, f"买入需 {need:.2f} 但只有 {acct.cash:.2f}"


@test("订单: 清仓时允许卖出零股(不强制整手)")
def t_plan_sell_odd_lot():
    # 持有 150 股, 目标权重 0 -> 必须能全部卖掉
    acct = _mk_acct(10_000, {"600519": {"shares": 150, "avg_cost": 100.0}})
    orders = pb.plan_orders(acct, {}, {"600519": 100.0}, 25_000)
    sells = [o for o in orders if o["side"] == "sell"]
    assert len(sells) == 1
    assert sells[0]["shares"] == 150, "清仓应卖掉全部 150 股(含零股)"


@test("订单: 差额太小则不动(避免被最低 5 元手续费吃掉)")
def t_plan_min_trade():
    acct = _mk_acct(100_000, {"600519": {"shares": 100, "avg_cost": 100.0}})
    # 目标市值 10_010 与当前 10_000 只差 10 元 -> 不该动
    orders = pb.plan_orders(acct, {"600519": 0.1001}, {"600519": 100.0}, 100_000)
    real = [o for o in orders if o["side"] in ("buy", "sell")]
    assert real == [], f"差额过小不该产生订单, 实际 {real}"


@test("订单: 无价格的标的被跳过并给出原因(停牌场景)")
def t_plan_no_price():
    acct = _mk_acct(100_000, {"600519": {"shares": 100, "avg_cost": 100.0}})
    orders = pb.plan_orders(acct, {"600519": 0.0}, {}, 100_000)
    skips = [o for o in orders if o["side"] == "skip"]
    assert len(skips) == 1, "无价格应记一笔 skip"
    assert "停牌" in skips[0]["reason"] or "价格" in skips[0]["reason"]


# --------------------------------------------------------------------------- #
# 撮合记账
# --------------------------------------------------------------------------- #
@test("★ 撮合: 买入后现金减少 = 成交额 + 手续费, 持仓增加")
def t_execute_buy():
    acct = _mk_acct(100_000)
    orders = [{"symbol": "600519", "side": "buy", "shares": 100,
               "price": 100.0, "reason": "建仓"}]
    before = acct.cash
    trades = pb.execute_orders(acct, orders, "2026-10-12")

    px = pb.fill_price(100.0, "buy")          # 100.1
    gross = 100 * px                           # 10010
    fee = pb.commission(gross)                 # 3.003
    assert len(trades) == 1
    assert abs(acct.cash - (before - gross - fee)) < 1e-6
    assert acct.positions["600519"]["shares"] == 100
    # 成本价含手续费
    assert abs(acct.positions["600519"]["avg_cost"] - (gross + fee) / 100) < 1e-6


@test("★ 撮合: 卖出后现金增加 = 成交额 - 手续费, 并记一笔已实现盈亏")
def t_execute_sell():
    acct = _mk_acct(0, {"600519": {"shares": 100, "avg_cost": 100.0,
                                   "opened": "2026-10-01"}})
    orders = [{"symbol": "600519", "side": "sell", "shares": 100,
               "price": 110.0, "reason": "清仓"}]
    trades = pb.execute_orders(acct, orders, "2026-10-12")

    px = pb.fill_price(110.0, "sell")          # 109.89
    gross = 100 * px
    fee = pb.commission(gross)
    assert len(trades) == 1
    assert abs(acct.cash - (gross - fee)) < 1e-6
    assert "600519" not in acct.positions, "清仓后不该还有持仓"
    assert len(acct.closed_trades) == 1
    # 盈亏 = 卖出净收入 - 成本(100*100)
    expect_pnl = (gross - fee) - 100 * 100.0
    assert abs(acct.closed_trades[0]["pnl"] - expect_pnl) < 0.01


@test("★ 撮合: 部分卖出后剩余持仓数量正确, 且不记平仓")
def t_execute_partial_sell():
    acct = _mk_acct(0, {"600519": {"shares": 200, "avg_cost": 100.0}})
    orders = [{"symbol": "600519", "side": "sell", "shares": 100,
               "price": 110.0, "reason": "减仓"}]
    pb.execute_orders(acct, orders, "2026-10-12")
    assert acct.positions["600519"]["shares"] == 100, "应剩 100 股"
    assert len(acct.closed_trades) == 1, "部分卖出也应记一笔盈亏"


@test("撮合: 卖出数量超过持仓时被截断(不会卖成负数)")
def t_execute_sell_clamp():
    acct = _mk_acct(0, {"600519": {"shares": 50, "avg_cost": 100.0}})
    orders = [{"symbol": "600519", "side": "sell", "shares": 500,
               "price": 110.0, "reason": "x"}]
    trades = pb.execute_orders(acct, orders, "2026-10-12")
    assert trades[0]["shares"] == 50, "最多只能卖掉持有的 50 股"
    assert "600519" not in acct.positions


@test("撮合: 买入现金不足时跳过并留痕(不产生负现金)")
def t_execute_buy_guard():
    acct = _mk_acct(100)
    orders = [{"symbol": "600519", "side": "buy", "shares": 100,
               "price": 100.0, "reason": "x"}]
    trades = pb.execute_orders(acct, orders, "2026-10-12")
    assert acct.cash == 100, "现金不该变"
    assert trades[0]["shares"] == 0, "该笔未成交"
    assert "现金不足" in trades[0]["note"]


@test("撮合: skip 订单不产生成交")
def t_execute_skip():
    acct = _mk_acct(100_000)
    trades = pb.execute_orders(acct, [{"symbol": "600519", "side": "skip",
                                       "shares": 0, "price": 0.0}], "2026-10-12")
    assert trades == []


@test("撮合: 多次买入后成本价是加权平均(含各自手续费)")
def t_execute_avg_cost():
    acct = _mk_acct(100_000)
    pb.execute_orders(acct, [{"symbol": "600519", "side": "buy", "shares": 100,
                              "price": 100.0}], "2026-10-12")
    pb.execute_orders(acct, [{"symbol": "600519", "side": "buy", "shares": 100,
                              "price": 200.0}], "2026-10-13")
    assert acct.positions["600519"]["shares"] == 200
    ac = acct.positions["600519"]["avg_cost"]
    # 两次成本: (100*100.1 + fee) + (100*200.2 + fee), 除以 200
    c1 = 100 * pb.fill_price(100.0, "buy"); c1 += pb.commission(c1)
    c2 = 100 * pb.fill_price(200.0, "buy"); c2 += pb.commission(c2)
    assert abs(ac - (c1 + c2) / 200) < 1e-6, f"成本价应为加权平均, 实际 {ac}"


# --------------------------------------------------------------------------- #
# 估值
# --------------------------------------------------------------------------- #
@test("估值: 总资产 = 现金 + 持仓市值")
def t_equity():
    acct = _mk_acct(5_000, {"600519": {"shares": 100, "avg_cost": 100.0}})
    prices = {"600519": 110.0}
    assert abs(acct.market_value(prices) - 11_000) < 1e-6
    assert abs(acct.equity(prices) - 16_000) < 1e-6


@test("★ 估值: 缺价格时沿用成本价(不把持仓估成 0)")
def t_equity_missing_price():
    acct = _mk_acct(5_000, {"600519": {"shares": 100, "avg_cost": 100.0}})
    # 停牌, 没有当日价格
    assert abs(acct.market_value({}) - 10_000) < 1e-6, "缺价格应沿用成本价"


# --------------------------------------------------------------------------- #
# 绩效统计
# --------------------------------------------------------------------------- #
@test("绩效: 单调上涨的净值曲线 -> 无回撤、胜率 100%")
def t_perf_up():
    # 11 个点 -> 10 个收益区间 (days = len(curve) - 1)
    curve = [{"date": f"2026-10-{i:02d}", "equity": 100_000 * (1.01 ** i)}
             for i in range(1, 12)]
    p = pb.performance(curve)
    assert p["days"] == 10, f"11 个点应有 10 个区间, 实际 {p['days']}"
    assert p["total_return"] > 0
    assert abs(p["max_drawdown"]) < 1e-9, "一直涨就不该有回撤"
    assert p["win_rate"] == 1.0


@test("★ 绩效: 盈亏比 = 平均盈 / 平均亏, 且能识别亏损策略")
def t_perf_pl_ratio():
    # 涨 2% / 跌 1% 交替 -> 盈亏比应约 2
    eq, curve = 100_000.0, []
    for i in range(1, 21):
        eq *= 1.02 if i % 2 else 0.99
        curve.append({"date": f"d{i}", "equity": eq})
    p = pb.performance(curve)
    assert abs(p["profit_loss_ratio"] - 2.0) < 0.05, \
        f"盈亏比应约 2.0, 实际 {p['profit_loss_ratio']:.3f}"


@test("绩效: 最大回撤能识别下跌")
def t_perf_drawdown():
    curve = [{"date": "d1", "equity": 100_000},
             {"date": "d2", "equity": 80_000},
             {"date": "d3", "equity": 90_000}]
    p = pb.performance(curve)
    assert abs(p["max_drawdown"] - (-0.20)) < 1e-9, \
        f"最大回撤应为 -20%, 实际 {p['max_drawdown']}"


@test("绩效: 数据不足 2 个点时返回空(不硬算)")
def t_perf_too_few():
    assert pb.performance([]) == {}
    assert pb.performance([{"date": "d1", "equity": 100}]) == {}


@test("按笔统计: 全盈时盈亏比为 inf, 且总盈亏正确")
def t_trade_stats():
    trades = [{"pnl": 100.0}, {"pnl": 300.0}]
    s = pb.trade_stats(trades)
    assert s["trades"] == 2 and s["win_trades"] == 2
    assert s["total_pnl"] == 400.0
    assert s["profit_loss_ratio"] == float("inf")


@test("按笔统计: 空列表安全返回")
def t_trade_stats_empty():
    assert pb.trade_stats([]) == {"trades": 0}


# --------------------------------------------------------------------------- #
# 一手门槛(本金不足时买不起高价股)
# --------------------------------------------------------------------------- #
@test("★ 一手门槛: 最低本金 = 最贵一手 x 只数")
def t_min_capital():
    # 3 只, 最贵一手 100 x 100 = 10000 -> 最低需要 30000
    w = {"a": 1 / 3, "b": 1 / 3, "c": 1 / 3}
    px = {"a": 100.0, "b": 50.0, "c": 10.0}
    info = pb.min_capital_required(w, px)
    assert info["n"] == 3
    assert info["max_lot_cost"] == 100.0 * 100
    assert info["min_capital"] == 100.0 * 100 * 3, \
        f"最低本金应为 30000, 实际 {info['min_capital']}"


@test("★ 一手门槛: 建议本金 >= 最低本金(留了余量)")
def t_recommended_ge_min():
    w = {"a": 0.5, "b": 0.5}
    px = {"a": 80.0, "b": 20.0}
    info = pb.min_capital_required(w, px)
    assert info["recommended"] >= info["min_capital"]


@test("★ 一手门槛: 最低本金下, 等权预算必须够最贵那一手")
def t_min_capital_sufficient():
    w = {"a": 1 / 3, "b": 1 / 3, "c": 1 / 3}
    px = {"a": 161.18, "b": 20.0, "c": 11.0}
    info = pb.min_capital_required(w, px)
    per = info["min_capital"] / info["n"]
    assert per >= info["max_lot_cost"] - 1e-9, \
        f"最低本金下每只预算 {per:.0f} 仍低于最贵一手 {info['max_lot_cost']:.0f}"


@test("一手门槛: 空权重安全返回")
def t_min_capital_empty():
    info = pb.min_capital_required({}, {})
    assert info["n"] == 0 and info["min_capital"] == 0.0


@test("★ 一手门槛: 缺价格的标的被跳过(不计入 lot_costs)")
def t_min_capital_missing_price():
    w = {"a": 0.5, "b": 0.5}
    info = pb.min_capital_required(w, {"a": 50.0})   # b 没价格
    assert "b" not in info["lot_costs"], "无价格的标的不该参与门槛计算"
    assert info["max_lot_cost"] == 50.0 * 100


@test("affordable_at_capital: 正确分出买得起与买不起")
def t_affordable():
    w = {"cheap": 1 / 3, "mid": 1 / 3, "rich": 1 / 3}
    px = {"cheap": 11.0, "mid": 40.0, "rich": 161.0}
    # 3 只等权, 本金 12000 -> 每只预算 4000
    # cheap 一手 1100 (行), mid 一手 4000 (刚好), rich 一手 16100 (不行)
    ok, bad = pb.affordable_at_capital(w, px, 12_000)
    assert "cheap" in ok and "mid" in ok
    assert "rich" in bad, f"rich 应买不起, 实际 ok={ok} bad={bad}"


@test("affordable_at_capital: 本金充足时全部买得起")
def t_affordable_all():
    w = {"a": 0.5, "b": 0.5}
    px = {"a": 100.0, "b": 200.0}
    ok, bad = pb.affordable_at_capital(w, px, 100_000)
    assert bad == [], f"10 万应都买得起, 买不起={bad}"


@test("affordable_at_capital: 空权重返回两个空列表")
def t_affordable_empty():
    assert pb.affordable_at_capital({}, {}, 10_000) == ([], [])


# --------------------------------------------------------------------------- #
# 序列化
# --------------------------------------------------------------------------- #
@test("账户: 存盘后读回, 状态完全一致")
def t_account_roundtrip():
    """注意: 临时目录用【项目内 .tmp_test/】而不是系统 %TEMP% ——
    某些受限环境禁止写系统临时目录(实测 PermissionError WinError 5)。"""
    from pathlib import Path
    d = Path(".tmp_test_paper")
    d.mkdir(parents=True, exist_ok=True)
    try:
        acct = _mk_acct(12_345.67, {"600519": {"shares": 100, "avg_cost": 101.5,
                                               "opened": "2026-10-01"}})
        acct.closed_trades = [{"symbol": "x", "pnl": 5.0}]
        f = d / "acc.json"
        acct.save(f)
        back = pb.PaperAccount.load(f)
        assert abs(back.cash - acct.cash) < 1e-9
        assert back.positions == acct.positions
        assert back.closed_trades == acct.closed_trades
        assert abs(back.initial_cash - acct.initial_cash) < 1e-9
    finally:
        for p in d.glob("*"):
            p.unlink(missing_ok=True)
        d.rmdir()
