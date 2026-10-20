#!/usr/bin/env python3
"""投资助手 3.0 — sqlite 存储层（单文件）。

四张表，正好覆盖四件事：
  messages  — 对话消息（每条 user/assistant/tool 一行）
  runs      — 回测 run（run_id 主键，可复核）
  series    — 行情序列（ETF/指数/期指标的点位）
  reports   — 晨报存档（每天一份）

设计取舍：
  行情放 sqlite 而不是只有 parquet —— 他要的是「一个地方能查全部」；
  点位级的数据量很小（年末 + 最新，几十到几百行/标的），sqlite 完全扛得住，
  而且能直接用 SQL 做「最近一次基线偏离」「某标的的历史」这类查询。
  纯配置仍留 YAML（要人能直接改）。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
DB_PATH = HERE.parent / "data" / "assistant.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT    NOT NULL,
  role       TEXT    NOT NULL,          -- user / assistant / tool
  content    TEXT,
  tool_name  TEXT,                      -- role=tool 时记录工具名
  created_at TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);

CREATE TABLE IF NOT EXISTS runs (
  run_id       TEXT PRIMARY KEY,
  strategy     TEXT,
  name         TEXT,
  period_start TEXT,
  period_end   TEXT,
  metrics_json TEXT,
  extra_json   TEXT,
  trade_count  INTEGER,
  artifact     TEXT,
  created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS series (
  code        TEXT NOT NULL,
  price_basis TEXT NOT NULL,
  date        TEXT NOT NULL,
  close       REAL NOT NULL,
  PRIMARY KEY (code, price_basis, date)
);

CREATE TABLE IF NOT EXISTS series_meta (
  code        TEXT NOT NULL,
  price_basis TEXT NOT NULL,
  label       TEXT,
  source      TEXT,
  first_date  TEXT,
  last_date   TEXT,
  points      INTEGER,
  updated_at  TEXT NOT NULL,
  PRIMARY KEY (code, price_basis)
);

CREATE TABLE IF NOT EXISTS reports (
  report_date   TEXT NOT NULL,          -- YYYY-MM-DD
  kind          TEXT NOT NULL,          -- daily / review
  path          TEXT,
  html          TEXT,
  sections_json TEXT,
  generated_at  TEXT,
  created_at    TEXT NOT NULL,
  PRIMARY KEY (report_date, kind)
);

-- 快讯（同花顺）：全量入库、日报只显示相关的。保留窗口靠 cleanup 控制（默认 90 天）
CREATE TABLE IF NOT EXISTS news (
  id         TEXT PRIMARY KEY,          -- 源站 id，去重键
  pub_at     TEXT,                      -- 发布时间（源站时间戳转 ISO）
  title      TEXT,
  digest     TEXT,
  tags       TEXT,
  stocks     TEXT,                      -- 关联股票
  url        TEXT,
  source     TEXT,
  fetched_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_news_pub ON news(pub_at DESC);

-- 研报（东方财富）。叫 research 以免和上面的「晨报存档 reports」混淆
CREATE TABLE IF NOT EXISTS research (
  info_code  TEXT PRIMARY KEY,          -- infoCode，去重键
  title      TEXT,
  org        TEXT,
  industry   TEXT,
  rating     TEXT,
  stock_name TEXT,
  stock_code TEXT,
  publish_at TEXT,
  url        TEXT,
  qtype      INTEGER,                   -- 0 个股 / 1 行业
  fetched_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_research_pub ON research(publish_at DESC);

-- 猫笔刀文章存档：不限窗口（要长期沉淀防删）
CREATE TABLE IF NOT EXISTS articles (
  link       TEXT PRIMARY KEY,
  account    TEXT,
  title      TEXT,
  published  TEXT,
  body       TEXT,
  source     TEXT,                      -- rss / maobidao
  fetched_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_articles_pub ON articles(published DESC);

-- 跨源去重：**按标题**而不是日期 —— 实测备份站的 date 是"备份发布日"，
-- 比文章实际发布日晚约一天，用日期做键去不掉重（同一篇会存两份）。
CREATE UNIQUE INDEX IF NOT EXISTS idx_articles_key ON articles(account, title);
"""


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ------------------------------------------------------------------ 写入

def save_series(conn, code: str, label: str, price_basis: str, source: str, points: list[tuple[str, float]]) -> int:
    """points: [(date_str, close), ...]"""
    if not points:
        return 0
    conn.executemany(
        "INSERT INTO series(code, price_basis, date, close) VALUES(?,?,?,?) "
        "ON CONFLICT(code, price_basis, date) DO UPDATE SET close=excluded.close",
        [(code, price_basis, d, float(c)) for d, c in points],
    )
    dates = sorted(d for d, _ in points)
    conn.execute(
        "INSERT INTO series_meta(code, price_basis, label, source, first_date, last_date, points, updated_at) "
        "VALUES(?,?,?,?,?,?,?,?) "
        "ON CONFLICT(code, price_basis) DO UPDATE SET label=excluded.label, source=excluded.source, "
        "first_date=excluded.first_date, last_date=excluded.last_date, points=excluded.points, updated_at=excluded.updated_at",
        (code, price_basis, label, source, dates[0], dates[-1], len(points), _now()),
    )
    conn.commit()
    return len(points)


def save_run(conn, d: dict) -> None:
    period = d.get("period") or []
    conn.execute(
        "INSERT INTO runs(run_id, strategy, name, period_start, period_end, metrics_json, extra_json, "
        "trade_count, artifact, created_at) VALUES(?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(run_id) DO UPDATE SET metrics_json=excluded.metrics_json, extra_json=excluded.extra_json",
        (
            d.get("run_id"), d.get("strategy_file"), d.get("name"),
            period[0] if period else None, period[1] if len(period) > 1 else None,
            json.dumps(d.get("metrics"), ensure_ascii=False),
            json.dumps(d.get("extra"), ensure_ascii=False),
            d.get("trade_count"), d.get("artifact"), _now(),
        ),
    )
    conn.commit()


