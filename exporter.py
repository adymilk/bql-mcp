"""把分析结果导出为 PDF、Word、PowerPoint、HTML 或 Markdown。"""
from __future__ import annotations

import json
import base64
import io
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from docx import Document
from docx.shared import Pt
from docx.oxml.ns import qn
from pptx import Presentation
from pptx.util import Inches, Pt as PPt

import report as report_module

SUPPORTED = {"html", "md", "docx", "pdf", "pptx"}


def load_metrics(name: str) -> dict[str, Any]:
    json_name = name.replace(".html", "").replace(".json", "") + ".json"
    path = (report_module.REPORTS_DIR / json_name).resolve()
    if not path.exists() or report_module.REPORTS_DIR.resolve() not in path.parents:
        raise ValueError(f"指标记录不存在：{json_name}")
    return json.loads(path.read_text(encoding="utf-8"))


def _fmt(value: Any, digits: int = 4) -> str:
    try:
        if value is None or isinstance(value, bool):
            return "—"
        return f"{float(value):.{digits}f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError):
        return "—"


def _text(metrics: dict[str, Any]) -> list[str]:
    interp = metrics.get("interpretation") or {}
    charts = metrics.get("charts") or []
    values = charts[0].get("values") if charts else {}
    values = values if isinstance(values, dict) else {}
    lines = [
        f"# BQL 质量分析报告 · {metrics.get('endpoint', 'analysis')}",
        "",
        f"频道：{metrics.get('channel', '—')} ｜ 来源：{metrics.get('source', '—')}",
        "",
        "## 结论",
        "",
        f"**{interp.get('verdict', '—')}（{interp.get('grade', '—')} 级）** — {interp.get('headline', '')}",
        "",
    ]
    for title, key in (("关键发现", "diagnosis"), ("风险与假设", "risks"), ("行动建议", "actions")):
        items = interp.get(key) or []
        if items:
            lines.append(f"## {title}")
            lines.append("")
            lines.extend(f"- {item}" for item in items)
            lines.append("")
    if values:
        lines.append("## 核心指标")
        lines.append("")
        lines.append("| 指标 | 值 |")
        lines.append("| --- | --- |")
        for key in ("Cpk1", "Ppk1", "Cp1", "Pp1", "均值1", "WStDev1", "OStDev1"):
            if key in values:
                lines.append(f"| {key} | {_fmt(values[key])} |")
        lines.append("")
    return lines


def _safe_html_path(name: str) -> Path:
    filename = Path(name).name
    if filename != name or not filename.endswith(".html"):
        filename = Path(name).stem + ".html"
    path = (report_module.REPORTS_DIR / filename).resolve()
    if path.parent != report_module.REPORTS_DIR.resolve() or not path.is_file():
        raise ValueError(f"未找到对应 HTML 报告：{filename}")
    return path


