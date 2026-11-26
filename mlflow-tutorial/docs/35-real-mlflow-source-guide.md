# 第 35 章 真实 MLflow 源码导读：从 CLI 到存储适配器

## 学习目标

- 建立真实 MLflow 仓库的“地图”：知道一个问题该去哪个目录找答案。
- 顺着一次 `log_metric` 调用，走完 fluent -> client -> store -> SQL 的完整链路。
- 理解 tracking store、artifact repository、model registry store、flavor、plugin 五个扩展点。
- 学会用测试、proto、CLI 三类证据核对行为，而不是依赖博客和记忆。
- 把 Mini 的模块与真实 MLflow 的类做映射，知道哪些设计是等价的、哪些是简化。

> 说明：本章按 MLflow 3.x 的公开仓库结构描述。文件路径可能随版本调整；阅读时以你锁定版本的源码为准。3.3 起 tracking server 默认使用 FastAPI + uvicorn（保留 Flask 兼容路径），评分服务也在 3.x 转向 FastAPI/uvicorn；不要把旧版 Flask/gunicorn 的结论套到 3.18。

## 35.1 仓库地图

先看顶层，不要一头扎进 `mlflow/store`：

```text
mlflow/
  __init__.py              # 对外 fluent API 的聚合导出
  tracking/
    fluent.py              # start_run / log_metric / active run
    client.py              # MlflowClient：显式、无全局状态的 API
    _tracking_service/     # URI -> store 的解析与注册
  store/
    tracking/              # 实验/run/metric 的存储抽象与实现
    model_registry/        # 注册表存储
    artifact/              # artifact repository 抽象与各后端
  models/
    model.py               # MLmodel 格式、save/load、flavor 调度
    signature.py           # 输入输出签名
    evaluation.py          # 通用评测
  pyfunc/
    __init__.py            # python_function flavor、PyFuncModel
    scoring_server/        # /invocations 服务（3.x FastAPI）
  server/
    __init__.py            # 跟踪服务端应用
    handlers.py            # REST handler
  cli/                     # click 命令
  genai/                   # prompt / tracing / evaluation / judges
  projects/                # MLproject 执行
  protos/service.proto     # REST 的协议定义（重要证据）
tests/
  tracking/ store/ models/ server/ pyfunc/ genai/ ...
```

几个判断：

- **`mlflow/__init__.py` 是便利层，不是实现层**。看到 `mlflow.log_metric` 时，真正逻辑在 `tracking/fluent.py`。
- **`store/` 是持久化边界**。业务规则不应下沉到 store；store 负责“怎么存/取”，不负责“能不能”。
- **`protos/service.proto` 是协议事实来源**。字段名、必填/可选、枚举值以它为准，比文档措辞可靠。
- **`genai/` 是 3.x 增长最快的区域**，但传统 tracking 仍然是底座。

## 35.2 一次 `log_metric` 的旅程

### 35.2.1 Fluent 层

```python
import mlflow
with mlflow.start_run():
    mlflow.log_metric("loss", 0.42, step=3)
```

`mlflow.log_metric` 来自 `tracking/fluent.py`。它做四件事：

1. 从 context 里取 active run；
2. 解析 tracking URI / client；
3. 组装 `Metric` 对象（key、value、timestamp、step）；
4. 调用 client 的 `log_metric`。

active run 的管理是 fluent 层最容易被误解的部分：它不是一个简单的全局变量，而是要考虑嵌套 run、线程/协程上下文、子进程恢复。Mini 用 `ContextVar` 模拟了这一点；真实实现还兼容更多历史行为。

### 35.2.2 Client 层

`tracking/client.py` 的 `MlflowClient` 是“显式 API”：

```python
client = MlflowClient(tracking_uri="http://localhost:5000")
client.log_metric(run_id, "loss", 0.42, step=3)
```

它不依赖 active run，适合服务端代码、批处理、测试。内部会根据 URI 选择：

- 本地文件存储：直接调用 `FileStore`；
- 数据库/HTTP：通过 REST 调用 tracking server；
- 其他后端：通过 plugin 解析。

这一层负责参数校验、错误映射、重试/超时（取决于实现），并把 REST 的 JSON 与 Python 对象互转。

### 35.2.3 Store 层

`store/tracking/abstract_store.py` 定义接口，典型方法：

```text
create_experiment / get_experiment / search_experiments
create_run / get_run / update_run_info / search_runs
log_metric / log_batch / get_metric_history
set_tag / delete_tag / log_input
```

两个主要实现：

