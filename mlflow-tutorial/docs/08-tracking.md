# 第 8 章 Tracking 核心：Run 生命周期与日志语义

## 学习目标

- 实现并解释 Run 状态机，知道哪些操作在终态后必须拒绝。
- 理解参数、指标、标签、artifact 的不同一致性要求。
- 能设计一个原子的批量写入接口，并解释为什么需要它。
- 识别“日志成功但数据不完整”的常见原因。
- 理解系统指标与环境采集为什么是旁路，以及采样频率、线程生命周期和指标基数的工程取舍。

## 8.1 状态机

```text
             create
               │
               ▼
           RUNNING ────── end(FINISHED) ──> FINISHED
              │  │
              │  └────── end(FAILED) ─────> FAILED
              │
              └───────── end(KILLED) ─────> KILLED
```

Mini 支持的终态：`FINISHED`、`FAILED`、`KILLED`；`RUNNING`/`SCHEDULED` 非终态。

规则：

- 只能从非终态进入终态；
- 对同一终态重复 `end` 是幂等；
- 终态改成另一个终态必须失败；
- 终态 run 不允许写参数/指标/标签/输入数据集；
- artifact 读取始终允许（只要 run 未删除），artifact 写入在 Mini 中被允许，便于事后补模型/报告，但真实 MLflow 的 `log_model` 通常要求 active run。这个差异在文档中明确。

代码在 `TrackingService.end_run`：服务层只做终态白名单校验，真正的竞态处理在存储层的一次事务里完成：

```python
# TrackingService
if status not in {"FINISHED", "FAILED", "KILLED"}:
    raise InvalidParameter(...)
return self.store.end_run(run_id, status, now_ms())

# SqliteStore.end_run（同一事务）
row = conn.execute("SELECT status, lifecycle_stage FROM runs WHERE run_id=?", ...).fetchone()
if row["status"] in terminal:
    return same_status_or_raise(row["status"], status)   # 幂等或冲突
cursor = conn.execute(
    "UPDATE runs SET status=?, end_time=? WHERE run_id=? AND status NOT IN (?,?,?)",
    (status, end_time, run_id, *terminal),
)
if cursor.rowcount == 0:                                 # 输掉竞态
    return same_status_or_raise(current_status(), status)
```

关键点：

- **不要先读再写**：`SELECT` 判断终态、再 `UPDATE` 会把第 7 章讲过的读-改-写竞态重新引入；
- 条件 `UPDATE` 让“只有第一个到达者获胜”成为数据库事实；
- 输掉竞态时再读一次赢家的状态：同终态幂等返回，不同终态报错；
- 数据库只约束状态枚举，业务错误信息由服务层提供。

### 为什么要 KILLED

`FAILED` 表示程序出错，`KILLED` 表示被外部终止（用户取消、调度器抢占、超时）。区分二者对统计“模型失败率”很重要：被抢占不是模型 bug。

## 8.2 启动与恢复

`start_run` 支持两种路径：

1. 创建新 run：解析实验、生成 run_id、分配 artifact URI、写默认标签。
2. 恢复已有 run：传入 `run_id`，读取并验证仍未终态。

恢复机制用于多进程/分布式训练：

```python
with mlflow.start_run(run_id=os.environ["MINI_MLFLOW_RUN_ID"]):
    ...
```

注意 Mini 不会在子进程中自动读取环境变量，必须显式传入。这是刻意的：环境变量误设不应该悄悄改变日志目标。

### 默认标签

- `mlflow.user`：`MINI_MLFLOW_USER` / 系统用户名；
- `mlflow.source.name`：`sys.argv[0]`；
- `mlflow.runName`：与 `info.run_name` 同步。

真实 MLflow 还有 source type/version、git 信息等；这些应由项目模板或 CI 注入，而不是靠平台猜测。

## 8.3 参数：幂等但不可变

```python
service.log_param(run_id, "alpha", 1)
service.log_param(run_id, "alpha", 1)   # OK
service.log_param(run_id, "alpha", 2)   # InvalidParameter
```

为什么不允许覆盖？

- 覆盖会让“同一个 run”变成多个实验的混合体；
- 指标与参数的关系不再可解释；
- 并行 worker 写同一参数会发生静默竞争。

存储层在 `log_batch` 内再次检查：

```sql
SELECT value FROM run_params WHERE run_id=? AND key=?
```

如果已存在且不同，抛 `InvalidParameter`。这属于“纵深防御”：即使未来有新的调用方绕过服务层，也不会破坏不变量。

### 参数值长度

