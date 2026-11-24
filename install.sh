#!/usr/bin/env bash
# BQL MCP Connector 一键安装脚本
# 用法: curl -fsSL https://raw.githubusercontent.com/adymilk/bql-mcp/main/install.sh | bash
# 指定版本: BQL_VERSION=1.3.0 curl -fsSL .../install.sh | bash
# 指定目录: BQL_MCP_DIR=/path curl -fsSL ... | bash -s -- <base_url> <token>
set -euo pipefail

REPO="https://github.com/adymilk/bql-mcp"
REF="${BQL_REF:-${BQL_VERSION:+v${BQL_VERSION}}}"
REF="${REF:-main}"
RAW="${BQL_RAW_BASE:-https://raw.githubusercontent.com/adymilk/bql-mcp/$REF}"
USER_HOME="${BQL_HOME:-$HOME}"
DEST="${BQL_MCP_DIR:-$USER_HOME/.workbuddy/connectors/bql}"
PY="${PYTHON:-python3}"
BASE_URL="${1:-${BQL_BASE_URL:-}}"
TOKEN="${2:-${BQL_TOKEN:-}}"

say() { printf '\033[1;34m[bql-mcp]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[bql-mcp]\033[0m %s\n' "$*"; }
err() { printf '\033[1;31m[bql-mcp]\033[0m %s\n' "$*" >&2; }

command -v "$PY" >/dev/null 2>&1 || { err "需要 Python 3，$PY 未找到"; exit 1; }
command -v curl >/dev/null 2>&1 || { err "需要 curl，未找到"; exit 1; }
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' || { err "需要 Python 3.10+"; exit 1; }

say "安装到 ${DEST}（来源 ${REF}）"
mkdir -p "$DEST"

FILES="server.py report.py quality.py minitab_doc.py report_templates.py interpret.py exporter.py settings.html endpoints.json config.example.json requirements.txt VERSION CHANGELOG.md"
for f in $FILES; do
  say "下载 $f"
  curl -fsSL "$RAW/$f" -o "$DEST/$f"
done

# 报告模板：内置模板每次升级覆盖；用户目录只补齐示例，不覆盖已有文件
TEMPLATES="builtin/aurora.html builtin/audit.html"
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
else
  "$PY" -m venv "$DEST/venv"
fi

if [ -x "$DEST/venv/bin/python" ]; then
  PYBIN="$DEST/venv/bin/python"
elif [ -x "$DEST/venv/Scripts/python.exe" ]; then
  PYBIN="$DEST/venv/Scripts/python.exe"
else
  err "虚拟环境创建失败：未找到 Python 解释器"
  exit 1
fi

if command -v uv >/dev/null 2>&1; then
  uv pip install --python "$PYBIN" --quiet -r "$DEST/requirements.txt"
else
  "$PYBIN" -m pip install --quiet --upgrade pip
  "$PYBIN" -m pip install --quiet -r "$DEST/requirements.txt"
fi

SERVER="$DEST/server.py"

register_json() {
  local file="$1"
  local parent
  parent="$(dirname "$file")"
  [ -d "$parent" ] || return 0
  "$PY" - "$file" "$PYBIN" "$SERVER" <<'PY'
import json
import os
import stat
import sys

path, pybin, server = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    with open(path, encoding='utf-8') as f:
        data = json.load(f)
except FileNotFoundError:
    data = {}
except (OSError, ValueError) as exc:
    print(f"  跳过 {path}：{exc}")
    sys.exit(0)

servers = data.setdefault('mcpServers', {})
servers['bql'] = {'command': pybin, 'args': [server]}
try:
    mode = stat.S_IMODE(os.stat(path).st_mode)
except OSError:
    mode = 0o600
tmp = path + '.bql-install.tmp'
with open(tmp, 'w', encoding='utf-8') as f:
    json.dump(data, f, ensure_ascii=False, indent=2)
    f.write('\n')
os.replace(tmp, path)
try:
    os.chmod(path, mode)
except OSError:
    pass
print(f'  已注册/更新 bql：{path}')
PY
}

