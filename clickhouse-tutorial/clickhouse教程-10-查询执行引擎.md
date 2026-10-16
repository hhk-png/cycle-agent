# 第 10 章 查询执行引擎

同一条 `SELECT`,在 MySQL 上跑一分钟,在 ClickHouse 上跑两百毫秒,很多人第一反应是"列存快"。列存确实快,但它解释不了另一件事:把 ClickHouse 的 CPU 监控打开,你会看到查询期间**每个核都在满载**;而在一台 64 核的机器上跑一个 `GROUP BY`,MySQL 大概率只用了一个核。这个差别不在存储格式上,在执行模型上。

这一章要讲清 ClickHouse 的执行模型:一条 SQL 怎么从文本变成一张 processor 图,这张图为什么天然能并行、能背压、能提前停,以及它和教科书里的火山模型到底差在哪。这些不是实现细节 —— 当你的查询只用了一个核,或者内存爆掉、第一行结果迟迟出不来的时候,答案都在这一章里。

## 10.1 从 SQL 到结果的完整链路

一条 `SELECT` 到达服务端后,依次经过四层:

```text
SQL 文本
  │  ① Parser           词法/语法分析 → AST
  ▼
AST
  │  ② Interpreter / Planner   语义分析、改写、生成逻辑计划
  ▼
QueryPlan            逻辑计划:有哪些步骤、谁先谁后(树)
  │  ③ buildQueryPipeline()    把逻辑步骤落到具体的 processor 上
  ▼
QueryPipeline        物理流水线:一张 processor 图 + 端口连接
  │  ④ PipelineExecutor        按拓扑序驱动每个 processor 的 work()
  ▼
结果块 → 序列化 → 客户端
```

`QueryPlan` 与 `QueryPipeline` 的分工值得记住:**前者回答"要做什么",后者回答"用哪些算子、几条并行、谁接到谁"**。同一棵逻辑计划树,在不同的并行度、不同的表结构下会生成完全不同的流水线 —— `EXPLAIN PLAN` 看前者,`EXPLAIN PIPELINE` 看后者。

这里要顺带定位一个常被误放进这张图的机制:**查询结果缓存(query cache)不在这条执行链上**。它工作在更外层 —— 一条语句先被拆解、规范化、算出一个缓存键,命中就直接把上次存下的结果块返回,压根不会走到上面的 Parser/Planner(更不会建流水线);未命中才把语句交给这条链执行,算完再把结果写回缓存。所以你在 `EXPLAIN PIPELINE` 里**永远看不到缓存这一步**:它属于"这条语句要不要执行"的前置判断,而不是"这条语句怎么执行"的一环。

这里还要补一个上图画不进去、但读源码时一定会撞到的中间概念:**`Pipe`**。第 ③ 步"buildQueryPipeline"并不是一口气拼出一张图,而是先拼出若干个 `Pipe`。一个 `Pipe` 是**一组线性相连的 processor**(自带 source 与 sink 端口),比如"读 part → 过滤 → 表达式求值"就是一段 pipe。各个构建器(读 MergeTree、聚合、排序……)各自产出或消费 pipe,再靠 `Pipe::unitePipes` 之类的操作把它们首尾接起来。直到最后 `QueryPipeline` 才把这些 pipe **展开成一张真正的 DAG**:在这一步才插入 `ResizeProcessor`(做扇入/扇出)、才决定每条泳道实例化几份、才把延迟端口接上。

为什么要分这两层?因为 `Pipe` 是**逻辑**的一段(还不关心并行度),`QueryPipeline` 是**物理**的整图(并行度、端口类型都定死了)。所以 `EXPLAIN PIPELINE` 里的 `Pipeline 0 / Pipeline 1` 那种分段,本质上就是 pipe 的边界;而"同一个算子实例化几份"这种信息,只有展开成 `QueryPipeline` 之后才存在。

历史上 ClickHouse 并没有这两层。老的 `InterpreterSelectQuery` 直接在解释阶段就把 processors 拼起来了,逻辑计划与物理流水线糊在一起,结果是优化规则无处安放、`EXPLAIN` 也只能打印 implementation 细节。21.9 前后引入的 `QueryPlan` + 新分析器是一次大重构,把这两件事拆开了;新分析器自 24.x 起逐步成为默认(本书对标的 25.3 上它已是默认,但仍可用 `enable_analyzer = 0` 退回老路径)。它真正**完全接管**是晚得多的事:**26.9** 把这个开关冻结为 `1`(不再允许关闭),**26.10** 干脆删掉了老的分析路径。今天非 SELECT 语句(DDL、系统命令)仍然走老的 Interpreter 路径。

## 10.2 推送与拉取:为什么不是火山模型

教科书里的执行模型是**火山模型**(Volcano / Iterator Model),PostgreSQL、MySQL 用的都是它:

```text
上层算子:  while ((row = child->next()) != nullptr) { 处理一行; }
```

每次调用 `next()` 返回**一行**,一个查询就是**一条调用栈**:`Limit → Sort → GroupAgg → Filter → TableScan`,`next()` 从栈顶一路递归到栈底。它简单、优雅、天然支持流式输出(第一行很快),但有两个固有问题:每次函数调用只处理一行,虚函数调用的开销远大于实际计算;而且整条调用栈跑在一个线程上,并行化需要额外的框架。

ClickHouse 换了一套:**处理器图 + 按块传递**。

```text
每个 processor 实现一个 work() 方法
   work() 从输入端口取一个 Chunk,算完,推到输出端口
   返回四个状态之一:NeedData / PortFull / Finished / Ready
```

三个差异点:

第一,**单位是块(Chunk)不是行**。一次 `work()` 处理的是 8192 行或者几万行的一整列,所有循环都在列上跑,天然可以向量化 —— 编译器甚至能把 `a + b` 编译成 SIMD 指令,一次算 8 个值。

第二,**数据流不是调用栈,是一个图**。上游把块推进端口,下游从端口取块,两者之间没有"谁调用谁"的关系。于是同一条逻辑计划可以实例化成 N 条互相独立的"泳道(lane)",每条泳道跑在自己线程上 —— 这就是算子级并行的来源。

第三,**背压是端口的物理属性,不是靠限流算法**。端口一次只能容纳一个块。下游没取走,上游的 `push` 就返回 `PortFull`,上游的 `work()` 就必须返回 `PortFull` 而不是继续读。整条链上的内存占用因此有个上界:核数 × 每条泳道上的块数 × 块大小。相比之下,火山模型里"上游是全量的迭代器,下游只要不调 `next()` 上游就不跑",天然有背压;而一个实现得不好的流水线(比如上游自己开了个线程池狂读)会直接把内存打爆。

由此得到四个实打实的好处,它们都能在 `EXPLAIN PIPELINE` 上看出来:

