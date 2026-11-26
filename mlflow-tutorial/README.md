# MLflow 深潜：从原理到可运行的 MiniMLflow，再到 GenAI 与商业分析

一本“双轨制”中文教程：**47 章正文 + 13 个附录 + 一个从零实现、可运行、可测试的 MiniMLflow**。既讲真实 MLflow 的概念/生态/生产实践，也把核心控制面用约一万八千行 Python 造一遍，让你看得见数据库、REST、模型包、Registry、LoggedModel、Trace、Prompt、Feedback、Dataset、Monitor、Gateway、Webhook、AgentOps、RAG 评测和 UI 的内部。第 37 章覆盖 2026 年 MLflow 3.9–3.18 的平台化能力（工作区、RBAC、MCP Registry、Gateway 预算与治理）；第 38 章实现事件驱动治理（签名 Webhook、Outbox、重试与 SSRF）；第 39 章用真实 MLflow 3.x 走一遍 GenAI 实战（Tracing、Prompt、Evaluate、Judge 与 CI 回归）；第 40 章把 LoggedModel 真正写进 MiniMLflow；第 41 章给出 Postgres + MinIO + Nginx 的参考部署编排与验收清单；第 42 章拆解模型服务与推理协议（`/invocations`、容器、K8s、批处理与灰度）；第 43 章用客户流失预警把数据、训练、评测、注册、服务与反馈串成一条可运营的交付链；第 44 章把评测数据集做成一等资产（digest、schema/profile、合并、搜索、trace → golden set 与数据治理）；第 45 章补上上线后的最后一公里：监控基线、PSI/KS/卡方漂移检测、告警与再训练闭环；第 46 章把 Agent 的会话、模拟评测、在线评分、Issues 分诊与统计门禁做成可运行闭环；第 47 章用知识助手把 Prompt、Gateway、Trace、黄金集、RAG 指标、配对门禁、在线抽样与漂移监控串成一条可运行、可回滚的 GenAI 交付链。

> 适用于 MLflow 2.x/3.x（含 2026 年的 3.18）的概念学习。真实 MLflow 的 API 与功能随版本变化，命令以 `--help` 和官方文档为准。MiniMLflow 是教学实现，不是生产平台的替代品。

## 本版更新（2026-11-26，GenAI 交付实战版）

- 新增**第 47 章 GenAI 综合实战：把企业知识助手做成可运营的产品**：从业务成功标准与文档契约讲到 Prompt/模型路由两个可变指针、BM25 检索基线、`recall@k`/`nDCG`、引用与 groundedness、配对 bootstrap 门禁、在线抽样、人工反馈、请求分布漂移与 5 分钟回滚演练。
- MiniMLflow 新增 `mini_mlflow/rag.py`：确定性 BM25（中文 bigram、轻量复数折叠、元数据过滤）、`recall@k`/`precision@k`/`MRR@k`/`nDCG@k`、引用提取与 precision/recall/F1、词项 groundedness；新增 `tests/test_rag.py` 的 17 个测试。测试总数从 **256 增至 273**。
- 新增可运行示例 `examples/20_genai_capstone.py`：4 篇知识库、8 条黄金问题、Prompt v1/v2、Gateway primary+backup、离线评测与双门禁、在线 CODE assessment + HUMAN feedback、稳定/漂移监控窗口；示例 19→20。
- 新增**附录 M 检索与 RAG 评测手册**：指标定义与手算、BM25 细节、分块/ACL、must_include/token F1/ROUGE-L 的边界、judge 校准、样本量/配对/多重比较、黄金集治理与检查清单。
- 同步全书相关章节（0/5/17/21/28/44/46/99）与附录 H/I/J/L 的计数、链接与交叉引用；保留第 46 章 AgentOps、第 45 章监控、第 44 章评测数据集、第 40 章 LoggedModel 与第 38 章 Webhook/Outbox 的全部内容。

## 你会得到什么

