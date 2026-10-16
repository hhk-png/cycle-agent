# 第 06 章 表结构与 DDL

建表是 ClickHouse 里最容易"一次写错、后面几个月一直在还债"的操作。`CREATE TABLE` 里那几行看起来只是语法,实际上是一次性的、几乎不可撤销的架构决策:`ORDER BY` 一旦定下,后续所有写入的物理顺序、所有范围查询能裁掉多少数据、压缩率有多高,全部被它锁死;`PARTITION BY` 选得太细,后台会因为分区数爆炸而拖着 merge 一起崩;`DEFAULT` / `MATERIALIZED` / `ALIAS` 用混了,你会看到"插进去的和读出来的不是一个东西"。

这一章从"怎么建库"讲起,再把 `CREATE TABLE` 拆成"每个参数影响什么"分别说,重点是让人能自己判断:**我这一行写下去,以后查询会变成什么样**。合并、mutation、TTL 的执行细节留给第 18 章(这里只把 TTL 的 DDL 语法摆全),存储结构(part / granule / mark)留给第 05 章。章末还给一份其他 DDL 对象的清单式概览,展开在第 09 章。

## 06.1 CREATE DATABASE 与数据库引擎

建表之前得先有个库。`CREATE DATABASE` 的骨架比建表短得多:

```sql
CREATE DATABASE [IF NOT EXISTS] db_name
[ON CLUSTER cluster_name]
[ENGINE = engine(...)]
[SETTINGS ...]
[COMMENT '注释']
```

关键是 `ENGINE`——它选的是**数据库引擎(database engine)**,决定"这个库的元数据怎么管、DDL 能不能原子地改"。**新库一律选 `Atomic`(默认)**:它把每张表的元数据放进一个带 UUID 的目录,于是 `RENAME TABLE` 与 `EXCHANGE TABLES` 是**原子**的、`UNDROP TABLE` 也因此可能,它还是 `ReplicatedMergeTree` 用 `{uuid}` 宏路径的前提(§06.14)。至于 `Ordinary` / `Replicated` / `Memory` / `Lazy` 这几个库引擎各自怎么组织表、怎么和副本协作,以及 `MySQL`、`PostgreSQL` 这些"远端库引擎"如何把外部数据源映射成一个库,第 04 章 §04.9 已经逐一讲清了,这里不再重复——建表只需要记住"默认选 `Atomic`"这一条。

还有一个常被提起、却**在 25.3 上并不存在**的库级设置,通常写作 `SETTINGS lazy_load_tables = 1`:据说它让服务端启动时**不把库里的表全部加载进内存**,而是先建轻量代理、第一次访问某张表时才真正加载,库里有成百上千张表时能压启动时间和常驻元数据。**这个设置在本书对标的 25.3 上确实没有**——它不是"文档没写清",而是**它到 25.3 之后才被加进 `Atomic` 库引擎**(默认 `0`),属于上游移除 `Lazy` 库引擎后给出的替代能力(§04.9 按版本讲过这条时间线)。所以在 25.3 上按这个名字写 `CREATE DATABASE ... SETTINGS lazy_load_tables = 1` 是无效的;要在目标版本上用它,先确认那个版本是否已经引入。

库建好之后,下面才有表可建。这一章余下的部分都在讲 `CREATE TABLE`。

## 06.2 CREATE TABLE 的完整骨架

一个完整的 MergeTree 建表语句长这样:

```sql
CREATE TABLE IF NOT EXISTS db.hits
(
    id        UInt64,
    user_id   UInt64,
    url       String,
    ts        DateTime,
    region    LowCardinality(String) DEFAULT 'unknown',
    day       Date MATERIALIZED toDate(ts),
    url_len   UInt32 ALIAS length(url),
    payload   String CODEC(ZSTD(3)),
    INDEX idx_url url TYPE bloom_filter(0.01) GRANULARITY 4,
    CONSTRAINT url_not_empty CHECK length(url) > 0,
    PROJECTION by_user (SELECT user_id, count() GROUP BY user_id)
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(ts)
PRIMARY KEY (user_id, ts, id)
ORDER BY (user_id, ts, id)
SAMPLE BY id
TTL ts + INTERVAL 90 DAY DELETE
SETTINGS index_granularity = 8192
```

括号里是**列定义和表级结构**(列、跳过索引、约束、投影)。括号外是**存储语义**,顺序上属于引擎的参数:`ENGINE` 决定语义(第 04 章),`PARTITION BY` 决定数据按什么切开,`ORDER BY` 决定 part 内的物理顺序,`PRIMARY KEY` 决定稀疏索引,`SAMPLE BY` 决定 `SAMPLE` 子句怎么取样,`TTL` 决定数据什么时候被清理或搬走,`SETTINGS` 是**MergeTree 引擎自己的建表参数**。

这里有个非常容易混的点:语句末尾的 `SETTINGS` 不是查询级的 `SET`,它写进表的元数据、随表持久化,只对这一张表生效。`SET index_granularity=...` 改的是会话,`CREATE TABLE ... SETTINGS index_granularity=...` 改的是这张表永久的存储行为。

