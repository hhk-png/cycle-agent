# 第13章 CDC、数据湖与实时数仓

## 13.1 CDC 解决什么问题

业务数据在 MySQL/PostgreSQL/Oracle 中，分析系统需要实时看到变化。定时抽全表：

- 延迟高；
- 对源库压力大；
- 无法捕获删除和中间状态；
- 数据量大时抽不动；
- 全量与增量切换容易重复/遗漏。

CDC（Change Data Capture）读取数据库日志（binlog/WAL/redo），把 INSERT/UPDATE/DELETE 变成事件流。Flink CDC 把这件事做成 source，并支持全量 + 增量一体化读取。

```text
MySQL binlog ──> Flink CDC Source ──> Flink 转换 ──> 湖表/OLAP
  INSERT/UPDATE/DELETE               清洗/打宽/聚合
```

CDC 事件通常包含：

- 变更类型（+I / -U / +U / -D）；
- 变更前后的完整行或部分列；
- 数据库/表/主键；
- 位点（binlog file + position / LSN / SCN）；
- 事务边界（有些连接器支持）。

## 13.2 全量 + 增量快照

传统 CDC 先全量导出，再切到 binlog，切点难对齐。Flink CDC 的增量快照算法：

1. 给表加读锁/记录低水位；
2. 分片并行读取历史数据；
3. 同时消费 binlog；
4. 用水位判断哪些 binlog 已包含在快照中；
5. 快照完成后无缝切换到纯增量。

```text
时间 →
binlog:   b1  b2  b3  b4  b5  b6 ...
快照:      [====分片1====] [====分片2====]
低水位:    ↑
高水位:                        ↑
切换点:  快照完成 + binlog 追平高水位
```

优点：

- 全量阶段可并行，速度快；
- 不阻塞写入（取决于数据库和锁策略）；
- 断点续传，失败可恢复；
- 支持 schema 变更。

注意：

- 不同数据库的连接器实现、锁行为、权限要求不同；
- 全量阶段仍可能对源库造成压力（连接数、IO）；
- 需要监控 binlog 保留时间，避免位点过期；
- 生产上必须阅读对应版本的限制。

## 13.3 Flink CDC 3.x Pipeline

Flink CDC 3.x 提供声明式 pipeline。下面是一个**字段示意**，具体配置项以所用 CDC 版本为准：

```yaml
source:
  type: mysql
  hostname: mysql
  port: 3306
  username: flink
  password: ${MYSQL_PASSWORD}      # 由密钥管理/环境注入
  tables: app_db.\.*               # 整库同步
  server-id: 5400-5404             # 必须唯一，避免与其它 CDC 冲突
  server-time-zone: UTC

sink:
  type: paimon
  catalog.properties.metastore: filesystem
  catalog.properties.warehouse: s3://lake/warehouse
  database: ods

pipeline:
  name: mysql-to-paimon
  parallelism: 4
  schema.change.behavior: evolve   # lenient/evolve/exception，按版本确认
```

命令行：

```bash
flink-cdc.sh mysql-to-paimon.yaml
```

它把 schema 路由、表映射、类型转换、DDL 同步等模板化，适合整库同步；复杂清洗仍建议用 Flink SQL/DataStream 作业。

