# 第14章 Table API 与 SQL 基础

## 14.1 动态表：流上的关系

Table API/SQL 把流看成一张**动态表**：表会随时间变化，查询结果是另一张动态表。对动态表的每次 INSERT/UPDATE/DELETE，都有一个 changelog：

- `+I`：插入；
- `-U`：更新前；
- `+U`：更新后；
- `-D`：删除。

```text
orders 流:  +I (id=1, amount=10)
            +I (id=2, amount=20)
            +U (id=1, amount=15)      -- 同一主键的金额从 10 更正为 15
                 ↓ SQL
结果表:      +I (total=10) → +U (total=30) → +U (total=35)
```

这与批处理表的关键区别是：**结果不是一次性算完，而是持续更新**。下游必须能消费 changelog（Kafka upsert、数据库 upsert、湖表 merge）。

### 动态表的三种结果模式

| 模式 | 含义 | 典型算子 |
|---|---|---|
| append-only | 只追加，不更新/删除 | filter、map、窗口聚合（未允许迟到） |
| retract | 先撤回旧值，再插入新值 | 无窗口聚合、TopN |
| upsert（sink 侧） | 按主键合并更新 | 不是查询的逻辑模式；由主键 sink 或 `SinkUpsertMaterializer` 把 retract/upsert 流物化后产生 |

需要区分“查询产生的逻辑 changelog”和“sink 看到的形态”：无窗口 `GROUP BY` 通常产生 retract；带主键的 sink 可能在写入前把它物化成 upsert。不要把 upsert 当成一种独立的查询模式。

下游能力必须匹配：

- append sink（普通 Kafka）不能接 retract；
- upsert-kafka 需要主键；
- JDBC/湖表需要主键 upsert；
- 如果 SQL 产生 retract 而 sink 只支持 append，作业会在编译期或运行期失败。

## 14.2 环境与 Catalog

```java
EnvironmentSettings settings = EnvironmentSettings
    .newInstance()
    .inStreamingMode()
    .build();
TableEnvironment tEnv = TableEnvironment.create(settings);

tEnv.executeSql("CREATE CATALOG hive WITH ('type'='hive', ...)");
tEnv.useCatalog("hive");
tEnv.useDatabase("realtime");
```

Catalog 管理数据库、表、函数和元数据。生产上通常接 Hive Metastore、JDBC Catalog、Paimon/Iceberg Catalog，保证 SQL 作业与离线表共享元数据。

```sql
CREATE CATALOG paimon WITH (
  'type' = 'paimon',
  'warehouse' = 's3://lake/warehouse'
);

USE CATALOG paimon;
USE `realtime`;
```

Catalog 的价值：

- 统一表名解析，避免每个作业重复写 DDL；
- 共享 schema，减少漂移；
- 支持跨引擎（Flink/Spark/Trino）访问同一张表；
- 权限和审计可以挂在 Catalog 层。

本节讲的是 Catalog 的使用；如果你想自己实现一个 Catalog 或 Table 连接器（`CatalogFactory`、`DynamicTableFactory`、谓词/投影下推、元数据列），见[附录 K](../appendix/K-table-connector-catalog.md)。

### 临时表 vs 永久表

- `CREATE TEMPORARY TABLE`：只在当前会话/作业有效，适合测试和临时连接；
- `CREATE TABLE`：写入 Catalog，持久化；
- 临时表优先于永久表解析，容易“覆盖”同名表，生产要避免同名。

## 14.3 DDL 与连接器

```sql
CREATE TABLE orders (
  order_id STRING,
  user_id STRING,
  amount DECIMAL(10, 2),
  order_time TIMESTAMP(3),
  WATERMARK FOR order_time AS order_time - INTERVAL '5' SECOND
) WITH (
  'connector' = 'kafka',
  'topic' = 'orders',
  'properties.bootstrap.servers' = 'kafka:9092',
  'properties.group.id' = 'orders-sql',
  'scan.startup.mode' = 'earliest-offset',
  'format' = 'json'
);
```

DDL 同时描述：

- schema 和类型；
- 时间属性/watermark；
- 连接器与格式；
- 并发、分区、启动位置、sink 语义等参数。

表不是数据，而是“连接器 + schema + 元数据”的绑定。修改表参数可能改变作业行为，必须版本化管理。

### 计算列与元数据列

```sql
CREATE TABLE orders (
  order_id STRING,
  amount DECIMAL(18, 2),
  order_time TIMESTAMP(3),
  proc_time AS PROCTIME(),                    -- 计算列
  order_date AS CAST(order_time AS DATE),     -- 计算列
  kafka_partition INT METADATA FROM 'partition' VIRTUAL,
  WATERMARK FOR order_time AS order_time - INTERVAL '5' SECOND
) WITH (...);
```

