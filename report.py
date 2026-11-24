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
import threading
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import quality

import minitab_doc
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
    if digits <= 0:
        return f"{number:.0f}"
    text = f"{number:.{digits}f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


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

    # 实测超差件数：直接点数，而不是用 Minitab 的「合计」(PPM 口径) 当件数
    _lsl = _num(payload.get("lspec"))
    _usl = _num(payload.get("uspec"))
    _lower = _lsl if (_lsl is not None and _lsl != 0) else None   # lspec=0 是“未设下限”哨兵
    observed_out: Optional[int] = None
    if points and (_lower is not None or _usl is not None):
        observed_out = sum(
            1 for p in points
            if (_usl is not None and p > _usl) or (_lower is not None and p < _lower)
        )

    return {
        "variable": variable or "",
        "points": points,
        "observed_out": observed_out,
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
        "out_usl": _find_value(values, ">USL") if _find_value(values, ">USL") is not None
        else _find_value(values, "合计"),
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

    # lsl=0 是 BQL “未设下限”的哨兵值，不画线，避免误导读者
    lsl_draw = None if (lsl == 0) else lsl
    for value, label, color in ((lsl_draw, "LSL", "var(--danger)"), (usl, "USL", "var(--danger)")):
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
        # 标签放在图顶，避开底部的观测点与轴标签
        parts.append(f'<text x="{x+6:.1f}" y="{top+13}" fill="var(--accent)" font-size="12" font-weight="600">μ {_fmt(mean, 2)}</text>')
    if metrics["target"] is not None:
        x = px(metrics["target"])
        parts.append(f'<line x1="{x:.1f}" y1="{top}" x2="{x:.1f}" y2="{bottom}" stroke="var(--target)" stroke-width="1.5" stroke-dasharray="2 4"/>')
        parts.append(f'<text x="{x+6:.1f}" y="{top+30}" fill="var(--target)" font-size="12">目标 {_fmt(metrics["target"], 2)}</text>')

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
.hero-img{display:block;width:100%;max-height:420px;object-fit:contain;
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
.two>*{min-width:0}
.chips{display:flex;flex-wrap:wrap;gap:7px}
.chip{background:var(--surface2);border:1px solid var(--line);border-radius:8px;padding:5px 10px;
  font:11px ui-monospace,SFMono-Regular,Menlo,monospace;font-variant-numeric:tabular-nums}
/* ---- 原始数据表（按子组展开） ---- */
.dmeta{display:flex;gap:18px;flex-wrap:wrap;margin:0 0 12px;color:var(--muted);font-size:11px}
.dmeta b{color:var(--text);font-weight:600;font-variant-numeric:tabular-nums;
  font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.dtable-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(196px,1fr));gap:14px}
.dtable-grid>*{min-width:0}
.dtable-wrap{border:1px solid var(--line);border-radius:11px;overflow-x:auto;overflow-y:hidden;
  background:var(--surface);-webkit-overflow-scrolling:touch;max-width:100%;
  scrollbar-width:thin;scrollbar-color:color-mix(in srgb,var(--faint) 45%,transparent) var(--surface2)}
.dtable-wrap::-webkit-scrollbar,.mtb-table-wrap::-webkit-scrollbar{height:9px}
.dtable-wrap::-webkit-scrollbar-thumb,.mtb-table-wrap::-webkit-scrollbar-thumb{
  background:color-mix(in srgb,var(--faint) 42%,transparent);border-radius:9px}
.dtable-wrap::-webkit-scrollbar-track,.mtb-table-wrap::-webkit-scrollbar-track{
  background:var(--surface2);border-radius:9px}
.dtable{width:100%;border-collapse:collapse;font-size:11.5px;white-space:nowrap}
.dtable caption{caption-side:top;text-align:left;padding:9px 12px 7px;color:var(--faint);font-size:10.5px;
  letter-spacing:.2px;background:var(--surface2);border-bottom:1px solid var(--line)}
.dtable thead th{background:var(--surface2);color:var(--muted);font-weight:600;font-size:10.5px;
  letter-spacing:.2px;padding:7px 10px;text-align:right;border-bottom:1px solid var(--line)}
.dtable thead th:first-child{text-align:left}
.dtable tbody td{padding:6px 10px;text-align:right;border-bottom:1px solid var(--line);
  font-variant-numeric:tabular-nums;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--text)}
.dtable tbody td:first-child{text-align:left;color:var(--faint);font-size:10.5px}
.dtable tbody tr:nth-child(even) td{background:var(--surface2)}
.dtable tbody tr:last-child td{border-bottom:0}
.dtable tbody tr.grp-start td{border-top:1px solid var(--line)}
.dtable td.oos{background:var(--danger-fill,rgba(217,48,37,.09));color:var(--danger);font-weight:650}
.dtable tfoot td{padding:7px 10px;text-align:right;border-top:1px solid var(--line);background:var(--surface2);
  font-size:10.5px;color:var(--muted);font-variant-numeric:tabular-nums;
  font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
.dtable tfoot td:first-child{text-align:left}
.dlegend{margin-top:9px;color:var(--faint);font-size:10.5px}
.links{display:flex;flex-wrap:wrap;gap:9px}
.links a{color:var(--accent);text-decoration:none;border:1px solid var(--line);background:var(--surface2);
  border-radius:9px;padding:8px 12px;font-size:11px}
.links a:hover{border-color:var(--accent)}
.legend{display:flex;gap:18px;flex-wrap:wrap;margin-top:8px;color:var(--muted);font-size:11px}
.legend i{display:inline-block;width:15px;height:3px;border-radius:2px;margin-right:6px;vertical-align:3px}

/* ---- Minitab 完整输出（图 + 表 + 注） ---- */
.mtb-section{margin:0 0 26px}
.mtb-section:last-child{margin-bottom:0}
.mtb-h1{font-size:15px;font-weight:650;margin:0 0 12px;padding-left:10px;
  border-left:3px solid var(--accent);letter-spacing:.1px;line-height:1.4}
.mtb-h2{font-size:12px;font-weight:600;color:var(--muted);margin:18px 0 8px;letter-spacing:.2px}
.mtb-figure{margin:0 0 18px;padding:14px;background:var(--surface2);border:1px solid var(--line);
  border-radius:12px;text-align:center}
.mtb-figure:last-child{margin-bottom:0}
.mtb-figure a{display:block;line-height:0}
.mtb-figure img{display:block;width:100%;height:auto;border-radius:8px;background:#fff;
  box-shadow:0 1px 3px rgba(16,24,40,.06)}
.mtb-figure figcaption{margin-top:9px;color:var(--faint);font-size:11px;text-align:left}
.mtb-table-wrap{overflow-x:auto;border:1px solid var(--line);border-radius:11px;background:var(--surface);
  -webkit-overflow-scrolling:touch;max-width:100%}
.mtb-table{width:100%;border-collapse:collapse;font-size:12px;white-space:nowrap}
.mtb-table thead th{position:sticky;top:0;z-index:1;background:var(--surface2);
  border-bottom:1px solid var(--line);font-weight:650;font-size:11px;color:var(--text);
  letter-spacing:.2px;padding:9px 12px;text-align:left}
.mtb-table tbody th{text-align:left;font-weight:600;color:var(--text);padding:8px 12px;
  border-bottom:1px solid var(--line);background:var(--surface)}
.mtb-table tbody td{text-align:right;padding:8px 12px;border-bottom:1px solid var(--line);
  font-variant-numeric:tabular-nums;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--text)}
.mtb-table tbody tr:nth-child(even) th,.mtb-table tbody tr:nth-child(even) td{background:var(--surface2)}
.mtb-table tbody tr:last-child th,.mtb-table tbody tr:last-child td{border-bottom:0}
.mtb-table tbody tr:hover th,.mtb-table tbody tr:hover td{background:var(--accent-fill)}
.mtb-note{margin:10px 0 0;padding:9px 12px;background:var(--surface2);border-left:2px solid var(--warn);
  border-radius:6px;color:var(--muted);font-size:11.5px;line-height:1.7}
.mtb-notes{margin:12px 0 0;padding:10px 14px;background:var(--surface2);
  border-left:2px solid var(--warn);border-radius:6px}
.mtb-notes p{margin:0;color:var(--muted);font-size:11.5px;line-height:1.75}
.mtb-notes p+p{margin-top:5px}
.mtb-pre{margin:0 0 14px;padding:14px 16px;background:var(--surface2);border:1px solid var(--line);
  border-radius:10px;overflow-x:auto;font:11.5px/1.75 ui-monospace,SFMono-Regular,Menlo,monospace;
  color:var(--text);white-space:pre;tab-size:4}
.mtb-foot{margin-top:14px;padding-top:12px;border-top:1px dashed var(--line);color:var(--faint);font-size:11px}
/* ---- 判定链（受控 → 正态 → 能力） ---- */
.gate{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin:0 0 4px}
.gstep{display:grid;grid-template-columns:auto 1fr auto;gap:10px;align-items:center;
  background:var(--surface2);border:1px solid var(--line);border-radius:10px;padding:11px 13px}
.gstep b{display:block;font-size:12px;font-weight:650;color:var(--text);line-height:1.4}
.gstep small{display:block;color:var(--faint);font-size:10.5px;margin-top:2px;line-height:1.5}
.gstep em{font-style:normal;font-size:10.5px;font-weight:650;letter-spacing:.3px}
.gdot{width:8px;height:8px;border-radius:50%;background:var(--faint);flex:0 0 auto}
.gstep.ok .gdot{background:var(--success)} .gstep.ok em{color:var(--success)}
.gstep.warn .gdot{background:var(--warn)} .gstep.warn em{color:var(--warn)}
.gstep.bad .gdot{background:var(--danger)} .gstep.bad em{color:var(--danger)}
.gstep.ok{background:linear-gradient(0deg,var(--surface2),var(--surface2))}
/* ---- 判定徽标 / 控制限表 ---- */
.pill{display:inline-flex;align-items:center;gap:6px;border-radius:999px;padding:4px 11px;
  font-size:11px;font-weight:650;letter-spacing:.2px}
.pill:before{content:"";width:6px;height:6px;border-radius:50%;background:currentColor}
.pill.ok{background:rgba(22,163,74,.1);color:var(--success)}
.pill.warn{background:rgba(217,119,6,.12);color:var(--warn)}
.pill.bad{background:rgba(217,48,37,.1);color:var(--danger)}
.ctl-head{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin:0 0 14px}
.ctl-head .ctl-note{color:var(--muted);font-size:11.5px}
.limits{width:100%;border-collapse:collapse;font-size:11.5px}
.limits thead th{text-align:right;color:var(--muted);font-weight:600;font-size:10.5px;
  padding:7px 12px;border-bottom:1px solid var(--line);background:var(--surface2)}
.limits thead th:first-child{text-align:left}
.limits tbody td{text-align:right;padding:8px 12px;border-bottom:1px solid var(--line);
  font-variant-numeric:tabular-nums;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--text)}
.limits tbody td:first-child{text-align:left;color:var(--muted);font-family:inherit}
.limits tbody tr:last-child td{border-bottom:0}
.rules{margin:0;padding-left:16px;font-size:11.5px;line-height:1.9;color:var(--text)}
.rules li.hit{color:var(--danger)}
.mast-no{display:block;margin-top:6px;color:var(--faint);font-size:10.5px;letter-spacing:.2px}
.mast-no b{color:var(--muted);font-weight:600}
.meta-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px 22px}
.meta-grid div{display:flex;justify-content:space-between;gap:10px;padding:7px 0;
  border-bottom:1px solid var(--line);font-size:11.5px}
.meta-grid span{color:var(--muted)} .meta-grid b{font-weight:600;color:var(--text);
  font-variant-numeric:tabular-nums;text-align:right}
.msa{margin:12px 0 0;padding:10px 13px;background:var(--surface2);border-left:2px solid var(--warn);
  border-radius:6px;color:var(--muted);font-size:11.5px;line-height:1.75}
.dtool{display:flex;gap:8px;align-items:center;margin:0 0 12px}
.dtool input{flex:1;max-width:260px;border:1px solid var(--line);background:var(--surface2);
  border-radius:8px;padding:7px 11px;font-size:11.5px;color:var(--text);outline:none}
.dtool input:focus{border-color:var(--accent)}
.dtool .dcount{color:var(--faint);font-size:10.5px}
.dtable thead th.sortable{cursor:pointer;user-select:none}
.dtable thead th.sortable:hover{color:var(--text)}
.dtable tbody tr.hide{display:none}
/* 横向滚动时冻结首列（子组号 / 序号），避免滚到右侧后认不出是哪一行 */
.dtable thead th:first-child,.dtable tbody td:first-child,.dtable tfoot td:first-child{
  position:sticky;left:0;z-index:2;background:var(--surface);
  box-shadow:1px 0 0 var(--line),8px 0 12px -8px rgba(16,24,40,.16)}
.dtable thead th:first-child{background:var(--surface2);z-index:3}
.dtable tbody tr:nth-child(even) td:first-child{background:var(--surface2)}
/* 越限（超控制限）与超规格分开着色：前者是"数不可信"，后者是"出了不良" */
.dtable td.ooc{background:color-mix(in srgb,var(--warn) 11%,transparent);
  color:var(--warn);font-weight:650}
.dtable td.pt-no{color:var(--faint);font-size:10.5px;font-variant-numeric:tabular-nums;
  letter-spacing:.2px}
.dtable td.marks{color:var(--warn);font-size:10.5px;font-weight:600;letter-spacing:.3px;
  white-space:nowrap}
.dtable td.rcell{position:relative;padding-bottom:9px}
.dtable td.rcell i{position:absolute;left:8px;bottom:2px;height:2px;border-radius:2px;
  background:color-mix(in srgb,var(--accent) 45%,transparent);max-width:calc(100% - 16px)}
.vt{font-size:10.5px;color:var(--faint)}
.vt.bad{color:var(--danger);font-weight:600}
.vt.warn{color:var(--warn);font-weight:600}

/* ---- 表格横向滚动提示 ---- */
.dscroll-hint{display:none;margin:7px 0 0;color:var(--faint);font-size:10.5px;letter-spacing:.2px}
.dscroll-hint.on{display:flex;align-items:center;gap:6px}
.dscroll-hint .k{display:inline-flex;align-items:center;justify-content:center;height:16px;padding:0 6px;
  border:1px solid var(--line);border-radius:5px;background:var(--surface2);font-size:10px;color:var(--muted)}

/* ---- L1 判决书 ---- */
.hero{margin:0 0 18px;padding:26px 28px 24px;border-radius:16px;background:var(--surface);
  border:1px solid var(--line);box-shadow:var(--shadow);position:relative;overflow:hidden}
.hero:before{content:"";position:absolute;left:0;top:0;bottom:0;width:4px;background:var(--faint)}
.hero.t-bad:before{background:var(--danger)} .hero.t-warn:before{background:var(--warn)}
.hero.t-good:before{background:var(--success)}
.hdisp{font-size:33px;font-weight:600;letter-spacing:-.6px;line-height:1.15}
.hero.t-bad .hdisp{color:var(--danger)} .hero.t-warn .hdisp{color:var(--warn)}
.hero.t-good .hdisp{color:var(--success)} .hero.t-neutral .hdisp{color:var(--muted)}
.hgrade{display:inline-block;margin-top:8px;padding:3px 9px;border-radius:999px;
  border:1px solid var(--line);background:var(--surface2);color:var(--muted);
  font-size:11px;letter-spacing:.2px}
