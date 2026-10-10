# Agent-X Business Wiki MCP · v0.1

独立业务知识服务：原文 RAG + AI 编译的跨文档 Wiki + 子系统/特性路由。
不改 Agent-X harness，不复制 CodeGraph；Wiki 引导检索，CodeGraph 验证当前实现。

## 安装与启动

Python 3.12+，使用独立虚拟环境（Agent-X 使用 MCP v2，本服务固定 SDK v1）。

```bash
cd services/business-wiki-mcp
python3.12 -m venv .venv
.venv/bin/pip install .
.venv/bin/business-wiki-mcp --db /absolute/path/business-wiki.sqlite3
# 导入/维护用可写服务；日常问答建议用默认只读服务
.venv/bin/business-wiki-mcp --db /absolute/path/business-wiki.sqlite3 --writable
# HTTP MCP，地址 http://127.0.0.1:8765/mcp
.venv/bin/business-wiki-mcp --db /absolute/path/business-wiki.sqlite3 --transport streamable-http
```

服务仅允许 loopback。远程使用需部署带鉴权/TLS 的网关，并隔离读写端点；
本版本不含账号/ACL，不应直接公网暴露。发布代码或启动本地服务不等于已部署远程服务。
不需要 Docker。数据库路径由 `--db` 或 `WIKI_DB_PATH` 指定，备份使用 SQLite backup API。

## Agent-X 接入

将 `agent-x.example.yaml` 的 servers 项合并到自己的 MCP 配置，替换绝对路径。
将 `skill.md` 安装为业务问答 skill，按项目约定绑定 `business_knowledge` 工具组。
MCP 配置路径的现有用法见 Agent-X 主 README。
问答用只读进程，导入用单独可写进程，同一个数据库；不要把写工具标成 read_only。

## 工具

| 工具 | 模式 | 作用 |
|---|---|---|
| knowledge_search | 只读 | 原文/Wiki BM25 检索，返回原始片段、标签、版本 |
| knowledge_read | 只读 | 读取完整原文或 Wiki，Wiki 带 stale 标记 |
| wiki_lint | 只读 | 过期引用、断链检查 |
| source_ingest | 可写 | 上传解析后的文本和标签，可选 AI 分析 |
| wiki_compile | 可写 | 使用已有 Wiki 上下文生成修改草稿，可重试 |
| wiki_propose_update | 可写 | 外部 Agent 提交跨页面修改草稿 |
| wiki_apply_update | 可写 | 原文引用和版本校验后事务提交 |

## 原文入库及 AI 编译

本版接收 Markdown/纯文本；PDF、Word 先由调用端解析。服务器不接受任意本机文件路径。
代码可通过 CodeGraph 定位后，将必要的代码片段作为带 repo/commit/path 的原始证据入库，
不全量复制业务仓库。

可写服务启动前配置 OpenAI-compatible 模型（包括兼容接口的 DeepSeek）：

```bash
export WIKI_LLM_BASE_URL=http://your-internal-llm/v1
export WIKI_LLM_MODEL=your-model
export WIKI_LLM_API_KEY=your-key
```

模型须支持 JSON object response_format。调用示例：

```json
{"source_id":"WH/lifecycle/design","title":"WHSSD 初始化设计",
 "content":"初始化要求 VUR 就绪。失败时进入恢复状态。",
 "metadata":{"subsystems":["WH"],"features":["WHSSD初始化"],
 "related_subsystems":["SC"],"doc_type":"design","version":"v1",
 "repo":"business-WH","commit":"actual-commit","path":"docs/init.md"},
 "analyze":true}
```

`source_ingest` 返回 source revision，分析成功返回 pending proposal；失败返回
analysis.status=failed，原文已保存，可调用 wiki_compile 重试。结果重复入库不会重复建索引。
AI 获取同范围已有页面并生成融合修改，不自动写正式 Wiki。
检查草稿后调用 wiki_apply_update；apply 会拒绝来源版本变化、伪造引用和页面版本冲突。
引用文字匹配只能检查引用存在，不能证明模型推论正确，仍需业务审查。

## 查询与路由

```json
{"query":"WHSSD 初始化失败 recovery required", "subsystems":["WH","COMMON"],
 "features":["WHSSD初始化"],"kind":"all","limit":8}
```

固定工具 schema，通过参数传范围。标签是精确过滤：特性别名由 skill/调用端归一。
空结果先去掉 features，再扩展关联子系统/COMMON；不自动全仓盲搜。
原文与 Wiki 共用同一元数据结构，允许多子系统、多特性。
本版本查询是中文双字/英文标识符 BM25，非 embedding/混合检索；每项返回最佳分块。
语义召回、rerank、特性别名表、按 claim 的引用、自动代码变更检测留待下一版。

## 存储与版本

SQLite 是唯一持久化真源，避免 SQLite/Markdown 双写不一致。
保存最新原文快照、分块索引、Wiki 页面、草稿、应用审计。
原文被替换后，Wiki 的旧引用会被标记过期；本版不保留旧原文全文，
需要历史证据时应使用不可变 source_id（例如路径+commit）。审计保存每次应用的页面内容。
Wiki 正文为 Markdown，但没有磁盘目录/Obsidian 同步；可通过 knowledge_read 获取。
links 是页面 ID 列表，wiki_lint 检测断链，不自动修复或判断语义矛盾。

## 验证

```bash
.venv/bin/pip install pytest
.venv/bin/python -m pytest tests -q
.venv/bin/python -m pip install build
.venv/bin/python -m build
```

测试包含中文范围检索、重复入库、事务回滚、并发版本冲突、伪造引用、过期标记、
模拟模型融合、真实 stdio MCP initialize/list_tools/call_tool 入库至检索。
真实业务效果需提供文档和模型配置；测试中的模型是替身，不代表完成线上推理验证。

## 后续扩展

优先用一个子系统的实际资料与 20 个固定问题，比较原文 RAG 与 Wiki+原文的
答案证据准确率、工具次数、token、入库成本。之后加入 embedding/RRF/rerank、
异步入库任务、多租户 ACL、不可变来源版本、Markdown 导出和代码变更影响追踪。