计算列不落存储，查询时计算；元数据列可以暴露 Kafka 分区、offset、topic 等信息，便于调试和审计。

## 14.4 查询

```sql
SELECT user_id, SUM(amount) AS total
FROM orders
WHERE amount > 0
GROUP BY user_id;
```

流式语义：

- 每条输入更新对应 user 的聚合结果；
- 结果流是 changelog，不是最终快照；
- 没有窗口的聚合会无限保留状态；
- 没有 watermark 的排序/join 可能无法执行或状态无限增长。

```sql
SELECT *
FROM orders
WHERE amount > 100;
```

这是无状态过滤：append 输入只产生 `+I`；如果输入本身是 changelog，filter 会按行透传 `+I/-U/+U/-D`，不会把更新改写成删除。

### 无窗口聚合的状态

```sql
SELECT user_id, COUNT(*) AS cnt
FROM orders
GROUP BY user_id;
```

这个查询会为每个 user 保留一个计数，状态随用户数增长。如果用户数上亿，状态会非常大。解决：

- 加窗口，让状态可清理；
- 设置状态 TTL（`table.exec.state.ttl`）；
- 只保留需要的维度，避免高基数 GROUP BY；
- 用预聚合/去重减少 key 数。

### 多路输出：STATEMENT SET

一个作业要写多个 sink 时，不要拆成多个作业重复消费 source；用 `STATEMENT SET` 把多条 INSERT 放进同一个 JobGraph：

```sql
EXECUTE STATEMENT SET
BEGIN
  INSERT INTO order_detail SELECT ... FROM orders;
  INSERT INTO order_metrics SELECT ... FROM orders;
END;
```

Table API 对应：

```java
StatementSet statements = tEnv.createStatementSet();
statements.addInsert("order_detail", detailTable);
statements.addInsert("order_metrics", metricsTable);
statements.execute();
```

执行语义：

- 所有 INSERT 进入**同一个 JobGraph**，共享 source 和可合并的上游算子；planner 无法合并时会复制读取逻辑，但它仍然是一个作业；
- 共享**同一个 checkpoint 域**：一次 checkpoint 覆盖所有 sink，恢复时一起回滚到同一批 offset；
- 共享**同一个失败域**：任一算子失败，整个作业重启，所有 sink 一起恢复；
- 它**不是跨 sink 的分布式事务**。Kafka 事务、JDBC 事务、文件提交各自独立，“都成功或都失败”只在同一 checkpoint 边界内近似成立；
- UPDATE 语义的 sink 仍需要 upsert 物化，Statement Set 不会替你消除 changelog 的乱序。

适合：同一份 source 派生多个下游、指标与明细同源、维度和事实一起写。不适合：不同 SLO/不同团队独立发布的下游、单个 sink 故障不应拖垮其他链路、需要独立扩缩容的作业。遇到这些情况，优先拆作业，用消息总线或湖表解耦。

注意：

- 所有语句必须在同一个会话/Catalog 下解析；
- 多个 sink 的 exactly-once 仍要各自满足；
- 一条 INSERT 失败会拖垮整个作业，爆炸半径比拆作业大；
- 并非所有连接器都支持在同一作业里被多次复用；
- 共享 source 时，source 的并行度/分区数会成为所有下游的共同上限。

## 14.5 时间属性

处理时间列：

```sql
proc_time AS PROCTIME()
```

事件时间列：

```sql
order_time TIMESTAMP(3),
WATERMARK FOR order_time AS order_time - INTERVAL '5' SECOND
```

只有事件时间列能用于窗口 TVF、interval join 等需要 watermark 的操作。处理时间列适合 lookup join、处理时间窗口。

时间属性的三个限制：

1. 一个表只能有一个事件时间属性；
2. watermark 表达式必须引用时间列，且是确定性的；
3. 时间列类型必须是 `TIMESTAMP(3)` 或 `TIMESTAMP_LTZ(3)`（事件时间）。

## 14.6 Table ↔ DataStream

```java
Table table = tEnv.fromDataStream(stream, $("id"), $("amount"), $("ts").rowtime());
DataStream<Row> result = tEnv.toDataStream(table, Row.class);
// toChangelogStream 返回 DataStream<Row>，每行的 RowKind 表示 +I/-U/+U/-D
DataStream<Row> changelog = tEnv.toChangelogStream(table);
```

转换时要处理：

- changelog 的 RowKind；
- 事件时间/watermark 是否保留；
- 字段名与类型；
- 有状态转换的 uid。

Table API 不是“另一个运行时”，它最终会编译成 DataStream 算子；两者可以混合使用，但跨 API 的拓扑变更会影响 savepoint 兼容性。

### 混合使用的边界

