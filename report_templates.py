"""报告模板注册表与占位符渲染器。

模板是普通 HTML 文件，放在 templates/builtin（内置）或 templates/user（用户自定义）。
占位符语法：
  {{key}}    转义后的文本（安全，用于变量值）
  {{{key}}}  原始 HTML（用于报告区块，如 Minitab 图表、指标卡）
可用占位符见 templates/user/README.md。
"""
from __future__ import annotations

import html
import re
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
BUILTIN_DIR = TEMPLATES_DIR / "builtin"
USER_DIR = TEMPLATES_DIR / "user"

for directory in (BUILTIN_DIR, USER_DIR):
    directory.mkdir(parents=True, exist_ok=True)


def _label_from(path: Path) -> str:
    try:
        first = path.read_text(encoding="utf-8").splitlines()[:6]
        for line in first:
            match = re.match(r"\s*<!--\s*label:\s*(.+?)\s*-->\s*$", line)
            if match:
                return match.group(1)
    except OSError:
        pass
    return path.stem


def list_templates() -> list[dict[str, Any]]:
    """列出全部可用模板（内置 + 用户自定义）。"""
    items: list[dict[str, Any]] = []
    for directory, origin in ((BUILTIN_DIR, "builtin"), (USER_DIR, "user")):
        if not directory.exists():
            continue
        for path in sorted(directory.glob("*.html")):
            items.append({
                "name": path.stem,
                "label": _label_from(path),
                "origin": origin,
                "path": str(path),
            })
    return items


def resolve_template(name: str) -> Path:
    """用户模板优先于同名内置模板，便于覆盖。"""
    safe = re.sub(r"[^A-Za-z0-9_-]", "", str(name or ""))
    if not safe:
        safe = "aurora"
    user_file = USER_DIR / f"{safe}.html"
    if user_file.exists():
        return user_file
    builtin_file = BUILTIN_DIR / f"{safe}.html"
    if builtin_file.exists():
        return builtin_file
    available = ", ".join(item["name"] for item in list_templates())
    raise ValueError(f"未知报告模板：{safe}；可用模板：{available}")


def render_template(name: str, context: dict[str, Any]) -> str:
    source = resolve_template(name).read_text(encoding="utf-8")

    def raw_replace(match: re.Match) -> str:
        key = match.group(1)
        value = context.get(key, "")
        return "" if value is None else str(value)

    def escaped_replace(match: re.Match) -> str:
        key = match.group(1)
        value = context.get(key, "")
        return html.escape("" if value is None else str(value), quote=True)

    # 先替换原始块，再替换转义标量，避免块内占位符被二次处理。
    source = re.sub(r"\{\{\{([\w.]+)\}\}\}", raw_replace, source)
    source = re.sub(r"\{\{([\w.]+)\}\}", escaped_replace, source)
    return source


def available_names() -> list[str]:
    return [item["name"] for item in list_templates()]