def _html_to_docx(source: Path, target: Path) -> Path:
    # lxml is part of the bundled document runtime.  Keeping this parser local
    # avoids requiring a separate HTML dependency just to export a report.
    try:
        from lxml import html as lxml_html
    except ImportError as exc:
        raise RuntimeError("Word 导出需要文档运行时的 lxml 依赖。") from exc

    root = lxml_html.document_fromstring(source.read_text(encoding="utf-8"))
    doc = Document()
    for style_name in ("Normal", "Title", "Heading 1", "Heading 2", "Heading 3"):
        style = doc.styles[style_name]
        style.font.name = "Aptos"
        style.font.size = Pt(10 if style_name == "Normal" else 13 if style_name == "Title" else 11)
        style._element.rPr.rFonts.set(qn("w:eastAsia"), "Heiti SC")

    body = root.find("body")
    if body is None:
        body = root
    block_tags = {"h1", "h2", "h3", "h4", "p", "table", "ul", "ol", "details", "img", "hr"}
    skip_tags = {"script", "style", "nav", "button", "svg"}
    image_cache: dict[str, bytes] = {}

    def hidden(node: Any) -> bool:
        style = (node.get("style") or "").replace(" ", "").lower()
        return node.get("hidden") is not None or node.get("aria-hidden") == "true" or "display:none" in style

    def text_of(node: Any) -> str:
        return " ".join(" ".join(node.itertext()).split())

    def add_table(node: Any) -> None:
        rows = node.xpath(".//tr")
        widths = [len(row.xpath("./th|./td")) for row in rows]
        width = max(widths, default=0)
        if not width:
            return
        table = doc.add_table(rows=0, cols=width)
        table.style = "Light Shading Accent 1"
        for row_node in rows:
            cells = row_node.xpath("./th|./td")
            out = table.add_row().cells
            for index, cell in enumerate(cells[:width]):
                out[index].text = text_of(cell)

    def add_summary_table(node: Any, kind: str) -> None:
        if kind == "hnums":
            items = node.xpath("./div[contains(concat(' ', normalize-space(@class), ' '), ' hn ')]")
            rows = ["\n".join(text_of(part) for part in item if text_of(part)) for item in items]
            width = len(rows)
            if not width:
                return
            table = doc.add_table(rows=1, cols=width)
            table.style = "Light Shading Accent 1"
            for cell, value in zip(table.rows[0].cells, rows):
                cell.text = value
            return
        if kind == "kv":
            items = node.xpath("./div[contains(concat(' ', normalize-space(@class), ' '), ' kvi ')]")
            if not items:
                return
            table = doc.add_table(rows=0, cols=2)
            table.style = "Light Shading Accent 1"
            for item in items:
                cells = table.add_row().cells
                cells[0].text = text_of(item[0]) if len(item) else ""
                cells[1].text = text_of(item[1]) if len(item) > 1 else ""
            return
        if kind == "strip":
            items = node.xpath("./div[contains(concat(' ', normalize-space(@class), ' '), ' st ')]")
            if not items:
                return
            table = doc.add_table(rows=0, cols=2)
            table.style = "Light Shading Accent 1"
            for item in items:
                cells = table.add_row().cells
                cells[0].text = text_of(item[1]) if len(item) > 1 else ""
                cells[1].text = " · ".join(text_of(part) for part in item[1:] if text_of(part))

    def add_image(node: Any, paragraph: Any = None) -> None:
        src = str(node.get("src") or "")
        match = re.match(r"data:image/(?:png|jpeg|jpg);base64,(.+)", src, re.I | re.S)
        if not match:
            if not src.lower().startswith(("http://", "https://")):
                return
            data_uri = report_module._fetch_data_uri(src)
            match = re.match(r"data:image/(?:png|jpeg|jpg);base64,(.+)", data_uri, re.I | re.S)
            if not match:
                return
        try:
            if src in image_cache:
                data = image_cache[src]
            else:
                data = base64.b64decode(match.group(1), validate=True)
                image_cache[src] = data
            if len(data) > 8 * 1024 * 1024:
                return
            target_run = paragraph.add_run() if paragraph is not None else None
            (target_run or doc).add_picture(io.BytesIO(data), width=Inches(6.2))
        except Exception:
            return

    def walk(node: Any) -> None:
        if hidden(node):
            return
        name = str(node.tag).lower() if isinstance(node.tag, str) else ""
        if name in skip_tags:
            return
        classes = set((node.get("class") or "").split())
        if "hnums" in classes:
            add_summary_table(node, "hnums")
            return
        if "kv" in classes:
            add_summary_table(node, "kv")
            return
        if "strip" in classes:
            add_summary_table(node, "strip")
            return
        if name in {"h1", "h2", "h3", "h4"}:
            p = doc.add_heading(level=min(int(name[1]), 3))
            p.add_run(text_of(node))
        elif "hdisp" in classes:
            p = doc.add_heading(level=1)
            p.add_run(text_of(node))
        elif "hgrade" in classes:
            doc.add_paragraph(text_of(node))
        elif name == "p":
            p = doc.add_paragraph()
            p.add_run(text_of(node))
        elif name in {"ul", "ol"}:
            style = "List Bullet" if name == "ul" else "List Number"
            for item in node.xpath("./li"):
                p = doc.add_paragraph(style=style)
                p.add_run(text_of(item))
        elif name == "table":
            add_table(node)
        elif name == "img":
            add_image(node)
        elif name == "details":
            summary = node.xpath("./summary")
            if summary:
                p = doc.add_paragraph(style="Heading 2")
                p.add_run(text_of(summary[0]))
            for child in node:
                if str(child.tag).lower() != "summary":
                    walk(child)
        elif name == "hr":
            doc.add_paragraph(" ")
        elif node.xpath(".//h1|.//h2|.//h3|.//h4|.//p|.//table|.//ul|.//ol|.//details|.//img|.//hr"):
            for child in node:
                walk(child)
        elif text_of(node):
            p = doc.add_paragraph()
            p.add_run(text_of(node))

    for child in body:
        walk(child)
    if not doc.paragraphs and not doc.tables:
        raise ValueError("报告中没有可导出的内容")
    doc.save(target)
    return target


