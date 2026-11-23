"""确定性质量解读：基于分析指标生成可审计的结论、诊断、风险与行动建议。

规则层只做确定性推理，不调用 LLM。LLM 的表达层通过 MCP Prompt 交给 client 完成。
"""
from __future__ import annotations

from typing import Any

import report as report_module


def interpret(metrics: dict[str, Any]) -> dict[str, Any]:
    cpk = _f(metrics.get("cpk"))
    ppk = _f(metrics.get("ppk"))
    mean = _f(metrics.get("mean"))
    wstd = _f(metrics.get("wstd"))
    ostd = _f(metrics.get("ostd"))
    n = _f(metrics.get("n"))
    lsl = _f(metrics.get("lsl"))
    usl = _f(metrics.get("usl"))
    out_usl = _f(metrics.get("out_usl"))
    out_lsl = _f(metrics.get("out_lsl"))

    verdict = _verdict(cpk)
    diagnosis: list[str] = []
    risks: list[str] = []
    actions: list[str] = []

    if cpk is not None and ppk is not None:
        gap = ppk - cpk
        if gap > 0.15:
            diagnosis.append(
                f"Ppk({ppk:.3f}) 高于 Cpk({cpk:.3f})，差值 {gap:.3f}，提示过程存在特殊原因波动或子组划分不合理，"
                "整体表现好于组内预期。"
            )
        elif gap < -0.15:
            diagnosis.append(
                f"Ppk({ppk:.3f}) 低于 Cpk({cpk:.3f})，差值 {gap:.3f}，提示过程均值漂移或存在趋势性偏移，"
                "短期能力无法代表长期表现。"
            )
        else:
            diagnosis.append(f"Cpk({cpk:.3f}) 与 Ppk({ppk:.3f}) 接近，过程波动以普通原因为主，表现稳定。")

    if wstd is not None and ostd is not None and wstd > 0:
        ratio = ostd / wstd
        if ratio > 1.3:
            diagnosis.append(f"整体标准差约为组内的 {ratio:.2f} 倍，组间差异明显，建议排查批次/设备/班次差异。")

    if n is not None and n < 30:
        risks.append(f"样本量仅 {int(n)} 个，能力指数置信区间较宽，判定结论仅供参考，建议补充至 30 个以上。")

    if lsl is not None and lsl == 0:
        risks.append("规格下限 lspec=0 疑似被服务端按“未设置下限”处理，实际可能为单侧能力分析，双侧 Cpk 可能被高估。")

    single_sided = lsl is None or lsl == 0  # lspec=0 是 BQL “未设下限”哨兵值

    if single_sided and mean is not None and usl is not None:
        margin = usl - mean
        if margin > 0:
            span_pct = margin / usl * 100 if usl else 0
            diagnosis.append(
                f"单侧规格（仅 USL={usl:g}）：均值 {mean:.2f} 距上限仅 {margin:.2f}，"
                f"约占上限的 {span_pct:.1f}%，规格余量偏小，是能力损失的主要来源。"
            )
            actions.append("优先降低过程波动，并适度下移均值以扩大上限侧余量，可最直接提升 Cpk。")
    elif mean is not None and lsl is not None and usl is not None and usl > lsl:
        center = (lsl + usl) / 2
        offset = (mean - center) / (usl - lsl) * 100
        if abs(offset) > 10:
            diagnosis.append(f"均值 {mean:.2f} 偏离规格中心 {offset:+.1f}% ，过程未对中，是能力损失的主要来源之一。")
            actions.append("优先将过程均值调整至规格中心，可在不降低波动的前提下直接提升 Cpk。")

    # BQL 返回的 >USL / <LSL 是 PPM（百万分之），不是观测点数
    if out_usl is not None and out_usl > 0:
        risks.append(f"预期超出上限约 {out_usl / 10000:.2f}%（{_n(out_usl)} PPM），存在实际不良风险。")
    if out_lsl is not None and out_lsl > 0:
        risks.append(f"预期超出下限约 {out_lsl / 10000:.2f}%（{_n(out_lsl)} PPM），存在实际不良风险。")

    if cpk is not None:
        if cpk >= 1.67:
            actions.append("过程能力充分，可维持现有控制方案，建议纳入常规 SPC 监控以防退化。")
        elif cpk >= 1.33:
            actions.append("过程能力满足常规要求，建议持续监控关键因子，避免均值漂移。")
        elif cpk >= 1.0:
            actions.append("过程能力勉强可接受，建议识别主要波动源并制定改善计划，特别是接近规格侧的能力提升。")
            if diagnosis:
                actions.append("结合上述诊断结果，优先验证子组划分与测量系统（MSA），再针对性降低波动。")
        else:
            actions.append("过程能力不足，建议暂停放量并启动专项改善（DMAIC），优先做测量系统分析与过程稳定性确认。")

    if not risks:
        risks.append("未发现明显数据风险；结论仍受样本量与规格设定影响，需结合业务场景判断。")

    return {
        "headline": verdict["headline"],
        "grade": verdict["grade"],
        "verdict": verdict["label"],
        "tone": verdict["tone"],
        "diagnosis": diagnosis or ["指标未发现明显异常模式。"],
        "risks": risks,
        "actions": actions,
        "basis": {
            "cpk": cpk,
            "ppk": ppk,
            "sample_size": n,
            "wstd": wstd,
            "ostd": ostd,
            "lsl": lsl,
            "usl": usl,
        },
    }


def _verdict(cpk: float | None) -> dict[str, str]:
    if cpk is None:
        return {"label": "无法判定", "grade": "—", "tone": "neutral", "headline": "缺少 Cpk 指标，无法判定过程能力。"}
    if cpk >= 1.67:
        return {"label": "能力充分", "grade": "A", "tone": "good", "headline": f"Cpk = {cpk:.3f}，过程能力充分，满足严格行业要求。"}
    if cpk >= 1.33:
        return {"label": "能力充足", "grade": "B", "tone": "good", "headline": f"Cpk = {cpk:.3f}，过程能力充足，满足常规质量要求。"}
    if cpk >= 1.0:
        return {"label": "能力可接受", "grade": "C", "tone": "warn", "headline": f"Cpk = {cpk:.3f}，过程能力勉强可接受，但存在改进空间。"}
    return {"label": "能力不足", "grade": "D", "tone": "bad", "headline": f"Cpk = {cpk:.3f}，过程能力不足，需立即改善。"}


def _f(value: Any) -> float | None:
    try:
        if value is None or isinstance(value, bool):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _n(value: float) -> str:
    return str(int(value)) if value.is_integer() else f"{value:g}"


def from_result(result: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    metrics = report_module.extract_metrics(result, payload)
    return interpret(metrics)


def to_markdown(data: dict[str, Any]) -> str:
    lines = [f"## {data['verdict']}（{data['grade']} 级）", "", data["headline"], ""]
    for title, key in (("诊断", "diagnosis"), ("风险提示", "risks"), ("行动建议", "actions")):
        lines.append(f"### {title}")
        lines.extend(f"- {item}" for item in data.get(key, []))
        lines.append("")
    return "\n".join(lines)
