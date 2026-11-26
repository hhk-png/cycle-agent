# 附录 L 状态迁移、Savepoint 兼容与 State Processor API

第 8 章讲 checkpoint 的机制，第 9 章讲故障恢复与 rescale，第 34 章讲 1.x → 2.x 的版本变化。它们回答的是“状态怎么存、怎么恢复”；本附录回答一个更具体、也更容易在升级夜出事的问题：

> 当代码、并行度、序列化器或 Flink 版本发生变化时，旧状态还能不能被新作业正确读懂？如果不能，如何离线修复、搬迁或重建？

状态迁移的难点在于：**状态不是一份普通数据，而是“逻辑结构 + 序列化格式 + 分区布局 + 后端物理格式”四层身份的组合。** 任何一层变了，恢复都可能失败；更危险的是不失败，而是安静地读出错误结果。

```text
本附录的位置
  第 8 章    checkpoint/savepoint 机制
  第 9 章    故障恢复、rescale、状态 schema 演进
  第 34 章   1.x → 2.x 的 API 与运行时变化
  本附录     迁移规则、State Processor API、演练手册
```

## L.1 状态的四层身份

```text
逻辑结构    算子 UID、状态名、StateDescriptor、key 类型、value 类型
序列化格式  TypeSerializer、TypeSerializerSnapshot、POJO/Kryo/Avro 规则
分区布局    maxParallelism、key group、subtask 映射、operator state 重分配模式
物理格式    HashMapStateBackend / EmbeddedRocksDBStateBackend / ForSt
```

恢复时，Flink 要同时匹配这四层：

- **逻辑结构**决定“这份状态属于哪个算子、哪个状态名”；
- **序列化格式**决定“字节怎么解释成对象”；
- **分区布局**决定“哪个 key group 归哪个 subtask”；
- **物理格式**决定“快照文件怎么读”。

最常见的升级事故不是“启动报错”，而是：

- UID 变了，状态被当成“新算子”的初始状态，结果从零开始；
- 序列化器兼容但字段语义变了（毫秒改成秒），结果静默偏移；
- `allowNonRestoredState` 把找不到的状态悄悄丢掉，作业能跑但少了历史；
- maxParallelism 不一致，恢复时 key group 对不上；
- 从 RocksDB 换到 HashMap，快照能读但本地恢复/性能假设失效。

## L.2 算子 UID：状态的身份证

Flink 用 **operator UID** 把 savepoint 里的状态映射回算子。`name()` 只是展示名，不参与映射；`uid()` 才是稳定身份。

```java
DataStream<Order> orders = env
    .fromSource(source, watermarkStrategy, "orders")
    .uid("orders-source")
    .map(new ParseOrder())
    .uid("parse-order")
    .keyBy(Order::getUserId)
    .window(TumblingEventTimeWindows.of(Duration.ofMinutes(5)))
    .aggregate(new SumAmount())
    .uid("sum-amount")
    .sinkTo(sink)
    .uid("orders-sink");
```

规则：

- 每个有状态的算子都必须显式 `uid()`；
- UID 一旦上线就不要改，改名等于换了一个算子；
- 不要用自增序号、随机数、时间戳生成 UID；
- 多个作业共享同一份 savepoint 时，UID 不能冲突；
- 算子链化/去链化不改变 UID，但会改变 Task 边界和恢复粒度；
- 调整拓扑顺序、拆合算子时，旧 UID 的状态需要显式迁移或丢弃。

SQL 作业的算子 UID 通常由 planner 生成，和查询计划绑定。改 SQL、换优化器版本、调整 join 顺序都可能改变计划，从而改变 UID。升级 SQL 作业时：

1. 用 `EXPLAIN` 对比新旧计划；
2. 在副本环境用生产 savepoint 恢复；
3. 观察是否有 “Cannot map savepoint state” 或状态从零开始；
4. 必要时用 State Processor API 读取旧状态并按新 UID 重写。

部分版本提供显式指定 SQL 算子 UID/状态恢复策略的配置，但键名和行为随版本变化，落地时以发行版文档为准。不要假设“SQL 作业不需要管 UID”。

## L.3 Savepoint 与 checkpoint 的差别

