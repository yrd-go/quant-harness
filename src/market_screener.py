#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
market_screener.py — 第一层: 全市场海选漏斗 (5000 -> 300 -> 100)

漏斗流程
--------
    ① 全市场实时快照          一次请求拿到全部 A 股
    ② 硬性剔除                ST/退市、北交所/B股、停牌无成交、价格异常
    ③ 按成交额降序取前 N      默认 300
    ④ 逐只拉近 21 个交易日K线 算 20 日动量 (带限速与重试)
    ⑤ 按动量降序取前 N        默认 100 -> config/candidates.json

本层【不使用任何大模型】, 纯 pandas。

数据源实测结论(2026-10-04 探测)
-------------------------------
    东财快照 stock_zh_a_spot_em      -> 被限流(RemoteDisconnected), 连续 3 次失败
    新浪快照 stock_zh_a_spot         -> 可用(5571 行), 但慢(约 27s, 分页 70 次)
    东财K线  stock_zh_a_hist         -> 可用, 约 1.2s/只, 列名中文
    腾讯K线  stock_zh_a_hist_tx      -> 可用, 约 3.1s/只, 列名英文
因此: 快照层【新浪为主、东财为备】(与直觉相反, 因为东财正被限流);
      K线层【东财为主、腾讯为备】(要拉 300 次, 速度差异会放大到十几分钟)。

为什么要有"列名自适应"
----------------------
不同数据源的列名不一样(新浪有'成交额'、东财有'成交额'和'总市值'),
且 akshare 改版时会改字段名。硬写 df["成交额"] 一旦改名就 KeyError。
所以这里对每个需要的字段都接受一组候选列名, 找不到时把实际列名打出来。

用法
----
    # 完整跑一遍(约 300 只 K线, 预计 4~8 分钟)
    python src/market_screener.py

    # 极小规模验证(强烈建议第一次这样跑)
    python src/market_screener.py --amount-top 10 --momentum-top 5

    # 只探测快照接口的列名与可用性, 不跑漏斗
    python src/market_screener.py --probe

    # 只跑到成交额前 N, 不拉历史K线
    python src/market_screener.py --dry-run --amount-top 30

退出码
------
    0  成功
    1  数据源全部失败 / 通过校验失败
    2  参数错误
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd

# --------------------------------------------------------------------------- #
# 路径引导 + 配置导入
# --------------------------------------------------------------------------- #
_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent if (_HERE.parent / "config.py").exists() else _HERE
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from config import (  # noqa: E402
    AK_RETRY_BACKOFF, AK_RETRY_TIMES, AK_SLEEP_EVERY, AK_SLEEP_LONG,
    AK_SLEEP_PER_REQUEST, CONFIG_DATA_DIR, LOGS_DIR,
    SCREEN_AMOUNT_TOP_N, SCREEN_MOMENTUM_TOP_N, SCREEN_MOMENTUM_WINDOW,
    TIMEZONE_NAME, now_bj,
)

OUT_FILE = CONFIG_DATA_DIR / "candidates.json"
LOG_FILE = LOGS_DIR / "market_screener.log"

# 需要剔除的代码前缀
EXCLUDE_PREFIX = ("4", "8", "9")        # 北交所(4/8) 与 B股(9)
ST_KEYWORDS = ("ST", "退", "PT")        # 名称里含这些字样视为风险股

# 动量计算结果里 bars(可用K线根数)低于该值就视为"数据不足"
MIN_BARS_NEEDED = 21                    # window+1

log = logging.getLogger("market_screener")


# --------------------------------------------------------------------------- #
# 日志
# --------------------------------------------------------------------------- #
def setup_logging(verbose: bool = True) -> None:
    """终端 + 文件双写。文件落在 logs/market_screener.log。"""
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

    # 让 akshare / urllib3 的噪音闭嘴
    for noisy in ("urllib3", "akshare", "chardet"):
        logging.getLogger(noisy).setLevel(logging.ERROR)


