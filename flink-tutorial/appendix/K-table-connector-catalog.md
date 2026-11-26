# 附录 K Flink Table 连接器与 Catalog 开发手册

第 10 章讲的是 DataStream 层的 Source/Sink V2：以“记录流”为中心，解决分片、位点、watermark 和提交协议。Table/SQL 层的连接器是另一套抽象：它以“表”为中心，除了读写数据，还要描述 schema、changelog、元数据列、分区裁剪和谓词下推。很多内部系统只提供 HTTP/RPC 接口，却希望被 SQL 直接查询；这时需要写的不是 `SourceFunction`，而是一个 `DynamicTableFactory`。

本附录以 Flink 2.3 的 Table API 为基线。Table 连接器接口在 1.x 与 2.x 之间有过多次演进（尤其是 Sink V2、`SupportsReadingMetadata`、CatalogStore），落地时请以所用发行版的 Javadoc 和 `flink-table-common` 源码为准。

```text
本附录的位置
  第 10 章        DataStream Source/Sink V2、schema 契约
  第 14 章        Table API/SQL、Catalog 使用
  第 16 章        SQL 算子状态与 upsert materializer
  附录 H          Table/SQL 源码地图
  本附录          Table 连接器与 Catalog 的开发侧实现
```

## K.1 两类连接器的分工

先分清三个概念，否则很容易在错误的层次上写代码：

| 层次 | 抽象 | 你实现什么 | 典型入口 |
|---|---|---|---|
| 记录流 | `Source` / `Sink`（V2） | 分片、reader、writer、committer | `flink-connector-base` |
| 表连接器 | `DynamicTableSource` / `DynamicTableSink` | schema、changelog、下推、元数据 | `flink-table-common` |
| 格式 | `DecodingFormat` / `EncodingFormat` | 字节 ↔ `RowData` | `flink-table-common` |

一个 Table 连接器通常组合三者：

```text
CREATE TABLE t (...) WITH ('connector'='my', 'format'='json', ...)
        │
        ▼
DynamicTableFactory（解析 WITH 选项）
        │
        ├── DynamicTableSource ──► Source V2 ──► 字节流
        │        └── DecodingFormat（可选）
        └── DynamicTableSink   ──► Sink V2   ──► 字节流
                 └── EncodingFormat（可选）
```

什么时候值得自己写 Table 连接器：

- 内部存储或协议没有官方连接器，但业务希望用 SQL 访问；
- 需要把分区裁剪、列裁剪、谓词、limit 下推到远端，否则全表扫描不可接受；
- 需要把远端的分区、offset、版本号等暴露成元数据列；
- 需要 changelog/upsert 语义，而不只是 append；
- 需要统一 Catalog 元数据，让多个引擎共享表定义。

如果只是“把一批数据读进来跑一次”，优先用 DataStream Source V2 + `fromSource`，不要为了 SQL 而写 Table 连接器。Table 连接器的成本主要在**计划期契约**：下推、投影、元数据、changelog，任何一项做错都会产生“能跑但结果不对”的问题。

## K.2 工厂发现与 DDL 解析

### K.2.1 工厂接口

Table 连接器的入口是 `DynamicTableFactory`。它通过 `factoryIdentifier()` 与 DDL 里的 `'connector'` 值匹配：

```java
public final class MyTableFactory implements DynamicTableFactory {

    public static final ConfigOption<String> ENDPOINT =
        ConfigOptions.key("endpoint")
            .stringType()
            .noDefaultValue()
            .withDescription("远端服务地址，例如 https://api.internal");

    public static final ConfigOption<Integer> BATCH_SIZE =
        ConfigOptions.key("batch-size")
            .intType()
            .defaultValue(1000)
            .withDescription("单次拉取的最大记录数");

    @Override
    public String factoryIdentifier() {
        return "my-connector";
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
        return new MyTableSource(
            options.get(ENDPOINT),
            options.get(BATCH_SIZE),
            context.getPhysicalRowDataType(),
            context.getConfiguration());
    }

    @Override
    public DynamicTableSink createDynamicTableSink(Context context) {
        FactoryUtil.TableFactoryHelper helper =
            FactoryUtil.createTableFactoryHelper(this, context);
        ReadableConfig options = helper.getOptions();
        helper.validate();
        return new MyTableSink(
            options.get(ENDPOINT),
            context.getPhysicalRowDataType());
    }
}
```

几个容易忽略的点：

