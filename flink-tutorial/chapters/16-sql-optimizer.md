# 第16章 SQL 优化、执行计划与调优

## 16.1 从 SQL 到算子

Flink SQL 的执行链路：

```text
SQL 文本
  → Calcite 解析（AST）
  → 校验（schema、类型、函数）
  → 逻辑计划（RelNode）
  → 优化（谓词下推、投影裁剪、join 重排、聚合拆分）
  → 物理计划（Flink 算子）
  → JobGraph
  → ExecutionGraph
```

`EXPLAIN` 可以看到逻辑计划和物理计划：

```sql
EXPLAIN
SELECT user_id, SUM(amount)
FROM orders
WHERE amount > 0
GROUP BY user_id;
```

还可以看 changelog 模式和代价：

```sql
EXPLAIN CHANGELOG
SELECT user_id, SUM(amount) FROM orders GROUP BY user_id;

EXPLAIN ESTIMATED_COST
SELECT ...;
```

输出通常包含 `LogicalProject`、`LogicalAggregate`、流式/批式聚合算子（如 `GroupAggregate`、`LocalGroupAggregate` + `GlobalGroupAggregate`）、`TableSourceScan`、`Sink` 等节点。节点名随版本/执行模式变化，不要只背名字，要关注：

- 扫描是否下推了过滤/投影；
- 聚合是否拆分；
- join 使用什么策略；
- 是否有不必要的 shuffle；
- 状态保留时间。

```text
== Optimized Physical Plan ==
GroupAggregate(groupBy=[user_id], select=[user_id, SUM(amount)])
+- TableSourceScan(table=[[orders]], fields=[user_id, amount], filter=[amount > 0])
```

这个计划说明过滤下推到了 source，投影只保留了需要的列。如果 `filter` 不在 scan 上，说明连接器不支持下推，数据会先全量读进来。

## 16.2 常见优化规则

### 谓词下推

把 `WHERE` 条件下推到 source/scan，减少读取和网络传输。如果连接器不支持，可能只在 Flink 内部过滤。

```sql
-- 好：过滤能下推到 Kafka/湖表
SELECT * FROM orders WHERE order_date = DATE '2026-11-26';

-- 差：函数包裹列，可能阻止下推
SELECT * FROM orders WHERE DATE_FORMAT(order_time, 'yyyy-MM-dd') = '2026-11-26';
```

### 投影裁剪

只读取用到的列。对列式存储（Parquet/ORC/Paimon）收益很大；对 Kafka JSON 仍要反序列化整条消息。

```sql
-- 只读两列
SELECT user_id, amount FROM orders;
```

### 聚合拆分

把 `SUM/COUNT/MAX` 拆成局部聚合 + 全局聚合，减少 shuffle 数据量。`COUNT(DISTINCT)` 可能拆成两阶段去重，代价高。

```text
PartialAggregate (local) → Hash Shuffle → FinalAggregate (global)
```

### MiniBatch

把短时间内的记录攒批处理，减少状态访问和输出。适合高吞吐、可接受小幅延迟的场景：

```yaml
table.exec.mini-batch.enabled: true
table.exec.mini-batch.allow-latency: 5s
table.exec.mini-batch.size: 5000
```

MiniBatch 的代价：

- 结果延迟增加（最多 allow-latency）；
- 状态更新批量进行，checkpoint 可能更大；
- 不适合超低延迟场景。

### Local/Global 聚合

先在各 subtask 做局部聚合，再按 key shuffle 做全局聚合，降低网络传输。

```text
source → local agg → shuffle → global agg → sink
```

### 去重优化

`ROW_NUMBER() = 1` 的去重会保留每个 key 的状态；可以配置状态 TTL 或使用 `Deduplicate` 算子。无界的去重状态会无限增长。

## 16.3 Join 策略

| 策略 | 适用 | 特点 |
|---|---|---|
| Hash Join | 等值连接、有界/流式 | 构建侧放内存，状态可能大 |
| Sort-Merge Join | 批、大表 | 需要排序，吞吐高 |
| Broadcast Hash Join | 小表 | 广播小表，避免 shuffle |
| Lookup Join | 维表 | 外部查询，需 Async/缓存 |
| Interval Join | 双流时间区间 | 状态有界（按时间清理） |
| Temporal Join | 版本表 | 按事件时间取版本 |
| Delta Join | 多流同源 | 外部索引点查替代两侧完整状态，减少重复读取 |