| 维度 | Checkpoint | Savepoint |
|---|---|---|
| 触发 | 周期性/手动 | 手动/运维操作 |
| 生命周期 | Flink 管理，可被新 checkpoint 覆盖 | 用户管理，长期保留 |
| 格式 | 运行时快照，偏内部 | 规范化、可移植 |
| 用途 | 故障恢复 | 升级、迁移、备份、扩缩容 |
| 语义 | 自动清理 | 显式创建/删除 |
| 兼容性 | 通常只保证同版本/邻近版本 | 设计上跨版本更友好，但不保证 |

几个关键配置：

```yaml
# checkpoint 目录
state.checkpoints.dir: s3://bucket/flink/checkpoints

# savepoint 默认目录
state.savepoints.dir: s3://bucket/flink/savepoints

# 外部化 checkpoint 保留策略
execution.checkpointing.externalized-checkpoint-retention: RETAIN_ON_CANCELLATION

# 保留多少个 checkpoint
state.checkpoints.num-retained: 3
```

`RETAIN_ON_CANCELLATION` 适合“取消作业后还要保留现场”的场景；不需要时不要开，否则存储会持续增长。

Savepoint 的创建：

```bash
# 触发 savepoint 并继续运行
flink savepoint <jobId> s3://bucket/flink/savepoints

# 停止作业并做 savepoint
flink stop --savepointPath s3://bucket/flink/savepoints <jobId>

# 从 savepoint 恢复
flink run -s s3://bucket/flink/savepoints/savepoint-xxxx job.jar
```

2.x 中 `flink stop` 的行为与 1.x 有差异（例如是否等待 drain），以发行版 CLI 为准。生产上建议把 savepoint 的触发、校验、保留写进 runbook，而不是临时敲命令。

## L.4 Savepoint 兼容性规则

### L.4.1 必须满足的条件

| 条件 | 说明 | 不满足的后果 |
|---|---|---|
| UID 一致 | 新旧作业的算子 UID 能对应 | 状态找不到，从零开始或报错 |
| maxParallelism 一致 | 默认 128，创建后不可随意改 | key group 映射失败 |
| 状态名/描述符一致 | `ValueStateDescriptor("x", ...)` 的名字和类型 | 状态读不到或类型错误 |
| 序列化器兼容 | `TypeSerializerSnapshot` 能恢复 | `Incompatible serializer` |
| 状态后端可用 | RocksDB 原生库/ForSt 支持 | 启动失败 |
| 算子状态重分配模式明确 | `SPLIT_DISTRIBUTE`/`UNION`/`BROADCAST` | 状态丢失或重复 |
| key 类型稳定 | key 的序列化与 hash 不变 | 分区错位、状态张冠李戴 |

### L.4.2 maxParallelism 与 key group

```text
key → hash → key group（0..maxParallelism-1）→ subtask
subtask = keyGroup * parallelism / maxParallelism
```

默认 `maxParallelism = 128`。它决定了 key group 的数量，也决定了并行度的上限：

- 新并行度不能超过 `maxParallelism`；
- 修改 `maxParallelism` 通常需要 State Processor API 重写 savepoint；
- 并行度可以在 `1..maxParallelism` 之间 rescale，Flink 按 key group 重新分配；
- 增大 `maxParallelism` 会让每个 key group 变小，恢复/扩缩容更灵活，但元数据更多；
- 减小 `maxParallelism` 可能违反“一个 key group 不能拆给多个 subtask”的约束。

显式设置：

```java
env.setMaxParallelism(256);          // 作业级
stream.setMaxParallelism(256);       // 算子级
```

不要在运行中修改。创建 savepoint 时使用的 maxParallelism 会写进快照；恢复时必须匹配，除非走离线重写。

### L.4.3 算子状态的重分配模式

重分配模式**不是** `ListStateDescriptor` 的 setter，而是由注册方法决定：

```java
ListStateDescriptor<Long> descriptor =
    new ListStateDescriptor<>("offsets", Long.class);

// 每个 subtask 均分元素
context.getOperatorStateStore().getListState(descriptor);
// 每个 subtask 拿到全量
context.getOperatorStateStore().getUnionListState(descriptor);
// 每个 subtask 一份相同副本
context.getOperatorStateStore().getBroadcastState(broadcastDescriptor);
```

