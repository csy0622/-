"""PRD Markdown → 可粘贴到飞书/钉钉云文档的 HTML（内联样式、表格边框、可离线打开）。

与 ic_backtest/scripts/md2html.py 的差异：
- 表格单元格内允许少量原始 HTML（<br> <b> <strong> <code> <em> <ul> <li>）直通，便于写「多行需求」；
  其余未在白名单内的标签仍会被转义，避免注入。
- 支持无序列表的嵌套（两空格缩进）与行内代码。
"""
import html as htmllib
import os
import re
import sys

ALLOWED_INLINE = ("br", "b", "strong", "code", "em", "ul", "li")

STYLE = {
    "h1": "font-size:22px;font-weight:700;margin:20px 0 12px;color:#1a1a1a;",
    "h2": "font-size:18px;font-weight:700;margin:22px 0 10px;color:#1a1a1a;border-left:4px solid #2f6fd6;padding-left:10px;",
    "h3": "font-size:15px;font-weight:700;margin:16px 0 8px;color:#333;",
    "p": "font-size:14px;line-height:1.8;margin:8px 0;color:#333;",
    "ul": "font-size:14px;line-height:1.9;margin:6px 0;padding-left:22px;color:#333;",
    "table": "border-collapse:collapse;width:100%;margin:10px 0;font-size:13px;",
    "th": "border:1px solid #d0d5db;background:#f0f3f7;padding:7px 10px;text-align:left;font-weight:700;color:#1a1a1a;",
    "td": "border:1px solid #d0d5db;padding:7px 10px;text-align:left;vertical-align:top;color:#333;",
    "blockquote": "border-left:3px solid #d0d5db;background:#f7f9fb;margin:8px 0;padding:8px 12px;color:#555;font-size:13px;",
    "hr": "border:none;border-top:1px solid #e3e6ea;margin:16px 0;",
    "code": "background:#f2f4f7;border-radius:4px;padding:1px 5px;font-size:12.5px;"
            "font-family:SFMono-Regular,Consolas,Menlo,monospace;color:#24292f;",
    "pre": "background:#f6f8fa;border:1px solid #e3e6ea;border-radius:4px;padding:10px 12px;overflow-x:auto;"
           "font-size:12.5px;line-height:1.6;margin:10px 0;white-space:pre;"
           "font-family:SFMono-Regular,Consolas,Menlo,monospace;color:#24292f;",
}

PLACEHOLDER = "\x00%d\x00"


def _protect(text):
    """把白名单标签抽出来存成占位符，其余交给转义。"""
    saved = []

    def repl(m):
        tag = m.group(1)
        name = re.match(r"/?\s*([a-zA-Z0-9]+)", tag)
        if name and name.group(1).lower() in ALLOWED_INLINE:
            saved.append("<%s>" % tag)
            return PLACEHOLDER % (len(saved) - 1)
        return m.group(0)

    text = re.sub(r"<(/?[a-zA-Z][^>]*)>", repl, text)
    return text, saved


def inline(text):
    text, saved = _protect(text)
    text = htmllib.escape(text, quote=False)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"`(.+?)`", lambda m: '<code style="%s">%s</code>' % (STYLE["code"], m.group(1)), text)
    for i, tag in enumerate(saved):
        text = text.replace(PLACEHOLDER % i, tag)
    return text


def cell(text):
    text = text.strip().replace("<br>", "\n")
    text = inline(text).replace("\n", "<br>")
    return '<td style="%s">%s</td>' % (STYLE["td"], text)


def _table(lines, i, n):
    header = [c for c in lines[i].strip().strip("|").split("|")]
    th = "".join('<th style="%s">%s</th>' % (STYLE["th"], inline(c.strip())) for c in header)
    rows = ["<tr>%s</tr>" % th]
    i += 2
    while i < n and lines[i].strip().startswith("|"):
        cells = lines[i].strip().strip("|").split("|")
        rows.append("<tr>" + "".join(cell(c) for c in cells) + "</tr>")
        i += 1
    return '<table style="%s">%s</table>' % (STYLE["table"], "".join(rows)), i


def md_to_html(md):
    lines = md.split("\n")
    out, i, n = [], 0, len(lines)
    while i < n:
        s = lines[i].strip()

        if s.startswith("```"):                                    # 代码块
            i += 1
            buf = []
            while i < n and not lines[i].strip().startswith("```"):
                buf.append(lines[i]); i += 1
            i += 1
            code = htmllib.escape("\n".join(buf), quote=False)
            out.append('<pre style="%s"><code>%s</code></pre>' % (STYLE["pre"], code))
            continue

        if s.startswith("|") and i + 1 < n and re.match(r"^\|[\s:|-]+\|$", lines[i + 1].strip()):
            html, i = _table(lines, i, n)
            out.append(html)
            continue

        m = re.match(r"^(#{1,4})\s+(.*)$", s)                      # 标题
        if m:
            lvl = len(m.group(1))
            tag = "h1" if lvl == 1 else ("h2" if lvl == 2 else "h3")
            out.append('<%s style="%s">%s</%s>' % (tag, STYLE[tag], inline(m.group(2)), tag))
            i += 1
            continue

        if s == "---":
            out.append('<hr style="%s">' % STYLE["hr"]); i += 1; continue

        if s.startswith(">"):                                      # 引用（可多行）
            buf = []
            while i < n and lines[i].strip().startswith(">"):
                buf.append(lines[i].strip().lstrip(">").strip()); i += 1
            out.append('<blockquote style="%s">%s</blockquote>' % (STYLE["blockquote"], "<br>".join(inline(b) for b in buf)))
            continue

        if s.startswith("- "):                                     # 无序列表（支持两空格缩进嵌套）
            items = []
            while i < n and lines[i].strip().startswith("- "):
                raw = lines[i]
                indent = len(raw) - len(raw.lstrip())
                items.append((indent, inline(raw.strip()[2:])))
                i += 1
            html, stack = [], 0
            for indent, text in items:
                while stack < indent:
                    html.append('<ul style="%s">' % STYLE["ul"]); stack += 2
                while stack > indent:
                    html.append("</ul>"); stack -= 2
                html.append("<li>%s</li>" % text)
            while stack > 0:
                html.append("</ul>"); stack -= 2
            out.append("".join(html))
            continue

        if s == "":
            i += 1; continue

        out.append('<p style="%s">%s</p>' % (STYLE["p"], inline(s)))
        i += 1
    return "\n".join(out)


def main(md_path):
    with open(md_path, encoding="utf-8") as f:
        md = f.read()
    title = os.path.basename(md_path).replace(".md", "")
    html = (
        '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width,initial-scale=1">\n'
        "<title>%s</title></head>\n"
        "<body style=\"font-family:-apple-system,BlinkMacSystemFont,'PingFang SC','Microsoft YaHei',sans-serif;"
        'max-width:960px;margin:0 auto;padding:24px 20px 60px;color:#222;background:#fff;">\n%s\n</body></html>'
    ) % (title, md_to_html(md))
    out_path = md_path[:-3] + ".html"
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    print("HTML:", out_path, "(%d bytes)" % len(html))
    return out_path


if __name__ == "__main__":
    main(sys.argv[1])