- 一条完整的实验追踪链路：Experiment → Run → Param/Metric/Artifact → Model → Registry → 部署。
- 一个可运行的 REST 服务、Python 客户端、CLI、Web UI、评分服务。
- 一个带签名的模型打包格式（`MLmodel` + PyFunc + 单一安全 flavor）。
- Projects、Evaluate、Autolog、Tracing/OTel、搜索 DSL、路径安全、软删除、分页。
- 数据血缘：`log_input` 记录 name/digest/source/context，并支持 `datasets.name/digest/context` 反查 run。
- 评测数据集：`create/get/search/merge/delete`、内容 digest、schema/profile、并发合并 CAS、数据集事件与 golden-set 治理（第 44 章）。
- 上线后监控：基线 profile、特征契约、PSI/KS/卡方、缺失率、不可变窗口报告、告警分级与再训练决策矩阵（第 45 章）。
- Agent 运营：会话聚合、脚本化多轮模拟、多轮 scorer、在线抽样评分、规则化 issue 分诊、配对 bootstrap 比较与 CI 门禁（第 46 章）。
- GenAI 交付闭环：知识库文档契约、BM25/召回指标、引用与 groundedness、Prompt+Gateway+Trace、离线双门禁、在线抽样、人工反馈、请求分布漂移与回滚（第 47 章、附录 M）。
- GenAI 控制面：Prompt Registry（版本/别名/安全渲染）、Feedback/Assessment（类型/来源/搜索）、AI Gateway（路由/回退/限流/缓存/成本）。
- 事件驱动治理：Registry/Prompt Webhook、事务性 outbox、HMAC 签名、指数退避、SSRF 边界与投递审计。
- MLflow 3 迁移：LoggedModel、`models:/<model_id>`、stages → aliases、FastAPI 评分服务。
- 模型中心落地：四张表、三状态机、模型级 params/metrics/tags、搜索方言、本地加载链与 11 个测试（第 40 章）。
- 生产部署实战：Postgres + MinIO + Nginx 参考编排、artifact 代理、迁移/备份/恢复与验收清单（第 41 章）。
- 模型服务与推理协议：评分端点、输入形状、签名、容器、Kubernetes、动态批处理、灰度与回滚（第 42 章）。
- 端到端综合实战：流失预警的数据契约、评测门禁、注册审批、服务化与反馈闭环（第 43 章）。
- 2026 平台化：多工作区、RBAC/子资源 DENY、MCP Registry、Gateway 预算/guardrail、Review Queue、Trace 自定义视图。
- 真实 MLflow 3.x GenAI 实战：`@mlflow.trace`、`mlflow.genai.evaluate`、scorer/judge、`@mlflow.test` CI 门禁、会话级评测（`mlflow.trace.session`/`search_sessions`）、`optimize_prompts` 与 MCP 供应链。
- 对真实生产差距的诚实分析：多租户、RBAC、对象存储、HA、迁移、运维与成本。
- 竞品/商业模式/ROI 分析，以及真实源码导读。

## 快速开始

```bash
python -m pip install -r requirements.txt
python -m pytest -q
python scripts/check_docs.py
```

启动教学服务器：

```bash
python -m mini_mlflow server \
  --backend-store-uri sqlite:///mlflow.db \
  --artifact-root ./mlartifacts \
  --host 127.0.0.1 --port 5000
```

打开 <http://127.0.0.1:5000/ui>。然后：

```bash
python examples/01_quickstart.py
python examples/05_tracing.py
python examples/06_project.py
python examples/11_prompts_feedback.py
python examples/12_gateway.py
python examples/13_webhooks.py
python examples/14_logged_models.py
python examples/15_serving_protocol.py
python examples/16_capstone_churn.py
python examples/17_datasets.py
python examples/18_monitoring.py
python examples/19_agent_ops.py
python examples/20_genai_capstone.py
```

完整验证（测试 + 文档 + 示例）：

```bash
python scripts/verify.py
# 只检查部署参考编排的 YAML/Nginx/脚本，不启动 Docker
python scripts/check_deploy.py
```

Prompt / Gateway CLI：

```bash
python -m mini_mlflow prompts register support-reply \
  --template "Hello {{name}}" --tag owner=support
python -m mini_mlflow prompts set-alias support-reply production 1
python -m mini_mlflow gateway validate --config examples/gateway.yaml
python -m mini_mlflow gateway invoke --config examples/gateway.yaml \
  --route support --message "hello"
```

## 架构一览

