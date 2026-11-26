# 第19章 双流 Join

## 19.1 四种双流 Join

| 类型 | 语义 | 状态 | 典型场景 |
|---|---|---|---|
| Regular Join | 两条流持续关联，任一侧到达都可能输出 | 两侧历史，可能无限 | 订单 + 支付，不要求时间窗口 |
| Interval Join | 只关联时间区间内的记录 | 按时间清理 | 支付后 30 分钟内发货 |
| Window Join | 同一窗口内关联 | 窗口结束清理 | 分钟级曝光/点击 |
| Temporal Join | 按事件时间取维表版本 | 版本表状态 | 汇率、价格历史 |

选择顺序：能窗口就窗口，能区间就区间，最后才 regular join。

```text
状态成本：Window/Interval/Temporal < Regular
语义灵活度：Regular > Temporal > Interval > Window
```

## 19.2 Regular Join 的状态陷阱

```sql
SELECT o.order_id, p.pay_id, o.amount
FROM orders o
JOIN payments p ON o.order_id = p.order_id;
```

流式语义：

- order 到达时，查历史 payment；没有则暂存 order；
- payment 到达时，查历史 order；没有则暂存 payment；
- 任一侧更新/删除都可能触发结果更新；
- 两侧历史都要保留，否则未来到达的记录无法关联。

```text
t1: order 1 到达    → 状态: order 1
t2: payment 1 到达  → 输出 (order 1, payment 1)
t3: order 2 到达    → 状态: order 1,2
t4: payment 1 更新  → 输出撤回/更新
...
状态只增不减，除非 TTL
```

如果订单和支付相隔很久，或者数据永远不完整，状态会无限增长。必须设置 `table.exec.state.ttl`，或改用 interval join。

```yaml
table.exec.state.ttl: 24h
```

注意：TTL 会让“迟到超过 24 小时的匹配”丢失。业务上是否可接受，要明确。

### Regular Join 的输出语义

- Inner join：只有匹配时输出；
- Left join：右表未匹配时输出 null，右表后续到达时发更新；
- Full join：两侧都可能补 null，更新更复杂；
- 下游必须能处理 changelog（+I/-U/+U/-D）。

## 19.3 Interval Join

```sql
SELECT o.order_id, s.ship_id
FROM orders o
JOIN shipments s
ON o.order_id = s.order_id
AND s.ship_time BETWEEN o.order_time AND o.order_time + INTERVAL '30' MINUTE;
```

语义：

- 只保留时间区间内的匹配；
- watermark 推进后，超出区间的状态可以清理；
- 状态大小与区间长度、流量成正比；
- 需要事件时间和 watermark；
- 两侧必须按同一个 key 分区。

```text
order 时间 t
shipment 允许区间 [t, t+30m]
watermark 超过 t+30m 后，order 状态可清理
```

这是实时风控、交易履约最常用的双流 join。

### Interval Join 的实现

```text
左流 keyBy → 状态: 按 key 存 [timestamp, record]
右流 keyBy → 状态: 按 key 存 [timestamp, record]
每条记录到达：
  扫描另一侧状态中时间区间内的记录
  输出匹配
watermark 推进：
  清理超出区间的状态
```

扫描是“按时间范围”的，不是全量。状态按时间排序/索引，才能高效清理。

### 边界语义

`BETWEEN lower AND upper` 通常是闭区间。要确认：

- 下界是否包含等于；
- 上界是否包含等于；
- 时间单位（毫秒/秒）；
- 事件时间还是处理时间。

差一个边界，结果可能差很多。

## 19.4 Window Join

```sql
SELECT o.window_start, o.user_id, o.amount, c.campaign
FROM TABLE(TUMBLE(TABLE orders, DESCRIPTOR(order_time), INTERVAL '5' MINUTES)) o
JOIN TABLE(TUMBLE(TABLE clicks, DESCRIPTOR(click_time), INTERVAL '5' MINUTES)) c
ON o.user_id = c.user_id AND o.window_start = c.window_start;
```

窗口 join 在窗口结束时输出，状态随窗口清理。适合明确的时间桶分析，不适合跨窗口的长事务。

```text
窗口 [10:00,10:05)
  orders: o1, o2
  clicks: c1, c2
  输出: o1×c1, o1×c2, o2×c1, o2×c2
```

窗口 join 的笛卡尔积可能爆炸：如果窗口内两侧各有 10 万条，输出是 100 亿条。必须确保 join key 足够细，或先聚合。

## 19.5 Temporal Join

```sql
SELECT o.order_id, o.amount * r.rate AS amount_cny
FROM orders AS o
JOIN exchange_rates FOR SYSTEM_TIME AS OF o.order_time AS r
ON o.currency = r.currency;
```

Temporal join 会保留维表版本，按事实时间取对应版本。适合：

- 汇率；
- 商品价格历史；
- 用户等级变更；
- 费率表。

维表版本状态可能很大，要设置 TTL 和合理的时间范围。

```text
汇率表:
  10:00 USD→CNY 7.10
  10:05 USD→CNY 7.12
订单 10:03 → 用 7.10
订单 10:06 → 用 7.12
```