真实 MLflow 有长度限制；Mini 限制 6000 字符。长配置应作为 JSON artifact 保存，并在参数中放 digest/路径。

## 8.4 指标：追加与“最新值”

```python
service.log_metric(run_id, "val_auc", 0.81, step=1)
service.log_metric(run_id, "val_auc", 0.84, step=2)
```

每次调用追加一行。`get_run` 返回最新值，`get_metric_history` 返回全部：

```python
run.data.metrics["val_auc"]        # 0.84
[point.value for point in service.get_metric_history(run_id, "val_auc")]
# [0.81, 0.84]
```

“最新”排序：

```sql
ORDER BY timestamp DESC, id DESC
```

注意：如果客户端传了回退的时间戳，最新值可能不是最后调用的值。这是有意的：时间戳是语义的一部分。Mini 的测试覆盖“时间戳更旧但后写入”的情况。

### 时间戳与 step 的选择

- 训练曲线：`step=epoch/iteration`，时间戳用实际时间；
- 在线指标：时间戳是业务时间，step 可为 0；
- 同一 step 的多个点：允许，但图表应能处理。

真实 MLflow 对 step 有单调性建议；Mini 不强制单调，只强制非负。强制单调会让补写历史点变得困难，取舍写进文档。

## 8.5 标签：可变、用于协作

标签可以随时改：

```python
service.set_tag(run_id, "review_status", "approved")
```

`mlflow.runName` 是特殊标签：更新它会同步 `info.run_name`。这模仿真实 MLflow 的行为，让 UI 显示更友好。

标签不应承载高基数或高频数据。一个反例：

```python
service.set_tag(run_id, f"prediction_{i}", value)  # 不要这样做
```

应该作为指标（如果是数值序列）或 artifact（如果是明细）。

## 8.6 批量日志的原子性

`log_batch` 一次可以写：

```json
{
  "metrics": [{"key":"loss","value":0.5,"step":3}],
  "params": {"lr":"0.001"},
  "tags": {"stage":"train"}
}
```

Mini 的流程：

1. 验证 run 可写；
2. 验证所有 metric/param/tag；
3. 在一个 SQLite 事务中写入；
4. 任一失败回滚。

测试 `test_log_batch_is_atomic_on_validation_error` 验证：一个非法嵌套参数会导致整个 batch 不落库。

### 为什么需要批量

- 减少往返（远端模式尤其重要）；
- 保证一次逻辑更新的原子性；
- 让客户端可以按大小/时间聚合，降低服务端压力。

真实 MLflow 有 `log_batch` REST 接口；不同后端的批大小和超时不同。生产客户端应实现“有界队列 + 失败重试 + 幂等键”。

## 8.7 Artifact 与 Run 的关系

- `run.info.artifact_uri` 在创建时确定，之后不变；
- artifact 路径是相对该 URI 的命名空间；
- 删除 run 不会自动删除 artifact；
- Mini 允许终态后写 artifact，真实 MLflow 可能拒绝；
- 模型版本只保存 `source`，不复制 artifact。

日志顺序建议：

```text
创建 run → 写参数/标签 → 训练 → 写指标 → 写报告/模型 → end run
```

如果先 end 再补 artifact，跨系统（数据库/对象存储）失败窗口更难对账。

## 8.8 模型与 Registry 的衔接

`TrackingService.log_model` 做四件事：

1. 在临时目录 `save_model`（生成 `MLmodel`、`model.json`、签名等）；
2. `log_artifacts` 上传到 `<run artifact>/<artifact_path>`；
3. 构造 `runs:/<run_id>/<artifact_path>`；
4. 若指定 `registered_model_name`，确保注册模型存在，创建新版本，source 指向上面的 URI。

失败模式：

- 第 2 步成功、第 4 步失败：留下未注册 artifact；
- 第 4 步成功、响应丢失：客户端重试可能创建重复版本。

解决方向：幂等键（run_id + artifact_path + digest）、注册事务化（元数据 + 制品校验）、对账任务。

## 8.9 搜索与分页

`search_runs` 不在服务层做 Python 过滤，而是调用 `compile_run_filter` 生成参数化 SQL。返回 `(runs, next_token)`：

```python
runs, token = service.search_runs(
    'metrics.val_auc > 0.8 and tags.team = "nlp"',
    order_by=["metrics.val_auc DESC"],
    max_results=20,
)
```

服务层负责：

- 校验 `max_results <= config.search_max_results`；
- 解析 DSL → SQL；
- 计算 `ORDER BY` 的动态 key 参数；
- 多取一条判断是否有下一页；
- 返回 token。