```text
┌──────────────┐   REST/HTTP   ┌─────────────────────────────┐
│ Python Client│ ────────────> │ Tracking Server             │
│ CLI / UI     │               │  experiments/runs/metrics   │
└──────────────┘               │  registry/traces/prompts    │
                               │  assessments/UI             │
                               └───────┬──────────────┬──────┘
                                       │              │
                          ┌────────────▼───┐   ┌──────▼────────────┐
                          │ SQLite         │   │ Local Artifacts   │
                          │ metadata       │   │ MLmodel / reports │
                          └────────────────┘   └───────────────────┘
```

Mini 刻意只实现“本地 SQLite + 本地文件 + 单 token”，把真实平台的多后端、云存储、RBAC、HA 留到正文分析。AI Gateway 是服务端可选组件（`--gateway-config`），与 tracking 协议解耦。RAG 检索与评测（`rag.py`）是应用侧胶水，不改变 tracking 协议，也不替你做向量库选型。

## 目录

### 第一部分 全景

- [前言：为什么用“造一遍”的方式学 MLflow](docs/00-preface.md)
- [第 1 章 为什么需要 MLflow：从 Notebook 到可复现的生产系统](docs/01-why-mlflow.md)
- [第 2 章 MLflow 的架构与能力边界](docs/02-architecture.md)
- [第 3 章 概念模型：Experiment、Run、Metric、Artifact、Model](docs/03-concepts.md)
- [第 4 章 15 分钟快速上手：真实 MLflow 与 MiniMLflow 双轨](docs/04-quickstart.md)

### 第二部分 MiniMLflow：从需求到可运行系统

- [第 5 章 工程蓝图：需求、非目标、分层与目录](docs/05-mini-blueprint.md)
- [第 6 章 配置、错误与数据实体](docs/06-config-errors-entities.md)
- [第 7 章 SQLite 存储层：Schema、事务与并发](docs/07-sqlite-store.md)
- [第 8 章 Tracking 核心：Run 生命周期与日志语义](docs/08-tracking.md)
- [第 9 章 搜索 DSL：解析、校验与安全 SQL](docs/09-search-dsl.md)
- [第 10 章 Artifact Store：路径安全、原子写与生命周期](docs/10-artifacts.md)
- [第 11 章 Model 封装：Flavor、MLmodel、签名与 PyFunc](docs/11-models.md)
- [第 12 章 Model Registry：版本、别名与发布状态](docs/12-registry.md)
- [第 13 章 REST Server：协议、错误模型与中间件](docs/13-rest-server.md)
- [第 14 章 Python Client 与远端一致性](docs/14-client.md)
- [第 15 章 CLI 与 Web UI：把系统变成可操作的产品](docs/15-cli-ui.md)
- [第 16 章 Projects：可复现实验的执行器](docs/16-projects.md)
- [第 17 章 Evaluate：回归、分类与 LLM 评测](docs/17-evaluate.md)
- [第 18 章 Autolog 与框架集成：便利的代价](docs/18-autolog.md)
- [第 19 章 Tracing：LLM 可观测性与 OpenTelemetry](docs/19-tracing.md)
- [第 20 章 安全、可靠性与多租户：从教学实现到生产系统](docs/20-security-reliability.md)
- [第 21 章 测试与验证：如何证明它能跑](docs/21-testing-verification.md)

### 第三部分 生产实践

- [第 22 章 实验管理方法论：命名、指标、对比与复现](docs/22-experiment-methodology.md)
- [第 23 章 模型注册与发布治理：审批、别名、回滚](docs/23-release-governance.md)
- [第 24 章 部署：批量、在线、流式与 Kubernetes](docs/24-deployment.md)
- [第 25 章 规模、性能与成本：从 SQLite 到平台](docs/25-scale-cost.md)
- [第 26 章 迁移与互操作：从文件存储到服务端](docs/26-migration-interop.md)
- [第 27 章 生态集成：Spark、Ray、PyTorch、Hugging Face 与 LLM 框架](docs/27-ecosystem.md)

### 第四部分 GenAI、未来与商业

