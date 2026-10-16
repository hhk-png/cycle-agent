# 第 27 章 可观测性与 system 表

"我的 ClickHouse 怎么了"——这个问题的答案几乎全在 `system` 库里。它慢在哪、内存被谁吃了、写入为什么被拒、副本落后多少、磁盘为什么涨、哪类错误在变多,全都能用一条 SQL 查出来,不用 SSH 上去翻文件。这一章要做两件事:一是把`system` 库按"回答什么问题、什么时候查它"梳理清楚;二是把最容易写错的监控判断(把累积计数器当瞬时值来做阈值告警)讲透。

## 27.1 三层观测:指标、日志、追踪

先把工具箱分三层,别混着用:

- **指标(metrics)**:数值,适合画图和告警。`system.metrics` 与 `system.asynchronous_metrics` 是**瞬时值(gauge)**,`system.events` 是**累积计数器(counter)**。
- **日志(logs/events)**:带时间戳的事件明细。`system.query_log`、`system.query_thread_log`、`system.part_log`、`system.text_log`、`system.crash_log`、`system.metric_log`。
- **追踪(traces)**:跨节点/跨阶段的耗时分解。ClickHouse 内置的**查询剖析器**把每个算子/线程的耗时写进 `system.trace_log`(见 §27.7),它是生产里最常用的剖析面;要接入外部生态时还有 `system.opentelemetry_span_log`(见 §27.19)。

**gauge 与 counter 的区别是这一章最重要的一个知识**,搞混会让监控告警直接失效。

```text
system.metrics        Query = 3        现在正在跑 3 条查询          ← 可上可下(gauge)
system.events         Query = 12345    自启动以来累计跑了 12345 条   ← 只增不减(counter)
```

推论:**对 counter 做阈值告警必然误报**。你写"`Query > 5` 就告警",服务跑一会儿,累计查询数一定超过 5,于是必然触发——而且从此永远处于告警状态。counter 的正确用法只有一种:**取两次快照相减,得到"这段时间内的增量"**,再对这个速率(增量/时间)报警。`system.query_log` 里每行的 `ProfileEvents` 列,本质上就是"这条查询的 counter 增量"。

真实 ClickHouse 里 gauge 与 counter 还各有侧重:`system.metrics` 是"读起来便宜、就地更新"的瞬时量(活跃查询数、连接数),`system.asynchronous_metrics` 是"后台线程周期性采样、读起来贵"的量(内存分配器的细项、`/proc` 里的东西、磁盘剩余)。它的刷新周期由服务端配置 `asynchronous_metrics_update_period_s` 控制,默认 **1 秒**;另有一项 `asynchronous_heavy_metrics_update_period_s` 专门管那些更重的采样项,默认 **120 秒**——重的采样项单独放到一个更慢的周期里,避免每次都把 `/proc` 那一批全扫一遍。想让轻量指标更实时就调小前者,但它越勤、后台采样开销越大——1 秒已经是很激进的默认值,不要凭感觉再往下压;后者(120 秒)一般不用动。

## 27.2 `system.parts`:排查 "too many parts" 与磁盘增长的第一站

`system.parts` 是 MergeTree 家族在磁盘上的 **part 清单**:数据被切成了多少块、每块多大、属于哪个分区、哪一块是活跃的。它是回答两个常见问题的唯一入口。

关键列:`active`(是不是当前有效的 part)、`rows`、`bytes_on_disk`、`marks`(索引标记数)、`level`(合并层级)、`min_block_number` / `max_block_number`(这个 part 覆盖的插入块区间)、`partition`、`part_type`(`Wide` 还是 `Compact`)、`data_compressed_bytes` / `data_uncompressed_bytes`、`primary_key_bytes_in_memory`。

```sql
-- 每个分区的活跃 part 数与总大小 —— "为什么插入越来越慢"
SELECT database, table, partition,
       count() AS parts, sum(rows) AS rows,
       formatReadableSize(sum(bytes_on_disk)) AS size
FROM system.parts
WHERE active
GROUP BY database, table, partition
ORDER BY parts DESC
LIMIT 10;
```

几条判读经验:

- **`active = 0` 的行是"被合并取代、等待删除"的旧 part**。真实 ClickHouse 会保留它们一段时间(`old_parts_lifetime`,默认 480 秒),因为可能还有查询在读。"磁盘占用比数据量大"最常见的解释就是新旧 part 并存——过几分钟再看,旧的会消失。
- **单个分区活跃 part 数逼近 `parts_to_delay_insert`(现值 1000)**,写入就开始被限流;逼近 `parts_to_throw_insert`(现值 3000)就报 `TOO_MANY_PARTS`。这两个是 MergeTree 表设置,现值以 `system.merge_tree_settings` 为准:尤其 `parts_to_delay_insert` 的 `150 → 1000` 改动**版本归属有冲突**(官方 changelog 的 23.6 与托管版文档的 25.1 两种说法,确切版本未确认,见 §25.8),`parts_to_throw_insert` 的 `300 → 3000` 则明确系于 23.6。它们是**保护机制不是调优参数**——别为了消掉 `TOO_MANY_PARTS` 就把阈值往上调。
- **`part_type` 是 `Wide` 还是 `Compact`** 由 `min_bytes_for_wide_part`(默认 10 MiB)决定:小 part 用 `Compact`(所有列挤在一个文件里)以省文件句柄。看到大量小 `Compact` part,基本可以断定写入太碎。
- **`level` 越大说明这个 part 是越多次合并的产物**;如果所有 part 的 `level` 都很低、数量很多,说明后台合并没跟上或没触发。

磁盘为什么涨,还可以对照 `system.parts` 的 `bytes_on_disk` 与 `system.tables` 的 `total_bytes`(§27.3)一起看:前者是磁盘真实占用(含索引、含未清理的旧 part),后者是表的逻辑大小。

## 27.3 `system.tables` / `system.columns` / `system.databases`

这三张是"结构速查"。

`system.tables` 每行一张表:`engine`(引擎族名,如 `MergeTree`)、`engine_full`(带参数的完整名)、`partition_key` / `sorting_key` / `primary_key`、`total_rows`、`total_bytes`、`create_table_query`、`metadata_path`。**`engine` 与 `engine_full` 的区别经常被搞混**:`ENGINE = MergeTree ORDER BY x` 的 `engine` 是 `MergeTree`、`sorting_key` 是 `x`;而 `engine_full` 才带完整参数。

```sql
-- 容量 Top 10,一眼看出哪张大表在占地方
SELECT database, name, total_rows,
       formatReadableSize(total_bytes) AS size
FROM system.tables
WHERE database NOT IN ('system', 'INFORMATION_SCHEMA')
ORDER BY total_bytes DESC LIMIT 10;
```

`system.columns` 每行一列:`type`、`position`、`default_kind`(DEFAULT / MATERIALIZED / ALIAS / EPHEMERAL)、`default_expression`、`codec_expression`、`ttl_expression`,以及两个非常有用的容量列 `data_compressed_bytes` 和 `data_uncompressed_bytes`。**后者是"哪个列最占地方"的直接答案**——你常会发现一个 `String` 的日志列比所有其它列加起来还大,而它其实可以换成 `LowCardinality` 或加个编解码器。

```sql
-- 谁最占地方(按未压缩字节)
SELECT table, name, type,
       formatReadableSize(data_uncompressed_bytes) AS raw_size,
       formatReadableSize(data_compressed_bytes)   AS compressed
FROM system.columns
WHERE database = 'analytics'
ORDER BY data_uncompressed_bytes DESC LIMIT 15;
```

`system.databases` 看有哪些库、各自用的什么引擎(尤其要盯有没有还在用老的 `Ordinary` 库——它的 DDL 会互相加锁,是大集群的经典坑)。

## 27.4 `system.query_log`:慢查询的主表

**这是最重要的一张排障表,没有之一。** 每条查询在 `query_log` 里留下的事件行,把"干了什么、花了多少、多久、成没成"全记下来了。

关键列:

- `type`:`QueryStart` / `QueryFinish` / `ExceptionWhileProcessing` / `ExceptionBeforeStart`。**`QueryStart` 不是"成功"**,它只是"开始"。成功的查询最终以 `QueryFinish` 收尾;出错的有 `ExceptionWhileProcessing`(执行中失败)或 `ExceptionBeforeStart`(还没开始就失败,只此一行)。
- `read_rows` / `read_bytes`、`written_rows` / `written_bytes`、`result_rows` / `result_bytes`:本次查询读/写/返回的量。
- `memory_usage`:本次查询的**峰值内存**——找 OOM 元凶就靠它排序。
- `query_duration_ms`:执行时长。
- `ProfileEvents`:本次查询各计数器**增量**的映射。**这是最有价值的一列之一**:`SelectedRows` / `SelectedMarks` / `SelectedParts` 能直接告诉你"索引裁剪到底有没有生效"(见 §27.17)。
- `exception`:失败时的错误文本(含错误码)。
- `query`、`user`、`query_id`、`current_database`、`log_comment`。

```sql
-- 最慢的 10 条(只看成功且结束的)
SELECT query_duration_ms, read_rows,
       formatReadableSize(memory_usage) AS peak_mem,
       substring(query, 1, 80) AS q
FROM system.query_log
WHERE type = 'QueryFinish'
ORDER BY query_duration_ms DESC LIMIT 10;
```

**为什么查不到刚跑的那条查询?** 因为 `query_log` 不是内存里的一张表,而是一张由 **MergeTree 支撑的系统日志表**:服务端把查询记录先写进内存缓冲,再按 `flush_interval_milliseconds` 定时刷到表里。这个值的**文档默认值是 7500 毫秒**(约 7.5 秒),可在 `<query_log>` 配置段里改。所以刚结束的查询在 `query_log` 里可能还看不到——这是设计,不是 bug。想看最新的,先执行:

```sql
SYSTEM FLUSH LOGS;
```

`query_log` 的写入由 `log_queries`(默认 1)控制;`log_queries` 设为 0 会关掉它,官方**不建议**关,因为排障全靠它。还有两个开关:`log_queries_probability`(按概率采样写入,降低高频查询的日志量)、`log_formatted_queries`(额外记录格式化后的查询到 `formatted_query` 列)。

**日志表的生命周期:别让它把磁盘吃光。** 系统日志表都是 MergeTree,会一直长;保留策略由**服务端配置段**(不是会话设置)控制。以 `query_log` 为例:

```xml
<query_log>
    <database>system</database>
    <table>query_log</table>
    <engine>ENGINE = MergeTree PARTITION BY event_date ORDER BY event_time</engine>
    <flush_interval_milliseconds>7500</flush_interval_milliseconds>
    <ttl>event_date + INTERVAL 30 DAY</ttl>
    <max_size_rows>1048576</max_size_rows>   <!-- 行数上限,超了按 part 清理 -->
</query_log>
```

要点:`ttl` 按时间过期、`max_size_rows` / `max_size_bytes`(以及在旧写法里的 `max_size`)按容量上限清理,两者可同时用;`flush_interval_milliseconds` 决定缓冲多久刷一次(也是"查不到刚跑的查询"的根因);`SYSTEM FLUSH LOGS` 强制立刻把内存缓冲刷盘,是排查时最常用的手动动作。想减少写入量、又不想关日志,还有几条**会话/服务端设置**:`log_queries_min_type`(只记某类事件以上,如只记 `ExceptionWhileProcessing` 起的)、`log_queries_min_query_duration_ms`(只记慢于某毫秒数的查询)、`log_profile_events`(精简或关闭那几百个 `ProfileEvents` 列的记录)、`log_query_settings`(是否记录查询里改过的设置)、`log_queries_cut_to_length`(`query` 文本超长时截断到多少字节,防止一条巨 SQL 撑爆日志)。这几条的默认值随版本有过调整,以目标版本 `system.settings` 的实际输出为准。**生产上一定要给 `query_log` 配保留策略**——最典型的"磁盘无声无息满掉"就是日志表没设 `ttl`。

## 27.5 `system.query_thread_log`:哪条线程在拖后腿

`system.query_log` 只记整条查询的汇总,但一条查询在真实 ClickHouse 里会被拆到**很多执行线程**上(读线程、聚合线程、合并线程……)。`system.query_thread_log` 就是"每个执行线程一行":

```text
query_log:                q-1 总耗时 1000 ms
query_thread_log:         线程A 950 ms | 线程B 40 ms | 线程C 10 ms
                                    ↑ 瓶颈在 A,这才是"哪一步慢"的答案
```

它默认**不开**:`log_query_threads` 默认是 **0**。而且这里有两个**缺一不可**的前提,少一个这张表就是空的:(a) 会话/查询级设 `log_query_threads = 1`;(b) 服务端配置里**存在 `<query_thread_log>` 段**(和 `query_log` 一样,日志表由服务端按配置段创建)。很多人只记得 (a)、忘了 (b),于是"开了却还是查不到",根因其实在服务端配置——和 §27.15 的 `text_log` 一样,日志表是"设置 + 配置段"两把钥匙。

当真开了,它的列与 `query_log` 同构(同一套日志框架的另一张表):`type`(`QueryStart` / `QueryFinish` …)、`query_id`、`thread_id` / `thread_name` / `os_thread_id`、`query_duration_ms`、`read_rows` / `read_bytes`、`written_rows` / `written_bytes`、`memory_usage`,以及本线程的 `ProfileEvents` 映射。读法就是上面那个图:按 `query_id` 把一条查询的所有线程拉出来,按耗时倒排——

```sql
SELECT thread_id, thread_name, query_duration_ms, read_bytes, memory_usage
FROM system.query_thread_log
WHERE query_id = '...'
ORDER BY query_duration_ms DESC;
```

**最慢的那几个线程就是答案**:它们告诉你瓶颈是"读线程在等 IO"还是"某个算子在闷头算"。什么时候值得开:一条查询总耗时很长,但你看不出是"读慢"还是"某个算子在等";这时把线程拆开,瓶颈线程会立刻显形。代价是行数随线程数翻好几倍,所以默认关着,排查时才临时打开,查完就关。

## 27.6 `system.processes`:正在跑的查询

`system.processes` 是 `SHOW PROCESSLIST` 的底层表:`query_id`、`user`、`elapsed`(已经跑了多久)、`read_rows` / `read_bytes`(读到哪了)、`memory_usage`、`query`。两个用途:

```sql
-- 谁跑得最久
SELECT query_id, user, elapsed, read_rows, substring(query,1,60) AS q
FROM system.processes ORDER BY elapsed DESC;

-- 顺手杀掉
KILL QUERY WHERE query_id = '...' SYNC;
```

`is_initial_query` / `initial_user` / `initial_address` 这一组是为**分布式**准备的:当一条查询从 A 发到 B、C 执行时,B/C 上看到的 `is_initial_query = 0`,而 `initial_address` 指回发起方——排查"这条查询到底是谁发起的"就靠它们。`KILL QUERY` 为什么不一定立刻生效,见第 25 章 §25.6。

## 27.7 `system.trace_log` 与查询剖析:火焰图的来源

`system.trace_log` 是**查询剖析器(query profiler)**的落点,也是生产上最常用的剖析面——它在运行期把"时间花在哪些函数上"采样记录下来,可以直接据此画火焰图。

剖析器分两种,**默认周期都是 1 秒**(即开着);采样结果只有在 `<trace_log>` 服务端配置存在、表已建出来时才落盘。

- **实时剖析(wall-clock)**:由 `query_profiler_real_time_period_ns` 控制(默认 1000000000 纳秒 = 1 秒),对每个执行线程的调用栈采一次样。量的是**墙上时间**,包含等待、IO、等锁——回答"这段时间线程在干嘛"。
- **CPU 剖析**:由 `query_profiler_cpu_time_period_ns` 控制(默认同为 1 秒),量的是**CPU 时间**,不含等待——回答"CPU 真正烧在哪个函数上"。

