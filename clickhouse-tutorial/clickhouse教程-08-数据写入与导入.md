# 第 08 章 数据写入与导入

一个 ClickHouse 集群跑得快不快,九成取决于两件事:数据是怎么写进来的、写进来以后变成了多少个 part。查询写得再漂亮,只要表底下躺着十万个小 part,每次 SELECT 都要打开几十万个文件句柄,列存和稀疏索引带来的优势会被 IO 开销吃得一干二净。

这一章要回答的是:一条 `INSERT` 从客户端出发到落盘,中间经过了哪些步骤,每一步为什么必须存在;同一个"导入一亿行"的任务,为什么写成一万条 `INSERT` 会让集群直接报错,而写成一个文件流进去只要几分钟;以及当数据写坏了、写重了、写到一半崩了,ClickHouse 会怎么反应。

## 08.1 一次 INSERT 的真实顺序

先看一条最简单的写入:

```sql
INSERT INTO hits (id, url, ts) VALUES (1, '/a', '2024-01-01 00:00:00'), (2, '/b', '2024-01-01 00:00:01');
```

客户端把这段 SQL 发出去之后,服务端做的事情按顺序是:

1. 解析 SQL,得到 AST,`InterpreterInsertQuery` 接管。
2. 从输入流里按 `max_insert_block_size` 行切块(`INSERT ... SELECT` 和带格式的插入都是流式的,不是先把全部数据读进内存)。
3. 对**每一个块**执行下面的流水线:

   1. 按列的定义补 `DEFAULT` / `MATERIALIZED` 表达式的结果;显式写入 `MATERIALIZED` 列会直接报错;
   2. 按排序键(`ORDER BY`)在内存里排序 —— 这一步是后面一切的前提;
   3. 按 `index_granularity`(默认 8192 行)切成 granule;
   4. 逐列序列化、压缩,写成 `<partition_id>_<min>_<max>_<level>` 这个 part 的全部文件,但**先写在 `tmp_insert_<part_name>/` 这个临时目录里**;
   5. 一个 part 的所有文件写完并通过校验后,**原子改名**成正式目录名。

把第 4、5 步分开,是这条路径上唯一一个"看起来多余、实际上不能省"的设计。

## 08.2 为什么必须先写临时目录、再原子改名

一个 part 不是一个文件,而是几十个文件组成的一个目录。如果没有原子提交这一步,读路径要面对的局面是这样的:查询和写入并发时,查询可能正好读到一个只写了一半的 part —— 列 A 的 `.bin` 有了,列 B 的还没写;或者 `count.txt` 说是 100 万行,而 `primary.idx` 只覆盖了前 30 万个 granule。这时候无论读路径写多少防御代码,都无法区分"这个 part 是坏的"和"这个 part 还没写完"。

正确做法是把"可见性"压缩成一个原子操作:

```text
写 tmp_insert_<part_name>/   ← 此刻正式目录还不存在,任何查询都看不到它
     ...
rename(tmp_insert_<part_name>, <part_name>)   ← 同文件系统内的 rename 是原子的
```

从这一刻起,这个 part 才进入这张表的"活跃 part 集合"。**读的人永远看不到半个 part** —— 这条保证不是靠加锁实现的(锁保护不了进程崩溃),而是靠"改名是原子的"。

由此还得到两个附带好处。其一,写入失败或进程崩溃时,磁盘上只会留下一个 `tmp_insert_*` 目录,启动时扫到删掉即可,不会污染数据。其二,写入路径上不需要任何"写锁"来阻挡读 —— 读路径可以完全不管写入,这正是 ClickHouse 敢把读做得那么激进的前提(第 05 章、第 18 章)。

有一条容易忽略的限制:跨文件系统的 `rename` 会退化成"拷贝 + 删除",原子性就没了。所以临时目录必须与目标 part 落在**同一个文件系统**上。在真实 ClickHouse 里,对应的约束是"临时 part 与最终 part 必须在同一**存储策略/卷组**下才能原子改名"——多卷存储策略(`storage_policy` 里配多块盘、做冷热分层)本就是为跨不同磁盘/挂载点设计的,所以要求不是"所有卷都在同一挂载点",而是写临时 part 的那块盘与最终的落脚卷处在同一个可原子改名的范围里。`tmp_policy` 的作用正是在这条约束下,指定"临时 part 用哪个卷放",它要求配置里**恰好一个卷**(而一个卷本身可以包含多块盘)。

## 08.3 三种 INSERT 写法的差异

ClickHouse 支持三类数据来源,它们的代价差别很大:

```sql
-- ① 字面量:解析开销最大
INSERT INTO t VALUES (1, 'a'), (2, 'b');

-- ② 查询结果:最快,推荐用于批量导入
INSERT INTO t SELECT * FROM source_table;
INSERT INTO t SELECT * FROM file('data.csv', 'CSV', 'id UInt64, name String');

-- ③ 指定格式:客户端把一段原始文本送进来
INSERT INTO t FORMAT CSV
INSERT INTO t FORMAT JSONEachRow
```

三者的本质区别在**数据是怎么变成列的**。