def _html_to_pdf(source: Path, target: Path) -> Path:
    render_error = None
    try:
        from weasyprint import HTML
        HTML(filename=str(source), base_url=str(source.parent)).write_pdf(str(target))
        return target
    except ImportError:
        pass
    except Exception as exc:
        render_error = exc

    # Chromium prints the same CSS layout users see in the report editor. It
    # is preferable to office HTML import, which can collapse grid/flex cards.
    chrome_candidates = (
        shutil.which("google-chrome"),
        shutil.which("google-chrome-stable"),
        shutil.which("chromium"),
        shutil.which("chromium-browser"),
        shutil.which("msedge"),
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        str(Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Google/Chrome/Application/chrome.exe"),
        str(Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / "Google/Chrome/Application/chrome.exe"),
    )
    chrome = next((item for item in chrome_candidates if item and Path(item).is_file()), None)
    if chrome:
        with tempfile.TemporaryDirectory(prefix="bql-chrome-") as temp_dir:
            chrome_target = Path(temp_dir) / target.name
            result = subprocess.run(
                [
                    chrome,
                    "--headless=new",
                    "--disable-gpu",
                    "--no-sandbox",
                    "--no-pdf-header-footer",
                    f"--user-data-dir={temp_dir}/profile",
                    f"--print-to-pdf={chrome_target}",
                    source.as_uri(),
                ],
                capture_output=True,
                text=True,
                timeout=90,
            )
            if result.returncode == 0 and chrome_target.exists():
                chrome_target.replace(target)
                return target

    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if soffice:
        env = os.environ.copy()
        fontconfig_candidates = (
            Path("/opt/homebrew/etc/fonts/fonts.conf"),
            Path("/usr/local/etc/fonts/fonts.conf"),
            Path("/etc/fonts/fonts.conf"),
        )
        system_fontconfig = next((path for path in fontconfig_candidates if path.is_file()), None)
        if system_fontconfig:
            # LibreOffice may inherit a stale per-process font configuration;
            # force the system catalog so CJK glyphs resolve consistently.
            env["FONTCONFIG_FILE"] = str(system_fontconfig)
        with tempfile.TemporaryDirectory(prefix="bql-pdf-") as temp_dir:
            result = subprocess.run(
                [soffice, "--headless", "--convert-to", "pdf", "--outdir", temp_dir, str(source)],
                capture_output=True, text=True, timeout=90, env=env,
            )
            generated = Path(temp_dir) / f"{source.stem}.pdf"
            if result.returncode == 0 and generated.exists():
                generated.replace(target)
                return target
            detail = (result.stderr or result.stdout).strip()
            raise RuntimeError(f"LibreOffice PDF 渲染失败：{detail or result.returncode}")
    if render_error:
        raise RuntimeError(f"PDF 渲染失败：{render_error}") from render_error
    raise RuntimeError("本机未安装 PDF 渲染器。请在报告页选择“导出 PDF”，再在打印窗口选择“存储为 PDF”。")


def export(name: str, format: str) -> Path:
    fmt = format.lower().lstrip(".")
    if fmt not in SUPPORTED:
        raise ValueError(f"不支持的格式：{format}；可选：{', '.join(sorted(SUPPORTED))}")
    base = Path(name).stem
    out_dir = report_module.REPORTS_DIR / "exports"
    out_dir.mkdir(parents=True, exist_ok=True)

    if fmt in {"html", "docx", "pdf"}:
        html_src = _safe_html_path(name)
        if fmt == "html":
            return html_src
        if fmt == "docx":
            return _html_to_docx(html_src, out_dir / f"{base}.docx")
        return _html_to_pdf(html_src, out_dir / f"{base}.pdf")

    metrics = load_metrics(name)

    if fmt == "md":
        path = out_dir / f"{base}.md"
        path.write_text("\n".join(_text(metrics)), encoding="utf-8")
        return path

    path = out_dir / f"{base}.pptx"
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[0])
    slide.shapes.title.text = "BQL 质量分析结论"
    interp = metrics.get("interpretation") or {}
    slide.placeholders[1].text = interp.get("headline", "")
    slide = prs.slides.add_slide(prs.slide_layouts[1])
    slide.shapes.title.text = "关键发现与风险"
    body = slide.placeholders[1].text_frame
    body.text = ""
    for item in (interp.get("diagnosis") or []) + (interp.get("risks") or []):
        body.add_paragraph().text = "• " + item
    slide = prs.slides.add_slide(prs.slide_layouts[1])
    slide.shapes.title.text = "下一步行动"
    body = slide.placeholders[1].text_frame
    body.text = ""
    for index, item in enumerate(interp.get("actions") or [], 1):
        body.add_paragraph().text = f"{index}. {item}"
    prs.save(path)
    return path
