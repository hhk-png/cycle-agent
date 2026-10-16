# 第 30 章 集成与生态

这一章解决什么问题:前面二十九章都在讲 ClickHouse **内部**怎么工作。但一个真实系统里,ClickHouse 很少是孤岛——上游有消息队列和业务库,下游有 BI 和告警,中间可能还有 ETL 和特征工程。这些连接处才是项目最容易翻车的地方:数据没进来、进来了重复了、出去了被 BI 拖垮。这一章不按产品列表组织,而是按**数据怎么进来、怎么出去、怎么被消费**三条线来讲,最后给一份"什么该交给 ClickHouse、什么不该"的取舍清单。

## 30.1 三条线

把生态问题拆成三段,每一段的失败模式都不一样:

- **进来**要解决的是"不丢、不重、可重放"。这一段的工具是消息队列引擎、数据库引擎、对象存储引擎、表函数、**字典**和**命名集合**。
- **出去**要解决的是"接口稳定、驱动正确、也别只出不进"。这一段的工具是 HTTP 接口、JDBC/ODBC 驱动、`INSERT INTO FUNCTION` 的 Sink 方向、命令行工具。
- **被消费**要解决的是"建模和查询模式对得上"。这一段的工具是 dbt、BI、可观测性栈,以及访问方式的取舍。

搞清楚一条数据流分别落在这三段里的哪一段,比记住一堆连接器名字有用得多。

## 30.2 数据进来:消息队列

### Kafka 引擎 + 物化视图的经典模式

ClickHouse 有一个 `Kafka` 表引擎,它把 Kafka topic 当成一张"只能读一次"的表。关键在于理解它**不是**一个持久表:

```sql
CREATE TABLE kafka_events (
  ts DateTime, user_id UInt64, action String
) ENGINE = Kafka SETTINGS
  kafka_broker_list = 'broker1:9092',
  kafka_topic_list = 'events',
  kafka_group_name = 'ch_events',
  kafka_format = 'JSONEachRow';
```

对这张表 `SELECT`,ClickHouse 会从 Kafka 拉一批消息、**推进 consumer offset**、然后返回给你。也就是说,**数据流过一次就没了**——如果你只是手工 `SELECT` 它,下次再 `SELECT` 就拿到新 offset 之后的消息,中间那次读到的数据如果不落盘就永远丢了。

所以生产上**必须**配物化视图,把流过来的数据落进真正的表:

```sql
CREATE TABLE events (ts DateTime, user_id UInt64, action String)
ENGINE = MergeTree ORDER BY (ts, user_id);

CREATE MATERIALIZED VIEW mv_events TO events AS
SELECT ts, user_id, action FROM kafka_events;
```

物化视图把 `kafka_events` 当源,每当 Kafka 引擎推进一段数据,就把这段数据 INSERT 进 `events`。这才是"Kafka → ClickHouse"的标准形态。少配 MV,后果就是数据只被消费一次就丢,而且你多半过了很久才发现。

### offset 管理与重复消费

**默认情况下,offset 由 Kafka 自己管**——ClickHouse 就是一个普通的消费者组成员,提交到消费组上,重启后从 Kafka 那里续。这不是细节:把 offset 存在 Keeper 里是**另一条路,而且是实验性的、默认关闭**的(会话开关 `allow_experimental_kafka_offsets_storage_in_keeper` 在 25.3 上默认 `false`,25.3 的 `Settings.cpp` 里它的说明就是 "Allow the usage of experimental Kafka storage engine that stores the committed offsets in ClickHouse Keeper")。要走那条路得同时满足三件事:打开这个会话开关,并在建表时给出 `kafka_keeper_path` 与 `kafka_replica_name`(两个都是 **Kafka 表引擎设置**,默认空串;见上游 `src/Storages/Kafka/KafkaSettings.cpp`)。所以"Kafka 的 offset 存在 Keeper 里"这句话只在**那套实验配置**下成立,把它当成默认行为是错的(第 08 章 §08.9 的表述是对的:传统实现交给 Kafka 消费者组,较新的实现才把状态存 Keeper)。

不管是哪条路,语义都是"**至少一次(at-least-once)**":**如果 ClickHouse 在处理完一批数据、落盘之前崩溃,重启后会从上次提交的 offset 重新拉,那批数据会被重复处理一次**。这是重复消费最常见的成因。

相关设置:

- **`kafka_num_consumers`**:一个 Kafka 表起几个 consumer。注意它不能超过 topic 的分区数,超过的部分会闲置。
- **`kafka_max_block_size`**:一次拉多少行;调大能提升吞吐,但加大重复窗口。
- **`kafka_handle_error_mode`**:默认 `default`,遇到坏消息直接抛错。设成 `stream` 可以让坏消息不阻塞整体,并把原始消息与错误文本暴露在两个虚拟列 **`_raw_message`** 与 **`_error`** 里(只在解析失败的那一行非空)。还有第三个取值 **`dead_letter_queue`**,把坏消息写进内置的 `system.dead_letter_queue` 表——但它**是 25.8 才加的,25.3 上只有 `default` / `stream` 两个取值**。这正是"这条设置随版本在长"的具体例子。
- **`kafka_skip_broken_messages`**:跳过多少条坏消息,配合上面的模式用。
- **`kafka_commit_every_batch`**:默认 `0`——整块数据落盘之后才提交一次 offset;设成 `1` 则每消费一批就提交一次,把重复窗口收窄、代价是提交更频繁(在 broker 模式即配了 `kafka_keeper_path` 时该值会被忽略,以避免中间提交)。提交时机直接决定崩溃时重复窗口有多大,要与下面的幂等要求配对使用。

对付重复的正确姿势不是"祈祷不重复",而是让下游**幂等**:表里带上 Kafka 的 partition/offset,或者用 `ReplacingMergeTree` 之类的引擎在查询时去重(第 04 章、第 18 章)。

**还有一件事这份清单**不**包含:broker 侧的认证与加密。** 上面这些 `kafka_*` 是**表设置**,而 25.3 的表设置里没有任何一项管 SASL、SSL 或 security.protocol(翻一遍上游 `src/Storages/Kafka/KafkaSettings.cpp` 的 `DECLARE` 列表就能确认:只有地址、topic、group、格式、消费者数、超时、提交策略这一类)。认证走的是 **librdkafka 自己的配置项** —— 上游 `KafkaConfigLoader.cpp` 从服务端配置与**命名集合**里把键值读出来,再逐项喂给 cppkafka 的 `Configuration`(第 26 章 §26.11 讲的命名集合在这里正好派上用场,凭证不必落进建表语句)。所以"连不上一个需要认证的 Kafka"时,别在 `CREATE TABLE` 里找开关 —— 它不在那儿。

### 不止 Kafka:其他消息总线

"消息队列引擎"这一节不只有 Kafka。ClickHouse 提供了一族表引擎来接不同的队列/总线,套路和 Kafka 一致(消费 → 配 MV 落盘),但各有取舍:

- **`S3Queue`**:把对象存储上一批批落下来的文件当队列消费(消费过的文件记进 Keeper,不重复处理)。云端"对象存储 + 事件通知"的落地方式,比 Kafka 少一层组件。
- **`NATS`**:轻量级消息系统,适合低延迟、小消息的场景。
- **`RabbitMQ`**:传统 AMQP 消息队列的接入,生态成熟,适合"已有 RabbitMQ 基础设施、不想再架一套 Kafka"的团队复用。
- **`Redis`**:把 Redis 的 list/pub-sub 当队列(也存在消息丢失的窗口,别当持久队列用)。
- **`AzureQueue`**:Azure 的队列服务。
- **`Kafka`**:事实上的默认选择,吞吐与持久性兼顾。

**怎么选**:有 Kafka 就用 Kafka;**云端对象存储 + 事件驱动**用 `S3Queue`;轻量低延迟用 `NATS`;已有 RabbitMQ/Redis 基础设施就复用。无论哪种,**"必须配 MV 落盘"这条铁律不变**——它们都是"读一次就推进 offset"的消费语义。各引擎的设置名(如消费批量、错误处理)与具体能力以目标版本文档为准。

## 30.3 数据进来:数据库同步、JDBC/ODBC 与 CDC