@contextlib.contextmanager
def suppress_tqdm():
    """屏蔽 akshare 内部的 tqdm 进度条。

    新浪快照源会分页抓 70 次并刷进度条(stderr), 在终端里非常吵。
    实现要点(踩过坑): akshare 里写的是 ``from akshare.utils.tqdm import get_tqdm``
    再 ``tqdm = get_tqdm()``, 所以必须改【调用方模块】命名空间里的引用,
    而且替换品仍然要是"返回迭代器"的工厂函数。
    """
    patched: list[tuple[object, str, object]] = []
    try:
        from akshare.utils import tqdm as ak_tqdm

        def _disabled_get_tqdm(enable: bool = True):
            _ = enable
            return lambda iterable, *args, **kwargs: iterable

        # 把工具模块和常见调用方都换掉
        targets = [(ak_tqdm, "get_tqdm")]
        for mod_name in ("akshare.stock.stock_zh_a_sina",
                         "akshare.stock_feature.stock_hist_tx"):
            try:
                mod = __import__(mod_name, fromlist=["get_tqdm"])
                targets.append((mod, "get_tqdm"))
            except Exception:
                continue

        for mod, attr in targets:
            if hasattr(mod, attr):
                patched.append((mod, attr, getattr(mod, attr)))
                setattr(mod, attr, _disabled_get_tqdm)
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
# 通用: 重试 + 列名自适应
# --------------------------------------------------------------------------- #
def with_retry(func, what: str, times: int = AK_RETRY_TIMES,
               backoff: float = AK_RETRY_BACKOFF):
    """带指数退避的重试包装。全部失败时抛出最后一次异常。"""
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


def pick_column(df: pd.DataFrame, candidates: list[str], field: str) -> str | None:
    """从候选列名里挑出实际存在的那个; 都找不到返回 None。

    不抛异常是为了让调用方能决定"这个字段是不是必需"(比如总市值可有可无)。
    """
    for name in candidates:
        if name in df.columns:
            return name
    return None


def require_column(df: pd.DataFrame, candidates: list[str], field: str) -> str:
    """必需字段: 找不到就抛出带实际列名的清晰错误。"""
    col = pick_column(df, candidates, field)
    if col is None:
        raise KeyError(
            f"快照数据里找不到字段【{field}】(尝试过这些列名: {candidates})\n"
            f"       实际列名: {list(df.columns)}\n"
            f"       请运行: python src/market_screener.py --probe  查看真实列名, "
            f"再把新列名加进本文件的候选列表。"
        )
    return col


# --------------------------------------------------------------------------- #
# 阶段①: 全市场快照 (新浪为主, 东财为备)
# --------------------------------------------------------------------------- #
def fetch_spot_sina(ak) -> pd.DataFrame:
    """新浪: stock_zh_a_spot —— 实测可用(5571 行), 但慢(约 27s)。"""
    with suppress_tqdm():
        df = ak.stock_zh_a_spot()
    if df is None or df.empty:
        raise RuntimeError("新浪快照返回空数据")
    df = df.copy()
    df["_source"] = "sina"
    return df


def fetch_spot_eastmoney(ak) -> pd.DataFrame:
    """东财: stock_zh_a_spot_em —— 实测正被限流, 作为备源。"""
    with suppress_tqdm():
        df = ak.stock_zh_a_spot_em()
    if df is None or df.empty:
        raise RuntimeError("东财快照返回空数据")
    df = df.copy()
    df["_source"] = "eastmoney"
    return df


def fetch_spot(ak) -> tuple[pd.DataFrame, str]:
    """按优先级尝试快照源, 返回 (数据, 实际使用的源名)。"""
    sources = [("sina", fetch_spot_sina), ("eastmoney", fetch_spot_eastmoney)]
    errors: list[str] = []

    for name, func in sources:
        log.info("正在获取全市场快照: %s ...", name)
        t0 = time.time()
        try:
            df = with_retry(lambda: func(ak), f"快照@{name}")
            log.info("  %s 成功: %d 行 x %d 列, 耗时 %.1fs",
                     name, len(df), len(df.columns), time.time() - t0)
            if name != sources[0][0]:
                log.warning("  已自动切换到备用快照源: %s", name)
            return df, name
        except Exception as exc:
            msg = f"{name}: {type(exc).__name__}: {exc}"
            errors.append(msg)
            log.warning("  快照源 %s 失败: %s", name, type(exc).__name__)

    raise RuntimeError(
        "所有快照源都失败了:\n        " + "\n        ".join(errors) +
        "\n       提示: 东财接口此前已被实测限流; 若新浪也不可用, 请稍后重试。"
    )