- `requiredOptions()` 只放**真正必填**的选项。把可选选项误列为必填，会让用户被迫写无意义的值；
- `helper.validate()` 会检查未知选项。连接器通常要 `validateExcept("format")` 或允许 `properties.*` 前缀，否则合法的格式参数会被判为未知；
- `noDefaultValue()` 的选项在缺失时由 `validate()` 报错，不要在构造函数里再用一个“魔法默认值”兜底；
- 选项名一旦发布就要保持稳定，改名等于破坏所有已有 DDL。需要兼容时同时接受旧名并给出弃用日志。

### K.2.2 服务发现

工厂必须注册到：

```text
src/main/resources/META-INF/services/org.apache.flink.table.factories.Factory
```

内容是实现类的全限定名：

```text
com.acme.flink.table.MyTableFactory
```

Flink 通过 `FactoryUtil.discoverFactory(classLoader, DynamicTableFactory.class, "my-connector")` 查找。常见故障：

- 文件路径或文件名写错，SQL 报 “Could not find any factory for identifier 'my-connector'”；
- 多个 jar 各自带 service 文件，打包时后者覆盖前者（见 K.9 的 shade 配置）；
- 连接器 jar 没有放进 `lib/`、`--jar`、`ADD JAR` 或 K8s artifacts；
- 类初始化抛异常，被包装成“factory not found”，真实原因在 caused by 里。

### K.2.3 DDL 到选项的映射

```sql
CREATE TABLE my_source (
  id BIGINT,
  name STRING,
  ts TIMESTAMP(3),
  WATERMARK FOR ts AS ts - INTERVAL '5' SECOND
) WITH (
  'connector' = 'my-connector',
  'endpoint' = 'https://api.internal',
  'batch-size' = '500',
  'format' = 'json'
);
```

工厂拿到的是 `CatalogTable`：schema、watermark、分区键、注释和 `WITH` 选项都在里面。不要在工厂里重新解析 SQL 字符串；DDL 已经被 parser 和 validator 处理过，工厂只消费结构化结果。

## K.3 DynamicTableSource：扫描与查找

Table Source 有两种形态，选择错误会导致计划期就失败：

| 形态 | 接口 | 适用 | 运行时 |
|---|---|---|---|
| 扫描 | `ScanTableSource` | 全表/分区/增量读取 | `Source` V2 或 `InputFormat` |
| 查找 | `LookupTableSource` | 按 key 点查维表 | `TableFunction` / `AsyncTableFunction` |

### K.3.1 ScanTableSource

```java
public final class MyTableSource
        implements ScanTableSource, SupportsProjectionPushdown,
                   SupportsFilterPushdown, SupportsLimitPushdown {

    private final DataType producedDataType;
    private final List<ResolvedExpression> filters;
    private final Long limit;

    @Override
    public ChangelogMode getChangelogMode() {
        // 只追加的源；如果有更新/删除，要返回对应的 RowKind 集合
        return ChangelogMode.insertOnly();
    }

    @Override
    public ScanRuntimeProvider getScanRuntimeProvider(ScanContext context) {
        DataStructureConverter converter =
            context.createDataStructureConverter(producedDataType);
        MySource source = new MySource(endpoint, filters, limit, converter);
        return SourceProvider.of(source);
    }

    @Override
    public DynamicTableSource copy() {
        return new MyTableSource(producedDataType, filters, limit);
    }

    @Override
    public String asSummaryString() {
        return "my-connector";
    }
}
```

`ScanContext.createDataStructureConverter(...)` 把 `RowData` 转成内部数据结构；如果连接器直接产出 `RowData`，可以让转换器为恒等。不要自己缓存 converter 的实例跨 subtask 共享。

`ChangelogMode` 是计划期的硬约束：

- `insertOnly()`：只有 `+I`，下游可以是 append sink；
- `ChangelogMode.newBuilder().addContainedKind(RowKind.UPDATE_BEFORE)...`：产生回撤/更新；
- Source 声明了更新，但实际只发 `+I`，通常不会报错，却会让下游的 upsert 逻辑失效；
- Source 只声明 `+I`，实际发 `-U/+U`，运行期可能抛异常或产生错误结果。

### K.3.2 LookupTableSource

维表点查实现 `LookupTableSource`，返回 `LookupRuntimeProvider`：