上游业务库是另一类数据源。这里有四条不同层次的路径,别混:**直接远程读**(表函数/表引擎)、**JDBC/ODBC 作为源**、**外部 CDC 工具推**、**ClickHouse 自带的数据库引擎镜像**。

**直接远程读:表函数与表引擎。** `MySQL` 和 `PostgreSQL` 都有表引擎与表函数,可以**直接把上游库当表读**:

```sql
SELECT * FROM mysql('host:3306', 'db', 'orders', 'user', 'pass');
SELECT * FROM postgresql('host:5432', 'db', 'orders', 'user', 'pass');
```

这是**拉取式**的:每次查询都打到上游库。用它做小维度表的 JOIN 补充没问题,用它当"同步方案"会拖垮上游(第 21 章讲字典是同一个道理——大批量维度用字典、别实时打上游)。

**JDBC/ODBC 作为源。** 除了专用的 MySQL/PostgreSQL 函数,还有 `jdbc()` / `odbc()` 这两个**通用**表函数——通过 JDBC/ODBC 驱动连**任意**数据库:

```sql
SELECT * FROM jdbc('jdbc:mysql://host:3306/db?user=u&password=p', 'orders');
SELECT * FROM odbc('DSN=my_dsn', 'db', 'orders');
```

关键限制:ClickHouse 进程自己**不带 JDBC 驱动**,`jdbc()` 依赖一个叫 **clickhouse-jdbc-bridge** 的独立服务——它是一个单独部署的进程,ClickHouse 把调用转发给它、它用真正的 JDBC 驱动连上游。所以用 `jdbc()` 前要先起 bridge、配好驱动;`odbc()` 则依赖系统里装的 ODBC 驱动。这里要分清方向:**§30.8 讲的 JDBC/ODBC 驱动是"客户端接入 ClickHouse"用的,而 `jdbc()`/`odbc()` 是"服务端去连别的库"用的**,一进一出,别混。

**外部 CDC 工具推(最主流的持续同步方式)。** 先讲清一条定位:**ClickHouse 本身不是 CDC 工具**——它没有内建 binlog 解析能力,也没有"变更流"的概念。你在 ClickHouse 里看到的所谓"同步",绝大多数是上游把变更推过来的。两条现实路线:

- **外部 CDC 工具 + Kafka**:用 Debezium(读 MySQL binlog / PostgreSQL 逻辑复制)或 Kafka Connect 把变更打成 Kafka 消息,再用 §30.2 的 Kafka 引擎接进来。这是最主流、最可控的做法——你可以自己决定怎么把 `INSERT`/`UPDATE`/`DELETE` 映射成 ClickHouse 的写入(通常是插入新版本 + `ReplacingMergeTree` 去重,而不是原地更新,第 18 章)。
- **ClickHouse 自带的数据库引擎**:`MaterializedPostgreSQL` 是数据库级引擎,能把上游库"镜像"成 ClickHouse 里的库;它依赖上游的逻辑复制,目前**仍是实验特性**,要开 `allow_experimental_database_materialized_postgresql`,而且 **Cloud 上不支持**,限制也随版本变化。名字更顺口的 `MaterializedMySQL` **在 22.8 被标为弃用、24.1 起默认禁用、随后被移除**,**25.3 上不要再指望它**。要选这条路线,请用目标版本实测,别依赖文档摘要。

**四条怎么选**:维度表偶尔查 → 表函数 / 字典;**持续、低延迟同步事实数据** → 外部 CDC + Kafka;异构库的一次性读取 → `jdbc()` / `odbc()`;`Materialized*` 引擎除非实测过,否则别上生产。

## 30.4 数据进来:对象存储与数据湖

### 表函数与表引擎:在对象存储上原地读

ClickHouse 可以直接把对象存储上的文件当成表读:

```sql
SELECT * FROM s3('https://bucket.s3.amazonaws.com/data/*.parquet', 'Parquet');
SELECT * FROM iceberg('https://bucket/warehouse/db/table', 'id', 'key');
SELECT * FROM deltaLake('s3://bucket/path', 'id', 'key');
```

`S3` 引擎、`AzureBlobStorage`、`HDFS` 是表级引擎;`s3()`、`iceberg()`、`deltaLake()`、`hudi()` 是表函数。它们的共同点是"**在对象存储上原地查询,不导入**",适合数据湖场景。要注意:

- 对象存储上的文件没有本地索引,扫描比本地 MergeTree 慢;ClickHouse 只能靠分区裁剪(读 Iceberg 的 manifest、Parquet 的 footer 统计)和谓词下推去减少读取。谓词下推只在数据**物理上按该列有序或聚簇**时才划算——列值散布在各个文件里,min/max 统计就筛不掉任何文件。
- 25.3 上 Iceberg / Delta Lake / Hudi 三类都**只读**:官方对 Iceberg 表引擎的表述就是 "read-only"。**写入能力是更晚版本才出现的**:Iceberg 的 `INSERT` 用 `allow_insert_into_iceberg`(早期叫 `allow_experimental_insert_into_iceberg`,约 26.2 起转 beta);Delta Lake 的写入仍是实验特性,开关 `allow_experimental_delta_lake_writes`(更晚才有非实验别名);Hudi 与 Paimon **只读**。所以**本书对标的 25.3 上没有任何湖表写回能力**,用到时以目标版本实测为准。

**第四个开放表格式:Apache Paimon。** 除了 Iceberg / Delta Lake / Hudi,还有在 Flink 生态里常见的 **Paimon**——ClickHouse 侧**确实有对应入口**:表函数 `paimon(...)`(它是 `paimonS3` 的别名,另有 `paimonAzure` / HDFS / 本地等变体)。但要点是**这个入口不在 25.3 上**——它是更晚的版本才加进来的,而且**只读**、**不支持 merge-on-read**(读主键表会直接报错)。所以如果你们的湖用的是 Paimon,**别按"Iceberg 能做的它也一定能做"去假设**,更别在 25.3 上找它:先在目标版本上确认入口存在(`system.table_functions` 里查得到),再确认它能读到什么。

**Delta Lake 与 Hudi 的最小读法,以及它们各自的边界。** 前面那个代码块给的是最省事的几种写法;这里把 Delta Lake 与 Hudi 单独展开一点——它们最容易被和 Iceberg 混为一谈,但入口名、成熟度与分区裁剪能做的事并不相同。

Delta Lake 的入口是表函数 `deltaLake(...)`,它其实是 `deltaLakeS3` 的别名;另有按存储位置分的变体 `deltaLakeAzure(...)` 与 `deltaLakeLocal(...)`。最小读示例(表函数与表引擎两种形态):

```sql
-- 表函数:把对象存储上的 Delta 表原地读成一张表
SELECT * FROM deltaLake('s3://my-bucket/path/to/table', 'ACCESS_KEY', 'SECRET_KEY');

-- 本地路径(不带凭据)
SELECT * FROM deltaLakeLocal('/var/lib/clickhouse/user_files/delta_table');

-- 表引擎:把它当成一张外部表挂起来
CREATE TABLE delta_tbl
ENGINE = DeltaLake('s3://my-bucket/path/to/table', 'ACCESS_KEY', 'SECRET_KEY');
```

Delta 在 25.3 上**只读**(属实验性集成)。它的分区裁剪靠读事务日志里记录的**分区列**,能不能裁掉文件取决于表在写入时有没有按你查询的维度做分区——和 Iceberg 读 manifest 是同一个道理;文件级的 min/max 统计也取决于写入端有没有写,统计缺失时谓词下推就退化成整文件扫描。

Hudi 的入口是表函数 `hudi(...)` 与同名的表引擎 `Hudi(...)`:

```sql
SELECT * FROM hudi('s3://my-bucket/path/to/hudi_table', 'ACCESS_KEY', 'SECRET_KEY');

CREATE TABLE hudi_tbl
ENGINE = Hudi('s3://my-bucket/path/to/hudi_table', 'ACCESS_KEY', 'SECRET_KEY');
```

Hudi 同样**只读**,但有一个必须先知道的边界:**Copy-on-Write(CoW)表能完整读**(走 snapshot 读),而 **Merge-on-Read(MoR)表只部分支持**——读取端只看已经落好的 base / compacted Parquet 文件,不会去合并 delta 日志,所以 MoR 的增量部分读不到、这部分状态也是实验性的。Hudi 还暴露一批虚拟列(`_path`、`_file`、`_size`、`_time`、`_etag`),排查"这一行来自哪个文件"时很好用。

