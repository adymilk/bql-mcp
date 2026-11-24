# BQL MCP 连接器

一个标准 **MCP Server（stdio）**，把 BQL 的分析接口包装成通用 Agent 可直接对话调用的质量分析工具。

当前版本 **v1.3.1** · 110 个分析接口 · 版本历史见 [CHANGELOG.md](CHANGELOG.md)。

> 装完会自动在浏览器打开本地配置页（`http://127.0.0.1:<port>/settings?token=…`）；
> 想再打开：`~/.workbuddy/connectors/bql/bql-mcp --settings`（`--no-open` 只打印地址不弹窗）。

## 一句话安装

把这句话（连同命令）整段丢给任意支持 MCP 的 Agent 即可：

> 帮我安装 BQL MCP 连接器：执行 `curl -fsSL https://raw.githubusercontent.com/adymilk/bql-mcp/main/install.sh | bash`，装完把 bql 注册进本机 MCP 配置，然后用 `bql-mcp --self-check` 把版本和配置状态报给我。

只想要命令：

```bash
curl -fsSL https://raw.githubusercontent.com/adymilk/bql-mcp/main/install.sh | bash
```

**零配置版**（装完即用，无需再填任何东西）：

```bash
curl -fsSL https://raw.githubusercontent.com/adymilk/bql-mcp/main/install.sh | BQL_BASE_URL=https://your-bql-host BQL_TOKEN=your-token bash
```

**装指定版本**（可复现部署 / 回滚）：

```bash
curl -fsSL https://raw.githubusercontent.com/adymilk/bql-mcp/main/install.sh | BQL_VERSION=1.3.1 bash
```

脚本会自动：下载连接器与报告模板 → 建 venv 装依赖（检测到 `uv` 时走极速安装）→ 写入配置 → 注册到 WorkBuddy / Cursor / Claude Desktop / Codex。
无图形界面的服务器可加 `BQL_NO_UI=1` 跳过配置页启动；隔离测试还支持 `BQL_HOME` 与 `BQL_RAW_BASE`。
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
| `bql_version` | 查版本、安装目录、接口数、模板列表、是否已配置 |
| `bql_configure` | 在对话里设置 BASE_URL / TOKEN / 结果通道等（无需打开网页） |
| `bql_list_endpoints` | 列出已注册的分析接口（110 个，含控制图、正态性、MSA、DOE 等） |
| `bql_describe_endpoint` | 查看某接口的必填/选填字段 |
| `bql_full_analysis` | **首选**：一步出完整报告——能力 + 控制图 + 正态性检验（3 个接口并发） |
| `bql_quick_analysis` | 轻量：只要 Cpk/Ppk 数字时用，只调能力接口 |
| `bql_call` | 调用接口，等待并返回完整分析结果；自动生成可视化报告 + 确定性解读 |
| `bql_list_templates` | 列出报告模板（内置 + 用户自定义）及当前模板 |
| `bql_set_template` | 切换报告模板，例如 `bql_set_template("audit")` |
| `bql_export` | 把报告导出为 PDF / Word / PPT / HTML / Markdown |
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

### 报告结构：三层披露

报告按「3 秒决策 → 证据 → 追溯」组织。模板只决定配色与版式，信息层级由 `report.py` 统一生成，
所以所有内置模板与用户自定义模板拿到的是同一套阅读顺序。

**L1 判决层（首屏，不滚动）**

| 内容 | 说明 |
|---|---|
| 放行判决 | `放行` / `有条件放行` / `拒收`，闸门式结论，先于能力等级出现 |
| 判决理由 | 一句话说清为什么；不受控时明确写「Cpk 不能直接采信」 |
| 三个数字 | Cpk（含 95% 下限）/ Ppk / 超差件数，其余数字全部下移 |
| 判定状态条 | 受控 → 正态 → 能力 三列，未做的那步显式标「未评估」 |

阈值在 `config.json` 的 `report.disposition` 里改：

```json
"disposition": { "release_cpk": 1.33, "hold_cpk": 1.00 }
```

判据优先级：有超差 → 拒收；过程不受控 → 有条件放行（能力数不可采信）；
未做控制图 → 不判放行；之后再按 Cpk 与 release / hold 分档。

**首屏阻断**：过程不受控时，首屏三个数字格改为 `判异 N 点 / Cpk（参考值）/ 超差 N 件`，
Cpk 灰色降级并标注「不可用于判定」，等级徽标同步加「（参考）」。理由是 Cpk 的定义前提
就是过程受控——不受控时把 Cpk 当结论展示，等于让读者记住一个不可采信的数字。
可信边界行会给出 Cpk 的 95% 区间（Bissell 近似），量化「单点 Cpk 有多不牢」。

**L2 证据层（默认展开，一图一句结论）**