| 模式 | 恢复/扩缩容行为 | 适用 |
|---|---|---|
| `SPLIT_DISTRIBUTE` | 元素均分到新 subtask | 可分片的状态（offset 列表） |
| `UNION` | 每个 subtask 拿到全量 | 小规模全局状态 |
| `BROADCAST` | 每个 subtask 一份相同副本 | 广播规则/配置 |

DataStream API V2 用 `StateDeclaration` 的 `NONE`/`REDISTRIBUTABLE`/`IDENTICAL` 描述同类语义。不要混用旧新 API 的概念；升级时逐个算子确认。

### L.4.4 序列化器兼容

Flink 用 `TypeSerializerSnapshot` 判断旧字节能否被新序列化器读取。原则：

- 优先 POJO/Avro/Protobuf，避免 Kryo 兜底；
- 开发期 `env.getConfig().disableGenericTypes()`，让隐式 Kryo 变成显式失败；
- 状态对象只放必要字段，避免大对象和复杂集合；
- 字段增删改要写兼容性测试；
- 自定义 `TypeSerializer` 必须实现并版本化 `TypeSerializerSnapshot`；
- 不要依赖 Java 原生序列化做长期状态格式。

一个最小的自定义序列化器快照：

```java
public final class MyTypeSerializerSnapshot
        extends SimpleTypeSerializerSnapshot<MyType> {

    public MyTypeSerializerSnapshot() {
        super(MyTypeSerializer::new);
    }

    @Override
    public int getCurrentVersion() {
        return 1;
    }
}
```

如果新旧格式差异较大，实现 `TypeSerializerSnapshot` 的 `resolveSchemaCompatibility`，在兼容时返回 `Compatible`，否则返回 `Incompatible` 并给出原因。不要用“永远兼容”掩盖真实的不兼容。

## L.5 状态 schema 演进

### L.5.1 常见变更矩阵

| 变更 | 兼容性 | 建议 |
|---|---|---|
| 新增可选字段 | 通常兼容 | 用默认值/可空类型 |
| 删除字段 | 视序列化器 | 保留字段并标记废弃 |
| 字段顺序变化 | POJO 可能不兼容 | 不要依赖字段顺序 |
| 字段类型变更 | 通常不兼容 | 新字段 + 迁移逻辑 |
| 类名/包名变化 | Kryo 高风险 | 避免；或写自定义序列化器 |
| key 类型变化 | 不兼容 | 新作业 + 重放/SPI |
| 状态名变化 | 不兼容 | 保持状态名稳定 |
| `StateTtlConfig` 变化 | 格式兼容，语义变化 | 评估过期行为 |

### L.5.2 POJO 规则

Flink 的 POJO 序列化要求：

- 类是 public、有 public 无参构造；
- 字段有 public getter/setter 或 public 字段；
- 字段类型可序列化；
- 不依赖 `Serializable` 的默认 Java 序列化。

POJO 序列化器按字段名匹配，新增字段通常能读旧数据（新字段为默认值），删除字段通常能忽略。但字段类型变化、字段名变化、嵌套类变化都可能不兼容。

### L.5.3 Kryo 的风险

Kryo 是兜底序列化器，对类结构变化非常敏感：

- 字段增删、类型变化、类名变化都可能读错；
- 不同 Kryo 版本（Flink 2.0 从 2.24 升到 5.x，FLIP-317）可能不兼容；
- 注册顺序变化会影响序列化 ID；
- 匿名类/内部类/Lambda 不适合做长期状态。

生产状态不要依赖 Kryo。用 `disableGenericTypes()` 在开发期暴露问题，用 POJO/Avro 显式建模。

### L.5.4 状态 TTL 的迁移

`StateTtlConfig` 改变的是“什么时候把状态视为过期”，不是序列化格式。但：

- 从“无 TTL”切到“有 TTL”，旧状态会按新规则过期；
- 从“有 TTL”切到“无 TTL”，旧状态不再过期，可能无限增长；
- TTL 的可见性（`NeverReturnExpired` 等）影响读语义；
- TTL 元数据本身占空间，开启后状态会变大。