```java
public final class MyLookupSource implements LookupTableSource {

    @Override
    public LookupRuntimeProvider getLookupRuntimeProvider(LookupContext context) {
        // context.getKeys() 给出关联键在物理行中的下标
        int[] keyIndexes = context.getKeys()[0];
        return TableFunctionProvider.of(new MyLookupFunction(endpoint, keyIndexes));
    }

    @Override
    public ChangelogMode getChangelogMode() {
        return ChangelogMode.insertOnly();
    }
}
```

`LookupContext.getKeys()` 是 `int[][]`：每个元素是一组关联键的字段下标。复合键要全部取到，不能只取第一列。异步点查返回 `AsyncTableFunctionProvider`，但要注意：

- 异步函数的并发由 `table.exec.async-lookup.buffer-capacity` 等参数控制；
- 超时、重试、缓存要在函数内部实现；
- 维表更新后是否需要失效缓存，是业务语义而不是连接器细节。

## K.4 能力接口与下推

下推是 Table 连接器最有价值、也最容易写错的部分。规划器的原则是：**你不声明的能力，它会自己在上面加算子；你声明了却没正确实现，它就不会再加，结果就错了。**

| 接口 | 方法 | 契约 |
|---|---|---|
| `SupportsFilterPushdown` | `applyFilters(List<ResolvedExpression>)` | 返回真正下推后仍需保留的过滤条件 |
| `SupportsProjectionPushdown` | `applyProjection(int[][], DataType)` | 接收投影字段路径与产出类型 |
| `SupportsLimitPushdown` | `applyLimit(@Nullable Long)` | 接收 limit；`null` 表示无限制 |
| `SupportsPartitionPushDown` | `listPartitions()` / `applyPartitions(List<Map<String,String>>)` | `listPartitions` 返回可选分区；`applyPartitions` 接收裁剪后的剩余分区 |
| `SupportsReadingMetadata` | `listReadableMetadata()` / `applyReadableMetadata(...)` | 暴露元数据列 |

### K.4.1 谓词下推

```java
@Override
public Result applyFilters(List<ResolvedExpression> filters) {
    List<ResolvedExpression> accepted = new ArrayList<>();
    List<ResolvedExpression> remaining = new ArrayList<>();
    for (ResolvedExpression filter : filters) {
        if (canPushDown(filter)) {
            accepted.add(filter);
        } else {
            remaining.add(filter);
        }
    }
    this.pushedFilters = accepted;
    return Result.of(remaining);
}
```

三个错误最典型：

1. **把所有条件都当成已下推**：`Result.of(Collections.emptyList())` 但远端并没有真正过滤，结果多出行；
2. **修改了入参列表**：规划器可能复用该列表，应该新建 `remaining`；
3. **只支持下推一部分表达式却返回空 remaining**：例如只支持 `=`，却把 `>` 也吞掉。

下推的表达式要在 `asSummaryString()`/`explain` 里可见，方便用 `EXPLAIN` 验证。测试时不要只看结果行数，要看计划里 `Filter` 是否还在。

### K.4.2 投影下推

```java
@Override
public void applyProjection(int[][] projectedFields, DataType producedDataType) {
    // projectedFields 的每个元素是一条字段路径：
    //   {2}      第 2 列
    //   {1, 0}   第 1 列的嵌套字段 0
    this.projectedFields = projectedFields;
    this.producedDataType = producedDataType;
}
```

注意：

- `producedDataType` 是投影后的类型，不是原始 schema；
- 空投影（例如 `COUNT(*)` 只需要行数）可能以空数组或特殊形式出现，要按版本语义处理；
- 投影后元数据列、计算列的下标会变化，不能再用原始 schema 的下标；
- 如果远端不支持列裁剪，就不要实现该接口，让规划器保留投影算子。

### K.4.3 Limit 与分区

`SupportsLimitPushdown` 只应下推**真正的行数限制**。如果远端有分页、聚合或最终排序，limit 下推可能改变语义。

分区下推的接口是 `SupportsPartitionPushDown`（注意大写 `D`），它有两个方法：

- `Optional<List<Map<String, String>>> listPartitions()`：返回 source 能提供的分区列表；`Optional.empty()` 表示不提供分区列表，planner 不应据此做分区下推/裁剪（不是“没有分区”）；
- `void applyPartitions(List<Map<String, String>> remainingPartitions)`：接收裁剪后的剩余分区。调用后 source 不能再读其他分区（包括它们的元数据），否则结果会多出行。

### K.4.4 元数据列