`VALUES` 走的是 SQL 解析器:每一行的每一个字面量都要经过词法分析、语法分析、表达式求值、类型转换。它就是一条 SQL 语句,只不过恰好很长。几万行还可以忍,几十万行的 `VALUES` 里,解析耗时会远远超过真正的写入耗时 —— 你会看到 CPU 全花在 `Parser` 上,而磁盘几乎闲着。

`SELECT` 走的是查询执行引擎,产出的是已经类型正确的列块,直接灌进写入路径,中间没有"文本 → 值"的转换。这是导入数据的**首选**写法。

`FORMAT` 走的是格式解析器(第 19 章)。它比 SQL 解析便宜得多,因为 `CSV`、`TSV` 这类按分隔符切分的格式只需要按字段分隔符切开,不需要理解 SQL 语法(`RawBLOB`、`LineAsString` 这类"整段原样当值"的格式更便宜)。`clickhouse-client` 的 `--query "INSERT INTO t FORMAT CSV" < file.csv` 就是这条路。

无论走哪条路,最终都会落到同一件事上:**每产生一个块,就写一个 part**。这是理解后面所有坑的关键 —— 三个写法的差异是"多快",而 part 的数量只取决于"你能塞进多少个块"。

## 08.4 part 太多会怎样

一个 part 的查询成本,主要不是它的大小,而是"打开它"这件事本身:每个 part 至少有一份 `columns.txt`、一份 `primary.idx`、每列一个 `.bin` 和 `.mrk2`。一个 100 行的 part 和一个 100 万行的 part,文件数量是一样的。所以 part 数量一上去,读一个分区的固定开销就线性增长。

ClickHouse 对此有两道闸,它们都作用在**单个分区**的活跃 part 数量上:

`parts_to_delay_insert`(默认 1000)—— 超过这个数,后续 `INSERT` 会被**指数退避式地限速**。它不报错,只是让你变慢,给后台合并争取时间。这是"软闸"。

`parts_to_throw_insert`(默认 3000)—— 超过这个数,`INSERT` 直接失败:

```text
Code: 252. DB::Exception: Too many parts (3001). Merges are processing
significantly slower than inserts.
```

这是"硬闸"。这两道闸的默认值都在版本间上调过,而且**分属两个版本,别混成一句**:`parts_to_throw_insert` 的 300 → 3000 发生在 **23.6**——官方 `parts_to_*` 设置文档在 `parts_to_throw_insert` 条目下明确写着"Before version 23.6 this setting was 300",这条确定。`parts_to_delay_insert` 的 150 → 1000 则**更晚**:上游源码里它如今的默认值确实是 `DECLARE(UInt64, parts_to_delay_insert, 1000, ...)`,但官方 OSS 文档没有为它标注版本;能查到的、带明确版本边界的来源(厂商托管 ClickHouse 的文档,写着"1000 for versions 25.1 and higher, 150 for versions 24.12 and lower")把它系于 **25.1**。上调的原因是现代硬件上单分区能承受的 part 数比过去多得多。生产环境里这两个数经常需要按表的写入模式调整。

**还有一道管着另一个轴的闸门,很多人是撞上它才知道它存在:一次 `INSERT` 触及多少个分区。** 服务端设置 `max_partitions_per_insert_block`(默认 100)限制的是**单个块**里的数据被分派到了多少个分区:超过 100 个,这条 `INSERT` 直接失败、整批不落盘。它和上面两道闸不是一回事——那两个数的是**单个分区里堆积的 part 数**(写得太碎、合并追不上),这个数的是**单批写入的分区跨度**(分区粒度太细)。分区键选得越细,一批数据越容易同时铺进成百上千个分区:按秒分区、或拿高基数列当分区键,几乎必然触发。治它的药也不同:"part 太多"靠客户端攒批或 `async_insert`,而这一条要么**分桶写入**(把一次大批量拆成多批,每批只覆盖少量分区),要么**换粗的分区粒度**(日分区换月分区、别拿高基数列当分区键)。分区键怎么选、这条上限的完整语境在第 06 章 §06.5。

观察 part 数量的标准工具是 `system.parts`:

```sql
SELECT database, table, partition, count() AS part_count,
       sum(rows) AS rows, formatReadableSize(sum(bytes_on_disk)) AS bytes
FROM system.parts
WHERE active AND database = 'default' AND table = 'hits'
GROUP BY database, table, partition
ORDER BY part_count DESC;
```

`active = 1` 这一条不能漏:被合并掉但还没删除的旧 part 仍然在系统表里,不过滤会让你以为 part 多了一倍。

现在看开头的那个数量级问题。"一次插 100 万行"和"一万次插 100 行",写进去的行数完全一样,但:

```text
一次 INSERT 100 万行
   → 按 max_insert_block_size 切成若干块,每块一个 part
   → 假设单分区,通常是 1~几个 part

一万次 INSERT × 100 行
   → 一万个块,一万个 part(只要都落在同一个分区)
   → 每个 part 都有完整的一套元文件
   → 触发 parts_to_throw_insert:直接报 Too many parts
   → 即使没触发,查询也要打开一万倍的文件,后台合并线程永远追不上
```

