# 附录 G Flink SQL 语法与函数速查

> 版本基线：Flink 2.3。SQL 语法随版本和 connector 变化，本附录只列常用子集；生产落地以所用发行版文档为准。MiniFlink 的 SQL 是 SQLite 子集，不能直接套用本附录的全部语法。

## G.1 表 DDL

### 基本表

```sql
CREATE TABLE orders (
  order_id STRING,
  user_id STRING,
  amount DECIMAL(18, 2),
  order_time TIMESTAMP(3),
  PRIMARY KEY (order_id) NOT ENFORCED
) WITH (
  'connector' = 'kafka',
  'topic' = 'orders',
  'properties.bootstrap.servers' = 'kafka:9092',
  'properties.group.id' = 'orders-sql',
  'scan.startup.mode' = 'earliest-offset',
  'format' = 'json'
);
```

### 水位线

```sql
CREATE TABLE orders (
  order_id STRING,
  amount DECIMAL(18, 2),
  order_time TIMESTAMP(3),
  WATERMARK FOR order_time AS order_time - INTERVAL '5' SECOND
) WITH (...);
```

- 事件时间列必须是 `TIMESTAMP(3)` 或 `TIMESTAMP_LTZ(3)`；
- 一个表只能有一个事件时间属性；
- watermark 表达式必须确定性、只引用时间列；
- 处理时间列用 `PROCTIME()`。

### 计算列与元数据列

```sql
CREATE TABLE orders (
  order_id STRING,
  amount DECIMAL(18, 2),
  order_time TIMESTAMP(3),
  proc_time AS PROCTIME(),
  order_date AS CAST(order_time AS DATE),
  kafka_partition INT METADATA FROM 'partition' VIRTUAL,
  kafka_offset BIGINT METADATA FROM 'offset' VIRTUAL,
  WATERMARK FOR order_time AS order_time - INTERVAL '5' SECOND
) WITH (...);
```

### 临时表与 Catalog

```sql
CREATE TEMPORARY TABLE tmp_orders (...);

CREATE CATALOG paimon WITH (
  'type' = 'paimon',
  'warehouse' = 's3://lake/warehouse'
);
USE CATALOG paimon;
USE `realtime`;
```

### CTAS / RTAS

```sql
-- 目标 schema 由 SELECT 推导；WITH 只写连接器参数
CREATE TABLE order_summary WITH (
  'connector' = 'paimon',
  'path' = 's3://lake/order_summary'
) AS
SELECT user_id, SUM(amount) AS total
FROM orders
GROUP BY user_id;

-- 2.x：显式列/watermark + 替换目标表
CREATE OR REPLACE TABLE order_summary (
  WATERMARK FOR window_end AS window_end - INTERVAL '5' SECOND
) WITH (...) AS
SELECT user_id, window_end, total FROM tmp_summary;
```

- 默认非原子：失败后目标表可能残留；原子性需 `table.rtas-ctas.atomicity-enabled=true` **且** sink 实现 `SupportsStaging`；
- 不支持临时表（用 `CREATE TEMPORARY VIEW`）；分区/分布可在 CREATE 部分用 `PARTITIONED BY` / `DISTRIBUTED BY` 声明；
- `CDAS`（`CREATE DATABASE AS SELECT`）不是开源 Flink 语法，整库同步用 Flink CDC Pipeline。

### 函数 DDL

```sql
CREATE FUNCTION my_udf AS 'com.acme.MyUdf';
CREATE FUNCTION my_ptf AS 'com.acme.MyPtf';
-- 2.3 的 artifact 语法（FLIP-559）：AS 只写类名，JAR 用 USING ARTIFACT
CREATE FUNCTION my_udf_jar AS 'com.acme.MyUdf' USING ARTIFACT 's3://libs/my-udf.jar';
```

## G.2 DML

```sql
INSERT INTO sink_table SELECT ... FROM source_table;
INSERT OVERWRITE sink_table SELECT ...;       -- 批模式/支持覆盖的 sink
```

2.3 的 upsert sink 冲突策略：

```sql
INSERT INTO upsert_sink
SELECT user_id, total FROM user_totals
ON CONFLICT DO DEDUPLICATE;   -- 或 DO NOTHING / DO ERROR
```

注意：

- `ON CONFLICT` 只适用于 upsert sink；
- 不支持时会在计划期报错；
- 不要用它替代业务去重。

## G.3 查询

### 基础

```sql
SELECT user_id, COUNT(*) AS cnt, SUM(amount) AS total
FROM orders
WHERE amount > 0
GROUP BY user_id
HAVING SUM(amount) > 1000
ORDER BY total DESC
LIMIT 100;
```

### 窗口 TVF

