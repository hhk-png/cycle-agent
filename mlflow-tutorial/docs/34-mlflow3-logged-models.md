# 第 34 章 MLflow 3 迁移与 Logged Model：从 Run 中心到模型中心

## 学习目标

- 说清 MLflow 3 的数据模型变化：为什么“模型”从 run 的附属制品变成一等实体。
- 理解 LoggedModel、ModelVersion、RegisteredModel、Alias 四者的关系与 URI 形式。
- 掌握 2.x -> 3.x 的关键迁移项：`artifact_path` -> `name`、stages -> aliases、评分服务、序列化默认值。
- 设计一个可回滚的升级方案，而不是在生产上直接 `pip install --upgrade`。
- 把 Mini 的 Registry/模型 URI 映射到 3.x 概念，知道已实现什么、还缺什么（第 40 章给出实现）。

> 版本说明：MLflow 3.0 于 2025-06 发布。本章描述的是 3.x 的稳定方向；具体 API 以你锁定的版本文档和 `mlflow --help` 为准。Mini 已从纯 2.x 风格推进到模型中心：本章负责概念与迁移，第 40 章给出 LoggedModel 的可运行参考实现。

## 34.1 3.0 到底改了什么

把 breaking changes 按“对使用者的影响”排序，而不是按发布说明的字母序：

| 变化 | 影响 | 迁移动作 |
|---|---|---|
| 模型成为 LoggedModel | `log_model` 不再只是往 run 目录写文件 | 使用返回的 `ModelInfo.model_uri`/`model_id` |
| Registry stages 弃用 | `Staging/Production/Archived` 不再是推荐发布方式 | 改用 alias + `models:/name@alias` |
| 独立 deployments server 移除 | `mlflow deployments start-server` 不再可用 | 用 `mlflow models serve` 或平台网关 |
| Recipes 移除 | 旧 pipeline 代码失效 | 用 Projects/普通训练脚本/编排系统替代 |
| 部分 flavor 移除 | fastai/mleap/diviner/gluon 等 | 用 pyfunc/onnx/pytorch 包装 |
| 评分服务改为 FastAPI + uvicorn | WSGI 中间件、gunicorn 参数失效 | 用 `-w` workers、ASGI 方式扩展 |
| 文件存储不再是默认推荐 | `./mlruns` 适合本地，不适合团队 | 用数据库后端 + 对象存储 |

序列化默认值不是 3.0 的一次性变化：sklearn/LightGBM→skops、PyTorch→pt2 在 3.14 才成为默认，XGBoost 自 3.6 起默认 UBJSON。迁移时按自己锁定的版本做加载回归（见 34.5.2）。

一条常被忽略的兼容规则：**3.x 客户端/服务端可以读取 2.x 创建的资源，但 2.x 不能读取 3.x 的新资源**。所以升级顺序通常是“先升读取方，再升写入方”，并且客户端与服务端版本要匹配。

## 34.2 LoggedModel：模型成为一等实体

### 34.2.1 2.x 的模型是什么

在 2.x：

```text
run/<run_id>/artifacts/model/
  MLmodel
  model.pkl
  ...
```

模型是 run 的一个 artifact 目录。Registry 的 ModelVersion 通过 `source = runs:/<run_id>/model` 指向它。

这带来几个结构性问题：

- 模型只能依附于 run；没有 run 就没有模型实体；
- 同一个 run 里多个模型只能靠 artifact path 区分；
- 模型级参数/指标/数据集没有独立命名空间；
- 查找“这个实验里所有未注册的模型”只能扫描 artifact 目录；
- 模型文件出现在 run 的 artifact 列表里，和日志、图片混在一起。

### 34.2.2 3.x 的 LoggedModel

3.x 的 `log_model()` 创建一个 **LoggedModel**：

```text
models/<model_id>/artifacts/
  MLmodel
  ...
```

核心字段（来自 tracking service proto 与公开文档）：

| 字段 | 含义 |
|---|---|
| `model_id` | 模型实体 ID，形如 `m-...`（以实际版本为准） |
| `name` | 模型名称，在同一实验内可搜索 |
| `model_type` | 例如 `Classifier`、`LLM`、`Agent` |
| `source_run_id` | 创建它的 run；保留训练血缘 |
| `registrations` | 已注册到的 `(registered_model_name, version)` 列表 |
| params/metrics/tags/datasets | 模型级元数据，可独立于 run 记录 |

