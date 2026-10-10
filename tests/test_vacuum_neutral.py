#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""测试: 信息真空期的代码层强制改写(enforce_vacuum_neutral) —— ★本文件是核心★

为什么这个测试最重要:
    "信息真空期"是 README 与简历里最亮的卖点之一, 但【实测中它一次都没触发过】
    (每次都打印"没有'信息真空期却非中性'的结论")。也就是说, 它此前只是
    "我设计了它", 而不是 "它被验证过有效"。

    本文件用【注入】的方式直接构造模型的错误输出, 验证护栏确实会拦下它:
        无新闻依据 + 无财报数据 + 模型给了 favored/cautioned
        -> 必须被强制改写为 neutral, 并记入改写日志

设计原则:
    只依赖 enforce_vacuum_neutral 这个纯函数, 不调用任何大模型、不联网。
    所以它跑得飞快(毫秒级), 可以在每次改动后都跑。
"""

from _runner import test
import news_agent as na


def _mk(symbol, decision, has_news, evidence=None, confidence=0.9):
    """造一条模型输出的"结果行"。"""
    return {
        "symbol": symbol,
        "decision": decision,
        "confidence": confidence,
        "reason": "模型给的理由",
        "cited_evidence": evidence if evidence is not None else [],
        "has_news": has_news,
    }


# --------------------------------------------------------------------------- #
# 核心: 真空调理应被强制改为 neutral
# --------------------------------------------------------------------------- #
@test("★ 注入: 无新闻+无财报 却给 favored -> 强制改写为 neutral")
def t_force_favored():
    results = [_mk("111111", "favored", has_news=False)]
    out, rewritten = na.enforce_vacuum_neutral(results, {})
    assert out[0]["decision"] == "neutral", \
        f"应被改为 neutral, 实际 {out[0]['decision']!r}"
    assert len(rewritten) == 1, f"应记 1 条改写日志, 实际 {len(rewritten)}"
    assert "111111" in rewritten[0], f"改写日志应含代码, 实际 {rewritten[0]!r}"
    assert "favored" in rewritten[0], "改写日志应含原结论"


@test("★ 注入: 无新闻+无财报 却给 cautioned -> 也强制改为 neutral")
def t_force_cautioned():
    """注意: 无数据【不是利空】, 所以 cautioned 同样要被拦。"""
    results = [_mk("222222", "cautioned", has_news=False)]
    out, rewritten = na.enforce_vacuum_neutral(results, {})
    assert out[0]["decision"] == "neutral", \
        f"应被改为 neutral, 实际 {out[0]['decision']!r}"
    assert len(rewritten) == 1


@test("★ 强制改写时 confidence 必须清零(不能保留模型的高置信度)")
def t_confidence_reset():
    results = [_mk("333333", "favored", has_news=False, confidence=0.99)]
    out, _ = na.enforce_vacuum_neutral(results, {})
    assert out[0]["confidence"] == 0.0, \
        f"改写后 confidence 应清零, 实际 {out[0]['confidence']}"


@test("★ 强制改写时 reason 必须留痕(写明是代码层改写)")
def t_reason_traced():
    results = [_mk("444444", "favored", has_news=False)]
    out, _ = na.enforce_vacuum_neutral(results, {})
    reason = out[0]["reason"]
    assert "强制改写" in reason, f"reason 应标明是强制改写, 实际 {reason!r}"
    assert "favored" in reason, "reason 应保留原结论以便追溯"


# --------------------------------------------------------------------------- #
# 不该被改写的正常情况(防止护栏"过度拦截")
# --------------------------------------------------------------------------- #
@test("不该改: 有新闻依据的 favored 必须保留")
def t_keep_favored_with_news():
    results = [_mk("555555", "favored", has_news=True,
                   evidence=["[3] 化工周期供给收缩"])]
    out, rewritten = na.enforce_vacuum_neutral(results, {})
    assert out[0]["decision"] == "favored", "有新闻依据的结论不该被改"
    assert rewritten == [], f"不该有改写记录, 实际 {rewritten}"


@test("不该改: 无新闻但【有财报数据】的 cautioned 必须保留")
def t_keep_cautioned_with_earnings():
    """财报雷区的 cautioned 是有依据的, 不能被当成信息真空。"""
    results = [_mk("666666", "cautioned", has_news=False, evidence=[])]
    earnings = {"666666": {"date": "2026-10-23", "soon": True, "disclosed": False}}
    out, rewritten = na.enforce_vacuum_neutral(results, earnings)
    assert out[0]["decision"] == "cautioned", \
        "有财报数据的 cautioned 不该被改(它不是信息真空)"
    assert rewritten == []


@test("不该改: 本来就是 neutral 的保持不动")
def t_keep_neutral():
    results = [_mk("777777", "neutral", has_news=False, confidence=0.0)]
    out, rewritten = na.enforce_vacuum_neutral(results, {})
    assert out[0]["decision"] == "neutral"
    assert rewritten == [], "已经是 neutral 不该记改写"


@test("不该改: has_news=False 但给了 cited_evidence 的, 视为有依据")
def t_evidence_counts_as_signal():
    """判定条件是"无 has_news 且无 evidence", 二者有其一就不算真空。"""
    results = [_mk("888888", "favored", has_news=False,
                   evidence=["[7] 某行业利好"])]
    out, rewritten = na.enforce_vacuum_neutral(results, {})
    assert out[0]["decision"] == "favored", \
        "有 cited_evidence 应视为有依据, 不该被改写"
    assert rewritten == []


# --------------------------------------------------------------------------- #
# 边界与健壮性
# --------------------------------------------------------------------------- #
@test("空列表输入不崩, 返回空结果")
def t_empty_input():
    out, rewritten = na.enforce_vacuum_neutral([], {})
    assert out == [], f"期望空列表, 实际 {out!r}"
    assert rewritten == []


@test("多只混合: 只有真空期那几只被改, 其余不受影响")
def t_mixed():
    results = [
        _mk("A00001", "favored", has_news=False),                    # 真空 -> 改
        _mk("A00002", "favored", has_news=True, evidence=["[1] x"]),  # 有依据 -> 留
        _mk("A00003", "cautioned", has_news=False),                   # 真空 -> 改
        _mk("A00004", "neutral", has_news=False),                     # 已中性 -> 留
    ]
    earnings = {"A00002": {"date": "2026-11-01", "soon": False, "disclosed": False}}
    out, rewritten = na.enforce_vacuum_neutral(results, earnings)
    got = {r["symbol"]: r["decision"] for r in out}
    assert got["A00001"] == "neutral", "A00001 应被改"
    assert got["A00002"] == "favored", "A00002 有新闻依据应保留"
    assert got["A00003"] == "neutral", "A00003 应被改"
    assert got["A00004"] == "neutral", "A00004 本来就中性"
    assert len(rewritten) == 2, f"应记 2 条改写, 实际 {len(rewritten)}: {rewritten}"


@test("大小写不敏感: 'FAVORED' 也应被识别并改写")
def t_case_insensitive():
    results = [_mk("B00001", "FAVORED", has_news=False)]
    out, rewritten = na.enforce_vacuum_neutral(results, {})
    assert out[0]["decision"] == "neutral", "大写结论也应被拦下"
    assert len(rewritten) == 1


@test("symbol 补零: 传 '1' 也能正确匹配 earnings 里的 '000001'")
def t_symbol_padding():
    """财报日历里的代码是 6 位, 模型可能返回不补零的形式。"""
    results = [{"symbol": "1", "decision": "favored", "confidence": 0.9,
                "reason": "x", "cited_evidence": [], "has_news": False}]
    earnings = {"000001": {"date": "2026-10-23", "soon": True, "disclosed": False}}
    out, rewritten = na.enforce_vacuum_neutral(results, earnings)
    assert out[0]["decision"] == "cautioned" or out[0]["decision"] == "favored", \
        "有财报数据时不该被判定为信息真空"
    # 关键断言: 不能因为代码没补零就误判成真空
    assert "强制改写" not in str(out[0].get("reason", "")), \
        "补零不匹配导致误判为信息真空 —— 这是个真 bug"