需要区分两种“整库同步”写法：开源 Flink SQL 支持的是 **CTAS**（单表 `CREATE TABLE ... AS SELECT`，见[第 14 章 14.9 节](14-table-sql.md#149-ctasrtas-与数据管道)）；**CDAS**（`CREATE DATABASE AS SELECT`）是阿里云实时计算 Flink 版（VVR）的扩展语法，且该语法已进入退役流程，不在开源 Apache Flink 中。开源 Flink CDC 3.x 的整库同步走 YAML pipeline，或为每张表生成 CTAS。

### Pipeline 的边界

CDC Pipeline 适合“源表到目标表的同构同步”：

- 表名/库名映射；
- 列裁剪/改名；
- 基本类型转换；
- DDL 同步；
- 整库/多表同步。

不适合：

- 复杂多表 join；
- 窗口聚合；
- 自定义 UDF；
- 多路分发/条件路由。

这些场景应该用 Flink SQL/DataStream 消费 CDC 流，而不是把 Pipeline 当万能 ETL。

## 13.4 Schema 演进

CDC 流会携带 DDL。需要处理：

- 新增列：下游表能否自动加列；
- 删除列：历史数据如何保留；
- 改类型：int → bigint、decimal 精度变化；
- 改列名：按列 id 还是列名映射；
- 主键变化：几乎不可自动处理；
- 默认值/非空约束变化。

湖仓格式（Paimon/Iceberg/Hudi）通常支持 schema evolution，但每个引擎/版本能力不同。生产上要建立 schema 契约和兼容性测试。

```sql
-- Flink SQL 中显式处理 schema 变化
ALTER TABLE paimon_orders ADD (new_col STRING);
```

### Schema 演进的三种策略

1. **自动同步**：CDC 的 DDL 直接应用到下游；简单，但风险高；
2. **受控演进**：DDL 进入审批/人工确认，再应用到下游；适合核心表；
3. **版本化**：保留多版本 schema，查询时按版本解析；适合长期历史。

无论哪种，都要保证：

- 下游查询不会因为新列/类型变化而失败；
- 历史数据仍可读；
- 回滚方案存在。

## 13.5 数据湖表格式

| 格式 | 特点 | 适合 |
|---|---|---|
| Paimon | 流批一体、LSM、增量读、Flink 原生 | 实时湖仓、CDC 入湖 |
| Iceberg | 开放表格式、快照隔离、生态广 | 多引擎共享、批分析 |
| Hudi | upsert、增量拉取、COW/MOR | 需要频繁更新的湖表 |
| Delta Lake | Databricks 生态、ACID | Spark 为主的环境 |

三者的差异不只在文件格式，更在**提交协议、compaction 和增量读**：Iceberg 依赖 catalog 的原子提交，适合多引擎共享，但流式增量读通常要额外配置；Hudi 的 COW/MOR 与 timeline 决定 upsert 的写放大和增量拉取成本；Delta Lake 的并发提交依赖 LogStore，跨引擎写入要确认锁与冲突处理。实时入湖前要验证目标格式的并发写、快照过期、compaction 策略和小文件治理，而不是只看“支持 upsert”一行。

Flink 可以作为写入引擎，也可以作为读取引擎。选型要看：

- 是否支持流式写入和增量读；
- 小文件/compaction 策略；
- 并发写与冲突处理；
- 元数据服务和权限；
- 查询引擎兼容性；
- 与 Flink 2.x 的兼容性。

### Paimon 的核心设计

Paimon 是 Flink 社区主导的湖表格式，核心是 LSM 树 + 快照：

- 写入先进入内存 buffer，flush 成小文件；
- 后台 compaction 合并小文件；
- 每次提交产生一个 snapshot，读端看到一致快照；
- 支持主键表（upsert）和追加表（append）；
- 支持 changelog 产生，供下游流读。

```sql
CREATE TABLE paimon_orders (
  order_id STRING,
  user_id STRING,
  amount DECIMAL(18, 2),
  order_time TIMESTAMP(3),
  PRIMARY KEY (order_id) NOT ENFORCED
) WITH (
  'connector' = 'paimon',
  'path' = 's3://lake/warehouse/orders',
  'bucket' = '4',
  'changelog-producer' = 'input'
);
```

主键表适合 CDC 入湖（upsert），追加表适合日志/事件。`changelog-producer` 决定下游能否流读完整变更。

## 13.6 实时数仓分层

常见分层：

```text
ODS：CDC 原始变更 / 日志
DWD：清洗、去重、维表打宽
DWS：主题宽表、分钟/小时聚合
ADS：面向应用的指标/接口
```

Flink 负责 ODS→DWD→DWS 的实时链路，湖仓负责存储和批式修正。指标结果写入 OLAP（Doris/ClickHouse/StarRocks）或 KV，供应用查询。

```text
MySQL ─CDC→ Paimon ODS ─Flink→ Paimon DWD ─Flink→ Doris DWS ─→ BI/API
                                   │
                                   └─ 离线批任务修正 ─→ 对账
```

关键设计：

- 明细层保留业务主键和变更类型；
- 聚合层明确 watermark 和迟到处理；
- 离线批任务定期修正实时结果；
- 指标口径统一，避免“实时和离线对不上”；
- 数据质量监控（空值、重复、延迟、行数突变）。

### 实时与离线对账

实时链路一定会有误差：迟到、去重、状态恢复。对账是必需的：

- 按小时/天比对行数、金额、去重后的主键数；
- 差异超过阈值时告警；
- 用离线结果修正实时结果（Lambda 架构的“批修正”思想）；
- 记录差异原因（迟到、重复、schema 变化）。

## 13.7 Exactly-once 入湖

湖表写入的 exactly-once 依赖：

- checkpoint 与提交协议；
- 表格式的事务/快照隔离；
- 失败时未提交文件清理；
- 并发写冲突处理；
- compaction 不破坏快照。

Paimon/Iceberg 的 Flink sink 通常支持两阶段提交。配置时要确认：

- checkpoint 间隔与提交超时；
- 小文件合并频率；
- 写入并行度与分区数；
- 是否启用动态 bucket；
- 元数据服务（Hive Metastore/Unity/JDBC）可用性。

```yaml
execution.checkpointing.interval: 60s
# 是否物化：AUTO（默认）/ NONE / FORCE
table.exec.sink.upsert-materialize: AUTO
# 2.2+ 的细粒度实现策略：LEGACY / MAP / VALUE / ADAPTIVE
table.exec.sink.upsert-materialize-strategy.type: LEGACY
```

`SinkUpsertMaterializer` 用于处理 upsert 流中的乱序/撤回，避免下游收到错误顺序。2.2 的 V2 版本优化了性能，2.3 增加了 `ON CONFLICT` 语义。

## 13.8 小文件与 compaction

流式写入会持续产生小文件。后果：

- 查询变慢；
- 元数据膨胀；
- 存储成本上升；
- checkpoint 变慢。

解决：

- 按时间/大小滚动文件；
- 异步 compaction；
- 分区合理（不要过细）；
- 定期重写；
- Paimon 的 LSM 合并、Iceberg 的 rewrite manifests/data files。

compaction 本身也消耗资源，要和写入作业隔离或限流。

```text
写入 1 分钟/文件 → 一天 1440 个文件/分区
compaction 合并成 100MB/文件 → 查询扫描文件数下降 10 倍
```

### 小文件治理检查

- 单文件大小是否在 64～256 MB；
- 每分区文件数是否可控；
- compaction 是否跟得上写入；
- compaction 是否影响写入延迟；
- 元数据文件是否膨胀；
- 查询引擎是否能利用分区裁剪。

## 13.9 Paimon 合并引擎与湖仓写入模式

CDC 入湖最常被低估的不是“写进去”，而是“同一条主键的多次变更按什么规则合并”。Paimon 的主键表提供多种 **merge engine**；选错引擎，结果可能静默错误，而且越跑越难修。

### 13.9.1 主键表、追加表与 bucket

Paimon 有两类表：

- **主键表（primary-key table）**：按主键去重/合并，支持 upsert、changelog、部分列更新；
- **追加表（append table）**：只追加，不做主键合并，适合日志/事件明细。

主键表的物理布局由 `bucket` 决定：

```text
table/
├── bucket-0/
│   ├── data files (LSM)
│   └── ...
├── bucket-1/
└── ...
```

bucket 太少会导致单桶写入瓶颈，太多会产生小文件和元数据开销。流式写入通常按“目标文件大小 + 写入并行度”反推 bucket 数；分区表还要考虑每个分区是否独立 bucket。

### 13.9.2 四种 merge engine

| merge engine | 合并规则 | 适合 | 风险 |
|---|---|---|---|
| `deduplicate` | 同一主键保留最新一条（按 sequence/到达顺序） | CDC 镜像表、维表 | 乱序时“最新”可能不是业务最新 |
| `partial-update` | 非 null 字段覆盖旧值，null 字段保留 | 多流打宽、宽表拼接 | 需要可靠的 sequence 防止旧值覆盖新值 |
| `aggregation` | 按字段聚合函数合并（sum/max/min 等） | 指标预聚合、累计值 | 不适合需要精确回撤的场景 |
| `first-row` | 同一主键只保留第一条 | 首次触达、首单、去重 | 后续修正被丢弃，业务要明确接受 |

选择顺序通常是：

```text
需要完整行镜像？ → deduplicate
需要多流拼宽表？ → partial-update
需要预聚合指标？ → aggregation
只需要第一次？   → first-row
```

### 13.9.3 用 sequence field 处理乱序

CDC/消息流可能乱序到达。`deduplicate`/`partial-update` 如果只按“到达顺序”取最新，旧事件晚到会覆盖新状态。解决方法是声明业务上的版本字段：

```sql
CREATE TABLE paimon_orders (
  order_id STRING,
  user_id STRING,
  amount DECIMAL(18, 2),
  status STRING,
  update_time TIMESTAMP(3),
  PRIMARY KEY (order_id) NOT ENFORCED
) WITH (
  'merge-engine' = 'deduplicate',
  'sequence.field' = 'update_time'
);
```

`sequence.field` 告诉 Paimon：同一主键比较这个字段，值更大的版本获胜。要求：

- 字段单调、可比、能反映业务更新顺序；
- 不同上游的时钟偏差不能破坏顺序；
- 字段类型稳定，schema 演进不能改变语义；
- 不要用处理时间代替业务版本时间。

对于多流 partial update，sequence 更重要：没有它，晚到的旧值可能把新值覆盖成 null 或旧值。

### 13.9.4 partial-update：多流打宽表

```sql
CREATE TABLE user_wide (
  user_id STRING,
  profile_name STRING,
  last_login_ip STRING,
  last_order_amount DECIMAL(18, 2),
  updated_at TIMESTAMP(3),
  PRIMARY KEY (user_id) NOT ENFORCED
) WITH (
  'merge-engine' = 'partial-update',
  'sequence.field' = 'updated_at'
);
```

不同作业分别写 `profile_name`、`last_login_ip`、`last_order_amount`；Paimon 按主键合并，null 不覆盖已有值。这比“每个作业都写整行”更松耦合，但要接受：

- 同一主键的写入仍有并发冲突；
- 字段级更新不等于事务级更新；
- 下游读到的可能是“半个新行”；
- 删除语义要显式设计（`partial-update` 对 null 的处理与删除不同）。

多流各自更新不同列时，单个 `sequence.field` 可能被另一条流的“更大时间戳”覆盖。Paimon 提供 **sequence group**，把“排序字段”和“受保护的列”绑定：

```sql
CREATE TABLE user_wide (
  user_id STRING,
  profile_name STRING,
  last_login_ip STRING,
  profile_seq BIGINT,
  login_seq BIGINT,
  PRIMARY KEY (user_id) NOT ENFORCED
) WITH (
  'merge-engine' = 'partial-update',
  'fields.profile_seq.sequence-group' = 'profile_name',
  'fields.login_seq.sequence-group' = 'last_login_ip'
);
```

每个组只在自己的排序字段推进时更新；`profile_seq` 不会误覆盖 `last_login_ip`。这比“全表一个 sequence”更适合多流打宽。

### 13.9.5 aggregation：指标预聚合

```sql
CREATE TABLE user_metrics (
  user_id STRING,
  order_count BIGINT,
  total_amount DECIMAL(18, 2),
  PRIMARY KEY (user_id) NOT ENFORCED
) WITH (
  'merge-engine' = 'aggregation',
  'fields.order_count.aggregate-function' = 'sum',
  'fields.total_amount.aggregate-function' = 'sum'
);
```

聚合引擎适合“只增不减”的累计指标；如果业务需要撤回/修正（例如退款），要确认聚合函数和下游查询能否表达负向更新。不要把 `sum` 当成“自动支持 retract”。

### 13.9.6 changelog-producer：下游能不能流读

Paimon 主键表要向下游产生 changelog，需要配置 `changelog-producer`：

| 值 | 机制 | 代价 |
|---|---|---|
| `none` | 不生成 changelog，下游只能批读快照 | 最低 |
| `input` | 把输入变更直接作为 changelog | 依赖输入完整，partial-update 可能不完整 |
| `lookup` | 写入时查旧值，生成完整 changelog | 额外读放大 |
| `full-compaction` | compaction 时生成 changelog | 延迟高、资源重 |

选择要点：

- 下游要流式读完整 `+I/-U/+U/-D`，通常需要 `lookup` 或 `full-compaction`；
- `input` 最便宜，但对 partial-update/aggregation 可能无法给出完整旧值；
- `full-compaction` 的延迟与 compaction 周期相关，不适合低延迟链路；
- changelog 生成会增加写放大和存储，要和 compaction 一起压测。

### 13.9.7 写入、compaction 与快照治理

- 写入并行度与 bucket 数匹配，避免“多个 writer 抢一个 bucket”；
- 小文件靠 compaction 合并，compaction 要限流/隔离；
- 快照保留数量影响存储和时间旅行，不能无限保留；
- 分区过细会产生大量元数据和小文件；
- schema 演进要同时验证写入、流读和批读；
- 下游查询引擎要支持 Paimon 的 snapshot/changelog 语义。

### 13.9.8 什么时候不该用 Paimon

- 只需要 append-only 日志，用普通文件/消息队列更简单；
- 需要极低延迟点查，Paimon 不是 KV 数据库；
- 团队没有 compaction/元数据运维能力；
- 业务不能接受“最终合并”的语义，需要强事务行锁；
- 上游没有可靠的主键/版本字段，merge engine 无法判断新旧。

湖仓写入的本质是“把流上的变更协议映射成表上的合并协议”。merge engine、sequence field、changelog-producer 三者必须一起选，不能只看其中一个开关。

## 13.10 MiniFlink 的边界

MiniFlink 没有 CDC 和湖仓连接器。它提供文件、内存 Kafka、SQLite，足以演示“source offset + checkpoint + sink”的骨架，但无法模拟 binlog、schema 演进和湖表事务。CDC/湖仓部分以生产 Flink 的机制为主，MiniFlink 只作为概念对照。对应的运行时实现见[第 31 章](31-miniflink-checkpoint.md)，边界清单见[附录 C](../appendix/C-miniflink-limits.md)。

## 13.11 从管道到模型：实时数仓的建模闭环

CDC 和湖仓解决“数据怎么进来、变更怎么存”，但一条链路能不能被业务信任，取决于更上游的建模决策：事实表的粒度是什么、维度要不要保留历史、指标是否可加、迟到和更正如何表达。技术栈相同，粒度设计不同，最后会得到完全不同的 GMV 和转化率。

实时建模至少要在开工前定下四件事：

1. **粒度**：一行事实代表一次事件、一个实体当前状态，还是一个实体在一个周期末的快照；
2. **时间**：业务事实用事件时间，新鲜度/运维用摄入或处理时间，两者不能混用；
3. **更新协议**：append、upsert 还是 retract；下游 sink 是否支持对应的 changelog；
4. **历史**：维度用 SCD1 覆盖、SCD2 版本化，还是只保留当前值；审计和回放是否要求任意时点可还原。

这四件事会直接决定 Flink 作业的状态规模、checkpoint 成本和湖表 compaction 压力。例如把无界 Regular Join 当作维表关联，状态会随两侧历史无限增长；把处理时间用于财务事实，回放时结果会漂移；把各窗口的 UV 直接相加，指标会虚高。

完整的建模方法、Flink SQL 模板、SCD 设计、订单生命周期案例和反模式清单见[附录 N](../appendix/N-realtime-warehouse-modeling.md)。本章的 CDC/湖仓负责“把变化可靠地送达”，附录 N 负责“把变化组织成可解释、可对账的模型”。

## 13.12 实时数仓检查清单

- [ ] CDC 全量阶段是否会影响源库？锁/连接数/IO 是否评估？
- [ ] binlog/WAL 保留时间是否覆盖故障恢复窗口？
- [ ] schema 演进策略是什么？自动还是受控？
- [ ] 湖表格式是否支持 upsert、增量读、schema evolution？
- [ ] exactly-once 入湖的提交协议是否验证？
- [ ] 小文件/compaction 是否有监控和限流？
- [ ] 实时与离线口径如何对账？
- [ ] 数据质量监控覆盖哪些维度？

## 本章小结

CDC + 湖仓是实时数仓的主流组合：CDC 解决“变化从哪来”，湖仓解决“历史与更新存到哪”，Flink 解决“如何实时转换”。三者都依赖 schema、事务和小文件治理。

下一章进入 Table API 与 SQL：如何用关系语义描述流上的持续查询，以及动态表、changelog 和物化表如何统一流批。
