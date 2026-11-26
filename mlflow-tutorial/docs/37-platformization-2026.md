# 第 37 章 MLflow 3.9–3.18 平台化：工作区、RBAC、MCP 与 AI 治理（2026）

> 版本说明：本章基于 2026-11 的公开 release notes、官方文档与 changelog 整理。MLflow 3.18.0 是写作时的当前版本线；具体字段、API 与默认值以你锁定的版本文档为准。本章讨论的是真实平台能力，MiniMLflow 不实现它们，只给出映射与最小改造清单。

## 学习目标

- 说清 MLflow 3.9 之后为什么从“GenAI 功能点”转向“平台治理”。
- 画出 3.10 工作区、3.13/3.17 RBAC、3.9/3.11/3.12 Gateway 三者的关系。
- 理解 MCP Registry 的数据模型、版本语义与供应链风险。
- 把评测、人工反馈、Review Queue、pytest 回归串成一条可运营的闭环。
- 评估 3.18 的数据库升级、安全开关与运维成本，而不是只看新功能。
- 把平台能力映射到 MiniMLflow，知道补哪几张表、哪几个中间件。

## 37.1 为什么把 3.9–3.18 单独成章

MLflow 3.0（2025-06）完成了数据模型的重心转移：模型成为 LoggedModel，Registry 走向 alias，Tracing/Prompt/Evaluation 进入主路径。3.1 到 3.8 主要是在这条主路径上补细节：会话级评测、评分器生态、Prompt Registry、Trace UI。

3.9 之后，问题变了。用户不再问“能不能记录一次 LLM 调用”，而是问：

- 一个 tracking server 能不能同时服务多个团队，且互相看不见实验和 prompt？
- 谁能读 trace、谁能改 prompt 别名、谁能调用某个 Gateway endpoint？
- 评测结果能不能自动发现问题、分配到人、形成可追溯的人工反馈？
- Agent 依赖的 MCP server 由谁注册、哪个版本在 production、工具清单变没变？
- 网关的预算、guardrail、密钥和审计在哪里统一管理？

这些问题不是“再加一个 API”，而是**平台治理**。它带来新的实体（workspace、role、grant、MCP server、review queue、budget policy）、新的失败模式（越权、预算击穿、供应链投毒、数据库迁移失败）和新的运维成本。

```text
3.0–3.8   GenAI 原语：LoggedModel / Trace / Prompt / Eval
3.9–3.12  运行时与体验：Assistant / Dashboard / Gateway 内嵌 / Guardrail
3.13–3.17 治理：RBAC / 子资源权限 / MCP / Review Queue / Trace 视图
3.18      运营硬化：自动问题检测 / Skill Registry / 数据库升级 / 安全开关
```

这张时间线解释了一个常见误判：**开源 MLflow 不是“只有实验追踪”**。到 3.18，它已经包含一套可自托管的多工作区、RBAC、网关预算与 MCP 资产注册能力。是否好用、是否适合你的规模是另一个问题，但“能力不存在”已经不符合事实。

## 37.2 版本年表（3.9 → 3.18）

