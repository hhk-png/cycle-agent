# 附录 C:术语表

读 ClickHouse 的文档、报错和源码时,真正拖慢你的往往不是某个生词本身,而是几个**长得很像、意思却差很远**的词——`part` 与 `partition`、`主键` 与`排序键`、`replica` 与 `shard`、`ProfileEvents` 与 `CurrentMetrics`。这一份术语表按主题分组,每条给中文名、英文原文、一句话定义,并在最容易混的地方点明区别;每条末尾用"详见第 NN 章"指向正文里讲透它的地方。查的时候先按主题定位,再按英文名找行。

## C.1 怎么用这份术语表

- 想弄清一个词在讲什么 → 直接看该条定义;
- 被两个近义词绕住 → 先看条目里的"容易混淆"一句,再到 C.11 的"十组概念"总览;
- 想知道"展开在哪一章" → 看条目末尾的引用。

术语按**主题**分组,组内按英文名首字母排序。定义力求一句话;凡涉及数字默认值而正文未给定的,本文不强行给数,以免与版本脱节。

## C.2 存储与 part

- **`active part`(活跃 part)** —— 当前参与查询的 part;与之相对的是后台合并完成后被标记删除的 inactive part。`system.parts` 里用 `active` 列区分。详见第 05 章。
- **`Atomic`(原子数据库引擎)** —— 默认的数据库引擎(较新的版本);表的元数据以原子方式增删,支持 `RENAME` 的原子性与表 UUID。老式引擎叫 `Ordinary`。详见第 06 章。
- **`checksum`(校验和)** —— 每个 part 的列文件与索引文件都带校验,损坏可被检出。详见第 05 章。
- **`CODEC`(压缩编解码器)** —— 列级的压缩/编码声明(`CODEC(Delta, ZSTD)`),决定这一列怎么压、怎么存。通用压缩器(LZ4/ZSTD)负责压,专用编码器(Delta/DoubleDelta/Gorilla/T64)只改布局、不压缩,所以专用编码器必须**跟在一个通用压缩器前面**。详见第 20 章。
- **`column file`(列文件)** —— part 目录下每一列一个 `.bin` 文件,列式存储的物理落点。详见第 05 章。
- **`compact part`(紧凑 part)** —— 小 part 把各列挤进单个文件,减少小文件数量;超过阈值自动转成 wide part。详见第 05、20 章。
- **`data part`(数据块 / part)** —— MergeTree 在磁盘上的不可变数据单元,一次 `INSERT` 通常落成一个(或几个)part。**注意:它的"块"是数据单元,不是查询执行里的 block,也不是 partition。** 详见第 05 章。
- **`deduplication token` / `block id`(去重令牌)** —— 复制表用来判重的插入块标识,基于插入内容计算,让"客户端重试"不产生重复。详见第 22 章。
- **`disk`(磁盘)** —— ClickHouse 抽象的存储端点(本地盘、S3、HDD),可组合成 volume。详见第 29 章。
- **`Disk S3`(S3 磁盘)** —— 把对象存储挂成一种 disk 的实现,配合 storage policy 与 TTL 做冷热分层与归档。详见第 29 章。
- **`level`(层级)** —— part 的合并代数:新插入的 part level 为 0,每次被合并加一;合并器据此决定合并谁。详见第 05、18 章。
- **`lightweight delete`(轻量级删除)** —— `DELETE FROM t WHERE ...`:靠一个 `_row_exists` 标记列把行"标记删除",读时过滤掉。**它区别于 `ALTER ... DELETE` mutation,不重写整份 part**(后台再补合并),所以便宜得多。详见第 18 章。
- **`mark`(标记)** —— 指向某个 granule 在某列文件里偏移的指针,是"稀疏索引 → 列数据"的桥。**mark 是位置信息,不是数据、也不是 granule。** 详见第 05 章。
- **`mark file`(标记文件)** —— 存放 mark 的文件:宽 part 每列一个 `<列>.mrk2`(自适应粒度),紧凑 part 是单个 `data.mrk3`,旧的非自适应粒度用 `.mrk`。
- **`merge`(合并)** —— 后台把若干小 part 读出来、按排序键归并、写成更大 part 的过程,是 MergeTree 的核心机制。**merge 是"重写",不是"就地修改"。** 详见第 18 章。
- **`merge selector`(合并选择器)** —— 决定"这次合并哪些 part"的调度策略。详见第 18 章。
- **`MergeTree`(MergeTree 家族基类)** —— 这一族所有引擎的**基类**:`PARTITION BY`、`ORDER BY`(排序键)、稀疏主键索引、granule 与后台合并都在它这里定义。`*MergeTree` 家族(`ReplacingMergeTree`、`SummingMergeTree`、`AggregatingMergeTree`、`ReplicatedMergeTree` …)都是在基类上**再加一层"合并时怎么折叠"的语义**;"只有 `MergeTree` 家族才真正落库、才有分区裁剪与 TTL"这句话里的 `MergeTree`,指的正是这一族。详见第 04、05 章。
- **`mutation`(变更)** —— `ALTER TABLE ... UPDATE/DELETE` 触发的**异步、重写整份 part** 的数据修改。**mutation 不是"就地改一行",而是"重写 part"。** 详见第 18 章。
- **`partition`(分区)** —— 按分区键把数据切成的逻辑块;`DROP PARTITION` 能整块删,是"删旧数据"的正道。**partition 是一组 part 的集合,part 是它的组成单位。** 详见第 18 章。
- **`partition key`(分区键)** —— 决定行落到哪个 partition 的表达式(如 `toYYYYMM(ts)`)。详见第 06、18 章。
- **`part name`(part 命名)** —— 形如 `partition_id_minblock_maxblock_level_mutation` 的目录名,把 part 的身份写在名字里。详见第 05 章。
- **`primary key`(主键)** —— **只用于索引**,决定稀疏索引按什么顺序建、哪些 granule 能被裁剪;**不要求唯一**。详见第 06、11 章。
- **`projection`(投影)** —— 表内为某类查询预建的物化副本,查询时可自动改写命中。详见第 05、28 章。
- **`replica path`** —— 副本在 Keeper 里的路径,名字相同即指向同一份逻辑数据。详见第 22 章。
- **`ReplacingMergeTree`(替换去重引擎)** —— MergeTree 家族成员:后台合并时**按排序键去重、同一键只留一行**。可选一个**版本列**(如 `ver UInt64` 或 `DateTime`),合并时保留版本最大的那行;不写版本列则"留哪一条不确定"。**去重只发生在合并时**,合并没跑完就仍可能有重复行,所以查询侧要么 `FINAL`、要么 `argMax`,别以为写进去就唯一。详见第 04、18 章。
- **`sorting key`(排序键)** —— 决定 part 内数据**物理排列顺序**的表达式。**排序键是"数据怎么排",主键是"索引取谁",二者常相同但可不同。** 详见第 06、11 章。
- **`storage policy`(存储策略)** —— 把 volume/disk 分组,配合 TTL 做冷热分层。详见第 29 章。
- **`SummingMergeTree`(求和引擎)** —— MergeTree 家族成员:后台合并时把**排序键相同**的多行里、数值列**加起来**(非数值列取其中一条的值)。它只做"同键相加",所以只适合可累加的量;`avg` 之类不可累加的量要改用 `AggregatingMergeTree` 存状态。查询侧同样受"合并才收敛"的约束,常用 `sum()` + `FINAL`(或 `OPTIMIZE ... FINAL`)拿准数。详见第 04、12 章。
- **`table UUID`** —— Atomic 引擎给每张表分配的稳定标识,`RENAME` 后不变。详见第 06 章。
- **`TTL`(生存时间)** —— 按时间(或条件)自动删除/移动/汇总老数据的规则。详见第 18 章。
- **`volume`(卷)** —— 一组 disk 的集合,可配最大数据量,是分层存储的中间层。详见第 29 章。
- **`wide part`(宽 part)** —— 每列一个文件的常规 part 布局;数据量大时用。详见第 05、20 章。

