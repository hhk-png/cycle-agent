# 第 24 章 查询优化与执行计划

一条查询从 40 秒变成 400 毫秒,通常不是靠换机器换来的,而是靠改对了一个地方。难点在于:你怎么知道该改哪个地方?猜是没有用的 —— "加个索引""改成 `PREWHERE`""把排序键换掉"这些动作里,只有一个是对的,其余的组合会让你把一天浪费在无效的试错上。

这一章解决的就是这件事:**用 `EXPLAIN` 把"引擎打算怎么跑"读出来**。读完这一章,你应该能把"查询慢"这句话拆成一个具体的、可验证的判断,比如"主键只裁掉了 3% 的 granule""排序算子吃掉了全部内存""读取阶段只有一个 processor,所以只有一个核在干活"。

## 24.1 优化器是一条规则链,不是代价模型

ClickHouse 的优化器主要走**基于规则**的一侧(RBO),而不是基于代价的一侧(CBO)。差别不在实现细节,而在"它凭什么做决定":

- **基于代价(CBO)**:维护统计信息,对每种候选计划估算代价,挑最便宜的。PostgreSQL 是典型。它能处理"两张表先 JOIN 谁"这种没有确定答案的问题;代价是统计信息会过期,过期就选错计划。
- **基于规则(RBO)**:在源码里写死"遇到这种形状就这么改"。改写确定、可解释、不需要任何统计信息,所以也不需要 `ANALYZE`。

ClickHouse 的取舍很明确:**以规则为主,把代价敏感的那部分决定权尽量交还给用户**。最典型的是 JOIN 顺序 —— 默认情况下它不替你重排多表 JOIN,你自己决定谁在左谁在右(第 15 章)。但这里必须说精确:**它并非完全不碰 JOIN**。ClickHouse 有一个受 `query_plan_join_swap_table` 控制的能力(25.3 默认 `auto`,由规划器决定把哪一侧当作 build 表,即被读进内存、用于建哈希表的那一侧),它只对**带 `JOIN ON` 的 `ALL` 严格度**(`ALL` 是 JOIN 匹配严格度里**最宽松**的一档,允许同一连接键的多行彼此任意匹配)查询生效——也就是为这一张 JOIN 换左右边,而不是重排多张表之间的先后顺序。更完整的 join 顺序优化开关名叫 `query_plan_optimize_join_order_limit`,**但 25.3 上没有它** —— 把 25.3 `src/Core/Settings.cpp` 里全部 `query_plan_*` 声明列出来数一遍就能确认,里面只有 `query_plan_join_swap_table` 这一个和 JOIN 换边有关,没有任何重排序开关(本书早先在这里写的是"是否存在未确认",现在答案是**不存在**,属于基线之后的新版本)。所以**在 25.3 上,"多表之间的先后顺序"完全由你的写法决定**(§24.16 专门讲这套"谁来决定顺序"的机制,口径与这里一致)。但实践结论不变:别指望引擎替你选对左右表,大表放错边照样把哈希表撑爆。换来的好处是同一份 SQL 在任意机器、任意时刻得到同一个计划,排查线上问题时这比"理论最优计划"值钱。

代价也真实:规则是"局部"的,每条只看得懂自己关心的模式,一堆局部最优的改写拼起来不保证全局最优。所以会遇到"这个查询明明能更快"的情况。知道之后,排查思路就该变:别问"优化器为什么不够聪明",要问"我写的形状有没有落在某条规则的触发器上"。

还有一条边界要补:RBO 不是 ClickHouse 的全部。25.x 里它也在往 CBO 那一侧长出**列级统计信息**,只是这套东西在基线 25.3 上仍是**实验性、默认关闭**,离"ClickHouse 也有一个 CBO"还差得远 —— 见 §24.19。

## 24.2 先用三种 `EXPLAIN` 确认"我写的是什么"

排查的第一步不是看计划,而是确认输入。这三个 `EXPLAIN` 是三个不同阶段的快照。

**`EXPLAIN AST`** 回答"解析对了吗"。怀疑 SQL 被解析成了另一种意思时看它 —— 括号优先级、`AND`/`OR` 的结合、别名的绑定位置。它不需要表存在,连表名都不校验。

**`EXPLAIN SYNTAX`** 回答"优化器把 SQL 改写成什么了"。它输出规范化之后的查询文本:别名展开成原表达式(`ORDER BY day` 可能变成 `ORDER BY toDate(created_at) ASC`)、通配符展开成显式列名、常量折叠(`amount * (1 + 0.08)` 变成 `amount * 1.08`)、CTE 内联。写简写 SQL 又想确认"引擎看到的完整版本"时看它 —— 尤其是别名展开,因为"别名在 `WHERE` 里能不能用上主键"取决于它展开成什么。

**`EXPLAIN QUERY TREE`** 回答"新分析器的中间形态长什么样"。`SYNTAX` 是文本、看不出结构时看它。加 `dump_passes = 1` 会额外打印这棵树经过的每一个优化 pass(以及 pass 名),用来回答"是哪一步改写把这段变掉的" —— 这是新分析器才有的观测面。(同族的 `run_passes` 默认 1,意思是"打印前先跑完所有 pass";`passes` 控制跑几趟,默认 -1 即全跑。)

这三者与真正的计划之间有一条分界线:**它们都在 AST 这一层,而优化规则大多在计划这一层**。所以"`EXPLAIN SYNTAX` 里谓词没被下推"不等于"优化没生效"。(`EXPLAIN SYNTAX` 能看到多深的改写,由它自己的 `run_query_tree_passes` 选项控制——是否在打印前把查询树的 pass 也跑一遍;可靠做法始终是同时看 `EXPLAIN PLAN` 对照。)

## 24.3 `EXPLAIN PLAN`:定位"为什么没走索引"

`EXPLAIN` 单独写不带类型时,默认就是 `EXPLAIN PLAN`。它打印**逻辑计划**:一棵由步骤组成的树,从上往下看依次是最终投影、排序、聚合、过滤、读取。读它的方法是**从下往上**找 `ReadFromMergeTree` 那一段。下面这段输出**带上了 `indexes = 1`**(否则不会有 `Indexes:` 段):

```text
Expression ((Projection + Before ORDER BY))
  Aggregating
    Expression (Before GROUP BY)
      Filter (WHERE ...)
        ReadFromMergeTree (default.hits)
        Indexes:
          PrimaryKey
            Condition: (event_date in ['2025-01-05', '2025-01-05'])
            Parts: 1/1
            Granules: 4/30
```

三个信息量最大的地方:

**第一,读取步骤上有没有 `Indexes` 段。** 细节见 §24.9。`EXPLAIN PLAN` 的 `indexes` 选项**默认是 0**——不写它时根本不会打印 `Indexes` 段,要看主键/分区/跳数索引的裁剪数字就必须显式写 `EXPLAIN PLAN indexes = 1 SELECT ...`(25.3 官方文档明确其默认为 0)。所以排查时**不要**省略这个参数,否则你会把"裁掉了多少"误读成"没有可看的裁剪信息"。

**第二,过滤算子出现在什么位置。** 是 `WHERE` 还是 `PREWHERE`、有没有被下推进子查询,决定了"过滤发生在读完数据之后"还是"过滤帮你少读数据"。**这是"为什么没走索引"最直接的证据**:当 `ReadFromMergeTree` 没有 `Indexes` 段、或者 `Condition: true`、`Granules: N/N`,而筛选步骤挂在读取之上,引擎就是在告诉你"我裁不掉任何数据,只能全读出来再筛"。

**第三,步骤之间有没有你想要的那次改写。** `ORDER BY` 写了排序键前缀,计划里却仍有 `Sorting`,说明有序读没生效;`GROUP BY` 的键与排序键一致,`Aggregating` 却还是普通哈希聚合,说明有序聚合没生效。

另外几个参数:`header = 1` 附输出表头(排查"列在哪一步丢的"),`json = 1` 输出 JSON。`EXPLAIN` 不执行查询,零成本。

新分析器(在 25.3 已默认 `1`;**两个名字里,`allow_experimental_analyzer` 是声明里的正式名,`enable_analyzer` 是它在 24.8 加上的别名** —— 方向和直觉相反,详见 §24.17)给 `EXPLAIN PLAN` 补了 `actions = 1`(默认 0):它把每个逻辑步骤打印成一串**动作**,把一个算子拆成"读哪些列、调哪个函数"的逐步描述。而"是哪条优化规则把这一步改掉的"要问的是另一条命令 —— **`EXPLAIN QUERY TREE` 的 `dump_passes = 1`**(默认 0,在打印查询树之前额外把经过的每一个优化 pass 及其名字列出来;同族的 `run_passes` 默认 1,含义是"先跑完全部 pass 再打印",`passes` 控制跑几趟、默认 -1)。`EXPLAIN PLAN` 上还有个 `keep_logical_steps`,官方文档给的语义是"对 JOIN 保留逻辑步骤、不转换成物理 join 实现",而 25.3 的 EXPLAIN 文档并没有列出它,**该选项在 25.3 是否存在未确认**。这几个参数合起来,能让你从"结果计划对不对"深入到"哪一步改写出了问题"。