```java
private static final List<ReadableMetadata> READABLE = List.of(
    new ReadableMetadata("partition", DataTypes.INT()),
    new ReadableMetadata("offset", DataTypes.BIGINT()),
    new ReadableMetadata("event_time", DataTypes.TIMESTAMP_LTZ(3)));

@Override
public List<ReadableMetadata> listReadableMetadata() {
    return READABLE;
}

@Override
public void applyReadableMetadata(List<String> metadataKeys, DataType producedDataType) {
    this.metadataKeys = metadataKeys;
    this.producedDataType = producedDataType;
}
```

DDL 侧：

```sql
CREATE TABLE orders (
  id BIGINT,
  kafka_partition INT METADATA FROM 'partition' VIRTUAL,
  kafka_offset BIGINT METADATA FROM 'offset' VIRTUAL
) WITH ('connector' = 'my-connector', ...);
```

元数据列分两类：

- `VIRTUAL`：不落存储，查询时由连接器提供；
- 非 virtual：作为物理列参与读写，写入时需要连接器消费。

`applyReadableMetadata` 必须把请求的元数据字段按顺序追加到 `producedDataType` 末尾，并保证 converter 的字段下标一致。最常见的 bug 是“元数据列顺序与 `metadataKeys` 不一致”，表现为 offset 被读成 partition。

## K.5 DynamicTableSink 与 Sink V2

Sink 侧的入口是 `getSinkRuntimeProvider(Context)`：

```java
public final class MyTableSink implements DynamicTableSink {

    @Override
    public SinkRuntimeProvider getSinkRuntimeProvider(Context context) {
        DataStructureConverter converter =
            context.createDataStructureConverter(rowDataType);
        Sink<RowData> sink = new MySink(endpoint, converter);
        return SinkV2Provider.of(sink);
    }

    @Override
    public ChangelogMode getChangelogMode(ChangelogMode requestedMode) {
        // 只接受 insert-only；如果 planner 产生更新，计划期失败
        return ChangelogMode.insertOnly();
    }

    @Override
    public DynamicTableSink copy() {
        return new MyTableSink(rowDataType);
    }

    @Override
    public String asSummaryString() {
        return "my-connector";
    }
}
```

`getChangelogMode(requestedMode)` 是**协商**而不是断言：

- 返回 `insertOnly()`，planner 会确保上游不产生更新；做不到就在计划期报错；
- 返回包含 `UPDATE_BEFORE/UPDATE_AFTER` 的模式，sink 必须能消费回撤；
- 如果 sink 只支持 upsert（按主键覆盖），要声明主键并正确处理 `-U/+U`；
- 返回的模式与实际 writer 行为不一致，是最危险的“能跑但重复/丢数”来源。

### K.5.1 Sink V2 的提交协议

`SinkV2Provider` 背后是 `Sink` / `SinkWriter` / `Committer`。与 DataStream 层一样，端到端 exactly-once 依赖：

```text
write → flush → prepareCommit → checkpoint 完成 → commit
                                  │
                                  └─ 失败/取消 → abort
```

Table sink 的特殊点：

- writer 收到的是 `RowData`，不是业务 POJO；转换要在 `DataStructureConverter` 之后做；
- checkpoint 期间不能偷偷提交，提交只能发生在 `Committer.commit`；
- 如果远端没有事务，只能做幂等 upsert，并要求表有主键；
- `SupportsOverwrite` 表达“可以覆盖写分区/整表”，不要让 planner 误以为 append 可以覆盖；
- `SupportsWritingMetadata` 让 sink 接收元数据列（例如把分区写到目标路径）。

### K.5.2 与 upsert materializer 的关系

当 SQL 产生回撤而 sink 只支持 upsert 时，planner 可能插入 `SinkUpsertMaterializer`。它维护一份按主键的状态，把 changelog 规范化成 upsert。相关配置：

```yaml
table.exec.sink.upsert-materialize: AUTO        # AUTO / NONE / FORCE
table.exec.sink.upsert-materialize-strategy.type: ADAPTIVE
```

连接器开发者要明确：materializer 不是连接器的一部分，但它会改变输入顺序和状态开销。第 16 章 16.10 给出了各类 SQL 算子的状态预算；本附录只强调一点：**不要让 sink 的 changelog 声明与 materializer 的假设冲突**。

## K.6 Format 与类型转换

### K.6.1 Format 的两种接口

