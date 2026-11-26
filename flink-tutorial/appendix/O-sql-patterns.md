# 附录 O Flink SQL 生产模式库

第 14～19 章讲的是 Table API/SQL 的语义、优化器和 join 理论；附录 G 是语法与函数速查。本附录补的是两者之间最容易缺的一层：**当业务说“帮我按订单去重”“取每个品类前 10”“把 CDC 写进湖里”时，SQL 到底长什么样，状态和 changelog 会发生什么，哪些写法在生产上会失控。**

这里的“模式”不是复制粘贴的模板。每个模式都按同一个结构展开：

```text
问题 → SQL → 语义与状态 → 生产坑 → 何时不要用
```

> 版本基线：以 Flink 2.3 的 SQL 语法为主。连接器 DDL 只保留理解语义所需的选项，真实参数以所用连接器版本为准；窗口 TVF、物化表、changelog 算子的版本边界见第 15、34 章。本附录的 SQL 是生产写法参考，未在真实 Flink 集群逐条编译；可执行的 MiniFlink 子集见 O.18 与 `examples/22_sql_patterns.py`。

## O.1 使用约定

阅读下面的模式前，先固定四件事：

1. **时间属性**：源表必须有 `WATERMARK FOR ... AS ...` 或 `PROCTIME()` 计算列，窗口/interval join/temporal join 才有时间语义可依。
2. **主键**：CDC、upsert sink、版本表、去重模式都依赖主键；没有主键的表只能做 append 语义。
3. **changelog 能力**：查询产生 `+I/-U/+U/-D` 时，下游 sink 必须支持 upsert 或 retract；只支持 append 的 sink 会报错或丢更新。
4. **状态预算**：去重、TopN、regular join、无窗口聚合都是“有状态”的；没有 TTL 或窗口边界的模式，状态会随 key 基数增长。

```text
一个查询是否“流式安全”，先问三句：
- 它保留多少 key 的状态？
- 状态什么时候清理？
- 输出是 append 还是 changelog？
```

## O.2 模式一：按业务键去重

### 问题

上游至少一次投递、CDC 重放或客户端重试，导致同一业务主键出现多条记录。需要保留“最新一条”或“第一条”。

### SQL：保留最新一条

```sql
CREATE VIEW v_order_dedup AS
SELECT order_id, user_id, amount, event_time, ingest_time
FROM (
  SELECT *,
         ROW_NUMBER() OVER (
           PARTITION BY order_id
           ORDER BY event_time DESC, ingest_time DESC
         ) AS rn
  FROM ods_orders
)
WHERE rn = 1;
```

`ORDER BY` 里同时放 `event_time` 和 `ingest_time`，是为了在事件时间相同或时钟回拨时仍有确定的 tie-breaker。只按 `event_time` 排序，重复记录的去留可能随到达顺序变化。

### SQL：保留第一条

```sql
SELECT order_id, user_id, amount, event_time
FROM (
  SELECT *,
         ROW_NUMBER() OVER (
           PARTITION BY order_id
           ORDER BY event_time ASC, ingest_time ASC
         ) AS rn
  FROM ods_orders
)
WHERE rn = 1;
```

“第一条”适合“创建事件只应出现一次”的场景；如果业务允许更正，应该保留最新并让下游支持 upsert。

### 语义与状态

- 每个 `order_id` 至少保留一行候选，状态量与 key 基数成正比；
- 新记录到达后，旧结果可能被撤回（`-U`）再发出新结果（`+U`），所以输出是 changelog；
- 如果 sink 只支持 append，需要把查询改成“按事件时间窗口去重”，或先把 changelog 转成 append（见 O.10）。

### 生产坑

- **无界去重**：没有 TTL 时，历史订单永远占状态。可设置 `table.exec.state.ttl`，但要确认“超过 TTL 后同一 key 再来”是否允许被当作新记录。
- **用处理时间排序**：重放时结果会变，无法对账；业务去重应优先事件时间。
- **去重键选错**：用 `user_id` 去重会把同一用户的多个订单合并；用整行哈希去重会把“金额更正”当成新记录。
- **`DISTINCT` 不是去重最新值**：`SELECT DISTINCT *` 只能去掉完全相同的行，不能按主键选版本。

### 何时不要用

如果业务要求“同一订单只允许一条，迟到记录直接拒绝并告警”，应该用窗口 + 侧输出/死信表，而不是让 `ROW_NUMBER` 静默改写结果。

## O.3 模式二：TopN 与去重 TopN

### 问题

每个品类取销售额前 3、每个用户取最近 10 次点击。TopN 本质上是“按分区排序 + 截断”。

