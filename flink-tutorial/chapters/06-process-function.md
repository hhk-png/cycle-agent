# 第6章 ProcessFunction、定时器与侧输出

## 6.1 为什么需要 ProcessFunction

窗口和聚合封装了常见模式，但真实业务经常需要：

- 按事件时间设置“超时未支付”定时器；
- 访问键控状态，维护会话、去重、规则；
- 把异常数据分流到侧输出；
- 同时处理两条不同结构的流（connect/co-process）；
- 精确控制输出时间戳和 watermark；
- 根据外部规则动态改变处理逻辑。

这些都需要 `ProcessFunction`。它是 Flink 最底层的用户算子接口，窗口、CEP、join 等都可以用它实现。可以把 ProcessFunction 理解为“有状态、能访问时间、能分流的事件处理函数”。

## 6.2 五种上下文

| 类型 | 输入 | 能力 |
|---|---|---|
| `ProcessFunction` | 单流 | 时间戳、watermark、侧输出 |
| `KeyedProcessFunction` | keyBy 之后 | 额外拥有键控状态、按 key 的计时器 |
| `CoProcessFunction` | 两条流 | 两个处理方法；键控版本 `KeyedCoProcessFunction` 可共享键控状态 |
| `BroadcastProcessFunction` | 键控流 + 广播流 | 广播状态，规则动态更新 |
| `KeyedBroadcastProcessFunction` | 键控流 + 广播流 | 额外拥有键控状态和定时器 |

`KeyedProcessFunction` 的 `processElement` 收到当前记录的 key、timestamp；`onTimer` 在 watermark 推进或处理时间到达时触发。

```java
public abstract class KeyedProcessFunction<K, I, O> extends AbstractRichFunction {
    public abstract void processElement(I value, Context ctx, Collector<O> out);
    public void onTimer(long timestamp, OnTimerContext ctx, Collector<O> out) {}
}
```

`Context` 提供：

- `timestamp()`：当前记录的事件时间；
- `timerService()`：注册/删除事件时间和处理时间定时器；
- `output(tag, value)`：侧输出；
- `getCurrentKey()`：当前 key（Keyed 版本）。

## 6.3 事件时间定时器

```java
public class PaymentTimeout extends KeyedProcessFunction<String, Payment, Alert> {
    private ValueState<Long> deadline;

    @Override
    public void open(OpenContext ctx) {
        deadline = getRuntimeContext().getState(
            new ValueStateDescriptor<>("deadline", Types.LONG));
    }

    @Override
    public void processElement(Payment payment, Context ctx, Collector<Alert> out) {
        if (payment.isPaid()) {
            Long timer = deadline.value();
            if (timer != null) {
                ctx.timerService().deleteEventTimeTimer(timer);
                deadline.clear();
            }
            return;
        }
        long fireAt = payment.eventTime() + 15 * 60_000;
        deadline.update(fireAt);
        ctx.timerService().registerEventTimeTimer(fireAt);
    }

    @Override
    public void onTimer(long timestamp, OnTimerContext ctx, Collector<Alert> out) {
        Long current = deadline.value();
        if (current != null && current == timestamp) {
            out.collect(new Alert(ctx.getCurrentKey(), "payment-timeout"));
            deadline.clear();
        }
    }
}
```

关键点：

- 计时器按 **key + timestamp** 注册，同一个 key 同一时间重复注册只会触发一次；
- 计时器存在状态后端中，会随 checkpoint 持久化；
- 删除计时器要精确匹配 timestamp；
- 在 `onTimer` 里要再次校验状态，避免旧计时器误触发；
- 事件时间计时器只在 watermark 推进时触发，watermark 不前进就永远不会触发；
- 定时器的时间戳是“逻辑时间”，不一定等于真实时钟。

### 定时器的状态布局

生产 Flink 中，事件时间定时器按 key + timestamp 存在 state backend 的 timer state 里。RocksDB 后端会把定时器也写到列族，checkpoint 时一起快照。堆内后端则用优先队列。

这带来两个工程后果：

1. **定时器数量就是状态量**：每个未触发定时器都占内存/磁盘；
2. **定时器太多会拖慢 checkpoint**：RocksDB 的 timer state 可能很大。

优化手段：

- 合并定时器：同一 key 只保留“最早/最晚”的一个，触发时再扫描状态；
- 用窗口代替逐事件定时器；
- 给状态设 TTL；
- 用 `deleteEventTimeTimer` 及时清理。

## 6.4 处理时间定时器

