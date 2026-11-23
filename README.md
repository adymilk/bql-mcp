# BQL MCP 连接器

一个标准 **MCP Server（stdio）**，把 BQL 的分析接口包装成通用 Agent 可直接对话调用的质量分析工具。

## 一句话安装

最简（装完在 Agent 里说一句"帮我配置 BQL 连接器"）：

```bash
curl -fsSL https://raw.githubusercontent.com/adymilk/bql-mcp/main/install.sh | bash
```

**零配置版**（装完即用，无需再填任何东西）：

```bash
curl -fsSL https://raw.githubusercontent.com/adymilk/bql-mcp/main/install.sh | BQL_BASE_URL=https://your-bql-host BQL_TOKEN=your-token bash
```

脚本会自动：下载连接器与报告模板 → 建 venv 装依赖（检测到 `uv` 时走极速安装）→ 写入配置 → 注册到 WorkBuddy / Cursor / Claude Desktop / Codex。
装完后在 Agent 的连接器管理里信任（Trust）`bql`，直接说：

> 用 BQL 分析这组数据：32,53,57,14,48,41，规格 0~100

> 安全说明：Token 只存本地 `config.json`（权限 0600），不会写入 mcp 配置、不会进对话回显、不在本仓库中。分发给他人时请为每人签发独立 token，便于单独撤销。

**设计要点**
- 任何支持 MCP 的通用 Agent 都能用：WorkBuddy、Codex CLI、Claude Desktop、Cursor、Cline 等。
- 使用方式是在 Agent 对话里让模型调用工具，不需要打开网页、不需要新建对话页面。
- **结果通过 WebSocket 出站连接获取**：MCP 主动连外网 BQL 服务的广播地址，不需要公网 IP、不需要本地 webhook 回调。
- POST 的 202 响应里服务端会返回广播地址/端口/channel，自动解析使用，通常无需手填 WS 参数。

## 提供的工具

| 工具 | 作用 |
|------|------|
| `bql_configure` | 在对话里设置 BASE_URL / TOKEN / 结果通道等（无需打开网页） |
| `bql_list_endpoints` | 列出已注册的分析接口（当前：capa） |
| `bql_describe_endpoint` | 查看某接口的必填/选填字段 |
| `bql_quick_analysis` | **推荐**：一步完成能力分析，直接传一列数据 + 规格限，返回精简结论 |
| `bql_call` | 调用接口，等待并返回完整分析结果；自动生成可视化报告 + 确定性解读 |
| `bql_list_templates` | 列出报告模板（内置 + 用户自定义）及当前模板 |
| `bql_set_template` | 切换报告模板，例如 `bql_set_template("audit")` |
| `bql_export` | 把报告导出为 docx / pptx / html / md |
| `bql_open_report` | 打开最近一次分析的可视化报告（可传 `path` 打开指定报告） |
| `bql_open_settings` | 可选：打开本地可视化配置页 |

### MCP Resources（Agent 可直接读取的上下文）

| URI | 内容 |
|-----|------|
| `bql://reports` | 报告列表 |
| `bql://report/{name}` | 单份 HTML 报告 |
| `bql://metrics/{name}` | 结构化指标 JSON（含解读） |
| `bql://endpoint/{name}` | 接口字段定义 |

### MCP Prompts（可审计、可定制的工作流模板）

- `bql_quality_interpret`：以质量工程专家身份解读分析结果，支持行业背景（通用制造 / 汽车 IATF / 航空 AS9100 / 医疗 FDA）。
- `bql_analyze_workflow`：完整分析工作流（配置 → 字段确认 → 调用 → 解读 → 导出）。

解读采用双层设计：**server 端规则层做确定性判定**（可信、可审计），**Prompt 借用 client 的 LLM 做深度表达**——因此不依赖任何服务端 LLM Key，任何通用 Agent 都能得到专家级解读。

## 对话里怎么用（通用）

1. 配置一次：
   > 帮我配置 BQL 连接器：base_url 是 https://your-bql-host，token 是 xxx

   Agent 会调用 `bql_configure`。Token 只存本地 `config.json`（权限 0600），不会写进 mcp 配置。

2. 之后直接说：
   > 用 BQL 跑一个正态能力分析，数据是 32/53/57/14/48/41，规格 0~100，子组大小 1，type 用 CStat。

   Agent 先 `bql_describe_endpoint("capa")` 看字段，再 `bql_call(...)`，然后解读返回的 Cpk/Ppk 等指标。

3. 新增接口：在 `endpoints.json` 加一条配置即可，主程序不用改。

## 最快路径（推荐）

分析请求只有两步，不要先查接口、不要先 describe：

```text
bql_quick_analysis(data="32,53,57,14,48,41", lspec=0, uspec=100)
```

一次返回：能力判定、Cpk / Ppk / 均值 / 标准差、报告路径、Minitab 图链接、专家解读。
`data` 支持 `[32,53]`、`"32,53"`、`[{"x":32}]` 三种写法。

## 性能设计（为什么快）

| 优化 | 效果 |
|------|------|
| `bql_quick_analysis` 一步到位 | 省掉 1–2 次 LLM 工具往返（通常数秒） |
| HTTP 连接池复用（keep-alive） | 省掉每次调用的 TLS 握手 |
| POST 与 WebSocket 订阅并行 | 省掉「提交 → 连接 → 订阅」的串行等待 |
| 广播地址跨调用缓存并预订阅 | 第二次起进一步缩短首字节时间 |
| 报告图片内嵌改为后台任务 | 报告立即返回（约 20 KB），不再被图片下载阻塞 |
| 接口表内存缓存 + 导出依赖延迟加载 | 降低每次调用与进程启动开销 |
| 返回精简（默认不带 raw） | 减少传输与模型上下文开销 |

