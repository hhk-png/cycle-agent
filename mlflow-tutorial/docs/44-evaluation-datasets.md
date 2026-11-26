# 第 44 章 评测数据集：从 `log_input` 到可运营的 Golden Set

> 本章把“数据”从 run 的一个附属字段提升为一等资产。MiniMLflow 新增 `datasets` / `dataset_records` 两张表、`DatasetRegistry`、REST/客户端/CLI、示例 17 与 12 个测试；真实 MLflow 3.x 的 `mlflow.genai.datasets` 与 `mlflow.data` 在本章逐项对照。数据集 API 在 3.x 中仍快速演进，执行前以锁定版本的 `help()` 与官方文档为准。

## 学习目标

- 区分 run input、dataset、evaluation record 与 artifact，知道每种对象回答什么问题。
- 设计数据集的五个身份：`dataset_id`、name、version、digest、schema/profile。
- 理解“追加记录 + 版本递增 + 内容摘要”的一致性要求，以及并发合并为什么会算错 digest。
- 把线上 trace、人工 feedback 和 expectation 变成可回归的 golden set，而不是一次性 Notebook。
- 为评测数据建立治理边界：PII、许可证、污染、切片、保留期与访问控制。
- 把 Mini 的 `DatasetRegistry` 映射到真实 MLflow 3.x 的 `create_dataset` / `merge_records` / `search_datasets` / `mlflow.genai.evaluate`。

## 44.1 为什么数据版本比模型版本更难

模型版本是字节：同一个文件、同一个 digest，复制到哪里都一样。数据版本不是：

- 同一个 S3 前缀，今天的 Parquet 可能已经被覆盖；
- 同一个 SQL 查询，明天会返回新分区；
- 同一个 CSV 文件名，可能是“最终版(3)”的又一次手工导出；
- 同一批样本，标注规则改了，标签含义就变了。

第 8 章的 `log_input` 解决的是“这次训练指向了哪份数据”，但它只记录**指针和摘要**，不保存评测样本，也不管理标注。第 43 章反复强调 point-in-time correctness，但数据平台只保证“取数时点”，不保证“评测集本身可复现”。

一个真实事故：

```text
团队用 200 条人工标注的客服问答做回归评测。
有人为了让新 prompt 通过，删掉了 30 条“太难”的样本。
评测分数从 0.78 涨到 0.86，模型上线后线上满意度反而下降。
三个月后，没人能说清当时评测集到底有哪 200 条。
```

这不是模型问题，而是**评测数据没有身份、版本和变更审计**。数据集要像模型一样被注册、评审和追踪。

## 44.2 四种“数据”不要混

| 对象 | 回答的问题 | 生命周期 | Mini 对应 |
|---|---|---|---|
| Run input | 这次训练/评测指向了哪份数据？ | 跟随 run，不可变 | `run_inputs` + `log_input` |
| Dataset | 我们认可哪一批评测样本？ | 独立资产，可合并/删除 | `datasets` |
| Evaluation record | 单条样本的输入、期望、标签是什么？ | 数据集内一行 | `dataset_records` |
| Artifact | 原始文件/报告字节在哪里？ | 对象存储，可能被 GC | artifact store |

四个常见错误：

1. **把 dataset 当成 artifact**：上传一个 `eval.jsonl` 到 run，然后说“评测集在 artifact 里”。问题是无法按样本查询、无法合并、无法知道哪条被删了。
2. **把 run input 当成 dataset**：`log_input` 只说明“训练用过这份数据”，不说明“这是评审通过的 golden set”。
3. **把 feedback 当成 expectation**：feedback 是“这次输出好不好”，expectation 是“正确答案/必须满足什么”。前者是评价，后者是数据。
4. **把评测集当成训练集**：一旦评测样本进入训练，后续分数就不再是无偏估计。数据集需要显式的用途标签与访问边界。

## 44.3 数据集的五个身份

### 44.3.1 `dataset_id`：不可变身份

`dataset_id` 是系统生成的标识，创建后不变。名称可以重复、可以改，但 ID 不会。REST、Trace、评测报告和审计日志都应该引用 ID，而不是名称。

### 44.3.2 name：人类标签，不是主键

名称用于搜索和展示。两个团队可能都叫 `support-eval`，这不应该导致覆盖。真实 MLflow 的 `create_dataset(name=...)` 也返回独立数据集；名称不是唯一键。