**返回类型差异（迁移真实 MLflow 时最容易踩的坑）**：

| API | 返回 |
|---|---|
| Mini `TrackingService.search_runs` | `(list[Run], next_token)` |
| 真实 `mlflow.search_runs`（fluent） | pandas `DataFrame`，列名形如 `metrics.acc`、`params.alpha`、`tags.team` |
| 真实 `MlflowClient.search_runs`（client） | `list[Run]` 实体 |

真实 fluent API 示例：

```python
import mlflow

df = mlflow.search_runs(
    experiment_ids=["1"],
    filter_string="metrics.val_auc > 0.8",
    order_by=["metrics.val_auc DESC"],
    max_results=20,
)
print(df[["run_id", "metrics.val_auc"]].head())
```

不要把 Mini 的 tuple 解包直接套到真实 fluent API，也不要把 DataFrame 当实体列表迭代。

## 8.10 输入数据集与血缘

```python
service.log_input(
    run_id,
    dataset_name="transactions",
    dataset_digest="sha256:...",
    dataset_source="s3://bucket/transactions/2026-11-20",
    context="training",
)
```

它记录的是**指针和摘要**，不是数据本身。真正实现 point-in-time 正确性需要数据平台配合。若 digest 只是文件 hash 而非逻辑快照，仍可能出现“同一 digest 不同内容”（例如分区更新）。

真实 MLflow 的 dataset 输入更完整：

```python
import mlflow
import mlflow.data
from mlflow.data.pandas_dataset import PandasDataset

dataset: PandasDataset = mlflow.data.from_pandas(
    df,
    source="s3://bucket/transactions/2026-11-20.parquet",
    name="transactions",
    targets="fraud",
)
with mlflow.start_run() as run:
    mlflow.log_input(dataset, context="training")
```

`log_input` 会记录：

- name、digest、source、source_type；
- context（training/evaluation/validation）；
- schema/profile（列类型、缺失率、分位数等，取决于集成）；
- 与 run 的关联，便于回答“这个模型训练用了哪份数据”。

真实 MLflow 的搜索作用域是复数形式：`datasets.name`、`datasets.digest`、`datasets.context`（不是 `dataset.name`）。Mini 也实现了这三个字段，语义是“该 run 的**任意一条** dataset input 满足条件”：

```python
runs = service.search_runs(
    'datasets.name = "transactions" AND datasets.context = "training"'
)
```

编译结果是相关子查询，而不是 JOIN：

```sql
EXISTS (SELECT 1 FROM run_inputs d
        WHERE d.run_id = r.run_id AND d.dataset_name = ?)
```

为什么用 `EXISTS`：

- 一个 run 可以登记多份数据集；JOIN 会让同一个 run 出现多行，破坏分页和 `max_results` 的计数；
- `EXISTS` 找到第一行即可短路，配合 `(run_id, dataset_name, dataset_digest, context)` 覆盖索引，成本与输入条数线性相关；
- 所有值仍然是绑定参数，字段名经过白名单映射（`name -> dataset_name`、`digest -> dataset_digest`、`context -> context`）。

两个必须写进文档的语义：

1. **AND 不是“同一份数据集”**。上面的表达式会编译成两个独立的 `EXISTS`，可能由两条不同的 input 分别满足。要表达“同一份数据同时满足”，要么在应用层先解析出候选 digest，要么扩展一个复合过滤字段；真实 MLflow 同样按独立条件处理，调用方不应假设两个条件指向同一条 dataset input。
2. **只支持字符串比较**。`>`、`<` 这类关系比较会被拒绝；可用 `=`、`!=`、`IN`、`LIKE`、`ILIKE`。`IS NULL` 也不支持——`dataset_name` 是 `NOT NULL`，“没有登记数据集”应通过反向条件或单独查询表达，而不是伪装成 NULL。

仍然存在的差距：

- Mini 只存 name/digest/source/source_type/context，不计算 digest，也不存 schema/profile；
- 没有 `mlflow.data` 的数据集构造器、自动 digest 与 schema 推断；
- 不支持按 `source`/`source_type` 过滤（真实 MLflow 也只把 name/digest/context 列为搜索字段）；
- 没有“按数据集反查所有 run”的专用 API，只能通过 `search_runs` 过滤；
- digest 规范化仍是使用方责任：同一逻辑快照若用不同哈希算法、不同文件顺序生成，会得到不同 digest。

## 8.11 系统指标、环境与自动上下文