## C.3 索引与裁剪

- **`adaptive granularity`(自适应粒度)** —— 不再"每 8192 行",而是同时按字节上限(`index_granularity_bytes`)划分 granule。详见第 11 章。
- **`bloom_filter`(布隆过滤索引)** —— 数据跳过索引的一种,用可空判定快速排除不含某值的 granule。详见第 11 章。
- **`data skipping index`(数据跳过索引)** —— 主键索引之外,为某列单独建的辅助索引(`minmax`/`set`/`bloom_filter` 等),用于跳过整块。详见第 11 章。
- **`granule`(粒度)** —— 索引与读取的最小单位,默认 8192 行(`index_granularity`)。**granule 不是压缩块,也不是 part;它是"一次能跳过的最小块"。** 详见第 05、11 章。
- **`index granularity`(索引粒度)** —— 每个 granule 的行数,建表时 `index_granularity` 指定,最常改的物理参数之一。详见第 11 章。
- **`minmax index`(最小最大索引)** —— 记录 granule 内某列的 min/max,范围外的查询条件可整块跳过。详见第 11 章。
- **`monotonic function`(单调函数)** —— 若 `WHERE` 里的函数对主键单调,裁剪仍能生效(如 `toDate(ts)`,当 `ts` 是主键前缀时);但这条折叠路径**脆弱、依赖具体类型对与版本**,可操作的做法仍是写成原始列的区间谓词。详见第 24 章。
- **`ngrambf_v1` / `tokenbf_v1`** —— 面向子串/分词的布隆索引变体,常用于 `LIKE`/全文过滤。详见第 11 章。
- **`partition pruning`(分区裁剪)** —— 按分区键直接从"分区集合"里划掉不相关分区,是最粗粒度、收益最大的裁剪。详见第 11 章。
- **`predicate pushdown`(谓词下推)** —— 把过滤条件尽量推到**读取层**去执行(分区/主键裁剪、自动提升成 `PREWHERE`、以及下推到外部表/远端),目标是"让更少的行进入上层算子"。它不是一个单独的算子,而是一类改写的统称。详见第 11、24 章。
- **`PREWHERE`** —— 先做的、代价更低的过滤;ClickHouse 默认会把可提前的 `WHERE` 条件自动提升为 `PREWHERE`。详见第 07、11 章。
- **`primary index`(主索引)** —— 主键对应的稀疏索引,常驻内存,是裁剪的第一道闸。详见第 11 章。
- **`primary key pruning`(主键裁剪)** —— 用 `WHERE` 与主键的关系算出"只需读哪些 granule"。详见第 11 章。
- **`SAMPLE`(采样读)** —— `SAMPLE k` 只读一部分数据来快速估个大概;**只有 MergeTree 且建表时声明了 `SAMPLE BY`** 才可用,且采样不保证统计均匀。详见第 07 章。
- **`secondary index`(二级索引)** —— 见"data skipping index",是同一件事的另一个叫法。详见第 11 章。
- **`set index`(集合索引)** —— 记录 granule 内某列的取值集合,等值/`IN` 过滤可跳过。详见第 11 章。
- **`sparse index`(稀疏索引)** —— 只为每个 granule 存一个键值的索引,体积极小,这是 ClickHouse 与 B 树的最大分野。详见第 11 章。
- **`key condition`(键条件)** —— 优化器把 `WHERE` 翻译成的"对主键坐标的区间限制"。详见第 11 章。

