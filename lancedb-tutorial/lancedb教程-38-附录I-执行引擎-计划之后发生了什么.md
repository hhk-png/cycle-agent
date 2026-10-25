# 附录 I　执行引擎：计划之后发生了什么

## I.1 这一篇补的是哪一层

第 19 章标题写的是"查询规划**与执行**"，但它的十个小节讲的都是**规划**：
投影下推、分区裁剪、prefilter 选择、延迟物化，以及怎么把这几件事
打印成一棵可读的树。真正的问题——**这棵树是怎么被跑起来的**——
全书没有正面讲过。§19.9 末尾承认了这件事，用的是"本实现逐片段顺序扫描、
单线程"这样一句反面描述。

所以这一篇补的就是这一层：**计划确定之后，数据是怎么从磁盘流到你的
`to_list()` 里的。** 它横跨几个已有的章节，放进任何单独一章都不合适——
算子的形状属于第 19 章，I/O 计数属于第 6 章，内存边界属于第 24 章，
而"哪些旋钮从 Python 这一层根本够不着"属于附录 H 讲的那条摄入边界。
和第 1.6 节那个"把标签扔掉"的立场一致：**这一层不写清楚，
"查询慢"就只能靠猜。**

先说清楚和最容易被混淆的那一篇的分工。附录 G 叫"运行时面"，
一个想找执行引擎的读者会去翻它——但 G 讲的是**客户端运行时**：
异步接口、作业句柄、LSM 写路径、克隆、预热。那是"调用是怎么发出去的"。
这一篇讲的是**引擎运行时**：算子在哪儿、数据分几路、
内存在哪儿封顶、结果怎么一段一段吐出来。**两篇加起来才是一条完整的路**，
从你按下 `.to_arrow()` 到磁盘上的字节被读出来。

验证环境照旧：`lancedb 0.40.0` + Python 3.14.6 + pyarrow 26.0.0，
**没有装 `pylance`**（`import lance` 直接 `ModuleNotFoundError`）。
这个缺件在 I.10 里会变成一个具体的边界，而不是一句免责声明。
下面每个输出都是本机实跑贴出来的，不是从文档里抄的。

## I.2 真实引擎是 DataFusion，而计划树长得和本实现不一样

第 19.2 节展示的是**本实现**的 `explain()`，那一节也明确说了
"两者的节点名有重叠也有分歧"。分歧有多大，看一眼真实的就清楚了。
2000 行、3 维向量、一张没建索引的表，跑一次最普通的向量检索：

```python
q = t.search([1.0, 1.0, 0.5]).limit(5)
print(q.explain_plan())
```

```
ProjectionExec: expr=[id@3 as id, vector@0 as vector, _distance@2 as _distance]
  LanceRead: uri=.../t.lance/data, projection=[id], source=stream(_rowid)
    GlobalLimitExec: skip=0, fetch=5
      FilterExec: _distance@2 IS NOT NULL
        SortExec: TopK(fetch=5), expr=[_distance@2 ASC NULLS LAST, _rowid@1 ASC NULLS LAST], preserve_partitioning=[false]
          KNNVectorDistance: metric=l2
            LanceRead: uri=.../t.lance/data, projection=[vector], num_fragments=1, range_before=None, range_after=None, row_id=true, row_addr=false, full_filter=--, refine_filter=--
```

**第一件要认出来的事：这是 DataFusion 的物理计划。**
`ProjectionExec` / `FilterExec` / `SortExec` / `GlobalLimitExec`
这几个 `*Exec` 后缀就是 DataFusion 的物理算子命名法。
第 1.1 节那张分层图里"DataFusion 系"四个字，
到这里才有了具体的形状——**LanceDB 的查询执行不是自己写的一个执行器，
它把 Lance 的读取算子挂到 DataFusion 的物理计划上跑。**
这件事解释了很多表现：为什么过滤表达式是 SQL 语法、
为什么计划里会出现 `@数字` 这种列下标引用（`_distance@2` 是
"第 2 个输出列"）、以及为什么第 11 章那套表达式引擎
最后要落到"能被下推给执行器的形式"。

