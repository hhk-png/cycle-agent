# 第 25 章 并发控制、资源管理与配额

一条 SQL 本身没错,却报 `MEMORY_LIMIT_EXCEEDED`;机器看起来还有一半内存,`top` 里的 `clickhouse-server` 也没到上限;写入突然变慢,日志里出现 `Too many parts`;某个租户跑一个 `GROUP BY` 把整台机器拖垮。这四件事看起来毫不相干,其实都落在同一层:ClickHouse 对**资源**的管理。这一章把这一层拆成四块——线程、内存、限制、配额——并说清每类问题该动哪个旋钮、为什么"调大它"经常是错误答案。

## 25.1 并行是算子级的,不是"一查询一线程"

先纠正一个最常见的误解。很多人以为 `max_threads` 是"同时能跑多少条查询",于是把它当成并发数去调。它不是。`max_threads` 管的是**一条查询内部**最多用多少个线程来并行执行流水线的同一阶段。

看一个例子:表有 1000 个 granule(数据块),一条 `SELECT ... WHERE ... GROUP BY ...`。执行器不会开一个线程跑完整条查询,而是把流水线拆成若干算子——读、表达式求值、过滤、部分聚合——同一个算子在**多个 granule** 上被复制成多份并行跑。`ResizeProcessor` 把上游一路 chunk 轮流投给下游的 N 路(在 mini-clickhouse 里就有一个同名的小实现,见 `minich/execution/processors.py`),N 就来自 `max_threads`。所以是"单查询内部,算子被复制成 N 份",而不是"一查询一线程"。

这个设计直接带来了**线性扩展**:每个线程处理的是互不相交的 granule 子集,列存 + 向量化的处理器之间没有共享可变状态,加一个线程就多一份算力。真实 ClickHouse 的流水线是**一整张图**(数据块在图上流动),空闲算子被挂起、有新数据时被唤醒,所以并行度能跟着数据量和核数自动伸缩。`max_threads` 的默认值是 `0`,意思是"自动",实际生效值约等于**物理核数**。

几个由此推出的结论:

- 一条查询实际会创建的执行线程数可能**多于** `max_threads`,除了 N 路算子还有源、sink、以及后台 IO 线程。
- 内存占用与 `max_threads` 正相关。官方文档明确写着"The smaller the `max_threads` value, the less memory is consumed"——因为每个并行分支都在各持一份块和状态。
- 小查询(尤其是带 `LIMIT` 的)反而应该把 `max_threads` 调小。一个 `LIMIT 10` 只需要读第一个 granule,却可能被 8 个线程各读一个块,浪费是实打实的。

一个反模式:把 `max_threads` 当"限流阀"。真实 ClickHouse 里同时执行多少条查询由 `max_concurrent_queries` 一族控制(见 §25.6),两件事不要混。

## 25.2 内存:三层上限,以及"为什么 top 看着没满"

ClickHouse 的内存限制是**分层**的,从近到远三层:

- `max_memory_usage` —— **单条查询**。默认 `0`,表示"由服务端配置决定";用户可写。
- `max_memory_usage_for_user` —— **一个用户在单机上所有并发查询之和**。默认 `0`,不限制。
- `max_server_memory_usage` —— **整个进程**。这是**服务端配置项**(`config.xml`),不是会话设置。默认 `0`,实际默认值按 `物理内存 × max_server_memory_usage_to_ram_ratio` 计算,而 `max_server_memory_usage_to_ram_ratio` 默认是 `0.9`。

超过任一上限,查询抛 `MEMORY_LIMIT_EXCEEDED`(错误码 241)。第一、二层是"主动限制",第三层是"保命阀":一旦内存上限被撞到,ClickHouse 不会立刻杀人,而是让**新申请内存的查询先等一会儿**(等待时长由 `memory_usage_overcommit_max_wait_microseconds` 控制,默认 `5000000`,即 5 秒);等不到内存释放,就选中一条查询终止它。选谁?按**超配比例**而不是绝对大小——超配比例 ≈ 已分配字节 ÷ 软上限的分母,全局按 `memory_overcommit_ratio_denominator`、每用户按 `memory_overcommit_ratio_denominator_for_user` 算,挑比例最大的那条杀。**注意这两个分母是查询期设置(可以写进 profile),不是服务端配置**,默认值都是 `1 GiB`(`1_GiB`);以查询期设置存在,是为了让你能挂在 `SETTINGS PROFILE` 上按租户调。如果连一条都选不出来,就直接抛 `MEMORY_LIMIT_EXCEEDED`。这套机制叫 **memory overcommit**,在当前 25.x 上**默认开启**——但要把话说准:官方**至今仍把它归为实验性机制**,"默认开"不等于"已毕业"。它宁可牺牲几条查询,也不让整个进程被 OOM killer 干掉。

注意一个反直觉的点:这些限制**只和查询自己的分配量有关,与机器空闲内存无关**。`max_memory_usage = 10GB` 的意思是"这条查询最多用 10GB",哪怕机器有 512GB 空闲。所以"机器还很空却报超限"完全正常——那是你设的上限在起作用。

**为什么 top 看着没满,查询却报超限?** 至少四种原因,要逐个排除:

1. **追踪的是分配量,不是 RSS**。`system.metrics` 里的 `MemoryTracking`("服务端总共分配的字节")统计的是内存分配器(jemalloc)记账的字节,而不是 `top` 的 RSS。两者**没有固定的方向关系** —— `MemoryTracking` 可能**高于** RSS(记账里包含"预留但尚未触碰"的部分,只进了计数器、还没落到物理页),也可能**低于** RSS(绕过记账的第三方 `malloc`、以及 `mmap` 进来的文件页,都真实占着 RSS 却不计入)。所以别拿 RSS 去对 `max_memory_usage`。三种偏差的成因、以及该看哪个数字,§25.13 单独讲透;这里只记结论:追踪值 ≠ RSS。mini-clickhouse 的 `Memory` 引擎文档也写了同一件事:你 `TRUNCATE` 一张 Memory 表,`MemoryTracking` 会降,但 RSS 未必降——因为分配器没把内存还给 OS。
2. **追踪本身是"攒着记"的,天然有偏差**。ClickHouse 不会每次 `malloc` / `free` 都去更新全局计数器——那样多线程下会疯狂争锁;它让每个线程在本地**累积**一小块"尚未记账"的增量,攒到由 **`max_untracked_memory`** 控制的阈值(默认 4 MiB)才一次性并进全局计数器。所以 `MemoryTracking` 和"真实分配"之间**天然存在一个缓冲区大小的偏差**:小分配、短命查询尤其容易被**迟记**甚至**少记**。把 `max_untracked_memory` 调小,追踪更准,代价是更频繁地并账、更快更锁。
3. **`max_memory_usage` 是"单查询"的**。如果一个进程里有十条并发查询各占 2GB,机器 32GB 远远没满,但每条查询自己的上限是 1GB,那它们一起报超限。这时该看的是 `max_memory_usage_for_user` 或总量。
4. **容器 / cgroup**。在容器里,`top` 看到的是宿主机内存,而触发限制的可能是 cgroup 的限额。

排查用这几条:先看 `system.query_log` 按 `memory_usage` 倒排找元凶;再看 `system.metrics` 的 `MemoryTracking`;想看"哪条正在涨"就查 `system.processes` 的 `memory_usage`。

```sql
-- 最近一小时最耗内存的 10 条查询
SELECT query_id, user, memory_usage, read_rows, query_duration_ms,
       substring(query, 1, 80) AS q
FROM system.query_log
WHERE event_time > now() - 3600 AND type != 'QueryStart'
ORDER BY memory_usage DESC
LIMIT 10;
```

