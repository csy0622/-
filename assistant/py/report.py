#!/usr/bin/env python3
"""投资助手 3.0 — 每日晨报生成器（规则化，不调模型）。

四块，全部从 sqlite 取数：
  a 猫笔刀文章   ← articles（正文若拿不到会如实标注）
  b 要闻与异动   ← news（按板块关键词过滤）+ series（关注板块当日涨跌幅）
  c 研报与关注    ← research（按板块过滤）
  d 基线与贝塔   ← series + watchlist 里**已冻结**的基线参数

产物：data/reports/daily/<日期>.html（自包含单文件，白色系），并写入 reports 表存档。

用法：
    python3 report.py daily [--date YYYY-MM-DD] [--no-archive]
"""
from __future__ import annotations

import argparse
import html as H
import json
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from store import connect, save_report  # noqa: E402

OUT_DIR = HERE.parent / "data" / "reports" / "daily"
WATCHLIST = HERE.parent / "config" / "watchlist.yaml"
HOLDINGS = HERE.parent / "config" / "holdings.yaml"
BENCH = "000905"          # 贝塔基准：中证500
MOVE_THRESHOLD = 2.0      # 异动阈值 %


def load_holdings() -> dict:
    if not HOLDINGS.exists():
        return {}
    with open(HOLDINGS, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def build_advice(conn, wl: dict, d_rows: list[dict], moves: list[dict],
                 rel_news: list, rel_res: list) -> tuple[str, dict]:
    """模块 e：当前提示与风险（规则化汇总，不调模型）。

    三部分：汇总研报 / 重要信息 / 风险项。风险项是从数据里**算出来**的，不是模型编的。
    """
    hold = load_holdings()
    proxy = {str(k): str(v) for k, v in (hold.get("proxy_map") or {}).items()}
    pos = hold.get("positions") or []
    acct = hold.get("account") or {}
    dev_by_code = {r["code"]: r for r in d_rows if not r.get("missing")}
    move_by_code = {}
    for m in moves:
        # moves 的 label 里带代码，从 d_rows 反查更稳
        for code, r in dev_by_code.items():
            if r["label"] in m["label"]:
                move_by_code[code] = m["chg"]

    risks, watch = [], []

    # 风险 1：仓位与可用资金
    if acct:
        if float(acct.get("仓位") or 0) >= 99 and float(acct.get("可用") or 0) < 1000:
            risks.append(f"**仓位已满**（{acct.get('仓位')}%），可用资金仅 {acct.get('可用')} 元 —— 没有加仓或应对回撤的缓冲")

    # 风险 2：浮亏持仓
    for p in pos:
        if p.get("pnl_pct") is not None and p["pnl_pct"] < 0:
            risks.append(f"**{p['name']} 浮亏 {p['pnl_pct']:.2f}%**（成本 {p['cost']}，现价 {p['price']}）")

    # 风险 3：持仓/关注标的当日异动
    for p in pos:
        code = str(p["code"])
        etf = proxy.get(code, code)
        chg = move_by_code.get(etf)
        if chg is not None and abs(chg) >= MOVE_THRESHOLD:
            risks.append(f"{p['name']} 对应 ETF（{etf}）当日 **{chg:+.2f}%**，触及 ±{MOVE_THRESHOLD}% 异动阈值")

    # 风险 4：偏离基线过高 + 拟合很弱
    for r in d_rows:
        if r.get("missing") or r.get("dev") is None:
            continue
        if r["dev"] >= 20:
            weak = "（但 R² 很低，这条线几乎没解释力）" if (r.get("r2") or 0) < 0.2 else ""
            risks.append(f"{r['label']} 高于基线 **{r['dev']:+.1f}%**{weak}")

    # 关注清单（他点名要看的板块）
    for code, r in dev_by_code.items():
        for f in wl.get("watch_extra", {}).get("etf", []):
            if str(f["code"]) == code:
                chg = move_by_code.get(code)
                dev = r.get("dev")
                dev_s = f"{dev:+.1f}%" if dev is not None else "—（尚无冻结基线）"
                watch.append(f"{r['label']}：偏离基线 {dev_s}" + (f"，当日 {chg:+.2f}%" if chg is not None else ""))

    li = lambda xs: "\n".join(f"<li>{x}</li>" for x in xs)  # noqa: E731
    risk_html = li(risks) if risks else "<li class='mut'>按当前规则未触发风险项</li>"
    watch_html = li(watch) if watch else "<li class='mut'>暂无额外关注标的的更新</li>"
    news_html = "\n".join(
        f"<li><span class='mut'>{(n[0] or '')[:16]}</span> "
        f"<a href='{H.escape(n[4] if len(n) > 4 and n[4] else '#')}' target='_blank' rel='noopener'>{H.escape(n[1] or '')}</a></li>"
        for n in rel_news[:6]
    ) or "<li class='mut'>近 48 小时没有相关快讯</li>"
    res_html = "\n".join(
        f"<li><a href='{H.escape(r[5] or '#')}' target='_blank' rel='noopener'>{H.escape(r[1] or '')}</a>"
        f"　<span class='mut'>{H.escape(r[2] or '')} · {H.escape(r[3] or '')} · {H.escape(r[4] or '')}</span></li>"
        for r in rel_res[:5]
    ) or "<li class='mut'>没有相关研报</li>"

    html = f"""<div class="card"><h2>e · 当前提示与风险 <span class="tag a">规则汇总 · 不是投资建议</span>
<span class="tag">持仓 {len(pos)} 项{('／总资产 ' + format(acct.get('总资产', 0), ',.0f')) if acct else ''}</span></h2>
<div style="display:flex;gap:12px;flex-wrap:wrap">
  <div style="flex:1;min-width:260px">
    <div style="font-weight:600;margin:2px 0 4px">① 风险项</div><ul>{risk_html}</ul>
    <div style="font-weight:600;margin:10px 0 4px">② 关注板块</div><ul>{watch_html}</ul>
  </div>
  <div style="flex:1;min-width:260px">
    <div style="font-weight:600;margin:2px 0 4px">③ 相关研报（{len(rel_res)} 篇）</div><ul>{res_html}</ul>
    <div style="font-weight:600;margin:10px 0 4px">④ 相关快讯（{len(rel_news)} 条）</div><ul>{news_html}</ul>
  </div>
</div>
<div class="foot">风险项由数据规则算出（仓位/浮亏/异动/偏离基线），研报与快讯按关注板块过滤。
<b>持仓个股按其行业 ETF 代理分析</b>（招商/平安银行→银行ETF、美的→家电ETF、中际旭创/亨通光电/新易盛→通信ETF）。
本模块只做信息汇总与提示，<b>不构成投资建议</b>。</div></div>"""
    stats = {"risks": len(risks), "holdings": len(pos), "watch": len(watch)}
    return html, stats


def load_watchlist() -> dict:
    with open(WATCHLIST, encoding="utf-8") as f:
        return yaml.safe_load(f)


def series_df(conn, code: str) -> pd.DataFrame:
    """取该标的**当前口径**的序列。

    ⚠️ 必须按价格口径过滤：一个 code 可能同时存有多个口径的历史行（换口径重取时旧行还在），
    不过滤会让同一日期出现多行，pandas 直接报 "cannot reindex on an axis with duplicate labels"。
    """
    row = conn.execute(
        "SELECT price_basis FROM series_meta WHERE code=? ORDER BY updated_at DESC LIMIT 1", (code,)
    ).fetchone()
    if row:
        rows = conn.execute(
            "SELECT date, close FROM series WHERE code=? AND price_basis=? ORDER BY date",
            (code, row[0]),
        ).fetchall()
    else:
        rows = conn.execute("SELECT date, close FROM series WHERE code=? ORDER BY date", (code,)).fetchall()
    if not rows:
        return pd.DataFrame(columns=["date", "close"])
    df = pd.DataFrame(rows, columns=["date", "close"])
    df["date"] = pd.to_datetime(df["date"])
    df = df.drop_duplicates(subset=["date"], keep="last")
    return df.sort_values("date").reset_index(drop=True)


def year_frac(ts) -> float:
    return ts.year + (ts.dayofyear - 1) / 365.25


def baseline_at(params: dict, ts) -> float | None:
    if params.get("slope") is None:
        return None
    return params["value_at_base"] + params["slope"] * (year_frac(ts) - 2014)


def beta_of(conn, code: str, window: int | None = None) -> tuple[float | None, float | None, int]:
    a, b = series_df(conn, code), series_df(conn, BENCH)
    if a.empty or b.empty:
        return None, None, 0
    ra = a.set_index("date")["close"].pct_change()
    rb = b.set_index("date")["close"].pct_change()
    j = pd.concat([ra.rename("a"), rb.rename("b")], axis=1).dropna()
    if window:
        j = j.tail(window)
    if len(j) < 60 or j["b"].var() == 0:
        return None, None, len(j)
    beta = float(j["a"].cov(j["b"]) / j["b"].var())
    return beta, float(j["a"].corr(j["b"]) ** 2), len(j)


def sector_keywords(wl: dict) -> list[str]:
    kws = []
    for e in wl.get("etf", []):
        kws.append(e["sector"])
        kws.extend([w for w in str(e.get("name", "")).replace("ETF", " ").split() if len(w) >= 2])
    for k in ("银行", "券商", "证券", "家电", "光伏", "稀土", "机器人", "通信", "光模块", "电池", "电力设备", "有色金属", "机械设备"):
        kws.append(k)
    seen, out = set(), []
    for k in kws:
        k = k.strip()
        if k and k not in seen:
            seen.add(k)
            out.append(k)
    return out


PAGE = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>投资晨报 {date}</title><style>
body{{font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;background:#f5f6f8;margin:0;color:#222;font-size:13px}}
.wrap{{max-width:1080px;margin:0 auto;padding:20px 16px 60px}}
h1{{font-size:19px;margin:0 0 6px}}
.sub{{color:#666;font-size:12.5px;margin-bottom:14px;line-height:1.7}}
.card{{background:#fff;border:1px solid #e3e6ea;border-radius:12px;padding:16px;box-shadow:0 1px 3px rgba(0,0,0,.04);margin-bottom:16px}}
.card h2{{font-size:14.5px;margin:0 0 12px}}
table{{border-collapse:collapse;width:100%;font-size:12.5px}}
td,th{{border:1px solid #e3e6ea;padding:7px 10px;text-align:left;vertical-align:top}}
th{{background:#f0f3f7;font-weight:600}}
.num{{text-align:right;font-variant-numeric:tabular-nums}}
.red{{color:#e0415b}}.green{{color:#2ca02c}}.mut{{color:#999}}
.tag{{font-size:11px;padding:2px 7px;border-radius:5px;background:#eef2f8;color:#2f6fd6}}
.tag.r{{background:#fdecef;color:#e0415b}}.tag.g{{background:#eaf6ec;color:#2ca02c}}.tag.a{{background:#fdf3e2;color:#d98a12}}
.art{{border-left:3px solid #2f6fd6;background:#f7f9fb;border-radius:0 8px 8px 0;padding:10px 13px;line-height:1.9;white-space:pre-wrap;max-height:420px;overflow:auto}}
.foot{{font-size:11.5px;color:#999;line-height:1.7;margin-top:10px}}
.warn{{font-size:12px;color:#333;line-height:1.8;background:#fff8e6;border:1px solid #f0e0a8;border-radius:8px;padding:10px 12px}}
ul{{margin:6px 0;padding-left:20px}}li{{margin:3px 0;line-height:1.7}}
a{{color:#2f6fd6;text-decoration:none}}a:hover{{text-decoration:underline}}
</style></head><body><div class="wrap">
<h1>投资晨报 · {date}</h1>
<div class="sub">{subtitle}</div>
{body}
<div class="foot">本报告由投资助手 3.0 自动生成，只做信息整理与解读，<b>不构成投资建议</b>。</div>
</div></body></html>"""


def build(conn, date_str: str) -> tuple[str, dict]:
    wl = load_watchlist()
    base_cfg = wl["baseline"]
    frozen: dict[str, dict] = {}
    for bucket in ("items", "etf_items", "index_items"):
        for it in base_cfg.get(bucket, []):
            frozen[str(it["code"])] = it
    for it in wl.get("watch_extra", {}).get("etf", []):
        if it.get("slope") is not None:
            frozen[str(it["code"])] = it

    # ---- d 基线与贝塔 ----
    d_rows, as_of = [], None
    targets = []
    for f in wl.get("futures", []):
        targets.append((str(f["underlying"]), f"{f['code']} · {f['name']}", "期指标的"))
    for e in wl.get("etf", []):
        targets.append((str(e["code"]), f"{e['sector']} ETF {e['code']}", "ETF"))
    # 额外关注标的（黄金/有色/油气/电池，2026-10-13 加的）也要进日报，否则模块 e 的"关注板块"是空的
    for e in wl.get("watch_extra", {}).get("etf", []):
        targets.append((str(e["code"]), f"{e.get('sector', '')} · {e['name']}（关注）", "ETF"))
    for idx in wl.get("indices", {}).get("sectors", []):
        targets.append((str(idx["code"]), f"{idx['sector']} 指数", "指数"))

    etf_to_index = {str(e["code"]): str(e.get("track_index") or "") for e in wl.get("etf", [])}

    for code, label, kind in targets:
        df = series_df(conn, code)
        if df.empty:
            d_rows.append({"label": label, "kind": kind, "code": code, "missing": True})
            continue
        last = df.iloc[-1]
        last_d = last["date"].strftime("%Y-%m-%d")
        as_of = as_of or last_d
        b = baseline_at(frozen.get(code, {}), last["date"])
        dev = (last["close"] / b - 1) * 100 if b else None
        # 贝塔一律用**日频序列**算。ETF 只有稀疏点（年末+最新），直接算出来的贝塔是无意义的，
        # 所以 ETF 改用它跟踪的指数来算（口径见技术方案 9.4）；没配跟踪指数的就留空。
        if kind == "ETF":
            ic = etf_to_index.get(code)
            if ic:
                bt, _, _ = beta_of(conn, ic)
                bt1, _, _ = beta_of(conn, ic, window=250)
            else:
                bt, bt1 = None, None
        else:
            bt, _, _ = beta_of(conn, code)
            bt1, _, _ = beta_of(conn, code, window=250)
        d_rows.append({"label": label, "kind": kind, "code": code, "close": float(last["close"]),
                       "as_of": last_d,
                       "baseline": b, "dev": dev, "slope": frozen.get(code, {}).get("slope"),
                       "r2": frozen.get(code, {}).get("r2"), "beta": bt, "beta_1y": bt1})

    # ---- b 异动（关注板块当日涨跌） ----
    moves = []
    for code, label, kind in targets:
        if kind == "期指标的":
            continue
        df = series_df(conn, code)
        if len(df) < 2:
            continue
        chg = (df.iloc[-1]["close"] / df.iloc[-2]["close"] - 1) * 100
        moves.append({"label": label, "chg": chg, "date": df.iloc[-1]["date"].strftime("%Y-%m-%d")})
    moves.sort(key=lambda m: -abs(m["chg"]))

    # ---- b 要闻 ----
    kws = sector_keywords(wl)
    since = (datetime.now() - timedelta(days=2)).isoformat(timespec="seconds")
    news = conn.execute(
        "SELECT pub_at, title, digest, tags, url FROM news WHERE pub_at >= ? ORDER BY pub_at DESC LIMIT 400", (since,)
    ).fetchall()
    rel = [n for n in news if any(k in (n[1] or "") + (n[2] or "") + (n[3] or "") for k in kws)][:12]

    # ---- a 文章 ----
    art = conn.execute("SELECT title, published, link, body FROM articles ORDER BY published DESC LIMIT 1").fetchone()

    # ---- c 研报 ----
    res = conn.execute(
        "SELECT publish_at, title, org, industry, rating, url FROM research ORDER BY publish_at DESC LIMIT 200"
    ).fetchall()
    res_rel = [r for r in res if any(k in (r[1] or "") + (r[3] or "") for k in kws)][:8]

    # ---------- 渲染 ----------
    def tr_dev(r):
        if r.get("missing"):
            return f"<tr><td>{H.escape(r['label'])}</td><td colspan=5 class='mut'>未取到数据</td></tr>"
        dev = "—" if r["dev"] is None else f"{r['dev']:+.2f}%"
        cls = "red" if (r["dev"] or 0) > 0 else "green"
        bt = "—" if r["beta"] is None else f"{r['beta']:.2f}"
        return (f"<tr><td>{H.escape(r['label'])}</td><td class='num'>{r['close']:.2f}</td>"
                f"<td class='num'>{'—' if r['baseline'] is None else round(r['baseline'], 2)}</td>"
                f"<td class='num {cls}'>{dev}</td>"
                f"<td class='num'>{'—' if r['slope'] is None else round(r['slope'], 2)}</td>"
                f"<td class='num mut'>{'—' if r['r2'] is None else r['r2']}</td>"
                f"<td class='num'>{bt}</td></tr>")

    dev_block = "\n".join(tr_dev(r) for r in d_rows)
    MOVE_TAG = '<span class="tag r">异动</span>'
    mv_block = "\n".join(
        f"<tr><td>{H.escape(m['label'])}</td>"
        f"<td class='num {'red' if m['chg'] > 0 else 'green'}'>{m['chg']:+.2f}%</td>"
        f"<td class='num mut'>{m['date']}</td>"
        f"<td>{MOVE_TAG if abs(m['chg']) >= MOVE_THRESHOLD else ''}</td></tr>"
        for m in moves
    )
    news_block = "\n".join(
        f"<li><span class='mut'>{(n[0] or '')[:16]}</span> "
        f"<a href='{H.escape(n[4] or '#')}' target='_blank' rel='noopener'><b>{H.escape(n[1] or '')}</b></a>"
        f"<span class='tag' style='margin-left:6px'>{H.escape((n[3] or '')[:24])}</span><br>"
        f"<span class='mut'>{H.escape((n[2] or '')[:150])}</span></li>"
        for n in rel
    ) or "<li class='mut'>近 48 小时没有匹配关注板块的快讯</li>"
    res_block = "\n".join(
        f"<tr><td>{(r[0] or '')[:10]}</td>"
        f"<td><a href='{H.escape(r[5] or '#')}' target='_blank' rel='noopener'>{H.escape(r[1] or '')}</a></td>"
        f"<td>{H.escape(r[2] or '')}</td><td>{H.escape(r[3] or '')}</td><td>{H.escape(r[4] or '')}</td></tr>"
        for r in res_rel
    ) or "<tr><td colspan=5 class='mut'>没有匹配关注板块的研报</td></tr>"

    if art:
        art_html = (f"<div class='art'><b>{H.escape(art[0])}</b><br><span class='mut'>发布日期："
                    f"{H.escape(str(art[1])[:19])}</span><br><br>"
                    f"{H.escape(art[3]) if art[3] else '<span class=mut>（正文未获取到，见下方说明）</span>'}</div>"
                    f"<div class='foot'>原文链接：<a href='{H.escape(art[2] or '#')}' target='_blank' rel='noopener'>{H.escape(art[2] or '')}</a>　"
                    f"<b>注意：RSS 有 0–24h 延迟，标题与日期以此处标注为准；正文需在微信内查看。</b></div>")
    else:
        art_html = "<div class='warn'>今日未获取到文章：RSS 无新条目或抓取失败。请手动补一次链接。</div>"

    advice_html, advice_stats = build_advice(conn, wl, d_rows, moves, rel, res_rel)

    body = f"""
<div class="card"><h2>a · 猫笔刀文章 <span class="tag">原文原样</span></h2>{art_html}</div>
<div class="card"><h2>b · 关注板块异动 <span class="tag r">阈值 ±{MOVE_THRESHOLD}%</span>
<span class="tag a">口径说明：当前只覆盖清单内 8 板块 + 8 指数，申万一级全市场扫描待接入</span></h2>
<table><tr><th>板块/标的</th><th class="num">当日涨跌</th><th class="num">日期</th><th>标记</th></tr>{mv_block}</table></div>
<div class="card"><h2>b · 要闻 <span class="tag">按关注板块过滤</span></h2><ul>{news_block}</ul></div>
<div class="card"><h2>c · 研报 <span class="tag">按关注板块过滤</span></h2>
<table><tr><th>日期</th><th>标题</th><th>机构</th><th>行业</th><th>评级</th></tr>{res_block}</table></div>
<div class="card"><h2>d · 当前点位、基线与贝塔 <span class="tag a">基线为冻结参数</span></h2>
<table><tr><th>标的</th><th class="num">最新</th><th class="num">基线值</th><th class="num">偏离</th>
<th class="num">斜率/年</th><th class="num">R²</th><th class="num">贝塔(vs中证500)</th></tr>{dev_block}</table>
<div class="foot">基线是<b>冻结</b>的线性线，不随新数据重拟合。R² 越小说明这条线越没有解释力，偏离要谨慎解读。
贝塔用<b>指数日频收益</b>算（ETF 目前只有稀疏点，算不了日频贝塔，故用它跟踪的指数）。</div></div>
{advice_html}
"""
    # 顶部「数据截至」用**全部标的里最新**的那天，并把落后的标的名列出来 ——
    # 之前用的是第一行的日期，某个标的一滞后就会把整份日报的截止日拖旧（实测拖了 3 个交易日）。
    all_dates = [r["as_of"] for r in d_rows if r.get("as_of")]
    newest = max(all_dates) if all_dates else None
    stale = sorted({r["label"] for r in d_rows if r.get("as_of") and r["as_of"] < newest})
    sub = f"数据截至 {newest or '—'}（上一交易日收盘）· 生成于 {datetime.now():%Y-%m-%d %H:%M}"
    if stale:
        sub += f"<br><b>注意：以下标的数据较旧</b>（{min(all_dates)}）：" + "、".join(stale[:8])
    html = PAGE.format(date=date_str, subtitle=sub, body=body)
    meta = {"as_of": as_of, "dev_rows": len(d_rows), "moves": len(moves),
            "news_relevant": len(rel), "research_relevant": len(res_rel), "has_article": bool(art),
            **advice_stats}
    return html, meta


def main() -> int:
    ap = argparse.ArgumentParser(prog="report")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("daily")
    p.add_argument("--date", default=datetime.now().strftime("%Y-%m-%d"))
    p.add_argument("--no-archive", action="store_true")
    args = ap.parse_args()

    conn = connect()
    try:
        html, meta = build(conn, args.date)
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        path = OUT_DIR / f"{args.date}.html"
        path.write_text(html, encoding="utf-8")
        if not args.no_archive:
            save_report(conn, args.date, "daily", str(path), html, meta, datetime.now().isoformat(timespec="seconds"))
        print(json.dumps({"ok": True, "data": {"path": str(path), "bytes": len(html), "archived": not args.no_archive, **meta}},
                         ensure_ascii=False))
        return 0
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": {"type": type(e).__name__, "message": str(e)}}, ensure_ascii=False))
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