- **向量化**:一次算一列,而不是一行八次虚函数调用;
- **背压**:端口满就停,内存不会无限增长;
- **算子级并行**:同一种 processor 实例化多份,各跑一条泳道;
- **`LIMIT` 的反向传播**:`LimitTransform` 凑够行数后,会给自己的输入端口发"结束"信号,上游收到 `PushResult::NotNeeded` 就不再产出 —— 于是 `SELECT * FROM huge_table LIMIT 10` 只读前几个 granule 就返回了。这也是唯一一处"下游反向影响上游"的地方,在纯火山模型里它靠的是抛异常/提前 return,不太好读。

## 10.3 Chunk 与 Block 的区别

这两个词在 ClickHouse 里指的是不同的东西,而它们在别处常常被混着用。

`Block` 原本就是"一批列",它既能当数据容器,也能当**表头**使用 —— 表头就是"把数据清空的 Block",只保留了列名与类型。

`Chunk` 是 2019 年前后引入的,它的定义是:**一个表头(Block)+ 一批真正的数据列 + 行数**。

```text
Chunk
 ├── header: Block        ← 列名、类型(列是空的)
 ├── columns: [IColumn]   ← 真正的数据
 └── num_rows
```

为什么要拆成两个?因为**表头要被复用,数据要被替换**。一个 100 GB 的表,读出来可能是上百万个 Chunk,但它们的表头**完全相同**。如果每个 Chunk 都自己带一份"这一列叫 id、类型是 UInt64"的元数据,内存里会多出一大堆重复的字符串与类型对象。拆开之后,同一个 `Block` 可以被上百万个 Chunk 共享。

另一个好处是校验:接收方拿到 Chunk 的第一件事就是拿 header 比对"列数、列名、类型对不对"。类型不匹配的 Chunk 流下去,轻则结果错误,重则崩溃 —— 所以真实 ClickHouse 在这里是直接抛 `LOGICAL_ERROR` 的。这个校验之所以能便宜到"随便做",正是因为比的是共享的表头对象,而不是逐行逐列。

需要提醒一句:如果你读 2020 年以前的博客,会看到大量"Block 就是一批数据"的说法。那在当年是对的,现在过时了 —— 现在引擎内部流动的单位是 Chunk,Block 退化成表头。

## 10.4 数据处理单位:max_block_size 与 index_granularity

有两个数字贯穿整个读写路径,它们经常被混为一谈(下文还会补上第三个 —— 只作用在内存侧的字节预算 `preferred_block_size_bytes`):

`max_block_size`,默认 **65409**。它是**内存里**一次处理的行数 —— 一个 Chunk 最多这么多行。它不决定磁盘布局,只影响算子一次处理多少数据:调大则单次调用开销摊薄(cache 命中更好),但内存峰值升高、第一行结果的延迟变大。

`index_granularity`,默认 **8192**。它是**磁盘上**的最小读取单位 —— 对应 `primary.idx` 里的一个 mark。它决定主键能定位到多细的粒度:你最后总是要**整 granule**地读,哪怕只需要其中一行。但要当心说成"一个 granule 就是 8192 行":8192 只是 granule 的**行数上限**,默认开启的自适应粒度还会让它在**字节数**先到顶时提前封口(`index_granularity_bytes`,默认 10 MiB),于是宽行表上一个 granule 可能远不到 8192 行。完整的口径在第 05 章 §05.7 与第 11 章 §11.2.1,这里只要记住"8192"是一个会被字节上限往下拉的数字。

两者的关系是"块由若干 granule 组成":一个 65409 行的块大约覆盖 8 个 granule。这不是巧合 —— 块的大小要比 granule 大一点,才不会频繁地在 granule 边界上切碎。

**上面只说了 `max_block_size` 这个"行数上限",但真实跑起来,一个块的行数常常远小于 65409。** 原因是读侧还有第二个、按**字节**计量的预算:**`preferred_block_size_bytes`,默认 1000000(约 1 MB)**。它自己的 docstring 把分工讲得很直白:这是对较粗的 `max_block_size` 的**细调**;当列很宽、按 `max_block_size` 行凑出来的块字节数远超这个值时,块会被**压低行数**("its size will be lowered"),为的是让每一块更贴合 CPU cache。两个上限于是构成"**行数到顶或字节到顶,谁先到用谁**"的关系 —— 这跟 §05.7 里 granule 的 `index_granularity` / `index_granularity_bytes` 是**完全同构**的一对,只是一个作用在磁盘上的 granule、一个作用在内存里的 Chunk。它也和前面那条经验对得上:窄行表上 1 MB 远到不了 65409 行,两个上限都用不上,块接近 65409 行;宽行表上一行几百字节,1 MB 可能只装得下几千行,于是块被 `preferred_block_size_bytes` 提前封口。**这正是"为什么块的行数远少于 `max_block_size`"的答案。**

所以看到 `EXPLAIN PIPELINE` 的输出或 trace 日志里"一个块只有几百/几千行"时,先别怀疑配置坏了:那是 `preferred_block_size_bytes` 在宽表上主动把块收小,是设计如此,不是 bug。想确认它在你实例上的取值,查 `system.settings`(`SELECT value FROM system.settings WHERE name = 'preferred_block_size_bytes'`);想更严格地按 cache 容量卡,还有一个默认关闭的 `preferred_max_column_in_block_size_bytes`(默认 **0**,即不限制),一旦设成正数,它会进一步把"单列在一个块里占的字节"也卡住。

关于 65409 这个数字,有一个必须讲清的事实:**它不是 2 的幂**。为什么不用整整齐齐的 65536?流传最广的解释是:2 的幂的行数会让每一列的跨行步长也接近 2 的幂,访问同一列相邻行时地址低位大量重复,容易映射到同一组 cache set,造成 cache 冲突缺失;而 65536 又是常见 SIMD 宽度的整数倍,尾循环的边界处理不理想。`65536 − 127 = 65409` 既不是 2 的幂,也不是任何常见向量的整数倍,而少 127 行(约 0.2%)对吞吐几乎没有影响。

**这个解释现在可以在上游源码里直接核对到了。** `src/Core/Defines.h` 里就是这两行:`static constexpr auto PADDING_FOR_SIMD = 64;` 与 `static constexpr auto DEFAULT_BLOCK_SIZE = 65409; /// 65536 - PADDING_FOR_SIMD - (PADDING_FOR_SIMD - 1) bytes padding that we usually have in arrays`——注释本身就把 `65409 = 65536 − 64 − 63` 这个算式写了出来,并且点明它是"我们在数组里常有的首尾 padding 字节数",与前面那句"为 SIMD 对齐留填充"的说法完全对得上。(**勘误**:本节早先写的是"出处仍未能亲自打开核对、官方文档没给解释"——前半句当时是因为取不到源码,现已直接对上源码注释;后半句仍然成立:官方**文档**确实没解释这个数,解释只在源码注释里。)可以确定的是这个默认值本身:

```sql
SELECT name, value FROM system.settings WHERE name = 'max_block_size';
-- 65409
```

## 10.5 processor 的四态状态机

每个 processor 的 `work()` 必须**尽快返回**,不能阻塞等待。它的返回值是一个四态枚举:

`NeedData` —— 我有输入端口,但上游没给数据,这一步什么也没做。执行器需要区分"我在等工作"和"我卡住了",所以必须单独一个状态。

`PortFull` —— 我想往下推,但下游的端口是满的。这是背压的载体。

`Finished` —— 我彻底干完了。

`Ready` —— 我刚做了一件有用的事(读了一块、算了一块、推了一块)。

执行器的主循环就是"反复轮询所有没结束的 processor,直到全部 `Finished`"。这个循环有一个必须遵守的约定:**输入结束必须传播成输出结束**。真实 ClickHouse 里最常见的挂死故障就出在这里。

错误做法 → 后果 → 正确做法:

```text
✗ 错误做法:processor 在处理完后只把自己标记为 finished,
           忘了给输出端口发 finish 信号。
→ 后果:下游永远等不到"输入结束",它的 work() 一直返回 NeedData;
        执行器认为"还有数据要来",整条查询永久挂起。
        客户端表现为查询一直不返回、也不报错 —— 你只能去 kill 它,
        而线索只有 CPU 占用掉到 0。
✓ 正确做法:把"输入结束 ⇒ 输出结束"这条规则写在基类里实现。
        真实 ClickHouse 的 ISimpleTransform 就是这么做的;
        任何自己实现的 processor 都必须显式覆盖"结束传播"这一段。
```

这也是为什么自定义 processor 是一条高危的扩展路径 —— 状态机写错不会给你一个错误结果,而是给你一个**不返回的查询**。ClickHouse 对这种挂起没有超时兜底(有查询级的 `max_execution_time`,但它管的是"跑得久",不是"卡住了")。

## 10.6 并行来自哪里

ClickHouse 的并行是**算子级**(operator-level)的,不是查询级的。搞清这一点,很多调优手段就顺理成章了。

并行的起点是读:`ReadFromMergeTree` 把一个分区的数据按 part、再按 granule 区间切成 N 份,**每份一条独立的泳道**。每条泳道上挂着 `Expression`、`Filter`、`Aggregating(部分)` 等 processor 的各一份拷贝,各跑各的,互不干扰。于是"加核"确实能加速 —— 从读开始,整条链都跟着分成多份。

`max_threads` 作用的就是这条泳道的数量。它默认是 0(表示"按 CPU 核数自动决定"),你把它设成 4,那就最多 4 条泳道。这里有一个反直觉的点:**processor 的数量通常远多于 `max_threads`**。4 条泳道的图上可能有二三十个 processor,它们被调度到 4 个线程上;真正决定并发的是泳道数,不是 processor 数。

与 `max_threads` 相伴的还有一个几乎没被提过、但决定了"泳道会不会空转"的旋钮:`max_streams_to_max_threads_ratio`。两者的分工是被设计出来的:**`max_threads` 管"同一时刻最多有几条泳道在跑"**(并发上限),而 ratio 管"允许切出比线程数更多的**读流**"。读 MergeTree 时,数据被切成一批可独立读的流(按 part、按 granule 区间),默认流数大致与线程数相当;当某条流一时没有数据可取(在等磁盘 IO、或它负责的 part 已经读完),对应线程就会空转。把 ratio 调大(例如 2),就是允许"多切几条流排着队",让线程在一条流阻塞时立刻接手另一条 —— 代价是同时驻留的中间块变多、内存峰值抬高。**一句话:`max_threads` 定"同时跑几条泳道",`max_streams_to_max_threads_ratio` 定"要不要多切几条流来喂饱这些泳道"。**(它确切的默认值随版本会有出入,以你那版 `system.settings` 为准。)

`max_threads_for_indexes` 是一个更细的旋钮:它单独限制"做主键分析、读索引"的线程数。索引分析是纯 CPU 的精细活,和真正读数据的 IO 并发需求不一样,所以被拆开控制。

当并行度需要变化时(比如 4 路部分聚合要收成 1 路做最终合并),流水线上会出现 `ResizeProcessor`。它像个"圆环":上游只读一遍,把块轮流投给 N 个下游,谁空就投给谁。同一个 processor 既能做扇出(1 → N)也能做扇入(N → 1)。在 `EXPLAIN PIPELINE` 的输出里看到它,说明这里有一次并行度的变化。

### 10.6.1 并行副本:把泳道铺到别的机器上

上面讲的泳道全都长在**一台**机器里 —— 读的是本地 part。**并行副本(parallel replicas)** 把同一套模型往外扩一层:让同一个查询分到多个**副本**上,每个副本只读自己那一份数据(不同副本持有一份**相同**的表数据,所以切的是同一份数据、不是分片),各自做部分聚合,发起节点再把部分状态收拢合并。

它的开关是 `enable_parallel_replicas`,与 `allow_experimental_parallel_reading_from_replicas` 是**同一个设置的两个名字**。哪个是"真名"?25.3 的源码给了明确答案:声明的是**长名** `DECLARE(UInt64, allow_experimental_parallel_reading_from_replicas, 0, ...)`,短名跟在后面写作 `ALIAS(enable_parallel_replicas)` —— 也就是说 **`enable_parallel_replicas` 是那个别名**,`system.settings` 里 `name` 与 `alias_to` 两列能直接看出来。默认 `0` = 关闭(取值 `0` 关 / `1` 开、失败时静默降级 / `2` 开、失败时抛错),并且在 25.3 里它已经升到 **BETA** 档,不再是"实验"档。`max_parallel_replicas` 则限定"一个分片最多用几个副本",**不是开关**,25.3 上它的默认值是 **1000**(声明为 `NonZeroUInt64`,**不接受 0**)。这本书前几轮一度把它标成"未确认":二手资料在这条上分成 24.12 与 25.4 两说。**现在可以直接从上游源码定案**——`SettingsChangesHistory.cpp` 里 `{"max_parallel_replicas", 1, 1000, "Use up to 1000 parallel replicas by default."}` 这一条落在 **25.1** 的 `addSettingsChanges(...)` 块内(既不是 24.12,也不是 25.4),而 25.3 的 `Settings.cpp` 里 `DECLARE(NonZeroUInt64, max_parallel_replicas, 1000, ...)` 确认了最终值。所以 25.3 的默认就是 `1000`,"设成大于 1 就启用"的老经验在这一版上已经作废(详见第 23 章 §23.11)。**仍建议现场复核一句** `SELECT value FROM system.settings WHERE name = 'max_parallel_replicas'`;想确认它是哪一版翻的,用 `SELECT version, change.2 AS previous_value, change.3 AS new_value FROM system.settings_changes ARRAY JOIN changes AS change WHERE change.1 = 'max_parallel_replicas'`(查法见第 24 章 §24.13)。打开后再配合一组协调参数,一次查询就能吃满多台机器的读能力。必须说清它和**分布式表**的区别:分布式表是把查询按**分片**铺开,每个分片有**不同的**数据,靠网络把中间结果搬来搬去;并行副本是把**同一份**数据在多个副本间切着读,省掉数据搬运,代价是要求各副本数据一致。