还有一条容易被忽略:`MemoryTracking` 之外的 `MemoryAllocatorPurge` 事件记录分配器回收内存的次数。它涨得快,说明进程在为内存做挣扎。

一个必须记住的追不上账:**某些聚合函数的状态不(完整)计入内存追踪**。官方文档点名了 `min` / `max` / `any` / `anyLast` / `argMin` / `argMax` 作用在 `String` 和 `Array` 参数上的情况。也就是说,对一列长字符串做 `argMax`,内存可能涨得比追踪到的多。这类查询要留出额外余量,或者干脆先用 `length()` 之类的办法减小中间态。

## 25.3 溢出到磁盘:兜底,不是调优手段

`max_bytes_before_external_group_by` 和 `max_bytes_before_external_sort` 让 `GROUP BY` 和 `ORDER BY` 在超过阈值时把中间结果**落到磁盘**再合并。两者默认都是 `0`,即**关闭**。

它们的定位要先说清楚:**这是防止 OOM 的逃生门,不是提速手段**。开了它,查询会慢好几倍(要写临时文件、再读回来、多几次归并),还会吃临时磁盘空间,并在 `system.parts`/磁盘监控之外多出一块你平时不会注意的写入量。正确姿势是:先想办法让查询本身少占内存(更好的主键/分区裁剪、`LIMIT`、换 `uniq` 这类近似函数、分片并行),实在压不下来,才把它当安全网。

配置上的经验值(官方文档的建议):`max_bytes_before_external_group_by` 和 `max_bytes_before_external_sort` 应当设成 `max_memory_usage` 的**一半左右**。原因是这类操作在落盘的同时仍需在内存里保留一份正在构建的结构,阈值设得太接近 `max_memory_usage`,会在落盘前就先把内存上限撞破。

**触发落盘的其实不是上面那两个开关,而是"两级聚合"。** 聚合在内部有两种形态:单级(一张哈希表装下所有组)和**两级**(把键哈希成若干桶,每桶一张小表,分两趟做,以便先落一部分、再合并)。引擎在聚合**开始之前**就要决定用哪种,判据就是两个阈值:

- `group_by_two_level_threshold`(默认 100000,单位是**组数**):预估的组数超过它,就走两级。
- `group_by_two_level_threshold_bytes`(默认 50000000,即 50 MB,单位是**字节**):聚合状态的预估字节数超过它,也走两级。

一旦走了两级,聚合就会产生**中间临时数据**;把这部分临时数据写到磁盘,才是 `max_bytes_before_external_group_by` 干的活。所以"聚合为什么突然变慢"常常是**两级聚合先被触发**(组数越过了 10 万),再叠加上外部聚合落盘——前者是引擎的自动决策、后者才是你设的开关。这也解释了一个反直觉现象:把 `group_by_two_level_threshold` **调大**能让某些查询留在单级、更快,但代价是那张单级哈希表会更大,更容易撞内存上限。两级的每一个阈值都是"用内存换速度"的方向,别只盯着一个调。

(这一节只讲机制;`max_bytes_before_external_group_by` 当性能旋钮用会有什么后果,第 28 章 §28.9 讲。)

## 25.4 限制类设置:按作用对象分家族

"限制类"设置是**单条查询**粒度的护栏,和配额(§25.5)正好相对。它们按作用对象分成一族一族,每族都是"数量上限 + 溢出行为"两条设置配对——上限为 `0` 即"不限",`*_overflow_mode` 取 `throw`(默认,抛错)或 `break`(停止计算、返回已算出的部分结果,仿佛源数据提前读完了)。下面先把日常最常打交道的四族讲透,再补齐三个几乎没人主动配、却同样会把线上打挂的族——**`SELECT DISTINCT` 撞内存就落在其中之一**。

**第一族,读取。** `max_rows_to_read`(最多读多少行)、`max_bytes_to_read`(最多读多少**未压缩**字节),溢出行为由 `read_overflow_mode` 决定,取值 `throw`(默认,抛错)或 `break`(停止执行并返回已算出的部分结果,仿佛源数据用完了)。要特别注意:这些检查是**按块**做的,不是按行,所以实际读到的量可能略微超过阈值。分布式场景还有 `max_rows_to_read_leaf` / `max_bytes_to_read_leaf`,只在叶子节点生效——它解决的是"每个分片各读一部分、总量检查反而误伤"的问题。

**第二族,结果。** `max_result_rows`、`max_result_bytes`,溢出行为 `result_overflow_mode`(`throw`/`break`)。

错误做法:以为 `result_overflow_mode = 'break'` 等同于 `LIMIT`。
后果:`break` 只在**块边界**中断,返回行数是 `max_block_size` 的整数倍,还取决于用了几个线程。官方文档举的例子很典型:设 `max_threads = 3, max_block_size = 3333, max_result_rows = 3334, result_overflow_mode = 'break'`,跑 `numbers_mt(100000)`,拿到的不是 3334 行而是 **6666 行**。
正确做法:要精确控制返回行数,用 `LIMIT`;`max_result_rows` 只当作"别把客户端灌爆"的安全网。

**第三族,聚合。** `max_rows_to_group_by`(最多维护多少个聚合键),溢出行为 `group_by_overflow_mode` 取 `throw` / `break` / `any`——**只有这一族有 `any`**。三者的差别是这一章最需要记牢的:

- `throw`:抛 `TOO_MANY_ROWS`。
- `break`:停止接收新数据,用已累积的状态出结果,像是"数据提前读完了"。
- `any`:继续对**已经进入集合**的键做聚合,但**不再把新键加进来**。也就是对超出部分做近似——结果的精度取决于数据的统计特性。

`any` 不是"随便取几行",而是"在键空间上截断"。想精确限制分组数,用 `throw` 配合前面把键的基数降下来(例如对高基数列先做 `toStartOfHour()`)。

**第四族,JOIN。** `max_bytes_in_join`(哈希表最多多少字节)、`max_rows_in_join`(哈希表最多多少行),溢出行为 `join_overflow_mode`,取值只有 `throw` 和 `break`。这里要更正一个流传很广的说法:`join_overflow_mode` **没有 `any`**——官方文档给它的取值就是 `THROW` / `BREAK` 两个。`any` 是 `group_by_overflow_mode` 独有的。如果一个查询里有多个 JOIN,这个限制会**对每个中间结果**分别检查。

JOIN 这一族在实践中比看起来重要:最常见的生产事故就是把大表放错边导致 `hash` 哈希表撑爆内存(第 15 章详述)。`max_bytes_in_join` 就是给这个场景装的天花板。

**第五族,`IN` 右集合。** `max_rows_in_set` / `max_bytes_in_set` 限制 `WHERE x IN (子查询)` 那个右集合的行数与(未压缩)字节数,溢出行为是 `set_overflow_mode`,取值 `throw` / `break`(默认 `throw`)。第 11 章 §11.6(`set(N)` 跳数索引那节)点到过这两个名字,第 28 章的反模式清单里也出现过 `max_rows_in_set`,但两处都没提它的溢出模式。`break` 在这里的含义是"停止往集合里加成员":查询不再失败,但成员判定会把**没能装进去**的值一律当成"不在集合里",于是结果**可能静默漏掉一些行**。三个成员默认都不限制,所以它平时不拦人,得你显式配才起作用。

