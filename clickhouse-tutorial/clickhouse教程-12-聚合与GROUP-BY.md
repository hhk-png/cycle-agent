# 第 12 章 聚合与 GROUP BY

一句 `GROUP BY` 看起来只是一个句子,但在 ClickHouse 内部它至少被拆成三个阶段:每个线程先把分到自己手上的那一部分行压成**聚合状态**,线程（或分片）之间把状态两两**合并**,最后才把状态**变成结果**。这三个阶段解释了三个你迟早会撞上的实际问题:为什么 `avg` 不能只存平均值?为什么 `uniq` 会在只有两个不同值的时候返回 3?为什么对高基数列做 `GROUP BY` 会把内存吃光?这一章就把这三件事从原理讲到调优参数。

## 12.1 聚合的本质是一个可合并的状态机

评价一个聚合函数,不看它算什么,而看它的**状态**长什么样。

```text
一次聚合 = 状态 + 三条边界

   create_state()            一个「空状态」
        │
   add(state, 列, 行号)       一行接一行地喂进去
        │
   insert_result_into(state, 结果列)   把状态变成最终值,写进结果列

另外两条边界决定了它能不能上生产:
   merge(dst, src)                  把两份状态并成一份
   serialize_state / deserialize_state   把状态落盘、跨网络传
```

对照几个常见函数:

```text
sum          状态 = 一个数                 merge 就是加法
avg          状态 = (和, 计数)             merge 是两个分量各自相加
uniq         状态 = 自适应采样摘要          merge 是采样摘要的合并
uniqCombined 状态 = 小基数精确结构,大基数退化成 HyperLogLog   merge 逐桶取最大
groupArray   状态 = 一个数组               merge 是两个数组拼接
```

这里把最容易被将错就错的一处先钉死:**`uniq` 的状态不是 HyperLogLog**,而是**自适应采样(adaptive sampling)**的摘要;**HyperLogLog 属于 `uniqCombined`(大基数时退化的那条路径)与 `uniqHLL12`** —— 详见 §12.6。`uniq` 的状态是一份受控规模的采样摘要,`groupArray` 是可能上百万行的数组 —— **状态大小就是每个分组的代价**,这是后面所有内存问题的根源。

状态必须**可合并**,否则 `GROUP BY` 就只能单线程跑。而“可合并”最漂亮的反例就是平均值:

```text
分片 A: [10, 20]        平均值 = 15
分片 B: [30, 40, 200]   平均值 = 90

(15 + 90) / 2 = 52.5          ← 错!
(10+20+30+40+200) / 5 = 60    ← 对
```

所以 `avg` 的中间状态必须是 `(和, 计数)` 两个可加量,除法只在最后做一次。这也是它属于 `AggregateFunction`(存二进制状态)而不是 `SimpleAggregateFunction`(状态就是最终值)的原因。

## 12.2 两阶段聚合:部分聚合,然后归并

`GROUP BY` 的并行骨架是"**部分聚合 → 归并**"两段:

```text
原始行 ──► AggregatingTransform ──► 部分状态 ──► MergingAggregatedTransform ──► 结果
           (每线程一份)              (每个分组一份)
```

`AggregatingTransform` 把多行压成一份状态,`MergingAggregatedTransform` 把多份状态合成一份并算出最终值。分布式部署里骨架一样,只是归并跑在发起查询的节点上,中间多了网络传输(第 23 章)。

但要把一个过度绝对的说法纠正掉:**这两段并非总会都在。** 归并那一级是在**存在多条独立聚合流**(多线程、多分片,或被两级哈希摊成多个桶)时才被实例化出来的;单线程(`max_threads = 1`)且非分布式时,往往只有一个 `AggregatingTransform` **就地 finalize**,没有单独的归并算子。`optimize_aggregation_in_order`(§12.5)还会改变计划形状、消掉"部分状态归并"这一环。"什么时候其实只有一段"是引擎侧的问题,在第 10 章 §10.9 完整讲;本节只从**语义**上回答:为什么这两段能切得正确。

手算一个例子。设想两个线程各处理一半数据:

```text
线程 1 读到: A=1, A=2, B=5
线程 2 读到: A=3, B=7, B=1

部分聚合的输出(注意是状态,不是结果):
线程 1:  A → {sum: 3, count: 2}
         B → {sum: 5, count: 1}
线程 2:  A → {sum: 3, count: 1}
         B → {sum: 8, count: 2}
         ↑ B 共出现三次:线程 1 一次(B=5)、线程 2 两次(B=7、B=1)

归并:
A → {sum: 6, count: 3} → avg = 2
B → {sum: 13, count: 3} → avg ≈ 4.33
```

这组数字是本章独有的**语义推演**:第 10 章 §10.9 讲执行引擎时,把这份推演**引用**过来(它自己不再重复),所以两处指的是同一个例子 —— A 合并后是 `{sum:6, count:3}`、B 是 `{sum:13, count:3}`,别看岔。本章关心的是**语义为什么成立**,那一节关心的是**引擎怎么把它跑快**。

如果状态是“平均值”这种本身不可加的东西,归并根本无从下手。**“状态可合并”是 `GROUP BY` 能并行的前提,也是 `AggregatingMergeTree` 与物化视图能做增量聚合的前提**(§12.10)。

`sum` 这类可加聚合还有一条捷径:`SimpleAggregateFunction(sum, UInt64)` 的状态**就是**最终值,归并直接相加即可;但它只适用于“加 / 取最值 / 取任意”,`avg`、`uniq` 都不行。

## 12.3 分组数爆炸:两级哈希与应对

聚合的内存 = Σ(每个分组的状态大小)。行数多不可怕,**分组数多**才可怕:1000 万行按 `status` 分组只有 5 份状态(几 KB),按 `user_id` 分组就是 800 万份 —— 哪怕每份状态只有几十字节(比如 `uniq` 的采样摘要),800 万份乘起来也是几个 GB,直接 OOM。**"分组数 × 每份状态大小"才是内存,不是行数。**

单级哈希表的毛病**不在"不能并行"**——§12.2 已经说了,并行来自部分聚合:每条泳道各攒一张小表,谁也不挡谁。毛病在**归并那一步**:所有泳道的表必须在**一个**执行点上合成一张覆盖完整键空间的表,而这张表的规模随分组数线性增长。分组只有几十个时,归并可以忽略;分组到百万级时,归并本身就成了串行瓶颈,而且它**没法只溢出一部分**——要么整张表在内存里,要么整张表刷盘,中间没有"先处理一半"的选项。

ClickHouse 的答案是**两级哈希表**,由两个阈值控制:

