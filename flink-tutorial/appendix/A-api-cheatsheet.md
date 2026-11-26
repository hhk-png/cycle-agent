# 附录 A Flink 与 MiniFlink API 速查

> 版本基线：Flink 2.3 / Java 17。1.x 的 `SourceFunction`、`SinkFunction` V1、DataSet、Scala API 在 2.0 已移除；旧代码迁移时先看第 2、9 章和官方升级指南。

## A.1 环境与执行

```java
StreamExecutionEnvironment env = StreamExecutionEnvironment.getExecutionEnvironment();
env.setParallelism(8);
env.enableCheckpointing(30_000, CheckpointingMode.EXACTLY_ONCE);
env.getCheckpointConfig().setMinPauseBetweenCheckpoints(10_000);
env.getCheckpointConfig().setCheckpointTimeout(10 * 60_000);
env.getCheckpointConfig().setTolerableCheckpointFailureNumber(3);
env.setRuntimeMode(RuntimeExecutionMode.STREAMING);
env.execute("job-name");
```

```python
from miniflink import StreamExecutionEnvironment

env = StreamExecutionEnvironment(parallelism=8)
env.enable_checkpointing(30_000, micro_batch_records=50)
job = env.execute("job-name")
print(job.metrics())
```

## A.2 Source / Sink

| 能力 | Flink Java | MiniFlink |
|---|---|---|
| 集合 | `env.fromData(collection)` | `env.from_collection(data)` |
| 文件 | `FileSource.forRecordStreamFormat(...)` | `env.read_text_file(path)` |
| Kafka | `KafkaSource` | `env.from_kafka(bus, topic)` |
| 自定义 | `Source` API | `env.add_source(factory)` |
| Sink | `sinkTo(Sink)` | `sink_to(fn)` / `print()` / `write_as_text()` |

Flink 2.0 移除了 `StreamExecutionEnvironment.readTextFile`；文件源请用 `FileSource.forRecordStreamFormat(new TextLineInputFormat(), path)` 配合 `env.fromSource(...)`。

```java
KafkaSource<Order> source = KafkaSource.<Order>builder()
    .setBootstrapServers("kafka:9092")
    .setTopics("orders")
    .setGroupId("risk")
    .setStartingOffsets(OffsetsInitializer.committedOffsets())
    .setValueOnlyDeserializer(new OrderDeserializer())
    .build();

env.fromSource(source, watermarkStrategy, "orders");
```

```java
KafkaSink<Order> sink = KafkaSink.<Order>builder()
    .setBootstrapServers("kafka:9092")
    .setRecordSerializer(new OrderSerializer("orders-out"))
    .setDeliveryGuarantee(DeliveryGuarantee.EXACTLY_ONCE)
    .setTransactionalIdPrefix("risk-")
    .build();
```

## A.3 转换

| 语义 | Flink | MiniFlink |
|---|---|---|
| 映射 | `map` | `map` |
| 过滤 | `filter` | `filter` |
| 展开 | `flatMap` | `flat_map` |
| 键控 | `keyBy` | `key_by` |
| 归约 | `reduce` | `reduce` |
| 聚合 | `aggregate` | `aggregate` |
| 并流 | `union` | `union` |
| 连接 | `connect` | `connect` |
| 侧输出 | `getSideOutput` | `get_side_output` |
| 异步 | `AsyncDataStream` | `async_map` |
| 分区 | `rebalance/rescale/broadcast` | 同名子集 |

```java
stream
    .keyBy(Order::userId)
    .window(TumblingEventTimeWindows.of(Duration.ofMinutes(5)))
    .aggregate(new SumAmount())
    .uid("window-sum")
    .name("window-sum");
```

## A.4 时间与窗口

```java
WatermarkStrategy<Event> strategy = WatermarkStrategy
    .<Event>forBoundedOutOfOrderness(Duration.ofSeconds(5))
    .withTimestampAssigner((e, ts) -> e.eventTime())
    .withIdleness(Duration.ofSeconds(30));

stream.keyBy(Event::key)
      .window(TumblingEventTimeWindows.of(Duration.ofMinutes(1)))
      .allowedLateness(Duration.ofSeconds(10))
      .sideOutputLateData(lateTag)
      .aggregate(new MyAggregate());
```

```python
strategy = WatermarkStrategy.for_bounded_out_of_orderness(
    Duration.of_seconds(5), timestamp_assigner=lambda e: e["ts"]
)
(
    stream.key_by(lambda e: e["key"])
    .window(TumblingEventTimeWindows.of(Duration.of_minutes(1)))
    .allowed_lateness(Duration.of_seconds(10))
    .side_output_late_data(OutputTag("late"))
    .aggregate(SumAggregate("amount"))
)
```