| 版本 | 时间 | 头条能力 | 对平台的含义 |
|---|---|---|---|
| 3.9 | 2026-01 | MLflow Assistant、Trace Overview Dashboard、Judge Builder 与 Judge 优化、Gateway 内嵌 tracking server | GenAI 可观测性从“能看单条 trace”走向“能看整体质量与成本” |
| 3.10 | 2026-02 | 多工作区、多轮评测与对话模拟、Trace 成本、`mlflow demo` | 一个 server 开始服务多个逻辑租户 |
| 3.11 | 2026-04 | 自动问题检测、Gateway 预算管理、无 pickle 路径扩展、原生 OTel GenAI 语义约定 | 成本与供应链成为一等约束；sklearn/LightGBM 的默认切换在 3.14 |
| 3.12 | 2026-05 | 多模态 Trace（附件）、Gateway guardrail、Trace 分页、更多 coding-agent 埋点 | 输入输出不再只是文本；网关承担安全策略 |
| 3.13 | 2026-06 | RBAC 与 Admin UI、Prompt 成为一等 RBAC 资源、Trace 归档 | 权限模型从“资源表”升级为“角色 + 授权” |
| 3.14 | 2026-06 | Review Queue、`@mlflow.test` pytest 集成、LLM Playground、Agent 一键接入 | 人工反馈与 CI 回归进入同一闭环 |
| 3.15 | 2026-07 | MCP Registry（实验）、多模态 Judge、Assistant 增强 | Agent 的工具依赖开始被版本化管理 |
| 3.16 | 2026-09 | 重设计 Trace Explorer、Span Links、自定义 Trace 视图 | Trace 从“调试工具”变成“可配置的评审界面” |
| 3.17 | 2026-10 | 子资源权限与显式 DENY、Trace 分析 SQL 汇总、类型化评测结果 | 权限粒度细到 run/trace/assessment；分析查询提速 |
| 3.18 | 2026-11 | Issues 分诊/自动问题检测增强、在线 code scorer、`compare_evaluations`/`assert_improved`、Skill Registry（实验）、presigned artifact、Helm 自动扩缩容、模型代码执行开关、**强制数据库升级** | 平台化能力落地，同时带来最重的一次升级约束 |

表中“时间”精确到月即可：不同镜像、rc 与正式版可能跨周，拿具体日期做审计没有意义。真正要记住的是**能力分组**与**升级顺序**。

## 37.3 工作区：多租户的第一层（3.10）

3.10 给 tracking server 加了 workspace 概念：

```bash
mlflow server \
  --backend-store-uri postgresql://... \
  --artifacts-destination s3://... \
  --enable-workspaces
```

也可以用环境变量 `MLFLOW_ENABLE_WORKSPACES=true`。开启后：

- 实验、注册模型、prompt 等资源按 workspace 逻辑隔离；
- UI 有 workspace landing page；
- 请求可以携带 `X-MLFLOW-WORKSPACE` 头，Gateway 等接口有 workspace fallback 行为；
- 3.12 增加 `mlflow db move-resources`，用于在 workspace 之间迁移资源。

### 工作区不是安全边界本身

这是最容易踩的坑。workspace 解决的是**命名空间与默认可见性**，不是“绝对隔离”。真正的隔离至少需要三者同时成立：

1. **认证**：请求必须能映射到一个 principal；
2. **授权**：principal 对 `(workspace, resource_type, resource_id)` 有明确 grant；
3. **存储隔离**：artifact 路径、数据库查询、缓存键都带 workspace 维度。

只开 `--enable-workspaces` 而不开认证，等于给所有用户一个带前缀的共享目录。反过来，只做认证不做 workspace，RBAC 的资源模式会变得非常长。

### 对自建平台的启示

如果你的团队只需要“实验分组”，用 experiment tags 或命名约定就够了，不必上 workspace。出现下面任一信号才值得引入：

- 多个团队要求“默认看不到对方的实验”；
- 需要按团队做配额、成本归集或审计；
- 同一套 Prompt/Model 名称在不同团队重复出现；
- 需要把资源从一个团队移交到另一个团队。

Mini 没有 workspace。最小改造不是加一个 `workspace` 字符串列，而是：

```text
workspaces(workspace_id, name, created_by)
每个资源表增加 workspace_id
所有查询强制注入 workspace_id
artifact 根路径增加 /workspaces/<id>/
鉴权中间件先解析 workspace，再解析资源
```

最后一条最关键：**workspace 必须在鉴权之前解析**，否则“先按 ID 查到资源，再检查它属于哪个 workspace”的写法会留下越权读取窗口。

## 37.4 RBAC：从资源表到角色（3.13 / 3.17 / 3.18）

### 3.13 的模型变化

3.13 把早期的“每资源一张权限表”收敛为角色模型：