```text
group_by_two_level_threshold        默认 100000   (分组数超过 10 万个)
group_by_two_level_threshold_bytes  默认 50000000 (状态字节超过约 50 MB)
```

任一条件满足就切到两级模式:按聚合键的**高位**把哈希表切成 256 个桶,每个桶可以独立地被一个线程处理、独立地被刷到磁盘 —— “分组数极多”的查询也能分到多核上,内存也不再是“全部键”,而是“一次一个桶”。

阈值是**或**的关系、不是**与**:分组数只有 5 个但每份状态是 100 MB 的 `groupArray`,一样会触发两级模式。

分组数爆炸的典型场景与应对:

- **对高基数列直接 `GROUP BY`**(如 `GROUP BY user_id`):先问这个查询真的需要这么多行吗。
- **加 `LIMIT BY` 砍掉每组的多余行**:`ORDER BY ts DESC LIMIT 3 BY user_id` 比 `row_number()` 便宜得多(第 16 章 §16.6)。
- **先 `uniq` 再分组**:要“每个用户有多少种行为”,用 `SELECT user_id, uniq(behavior) FROM ... GROUP BY user_id`,而不是先 `GROUP BY user_id, behavior` 再外层 `count()`。
- **用 `LowCardinality`**:`LowCardinality(String)` 的分组键在哈希表里是整数序号,既省内存又快。

## 12.4 内存控制:先设上限,再选溢出策略

```text
max_rows_to_group_by        默认 0(不限)
group_by_overflow_mode      默认 throw
                            可选 throw / break / any
```

三个语义的差别必须分清:

- `throw`(默认):超限直接抛错 —— **安全**,你立刻知道查询超了预算。
- `break`:不再接受新分组,只返回已有分组。**结果静默地不完整**,你不知道丢了多少。
- `any`:只保留前 N 个分组,落在新分组上的行丢弃 —— 同样不完整,但配合 `ORDER BY count() DESC` 能拿到“最热门的 N 个”。

**错误做法**:为“别让查询挂掉”把 `group_by_overflow_mode` 全局设成 `break`。**后果**:报表数字悄悄偏小,没有任何报错,几个月后对不上账才知道。**正确做法**:保持 `throw`,把 `max_rows_to_group_by` 设成能解释给业务方的数;真要“取前 N 个大组”,用 `ORDER BY ... LIMIT N`。

超出行数上限还有一条出路是**外部聚合**(落盘):

```text
max_bytes_before_external_group_by   默认 0(不落盘)
```

设成比如 10 GB,状态超过这个量就溢写到磁盘、最后再归并回来。它能救回一个大查询,但代价是磁盘读写、序列化往返,以及**归并慢一个数量级**;经验上设成可用内存的一半左右。

绝对阈值的麻烦在于"换台机器就得重设":10 GB 对一台 64 GB 的实例合适,对一台 512 GB 的实例又太保守。于是较新的 ClickHouse 给了它一个**按比例**的兄弟 `max_bytes_ratio_before_external_group_by`(取值是 0~1 的比例):把触发溢出点定义成"可用内存的某个百分比",随实例内存**自动缩放**,一套配置在大小机器上都说得过去。不过它的引入版本与默认值我**未确认**:有资料称它 24.12 引入(当时默认 `0`)、25.1 起默认 `0.5`(若属实,25.3 上就已经默认生效),但这是版本相关的细节,请以 `SELECT name, value, default FROM system.settings WHERE name LIKE 'max_bytes_ratio_before_external%'` 的实际输出为准。要留意的是:它和绝对阈值 `max_bytes_before_external_group_by` **互斥**——两个一起设会直接报错(提示二者不能同时设置),所以根本不存在“谁优先”的问题,二选一即可。要同时防住"分组数太多"和"单份状态太大"两种内存爆炸,建议把 `max_rows_to_group_by`(硬上限,配 `throw`)和这两个落盘阈值一起设上。

## 12.5 有序聚合:让 GROUP BY 顺着排序键流式跑

两级哈希表(§12.3)解决的是"分组太多、内存装不下"。但如果**数据本身已经按分组键有序**,还有一条更彻底的路子:根本不用为所有分组维护状态,而是**边读边聚合** —— 读到行就累加进"当前组"的状态,一旦分组键的值变了,就把当前组的状态吐出去、重置,再开下一组。

这样一来,内存从"所有分组的状态之和"降到"一个分组的状态"。分组数再多也不怕,因为你从头到尾只在内存里留一份。这就是 `optimize_aggregation_in_order`,它和 §10.7 讲的 `optimize_read_in_order` 是一对孪生优化:前者消掉排序算子,后者消掉聚合哈希表,共同前提都是"`GROUP BY` / `ORDER BY` 的键正好顺着排序键"。

```text
optimize_aggregation_in_order   默认 0      —— 必须显式打开
```

**默认是 0**,这点很重要:打开它不会自动发生,你得在需要的查询上 `SET optimize_aggregation_in_order = 1`(或者写进 profile)。它生效的前提是 `GROUP BY` 的键是排序键的前缀;键不匹配时这条设置不起作用,查询会退回哈希表方案。

配套还有几个设置,职责各不相同:

`aggregation_in_order_max_block_bytes` 限定有序聚合时中间块的大小 —— 有序聚合不是"一行一行"吐,而是攒成块再往上游传;块越大,内存占用越高、但合并次数越少。它确切的默认值是 **50000000 字节(50 MB)**;把它调小一点,能让最终归并阶段并行得更好。

`optimize_aggregators_of_group_by_keys`(**默认 1**)是一个更小的优化:`GROUP BY` 键上的 `min(k)`、`max(k)`、`any(k)` 这类聚合,结果**就是 `k` 本身**(一个分组里 `k` 只有一个值),优化器可以直接取键值、省掉实际的聚合计算。

`distributed_aggregation_memory_efficient`(**默认 1**)作用在**分布式**聚合的归并阶段:它让来自各分片的中间状态**分桶合并**(按 key 的哈希分成若干桶,一桶一桶地合并、合并完就释放),而不是先把所有分片的所有状态都加载进内存再合。分片多、分组多时,这是把归并阶段的内存峰值压下来的关键;关掉它(设 0)通常只为排查内存问题。

还有一条与分布式聚合**形态**有关的设置:`distributed_group_by_no_merge`。默认 `0` 时,分布式聚合会把各分片的**部分状态**收拢到发起节点、再做最终归并(就是 §12.2 的第二段)。如果把它打开,发起节点**不做这层归并**,各分片各算各的、结果直接拼接返回 —— 这适用于"**分组键就是分片键、每个分组只可能落在某一个分片**"的情形:此时各分片的结果本就不重叠,再归并纯属浪费。反过来说,一旦分组键与分片键不一致,同一个分组会散落在多个分片,**关掉归并就会得到重复的分组行**。所以用之前必须先确认"分组键 ⊆ 分片键",否则结果会悄悄错。(它的取值范围、以及是否还接受其它模式标记,我**未确认**,以你那版文档为准。)

