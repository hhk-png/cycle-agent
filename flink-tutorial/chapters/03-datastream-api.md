# 第3章 DataStream API 与类型系统

## 3.1 DataStream 是“不可变描述”，不是数据容器

在 Flink 中，`DataStream<T>` 表示一条逻辑数据流。调用 `map` 不会立刻处理数据，而是向执行图追加一个转换；只有 `env.execute()` 才触发作业提交。因此可以把 DataStream 理解成**带类型的转换 DAG 句柄**。

```java
StreamExecutionEnvironment env = StreamExecutionEnvironment.getExecutionEnvironment();
env.setParallelism(4);
env.enableCheckpointing(10_000);

DataStream<String> lines = env.fromSource(
    kafkaSource, WatermarkStrategy.noWatermarks(), "lines");

DataStream<Order> orders = lines
    .flatMap(new ParseOrder())
    .filter(order -> order.amount() > 0)
    .name("parse-and-filter");

orders
    .keyBy(Order::userId)
    .window(TumblingEventTimeWindows.of(Duration.ofMinutes(5)))
    .aggregate(new SumAmount())
    .uid("window-sum-amount")
    .name("window-sum-amount")
    .sinkTo(kafkaSink);

env.execute("order-risk");
```

这段代码体现了 DataStream API 的三个特征：

1. **惰性**：转换只建图，不读数据；
2. **类型化**：每个 `DataStream<T>` 携带类型信息，供序列化和状态使用；
3. **可命名**：`name` 用于 UI/日志，`uid` 用于 savepoint 兼容。

### 为什么 uid 是生产必选项

Flink 在生成 JobGraph 时会给每个算子分配一个自动 UID，通常基于拓扑位置和哈希。只要拓扑变化（插入一个 map、调整顺序），自动 UID 就会变，savepoint 里的状态找不到对应算子，恢复失败或状态错位。

```java
stream
    .keyBy(Order::userId)
    .process(new RiskDetector())
    .uid("risk-detector-v1")   // 稳定、唯一、永不复用
    .name("risk-detector");
```

规则：

- 所有**有状态**算子必须显式 `uid`；
- uid 一旦上线不要改，逻辑升级用新版本号或状态迁移；
- 不要用随机数、时间戳或自增计数生成 uid；
- 同一个作业内 uid 不能重复。

## 3.2 执行环境与作业配置

```java
StreamExecutionEnvironment env = StreamExecutionEnvironment.getExecutionEnvironment();

env.setParallelism(8);
env.enableCheckpointing(30_000, CheckpointingMode.EXACTLY_ONCE);
env.getCheckpointConfig().setMinPauseBetweenCheckpoints(10_000);
env.getCheckpointConfig().setCheckpointTimeout(10 * 60_000);
env.getCheckpointConfig().setTolerableCheckpointFailureNumber(3);
env.setRestartStrategy(RestartStrategies.exponentialDelayRestart(
    Duration.ofSeconds(1), Duration.ofMinutes(5), 2.0));
```

执行环境还负责：

- 注册 source/sink；
- 设置执行模式（streaming/batch）；
- 配置序列化器、对象复用、自动 watermark 间隔；
- 生成 execution plan（`env.getExecutionPlan()`）；
- 提交作业（`env.execute()`）。

在测试中，通常用 `StreamExecutionEnvironment.createLocalEnvironment()` 或 `MiniCluster`，避免连接真实集群。

## 3.3 常用转换

| 转换 | 语义 | 是否产生 shuffle | 备注 |
|---|---|---|---|
| `map` | 一对一 | 否 | 保持并行度 |
| `filter` | 丢弃部分记录 | 否 | 同 map |
| `flatMap` | 一条变零到多条 | 否 | 适合拆分/过滤 |
| `keyBy` | 按 key 分区 | 是（hash） | 得到 KeyedStream |
| `reduce` | 键控滚动聚合 | 依赖上游 | 只保留结果 |
| `aggregate` | 键控增量聚合 | 依赖上游 | 有 accumulator |
| `window` | 把记录分配到窗口 | 否 | keyBy 后使用 |
| `union` | 合并同类型流 | 可能 | 多输入 |
| `connect` | 合并两条不同类型流 | 可能 | 保留两个输入 |
| `rebalance` | 轮询分发 | 是 | 缓解倾斜 |
| `rescale` | 按比例本地轮询 | 是 | 减少网络 |
| `broadcast` | 广播到所有下游 | 是 | 规则/维表 |
| `process` | 底层算子 | 否 | 状态/时间/侧输出 |

