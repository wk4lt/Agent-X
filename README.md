# Agent-X

Agent-X 是一个面向代码、文档与复杂任务的可观察 Agent Runtime。它将可流式交互的 Web UI、
任务后端与单 Agent Harness 分离，形成从用户输入、工具调用到运行事件的完整闭环。

当前 V1 已包含版本化契约、单 Agent 执行核心、会话历史、项目本地 Skills，以及 React
Web UI。它基于 `Codex_Agent_Harness_Development_Guide.md` 从零构建，不复用旧实现。

## 特性

- **流式运行**：Harness 产生标准化事件，Backend 通过 SSE 持续推送到 Web UI。
- **可追踪上下文**：记录上下文预算、压缩状态、实际 Token 使用量、工具执行和终止原因。
- **持久会话**：匿名浏览器身份、会话、消息和 Task/Run 映射保存在 SQLite 中。
- **Python 调用**：Bearer API Key 与轻量客户端复用同一套 Task API，无需绕过 Backend 直连 Harness。
- **独立工作区**：每个会话有独立文件工作区；上传文件不会被自动放入模型上下文。
- **Skills**：发现项目本地 `SKILL.md`，按需加载，并对脚本执行施加显式策略、超时和输出限制。
- **MCP Host**：按 Skill 动态接入外部 MCP Server，并把远端工具统一映射到现有 Tool 执行与审计链路。
- **可替换模型**：支持 OpenAI 兼容接口；本地未配置凭据时使用安全且确定性的 EchoProvider。

## 架构

```mermaid
flowchart LR
    UI["Web UI"] --> BFF["Backend / BFF"]
    BFF --> H["Agent-X Harness"]
    H --> LLM["LLM Provider"]
    H --> Tools["Local Tools & Skills"]
    H --> MCP["MCP Servers"]
```

| 模块 | 职责 |
| --- | --- |
| `contracts/` | 版本化的 Task、Run、Event 与 Tool 契约。 |
| `harness/` | Session/Run 生命周期、上下文构造、模型与工具端口、标准事件写入。 |
| `backend/` | 会话归属、历史持久化、文件工作区与对 Web UI 的 SSE/API。 |
| `web/` | Vite + React 前端，只消费公开 API 和事件信封。 |

V1 中 Harness 运行状态保留在进程内；浏览器会话历史、归属、消息和 Harness Run 映射则通过
SQLAlchemy Async 与 Alembic 持久化到 SQLite。

## 本地启动

需要 Python 3.12 和 Node.js：

```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
uvicorn harness.app:app --port 8001
# separate terminal
HARNESS_URL=http://127.0.0.1:8001 uvicorn backend.app:app --port 8000
# separate terminal
cd web && npm install && npm run dev
```

首次安装好 Python 与 Web 依赖后，也可以一次启动三个服务：

```bash
./dev.sh          # 启动；已运行时显示状态和重启提示
./dev.sh restart  # 停止当前项目的旧服务，再以前台模式启动
./dev.sh stop     # 从另一个终端停止服务
./dev.sh status   # 查看各服务的状态、端口和进程组
```

`dev.sh` 面向 Linux（包括 WSL），会启动 Harness（`8001`）、Backend（`8000`）和 Web UI（`5173`），
日志写入 `LOG_ROOT/services/`；按 `Ctrl+C` 会停止全部子进程。Harness、Backend 默认启用
Python 热更新，各自只监听本模块和共享 `contracts/`；前端继续使用 Vite 热更新。
如不需要 Python 热更新，使用 `DEV_RELOAD=0 ./dev.sh`。Harness 重载会清空进程内的运行状态，
正在执行的任务会中断；持久化的会话历史仍保留。

监听地址、端口和热更新开关可以写入项目根目录的 `.env`：

```dotenv
AGENT_X_HOST=0.0.0.0
HARNESS_PORT=8001
BACKEND_PORT=8000
WEB_PORT=5173
DEV_RELOAD=1
```