### 44.3.3 version：合并次数，不是内容哈希

每次合并记录，`version` 递增。它表达“这是第几次变更”，便于审计和乐观并发控制，但**不保证不同数据集之间可比**：v3 可能只加了一条样本，也可能重写了全部标签。

### 44.3.4 digest：内容身份

digest 是记录集合的规范化哈希。Mini 的实现：

```python
def _digest(records):
    payload = compact_json(list(records)).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()
```

`compact_json` 会排序对象的 key、去掉多余空白，因此：

- 同样的记录、同样的顺序，在任何机器上得到同样的 digest；
- 改变一个字段、增删一条记录、调整记录顺序，digest 都会变；
- digest 不包含名称、标签、来源，所以“同一批样本换了名字”仍可识别为同一内容。

**顺序敏感是刻意的**。评测样本的顺序会影响抽样、分桶和人工评审；如果希望“集合语义”的 digest，应先按规范化 key 排序记录再哈希，并在文档里写清楚。

### 44.3.5 schema/profile：形状与分布

schema 描述输入字段的类型；profile 描述记录数、字段集合、空值计数等。Mini 从记录推导，不允许用户手填，避免“元数据说一套、数据是另一套”。

```json
{
  "schema": {"locale": "string", "question": "string"},
  "profile": {
    "record_count": 3,
    "input_keys": ["locale", "question"],
    "expectation_keys": ["answer"],
    "null_inputs": {}
  }
}
```

据当前文档，真实 MLflow 的 dataset profile 可能包含更丰富的观测（分位数、缺失率、类别分布等，随版本变化）；原则相同：**profile 是从数据算出来的观测，不是营销文案**。以你锁定版本的字段为准。

## 44.4 记录契约：`inputs` / `expectations` / `tags`

Mini 的记录只允许三个顶层字段：

```json
{
  "inputs": {"question": "What is the refund window?", "locale": "en"},
  "expectations": {"answer": "30 days"},
  "tags": {"slice": "policy", "difficulty": "easy"}
}
```

规则：

- `inputs` 必须是非空对象，key 是非空字符串；
- `expectations` 可为空对象，表达“正确答案/必须满足的条件”；
- `tags` 是字符串键值，用于切片、来源、难度、语言等；
- 整条记录必须可 JSON 序列化，且拒绝 `NaN` / `Infinity`；
- 单条记录默认不超过 1 MiB，单个数据集不超过 16 MiB、5 万条。

为什么不允许任意顶层字段？因为评测工具需要一个稳定契约来定位输入、期望和元数据。允许 `{"prompt": ..., "answer": ..., "metadata": ...}` 这类自由结构，会让每个 scorer 都写一套适配层，最终无法跨团队复用。

为什么拒绝 NaN？JSON 标准不支持 NaN；Python 的 `json.dumps` 默认却会输出 `NaN`，很多下游解析器会失败。在边界拒绝，比在评测报告里发现 `nan` 更便宜。

### 44.4.1 expectation 与 feedback 的关系

第 33 章的 Assessment 已经区分 `is_expectation`：

- `feedback`：评审对某次输出的判断，例如 `quality=0.3`；
- `expectation`：这批输入应该得到的答案/约束，例如 `answer="30 days"`。

数据集存的是后者。把线上低分 trace 的 feedback 变成 expectation，需要人工确认，不能直接把“模型当时的输出”当成“正确答案”。

## 44.5 真实 MLflow 3.x 的数据集能力

### 44.5.1 训练数据血缘：`mlflow.data`

真实 MLflow 用 `mlflow.data` 构造数据集对象，再 `log_input`：

```python
import mlflow
import mlflow.data
from mlflow.data.pandas_dataset import PandasDataset

dataset: PandasDataset = mlflow.data.from_pandas(
    df,
    source="s3://warehouse/churn/2026-11-20.parquet",
    name="churn_snapshot",
    targets="churned_30d",
)
with mlflow.start_run() as run:
    mlflow.log_input(dataset, context="training")
```

它会记录 name、digest、source、source_type、context，并在可推断时记录 schema/profile。搜索字段是复数形式：`datasets.name`、`datasets.digest`、`datasets.context`。第 8 章已经解释 Mini 的 `EXISTS` 语义：两个条件不保证指向同一条输入。