| 板块 | 内容 |
|---|---|
| 过程稳定性 | 控制图（Xbar-R / I-MR）+ 控制限 + Nelson 判异 + 结论带（「第 24 点越限，MR 同步异常 → 一次性特殊原因」） |
| 过程能力 | Cp / Cpk / Pp / Ppk + 均值 + 组内/整体 σ + 归因结论（问题在中心还是在批间） |
| 改善建议 | 三步以内、按成本排序，每步给可验证结果；0 成本的排前面，附 MSA 提示 |

**L3 附录层（默认折叠，标题行带摘要）**

Minitab 完整输出 / 正态性检验详情 / 完整指标与过程特性 / 完整解读与风险清单 / 原始数据与报告信息。
折叠标题右侧直接显示关键值（如 `Cpk 1.183 · Ppk 1.144`），不展开也知道里面是什么；
打印与导出 PDF 时由 `beforeprint` 自动全部展开，内容不会缺失。

实测同一组 100 点数据：总高从 5730px（7 屏）降到 2849px（3.5 屏），
改善建议从第 6 屏提到第 2.4 屏，首屏即可看完判决、三个数字与判定状态条。
过程完全受控且全部达标时，三张证据卡会再收进一个「过程证据与明细」折叠区，整页 1 屏。

### 明细表：与图对齐的查阅口径

工程师看明细表只有一个动机——**图上第 N 点不对，去表里核实**。所以表不是数据罗列，
而是控制图的索引：

| 设计 | 做法 |
|---|---|
| 观测号可定位 | 分组表给出该组点号区间（`#21–25`），与 X̄ 图横轴一一对应 |
| 三类异常分列 | `超差`（红，超规格）/ `异常`（琥珀，控制图判异）/ `正常`；判异列写明 `X̄↑`、`R↑`、`同侧`、`趋势` |
| 极差可视化 | R 单元格内嵌细条，宽度为 `R ÷ UCL`，一眼看出哪组离散度顶到控制限 |
| 分组宽表 | 子组 × 观测值平铺，n>5 时自动补 s 列；超过 25 列退化为单值分栏，避免横向翻不完 |
| 单值分栏 | 每 25 点一栏，列 `# / 测量值 / 偏差 / 判定`，偏差相对目标值或样本均值 |

判异点来自 `quality.control_check` 返回的 `beyond_points / rule2_points / rule3_points`，
与图上的判定同源，因此不会出现「图说越限、表说正常」的两套账。

正态性检验的 AD/p 值由本地纯 Python 计算（与 scipy 的统计量完全一致，已对三种分布交叉验证），
概率图取 Minitab 输出，两者互相印证。

### 表格与导航（模板无需改造）

- **超宽表格**：`group_size` 在 2–25 之间按子组展开宽表，容器 `overflow-x:auto` 横向滚动；
  首列（子组号/序号）`position:sticky` 冻结，滚到右侧仍知道是哪一行；滚动条细样式 + `← →` 提示条。
- **右侧锚点**：`render_report` 在 `</body>` 前统一注入 `NAV_STYLE + NAV_SCRIPT`，
  自动扫描 `.card` / `.verdict-card` 的标题生成导航，无需修改任何模板；
  用 `.sec .idx` 拼接序号（如 `06 · 能力解读与改善潜力`），滚动时高亮当前区块，窄屏（≤1240px）与打印自动隐藏。
- 注入是运行期的，静态 HTML 里只有一段 `<script>`，导出 docx/pptx（读指标 JSON）不受影响。

### 输出纪律（不要啰嗦）

MCP 侧已经锁死"说多少"，接管不住的部分靠宿主规则补：

| 层 | 手段 | 作用 |
|---|---|---|
| 协议层 | `FastMCP(instructions=...)` | `initialize` 时下发使用纪律，支持 MCP instructions 的宿主都会读 |
| 工具层 | `bql_quick_analysis` 的 description | 工具描述始终在模型上下文里，最稳的一道 |
| 返回层 | `answer` 只包含报告链接 | 没有指标段落需要复述 |
| 宿主层 | WorkBuddy 用户级 `MEMORY.md` 硬规则 | 唯一能真正管住宿主 Agent「播报欲」的地方，换机器/换人不迁移 |

`answer` 字段就是给用户看的最终文案，直接原文输出即可：

```text
报告：http://127.0.0.1:8765/report/capa-wb-0c0ad48eb75d.html?token=...
```

报告默认按 `config.json` 的 `report.template` 渲染（默认 `aurora` 白色），
分析完成只返回本地报告链接，不自动打开浏览器；显式调用 `bql_open_report` 时才打开报告。

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