### keyBy 之后发生了什么

`keyBy` 不是把数据“分组存起来”，而是给每条记录计算 key，并在后续算子的输入边上做 hash 分区：

```java
KeyedStream<Order, String> keyed = orders.keyBy(Order::userId);
```

运行时行为：

- 同一条记录的 key 决定它去哪个 subtask；
- 同一个 key 永远落在同一个 subtask（在同一并行度下）；
- 该 subtask 的 keyed state 只保存它负责的 key；
- 窗口、定时器、reduce 都按 key 隔离。

三个常见错误：

1. **key 为 null**：hash 分区无法路由，通常直接报错；要在 source 端过滤或补默认值。
2. **key 不稳定**：对象的 `hashCode()` 依赖可变字段或随机值，重放时分区变化，状态错位。
3. **key 太大**：把整个 POJO 当 key，哈希和序列化成本高；只取必要字段。

```java
// 推荐：稳定、窄 key
orders.keyBy(order -> order.getUserId());

// 避免：依赖可变字段或随机值
orders.keyBy(order -> order.getPayload().hashCode());
```

## 3.4 富函数与 ProcessFunction

普通 `map/filter` 无状态。需要 `open/close`、运行时上下文、状态或定时器时，用 `RichFunction` 或 `ProcessFunction`：

```java
public class Deduplicate extends KeyedProcessFunction<String, Event, Event> {
    private ValueState<Long> lastSeen;

    @Override
    public void open(OpenContext ctx) {
        lastSeen = getRuntimeContext().getState(
            new ValueStateDescriptor<>("last-seen", Types.LONG));
    }

    @Override
    public void processElement(Event event, Context ctx, Collector<Event> out) {
        Long previous = lastSeen.value();
        if (previous == null || event.ts() - previous > 60_000) {
            lastSeen.update(event.ts());
            out.collect(event);
        }
        ctx.timerService().registerEventTimeTimer(event.ts() + 60_000);
    }

    @Override
    public void onTimer(long timestamp, OnTimerContext ctx, Collector<Event> out) {
        // 可在这里清理状态或发出超时事件
    }
}
```

注意 2.x 中 `open(Configuration)` 已改为 `open(OpenContext)`。`ProcessFunction` 是 Flink 的“底层汇编”：窗口、CEP、join 等高层算子最终都能用它表达。第 6 章会展开。

### 侧输出与异步 I/O

侧输出（side output）用于把“异常/迟到/死信”分流，而不污染主流程：

```java
OutputTag<Order> lateTag = new OutputTag<Order>("late") {};
SingleOutputStreamOperator<Result> result = stream
    .keyBy(Order::userId)
    .window(TumblingEventTimeWindows.of(Duration.ofMinutes(5)))
    .allowedLateness(Duration.ofMinutes(1))
    .sideOutputLateData(lateTag)
    .aggregate(new SumAmount());

DataStream<Order> late = result.getSideOutput(lateTag);
```

异步 I/O 用于访问外部系统而不阻塞算子线程，第 12 章会详细讲。

## 3.5 Source 与 Sink 的现代写法

旧版 `addSource`、`SourceFunction`、`SinkFunction` V1 在 Flink 2.0 已移除。新 API 是 FLIP-27 的 `Source` 和 Sink V2：

```java
KafkaSource<Order> source = KafkaSource.<Order>builder()
    .setBootstrapServers("kafka:9092")
    .setTopics("orders")
    .setGroupId("flink-orders")
    .setStartingOffsets(OffsetsInitializer.committedOffsets(OffsetResetStrategy.EARLIEST))
    .setValueOnlyDeserializer(new OrderDeserializationSchema())
    .build();

env.fromSource(source, watermarkStrategy, "kafka-orders");
```

Sink 侧推荐 Sink V2 或连接器自带的 exactly-once sink：

```java
env.fromSource(source, watermarkStrategy, "orders")
   .sinkTo(KafkaSink.<Order>builder()
       .setBootstrapServers("kafka:9092")
       .setRecordSerializer(new OrderSerializationSchema("orders-sink"))
       .setDeliveryGuarantee(DeliveryGuarantee.EXACTLY_ONCE)
       .setTransactionalIdPrefix("orders-sink-")
       .build());
```