一个容易踩的坑:**对 `Distributed` 表跑 `EXPLAIN`,输出的只是发起节点上的计划。** 每个分片上的本地计划不会出现在里面,`ReadFromMergeTree` 那一段反映的是**某一个**分片的读取形态(而不是所有分片),`Indexes` 段的裁剪数字也只代表那一份。要看某个分片真实读到多少,得直接连到那个分片、对**本地表**跑 `EXPLAIN`(第 23 章)。

## 24.4 `EXPLAIN PIPELINE`:定位"为什么只有一个核在跑"

`EXPLAIN PLAN` 看"要做什么",`EXPLAIN PIPELINE` 看"用几个 processor、怎么连起来"。并行度就藏在流水线里,判断方法很朴素:**数 processor 上的 `× N` 倍数,再看有没有 `Resize`。**

输出的每一行是一个 processor,行尾的 `× N` 表示它被复制成几路并行——`× 8` 就是八个核在干这件事。如果从读取段到它后面**每一行都是 `× 1`**,那不管你机器几个核,这条查询都是**单核**在跑:常见原因是数据量太小不值得并行,或者 `optimize_read_in_order`(§24.6)把读取锁成了单路。

`Resize` 是"路数在这里变化"的显式标记,要看它出现在哪:

- `Resize` 出现在**读取之后**、把 `N 路 → 1 路` 收拢,通常是**正常**的——多路并行聚合最终要汇成一路输出。
- `Resize` 出现在**读取之前**(甚至紧挨着源),说明上游**根本没铺开**——该并行的地方没并行起来。这时优化方向是把并行度往上游推,而不是去调 `max_threads`。

**阻塞算子(`Sorting`、`MergingAggregated`、`Join`)的位置还有第二层含义**:它们必须先把输入吃全才能出结果,所以既是**首行延迟**的来源,也是**内存峰值**的所在(第 10 章 §10.7)。看 `PIPELINE` 时顺手定位这三个词,就能回答"这条查询是慢在读、慢在算,还是慢在等一个攒了半天的大算子"。

两个让输出好读的参数:`graph = 1` 输出 DOT 图——泳道一多,文本就对齐得看不清,交给图形工具;`compact = 1` 把**同类型**的 processor 折叠成一行(默认展开),中等复杂度的查询开它更容易一眼看出主干。完整读法(每类 processor 的含义、端口怎么连)在第 10 章 §10.8。

## 24.5 `EXPLAIN ESTIMATE`:先知道数据量有多大

`EXPLAIN ESTIMATE` 打印每一步的 `rows`(行数)/ `bytes`(字节数)/ `parts`(part 数)。价值是**先排除"这个查询本来就该慢"**:如果读取步骤就要处理 80 亿行,再怎么改写法都是徒劳的——该改的是数据模型、加预聚合,或换更粗的分区粒度。**这一步常常能提前结束整个排查流程**(§24.11 把它放在第一步)。

三个容易误读的点:

- **它不执行查询,数字来自元数据。** ClickHouse 没有 `ANALYZE` 那种统计信息,读取步骤的行数是从 part 元信息里读出来的,所以**单表**的 `rows` 接近精确;**涉及 JOIN、子查询或表达式过滤时就是估算**,别拿它当真值去对账。
- **`bytes` 是"磁盘上压缩后"的字节数**,不是解压后要处理的内存。所以它反映"要读多少 IO",不是"要占多少内存"——同一份数据压缩比不同,`bytes` 会差很多,拿它推内存峰值会错。
- **`parts` 是这一步要打开的 part 数。** 它和 `EXPLAIN PLAN` 里 `Indexes` 段的 `Parts: 已选/总数`(§24.9)对应,能提前看出分区裁剪有没有生效。

一句话:`ESTIMATE` 用来看**量级**,不用来看**精度**——它的用途是判断"问题是模型还是写法",而不是替你验证某个改写到底省了多少行。

## 24.6 关键优化规则:什么条件下生效,怎么验证它生效

规则本身不用背,要记住的是**触发条件**和**验证手段**。

**谓词下推(`enable_optimize_predicate_expression`,默认 1)。** 把外层 `WHERE` 的条件下推进子查询或 JOIN 的另一侧,前提是下推目标里存在同名可比较的列且语义不变(外连接里下推 `WHERE` 会改变语义,引擎不做)。验证:看 `EXPLAIN PLAN` 里筛选步骤有没有移到读取附近。

**`PREWHERE` 自动下推(`optimize_move_to_prewhere`,默认 1)。** 只对 `*MergeTree` 家族生效。引擎把 `WHERE` 里"只靠少数列就能判断"的条件**搬**到 `PREWHERE`,先只读这几列过滤,再按存活的行号读其余列 —— 对宽表来说少读的列就是少解压的数据。验证:`EXPLAIN PLAN` 里是否出现 `PREWHERE`。`optimize_move_to_prewhere_if_final` 默认为 0,带 `FINAL` 的查询默认不做这个下推。

**有序读(`optimize_read_in_order`,默认 1)。** 当 `ORDER BY` 是排序键的**前缀**,或者是排序键上的单调函数(`toStartOfHour(ts)` 之于 `ts`)时,引擎按排序键顺序读,省掉一次排序 —— 省下的不只是 `Sorting` 算子,还有它的内存。代价:会限制并行度,数据量大时可能反而比"并行读 + 排序"慢,这时可以 `SET optimize_read_in_order = 0` 对比。

**有序聚合(`optimize_aggregation_in_order`,默认 0)。** `GROUP BY` 的键与排序键一致时可以边读边聚合,不必为整个结果集维护哈希表 —— 这是"分组数极多"场景(第 12 章 §12.3)的救命稻草。**默认是 0,必须显式打开。** 代价同样落在并行度上。

**平凡 `count()`(`optimize_trivial_count_query`,默认 1)。** `SELECT count() FROM t`(没有任何 `WHERE`)直接读 part 元数据回答。**关键限制就是"没有 `WHERE`"** —— 一旦带上条件,元数据里没有"满足该条件的行数",优化立刻失效。很多人以为 `count()` 永远快,于是写出带条件的报表查询然后抱怨慢。验证:对比加/不加 `WHERE` 时 `SelectedRows` 的差别。

**`LIMIT` 下推。** `LIMIT` 被推到尽量靠下让上游提前停止,和有序读是组合技:`ORDER BY 排序键 LIMIT 10` 快到离谱,是因为按序读到第 10 行就可以停。`LIMIT BY` 与 `LIMIT` 是两件事,前者不阻止全表扫描(第 16 章)。

**投影选择(`optimize_use_projections`,默认 1)。** 表上定义了 projection 且它能回答这条查询时,引擎改读 projection,读取代价可能低一个数量级。没被选中的 projection 只占磁盘不产生收益,写完一定要用 `EXPLAIN` 确认它被选中。定义、回填与验证 projection 的完整方法见 §24.10。

**函数改写成子列(`optimize_functions_to_subcolumns`,默认 1)。** 对 `Map`/JSON 列,`SELECT m['a']` 改写成只读 `m.a` 子列,而不是"读出整个 map 再取键"。验证:读取步骤涉及的列是否只剩你真正要的那几个。

**JOIN 左右边交换(`query_plan_join_swap_table`,默认 `auto`)。** 这是"规则优化器并不完全不管 JOIN"的证据:对带 `JOIN ON` 的 `ALL` 严格度查询,规划器会决定把哪一侧当作 build 表(因此可能把左右交换)。它管的是**单张 JOIN 的 build 侧**,不重排多表 JOIN 的先后顺序。实践结论不变——**别依赖引擎替你选对左右表**,大表放错边照样把哈希表撑爆(第 15 章)。想确认这次查询有没有被换边,看 `EXPLAIN PLAN` 里 JOIN 两棵子树的先后是否和你写的一致。

## 24.7 优化规则目录:按改写目标分组

规则优化器是一堆**彼此独立**的开关,每个开关对应一条改写。§24.6 已经讲过最常撞上的那批(谓词下推、`PREWHERE`、有序读/有序聚合、平凡 `count()`、`LIMIT` 下推、projection、子列改写、JOIN 换边);这一节只补 §24.6 **没覆盖**的其余开关,并按"改写目标"分组——排查时按目标找开关,比背一张清单有用。默认值以 25.3 为准,个别条目若与你手上版本不符,以 `SELECT name, value, default FROM system.settings WHERE name LIKE 'optimize%'` 的实际输出为准。

**第一组,排序与去重——目标是省掉一次排序或一次哈希去重。**

