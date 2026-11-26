# 第 7 章 SQLite 存储层：Schema、事务与并发

## 学习目标

- 从 DDL 理解 MLflow 式元数据模型：实验、run、指标历史、注册表、trace。
- 理解 WAL、单写者、短事务和 `busy_timeout` 的实际含义。
- 学会用“软删除 + 历史表”同时满足审计与查询。
- 识别 N+1、参数上限、迁移和并发升级死锁等隐患。

## 7.1 表清单

MiniMLflow 的 schema 在 `mini_mlflow/store/schema.sql`，当前 schema v7。Tracking 主链的表：

```text
experiments
experiment_tags
runs
run_params
run_tags
metrics
run_inputs
registered_models
registered_model_tags
model_versions
model_version_tags
model_version_aliases
traces
spans
assessments
prompts
prompt_versions
prompt_aliases
webhooks
webhook_deliveries
schema_meta
```

后续章节新增的表（本章只讲 Tracking 主链，概念分别见第 40、44、45 章；第 46 章的 agentops 是 trace tag 的派生视图，不新增表）：

```text
logged_models              # 第 40 章：模型实体
logged_model_tags
logged_model_params
logged_model_metrics
datasets                   # 第 44 章：评测数据集
dataset_records
monitors                   # 第 45 章：监控定义
monitor_windows            # 第 45 章：不可变评估窗口
```

关系：

```text
experiments 1 ── N runs 1 ── N run_params
                         ├── N run_tags
                         ├── N metrics (append-only)
                         └── N run_inputs

experiments 1 ── N logged_models 1 ── N logged_model_*
runs        1 ── N logged_models (source_run_id, ON DELETE SET NULL)

registered_models 1 ── N model_versions 1 ── N model_version_tags
        │                         └── N aliases (alias → version)
        └── N model_tags

traces 1 ── N spans

prompts 1 ── N prompt_versions 1 ── N prompt_aliases

datasets 1 ── N dataset_records

monitors 1 ── N monitor_windows

webhooks 1 ── N webhook_deliveries   (outbox；删除 webhook 级联删除投递)
```

外键**并非全部** `ON DELETE CASCADE`：绝大多数子表级联删除；`logged_models.source_run_id` 故意用 `ON DELETE SET NULL`，这样删除 run 不会带走模型实体。注册表相关外键额外 `ON UPDATE CASCADE`，以支持注册模型重命名。

## 7.2 DDL 中的设计决策

### 实验与默认实验

```sql
CREATE TABLE experiments (
  experiment_id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  artifact_location TEXT NOT NULL,
  lifecycle_stage TEXT NOT NULL CHECK (lifecycle_stage IN ('active','deleted')),
  creation_time INTEGER NOT NULL,
  last_update_time INTEGER NOT NULL
);
```

- `name UNIQUE` 在**所有**实验中唯一，包括已删除实验。这样恢复语义明确；代价是删除后不能立即用同名创建，必须恢复或改名。
- `experiment_id=0` 是 Default，由存储在初始化时 `INSERT OR IGNORE`，artifact 路径来自配置。
- `lifecycle_stage` 用 CHECK 约束，防止应用写错状态。

### Run 与软删除

```sql
CREATE TABLE runs (
  run_id TEXT PRIMARY KEY,
  experiment_id INTEGER NOT NULL,
  status TEXT NOT NULL CHECK (...),
  lifecycle_stage TEXT NOT NULL DEFAULT 'active',
  deleted_time INTEGER,
  ...
);
```

为什么不物理删除？

- 误删可恢复；
- 审计需要“曾经存在”；
- 外键下的 metrics/params 不会瞬间消失。

删除实验时，Mini 把实验和其 active run 都标记为 deleted，并在 run 上记录 `deleted_with_experiment=1`。恢复实验只恢复这些“随实验删除”的 run；此前被单独删除的 run 保持删除。`restore_run` 会检查所属实验是否仍为 deleted，若是则拒绝，避免出现“active run 挂在 deleted 实验下”的非法状态。这比“删除来源”更细的一步是审计事件表，生产应补上。

### 指标历史

```sql
CREATE TABLE metrics (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL,
  key TEXT NOT NULL,
  value REAL NOT NULL,
  timestamp INTEGER NOT NULL,
  step INTEGER NOT NULL DEFAULT 0 CHECK (step >= 0),
  FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
);

CREATE INDEX idx_metrics_run_key_latest
  ON metrics(run_id, key, timestamp DESC, id DESC);
```

