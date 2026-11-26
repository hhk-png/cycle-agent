# 第7章 状态管理与 StateBackend

## 7.1 状态是流处理的记忆

无状态算子只看当前记录；有状态算子还要记住过去。例如：

- 去重：记住最近见过的 id；
- 累加：记住每个 key 的金额；
- 会话：记住窗口内的行为；
- join：缓存另一条流的数据；
- CEP：记住部分匹配；
- 规则：广播配置；
- 机器学习：维护特征、模型版本和采样窗口。

Flink 的状态不是普通成员变量。成员变量只存在于 task 内存中，task 失败就丢，扩缩容也无法重新分配。Flink 的状态是**受运行时管理、可快照、可恢复、可按 key 分区**的数据。

这个定义包含四个约束：

1. **可快照**：状态必须能被序列化到 checkpoint；
2. **可恢复**：恢复后要能重建到一致逻辑时刻；
3. **可分区**：keyed state 要能按 key group 重新分配；
4. **可演进**：状态 schema 变化不能轻易让作业无法升级。

## 7.2 三种状态

### Keyed State

只能用在 `keyBy` 之后，每个 key 一份：

- `ValueState<T>`：单值；
- `ListState<T>`：列表；
- `MapState<K,V>`：映射；
- `ReducingState<T>`：滚动 reduce；
- `AggregatingState<IN,OUT>`：增量聚合。

```java
public class CountPerUser extends KeyedProcessFunction<String, Event, Long> {
    private ValueState<Long> count;

    @Override
    public void open(OpenContext ctx) {
        count = getRuntimeContext().getState(
            new ValueStateDescriptor<>("count", Types.LONG));
    }

    @Override
    public void processElement(Event e, Context ctx, Collector<Long> out) {
        Long c = count.value();
        c = (c == null ? 0L : c) + 1;
        count.update(c);
        out.collect(c);
    }
}
```

Keyed State 与 key 绑定，运行时按 key 分区。相同 key 一定在同一 subtask，因此可以本地读写。

**namespace**：窗口状态、定时器等会在 key 之上再加一层 namespace（例如窗口对象）。用户通常不直接操作 namespace，但理解它有助于排查“为什么状态比预期大”。

### Operator State

与算子实例绑定，不按 key 分区，常见于 source/sink：

- `ListState`：每个 subtask 一个列表；
- `UnionListState`：恢复时所有 subtask 都能看到全量；
- `BroadcastState`：所有 subtask 持有相同副本，用于规则。

Kafka source 的 offset、文件 source 的读取位置通常就是 operator state。

```java
public class OffsetTracker extends RichFlatMapFunction<Event, Event>
        implements CheckpointedFunction {

    private ListState<Long> offsets;
    private final List<Long> buffer = new ArrayList<>();

    @Override
    public void initializeState(FunctionInitializationContext context) throws Exception {
        // operator state 只能从 FunctionInitializationContext 拿，
        // RuntimeContext 没有 getOperatorStateStore()
        offsets = context.getOperatorStateStore()
            .getListState(new ListStateDescriptor<>("offsets", Types.LONG));
        if (context.isRestored()) {
            for (Long offset : offsets.get()) {
                buffer.add(offset);
            }
        }
    }

    @Override
    public void snapshotState(FunctionSnapshotContext context) throws Exception {
        offsets.clear();
        offsets.addAll(buffer);
    }

    @Override
    public void flatMap(Event event, Collector<Event> out) {
        out.collect(event);
    }
}
```

注意：`RichSourceFunction` 随 `SourceFunction` 一起在 2.0 被移除。2.x 的自定义 source 要用 FLIP-27 的 `Source`/`SourceReader`；自定义算子要读写 operator state，则实现 `CheckpointedFunction`，在 `initializeState` 里通过 `FunctionInitializationContext.getOperatorStateStore()` 获取。上面的例子用 `RichFlatMapFunction` 只是为了展示状态 API，不代表 offset 应该这样业务化实现。

Operator State 的重新分配模式：

- **even-split**：恢复时平均分给所有 subtask；
- **union**：所有 subtask 拿到全量，适合“每个 subtask 都需要完整列表”的场景；
- **broadcast**：每个 subtask 一份相同副本。

### Broadcast State

广播状态是一种特殊的 operator state，通过 `BroadcastProcessFunction` 使用。它适合“小表 + 大流”的规则匹配，不适合存储大量数据。第 20 章会展开。

## 7.3 StateBackend