def probe_spot(ak) -> int:
    """--probe: 打印各快照源的可用性与列名, 不跑漏斗。"""
    print("=" * 76)
    print("快照接口探测")
    print("=" * 76)
    for name, func in (("sina (stock_zh_a_spot)", fetch_spot_sina),
                       ("eastmoney (stock_zh_a_spot_em)", fetch_spot_eastmoney)):
        print(f"\n--- {name} ---")
        t0 = time.time()
        try:
            df = with_retry(lambda: func(ak), f"probe@{name}", times=1)
            print(f"  成功: {len(df)} 行 x {len(df.columns)} 列, 耗时 {time.time()-t0:.1f}s")
            for i, c in enumerate(df.columns):
                print(f"    {i:2d}. {c!r}")
            print("  前 3 行:")
            print(df.head(3).to_string())
            # 顺便报告本脚本关心的字段能否被识别
            print("  字段识别结果:")
            for field, cands in (("代码", ["代码", "symbol"]),
                                 ("名称", ["名称", "name"]),
                                 ("成交额", ["成交额", "amount"]),
                                 ("最新价", ["最新价", "close"]),
                                 ("总市值", ["总市值", "流通市值", "market_cap"])):
                got = pick_column(df, cands, field)
                print(f"    {field:6s} -> {got!r}" + ("" if got else "  (未找到, 可选)"))
        except Exception as exc:
            print(f"  失败: {type(exc).__name__}: {str(exc)[:120]}")
        time.sleep(1.0)
    print("\n" + "=" * 76)
    return 0