- `optimize_redundant_functions_in_order_by`(默认 1):`ORDER BY` 里包在排序键上的**单调函数**被去掉,`ORDER BY toDate(ts)` 在 `ts` 已是键时退化成按 `ts` 排。
- `optimize_sorting_by_input_stream_properties`(默认 1):若输入流**本身已按目标顺序排好**(上游是有序读或已排序子查询),复用它、省掉这次 `Sorting`。它是"两个排序链起来时省掉一个"的关键。
- `optimize_distinct_in_order`(默认 1):`SELECT DISTINCT` 与 `ORDER BY` 前缀一致时,用**有序去重**替代哈希去重,省内存。
- `optimize_use_implicit_projections`(默认 1):允许引擎为 `ORDER BY` 临时构造**隐式投影**来处理排序。
- `optimize_duplicate_order_by_and_distinct`:**25.3 已废弃**,不再是可生效的开关(名字还在 `system.settings` 里能看到)。历史上它折叠 `ORDER BY a, a` 这类重复键,现在同类形状交给别的规则处理。

**第二组,聚合——目标是让聚合更便宜。**

- `optimize_group_by_function_keys`(默认 1):`GROUP BY f(a), a` 里能由同一函数推出的键被折叠,减少键的维数。
- `optimize_uniq_to_count`(默认 1):`uniq(x)` 在特定条件下(如 `x` 就是 `GROUP BY` 键)改写成更便宜的 `count()`,对含 `uniq` 的分组查询常有明显收益。
- `optimize_aggregators_of_group_by_keys`(默认 1):`GROUP BY a` 时 `any(a)` / `max(a)` 等退化成该组的键常量,省掉一次聚合。
- `optimize_arithmetic_operations_in_aggregate_functions`(默认 1):把聚合函数参数里的算术搬到外面,`SUM(x * 2)` 改写成 `SUM(x) * 2`、`AVG(x / 2)` 改写成 `AVG(x) / 2`,减少每行计算量。
- `optimize_trivial_approximate_count_query`(默认 0):对支持估算的存储(如 `EmbeddedRocksDB`)允许用**近似值**回答平凡计数;它和 §24.6 的 `optimize_trivial_count_query`(默认 1)是一对。**注:网上常见的 `optimize_trivial_aggregate_query` 这个名字在 25.3 并不存在,别照抄。**

**第三组,谓词与表达式化简——目标是减少每行计算、合并重复扫描。**

- `optimize_if_chain_to_multiif`(默认 **0**,需显式打开):把一串 `if(...)` 改写成 `multiIf(...)`。
- `optimize_or_like_chain`(默认 **0**,需显式打开):`col LIKE 'a%' OR col LIKE 'b%'` 这类同列 `OR LIKE` 链合并成一次多模式匹配,减少对同一列的重复扫描。

**第四组,跳数索引与操作行为。**

- `use_skip_indexes`(默认 1):是否允许使用跳数索引(第 11 章)。
- `use_skip_indexes_if_final`(默认 0):带 `FINAL` 时是否仍然用跳数索引——默认不用,因为 `FINAL` 的语义和跳数索引的裁剪假设有冲突。
- `optimize_throw_if_noop`(默认 0):`OPTIMIZE` 实际无事可做时直接抛 `THROW`,而不是空转一遍。

两条纪律。第一,**规则目录不能当清单背**——它的价值是排查时能快速回答"这条改写有没有开关、默认开还是关";§24.6 的机制加这里的四组,就是一份完整的"开关地图"。第二,规则是**局部**的:每条只看得懂自己关心的形状,`optimize_uniq_to_count` 不会因为你想让它生效就生效,它只在 `x` 满足条件时才动手。所以"我写了 `uniq` 它没改成 `count`"这类问题的答案,永远在**触发条件**里,不在开关上。

## 24.8 子句的执行顺序,与"为什么 `WHERE` 不能用聚合结果"

`SELECT` 的子句不是按你写的顺序执行的。完整的依赖链在第 07 章 §07.1,这里只重复和优化相关的三个位置:`PREWHERE` 在 `WHERE` 之前(所以能推到读取阶段的谓词要尽量放 `PREWHERE`);`WHERE` 在 `GROUP BY` 之前,所以它引用不了聚合结果,那是 `HAVING` 的事;`ORDER BY` 在 `SELECT` 之后,所以它可以引用 `SELECT` 里定义的别名,而 `WHERE` 不行。另外 `QUALIFY` 是窗口版的 `HAVING`,过滤的是窗口函数的结果,因此必须排在 `WINDOW` 之后(第 16 章、附录 A 的窗口函数速查)。

把这条顺序记住,很多"为什么报错"就自动解释了:`WHERE` 在 `GROUP BY` **之前**执行,那个时刻还没有"组"这个概念,所以 `WHERE count() > 10` 是非法查询(报 `ILLEGAL_AGGREGATION`)—— 过滤聚合结果属于 `GROUP BY` 之后,也就是 `HAVING`。

**这正是优化的核心杠杆**:能驱动主键裁剪和跳数索引的,只有 `WHERE`/`PREWHERE` 里的条件(以及分区键条件)。`HAVING` 是在你聚合完之后才扔掉结果,数据早就读进来了。所以正确做法永远是"能在 `WHERE` 里过滤的,绝不留给 `HAVING`":

```sql
-- 错误做法:全表聚合,再扔掉 99% 的组
SELECT domain, count() FROM hits GROUP BY domain HAVING count() > 100000;
-- 正确做法:先用 WHERE 把数据量压下来,或改成读预聚合表
SELECT domain, count() FROM hits WHERE event_date >= today() - 7 GROUP BY domain HAVING count() > 100000;
```

还有一个 ClickHouse 特有的点:`AS` 别名可以用在查询的**任何**位置,包括 `WHERE`(`SELECT a + b AS c ... WHERE c > 5` 合法,比标准 SQL 宽松),但它是把别名**展开成原表达式**之后才处理的。于是别名能不能用上索引,取决于展开后长什么样:

```sql
-- 错误做法:别名把主键列包进函数
SELECT toDate(ts) AS d, count() FROM hits WHERE d = '2025-01-05' GROUP BY d;
-- 正确做法:把可索引的列裸着写成区间
SELECT toDate(ts) AS d, count() FROM hits
WHERE ts >= '2025-01-05 00:00:00' AND ts < '2025-01-06 00:00:00' GROUP BY d;
```

第二条能裁掉绝大部分 granule,第一条大概率是 `Granules: N/N`。两条结果完全一样,运行时间可能差两个数量级 —— 这就是"优化器能干什么"与"你该怎么写"的分界线。

不过"大概率"这三个字要留着:ClickHouse 的键条件分析其实**能**沿单调函数链折叠,所以 `toDate(ts)` **有时**也用得上裁剪,并不是一律打掉。但这条路径**脆弱、依赖具体类型对与版本**:一旦链上的转换是**窄化**的——`Date` → `DateTime`、`Date32` → `Date`、`DateTime64` → `DateTime` / `Date`,以及各类收窄的整数 `CAST`——超出目标类型值域的那部分会**回绕**(默认 `date_time_overflow_behavior = Ignore`),于是"映射后有序、映射前却越界"的行会被错误地裁掉。所以引擎**故意**取消了这类窄化转换的单调性资格:判据按"(入参类型 → 结果类型)"**类型对**来判,`toDateTime` / `toDate` / `CAST` / `accurateCast` 各种拼法一视同仁。结论不变:别指望"用函数把列包起来"还能裁剪,稳妥写法始终是把原始列写成区间谓词(与第 36 章同一条口径)。

## 24.9 读懂 `EXPLAIN PLAN` 的 `Indexes` 段

这是判断"到底用没用上主键"的**唯一可靠证据**,值得单独花时间。它的典型形态是:

```text
ReadFromMergeTree (default.hits)
  Indexes:
    MinMax
      Condition: and((event_date in ['2025-01-05', '2025-01-05']), ...)
      Parts: 1/1
      Granules: 4/30
    Partition
      Condition: (toYYYYMM(event_date) in [202501, 202501])
      Parts: 1/30
      Granules: 30/30
    PrimaryKey
      Condition: and((event_date in ['2025-01-05', '2025-01-05']), (domain in ['d7', 'd7']))
      Parts: 1/1
      Granules: 4/30
    Skip
      Name: idx_url
      Description: bloom_filter GRANULARITY 1
      Condition: url = '/p7'
      Parts: 1/1
      Granules: 2/4
    Skip
      Name: idx_level
      Description: set GRANULARITY 4
      Condition: ...
      Parts: 1/1
      Granules: 6/30
```

四个字段各管一件事:

**`Condition`** 是"引擎从这个索引里读出了什么范围"。它可能是 `Condition: true`,表示没读到任何有效范围,等于没裁。这里出现的字段名就是真正被用作裁剪依据的列;`WHERE` 里写了三列而这里只出现一列,另外两列就是白写的 —— 它们仍会被逐行过滤,只是帮不上"少读数据"。

**`Parts: 已选/总数`** 反映**分区裁剪**(以及 part 级 min/max 裁剪)。`3/12` 说明 12 个 part 里只有 3 个要读。分区键设计得对不对,看这一行最快。