- [第 28 章 GenAI 时代的 MLflow：Tracing、Prompt、Eval 与 Gateway](docs/28-genai-mlflow.md)
- [第 29 章 MLflow 3 与未来路线：机会、标准与风险](docs/29-future.md)
- [第 30 章 竞品对比：W&B、Kubeflow、LangSmith 与云平台（含 Neptune 复盘）](docs/30-competitors.md)
- [第 31 章 商业模式与市场：开源核心、托管服务与平台战争](docs/31-business-model.md)
- [第 32 章 采用策略、ROI 与组织落地：从试点到平台化](docs/32-adoption-roi.md)

### 第五部分 工程进阶、平台化与运维

- [第 33 章 Prompt Registry、Feedback 与 AI Gateway：把 GenAI 控制面写出来](docs/33-prompts-feedback-gateway.md)
- [第 34 章 MLflow 3 迁移与 Logged Model：从 Run 中心到模型中心](docs/34-mlflow3-logged-models.md)
- [第 35 章 真实 MLflow 源码导读：从 CLI 到存储适配器](docs/35-real-mlflow-source-guide.md)
- [第 36 章 生产运维手册：升级、备份、监控与演练](docs/36-production-runbook.md)
- [第 37 章 MLflow 3.9–3.18 平台化：工作区、RBAC、MCP 与 AI 治理（2026）](docs/37-platformization-2026.md)
- [第 38 章 事件驱动与自动化治理：Webhook、Outbox 与签名](docs/38-webhooks-events.md)
- [第 39 章 真实 MLflow 3.x GenAI 实战：Tracing、Prompt、Evaluate 与 CI 回归](docs/39-real-genai-hands-on.md)
- [第 40 章 LoggedModel 参考实现：把 MiniMLflow 推进到模型中心](docs/40-logged-model-implementation.md)
- [第 41 章 真实 MLflow 生产部署实战：Compose、Postgres、MinIO 与反向代理](docs/41-production-deployment.md)

### 第六部分 服务化、综合实战、数据资产与 GenAI 交付

- [第 42 章 模型服务与推理协议：从 `/invocations` 到生产网关](docs/42-model-serving-protocol.md)
- [第 43 章 综合实战：把流失预警做成可运营的 AI 产品](docs/43-capstone-churn.md)
- [第 44 章 评测数据集：从 `log_input` 到可运营的 Golden Set](docs/44-evaluation-datasets.md)
- [第 45 章 上线之后：模型监控、漂移检测与再训练闭环](docs/45-monitoring-drift.md)
- [第 46 章 Agent 工程闭环：会话、模拟评测、在线评分与问题分诊（MLflow 3.18）](docs/46-agent-ops.md)
- [第 47 章 GenAI 综合实战：把企业知识助手做成可运营的产品](docs/47-genai-capstone.md)
- [结语：把控制面做成肌肉记忆](docs/99-conclusion.md)

### 附录

- [附录 A REST API 速查](docs/appendix-a-rest-api.md)
- [附录 B 数据库 Schema 与不变量](docs/appendix-b-schema.md)
- [附录 C 配置与环境变量](docs/appendix-c-configuration.md)
- [附录 D 搜索语法参考](docs/appendix-d-search-syntax.md)
- [附录 E CLI 参考](docs/appendix-e-cli.md)
- [附录 F 故障排查](docs/appendix-f-troubleshooting.md)
- [附录 G 术语表](docs/appendix-g-glossary.md)
- [附录 H 练习与参考答案](docs/appendix-h-exercises.md)
- [附录 I 验证报告](docs/appendix-i-verification.md)
- [附录 J 延伸阅读](docs/appendix-j-reading.md)
- [附录 K MLflow 3.x 迁移与 API 对照](docs/appendix-k-mlflow3-migration.md)
- [附录 L 真实 MLflow 源码地图与调试入口](docs/appendix-l-source-map.md)
- [附录 M 检索与 RAG 评测手册：从 recall@k 到 groundedness](docs/appendix-m-rag-evaluation.md)

## 源码地图