# --------------------------------------------------------------------------- #
# 阶段②: 过滤
# --------------------------------------------------------------------------- #
def filter_universe(df: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """剔除 ST/退市、北交所/B股、停牌无成交、价格异常。返回 (清洗后, 统计)。"""
    col_code = require_column(df, ["代码", "symbol"], "代码")
    col_name = require_column(df, ["名称", "name"], "名称")
    col_amount = require_column(df, ["成交额", "amount"], "成交额")
    col_price = pick_column(df, ["最新价", "close"], "最新价")   # 可选

    stat: dict[str, int] = {}
    work = df.copy()
    work["_code"] = work[col_code].astype(str).str.extract(r"(\d{6})", expand=False)
    work = work.dropna(subset=["_code"])          # 代码解析不出来的直接丢
    work["_name"] = work[col_name].astype(str)
    work["_amount"] = pd.to_numeric(work[col_amount], errors="coerce")
    work["_price"] = (pd.to_numeric(work[col_price], errors="coerce")
                      if col_price else pd.Series([pd.NA] * len(work), index=work.index))

    n0 = len(work)

    mask = work["_code"].str.startswith(EXCLUDE_PREFIX)
    stat["exchange"] = int(mask.sum())
    work = work[~mask]

    mask = work["_name"].str.upper().str.contains("|".join(ST_KEYWORDS), na=False)
    stat["st"] = int(mask.sum())
    work = work[~mask]

    mask = work["_amount"].isna() | (work["_amount"] <= 0)
    stat["suspended"] = int(mask.sum())
    work = work[~mask]

    if col_price:
        mask = work["_price"].isna() | (work["_price"] <= 0)
        stat["bad_price"] = int(mask.sum())
        work = work[~mask]
    else:
        stat["bad_price"] = 0

    stat["_total"] = n0
    stat["_after"] = int(len(work))
    return work, stat


# --------------------------------------------------------------------------- #
# 阶段④: 动量计算 (东财为主, 腾讯为备)
# --------------------------------------------------------------------------- #
def _market_prefix(symbol: str) -> str:
    """'000001' -> 'sz000001'; '600519' -> 'sh600519' (5/6/9 开头为沪市)"""
    return ("sh" if symbol.startswith(("5", "6", "9")) else "sz") + symbol


def _hist_eastmoney(ak, symbol: str, start: str, end: str) -> pd.DataFrame:
    with suppress_tqdm():
        return ak.stock_zh_a_hist(symbol=symbol, period="daily",
                                  start_date=start, end_date=end, adjust="qfq")


def _hist_tencent(ak, symbol: str, start: str, end: str) -> pd.DataFrame:
    with suppress_tqdm():
        df = ak.stock_zh_a_hist_tx(symbol=_market_prefix(symbol),
                                   start_date=start, end_date=end, adjust="qfq")
    return df.rename(columns={"date": "日期", "close": "收盘"})


def fetch_momentum_one(ak, symbol: str, window: int) -> dict | None:
    """取单只最近 window+1 个交易日收盘价, 算动量。

    返回 {"momentum_pct": float, "bars": int, "source": str} ; 数据不足返回 None。

    动量口径与 backtest.py / quant_agent.py 完全一致:
        momentum = close[最新] / close[window 个交易日前] - 1
    数据不足(不足 window+1 根K线)返回 None —— 调用方会剔除而不是填 0,
    否则这些"算不出来"的票会排到中游污染漏斗。
    """
    end = now_bj().strftime("%Y%m%d")
    # 留足日历余量: window 个交易日 ≈ window*1.5 个自然日, 再放宽一些
    start = (now_bj() - pd.Timedelta(days=int(window * 2.5) + 40)).strftime("%Y%m%d")

    df = None
    used = ""
    for src_name, func in (("eastmoney", _hist_eastmoney), ("tencent", _hist_tencent)):
        try:
            candidate = func(ak, symbol, start, end)
            if candidate is not None and not candidate.empty:
                df, used = candidate, src_name
                break
        except Exception:
            continue

    if df is None or df.empty:
        return None

    col_close = pick_column(df, ["收盘", "close"], "收盘")
    if col_close is None:
        return None
    close = pd.to_numeric(df[col_close], errors="coerce").dropna()
    if len(close) < window + 1:
        return None
    newest = float(close.iloc[-1])
    oldest = float(close.iloc[-(window + 1)])
    if oldest <= 0:
        return None
    return {
        "momentum_pct": round((newest / oldest - 1.0) * 100.0, 4),
        "bars": int(len(close)),
        "source": used,
    }


def compute_momentum_batch(ak, symbols: list[str], window: int,
                           sleep_per: float
                           ) -> tuple[dict[str, dict], list[str], dict[str, int]]:
    """批量算动量, 返回 ({symbol: 结果}, 失败列表, 数据源使用统计)。

    限速: 每次请求后 sleep_per 秒; 每 AK_SLEEP_EVERY 次额外休眠 AK_SLEEP_LONG 秒。
    目的: 别把仅剩可用的 K线接口也打挂(东财此前已被限流)。

    为什么返回"源使用统计": 当主源(东财)被限流时, 全部请求会静默退化到备用源(腾讯),
    速度差 3 倍。把统计暴露出来, 才能一眼看出"这次实际是靠哪个源跑完的"。
    """
    results: dict[str, dict] = {}
    failed: list[str] = []
    source_used: dict[str, int] = {}
    total = len(symbols)

    for i, sym in enumerate(symbols, 1):
        got = None
        try:
            got = with_retry(lambda: fetch_momentum_one(ak, sym, window),
                             f"动量@{sym}")
        except Exception as exc:
            log.debug("  %s 拉取失败: %s", sym, type(exc).__name__)

        if got is None:
            failed.append(sym)
        else:
            results[sym] = got
            src = got.get("source") or "unknown"
            source_used[src] = source_used.get(src, 0) + 1

        if i % 25 == 0 or i == total:
            log.info("  进度 %d/%d  成功 %d  失败/数据不足 %d",
                     i, total, len(results), len(failed))

        time.sleep(sleep_per)
        if AK_SLEEP_EVERY and i % AK_SLEEP_EVERY == 0 and i < total:
            log.info("  已请求 %d 次, 额外休眠 %.1fs 限速", i, AK_SLEEP_LONG)
            time.sleep(AK_SLEEP_LONG)

    # 源使用情况汇总: 主源全挂时必须让用户看到, 否则他会以为跑得慢是别的原因
    if source_used:
        log.info("  K线数据源使用统计: %s",
                 ", ".join(f"{k} {v} 只" for k, v in sorted(source_used.items())))
        if "eastmoney" not in source_used:
            log.warning("  ⚠ 主源(东财 stock_zh_a_hist)本次【全部失败】, "
                        "已全部退化到腾讯备用源 —— 速度约慢 3 倍, 属预期行为(该接口此前被限流)。")

    return results, failed, source_used


# --------------------------------------------------------------------------- #
# 数据校验 (落盘前的最后一道闸)
# --------------------------------------------------------------------------- #
class ScreenValidationError(RuntimeError):
    """漏斗结果没通过校验 —— 不允许把这种数据写进 JSON 给下游 Agent。"""


def validate_results(total_attempted: int, mom_map: dict, final_count: int,
                     fail_ratio_limit: float = 0.5) -> dict:
    """四道校验。任何一道不通过就抛 ScreenValidationError。

    为什么失败率要按"拉取尝试数"算, 而不是按"最终保留数"算:
        --amount-top 300 --momentum-top 100 时, 有 200 只是被动量排序【正常淘汰】的,
        不是"算不出来"。如果按 (300-100)/300 = 67% 算, 每次都会误报失败。
        正确口径: 失败率 = 1 - 成功算出动量的数量 / 已尝试拉取的只数。
    """
    if total_attempted <= 0:
        raise ScreenValidationError(
            "没有任何标的进入动量计算阶段(拉取尝试数为 0), 疑似上游过滤全部失败。"
        )

    ok = len(mom_map)
    fail_ratio = 1.0 - (ok / total_attempted)

    if ok == 0:
        raise ScreenValidationError(
            f"动量计算全部失败(尝试 {total_attempted} 只, 成功 0 只)。\n"
            f"       最可能原因: K线接口被限流。请稍后重试, 或调大 --sleep。"
        )
    if fail_ratio > fail_ratio_limit:
        raise ScreenValidationError(
            f"动量计算失败率过高: {fail_ratio:.1%} (阈值 {fail_ratio_limit:.0%}), "
            f"成功 {ok} / 尝试 {total_attempted}。\n"
            f"       拒绝写入 JSON —— 否则下游 Agent 会基于残缺数据做决策。"
        )
    if final_count <= 0:
        raise ScreenValidationError(
            f"最终保留数为 0(成功算出动量 {ok} 只, 但按 top_n 过滤后为空)。"
        )

    return {"attempted": total_attempted, "momentum_ok": ok,
            "fail_ratio": round(fail_ratio, 4), "final": final_count}


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="第一层: 全市场海选漏斗")
    p.add_argument("--amount-top", type=int, default=SCREEN_AMOUNT_TOP_N,
                   help=f"按成交额保留前 N(默认 {SCREEN_AMOUNT_TOP_N})")
    p.add_argument("--momentum-top", type=int, default=SCREEN_MOMENTUM_TOP_N,
                   help=f"按动量保留前 N(默认 {SCREEN_MOMENTUM_TOP_N})")
    p.add_argument("--momentum-window", type=int, default=SCREEN_MOMENTUM_WINDOW,
                   help=f"动量窗口(默认 {SCREEN_MOMENTUM_WINDOW})")
    p.add_argument("--sleep", type=float, default=AK_SLEEP_PER_REQUEST,
                   help=f"每次K线请求后的休眠秒数(默认 {AK_SLEEP_PER_REQUEST})")
    p.add_argument("--fail-ratio-limit", type=float, default=0.5,
                   help="动量失败率上限, 超过则终止不落盘(默认 0.5)")
    p.add_argument("--probe", action="store_true",
                   help="只探测快照接口可用性与列名, 不跑漏斗")
    p.add_argument("--dry-run", action="store_true",
                   help="只跑到成交额前 N, 不拉历史K线")
    p.add_argument("--quiet", action="store_true", help="只输出 INFO 及以上")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    setup_logging(verbose=not args.quiet)

    if args.amount_top < 1 or args.momentum_top < 1 or args.momentum_window < 1:
        print("[错误] --amount-top / --momentum-top / --momentum-window 都必须 >= 1")
        return 2
    if not (0.0 <= args.fail_ratio_limit <= 1.0):
        print(f"[错误] --fail-ratio-limit 必须在 0~1 之间, 当前 {args.fail_ratio_limit}")
        return 2

    log.info("=" * 74)
    log.info("第一层海选开始 | 时区 %s | 当前北京时间 %s",
             TIMEZONE_NAME, now_bj().strftime("%Y-%m-%d %H:%M:%S"))
    log.info("参数: 成交额 Top%d -> 动量 Top%d (窗口 %d 日) | 限速 %.2fs/次",
             args.amount_top, args.momentum_top, args.momentum_window, args.sleep)
    log.info("=" * 74)

    # ---- akshare ----
    try:
        import akshare as ak
    except ImportError:
        log.error("未安装 akshare, 请执行: pip install -U akshare")
        return 1

    if args.probe:
        return probe_spot(ak)

    # ---- 阶段① 快照 ----
    try:
        spot, source = fetch_spot(ak)
    except RuntimeError as exc:
        log.error("%s", exc)
        return 1

    # ---- 阶段② 过滤 ----
    try:
        clean, stat = filter_universe(spot)
    except KeyError as exc:
        log.error("%s", exc)
        return 1

    log.info("过滤: 全市场 %d -> 剩余 %d  (剔除 北交所/B股 %d, ST/退市 %d, "
             "停牌无成交 %d, 价格异常 %d)",
             stat["_total"], stat["_after"], stat.get("exchange", 0), stat.get("st", 0),
             stat.get("suspended", 0), stat.get("bad_price", 0))

    if clean.empty:
        log.error("过滤后没有剩余标的。请用 --probe 检查快照列名是否匹配。")
        return 1

    # ---- 阶段③ 成交额 ----
    top_amount = (clean.sort_values("_amount", ascending=False)
                       .head(args.amount_top).reset_index(drop=True))
    log.info("成交额 Top%d 已选出; 成交额区间 %.2f 亿 ~ %.2f 亿",
             args.amount_top, top_amount["_amount"].min() / 1e8,
             top_amount["_amount"].max() / 1e8)

    if args.dry_run:
        print()
        print("--dry-run: 到此为止(不拉历史K线)。前 10 名:")
        print(top_amount[["_code", "_name", "_amount"]]
              .head(10).to_string(index=False))
        return 0

    # ---- 阶段④ 动量 ----
    symbols = top_amount["_code"].tolist()
    est = len(symbols) * (args.sleep + 1.4)
    log.info("开始拉取 %d 只的近 %d 日K线, 预计 %.1f 分钟",
             len(symbols), args.momentum_window, est / 60)

    mom_map, failed, source_used = compute_momentum_batch(
        ak, symbols, args.momentum_window, args.sleep)

    # ---- 校验(不通过就抛异常, 不落盘) ----
    scored = top_amount.copy()
    scored["_momentum"] = scored["_code"].map(
        lambda c: mom_map.get(c, {}).get("momentum_pct"))
    scored["_bars"] = scored["_code"].map(
        lambda c: mom_map.get(c, {}).get("bars"))
    scored["_ksource"] = scored["_code"].map(
        lambda c: mom_map.get(c, {}).get("source"))
    scored = scored.dropna(subset=["_momentum"])
    scored = scored.sort_values("_momentum", ascending=False).head(args.momentum_top)

    try:
        checks = validate_results(len(symbols), mom_map, len(scored),
                                  args.fail_ratio_limit)
    except ScreenValidationError as exc:
        log.error("数据校验未通过, 已终止且【未写入】%s", OUT_FILE.name)
        log.error("%s", exc)
        return 1

    log.info("校验通过: 尝试 %d, 成功 %d, 失败率 %.1f%%, 最终 %d 只",
             checks["attempted"], checks["momentum_ok"],
             checks["fail_ratio"] * 100, checks["final"])

    # ---- 落盘 ----
    payload = {
        "generated_at": now_bj().strftime("%Y-%m-%d %H:%M:%S"),
        "timezone": TIMEZONE_NAME,
        "spot_source": source,
        "kline_sources": source_used,
        "stages": {
            "market_total": stat["_total"],
            "after_filter": stat["_after"],
            "top_amount": int(len(top_amount)),
            "momentum_ok": checks["momentum_ok"],
            "final": checks["final"],
        },
        "params": {
            "amount_top_n": args.amount_top,
            "momentum_top_n": args.momentum_top,
            "momentum_window": args.momentum_window,
            "adjust": "qfq",
        },
        "excluded": {
            "exchange": stat.get("exchange", 0),
            "st": stat.get("st", 0),
            "suspended": stat.get("suspended", 0),
            "bad_price": stat.get("bad_price", 0),
            "momentum_failed": len(failed),
        },
        "checks": checks,
        "candidates": [
            {
                "rank": i,
                "symbol": row["_code"],
                "name": row["_name"],
                "amount": float(row["_amount"]),
                "momentum_pct": round(float(row["_momentum"]), 2),
                # bars = 可用K线根数。下游若要"上市满N年"约束, 用 bars >= N*250 判定,
                # 比用代码段猜次新股可靠得多。
                "bars": int(row["_bars"]) if pd.notna(row["_bars"]) else None,
                "kline_source": row["_ksource"],
            }
            for i, (_, row) in enumerate(scored.iterrows(), 1)
        ],
    }

    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUT_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    log.info("已保存 %d 只候选股 -> %s", checks["final"], OUT_FILE)

    print()
    print("=" * 76)
    print(f"海选完成 | 快照源 {source} | 最终 {checks['final']} 只 | 动量前 15 名:")
    print("=" * 76)
    for c in payload["candidates"][:15]:
        print(f"  {c['rank']:>3}. {c['symbol']} {c['name']:<10} "
              f"动量 {c['momentum_pct']:+7.2f}%   "
              f"成交额 {c['amount'] / 1e8:6.2f} 亿   bars {c['bars']}")
    print("=" * 76)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