- `fromDataStream` 后做 SQL，再 `toDataStream`：常见于“DataStream 解析 → SQL 聚合 → DataStream 写出”；
- 跨 API 时，算子 uid 由 planner 生成，升级 SQL 可能导致 uid 变化；
- 事件时间属性要在 `fromDataStream` 时声明，否则 SQL 看不到 watermark；
- changelog 的 RowKind 要显式处理，不能当普通 Row。

## 14.7 Table API 流式 DSL

本章标题里的“Table API”不是 SQL 的别名。SQL 和 Table API 共享同一个 planner、同一个运行时和同一套 changelog 语义，只是表达方式不同：

- **SQL**：适合分析师、平台化、跨引擎移植和运维审查；
- **Table API**：适合 Java/Scala 代码中的动态组合、单元测试、与 DataStream 混编；
- **两者可以互转**：`tEnv.sqlQuery(...)` 得到 `Table`，`Table` 也可以注册成临时视图给 SQL 用。

`Table` 是**不可变、惰性**的查询描述。每次 `select/where/join` 都返回新 `Table`，只有 `execute()` 才提交作业。

### 14.7.1 环境与第一个查询

```java
import static org.apache.flink.table.api.Expressions.*;

EnvironmentSettings settings = EnvironmentSettings
    .newInstance()
    .inStreamingMode()
    .build();
TableEnvironment tEnv = TableEnvironment.create(settings);

Table orders = tEnv.from("orders");

Table result = orders
    .filter($("amount").isGreater(0))
    .groupBy($("user_id"))
    .select(
        $("user_id"),
        $("amount").sum().as("total"),
        $("order_id").count().as("orders"));

result.execute().print();
```

要点：

- `$("字段")` 是字段引用；拼错字段名在编译/计划期报错，不会静默变成 null；
- `as("别名")` 给结果列命名，避免下游出现 `EXPR$0`；
- 聚合后的 `Table` 是 changelog（可能是 `+U/-U`），不是“最终结果快照”；
- 流模式下 `execute()` 不会自己结束，`print()` 只适合开发调试。

### 14.7.2 过滤、投影、排序与去重

```java
Table recent = orders
    .filter($("order_time").isGreaterOrEqual(
        currentTimestamp().minus(lit(1).hours())))
    .select($("order_id"), $("user_id"), $("amount"))
    .orderBy($("amount").desc())
    .limit(100);
```

`orderBy + limit` 在流模式下会产生带状态的 TopN 算子；`limit` 不是“取一批就结束”，而是持续维护前 N 名。无界输入上要确认状态和更新语义。

去重的 Table API 写法：

```java
Table dedup = orders
    .window(Tumble.over(lit(1).hours()).on($("order_time")).as("w"))
    .groupBy($("w"), $("order_id"))
    .select($("w").start().as("window_start"), $("order_id"),
            $("order_time").max().as("last_time"));
```

更精细的“按主键保留最新一条”通常用 `ROW_NUMBER()` 的 SQL 形式，或 `Table` 上的 `flatMap`/`process` 自定义算子；Table API 不是所有 SQL 窗口函数都有等价的链式方法。

### 14.7.3 窗口

Table API 的窗口通过 `Tumble/Hop/Cumulate/Session` 表达：

```java
Table hourly = orders
    .window(
        Tumble.over(lit(1).hours())
              .on($("order_time"))
              .as("w"))
    .groupBy($("user_id"), $("w"))
    .select(
        $("user_id"),
        $("w").start().as("window_start"),
        $("w").end().as("window_end"),
        $("amount").sum().as("total"));
```

`window(...).groupBy(...)` 生成的是窗口聚合；窗口列（`w.start()`/`w.end()`）可以继续参与后续查询。和 SQL TVF 一样，窗口大小/滑动步长是常量，不能由每行字段动态决定。这是 Table API 的 group-window 形式；部分版本也在推进基于窗口 TVF 的 Table API 写法，落地时以所用版本文档为准。

### 14.7.4 Join

```java
// Table API 没有“表别名” API；join 前先把同名字段改名为唯一列名，
// 避免 $("order_id") 在两侧之间产生歧义。
Table o = tEnv.from("orders").select(
    $("order_id").as("o_order_id"),
    $("amount").as("o_amount"));
Table p = tEnv.from("payments").select(
    $("order_id").as("p_order_id"),
    $("pay_id"),
    $("pay_time"));

Table joined = o
    .join(p)
    .where($("o_order_id").isEqual($("p_order_id")))
    .select(
        $("o_order_id"),
        $("o_amount"),
        $("pay_id"),
        $("pay_time"));
```

如果字段很多、重命名很繁琐，直接用 SQL 的 `FROM orders AS o JOIN payments AS p` 更清晰；SQL 的表别名是合法的，Table API 的不是。