**路径写法有讲究,别照搬别家系统的写法。** 这些表函数都接受 `s3://bucket/prefix/...` 这样的一级路径;本地路径用各自的 `*Local(...)` 变体;Azure 则改用专门的 `*Azure(...)` 变体(参数是账号 / 容器 / blob 路径这一套,而不是拼进 URL)。至于哪些 URI scheme(例如 `s3a://`)被接受,各版本、各函数并不一致,**本书不打包票**——以你那一版的函数文档为准。

上面每个入口名到底在不在、在你的版本里叫什么,最快的自证是直接问实例:

```sql
SELECT name FROM system.table_functions
WHERE name ILIKE '%delta%' OR name ILIKE '%hudi%'
   OR name ILIKE '%iceberg%' OR name ILIKE '%paimon%'
ORDER BY name;
```

这条查询既是"入口在不在"的答案,也是"这个版本把它叫什么"的答案——比任何速查表都可靠(确切的表名与签名以 `system.table_functions` 和目标版本的官方文档为准)。

**别把"S3 盘"和"`s3()` 表函数 / `S3` 引擎"混为一谈。** 两者都连对象存储,但层次完全不同。**S3 盘**(`storage_configuration` 里的 `<disk><type>s3</type>`)是把对象存储当成**本地数据的延伸**:数据仍由 `MergeTree` 管理、仍有 part 与主键索引,只是 part 的字节落在 S3 上——它服务于**分层存储**(§29.13),写入走正常 `INSERT`,查询仍享受本地索引。而 **`s3()` 表函数 / `S3` 引擎**是把对象存储上的**现成文件**当成一张外部表**原地读**(文件可以是 Parquet/CSV/JSON,ClickHouse 不接管它的组织方式),既没有 part 也没有主键索引,只能靠分区裁剪与谓词下推。一句话:**"S3 盘是我的存储层,`s3()` 是别人的文件。"**

### 库引擎 DataLakeCatalog:连的是 catalog,不是路径

**接入湖表有两种方式,成熟度差别很大。** 一种是上面那一类**表函数 / 表引擎**(`iceberg(...)`、`deltaLake(...)`、`hudi(...)` 以及 `S3` / `AzureBlobStorage` / `HDFS`),把湖表的位置(路径或表名)写死在查询或建表里;另一种是**库引擎 `DataLakeCatalog`**(25.3 引入),它连的是一个**外部 catalog**(AWS Glue、Databricks Unity Catalog、Hive Metastore、Iceberg REST 等),把 catalog 里登记的表**自动发现**成一张张可查的表:

```sql
SET allow_experimental_database_glue_catalog = 1;   -- 开关名按 catalog 类型而异,以文档为准
CREATE DATABASE lake
ENGINE = DataLakeCatalog
SETTINGS catalog_type = 'glue', region = 'us-west-2',
         aws_access_key_id = 'AKIA...', aws_secret_access_key = '...';
SHOW TABLES FROM lake;
SELECT * FROM lake."db"."table_name";
```

(表名要带命名空间、整体用引号括起来,因为 ClickHouse 只认单层命名空间。) 官方对**有 catalog 的场景**更推荐用 `DataLakeCatalog` 而不是逐个手写表引擎:表的发现与 schema 演进由 catalog 统一负责,你少维护一份"表在哪儿"的映射;只有在**没有 catalog、只认一个固定路径**时才用表函数 / 表引擎。

### 读与写的成熟度不在一个层次

读这一侧已经相当完整:能借助 catalog / manifest 做**分区裁剪**、把谓词下推到 Parquet 读取层、做**时间旅行**(按 snapshot id 或时间戳读历史版本)、跟随**schema 演进**(列的新增、删除、重排、改名与类型提升,如 int→long、float→double 都能读)。写这一侧则仍嫩:25.3 完全没有写回能力,之后才以实验开关的形式出现;**什么时候能当稳定能力用,取决于你的版本,不要按"应该能写"去设计**。所以现实且稳妥的用法是"**读湖、写 MergeTree**":湖表当作**只读数据源**,要用它做重分析就 `INSERT ... SELECT` 进本地表。

**schema 演进:读得到,不等于全都支持。** ClickHouse 对 Iceberg 的列级演进是跟随的,但官方同时明确提示它**原本不是为"外部会改 schema"设计的**,某些特性可能不可用(复杂类型、被重整过的分区规范、非默认的列名映射都会踩坑)。落到实践上:凡是**依赖湖表 schema 长时间稳定**的查询才好写进生产;而对频繁加列 / 改类型的宽表,更适合在**导入本地 MergeTree 的那一步**用一次显式 `SELECT` 把列对齐,把"schema 漂移"挡在入库处,而不是让每条查询都去适配外部 schema。

**和对象存储、冷热分层的关系。** 直查湖表时,ClickHouse 是**按文件粒度从对象存储读**的,它不管这些文件的生命周期——湖侧的冷热由**你**在对象存储上按生命周期策略安排,ClickHouse 这侧只能在读取层加缓存(`enable_filesystem_cache` 把热 Parquet 文件落本地盘;Iceberg 的元数据与 manifest 列表也各有缓存,默认开启与否按版本查)。另外,连 catalog 的湖表**每次查询都要拉一次元数据**,所以"元数据缓存 / 加速刷新"这类开关在湖表上比在普通表上更值得调。相反,**把数据搬进本地 MergeTree** 之后,冷热分层就回到 ClickHouse 自己手里:用 `storage_configuration` 的多卷策略加 TTL,把老数据自动挪到廉价盘(第 29 章 §29.13、第 18 章 §18.13)。两者的分工是清楚的:**对象存储负责"便宜地长期存",本地盘负责"快地被反复查"**。

### 直查还是导入本地 MergeTree

**什么时候该直查湖表,什么时候该导入本地 MergeTree。** 判断标准是**查询的重复度与延迟要求**,而不是数据量大小。

**直查湖表**适合:探索性 / 临时查询、一次性对账、只跑一次就丢的分析,以及"数据已经在湖里、再搬一份不划算"的场景。它的代价是**每次查询都要走"读元数据 → 列文件 → 解析 Parquet → 扫描"这一整套**,没有主键索引、也没有跳数索引可用,一次没有有效分区裁剪的查询就是一次全量文件扫描。官方给过一个对照实验:同一条分析查询,直查 Iceberg/Parquet 要数秒,`INSERT ... SELECT` 进一张带 `ORDER BY` 与索引的 MergeTree 之后降到零点几秒,差了几十倍。

**导入本地 MergeTree** 适合:看板 / 报表这类**高频、要低延迟、要高并发**的查询,以及需要 ClickHouse 特有能力(物化视图增量聚合、字典、TTL、projection、全文 / 向量索引)的负载。它的代价是**多一次搬运**:要么周期性 `INSERT ... SELECT`,要么搭一条同步链路(§30.2、§30.3),并且要接受数据**多存一份**、与湖之间有一段**延迟**。

经验法则:**一次性、低频、探索性 → 直查;高频、低延迟、要索引 → 导入。** 若两者都要,常见解法是"**湖做唯一真相源,只把要反复查的那一部分(最近 N 天、或某几张热点宽表)导入 MergeTree 做服务层**",而不是二选一。

**一点版本提醒。** 上面这些能力(尤其 `DataLakeCatalog`、时间旅行、schema 演进的具体覆盖面,以及写回)在不同版本之间差异很大,**凡是本节没有给出确切开关名 / 版本号的地方,都请在目标版本上用 `SHOW TABLES`、`system.settings` 与官方文档自证**,不要照抄一个"看起来对"的默认值。

### 文件、URL 与流式表函数

- `file(path, format, structure)`:读本地文件,路径相对 `user_files_path`。
- `url(url, format)` / `URL` 引擎:把一个 HTTP 端点当表读。
- `input(structure)`:把跟在本条查询后面的数据当成表,方便 `INSERT ... SELECT` 和脚本。
- `remote(host, db, table)` / `cluster(...)`:读远程实例的表,是分布式查询的基础(第 23 章)。

