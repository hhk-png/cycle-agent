# 第 5 章 工程蓝图：需求、非目标、分层与目录

## 学习目标

- 把“实现一个 MLflow”拆成可交付的工程需求，而不是功能清单。
- 理解质量属性（一致性、安全、可测试性）如何影响架构。
- 读懂 MiniMLflow 的目录结构与依赖规则。
- 学会用 ADR（Architecture Decision Record）记录取舍。

## 5.1 需求不是“所有功能”

“实现 MLflow 的所有内容”如果按字面执行，会得到数百万行代码、几十个后端、云 SDK 和前端应用。教学实现必须把需求定义成**可验证的行为**：

### 功能需求（本实现覆盖）

| 编号 | 需求 | 验收方式 |
|---|---|---|
| F1 | 创建/查询/搜索实验，支持标签与软删除 | 测试 + UI |
| F2 | Run 生命周期、参数、指标历史、标签、输入数据集 | 单元/集成测试 |
| F3 | 搜索 DSL（属性/参数/指标/标签）与分页 | DSL 测试 + SQL 参数化检查 |
| F4 | 本地 artifact 上传、列表、下载、路径隔离 | 安全测试 |
| F5 | 模型保存/加载、签名、PyFunc、单 flavor | 模型测试 |
| F6 | Registry 版本、别名、阶段、标签、来源校验 | API 测试 |
| F7 | REST 服务、鉴权、错误模型、UI | 客户端契约测试 |
| F8 | Python 客户端本地/远端一致 | 双后端测试 |
| F9 | MLproject 执行、参数、日志、源码快照 | 子进程测试 |
| F10 | 回归/分类/LLM 评测 | 公式测试 |
| F11 | autolog、Trace/Span、OTel 导出 | 上下文/异常测试 |
| F12 | CLI、评分服务、端到端示例 | 子进程测试 |
| F13 | Prompt Registry：版本、别名、变量校验、安全渲染 | 单元 + 远端契约测试 |
| F14 | Feedback/Assessment：类型、来源、upsert、trace 搜索 | 单元 + 搜索测试 |
| F15 | AI Gateway：路由、回退、限流、缓存、成本、密钥隔离 | 故障注入 + HTTP 契约测试 |
| F16 | Webhook：订阅、outbox、HMAC 签名、重试、投递历史 | 失败注入 + 重启恢复测试 |
| F17 | LoggedModel：模型实体、三状态、模型级参数/指标/标签、`models:/<id>` 加载 | 第 40 章测试 |
| F18 | 评测数据集：记录、digest、schema/profile、合并 CAS、搜索 | 第 44 章测试 |
| F19 | 监控：基线 profile、PSI/KS/卡方、不可变窗口报告、阈值 | 第 45 章测试 |
| F20 | AgentOps：会话聚合、脚本模拟、确定性 scorer、在线采样、问题分诊、配对比较 | 第 46 章测试 |
| F21 | RAG 评测：BM25、recall/precision/MRR/nDCG、引用 F1、词项 groundedness | 第 47 章测试 |

### 非功能需求

- **一致性**：参数不可变、批量日志原子、状态机单向。
- **安全**：不执行不可信反序列化；artifact 路径不可逃逸；搜索不使用字符串拼接；HTML 全量转义。
- **可测试性**：服务可注入临时配置；不依赖全局单例（fluent 除外）。
- **可移植性**：Windows/Linux；只依赖标准库 + PyYAML。
- **可解释性**：错误码稳定，异常信息包含上下文。
- **性能下限**：单机千级 run、万级 metric 可交互；不承诺生产吞吐。

### 非目标

- REST 与真实 MLflow 逐字节兼容；
- 多租户/RBAC/OIDC；
- S3/GCS/Azure；
- 分布式训练与调度；
- 生产级推理网关（Mini 只实现教学版路由/限流，不含真实 provider、分布式配额与流式）；
- 自动埋点所有 LLM 框架；
- 完整数据版本系统（Mini 只有第 44 章的评测数据集最小闭环）与 labeling session；
- 在线 schema 迁移（Mini 只有 `schema_meta.version` + 幂等 DDL 的最小演示）。