- `join` 是 inner join；`leftOuterJoin`/`rightOuterJoin`/`fullOuterJoin` 会产生 null 补行和 changelog；
- 别名（`as("o")`）用于消除同名字段歧义；
- 流式 regular join 会保留两侧历史，必须配合 TTL 或改用 interval/temporal join；
- Table API 也支持 `joinLateral`、`leftOuterJoinLateral` 等表函数形式。

### 14.7.5 与 DataStream 互转

```java
// DataStream → Table：显式声明字段和事件时间
Table table = tEnv.fromDataStream(
    stream,
    $("order_id"), $("user_id"), $("amount"),
    $("order_time").rowtime());

// Table → DataStream：普通行
DataStream<Row> rows = tEnv.toDataStream(table);

// Table → changelog：保留 RowKind
DataStream<Row> changelog = tEnv.toChangelogStream(table);
```

跨 API 的坑：

- `fromDataStream` 不自动推断 watermark，事件时间要显式声明；
- `toDataStream` 会丢掉 changelog 的更新语义，只适合 append-only 结果；
- 跨 API 后算子 uid 由 planner 生成，SQL/Table 逻辑变化可能影响 savepoint 兼容性；
- 混合拓扑要在集成测试里验证 changelog 的 `RowKind` 和下游 sink 能力。

### 14.7.6 什么时候用 Table API，什么时候用 SQL

| 场景 | 建议 |
|---|---|
| 分析师/平台用户写指标 | SQL |
| 动态拼接查询、按配置生成字段 | Table API |
| 需要和 DataStream 算子混编 | Table API |
| 复杂窗口/去重/TopN | SQL 通常更简洁 |
| 单元测试与计划断言 | 两者都可以，Table API 更易组合 |
| 跨引擎迁移 | SQL |

Table API 不是“更底层的 API”。它和 SQL 最终都进 Calcite 逻辑计划；真正的底层仍然是 DataStream 运行时的算子和状态。

## 14.8 物化表（Materialized Table）

物化表由 Flink 1.20（FLIP-435）引入，2.x 继续扩展（可选 `FRESHNESS`、`DISTRIBUTED BY`、完整 DDL）。`START_MODE` 是 FLIP-557 提出的后续能力（社区目标 2.4，厂商方言已有），不在 2.3 开源语法中。物化表用一条 DDL 同时描述“持续刷新”和“全量刷新”：

```sql
CREATE MATERIALIZED TABLE user_revenue
  FRESHNESS = INTERVAL '1' MINUTE
  AS
  SELECT user_id, SUM(amount) AS total
  FROM orders
  GROUP BY user_id;
```

语义：

- `FRESHNESS` 是目标新鲜度，不是硬保证；
- 新鲜度低于阈值时，planner 选择 CONTINUOUS（流式增量刷新）；
- 新鲜度较高时，选择 FULL（批式全量覆盖）；
- 2.3 支持显式列、主键、watermark、`ALTER` 等更完整的 DDL；`START_MODE` 由 FLIP-557 推进，2.3 开源版没有这个子句。

物化表试图统一“实时链路”和“离线调度”的开发体验：用户只写查询和新鲜度，由平台决定刷新方式。它适合：

- 指标表/宽表，下游只关心“足够新”；
- 流批一体开发，避免维护两套 SQL；
- 数据湖上的物化视图。

注意：

- 新鲜度是目标，不是 SLA；
- CONTINUOUS 模式仍然消耗流资源；
- FULL 模式需要调度器和全量覆盖能力；
- 不同 connector 对物化表的支持不同。

2.3 的物化表 DDL 完整化，以及 upsert sink 的 `INSERT ... ON CONFLICT`，见第 34 章；`START_MODE` 的 2.4 路线与厂商差异也在 34.5 说明。

物化表与 CTAS 的区别：物化表是“由平台持续维护刷新策略的表”，CTAS 是“建表 + 一次性提交写入作业”。后者的 DDL、原子性和数据管道用法见 14.9。

## 14.9 CTAS、RTAS 与数据管道

### 14.9.1 一条 DDL 完成建表和写入

CTAS（`CREATE TABLE AS SELECT`）把“建目标表 + 插入查询结果”合并成一条语句：

```sql
CREATE TABLE order_summary WITH (
  'connector' = 'paimon',
  'path' = 's3://lake/order_summary',
  'primary-key' = 'user_id'
) AS
SELECT user_id, SUM(amount) AS total
FROM orders
GROUP BY user_id;
```

语义要点：

- 不写显式 schema 时，目标表的 schema 由 `SELECT` 推导（列名、类型、可空性）；`WITH` 里只写连接器/存储参数；需要覆盖列类型、加 watermark/主键时，再在 CREATE 部分声明；
- 流模式下提交的是一个**持续运行的 INSERT 作业**；批模式下是一次有界写入；
- 目标表由 Catalog 创建：Hive/JDBC/Paimon 等 Catalog 会在元数据或存储侧建表；内存 Catalog 只在客户端注册元数据，不会在外部系统建物理表，目标存储/表要由连接器或人工准备；
- Flink 1.18 起支持 RTAS（`CREATE OR REPLACE TABLE AS SELECT`，FLIP-303）。Flink 2.0 起 CREATE 部分支持显式列、计算列、watermark、主键、`PARTITIONED BY` 和 `DISTRIBUTED BY`（FLIP-463），但列顺序、类型覆盖和主键约束仍受限制，落地前用发行版文档确认。

