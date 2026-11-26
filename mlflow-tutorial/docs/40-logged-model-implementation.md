# 第 40 章 LoggedModel 参考实现：把 MiniMLflow 推进到模型中心

## 学习目标

- 把第 34 章的概念落成可运行的 schema、实体、服务、REST、客户端与 CLI。
- 说清 `PENDING → READY / UPLOAD_FAILED` 状态机与“先写元数据还是先写制品”的失败窗口。
- 实现一个只接受 `attributes` / `params` / `metrics` 的搜索方言，并解释为什么它和 run 搜索不同。
- 让 `models:/<model_id>` 真正可加载，同时保留 Registry 的 `source = models:/<model_id>` 血缘。
- 用 11 个测试覆盖不可变参数、终态、软删除、分页、REST 契约与模型加载。
- 诚实列出教学实现与 MLflow 3.18 的差距：注册关系、数据集、别名、远程制品下载、GC 与 UI。

> 本章不是第 34 章的重复。第 34 章回答“为什么要迁移”；本章回答“如果今天从零写一个模型中心，代码应该长什么样”。所有代码都在 `mini_mlflow/` 中，所有结论都能用 `python -m pytest -q` 复现。

## 40.1 为什么单独造一个“模型实体”

在 2.x 里，模型是 run 目录下的一堆文件：

```text
runs/<run_id>/artifacts/model/
  MLmodel
  model.json
```

Registry 的版本通过 `source = runs:/<run_id>/model` 指向它。这个模型没有自己的 ID，也没有自己的生命周期：删除 run 会连带删除制品，模型级参数只能借用 run 的参数，想找“这个实验里所有模型”只能扫 artifact 目录。

3.x 把模型提升为一等实体后，问题变成另一组：

- 模型有独立 ID，`models:/<model_id>` 可以直接加载，不需要先注册；
- 模型可以有自己的 params/metrics/tags，不必污染 run；
- `source_run_id` 只是血缘，run 结束后模型仍然存在；
- 搜索可以按 `model_type`、`status`、模型级指标进行；
- Registry 版本可以记录 `model_id`，从部署版本反查训练产物。

Mini 的实现保留了这五个性质，但刻意不做多工作区、RBAC、数据集关联和远程制品下载。下面从表结构开始。

## 40.2 数据模型：四张表与三条不变量

`mini_mlflow/store/schema.sql` 新增了四张表：

```sql
CREATE TABLE logged_models (
    model_id               TEXT PRIMARY KEY,
    experiment_id          INTEGER NOT NULL,
    name                   TEXT NOT NULL,
    model_type             TEXT NOT NULL DEFAULT '',
    source_run_id          TEXT,
    status                 TEXT NOT NULL DEFAULT 'LOGGED_MODEL_PENDING'
                               CHECK (status IN ('LOGGED_MODEL_PENDING',
                                                 'LOGGED_MODEL_READY',
                                                 'LOGGED_MODEL_UPLOAD_FAILED')),
    status_message         TEXT NOT NULL DEFAULT '',
    artifact_location      TEXT NOT NULL,
    creation_timestamp     INTEGER NOT NULL,
    last_updated_timestamp INTEGER NOT NULL,
    FOREIGN KEY (experiment_id) REFERENCES experiments(experiment_id) ON DELETE CASCADE,
    FOREIGN KEY (source_run_id) REFERENCES runs(run_id) ON DELETE SET NULL
);

CREATE TABLE logged_model_tags   (model_id, key, value, PRIMARY KEY (model_id, key), ...);
CREATE TABLE logged_model_params (model_id, key, value, PRIMARY KEY (model_id, key), ...);
CREATE TABLE logged_model_metrics(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model_id TEXT NOT NULL,
    key TEXT NOT NULL,
    value REAL NOT NULL,
    timestamp INTEGER NOT NULL,
    step INTEGER NOT NULL DEFAULT 0,
    ...
);
```

三条关键不变量：

