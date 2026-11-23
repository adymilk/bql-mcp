"""把 BQL 分析结果渲染成自包含的单文件 HTML 可视化报告。

纯标准库实现，不依赖 matplotlib / PIL / 前端 CDN。
设计语言：Linear / Vercel 风格的深色界面——克制的边框、语义色、大数字指标。
"""
from __future__ import annotations

import html
import math
import re
from datetime import datetime
from pathlib import Path
from typing import Any

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

    lsl = _num(payload.get("lspec"))
    usl = _num(payload.get("uspec"))
    target = _num(payload.get("target"))

    return {
        "variable": variable or "",
        "points": points,
        "lsl": lsl,
        "usl": usl,
        "target": target,
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
        return {"label": "无数据", "grade": "—", "color": "var(--muted)", "tone": "neutral"}
    if value >= 1.67:
        return {"label": "能力充分", "grade": "A", "color": "var(--success)", "tone": "good"}
    if value >= 1.33:
        return {"label": "能力充足", "grade": "B", "color": "var(--success)", "tone": "good"}
    if value >= 1.00:
        return {"label": "能力可接受", "grade": "C", "color": "var(--warn)", "tone": "warn"}
    return {"label": "能力不足", "grade": "D", "color": "var(--danger)", "tone": "bad"}


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
    lo_candidates = [p for p in finite]
    hi_candidates = [p for p in finite]
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

    # 正态曲线（用整体标准差），缩放到绘图区高度的 78%
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

    # 数据点（dot plot，同值堆叠）
    buckets: dict[float, int] = {}
    for value in finite:
        key = round(value, 6)
        buckets[key] = buckets.get(key, 0) + 1
    for value, count in sorted(buckets.items()):
        x = px(value)
        for level in range(count):
            y = bottom - 14 - level * 16
            parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="5" fill="var(--dot)" stroke="var(--surface)" stroke-width="1.5"/>')

    # 规格线与均值线
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


def _kpi_card(label: str, value: Any, caption: str) -> str:
    tone = _tone_for(value)
    return (
        f'<div class="kpi {tone}"><div class="kpi-label">{html.escape(label)}</div>'
        f'<div class="kpi-value">{_fmt(value, 3)}</div>'
        f'<div class="kpi-cap">{html.escape(caption)}</div></div>'
    )


def _metric_row(label: str, value: Any, note: str = "") -> str:
    display = _fmt(value, 4)
    return (
        f'<div class="mrow"><span class="mlabel">{html.escape(label)}</span>'
        f'<span class="mvalue">{display}</span>'
        f'<span class="mnote">{html.escape(note)}</span></div>'
    )


STYLE = """
:root{
  --bg:#0b0d11; --surface:#14171e; --surface2:#1b1f28; --line:#272c37;
  --text:#edf0f6; --muted:#8b93a3; --faint:#646c7a;
  --accent:#6c9cff; --accent-fill:rgba(108,156,255,.10); --dot:#9db8ff;
  --success:#35c08e; --warn:#f5a524; --danger:#ff6b6b; --target:#c792ea;
  --grid:#20242d;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
  font:14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",sans-serif;
  -webkit-font-smoothing:antialiased}
.wrap{max-width:1080px;margin:0 auto;padding:38px 28px 72px}
.top{display:flex;justify-content:space-between;align-items:flex-start;gap:20px;margin-bottom:30px}
.brand{display:flex;align-items:center;gap:12px}
.mark{width:38px;height:38px;border-radius:11px;background:var(--accent);color:#0b0d11;
  display:flex;align-items:center;justify-content:center;font-weight:800;font-size:18px;flex:none}
.brand h1{font-size:17px;font-weight:600;margin:0;letter-spacing:.2px}
.brand p{margin:2px 0 0;color:var(--muted);font-size:12px}
.stamp{text-align:right;color:var(--faint);font-size:12px;line-height:1.8}
.stamp b{color:var(--muted);font-weight:500}
.card{background:var(--surface);border:1px solid var(--line);border-radius:16px;padding:26px 28px;margin-bottom:16px}
.verdict{display:flex;align-items:center;gap:22px;padding:4px 2px 24px;border-bottom:1px solid var(--line);margin-bottom:24px}
.grade{width:74px;height:74px;border-radius:18px;display:flex;align-items:center;justify-content:center;
  font-size:38px;font-weight:700;flex:none;border:1px solid currentColor}
.verdict h2{font-size:24px;font-weight:600;margin:0 0 4px}
.verdict p{margin:0;color:var(--muted);font-size:13px}
.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:14px}
.kpi{background:var(--surface2);border:1px solid var(--line);border-radius:13px;padding:18px 20px;border-top:2px solid var(--faint)}
.kpi.good{border-top-color:var(--success)} .kpi.warn{border-top-color:var(--warn)} .kpi.bad{border-top-color:var(--danger)}
.kpi-label{color:var(--muted);font-size:12px;letter-spacing:.4px}
.kpi-value{font-size:34px;font-weight:600;line-height:1.25;margin:6px 0 2px;font-variant-numeric:tabular-nums;
  font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.kpi.good .kpi-value{color:var(--success)} .kpi.warn .kpi-value{color:var(--warn)} .kpi.bad .kpi-value{color:var(--danger)}
.kpi-cap{color:var(--faint);font-size:11px}
.section-head{display:flex;align-items:baseline;justify-content:space-between;gap:12px;margin-bottom:16px}
.section-head h3{font-size:14px;font-weight:600;margin:0}
.section-head span{color:var(--faint);font-size:12px}
.legend{display:flex;gap:20px;flex-wrap:wrap;margin-top:6px;color:var(--muted);font-size:12px}
.legend i{display:inline-block;width:16px;height:3px;border-radius:2px;margin-right:7px;vertical-align:3px}
.two{display:grid;grid-template-columns:1fr 1fr;gap:16px}
.interpret{gap:0}
.interpret .headline{font-size:15px;line-height:1.6;margin:0 0 16px;color:var(--text)}
.iblock{display:grid;grid-template-columns:88px 1fr;gap:10px 16px;padding:11px 0;border-top:1px solid var(--line)}
.iblock:first-of-type{border-top:0}
.ititle{color:var(--muted);font-size:12px;padding-top:2px}
.ilist{margin:0;padding-left:17px;font-size:13px;line-height:1.65}
.ilist li{margin:2px 0}
.mrow{display:grid;grid-template-columns:1fr auto;grid-template-areas:"l v" "n v";
  padding:10px 0;border-bottom:1px solid var(--line);align-items:center}
.mrow:last-child{border-bottom:0}
.mlabel{grid-area:l;color:var(--muted);font-size:13px}
.mnote{grid-area:n;color:var(--faint);font-size:11px;margin-top:1px}
.mvalue{grid-area:v;align-self:center;font:13px ui-monospace,SFMono-Regular,Menlo,monospace;
  font-variant-numeric:tabular-nums;color:var(--text)}
.chips{display:flex;flex-wrap:wrap;gap:8px}
.chip{background:var(--surface2);border:1px solid var(--line);border-radius:8px;padding:6px 12px;
  font:12px ui-monospace,SFMono-Regular,Menlo,monospace;font-variant-numeric:tabular-nums}
.links{display:flex;flex-wrap:wrap;gap:10px}
.links a{color:var(--accent);text-decoration:none;border:1px solid var(--line);background:var(--surface2);
  border-radius:9px;padding:9px 13px;font-size:12px}
.links a:hover{border-color:var(--accent)}
.foot{color:var(--faint);font-size:11px;text-align:center;margin-top:26px;line-height:1.9}
.foot code{color:var(--muted);font-family:ui-monospace,Menlo,monospace}
@media(max-width:760px){.kpis{grid-template-columns:repeat(2,1fr)}.two{grid-template-columns:1fr}
  .verdict{flex-direction:column;align-items:flex-start;gap:14px}.wrap{padding:24px 16px 56px}}
@media print{body{background:#fff;color:#111}}"""


def _interpret_block(result: dict[str, Any]) -> str:
    data = result.get("interpretation")
    if not isinstance(data, dict):
        return ""
    headline = html.escape(str(data.get("headline") or ""))
    parts = [
        '<div class="card interpret">',
        '<div class="section-head"><h3>专家解读</h3><span>规则判定 · 可审计</span></div>',
        f'<p class="headline">{headline}</p>',
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


def render_report(
    endpoint: str,
    payload: dict[str, Any],
    result: dict[str, Any],
    channel: str,
    source: str,
    broadcast: dict[str, Any],
) -> Path:
    metrics = extract_metrics(result, payload)
    status = capability_status(metrics["cpk"])
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    charts = result.get("charts") or []
    mt = charts[0].get("minitab") if charts else {}
    mt = mt if isinstance(mt, dict) else {}
    graph = mt.get("OTGraph")
    graph_url = graph[0] if isinstance(graph, list) and graph else (graph if isinstance(graph, str) else None)
    doc_url = mt.get("mtbOutDoc")
    csv_url = mt.get("data_file_path")

    n_value = _num(metrics["n"]) or len(metrics["points"])
    spec_text = " · ".join(filter(None, [
        f"LSL {_fmt(metrics['lsl'], 2)}" if metrics["lsl"] is not None else "",
        f"USL {_fmt(metrics['usl'], 2)}" if metrics["usl"] is not None else "",
        f"目标 {_fmt(metrics['target'], 2)}" if metrics["target"] is not None else "",
    ])) or "未设置规格限"

    kpis = "".join([
        _kpi_card("Cpk · 组内能力", metrics["cpk"], "潜在能力"),
        _kpi_card("Ppk · 整体能力", metrics["ppk"], "实际表现"),
        _kpi_card("Cp · 组内精度", metrics["cp"], "潜在精度"),
        _kpi_card("Pp · 整体精度", metrics["pp"], "整体精度"),
    ])

    index_rows = "".join([
        _metric_row("Cpk", metrics["cpk"], "组内能力指数"),
        _metric_row("Ppk", metrics["ppk"], "整体能力指数"),
        _metric_row("CPU / Z.USL", metrics["cpu"], "上限侧能力"),
        _metric_row("CPL / Z.LSL", metrics["cpl"], "下限侧能力"),
        _metric_row("PPU / Z.USL", metrics["ppu"], "上限侧整体"),
        _metric_row("PPL / Z.LSL", metrics["ppl"], "下限侧整体"),
    ])
    stat_rows = "".join([
        _metric_row("样本均值", metrics["mean"], "均值"),
        _metric_row("组内标准差", metrics["wstd"], "WStDev"),
        _metric_row("整体标准差", metrics["ostd"], "OStDev"),
        _metric_row("样本量 N", n_value, "观测数"),
        _metric_row("超出上限", metrics["out_usl"], "不良数"),
        _metric_row("超出下限", metrics["out_lsl"], "不良数"),
    ])

    point_chips = "".join(f'<span class="chip">{p:g}</span>' for p in metrics["points"]) or '<span class="mnote">无原始数据</span>'

    links: list[str] = []
    if graph_url:
        links.append(f'<a href="{html.escape(graph_url)}" target="_blank" rel="noopener">能力分析图（OTGraph）↗</a>')
    if doc_url:
        links.append(f'<a href="{html.escape(doc_url)}" target="_blank" rel="noopener">Minitab 报告 ↗</a>')
    if csv_url:
        links.append(f'<a href="{html.escape(csv_url)}" target="_blank" rel="noopener">原始数据 CSV ↗</a>')
    links_html = "".join(links) or '<span class="mnote">本次结果未附带产出物链接</span>'

    graph_block = ""
    if graph_url:
        graph_block = (
            '<div class="card"><div class="section-head"><h3>Minitab 能力分析图</h3>'
            '<span>服务端生成</span></div>'
            f'<img src="{html.escape(graph_url)}" alt="Minitab 能力分析图" '
            'style="width:100%;border-radius:10px;border:1px solid var(--line);display:block" '
            'onerror="this.style.display=\'none\'"></div>'
        )

    doc = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>BQL 分析报告 · {html.escape(endpoint)} · {channel}</title>
<style>{STYLE}</style></head><body>
<div class="wrap">
  <div class="top">
    <div class="brand"><div class="mark">B</div><div>
      <h1>BQL 能力分析报告</h1>
      <p>{html.escape(endpoint)} · 变量 {html.escape(metrics["variable"]) or '—'} · {html.escape(str(metrics["analysis_type"] or ''))}</p>
    </div></div>
    <div class="stamp">生成时间 <b>{now}</b><br>数据源 <b>{html.escape(source)}</b> · 频道 <b>{html.escape(channel)}</b></div>
  </div>

  <div class="card">
    <div class="verdict">
      <div class="grade" style="color:{status['color']}">{status['grade']}</div>
      <div><h2 style="color:{status['color']}">{status['label']}</h2>
      <p>依据 Cpk = {_fmt(metrics['cpk'], 3)} 判定 · 规格：{html.escape(spec_text)} · 子组大小 {html.escape(str(metrics['group_size'] or '—'))}</p></div>
    </div>
    <div class="kpis">{kpis}</div>
  </div>

  {_interpret_block(result)}

  <div class="card">
    <div class="section-head"><h3>数据分布与规格限</h3><span>{len(metrics['points'])} 个观测点 · 曲线为整体分布拟合</span></div>
    {build_chart(metrics)}
    <div class="legend">
      <span><i style="background:var(--accent)"></i>拟合分布 / 均值</span>
      <span><i style="background:var(--danger)"></i>规格限 LSL / USL</span>
      <span><i style="background:var(--target)"></i>目标值</span>
      <span><i style="background:var(--dot)"></i>观测点</span>
    </div>
  </div>

  {graph_block}

  <div class="two">
    <div class="card"><div class="section-head"><h3>能力指数</h3><span>index</span></div>{index_rows}</div>
    <div class="card"><div class="section-head"><h3>过程特性</h3><span>statistics</span></div>{stat_rows}</div>
  </div>

  <div class="card">
    <div class="section-head"><h3>原始数据</h3><span>{html.escape(metrics['variable'])}</span></div>
    <div class="chips">{point_chips}</div>
  </div>

  <div class="card">
    <div class="section-head"><h3>产出物</h3><span>Minitab</span></div>
    <div class="links">{links_html}</div>
  </div>

  <div class="foot">
    BQL MCP Connector · 结果来源 <code>{html.escape(source)}</code> · 频道 <code>{html.escape(channel)}</code><br>
    广播地址 <code>{html.escape(str(broadcast.get('host') or '—'))}:{html.escape(str(broadcast.get('port') or '—'))}</code>
  </div>
</div></body></html>"""

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    safe_channel = re.sub(r"[^A-Za-z0-9_-]", "_", channel)
    path = REPORTS_DIR / f"{endpoint}-{safe_channel}.html"
    path.write_text(doc, encoding="utf-8")
    return path