### SQL：每个品类前 3

```sql
CREATE VIEW v_category_top3 AS
SELECT category, product_id, sales, rn
FROM (
  SELECT category, product_id, sales,
         ROW_NUMBER() OVER (
           PARTITION BY category
           ORDER BY sales DESC, product_id ASC
         ) AS rn
  FROM product_sales
)
WHERE rn <= 3;
```

### SQL：先去重再 TopN

```sql
WITH dedup AS (
  SELECT product_id, category, sales
  FROM (
    SELECT *,
           ROW_NUMBER() OVER (
             PARTITION BY product_id
             ORDER BY update_time DESC
           ) AS rn
    FROM product_sales_raw
  )
  WHERE rn = 1
)
SELECT category, product_id, sales
FROM (
  SELECT *,
         ROW_NUMBER() OVER (
           PARTITION BY category
           ORDER BY sales DESC, product_id ASC
         ) AS rn
  FROM dedup
)
WHERE rn <= 3;
```

### 语义与状态

- 结果集每个分区最多 N 条；运行时通常保留候选集与必要的撤回信息，状态量至少与“分区基数 × N”同阶，乱序/回撤多时还会更大；
- 新记录可能把旧的第 N 名挤出榜单，输出是 changelog；
- 无窗口 TopN 的榜单是“截至当前的全局榜单”，不是“某个时间窗内的榜单”。业务说“今日 TopN”时，必须加窗口或时间过滤。

### 生产坑

- **分区键基数爆炸**：按 `user_id` 做 TopN，状态随用户数增长；按 `request_id` 做 TopN 基本等于不去重。
- **`RANK` 与 `ROW_NUMBER` 混淆**：并列名次用 `RANK`/`DENSE_RANK`，需要唯一行号用 `ROW_NUMBER`。
- **无时间边界**：流式 TopN 没有“今天”的概念，除非窗口或 `WHERE event_time >= ...` 明确限定。
- **输出顺序**：SQL 不保证输出按 `rn` 排序；需要有序结果时在下游或外层查询显式排序（有界场景）。

### 何时不要用

如果只需要“每个 key 的最大值”而不需要前 N 名，用 `MAX_BY`/`FIRST_VALUE` 或增量聚合，状态更小。

## O.4 模式三：窗口聚合

### 问题

按分钟/小时统计 GMV、PV、UV。窗口 TVF 把“时间切块”变成表值函数，是流式 SQL 的主力写法。

### 滚动窗口

```sql
CREATE VIEW v_gmv_1m AS
SELECT window_start, window_end, user_id,
       COUNT(*) AS order_cnt,
       SUM(amount) AS gmv
FROM TABLE(
  TUMBLE(TABLE orders, DESCRIPTOR(order_time), INTERVAL '1' MINUTE)
)
GROUP BY window_start, window_end, user_id;
```

### 滑动窗口

```sql
SELECT window_start, window_end, user_id, SUM(amount) AS gmv_5m
FROM TABLE(
  HOP(TABLE orders, DESCRIPTOR(order_time), INTERVAL '1' MINUTE, INTERVAL '5' MINUTE)
)
GROUP BY window_start, window_end, user_id;
```

滑动窗口的一条记录会进入多个窗口，状态和输出量按 `窗口大小 / 滑动步长` 放大。5 分钟窗口、1 分钟步长意味着 5 倍状态与输出。

### 累积窗口

```sql
SELECT window_start, window_end, user_id, SUM(amount) AS gmv_cumulative
FROM TABLE(
  CUMULATE(
    TABLE orders,
    DESCRIPTOR(order_time),
    INTERVAL '1' MINUTE,
    INTERVAL '1' HOUR
  )
)
GROUP BY window_start, window_end, user_id;
```

累积窗口适合“每小时累计，但每分钟更新一次”的看板；它比 60 个独立滚动窗口更省状态，但仍会为每个中间结果发一条 changelog。

### 会话窗口

```sql
SELECT window_start, window_end, user_id, COUNT(*) AS event_cnt
FROM TABLE(
  SESSION(TABLE orders PARTITION BY user_id, DESCRIPTOR(order_time), INTERVAL '10' MINUTE)
)
GROUP BY window_start, window_end, user_id;
```

会话窗口通常要按业务 key `PARTITION BY`，否则不同 key 会合并成全局会话；语法上 `PARTITION BY` 可选。它依赖 watermark 判断“会话是否结束”。如果某个用户长期无数据但分区未标记 idle，会话可能迟迟不关闭。

### 早期触发与迟到