报告内容（按阅读顺序）：
1. **页头**：报告标题（取自 Minitab 输出标题）、变量、规格、子组、样本量与生成信息
2. **结论卡**：A/B/C/D 等级 + 结论 + Cpk/Ppk/Cp/Pp 四张指标卡
3. **专家解读**：规则层确定性结论（关键发现 / 风险提示 / 行动建议）
4. **Minitab 完整输出**：解析 `mtbOutDoc`，把 Minitab 的**全部图片、表格和文字注释**原样搬进报告
5. **分布与规格图**：SVG 观测点 + 整体分布拟合曲线 + USL/均值/目标线
6. **明细**：能力指数、过程特性（PPM 口径）
7. **原始数据与产出物**：观测值 chip、OTGraph、Minitab 报告、原始数据 / 工作表 CSV

### Minitab 完整输出（重点）

报告不再只放第一张图，而是解析 Minitab 的完整输出文档 `mtbOutDoc`：

- **所有图**全部展示（如箱线图 3 张、六合一图 6 张），图片本就以 base64 内嵌在文档里，**无需联网**
- **所有表格**转成语义化 `<table>`：表头 `thead` 吸顶、斑马纹、数字右对齐等宽字体、窄屏横向滚动
- **所有文字**（警告 / 注释 / 收敛失败提示）合并为紧凑注释块
- 解析失败时自动兜底：改用 `OTGraph` 全部图片 + `OTTable` / `OTMessage` 纯文本表

解析逻辑在 `minitab_doc.py`，纯标准库（`html.parser`），不引入第三方依赖。

### 内置报告模板

**全部为浅色模板**，固定 `color-scheme: light`，不跟随系统深色模式，也不提供深色皮肤。

| 模板 ID | 名称 | 风格 |
|---------|------|------|
| `aurora`（默认） | 极光 · 阅读优先 | 白色背景、靛蓝主色；结论先行、编号章节、Minitab 全量输出；打印保持浅色 |
| `audit` | 审计白皮书 | 浅色打印版式，适合 PPAP / 客户审核 |

原 `midnight` / `executive` 深色模板已停用，文件移到 `templates/archive/`（不再出现在 `bql_list_templates` 中，也不可被 `bql_set_template` 选中）。

切换方式（任选其一）：
- 对话里：`bql_set_template("audit")`
- 配置页「报告样式」卡片直接点选
- 编辑 `config.json`：`"report": {"template": "audit"}`

### 报告编辑与导出

打开报告后点右下角「编辑报告」，可修改标题和报告结论/工程师评审意见；「编辑布局」中可拖拽区块排序、隐藏暂不需要的区块，并将当前设置保存为可复用布局。系统判定、SPC 指标和分析证据保留在原始记录中，不会被评审意见覆盖。

编辑完成后先保存报告，再导出 PDF 或 Word。两个格式都从已保存的 HTML 报告生成，因此会保留当前区块顺序、标题和评审意见；PDF 优先使用本机渲染器，没有渲染器时报告页会打开浏览器打印窗口，可选择「存储为 PDF」。MCP 侧也可使用 `bql_export(name, "pdf")` 或 `bql_export(name, "docx")`。

### 自定义模板

把任意 HTML 文件放到 `~/.workbuddy/connectors/bql/templates/user/`，文件名即模板 ID（如 `mybrand.html`）。
模板支持占位符：`{{title}}`、`{{report_title}}`、`{{minitab_meta}}`、`{{spec_legend}}`、`{{cpk}}`、`{{verdict}}`
等标量，以及 `{{{minitab_full}}}`（Minitab 图 + 表 + 文字全套）、`{{{minitab_chart}}}`（仅首图）、
`{{{kpis}}}`、`{{{interpretation}}}`、`{{{distribution}}}`、`{{{base_css}}}` 等原始区块。详见
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

## 版本管理与升级

- 版本号唯一来源：仓库根 `VERSION`；语义化版本，改动记录在 [CHANGELOG.md](CHANGELOG.md)，对应 git tag `vX.Y.Z` 与 GitHub Release。
- 查当前版本：`~/.workbuddy/connectors/bql/bql-mcp --version`，或在 Agent 里调 `bql_version`。
- 打开配置页：`bql-mcp --settings`（报告工作台也会由 MCP 独立进程自动启动；加 `--no-open` 只打印地址）。
- 自检：`bql-mcp --self-check`（未配置返回码 3，方便脚本判断）。
- 升级：重跑安装脚本即可覆盖代码与内置模板；**`config.json` 与 `templates/user/` 不会被覆盖**。
- 回滚/锁定版本：`BQL_VERSION=1.0.0` 触发同一条安装命令。
- 发布流程：改代码 → 更新 `VERSION` 与 `CHANGELOG.md` → `git tag vX.Y.Z && git push --tags` → `gh release create`。