差距不在写入速度上,而在"写完之后这张表还能不能查"。这也是几乎每一篇 ClickHouse 调优文档都会把"批量化写入"排在第一位的原因。

## 08.5 批量导入的实践

最省事的办法是把整个文件交给客户端,用 `FORMAT` 一次性送进去:

```bash
clickhouse-client \
  --query "INSERT INTO hits FORMAT CSVWithNames" \
  < hits.csv
```

`clickhouse-client`(以及 `clickhouse-local`)还支持把文件名直接写进语句,让客户端去读**本机**文件:

```sql
INSERT INTO hits FROM INFILE 'hits.csv' FORMAT CSVWithNames;
```

它与上面用 shell 重定向 `<` 是同一件事的两种写法,区别只在于文件名出现在 SQL 里、更适合放进脚本;`FORMAT` 可以省略,此时按文件扩展名推断(如 `.csv` → `CSV`,`.tsv` → `TSV`),也支持 `COMPRESSION` 指定压缩。和 `INTO OUTFILE` 一样,`FROM INFILE` 读的是**客户端所在机器**上的文件、靠客户端实现,走 HTTP 接口用不了。

数据量更大、需要预处理时,用 `clickhouse-local` 先洗一遍数据,再灌进服务端 —— 它不需要服务端进程,能直接对本地文件跑 SQL:

```bash
clickhouse-local \
  --query "SELECT toDate(ts) AS d, url, count() FROM file('raw.csv', 'CSVWithNames') GROUP BY d, url ORDER BY d FORMAT Native" \
  > agg.native

clickhouse-client --query "INSERT INTO hits_daily FORMAT Native" < agg.native
```

如果数据已经在对象存储或者别的表里,用表函数把它当成一张表来读,让服务端自己去取:

```sql
INSERT INTO hits SELECT * FROM s3('https://bucket/hits/*.parquet', 'Parquet');
INSERT INTO hits SELECT * FROM file('hits.csv', 'CSV', 'id UInt64, url String, ts DateTime');
INSERT INTO hits SELECT * FROM input('id UInt64, url String, ts DateTime') FORMAT CSV;
```

`input()` 比较特别:它不读文件也不读网络,而是把"客户端接下来发过来的字节流"当表读。它的用途是给 `INSERT ... SELECT` 提供一个带 schema 的入口,方便在客户端侧做转换。

从远端拉数据时还有 `url()` 和 `remote()`(指向另一个 ClickHouse 实例),它们的语义见第 30 章;`s3()` 这类对象存储表的细节在第 30 章也会展开。

## 08.6 幂等与去重

重试是分布式系统里躲不掉的事 —— 网络超时之后你不知道服务端到底写进去没有,只能再发一次。这时问题变成:重复写入会不会产生重复数据?

**先说结论:ClickHouse 默认不是"按主键去重"的数据库。** 往一个 `MergeTree` 表里插两次同样的行,你就得到两份数据。幂等性要靠两个不同的机制:

第一个机制是**块级去重**。对 `Replicated*MergeTree`,服务端会为每个插入的块算一个指纹并记录在 Keeper 里,默认 `insert_deduplicate = 1`。同一个块重发时,指纹命中,第二次写入被直接跳过 —— 这对"客户端重试"这个场景是正确的语义,但只在数据量、内容、顺序完全相同(即真的同一个块)时才生效。普通 `MergeTree` 也有一套非复制的去重窗口(`non_replicated_deduplication_window`),不过它 25.3 的默认值是 `0`,也就是**默认关闭**、得自己打开才生效,可以用 `SELECT value FROM system.merge_tree_settings WHERE name = 'non_replicated_deduplication_window'` 现场确认。

去重的**粒度与窗口**都是可以调的,三个设置值得认识:

- **`insert_deduplication_token`** 是**自定义的去重键**。不设它时,服务端拿"整块数据的校验和"当指纹,于是只有当重发的块**逐字节相同**(真的是一次重试)才判定为重复。设了 `insert_deduplication_token = 'batch-2025-03-01-001'`,就用**你自己的键**表达"这是一个逻辑批次":只要每次重试都带同一个 token,即使块内容因时间戳之类的差异而并不完全一致,也能去重。它**按分区生效**——一次插入涉及多个分区时,各分区分别记这个 token。
- **`replicated_deduplication_window`**(复制表)控制**保留多少个近期块指纹**参与比对,25.3 上默认 `1000`(调大能覆盖更长的重试窗口,代价是 Keeper 里留的记录更多;这个默认值随版本变过——**25.9 起被提到 `10000`**,所以别照抄,以本机 `system.merge_tree_settings` 为准);`replicated_deduplication_window_seconds` 是它的时间窗版本,25.3 上默认 `604800`(7 天)——**这个值在更新的版本里被下调过**,所以同样别照搬。窗口之外的老块不再比对,于是"隔太久再重发"可能不再被去重。两个窗口怎么配合,`第 22 章 §22.5` 讲得更完整。
- **`deduplicate_blocks_in_dependent_materialized_views`** 决定 token 是否**传播到下游物化视图的目标表**。默认关;打开后,源表去重时物化视图目标表也按同一 token 去重,这才谈得上"源表 + MV 一起 exactly-once"。它依赖 `insert_deduplicate = 1`(默认在效)。要留意它的一个已知副作用:聚合型 MV 只要两次插入产出**逐字节相同**的输出(比如两次不同的源数据都聚合出 `count() = 100`),第二份就会被**误判重复而丢掉**——这是很多人踩过的坑。