这里先把 `ORDER BY` 与 `PRIMARY KEY` 的关系摆清楚:两者都是排序键,但分工不同——`ORDER BY` 决定数据在磁盘上的物理顺序,**`PRIMARY KEY` 必须是 `ORDER BY` 的前缀**,不写就默认等于 `ORDER BY`。上面那条骨架之所以把 `PRIMARY KEY` 写成 `(user_id, ts, id)`、而不是只到 `(user_id, ts)`,原因就在 `SAMPLE BY`。`SAMPLE BY` 是给 `SELECT ... SAMPLE` 用的**采样键**:它必须能稳定地把行映射到整数块(通常取自增 id 或时间戳),并且**它的采样表达式必须出现在主键里**——否则建表会直接报 `BAD_ARGUMENTS: Sampling expression must be present in the primary key`。这里的采样键是 `id`,所以 `id` 必须落在主键覆盖的范围内;平时为了省索引体积可以让主键短于排序键(§06.4),但一旦用了 `SAMPLE BY`,被采样的那一列就必须留在主键里。没有 `SAMPLE BY` 的表,`SAMPLE` 没有可依据的划分,只能得到随机行而不是可复现的子集。`TTL` 决定数据什么时候被删除、搬到别的磁盘卷或被重新压缩,细节在第 18 章。

## 06.3 排序键:这张表最重要的决定

`ORDER BY` 表达式是**数据在磁盘上的物理排序依据**,它一次性决定了三件事:

- **part 内数据有序**。于是二分查找、主键区间裁剪、`ORDER BY` 免排序都成为可能;
- **稀疏索引的内容**。索引里存的是每个 granule 第一行的键值,也就是 `ORDER BY` 表达式的值;
- **哪些 `WHERE` 能被快速裁剪**。只有 `ORDER BY` 前缀上的条件才能被翻译成 key range,剩下的一律退化成逐 granule 扫描或者干脆全扫。

由此得到最重要的一条规则:**把最常用于过滤的列放在排序键的最左边,按"过滤时能不能用上等值/范围条件"排序,而不是按重要性排序。** `WHERE user_id = ? AND ts BETWEEN ? AND ?` 这种形状,排序键就应该写成 `(user_id, ts)`,而不是 `(ts, user_id)`——虽然后者也能裁掉时间范围,但它裁不掉 `user_id`。

另一条规则:**`ORDER BY` 不等于"查询时的排序"**。你写 `SELECT ... FROM t ORDER BY x`,如果 `x` 不是排序键前缀,ClickHouse 要真的把结果排一遍,那是一次带内存上限的操作(超出会落盘,见 `max_bytes_before_external_sort`)。排序键选得好,很多查询的 `ORDER BY` 是"免费的"。

低基数列(比如 `region`、`status`)放在排序键最左边通常**是错的**:它几乎不能减少要读的 granule 数,却让索引前缀的区分度变差。这类列的加速手段是 `LowCardinality` 编码和第 11 章讲的跳过索引。

## 06.4 主键与排序键的区别

在 MySQL 里"主键"意味着唯一约束和聚簇索引;在 ClickHouse 里**它既不唯一、也不是行标识**,它只表达一件事:**哪些前缀会被写进稀疏索引、用来加速查找**。

两条硬规则:

- `PRIMARY KEY` 必须是 `ORDER BY` 的**前缀**(写 `ORDER BY (a, b, c)` 时,主键只能是 `(a)`、`(a, b)`、`(a, b, c)` 这样的前缀);
- **不写 `PRIMARY KEY` 时,它默认等于 `ORDER BY`**。

那为什么要分开写?因为"索引更短"有时候是划算的:索引越短,每个 granule 的键越少、索引文件越小、常驻内存越容易。`ORDER BY (user_id, ts, id)` 加上 `PRIMARY KEY (user_id, ts)` 的意思是"数据按三元组排好,但索引只到 `(user_id, ts)` 这一级"——用一点点裁剪精度换索引体积。

**错误做法**:把 `PRIMARY KEY` 当成唯一约束,靠它去重。**后果**:重复行照样存在,而且是对外可见的。**正确做法**:去重语义由引擎提供(`ReplacingMergeTree`,见第 04 章),而且要知道它也只是"最终一致"。

还有一类**没有排序键**的表,写法是把排序键写成**空元组**:

```sql
CREATE TABLE raw_stage (
    ts   DateTime,
    body String
) ENGINE = MergeTree
ORDER BY tuple();
```

这时的稀疏索引里只有一条(空元组),`primary.idx` 再也裁不掉任何 granule——**主键裁剪彻底失效,只剩下分区裁剪和列裁剪**(§05.1 的三层里少了中间那层),数据仍按写入顺序落盘。它适用的场景很窄:"只追加、几乎不按范围过滤、靠分区整块淘汰"的落地表(纯写入的暂存表、一次性 `INSERT ... SELECT` 出来的中间表)。一旦这张表上出现按某列的等值或范围查询,空排序键就是最坏的选择——那条查询只能全表扫。两种写法要分清:**显式**写出的 `ORDER BY tuple()` 任何情况下都能建出空排序键;而**把 `ORDER BY` 整句省略**(这才是真正的"什么都不写")时能不能建出空主键,受 `create_table_empty_primary_key_by_default` 控制(§06.15)——在 25.3 上它默认 `0`,省略 `ORDER BY` 会被直接拒绝。至于显式写 `ORDER BY ()` 这种空主键是否另受别的开关约束,**未确认**(25.3 上未实测,上游文档也没有逐字说明),所以稳妥的写法始终是显式写出 `ORDER BY tuple()`。