窗口类型：`TumblingEventTimeWindows`、`SlidingEventTimeWindows`、`EventTimeSessionWindows`、`GlobalWindows`；处理时间版本用 `TumblingProcessingTimeWindows`、`ProcessingTimeSessionWindows` 等。动态 gap 用 `DynamicEventTimeSessionWindows` / `DynamicProcessingTimeSessionWindows`。

```java
// Trigger / Evictor
.window(...)
.trigger(ContinuousEventTimeTrigger.of(Duration.ofSeconds(10)))
.evictor(CountEvictor.of(1000))
```

## A.5 状态与定时器

```java
ValueStateDescriptor<Long> desc = new ValueStateDescriptor<>("count", Types.LONG);
ValueState<Long> state = getRuntimeContext().getState(desc);

StateTtlConfig ttl = StateTtlConfig.newBuilder(Duration.ofHours(24))
    .setUpdateType(StateTtlConfig.UpdateType.OnCreateAndWrite)
    .setStateVisibility(StateTtlConfig.StateVisibility.NeverReturnExpired)
    .build();
desc.enableTimeToLive(ttl);

ctx.timerService().registerEventTimeTimer(ts);
ctx.timerService().deleteEventTimeTimer(ts);
```

```python
state = ctx.get_state(ValueStateDescriptor("count", lambda: 0))
ctx.timer_service.register_event_time_timer(ts, ctx.current_key)
```

状态类型：Value/List/Map/Reducing/Aggregating；MiniFlink 还提供 `StateTtlConfig`。

## A.6 ProcessFunction

```java
public class Timeout extends KeyedProcessFunction<String, Payment, Alert> {
    private ValueState<Long> deadline;

    @Override
    public void open(OpenContext ctx) {
        deadline = getRuntimeContext().getState(
            new ValueStateDescriptor<>("deadline", Types.LONG));
    }

    @Override
    public void processElement(Payment p, Context ctx, Collector<Alert> out) {
        long fireAt = p.eventTime() + 15 * 60_000;
        deadline.update(fireAt);
        ctx.timerService().registerEventTimeTimer(fireAt);
    }

    @Override
    public void onTimer(long ts, OnTimerContext ctx, Collector<Alert> out) {
        if (deadline.value() != null && deadline.value() == ts) {
            out.collect(new Alert(ctx.getCurrentKey(), "timeout"));
            deadline.clear();
        }
    }
}
```

## A.7 检查点与恢复

```bash
flink savepoint <job-id> s3://bucket/savepoints
flink stop --savepointPath s3://bucket/savepoints <job-id>
flink run -s s3://bucket/savepoints/savepoint-xxx job.jar
```

```yaml
execution.checkpointing.interval: 30s
execution.checkpointing.timeout: 10min
execution.checkpointing.min-pause: 10s
execution.checkpointing.mode: EXACTLY_ONCE
state.checkpoints.dir: s3://bucket/checkpoints
state.savepoints.dir: s3://bucket/savepoints
state.backend.incremental: true
```

```python
env.enable_checkpointing(10_000, storage=FileCheckpointStorage("build/cp"))
execution = env.create_execution()
execution.run(max_rounds=1)
checkpoint = execution.checkpoint()
restored = env.create_execution().restore(checkpoint)
restored.run()
```

## A.8 Table / SQL

```java
EnvironmentSettings settings = EnvironmentSettings.newInstance()
    .inStreamingMode().build();
TableEnvironment tEnv = TableEnvironment.create(settings);
tEnv.executeSql("CREATE TABLE ... WITH (...)");
Table result = tEnv.sqlQuery("SELECT ...");
tEnv.toChangelogStream(result).print();
```

```java
import static org.apache.flink.table.api.Expressions.*;

Table orders = tEnv.from("orders");
Table result = orders
    .filter($("amount").isGreater(0))
    .window(Tumble.over(lit(5).minutes()).on($("order_time")).as("w"))
    .groupBy($("user_id"), $("w"))
    .select($("user_id"), $("w").start(), $("w").end(),
            $("amount").sum().as("total"));
result.execute().print();

DataStream<Row> changelog = tEnv.toChangelogStream(result);
DataStream<Row> rows = tEnv.toDataStream(result);
```

