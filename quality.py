"""纯 Python 质量统计计算：不依赖 numpy / scipy。

提供报告需要的四类能力：
1. 正态性检验（Anderson-Darling，含 p 值、偏度、峰度）
2. 控制图判异（超控制限、连续同侧、连续趋势）
3. Cpk 置信下限（Bissell 近似，Minitab 同源）
4. 能力归因与改善潜力（偏移损失 / 组间波动 / 目标化后的 Cpk / 需要的 σ）
"""
from __future__ import annotations

import math
from typing import Any, Optional

Z_95 = 1.645          # 单侧 95%
GRADE_CPK = 1.33      # 常规改善目标


def _norm_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _mean(values: list[float]) -> float:
    return sum(values) / len(values)


def _stdev(values: list[float]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    mu = _mean(values)
    return math.sqrt(sum((v - mu) ** 2 for v in values) / (n - 1))


def _moments(values: list[float]) -> tuple[float, float]:
    """返回（偏度，超额峰度）。"""
    n = len(values)
    if n < 4:
        return 0.0, 0.0
    mu = _mean(values)
    m2 = sum((v - mu) ** 2 for v in values) / n
    if m2 <= 0:
        return 0.0, 0.0
    m3 = sum((v - mu) ** 3 for v in values) / n
    m4 = sum((v - mu) ** 4 for v in values) / n
    return m3 / (m2 ** 1.5), m4 / (m2 ** 2) - 3.0


def anderson_darling(values: list[float]) -> Optional[dict[str, Any]]:
    """Anderson-Darling 正态性检验（均值与标准差由样本估计）。

    p 值为 D'Agostino & Stephens (1986) 近似，与 Minitab 默认检验一致。
    样本量 < 8 时不做检验。
    """
    n = len(values)
    if n < 8:
        return None
    mu = _mean(values)
    sd = _stdev(values)
    if sd <= 0:
        return None

    ordered = sorted(values)
    total = 0.0
    for index in range(1, n + 1):
        low = _norm_cdf((ordered[index - 1] - mu) / sd)        # F(x_i)
        high = _norm_cdf((ordered[n - index] - mu) / sd)       # F(x_{n+1-i})
        low = min(max(low, 1e-12), 1.0 - 1e-12)
        high = min(max(high, 1e-12), 1.0 - 1e-12)
        total += (2 * index - 1) * (math.log(low) + math.log(1.0 - high))
    a2 = -n - total / n

    adjusted = a2 * (1.0 + 0.75 / n + 2.25 / (n * n))
    if adjusted < 0.2:
        p = 1.0 - math.exp(-13.436 + 101.14 * adjusted - 223.73 * adjusted ** 2)
    elif adjusted < 0.34:
        p = 1.0 - math.exp(-8.318 + 42.796 * adjusted - 59.938 * adjusted ** 2)
    elif adjusted < 0.6:
        p = math.exp(0.9177 - 4.279 * adjusted - 1.38 * adjusted ** 2)
    else:
        p = math.exp(1.2937 - 5.709 * adjusted + 0.0186 * adjusted ** 2)
    p = min(max(p, 0.0), 1.0)

    skew, kurt = _moments(values)
    return {
        "ad": a2,
        "adjusted": adjusted,
        "p_value": p,
        "is_normal": p >= 0.05,
        "skewness": skew,
        "kurtosis": kurt,
        "n": n,
    }


def _same_side_segments(values: list[float], center: float) -> list[tuple[int, int, int]]:
    """返回连续位于中心线同侧的段：(起点索引, 长度, 方向)。"""
    segments: list[tuple[int, int, int]] = []
    start: Optional[int] = None
    side = 0
    for index, value in enumerate(values):
        current = 0 if abs(value - center) < 1e-12 else (1 if value > center else -1)
        if current == 0:
            if start is not None and side:
                segments.append((start, index - start, side))
            start, side = None, 0
        elif side == 0:
            start, side = index, current
        elif current != side:
            segments.append((start, index - start, side))
            start, side = index, current
    if start is not None and side:
        segments.append((start, len(values) - start, side))
    return segments


def _monotonic_segments(values: list[float]) -> list[tuple[int, int, int]]:
    """返回连续单调段：(起点索引, 长度, 方向)。"""
    segments: list[tuple[int, int, int]] = []
    start: Optional[int] = None
    direction = 0
    for index in range(1, len(values)):
        delta = values[index] - values[index - 1]
        current = 0 if delta == 0 else (1 if delta > 0 else -1)
        if current == 0:
            if start is not None and direction:
                segments.append((start, index - start, direction))
            start, direction = None, 0
        elif direction == 0:
            start, direction = index - 1, current
        elif current != direction:
            segments.append((start, index - start, direction))
            start, direction = index - 1, current
    if start is not None and direction:
        segments.append((start, len(values) - start, direction))
    return segments


def control_check(means: list[float], center: Optional[float],
                  ucl: Optional[float], lcl: Optional[float]) -> dict[str, Any]:
    """按 Western Electric / Nelson 常用三条规则检查控制图。

    除了汇总结果，还返回命中规则的具体点号（1-based）。报告里的原始数据表要用它把
    「连续 8 点同侧」这类判异标到具体子组上——否则控制图说不受控、数据表全标正常，
    两边自相矛盾。
    """
    if not means or center is None or ucl is None or lcl is None:
        return {"available": False}

    beyond = [i + 1 for i, m in enumerate(means) if m > ucl or m < lcl]

    side_segments = _same_side_segments(means, center)
    longest_run = max((length for _, length, _ in side_segments), default=0)
    rule2_points: list[int] = []
    for start, length, _ in side_segments:
        if length >= 8:
            rule2_points.extend(range(start + 1, start + length + 1))

    mono_segments = _monotonic_segments(means)
    longest_trend = max((length for _, length, _ in mono_segments), default=0)
    rule3_points: list[int] = []
    for start, length, _ in mono_segments:
        if length >= 6:
            rule3_points.extend(range(start + 1, start + length + 1))

    return {
        "available": True,
        "beyond": len(beyond),
        "beyond_points": beyond,
        "longest_run": longest_run,
        "longest_trend": longest_trend,
        "rule2": longest_run >= 8,
        "rule3": longest_trend >= 6,
        "rule2_points": sorted(set(rule2_points)),
        "rule3_points": sorted(set(rule3_points)),
        "controlled": not beyond and longest_run < 8 and longest_trend < 6,
        "points": len(means),
    }


def cpk_lower_bound(cpk: Optional[float], n: float, z: float = Z_95) -> Optional[float]:
    """Cpk 的单侧置信下限（Bissell 近似，与 Minitab 的 95% 下限一致）。"""
    if cpk is None or not n or n < 2 or cpk <= 0:
        return None
    try:
        spread = math.sqrt(1.0 / (9.0 * n * cpk * cpk) + 1.0 / (2.0 * (n - 1.0)))
    except (ValueError, ZeroDivisionError):
        return None
    return cpk * (1.0 - z * spread)


def cpk_bounds(cpk: Optional[float], n: float, z: float = Z_95) -> tuple:
    """Cpk 的双侧区间（同一 Bissell 近似），用于说明点估计的抽样不确定性。

    n=100、Cpk=1.18 时区间约 1.03–1.33，宽 0.3 —— 下一批可能差这么多，
    这是「单点 Cpk 不可辩护」的量化依据。
    """
    if cpk is None or not n or n < 2 or cpk <= 0:
        return None, None
    try:
        spread = math.sqrt(1.0 / (9.0 * n * cpk * cpk) + 1.0 / (2.0 * (n - 1.0)))
    except (ValueError, ZeroDivisionError):
        return None, None
    return cpk * (1.0 - z * spread), cpk * (1.0 + z * spread)


def capability_breakdown(metrics: dict[str, Any]) -> dict[str, Any]:
    """把 Cp→Cpk、Cp→Pp 的损失拆成可解释的数字。"""
    cp = metrics.get("cp")
    cpk = metrics.get("cpk")
    pp = metrics.get("pp")
    ppk = metrics.get("ppk")
    mean = metrics.get("mean")
    lsl = metrics.get("lsl")
    usl = metrics.get("usl")
    target = metrics.get("target")
    wstd = metrics.get("wstd")
    ostd = metrics.get("ostd")

    offset = None
    offset_ratio = None
    if mean is not None and lsl is not None and usl is not None:
        mid = (lsl + usl) / 2.0
        reference = target if target is not None else mid
        offset = mean - reference
        if usl > lsl:
            offset_ratio = abs(offset) / (usl - lsl)

    centering_loss = (cp - cpk) if (cp is not None and cpk is not None) else None
    between_loss = (cp - pp) if (cp is not None and pp is not None) else None

    between_share = None
    if wstd and ostd and ostd > 0:
        between_share = max(0.0, 1.0 - (wstd * wstd) / (ostd * ostd))

    return {
        "offset": offset,
        "offset_ratio": offset_ratio,
        "centering_loss": centering_loss,
        "between_loss": between_loss,
        "between_share": between_share,
        "cp": cp,
        "cpk": cpk,
        "pp": pp,
        "ppk": ppk,
    }


def improvement_potential(metrics: dict[str, Any], goal: float = GRADE_CPK) -> dict[str, Any]:
    """给出两个可执行数字：中心化后的 Cpk、达到目标所需的标准差。"""
    mean = metrics.get("mean")
    lsl = metrics.get("lsl")
    usl = metrics.get("usl")
    wstd = metrics.get("wstd")
    cp = metrics.get("cp")

    out: dict[str, Any] = {"goal": goal, "cpk_if_centered": cp}

    # 单侧规格（仅 USL 或仅 LSL）同样要给出“需要多少 σ”——否则一条边都没有时
    # 报告会退化成“维持现状”，与 D 级能力自相矛盾。
    sides: list[float] = []
    if mean is not None:
        if usl is not None:
            sides.append(usl - mean)
        if lsl is not None and lsl != 0:
            sides.append(mean - lsl)
    if sides and wstd and wstd > 0:
        nearest = min(sides)
        out["sigma_needed"] = nearest / (3.0 * goal) if nearest > 0 else None
        out["sigma_now"] = wstd
        if out["sigma_needed"]:
            out["sigma_cut_pct"] = max(0.0, (1.0 - out["sigma_needed"] / wstd) * 100.0)
    return out


MSA_NOTE = (
    "过程能力指数建立在“测量数据可信”的前提上。若未做测量系统分析（MSA / Gage R&R），"
    "本报告的 Cpk 为名义值；GRR 超过 10% 时，真实能力会低于报告值，建议先补 MSA 再对外交付。"
)