先纠正一个常见误解：**窗口 TVF 不支持 legacy group window 的 `table.exec.emit.early-fire.*` / `late-fire.*` 配置**。这些键只作用于旧版 `GROUP BY TUMBLE(...)`，对 `TABLE(TUMBLE(...))` 无效（FLINK-29692 仍未给 TVF 补上触发器语义）。如果业务要“窗口没结束就先看一眼”，现实做法是：

1. 用 `CUMULATE` 窗口按 step 递增输出；
2. 用无界 `GROUP BY` 聚合 + 状态 TTL，接受状态不会按窗口自动关闭；
3. 改用 DataStream 窗口的 `allowedLateness` + 侧输出/自定义 Trigger。

窗口 TVF 的迟到控制主要靠 watermark：超过 watermark 的迟到记录默认不能更新已关闭窗口。不要为了“早触发”去写一个不生效的 TVF 参数。

### 语义与状态

- 滚动窗口状态 ≈ 活跃 key 数 × 窗口内聚合值；
- 滑动窗口按重叠倍数放大；
- 会话窗口需要维护“未合并的会话”，合并时状态瞬时上升；
- 所有窗口都依赖 watermark；watermark 不推进，窗口永远不关闭。

### 生产坑

- **窗口太大**：1 小时窗口 + 100 万活跃 key，状态和 checkpoint 都会很大；
- **窗口太小**：输出量爆炸，下游写入成为瓶颈；
- **水位线太激进**：迟到数据被丢弃；太保守则延迟高；
- **没有迟到处理**：窗口 TVF 下只能靠 watermark 容忍度；early/late fire 只适用于 legacy group window，TVF 也没有 DataStream 式侧输出。需要 `allowedLateness`/侧输出时改用 DataStream；
- **把窗口开始时间当事件时间**：`window_start` 是窗口边界，不是记录的业务时间。

### 何时不要用

如果业务只关心“最新值”而不是“时间段聚合”，用去重/TopN/LOOKUP，不要为了用窗口而强行开窗。

## O.5 模式四：维表 lookup join

### 问题

事实流需要补用户等级、门店区域、商品类目。维表在外部数据库/缓存中，不能全量广播。

### SQL：准备处理时间列

```sql
CREATE VIEW orders_with_proc AS
SELECT *, PROCTIME() AS proc_time
FROM orders;
```

### SQL：lookup join

```sql
CREATE TABLE dim_user (
  user_id STRING,
  region STRING,
  user_level STRING,
  PRIMARY KEY (user_id) NOT ENFORCED
) WITH (
  'connector' = 'jdbc',
  'url' = 'jdbc:mysql://dim-db:3306/dim',
  'table-name' = 'dim_user'
);

SELECT o.order_id, o.amount, d.region, d.user_level
FROM orders_with_proc AS o
JOIN dim_user FOR SYSTEM_TIME AS OF o.proc_time AS d
ON o.user_id = d.user_id;
```

`FOR SYSTEM_TIME AS OF o.proc_time` 表示“按处理时间取维表当前值”。它不是事件时间版本 join；如果要取“订单发生时的维表版本”，见 O.6。

### 异步与重试

```sql
SELECT /*+ LOOKUP('table'='dim_user', 'async'='true',
                   'retry-predicate'='lookup_miss',
                   'retry-strategy'='fixed_delay',
                   'fixed-delay'='1s',
                   'max-attempts'='3') */
       o.order_id, o.amount, d.region
FROM orders_with_proc AS o
JOIN dim_user FOR SYSTEM_TIME AS OF o.proc_time AS d
ON o.user_id = d.user_id;
```

异步 lookup 让一个 subtask 同时发起多个外部查询，避免逐条阻塞；但并发上限、连接池、下游 QPS 仍然要算进容量。

### 缓存

缓存通常由连接器提供，例如 JDBC connector 的 `lookup.cache.max-rows` / `lookup.cache.ttl`。缓存能显著降低 QPS，但会引入“维表更新后多久可见”的新鲜度问题：

```text
缓存 TTL 越长 → QPS 越低、结果越旧
缓存 TTL 越短 → 结果越新、外部压力越大
```

### 生产坑

- **每条记录一次查询**：没有缓存/异步时，外部数据库会被打爆；
- **lookup key 不唯一**：维表主键重复会返回多行，join 放大；
- **维表更新不可见**：缓存 TTL 内看到旧值，业务要接受这个新鲜度；
- **外部系统超时**：没有重试/降级会把整个作业拖死；
- **把 lookup join 当 temporal join**：处理时间取的是“现在”，不是“订单发生时”。

### 何时不要用

如果维表很小（几万行以内）且更新不频繁，广播维表更稳；如果必须按事件时间取历史版本，用 temporal join + 版本表。