模型不再需要出现在 run 的 artifact 列表里。这既是 breaking change，也是设计目标：**模型有自己的人生周期，run 只是它的出生记录**。

### 34.2.3 新写法

```python
import mlflow

with mlflow.start_run() as run:
    model = train()
    info = mlflow.sklearn.log_model(model, name="churn")

print(info.model_id)     # LoggedModel ID
print(info.model_uri)    # models:/<model_id>

loaded = mlflow.sklearn.load_model(info.model_uri)
# 或者只有 model_id 时：
loaded = mlflow.sklearn.load_model(f"models:/{info.model_id}")
```

要点：

- `name=` 取代了 `artifact_path=`；两者同时传会报错；
- `log_model` 不强制要求 active run（仍可用 run 记录训练过程）；
- 模型 artifact 位于 `models/<model_id>/`，`MlflowClient.list_artifacts(run_id)` 看不到它；
- 旧的 `runs:/<run_id>/<name>` 仍可作为兼容桥，但 `<name>` 必须与 logged name 匹配；硬编码 `runs:/<run_id>/model` 在 3.x 可能失效。

## 34.3 四种“模型标识”不要混

| 概念 | 例子 | 用途 |
|---|---|---|
| LoggedModel ID | `models:/m-abc123` | 指向一次具体训练产物，未注册也可加载 |
| RegisteredModel 名称 | `churn` | 稳定的业务名称 |
| ModelVersion | `churn/3` 或 `models:/churn/3` | 某个注册版本的不可变快照 |
| Alias | `models:/churn@champion` | 可变的发布指针，部署系统应读它 |

关系：

```text
Run ──source_run_id──> LoggedModel ──register──> ModelVersion ──alias──> @champion
                              │                         │
                              └── model_id 字段 ────────┘
```

3.x 的 ModelVersion 记录 `model_id`，因此 Registry 可以反查“这个版本来自哪个 LoggedModel”，而不是只靠 `runs:/` 字符串猜。

Mini 的 `model_versions` 表只有 `source` 和 `run_id`，没有 `model_id`。这在 2.x 语义下是自洽的；要支持 3.x，需要：

1. 新增 `logged_models` 表（model_id、name、model_type、source_run_id、artifact_uri、tags、params、metrics）；
2. `log_model` 先写 LoggedModel，再写 artifact；
3. `model_versions` 增加 `model_id` 列；
4. `load_model` 支持 `models:/<model_id>`；
5. 搜索接口支持 `search_logged_models`。

这正是第 5 章“需求变更是架构变更”的案例：不是加一个字段，而是改主对象。

## 34.4 Stages -> Aliases：发布语义的迁移

### 34.4.1 为什么 stages 不够

Stage 是一组固定枚举：`Staging`、`Production`、`Archived`。它的问题是：

- 只能表达“一个模型在一个阶段”的粗粒度状态；
- 无法同时表达 `champion`、`challenger`、`shadow`、`canary`；
- 阶段迁移是覆盖式的，历史语义弱；
- 多环境、多租户时枚举不够用。

Alias 是 `(registered_model, alias) -> version` 的命名指针：

```python
client.set_registered_model_alias("churn", "champion", 3)
client.set_registered_model_alias("churn", "challenger", 4)
```

部署系统读 `models:/churn@champion`，不需要知道版本号；回滚就是把 alias 指回旧版本。

### 34.4.2 迁移动作

| 2.x | 3.x |
|---|---|
| `transition_model_version_stage(..., "Production")` | `set_registered_model_alias(..., "champion", v)` |
| `models:/churn/Production` | `models:/churn@champion` |
| `get_latest_versions(stage="Production")` | `get_model_version_by_alias("churn", "champion")` |
| 用 stage 做审批状态 | 用 tag/alias/外部审批系统 |

注意：`get_latest_versions` 和 `models:/name/latest` 在 3.x 对部署场景已不推荐，因为“最新”不等于“已批准”。

一个更严格的发布模式是**每个环境一个注册模型**：

```text
churn.dev      -> alias candidate
churn.staging  -> alias candidate
churn.prod     -> alias champion
```

晋升用 `copy_model_version` 复制版本，而不是把同一个模型的 alias 到处指。这样权限、审计和回滚边界更清晰（这是 Databricks 生态常见做法，开源版可用 Registry API 组合实现）。

## 34.5 评测与序列化的迁移

### 34.5.1 `mlflow.evaluate`

3.x 把通用评测收敛到 `mlflow.models.evaluate`，并用 `validate_evaluation_results` 做门禁；旧的顶层 `mlflow.evaluate` 仍在兼容层，但新代码应使用明确命名空间。