| 实现 | 适用 | 特点 |
|---|---|---|
| `FileStore` | 本地 `./mlruns` | 目录结构可读；并发/查询能力弱 |
| `SqlAlchemyStore` | SQLite/Postgres/MySQL/MSSQL | 事务、索引、搜索；生产主流 |

Mini 的 `SqliteStore` 对应 `SqlAlchemyStore` 的一个极小子集：只支持 SQLite，SQL 手写，搜索 DSL 自己编译。真实实现用 SQLAlchemy 统一方言，并把搜索过滤编译成 SQLAlchemy 表达式。

### 35.2.4 服务端与协议

走 HTTP 时：

```text
client.log_metric
  -> POST /api/2.0/mlflow/runs/log-metric
  -> mlflow/server/handlers.py 的 handler
  -> store.log_metric
```

`handlers.py` 的职责很薄：解析请求、转换 proto/JSON、调用 store、序列化响应、映射错误码。业务校验应尽量在 store 或 client 层，而不是散落在 handler 里。Mini 的 `server.py` 遵循同样的原则：每个路由只调用一次 service。

`protos/service.proto` 里能看到请求/响应消息的精确定义。例如 `LogMetric` 的字段、`RunInfo` 的枚举、`ModelVersion` 的 `model_id`。**当文档与代码不一致时，proto + store + tests 三者交叉验证。**

## 35.3 扩展点一：Tracking Store

真实 MLflow 允许你注册自定义 tracking store：

```toml
[project.entry-points."mlflow.tracking_store"]
my_store = "my_package.store:MyStore"
```

规则：

- entry point 的**名字**就是 URI scheme，例如 `my_store://...`；
- 类要实现 `AbstractStore` 的接口；
- MLflow 通过 registry 发现并实例化；
- 完整 URI 会传给构造函数。

这解释了为什么 MLflow 能支持“数据库、文件、HTTP、云服务”而不改核心：**URI scheme 是插件键**。

Mini 没有 plugin 系统，但 `MiniConfig._sqlite_path` 明确拒绝非 SQLite scheme，并提示“实现一个 SqliteStore 兼容适配器”。这是同一个扩展点的手工版本。

## 35.4 扩展点二：Artifact Repository

`store/artifact/` 下每个后端一个类：

```text
artifact_repository.py          # 抽象接口
artifact_repository_registry.py # scheme -> 类
local_artifact_repo.py
s3_artifact_repo.py
gcs_artifact_repo.py
azure_blob_artifact_repo.py
...
```

接口的核心方法：

```text
log_artifact(local_path, artifact_path)
log_artifacts(local_dir, artifact_path)
list_artifacts(path)
download_artifacts(path, dst_path)
delete_artifacts(path)
```

设计要点：

- **元数据与字节分离**：tracking store 存 URI，artifact repo 存字节；
- **路径语义统一**：所有后端都接受相对路径，拒绝逃逸；
- **凭据来自环境/实例角色**，不写进 URI（虽然历史上有些写法）；
- **代理模式**：客户端可以把 artifact 上传/下载代理给 tracking server，避免把对象存储凭据发给每个客户端。

Mini 的 `ArtifactRepository` 是这个接口的本地文件子集，额外实现了原子写、路径 containment、Windows 保留名拒绝。

## 35.5 扩展点三：Model Registry Store

`store/model_registry/` 与 tracking store 平行：

```text
abstract_store.py
sqlalchemy_store.py
```

它管理 `RegisteredModel`、`ModelVersion`、tags、aliases。关键不变量：

- `(name, version)` 唯一且版本号单调；
- alias 指向存在的版本；
- 删除注册模型时对版本的处理策略；
- 3.x 的 ModelVersion 关联 `model_id`（LoggedModel）。

Mini 的 `registry.py` + `sqlite_store.py` 实现了同样的规则，只是没有 SQLAlchemy；LoggedModel 的参考实现见第 40 章，Registry 仍未记录 `model_id` 反向关系。

## 35.6 扩展点四：Flavor 与 PyFunc

模型保存/加载的主线：

```text
mlflow/models/model.py       # MLmodel 文件、save/load 调度
mlflow/pyfunc/__init__.py    # python_function 契约
mlflow/models/signature.py   # ModelSignature
mlflow/<flavor>/             # sklearn、pytorch、transformers...
```

`MLmodel` 是一个 YAML：

```yaml
flavors:
  python_function:
    loader_module: mlflow.sklearn
    model_path: model.pkl
    env: ...
  sklearn:
    ...
signature:
  inputs: ...
  outputs: ...
run_id: ...
```