## O.6 模式五：版本表 temporal join

### 问题

汇率、价格、客户等级会随时间变化。指标要按“订单发生时的版本”计算，而不是按“现在的最新值”。

### SQL：版本表

```sql
CREATE TABLE currency_rates (
  currency STRING,
  rate DECIMAL(18, 6),
  rate_time TIMESTAMP(3),
  WATERMARK FOR rate_time AS rate_time - INTERVAL '5' SECOND,
  PRIMARY KEY (currency) NOT ENFORCED
) WITH (
  'connector' = 'kafka',
  'topic' = 'currency_rates',
  'format' = 'debezium-json'
);
```

### SQL：事件时间 temporal join

```sql
SELECT o.order_id, o.amount, o.currency,
       r.rate,
       o.amount * r.rate AS amount_cny
FROM orders AS o
JOIN currency_rates FOR SYSTEM_TIME AS OF o.order_time AS r
ON o.currency = r.currency;
```

### 语义与状态

- Flink 内置 temporal join 用版本表的**主键 + 事件时间属性**标识版本；`FOR SYSTEM_TIME AS OF o.order_time` 取“小于等于探测时间的最后一个版本”；
- 状态量与版本表的保留策略、watermark/TTL 相关，不是自动保留无限历史；
- 如果业务需要显式的 `valid_from/valid_to` 区间和 as-of 查询，那是**手写 SCD2 表 + 普通/interval join** 的模型，不是内置 temporal join 的语义。

### 生产坑

- **版本表没有主键/时间属性**：无法确定“哪个版本生效”；
- **时间属性选错**：用处理时间做版本时间，重放会得到不同结果；
- **以为 temporal join 自动保留所有历史**：状态受版本表保留、watermark/TTL 限制；历史很长时要设计分层存储或回填；
- **手写 SCD2 的边界**：如果不用内置 temporal join，而是自己维护 `valid_from/valid_to`，要把 `valid_from <= t < valid_to` 写死，否则同一时刻匹配两个版本。

### 何时不要用

如果业务只关心“当前维表值”，lookup join 更简单、成本更低；temporal join 的复杂度只在“必须还原历史”时才值得。

## O.7 模式六：双流 join

### Interval Join：有界状态

```sql
SELECT o.order_id, o.amount, s.ship_time
FROM orders AS o
JOIN shipments AS s
ON o.order_id = s.order_id
AND s.ship_time BETWEEN o.order_time AND o.order_time + INTERVAL '1' HOUR;
```

两条流都必须是事件时间，且 join 条件包含时间区间。状态只保留区间内的记录，watermark 推进后可以清理，适合“下单后一小时内发货”的关联。

### Regular Join：状态无界

```sql
SET 'table.exec.state.ttl' = '24h';

SELECT o.order_id, o.amount, p.pay_time
FROM orders AS o
JOIN payments AS p
ON o.order_id = p.order_id;
```

Regular join 会保留两侧历史，任何一侧到达都可能产生结果；输出是 changelog（`+I/-U/+U`）。没有 TTL 时状态会无限增长。

### 语义与状态

| Join | 状态边界 | 输出 | 适用 |
|---|---|---|---|
| Interval | 时间区间 | append/更新取决于查询 | 有明确时间窗口的关联 |
| Regular | 无界（除非 TTL） | changelog | 任意时间到达的关联 |
| Temporal | 版本历史 | append/更新 | 按事件时间取版本 |
| Lookup | 外部表/缓存 | append | 维表当前值 |
| Delta | 外部索引 | append（宽表） | 多流同源、append-only |

### 生产坑

- **用 regular join 做“订单 + 支付”却要求严格一次**：两侧都可能乱序，结果会更新；下游必须支持 changelog；
- **TTL 设得太短**：晚到的支付找不到订单，结果丢失；
- **interval join 的区间写成负数/过大**：状态边界失真；
- **join key 倾斜**：热门 key 压垮单个 subtask，见 O.15。

### 何时不要用

如果只是补维表，优先 lookup/temporal；如果两条流来自同一 CDC 源且只需宽表，评估 Delta Join（第 16、19、34 章），不要默认 regular join。

## O.8 模式七：CDC 入湖

### 问题

MySQL/Postgres 的变更要实时进入 Paimon/Iceberg/Hudi，并保持 upsert 语义。

### SQL：CDC source

```sql
CREATE TABLE orders_cdc (
  order_id STRING,
  user_id STRING,
  amount DECIMAL(18, 2),
  order_time TIMESTAMP(3),
  PRIMARY KEY (order_id) NOT ENFORCED
) WITH (
  'connector' = 'kafka',
  'topic' = 'orders',
  'format' = 'debezium-json',
  'properties.bootstrap.servers' = 'kafka:9092'
);
```

