#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
news_agent.py — 第二层: 宏观新闻 + 财报预警 过滤

定位
----
把第一层(market_screener.py)产出的候选股, 结合【宏观新闻】与【财报披露日历】
做一次过滤, 收敛成目标池(config/target_pool.json), 供第三层多智能体深度分析。

数据源(2026-10 实测确定)
------------------------
1. 宏观新闻   : akshare.stock_news_main_cx()  —— 财新网约 100 条, 0.7s, 稳定。
                字段 tag / summary / url, summary 是中位 45 字的摘要级文本。
2. 财报日历   : akshare.stock_yysj_em("沪深A股", date=报告期) —— 东财业绩预约披露
                时间表, 约 5200 行, 覆盖全部 A 股, 含"首次预约时间/实际披露时间"。

为什么不用个股新闻接口
----------------------
akshare.stock_news_em(个股新闻)已失效, 且有两层原因, 都不是"限流":
  a) akshare 自身 bug: 用 .str.replace(r"\\u3000", ...) 触发 pandas 3.0/pyarrow
     的 "invalid escape sequence: \\u" —— 请求成功但清洗时崩;
  b) 更根本的: 直连该 JSONP 接口, 把 param 故意写成非法字符串, 服务端返回
     与正常请求【完全相同】的响应(只有 passportWeb) —— 说明它已完全忽略请求体,
     属于接口契约变更, 修 bug 也救不回来。
所以本层改为"宏观新闻 + 财报预警"组合: 放弃逐只个股新闻, 换取稳定可靠的数据源。

核心分析逻辑
------------
采用【一次大模型调用】完成全部判断(绝不每只股票调一次, 既省钱又避免单点中断):
  输入 = 100 条宏观新闻摘要(带编号, 供引用) + 候选股清单(带动量 + 财报标记)
  输出 = 结构化筛选结果(每只股票的 decision / 理由 / 引用依据)

两条硬性安全规则
----------------
1. 财报雷区: 候选股中若在窗口期(默认未来 10 个自然日)内要披露财报, 数据里会打上
   [⚠️即将财报, 波动率放大] 标签, 提示词要求模型必须给出"极度谨慎/建议观望"。
2. 信息真空期(双保险):
   - 提示词层: 明确禁止把"无新闻且无财报数据"解读为利好, 要求输出中性;
   - 代码层: 若某股票既无新闻命中又无近期财报, 而模型却给了非中性结论,
     会【强制改写为 neutral】并记 warning —— 不让模型的幻觉绕过这条安全底线。

失败隔离
--------
抓不到新闻或没有财报数据的股票【保留并标注】, 绝不从目标池剔除。
"没有数据"不等于"利空", 也不等于"利好", 而是信息真空期。

用法
----
    python src/news_agent.py                      # 默认: 前 15 只, 财报窗口 10 天
    python src/news_agent.py --top-n 10
    python src/news_agent.py --earnings-window 25  # 放宽财报窗口(便于观察预警效果)
    python src/news_agent.py --fetch-full-text     # 抓新闻正文而非摘要(慢, 默认关)
    python src/news_agent.py --dry-run             # 只抓数据+拼提示词, 不调模型
    python src/news_agent.py --check               # 只列出会选哪些, 不写文件
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

# --------------------------------------------------------------------------- #
# 路径引导
# --------------------------------------------------------------------------- #
_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent if (_HERE.parent / "config.py").exists() else _HERE
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from config import (  # noqa: E402
    AK_RETRY_BACKOFF, AK_RETRY_TIMES, CONFIG_DATA_DIR, LOGS_DIR,
    TIMEZONE_NAME, now_bj,
)

CANDIDATES_FILE = CONFIG_DATA_DIR / "candidates.json"
TARGET_POOL_FILE = CONFIG_DATA_DIR / "target_pool.json"
LOG_FILE = LOGS_DIR / "news_agent.log"

