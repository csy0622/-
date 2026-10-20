#!/usr/bin/env python3
"""投资助手 3.0 — 资讯抓取层（同花顺快讯 / 东财研报 / 猫笔刀文章）。

三个源都是**只读公开接口**，不需要登录、不需要浏览器自动化：
  同花顺快讯：news.10jqka.com.cn/tapp/news/push/stock/   → 干净 JSON（页面本身是 JS 渲染的）
  东财研报：  reportapi.eastmoney.com/report/list         → JSON，qType=0 个股 / 1 行业
  猫笔刀文章：wechat2rss 公开 feed（主）+ maobidao.net（交叉验证）

保留窗口：快讯与研报默认只留 **90 天**（他要的是"可短不可长"，避免存储膨胀与信息过远）；
文章不限窗口（要长期沉淀，防原文被删）。

用法：
    python3 news.py sync [--days 90]        # 抓快讯+研报，入库并清理超期
    python3 news.py article                 # 抓猫笔刀最新，入库（交叉验证备份站）
    python3 news.py latest-news [--kw 光伏] [--limit 20] [--days 3]
    python3 news.py latest-research [--kw 银行] [--limit 10] [--days 90]
    python3 news.py stats
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from store import add_message, connect  # noqa: E402  (add_message 暂未用，保持导入面稳定)

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122 Safari/537.36"
# 取微信文章页时用这个 UA（带 MicroMessenger 更稳）
UA_WX = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 MicroMessenger/8.0.49"
TIMEOUT = 30
RETENTION_DAYS = 90
WECHAT_RSS = "https://wechat2rss.xlab.app/feed/33d986064f59be5263de2ca822fb3e0bdd59eb81.xml"
MAOBIDAO_BACKUP = "https://www.maobidao.net/"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _strip_html(s: str) -> str:
    s = re.sub(r"<br\s*/?>", "\n", s or "")
    s = re.sub(r"<[^>]+>", "", s)
    return html.unescape(s).strip()


# ------------------------------------------------------------------ 同花顺快讯

def _tag_str(tags) -> str:
    """同花顺的 tags 是 list[dict]（实测），不是 list[str] —— 直接 join 会 TypeError。"""
    if not tags:
        return ""
    if isinstance(tags, str):
        return tags
    out = []
    for t in tags if isinstance(tags, list) else [tags]:
        if isinstance(t, dict):
            out.append(str(t.get("name") or t.get("tag") or t.get("title") or t.get("id") or ""))
        else:
            out.append(str(t))
    return ",".join(x for x in out if x)


def _stock_str(stock) -> str:
    """关联股票同样是 list[dict]，只留代码与名称，避免整块 JSON 塞进库里。"""
    if not stock:
        return ""
    if not isinstance(stock, list):
        return str(stock)[:300]
    out = []
    for s in stock:
        if isinstance(s, dict):
            code = s.get("code") or s.get("stockCode") or ""
            name = s.get("name") or s.get("stockName") or ""
            out.append(f"{code}{name}".strip() or json.dumps(s, ensure_ascii=False)[:40])
        else:
            out.append(str(s))
    return ",".join(x for x in out if x)[:500]


def fetch_ths_news(days: int) -> list[dict]:
    """分页拉到超过窗口为止。pagesize 实测 200 可用。"""
    cutoff = datetime.now() - timedelta(days=days)
    out, page = [], 1
    while page <= 40:  # 硬上限，避免无限翻页
        url = f"https://news.10jqka.com.cn/tapp/news/push/stock/?page={page}&pagesize=200"
        r = requests.get(url, headers={"User-Agent": UA, "Referer": "https://news.10jqka.com.cn/realtimenews.html"},
                         timeout=TIMEOUT)
        r.raise_for_status()
        items = (r.json().get("data") or {}).get("list") or []
        if not items:
            break
        stop = False
        for it in items:
            ts = it.get("ctime")
            try:
                dt = datetime.fromtimestamp(int(ts))
            except (TypeError, ValueError):
                continue
            if dt < cutoff:
                stop = True
                continue
            out.append({
                "id": str(it.get("id") or f"{ts}-{it.get('title','')[:20]}"),
                "pub_at": dt.isoformat(timespec="seconds"),
                "title": it.get("title") or "",
                "digest": _strip_html(it.get("digest") or ""),
                "tags": _tag_str(it.get("tags")),
                "stocks": _stock_str(it.get("stock")),
                "url": it.get("url") or it.get("appUrl") or "",
                "source": it.get("source") or "同花顺",
            })
        if stop:
            break
        page += 1
    return out


# ------------------------------------------------------------------ 东财研报

def fetch_em_research(days: int, qtype: int) -> list[dict]:
    end = datetime.now().date()
    begin = end - timedelta(days=days)
    url = ("https://reportapi.eastmoney.com/report/list"
           f"?pageSize=100&pageNo=1&qType={qtype}"
           f"&beginTime={begin}&endTime={end}&fields=&_={int(datetime.now().timestamp() * 1000)}")
    r = requests.get(url, headers={"User-Agent": UA, "Referer": "https://data.eastmoney.com/report/"}, timeout=TIMEOUT)
    r.raise_for_status()
    data = r.json().get("data") or []
    out = []
    for it in data:
        code = it.get("infoCode")
        if not code:
            continue
        out.append({
            "info_code": str(code),
            "title": it.get("title") or "",
            "org": it.get("orgSName") or it.get("orgName") or "",
            "industry": it.get("indvInduName") or "",
            "rating": it.get("emRatingName") or "",
            "stock_name": it.get("stockName") or "",
            "stock_code": it.get("stockCode") or "",
            "publish_at": (it.get("publishDate") or "")[:19],
            "url": ("https://data.eastmoney.com/report/info/" + str(code)) if code else "",
            "qtype": qtype,
        })
    return out


# ------------------------------------------------------------------ 猫笔刀文章

def extract_body(text: str) -> str:
    """从已经取回的 HTML 里抽正文。**不要在函数内再发请求** ——
    实测同一链接连续两次请求，第二次会被限流返回 JS 壳（正文取不到）。"""
    i = text.find('id="js_content"')
    if i < 0:
        return ""                     # JS 壳 / 验证页，正文不在服务端返回里
    k = text.find(">", i)             # 跳过起始标签残片（id="js_content" style="..."）
    seg = text[k + 1 : k + 1 + 900_000] if k > 0 else text[i : i + 900_000]
    j = seg.find("<script")           # 正文容器之后通常紧跟 script，用它当右边界（比非贪婪匹嵌套 div 稳）
    if j > 0:
        seg = seg[:j]
    seg = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", seg, flags=re.S)
    seg = re.sub(r"<br\s*/?>", "\n", seg)
    seg = re.sub(r"</(p|div|section|li|h[1-6])\s*>", "\n", seg)
    seg = re.sub(r"<[^>]+>", "", seg)
    txt = html.unescape(seg)
    txt = re.sub(r"[ \t\u00a0]+", " ", txt)
    txt = re.sub(r"\n\s*\n+", "\n", txt)
    return txt.strip()


def fetch_article_body(link: str) -> str:
    """单独取一次文章页并抽正文（调用方若已有 HTML，请直接用 extract_body）。"""
    if not link:
        return ""
    r = requests.get(link, headers={"User-Agent": UA_WX}, timeout=TIMEOUT)
    r.raise_for_status()
    return extract_body(r.text)


def _page_title(text: str) -> str:
    for pat in (r'<meta[^>]+property="og:title"[^>]+content="([^"]+)"',
                r'<meta[^>]+content="([^"]+)"[^>]+property="og:title"',
                r'var msg_title = [\'"]([^\'"]+)[\'"]',
                r'<title>([^<]*)</title>'):
        m = re.search(pat, text)
        if m and m.group(1).strip():
            return html.unescape(m.group(1).strip())
    return ""


def fetch_by_link(link: str) -> dict:
    """直接用用户给的链接抓一篇（RSS 长链拿不到正文时的兜底路径）。**只发一次请求。**"""
    r = requests.get(link, headers={"User-Agent": UA_WX}, timeout=TIMEOUT)
    r.raise_for_status()
    text = r.text
    body = extract_body(text)
    title = _page_title(text)
    pub = ""
    m = re.search(r'var ct = "(\d+)"', text) or re.search(r'"publish_time"\s*:\s*"?(\d{10})', text)
    if m:
        pub = datetime.fromtimestamp(int(m.group(1))).isoformat(timespec="seconds")
    return {"link": link, "account": "猫笔刀", "title": title, "published": pub, "body": body,
            "source": "manual-link", "backup_title": None, "rss_item_count": 0,
            "body_error": None if len(body) >= 50 else "该链接取不到正文（是 JS 壳），换 /s/<token> 短链再试",
            "cross_checked": False}


RSS_CONTENT_NS = "{http://purl.org/rss/1.0/modules/content/}encoded"


def _html_to_text(h: str) -> str:
    """把 RSS 里的全文 HTML 转成干净文本。图片丢弃，避免报告依赖第三方图床。"""
    h = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", h or "", flags=re.S)
    h = re.sub(r"<img[^>]*>", "", h)
    h = re.sub(r"<br\s*/?>", "\n", h)
    h = re.sub(r"</(p|div|section|li|h[1-6]|blockquote)\s*>", "\n", h)
    h = re.sub(r"<[^>]+>", "", h)
    t = html.unescape(h)
    t = re.sub(r"[ \t\u00a0]+", " ", t)
    t = re.sub(r"\n\s*\n+", "\n", t)
    # 去掉 RSS 里夹带的微信 UI 文案（实测会混在正文尾部）
    t = re.sub(r"\n?\s*(跳转微信打开|在小说阅读器读本章|去阅读|在公众号小说中沉浸阅读)\s*\n?", "\n", t)
    return t.strip()


MAOBIDAO_API = ("https://www.maobidao.net/index.php?rest_route=/wp/v2/posts"
                "&categories=1&per_page=5&orderby=date&order=desc&_fields=title,date,link,content")
# 分页版：实测 236 篇 / 3 页 / 每页 100 —— 这是拿历史文章的链路
MAOBIDAO_API_PAGED = ("https://www.maobidao.net/index.php?rest_route=/wp/v2/posts"
                      "&categories=1&orderby=date&order=desc&per_page=100&page={page}"
                      "&_fields=title,date,link,content")


def fetch_maobidao_latest() -> dict | None:
    """备份站 maobidao.net 的 WordPress REST API（categories=1 是"公众号内容备份"）。

    用途：RSS 万一停更/限流时的**第二份独立备份**（他要"防删"，两条互不依赖的源才叫防删）。
    备份正文里还内嵌带 `chksm` 的 mp 原始链接 —— 那个链接能直连拿到真原文 HTML。
    """
    r = requests.get(MAOBIDAO_API, headers={"User-Agent": UA}, timeout=TIMEOUT)
    r.raise_for_status()
    posts = r.json()
    if not posts:
        return None
    p = posts[0]
    title = html.unescape(re.sub(r"<[^>]+>", "", p["title"]["rendered"])).strip()
    raw = p["content"]["rendered"]
    body = _html_to_text(raw)
    # 优先用备份里内嵌的 mp 原文链接（带 chksm，直连可取真原文）
    m = re.search(r'https://mp\.weixin\.qq\.com/s\?[^"\'\s<]*chksm=[^"\'\s<]*', raw)
    link = html.unescape(m.group(0)) if m else p["link"]
    return {"link": link, "account": "猫笔刀", "title": title,
            "published": (p.get("date") or "")[:19], "body": body, "source": "maobidao",
            "backup_title": None, "rss_item_count": 0, "body_error": None if len(body) >= 50 else "备份站也没正文",
            "cross_checked": False}


def fetch_article() -> dict | None:
    """RSS 主源；再用备份站交叉验证（备份站拿到的标题作为佐证）。"""
    r = requests.get(WECHAT_RSS, headers={"User-Agent": UA}, timeout=TIMEOUT)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    items = root.findall(".//item")
    if not items:
        return None
    it = items[0]  # feed 按时间倒序，第一条即最新
    title = (it.findtext("title") or "").strip()
    link = (it.findtext("link") or "").strip()
    pub = (it.findtext("pubDate") or "").strip()
    try:
        published = datetime.strptime(pub, "%a, %d %b %Y %H:%M:%S %z").isoformat(timespec="seconds")
    except ValueError:
        published = pub

    # ⭐ RSS 的 content:encoded 里**就是全文 HTML**（实测）。
    # 之前误读成 description（那是空的）才以为"RSS 不带正文"。
    # 这条让全流程可以全自动：RSS 一次请求同时拿到 标题+日期+链接+正文，不需要登录/浏览器/桌面操作。
    body = _html_to_text(it.findtext(RSS_CONTENT_NS) or "")
    body_error = None
    if len(body) < 200:
        # 兜底：RSS 万一不带给全文，就去文章页取（注意只有短链 /s/<token> 能拿到）
        try:
            body = fetch_article_body(link)
        except Exception as e:  # noqa: BLE001
            body_error = f"{type(e).__name__}: {e}"
        if len(body) < 50:
            body_error = body_error or "RSS 与文章页都取不到正文"

    # 第二份独立备份：RSS 取不到正文时，改从备份站拿（两条源互不依赖）
    if len(body) < 200:
        try:
            alt = fetch_maobidao_latest()
            if alt and len(alt["body"]) >= 200:
                alt["note"] = "正文来自备份站 maobidao.net（RSS 未取到）"
                return alt
        except Exception as e:  # noqa: BLE001
            body_error = (body_error or "") + f" | 备份站也失败：{type(e).__name__}"

    backup_title = None
    try:
        b = requests.get(MAOBIDAO_BACKUP, headers={"User-Agent": UA}, timeout=TIMEOUT)
        m = re.search(r'<h2[^>]*class="[^"]*entry-title[^"]*"[^>]*>\s*<a[^>]*>([^<]+)</a>', b.text)
        backup_title = html.unescape(m.group(1)).strip() if m else None
    except Exception:  # noqa: BLE001
        pass

    # 只有备份站确实出现同一篇文章时才算交叉验证通过
    cross = bool(backup_title and title and (title[:6] in backup_title or backup_title[:6] in title))
    return {"link": link, "account": "猫笔刀", "title": title, "published": published, "body": body,
            "source": "rss", "backup_title": backup_title, "rss_item_count": len(items),
            "body_error": body_error, "cross_checked": cross}


# ------------------------------------------------------------------ 入库

def sync(conn, days: int) -> dict:
    n_news = n_res = 0
    for it in fetch_ths_news(days):
        conn.execute(
            "INSERT INTO news(id,pub_at,title,digest,tags,stocks,url,source,fetched_at) "
            "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING",
            (it["id"], it["pub_at"], it["title"], it["digest"], it["tags"], it["stocks"], it["url"], it["source"], _now()),
        )
        n_news += 1
    for qt in (0, 1):
        for it in fetch_em_research(days, qt):
            conn.execute(
                "INSERT INTO research(info_code,title,org,industry,rating,stock_name,stock_code,publish_at,url,qtype,fetched_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(info_code) DO NOTHING",
                (it["info_code"], it["title"], it["org"], it["industry"], it["rating"], it["stock_name"],
                 it["stock_code"], it["publish_at"], it["url"], it["qtype"], _now()),
            )
            n_res += 1
    conn.commit()
    # 清理超期（文章不清理）
    cutoff = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
    c1 = conn.execute("DELETE FROM news WHERE pub_at < ?", (cutoff,)).rowcount
    c2 = conn.execute("DELETE FROM research WHERE publish_at < ?", (cutoff,)).rowcount
    conn.commit()
    return {"news_upserted": n_news, "research_upserted": n_res, "purged_news": c1, "purged_research": c2,
            "window_days": days}


def save_article(conn, art: dict) -> None:
    # published 统一成 YYYY-MM-DD：两个源的时刻格式不同（RSS 带 +08:00、备份站不带），
    # 不归一化就跨源去不掉重（唯一键是 account+published+title）。
    pub = (str(art.get("published") or "")[:10]) or None
    conn.execute(
        "INSERT OR IGNORE INTO articles(link,account,title,published,body,source,fetched_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (art["link"], art["account"], art["title"], pub, art["body"], art["source"], _now()),
    )
    conn.commit()


def fetch_article_all() -> list[dict]:
    """RSS 里通常有 ~20 篇，**全部**取回（之前只取第一篇，才导致"只有一天"）。"""
    r = requests.get(WECHAT_RSS, headers={"User-Agent": UA}, timeout=TIMEOUT)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    out = []
    for it in root.findall(".//item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        pub = (it.findtext("pubDate") or "").strip()
        body = _html_to_text(it.findtext(RSS_CONTENT_NS) or "")
        if not title:
            continue
        try:
            published = datetime.strptime(pub, "%a, %d %b %Y %H:%M:%S %z").isoformat(timespec="seconds")
        except ValueError:
            published = pub
        out.append({"link": link, "account": "猫笔刀", "title": title, "published": published,
                    "body": body, "source": "rss", "backup_title": None, "rss_item_count": len(out) + 1,
                    "body_error": None if len(body) >= 50 else "RSS 该篇未带全文",
                    "cross_checked": False})
    return out


def _maobidao_post_to_art(p: dict) -> dict:
    title = html.unescape(re.sub(r"<[^>]+>", "", p["title"]["rendered"])).strip()
    raw = p["content"]["rendered"]
    body = _html_to_text(raw)
    # 优先用备份里内嵌的 mp 原文链接（带 chksm，直连可取真原文）
    m = re.search(r'https://mp\.weixin\.qq\.com/s\?[^"\'\s<]*chksm=[^"\'\s<]*', raw)
    link = html.unescape(m.group(0)) if m else p["link"]
    return {"link": link, "account": "猫笔刀", "title": title,
            "published": (p.get("date") or "")[:19], "body": body, "source": "maobidao",
            "backup_title": None, "rss_item_count": 0,
            "body_error": None if len(body) >= 50 else "备份站该篇也没正文", "cross_checked": False}


def fetch_maobidao_latest() -> dict | None:
    """备份站最新一篇（RSS 取不到正文时的第二独立源）。"""
    r = requests.get(MAOBIDAO_API, headers={"User-Agent": UA}, timeout=TIMEOUT)
    r.raise_for_status()
    posts = r.json()
    return _maobidao_post_to_art(posts[0]) if posts else None


def backfill_maobidao(pages: int = 3) -> list[dict]:
    """翻页拉备份站的历史文章（实测 236 篇 / 3 页 / 每页 100）。

    这是拿"其他日期文章"的链路 —— 单篇文章链接只能拿到那一篇。
    """
    out = []
    for pg in range(1, pages + 1):
        url = (MAOBIDAO_API_PAGED.format(page=pg))
        r = requests.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT)
        r.raise_for_status()
        posts = r.json()
        if not isinstance(posts, list) or not posts:
            break
        for p in posts:
            out.append(_maobidao_post_to_art(p))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(prog="news")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("sync", "latest-news", "latest-research"):
        p = sub.add_parser(name)
        p.add_argument("--days", type=int, default=RETENTION_DAYS)
        p.add_argument("--kw", default="")
        p.add_argument("--limit", type=int, default=20)
    art_p = sub.add_parser("article")
    art_p.add_argument("--link", default="", help="直接用这个文章链接抓（RSS 长链拿不到正文时用）")
    art_p.add_argument("--all", action="store_true", help="把 RSS 全部条目（约 20 篇）都存下来")
    bf_p = sub.add_parser("backfill")
    bf_p.add_argument("--pages", type=int, default=3, help="备份站翻几页，每页 100 篇（实测共 3 页 236 篇）")
    sub.add_parser("stats")
    args = ap.parse_args()

    conn = connect()
    try:
        if args.cmd == "sync":
            print(json.dumps({"ok": True, "data": sync(conn, args.days)}, ensure_ascii=False))
        elif args.cmd == "article":
            if getattr(args, "link", ""):
                art = fetch_by_link(args.link)
            elif getattr(args, "all", False):
                arts = fetch_article_all()
                for a in arts:
                    save_article(conn, a)
                withbody = sum(1 for a in arts if len(a["body"]) >= 200)
                print(json.dumps({"ok": True, "data": {
                    "source": "rss 全部条目", "stored": len(arts), "with_body": withbody,
                    "newest": f"{arts[0]['title']}（{arts[0]['published'][:10]}）" if arts else None,
                    "oldest": f"{arts[-1]['title']}（{arts[-1]['published'][:10]}）" if arts else None,
                }}, ensure_ascii=False))
                return 0
            else:
                art = fetch_article()
            if not art:
                print(json.dumps({"ok": False, "error": {"message": "RSS 里没有条目"}}, ensure_ascii=False))
                return 1
            save_article(conn, art)
            ok = bool(art["backup_title"] and art["title"][:8] in art["backup_title"] or art["backup_title"])
            print(json.dumps({"ok": True, "data": {
                "title": art["title"], "published": art["published"], "link": art["link"],
                "body_chars": len(art["body"]), "backup_title": art["backup_title"],
                "rss_items": art["rss_item_count"], "cross_checked": art["cross_checked"],
                "body_error": art.get("body_error"),
                "note": "RSS 有 0–24h 延迟；日报里必须显式标注文章发布日期",
            }}, ensure_ascii=False))
        elif args.cmd == "backfill":
            arts = backfill_maobidao(args.pages)
            for a in arts:
                save_article(conn, a)
            withbody = sum(1 for a in arts if len(a["body"]) >= 200)
            dates = sorted(a["published"][:10] for a in arts if a.get("published"))
            print(json.dumps({"ok": True, "data": {
                "source": "maobidao.net 备份站", "fetched": len(arts), "with_body": withbody,
                "date_range": [dates[0], dates[-1]] if dates else [],
                "note": "这是拿历史文章的链路；单篇文章链接只能拿到那一篇",
            }}, ensure_ascii=False))
        elif args.cmd == "latest-news":
            sql = "SELECT pub_at,title,digest,tags,url FROM news WHERE pub_at >= ?"
            params = [(datetime.now() - timedelta(days=args.days)).isoformat(timespec="seconds")]
            if args.kw:
                sql += " AND (title LIKE ? OR digest LIKE ? OR tags LIKE ?)"
                params += [f"%{args.kw}%"] * 3
            sql += " ORDER BY pub_at DESC LIMIT ?"
            params.append(args.limit)
            rows = conn.execute(sql, params).fetchall()
            print(json.dumps({"ok": True, "data": {"count": len(rows), "items": [
                {"pub_at": r[0], "title": r[1], "digest": (r[2] or "")[:200], "tags": r[3], "url": r[4]} for r in rows]}},
                ensure_ascii=False))
        elif args.cmd == "latest-research":
            sql = "SELECT publish_at,title,org,industry,rating,url FROM research WHERE publish_at >= ?"
            params = [(datetime.now() - timedelta(days=args.days)).isoformat(timespec="seconds")]
            if args.kw:
                sql += " AND (title LIKE ? OR industry LIKE ? OR stock_name LIKE ?)"
                params += [f"%{args.kw}%"] * 3
            sql += " ORDER BY publish_at DESC LIMIT ?"
            params.append(args.limit)
            rows = conn.execute(sql, params).fetchall()
            print(json.dumps({"ok": True, "data": {"count": len(rows), "items": [
                {"publish_at": r[0], "title": r[1], "org": r[2], "industry": r[3], "rating": r[4], "url": r[5]}
                for r in rows]}}, ensure_ascii=False))
        elif args.cmd == "stats":
            def one(sql):
                return conn.execute(sql).fetchone()[0]
            print(json.dumps({"ok": True, "data": {
                "news": one("SELECT COUNT(*) FROM news"),
                "news_oldest": conn.execute("SELECT MIN(pub_at) FROM news").fetchone()[0],
                "research": one("SELECT COUNT(*) FROM research"),
                "research_oldest": conn.execute("SELECT MIN(publish_at) FROM research").fetchone()[0],
                "articles": one("SELECT COUNT(*) FROM articles"),
            }}, ensure_ascii=False))
        return 0
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": {"type": type(e).__name__, "message": str(e)}}, ensure_ascii=False))
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