### SQL：Paimon 主键表

```sql
CREATE TABLE paimon_orders (
  order_id STRING,
  user_id STRING,
  amount DECIMAL(18, 2),
  order_time TIMESTAMP(3),
  PRIMARY KEY (order_id) NOT ENFORCED
) WITH (
  'connector' = 'paimon',
  'path' = 's3://lake/warehouse/paimon/orders',
  'merge-engine' = 'deduplicate'
);

INSERT INTO paimon_orders
SELECT order_id, user_id, amount, order_time
FROM orders_cdc;
```

### 语义与状态

- 主键表按主键 upsert，下游查询看到“最新状态”；
- `deduplicate` 只保留最新一行；`partial-update` 允许多流补列；`aggregation` 允许预聚合；`first-row` 保留首行；
- 入湖的 exactly-once 依赖 Paimon 的提交协议与 checkpoint，不是 SQL 本身；
- 源端 DELETE 是否保留取决于 merge engine 和 `changelog-producer` 配置。

### 生产坑

- **没有主键**：只能写 append 表，无法表达更新；
- **schema 演进**：源端加列后，Paimon 表要能自动/手动演进；
- **小文件**：高频写入产生大量小文件，需要 compaction；
- **乱序更新**：用 `sequence.field` 指定版本字段，避免旧变更覆盖新变更；
- **把 CDC 当 append**：下游会看到重复/旧值。

### 何时不要用

如果只需要“最新快照”且能接受批处理延迟，定时全量同步可能更简单；CDC 的价值在“持续、低延迟、增量”。

## O.9 模式八：物化表与 CTAS

### 问题

业务要一张“每分钟刷新”的指标表，不想自己维护流作业、调度和全量重跑。

### SQL：物化表

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

### SQL：CTAS/RTAS

```sql
CREATE TABLE user_revenue_sink
WITH ('connector' = 'paimon', 'path' = 's3://lake/user_revenue')
AS
SELECT user_id, SUM(amount) AS total
FROM orders
GROUP BY user_id;
```

物化表由 Flink 1.20（FLIP-435）引入；2.2 让 `FRESHNESS` 可选并加入 `DISTRIBUTED BY`；2.3 补齐更完整的 `CREATE/ALTER`。`START_MODE` 是 FLIP-557 的 2.4 路线/厂商方言，2.3 开源语法没有。2.3 的 upsert sink 还引入 `INSERT ... ON CONFLICT`（FLIP-558），它是 DML 的冲突策略，不是 `CREATE MATERIALIZED TABLE` 的子句。CTAS 是 1.16（FLIP-218）、RTAS 是 1.18（FLIP-303）；2.0 的 FLIP-463 增加了 CTAS 的显式 schema 定义（`PARTITIONED BY`、`DISTRIBUTED BY`、`NOT NULL`）。

### 语义与状态

- 物化表是“持续维护的表”，不是“查询结果的快照”；
- `FRESHNESS` 是目标新鲜度，不是硬 SLA；
- `CONTINUOUS` 刷新仍消耗流资源，`FULL` 刷新需要调度与全量覆盖；
- `START_MODE`（2.4 路线/厂商方言）决定首次/重建时从哪读，直接影响回填范围和结果完整性；2.3 开源版没有这个子句。

### 生产坑

- **把 FRESHNESS 当 SLA**：资源不足、源端延迟、故障都会让实际新鲜度变差；
- **重建时没有回填契约**：2.3 没有 `START_MODE`，要靠 source 起始位点/表选项/全量重跑策略控制；支持 `START_MODE` 的版本/方言也要显式设置，否则可能回放海量历史或漏掉历史；
- **主键与查询 upsert key 不一致**：触发 materializer，状态和乱序风险上升；
- **CTAS 不是“建表 + 持续写入”的万能语法**：表生命周期、schema 演进、失败恢复仍要设计。

### 何时不要用

需要严格逐条语义、精确控制拓扑/状态、或源端不支持增量读时，不要用物化表；直接用流作业更可控。

## O.10 模式九：Changelog 规范化

### 问题

上游是带 `op` 列的追加流，或者下游只接受 append，而查询产生 retract/upsert。

### SQL：FROM_CHANGELOG

```sql
SELECT *
FROM FROM_CHANGELOG(
  input => TABLE cdc_raw,
  op => DESCRIPTOR(op),
  op_mapping => MAP['c', 'INSERT', 'r', 'INSERT',
                     'ub', 'UPDATE_BEFORE', 'ua', 'UPDATE_AFTER',
                     'd', 'DELETE']
);
```