register_codex() {
  local codex_home="${CODEX_HOME:-$USER_HOME/.codex}"
  local file="$codex_home/config.toml"
  [ -d "$codex_home" ] || return 0
  "$PY" - "$file" "$PYBIN" "$SERVER" <<'PY'
import json
import os
import stat
import sys

path, pybin, server = sys.argv[1], sys.argv[2], sys.argv[3]
header = "[mcp_servers.bql]"
block = [header, f"command = {json.dumps(pybin)}", f"args = [{json.dumps(server)}]"]
try:
    with open(path, encoding="utf-8") as f:
        lines = f.read().splitlines()
except FileNotFoundError:
    lines = []
except OSError as exc:
    print(f"  跳过 {path}：{exc}")
    raise SystemExit(0)

start = next((i for i, line in enumerate(lines) if line.strip() == header), None)
if start is None:
    if lines and lines[-1] != "":
        lines.append("")
    lines.extend(block)
else:
    end = start + 1
    while end < len(lines) and not lines[end].lstrip().startswith("["):
        end += 1
    lines[start:end] = block

try:
    mode = stat.S_IMODE(os.stat(path).st_mode)
except OSError:
    mode = 0o600
tmp = path + ".bql-install.tmp"
with open(tmp, "w", encoding="utf-8") as f:
    f.write("\n".join(lines).rstrip() + "\n")
os.replace(tmp, path)
try:
    os.chmod(path, mode)
except OSError:
    pass
print(f"  已注册/更新 bql：{path}")
PY
}

say "注册到已安装的 Agent"
register_json "$USER_HOME/.workbuddy/mcp.json"
register_json "$USER_HOME/.cursor/mcp.json"
register_json "$USER_HOME/Library/Application Support/Claude/claude_desktop_config.json"
register_json "$USER_HOME/.config/Claude/claude_desktop_config.json"
register_json "$USER_HOME/.claude/claude_desktop_config.json"
register_codex

# 命令行入口：装完可直接 `~/.workbuddy/connectors/bql/bql-mcp --self-check`
LAUNCHER="$DEST/bql-mcp"
cat > "$LAUNCHER" <<EOF
#!/usr/bin/env bash
exec "$PYBIN" "$SERVER" "\$@"
EOF
chmod +x "$LAUNCHER"

run_launcher() {
  if [ -x "$LAUNCHER" ]; then
    "$LAUNCHER" "$@"
  else
    "$PYBIN" "$SERVER" "$@"
  fi
}

say ""
say "安装完成，自检结果："
run_launcher --self-check || warn "自检未通过（多半是还没配置 base_url + token）"

say ""
say "版本：$(run_launcher --version 2>/dev/null || echo '未知')"
if [ -n "$BASE_URL" ] && [ -n "$TOKEN" ]; then
  say "已预配置，直接把这句话丢给 Agent 跑一次："
  say "  用 BQL 跑一次过程能力分析：数据 12.5,13.1,11.8,12.9,12.2,13.4,12.0,12.7，USL 15（单侧），把报告路径和结论给我"
else
  say "下一步（任选其一）："
  say "  1) 在下面的配置页里填 base_url 与 token（推荐）"
  say "  2) 在 Agent 对话里说：帮我配置 BQL 连接器，base_url=... token=..."
  say "  3) 直接编辑 $DEST/config.json"
fi
say "然后在 Agent 的连接器/设置里信任（Trust）bql。"
say "仓库 / 版本历史：$REPO · $REPO/blob/main/CHANGELOG.md"

# 装完自动打开本地配置页：常驻 127.0.0.1，URL 带一次性 token
open_settings_ui() {
  local url="" file="$DEST/settings.url"
  if [ "${BQL_NO_UI:-0}" = "1" ]; then
    say "（BQL_NO_UI=1，已跳过配置页启动）"
    return 0
  fi
  if [ -f "$file" ]; then
    local cached
    cached="$(cat "$file" 2>/dev/null || true)"
    if [ -n "$cached" ] && curl -fsS -o /dev/null -m 2 "$cached" 2>/dev/null; then
      url="$cached"
      say "复用已在运行的配置页"
    fi
  fi
  if [ -z "$url" ]; then
    rm -f "$file"
    nohup "$PYBIN" "$SERVER" --settings --no-open </dev/null >"$DEST/settings.log" 2>&1 &
    local i=0
    while [ "$i" -lt 20 ]; do
      if [ -s "$file" ]; then
        url="$(cat "$file" 2>/dev/null || true)"
        [ -n "$url" ] && break
      fi
      i=$((i + 1))
      sleep 0.3
    done
  fi

  if [ -z "$url" ]; then
    warn "配置页没起来（看 $DEST/settings.log），可手动运行：$LAUNCHER --settings"
    return 0
  fi
  say ""
  say "配置页：$url"
  if [ "${BQL_NO_OPEN:-0}" = "1" ]; then
    say "（BQL_NO_OPEN=1，已跳过自动打开浏览器）"
    return 0
  fi
  case "$(uname -s)" in
    Darwin) open "$url" >/dev/null 2>&1 || true ;;
    Linux) xdg-open "$url" >/dev/null 2>&1 || true ;;
    MINGW*|MSYS*|CYGWIN*) start "" "$url" >/dev/null 2>&1 || true ;;
  esac
  say "已在浏览器打开配置页；改完点「保存」即可，无需重启 Agent 也能生效。"
  say "（页面打不开就重跑：$LAUNCHER --settings）"
}

open_settings_ui