**`Granules: 已选/总数`** 反映 **granule 级裁剪**,也就是排序键到底有没有帮上忙。每个 granule 默认 8192 行(第 05 章)。`4/30` 意味着只读了 13% 的数据,`30/30` 就是全表扫描 —— 这是你最常要盯的一行。

**`Ranges`** 是命中的区间段数(有的版本还会打印 `Search Algorithm: ...` 之类的辅助行)。段数多本身不一定是坏事,但特别多时要注意 `.mrk` 的随机读次数会跟着涨,顺序 IO 的收益会被吃掉。

`Indexes` 段不止 `PrimaryKey` 和单个跳数索引。打开跳数索引后,`ReadFromMergeTree` 下会**逐块列出所有可能用上的裁剪来源**:

- **`MinMax`**:用 part 级的列 min/max 做粗裁,不依赖排序键。它有独立于 `PrimaryKey` 的一行。
- **`Partition`**:反映**分区键**裁剪。它的 `Granules` 常常是满的(`30/30`)—— 别误以为没生效,它是靠 `Parts` 那一行砍 part 的。
- **`PrimaryKey`**:稀疏主键,砍 granule。
- **`Skip`**:每个跳数索引一块,`Description` 里写清索引类型(`set` / `bloom_filter` / `minmax` / `ngrambf_v1` / `tokenbf_v1`)与 `GRANULARITY`。第 11 章 §11.4 起的各节讲了每类索引适合什么谓词。

所以**哪一块把 `Granules` 砍了下来,真正起作用的就是它**。`MinMax` 与 `Partition` 的 `Granules` 数字可能和 `PrimaryKey` 不一致,这是正常的:它们在**不同粒度**上裁剪(part 级 vs granule 级),最终读多少是这几层裁剪的**交集**。

**强制使用跳数索引。** 跳数索引默认"能帮上忙才用",选择性太差时引擎会跳过它。要验证某个索引到底有没有用,可以开 `force_data_skipping_indices = 'idx_url, idx_level'`:一旦查询的谓词无法用上列出的这些索引,引擎**直接抛错**,而不是悄悄退化。这是"用报错证明索引该生效却没生效"的手段,适合放进慢查询排查流程(§24.11),**不适合常开** —— 它会把原本能跑的查询变成硬失败。

读的顺序是固定的:**先看 `Granules` 比值决定要不要继续优化,再看 `Parts` 判断分区键有没有问题,最后回到 `Condition` 看是哪一列真的起了作用。** 如果 `Parts` 很理想而 `Granules` 是满的,问题在排序键的顺序上,不在分区键上。

## 24.10 projection:让引擎替你换一份排序

projection 是表**内部**额外维护的一份数据(通常换一套排序键或做预聚合),查询不改、优化器自动选择。它的机制在第 11 章 §11.12 讲了;这一节从**优化器 / `EXPLAIN` 的角度**讲怎么定义它、怎么确认它真的被选中。

```sql
ALTER TABLE hits ADD PROJECTION p_domain (
    SELECT domain, toStartOfHour(ts) AS h, count()
    GROUP BY domain, h
);
ALTER TABLE hits MATERIALIZE PROJECTION p_domain;
```

三件必须分清的事:

**其一,`ADD` 只改元数据,不改数据。** `ADD PROJECTION` 之后,**只有此后新写入的 part** 才带上这份投影,已存在的 part 没有。紧接着要跑 `MATERIALIZE PROJECTION p_domain`,把投影**回填**到已有 part(重写 part,是一次大 IO,可按分区/按 part 分批做)。没做这一步,查询会一部分走投影、一部分走主表——可能比不做还慢。撤掉回填数据用 `CLEAR PROJECTION p_domain`(只清数据、留定义),连定义一起删用 `DROP PROJECTION`。

**其二,它按 part 存,不按表存。** 投影的物理文件跟着每个 part 走,所以"投影有没有生效"是**逐 part** 的问题:`system.projections` 是投影的**定义**(每个 projection 一行,含 `name` / `type` / 所属表/库),`system.projection_parts` 是**每个 part 上的投影数据**(有多少活跃 part 真的带上了这份投影、各多大)。排查"投影加了但查询没变快",先看 `system.projection_parts` 里带投影的 part 数是否等于活跃 part 数——不等,就是没 `MATERIALIZE`。

**其三,它是有限制的。** projection 只服务**单表**查询:查询里一旦出现 JOIN,优化器不会用投影。带 `FINAL` 的查询在部分版本里也不能命中投影。所以它适合"单表、固定形状、重查询",不是万能加速层。

**怎么确认它被选中。** `optimize_use_projections`(默认 1)是总开关;命中时 `EXPLAIN PLAN` 里对应的读取步骤会从 `ReadFromMergeTree (default.hits)` 变成带投影名的读取(注释里出现所选 projection 的名字)。`force_optimize_projection`(默认 0)可以**强制**必须用投影、用不上就报错,用于验证"我期望它用,它到底用了没有";和 `force_data_skipping_indices` 一样,**不适合常开**。projection 也不免费:每写一行要维护多一份(写入放大)、占额外磁盘,且**没被任何查询命中的投影只占空间不产生收益**——建了就要用 `EXPLAIN` 盯着它被用上。

## 24.11 一份"查询为什么慢"的诊断流程

按下面的顺序走,每一步都可能提前结束流程 —— 这是它比"逐个试参数"快的原因。

**第一步,`EXPLAIN ESTIMATE`。** 看读取步骤要处理多少行、多少字节。如果量级本身就很大(比如要读 80 亿行),那就不是写法问题,该改数据模型:加预聚合、加 projection、改分区粒度。到这个结论就停,别再调参数。

**第二步,`EXPLAIN PLAN indexes = 1`。** 看 `ReadFromMergeTree` 那一段:`Indexes` 段是否存在、`Condition` 是不是 `true`、`Granules` 比值多少。裁得很好却仍然慢,说明瓶颈不在读取上,跳到第四步。

**第三步,没裁掉就问"为什么"。** 过滤用的列是不是排序键的前缀?条件是不是被函数或别名包住了?是不是 `!=`、`NOT IN`、`OR` 这类天生难裁剪的形状?分区键上的条件下推了吗(看 `Parts`)?找到原因就改 SQL 或改排序键,重新跑第二步验证 —— 不要一次改三处,否则分不清是哪一处起了作用。

**第四步,`EXPLAIN PIPELINE`。** 读得不多但跑得慢,问题在算。processor 只有一路就是单核在跑(常见原因:数据量太小不值得并行、`optimize_read_in_order` 锁住了并行度)。阻塞算子的位置决定内存峰值和首行延迟。

**第五步,回到运行时数据。** `EXPLAIN` 不执行查询,看不到真实的内存、线程和 IO。跑起来之后从 `system.query_log` 的 `ProfileEvents` 拿两个数:`SelectedMarks / 总 mark 数`(裁剪效率)和 `SelectedRows`(真正参与计算的行数)。第 27 章 §27.4 与 §27.17 讲了这个表和该盯的计数器;判断改动是否真的有效也该盯这两个数,而不是只看墙钟时间(第 28 章 §28.13)。

一个提醒:`EXPLAIN` 的输出会随表结构变化,而表结构会因后台合并而变化。同一张表昨天 `Parts: 12/12`,今天合并后变成 `1/1`,这不是优化生效了,是 part 数变了。

## 24.12 `EXPLAIN TABLE OVERRIDE` 与一条升级预警

**`EXPLAIN TABLE OVERRIDE`。** 前面讲的 `PLAN` / `PIPELINE` 都是"查询怎么跑",这条命令看的是"**这张表的定义合并之后长什么样**"。当你在**集群里、对一个 `Distributed`/`Replicated` 表**运行它,它输出本地视角下"这个表的最终结构"(列、引擎、设置、依赖),回答"我这条 `CREATE` / `ALTER` 到底落成了什么"。它尤其适合排查"`ON CLUSTER` 建表后各节点定义不一致""视图/表套了几层、最终展开成什么"这类问题——和 `SHOW CREATE TABLE` 相比,它给的是**解析/合并之后**的结果,而不是原始 DDL 文本。

```sql
EXPLAIN TABLE OVERRIDE SELECT * FROM hits;   -- 看 hits 在这台机器上的最终结构
```

**一条升级预警:更高版本上 `indexes = 1` 的输出会变。** §24.3 说 `EXPLAIN PLAN indexes = 1` 的默认值是 0、要显式写——这在基线 25.3 上是对的,**不必改**。但要提前记一笔:**在更高的版本里,单靠 `indexes = 1` 可能不再给出"干净"的读取计划**,因为新版本把主键/跳数索引的裁剪下推到了**读数据阶段**,于是 `EXPLAIN` 会去估那两条路径:

- `use_query_condition_cache`(查询条件缓存,§24.13),
- `use_skip_indexes_on_data_read`(在读取阶段应用跳数索引)。