Flink 的状态后端主要有三类：

| 后端 | 存储位置 | 优点 | 缺点 |
|---|---|---|---|
| HashMapStateBackend | JVM 堆 | 读写快、实现简单 | 受 GC 和内存限制，状态大时不稳定 |
| EmbeddedRocksDBStateBackend | 本地磁盘 + 堆外缓存 | 支持超大状态、增量 checkpoint | 序列化/反序列化开销，调优复杂 |
| ForStStateBackend | 远程 DFS 为主、本地盘缓存 | 分离计算与存储、快照快、恢复快 | 2.3 仍实验，不支持 canonical savepoint/全量快照/changelog |

配置示例：

```yaml
state.backend.type: rocksdb
state.backend.incremental: true
state.checkpoints.dir: s3://bucket/flink/checkpoints
state.backend.rocksdb.memory.managed: true
```

选择建议：

- 状态小于几 GB、追求低延迟：HashMap；
- 状态几十 GB 到 TB、需要增量快照：RocksDB；
- 状态大小不确定、频繁扩缩容：RocksDB + 增量 checkpoint；
- 超大状态、云原生、能接受实验特性：评估 ForSt，但不要把生产押在它上面；
- 不要用“堆内状态 + 大缓存”硬扛，GC 停顿会让 checkpoint 和反压同时恶化。

### HashMapStateBackend 的代价

HashMap 把状态存成 JVM 对象。优点是读写没有序列化开销；缺点是：

- 对象头、指针、装箱带来 2～5 倍内存放大；
- 状态大时 Full GC 可能停顿数秒；
- checkpoint 需要遍历并序列化全部对象，快照时间长；
- 无法做增量快照，每次全量上传。

### RocksDBStateBackend 的代价

RocksDB 把状态存在本地 LSM 树中，堆内只保留 block cache 和 memtable。优点是状态可以远大于内存；缺点是：

- 每次读写都要序列化/反序列化；
- 需要调优 block cache、write buffer、compaction；
- 本地磁盘容量和 IOPS 成为瓶颈；
- 恢复时要下载 SST 文件并重建本地状态。

```yaml
state.backend.rocksdb.block.cache-size: 256mb
state.backend.rocksdb.writebuffer.size: 64mb
state.backend.rocksdb.writebuffer.count: 4
state.backend.rocksdb.compaction.style: LEVEL
```

注意：`state.backend.rocksdb.memory.managed: true`（默认）时，Flink 会用 managed memory 统一分配 block cache/write buffer，`block.cache-size` / `writebuffer.size` 的绝对值不生效。要么关闭 managed memory 并手工预留 off-heap，要么调 `memory.write-buffer-ratio` / `memory.high-prio-pool-ratio`。

### ForSt 的定位

ForSt 把工作状态直接写到远程 DFS，本地盘只做缓存。它试图解决三个问题：

- checkpoint 变成“轻量快照”，不需要每次上传全量；
- 恢复时不需要重新下载全部状态；
- 扩缩容时状态可以更快重分配。

但截至 Flink 2.3，它仍有明确限制：不支持 canonical savepoint、全量快照、changelog 和文件合并 checkpoint；远程读写延迟更高；孤儿文件清理仍是 best-effort。生产选型要把它当“可评估的实验特性”，而不是默认答案。

## 7.4 状态 TTL

状态不会自动过期。会话、去重、维表缓存等场景必须设置 TTL，否则状态无限增长。

```java
StateTtlConfig ttl = StateTtlConfig
    .newBuilder(Duration.ofHours(24))
    .setUpdateType(StateTtlConfig.UpdateType.OnCreateAndWrite)
    .setStateVisibility(StateTtlConfig.StateVisibility.NeverReturnExpired)
    .build();

ValueStateDescriptor<Session> descriptor =
    new ValueStateDescriptor<>("session", Session.class);
descriptor.enableTimeToLive(ttl);
```

TTL 的注意点：

- `UpdateType.OnCreateAndWrite` 只在写入时续期，`OnReadAndWrite` 读取也续期，`Disabled` 不做过期；
- TTL 是**惰性清理**，过期数据可能仍占磁盘，直到被读写或 compaction；
- 不同后端、不同版本的 TTL 行为有差异；
- TTL 不能替代业务上的删除逻辑，重要数据仍要显式 clear；
- TTL 会增加状态元数据，短 TTL + 大状态时要评估开销。

### TTL 不是“定时删除”