主键与排序键的完整区别(什么是前缀、截前缀换来的索引体积与裁剪范围的换算)在第 11 章 §11.2.2 展开,这里只留建表时的那条判断。

## 06.5 分区键怎么选

`PARTITION BY` 是**数据淘汰与移动的单位**,不是"查询加速的单位"——加速主要靠排序键和跳过索引。选分区键的判断标准只有两条:**能不能整块删除旧数据**,以及**分区数量会不会失控**。

按时间分区是最常见的做法,粒度通常是月或日:

```sql
PARTITION BY toYYYYMM(ts)   -- 一个月一个分区
PARTITION BY toDate(ts)     -- 一天一个分区
```

`PARTITION BY` 的表达式**可以是元组**,于是分区键也能是多列的组合:

```sql
PARTITION BY (toYYYYMM(ts), region)   -- 月和地区一起当分区键
```

这时每个 `(月, 地区)` 组合各自一个分区:`_partition_id` 是两段拼成的字符串,而 §05.12 讲的 `_partition_value` 能把那个元组原样取出来核对。加第二列**绝不是免费的**:分区数量按各列基数的**乘积**增长(保留两年 × 五个地区 = 120 个分区,再叠一个十值的 `status` 就是 1200 个),直接顶到上面那条"分区数量失控"的账上。所以它只在"你确实会**按这个组合整块淘汰或搬移**数据"时才划算;如果目的只是加速"按地区过滤",那是排序键和跳过索引的职责,不该往分区键上加(§06.3、§06.9)。

两者的差别在代价上,不在语法上。`toYYYYMM(ts)` 让分区数量随保留月数线性增长(保留两年 = 24 个分区),而 `toDate(ts)` 在同样两年里会产生 730 个分区。分区数量一多:每个分区至少有一个活跃 part,元数据变大;后台合并的任务数变多;`INSERT` 时"每个分区写一个新的 part",于是写入越分散、小 part 越多、合并压力越大。经验上的舒适区是**几十到几千个分区**,上万就该重新考虑粒度。

分区数量失控还有一种**更硬的报错**,不在元数据层面、而在写入那一刻:服务端设置 **`max_partitions_per_insert_block`(默认 100)**。它限制的不是表的**分区总数**,而是**一次 `INSERT` 的这个块触及了多少个分区**——超过 100 就直接报错,整批写入失败。分区键选得太细时这很容易触发:比如按秒分区、或拿高基数列当分区键,一批数据会同时铺进成百上千个分区,`INSERT` 当场被拒。应对有两条,视情况选:一是**分桶写入**,把一次大批量拆成多批,每批只覆盖少量分区,让每次插入横跨的分区数留在上限之内;二是**减少分区粒度**,把日分区换回月分区、或干脆别拿高基数列当分区键,从源头降低单批数据触及的分区数——两条经常一起用。

还有两个容易踩的坑。第一,**分区键用高基数列**(比如 `PARTITION BY user_id`)会让每个分区只有几行,系统会被元数据拖垮。第二,**分区表达式不要用会随时间变化的函数**——比如 `PARTITION BY toYYYYMM(now())`,历史数据的分区归属会在查询时和写入时对不上。

## 06.6 默认值的四种语义

这是 ClickHouse 特有的一组概念,四种写法看起来相近,可观察行为完全不同。

`DEFAULT expr` 是"**省略时才用**"。`INSERT` 里没给这一列,就求值填入;给了,就完全以你给的为准。它也可以写成 `DEFAULT` 不带表达式(那就是类型默认值),还可以配合 `EPHEMERAL` 列做中间计算。已有的 part 缺这一列时,读取时也用这个表达式补出来。

`MATERIALIZED expr` 是"**永远由表达式算,而且禁止显式写入**"——`INSERT` 语句里带上这一列会直接报错。它和 `DEFAULT` 最容易混,判断方法很简单:`MATERIALIZED` 列的取值不受输入影响,`DEFAULT` 列受输入影响。

`ALIAS expr` 是"**不落盘、查询时替换**"的列级宏。它不占存储,也不出现在 `SELECT *` 的结果里,但可以在表达式里直接引用。`MATERIALIZED` 列会出现在 `SELECT *` 里(它是一列真实数据),`ALIAS` 不会——这个差异经常让人以为"表里没这一列"。

`EPHEMERAL expr` 是"**参与计算但不落盘、也不可查询**"的列:它只用来给别的 `DEFAULT` / `MATERIALIZED` 表达式提供一个中间值,自身不会被存、也不能被 `SELECT`。它的价值是把复杂的默认值表达式拆开写。

最后一种"默认值"是**完全没有默认值**:插入时缺列就填类型默认值(`0`、空串、`NULL` 如果可空)。这也是为什么很多"表里突然出现一堆 0"的问题,根因是列名写错了一个字母、`INSERT` 静默按列名对齐。

## 06.7 列修饰符:COMMENT 与 NOT NULL

列定义后面除了 `DEFAULT`/`CODEC`/`TTL`,还有两个修饰符值得单说。