显式 schema 的写法：

```sql
CREATE TABLE my_ctas_table (
  description STRING,
  cost AS price * quantity,
  WATERMARK FOR order_time AS order_time - INTERVAL '5' SECOND
) WITH (
  'connector' = 'kafka',
  ...
) AS
SELECT id, price, quantity, order_time FROM source_table;
```

规则是：CREATE 部分独有的列排在前面，CREATE 和 SELECT 同名的列保持 SELECT 中的位置；SELECT 列的类型可以被 CREATE 部分覆盖。主键列必须 `NOT NULL`：要么 SELECT 输出已经是 `NOT NULL`，要么在 CREATE 部分显式声明该列为 `NOT NULL`（FLIP-463 允许）。仍有一个限制：**不支持创建临时表**（用 `CREATE TEMPORARY VIEW` 代替）；分区和分布可以通过 CREATE 部分的 `PARTITIONED BY` / `DISTRIBUTED BY` 声明。

CTAS 不是物化视图：它只负责“建表 + 写入”，不负责刷新调度和新鲜度。需要“按新鲜度自动选择流/批刷新”的场景，用 14.8 的物化表。

### 14.9.2 原子性、失败语义与表生命周期

CTAS 默认是**非原子**的：目标表先在 Client 侧创建，然后提交 INSERT。作业失败、被取消或建表后写入失败时，表通常仍然存在，里面可能是空表或部分数据。这带来三个运维问题：

1. **重试会撞表**：第二次执行同一条 CTAS 可能因为表已存在而失败；要么加 `IF NOT EXISTS`（但不会修复残缺数据），要么显式删除/重建；
2. **权限分散**：CTAS 同时需要目标 Catalog 的 CREATE、目标存储的 WRITE，以及失败清理时的 DROP 权限；
3. **schema 漂移**：`SELECT` 输出变化后，目标表 schema 不会自动跟着变；要么固定查询输出，要么依赖 Catalog 的 schema 演进能力。

原子 CTAS 需要两个条件同时满足：

- 打开原子性开关 `table.rtas-ctas.atomicity-enabled`（1.18+ 即为此键，同时覆盖 RTAS 和 `CREATE OR REPLACE`）；
- 目标 sink 实现 `SupportsStaging`（FLIP-305）。planner 会通过 `StagedTable` 在 JobManager 侧建表，并在作业完成时提交、失败/取消时删除。

```sql
SET 'table.rtas-ctas.atomicity-enabled' = 'true';
```

流作业通常长时间运行，“建表失败后残留空表”比“作业中途失败”更容易处理，所以很多团队接受非原子 CTAS；批作业和一次性回填更值得开启原子性。

RTAS 会先删除再重建目标表，危险性更高：

- 流作业执行 RTAS 等于停掉旧写入并替换表，下游可能读到空窗；
- 失败时旧表是否还能恢复，取决于 Catalog 的 replace 实现和权限；
- 生产环境应把它当作“带数据风险的 DDL”，纳入变更审批，而不是日常发布手段。

### 14.9.3 CTAS、CDAS 与整库同步的边界

这是最容易混淆的一点：

- **CTAS 是开源 Apache Flink 的 SQL 能力**（FLIP-218，Flink 1.16+），流批都支持；
- **CDAS（`CREATE DATABASE AS SELECT`）不是开源 Flink 的语法**。它来自 Flink CDC / 阿里云实时计算等发行版的扩展，用一条语句同步整库并自动建表；
- 开源路线做整库同步，应使用 **Flink CDC Pipeline**（第 13 章 13.3）：YAML 描述 source/sink、路由和 schema 演进，由 pipeline 负责建表与转换；
- 如果只有少量表，逐表写 CTAS 更简单、可控，也更容易做权限和血缘管理。

| 需求 | 推荐 |
|---|---|
| 少量表建表并持续写入 | CTAS / RTAS |
| 整库同步、自动建表、schema 演进 | Flink CDC Pipeline |
| 按新鲜度自动刷新指标表 | 物化表（14.8） |
| 多个 sink 共享同一 source | Statement Set（14.4） |
| 复杂 ETL、复用 UDF/DataStream | `INSERT INTO` + 显式 DDL |

### 14.9.4 CTAS 生产检查清单