### SQL：TO_CHANGELOG

```sql
SELECT *
FROM TO_CHANGELOG(input => TABLE user_revenue);
```

2.3 的 `FROM_CHANGELOG`/`TO_CHANGELOG` 只覆盖基础用法；`PARTITION BY`、`invalid_op_handling` 以及 `consume_full_deletes`（FROM_CHANGELOG）/`produces_full_deletes`（TO_CHANGELOG）的完整组合在后续版本才逐步就绪。未映射/映射错的 `op` 行为随版本和参数而异，可能被跳过、静默产生错误结果或运行期失败；上线前用包含脏 `op` 的数据验证。

### SinkUpsertMaterializer

当查询的输出 upsert key 与 sink 主键不一致、且 changelog 可能乱序时，Flink 会引入 `SinkUpsertMaterializer` 在 sink 前保序并物化。相关配置是 `table.exec.sink.upsert-materialize`（`AUTO`/`NONE`/`FORCE`，2.2+ 还有实现策略；2.3 默认仍是 `LEGACY`）。

### 生产坑

- **下游不支持 retract**：写入只接受 append 的 Kafka/文件时会报错或丢更新；
- **materializer 状态爆炸**：乱序严重、key 基数大时，物化状态会持续增长；
- **把 TO_CHANGELOG 当“去重”**：它只转换编码，不消除重复；
- **op 值映射漏项**：未映射的 op 可能被当成插入或直接失败。

### 何时不要用

如果下游本身支持 upsert（Paimon/JDBC/Redis），优先直接写 changelog，不要多套一层 materializer。

## O.11 模式十：半结构化数据

### 问题

埋点/日志的 JSON 字段频繁变化，不想为每个字段建强类型表。

### SQL：VARIANT

```sql
CREATE VIEW parsed_events AS
SELECT id, PARSE_JSON(payload) AS v
FROM raw_events;

SELECT
  id,
  VARIANT_VALUE(v, '$.user.id' RETURNING BIGINT) AS user_id,
  VARIANT_VALUE(v, '$.amount' RETURNING DECIMAL(18, 2)) AS amount,
  VARIANT_QUERY(v, '$.items' RETURNING ARRAY<STRING>) AS items,
  TO_JSON(v) AS normalized_json
FROM parsed_events;
```

脏数据用 `TRY_PARSE_JSON` 返回 `NULL`；重复 key 默认不允许，需要覆盖时显式开启。`VARIANT` 保留标量/数组/对象的类型信息，但不能随意 cast，取值要用 `VARIANT_VALUE`/`VARIANT_QUERY`。

### 生产坑

- **反复 PARSE_JSON**：同一列解析多次会浪费 CPU，用视图/计算列只解析一次；
- **把 VARIANT 当 schema 演进方案**：字段稳定且需要大量聚合时，解析成强类型列更快；
- **脏数据未隔离**：非法 JSON 会让作业失败；用 `TRY_PARSE_JSON` + 死信表；
- **存储格式不支持**：Parquet/湖格式对 VARIANT 的支持随版本演进，落地前验证读写链路。

### 何时不要用

字段稳定、查询需要大量数值运算时，强类型列 + schema 演进更合适。

## O.12 模式十一：模式匹配 MATCH_RECOGNIZE

### 问题

检测“连续三笔大额交易”“先登录失败再成功”等事件序列。CEP 在 SQL 里的入口是 `MATCH_RECOGNIZE`。

### SQL

```sql
SELECT *
FROM orders
MATCH_RECOGNIZE (
  PARTITION BY user_id
  ORDER BY order_time
  MEASURES
    A.order_time AS start_time,
    C.order_time AS end_time,
    A.amount + B.amount + C.amount AS total_amount
  ONE ROW PER MATCH
  PATTERN (A B C)
  DEFINE
    A AS amount > 100,
    B AS amount > 100,
    C AS amount > 100
);
```

### 语义与状态

- 每个分区维护部分匹配；模式越复杂、量词越多，状态越大；
- `ONE ROW PER MATCH` 每个匹配输出一行；`ALL ROWS PER MATCH` 输出匹配内的每一行；
- `AFTER MATCH SKIP` 决定匹配后从哪里继续，直接影响重叠匹配的数量；
- 依赖 watermark/处理时间推进来判断“模式是否超时”。

### 生产坑