```text
role_permissions(role, resource_type, resource_pattern, permission)
user_roles(user, role, workspace)
```

- 角色是 `(resource_type, resource_pattern, permission)` 的可复用包；
- 用户的有效权限是所拥有角色的并集；
- 资源类型包括 experiment、registered model、prompt、scorer、Gateway 的 secret/endpoint/model definition 等；
- 权限级别是 `READ → USE → EDIT → MANAGE`；
- Admin UI 提供账号页、角色页、授权页；
- 旧的逐资源权限 API/表被移除，`default_permission` 的语义也变了。

这是一次**破坏性变更**。如果你的自动化脚本调用过 `create_experiment_permission()` 之类的接口，升级到 3.13 必须改写为角色 API。

### 3.17 的子资源与 DENY

3.13 的权限粒度还停在“实验级”。3.17 把权限下沉到容器内资源：

```text
run
trace
assessment
logged_model
review_queue
registered_model_version
prompt_version
scorer_version
mcp_server_version
```

并引入显式 `DENY`：

- `DENY` 是覆盖性规则，优先级高于同一层级上的正向 grant；
- 子资源 tier 通常只接受 `*` 通配模式，按单个 ID 授权会被拒绝；
- 典型用法：给评测人员实验级 `READ`，但 `(trace, *, DENY)` 阻止其读取 trace 内容；
- `(assessment, *, DENY)` 会让 `GetTrace`/`SearchTraces` 不再返回 assessments，并拒绝 assessment 指标查询；
- 据 3.17 release notes，Admin UI 的资源类型从 8 个扩展到 17 个（精确数量以版本文档为准）。

这条设计的价值在于**可解释的例外**。只有“并集”时，想收回某个权限只能删除角色；有了 DENY，可以保留大部分能力，只屏蔽敏感子资源。代价是权限判定的复杂度上升：你必须能回答“当 READ 与 DENY 同时命中时谁赢”，并在测试里固定这个顺序。

### 3.18 的迁移陷阱

3.18 要求执行数据库升级，并且 MCP 版本标识在 MySQL/SQL Server 上改为**大小写敏感**。迁移会先检查是否存在仅大小写不同的引用；如果存在，会在改 schema 前中止。对使用大小写不敏感排序规则的数据库，这是一次真实的数据清理任务，不是“点一下升级”。

### 最小权限落地清单

1. 先定义 principal：人、服务账号、CI 机器人分开；
2. 按“团队角色 + 平台角色”两层建模，不要把个人直接绑到资源；
3. 默认拒绝，显式 grant；`default_permission` 只作为地板，不作为策略；
4. 对高风险资源（secret、Gateway endpoint、MCP server）单独设 `MANAGE`；
5. 每次权限变更写审计事件，包含 actor、target、old/new、request_id；
6. 用“越权测试”验证：低权限 principal 对每个资源类型至少尝试一次读/写，断言 403。

Mini 只有单一 Bearer token。最小 RBAC 改造需要：

```text
users(user_id, name, status)
roles(role_id, name)
role_permissions(role_id, resource_type, pattern, permission)
user_roles(user_id, role_id, workspace_id)
audit_events(id, actor, action, resource, before_json, after_json, ts)
```

然后在 HTTP 中间件里完成“认证 → 解析 workspace → 解析资源 → 授权 → 业务”的固定顺序。不要在业务函数里零散地写 `if user.is_admin`。

## 37.5 AI Gateway 回归：从独立进程到 tracking server（3.9 / 3.11 / 3.12）

第 29 章曾记录：2.9 把 AI Gateway 改名为 Deployments for LLMs，3.0 移除了独立 deployments server。这个事实只说到 3.0。**3.9 起 Gateway 以“内嵌在 tracking server”的形式回归**，不需要再启动第二个进程。

3.9 的能力包括：

- 路由策略与 fallback model；
- passthrough endpoint（Anthropic Messages、Gemini generateContent 等）；
- LiteLLM provider，覆盖大量上游；
- Gateway 调用校验移到 FastAPI 中间件；
- Gateway 作为 scorer 的 provider。