## C.4 查询执行

- **`analyzer`(分析器)** —— 新版查询分析与改写的前端;25.x 已默认启用,与旧的解释器并存过一段时间(确切开关以自检为准)。详见第 10、24 章。
- **`AST`(抽象语法树)** —— 解析后的语法结构,`EXPLAIN AST` 打印它。详见第 24 章。
- **`block`(块)** —— 查询执行时在算子间流动的**列式数据批次**(一批列,每列若干行)。**注意与磁盘上的 data part 区分。** 详见第 10 章。
- **`exchange`(数据交换)** —— 并行/分布式阶段之间搬运、切分数据的中转。详见第 10 章。
- **`expression`(表达式)** —— 编译后可对 block 逐列求值的计算单元。详见第 10 章。
- **`final` / `FINAL`(读时合并)** —— 查询修饰符,读取时即时做合并语义(去重/取最新),代价高。详见第 04、28 章。
- **`interpreter`(解释器)** —— 把逻辑计划变成可执行 processor 管线的组件。详见第 10 章。
- **`pipeline`(管线)** —— 一组 source/transform/sink processor + port 组成的执行图。详见第 10 章。
- **`pipeline_dag`** —— `EXPLAIN PIPELINE` 打印的执行图。详见第 10 章。
- **`port`(端口/连接)** —— processor 之间的数据出入口,分输入/输出两类。详见第 10 章。
- **`prepared statement`(预备语句)** —— 用占位符(`{name:Type}`)预编译 SQL、复用解析结果并安全传参的写法。**各客户端支持的形态不一致**(原生协议有参数化查询,HTTP 接口靠 `param_*`),确切支持以你用的驱动为准(**未确认**)。详见第 02 章。
- **`processor`(处理器)** —— 管线上一个执行单元(读表、算表达式、聚合、排序、写出各是一个)。详见第 10 章。
- **`query cache`(查询缓存)** —— 服务端把**确定性 `SELECT`** 的结果缓存下来,同样的查询直接返回缓存。**默认关闭**(`use_query_cache=0`),要显式开启并配 `query_cache_ttl`;键是查询文本 + 设置,改一个空格都可能不命中。`system.query_cache` 看现状。详见第 28 章。
- **`query plan`(查询计划)** —— 从 SQL 到执行的中间表示;分逻辑计划与物理(管线)计划。详见第 24 章。
- **`shuffle`(重分布)** —— 为并行聚合/JOIN 把数据按 key 重新分发给各线程/节点。详见第 10、23 章。
- **`sink` / `source` / `transform`** —— processor 的三种角色:只写、只读、读写。详见第 10 章。
- **`spill` / `external aggregation`(溢写 / 外存聚合)** —— 内存超阈值时把中间结果写到磁盘继续算,避免 OOM。详见第 25 章。
- **`thread pool`(线程池)** —— 执行查询、后台合并、复制各自有独立的池;线程数由 `max_threads` 等控制。详见第 25 章。
- **`two-level aggregation`(两级聚合)** —— 键太多时把哈希表分桶、降低峰值内存的聚合方式。详见第 12、25 章。
- **`vectorization` / `SIMD`** —— 按列批量处理一整个 block、借 CPU 向量指令加速,是 ClickHouse 快的根因之一。详见第 10 章。