第二个机制是**表引擎语义**。`ReplacingMergeTree` 在后台合并时,对排序键相同的行只保留版本最大(或最后写入)的那一条:

```sql
CREATE TABLE users_replacing
(
    id UInt64,
    name String,
    ver UInt64
)
ENGINE = ReplacingMergeTree(ver)
ORDER BY id;

INSERT INTO users_replacing VALUES (1, 'alice', 100);
INSERT INTO users_replacing VALUES (1, 'alice-v2', 200);

SELECT * FROM users_replacing;          -- 可能看到 2 行
SELECT * FROM users_replacing FINAL;    -- 一定看到 1 行(name = 'alice-v2')
```

注意第一句 `SELECT` 的结果:**不保证**。合并是异步的,`INSERT` 刚返回时两个 part 都还在,查询会看到两行。这就是"最终一致"的字面含义 —— 不是"错了",是设计如此。想让结果立刻收敛,可以在查询里加 `FINAL`(代价见第 18 章),或者在写入后用 `OPTIMIZE TABLE ... FINAL` 催一下。

`insert_quorum` 是另一个容易被误解的设置:它管的是"写入返回成功之前,要得到多少个副本的确认",不是去重。`insert_quorum = 0`(默认)表示不等待;设成 ≥2 就启用法定写入,要求**这么多个**副本写成功才算成功(想要"多数副本"可以设 `insert_quorum = 'auto'`,它会用 `副本数 / 2 + 1`)。它只在复制表上有意义,而且会显著拖慢写入。这三个机制(块级去重、`ReplacingMergeTree`、`insert_quorum`)的准确边界都指向第 22 章。

## 08.7 写入时的资源与并发

写入路径上有几个旋钮,它们各自管一件事:

`max_insert_block_size`(默认 1048449 行)决定"多大的数据会被攒成一个块"。块越大,part 越大、part 数越少、压缩率通常越好,但单块的内存占用也越高。这里有一条**最容易被记错**的语义:一个块超过了这个阈值时,ClickHouse **不报错**,而是把它切开继续写,只在日志里留一条"把一个块拆成多个 part"之类的警告。指望用它来"防止写出太多 part"是无效的 —— 防线的真正位置是下面要讲的 `parts_to_delay_insert`。

`min_insert_block_size_rows`(默认 1048449 行)与 `min_insert_block_size_bytes`(默认 268402944 字节,约 256 MiB)是**反向**的:当 `INSERT ... SELECT` 从多个线程并行读取、产出很多小结果块时,写入侧会把这些小块**攒(squash)**到至少这个规模再落盘,避免"查询很快但写出几百个 part"。

`max_insert_threads` 控制 `INSERT ... SELECT` 里并行执行 SELECT 的线程数。25.3 上默认不并行(`0` 与 `1` 同义,都表示 `INSERT ... SELECT` 不开并行)。它**确实**后来被改成了"随 CPU 核数自动取值"——上游 PR #109006 把默认值改成 `auto`(即 CPU 核数),并配了一条 `max_insert_threads_min_free_memory_per_thread`(默认 4 GiB)在空闲内存不够时把它往下压;但这次改动**落在 25.3 之后**(26.x 才生效),别据此给 25.3 调参。设大于 1 能显著加速"从别的表搬数据",代价是同一批数据会被切成多个 part。

`async_insert` 解决的是一个很具体的问题:**大量的小 `INSERT`**。开了它之后,服务端不会为每个连接上来的小插入立刻生成 part,而是先把数据攒在服务端的内存缓冲里,攒够一定大小或过了一小段时间再合并成一个大 part 落盘。它能让"每秒钟几千条单行插入"这种场景从"必然报 Too many parts"变成"平稳运行"。典型用法是 `SET async_insert = 1, wait_for_async_insert = 1;` —— 后者让客户端仍然等到数据真正落盘才返回,代价是失去一部分异步带来的吞吐。它的缓冲上限由 `async_insert_max_data_size` 控制(开源版默认 10 MiB),超时由 `async_insert_busy_timeout_ms`(默认 200 ms;它是 `async_insert_busy_timeout_max_ms` 的别名)控制;较新版本还用 `async_insert_use_adaptive_busy_timeout` 在这上下限之间自适应调整。它的完整语义、与 `wait_for_async_insert` 的组合、以及它在复制表上的行为都在第 18 章。

攒在服务端缓冲里的数据什么时候落盘,除了靠上面那些阈值与超时,还能**手动催**:`SYSTEM FLUSH ASYNC INSERT QUEUE` 会把当前所有待刷的异步插入缓冲立刻刷到目标表。停机维护前、或者"刚用 `async_insert` 写完就想立刻在表里查到"时就是用它;它和 `wait_for_async_insert = 1` 的差别在于——后者让**每次写入**都等到落盘,前者是事后按需强制刷一次,平时不必为此牺牲吞吐。(单表作用的变体是更晚才加进 `SYSTEM` 的,25.3 上是否可用以你那一版为准。)

