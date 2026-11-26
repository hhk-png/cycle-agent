# 第12章 维表 Join、Async I/O 与缓存

## 12.1 为什么维表 join 难

事实流是无限的，维表通常存放在 MySQL、HBase、Redis 或 OLAP 中。每来一条事实记录就查一次维表，会遇到：

- 外部系统 QPS 被打爆；
- 同步查询阻塞算子线程，吞吐骤降；
- 维表更新后，流作业如何感知；
- 查询失败/超时如何降级；
- 缓存一致性如何保证；
- 维表 key 倾斜导致热点查询。

这是实时宽表最典型的性能与稳定性问题。核心矛盾是：

```text
事实流的到达速率 × 每条记录的维表查询成本 > 外部系统的承载能力
```

解决思路只有三类：**不查**（广播/本地缓存）、**少查**（缓存/批量）、**异步查**（Async I/O）。

## 12.2 四种常见方案

| 方案 | 适用 | 优点 | 缺点 |
|---|---|---|---|
| 广播维表 | 小维表、变化不频繁 | 无外部查询、低延迟 | 维表必须能放进内存 |
| 查库（同步） | 低频查询、简单场景 | 实现简单 | 阻塞、QPS 高 |
| Async I/O | 高并发外部查询 | 提高吞吐、可控并发 | 需要异步客户端、超时/重试 |
| 本地缓存 + 查库 | 维表较大、热点集中 | 降低 QPS | 缓存一致性、内存 |

选择顺序通常是：能广播就广播；不能广播就 Async I/O + 缓存；最后才考虑同步查库。

### 决策树

```text
维表能否放进单个 subtask 的内存？
├─ 能，且更新可接受秒级延迟 → 广播维表
└─ 不能
   ├─ 外部系统支持异步客户端？
   │  ├─ 是 → Async I/O + 本地缓存
   │  └─ 否 → 线程池模拟异步（注意背压）/ 同步查库
   └─ 查询 QPS 是否可控？
      ├─ 是 → 同步查库 + 缓存
      └─ 否 → 必须先做缓存/预聚合，或改架构
```

## 12.3 广播维表

```java
MapStateDescriptor<String, Product> descriptor =
    new MapStateDescriptor<>("products", Types.STRING, Types.POJO(Product.class));

BroadcastStream<Product> products = env
    .fromSource(productSource, WatermarkStrategy.noWatermarks(), "products")
    .broadcast(descriptor);

orders.keyBy(Order::productId)
      .connect(products)
      .process(new BroadcastEnrich())
      .sinkTo(...);
```

```java
public class BroadcastEnrich
        extends BroadcastProcessFunction<Order, Product, Enriched> {

    @Override
    public void processElement(Order order, ReadOnlyContext ctx, Collector<Enriched> out) {
        Product p = ctx.getBroadcastState(DESCRIPTOR).get(order.productId());
        out.collect(new Enriched(order, p));
    }

    @Override
    public void processBroadcastElement(Product p, Context ctx, Collector<Enriched> out) {
        ctx.getBroadcastState(DESCRIPTOR).put(p.id(), p);
    }
}
```

注意：

- 广播流会复制到每个 subtask，维表不能太大；
- 更新是“最终一致”，主数据可能先到、规则后到；
- checkpoint 会保存广播状态；
- 维表变化频繁时，广播流量和状态更新成本高；
- 广播流没有 watermark 时，可能影响主流的 watermark 推进。

### 广播维表的容量估算

```text
每个 TaskManager 的广播状态 ≈ 维表大小 × 该 TM 上的 subtask 数
```

如果维表 1 GB、并行度 16、每台 TM 4 个 subtask，每台 TM 可能要多出 4 GB 堆内存。维表上 GB 级时，广播通常不可行。

## 12.4 Async I/O

同步查询：

```text
记录 1 → 查库（20ms）→ 输出
记录 2 → 查库（20ms）→ 输出
```