两者互补:实时剖析看到等待,CPU 剖析看到计算。要更细地定位热点,要把周期调小(如实时 100000 ns = 0.1 秒),代价是采样开销变大。

**内存剖析是另一路**:`memory_profiler_step`(默认 4194304,即 4 MB)——查询每**多分配**这么多字节就采一次样,抓内存的分配点;`memory_profiler_sample_probability`(默认 0)——按概率对分配点采样,用于更细的分配画像。两者能回答"这 8 GB 是哪个调用栈吃掉的",在排查聚合/JOIN 的内存峰值时非常有用。

`system.trace_log` 的关键列(**注意 25.3 的表结构与早年不同,别再拿 `span_id` / `operation_name` / `duration_ns` 那一套旧列名来查**):

- `trace_type`:`Real`(墙上时间)/ `CPU`(CPU 时间)/ `Memory` / `MemorySample` / `MemoryPeak` / `ProfileEvent` 等——**先按它过滤**,否则几种采样混在一起,数字没有意义。
- `trace`:`Array(UInt64)`,**一次采样得到的一整条调用栈**,每个元素是进程内的一个虚拟内存地址(数组从根到叶)。
- `symbols` / `lines`:符号化后的函数名、以及文件名加行号,与 `trace` 一一对应;只有在 `<trace_log>` 里开了符号化(`symbolize`)才有值,否则是空数组。
- `thread_id` / `query_id`:哪个线程、哪条查询(把样本和 `system.query_log` 关联起来)。
- 内存类还有 `size` / `ptr`,`ProfileEvent` 类还有 `event` / `increment`。

**怎么从它画火焰图?** 25.3 的 `trace_log` 把每条调用栈**打平存成了 `trace`(`symbols`)这一个数组列**,不再是"每行一对父子指针"。所以画火焰图的做法是:**把每行的整个数组当作一条从根到叶的栈**——先按 `trace_type = 'CPU'`(或 `Real`)过滤出某个 `query_id` 的样本,再按这个数组 `GROUP BY` 取 `count()`(样本数占比 ≈ 时间占比),数组本身就是路径、不需要任何回溯;要交给外部工具就把它导出成 speedscope 或 `flamegraph.pl` 的栈折叠格式。判读只剩一条:**横轴越宽 = 越耗时**,某个函数占了大半宽度,它就是优化的靶子。这套"先量再调"的用法在第 28 章 §28.10 还有展开。

**开销提醒。** 采集本身有成本——高并发线上把周期压得很低、或把 `trace_log` 长期开着,会明显增加 CPU 和写入。把它当"排查时临时打开、或者只对重要表开启"的工具,而不是常开的。

## 27.8 `system.part_log`:part 的一生

`system.part_log` 记录 **part 级别的生命周期事件**——每一次 part 的诞生、合并、下载、删除、mutation、搬移都在这里留一行。`system.merges` 只能看**正在跑**的合并,`part_log` 能看**已经跑完**的。

`<part_log>` 配置段与 `query_log` 同构(ttl / 大小上限 / `flush_interval_milliseconds`),同样要配保留策略。

`event_type` 的取值:

- `NewPart`:一次 `INSERT` 落成一个新 part。
- `MergeParts`:若干 part 合并成一个(含后台合并与 `OPTIMIZE`)。
- `MutatePart`:mutation(`ALTER UPDATE` / `DELETE`)重写了这个 part。
- `DownloadPart`:副本从别的副本拉来了这个 part。
- `RemovePart`:part 被删除(被合并取代、`DROP PARTITION` 等)。
- `MovePart`:part 在卷/盘之间被搬移(冷热分层)。

关键列:`event_type`、`event_time`、`database` / `table` / `partition_id`、`part_name`、`part_type`、`rows` / `size_in_bytes`、`duration_ms`(这次操作花了多久)、`merge_reason` / `merge_algorithm`,以及合并事件会带的 `source_part_names`。

**它最直接的用途:看哪些合并最慢、最贵。**

```sql
-- 最近一小时最耗时的 10 次合并
SELECT event_time, database, table, partition_id,
       source_part_names, rows, formatReadableSize(size_in_bytes) AS size,
       round(duration_ms / 1000, 1) AS seconds, merge_algorithm
FROM system.part_log
WHERE event_type = 'MergeParts' AND event_time > now() - 3600
ORDER BY duration_ms DESC
LIMIT 10;
```

判读经验:合并耗时长且 `size_in_bytes` 大,是在合大 part(正常,但要确认没和前台查询抢 IO);短时间大量小 `MergeParts`,说明写入太碎、合并疲于奔命;`NewPart` 频繁而 `MergeParts` 稀少,说明合并跟不上写入(`Too many parts` 的前兆);大量 `MutatePart` 说明有人在跑 `ALTER UPDATE` / `DELETE`,配合 §27.9 的 mutation 进度看。合并与 mutation 的机制在第 18 章。

## 27.9 `system.merges` / `system.mutations`:后台任务进度

`system.merges` 是**当前正在跑**的合并与 part mutation:`database` / `table`、`elapsed`、`progress`(0~1)、`num_parts`(参与合并的 part 数)、`result_part_name`、`is_mutation`(1 表示这其实是 mutation)、`rows_read` / `rows_written`、`memory_usage`、`merge_type` / `merge_algorithm`。

```sql
-- 合并是不是卡住了:有一条 elapsed 几百秒还没动,基本就是卡住了
SELECT database, table, elapsed, progress, num_parts, result_part_name
FROM system.merges ORDER BY elapsed DESC;
```

`system.mutations` 是 mutation(`ALTER UPDATE` / `ALTER DELETE`)的进度与状态:`mutation_id`、`command`(改动命令)、`create_time`、`parts_to_do`(还有多少 part 要处理)、`is_done`、以及最关键的 `latest_fail_reason` / `latest_fail_time`。**mutation 失败最常见的表现就是它一直不 `is_done`,而失败原因写在 `latest_fail_reason` 里**——不查这一列,你会一直以为是"它还在跑"。

## 27.10 `system.replicas` / `system.replication_queue`:副本健康

`system.replicas` 每行一个复制表:`is_leader` / `is_readonly`、`absolute_delay`(这个副本落后 leader 多少秒)、`queue_size`、`inserts_in_queue` / `merges_in_queue`、`total_replicas` / `active_replicas`、`last_queue_update`、`zookeeper_path`。

`is_readonly = 1` 是很强的信号:副本要么丢过 Keeper 会话,要么缺了某个 part 的数据,已经**拒绝写入**——这时它只会等着被别人修,不会自己好。`absolute_delay` 持续增长则是副本跟不上。

`system.replication_queue` 细看队列里**每一条待办**:`type`(GET_PART / MERGE_PARTS / DROP_RANGE 等)、`num_tries`、`last_exception`、`last_attempt_time`。**队列里 `num_tries` 一直涨、`last_exception` 一直同一个错**,是"复制卡住了"的标准症状。细节与处置指向第 22 章(复制与 Keeper)。

## 27.11 `system.distribution_queue` 与 `system.distributed_ddl_queue`

这两张表是**分布式写入**的观测面,对应第 23 章的分片与分布式 DDL。

`system.distribution_queue`:往 `Distributed` 表 `INSERT` 时,数据先落到接入节点本地磁盘上的一个**队列目录**,再由后台异步发给各分片。这张表每个队列一行,关键列:`database` / `table`、`data_path`(本地队列目录)、`is_blocked`(**是否被阻塞——为 1 说明发送卡住了**)、`error_count`、`last_exception`、`data_files` / `data_compressed_bytes`(还积压多少)、`broken_data_files`(损坏、发不出去的文件)。**判读:`is_blocked = 1`、或 `error_count` 持续增长加 `last_exception` 非空,就是"数据卡在接入节点没发出去"**——分布式写入最典型的故障,常见根因是某个分片不可达或磁盘满;`data_files` 一直堆积则说明发送速度跟不上写入(或下游分片慢)。