TTL 变更要当成业务语义变更，做回归和对账，而不是当成纯配置调整。

## L.6 并行度变化与 rescale

### L.6.1 可以做什么

- 在 `1..maxParallelism` 范围内改变并行度；
- keyed state 按 key group 自动重分配；
- operator state 按声明模式重分配；
- source/sink 的分区/offset 状态按连接器实现重分配。

### L.6.2 不能做什么

- 超过 `maxParallelism` 的并行度；
- 在运行中直接改 `maxParallelism`；
- 期望所有 operator state 都能自动均分（`UNION`/`BROADCAST` 语义不同）；
- 期望外部副作用自动对齐（sink 事务、下游缓存、告警）。

### L.6.3 操作顺序

```text
1. 冻结写入或准备双跑
2. 触发 savepoint 并校验
3. 用新并行度从 savepoint 恢复
4. 对比状态大小、key 分布、输出
5. 切换上游/下游
6. 保留旧作业与旧 savepoint 以便回滚
```

不要直接改并行度重启生产作业，除非你确认：

- 状态可以 rescale；
- 下游能承受新并发；
- checkpoint 存储带宽足够；
- 有回滚路径。

## L.7 State Processor API

State Processor API 允许用 DataStream API **离线**读取、修改和创建 savepoint。它不启动原作业，也不依赖原作业代码的完整拓扑，适合：

- 从数仓/批处理结果**引导（bootstrap）**初始状态；
- 修复坏状态（删除某些 key、重置某些值）；
- 改变 key（re-key）；
- 迁移 schema/UID/并行度；
- 拆分或合并作业；
- 从旧版本 savepoint 生成新版本可读的快照。

### L.7.1 读取 savepoint

```java
StreamExecutionEnvironment env =
    StreamExecutionEnvironment.getExecutionEnvironment();
// State Processor API 在 2.x 走 DataStream 的 BATCH 模式
env.setRuntimeMode(RuntimeExecutionMode.BATCH);

ExistingSavepoint savepoint = SavepointReader.read(
    env,
    "s3://bucket/flink/savepoints/savepoint-123",
    new HashMapStateBackend());

DataStream<OrderState> states = savepoint.readKeyedState(
    OperatorIdentifier.forUid("sum-amount"),
    new KeyedStateReaderFunction<Long, OrderState>() {

        private transient ValueState<OrderState> state;

        @Override
        public void open(OpenContext ctx) {
            // 2.x 的 KeyedStateReaderFunction 在 open 里注册状态描述符
            state = getRuntimeContext().getState(
                new ValueStateDescriptor<>("order-state", OrderState.class));
        }

        @Override
        public void readKey(Long key, Context ctx, Collector<OrderState> out)
                throws Exception {
            OrderState value = state.value();
            if (value != null) {
                out.collect(value);
            }
        }
    });
```

要点：

- `readKeyedState` 的第一个参数是 `OperatorIdentifier`（通常用 `OperatorIdentifier.forUid(uid)`），不是算子名；2.0 起旧的 `String uid` 重载已移除；
- 状态描述符要在 `open` 里注册，名字和类型必须与写入时一致；
- `Context` 提供 keyed state store 和 timer service；
- 读取结果是 `DataStream`，可以继续用批/流算子处理；
- 状态后端要能加载该 savepoint（RocksDB 需要原生库）。

读取 operator state（list/union/broadcast 各有专用入口）：

```java
DataStream<Long> offsets = savepoint.readListState(
    OperatorIdentifier.forUid("kafka-source"),
    new ListStateDescriptor<>("offsets", Long.class));
```

`ExistingSavepoint` 还提供 `readUnionState`、`readBroadcastState`；键控状态用 `readKeyedState`。2.x 新增了 `SavepointKeyFilter`，可以按精确 key/范围/自定义条件过滤，并在读取时做 key-group split pruning，适合只迁移部分 key 的场景。

### L.7.2 写入/引导 savepoint

