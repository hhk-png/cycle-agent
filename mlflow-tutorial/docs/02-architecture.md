# 第 2 章 MLflow 的架构与能力边界

## 学习目标

- 画出 MLflow 的逻辑架构：客户端、Tracking Server、后端存储、Artifact Store、Registry、UI。
- 区分“元数据”和“制品”，理解为什么它们通常分开存储。
- 理解本地模式与服务器模式的差异，以及 SQLite 教学实现的边界。
- 把真实组件映射到 MiniMLflow 的模块。

## 2.1 逻辑架构

```text
┌──────────────┐   REST/HTTP   ┌─────────────────────────────┐
│ Python Client│ ────────────> │ Tracking Server             │
│ CLI          │               │  ├─ Experiments / Runs      │
│ Web UI       │               │  ├─ Metrics / Params / Tags │
└──────────────┘               │  ├─ Model Registry          │
                               │  └─ Artifact service        │
                               └───────┬──────────────┬──────┘
                                       │              │
                          ┌────────────▼───┐   ┌──────▼────────────┐
                          │ Metadata DB    │   │ Artifact Store    │
                          │ SQLite/Postgres│   │ FS/S3/GCS/Azure   │
                          └────────────────┘   └───────────────────┘
```

关键点：

1. **客户端不直接写数据库**：服务器模式由服务端统一校验、事务和鉴权。
2. **元数据与制品分离**：run 记录里保存 artifact URI，而不是把模型二进制塞进数据库。
3. **UI 是读模型**：真实 MLflow 的 React UI 通过 REST 读取数据，不能绕过权限；Mini 的 UI 是服务端渲染、进程内调用 `TrackingService`，但同样经过 `_authenticate` 的鉴权入口。两者都不应让 UI 直接写数据库。
4. **Registry 不是文件库**：模型版本保存 source URI（3.x 常为 `models:/<model_id>`，2.x 兼容为 `runs:/...`），制品仍在 Artifact Store 中。
5. **Webhook 是出站事件适配器**：Registry/Prompt/Dataset 的写操作产生 outbox 事件，由 dispatcher 投递给 CI、部署或资产目录；它不改变 tracking 协议，也不承载模型字节。
6. **检索器不属于 MLflow 控制面**：`rag.py` 是应用侧胶水，负责把文档变成候选、把候选变成可评测的证据；MLflow 记录的是检索 span、数据集、指标与 assessment，而不是替你选向量库。

### 历史组件：Recipes

早期 MLflow 还提供 Recipes（`MLproject`/recipe YAML 驱动的数据准备、训练、评估流水线），试图标准化“一个完整 ML 工程”的步骤。它在 2.x 被标记弃用，并在 3.0 移除；现代实践更常用显式的训练脚本 + Projects/工作流引擎（Airflow、Kubeflow Pipelines、Ray、云流水线）。MiniMLflow 不实现 Recipes；理解它有助于读懂旧文档，但不应把 Recipes 当成新项目的默认选择。真实 MLflow 的组件可用性请以当前版本文档为准。

## 2.2 元数据与制品：为什么要分开

把 10 GB 模型放进 Postgres 的 BLOB 列会带来：

- 备份/恢复时间不可控；
- 数据库连接被大对象传输占满；
- 无法直接利用对象存储的版本、生命周期和 CDN；
- 权限模型变得复杂（数据库账号与对象存储账号不一致）。

因此，MLflow 的设计是：数据库存**指针和描述**，对象存储存**字节**。代价是两套系统的一致性：

- run 记录已提交，但 artifact 上传失败，会出现“有 run 没制品”；
- 对象存储成功但数据库回滚，会出现“孤儿制品”；
- 删除 run 默认不删除远端 artifact，需要 GC/生命周期策略。

MiniMLflow 采用本地目录 + SQLite，把一致性问题缩小成“同一文件系统内的两次操作”，但问题并没有消失：`log_model` 先写 artifact 再注册版本，中间失败会留下孤儿文件。第 20 章给出对账与清理方案。