3.11 增加预算管理：

- 按日/周/月设置 spending limit；
- 超限前 webhook 告警，超限后返回 429；
- 本地 BudgetTracker 适合单实例，Redis 后端用于多副本原子扣减；
- 预算策略、违规状态与当前花费进入 UI。

3.12 增加 guardrail：

- 在 Gateway endpoint 上配置输入/输出 guardrail；
- `Guardrail` 基类、`JudgeGuardrail`、pre-LLM/post-LLM 阶段；
- guardrail 执行本身产生 trace span，便于审计；
- provider allow/deny list，降低“误配到非合规供应商”的风险。

### 架构影响

Gateway 内嵌不是“少一个进程”这么简单：

- **资源竞争**：LLM 请求的并发、超时与 tracking 写入共享同一个服务进程；必须分别设限并监控；
- **密钥边界**：provider secret 存在 tracking server 侧，能管理 Gateway 的人等于能花钱、能调用外部模型；
- **多副本一致性**：预算与限流若用本地内存，会随副本数线性放大额度；生产必须用 Redis 或数据库原子操作；
- **故障域**：Gateway 故障会拖垮 tracking UI/API。至少要把 Gateway 路由与 tracking 路由在超时、线程池/worker、健康检查上分开；
- **审计**：每次调用要能回答“谁、用哪个 endpoint、哪个模型、花了多少、命中哪条 guardrail”。

Mini 的 `gateway.py` 是单进程、内存限流/缓存、无真实 provider。它适合讲清“路由 + 回退 + 配额 + 成本”的控制面，但不能直接当生产网关。第 33 章的实现与本章的平台能力之间，差的是持久化、分布式状态和密钥管理。

## 37.6 MCP Registry 与 Skill Registry（3.15 / 3.18）

### 为什么 Agent 需要 Registry

一个 Agent 的能力来自工具。工具可能来自：

- 本地函数；
- 远程 MCP server；
- 平台提供的 skill/plugin。

当工具有了版本、别名、访问地址和工具清单，它就不再是配置文件里的一行 URL，而是一份**需要治理的资产**：谁注册的、当前 production 是哪个版本、工具列表何时变了、谁能访问。

3.15 的 MCP Registry（实验）给出三个核心实体：

```text
MCPServer          逻辑名（reverse-DNS，如 io.github.org/slug）
MCPServerVersion   语义化版本 + server.json 配置 + 工具快照
MCPAccessEndpoint  URL + transport，可绑定版本或别名
```

关键语义：

- 配置用语义化版本管理，支持 `@production`、`@staging` 这类可提升别名；
- 注册时可自动连接远程 server，抓取并快照工具列表；之后可手动刷新（支持 dry-run）；
- 工具清单按版本固定，便于回答“这次 Agent 行为变化是不是工具 schema 变了”；
- 状态生命周期是 draft → active → deprecated → deleted；
- 可通过 UI、REST、Python（`mlflow.genai` 下的 `register_mcp_server`、`search_mcp_servers`、`set_mcp_server_alias` 等）管理；
- 数据存在 tracking store，不是某个 experiment 的 artifact。

这套模型和 Prompt Registry 同构：**不可变版本 + 可变别名 + 元数据标签**。区别在于 MCP 版本还包含“工具 schema 快照”，因此 diff 能直接告诉你工具参数变了什么。

### 供应链风险

MCP server 是远程代码/服务的入口，风险比一段 prompt 高得多：

- 工具描述可能被投毒，诱导模型调用不该调用的工具；
- 远程 server 可能在同名版本下悄悄改行为；
- transport URL 可能指向内网或元数据服务（SSRF）；
- 工具返回内容可能携带 prompt injection；
- 别名提升没有评审，等于绕过发布流程。

最小控制集：