### 44.5.2 评测数据集：`mlflow.genai.datasets`

真实 MLflow 3.x 把评测数据集做成独立资产：

```python
from mlflow.genai.datasets import create_dataset, get_dataset, search_datasets

dataset = create_dataset(
    name="support-regression",
    experiment_id=["1"],
    tags={"owner": "cx", "stage": "golden"},
)

dataset.merge_records(
    [
        {
            "inputs": {"question": "What is the refund window?"},
            "expectations": {"answer": "30 days"},
            "tags": {"slice": "policy"},
        }
    ]
)

loaded = get_dataset(dataset_id=dataset.dataset_id)
hits = search_datasets(
    experiment_ids=["1"],
    filter_string="tags.stage = 'golden' AND name LIKE '%support%'",
    order_by=["last_update_time DESC"],
    max_results=10,
)
```

关键语义（以锁定版本为准）：

- `merge_records` 按 `inputs` 的哈希匹配：相同 inputs 的记录不会重复插入，而是把新的 expectations/tags 合并进已有记录；inputs 有任何差异（哪怕多一个 `temperature` 键）都会成为新记录。
- 记录来源可自动推断：来自 trace 的记录、带 expectation 的记录、只有 inputs 的记录使用不同的 source type；显式 `source` 可覆盖。
- `EvaluationDataset` 提供 `.records`、`.to_df()`、`.to_dict()`、`.schema`、`.profile`、`.tags`、`.dataset_id`、`.name`，以及 `delete_records(record_ids)`。注意不同版本/不同来源（`get_dataset` 与 `search_datasets` 返回对象）的懒加载行为不完全一致；统计记录数时 `.to_df()` 通常更稳。
- `search_datasets` 支持 `name`、`tags.<key>`、`created_by`、`last_updated_by`、`created_time`、`last_update_time` 等字段，操作符包括 `=`、`!=`、`LIKE`/`ILIKE`、比较运算和 `AND`；当前不支持 `OR`。
- 评测数据集需要 SQL 后端；FileStore 不支持。
- Databricks 托管版与开源版的可用 API、`experiment_ids` 支持情况可能不同。

### 44.5.3 直接喂给评测

```python
from mlflow.genai.scorers import Correctness, RelevanceToQuery

result = mlflow.genai.evaluate(
    data=loaded,              # EvaluationDataset、DataFrame 或 list[dict]
    predict_fn=agent.invoke,
    scorers=[Correctness(), RelevanceToQuery()],
)
```

`data` 不必须是受管数据集；但受管数据集带来版本、搜索、复用和审计。小团队可以从 list[dict] 起步，等评测集超过几十条、需要多人评审时再升级。

## 44.6 Mini 实现：表、事务与不变量

### 44.6.1 表结构

```sql
CREATE TABLE datasets (
    dataset_id        TEXT PRIMARY KEY,
    name              TEXT NOT NULL,
    version           INTEGER NOT NULL CHECK (version >= 1),
    digest            TEXT NOT NULL DEFAULT '',
    source            TEXT NOT NULL DEFAULT '',
    source_type       TEXT NOT NULL DEFAULT '',
    schema_json       TEXT NOT NULL DEFAULT '{}',
    profile_json      TEXT NOT NULL DEFAULT '{}',
    tags_json         TEXT NOT NULL DEFAULT '{}',
    record_count      INTEGER NOT NULL DEFAULT 0 CHECK (record_count >= 0),
    created_time      INTEGER NOT NULL,
    last_updated_time INTEGER NOT NULL
);

CREATE TABLE dataset_records (
    dataset_id  TEXT NOT NULL,
    record_id   INTEGER NOT NULL,
    record_json TEXT NOT NULL,
    PRIMARY KEY (dataset_id, record_id),
    FOREIGN KEY (dataset_id) REFERENCES datasets(dataset_id) ON DELETE CASCADE
);
```

为什么记录单独一张表，而不是把整个数组塞进一个 JSON 列？

- 元数据查询（搜索、列表）不必加载全部记录；
- 未来可以按 `record_id` 删除/修正单条记录，而不重写整列；
- 可以给记录加索引、做分页、做审计；
- 外键级联保证删除数据集时记录不会成为孤儿。

Mini 的 `get_dataset` 仍会一次性加载全部记录，这是教学简化；生产应支持分页和按需加载。