1. **实验是所有者，run 只是来源。** `experiment_id` 用 `ON DELETE CASCADE`：实验被物理删除时模型元数据一起消失；`source_run_id` 用 `ON DELETE SET NULL`：run 消失不能带走模型。
2. **ID 即身份。** 没有 `(name, version)` 命名空间。同名模型可以有多个，靠 `model_id` 区分；Registry 才负责稳定名称与版本。
3. **参数不可变，标签可变，指标追加。** 参数表的主键是 `(model_id, key)`，服务层在写入不同值时拒绝；标签用 upsert；指标表保留完整历史，读取时取“最高 step，再取最新 timestamp”。

第 3 条里“最高 step”是 3.18 的行为变化。很多实现会按 timestamp 取最新值，但训练脚本可能补写低 step 的指标；按 step 优先能避免“补写的历史点覆盖了最新进度”。Mini 的 SQL 与读取都遵循这个顺序：

```sql
ROW_NUMBER() OVER (
    PARTITION BY model_id, key
    ORDER BY step DESC, timestamp DESC, id DESC
) AS rn
```

### 状态为什么用 protobuf 拼写

数据库里存的是 `LOGGED_MODEL_PENDING`、`LOGGED_MODEL_READY`、`LOGGED_MODEL_UPLOAD_FAILED`，而不是 Python 枚举的 `PENDING/READY/FAILED`。原因是 REST 的 finalize 请求体使用前者：

```http
PATCH /api/2.0/mlflow/logged-models/m-...
{"status": "LOGGED_MODEL_READY"}
```

Mini 在 `entities.normalize_logged_model_status` 里同时接受短别名：

| 输入 | 落库值 |
|---|---|
| `READY` / `LOGGED_MODEL_READY` | `LOGGED_MODEL_READY` |
| `PENDING` / `LOGGED_MODEL_PENDING` | `LOGGED_MODEL_PENDING` |
| `FAILED` / `UPLOAD_FAILED` / `LOGGED_MODEL_UPLOAD_FAILED` | `LOGGED_MODEL_UPLOAD_FAILED` |

这看起来是小事，实际是互操作问题：如果服务端只认一种拼写，客户端升级后就会收到 400，而错误信息往往只说“invalid status”。

## 40.3 生命周期：三个状态、两个失败窗口

```text
create ──> LOGGED_MODEL_PENDING
                │
                ├── finalize(READY)             ──> LOGGED_MODEL_READY
                └── finalize(UPLOAD_FAILED)     ──> LOGGED_MODEL_UPLOAD_FAILED
```

Mini 把“终态不可再改、同状态重复 finalize 幂等”作为自己的强化约束；真实 MLflow 3.x 的 finalize 实现更宽松，可能允许覆盖终态（第 40.10 节列出差异）。存储层用一次条件更新完成检查：

```python
row = conn.execute("SELECT status FROM logged_models WHERE model_id=?", ...).fetchone()
if row["status"] != status and row["status"] != "LOGGED_MODEL_PENDING":
    raise InvalidParameter("already finalized")
```

这不是“多此一举”的防御。考虑两个客户端同时 finalize 同一个模型：一个写 READY，一个写 FAILED。如果没有条件更新，后到的会覆盖先到的，模型状态取决于调度顺序。Mini 的规则是“第一个终态获胜”，与 run 的 `end_run` 一致。真实 MLflow 是否提供同样的并发保证取决于版本与 store；不要把 Mini 的规则当成跨实现的契约。

### 失败窗口一：元数据已写，制品未写

`log_model(name=...)` 的顺序是：

```text
create_logged_model(PENDING)
  -> save_model(temp dir)
  -> copy temp dir into artifact_location
  -> finalize(READY)
```

如果进程在 copy 中途崩溃，数据库里留下一个 PENDING 模型，artifact 目录可能是半成品。Mini 用 `except BaseException` 把模型标成 `UPLOAD_FAILED`，但进程被 kill 时连这个补偿也执行不了。生产系统需要：

- 启动时的 orphan 扫描：PENDING 超过 N 分钟且没有心跳，转 FAILED；
- 制品写入使用临时目录 + 原子 rename，最终目录要么完整要么不存在；
- 加载端拒绝 PENDING/FAILED 的模型，而不是“能读到文件就加载”（Mini 已在 `_load_logged_model` 中按 status 拒绝）。

### 失败窗口二：制品已写，元数据未提交