早期的并行副本是**按自定义键采样**实现的:`parallel_replicas_custom_key` 决定按哪一列切,查询靠 `SAMPLE` 把数据在副本间分片,所以那个键必须是排序键的一部分,切分才均匀(`parallel_replicas_count` / `parallel_replica_offset` 是协调器/客户端用来告诉每个副本"你负责第几片"的参数)。这条路一直用一个实验开关控制,`allow_experimental_parallel_reading_from_replicas` 取 0/1/2(0 关闭、1 开启并走 Keeper 协调、2 开启并走**协调节点**)。

较新的实现换成了**协调节点**方案:由一个节点分派任务、各副本执行;上面那个开关的另一个名字就是简短名 `enable_parallel_replicas`(两者是同一个设置),配套还有 `parallel_replicas_mode`、`parallel_replicas_local_plan` 等。**我未确认**这一族设置里各条的默认值与废弃次序 —— 这一块升级很快,请以你那一版的 `system.settings` 与实际文档为准,不要照搬这里写的名字。

用之前要知道它的边界:只对 `*MergeTree`(尤其是 Replicated 一族)有意义;带 `FINAL`、或表本身是单副本/非复制表时用不上;按自定义键切分时,键必须落在排序键里,否则各副本读到的是重叠数据、结果照样对但可能重复计算。它是"同一条查询想用多台机器"的答案,而不是"数据太大想分片存"的答案 —— 后者才是分布式表和分片(第 23 章)。

## 10.7 阻塞算子:内存峰值与首行延迟的源头

大部分 processor 是"来一块算一块走一块"的流式算子,但有一类算子**在看到全部输入之前无法产出任何东西**:

- `ORDER BY` 的排序算子。要排序就得先把数据收全(除非能用"读即有序"的优化,见下);
- `GROUP BY` 的**最终**归并。部分聚合可以流式输出,但把所有部分状态合成最终结果那一步必须等全部部分状态到齐;
- `JOIN` 的 build 阶段。哈希表建完之前,probe 阶段一行也出不来;
- `DISTINCT` / `UNION DISTINCT` 的去重算子。去重要"把所有出现过的行放进一张哈希表",同样得等全部输入到齐才能确定哪些行是第一次出现;
- 窗口函数。它需要先按 `PARTITION BY` 分区、按 `ORDER BY` 排序,才能算第一行。

它们是查询的内存峰值与"第一行延迟"的**主要**来源。一个流式的 `SELECT ... WHERE ... LIMIT 10` 可能 5 毫秒就吐出第一行;一旦加上 `ORDER BY`,第一行要等到整个排序结束才出现,而且内存峰值约等于参与排序的数据量。这也解释了为什么 `ORDER BY ... LIMIT 10` 在 ClickHouse 里有个特别的优化路径 —— 如果表本身的有序性能够满足这个 `ORDER BY`(排序键是 `ORDER BY` 的前缀),读取可以按序进行,根本不需要排序算子,内存峰值降到接近零。这条优化叫 `optimize_read_in_order`,是第 24 章的重点之一。

聚合算子也有它对应的一招。`optimize_read_in_order` 消掉的是 `Sorting`,而它的孪生设置 `optimize_aggregation_in_order`(**默认 0,得显式打开**)消掉的是 `GROUP BY` 的那张哈希表:`GROUP BY` 的键恰好是排序键前缀时,可以**边读边聚合**,只保留"当前这一组"的状态,内存从"所有分组"降到"一个分组"。代价和有序读一样落在并行度上 —— 有序读是串行的、要切也只能切成几段有序区间再归并。它的参数(`aggregation_in_order_max_block_bytes` 等)与适用场景在第 12 章 §12.5 展开,这里只点一句:**它是"分组数极多但数据本来就按分组键排好"时的救命稻草。**

真的顶不住的时候,还有外部排序:`max_bytes_before_external_sort` 与 `max_bytes_before_external_group_by` 会在内存用量越过阈值时把中间结果溢写到磁盘。代价是慢一个数量级,但至少不会 OOM。

本章反复说"内存峰值",到现在才把**负责这件事的机制点名:内存追踪(MemoryTracker)**。每个查询、每条线程、每个算子都有各自的 tracker,它们拼成一棵从查询根向下的**树**(查询 → 线程 → 算子);算子每申请或释放一块内存,都要在这棵树上做加减。上面那些"越过阈值就落盘"、以及 `max_memory_usage` 这类上限,靠的正是树上实时的读数 —— 阈值判断不是"算完再看",而是分配时就被 tracker 拦下来。所以要回答"到底是谁把内存吃掉了",看的不是单个算子的绝对值,而是**这棵树哪个分支最大**;`system.processes` / `system.query_log` 里的 `memory_usage`、以及各种内存剖析视图,源头都是它。这棵树与配额(`max_memory_usage`、`max_memory_usage_for_user` 等)如何联动,是第 25 章(并发控制、资源管理与配额)的主题,这里先记住:**没有内存追踪,那些阈值和落盘策略都无从谈起。**

## 10.8 怎么读 EXPLAIN PIPELINE

`EXPLAIN PIPELINE` 打印的是物理流水线,它回答的问题是"为什么只有一个核在跑"。看的时候按这个顺序:

先看**有没有并行度**。如果图里从头到尾都是一路,而你的表有几百个 part、查询又是个大聚合,那说明并行没起来 —— 常见原因是数据量太小(低于 `merge_tree_min_rows_for_concurrent_read` 之类的阈值,不值得并行)、或者算子本身是单点的。

再看**有没有 `Resize`**。有 `Resize` 说明并行度在这里发生了变化;多路聚合收成一路输出是正常的,但如果 `Resize` 出现在读之前,反而说明上游被限制住了。

最后看**阻塞算子的位置**。`Sorting`、`MergingAggregated`、`Join` 出现在哪个位置,内存峰值就出现在哪里,首行延迟也由它们决定。