把这些写进非目标，能防止实现被“顺手加一个”拖垮。

## 5.2 质量属性场景

需求只有配上场景才可验证：

1. **可审计性**：给定 run_id，5 秒内能列出参数、最新指标、artifact 树和模型版本来源。
2. **不可变性**：两个并发客户端把同一参数写成不同值，最多一个成功，不能静默覆盖。
3. **隔离性**：恶意 artifact 路径 `../../etc/passwd` 无法写出 artifact 根。
4. **可恢复性**：删除 run 后可通过 API 恢复元数据；artifact 不被自动删除。
5. **协议稳定性**：客户端断网时给出可识别错误，而不是 500。
6. **可测试性**：每个服务方法可在临时 SQLite 上独立运行。

第 20、21 章会逐项验证。

## 5.3 分层

```text
┌───────────────────────────────────────────────┐
│ Interface: HTTP handler / CLI / UI / Fluent   │
├───────────────────────────────────────────────┤
│ Application: TrackingService / ModelRegistry  │
│              Project runner / Evaluator        │
├───────────────────────────────────────────────┤
│ Domain: entities / errors / search AST / rules │
├───────────────────────────────────────────────┤
│ Infrastructure: SqliteStore / ArtifactRepo     │
│                 HTTP client / model loaders    │
└───────────────────────────────────────────────┘
```

依赖规则：

- Domain 层不导入 Infrastructure；
- Service 依赖 Domain + Infrastructure 接口；
- Interface 只做参数提取和序列化；
- Infrastructure 不包含“业务应该怎么做”的判断。

这条规则的实际收益：测试 `TrackingService` 不需要启动 HTTP；测试搜索编译器不需要数据库；测试 artifact 路径安全不需要服务。

## 5.4 目录

```text
mini_mlflow/
  __init__.py          # 对外 fluent API
  config.py            # 配置优先级与路径解析
  errors.py            # 错误码 → HTTP 状态
  entities.py          # 数据类 + from_dict/to_dict
  utils.py             # 路径、JSON、时间、校验
  artifacts.py         # 本地制品仓库
  search.py            # DSL 词法/语法/编译
  store/
    schema.sql         # DDL（可审计）
    sqlite_store.py    # 唯一包含 SQL 的模块
  tracking.py          # 用例层
  registry.py          # Registry 规则
  models/
    data.py            # 输入规范化
    signature.py       # Schema 推断/校验
    linear.py          # 教学 flavor
    model.py           # MLmodel 打包/加载
    pyfunc.py          # 统一预测接口
  fluent.py            # ContextVar active run
  client.py            # 本地/远端客户端
  server.py            # REST + UI 路由 + 鉴权
  ui.py                # 服务端渲染
  cli.py               # 命令行
  projects.py          # MLproject
  evaluate.py          # 指标
  autolog.py           # fit 回调
  tracing.py           # Trace/Span
  scoring.py           # 推理服务
  prompts.py           # Prompt Registry
  datasets.py          # 评测数据集：digest、schema/profile、合并 CAS
  monitoring.py        # 监控基线、PSI/KS/卡方、窗口报告
  gateway.py           # AI Gateway
  webhooks.py          # 事件订阅、outbox、签名、重试
```

## 5.5 关键 ADR

### ADR-001：只支持 SQLite

**背景**：教学环境不能假设有 Postgres；需要零依赖启动。
**决定**：用 SQLite + WAL，单写者。
**后果**：并发写有限；不能多服务器共享；迁移能力弱。
**复审条件**：需要团队共享、>10 写/秒、或需要在线迁移时换 Postgres。

### ADR-002：不做真实 MLflow 的 REST 兼容层

**背景**：逐字段兼容会引入大量历史包袱，且难以解释。
**决定**：路径与核心字段同构，语义自定义，文档明确差异。
**后果**：真实 MLflow 客户端不能直接连 Mini；需要适配器。
**复审条件**：如果目标是替换真实 MLflow，应先做 OpenAPI 差异测试与契约测试。