如果先写制品再写数据库，崩溃会留下“孤儿目录”。它不危险，但会浪费存储。Mini 选择“先元数据后制品”，因为孤儿元数据可以被状态机识别，孤儿目录只能靠 GC 扫描。两种顺序都需要对账，关键是明确哪一种是“可解释的失败”。

## 40.4 写路径：`log_model(name=...)` 到底做了什么

先看调用方：

```python
with mlflow.start_run() as run:
    model = train()
    info = mlflow.log_model(
        model,
        name="churn",
        model_type="Classifier",
        params={"alpha": 0.1},
    )

print(info.model_id)   # m-...
print(info.model_uri)  # models:/m-...
```

Mini 的 `TrackingService.log_model` 在 `name is not None` 时转入 `_log_logged_model`：

1. `create_logged_model` 生成 `m-<32 hex>`，默认制品目录是
   `<experiment artifact_location>/models/<model_id>/`；
2. `save_model` 在临时目录写出 `MLmodel`、`model.json`、签名和输入示例；
3. `ArtifactRepository.log_artifacts` 把临时目录复制到模型制品目录；
4. finalize 为 READY；
5. 如果传了 `registered_model_name`，创建 ModelVersion，`source = models:/<model_id>`。

三个容易忽略的细节：

- **模型目录不在 run 的 artifact 列表里。** `list_artifacts(run_id)` 看不到它，这是 3.x 的预期行为，不是 bug。
- **`artifact_path` 参数被忽略。** 3.x 用 `name` 取代了它；同时传两者在真实 MLflow 会报错，Mini 选择“name 优先”，并在文档里标注差异。
- **Registry 的 source 是 `models:/...`。** 因此 `load_model("models:/churn/1")` 会先解析到 LoggedModel，再加载它的制品目录；这条链路在第 40.7 节展开。

### 并发创建

`model_id` 是随机 32 hex，碰撞概率可以忽略；即使碰撞，主键冲突会抛 `ResourceAlreadyExists`。真正需要小心的是 `name`：Mini 不要求同一实验内唯一，因为真实 MLflow 也允许同名模型（它只是标签，不是主键）。如果你需要“同名只能有一个”，应该在业务层用 tag 或 Registry 表达，而不是给数据库加唯一索引。

### 制品路径安全

`artifact_location` 必须落在配置的 artifact root 内，由 `ArtifactRepository.validate_experiment_location` 统一检查。即使用户传入 `file:///etc/...`，也会被拒绝。这与第 10 章的路径穿越防护共用同一条边界。

## 40.5 REST 面：路径与线格式同构，语义有教学简化

Mini 的服务端路由位于 `mini_mlflow/server.py`。下表列出已实现的端点：

| 方法 | 路径 | 作用 |
|---|---|---|
| POST | `/api/2.0/mlflow/logged-models` | 创建（PENDING） |
| GET | `/api/2.0/mlflow/logged-models/{model_id}` | 读取 |
| PATCH | `/api/2.0/mlflow/logged-models/{model_id}` | finalize |
| DELETE | `/api/2.0/mlflow/logged-models/{model_id}` | 删除元数据 |
| POST | `/api/2.0/mlflow/logged-models/search` | 搜索 |
| PATCH | `/api/2.0/mlflow/logged-models/{model_id}/tags` | 批量设置标签 |
| DELETE | `/api/2.0/mlflow/logged-models/{model_id}/tags/{key}` | 删除标签 |
| POST | `/api/2.0/mlflow/logged-models/{model_id}/params` | 记录参数 |
| POST | `/api/2.0/mlflow/logged-models/{model_id}/metrics` | 记录指标 |

字段要按端点区分：

| 端点 | 请求体 |
|---|---|
| Create（POST） | `experiment_id`（必填）、`name`、`model_type`、`source_run_id`、`params`、`tags` |
| Finalize（PATCH） | `status`（必填）、`status_message` |
| Set tags（PATCH） | `tags`（list of `{key,value}`） |
| Log params（POST） | `params`（list of `{key,value}`） |
| Log metrics（POST） | `metrics`（list of `{key,value,timestamp,step}`） |
| Search（POST） | `experiment_ids`、`filter`、`order_by`、`max_results`、`page_token` |