```java
DataStream<OrderState> bootstrap = env.fromCollection(initialStates);

BootstrapTransformation<OrderState> transformation = OperatorTransformation
    .bootstrapWith(bootstrap)
    .keyBy(OrderState::getUserId)
    .transform(new KeyedStateBootstrapFunction<Long, OrderState>() {
        @Override
        public void processElement(
                OrderState value, Context ctx, Collector<Object> out)
                throws Exception {
            ValueState<OrderState> state = ctx.getKeyedStateStore().getState(
                new ValueStateDescriptor<>("order-state", OrderState.class));
            state.update(value);
        }
    });

SavepointWriter
    .newSavepoint(env, new HashMapStateBackend(), 128)
    // 2.x 需要显式 OperatorIdentifier；目标作业里的 uid 必须与这里一致
    .withOperator(OperatorIdentifier.forUid("order-state"), transformation)
    .write("s3://bucket/flink/savepoints/bootstrap");
```

要点：

- `newSavepoint(env, backend, maxParallelism)` 的 `maxParallelism` 要与目标作业一致，`backend` 要能承载目标状态；
- `keyBy` 的 key 必须与目标算子的 key 语义一致；
- bootstrap 的并行度可以与目标不同，但状态会按 key group 重新分配；
- 生成的 savepoint 可以直接用于恢复；
- 定时器、广播状态、operator state 的 bootstrap 能力随版本不同，使用前要验证。

### L.7.3 典型用途

| 用途 | 做法 | 风险 |
|---|---|---|
| 冷启动状态 | 从数仓/批结果 bootstrap | key 语义不一致 |
| 修复坏 key | 读取 → 过滤/重置 → 重写 | 漏掉关联状态 |
| 改 key | 读取旧 key → 用新 key bootstrap | 分区变化、下游需重算 |
| 拆分作业 | 读一个 savepoint，写多个 | UID/状态归属要重新设计 |
| 合并作业 | 读多个 savepoint，合并写一个 | 状态名冲突、key 冲突 |
| 跨版本迁移 | 旧版本读 → 新版本写 | 序列化器/后端限制 |

### L.7.4 限制与注意事项

- State Processor API 在 2.x 走 DataStream 的 **BATCH** 执行模式（DataSet API 已在 2.0 移除），作业要显式 `setRuntimeMode(BATCH)`；
- State Processor API 读的是 **savepoint**，不是任意 checkpoint 文件；先确保 savepoint 可用；
- 需要能加载原状态后端；RocksDB savepoint 读取依赖本地 RocksDB 库；
- `maxParallelism` 只能在重写时改变，不能通过普通恢复改变；
- 状态的序列化器必须能读旧格式；
- 定时器的读取/写入支持随版本演进，关键作业要专门验证；
- 重写后的 savepoint 要在一个隔离环境先恢复演练，再用于生产；
- 不要用 State Processor API 绕过 schema 兼容性检查；它只是给你一个离线修复的机会。

## L.8 1.x → 2.x 的状态迁移

Flink 2.x 不是 1.x 的二进制兼容升级。状态迁移要同时处理 API、序列化和运行时：

| 变化 | 影响 | 应对 |
|---|---|---|
| Java 17 默认 | 依赖/字节码 | 升级编译目标与依赖 |
| `SourceFunction`/`SinkFunction` V1 移除 | 自定义连接器 | 迁移到 Source/Sink V2 |
| DataSet API 移除 | 批处理代码 | 迁移到 DataStream/Table |
| Scala API 移除 | Scala 作业 | 迁移到 Java/Python |
| Per-job 模式移除 | 部署脚本 | 改 Application 模式 |
| Kryo 2.24 → 5.x | 泛型状态 | 验证/迁移；优先 POJO |
| RocksDB 版本变化 | 状态二进制 | 用 savepoint 迁移，不直接复用本地盘 |
| ForSt 实验 | 状态后端 | 生产仍以 RocksDB 为主 |

跨大版本恢复的可行路径：

1. **1.20 savepoint → 2.x 恢复**：先在 1.20 生成规范 savepoint，再在 2.x 用兼容的连接器/序列化器恢复；
2. **State Processor API 翻译**：读旧 savepoint，按新 UID/schema 重写；
3. **从源端重放**：最稳妥但成本最高，适合状态可重建的场景；
4. **双写/影子**：新旧作业并行一段时间，对账后切换。