.hreason{margin:11px 0 0;max-width:58em;color:var(--muted);font-size:13px;line-height:1.7}
.hnums{display:grid;grid-template-columns:repeat(3,1fr);gap:1px;margin-top:20px;
  background:var(--line);border:1px solid var(--line);border-radius:12px;overflow:hidden}
.hn{background:var(--surface2);padding:12px 16px;display:flex;flex-direction:column;gap:2px}
.hn span{color:var(--faint);font-size:10px;letter-spacing:.5px}
.hn b{font-size:23px;font-weight:600;line-height:1.25;font-variant-numeric:tabular-nums;letter-spacing:-.4px}
.hn em{color:var(--muted);font-size:10.5px;font-style:normal}
.hn.hot b{color:var(--warn)}
.hn.ref b{color:var(--faint);font-weight:500}
.hn.ref em{color:var(--warn)}
.hbound{margin:12px 0 0;padding-top:10px;border-top:1px dashed var(--line);
  color:var(--faint);font-size:10.5px;line-height:1.6}
.hnext{display:inline-flex;align-items:center;gap:6px;margin-top:16px;padding:7px 14px;
  border-radius:9px;border:1px solid var(--line);background:var(--surface2);color:var(--text);
  font-size:12px;text-decoration:none;transition:background .16s ease,border-color .16s ease}
.hnext:hover{background:var(--accent-fill);border-color:var(--accent);color:var(--accent)}

/* ---- 判定状态条 ---- */
.strip{display:grid;grid-template-columns:repeat(3,1fr);border:1px solid var(--line);
  border-radius:12px;overflow:hidden;background:var(--surface);margin:0 0 18px}
.st{display:flex;align-items:center;gap:9px;padding:11px 15px;border-right:1px solid var(--line);min-width:0}
.st:last-child{border-right:0}
.st i{width:8px;height:8px;border-radius:50%;flex:none;background:var(--faint)}
.st.ok i{background:var(--success)} .st.bad i{background:var(--danger)} .st.warn i{background:var(--warn)}
.st.skip i{background:transparent;box-shadow:0 0 0 1px var(--faint) inset}
.st div{display:flex;flex-direction:column;line-height:1.3;min-width:0}
.st span{color:var(--faint);font-size:10px;letter-spacing:.3px}
.st b{font-size:12px;font-weight:600}
.st.ok b{color:var(--success)} .st.bad b{color:var(--danger)} .st.warn b{color:var(--warn)}
.st em{margin-left:auto;color:var(--muted);font-size:10px;font-style:normal;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:46%}

/* ---- 结论带：一图一句 ---- */
.concl{margin:0 0 15px;padding:12px 15px;border-radius:10px;background:var(--surface2);
  border-left:3px solid var(--faint);font-size:12.5px;line-height:1.7;color:var(--text)}
.concl.t-ok{border-left-color:var(--success)} .concl.t-bad{border-left-color:var(--danger)}
.concl.t-warn{border-left-color:var(--warn)}

/* ---- 能力速览表 ---- */
.kv{display:grid;grid-template-columns:repeat(2,1fr);gap:0 30px}
.kvi{display:flex;justify-content:space-between;align-items:baseline;
  padding:9px 0;border-bottom:1px solid var(--line)}
.kvi span{color:var(--muted);font-size:11.5px}
.kvi b{font-size:13px;font-weight:600;font-variant-numeric:tabular-nums;color:var(--text)}

/* ---- 行动清单 ---- */
.plist{list-style:none;margin:0;padding:0;display:flex;flex-direction:column;gap:8px}
.plist li{display:grid;grid-template-columns:26px 1fr auto auto;gap:12px;align-items:center;
  padding:12px 15px;border:1px solid var(--line);border-radius:11px;background:var(--surface2)}
.pn{display:flex;align-items:center;justify-content:center;width:22px;height:22px;
  border-radius:50%;background:var(--surface);border:1px solid var(--line);
  font-size:11px;color:var(--muted);font-variant-numeric:tabular-nums}
.pt{display:flex;flex-direction:column;gap:2px;min-width:0}
.pt b{font-size:12.5px;font-weight:600}
.pt small{color:var(--muted);font-size:10.5px}
.pc{padding:3px 9px;border-radius:999px;background:var(--surface);border:1px solid var(--line);
  color:var(--muted);font-size:10.5px;font-style:normal;white-space:nowrap}
.pc.free{background:var(--accent-fill);border-color:var(--accent);color:var(--accent)}
.pg{color:var(--text);font-size:11.5px;font-weight:600;white-space:nowrap;font-variant-numeric:tabular-nums}

/* ---- 附录分隔 ---- */
.appendix-head{margin:28px 0 12px;padding-top:18px;border-top:1px solid var(--line);
  color:var(--faint);font-size:10.5px;letter-spacing:.7px}

/* 折叠区内的卡片去掉自带的边框与标题，避免卡片套卡片 */
.fbody .card{border:0;padding:0;margin:0;box-shadow:none;background:none;border-radius:0}
.fbody .card>.sec,.fbody .card>.section-head{display:none}
.fbody .mtb-h2:first-child{margin-top:0}

/* ---- 附录折叠 ---- */
.fold{border:1px solid var(--line);border-radius:12px;background:var(--surface);margin:0 0 10px;overflow:hidden}
.fold summary{display:flex;align-items:center;gap:10px;padding:14px 18px;cursor:pointer;
  list-style:none;user-select:none}
.fold summary::-webkit-details-marker{display:none}
.fmark{width:7px;height:7px;border-right:1.5px solid var(--faint);border-bottom:1.5px solid var(--faint);
  transform:rotate(-45deg);flex:none;transition:transform .18s ease;margin-left:2px}
.fold[open] .fmark{transform:rotate(45deg)}
.fold summary b{font-size:12.5px;font-weight:600}
.fold summary em{margin-left:auto;color:var(--faint);font-size:10.5px;font-style:normal;
  font-variant-numeric:tabular-nums;white-space:nowrap}
.fold summary:hover{background:var(--surface2)}
.fold[open] summary{border-bottom:1px solid var(--line);background:var(--surface2)}
.fbody{padding:18px}