**第二件事和本实现有关，而且是一条真实的差异。**
第 19.7 节那个例子里，树的根是 `ProjectionExec`、第二层是 `LanceRead`，
看上去和这里一样；但本实现那棵树**不携带父子层级语义**
（§19.8 自己承认了："一棵树形状的列表"）。真实这棵是真树：
缩进表达的是"谁是谁的输入"，`LanceRead` 出现在**两个不同深度**上
不是排版巧合，而是两个不同的读取阶段（I.5 会讲这件事）。

还有一个只有真树才表达得出的东西：**`SortExec` 在 `LanceRead` 下面，
而不是上面。** 顺序是"先算距离 → 再排序取前 5 → 再回表取 id"。
如果按直觉画成"读完数据、排个序、输出"，`SortExec` 会在读取之上——
**这棵树的形状本身就是"延迟物化"的证据**，比任何文字描述都直接。

## I.3 每个算子都带着自己的账本

`explain_plan()` 只说"应该怎么做"。要拿实测数字换 `analyze_plan()`：

```python
print(q.analyze_plan())
```

```
AnalyzeExec verbose=true, elapsed=12.9332ms, metrics=[]
  TracedExec, elapsed=12.9332ms, metrics=[]
    ProjectionExec: elapsed=12.9332ms, expr=[id@3 as id, vector@0 as vector, _distance@2 as _distance], metrics=[output_rows=5, elapsed_compute=6.10µs, output_bytes=32.1 KB, output_batches=1, expr_0_eval_time=1.00µs, expr_1_eval_time=100ns, expr_2_eval_time=100ns]
      LanceRead: elapsed=12.9217ms, uri=..., projection=[id], source=stream(_rowid), metrics=[output_rows=5, elapsed_compute=35.60µs, output_bytes=96.1 KB, output_batches=1, fragments_scanned=1, ranges_scanned=1, rows_scanned=5, bytes_read=1.30 K, iops=2, requests=2]
        GlobalLimitExec: elapsed=12.1289ms, skip=0, fetch=5, metrics=[output_rows=5, elapsed_compute=8.30µs, output_bytes=96.1 KB, output_batches=1]
          FilterExec: elapsed=12.1213ms, _distance@2 IS NOT NULL, metrics=[output_rows=5, elapsed_compute=27.90µs, output_bytes=96.1 KB, output_batches=1, selectivity=100% (5/5)]
            SortExec: elapsed=11.9887ms, TopK(fetch=5), expr=[_distance@2 ASC NULLS LAST, _rowid@1 ASC NULLS LAST], preserve_partitioning=[false], filter=[_distance@2 < 21.25 OR _distance@2 = 21.25 AND _rowid@1 < 4], metrics=[output_rows=5, elapsed_compute=341.10µs, output_bytes=124.0 B, output_batches=1, row_replacements=5]
              KNNVectorDistance: elapsed=11.9887ms, metric=l2, metrics=[output_rows=2.00 K, elapsed_compute=573.90µs, output_bytes=46.9 KB, output_batches=2]
                LanceRead: elapsed=11.3719ms, uri=..., projection=[vector], num_fragments=1, ..., metrics=[output_rows=2.00 K, elapsed_compute=11.18ms, output_bytes=0.0 B, output_batches=0, fragments_scanned=1, ranges_scanned=1, rows_scanned=2.00 K, bytes_read=28.14 K, iops=3, requests=3, task_wait_time=11.18ms]
```

最外层两个节点是**插进来的**，不在 `explain_plan()` 里：
`AnalyzeExec` 是"这次要采集指标"这个开关本身，
`TracedExec` 是给它加一层计时包装。**看到它们就说明这是分析模式**，
不是正常执行的计划——别把它们当成流水线的一部分去数算子个数。

**每个算子的指标是自解释的，不需要猜**，这是这一层最有用的性质。
所有算子都有的六个是：

`output_rows`（这个算子吐了多少行）、`output_batches`（分几批吐的）、
`output_bytes`（输出缓冲区多大）、`elapsed_compute`（**纯计算**耗时）、
以及算子自身的状态量（`FilterExec` 的 `selectivity`、
`SortExec` 的 `row_replacements`）、表达式级的 `expr_N_eval_time`。

