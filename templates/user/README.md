# 自定义报告模板

把任意 `.html` 文件放进本目录（`~/.workbuddy/connectors/bql/templates/user/`），
即可在配置页的「报告模板」或对话里通过 `bql_set_template` 选用。

- 文件名即模板 ID，例如 `mybrand.html` → 模板名 `mybrand`。
- 与内置模板同名的用户模板会**覆盖**内置模板。
- 模板是完整 HTML，可以写自己的 CSS、Logo、页眉页脚、公司配色。
- 报告仍是单文件，Minitab 图表会被内嵌为 base64 图片，无需外网。

## 占位符

| 写法 | 含义 |
|------|------|
| `{{key}}` | 转义后的文本，用于变量值（安全） |
| `{{{key}}}` | 原始 HTML，用于报告区块 |

### 文本标量

`title` `subtitle` `endpoint` `variable` `analysis_type` `generated_at` `date` `time`
`channel` `source` `grade` `verdict` `verdict_color` `spec` `group_size`
`cpk` `ppk` `cp` `pp` `sample_n` `minitab_url` `doc_url` `csv_url` `broadcast`

### 原始 HTML 区块

| 占位符 | 内容 |
|--------|------|
| `{{{base_css}}}` | 通用组件样式（指标卡/表格/图表等，使用 CSS 变量，可换主题） |
| `{{{minitab_chart}}}` | **Minitab 能力分析图（首屏优先展示的区块）** |
| `{{{kpis}}}` | Cpk / Ppk / Cp / Pp 四张指标卡 |
| `{{{interpretation}}}` | 专家解读（结论 / 风险 / 行动） |
| `{{{distribution}}}` | 数据分布与规格限 SVG 图 |
| `{{{index_rows}}}` | 能力指数表 |
| `{{{stat_rows}}}` | 过程特性表 |
| `{{{raw_data}}}` | 原始数据 chip |
| `{{{links}}}` | 产出物链接 |
| `{{{footer}}}` | 页脚信息 |

## 组件 CSS 变量

`{{{base_css}}}` 中的组件使用以下变量，重新定义即可整体换肤：

```
--surface --surface2 --line --text --muted --faint
--accent --accent-fill --dot --success --warn --danger --target --grid
```

## 最小示例

可参考同目录的 `mybrand.html.example`（复制为 `.html` 即可启用）。