| 接口 | 方向 | 典型实现 |
|---|---|---|
| `DeserializationSchema` | 字节 → 对象 | `JsonDeserializationSchema` |
| `SerializationSchema` | 对象 → 字节 | `JsonSerializationSchema` |
| `DecodingFormat` | 字节 → `RowData` | `JsonFormatFactory` |
| `EncodingFormat` | `RowData` → 字节 | `JsonFormatFactory` |

Table 连接器优先使用 `DecodingFormat`/`EncodingFormat`，因为它们能拿到 `DataType` 和 schema，支持投影下推与类型校验。`DeserializationSchema` 更底层，适合复用已有实现。

```java
public final class MyFormatFactory implements FormatFactory {

    @Override
    public String factoryIdentifier() {
        return "my-json";
    }

    @Override
    public DecodingFormat<DeserializationSchema<RowData>> createDecodingFormat(
            DynamicTableFactory.Context context, ReadableConfig formatOptions) {
        return new MyDecodingFormat(formatOptions);
    }

    @Override
    public EncodingFormat<SerializationSchema<RowData>> createEncodingFormat(
            DynamicTableFactory.Context context, ReadableConfig formatOptions) {
        return new MyEncodingFormat(formatOptions);
    }
}
```

### K.6.2 类型映射

`DataType` 是逻辑类型，`RowData` 是内部行表示。常见映射：

| Flink 类型 | `RowData` 访问器 | 注意事项 |
|---|---|---|
| `BOOLEAN` | `getBoolean` | |
| `INT` | `getInt` | |
| `BIGINT` | `getLong` | |
| `DECIMAL(p,s)` | `getDecimal` | 精度/标度必须匹配 |
| `STRING` | `getString` | 注意 UTF-8 与 null |
| `TIMESTAMP(3)` | `getTimestamp` | 无时区 |
| `TIMESTAMP_LTZ(3)` | `getTimestamp` + zone | 依赖会话时区 |
| `ARRAY` | `getArray` | 元素类型 |
| `ROW` | `getRow` | 嵌套字段 |
| `RAW` | `getRawValue` | 自定义类型，序列化要稳定 |

时间类型是事故高发区：`TIMESTAMP` 与 `TIMESTAMP_LTZ` 的语义不同，`table.local-time-zone` 会改变转换结果。连接器不要在内部偷偷按 `UTC` 或本地时区解析，必须遵循传入的 `DataType` 和配置。第 14 章 14.13 有完整的时区讨论。

### K.6.3 坏数据与 schema 演进

Format 层要决定：

- 解析失败是抛异常、跳过、还是写死信？
- 未知字段是忽略、保留、还是报错？
- 字段缺失是填 `null`、默认值，还是失败？
- 类型不匹配是隐式转换还是拒绝？

生产建议：解析失败写死信并带上原始字节和错误原因；未知字段默认忽略但记录计数；类型不匹配不要静默 `null`。这些策略要写进连接器文档，并通过测试固定。

## K.7 Catalog SPI

Catalog 是元数据平面：数据库、表、视图、函数、分区。它解决的问题不是“读写数据”，而是“让多个作业、多个引擎共享同一套表定义”。

### K.7.1 核心接口

| 接口 | 职责 |
|---|---|
| `Catalog` | 数据库/表/视图/函数/分区的 CRUD 与存在性检查 |
| `CatalogFactory` | 根据 `type` 与选项创建 Catalog |
| `AbstractCatalog` | 提供默认实现，减少样板代码 |
| `CatalogDatabase` | 库名、注释、属性 |
| `CatalogTable` | schema、分区键、选项、注释 |
| `CatalogView` | 查询文本与展开后的查询 |
| `CatalogFunction` | 类名与资源 |
| `CatalogPartition` | 分区规格与属性 |
| `CatalogStore` | Catalog 配置的持久化存储（不存表元数据） |
| `CatalogStoreFactory` | 创建 `CatalogStore` |

```java
public final class MyCatalog extends AbstractCatalog {

    public MyCatalog(String name, String defaultDatabase) {
        super(name, defaultDatabase);
    }

    @Override
    public void open() throws CatalogException {
        // 建立连接、加载元数据
    }

    @Override
    public List<String> listDatabases() throws CatalogException {
        return List.of(getDefaultDatabase());
    }

    @Override
    public CatalogTable getTable(ObjectPath tablePath) throws CatalogException {
        // Flink 2.x：TableSchema/CatalogTable.of 已移除，使用 Schema + newBuilder
        return CatalogTable.newBuilder()
            .schema(
                Schema.newBuilder()
                    .column("id", DataTypes.BIGINT())
                    .column("ts", DataTypes.TIMESTAMP(3))
                    .build())
            .comment("my table")
            .partitionKeys(List.of())
            .options(Map.of("connector", "my-connector"))
            .build();
    }
}
```