一句话:**当"分组数极多"而且"数据本来就按分组键排好"时,有序聚合是比两级哈希更省的选择;代价和有序读一样落在并行度上**(顺序读天然不那么好切并行)。它和第 24 章的优化器设置是同一套东西,只是从聚合这一侧看。

## 12.6 常用聚合函数:按“状态”分成四族

记一个聚合函数,最省力的方式不是背它的算法,而是记住它**在一个分组里维护的那份状态是什么**。状态决定了三件事:能不能并行合并、能不能增量维护、以及内存有多大。按"状态"把常用函数分成四族,顺着走下来,"为什么有的准、有的不准、有的很吃内存"就都能自己推出来。

**第一族:状态就是"一个或几个可加的数",最便宜。** `count()` 数行,`count(x)` 数 `x` 非 NULL 的行,两者不等价;`count()` 连列都不用读,这是 `SELECT count()` 极快的原因。`countIf(x, c)` 来自 `-If`;`countDistinct(x)` 语义等同 `uniqExact`,别看它名字像计数就以为它便宜 —— 它其实属于下一族。

`sum` 对小整数会**自动提升**(`sum(UInt8) → UInt64`、`sum(Int8) → Int64`),因为一列一百万个 200 会瞬间把 `UInt8` 撑爆,提升是在防溢出。想按 C++ 语义回绕用 `sumWithOverflow`;浮点累加误差大换 `sumKahan`;`sumCount` 一次给出 `(和, 计数)`(它正是 `avg` 的内部状态被单独暴露出来);`sumArray` 是 `-Array`,对每个数组元素求和。

`avg` 的状态是 `(和, 计数)` 这一对 —— §12.1 已经说明为什么它不能只存一个平均值。空集上 `avg` 返回 `nan`(不是 0 也不是 NULL),要 NULL 用 `avgOrNull`;`avgWeighted(x, w)` 的状态是 `(Σx·w, Σw)`。

`min`/`max` 只留当前最优值,状态就是那个值本身,所以它们既便宜又可退化成 `SimpleAggregateFunction`。`argMin(arg, val)` / `argMax(arg, val)` 则不同:它要留的是**使 `val` 取到极值的那一行的 `arg`**,所以状态里必须同时记住 `arg` 和 `val`。注意这个状态**是可合并的**——`argMin`/`argMax` 各有对应的 `argMinMerge`/`argMaxMerge`,分布式聚合、多线程部分聚合都靠它把两份 `(arg, val)` 状态并成一份。它**不能**退化成 `SimpleAggregateFunction` 的原因不在这里,而在"**状态 ≠ 结果**":`SimpleAggregateFunction` 要求列里存的就是最终值,而 `argMin` 的最终值是一个 `arg` 类型的值,状态却是 `(arg, val)` 一对,两者对不上。

`any` 取第一个见到的值,`anyLast` 取最后一个,`anyHeavy` 用重击者算法取“出现得最多”的值。`any` 的状态极小、几乎不要钱,代价是**结果不确定** —— 只适合“同一分组内这一列本来就该相同”的场景(比如按 `order_id` 分组取 `user_id`),把它当成“随便取一个有代表性的”用在语义不唯一的地方,就是 bug 温床。

**第二族:近似状态 —— 用受控的空间换“内存不随基数爆炸”。** 这一族的状态不是精确答案,而是一份摘要,结果**可能不准**;但正因为摘要小、可合并,它才撑得起海量去重与分位数。**去重计数**是这一族的代表,先看一张“状态对照表”:

```text
uniqExact       精确。状态 = 一个键集合,内存随基数线性涨
uniq            近似。状态 = 自适应采样(adaptive sampling),内存受控
uniqCombined    小基数精确、大基数近似(先数组/哈希表,过阈值才退化成 HLL)
uniqCombined64  同 uniqCombined,但哈希用 64 位,基数可超 2^32 而不塌缩
uniqHLL12       HyperLogLog,固定较低的精度(更省内存、更不准)
uniqTheta       近似。状态 = Theta 草图(DataSketches),可做集合交并差
uniqUpTo(N)     精确,但数到 N 就够:≤N 返回真值,>N 返回 N+1
```

这里要专门纠正一个流传很广、连不少教程都抄错的断言:**`uniq` 用的不是 HyperLogLog。** 真实 ClickHouse 的 `uniq` 走的是**自适应采样(adaptive sampling)** —— 小基数时保留精确信息,基数一大就切换到采样估计,它**不接受精度参数**。真正用 HyperLogLog 的是 **`uniqHLL12`**,以及 **`uniqCombined`** 在大基数时退化的那条路径;把 `uniq` 和 HLL 划等号是错的。(§12.12 会看到,mini-clickhouse 用 HLL 实现了 `uniq`,那只是一个**简化**,不代表真实语义。)

HLL 那条路长什么样?它用固定长度的寄存器数组,每个元素只记录“哈希尾部的连续零”信息,插入只更新单个寄存器,**内存与基数几乎无关**,合并两份草图就是**逐桶取最大**。参数上要分清“是谁的”:真实 ClickHouse 的 **`uniqCombined` 默认精度是 17**(`2^17` 个寄存器、每格约 **6 bit**、合计约 **96 KiB**),可调范围 **[12, 20]**;而 `uniq` 本身没有精度参数。`uniqCombined64` 与 `uniqCombined` 的差别只在哈希宽度:64 位哈希适合基数可能超过 `2^32` 的列,否则 32 位哈希在大基数上会因碰撞而**低估**。`uniqTheta` 的价值不只是“数得准”,而是**草图之间可做交、并、差** —— “有多少用户既访问过 A 又访问过 B”这类集合运算,它比 `uniq` 家族更合适(`uniq`/HLL 的草图只能合并成并集,求不了交)。

mini-clickhouse 的 `HyperLogLog` 默认精度是 **14**(`2^14` 个**一字节**寄存器),可调范围同为 `[12, 20]` —— 那是 **mini 的实现参数**(出现在 §12.12 的对照里),**不是**真实 ClickHouse 的默认值。真实 `uniqCombined` 的默认是 `2^17` 个 6-bit 寄存器,两边的数字别记混。

