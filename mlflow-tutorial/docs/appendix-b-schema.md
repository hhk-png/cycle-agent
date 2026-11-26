# 附录 B 数据库 Schema 与不变量

完整 DDL 见 [`mini_mlflow/store/schema.sql`](../mini_mlflow/store/schema.sql)。

## B.1 ER 图

```text
experiments 1 ─── N runs
     │               ├── N run_params
     │               ├── N run_tags
     │               ├── N metrics
     │               └── N run_inputs
     │
     ├── N experiment_tags
     └── N logged_models
              ├── N logged_model_tags
              ├── N logged_model_params
              └── N logged_model_metrics
                 (source_run_id -> runs.run_id, ON DELETE SET NULL)

registered_models 1 ─── N model_versions
      │                        ├── N model_version_tags
      │                        └── N model_version_aliases
      └── N registered_model_tags

traces 1 ─── N spans
   │
   └── N assessments

prompts 1 ─── N prompt_versions
                    └── N prompt_aliases

datasets 1 ─── N dataset_records
       (记录追加；合并时元数据与记录同一事务)

monitors 1 ─── N monitor_windows
       (基线 profile 在 monitor；窗口不可变)

webhooks 1 ─── N webhook_deliveries
```

## B.2 表与关键列

| 表 | 主键 | 关键列 | 说明 |
|---|---|---|---|
| schema_meta | key | value | schema 版本 |
| experiments | experiment_id | name UNIQUE, artifact_location, lifecycle_stage | 0=Default |
| experiment_tags | (experiment_id,key) | value | 实验标签 |
| runs | run_id | experiment_id, status, lifecycle_stage, deleted_with_experiment, artifact_uri | 软删除与删除来源 |
| run_params | (run_id,key) | value | 不可变 |
| run_tags | (run_id,key) | value | 可变 |
| metrics | id | run_id,key,value,timestamp,step | 追加历史 |
| run_inputs | id | run_id,dataset_name,dataset_digest,dataset_source,dataset_source_type,context | 数据血缘指针；覆盖索引 `(run_id,dataset_name,dataset_digest,context)` 支撑 `datasets.*` 搜索 |
| registered_models | name | description | 逻辑模型 |
| registered_model_tags | (name,key) | value | 注册模型标签 |
| model_versions | (name,version) | source,run_id,status,current_stage | 不可变版本 |
| model_version_tags | (name,version,key) | value | 版本标签 |
| model_version_aliases | (name,alias) | version | 发布指针 |
| logged_models | model_id | experiment_id, name, model_type, source_run_id, status, artifact_location, timestamps | MLflow 3 模型实体；实验删除级联，run 删除置空 |
| logged_model_tags | (model_id,key) | value | 可变标签 |
| logged_model_params | (model_id,key) | value | 不可变参数 |
| logged_model_metrics | id | model_id,key,value,timestamp,step | 追加历史；最新值按 step DESC, timestamp DESC |
| traces | trace_id | experiment_id,status,request_json,... | 请求级 |
| spans | (trace_id,span_id) | parent,start/end,payload | 调用节点 |
| assessments | assessment_id | trace_id,name,value_json,value_type,source_type,source_id,is_expectation | feedback/expectation；唯一身份 `(trace,name,source,is_expectation)` |
| prompts | name | creation/last_update | prompt 逻辑名 |
| prompt_versions | (name,version) | template,commit_message,tags_json | 不可变版本 |
| prompt_aliases | (name,alias) | version | 可发布指针 |
| datasets | dataset_id | name, version, digest, source, source_type, schema_json, profile_json, tags_json, record_count, timestamps | 评测数据集元数据；版本随合并递增 |
| dataset_records | (dataset_id,record_id) | record_json | 不可变记录；`{inputs,expectations,tags}`；删除数据集级联 |
| monitors | monitor_id | name UNIQUE, baseline_json, thresholds_json, model_name/version, baseline_dataset_id/digest | 监控定义；基线派生并带 digest |
| monitor_windows | window_id | monitor_id, window_start/end, sample_count, status, metrics_json, report_json | 不可变评估窗口；删除 monitor 级联 |

第 46 章的 AgentOps 不新增表：会话由 `traces.tags_json` 里的 `mlflow.trace.session` 派生，会话级 assessment 复用 `assessments` 表并挂在首条 trace 上。只有要做跨天分页、会话状态机或权限隔离时，才值得增加独立 `sessions` 表。
| webhooks | webhook_id | name UNIQUE, url, events_json, secret, status | 事件订阅；secret 不对外返回 |
| webhook_deliveries | delivery_id | webhook_id, event, payload_json, status, attempts, next_attempt_time | outbox；至少一次投递 |