`status` 不属于 Create；Mini 的 Create handler 也不读取它。Mini 额外接受 `artifact_location`（真实 Create 的公开字段集里没有它），这是教学实现的超集。响应统一包在 `{"model": {"info": ..., "data": ...}}` 中。

一个必须自己加的细节是 **PATCH 支持**。Mini 原来的 HTTP 层只处理 GET/POST/DELETE，而真实 finalize 用 PATCH；如果照抄“全部用 POST”，客户端代码在真实服务上会 405。为此 `server.py` 增加了 `do_PATCH`，并让请求体读取覆盖 `{"POST", "PATCH"}`。

### 错误模型

| 场景 | 错误码 | HTTP |
|---|---|---|
| `model_id` 格式不对 | `INVALID_PARAMETER_VALUE` | 400 |
| 模型不存在，或所属实验已删除 | `RESOURCE_DOES_NOT_EXIST` | 404 |
| 参数已存在且值不同 | `INVALID_PARAMETER_VALUE` | 400 |
| 终态再次改成另一个状态 | `INVALID_PARAMETER_VALUE` | 400 |
| `experiment_ids` 超过 10 个（Mini 强化限制） | `INVALID_PARAMETER_VALUE` | 400 |
| 搜索语法不支持（OR、NOT、tags、name LIKE、非字符串 params） | `INVALID_PARAMETER_VALUE` | 400 |

“所属实验已删除”返回 404 而不是 403，是刻意的：软删除的实验对外表现为资源不存在；恢复实验后模型自动重新可见。Mini 在 `get_logged_model_row` 中 JOIN `experiments.lifecycle_stage` 实现这一点，而不是物理删除模型。

## 40.6 搜索方言：比 run 搜索更窄，也更安全

真实 `search_logged_models` 的过滤能力比 `search_runs` 窄，但“窄”的边界本身存在版本与文档差异：

- 支持裸属性：`model_id`、`name`、`model_type`、`status`、`source_run_id`、`creation_time`、`last_updated_time`；
- 支持 `params.*`（字符串）与 `metrics.*`（数值）；
- **tags 是一个灰色地带**：官方搜索指南明确写“No Tag Support”，但 API docstring/实现中存在 `tags.*` 示例，文档与实现有已知矛盾（mlflow#17920）。Mini 选择按指南不支持 tags，并在 `40.10` 标注；如果你依赖 tags 过滤，先在目标版本上实测。
- `name` 只支持 `=`、`!=`、`IN`、`NOT IN`，不支持 `LIKE`；
- 文档只保证 AND 组合；Mini 直接拒绝 `OR` 与一元 `NOT`，避免写出“本地能跑、真实服务报错”的过滤器；
- 官方建议“一次不要搜索超过 10 个实验”，这更像性能建议而非 REST 校验；Mini 把它固化成 400，属于教学强化，真实服务不保证同样拒绝。
- `params.*` 的值是字符串；Mini 拒绝 `params.lr = 0.1`，要求写成 `params.lr = '0.1'`，避免 SQLite 中 `'1e-07'` 与 `1e-07` 这类“看起来相等、实际不匹配”的陷阱。

Mini 的编译器在 `mini_mlflow/search.py` 的 `compile_logged_model_filter`：

```python
where, params = compile_logged_model_filter(
    "status = 'READY' AND params.temperature = '0.2' AND metrics.quality >= 0.8"
)
```

生成的 SQL 形如：

```sql
lm.experiment_id IN (?, ?)
AND (lm.status = ?)
AND (EXISTS (SELECT 1 FROM logged_model_params lmp
             WHERE lmp.model_id = lm.model_id AND lmp.key = ?
               AND lmp.value = ?))
AND (EXISTS (SELECT 1 FROM logged_model_metrics lmm
             WHERE lmm.model_id = lm.model_id AND lmm.key = ?
               AND lmm.value >= ?
               AND lmm.id = (SELECT lmm2.id FROM logged_model_metrics lmm2
                             WHERE lmm2.model_id = lmm.model_id AND lmm2.key = lmm.key
                             ORDER BY lmm2.step DESC, lmm2.timestamp DESC,
                                      lmm2.id DESC LIMIT 1)))
```

三个设计点：