DEFAULT_TOP_N = 15
DEFAULT_EARNINGS_WINDOW = 10        # 未来 N 个自然日内披露财报 -> 打预警标签
FULL_TEXT_LIMIT = 1500              # --fetch-full-text 时每篇正文截断长度

# 提示词里告诉模型的分类取值(也是代码层校验的依据)
DECISION_NEUTRAL = "neutral"
DECISION_FAVORED = "favored"
DECISION_CAUTIONED = "cautioned"
VALID_DECISIONS = {DECISION_NEUTRAL, DECISION_FAVORED, DECISION_CAUTIONED}

log = logging.getLogger("news_agent")


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
        fh = logging.FileHandler(LOG_FILE, mode="a", encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)
        log.addHandler(fh)
    except Exception as exc:
        print(f"[警告] 无法写入日志文件 {LOG_FILE}: {exc}")

    for noisy in ("urllib3", "akshare", "chardet"):
        logging.getLogger(noisy).setLevel(logging.ERROR)


# --------------------------------------------------------------------------- #
# 通用: 重试
# --------------------------------------------------------------------------- #
def with_retry(func, what: str, times: int = AK_RETRY_TIMES,
               backoff: float = AK_RETRY_BACKOFF):
    """带指数退避的重试。全部失败抛出最后一次异常。"""
    last_exc: Exception | None = None
    for attempt in range(1, times + 1):
        try:
            return func()
        except Exception as exc:
            last_exc = exc
            if attempt < times:
                wait = backoff * attempt
                log.debug("%s 第 %d/%d 次失败(%s), %.0fs 后重试",
                          what, attempt, times, type(exc).__name__, wait)
                time.sleep(wait)
    assert last_exc is not None
    raise last_exc


# --------------------------------------------------------------------------- #
# 报告期推算
# --------------------------------------------------------------------------- #
def current_report_period(today: date | None = None) -> str:
    """推算当前应关注的财报报告期(最近一个已结束的季度末), 返回 YYYYMMDD。

    不能写死日期: 系统要长期运行, 报告期必须随当前时间滚动。
    规则: 落在哪个区间, 就取该区间之前最近的那个季度末。
      1/1 ~ 3/31   -> 上年 12-31 (年报)
      4/1 ~ 6/30   -> 当年 03-31 (一季报)
      7/1 ~ 9/30   -> 当年 06-30 (中报)
      10/1 ~ 12/31 -> 当年 09-30 (三季报)

    注意: 刚过季度末的几天里, 上一期的披露可能还没结束, 这里仍取新一期。
    调用方会在命中为空时打印池内最近的披露日, 便于判断是否只是"时点问题"。
    """
    d = today or now_bj().date()
    if d.month <= 3:
        return f"{d.year - 1}1231"
    if d.month <= 6:
        return f"{d.year}0331"
    if d.month <= 9:
        return f"{d.year}0630"
    return f"{d.year}0930"


# --------------------------------------------------------------------------- #
# 数据源 1: 宏观新闻(财新)
# --------------------------------------------------------------------------- #
def fetch_macro_news(limit: int = 100) -> list[dict]:
    """抓财新宏观新闻。返回 [{tag, summary, url}]。

    这是本层的主数据源, 失败时直接抛出(宏观研判没有替代品)。
    """
    import akshare as ak

    def _call():
        df = ak.stock_news_main_cx()
        if df is None or df.empty:
            raise RuntimeError("stock_news_main_cx 返回空数据")
        return df

    df = with_retry(_call, "宏观新闻", times=AK_RETRY_TIMES)
    out: list[dict] = []
    for _, r in df.head(limit).iterrows():
        summary = str(r.get("summary", "") or "").replace("\n", " ").strip()
        if not summary:
            continue
        out.append({
            "tag": str(r.get("tag", "") or "").strip(),
            "summary": summary,
            "url": str(r.get("url", "") or "").strip(),
        })
    return out