这三个默认值都是可以直接查的,建议在真机上跑一遍再记住它们:

```sql
SELECT name, value, description FROM system.settings
WHERE name IN ('max_block_size', 'max_insert_block_size',
               'min_insert_block_size_rows', 'min_insert_block_size_bytes');
```

顺带说清一个容易混淆的地方:`max_block_size`(默认 65409)管的是**查询时**内存里一次处理多少行,`max_insert_block_size`(默认 1048449)管的是**写入时**一个块多大。两个数其实都取"2 的幂减 127"(源码里的 `DEFAULT_BLOCK_SIZE = 65536 - 127 = 65409`、`DEFAULT_INSERT_BLOCK_SIZE = 1048576 - 127 = 1048449`)。为什么偏偏减 127 而不是用整整齐齐的 65536?**官方文档没有给出解释**;流传最广的社区解释是"2 的幂的行数会让跨行步长也接近 2 的幂、访问同列相邻行时容易映射到同一组 cache set 造成冲突缺失,而 65536 又是常见 SIMD 宽度的整数倍、尾循环边界处理不理想"(详细口径见第 10 章 §10.4)。这只是一个**未经官方确认的合理工程解释**,不要当成定论。可以确定的是:少 127 行(约 0.2%)对吞吐几乎没有影响,而"前者是个别扭的非 2 的幂、后者是干净的 2 的幂"这种印象并不成立——两个都不是 2 的幂。

## 08.8 Buffer 引擎:客户端侧的写缓冲

`async_insert` 是在**服务端**替你攒批。更早、更"客户端侧"的思路是 **`Buffer` 引擎**:建一张 `Buffer` 表,让它把写入先收在**内存**里,攒够一定行数/字节/时间再整批写进目标表(目标表必须和它同在一个服务端):

```sql
CREATE TABLE hits_buffer AS hits
ENGINE = Buffer(default, hits, 16, 10, 100, 10000, 1000000, 1000000, 10000000);
```

`Buffer` 的引擎参数依次是:目标库、目标表、层数(`num_layers`),再是三组**下限 / 上限**:时间(`min_time` / `max_time`,单位秒)、行数(`min_rows` / `max_rows`)、字节数(`min_bytes` / `max_bytes`)。攒批逻辑由这些阈值触发:三组下限**同时**满足、或任意一个上限满足时,这一批就被写下去(所以哪怕只写进一行,过了 `max_time` 也一定会被刷出)。

它最要命的 **caveat** 是持久性:

- **异常重启会丢数据。** 官方文档写得很直白:服务器**异常**重启时,缓冲区里的数据丢失。它住在内存里,没有预写日志。(正常 `STOP`/优雅关闭时会先 flush 再退出,但那不是"崩溃也不丢"的保证。)
- **读到的只是一半。** 对 `Buffer` 表做 `SELECT` 返回的是"缓冲区里的行 + 目标表里已 flush 的行"的并集,而且缓冲部分在不同查询之间**不一致**;`FINAL`、`SAMPLE`、跳过索引这些语义**不会正确作用到缓冲数据**上。实践准则是:**只把它当写入入口,读永远去读目标表**。
- **schema 漂移会清空缓冲(或静默丢列)。** 如果缓冲区和目标表的列**类型**对不上、或刷出时目标表根本不存在,服务端只在日志里**记一条错误并把该缓冲清空**——数据直接没了,不会以更强的错误回传;而如果只是列**集合**不同,则只把两边都有的那部分列插进去。
- **内存占用**的上界约为"层数 × 每层字节上限",配置时要按这个估 RAM。

正因为 Buffer 把"不丢数据"的责任推回给了上游,它在较新版本里**基本被 `async_insert` 取代**:后者先把数据写进服务端的预写日志(WAL)再回执,同样能攒批,却不以丢数据为代价。Buffer 还剩的场景很窄——大致只有"很多台服务器各自发来大量小 `INSERT`、你无法在客户端侧攒批"这类边缘情况。**新项目优先用 `async_insert`。**

## 08.9 流式摄入:Kafka 与队列引擎

前面讲的是"数据已在手边、想办法高效灌进去"。还有一类场景是**数据源本身是一条持续不断的事件流**:Kafka、RabbitMQ、NATS 队列,或者一个不断有新文件落地的 S3 桶。对这种源不该轮询 `INSERT`,而应用**流式表引擎**把它当成一张表来读。ClickHouse 提供一族这样的引擎:

- **Kafka** —— 消费 Kafka topic;
- **RabbitMQ**、**NATS** —— 消费对应消息队列(NATS 还支持多队列);
- **S3Queue** / **AzureQueue** —— 盯住对象存储桶,把"新出现的文件"当成一条条消息来处理(有有序、无序两种模式)。