真实 MLflow 除了业务指标，还能自动采集**运行环境**的时序数据。它不是“又一个监控系统”，而是把“这次训练在什么机器、什么负载下跑”写进 run，便于解释性能异常和复现环境。

### 8.11.1 开启方式

```python
import mlflow

# 全局：当前进程内所有 run 都采集
mlflow.enable_system_metrics_logging()

# 单 run 覆盖全局设置
with mlflow.start_run(log_system_metrics=True):
    ...
```

也可以用环境变量 `MLFLOW_ENABLE_SYSTEM_METRICS_LOGGING=true`。关闭用 `mlflow.disable_system_metrics_logging()`。

### 8.11.2 指标命名

默认前缀是 `system/`，常见 key：

```text
system/cpu_utilization_percentage
system/system_memory_usage_megabytes
system/system_memory_usage_percentage
system/disk_usage_megabytes
system/disk_available_megabytes
system/disk_usage_percentage
system/network_receive_megabytes
system/network_transmit_megabytes
system/gpu_utilization_percentage
system/gpu_memory_usage_megabytes
system/gpu_memory_usage_percentage
system/gpu_power_usage_watts
system/gpu_power_usage_percentage
```

GPU 指标需要机器上有 GPU 且安装 `nvidia-ml-py`。多节点训练可用 `mlflow.set_system_metrics_node_id(...)`，key 会变成 `system/<node_id>/...`。

### 8.11.3 采样与聚合

默认每 **10 秒**采样一次，`samples_before_logging=1`；可以把多个采样点聚合（均值）后再写，降低写入量：

```python
mlflow.set_system_metrics_sampling_interval(10)
mlflow.set_system_metrics_samples_before_logging(6)  # 约每分钟写一个点
```

环境变量：`MLFLOW_SYSTEM_METRICS_SAMPLING_INTERVAL`、`MLFLOW_SYSTEM_METRICS_SAMPLES_BEFORE_LOGGING`。注意采样频率与写入频率是两件事：采样太稀会漏掉尖峰，写入太密会放大数据库压力。

### 8.11.4 工程陷阱

- **采集失败不能拖垮训练**：系统指标是旁路，权限/驱动/平台不支持时应降级，而不是让 run 失败；
- **后台线程的生命周期**：run 结束必须停止采集；进程 fork 后线程不会自动继承，子进程要重新注册；
- **指标基数**：多节点/多 GPU 会产生大量 key，保留期和聚合要先设计；
- **时钟**：采样时间戳与训练 step 不是一回事，图表上不要把两者混用；
- **敏感信息**：主机名、节点 ID 可能暴露基础设施信息，对外分享 run 前要脱敏。

Mini 没有后台采样器，但 `metrics` 表可以存 `system/*` 指标。如果要实现，建议：

```text
SystemMetricsMonitor(run_id, interval, samples_before_logging)
  - daemon 线程 + stop Event
  - 每个采样点写 memory，按窗口均值落库
  - 异常只 log，不影响 run 状态
```

### 8.11.5 环境与来源标签

真实 MLflow 还会记录 `mlflow.source.type`、`mlflow.source.name`、`mlflow.source.git.commit`、`mlflow.user` 等标签；`mlflow.log_params` 可以记录 Python/框架版本，`mlflow.log_text` 可以保存 `requirements.txt`。原则是：**平台能自动采集的自动采集，采集不到的由 CI/项目模板显式注入**，不要靠人回忆。

## 8.12 与真实 MLflow 的差距

- 真实 `log_model` 与 flavor 注册、签名推断、依赖推断更复杂；
- 真实 run 可以有嵌套 run、parent run ID、source version；
- 真实系统支持异步日志与更丰富的 dataset input 结构；
- 真实终态规则和 artifact 写入规则依版本/后端而异；
- 真实搜索语法更广，但仍不是完整 SQL。

## 小结

- Run 状态机必须单向且可审计；终态写入应拒绝。
- 参数不可变是实验可解释性的基础；批量日志保证一致性。
- 指标是追加历史，最新值由时间戳与写入顺序共同决定。
- 模型日志横跨数据库与对象存储，天然存在失败窗口，需要幂等与对账。
- 系统指标是旁路采集：采样频率、线程生命周期、指标基数和失败降级要先设计。

## 练习

1. 设计一个“运行被抢占”的流程，确保状态最终为 KILLED 且资源被清理。
2. 为什么 `log_batch` 中的参数冲突要在存储层再检查一次？
3. 给 `log_model` 设计幂等键，说明如何避免重复注册版本。

---

下一章：[第 9 章 搜索 DSL](09-search-dsl.md)