Source 的并行度、分区发现、watermark 生成和 offset 提交是四个不同的问题，后面第 10、11 章会分别讲。

### DataStream API v2

Flink 2.x 正在推进 DataStream API v2（伞形 FLIP-408），它不是经典 API 的“加参数版本”，而是一次重新划边界：

- 用户只通过 **ProcessFunction** 描述处理逻辑，不再暴露 mailbox、barrier 对齐、算子链等内部细节；
- 流类型显式区分 `GlobalStream`、`NonKeyedPartitionStream`、`KeyedPartitionStream`、`BroadcastStream`；
- 分区收敛为 `keyBy`、`shuffle`、`global`、`broadcast` 四种逻辑方式；
- 状态通过 `StateDeclaration` 声明，并带 `NONE`/`REDISTRIBUTABLE`/`IDENTICAL` 重分配模式；
- watermark 抽象为“广义 watermark”，处理时间定时器走独立的 `ProcessingTimeManager`。

一个最小的 V2 作业形状（示意，签名以所用版本文档为准）：

```java
ExecutionEnvironment env = ExecutionEnvironment.getExecutionEnvironment();

env.fromSource(source, WatermarkStrategy.noWatermarks(), "source")
   .process(new OneInputStreamProcessFunction<Integer, Integer>() {
       @Override
       public void processRecord(Integer value, Collector<Integer> out, Context ctx) {
           out.collect(value + 1);
       }
   })
   .global()
   .toSink(sink);

env.execute("v2-job");
```

注意：

- 环境类叫 `ExecutionEnvironment`，不是 `StreamExecutionEnvironment`；
- `processRecord(value, out, ctx)` 的参数顺序与 V1 不同；
- 高层 window/join 算子还不完整，窗口要用 ProcessFunction + 状态/定时器组合；
- 截至 2.3，V2 仍是实验 API，**不要重写生产作业**。

目前生产代码仍以经典 DataStream API 为主。新项目应优先使用 Source/Sink V2，避免依赖已移除的 V1 接口；V2 的详细设计与迁移策略见第 34 章。

## 3.6 类型系统与序列化

Flink 需要知道如何序列化记录，才能在网络传输、状态存储和 checkpoint 中使用。类型信息通常来自：

- 基本类型与常见集合；
- POJO（无参构造、字段可访问、标准 getter/setter）；
- Tuple；
- Scala case class（2.x 中 Scala API 已移除，历史代码需迁移）；
- 显式 `TypeInformation`；
- 兜底 Kryo。

### POJO 规则

一个类要被视为 POJO，通常需要：

- public 无参构造；
- 类本身 public；
- 字段有 public getter/setter，或字段本身 public；
- 字段类型可序列化；
- 不能是纯 final 字段（否则无法反序列化赋值）。

```java
public class Order {
    private String userId;
    private long amount;
    private long eventTime;

    public Order() {}
    public String getUserId() { return userId; }
    public void setUserId(String v) { this.userId = v; }
    // ... 其余 getter/setter
}
```

POJO 序列化器比 Kryo 更快、更稳定，也更容易做 schema 演进。生产状态对象优先用 POJO、Avro 或 Protobuf。

### 类型系统的五个坑

1. **Java lambda 的类型擦除**：`map(x -> ...)` 的返回类型可能推断不出来，复杂场景要加 `returns(Types.POJO(...))` 或使用匿名类。
2. **Kryo 不是稳定格式**：字段增删、类改名可能导致 savepoint 无法恢复；Flink 2.0 已把 Kryo 从 2.24 升级到 5.x（FLIP-317），这是 1.x→2.x 状态不兼容的重要原因，2.2 沿用该变化。关键状态尽量用 POJO/Avro/Protobuf，并管理 schema 演进。
3. **POJO 字段必须可访问**：全是 private 且无 getter/setter 会退回 Kryo，性能和兼容性都变差。
4. **泛型嵌套**：`DataStream<List<Order>>` 这类类型需要显式类型信息，否则运行期可能按 `GenericType` 处理。
5. **序列化器性能**：Kryo 通常比 Flink 专用序列化器慢，状态大时差异明显。

```java
DataStream<Order> orders = raw
    .map(new ParseOrder())
    .returns(TypeInformation.of(Order.class));

// 开发期检测 Kryo 兜底：遇到无法处理的类型直接失败
env.getConfig().disableGenericTypes();
```