监听地址使用项目专属的 `AGENT_X_HOST`，避免与服务器用于表示机器名的通用 `HOST` 冲突。
所有配置均按 shell 环境变量、`.env`、内置默认值的顺序取值。Web UI 的 `/api` 代理会跟随
`BACKEND_PORT`。例如：

```bash
AGENT_X_HOST=0.0.0.0 HARNESS_PORT=8101 BACKEND_PORT=8100 WEB_PORT=5273 ./dev.sh restart
```

进程记录固定保存在项目的 `.run/dev/`，因此改变日志路径或端口后也能停止旧服务。
启停操作使用文件锁串行执行，停止时校验项目、系统启动标识、进程启动时间与会话标记，
先正常终止进程组，超时后再强制清理已确认属于本次会话的子进程。
端口被其他程序或旧版脚本启动的未登记服务占用时，会报错并提示检查占用情况，不会自动杀掉它们。
首次切换新版脚本前，请先在旧脚本终端按 `Ctrl+C` 停止原服务。

访问 Vite 输出的地址即可使用 Agent-X。复制 `.env.example` 为 `.env` 后填入模型凭据；
没有凭据时 Harness 使用 `EchoProvider` 返回安全、确定性的结果。启动时会校验模型设置、HTTPS
基础地址、上下文窗口、输出预留、工具结果上限、超时和并发配置；密钥不会写入日志。

OpenAI 兼容 Provider 默认最多尝试 3 次，并对连接错误、超时、`404/408/429/500/502/503/504`
使用指数退避，覆盖内部网关偶发的空白或路由 `404`；持续的错误配置会在尝试耗尽后失败。
重试只会发生在尚未向客户端输出任何流式内容时，
避免产生重复文本或重复 Tool Call。可通过 `LLM_MAX_ATTEMPTS`、
`LLM_RETRY_BASE_DELAY_SECONDS` 和 `LLM_RETRY_MAX_DELAY_SECONDS` 调整。

## 测试

```bash
python -m pytest
```

测试使用假 Provider、Tool 和本地 MCP 子进程，不需要模型或 MCP 凭据；真实 stdio 测试覆盖
连接复用、子进程被杀后的重建、取消后的清理和关闭时的进程回收。

## 会话工作区

每个会话都有独立本地工作区：`.data/workspaces/<conversation_id>/`；同一会话内的多个 Task/Run
复用该工作区。UI 默认支持上传、列出和下载最大 25 MiB 的文件；可在 `.env` 配置
`WORKSPACE_ROOT` 与 `WORKSPACE_MAX_FILE_BYTES`。工作区默认保留 30 天，以会话最后更新时间为准；
可通过 `WORKSPACE_RETENTION_DAYS` 调整，设为 `0` 可关闭自动清理。后台按
`WORKSPACE_CLEANUP_INTERVAL_SECONDS`（默认 3600 秒）在启动时及后续周期中清理已过期会话的文件。
清理不会删除会话历史记录；再次打开旧会话时只会得到一个新的空工作区。
每个 Run 会把该工作区的绝对路径作为可信运行时元数据注入模型上下文；Agent 与 Skill 必须把它
作为所有用户文件读写和生成产物的基准目录。Skill 脚本也会以此目录为 `cwd`，并通过
`AGENT_X_WORKSPACE` 获得同一路径。工作区文件内容不会自动进入模型上下文，仍由 Agent 按需读取。

运行诊断分别以 JSONL 写入 `LOG_ROOT/<session_id>/`，服务标准输出写入
`LOG_ROOT/services/`；两个目录均可配置。
会话页面顶部显示可复制的 Session ID；展开“日志定位”可查看和复制会话记录 ID、Run ID、Task ID
以及日志目录。尚未提交任务时显示会话记录 ID；历史任务即使在 Harness 重启后也保留这些定位信息。

## 持久化会话历史