@media(max-width:760px){
  .kpis{grid-template-columns:repeat(2,1fr)}.two{grid-template-columns:1fr}
  .iblock{grid-template-columns:1fr;gap:4px}.gate{grid-template-columns:1fr}
  /* 窄屏防溢出：页头右侧信息不再 nowrap，否则整页被撑宽 */
  .masthead{flex-wrap:wrap;gap:14px}
  .mast-right{text-align:left;white-space:normal}
  .page{padding:26px 16px 48px}
  .kpis>*,.two>*,.dtable-grid>*{min-width:0}
  .kpi{padding:13px 14px}.kpi-value{font-size:25px}
  .card,.verdict-card{padding:18px 16px;border-radius:13px}
  .hero{padding:20px 18px 18px;border-radius:13px}
  .hdisp{font-size:26px}
  .hnums{grid-template-columns:1fr 1fr}
  .hn:nth-child(3){grid-column:1/-1}
  .strip{grid-template-columns:1fr}
  .st{border-right:0;border-bottom:1px solid var(--line)}
  .st:last-child{border-bottom:0}
  .plist li{grid-template-columns:26px 1fr;row-gap:8px}
  .pc{grid-column:2;justify-self:start}
  .pg{grid-column:2;justify-self:start}
  .fbody{padding:14px}
}
@page{size:A4;margin:14mm 12mm}
@media print{
  .masthead{padding-bottom:12px;margin-bottom:16px;border-bottom:1.5px solid var(--line)}
  .mast-no{display:block;margin-top:8px;color:var(--faint);font-size:10px;letter-spacing:.3px}
  .card{break-inside:auto;page-break-inside:auto}
  .verdict-card,.gate-card,.gate,.iblock,.mtb-figure,.mtb-table-wrap,.dtable-wrap,.limits{
    break-inside:avoid;page-break-inside:avoid}
  .gate{grid-template-columns:repeat(3,1fr)}
  .sec{break-after:avoid;page-break-after:avoid}
  /* 打印视口约 600px 会命中窄屏断点，这里逐项锁定回桌面列数 */
  .hnums{grid-template-columns:repeat(3,1fr)}
  .hn:nth-child(3){grid-column:auto}
  .strip{grid-template-columns:repeat(3,1fr)}
  .st{border-right:1px solid var(--line);border-bottom:0}
  .st:last-child{border-right:0}
  .sec{flex-wrap:nowrap}
  .sec h3{white-space:nowrap}
  /* 打印改为流式文档：去掉卡片外观，避免跨页切断边框，也省回被整块推走的空白 */
  .hero,.strip,.card{box-shadow:none;border:0;background:none;border-radius:0;
    break-inside:auto;page-break-inside:auto;padding:0 0 12px;margin:0 0 14px;
    border-bottom:1px solid var(--line)}
  .hero{padding-left:14px}
  .hero:before{width:3px}
  .strip{padding:0 0 10px}
  .st{padding:0 12px}
  .st:first-child{padding-left:0}
  .plist li{grid-template-columns:26px 1fr auto auto}
  .pc,.pg{grid-column:auto;justify-self:auto}
  .kv{grid-template-columns:repeat(2,1fr)}
  .gate{grid-template-columns:repeat(3,1fr)}
  .hero,.strip,.concl,.plist li,.kv{break-inside:avoid;page-break-inside:avoid}
  .fold{border:0;border-radius:0;margin:0 0 10px;overflow:visible}
  .fold summary{padding:0 0 6px;background:none!important;border-bottom:1px solid var(--line);cursor:default}
  .fmark{display:none}
  .fbody{padding:10px 0 0}
  .dtool,.aq-rail,.dscroll-hint,.hnext{display:none!important}
  .dtable tbody tr.hide{display:table-row}
  .foot{border-top:1px solid var(--line);margin-top:18px;padding-top:8px;font-size:10px}
}
"""


# ---------------------------------------------------------------- 右侧锚点
# 由 render_report 统一注入，模板不需要任何改动即可获得区域导航与表格滚动提示。
NAV_STYLE = """<style>
.aq-rail{position:fixed;right:14px;top:50%;transform:translateY(-50%);z-index:80;
  display:flex;flex-direction:column;gap:1px;padding:9px 7px;max-height:78vh;overflow-y:auto;
  background:color-mix(in srgb,var(--surface,#fff) 90%,transparent);
  border:1px solid var(--line,#e3e8ef);border-radius:13px;
  box-shadow:0 8px 26px rgba(16,24,40,.08);
  backdrop-filter:blur(12px) saturate(1.1);-webkit-backdrop-filter:blur(12px) saturate(1.1);
  scrollbar-width:none}
.aq-rail::-webkit-scrollbar{display:none}
.aq-rail a{display:flex;align-items:center;gap:8px;padding:5px 6px;border-radius:8px;
  text-decoration:none;color:var(--muted,#5a6678);font-size:11px;line-height:1.15;white-space:nowrap;
  cursor:pointer;transition:background .16s ease,color .16s ease}
.aq-rail a .aq-dot{width:6px;height:6px;flex:none;border-radius:50%;background:var(--line,#ccd4e0);
  transition:background .18s ease,box-shadow .18s ease,transform .18s ease}
.aq-rail a .aq-txt{max-width:0;opacity:0;overflow:hidden;
  transition:max-width .24s cubic-bezier(.4,0,.2,1),opacity .16s ease}
.aq-rail:hover a .aq-txt,.aq-rail a:focus-visible .aq-txt{max-width:210px;opacity:1}
.aq-rail a:hover{background:var(--surface2,#f5f7fa);color:var(--text,#0f172a)}
.aq-rail a.on{color:var(--text,#0f172a);font-weight:600}
.aq-rail a.on .aq-dot{background:var(--accent,#4f46e5);transform:scale(1.25);
  box-shadow:0 0 0 3px color-mix(in srgb,var(--accent,#4f46e5) 20%,transparent)}
.aq-rail .aq-top{border-bottom:1px solid var(--line,#e3e8ef);border-radius:8px 8px 0 0;
  margin-bottom:4px;padding-bottom:7px}
.aq-rail a.aq-l1{margin-left:7px}
.aq-rail a.aq-l2{margin-left:14px}
.aq-rail a.aq-l2 .aq-txt{font-size:10.5px}
.aq-rail a.aq-group{color:var(--faint,#8a94a6);font-size:10px;letter-spacing:.7px;
  text-transform:uppercase;cursor:default;pointer-events:none;padding-top:7px}
.aq-rail a.aq-group .aq-dot{display:none}
.aq-rail a.aq-group:hover{background:none;color:var(--faint,#8a94a6)}
.aq-rail a .aq-dot.ring{background:none;border:1.5px solid var(--line,#ccd4e0)}
.aq-rail a.on .aq-dot.ring{border-color:var(--accent,#4f46e5);background:none}
.aq-rail a.aq-owner .aq-dot{background:color-mix(in srgb,var(--accent,#4f46e5) 40%,var(--line,#ccd4e0))}
.aq-sec{scroll-margin-top:16px}
@media(max-width:1240px){.aq-rail{display:none}}
@media print{.aq-rail,.dscroll-hint{display:none!important}}
</style>"""

NAV_SCRIPT = """<script>
(function(){
  var items=[],links=[],used=[];

  function isFold(el){ return el.tagName==='DETAILS' && el.classList.contains('fold'); }
  function foldOf(el){
    var p=el&&el.parentElement;
    while(p){ if(isFold(p)) return p; p=p.parentElement; }
    return null;
  }
  function headLabel(el){
    var h=el.querySelector('.sec h3,.section-head h3,h3,h2,.hdisp');
    return h?(h.textContent||'').trim():'';
  }
  function foldLabel(d){
    var s=d.querySelector('summary');
    if(!s) return '';
    var b=s.querySelector('b');
    return ((b||s).textContent||'').trim();
  }
  function add(el,txt,kind,depth,folds){
    if(!el||!txt) return;
    for(var i=0;i<used.length;i++){ if(used[i]===el) return; }
    used.push(el);
    if(!el.id) el.id='aq-sec-'+(items.length+1);
    el.classList.add('aq-sec');
    var idx=el.querySelector('.sec .idx,.section-head .idx');
    items.push({el:el,id:el.id,txt:txt,kind:kind,depth:depth,folds:folds||[],
      idx:idx?(idx.textContent||'').trim():''});
  }

  // 正文区段、折叠块（details.fold）与附录分隔按文档顺序统一收集：
  // 折叠区不展开时也要在导航里占位，点一下既展开又定位。
  [].slice.call(document.querySelectorAll(
    '.verdict-card,.hero,.strip,.card,.appendix-head,details.fold'
  )).forEach(function(el){
    if(el.classList.contains('appendix-head')){
      add(el,'附录','group',0,[]);
      return;
    }
    if(isFold(el)){
      if(foldOf(el)) return;                 // 嵌套折叠不单列
      add(el,foldLabel(el),'fold',1,[el]);
      return;
    }
    if(foldOf(el)) return;                   // 折叠区内的卡片标题已隐藏，不单列
    if(el.parentElement&&el.parentElement.closest('.card,.verdict-card')) return;
    add(el,el.classList.contains('hero')?'结论':headLabel(el),'card',0,[]);
  });

  if(items.length>=3){
    var rail=document.createElement('nav');
    rail.className='aq-rail';
    rail.setAttribute('aria-label','报告区域导航');

    function mk(label,targetId,item){
      var a=document.createElement('a');
      a.href='#'+targetId;
      a.title=label;
      a.dataset.target=targetId;
      var dot=document.createElement('span'); dot.className='aq-dot';
      var sp=document.createElement('span'); sp.className='aq-txt'; sp.textContent=label;
      a.appendChild(dot); a.appendChild(sp);
      if(item){
        if(item.depth===1) a.classList.add('aq-l1');
        if(item.depth>=2) a.classList.add('aq-l2');
        if(item.kind==='fold'){ a.classList.add('aq-fold'); dot.classList.add('ring'); }
        if(item.kind==='group'){ a.classList.add('aq-group'); a.dataset.group='1'; }
      }
      a.addEventListener('click',function(e){
        e.preventDefault();
        var t=document.getElementById(targetId);
        if(!t) return;
        if(targetId==='__top'){ window.scrollTo({top:0,behavior:'smooth'}); }
        else {
          if(item&&item.folds.length){          // 目标在折叠区里：先展开再定位
            item.folds.forEach(function(d){ if(!d.open) d.open=true; });
          }
          var before=window.scrollY;
          t.scrollIntoView({behavior:'smooth',block:'start'});
          setTimeout(function(){                 // 平滑滚动不可用时兜底硬定位
            if(Math.abs(window.scrollY-before)<4){
              window.scrollTo(0,t.getBoundingClientRect().top+window.scrollY-16);
            }
          },420);
        }
        try{ history.replaceState(null,'',targetId==='__top'?location.pathname:'#'+targetId); }catch(err){}
        setTimeout(pick,80);
        setTimeout(pick,520);       // 滚动稳定后再校一次高亮，避免平滑滚动丢帧漏判
      });
      rail.appendChild(a);
      return a;
    }

    var topLink=mk('回到顶部','__top');
    topLink.classList.add('aq-top');
    links.push(topLink);
    items.forEach(function(it){
      var label=(it.kind==='card'&&it.idx&&it.idx.length<=4) ? it.idx+' · '+it.txt : it.txt;
      it.link=mk(label,it.id,it);
      links.push(it.link);
    });
    document.body.appendChild(rail);

    var picked=null;
    function ownerOf(id){
      for(var i=0;i<items.length;i++){
        if(items[i].id!==id||!items[i].folds.length) continue;
        for(var j=0;j<items.length;j++){ if(items[j].el===items[i].folds[0]) return items[j].id; }
      }
      return null;
    }
    function setActive(id){
      if(id===picked) return;
      picked=id;
      links.forEach(function(a){
        a.classList.toggle('on',a.dataset.group!=='1'&&a.dataset.target===id);
      });
      var own=ownerOf(id);
      if(own===id) own=null;
      links.forEach(function(a){
        a.classList.toggle('aq-owner',own!==null&&a.dataset.target===own);
      });
      var on=rail.querySelector('a.on');
      if(on && rail.scrollHeight>rail.clientHeight+4){
        var rt=rail.getBoundingClientRect(),ot=on.getBoundingClientRect();
        if(ot.top<rt.top+6 || ot.bottom>rt.bottom-6){
          rail.scrollTop += (ot.top-rt.top) - rt.height/2 + ot.height/2;
        }
      }
    }
    var ticking=false;
    function pick(){
      ticking=false;
      var line=window.innerHeight*0.32, best=null, first=null;
      for(var i=0;i<items.length;i++){
        var el=items[i].el;
        if(!el.getClientRects().length) continue;   // 折叠未展开的隐藏项不参与判定
        if(first===null) first=items[i].id;
        if(el.getBoundingClientRect().top-line<=0) best=items[i].id;
      }
      if(best===null) best=first;
      if(window.scrollY<40) best='__top';
      setActive(best);
    }
    window.addEventListener('scroll',function(){
      if(!ticking){ ticking=true; requestAnimationFrame(pick); }
    },{passive:true});
    window.addEventListener('resize',pick);
    pick();
  }

  var folds=[].slice.call(document.querySelectorAll('details.fold'));
  if(folds.length){
    var reopened=[];
    window.addEventListener('beforeprint',function(){
      reopened=[];
      folds.forEach(function(d){ if(!d.open){ d.open=true; reopened.push(d); } });
    });
    window.addEventListener('afterprint',function(){
      reopened.forEach(function(d){ d.open=false; }); reopened=[];
    });
  }

  var wraps=[].slice.call(document.querySelectorAll('.dtable-wrap,.mtb-table-wrap'));
  wraps.forEach(function(w){
    var hint=document.createElement('div');
    hint.className='dscroll-hint';
    hint.innerHTML='<span class="k">\u2190</span><span class="k">\u2192</span>'
      +'<span>\u8868\u683c\u8d85\u51fa\u5bb9\u5668\u5bbd\u5ea6\uff0c\u53ef\u6a2a\u5411\u6eda\u52a8\u67e5\u770b\u5168\u90e8\u5217</span>';
    if(w.parentElement) w.parentElement.insertBefore(hint,w.nextSibling);
    function upd(){
      var need=w.scrollWidth-w.clientWidth>2;
      hint.classList.toggle('on',need);
      w.classList.toggle('scrollable',need);
    }
    upd();
    window.addEventListener('resize',upd);
    window.addEventListener('load',upd);
    setTimeout(upd,400);
  });
})();
</script>"""


def attach_navigation(doc: str) -> str:
    """给任意模板注入右侧锚点与滚动提示，模板本身无需改造。"""
    payload = NAV_STYLE + NAV_SCRIPT
    marker = "</body>"
    if marker in doc:
        index = doc.rfind(marker)
        return doc[:index] + payload + doc[index:]
    return doc + payload


def _graph_url(result: dict[str, Any]) -> str:
    charts = result.get("charts") or []
    mt = charts[0].get("minitab") if charts else {}
    if not isinstance(mt, dict):
        return ""
    graph = mt.get("OTGraph")
    if isinstance(graph, list) and graph:
        return str(graph[0])
    return str(graph) if isinstance(graph, str) else ""


def _doc_url(result: dict[str, Any]) -> str:
    """取一份结果对应的 Minitab 原始文档链接。"""
    charts = result.get("charts") or []
    for chart in charts:
        mt = chart.get("minitab") if isinstance(chart, dict) else None
        if not isinstance(mt, dict):
            continue
        value = mt.get("mtbOutDoc")
        if isinstance(value, str) and value:
            return value
    return ""


def _fetch_data_uri(url: str, timeout: float = 6.0) -> str:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 BQL-MCP"})
        with urllib.request.urlopen(req, timeout=timeout) as response:
            content_type = response.headers.get("Content-Type") or "image/png"
            data = response.read()
        if not data or len(data) > 8 * 1024 * 1024:
            return ""
        encoded = base64.b64encode(data).decode("ascii")
        return f"data:{content_type.split(';')[0]};base64,{encoded}"
    except Exception:
        return ""


def embed_minitab_image_async(path: Path, result: dict[str, Any], timeout: float = 6.0) -> None:
    """后台把远程 Minitab 图内嵌为 base64 并重写文件，不阻塞分析返回。"""
    url = _graph_url(result)
    if not url:
        return

    def work() -> None:
        data_uri = _fetch_data_uri(url, timeout)
        if not data_uri:
            return
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            return
        for candidate in (html.escape(url, quote=True), url):
            marker = f'src="{candidate}"'
            if marker in text:
                text = text.replace(marker, f'src="{data_uri}"', 1)
                break
        else:
            return
        try:
            path.write_text(text, encoding="utf-8")
        except OSError:
            pass

    threading.Thread(target=work, daemon=True, name="bql-embed-image").start()


def embed_images_async(path: Path, urls: list[str], timeout: float = 6.0) -> None:
    """后台把多张远程图内嵌为 base64 并重写文件，报告可离线归档。"""
    targets = [url for url in dict.fromkeys(urls) if url]
    if not targets:
        return

    def work() -> None:
        text: Optional[str] = None
        for url in targets:
            data_uri = _fetch_data_uri(url, timeout)
            if not data_uri:
                continue
            if text is None:
                try:
                    text = path.read_text(encoding="utf-8")
                except OSError:
                    return
            for candidate in (html.escape(url, quote=True), url):
                marker = f'src="{candidate}"'
                if marker in text:
                    text = text.replace(marker, f'src="{data_uri}"', 1)
                    break
        if text is not None:
            try:
                path.write_text(text, encoding="utf-8")
            except OSError:
                pass

    threading.Thread(target=work, daemon=True, name="bql-embed-images").start()


def _minitab_block(
    graph_url: str,
    doc_url: str,
    embed: bool = False,
    title: str = "Minitab 能力分析图",
    caption: str = "由 Minitab 引擎生成的原始过程能力分析图。",
) -> str:
    """渲染首屏主图；图本身保持 Minitab 原始输出，标题说明其判读角色。"""
    head = (
        f'<div class="hero-head"><strong>{html.escape(title)}</strong>'
        '<span class="hero-badge">服务端原始输出 · 优先展示</span></div>'
    )
    if graph_url:
        src = _fetch_data_uri(graph_url) if embed else html.escape(graph_url, quote=True)
        body = (
            f'<img class="hero-img" src="{src}" alt="{html.escape(title, quote=True)}" '
            'onerror="this.style.display=\'none\'">'
            f'<p class="hero-caption">{html.escape(caption)}'
            + (f'<a href="{html.escape(doc_url, quote=True)}" target="_blank" rel="noopener">查看完整 Minitab 报告 ↗</a>' if doc_url else "")
            + '</p>'
        )
    else:
        body = '<div class="hero-empty">本次结果未附带 Minitab 图表，请查看下方数据分布图。</div>'
    return f'<div class="minitab-hero">{head}{body}</div>'


def _all_graph_urls(result: dict[str, Any]) -> list[str]:
    """Minitab 可能返回多张图（如箱线图 3 张、六合一图 6 张），全部保留。"""
    charts = result.get("charts") or []
    urls: list[str] = []
    for chart in charts:
        mt = chart.get("minitab") if isinstance(chart, dict) else None
        if not isinstance(mt, dict):
            continue
        graph = mt.get("OTGraph")
        if isinstance(graph, list):
            urls.extend(str(u) for u in graph if u)
        elif isinstance(graph, str) and graph:
            urls.append(graph)
    # 去重保序
    seen: set[str] = set()
    unique = []
    for url in urls:
        if url not in seen:
            seen.add(url)
            unique.append(url)
    return unique


def _mtb_text_outputs(result: dict[str, Any]) -> dict[str, Any]:
    """收集 Minitab 的文字产出：OTTable / OTMessage / OTTitle / OTFormula。"""
    charts = result.get("charts") or []
    merged: dict[str, list[str]] = {"OTTable": [], "OTMessage": [], "OTTitle": [], "OTFormula": []}
    for chart in charts:
        mt = chart.get("minitab") if isinstance(chart, dict) else None
        if not isinstance(mt, dict):
            continue
        for key in merged:
            value = mt.get(key)
            if isinstance(value, list):
                merged[key].extend(str(v) for v in value if str(v).strip())
            elif isinstance(value, str) and value.strip():
                merged[key].append(value)
    return merged


def _minitab_full_section(result: dict[str, Any], doc_url: str) -> dict[str, Any]:
    """构建「Minitab 完整输出」区块：优先解析 mtbOutDoc，兜底用 OTGraph + OTTable。"""
    blocks: list[dict[str, Any]] = []
    title = ""
    if doc_url:
        doc = minitab_doc.fetch(doc_url)
        blocks = doc.get("blocks") or []
        title = doc.get("title") or ""

    image_count = sum(1 for b in blocks if b.get("type") == "image")
    table_count = sum(1 for b in blocks if b.get("type") == "table")

    texts = _mtb_text_outputs(result)

    if blocks:
        body = minitab_doc.render_blocks(blocks)
        # mtbOutDoc 有时只含图（能力报告类），此时补上服务端单独返回的文本表
        if table_count == 0 and texts["OTTable"]:
            body += minitab_doc.render_text_tables(texts["OTTable"])
            table_count = len(texts["OTTable"])
        # 补充文档里没有的 Minitab 提示文字
        extra = [m for m in texts["OTMessage"] if m.strip() and m.strip()[:20] not in body]
        if extra:
            body += '<div class="mtb-notes">' + "".join(
                f"<p>{html.escape(m.strip())}</p>" for m in extra
            ) + "</div>"
    else:
        # 兜底：把服务端返回的图与文本表全部列出
        graphs = _all_graph_urls(result)
        parts: list[str] = []
        for index, url in enumerate(graphs, 1):
            parts.append(
                '<figure class="mtb-figure">'
                f'<a href="{html.escape(url, quote=True)}" target="_blank" rel="noopener">'
                f'<img src="{html.escape(url, quote=True)}" alt="Minitab 图 {index}" loading="lazy"></a>'
                f'<figcaption>Minitab 输出图 {index}（远程地址，点击查看原图）</figcaption></figure>'
            )
        if texts["OTTitle"]:
            parts.append("".join(
                f'<h4 class="mtb-h2">{html.escape(t)}</h4>' for t in texts["OTTitle"]
            ))
        parts.append(minitab_doc.render_text_tables(texts["OTTable"]))
        if texts["OTMessage"]:
            parts.append("".join(
                f'<p class="mtb-note">{html.escape(m)}</p>' for m in texts["OTMessage"]
            ))
        if texts["OTFormula"]:
            parts.append(minitab_doc.render_text_tables(texts["OTFormula"]))
        body = "".join(parts)
        image_count = len(graphs)
        table_count = len(texts["OTTable"])

    meta = []
    if image_count:
        meta.append(f"{image_count} 张图")
    if table_count:
        meta.append(f"{table_count} 个表格")
    meta_text = " · ".join(meta) or "未附带图表"

    if not body:
        body = '<div class="hero-empty">本次结果未附带 Minitab 输出内容。</div>'

    foot = ""
    if doc_url:
        foot = (
            '<p class="mtb-foot">以上图表与文字均来自 Minitab 引擎原始输出'
            f' · <a href="{html.escape(doc_url, quote=True)}" target="_blank" rel="noopener">'
            "查看原始 Minitab 报告 ↗</a></p>"
        )

    return {
        "html": body + foot,
        "title": title,
        "image_count": image_count,
        "table_count": table_count,
        "meta": meta_text,
        "has_content": bool(blocks) or image_count > 0 or table_count > 0,
    }


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
        shown = value if isinstance(value, str) else _fmt(value, 4)
        parts.append(
            f'<div class="mrow"><span class="mlabel">{html.escape(label)}</span>'
            f'<span class="mvalue">{shown}</span>'
            f'<span class="mnote">{html.escape(note)}</span></div>'
        )
    return "".join(parts)


def _ppm_text(value: Any) -> str:
    """PPM 用整数展示，避免 333333.3333 这种无意义的尾数。"""
    number = _num(value)
    if number is None:
        return "—"
    return f"{int(round(number)):,}"


_TABLE_JS = """<script>
(function(){
  var tables=[].slice.call(document.querySelectorAll('.dtable'));
  if(!tables.length)return;
  tables.forEach(function(t){
    var ths=t.querySelectorAll('thead th');
    [].forEach.call(ths,function(th,i){
      var label=th.textContent.trim();
      if(label==='判定'||label==='#'||label==='子组')return;
      th.classList.add('sortable');
      th.title='点击按此列排序';
      var dir=1;
      th.addEventListener('click',function(){
        dir=-dir;
        var tb=t.tBodies[0];
        var rows=[].slice.call(tb.rows);
        rows.sort(function(a,b){
          var x=a.cells[i],y=b.cells[i];
          if(!x||!y)return 0;
          var nx=parseFloat(x.textContent),ny=parseFloat(y.textContent);
          if(!isNaN(nx)&&!isNaN(ny))return (nx-ny)*dir;
          return x.textContent.localeCompare(y.textContent)*dir;
        });
        rows.forEach(function(r){tb.appendChild(r)});
      });
    });
  });
  var box=document.querySelector('.dtool input');
  if(box)box.addEventListener('input',function(){
    var q=box.value.trim().toLowerCase();
    tables.forEach(function(t){
      var shown=0;
      [].forEach.call(t.tBodies[0].rows,function(r){
        var hit=!q||r.textContent.toLowerCase().indexOf(q)>=0;
        r.classList.toggle('hide',!hit);
        if(hit)shown++;
      });
      var wrap=t.closest('.dtable-wrap');
      if(wrap)wrap.style.display=shown?'':'none';
    });
  });
})();
</script>"""


def _raw_table(metrics: dict[str, Any], limits: Optional[list] = None,
               check: Optional[dict] = None) -> str:
    """原始数据表：与图对齐的查阅口径，而不是数据罗列。

    分组 → 子组 | 观测号 | X1..Xn | 均值 | R | (s) | 越限 | 判定
    单值 → 分栏：# | 测量值 | 偏差 | 标记

    limits 来自控制图接口 [(名称, UCL, CL, LCL), ...]。把它接进来是为了让图上的
    「第 N 点越限」能在表里被直接定位：观测号列给出该组的点号区间，越限列标出是
    主图还是离散图越限。原来表只标超规格，图说越限、表说正常，两边对不上。
    s 列只在子组大于 5 时出现——n≤5 时 R 的效率已经够，多一列只是增加宽度。
    """
    points = [p for p in (metrics.get("points") or []) if isinstance(p, (int, float))]
    if not points:
        return '<div class="mnote">无原始数据</div>'

    lsl = metrics.get("lsl")
    usl = metrics.get("usl")
    target = metrics.get("target")
    mean = metrics.get("mean")
    ref = target if target is not None else mean

    try:
        group_size = int(metrics.get("group_size") or 1)
    except (TypeError, ValueError):
        group_size = 1
    # 表格容器已支持横向滚动，上限放宽到 25 列；再宽就退化为单值分栏，避免横向翻太久。
    if group_size > 25 or group_size < 1 or len(points) % group_size != 0:
        group_size = 1
    groups = len(points) // group_size

    # 控制限：limits[0] 是主图（I / X̄），limits[1] 是离散图（MR / R）
    def pick(index: int) -> tuple:
        if not limits or len(limits) <= index:
            return None, None
        row = limits[index]
        try:
            return _num(row[1]), _num(row[3])
        except (IndexError, TypeError):
            return None, None

    top_ucl, top_lcl = pick(0)
    sec_ucl, sec_lcl = pick(1)
    check = check or {}
    beyond_pts = set(check.get("beyond_points") or [])
    rule2_pts = set(check.get("rule2_points") or [])
    rule3_pts = set(check.get("rule3_points") or [])
    top_name = "I" if group_size <= 1 else "X̄"
    sec_name = "MR" if group_size <= 1 else "R"

    def state(value: float) -> str:
        if usl is not None and value > usl:
            return "上超差"
        if lsl is not None and value < lsl:
            return "下超差"
        return ""

    def beyond(value: float, ucl: Optional[float], lcl: Optional[float]) -> str:
        if ucl is not None and value > ucl:
            return "up"
        if lcl is not None and value < lcl:
            return "down"
        return ""

    # 统一小数位：按数据自身精度（最多 3 位），避免 40.5 与 40.090 混排
    decimals = 0
    for value in points:
        text = f"{value}".rstrip("0")
        if "." in text:
            decimals = max(decimals, len(text.split(".")[1]))
    decimals = min(decimals, 3)

    def num_text(value: float) -> str:
        return f"{value:.{decimals}f}"

    def deviation(value: float) -> str:
        if ref is None:
            return "—"
        delta = value - ref
        if abs(delta) < 0.5 * (10 ** -decimals):   # 避免出现 -0.00
            delta = 0.0
        return f"{delta:+.{decimals}f}"

    show_s = group_size > 5
    oos_total = sum(1 for p in points if state(p))

    if group_size > 1:
        group_means = [sum(points[g * group_size:(g + 1) * group_size]) / group_size
                       for g in range(groups)]
        group_ranges = [max(points[g * group_size:(g + 1) * group_size])
                        - min(points[g * group_size:(g + 1) * group_size]) for g in range(groups)]
    else:
        group_means, group_ranges = points, []
    beyond_groups = sum(1 for m in group_means if beyond(m, top_ucl, top_lcl))
    beyond_sec = sum(1 for r in group_ranges if beyond(r, sec_ucl, sec_lcl))

    meta = [
        f"样本量 <b>{len(points)}</b>",
        f"分组 <b>{groups} × {group_size}</b>" if group_size > 1 else "单值序列",
        f"均值 <b>{_fmt(mean, 3)}</b>" if mean is not None else "",
        f"超差 <b>{oos_total}</b> 点",
        f"判异 <b>{beyond_groups}</b> {'组' if group_size > 1 else '点'}" if top_ucl is not None else "",
    ]
    meta_html = '<div class="dmeta">' + "".join(f"<span>{m}</span>" for m in meta if m) + "</div>"
    toolbar = (
        '<div class="dtool"><input type="search" placeholder="筛选：数值 / 超差 / 判异" '
        'aria-label="筛选原始数据"><span class="dcount">共 %d 行</span></div>' % groups
        if group_size > 1 else
        '<div class="dtool"><input type="search" placeholder="筛选：数值 / 超差 / 判异" '
        'aria-label="筛选原始数据"><span class="dcount">共 %d 点</span></div>' % len(points)
    )

    if group_size > 1:
        head = "".join(f"<th>X{i + 1}</th>" for i in range(group_size))
        s_head = "<th>s</th>" if show_s else ""
        head = (f"<thead><tr><th>子组</th><th>观测号</th>{head}"
                f"<th>均值</th><th>R</th>{s_head}<th>判异</th><th>判定</th></tr></thead>")
        rows = []
        s_values = []
        for g in range(groups):
            chunk = points[g * group_size:(g + 1) * group_size]
            first_no = g * group_size + 1
            last_no = first_no + group_size - 1
            cells, hit_spec = [], False
            for value in chunk:
                bad = bool(state(value))
                hit_spec = hit_spec or bad
                cells.append(f'<td class="oos">{num_text(value)}</td>' if bad else f"<td>{num_text(value)}</td>")

            g_mean = group_means[g]
            rng = group_ranges[g]
            mean_hit = beyond(g_mean, top_ucl, top_lcl)
            r_hit = beyond(rng, sec_ucl, sec_lcl)

            # 越限 = 单点超控制限；同侧 / 趋势 = 规则命中。三类都标在同一列，
            # 这样控制图说"不受控"时，表里一定能找到对应的子组，不会两边对不上。
            marks = []
            if (g + 1) in beyond_pts or mean_hit:
                marks.append(f"{top_name}{'↑' if mean_hit != 'down' else '↓'}")
            if r_hit:
                marks.append(f"{sec_name}↑")
            if (g + 1) in rule2_pts:
                marks.append("同侧")
            if (g + 1) in rule3_pts:
                marks.append("趋势")
            mark_text = " ".join(marks)

            mean_cls = ' class="ooc"' if mean_hit else ""
            bar = ""
            if sec_ucl:
                ratio = max(0.0, min(rng / sec_ucl, 1.0))
                bar = f'<i style="width:{ratio * 100:.0f}%"></i>'
            r_cls = ' class="rcell ooc"' if r_hit else ' class="rcell"'

            if hit_spec:
                verdict = '<span class="vt bad">超差</span>'
            elif marks:
                verdict = '<span class="vt warn">异常</span>'
            else:
                verdict = '<span class="vt">正常</span>'

            s_cell = ""
            if show_s:
                sigma = (sum((v - g_mean) ** 2 for v in chunk) / (len(chunk) - 1)) ** 0.5 if len(chunk) > 1 else 0.0
                s_values.append(sigma)
                s_cell = f"<td>{sigma:.{decimals}f}</td>"

            rows.append(
                f'<tr><td>{g + 1}</td><td class="pt-no">#{first_no}–{last_no}</td>{"".join(cells)}'
                f"<td{mean_cls}>{g_mean:.{decimals}f}</td>"
                f"<td{r_cls}>{bar}{rng:.{decimals}f}</td>{s_cell}"
                f"<td class=\"marks\">{mark_text}</td><td>{verdict}</td></tr>"
            )

        grand = sum(points) / len(points)
        rbar = sum(group_ranges) / groups
        s_foot = f"<td>s̄ {sum(s_values) / len(s_values):.{decimals}f}</td>" if show_s else ""
        foot = (
            f'<tfoot><tr><td>合计</td>'
            f'<td colspan="{group_size + 1}">总均值 {grand:.{decimals}f}</td>'
            f"<td>R̄ {rbar:.{decimals}f}</td>{s_foot}"
            f'<td colspan="2">{groups} 组 · 越限 {beyond_groups + beyond_sec} 处</td></tr></tfoot>'
        )
        table = (
            '<div class="dtable-wrap"><table class="dtable"><caption>原始测量值 · 按子组展开</caption>'
            f"{head}<tbody>{''.join(rows)}</tbody>{foot}</table></div>"
        )
        legend = (f"观测号与 {top_name} 图横轴一致，图上第 N 点可直接落到对应行；"
                  f"红=超规格，琥珀=控制图判异（{top_name}/{sec_name} 超限、连续同侧、单调趋势）；"
                  f"R 列下方细条为 R ÷ UCL。")
        return (meta_html + toolbar + table
                + f'<div class="dlegend">{legend}表头可点击排序。</div>' + _TABLE_JS)

    # 单值序列：分栏，每栏 25 行
    chunk_size = 25 if len(points) > 30 else max(len(points), 1)
    panels = []
    for start in range(0, len(points), chunk_size):
        chunk = points[start:start + chunk_size]
        body = []
        for offset, value in enumerate(chunk):
            bad = state(value)
            index = start + offset + 1
            hit = beyond(value, top_ucl, top_lcl) or (index in beyond_pts)
            tags = []
            if hit:
                tags.append("越限")
            if index in rule2_pts:
                tags.append("同侧")
            if index in rule3_pts:
                tags.append("趋势")
            if bad:
                cls, mark = ' class="oos"', '<span class="vt bad">超差</span>'
            elif tags:
                cls, mark = ' class="ooc"', f'<span class="vt warn">{"·".join(tags)}</span>'
            else:
                cls, mark = "", '<span class="vt">正常</span>'
            body.append(
                f'<tr><td>{start + offset + 1}</td><td{cls}>{num_text(value)}</td>'
                f"<td>{deviation(value)}</td><td>{mark}</td></tr>"
            )
        panels.append(
            '<div class="dtable-wrap"><table class="dtable">'
            f'<caption>第 {start + 1}–{start + len(chunk)} 点</caption>'
            "<thead><tr><th>#</th><th>测量值</th><th>偏差</th><th>判定</th></tr></thead>"
            f"<tbody>{''.join(body)}</tbody></table></div>"
        )
    legend = "偏差 = 测量值 − " + ("目标值" if target is not None else "样本均值")
    return (meta_html + toolbar + f'<div class="dtable-grid">{"".join(panels)}</div>'
            + f'<div class="dlegend">{legend}；红=超规格，琥珀=超 {top_name} 图控制限。表头可点击排序。</div>'
            + _TABLE_JS)


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


def _gate_block(gate: dict[str, Any]) -> str:
    """判定链：受控 → 正态 → 能力，按专业顺序给出通过/不通过。"""
    if not gate:
        return ""

    def step(title: str, detail: str, state: str, tag: str) -> str:
        return (
            f'<div class="gstep {state}"><span class="gdot"></span>'
            f"<div><b>{html.escape(title)}</b><small>{html.escape(detail)}</small></div>"
            f"<em>{html.escape(tag)}</em></div>"
        )

    controlled = gate.get("controlled")
    normal = gate.get("normal")
    cpk = gate.get("cpk")
    grade = str(gate.get("grade") or "")

    if controlled is True:
        ctl = step("① 过程受控", gate.get("control_note") or "无越限点", "ok", "通过")
    elif controlled is False:
        ctl = step("① 过程受控", gate.get("control_note") or "存在越限点", "bad", "不通过")
    else:
        ctl = step("① 过程受控", "未提供控制图", "warn", "未评估")

    if normal is True:
        nrm = step("② 数据正态", gate.get("normal_note") or "p ≥ 0.05", "ok", "通过")
    elif normal is False:
        nrm = step("② 数据正态", gate.get("normal_note") or "p < 0.05", "warn", "偏离")
    else:
        nrm = step("② 数据正态", "未做正态性检验", "warn", "未评估")

    lab = gate.get("verdict") or "—"
    detail = f"Cpk {cpk} · 档位 {grade}"
    state = "ok" if grade in {"A+", "A", "B"} else ("warn" if grade == "C" else "bad")
    cap = step("③ 过程能力", detail, state, lab)

    return f'<div class="gate">{ctl}{nrm}{cap}</div>'


def _limits_table(rows: list[tuple[str, Any, Any, Any]]) -> str:
    body = "".join(
        f"<tr><td>{html.escape(name)}</td><td>{_fmt(ucl, 4)}</td>"
        f"<td>{_fmt(cl, 4)}</td><td>{_fmt(lcl, 4)}</td></tr>"
        for name, ucl, cl, lcl in rows
    )
    return (
        '<div class="dtable-wrap"><table class="limits">'
        "<thead><tr><th>控制图</th><th>UCL</th><th>CL</th><th>LCL</th></tr></thead>"
        f"<tbody>{body}</tbody></table></div>"
    )


def _control_body(control: dict[str, Any], show_verdict: bool = True) -> str:
    """控制图区块：判定 + 图 + 控制限 + 判异明细。"""
    if not control or not control.get("available"):
        return '<p class="mnote">本次未生成控制图（可在分析时启用）。</p>'

    check = control.get("check") or {}
    controlled = check.get("controlled")
    pill = (
        '<span class="pill ok">过程受控</span>' if controlled
        else '<span class="pill bad">过程不受控</span>' if controlled is False
        else '<span class="pill warn">无法判定</span>'
    )
    unit = "个观测点" if (control.get("size") or 1) <= 1 else "个子组"
    beyond_text = f"越限 {check.get('beyond') or 0} 点"
    if check.get("beyond_second"):
        beyond_text += f"（{check.get('second_label') or 'MR'} 图另有 {check['beyond_second']} 点越限）"
    note = (
        f"{check.get('points') or 0} {unit} · {beyond_text} · "
        f"最长连续同侧 {check.get('longest_run') or 0} 点 · 最长趋势 {check.get('longest_trend') or 0} 点"
    )

    chart_url = control.get("chart_url") or ""
    figure = ""
    if chart_url:
        figure = (
            '<div class="mtb-figure"><a href="' + html.escape(chart_url, quote=True) +
            '" target="_blank" rel="noopener"><img src="' + html.escape(chart_url, quote=True) +
            '" alt="控制图" loading="lazy"></a>'
            f'<figcaption>{html.escape(control.get("title") or "控制图")} · 由 BQL / Minitab 引擎生成，点击查看原图</figcaption></div>'
        )

    limits = _limits_table(control.get("limits") or [])

    rules = [
        ("规则 1：任一点超出控制限", bool(check.get("beyond"))),
        (f"规则 2：连续 8 点位于中心线同一侧（最长 {check.get('longest_run') or 0}）", bool(check.get("rule2"))),
        (f"规则 3：连续 6 点单调升/降（最长 {check.get('longest_trend') or 0}）", bool(check.get("rule3"))),
    ]
    hits = "".join(
        f'<li class="{"hit" if bad else ""}">{"命中 · " if bad else ""}{html.escape(text)}</li>'
        for text, bad in rules
    )

    verdict_line = (
        "过程稳定，能力指数的计算前提成立。" if controlled
        else "过程存在异常信号，能力指数仅供参考，应先查明特殊原因并剔除后再评估能力。"
    )

    tail = (
        f'<p class="msa" style="border-left-color:var(--{"success" if controlled else "danger"})">'
        f"{html.escape(verdict_line)}</p>"
    ) if show_verdict else ""
    head = (
        f'<div class="ctl-head">{pill}<span class="ctl-note">{html.escape(note)}</span></div>'
        if show_verdict else f'<div class="ctl-head">{pill}</div>'
    )
    return (
        f"{head}"
        f"{figure}"
        f'<div class="two" style="margin-top:4px">'
        f'<div><div class="mtb-h2">控制限</div>{limits}</div>'
        f'<div><div class="mtb-h2">判异规则（Nelson 常用三条）</div><ul class="rules">{hits}</ul></div>'
        f"</div>"
        f"{tail}"
    )


def _normality_body(norm: dict[str, Any]) -> str:
    """正态性检验：AD 统计量 + p 值 + 偏度峰度 + 概率图。"""
    if not norm or not norm.get("ad"):
        return '<p class="mnote">样本量不足，未做正态性检验（需 n ≥ 8）。</p>'

    normal = bool(norm.get("is_normal"))
    pill = '<span class="pill ok">符合正态</span>' if normal else '<span class="pill warn">偏离正态</span>'

    rows = [
        ("Anderson-Darling 统计量", _fmt(norm.get("ad"), 3)),
        ("p 值", _fmt(norm.get("p_value"), 3)),
        ("偏度", f"{norm.get('skewness'):+.2f}" if norm.get("skewness") is not None else "—"),
        ("峰度", f"{norm.get('kurtosis'):+.2f}" if norm.get("kurtosis") is not None else "—"),
        ("样本量 N", str(norm.get("n") or "")),
    ]
    stats_html = "".join(
        f'<div class="mrow"><div class="mlabel">{html.escape(k)}</div>'
        f'<div class="mvalue">{html.escape(str(v))}</div></div>'
        for k, v in rows
    )

    chart_url = norm.get("chart_url") or ""
    figure = ""
    if chart_url:
        figure = (
            '<div class="mtb-figure"><a href="' + html.escape(chart_url, quote=True) +
            '" target="_blank" rel="noopener"><img src="' + html.escape(chart_url, quote=True) +
            '" alt="正态概率图" loading="lazy"></a>'
            '<figcaption>正态概率图 · 点大致沿直线分布即符合正态</figcaption></div>'
        )

    if normal:
        note = "p ≥ 0.05，不能拒绝正态假设：Cpk / Ppk 的计算前提成立，指数可直接用于判定。"
    else:
        note = ("p < 0.05，数据偏离正态：Cpk / Ppk 仅为近似值。建议先查混料、双峰或长尾原因，"
                "必要时用 Box-Cox 变换或非正态能力分析（如 Johnson 变换）后再评估。")

    return (
        f'<div class="ctl-head">{pill}<span class="ctl-note">显著性水平 α = 0.05</span></div>'
        f'<div class="two"><div>{figure}</div><div>{stats_html}</div></div>'
        f'<p class="msa" style="border-left-color:var(--{"success" if normal else "warn"})">{html.escape(note)}</p>'
    )


def _capability_extra(cap: dict[str, Any]) -> str:
    """能力补充：置信下限、Cpk/Ppk 差归因、改善潜力、MSA 提示。"""
    if not cap:
        return ""
    bd = cap.get("breakdown") or {}
    po = cap.get("potential") or {}

    def pct(value: Any, digits: int = 1) -> str:
        number = _num(value)
        return f"{number * 100:.{digits}f}%" if number is not None else "—"

    rows: list[tuple[str, str, str]] = []
    cpk_lo = _num(cap.get("cpk_lower"))
    if cpk_lo is not None:
        rows.append(("Cpk 95% 置信下限", _fmt(cpk_lo, 3), f"按 n={_fmt(cap.get('n'), 0)} 的单侧 95% 下限，客户审核常看这个数"))
    if bd.get("offset") is not None:
        rows.append(("中心偏移", f"{bd['offset']:+.3f}", f"均值相对目标，占公差带 {pct(bd.get('offset_ratio'))}"))
    if bd.get("centering_loss") is not None:
        rows.append(("中心化损失", _fmt(bd["centering_loss"], 3), "Cp 与 Cpk 之差，纯靠调中心就能收回的能力"))
    if bd.get("between_loss") is not None:
        rows.append(("组间波动损失", _fmt(bd["between_loss"], 3), f"Cp 与 Pp 之差，组间漂移占方差 {pct(bd.get('between_share'))}"))
    if po.get("cpk_if_centered") is not None:
        rows.append(("中心化后 Cpk", _fmt(po["cpk_if_centered"], 3), "把均值移到目标、波动不变时的能力"))
    if po.get("sigma_needed") is not None:
        rows.append((
            f"达到 Cpk {po.get('goal')} 所需 σ",
            _fmt(po["sigma_needed"], 4),
            f"当前 {_fmt(po.get('sigma_now'), 4)}，需再降 {_fmt(po.get('sigma_cut_pct'), 1)}%",
        ))

    body = "".join(
        f'<div class="mrow"><div class="mlabel">{html.escape(k)}</div>'
        f'<div class="mnote">{html.escape(n)}</div>'
        f'<div class="mvalue">{html.escape(v)}</div></div>'
        for k, v, n in rows
    )
    return body + f'<p class="msa">{html.escape(cap.get("msa_note") or "")}</p>'


def _lookup(values: dict[str, Any], *names: str) -> Optional[float]:
    """按名称取数值：先精确匹配，再忽略 Minitab 的尾部序号。"""
    for name in names:
        if name in values:
            return _num(values[name])
    stripped = {re.sub(r"\d+$", "", str(k)): v for k, v in values.items()}
    for name in names:
        if name in stripped:
            return _num(stripped[name])
    return None


def _subgroup_means(points: list[float], size: int) -> list[float]:
    if size <= 1:
        return list(points)
    return [
        sum(points[i:i + size]) / len(points[i:i + size])
        for i in range(0, len(points) - size + 1, size)
    ]


def control_summary(result: dict[str, Any], payload: dict[str, Any], kind: str = "xr") -> dict[str, Any]:
    """从控制图接口结果里取出控制限，并做判异检查。"""
    metrics = extract_metrics(result, payload)
    values = metrics.get("values") or {}
    points = metrics.get("points") or []

    ucl0 = _lookup(values, "ucl0")
    cl0 = _lookup(values, "centerLine0")
    lcl0 = _lookup(values, "lcl0")
    ucl1 = _lookup(values, "ucl1")
    cl1 = _lookup(values, "centerLine1")
    lcl1 = _lookup(values, "lcl1")
    if cl0 is None or ucl0 is None:
        return {"available": False}

    try:
        size = int(payload.get("group_size") or values.get("sampSize") or 1)
    except (TypeError, ValueError):
        size = 1
    means = _subgroup_means(points, size)
    check = quality.control_check(means, cl0, ucl0, lcl0)

    top_label = f"X̄ (n={size})" if kind in {"xr", "xbar"} and size > 1 else "I"
    second_label = "R" if kind in {"xr", "xbar"} and size > 1 else "MR"

    # 离散图（R / MR）只判超限：连续同侧与趋势规则不适用于极差序列，
    # 但超限一定要算，否则 I 图正常、MR 图超限时会误判为受控。
    if cl1 is not None and ucl1 is not None:
        if size > 1:
            dispersion = [
                max(points[i:i + size]) - min(points[i:i + size])
                for i in range(0, len(points) - size + 1, size)
            ]
        else:
            dispersion = [abs(points[i] - points[i - 1]) for i in range(1, len(points))]
        beyond_disp = [
            index + 1 for index, value in enumerate(dispersion)
            if value > ucl1 or (lcl1 is not None and value < lcl1)
        ]
        check["beyond_second"] = len(beyond_disp)
        check["top_label"] = top_label
        check["beyond_second_points"] = beyond_disp
        check["second_label"] = second_label
        check["observation_label"] = "观测点" if size <= 1 else "子组"
        if beyond_disp:
            check["controlled"] = False
    limits = []
    if cl0 is not None:
        limits.append((top_label, ucl0, cl0, lcl0))
    if cl1 is not None:
        limits.append((second_label, ucl1, cl1, lcl1))

    return {
        "available": True,
        "kind": kind,
        "chart_url": _graph_url(result),
        "title": f"{top_label}-{second_label} 控制图",
        "size": size,
        "limits": limits,
        "check": check,
    }


def normality_summary(result: Optional[dict[str, Any]], payload: dict[str, Any]) -> dict[str, Any]:
    """正态性结论以本地 Anderson-Darling 为准，概率图取 Minitab 输出。"""
    metrics = extract_metrics(result or {}, payload)
    points = metrics.get("points") or []
    stats = quality.anderson_darling(points)
    if not stats:
        return {}
    stats["chart_url"] = _graph_url(result or {})
    return stats


def capability_summary(result: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    metrics = extract_metrics(result, payload)
    n = metrics.get("n") or len(metrics.get("points") or [])
    return {
        "cpk_lower": quality.cpk_lower_bound(metrics.get("cpk"), n),
        "n": n,
        "breakdown": quality.capability_breakdown(metrics),
        "potential": quality.improvement_potential(metrics),
        "msa_note": quality.MSA_NOTE,
    }


def build_extra(
    cap_result: dict[str, Any],
    payload: dict[str, Any],
    control_result: Optional[dict[str, Any]] = None,
    norm_result: Optional[dict[str, Any]] = None,
    control_kind: str = "xr",
    meta: Optional[list[tuple[str, str]]] = None,
    disposition_cfg: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """把能力 / 控制图 / 正态性三份结果合成报告需要的附加区块。"""
    metrics = extract_metrics(cap_result, payload)
    status = capability_status(metrics.get("cpk"))

    control = control_summary(control_result, payload, control_kind) if control_result else {"available": False}
    norm = normality_summary(norm_result, payload) if norm_result else {}
    cap = capability_summary(cap_result, payload)

    check = control.get("check") or {}
    control_note = ""
    if control.get("available"):
        if check.get("controlled"):
            control_note = "无越限点"
        else:
            bits = []
            if check.get("beyond") or check.get("beyond_second"):
                part = f"越限 {check.get('beyond') or 0} 点"
                if check.get("beyond_second"):
                    part = (f"{check.get('top_label') or 'I'} {check.get('beyond') or 0} 点"
                            f" / {check.get('second_label') or 'MR'} {check['beyond_second']} 点")
                bits.append(part)
            if check.get("rule2"):
                bits.append(f"连续同侧 {check.get('longest_run')} 点")
            if check.get("rule3"):
                bits.append(f"趋势 {check.get('longest_trend')} 点")
            control_note = " · ".join(bits) or "存在异常信号"
    normal_note = f"AD {_fmt(norm.get('ad'), 3)} · p {_fmt(norm.get('p_value'), 3)}" if norm else ""

    gate = {
        "controlled": check.get("controlled") if control.get("available") else None,
        "control_note": control_note,
        "normal": norm.get("is_normal") if norm else None,
        "normal_note": normal_note,
        "cpk": _fmt(metrics.get("cpk"), 3),
        "grade": status["grade"],
        "verdict": status["label"],
    }

    def card(idx: str, title: str, note: str, body: str) -> str:
        if not body:
            return ""
        cls = "card gate-card" if idx == "判定链" else "card"
        return (
            f'<section class="{cls}"><div class="sec"><span class="idx">{idx}</span>'
            f"<h3>{html.escape(title)}</h3><span class=\"note\">{html.escape(note)}</span></div>"
            f"{body}</section>"
        )

    has_flow = bool(control.get("available")) or bool(norm)
    ppm_out = int((_num(metrics.get("out_usl")) or 0) + (_num(metrics.get("out_lsl")) or 0))
    observed_out = metrics.get("observed_out")
    # 「超差 N 件」必须是实测件数；PPM 只用于“预期超差”口径
    out_of_spec = int(observed_out) if observed_out is not None else ppm_out
    disp = disposition(
        check.get("controlled") if control.get("available") else None,
        metrics.get("cpk"), out_of_spec, disposition_cfg,
    )
    anomaly_points = (set(check.get("beyond_points") or [])
                      | set(check.get("rule2_points") or [])
                      | set(check.get("rule3_points") or []))
    n_for_ci = _num(metrics.get("n")) or len(metrics.get("points") or [])
    cpk_bounds = quality.cpk_bounds(_num(metrics.get("cpk")), n_for_ci)
    # 全绿：受控 + 正态 + 达到放行阈值。此时证据层收进折叠区，报告只留一屏。
    all_green = disp.get("key") == "release" and norm.get("is_normal") is True

    # 首屏主图服从 SPC 判读顺序：有特殊原因时先看控制图；过程受控后再看能力图。
    # 完整 Minitab 输出仍在附录保留，主图只承担快速定位证据的职责。
    control_graph = _graph_url(control_result or {})
    capability_graph = _graph_url(cap_result)
    control_doc = _doc_url(control_result or {})
    capability_doc = _doc_url(cap_result)
    control_check = control.get("check") or {}
    control_is_abnormal = bool(control.get("available") and control_check.get("controlled") is False)
    if control_is_abnormal and control_graph:
        primary_graph = control_graph
        primary_doc = control_doc or capability_doc
        primary_title = "Minitab 控制图"
        primary_caption = "过程稳定性是能力指数的前提；本图优先用于定位特殊原因信号。"
    elif capability_graph:
        primary_graph = capability_graph
        primary_doc = capability_doc or control_doc
        primary_title = "Minitab 能力分析图"
        primary_caption = "过程受控后，优先查看分布、中心位置与规格限的关系。"
    elif control_graph:
        primary_graph = control_graph
        primary_doc = control_doc or capability_doc
        primary_title = "Minitab 控制图"
        primary_caption = "本次未返回能力分析图，先展示可用的过程稳定性证据。"
    else:
        primary_graph = ""
        primary_doc = capability_doc or control_doc
        primary_title = "Minitab 分析图"
        primary_caption = "本次结果未附带 Minitab 图表。"

    return {
        "all_green": all_green,
        # 旧键：保留给用户自定义模板，内容与新区块等价
        "gate_section": card("判定链", "受控 · 正态 · 能力", "能力指数的前置条件", _gate_block(gate)) if has_flow else "",
        "control_section": card("02", "过程稳定性 · 控制图", "判异规则 · 特殊原因", _control_body(control)),
        "normality_section": card("03", "正态性检验", "Anderson-Darling", _normality_body(norm)),
        "capability_extra": _capability_extra(cap),
        "meta": meta,
        # 新键：三层披露
        "disposition": disp,
        "verdict_hero": _verdict_hero(
            disp, status, metrics, cap.get("cpk_lower"), out_of_spec,
            has_evidence=has_flow and not all_green,
            controlled=check.get("controlled") if control.get("available") else None,
            bounds=cpk_bounds, anomalies=len(anomaly_points),
        ),
        "gate_strip": _gate_strip(gate),
        "primary_graph_url": primary_graph,
        "primary_doc_url": primary_doc,
        "primary_graph_title": primary_title,
        "primary_graph_caption": primary_caption,
        "control_html": _control_body(control, show_verdict=False) if control.get("available") else "",
        "concl_control": conclusion_control(control),
        "concl_capability": conclusion_capability(cap, metrics),
        "action_plan": _action_plan(control, cap, metrics),
        "normality_body": _normality_body(norm) if norm else "",
        "normality_meta": (
            f"AD {_fmt(norm.get('ad'), 3)} · p {_fmt(norm.get('p_value'), 3)}" if norm else ""
        ),
        "out_of_spec": out_of_spec,
        "cpk_lower": cap.get("cpk_lower"),
        "controlled": check.get("controlled") if control.get("available") else None,
        "is_normal": norm.get("is_normal") if norm else None,
        "control_limits": control.get("limits") or [],
        "control_check": check,
    }


# ---------------------------------------------------------------- 三层披露
# L1 判决 / L2 证据 / L3 附录。模板只负责配色与版式，阅读顺序由这里统一决定，
# 因此所有内置模板与用户自定义模板拿到的是同一套信息层级。
DEFAULT_DISPOSITION = {"release_cpk": 1.33, "hold_cpk": 1.00}


def disposition(
    controlled: Optional[bool],
    cpk: Any,
    out_of_spec: Any,
    thresholds: Optional[dict[str, Any]] = None,
) -> dict[str, str]:
    """放行判决：车间的语言，先于能力等级出现。阈值可由 config.report.disposition 覆盖。

    判据优先级：有超差 → 拒收；过程不受控 → 有条件放行（能力数不可采信）；
    随后才按 Cpk 与 release_cpk / hold_cpk 分档。
    """
    cfg = {**DEFAULT_DISPOSITION, **(thresholds or {})}
    value = _num(cpk)
    if value is None:
        return {
            "key": "unknown", "label": "无法判定", "tone": "neutral",
            "reason": "缺少能力指数，无法给出放行结论。",
        }
    release = _num(cfg.get("release_cpk")) or 1.33
    hold = _num(cfg.get("hold_cpk")) or 1.00
    bad = int(_num(out_of_spec) or 0)

    if bad > 0:
        return {
            "key": "reject", "label": "拒收", "tone": "bad",
            "reason": f"样本中已有 {bad} 件超出规格，批次需筛选或返工后方可重新评估。",
        }
    if controlled is False:
        return {
            "key": "conditional", "label": "有条件放行", "tone": "warn",
            "reason": (f"过程存在特殊原因信号，Cpk {_fmt(value, 3)} 不能直接采信；"
                       "查明并剔除异常点、确认过程受控后可放行。"),
        }
    if controlled is None and value >= release:
        return {
            "key": "unverified", "label": "有条件放行", "tone": "warn",
            "reason": (f"Cpk {_fmt(value, 3)} 已达 {release:g}，但本次未做控制图，"
                       "过程是否受控未经确认；补做控制图后可判放行。"),
        }
    if value >= release:
        return {
            "key": "release", "label": "放行", "tone": "good",
            "reason": f"Cpk {_fmt(value, 3)} ≥ {release:g}，过程受控，能力满足常规放行要求。",
        }
    if value >= hold:
        return {
            "key": "conditional", "label": "有条件放行", "tone": "warn",
            "reason": (f"Cpk {_fmt(value, 3)} 未达 {release:g}，但无超差；"
                       "建议加严出厂检验，并按下方建议收敛能力后放行。"),
        }
    return {
        "key": "reject", "label": "拒收", "tone": "bad",
        "reason": f"Cpk {_fmt(value, 3)} 低于 {hold:g}，过程能力不足，批次不宜放行。",
    }


def _verdict_hero(
    disp: dict[str, str],
    status: dict[str, str],
    metrics: dict[str, Any],
    cpk_lower: Any = None,
    out_of_spec: Optional[int] = None,
    has_evidence: bool = True,
    controlled: Optional[bool] = None,
    bounds: Optional[tuple] = None,
    anomalies: Optional[int] = None,
) -> str:
    """首屏判决书：一个判决 + 三数字 + 一行可信边界 + 一个去向。

    过程不受控时，Cpk 会被降为"参考值"并让位给判异点数——这不是排版偏好。
    Cpk 的前提是过程受控，把不可采信的数字用最大字号摆出来，等于让读者记住一个
    没法用的结论；先让他看到"这个数不能用"，再看这个数是多少。
    """
    n_value = _num(metrics.get("n"))
    n_text = _fmt(n_value, 0) if n_value is not None else str(len(metrics.get("points") or []) or "—")
    bad = out_of_spec
    if bad is None:
        bad = int((_num(metrics.get("out_usl")) or 0) + (_num(metrics.get("out_lsl")) or 0))
    lower = _num(cpk_lower)
    cpk = _fmt(metrics.get("cpk"), 3)

    if controlled is False:
        cells = [
            ("判异", f"{int(anomalies or 0)} 点", "特殊原因信号", "hot"),
            ("Cpk", cpk, "参考值 · 不可用于判定", "ref"),
            ("超差", f"{bad} 件", f"n = {n_text}", ""),
        ]
    else:
        cells = [
            ("Cpk", cpk, f"95% 下限 {_fmt(lower, 3)}" if lower is not None else "组内能力", ""),
            ("Ppk", _fmt(metrics.get("ppk"), 3), "整体表现", ""),
            ("超差", f"{bad} 件", f"n = {n_text}", ""),
        ]

    nums = "".join(
        f'<div class="hn{" " + cls if cls else ""}"><span>{html.escape(label)}</span>'
        f'<b>{html.escape(value)}</b><em>{html.escape(cap)}</em></div>'
        for label, value, cap, cls in cells
    )

    bound_html = ""
    if bounds and bounds[0] is not None and bounds[1] is not None:
        bound_html = (
            f'<p class="hbound">基于 {n_text} 件样本 · Cpk 95% 区间 '
            f"{bounds[0]:.3f}–{bounds[1]:.3f}（下一批可能落在此范围）"
            + ("　·　过程不受控，该区间亦仅供参考" if controlled is False else "")
            + "</p>"
        )

    nxt = '<a class="hnext" href="#ev-action">看改善建议 ↓</a>' if has_evidence else ""
    # 过程不受控时 Cpk 已降级为参考值，等级徽标必须同步降级，否则读者会拿
    # "C 级 · 能力可接受" 当成判决依据——这正是首屏阻断要避免的误读。
    grade_text = (f'{status.get("grade") or "—"} 级 · {status.get("label") or ""}'
                  + ("（参考）" if controlled is False else ""))
    return (
        f'<section class="hero t-{disp.get("tone") or "neutral"}">'
        '<div class="hmain">'
        f'<div class="hdisp">{html.escape(disp.get("label") or "—")}</div>'
        f'<div class="hgrade">{html.escape(grade_text)}</div>'
        f'<p class="hreason">{html.escape(disp.get("reason") or "")}</p>'
        '</div>'
        f'<div class="hnums">{nums}</div>{bound_html}{nxt}</section>'
    )


def _gate_strip(gate: dict[str, Any]) -> str:
    """判定链压成一行：受控 · 正态 · 能力，三列状态条。"""
    if not gate:
        return ""
    grade = str(gate.get("grade") or "—")
    items: list[tuple[str, str, str, str]] = []

    controlled = gate.get("controlled")
    if controlled is True:
        items.append(("ok", "过程受控", "通过", gate.get("control_note") or "无越限点"))
    elif controlled is False:
        items.append(("bad", "过程受控", "不通过", gate.get("control_note") or "存在异常信号"))
    else:
        items.append(("skip", "过程受控", "未评估", "未提供控制图"))

    normal = gate.get("normal")
    if normal is True:
        items.append(("ok", "数据正态", "通过", gate.get("normal_note") or "p ≥ 0.05"))
    elif normal is False:
        items.append(("warn", "数据正态", "偏离", gate.get("normal_note") or "p < 0.05"))
    else:
        items.append(("skip", "数据正态", "未评估", "未做正态性检验"))

    tone = "ok" if grade in {"A+", "A", "B"} else ("warn" if grade == "C" else "bad")
    # 过程不受控时，能力等级同样不能作为结论，这里只保留数值做对照
    cap_note = "参考值" if controlled is False else (gate.get("verdict") or "")
    items.append((tone, "过程能力", f"{grade} · {gate.get('cpk') or '—'}", cap_note))

    cells = "".join(
        f'<div class="st {tone}"><i></i><div><span>{html.escape(name)}</span>'
        f'<b>{html.escape(tag)}</b></div><em>{html.escape(note)}</em></div>'
        for tone, name, tag, note in items
    )
    return f'<div class="strip">{cells}</div>'


def conclusion_control(control: dict[str, Any]) -> tuple[str, str]:
    """控制图结论带：一句话说清这张图告诉我们什么，读者不必自己读图。"""
    if not control or not control.get("available"):
        return "", "neutral"
    check = control.get("check") or {}
    if check.get("controlled"):
        return "过程受控，无特殊原因信号 → Cpk 可用于判断能力。", "ok"

    points = check.get("beyond_points") or []
    second = check.get("beyond_second") or 0
    second_label = check.get("second_label") or "MR"
    if points:
        head = "第 " + "、".join(str(int(p)) for p in points[:3]) + " 点超出控制限"
        head += f"，{second_label} 图同步越限 {second} 点" if second else f"，{second_label} 图正常"
        return head + " → 一次性特殊原因，先查工程原因再重算能力。", "bad"
    if check.get("rule2"):
        return (f"连续 {check.get('longest_run') or 0} 点在中心线同侧 → 过程存在偏移或漂移，"
                "属系统性原因，不是单点异常。"), "bad"
    if check.get("rule3"):
        return (f"连续 {check.get('longest_trend') or 0} 点单调变化 → 典型刀具磨损或参数漂移，"
                "应按趋势提前换刀或补偿。"), "bad"
    return "存在异常信号 → 能力指数仅供参考。", "warn"


def conclusion_capability(cap: dict[str, Any], metrics: dict[str, Any]) -> tuple[str, str]:
    """能力结论带：把 Cpk 与 Ppk 的差归因到「中心」还是「批间」，直接指向要动的地方。"""
    if not cap:
        return "", "neutral"
    bd = cap.get("breakdown") or {}
    po = cap.get("potential") or {}
    offset = _num(bd.get("offset"))
    loss = _num(bd.get("centering_loss"))
    share = _num(bd.get("between_share"))
    centered = _num(po.get("cpk_if_centered"))
    target = _num(metrics.get("target"))
    cpk = _num(metrics.get("cpk"))
    ppk = _num(metrics.get("ppk"))

    if share is not None and share >= 0.15:
        gap = (cpk - ppk) if (cpk is not None and ppk is not None) else None
        gap_text = f"，Cpk 与 Ppk 差 {_fmt(gap, 3)}" if gap is not None else ""
        return (f"组间波动占方差 {share * 100:.0f}%{gap_text} → "
                "改善重点在批间一致性，而不是单件精度。"), "warn"

    if offset is not None and loss is not None and abs(offset) > 0 and loss >= 0.03:
        side = "高" if offset > 0 else "低"
        where = f"目标 {_fmt(target, 3)}" if target is not None else "样本中心"
        tail = f"，调机即可回到 {_fmt(centered, 3)}（零成本）" if centered is not None else ""
        return (f"中心偏{side} {abs(offset):.3f}（{where}），吃掉 {loss:.3f} 的 Cpk{tail}。"), "warn"

    if cpk is not None and ppk is not None:
        return (f"Cpk {_fmt(cpk, 3)} 与 Ppk {_fmt(ppk, 3)} 接近 → 波动以普通原因为主，"
                "中心与分散均无显著偏移。"), "ok"
    return "", "neutral"


def _action_plan(control: dict[str, Any], cap: dict[str, Any], metrics: dict[str, Any]) -> str:
    """改善建议：三步以内，按成本排序，每步给出可验证的结果。"""
    check = (control or {}).get("check") or {}
    bd = (cap or {}).get("breakdown") or {}
    po = (cap or {}).get("potential") or {}
    cpk = _num(metrics.get("cpk"))
    steps: list[tuple[str, str, str, str]] = []

    points = check.get("beyond_points") or []
    if points:
        where = "、".join(str(int(p)) for p in points[:3])
        steps.append((f"查第 {where} 点的工程原因",
                      "刀具 / 装夹 / 测量 / 换料", "0 成本", "剔除后重算 Cpk"))
    elif check.get("rule2") or check.get("rule3"):
        steps.append(("定位漂移起点并校正",
                      "设备参数 / 刀具磨损 / 环境", "低", "恢复受控后再评估"))

    offset = _num(bd.get("offset"))
    centered = _num(po.get("cpk_if_centered"))
    target = _num(metrics.get("target"))
    if (offset is not None and centered is not None and abs(offset) > 0
            and (cpk is None or centered > cpk + 0.02)):
        aim = f"{_fmt(target, 3)}" if target is not None else "公差中心"
        steps.append((f"把均值调回 {aim}",
                      f"当前偏差 {offset:+.3f}", "0 成本", f"Cpk → {_fmt(centered, 3)}"))

    need = _num(po.get("sigma_needed"))
    now = _num(po.get("sigma_now"))

    # 单侧规格：没有“公差中心”可回，最省的改善是把均值往规格余量大的一侧平移
    usl = _num(metrics.get("usl"))
    lsl = _num(metrics.get("lsl"))
    mean = _num(metrics.get("mean"))
    single_side = (usl is not None) != (lsl is not None and lsl != 0)
    if (single_side and mean is not None and cpk is not None and cpk < 1.33
            and not any("均值" in s[0] for s in steps)):
        if usl is not None:
            margin = usl - mean
            steps.append((f"把均值从 {_fmt(mean, 2)} 下移，扩大上限侧余量",
                          f"当前距 USL {_fmt(usl, 2)} 仅 {_fmt(margin, 2)}", "0 成本",
                          "余量翻倍 ≈ Cpk 翻倍"))
        else:
            margin = mean - lsl
            steps.append((f"把均值从 {_fmt(mean, 2)} 上移，扩大下限侧余量",
                          f"当前距 LSL {_fmt(lsl, 2)} 仅 {_fmt(margin, 2)}", "0 成本",
                          "余量翻倍 ≈ Cpk 翻倍"))

    if need is not None and now is not None and need < now * 0.995:
        steps.append((f"把 σ 从 {now:.3f} 压到 {need:.3f}",
                      f"需再降 {_fmt(po.get('sigma_cut_pct'), 0)}%", "改工艺",
                      f"Cpk → {_fmt(po.get('goal'), 2)}"))

    if not steps:
        goal = _num(po.get("goal")) or 1.33
        if cpk is not None and cpk < goal:
            steps.append(("先做测量系统分析（MSA / Gage R&R）",
                          f"Cpk {_fmt(cpk, 3)} 未达 {_fmt(goal, 2)}，先排除测量误差", "低",
                          "确认 Cpk 可信后再定改善方向"))
        else:
            steps.append(("维持现状，按常规频次监控", "能力与稳定性均已满足要求", "—", "保持当前水平"))

    rows = "".join(
        f'<li><span class="pn">{index}</span>'
        f'<div class="pt"><b>{html.escape(title)}</b><small>{html.escape(note)}</small></div>'
        f'<em class="pc{" free" if cost == "0 成本" else ""}">{html.escape(cost)}</em>'
        f'<span class="pg">{html.escape(gain)}</span></li>'
        for index, (title, note, cost, gain) in enumerate(steps[:3], start=1)
    )
    msa = str((cap or {}).get("msa_note") or "")
    msa_html = f'<p class="msa">{html.escape(msa)}</p>' if msa else ""
    return f'<ol class="plist">{rows}</ol>{msa_html}'


def _evidence_card(
    idx: str,
    title: str,
    note: str,
    conclusion: tuple[str, str] | None,
    body: str,
    anchor: str = "",
) -> str:
    """L2 证据卡：结论带在上，图与表在下。没有内容就整块消失，不留空卡片。"""
    if not body:
        return ""
    head = (
        f'<div class="sec"><span class="idx">{html.escape(idx)}</span>'
        f"<h3>{html.escape(title)}</h3><span class=\"note\">{html.escape(note)}</span></div>"
    )
    band = ""
    if conclusion and conclusion[0]:
        text, tone = conclusion
        band = f'<div class="concl t-{tone}">{html.escape(text)}</div>'
    ident = f' id="{anchor}"' if anchor else ""
    return f'<section class="card ev"{ident}>{head}{band}{body}</section>'


def _fold(title: str, meta: str, body: str, open_by_default: bool = False) -> str:
    """L3 附录折叠区。标题行带关键值摘要，不展开也知道里面是什么。"""
    if not body:
        return ""
    attr = " open" if open_by_default else ""
    meta_html = f"<em>{html.escape(meta)}</em>" if meta else ""
    return (
        f'<details class="fold"{attr}><summary><span class="fmark"></span>'
        f"<b>{html.escape(title)}</b>{meta_html}</summary>"
        f'<div class="fbody">{body}</div></details>'
    )


# 报告是单文件 HTML，但编辑状态需要能回写到本地报告目录。编辑器只依赖
# 浏览器原生 Drag and Drop / fetch，因此打开报告时不需要额外前端依赖。
EDITOR_STYLE = r"""<style>
.report-block{position:relative}
.analyst-note{padding:12px 15px;margin:0 0 16px;border:1px solid var(--line,#dfe4ec);border-left:3px solid var(--accent,#4f46e5);border-radius:8px;background:var(--surface2,#f7f9fc)}
.analyst-note b{font-size:11px;color:var(--muted,#5a6678)}
.analyst-note p{margin:4px 0 0;white-space:pre-wrap}
.report-editor-toolbar{position:fixed;right:16px;top:16px;z-index:120;width:344px;max-height:calc(100vh - 32px);overflow:auto;
  padding:18px;background:rgba(255,255,255,.98);border:1px solid var(--line,#dfe4ec);
  border-radius:12px;box-shadow:0 18px 48px rgba(16,24,40,.18);font:12px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC",sans-serif;color:var(--text,#0f172a)}
.report-editor-toolbar[hidden]{display:none}
.report-editor-toolbar [hidden]{display:none!important}
.report-editor-toolbar .re-head{display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:16px;font-weight:650;font-size:14px}
.report-editor-toolbar .re-close{width:26px;height:26px;border:0;background:transparent;color:var(--muted,#5a6678);font-size:20px;line-height:1;cursor:pointer;border-radius:6px}
.report-editor-toolbar .re-close:hover{background:var(--surface2,#f7f9fc)}
.report-editor-toolbar .re-field{display:block;margin:0 0 12px;color:var(--muted,#5a6678);font-size:11px}
.report-editor-toolbar input:not([type="checkbox"]),.report-editor-toolbar textarea,.report-editor-toolbar select{display:block;width:100%;margin-top:5px;border:1px solid var(--line,#dfe4ec);background:#fff;color:var(--text,#0f172a);border-radius:7px;padding:8px 9px;font:12px/1.45 inherit;outline:none}
.report-editor-toolbar textarea{min-height:68px;resize:vertical}
.report-editor-toolbar input:focus,.report-editor-toolbar textarea:focus,.report-editor-toolbar select:focus{border-color:var(--accent,#4f46e5);box-shadow:0 0 0 3px color-mix(in srgb,var(--accent,#4f46e5) 14%,transparent)}
.report-editor-toolbar input[type="checkbox"]{width:15px;height:15px;flex:none;margin:0;padding:0}
.report-editor-toolbar .re-section-title{display:flex;justify-content:space-between;align-items:center;margin:16px 0 7px;padding-top:14px;border-top:1px solid var(--line,#dfe4ec);font-size:11px;font-weight:650;color:var(--muted,#5a6678)}
.report-editor-toolbar .re-section-title span{white-space:nowrap}
.report-editor-toolbar .re-section-title select{width:auto;min-width:84px;margin-top:0;padding:5px 8px}
.report-editor-toolbar .re-note{margin:-5px 0 12px;color:var(--faint,#8a94a6);font-size:10px}
.report-editor-toolbar .re-actions{display:flex;gap:7px;flex-wrap:wrap;margin-top:16px;padding-top:14px;border-top:1px solid var(--line,#dfe4ec)}
.report-editor-toolbar button{border:1px solid var(--line,#dfe4ec);background:var(--surface2,#f7f9fc);color:var(--text,#0f172a);border-radius:7px;padding:7px 10px;cursor:pointer;font:inherit}
.report-editor-toolbar button.primary{background:var(--accent,#4f46e5);border-color:var(--accent,#4f46e5);color:#fff}
.report-editor-toolbar button:hover{filter:brightness(.97)}
.report-editor-toolbar .re-list{display:flex;flex-direction:column;gap:4px;max-height:230px;overflow:auto}
.report-editor-toolbar .re-row{display:flex;align-items:center;gap:6px;padding:5px 6px;border:1px solid var(--line,#dfe4ec);border-radius:7px;background:var(--surface2,#f7f9fc);cursor:grab}
.report-editor-toolbar .re-row.dragging{opacity:.45}
.report-editor-toolbar .re-row .re-grip{width:12px;flex:none;color:var(--faint,#8a94a6);text-align:center}
.report-editor-toolbar .re-row span:nth-child(2){flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.report-editor-toolbar .re-row input{accent-color:var(--accent,#4f46e5)}
.report-editor-toolbar .re-status{min-height:16px;margin-top:8px;color:var(--muted,#5a6678)}
.report-editor-toolbar .re-dialog{width:min(320px,calc(100vw - 40px));border:1px solid var(--line,#dfe4ec);border-radius:10px;padding:16px;color:var(--text,#0f172a);box-shadow:0 16px 48px rgba(16,24,40,.2)}
.report-editor-toolbar .re-dialog::backdrop{background:rgba(15,23,42,.36)}
.report-editor-toolbar .re-dialog h3{margin:0 0 12px;font-size:13px}
.report-editor-toolbar .re-dialog label{display:block;color:var(--muted,#5a6678);font-size:11px}
.report-editor-toolbar .re-dialog-actions{display:flex;justify-content:flex-end;gap:7px;margin-top:14px}
.report-editing .report-block{cursor:grab}
.report-editing .report-block:hover{box-shadow:inset 0 0 0 2px color-mix(in srgb,var(--accent,#4f46e5) 15%,transparent)}
@media(max-width:760px){.report-editor-toolbar{left:10px;right:10px;top:10px;width:auto;max-height:calc(100vh - 20px)}.report-editor-toolbar .re-list{max-height:180px}}
@media print{.report-editor-toolbar,.report-editor-open{display:none!important}.report-block[aria-hidden="true"]{display:none!important}}
</style>"""

EDITOR_SCRIPT = r"""<script>
(function(){
  var reportName="__REPORT_NAME__";
  var blocks=[].slice.call(document.querySelectorAll('.report-block[data-report-block]'));
  if(document.querySelector('.report-editor-toolbar')) return;
  if(!blocks.length){var root=document.querySelector('.page,.wrap')||document.body;[].slice.call(root.children).forEach(function(el,index){if(el.tagName==='SCRIPT')return;el.classList.add('report-block');el.dataset.reportBlock='section-'+(index+1)});blocks=[].slice.call(document.querySelectorAll('.report-block[data-report-block]'))}
  if(!document.querySelector('[data-report-conclusion]')){var parent=blocks[0]&&blocks[0].parentNode;if(parent){var note=document.createElement('div');note.className='report-block analyst-note-block';note.dataset.reportBlock='analyst-note';note.hidden=true;note.innerHTML='<section class="analyst-note"><b>工程师评审意见</b><p data-report-conclusion></p></section>';parent.insertBefore(note,blocks[1]||null);blocks=[].slice.call(document.querySelectorAll('.report-block[data-report-block]'))}}
  var labels={header:'页头与报告标题',verdict:'结论摘要',gate:'判定状态',evidence:'SPC 证据与改善建议',
    'main-chart':'主图证据',
    'analyst-note':'工程师评审意见',
    'appendix-title':'附录标题','appendix-minitab':'Minitab 完整输出','appendix-normality':'正态性检验',
    'appendix-metrics':'完整指标','appendix-interpret':'解读与风险','appendix-data':'原始数据与产出物'};
  var toolbar=document.createElement('aside'); toolbar.className='report-editor-toolbar'; toolbar.hidden=true;
  toolbar.innerHTML='<div class="re-head"><span>报告编辑</span><button class="re-close" data-act="close" aria-label="关闭">×</button></div>'+
    '<label class="re-field">标题<input data-field="title" maxlength="120"></label>'+
    '<label class="re-field">报告结论 / 工程师评审意见<textarea data-field="conclusion" maxlength="2000"></textarea></label>'+
    '<div class="re-note">系统判定、指标与分析证据保留为原始记录。</div>'+
    '<div class="re-section-title"><span>报告区块</span><select data-template title="应用布局"><option value="">布局</option></select></div>'+
    '<div class="re-list"></div>'+
    '<div class="re-actions"><button class="primary" data-act="edit">编辑内容与布局</button><button data-act="save" hidden>保存</button><button data-act="cancel" hidden>取消</button>'+
    '<button data-act="template" hidden>保存布局</button><button data-act="pdf">PDF</button><button data-act="docx">Word</button></div>'+
    '<div class="re-status"></div><dialog class="re-dialog"><form method="dialog"><h3>保存布局</h3>'+
    '<label>布局名称<input data-layout-name maxlength="80" required></label>'+
    '<div class="re-dialog-actions"><button value="cancel">取消</button><button class="primary" type="button" data-act="confirm-template">保存</button></div></form></dialog>';
  document.body.appendChild(toolbar);
  var list=toolbar.querySelector('.re-list'), status=toolbar.querySelector('.re-status');
  var editing=false;
  function apiToken(){try{return new URLSearchParams(location.search).get('token')||''}catch(e){return ''}}
  function apiPath(path){var t=apiToken(); return path+(path.indexOf('?')>=0?'&':'?')+'token='+encodeURIComponent(t)}
  function currentLayout(){return {order:[].slice.call(document.querySelectorAll('.report-block[data-report-block]')).map(function(b){return b.dataset.reportBlock}),
    hidden:[].slice.call(document.querySelectorAll('.report-block[data-report-block][aria-hidden="true"]')).map(function(b){return b.dataset.reportBlock})}}
  function applyLayout(layout){
    if(!layout||typeof layout!=='object')return;
    var parent=blocks[0]&&blocks[0].parentNode;
    var footer=parent&&parent.querySelector('.foot');
    (layout.order||[]).forEach(function(id){var block=document.querySelector('.report-block[data-report-block="'+id+'"]');if(block&&block.parentNode===parent)parent.insertBefore(block,footer||null)});
    var hidden=layout.hidden||[];
    blocks.forEach(function(block){var off=hidden.indexOf(block.dataset.reportBlock)>=0;block.setAttribute('aria-hidden',off?'true':'false');block.style.display=off?'none':''});
    var title=document.querySelector('.masthead h1,.top h1,h1'),note=document.querySelector('.analyst-note-block'),conclusion=document.querySelector('[data-report-conclusion]');
    if(layout.title&&title&&layout.useText===true){title.textContent=layout.title;document.title=layout.title}
    if(conclusion&&typeof layout.conclusion==='string'&&layout.useText===true){conclusion.textContent=layout.conclusion;if(note)note.hidden=!layout.conclusion.trim()}
    var ti=toolbar.querySelector('[data-field="title"]'),ci=toolbar.querySelector('[data-field="conclusion"]');
    if(ti&&title)ti.value=title.textContent.trim();if(ci&&conclusion)ci.value=conclusion.textContent.trim();
  }
  function loadTemplates(){fetch(apiPath('/api/report/templates')).then(function(r){return r.json()}).then(function(x){var select=toolbar.querySelector('[data-template]');(x.templates||[]).forEach(function(t){var opt=document.createElement('option');opt.value=t.name;opt.textContent=t.name;select.appendChild(opt)});select.addEventListener('change',function(){var item=(x.templates||[]).find(function(t){return t.name===select.value});if(item){applyLayout(item.layout);renderList()}})}).catch(function(){})}
  function renderList(){list.textContent='';[].slice.call(document.querySelectorAll('.report-block[data-report-block]')).forEach(function(block){
    if(block.dataset.reportBlock==='analyst-note'&&!editing&&!block.textContent.trim().replace('工程师评审意见',''))return;
    if(!block.textContent.trim()&&!block.querySelector('img,svg,table,canvas'))return;
    var row=document.createElement('div');row.className='re-row';row.draggable=editing;row.dataset.id=block.dataset.reportBlock;
    row.innerHTML='<span class="re-grip">≡</span><span></span><input type="checkbox" title="显示区块">';
    row.querySelector('span:nth-child(2)').textContent=labels[block.dataset.reportBlock]||block.dataset.reportBlock;
    var check=row.querySelector('input');check.checked=block.getAttribute('aria-hidden')!=='true';check.disabled=!editing;
    check.addEventListener('change',function(){block.setAttribute('aria-hidden',check.checked?'false':'true');block.style.display=check.checked?'':'none'});
    row.addEventListener('dragstart',function(e){if(!editing)return;row.classList.add('dragging');if(e.dataTransfer){e.dataTransfer.setData('text/plain',row.dataset.id);e.dataTransfer.effectAllowed='move'}});
    row.addEventListener('dragend',function(){row.classList.remove('dragging')});
    row.addEventListener('dragover',function(e){if(!editing)return;e.preventDefault();if(e.dataTransfer)e.dataTransfer.dropEffect='move';var drag=list.querySelector('.dragging');if(!drag||drag===row)return;var rect=row.getBoundingClientRect();list.insertBefore(drag,e.clientY<rect.top+rect.height/2?row:row.nextSibling);var db=document.querySelector('.report-block[data-report-block="'+drag.dataset.id+'"]');var rb=document.querySelector('.report-block[data-report-block="'+row.dataset.id+'"]');if(db&&rb)rb.parentNode.insertBefore(db,e.clientY<rect.top+rect.height/2?rb:rb.nextSibling)});
    list.appendChild(row);
  })}
  function setEditable(on){editing=on;document.body.classList.toggle('report-editing',on);
    var h=document.querySelector('.masthead h1,.top h1,h1'), note=document.querySelector('.analyst-note-block'), c=document.querySelector('[data-report-conclusion]');
    var titleInput=toolbar.querySelector('[data-field="title"]'), conclusionInput=toolbar.querySelector('[data-field="conclusion"]');
    if(titleInput&&h)titleInput.value=h.textContent.trim();if(conclusionInput&&c)conclusionInput.value=c.textContent.trim();
    if(note)note.hidden=!on&&!c.textContent.trim();
    [titleInput,conclusionInput].forEach(function(el){if(el)el.disabled=!on});
    toolbar.querySelector('[data-act="edit"]').hidden=on;toolbar.querySelector('[data-act="save"]').hidden=!on;toolbar.querySelector('[data-act="cancel"]').hidden=!on;
    toolbar.querySelector('[data-act="template"]').hidden=!on;toolbar.querySelector('[data-template]').hidden=!on;
    toolbar.querySelector('[data-act="pdf"]').hidden=on;toolbar.querySelector('[data-act="docx"]').hidden=on;
    toolbar.querySelector('.re-list').hidden=!on;toolbar.querySelector('.re-section-title').hidden=!on;toolbar.querySelector('.re-note').hidden=!on;
    toolbar.querySelector('[data-act="close"]').disabled=on;
    blocks=[].slice.call(document.querySelectorAll('.report-block[data-report-block]'));blocks.forEach(function(b){b.draggable=on});renderList();
    status.textContent='';
  }
  function cleanHTML(){var clone=document.documentElement.cloneNode(true);clone.querySelectorAll('.report-editor-toolbar,.report-editor-open,.aq-rail,.dscroll-hint').forEach(function(e){e.remove()});clone.querySelectorAll('[contenteditable]').forEach(function(e){e.removeAttribute('contenteditable')});clone.querySelectorAll('.report-block').forEach(function(e){e.removeAttribute('draggable')});var note=clone.querySelector('.analyst-note-block');if(note&&!note.textContent.trim().replace('工程师评审意见',''))note.remove();return '<!doctype html>\n'+clone.outerHTML}
  function commitFields(){var h=document.querySelector('.masthead h1,.top h1,h1'),note=document.querySelector('.analyst-note-block'),text=document.querySelector('[data-report-conclusion]');var ti=toolbar.querySelector('[data-field="title"]'),ci=toolbar.querySelector('[data-field="conclusion"]');if(h&&ti&&ti.value.trim()){h.textContent=ti.value.trim();document.title=ti.value.trim()}if(text&&ci)text.textContent=ci.value.trim();if(note&&text)note.hidden=!text.textContent.trim()}
  toolbar.addEventListener('click',function(e){var b=e.target.closest('button');if(!b)return;var act=b.dataset.act;
    if(act==='close'){toolbar.hidden=true;if(openButton)openButton.hidden=false;return}
    if(act==='edit'){setEditable(true);return}
    if(act==='cancel'){location.reload();return}
    if(act==='save'){commitFields();var payload={name:reportName,html:cleanHTML(),layout:currentLayout()};fetch(apiPath('/api/report/layout'),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}).then(function(r){return r.json()}).then(function(x){if(!x.ok)throw new Error(x.error||'保存失败');setEditable(false);status.textContent='已保存';}).catch(function(err){status.textContent=err.message})}
    else if(act==='template'){toolbar.querySelector('.re-dialog').showModal();toolbar.querySelector('[data-layout-name]').focus()}
    else if(act==='confirm-template'){var name=toolbar.querySelector('[data-layout-name]').value.trim();if(!name){toolbar.querySelector('[data-layout-name]').focus();return}var layout=currentLayout();fetch(apiPath('/api/report/templates'),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:name,layout:layout})}).then(function(r){return r.json()}).then(function(x){if(!x.ok)throw new Error(x.error||'保存失败');var select=toolbar.querySelector('[data-template]'),opt=document.createElement('option');opt.value=x.name;opt.textContent=x.name;select.appendChild(opt);select.value=x.name;toolbar.querySelector('.re-dialog').close();toolbar.querySelector('[data-layout-name]').value='';status.textContent='布局已保存'}).catch(function(err){status.textContent=err.message})}
    else if(act==='pdf'||act==='docx'){var f=act==='pdf'?'pdf':'docx';fetch(apiPath('/api/report/export'),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:reportName,format:f})}).then(function(r){return r.json()}).then(function(x){if(!x.ok)throw new Error(x.error||'导出失败');if(x.download_url)window.location.href=x.download_url;status.textContent='已生成 '+f.toUpperCase()}).catch(function(err){if(f==='pdf'){window.print();status.textContent='已打开打印窗口，请选择“存储为 PDF”'}else status.textContent=err.message})}
  });
  loadTemplates();
  fetch(apiPath('/api/report/layout?name='+encodeURIComponent(reportName))).then(function(r){return r.json()}).then(function(x){if(x.layout&&x.layout.order&&x.layout.order.length){applyLayout(x.layout);renderList()}}).catch(function(){});
  renderList();
  var openButton=document.createElement('button');openButton.className='report-editor-open';openButton.textContent='编辑报告';openButton.title='打开报告编辑';openButton.style.cssText='position:fixed;right:18px;bottom:18px;z-index:119;border:1px solid var(--line,#dfe4ec);background:var(--surface,#fff);color:var(--text,#0f172a);border-radius:8px;padding:8px 11px;box-shadow:0 8px 24px rgba(16,24,40,.12);cursor:pointer;font:12px inherit';
  openButton.addEventListener('click',function(){openButton.hidden=true;toolbar.hidden=false;setEditable(false)});document.body.appendChild(openButton);setEditable(false);
})();
</script>"""


def attach_report_editor(doc: str, report_name: str) -> str:
    """注入报告工作台；文件仍可离线查看，保存/导出时连接本地设置服务。"""
    safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", report_name)
    payload = EDITOR_STYLE + EDITOR_SCRIPT.replace("__REPORT_NAME__", safe_name)
    marker = "</body>"
    if marker in doc:
        index = doc.rfind(marker)
        return doc[:index] + payload + doc[index:]
    return doc + payload


def _capability_kv(metrics: dict[str, Any]) -> str:
    """能力速览：八个格子，只放判读需要的数，完整指标进附录。"""
    n_value = _num(metrics.get("n"))
    n_text = _fmt(n_value, 0) if n_value is not None else str(len(metrics.get("points") or []) or "—")
    pairs = [
        ("Cp", _fmt(metrics.get("cp"), 3)),
        ("Cpk", _fmt(metrics.get("cpk"), 3)),
        ("Pp", _fmt(metrics.get("pp"), 3)),
        ("Ppk", _fmt(metrics.get("ppk"), 3)),
        ("均值", _fmt(metrics.get("mean"), 3)),
        ("组内 σ", _fmt(metrics.get("wstd"), 3)),
        ("整体 σ", _fmt(metrics.get("ostd"), 3)),
        ("样本量", n_text),
    ]
    cells = "".join(
        f'<div class="kvi"><span>{html.escape(k)}</span><b>{html.escape(v)}</b></div>'
        for k, v in pairs
    )
    return f'<div class="kv">{cells}</div>'


def build_context(
    endpoint: str,
    payload: dict[str, Any],
    result: dict[str, Any],
    channel: str,
    source: str,
    broadcast: dict[str, Any],
    embed_image: bool = False,
    extra: Optional[dict[str, Any]] = None,
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
    sheet_url = mt.get("worksheetcsv")

    full = _minitab_full_section(result, doc_url or "")
    report_title = full["title"] or "过程能力分析报告"

    # lspec=0 是 BQL 约定的“未设下限”哨兵值，单侧规格时明确标注，避免误读
    lsl_missing = metrics["lsl"] is None or metrics["lsl"] == 0
    spec_parts = [
        f"LSL {_fmt(metrics['lsl'], 2)}" if not lsl_missing else "",
        f"USL {_fmt(metrics['usl'], 2)}" if metrics["usl"] is not None else "",
        f"目标 {_fmt(metrics['target'], 2)}" if metrics["target"] is not None else "",
    ]
    spec_text = " · ".join(filter(None, spec_parts)) or "未设置规格限"
    if spec_parts and any(spec_parts) and lsl_missing and metrics["usl"] is not None:
        spec_text += " · 单侧规格"

    n_value = _num(metrics["n"]) or len(metrics["points"])
    if isinstance(n_value, float) and n_value.is_integer():
        n_value = int(n_value)
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
        ("预期超上限", _ppm_text(metrics["out_usl"]), "PPM"),
        ("预期超下限", _ppm_text(metrics["out_lsl"]), "PPM"),
    ])

    raw_table = _raw_table(metrics, (extra or {}).get("control_limits"),
                           (extra or {}).get("control_check"))
    chart_html = build_chart(metrics)
    interpretation_html = _interpret_block(result)
    capability_kv = _capability_kv(metrics)

    # 证据卡按实际存在的内容连续编号：缺控制图时从 01 开始，不留空号
    _seq = 0
    _control_body_html = extra.get("control_html") or ""
    if _control_body_html:
        _seq += 1
        evidence_control = _evidence_card(
            f"{_seq:02d}", "过程稳定性", "控制图 · 判异规则",
            extra.get("concl_control"), _control_body_html, anchor="ev-control",
        )
    else:
        evidence_control = ""
    _seq += 1
    evidence_capability = _evidence_card(
        f"{_seq:02d}", "过程能力", "Cp / Cpk / Pp / Ppk",
        extra.get("concl_capability"), capability_kv, anchor="ev-cap",
    )
    _seq += 1
    action_plan_html = _evidence_card(
        f"{_seq:02d}", "改善建议", "按成本排序", None,
        extra.get("action_plan") or "", anchor="ev-action",
    )

    # 自适应详略：过程受控、数据正态、Cpk 达标时，正文只留判决与数字，
    # 三张证据卡收进折叠区——没问题就不该让读者滚动 3 屏去确认"没问题"。
    _l2 = evidence_control + evidence_capability + action_plan_html
    if extra.get("all_green") and _l2:
        evidence_l2 = _fold(
            "过程证据与明细",
            f"受控 · 正态 · Cpk {_fmt(metrics.get('cpk'), 3)}",
            _l2,
        )
    else:
        evidence_l2 = _l2

    links: list[str] = []
    if graph_url:
        links.append(f'<a href="{html.escape(graph_url, quote=True)}" target="_blank" rel="noopener">能力分析图（OTGraph）↗</a>')
    if doc_url:
        links.append(f'<a href="{html.escape(doc_url, quote=True)}" target="_blank" rel="noopener">Minitab 报告 ↗</a>')
    if csv_url:
        links.append(f'<a href="{html.escape(csv_url, quote=True)}" target="_blank" rel="noopener">原始数据 CSV ↗</a>')
    if sheet_url:
        links.append(f'<a href="{html.escape(sheet_url, quote=True)}" target="_blank" rel="noopener">工作表 CSV ↗</a>')
    links_html = "".join(links) or '<span class="mnote">本次结果未附带产出物链接</span>'

    broadcast_text = f"{broadcast.get('host') or '—'}:{broadcast.get('port') or '—'}"
    footer = (
        f'结果来源 <code>{html.escape(source)}</code> · 频道 <code>{html.escape(channel)}</code><br>'
        f'广播地址 <code>{html.escape(str(broadcast_text))}</code>'
    )

    extra = extra or {}
    report_no = f"BQL-{now_dt.strftime('%Y%m%d')}-{re.sub(r'[^A-Za-z0-9]', '', channel)[-6:].upper()}"
    meta_pairs = extra.get("meta") or [
        ("报告编号", report_no),
        ("分析接口", endpoint),
        ("数据来源", channel),
        ("生成时间", now_dt.strftime("%Y-%m-%d %H:%M:%S")),
        ("引擎", "Minitab / BQL"),
    ]
    meta_grid = "".join(
        f"<div><span>{html.escape(str(k))}</span><b>{html.escape(str(v))}</b></div>"
        for k, v in meta_pairs
    )

    return {
        "title": report_title,
        "report_title": report_title,
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
        "spec_legend": "规格上限 USL" if (lsl_missing and metrics["usl"] is not None) else "规格限 LSL / USL",
        "minitab_chart": _minitab_block(graph_url, doc_url, embed=embed_image),
        "primary_minitab_chart": _minitab_block(
            extra.get("primary_graph_url") or graph_url,
            extra.get("primary_doc_url") or doc_url,
            embed=embed_image,
            title=extra.get("primary_graph_title") or "Minitab 能力分析图",
            caption=extra.get("primary_graph_caption") or "由 Minitab 引擎生成的原始分析图。",
        ),
        "minitab_full": full["html"],
        "minitab_title": full["title"],
        "minitab_meta": full["meta"],
        "minitab_image_count": full["image_count"],
        "minitab_table_count": full["table_count"],
        "kpis": f'<div class="kpis">{_kpi_cards(metrics)}</div>',
        "interpretation": interpretation_html,
        "distribution": build_chart(metrics),
        "index_rows": index_rows,
        "stat_rows": stat_rows,
        "raw_data": raw_table,
        "report_no": report_no,
        # ---- L1 判决层 ----
        "verdict_hero": extra.get("verdict_hero") or "",
        "gate_strip": extra.get("gate_strip") or "",
        "disposition": str((extra.get("disposition") or {}).get("label") or ""),
        "disposition_tone": str((extra.get("disposition") or {}).get("tone") or "neutral"),
        "disposition_reason": str((extra.get("disposition") or {}).get("reason") or ""),
        # ---- L2 证据层 ----
        "evidence_l2": evidence_l2,
        "evidence_control": evidence_control,
        "evidence_capability": evidence_capability,
        "action_plan": action_plan_html,
        # ---- L3 附录层 ----
        "appendix_minitab": _fold(
            "Minitab 完整输出",
            f"Cpk {_fmt(metrics.get('cpk'), 3)} · Ppk {_fmt(metrics.get('ppk'), 3)}",
            full["html"],
        ),
        "appendix_normality": _fold(
            "正态性检验详情",
            extra.get("normality_meta") or "",
            extra.get("normality_body") or "",
        ),
        "appendix_interpret": _fold(
            "完整解读与风险清单", "规则判定 · 可审计", interpretation_html
        ),
        "appendix_metrics": _fold(
            "完整指标与过程特性", "CPU / CPL / PPU / PPL",
            '<div class="two">'
            f'<div><div class="mtb-h2">能力指数</div>{index_rows}</div>'
            f'<div><div class="mtb-h2">过程特性</div>{stat_rows}</div>'
            "</div>",
        ),
        "appendix_data": _fold(
            "原始数据与报告信息",
            f"{str(n_value)} 点 · 报告编号 {report_no}",
            raw_table + f'<div style="margin-top:16px">{meta_grid}</div>'
            f'<div style="margin-top:14px">{links_html}</div>',
        ),
        "gate_section": extra.get("gate_section") or "",
        "control_section": extra.get("control_section") or "",
        "normality_section": extra.get("normality_section") or "",
        "capability_extra": extra.get("capability_extra") or "",
        "meta_grid": f'<div class="meta-grid">{meta_grid}</div>',
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
    embed_image: bool = False,
    extra: Optional[dict[str, Any]] = None,
) -> Path:
    context = build_context(
        endpoint, payload, result, channel, source, broadcast,
        embed_image=embed_image, extra=extra,
    )
    doc = report_templates.render_template(template, context)
    doc = attach_navigation(doc)

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    safe_channel = re.sub(r"[^A-Za-z0-9_-]", "_", channel)
    report_name = f"{endpoint}-{safe_channel}.html"
    doc = attach_report_editor(doc, report_name)
    path = REPORTS_DIR / report_name
    path.write_text(doc, encoding="utf-8")
    return path


def list_templates() -> list[dict[str, Any]]:
    return report_templates.list_templates()