## C.5 数据类型

- **`AggregateFunction(...)`** —— 存聚合**中间状态**的列类型,`-State` 写入、`-Merge` 读出。详见第 12 章。
- **`Array(T)`** —— 数组,下标 **1-based**,越界返回该元素类型的默认值。详见第 03、17 章。
- **`Bool`** —— 布尔类型,底层存储为 `UInt8`。详见第 03 章。
- **`Date` / `Date32` / `DateTime` / `DateTime64`** —— 日期与时间类型,时区是 `DateTime` **列的属性**而非会话属性。详见第 03、17 章。
- **`Decimal(P, S)`** —— 定点数,`P` 总位数、`S` 小数位;有 `Decimal32/64/128/256` 别名。详见第 03 章。
- **`Domain`** —— 某个基础类型的别名(如给 `IPv4` 起业务名),不新增存储。详见第 03 章。
- **`Dynamic` / `Variant` / `JSON`** —— 半结构化类型族。原生 `JSON` 自 **25.3 起生产就绪、并默认开启**(由 `enable_json_type` 控制,该设置自 25.3 起默认 `true`),可直接用于生产;`Variant`、`Dynamic` 同属这套体系,服务于"一列里装多种类型"的建模。详见第 03、32 章。
- **`Enum8` / `Enum16`** —— 具名枚举,取值是 `'name' = number` 的映射。详见第 03 章。
- **`FixedString(N)`** —— 定长字节串,常用于哈希摘要、IP。详见第 03 章。
- **`Float32` / `Float64`** —— IEEE 浮点;不适合作金额。详见第 03 章。
- **`Int8/16/32/64/128/256` / `UInt...`** —— 定宽整数族,运算会按"进一档"提升类型。详见第 03、17 章。
- **`IPv4` / `IPv6`** —— IP 类型,`IPv4` 内部是 `UInt32`,`IPv6` 是 16 字节。详见第 03 章。
- **`LowCardinality(T)`** —— 字典编码的低基数列包装,适合枚举式字符串。详见第 03 章。
- **`Map(K, V)`** —— 映射,存储上就是 `Array(Tuple(K, V))`。详见第 03、17 章。
- **`Nested(...)`** —— 嵌套结构,展开成多个等长的数组列。详见第 03 章。
- **`Nullable(T)`** —— 可空包装,多存一个空标记列,会拖慢、一般尽量避免。详见第 03 章。
- **`Point` / `Ring` / `Polygon` / `MultiPolygon`** —— 几何类型。详见第 03 章。
- **`SimpleAggregateFunction(...)`** —— 可"就地合并"的聚合状态(如 `sum`/`max`),比 `AggregateFunction` 简单。详见第 12 章。
- **`String`** —— 任意长度、**二进制安全**的字节序列;`length` 数字节、`lengthUTF8` 数码点。详见第 03、17 章。
- **`Tuple(T1, ...)`** —— 元组,每个位置保留自身类型,可具名。详见第 03、17 章。
- **`UUID`** —— 16 字节唯一标识。详见第 03 章。

## C.6 聚合与函数

- **`aggregate function`(聚合函数)** —— 把一整列(或分组)折叠成一个值/状态的函数。详见第 12 章。
- **`argMax` / `argMin`** —— 取"另一列取极值处"的那一列的值。详见第 12、17 章。
- **`combinator`(组合器)** —— 给聚合函数加后缀(`-If`/`-Array`/`-State`/`-Merge`/`-Distinct`/`-OrNull`/`-OrDefault`/`-Resample`/`-ForEach`)生成新函数,可叠加。详见第 12、33 章。
- **`constant folding`(常量折叠)** —— 把可在分析期算好的表达式提前算成常量;非确定性函数(`now`/`rand`)不能被折叠。详见第 17、24 章。
- **`deterministic function`(确定性函数)** —— 同输入恒同输出的函数;相对的是 `now`/`rand`/`generateUUIDv4`。详见第 17 章。
- **`higher-order function`(高阶函数)** —— 参数里带 lambda 的函数(`arrayMap`/`arrayFilter`/`arrayFold`)。详见第 17 章。
- **`lambda`** —— 匿名函数语法 `x -> expr` 或 `(x, y) -> expr`,可捕获外层列。详见第 17 章。
- **`-Merge`** —— 组合器,吃 `-State` 产生的状态并合并成结果。详见第 12、33 章。
- **`-State`** —— 组合器,返回聚合中间状态而非结果,用于写入 `AggregatingMergeTree`。详见第 12、33 章。
- **`quantile` / `median`** —— 分位数 / 中位数聚合,默认是近似实现(如 TDigest)。详见第 12 章。
- **`scalar function`(标量函数)** —— 一行进一行出(逐列求值)的函数,与聚合函数相对。详见第 17 章。
- **`state`(聚合状态)** —— 聚合未合并完成的中间表示,类型是 `AggregateFunction(...)`。详见第 12 章。
- **`uniq` / `uniqExact` / `uniqHLL12` / `uniqCombined`** —— 去重计数,前近似、后精确,精度与内存是权衡。详见第 12 章。