看一份**形态示意**(真实输出会随查询与版本变,这里画的是一张典型的多路聚合图,不是某次实测,所以别把算子名与 `× N` 当成逐字照抄;要逐字输出就在你自己的库上跑一遍):

```text
(Expression)
ExpressionTransform × 4
  (Aggregating)
  AggregatingTransform × 4
    (Expression)
    ExpressionTransform × 4
      (ReadFromMergeTree)
      MergeTreeThread × 4

Resize 1 → 1
  (MergingAggregated)
  MergingAggregatedTransform × 1
```

逐行读它:

- 缩进表示**数据流方向**:最深的 `MergeTreeThread` 是源头,越往上越靠近输出。读这张图要从**下往上**看,和你写 SQL 的顺序相反。
- `ExpressionTransform` 是投影/表达式求值,`AggregatingTransform` 是部分聚合,`MergeTreeThread` 是"读 part 的那条泳道"。
- **`× N` 就是这条泳道/算子被实例化了几份**。四个 `× 4` 连成一条自上而下的竖线,说明这里从读开始就分成了 **4 条完全独立的泳道**,每份拷贝各跑各的 —— 这正是"每个核都在满载"的可视证据。若整张图从头到尾都是 `× 1`,那不管表有多大,都只有一个核在干活,该往"为什么没并行"上查(数据量低于并发读阈值、算子单点、或 `max_threads` 被压成 1)。
- **`Resize 1 → 1`** 是并行度的变化点。这里显示 `1 → 1`,含义是"4 条聚合流的输出被扇入到 1 条",而输出侧本来就是 1,所以两头的数字都是 1 —— 真正变化的是扇入侧。看到 `Resize`,`X`(左边)是**扇入前**的份数、`Y`(右边)是**扇出后**的份数:`N → 1` 是把并行收成一,`1 → N` 是把一样东西广播成 N 份(常见于把小表广播到每条泳道)。若 `Resize` 出现在**读的上游**(图中更靠下的位置),反而说明是上游被限制住了、下游想多要却没有源,那是另一种问题。
- `MergingAggregatedTransform × 1` 就是 §10.9 的**归并级**,它只出现一份(归并是收口,天然单点),而且是这张图里唯一的**阻塞算子** —— 内存峰值与"第一行结果什么时候出来",都由它决定。

这张图对应的查询大致是"读一张大盘、按某列分组、算个聚合"。把它和 §10.10 里 mini-clickhouse 的分段执行输出并排看,差异一眼就出来了:真实版本是**一张图 + 一个 `Resize` 收口**,而 mini 会把它切成四段 `Pipeline`、且每个算子都是 `× 1`。

`EXPLAIN PIPELINE graph = 1` 会把图打印成 DOT 格式,可以直接渲染成图看,泳道一多就比文本清楚得多。`header = 1` 会在每个 processor 后面附上它的输出表头,排查"列在哪一步丢的"时很有用。完整的 `EXPLAIN` 家族(`PLAN` / `PIPELINE` / `ESTIMATE` / `SYNTAX`)在第 24 章会逐个展开。

上面这一整族都是**静态**的:它们只描述计划或流水线,**不真的跑查询**。较新的 ClickHouse 另提供了一个会真跑的 `EXPLAIN ANALYZE SELECT ...`:它**真的执行**这条查询,并打印一棵带运行时指标的树,每个节点后面标出实测耗时/行数而不是估算值,于是"瓶颈到底在过滤还是在聚合"从猜变成了读。**但它在本书对标的 25.3 上到底有没有,我仍未确认** —— 它是较晚才加入的语句:能见到的"26.x 才引入"之类的版本号说法,**都还没有被官方 release notes 锚定**(上游对应 PR 仍在演进,文献横跨 25.x–26.x),所以 25.3 上是否存在,请直接拿一条 `EXPLAIN ANALYZE` 实测为准;也不排除更早的版本已有雏形。所以正确态度是两不:既**不要断言它在 25.3 上会报错**,也**不要假定它一定可用** —— 用之前拿你那一版直接试一条,或查那版的 `EXPLAIN` 文档。在确认它可用之前,要为"哪一步实际慢"取证,只能退回到两件静态工具 + 两件运行期工具的组合:`EXPLAIN PIPELINE`(看结构,哪一步是阻塞点)+ `system.query_log` 里的 `ProfileEvents`(看这条查询整体读了多少行/多少字节)+ `system.trace_log`(采样线程栈,看 CPU 花在哪)+ 临时把 `send_logs_level` 调高,让执行器把每步的行数/时序打进客户端日志。这套要自己把结构图和实测指标对起来,不如 `EXPLAIN ANALYZE` 直观,但它就是 25.3 时代的现实。

## 10.9 两阶段聚合与聚合状态

`GROUP BY` 是 ClickHouse 最自豪的能力之一,它的并行骨架是"**部分聚合 → 归并**"两段:每条泳道先把自己手上的那些行压成一份**部分状态**,再把多份状态合成最终值。多线程、多分片下的聚合,大多是这个形状。

但先要把一个过度绝对的说法纠正掉:**这两段并不是总会都在。** 归并那一级(`MergingAggregatedTransform`)是在**存在多条独立的聚合流**时才被实例化出来的 —— 单线程(`max_threads = 1`)且非分布式的聚合只有**一条**聚合流,这时计划里往往就只有一个 `AggregatingTransform`,它**就地 finalize**,没有单独的归并算子;只有当并行度 > 1、或数据来自多个分片(每个分片各出一份部分状态)、或走了两级哈希把状态摊到多个桶上时,才需要有东西**把多份状态收拢**,归并级才会出现。此外 §10.7 讲的 `optimize_aggregation_in_order` 会改变计划形状:它让聚合顺着排序键**流式**进行,不维护全量哈希表,也就没有"部分状态归并"这一环。所以"两段"是**最常见**的形态,不是**必然**的形态 —— 把"一定有两段"当成事实,读到单流计划或用了有序聚合时就会对不上。

`GROUP BY` 能这样切开、还切得正确,关键在于:**每个分组维护的是"聚合状态",不是一个结果值**。`avg` 的中间状态是 `(sum, count)` 而不是一个浮点数 —— 因为 `(sum, count)` 可以合并,而"平均值"不能合并(两个组平均值的平均是错的)。那组典型的数字(两条线程各处理一半数据、归并后 `A → (sum:6, count:3)`、`B → (sum:13, count:3)`)是**语义**问题,放到第 12 章 §12.2 完整推一遍,这里不重复,只记住引擎这一侧的一句话:**它在泳道之间传的是状态,不是结果。**

这就是"聚合状态"这个概念的价值,也是 ClickHouse 把 `AggregateFunction` 做成一种**类型**的原因:既然状态是一个值,它就能被序列化、能在网络上传输、能被写进 part(`AggregatingMergeTree`)、能被物化视图保存下来做增量聚合。

