# 第 3 章 概念模型：Experiment、Run、Metric、Artifact、Model

## 学习目标

- 准确区分 Experiment、Run、Param、Metric、Tag、Artifact、Model Version、Alias、Trace。
- 理解指标历史、`step`、时间戳和“最新值”的语义。
- 理解 artifact URI、source URI 和模型版本之间的关系。
- 能够设计一套不会在三个月后失控的命名与标签体系。

## 3.1 Experiment：问题的边界

Experiment 回答“我们在解决哪个问题”，而不是“第几次尝试”。

推荐：

```text
fraud-detection-v2
search-ranking-epoch-v3
support-ticket-routing
```

不推荐：

```text
test1
小陈的
final
2026-11-26
```

一个 Experiment 可以包含多个模型族、多个特征版本、多个失败尝试。它更像“项目文件夹”，不是“一次实验”。如果两个任务的成功指标完全不同，就不应放在同一个 Experiment 中，否则搜索和对比会被污染。

MLflow 有默认 Experiment（ID `0`），MiniMLflow 也保留它。默认实验适合临时脚本，不适合团队资产。

## 3.2 Run：一次可审计的执行

Run 是追踪的基本单位，核心字段：

| 字段 | 含义 | 是否可变 |
|---|---|---|
| run_id | 32 位十六进制唯一 ID | 否 |
| experiment_id | 所属实验 | 否 |
| run_name | 人类可读名称 | 是（标签同步） |
| status | RUNNING/SCHEDULED/FINISHED/FAILED/KILLED | 非终态（RUNNING/SCHEDULED）可终结为 FINISHED/FAILED/KILLED |
| start_time/end_time | 毫秒时间戳 | 否/终结时写 |
| artifact_uri | 制品根 URI | 否 |
| user_id | 发起者 | 否 |
| lifecycle_stage | active/deleted（软删除） | 可变 |

一次 Run 应当对应一次**可解释的训练/评估/批处理执行**。把 30 天的持续训练塞进一个 Run 会让指标时间线和 artifact 无法解释；应该按天/按 epoch 或按检查点拆分，并用标签关联。

## 3.3 Param：不可变的输入

Param 是“影响结果的配置”：

```python
mlflow.log_param("learning_rate", 0.001)
mlflow.log_param("model_type", "linear")
mlflow.log_param("seed", 42)
```

语义要点：

- 值在 MLflow 中通常以字符串保存，但客户端可以传数字/布尔；查询时注意类型。
- 同一个 key 重复写相同值是幂等的；写不同值应失败。这防止训练中途悄悄改配置。
- 不适合放高基数、随时间变化的数据（如每步 loss、用户 ID 列表），那是指标或标签/制品。
- 参数长度有限制（真实 MLflow 通常约 6000 字符）；大配置应存成 JSON artifact。

MiniMLflow 在 `TrackingService.log_batch` 中校验并调用 `SqliteStore.log_batch`，后者在事务里再次检查参数冲突（纵深防御）。

## 3.4 Metric：时间序列，不是单个数字

一条 Metric 记录至少包含：

```text
key, value, timestamp(ms), step
```

同一个 key 可以有多个点：

```text
loss step=1 2.31
loss step=2 1.87
loss step=3 1.52
```

关键规则：

- **历史是追加的**，不是覆盖。审计时需要看到训练过程中的曲线。
- **“最新值”由排序决定**。MiniMLflow 取 `ORDER BY timestamp DESC, id DESC` 的第一条；真实 MLflow 也以时间/步骤为语义基础，但具体实现依后端。
- **`step` 不是时间戳**：它表示优化步、epoch 或业务序号，允许相同 step 的多个点（例如同一 step 的 train/val 指标应使用不同 key）。
- 数值必须是有限浮点数。`NaN`/`Infinity` 会污染聚合和图表，MiniMLflow 直接拒绝。
- 指标 key 应稳定、可枚举，避免把参数编码进 key 导致基数爆炸（如 `user_123_auc`）。

### 表结构上的取舍

如果只存每个 key 的最新值，表很小，但无法画曲线；如果存全历史，查询最新值时需要索引和窗口函数。MiniMLflow 选择后者：

```sql
CREATE INDEX idx_metrics_run_key_latest
  ON metrics(run_id, key, timestamp DESC, id DESC);
```