1. 只允许受信 registry/来源，记录来源 URL 与内容摘要；
2. 工具清单变化必须产生 diff 并触发评审；
3. 对 URL 做协议/域名/私网地址校验；
4. 版本固定到 digest/语义化版本，生产不读 `latest`；
5. 调用侧记录 server/version/tool，便于事故回溯。

3.18 的实验性 Skill Registry 是同一方向的延伸：把“可被 Agent 调用的能力”作为版本化资产管理。它仍是实验功能，不要把本章的描述当成稳定 API 承诺。

Mini 没有 MCP Registry。最小实现可以复用 Prompt 的三张表：

```text
mcp_servers(name, ...)
mcp_server_versions(name, version, config_json, tools_json, created_at)
mcp_server_aliases(name, alias, version)
```

真正难的不是表，而是“抓取工具清单”“对比 diff”“提升别名的审批”和“SSRF 防护”。

## 37.7 评测与人工闭环（3.9 / 3.10 / 3.14）

3.9–3.14 把“评测”从一次性脚本变成持续流程：

- **Judge Builder**：在 UI 里创建 LLM judge，配置指令、模型与评分标准；
- **Judge 优化**：MemAlign 一类算法从历史人工反馈中学习校准规则，减少 judge 与人的偏差；GePA 一类方法优化 judge 指令；
- **会话级评测**：3.8 引入 session-level scorer，3.10 支持多轮评测与对话模拟；
- **Review Queue（3.14）**：把 trace 分配给个人或评审组，按预设问题（Pass/Fail、分类、数值、文本）收集反馈；答案直接写成 assessment 或 expectation；
- **`@mlflow.test`（3.14）**：在 pytest 函数上跑 scorer，用 `assert result.passed, result.reason` 做回归门禁；结果进入 Evaluation runs UI；
- **LLM Playground（3.14）**：在浏览器里对 Gateway endpoint 和 Prompt 版本做迭代，支持工具、结构化输出与模板变量，可把结果存回 Prompt Registry；
- **评测数据集 UI**：搜索、排序、JSON 校验编辑、批量操作、从 trace 构建数据集。

3.18 又把这条链路推进到“运营面”：在线 code scorer（`ScorerSamplingConfig`）、Issues 自动问题检测与分诊、`compare_evaluations` 配对统计与 `assert_improved` 门禁。它们的 API、约束与工程取舍见第 46 章；本节只保留平台治理视角。

### 工程闭环

```text
线上 trace
  → 自动 scorer / judge 打分
  → Review Queue 人工复核
  → assessment（feedback / expectation）
  → 进入评测数据集
  → judge 校准 / prompt 迭代
  → CI 里 @mlflow.test 回归
  → 发布门禁
```

这条链路的技术难点不在 API，而在**数据契约**：

- feedback 与 expectation 必须区分：前者是“人觉得好不好”，后者是“正确答案是什么”；
- 同一 trace 的多次评估要能覆盖/失效，而不是无限追加；
- 评测集要能按来源、模型版本、prompt 版本切片；
- judge 的版本、指令、模型、温度都要记录，否则分数不可比；
- 人工评审要有配额与一致性检查（多人标注同一批样本）。

Mini 已有 assessment（feedback/expectation）、trace 关联、搜索和 evaluate 指标；第 46 章补上了会话级确定性 scorer、在线采样、规则分诊与配对比较，但没有 Review Queue、judge 校准、pytest 集成。第 33 章的最小闭环可以扩展成：

```text
review_queues(id, name, questions_json, assignee_group)
review_items(queue_id, trace_id, status, assignee)
```

状态机建议只有 `pending → in_review → completed → skipped`，并且“第一个完成的人清空整组任务”的语义要显式建模，否则会出现重复评审。

## 37.8 Trace 可观测性（3.9 / 3.12 / 3.16 / 3.17）

3.9 之后 Trace 的变化集中在四层：