Async I/O：

```text
记录 1 ┐
记录 2 ├→ 并发查库 → 按顺序/完成顺序输出
记录 3 ┘
```

Flink 的 `AsyncDataStream` 提供两种模式：

- `orderedWait`：输出顺序与输入一致，延迟受最慢请求影响；
- `unorderedWait`：谁先完成谁先输出，吞吐更高，但顺序变化。

```java
DataStream<Enriched> enriched = AsyncDataStream.orderedWait(
    orders,
    new AsyncProductLookup(),
    2,                   // timeout 数值
    TimeUnit.SECONDS,    // timeout 单位
    100                  // 最大并发请求数（capacity）
);
```

关键参数：

- **timeout**：单请求超时，超时记录通常进侧输出或降级；
- **capacity**：同时 in-flight 的请求数。太小吞吐上不去，太大打爆外部系统；
- **orderedWait / unorderedWait**：是否保序；保序会受最慢请求影响；
- **checkpoint**：Async I/O 会把 in-flight 请求计入快照，恢复时可能重放，外部查询需幂等。

### Async I/O 的内部机制

Async I/O 不是“开个线程池随便查”。Flink 的 `AsyncWaitOperator` 维护：

- 一个按输入顺序编号的队列；
- 一组 in-flight 请求；
- 一个完成回调队列；
- checkpoint 时的 in-flight 快照。

```text
input queue: [1,2,3,4,5]
in-flight:   [2,3,4]  (capacity=3)
completed:   [2,4]
output:      按 ordered/unordered 规则输出
```

当 capacity 满时，算子会暂停消费上游，形成反压。这是**有意设计**：它把外部系统的压力传导回 source，而不是无限堆积请求。

## 12.5 AsyncFunction 的实现

```java
// 需要 open/close 生命周期时继承 RichAsyncFunction；
// 纯 AsyncFunction 接口没有这些方法，初始化逻辑不会被调用
public class AsyncProductLookup extends RichAsyncFunction<Order, Enriched> {
    private transient AsyncClient client;

    @Override
    public void open(OpenContext ctx) {
        client = AsyncClient.create(...);
    }

    @Override
    public void asyncInvoke(Order order, ResultFuture<Enriched> future) {
        client.get(order.productId())
            .thenApply(product -> new Enriched(order, product))
            .whenComplete((result, error) -> {
                if (error != null) {
                    future.completeExceptionally(error);
                } else {
                    future.complete(Collections.singleton(result));
                }
            });
    }

    @Override
    public void close() {
        client.close();
    }
}
```

要点：

- 不要在 `asyncInvoke` 里阻塞等待；
- 使用带连接池/限流的异步客户端；
- 超时、重试、熔断要显式实现；
- 失败记录进入侧输出，不要拖垮整个作业；
- `close` 释放客户端。

### 超时与重试

```java
client.get(id)
    .orTimeout(2, TimeUnit.SECONDS)
    .exceptionally(err -> fallback(id))
    .thenAccept(result -> future.complete(Collections.singleton(result)));
```

重试要区分：

- 可重试：网络超时、限流、5xx；
- 不可重试：数据格式错误、404、权限。

重试要有上限和退避，否则会把外部系统打得更惨。

## 12.6 缓存策略

- **本地 LRU**：每个 subtask 一份，热点 key 命中率高；更新不实时；
- **Redis 缓存**：共享缓存，容量大，增加一次网络往返；
- **Caffeine/Guava**：JVM 内缓存，注意堆内存和 TTL；
- **布隆过滤器**：挡掉不存在的 key，减少穿透；
- **空值缓存**：防止反复查不存在的维表；
- **版本号**：维表更新后通过广播/消息失效缓存。

```java
Cache<String, Product> cache = Caffeine.newBuilder()
    .maximumSize(100_000)
    .expireAfterWrite(Duration.ofMinutes(5))
    .build();
```

缓存一致性没有银弹：