```sql
-- 窗口 TVF
SELECT window_start, window_end, user_id, SUM(amount)
FROM TABLE(TUMBLE(TABLE orders, DESCRIPTOR(order_time), INTERVAL '5' MINUTES))
GROUP BY window_start, window_end, user_id;

-- 去重
SELECT * FROM (
  SELECT *, ROW_NUMBER() OVER (PARTITION BY id ORDER BY ts DESC) AS rn
  FROM orders
) WHERE rn = 1;

-- 物化表（1.20+）
CREATE MATERIALIZED TABLE user_revenue
  FRESHNESS = INTERVAL '1' MINUTE
  AS SELECT user_id, SUM(amount) AS total FROM orders GROUP BY user_id;

-- 模型推理（2.1+，TVF 形态）
SELECT order_id, score
FROM ML_PREDICT(
  TABLE orders,
  MODEL risk_model,
  DESCRIPTOR(features),
  MAP['async', 'true']
);
```

```python
from miniflink import TableEnvironment

t = TableEnvironment()
t.from_elements([{"user": "a", "amount": 10}], name="orders")
print(t.sql("SELECT user, SUM(amount) AS total FROM orders GROUP BY user").execute())

# CTAS 教学子集：结果一次性物化并注册为新表
t.execute_sql("CREATE TABLE totals AS SELECT user, SUM(amount) AS total FROM orders GROUP BY user")
print(t.sql("SELECT * FROM totals").execute())
```

MiniFlink SQL 是 SQLite 子集，不支持 changelog/窗口 TVF 完整语义；CTAS/VIEW 只做一次性物化，没有 Catalog、外部存储和持续写入。

## A.9 常用配置

| 配置 | 作用 |
|---|---|
| `parallelism.default` | 默认并行度 |
| `taskmanager.numberOfTaskSlots` | 每 TM slot 数 |
| `state.backend.type` | hashmap/rocksdb/forst |
| `state.checkpoints.dir` | checkpoint 存储 |
| `execution.checkpointing.interval` | checkpoint 间隔 |
| `execution.checkpointing.mode` | exactly-once/at-least-once |
| `execution.checkpointing.unaligned.enabled` | 非对齐 checkpoint |
| `table.exec.state.ttl` | SQL 状态 TTL |
| `execution.buffer-timeout` | 网络缓冲延迟 |
| `pipeline.watermark-alignment.allow-unaligned-source-splits` | watermark alignment |

## A.10 常用命令

```bash
flink run -c com.acme.Job job.jar
flink run --target kubernetes-application ...   # 2.x；1.x 为 run-application -t ...
flink list
flink cancel <job-id>
flink savepoint <job-id> <path>
flink run -s <path> job.jar
```

MiniFlink：

```bash
python -m miniflink.cli run examples/01_word_count.py
python -m miniflink.cli sql --table orders=orders.csv --query "SELECT ..."
python -m pytest -q
python check_docs.py
```

## A.11 2.x 新特性速查

| 特性 | 版本 | 入口/形态 | 备注 |
|---|---|---|---|
| DataStream API V2 | 2.0 实验 | `org.apache.flink.datastream.api.*` | ProcessFunction/StateDeclaration/广义 watermark |
| ForSt 分离式状态 | 2.0 实验 | `state.backend.type: forst` | 远程 DFS 主存储，本地 cache |
| 物化表 | 1.20+ | `CREATE MATERIALIZED TABLE ... FRESHNESS ...` | CONTINUOUS/FULL 刷新 |
| CTAS / RTAS | 1.16+ / 1.18+ | `CREATE TABLE ... AS SELECT` / `CREATE OR REPLACE TABLE ... AS SELECT` | 默认非原子；schema 可推导，2.x 起可显式声明 |
| PTF | 2.1 | `ProcessTableFunction` + SQL 调用 | 状态/定时器/多表输入输出 |
| `ML_PREDICT` | 2.1 | `ML_PREDICT(TABLE ..., MODEL ..., DESCRIPTOR(...))` | 模型推理，注意超时/限流 |
| `VECTOR_SEARCH` | 2.2 | `LATERAL TABLE(VECTOR_SEARCH(SEARCH_TABLE, COLUMN_TO_QUERY, COLUMN_TO_SEARCH, TOP_K[, CONFIG]))` | 需要 VectorSearchTableSource；厂商方言顺序可能不同，优先用命名参数 |
| `FROM_CHANGELOG`/`TO_CHANGELOG` | 2.3 | SQL PTF | changelog 与 append 转换 |
| `ON CONFLICT` | 2.3 | `INSERT ... ON CONFLICT DO ...` | 仅 upsert sink |
| 原生 S3 | 2.3 实验 | `flink-s3-fs-native` | 不依赖 Hadoop |
| 自适应分区 | 2.3 | `taskmanager.network.adaptive-partitioner.enabled` | 按数据分布选择分区 |

实验特性不要直接进核心生产链路；迁移前做 savepoint/恢复演练。