这两条**不是同一批**的东西,写坑标的时候别并成一句。`use_query_condition_cache` 在 **25.3 上已经存在、默认 `0`**(25.3 的 `SettingsChangesHistory.cpp` 里它的条目是 `false -> false, "New setting."`),**25.4 起翻成 `1`**;而 `use_skip_indexes_on_data_read` 在 **25.3 上根本不存在** —— 25.3 的 `Settings.cpp` 里没有这个声明,`SET` 它会直接报未知设置。所以"同时把两个都关掉"这种做法,只在两个默认值都翻过来的**更晚版本**上才成立。

这条坑标要留的意思是:升级后发现同一张表的 `EXPLAIN PLAN indexes = 1` 结果忽然"变复杂/对不上",先想到**查询条件缓存**这一条;至于跳数索引下推到读阶段的那条,得先确认你的版本里到底有没有它(`SELECT name FROM system.settings WHERE name = 'use_skip_indexes_on_data_read'`,查不到就是没有)。

## 24.13 查询缓存:Query Cache 与 Query Condition Cache

前面所有优化手段都在"把这条查询跑得更快";缓存换了思路——**把上一次的结果直接还给这一次**。ClickHouse 有两层缓存,名字接近、作用完全不同,别混。

**其一,Query Cache——缓存"整条查询的结果"。** 它把一个 `SELECT` 的结果按查询文本(连同设置、上下文)缓存,下一次**完全相同的查询**直接命中、跳过执行。默认是**关**的,要在会话里打开:

```sql
SET use_query_cache = 1;                       -- 总开关,默认 0
SET enable_reads_from_query_cache = 1;         -- 允许读缓存
SET query_cache_min_query_runs = 1;            -- 同一查询至少跑过几次才值得缓存
SELECT count() FROM hits WHERE domain = 'd7';  -- 第一次执行并写入缓存
SELECT count() FROM hits WHERE domain = 'd7';  -- 命中,几乎瞬返
```

几个必须知道的行为:

- **缓存的是结果,不是中间状态。** 所以它只对"重复执行的同一条查询"有意义——BI 看板上反复刷新的同一条聚合、监控里周期性跑的同一段 SQL 是典型受益者;一次性探索查询用不上。
- **只缓存 `SELECT`**,写入与 DDL 不缓存。
- **含非确定性函数的查询默认不缓存。** 这是最有意思的一条:`now()`、`rand()`、`generateUUIDv4()` 这类函数**每次结果都可能不同**,把它们的输出缓存起来再还给下一次,等于返回了**错误**的数据(比如把上一分钟的 `now()` 当成这一分钟的)。所以默认 `query_cache_store_results_of_queries_with_nondeterministic_functions = 0`,含这类函数的查询不进缓存;除非你明确接受"用旧值",才把它设成 1。
- **`system` 表查询默认不缓存**(除非改 `query_cache_system_table_handling`),因为系统表内容一直在变。
- **缓存是服务端级、跨会话共享的**;"是否共享给别的用户"由 `query_cache_share_between_users` 控制——一个用户缓进去的结果,别的用户能否命中取决于这个开关。
- **容量由服务端配置的 `query_cache` 段限制**(`max_size_in_bytes`、`max_entries`、`max_entry_size_in_bytes`、`max_entry_size_in_rows`),超限按 LRU 淘汰;`query_cache_ttl`、`query_cache_max_size_in_bytes` 等是会话侧的上限。**结果集太大就不进缓存**(受 `max_entry_size_*` 限制)——这点常被忽略:你以为开了缓存,其实这条查询太大、根本没被缓存。

观察与清理:

```sql
SELECT * FROM system.query_cache;   -- 当前缓存了哪些条目、多大、命中次数
SYSTEM DROP QUERY CACHE;            -- 清空整张缓存
```

`system.query_cache` 看的是"缓存里有什么";某条查询实际有没有命中,看 `system.query_log` 里的 `ProfileEvents['QueryCacheHits']` / `QueryCacheMisses`。缓存会随底层数据变化而**变旧**:表被 `INSERT`、`OPTIMIZE`、mutation 改动后,相关条目可能继续返回旧结果(受 TTL/失效策略约束)——对"结果必须实时"的查询别开它,或配一个短的 `query_cache_ttl`。

**其二,Query Condition Cache——缓存"哪些 granule 满足这个条件"。** 它不缓存结果,缓存的是**过滤条件的判定**:对某个 `WHERE` 条件,记住"哪些 granule 整体满足、哪些整体不满足",于是**后续形状相同、只是参数不同的查询**可以跳过重新求值,直接在 granule 层面裁剪。25.3 里它由 `use_query_condition_cache` 控制:**该设置在 25.3 默认 `0`(关),从 25.4 起才改为默认 `1`(开)**;而且它要生效还需 `enable_analyzer = true`(新分析器,24.3 起默认开)。也就是说,在本书基线 25.3 上它是**关闭**的,别以为它在后台无声地替你裁数据。它也是 §24.12 那条升级预警里点名的开关之一。它和第 11 章的跳数索引目标一致(都是"少读数据"),区别是跳数索引是**建表时物化**的结构,而 Query Condition Cache 是**运行时**攒出来的缓存,不用建、但清空后需重新积累。

**顺带记一个比这个设置本身更有用的手段:怎么查"任意一个设置的默认值何时变过"。** 标准做法不是翻 changelog,而是查 `system.settings_changes`:

```sql
SELECT version, change.1 AS name, change.2 AS previous_value,
       change.3 AS new_value, change.4 AS reason
FROM system.settings_changes
ARRAY JOIN changes AS change
WHERE change.1 = 'use_query_condition_cache'
ORDER BY version DESC;
```

这张表的列是 `type`(`Enum8`,取值 `Session` / `Core` / `MergeTree`)、`version`(`String`)、`changes`(`Array(Tuple(name, previous_value, new_value, reason, compatibility_mode))`);其中 `compatibility_mode` 取 `Apply`(允许回滚到 `previous_value`)或 `Ignore`(禁止回滚该设置及其更早的改动)。它有两个必须一起记住的局限:**其一,它只覆盖 `compatibility` 机制引入之后的改动**——更老的设置若默认值从没变过,表里就没有它的记录;**其二,它不覆盖服务端配置**(`config.xml` 那一类,服务端设置只能查文档与配置模板)。这也正是本书里个别默认值仍只能标"未确认"的真正原因:不是查不到来源,而是那类设置根本不在可查的体系里。

```sql
SET use_query_condition_cache = 0;   -- 关掉它(排查/对照时)
```

一句话区分:Query Cache 缓存**结果**(命中即跳过执行),Query Condition Cache 缓存**裁剪信息**(命中即少读数据)。前者收益大、适用面窄(要求查询完全相同),后者收益小、适用面宽(条件相似即可)。两者对"结果必须实时"的场景都要谨慎——它们都可能在底层数据变后仍返回旧信息,**做对账/计费这类查询时应手动关闭**。

这里讲的是**机制与设置**;同一套缓存,第 28 章 §28.14 会从**调优视角**再讲一次——到底该不该开、命中率怎么读(`QueryCacheHits` / `QueryCacheMisses`)、什么情况下它反而是负担,两处对照着看。

## 24.14 读时有序:`optimize_read_in_order` 与一组 `read_in_order_*` 细调

§24.6 已经点过**有序读(`optimize_read_in_order`,默认 1)**:当查询的 `ORDER BY` 是排序键的**前缀**(或排序键上的单调函数)时,引擎不再把所有数据搬进内存排一遍,而是**按主键顺序读**、再对各 part 做多路归并。省下的不只是一个 `Sorting` 算子,还有它的内存峰值。完整的**生效判据**(前缀、方向一致、过滤能形成连续主键区间、不和聚合冲突)在第 16 章 §16.4,这里不重复;这一节补两件它那里没讲的事:设置层的**分工**,以及它**什么时候会悄悄失效或变慢**。

**先把一个名字说清。** 有人把这一族设置记成 `merge_tree_read_in_order`,但这个名字在 25.3 的 `system.settings` 里**查不到**(本书未在本机 ClickHouse 上复核,以你目标版本的 `SELECT name, value, default FROM system.settings WHERE name ILIKE '%read_in_order%'` 为准)。控制"读时有序"的**正式设置在查询层,叫 `optimize_read_in_order`** —— 它是**总开关**,只回答"这次到底走不走有序读"。

真正存在的,是一组**以 `read_in_order_` 开头的细调开关**,回答"走了有序读之后,这条路怎么执行",都是会话级设置:

- **`read_in_order_use_buffering`(默认 1)**:在归并之前加一层缓冲,让有序读**仍然能并行**。关掉它,有序读更接近"串行地一路读下去",并行度会掉 —— "有序读锁住单核"这个印象在某些配置下是真的。(它从哪一版引入、25.3 上默认是不是 1,本书未在本机 ClickHouse 上复核,以 `system.settings` 为准。)
- **`read_in_order_use_virtual_row`(默认 0)**:用一个**虚拟行**标记"某个 part 此刻没有能贡献结果的数据",从而跳过不可能有贡献的 part、限制预读窗口,主要用来压低"part 很多时有序读的峰值内存"。有资料说它后来会把默认值翻成 1,**该时间点未确认**;在 25.3 上按"默认关"处理,别默认它开着。
- **`read_in_order_two_level_merge_threshold`(默认 100)**:参与归并的 part 数超过这个阈值时先做一次**预归并**,把一堆小 part 先并成少数几路,再和主归并合流。(这个默认值 `100` 是否就适用于 25.3,本书**未确认**,以本机 `system.settings` 为准。)
- 更晚的版本还加了 `read_in_order_use_virtual_row_per_block` 与 `read_in_order_max_primary_key_ratio`(在**主键选择率太差时自动放弃**有序读、退回并行读)。**这两个在 25.3 上是否存在,本书未确认**——只知道它们的引入版本晚于基线,因而 25.3 上很可能没有;用不起时按"没有"处理,并在 `system.settings` 里确认。

所以分工是:**`optimize_read_in_order` 决定"要不要",`read_in_order_*` 决定"怎么要得更好"**。有序读"该生效却没生效"、或"生效了反而更慢",先查总开关与判据(第 16 章 §16.4),再动这一组细调。

**什么时候它不但不省,反而更慢。** 有序读的代价是**并行度**:它要求数据被顺序消费,和"多路并行扫描 + 事后排序"是两种不同的资源换法。数据量大、`LIMIT` 又不靠前时,并行读再排一次常常更快。判断只能靠实测:

```sql
-- 默认走有序读
SELECT domain, ts FROM hits ORDER BY ts LIMIT 1000;
-- 对照:关掉有序读,看是不是"并行读 + 排序"更快
SET optimize_read_in_order = 0;
SELECT domain, ts FROM hits ORDER BY ts LIMIT 1000;
```

还有一条**优先级**要记:有序读一旦成立,它会**压过**异步读 —— `allow_asynchronous_read_from_io_pool_for_merge_tree` 的异步预读在这种情况下被忽略。"我明明开了异步读,怎么还是同步读"的答案,往往就是有序读先接管了。

**怎么确认它生效。** `EXPLAIN PLAN` 的读取步骤会标出读取类型为 `ReadType: InOrder`(不同版本的字段写法**未确认**,以你手上版本为准);`EXPLAIN PIPELINE` 里出现的是 `MergeTreeInOrder` 而不是 `MergeTreeThread`,并且**看不到** `PartialSortingTransform` / `MergeSortingTransform` —— 那两个排序算子**整个消失了**,才是有序读真的省掉一次排序的证据。

孪生兄弟是有序聚合(`optimize_aggregation_in_order`,**默认 0**,§24.6):同样押在"数据按排序键有序"上,只是用于 `GROUP BY`,同样要**显式打开**、同样牺牲并行度。

## 24.15 runtime filter:把 build 侧的键下推到 probe 侧的扫描

先看哈希 JOIN 的默认流程:右表(build 侧)整体建哈希表,左表(probe 侧)逐行去查。问题在于,左表里**根本不可能匹配上的那些行,也照样被读出来、照样做了一次哈希探测**。在星型模型里,左表是事实表(几十亿行)、右表是维表(几百万行),绝大多数探测都是白费的。

**runtime filter(运行时过滤)要解决的就是这件事**:在构建哈希表的同时把它见过的 key 收集起来(通常做成一个 Bloom filter,键很少时用精确集合),再把这个"键集合"**下推到 probe 侧的扫描步**上 —— 扫描时就把不可能匹配的行丢掉,连哈希探测都省了。它等于给"大表探小表"这类查询补上了一次**只有运行时才知道的 `WHERE`**。

**什么时候会生成。** 它只在 build 侧已知、且能形成过滤集合的 join 形状上生成。如果 probe 侧估计本来就没几行(建过滤器的开销比省下的还多),引擎会**跳过**它 —— 这个"太小就不建"的门槛由 `join_runtime_filter_min_probe_rows` 控制(设成 0 表示永远建)。若过滤器的通过率太高(几乎没筛掉什么),它还会被临时**停用**,对应 `join_runtime_filter_pass_ratio_threshold_for_disabling`。过滤器的大小与形态由 `join_runtime_bloom_filter_bytes`(默认 512 KiB)、`join_runtime_bloom_filter_hash_functions`(默认 3)这类参数控制。

**收益**是双份的:CPU(少做探测)和内存(少往哈希表里塞匹配行),官方与社区给出的量级常在"约两倍"这一档 —— 但**它取决于数据分布,别当成保证**。

**版本前提(重要)。** 这个能力很新:控制它的开关 `enable_join_runtime_filters` **在 25.10 引入、当时默认 0,26.2 起才默认 1**。也就是说,**本书基线 25.3 上根本没有这个设置** —— 这一节和 §24.12 那条升级预警一样,是写给"以后升级"的人的。在 25.3 上你只能手工模拟(把右表的键物化成一个小表、或改用 `IN` 子查询),拿不到引擎的自动下推。

**风险与边界,三句话。** 第一,它的**正确性靠"宁可多留、不可错杀"**保证:Bloom filter 的假阳性只会让某些不匹配的行**多走一次真正的连接**,不会把本该匹配的行丢掉,所以结果不会错;但反过来,**它不是免费的** —— 建过滤器本身要开销,选择率差时这开销就白花了。第二,当 build 侧因为 `max_rows_in_join` / `join_overflow_mode = 'break'` 被**截断**时,过滤器只收集了"截断前看到的那部分键",此时它必须被当作**不存在**(fail-open),否则就会错杀 probe 侧那些键落在"没读到的那部分"里的行。第三,它和 **join 顺序**耦合:一旦规划器把 build / probe 两侧调了个个(见 §24.16),probe 侧换了人,过滤器可能**就不再生成** —— 所以"同样的 SQL,昨天 `EXPLAIN` 里有 `BuildRuntimeFilter`、今天没了",先别当 bug,先看 join 顺序是不是被重排了。

**怎么在 `EXPLAIN` 里看它。** 生成时,`EXPLAIN PLAN` 会多出一个 `BuildRuntimeFilter` 步骤,并出现形如 `Prewhere filter column: __filterContains(..., key)` 的过滤列(实际字段写法随版本可能不同,**未确认**)。这些输出只在带这个能力的版本上才有,25.3 看不到。

## 24.16 JOIN 顺序由谁决定:换边、重排序,与"右表放小的一侧"

第 15 章 §15.7 讲的是"**用哪个算法**连接",§24.1 也只提了一句 `query_plan_join_swap_table`。这里把"**谁来决定左右表的先后**"一次性讲清 —— 它是本章里唯一一处"优化器真的会替你动 JOIN"的地方。

**第一层,单张 JOIN 的换边(`query_plan_join_swap_table`,默认 `auto`)。** 对**带 `ON` 的 `ALL` 严格度**查询,规划器根据估算的行数,决定把**哪一侧当作 build 表**(因此可能把左右交换)。它只换这一张 JOIN 的边,**不重排多表之间的先后**。`false` 恢复"永不交换"的旧行为,`true` 强制交换。想确认这次有没有被换,看 `EXPLAIN PLAN` 里 JOIN 两棵子树的先后是否还和你写的一致。

**第二层,多表 JOIN 的重排序(`query_plan_optimize_join_order_limit`)。** 它控制"最多对多少张表做自动重排序":规划器替**两张表**决定谁在右,再扩展到**三张及以上**;把上限设成 `0` 会连同 `query_plan_join_swap_table` 的单表换边一起禁掉。**但这条对我们的基线不成立 —— 25.3 上根本没有这个设置。** 把 25.3 `src/Core/Settings.cpp` 里所有 `query_plan_*` 声明列出来数一遍就能确认:里面只有 `query_plan_join_swap_table`,没有 `query_plan_optimize_join_order_limit`(26 行名单里连名字相似的一个都没有)。本书早先在这里标的是"是否存在、默认值多少未确认";现在的答案是**不存在**,它属于基线之后的新版本 —— 所以**在 25.3 上,优化器能替你做的只有单表换边,多表之间的先后仍然是你写什么就是什么**。这一条恰好把下面的实践纪律衬得更硬 —— **在 25.3 上,你写什么顺序,它就执行什么顺序**(除开单表换边那一处例外)。想在更高的版本上确认"重排序到底有没有来、从哪一版来",别猜,用 §24.13 那条 `system.settings_changes` 按设置名查它的 `previous_value` / `new_value` 就行。