- 追加式写入保留曲线。
- 最新值由 `ROW_NUMBER() OVER (PARTITION BY run_id,key ORDER BY timestamp DESC,id DESC)` 选出。
- `id` 作为同时间戳的稳定次序，避免结果随机。
- 索引顺序与查询一致；如果反过来 `(key, run_id, ...)`，单 run 的曲线查询会变慢。

### 注册表

```sql
CREATE TABLE model_versions (
  name TEXT NOT NULL,
  version INTEGER NOT NULL,
  source TEXT NOT NULL,
  run_id TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL,
  current_stage TEXT NOT NULL DEFAULT 'None',
  ...
  PRIMARY KEY (name, version),
  FOREIGN KEY (name) REFERENCES registered_models(name)
    ON DELETE CASCADE ON UPDATE CASCADE
);
```

版本号是 `MAX(version)+1`，在事务中计算。真实高并发下需要唯一约束 + 重试，或者由数据库序列分配；Mini 的单写者降低了风险，但没有消除“两个连接同时读到相同 MAX 再插入”的可能。第 20 章给出重试方案。

### Trace 与 Span

```sql
CREATE TABLE traces (
  trace_id TEXT PRIMARY KEY,
  experiment_id INTEGER NOT NULL DEFAULT 0,
  name TEXT NOT NULL,
  timestamp INTEGER NOT NULL,
  status TEXT NOT NULL,
  tags_json TEXT NOT NULL DEFAULT '{}',
  request_json TEXT,
  response_json TEXT
);

CREATE TABLE spans (
  trace_id TEXT NOT NULL,
  span_id TEXT NOT NULL,
  parent_span_id TEXT,
  start_time_ns INTEGER NOT NULL,
  end_time_ns INTEGER,
  inputs_json TEXT,
  outputs_json TEXT,
  ...
  PRIMARY KEY (trace_id, span_id)
);
```

JSON 列是在“快速迭代”和“可查询性”之间的取舍：Trace 负载结构变化快，不适合为每个属性建列；代价是无法高效按任意属性过滤。Mini 的 trace 过滤在服务层对有限结果做 Python 求值，生产应使用专用 trace 存储/OLAP。

## 7.3 连接与并发模型

```python
self._local = threading.local()

def _connection(self):
    if not hasattr(self._local, "conn"):
        conn = sqlite3.connect(path, timeout=busy_timeout_ms / 1000)
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
    return conn
```

要点：

- **每线程一个连接**。SQLite 连接不是线程安全的共享对象；`check_same_thread=False` 只是关闭检查，不是让共享变安全。
- **WAL** 允许读写并发，但仍是单写者。多个写事务会串行。
- **`busy_timeout`** 让写锁等待，而不是立即 `database is locked`。
- **短事务**：批量写在同一事务内，但不要在事务里做网络/文件 IO。
- **`synchronous=NORMAL`** 在 WAL 下是常见折中；崩溃可能丢最近事务，但不会损坏库。强持久性用 `FULL`。

### 事务模式与锁升级

SQLite 默认 `DEFERRED` 事务：先读后写时，如果另一个连接已写并提交，当前连接可能无法升级锁，`busy_timeout` 也救不了。

Mini 的 `SqliteStore.transaction()` 在事务开始时执行 `BEGIN IMMEDIATE`，让“检查 run 是否终态”和“写入”处于同一个写事务中，避免“检查通过后另一个连接先结束 run，随后指标才落库”的竞态。代价是所有写事务串行化，写吞吐受单写者限制。

生产仍需要：

- 对版本号分配等“读-改-写”操作加应用层重试（Mini 在 Registry 层做了有限重试）；
- 数据库连接池/超时；
- 或直接换 Postgres，使用合适隔离级别。

这是面试和 code review 常问的点：**并发正确性不是“有事务”就自动成立，还要看事务从哪一刻开始。**

## 7.4 批量读取：避免 N+1

`search_runs` 返回一页 run，需要 metrics/params/tags。朴素实现会对每个 run 查三次，50 条就是 150 次查询。Mini 的做法：

```python
# 1. 先取 run 行
rows = conn.execute("SELECT r.* FROM runs r WHERE ... LIMIT ? OFFSET ?")

# 2. 对 run_ids 分批 IN 查询
_run_data(run_ids)  # 每类一张表，一次取回
```

`IN` 的参数受 SQLite 变量上限约束（通常 999），所以用 400 一批 `chunked`。指标最新值用窗口函数一次算完，而不是每个 run 一条子查询。

代价：SQL 更长、窗口函数在超大表上可能慢；可以物化 `latest_metrics` 表或用缓存。第 25 章讨论。

## 7.5 搜索 SQL 的形态

服务层把 DSL 编译成：

