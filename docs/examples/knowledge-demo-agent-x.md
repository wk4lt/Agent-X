# 真实仓库联合检索验证

执行日期：2026-10-19（UTC）

- 项目 / 基线：`demo` / `demo-v1`
- 代码版本：`4e09b0e82c00b9f882379c9f89571f00745e1f7d`
- 索引引擎：`codegraph-1.6.2-adapter-1`
- 构建状态：`ready`
- 索引文件 / 节点 / 边：53 / 1035 / 2677
- 配置摘要：`15198962e69fb7176685d6fa244c3eb7881a47cec0eeace34b5c143f5807e183`

本报告由真实 stdio MCP、真实 CodeGraph SDK 与 Wiki 原文检索生成。没有调用语言模型；
用于核验初始化工具链、基线和证据联动，AI 引导对话由 project-init Skill 提供。

## 文档证据

查询：`工作区 清理`

来源：`readme`，revision：`86659c9d49169d45329eb9f2888f430187621b01acc72b60271e3fc5a3d144d0`

````text
尝试 3 次，并对连接错误、超时、`408/429/5xx`，以及明确返回
“model does not exist”的 `404` 使用指数退避。重试只会发生在尚未向客户端输出任何流式内容时，
避免产生重复文本或重复 Tool Call。可通过 `LLM_MAX_ATTEMPTS`、
`LLM_RETRY_BASE_DELAY_SECONDS` 和 `LLM_RETRY_MAX_DELAY_SECONDS` 调整。

## 测试

```bash
python -m pytest
```

测试使用脚本化的假 Provider 与 Tool，不需要模型或 MCP 凭据。

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


````

## 代码证据

查询：`prune_expired_workspaces`

`backend/workspace_cleanup.py:26-33` — `prune_expired_workspaces`

````text
async def prune_expired_workspaces(history, root: Path, retention_days: int,
                                   *, now: datetime | None = None) -> list[str]:
    """Delete workspace files for conversations inactive beyond the configured retention period."""
    if retention_days == 0:
        return []
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=retention_days)
    conversation_ids = await history.expired_workspace_conversation_ids(cutoff)
    return await asyncio.to_thread(remove_workspace_directories, root, conversation_ids)
````

`backend/app.py:19-19` — `backend.workspace_cleanup`

````text
from backend.workspace_cleanup import prune_expired_workspaces
````

`tests/test_history.py:12-12` — `backend.workspace_cleanup`

````text
from backend.workspace_cleanup import prune_expired_workspaces
````

## 静态调用关系

- `tests/test_history.py:117` — `test_workspace_retention_removes_only_expired_conversation_directories`
- `backend/app.py:43` — `cleanup_once`
- `backend/app.py:1` — `app.py`
- `tests/test_history.py:1` — `test_history.py`

边界：此版本没有跨仓调用图和运行轨迹分析。架构示例仅登记一个仓库组件，没有把目录自动确认为业务架构。
查询使用固定快照；文档入库与代码查询采用同一项目/基线，config_hash 已核对。