`registerProcessingTimeTimer` 使用算子所在机器的时钟。它适合“从现在起 30 秒后检查”这类不要求可重放的逻辑。处理时间定时器也会随 checkpoint 恢复；若恢复时定时器的时间戳已经过去，它会**立即触发**，而不是重新排到未来。故障期间积压的定时器可能集中爆发，业务副作用也可能重复，因此不能用于强一致业务。

```java
long now = ctx.timerService().currentProcessingTime();
ctx.timerService().registerProcessingTimeTimer(now + 30_000);
```

语义差异：

- 事件时间定时器：由 watermark 驱动，可重放，适合业务超时；
- 处理时间定时器：由系统时钟驱动，不保证重放，适合缓存刷新、心跳、清理；
- 恢复后，错过触发时刻的处理时间定时器会立即补发，可能造成瞬时压力和重复副作用。

## 6.5 侧输出

侧输出（Side Output）是从主流分叉出的带标签数据流：

```java
OutputTag<String> invalid = new OutputTag<String>("invalid") {};

SingleOutputStreamOperator<Order> valid = stream.process(new Validate());
DataStream<String> errors = valid.getSideOutput(invalid);
```

```java
public class Validate extends ProcessFunction<RawOrder, Order> {
    @Override
    public void processElement(RawOrder raw, Context ctx, Collector<Order> out) {
        try {
            out.collect(parse(raw));
        } catch (Exception e) {
            ctx.output(INVALID_TAG, raw.toString() + ":" + e.getMessage());
        }
    }
}
```

适合：

- 脏数据/解析失败；
- 迟到数据；
- 需要单独告警的异常；
- 冷数据分流，避免污染主链路。

```java
// 侧输出可以有自己的 sink、并行度和处理逻辑
errors
    .keyBy(e -> e.split(":")[0])
    .process(new ErrorAggregator())
    .sinkTo(deadLetterSink);
```

侧输出是**独立类型**的流：`OutputTag<T>` 决定它的元素类型和序列化器，与主输出无关（上面的例子就是 `Order` 主流里分出 `String` 错误流）。它一旦被下游消费、跨 task 传输，仍然会序列化并占用网络缓冲。标签必须是静态常量，不能在运行时动态创建。侧输出不是“免费”的：标签太多会让作业图、网络和下游资源都变复杂。

## 6.6 CoProcessFunction 与双流逻辑

```java
public class Enrich extends KeyedCoProcessFunction<String, Order, User, Enriched> {
    private ValueState<User> user;

    @Override
    public void open(OpenContext ctx) {
        user = getRuntimeContext().getState(
            new ValueStateDescriptor<>("user", User.class));
    }

    @Override
    public void processElement1(Order order, Context ctx, Collector<Enriched> out) {
        User u = user.value();
        if (u != null) out.collect(new Enriched(order, u));
        else ctx.output(MISSING_USER, order);
    }

    @Override
    public void processElement2(User value, Context ctx, Collector<Enriched> out) {
        user.update(value);
    }
}
```

这实际上是一个“手写 join”。两条流必须按同一个 key 分区，否则同一个 key 的 order 和 user 可能落到不同 subtask。`connect` 不会自动帮你 keyBy；要显式处理。

```java
orders.keyBy(Order::userId)
      .connect(users.keyBy(User::userId))
      .process(new Enrich());
```

`KeyedCoProcessFunction` 的状态是**两个输入共享**的键控状态；普通 `CoProcessFunction` 没有键控状态，只能做无状态转换。要小心：

- 两条流的 watermark 取最小，慢流会阻塞快流的定时器；
- 一条流没有数据时，另一条流的 join 结果可能永远不完整；
- 需要定义“等待多久后输出未匹配结果”的超时逻辑。

## 6.7 BroadcastProcessFunction 与动态规则

广播状态用于“一条小流（规则/配置）广播到所有 subtask，另一条大流按 key 处理”：

```java
public class RuleMatcher extends BroadcastProcessFunction<Event, Rule, Alert> {
    private final MapStateDescriptor<String, Rule> rules =
        new MapStateDescriptor<>("rules", Types.STRING, TypeInformation.of(Rule.class));

    @Override
    public void processElement(Event event, ReadOnlyContext ctx, Collector<Alert> out) {
        for (Map.Entry<String, Rule> e : ctx.getBroadcastState(rules).immutableEntries()) {
            if (e.getValue().matches(event)) {
                out.collect(new Alert(event, e.getValue()));
            }
        }
    }

    @Override
    public void processBroadcastElement(Rule rule, Context ctx, Collector<Alert> out) {
        ctx.getBroadcastState(rules).put(rule.id(), rule);
    }
}
```