```sql
-- 滚动窗口
SELECT window_start, window_end, user_id, SUM(amount)
FROM TABLE(TUMBLE(TABLE orders, DESCRIPTOR(order_time), INTERVAL '5' MINUTES))
GROUP BY window_start, window_end, user_id;

-- 滑动窗口
SELECT window_start, window_end, user_id, SUM(amount)
FROM TABLE(HOP(TABLE orders, DESCRIPTOR(order_time), INTERVAL '1' MINUTE, INTERVAL '5' MINUTES))
GROUP BY window_start, window_end, user_id;

-- 累积窗口
SELECT window_start, window_end, user_id, SUM(amount)
FROM TABLE(CUMULATE(TABLE orders, DESCRIPTOR(order_time), INTERVAL '1' MINUTE, INTERVAL '1' HOUR))
GROUP BY window_start, window_end, user_id;

-- 会话窗口
SELECT window_start, window_end, user_id, COUNT(*)
FROM TABLE(SESSION(TABLE orders, DESCRIPTOR(order_time), INTERVAL '10' MINUTES))
GROUP BY window_start, window_end, user_id;
```

### 去重

```sql
SELECT *
FROM (
  SELECT *,
    ROW_NUMBER() OVER (PARTITION BY event_id ORDER BY event_time) AS rn
  FROM events
)
WHERE rn = 1;
```

保留最新一条：

```sql
ROW_NUMBER() OVER (PARTITION BY event_id ORDER BY event_time DESC)
```

### TopN

```sql
SELECT *
FROM (
  SELECT *,
    ROW_NUMBER() OVER (PARTITION BY category ORDER BY amount DESC) AS rn
  FROM orders
)
WHERE rn <= 10;
```

### 聚合扩展

```sql
SELECT user_id, region, SUM(amount)
FROM orders
GROUP BY GROUPING SETS ((user_id), (region), ());

SELECT user_id, region, SUM(amount)
FROM orders
GROUP BY CUBE(user_id, region);

SELECT user_id, region, SUM(amount)
FROM orders
GROUP BY ROLLUP(user_id, region);
```

### 窗口函数与 QUALIFY

```sql
SELECT *
FROM orders
QUALIFY ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY amount DESC) <= 3;
```

### Join

```sql
-- Regular join（注意状态）
SELECT o.order_id, u.name
FROM orders o
LEFT JOIN users u ON o.user_id = u.user_id;

-- Interval join（事件时间）
SELECT o.order_id, p.pay_id
FROM orders o, payments p
WHERE o.user_id = p.user_id
  AND p.pay_time BETWEEN o.order_time AND o.order_time + INTERVAL '10' MINUTE;

-- Lookup join（处理时间）
SELECT o.order_id, u.name
FROM orders o
LEFT JOIN users FOR SYSTEM_TIME AS OF o.proc_time AS u
  ON o.user_id = u.user_id;
```

### CEP / MATCH_RECOGNIZE

```sql
SELECT *
FROM orders
MATCH_RECOGNIZE (
  PARTITION BY user_id
  ORDER BY order_time
  MEASURES A.order_id AS first_id, B.order_id AS second_id
  ONE ROW PER MATCH
  PATTERN (A B)
  DEFINE
    A AS A.amount > 1000,
    B AS B.amount > 5000
);
```

## G.4 时间与水位线

```sql
-- 处理时间
proc_time AS PROCTIME()

-- 事件时间
event_time TIMESTAMP(3),
WATERMARK FOR event_time AS event_time - INTERVAL '3' SECOND

-- 时区
event_time TIMESTAMP_LTZ(3)
```

常见函数：

```sql
CURRENT_TIMESTAMP
NOW()
CURRENT_WATERMARK(event_time)
TIMESTAMPDIFF(SECOND, a, b)
TIMESTAMPADD(MINUTE, 5, ts)
TO_TIMESTAMP_LTZ(ts_ms, 3)
DATE_FORMAT(ts, 'yyyy-MM-dd HH:mm:ss')
```

## G.5 函数速查

### 字符串/条件

```sql
UPPER(s), LOWER(s), TRIM(s), CONCAT(a, b), CONCAT_WS(',', a, b)
SUBSTRING(s FROM 1 FOR 3), REGEXP_REPLACE(s, '\\d+', '')
CASE WHEN ... THEN ... ELSE ... END
COALESCE(a, b), NULLIF(a, b)
```

### 聚合

```sql
COUNT(*), COUNT(DISTINCT x), SUM(x), AVG(x), MIN(x), MAX(x)
STDDEV_POP(x), VAR_POP(x)
COLLECT(x)          -- 收集为数组/MAP，视版本
LISTAGG(x, ',')     -- 字符串聚合，视版本
```