`system.distributed_ddl_queue`:用 `ON CLUSTER` 执行 DDL 时,这条 DDL 进 Keeper 的队列、由各节点依次取出执行。这张表就是那个队列的视图,关键列:`entry` / `entry_version`、`query`、`status`(各节点的执行状态)、`host`(**单数**,对应的端口是 `port`)与发起方 `initiator_host` / `initiator_port`、`exception_code` / `exception_text`。**判读:`status` 里出现非 `Finished` 的项、或 `exception_text` 非空,说明某次集群级 DDL 有的节点成功、有的失败**——分布式 DDL 最常见的情形,必须查它才知道哪些节点没跟上。

**实操:三条查询把"积压、坏文件、DDL 卡在哪"一次问清。**

```sql
-- ① 分布式写入的积压:哪些 Distributed 表卡住了、积了多少、错在哪
SELECT database, table, is_blocked, error_count,
       formatReadableSize(data_compressed_bytes) AS pending,
       data_files, broken_data_files, last_exception
FROM system.distribution_queue
WHERE is_blocked = 1 OR error_count > 0 OR data_files > 0
ORDER BY data_files DESC;

-- ② 只看"坏文件"(损坏、永远发不出去的那些,必须先人工处理)
SELECT database, table, data_path, broken_data_files, last_exception
FROM system.distribution_queue
WHERE broken_data_files > 0;

-- ③ 集群级 DDL 卡在哪个节点:哪些 entry 没走到 Finished、哪个 host 报了错
SELECT entry, query, status, host, port, initiator_host, exception_code, exception_text
FROM system.distributed_ddl_queue
WHERE status != 'Finished' OR exception_text != ''
ORDER BY entry DESC;
```

三条查询的读法:①里 `is_blocked = 1` 或 `error_count` 持续增长,就是"数据卡在接入节点没发出去",常见根因是某个分片不可达或磁盘满;`data_files` 一直堆积则是发送速度追不上写入(或下游分片慢)。②的 `broken_data_files` 不为零意味着队列里有**发不出去的坏文件**,它们不会自己消失——得按 `data_path` 到本地队列目录查清楚(磁盘损坏、权限、格式),处理掉之后队列才会继续推进。③的 `status` 是**各节点的执行状态**——一次 `ON CLUSTER` 的 DDL 有的节点成功、有的失败,就在这里显形;`exception_text` 非空的那个 host,就是需要单独去修的节点。

两张表的列名与格式随版本有过调整,以目标版本 `system` 表的实际列为准。

## 27.12 `system.metrics` / `system.events` / `system.asynchronous_metrics`

这三张一起用,但记住 §27.1 的区别。

生产上**必须监控**的几条(前面是对应的表/列):

- **内存**:`system.metrics.MemoryTracking`(服务端分配的字节)——逼近 `max_server_memory_usage` 就意味着快 OOM 了。更细的分配器信息在 `system.asynchronous_metrics`。
- **连接数**:`system.metrics.TCPConnection` / `HTTPConnection`——突增往往意味着客户端重连风暴或连接泄漏。
- **后台 merge 任务数**:`system.metrics.BackgroundMergesAndMutationsPoolTask`——长期贴着 `background_pool_size` 顶格说明合并跟不上写入。
- **part 数**:`system.metrics.PartsActive` 是**最该告警的那一个**——它数的是当前活跃 part 总数,长期只涨不降就是合并跟不上(配合 §27.2 的 `system.parts` 聚合看是哪个库/表在涨)。另有 `PartsTemporary` / `PartsPreActive` 反映合并/写入中间态的 part(`PartsPreActive` 是现行名,`Committed` 一系在改名后已改叫 `Active`,`PartsPreCommitted` 只是已废弃的旧别名)。要注意 mini-clickhouse 内部**仍沿用旧名 `PartsPreCommitted`** 作为它的 metric 键,对照 mini 的 `system.metrics` 输出时别以为书里写错了。很多人误以为该盯的是别的 part 计数,实际能反映"part 爆炸"趋势、值得配阈值告警的正是 `PartsActive`。
- **写入被限流**:`system.metrics.DelayedInserts`——不为零说明已经因为 part 太多在被打回。
- **只读副本**:`system.metrics.ReadonlyReplica`——大于 0 就要查 §27.10。
- **失败查询数**:`system.events.FailedQuery`(counter,看增量),"错误率"的最好来源之一。
- **磁盘剩余**:`system.asynchronous_metrics.FilesystemMainPathAvailableBytes`(同族还有 `FilesystemMainPathTotalBytes` / `FilesystemMainPathUsedBytes`)。注意 `FilesystemAvailable` 只是一个 SQL 函数(`filesystemAvailable()`),**不是**异步指标名。
- **副本延迟**:`system.replicas.absolute_delay`(或 `system.asynchronous_metrics` 里的副本延迟指标)。

`system.events` 的名字与 `ProfileEvents` 一一对应,值只增不减,所以正确用法是**定期采样、算速率**。

**`system.metric_log` 与 `system.asynchronous_metric_log`:指标的落盘版。** 上面这几张表是**当前瞬时值**,要做"指标随时间的曲线"就得靠日志表。

`system.metric_log` 的形态很关键:它是**宽表(WIDE)**——不是"每行一个指标名 + 值",而是**每行一个时刻、每个指标占一列**。`system.metrics` 与 `system.events` 两类快照都被拍进这一行(异步指标不在这张表里,它们另走 `system.asynchronous_metric_log`),但列名**不是**指标名本身,而是**带前缀**的:来自 `system.events` 的计数叫 `ProfileEvent_<名字>`、来自 `system.metrics` 的当前值叫 `CurrentMetric_<名字>`,外加 `event_time` / `event_date`。所以查曲线要用带前缀的列名:`SELECT event_time, ProfileEvent_Query FROM system.metric_log WHERE event_time > now() - 3600`,而不是 `WHERE metric = 'Query'`。它由 `<metric_log>` 配置段控制,`collect_interval_milliseconds` 决定采样周期(默认 1000 毫秒),同样要配 ttl / 大小上限。

`system.asynchronous_metric_log` 则是另一种形态:它是"每行一个指标名 + 值"的**长表**。它**只覆盖 `system.asynchronous_metrics` 那一类**(异步采样的那些指标),**而不是"`metric_log` 的新旧两种写法之一"**。要分清:`metric_log` 这一张宽表里拍进的是 **metrics / events 两类**,而 `asynchronous_metric_log` 对应的是它**没有**覆盖的异步指标那一路——两者覆盖的指标并不重叠,不是同一张表的两种写法。

如果你在旧资料里看到"按 `metric` 列过滤"的写法,那是长表(如 `asynchronous_metric_log`)的用法。宽表 `metric_log` 就得直接用指标名当列名。

**还有第三张,回答的问题和前两张都不一样:`system.query_metric_log`。** 前两张是**服务端视角**——它们记录"这台机器当时是什么状态",你没法从它们回答"**是哪条查询把 `SelectedMarks` 抬上去的**"。`system.query_metric_log` 就是为这个问题准备的:它把 **`ProfileEvents` 按查询、按时间**落盘,于是"升级之后这条报表的读放大变大了"这类问题可以直接按 `query_id` 拉出时间序列,而不必去 `system.query_log` 里翻那几个已经聚合过的终值。25.3 上它由服务端配置段 `<query_metric_log>` 定义(随发行版模板一起给,写进 `system.query_metric_log`,采样周期 `collect_interval_milliseconds` 默认 `1000`、刷盘间隔 `flush_interval_milliseconds` 默认 `7500`);和 `query_log` 一样,**表在不在取决于配置段在不在**——`system.query_metric_log` 查不到时,先去配置里找那一段,别怀疑查询写错了。

把三张摆在一起看就清楚了:`system.metric_log` 回答"**这台机器**什么时候紧张",`system.asynchronous_metric_log` 回答"**操作系统和后台**那条线什么时候变了",`system.query_metric_log` 回答"**哪条查询**干的"。前两张用来定位时段,第三张用来定位人。

## 27.13 `system.errors`:为什么它比翻日志好用

`system.errors` 是**按错误类型聚合**的计数表:**每个 `(name, code, remote)` 组合一行**,带 `code`(错误码)、`value`(累计发生次数)、`last_error_time`、`last_error_message`,以及区分来源的 `remote`。