- **部分匹配爆炸**：`A* B* C*` 这类模式在长序列上会保留大量状态；
- **没有超时**：未完成的匹配永远不清理；用 `WITHIN` 或分区空闲策略；
- **匹配后跳过策略**：`PAST LAST ROW` 可能漏掉重叠模式，`TO NEXT ROW` 可能放大输出；注意 Flink 语法用空格形式，不是下划线常量；
- **把 CEP 当规则引擎**：多规则、动态规则、复杂动作编排更适合广播状态/规则引擎（第 20 章）。

### 何时不要用

需要动态规则热更新、跨多条流、或复杂外部动作时，用 DataStream CEP + 广播状态或专门的规则引擎。

## O.13 模式十二：多语句与 STATEMENT SET

### 问题

同一条源流要写入多个 sink，且希望共享一个作业/checkpoint。

### SQL

```sql
BEGIN STATEMENT SET;

INSERT INTO sink_order_detail
SELECT order_id, user_id, amount FROM orders WHERE amount > 0;

INSERT INTO sink_order_metrics
SELECT user_id, COUNT(*) AS cnt, SUM(amount) AS total
FROM orders
GROUP BY user_id;

END;
```

### 语义与状态

- 多条 INSERT 编译进同一个 JobGraph，共享 source 读取和 checkpoint；
- 每个 sink 的 exactly-once 仍取决于 sink 自身的事务/幂等能力；
- 一条 INSERT 失败会导致整个 statement set 失败并一起恢复；
- 不是“跨 sink 原子提交”：checkpoint 完成不代表所有外部系统同时可见。

### 生产坑

- **把 statement set 当分布式事务**：两个 sink 之间仍可能短暂不一致；
- **共享 source 的并行度不匹配**：不同 INSERT 的并行度可能被统一，影响性能；
- **一条慢 sink 拖垮整个作业**：反压会传到共享 source；
- **状态预算按单条查询估**：多条有状态查询的状态会叠加。

### 何时不要用

如果两个 sink 的 SLA、发布节奏、故障域完全不同，拆成独立作业更稳；共享作业会放大爆炸半径。

## O.14 模式十三：数据质量与死信

### 问题

坏数据不能让作业失败，也不能静默丢弃。SQL 没有 DataStream 的侧输出，需要用“分流写表”表达。

### SQL：好/坏分流

```sql
BEGIN STATEMENT SET;

INSERT INTO good_orders
SELECT order_id, user_id, amount, order_time
FROM raw_orders
WHERE order_id IS NOT NULL
  AND amount IS NOT NULL
  AND amount >= 0;

INSERT INTO bad_orders
SELECT order_id, user_id, amount, order_time,
       CASE
         WHEN order_id IS NULL THEN 'missing_order_id'
         WHEN amount IS NULL THEN 'missing_amount'
         WHEN amount < 0 THEN 'negative_amount'
         ELSE 'unknown'
       END AS reason
FROM raw_orders
WHERE order_id IS NULL
   OR amount IS NULL
   OR amount < 0;

END;
```

### 语义与状态

- 两条 INSERT 共享 source，坏数据不会进入主链路；
- 死信表要保留原始字段、错误原因、摄入时间，便于回放；
- 这不是“自动修复”，只是隔离；修复仍要回到源端或离线流程。

### 生产坑

- **只写死信不监控**：坏数据比例上升没人知道；对死信表做计数告警；
- **过滤条件与业务口径不一致**：把合法数据误判为坏数据；
- **死信表也失败**：整个 statement set 回滚，主链路一起停；
- **用 `TRY_CAST` 静默变 NULL**：后续聚合可能把 NULL 当 0，结果失真。

### 何时不要用

如果坏数据需要复杂修复/外部调用，用 DataStream + 侧输出 + 死信队列更灵活。

## O.15 性能配置与状态预算

### 常用配置

```sql
-- 微批：降低状态访问和输出频率，代价是延迟
SET 'table.exec.mini-batch.enabled' = 'true';
SET 'table.exec.mini-batch.allow-latency' = '2s';
SET 'table.exec.mini-batch.size' = '5000';

-- 两阶段聚合：先局部聚合再 shuffle
SET 'table.optimizer.agg-phase-strategy' = 'TWO_PHASE';

-- 拆分 distinct 聚合，缓解热点
SET 'table.optimizer.distinct-agg.split.enabled' = 'true';

-- 状态空闲 TTL；按业务语义设置，不是越大越好
SET 'table.exec.state.ttl' = '24h';
```

### Join/聚合 hints