### JSON / VARIANT

```sql
JSON_VALUE(json_col, '$.user_id')
JSON_QUERY(json_col, '$.items')
PARSE_JSON(text)           -- 2.1+
TRY_PARSE_JSON(text)
```

### 表函数 / PTF

```sql
SELECT * FROM TABLE(my_table_function(DESCRIPTOR(col)));

SELECT *
FROM my_ptf(
  input => TABLE orders PARTITION BY user_id,
  on_time => DESCRIPTOR(order_time)
);
```

### 模型推理 / 向量检索

```sql
-- ML_PREDICT（2.1+，语法以版本为准）
SELECT *
FROM ML_PREDICT(
  TABLE orders,
  MODEL risk_model,
  DESCRIPTOR(amount, tx_count_1m),
  MAP['async', 'true', 'timeout', '2s']
);

-- VECTOR_SEARCH（2.2+）
SELECT *
FROM events e,
LATERAL TABLE(VECTOR_SEARCH(
  TABLE knowledge_base,
  e.query_vector,
  DESCRIPTOR(embedding),
  10,
  MAP['async', 'true']
)) AS v;
```

### Changelog 转换（2.3 基础用法；2.4+ 增强）

```sql
SELECT *
FROM FROM_CHANGELOG(
  input => TABLE cdc_raw,
  op => DESCRIPTOR(op),
  op_mapping => MAP['c', 'INSERT', 'u', 'UPDATE_AFTER', 'd', 'DELETE']
);

SELECT *
FROM TO_CHANGELOG(input => TABLE user_totals);
```

2.3 不支持 `PARTITION BY`、`invalid_op_handling` 以及 `consume_full_deletes`（FROM_CHANGELOG）/`produces_full_deletes`（TO_CHANGELOG）的完整组合；这些增强在后续版本才逐步就绪。

## G.6 物化表

```sql
-- 2.3 开源语法：列/主键 + FRESHNESS；START_MODE 尚不在 2.3 中
CREATE MATERIALIZED TABLE user_revenue (
  user_id STRING,
  total DECIMAL(18, 2),
  PRIMARY KEY (user_id) NOT ENFORCED
)
FRESHNESS = INTERVAL '1' MINUTE
AS
SELECT user_id, SUM(amount) AS total
FROM orders
GROUP BY user_id;
```

```sql
ALTER MATERIALIZED TABLE user_revenue
  SUSPEND;   -- 具体语法以 2.3 文档为准
```

`FRESHNESS` 是目标，不是 SLA。`START_MODE` 是 FLIP-557 的 2.4 路线/厂商方言，常见取值包括 `FROM_BEGINNING`、`FROM_NOW`、`FROM_TIMESTAMP(...)`、`RESUME_OR_FROM_*`；2.3 开源语法没有这个子句。

## G.7 类型系统

| 类型 | 用途 |
|---|---|
| `STRING` | 文本 |
| `BOOLEAN` | 布尔 |
| `TINYINT/SMALLINT/INT/BIGINT` | 整数 |
| `DECIMAL(p, s)` | 金额/精确小数 |
| `FLOAT/DOUBLE` | 浮点 |
| `DATE/TIME/TIMESTAMP(3)/TIMESTAMP_LTZ(3)` | 时间 |
| `ARRAY<T>` | 数组 |
| `MAP<K, V>` | 映射 |
| `ROW<...>` | 结构化 |
| `VARIANT` | 半结构化 JSON（2.1+） |
| `BYTES` | 二进制 |

金额不要用 `FLOAT/DOUBLE`；时间统一 UTC 或明确时区。

## G.8 SQL Client / Gateway

```bash
./bin/sql-client.sh
./bin/sql-gateway.sh start
```

常用 Client 命令：

```text
SET 'execution.runtime-mode' = 'streaming';
SET 'table.exec.state.ttl' = '24h';
EXPLAIN SELECT ...;
EXPLAIN CHANGELOG SELECT ...;
```

## G.9 常见坑

- 没有 watermark 却用事件时间窗口；
- 无窗口 `GROUP BY` 状态无限增长；
- `SELECT *` 做生产同步，schema 变化打穿下游；
- 结果表是 changelog，下游只支持 append；
- lookup join 没有缓存/超时；
- 用 `FLOAT` 存金额；
- 时区/`TIMESTAMP` vs `TIMESTAMP_LTZ` 混用；
- `MATCH_RECOGNIZE` 的 `AFTER MATCH SKIP` 选错导致漏匹配/重复；
- 物化表 `FRESHNESS` 当成 SLA；
- `ON CONFLICT` 掩盖了上游乱序；
- 模型推理没有超时/限流/降级。