Flink 的 TTL 不会起一个定时器逐条删除。它在读写时判断是否过期，并在 compaction 时清理。这意味着：

- 状态文件大小可能长期不降；
- 监控“逻辑状态大小”和“物理存储大小”可能不一致；
- 依赖 TTL 做容量回收时，要预留 compaction 空间。

## 7.5 状态序列化与演进

状态最终要写入 checkpoint，因此必须可序列化。类型变化会导致恢复失败：

- 新增字段：通常可兼容（POJO/Avro/Protobuf 视 schema 策略而定）；
- 删除/重命名字段：可能不兼容；
- 改变字段类型：通常不兼容；
- 改变类名/包名：Kryo 可能不兼容；
- 改变 key 的类型：分区结果变化，状态无法对应。

生产建议：

- 状态对象使用稳定的 schema（Avro/Protobuf/JSON + 明确版本）；
- 避免把业务大对象直接塞进状态，存 id 或必要字段；
- 升级前用 savepoint 做兼容性测试；
- 给算子设置稳定 uid；
- 记录状态 schema 版本，准备迁移作业。

### State Processor API

Flink 提供 State Processor API，可以离线读写 savepoint：

```java
// 2.x：State Processor API 走 DataStream 的 BATCH 模式，返回 DataStream，不再是 DataSet
StreamExecutionEnvironment env =
    StreamExecutionEnvironment.getExecutionEnvironment();
env.setRuntimeMode(RuntimeExecutionMode.BATCH);

ExistingSavepoint savepoint = SavepointReader.read(
    env, "s3://bucket/savepoint", new HashMapStateBackend());

DataStream<MyState> state = savepoint.readKeyedState(
    OperatorIdentifier.forUid("uid"), new MyStateReader());
// 转换、修正、再写回新的 savepoint
```

2.x 的签名是 `SavepointReader.read(env, path, stateBackend)` 三参形式；关键是它已经回到 DataStream API，DataSet 版本在 2.0 已移除。`MyStateReader` 是一个 `KeyedStateReaderFunction`，要在 `open` 里注册与写入时一致的状态描述符（附录 L 给出完整示例）。

适用场景：

- 状态 schema 大改，无法在线兼容；
- 修复历史脏状态；
- 把状态从一个作业迁移到另一个作业；
- 做状态审计和统计。

代价是：需要写 Java 作业，理解状态布局，且要停机或双跑。完整的读写、bootstrap、`SavepointKeyFilter`、key 重写和迁移演练见[附录 L](../appendix/L-state-migration-spi.md)。

### 2.x 的序列化坑

Flink 2.0 把 Kryo 从 2.24 升级到 5.x（FLIP-317），2.2 沿用；状态二进制格式因此有变化，POJO 序列化器对集合字段的处理也有调整。跨 1.x/2.x 恢复时，常见错误包括：

- `StateMigrationException`；
- `ClassNotFoundException`；
- `IndexOutOfBoundsException`；
- 集合字段读出 `null` 或错位。

规避方式：状态对象用 Avro/Protobuf，避免在状态 POJO 中使用复杂集合，升级前在副本上做恢复演练。

## 7.6 状态大小与倾斜

状态大小不是均匀分布的。按用户 keyBy 时，大客户可能占绝大部分状态；一个 subtask 的 RocksDB 可能远大于其他 subtask。

```text
subtask 0: 2 GB
subtask 1: 1.8 GB
subtask 2: 18 GB   ← 热点 key
subtask 3: 2.1 GB
```

应对：

- 采样 key 分布，识别热点；
- 对热点 key 加盐，拆成多个逻辑 key，查询时再聚合；
- 两阶段聚合：先局部聚合，再全局聚合；
- 对历史状态设置 TTL 或归档；
- 使用 RocksDB 并监控每个 subtask 的 state size；
- 必要时把大 key 拆成独立作业。

```java
// 盐化：把一个大 key 拆成 N 个逻辑 key
int salt = ThreadLocalRandom.current().nextInt(16);
String salted = userId + "#" + salt;
```

盐化会增加下游聚合的复杂度：查询时要合并 N 个分片。它适合“写多读少”的聚合，不适合需要精确单 key 语义的场景。

## 7.7 状态与 checkpoint 的关系

Checkpoint 保存的是**算子状态 + source offset + 元数据**。状态后端决定快照方式：

- HashMap：全量快照，序列化堆内对象；
- RocksDB：全量或增量快照，依赖 SST 文件；
- 快照期间可能产生反压，尤其是大状态全量快照；
- 增量快照减少上传量，但恢复时仍要加载大量文件。