## B.3 核心 DDL（节选）

```sql
CREATE TABLE experiments (
  experiment_id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL UNIQUE,
  artifact_location TEXT NOT NULL,
  lifecycle_stage TEXT NOT NULL CHECK (lifecycle_stage IN ('active','deleted')),
  creation_time INTEGER NOT NULL,
  last_update_time INTEGER NOT NULL
);

CREATE TABLE runs (
  run_id TEXT PRIMARY KEY,
  experiment_id INTEGER NOT NULL,
  run_name TEXT NOT NULL DEFAULT '',
  user_id TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL CHECK (status IN ('RUNNING','SCHEDULED','FINISHED','FAILED','KILLED')),
  start_time INTEGER NOT NULL,
  end_time INTEGER,
  artifact_uri TEXT NOT NULL,
  lifecycle_stage TEXT NOT NULL DEFAULT 'active' CHECK (lifecycle_stage IN ('active','deleted')),
  deleted_time INTEGER,
  deleted_with_experiment INTEGER NOT NULL DEFAULT 0,
  FOREIGN KEY (experiment_id) REFERENCES experiments(experiment_id) ON DELETE CASCADE
);

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

注册表：

```sql
CREATE TABLE model_versions (
  name TEXT NOT NULL,
  version INTEGER NOT NULL,
  creation_timestamp INTEGER NOT NULL,
  last_updated_timestamp INTEGER NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL,
  run_id TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL,
  current_stage TEXT NOT NULL DEFAULT 'None',
  PRIMARY KEY (name, version),
  FOREIGN KEY (name) REFERENCES registered_models(name)
    ON DELETE CASCADE ON UPDATE CASCADE
);

CREATE TABLE model_version_aliases (
  name TEXT NOT NULL,
  alias TEXT NOT NULL,
  version INTEGER NOT NULL,
  PRIMARY KEY (name, alias),
  FOREIGN KEY (name, version) REFERENCES model_versions(name, version)
    ON DELETE CASCADE ON UPDATE CASCADE
);
```

LoggedModel：

```sql
CREATE TABLE logged_models (
  model_id TEXT PRIMARY KEY,
  experiment_id INTEGER NOT NULL,
  name TEXT NOT NULL,
  model_type TEXT NOT NULL DEFAULT '',
  source_run_id TEXT,
  status TEXT NOT NULL CHECK (status IN
    ('LOGGED_MODEL_PENDING','LOGGED_MODEL_READY','LOGGED_MODEL_UPLOAD_FAILED')),
  status_message TEXT NOT NULL DEFAULT '',
  artifact_location TEXT NOT NULL,
  creation_timestamp INTEGER NOT NULL,
  last_updated_timestamp INTEGER NOT NULL,
  FOREIGN KEY (experiment_id) REFERENCES experiments(experiment_id) ON DELETE CASCADE,
  FOREIGN KEY (source_run_id) REFERENCES runs(run_id) ON DELETE SET NULL
);

CREATE TABLE logged_model_metrics (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  model_id TEXT NOT NULL,
  key TEXT NOT NULL,
  value REAL NOT NULL,
  timestamp INTEGER NOT NULL,
  step INTEGER NOT NULL DEFAULT 0,
  FOREIGN KEY (model_id) REFERENCES logged_models(model_id) ON DELETE CASCADE
);

CREATE INDEX idx_logged_model_metrics_latest
  ON logged_model_metrics(model_id, key, step DESC, timestamp DESC, id DESC);
```

Webhook / outbox：

```sql
CREATE TABLE webhooks (
  webhook_id TEXT PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  url TEXT NOT NULL,
  events_json TEXT NOT NULL,
  secret TEXT NOT NULL DEFAULT '',
  description TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'ACTIVE'
         CHECK (status IN ('ACTIVE','DISABLED')),
  creation_timestamp INTEGER NOT NULL,
  last_updated_timestamp INTEGER NOT NULL
);

CREATE TABLE webhook_deliveries (
  delivery_id TEXT PRIMARY KEY,
  webhook_id TEXT NOT NULL,
  event TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'PENDING'
         CHECK (status IN ('PENDING','DELIVERED','FAILED')),
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT NOT NULL DEFAULT '',
  response_status INTEGER,
  created_time INTEGER NOT NULL,
  next_attempt_time INTEGER NOT NULL,
  delivered_time INTEGER,
  FOREIGN KEY (webhook_id) REFERENCES webhooks(webhook_id) ON DELETE CASCADE
);