Temporal join 要求维表有：

- 主键；
- 版本时间/事件时间；
- 可回溯的历史版本（或 changelog）。

事件时间版本表和“处理时间 lookup join”的工程差异、DDL、状态成本和常见错误见 19.10；不要把两者当成同一种 join。

## 19.6 数据倾斜

双流 join 按 key 分区，热点 key 会打满一个 subtask。表现：

- 某个 subtask busy 100%，其他空闲；
- checkpoint 卡在热点 subtask；
- 状态大小严重不均。

应对：

- 识别热点（采样、日志、metrics）；
- 加盐：热点 key 拆成 `key#0..N`，两侧都加盐，join 后再聚合；
- 广播小表；
- 两阶段 join；
- 单独处理大 key。

加盐不能两侧各用一次 `RAND()`：同一业务 key 在两侧会得到不同盐值，join 直接丢匹配。正确做法是**一侧按确定性哈希选盐，另一侧复制成 N 份**：

```sql
-- salts 是一张 0..15 的小表；只对热点 key 做复制更划算
-- 左表：user_id 的哈希决定一个盐
WITH orders_salted AS (
  SELECT *,
    CONCAT(user_id, '#', CAST(MOD(ABS(HASH_CODE(user_id)), 16) AS STRING)) AS salted_key
  FROM orders
),
-- 右表：复制 16 份，每份一个盐
payments_salted AS (
  SELECT p.*,
    CONCAT(p.user_id, '#', CAST(s.salt AS STRING)) AS salted_key
  FROM payments p
  CROSS JOIN salts s
)
SELECT ...
FROM orders_salted o
JOIN payments_salted p
  ON o.salted_key = p.salted_key;
```

这会放大右表 16 倍，只适合“右表较小/只复制热点 key”的场景。更稳妥的做法是：

- 先识别热点 key；
- 只对热点 key 复制/加盐，其他 key 走原路径；
- 或者用两阶段聚合/预聚合，避免大 key 直接 join；
- join 后再按业务 key 聚合，注意重复和撤回。

## 19.7 时间语义与空结果

- 没有 watermark，interval/window join 不会推进；
- 一侧长期无数据，watermark 停滞，join 状态不清理；
- outer join 需要处理 null 和撤回消息；
- 迟到数据可能导致 join 结果更新；
- 下游要能处理 changelog。

```text
左流有数据，右流空闲 → 右流 watermark 不推进 → 左流状态无法清理
```

解决：

- 给空闲流设置 idleness；
- 用处理时间/定时器补超时；
- 监控每个输入的 watermark。

## 19.8 Delta Join（2.1+）

Delta Join 针对“多条流来自同一 source、按相同 key 做多个 join”的宽表场景：它用外部索引表的双向点查替代两侧完整 join 状态（Fluss/Paimon 等），而不是为每个 join 各维护一份完整状态。它和本章前面的 regular/interval/window/temporal join 不是同一层概念——前四者是 join 语义，Delta Join 是优化器选择的执行策略。