**误差从哪来,以及为什么小基数也不一定准。** 近似估计值是连续量的舍入:真基数为 k 时绝对误差大致随 k 线性增长(相对误差近似恒定),所以**小基数上绝对误差也能到 ±1**,四舍五入就出错。社区里反复观察到“只有 2 个不同值却返回 3”这类偏差(近似结果可能**偏大**),它正是“小基数下也可能不准”的具体形态。所以“只有几万个不同值,`uniq` 总该准了吧”这个假设站不住:要精确就用 `uniqExact`。(官方文档对 `uniq` 只作“近似”的定性说明,并不承诺某个固定的误差百分比,不同版本、不同算法下措辞也不一,所以这里**不背一个具体数字**;工程判断很简单 —— 结果要进合同、进账单,就用 `uniqExact` 或先 `GROUP BY` 把基数降下来。)

**第三族:状态是集合或数组(大状态)。** 这一族最重,却也最直观 —— 收集类首当其冲:`groupArray(x)` 收集全部值,`groupUniqArray(x)` 去重后收集。这一族状态大、合并要复制;`groupArray` 之后接 `arrayJoin` 是“先聚起来再拆开”,只想展开数组就直接对原始列 `arrayJoin`。另有 `groupArrayLast(N)` 与 `groupArrayInsertAt(default_x, size)(x, pos)`。

`groupArrayInsertAt` 的签名值得单独记,因为它的两组参数职责不同:**要插入的值 `x` 和位置 `pos` 是内层实参,占位填充值 `default_x` 与结果数组长度 `size` 是外层参数**。外层第一个参数 `default_x` 决定空位填什么(省略则用元素类型的默认值);外层第二个参数 `size` 决定结果数组有多长——`default_x` 与 `size` 要么都不给、要么一起给(只给 `size` 会报错)。位置 `pos` **从 0 开始计数**;同一个位置被插多次时,单线程取第一个、多线程结果不定。

**回头看第二族的另一半:分位数。** 它和去重计数是一个思路 —— 用有限的结构,去近似一个"理论上要全体数据才算得准"的量:

```text
quantile(level)          近似(采样 + 插值),返回 Float64 —— 结果可能在数据里不存在
quantiles(0.5, 0.9)      多个 level,返回 Array(Float64)
median(x)                = quantile(0.5)
quantileExact            精确,返回真实存在的元素,保持输入类型
quantileTiming           为“耗时(毫秒)”设计的直方图近似
quantileTDigest          t-digest 草图,尾部更准
quantileDeterministic    带种子的可复现采样
```

最常踩的坑是**插值之别**:`quantile(0.5)` 会按**线性插值**取分位点,插值出来的值**可能并不存在于数据里** —— 例如对 `[1, 2, 3, 4]`,中位点落在第 2、3 个元素之间,返回 `2.5`(数据里没有 `2.5`)。也正因为结果可能是插值出的连续量,它的返回类型恒为 `Float64`。`quantileExact` 则保证返回**真实存在**的元素,所以保持输入类型。这不是随便定的,是“结果到底是不是原始值”决定的。(别走另一个极端:插值也不保证一定给出陌生值——`[1, 2, 2, 3]` 的中位点落在两个 `2` 之间,返回的仍是数据里存在的 `2`;区分点始终是“插值的结果允不允许不是原始值”,而不是“这一次碰巧取到什么”。)近似的一族:**`quantile`/`quantiles`/`median`/`quantileTiming`/`quantileTDigest`/`quantileDeterministic`**;精确的一族:**`quantileExact`/`quantilesExact`**。

**第四族:状态维度固定、但不止一个数。** 最典型的是统计量:`varPop`/`varSamp`、`stddevPop`/`stddevSamp`、`covarPop`/`covarSamp`、`corr`、`skew*`/`kurt*`。它们共同的难题是方差公式 `E[X²] − (E[X])²` 在均值大、方差小时会灾难性抵消,所以要靠 Welford 式在线算法(状态是 `[n, mean, M2]`)并保证状态可合并。选函数时记住:总体还是样本,决定了分母是 `n` 还是 `n−1`。

还有几件值得知道名字的:`topK(N)(x)`(近似取最高频的 N 个)、`topKWeighted(N)(x, w)`、`entropy(x)`、`histogram(N)(x)`、`groupBitAnd`/`Or`/`Xor`(按位与/或/异或),以及序列/漏斗分析的一族 —— `windowFunnel`(窗口内按序命中了几个步骤)、`sequenceMatch`(事件序列是否匹配某个图案)、`retention`(某天来的用户后续几天是否回来)。这三个是"手搓会话切分"的正解,第 16 章 §16.14 会用到它们的场合。

**回到大状态族,把它的成员补齐。** 先是"按 key 汇总":`sumMap(key, val)` 按 key 分组求和、返回一个 Map;它的兄弟 `minMap` / `maxMap` 做同一件事但取最小/最大值。`sumMapFiltered(keys_to_keep)(key, val)` 在 `sumMap` 的基础上再传一组"只保留这些 key",避免高基数的 key 把结果 Map 撑爆 —— 只要 key 的候选集是固定的(比如固定的几个状态码),就用它,别先 `sumMap` 再在外面过滤。

**大状态族的另一个成员是位图**,它有一个**极容易记反**的点:`groupBitmap(x)` 作为**顶层聚合函数,返回的是基数(一个整数)**,**不是**位图对象。真正的位图**状态**型是 `groupBitmapState`:它在分组里维护一份 Roaring 位图状态,供做去重计数或喂给 `AggregatingMergeTree`;要合并多份位图状态,用 `groupBitmapMerge(state)`(而不是指望 `groupBitmap` 直接吃状态)。`groupBitmapAnd` / `groupBitmapOr` / `groupBitmapXor` 则是对**传入的位图**做交/并/异或。所以"当去重之外还要做集合运算"时,位图比 `uniq` 一族更直接:它能**精确**求交并,代价是只吃整数、基数也不能太大。

**手工摆弄聚合状态的两个函数**。`initializeAggregation('sum', x)` 给单个值造出一份状态,`finalizeAggregation(state)` 把状态变回结果值 —— 想手动拼装/拆解 `AggregateFunction` 状态时用它俩。`arrayReduce('quantile(0.5)', arr)` 更常用:它在 SQL 里对一个数组**跑一遍聚合函数**,等价于把数组元素当行喂给这个聚合。它让你不用先 `arrayJoin`(把行数炸开)就能对数组做聚合,是"数组里求中位数 / 求和"的标准写法。

## 12.7 组合器:几十个函数是拼出来的

`system.functions` 里的聚合函数看起来有几百个,真正独立实现的只有五六十个,其余都是**组合器**(combinator)套出来的壳。组合器这套机制本身(后缀怎么拼、参数与结果类型怎么被改写、完整的组合器清单)属于"函数系统"的话题,第 17 章 §17.2 从那一侧讲;本节只挑**聚合侧最要紧的几个**,因为它们是增量聚合的全部基础。