## 2.3 核心数据模型

```text
Experiment 1 ─── N Run 1 ─── N Metric (history)
     │                 ├── N Param (immutable)
     │                 ├── N Tag (mutable)
     │                 ├── N Artifact (tree)
     │                 └── N LoggedModel (source_run_id)
     ├── N LoggedModel
     └── N Trace
RegisteredModel 1 ─── N ModelVersion
                       └── N Alias (name → version)
Trace 1 ─── N Span
Dataset 1 ─── N DatasetRecord
Monitor 1 ─── N MonitorWindow
```

几个容易混淆的关系：

- **Run 属于且仅属于一个 Experiment**。跨实验对比需要主动查询多个实验。
- **Metric 是时间序列**，`get_run` 通常返回每个 key 的最新值；历史点要单独查询。
- **Param 不可变**，同一个 key 重复写相同值幂等，写不同值应报错。
- **Tag 可变**，适合记录状态、团队、评审结果。
- **Model Version 不是 Run 的替代品**：一个 run 可以有多个模型制品，一个注册版本只指向一个 source。
- **Trace 可以与 Run 关联，但不是 Run 的子对象**。LLM 调用可能发生在服务请求中，没有训练 run。
- **Dataset 不是 Run input**。Run input 是 run 上的血缘指针；Dataset 是独立、可版本化、可合并的评测资产。
- **LoggedModel 不是 ModelVersion**。LoggedModel 是一次训练产物（`model_id`、可未注册、可独立加载）；ModelVersion 是 Registry 里的命名快照，3.x 通过 `model_id` 反查来源。详见第 34、40 章。
- **Monitor 是派生资产，不是 MLflow 原生对象**。它绑定基线数据与模型版本，窗口是不可变证据；实现见第 45 章。

## 2.4 两种部署模式

### 本地文件模式

```bash
mlflow run .          # 直接写本地目录
mlflow ui             # 读本地 mlruns
```

适合单人开发，不需要服务器。缺点是并发、权限、远程协作和数据库查询能力弱。

### 服务器模式

```bash
mlflow server \
  --backend-store-uri postgresql://... \
  --artifacts-destination s3://my-bucket/mlflow \
  --host 0.0.0.0 --port 5000
```

适合团队。服务器模式又分为：

- **元数据在远端，制品也在远端**：最常见；
- **元数据在远端，制品由客户端直传对象存储**：减少服务器带宽，但需要客户端凭证；
- **代理模式**：客户端只与服务器通信，服务器代理 artifact 上传/下载。

MiniMLflow 只实现“本地 SQLite + 本地目录 + 单 token”的最小服务器模式。映射关系如下：

| 真实 MLflow | MiniMLflow | 简化点 |
|---|---|---|
| Tracking Server (Flask/Gunicorn) | `http.server.ThreadingHTTPServer` | 无线程池上限、无 gunicorn |
| SQLAlchemy + Postgres | 直接 SQLite + SQL | 无迁移框架、无连接池 |
| S3/GCS/Azure | 本地文件系统 | 无分片上传、无预签名 URL |
| Artifact service | `/artifacts/*` JSON/base64 | 全量缓冲、33% 体积开销 |
| Auth (basic/OIDC) | 单一 Bearer Token | 无 RBAC/租户 |
| Model Registry | 自建表 | 兼容核心语义，不兼容全部 REST 字段 |
| Tracing | 自建 span 表 | 无采样、异步批处理、自动埋点 |
| Webhooks | `webhooks.py` + outbox 表 | 单进程、同步投递、无 secret 加密 |
| Evaluation Dataset | `datasets.py` + 两张表 | 追加语义、内存搜索；无 inputs 哈希去重/记录级更新 |
| Model Monitoring | `monitoring.py` + 两张表 | 批式评估、SQLite；无流式/标签 join/告警投递 |
| Agent Session/Eval | `agentops.py`（trace tag 派生） | 脚本模拟、确定性 scorer、批式采样；无 LLM 模拟用户/异步评分/独立 session 表 |
| RAG 检索/评测 | `rag.py`（应用侧胶水） | BM25、recall/nDCG、引用与词项 groundedness；无向量库/rerank/NLI judge |