**`elapsed_compute` 和整棵树的 `elapsed` 不是一回事**，这是第一个陷阱。
树根 `elapsed=12.9332ms`，而底下所有算子的 `elapsed_compute` 加起来
只有 `6.10µs + 35.60µs + 8.30µs + 27.90µs + 341.10µs + 573.90µs`，
不到 1 毫秒。**剩下那 12 毫秒不是算掉的，是等掉的。**
最内层 `LanceRead` 的 `task_wait_time=11.18ms` 把这个"等"直接暴露了：
它和该算子的 `elapsed_compute=11.18ms` **数值相等**，
但含义相反——这个算子在这 11 毫秒里主要是在等 I/O，不是在算。
所以排查"查询慢"的第一步永远是：**先看 `task_wait_time`，
它是零说明 CPU 是瓶颈，它占大头说明是 I/O 是瓶颈**，
两条路的解法完全不一样（第 24.2 节把这件事称作"先把 I/O 数出来"）。

**`output_bytes` 这个数不要拿去反推行宽。** 它和 `output_rows` 对不上：
`FilterExec` 只有 5 行，却报 `output_bytes=96.1 KB`。这说明它是
**Arrow 缓冲区的口径**（按批次分配的大小），不是"行数 × 行宽"。
拿 96.1 KB 除以 5 算出"每行 19 KB"是个假结论。
**要算真实的行宽，用 I/O 那几个字段**（`bytes_read` / `rows_scanned`），
它们是从存储层记账上来的，口径干净。

最后，`LanceRead` 上报的是**存储层**的账：`rows_scanned`（扫了多少行）、
`fragments_scanned` / `ranges_scanned`（碰了几个片段、几个区间）、
`bytes_read`、`iops`、`requests`。这几个字段值得和附录 G 里那条
`otel` 指标对起来看——同一个对象存储请求，一边是算子级、
一边是进程级，两条路都能自证。

## I.4 并行发生在哪：8 个片段、8 个批次、一个合并点

单片段看不出并行。把同一张表写 8 次（每次 500 行，不压实），
凑出 8 个片段再跑同一个查询，`explain_plan()` 里只多了一个数：

```
      LanceRead: ..., projection=[vector], num_fragments=8, ...
```

但 `analyze_plan()` 把并行度暴露得很清楚：

```
KNNVectorDistance: elapsed=14.6915ms, metric=l2, metrics=[output_rows=4.00 K, elapsed_compute=749.20µs, output_bytes=93.8 KB, output_batches=8]
  LanceRead: ..., projection=[vector], num_fragments=8, ..., metrics=[..., rows_scanned=4.00 K, ...]
    SortExec: ..., preserve_partitioning=[false], ...  metrics=[output_rows=5, ..., output_batches=1]
```

**`output_batches=8` 就是并行度。** 8 个片段各出一条批次流进来，
`KNNVectorDistance` 逐个批次算距离，所以它吐了 8 批、4000 行。
上层的 `SortExec` 只吐 **1 批** 5 行——**因为它的
`preserve_partitioning=[false]`**。

这一个布尔值就是整条流水线上**唯一的合并点**。
它的含义是"排序结果不要保留分区结构"，而要跨分区取全局 Top-5，
就必须先把 8 路结果并到一起。**并的地方就要同步，同步的地方就是
不能无限并行下去的地方**——这是所有流水线执行引擎的共同瓶颈，
只是不同引擎把它藏在不同名字下面（`CoalescePartitions`、
`gather`、`exchange`）。LanceDB 这棵树上，它写成了一个参数的取值。

顺带解释一个**反直觉的现象**：8 个片段并行的 `elapsed` 是 `15.2456ms`，
单片段那次是 `12.9332ms`——**数据量变成 1/2，时间反而没变快。**
原因是这次查询在 4000 行的规模上**根本不是 CPU 瓶颈**
（`KNNVectorDistance` 的 `elapsed_compute` 只有 `749.20µs`），
并行度再高也压不动那十几毫秒的固定开销（打开表、读 manifest、建 reader）。
这个观察和第 12.5 节"什么时候该建索引：约 10 万向量"是同一条定律的
两种表现：**在这个规模上，能并行的那部分本来就不是瓶颈，
所以加并行度的收益接近零。**