加载时：

1. 读 `MLmodel`；
2. 选 flavor（通常 `python_function`）；
3. 解析依赖环境（virtualenv/conda/uv）；
4. 调用对应 loader；
5. 用 signature 做输入校验（如果启用）。

安全重点：**loader 是代码执行入口**。真实 MLflow 支持多种序列化格式，不同格式的风险不同；Mini 只允许白名单 loader + JSON 模型，是刻意收紧。生产不要加载来源不明的模型，即使它是“官方 flavor”。

## 35.7 扩展点五：插件系统总表

官方文档列出的主要 entry point group：

| 插件类型 | entry point group | 典型实现 |
|---|---|---|
| Tracking store | `mlflow.tracking_store` | `FileStore`、`SqlAlchemyStore` |
| Artifact repository | `mlflow.artifact_repository` | `LocalArtifactRepository`、S3/GCS/Azure |
| Model registry store | `mlflow.model_registry_store` | `SqlAlchemyStore` |
| Run context provider | `mlflow.run_context_provider` | 采集 git/用户/环境上下文 |
| 认证/请求头 provider | 以版本文档为准 | 企业 SSO、网关注入 |

注意：

- 第三方教程常写错 group 名（例如 `mlflow.artifact_stores`），以官方 `plugins` 文档和仓库 `pyproject.toml`/`setup.py` 为准；
- 修改 entry points 后要重新 `pip install -e .`，否则元数据不刷新；
- plugin 的构造函数、异常类型、线程安全要求要以抽象基类为准；
- 不是所有组件都可插件化（例如某些 job store 仍是硬编码）。

## 35.8 GenAI 相关源码

3.x 的 `mlflow/genai/` 值得单独看：

```text
genai/
  __init__.py          # register_prompt / load_prompt / evaluate 等导出
  prompts/             # Prompt 实体、模板、URI 解析（3.1+ 支持 chat template）
  tracing/             # trace/span、OTel 导出、处理器
  scorers/             # 内置 scorer
  judges/              # LLM-as-judge、make_judge、judge 优化
  datasets/            # 评测数据集
  labeling/            # 人工标注 session；3.14+ Review Queue
  mcp_registry/        # 3.15+ MCP server 版本/别名/工具快照（实验）
```

平台治理相关代码（工作区、RBAC）在源码中分散于 server、store 与 CLI，不能只读 `genai/`；以仓库当前目录和 `mlflow/server/` 为准。

Webhooks 是 Registry/Prompt 的另一个扩展点：3.3+ 的 OSS 实现在 `mlflow/webhooks/` 附近，事件定义与 payload 可由 protobuf/测试交叉验证。阅读时重点看三处：事件在哪个 store 方法后发出、delivery 记录何时落库、签名内容如何拼接（`delivery_id.timestamp.payload`）。Mini 的 `webhooks.py` 把这三件事压缩到一个文件，便于对照。

阅读顺序建议：

1. `mlflow/genai/__init__.py`：看公开 API 面；
2. prompt 实体与 `load_prompt`：看 URI 与版本解析；
3. trace 数据模型与 exporter：看 span/assessment 如何落库；
4. `scorers/` 与 `judges/`：看确定性指标与模型评分的边界；
5. `evaluate`：看数据、predict_fn、scorer 如何组装成 evaluation run。

Mini 的 `prompts.py`、`tracing.py`、`evaluate.py`、`gateway.py` 是这条链路的简化版；对照阅读能快速建立“哪些是核心抽象，哪些是平台特性”的判断。

## 35.9 测试是行为文档

真实仓库的测试目录本身就是最好的文档：

```bash
# 只跑存储层
pytest tests/store/tracking/test_sqlalchemy_store.py -q

# 看某个 API 的行为
pytest tests/tracking/test_client.py -q -k metric

# 收集所有测试名，找你要验证的行为
pytest --collect-only -q | grep -i alias
```

阅读测试时关注：

- fixture 如何构造 store（临时 SQLite/Postgres）；
- 边界条件（重复参数、删除后恢复、并发）；
- 错误类型与错误码；
- 客户端/服务端契约测试是否成对出现。

一个高效的学习方法：**先写一个失败的测试，再顺着调用栈找实现**。这比从 `__init__.py` 逐行读快得多。

## 35.10 调试入口

```bash
mlflow doctor                          # 环境、依赖、连接诊断
mlflow server \
  --backend-store-uri sqlite:///mlflow.db \
  --default-artifact-root ./mlartifacts \
  --host 127.0.0.1 --port 5000
mlflow db upgrade sqlite:///mlflow.db  # 数据库迁移（按版本）
```