- 实时风控可以接受秒级延迟；
- 账务类可能需要强一致，直接查库或使用事务型维表；
- 缓存失效消息可能丢失，要有 TTL 兜底；
- 空值缓存要短 TTL，避免“永久查不到”。

### 缓存穿透、击穿、雪崩

- **穿透**：查询不存在的 key，缓存不命中，每次都打库；用布隆过滤器/空值缓存。
- **击穿**：热点 key 过期瞬间大量请求打库；用互斥锁/逻辑过期。
- **雪崩**：大量 key 同时过期；给 TTL 加随机抖动。

## 12.7 SQL 中的 lookup join

```sql
SELECT o.order_id, o.amount, p.name
FROM orders AS o
JOIN products FOR SYSTEM_TIME AS OF o.proc_time AS p
ON o.product_id = p.id;
```

Flink SQL 的 lookup join 会生成外部查询算子，常见 hint：

```sql
/*+ LOOKUP('table'='products', 'async'='true', 'capacity'='100') */
```

不同版本/连接器支持的 hint 不同，要以文档为准。Temporal join 适合带版本号的维表（例如汇率、价格历史），普通 lookup join 只取当前值。

```sql
-- 版本化维表：取订单时间对应的价格
SELECT o.order_id, o.amount, p.price
FROM orders AS o
JOIN product_price FOR SYSTEM_TIME AS OF o.order_time AS p
ON o.product_id = p.product_id;
```

Temporal join 的维表必须声明版本时间/主键，且状态会保留历史版本。版本越多，状态越大。

## 12.8 监控与容量

关键指标：

- 外部查询 QPS、P99 延迟、错误率、超时率；
- Async I/O 的 in-flight 数、capacity 使用率；
- 缓存命中率、缓存大小、淘汰数；
- 侧输出脏数据量；
- 反压位置是否在 lookup 算子。

容量估算：

```text
所需并发 ≈ 峰值 QPS × 平均查询延迟（秒）
```

例如峰值 5,000 QPS、平均 20ms，理论并发约 100；再留 2 倍余量，并受外部系统限流约束。

如果外部系统 P99 是 200ms，峰值 5,000 QPS，则并发需要 1,000；这时要么加缓存，要么降低查询量，要么换存储。

## 12.9 MiniFlink 的实现

MiniFlink 提供：

```python
stream.async_map(lambda key: lookup(key), capacity=10)

orders.key_by(lambda order: order["id"]) \
      .connect(rules.broadcast()) \
      .process(Enrich())
```

`AsyncMapOperator` 用线程池模拟异步请求，按输入顺序回收结果；`BroadcastProcessFunction` 维护广播状态。两者都没有真正的异步客户端、超时、重试和 checkpoint in-flight 管理，属于形状演示。对应的运行时实现与边界见[第 29 章](29-miniflink-runtime.md)和[附录 C](../appendix/C-miniflink-limits.md)。

## 12.10 维表 join 检查清单

- [ ] 维表多大？能否放进单个 subtask 的内存？
- [ ] 维表更新频率？能接受多长的一致性延迟？
- [ ] 外部系统支持异步客户端吗？QPS/并发上限是多少？
- [ ] 超时、重试、熔断、降级策略是什么？
- [ ] 缓存 TTL、容量、穿透保护是否设计？
- [ ] 失败记录是否有侧输出和补偿？
- [ ] Async I/O 的 capacity 是否与外部系统容量匹配？
- [ ] checkpoint 时 in-flight 请求如何恢复？

## 本章小结

维表 join 的核心是“不要把外部系统的延迟直接变成流作业的延迟”。优先广播小表，其次 Async I/O + 缓存，最后才同步查库。无论哪种方案，都要设计超时、降级、缓存一致性和监控。

下一章进入 CDC 与实时湖仓：变化如何从数据库流出，如何以 exactly-once 写入湖表，以及小文件、schema 演进和实时数仓分层。