```text
mini_mlflow/
  config.py, errors.py, entities.py, utils.py, version.py
  store/schema.sql, store/sqlite_store.py
  tracking.py, registry.py, search.py, artifacts.py
  models/ (__init__.py, data.py, signature.py, linear.py, model.py, pyfunc.py)
  client.py, fluent.py, server.py, ui.py, cli.py
  projects.py, evaluate.py, autolog.py, tracing.py, scoring.py
  prompts.py, gateway.py   # Prompt Registry / Feedback / AI Gateway
  datasets.py              # 评测数据集：digest、schema/profile、合并 CAS
  monitoring.py            # 监控基线、PSI/KS/卡方与窗口报告
  agentops.py              # 会话聚合、模拟评测、在线评分、问题分诊、配对比较
  rag.py                   # BM25、recall@k/MRR/nDCG、引用与 groundedness
  webhooks.py              # 事件订阅、outbox、签名与重试
  # LoggedModel 分布在 entities / tracking / store / search / server / client
tests/         273 个自动化测试（以当前 pytest 输出为准）
examples/      20 个可运行示例 + 1 个 MLproject + gateway.yaml + deploy/
scripts/       文档检查、全量验证
docs/          47 章 + 13 附录
```

关键文件：

- 数据库 DDL：[mini_mlflow/store/schema.sql](mini_mlflow/store/schema.sql)
- 搜索编译器：[mini_mlflow/search.py](mini_mlflow/search.py)
- REST 路由：[mini_mlflow/server.py](mini_mlflow/server.py)
- 模型打包：[mini_mlflow/models/model.py](mini_mlflow/models/model.py)
- Trace：[mini_mlflow/tracing.py](mini_mlflow/tracing.py)
- Prompt Registry：[mini_mlflow/prompts.py](mini_mlflow/prompts.py)
- 评测数据集：[mini_mlflow/datasets.py](mini_mlflow/datasets.py)
- 监控与漂移：[mini_mlflow/monitoring.py](mini_mlflow/monitoring.py)
- Agent 会话与评测：[mini_mlflow/agentops.py](mini_mlflow/agentops.py)
- RAG 检索与评测：[mini_mlflow/rag.py](mini_mlflow/rag.py)
- AI Gateway：[mini_mlflow/gateway.py](mini_mlflow/gateway.py)
- Webhook/Outbox：[mini_mlflow/webhooks.py](mini_mlflow/webhooks.py)
- 评分协议：[mini_mlflow/scoring.py](mini_mlflow/scoring.py)
- LoggedModel：[mini_mlflow/tracking.py](mini_mlflow/tracking.py) 的 `create_logged_model` 与 [schema.sql](mini_mlflow/store/schema.sql)
- 生产编排参考：[examples/deploy/](examples/deploy/)
- 验证：[scripts/verify.py](scripts/verify.py)

## 示例

| 示例 | 内容 |
|---|---|
| `examples/01_quickstart.py` | 训练/日志/评测/打包/注册/加载 |
| `examples/02_search_and_artifacts.py` | 搜索、数据集过滤、指标历史、artifact 下载 |
| `examples/03_registry_and_aliases.py` | 版本、别名、`models:/@alias` |
| `examples/04_evaluate.py` | 分类评测与报告 |
| `examples/05_tracing.py` | 嵌套 span、持久化、OTel 导出 |
| `examples/06_project.py` | 运行 `examples/projects/churn` |
| `examples/07_remote_server.py` | 进程内服务器 + 远端客户端 |
| `examples/08_serving.py` | 评分服务 |
| `examples/09_autolog.py` | autolog 捕获 fit |
| `examples/10_end_to_end.py` | 完整生命周期 |
| `examples/11_prompts_feedback.py` | Prompt 版本/别名、Trace feedback、assessment 搜索 |
| `examples/12_gateway.py` | 路由、回退、缓存、成本统计 |
| `examples/13_webhooks.py` | 签名 Webhook、outbox、投递验证 |
| `examples/14_logged_models.py` | LoggedModel 生命周期、搜索、`models:/<id>` 加载 |
| `examples/15_serving_protocol.py` | 评分协议矩阵：健康检查、四种输入形状、鉴权与错误码 |
| `examples/16_capstone_churn.py` | 数据血缘 → 训练 → 切片评测 → 注册 → 服务 → Trace/Feedback |
| `examples/17_datasets.py` | 数据集创建、内容 digest、合并、搜索与删除 |
| `examples/18_monitoring.py` | 基线数据集、稳定/漂移窗口、PSI/KS 报告与 run 记录 |
| `examples/19_agent_ops.py` | 会话模拟、多轮评分、issue 分诊、配对比较与 golden set |
| `examples/20_genai_capstone.py` | 知识助手：BM25、Prompt/Gateway/Trace、离线门禁、在线评分与漂移监控 |
| `examples/deploy/` | 参考生产编排（Compose + Postgres + MinIO + Nginx） |