它们的共同使用模式是**两步**:先建一张"缓冲表",引擎就是上面这些(**它不是 MergeTree,不落盘、通常不能直接查**);再给它挂一个**物化视图**,把消费到的数据写进真正的目标 MergeTree 表。

```sql
CREATE TABLE kafka_hits (id UInt64, url String, ts DateTime)
ENGINE = Kafka
SETTINGS kafka_broker_list = 'kafka:9092', kafka_topic_list = 'hits',
         kafka_group_name = 'ch_consumer', kafka_format = 'JSONEachRow';

CREATE MATERIALIZED VIEW hits_mv TO hits AS
SELECT * FROM kafka_hits;
```

三个要点:其一,**去重**在流式场景里尤其重要——重试、rebalance 都会导致重复消费,`ReplacingMergeTree` 或 `insert_deduplication_token`(§08.6)是常见兜底;其二,数据的拉取在**后台调度线程**里进行,消费进度的落点随引擎而异(Kafka 的传统实现交给 Kafka 消费者组,较新的实现与 NATS/S3Queue 把状态存在 Keeper),故障恢复靠它;其三,**格式与批大小**直接决定目标表产出多少 part,`kafka_format = 'JSONEachRow'` 这类逐行格式下要留意攒批,否则又会回到 §08.4 的"part 太多"。另外,`SELECT` 通常**不允许直接读**这张缓冲表(否则读走的消息可能被从队列里取走),要读得专门打开 `stream_like_engine_allow_direct_select`。

这些引擎的完整配置项(`kafka_*`、`rabbitmq_*`、S3Queue 的 `keeper_path` 等),以及"offset 存在哪、怎么 replay"的细节,集中在第 30 章。

## 08.10 错误与回退

导入真实世界的数据,出错是常态。ClickHouse 提供了几个"宽容度"设置:

`input_format_allow_errors_num` / `input_format_allow_errors_ratio` 允许一定数量或比例的**行**解析失败并被跳过。默认都是 0,也就是"一行坏就整条失败"。它的默认值不生效是有道理的:静默丢数据比报错危险得多。真要开,建议只开 `_num`,把它当成"我知道前 10 行里可能有表头错位"的一次性容忍,而不是长期配置。

`input_format_skip_unknown_fields` 决定"输入里出现了目标表没有的列"时是跳过还是报错。25.3 上默认是 `1`,也就是**默认跳过**多余列而不报错。这个默认值历史上确实变过,而且**现在能查到确切版本了**:上游 `SettingsChangesHistory.cpp` 把它记在 **22.6**(`false -> true`,理由是 "Optimize reading subset of columns for some input formats"),`FormatFactorySettings.h` 里的声明已经是 `DECLARE(Bool, input_format_skip_unknown_fields, true, ...)`。也就是说 22.6 之后的版本都是"默认放过"——**这意味着列名拼错、上游 schema 漂移都不会报错**;要严格模式得显式 `SET input_format_skip_unknown_fields = 0`(细节见第 19 章)。现场仍建议用 `system.settings` 复核一句,而不是照抄二手文档。

`NULL` 进入非可空列,后果**取决于走哪条路**,这是最容易踩的一处:

```sql
-- 路径 A:VALUES 字面量
INSERT INTO t (id) VALUES (NULL);        -- 报错:类型不匹配
-- 路径 B:格式导入
-- CSV 里的 \N 是 NULL;若列是 UInt64 非可空
```

走 SQL 字面量时,`NULL` 进非可空列是类型错误,直接失败。走文本格式时,`input_format_null_as_default`(默认 1)会把输入里的 `NULL` 记号替换成该列的**默认值**而不是报错 —— 也就是说,`NULL` 进去,`0` 出来,没有任何警告。

`CHECK` 约束失败则没有回旋余地:建表时写了 `CONSTRAINT c CHECK expr`,插入的数据不满足条件时,这条 `INSERT` 直接失败并报约束被违反的错误,数据不落盘。它的语义是"整条插入失败",不是"跳过这一行"。

## 08.11 常见坑清单

**一次插一行,写成十万次 `INSERT`。** 后果:十万个 part,超过 `parts_to_throw_insert` 之后直接报 `Too many parts`;即使没超,后台合并也永远追不上,查询性能断崖式下跌。正确做法:客户端侧攒批,或者打开 `async_insert` 让服务端替你攒。

**把 CSV 里的空串当成 `NULL`。** 后果:非可空列上,空串会变成默认值(数值列变成 0,日期变成 1970-01-01);`Nullable(String)` 列上,空串仍然是空串而不是 `NULL`,于是 `count()` 和 `count(col)` 对不上,聚合结果静默出错。正确做法:CSV 里用 `\N` 表示 `NULL`(可用 `format_csv_null_representation` 改),并且在导入前用 `clickhouse-local` 先看一眼数据的真实形状。

**在 `INSERT ... VALUES` 里写几十万行字面量。** 后果:耗时几乎全花在 SQL 解析和逐值类型转换上,写入本身的 IO 反而不占时间;内存也会因为整条语句被解析成 AST 而膨胀。正确做法:超过几千行就换成 `FORMAT CSV` 或 `INSERT ... SELECT`。