**与 mini-clickhouse 的对照。** 对齐点:mini-clickhouse 实现了 `file`、`url`、`input`、`remote`、`merge`、`values`、`format` 这些**不依赖外部 SDK** 的表函数,调用路径与真实 ClickHouse 同构。简化点:`s3`、`hdfs`、`mysql`、`postgresql`、`iceberg` 这些**依赖网络与外部 SDK** 的连接器,mini-clickhouse 没有实现,而是把它们注册成"未实现"并在调用时给出理由(缺 SDK、缺网络),而不是假装能用。这处简化恰好点出一个事实:对象存储/Kafka/数据库这些连接能力是 ClickHouse 的**外部边界**,不属于它的核心存储引擎。

## 30.5 字典:把外部系统当维度层

第 21 章把字典讲透了,这一节只讲它在**集成**里的位置:字典是 ClickHouse 里最"集成"的一类对象——它**把外部系统的数据同步到本地内存**,让你用一次哈希查找代替一次 JOIN。

**为什么接入场景特别需要它。** 上游业务库里的维度表(用户、商品、地区)变化慢、基数中等、查询频率高。每次查询都 `JOIN` 一张 MySQL 表或远程表,既慢又拖垮上游;字典让 ClickHouse 按 `LIFETIME` 周期把这张表**整份拉到本地内存**,查询时用 `dictGet('dict_name', 'attr', key)` 直接查内存。

```sql
CREATE DICTIONARY user_dim (
    user_id UInt64,
    country String DEFAULT 'unknown',
    tier    String DEFAULT 'free'
)
PRIMARY KEY user_id
SOURCE(MYSQL(host 'db:3306' user 'u' password 'p' db 'app' table 'users'))
LAYOUT(HASHED())
LIFETIME(MIN 300 MAX 600);
```

**SOURCE 覆盖的外部系统**:文件(`FILE`)、ClickHouse 表(`CLICKHOUSE`)、MySQL、PostgreSQL、HTTP 接口、MongoDB、可执行程序(`EXECUTABLE`,通过脚本/程序产出数据)。所以"把任何能批量取到的数据做成维度"几乎都可行。

**LAYOUT 决定内存与速度的取舍**:`HASHED` 全量进内存、最快,`CACHE` / `SSD_CACHE` 不全进内存、按需缓存更省内存但慢——具体型号与选型准则见第 21 章 §21.6,这里不重复。

**LIFETIME 是"懒"的**:过期不会立刻触发重载,而是等下次被查时按需刷新(第 21 章 §21.9)。所以字典更新有延迟,不适用于"必须读到最新值"的场景。

```sql
-- 查询侧:取一个属性,键找不到就返回 DEFAULT(不报错)
SELECT dictGet('user_dim', 'country', toUInt64(user_id)) AS country, count()
FROM events GROUP BY country;
```

**它作为 JOIN 替代的价值**(第 15 章 §15.11):高频查询 + 慢变维度 + 可容忍更新延迟,三条同时满足时,字典把"每次构建哈希表"变成"后台构建一次、查询查内存",常比 JOIN 快一个数量级;代价是维度数据必须能整份拉到本地,以及相应的内存占用。

**监控**:`system.dictionaries` 看加载状态与内存(第 21 章 §21.13);`status` 不是 `LOADED` 的行就是"为什么 `dictGet` 报错"的答案。字典加载失败是**静默**的,上线后要盯。

## 30.6 命名集合:连接凭证的共享机制

前面几节里的每个连接——S3、URL、MySQL、Kafka——都要凭证。把密钥写进每条 SQL 会泄漏进 `system.query_log`(第 26 章 §26.15),于是 ClickHouse 提供**命名集合(named collection)**:凭证集中定义一次,各处按名字引用。

它的建法、`ALTER` / `DROP`、`system.named_collections`、以及"按集合授权"的写法,第 26 章 §26.11 已经从**权限与安全**的角度完整讲过,这里只补集成视角的那一面:**命名集合是全部连接器的共享凭证层**——S3、URL、`remote`、MySQL、Kafka 都能引用同一个集合,而且集合里除了凭证还能带 `format` 这类默认参数(连接器把它当默认值用,所以同一条流不必在每个调用点重复写)。它在这里的地位是划线的:**给每条数据流配一个命名集合,是"可运维的集成"和"一堆硬编码密钥"的分界线**——定了集合之后,轮换密钥只改一处,`query_log` 里从头到尾只有集合名。反过来说,如果一份凭证只在一次性探索里用,直接内联反而更省事。

## 30.7 数据出去:接入协议与交换格式

ClickHouse 默认监听两个端口:HTTP 在 **8123**,原生 TCP 协议在 **9000**。HTTP 接口几乎什么都能干,而且用 `curl` 就能跑,是"出去"这条线里最通用的一个:

```bash
# 查询,指定输出格式
curl 'http://localhost:8123/?query=SELECT%20count()%20FROM%20events&default_format=JSON'

# 用 --data-binary 传 SQL(避免 URL 编码)
curl 'http://localhost:8123/' --data-binary 'SELECT 1 FORMAT CSV'

# 用 query 参数传 SQL,POST body 传数据做导入
curl 'http://localhost:8123/?query=INSERT%20INTO%20events%20FORMAT%20CSV' \
     --data-binary @events.csv
```

要用到的几个机制:

- **`FORMAT` 子句或 `default_format` 参数**:决定返回什么格式(第 19 章)。`JSONEachRow` 适合程序解析,`CSV` 适合人看。
- **`query` 参数**:把 SQL 放在 URL 里。SQL 里有特殊字符时要 URL 编码,或者改用 POST body。
- **POST 大 body 做导入**:`--data-binary` 直接把文件 POST 上去,配合 `?query=INSERT ... FORMAT ...`。这是最常用的批量导入方式。
- **认证**:用 `X-ClickHouse-User` / `X-ClickHouse-Key` 头,或者 `?user=&password=`。

HTTP 接口的短板是不支持真正的流式双向交互和会话状态(每个请求默认独立),重负载的交互式查询还是原生协议更合适。

**原生协议与"兼容 wire protocol"。** HTTP 之外,ClickHouse 默认还监听**原生 TCP 协议端口 9000**,`clickhouse-client` 与官方驱动走的就是它——它支持真正的流式双向交互与会话状态,是重负载交互式查询的正道。另外,新版 ClickHouse 内置了两个**兼容 wire protocol**,让你用**现成的生态驱动**直接连进来:

- **MySQL wire protocol,默认端口 9004**:开了它,任何 MySQL 客户端/驱动(JDBC 的 MySQL 驱动、MySQL CLI,以及大量只认 MySQL 协议的工具)都能把它当"一台 MySQL"连上;SQL 仍然是 ClickHouse 的 SQL(只覆盖一个受限子集,不是完整的 MySQL 语法)。对"让 BI / 现成驱动少改代码直接连 ClickHouse"这条线,这是最省事的一步。
- **PostgreSQL wire protocol,默认端口 9005**:同理,任何 PostgreSQL 客户端/驱动(psql、pg 生态的 BI 连接器)都能连进来。

两个协议在 `config.xml` 里各自有对应的配置段来启用监听与端口(段名与确切配置项以目标版本为准,**25.3 的确切项名未确认**),而 9004 / 9005 只是**约定俗成的默认示例值**,可以改。要点:兼容协议**只覆盖协议层**,不提供 MySQL/PostgreSQL 的服务器端特性(存储过程、事务语义、`mysql` 系统库那一套),别指望它是"完整的 MySQL 服务"——它的价值就在于让现成驱动零改动连上。

**原生协议内部:块、revision 与状态机。** 原生协议搬的是**列式块**,不是逐行:结果在服务端按块组好、每块自带列类型,客户端边收边解、边喂给上层,所以一次大查询可以"一边算一边吐",不必先把整个结果集在客户端物化。两端之间还有一次 **revision 协商**:连接建立时客户端与服务端交换各自支持的协议修订号,新特性只有在两边都认的情况下才启用——**版本不匹配时较新的能力会被静默降级**(查询照样成功,只是走了旧路径)。这也是排查"同一个驱动连两个版本、行为却不一样"时该先看的地方。传输层另有可选的压缩开关,与列存本身的分块压缩是两回事。这套"块流 + 状态机"的设计,正是它能同时做流式结果与长连接交互的底子。