查询最新值时用窗口函数或 `ORDER BY ... LIMIT 1`。第 7、25 章会讨论大规模下的分区与降采样。

## 3.5 Tag：可变的上下文

Tag 适合：

- `team=nlp`
- `git_commit=...`
- `data_snapshot=2026-11-20`
- `review_status=approved`
- `deploy_env=staging`

不适合：

- 高频变化的指标（用 Metric）；
- 需要强类型和聚合的字段（应该进专门表/数据仓库）；
- 巨量文本（应作为 artifact 或文档链接）。

Tag 与 Param 的区分不是“值类型”，而是**语义**：Param 是模型输入，改动意味着另一个实验结果；Tag 是运行上下文，可以事后修正。把 `learning_rate` 写成 Tag 会导致实验对比失真，把 `review_status` 写成 Param 会导致无法审批。

## 3.6 Artifact：字节与其 URI

Artifact 可以是：

- 模型目录（`MLmodel`、权重、配置）；
- 评估报告、混淆矩阵；
- 数据抽样、特征重要性图；
- 日志、环境锁文件、源码快照。

每个 Run 有独立的 `artifact_uri`，例如：

```text
file:///.../mlartifacts/12/4f2c.../artifacts
s3://bucket/mlflow/12/4f2c.../artifacts
```

路径是**命名空间**，不是数据库主键。Artifact 的元数据（大小、修改时间）通常由存储系统提供；MLflow 只保存根 URI 和文件树。

安全要点：

- 永远不要把用户输入的绝对路径或 `..` 直接拼进 artifact 根；
- 下载目录要与源目录隔离，避免 symlink 逃逸；
- 删除元数据不等于删除字节，需要独立 GC。

MiniMLflow 的 `ArtifactRepository` 对每个相对路径调用 `safe_join`，并拒绝绝对路径、盘符和 `..`，写文件使用“临时文件 + `os.replace`”保证原子性。

## 3.7 Model：从制品到可加载契约

一个模型目录通常包含：

```text
MLmodel            # flavor 声明、签名、依赖、元数据
model.json / .pkl  # 权重或参数
requirements.txt   # 运行依赖
input_example.json # 输入样例
signature.json     # 输入/输出 schema（也镜像在 MLmodel）
```

Flavor 是“如何理解和加载这个模型”的协议：

- `python_function`：统一的 `predict(data)` 接口；
- `sklearn`、`xgboost`、`pytorch`、`transformers`：原生格式与加载逻辑；
- 自定义 flavor：为内部框架扩展。

Model Version 指向一个 `source`，例如：

```text
runs:/4f2c.../model
```

它不复制字节。因此：

- 删除 run/artifact 会让已注册版本失效；
- 提升版本只改注册表指针，便宜且可回滚；
- 发布系统应解析 `models:/name@alias`，而不是硬编码版本号。

## 3.8 Alias 与 Stage：发布指针的两种表达

真实 MLflow 历史上用 Stage（Staging/Production/Archived），后来推荐 Alias：

| 维度 | Stage | Alias |
|---|---|---|
| 数量 | 一个版本通常一个阶段 | 一个模型可有多个别名 |
| 语义 | 固定枚举 | 自定义（champion、canary、shadow） |
| 回滚 | 改阶段 | 改别名指向 |
| 并发 | 容易出现“谁才是生产” | 明确解析一个别名 |
| 现状 | 遗留兼容 | 推荐 |

MiniMLflow 两者都实现，但默认教学路径使用 alias。发布治理见第 23 章。

## 3.9 Trace 与 Span：LLM 时代的观测单位

Run 描述一次训练/批处理；Trace 描述一次**在线调用链**：

```text
Trace: 用户提问
  Span: 检索（RETRIEVER）
  Span: 重排（RERANKER）
  Span: LLM 调用（LLM）
    Event: token 使用量
  Span: 工具调用（TOOL）
```

Trace 不一定属于某个 Run；它通常属于某个 Experiment 或线上服务。Span 之间是树/有向无环关系（MiniMLflow 实现单父树）。核心字段：

- trace_id / span_id / parent_span_id；
- start/end（纳秒）；
- status（OK/ERROR）；
- inputs/outputs/attributes/events；
- tags（环境、租户、模型版本）。