剩余耗时主要来自 Minitab 服务端计算本身（通常数秒），客户端侧已压到毫秒级。

## 结果可视化

每次 `bql_call` 成功后会自动生成一份**自包含的单文件 HTML 报告**，无需前端依赖或外网 CDN：

```
~/.workbuddy/connectors/bql/reports/<接口>-<channel>.html
```

报告内容：
- **Minitab 能力分析图优先**：服务端原始输出图内嵌为 base64，**在所有模板中都是首屏第一块内容**，无需外网加载
- **能力判定**：依据 Cpk 给出 A/B/C/D 等级与结论（充分 / 充足 / 可接受 / 不足）
- **大数字指标卡**：Cpk、Ppk、Cp、Pp，按阈值着色
- **专家解读**：规则层确定性结论（关键发现 / 风险提示 / 行动建议）
- **分布与规格图**：SVG 绘制的观测点 + 整体分布拟合曲线 + LSL/USL/均值/目标线
- **分组指标表**：能力指数、过程特性（均值/标准差/样本量/不良数）
- **原始数据 + 产出物链接**：观测值 chip、OTGraph、Minitab 报告、CSV

### 内置报告模板

| 模板 ID | 名称 | 风格 |
|---------|------|------|
| `aurora`（默认） | 极光 · 浅色旗舰 | 白色背景、靛蓝主色、精致卡片与柔和阴影，Stripe / Linear 浅色设计语言 |
| `midnight` | 午夜 · 深色专业 | 深色科技感 |
| `audit` | 审计白皮书 | 浅色打印版式，适合 PPAP / 客户审核 |
| `executive` | 决策驾驶舱 | 深色高对比，Cpk 大数字 + Minitab 图并排，适合管理层汇报 |

切换方式（任选其一）：
- 对话里：`bql_set_template("audit")`
- 配置页「报告样式」卡片直接点选
- 编辑 `config.json`：`"report": {"template": "audit"}`

### 自定义模板

把任意 HTML 文件放到 `~/.workbuddy/connectors/bql/templates/user/`，文件名即模板 ID（如 `mybrand.html`）。
模板支持占位符：`{{title}}`、`{{cpk}}`、`{{verdict}}` 等标量，以及 `{{{minitab_chart}}}`、`{{{kpis}}}`、
`{{{interpretation}}}`、`{{{distribution}}}`、`{{{base_css}}}` 等原始区块。详见
`templates/user/README.md` 与 `mybrand.html.example`。用户模板与内置同名时会覆盖内置模板。

返回结构里新增字段：
- `report_path`：报告文件路径
- `summary` 末尾附带报告路径

对话里可视化：
- WorkBuddy：直接说"打开刚才的分析报告"，Agent 调用 `bql_open_report`；`report_path` 也可用预览面板打开。
- 其他 Agent：`bql_call` 返回的 `report_path` 是绝对路径，用浏览器打开即可。

报告渲染逻辑在 `report.py`，纯标准库，渲染失败不会导致分析调用失败（会返回 `report_error`）。

## 注册到各 Agent

服务端的绝对路径：`/Users/james/.workbuddy/connectors/bql/venv/bin/python /Users/james/.workbuddy/connectors/bql/server.py`

### WorkBuddy
已注册在 `~/.workbuddy/mcp.json`（`bql` 条目）。到连接器管理页点一次“信任”即可在对话里调用 `bql_*` 工具。

### Codex CLI
写入 `~/.codex/config.toml`：

```toml
[mcp_servers.bql]
command = "/Users/james/.workbuddy/connectors/bql/venv/bin/python"
args = ["/Users/james/.workbuddy/connectors/bql/server.py"]
```

### Claude Desktop / Claude Code
写入 `~/Library/Application Support/Claude/claude_desktop_config.json`：

```json
{
  "mcpServers": {
    "bql": {
      "command": "/Users/james/.workbuddy/connectors/bql/venv/bin/python",
      "args": ["/Users/james/.workbuddy/connectors/bql/server.py"]
    }
  }
}
```

### Cursor / Cline / 其他
选择添加自定义 MCP Server（stdio），填同样的 `command` 和 `args`。

> 注意：不同机器路径不同，换成实际的 Python venv 路径。

## 结果通道说明

- **websocket（默认，推荐）**：MCP 出站连接服务端广播地址，穿透 NAT，不需要公网 IP。
- **webhook（可选）**：只有当你有一个**公网可达**的回调 URL 时才有意义。本地 `127.0.0.1` 地址外网服务回调不到，代码会自动忽略，绝不传给服务端。要用就配置 `defaults.webhook` 为公网 URL（可含 `{channel}` 占位符）或 `webhook.public_base_url`。
- **auto**：websocket 始终启用；webhook 仅在配置了公网地址时同时启用，谁先返回结果用谁。

## 端到端验证状态

已用真实服务跑通：`POST /api/Capa` → 202 返回广播地址 → WebSocket 订阅 → 收到 `App\Events\BroadcastAnalysisResults` → 解析出 Cpk/Ppk/均值/标准差及 OTGraph、Minitab 文档、原始数据链接。
