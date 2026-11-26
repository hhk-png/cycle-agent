# 第5章 窗口机制

## 5.1 窗口是“把无界流切成有界块”

窗口算子回答三个问题：

1. **怎么切**：每条记录属于哪些窗口（WindowAssigner）；
2. **什么时候算**：窗口何时触发（Trigger）；
3. **算什么、留什么**：窗口内是保留原始记录，还是增量聚合（WindowFunction / AggregateFunction / Evictor）。

```text
事件流:   a(1s)  b(2s)  a(3s)  c(6s)  d(11s)
滚动 5s:  [0,5)  -> a,b,a      [5,10) -> c      [10,15) -> d
滑动 5s/2s: 每个事件属于多个窗口，窗口之间重叠
会话 3s:  a,b,a 间隔 <3s 合并；c 与前面间隔 3s，可能新会话
```

窗口不是“缓存一批数据再算”这么简单。它需要：

- 为每个 key、每个窗口维护独立状态；
- 根据 watermark 或处理时间决定何时触发；
- 处理迟到数据、重复触发和状态清理；
- 在 checkpoint 中保存/恢复窗口状态；
- 在扩缩容时重新分配 key。

## 5.2 分配器

Flink 提供：

- `TumblingEventTimeWindows.of(size)`：固定大小、不重叠；
- `SlidingEventTimeWindows.of(size, slide)`：固定大小、可重叠；
- `EventTimeSessionWindows.withGap(gap)`：按活动间隔动态合并（处理时间用 `ProcessingTimeSessionWindows`）；
- `GlobalWindows`：所有记录一个窗口，默认 trigger 是 `NeverTrigger`，必须显式配置 trigger 才会输出；
- 处理时间版本：`TumblingProcessingTimeWindows` 等。

窗口大小/滑动步长在分配器创建时就是**常量**。DataStream 窗口 API 不支持“每条记录用不同 size/slide”，Table SQL 的 `TUMBLE`/`HOP` 也只接受常量 `INTERVAL`。如果业务需要由事件字段动态决定 size/slide，要用 `KeyedProcessFunction` + 状态/定时器自己实现（第 6 章）。会话窗口是例外：`EventTimeSessionWindows.withDynamicGap(...)` / `DynamicProcessingTimeSessionWindows` 支持逐条提取 gap，不需要手写算子。

窗口边界是左闭右开 `[start, end)`。事件时间窗口按记录时间戳分配，处理时间窗口按算子本地时钟分配。**同一个窗口函数里不要混用两种时间**，否则结果不可重放。

### 一条记录属于哪些窗口

```java
// 滚动：每条记录恰好属于一个窗口
.window(TumblingEventTimeWindows.of(Duration.ofMinutes(5)))

// 滑动：size=5m, slide=1m，每条记录最多属于 5 个窗口
.window(SlidingEventTimeWindows.of(Duration.ofMinutes(5), Duration.ofMinutes(1)))

// 会话：先按 gap 生成初始窗口，再合并
.window(EventTimeSessionWindows.withGap(Duration.ofMinutes(10)))
```

滑动窗口的成本要特别注意：`size/slide` 是“每条记录的窗口副本数”。size=1h、slide=1s 时，一条记录会进入 3600 个窗口，状态和计算量都不可接受。生产上优先滚动窗口，或用 SQL 的窗口 TVF + 增量聚合。

## 5.3 触发器与驱逐器

Trigger 决定何时 FIRE/PURGE：

- `EventTimeTrigger`：watermark 越过窗口结束；
- `ProcessingTimeTrigger`：处理时间越过结束；
- `CountTrigger`：窗口内记录数达到阈值；
- `PurgingTrigger`：包装其他触发器，触发后清空；
- `ContinuousEventTimeTrigger`：按固定间隔提前触发；
- `ContinuousProcessingTimeTrigger`：处理时间连续触发；
- 自定义 Trigger：`onElement/onEventTime/onProcessingTime/onMerge`，合并窗口还要实现 `canMerge()`。