`COMMENT '文字'` 给列(或表)挂一段注释,随表持久化,在 `DESCRIBE TABLE` 的 `comment` 列与 `system.columns` 里能看到。它的价值不在"好看",而在**让元数据自解释**:字段含义、单位、枚举取值写在这里,后来人(包括未来的你)不必去翻上游系统。表级注释写在建表语句末尾:

```sql
CREATE TABLE hits
(
    id  UInt64 COMMENT '自增主键',
    url String COMMENT '原始请求路径,含 query string'
)
ENGINE = MergeTree ORDER BY id
COMMENT '网关访问日志';
```

`NULL` 与 `NOT NULL` 控制列的**可空性**,但要先看清它们和类型的关系:对一个**不可空**类型写 `NULL`,会把它变成 `Nullable(T)`(`Int32 NULL` 就等同于 `Nullable(Int32)`);写 `NOT NULL` 则维持不可空。而对一个**已经是 `Nullable(...)`** 的列再加 `NULL` 或 `NOT NULL`,ClickHouse 会**直接抛异常**。

那 `NOT NULL` 什么时候真有用?当服务端设置了 `data_type_default_nullable = 1` 时,没写修饰的类型会**默认变可空**,这时 `NOT NULL` 就是把某一列单独拉回不可空的手段。

有一个**与标准 SQL 不同**的关键点必须记住:`NOT NULL` 在 ClickHouse 里基本是**语法层面**的,它**不会**像 MySQL 那样在插入 `NULL` 时拒绝——写进不可空列的 `NULL` 会被替换成类型默认值(字符串变空串,`DateTime` 变 `1970-01-01` 这类,行为由 `input_format_null_as_default` 决定,第 08 章 §08.10)。想真正拒绝 `NULL`,要用 `input_format_null_as_default = 0`,或者 §06.10 的 `CONSTRAINT ... CHECK`。一句话:**`NOT NULL` 管的是 schema 里的可空性声明,拦不住值的 `CHECK` 才管值**。

## 06.8 CODEC:写在列上

`CODEC(...)` 写在列定义后面,决定这一列的数据块用什么压缩算法与编码:

```sql
payload  String CODEC(ZSTD(3)),
level    Int8   CODEC(T64, LZ4),
ts       DateTime CODEC(DoubleDelta, LZ4)
```

列级 codec 比"全局压缩设置"更精细:**该不该写、写哪一种,由这一列的数据分布形态决定**。单调递增的时间戳是增量编码(DoubleDelta/Delta)的主场;取值范围集中、重复度高的数值列适合 T64 这类"按位截断 + 分块"的编码;而真正随机、高基数的字符串(比如 UUID、URL)几乎没有可利用的结构,只能靠 ZSTD 这类通用压缩。写错 codec 通常**不报错**——建表照过、插入照收——代价却是隐形的:一种对当前分布无效的编码不但省不下空间,还要在每次读写时多做一次解码 / 编码,于是**压不动、还更慢**。所以 `CODEC` 值得在**明确的列**上写,而不是全表随手套一个;判断依据是这一列的真实分布,而"哪种 codec 在这列上到底省了多少"最可靠的办法是拿真实数据实测一遍。原理、可用编码器清单与实测差异见第 20 章。

## 06.9 数据跳过索引的 DDL

跳过索引在一组 granule 上记一段"摘要",读取时用摘要排除掉不可能命中的组。写法就是在列定义区加 `INDEX`:

```sql
INDEX idx_url   url   TYPE bloom_filter(0.01)      GRANULARITY 4,
INDEX idx_price price TYPE minmax                  GRANULARITY 1,
INDEX idx_tags  tags  TYPE tokenbf_v1(10240, 3, 0) GRANULARITY 4
```

`TYPE` 常见的有 `minmax`、`set`、`bloom_filter`、`ngrambf_v1` 与 `tokenbf_v1`;`GRANULARITY n` 表示"每 n 个 granule 合成一组"(n 越大索引越小,但一次排除的粒度越粗)。它是写在表定义里的一段声明,加完只会对**之后写入的 part** 生效——老 part 要跑 `MATERIALIZE INDEX` 补(§09.5)。索引类型清单、原理与实测见第 11 章,这里只保留 DDL 形状。

## 06.10 CONSTRAINT 与 ASSUME

`CONSTRAINT name CHECK expr` 是**插入时真的会检查**的行级校验:违反约束的 `INSERT` 会失败。这正是 ClickHouse 里少见的"写入前拦一道"的机制,适合做数据质量的兜底(比如"URL 不能为空""金额不能为负")。

`CONSTRAINT name ASSUME expr` 则**不检查**,它只是告诉优化器"我保证这个条件恒成立",让优化器据此做推断(比如借它裁掉整段数据)。**错误做法**:把该拦住脏数据的规则写成 `ASSUME`。**后果**:数据照样写进去,而优化器按"假设成立"去裁剪,于是查询返回**错误结果**。**正确做法**:要拦用 `CHECK`,只有当业务上真的能保证时才写 `ASSUME`。

## 06.11 PROJECTION 的 DDL

投影是"表内部的物化视图",写法就是在列定义区加一句 `PROJECTION`:

```sql
PROJECTION by_user (SELECT user_id, count() GROUP BY user_id)
```