### K.7.2 CatalogFactory 与注册

```java
public final class MyCatalogFactory implements CatalogFactory {

    @Override
    public String factoryIdentifier() {
        return "my-catalog";
    }

    @Override
    public Catalog createCatalog(Context context) {
        return new MyCatalog(
            context.getName(),
            context.getOptions().get("default-database"));
    }
}
```

```sql
CREATE CATALOG my_cat WITH (
  'type' = 'my-catalog',
  'default-database' = 'realtime'
);
USE CATALOG my_cat;
```

注册方式还有 Java API：

```java
tableEnv.registerCatalog("my_cat", new MyCatalog("my_cat", "realtime"));
tableEnv.useCatalog("my_cat");
```

### K.7.3 临时表、永久表与视图

| 对象 | 生命周期 | 存储 | 典型用途 |
|---|---|---|---|
| 临时表 | 会话/作业 | 内存 | 测试、临时连接 |
| 永久表 | Catalog | 元数据存储 | 生产共享 |
| 临时视图 | 会话/作业 | 内存 | 调试查询 |
| 永久视图 | Catalog | 元数据存储 | 逻辑复用、权限封装 |

临时表优先于永久表解析，容易出现“同名覆盖”。生产上建议：

- 临时表名带 `tmp_` 前缀；
- 永久表的 DDL 进 Git，临时表不进；
- 视图只暴露必要的列，作为权限与语义的边界；
- Catalog 的变更走审批和审计。

### K.7.4 常见 Catalog 实现

| Catalog | `type` | 关键选项 | 适用 |
|---|---|---|---|
| Hive | `hive` | `hive-conf-dir`、`default-database` | Hadoop 生态、离线共享 |
| JDBC | `jdbc` | `base-url`、`default-database`、`username`、`password` | 关系库元数据 |
| Paimon | `paimon` | `warehouse`、`default-database` | 流式湖仓 |
| Iceberg | `iceberg` | `catalog-type`、`uri`、`warehouse` | 湖仓 |
| 内存 | `generic_in_memory` | 无 | 测试、本地 |

Catalog 的选型要与存储格式、权限系统、跨引擎访问一起看。一个常见误区是“Catalog 只是表名映射”：实际上它还决定了 schema 演进、分区发现、权限挂载点和跨引擎一致性。

### K.7.5 CatalogStore

默认情况下，Catalog 的元数据由实现自己持久化（Hive Metastore、JDBC 库等）。`CatalogStore` 提供了另一种方式：把 **Catalog 配置**（不是表元数据）存到 Flink 管理的存储中，让 `CREATE CATALOG` 在会话重启后仍然存在。

```yaml
# 默认值：只在内存中保存 Catalog 配置，重启即丢
table.catalog-store.kind: generic_in_memory

# 文件存储：跨会话保留 Catalog 配置
table.catalog-store.kind: file
table.catalog-store.file.path: file:///var/flink/catalog-store
```

注意区分两个概念：

- `table.catalog-store.kind` 决定 **Catalog 配置**存在哪（`generic_in_memory` 默认不持久化，`file` 写 YAML）；
- `CREATE CATALOG ... WITH ('type'='...')` 里的 `type` 决定 **Catalog 实现**（Hive/JDBC/Paimon/内存等）。

生产使用前要确认：

- 重启后 Catalog 配置是否还在（`generic_in_memory` 不会）；
- 多 JobManager 是否共享同一份存储；
- 是否支持并发修改/锁；
- 是否支持视图和函数；
- 备份与恢复怎么做。

## K.8 测试与验证

Table 连接器的测试分四层：

| 层次 | 测什么 | 工具 |
|---|---|---|
| 工厂单测 | 选项解析、必填校验、默认值 | JUnit + `FactoryUtil` |
| 计划测试 | 下推、投影、limit、changelog | `TableEnvironment.explainSql` |
| 运行时测试 | 读写、序列化、元数据 | `MiniCluster` + `TableEnvironment` |
| 端到端 | checkpoint/restore、故障、并发 | Testcontainers / 真实依赖 |