注意：

- 广播流在每个 subtask 都有一份完整副本，规则不能太大；
- 广播状态没有 key，更新是全量可见的；
- 两条流的到达顺序不保证，启动时要用 bootstrap 或“规则版本”处理；
- 广播状态会随 checkpoint 保存，恢复后规则仍在。

第 20 章会把广播状态与 CEP 放在一起讲规则引擎。

## 6.8 定时器与状态的常见坑

1. **计时器风暴**：每个事件都注册一个计时器，状态和堆外内存暴涨。能合并的计时器要合并，或用窗口代替。
2. **忘记删除**：业务已经完成，旧计时器仍然在 checkpoint 中，恢复后误触发。
3. **在 onTimer 中访问错误 key**：`ctx.getCurrentKey()` 才是计时器所属 key，不要用闭包里的变量。
4. **处理时间不可重放**：故障恢复后结果可能不同，不能用于账务类逻辑。
5. **状态无 TTL**：会话类状态长期不清理，最终 OOM 或 checkpoint 超时。
6. **侧输出标签重复**：同名标签在不同算子中可能混淆，建议集中定义常量。
7. **在 processElement 里做阻塞 IO**：一个 subtask 的所有记录都会被拖住；用 Async I/O。
8. **用 `System.currentTimeMillis()` 代替 `ctx.timestamp()`**：事件时间语义丢失。

## 6.9 性能与可观测性

- 定时器数量是关键指标，可在 Web UI 或 metrics 中观察；
- 大量短生命周期计时器适合 RocksDB，堆内状态容易 GC 压力大；
- `process` 中避免同步阻塞调用（数据库、HTTP），会拖垮整个 subtask；
- 外部查询用 Async I/O（第 12 章）；
- 给算子命名并记录关键计数（超时数、侧输出数、状态命中率）。

```java
private transient Counter timeouts;
private transient Counter invalid;

@Override
public void open(OpenContext ctx) {
    timeouts = getRuntimeContext().getMetricGroup().counter("timeouts");
    invalid = getRuntimeContext().getMetricGroup().counter("invalid");
}
```

## 6.10 测试 ProcessFunction

ProcessFunction 的测试要用 `KeyedOneInputStreamOperatorTestHarness`，而不是只测纯函数：

```java
KeyedProcessOperator<String, Payment, Alert> operator =
    new KeyedProcessOperator<>(new PaymentTimeout());

try (KeyedOneInputStreamOperatorTestHarness<String, Payment, Alert> harness =
         new KeyedOneInputStreamOperatorTestHarness<>(operator, Payment::userId, Types.STRING)) {
    harness.open();
    harness.processElement(new StreamRecord<>(payment, payment.eventTime()));
    harness.processWatermark(payment.eventTime() + 15 * 60_000);
    assertThat(harness.extractOutputValues()).hasSize(1);
}
```

测试要覆盖：

- 正常触发；
- 提前支付后取消定时器；
- watermark 不推进时不触发；
- 恢复后定时器仍然有效；
- 侧输出内容。

## 6.11 MiniFlink 的实现

MiniFlink 的 `KeyedProcessFunction`：

```python
class Timeout(KeyedProcessFunction):
    def process_element(self, value, ctx, out):
        ctx.timer_service.register_event_time_timer(
            ctx.timestamp + 60_000, ctx.current_key)
        out.collect(value)

    def on_timer(self, timestamp, ctx, out):
        out.collect(("timeout", ctx.current_key, timestamp))
```

`ProcessOperator` 在每条记录前设置 `current_key`，watermark 到来时从 `TimerService` 弹出所有到期计时器，逐个恢复 key 后调用 `on_timer`。`TimerService` 的快照会进入 checkpoint。

侧输出通过 `ctx.output(tag, value)` 写入运行时上下文；`ProcessOperator` 处理完后，`RuntimeTask` 把侧输出值按 `OutputTag` 路由到对应的侧输出边。没有下游的侧输出会汇总到 `JobExecution.side_outputs`。

MiniFlink 的定时器是单进程字典，不支持 RocksDB 的 timer state、异步快照和计时器合并；语义足够教学，但不能直接承载生产量级。

## 本章小结

ProcessFunction 把“状态、时间、分流”三件事交到用户手里，能力最强，也最容易写出状态泄漏和计时器风暴。使用它时先问：能不能用窗口/聚合表达？是否真的需要逐条控制？定时器是否能合并？外部调用是否应该异步？

下一章系统讨论状态管理：键控状态、算子状态、广播状态，以及 StateBackend 如何决定性能、容量和恢复行为。