**为什么"右表放小的"这条老经验仍然要记。** 不是因为它比优化器聪明,而是因为**重排序没有可靠的输入**:ClickHouse 不维护传统统计信息,重排序主要靠存储引擎给的**行数估算**,估算缺失或不准时,它会把边换错。而且它**覆盖不全**:`ANY` / `ASOF` 等严格度不在单表换边的射程之内;基于统计信息的那一套(§24.19;在高版本里要 `allow_statistics_optimize`,而它在 25.3 上仍是实验性、默认关闭)还得先有统计才谈得上。结论就是那条实践纪律 —— **主动把行数少的那张表写在右边(build 侧)**:规划器不动手时不亏,动手时也不至于更差。真要让左边当驱动表,改写顺序用 `RIGHT JOIN`,或干脆换成半连接 / `IN`,别指望"为了可读性把大表放右边"还能全身而退。

一句话分工:**§15.7 讲"用哪个算法连",这一节讲"谁来决定谁在左、谁在右"**;两者都调不动时,答案通常还是回到"把写法改成引擎认识的形状"(§24.6)。

## 24.17 从旧 analyzer 到新 analyzer:一条开关,对照两条路径

§24.1 与 §24.3 反复提到"新分析器",这里把它单独讲清,因为**迁移期**新旧两条路径会同时存在于生产环境,而它们的 `EXPLAIN` 输出和某些改写行为并不一样。

**正式名与别名 —— 答案和直觉相反,而且本书早先写反过。** 控制它的设置有两个名字:**`allow_experimental_analyzer` 是源码里声明的那个**(25.3 的 `src/Core/Settings.cpp` 写作 `DECLARE(Bool, allow_experimental_analyzer, true, ...) ALIAS(enable_analyzer)`),**`enable_analyzer` 是它的别名**。时间点也有据可查:25.3 的 `SettingsChangesHistory.cpp` 在 **24.8** 那一块记着 `{"enable_analyzer", 1, 1, "Added an alias to a setting \`allow_experimental_analyzer\`."}` —— 措辞是"**给它加了一个别名**",不是"改名";而 `allow_experimental_analyzer` 自己 `false → true` 的翻转记在 **24.3**(理由 "Enable analyzer and planner by default.")。

所以正确的说法是:**新分析器自 24.3 起默认开启;24.8 给这个设置加了一个更好记的别名 `enable_analyzer`,真名仍是那个带 `allow_experimental_` 前缀的。** 两者等价、写哪个都能跑,但要知道谁是别名 —— `system.settings.alias_to` 那列会直接告诉你:`enable_analyzer` 行的 `alias_to` 指向 `allow_experimental_analyzer`。查法:`SELECT name, value, default, alias_to FROM system.settings WHERE name IN ('enable_analyzer', 'allow_experimental_analyzer')`。

**这不是孤例。** 25.3 上同一套命名反复出现:`enable_json_type` 是 `allow_experimental_json_type` 的别名,`enable_dynamic_type` 是 `allow_experimental_dynamic_type` 的别名,`enable_parallel_replicas` 是 `allow_experimental_parallel_reading_from_replicas` 的别名(第 10 章、第 23 章 §23.11)。**规律是:那批更好念的 `enable_*` 名字是后加的别名,`allow_experimental_*` 才是声明名。** 记反了不影响查询能不能跑,但会影响你判断"这个功能还算不算实验特性" —— 那件事现在由另一处回答(看 `system.settings` 里该设置的类型档位、以及文档徽标),不再由名字里的前缀回答。

**用一个开关对照两条路径。** 迁移期最实用的手段,是在同一条 SQL 上把开关翻两次、diff 两份 `EXPLAIN`:

```sql
SET enable_analyzer = 1;   -- 新分析器(24.3 起默认)
EXPLAIN PLAN actions = 1 SELECT domain, count() FROM hits GROUP BY domain;
SET enable_analyzer = 0;   -- 退回旧分析器
EXPLAIN PLAN SELECT domain, count() FROM hits GROUP BY domain;
```

两个提醒。其一,**这个对照有保质期**:25.3 上你可以自由地 0/1 切换,但更晚的版本会把旧分析器**冻结**(`enable_analyzer` 不再允许置 0)、最终**整体删掉**,到那时"翻开关看差异"就不再可行 —— **要做对照就得用旧版本**。其二,**别在集群上只改一部分节点**:新旧分析器改了 server 之间的协议,`enable_analyzer` 不一致的集群跑分布式查询会报 `NOT_FOUND_COLUMN_IN_BLOCK` 这类列不匹配的错 —— 迁移时让**所有**节点同为 1(或同为 0)。

**迁移期该观察什么。** 找"哪些查询的计划会变"靠 `system.query_log`:同一个 `normalized_query_hash` 在迁移前后的两个版本上做比对,先把范围缩到"确实变了"的查询,再一条条看(第 27 章)。`EXPLAIN` 输出的变化集中在三处:

- **`EXPLAIN PLAN actions = 1`** 是新分析器才补上的一层:它把每个逻辑步骤摊成"读哪些列、调哪个函数"的动作清单(§24.3);旧分析器下没有这一层。
- **`EXPLAIN QUERY TREE`(尤其配 `dump_passes = 1`)** 看的是新分析器的中间形态与逐 pass 演进(§24.2)—— 旧分析器下这条命令的语义是另一回事。
- **`EXPLAIN SYNTAX`** 反映"分析器把 SQL 展开成什么";新旧分析器在别名作用域、通配符展开、谓词位置上不同,同一句 SQL 的 `SYNTAX` 输出可能不一样。

**几条已知的行为差异,迁移时最容易当 bug 上报。** 旧分析器**先优化、后校验**,于是"非法但能被优化掉"的查询可能跑得动,而新分析器**先校验、再优化**,这类查询会直接报错(也因此不能用一条非法 `SELECT` 去 `CREATE VIEW`)。JOIN `USING` 配合 `ALIAS` / `MATERIALIZED` 列再加 `*` 时,新分析器默认会把这些列一并带出。公共类型推导被统一(`LowCardinality(T)` 与 `T` 相与得 `T`,`Nullable(T)` 与 `T` 相与得 `Nullable(T)`),而且类型在分析期就推定,`if()` 两个分支**必须**有公共超类型(靠短路的写法可能因此报错)。投影的列名会保留别名,而不是被替换掉。

**给迁移期的几条兼容开关**(用来临时找回某些旧行为,不是长期方案):`analyzer_compatibility_join_using_top_level_identifier`(恢复旧的 `USING` 键解析)、`analyzer_compatibility_allow_non_aggregate_in_having`(找回旧分析器对 `HAVING` 里非聚合条件的改写)、`analyzer_compatibility_allow_cte_redefinition`(允许 CTE 重定义)。它们都是**已文档化**的设置,但名字多少、各自默认值会随版本增减,所以**别照抄本文这一份**:以你目标版本 `system.settings` 里 `name LIKE 'analyzer%'` 的实际清单为准;想查某个具体是从哪一版引入、改过什么,用 §24.13 的 `system.settings_changes`(它们都是会话设置,在它的覆盖范围内)。

## 24.18 与 mini-clickhouse 的对照

优化器在 `minich/execution/optimizer.py`,`EXPLAIN` 在 `minich/execution/explain.py`;先读代码再读这一节。

**对齐点**(mini 真的做出来的):`optimize_select()` 是一条规则链,四条规则对应真实设置——`trivial_count_query`(`_is_trivial_count()`)、`move_to_prewhere`(`_split_for_prewhere()`)、`limit_push_down`、`read_in_order`。其中 `trivial_count_query` **真的生效**(20 万行的表上 `SELECT count()` 几乎瞬返,读的是 part 的 `count.txt`,不是数据);`move_to_prewhere` 会**就地改写** `SelectQuery` 的 `prewhere`/`where`;`optimize_select()` 也**已经接线到解释器**:`minich/execution/interpreter.py` 在 `_try_trivial_count()`(读数据前回答平凡计数)与 `_run_optimizer()`(读取前跑规则链、结果记进 `self.optimization`,`EXPLAIN` 会打印)两处调用它。`EXPLAIN AST` / `PLAN` / `PIPELINE` / `ESTIMATE` / `SYNTAX` 都能输出。

**简化点**(mini 代替不了的):

- `limit_push_down` 与 `read_in_order` 的结果只被记进 `self.optimization` 并由 `EXPLAIN` 展示,**没有铺到最下面**——实测 `SELECT id FROM k LIMIT 10` 仍读完所有 mark。规则元信息表 `OPTIMIZATION_RULES` 里那行 `aggregation_in_order` 更只是文档:`optimize_select` 里没有对应分支,字段从未被赋值。
- `EXPLAIN PLAN` 的 `Indexes` 段它**没有**(`KeyCondition.describe()` 存在但没被 `explain.py` 调用),想知道裁剪效果只能读 `ProfileEvents`。
- `EXPLAIN SYNTAX` 走的是"把解析结果原样打印"这条路,**根本不调用优化器**,所以输出与输入一模一样(只在末尾补一行注释)——差异的根因不是"它不碰语法树",而是"它把 `SYNTAX` 定义成了输入回显"。`EXPLAIN QUERY TREE` 与 `EXPLAIN PLAN` 走同一分支,输出相同;`EXPLAIN ESTIMATE` 给的是**精确值**(直接读 part 的 `count.txt`),它没有统计信息这回事。
- **它是单线程分段执行**:聚合和排序这类阻塞算子会物化中间结果,`EXPLAIN PIPELINE` 于是被切成 Pipeline 0/1/2/3 四段、每行都是 `× 1`,看不到任何并行。**不要拿它的 `EXPLAIN PIPELINE` 推断真实 ClickHouse 的并行行为。**