示例默认写 `examples/runtime/`；可设置 `MINI_MLFLOW_TRACKING_URI` 指向服务器。真实 MLflow 的对照见第 4 章。

## 验证状态

截至 2026-11-26（详见[附录 I](docs/appendix-i-verification.md)）：

- `python -m pytest -q`：**273 passed**（Windows 11 / Python 3.14；非 Windows 会跳过 1 个 Windows 路径测试，以当前输出为准）；
- `python scripts/check_docs.py`：通过（47 章 + 13 附录）；
- 20 个示例均可运行；`examples/deploy/` 通过 `check_deploy.py` 的 YAML/Nginx/脚本静态检查，不启动 Docker；
- `python scripts/verify.py`：全量通过（测试 + 文档 + 部署静态检查 + 示例）。

测试通过 ≠ 生产就绪。第 20～21 章列出安全/可靠性/测试盲区。

## 约束与边界

- 只支持 SQLite 元数据后端与本地 artifact；
- 只内置 linear flavor，禁止未知 loader/pickle；
- 单 token 鉴权，无 RBAC/多租户；搜索支持 `datasets.name/digest/context`，但不存 schema/profile；
- REST 与真实 MLflow“同构但非逐字节兼容”；
- 同步 trace 写入，无采样/异步批处理；
- 不包含 Conda/Docker 项目执行、云部署、自动埋点生态；
- 评分服务只实现 `/invocations` 的最小 JSON 契约；无批处理、限流、TLS、OIDC、GPU 与多模型路由（第 42 章给出生产清单）；
- GenAI 实现手动 Tracing、基础评测、Prompt Registry（文本模板）、Feedback/Assessment、教学版 AI Gateway；不包含 chat-style prompt 模板、labeling session、分布式限流/缓存、真实 provider SDK；
- Webhook 是单进程、同步 `drain_once` 的教学实现；secret 未加密，outbox 与业务写入不在同一事务，生产改造清单见第 38 章；
- LoggedModel 已实现核心闭环（元数据、三状态、模型级 params/metrics/tags、搜索、本地 `models:/<id>` 加载），但未实现注册关系反查、模型别名、数据集关联、远程制品下载与 GC（第 40 章逐项列出）。
- 评测数据集已实现最小闭环（记录、digest、schema/profile、合并 CAS、搜索、事件），但为全局资产（无 experiment 归属），且未实现真实 MLflow 的 inputs 哈希去重、记录级删除/更新、分页与 `mlflow.genai.evaluate` 直连（第 44 章逐项列出）。
- 监控已实现批式最小闭环（基线 profile、PSI/KS/卡方、缺失率、不可变窗口、REST/CLI），但无流式聚合、标签 join、分群、告警投递、权限与保留期；卡方 p 值为近似，基线样本上限 2000（第 45 章逐项列出）。
- AgentOps 已实现派生式最小闭环（会话聚合、脚本模拟、确定性 scorer、批式在线采样、规则分诊、配对 bootstrap、数据集转换），但无 LLM 模拟用户、异步评分、独立 session 表、跨服务拼接、Issues 状态机、BCa/多重比较校正与 Skill Registry（第 46 章逐项列出）。
- RAG 评测已实现确定性最小闭环（BM25、recall/precision/MRR/nDCG、引用 F1、词项 groundedness、配对门禁），但无向量检索、真实 reranker（示例为 identity rerank）、NLI/LLM judge、分布式评测、流式在线评分与真实 provider；检索/回答指标不代表业务效果（第 47 章、附录 M 逐项列出）。

这些是刻意取舍，正文逐项解释“生产要补什么”。

## 许可

本教程与 MiniMLflow 代码用于教学，采用 MIT 许可证。真实 MLflow 的商标/许可以其项目为准。
