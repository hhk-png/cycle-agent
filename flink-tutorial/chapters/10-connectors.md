# 第10章 连接器与数据集成

## 10.1 连接器是流作业的边界

Flink 作业的可靠性，很大程度取决于两端的连接器：

- Source 能否正确分区、发现新分区、生成 watermark、提交 offset；
- Sink 能否批量写入、处理失败、参与 checkpoint、保证幂等或事务；
- 序列化格式能否演进；
- 连接器版本是否与 Flink 版本匹配；
- 连接器是否支持反压、限流和优雅停止。

一个内部逻辑完全正确的作业，也可能因为 source 没把 offset 放进 checkpoint，或 sink 重复写入，而破坏端到端语义。

```text
source 语义 × 算子语义 × sink 语义 = 端到端语义
```

连接器不是“读写插件”，而是**一致性协议的一部分**。

## 10.2 Source API 的演进

旧接口 `SourceFunction` 在 Flink 2.0 已移除。现代 API 是 FLIP-27 的 `Source`，它把 source 拆成：

- `SplitEnumerator`：发现/分配分片（Kafka 分区、文件分片）；
- `SourceReader`：读取分片、生成记录；
- `Split`：可分片的工作单元；
- `Source`：把上述组件组合起来。

```text
JobManager
  └─ SplitEnumerator：发现分区，分配给 reader
        │
        ▼
TaskManager
  └─ SourceReader 0：读 split 0,1
  └─ SourceReader 1：读 split 2,3
```

好处：

- 支持动态分区发现；
- 支持批流统一；
- 支持状态保存与恢复；
- 更容易实现 exactly-once offset 管理；
- 支持 watermark 与空闲检测。

Kafka、Pulsar、文件、CDC 等现代连接器基本都基于新 API。

### Source 的四个独立问题

很多“source 配置”讨论把四件事混在一起：

1. **分片发现**：什么时候发现新分区/新文件？
2. **读取位置**：首次从 earliest/latest/committed 开始？
3. **watermark**：如何从记录生成事件时间？
4. **offset 提交**：checkpoint 内保存，还是额外提交到外部？

这四个问题要分别配置、分别监控。例如 Kafka 的 `setStartingOffsets` 只影响首次启动；恢复时以 checkpoint 中的 offset 为准。

## 10.3 Sink API 与一致性

Sink 的写入语义通常有三种：

- **at-most-once**：失败就丢，几乎不用；
- **at-least-once**：失败重试，可能重复；
- **exactly-once**：事务/幂等，端到端不重不丢。

Flink 的 `Sink V2` 提供 `SinkWriter`、`Committer`、`State` 等抽象，支持：

- 预提交（pre-commit）；
- checkpoint 完成时提交（commit）；
- 失败时回滚/丢弃；
- 状态恢复。

```text
checkpoint 开始
  → SinkWriter 写临时文件/事务
  → 返回 committable 句柄
checkpoint 完成
  → Committer 提交（rename/commit transaction）
checkpoint 失败/恢复
  → 丢弃未提交句柄，重放数据
```

不同 sink 的实现：

- 文件 sink：写临时文件，checkpoint 完成时 rename；
- Kafka sink：事务；
- JDBC sink：幂等 upsert 或事务表；
- 湖仓 sink：表格式的 ACID 提交。

### 幂等 vs 事务

| 方式 | 原理 | 优点 | 缺点 |
|---|---|---|---|
| 事务 | 2PC，提交前不可见 | 严格 EOS | 依赖外部系统事务能力 |
| 幂等 | 主键 upsert，重复写覆盖 | 实现简单、兼容性好 | 要求业务主键；无法处理删除/聚合 |
| 去重表 | 记录已处理 id | 通用 | 额外存储和查询开销 |

“幂等”不是银弹：如果下游是 append-only 日志或聚合结果，重复写仍然会重复计数。

## 10.4 常见连接器地图

| 类型 | 连接器 | 典型语义 | 备注 |
|---|---|---|---|
| 消息 | Kafka、Pulsar、RabbitMQ、Fluss | EOS/至少一次 | 分区、offset、事务 |
| CDC | MySQL、PostgreSQL、Oracle、MongoDB | 至少一次/增量快照 | Flink CDC 3.x pipeline |
| 文件/对象存储 | FileSystem、S3、HDFS、OSS | 两阶段提交 | 小文件、压缩、分区 |
| 数据库 | JDBC、HBase、Redis | 至少一次/幂等 | 批大小、连接池、主键 |
| 搜索/OLAP | Elasticsearch、Doris、ClickHouse、StarRocks | 至少一次/幂等 | 反压、批量、副本 |
| 湖仓 | Paimon、Iceberg、Hudi | EOS/增量 | compaction、schema 演进 |
| 云服务 | Kinesis、Pub/Sub、EventHub | 视实现 | 限流、凭证、区域 |

选择连接器时不要只看“支持 exactly-once”标签，要确认：

- 哪个版本支持；
- 事务超时与 checkpoint 间隔是否匹配；
- 失败恢复时会不会重复；
- 是否有背压和限流；
- 社区维护状态；
- 与 Flink 2.x 的兼容性（很多 1.x 连接器尚未迁移）。