流式 regular join 会无限保留两侧状态，必须设置 TTL 或改用 interval/temporal join。广播 join 要确保小表真的小。

### Join Hints

优化器不总是知道你的数据分布，可以用 hint 干预：

```sql
-- 强制广播小表（避免 shuffle 大表）
SELECT /*+ BROADCAST(dim) */ *
FROM fact JOIN dim ON fact.dim_id = dim.id;

-- 强制走 hash join/shuffle
SELECT /*+ SHUFFLE_HASH(fact) */ *
FROM fact JOIN dim ON fact.dim_id = dim.id;

-- lookup join 的重试/异步参数
SELECT /*+ LOOKUP('table'='dim', 'async'='true',
                 'retry-predicate'='lookup_miss',
                 'retry-strategy'='fixed_delay',
                 'fixed-delay'='1s', 'max-attempts'='3') */ *
FROM fact JOIN dim FOR SYSTEM_TIME AS OF fact.proc_time
  ON fact.dim_id = dim.id;
```

Hint 不是魔法：`BROADCAST` 要求小表真的能放进每个 subtask 的内存；`LOOKUP` 的并发/重试最终仍受外部系统承载能力限制。先看 `EXPLAIN` 确认优化器原本选了什么，再决定是否干预。

### Delta Join（2.1+）

Delta Join 针对“多条流来自同一 source、按相同 key join”的场景：

```sql
SELECT ...
FROM orders o
JOIN shipments s ON o.order_id = s.order_id
JOIN payments p ON o.order_id = p.order_id;
```

传统计划会为每个 join 保留一份状态；Delta Join 不再保留两侧完整 join 状态，而是把 join 变成对外部索引表的双向点查（Fluss/Paimon 等支持点查/增量读的存储），本地只留缓存和键队列。它适合：

- 多张表/多个 topic 的宽表构建；
- 同源 CDC 的多流 join；
- 状态大、重复读取成本高的场景。

限制：SQL 模式有要求，CDC 源不能有 DELETE（部分版本），需要看具体版本支持。

### 倾斜 Join

如果某个 key 特别大，hash join 会把它压到一个 subtask。解决：

- 加盐：大 key 拆成多个，join 后再合并；
- broadcast：小表广播，避免 shuffle；
- 两阶段聚合：先局部去重/聚合；
- 拆分作业：把热点业务独立处理。

## 16.4 状态与 Retained Time

流 SQL 的许多算子需要保留历史：

- regular join：保留两侧输入；
- dedup：保留每个 key 的最新记录；
- TopN：保留候选集；
- 无窗口聚合：保留每个 key 的累加器。

配置：

```yaml
table.exec.state.ttl: 24h
```

TTL 太短会导致结果错误（历史被删），太长会导致状态膨胀。要按业务语义设置，而不是拍脑袋。

```sql
-- 会话/作业级设置：影响当前会话后续提交的查询，不是只作用于下一条 SQL
SET 'table.exec.state.ttl' = '12h';
```

注意：TTL 是“状态空闲时间”，不是“事件时间窗口”。它按处理时间/写入时间计算，和 watermark 无关。需要按单个查询或单表控制时，优先用查询级 hint/表选项（若版本支持），不要依赖 `SET` 的“下一条生效”误解。

## 16.5 并行度与资源

SQL 作业的并行度可以按算子设置：

```sql
SET 'parallelism.default' = '8';
```

但 source 并行度受分区数限制，sink 受目标系统限制。盲目提高并行度会增加小文件、连接数和调度开销。

```sql
-- 给特定算子设置并行度（部分版本支持 hint）
SELECT /*+ PARALLELISM(16) */ ...
```

并行度调优顺序：

1. source：按分区数；
2. 聚合/join：按 key 基数和状态大小；
3. sink：按目标系统承载能力；
4. 中间算子：按反压位置微调。