第 19、28 章会讲 OpenTelemetry、采样、隐私与评估闭环。

## 3.10 LoggedModel：模型成为一等实体

2.x 里模型只是 run 目录下的一个 artifact 目录；3.x 的 `log_model` 会创建一个 **LoggedModel**：

```text
models/<model_id>/artifacts/
  MLmodel
  ...
```

它有自己的 `model_id`、`name`、`model_type`、`source_run_id`、模型级 params/metrics/tags，以及三状态生命周期（PENDING → READY / UPLOAD_FAILED）。`models:/<model_id>` 可以在未注册时直接加载。

与 Registry 的关系：

```text
Run ──source_run_id──> LoggedModel ──register──> ModelVersion ──alias──> @champion
                              │                         │
                              └── model_id 字段 ────────┘
```

不要把 LoggedModel 当成“另一个 ModelVersion”：前者是训练产物，后者是命名/发布快照。概念迁移见第 34 章，Mini 的四张表与加载链见第 40 章。

## 3.11 Dataset：评测数据是资产

Tracking 的核心对象是 Experiment、Run、Param、Metric、Tag、Artifact；MLflow 的能力面还包括 Models、Registry、Projects。MLflow 3.x 又把 **LoggedModel** 与 **Dataset** 提升为一等对象。Dataset 容易与 Run 的输入数据集混淆（2.x 已有 `log_input` 血缘，3.x 新增的是独立、可版本化的评测数据集资产）：

```text
Run input：这次训练/评测用了哪份数据？（指针 + digest，挂在 run 上）
Dataset：我们认可哪批评测样本？（独立 ID + 版本 + 记录 + schema/profile）
```

Dataset 的关键字段：

- `dataset_id`：不可变身份；
- `name`：人类可读标签，不是主键；
- `version`：合并次数；
- `digest`：记录集合的内容摘要；
- `schema` / `profile`：从记录推导的字段类型、记录数、空值统计；
- `records`：每条样本的 `inputs` / `expectations` / `tags`。

为什么它必须是一等对象：评测集会被多人评审、反复复用、按切片查询、绑定发布门禁；把它塞进 artifact 或 run tag，就无法回答“这批评测样本什么时候被谁改过”。实现与治理见第 44 章。

## 3.12 一套可执行的命名约定

建议把以下维度固化：

```text
Experiment: <domain>-<task>-v<major>
Run name:   <model>-<featureset>-<yyyymmdd>-<shortsha>
Metric:     <split>_<metric>            # val_auc, test_f1_macro
Param:      <config_namespace>.<name>   # model.lr, data.window_days
Tag:        team, owner, git_commit, data_snapshot, review_status
Model:      <domain>-<task>
Alias:      champion, canary, shadow, previous
Artifact:   model/, eval/, data_profile/, logs/, source/
```

约定要写进模板与 CI，而不是只放在 Wiki。没有自动检查的命名规范，三个月后必然退化。

## 3.13 与真实 MLflow 的差距

- 真实 MLflow 的 REST 字段、Metric 时间精度、分页 token、Tag 长度等与 Mini 不同。
- 真实 MLflow 支持嵌套 run、数据集输入、模型签名 tensor spec、更多 artifact 后端。
- 真实 MLflow 的 Model Registry 阶段已被标记为 legacy，迁移到 alias 时要注意兼容。
- MiniMLflow 的 Trace 存储是同步单表，真实实现有异步批量写入与采样。

## 小结

- Experiment 是问题边界，Run 是一次执行；不要把两者混为一谈。
- Param 不可变，Metric 是历史序列，Tag 是可修正上下文，Artifact 是字节。
- Model Version 是指向 source 的不可变指针，Alias 是可回滚的发布指针。
- Trace 是 GenAI 时代的调用链观测单位，不能硬塞进 Run。

## 练习

1. 为“客服工单自动分类”设计 Experiment、Run 命名和 8 个核心字段。
2. `val_auc` 与 `val_auc@threshold=0.5`，哪种 key 更好？为什么？
3. 一个模型版本指向的 artifact 被误删，如何发现并恢复？列出三种手段。

---

下一章：[第 4 章 15 分钟快速上手](04-quickstart.md)