Python 侧：

```python
from mlflow.tracking import MlflowClient
client = MlflowClient("http://127.0.0.1:5000")
print(client.get_run(run_id))
print(client.search_model_versions("name = 'churn'"))
```

排查顺序：

1. **URI 解析**：`MLFLOW_TRACKING_URI` 是否指向你以为的后端？
2. **客户端/服务端版本**：是否同 minor？
3. **store 实现**：是 FileStore 还是 SqlAlchemyStore？
4. **artifact 位置**：3.x 模型在 `models/<model_id>/`，不在 run artifact 里；
5. **权限/网络**：对象存储凭据、代理、TLS；
6. **数据库迁移**：schema 版本是否匹配。

## 35.11 Mini 与真实的类映射

| Mini | 真实 MLflow | 说明 |
|---|---|---|
| `fluent.py` | `tracking/fluent.py` | active run 用 ContextVar |
| `client.py` | `tracking/client.py` | 本地/远端双后端 |
| `tracking.py` | `TrackingService` + handlers | 用例层 |
| `registry.py` | `ModelRegistry` + registry store | 版本/别名规则 |
| `store/sqlite_store.py` | `store/tracking/sqlalchemy_store.py` | SQL 手写 vs SQLAlchemy |
| `store/schema.sql` | SQLAlchemy models + migrations | 无 Alembic |
| `artifacts.py` | `store/artifact/*` | 本地文件子集 |
| `models/model.py` | `models/model.py` + flavors | 单 flavor |
| `server.py` | `server/handlers.py` + app（3.3+ 默认 FastAPI/uvicorn） | 无 ASGI/多 worker/Flask 兼容层 |
| `ui.py` | 前端应用 | 服务端渲染 |
| `evaluate.py` | `models/evaluation.py` + `genai` | 基础指标 |
| `tracing.py` | `tracing/` + OTel | 手动埋点 |
| `prompts.py` | `genai/prompts/` | 文本模板子集 |
| `webhooks.py` | `mlflow/webhooks/` + tracking store | outbox/签名/重试子集 |
| `gateway.py` | 3.9+ tracking server 内嵌的 Gateway | 教学控制面（独立组件） |

## 35.12 贡献路径

如果你想给真实 MLflow 提交代码：

1. **先读 `CONTRIBUTING.md`**，确认分支、DCO、测试要求；
2. **从小处开始**：文档、测试、错误信息、单个 store 方法的边界；
3. **不要改 wire format**：proto 字段、错误码、URI 语义是兼容性承诺；
4. **同时改测试**：存储层改动要有 SQLite/Postgres 覆盖；
5. **跑 lint/format**：按仓库配置（ruff/black 等）；
6. **写清迁移影响**：breaking change 需要 release note 与迁移指南。

一个适合入门的 PR：给某个错误信息补充上下文、给 search 增加一个受支持的字段、为某个 flavor 增加加载回归测试。小而正确，比大而模糊更容易被合并。

## 小结

- 真实 MLflow 的分层与 Mini 同构：fluent -> client -> service -> store -> 数据库/对象存储。
- 五个扩展点：tracking store、artifact repository、registry store、flavor、plugin；URI scheme 是插件键。
- `protos/service.proto`、store 实现、测试是行为的三重证据；不要只信博客。
- 3.x 的模型在 LoggedModel 下，artifact 位置与 URI 语义都变了；读源码时要区分 tracking 与 scoring 两套服务栈。
- 读源码的最高效方式：带着一个具体问题，写失败测试，顺调用栈定位。

## 练习

1. 在真实仓库里找到 `log_metric` 从 fluent 到 SQL 的完整调用链，列出每一层的文件与函数。
2. 选一个 artifact 后端（S3 或 GCS），说明它如何解析凭据、如何防止路径逃逸。
3. 找到 `ModelVersion` 的 proto 定义，列出 3.x 新增字段，并解释它们与 LoggedModel 的关系。
4. 写一个最小 tracking store 插件，支持 `memory://`，只实现 create/get experiment 与 create/get run。
5. 对比 `FileStore` 与 `SqlAlchemyStore` 的 `search_runs` 实现，说明为什么前者的搜索能力受限。
6. 找一个你关心的 bug，先写失败测试，再定位到具体 store 方法；记录你的调用栈。

---

下一章：[第 36 章 生产运维手册](36-production-runbook.md)