**第六族,DISTINCT。** `max_rows_in_distinct` / `max_bytes_in_distinct` 限制 `SELECT DISTINCT` 去重时维护的那份哈希表(行数 / 未压缩字节数),**默认都是 `0`(不限)**;溢出行为是 `distinct_overflow_mode`(`throw` / `break`,默认 `throw`)。这一族最该认真对待,因为 **`SELECT DISTINCT` 打在高基数列上是 ClickHouse 最常见的 OOM 来源之一**,而它默认根本没有任何上限。要理解它的脾气,得先明白一点:DISTINCT 和聚合是**两个不同的算子**,下面三点都由这一条推出:

- **它留下来的不是"每个键的聚合状态",而是"每个不同的键元组本身"。** DISTINCT 走的是一套去重哈希集合(`SetVariants`),和 `IN` 段的集合是同一台机器,只是键换成了你 `DISTINCT` 的那几列。于是内存规模 ≈ **不同键的个数 × 键元组的宽度**;当那一列几乎唯一时,不同键数就约等于总行数——它是 **O(行数)** 的,而且没有任何"把状态压小"的办法(不像聚合能用 `sum` 只留一个数),必须把每个不同的元组原样留着才能回答查询。键里含长字符串(URL、JSON、日志原文)时,代价由**最宽的那一列**主导,`max_bytes_in_distinct` 会先于 `max_rows_in_distinct` 被撞破;键是窄整数时则相反。
- **它和聚合是两套旋钮,互不覆盖。** `max_rows_to_group_by` / `group_by_overflow_mode` **不管** DISTINCT;`max_rows_in_distinct` / `max_bytes_in_distinct` / `distinct_overflow_mode` 才是它的。所以"给租户把 `max_rows_to_group_by` 钉住了"并不等于挡住了 `SELECT DISTINCT`——这正是 §25.11 要补的那类漏洞。内存形状也不同:GROUP BY 的每键状态取决于聚合函数(`groupArray` 会把该组的每个值都攒着、`uniqExact` 会留一整个集合),同样键数下往往比 DISTINCT 重;DISTINCT 每键只留一份元组。
- **报的错不一样,这决定了你怎么定位。** DISTINCT 撞上限抛的是 **`SET_SIZE_LIMIT_EXCEEDED`**——因为它在实现上就是一个带大小限制的集合,和 `IN` 集合是同一个错误码;而 `max_rows_to_group_by` 撞上限抛的是 `TOO_MANY_ROWS`(上面第三族)。看到一条 `SELECT DISTINCT` 报 `SET_SIZE_LIMIT_EXCEEDED`,你就知道该动的是 DISTINCT 这一族,而不是聚合那一族。

**为什么 `distinct_overflow_mode = 'break'` 很少帮上忙。** `break` 的通用含义是"停止执行、返回已算出的部分结果,仿佛源数据提前读完了"。放到 `SELECT DISTINCT` 上,它返回的就是**每键元组去重到一半、静静缺了一批值的集合**——而 DISTINCT 的全部意义恰恰是"给我一份完整的去重清单",一份残缺的清单是**错的答案**,客户端从返回结果里根本看不出它被截断过。这跟 `max_result_rows` 的 `break` 还不一样:后者截断的是一个"前缀",下游往往能容忍;DISTINCT 截断掉的是集合里**哪一部分**,则取决于读取顺序与线程数,不可复现。所以别把它当"近似 DISTINCT"用:要真正用有限内存回答"有多少个不同的值",正确的路是换**聚合**——`uniq` / `uniqCombined`(近似、有误差上界)或 `uniqExact`——而不是给 `DISTINCT` 配 `break`。**`break` 给的不是近似值,是一个你不知道缺了什么的集合。**(和其余各族一样,这项检查也是按块进行的,撞破阈值时可能已略超。)

**第七族,外传表(TRANSFER)。** `max_rows_to_transfer` / `max_bytes_to_transfer` 限制 **`GLOBAL IN` / `GLOBAL JOIN`** 里那张要发给远端服务器(或存进临时表)的**外部表**的行数与未压缩字节数,溢出行为是 `transfer_overflow_mode`(`throw` / `break`,默认 `throw`)。它在单机部署里碰不到,一上集群就会撞:一个 `GLOBAL IN (SELECT ...)` 先在本地把右表物化成一张外部表,再把它**发给每一个分片**;这两个上限卡的正是这张要外传的表。还要记住一条分布式专属的性质:**限制在每个服务器上各自检查一遍**(官方原文 "restrictions are applied on each server separately"),所以同一组数值在分片上的实际效果,可能和你按单机估的不一样。

除了这几族,还有几族与几条独立的护栏值得记住:

- `max_execution_time`(秒)配 `timeout_overflow_mode`(`throw`/`break`)。它有个容易被忽略的行为:它按**当前执行速度做插值估计**,只有当"按当前速度预估的总耗时会超时"才中断;在开头一段(由 `timeout_before_checking_execution_speed` 控制,默认 10 秒)还不做估计。把那个值设成 `0` 才会退化成"以墙上时钟为准"。
- `max_estimated_execution_time`(秒,默认 0 = 不限):它和 `max_execution_time` 是一对——后者过了宽限期才按"当前速度预估的总耗时"判断,前者**直接以这个预估值**为准。用途是**更早地掐断注定要跑很久的查询**:一条查询刚跑几秒、但按当前速度预估要跑一小时,它能让你在头几秒就失败,而不是先白烧一小时 CPU 再被 `max_execution_time` 拦下。
- `max_rows_to_sort` / `max_bytes_to_sort` 配 `sort_overflow_mode`——排序前的行数/字节护栏。
- `max_columns_to_read`——单查询最多读多少列,防"手一抖 `SELECT *`"。
- `max_query_size`——**解析器**能接受的查询文本字节数,默认 `262144`(256 KiB)。超过报 `QUERY_IS_TOO_LARGE`。注意 `INSERT ... VALUES` 里的数据由流式解析器处理,**不受**这个限制。
- `max_ast_depth` / `max_ast_elements`(默认 1000 / 50000)、`max_subquery_depth`(默认 100)是防"用超深嵌套的查询把服务器打挂"的护栏。
- `max_temporary_columns` / `max_temporary_non_const_columns`——限制查询中间态同时存在的临时列数。
- **磁盘版的"内存上限"**:`max_temporary_data_on_disk_size`(**服务端级**,单机所有查询的临时数据总量上限,写在 `config.xml` 里)。按查询、按用户收窄的是它的两个变体:分别是 `max_temporary_data_on_disk_size_for_query`(单查询)和 `max_temporary_data_on_disk_size_for_user`(单用户)。这两个是**会话 / 查询级**设置,能挂进 `SETTINGS PROFILE` 按租户钉死。所以三者是"同一族、不同作用域":base 那个卡整机,两个带后缀的分别卡单查询和单用户,并**没有"表级"这一档**。外部聚合/外部排序、`GROUP BY`、大 JOIN 落下来的临时文件都算在里面。它们和内存上限是同一套逻辑,只是作用对象换成了 `tmp_path` 所在磁盘 —— 内存管住了、临时盘被写爆,同样会把节点拖垮。(相关设置的具体默认值以目标版本 `system.settings` / `system.merge_tree_settings` 为准。)

## 25.5 配额:跨查询累积,而不是单查询

限制设置是"这一条查询最多多少",配额(QUOTA)是"**在一个时间窗口内,一个主体一共能用多少**"。这是两者最本质的区别,也是最常被搞混的地方。

配额用 SQL 创建:

```sql
CREATE QUOTA [IF NOT EXISTS | OR REPLACE] quota_name [ON CLUSTER cluster_name]
    [KEYED BY {user_name | ip_address | client_key | client_key,user_name | client_key,ip_address} | NOT KEYED]
    [FOR [RANDOMIZED] INTERVAL number {second | minute | hour | day | week | month | quarter | year}
        {MAX { {queries | query_selects | query_inserts | errors | result_rows | result_bytes
                | read_rows | read_bytes | execution_time} = number } [,...] |
         NO LIMITS | TRACKING ONLY} [,...]]
    [TO {role [,...] | ALL | ALL EXCEPT role [,...]}]
```

逐块读这个语法:

- `KEYED BY` 决定"配额按什么维度共享"。`NOT KEYED` 表示所有用户共享同一份额度;`KEYED BY user_name` 表示按用户名分配、各自独立;`KEYED BY ip_address` 按来源 IP;`client_key` 由客户端显式提供(例如 `clickhouse-client --quota_key`)。这些键名和 `system.quotas.keys` 列一一对应。
- `FOR [RANDOMIZED] INTERVAL n unit` 是一个**窗口**;可以在一个配额里写多个 `FOR INTERVAL` 段,长度不同。窗口从主体第一次消耗额度开始计时,窗口滚动后重新计数。
- `MAX { ... }` 里能限的项:`queries`(查询数)、`query_selects` / `query_inserts`(分开数)、`errors`(出错次数)、`result_rows` / `result_bytes`、`read_rows` / `read_bytes`、`execution_time`(秒)。`execution_time` 用小数表示秒,例如 `0.5` 表示半秒。
- `TO` 指定把配额施加给哪些角色或用户;`ALL` / `ALL EXCEPT` 是通配写法。

一个例子:让某个角色每 30 分钟最多执行 100 次查询、累计执行时间不超过 5 秒,并在 5 个季度内最多 10000 次查询、10 次错误:

```sql
CREATE QUOTA slow_tenant
    FOR INTERVAL 30 minute MAX queries = 100, execution_time = 5
    FOR INTERVAL 5 quarter MAX queries = 10000, errors = 10
    TO tenant_role;
```

查看配额状态靠两张系统表:

- `system.quotas` 是**配额定义**:`name` / `id` / `storage`(`users.xml` 还是 SQL 建的盘上存储)/ `keys`(即 `KEYED BY` 的维度)/ `durations`(各窗口秒数)/ `apply_to_all` / `apply_to_list` / `apply_to_except`。想知道"这个配额到底把额度分给了谁",看这里。
- `system.quota_usage` 是**当前用量**。关键列成对出现:累计值 `queries` / `errors` / `result_rows` / `read_rows` / `read_bytes` / `execution_time`,以及各自的上限 `max_queries` / `max_errors` / …;还有 `quota_key`(按维度展开后的键值)、`start_time` / `end_time` / `duration`(当前窗口)。它是**按当前用户**看的——"我还剩多少额度"是一个查这张表就能回答的问题。

配额超限会抛 `QUOTA_EXCEEDED` 异常(错误码请以你所处版本的 `ErrorCodes` 为准)。注意它和限制设置抛的错**不是同一个**,这也提示你是在撞哪一层。

## 25.6 并发限制与 KILL QUERY

并发控制分两层,别混:

- **服务端配置**:`max_concurrent_queries`(进程级总并发上限;**代码默认是 `0`,即不限**;官方发行版的 `config.xml` 模板把它预设为 `1000`,所以在按发行版默认安装的机器上实际生效的往往是 1000——这两件事别混,口径与第 33 章 §A.16 一致);另有 `max_concurrent_insert_queries` / `max_concurrent_select_queries` 按类型细分;`max_connections` 管连接数,和"查询数"不是一回事(一条连接可以在不同时刻跑不同查询,HTTP 接口则可能一请求一查询)。
- **会话设置**:`max_concurrent_queries_for_user`(默认 `0`)、`max_concurrent_queries_for_all_users`(默认 `0`)。前者限单个用户,后者是个有意思的设计:它规定"当全局并发数达到该值时,除你自己以外所有用户都无法再发起查询"——管理员可以给自己留一条 `max_concurrent_queries_for_all_users` 之外的通道,在服务器被打满时进来救火。

超并发上限抛 `TOO_MANY_SIMULTANEOUS_QUERIES`(错误码 202)。但"超上限"并不总是**立刻**报这个错:**排队还是拒绝**取决于队列设置。`max_concurrent_queries` 被撞到时,新查询先**进队列等**有并发槽空出来,而不是马上失败。档位由两头控制:服务端配置 **`max_waiting_queries`**(队列最多能排多少条,只能写在服务端配置、放 profile 里无效),以及查询期设置 **`queue_max_wait_ms`**(一条查询最多在队列里等多少毫秒)。**这两个的默认值未在 25.3 上确认**——常见说法是 `max_waiting_queries` 默认 `0`(不限)、`queue_max_wait_ms` 默认 `0`(不限等待),但也有资料把 `queue_max_wait_ms` 写成 5000,请以你版本的 `system.server_settings` / `system.settings` 为准。队列排满或等待超时,才抛 `TOO_MANY_SIMULTANEOUS_QUERIES`。(注意:网上常见的 `max_queued_queries` / `max_queued_queries_per_user` 这两个名字在 25.3 并不存在,别照抄。)所以现象上有两种:队列大、等待长,表现为查询**堆积、延迟上升**;队列小、等待短,表现为**直接失败**。选哪种,取决于你的服务是"宁可慢"还是"宁可快失败"。

关于线程数与 CPU:一条查询的开销大致是 `max_threads × 并发查询数` 个活跃线程。把 `max_concurrent_queries` 设成 100、`max_threads` 用默认(核数),在 16 核机器上就是理论上千线程一起抢 CPU——线程池(`max_thread_pool_size`,默认 10000)会兜住,但上下文切换和缓存抖动会把吞吐拉低。经验做法是让 `max_threads × 典型并发` 大致落在核数的量级,再根据实测调整。

**但不要把上一条当成"CPU 已经管住了"。** 限制并发**查询数**挡不住 CPU 超订:每条查询内部的 `max_threads` 之和仍可能远超核数,而真正的总 CPU 闸门是**现代的软性 CPU 槽位机制**,由三个**服务端设置**控制(写在服务端配置里,不在会话层):

- `concurrent_threads_soft_limit_num`:全局同时能持有的 CPU 槽位数上限。默认 `0` = **不限**。它是**软**上限:槽位用尽时新查询不会失败,而是"少拿线程、至少保留一条",等有槽位释放再往上加。
- `concurrent_threads_soft_limit_ratio_to_cores`:上一条的"按核数倍数"写法,二者取其一即可。这里有个**默认值不一致**的坑:源码里的默认是 `0`,而随包发行的 `config.xml` 可能把它覆盖成别的值(于是 `system.server_settings` 里 `value` 与 `default` 会不一致)——**具体数值以你版本 `config.xml`(或较新版本才有的 `system.server_settings`)为准,本书未确认**。
- `concurrent_threads_scheduler`:槽位在并发查询之间怎么分,取值如 `fair_round_robin`、`max_min_fair` 等;可**在线改**,不用重启。

它和 `max_threads` 的分工是:**`max_threads` 是单条查询自己的上限,`concurrent_threads_soft_limit_*` 是所有查询加起来的 CPU 闸门,`max_thread_pool_size` 兜的是"线程数别爆"**。所以推断方式要改:把 `max_concurrent_queries` 调小并不等于 CPU 不会被超订——真正兜住总 CPU 的是这组槽位设置。

`KILL QUERY` 用来终止正在跑的查询:

```sql
KILL QUERY WHERE query_id = '...' SYNC;
KILL QUERY WHERE user = 'someone' ASYNC;
```