def add_message(conn, session_id: str, role: str, content: str, tool_name: str | None = None) -> None:
    conn.execute(
        "INSERT INTO messages(session_id, role, content, tool_name, created_at) VALUES(?,?,?,?,?)",
        (session_id, role, content, tool_name, _now()),
    )
    conn.commit()


def save_report(conn, report_date: str, kind: str, path: str | None, html: str | None,
                sections: dict | None, generated_at: str | None) -> None:
    conn.execute(
        "INSERT INTO reports(report_date, kind, path, html, sections_json, generated_at, created_at) "
        "VALUES(?,?,?,?,?,?,?) "
        "ON CONFLICT(report_date, kind) DO UPDATE SET path=excluded.path, html=excluded.html, "
        "sections_json=excluded.sections_json, generated_at=excluded.generated_at",
        (report_date, kind, path, html, json.dumps(sections, ensure_ascii=False) if sections else None,
         generated_at, _now()),
    )
    conn.commit()


# ------------------------------------------------------------------ CLI

def _stdin_json() -> dict:
    raw = sys.stdin.read().strip()
    return json.loads(raw) if raw else {}


def main() -> int:
    ap = argparse.ArgumentParser(prog="store")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    sub.add_parser("stats")
    sess_p = sub.add_parser("sessions")          # 列出会话（给 H5 的历史切换用）
    sub.add_parser("reports")                    # 列出晨报（给 H5 的晨报切换用）
    msg_p = sub.add_parser("messages")           # 取某个会话的消息
    msg_p.add_argument("--session", required=True)
    msg_p.add_argument("--limit", type=int, default=200)
    sub.add_parser("add-message")     # JSON on stdin: {session_id, role, content, tool_name?}
    sub.add_parser("archive-report")  # JSON on stdin: {report_date, kind, path?, html?, sections?, generated_at?}
    args = ap.parse_args()

    conn = connect()
    try:
        if args.cmd == "init":
            print(json.dumps({"ok": True, "data": {"db": str(DB_PATH)}}, ensure_ascii=False))
        elif args.cmd == "stats":
            def one(sql):
                return conn.execute(sql).fetchone()[0]
            data = {
                "db": str(DB_PATH),
                "size_kb": round(DB_PATH.stat().st_size / 1024, 1) if DB_PATH.exists() else 0,
                "messages": one("SELECT COUNT(*) FROM messages"),
                "sessions": one("SELECT COUNT(DISTINCT session_id) FROM messages"),
                "runs": one("SELECT COUNT(*) FROM runs"),
                "series_rows": one("SELECT COUNT(*) FROM series"),
                "series_instruments": one("SELECT COUNT(*) FROM series_meta"),
                "reports": one("SELECT COUNT(*) FROM reports"),
            }
            print(json.dumps({"ok": True, "data": data}, ensure_ascii=False))
        elif args.cmd == "sessions":
            rows = conn.execute(
                "SELECT session_id, MIN(created_at), MAX(created_at), COUNT(*) FROM messages "
                "GROUP BY session_id ORDER BY MAX(created_at) DESC").fetchall()
            out = []
            for sid, first, last, n in rows:
                prev = conn.execute(
                    "SELECT content FROM messages WHERE session_id=? AND role='user' ORDER BY id LIMIT 1",
                    (sid,)).fetchone()
                out.append({"session_id": sid, "first": first, "last": last, "count": n,
                            "preview": ((prev[0] if prev else "") or "").replace("\n", " ")[:44]})
            print(json.dumps({"ok": True, "data": {"sessions": out}}, ensure_ascii=False))
        elif args.cmd == "reports":
            rows = conn.execute(
                "SELECT report_date, kind, path, generated_at, LENGTH(COALESCE(html,'')) FROM reports "
                "ORDER BY report_date DESC, kind").fetchall()
            print(json.dumps({"ok": True, "data": {"reports": [
                {"report_date": r[0], "kind": r[1], "path": r[2], "generated_at": r[3], "html_bytes": r[4]}
                for r in rows]}}, ensure_ascii=False))
        elif args.cmd == "messages":
            rows = conn.execute(
                "SELECT role, content, tool_name, created_at FROM messages WHERE session_id=? "
                "ORDER BY id LIMIT ?", (args.session, args.limit)).fetchall()
            print(json.dumps({"ok": True, "data": {"session_id": args.session, "messages": [
                {"role": r[0], "content": r[1], "tool_name": r[2], "created_at": r[3]} for r in rows]}},
                ensure_ascii=False))
        elif args.cmd == "add-message":
            d = _stdin_json()
            add_message(conn, str(d.get("session_id") or "unknown"), str(d.get("role") or "unknown"),
                        str(d.get("content") or ""), d.get("tool_name"))
            print(json.dumps({"ok": True, "data": {"stored": True}}, ensure_ascii=False))
        elif args.cmd == "archive-report":
            d = _stdin_json()
            save_report(conn, str(d["report_date"]), str(d.get("kind") or "daily"),
                        d.get("path"), d.get("html"), d.get("sections"), d.get("generated_at"))
            print(json.dumps({"ok": True, "data": {"stored": True}}, ensure_ascii=False))
        return 0
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": {"type": type(e).__name__, "message": str(e)}},
                         ensure_ascii=False))
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