**二进制交换格式与它们的 schema。** 出到别的系统时,`JSONEachRow` / `CSV` 方便但每行都要重新解析;要高效搬运结构化数据,ClickHouse 还支持:

- **`Arrow` / `ArrowStream`**:Apache Arrow 的列式内存格式(`Arrow` 是带 schema 的整体,`ArrowStream` 是流式分帧)。它与 Pandas / DuckDB / Spark 这些生态天然互通,是"把一坨列式数据高效搬进/搬出"的首选。
- **`Parquet` / `ORC`**:文件级列式格式,配合前面的对象存储表函数使用。
- **`Protobuf` / `CapnProto`**:**不是"由数据自己定义"的格式,而是"按你的 schema 序列化"**——它们需要你**显式提供 schema 文件**:`Protobuf` 要在 `format_schema` 设置里给出 `.proto` 文件名与消息类型名,`CapnProto` 要给 `.capnp` 文件;这些文件通常放在服务端 `format_schema_path` 指向的目录里。没配好 schema 文件,格式解析会直接失败。

这些格式都是**双向**的:`SELECT ... FORMAT Arrow` 能导出,`INSERT ... FORMAT Arrow` 能导入;选哪个取决于对端生态——和 Python/Arrow 生态对话就用 `Arrow`,落到文件与数据湖就用 `Parquet`。

**`FORMAT` 之外还有一条独立的"列式传输"通道:Arrow Flight 与 ADBC。** 这里要把两件容易混的事分开:上面那些 `FORMAT` 走的仍然是 ClickHouse 自己的 HTTP / TCP 接口,只是**载荷**是列式的;而 **Arrow Flight** 是一套**独立的 RPC 协议**(基于 gRPC),专门用来在进程之间搬 Arrow 数据,不经过 SQL 文本这一层;**ADBC**(Arrow Database Connectivity)则是架在它(或各语言库的原生实现)之上的**驱动标准**,大致对应"列式世界里的 JDBC/ODBC"。对"用 Python / Go / Rust 一次拉上亿行"这类场景,它们的意义是绕开逐行解析与一次多余的序列化,通常是吞吐最高的那条路。ClickHouse 侧对应的入口是 `arrowFlight` 表函数(**它是很新的能力:本书没有在 25.3 上核对到它,推测属于更晚的版本;确切签名、是否需要实验开关、是否默认可用,本书均未确认**——用之前先 `SELECT name FROM system.table_functions WHERE name ILIKE '%flight%'` 看一眼它认不认这个名字,再回官方文档确认开关)。

选择口径可以简单地分三档:**结果只有几万行** → 用什么格式都行,`JSONEachRow` 最省事;**结果很大、对端是 pandas / DuckDB / Spark 这类 Arrow 生态** → 先用 `FORMAT Arrow` / `ArrowStream`,这已经能省掉绝大部分解析开销;**需要的是"驱动层零拷贝、长连接、高并发拉取"** → 再考虑 Arrow Flight / ADBC,并接受它带来的额外部署与版本依赖。

## 30.8 数据出去:驱动与 BI 工具

ClickHouse 有官方的 JDBC、ODBC 驱动,以及 Python(`clickhouse-driver` / `clickhouse-connect`)、Go 等客户端。BI 工具方面,Grafana、Metabase、Superset、Tableau 这类都有可用的连接方式(官方插件或社区驱动)。

**驱动选型:HTTP 还是原生 TCP。** Python 生态里,`clickhouse-connect` 走 **HTTP 接口**、`clickhouse-driver` 走**原生 TCP 协议**,恰好代表两条路。取舍是直接的:HTTP 那侧部署最省事(过代理、过防火墙都简单,和你手写 `curl` 是同一条通道),代价是它天生**无状态**——每个请求默认独立,要靠 `session_id` 把一组请求串成一次会话、靠 `query_id` 去追踪或取消单条查询(**这两个参数的语义在第 02 章 §02.3、§02.4 已经建立,这里不重复解释**);原生 TCP 那侧有真正的会话状态与全双工流式,重负载的交互式查询、大批量读写都更顺,代价是端口与网络策略要单独放行。选哪个,先看你的网络环境允许什么,再看负载是"一次拉一小坨"还是"长连接持续拉大块"。

**连接池与并发:先撞的往往是连接数,不是 CPU。** 一个常见的坏习惯是"每条查询新开一个连接"。高并发下,这样做的第一道墙是**连接数上限与握手开销**,而不是 CPU——每建一条新连接都要重建会话、重走一次上面的 revision 协商,还没开始算就付了成本。正确姿势是**连接复用**:进程内维护一个连接池,让查询在少量长连接上轮流跑,把连接数压到可控范围。这和"点查弱"是同一条问题的两面:ClickHouse 面向的是"少量大查询",把它当"每请求一条小查询"用,连接层会先撑不住(见下面的坑)。

**客户端攒批,与服务端 `async_insert` 的配合与冲突。** 写入吞吐的第一杠杆是"批次够大"(第 08 章)。但要注意:**客户端攒批与服务端 `async_insert` 是两层攒批,别让它们互相打架。** 客户端每行发一次、指望 `async_insert` 替你攒,等于把反序列化与暂存的开销全丢给服务端;客户端自己攒成大块再发,则把压力前移、更可控。两层都开、参数又不匹配时(客户端密发、服务端阈值又很大),常见的结果是"看起来开了异步插入,却还是写出一堆小 part"。用之前先想清楚**最终生效的是哪一层**。

**重试语义:哪些错误能重试、哪些重试会写出重复数据。** 驱动层的重试不是免费的。对**只读查询**和**幂等的 DDL**,超时或连接中断后重试通常安全;但**对 `INSERT` 就危险**——写入可能已经在服务端落盘、只是响应没回来,这时重试会把同一批数据写两遍。ClickHouse 给的护栏不在驱动层,而在写入语义里:**复制表的插入去重**(`insert_deduplicate`)与 **`insert_quorum` 的 quorum 写入**(第 22 章 §22.5、§22.6)决定了"同一批重发"会不会变成重复行。**先把这层语义配好,再让驱动去重试,不要反过来。** 还有一条原子性的边界要一起记住:一批 `INSERT` 在**单分区内**是原子的,跨分区则不作保证——所以重试的粒度要对着"一批"来设计。

**点查为什么是弱项:瓶颈在往返,不在算。** 高并发小查询的开销大头往往不是计算,而是**往返**:一次网络往返、一次连接 / 会话与调度开销,换回来的却只是"一行"。列存也不是为"按主键取一行"设计的——它得为这一行把涉及的列拼起来。于是把它放在"每秒几千次点查"的位置上,先崩的是连接与调度,而不是 CPU 核数。这就是下面这个坑的技术根因。

**这里有一个几乎每个团队都会踩的坑:把 ClickHouse 当 MySQL 用。** 具体表现是:

- **错误做法**:BI 的每次刷新都对明细表跑带 `JOIN` 的实时查询,还带高并发点查(按主键取一行)。
- **后果**:ClickHouse 的单表扫描很强,但**高并发点查很弱**——它为"扫大量行算少数结果"设计,不是为"每秒几千次按主键取一行"设计。并发一上来,内存和 CPU 一起炸。
- **正确做法**:把 BI 的查询**预聚合物化**到宽表里,让 BI 只做"扫聚合好的表";点查类需求交给上游 OLTP;给 BI 用户单独设配额(第 25 章)和只读账号(第 26 章)。

## 30.9 数据出去:Sink 方向

前面讲的多是"读进来",反过来"写出去"同样是一等公民——ClickHouse 可以把查询结果直接**写进外部系统**,不用先导出文件再上传。统一形态是 **`INSERT INTO FUNCTION <表函数>`**:

```sql
-- 写回 Kafka(下游另有消费者)
INSERT INTO FUNCTION kafka('broker:9092', 'topic', 'group', 'JSONEachRow')
SELECT ts, user_id, action FROM events WHERE ts > now() - 300;

-- 写对象存储
INSERT INTO FUNCTION s3('https://bucket.s3.amazonaws.com/out/data.parquet', 'id', 'key', 'Parquet')
SELECT * FROM report_daily;

-- 写 Iceberg(表函数形态;**25.3 上不支持写入**——INSERT 自 26.2 起才是 beta,
-- 且要 allow_insert_into_iceberg)
INSERT INTO FUNCTION iceberg('s3://warehouse/db/table', 'id', 'key') SELECT ...;
```