- [ ] 目标 Catalog 是否支持建表？内存 Catalog 是否只是“注册元数据”？
- [ ] sink 是否支持 CTAS/RTAS，是否实现 `SupportsStaging`？
- [ ] 失败后残留表由谁清理？是否需要 `IF NOT EXISTS` 或人工巡检？
- [ ] `SELECT` 输出 schema 变化时，目标表如何演进？是否固定列顺序和类型？
- [ ] 目标表的 owner、生命周期、保留策略和权限是否明确？
- [ ] 流模式 CTAS 的 source 是否有界？无界 source 会变成永久作业；
- [ ] checkpoint/savepoint 的算子 uid 是否稳定？RTAS 是否会破坏下游恢复？
- [ ] 是否评估过 CTAS 与“显式 DDL + INSERT INTO”在权限、审计和回滚上的差异？

## 14.10 SQL Client 与 Gateway

```bash
# SQL Client
./bin/sql-client.sh

# SQL Gateway（REST）
./bin/sql-gateway.sh start
```

SQL Client 适合交互式开发和调试；SQL Gateway 提供 REST/JDBC 接口，适合平台化。第 17 章会展开。

## 14.11 MiniFlink 的 Table 层

MiniFlink 的 Table 层把数据物化为行集合，用 SQLite 执行关系代数：

```python
env = TableEnvironment()
env.from_elements([
    {"user": "alice", "amount": 20},
    {"user": "bob", "amount": 15},
], name="orders")

rows = env.sql("""
    SELECT user, SUM(amount) AS total
    FROM orders
    GROUP BY user
    ORDER BY total DESC
""").execute()
```

支持 SELECT/WHERE/GROUP BY/HAVING/ORDER BY/LIMIT/JOIN、常用聚合和 `TUMBLE_START` 的简化翻译，也支持 `CREATE [OR REPLACE] TABLE/VIEW ... AS SELECT` 的教学子集：

```python
env.execute_sql("""
    CREATE TABLE user_totals AS
    SELECT user, SUM(amount) AS total
    FROM orders
    GROUP BY user
""")
print(env.sql("SELECT * FROM user_totals ORDER BY total DESC").execute())
```

MiniFlink 的 CTAS 会立即物化查询结果并注册成内存表，`VIEW` 同样物化；它演示“建表 + 写入”的形状，但没有 Catalog、外部存储、持续写入、schema 演进和原子建表。生产语义见 14.9。

不支持动态表、changelog、流式增量、Catalog、Watermark 语法和优化器。它适合验证 SQL 语义和窗口列，不是 Flink SQL 兼容层。生产 SQL 请使用 Flink 的 Calcite 解析与优化器。MiniFlink 的 Table/SQL 实现见[第 32 章](32-miniflink-table-cep.md)。

## 14.12 常见误区

- 把流式 SQL 当批 SQL，期望“执行完才输出”；
- 忘记结果表是 changelog，下游只支持 append；
- 无窗口 GROUP BY 导致状态无限增长；
- 没有 watermark 却用事件时间窗口；
- DDL 中 connector 参数写死生产地址；
- 修改 SQL 拓扑不更新 uid，savepoint 恢复失败；
- 把 `FRESHNESS` 当成 SLA；
- 把 CTAS 当成物化视图，期望它自动刷新；
- 用无界 source 跑 CTAS，却以为批作业会自己结束；
- 忽略 CTAS 失败后残留的目标表和重复建表冲突；
- 用 `SELECT *` 做生产同步，schema 变化时下游崩溃。

## 14.13 时间类型、时区与夏令时

流处理里的“时间不对”经常不是 watermark 算错，而是**类型和时区**用错。Flink SQL 有两类时间戳，语义完全不同：

| 类型 | 含义 | 是否带时区 | 典型用途 |
|---|---|---|---|
| `TIMESTAMP(p)` | 本地日期时间（wall clock），不带时区 | 否 | 业务“墙上时间”、分区字段 |
| `TIMESTAMP_LTZ(p)` | 自 epoch 起的瞬时时间，按会话时区显示 | 是（内部 UTC） | 事件时间、跨时区比较 |
| `DATE` / `TIME` | 日期 / 时间（无时区） | 否 | 分区、营业日 |
| `BIGINT` epoch millis | 整数时间戳 | 隐含 UTC | Kafka/CDC 原始字段 |

`TIMESTAMP(3)` 的 `10:00` 在上海和 UTC 是同一个字面值，但不是同一个瞬时；`TIMESTAMP_LTZ(3)` 的 `10:00` 会按会话时区解释成某个 epoch。事件时间窗口、join、watermark 都依赖“瞬时”语义，所以跨时区链路优先用 `TIMESTAMP_LTZ(3)`，或者在入口统一转成 UTC 的 `TIMESTAMP(3)`。

### 14.13.1 会话时区

```sql
SET 'table.local-time-zone' = 'UTC';
```

`table.local-time-zone` 决定：

