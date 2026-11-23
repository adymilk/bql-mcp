"""解析 BQL 返回的 Minitab 输出文档（mtbOutDoc HTML）。

Minitab 的 mtbOutDoc 是一个自包含的 HTML 片段，里面包含：
  - 报告标题（<h1>）
  - 若干图片（<img src="data:image/...;base64,...">，已内嵌，无需联网）
  - 若干统计表格（<table class="stattable">，第一行 <th scope="col"> 为表头）
  - 脚注/警告（<footer class="footnote">）

本模块把它解析成有序的块列表，供报告重新排版使用；不复用 Minitab 自带的
Word 式内联样式，避免污染报告主题。
"""
from __future__ import annotations

import html
import re
import urllib.request
from html.parser import HTMLParser
from typing import Any

_SKIP_TAGS = {"style", "script", "head", "noscript"}
_BLOCK_HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}


class _MtbParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[dict[str, Any]] = []
        self._skip_depth = 0
        self._table_depth = 0
        self._heading_tag: str | None = None
        self._heading_buf: list[str] = []
        self._table: dict[str, Any] | None = None
        self._row: list[dict[str, Any]] | None = None
        self._cell: list[str] | None = None
        self._cell_meta: dict[str, Any] = {}
        self._footnote_depth = 0
        self._footnote_buf: list[str] = []

    # --- helpers -------------------------------------------------
    def _flush_heading(self) -> None:
        if self._heading_tag:
            text = _clean_text("".join(self._heading_buf))
            if text:
                self.blocks.append({"type": "heading", "level": int(self._heading_tag[1]), "text": text})
        self._heading_tag = None
        self._heading_buf = []

    def _close_table(self) -> None:
        if self._table and self._table.get("rows"):
            self.blocks.append(self._table)
        self._table = None
        self._row = None
        self._cell = None
        self._table_depth = 0

    # --- HTMLParser hooks ---------------------------------------
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = {k: (v or "") for k, v in attrs}
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return

        if tag == "br":
            target = self._cell if self._cell is not None else (
                self._heading_buf if self._heading_tag else (
                    self._footnote_buf if self._footnote_depth else None))
            if target is not None:
                target.append(" ")
            return

        if tag in _BLOCK_HEADINGS:
            self._flush_heading()
            self._heading_tag = tag
            self._heading_buf = []
            return

        if tag == "img":
            src = attr.get("src", "")
            if src.startswith("data:image"):
                self.blocks.append({
                    "type": "image",
                    "src": src,
                    "alt": attr.get("alt", "") or "Minitab 图",
                })
            return

        if tag == "table":
            if self._table is None:
                self._table = {"type": "table", "rows": []}
                self._table_depth = 1
            else:
                self._table_depth += 1
            return

        if self._table is not None and tag == "tr" and self._table_depth == 1:
            self._row = []
            return

        if self._row is not None and tag in ("td", "th"):
            self._cell = []
            self._cell_meta = {
                "tag": tag,
                "scope": attr.get("scope", ""),
                "colspan": _int_or_none(attr.get("colspan")),
                "rowspan": _int_or_none(attr.get("rowspan")),
            }
            return

        if tag == "footer" and self._table is None:
            self._flush_heading()
            self._footnote_depth += 1
            self._footnote_buf = []

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "br":
            self.handle_starttag("br", attrs)
        else:
            self.handle_starttag(tag, attrs)
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth:
            return

        if tag in _BLOCK_HEADINGS and self._heading_tag == tag:
            self._flush_heading()
            return

        if self._row is not None and tag in ("td", "th") and self._cell is not None:
            self._row.append({**self._cell_meta, "text": _clean_text("".join(self._cell))})
            self._cell = None
            return

        if self._table is not None and tag == "tr" and self._table_depth == 1 and self._row is not None:
            if any(c.get("text") for c in self._row):
                self._table["rows"].append(self._row)
            self._row = None
            return

        if tag == "table" and self._table is not None:
            self._table_depth -= 1
            if self._table_depth <= 0:
                self._close_table()
            return

        if tag == "footer" and self._footnote_depth:
            text = _clean_text("".join(self._footnote_buf))
            if text:
                self.blocks.append({"type": "note", "text": text})
            self._footnote_buf = []
            self._footnote_depth = max(0, self._footnote_depth - 1)

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._cell is not None:
            self._cell.append(data)
        elif self._heading_tag:
            self._heading_buf.append(data)
        elif self._footnote_depth:
            self._footnote_buf.append(data)


def _int_or_none(value: str | None) -> int | None:
    try:
        number = int(str(value))
        return number if number > 1 else None
    except (TypeError, ValueError):
        return None


def _clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def parse_html(text: str) -> dict[str, Any]:
    """把 Minitab 输出 HTML 解析为 {title, blocks}。"""
    parser = _MtbParser()
    try:
        parser.feed(text)
        parser.close()
    except Exception:
        pass
    if parser._table is not None and parser._table.get("rows"):
        parser.blocks.append(parser._table)

    blocks = [b for b in parser.blocks if not _is_noise(b)]
    title = ""
    # 第一个 h1 作为文档标题；若没有则用第一个 heading
    for block in blocks:
        if block["type"] == "heading" and block["level"] == 1:
            title = block["text"]
            break
    if not title:
        for block in blocks:
            if block["type"] == "heading":
                title = block["text"]
                break

    # 文档标题块从正文中移除（报告头部已展示）
    if title:
        for index, block in enumerate(blocks):
            if block["type"] == "heading" and block["text"] == title:
                blocks.pop(index)
                break

    return {"title": title, "blocks": blocks}