1. **采集**：分布式 tracing（跨服务传递上下文）、原生 OTel GenAI 语义约定、更多 coding-agent 埋点（Claude Code、Codex、Gemini、Qwen 等）、多模态附件（PDF/音频/图像以 artifact attachment 存储）；
2. **存储**：Trace 分页、按需加载、附件 URI（`mlflow-attachment://`）、3.17 的 SQL 日汇总（daily rollups）；
3. **分析**：Trace Overview Dashboard、成本提取、会话分组、按 tag/metadata 的自定义列；
4. **交互**：3.16 重设计的 Trace Explorer、Span Links（跨 span/跨 trace 的显式链接）、自定义 Trace 视图。

自定义 Trace 视图值得单独说：它把“评审界面”变成 experiment 级的声明式资产。视图由组件（Text、Markdown、Card、StatCard、AssessmentCard、反馈按钮等）组成，保存为 experiment tag；打开不同 trace 时重新绑定数据。反馈控件写入普通 assessment（source=HUMAN），视图本身不存业务数据。

这带来一个架构启示：**UI 不是硬编码的**。当团队对“一条 trace 该看什么”有不同意见时，与其改前端，不如让视图可配置。代价是：

- 视图是代码/配置，需要版本化与评审；
- 视图里的反馈控件会写 assessment，必须走权限与校验；
- 视图绑定字段名后，字段改名会破坏视图；
- 自定义视图不能成为绕过 RBAC 的旁路。

Mini 的 trace 是同步写入、固定字段、单一 UI。第 19 章已经说明采样/异步/OTel 的差距；本章补充的是**平台层**差距：没有跨服务上下文、没有附件存储、没有可配置视图、没有分析汇总表。

## 37.9 安全与供应链（3.11 / 3.18）

3.11 与 3.18 的安全变化可以归纳为四条主线。

### 1. 反序列化

- 3.11 支持 `torch.export`、`skops` 等无 pickle 路径，并在关闭 `MLFLOW_ALLOW_PICKLE_DESERIALIZATION` 时提供更完整的加载能力；
- 3.18 引入 `MLFLOW_ALLOW_MODEL_CODE_EXECUTION`，设为 `false` 时拒绝模型自带的代码；旧的 pickle 开关被标记为弃用，但保留 pickle-only 语义；
- 这项开关也影响 LlamaIndex 等集成加载设置。

结论：**“能加载”与“允许执行模型代码”必须分开治理**。生产加载模型时，默认关闭代码执行，按模型来源白名单放行。

### 2. Artifact 传输

- 3.12 增加 S3 presigned upload URL；
- 3.18 增加 presigned-only artifact server 模式，并让 UI 支持 presigned 流程；
- 代理模式与直传模式的取舍变成显式配置，而不是部署巧合。

presigned URL 减少服务器带宽，但把“谁能拿到 URL、URL 有效期、能否重放”变成新的攻击面。至少要限制有效期、绑定对象键、记录签发审计。

### 3. Web 与路径

- 3.18 修复了 FileStore trace 的路径穿越 CVE；
- artifact proxy 鉴权忽略 `.` 路径段，防止用 `./` 绕过路径规则；
- 这提醒我们：路径规范化必须在鉴权之前完成，且要处理 URL 编码、大小写、符号链接。

### 4. 供应链

- UV 依赖锁增加 SHA-256 校验；
- Gateway provider allow/deny list；
- MCP server 来源与工具快照；
- 第三方 flavor/plugin 的入口点审核。

安全清单：

- [ ] 模型加载默认禁止代码执行；
- [ ] pickle 开关有明确的迁移计划；
- [ ] artifact 代理与 presigned URL 都有有效期和审计；
- [ ] 路径规范化在鉴权前完成；
- [ ] Gateway provider 白名单；
- [ ] MCP server 来源白名单与工具 diff；
- [ ] 依赖锁文件校验；
- [ ] 升级前读 CVE 与 breaking changes。

## 37.10 运维影响：3.18 的升级不是 `pip install`

3.18 明确要求数据库升级，且不支持混合版本运行。推荐顺序：