1. **所有值都是绑定参数。** 字段名只经过白名单映射，永远不拼进 SQL；`params.temperature` 的“字段名”作为参数值传给 `lmp.key = ?`。
2. **指标取最新点。** 子查询按 `step DESC, timestamp DESC, id DESC` 选一行，避免“历史上某个高点”误命中。
3. **`EXISTS` 不会放大行数。** 一个模型有多个参数/指标时，JOIN 会让同一个模型出现多行，破坏分页计数；`EXISTS` 只做布尔判断。

`status` 过滤器会把短别名归一化：

```text
status = 'READY'                       -> status = 'LOGGED_MODEL_READY'
status IN ('READY', 'UPLOAD_FAILED')   -> IN ('LOGGED_MODEL_READY', 'LOGGED_MODEL_UPLOAD_FAILED')
```

这解决了一个真实世界的坑：不同 SDK/文档示例使用不同拼写，服务端如果只认一种，过滤会“静默返回空列表”。

### 排序与分页

排序字段同样走白名单：

```python
models, token = client.search_logged_models(
    [experiment_id],
    filter_string="status = 'READY'",
    order_by=["metrics.quality DESC", "name ASC"],
    max_results=20,
)
```

分页用不透明 offset token，和 run 搜索一致：服务端多取一条判断 `has_more`，返回 `next_page_token`；客户端不应解析 token 的内部结构。真实 MLflow 的 token 也是不透明的，但内部实现可能是游标而非 offset；Mini 在文档里明确这是教学简化。

## 40.7 `models:/<model_id>` 的解析链

模型中心最直接的收益是“没注册也能加载”。Mini 的 `models/__init__.py` 在 `load_model` 里增加了三条分支：

```text
models:/m-<32hex>
  -> client.get_logged_model(model_id)
  -> 检查 artifact_location 是 file://
  -> load_model_from_path(<artifact dir>)

models:/<registered_name>/<version>
  -> registry 返回 version.source
  -> 如果 source 是 models:/m-...，递归走上一条

models:/<registered_name>@<alias>
  -> 同上，alias 解析后仍可能落到 LoggedModel
```

这条链让 Registry 的 `source` 可以是 `models:/m-...`，而不是只能 `runs:/...`。测试 `test_log_model_with_name_and_load_by_model_uri` 同时覆盖了：

- 直接用 `models:/<model_id>` 加载；
- 注册后通过 `models:/agent/1` 加载；
- 两条路径得到相同预测。

### 远程模式的诚实边界

Mini 的远程客户端可以创建、读取、finalize、打标签、记录参数/指标、搜索和删除 LoggedModel，但**不能远程下载 LoggedModel 的制品**：`artifact_location` 是服务端的 `file://` 路径，客户端机器上没有这个目录。真实 MLflow 通过 logged-model artifact 端点（例如 `/ajax-api/2.0/mlflow/logged-models/{model_id}/artifacts/files` 与 `/artifacts/directories`）与 artifact 服务解决；Mini 明确抛错：

```text
remote models:/<model_id> loading needs a logged-model artifact download endpoint
```

这是一个刻意保留的缺口，而不是隐藏的行为。它也让“本地能跑”与“远程能跑”的差异变成可测试的断言（`test_remote_logged_model_roundtrip`）。

## 40.8 客户端、CLI 与可运行示例

Python 客户端的方法与真实 `MlflowClient` 对齐：

```python
model = client.create_logged_model(
    "agent", experiment_id="0", model_type="Agent",
    source_run_id=run_id, tags={"team": "nlp"}, params={"temperature": 0.2},
)
client.log_logged_model_params(model.model_id, {"max_tokens": 128})
client.log_logged_model_metrics(model.model_id, {"quality": 0.8}, step=1)
client.set_logged_model_tags(model.model_id, {"stage": "candidate"})
client.finalize_logged_model(model.model_id, "READY")
models, token = client.search_logged_models(["0"], filter_string="status = 'READY'")
client.delete_logged_model_tag(model.model_id, "stage")
client.delete_logged_model(model.model_id)
```

fluent API 提供 `initialize_logged_model`，在 active run 中自动继承 `experiment_id` 与 `source_run_id`：

```python
with mlflow.start_run() as run:
    model = mlflow.initialize_logged_model("agent", model_type="Agent")
    mlflow.log_logged_model_params(model.model_id, {"temperature": 0.2})
    mlflow.finalize_logged_model(model.model_id)
```