照 mini 学"优化器怎么写",记住:真实引擎里"规则怎么写""有没有接线""结果有没有传到执行层"是**三件独立的事**,少掉任何一件,规则就只是文档。

## 24.19 统计信息与代价模型:ClickHouse 补上的另一半

§24.1 把 ClickHouse 的优化器定成"以规则为主",并且在很长一段时间里这就是**全部**的答案:没有 `ANALYZE`、没有直方图、JOIN 顺序只能靠存储引擎给的行数估算。但规则优化器有一类问题天生答不了 —— "两张表先连谁"没有确定答案,只能靠代价。所以 24.x 起 ClickHouse 也在往 CBO 那一侧补一块:**列级统计信息(column statistics)**。这一节把它讲清,并点明它在本书基线 25.3 上的**真实地位:实验性、默认关闭**。

**它是什么。** 统计信息是**按 part 存**的、关于某一列的摘要,当前支持的类型有 `minmax`(最小/最大值)、`uniq`(不同值个数,即 NDV,也叫 `uniq_v2`)、`tdigest`(近似分位数)。规划器用它们在**不读数据**的前提下估算"这个过滤条件能留下多少行",进而判断 JOIN 该把哪一侧当 build 表、以及在 §24.16 那套重排序里怎么排。注意它和 §24.10 的 projection、第 11 章的跳数索引**不是一回事**:跳数索引是"为了少读数据"而建的裁剪结构,服务的是**执行**;统计信息是"为了估行数"而建的代价输入,服务的是**规划**。前者让查询跑得更快,后者让规划器选得更对 —— 两者常常要一起用,但不能互相替代。

**怎么建。** 统计信息只在 `*MergeTree` 家族上支持,用 `ALTER` 管理(确切语法以你版本的 `ALTER ... STATISTICS` 文档为准):

```sql
ALTER TABLE hits ADD STATISTICS domain TYPE minmax, uniq;   -- 只改元数据
ALTER TABLE hits MATERIALIZE STATISTICS domain;             -- 回填到已有 part(一次 mutation)
ALTER TABLE hits CLEAR STATISTICS domain;                   -- 只删统计数据、留定义(可再 MATERIALIZE)
ALTER TABLE hits DROP STATISTICS domain;                    -- 连定义一起删
```

和 projection 一样,**`ADD` 只改元数据、`MATERIALIZE` 才回填**(§24.10 那条纪律在这里同样成立):`ADD` 之后只有此后新写入的 part 带统计,要覆盖历史数据必须 `MATERIALIZE`。

**它生效的前提,是闸门打开。** 建了统计不等于用得上:还要让规划器真的去用它。这里有两个开关 —— `allow_experimental_statistics`(建/改统计的闸门)与 `allow_statistics_optimize`(让优化器**使用**统计)。**在本书基线 25.3 上,这两个都是实验性的、默认关闭**(`allow_statistics_optimize` 的文档记录是 24.6 从 `allow_statistic_optimize` 改名而来、当时默认 `0`;它默认翻成 `1` 是**更晚**的事,具体版本本书**未确认**)。所以**在 25.3 上默认既没有统计、也没有基于统计的规划**:这一节和 §24.12、§24.15 一样,读它是为了知道"升级后会多出来什么",而不是"现在就能靠它调优"。

**统计信息补上了什么、又没补什么。** 补上的是"估行数"这个长期缺口:有了 NDV 与分位数,§24.16 说的"重排序没有可靠输入"就有了一部分输入,多表重排序因而更靠得住。没补上的有三点。其一,**统计是近似且会过期**:`tdigest` / `uniq` 都是采样估计,数据变了统计不会自动跟着变,得重跑 `MATERIALIZE`(或等后台在合并时更新)—— 这和 PostgreSQL 那边"统计过期就选错计划"的老问题同源。其二,**它只喂规划、不改执行**:统计再准也只能让规划器"选得对一点",选完之后查询怎么跑、跑多快,仍取决于排序键、跳数索引、projection 这些真正碰数据的结构(第 11 章、§24.10)。其三,**覆盖范围有限**:规划器只在少数几类决策(JOIN 重排序、部分聚合优化)上消费统计,其余改写仍走规则。所以结论回到 §24.1:**统计信息是"让规则优化器的盲区小一点",不是"把 ClickHouse 换成一个 CBO"**。这也解释了为什么 §24.16 那条"主动把小的表写在右边"的老经验在 25.3 上依然成立 —— 在统计信息默认关闭的版本里,它仍然是最可靠的输入。

**怎么确认它到底有没有在起作用。** 先确认闸门开着:`SELECT name, value, default FROM system.settings WHERE name LIKE '%statistics%'`。再照 §24.11 的流程看变化:计划里 JOIN 的 build 侧有没有被重排、`query_log` 里 `SelectedRows` 有没有下降。至于"某条查询的哪个规划决策用了统计",要看的是规划阶段的调试输出而非 `EXPLAIN PLAN` 本身 —— 该观测面在 25.3 上是否存在,**未确认**,以你目标版本的文档为准。

## 24.20 小结

ClickHouse 的优化以规则为主,好处是确定、可解释、不需要统计信息,代价是可能错过更优计划,并且"用户的写法"直接决定规则能不能触发。它不是完全不碰 JOIN(受 `query_plan_join_swap_table` 约束,会为单张 JOIN 换 build 侧),但结论仍是别依赖引擎替你选左右表。§24.6 讲机制的触发条件与验证手段,§24.7 按"排序去重 / 聚合 / 谓词化简 / 跳数索引"四组列出其余开关——两者合起来是一份完整的"开关地图"。

§24.14 起补上了 25.x 优化器里几项新能力,也是本章原先的空白:读时有序的两层开关(§24.14)、把 JOIN 过滤条件下推到扫描侧的 runtime filter(§24.15)、"谁来决定 JOIN 左右顺序"(§24.16),从旧 analyzer 迁移到新 analyzer 时怎么用一条开关对照两条路径(§24.17),以及 ClickHouse 往 CBO 一侧补的列级统计信息(§24.19)。要留意口径:runtime filter 这个设置在 25.3 上**还不存在**;列级统计信息与 `allow_statistics_optimize` 在 25.3 上是**实验性、默认关闭**;`query_plan_optimize_join_order_limit` 这个"多表重排序"开关在 25.3 上**根本不存在**(§24.1 与 §24.16 都给了同一条结论,依据是 25.3 `src/Core/Settings.cpp` 里全部 `query_plan_*` 声明里没有它);read_in_order 那两个"虚拟行 / 自动放弃"细调、以及新分析器后续把旧分析器冻结并删除的行为,也都是 25.3 之后才有的 —— 这几节更适合当"升级预告"读。

七种 `EXPLAIN` 各有分工:`AST` 确认解析、`SYNTAX` 看 AST 层改写、`QUERY TREE` 看新分析器的中间形态(加 `dump_passes = 1` 看 pass 演进)、`PLAN` 定位"为什么没走索引"(`indexes = 1` 与 `actions = 1` 都要显式写,它们默认都是 0)、`PIPELINE` 定位"为什么只有一个核在跑"、`ESTIMATE` 先确认数据量是否本来就大、`TABLE OVERRIDE` 看"这张表合并后的最终定义"(§24.12)。

三个值得记住的具体事实:平凡 `count()` 只要有 `WHERE` 就不生效;`optimize_aggregation_in_order` 的默认值是 0,不是 1;判断索引有没有用上,唯一可靠的证据是 `ReadFromMergeTree` 下面 `Indexes` 段里的 `Granules: 已选/总数` 比值(那一段会按 `MinMax` / `Partition` / `PrimaryKey` / `Skip` 逐块列出)。

缓存是另一条路(§24.13):**Query Cache 缓存结果**(`use_query_cache` 默认 0,含非确定性函数的查询默认不缓存),**Query Condition Cache 缓存裁剪信息**(`use_query_condition_cache`)。两者都对"结果必须实时"的场景要谨慎。另外记一条升级预警:更高版本上要做 `indexes = 1` 的对照,得同时关掉 `use_query_condition_cache` 与 `use_skip_indexes_on_data_read`,25.3 不受影响。

诊断慢查询不要在参数里乱试。按 §24.11 的顺序走:先 `ESTIMATE` 判断是不是模型问题,再 `PLAN indexes = 1` 判断读取有没有裁剪,再 `PIPELINE` 判断计算有没有并行,最后回到 `query_log` 的 `ProfileEvents` 验证改动真的有效。做完这些,你手里应该有一个可验证的判断,而不是一句"感觉快了一点"。