### Schema 演进

状态里的对象会随版本变化。安全的演进方式：

- 只加字段，且新字段有默认值；
- 不改字段类型和顺序；
- 不改类名/包名；
- 用 Avro/Protobuf 的 schema 兼容规则；
- 大改动用 State Processor API 做离线迁移，或从源端重放。

第 9 章会展开状态 schema 演进和 savepoint 兼容性。

## 3.7 并行度与数据分布

并行度可以按作业、算子、执行环境设置：

```java
env.setParallelism(8);
stream.map(...).setParallelism(16);
```

数据分布由分区器决定：

- `forward`：上下游同并行度时一对一；
- `hash`：按 key 哈希，保证同 key 同 subtask；
- `rebalance`：轮询，缓解倾斜；
- `rescale`：本地轮询，减少网络；
- `broadcast`：复制到所有下游；
- `global`：全部发往下游第一个 subtask（慎用，易成瓶颈）。

`keyBy` 后同 key 一定在同一 subtask，这是键控状态正确性的基础。也正因如此，**热点 key 会把单个 subtask 打满**，加并行度也无效。需要盐化、预聚合或两阶段聚合（第 19、21 章）。

### 算子链与资源

```java
stream.map(...).startNewChain().filter(...); // 切断链
stream.slotSharingGroup("heavy");             // 独立 slot 组
stream.setParallelism(16);
```

链化减少序列化，但会放大故障影响。生产上建议：

- 轻量转换链在一起；
- 有状态、重计算、易失败的算子单独成链；
- 用 `name`/`uid` 让 UI 和 savepoint 可读。

## 3.8 常见反模式

- **在 lambda 里捕获不可序列化的外部对象**：提交时报 `NotSerializableException`，或运行时行为不一致。
- **用 `static` 可变字段传配置**：subtask 之间共享 JVM 状态，恢复/扩容后行为漂移。
- **把所有逻辑塞进一个 `process`**：无法链化、状态难管理、测试困难。
- **不设置 uid**：第一次改拓扑就恢复失败。
- **依赖默认并行度**：换环境后 slot 不够或吞吐骤降。
- **用 `print()` 做生产 sink**：无 exactly-once、无背压处理、无分区控制。
- **在 `open()` 里做重 IO**：每个 subtask 启动都打一次数据库，容易拖垮启动。

## 3.9 MiniFlink 的 DataStream 子集

MiniFlink 提供：

```python
(
    env.from_collection(records, watermark_strategy=strategy)
    .map(lambda x: x)
    .filter(lambda x: True)
    .flat_map(lambda x: [x])
    .key_by(lambda x: x.key)
    .window(TumblingEventTimeWindows.of(Duration.of_seconds(10)))
    .aggregate(SumAggregate("amount"))
    .process(SomeKeyedProcessFunction())
    .sink_to(print)
)
```

它没有 Java 的泛型类型系统，Python 对象直接传递；没有 Kryo/POJO 兼容性问题，但也没有跨语言 schema 管理。它的 `KeyByOperator` 会把 key 写入 `Record.key`，运行时用稳定哈希路由；这对应 Flink 的 `keyBy` + hash 分区。

MiniFlink 的 `DataStream` 每次转换都会创建一个新的 `Node`，`execute_and_collect()` 再从终止节点反向构图。这与 Flink 的“描述与执行分离”一致，只是没有优化器和链化。

## 3.10 生产建议

- 给所有有状态算子设置 `uid`，并纳入代码评审；
- 显式声明复杂类型，开发期开启 `disableGenericTypes()`；
- 不要在 lambda 里捕获不可序列化的外部对象；
- 算子名称包含业务含义和版本，便于 UI 定位；
- 控制单个算子的职责，避免一个 `process` 里塞进所有逻辑；
- 对 key 的分布做采样，提前发现热点；
- 用 Source/Sink V2，避免依赖 2.0 已移除的 API；
- 单元测试使用 MiniCluster/TestHarness，而不是只测纯函数。

## 本章小结

DataStream API 的本质是**带类型的转换图**。类型系统决定序列化与状态兼容性，`keyBy` 决定状态分区，`uid` 决定 savepoint 能否恢复，`name` 决定可观测性。

下一章进入流处理最核心的概念：时间与 watermark。只有理解了“什么时候认为数据到齐”，窗口、join、状态清理和 exactly-once 才有意义。
