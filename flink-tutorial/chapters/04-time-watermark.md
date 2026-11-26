# 第4章 时间语义、水位线与乱序

## 4.1 三种时间

Flink 中常见的三种时间：

- **Event Time**：事件实际发生的时间，通常由业务字段（下单时间、传感器时间）给出。它不随处理延迟改变，能重放、能对齐历史，是实时数仓和风控的首选。
- **Processing Time**：算子所在机器的系统时间。实现简单、延迟低，但结果依赖处理速度，重跑结果不同。
- **Ingestion Time**：数据进入 Flink source 的时间，介于两者之间。现代 API 中较少单独强调，通常用 source 的 processing time 或显式时间戳模拟。

一个例子：用户 10:00 下单，消息因网络延迟 10:05 才到。Event Time 认为它属于 10:00 的窗口；Processing Time 认为它属于 10:05 的窗口。两者算出的“10 点销售额”可能不同。

```text
事件发生        进入 Kafka       到达 Flink        被处理
10:00:00   →   10:00:01    →   10:05:00    →   10:05:02
   │               │               │               │
Event Time      Ingestion       Processing      Processing
（业务时间）    （入湖时间）      （处理时间）     （处理时间）
```

选择原则：

- 要可重放、可对账、跨时区一致 → Event Time；
- 只关心“现在系统看到什么”、可接受结果漂移 → Processing Time；
- 需要“入湖时刻”做审计、但不想依赖业务字段 → Ingestion Time（通常用 source 时间戳模拟）。

## 4.2 Watermark 是什么

Watermark 是一个时间戳 `W`，语义是：

> 事件时间小于等于 `W` 的数据已经全部到齐（允许用户定义的少量例外）。

它让无界流可以回答“窗口什么时候可以关闭”。没有 watermark，系统无法知道 10:00–10:05 的窗口是否还会来迟到数据，只能永远等下去或按处理时间猜测。

```text
事件到达顺序:  10:01, 10:03, 10:02, 10:07, 10:04
watermark:     ...  wm=10:02  ...  wm=10:07 ...
窗口 [10:00,10:05) 在 wm >= 10:05 时触发
```

Watermark 是**单调不减**的。算子收到多个输入时，取所有输入 watermark 的**最小值**，因为最慢的输入决定了“还有多少数据没到”。

```text
input A: wm=100 ─┐
                 ├─> effective wm = min(100, 80) = 80
input B: wm=80  ─┘
```

这里有一个容易被忽略的语义：**watermark 是“完整性承诺”，不是“时钟”**。它不表示“现在时间是多少”，而表示“我不再期待比 W 更早的数据”。因此：

- watermark 可以跳跃，不必逐毫秒推进；
- watermark 落后于真实时间不代表作业慢，可能只是乱序容忍度大；
- watermark 超前于真实时间则可能丢迟到数据。

## 4.3 生成策略

### 有界乱序

```java
WatermarkStrategy<Order> strategy = WatermarkStrategy
    .<Order>forBoundedOutOfOrderness(Duration.ofSeconds(5))
    .withTimestampAssigner((order, recordTimestamp) -> order.eventTime())
    .withIdleness(Duration.ofSeconds(30));
```

`forBoundedOutOfOrderness(5s)` 假设事件最多迟到 5 秒：

```text
watermark = max_event_time_seen - out_of_orderness
```

代价是窗口结果至少延迟 5 秒；如果实际乱序超过 5 秒，迟到数据会被丢弃或进入侧输出（取决于窗口配置）。

选择乱序容忍度的经验方法：

1. 采样一批真实数据，统计 `event_time` 与 `arrival_time` 的差值分布；
2. 取 P99 或 P99.9 作为初始值；
3. 观察窗口触发延迟和迟到数据量；
4. 在“结果及时性”和“迟到丢弃率”之间调参。

不要拍脑袋设 0 或 1 小时。前者几乎必然丢数据，后者让所有窗口延迟一小时。

### 单调时间戳

```java
WatermarkStrategy.<Order>forMonotonousTimestamps()
    .withTimestampAssigner((order, ts) -> order.eventTime());
```

适合日志、单分区有序数据。注意“单分区有序”不等于“全局有序”；多分区 source 仍要按分区取最小 watermark。

### 自定义生成器

```java
public class PunctuatedGenerator implements WatermarkGenerator<Event> {
    private long maxTs = Long.MIN_VALUE;

    @Override
    public void onEvent(Event event, long eventTimestamp, WatermarkOutput output) {
        maxTs = Math.max(maxTs, eventTimestamp);
        if (event.isEndOfBatch()) {
            output.emitWatermark(new Watermark(maxTs));
        }
    }

    @Override
    public void onPeriodicEmit(WatermarkOutput output) {
        // 也可以在这里按处理时间周期发射
    }
}
```

周期生成适合大多数场景；标点生成适合“消息里自带批次结束标记”的协议。

### 生成位置

Watermark 应该在**source 之后、有状态算子之前**生成：

```java
env.fromSource(kafkaSource, watermarkStrategy, "orders")
   .keyBy(...)
   .window(...);
```