def _is_noise(block: dict[str, Any]) -> bool:
    """过滤 Minitab 文档里的文件名声等无用块。"""
    if block.get("type") != "heading":
        return False
    text = block.get("text", "")
    if re.search(r"\.(csv|txt|xlsx?|mwx|mtw)$", text.strip(), re.I):
        return True
    return False


def fetch(url: str, timeout: float = 10.0) -> dict[str, Any]:
    """下载并解析 mtbOutDoc。失败时返回空结构，不影响报告其它部分。"""
    if not url:
        return {"title": "", "blocks": []}
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "Mozilla/5.0 BQL-MCP"}
        )
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read()
        if not raw or len(raw) > 40 * 1024 * 1024:
            return {"title": "", "blocks": []}
        text = raw.decode("utf-8", "replace")
        return parse_html(text)
    except Exception:
        return {"title": "", "blocks": []}


# --- 渲染 ---------------------------------------------------------------

def _cell_html(cell: dict[str, Any]) -> str:
    tag = "th" if cell.get("tag") == "th" else "td"
    attrs = []
    if cell.get("colspan"):
        attrs.append(f' colspan="{cell["colspan"]}"')
    if cell.get("rowspan"):
        attrs.append(f' rowspan="{cell["rowspan"]}"')
    scope = cell.get("scope")
    if tag == "th" and scope:
        attrs.append(f' scope="{scope}"')
    return f'<{tag}{"".join(attrs)}>{html.escape(cell.get("text", ""))}</{tag}>'


def _is_note_table(rows: list[list[dict[str, Any]]]) -> bool:
    """Minitab 的警告/注释块：单列且每格都是 th。"""
    meaningful = [r for r in rows if r]
    return bool(meaningful) and all(
        len(r) == 1 and r[0].get("tag") == "th" for r in meaningful
    )


def render_table(rows: list[list[dict[str, Any]]]) -> str:
    """渲染为语义化表格：首行全为 th[scope=col] 时作为表头。"""
    if not rows:
        return ""

    # 单列且全为 th 的块，是 Minitab 的警告/注释块，用注释列表呈现更易读
    if _is_note_table(rows):
        lines = [r[0].get("text", "") for r in rows if r and r[0].get("text")]
        if lines:
            return "".join(f'<p class="mtb-note">{html.escape(t)}</p>' for t in lines)

    first = rows[0]
    has_header = bool(first) and all(
        c.get("tag") == "th" and c.get("scope") == "col" for c in first
    )

    def row_html(cells: list[dict[str, Any]], tag: str = "tr") -> str:
        return f"<tr>{''.join(_cell_html(c) for c in cells)}</tr>"

    parts = ['<div class="mtb-table-wrap"><table class="mtb-table">']
    body_start = 0
    if has_header:
        parts.append(f"<thead>{row_html(first)}</thead>")
        body_start = 1
    parts.append("<tbody>")
    for row in rows[body_start:]:
        parts.append(row_html(row))
    parts.append("</tbody></table></div>")
    return "".join(parts)


def render_blocks(blocks: list[dict[str, Any]], image_caption: str = "") -> str:
    """把解析出的块渲染成报告 HTML。"""
    parts: list[str] = []
    section_open = False
    note_buf: list[str] = []
    seen_heading = False

    def flush_notes() -> None:
        if not note_buf:
            return
        lis = "".join(f"<p>{html.escape(t)}</p>" for t in note_buf)
        parts.append(f'<div class="mtb-notes">{lis}</div>')
        note_buf.clear()

    for block in blocks:
        kind = block.get("type")
        if kind != "note":
            flush_notes()
        if kind == "heading":
            if not section_open:
                parts.append('<div class="mtb-section">')
                section_open = True
            # 首个标题作为主标题，后续标题降一级，避免与报告章节标题抢层级
            cls = "mtb-h1" if not seen_heading else "mtb-h2"
            seen_heading = True
            parts.append(f'<h4 class="{cls}">{html.escape(block.get("text", ""))}</h4>')
        elif kind == "image":
            if not section_open:
                parts.append('<div class="mtb-section">')
                section_open = True
            src = html.escape(block.get("src", ""), quote=True)
            caption = image_caption or block.get("alt") or "Minitab 输出图"
            parts.append(
                '<figure class="mtb-figure">'
                f'<a href="{src}" target="_blank" rel="noopener" title="点击查看原图">'
                f'<img src="{src}" alt="{html.escape(caption, quote=True)}" loading="lazy"></a>'
                f'<figcaption>{html.escape(caption)}</figcaption></figure>'
            )
        elif kind == "table":
            rows = block.get("rows") or []
            if _is_note_table(rows):
                for row in rows:
                    if row and row[0].get("text"):
                        note_buf.append(row[0]["text"])
                continue
            if not section_open:
                parts.append('<div class="mtb-section">')
                section_open = True
            parts.append(render_table(rows))
        elif kind == "note":
            text = block.get("text", "")
            if text:
                note_buf.append(text)
    flush_notes()
    if section_open:
        parts.append("</div>")
    return "".join(parts)


def render_text_tables(tables: list[str]) -> str:
    """渲染服务端返回的 OTTable 纯文本表（mtbOutDoc 不可用时的兜底）。"""
    parts = []
    for text in tables:
        if not str(text).strip():
            continue
        parts.append(f'<pre class="mtb-pre">{html.escape(str(text).strip())}</pre>')
    return "".join(parts)