还有两条"整体搬走"的路径:

- **`BACKUP ... TO S3(...)` / `TO Disk(...)`**:把库/表/分区整份备份到对象存储或盘(第 29 章 §29.9)。它比 `INSERT ... SELECT` 更适合"整表/整库的归档与迁移",因为它一致、支持增量。
- **`INSERT INTO FUNCTION remote(...)` / `remoteSecure(...)`**:把结果推到另一个 ClickHouse 实例。`remoteSecure` 是 `remote` 的 TLS 版本(第 23 章 §23.7)。

**"出去"这条线的纪律**:写外部系统是**同步**动作,外部的可用性会直接影响你的 `INSERT`(Kafka 挂了、S3 限流都会让写变慢甚至失败)。别把关键数据流的出口设计成"依赖一个可能不可用的外部服务",要么用能缓冲的中间层,要么接受重试语义。

## 30.10 工具链:clickhouse-local、keeper、benchmark、copier

- **`clickhouse-local`**:不开服务端,直接对文件/标准输入跑 SQL。做一次性 ETL("把 CSV 转 Parquet""过滤日志")比写脚本快得多,而且用的是同一套 SQL 和函数。
- **`clickhouse-keeper`**:ClickHouse 自带的 Keeper 实现(第 22 章 §22.9),用来替代外部 ZooKeeper。**它本身就是工具链的一部分**——集群的复制与分布式 DDL 都依赖它,部署与升级顺序见第 29 章(§29.12 的"先升 Keeper 再升 server")。
- **`clickhouse-benchmark`**:固定并发与轮数地压测同一条 SQL,取中位数看 QPS 与延迟分位(第 28 章 §28.10)。
- **`clickhouse-obfuscator`**:把数据集脱敏成"可安全分享、又能复现性能问题"的版本(第 28 章 §28.10),用来把"我这边慢"变成"你能复现"。
- **`clickhouse-copier`**:在集群/分片之间搬数据,会把任务写进 Keeper,支持失败恢复(第 23 章)。
- **`clickhouse-disks`**:直接操作 `storage_configuration` 里定义的盘,做盘间拷贝、上传下载,排查存储问题时有用。

导出到对象存储或数据仓,通常就是 `INSERT INTO FUNCTION s3(...) SELECT ...` 或反过来 `INSERT INTO warehouse SELECT * FROM s3(...)`——把"外部"当成表函数的两端即可。

## 30.11 建模层:dbt-clickhouse

dbt 是"用 SQL 管理数据模型依赖"的工具,dbt-clickhouse 是它的适配器。它和物化视图的分工值得讲清楚:

- **物化视图**是**流式**的:数据一进来就增量处理,延迟低,但表达力受限(通常只能做"插入时加一段逻辑"),而且视图的逻辑改动后要重建。
- **dbt** 是**批处理**的:按依赖图(模型 DAG)定期重跑,每一步都是一条 `CREATE TABLE ... AS SELECT` 或 `INSERT`。它的优势是**可测试、可版本控制、依赖清晰**,代价是延迟高(要等调度)、算力成本高(每次重算)。

简单说:**实时链路交给物化视图,批量分层的数仓交给 dbt**。很多团队两者并用——Kafka → MV → 明细表,再由 dbt 从明细表跑出汇总模型。

一个 dbt-clickhouse 模型(`models/` 下的一个 `.sql` 文件)最小大概长这样(结构示意,具体 config 键以 dbt-clickhouse 版本文档为准):

```sql
{{ config(
    materialized='incremental',
    engine='MergeTree()',
    order_by='(event_date, service)',
    unique_key='id',
    incremental_strategy='delete+insert'
) }}

SELECT
    toDate(ts) AS event_date,
    service,
    count()    AS events
FROM {{ ref('stg_events') }}
{% if is_incremental() %}
WHERE ts >= (SELECT max(event_date) FROM {{ this }})
{% endif %}
GROUP BY event_date, service
```

要点:模型之间的 `ref()` 构成依赖图(DAG),`dbt run` 按拓扑序执行;`materialized` 选 `table` / `view` / `incremental`;针对 ClickHouse 必须显式给出 `engine` 与 `order_by`——**这正是"dbt 管建模、ClickHouse 管执行"的落点**:dbt 只负责把模型编译成 `CREATE TABLE ... AS SELECT` 或 `INSERT`,真正决定性能的还是排序键这些表级设置(第 28 章 §28.7)。

## 30.12 全文索引与向量检索

近几个版本 ClickHouse 补上了两类"原本要交给专用引擎"的能力。定位先讲清楚:它们是**让 ClickHouse 能再多接一部分负载**,不是要取代 Elasticsearch 或专用向量库。两类都能用,但都年轻,所以除了"怎么建",更要回答三个问题:**写进来要付什么代价、召回 / 精度怎么评估、什么时候干脆别用它**。下面逐条给。

**全文索引(倒排索引)。** ClickHouse 的全文检索不是靠 `LIKE '%keyword%'` 扫全表,而是建一份**倒排结构**,加速 `hasToken` / `hasAnyTokens` / `match` 这一类**词级**谓词。它在 DDL 里写在 `INDEX` 子句(或 `ALTER TABLE ... ADD INDEX`),`system.data_skipping_indices` 里也能看到,所以常被归进"跳数索引"名下;但**机制上不是"按 granule 跳"那一套**——官方文档明说它映射到**行号**而非 granule 号,建表时那个 `GRANULARITY` 对它**没有意义**(位置要写,值被忽略)。"文本索引不是 granule 级跳数索引"这件事,第 11 章 §11.9 讲得更细。

**这一节按 25.3 写。** 25.3 上它叫 `full_text`,是**实验特性**,要 `SET allow_experimental_full_text_index = true`:

```sql
SET allow_experimental_full_text_index = true;   -- 25.3:实验开关
ALTER TABLE logs
    ADD INDEX idx_msg msg TYPE full_text(0) GRANULARITY 1;
```