它对 SQL 完全透明——不用改查询,优化器自己选;和跳数索引一样,`ADD PROJECTION` 只对**之后写入的 part** 生效,老 part 要 `MATERIALIZE PROJECTION` 补(§09.5)。

容易和它混为一谈的是**物化视图**和**跳数索引**,区分它们的关键在"**谁来维护、什么时候生成**"。物化视图是**用户自己建的一张独立表**(外加一条触发链路),数据落在它自己的存储里,你能在 `system.tables` 里看到它、也能直接查它;投影则**藏在原表内部**,由原表在写入与后台合并时顺带生成和维护,对使用者不可见。跳数索引根本**不存另一份排好序的数据**,它只在每组 granule 上记一小段摘要用来**排除**数据;投影却是真的把"按另一种键排好序的预聚合结果"**物化**下来,能被优化器当成一条更省事的读取路径直接选走。写投影不是免费的:每写一个 part、每合并一次,都要**额外**把投影那一份也建出来,这就是**写放大**——投影越多,写入与合并越慢、占盘越大。什么时候值得付这份代价、优化器究竟在什么条件下会选中它,见第 11 章 §11.12。

## 06.12 TTL 的完整 DDL 形态

第 18 章讲 TTL 的**行为**(什么时候真的删、什么时候搬盘、合并里怎么触发),这里只把**语法**摆全——TTL 远不止 `TTL ts + INTERVAL 90 DAY DELETE` 一种写法。

TTL 出现在两个层级。**列级 TTL** 写在列定义后:`col DateTime TTL d + INTERVAL 1 MONTH`——到期后这一列的值被替换成类型默认值(**不是**整行被删),而且**不能**用在主键列上。**表级 TTL** 写在引擎参数区,表达式必须返回 `Date`/`DateTime`,形状是:

```text
TTL expr
    [DELETE | TO DISK 'disk' | TO VOLUME 'volume'
     | RECOMPRESS CODEC(codec) | CLEAR INDEX index_name]
    [WHERE condition]
    [GROUP BY key_expr [SET v1 = aggr(v1) [, v2 = aggr(v2) ...]]]
    [, 下一条 TTL 规则 ...]
```

动作(action)省略时默认是 `DELETE`;`TO DISK 'x'` / `TO VOLUME 'y'` 把整个 part **搬到**指定磁盘或卷(冷热分层靠它,配合 `storage_policy`);`RECOMPRESS CODEC(...)` 用新 codec **重新压缩**这个 part(例如"热数据 LZ4、三个月后转 ZSTD")。其中 **`CLEAR INDEX index_name` 是要晚得多才出现的动作** —— 上游 PR #108069 引入(检索时仍在评审中,编号远晚于 25.3 所处的时期),它把指定二级索引的文件从"整段都已过 TTL"的 part 上清掉,但保留索引定义;本书对标的 **25.3 上并没有这个动作**,用之前请以你那一版的语法文档与实测为准。`DELETE` 在一条规则里只能出现一次;`WHERE` 给这条规则加条件(如 `TTL ts + INTERVAL 7 DAY DELETE WHERE status != 'error'`);`GROUP BY key_expr SET v = aggr(v)` 则在删除前先把行**聚合**卷起来(聚合前各列的合并方式由此指定,`GROUP BY` 表达式必须是主键前缀)。

**链式 TTL** 把多条规则用逗号连起来,让数据随年龄**逐级降级**:

```sql
TTL d + INTERVAL 1 DAY   TO VOLUME 'hot',
    d + INTERVAL 1 MONTH TO VOLUME 'cold',
    d + INTERVAL 1 YEAR  DELETE
```

意思是:1 天后搬到热卷,1 个月后搬到冷卷,1 年后删除。执行靠后台合并触发,`merge_with_ttl_timeout`(默认 4 小时)控制"多久尝试一次";想立刻看效果就 `OPTIMIZE TABLE ... FINAL`。触发时机、`TO DISK` 与 `storage_policy` 的配合、"删了但磁盘没立刻回收"这些坑,以及四种动作在合并里的具体过程,都在第 18 章。

## 06.13 表级 SETTINGS

`SETTINGS` 子句里写的是 MergeTree 的**建表参数**,随表持久化。最常用的是:

```sql
SETTINGS index_granularity = 8192,
         index_granularity_bytes = 10485760,
         storage_policy = 'hot_cold'
```

`index_granularity`(默认 8192 行)决定"粒度"有多大,从而决定稀疏索引的密度。调小 → 索引更大、裁剪更精确;调大 → 索引更小、一次多读几行。`index_granularity_bytes`(默认 10485760 字节)是"自适应粒度"的字节上限:行宽变化大时,按字节切粒度比按行数切更稳。`storage_policy` 指向服务端配置里的存储策略,用来做冷热分层。

## 06.14 集群与复制 DDL:`ON CLUSTER` 与副本参数

前面所有建表语句都隐含"只在一个节点上执行"。集群上要在多个节点一次执行同一条 DDL,靠的是 `ON CLUSTER`:

```sql
CREATE TABLE hits ON CLUSTER my_cluster (...) ENGINE = MergeTree ORDER BY id;
```