```text
1. 读 release notes / breaking changes
2. 备份数据库与 artifact（含恢复演练）
3. 停止所有 MLflow server 与其他写入方
4. 用 3.18 的 CLI 执行 mlflow db upgrade <db-url>
5. 启动单个新版本实例，跑 smoke test
6. 逐步放量，观察错误率/延迟/迁移残留
7. 保留回滚窗口与旧版本镜像
```

额外检查：

- MySQL/SQL Server 上 MCP 版本名大小写是否一致；迁移前先清洗；
- Helm chart 3.18 支持可选的 CPU/内存自动扩缩容，但 tracking server 是无状态服务，扩缩容前先确认数据库连接池与 artifact 代理带宽；
- 多工作区场景先用 `mlflow db move-resources` 做预演；
- presigned-only 模式要求客户端与网络策略同步调整；
- 监控要覆盖迁移后的查询计划变化（3.17 的 SQL rollup 可能改变读路径）。

Mini 有 `schema_meta.version`（当前 7）和幂等的 `CREATE TABLE IF NOT EXISTS`，但没有编号迁移脚本、回滚历史与在线迁移。这是一个刻意的教学简化，但也是生产化必须补的第一块：**版本化迁移脚本 + 迁移前备份 + 回滚演练**。第 7 章与第 26 章已经讨论过；本章补充的是“平台功能越多，迁移窗口越长”。

## 37.11 自建与托管的边界（商业视角）

3.9–3.18 的能力有一个共同点：**它们让 MLflow 更像平台，而不是库**。这直接改变自建与托管的成本对比。

- 工作区、RBAC、审计、预算、guardrail、MCP Registry 都是“运营型功能”：代码开源，但需要数据库、Redis、对象存储、身份提供商、备份与值班；
- 开源版已经包含工作区、RBAC、子资源 DENY、Gateway 预算/guardrail 与 MCP Registry（部分为实验）；商业化的差异更多在 SSO/SCIM、审计导出与保留、跨区域、合规认证、SLA 与托管升级，而不是“开源只有单租户/基础鉴权”；
- 对使用方，关键不是“开源有没有”，而是“我能不能在可接受的运维成本内把它跑稳”；
- 对平台方，护城河从“功能有无”转向“治理体验 + 生态集成 + 迁移成本”。

选型时建议做一张四象限：

| | 开源可自建 | 仅托管/企业版 |
|---|---|---|
| 记录/追踪 | Tracking、Registry、Trace | 大规模分析、跨区域 |
| 治理 | 基础 RBAC、workspace | SSO/SCIM、审计导出、合规认证 |
| GenAI | Prompt、Eval、Gateway、MCP | 托管模型、judge 优化、数据目录 |
| 运维 | 备份/升级/扩容 | SLA、值班、灾备 |

把每个格子的“自建成本/托管价格/迁移代价”写清楚，比争论“开源是否够用”更有用。

## 37.12 MiniMLflow 映射与最小改造清单

| 平台能力 | 真实 MLflow 3.x | Mini 现状 | 最小改造 |
|---|---|---|---|
| 工作区 | 3.10 `--enable-workspaces` | 无 | resources 加 `workspace_id`；鉴权前解析 |
| RBAC | 3.13 角色、3.17 DENY | 单 Bearer token | users/roles/grants/audit 四表 + 中间件 |
| Gateway 内嵌 | 3.9 tracking server 内 | 可选独立组件 | 路由挂到 server；预算用持久化计数器 |
| 预算/限流 | 3.11 Redis BudgetTracker | 内存限流 | Redis/DB 原子扣减 + 429 + webhook |
| Guardrail | 3.12 pre/post LLM | 无 | 策略接口 + span + 审计 |
| MCP Registry | 3.15 实验 | 无 | 复用 prompt 版本/别名模型 + 工具快照 |
| Review Queue | 3.14 | 无 | queue/item/status 三表 + assessment 写入 |
| pytest 回归 | 3.14 `@mlflow.test` | 无 | 装饰器包装 evaluate + 断言 |
| Trace 视图 | 3.16 声明式组件 | 固定 HTML | JSON schema + 渲染器 + tag 存储 |
| presigned artifact | 3.12/3.18 | 代理/base64 | 签名 URL + 对象存储 |
| 无 pickle 加载 | 3.11/3.18 | 仅 linear flavor | 白名单 loader + 代码执行开关 |
| DB 迁移 | `mlflow db upgrade` | `CREATE IF NOT EXISTS` | schema_version + 迁移脚本 |
| Webhook | 3.3+ Registry/Prompt events | `webhooks.py` + outbox | 事务性 outbox、secret 加密、多副本 claiming |