### 44.6.2 创建：验证、推导、落库

`DatasetRegistry.create_dataset` 的顺序是：

1. 校验名称、来源、标签；
2. 逐条校验记录并计算规范化 JSON 大小；
3. 推导 schema/profile；
4. 计算 digest；
5. 在一个事务里插入数据集与全部记录；
6. 发出 `dataset.created` 事件。

```python
cleaned = _clean_records(records)
schema, profile = _schema_and_profile(cleaned)
dataset = Dataset(
    dataset_id=new_id(),
    name=name,
    version=1,
    digest=_digest(cleaned),
    schema=schema,
    profile=profile,
    records=cleaned,
    record_count=len(cleaned),
    ...
)
stored = self.store.create_dataset(dataset)
```

创建失败时不能留下“有数据集、没记录”的半成品；`create_dataset` 的插入在同一个 SQLite 事务里完成。

### 44.6.3 合并：为什么需要 CAS

最直观的实现是：

```text
读现有记录 -> 追加新记录 -> 计算新 digest -> 写回
```

两个并发 merge 会同时读到 v1：

```text
A: 读 2 条 -> 算 digest(2+1) -> 写 v2
B: 读 2 条 -> 算 digest(2+1) -> 写 v2
```

如果 B 的读取发生在 A 提交之前，B 写回的 digest 只覆盖“2 条旧记录 + 自己的 1 条”，但表里实际有 4 条。**记录数对、digest 错**，这是最难排查的一类数据事故。

Mini 用版本 CAS（compare-and-swap）解决：

```python
# store 内，事务中检查
if int(current["version"]) != expected_version:
    raise Conflict(...)
```

服务层捕获 `Conflict`，重新读取最新快照、重新计算、重试（最多 8 次）。SQLite 的 `BEGIN IMMEDIATE` 保证写事务串行化，CAS 保证“计算所依据的快照”没有被别的写者改过。

这不是分布式系统的完整答案：多进程/多数据库仍需要数据库级行锁、乐观锁列或串行化隔离。但它把“单机多线程”这个最常见的教学场景做对了。

### 44.6.4 合并的语义取舍

真实 MLflow 按 `inputs` 哈希去重并合并 expectations/tags；Mini 刻意采用**追加**语义：

- 相同 inputs 的两次提交会产生两条记录；
- 版本仍然递增，digest 覆盖全部记录；
- 优点：实现简单、记录顺序明确、不会静默覆盖人工标注；
- 缺点：重复样本会放大评测权重，重试可能重复写入。

这是一个必须写进文档的差异。生产化有两个方向：

1. 在应用层按 `inputs` 哈希去重，再调用 `merge_records`；
2. 在存储层增加 `input_digest` 唯一索引，把去重与合并放进事务。

第 44.10 节把“实现 input-digest 去重”列为练习。

## 44.7 搜索：字段、顺序与分页

Mini 的 `search_datasets` 支持：

| 字段 | 含义 |
|---|---|
| `attributes.dataset_id` | 数据集 ID |
| `attributes.name` | 名称 |
| `attributes.version` | 合并次数 |
| `attributes.digest` | 内容摘要 |
| `attributes.source` / `source_type` | 来源 |
| `attributes.record_count` | 记录数 |
| `attributes.created_time` / `last_updated_time` | 时间 |
| `tags.<key>` | 数据集标签 |

示例：

```python
golden = client.search_datasets(
    "tags.stage = 'golden' AND attributes.record_count >= 100",
    order_by=["attributes.last_updated_time DESC"],
    max_results=20,
)
```

Mini 的实现是“加载全部元数据后在内存过滤”，与 Prompt 搜索同一模式；它复用了第 9 章的词法/语法/AST，但没有把过滤下推到 SQL。数据集成千上万时，应改成 SQL 过滤 + 分页，并只返回元数据。

三个容易踩的语义坑：

1. **搜索返回元数据，不返回记录**。`records` 为空是正常的，用 `get_dataset` 取完整记录；`record_count` 仍然是准确的。
2. **`AND` 不是“同一字段的同一记录”**。数据集级搜索只有一行元数据，不存在记录级组合条件；要按记录过滤，需要 `search_records` 一类的新 API。
3. **排序字段必须白名单**。不要把用户输入直接拼进 `ORDER BY`；Mini 用正则 + 字段集合，未知字段直接报错。