Backend 在首次访问时创建随机匿名浏览器身份，只把持久化 HttpOnly Cookie 的 SHA-256 哈希写入
SQLite。会话/消息查询、任务控制、SSE 与工作区访问都会在服务端按该身份限制；归属不依赖 IP
地址或浏览器本地存储。Cookie 与 SQLite 路径可通过环境变量配置：

```bash
HISTORY_DB_PATH=.data/agent_history.db
HISTORY_BUSY_TIMEOUT_MS=5000
ANON_COOKIE_SECURE=false # HTTPS 部署时设为 true
```

启动时会创建数据库父目录；SQLite 启用外键、WAL 与配置的忙等待超时，Backend 启动阶段自动执行
Alembic 升级。迁移版本位于 `backend/migrations/`，也可以手动执行
`python -m backend.migrate`。SQLite 适用于单个 Backend 实例；多实例或高并发写入时，应保留
同一 Repository/API 契约并切换至 PostgreSQL。

同一 Conversation 的后续 Task 会复用同一个 Harness Session，因此追问会继续携带此前的用户消息、
回答和结构化工具调用历史；新建 Conversation 才会创建新的 Session。

## Python 脚本调用

Backend 的公开 Task API 同时供 Web UI 和 Python Client 使用。先生成一个至少 32 字符的随机 Key，
并把它配置给 Backend：

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
export AGENT_API_KEYS='将上一步生成的随机值填到这里'
HARNESS_URL=http://127.0.0.1:8001 uvicorn backend.app:app --port 8000
```

需要隔离多个脚本用户时，用逗号配置多个 Key。每个 Key 映射为独立且稳定的 principal；原始 Key
不会写入 SQLite。删除或轮换 Key 后，旧 Key 所属历史仍保留，但无法再通过新 Key 访问。

安装本项目后，脚本可以直接阻塞等待 Agent 最终回答：

```python
from agent_x import AgentXClient

with AgentXClient("http://127.0.0.1:8000", api_key="你的 API Key") as client:
    answer = client.ask(
        "ISPPowerOnSensorT 如何调用设备驱动？",
        selected_skills=["code-doc"],
        timeout=300,
    )
    print(answer)
