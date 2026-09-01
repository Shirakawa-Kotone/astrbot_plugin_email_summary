# 企业微信邮箱智能整理 - AstrBot 插件

通过 IMAP 协议访问企业微信邮箱，定时拉取邮件，**每封邮件调用 LLM 独立分析**（优先级/分类/摘要/行动项），自动生成汇总报告推送到 QQ。支持通过 QQ 命令查询邮件分析结果。

## 功能

- 📬 **IMAP 拉取**：连接企业微信邮箱（`imap.exmail.qq.com:993`）
- 🤖 **逐封 LLM 分析**：每封新邮件单独调用 LLM，提取优先级、分类、行动项、金额、截止日期等
- 📊 **汇总报告**：基于已分析结果，定时自动生成汇总报告并主动推送到 QQ
- 🎛️ **QQ 命令**：总结 / 邮件列表 / 邮件详情 / 手动扫描
- 🖥️ **WebUI 邮件看板**（AstrBot ≥ 4.24.1）：在 AstrBot 插件详情页打开「邮件看板」，网页上查看邮件列表 / 详情 / 汇总报告，一键触发扫描
- ⏰ **调度策略**：间隔模式（每 N 小时）或每日定时模式
- 💾 **持久化**：分析结果存于 `data/analysis/{uid}.json`，已处理邮件记录于 `data/state.json`
- 🐞 **失败可见**：LLM 分析失败会写入 AstrBot 日志（logger.error），并在列表/详情中标注失败原因（`analysis_error`）

## 安装

### 方式一：Web 控制台上传压缩包（推荐）

1. 先构建发布压缩包（在插件目录下执行）：

   ```bash
   python build_release.py
   ```

   生成 `dist/astrbot_plugin_email_summary.zip`，脚本会自动校验压缩包是否符合
   AstrBot 的识别规则（含 metadata.yaml、单一顶层目录、插件名合法等）。

2. 在 AstrBot Web 控制台「插件管理 → 安装插件 → 上传压缩包」，选择
   `dist/astrbot_plugin_email_summary.zip` 上传即可。

3. 填写插件配置：
   - `email_address`：企业邮箱地址
   - `email_auth_code`：企业微信邮箱授权码（邮箱设置 → 客户端设置 → 生成授权码）
   - `llm_api_key`：OpenAI 兼容 API Key
   - 其余使用默认值即可

### 方式二：手动复制目录

将本目录（`astrbot_plugin_email_summary`）复制到 AstrBot 的 `data/plugins/` 目录下，
然后在 AstrBot Web 控制台「插件管理」中启用本插件。

### ⚠️ 打包注意事项（安装报错的常见原因）

- **不要**用 macOS 访达的「压缩」直接打包：它会在 zip 里写入 `__MACOSX/`
  目录，导致 AstrBot 报「压缩包不是合法的 AstrBot 插件：未找到 metadata.yaml
  或 metadata.yml」。请始终使用 `python build_release.py` 打包。
- 插件名 `name` 必须是合法 Python 标识符（只能用字母、数字、下划线，不能有连字符），
  否则 AstrBot 加载时会报「name 不是合法的模块名称」。当前为 `email_summary_assistant`。
- 压缩包应为**单一顶层目录**（`astrbot_plugin_email_summary/`），其下直接包含
  `metadata.yaml`、`main.py`、`core/` 等文件。
- 可随时用 `python build_release.py 任意.zip` 校验任意压缩包是否符合规则。

## WebUI 邮件看板（AstrBot ≥ 4.24.1）

插件内置一个 WebUI 页面「邮件看板」（目录 `pages/dashboard/`）。安装插件后：

1. 打开 AstrBot Web 控制台 →「插件管理」→ 点击本插件进入详情页
2. 在插件 Pages 区域打开「邮件看板」

页面功能：

- **邮件列表**：最新在前，展示优先级图标、标题、发件人、日期、摘要；LLM 分析失败的邮件会标红并显示失败原因
- **邮件详情**：点击任意邮件查看完整分析（要点 / 行动项 / 金额 / 链接 / 标签等）
- **生成汇总**：调用已配置的 LLM 生成汇总报告（需先配置 `llm_api_key`）
- **触发扫描**：网页上直接触发一次拉取 + 逐封分析（后台执行）

> 该页面依赖 AstrBot v4.24.1+ 的「插件 Pages」功能；更早版本仍可通过 QQ 命令使用全部功能。

## QQ 命令

| 命令 | 说明 |
|------|------|
| `/总结` | 生成最新汇总报告 |
| `/邮件列表` | 查看已分析邮件列表 |
| `/邮件 <编号>` | 查看指定编号邮件的分析详情 |
| `/扫描` | 立即触发一次拉取+逐封分析（不推送） |
| `/帮助` | 显示可用命令 |

## 工作流

```
定时触发 (间隔/每日定时)
  → IMAP 拉取新邮件
  → 对每封邮件调用 LLM 独立分析 (结果存 data/analysis/{uid}.json)
  → 更新已处理状态 (data/state.json)
  → 生成汇总报告 (SummaryReporter, 调用 LLM 一次)
  → 主动推送到目标 QQ

QQ 命令触发
  /总结     → 基于已存储分析生成报告并回复
  /邮件列表 → 列出已分析邮件
  /邮件 N   → 查看第 N 封邮件详情
  /扫描     → 手动执行一次完整扫描
```

## 配置项

| 配置 | 默认值 | 说明 |
|------|--------|------|
| `imap_host` | `imap.exmail.qq.com` | IMAP 服务器 |
| `imap_port` | `993` | IMAP 端口 |
| `llm_api_base` | `https://api.openai.com/v1` | OpenAI 兼容 API 地址 |
| `llm_model` | `gpt-4o-mini` | LLM 模型 |
| `summary_mode` | `balanced` | 总结模式: brief/balanced/detailed/ultra_detailed |
| `schedule_mode` | `interval` | 调度: interval/fixed |
| `interval_hours` | `2` | 间隔模式的小时数 |
| `fixed_time` | `09:00` | 定时模式的时间 |
| `max_scan_days` | `7` | 扫描最近 N 天 |
| `max_emails` | `50` | 每次最多处理邮件数 |
| `report_recent_n` | `30` | 汇总报告包含最近 N 封 |
| `push_enabled` | `true` | 扫描后自动推送汇总 |
| `target_qq` | 空 | 推送目标 QQ（留空推给命令请求者） |

## 注意

- 企业微信邮箱需要先在邮箱设置中生成 **授权码**（非登录密码）才能通过 IMAP 访问
- 每封邮件一次 LLM 调用 + 每次汇总一次 LLM 调用，注意 API 费用
- 若邮箱或 LLM 配置不完整，插件会跳过扫描/报告生成，并在日志和命令回复中给出提示
- 插件加载时不会检查 LLM API Key（客户端惰性创建），未配置 Key 也能正常加载，仅在执行分析/汇总时报错提示
- LLM 分析失败（网络不通、Key 无效、模型名错误等）时：错误会写入日志面板（`logger.error`），
  该邮件保存为低优先级兜底结果并带有 `analysis_error` 字段，在 `/邮件列表`、`/邮件 <编号>` 和
  WebUI 看板中均可看到具体失败原因，便于排查 `llm_api_key / llm_api_base / llm_model` 配置