## I.5 数据是一段一段流过来的，不是一次性算完的

第 19.8 节把本实现的输出描述成"按固定缩进逐行打印"的静态树。
真实引擎是**拉模型（pull-based）**：上层算子向下层要一个批次，
下层给一个，算完再要下一个。这件事有三处可以直接观察到。

**第一处，`to_batches()` 返回的是一个流对象，不是列表。**

```python
r = t.search([1.0, 1.0, 0.5]).limit(100).to_batches(batch_size=7)
print(type(r))          # <class 'pyarrow.lib.RecordBatchReader'>
print([b.num_rows for b in r])
```

实测输出是 **15 个批次、每批 7 行**——100 行 = 14 × 7 + 2，
最后一批 2 行。不传 `batch_size` 则是一批 100 行。
**这个参数就是"流的分段粒度"**，而它是有实际后果的：
批次越小，第一条结果到得越早、峰值内存越低，
但每条批次的固定开销摊得越多。I.8 会回到这个权衡。

**第二处，`to_batches()` 的签名里有 `timeout`。**
一个已经算完的结果不需要超时参数。这个参数存在的唯一理由是
**迭代过程中可能等**——它给了调用方一个"等到某个点就放弃"的手段。
配合上一条读：`to_batches()` 是**惰性**的，你不迭代它就不继续算。

**第三处，也是最能说明问题的一处：那棵树上 `LanceRead` 出现了两次。**

```
      LanceRead: ..., projection=[id],   source=stream(_rowid), metrics=[rows_scanned=5,   bytes_read=1.30 K, requests=2]
        ...
                LanceRead: ..., projection=[vector], num_fragments=1, metrics=[rows_scanned=2.00 K, bytes_read=28.14 K, requests=3]
```

**同一个表被读了两次，而且读的是不同的列、不同的行数。**
先读 `vector` 一列、把 2000 行全过一遍算距离（`rows_scanned=2.00 K`），
排完序之后**只对活下来的 5 行**回表读 `id`（`rows_scanned=5`）。
这就是第 10.8 节讲的**延迟物化**，只不过在真实引擎里
它不是一个"优化开关"，而是**两个物理读取算子**。
收益也可以直接读出来：28.14 KB 对 1.30 KB。
如果反过来（先读全列再过滤），第二次读要付的是 2000 行的 `id` 列。

**这也解释了 `SortExec` 为什么在树的下方。** 数据是自下而上流的：
离叶子近的算子先跑。`SortExec` 在 `LanceRead(vector)` 之上、
`LanceRead(id)` 之下，所以它拿到的输入是"全量距离"，
产出的输出是"5 个行地址"——**上面那次回表读的是下面那次排序的结果。**

## I.6 TopK 的提前终止：`filter=` 那串东西是什么

`SortExec` 那行的末尾有一段很容易被当成噪音跳过的内容：

```
SortExec: ..., TopK(fetch=5), expr=[_distance@2 ASC NULLS LAST, _rowid@1 ASC NULLS LAST], preserve_partitioning=[false], filter=[_distance@2 < 0.25 OR _distance@2 = 0.25 AND _rowid@1 < 17179869185]
```

三个值得拆开看的东西。

**`TopK(fetch=5)` 而不是 `Sort`。** 这不是"排完再取前 5"，
而是**堆式 Top-K**：维护一个大小为 5 的堆，每条新数据来的时候
只和堆顶比一次。所以它的 `elapsed_compute`（`341.10µs`）
几乎和输入行数**无关**，而正排全量是 O(n log n)。

**`filter=` 是 TopK 的剪枝阈值，而且是动态收紧的。**
`_distance@2 < 0.25` 的意思是"堆满了之后，距离不小于第 5 名
就不用进堆了"。第 19.9 节讲 `limit` 下推时说的"TopK 的提前终止"，
落到实处就是这个谓词。**把两次运行的同一行并排看，
能看出它是活的**：2000 行那次是 `_distance@2 < 21.25`，
4000 行 8 片段那次是 `_distance@2 < 0.25`——阈值随数据分布变，
它是运行时状态，不是计划的一部分。