`ON CLUSTER cluster_name` 几乎可以挂在任何 DDL 语句后面(`CREATE`/`DROP`/`ALTER`/`RENAME`...),服务端按集群配置把这条语句**分发到每个节点**执行。它的价值是把"在十台机器上跑十次建表"变成"跑一次",并保证表结构完全一致。它的真正威力来自 **宏(macros)**:每个节点可在配置里定义一组宏(查看 `SELECT * FROM system.macros`),DDL 里写 `{shard}`(分片号)/`{replica}`(副本名),分发到各节点后会各自替换成自己那份值:

```sql
CREATE TABLE hits ON CLUSTER my_cluster
(...)
ENGINE = ReplicatedMergeTree('/clickhouse/tables/{uuid}/{shard}', '{replica}')
ORDER BY id;
```

这里就是复制表的两个参数:第一个是 **Keeper 里这张表的路径**,第二个是**本副本的名字**;都不写时采用默认值 `/clickhouse/tables/{uuid}/{shard}` 与 `{replica}`(可用服务端设置 `default_replica_path`、`default_replica_name` 覆盖)。路径里的 `{uuid}` 展开成**表的 UUID**——这也是推荐用 `Atomic` 库引擎(§06.1)的一个现实理由:有了库级 UUID,同一张表在所有副本上得到同一个路径,不必手工区分"库名/表名"。

`ON CLUSTER` 的分发机制、它与 `Replicated` 库引擎的关系、以及宏在多分片下的展开,在第 23 章 §23.4 展开;复制表的读写语义、Keeper 的路径组织、副本间怎么同步,都在第 22 章。这里只需记住"`ON CLUSTER` + 宏 = 一份 DDL 适配所有节点"。

## 06.15 建表的几种来源:AS t1 / AS SELECT / AS table_function

`CREATE TABLE` 的表结构来源有三种,只差几个字符,语义差很远:

```sql
CREATE TABLE t2 AS t1;                                                -- ① 照抄 t1 结构(含引擎定义),不灌数据
CREATE TABLE t2 ENGINE = MergeTree ORDER BY id AS SELECT * FROM t1;   -- ② 建表 + 把查询结果灌进去
CREATE TABLE t3 AS s3('https://bucket/x.parquet');                    -- ③ 结构取自表函数
```

**① `AS t1`** 是"照抄一份表定义":列、类型、引擎、`ORDER BY`、`PARTITION BY`、`PRIMARY KEY`、`SAMPLE BY`、`TTL`、`SETTINGS`、索引/约束/投影,全部从源表复制。它是这套语法里**唯一一个 `ENGINE` 可省**的形态——省略时就沿用源表的引擎。它**不灌数据**,新表是空的(也得到一个新的 UUID)。

**② `AS SELECT ...`** 是"按查询结果建表并灌数据"。有一条必须记住的规则:**实操上要把 `ENGINE` 连同 `ORDER BY` 一起写全**。ClickHouse 其实**有**默认表引擎——`default_table_engine` 默认就是 `MergeTree`(Cloud 上是 `SharedMergeTree`)——但在 25.3 上,省略 `ENGINE` / `ORDER BY` 的 `CREATE TABLE t2 AS SELECT * FROM t1;` 仍会以"必须给出 `ORDER BY` 或 `PRIMARY KEY`"为由报 `BAD_ARGUMENTS`(`You must provide an ORDER BY or PRIMARY KEY expression in the table definition`),而**不是**因为"没有默认引擎"。控制这件事的设置叫 `create_table_empty_primary_key_by_default`(布尔型,上游在 25.11 才把它的默认值改成 1),它在**本书对标的 25.3 上默认是 `0`**——这正是上面那条报错的来源:默认行为就是"必须显式给出 `ORDER BY` 或 `PRIMARY KEY`"。(顺带一提,上游到 2025 年底才修掉"没显式指定引擎时这条设置不生效"的问题,所以在 25.3 上更别指望它兜底。)要允许空主键,得显式把它设成 `1` 并把 `ORDER BY ()` 写出来。写全的形态是 `CREATE TABLE t2 ENGINE = MergeTree ORDER BY ... AS SELECT * FROM t1;`。还要清楚它**真的会执行那条查询**并把结果写进新表,大表上是一次完整的读写,不是元数据操作。

**③ `AS table_function()`** 从表函数(`s3`、`url`、`file`、`generateRandom`...)推断结构。但"表函数的 schema + 一个自定义引擎"这两个愿望**往往不能在同一条语句里同时满足**——把 `ENGINE = MergeTree` 追加在 `AS s3(...)` 后面会语法报错。要既拿到表函数的结构、又落成 MergeTree,标准做法是**反过来用 `EMPTY AS SELECT`**:

```sql
CREATE TABLE t3 ENGINE = MergeTree ORDER BY id
EMPTY AS SELECT * FROM s3('https://bucket/x.parquet');
```

`EMPTY` 的作用是"**只按 `SELECT` 推结构,不执行查询、不灌数据**"。于是上面这条既从 `s3()` 读到了列定义、又把引擎定成了 MergeTree,而一个字节的数据都没拉。它和 ② 的差别只有 `EMPTY` 一个词:去掉 `EMPTY`,就真的去把数据读回来写进去。

## 06.16 临时表与外部数据