计划测试的例子：

```java
TableEnvironment tEnv = TableEnvironment.create(
    EnvironmentSettings.inStreamingMode());
tEnv.executeSql("CREATE TABLE t (...) WITH ('connector'='my-connector', ...)");
String plan = tEnv.explainSql("SELECT id FROM t WHERE id > 10 LIMIT 5");
assertThat(plan).contains("MyTableSource");
// 断言 filter/limit 是否真的下推，而不是只断言“计划里有 source”
```

注意：`explainSql` 的计划文本会随版本变化，断言要尽量针对稳定片段（算子名、下推标记），不要硬编码整段字符串。

运行时测试要覆盖：

- 空表、单行、多分区；
- null、极值、时区；
- 元数据列顺序；
- changelog 模式（insert-only / retract / upsert）；
- checkpoint 后恢复、并行度变化；
- 远端超时、限流、部分失败。

## K.9 打包、类加载与版本

Table 连接器通常以独立 jar 分发，打包规则与 DataStream 连接器类似，但多了 Table 依赖：

- `flink-table-common`、`flink-table-api-java` 等用 `provided`，不要打进 fat jar；
- 不要打包 `flink-table-planner` 和 `flink-runtime`，否则与集群版本冲突；
- 第三方客户端 SDK 可以 shade，必要时 relocate 包名；
- 合并 `META-INF/services/org.apache.flink.table.factories.Factory`，不要覆盖；
- 记录连接器版本、Flink 版本、远端协议版本。

```xml
<plugin>
  <groupId>org.apache.maven.plugins</groupId>
  <artifactId>maven-shade-plugin</artifactId>
  <configuration>
    <relocations>
      <relocation>
        <pattern>com.acme.sdk</pattern>
        <shadedPattern>com.acme.flink.shaded.sdk</shadedPattern>
      </relocation>
    </relocations>
    <transformers>
      <transformer implementation="org.apache.maven.plugins.shade.resource.ServicesResourceTransformer"/>
    </transformers>
  </configuration>
</plugin>
```

类加载冲突的排查顺序：

1. `ClassNotFoundException` / `NoSuchMethodError`：先看集群 `lib/` 与作业 jar 的版本；
2. `factory not found`：看 service 文件是否被 shade 覆盖；
3. `NoSuchMethodError` 但类存在：典型的多版本共存，用 `mvn dependency:tree` 找重复；
4. `IllegalAccessError`：模块/类加载器边界，检查 `classloader.parent-first-patterns.additional`；
5. SQL Client 中 `ADD JAR` 后仍找不到：确认 jar 在会话的 classloader 中，而不是只加到了客户端。

## K.10 生产检查清单

- [ ] `factoryIdentifier()` 稳定，选项名有文档和默认值；
- [ ] `requiredOptions()`/`optionalOptions()` 准确，`validate()` 不误杀格式参数；
- [ ] service 文件打包正确，多个 jar 不互相覆盖；
- [ ] `ChangelogMode` 与真实读写行为一致；
- [ ] 谓词下推返回正确的 `remainingFilters`，不修改入参；
- [ ] 投影下推后的字段下标与 `producedDataType` 一致；
- [ ] 元数据列顺序、类型、virtual 语义正确；
- [ ] watermark 由 source 正确发出，不被连接器吞掉；
- [ ] sink 的提交协议幂等、可重试、可中止；
- [ ] 有工厂单测、计划测试、MiniCluster 运行时测试；
- [ ] checkpoint/restore、并行度变化、故障注入通过；
- [ ] 依赖用 `provided`，shade 不打包 Flink 核心；
- [ ] 凭证不写进 DDL/日志，支持 Secret 注入；
- [ ] 版本兼容矩阵和升级路径已记录。

Table 连接器的难点不在“读一个字节流”，而在计划期契约：你声明的能力会被规划器信任，声明错了不会立刻报错，而是安静地算出错误结果。把下推、元数据、changelog 和提交协议当成一等公民，连接器才能在生产里站得住。

相关章节：[第 10 章](../chapters/10-connectors.md)（DataStream 连接器与 schema 契约）、[第 14 章](../chapters/14-table-sql.md)（Table API 与 Catalog 使用）、[第 16 章](../chapters/16-sql-optimizer.md)（SQL 算子状态与 upsert materializer）、[附录 H](H-source-code-map.md)（Table/SQL 源码地图）。