**`AND _rowid@1 < 17179869185` 是并列打破。**
距离相等时按行号定序，保证**同一份数据每次查询返回的顺序完全一致**。
这一条对产品逻辑很重要：没有它，"距离相同的两个结果谁在前"
就是不确定的，翻页、去重、缓存都会飘。
第 11.5 节讲 `NaN` 与 null 的语义分歧是同一类关切——
**确定性不是自动的，是显式写进排序键的。**

`row_replacements=5` 也解释一下：它统计堆里被替换掉多少次，
5 行输入换 5 次说明堆一直在动。这个数在排查"TopK 是不是退化成全排序"
时有诊断价值——如果输入 10 万行、`row_replacements` 也是 10 万，
说明阈值没起作用。

## I.7 这条流水线的内存边界在哪

把整棵树的内存形状画出来，只有三个地方会累积：

**会累积的只有一处：`SortExec` 的堆。** 它的大小是 **O(k)**，
和输入行数无关——`fetch=5` 就是 5。**这是整条链上唯一一处
"必须把状态留到最后"的算子**，也正因为如此，它是唯一一处
不会随数据量线性增长的地方。

**它的上游 `KNNVectorDistance` 是 O(1) 的**：来一批算一批、算完就扔。
`output_rows=2.00 K` 说的是累计输出了 2000 行，
不是说它同时持有 2000 行。

**它的下游全是流式的**：`FilterExec` / `GlobalLimitExec` / `ProjectionExec`
都是逐批处理。真正"物化"的动作只有最后一次回表读 `id`，
而那次只读 5 行。

**所以"向量检索的内存上限由 k 决定，不由表大小决定"这句话，
在这棵树上是看得见的。** 危险的反面是把 `k` 开得很大：
`limit(10000)` 会让那个堆变成 O(10000)，而第 19.9 节讲的
深分页问题（`offset` 不下推）会让它更糟——`offset=10000`
意味着前面 10000 行照样被算出来再丢掉。

**但是有一个前提要说清楚：上面这套只对"单机、单查询"成立。**
真正的峰值内存还要乘以并发查询数，而这一层在计划树上完全看不出来——
它是调度器的事。I.9 会讲本实现和真实系统在这一层的差距。

## I.8 那些够不着的旋钮：一条由缺件划出的边界

第 24.8 节标题是"还没量到的几个旋钮"，这一节补上**为什么量不到**——
原因不是懒，是**这些旋钮根本不在 `lancedb` 这个包里**。

在 `LanceTable` 和连接对象上把所有和扫描相关的名字找一遍：

```python
[n for n in 公开名字 if any(k in n.lower() for k in
    ('readahead', 'batch', 'thread', 'concurr', 'prefetch', 'parallel'))]
# -> []
```

**空列表。** 查询构建器上能调的只有语义参数
（`nprobes` / `refine_factor` / `ef` / `fast_search` …），
执行层面的**并发度、预读深度、I/O 缓冲区大小一个都没有**。
它们属于 `lance`（导入名 `pylance`）里 `Dataset.scanner()` 那一层，
而本机没装这个包。

**这不是"本教程没查到"，是一个可验证的边界**：
想知道"Lance 的查询默认开几路并行"，光有 `lancedb` 是答不出来的。
能拿到的只有替身——`to_batches(batch_size=...)` 控制了
**客户端这一侧**的分段粒度（I.5 量过），但它改不了引擎内部的并行度。
**把"我调了 batch_size"当成"我调了扫描并行度"，是这一层最容易犯的错。**

还有一个名字上的坑，正好在附录 H 里已经踩过：
`batch_size` / `batch_readahead` / `fragment_readahead` 这三个名字
**在两侧都存在，但指的是两件事**。附录 H 讲的那一组是
**pyarrow 的 parquet scanner**（摄入路径，读外部文件进来），
值分别是 131072 / 16 / 4；Lance 自己的扫描器有**同名**参数，
在 `pylance` 里。**同名不同库**，看到这三个词先确认是哪一侧的，
和附录 H 里"哪一层的内存上限"是同一个要求。