代价是内存:分组数一多,哈希表会爆。ClickHouse 的对策是**两级哈希表** —— 当分组数超过 `group_by_two_level_threshold`(默认 100000)或状态占用超过 `group_by_two_level_threshold_bytes`(默认约 50 MB)时,它按 key 的高位把哈希表切成 256 个桶,每个桶可以独立地被一条泳道处理并独立合并。这样"分组数极多"的查询也能并行,而且合并时可以只驻留一部分桶在内存里。(顺带点破一个联系:两级哈希本身就是"归并级会出现"的一处来源 —— 桶一多,聚合流就多,就需要收拢。分组数爆炸的最危险场景与对策见 §12.3。)

这一节只从**执行引擎**的角度回答"部分聚合为什么能并行、归并级什么时候才出现、代价在哪"。聚合本身的**语义** —— 状态长什么样、组合器怎么拼、内存怎么控、`uniq` 为什么可能不准 —— 是第 12 章的主题,那组数字的完整推演也在第 12 章 §12.2,这里不再重复。

## 10.10 与 mini-clickhouse 的对照

mini-clickhouse 的流水线实现在 `minich/execution/processors.py`,这个文件的开头有它自己画的两张对比图,值得先读一遍。它的 processor 清单是:

源与汇聚:`SourceFromChunks`、`SourceFromBlocks`、`NullSink`、`CollectingSink`。

变换:`ExpressionTransform`、`FilterTransform`、`LimitTransform`、`OffsetTransform`、`ArrayJoinTransform`、`MaterializingTransform`、`DistinctTransform`、`LimitByTransform`、`UnionTransform`、`TotalsTransform`。

阻塞与归并:`SortingTransform`、`MergingSortedTransform`、`AggregatingTransform`、`MergingAggregatedTransform`、`WindowTransform`。

并行度:`ResizeProcessor`。

四态状态机是**照抄**的:`Status` 枚举就叫 `NeedData` / `PortFull` / `Finished` / `Ready`,连文档里说的"为什么需要这四个"都一致。另外 `PushResult` 也有 `NotNeeded` 这个取值 —— `LIMIT` 提前终止就是靠它:下游主动 finish,上游 `push` 时拿到 `NotNeeded`,于是停止产出。基类 `ISimpleTransform` 里写死了"输入结束 ⇒ 输出结束"这条规则,正是本书 10.5 节强调的那个约定。

不过我们的实现与真实 ClickHouse 有一处**必须说清楚的差异**:

**mini-clickhouse 的流水线是分段执行的。** 真实 ClickHouse 是**一张** processor 图,多个 processor 在**真线程**上并发运行,靠端口状态同步。mini 则按拓扑序在**单个线程**里轮转,而且聚合、排序、JOIN 这些阻塞算子会把中间结果**物化**成 Block 再交给下一段。语义一样(结果正确、背压也成立),但后果是:内存峰值更高,而且你看不到"边算边出"—— 第一行结果必须等所有段跑完。

这个差异在 `minich/execution/explain.py` 的 `_explain_pipeline` 里被如实地打印出来了,函数上还有一条 warning 明确写着"否则读者会以为 ClickHouse 也只是'一段一段跑'"。另外 `JoiningTransform` 在 `processors.py` 里是个 **stub** —— 它的 `work()` 直接抛 `NOT_IMPLEMENTED`,文档里写明 JOIN 实际由 `minich/execution/joins.py` 的 `JoinExecutor` 在 build/probe 两个阶段里一次算完(第 15 章)。

下面这段代码可以直接跑(`Context` 从 `minich.execution.context` 导入):

```python
from minich.execution.context import Context

ctx = Context()
sql = ("EXPLAIN PIPELINE SELECT number % 10 AS g, count() "
       "FROM numbers(1000) GROUP BY g ORDER BY g")
print(ctx.execute(sql).text("TSV"))
```

我在 25.3 对标版本的 mini-clickhouse 上跑出来是这样(这是**真实输出**,不是我编的):

```text
(mini-clickhouse 分段执行,不是一张完整的 processor 图)
Pipeline 0:
  (ReadFromStorage × 1)
Pipeline 1:
  (Expression × 1)
  (Aggregating × 1)
  (MergingAggregated × 1)   ← 阻塞点:必须看到全部输入
Pipeline 2:
  (Expression × 1)
  (Sorting × 1)             ← 阻塞点:内存峰值 = 结果集大小
Pipeline 3:
  (Expression(Projection) × 1)
  (Limit × 1) → (CollectingSink × 1)

max_threads = 0
max_block_size = 65409
```

请把这四段 Pipeline 与真实 ClickHouse 的输出对照着看:真实版本会打印**一张**图,而且每个 processor 有自己的编号,同一个算子会出现多份(比如 `Expression × 4`),还会出现 `Resize` 节点。我们这里每个算子都是 `× 1`,而且被切成了四段 —— 这两点差异,恰好就是"分段执行 + 单线程"的可见证据。

## 10.11 读路径的 IO 层:并发读的门槛、预取与异步读

前面几节讲的都是"数据进了流水线之后怎么流动"。但顺着 §05.13 那条读路径看,真正把字节从磁盘上取下来的动作发生在第 ④ 步:"按列 seek 到对应的压缩块,解压"。这一句看着轻描淡写,它却是整条查询里唯一一个**会被延迟(而不是被 CPU)卡住**的地方:一次主键裁剪之后要读的,往往是几百上千个**互不连续**的小区间,而且每一列都要各算一遍。所以"这条查询为什么慢"有一大类答案,最终落到这一层。

这一节的四组旋钮,对应四个不同的问题:**要不要并行读**(门槛)、**要不要提前读**(预取)、**读流能不能比线程多**(异步读)、**读出来的解压块要不要留在缓存里**(缓存门槛)。它们全都在 §10.6 那条"泳道"的下游——泳道决定"谁来读",这一节决定"读得有多宽、多深、多快"。

**① 并发读是有门槛的,而且门槛是自动的。**

把一段 mark 交给多个线程并行读,并不总是划算:数据量小的时候,调度开销和额外内存比省下的时间还多。所以 ClickHouse 不会"见缝就并发",而是先看这段要读多少:

- `merge_tree_min_rows_for_concurrent_read`(**表级**设置,默认 163840,即 20 × 8192)——一个文件里要读的**行数**超过它,才尝试并发读;
- `merge_tree_min_bytes_for_concurrent_read`(默认 25165824,即 24 MiB)——同一件事的**字节**口径。