```text
基函数 sum
  ├── sumIf(x, c)       = sum + -If          条件为假就跳过
  ├── sumArray(a)       = sum + -Array       对每个数组元素求和
  ├── sumState(x)       = sum + -State       返回状态,不是结果
  ├── sumMerge(s)       = sum + -Merge       吃状态,吐结果
  └── sumIfMerge(s)     = sum + -If + -Merge ← 组合器可以叠加
```

关于拼接,记住两条规则就够:

- **组合子有位置约束,不是"从右往左一层层剥"。** `-Array` 必须紧贴基函数名,其余组合子往后再接:所以 `uniq` 叠 `-Array` 再叠 `-If`,拼出来是 `uniqArrayIf(arr, cond)`,不是 `uniqIfArray`。
- **注册表里的整名优先。** 本身就是注册表名字的(`sumMap`、`groupUniqArray`、`countDistinct`)按整名解析,不会被误拆成"基函数 + 组合子"。

规则的全貌、名字冲突的优先级,以及随版本更新的完整组合器清单(`system.aggregate_function_combinators`)在第 17 章 §17.2 —— 那是函数系统的主场,本节不重复。

聚合侧真正的主角是下面这一对,以及它的两个变体。

`-State` 与 `-Merge` 是本节**重点**,它们是增量聚合的全部基础。`sumState(x)` 返回的不是和,而是 `AggregateFunction(sum, ...)` 类型的**中间状态**;`sumMerge(s)` 反过来把多份状态合并成结果。物化视图把 `-State` 写进 `AggregatingMergeTree` 的列、查询时用 `-Merge` 收口,靠的就是它们(§12.10 有完整例子)。记住一句话:**`-State` 把“结果”降级成“状态”,`-Merge` 再把它“结算”回结果。**

`-SimpleState` 与 `-MergeState` 是上面一对的变体。`-SimpleState` 产出的状态**就是最终值本身**(`sumSimpleState(x)` 返回 `SimpleAggregateFunction(sum, T)`)—— 这正是 §12.2 里 `SimpleAggregateFunction` 的来源:只有"状态 = 结果"的聚合(加、取最值、取任意)才能用它,`avg` / `uniq` 不行。`-MergeState` 和 `-Merge` 只差一个字、语义却不同:两者都吃状态,但 `-Merge` **算出最终值**,`-MergeState` **只把多份状态合并、仍返回状态** —— 用在"先把部分状态并起来、但还不想结算"的场合(合并后的状态还能继续喂给下游)。

还有两个在聚合侧常用、值得知道名字的。`-Map` 针对 `Map` 类型参数:把 Map 里每个 key 的值分别聚合,省掉"先展开 Map 再按 key `GROUP BY`"这一趟(它就对应 §12.6 里的 `sumMap` 一族)。`-ArgMin` / `-ArgMax` 是"用另一个参数的极值来挑选参与聚合的行":`sumArgMax(x, y)` 求的是"在 `y` 取到最大值的那些行上,对 `x` 求和";它和聚合函数 `argMin`/`argMax` 不是一回事——后者返回那一行的某个**列值**,前者返回的是"对这些行再做一次**聚合**"的结果(逐条对照见 §17.2)。

其余那些通用组合器(`-If`、`-Array`、`-OrNull`、`-OrDefault`、`-Resample`、`-ForEach`、`-Distinct` …)在标量与聚合上的表现一致,这里不重复,统一放在 §17.2;要给"数据缺桶时补默认值",用 `WITH FILL`(第 16 章 §16.5)或 `-Resample` 是可靠路径。

## 12.8 GROUP BY 的修饰符:小计、总计与它们的坑

设分组数为 `n`:

```text
GROUP BY a, b                  n 行
WITH TOTALS                    n + 1 行         多一行“总计”
WITH ROLLUP(两个分组键)         n_ab + n_a + 1   每个前缀一级小计
WITH CUBE(两个分组键)           n_ab + n_a + n_b + 1   2^k 个组合
GROUPING SETS ((a), (b), ())   各集合的行数之和
GROUP BY ALL                   非聚合列全当分组键,n 与其他写法一致
```

**`WITH TOTALS` 的坑必须讲透**:多出来的那一行是“所有行的总聚合”,而**它的分组键列会被填成类型的默认值**(整数 `0`、字符串空串、数组空数组)。

**错误做法**:`SELECT user_id, count() FROM t GROUP BY user_id WITH TOTALS`,然后在前端把 `user_id = 0` 的行当成“总计行”渲染。

**后果**:如果 `user_id` 本身可能是 `0`(或空串),总计行会与一个真实分组**混在一起**,数字莫名其妙地大一倍。

**正确做法**:总计行要么通过驱动程序的“totals”通道单独取(它是协议层的一等公民,不进普通结果集),要么在 SQL 里用 `GROUPING()` 显式标记。

`WITH ROLLUP` / `WITH CUBE` / `GROUPING SETS` 里被“卷起来”的那些键列,填充规则由这个设置控制:

```text
group_by_use_nulls   默认 0
```

- `0`(默认):填**类型默认值**,与 `WITH TOTALS` 一样 —— 一个 `0` 到底是“分组键真是 0”还是“这一级没用这个键”,分不出来。
- `1`:与 SQL 标准一致,这些键列转成 `Nullable`、没用的位置填 `NULL`,代价是 `Nullable` 的额外开销。

所以默认配置下,**判断“这一行是超级聚合行”只能靠 `grouping()`**,不能靠“值是不是 0 / 空串”。

`GROUP BY ALL` 是 ClickHouse 的便利语法:把所有**没出现在聚合函数里**的 SELECT 表达式都当分组键。写报表类查询省事,但改 SELECT 时容易悄悄改变分组粒度,**改完回看一眼结果行数**。

## 12.9 WHERE 与 HAVING 的分工

**`WHERE` 在聚合之前生效,`HAVING` 在聚合之后生效**。这不是语法问题,是量级问题。

**错误做法**:`SELECT user_id, sum(amount) FROM t GROUP BY user_id HAVING user_id != 0`。

**后果**:所有 `user_id = 0` 的行照常进哈希表、照常累加,最后才把那一组扔掉;分组越多浪费越大。

**正确做法**:`SELECT user_id, sum(amount) FROM t WHERE user_id != 0 GROUP BY user_id`。