**在 `Distributed` 表上 `INSERT`,以为它和本地表一样。** 后果:写入会先落到 Distributed 表所在节点的本地存储,再按分片键拆分转发给各个分片;默认情况下 `INSERT` 在转发完成之前就可能返回,于是你"明明插入成功"却查不到数据;每转发一次,目标分片上就产生一批 part。正确做法:能不用 `Distributed` 表做入口就不用(直接用 `remote()`/集群表函数分发,或写本地表);确实要用时把这个开关设成 `1`,让它变成同步转发 —— 25.3 上它的主名是 `distributed_foreground_insert`(`insert_distributed_sync` 是它的别名),默认 `false`(即后台异步转发),以 `system.settings` 为准。细节见第 23 章。

**忘了 `INSERT` 也会受 `parts_to_delay_insert` 影响。** 后果:写入正常但越来越慢,以为是磁盘问题,其实是单分区 part 数越过了软闸,插入被指数退避限速。正确做法:先查 `system.parts` 的单分区 part 数,再看 `system.merges` 有没有合并任务在跑。

**碰到 `max_partitions_per_insert_block` 报错却以为是"数据太大"。** 后果:写入报出"一次插入触及的分区数超过上限",直觉反应是"这批数据太大了,再拆细一点",结果把批次切得更碎、每批反而铺进更多分区,报错更频繁。根因从来不是数据量,而是**分区键太细**:一批数据同时落在上百个分区里。正确做法:先看 `system.parts` 里这张表的分区数是不是异常多,确认是分区粒度问题后,把日分区换回月分区、或去掉拿高基数列当分区键的写法——而不是继续切小批次。

## 08.12 与 mini-clickhouse 的对照

mini-clickhouse 的写入路径分成两层,与真实 ClickHouse 对得很齐。这一段分"对齐点"与"简化点"两部分。

**对齐点。** 解释器侧是 `minich/execution/interpreter.py` 里的 `InterpreterInsertQuery`,它做的事与真实 ClickHouse 一一对应:三种来源分流(`node.values` 走 `_build_values_block` 逐行求值 + 按列类型转换,`node.select` 走一次 `InterpreterSelectQuery`,`node.format` 走 `_read_format` 交给格式解析器);`_align_block` 承担"补 `DEFAULT` / `MATERIALIZED`"那一整块(按名字或位置映射输入列、缺列用默认值填、**显式写 `MATERIALIZED` 列直接抛错**、最后按表的列顺序排好);最后 `table.write(self.ctx, block)` 交给存储引擎。存储侧的入口是 `minich/storages/merge_tree/merge_tree.py` 里的 `MergeTreeData.write()`,它的六步生命周期(**补默认值 → 约束检查** → 按分区分块 → 按 `max_insert_block_size` 再切 → part 数量闸门 → `write_part()` 并登记;实现里 `_fill_defaults()` 排在 `_check_constraints()` 之前,所以约束检查看到的是补过默认值之后的数据)与 §08.1 的流水线一致;真正落盘的 `part.py::write_part()` 先 `_sort_block` 按排序键排序,再算分区值、切 granule,然后 `disk.temp_path_in(path, prefix="tmp_insert")` 建临时目录、`_write_part_files` 写全部文件、最后 `disk.move(tmp_path, path)` 原子改名(§08.2)。part 名拼法与真实一致,见 `format_part_name(partition_id, min_block, max_block, level)`,也是四段式;磁盘抽象在 `minich/storages/merge_tree/disk.py`,它的模块文档把"为什么把原子性压缩成一个 rename 原语"讲得比本章还细。引擎家族(以及 §08.6 的 `ReplacingMergeTree`)注册在 `minich/storages/merge_tree/engines.py`:`StorageMergeTree`、`StorageReplacingMergeTree`、`StorageSummingMergeTree`、`StorageAggregatingMergeTree`、`StorageCollapsingMergeTree`、`StorageGraphiteMergeTree`、`StorageReplicatedMergeTree` 都继承同一个 `MergeTreeData`,差别只在"合并时对键相同的行做什么"——`ReplacingMergeTree` 的语义在 `merger.py` 的 `_emit_replacing` 里,连 `is_deleted` 软删除列都实现了。

**简化点。** 几处刻意的取舍:

