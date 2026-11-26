# 第15章 窗口 TVF、聚合与 Changelog

## 15.1 表值函数窗口

Flink SQL 用表值函数（TVF）表达窗口：

```sql
SELECT window_start, window_end, user_id, SUM(amount) AS total
FROM TABLE(
  TUMBLE(TABLE orders, DESCRIPTOR(order_time), INTERVAL '5' MINUTES)
)
GROUP BY window_start, window_end, user_id;
```

与旧版 `GROUP BY TUMBLE(...)` 相比，TVF 更符合 SQL 标准，也支持窗口连接、窗口 TopN 和窗口去重。

四种窗口：

| TVF | 语义 | 典型用途 |
|---|---|---|
| `TUMBLE` | 固定大小、不重叠 | 每分钟/5 分钟指标 |
| `HOP` | 固定大小、滑动 | 滑动平均 |
| `CUMULATE` | 累积窗口 | 从整点开始累计到当前 |
| `SESSION` | 活动间隔 | 用户会话 |

```text
TUMBLE(5m):   |---w1---|---w2---|---w3---|
HOP(5m,1m):   |---w1---|
                |---w2---|
                  |---w3---|
CUMULATE(1h,1d): |w1|----w2----|------w3------|
SESSION(gap):  |--s1--|      |--s2--|
```

## 15.2 滚动窗口聚合

```sql
SELECT
  window_start,
  window_end,
  user_id,
  COUNT(*) AS orders,
  SUM(amount) AS revenue
FROM TABLE(
  TUMBLE(TABLE orders, DESCRIPTOR(order_time), INTERVAL '1' MINUTE)
)
GROUP BY window_start, window_end, user_id;
```

结果在 watermark 越过窗口结束时输出。窗口状态只保留到允许迟到结束。

### 运行时状态

窗口聚合在运行时维护：

```text
key = (user_id, window_start, window_end)
state = 累加器（count, sum, ...）
timer = window_end（事件时间）或处理时间
```

状态大小 ≈ 活跃 key 数 × 每 key 活跃窗口数 × 累加器大小。窗口越多、key 基数越大，状态越大。

## 15.3 滑动窗口与累积窗口

```sql
-- 每 1 分钟统计最近 5 分钟
FROM TABLE(HOP(TABLE orders, DESCRIPTOR(order_time), INTERVAL '1' MINUTE, INTERVAL '5' MINUTES))

-- 每 10 分钟输出一次“当天累计到当前”的指标
FROM TABLE(CUMULATE(TABLE orders, DESCRIPTOR(order_time), INTERVAL '10' MINUTES, INTERVAL '1' DAY))
```

滑动窗口一条记录会进入多个窗口，状态和计算量随 `size/slide` 增长。累积窗口常用于“日累计”看板，避免重复扫描全天数据。

```text
HOP(size=5m, slide=1m)：一条记录进入 5 个窗口
CUMULATE(step=10m, max=1d)：一条记录进入 144 个窗口（但可增量合并）
```

累积窗口的实现通常比滑动窗口高效：它把每个 step 的结果增量合并，而不是重复扫描。但状态仍然与 step 数相关。

## 15.4 会话窗口

```sql
FROM TABLE(SESSION(TABLE events, DESCRIPTOR(event_time), INTERVAL '30' MINUTES))
```

会话窗口按 key 合并，适合用户行为分析。注意：

- 会话 gap 决定状态保留；
- 活跃用户可能形成超长会话；
- 会话结果可能更新，下游要能 upsert；
- 会话窗口的合并成本与活跃会话数相关。

## 15.5 窗口去重与 TopN

去重（保留每个 key 最新一条）：

```sql
SELECT *
FROM (
  SELECT *,
    ROW_NUMBER() OVER (PARTITION BY order_id ORDER BY order_time DESC) AS rn
  FROM orders
)
WHERE rn = 1;
```

窗口去重（只在窗口内去重）：

```sql
SELECT *
FROM (
  SELECT *,
    ROW_NUMBER() OVER (
      PARTITION BY window_start, window_end, order_id
      ORDER BY order_time DESC) AS rn
  FROM TABLE(TUMBLE(TABLE orders, DESCRIPTOR(order_time), INTERVAL '1' HOUR))
)
WHERE rn = 1;
```

窗口 TopN：

```sql
SELECT *
FROM (
  SELECT *,
    ROW_NUMBER() OVER (
      PARTITION BY window_start, window_end
      ORDER BY revenue DESC
    ) AS rn
  FROM (
    SELECT window_start, window_end, product_id, SUM(amount) AS revenue
    FROM TABLE(TUMBLE(TABLE orders, DESCRIPTOR(order_time), INTERVAL '5' MINUTES))
    GROUP BY window_start, window_end, product_id
  )
)
WHERE rn <= 10;
```

TopN 需要保留窗口内所有候选，状态比普通聚合大。高基数窗口要谨慎。

### 去重与 TopN 的状态优化