不要假设“savepoint 是万能的”。1.x 与 2.x 的状态不保证兼容；升级前必须在副本上做完整恢复演练。

## L.9 迁移演练手册

### L.9.1 迁移前清单

- [ ] 作业清单：UID、状态名、状态大小、maxParallelism、状态后端；
- [ ] 连接器清单：版本、协议、offset/事务语义；
- [ ] schema 清单：事件、状态、Catalog 表结构；
- [ ] 依赖清单：Flink 版本、JDK、连接器、UDF 依赖；
- [ ] SLO：RPO/RTO、可接受的重放窗口；
- [ ] 回滚方案：旧镜像、旧 savepoint、旧集群。

### L.9.2 演练步骤

```text
1. 在隔离环境部署新版本集群
2. 用生产 savepoint 恢复（只读/影子 sink）
3. 对比状态大小、key 数、输出摘要
4. 注入故障：kill TM、重启 JM、限流存储
5. 测 rescale：N → N±k
6. 测回滚：旧版本从新 savepoint 恢复
7. 记录 RTO、重放量、人工步骤
```

### L.9.3 切换与回滚

```text
切换：
  停旧作业（savepoint）→ 启新作业（从 savepoint）→ 校验 → 切流量

回滚：
  停新作业 → 用旧镜像 + 旧 savepoint 恢复 → 校验 → 切回
```

回滚的关键是**旧 savepoint 仍然可用**。切换后不要立刻删除旧快照和旧镜像，至少保留一个观察窗口。

## L.10 常见故障与定位

| 现象 | 常见原因 | 处理 |
|---|---|---|
| `Cannot map savepoint state` | UID/拓扑变化 | 恢复 UID 或用 SPI 重写 |
| `Incompatible serializer` | 类型/序列化器变化 | 兼容快照或迁移 |
| `Max parallelism mismatch` | maxParallelism 改变 | 用 SPI 重写 |
| 状态从零开始 | UID 不匹配 + `allowNonRestoredState` | 检查 UID，禁用静默丢弃 |
| 恢复后结果偏移 | 单位/时区/字段语义变化 | 对账、回放、修正转换 |
| 定时器丢失 | 版本/序列化/恢复路径 | 验证 timer 快照，必要时重建 |
| RocksDB 启动失败 | 原生库/本地目录/版本 | 检查依赖与路径 |
| SQL 作业恢复失败 | 计划变化导致 UID 变化 | 对比计划，SPI 迁移 |

`allowNonRestoredState` 是一把双刃剑：

```bash
flink run -s <savepoint> --allowNonRestoredState job.jar
```

它让“找不到对应算子的状态”被跳过，适合“确实删除了某个算子”的场景；如果不确定，它会掩盖状态丢失。生产上默认不要开，开启要有明确的状态清单和验证。

## L.11 检查清单

- [ ] 每个有状态算子都有稳定 UID；
- [ ] maxParallelism 已显式规划并记录；
- [ ] 状态名、描述符、key 类型稳定；
- [ ] 序列化器有兼容性测试，避免 Kryo 兜底；
- [ ] 状态 schema 变更走兼容性矩阵；
- [ ] savepoint、checkpoint、外部化保留策略已配置；
- [ ] 扩缩容/迁移有演练记录和回滚方案；
- [ ] State Processor API 的读写脚本有版本和校验；
- [ ] 1.x → 2.x 的 API/依赖/连接器迁移已列出；
- [ ] `allowNonRestoredState` 默认关闭，开启有审批；
- [ ] 迁移后做对账，不只看作业是否 RUNNING。

状态迁移的本质不是“把文件搬过去”，而是“证明新作业对旧状态的解释与旧作业一致”。UID、序列化、key group、后端格式，任何一项没有验证，都可能让作业看起来正常、结果却已经偏了。

相关章节：[第 8 章](../chapters/08-checkpoint.md)（checkpoint/savepoint）、[第 9 章](../chapters/09-recovery.md)（恢复与状态演进）、[第 34 章](../chapters/34-flink2-deep-dive.md)（1.x → 2.x 迁移）、[附录 B](B-config-tuning.md)（配置与迁移提示）、[附录 H](H-source-code-map.md)（状态与 checkpoint 源码地图）。