```sql
SELECT r.* FROM runs r
WHERE r.lifecycle_stage = 'active'
  AND (EXISTS (
      SELECT 1 FROM metrics m
      WHERE m.run_id = r.run_id AND m.key = ?
        AND m.value > ?
        AND m.id = (
          SELECT m2.id FROM metrics m2
          WHERE m2.run_id = m.run_id AND m2.key = m.key
          ORDER BY m2.timestamp DESC, m2.id DESC LIMIT 1
        )
  ))
ORDER BY r.start_time DESC, r.run_id ASC
LIMIT ? OFFSET ?
```

所有用户值都是 `?` 绑定参数；字段名来自白名单映射。性能上：

- `runs(lifecycle_stage, start_time)` 索引可用于粗筛；
- 每个 metric 条件是一次相关子查询，多个条件会叠加；
- 超大数据量应做倒排/宽表/OLAP，而不是继续优化这条 SQL。

## 7.6 分页

Mini 使用 base64 JSON 的 offset token：

```json
{"offset": 100}
```

优点：实现简单、无状态。
缺点：深分页会 `OFFSET 100000` 扫描；并发插入会导致重复/漏项。

生产建议：

- 按 `(start_time, run_id)` 的 keyset 分页；
- token 携带最后一条的排序键；
- 明确一致性级别（快照/最终一致）。

## 7.7 初始化与迁移

```python
conn.executescript(schema.sql)
# 新库写入当前版本；已有库按 row["value"] 分支升级
INSERT OR IGNORE INTO schema_meta(key, value) VALUES ('version', SCHEMA_VERSION)
```

当前 `SCHEMA_VERSION = "7"`。初始化逻辑（`sqlite_store.py`）是：

- 新库：执行幂等 DDL，写入 7；
- 已有 v1–v6：执行同一套 `CREATE TABLE IF NOT EXISTS`（以及必要的 `ALTER TABLE`），把版本更新为 7，不改旧行数据；
- 未知版本（例如未来版本或手工改坏的值）：拒绝启动，而不是猜测迁移。

真实系统还需要：

- 版本化迁移脚本（alembic 等）与回滚；
- 向前兼容的滚动升级；
- 大表在线加列/建索引；
- 备份与回滚演练。

Mini 的“幂等 DDL + 版本号”只适合教学；它不能处理重命名、类型变更、数据回填和回滚。第 26、36 章给出生产迁移清单。

### Webhook outbox 的存储取舍

`webhook_deliveries` 是“先落库、再投递”的 outbox：`drain_once` 只取 `status='PENDING' AND next_attempt_time <= now`，成功后改 `DELIVERED`，达到重试上限改 `FAILED`。这保证服务重启后仍能继续投递，但**不保证**业务写入与 outbox 插入在同一个事务里——第 38 章把这一点列为生产改造的第一优先级。

## 7.8 数据完整性不变量

存储层强制：

- 外键级联；
- run status/lifecycle 枚举；
- metric step 非负；
- 参数主键 `(run_id,key)`；
- 模型版本主键 `(name,version)`；
- 别名主键 `(name,alias)`；
- span 主键 `(trace_id,span_id)`。

服务层强制：

- 参数不可变；
- 终态 run 不允许写；
- artifact 路径不逃逸；
- trace 必须有 root span；
- model version 的 run_id 必须存在。

**不要把所有规则都塞进数据库**：数据库适合表达结构性不变量；业务规则需要可读的错误信息和演进空间。

## 7.9 与真实 MLflow 的差距

- 真实 MLflow 支持 Postgres/MySQL，使用 SQLAlchemy 和迁移框架。
- Metric 表在不同后端有“最新值表 + 历史表”或分区策略。
- 真实 Registry 还可能把版本与对象存储/云目录绑定。
- 真实删除策略更细，支持 run 恢复和 artifact GC。
- 真实 trace 存储通常有异步写入、采样和专用索引。

## 小结

- Schema 是产品语义的镜像：追加指标、不可变参数、软删除、指针式模型版本。
- WAL 提升读并发但不改变单写者事实；短事务和重试是关键。
- 批量读取避免 N+1；分页在深页数下必须换 keyset。
- 迁移不是可选功能，而是长期运行系统的生存条件。

## 练习

1. 为 `metrics` 表设计一个“按天分区/归档”的策略，并说明查询影响。
2. 两个进程同时创建同一模型的下一个版本，如何保证版本号不冲突？给出 SQL 与应用层方案。
3. 为什么参数不可变规则不放在数据库 CHECK 里？给出至少两个理由。

---

下一章：[第 8 章 Tracking 核心](08-tracking.md)