```sql
SELECT name, code, value, last_error_time, last_error_message
FROM system.errors ORDER BY value DESC LIMIT 20;
```

它比翻日志好在两点:一是**聚合**,一眼看出"现在最痛的是哪类错误";二是**结构化**,能直接按错误码排序、做告警(错误率)。

一个使用要点:`value` 是**累积**的(和 `system.events` 同一套用法),而且 `system.errors` 是**每个 `(name, code, remote)` 组合一行**——它只存"从服务端启动到现在的累计值",表里**没有时间维度**。所以它单靠自己回答不了"最近是不是在变多"。`last_error_time` / `last_error_message` 那两列则给出"最后一次是什么样"的样例,不用去别处找。

**它能直接回答什么。** 不加任何技巧,这两条查询最实用——当前每个错误码累计发生了多少次,以及最近还在发生的是哪几类:

```sql
-- 累计计数最高的错误(自服务端启动以来)
SELECT name, code, value, last_error_time
FROM system.errors
ORDER BY value DESC
LIMIT 20;

-- 最近 5 分钟内又发生过的那几类错误(按"最后发生时间"筛,不是按增量)
SELECT name, code, value, last_error_time, last_error_message
FROM system.errors
WHERE last_error_time > now() - 300
ORDER BY last_error_time DESC;
```

注意上面第二条的 `WHERE last_error_time > ...` 只是筛出"最近 5 分钟内又发生过"的错误,**`value` 仍是那个全量累计值**,不是它在这 5 分钟里新增的次数——别把 `value` 当窗口增量读。

**要看增量(和 counter 一个套路),必须取两次快照。** `value` 只增不减,直接对绝对值做阈值必然误报(§27.1 的纪律);而 `system.errors` 每次查询返回的都是**同一时刻**的一份累计快照——`value` 是"自服务端启动以来"的累计计数、表里**没有时间维度**,所以**在同一份快照内部相减得不到任何时间上的增量**。如果你在它自己身上写 `GROUP BY name, code` + `max(value) - min(value)`,得到的至多是**同一时刻**本地行与远端行的差(25.3 对同一个 `(name, code)` 最多给两行,靠 `remote` 列区分),**不是**这段窗口里新增了多少次。要拿到真正的增量,就得隔几分钟各采一次、把两份快照按 `name` / `code` 对齐相减:

```sql
-- 第一次采集:把当前累计值落到一张临时表
CREATE TABLE errors_snap_1 ENGINE = Memory AS
SELECT name, code, value AS v1 FROM system.errors;

-- …等待你关心的窗口(如 5 分钟)…之后再跑下面这条:

-- 第二次:用当前的 system.errors 与第一次的快照按 name/code 对齐相减
SELECT live.name, live.code, live.value - s.v1 AS new_errors
FROM errors_snap_1 AS s
INNER JOIN system.errors AS live
    ON s.name = live.name AND s.code = live.code
WHERE live.value - s.v1 > 0
ORDER BY new_errors DESC;
```

提醒一句:同一个 `(name, code)` 下本地行与远端行靠 `remote` 列区分,快照里**最好把 `remote` 也一起存、并在 JOIN 上一并作为对齐键**,否则本地行与远端行会在 JOIN 里交叉组合,把两个来源的计数混着减。

**要看历史趋势,用 `system.error_log`,不要再自己攒快照。** 两次快照相减适合临时排查;想做"错误率在过去一小时怎么变"或长期告警,靠的是 `system.error_log`——它把每个错误名随时间的取值采样落盘(由 `<error_log>` 配置段开启),本身就带时间维度,不用你自己存快照(§27.20 有它的列与用法,列结构随版本变化,那里有一条版本提醒)。

**错误率告警具体怎么写。** 做"错误率"更稳的来源是 `system.events.FailedQuery`(counter):用两次采集的增量除以窗口内查询总数得到失败率,再对**速率**设阈值。写成 Prometheus 规则时,因为 §27.19 导出的 events 是 counter,规则里必须用 `rate()` / `increase()`——例如 `rate(ClickHouseProfileEvents_FailedQuery[5m]) > 1` 才对,**千万不要对导出的 counter 绝对值写 `>` 比较**(那必然一告警就再也停不下来)。

**它和 `error_log` / `metric_log` / `query_log` 的分工。** 讲错误监控时会碰到几张表,别混:`system.errors` 是**按错误类型聚合的当前计数**(实时看"哪类错误最多、最后发生在什么时候");`system.error_log` 是这些计数**随时间落盘**的日志表——要看"错误数在过去一小时怎么涨",得靠它(见 §27.20);要注意 **`system.metric_log` 里拍的是 metrics / events 两类,并不直接含 `system.errors` 那份计数**(错误要落成系统内的时间序列,走的是 `error_log`,不是 `metric_log`;想把它当 Prometheus 指标看,则走 §27.19 那个端点的 `<errors>true`);而 `system.query_log` 里 `type` 为 `ExceptionWhileProcessing` / `ExceptionBeforeStart` 的行是**逐条查询的失败明细**(要还原"哪条查询、什么参数失败",查它)。四者是"计数聚合 / 时间序列 / 指标快照(不含它)/ 逐条明细"的关系。

## 27.14 `system.settings` / `system.merge_tree_settings`

`system.settings` 列出所有**查询层**设置的当前生效值:`name`、`value`、`changed`(相对默认改过没有)、`default`、`min` / `max`、`type`、`readonly`、`description`。排查"为什么这条查询的行为和我想的不一样",第一站就是看 `changed = 1` 的那几行,以及查询级 `SETTINGS` 有没有覆盖:

```sql
SELECT name, value, changed, type, default
FROM system.settings WHERE changed = 1 ORDER BY name;
```

`system.merge_tree_settings` 是另一套:那些由建表 `SETTINGS` 子句或服务端 `<merge_tree>` 段控制、**不随会话变化**的 MergeTree 参数,例如 `index_granularity`、`parts_to_delay_insert`、`parts_to_throw_insert`、`max_bytes_to_merge_at_max_space_in_pool`。设置存在哪张表,取决于它的归属——这是最常见的混淆点。

## 27.15 `system.text_log`:默认不存在的真相

很多人以为服务端日志就在 `system.text_log` 里、`SELECT` 一下就行,结果发现查不到。真相是:**这张表默认不开启**。

按当前官方文档,启用方式是加一段**服务端配置**(不是会话设置):

```xml
<text_log>
    <level>trace</level>                    <!-- 记录到哪个级别,默认 trace -->
    <table>text_log</table>
    <flush_interval_milliseconds>7500</flush_interval_milliseconds>
    <partition_by>toYYYYMM(event_date)</partition_by>
    <ttl>event_date + INTERVAL 30 DAY</ttl>
</text_log>
```

要点(不写精确到每个版本的建表 SQL,只讲结构性的事):把它放在 `config.xml` 或其 `config.d/*.xml` 片段里;表是**由服务端按这段配置创建的**(在首次 flush 或启动时,取决于版本),引擎是 MergeTree,支持 `partition_by` / `ttl` / `flush_interval_milliseconds` 等;`database` 参数在新版本里已废弃,日志表都在 `system` 库。想尽快看到内容,`SYSTEM FLUSH LOGS`。

这里要澄清一个流传很广的说法:网上(也包括本教程配套实现自己的文档注释)有"手工 `CREATE TABLE system.text_log (...)` 再 `SET text_log = 1`"的写法——**这条说法不成立**。`system` 库里的系统表由服务端自己管理(它们不能被 drop / alter,只能 detach),日志表更是**由对应的 `<text_log>` 服务端配置段创建**:服务端在启动或首次 flush 时按这段配置把表建出来,引擎是 MergeTree。手册式的"用一条用户 `CREATE TABLE` 再 `SET` 一下开关"在真实 ClickHouse 里**没有这条受支持的路径**(你可能是在别的系统、或早期文档的摆弄里见过类似写法)。所以"开日志"的正确方式就是上面那段配置,配好再 `SYSTEM FLUSH LOGS`。事后想改保留策略,写进配置的 `ttl` 段,或用 `ALTER TABLE system.text_log MODIFY TTL event_date + INTERVAL 14 DAY`——能 `ALTER` 的是 TTL 这类属性,但不改变"表由配置创建"这一点。