CLI 增加了一组命令，便于在 shell 里检查和清理：

```bash
python -m mini_mlflow logged-models create agent --model-type Agent --tag team=nlp
python -m mini_mlflow logged-models list --filter "status = 'READY'"
python -m mini_mlflow logged-models get m-...
python -m mini_mlflow logged-models metrics m-... --metric quality=0.8 --step 1
python -m mini_mlflow logged-models finalize m-... --status READY
python -m mini_mlflow logged-models delete m-...
```

完整端到端示例是 `examples/14_logged_models.py`：训练一个线性模型、用 `name=` 记录、写模型级指标、打标签、创建一个故意失败的草稿模型、按 `status + metrics` 搜索、最后用 `models:/<id>` 加载并预测。

## 40.9 测试：11 个用例覆盖哪些不变量

`tests/test_logged_models.py` 的分布：

| 测试 | 证明什么 |
|---|---|
| `test_create_logged_model_defaults_and_artifact_location` | 默认 PENDING、`m-` ID、制品目录在 root 内、wire roundtrip |
| `test_create_logged_model_rejects_bad_links` | 删除的实验、跨实验 source run、不存在的 run、非法 ID |
| `test_params_are_immutable_and_tags_are_mutable` | 参数冲突拒绝；标签 upsert/删除；删除不存在的标签 404 |
| `test_metric_history_latest_value_uses_step_then_timestamp` | 3.18 的 step 优先语义；指标过滤；非数值拒绝 |
| `test_finalize_state_machine` | PENDING -> READY；重复 finalize 幂等；终态改状态失败 |
| `test_search_attributes_params_and_ordering` | 属性/参数/指标组合；排序；OR、NOT、tags、name LIKE、非字符串 params 被拒绝 |
| `test_search_pagination_and_experiment_scope` | token 分页；实验作用域；10 个实验上限 |
| `test_delete_and_experiment_cascade` | 删除元数据；软删除实验后读取与所有变更都 404；恢复后可见且未被污染 |
| `test_log_model_with_name_and_load_by_model_uri` | `name=` 写模型；`models:/m-...` 加载；Registry source 指向 LoggedModel |
| `test_remote_logged_model_roundtrip` | REST 契约、PATCH finalize、远程 `log_model(name=...)` 上传与 `models:/<id>` 下载的明确失败 |
| `test_entity_wire_roundtrip` | `info/data` 线格式与 Metric 时间戳/step |

这些测试仍然不能证明“实现正确”。它们证明的是“在当前假设下行为符合预期”。第 21 章列出的假绿风险同样适用：

- 测试使用 SQLite，未覆盖 Postgres 的并发与类型差异；
- 没有进程崩溃注入，PENDING 的 orphan 清理未被验证；
- 没有多客户端竞争同一模型的并发测试；
- 远程测试只覆盖元数据，不覆盖制品传输；
- 搜索只覆盖 Mini 支持的子集，不能证明与真实服务逐字节一致。

## 40.10 与真实 MLflow 3.18 的差距

| 能力 | 真实 MLflow 3.18 | Mini |
|---|---|---|
| 实体字段 | model_id、experiment_id、name、model_type、source_run_id、status、artifact_location、params、metrics、tags、registrations | 除 `registrations` 外基本一致 |
| 注册关系 | LoggedModel 记录已注册的版本 | Registry 只记录 `source`，不能从模型反查版本 |
| 数据集关联 | 模型级 dataset inputs | 只有 run 级 `run_inputs` |
| 模型别名 | `set_logged_model_alias` 等 | 未实现 |
| 远程制品 | artifact 服务 / presigned URL | 明确不支持 |
| 删除语义 | 元数据 + 可配置的制品清理 | 只删元数据，制品留给 GC |
| 终态语义 | 以 proto/实现为准；不同版本对重复 finalize 的宽容度可能不同（需核实） | 第一个终态获胜，后续改状态 400 |
| 实验数量 | 官方建议 ≤10，未必校验 | 超过 10 个直接 400 |
| tags 搜索 | 指南写不支持，实现/docstring 有 tags 示例 | 按指南不支持 |
| Create 字段 | 无 status/status_message | 同样不读取；额外接受 artifact_location |
| 搜索 | 真实 SQL store，支持更多属性与优化 | SQLite 子查询，AND-only 子集 |
| LIKE 大小写 | 取决于后端（Postgres 默认区分） | SQLite 的 ASCII LIKE 不区分大小写，未额外校正 |
| fluent 返回 | `search_logged_models` 默认返回 DataFrame | 返回 `list[LoggedModel]`，不引入 pandas |
| UI | Models 页面、对比、注册入口 | 只有 REST/CLI/示例 |
| 权限 | workspace + RBAC + 子资源 DENY | 单 token |
| 迁移 | `mlflow db upgrade` 管 schema | `CREATE TABLE IF NOT EXISTS` + 版本号 |