## I.9 本实现为什么是"逐片段、单线程"

到这里可以正面回答 §19.9 那句话了。本实现的 `Scanner`
**不做并行、不做流水线重叠**，逐片段顺序扫，一次只持有一个批次。
这不是省事，是一个刻意的取舍，理由有三条：

**第一，并行会掩盖这一篇想讲的东西。** 一旦引入线程池，
`elapsed_compute` 就不再是"这个算子花了多久"，而是"这个算子在多少
毫秒的墙钟时间里被调度过"——和真实引擎那两个数的区别一样。
教学实现里把并行去掉，`task_wait_time` 那种"等 I/O"的信号反而更干净。

**第二，并行是唯一一个"错了不会报错"的优化。** 单线程的实现输出
逐位可复现；引入并行之后，批次到达顺序、TopK 的并列打破、
浮点累加顺序都会变，测试就从"断言相等"退化成"断言集合相等"。
第 24 章反复强调"挂钟时间不能当断言"，而并行是挂钟噪声的主要来源。

**第三，也是最实际的一条：本实现的性能瓶颈不在并行上。**
它是纯 Python + numpy，单个算子的 `elapsed_compute` 比真实引擎高
一到两个数量级。在这个前提下加线程池，收益会被 GIL 吃掉，
而复杂度全额到账。

**代价也要照实说。** 本实现因此**没法演示**几件事：
多片段查询的实际加速比、合并点的开销、调度器的公平性、
以及"并发查询数 × 单查询内存 = 峰值内存"这条生产上最常见的
容量公式（第 24.7 节提过这条，但量不了）。
这几件事在本书里都是**用真实引擎的数字来讲的**，
不是用本实现推算的——这也是 I.2 到 I.8 全部用真实
`analyze_plan()` 而不是本实现的 `explain()` 的原因。

## I.10 小结

**真实引擎是 DataFusion。** 计划里那些 `*Exec` 是 DataFusion 的物理算子，
Lance 的读取算子挂在上面。这不是实现细节，它决定了
过滤表达式为什么是 SQL 语法、列引用为什么长成 `_distance@2`。

**每个算子自带账本，`elapsed_compute` 和 `elapsed` 不是一回事。**
2000 行的查询整棵树花了 12.9 ms，所有算子的计算时间加起来不到 1 ms，
剩下的被最内层 `LanceRead` 的 `task_wait_time=11.18ms` 拿走了。
**先分清是算还是等，再谈优化。**

**并行度写在 `output_batches` 上，合并点写在
`preserve_partitioning=[false]` 上。** 8 个片段 → 8 个批次 → 1 个批次。
并的地方要同步，同步的地方就是瓶颈。

**延迟物化在真实引擎里是两个物理读取算子**，不是开关：
先读 `vector` 扫 2000 行，再读 `id` 只读 5 行（28.14 KB 对 1.30 KB）。
`SortExec` 夹在两者之间，所以它排的是"全量距离"、
产出的是"5 个行地址"。

**内存上限由 k 决定，不由表大小决定**——整条链上只有 TopK 的堆是
O(k)，其余都是流式。但这是单查询的口径，乘上并发数才是在规划的容量。

**执行层的旋钮不在 `lancedb` 里。** 并发度、预读深度、I/O 缓冲
全在 `pylance` 那一层，本机没有这个包，所以本书里这些参数
一律标"引用文档"。**能确认的边界比含糊的覆盖面有用。**

后面还有一层和这一篇互补。这一篇讲的是"数据在引擎里怎么流"，
而**字节以什么形式交到别的系统手里**是另一条独立的路——
零拷贝、Arrow C Data Interface、以及为什么 `to_arrow()` 的结果
能直接喂给 DuckDB 而不经过序列化。那是附录 J 的内容，
也是这一篇留下的最后一个悬念：`LanceRead` 吐出来的 `RecordBatch`，
最终是怎么变成你手里那个对象的。