状态越大，checkpoint 越慢，恢复越久，扩缩容越困难。这是流作业容量规划的核心约束之一。

一个粗略估算：

```text
checkpoint 上传量 ≈ 状态大小 × 变化率（全量时变化率=1）
checkpoint 时间 ≈ 上传量 / 存储带宽 + 快照遍历时间
恢复时间 ≈ 下载量 / 带宽 + 状态加载/重建时间
```

## 7.8 MiniFlink 的状态实现

MiniFlink 的 `KeyedStateBackend` 是堆内字典：

```python
backend.set_current_key("user-1")
value_state.update(10)
value_state.get()
```

支持 Value/List/Map/Reducing/Aggregating State、TTL 配置和快照/恢复。状态按 task 隔离，task 内按 key 分层。`RuntimeContext` 负责在每条记录前设置当前 key。

广播状态使用 `BroadcastMapState`，保存在算子内部，随算子快照。它没有 Flink 的 namespace、key group、RocksDB 列族和增量快照；这些差异在附录 C 中列出。对应的逐行实现见[第 30 章](30-miniflink-time-state.md)。

## 7.9 Queryable State：为什么不再推荐

Flink 1.x 提供 Queryable State：把 keyed state 通过 `asQueryableState` 暴露成可远程查询的视图，外部进程按 key 直接读算子状态，不必先写外部存储。

```java
stream.keyBy(event -> event.getUserId())
      .asQueryableState("user-profile");
```

客户端通过 `QueryableStateClient` 按 key 查询。它听起来很适合“实时特征点查”，但该特性在 **Flink 1.18 起被标记 deprecated**；2.x 仍保留，但不再建议新系统使用，社区在等待替代方案。

不再推荐的原因：

- **耦合内部状态布局**：查询依赖状态名、序列化器和 StateBackend；升级 Flink、改状态 schema 或调整拓扑都可能让客户端失效；
- **绕过作业生命周期和权限**：能访问 TaskManager 查询端口的人就能读状态，与多租户、网络隔离和审计模型冲突；
- **扩展性差**：查询请求打到持有 key 的 TaskManager，热点 key 会直接争抢任务线程；没有统一的限流、路由和熔断；
- **语义弱**：只能按 key 读当前值，不能做范围查询、join、聚合，也没有跨算子的快照隔离；
- **覆盖面窄**：主要支持 keyed state 的一部分，SQL/Table 作业基本用不上；
- **运维复杂**：需要额外的代理/服务端口、服务发现和网络策略。

替代方案：

| 需求 | 推荐 |
|---|---|
| 外部低延迟点查 | Redis/HBase 等外部 KV，由 Flink 写出 |
| 结果集查询 | 物化表、OLAP、湖表 |
| 维表关联 | lookup join / Async I/O（第 12 章） |
| 调试与审计 | metrics、REST、日志、State Processor API 离线导出 |
| 在线特征 | 特征存储/在线 KV，而不是直接读算子状态 |

如果必须维护 1.x 的 Queryable State 老作业：限制网络访问、只读、固定版本、监控查询延迟，并在升级 2.x 前规划迁移。它适合作为遗留系统的兼容手段，不适合作为新架构的查询层。

## 7.10 状态设计检查清单

- [ ] 这个状态是 keyed 还是 operator？key 的基数多大？
- [ ] 状态对象是否稳定可序列化？是否避免 Kryo 兜底？
- [ ] 状态是否有 TTL 或显式清理？
- [ ] 状态大小是否按峰值流量估算过？
- [ ] 是否存在热点 key？盐化/两阶段聚合是否可行？
- [ ] StateBackend 选型是否匹配状态规模和延迟要求？
- [ ] uid 是否稳定？升级/扩缩容演练是否做过？
- [ ] checkpoint 大小和恢复时间是否在 SLO 内？
- [ ] 是否需要外部按 key 查询？如果是，用外部 KV/物化表，而不是 deprecated 的 Queryable State。

## 本章小结

状态是流作业的“数据库”。选后端、设 TTL、管 schema、防倾斜，比会写 `ValueState` 更重要。状态设计得好，checkpoint 快、恢复快、扩缩容顺；设计得差，作业会在流量增长后突然不可维护。

下一章进入 checkpoint 与 exactly-once：屏障如何划出一致切面，状态后端如何快照，source/sink 如何协作实现端到端语义。