## 16.6 批模式优化

批模式下还有：

- 动态分区裁剪；
- 多路 join 重排；
- 自适应执行（部分版本）；
- 算子融合与代码生成；
- 数据倾斜处理（skew join）；
- 自适应批调度（2.x 默认更智能）。

批模式可以使用阻塞 shuffle，内存和磁盘管理方式与流模式不同。

```sql
SET 'execution.runtime-mode' = 'batch';
SET 'table.optimizer.join-reorder-enabled' = 'true';
```

## 16.7 2.x 的 SQL 增强

Flink 2.x 在 SQL 层增加了不少能力：

- **物化表**（1.20+）：一条 DDL 描述持续/全量刷新；
- **PTF**（2.1）：Process Table Functions，支持有状态、定时器、事件驱动的表函数；
- **ML_PREDICT**（2.1/2.2）：在 SQL 中调用模型；
- **VECTOR_SEARCH**（2.2）：流式向量检索；
- **VARIANT/PARSE_JSON**（2.1）：半结构化数据；
- **QUALIFY**（2.0）：窗口函数过滤；
- **FROM_CHANGELOG/TO_CHANGELOG**（2.3）：changelog 与 append 流互转；
- **SinkUpsertMaterializer V2**（2.2）：优化 upsert 物化。

这些能力会改变执行计划和状态布局。升级/使用时，要重新看 `EXPLAIN`，不要假设旧计划仍然最优。

## 16.8 调优方法

1. 用 `EXPLAIN` 看计划，确认过滤/投影/聚合是否合理；
2. 看 Web UI 的算子指标：records in/out、busy、backpressure、state size；
3. 找瓶颈：source、shuffle、状态访问、sink；
4. 小步调整：并行度、mini-batch、TTL、join 策略；
5. 每次只改一个变量，记录基线；
6. 用真实数据量和倾斜分布压测。

不要在没有指标的情况下“凭感觉调参数”。

### 一个排障例子

```text
现象：SQL 作业延迟高，checkpoint 超时
计划：Regular Join 两侧都保留全量状态
指标：state size 800 GB，checkpoint 20 分钟
原因：join 没有时间约束，状态无限增长
修复：改为 interval join + state TTL，状态降到 80 GB
```

## 16.9 MiniFlink 的边界

MiniFlink 的 Table 层用 SQLite 执行有界查询，没有 Calcite、逻辑计划、优化器和流式算子。它可以帮助理解关系代数结果，但不能用来学习 Flink 的 SQL 优化。生产优化以 Flink `EXPLAIN` 和实际指标为准。

## 16.10 SQL 算子状态布局与状态预算

SQL 调优最后都会落到两个问题：**哪个算子有状态？每个 key 保留多少？** 只看“总 state size”不够，因为同样 500 GB，可能是 1 亿个 key 的小状态，也可能是 10 万个 key 的超大状态；前者恢复慢在元数据/文件数，后者慢在单点加载和倾斜。

### 16.10.1 常见算子的状态布局

| SQL 形态 | 运行时算子 | 状态大致是什么 | 何时清理 |
|---|---|---|---|
| 无窗口 `GROUP BY` | GroupAggregate / LocalGroupAggregate + GlobalGroupAggregate | 每个 key 的累加器 | 只能靠 TTL/显式更新，不会自动结束 |
| 窗口 TVF 聚合 | WindowAggregate | 每个 key × 窗口的累加器 + 窗口元数据 | watermark 越过窗口 + allowed lateness 后 |
| `ROW_NUMBER()=1` 去重 | Deduplicate | 每个 key 的当前记录/排名 | TTL 或窗口结束 |
| TopN | Rank/TopN | 每个 key 的候选集/排序缓冲 | TTL 或窗口结束 |
| Regular Join | Join | 两侧历史输入（可能无限） | 仅 TTL/业务清理 |
| Interval Join | IntervalJoin | 两侧按时间索引的缓冲 | watermark 越过区间 |
| Temporal Join | TemporalJoin | 版本表状态（事件时间）或 lookup 缓存（处理时间） | 版本保留/TTL/cache TTL |
| Lookup Join | LookupJoin | 通常是本地缓存，是否进 checkpoint 取决于 connector | cache TTL/容量 |
| Upsert 物化 | SinkUpsertMaterializer | 每个 upsert key 的排序/撤回状态 | 随查询状态/TTL |