SQL 形态、适用条件和 DELETE 限制见[第 16 章 16.3 节](16-sql-optimizer.md#163-join-策略)，这里不再重复。双流场景真正需要决策的是：用哪种 join 语义、状态保留多久、倾斜怎么处理；Delta Join 只在你已经确定要做多流同源宽表时才进入选型。

## 19.9 MiniFlink 的双流 Join

```python
left.join(right) \
    .where(lambda x: x["id"]) \
    .equal_to(lambda x: x["id"]) \
    .window(TumblingEventTimeWindows.of(Duration.of_seconds(10))) \
    .apply(lambda l, r: (l, r))

left.interval_join(right) \
    .between(Duration.of_seconds(0), Duration.of_seconds(30)) \
    .process(lambda l, r: (l, r))
```

MiniFlink 的窗口 join 在 watermark 越过窗口结束时输出笛卡尔积；interval join 维护左右缓冲，按时间区间匹配。两者都要求两侧先 keyBy 到相同 key 分区。

它没有 outer join、changelog、状态 TTL 和倾斜处理；这些是生产 Flink 的重点。

## 19.10 Temporal Join：事件时间版本 vs 处理时间查找

“Temporal Join”在 Flink 里常被混用，实际上有两种完全不同的语义：

| 形态 | 时间属性 | 右表 | 结果 | 可重放 |
|---|---|---|---|---|
| 事件时间 temporal join | `FOR SYSTEM_TIME AS OF fact.event_time` | 版本表（有历史） | 按事实时间取当时版本 | 是 |
| 处理时间 lookup join | `FOR SYSTEM_TIME AS OF fact.proc_time` | 外部维表/最新快照 | 按查询时刻取当前值 | 否 |

选错形态，轻则结果不可重放，重则状态爆炸或 join 永远不输出。

### 19.10.1 事件时间版本表

```sql
CREATE TABLE exchange_rates (
  currency STRING,
  rate DECIMAL(18, 4),
  update_time TIMESTAMP(3),
  WATERMARK FOR update_time AS update_time - INTERVAL '5' SECOND,
  PRIMARY KEY (currency) NOT ENFORCED
) WITH (
  'connector' = 'paimon',
  'path' = 's3://lake/warehouse/exchange_rates',
  'changelog-producer' = 'lookup'
);

SELECT
  o.order_id,
  o.amount * r.rate AS amount_cny
FROM orders AS o
JOIN exchange_rates FOR SYSTEM_TIME AS OF o.order_time AS r
  ON o.currency = r.currency;
```

语义：

- 对每条订单，取 `update_time <= order_time` 的最后一个版本；
- 版本表的 changelog 必须保留历史（`+I/-U/+U/-D`），不能只写最新快照；
- 右表需要主键和事件时间，否则 planner 无法确定版本；
- 版本状态会一直保留，直到 watermark/TTL 允许清理；
- 结果由事件时间决定，重放同一批输入得到同一结果。

版本表的状态大小取决于“key 数 × 版本数 × 保留时间”。汇率、价格、费率这类变化不频繁的维表适合；用户画像这种高频更新、每行都变的表不适合。

### 19.10.2 处理时间 lookup join

```sql
SELECT
  o.order_id,
  o.amount * d.rate AS amount_cny
FROM orders AS o
JOIN exchange_rates_latest FOR SYSTEM_TIME AS OF o.proc_time AS d
  ON o.currency = d.currency;
```

语义：

- 每条订单到达时，按当前处理时间查右表；
- 右表是外部系统（JDBC/HBase/Redis/OLAP）或“最新快照”；
- 不保留历史版本，重跑会得到不同结果；
- 需要 lookup cache、异步、重试、限流，否则会打爆维表；
- 适合“维表变化不频繁、可以接受最终一致”的场景。

lookup join 的优化：

```sql
SELECT /*+ LOOKUP('table'='d', 'async'='true',
                 'retry-predicate'='lookup_miss',
                 'retry-strategy'='fixed_delay',
                 'fixed-delay'='1s', 'max-attempts'='3') */ *
FROM orders AS o
JOIN dim FOR SYSTEM_TIME AS OF o.proc_time AS d
  ON o.dim_id = d.id;
```

以及连接器级 cache：

```sql
'lookup.cache.max-rows' = '100000',
'lookup.cache.ttl' = '10min'
```

cache 降低 QPS，但会引入陈旧数据；TTL 越短越新鲜，QPS 越高。不要用 cache 来“修复”维表更新延迟，二者是不同问题。

### 19.10.3 两种形态的工程取舍

| 问题 | 事件时间版本表 | 处理时间 lookup |
|---|---|---|
| 结果可重放 | 是 | 否 |
| 状态成本 | 高（保留版本） | 低（缓存/外部） |
| 维表更新可见性 | 按事件时间 | 按查询时刻 |
| 外部系统压力 | 低（读 changelog/湖表） | 高（点查） |
| 适合 | 汇率/价格/费率/等级历史 | 名单/配置/最新维表 |
| 失败语义 | 取决于版本表 changelog | 取决于外部系统 + cache |

实践建议：

- 需要审计、回放、对账 → 事件时间版本表；
- 维表小、变化慢、能接受秒级陈旧 → lookup + cache；
- 维表大、更新频繁、要求精确 → 先解决“版本从哪来”，不要硬做 lookup；
- 两种形态不要混在同一个 join 里，语义会失控。

### 19.10.4 常见错误

- 用 `window_start` 当 `FOR SYSTEM_TIME AS OF` 的时间属性：它不是时间属性，planner 会拒绝；
- 版本表只写最新值，没有 changelog，导致历史订单查不到当时版本；
- 忘记主键，版本表被当成 append 表；
- lookup join 用事件时间列做 `AS OF`，但连接器只支持处理时间；
- 维表更新后 cache 不失效，结果长期陈旧；
- 版本表没有 TTL，状态随历史无限增长；
- 把 lookup join 的结果写进需要 exactly-once 的链路，却忽略外部查询不可回滚。

## 19.11 双流 Join 检查清单

- [ ] 是否真的需要 regular join？能否用 interval/window/temporal？
- [ ] join key 的基数/分布如何？是否有热点？
- [ ] 状态保留多久？TTL 是否与业务语义匹配？
- [ ] 两侧 watermark 是否会卡住？空闲流如何处理？
- [ ] outer join 的 null/撤回下游能否处理？
- [ ] 窗口 join 的笛卡尔积是否可接受？
- [ ] temporal join 的版本表状态多大？
- [ ] checkpoint 是否因 join 状态超时？

## 本章小结

双流 join 的难点不是 SQL 语法，而是状态：保留多久、何时清理、如何应对倾斜、下游如何处理更新。优先选择有界状态（窗口/区间/temporal），谨慎使用 regular join。interval/regular/temporal/lookup join 的完整 SQL 模式与反模式见[附录 O](../appendix/O-sql-patterns.md)。

下一章进入 CEP 与广播状态：当业务需要匹配“事件之间的序列”时，如何用状态机而不是一堆 join 来表达。