def fetch_article_text(url: str, limit: int = FULL_TEXT_LIMIT) -> str:
    """抓新闻正文(--fetch-full-text 时用)。失败返回空串, 不影响主流程。"""
    try:
        import requests
        r = requests.get(url, timeout=20, headers={
            "user-agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/142.0.0.0")})
        r.raise_for_status()
        text = re.sub(r"<script.*?</script>", " ", r.text, flags=re.S)
        text = re.sub(r"<style.*?</style>", " ", text, flags=re.S)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:limit]
    except Exception:
        return ""


# --------------------------------------------------------------------------- #
# 数据源 2: 财报披露日历(东财业绩预约披露时间)
# --------------------------------------------------------------------------- #
def fetch_earnings_calendar(report_period: str) -> pd.DataFrame:
    """抓全 A 股业绩预约披露时间表。返回原始 DataFrame。

    失败时返回空 DataFrame(而不是抛异常): 财报数据缺失不应中断整个流程,
    只会导致所有股票标注"无近期财报数据"(信息真空期, 中性处理)。
    """
    import akshare as ak

    def _call():
        return ak.stock_yysj_em(symbol="沪深A股", date=report_period)

    try:
        df = with_retry(_call, f"财报日历({report_period})", times=AK_RETRY_TIMES)
    except Exception as exc:
        log.warning("财报日历获取失败(%s: %s), 本次将全部按'无财报数据'处理",
                    type(exc).__name__, exc)
        return pd.DataFrame()

    if df is None or df.empty:
        log.warning("财报日历返回空数据(报告期 %s), 本次将全部按'无财报数据'处理",
                    report_period)
        return pd.DataFrame()
    return df


def _pick_disclosure_date(row: pd.Series) -> tuple[date | None, bool]:
    """从财报日历的一行里取出"最快披露日"以及是否已实际披露。

    优先级: 实际披露时间 > 一次变更 > 二次变更 > 三次变更 > 首次预约时间。
    为什么要看"实际披露时间": 它有值说明财报已经出了, 就不再是"即将披露"的雷区。
    """
    order = ["实际披露时间", "一次变更日期", "二次变更日期", "三次变更日期", "首次预约时间"]
    actual_raw = row.get("实际披露时间")
    actual = pd.to_datetime(actual_raw, errors="coerce")
    disclosed = not pd.isna(actual)

    for col in order:
        if col not in row.index:
            continue
        v = pd.to_datetime(row.get(col), errors="coerce")
        if not pd.isna(v):
            return v.date(), disclosed
    return None, disclosed


def match_earnings(cands: list[dict], cal: pd.DataFrame,
                   window_days: int) -> tuple[dict[str, dict], dict]:
    """把财报日历与候选股交叉匹配。

    返回 (匹配结果, 窗口统计)。匹配结果形如:
        {"600418": {"date": date(2026,10,23), "soon": False, "disclosed": False}}
    只保留【尚未披露】的记录参与"即将财报"判定。
    """
    today = now_bj().date()
    win_to = today + timedelta(days=window_days)
    matched: dict[str, dict] = {}
    soon_list: list[str] = []
    nearest: date | None = None

    if cal is None or cal.empty:
        return matched, {"from": str(today), "to": str(win_to),
                         "matched": 0, "soon": 0, "nearest": None}

    code_col = next((c for c in cal.columns if "代码" in str(c)), None)
    if code_col is None:
        log.warning("财报日历里找不到'股票代码'列, 实际列=%s", list(cal.columns))
        return matched, {"from": str(today), "to": str(win_to),
                         "matched": 0, "soon": 0, "nearest": None}

    cal = cal.copy()
    cal[code_col] = cal[code_col].astype(str).str.extract(r"(\d{6})", expand=False)
    cal = cal.dropna(subset=[code_col])

    pool_codes = {str(c["symbol"]).zfill(6) for c in cands}
    sub = cal[cal[code_col].isin(pool_codes)]

    for _, row in sub.iterrows():
        sym = str(row[code_col]).zfill(6)
        d, disclosed = _pick_disclosure_date(row)
        if d is None:
            continue
        # 同一只股票可能有多行(不同报告期/变更), 取最早的那个
        if sym in matched and matched[sym]["date"] <= d:
            continue
        soon = (not disclosed) and (today <= d <= win_to)
        matched[sym] = {"date": d, "soon": soon, "disclosed": disclosed}
        if soon:
            soon_list.append(sym)
        if not disclosed and (nearest is None or d < nearest):
            nearest = d

    stats = {
        "from": str(today),
        "to": str(win_to),
        "matched": len(matched),
        "soon": len(soon_list),
        "soon_symbols": sorted(soon_list),
        # 池内最近的未披露财报日 —— 即使窗口内命中 0 只, 这个值也能说明"只是时点问题"
        "nearest": str(nearest) if nearest else None,
        "nearest_days": (nearest - today).days if nearest else None,
    }
    return matched, stats