### Apache Fluss

Fluss（Flink Unified Streaming Storage）是 2026-07 从 Apache 孵化器毕业的顶级项目（TLP），2026-09 发布 1.0.0。它定位是“为分析型流负载设计的湖仓原生流存储”，支持列式、实时更新和流读。它与 Flink 的集成适合：

- 需要低延迟点查 + 流式更新的场景；
- 替代“Kafka + 数据库”的组合；
- 实时湖仓的摄取层。

选型时要评估成熟度、社区和运维复杂度，不要因为“新”就上生产。

## 10.5 分区、并行度与顺序

Source 的并行度通常受分区数限制。Kafka 一个分区只能被一个 reader 消费，因此：

- source 并行度 > 分区数时，多余 subtask 空闲；
- 扩缩容时分区重新分配；
- 单分区内有序，跨分区不保证全局有序；
- watermark 取所有分区最小值。

```text
Kafka topic: 6 partitions
source parallelism=8
→ 6 个 subtask 工作，2 个空闲
```

Sink 的并行度决定写入并发。数据库 sink 并行度过高会打爆连接池；文件 sink 并行度过高会产生大量小文件；Kafka sink 的分区由消息 key 决定，可能倾斜。

### 顺序保证

- 同一 Kafka 分区内有序；
- 经过 `keyBy` 后，同一 key 有序；
- 经过 `rebalance` 后，顺序不保证；
- 多输入算子（union/connect）不保证跨输入顺序；
- sink 的批量提交可能改变可见顺序。

如果业务需要全局有序，只能把并行度设为 1，或在下游按业务键重新排序。

## 10.6 序列化与 Schema

连接器通常负责字节到对象的反序列化。要关注：

- 格式：JSON、Avro、Protobuf、Debezium、Canal、Parquet、ORC；
- Schema Registry 兼容性；
- 字段缺失/新增/类型变化；
- 时间字段的时区和精度；
- 数字精度（金额用 Decimal，不要用 float）；
- 空值语义；
- 字符集和转义。

生产上建议把 schema 作为契约管理，CDC 源尤其要处理 DDL 演进。

```sql
CREATE TABLE orders (
  order_id STRING,
  amount DECIMAL(18, 2),
  order_time TIMESTAMP(3),
  WATERMARK FOR order_time AS order_time - INTERVAL '5' SECOND
) WITH (
  'connector' = 'kafka',
  'topic' = 'orders',
  'properties.bootstrap.servers' = 'kafka:9092',
  'format' = 'avro-confluent',
  'avro-confluent.url' = 'http://schema-registry:8081'
);
```

## 10.7 文件与对象存储

文件 source 适合批处理和回填；文件 sink 常见于数据湖。

文件 sink 的挑战：

- 小文件：按时间/大小滚动，配合 compaction；
- 原子可见性：临时文件 + checkpoint 完成 rename；
- 压缩：snappy/zstd，注意可切分性；
- 分区提交：按日期/小时分区；
- 重复写：恢复时未提交文件要清理；
- 对象存储的 rename 不是原子操作，S3 上要用 multipart upload + copy。

```text
s3://bucket/table/dt=2026-11-26/hour=10/
├── .inprogress-<uuid>       ← 写入中
├── part-0-<uuid>.parquet    ← checkpoint 完成后可见
└── _SUCCESS
```

Paimon/Iceberg/Hudi 把这些复杂性封装成表格式，并提供 ACID、schema 演进和增量读。第 13 章会展开。

## 10.8 Hybrid Source 与批流切换

Hybrid Source 允许“先读历史数据，再切换到实时数据”：

```java
// historySource 可以是 FileSource，实时段是 KafkaSource
HybridSource<Event> source = HybridSource
    .builder(historySource)
    .addSource(kafkaSource)
    .build();
```

典型场景：

- 作业首次启动时回填历史，然后接 Kafka；
- 数据迁移期间双读；
- 批流一体的统一入口。

难点是**切换边界**：历史数据的结束位置必须与实时数据的起始位置对齐，否则会重复或遗漏。生产上要用“水位/分区/offset”做明确边界，并在切换后校验。

## 10.9 连接器的运维

- **版本矩阵**：Flink 2.x 与连接器版本的兼容性要查官方文档；
- **限流**：source 的读取速率、sink 的写入速率都要可控；
- **重试**：区分可重试（网络）和不可重试（数据格式）错误；
- **监控**：offset lag、写入失败、批量大小、连接池、事务数；
- **优雅停止**：`stop-with-savepoint` 让 source 停止读新数据，处理完 in-flight 再退出；
- **凭证管理**：不要在代码里硬编码密码，用 K8s Secret/凭证托管。

## 10.10 MiniFlink 的连接器

MiniFlink 提供：

- `CollectionSource` / `FileSource`；
- `InMemoryKafka` + `KafkaSource` / `KafkaSink`；
- `TextFileSink`（临时文件 + rename）；
- `SqliteSink`（JDBC 风格批提交）。