- **整条 `INSERT` 先物化成 `Block` 再切块。** 真实 ClickHouse 在输入流上按 `max_insert_block_size` 反复切块,mini 先把输入收集成 `Block`,再按分区、按 `max_insert_block_size` 切。语义一致(包括"超了只警告不报错"),但 mini 是一次性物化,看不出"边收边写"的流式效果。`PartWriteContext` 里的 `block_number` 默认 1、`level` 默认 0,对应 part 名后两段。
- **part 布局的简化。** 真实 ClickHouse 的多个 granule 共用一个压缩块,mini 是"一个 granule 一个压缩块"(于是 mark 里 `offset_in_decompressed_block` 恒为 0);真实用一个 **16 字节的 `CityHash128`** 校验压缩块(上游 `CompressedWriteBuffer.cpp` 里写的是 `CityHash_v1_0_2::CityHash128(...)` 的 `low64`/`high64`,读侧 `CompressedReadBufferBase.cpp` 用同一个哈希复算并比对),mini 换成 4 字节的 zlib CRC-32(`minich/compression.py` 的块头注释里写明了这是有意替换);真实在 `INSERT` 时**不建**跳数索引(留到 merge 时),mini 在写入时就建(`PartWriteContext.build_skip_indexes = True`)。
- **块级去重是简化实现。** `minich/replication.py` 里 `insert_deduplicate=1`(默认)会为块算一个 `block_id`,再查"这个块见过没有"来决定是否跳过(`paths.has_block(block_id)`),用进程内的路径记录模拟真实 ClickHouse 在 Keeper 里的块指纹表;`insert_deduplication_token`、`replicated_deduplication_window`、`deduplicate_blocks_in_dependent_materialized_views`(§08.6)在 mini 里**都没有实现**。
- **写入闸门只实现了"part 数"那一轴。** `MergeTreeData.write()` 里有 `parts_to_delay_insert`(1000)与 `parts_to_throw_insert`(3000)两道按分区统计的闸,但没有 `max_partitions_per_insert_block`(§08.4)这条"单块跨分区数"的上限——mini 的写入路径不检查一个块铺进了多少个分区。
- **`Buffer` 引擎实现了,流式引擎没有。** `minich/storages/buffer.py` 的 `StorageBuffer` 就是 §08.8 的实物,模块文档把"非正常退出会丢数据""只把它当写入入口"讲得比正文还直白;而 Kafka/RabbitMQ/NATS/S3Queue 这类**流式引擎在引擎注册表里不存在**。
- **`async_insert` 与 `SYSTEM FLUSH ASYNC INSERT QUEUE`(§08.7)没有实现**:mini 没有服务端攒批与异步插入队列。

如果想亲手看到"一次 INSERT 一个 part",可以这样跑(`Context` 从 `minich.execution.context` 导入):

```python
from minich.execution.context import Context

ctx = Context()
ctx.execute("CREATE TABLE t (id UInt64, s String) ENGINE = MergeTree ORDER BY id")
for i in range(5):
    ctx.execute(f"INSERT INTO t VALUES ({i}, 'v{i}')")

print(ctx.execute("SELECT count() FROM system.parts WHERE table = 't'").text("TSV"))
```

这段代码演示的是本章最想让你记住的那句话:**五行数据,五次 INSERT,至少五个 part**。把五次插入合并成一次,part 数会相应减少 —— 这就是 8.4 节那个数量级差异的微观版本。

## 08.13 小结

- 一条 `INSERT` 的真实顺序是:切块 → 补默认值 → 按排序键排序 → 切 granule → 逐列写进 `tmp_insert_*` → **原子改名**。临时目录 + 原子改名保证"读的人永远看不到半个 part",这是无锁读路径的地基。
- 三种写法里,`VALUES` 走 SQL 解析器,最慢;`SELECT` 与带格式的批量数据才适合导入。真正决定代价的不是"写多快",而是"产生多少个块",因为**一个块一个 part**。
- part 数量按分区计数。`parts_to_delay_insert`(默认 1000)是限速软闸,`parts_to_throw_insert`(默认 3000)是报错硬闸,用 `system.parts`(`active = 1`)观察。(这两个默认值都上调过,但分属两个版本:`parts_to_throw_insert` 的 300 → 3000 在 23.6,官方文档有明确标注;`parts_to_delay_insert` 的 150 → 1000 更晚,托管 ClickHouse 的文档把它系于 25.1。)
- 写入还有一道管着**另一个轴**的闸:`max_partitions_per_insert_block`(默认 100)限制**单个块跨了多少个分区**,超了整批插入失败。它反映的是**分区粒度太细**,治法是换粗的分区粒度或分桶写入,不是继续把批次切小。
- ClickHouse 默认不按主键去重。重试安全靠**块级去重**(复制表 + `insert_deduplicate`,可用 `insert_deduplication_token` 自定义键、用 `replicated_deduplication_window` 调窗口、用 `deduplicate_blocks_in_dependent_materialized_views` 把 token 传播到 MV)和**表引擎语义**(`ReplacingMergeTree` 的"最终一致");`insert_quorum` 管的是副本确认,不是去重。
- 攒批有两条路:服务端的 `async_insert`(先写 WAL,推荐)与内存里的 `Buffer` 引擎(异常重启丢数据、读到的是一半、语义受限,已基本被 `async_insert` 取代)。
- 数据源是持续事件流时,用 Kafka/RabbitMQ/NATS/S3Queue 这类**流式引擎**当缓冲表,再挂物化视图写进 MergeTree;去重与消费进度落点要提前设计。
- 导入真实数据时最容易踩的四类坑:一次一行;把空串当 `NULL`;把几十万字面量塞进 `VALUES`;在 `Distributed` 表上插入却不设同步语义。
- mini-clickhouse 把同一套路径实现在 `InterpreterInsertQuery` 与 `part.write_part()` 里,差异集中在"切块被简化""一个 granule 一个压缩块""写入时即建跳数索引""没有块指纹去重"这四处。
