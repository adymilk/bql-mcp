"""把 BQL 分析结果渲染成自包含的单文件 HTML 可视化报告。

纯标准库实现，不依赖 matplotlib / PIL / 前端 CDN。
报告布局由 templates/builtin 与 templates/user 中的 HTML 模板决定；
Minitab 能力分析图内嵌为 base64，并统一放在首屏最优先位置。
"""
from __future__ import annotations

import base64
import html
import math
import re
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

import report_templates

REPORTS_DIR = Path(__file__).resolve().parent / "reports"


def _find_value(values: dict[str, Any], *tokens: str) -> Any:
    """在 values 里按关键字找值，容忍变量名后缀（如 Cpk1、均值*1）和多余引号。"""
    for key, value in values.items():
        normalized = str(key).strip().strip('"').strip("*").strip()
        for token in tokens:
            if token in normalized:
                return value
    return None


def _num(value: Any) -> Optional[float]:
    try:
        if value is None or isinstance(value, bool):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _fmt(value: Any, digits: int = 4) -> str:
    number = _num(value)
    if number is None:
        return "—"
    return f"{number:.{digits}f}".rstrip("0").rstrip(".")


def extract_metrics(result: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    charts = result.get("charts") or []
    values = charts[0].get("values") if charts else {}
    values = values if isinstance(values, dict) else {}

    raw_data = payload.get("data") or []
    variables = payload.get("variables") or []
    variable = variables[0] if variables else None
    points: list[float] = []
    for row in raw_data:
        if not isinstance(row, dict):
            continue
        candidate = row.get(variable) if variable else None
        if candidate is None:
            numeric = [_num(v) for v in row.values()]
            candidate = next((n for n in numeric if n is not None), None)
        number = _num(candidate)
        if number is not None:
            points.append(number)

    return {
        "variable": variable or "",
        "points": points,
        "lsl": _num(payload.get("lspec")),
        "usl": _num(payload.get("uspec")),
        "target": _num(payload.get("target")),
        "group_size": payload.get("group_size"),
        "analysis_type": payload.get("type"),
        "mean": _find_value(values, "均值"),
        "wstd": _find_value(values, "WStDev"),
        "ostd": _find_value(values, "OStDev"),
        "n": _find_value(values, "样本N"),
        "cp": _find_value(values, "Cp", "CPU或"),
        "cpk": _find_value(values, "Cpk"),
        "pp": _find_value(values, "Pp", "PPU或"),
        "ppk": _find_value(values, "Ppk"),
        "cpu": _find_value(values, "CPU"),
        "cpl": _find_value(values, "CPL"),
        "ppu": _find_value(values, "PPU"),
        "ppl": _find_value(values, "PPL"),
        "out_usl": _find_value(values, ">USL"),
        "out_lsl": _find_value(values, "<LSL"),
        "values": values,
    }


def capability_status(cpk: Optional[float]) -> dict[str, str]:
    value = _num(cpk)
    if value is None:
        return {"label": "无数据", "grade": "—", "hex": "#8895a8", "tone": "neutral"}
    if value >= 1.67:
        return {"label": "能力充分", "grade": "A", "hex": "#3ddc97", "tone": "good"}
    if value >= 1.33:
        return {"label": "能力充足", "grade": "B", "hex": "#3ddc97", "tone": "good"}
    if value >= 1.00:
        return {"label": "能力可接受", "grade": "C", "hex": "#ffb648", "tone": "warn"}
    return {"label": "能力不足", "grade": "D", "hex": "#ff6b7a", "tone": "bad"}


def _tone_for(value: Any) -> str:
    number = _num(value)
    if number is None:
        return "neutral"
    if number >= 1.33:
        return "good"
    if number >= 1.0:
        return "warn"
    return "bad"


def build_chart(metrics: dict[str, Any]) -> str:
    """生成分布 + 规格限的 SVG 图。"""
    points = metrics["points"]
    lsl, usl, mean = metrics["lsl"], metrics["usl"], _num(metrics["mean"])
    ostd = _num(metrics["ostd"])

    W, H = 1000, 340
    left, right, top, bottom = 64, 968, 46, 280
    plot_w = right - left
    plot_h = bottom - top

    finite = [p for p in points if math.isfinite(p)]
    lo_candidates = list(finite)
    hi_candidates = list(finite)
    if lsl is not None:
        lo_candidates.append(lsl)
        hi_candidates.append(lsl)
    if usl is not None:
        lo_candidates.append(usl)
        hi_candidates.append(usl)
    if lo_candidates:
        xmin, xmax = min(lo_candidates), max(hi_candidates)
    else:
        xmin, xmax = 0.0, 100.0
    if xmax - xmin < 1e-9:
        xmin -= 1
        xmax += 1
    pad = (xmax - xmin) * 0.06
    xmin -= pad
    xmax += pad

    def px(value: float) -> float:
        return left + (value - xmin) / (xmax - xmin) * plot_w

    parts: list[str] = []
    parts.append(f'<svg viewBox="0 0 {W} {H}" width="100%" role="img" aria-label="数据分布与规格限图">')

    for i in range(5):
        y = top + plot_h * i / 4
        parts.append(f'<line x1="{left}" y1="{y:.1f}" x2="{right}" y2="{y:.1f}" stroke="var(--grid)" stroke-width="1"/>')
    for i in range(6):
        value = xmin + (xmax - xmin) * i / 5
        x = px(value)
        parts.append(f'<text x="{x:.1f}" y="{bottom+22}" fill="var(--muted)" font-size="12" text-anchor="middle">{value:.0f}</text>')

    if mean is not None and ostd and ostd > 0:
        curve_pts = []
        peak = 1.0 / (ostd * math.sqrt(2 * math.pi))
        steps = 160
        for i in range(steps + 1):
            value = xmin + (xmax - xmin) * i / steps
            density = math.exp(-0.5 * ((value - mean) / ostd) ** 2) / (ostd * math.sqrt(2 * math.pi))
            x = px(value)
            y = bottom - (density / peak) * plot_h * 0.78
            curve_pts.append(f"{x:.1f},{y:.1f}")
        parts.append(
            f'<polygon points="{left:.1f},{bottom} ' + " ".join(curve_pts) + f' {right:.1f},{bottom}" fill="var(--accent-fill)"/>'
        )
        parts.append(f'<polyline points="{" ".join(curve_pts)}" fill="none" stroke="var(--accent)" stroke-width="2.5" stroke-linejoin="round"/>')

    buckets: dict[float, int] = {}
    for value in finite:
        key = round(value, 6)
        buckets[key] = buckets.get(key, 0) + 1
    for value, count in sorted(buckets.items()):
        x = px(value)
        for level in range(count):
            y = bottom - 14 - level * 16
            parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="5" fill="var(--dot)" stroke="var(--surface)" stroke-width="1.5"/>')

    for value, label, color in ((lsl, "LSL", "var(--danger)"), (usl, "USL", "var(--danger)")):
        if value is None:
            continue
        x = px(value)
        parts.append(f'<line x1="{x:.1f}" y1="{top}" x2="{x:.1f}" y2="{bottom}" stroke="{color}" stroke-width="1.5" stroke-dasharray="6 5"/>')
        anchor = "start" if value <= (xmin + xmax) / 2 else "end"
        dx = 6 if anchor == "start" else -6
        parts.append(f'<text x="{x+dx:.1f}" y="{top-10}" fill="{color}" font-size="12" font-weight="600" text-anchor="{anchor}">{label} {_fmt(value, 2)}</text>')
    if mean is not None:
        x = px(mean)
        parts.append(f'<line x1="{x:.1f}" y1="{top}" x2="{x:.1f}" y2="{bottom}" stroke="var(--accent)" stroke-width="2"/>')
        parts.append(f'<text x="{x+6:.1f}" y="{bottom-8}" fill="var(--accent)" font-size="12" font-weight="600">μ {_fmt(mean, 2)}</text>')
    if metrics["target"] is not None:
        x = px(metrics["target"])
        parts.append(f'<line x1="{x:.1f}" y1="{top}" x2="{x:.1f}" y2="{bottom}" stroke="var(--target)" stroke-width="1.5" stroke-dasharray="2 4"/>')
        parts.append(f'<text x="{x+6:.1f}" y="{top+14}" fill="var(--target)" font-size="12">目标 {_fmt(metrics["target"], 2)}</text>')

    parts.append("</svg>")
    return "".join(parts)


COMPONENT_CSS = """
/* ---- 报告通用组件（由模板主题变量驱动） ---- */
.minitab-hero{background:var(--surface);border:1px solid var(--line);border-radius:16px;
  padding:18px 20px 20px;margin-bottom:16px}
.hero-head{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:14px}
.hero-head strong{font-size:13px;font-weight:600;letter-spacing:.2px}
.hero-badge{color:var(--accent);border:1px solid var(--line);border-radius:999px;
  padding:3px 10px;font-size:10px;letter-spacing:.4px}
.hero-img{display:block;width:100%;max-height:560px;object-fit:contain;
  border-radius:10px;border:1px solid var(--line);background:var(--surface2)}
.hero-caption{margin:10px 2px 0;color:var(--faint);font-size:11px}
.hero-empty{color:var(--muted);font-size:12px;padding:26px 4px;text-align:center}
.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}
.kpi{background:var(--surface2);border:1px solid var(--line);border-radius:12px;padding:16px 18px;
  border-top:2px solid var(--faint)}
.kpi.good{border-top-color:var(--success)} .kpi.warn{border-top-color:var(--warn)} .kpi.bad{border-top-color:var(--danger)}
.kpi-label{color:var(--muted);font-size:11px;letter-spacing:.3px}
.kpi-value{font-size:30px;font-weight:600;line-height:1.3;margin:5px 0 2px;font-variant-numeric:tabular-nums;
  font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.kpi.good .kpi-value{color:var(--success)} .kpi.warn .kpi-value{color:var(--warn)} .kpi.bad .kpi-value{color:var(--danger)}
.kpi-cap{color:var(--faint);font-size:10px}
.interpret .headline{font-size:14px;line-height:1.7;margin:0 0 12px;color:var(--text)}
.iblock{display:grid;grid-template-columns:76px 1fr;gap:8px 14px;padding:10px 0;border-top:1px solid var(--line)}
.iblock:first-of-type{border-top:0}
.ititle{color:var(--muted);font-size:11px;padding-top:2px}
.ilist{margin:0;padding-left:18px;font-size:12px;line-height:1.7}
.ilist li{margin:2px 0}
.mrow{display:grid;grid-template-columns:1fr auto;grid-template-areas:"l v" "n v";
  padding:9px 0;border-bottom:1px solid var(--line);align-items:center}
.mrow:last-child{border-bottom:0}
.mlabel{grid-area:l;color:var(--muted);font-size:12px}
.mnote{grid-area:n;color:var(--faint);font-size:10px;margin-top:1px}
.mvalue{grid-area:v;align-self:center;font:12px ui-monospace,SFMono-Regular,Menlo,monospace;
  font-variant-numeric:tabular-nums;color:var(--text)}
.two{display:grid;grid-template-columns:1fr 1fr;gap:16px}
.chips{display:flex;flex-wrap:wrap;gap:7px}
.chip{background:var(--surface2);border:1px solid var(--line);border-radius:8px;padding:5px 10px;
  font:11px ui-monospace,SFMono-Regular,Menlo,monospace;font-variant-numeric:tabular-nums}
.links{display:flex;flex-wrap:wrap;gap:9px}
.links a{color:var(--accent);text-decoration:none;border:1px solid var(--line);background:var(--surface2);
  border-radius:9px;padding:8px 12px;font-size:11px}
.links a:hover{border-color:var(--accent)}
.legend{display:flex;gap:18px;flex-wrap:wrap;margin-top:8px;color:var(--muted);font-size:11px}
.legend i{display:inline-block;width:15px;height:3px;border-radius:2px;margin-right:6px;vertical-align:3px}
@media(max-width:760px){.kpis{grid-template-columns:repeat(2,1fr)}.two{grid-template-columns:1fr}
  .iblock{grid-template-columns:1fr;gap:4px}}
"""


def _embed_image(url: str) -> str:
    """下载 Minitab 图并内嵌为 data URI，失败时回退原 URL。"""
    if not url:
        return ""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 BQL-MCP"})
        with urllib.request.urlopen(req, timeout=8) as response:
            content_type = response.headers.get("Content-Type") or "image/png"
            data = response.read()
        if not data or len(data) > 8 * 1024 * 1024:
            return html.escape(url, quote=True)
        encoded = base64.b64encode(data).decode("ascii")
        return f"data:{content_type.split(';')[0]};base64,{encoded}"
    except Exception:
        return html.escape(url, quote=True)


def _minitab_block(graph_url: str, doc_url: str) -> str:
    head = (
        '<div class="hero-head"><strong>Minitab 能力分析图</strong>'
        '<span class="hero-badge">服务端原始输出 · 优先展示</span></div>'
    )
    if graph_url:
        src = _embed_image(graph_url)
        body = (
            f'<img class="hero-img" src="{src}" alt="Minitab 能力分析图" '
            'onerror="this.style.display=\'none\'">'
            '<p class="hero-caption">由 Minitab 引擎生成的原始过程能力分析图。'
            + (f'<a href="{html.escape(doc_url, quote=True)}" target="_blank" rel="noopener">查看完整 Minitab 报告 ↗</a>' if doc_url else "")
            + '</p>'
        )
    else:
        body = '<div class="hero-empty">本次结果未附带 Minitab 图表，请查看下方数据分布图。</div>'
    return f'<div class="minitab-hero">{head}{body}</div>'


def _kpi_cards(metrics: dict[str, Any]) -> str:
    cards = [
        ("Cpk · 组内能力", metrics["cpk"], "潜在能力"),
        ("Ppk · 整体能力", metrics["ppk"], "实际表现"),
        ("Cp · 组内精度", metrics["cp"], "潜在精度"),
        ("Pp · 整体精度", metrics["pp"], "整体精度"),
    ]
    parts = []
    for label, value, caption in cards:
        parts.append(
            f'<div class="kpi {_tone_for(value)}"><div class="kpi-label">{html.escape(label)}</div>'
            f'<div class="kpi-value">{_fmt(value, 3)}</div>'
            f'<div class="kpi-cap">{html.escape(caption)}</div></div>'
        )
    return "".join(parts)


def _metric_rows(rows: list[tuple[str, Any, str]]) -> str:
    parts = []
    for label, value, note in rows:
        parts.append(
            f'<div class="mrow"><span class="mlabel">{html.escape(label)}</span>'
            f'<span class="mvalue">{_fmt(value, 4)}</span>'
            f'<span class="mnote">{html.escape(note)}</span></div>'
        )
    return "".join(parts)


def _interpret_block(result: dict[str, Any]) -> str:
    data = result.get("interpretation")
    if not isinstance(data, dict):
        return ""
    parts = [
        '<div class="card interpret">',
        '<div class="section-head"><h3>专家解读</h3><span>规则判定 · 可审计</span></div>',
        f'<p class="headline">{html.escape(str(data.get("headline") or ""))}</p>',
    ]
    for title, key in (("关键发现", "diagnosis"), ("风险提示", "risks"), ("行动建议", "actions")):
        items = data.get(key) or []
        if not items:
            continue
        lis = "".join(f"<li>{html.escape(str(item))}</li>" for item in items)
        parts.append(
            f'<div class="iblock"><div class="ititle">{title}</div><ul class="ilist">{lis}</ul></div>'
        )
    parts.append("</div>")
    return "".join(parts)


def build_context(
    endpoint: str,
    payload: dict[str, Any],
    result: dict[str, Any],
    channel: str,
    source: str,
    broadcast: dict[str, Any],
) -> dict[str, Any]:
    """构建与模板无关的报告上下文。"""
    metrics = extract_metrics(result, payload)
    status = capability_status(metrics["cpk"])
    now_dt = datetime.now()
    charts = result.get("charts") or []
    mt = charts[0].get("minitab") if charts else {}
    mt = mt if isinstance(mt, dict) else {}
    graph = mt.get("OTGraph")
    graph_url = graph[0] if isinstance(graph, list) and graph else (graph if isinstance(graph, str) else None)
    doc_url = mt.get("mtbOutDoc")
    csv_url = mt.get("data_file_path")

    spec_text = " · ".join(filter(None, [
        f"LSL {_fmt(metrics['lsl'], 2)}" if metrics["lsl"] is not None else "",
        f"USL {_fmt(metrics['usl'], 2)}" if metrics["usl"] is not None else "",
        f"目标 {_fmt(metrics['target'], 2)}" if metrics["target"] is not None else "",
    ])) or "未设置规格限"

    n_value = _num(metrics["n"]) or len(metrics["points"])
    variable = metrics["variable"]
    subtitle = " · ".join(filter(None, [
        endpoint,
        f"变量 {variable}" if variable else "",
        str(metrics["analysis_type"] or ""),
    ]))

    index_rows = _metric_rows([
        ("Cpk", metrics["cpk"], "组内能力指数"),
        ("Ppk", metrics["ppk"], "整体能力指数"),
        ("CPU / Z.USL", metrics["cpu"], "上限侧能力"),
        ("CPL / Z.LSL", metrics["cpl"], "下限侧能力"),
        ("PPU / Z.USL", metrics["ppu"], "上限侧整体"),
        ("PPL / Z.LSL", metrics["ppl"], "下限侧整体"),
    ])
    stat_rows = _metric_rows([
        ("样本均值", metrics["mean"], "均值"),
        ("组内标准差", metrics["wstd"], "WStDev"),
        ("整体标准差", metrics["ostd"], "OStDev"),
        ("样本量 N", n_value, "观测数"),
        ("超出上限", metrics["out_usl"], "不良数"),
        ("超出下限", metrics["out_lsl"], "不良数"),
    ])

    point_chips = "".join(f'<span class="chip">{p:g}</span>' for p in metrics["points"]) or '<span class="mnote">无原始数据</span>'

    links: list[str] = []
    if graph_url:
        links.append(f'<a href="{html.escape(graph_url, quote=True)}" target="_blank" rel="noopener">能力分析图（OTGraph）↗</a>')
    if doc_url:
        links.append(f'<a href="{html.escape(doc_url, quote=True)}" target="_blank" rel="noopener">Minitab 报告 ↗</a>')
    if csv_url:
        links.append(f'<a href="{html.escape(csv_url, quote=True)}" target="_blank" rel="noopener">原始数据 CSV ↗</a>')
    links_html = "".join(links) or '<span class="mnote">本次结果未附带产出物链接</span>'

    broadcast_text = f"{broadcast.get('host') or '—'}:{broadcast.get('port') or '—'}"
    footer = (
        f'结果来源 <code>{html.escape(source)}</code> · 频道 <code>{html.escape(channel)}</code><br>'
        f'广播地址 <code>{html.escape(str(broadcast_text))}</code>'
    )

    return {
        "title": "BQL 分析报告",
        "subtitle": subtitle,
        "endpoint": endpoint,
        "variable": variable,
        "analysis_type": str(metrics["analysis_type"] or ""),
        "generated_at": now_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "date": now_dt.strftime("%Y-%m-%d"),
        "time": now_dt.strftime("%H:%M:%S"),
        "channel": channel,
        "source": source,
        "grade": status["grade"],
        "verdict": status["label"],
        "verdict_color": status["hex"],
        "spec": spec_text,
        "group_size": str(metrics["group_size"] or "—"),
        "cpk": _fmt(metrics["cpk"], 4),
        "ppk": _fmt(metrics["ppk"], 4),
        "cp": _fmt(metrics["cp"], 4),
        "pp": _fmt(metrics["pp"], 4),
        "sample_n": str(n_value),
        "minitab_url": graph_url or "",
        "doc_url": doc_url or "",
        "csv_url": csv_url or "",
        "broadcast": broadcast_text,
        "base_css": COMPONENT_CSS,
        "minitab_chart": _minitab_block(graph_url, doc_url),
        "kpis": f'<div class="kpis">{_kpi_cards(metrics)}</div>',
        "interpretation": _interpret_block(result),
        "distribution": build_chart(metrics),
        "index_rows": index_rows,
        "stat_rows": stat_rows,
        "raw_data": f'<div class="chips">{point_chips}</div>',
        "links": f'<div class="links">{links_html}</div>',
        "footer": footer,
    }


def render_report(
    endpoint: str,
    payload: dict[str, Any],
    result: dict[str, Any],
    channel: str,
    source: str,
    broadcast: dict[str, Any],
    template: str = "aurora",
) -> Path:
    context = build_context(endpoint, payload, result, channel, source, broadcast)
    doc = report_templates.render_template(template, context)

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    safe_channel = re.sub(r"[^A-Za-z0-9_-]", "_", channel)
    path = REPORTS_DIR / f"{endpoint}-{safe_channel}.html"
    path.write_text(doc, encoding="utf-8")
    return path


def list_templates() -> list[dict[str, Any]]:
    return report_templates.list_templates()
