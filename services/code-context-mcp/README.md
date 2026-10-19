# Project Knowledge / Code Context MCP · v0.1

通用研发项目接入：Wiki MCP 管设计资料与项目配置，Code Context MCP 管代码快照和 CodeGraph 索引；
Agent-X 的 `project-init` 与 `project-qa` Skill 负责引导初始化和联合查证。复用现有 Backend 和 Web UI；Harness 增加默认关闭的维护服务白名单。

代码服务使用真实 `@colbymchenry/codegraph@1.6.2` SDK，不运行仓库代码、不安装项目依赖、不使用正则检索冒充代码图。

## 安装

需要 Linux/POSIX、Python 3.12+、Git、Node.js 24。两个服务使用 MCP SDK v1，必须与 Agent-X Harness 的 SDK v2 虚拟环境分开。
以下假定框架在 `/srv/Agent-X`，企业资料在 `/srv/imports`，持久数据在 `/srv/knowledge`；按部署修改路径。

```bash
cd /srv/Agent-X
python3.12 -m venv /srv/knowledge/.venv
/srv/knowledge/.venv/bin/pip install -e services/business-wiki-mcp -e services/code-context-mcp
npm ci --prefix services/code-context-mcp
cp services/code-context-mcp/roots.example.json /srv/knowledge/roots.json
```

roots.json 由管理员维护，例如 `{"source":"/srv/imports"}`。工具只接受 root ID 与相对路径，
不会接收任意模型指定的宿主绝对路径或远程地址。数据目录必须在资料目录之外，防止把索引再次复制进快照。

以上使用 editable 安装，让 `bridge.mjs` 从本服务的 node_modules 解析 SDK。
如安装 Python wheel，另用 npm 安装 SDK 并在服务进程中配置 `CODEGRAPH_MODULE` 为已安装包的绝对路径；
可以用 `CODEGRAPH_NODE` 指定 Node 24 可执行文件。不要在项目配置中放这两个运维设置。

## 接入 Agent-X

把 [agent-x.example.yaml](agent-x.example.yaml) 的四个条目合并到自己的 `mcp_servers.yaml`，修改绝对路径和项目白名单。
这些是两个逻辑服务各自的只读/维护入口，不是四套业务系统。读模式不暴露建索引、入库或修改配置的工具。

现有 Harness 默认禁止写工具。初始化管理员部署需在 Harness 环境中显式设置：

```dotenv
MCP_MAINTENANCE_SERVERS=project_wiki_writer,project_code_writer
```

仅放行这两个 source 的 workspace_write 工具，其他写工具、command_exec、external_side_effect 和显式 requires_approval
仍遵循原策略；模型不能修改白名单。普通用户部署保持为空，仅提供 project-qa。


项目本地 Skill 已包含在仓库：

- `skills/project-init/SKILL.md`：引导资料发现、提问、保存草稿、确认配置、构建与恢复。
- `skills/project-qa/SKILL.md`：确定项目基线、按架构路由，交替检索文档与代码，输出证据和差异。

在现有 Web UI 选择 `project-init`，例如：

> 初始化 demo 项目，基线 release-1。代码位于 source 资料根的 Agent-X 目录。
> 请先发现仓库和文档，再和我确认组件划分；不确定适用版本的文档先标为 unknown。

项目白名单来自服务的 `--project` 参数，不由模型修改。示例只允许 demo；新增项目时管理员修改部署配置。
未指定白名单表示该本地服务可管理多个项目，不表示具备企业用户级授权。维护工具应只提供给受信管理员。

Wiki 自动编译是可选能力。默认入库/检索不需要 LLM Key；需要 AI 编译时才给 Wiki writer 添加
`env_from` 映射 `WIKI_LLM_BASE_URL`、`WIKI_LLM_API_KEY`、`WIKI_LLM_MODEL`。

## 初始化实际做了什么

1. `project_init` 创建/恢复项目基线草稿，返回 revision 和缺失问题。
2. `code_discover` 有界扫描受控目录、构建清单、文档预览和 Git HEAD。目录只是候选组件。
3. Agent 提问并用 `project_propose` 保存结构化字段；`expected_revision` 防止覆盖并发修改。数组整体替换。
4. `project_apply` 确认准确草稿版本。确认后配置冻结；新增版本/结构变化使用新的 baseline。
5. `code_build` 接收 project_get 返回的完整 config，持久化 job 并启动独立 worker；MCP 连接关闭不会中断构建。
6. `code_status` 返回每个仓库的真实进度、错误、版本、图统计和能力覆盖。
7. `source_ingest` 将已登记文档的解析文本写入相同项目/基线，原文和 Wiki 保留可按 revision 读取的快照。
8. `project-qa` 使用配置、别名和特性入口联合查询；核对两侧 config_hash，引用具体版本的证据。

配置 confirmed 不是索引 ready。部分失败返回 partial，只查询成功仓库；所有仓库失败才是 failed。
failed/interrupted 可用相同 config + retry=true 重试。partial 的失败范围在新基线补齐，避免重建时破坏已发布证据。
查询不跟随可变工作树自动更新；变更应建立新基线。单机构建串行，避免多个大型索引竞争资源。

## 通用项目配置

固定语义为项目、基线、组件、仓库、特性、文档和关系。组件 kind 可为 subsystem/service/module/library/driver 等，
不强制分层或仓库与组件一一对应。`part_of` 的已确认关系禁止成环，`depends_on` 允许环。