这张表解释了为什么“SQL 写起来短”不等于“运行时便宜”。一个 `JOIN` 可能比十个窗口聚合更贵。

### 16.10.2 用 EXPLAIN 找状态算子

```sql
EXPLAIN
SELECT o.user_id, SUM(p.amount)
FROM orders o
JOIN payments p ON o.order_id = p.order_id
GROUP BY o.user_id;
```

在计划里重点看：

- `Join` 的 `joinType` 和是否有 `state`/`retain` 信息；
- `GroupAggregate` 是 global 还是 local-global；
- 是否有 `Deduplicate`、`Rank`、`SinkUpsertMaterializer`；
- `EXPLAIN CHANGELOG` 的结果模式（append/retract/upsert）；
- 是否有 `LookupJoin` 及其 cache 配置。

计划节点名随版本变化，不要背名字；要能回答“这个节点为什么需要记住历史”。

### 16.10.3 状态预算公式

```text
单算子状态 ≈ 活跃 key 数
           × 每 key 状态条目数
           × 每条目序列化字节
           × 后端放大系数（RocksDB 1.5～3）
```

再叠加：

```text
总状态 ≈ Σ 各算子状态
checkpoint 上传量 ≈ 全量状态 × 变化率（增量快照）
恢复时间 ≈ 下载量 / 带宽 + 单 task 最大状态加载时间
```

容量规划时不要只算平均值：**单 subtask 的最大状态**决定恢复尾部，**key 倾斜**决定 checkpoint 是否被一个 task 拖住。

### 16.10.4 一个状态预算表

| 项 | 估算 | 说明 |
|---|---|---|
| 去重 key 数 | 5000 万 | 每 key 约 200 B |
| 去重状态 | 10 GB | 加 RocksDB 放大 ~2× → 20 GB |
| 1 小时窗口 key 数 | 200 万/分钟 × 60 | 窗口累加器 + 定时器 |
| Regular Join 两侧 | 订单 1 亿 + 支付 1 亿 | 不设 TTL 会持续增长 |
| TopN 候选 | 每 key N 条 | N 越大，状态和更新成本越高 |

如果表里任何一行“没有上限”，就要有 TTL、窗口或业务清理方案；否则它会在流量增长时变成事故。

### 16.10.5 TTL 不是免费的

`table.exec.state.ttl` 能限制空闲状态，但：

- 它按处理时间/空闲时间计算，不是事件时间；
- TTL 太短会让迟到的 join/去重结果错误；
- TTL 清理是惰性的，物理文件可能不会立刻下降；
- 不同算子对 TTL 的支持粒度不同，窗口状态通常由 watermark 清理而不是 TTL；
- TTL 不能替代“查询设计”——无界 regular join 仍然会保留活跃 key 的全量历史。

### 16.10.6 调优顺序

1. **改查询语义**：能否用窗口/interval/temporal 替代 regular join？能否先去重/预聚合？
2. **减 key/减条目**：只保留必要字段，避免把大对象放进状态；
3. **设保留边界**：TTL、窗口、版本保留、cache 容量；
4. **调并行度**：只对非热点 key 有效；
5. **调后端/资源**：RocksDB、增量 checkpoint、本地恢复；
6. **最后才调参数**：mini-batch、buffer、timeout。

状态预算是 SQL 作业的“内存账本”。先算清楚每个算子记什么、记多久，再谈并行度和参数。

## 本章小结

SQL 优化不是背参数，而是理解“数据从哪里来、经过哪些 shuffle、保留多少状态、写到哪里”。执行计划是地图，指标是路况。具体到去重、TopN、窗口、lookup/temporal join 的写法与状态预算，见[附录 O](../appendix/O-sql-patterns.md)。

下一章讨论 UDF、SQL Gateway 和客户端生态：当内置函数不够用时，如何安全地把业务逻辑带进 SQL。