GenAI 评测则走 `mlflow.genai.evaluate`：

```python
import mlflow
from mlflow.genai.scorers import Correctness, RelevanceToQuery

result = mlflow.genai.evaluate(
    data=dataset,                 # 带 inputs/expectations 的记录
    predict_fn=my_app,
    scorers=[Correctness(), RelevanceToQuery()],
)
```

它和传统 `evaluate` 的区别：

- 数据是“输入 + 期望/评分标准”，不是单纯的 X/y；
- scorer 可以是确定性函数、LLM judge 或人工标注；
- 结果与 trace/assessment 关联，而不只是 metrics；
- 支持按 rubric、guideline、检索 groundedness 等维度评分。

更完整的真实 API 实战（tracing、prompt、scorer、judge、`@mlflow.test`）见第 39 章；把 LoggedModel 写成可运行代码的过程见第 40 章。

### 34.5.2 序列化默认值

默认序列化的变化不是 3.0 一次性发生的，而是分版本落地：

- **3.14**：`mlflow.sklearn` 与 `mlflow.lightgbm` 默认从 cloudpickle 改为 **skops**；`mlflow.pytorch` 默认从 pickle 改为 **pt2**（torch.export）；
- **3.6**：`mlflow.xgboost` 默认写 **UBJSON**；
- 更早的版本已经支持无 pickle 路径，但默认值不一定切换。

这不是“内部实现细节”。它影响：

- 加载端是否需要额外依赖；
- 旧模型能否被新运行时加载；
- 安全边界（pickle 风险降低，但不是所有 flavor 都安全）；
- 跨版本复现。

迁移时必须做**加载回归**：把生产在用的模型版本逐个在新环境加载并跑一组 golden inputs，而不是只看训练脚本能跑。

### 34.5.3 评分服务

3.x 的 `mlflow models serve` 使用 FastAPI + uvicorn。版本要点：

- MLServer 作为 pyfunc 后端在 **3.13** 被移除，`--enable-mlserver`/`enable_mlserver` 不再可用；
- 旧的 Flask/gunicorn scoring server 已被 FastAPI/uvicorn 取代；
- worker 数用 `-w`/`MLFLOW_MODELS_WORKERS`；
- `/invocations`、`/ping`、`/health`、`/version` 仍是主要端点；OSS 的 `/ping` 与 `/health` 都是“模型是否加载”的探针，不构成独立的 liveness/readiness 两层语义。

Mini 的 `scoring.py` 是一个 `ThreadingHTTPServer`，只实现契约，不模拟 ASGI。第 24 章的生产部署建议仍然适用：真正上线应使用模型服务框架/网关，而不是把教学服务器暴露出去。

## 34.6 一个可回滚的升级方案

### 34.6.1 升级前

1. **盘点**：列出所有客户端版本、服务端版本、flavor、序列化格式、评分服务镜像。
2. **备份**：数据库 + artifact 存储 + 配置 + 密钥引用；记录 RPO/RTO。
3. **建立基线**：用当前版本跑一组关键 run、模型加载、评测、trace 查询，保存输出。
4. **锁定版本**：`mlflow==3.x.y`、客户端与服务端同 minor；不要用浮动 `latest`。
5. **隔离环境**：在 staging 用生产数据副本升级，不直接动生产。

### 34.6.2 升级顺序

```text
备份 -> 升级只读副本/报表 -> 升级服务端 -> 升级客户端（先读后写）
     -> 跑 golden 回归 -> 切换流量 -> 观察 -> 保留回滚窗口
```

为什么先服务端后客户端：3.x 服务端能读 2.x 资源，3.x 客户端也能读旧资源；反过来不一定成立。先让“读”路径适应，再让“写”路径产生新格式。

### 34.6.3 回滚条件

提前写清楚，而不是出事再讨论：

- 模型加载失败率超过阈值；
- 评测结果与基线偏差超过阈值；
- trace/assessment 写入丢失；
- 评分服务 P99 延迟或错误率恶化；
- 客户端出现不可接受的 deprecation 错误。

回滚不是“把包降级”这么简单：如果新版本已经写入 LoggedModel 或新序列化格式，旧版本可能读不了。所以回滚窗口内应避免不可逆写入，或保留双写/导出。

### 34.6.4 迁移检查表