改造顺序建议：

1. **先鉴权，再工作区**：没有 principal，workspace 只是命名约定；
2. **先审计，再细粒度权限**：至少能回答“谁在什么时候改了什么”；
3. **先持久化预算，再谈多副本**：内存限流在多副本下等于没有；
4. **先固定评测集与回归，再谈 judge 优化**：没有稳定样本，优化的是噪声；
5. **先做工具清单 diff，再谈 MCP 别名提升**：供应链治理的核心是变更可见。

## 37.13 与真实 MLflow 的差距

- 本章描述的是 3.9–3.18 的公开能力，具体 API 名称、实验性标记与默认值可能随版本变化；
- 工作区、RBAC、Gateway 预算、MCP Registry 都依赖数据库/Redis/对象存储，Mini 的单 SQLite 模型无法表达其并发与一致性语义；
- 真实平台的 UI、审计与权限判定是产品级实现，本章只给出架构骨架；
- 3.18 的强制数据库升级意味着“升级”是一个项目，不是一次依赖 bump；
- 任何“开源版能不能替代托管”的结论都必须绑定规模、合规与运维能力，不能抽象回答。

## 小结

- 3.9–3.18 的主线是平台治理：工作区、RBAC、Gateway 预算/guardrail、MCP Registry、Review Queue 与 Trace 视图。
- 工作区是命名空间，不是安全边界；认证、授权、存储隔离必须同时成立。
- RBAC 从资源表走向角色 + 子资源 + 显式 DENY；DENY 的优先级必须被测试固定。
- Gateway 在 3.9 回归 tracking server，带来资源竞争、密钥集中与多副本预算一致性等新问题。
- MCP Registry 把 Agent 工具依赖变成版本化资产，核心风险是供应链与工具清单漂移。
- 评测闭环的关键是数据契约与人工反馈的可追溯，而不是多几个 scorer。
- 3.18 的数据库升级、安全开关与 presigned artifact 是运维事件，必须按变更项目管理。
- Mini 不实现这些能力，但它的版本/别名、assessment、限流、trace 与 webhook 模型提供了最小映射。
- 平台能力之间的自动化连接（alias 变更 → CI → 评测 → 部署）依赖第 38 章的事件投递；真实 GenAI API 的端到端用法见第 39 章。

## 练习

1. 为你的组织画一张权限矩阵：资源类型 × 角色 × 权限级别，标出至少两个需要 DENY 的例外。
2. 设计 workspace 的存储隔离方案：数据库、artifact、缓存、日志分别如何带 workspace 维度？
3. 写一个 Gateway 预算的并发测试：两个副本同时扣减最后 1 美元，断言不会超支。
4. 给 MCP server 设计一次“工具清单变更”的评审流程，列出必须阻断发布的条件。
5. 把一条 trace 的评审问题设计成 Review Queue 配置，说明答案如何写入 assessment。
6. 写一份 3.18 升级 runbook，包含备份、停写、迁移、smoke test、回滚与通知。
7. 用 Mini 的 Prompt 表设计 MCP Registry 的 schema，说明哪些字段不能复用。

---

返回：[教程首页](../README.md) · 上一章：[第 36 章 生产运维手册](36-production-runbook.md) · 下一章：[第 38 章 事件驱动与自动化治理](38-webhooks-events.md)
