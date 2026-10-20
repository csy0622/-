#!/usr/bin/env python3
"""投资助手 3.0 — Python 适配层（薄）。

把「取数与拟合」这件事包成「吃 JSON 吐 JSON」的进程接口，供 Node 侧的 Pi agent 以工具形式调用。
**不修改 icbt 内核**，只读复用它的数据与口径。

用法：
    python3 bridge.py deviate [--codes a,b,c] [--fresh]
    python3 bridge.py fit     [--codes a,b,c] [--fresh]

输出：单行 JSON 到 stdout，形如 {"ok": true, "data": {...}} / {"ok": false, "error": {...}}
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

# --- 让 akshare 走直连（本机实测：东财行情接口被网络层重置，指数/净值/新浪口径可用）---
for _k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
    os.environ.pop(_k, None)
os.environ.setdefault("NO_PROXY", "csindex.com.cn,sina.com.cn,eastmoney.com")

HERE = Path(__file__).resolve().parent
CACHE_DIR = HERE.parent / "data" / "cache"
WATCHLIST = HERE.parent / "config" / "watchlist.yaml"
ICBT_RAW = Path.home() / "ic_backtest" / "data" / "raw"
CACHE_TTL = timedelta(hours=20)


def load_watchlist() -> dict:
    import yaml

    with open(WATCHLIST, encoding="utf-8") as f:
        return yaml.safe_load(f)


def read_cache(code: str):
    """返回 (df, basis)。口径写进文件名（{code}.{basis}.parquet），这样命中缓存时
    也能如实报出口径，不会退化成一句 'cache'。"""
    import pandas as pd

    hits = sorted(CACHE_DIR.glob(f"{code}.*.parquet"), key=lambda p: p.stat().st_mtime, reverse=True)
    for p in hits:
        if datetime.now() - datetime.fromtimestamp(p.stat().st_mtime) > CACHE_TTL:
            continue
        basis = p.name[len(code) + 1 : -len(".parquet")]
        return pd.read_parquet(p), basis
    return None, None


def write_cache(code: str, basis: str, df) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    # 换口径重取时清掉旧口径的文件，避免残留被误读成当前口径
    for old in CACHE_DIR.glob(f"{code}.*.parquet"):
        if old.name != f"{code}.{basis}.parquet":
            old.unlink(missing_ok=True)
    df.to_parquet(CACHE_DIR / f"{code}.{basis}.parquet", index=False)


def normalize(df, date_col_candidates, close_col_candidates):
    """统一成 date/close 两列，并丢掉中证接口的 base-date 合成首行。"""
    import pandas as pd

    df = df.copy()
    df.columns = [str(c) for c in df.columns]
    dc = next((c for c in date_col_candidates if c in df.columns), None)
    cc = next((c for c in close_col_candidates if c in df.columns), None)
    if dc is None or cc is None:
        raise ValueError(f"列名不识别: {list(df.columns)}")
    df = df[[dc, cc]].dropna()
    df.columns = ["date", "close"]
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)
    df["close"] = df["close"].astype(float)
    if len(df) > 1 and abs(df["close"].iloc[0] - df["close"].iloc[1]) < 1e-9:
        df = df.iloc[1:].reset_index(drop=True)
    return df


def fetch_series(code: str, kind: str, fresh: bool = False):
    """按代码取日线。kind: futures_underlying | etf | index"""
    import akshare as ak
    import pandas as pd

    if not fresh:
        cached, basis = read_cache(code)
        if cached is not None:
            return cached, basis

    last_err = None
    # ⚠️ 这里**不能**优先用 icbt 的本地 parquet：那份数据只在 icbt 自己的更新任务跑过之后才新，
    # 实测停在 2026-10-09，比其它标的晚 3 个交易日，会把日报顶部「数据截至」整体拖旧。
    # 所以中证500 也走网络；全部网络路径失败时才退回本地 parquet（见函数末尾）。

    attempts = []
    if kind == "etf":
        # ETF 走「累计净值」口径，且**只取稀疏点**：每年年末 + 最近几天。
        # 为什么不用行情价：它是未复权的，分红/份额折算把序列压得很低 ——
        #   515880 未复权价 0.5920，而累计净值 3.5526（差 6 倍）；
        #   512800 上市 0.9990 → 现 0.8500，而银行指数同期是涨的。
        #   拿未复权价拟合基线，斜率会被拟成几乎持平甚至下行，结论就错了。
        # 为什么只取稀疏点：基线拟合只要年末收盘（fit_on=年末收盘），偏离只要最新值；
        #   全量日线要翻 113 页（实测每页 6–22 秒 → 一只约 28 分钟，8 只要 2 小时，跑不完）。
        # 注意：这条稀疏序列**不能**用来算贝塔/相关性（那需要日频收益，得另做全量回填）。
        import pandas as pd  # noqa: PLC0415

        def _fund(code_: str, s: str, e: str):
            return ak.fund_etf_fund_info_em(fund=code_, start_date=s, end_date=e)

        frames, notes = [], []
        for y in range(2010, datetime.now().year + 1):
            try:
                frames.append(normalize(_fund(code, f"{y}1218", f"{y}1231"),
                                        ("净值日期", "日期", "date"), ("累计净值", "收盘", "close")))
            except Exception:  # noqa: BLE001
                continue
        try:
            recent = (datetime.now() - timedelta(days=12)).strftime("%Y%m%d")
            frames.append(normalize(_fund(code, recent, datetime.now().strftime("%Y%m%d")),
                                    ("净值日期", "日期", "date"), ("累计净值", "收盘", "close")))
        except Exception:  # noqa: BLE001
            pass
        if frames:
            df = (pd.concat(frames, ignore_index=True)
                    .drop_duplicates(subset=["date"]).sort_values("date").reset_index(drop=True))
            if len(df) >= 4:
                write_cache(code, "nav-yearends", df)
                return df, "nav-yearends"

        sym = ("sh" if code.startswith("5") else "sz") + code
        attempts.append(("sina-raw", lambda: ak.fund_etf_hist_sina(symbol=sym)))
    else:
        sina_sym = ("sz" if code[0] in "39" else "sh") + code
        attempts.append(("csindex", lambda: ak.stock_zh_index_hist_csindex(
            symbol=code, start_date="20040101", end_date=datetime.now().strftime("%Y%m%d"))))
        attempts.append(("sina", lambda: ak.stock_zh_index_daily(symbol=sina_sym)))
        attempts.append(("em", lambda: ak.stock_zh_index_daily_em(symbol=code)))

    for how, fn in attempts:
        try:
            df = normalize(fn(), ("日期", "date", "净值日期"), ("收盘", "close", "累计净值"))
            write_cache(code, how, df)
            return df, how
        except Exception as e:  # noqa: BLE001
            last_err = f"{how}:{type(e).__name__}"
            continue
    # 最后兜底：中证500 用 icbt 本地 parquet（**可能滞后**，口径里明确标出来，避免被当新数据）
    if code == "000905" and (ICBT_RAW / "spot_000905_daily.parquet").exists():
        df = normalize(pd.read_parquet(ICBT_RAW / "spot_000905_daily.parquet"), ("date",), ("close",))
        write_cache(code, "icbt-parquet-stale", df)
        return df, "icbt-parquet-stale"
    raise RuntimeError(f"取数失败 {code}（尝试 {[a[0] for a in attempts]}，最后错误 {last_err}）")


def fit_baseline(df, base_year: int, fit_on: str = "年末收盘"):
    """拟合 点位 ≈ a + b × (年份小数 − base_year)。返回 (a, b, r2, n_points, basis)。"""
    import numpy as np
    import pandas as pd

    x_year = df["date"].dt.year
    if fit_on == "年末收盘":
        yearly = df.assign(y=df["date"].dt.year, m=df["date"].dt.month).sort_values("date")
        yearly = yearly.groupby("y").tail(1)
        # 当年尚未走完，剔除，避免用不完整的年份污染拟合
        cur = datetime.now().year
        yearly = yearly[yearly["y"] < cur]
        if len(yearly) >= 4:
            sample, basis = yearly, "年末收盘"
        else:
            sample, basis = df, "日线收盘（年末样本不足）"
    else:
        sample, basis = df, "日线收盘"

    xs = sample["date"].dt.year + (sample["date"].dt.dayofyear - 1) / 365.25 - base_year
    ys = sample["close"].astype(float)
    b, a = np.polyfit(xs, ys, 1)
    pred = a + b * xs
    ss_res = float(((ys - pred) ** 2).sum())
    ss_tot = float(((ys - ys.mean()) ** 2).sum())
    r2 = float(1 - ss_res / ss_tot) if ss_tot > 0 else float("nan")
    return float(a), float(b), r2, int(len(sample)), basis


def year_fraction(ts) -> float:
    return ts.year + (ts.dayofyear - 1) / 365.25


def build_rows(codes: list[str] | None, fresh: bool) -> dict:
    wl = load_watchlist()
    base_year = int(wl["baseline"]["base_year"])
    fit_on = wl["baseline"].get("fit_on", "年末收盘")
    frozen = {}
    for bucket in ("items", "etf_items", "index_items"):
        for it in wl["baseline"].get(bucket, []):
            if it.get("slope") is not None:
                frozen[str(it["code"])] = it
    # 额外关注标的（黄金/有色/油气/电池）的冻结基线也放 watch_extra 里
    for it in wl.get("watch_extra", {}).get("etf", []):
        if it.get("slope") is not None:
            frozen[str(it["code"])] = it

    # 目标清单：期指挂标的指数；**先指数（快），后 ETF（慢）**——
    # ETF 的净值端点实测能慢到 120 秒/页，放在前面会把快的指数全堵住。
    jobs: list[tuple[str, str, str]] = []          # (code, kind, label)
    seen = set()
    for f in wl.get("futures", []):
        u = str(f["underlying"])
        if u not in seen:
            jobs.append((u, "index", f"{f['code']} 的标的 · {f['name']}"))
            seen.add(u)
    for idx in wl.get("indices", {}).get("sectors", []):
        c = str(idx["code"])
        if c not in seen:
            jobs.append((c, "index", f"{idx['sector']} · {idx['name']}（指数）"))
            seen.add(c)
    for e in wl.get("etf", []):
        jobs.append((str(e["code"]), "etf", f"{e['sector']} · {e['name']}（ETF）"))
    # 额外关注（2026-10-13）：黄金/有色/油气/电池 ETF。个股不纳入（按 ETF 代理）
    for e in wl.get("watch_extra", {}).get("etf", []):
        jobs.append((str(e["code"]), "etf", f"{e.get('sector', '')} · {e['name']}（关注）"))

    if codes:
        want = {c.strip() for c in codes}
        jobs = [j for j in jobs if j[0] in want]

    rows, errors = [], []
    from store import connect as _connect, save_series as _save_series  # noqa: PLC0415

    _conn = _connect()
    for code, kind, label in jobs:
        try:
            df, how = fetch_series(code, kind, fresh=fresh)
            latest = df.iloc[-1]
            fr = year_fraction(latest["date"])
            if code in frozen:
                it = frozen[code]
                a = float(it["value_at_base"])
                b = float(it["slope"])
                r2 = it.get("r2")
                status = "已冻结"
                basis = it.get("status", "")
            else:
                a, b, r2, n, basis = fit_baseline(df, base_year, fit_on)
                status = f"临时拟合·待冻结（{basis}，{n} 点）"
            baseline = a + b * (fr - base_year)
            price_basis = {"nav-yearends": "累计净值(年末+最新)", "nav-cum": "累计净值",
                           "sina-raw": "未复权行情价",
                           "icbt-parquet": "现货指数收盘",
                           "icbt-parquet-stale": "⚠本地parquet(可能滞后)"}.get(how, how)
            # ETF 若退回到未复权行情价，基线斜率会被分红系统性低估，必须显式警告，
            # 不能让调用方把这种数字当结论用。
            if kind == "etf" and how not in ("nav-yearends", "nav-cum"):
                status += "  ⚠ 用的是未复权行情价，斜率被分红低估，本行不可用于结论"
            # 点位落 sqlite（他要求 ETF 与行情都进库，方便统一查询）
            try:
                _save_series(_conn, code, label, price_basis, how,
                             [(d.strftime("%Y-%m-%d"), float(c)) for d, c in zip(df["date"], df["close"])])
            except Exception as e:  # noqa: BLE001
                errors.append({"code": code, "label": label, "error": f"入库失败 {type(e).__name__}: {e}"})
            rows.append({
                "code": code,
                "label": label,
                "as_of": latest["date"].strftime("%Y-%m-%d"),
                "latest": round(float(latest["close"]), 4),
                "baseline": round(float(baseline), 4),
                "deviation_pct": round((float(latest["close"]) / baseline - 1) * 100, 2),
                "slope_per_year": round(b, 4),
                "r2": None if r2 is None else round(float(r2), 3),
                "start": df["date"].iloc[0].strftime("%Y-%m-%d"),
                "points": int(len(df)),
                "source": how,
                "price_basis": price_basis,
                "status": status,
            })
        except Exception as e:  # noqa: BLE001
            errors.append({"code": code, "label": label, "error": f"{type(e).__name__}: {e}"})

    _conn.close()
    return {
        "as_of": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "base_year": base_year,
        "fit_on": fit_on,
        "rows": rows,
        "errors": errors,
    }


def list_strategies() -> list[str]:
    d = Path.home() / "ic_backtest" / "configs" / "strategies"
    return sorted(p.name for p in d.glob("*.yaml"))


def cmd_backtest(strategy: str, principal: float | None, lots: int | None) -> dict:
    """用 icbt 的引擎跑一次回测，**结果落盘到助手自己的目录**，返回 run_id 与绩效。

    关键：调用 runner.run(..., persist=False) —— 这样 icbt 自身一个字节都不会被写。
    上一轮实测过：让模型自己写内联命令跑回测，它会把产物写进 ~/ic_backtest，且不留痕、无法复核。
    """
    import yaml as _yaml

    icbt_root = Path.home() / "ic_backtest"
    if str(icbt_root) not in sys.path:
        sys.path.insert(0, str(icbt_root))
    from icbt import runner  # noqa: PLC0415

    sfile = icbt_root / "configs" / "strategies" / strategy
    if not sfile.exists():
        raise FileNotFoundError(
            f"找不到策略配置 {strategy}。可用：{', '.join(list_strategies())}"
        )
    with open(sfile, encoding="utf-8") as f:
        cfg = _yaml.safe_load(f)
    cfg.setdefault("sizing", {})
    if principal:
        cfg["sizing"]["principal"] = float(principal)
    if lots:
        cfg["sizing"]["lots"] = int(lots)

    res = runner.run(cfg, use_cache=False, persist=False)   # ← 不写 icbt
    d = res.to_dict()

    out_dir = HERE.parent / "data" / "runs" / str(d["run_id"])
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "result.json").write_text(
        json.dumps(d, ensure_ascii=False, default=str), encoding="utf-8"
    )

    # run 元数据入 sqlite（便于「历史跑过哪些策略、结果如何」这类查询）
    from store import connect as _connect, save_run as _save_run  # noqa: PLC0415

    _c = _connect()
    try:
        _save_run(_c, {
            "run_id": d["run_id"], "strategy_file": strategy, "name": d.get("name"),
            "period": [d["dates"][0], d["dates"][-1]] if d.get("dates") else [],
            "metrics": d.get("metrics"), "extra": d.get("extra"),
            "trade_count": len(d.get("trades") or []),
            "artifact": str(out_dir / "result.json"),
        })
    finally:
        _c.close()

    return {
        "run_id": d["run_id"],
        "name": d.get("name"),
        "strategy_file": strategy,
        "period": [d["dates"][0], d["dates"][-1]] if d.get("dates") else [],
        "trading_days": len(d.get("dates") or []),
        "metrics": d.get("metrics"),
        "yearly": d.get("yearly"),
        "oos": d.get("oos"),
        "extra": d.get("extra"),
        "trade_count": len(d.get("trades") or []),
        "artifact": str(out_dir / "result.json"),
        "data_version": d.get("data_version"),
        "code_version": d.get("code_version"),
        "reproducible": "同 配置+数据+代码 会得到同一 run_id；artifacts 里存了完整结果",
    }


def main() -> int:
    ap = argparse.ArgumentParser(prog="bridge")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("deviate", "fit"):
        p = sub.add_parser(name)
        p.add_argument("--codes", default="")
        p.add_argument("--fresh", action="store_true")
    bt = sub.add_parser("backtest")
    bt.add_argument("--strategy", default="策略1.yaml", help="configs/strategies/ 下的文件名")
    bt.add_argument("--principal", type=float, default=None)
    bt.add_argument("--lots", type=int, default=None)
    bt.add_argument("--list", action="store_true", help="只列出可用策略")
    args = ap.parse_args()

    if args.cmd == "deviate" or args.cmd == "fit":
        codes = [c for c in args.codes.split(",") if c.strip()]
        try:
            data = build_rows(codes or None, args.fresh)
            print(json.dumps({"ok": True, "data": data}, ensure_ascii=False))
            return 0
        except Exception as e:  # noqa: BLE001
            print(json.dumps({"ok": False, "error": {"type": type(e).__name__, "message": str(e)}},
                             ensure_ascii=False))
            return 1

    # backtest
    try:
        if args.list:
            print(json.dumps({"ok": True, "data": {"strategies": list_strategies()}},
                             ensure_ascii=False))
            return 0
        data = cmd_backtest(args.strategy, args.principal, args.lots)
        print(json.dumps({"ok": True, "data": data}, ensure_ascii=False))
        return 0
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": {"type": type(e).__name__, "message": str(e)}},
                         ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.exit(main())