```python
bus = InMemoryKafka()
bus.create_topic("orders")
bus.publish("orders", {"id": 1, "amount": 10})

env = StreamExecutionEnvironment()
stream = env.from_kafka(bus, "orders", group_id="demo")
env.add_sink(lambda: KafkaSink(bus, "out"), stream)
env.execute()
```

MiniFlink 的 Kafka 是内存模拟，没有分区副本、事务和消费组协调；`KafkaSink` 也不是事务 sink。它用于演示 offset 快照和连接器形状，生产语义见第 11 章。

## 10.11 连接器选型清单

- [ ] source 是否支持分区发现、offset checkpoint、watermark？
- [ ] sink 的一致性语义是什么？事务、幂等还是至少一次？
- [ ] 连接器版本是否支持当前 Flink 版本？
- [ ] 事务超时、批量大小、连接池是否与 checkpoint 匹配？
- [ ] 是否有反压和限流？下游能否承受峰值？
- [ ] schema 演进策略是什么？CDC DDL 如何处理？
- [ ] 失败恢复时会不会重复/丢数据？如何验证？
- [ ] 社区是否活跃？出问题能否拿到支持？

## 10.12 连接器关键参数对照

### Kafka

| 参数 | 方向 | 作用 |
|---|---|---|
| `properties.bootstrap.servers` | 双向 | broker 地址 |
| `topic` / `topic-pattern` | source | 订阅主题/正则 |
| `properties.group.id` | source | 消费组 |
| `scan.startup.mode` | source | earliest/latest/group-offsets/timestamp/specific-offsets |
| `scan.topic-partition-discovery.interval` | source | 分区发现 |
| `sink.delivery-guarantee` | sink | at-least-once/exactly-once |
| `sink.transactional-id-prefix` | sink | 事务前缀，必须唯一 |
| `properties.transaction.timeout.ms` | sink | 事务超时 |
| `properties.isolation.level` | consumer | 下游 read_committed |

### JDBC

| 参数 | 作用 |
|---|---|
| `url` / `table-name` | 连接与目标表 |
| `driver` | JDBC 驱动 |
| `sink.buffer-flush.max-rows` | 批量大小 |
| `sink.buffer-flush.interval` | 批量间隔 |
| `sink.max-retries` | 重试次数 |
| `connection.max-retry-timeout` | 连接超时 |
| `lookup.cache.max-rows` / `lookup.cache.ttl` | 维表缓存 |

JDBC sink 通常是 at-least-once；要 exactly-once，需要主键 upsert + 幂等，或外部事务表。

### Paimon

| 参数 | 作用 |
|---|---|
| `warehouse` | 表仓库路径 |
| `database` / `table` | 库表 |
| `primary-key` | 主键（upsert） |
| `changelog-producer` | 生成 changelog 的方式 |
| `compaction.min.file-num` | 小文件合并阈值 |
| `snapshot.num-retained` | 快照保留 |

Paimon 的写入一致性、changelog 和小文件治理要一起设计，不能只看“支持 upsert”。

### 文件/对象存储

| 参数 | 作用 |
|---|---|
| `path` | 目录 |
| `format` | parquet/orc/avro/json |
| `sink.rolling-policy.file-size` | 文件滚动大小 |
| `sink.rolling-policy.rollover-interval` | 滚动时间 |
| `sink.partition-commit.policy.kind` | 分区提交策略 |
| `sink.partition-commit.delay` | 提交延迟 |

文件 sink 的“原子可见”依赖临时文件 + checkpoint 完成后的 rename/提交；下游要能处理延迟可见。

## 10.13 自定义 Source/Sink V2 连接器开发

前面讲的是“怎么选连接器”。当现有连接器不满足需求时，平台团队往往要自己写一个。自定义连接器不是“实现一个 read/write 方法”这么简单：它要把**分片、offset、watermark、checkpoint、恢复和提交**纳入 Flink 的一致性协议。这一节给出工程化的实现骨架；具体接口签名以所用 Flink 版本的 `flink-core`/`flink-connector-base` 为准。

### 10.13.1 什么时候值得自己写

适合自研：

- 内部存储/协议没有成熟连接器；
- 需要特殊的并行读取、分片发现或限流；
- 现有连接器的 checkpoint、恢复或 schema 行为不满足；
- 需要把公司内部的权限、审计、配额嵌进读写路径。

不适合自研：

- 只是为了“少一个依赖”；
- 上游系统没有分片/位点/幂等能力，却希望连接器凭空实现 exactly-once；
- 团队没有长期维护连接器版本矩阵的预算。

连接器的成本不在第一版，而在**每次 Flink 升级、每次上游协议变化、每次故障复盘**。先确认没有现成连接器可用，再决定自研。

### 10.13.2 Source V2 的四个对象

FLIP-27 把 source 拆成四个协作对象：

```text
Source
├── SplitEnumerator  发现分片、分配给 reader、保存枚举状态
├── SourceReader     读取一个或多个 Split、发记录/watermark、保存读取位点
├── Split            可分片的工作单元（分区、文件段、查询区间）
└── SourceOutput     向下游发记录、watermark、idleness
```