`project_propose` 的 patch 示例（项目与基线已由 project_init 建立）：

```json
{
  "components": [
    {"id":"control","name":"控制模块","kind":"subsystem","aliases":["Control"]}
  ],
  "repositories": [
    {"id":"app","root_id":"source","relative_path":"project-a","revision":"snapshot",
     "mappings":[{"component_id":"control","path":"src"}]}
  ],
  "documents": [
    {"source_id":"startup-design","title":"启动设计","component_ids":["control"],"version_status":"unknown"}
  ],
  "features": [
    {"id":"startup","name":"启动","aliases":["startup"],"component_ids":["control"],
     "document_ids":["startup-design"],
     "code_anchors":[{"repo_id":"app","path":"src/start.py","symbol":"initialize"}]}
  ],
  "relations": []
}
```

Git 仓库优先使用发现得到的完整 commit：通过 git archive 固定已提交文件，不含工作树未提交修改。
无 Git 用 `revision: snapshot`，构建时复制文本/代码资料并返回 `snapshot:<内容清单摘要>`。
跳过符号链接和常见产物目录；超过文件数、单文件或总体积上限明确失败，不静默声称全量覆盖。
子模块内容不会被 git archive 自动展开，需作为独立仓库登记。Git LFS 对象也需管理员事先准备可解析的代码快照。

`project_route` 用别名/特性返回组件、仓库、文档及代码入口；不做 embedding，也不假装目录发现已完成业务架构理解。
AI 根据资料与用户确认提出关联，已确认关系必须记录 provenance。映射遗漏时可以沿证据在同一授权基线内扩展。

## 工具

| 代码工具 | 权限 | 作用 |
|---|---|---|
| code_discover | 维护 | 有界发现，只在登记的资料根内读取 |
| code_build | 维护 | 提交幂等持久任务，返回 job_id |
| code_status | 只读 | 构建状态、版本和逐仓库错误 |
| code_search | 只读 | CodeGraph 符号与源码，支持组件映射过滤 |
| code_read | 只读 | 固定快照内按行读取，限制路径与输出大小 |
| code_relations | 只读 | 同仓静态 callers/callees，限制深度和结果数 |

所有查询带 project_id/baseline_id；code_search 另带 repo_id。schema 固定，通过参数路由多个仓库。
Wiki 启用 `--projects-root` 后同样强制显式 scope，不回退 legacy DB。直接读取、草稿、引用解析、编译也按物理数据库隔离。
`--db` 仍支持旧单库模式，但不会在新项目模式下隐式读取旧库。旧库可备份后用受控脚本显式重新入库，未实现自动迁移。

## 跑一个真实仓库

在仓库根目录运行；示例分析 Agent-X 当前提交，代码目录和数据目录必须分开：

```bash
/srv/knowledge/.venv/bin/python services/code-context-mcp/scripts/demo.py \
  --source /srv/Agent-X \
  --data /srv/knowledge/demo-agent-x \
  --query prune_expired_workspaces \
  --doc-query '工作区 清理' \
  --feature '工作区保留策略'
```

脚本通过两个真实 stdio MCP 服务完成登记、确认、构建、文档入库、特性路由、源码与静态调用查询，
并输出 report.md/evidence.json。重复运行复用已确认配置和任务；想分析新版本请指定新的 `--baseline`。
换任意本地仓库只需修改 `--source`、`--query`、`--doc-query` 和可选 `--document`。
演示采用中性“仓库组件”而非猜测架构；交互式 AI 引导由 Agent-X Skill 承担，脚本本身不调用模型。

[本次 Agent-X 真实运行报告](../../docs/examples/knowledge-demo-agent-x.md) 包含固定 commit、图统计、文档片段和源码位置。
文档示例是 README 功能说明，不是伪造的正式设计文档；企业接入时替换为真实设计资料。

## 测试

在独立 service venv 安装 pytest：

```bash
/srv/knowledge/.venv/bin/pip install pytest
/srv/knowledge/.venv/bin/python -m pytest -c /dev/null services/business-wiki-mcp/tests services/code-context-mcp/tests -q
CODEGRAPH_INTEGRATION=1 /srv/knowledge/.venv/bin/python -m pytest -c /dev/null services/code-context-mcp/tests/test_integration.py -q
```

默认测试无需 Node/CodeGraph；opt-in 测试使用真实 SDK 和 stdio MCP，覆盖 Python、JavaScript 两种组织方式、
连接关闭后继续构建、实际运行时缺失后的重试、项目隔离、组件范围、快照读取及静态调用边。
无需模型凭据；未用真实 LLM 验证初始化对话质量，也未用大规模 C++ 企业项目验证索引覆盖。

## 部署边界

v0.1 面向企业独立部署试点；HTTP 只绑定 loopback。远程需要受信鉴权/TLS 网关，生产多用户需要另行对接身份与 ACL。
项目 ID/Skill 选择不是权限凭据。原文、配置和索引存放在服务持久目录，不会随 Agent-X 会话工作区的过期清理而删除。

当前没有跨仓调用图、自动代码变更失效追踪、分布式构建、PDF/Word 解析或权限同步。
静态图覆盖取决于上游语言解析与动态调用能力，不能据此证明运行轨迹；设计不一致也不能直接认定代码有 Bug。
