#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
app.py — 投资委员会前端 (Streamlit)

把 quant_agent.py 里的 LangGraph 多智能体系统套一个网页界面:
  左栏: 基本面分析师 / 技术面分析师 / 风险管理员 三份独立报告
  右栏: 投资组合经理的最终决策
  下方: Agent 工作流程(用 st.status 实时展示)与消息记录

运行
----
    streamlit run app.py

然后浏览器打开提示的地址(通常是 http://localhost:8501)。

注意
----
* 本工具只做【决策辅助】, 不接任何真实交易接口, 不会下单。
* 需要在 .env 或环境变量里配置 DEEPSEEK_API_KEY(详见 quant_agent.py 的说明)。
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import streamlit as st

# --------------------------------------------------------------------------- #
# 路径引导: 保证无论从哪里启动 streamlit, 都能 import 到同目录的 quant_agent
# --------------------------------------------------------------------------- #
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import quant_agent as qa  # noqa: E402

# 页面一加载就把 .env 读进 os.environ。
# 必要性: 下面侧边栏要检查有没有 API Key, 而 .env 里的变量只有被 load_dotenv()
# 加载后才会出现在 os.environ 里; 不先加载的话, 只看 .env 配 Key 的用户
# 会被误报"未检测到 API Key"。
qa.load_env_file()

from config import CONFIG_DATA_DIR  # noqa: E402

# --------------------------------------------------------------------------- #
# 选股流水线涉及的路径与脚本
# --------------------------------------------------------------------------- #
CANDIDATES_FILE = CONFIG_DATA_DIR / "candidates.json"    # 第一层产出
TARGET_POOL_FILE = CONFIG_DATA_DIR / "target_pool.json"  # 第二层产出
SCREENER_SCRIPT = _HERE / "src" / "market_screener.py"
NEWS_SCRIPT = _HERE / "src" / "news_agent.py"
DATA_CENTER_SCRIPT = _HERE / "src" / "data_center.py"    # 用于自动拉取新标的
PYTHON_EXE = sys.executable          # 与当前 Streamlit 同一个解释器, 保证依赖一致

# --------------------------------------------------------------------------- #
# 页面配置
# --------------------------------------------------------------------------- #
st.set_page_config(
    page_title="投资委员会 · 多智能体决策",
    page_icon="📊",
    layout="wide",
)

# 节点的中文名与显示顺序(与 quant_agent 里的节点名对应)
NODE_LABELS = {
    "fundamental_analyst": "基本面分析师",
    "technical_analyst": "技术面分析师",
    "risk_manager": "风险管理员",
    "portfolio_manager": "投资组合经理",
}
# 三个并行分析师(用于工作流程展示的顺序)
PARALLEL_NODES = ["fundamental_analyst", "technical_analyst", "risk_manager"]


def _read_json(path: Path) -> dict | None:
    """安全读取 JSON, 失败返回 None(UI 不该因为一个坏文件直接崩)。"""
    try:
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def load_stock_pool() -> tuple[list[dict], str, bool]:
    """决定下拉框的可选标的。

    优先级:
        1. config/target_pool.json  —— 第二层(新闻过滤)的产出, 正常路径
        2. config/candidates.json   —— 第一层产出, 取前 10 只作为临时备用
        3. 都没有                     —— 返回空列表, 由调用方回退到手动输入
    返回 (标的列表, 来源说明, 是否为临时备用)。
    列表元素形如 {"symbol": "600519", "name": "贵州茅台"}。
    """
    payload = _read_json(TARGET_POOL_FILE)
    if payload and payload.get("pool"):
        pool = [{"symbol": str(x.get("symbol")), "name": x.get("name") or x.get("symbol")}
                for x in payload["pool"] if x.get("symbol")]
        if pool:
            tag = "占位逻辑" if payload.get("placeholder") else "已过滤"
            return pool, f"target_pool.json（{tag}，{len(pool)} 只）", False

    payload = _read_json(CANDIDATES_FILE)
    if payload and payload.get("candidates"):
        pool = [{"symbol": str(x.get("symbol")), "name": x.get("name") or x.get("symbol")}
                for x in payload["candidates"][:10] if x.get("symbol")]
        if pool:
            return pool, f"candidates.json 前 {len(pool)} 只（临时备用）", True

    return [], "无可用的选股池文件", True


def run_pipeline_script(script: Path, extra_args: list[str]) -> tuple[bool, str]:
    """在子进程里跑一个流水线脚本, 返回 (是否成功, 输出文本)。

    两个刻意的工程选择(与项目其它脚本保持一致):
      1. 不用 subprocess 的管道(PIPE): 某些受限环境禁止创建匿名管道,
         会直接抛 PermissionError: [WinError 5]。改为重定向到文件再读回。
      2. 临时文件放在【项目内 .tmp/】而不是系统 %TEMP%: 受限环境下系统
         临时目录可能不可写。
    """
    if not script.exists():
        return False, f"脚本不存在: {script}"

    tmp_dir = _HERE / ".tmp"
    try:
        tmp_dir.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        return False, f"无法创建临时目录 {tmp_dir}: {exc}"

    out_path = tmp_dir / f"{script.stem}_stdout.txt"
    err_path = tmp_dir / f"{script.stem}_stderr.txt"

    # 强制子进程用 UTF-8 输出, 否则 Windows 中文环境下会按 GBK 输出造成乱码,
    # 而父进程按 UTF-8 读取时会得到一堆无法解析的字符。
    env = qa.os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"

    try:
        with open(out_path, "w+", encoding="utf-8", errors="replace") as f_out, \
                open(err_path, "w+", encoding="utf-8", errors="replace") as f_err:
            proc = subprocess.run(
                [PYTHON_EXE, str(script), *extra_args],
                cwd=str(_HERE), env=env, stdout=f_out, stderr=f_err, text=True,
            )
        output = out_path.read_text(encoding="utf-8-sig", errors="replace")
        output += err_path.read_text(encoding="utf-8-sig", errors="replace")
        return proc.returncode == 0, output
    except Exception as exc:
        return False, f"执行 {script.name} 失败: {type(exc).__name__}: {exc}"
    finally:
        for p in (out_path, err_path):
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass


def symbol_has_data(symbol: str) -> tuple[bool, int, str | None]:
    """查库判断某标的是否已有可用行情。

    返回 (是否充足, 行数, 最新交易日字符串)。

    "充足"的判定: 行数 > 0 且最新交易日落在 HISTORY_YEARS 窗口内。
    为什么要检查日期而不只看行数: 若某标的数据被滚动清理过、只剩很旧的几行,
    深度分析依然会取不到近期数据, 那种情况应当触发自动拉取。
    """
    import sqlite3
    try:
        from config import HISTORY_YEARS, now_bj
        cutoff = (now_bj() - pd_date_offset_years(HISTORY_YEARS)).strftime("%Y-%m-%d")
    except Exception:
        cutoff = "1900-01-01"

    try:
        conn = sqlite3.connect(str(qa.DB_FILE))
        try:
            row = conn.execute(
                "SELECT COUNT(*), MAX(trade_date) FROM daily_price WHERE symbol = ?",
                (symbol,)).fetchone()
        finally:
            conn.close()
    except Exception:
        # 查库失败不该阻塞审议 —— 交给下游工具去报错, 这里当作"有数据"放行
        return True, -1, None

    n = int(row[0] or 0)
    latest = row[1]
    if n == 0 or not latest:
        return False, 0, None
    return (str(latest) >= cutoff), n, str(latest)


def pd_date_offset_years(years: int):
    """返回一个可用于 now_bj() 相减的 DateOffset(避免在 app.py 顶层 import pandas)。"""
    from pandas import DateOffset
    return DateOffset(years=years)


def ensure_symbol_data(symbol: str) -> bool:
    """确保该标的在库里有可用行情; 没有就自动拉取。

    返回 True 表示可以继续审议, False 表示拉取失败(已给出友好提示)。

    设计说明:
      - 本函数是【纯前置守卫】, 成功时调用方走的是原有的 build_graph -> graph.stream
        全链路, 一个字都不改; 失败时只提示并跳过本轮, 不污染 session_state.state。
      - 用 session_state.verified_symbols 做会话内缓存, 同一标的只查一次库,
        避免每次点击都白跑 SQL。
      - 拉取是同步阻塞的(实测单只约 17 秒), 完成后数据已入库, 因此可以【无缝续跑】,
        不需要用户手动再点一次。
    """
    # ---- 会话内缓存 ----
    verified = st.session_state.setdefault("verified_symbols", set())
    if symbol in verified:
        return True

    ok, n, latest = symbol_has_data(symbol)
    if ok:
        verified.add(symbol)
        return True

    # ---- 数据不足: 自动拉取 ----
    st.info(f"检测到新标的 **{symbol}**，正在后台拉取最近 5 年数据…"
            f"（实测单只约 20~40 秒，请稍候）")

    if not DATA_CENTER_SCRIPT.exists():
        st.error(f"自动拉取失败：找不到脚本 {DATA_CENTER_SCRIPT}")
        return False

    with st.spinner(f"正在拉取 {symbol} 的历史行情与基本面…"):
        success, output = run_pipeline_script(DATA_CENTER_SCRIPT, ["--symbol", symbol])

    # 拉取后再查一次库 —— 以数据库实际内容为准, 而不是只看子进程退出码
    ok2, n2, latest2 = symbol_has_data(symbol)

    if ok2:
        st.success(f"✅ 已自动拉取 **{symbol}** 共 **{n2}** 行行情"
                   f"（最新交易日 {latest2}）。现在继续召开投资委员会。")
        verified.add(symbol)
        return True

    st.error(f"❌ 自动拉取 **{symbol}** 未成功（脚本退出码 {'0' if success else '非0'}），"
             f"库中仍无可用数据。")
    with st.expander("查看脚本输出（便于定位原因）", expanded=True):
        st.code(output[-2000:] if output else "(无输出)", language="text")
    st.info(f"也可以在终端手动排查：`python src/data_center.py --symbol {symbol}`\n\n"
            f"常见原因：代码不存在 / 已退市 / 数据源临时限流。")
    return False


def render_message(msg) -> None:
    """把一条 LangChain 消息渲染成可读文本。"""
    cls = type(msg).__name__
    content = getattr(msg, "content", "")
    if not isinstance(content, str):
        content = str(content)

    if cls == "HumanMessage":
        st.markdown(f"**👤 任务**：{content}")
    elif cls == "SystemMessage":
        st.markdown(f"**⚙️ 流程**：{content}")
    elif cls == "ToolMessage":
        # 工具返回的是原始 dict 字符串, 用代码块展示, 避免 markdown 把花括号吃掉
        preview = content if len(content) <= 600 else content[:600] + " ...(截断)"
        st.markdown(f"**🔧 工具返回**（{getattr(msg, 'tool_call_id', '')}）")
        st.code(preview, language="json")
    elif cls == "AIMessage":
        # 模型要求调用工具时 content 可能为空, 这时展示它想调什么
        calls = getattr(msg, "tool_calls", None) or []
        if calls:
            names = ", ".join(c.get("name", "?") for c in calls)
            st.markdown(f"**🤖 分析师**：调用工具 `{names}`")
        if content.strip():
            st.markdown(f"**🤖 分析师**：{content}")
    else:
        st.markdown(f"**{cls}**：{content}")


def main() -> None:
    st.title("📊 投资委员会 · 多智能体决策系统")
    st.caption(
        "LangGraph 编排 · 基本面 / 技术面 / 风险 三路并行分析 → 投资组合经理汇总。"
        "本工具仅做决策辅助，不接交易接口，不构成投资建议。"
    )

    # ---------------- 侧边栏: 输入与配置 ----------------
    with st.sidebar:
        # ===== 选股流水线(新增) =====
        st.header("🧭 选股流水线")

        # 初始化状态(防重复点击/跨 rerun 记忆结果都靠它)
        for key, default in (("screening_phase", None), ("screen_msg", None),
                             ("news_msg", None), ("pool_source", None),
                             ("last_scan_output", None)):
            st.session_state.setdefault(key, default)

        busy = st.session_state.screening_phase is not None

        col_a, col_b = st.columns(2)
        with col_a:
            btn_screen = st.button("🔍 全市场海选", use_container_width=True,
                                   disabled=busy,
                                   help="调用 market_screener.py：全市场 → 成交额前300 → 动量前100")
        with col_b:
            btn_news = st.button("📰 宏观新闻筛选", use_container_width=True,
                                 disabled=busy,
                                 help="调用 news_agent.py：候选股 → 目标池（当前为占位逻辑）")

        # ---- 按钮A: 全市场海选 ----
        if btn_screen:
            st.session_state.screening_phase = "screening"
            with st.status("正在扫描全市场…（约 20 秒~20 分钟，取决于限速）",
                           expanded=True) as status:
                st.write("① 拉取全市场快照 → ② 剔除 ST/次新/停牌 → "
                         "③ 按成交额排序 → ④ 逐只计算 20 日动量")
                ok, output = run_pipeline_script(SCREENER_SCRIPT, ["--quiet"])
                st.session_state.last_scan_output = output[-4000:]

                data = _read_json(CANDIDATES_FILE)
                if ok and data and data.get("candidates"):
                    n = len(data["candidates"])
                    status.update(label=f"✅ 海选完成：找到 {n} 只候选股", state="complete")
                    st.session_state.screen_msg = (
                        f"找到 {n} 只候选股（成交额 Top"
                        f"{data.get('params', {}).get('amount_top_n')} → 动量 Top"
                        f"{data.get('params', {}).get('momentum_top_n')}）")
                else:
                    status.update(label="❌ 海选失败", state="error")
                    st.session_state.screen_msg = None
                    st.error("海选未成功，下面是脚本输出的末尾，便于定位原因：")
                    st.code(output[-1500:] or "(无输出)", language="text")
            st.session_state.screening_phase = None

        if st.session_state.screen_msg:
            st.success(st.session_state.screen_msg)

        # ---- 按钮B: 宏观新闻筛选 ----
        if btn_news:
            if not CANDIDATES_FILE.exists():
                st.warning("还没有候选股文件，请先点上面的【全市场海选】。")
            else:
                st.session_state.screening_phase = "news"
                with st.status("正在进行宏观/新闻筛选…", expanded=True) as status:
                    ok, output = run_pipeline_script(NEWS_SCRIPT, [])
                    data = _read_json(TARGET_POOL_FILE)
                    if ok and data and data.get("pool"):
                        n = len(data["pool"])
                        status.update(label=f"✅ 已聚焦到 {n} 只目标股",
                                      state="complete")
                        st.session_state.news_msg = f"筛选出 {n} 只目标股"
                    else:
                        status.update(label="❌ 新闻筛选失败", state="error")
                        st.session_state.news_msg = None
                        st.error("新闻筛选未成功，脚本输出末尾：")
                        st.code(output[-1500:] or "(无输出)", language="text")
                st.session_state.screening_phase = None

        if st.session_state.news_msg:
            st.info(st.session_state.news_msg)
            # 占位实现的诚实提示: 不能让人以为真的做过新闻研判
            _tp = _read_json(TARGET_POOL_FILE)
            if _tp and _tp.get("placeholder"):
                st.warning("⚠️ 当前为占位逻辑，尚未接入真实新闻过滤 —— "
                           "目标池只是候选股的前 N 只，未做任何政策/新闻研判。")

        # ---- 当前选股池信息 ----
        pool, pool_source, is_fallback = load_stock_pool()
        with st.expander("📋 当前选股池", expanded=False):
            st.caption(f"来源：{pool_source}")
            if not pool:
                st.warning("没有可用的选股池文件。请先点【全市场海选】，"
                           "或用下面的手动输入。")
            else:
                if is_fallback:
                    st.warning("正在使用临时备用池（第一层候选股前 10 只），"
                               "建议再点一次【宏观新闻筛选】生成正式目标池。")
                for i, item in enumerate(pool, 1):
                    st.markdown(f"{i}. `{item['symbol']}` {item['name']}")

        st.divider()

        # ===== 原有参数设置(保持不变) =====
        st.header("⚙️ 参数设置")

        # 股票代码: 由输入框改为下拉框(数据来自 target_pool.json)。
        # 保留一个手动输入通道, 否则选股池文件缺失时整个页面就没法用了。
        if pool:
            options = [p["symbol"] for p in pool]
            labels = {p["symbol"]: f"{p['symbol']} {p['name']}" for p in pool}
            default_idx = (options.index(qa.DEFAULT_SYMBOL)
                           if qa.DEFAULT_SYMBOL in options else 0)
            symbol = st.selectbox(
                "股票代码", options=options, index=default_idx,
                format_func=lambda s: labels.get(s, s),
                help="来自 config/target_pool.json（或临时备用池）",
            )
        else:
            symbol = st.text_input("股票代码", value=qa.DEFAULT_SYMBOL,
                                   help="6 位代码，例如 600519（贵州茅台）")

        with st.expander("✏️ 手动输入其它代码", expanded=False):
            manual = st.text_input("手动覆盖股票代码", value="",
                                   help="填了就优先用它，忽略上面的下拉框")
            if manual.strip():
                symbol = manual.strip()

        years = st.selectbox(
            "回测年限",
            options=[1, 3, 5],
            index=0,
            format_func=lambda y: f"{y} 年",
            help="组合经理会用这个年限去回测该标的的历史最大回撤/夏普/总收益",
        )
        temperature = st.slider("模型温度", 0.0, 1.0, 0.2, 0.1,
                                help="越低越稳定保守，建议 0.0~0.3")
        show_messages = st.checkbox("展示完整消息流（含工具原始返回）", value=False)

        st.divider()
        st.subheader("数据覆盖检查")
        # 这张表回答一个实际问题: 选股池里的标的, 本地数据库里有行情吗?
        # 没有的话深度分析会取不到数, 需要先跑 data_center.py。
        try:
            import sqlite3
            conn = sqlite3.connect(str(qa.DB_FILE))
            have = {r[0] for r in conn.execute(
                "SELECT DISTINCT symbol FROM daily_price").fetchall()}
            conn.close()
            if pool:
                missing = [p["symbol"] for p in pool if p["symbol"] not in have]
                st.markdown(f"- 选股池 {len(pool)} 只，库内有行情 "
                            f"**{len(pool) - len(missing)}** 只")
                if missing:
                    st.info(f"以下 {len(missing)} 只标的库里暂无行情："
                            f"{', '.join(missing[:8])}"
                            f"{' 等' if len(missing) > 8 else ''}\n\n"
                            "**无需手动建库**：在下方选中它并点击【召开投资委员会】时，"
                            "会自动拉取最近 5 年数据后再开始分析。")
                else:
                    st.success("选股池全部标的都有本地行情数据 ✅")
            else:
                st.markdown(f"- 库内共有 {len(have)} 个标的的行情")
        except Exception as exc:
            st.warning(f"读取数据库失败：{exc}")

        st.divider()
        if not (qa.os.environ.get("DEEPSEEK_API_KEY") or qa.os.environ.get("OPENAI_API_KEY")):
            st.warning("未检测到 API Key。请参考 quant_agent.py 顶部说明配置 .env")

    # ---------------- 主区: 按钮 ----------------
    col_btn, col_hint = st.columns([1, 3])
    with col_btn:
        run = st.button("🏛️ 召开投资委员会", type="primary", use_container_width=True)
    with col_hint:
        st.caption(f"将对 **{symbol}** 召开委员会；三位分析师并行工作，"
                   f"组合经理再回测 **{years} 年**历史，约需 20~60 秒。")

    # 用 session_state 保存上一次结果, 这样点按钮之外的交互不会清空页面
    if "state" not in st.session_state:
        st.session_state.state = None
        st.session_state.ran_symbol = None
        st.session_state.ran_years = None

    if run:
        symbol = (symbol or "").strip()
        if not symbol:
            st.error("请先填写股票代码。")
        elif len(symbol) != 6 or not symbol.isdigit():
            st.error(f"股票代码格式不对：`{symbol}`。应为 6 位数字，例如 600519。")
        else:
            # ★ 第五步新增: 前置守卫 —— 库里没有该标的就自动拉取 5 年数据。
            # 成功则无缝续跑下面的原有审议流程。
            #
            # 这里刻意【不用 st.stop()】: 它会终止整个脚本渲染, 连下方"上一次审议结果"
            # 也会一起消失。改用局部标志位跳过本轮, 页面其它部分照常渲染。
            ready = ensure_symbol_data(symbol)
            graph = None
            if ready:
                try:
                    graph = qa.build_graph(qa.build_llm(temperature=temperature),
                                           backtest_years=years)
                except ImportError as exc:
                    st.error(f"依赖缺失：{exc}")
                    graph = None
                except RuntimeError as exc:
                    # 这里通常是缺少 API Key
                    st.error(f"初始化失败：{exc}")
                    graph = None
                except Exception as exc:
                    st.error(f"初始化失败：{type(exc).__name__}: {exc}")
                    graph = None

            if graph is not None:
                # ---- 用 st.status 展示 Agent 工作流程 ----
                final_state = dict(qa.new_state(symbol))
                finished: list[str] = []

                with st.status(f"委员会正在审议 {symbol} …", expanded=True) as status:
                    try:
                        # stream(..., stream_mode="updates") 会在【每个节点完成后】
                        # 吐出一批增量更新, 正好拿来实时刷新进度。
                        for chunk in graph.stream(qa.new_state(symbol),
                                                  stream_mode="updates"):
                            for node_name, update in chunk.items():
                                label = NODE_LABELS.get(node_name, node_name)
                                finished.append(label)
                                st.write(f"✅ {label} 已完成")
                                # 把增量合并进最终状态
                                if isinstance(update, dict):
                                    for k, v in update.items():
                                        if k == "messages":
                                            final_state.setdefault("messages", [])
                                            final_state["messages"] = list(
                                                final_state["messages"]) + list(v or [])
                                        else:
                                            final_state[k] = v
                        status.update(
                            label=f"✅ 委员会审议完毕：{symbol}", state="complete")
                    except Exception as exc:
                        status.update(label=f"❌ 审议失败：{type(exc).__name__}",
                                      state="error")
                        st.error(f"执行失败：{type(exc).__name__}: {exc}")
                        st.info("常见原因：API Key 无效、网络不通、或所选模型不支持工具调用。")

                st.session_state.state = final_state
                st.session_state.ran_symbol = symbol
                st.session_state.ran_years = years
                st.session_state.finished = finished

    # ---------------- 结果展示 ----------------
    state = st.session_state.state
    if not state:
        st.info("👈 在左侧填写股票代码，然后点击「召开投资委员会」。"
                "想先确认数据没问题，可以在终端跑：`python quant_agent.py --tools-only`")
        return

    st.divider()
    st.subheader(f"审议结果 · {st.session_state.ran_symbol}"
                 f"（回测 {st.session_state.ran_years or '?'} 年）")

    # ---- 两栏布局: 左=三位分析师, 右=组合经理 ----
    left, right = st.columns([1, 1], gap="large")

    with left:
        st.markdown("### 🔍 独立分析报告")
        with st.expander("① 基本面分析师", expanded=True):
            st.markdown(state.get("fundamental_analysis") or "_（无内容）_")
        with st.expander("② 技术面分析师", expanded=True):
            st.markdown(state.get("technical_analysis") or "_（无内容）_")
        with st.expander("③ 风险管理员", expanded=True):
            st.markdown(state.get("risk_analysis") or "_（无内容）_")
        # 组合经理调 run_backtest 拿到的原始数字, 单独展示一份。
        # 必要性: 否则用户只能从决策文字里猜"模型到底看到了什么回测数据"。
        with st.expander(f"④ 历史回测数据（{st.session_state.ran_years or '?'} 年 · "
                         f"买入并持有）", expanded=True):
            if state.get("backtest_analysis"):
                st.markdown(state["backtest_analysis"])
            else:
                st.caption("_（组合经理本次没有调用回测工具，或该标的无行情数据）_")

    with right:
        st.markdown("### 🏛️ 投资组合经理 · 最终决策")
        decision = state.get("final_decision") or ""
        if decision:
            # 决策内容较长, 放在一个高亮的容器里
            st.success(decision)
        else:
            st.warning("_（未产生最终决策）_")

        # 若前面有节点报错, 单独提示
        if state.get("error"):
            st.error(f"过程中出现错误：{state['error']}")

        st.caption("⚠️ 以上为决策辅助输出，不构成投资建议；请自行判断风险。")

    # ---------------- 工作流程与消息记录 ----------------
    st.divider()
    with st.expander("🧭 Agent 工作流程", expanded=False):
        finished = st.session_state.get("finished", [])
        if finished:
            st.markdown("**执行顺序**（三个分析师在同一轮并行，组合经理最后执行）：")
            for i, name in enumerate(finished, 1):
                st.markdown(f"{i}. {name}")
            st.markdown(
                "说明：LangGraph 会在同一 superstep 里并行跑三个分析师，"
                "等它们全部完成后才执行投资组合经理（barrier 同步）。"
            )
        else:
            st.write("暂无记录。")

    with st.expander("💬 消息记录", expanded=show_messages):
        msgs = state.get("messages") or []
        if not msgs:
            st.write("暂无消息。")
        else:
            if not show_messages:
                st.caption(f"共 {len(msgs)} 条消息。勾选左侧「展示完整消息流」可看工具原始返回。")
            for m in msgs:
                render_message(m)

    # 提供一个下载按钮, 方便把结果存档
    st.download_button(
        "⬇️ 下载本次决策（Markdown）",
        data=(
            f"# 投资委员会决策报告 · {st.session_state.ran_symbol}\n\n"
            f"回测年限：{st.session_state.ran_years or '?'} 年\n\n"
            f"## 基本面分析\n{state.get('fundamental_analysis', '')}\n\n"
            f"## 技术面分析\n{state.get('technical_analysis', '')}\n\n"
            f"## 风险预警\n{state.get('risk_analysis', '')}\n\n"
            f"## 历史回测数据\n{state.get('backtest_analysis', '') or '_（无）_'}\n\n"
            f"## 最终决策\n{state.get('final_decision', '')}\n"
        ),
        file_name=f"committee_{st.session_state.ran_symbol}.md",
        mime="text/markdown",
    )


if __name__ == "__main__":
    main()
