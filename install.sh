#!/usr/bin/env bash
# BQL MCP Connector 一键安装脚本
# 用法: curl -fsSL https://raw.githubusercontent.com/adymilk/bql-mcp/main/install.sh | bash
set -euo pipefail

REPO="https://github.com/adymilk/bql-mcp"
RAW="https://raw.githubusercontent.com/adymilk/bql-mcp/main"
DEST="${BQL_MCP_DIR:-$HOME/.workbuddy/connectors/bql}"
PY="${PYTHON:-python3}"
BASE_URL="${1:-${BQL_BASE_URL:-}}"
TOKEN="${2:-${BQL_TOKEN:-}}"

say() { printf '\033[1;34m[bql-mcp]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[bql-mcp]\033[0m %s\n' "$*"; }
err() { printf '\033[1;31m[bql-mcp]\033[0m %s\n' "$*" >&2; }

command -v "$PY" >/dev/null 2>&1 || { err "需要 Python 3，$PY 未找到"; exit 1; }
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' || { err "需要 Python 3.10+"; exit 1; }

say "安装到 $DEST"
mkdir -p "$DEST"

FILES="server.py report.py report_templates.py interpret.py exporter.py settings.html endpoints.json config.example.json requirements.txt"
for f in $FILES; do
  say "下载 $f"
  curl -fsSL "$RAW/$f" -o "$DEST/$f"
done

# 报告模板：内置模板每次升级覆盖；用户目录只补齐示例，不覆盖已有文件
TEMPLATES="builtin/aurora.html builtin/midnight.html builtin/audit.html builtin/executive.html"
for f in $TEMPLATES; do
  mkdir -p "$DEST/templates/$(dirname "$f")"
  say "下载模板 $f"
  curl -fsSL "$RAW/templates/$f" -o "$DEST/templates/$f"
done
mkdir -p "$DEST/templates/user"
for f in README.md mybrand.html.example; do
  [ -f "$DEST/templates/user/$f" ] || curl -fsSL "$RAW/templates/user/$f" -o "$DEST/templates/user/$f"
done

if [ ! -f "$DEST/config.json" ]; then
  cp "$DEST/config.example.json" "$DEST/config.json"
  say "已创建 config.json"
else
  warn "保留已有 config.json，未覆盖"
fi
chmod 600 "$DEST/config.json"

# 零配置安装：命令行参数或环境变量直接写入配置
if [ -n "$BASE_URL" ] || [ -n "$TOKEN" ]; then
  "$PY" - "$DEST/config.json" "$BASE_URL" "$TOKEN" <<'PY'
import json, sys
path, base, token = sys.argv[1], sys.argv[2].strip(), sys.argv[3].strip()
try:
    with open(path, encoding='utf-8') as f:
        cfg = json.load(f)
except Exception:
    cfg = {}
if base:
    cfg['base_url'] = base.rstrip('/')
if token:
    cfg['token'] = token
with open(path, 'w', encoding='utf-8') as f:
    json.dump(cfg, f, ensure_ascii=False, indent=2)
    f.write('\n')
PY
  chmod 600 "$DEST/config.json"
  say "已写入 BASE_URL 与 TOKEN，安装后即可直接调用"
fi

say "创建 Python 虚拟环境"
if command -v uv >/dev/null 2>&1; then
  say "检测到 uv，使用极速安装"
  uv venv --python "$PY" "$DEST/venv" >/dev/null 2>&1 || "$PY" -m venv "$DEST/venv"
  uv pip install --python "$DEST/venv/bin/python" --quiet -r "$DEST/requirements.txt"
else
  "$PY" -m venv "$DEST/venv"
  "$DEST/venv/bin/pip" install --quiet --upgrade pip
  "$DEST/venv/bin/pip" install --quiet -r "$DEST/requirements.txt"
fi

PYBIN="$DEST/venv/bin/python"
SERVER="$DEST/server.py"

register_json() {
  local file="$1"
  [ -f "$file" ] || return 0
  "$PY" - "$file" "$PYBIN" "$SERVER" <<'PY'
import json, sys
path, pybin, server = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    with open(path, encoding='utf-8') as f:
        data = json.load(f)
except Exception:
    sys.exit(0)
servers = data.setdefault('mcpServers', {})
if 'bql' in servers:
    print('  bql 已存在，跳过')
else:
    servers['bql'] = {'command': pybin, 'args': [server]}
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write('\n')
    print('  已注册 bql')
PY
}

register_codex() {
  local file="$HOME/.codex/config.toml"
  [ -f "$file" ] || return 0
  if grep -q '\[mcp_servers.bql\]' "$file"; then
    say "Codex 已注册 bql，跳过"
  else
    cat >> "$file" <<EOF

[mcp_servers.bql]
command = "$PYBIN"
args = ["$SERVER"]
EOF
    say "Codex 已注册 bql"
  fi
}

say "注册到已安装的 Agent"
register_json "$HOME/.workbuddy/mcp.json"
register_json "$HOME/.cursor/mcp.json"
register_json "$HOME/Library/Application Support/Claude/claude_desktop_config.json"
register_json "$HOME/.claude/claude_desktop_config.json"
register_codex

say ""
say "安装完成！"
if [ -n "$BASE_URL" ] && [ -n "$TOKEN" ]; then
  say "已预配置，直接在 Agent 对话里说一句即可："
  say "  用 BQL 分析这组数据：32,53,57,14,48,41，规格 0~100"
else
  say "下一步（任选其一）："
  say "  1) 在 Agent 对话里说：帮我配置 BQL 连接器，base_url=... token=..."
  say "  2) 编辑 $DEST/config.json"
fi
say "然后在 Agent 的连接器/设置里信任（Trust）bql。"
say "仓库与文档：$REPO"