- 去重：只保留 `rn=1` 的记录，状态与 key 数相关；可以设置 TTL；
- TopN：需要保留 N 个候选 + 所有输入（用于更新排名），状态更大；
- 用 `RANK()`/`DENSE_RANK()` 会影响并列处理；
- 窗口 TopN 比全局 TopN 状态小，因为窗口结束后可清理。

## 15.6 窗口连接

```sql
-- 两条流按相同窗口 join
SELECT o.window_start, o.order_id, s.ship_id
FROM TABLE(TUMBLE(TABLE orders, DESCRIPTOR(order_time), INTERVAL '5' MINUTES)) o
JOIN TABLE(TUMBLE(TABLE shipments, DESCRIPTOR(ship_time), INTERVAL '5' MINUTES)) s
ON o.window_start = s.window_start AND o.order_id = s.order_id;
```

窗口连接把两条流按窗口对齐，适合“订单 + 曝光”“支付 + 发货”等场景。区间连接用 `INTERVAL` 条件表达时间范围。

如果要按版本查维表，那是 **temporal join**，不是窗口 join。窗口 TVF 提供的 `window_time` 才是行时间属性，`window_start`/`window_end` 只是普通时间列：

```sql
SELECT o.window_start, o.user_id, o.amount, p.name
FROM TABLE(TUMBLE(TABLE orders, DESCRIPTOR(order_time), INTERVAL '5' MINUTES)) o
JOIN products FOR SYSTEM_TIME AS OF o.window_time AS p
ON o.product_id = p.id;
```

写成 `FOR SYSTEM_TIME AS OF o.window_start` 通常会被 planner 拒绝，因为 `window_start` 不是时间属性。

窗口连接的运行时需要缓存窗口内两条流的数据，状态与窗口大小和到达速率相关。窗口越大，状态越大。

## 15.7 早期触发与迟到数据

先纠正一个常见误解：**窗口 TVF 不支持 legacy group window 的早触发/迟到重算**。`table.exec.emit.early-fire.enabled`、`table.exec.emit.late-fire.enabled` 只作用于旧版 `GROUP BY TUMBLE(...)`，对 `TABLE(TUMBLE(...))` 无效；FLINK-29692 至今仍是 open，没有给 TVF 补上触发器语义。不要在 TVF 后面写一个不存在的 `TABLE(...)` 参数并期待它生效。

窗口 TVF 的迟到控制主要靠 watermark：

- watermark 容忍度决定“等多久”；
- 超过 watermark 的迟到记录默认不能更新已关闭窗口；
- 需要更强控制时，改用 DataStream 窗口（`allowedLateness` + 侧输出）或 `KeyedProcessFunction`。

如果业务要“窗口没结束就先看一眼”，有三种现实做法：

1. **CUMULATE 窗口**：按 step 递增输出，天然是“逐步累计”的语义，不是早期触发，但能满足看板趋势；
2. **无界 GROUP BY 聚合 + 状态 TTL**：持续输出更新，状态不会按窗口自动关闭，要靠 TTL 控制；
3. **DataStream 窗口 + 自定义 Trigger**：需要精确控制触发/清理时，这是唯一完整的选择。

无论哪种，下游都要能处理同一个 key 的多次更新（upsert），不能只支持 append。

## 15.8 Changelog 与下游

窗口聚合的结果通常是 `+I` 或 `+U`：

- 第一次触发：插入；
- 允许迟到后再次触发：更新；
- 下游 Kafka 需要 upsert-kafka 或带 key 的 compacted topic；
- 数据库需要主键 upsert；
- 湖表需要 merge/upsert。

```sql
CREATE TABLE sink (
  window_start TIMESTAMP(3),
  window_end TIMESTAMP(3),
  user_id STRING,
  total DECIMAL(18, 2),
  PRIMARY KEY (window_start, user_id) NOT ENFORCED
) WITH (
  'connector' = 'upsert-kafka',
  'topic' = 'user_revenue',
  'key.format' = 'json',
  'value.format' = 'json'
);
```

如果下游只支持 append，应关闭 allowed lateness 或只输出最终结果，否则会出现重复/更新语义不匹配。

### SinkUpsertMaterializer

当 upsert 流经过有状态算子（如 join、聚合）后，可能出现乱序/撤回。`SinkUpsertMaterializer` 会维护一份状态，确保下游收到正确的 upsert 序列：

```yaml
# 2.2+ 的实现策略；2.3 的默认仍是 LEGACY
table.exec.sink.upsert-materialize-strategy.type: ADAPTIVE
# 是否启用物化：AUTO / NONE / FORCE
table.exec.sink.upsert-materialize: AUTO
```

代价是额外状态。2.2 的 V2 版本优化了性能，2.3 增加了 `ON CONFLICT` 语义。

## 15.9 物化表与窗口

物化表可以把窗口聚合封装成“按新鲜度刷新”的表：

```sql
CREATE MATERIALIZED TABLE user_revenue_1m
  FRESHNESS = INTERVAL '1' MINUTE
  AS
  SELECT window_start, window_end, user_id, SUM(amount) AS total
  FROM TABLE(TUMBLE(TABLE orders, DESCRIPTOR(order_time), INTERVAL '1' MINUTE))
  GROUP BY window_start, window_end, user_id;
```