原则很简单:**能在 `WHERE` 里过滤的,不要放到 `HAVING`**。`WHERE` 的过滤还能吃到主键与跳过索引(第 11 章),`HAVING` 什么都吃不到。`HAVING` 留给“依赖聚合结果”的条件,比如 `HAVING sum(amount) > 10000` 或 `HAVING count() > 100`。

再往上一层:过滤其实有三档,越靠前越省。`PREWHERE` 排在 `WHERE` **之前**执行,并且**按列推进**——先只用它涉及的少数列把行读进来、筛掉大部分,只把存活的行送去读剩余的列(第 07 章 §07.5)。于是三段过滤对聚合的代价是递增的:

- `PREWHERE`:在“读列”这一层就砍掉行,省的是 IO 与解压;
- `WHERE`:在聚合前过滤,能吃到主键裁剪与跳过索引,但列已经读了;
- `HAVING`:在聚合后过滤,数据早已进过哈希表、累加过,省不了任何读取。

实际很少手写 `PREWHERE`:`optimize_move_to_prewhere`(默认 1)会让优化器自动把 `WHERE` 里适合提前的条件提上去,所以平时只管把条件写进 `WHERE` 就好;需要确认时看 `EXPLAIN` 里条件落到了哪一层(第 24 章 §24.6)。两个边界:它只对 `*MergeTree` 家族生效,而且带 `FINAL` 的查询默认不做这个下推(`optimize_move_to_prewhere_if_final` 在 25.3 上默认 0)。一句话:**别把 `WHERE` 里的条件挪进 `HAVING` 当成“等价写法”,那会把“读的时候顺便筛”退化成“读完、聚完才扔”。**

## 12.10 实用模式:物化视图 + AggregatingMergeTree 做增量预聚合

明细是秒级的,报表要看分钟级汇总,又不能每次查询都重扫全量明细 —— 做法是让**写入时**就把状态存下来。骨架只有三块:明细表、一张列类型是**聚合状态**的汇总表(`AggregatingMergeTree`),以及把两者接起来、在 SELECT 里用 `-State` 把结果降级成状态的物化视图。

```sql
CREATE TABLE events_by_minute (
    minute DateTime,
    user_id UInt64,
    amount_sum AggregateFunction(sum,  Decimal(18, 2)),   -- 存「和」这个状态
    user_uniq  AggregateFunction(uniq, UInt64)            -- 存采样摘要
) ENGINE = AggregatingMergeTree ORDER BY (minute, user_id);

CREATE MATERIALIZED VIEW mv_events_minute TO events_by_minute AS
SELECT toStartOfMinute(ts) AS minute, user_id,
       sumState(amount) AS amount_sum,      -- -State:把结果降级成状态
       uniqState(user_id) AS user_uniq
FROM raw_events GROUP BY minute, user_id;

-- 读的时候再用 -Merge 把多份状态结算成一个结果
SELECT minute, sumMerge(amount_sum) AS total_amount, uniqMerge(user_uniq) AS users
FROM events_by_minute GROUP BY minute ORDER BY minute;
```

要看的是一条更完整的三列版本(`sum` / `avg` / `uniq` 各一列,连同 `-Merge` 结算)以及逐条推演,去 `第 13 章 §13.4` —— 那里才是这套模式的主场。本节只需要把"为什么必须这么写"讲通。

**为什么必须用 `-State` 而不是直接存结果?** 因为物化视图**按插入块触发**:`raw_events` 每收到一批数据就执行一次 SELECT,结果**追加**进 `events_by_minute`;同一分钟会有很多次插入,于是同一分钟的汇总行会出现**很多行**。

- 存 `sum` 的结果还能靠外层 `sum()` 加回来 —— 侥幸能对。
- 存 `avg` 的结果,外层再平均一次就**错了**(§12.1 那个 52.5 的例子)。
- 存 `uniq` 的结果,外层再 `uniq` 一次也**错了** —— 你不是在求并集,而是把多个估计值又当成输入去估计。
- 存 `quantile`/`median` 的结果,同样无法还原。

存状态就没有这个问题:外层用 `-Merge` 把多份状态**正确地合并**,再一次性算出结果;`AggregatingMergeTree` 的后台合并还会顺手把同一分钟的状态行合并掉(第 05 章)。对 `sum`/`max`/`min`/`any` 这类状态就是结果本身的聚合,可以省掉 `-State`/`-Merge`,改用 `SimpleAggregateFunction`。

这一节给的是"能用"的最小闭环。物化视图本身还有一批必须知道的机制与坑——触发器在写入路径上具体怎么被触发、`TO` 与自带内部表的落点差异、`POPULATE` 的竞态、幂等与去重的边界、物化视图链的执行顺序、以及回填历史数据的三种做法——集中在 `第 13 章 物化视图与增量聚合`。本节只负责把"聚合状态为什么能持久化"这件事讲通。

## 12.11 位图:当去重之外还要做集合运算

§12.6 把位图归进"大状态族",只给了一段。这一节把它补全——因为位图解决的是 `uniq` 一族**根本解决不了**的那类问题:**除了去重计数,还要把集合本身拿去求交并差**。

**先看 `uniq` 为什么不够。** 问:"近 30 天活跃的用户里,有多少人同时下过单?"用近似函数答不出来。`uniq` / `uniqCombined` / `uniqHLL12` 返回的是**估计值**,把两个估计值拿去求交本身就没有意义——误差会叠加,甚至能算出负数或"交集大于子集"这种荒谬结果。换成 `uniqExact` 也不行:它给出的同样只是一个**计数**,而交集需要知道"**具体是哪些值**",计数里没有这个信息。**你要的不是数字,是集合。**

**位图就是"可存储、可运算的集合"。** 一串比特,第 k 位为 1 表示"值 k 在这个集合里";求交并差就是按位与或非,一次遍历完事。ClickHouse 底层用的是 Roaring 位图。

它在表里的列类型写作 **`AggregateFunction(groupBitmap, UInt32)`**。这个写法本身就在提示你:它**不是普通类型,而是一个聚合函数的中间状态**——和 §12.10 里 `AggregateFunction(sum, Decimal(18,2))` 处在完全相同的位置。这条决定了它的用法:**位图几乎总是和 `AggregatingMergeTree` 一起出现**(先把集合存下来,以后任意时刻再拿出来做运算)。

**函数分三层,把这三层认清就不会写错。**

**第一层:造位图。** 聚合函数 `groupBitmapState(x)` 按分组把一列无符号整数攒成一个位图状态。

这里有一处**极容易记反**、而且错了会静默出错的地方:**`groupBitmap(x)` 与 `groupBitmapState(x)` 返回的东西完全不同**。`groupBitmap(x)` 是**顶层聚合函数**,返回 `UInt64`——一个**基数**,一个普通整数;`groupBitmapState(x)` 返回的才是**位图对象**。前者不能拿去求交,后者才能。想直接从现成数组造位图,还有 `bitmapBuild(array)`(它要求无符号整数,有符号列要先 `toUInt32` 转一次)。