规划阶段会把这两个阈值折成一个 mark 数门槛(`min_marks_for_concurrent_read`,列在 `EXPLAIN PLAN` 的 `Indexes` 段附近),**一段要读的 mark 数越过它,才值得拆给多个线程**。这两条都是表级设置,查它们的当前值用 `system.merge_tree_settings`(不是 `system.settings`);默认值随版本调整过,**以你那一版的实际输出为准**。

这条设计带来一个很好的性质:**小查询自动串行、大查询自动并发,你不需要手调**。但它也解释了一类反直觉的现象——**把查询条件收得更窄之后,查询有时反而没变快多少**:数据量降到了门槛以下,引擎退化成单线程读,省下的 IO 被丢掉的那部分并行度抵消了。看到这种"越窄越不划算",先怀疑门槛,而不是先怀疑索引。

门槛之外还有一层:**能不能切开**。并行读的前提是这段数据能被切成若干独立区间。如果查询落在一个 part 的**一个连续小区间**里(比如按主键点查),它就切不开——不是引擎不想并行,是没有第二份活可以分出去。这也是"点查为什么吃不满 CPU"的根源。真正把 mark 均分给各条流的算法在 `ReadFromMergeTree.cpp` 里(按"总 mark 数减去 1、除以流数、再加 1"的思路均分),属于源码级细节,**具体公式随版本会变,别背**。

**② 预取:在读当前 granule 的时候,顺手把下一个也读进来。**

顺序扫描是 MergeTree 上最常见的姿势(按时间范围扫一段分区),而顺序扫描的访问模式是**高度可预测**的:读完 granule `k`,下一个要读的就是 `k+1`。既然能预测,就没必要等"真的要用了"才发读请求——在读当前块的同时,把后续的读也发出去,等轮到它时数据已经在内存里了。这就是预取,它用**空闲的带宽**换**隐藏的延迟**。

它由一组 `filesystem_prefetch_*` 设置控制:`filesystem_prefetch_step_bytes` 与 `filesystem_prefetch_step_marks` 决定"一次往外看多远"(默认都是 0,表示由引擎**自动**选步长),`filesystem_prefetch_max_memory_usage`(默认 1Gi)限制所有预取加起来的内存,`filesystem_prefetches_limit`(默认 200,设 0 表示不限)限制**同时在途**的预取请求个数。后两个是"别让预取把内存或 IO 队列吃光"的刹车。

这里有一个**很能说明问题的默认值**:预取该不该占用专门的线程池,分远端盘和本地盘两种口径——`allow_prefetched_read_pool_for_remote_filesystem`(默认 **true**)与 `allow_prefetched_read_pool_for_local_filesystem`(默认 **false**)。为什么不一样?因为预取的价值等于"延迟能被隐藏多少":对象存储、网络盘的延迟是毫秒级,预取的收益巨大,值得专门养一个池;本地 NVMe 的延迟是微秒级,预取省下的那点时间还抵不上多占的线程和内存。**同一个机制,在两种介质上得到相反的默认值**——这条取舍比机制本身更值得记住,它也是"为什么别人的调优参数抄过来没用"的典型例子。(另有 `local_filesystem_read_prefetch`,控制本地文件系统读要不要走预取,默认关闭。)

**③ 异步读:允许"读流"比"处理线程"多。**

§10.6 讲过 `max_streams_to_max_threads_ratio`,那里切的是**处理流水线**的流。这里要补的是读侧的另一条路:`allow_asynchronous_read_from_io_pool_for_merge_tree`(**默认 0/关闭**)。

默认情况下,读流数与流水线的线程数是绑在一起的——有多少条泳道,就开多少条读流。这在本地盘上没问题(IO 快,线程不怎么会等)。但在**对象存储这类高延迟介质**上就吃亏了:线程大部分时间在等网络往返,而"等"是不占 CPU 的,却被绑死在一条流上。打开这个开关后,读会走单独的 IO 线程池,**读流数可以超过处理线程数**,具体开多少条由 `max_streams_for_merge_tree_reading` 控制。效果就是"用更多在途请求填满网络,把等待藏起来"——和上一条预取解决的是同一类问题,只是粒度更粗(整条流 vs 单个读请求)。

**用之前必须知道一个硬约束:它和 `optimize_read_in_order` 互斥。** "读时有序"这条优化(§10.7、第 24 章 §24.14)要求读取**按排序键单流推进**,天然没法再拆成多条异步流,所以一旦 `optimize_read_in_order` 生效,异步读就被压下去。于是这里出现一个真实的两难:**你要"不排序、流式出结果",还是要"把远端盘的带宽吃满"**——`ORDER BY` 正好是排序键前缀时,前者往往更值;纯粹的大范围扫描(没有可利用的有序性)时,后者才是正解。两个都想要是没有的。

**④ 解压缓存也有门槛:别让一次大扫描把热块冲走。**

§05.8 讲过解压块缓存(`uncompressed_cache_size`,并且要 `use_uncompressed_cache = 1` 才生效)。这里补上它为什么**通常默认开着也不生效**:除了总开关,单个读任务还有一个"够不够格进缓存"的门槛——`merge_tree_max_rows_to_use_cache`(默认 128 × 8192)与 `merge_tree_max_bytes_to_use_cache`,读的量超过它,这一次读取就**只在内存里过一遍、不往缓存里放**。

理由是纯粹的缓存经济学:缓存的价值来自"同一块被反复读"。一次全表扫描读进来的块,很可能再也不会被读第二次,把它们塞进缓存只会把真正会被复用的热块挤出去,命中率反而下降。所以这条门槛的作用不是省内存,而是**保护命中率**。

**⑤ 收口:一张"为什么只有一个核在跑"的排查顺序。**

把这四组旋钮和 §10.6 合起来,"CPU 吃不满"就有了一条从粗到细的排查顺序,按这个顺序查比乱翻设置快得多:

第一,看**泳道数**:`EXPLAIN PIPELINE` 里同一类 processor 是不是只有 `× 1`,以及 `max_threads` 是不是被设成了小数——这是最常见的原因(§10.6、§24.4)。第二,看**数据切不切得开**:主键裁剪之后是不是只剩一段连续区间(点查、或条件极窄的查询),或者是单 part、单 granule 的小表——这时候并行本来就没有意义。第三,看**并发读门槛**:数据量没到 `merge_tree_min_rows_for_concurrent_read`,引擎主动选择串行。第四,看**是不是自己关掉了并行**:走了 `optimize_read_in_order` / `optimize_aggregation_in_order`(两者都以并行度为代价,§10.7),或者被 `max_threads_for_indexes` 卡在索引分析阶段。第五,才是看**线程池是不是被别的查询占满了**——这是并发问题而不是单查询问题,归第 25 章。