它适合下游只关心“最近一分钟”的场景。注意：CONTINUOUS 模式下仍是流作业，状态和资源照常消耗；FULL 模式会全量重算；新鲜度是目标，不是硬 SLA。物化表的完整 DDL、`ON CONFLICT` 与刷新成本见[第 14 章 14.8 节](14-table-sql.md#148-物化表materialized-table)和[第 34 章 34.5 节](34-flink2-deep-dive.md#345-物化表把新鲜度变成一等公民)；`START_MODE` 是 FLIP-557 的 2.4 路线/厂商方言，2.3 开源语法没有，这里不重复。

## 15.10 MiniFlink 的窗口 SQL

MiniFlink 的 SQL 子集支持：

```sql
SELECT TUMBLE_START(ts, 5000) AS window_start, SUM(amount) AS revenue
FROM orders
GROUP BY window_start
ORDER BY window_start;
```

`TUMBLE_START` 会被翻译成 `(ts / 5000) * 5000`。这不是完整的 TVF 语义，只用于有界表教学。Table API 提供 `window_tumble(time_column, size)` 给每行补 `window_start/window_end`，再分组聚合。

MiniFlink 没有 watermark、changelog、早期触发和迟到更新，因此不能用来验证流式窗口 SQL 的语义，只能验证“窗口列 + 关系聚合”的形状。实现细节见[第 32 章](32-miniflink-table-cep.md)。

## 15.11 SQL CEP 与高级窗口函数

### MATCH_RECOGNIZE

SQL 也能表达序列模式。`MATCH_RECOGNIZE` 是 SQL 标准的行模式识别，适合“固定序列 + 时间约束”：

```sql
SELECT *
FROM orders
MATCH_RECOGNIZE (
  PARTITION BY user_id
  ORDER BY order_time
  MEASURES
    A.order_id AS first_id,
    B.order_id AS second_id,
    A.order_time AS first_time
  ONE ROW PER MATCH
  AFTER MATCH SKIP TO NEXT ROW
  PATTERN (A B)
  DEFINE
    A AS A.amount > 1000,
    B AS B.amount > 5000
);
```

关键选项：

- `ONE ROW PER MATCH` / `ALL ROWS PER MATCH`：每个匹配输出一行还是多行；
- `AFTER MATCH SKIP`：匹配后从哪继续（`TO NEXT ROW`/`PAST LAST ROW`/`TO FIRST`/`TO LAST`，用空格形式）；
- `PATTERN`：支持 `A B`、`A+`、`A*`、`A?`、`{- ... -}` 等；
- `DEFINE`：每个变量的条件；
- `WITHIN`：约束整个模式的时间跨度；Flink SQL `MATCH_RECOGNIZE` 的标准子句，用于限制未完成匹配的保留时间。

运行时它维护部分匹配状态，和 DataStream CEP 类似。`AFTER MATCH SKIP` 选错会导致漏匹配或重复匹配，必须用真实数据验证。

### LAG / LEAD

```sql
SELECT
  user_id,
  order_time,
  amount,
  LAG(amount, 1) OVER (PARTITION BY user_id ORDER BY order_time) AS prev_amount,
  LEAD(amount, 1) OVER (PARTITION BY user_id ORDER BY order_time) AS next_amount
FROM orders;
```

适合“与前一笔比较”“未来 N 笔”的场景。注意：

- 需要事件时间/处理时间排序；
- 状态与 key 数、保留行数相关；
- 无界流上要限制 `ORDER BY` 的范围/TTL，否则状态无限增长。

### OVER 聚合

```sql
SELECT
  user_id,
  order_time,
  SUM(amount) OVER (
    PARTITION BY user_id
    ORDER BY order_time
    RANGE BETWEEN INTERVAL '1' HOUR PRECEDING AND CURRENT ROW
  ) AS amount_1h
FROM orders;
```

OVER 聚合比窗口 TVF 更灵活，但状态/计算成本也更高。优先用窗口 TVF；需要“每行一个滚动指标”时再用 OVER。

### SQL CEP vs DataStream CEP

| 维度 | SQL `MATCH_RECOGNIZE` | DataStream CEP |
|---|---|---|
| 表达 | SQL 标准 | Pattern API |
| 状态控制 | 有限 | 更细 |
| 超时/侧输出 | 视版本 | 完整 |
| 调试 | EXPLAIN | 代码/日志 |
| 适用 | 固定序列、SQL 团队 | 复杂状态/外部调用 |

## 本章小结

窗口 TVF 把窗口、聚合和 SQL 组合起来，是实时指标开发的主力。要同时关注窗口类型、状态大小、changelog 语义和下游写入方式。`MATCH_RECOGNIZE`、`LAG/LEAD`、OVER 聚合扩展了 SQL 的表达力，但状态成本也更高。窗口、TopN、去重和 changelog 的完整 SQL 模式与反模式见[附录 O](../appendix/O-sql-patterns.md)。

下一章讨论 SQL 优化器与执行计划：一条 SQL 如何变成算子图，哪些规则会改变状态和性能，以及如何用 `EXPLAIN` 定位问题。