25.3 的 `full_text(...)` 用**位置参数**选分词方式(文档口径:`N = 0` 按空白切词、`N = 2..8` 走 N-gram;另有一个可选参数限制单个 postings 列表的最大行数)——**具体参数语义以你的版本文档为准**,`GRANULARITY` 照写、但会被忽略。它**改过两次名,都在 25.3 之后**(`full_text` → `gin`,PR #79024;`gin` → `text`,PR #80855,记录在 v25.6 的 changelog 系列里),并把位置参数换成了 `tokenizer = '...'` 键值语法;再往前它叫过 `inverted`。**完整的改名链与逐版本锚点在**第 32 章 §32.7 与第 11 章 §11.9,**本节不重复**;要用哪个名字以你目标版本的文档为准——旧名字仍能加载但会抛异常。只补一条实践提醒:不管哪个版本,**建完索引先 `SELECT * FROM system.data_skipping_indices` 看一眼实际落下来的类型名**——这一步比背住某个名字可靠得多。

**什么时候该用它、什么时候不该。** 它加速的是**过滤**而不是**相关度排序**——ClickHouse 在定位上就不是相关度引擎:官方明说它不做 BM25 / TF-IDF 打分、也不存位置信息,所以拿不到"最相关的十条"这种排序结果;**这一条对 25.3 与更晚的版本都成立**。**写入代价**:建索引要维护 postings 列表,`INSERT` 与后台 merge 都会更慢、part 更大;`ALTER TABLE ... ADD INDEX` 只改元数据,**老 part 必须 `MATERIALIZE INDEX` 才带上索引**(同第 11 章 §11.15 的跳数索引规则)。**该用它**当"日志 / 文档里按关键词粗筛、再叠加时间或其它过滤"——它把"肯定不含这个词"的行整块跳过;**不该用它**当"必须全召回 + 复杂相关度排序 + 高亮"的搜索主界面,那是 Elasticsearch / OpenSearch 的主场;也不该指望它替 `LIKE '%x%'` 的任意子串(那是 n-gram 或 `ngrambf_v1` 的地盘)。评估它是否生效的硬指标,仍是"开关前后**读取的行数 / `SelectedRows`** 差多少",而不是"感觉变快了"。

**向量检索(近似最近邻)。** ClickHouse 支持基于 **ANN** 的向量索引:**类型名 `vector_similarity`**,底层 **HNSW**(沿用第 01 章 §01.8 的口径,由 **USearch** 库实现、距离计算有 SIMD 加速),在存向量的列(`Array(Float32)` / `Array(Float64)`)上声明;25.3 上是**实验特性**,要显式打开 `allow_experimental_vector_similarity_index`:

```sql
SET allow_experimental_vector_similarity_index = 1;   -- 25.3:实验开关
ALTER TABLE items
    ADD INDEX idx_vec embedding TYPE vector_similarity('hnsw', 'cosineDistance', 768, 'bf16') GRANULARITY 1;
```

参数依次是"**算法**(25.3 只支持 `hnsw`)、**距离函数**(`cosineDistance` / `L2Distance` 等)、**维度**、**量化方式**(`bf16` / `f16` / `i8` 等)";量化用精度换内存与速度,是那一档**最主要的旋钮**。查询侧必须写成"最近邻"的标准形状,优化器才认:**`ORDER BY 距离函数(向量列, 参考向量) LIMIT k`**,且 `ORDER BY` 里的距离函数要与索引定义**字面一致**,否则索引**静默不生效**——这和第 11 章 §11.16 讲的"跳数索引谓词必须字面匹配"是同一条坑。**更早的实验类型 `annoy` / `usearch` 已被它取代**(上游 PR #63675;`TYPE annoy(100)` 那类写法不要再用,旧类型仍可加载但写入与检索会抛 `ILLEGAL_INDEX`),**25.6 起它转为 beta、开关去实验前缀改名 `enable_vector_similarity_index`**——和全文索引是同一条规律:**名字随版本变,用之前先按第 11 章 §11.9 的自检动作确认,别照抄任何一处写死的名字。**

**写入代价与"召回 / 精度怎么评估"。** 和全文索引一样,向量索引在**写入与后台合并时构建**:`INSERT` 明显变慢,每个 part 都要带一份图结构,常驻内存随向量数上升;`ADD INDEX` 同样只对新数据生效,**老 part 要 `MATERIALIZE INDEX`**。它是**近似**检索,拿不到精确 K 近邻,召回率取决于索引参数(算法、图的连接度、构建与查询时的候选列表大小)与量化档位。评估方式只能是**离线跑对照**:在小数据量上用全表精确排序得到基准,再用同一批查询量 **recall@k(近似结果与精确结果的重合度)** 和 p95 延迟,在"召回够用"和"延迟可接受"之间取值——**别只看"查询变快了"**,那可能只是召回塌了。**什么时候不该用它**:需要**精确** KNN(漏召回不可接受的场景)、向量**频繁更新**(每次写完都要重建图)、同一列上要用多种距离度量、或要"过滤 + 检索"强耦合的复杂条件——这些都应退回**专用向量数据库**。ClickHouse 的定位始终是"在分析库里顺带做一点语义检索"。

**别搞错适用面。** 这两类能力都还年轻,但共同点很清晰:它们**降低的是"搬迁 / 双写"的成本**,把原本要另起一套系统的过滤、检索搬进同一个库,省掉两套索引与两份数据。代价是它们都不追求"最佳检索质量",而是"够用的近似 + 少维护一条链路"。底层机制(跳数索引怎么在扫描时被用上、`GRANULARITY` 为什么对文本索引无意义)在第 11 章 §11.4 与 §11.9;这里给的是"现在能建哪一种、怎么建、值不值得建"。**本节原先留的两处"未确认"现在都可以收口**:一是全文索引的改名链(三段:`inverted` → `full_text` 在 25.3 之前;`full_text` → `gin` → `text` 在 25.3 之后——完整逐版本锚点见第 32 章 §32.7);二是 25.3 是否有内置 BM25 打分——**ClickHouse 原生就不支持**(它不是相关度引擎、不存位置信息,这条对更晚版本同样成立)。仍然要自证的只是"你那个版本认不认某个类型名",请在目标版本上用 `system.data_skipping_indices` 与官方 changelog 核对。

## 30.13 与 Spark / Flink 的分工

一个常见的架构问题是"重计算到底放哪"。经验分工是:

- **Flink**:真正的**流式**处理——事件时间窗口、状态管理、精确一次的复杂处理逻辑。它擅长"持续变化的状态",ClickHouse 不擅长。
- **Spark**:大规模的**批处理**和 ETL——离线重算、复杂多源清洗。ClickHouse 也能做,但 Spark 的容错和数据湖集成更成熟。
- **ClickHouse**:最终的**服务层**——把处理好的结果存起来,给低延迟、高并发的查询用。

所以典型形态是 "Flink/Spark 做重计算 → 结果写进 ClickHouse → 服务查询",而不是让 ClickHouse 去扛流状态或离线重算。

**边界怎么切。** 三条判断依据,按重要性排:

- **有没有"状态"**。处理逻辑如果需要跨事件维护可变状态(会话、去重窗口、复杂状态机、精确一次的副作用),放 Flink——这正是 ClickHouse 结构性地做不了的部分(它的写入是追加、part 不可变,没有行级更新)。反过来,如果逻辑只是"对一段时间的数据做聚合/关联",那是 ClickHouse 的主场。
- **数据量在不在单机内存里**。Spark 之所以在"多源清洗、超大宽表关联"上更稳,是因为它能把中间结果溢写到磁盘与 shuffle 存储;ClickHouse 的 JOIN 是 build 侧整体进内存(§30.8 的坑),大表对撞会先 OOM。
- **延迟要求**。秒级到亚秒级的服务查询只能落在 ClickHouse;分钟级以上的批算放在 Spark 更划算,因为它的调度、重试、可重现性都是为批设计的。

**两侧怎么接线。** 下游写、上游读各有一条最省事的路:写到 ClickHouse 用 `INSERT ... SELECT`(§30.9 的 Sink 方向,或 Kafka 引擎 + MV,§30.2);反过来,让 Spark/Flink 读 ClickHouse 的结果做校验或再加工,走它的 JDBC 驱动或 `INSERT INTO FUNCTION remote(...)`。**要避免的反模式有两个**:一是让 ClickHouse 承担"逐条 UPDATE 的状态机"(那是 Flink 的活,在 ClickHouse 里会退化成高频 mutation,part 数爆炸);二是让 Spark 把 ClickHouse 明细表当"廉价存储"反复全表拉去做大 JOIN(把 ClickHouse 当数据湖用,得不偿失)。

## 30.14 可观测性栈:日志、指标、追踪的三件套

把 ClickHouse 当**日志、指标、追踪**的后端,是它近几年增长最快的一块用例(ClickHouse 官方也为此推出了打包方案,例如 ClickStack 一类的定位;具体组件与命名**未确认**)。这一节讲**怎么把这三类数据接进来、怎么查出去**;完整的端到端建站过程在附录 F(第 38 章),这里给的是集成面。

### 日志:时间分区 + 排序键

- **时间做分区键**:`PARTITION BY toYYYYMM(ts)` 或按天,让老数据整分区过期(第 18 章),也让时间过滤裁剪掉绝大多数分区。
- **排序键把最常用的过滤维度放前面**:常见 `(service, ts)` 或 `(ts, service)`,取决于查询模式——查单个服务的日志用前者,查时间范围用后者。
- **写进高基数维度要克制**:`Nullable` 和超高基数字符串会显著拖慢查询、抬高存储(第 28 章)。

### 指标:Prometheus 双向

指标这条线的**配置细则(抓取端点、`<prometheus>` 段、`rate()` 纪律、告警写法)在第 27 章 §27.19**,这里不重复,只补两个集成视角的点。其一,ClickHouse 自身指标对外暴露靠的是**内置 Prometheus 端点**(默认 `/metrics`、端口 9363),接进你现有的 Prometheus 就有一条完整的指标链——这是把 ClickHouse 纳管最省事的做法。其二,反方向有个常见误解:新版本引入了 `prometheusQuery()` / `prometheusQueryRange()` 表函数,但**它们的参数是 `(库, TimeSeries 表, PromQL 文本, 时间点/区间, step)`,即在 ClickHouse 自己的 `TimeSeries` 表上对 PromQL 求值,而不是"去连一台 Prometheus"**——别照抄"传一个 `http://prom:9090` 当第一个参数"的写法。**未确认**:这套能力属于较新的 `TimeSeries` 引擎,它在 25.3 上是否可用、函数的确切签名,请以 `system.functions` 与目标版本文档为准。

所以"指标"这条路是双向的:ClickHouse 把自己的指标交给 Prometheus;反过来要把 Prometheus 的指标取进 ClickHouse,就得走**采集/导出那一层**(Prometheus 的 remote-write、OTel Collector 的 prometheus receiver + ClickHouse exporter,或外部 ETL),而不是指望一个内置表函数直接去抓远端 Prometheus。

### 追踪/日志接入:OTLP 与 Collector

现代可观测性栈的实际形态是 **OpenTelemetry Collector → ClickHouse**:应用通过 OTLP 把 trace/log/metric 发给 Collector,Collector 用 ClickHouse exporter 写进 ClickHouse 的日志/追踪表。这是"大多数人真正搭出来的形态"——你不用自己写 Kafka 管道,Collector 就是那个管道。ClickHouse 侧的表结构(`trace_id` / `span_id` / `parent_span_id` / `timestamp` 等)在附录 F(第 38 章 §F.4)有完整设计。ClickHouse 也提供自己的 OTel 集成能力(第 27 章 §27.19),具体组件以官方文档为准。

### Grafana:可视化层

Grafana 是最常用的可视化前端,通过**官方/社区插件**(ClickHouse datasource plugin)接进来:配好 ClickHouse 连接,就能把上面的日志/指标/追踪表画成面板和告警。一条**性能纪律**要提前立好:**别让 Grafana 直接对明细大表跑实时查询**——仪表盘刷新频繁,一条没优化的明细聚合就能把集群拖慢。做法是**为 Grafana 预聚合**:用物化视图或 dbt 把面板需要的粒度先算好(§30.11 的 dbt、§30.2 的 MV),Grafana 只查那张小表。这和 §30.8 里"别把 ClickHouse 当 MySQL 用"是同一条纪律,只是换成了 Grafana 场景。

## 30.15 机器学习与特征

ClickHouse 在 ML 里的常见角色是**离线特征库**和**特征回填**:

- 用 SQL 把用户历史行为聚合成特征宽表,导出给训练;
- 训练好的特征表存回 ClickHouse,在线服务低延迟地读。

取舍要点:ClickHouse 适合"批量算特征、按实体取特征"的场景,不适合做在线推理(它不是推理引擎)也不适合做低延迟点查(见 §30.8 的坑)。真正的特征存储的"在线读"那一环,通常还是交给 KV 存储。

**一个最小片段:把行为聚合成特征宽表。**

```sql
CREATE TABLE user_features
ENGINE = MergeTree ORDER BY user_id AS
SELECT
    user_id,
    countIf(action = 'purchase')         AS purchases_7d,
    countIf(action = 'view')             AS views_7d,
    avgIf(amount, action = 'purchase')   AS avg_amount_7d,
    dateDiff('day', max(ts), now())      AS days_since_last_seen
FROM events
WHERE ts > now() - INTERVAL 7 DAY
GROUP BY user_id;
```

这就是"用 SQL 把明细聚成按实体排列的特征表"的典型写法:训练时把这张表(或它上面的查询结果)读出去即可。在线服务若要按实体低延迟取特征,要保证 `user_id` 是排序键前缀、用点查读单行——但正因为那是 §30.8 说的弱项,真正在线的那一环通常仍落到 KV 存储,ClickHouse 负责**离线算好、批量回填**。

**回填最容易错的地方是"时间点对齐"(point-in-time correctness)。** 上面这条 SQL 用 `now()` 算"最近 7 天",得到的是**当下这一版**特征;但训练样本散布在历史各个时刻,每个样本能用的特征必须**只来自它自己那个时刻之前**的数据。如果拿"截止到今天的聚合"去回填一条上个月的样本,特征里就混进了那一刻还不存在的信息(时间泄漏),线下指标好看、线上直接崩。ClickHouse 侧的应对是把特征表按"特征时刻"分区/排序,回填时显式限定 `WHERE ts <= sample_time - INTERVAL 1 DAY`(留出标签的观察期),而不是在查询里用 `now()`。这一条与"在线点查弱"是两回事——它是建模纪律,不是性能问题。

**特征的维护方式有两种**,按更新频率选:改动不频繁的(用户画像这类)适合**定期全量重算**(`INSERT` 覆盖或 `REPLACE PARTITION`);需要跟得上近实时行为的,适合**用物化视图增量维护**(第 13 章),把"事件流 → 增量聚合状态"接起来,再定期物化成可读取的宽表。两种都要面对同一个取舍:重算频率越高越新鲜,但等价于反复读全量明细,成本随频率线性上涨。

## 30.16 取舍清单

**该交给 ClickHouse 的:**

- 大批量数据的聚合、扫描、分组分析;
- 明细日志的存储与检索(时间分区 + 排序键设计好);
- 预聚合好的宽表服务 BI;
- 离线特征计算与回填;
- 对对象存储上数据湖的即席查询。

**该交给上游或下游的:**

- 高并发按主键点查 → 上游 OLTP 数据库;
- 事务性写入、行级强一致更新 → 上游 OLTP 数据库;
- 有状态的复杂流处理 → Flink;
- 大规模离线重算和多源清洗 → Spark;
- 全文检索的高召回/复杂相关度排序 → 专门的搜索引擎;
- 在线推理、低延迟向量点查 → 专门的推理/向量服务。

把不该它做的塞进来,几乎总会以"某天突然 OOM"或"BI 刷新拖垮集群"的形式还债。

## 30.17 关于时效性

这一章的**时效性压力很大**:生态变化很快,连接器的名称、实验开关、默认取值几乎每个版本都在动。全书时效性最敏感的一章是第 32 章(它直接讲现状与未来方向),本章的压力则集中在"外部产品的连接器"这一类上。本章以 **ClickHouse 25.3** 为基准,但:

- 所有涉及**外部产品**的判断(某个连接器是否存在、某个驱动的能力),请以**官方仓库/官方文档**为准,本章只能给方向;
- 凡是**仍标着"未确认"**的条目——目前剩下的是 **Compatibility 协议监听段的配置项名**(§30.7)、**`arrowFlight` 表函数的确切签名与是否需要实验开关**(§30.7)、**`prometheusQuery()` 的 `TimeSeries` 入口在 25.3 上是否可用**(§30.14)、以及 **ClickStack 的具体组件命名**(§30.14)——都请你**在目标版本上实测**再用。反过来,**全文 / 向量索引的类型名与 BM25 打分**(§30.12)、**湖表写入能力**(§30.4)、**Kafka 错误处理的取值与虚拟列**(§30.2)、**`MaterializedMySQL` 的状态**(§30.3)这几项本次已经收口,不再留"未确认";
- 如果你在生产里依赖某一条本章的断言,记得它可能在你升级之后就变了。

## 30.18 小结

集成可以拆成三条线:**进来**要保证不丢不重(消息队列必须配 MV,CDC 交给外部工具,对象存储原地查,注意"S3 盘"与 "`s3()` 表函数"是两回事;**维度用字典、凭证用命名集合**),**出去**要保证接口和驱动正确(HTTP 8123 万能但别忘 URL 编码;要让现成驱动零改动直连,可开 MySQL wire 9004 / PostgreSQL wire 9005;大数据搬运用 `Arrow`/`ArrowStream`;BI 别当 MySQL 用;也能用 `INSERT INTO FUNCTION` 把结果写回 Kafka/S3),**被消费**要保证建模和访问方式对得上(dbt 做批、MV 做流,Grafana 要预聚合,可观测性靠时间分区 + 排序键,OTLP Collector 是接入的主路)。

最后一句提醒:**把不该 ClickHouse 做的事推给它,是集成阶段最常见的错误**。点查、事务、流状态、在线推理,各有其主。§30.16 的取舍清单值得贴在架构评审的墙上。