**第二层:读位图。** `bitmapCardinality(bitmap)` 给出基数(对无符号整数而言,它等于"精确去重计数");`bitmapContains(bitmap, x)` 判断某个值在不在集合里;`bitmapToArray(bitmap)` 把位图展开回数组——**结果按升序**,读起来直观,但在大位图上非常吃内存,**不要在生产查询里随手用它**。

**第三层:算位图。** `bitmapAnd` / `bitmapOr` / `bitmapXor` / `bitmapAndnot` 做交、并、异或、差,**返回的还是位图**;`bitmapAndCardinality` 则是"算完直接给基数"的合体版本(交、并、异或、差各有对应)。**只要你不需要把结果位图继续用下去,就优先用带 `Cardinality` 的那个版本**——省掉一次位图物化,而位图可能很大。

聚合侧另配了三个组合器:`groupBitmapOr` / `groupBitmapAnd` / `groupBitmapXor`,用来把**多份位图**并起来;已经落在存储里的位图状态,用 `groupBitmapMerge(state)` 结算(写入路径上把结果降级成状态则用 `groupBitmapMergeState`)。

**一个完整例子:把"每天的活跃用户"存成位图。**

这是位图最主流的用法——不做"计数",而是把**用户集合本身**按天存下来,之后所有涉及"人群之间做运算"的问题都变成位图运算。

```sql
CREATE TABLE daily_users (
    day     Date,
    segment String,
    users   AggregateFunction(groupBitmap, UInt32)   -- 这一天、这个分群的全部用户
) ENGINE = AggregatingMergeTree ORDER BY (day, segment);

CREATE MATERIALIZED VIEW mv_daily_users TO daily_users AS
SELECT toDate(ts) AS day, segment, groupBitmapState(toUInt32(user_id)) AS users
FROM events GROUP BY day, segment;
```

有了这张表,三类问题都变成按位运算:

```sql
-- ① 日活:直接读基数,不需要回头扫明细
SELECT day, bitmapCardinality(groupBitmapMerge(users)) AS dau
FROM daily_users GROUP BY day ORDER BY day;

-- ② 次日留存:两天的活跃集合求交,再取基数
SELECT bitmapAndCardinality(
    (SELECT groupBitmapMerge(users) FROM daily_users
      WHERE day = '2026-10-15' AND segment = 'all'),
    (SELECT groupBitmapMerge(users) FROM daily_users
      WHERE day = '2026-10-16' AND segment = 'all')
) AS retained;

-- ③ 人群圈选:同一批人的两个标签求交
SELECT bitmapAndCardinality(
    (SELECT groupBitmapMerge(users) FROM daily_users
      WHERE day = '2026-10-16' AND segment = 'paid'),
    (SELECT groupBitmapMerge(users) FROM daily_users
      WHERE day = '2026-10-16' AND segment = 'mobile')
) AS paid_and_mobile;
```

请重点看清 ② 意味着什么:**留存率从一个"查不动的问题"变成了一个 O(位图大小) 的按位与**。如果这张表存的是 `uniqExact(user_id)` 的**计数**,这个留存率**永远算不出来**——你必须回头去扫两天的明细表做一次真实的 join。这就是位图不可替代的地方,也是它在用户增长、人群圈选、广告归因这类场景里成为标配的原因。

**什么时候不该用位图。**

- **它只吃无符号整数。** 用户 ID、商品 ID、设备号这类天然合适;字符串要先用字典映射成整数,或用 `cityHash64` 一类哈希——但**哈希会引入碰撞,"精确"就退化成了"近似"**,值域大的时候尤其要自己权衡清楚。
- **基数有上界。** 列类型里的那个 `UInt32` 就是上界,值域不能超过 2^32。也存在 `UInt64` 的变体,但空间与计算代价明显更高,只在确实需要时才用。
- **它是"整体物化"的对象。** 求交并时**整张位图都要参与计算**,不像 `uniq` 那样能随时合并小摘要。所以在基数极大的集合上,位图的内存与计算代价会明显高于 `uniq` 一族。
- **只要一个去重计数、不需要做集合运算时,不要用位图。** 这时 `uniq`(近似、快、状态小)或 `uniqExact`(精确、内存随基数增长)更合适。判据一句话就能记住:**"这个集合以后要不要和别的集合做运算?"要,用位图;不要,用 `uniq` 一族。**

**还有一点容易误解:位图和 `uniqExact` 不是"谁一定更好"。** 对无符号整数输入,`groupBitmap` 的基数与 `uniqExact` 的结果**是相等的**——两者都精确。差别在**代价模型**:`uniqExact` 的内存随基数**线性**增长(它得存下所有不同的值),而 Roaring 位图对**稠密**值域非常便宜(一段连续整数只占极少空间)、对**稀疏且分散**的值域反而可能更贵。所以选哪个,取决于你的 ID 是稠密还是稀疏、以及你需不需要做集合运算——**"位图一定比 uniqExact 省"是错的**,反过来也一样。

**mini-clickhouse 的边界。** `groupBitmap` 与 `groupBitmapAnd/Or/Xor`、以及整族 `bitmap*` 函数在 mini-clickhouse 里**都没有实现**(§12.12 的清单里就列着它们),所以这一节的例子只能在真实 ClickHouse 上跑。mini 那边用的是另一套近似方案(带小基数修正的 HyperLogLog),那是**简化**,不代表真实语义——这一点在 §12.1 已经说过一次,这里再强调一遍,因为位图恰好是"近似 vs 精确 vs 集合运算"三者差别最大的地方。

## 12.12 与 mini-clickhouse 的对照

`mini-clickhouse/minich/aggregates/base.py` 里的 `IAggregateFunction` 与真实 ClickHouse 同名同形:`get_return_type` / `create_state` / `add` / `merge` / `insert_result_into` / `serialize_state` / `deserialize_state`。唯一简化是 `add(state, columns, row)` 收“列 + 行号”而不是 C++ 版的一堆重载。组合器的解析在 `_resolve` 里:先查注册表,查不到才按后缀从右往左剥(`Merge` → `State` → `Resample` → `OrNull` → `OrDefault` → `Distinct` → `Array` → `ForEach` → `If`)—— 也就是说 §12.7 里点名的 `-SimpleState`、`-MergeState`、`-Map`、`-ArgMin`/`-ArgMax` 这几个 mini 都不认,写出来会当作未知函数报错。