```

`ask()` 内部只组合现有的 `POST /api/tasks` 和 `GET /api/tasks/{task_id}`。如需自行控制生命周期，
可分别调用 `submit()`、`get_task()`、`wait()` 和 `cancel()`；`submit()` 默认生成
`Idempotency-Key`，网络重试时也可以显式复用同一个值。实时事件仍可通过现有
`GET /api/tasks/{task_id}/events` SSE 接口获取。

## Skills

Harness 在每次 Run 开始时，只从项目根目录的 `skills/<skill-name>/SKILL.md` 发现 Skill。
Skill 的目录名必须与 frontmatter 中的 `name` 一致。模型在调用 `load_skill` 前只能看到名称和描述，
加载说明不会执行脚本。

每次请求必须通过 `selected_skills` 指定 Skill；未选择、选择失效或 Skill 被禁用时，
直接返回提示，不调用模型或工具。系统提示词在每轮注入本次选择的名称和描述，要求模型
只回答已选 Skill 范围内的问题，拒绝无关问题；混合请求只处理相关部分。加载和脚本工具
也只能访问本次已选 Skill。Web UI 发送后保留已选标签，连续追问时可继续使用或手动更换。

固定的范围和拒答规则统一维护在 `ContextBuilder.system_rules`；执行器仅在 Run 副本中
追加本次选择的 Skill 元数据，不重复定义固定规则。明显无关的问题直接拒答，无需加载 Skill。

`run_skill_script` 是独立的、仅接收 argv 的工具。它只接受已发现的 Skill 名，以及最终解析到该
Skill 根目录内的脚本路径；允许相对路径、目录内可安全归一化的 `..` 和根目录内绝对路径，但拒绝
任何最终逃出 Skill 根目录的路径或符号链接。默认只允许 `.py` 和 `.sh`，使用受限环境并施加超时、
输出与并发限制。项目 Skill
脚本在当前会话工作区执行，并收到 `AGENT_X_WORKSPACE` 环境变量；默认遵循
`SKILL_SCRIPT_POLICY=ask`，审阅脚本后才能在可信部署中显式改为 `trusted` 或 `allow`。

## MCP 工具接入

Agent-X 当前作为 MCP Host，支持 `stdio` 与 Streamable HTTP 两种客户端传输。它在 Run 开始时根据
用户选中的 Skill 收集 `tool_groups`，只发现并挂载相应 MCP Server 的工具；远端工具以
`mcp__<server>__<tool>` 命名，继续复用 `ToolPolicy`、并发限制、超时、结果截断和运行事件。

先复制示例配置：

```bash
cp mcp_servers.example.yaml mcp_servers.yaml
```

在需要 MCP 工具的 Skill frontmatter 中声明工具组：

```yaml
---
name: code-doc
description: Search code and documentation.
tool_groups: [code_doc]
---
```

然后在 `mcp_servers.yaml` 中让一个或多个 Server 加入同名工具组。完整的 stdio、Streamable HTTP、
工具白名单、风险级别和环境变量引用示例见 `mcp_servers.example.yaml`。每个 Server 必须显式声明
`risk`；未知字段会让 Harness 启动失败。凭据不要直接写入 YAML：
`env_from` 与 `headers_from_env` 的值是 Agent-X 进程中的环境变量名。例如 HTTP Bearer 值可设置为：

```bash
export KNOWLEDGE_MCP_AUTHORIZATION='Bearer ...'
```

工具 schema 缓存在进程内，默认 300 秒。每个 Harness worker 按 Server 名懒创建一条长驻连接，
工具发现与实际调用共用该连接；所有 Run 对同一 Server 的调用串行，不同 Server 可并行。
建议 `max_concurrency: 1`；更大的值不会突破连接层串行限制。Server 应通过工具参数显式区分项目、
版本等请求范围；需要独立会话状态的服务应使用不同 Server 配置名。

连接中断时释放旧连接；仅 `idempotent: true` 的工具自动重连并重试一次，工具业务错误不自动重放。
超时或取消会清理正在使用的连接，下一次调用再重建；取消尚在排队的请求不会打断其他 Run 的调用。
Harness 退出时先停止运行中的任务，再异步关闭所有 MCP 连接。`agent_x.mcp` 日志包含建连耗时、
锁排队耗时、调用耗时和重连原因类型，不记录工具参数或凭据。毫秒级热调用应通过实际工具测量，
不代表消除了 MCP 服务内部的额外进程启动或检索开销。

配置的 Server 清单可通过 Harness 的 `GET /internal/mcp/servers` 查看；该接口不返回命令
参数、URL、Header 或环境变量值。未选择声明了 `tool_groups` 的 Skill 时，不会向模型暴露 MCP 工具。

## 业务知识 Wiki MCP

独立的原文 RAG 与 AI Wiki 服务，支持子系统/特性过滤、来源引用和版本校验。
安装、模型配置和 MCP 接入见 [Business Wiki MCP](services/business-wiki-mcp/README.md)。

## 通用研发项目知识底座

Wiki 与 CodeGraph 通过项目、基线、组件和特性关联。选择 `project-init` Skill 可引导发现资料、确认架构、
保存可恢复草稿并构建索引；`project-qa` 在同一版本范围内联合检索设计文档与代码，返回可追溯证据。
代码侧提供独立 Code Context MCP，使用固定快照与真实 CodeGraph SDK，构建任务可跨 MCP 连接继续运行。

安装和完整示例见 [Code Context MCP](services/code-context-mcp/README.md)，
实际效果见 [Agent-X 仓库验证报告](docs/examples/knowledge-demo-agent-x.md)。