```sql
-- 小表广播
SELECT /*+ BROADCAST(dim) */ *
FROM fact JOIN dim ON fact.dim_id = dim.id;

-- 强制 hash shuffle
SELECT /*+ SHUFFLE_HASH(fact) */ *
FROM fact JOIN dim ON fact.dim_id = dim.id;

-- lookup join 异步/重试
SELECT /*+ LOOKUP('table'='dim', 'async'='true',
                   'retry-predicate'='lookup_miss',
                   'retry-strategy'='fixed_delay',
                   'fixed-delay'='1s',
                   'max-attempts'='3') */ *
FROM fact JOIN dim FOR SYSTEM_TIME AS OF fact.proc_time
  ON fact.dim_id = dim.id;
```

### 状态预算

```text
去重：key 基数 × 单行大小
TopN：分区基数 × N × 单行大小
Regular Join：两侧 key 基数 × 单行大小 × 保留版本数
Temporal Join：维表基数 × 版本数 × 单行大小
窗口：活跃 key 数 × 窗口内聚合值 × 重叠倍数
```

先估状态，再估 checkpoint 带宽，最后才估 CPU。状态是流 SQL 最容易失控的成本。

## O.16 模式选择决策树

```text
要解决什么问题？
├─ 同一主键多条记录 → 去重（O.2）
├─ 每个分组取前 N → TopN（O.3），注意时间边界
├─ 按时间段聚合 → 窗口 TVF（O.4）
├─ 补维表当前值 → lookup join（O.5）
├─ 补维表历史版本 → temporal join（O.6）
├─ 两条流按时间关联 → interval join（O.7）
├─ 任意时间到达的关联 → regular join + TTL（O.7）
├─ CDC 入湖 → 主键表 + merge engine（O.8）
├─ 声明式持续刷新 → 物化表/CTAS（O.9）
├─ 下游只接受 append → TO_CHANGELOG（O.10）
├─ JSON 结构不稳定 → VARIANT（O.11）
├─ 事件序列检测 → MATCH_RECOGNIZE（O.12）
└─ 多 sink 共享 source → STATEMENT SET（O.13）
```

## O.17 反模式清单

1. **无界 regular join 不设 TTL**：状态随业务历史无限增长；
2. **无界去重不设 TTL**：所有历史 key 永久保留；
3. **TopN 没有时间边界**：把“历史全局 TopN”当成“今日 TopN”；
4. **lookup join 每条记录查一次**：没有缓存/异步，外部系统被打爆；
5. **把处理时间当业务时间**：重放结果不可复现；
6. **append sink 接 changelog**：更新被丢弃或作业报错；
7. **用 `SELECT *` 做流式查询**：schema 演进和投影裁剪都失控；
8. **用 `DISTINCT` 做按主键去重**：不能选版本，只能去完全相同行；
9. **把物化表 `FRESHNESS` 当 SLA**：它只是目标，不是保证；
10. **把 STATEMENT SET 当跨 sink 事务**：checkpoint 不等于外部系统同时可见；
11. **忽略 changelog 乱序**：materializer 状态爆炸或结果回退；
12. **所有维表都广播**：小表变大后内存和 checkpoint 失控；
13. **窗口越小越好**：输出量和下游压力被低估；
14. **所有配置都调大**：TTL/并行度/buffer 都有反作用。

## O.18 可执行子集与练习

MiniFlink 的 Table 层是 SQLite 有界表实现，支持本附录的一部分“形状”：

- 去重（`ROW_NUMBER`）；
- TopN（`ROW_NUMBER` + `WHERE rn <= N`）；
- 窗口列（`window_tumble` / `TUMBLE_START` 翻译）；
- join、聚合、CTAS/VIEW AS SELECT 物化子集。

它不支持窗口 TVF 的流式语义、changelog、lookup/temporal join、Paimon、VARIANT 和 `MATCH_RECOGNIZE`。`examples/22_sql_patterns.py` 用一张有界订单表演示去重、窗口聚合、TopN 和维表 join，并断言结果；对应测试在 `tests/test_sql_patterns.py`。

练习建议：

1. 把 O.2 的去重改成“按事件时间窗口去重”，比较状态量和输出语义；
2. 给 O.3 的 TopN 加一个时间过滤，观察“全局榜单”和“今日榜单”的差异；
3. 用 O.5 的 lookup join 设计一个缓存 TTL 实验，量化 QPS 与新鲜度的取舍；
4. 把 O.8 的 Paimon 主键表换成 append 表，写出下游会看到的重复/旧值；
5. 用 O.12 的 `MATCH_RECOGNIZE` 设计一个“连续失败后成功”的检测，并评估 `AFTER MATCH SKIP` 对输出量的影响。

生产落地时，SQL 只是链路的一层。模式选对之后，仍要回到第 8、9、16、21、26 章检查 checkpoint、状态、反压和容量；模式选错，调优只会把错误算得更快。