- `TIMESTAMP_LTZ` 如何显示/解析；
- `CURRENT_TIMESTAMP`、`PROCTIME()` 等函数按哪个时区产生本地表示；
- `DATE_FORMAT`、`EXTRACT` 等函数如何取日期/小时；
- JDBC/文件 sink 写入字符串时间时如何格式化。

生产建议：

- 事件时间统一用 UTC 存储和计算；
- 只在展示层按用户时区转换；
- 不要依赖集群默认时区（不同节点/镜像可能不同）；
- 在作业配置里显式写 `table.local-time-zone: UTC`，并纳入代码评审。

### 14.13.2 从 Kafka/CDC 的 epoch 字段构造事件时间

很多上游传的是 epoch millis，而不是格式化字符串：

```sql
CREATE TABLE payments (
  event_id STRING,
  ts_ms BIGINT,
  event_time AS TO_TIMESTAMP_LTZ(ts_ms, 3),
  user_id STRING,
  amount DECIMAL(18, 2),
  WATERMARK FOR event_time AS event_time - INTERVAL '5' SECOND
) WITH (
  'connector' = 'kafka',
  'topic' = 'payments',
  'properties.bootstrap.servers' = 'kafka:9092',
  'format' = 'json'
);
```

`TO_TIMESTAMP_LTZ(ts_ms, 3)` 把 epoch 毫秒转成 `TIMESTAMP_LTZ(3)`，不经过本地时区猜测。不要写 `TO_TIMESTAMP(ts_ms)` 再隐式转换，否则很容易把“秒/毫秒”和“本地/UTC”混在一起。

如果上游是 ISO-8601 字符串（如 `2026-11-26T10:00:00Z`），优先在 source 的 JSON format 里声明 timestamp 格式，或先用 `TO_TIMESTAMP` 解析成 `TIMESTAMP`，再显式转换；不要让默认时区替你决定。

### 14.13.3 夏令时（DST）

DST 会产生两类边界：

- **gap**：本地时间 `02:30` 在春季调时钟时可能不存在；
- **overlap**：秋季回拨时 `01:30` 可能出现两次。

如果事件时间用 `TIMESTAMP`（无时区）+ 本地时区解释，窗口边界和去重键可能在 DST 日出现一小时偏差或重复。工程上用 UTC 可以规避：

```text
所有事件时间 → UTC epoch / TIMESTAMP_LTZ
窗口按 UTC 切分
展示层再转 America/New_York、Asia/Shanghai
```

如果业务必须按“当地营业日”切分（例如每天 00:00 关账），要明确：

- 营业日边界由业务时区定义，不是 UTC；
- DST 日可能只有 23 或 25 小时；
- 分区字段、报表口径、离线对账要使用同一套时区规则；
- 不要用 `DATE_FORMAT(event_time, 'yyyy-MM-dd')` 直接做分区，除非会话时区已固定。

### 14.13.4 时间类型与 watermark 的兼容性

事件时间列必须是 `TIMESTAMP(3)` 或 `TIMESTAMP_LTZ(3)`。把 `DATE`、`TIME`、`BIGINT` 直接声明为事件时间通常会被 planner 拒绝，或者得到不符合预期的 watermark。

```sql
-- 不推荐：BIGINT 直接当事件时间
event_time BIGINT,
WATERMARK FOR event_time AS event_time - 5000

-- 推荐：显式转成 TIMESTAMP_LTZ(3)
ts_ms BIGINT,
event_time AS TO_TIMESTAMP_LTZ(ts_ms, 3),
WATERMARK FOR event_time AS event_time - INTERVAL '5' SECOND
```

### 14.13.5 排查清单

- [ ] 事件时间列是 `TIMESTAMP(3)` 还是 `TIMESTAMP_LTZ(3)`？是否符合跨时区语义？
- [ ] 作业是否显式设置了 `table.local-time-zone`？
- [ ] Kafka/CDC 的 epoch 单位是秒还是毫秒？是否用了 `TO_TIMESTAMP_LTZ`？
- [ ] 窗口边界、分区字段、报表口径是否使用同一时区？
- [ ] DST 日的 23/25 小时是否被业务接受？
- [ ] 下游 JDBC/文件 sink 的时间格式是否与源端一致？
- [ ] 离线对账是否按同一时区和同一时间类型比较？

时间类型选错，watermark、窗口、join 和去重都会“看起来能跑，但对不上”。先把时区钉死，再谈乱序和延迟。

## 14.14 Catalog SPI 与元数据治理

14.2 讲的是“怎么用 Catalog”。当平台要接入内部元数据系统、做跨引擎共享、或者在多租户环境里隔离库表时，需要自己实现 Catalog。

### 14.14.1 Catalog 的职责边界