为什么值得开:它让"用 SQL 查日志"成立——不用 SSH 上去 `grep`,可以直接

```sql
SELECT event_time, level, query_id, message
FROM system.text_log
WHERE level IN ('Error', 'Warning') AND event_time > now() - 300
ORDER BY event_time DESC;
```

还能按 `query_id` 与 `system.query_log` JOIN,把"出错日志"和"那条查询"拼起来看。代价是日志量:级别开太细(`trace`)会显著增加写入,一般生产用 `warning` 或 `error`。

## 27.16 自查手册:几张"元数据"系统表

有几张表是"翻手册"用的,不需要监控,但排查时可以少搜一次网页。每张配一条最小查询:

- **`system.functions`**:每个函数一行,带 `name`、`is_aggregate`、`case_insensitive`、`description`。确认"这个函数在不在、是不是聚合函数":

  ```sql
  SELECT name, is_aggregate, case_insensitive FROM system.functions
  WHERE name ILIKE '%uniq%' ORDER BY name;
  ```

- **`system.data_type_families`**:数据类型家族,含别名(几个类型名指向同一族):

  ```sql
  SELECT name, alias_to, case_insensitive FROM system.data_type_families
  WHERE name = 'JSON';
  ```

- **`system.formats`**:所有输入/输出格式,带 `is_input` / `is_output`。想知道"能不能 `FORMAT Arrow`":

  ```sql
  SELECT name, is_input, is_output FROM system.formats
  WHERE is_output = 1 AND name ILIKE '%arrow%';
  ```

- **`system.table_engines`**:引擎清单,带 `supports_*` 系列能力列。确认某个引擎支不支持 TTL / 排序键:

  ```sql
  SELECT name, supports_ttl, supports_sort_order, supports_replication
  FROM system.table_engines WHERE name = 'MergeTree';
  ```

- **`system.dictionaries`**:字典的加载状态与内存占用(指向第 21 章)。`status` 不是 `LOADED` 的那几行,就是"为什么 JOIN / `dictGet` 报 `Dictionary not found`"的答案:

  ```sql
  SELECT name, status, last_exception, bytes_allocated
  FROM system.dictionaries WHERE status != 'LOADED';
  ```

- **`system.disks` / `system.storage_policies`**:盘与存储策略的组成。写多盘 / 冷热分层时必看:

  ```sql
  SELECT name, path, formatReadableSize(free_space) AS free FROM system.disks;
  SELECT policy_name, volume_name, disks, move_factor FROM system.storage_policies;
  ```

- **`system.clusters`**:集群拓扑(有哪些分片、每个分片有哪些副本),排查分布式查询(指向第 23 章):

  ```sql
  SELECT cluster, shard_num, replica_num, host_name, is_local
  FROM system.clusters ORDER BY cluster, shard_num, replica_num;
  ```

## 27.17 `ProfileEvents` 里最该盯的几条

`ProfileEvents` 既在 `system.events` 里累积,也以**每条查询的增量**形式进了 `query_log.ProfileEvents`。下面几条值得单独记,以及"它涨得快说明什么":

- `SelectedRows` / `SelectedBytes`:本次实际从表里读出的行/字节。与 `read_rows` 不同的是,它是**叶子扫描**的视角,能看清"裁剪之前到底碰了多少数据"。
- `SelectedMarks` / `SelectedRanges` / `SelectedParts`:**索引裁剪有没有生效的判据**。`SelectedMarks` 涨得快 = 扫了太多 granule = 主键/分区谓词没帮上忙;`SelectedParts` 涨得离谱 = 没有用上分区裁剪,把整表所有 part 都打开了。
- `ReadCompressedBytes` 与 `ReadUncompressedBytes`:两者之比就是**实际读到的压缩率**。比偏低(压缩不明显)说明列的编码没选好(指向第 20 章)。
- `MemoryAllocatorPurge`:分配器主动回收的次数。它涨得快,说明进程在为内存做挣扎——往往伴随 `MemoryTracking` 高位。
- `QueryCacheHits` / `QueryCacheMisses`:查询结果缓存的命中与未命中。命中率长期为 0,要么是缓存没开,要么是查询参数总在变(缓存键不重复)。
- `ZooKeeperRequests` / `KeeperRequests`:对协调服务的请求量。它涨得离奇,通常意味着复制表太多、或某张表的队列在空转重试——去看 `system.replication_queue`。

用法上永远记得:它们是 counter,要看"这条查询读了多少"就用 `query_log.ProfileEvents` 的**单条增量**;要看"服务最近读了多少"就用 `system.events` 的**两次快照相减**。

## 27.18 按症状查:排查速查

- **查询慢** → 先 `system.query_log` 按 `query_duration_ms` 倒排找到那条;看它的 `ProfileEvents.SelectedMarks` / `SelectedParts` 判断索引有没有用上;再看 `memory_usage` 是不是在拖地;必要时 `SET log_query_threads = 1` 后用 `system.query_thread_log` 找瓶颈线程。如果它还在跑,用 `system.processes` 看 `elapsed` 和 `read_rows`。
- **内存涨** → `system.metrics.MemoryTracking` 看总量,`system.query_log` 按 `memory_usage` 倒排找元凶,`system.processes` 看**正在**涨的那条。若总量逼近上限而单条都不大,考虑是并发把额度乘起来了(第 25 章)。
- **写入被拒** → 看错误文本:是 `TOO_MANY_PARTS`(去 `system.parts` 数分区 part,查 `parts_to_throw_insert`)还是 `MEMORY_LIMIT_EXCEEDED`(去 §27.4),或是 `QUOTA_EXCEEDED`(去 `system.quota_usage`)。`system.metrics.DelayedInserts` 不为零说明已经在被限流。
- **副本落后** → `system.replicas` 看 `absolute_delay` / `is_readonly` / `queue_size`,再进 `system.replication_queue` 看 `type` / `num_tries` / `last_exception`。指向第 22 章。
- **磁盘满** → `system.parts` 看 `active` 的 part 大小与数量(注意旧 part 还没删掉的假象),`system.tables` 找哪张表大,`system.columns` 找哪个列大,`system.disks` 看是哪块盘满。

## 27.19 与外部监控的对接

**Prometheus**:ClickHouse 内置了 Prometheus 抓取端点。在 `config.xml` 里配一段:

```xml
<prometheus>
    <endpoint>/metrics</endpoint>
    <port>9363</port>
    <metrics>true</metrics>
    <events>true</events>
    <asynchronous_metrics>true</asynchronous_metrics>
    <errors>true</errors>
</prometheus>
```

之后 `curl 127.0.0.1:9363/metrics` 就能拿到 `system.metrics` / `system.events` / `system.asynchronous_metrics` / `system.errors` 的指标(`/metrics` 与 `9363` 取自官方示例,端口可自选,只要没被占用)。注意这里导出的 `events` 是 counter,Prometheus 侧要用 `rate()` / `increase()`,不要直接对绝对值做阈值——回到 §27.1 那条纪律。

**一条能直接用的告警规则。** 因为导出的 events 是 counter,规则要落在**速率**上。比如"失败查询速率持续升高"与"有副本变只读"这两条(示意,指标名的前缀以你实际抓到的为准):

```yaml
groups:
  - name: clickhouse
    rules:
      - alert: ClickHouseHighFailedQueryRate
        expr: rate(ClickHouseProfileEvents_FailedQuery[5m]) > 1
        for: 10m
        labels:
          severity: warning
        annotations:
          summary: "ClickHouse 失败查询速率 > 1/s,持续 10 分钟"
      - alert: ClickHouseReadonlyReplica
        expr: ClickHouseMetrics_ReadonlyReplica > 0
        for: 5m
        annotations:
          summary: "有副本进入只读,去查 system.replicas"
```