## 44.8 从 trace 到 golden set：一条可运营的链路

线上失败样本是最好的评测素材，但不能直接拿来用：

```text
线上 trace
  → 按 assessment 过滤（低分/人工差评/安全告警）
  → 脱敏、去重、补充 expectation
  → 进入数据集（带来源标签）
  → 固定版本跑回归
  → 评审通过后标记 stage=golden
```

Mini 的拼装方式：

```python
bad = client.search_traces(
    filter_string="assessments.quality < 0.5",
    max_results=50,
)
records = []
for trace in bad:
    records.append({
        "inputs": {"question": trace.request["question"]},
        "expectations": {},          # 人工补充，不能拿旧输出当答案
        "tags": {
            "source": "online-trace",
            "trace_id": trace.trace_id,
            "slice": trace.tags.get("intent", "unknown"),
        },
    })
dataset = client.create_dataset(
    "support-failures-2026-11",
    records,
    source="trace://support-agent",
    source_type="trace",
    tags={"stage": "candidate", "pii": "redacted"},
)
```

注意：

- **不能把模型当时的输出直接当 expectation**。那会把模型的错误固化成“标准答案”。
- **必须脱敏**。trace 里可能含订单号、邮箱、手机号；进入数据集前要脱敏或哈希。
- **必须去重**。同一用户重复提问会产生高度相似的样本；重复样本会放大某个切片的权重。
- **必须保留来源**。`trace_id` 放在记录 tag 里，便于从评测失败回溯到原始请求。
- **必须版本化**。数据集 ID + version 要写进评测报告，否则“分数提升”无法复现。

真实 MLflow 的 Review Queue（第 37、39 章）把“人工补充 expectation”做成可分配任务；Mini 只提供 assessment 与数据集，没有队列。

## 44.9 数据治理：比 API 更难的部分

### 44.9.1 PII 与许可证

- 数据集可能包含个人信息、合同、工单、代码；进入评测集前要脱敏、审批、记录用途。
- 第三方数据要记录许可证与到期时间；许可证过期的数据集不应继续参与发布门禁。
- 删除请求（DSAR）要能定位到具体记录，而不是“删整个数据集”。

Mini 只存记录与标签，不实现字段级加密、访问控制或保留期；这些是平台能力，不是数据集 API 能自动解决的。

### 44.9.2 污染与泄漏

- 评测集样本进入训练集 → 分数虚高；
- 训练时用未来数据 → 时间泄漏；
- 同一用户/同一会话的样本跨 train/test → 分组泄漏；
- 用 judge 生成 expectation，再用同一 judge 评测 → 自我偏好。

控制手段：数据集用途标签、训练/评测隔离、按时间/用户分组切分、记录数据来源与生成方式、对 judge 做人工校准。

### 44.9.3 切片与样本量

总体分数会掩盖切片退化。数据集应显式记录切片标签（语言、租户、意图、难度、渠道），并在评测报告中分组。小切片要报告样本量与置信区间，避免把噪声当提升。

### 44.9.4 保留期与成本

评测集不是越大越好：

- 存储与加载成本随记录数线性增长；
- 人工评审成本随样本数增长；
- 过时的评测集会让模型过拟合历史分布。

建议保留“核心 golden set + 按季度轮换的挑战集”，并定期删除低价值/重复样本。删除要留下审计记录，而不是静默消失。

### 44.9.5 与数据版本系统的边界

MLflow 的 dataset 不是湖仓表格式，也不是数据版本系统。几类工具的职责不同：

| 系统 | 主要解决 | 与 MLflow dataset 的关系 |
|---|---|---|
| Iceberg / Delta Lake | 表级快照、时间旅行、schema 演进 | 提供 source/digest 的底层快照；MLflow 记录“用了哪个快照” |
| DVC / LakeFS | 文件级版本、分支、数据 CI | 可作为 source；MLflow 不复制字节 |
| 特征库 | point-in-time 特征、线上/离线一致 | 训练数据血缘的另一半；MLflow 不保证特征正确性 |
| 数据目录 / Unity Catalog | 资产发现、权限、许可 | 可能接管 dataset 的治理；需要明确 system of record |
| MLflow Dataset | 评测样本、expectation、版本、与 run/评测绑定 | 专注“评测资产”，不替代上述系统 |