## C.7 物化视图与建模

这一组是"把机制变成设计决定"的那批词:物化视图的几种形态、预聚合的落点,以及表设计时反复要判断的几个概念。`第 13 章` 讲机制,`第 14 章` 讲方法论。

- **`AggregatingMergeTree`** —— MergeTree 家族里**存聚合状态**的成员:列类型是 `AggregateFunction(...)` / `SimpleAggregateFunction(...)`,后台合并会把同一分组的多份状态合并掉。它是物化视图做增量聚合的默认落点。详见第 12、13 章。
- **`async_insert`(异步插入)** —— 写入侧的一层攒批:客户端把小块丢给服务端后,服务端**先攒在内存里、攒够阈值或超时才落成 part**,以减少"高频小块写入"制造的无数小 part。它**不是去重**,也不改变数据内容;`wait_for_async_insert=1`(默认)让客户端等服务端落盘确认,设成 `0` 则客户端立刻返回、此时读自己刚写的数据可能落空。它和物化视图同属"改写入路径"的机制,写放大要一起算。详见第 08 章;排错见第 36 章 §D.7。
- **`backfill`(回填)** —— 给"视图建立之前就已经存在"的数据补算历史。`TO` 形式下没有 `POPULATE` 可用,正确做法是先建视图、再手动 `INSERT INTO 目标 SELECT ... FROM 源`。详见第 13 章 §13.7。
- **`grain`(粒度)** —— 预聚合表的一行代表"多大范围的数据"。粒度选细了存不下,选粗了答不了问题;分层建模本质上就是在几个粒度上各存一份。详见第 14 章 §14.8。
- **`incremental aggregation`(增量聚合)** —— 写入时就把聚合**状态**算好并落盘,查询时只做状态合并;相对的是"每次查询都重扫明细"。详见第 13 章。
- **`MaterializedView`(物化视图)** —— **插入触发器**,不是查询缓存;**建视图之前的历史数据不会自动进来**。写时算、结果**追加**进目标表,所以目标表通常是会在后台收敛的引擎。详见第 13 章,另见第 04 章 §04.8、第 12 章 §12.10。
- **`MV chain`(物化视图链)** —— 一条 MV 的目标表同时是下一条 MV 的源表,用来做多级 rollup。链上的中间层一般也必须是 `AggregatingMergeTree`,否则上一级算出的**结果**会在下一级被当成原始行再聚合一次。详见第 13 章 §13.6。
- **`POPULATE`** —— 建视图时一次性回填历史数据。**与 `TO` 互斥**,且**带竞态**(回填期间写进源表的行可能漏掉或重复)。详见第 13 章 §13.3、§13.7。
- **`projection`(投影)** —— 表**内部**的一份额外排序副本,优化器可以自动改写命中;**只服务单表查询**,带 `FINAL` 的查询不会用它。它和物化视图不是一回事:投影是表的一部分、由优化器自动使用;物化视图是独立对象、通常要显式查。详见第 11 章 §11.12、第 06 章 §06.11。
- **`Refreshable Materialized View`(可刷新物化视图)** —— 带 `REFRESH EVERY ...` 的 MV:按周期**重跑整段查询**,对"整个源表的当前状态"负责,`APPEND` 决定追加还是覆盖。旧的实验开关 `allow_experimental_refreshable_materialized_view` 自 24.10 起已废弃,25.3 上直接可用。详见第 13 章 §13.8。
- **`rollup`(汇总表)** —— 按更粗粒度预先算好的表。ClickHouse 里它通常就是一张 `AggregatingMergeTree` 加一条物化视图。详见第 14 章 §14.7。
- **`View`(普通视图)** —— 只存 SQL,查询时重跑子查询,不存数据,而且不可写。详见第 04 章 §04.8。
- **宽表(wide table)vs 星型(star schema)vs 规范化(normalization)** —— ClickHouse 偏好**大宽表**:查询少 JOIN、排序键能覆盖更多过滤条件。星型/雪花在这里往往是负担而不是资产。详见第 14 章 §14.6。

## C.8 复制与分布式