# --------------------------------------------------------------------------- #
# 提示词构造
# --------------------------------------------------------------------------- #
SYSTEM_PROMPT = """你是一位严谨的A股宏观策略分析师, 服务于投资委员会。

你会收到两类材料:
  (一) 一组【宏观/行业新闻】(带编号, 来自财新网);
  (二) 一份【候选股票清单】(含 20 日动量, 部分股票带有即将披露财报的标记)。

你的任务: 结合宏观环境与财报日历, 对每只候选股给出过滤结论。

【绝对禁止】
1. 只依据我给你的新闻与财报日期作判断, 严禁引入任何外部信息、常识补充或你的记忆。
   如果你觉得某只股票的行业"应该"受某政策影响, 但没有新闻依据, 就不要写。
2. 严禁编造新闻编号、标题或日期。

【判断规则】
1. 宏观契合度: 若候选股所处行业/概念与国家政策方向或宏观趋势契合 -> favored;
   若明显相悖 -> cautioned; 看不出来 -> neutral。
   不一定每只股票都能在新闻里找到对应, 找不到就如实标 neutral, 不要硬凑。
2. 财报雷区: 若某股票标有 [⚠️即将财报, 波动率放大], 你必须给出"极度谨慎/建议观望",
   即 decision 必须为 cautioned, 并在 reason 里写明财报日期。
3. 信息真空期(重要): 若某股票【既没有新闻依据, 也没有近期财报数据】,
   这属于"信息真空期", 不是利好也不是利空, decision 必须是 neutral,
   reason 要说明"信息真空期, 维持原有基本面/技术面权重"。
   绝对不可以把"没有利空"当成"利好"。
4. 每只候选股都必须出现在结果里, 一只都不能少(包括无数据的)。

【输出格式】
只输出一个 JSON 对象, 不要任何解释文字、不要 markdown 代码块围栏:
{
  "summary": "一段不超过150字的宏观环境概述",
  "results": [
    {
      "symbol": "600418",
      "decision": "favored | neutral | cautioned",
      "confidence": 0.0,
      "reason": "不超过80字, 说明判断依据",
      "cited_evidence": ["[5] 化工周期…", "财报日 2026-10-23"],
      "has_news": true
    }
  ]
}
其中 confidence 取 0~1 的小数, cited_evidence 可为空数组(信息真空期时)。"""