第二条是 gauge(瞬时值),可以直接比阈值;第一条是 counter,必须包一层 `rate()`——这就是 §27.1 那条纪律在告警配置里的样子。

**抓取开销提醒。** Prometheus 端点不是免费的:`<events>true` / `<asynchronous_metrics>true` / `<errors>true` 每开一项,每次 scrape 都要在服务端把对应那批值组装一遍,指标越多、scrape 越频繁,开销越大。所以(1)别把 scrape 间隔设得过短(15 秒级别的默认通常够用,没必要 1 秒);(2)只开你真正要告警的几类,不需要的(event / errors 里的大多数)可以关掉省这份开销;(3)只是"想看一眼现状"的话,直接 `SELECT` 那几张 `system` 表,比为此专门开一个 scrape 端点更便宜。

**OpenTelemetry 追踪**:ClickHouse 有 `system.opentelemetry_span_log` 这类表,能把查询内部阶段的 span 记下来,用于接入 OTel 生态。**未确认**:较新版本引入了更原生的分布式追踪上下文传播能力,其具体配置项、默认开关与与 `opentelemetry_span_log` 的关系,我在 25.3 的官方文档中**没有核实**,使用前请查对应版本的文档,不要照搬其它文章的配置。

## 27.20 更多值得知道的 system 表(真实 ClickHouse 有、mini 未实现)

除了前面逐张讲过的那些,真实 ClickHouse 里还有一批系统表值得知道。它们大多是**日志表**(由 MergeTree 支撑、要配对应的 `<xxx_log>` 配置段来建表与设 TTL,默认可能未开启),**mini-clickhouse 的那 43 张表里没有它们**——mini 只实现了一组能演示机制的**子集**,下面这些是"真实 CH 有、mini 未实现"的补充。按"回答什么问题"列。

- **`system.backup_log`**:每次 `BACKUP` / `RESTORE` 的**持久**记录,与 §27 的备份监控直接相关。`system.backups` 是内存表、重启即清空,而 `backup_log` 落盘、可事后追查与审计。关键列:`status`(`CREATING_BACKUP` / `BACKUP_CREATED` / `BACKUP_FAILED` / `RESTORING` / `RESTORED` / `RESTORE_FAILED` / `BACKUP_CANCELLED` / `RESTORE_CANCELLED`)、`error`、`start_time` / `end_time`、大小与文件数。查法:

  ```sql
  SELECT status, error, start_time, end_time
  FROM system.backup_log
  WHERE event_time > now() - 86400 AND status LIKE '%FAILED%'
  ORDER BY event_time DESC;
  ```

- **`system.query_views_log`**:物化视图执行时的**运行时**记录——每次有数据进到源表、MV 被触发时这里留一行,记下是哪个 MV、处理了多少行、多久、成没成。排查"MV 是不是拖慢了写入""哪个 MV 报错了"就看它(普通查询走 `query_log`,MV 的运行时由 `query_views_log` 专门记)。

- **`system.session_log`**:TCP / HTTP 会话的登录与登出事件——谁、从哪个地址、什么时候连上又断开。做访问审计、排查"连接泄漏 / 重连风暴"时有用(连接数突增的指标是 `system.metrics.TCPConnection`,会话级的明细则在这里)。

- **`system.asynchronous_insert_log`**:异步插入(`async_insert = 1`)的每次落盘记录——一批异步插入什么时候被刷成 part、多少行、耗时多少。用异步插入时,它是排查"写入延迟到底花在哪一段"的入口。

- **`system.error_log`**:错误计数**随时间**的落盘版——`system.errors` 只给"当前累计值",而 `error_log` 把每个错误名随时间的取值采样下来。要看"错误率在过去一小时怎么变",查它,而不是只看 `system.errors` 的瞬时值(§27.13)。**注意这一列结构是 25.3 的口径**:那时它的列是 `hostname`、`event_date`、`event_time`、`code`、`error`、`value`、`remote`——错误名在 **`error`** 列,且**没有** `name`,也没有 `last_error_time` / `last_error_message`;更新的版本(含当前官方文档)已经把这几个名字/时间列补上了,所以别把"没有"当成永久事实,以你那一版 `DESCRIBE system.error_log` 的输出为准。

- **`system.processors_profile_log`**:查询执行**流水线里每个算子(processor)**的耗时记录,比 `trace_log` 的火焰图更"结构化"——直接按 processor 名给出耗时与输入/输出行数,回答"这条查询的时间花在哪个算子(读、聚合、Join、排序……)上"。

- **`system.dashboards`**:内置 Web UI(自带那个网页控制台)里**仪表盘的定义**。它存的不是指标数据,而是**看板的元数据**:**一行就是一块图表**——记的是它属于哪个看板、标题叫什么、以及驱动它的那条查询。意义在于"看板可以被 SQL 管理":把面板定义 `INSERT` 进去、随版本一起版本化、跟部署脚本一起分发,而不是只能在界面上手点。它的**确切列名、以及是否所有版本都默认存在,本书未逐条核对**(以你那一版 `DESCRIBE system.dashboards` 的输出为准);但"用一张 system 表来承载看板定义"这件事本身,足够让"监控面板怎么版本化"这个问题多一个答案。

- **`system.blob_storage_log`**:对对象存储(S3 等)的**操作审计**——每次对 S3 的读 / 写 / 删 / 列目录都记一行,带请求类型、大小、耗时、错误。排查"对象存储延迟高 / 请求量大 / 这波 IO 为什么这么贵"时靠它,也是核对 S3 请求费的依据。

这些表在真实 ClickHouse 上大多需要对应的 `<xxx_log>` 服务端配置段才会被创建并写入,与 §27.4 讲的 `query_log` 是同一套机制:**表由服务端按配置创建、由后台按 `flush_interval_milliseconds` 异步刷盘、靠 ttl 控制保留**。开哪几张取决于你要排查的问题——但**每一张开了就都要配保留策略**,否则又是"一张无声无息把磁盘吃掉的表"。

## 27.21 与 mini-clickhouse 的对照

mini-clickhouse 有一整包 `minich/system/`,在**接口层面**与真实 ClickHouse 同构:每张系统表是一个 `ISystemTable`(对应真实 CH 的 `IStorage`),解释器读它和读 MergeTree 走**同一条路径**,不需要任何后门。注册表在 `minich/system/base.py`,`system_table_names()` 是权威清单——当前注册了 **43 张表**:

```text
asynchronous_metrics, build_options, clusters, columns, contributors, crash_log,
current_roles, data_type_families, databases, dictionaries, disk_space, disks,
errors, events, formats, functions, grants, merge_tree_settings, merges, metric_log,
metrics, mutations, numbers, numbers_mt, one, part_log, parts, processes, query_log,
query_thread_log, quotas, replicas, roles, row_policies, settings, settings_profiles,
table_engines, tables, text_log, time_zones, timezones, users, warnings
```

按"数据是否真实"分三类,这正好对应本节要诚实交代的事:

**真实且有数据的**——`databases` / `tables` / `columns` / `parts` / `settings` / `merge_tree_settings` / `functions` / `data_type_families` / `formats` / `table_engines` / `metrics` / `events` / `asynchronous_metrics` / `query_log` / `build_options` / `time_zones` / `disk_space` / `disks`(列出本地磁盘,至少一行)/ `dictionaries`(挂上字典子系统时)/ `clusters`(注入 `ctx.clusters` 时)。另外 `merges` / `mutations` / `replicas` 是**运行时表**:空闲时 0 行,分别在有合并 / mutation / 复制表时才出现行——本章 §27.9、§27.10 讲的就是它们。

**schema 对齐(个别列收窄)、但恒空的**(刻意不伪造,文档里逐张说明了为什么):