不要把 watermark 生成放在 `keyBy` 之后，除非你明确知道并行度变化对 watermark 的影响。source 端的每个分区各自生成 watermark，下游再取最小，这是最自然的模型。

## 4.4 空闲分区与 Watermark 卡住

如果 Kafka 有 8 个分区，其中一个分区长时间没有数据，它的 watermark 会停在很久以前，下游取最小值后整体 watermark 不再推进，窗口永远不触发。

```text
partition 0: wm=10:05  ─┐
partition 1: wm=10:05  ─┤
partition 2: wm=10:05  ─┼─> min = 09:00  ← 卡住
partition 3: wm=09:00  ─┘（空闲）
```

两种处理：

- `withIdleness(Duration)`：分区空闲超时后，暂时把它排除在 watermark 最小值之外；
- 在 source 层保证每个分区都能推进 watermark，或减少不必要的分区。

```java
WatermarkStrategy.<Order>forBoundedOutOfOrderness(Duration.ofSeconds(5))
    .withTimestampAssigner((order, ts) -> order.eventTime())
    .withIdleness(Duration.ofSeconds(30));
```

空闲检测是“用正确性换可用性”的折中：被排除的分区如果突然来数据，可能已经是迟到数据，需要有 late data 策略。

## 4.5 Watermark 对齐与多输入

多分区/多分片场景还有另一个问题：某个分区跑得特别快，watermark 远远领先，导致它的数据在窗口里等待最慢的分区，状态膨胀。**Watermark alignment**（FLIP-182，2.3 修复了 backlog 场景）把对齐组作为目标，暂停领先分区的读取，直到最慢分区追上。

对齐参数在 **source 的 `WatermarkStrategy`** 上设置，不是一个全局 `watermark-alignment.max-drift` 配置：

```java
WatermarkStrategy.<Order>forBoundedOutOfOrderness(Duration.ofSeconds(5))
    .withTimestampAssigner((order, ts) -> order.eventTime())
    .withWatermarkAlignment(
        "orders-group",              // 对齐组
        Duration.ofSeconds(30),      // 最大漂移
        Duration.ofSeconds(1));      // 更新间隔
```

SQL connector 通常用表选项：

```sql
'scan.watermark.alignment.group' = 'orders-group',
'scan.watermark.alignment.max-drift' = '30s'
```

另外有两个全局开关：

```yaml
# 允许 split 级别的不对齐（默认通常为 false）
pipeline.watermark-alignment.allow-unaligned-source-splits: true
# 2.3 新增（实验）：对齐更新的环形缓冲，默认 3 个 update interval；
# 设为 0 恢复 2.2 的旧行为，一般不需要改
pipeline.watermark-alignment.buffer-size: 3
```

`buffer-size` 解决的是“有 backlog 时对齐反而限速”的问题（FLINK-37399）：对齐信息本身有传播延迟，2.2 及以前会把快分区误判为领先并暂停，导致积压只能按 `max-drift / update-interval` 的速度消化。2.3 用环形缓冲延迟几个周期再判断；代价是暂停稍晚，窗口状态可能略大。

它适合：

- 多分区 Kafka，分区速率差异大；
- 多个 source 的作业，某些 source 暂时无数据；
- 状态大、窗口多的作业，想限制“等待中的状态”。

代价是吞吐可能下降（人为暂停快分区），需要配合监控调 `max-drift`。

### 多输入的 watermark

对于 `union`、`connect`、双流 join，算子会收到多条输入。每条输入有自己的 watermark：

```text
input A: wm=100
input B: wm=80
effective wm = min(100, 80) = 80
```

只有当所有输入都推进到 80 以上，算子才会认为 80 之前的数据到齐。这也是为什么双流 join 中“一条流没数据”会让另一条流的窗口不触发。

处理方法：

- 给空闲流设置 idleness；
- 用 `coalesce`/`union` 前先做 watermark 对齐；
- 双流 join 设计时考虑“单边超时”逻辑，而不是无限等待；
- 监控每个输入的 watermark，定位是哪条流拖慢。

## 4.6 迟到数据

即使有 watermark，也总会有超预期迟到。处理方式：

1. **允许迟到**：`allowedLateness`，窗口在 watermark 过后仍保留一段时间，迟到数据会更新结果并再次触发；
2. **侧输出**：`sideOutputLateData(tag)` 把超过允许范围的记录收集起来，离线补算或告警；
3. **不处理**：直接丢弃，适合可接受的近似场景；
4. **重新定义 watermark**：增大乱序容忍度，代价是延迟增加；
5. **离线对账**：流式结果 + 批式修正，常见于实时数仓。

```java
OutputTag<Order> lateTag = new OutputTag<Order>("late") {};

SingleOutputStreamOperator<Result> result = stream
    .keyBy(Order::userId)
    .window(TumblingEventTimeWindows.of(Duration.ofMinutes(5)))
    .allowedLateness(Duration.ofMinutes(1))
    .sideOutputLateData(lateTag)
    .aggregate(new SumAmount());

DataStream<Order> late = result.getSideOutput(lateTag);
late.sinkTo(lateSink);
```

注意：