```java
public class EndOfWindowTrigger extends Trigger<Order, TimeWindow> {
    @Override
    public TriggerResult onElement(Order e, long ts, TimeWindow w, TriggerContext ctx) {
        ctx.registerEventTimeTimer(w.getEnd() - 1);
        return TriggerResult.CONTINUE;
    }

    @Override
    public TriggerResult onEventTime(long time, TimeWindow w, TriggerContext ctx) {
        return time == w.getEnd() - 1
            ? TriggerResult.FIRE
            : TriggerResult.CONTINUE;
    }

    @Override
    public TriggerResult onProcessingTime(long time, TimeWindow w, TriggerContext ctx) {
        return TriggerResult.CONTINUE;
    }

    @Override
    public boolean canMerge() {
        return true;
    }

    @Override
    public void onMerge(TimeWindow merged, OnMergeContext ctx) {
        // 会话窗口合并后，必须为合并后的窗口重新注册定时器；
        // 如果 Trigger 自己维护了计数/上次触发时间，也要在这里合并。
        ctx.registerEventTimeTimer(merged.getEnd() - 1);
    }

    @Override
    public void clear(TimeWindow w, TriggerContext ctx) {
        ctx.deleteEventTimeTimer(w.getEnd() - 1);
    }
}
```

这个触发器在窗口结束前 1ms 触发，本质是“正常结束触发”，不是提前触发。真正的提前触发可以用 `ContinuousEventTimeTrigger`，或自己注册更早的 timer。合并窗口（会话窗口）会调用 `canMerge()`/`onMerge()`；如果 Trigger 有内部状态而 `onMerge` 没有合并它，合并后的窗口可能不触发或重复触发。

Trigger 的返回值：

- `CONTINUE`：不触发；
- `FIRE`：触发计算，保留窗口内容；
- `PURGE`：清空窗口内容；
- `FIRE_AND_PURGE`：触发后清空。

Evictor 在触发前/后移除部分元素：

- `CountEvictor`：保留最近 N 条；
- `TimeEvictor`：保留最近一段时间；
- 自定义 Evictor。

Evictor 通常与 `ProcessWindowFunction` 搭配，因为它需要看到元素列表；如果使用增量聚合，元素已经被折叠，无法再驱逐。反过来，增量聚合的内存开销远小于保留全量元素。

## 5.4 增量聚合 vs 全量窗口

```java
// 增量：每个窗口只存一个累加器
stream.keyBy(Order::userId)
      .window(TumblingEventTimeWindows.of(Duration.ofMinutes(5)))
      .aggregate(new SumAmount());

// 全量：窗口内保留所有元素，适合排序、TopN、需要明细的逻辑
stream.keyBy(Order::userId)
      .window(TumblingEventTimeWindows.of(Duration.ofMinutes(5)))
      .process(new RankInWindow());
```

`AggregateFunction` 有 `createAccumulator/add/getResult/merge`，会话窗口合并时依赖 `merge`。如果 `merge` 不满足结合律，会话窗口结果会不稳定。

```java
public class SumAmount implements AggregateFunction<Order, BigDecimal, BigDecimal> {
    public BigDecimal createAccumulator() { return BigDecimal.ZERO; }
    public BigDecimal add(Order o, BigDecimal acc) { return acc.add(o.amount()); }
    public BigDecimal getResult(BigDecimal acc) { return acc; }
    public BigDecimal merge(BigDecimal a, BigDecimal b) { return a.add(b); }
}
```

窗口状态大小可以粗略估算：

```text
state ≈ 活跃 key 数 × 每 key 活跃窗口数 × (累加器大小 或 元素大小 × 元素数)
```

滑动窗口的“每 key 活跃窗口数”约为 `size/slide`；会话窗口取决于活跃度和 gap。这个公式是容量规划的基础（第 26 章）。

### ReduceFunction 与 AggregateFunction 的区别