### ADR-003：模型反序列化禁止 pickle

**背景**：pickle 加载可执行任意代码，服务端加载用户上传的模型等于 RCE。
**决定**：内置 linear flavor 只用 JSON；未知 loader 明确拒绝。
**后果**：不能直接加载 sklearn/torch；需要可信沙箱或签名制品。
**复审条件**：引入签名验证、隔离沙箱和制品准入后再支持。

### ADR-004：搜索 DSL 编译为参数化 SQL

**背景**：`eval()` 或字符串拼接会导致注入。
**决定**：词法 → AST → 受控字段映射 → 绑定参数；只允许白名单字段。
**后果**：不支持函数、子查询、算术；用户需要学习子集。
**复审条件**：若需要更复杂查询，应引入专用查询服务/OLAP，而不是扩大手写 SQL 面。

### ADR-005：同步写 Trace

**背景**：教学实现简单优先。
**决定**：Trace 结束时同步写库。
**后果**：高 QPS 下阻塞应用；无采样。
**复审条件**：生产需异步队列 + 批写 + 背压 + 采样。

### ADR-006：Webhook 用 outbox，而不是同步回调

**背景**：Registry 写操作不能因为外部接收方超时/5xx 而失败；但事件也不能因为进程崩溃而丢失。
**决定**：先写 `webhook_deliveries`（PENDING），再由 dispatcher 投递；成功改 DELIVERED，失败按指数退避重试，达到上限改 FAILED。
**后果**：投递是至少一次，接收方必须按 delivery id 去重；Mini 的业务写入与 outbox 插入仍不在同一事务，存在提交后崩溃丢事件的窗口。
**复审条件**：生产必须改事务性 outbox、secret 加密、多副本 claiming、DLQ 回放与 SSRF 出口策略。

## 5.6 版本与兼容策略

- `mini_mlflow/version.py` 是唯一版本来源。
- `MLmodel` 包含 `model_format_version` 与 `mini_mlflow_version`。
- 数据库 `schema_meta.version=7`（v7 新增 `monitors`/`monitor_windows`）；不兼容变更必须新增迁移，不得静默改表。
- REST 路径带 `/api/2.0/mlflow/`，但字段不承诺跨大版本稳定。

## 5.7 交付顺序

```text
1. 实体与错误
2. SQLite schema + store
3. Tracking 服务 + 测试
4. 搜索编译器
5. Artifact 仓库 + 路径安全
6. 模型打包 + 注册表
7. REST + 客户端契约测试
8. CLI/UI
9. Projects/Evaluate/Autolog/Tracing
10. Webhook/Outbox/签名
11. Prompt/Assessment/Gateway
12. LoggedModel/评测数据集
13. 监控与漂移
14. 安全与性能加固
```

这个顺序的要点是：**先有可测试的领域行为，再有接口**。反过来做，很容易得到一个“路由很多但语义错误”的系统。

## 5.8 与真实 MLflow 的差距

真实 MLflow 是多年演进的平台，包含：

- 数据库迁移与多后端；
- 完整的模型 flavor 生态；
- 生产级 REST 契约；
- 企业鉴权与云集成；
- 大规模 tracing 管道；
- 发布与治理 UI。

Mini 的蓝图是“可解释的最小闭环”，不是“缩小版生产系统”。

## 小结

- 把“全部功能”改写为可验证的功能/非功能需求，才能工程化。
- 分层与依赖规则决定可测试性，ADR 决定取舍可追溯。
- 明确非目标与复审条件，避免范围蔓延。
- 先领域、后接口、再交付。

## 练习

1. 为“支持 S3 artifact”写一份 ADR，列出新增模块、配置、测试和风险。
2. 如果把 Trace 改成异步写入，哪些一致性场景会变化？如何保证不丢数据？
3. 选一个非目标，论证在什么业务条件下应该转为目标。

---

下一章：[第 6 章 配置、错误与数据实体](06-config-errors-entities.md)