一个 source 必须回答：

1. **有界还是无界**：`Boundedness.BOUNDED` 还是 `CONTINUOUS_UNBOUNDED`；
2. **怎么分片**：分片是静态列表，还是运行期动态发现；
3. **谁来分配**：enumerator 按什么策略把 split 给 reader；
4. **位点是什么**：split 内的 offset/游标如何序列化；
5. **watermark 从哪来**：记录时间戳、分区最小值、idleness 如何处理；
6. **恢复时谁记得什么**：enumerator 的待分配队列、reader 的当前 split 和位点。

### 10.13.3 一个分片 source 的骨架

下面是一个“可动态发现分片的有界/无界 source”的形状。省略了具体存储客户端与两个序列化方法（`getSplitSerializer()`、`getEnumeratorCheckpointSerializer()`），只保留 Flink 契约的主干；完整实现必须补齐这两个方法，否则 `Source` 接口无法编译。

```java
public final class MySource
        implements Source<Event, MySplit, List<MySplit>> {

    private final SourceConfig config;

    @Override
    public Boundedness getBoundedness() {
        return config.bounded()
            ? Boundedness.BOUNDED
            : Boundedness.CONTINUOUS_UNBOUNDED;
    }

    @Override
    public SplitEnumerator<MySplit, List<MySplit>> createEnumerator(
            SplitEnumeratorContext<MySplit> ctx) {
        return new MyEnumerator(ctx, config);
    }

    @Override
    public SplitEnumerator<MySplit, List<MySplit>> restoreEnumerator(
            SplitEnumeratorContext<MySplit> ctx,
            List<MySplit> checkpoint) {
        return new MyEnumerator(ctx, config, checkpoint);
    }

    @Override
    public SourceReader<Event, MySplit> createReader(
            SourceReaderContext ctx) {
        return new MyReader(ctx, config);
    }
}
```

枚举器负责“发现 + 分配 + 记住待分配分片”：

```java
final class MyEnumerator
        implements SplitEnumerator<MySplit, List<MySplit>> {

    private final SplitEnumeratorContext<MySplit> ctx;
    private final Deque<MySplit> pending = new ArrayDeque<>();

    @Override
    public void start() {
        // 动态发现：不要阻塞 start()；用 callAsync 周期性拉取
        ctx.callAsync(
            this::discoverSplits,
            this::handleDiscoveryFailure);
    }

    @Override
    public void handleSplitRequest(int subtaskId, String hostname) {
        MySplit split = pending.pollFirst();
        if (split != null) {
            ctx.assignSplit(split, subtaskId);
        } else if (boundedAndDiscovered()) {
            ctx.signalNoMoreSplits(subtaskId);
        }
    }

    @Override
    public void addSplitsBack(List<MySplit> splits, int subtaskId) {
        // reader 失败/扩缩容：把未完成分片放回待分配队列
        pending.addAll(splits);
    }

    @Override
    public void addReader(int subtaskId) {
        // 新 reader 上线，可以主动分配
    }

    @Override
    public void snapshotState(List<MySplit> state) {
        state.clear();
        state.addAll(pending);
        // 已分配但未完成的 split 由 reader 自己快照；
        // 枚举器只保存“还没分出去”的部分，避免重复分配。
    }

    @Override
    public void notifyCheckpointComplete(long checkpointId) {
        // 如果源端有“已确认位点”需要提交，在这里做；
        // 注意它可能被重复调用，必须幂等。
    }

    @Override
    public void close() {
        // 关闭客户端/线程池
    }
}
```

reader 负责读取、发 watermark、快照当前位点：

```java
final class MyReader implements SourceReader<Event, MySplit> {

    private final SourceReaderContext ctx;
    private final Map<String, MySplit> current = new HashMap<>();
    private boolean noMoreSplits = false;

    @Override
    public void start() {
        ctx.sendSplitRequest();
    }

    @Override
    public InputStatus pollNext(ReaderOutput<Event> output) {
        for (MySplit split : current.values()) {
            Event event = readNext(split);
            if (event == null) {
                continue;
            }
            output.collect(event);
            if (event.watermark() != null) {
                output.emitWatermark(event.watermark());
            }
            return InputStatus.MORE_AVAILABLE;
        }
        return noMoreSplits
            ? InputStatus.END_OF_INPUT
            : InputStatus.NOTHING_AVAILABLE;
    }

    @Override
    public List<MySplit> snapshotState(long checkpointId) {
        // 保存每个 split 的位点；必须可序列化、可恢复
        return new ArrayList<>(current.values());
    }

    @Override
    public void addSplits(List<MySplit> splits) {
        for (MySplit split : splits) {
            current.put(split.splitId(), split);
        }
    }

    @Override
    public void notifyNoMoreSplits() {
        noMoreSplits = true;
    }

    @Override
    public void close() {
        // 关闭读取客户端
    }
}
```

这段骨架故意没有写“如何读存储”，因为那部分因系统而异。真正需要对齐 Flink 的是：