**与 mini-clickhouse 的对照。** 这一节讲的四组旋钮,在 mini-clickhouse 里**全部不存在**,而且不是"简化了",是"没有"——它的读取是逐 granule **同步**进行的:没有线程池(流水线本身就是单线程轮转,§10.10)、没有预取、没有异步读、也没有解压块缓存。它能如实对照的只有**决定"要读多少"的那一层**:列裁剪、分区裁剪、主键与跳数索引裁剪(§05.13 的 ①–③),以及"裁完之后按 mark 逐个 seek"的顺序。换句话说,**mini 能证明"裁剪是对的",但证明不了"读取是多快的"**——后者恰好是这一节所有旋钮所在的位置。这也是 `mini-clickhouse/README.md` 里"不要拿它的耗时做任何性能结论"那句话在 IO 这一层的具体含义。

最后照例提醒一句:这一节的设置名与默认值都**以 `system.settings`(查询/会话级)与 `system.merge_tree_settings`(表级)的当场输出为准**。`filesystem_prefetch_*` 这一族尤其新,名字在版本之间改过;凡是你没在本机 `SELECT` 出来的,都当"未确认"对待。

## 10.12 小结

- 一条查询走四层:Parser → Interpreter/Planner(QueryPlan,逻辑)→ buildQueryPipeline(QueryPipeline,物理)→ PipelineExecutor(执行)。`EXPLAIN PLAN` 看前一层,`EXPLAIN PIPELINE` 看后一层。
- ClickHouse 不是火山模型。它按**块**在 processor 图上传递数据,`work()` 返回 `NeedData` / `PortFull` / `Finished` / `Ready` 四态。由此得到向量化、端口背压、算子级并行、`LIMIT` 反向传播四个好处。
- `Chunk` = 表头(一个 `Block`)+ 数据列,`Block` 现在主要当表头用。拆开是为了让表头在上百万个 Chunk 之间复用,并让"类型对不对"的校验变得便宜。
- `max_block_size`(65409,内存处理单位)与 `index_granularity`(8192,磁盘读取单位)是两回事;65409 不是 2 的幂,官方文档没有给出解释,但**上游源码注释已写明**它是 `65536 - PADDING_FOR_SIMD - (PADDING_FOR_SIMD - 1)`(`PADDING_FOR_SIMD = 64`,见 `src/Core/Defines.h`),与"为 SIMD 对齐留填充"的说法一致;这个默认值本身也可以现场查 `system.settings`。内存里块的实际行数还受第二个、按字节计的预算 **`preferred_block_size_bytes`(默认 1000000,约 1 MB)** 压低:块在"行数到顶(`max_block_size`)或字节到顶(`preferred_block_size_bytes`)"里先到先切,与 `index_granularity` / `index_granularity_bytes` 在 granule 上的关系同构。这就是"为什么块的行数常远少于 65409"的答案(还有个默认 0、即关闭的 `preferred_max_column_in_block_size_bytes`,能进一步卡单列字节数)。
- 构建流水线时先拼出若干 `Pipe`(逻辑上线性的一段 processor),最后由 `QueryPipeline` 把 pipe 展开成一张 DAG —— 并行度、`Resize`、端口类型都在这一步才定下来。
- 并行来自 `ReadFromMergeTree` 按 part/granule 拆分出的多条泳道,`max_threads` 控制泳道数,`ResizeProcessor` 负责并行度的扇出与扇入。processor 数量通常远多于 `max_threads`。
- 想跨机器并行,用**并行副本**:靠 `enable_parallel_replicas`(与 `allow_experimental_parallel_reading_from_replicas` 是同一个设置的别名)打开,`max_parallel_replicas` 限定每个分片用几个副本 —— 把**同一份**数据在多个副本间切着读、各自部分聚合,和按分片切数据的分布式表是两回事(25.3 上默认值 **1000**,**25.1** 由 `1` 抬上来的,依据见本页上文;仍以 `SELECT value FROM system.settings WHERE name = 'max_parallel_replicas'` 实测为准)。
- 泳道之下还有一层 **IO 层**,它决定"读得有多宽"而不是"谁来读":并发读要越过 `merge_tree_min_rows_for_concurrent_read` / `merge_tree_min_bytes_for_concurrent_read` 的 mark 门槛(`min_marks_for_concurrent_read`),否则引擎主动选串行;顺序扫描靠 `filesystem_prefetch_*` 一族预取后续读,而预取要不要占专用线程池在远端盘与本地盘上**默认相反**(`allow_prefetched_read_pool_for_remote_filesystem` 默认 true、`..._for_local_filesystem` 默认 false);`allow_asynchronous_read_from_io_pool_for_merge_tree`(默认关)允许读流数超过处理线程数,但它**与 `optimize_read_in_order` 互斥**;解压块能不能进缓存还受 `merge_tree_max_rows_to_use_cache` / `merge_tree_max_bytes_to_use_cache` 的量级门槛约束,这道门槛保护的是命中率而不是内存。
- `ORDER BY`、`GROUP BY` 的最终归并、`JOIN` 的 build、窗口函数是阻塞算子,它们决定了内存峰值与首行延迟。对付它们的两条捷径是 `optimize_read_in_order`(消排序)与 `optimize_aggregation_in_order`(消 `GROUP BY` 哈希表,默认 0),代价都落在并行度上。
- `EXPLAIN PLAN` / `PIPELINE` 是**静态**的(不执行查询);会真的跑一遍并打印每个节点实测耗时/行数的 `EXPLAIN ANALYZE` 是较晚才加入的语句,**它在 25.3 上是否存在我未确认** —— 有人提到"26.x 才引入"的版本号,但**这些说法都还没有被官方 release notes 锚定**,请直接拿 `EXPLAIN ANALYZE` 实测为准;既不要断言它一定报错、也不要假定它可用;要实测只能靠 `EXPLAIN PIPELINE` + `query_log`/`trace_log` 拼出来。
- 聚合让 `GROUP BY` 线性扩展的关键,是它传递**可合并的聚合状态**(`avg` 的状态是 `(sum, count)`),而不是结果值。但"部分聚合 → 归并"这两段**并非总会都在**:单线程非分布式下可以只有一个 `AggregatingTransform` 就地 finalize,归并级只在存在多条聚合流时才被实例化,`optimize_aggregation_in_order` 又会改变计划形状。
- 查询结果缓存**不在**这条四层执行链上(它在更外层做"命中就返回、不建流水线"的短路);本章反复说的"内存峰值"与各阈值背后的机制是 **MemoryTracker**(查询/线程/算子构成的树,分配时即计数)。
- mini-clickhouse 照抄了四态状态机与 `PushResult::NotNeeded`,但它的流水线是**分段执行**的:单线程轮转、阻塞算子物化中间结果,所以内存峰值更高、看不到"边算边出"。这一点在 `explain.py` 的输出里被显式打印出来。
