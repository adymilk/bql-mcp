# Changelog

本连接器遵循 [语义化版本](https://semver.org/lang/zh-CN/)：`主版本.次版本.补丁`。
版本号唯一来源是仓库根的 `VERSION` 文件，`bql_version` 工具与 `bql-mcp --version` 都读它。
升级只需重跑安装脚本覆盖代码，`config.json` 不会被覆盖。

## [1.1.0] - 2026-11-23

### 新增
- `bql_version` 工具：返回版本、仓库、安装目录、接口数、模板列表与配置状态。
- 命令行自检：`python server.py --version` / `--self-check`（安装脚本与排障用）。
- 单页配置界面 `settings.html`（浅色），支持连接测试与模板切换。
- 报告模板可切换：`aurora`（默认，阅读优先）、`audit`（审计优先）。

### 改进
- 报告改为三层披露：判决层（放行/拒收 + 三数字）→ 证据层（控制图/能力/改善建议）→ 附录层（折叠）。
- 报告展示 Minitab 的**全部**图表与文字（`minitab_doc.py` 解析 `mtbOutDoc`），不再只取首图。
- 右侧锚点导航补齐折叠区：每个 `details.fold` 独立成锚点，点击先展开再定位；折叠区内的隐藏项不参与高亮判定。
- 全部模板固定浅色，不再跟随系统深色；打印时自动展开折叠区。
- 接口从 8 个扩到 110 个（覆盖能力分析 / 量具 MSA / 控制图 / 假设检验 / 回归 / 分布与可靠性 / 图形）。

### 修复
- 单侧规格（`lspec=0` 哨兵）不再画 LSL 0 线、不再按规格中心判"未对中"，改善建议按可用的一侧计算。
- `>USL` / `<LSL` 实为 PPM，报告与解读口径统一为 PPM/百分比，不再写成"观测点数"。
- `out_of_spec` 误把合计 PPM 当件数，导致"超差 333333 件"。
- 导航在页顶无高亮（`__top` 未加入链接集合）。

## [1.0.0] - 2026-11-17

### 首个版本
- stdio MCP 服务，封装 BQL（Minitab）质量分析：能力分析、控制图、正态性检验。
- 一键式 `bql_full_analysis` / 轻量 `bql_quick_analysis`，结果经 WebSocket 回传并渲染自包含 HTML 报告。
- `install.sh` 零配置安装：拉取文件、建 venv、注册到 WorkBuddy / Cursor / Claude Desktop / Codex。