- **分片 id 稳定**：恢复/扩缩容后同一个 split 能被识别；
- **位点可序列化**：offset、文件路径、游标、查询条件都要能进 checkpoint；
- **watermark 单调**：多分片时下游取最小值，空闲分片要有 idleness 或显式结束；
- **pollNext 不阻塞**：慢 IO 要用异步/批量，阻塞会拖住 task 线程；
- **addSplitsBack 幂等**：失败重分配不能造成重复读取或丢分片；
- **有界 source 要能结束**：`END_OF_INPUT` 之后下游才能收尾。

### 10.13.4 状态、恢复与 rescale

Source 的状态分两半：

```text
Enumerator 状态：待分配 split 队列、发现进度、分配策略元数据
Reader 状态：当前 split + split 内位点
```

恢复时：

1. 枚举器从 checkpoint 恢复待分配队列；
2. reader 从 checkpoint 恢复当前 split 和位点；
3. 新并行度下，未完成 split 通过 `addSplitsBack` 重新分配；
4. 已经完成但尚未提交的外部位点，按源的语义决定是否重放。

扩缩容最容易出问题的地方：

- 枚举器把“已分配”当成“已完成”，导致新 reader 拿不到 split；
- reader 快照只存了 split id，没存 split 内 offset；
- 动态发现的新 split 在 checkpoint 之后才加入，恢复后重复/遗漏；
- 外部系统按连接而不是按 split 记录位点，扩缩容后无法拆分。

一个实用检查：**把并行度从 N 改到 M，再从 savepoint 恢复，核对每个 split 恰好被处理一次**。这比只跑单并行度测试更能暴露状态设计问题。

### 10.13.5 Sink V2 与两阶段提交

Sink V2 把写入拆成 `SinkWriter` 和 `Committer`：

```text
SinkWriter.write()
  → 写临时文件/事务/缓冲
  → prepareCommit() 返回 committable
  → checkpoint 完成
  → Committer.commit() 让结果对外可见
```

骨架：

```java
public final class MySink implements Sink<Event> {

    @Override
    public SinkWriter<Event, CommitHandle, Void> createWriter(
            InitContext context) {
        return new MyWriter(context);
    }

    @Override
    public Optional<Committer<CommitHandle>> createCommitter() {
        return Optional.of(new MyCommitter());
    }

    @Override
    public Optional<SimpleVersionedSerializer<CommitHandle>>
            getCommittableSerializer() {
        return Optional.of(new CommitHandleSerializer());
    }

    @Override
    public Optional<SimpleVersionedSerializer<Void>>
            getWriterStateSerializer() {
        return Optional.of(new VoidSerializer());
    }
}
```

Writer 的关键是“预提交”和“快照”分离：

```java
final class MyWriter implements SinkWriter<Event, CommitHandle, Void> {

    @Override
    public void write(Event event, Context context) {
        // 写入临时位置/事务；此时对外不可见
    }

    @Override
    public void flush(boolean endOfInput) {
        // 刷出当前缓冲；不等于提交
    }

    @Override
    public List<CommitHandle> prepareCommit(boolean flush) {
        // 返回“checkpoint 完成后可以提交”的句柄
        return List.of(snapshotHandle());
    }

    @Override
    public List<Void> snapshotState(long checkpointId) {
        // 保存 writer 自身状态（未提交批次、句柄等）
        return List.of();
    }

    @Override
    public void close() {
        // 释放资源；不要在这里偷偷提交
    }
}
```

Committer 必须满足：

- **幂等**：同一个 committable 重复提交不产生第二份可见数据；
- **可重试**：网络抖动后能安全重试；
- **可中止**：checkpoint 失败/作业取消时能 abort 未提交事务；
- **有界**：提交失败不能无限阻塞 checkpoint 完成通知。

```java
final class MyCommitter implements Committer<CommitHandle> {

    @Override
    public void commit(List<CommitHandle> handles) {
        for (CommitHandle handle : handles) {
            commitIdempotently(handle);
        }
    }

    @Override
    public void abort(List<CommitHandle> handles) {
        for (CommitHandle handle : handles) {
            abortQuietly(handle);
        }
    }
}
```

如果 sink 没有事务能力，只能用**幂等写**：要求下游有主键/唯一键，重复写覆盖而不是追加。幂等写不是“连接器自己保证”的，它依赖业务键和下游约束。

### 10.13.6 错误处理、测试与打包

错误分三类：

- **可重试**：网络超时、限流、5xx。退避重试，不要把整个作业打挂；
- **不可重试**：schema 错误、权限拒绝、非法参数。进入死信/侧输出，或快速失败；
- **分片级失败**：某个 split 读不了。把 split 放回枚举器，其他 split 继续。

测试至少覆盖：

- `SplitEnumerator` 的分配、`addSplitsBack`、快照/恢复；
- `SourceReader` 的 poll、watermark、offset 快照、`END_OF_INPUT`；
- `SinkWriter` 的 write/prepareCommit/abort；
- MiniCluster 端到端：source → 算子 → sink；
- savepoint 恢复、并行度变化、故障注入；
- 连接器版本与 Flink 版本的兼容矩阵。