- [ ] `log_model` 的 `artifact_path` 全部改为 `name`，或确认兼容路径；
- [ ] 所有 `runs:/<run_id>/model` 硬编码改为使用返回的 `model_uri`；
- [ ] stage 相关代码改为 alias；
- [ ] 部署配置从 `models:/name/Production` 改为 `models:/name@champion`；
- [ ] 评测入口改为 `mlflow.models.evaluate` / `mlflow.genai.evaluate`；
- [ ] 模型加载回归覆盖所有生产 flavor；
- [ ] 评分服务镜像与启动参数适配 FastAPI/uvicorn；
- [ ] 备份与回滚演练完成；
- [ ] 监控能区分 2.x/3.x 客户端与服务端错误。

## 34.7 Mini 的映射与缺口

| MLflow 3 概念 | Mini 现状（第 40 章） | 仍缺什么 |
|---|---|---|
| LoggedModel | 四张表、实体、三状态机、`log_model(name=...)` | `registrations` 反查、模型别名、数据集关联 |
| ModelVersion.model_id | Registry 只存 `source`/`run_id` | 增加 `model_id` 列与外键；保留 source 兼容 |
| `models:/<model_id>` | 本地后端可加载；Registry source 可指向它 | 远程制品下载端点 |
| `search_logged_models` | attributes/params/metrics，AND-only，分页 | tags 过滤、更多属性、跨 10 个实验 |
| alias | 已实现（Registry 级别） | LoggedModel 级别别名 |
| stages | 已实现（兼容） | 保留但标记 deprecated，文档引导 alias |
| Assessment | 已实现最小集 | 增加 overrides/valid 语义与更多 source |
| Prompt Registry | 已实现最小集 | 增加 chat-style message 模板与权限 |
| FastAPI 评分服务 | `ThreadingHTTPServer` | 教学够用；生产换 ASGI 服务 |

一个仍然有价值的练习：把 Mini 的 `model_versions` 增加 `model_id` 列，并实现“从版本反查 LoggedModel”的 API。第 40 章已经完成了正向链路（模型 -> 注册版本），反向链路需要额外处理历史版本、删除模型和 source 为 `runs:/` 的混合状态，正好体验 3.x 迁移的真实成本。

## 34.8 常见迁移事故

1. **模型“消失”**：升级后 run 的 artifact 列表里看不到模型，误以为丢失。实际在 LoggedModel 页面/`models/<model_id>/`。
2. **硬编码 `runs:/.../model`**：旧代码在 3.x 下找不到模型；应使用 `info.model_uri`。
3. **stage 脚本失效**：`models:/name/Production` 不再可靠；改 alias。
4. **序列化不兼容**：旧镜像加载新模型失败；固定版本并做加载回归。
5. **客户端/服务端版本错配**：新客户端写的新字段旧服务端不认识；同 minor 匹配。
6. **回滚失败**：新格式已写入，旧版本读不了；升级前设计不可逆边界。

## 小结

- MLflow 3 的核心不是“多了几个 GenAI API”，而是模型成为一等实体：LoggedModel 有独立 ID、血缘、元数据和注册关系。
- 四种模型标识（model_id、name、version、alias）各有用途，部署应读 alias。
- `artifact_path` -> `name`、stage -> alias、Flask -> FastAPI、序列化默认值变化，是迁移的四个高频坑。
- 升级要按“备份 -> 只读副本 -> 服务端 -> 客户端 -> 回归 -> 切换 -> 回滚窗口”执行。
- Mini 已经实现 LoggedModel 的核心闭环；第 40 章给出表结构、状态机、搜索方言、`models:/<id>` 加载与测试，第 34.7 节列出与真实平台的剩余差距。

## 练习

1. 画出你的生产系统里从 run 到部署的引用链，标出哪些是 `runs:/`、哪些是 `models:/name/version`、哪些是 alias。
2. 写一个迁移脚本，把 registry 里的 stage 映射为 alias，并处理一个模型同时有多个 stage 版本的情况。
3. 设计 `logged_models` 表的 DDL，要求支持按实验、name、model_type、source_run_id 搜索，并说明索引。
4. 解释为什么“3.x 能读 2.x 资源，2.x 不能读 3.x 资源”会导致升级顺序必须是“先读后写”。
5. 选一个生产 flavor，列出 3.x 序列化变化后必须回归的 10 个输入。
6. 设计一个回滚演练：在不丢失新写入数据的前提下，把服务端回退到上一个 minor。

---

下一章：[第 35 章 真实 MLflow 源码导读](35-real-mlflow-source-guide.md)