`minich/execution/processors.py` 的两个处理器正是 §12.2 的两段:

- `AggregatingTransform` 做部分聚合,状态存在 `self._states: Dict[Any, List[Any]]`,另有 `self._order` 保持分组首次出现的顺序以便确定性输出。`max_rows_to_group_by` 在新建分组时检查,超了抛 `TOO_MANY_ROWS`。
- `MergingAggregatedTransform` 把多份状态合并。它的 `with_finalize` 开关正是 `-State` 的实现:为真输出最终值,为假**原样保留状态**给外层(物化视图的场景)。合并时用 `serialize_state`/`deserialize_state` 往返一次来克隆第一个状态 —— 这正是“状态必须精确往返”的实战用途。

**单级哈希表 vs 两级**:mini-clickhouse **只实现单级哈希表**(一个 Python dict),`group_by_two_level_threshold` 那两个设置在这里只是“注册了名字”,所以它不会 OOM 到触发落盘,而是直接把内存吃光 —— 刻意的简化,不是遗漏。

**HyperLogLog 是真实实现,不是库调用**。`minich/aggregates/uniq.py` 里的 `HyperLogLog` 用 `bytearray` 存 `2^p` 个一字节寄存器,精度限制在 `[12, 20]`、默认 14;`add_hash` 取哈希的**低 p 位**当桶号、剩余位用来数前导零;`merge` 是逐桶取最大;估计用 `alpha · m² / Σ2^(-r)`,并在估计值小于 `2.5m` 时切到 **linear counting**(`m · ln(m / 空桶数)`)。那个小基数修正让 mini-clickhouse 比真实 ClickHouse **在小基数上更准**,所以 §12.6 里“2 个不同值返回 3”的现象在这里**通常不会复现**,`uniq` 与 `uniqExact` 要等到基数**数千**之后才见得着分歧。

`uniqCombined` 在这里是**真的**“先精确后近似”:阈值 `EXACT_THRESHOLD = 1000`,超过才把键集合喂进 HLL 并切换模式,合并时还要处理“一边精确一边近似”的情况 —— 它对应的是真实 ClickHouse 的 **`uniqCombined`**(官方口径:小基数先用数组、再用哈希表精确计数,大基数才切 HLL),**不是 `uniq`**。真实 `uniq` 走的是另一条路:**自适应采样**(adaptive sampling,小基数走精确路径、大基数才采样估计),与 `uniqCombined` 是本章 §12.6 并列区分的两种策略,不要混为一谈。

**没实现的**:`groupBitmap` 与 `groupBitmapAnd/Or/Xor`、`uniqTheta`、`uniqCombined64`、`minMap`/`maxMap`、`sumMapFiltered`、`retention`、`sequenceMatch`、`windowFunnel` 等 —— §12.6 里补的那些里,只有一部分能在 mini 上跑。`sequenceCount` 有实现,但假设行**按时间到达**。`-Resample` 只记住参数、**不做真正的时间轴分桶**;`-If` 允许省略条件列(真实 ClickHouse 要求第二个参数必须存在)。另外 §12.5 讲的有序聚合、两级哈希,mini 都没有:它只有单级哈希、也没有 `optimize_aggregation_in_order` 这条路径。

## 12.13 小结

- 聚合的本质是“**可合并的状态**”:`sum` 是一个数,`avg` 是 `(和, 计数)`,`uniq` 是**自适应采样**的摘要(不是 HyperLogLog —— HLL 属于 `uniqCombined`/`uniqHLL12`)。状态不可合并,`GROUP BY` 就无法并行,物化视图也无法增量维护。
- 两阶段聚合(部分聚合 → 归并)是并行的前提,也是分布式聚合与 `AggregatingMergeTree` 的共同骨架;`-State`/`-Merge` 是它在 SQL 表面的名字。但**归并级并非总会存在**:单线程非分布式下可以只有一个 `AggregatingTransform` 就地 finalize(§10.9)。
- 内存由**分组数 × 每份状态大小**决定。两级哈希在 `group_by_two_level_threshold`(10 万分组)或 `group_by_two_level_threshold_bytes`(50 MB)任一满足时启动;`max_rows_to_group_by` + `group_by_overflow_mode` 是硬上限,`max_bytes_before_external_group_by`(绝对阈值)与 `max_bytes_ratio_before_external_group_by`(相对内存的比例阈值)是落盘逃生门。
- 数据本来按分组键有序时,**有序聚合**(`optimize_aggregation_in_order`,**默认 0**)能把内存从"所有分组"降到"一个分组";配套的 `distributed_aggregation_memory_efficient`(默认 1)、`optimize_aggregators_of_group_by_keys`(默认 1)分别优化分布式归并阶段与分组键上的 `min`/`max`/`any`;`distributed_group_by_no_merge` 则用于"分组键 ⊆ 分片键"时省掉跨节点归并(用错会得到重复分组行)。
- `uniq` 是近似(自适应采样),**小基数下也可能不准**;要精确就 `uniqExact`。基数可能超过 `2^32` 用 `uniqCombined64`,要做草图的交并差用 `uniqTheta`。`groupBitmap(x)` 顶层返回的是**基数**、位图状态要用 `groupBitmapState`/`groupBitmapMerge`。这条"精确 vs 近似"的判断贯穿 `quantile` 一族。
- **当"去重"之外还要做集合运算时,近似函数一律不够用**——两个估计值求交没有意义,`uniqExact` 也给不出"是哪些值"。这时要用**位图**:列类型写作 `AggregateFunction(groupBitmap, UInt32)`,靠 `groupBitmapState` 造、`groupBitmapMerge` 结算、`bitmapCardinality` 取基数、`bitmapAnd`/`Or`/`Xor`/`Andnot` 做交并差(只要基数时用 `bitmapAndCardinality` 这类合体版本,省一次物化)。它只吃无符号整数、值域受 `UInt32` 限制、按**整体**参与运算,所以"集合以后要不要和别的集合运算"才是选它的判据;不需要集合运算就别用位图。它和 `uniqExact` 是**代价模型不同**而非优劣关系:位图对稠密值域便宜、对稀疏值域可能更贵。
- 组合器把一个基函数撑成几十个名字,`-State`/`-Merge` 最重要;此外还有 `-SimpleState`(状态即结果)、`-MergeState`(合并但不结算)、`-Map`(按 Map 的 key 聚合)、`-ArgMin`/`-ArgMax`(用另一参数的极值挑行)。`WITH TOTALS` 与 `group_by_use_nulls=0` 都用**类型默认值**填充,默认配置下只能靠 `grouping()` 区分超级聚合行。
- 能在 `WHERE` 里过滤的绝不放 `HAVING`。