- **`absolute_delay`(绝对延迟)** —— 副本落后主副本的秒数,`system.replicas` 里的健康指标。详见第 22、27 章。
- **`cluster`(集群)** —— 在配置里命名的一组分片 × 副本拓扑。详见第 23 章。
- **`Distributed`(分布式表)** —— **不存数据**的分片代理:写按分片键转发,读拆成子查询再汇总。详见第 23 章。
- **`Distributed DDL` / `ON CLUSTER`(分布式 DDL)** —— 一条 DDL 在集群每个节点上各自执行;靠 Keeper 里的 DDL 队列协调,进度看 `system.distributed_ddl_queue`。**它是"每个节点各跑一遍",不是"执行一次全局生效"**——某节点卡住,整个 `ON CLUSTER` 就卡住。详见第 09、22 章。
- **`distributed_product_mode`** —— 控制跨分片 JOIN 是否允许及相关形态的设置。详见第 23 章。
- **`fetch`(拉取 part)** —— 副本从别的副本或对象存储取缺失 part 的过程。详见第 22 章。
- **`GLOBAL JOIN` / `GLOBAL IN`** —— 在分布式查询里,把右表先汇总再广播,**是正确性问题而非优化**。详见第 23 章。
- **`Keeper`** —— ClickHouse 自实现的、兼容 ZooKeeper 协议的协调服务,替代 ZooKeeper。详见第 22 章。
- **`leader election`(选主)** —— 副本间选出负责合并的一方,避免重复合并。详见第 22 章。
- **`macro`(宏)** —— 配置里按副本替换的占位符(`{shard}`/`{replica}`),用于统一写建表语句。详见第 22、23 章。
- **`quorum insert`(法定写入)** —— 要求至少 N 个副本确认写入成功,让客户端重试更安全。详见第 22 章。
- **`replica`(副本)** —— 同**一份**数据的多个物理拷贝,解决**高可用**。**副本≠分片:副本是同一份数据的冗余,分片是不同份数据的分区。** 详见第 22 章。
- **`replication log`(复制日志)** —— 一份按序执行的待办清单,副本据此重放操作。详见第 22 章。
- **`replication queue`(复制队列)** —— 本副本还没执行完的复制任务列表。详见第 22、27 章。
- **`remote_servers`** —— 配置里定义集群拓扑的段。详见第 23 章。
- **`ReplicatedMergeTree`** —— 带 `Replicated` 前缀的 MergeTree 族,通过 Keeper 做复制。详见第 22 章。
- **`resharding`(重分片)** —— 改变分片数/分片键后搬迁数据的过程。详见第 23 章。
- **`sequential consistency`(顺序一致读)** —— `select_sequential_consistency=1` 时,只读已确认写入的一致快照。详见第 22 章。
- **`shard`(分片)** —— 数据被切成**不同份**、分布到不同节点,解决**容量与并行**。**分片≠副本。** 详见第 23 章。
- **`sharding key`(分片键)** —— 决定一行落到哪个分片的表达式。详见第 23 章。
- **`ZooKeeper`** —— 外部协调服务;ClickHouse 曾依赖它,现多用内置 Keeper。详见第 22 章。

## C.9 运维与监控

- **`asynchronous_metrics`(异步指标)** —— `system.asynchronous_metrics`,周期性采集的系统级度量(磁盘剩余、内存等),不给采样时刻。详见第 27 章。
- **`background pool`(后台池)** —— 执行 merge、mutation、TTL、fetch 的线程池;打满意味着后台跟不上。详见第 18、27 章。
- **`CurrentMetrics`(当前指标)** —— `system.metrics` 里的**瞬时值**(量),如当前内存占用、连接数、正在跑的 merge 数。**CurrentMetrics 是"此刻是多少",ProfileEvents 是"累计多少"。** 详见第 27 章。
- **`DDL queue`** —— 等待执行的复制 DDL 语句队列(`system.distributed_ddl_queue`)。详见第 22、27 章。
- **`DelayedInserts`(延迟写入)** —— 因 part 过多被人为拖慢的插入数,`system.metrics` 里不为零就是写入被打压。详见第 27 章。
- **`events`(事件计数)** —— `system.events`,只增不减的 **counter**,需采样算速率。详见第 27 章。
- **`max_server_memory_usage`** —— 服务端可用内存上限,逼近它就有 OOM 风险。详见第 25、29 章。
- **`merge throttle`(合并限流)** —— 限制同时/累计合并资源,防止合并压垮查询。详见第 18、29 章。
- **`mutation`** —— 见 C.2;运维上要盯 `system.mutations` 的进度与失败。详见第 18、27 章。
- **`OPTIMIZE`** —— 手动触发 merge(可带 `FINAL`/`DEDUPLICATE`/`PARTITION`);对大表是昂贵操作。详见第 09、18 章。
- **`part_log`(part 日志)** —— `system.part_log`,记录 part 的增删合并事件。详见第 27 章。
- **`ProfileEvents`(画像事件)** —— 每条查询各自统计的事件计数(`SelectedRows`/`ReadBytes` 等),`system.query_log.ProfileEvents` 与 `system.events` 同源。**它是累计计数,不是当前值。** 详见第 27 章。
- **`query_log`(查询日志)** —— `system.query_log`,慢查询分析的主表(**默认开启**;它按 `flush_interval_milliseconds`(默认 7.5 秒)异步落盘,所以刚跑完的查询要 `SYSTEM FLUSH LOGS` 才看得到)。详见第 27、36 章。
- **`readonly replica`(只读副本)** —— 因 Keeper 失联等原因进入只读的副本,`ReadonlyReplica` 指标可查。详见第 22、27 章。
- **`Role` / `Row policy` / `Quota`(角色 / 行策略 / 配额)** —— 访问控制三件套:**角色**把一组权限打包装给用户,**行策略**对查询做行级过滤(不只是能否读表,而是能读哪些行),**配额**限制用户在时间窗口内的资源用量(查询数、读写行数、执行时间)。详见第 26 章。
- **`system tables`(系统表)** —— `system.*` 下的元数据/监控表,是自查的第一站。详见第 27 章。
- **`text_log`(文本日志)** —— `system.text_log`,默认**不开启**,需配置 `<text_log>` 段。详见第 27、36 章。
- **`unmerged parts`(未合并 part)** —— 尚未被后台合并的 part,过多会拖慢查询并触发写入保护。详见第 18、27 章。