- `reduce` 的输入输出类型相同，适合“同类型滚动合并”；
- `aggregate` 有独立 accumulator，适合“输入类型 ≠ 结果类型”；
- `process(ProcessWindowFunction)` 能拿到窗口元信息和全部元素，但状态最大；
- `aggregate(AggregateFunction, ProcessWindowFunction)` 可以增量聚合 + 补充窗口元信息，是生产常用组合。

`ProcessWindowFunction` 的签名是 `process(KEY key, Context ctx, Iterable<IN> elements, Collector<OUT> out)`，`Context` 提供：

- `window()`：窗口对象；
- `currentProcessingTime()` / `currentWatermark()`；
- `windowState()`：**窗口级**状态，按 key + window namespace 隔离；
- `globalState()`：该 key 跨窗口共享的状态；
- `output(tag, value)`：侧输出。

它和 `KeyedProcessFunction` 的 `getRuntimeContext().getState(...)` 不是一回事：后者只按 key 隔离，不自动带窗口 namespace。在 `ProcessWindowFunction` 里做窗口级累加要用 `windowState()`，否则同一 key 的不同窗口会读到彼此的状态。`aggregate(AggregateFunction, ProcessWindowFunction)` 的重载则只把聚合结果交给后者，避免把全部元素放进内存。

## 5.5 允许迟到与侧输出

```java
OutputTag<Order> late = new OutputTag<Order>("late") {};

SingleOutputStreamOperator<Result> result = stream
    .keyBy(Order::userId)
    .window(TumblingEventTimeWindows.of(Duration.ofMinutes(5)))
    .allowedLateness(Duration.ofMinutes(1))
    .sideOutputLateData(late)
    .aggregate(new SumAmount());

DataStream<Order> lateStream = result.getSideOutput(late);
```

语义：

```text
window [10:00, 10:05)
wm 越过 10:05          → 首次触发（FIRE）
10:05 ~ 10:06 迟到数据  → 更新窗口，再次触发（可能多次）
wm 越过 10:06          → 清理窗口状态
超过 10:06 的记录       → 进入 side output
```

这会增加状态保留时间，也会让下游看到同一个窗口的多次结果。下游必须能处理更新（例如按窗口主键 upsert），不能简单 append。

```sql
-- 下游用 upsert 语义接收窗口更新
INSERT INTO sink
SELECT window_start, window_end, user_id, total_amount
FROM windowed_result;
-- sink 表声明 PRIMARY KEY (window_start, user_id)
```

## 5.6 会话窗口的特殊性

会话窗口没有固定边界。每条记录先形成一个 `[ts, ts+gap)` 的初始窗口，当新记录与已有窗口重叠时合并。合并意味着：

- 聚合函数必须支持 `merge`；
- 窗口状态可能被多次重写；
- 长时间不活跃的 key 会留下很多小窗口，直到 watermark 推进才清理；
- 会话 gap 越大，状态越大。

```text
事件:  1s   2s   8s   9s   20s
gap=5s
初始: [1,6) [2,7) [8,13) [9,14) [20,25)
合并: [1,7)      [8,14)        [20,25)
```

会话窗口适合“用户一次访问”“设备一次上线”这类天然有间隔的事件，不适合无限活跃的实体。对无限活跃实体，会话永远不会结束，状态会无限增长。

## 5.7 窗口的并行与 key

窗口必须作用在 `KeyedStream` 上（`windowAll` 除外）。同一个 key 的所有记录会路由到同一个 subtask，因此窗口状态是本地键控状态。

`windowAll` 的并行度是 1，所有数据汇到一个 subtask，容易成为瓶颈。除非数据量很小或确实需要全局 TopN，否则优先用 `keyBy + window`。

### 窗口状态与 key group

生产 Flink 的窗口状态按 key group 组织，扩缩容时以 key group 为单位迁移：

```text
并行度 4 → 8：
key group 0..127 重新分配到 8 个 subtask
每个 key 的窗口状态跟随 key group 迁移
```

这意味着：

- 扩缩容会移动状态，可能很慢；
- 窗口状态越大，rescale 越慢；
- 用 `uid` 稳定算子身份，否则 savepoint 找不到状态。