**临时表**(`CREATE TEMPORARY TABLE`)的生命周期绑在**会话**上:会话结束就消失,别的会话看不到,也不写入磁盘。它是"会话内的中间结果"最合适的落点。它未指定引擎时默认用 `Memory` 引擎(由 `default_temporary_table_engine` 控制),把它当成持久表来设计是错的;但**"必须用 `Memory`"并不成立**——上游已放开这个限制,临时表现在可以用任意引擎,只把 `Replicated` 与 `KeeperMap` 排除在外。临时表的存在会**遮蔽同名持久表**,这一点在排错时经常骗人。

**外部数据**是另一种"一次性表":查询的发起方把数据一起送过来,服务端把它当成一张临时表参与本次查询——命令行客户端的 `--external` 参数、HTTP 接口随参数携带的数据都属于这一类。它与表函数 `input()` 是同一件事的两种入口,适合"我手上有一小段数据,想和库里的表 join 一下"。

## 06.17 SHOW CREATE TABLE 与 DESCRIBE TABLE

`SHOW CREATE TABLE db.t` 返回一列 `statement`,内容是这张表的建表语句——它用于**重建**表,所以是备份与迁移的关键产物。

`DESCRIBE TABLE db.t` 每列一行,常见字段含义:

- `name`、`type`:列名与类型名;
- `default_type`:这一列的默认值种类,取值是 `DEFAULT` / `MATERIALIZED` / `ALIAS` / `EPHEMERAL`,普通列是空串;
- `default_expression`:`default_type` 对应的表达式文本;
- `comment`:列注释;
- `codec_expression`:列上写的 `CODEC(...)`,没写就是空串;
- `ttl_expression`:列级 `TTL`,没写就是空串。

看 `default_type` 是排错的第一招:如果它显示 `MATERIALIZED` 而你以为只是 `DEFAULT`,那"为什么我插入的值被忽略"就有了答案。需要注意,`DESCRIBE` 的结果里还可能多出一列 `is_subcolumn` —— **它只在开了 `describe_include_subcolumns` 设置时才出现**(值为 1 表示这是一个内部子列),默认不显示;本文以 25.3 上默认的这一组为准。

## 06.18 其他 DDL 对象一览

一个库里的 DDL 对象远不止表。这里给一份**清单式的概览**——每类只点到"是什么、容易踩在哪",展开留给对应章节。

- **视图:** `CREATE VIEW v AS SELECT ...`(普通视图,查询时展开);`CREATE MATERIALIZED VIEW mv [TO target] [ENGINE = ...] AS SELECT ...`(写入时触发,把结果落到 `TO` 的表或自带存储)。物化视图的触发与去重在第 08 章 §08.6 与第 18 章有交叉。
- **字典:** `CREATE DICTIONARY d (...) PRIMARY KEY k SOURCE(...) LAYOUT(...) LIFETIME(...)`,把外部数据源缓存成一张可 `dictGet` 的查找表(第 21 章)。
- **函数:** `CREATE FUNCTION f AS (x) -> x + 1` 是 **SQL UDF**,用表达式定义一个标量函数;另一种**可执行 UDF(executable UDF)** 不通过 SQL 定义,而是在服务端配置里登记一个外部脚本,再在 SQL 里调用(第 17 章)。
- **权限对象:** `CREATE USER`、`CREATE ROLE`、`CREATE ROW POLICY`(行级策略)、`CREATE QUOTA`、`CREATE SETTINGS PROFILE`(第 26 章)。
- **表管理:** `ATTACH TABLE`(把已在磁盘上的表重新挂上)与 `DETACH TABLE`(摘下但不删数据);`RENAME TABLE a TO b [, c TO d]`;`EXCHANGE TABLES a AND b`(原子交换两张表,依赖 `Atomic` 库);`DROP TABLE` 与 `UNDROP TABLE`(后者依赖 `Atomic` 库保留的删除元数据);`CREATE OR REPLACE TABLE / VIEW / ...`(存在则替换)。

这份清单的目的是让你**知道有这些东西**、知道遇到时该去第几章找;每条语句的完整语法与运维细节集中在第 09 章。一个通用规律值得提前记住:**凡是"重命名、交换、恢复"这类要求原子性的操作,基本都依赖 `Atomic` 库引擎(§06.1)**——这也是新库一律选 `Atomic` 的一个现实理由。

## 06.19 与 mini-clickhouse 的对照

AST 层的 `CreateTableQuery`(`mini-clickhouse/minich/sql/ast_.py`)是本章的直接落地,分"对齐点"与"简化点"两部分看。