CREATE INDEX idx_webhook_deliveries_due
  ON webhook_deliveries(status, next_attempt_time);
```

`secret` 在默认 API 输出中被隐藏；`webhook_deliveries` 是 outbox，不是不可篡改审计日志。

```sql
CREATE TABLE datasets (
  dataset_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  version INTEGER NOT NULL CHECK (version >= 1),
  digest TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL DEFAULT '',
  source_type TEXT NOT NULL DEFAULT '',
  schema_json TEXT NOT NULL DEFAULT '{}',
  profile_json TEXT NOT NULL DEFAULT '{}',
  tags_json TEXT NOT NULL DEFAULT '{}',
  record_count INTEGER NOT NULL DEFAULT 0 CHECK (record_count >= 0),
  created_time INTEGER NOT NULL,
  last_updated_time INTEGER NOT NULL
);

CREATE TABLE dataset_records (
  dataset_id TEXT NOT NULL,
  record_id INTEGER NOT NULL,
  record_json TEXT NOT NULL,
  PRIMARY KEY (dataset_id, record_id),
  FOREIGN KEY (dataset_id) REFERENCES datasets(dataset_id) ON DELETE CASCADE
);
```

`digest` 覆盖全部记录的规范化 JSON；`schema_json`/`profile_json` 由服务层从记录推导，不接受客户端直填。

## B.4 不变量

### 存储层

- 实验名全局唯一（含删除）；
- run 必须属于存在的实验；
- metric step >= 0；
- params 主键唯一；
- model version `(name,version)` 唯一；
- alias 每个模型内唯一；
- span 主键唯一。

### 服务层

- 参数不可变；
- 终态 run 不写数据；
- artifact 路径不逃逸；
- 删除实验/Default 保护；
- trace 至少一个 root span，parent 必须存在；
- model version 的 run_id 必须存在；
- logged model 的实验必须 active；source run 必须属于同一实验；
- logged model 参数不可变，终态不可再改；
- 软删除实验后，其 logged model 对外不可见（恢复后重新可见）；
- dataset 记录只追加；合并时版本 CAS 失败要重算，不能写回陈旧 digest；
- dataset 的 `record_count` 必须与 `dataset_records` 行数一致，删除数据集级联删记录。

### 不在数据库层

- 指标 key 的业务命名；
- 模型 source 可加载；
- 评测报告完整性；
- 删除引用检查。

## B.5 查询模式

最新指标：

```sql
SELECT run_id, key, value
FROM (
  SELECT run_id, key, value,
         ROW_NUMBER() OVER (PARTITION BY run_id, key
                            ORDER BY timestamp DESC, id DESC) AS rn
  FROM metrics
  WHERE run_id IN (...)
) WHERE rn = 1;
```

LoggedModel 最新指标（3.18 起 step 优先）：

```sql
SELECT model_id, key, value
FROM (
  SELECT model_id, key, value,
         ROW_NUMBER() OVER (PARTITION BY model_id, key
                            ORDER BY step DESC, timestamp DESC, id DESC) AS rn
  FROM logged_model_metrics
  WHERE model_id IN (...)
) WHERE rn = 1;
```

Run 搜索：

```sql
SELECT r.* FROM runs r
WHERE r.lifecycle_stage = 'active'
  AND EXISTS (SELECT 1 FROM metrics m
              WHERE m.run_id = r.run_id AND m.key = ? AND m.value > ?
                AND m.id = (SELECT m2.id FROM metrics m2
                            WHERE m2.run_id = m.run_id AND m2.key = m.key
                            ORDER BY m2.timestamp DESC, m2.id DESC LIMIT 1))
ORDER BY r.start_time DESC, r.run_id ASC
LIMIT ? OFFSET ?;
```

## B.6 迁移注意事项

- 新增列优先可空/带默认；
- 大表加索引要评估写放大；
- 修改枚举 CHECK 需要重建表（SQLite 限制）；
- 数据迁移要可回滚；
- 先用备份库演练。

## B.7 Mini 的限制

- 单文件 SQLite；
- 无 agent/租户列；
- 无审计表；
- 无异步任务表；
- 迁移历史仅 `schema_meta.version`（当前 7）；v1–v6 自动升级到 v7，只做 `CREATE TABLE IF NOT EXISTS` 与必要的 `ALTER TABLE`，不改旧行数据；
- 生产应使用编号迁移脚本（Alembic 等），并为每次迁移准备回滚。

---

返回：[教程首页](../README.md) · [附录 C](appendix-c-configuration.md)