## 5.8 窗口与 watermark 的交互

- 没有 watermark，事件时间窗口不会触发；
- watermark 停滞，窗口不关闭，状态持续增长；
- 多输入算子取最小 watermark，最慢输入决定窗口进度；
- 窗口结束信号（作业结束）会冲刷剩余窗口，但生产无界流不会“自然结束”，不能依赖这一点；
- checkpoint 会保存窗口状态，恢复后窗口继续；
- watermark 本身不持久化；恢复后 source 重放并重新生成 watermark，窗口可能因重放再次触发，需关注下游幂等和尖峰。

## 5.9 SQL 中的窗口

Table SQL 用窗口 TVF 表达窗口：

```sql
-- 滚动窗口
SELECT user_id, window_start, window_end, SUM(amount)
FROM TABLE(TUMBLE(TABLE orders, DESCRIPTOR(order_time), INTERVAL '5' MINUTES))
GROUP BY user_id, window_start, window_end;

-- 滑动窗口
SELECT user_id, window_start, window_end, SUM(amount)
FROM TABLE(HOP(TABLE orders, DESCRIPTOR(order_time),
               INTERVAL '1' MINUTE, INTERVAL '5' MINUTES))
GROUP BY user_id, window_start, window_end;

-- 累积窗口（适合从小时累计到天）
SELECT user_id, window_start, window_end, SUM(amount)
FROM TABLE(CUMULATE(TABLE orders, DESCRIPTOR(order_time),
                    INTERVAL '1' HOUR, INTERVAL '1' DAY))
GROUP BY user_id, window_start, window_end;
```

SQL 窗口的语义与 DataStream 窗口一致，但优化器会做更多事情：增量聚合、mini-batch、状态 TTL、窗口去重。第 15 章会展开。

## 5.10 MiniFlink 的窗口实现

MiniFlink 支持滚动、滑动、会话、全局窗口，以及事件时间/处理时间、CountTrigger、PurgingTrigger、CountEvictor、TimeEvictor、allowedLateness 与侧输出。

```python
result = (
    env.from_collection(events, watermark_strategy=strategy)
    .key_by(lambda e: e["user"])
    .window(TumblingEventTimeWindows.of(Duration.of_minutes(5)))
    .allowed_lateness(Duration.of_minutes(1))
    .side_output_late_data(OutputTag("late"))
    .aggregate(SumAggregate("amount"))
)
```

内部 `WindowOperator` 维护 `{key: {window: bucket}}`，bucket 保存累加器或原始记录；watermark 推进时触发并清理；会话窗口通过 `merge` 合并 bucket。第 30 章会拆解代码。

MiniFlink 的窗口快照会保存累加器、记录和 watermark；恢复后窗口继续。由于是单进程实现，没有 Flink 的 window state namespace、RocksDB 列族和增量 checkpoint。

## 5.11 窗口设计检查清单

- [ ] 窗口是否必须重叠？能否用滚动窗口 + 预聚合替代滑动窗口？
- [ ] 每条记录会进入几个窗口？`size/slide` 是否在可接受范围？
- [ ] 用增量聚合还是全量窗口？全量窗口的 key 基数是否可控？
- [ ] `allowedLateness` 多长？下游能否处理重复/更新？
- [ ] 迟到数据是否有侧输出和补偿链路？
- [ ] 窗口状态 TTL 是否配置？与 checkpoint 大小是否匹配？
- [ ] watermark 是否会因空闲分区卡住？
- [ ] 扩缩容时窗口状态迁移是否演练过？

## 本章小结

窗口 = 分配器 + 触发器 + 计算函数 + 状态策略。选型时先问“窗口是否重叠、是否需要明细、允许迟到多久”，再决定增量聚合还是全量处理。窗口状态是流作业最容易膨胀的部分，必须和 watermark、TTL、checkpoint 一起设计。

下一章进入更底层的 ProcessFunction：当窗口和聚合无法表达业务时，如何用状态、定时器和侧输出手工构造精确的流式逻辑。