- `query_thread_log` —— mini-clickhouse 是**单线程**的,一条查询至多一个线程,所以"每线程一行"和"每查询一行"没有区别,单独建表没有信息量。
- `part_log` —— 没有后台合并调度,所以永远没有 part 生命周期事件。
- `crash_log` / `metric_log` —— 同理,没有对应的后台写手,两张都恒空。注意 `metric_log` 在 mini 里是**窄表**、而非真实 CH 的宽表(这是有意收窄,见本节末尾);`crash_log` 的 schema 与真实 CH 同构。
- `errors` —— **钩子已经写好但没接上**:`minich/system/logs_tables.py` 提供了 `record_error(exc)` 与 `error_rows()`,但解释器在 `except` 分支里**还没有调用** `record_error`。所以这张表现在恒空,只差一行调用。
- `contributors`、`current_roles` / `grants` / `quotas` / `roles` / `row_policies` / `settings_profiles` / `users` —— 后一组读的是 `ctx.access`,它们**不是用不了**,而是"没建过对应对象就为空":`CREATE USER` / `GRANT` / `CREATE QUOTA` 之后这些表立刻有数据(见第 26 章 §26.18);`current_roles` 还额外需要"当前会话激活了哪些角色"的上下文。`contributors` 是静态名单,这里没有。

**有一处必须点名的"不忠实"**:`system.numbers` 在真实 ClickHouse 里是**真正无限**的,忘了写 `LIMIT` 就永远不返回;mini-clickhouse 的解释器没有把查询的 `LIMIT` 传进 `get_system_table_chunks`,所以它给 `system.numbers` 加了一个默认上界(`DEFAULT_NUMBERS_LIMIT = 10000`)。这是整个包里唯一一处"为了不挂住而偏离真实行为"的地方,mini-clickhouse 自己把它标成了必须写进报告的一条。

还有几个具体的实现细节值得记:

- **`system.metrics` 的 `MemoryTracking` 用的是进程 RSS**,即 `_process_memory()` 读到的常驻内存,而不是分配器记账——真实 ClickHouse 的 `MemoryTracking` 是分配字节数。这是"近似但诚实"的简化,取不到时会报 0。
- **`system.events` 的行是"已有计数 + 尚未触发的已知事件(值 0)"的并集**,所以 `WHERE event = 'X'` 不会因为"刚重启还没发生"而查不到行;每条都带一句中文说明。
- **`system.query_log` 在这个实现里是内存里的**:数据源是 `Context.query_log`(一个 Python list),`Context.clone()` 会**共享**这个 list,所以读到的是"服务级"的视图。它是**进程内**的——进程一结束就没了,也没有真实 CH 那种 MergeTree 落盘、`flush_interval_milliseconds`、`SYSTEM FLUSH LOGS` 的机制。`_finish()` 里给列表加了一个 `10000` 行的上限(真实 CH 是更复杂的保留策略)。**所以"查不到刚跑的查询"这件事在 mini-clickhouse 里不会发生**——它恰好相反,一写完就在列表里了。真实 CH 的 7.5 秒延迟是它自己的特性,不要拿 mini 的行为去理解它。
- **`system.text_log` 在 mini-clickhouse 里默认就有**,因为日志本来就在内存里(`minich.util.logger.all_loggers()`),`level` 用 `String` 表示(真实 CH 是 `Enum8`)。
- **`system.users` 等表的 schema 被有意收窄**:例如真实 `auth_type` 是 `Array(Enum8)`(一个用户可以有多种认证方式)、`host_ip` 是 `Array(String)`,mini 里分别是 `String` 和单个 `String`。

**个别列是有意收窄的:接口同构,不等于每一列都逐字照抄。** 这份 43 张表的**清单**与真实 ClickHouse 对齐,但**个别表的列**在 mini-clickhouse 里被简化或收窄,下面三处必须点名:

- **`system.metric_log`**。真实 ClickHouse 是**宽表**(WIDE):每个指标占一列,一行是一个时刻。而 mini-clickhouse 里它是**窄表**:`(event_date, event_time, event_time_microseconds, metric, value)`,即"每行一个指标名 + 值"。所以 **§27.12 讲"真实 CH 的 `metric_log` 是宽表"是对的,但那条不能拿来理解 mini 的这张表**——两者只是**表名和"记录指标随时间变化"这个语义同构,物理形态不同**。
- **`system.columns`**。真实 ClickHouse 有 `data_compressed_bytes` / `data_uncompressed_bytes` 两个容量列(§27.3 正是用它们回答"哪个列最占地方"),但 **mini-clickhouse 的 `columns` 没有这两列**;它只暴露 `database` / `table` / `name` / `type` / `position` / `default_kind` / `default_expression` / `comment` / `codec_expression` / `ttl_expression`。所以 §27.3 那条"按未压缩字节排序列大小"的查询,在 mini 上跑不出来。
- **`system.query_log.ProfileEvents`**。真实 ClickHouse 是 `Map(String, UInt64)`(值是计数),mini-clickhouse 是 `Map(String, String)`(值是字符串)。语义一致(都是"本次查询的 counter 增量"),**但类型不同**——直接按数值对 `ProfileEvents` 做比较 / 聚合的 SQL,在 mini 上要先做类型转换。

这三处不是"做错了",而是**教学实现有意收窄**:它们要么依赖真实引擎里 mini 不具备的机制(后台线程落盘),要么不在本书要演示的路径上。一句话概括——**mini-clickhouse 做到的是"系统表的接口同构",不是"每一列都逐字照抄"**;而真实 ClickHouse 的系统日志表是 MergeTree、有 buffer 与 flush 周期,这一点要以上游文档为准。

## 27.22 小结

- 三层观测:**metrics(gauge)**、**events(counter)**、**logs**。gauge 可以直接比阈值,counter 必须取增量再算速率——**对 counter 做阈值告警必然误报**。
- `system.parts` 是 "too many parts" 与"磁盘为什么涨"的第一站;`active = 0` 的旧 part 会造成"磁盘比数据量大"的假象。
- `system.query_log` 是慢查询主表。`type` 里 `QueryStart` 只是"开始";`ProfileEvents` 列是本次查询的 counter 增量。它由 MergeTree 支撑、按 `flush_interval_milliseconds`(默认 7.5 秒)异步落盘,所以**刚跑完的查询可能查不到**,要 `SYSTEM FLUSH LOGS`。
- `system.query_thread_log` 把查询拆到线程,用来定位瓶颈线程(默认关);`system.processes` 是正在跑的查询,配 `KILL QUERY` 用。
- `system.merges` 看合并进度,`system.mutations` 看 mutation——**别忘了 `latest_fail_reason`**。
- `system.replicas` 的 `absolute_delay` / `is_readonly` 与 `system.replication_queue` 的 `num_tries` / `last_exception` 是副本健康的两个入口。
- `system.errors` 按错误码聚合,比翻日志好用;它的 `value` 是累积值,同样要取增量。
- `system.text_log` 默认不开启,靠服务端 `<text_log>` 配置段打开(不是会话设置)。
- ProfileEvents 里最该盯:`SelectedMarks` / `SelectedParts`(索引裁剪)、`ReadCompressedBytes` vs `ReadUncompressedBytes`(压缩率)、`MemoryAllocatorPurge`、`QueryCacheHits/Misses`、`ZooKeeperRequests`。
- 对外:`system.metrics` 一族可通过内置 Prometheus 端点(默认 `/metrics`,端口 9363)导出,告警规则必须对 counter 用 `rate()`(§27.19);OTel 侧以官方文档为准,本章标了未确认。
- 真实 ClickHouse 还有一批日志类系统表是 mini 未实现的(§27.20):`backup_log`(备份的持久记录)、`query_views_log`、`session_log`、`asynchronous_insert_log`、`error_log`、`processors_profile_log`、`blob_storage_log`——它们由对应的 `<xxx_log>` 配置段开启,开了就得配保留策略。
- mini-clickhouse 注册了 **43 张**系统表,**接口**与真实 ClickHouse 同构,但**个别列有意收窄**(`metric_log` 是窄表、`columns` 缺两个容量列、`query_log.ProfileEvents` 是 `String`,逐条见 §27.21)。`query_thread_log` / `part_log` / `errors` 等**恒空但 schema 正确**,`system.query_log` 是**内存里的**、进程结束即失;`system.numbers` 有一个**非真实的默认上界**,这一条必须记住。