def build_user_prompt(news: list[dict], cands: list[dict],
                      earnings: dict[str, dict], stats: dict,
                      full_text: dict[str, str] | None = None) -> str:
    """把新闻 + 候选股 + 财报标记拼成一次调用的用户提示词。"""
    lines: list[str] = []

    lines.append(f"【一、宏观/行业新闻】共 {len(news)} 条")
    for i, n in enumerate(news, 1):
        tag = f"<{n['tag']}>" if n.get("tag") else ""
        body = n["summary"]
        if full_text and n.get("url") in full_text and full_text[n["url"]]:
            body = full_text[n["url"]]
        lines.append(f"[{i}] {tag} {body}")

    lines.append("")
    lines.append("【二、候选股票清单】")
    today = now_bj().date()
    for i, c in enumerate(cands, 1):
        sym = str(c["symbol"]).zfill(6)
        mom = c.get("momentum_pct")
        mom_s = f"{mom:+.2f}%" if isinstance(mom, (int, float)) else "N/A"
        line = f"{i}. {sym} {c.get('name') or sym}  20日动量 {mom_s}"

        e = earnings.get(sym)
        if e and e.get("soon"):
            d = e["date"]
            line += (f"   [⚠️即将财报, 波动率放大] 预计披露日 {d}"
                     f" (距今 {(d - today).days} 天)")
        elif e and e.get("date"):
            line += f"   [近期已披露/待披露 {e['date']}]"
        else:
            line += "   [无近期财报数据]"
        lines.append(line)

    lines.append("")
    lines.append("【三、本次财报窗口】")
    lines.append(f"窗口 = {stats['from']} ~ {stats['to']} "
                 f"(未来 { (pd.Timestamp(stats['to']) - pd.Timestamp(stats['from'])).days } 天)")
    lines.append(f"窗口内即将披露财报的候选股数量 = {stats['soon']}")
    if stats.get("nearest"):
        lines.append(f"注意: 池内最近的一个未披露财报日是 {stats['nearest']}"
                     f"(距今 {stats['nearest_days']} 天), 若不在窗口内则本次无财报预警属正常。")
    lines.append("")
    lines.append("请按系统提示的规则, 对上述每一只候选股给出 decision, "
                 "并严格只输出那个 JSON 对象。")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 大模型调用与结果解析
# --------------------------------------------------------------------------- #
def call_llm(system_prompt: str, user_prompt: str) -> str:
    """调用大模型(复用 quant_agent 的配置读取, 不重复造 Key 逻辑)。"""
    try:
        from quant_agent import build_llm
    except ImportError as exc:
        raise RuntimeError(
            "无法导入 quant_agent 里的 build_llm, 请确认项目根目录下有 quant_agent.py"
        ) from exc

    llm = build_llm(temperature=0.2)
    resp = llm.invoke([("system", system_prompt), ("human", user_prompt)])
    content = getattr(resp, "content", "")
    return content if isinstance(content, str) else str(content)


def parse_llm_json(text: str) -> dict:
    """从模型输出里解析 JSON。兼容被 markdown 代码块包裹的情况。"""
    raw = (text or "").strip()
    # 去掉 ```json ... ``` 围栏
    fence = re.search(r"```(?:json)?\s*(.*?)```", raw, flags=re.S)
    if fence:
        raw = fence.group(1).strip()
    # 退而求其次: 截取第一个 { 到最后一个 }
    if not raw.startswith("{"):
        i, j = raw.find("{"), raw.rfind("}")
        if i >= 0 and j > i:
            raw = raw[i:j + 1]
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"模型输出不是合法 JSON: {exc}\n"
            f"       原始输出前 300 字: {text[:300]!r}"
        ) from exc


def enforce_vacuum_neutral(results: list[dict], earnings: dict[str, dict]
                           ) -> tuple[list[dict], list[str]]:
    """代码层强制校验(信息真空期的第二道保险)。

    规则: 若某股票【既无新闻依据(has_news=False 且 cited_evidence 为空)】
          又【无近期财报数据】, 而模型却给出了非中性结论, 则强制改写为 neutral。

    为什么要这层: 提示词可能被模型忽略或误解。这是实盘系统的安全底线 ——
    "没有数据"绝不能被当成买入理由。
    返回 (改写后的结果, 改写记录)。
    """
    rewritten: list[str] = []
    for r in results:
        sym = str(r.get("symbol", "")).zfill(6)
        decision = str(r.get("decision", "")).strip().lower()
        has_news = bool(r.get("has_news"))
        evidence = r.get("cited_evidence") or []
        has_earnings = sym in earnings and earnings[sym].get("date") is not None

        # 认定"信息真空": 无新闻依据 且 无财报数据
        no_news_signal = (not has_news) and (not evidence)
        if no_news_signal and not has_earnings and decision in {DECISION_FAVORED,
                                                                DECISION_CAUTIONED}:
            old = decision
            r["decision"] = DECISION_NEUTRAL
            r["confidence"] = 0.0
            reason = str(r.get("reason", "")).strip()
            r["reason"] = (f"[代码层强制改写: 信息真空期必须中性] "
                           f"原结论={old}; 原理由={reason[:60]}")
            rewritten.append(f"{sym}: {old} -> neutral")
    return results, rewritten


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def load_candidates() -> list[dict]:
    if not CANDIDATES_FILE.exists():
        raise FileNotFoundError(
            f"找不到 {CANDIDATES_FILE.name}。\n"
            f"       请先运行第一层海选: python src/market_screener.py"
        )
    payload = json.loads(CANDIDATES_FILE.read_text(encoding="utf-8"))
    cands = payload.get("candidates") or []
    if not cands:
        raise ValueError(f"{CANDIDATES_FILE.name} 里没有任何候选股, "
                         f"请重新运行 src/market_screener.py")
    return cands