它有三个模式:`ASYNC`(默认,发出信号后不等待)、`SYNC`(等待并逐条回报,返回的 `kill_status` 常见取值是 `finished` 与 `waiting`,前者表示确实停掉了、后者表示信号已发但还没走到检查点)、`TEST`(只列出会杀哪些查询,不真杀)。只读用户只能杀自己的查询。

**为什么 `KILL QUERY` 不一定立刻生效?** 因为它不是"拔电源",而是设一个**取消标志**,执行线程要在自己的检查点上去看这个标志。遭遇大块的计算(比如 `ORDER BY` 的归并尾段、聚合的 `finalize`、一次长时间磁盘 IO)时,线程要跑过这一段才会看到标志;检查的节奏和 `interactive_delay`(默认 100000 微秒 = 0.1 秒)这类进度上报周期相关。所以"杀了没反应"通常是正常现象,等几秒再看 `system.processes` 里那条还在不在。极端情况下,若某段代码路径根本不检查标志,那条查询只能等它自己结束。

## 25.7 后台任务的资源:merge / mutation 池

查询不是唯一的吃资源者。后台 merge、mutation、副本拉取(fetch)各有自己的线程池,其中最要命的是 merge/mutation 池:

- `background_pool_size`(**服务端配置**,默认 16):后台 merge 与 mutation 的线程数。它能在运行时**调大**、但不能调小(要重启)。调大它能让合并追上写入,代价是和前台查询抢 CPU 和磁盘 IO。
- `number_of_free_entries_in_pool_to_lower_max_size_of_merge`(默认 8,建表/MergeTree 设置):池里空闲名额少于它时,降低单次合并允许的最大体积,**给小的(紧急的)合并留出资源**。
- `max_bytes_to_merge_at_max_space_in_pool`(默认 `161061273600`,即 150 GiB):后台自动合并能产生的**最大 part 体积**。它基本决定了"后台合并最终能把数据合到多大"——这也是最关键的合并调参之一。注意 `OPTIMIZE FINAL` 不受它约束,会一直合到分区里只剩一个 part(前提是磁盘空间够)。

后台**不止 merge 一个池**,每个负责一类工作,都是服务端配置:

- `background_schedule_pool_size`(默认 512):跑"周期性小任务"的池——后台调度器用它定期检查每个表要不要合并、TTL 到期没有、复制队列要不要动。调大它能让大规模集群里的这些"检查"不互相排队(表很多时容易成为瓶颈)。
- `background_fetches_pool_size`(默认 16):副本**拉取 part** 的池。副本落后要补数据时,靠它从别的副本下载 part;带宽够却追不上,常常是这个池太小。
- `background_distributed_schedule_pool_size`:**推进 `Distributed` 表异步发送**的池——`Distributed` 表靠它定期检查"本地队列里有没有待发数据"并把它们发往各分片(第 23 章 §23.5)。分片多、Distributed 表多时,这个池太小会让发送排队、`system.distribution_queue` 里的积压下降变慢。**默认值以你版本 `config.xml` 为准(未确认)。**
- `background_move_pool_size`(默认 8):在卷/盘之间**搬 part** 的池(冷热分层、`move_factor` 触发的搬移),见第 29 章 §29.13。
- `max_thread_pool_size`(默认 10000):**全局线程池**上限。前台查询的执行线程、以及各专用池,都从这里借线程;撞到上限时新任务得等别人还线程。它不解决"慢",只是兜住"别把线程数打爆"(§25.6)。

这几个的**确切默认值以目标版本 `config.xml` / `system.server_settings` 为准**(部分数值随版本调整过,标**未确认**)。这里没法像会话设置那样甩一句"去查 `system.settings_changes`"就完事:那类**服务端配置**不在 `system.settings_changes` 的覆盖范围内(§24.13),只能查文档与配置模板。排查原则一致:看 `system.metrics` 里对应的 `*PoolTask` / 池占用,长期贴顶就是该池不够用。

`system.metrics` 里的 `BackgroundMergesAndMutationsPoolTask`(当前在跑的合并/mutation 任务数)长期贴着 `background_pool_size` 顶格,说明合并队列积压。这是"该看看是不是写入太碎"的早期信号。

## 25.8 写入限流:`parts_to_delay_insert` 与 `parts_to_throw_insert`

这两个是 MergeTree 表设置(可在建表 `SETTINGS` 子句或服务端 `<merge_tree>` 段里配),现值是 `parts_to_delay_insert = 1000`、`parts_to_throw_insert = 3000`(旧值 150 / 300 本身没有争议)。**有争议的只是它们各自在哪个版本改成现值**:`parts_to_throw_insert` 的 300 → 3000 有**官方版本标注**——**23.6**;而 `parts_to_delay_insert` 的 150 → 1000,**官方设置页没有给版本标注**,不同来源说法冲突(有说与 throw 同为 23.6 的,也有把这笔改动系于 25.1 的),**确切版本未确认**。所以别把它们混成"某一版之前都是 150/300"一句话——**它们是否同一版本改的,没有可靠定论**;你那一版的实际值,直接读 `system.merge_tree_settings` 里这两列即可,不必猜。

它们是**保护机制,不是调优参数**——这一条必须讲清楚,否则一定会有人把 `parts_to_throw_insert` 改到十万来"解决" `Too many parts`。

机制是这样的:单个分区里的活跃 part 数超过 `parts_to_delay_insert`,`INSERT` 会被**人为拖慢**(逐步加 `sleep`,由 `max_delay_to_insert` 控制上限,默认 1 秒);超过 `parts_to_throw_insert`,`INSERT` 直接报 `TOO_MANY_PARTS` 并中止。

错误做法:把 `parts_to_throw_insert` 调大。
后果:表面上不报错了,但 part 数继续失控。part 越多,查询要打开的文件越多、启动时要加载的 part 元信息越多、`SELECT` 越慢;最终不是不报错,而是**在更晚、更难收拾的时候以另一种方式崩**——磁盘被撑爆、启动要几分钟、查询全面变慢。
正确做法:把它当"症状报警",去治根——把碎写合成大批次(一次 INSERT 几十万行而不是每行一次)、检查是不是分区键太细导致每个分区都在堆积、看后台合并是不是被 `max_bytes_to_merge_at_max_space_in_pool` 或磁盘空间卡住了(见 §25.7)。

判别方法很直接:查 `system.parts` 数每个分区的活跃 part 数(第 27 章 §27.2 有完整写法),同时看 `system.metrics` 的 `DelayedInserts`——它数的是"因为 part 太多而被限流"的 INSERT 次数,不为零就说明已经在拖了。

## 25.9 优先级与调度:从 nice 值到工作负载

`os_thread_priority`(默认 `0`,可写范围 `[-20, 19]`)给执行查询的线程设置 nice 值,让操作系统调度器决定谁多拿 CPU。**值越小优先级越高**:高值(nice 大)适合长跑的非交互查询,因为它在 CPU 紧张时更容易让出资源给短查询;交互式查询该用较小的值。用它需要 `CAP_SYS_NICE` 能力(官方的 `clickhouse-server` 包在安装时会配好)。

但 nice 值只是"请求操作系统帮忙",粒度粗、对 IO 与内存无效,同一台机器上所有查询还共享同一个 nice 值空间。现代 ClickHouse 的方向是**工作负载调度(workload scheduling)**:把资源和优先级做成可按"工作负载"命名的对象,再绑定到用户/查询上,而不是靠调 nice。这就是本节的主角,`os_thread_priority` 退居补充。

**先把三个名字分清楚,后面就不会混**(本节从头到尾只有这三样东西):