## C.10 生态与工具

这一组是"ClickHouse 之外、但你迟早会碰到"的东西:嵌入式引擎、命令行工具、协议与接口、外部数据源的门、湖仓工具、托管与周边项目。术语表里它原本最薄,这里补厚一点。

- **`chDB`** —— 把一个类 ClickHouse 的引擎**嵌进 Python 进程**的库:进程内直接跑 SQL,没有服务端。**和谁比:** 定位接近 DuckDB(单机、面向分析文件与 DataFrame),但走的是 ClickHouse 的 SQL、类型与格式。**边界:** 它是**库不是服务**——没有端口、没有并发用户与配额管理、没有集群与副本,也别指望它复刻服务端的后台合并与复制语义;要这些就得用真 ClickHouse(第 01、32 章)。
- **`chproxy`** —— 常见的第三方 **HTTP** 代理/负载均衡器,做限流、路由、缓存与认证。**和谁比:** 它不在 ClickHouse 进程内,而是站在客户端与 `:8123` 之间的独立进程;**边界:** 它只治理**流量**,不改变 SQL 语义,也不代理原生协议(:9000)。详见第 30 章。
- **`clickhouse-client`** —— 官方交互式命令行客户端。详见第 02 章。
- **`clickhouse-keeper`** —— 内置 Keeper 的独立进程,可替代 ZooKeeper。详见第 22 章。
- **`clickhouse-local`** —— 不启服务端、直接对文件跑 SQL 的本地工具。详见第 30 章。
- **`ClickHouse Cloud`** —— 官方托管服务,提供 `SharedMergeTree` 等仅云上有的特性。详见第 29、32 章。
- **`clickhouse-wasm` / Wasm 构建** —— 把 ClickHouse 编译到 WebAssembly、在浏览器或边缘进程内运行;较新,生态仍在演进,**成熟度未确认**。详见第 32 章。
- **`Delta Lake` / `Iceberg` / `Hudi`(湖表格式)** —— 开放的表格式(数据文件 + 元数据/事务日志)。ClickHouse 读它们走的是**"表函数为主、也有对应引擎形态"**这一套(如 `iceberg()` / `deltaLake()` 表函数,以及同名的表引擎),**不是**像 `MergeTree` 那样一个单纯的存储引擎。**写能力要按版本分清:在本书对标的 25.3 上只有读、没有任何湖表写入能力**——`Iceberg` 的 `INSERT` 自 26.2 起才进入 beta,Delta 写入仍是实验特性(开关 `allow_experimental_delta_lake_writes`),`Hudi` / `Paimon` 只读。口径与附录 A 的 A.19/A.20 一致。详见第 30、32 章。
- **`Dictionary`(字典)** —— 用于 `dictGet` 的键值映射结构,有独立 SOURCE/LAYOUT/LIFETIME。详见第 21 章。
- **`HTTP interface`(HTTP 接口)** —— 通过 HTTP 发 SQL 的接口,端口默认 `8123`。详见第 02、26 章。
- **`integration engine`(集成引擎)** —— `Kafka`/`MySQL`/`PostgreSQL`/`S3`/`HDFS` 等"外部数据的门",非本地存储。详见第 30 章。
- **`Named collection`(命名集合)** —— 把凭据/连接参数命名复用(如 S3、Kafka 的密钥),避免把密码写进每一条 DDL。详见第 26、30 章。
- **`Native protocol`(原生协议)** —— 客户端与服务端之间的高效二进制协议,端口默认 `9000`。详见第 02 章。
- **`ODBC` / `JDBC` bridge** —— 通过桥接进程访问 ODBC/JDBC 数据源。详见第 30 章。
- **`S3Queue`(S3 队列引擎)** —— 把对象存储上的文件按到达顺序当**队列**消费,像 Kafka 那样做"流落库",但不必维护 Kafka 集群。详见第 30 章。
- **`table function`(表函数)** —— 用在 `FROM` 里的函数(如 `numbers`/`remote`/`s3`/`url`),把外部数据当表读。详见第 30 章。