**对齐点。** `CreateTableQuery` 的字段与真实 ClickHouse 的 `ASTCreateQuery` 几乎一一对应:`columns`、`indexes`、`constraints`、`projections`、`engine` + `engine_params`、`order_by`、`partition_by`、`primary_key`、`sample_by`、`ttl`、`settings`、`if_not_exists`、`temporary`、`as_table` / `as_select`、`cluster`、`comment`、`uuid`,并且能通过 `to_sql()` 回打成原文。列定义 `ColumnDefinition` 的 `default_kind` 取 `DEFAULT` / `MATERIALIZED` / `ALIAS` / `EPHEMERAL` 四值之一,并带 `codec`、`ttl`、`comment`、`not_null`、`primary_key`;跳过索引、约束、投影分别是 `IndexDefinition`(`type` / `type_params` / `granularity`)、`ConstraintDefinition`(`CHECK` / `ASSUME`)、`ProjectionDefinition`。解释器把列定义解析成存储层的 `ColumnDefinition` 后,连同 `params` 字典(`order_by`、`partition_by`、`primary_key`、`sample_by`、`ttl`、`settings`、`indexes`、`constraints`、`projections`、`columns_ast`)一起交给存储工厂——这条路径与真实 ClickHouse 的 `EngineFactory` + `Arguments` 是同一个形状。`is_stored()` 的判据是 `default_kind not in ("ALIAS", "EPHEMERAL")`,即只有 `ALIAS` 与 `EPHEMERAL` 不落盘、`MATERIALIZED` 会落盘,与真实 ClickHouse 一致。`CREATE TABLE t2 AS t1` 会连引擎参数一起继承:`_resolve_definition` 的 `as_table` 分支先整份复制源表的列定义,再把源表的 `engine_params`(`ORDER BY`、`PARTITION BY`、`PRIMARY KEY`、`SAMPLE BY`、`TTL`、`SETTINGS`,以及索引/约束/投影定义)整体带过来,最后用 `AS` 语句里**显式写出**的子句覆盖同名项(引擎名取 `node.engine or src.engine.engine_name`),与真实 ClickHouse"连排序键一起复制"的语义相符。

**简化点。** 几处刻意的取舍:

- **`DESCRIBE TABLE` 只输出 `name` 与 `type` 两列**,而 §06.17 列出的 `default_type` / `default_expression` / `comment` / `codec_expression` / `ttl_expression` 在 `catalog.py` 的 `describe_row()` 里其实都算好了,只是没接到 SQL 输出上;要看更完整的列元数据可以查 `system.columns`。
- **`SHOW CREATE TABLE` 是近似实现**:优先返回建表时记下的原始 SQL(`table.metadata_sql`),没有时才退回一个占位渲染(`_render_create()` 生成的是 `ENGINE = ...` 这样的简写),不是规范化的完整建表语句。
- **`CREATE TABLE` 没有 `ENGINE` 时会直接抛 `SYNTAX_ERROR`**;`Context.create_table(name, columns_sql, engine_sql)` 这个便捷函数在引擎串以 `mergetree` 开头且没写 `ORDER BY` 时,会自动补上 `ORDER BY tuple()`。
- **`PRIMARY KEY` 必须是 `ORDER BY` 前缀这条规则没有在解析或建表时校验**;`SAMPLE BY`、表级 `TTL`、`PROJECTION`、跳过索引都只被解析并原样传给引擎,是否真的生效取决于引擎实现:跳过索引由 `minich/storages/merge_tree/skip_index.py` 实现,注册了 `minmax`、`set`、`bloom_filter`、`ngrambf_v1`、`tokenbf_v1` 五种;列上的 `ttl` 只被记进元数据(供 `system.columns` 的 `ttl_expression` 显示),没有后台行为去执行它;`projections` 只被传进 params,执行层没有使用。
- **表级 `COMMENT` 不随 `AS t1` 继承**(`Table.comment` 不在克隆之列;列注释会跟着走,因为列定义是整份复制的),数据本身也不继承(UUID 与内容都是新的空表)。

## 06.20 小结

一切从库开始。`CREATE DATABASE` 的 `ENGINE` 决定元数据怎么管、DDL 能不能原子地改——新库一律选 `Atomic`(默认),它给表 UUID、让 `RENAME`/`EXCHANGE`/`UNDROP` 成为可能,也是复制表 `{uuid}` 路径的前提;`Replicated` 库让 DDL 自动跨副本生效。然后才是表。

`CREATE TABLE` 的括号里描述"数据长什么样",括号外描述"数据怎么存、怎么被读"。其中 `ORDER BY` 是最重要的一个决定:它同时是物理顺序、稀疏索引和裁剪能力的来源;`PRIMARY KEY` 只是它的前缀,默认等于它,并且它**不保证唯一**。分区键的选择标准是"淘汰单位"与"分区数量",不是查询加速。四种默认值里,`MATERIALIZED` 禁止显式写入、`ALIAS` 不落盘也不出现在 `SELECT *` 里、`EPHEMERAL` 只服务于别的默认值;`COMMENT` 给元数据加注释,`NOT NULL` 声明不可空(注意它**不拦插入**,拦插入要靠 `CHECK`)。跳过索引、约束、投影都是写进表定义的结构,而 TTL 的语法远不止 `DELETE`——还有 `TO DISK`/`TO VOLUME`/`RECOMPRESS`/`CLEAR INDEX` 与 `GROUP BY ... SET`,并能链式逐级降级。

建表来源有讲究:`AS t1` 照抄结构(引擎可省)、`AS SELECT ...` 会真的灌数据(**引擎不能省**)、`AS table_function()` 借表函数的结构,而"表函数结构 + 自定义引擎"要靠 `EMPTY AS SELECT` 才能两全。集群上用 `ON CLUSTER` + `{shard}`/`{replica}` 宏让一份 DDL 适配所有节点,复制表的两个参数就是 Keeper 路径与副本名。最后,库里远不止表这一种对象:视图、字典、UDF、权限对象、`ATTACH`/`DETACH`/`RENAME`/`EXCHANGE`/`DROP`/`UNDROP` 各成一类,凡是要求原子性的都依赖 `Atomic` 库。