- `os_thread_priority`(上面刚讲的会话设置):给执行查询的线程设 **nice 值**,交给**操作系统**的调度器;粒度粗、只对 CPU 有效。
- **`workload`(会话设置,默认 `'default'`)**:把查询归入一个命名的**工作负载**,由 ClickHouse **自己的**调度器按各 workload 的 `weight` 在 CPU / 内存 / IO 多个维度上分配。这是现代方向。
- `priority`(会话设置):一个**历史遗留名字**,它的角色已被工作负载调度取代 —— 今天要表达调度优先级,用 `CREATE WORKLOAD ... SETTINGS`(weight 等),而不是它。**别把它和 `os_thread_priority` 混为一谈**:后者是给操作系统调度器的 nice 值,前者是查询调度器里的历史字段,两者作用在不同层。

调度由两个新对象组成,都用 SQL 创建:

- **`CREATE RESOURCE`**:定义一个**资源**,并绑定到具体的调度维度。25.3 文档演示的是磁盘/IO 资源(如 `READ DISK` / `WRITE DISK`);CPU(`MASTER THREAD` / `WORKER THREAD`)、内存、query 槽等资源类型是更晚才补齐的。
- **`CREATE WORKLOAD`**:定义一个**工作负载**,可以嵌套(子 workload 用 **`IN <父>`** 声明),用 `SETTINGS` 设它的**权重 `weight`** 等;workload 的层级会自动为每个 resource 生成对应的调度节点。

创建之后,会话或查询用 **`workload` 设置**把自己归入某个工作负载(`workload` 的默认值就是 `'default'`):

```sql
-- 资源形态以 25.3 文档确认的磁盘资源为例
CREATE RESOURCE network_read  (READ DISK s3);
CREATE RESOURCE network_write (WRITE DISK s3);
CREATE WORKLOAD all SETTINGS weight = 1;
CREATE WORKLOAD etl IN all SETTINGS weight = 3;
SET workload = 'etl';                    -- 会话级:后续查询都走 etl
SELECT ... SETTINGS workload = 'etl';    -- 查询级
```

(两个易错点:子 workload 的关键字是 **`IN`** 而不是 `PARENT`;`weight` 是文档确认的字段,`priority` 等更多字段的语义与默认值请以目标版本文档为准。)

可观测性靠几张薄表:`system.workloads`(列 `name` / `parent` / `create_query`,即每个 workload 的父级与建它的原始语句——权重、优先级写在 `create_query` 文本里)、`system.resources`(列 `name` / `read_disks` / `write_disks` / `create_query`)。想看调度器的实际排布,25.x 还提供 `system.scheduler`。相比 nice 值,workload 调度的好处很明确:**CPU 和内存一起管**、**按命名对象而不是按查询语句管理**(能用 `SETTINGS PROFILE` 直接给某类租户绑定一个 workload),并且**有 `system` 表可查**——`SET` 一条 nice 值你事后根本不知道谁改过,而 workload 是可见的。

**把 workload 绑到用户 / 租户上。** 前面说"能绑定到用户 / 查询上"只是一笔带过,这里把路径写清。它有三条:(1) **写进 `SETTINGS PROFILE` 或用户定义**——`CREATE SETTINGS PROFILE etl_profile SETTINGS workload = 'etl' TO etl_user`,或 `ALTER USER etl_user SETTINGS workload = 'etl'`;此后该用户/角色发出的查询默认就归入这个 workload,这是"按租户分优先级"最省事的做法,改一处覆盖一批人。(2) **按会话临时切**——登录后 `SET workload = 'etl'`,只影响当前会话。(3) **按查询单次指定**——`SELECT ... SETTINGS workload = 'etl'`,只管这一条。三者的覆盖顺序沿用 ClickHouse 的设置覆盖链(§25.10)。至于 `workload` 与 `os_thread_priority` 在同一时刻究竟谁说了算,官方没有稳定的叠加规则、本节也未逐项核对,**未确认**,以目标版本实测为准。

诚实的边界:`CREATE WORKLOAD` / `CREATE RESOURCE` 是较新的能力,语法细节、`weight`/`priority` 的语义与默认值、以及它与 `os_thread_priority` 如何叠加,我没有在 25.3 上逐项核对,**未确认**。可以确定的方向是:涉及"按租户或按查询分 CPU 与内存优先级"时,**先考虑 workload 调度**,把 `os_thread_priority` 当补充,而不是反过来。

IO 线程池方面,现代 ClickHouse 的参数是服务端配置的 `max_io_thread_pool_size`(默认 100)、`max_io_thread_pool_free_size`(默认 0)、`io_thread_pool_queue_size`(默认 10000)这一族。**未确认**:旧名 `max_io_threads` 我在 25.3 的官方服务端配置文档里没有确认到,它可能已被上面这组参数取代——如果你的版本里还有这个名字,请以 `system.server_settings`(新版本才有该表)或 `config.xml` 的校验为准,不要照搬。

## 25.10 设置的作用域、优先级与 readonly

前面反复提到"会话设置""服务端配置""profile 里钉死",但一直没把**一条设置到底从哪来、谁覆盖谁**讲清楚。这一节补齐它——理解这条链,才能回答"我明明设了为什么没生效"。

**五层作用域,从外到内覆盖。** ClickHouse 的一条设置可以在五个层次上被赋值,查找时**内层覆盖外层**:

```text
内置默认值(built-in default)
  → 服务端配置(config.xml / server settings)      ← 最外层
    → 用户档(SETTINGS PROFILE,通常经用户的 default profile)
      → 会话(session / 连接)
        → 查询(查询里的 SETTINGS 子句或在 client 里 SET)  ← 最内层
```

具体到一次查询的解析顺序:**先取内置默认**,被**服务端配置**覆盖,再被用户挂的 **profile** 覆盖,再被**会话级 `SET`** 覆盖,最后被**查询级 `SETTINGS`** 覆盖。所以"我 profile 里设了 `max_threads = 4`,为什么这条查询用了 16"——因为有人在查询里 `SETTINGS max_threads = 16`(查询级比 profile 更靠内)。反过来,"我 `SET` 了怎么不变"通常是撞上了 profile 里的 `CONST`(§26.5):`CONST` 是"钉死",内层也覆盖不了它。

看点:`SELECT name, value, changed, default FROM system.settings WHERE changed = 1` 就是"相对内置默认改过的"那一列(第 27 章 §27.14)。

**readonly,一档之差决定用户能不能改。** `readonly` 是控制"这个会话还能改什么"的总闸,取三档:

- `readonly = 0`(默认):可以读写、可以改设置。
- `readonly = 1`:只能读数据,**不能改设置**(`SET` 会被拒)。给只看报表的账号用——防止它 `SET max_threads = 1000` 把机器打爆。
- `readonly = 2`:只能读数据、**但允许改设置**。适合"只读数据、又要自己调性能参数"的分析场景。

注意 `readonly` 管的是"**能不能改**",不是"能读哪些数据"——后者由 `GRANT SELECT` 与行策略管(第 26 章)。两者正交:一个账号可以 `readonly = 0` 却只有一张表的 `SELECT`。别把 `readonly` 当成权限控制的替代。

## 25.11 多租户:把三层旋钮组合起来

把这一章的东西拼成一个租户隔离方案,顺序是这样的:

1. **钉死单查询上限**:为每类租户建一个 `SETTINGS PROFILE`,用 `CONSTRAINT` / `MIN` / `MAX` / `READONLY` 把 `max_memory_usage`、`max_threads`、`max_execution_time` 固定住,让用户**自己改也无效**。(§25.4 里的每一族都是**独立**的旋钮:`max_rows_to_group_by` 只管聚合,挡不住 `SELECT DISTINCT`;`max_bytes_in_join` 只管 JOIN,挡不住 `IN` 的大集合。所以若租户会跑高基数 `SELECT DISTINCT`、大 `IN` 子查询或 `GLOBAL IN`/`GLOBAL JOIN`,这一层 profile 要把 **DISTINCT / SET / TRANSFER 三族**一并 `CONSTRAINT` 住,否则"钉住了聚合和 JOIN"并不等于钉住了内存。
2. **限制用户总量**:给用户设 `max_memory_usage_for_user`,防止一个租户开很多并发查询把额度乘起来。
3. **限制累积用量**:给租户角色挂 `QUOTA`,限制窗口内的查询数/读行数/执行时间——这是对抗"查询本身都不大、但一直跑"的唯一手段。
4. **并发与 CPU 闸门**:服务端用 `max_concurrent_queries` 兜底"最多几条查询同时跑",再用 §25.6 那组 `concurrent_threads_soft_limit_*` 兜住"所有查询加起来的 CPU 别超订"——两者管的是不同的东西,别只配一个。

把这四步压成一段可执行的 SQL:

```sql
-- 1) 用 profile 钉死单查询上限:MAX/MIN 夹住用户的自调空间,READONLY 直接钉死
CREATE SETTINGS PROFILE tenant_profile SETTINGS
    max_memory_usage = 20000000000 MAX 30000000000,  -- 允许在 20~30 GB 之间自调
    max_threads = 8 READONLY,                         -- 钉死,用户改不动
    max_execution_time = 300,
    max_memory_usage_for_user = 40000000000;          -- 2) 整个租户(用户)的总量闸门
-- 3) 配额:窗口内累积用量
CREATE QUOTA tenant_quota
    FOR INTERVAL 1 hour MAX queries = 500, read_rows = 10000000000
    TO tenant_role;
-- 4) 需要时再把该租户归入一个低权重的 workload(§25.9)
ALTER USER tenant_user SETTINGS workload = 'reporting';
```

**为什么四层缺一不可,因为它们的粒度不同**:profile 管"单条查询最多用什么"(§25.4),`max_memory_usage_for_user` 管"这个用户所有并发查询加起来"(§25.2),配额管"一个时间窗口内累积了多少"(§25.5)。只设第一层,租户就靠"开很多条并发查询"把额度乘起来绕过;只设配额,单条大查询仍可能一口吃光内存。

**两条纪律。** 其一,压小上限能防事故,但不能换来吞吐:把 `max_threads` 钉成 8 之后,该租户的每条查询就真的最多 8 路并行,这不是"隔离的代价",而是"隔离本身"(§25.1)。其二,**配额是最容易被漏掉的一层**:它不报错、不 OOM,只是安静地让"一直跑小块查询"的租户把资源磨光 —— `system.quota_usage` 是唯一能看见它的地方(§25.5)。

第 1、2 步里 `SETTINGS PROFILE` 那些 `MIN` / `MAX` / `READONLY` / `CONSTRAINT` 修饰的确切含义在下一章(第 26 章 §26.5)。

## 25.12 与 mini-clickhouse 的对照

`minich/settings.py` 是一个"有类型、有约束、分层覆盖"的设置注册表(不是 `dict[str, str]`):它注册了 **124 条设置**,名字与默认值对着 25.3 抄,每条带类型、`min`/`max`、`is_readonly`、`tier`。

**对齐点**(mini 做完整了的)——**设置的定义与校验**:五层作用域(query / session / profile / server / 内置默认)被表达成可层叠的 `Settings(base=...)`,`ctx.settings.changed()` 能算出"相对默认改了什么";`SET max_threads = 'abc'` 会在设置的那一刻就抛 `CANNOT_PARSE_TEXT`,超出 `min`/`max` 抛 `SETTING_CONSTRAINT_VIOLATION`——写错在当下暴露,而不是跑到一半才炸。`minich/execution/context.py` 里 `Context` 还同时持有 `profile_events`(对应 `system.events`)与 `metrics`(对应 `system.metrics`)。

**简化点**(mini 留白的"设置的执行后果"——照着它理解真实 ClickHouse 会出错):

- **真正生效的**只有两处:`max_rows_to_group_by` 在 `minich/execution/processors.py` 的 `AggregatingTransform` 里检查(超上限抛 `TOO_MANY_ROWS`);`max_block_size` 被各格式的读写路径使用。
- **只抛错、不看溢出模式**:上面那处 `AggregatingTransform` **硬抛异常**,`group_by_overflow_mode` 只出现在报错文本里,`break` / `any` 的语义**没实现**——设成 `any` 不会改变行为。
- **完全没有生效的**:`max_memory_usage` / `max_memory_usage_for_user` / `max_server_memory_usage` 只在注册表里有名字;`max_bytes_in_join` 被 `minich/execution/joins.py` 明确标注为"**不做**检查"(JOIN 一律整表进内存);`max_execution_time`、`max_rows_to_read`、`max_result_rows`、`max_threads`、`max_query_size`、`max_columns_to_read` 在查询路径上都不存在真正的强制点。
- **配额对象能建、不执行**:`CREATE QUOTA` 的解释器(走 `minich/access.py`)能让 `system.quotas` 立刻有数据,但没有 `system.quota_usage` 的数据源,查询时也不检查配额——配额只被记录,不会真的拦查询(见第 26 章 §26.18)。

一句话:mini-clickhouse 把"设置的定义与校验"做得很完整,却把"设置的执行后果"大量留白——这正是它作为教学实现可以接受、作为数据库不可以接受的地方,也是你读真实 ClickHouse 行为时必须以真实文档为准的主要原因。

## 25.13 内存是怎么被追踪的

§25.2 讲了"三层上限"和"为什么 top 看着没满"的结论。那一节的三个现象——追踪值不等于 RSS、偏差来自"攒着记账"、超限先排队再杀查询——都建立在一套**追踪机制**之上。这一节把这套机制单独讲透,因为线上排查内存问题时,你要读的每一个数字(`system.metrics.MemoryTracking`、`system.processes.memory_usage`、`query_log.memory_usage`、`MEMORY_LIMIT_EXCEEDED` 的报错文本)都由它产生。

**分工:jemalloc 负责"分配",MemoryTracker 负责"记账"。** 真正向操作系统要内存的是分配器,ClickHouse 内置的是 **jemalloc**(多线程下比 glibc 的 malloc 争锁更少、碎片更少)。但 jemalloc 只管"给一块内存、回收一块内存",它不知道这块内存是"哪条查询、哪个用户"在要。这个归属信息由 **MemoryTracker** 维护:每次分配 / 释放,除了交给 jemalloc,还会往当前的跟踪器上报一次增减。所以"内存上限"拦的是**上报到跟踪器的字节数**,不是 jemalloc 实际向内核拿的字节数——这是理解后面所有偏差的起点。

**三个层级,逐层归因。** 跟踪器是**树形**的:最上面一个**服务端级**(对应 `max_server_memory_usage`),它下面按**用户**分(`max_memory_usage_for_user`),每个用户下面再按**查询**分(`max_memory_usage`)。一次分配会被同时计入它所属的这三层,所以任何一份内存都能被回答成"哪条查询 / 哪个用户 / 整个进程"。`MEMORY_LIMIT_EXCEEDED` 的报错文本会点名**是哪一层超了**(单查询 / 单用户 / 服务端),读报错时先看这一点——它直接指向该动哪个上限(§25.2),而不是笼统地"内存不够"。

**为什么 `MemoryTracking` 和 `top` 的 RSS 常常对不上。** 两者的差**两个方向都有**(追踪值可以高于、也可以低于 RSS),至少三层原因:其一,**被追踪的 vs 未追踪的分配**——只有走 ClickHouse 内存分配器的分配才被记账,那些绕过它、直接 `malloc` 的第三方库分配(某些外部解码库之类)不计入 `MemoryTracking`,却真实占着 RSS;其二,**分配器不还给内核、也未必都已落到物理页**——jemalloc 释放的内存往往留在自己的空闲池里备用,`MemoryTracking` 会立刻降,但 RSS 不降(§25.2 那个 `TRUNCATE` 的例子);反过来,记账里"预留但尚未触碰"的那部分只进计数器、不进 RSS,于是追踪值又**高于** RSS;其三,**页缓存与碎片**——读 part 走的是 `mmap`,那是**文件页**,占 RSS 里"共享页"的一部分,而它跟"查询分配了多少内存"是两个账,再叠上分配器与内核的碎片,两者能差出可观的一截。所以**别拿 `top` / RSS 去对 `max_memory_usage`**:要判"该不该报超限"看 `MemoryTracking`,要判"机器还剩多少物理内存"才看 RSS / `MemAvailable`。

**峰值内存主要来自哪几类算子。** 吃内存的大头是几类**阻塞算子**,它们的共同点是"必须把输入攒全才出结果"(§25.1、第 10 章 §10.7):**聚合**(`GROUP BY` 的哈希表,基数越高越大;**两级聚合**正是为了在键太多时先分桶,§25.3)、**排序**(`ORDER BY` 的中间结果,可能触发外部排序落盘)、**JOIN 的 build 侧**(右表整体建哈希表,大表放错边就爆,第 15 章)。实用含义是:当 `query_log.memory_usage` 很高时,先在 `EXPLAIN PLAN` 里定位这几类算子在哪,再决定是加 `LIMIT` / 换近似函数 / 打开有序聚合,还是给 `max_bytes_before_external_*` 设个落盘阈值(§25.3)让它**退化成"慢但能跑完"**。

**怎么定位"是哪一步把内存吃掉的"。** 两条线索一起用:**其一,`system.query_log`**——每条查询一行,`memory_usage` 是它的峰值,按它倒排就能找到元凶(§25.2 给过查法);想看**正在涨**的那条,查 `system.processes` 的 `memory_usage`。**其二,`system.trace_log`**——光知道"这条查询吃了很多内存"还不够,还要知道**吃在哪**。打开内存剖析(设 `memory_profiler_step`,每分配若干 MB 采一次样),剖析器会把"分配发生时正在执行的那段调用栈"落进 `trace_log`;按栈聚合 `count()`,哪段栈出现得多,内存就是从哪条代码路径漏出来的(第 27 章 §27.7 讲这张表与火焰图)。这两条是"先知道谁、再知道哪里"的一对。

**最后回到 cgroup。** 单机部署时,`max_server_memory_usage` 就是进程的软上限;但容器里真正的硬墙是 **cgroup 的内存限额**。两者必须**对齐**:如果 cgroup 限额比 ClickHouse 的软上限还小,内存一紧张,内核 OOM killer 会在 ClickHouse 走到"先排队再杀查询"那条自保路径**之前**就把整个进程干掉——你看到的是"进程被杀、重启后日志停在半路",而不是那条友好的 `MEMORY_LIMIT_EXCEEDED`(§29.3)。所以容器部署的第一条内存纪律是:让 `max_server_memory_usage` 落在 cgroup 限额的**内侧**。

## 25.14 小结

- 并行是**算子级**的:`max_threads` 决定一条查询内同一阶段被复制成几路,默认 0 = 自动 ≈ 物理核数。加线程能线性扩展,但也线性增加内存。
- 内存限制分三层:单查询 `max_memory_usage`、单用户 `max_memory_usage_for_user`、进程 `max_server_memory_usage`(服务端配置)。超限抛 `MEMORY_LIMIT_EXCEEDED`。它和机器空闲内存无关,而且追踪的是分配器记账,不等于 `top` 的 RSS——这就是"报超限但 top 没满"的答案。偏差**两个方向都有**、来源不止一个:分配器不 `munmap`、`max_untracked_memory`(默认 4 MiB)的"攒着记账"、绕过记账的第三方分配、以及 `mmap` 进来的文件页。撞到上限时先排队再杀查询的 **memory overcommit 在 25.x 默认开启**,但官方仍把它归为**实验性机制**。追踪机制本身(分配器与 MemoryTracker 的分工、三层归因、峰值来自哪几类算子、怎么用 `trace_log` 定位是哪一步吃的)见 §25.13。
- `max_bytes_before_external_group_by` / `max_bytes_before_external_sort` 是防 OOM 的**兜底**,默认关闭,开了慢好几倍,别当调优手段。真正触发落盘的机制是**两级聚合**(`group_by_two_level_threshold` / `group_by_two_level_threshold_bytes`)。
- 限制类设置按**作用对象**分族,每族都是"上限 + `*_overflow_mode`":读(`max_rows_to_read`/`max_bytes_to_read`)、结果(`max_result_*`)、聚合(`max_rows_to_group_by`)、JOIN(`max_*_in_join`)、**`IN` 右集合(`max_*_in_set`)**、**DISTINCT(`max_*_in_distinct`)**、**外传表(`max_*_to_transfer`)**;外加排序(`max_*_to_sort`)与超时几族。`any` 只有 `group_by_overflow_mode` 有,其余全族都只有 `throw`/`break`、默认 `throw`。族与族**互不覆盖**:`SELECT DISTINCT` 打在高基数列上撞内存抛的是 `SET_SIZE_LIMIT_EXCEEDED`(它本质是个带限集合,和 `IN` 集合同码),受自己那一族管辖,`max_rows_to_group_by` 拦不住它;给 DISTINCT 配 `break` 只会得到一份你不知道缺了什么的残缺集合。检查按块进行,不是按行。磁盘侧另有 `max_temporary_data_on_disk_size` 一族。
- 配额是**跨查询、按时间窗口累积**的限制,靠 `CREATE QUOTA` 定义,用 `system.quotas` / `system.quota_usage` 观察。它和"限制设置"是两个不同粒度的东西。
- 并发限制在服务端(`max_concurrent_queries` 一族,代码默认 `0` 即不限、发行版 `config.xml` 模板预设为 `1000`)与会话(`max_concurrent_queries_for_user`)两层;撞到上限默认是**排队**(服务端配置 `max_waiting_queries` 队列长度、查询期 `queue_max_wait_ms` 最长等待,两者默认值**未确认**)而不是立刻拒绝;`KILL QUERY` 靠协作式检查取消标志,所以不一定立刻生效。总 CPU 另由服务端的软性槽位 `concurrent_threads_soft_limit_num` / `concurrent_threads_soft_limit_ratio_to_cores` 兜住(§25.6)——它、`max_threads`(单查询并行上限)、`max_thread_pool_size`(线程数上限)是三件不同的事。
- 优先级有两条路:`os_thread_priority`(nice 值,粗)和**工作负载调度**(`CREATE WORKLOAD` / `CREATE RESOURCE` + `workload` 设置,CPU 与内存一起管)。涉及调度,优先考虑后者。
- 一条设置按"内置默认 → 服务端配置 → profile → 会话 → 查询"逐层覆盖;`readonly` 取 0/1/2 三档,管的是"能不能改设置",不是"能读哪些数据"。
- `parts_to_delay_insert` / `parts_to_throw_insert` 是**写入保护**,不是调优参数。看到 `Too many parts`,治根在"把碎写合成大批次",不是把它们调大。