打包时：

- Flink 核心依赖用 `provided`；
- 连接器依赖按需 shade，避免和集群自带版本冲突；
- 不要把测试凭证、连接串打进 jar；
- 记录 connector 版本、Flink 版本、上游协议版本；
- 为 1.x V1 接口（`SourceFunction`/`SinkFunction`）的移除准备迁移路径。

### 10.13.7 自定义连接器检查清单

- [ ] 分片 id、位点、枚举器状态都可序列化且稳定；
- [ ] 恢复/扩缩容后每个 split 恰好被处理一次；
- [ ] 有界 source 能正确发出 `END_OF_INPUT`；
- [ ] 空闲分片不会永久拖住 watermark；
- [ ] sink 的 commit 幂等、abort 安全、失败可重试；
- [ ] checkpoint 完成通知重复调用不会产生副作用；
- [ ] 错误分类清晰，死信/重试/快速失败有明确策略；
- [ ] 有 savepoint、rescale、故障注入测试；
- [ ] 版本矩阵和升级路径已记录；
- [ ] 没有把 Flink 核心依赖打进 fat jar。

以上是 DataStream 层的 Source/Sink V2。如果目标是让内部系统被 SQL 直接查询，需要的是 Table 层的 `DynamicTableFactory`、`DynamicTableSource`/`DynamicTableSink` 以及谓词/投影/limit 下推；这部分见[附录 K](../appendix/K-table-connector-catalog.md)。

## 10.14 Schema 契约、序列化与兼容性工程

连接器把字节变成记录，Schema 决定“这些字节是什么意思”。生产事故里，schema 问题往往比连接器 bug 更多：上游加了一个字段、改了一个类型、把毫秒改成秒，下游作业可能不会立刻报错，而是静默算出错误结果。

### 10.14.1 三层契约

```text
事件契约：业务字段、语义、单位、枚举、时区、必填/可选
序列化契约：Avro/Protobuf/JSON Schema 的字段编号、类型、默认值
状态契约：Flink 算子状态里对象的序列化格式
```