## 2.5 一次日志请求的完整路径

以远程模式 `client.log_metric(run_id, "auc", 0.91, step=3)` 为例：

```text
1. Client 校验参数（类型、step），构造 JSON
2. POST /api/2.0/mlflow/runs/log-metric
3. HTTP Handler 鉴权、限制请求体、解析 JSON
4. TrackingService 读取 run，检查是否 RUNNING / 非 deleted
5. 校验 metric 名称、数值有限性、时间戳、step
6. Store 在事务中 INSERT metrics 一行
7. 返回 200 {}
8. 客户端把异常映射为稳定错误码
```

注意第 4 步：**校验发生在服务层，而不是只在客户端**。否则绕过 Python 客户端直接发 HTTP 就能写入非法数据。MiniMLflow 的 `server.py` 刻意保持“薄”，每个路由只做参数提取，业务规则集中在 `TrackingService`。

## 2.6 为什么用 SQLite 教学

SQLite 的优点：

- 零运维，单文件，容易观察 schema；
- 支持事务、外键、WAL、窗口函数；
- 足以演示并发读 + 单写者和软删除。

缺点：

- 单写者，写吞吐有限；
- 不适合多台应用服务器共享；
- 备份/恢复和在线 schema 迁移能力弱；
- `:memory:` 数据库在多线程下不是共享连接。

因此教程中的生产章节会明确要求：团队级部署换 Postgres/MySQL，并把 SQLite 仅用于本地开发、测试和单机演示。

## 2.7 分层与依赖方向

MiniMLflow 的依赖方向是单向的：

```text
HTTP / CLI / Fluent / UI
          ↓
     Client / Service
          ↓
   Store / Artifacts / Registry
          ↓
      Entities / Errors
```

约束：

- `entities` 不依赖存储或 HTTP；
- `store` 不含业务校验（只保证数据库不变量）；
- `server` 不直接写 SQL；
- `client` 不导入 `server`；
- `models` 通过 `client` 接口落盘/下载，不反向依赖具体存储。

这条依赖规则让测试可以只启动所需层：存储测试不需要 HTTP，模型测试不需要数据库（除非走 `runs:/`）。

## 2.8 与真实 MLflow 的差距

真实 MLflow 的架构还包含：

- 数据库迁移（`alembic`）与多版本兼容；
- Artifact Repository 抽象和云存储实现；
- 多后端 Tracking Store；
- 服务器端的批量日志、异步 trace 写入；
- 企业鉴权、审计、配额；
- 与 Databricks Unity Catalog / 云模型的集成；
- 更复杂的 UI（对比、图表、模型版本审批）。

MiniMLflow 用“一张 SQLite + 一个本地目录 + 一个 HTTP 进程”保留了架构关系，但没有复制运维复杂度。

## 小结

- MLflow 是控制面：数据库存元数据，对象存储存制品，Registry 存发布指针。
- 本地模式与服务器模式解决不同规模的问题；SQLite 只适合单机/教学。
- 元数据与制品分离带来一致性与 GC 问题，是平台工程的核心议题之一。
- MiniMLflow 的分层让你能定位每个问题的归属：协议、业务、存储还是制品。

## 练习

1. 画出“删除一个 run”在真实 MLflow 与 MiniMLflow 中分别影响哪些存储。
2. 如果 artifact 上传成功但数据库提交失败，你会如何对账？给出一个最小方案。
3. 为什么 Registry 不直接存模型文件？举出两个反例。

---

下一章：[第 3 章 概念模型](03-concepts.md)