选型时先回答：**你要版本化的是表、文件、特征，还是评测样本？** 四者可以组合，但不能用一个工具的“版本”概念硬套另一个。第 26 章讲迁移与互操作，第 43 章讲数据契约，本节只划边界。

## 44.10 与真实 MLflow 的差距

| 能力 | Mini | 真实 MLflow 3.x / 平台 |
|---|---|---|
| 归属 | 全局（无 `experiment_id`） | 实验归属，搜索可按实验过滤 |
| 数据集身份 | `dataset_id` + name + version + digest | `dataset_id`、实验归属、创建者/更新者 |
| 记录模型 | `inputs` / `expectations` / `tags` | 更丰富的 schema、来源推断、trace 关联 |
| 合并语义 | 追加，版本递增 | 按 inputs 哈希去重并合并 expectations/tags |
| 搜索 | 元数据内存过滤，复用通用 DSL | `search_datasets`，字段/操作符更全，分页 |
| 记录级操作 | 无 | `delete_records`、按记录 ID 更新 |
| 评测集成 | 手动取记录 | `mlflow.genai.evaluate(data=dataset)` |
| UI | 无数据集页面 | 数据集列表、记录编辑、从 trace 构建 |
| 权限/审计 | 单 token | workspace/RBAC、审计、数据集级权限 |
| 存储 | SQLite | SQL 后端；FileStore 不支持评测数据集 |
| 治理 | 标签约定 | 数据目录/Unity Catalog、许可证、保留期 |

一个必须强调的差异：**真实 MLflow 的 `merge_records` 会按 inputs 哈希去重**。如果你从 Mini 迁移到真实 MLflow，原来“追加两次”的脚本会变成“合并成一条”，记录数与 digest 都会不同。迁移前要跑一次对账，确认重复样本是否符合预期。

## 44.11 练习

1. 给 `dataset_records` 增加 `input_digest` 列与唯一索引，实现“相同 inputs 合并 expectations/tags”的语义；写一个并发测试证明不会重复插入。
2. 为 `search_datasets` 增加 `attributes.record_count` 的范围过滤与 keyset 分页，说明为什么 offset 分页在频繁合并下会漏数据。
3. 设计一个“从 trace 生成候选数据集”的 Job：如何脱敏、如何去重、如何补充 expectation、如何记录来源？
4. 给数据集增加 `stage`（draft/candidate/golden/retired）与 `license` 标签，写出发布门禁只允许 `golden` 的规则。
5. 写一个对账脚本：比较两个数据集的 digest、记录数、schema，输出“新增/删除/修改”的样本 ID。
6. 解释为什么 `merge_records` 的 version 不能替代 digest；举一个“version 相同但内容不同”的场景。
7. 为数据集设计保留期策略：按 stage、最后使用时间、许可证到期时间分别怎么处理？
8. 把第 43 章的流失预警评测集改造成受管数据集，记录数据契约、切片和门禁报告。

## 小结

- 评测数据是资产，不是 run 的附属字段；它需要身份、版本、digest、schema/profile 和审计。
- run input 记录“指向哪份数据”，dataset 记录“我们认可哪批样本”，expectation 记录“正确答案是什么”，三者不能混。
- 合并是读-算-写，必须用事务 + 版本 CAS 防止并发写回陈旧 digest；追加语义与真实 MLflow 的去重语义不同，迁移前要对账。
- 从 trace 构建 golden set 需要脱敏、去重、人工补充 expectation 和来源标签；不能拿旧输出当标准答案。
- 数据治理（PII、许可证、污染、切片、保留期）比 API 更难，也是评测能否上生产的分水岭。
- Mini 实现了最小闭环（12 个测试、示例 17）；真实 MLflow 的 `mlflow.genai.datasets` 在合并语义、搜索、记录级操作和评测集成上更完整。
- 把 Agent 会话转成评测记录见第 46 章的 `sessions_to_dataset_records`；转换只是候选，进入 golden set 前仍要脱敏、筛选与人工补充 expectation。

---

下一章：[第 45 章 上线之后：模型监控、漂移检测与再训练闭环](45-monitoring-drift.md)

---

返回：[教程首页](../README.md) · 上一章：[第 43 章 综合实战：把流失预警做成可运营的 AI 产品](43-capstone-churn.md)
