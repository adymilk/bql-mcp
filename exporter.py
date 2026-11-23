"""把分析结果导出为 docx / pptx / html / md。纯数据驱动，输入为指标 JSON。"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from docx import Document
from docx.shared import Pt, RGBColor
from pptx import Presentation
from pptx.util import Inches, Pt as PPt

import report as report_module

SUPPORTED = {"html", "md", "docx", "pptx"}


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


def export(name: str, format: str) -> Path:
    fmt = format.lower().lstrip(".")
    if fmt not in SUPPORTED:
        raise ValueError(f"不支持的格式：{format}；可选：{', '.join(sorted(SUPPORTED))}")
    metrics = load_metrics(name)
    base = name.replace(".html", "").replace(".json", "")
    out_dir = report_module.REPORTS_DIR / "exports"
    out_dir.mkdir(parents=True, exist_ok=True)

    if fmt == "md":
        path = out_dir / f"{base}.md"
        path.write_text("\n".join(_text(metrics)), encoding="utf-8")
        return path

    if fmt == "html":
        html_name = base + ".html"
        src = report_module.REPORTS_DIR / html_name
        if src.exists():
            return src
        raise ValueError(f"未找到对应 HTML 报告：{html_name}")

    if fmt == "docx":
        path = out_dir / f"{base}.docx"
        doc = Document()
        doc.add_heading("BQL 质量分析报告", 0)
        interp = metrics.get("interpretation") or {}
        p = doc.add_paragraph()
        run = p.add_run(f"{interp.get('verdict', '—')}（{interp.get('grade', '—')} 级）")
        run.bold = True
        run.font.size = Pt(14)
        doc.add_paragraph(interp.get("headline", ""))
        doc.add_paragraph(f"频道：{metrics.get('channel', '—')}    来源：{metrics.get('source', '—')}")
        for title, key in (("关键发现", "diagnosis"), ("风险与假设", "risks"), ("行动建议", "actions")):
            doc.add_heading(title, level=1)
            for item in interp.get(key) or []:
                doc.add_paragraph(item, style="List Bullet")
        charts = metrics.get("charts") or []
        values = charts[0].get("values") if charts else {}
        if isinstance(values, dict) and values:
            doc.add_heading("核心指标", level=1)
            table = doc.add_table(rows=1, cols=2)
            table.style = "Light Grid Accent 1"
            table.rows[0].cells[0].text = "指标"
            table.rows[0].cells[1].text = "值"
            for key in ("Cpk1", "Ppk1", "Cp1", "Pp1", "均值1", "WStDev1", "OStDev1"):
                if key in values:
                    row = table.add_row().cells
                    row[0].text = key
                    row[1].text = _fmt(values[key])
        doc.add_paragraph("")
        doc.add_paragraph("本报告由 BQL MCP Connector 生成。")
        doc.save(path)
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