`registrations` 是最值得补的一项。它不是简单的“多一张表”：一个 LoggedModel 可以注册到多个 Registry 名称，每个注册产生一个版本；删除模型时是否级联删除版本、版本是否允许 source 指向已删除模型，都需要明确策略。Mini 选择“Registry 单向引用模型”，把这个问题留给读者。

## 40.11 从 2.x 迁移到模型中心的最小步骤

如果你有一个 2.x 风格的实现，按下面顺序改，风险最低：

1. **加表，不改旧路径。** 新建 `logged_models` 与三张子表，旧 `runs:/` 模型继续可读。
2. **让 `log_model` 支持双写。** `artifact_path=` 走旧逻辑；`name=` 走新逻辑。不要强制所有调用方立刻迁移。
3. **先支持读取。** `load_model` 增加 `models:/<model_id>` 分支；部署系统可以先读新 URI，再切写入。
4. **Registry 增加 `model_id` 列。** 先允许为空，新版本写入，旧版本回填；不要在一个迁移里设为 NOT NULL。
5. **加搜索，再切 UI。** 搜索是只读能力，风险最低；UI 最后接。
6. **最后改默认值。** 例如默认 `log_model` 创建 LoggedModel、默认加载走 `models:/`；这一步需要回滚演练。

这个顺序与第 34 章的“先读后写、先服务端后客户端”一致。区别是本章给出了可运行的参考代码，你可以直接对照 `mini_mlflow/tracking.py`、`store/sqlite_store.py`、`server.py` 和 `models/__init__.py` 的实现顺序。

## 小结

- LoggedModel 把“模型”从 run 的附属目录变成有独立 ID、生命周期和模型级元数据的实体。
- 四张表的核心不变量是：实验拥有模型、run 只是血缘、参数不可变、标签可变、指标按 step 优先取最新。
- 生命周期只有三个状态；终态不可逆，重复 finalize 幂等；真正难的是元数据与制品之间的两个失败窗口。
- 搜索方言比 run 搜索窄：无 tags、无 OR/NOT、name 不支持 LIKE；Mini 把“官方建议 ≤10 个实验”固化成硬限制。窄不是缺陷，是可移植性。
- `models:/<model_id>` 让未注册模型也能加载；Registry 的 `source` 可以指向它，从而保住“版本 -> 训练产物”的血缘。
- Mini 已实现核心闭环，但注册关系、数据集、模型别名、远程制品、UI、GC 与 RBAC 仍是明确缺口。

## 练习

1. 给 `logged_models` 增加 `registrations` 表，设计“一个模型注册到多个名称”的约束，并说明删除模型时的级联策略。
2. 实现 PENDING 模型的 orphan 扫描：超过 30 分钟仍为 PENDING 的模型转 FAILED，并写出对应的测试。
3. 把 `search_logged_models` 的 offset token 改成游标分页，比较两种实现在并发插入下的稳定性。
4. 为 `models:/<model_id>` 增加远程制品下载：设计一个返回 zip 的端点，并限制单次下载大小。
5. 设计一个“模型卡”生成器：从 LoggedModel 的 params/metrics/tags/source_run 自动生成 Markdown，并接入第 23 章的发布审批。
6. 解释为什么 `source_run_id` 使用 `ON DELETE SET NULL`，而 `experiment_id` 使用 `ON DELETE CASCADE`；如果反过来会发生什么？

---

下一章：[第 41 章 真实 MLflow 生产部署实战](41-production-deployment.md)