- 允许迟到会延长窗口状态的生命周期，状态和 checkpoint 都会变大；
- 每次迟到数据触发都会向下游发一条更新，下游必须能处理重复/撤回；
- 侧输出不是“免费”的，它也会占用网络和存储；
- `allowedLateness` 只对事件时间窗口有效，处理时间窗口没有“迟到”概念。

## 4.7 SQL 中的时间属性

Table API/SQL 用 `WATERMARK FOR ... AS ...` 声明时间列：

```sql
CREATE TABLE orders (
  user_id STRING,
  amount DECIMAL(10, 2),
  order_time TIMESTAMP(3),
  WATERMARK FOR order_time AS order_time - INTERVAL '5' SECOND
) WITH (...);
```

之后才能使用 `TUMBLE`、`HOP`、`SESSION`、`CUMULATE` 等窗口 TVF。事件时间列必须是 `TIMESTAMP(3)` 或 `TIMESTAMP_LTZ(3)`，且 watermark 表达式决定乱序容忍度。

```sql
SELECT
  user_id,
  window_start,
  window_end,
  SUM(amount) AS total_amount
FROM TABLE(
  TUMBLE(TABLE orders, DESCRIPTOR(order_time), INTERVAL '5' MINUTES))
GROUP BY user_id, window_start, window_end;
```

SQL 中的时间属性还有两个细节：

- **处理时间列**用 `PROCTIME()` 生成，不能用于 `TUMBLE` 的事件时间窗口；
- **watermark 表达式必须是确定性的**，不能引用非时间列或随机函数；
- 一个表只能有一个事件时间属性（用于窗口），但可以有多个处理时间列。

`TIMESTAMP` 与 `TIMESTAMP_LTZ` 的时区语义、epoch 转换和 DST 边界见[第 14 章 14.13 节](14-table-sql.md#1413-时间类型时区与夏令时)。

## 4.8 常见误区

- **把处理时间当事件时间**：结果不可重放，晚到数据归错窗口；
- **watermark 延迟设为 0**：任何乱序都会变迟到，生产上几乎不可行；
- **认为 watermark 是“全局时钟”**：它是每个分区、每个算子输入各自维护的单调值；
- **忘记空闲分区**：Kafka 多分区下窗口不触发的最常见原因之一；
- **把 allowedLateness 当作无限兜底**：它只延长有限时间，且会增大状态；
- **在 source 之后又 assign 一次时间戳**：可能覆盖或丢失原始时间语义；
- **只看作业的 watermark，不看每个 subtask**：某个 subtask 卡住时，全局指标可能被平均值掩盖；
- **误以为 watermark 会随 checkpoint 持久化**：watermark 本身不进 checkpoint；恢复后 source 从 checkpoint offset 重放并重新生成 watermark。窗口可能因重放再次触发，迟到判定、下游幂等和尖峰都要按这个语义验证。

## 4.9 监控与排障

关键指标：

- `currentInputWatermark`：每个 subtask 的当前 watermark；
- `currentOutputWatermark`：输出 watermark；
- watermark lag：Flink 没有内置的 `watermarkLag` 指标，通常用“当前系统时间 − `currentInputWatermark`”自定义 Gauge；
- `numLateRecordsDropped`：迟到丢弃数；
- `idleTimeMsPerSecond`：算子/分区的空闲比例；
- checkpoint 的 `lastCheckpointSize` 与窗口状态大小。

排障顺序：

1. 看 source 各分区的 watermark 是否推进；
2. 看是否有空闲分区/输入；
3. 看下游算子的 `currentInputWatermark` 是否被最小值卡住；
4. 看窗口是否因为 `allowedLateness` 一直保留；
5. 看 checkpoint 是否因为窗口状态过大而超时。

## 4.10 MiniFlink 的实现

MiniFlink 的 `WatermarkStrategy` 支持：

```python
strategy = WatermarkStrategy.for_bounded_out_of_orderness(
    Duration.of_seconds(5),
    timestamp_assigner=lambda event: event["ts"],
)
```

`CollectionSource` 每读一条记录就调用 `WatermarkGenerator.on_record`，按 `max_ts - out_of_orderness` 生成 watermark；结束时发射 `Watermark(+INF)`。多输入算子由 `RuntimeTask._recompute_watermark` 取所有未结束通道的最小值，和 Flink 的语义一致。

MiniFlink 的 `with_idleness` 只保存配置，没有实现真正的空闲检测；这是附录 C 中明确列出的差异。窗口的 `allowed_lateness` 与侧输出已实现，可用于观察迟到数据的路径。

```python
# MiniFlink 中观察 watermark 与迟到
execution = env.execute("late-demo")
print(execution.metrics()["tasks"])
```

## 本章小结

Watermark 是流处理从“逐条处理”走向“可关闭窗口、可重放”的关键。它用延迟换确定性：容忍多少乱序，就至少延迟多久。生产上要同时关注生成策略、空闲分区、多输入最小值、watermark 对齐、迟到数据和状态成本。

下一章讨论窗口如何消费 watermark，以及滚动、滑动、会话窗口在运行时到底维护了什么状态。