| 能力 | 说明 | 不做好会怎样 |
|---|---|---|
| 库/表/视图 CRUD | 元数据的增删改查 | 表定义漂移、作业互相覆盖 |
| 函数管理 | UDF 类名、资源 | UDF 版本不一致 |
| 分区管理 | 分区规格与属性 | 分区裁剪失效、读全表 |
| 权限挂载 | 库/表级授权 | 越权访问、审计缺失 |
| schema 演进 | 加列/改类型 | 下游查询失败或静默错列 |

Flink 的 `Catalog` 接口覆盖数据库、表、视图、函数、分区；`CatalogFactory` 负责按 `type` 创建；`AbstractCatalog` 提供默认实现；`CatalogStore` 负责 **Catalog 配置**的持久化（如 `table.catalog-store.kind` 与 Catalog 选项），不负责表元数据本身。

### 14.14.2 实现一个 Catalog 的最小形状

```java
public final class MyCatalog extends AbstractCatalog {

    public MyCatalog(String name, String defaultDatabase) {
        super(name, defaultDatabase);
    }

    @Override
    public void open() {
        // 建立到元数据系统的连接
    }

    @Override
    public List<String> listDatabases() {
        return List.of(getDefaultDatabase());
    }

    @Override
    public CatalogTable getTable(ObjectPath tablePath) {
        // Flink 2.x：使用 Schema + CatalogTable.newBuilder，不再使用 TableSchema/CatalogTable.of
        return CatalogTable.newBuilder()
            .schema(
                Schema.newBuilder()
                    .column("id", DataTypes.BIGINT())
                    .column("ts", DataTypes.TIMESTAMP(3))
                    .build())
            .comment("internal table")
            .partitionKeys(List.of())
            .options(Map.of("connector", "internal-orders"))
            .build();
    }
}
```

配套的 `CatalogFactory`：

```java
public final class MyCatalogFactory implements CatalogFactory {

    @Override
    public String factoryIdentifier() {
        return "my-catalog";
    }

    @Override
    public Catalog createCatalog(Context context) {
        return new MyCatalog(
            context.getName(),
            context.getOptions().get("default-database"));
    }
}
```

注册：

```sql
CREATE CATALOG my_cat WITH (
  'type' = 'my-catalog',
  'default-database' = 'realtime'
);
USE CATALOG my_cat;
```

### 14.14.3 常见 Catalog 与选型

| Catalog | `type` | 关键选项 | 适用 |
|---|---|---|---|
| Hive | `hive` | `hive-conf-dir`、`default-database` | Hadoop 生态、离线共享 |
| JDBC | `jdbc` | `base-url`、`default-database`、`username`、`password` | 关系库元数据 |
| Paimon | `paimon` | `warehouse`、`default-database` | 流式湖仓 |
| Iceberg | `iceberg` | `catalog-type`、`uri`、`warehouse` | 湖仓 |
| 内存 | `generic_in_memory` | 无 | 测试、本地 |

选型要和存储格式、权限系统、跨引擎访问一起看。Catalog 不只是“表名映射”，它还是 schema 演进、分区发现、权限挂载和跨引擎一致性的锚点。

### 14.14.4 常见错误

| 现象 | 原因 | 处理 |
|---|---|---|
| `Catalog not found` | 未注册/`type` 拼错 | 检查 `CREATE CATALOG` 与 factory 注册 |
| 表解析到错误的库 | 临时表/当前库/大小写 | `USE CATALOG/DB`，避免同名临时表 |
| schema 与远端不一致 | 缓存/未刷新 | 重新 `DESCRIBE`，检查 Catalog 缓存 |
| 多租户越权 | Catalog 层没有权限 | 在 Metastore/网关层加 RBAC |
| 重启后元数据丢失 | 用了内存 Catalog | 换持久化 Catalog（Hive Metastore/JDBC 等）；CatalogStore 只持久化 Catalog 配置 |
| CTAS 建表失败 | 权限/原子性/存储 | 检查 `table.rtas-ctas.atomicity-enabled` 与 Catalog 能力 |

Catalog 的完整开发手册（Catalog SPI、CatalogStore、元数据列、Table 连接器、测试与打包）见[附录 K](../appendix/K-table-connector-catalog.md)。如果问题从“表怎么注册”上升到“事实/维度怎么建模、SCD 怎么设计、指标是否可加”，见[附录 N](../appendix/N-realtime-warehouse-modeling.md)。从“语法怎么写”落到“去重/TopN/窗口/维表/CDC 的生产模式”，见[附录 O](../appendix/O-sql-patterns.md)。

## 本章小结

Table API/SQL 用关系模型描述流，用 changelog 表达更新。它降低了实时计算的门槛，但没有消除时间、状态和一致性这些底层问题。

下一章深入窗口 TVF、聚合、去重、TopN 和 changelog，看看这些 SQL 在运行时到底维护了什么状态。