## C.11 最容易混淆的十组概念

1. **part vs partition** —— part 是磁盘上不可变的数据块,partition 是"按分区键归到一组"的 part 集合;一个 partition 含多个 part。存储机制见第 05 章,分区操作见第 18 章。
2. **主键(primary key) vs 排序键(sorting key)** —— 排序键决定 part 内数据的物理顺序,主键决定稀疏索引取哪些点;主键必须是排序键的前缀。见第 06、11 章。
3. **granule vs mark** —— granule 是"行的范围"(默认 8192 行),mark 是指向这个范围在列文件里位置的指针;一个 granule 对应一个 mark。见第 05、11 章。
4. **merge vs mutation** —— merge 是后台把小 part 归并成大 part 的常态机制;mutation 是 `ALTER UPDATE/DELETE` 触发的"按需重写"。两者都会重写 part,但触发者与语义不同。**还有第三种:轻量级删除**(`DELETE FROM`,靠 `_row_exists` 标记列,读时过滤),它既不 merge 也不重写整份 part,别和 mutation 混为一谈。见第 18 章。
5. **ALTER(改元数据)vs ALTER(改数据)** —— 加列/改注释是秒级元数据改动;`ALTER ... UPDATE`/`DELETE`/改类型是异步 mutation;同一个关键字,代价天差地别。**不带 `ALTER` 的轻量级 `DELETE FROM` 落在两类之外**(靠隐藏的 `_row_exists` 掩码列标记删除、不重写整份 part,通常便宜得多)——它早已可用(22.8 引入);而轻量级 `UPDATE ... SET`(靠 patch part 在读取时叠加)是更晚才有的能力:**它 25.7 才以实验特性出现、25.8 转入 Beta(开关 `enable_lightweight_update`),在本书对标的 25.3 上尚未提供**,别照着语法建生产流程。见 C.2 与本表第 4 条,详见第 09、18 章。
6. **副本(replica)vs 分片(shard)** —— 副本是同一份数据的冗余(管高可用),分片是不同份数据的分区(管容量)。见第 22、23 章。
7. **ProfileEvents vs CurrentMetrics** —— 前者是**累计计数**(只增,算速率),后者是**瞬时值**(此刻多少)。见第 27 章。
8. **分区键 vs 排序键** —— 分区键决定"行落到哪个分区",排序键决定"分区内怎么排"。选错分区键会制造海量小 part,选错排序键会让裁剪失效。见第 06、11、18 章。
9. **`FINAL` vs `OPTIMIZE ... FINAL`** —— `SELECT ... FINAL` 是**读时**即时合并(不动数据);`OPTIMIZE ... FINAL` 是**写时**强制合并(真的重写 part)。前者的代价随查询走,后者是一次昂贵的全表重写。见第 04、18、28 章。
10. **View vs MaterializedView vs LiveView vs Refreshable MV** —— 普通视图 = 查询时重跑;物化视图 = 插入触发器(写时算,存结果);LiveView = 变更时推送(`WATCH`);**Refreshable MV** = `REFRESH EVERY` 的定时重跑(旧实验开关已废弃,25.3 直接可用)。四者的分野是"数据什么时候进来",别把普通物化视图当成会自动刷新的定时任务。见 C.7 与第 13 章。

## C.12 小结

- 记住一条主线:`part` 是存储单位、`partition` 是分组、`granule` 是读取单位、`mark` 是指针——这四个词常被混着说,但各管一层。
- 主键与排序键是两件事,前者服务索引、后者服务物理顺序;分区键与排序键也是两件事,前者决定文件怎么分、后者决定文件内怎么排。
- 改数据(`mutation`)和改元数据(普通 `ALTER`)是两个量级;`merge` 是常态、`FINAL` 是读时补语义;而**轻量级删除**(`DELETE FROM`)是第三类,标记式、不重写整份 part。
- 物化视图有**四种形态**(普通 View、插入触发器式 MV、LiveView、可刷新 MV),它们的"数据什么时候进来"完全不同;`query cache` 缓存的是**确定性 SELECT 的结果**,和 `mark cache`/`uncompressed cache` 缓存的缓存对象不是一回事。
- 运维上最该分清的是 `ProfileEvents`(累计计数)与 `CurrentMetrics`(瞬时值);而在集群语义上,最该分清的是 `replica`(冗余)与 `shard`(分区)。
- 术语不熟时,除了这份表,`system` 库本身就是最好的字典:任何"这个词到底指什么"的疑问,通常能用一条 `SELECT ... FROM system.*` 直接问出答案(第 27 章)。