三层要分别版本化。只改事件契约不升级序列化，或只升级序列化不管状态，都会在恢复/升级时出问题。Flink 类型系统与 POJO/Kryo 规则见[第 3 章 3.6 节](03-datastream-api.md#36-类型系统与序列化)，状态 schema 演进见[第 9 章 9.7 节](09-recovery.md#97-状态-schema-演进)，CDC/湖仓 schema 演进见[第 13 章 13.4 节](13-cdc-lakehouse.md#134-schema-演进)。

### 10.14.2 兼容性规则

| 变更 | 向后兼容（新读旧） | 向前兼容（旧读新） | 建议 |
|---|---|---|---|
| 新增带默认值的可选字段 | 是 | 是 | 优先 |
| 新增必填字段 | 否 | 视格式 | 先填默认值/双写 |
| 删除字段 | 视格式 | 可能否 | 标记 deprecated，保留一段时间 |
| 改字段类型 | 通常否 | 通常否 | 新字段 + 迁移 |
| 改字段名 | 否（按名） | 否 | 用字段 id/别名 |
| 扩大枚举 | 是 | 可能否 | 下游要有 unknown 分支 |
| 改单位/时区 | 语义破坏 | 语义破坏 | 新字段 + 版本 |

Avro 用字段名、默认值和别名支持演进，Protobuf 用字段编号和默认值；JSON 没有强约束，必须靠 Schema Registry 和代码校验。不要把“JSON 能解析”当成“兼容”。

### 10.14.3 Schema Registry 的工程策略

- **subject 命名**：按 topic 还是按事件类型？按 topic 简单，按类型更适合多 topic 复用；
- **兼容性级别**：BACKWARD（默认常用）、FORWARD、FULL、NONE；生产至少 BACKWARD，核心链路 FULL；
- **版本不可变**：已注册的 schema 不要原地修改，发新版本；
- **ID 传递**：消息头/消息体带 schema id，消费者按 id 解析；
- **灰度**：新 schema 先双写/影子消费，再切主链路；
- **权限**：谁能注册、谁能升级兼容性级别，要审计。

### 10.14.4 CDC/DDL 演进

CDC 会把源库 DDL 带进流：

- 新增列：下游表能否自动加列？历史行怎么补默认值？
- 删除列：流里不再出现，但状态/湖表还保留旧列；
- 改类型：`INT → BIGINT` 通常可扩，反向可能截断；
- 改列名：按列 id 还是列名映射？
- 主键变更：通常需要新表/重放，不能在线合并。

策略：

- 核心表用“受控演进”（DDL 审批 + 显式 `ALTER`）；
- 非核心表可以自动同步，但要有回滚；
- 下游查询/湖表 schema 要一起验证；
- 保留 schema 版本和变更记录，便于对账。

### 10.14.5 Flink 类型与状态序列化

- 优先 POJO/Avro/Protobuf，避免 Kryo 兜底；
- 开发期用 `disableGenericTypes()` 把隐式 Kryo 变成显式失败；
- 状态对象只放必要字段，避免大对象/复杂集合；
- 字段增删改按第 9 章的兼容性矩阵验证；
- 跨 Flink 大版本升级必须做 savepoint 恢复演练；
- 自定义 `TypeSerializer` 要版本化，并测试旧数据读取。

### 10.14.6 坏数据处理

- 解析失败 → 侧输出/死信队列，不要静默丢弃；
- schema 不匹配 → 记录原始字节 + schema id + 错误原因；
- 毒消息 → 隔离并告警，不要无限重试；
- 下游兼容 → 未知枚举/新增字段要有默认分支；
- 死信要有保留期、重放工具和责任人。

### 10.14.7 兼容性测试矩阵

| 测试 | 目的 |
|---|---|
| 新代码读旧消息 | 向后兼容 |
| 旧代码读新消息 | 向前兼容 |
| 新作业从旧 savepoint 恢复 | 状态兼容 |
| 旧作业从新 savepoint 恢复 | 回滚能力 |
| schema 注册中心拒绝不兼容变更 | 门禁 |
| 坏消息进入死信而非拖垮作业 | 容错 |
| 双跑/对账 | 语义兼容 |

### 10.14.8 检查清单

- [ ] 事件 schema 有 owner、版本和兼容性级别；
- [ ] 新增/删除/改类型有明确迁移方案；
- [ ] Schema Registry 的 subject 和权限已设计；
- [ ] CDC DDL 是自动同步还是受控演进？
- [ ] 状态对象避免 Kryo 兜底，升级演练通过；
- [ ] 坏消息有死信、告警和重放工具；
- [ ] 兼容性测试覆盖新读旧、旧读新、状态恢复；
- [ ] schema 变更进入 CI/评审，不靠口头约定。

## 10.15 Table 连接器：让 SQL 认识你的系统

10.13 讲的是 DataStream 层的 Source/Sink V2。很多平台场景需要的不是 `fromSource`，而是让业务同学直接写：

```sql
CREATE TABLE internal_orders (...) WITH ('connector' = 'internal-orders', ...);
SELECT ... FROM internal_orders;
```

这就是 Table 连接器。它比 DataStream 连接器多了一层**计划期契约**：

| 契约 | 作用 | 写错的后果 |
|---|---|---|
| 工厂与选项 | DDL → 连接器实例 | `factory not found`、选项被忽略 |
| `ChangelogMode` | 声明产出 `+I/-U/+U/-D` | 下游 upsert 失效、结果错误 |
| 谓词下推 | 把过滤条件下推到远端 | 全表扫描、结果多行/少行 |
| 投影下推 | 只读需要的列 | 带宽浪费、字段下标错位 |
| 元数据列 | 暴露分区/offset/版本 | 调试/审计信息缺失 |
| watermark | 事件时间推进 | 窗口不触发、结果延迟 |
| Sink 提交协议 | 端到端一致性 | 重复写、丢数 |

一个最小工厂：

```java
public final class InternalOrdersFactory implements DynamicTableFactory {

    @Override
    public String factoryIdentifier() {
        return "internal-orders";
    }

    @Override
    public Set<ConfigOption<?>> requiredOptions() {
        return Set.of(ENDPOINT);
    }

    @Override
    public Set<ConfigOption<?>> optionalOptions() {
        return Set.of(BATCH_SIZE);
    }

    @Override
    public DynamicTableSource createDynamicTableSource(Context context) {
        FactoryUtil.TableFactoryHelper helper =
            FactoryUtil.createTableFactoryHelper(this, context);
        ReadableConfig options = helper.getOptions();
        helper.validate();
        return new InternalOrdersSource(
            options.get(ENDPOINT), context.getPhysicalRowDataType());
    }

    @Override
    public DynamicTableSink createDynamicTableSink(Context context) {
        // 不需要写就抛 UnsupportedOperationException，或返回一个明确的错误
        throw new UnsupportedOperationException("internal-orders 只支持读取");
    }
}
```

三个最常见的坑：

1. **声明了下推却没实现**：规划器信任 `SupportsFilterPushdown`，不再加 `Filter`，远端又没过滤，结果多出行；
2. **`ChangelogMode` 与真实行为不一致**：声明 insert-only，实际发 `-U/+U`，下游可能丢更新；
3. **元数据列顺序错位**：`applyReadableMetadata` 的字段顺序与 `metadataKeys` 不一致，offset 被读成 partition。

Table 连接器的完整开发手册（`ScanTableSource`/`LookupTableSource`、Sink V2、下推契约、Catalog SPI、测试与打包）见[附录 K](../appendix/K-table-connector-catalog.md)。本节只建立边界：**DataStream 连接器解决“记录怎么进出”，Table 连接器还要解决“SQL 计划怎么信任你”。**

## 10.16 连接器生态矩阵与升级路径

连接器选型不能只看“有没有”，还要看一致性、并行度、schema 演进和维护状态。下表是常见连接器的能力速查；具体版本、参数和限制以所用连接器文档为准。

| 连接器 | 方向 | 一致性（典型） | 并行度/分区 | 关键注意 |
|---|---|---|---|---|
| Kafka | source/sink | sink 2PC exactly-once；source 按 offset | source 受分区数限制 | 事务超时、`read_committed`、分区倾斜 |
| Pulsar | source/sink | sink 事务/幂等 | 按 topic/partition | 订阅类型、事务超时 |
| RabbitMQ | source/sink | 通常 at-least-once | 队列/消费者 | 没有原生 2PC，靠幂等/去重 |
| Fluss | source/sink/lookup | 流式存储，支持点查/增量读 | 按 bucket/partition | 独立 Apache TLP，Delta Join 的常见底座 |
| Flink CDC | source | 全量+增量，checkpoint 对齐 | 按 snapshot split/分区 | schema 演进、锁、位点 |
| JDBC | source/sink | sink 通常 at-least-once；靠主键幂等 | 按分区/批量 | 连接池、批量大小、upsert 键 |
| Hive | source/sink | 批写，提交靠分区/文件 | 按分区 | ACID 表限制、小文件 |
| Iceberg | source/sink | 2PC/原子提交 | 按分区 | 提交冲突、快照保留、小文件 |
| Paimon | source/sink | 2PC/原子提交 | 按 bucket/分区 | 合并引擎、compaction、changelog |
| Hudi | source/sink | 2PC/幂等 upsert | 按分区 | 表类型、索引、compaction |
| Elasticsearch | sink | sink 通常 at-least-once；按 doc id 幂等 | 按分片 | 官方只有 sink；读 ES 用 CDC/Kafka 或第三方 source |
| HBase | source/sink | 通常 at-least-once；按 rowkey 幂等 | 按 region | rowkey 设计、热点 |
| Redis | sink | 通常 at-least-once | 按 key | 连接池、幂等、内存 |
| Doris/StarRocks | sink | 视版本支持 2PC/stream load | 按 tablet/分区 | 事务/标签、批量、反压 |
| ClickHouse | sink | 通常 at-least-once；靠去重表/幂等 | 按分片 | 批量、分布式表、去重 |
| Kinesis | source/sink | source 按 shard；sink 通常 at-least-once | 受 shard 数限制 | 吞吐/分片、KPL |
| Pub/Sub | source/sink | 通常 at-least-once | 按 subscription | 去重、流控 |
| Event Hubs | source/sink | 视 checkpoint/事务 | 按 partition | 吞吐单位、epoch |
| 文件/对象存储 | source/sink | 2PC/rename；取决于格式 | 按文件/分区 | 小文件、可见性、提交 |
| DataGen/Print/Blackhole | source/sink | 测试用 | 可配置 | 只用于本地验证 |

### 10.16.1 选型的五个问题

1. **端到端语义**：source 能否回拨？sink 是否支持 2PC/幂等？下游是否 `read_committed`？
2. **并行度边界**：source 并行度是否受分区数限制？扩到超过分区数有没有收益？
3. **schema 演进**：格式/连接器是否支持新增字段、类型变更、未知字段？
4. **运维成本**：版本兼容、连接池、限流、监控、升级路径；
5. **故障语义**：超时、重试、死信、部分提交，是否有明确处理。

### 10.16.2 升级路径

连接器升级通常比 Flink 升级更频繁，也更容易出问题：

```text
1. 查兼容矩阵：连接器版本 ↔ Flink 版本 ↔ 上游/下游版本
2. 在副本环境跑：读写、checkpoint/restore、rescale、故障注入
3. 对比 schema/序列化/状态：连接器内部状态是否兼容
4. 灰度：新连接器只接一部分分区/作业
5. 回滚：保留旧 jar/旧镜像/旧 savepoint
```

不要把“连接器能编译”当成“能升级”。连接器的状态（offset、事务句柄、分片分配）也是 savepoint 的一部分；升级后恢复失败，往往不是 Flink 的问题，而是连接器状态 schema 变了。

### 10.16.3 检查清单

- [ ] 每个连接器都有明确的端到端语义（EOS/at-least-once）；
- [ ] source 并行度与分区数匹配；
- [ ] sink 的幂等键/事务边界已设计；
- [ ] schema 演进和坏数据处理有策略；
- [ ] 连接器版本进入兼容矩阵和制品清单；
- [ ] checkpoint/restore、rescale、故障注入已测试；
- [ ] 有升级/回滚步骤和旧制品保留；
- [ ] 监控覆盖 lag、失败率、重试、事务超时。

## 本章小结

连接器决定作业能读什么、写什么、以什么语义写。选型时要同时看功能、一致性、版本、限流和运维成本。source 的 offset、sink 的事务、schema 的演进，三者共同决定端到端语义。

自定义连接器时，真正的工作量在分片、位点、watermark、checkpoint 和提交协议，而不是读写 API。把这几件事设计对，连接器才能在生产升级、扩缩容和故障恢复中站得住。

下一章专门拆解 Kafka 端到端 exactly-once，这是实时链路最常被问、也最容易配错的部分。