def build_target_payload(cands: list[dict], results: list[dict],
                         earnings: dict[str, dict], stats: dict,
                         params: dict, macro_summary: str) -> dict:
    """组装 target_pool.json。

    JSON 结构保持与原占位实现一致(前端 app.py 零改动):
      - placeholder 改为 false
      - source 改为 "宏观新闻+财报预警分析"
      - pool 里保留原有 symbol/name/momentum_pct/amount 字段, 另加分析字段
    """
    by_sym = {str(r.get("symbol", "")).zfill(6): r for r in results}
    pool: list[dict] = []
    for c in cands:
        sym = str(c["symbol"]).zfill(6)
        r = by_sym.get(sym, {})
        e = earnings.get(sym) or {}
        decision = str(r.get("decision", DECISION_NEUTRAL)).strip().lower()
        if decision not in VALID_DECISIONS:
            decision = DECISION_NEUTRAL
        pool.append({
            # ---- 原有字段(前端下拉框依赖) ----
            "symbol": sym,
            "name": c.get("name"),
            "momentum_pct": c.get("momentum_pct"),
            "amount": c.get("amount"),
            # ---- 本层新增的分析字段 ----
            "decision": decision,
            "confidence": r.get("confidence"),
            "reason": r.get("reason", ""),
            "cited_evidence": r.get("cited_evidence") or [],
            "has_news": bool(r.get("has_news")),
            "earnings_date": str(e["date"]) if e.get("date") else None,
            "earnings_soon": bool(e.get("soon")),
            # information_vacuum 的严格定义: 既无新闻依据、也完全查不到财报记录。
            # 注意与"财报日在窗口外"区分开 —— 后者是"有数据但不在雷区", 不是信息真空。
            "information_vacuum": (not r.get("has_news")) and (not e.get("date")),
            "earnings_out_of_window": bool(e.get("date")) and not bool(e.get("soon")),
        })
    return {
        "generated_at": now_bj().strftime("%Y-%m-%d %H:%M:%S"),
        "timezone": TIMEZONE_NAME,
        "placeholder": False,
        "source": "宏观新闻+财报预警分析",
        "params": params,
        "earnings_window": stats,
        "macro_summary": macro_summary,
        "pool": pool,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="第二层: 宏观新闻 + 财报预警 过滤",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--top-n", type=int, default=DEFAULT_TOP_N,
                   help="目标池保留几只")
    p.add_argument("--earnings-window", type=int, default=DEFAULT_EARNINGS_WINDOW,
                   help="未来 N 个自然日内披露财报则打风险标签")
    p.add_argument("--news-limit", type=int, default=100,
                   help="喂给模型的宏观新闻条数上限")
    p.add_argument("--fetch-full-text", action="store_true",
                   help="抓新闻正文而非摘要(慢: 每篇一次请求, 默认关闭)")
    p.add_argument("--report-period", default=None,
                   help="财报报告期 YYYYMMDD(默认按当前北京时间自动推算)")
    p.add_argument("--dry-run", action="store_true",
                   help="只抓数据并拼提示词, 不调用大模型、不写文件")
    p.add_argument("--check", action="store_true",
                   help="只列出将选出的股票, 不调用模型、不写文件")
    p.add_argument("--quiet", action="store_true", help="只输出 INFO 及以上")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    setup_logging(verbose=not args.quiet)

    if args.top_n < 1 or args.earnings_window < 0:
        log.error("--top-n 必须 >= 1 且 --earnings-window 必须 >= 0")
        return 2

    log.info("=" * 76)
    log.info("第二层: 宏观新闻 + 财报预警 | 当前北京时间 %s",
             now_bj().strftime("%Y-%m-%d %H:%M:%S"))
    log.info("参数: top_n=%d, 财报窗口=%d 天, 新闻上限=%d 条, 抓正文=%s",
             args.top_n, args.earnings_window, args.news_limit, args.fetch_full_text)
    log.info("=" * 76)

    # ---- 1. 候选股 ----
    try:
        all_cands = load_candidates()
    except (FileNotFoundError, ValueError) as exc:
        log.error("%s", exc)
        return 1
    cands = all_cands[:args.top_n]
    log.info("读入候选股 %d 只, 本次取前 %d 只: %s",
             len(all_cands), len(cands), ", ".join(str(c["symbol"]) for c in cands))

    # ---- 2. 宏观新闻 ----
    try:
        news = fetch_macro_news(args.news_limit)
        log.info("宏观新闻 OK: %d 条 (字段 tag/summary/url)", len(news))
    except Exception as exc:
        log.error("宏观新闻获取失败: %s: %s", type(exc).__name__, exc)
        return 1

    full_text: dict[str, str] | None = None
    if args.fetch_full_text:
        log.info("正在抓取 %d 篇新闻正文(每篇截断 %d 字)...", len(news), FULL_TEXT_LIMIT)
        full_text = {}
        for i, n in enumerate(news, 1):
            if n.get("url"):
                full_text[n["url"]] = fetch_article_text(n["url"])
            if i % 20 == 0:
                log.info("  正文进度 %d/%d", i, len(news))
            time.sleep(0.3)
        got = sum(1 for v in full_text.values() if v)
        log.info("正文抓取完成: %d/%d 篇成功", got, len(news))

    # ---- 3. 财报日历 ----
    report_period = args.report_period or current_report_period()
    log.info("读取财报日历: 报告期 %s (stock_yysj_em)", report_period)
    cal = fetch_earnings_calendar(report_period)
    log.info("财报日历: %s 行", len(cal) if cal is not None else 0)

    earnings, stats = match_earnings(cands, cal, args.earnings_window)
    log.info("财报匹配: 池内 %d/%d 只有财报记录; 窗口 %s~%s 内即将披露 %d 只",
             stats["matched"], len(cands), stats["from"], stats["to"], stats["soon"])
    if stats.get("nearest"):
        log.info("  池内最近未披露财报日 = %s (距今 %d 天)",
                 stats["nearest"], stats["nearest_days"])
    if stats["soon"] == 0:
        log.info("  [提示] 窗口内命中 0 只属正常时点现象(如财报季尚未开始), "
                 "不是接口故障。可用 --earnings-window 放宽窗口观察效果。")
    if stats.get("soon_symbols"):
        log.info("  即将财报(风险标记): %s", ", ".join(stats["soon_symbols"]))

    # ---- 4. 明细: 信息真空期的股票 ----
    vacuum = [str(c["symbol"]).zfill(6) for c in cands
              if str(c["symbol"]).zfill(6) not in earnings]
    if vacuum:
        log.info("无近期财报数据的候选股(将被标注为信息真空期, 中性处理): %s",
                 ", ".join(vacuum))

    # ---- 5. --check 早退 ----
    if args.check:
        print()
        print(f"--check: 将选出 {len(cands)} 只(不调模型、不写文件)")
        for i, c in enumerate(cands, 1):
            sym = str(c["symbol"]).zfill(6)
            e = earnings.get(sym) or {}
            flag = " [⚠️即将财报]" if e.get("soon") else ""
            print(f"  {i:>3}. {sym} {c.get('name')}  动量 {c.get('momentum_pct')}%{flag}")
        return 0

    # ---- 6. 拼提示词 ----
    user_prompt = build_user_prompt(news, cands, earnings, stats, full_text)
    log.info("提示词长度: %d 字 (约 %d tokens)", len(user_prompt), len(user_prompt) // 2)

    if args.dry_run:
        print()
        print("=" * 76)
        print("--dry-run: 不调用大模型。提示词预览(前 1800 字):")
        print("=" * 76)
        print(user_prompt[:1800])
        print("...")
        print("=" * 76)
        print(f"完整提示词 {len(user_prompt)} 字; 将调用模型 1 次(合并调用, 非每只一次)")
        return 0

    # ---- 7. 一次调用大模型 ----
    try:
        raw = call_llm(SYSTEM_PROMPT, user_prompt)
    except RuntimeError as exc:
        log.error("%s", exc)
        return 1
    except Exception as exc:
        log.error("大模型调用失败: %s: %s", type(exc).__name__, exc)
        return 1

    try:
        parsed = parse_llm_json(raw)
    except RuntimeError as exc:
        log.error("%s", exc)
        return 1

    results = parsed.get("results") or []
    macro_summary = str(parsed.get("summary", "") or "")
    if not results:
        log.error("模型返回的 results 为空, 拒绝写入 JSON。原始输出前 300 字: %r",
                  raw[:300])
        return 1
    log.info("模型返回 %d 只股票的结论; 宏观概述 %d 字",
             len(results), len(macro_summary))

    # ---- 8. 代码层强制校验(信息真空期) ----
    results, rewritten = enforce_vacuum_neutral(results, earnings)
    if rewritten:
        log.warning("代码层强制改写 %d 条(模型把信息真空期判成了非中性):", len(rewritten))
        for w in rewritten:
            log.warning("  %s", w)
    else:
        log.info("代码层校验通过: 没有'信息真空期却非中性'的结论")

    # ---- 9. 写文件 ----
    params = {
        "top_n": args.top_n,
        "earnings_window_days": args.earnings_window,
        "macro_news_count": len(news),
        "report_period": report_period,
        "full_text": bool(args.fetch_full_text),
    }
    payload = build_target_payload(cands, results, earnings, stats, params,
                                   macro_summary)

    TARGET_POOL_FILE.parent.mkdir(parents=True, exist_ok=True)
    TARGET_POOL_FILE.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("已保存 %d 只目标股 -> %s", len(payload["pool"]), TARGET_POOL_FILE)

    # ---- 10. 汇总 ----
    print()
    print("=" * 76)
    print(f"目标池已生成: {len(payload['pool'])} 只  |  财报窗口 {stats['from']} ~ {stats['to']}")
    print("=" * 76)
    if macro_summary:
        print(f"宏观概述: {macro_summary[:150]}")
        print("-" * 76)
    cnt = {"favored": 0, "neutral": 0, "cautioned": 0}
    for p in payload["pool"]:
        cnt[p["decision"]] = cnt.get(p["decision"], 0) + 1
    print(f"结论分布: favored(利好) {cnt['favored']} | neutral(中性) {cnt['neutral']} "
          f"| cautioned(谨慎) {cnt['cautioned']}")
    print("-" * 76)
    for i, p in enumerate(payload["pool"], 1):
        mark = {"favored": "🟢", "neutral": "⚪", "cautioned": "🔴"}.get(p["decision"], "?")
        tag = " [⚠️即将财报]" if p["earnings_soon"] else ""
        vac = " [信息真空期]" if p["information_vacuum"] else ""
        print(f"  {i:>3}. {mark} {p['symbol']} {p['name']:<10} "
              f"{p['decision']:<9}{tag}{vac}")
        if p["reason"]:
            print(f"        理由: {p['reason'][:80]}")
    print("=" * 76)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
