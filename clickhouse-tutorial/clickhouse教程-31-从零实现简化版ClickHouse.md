# 第 31 章 从零实现简化版 ClickHouse

读了前面三十章,你对 ClickHouse 的每一块应该都有概念了,但这些概念是**分开**装进脑子里的:列存、granule、稀疏索引、part、processor、聚合状态、格式、编解码器……它们之间的依赖关系、谁必须在谁之前、拿掉哪一块整个东西就塌了,光靠读文档很难有体感。这一章换一个方向:**把这些部件用 Python 从零重写一遍**,规模控制在能读完、能跑起来、能改动的量级,然后逐块对照真实 ClickHouse,看它"必须这么做"的理由是什么,以及我们的简化在哪一步付出了代价。

配套实现在 `mini-clickhouse/` 目录下,全部源码在 `mini-clickhouse/minich/` 里:**123 个 `.py` 文件、86 242 行**。这一章是**导览**,不是手册——手册是第 34 章(附录 B),那里有完整的文件树、能力矩阵与 API 签名速查。本章只做三件事:讲清它的架构骨架、讲清十个最关键的设计各自的取舍、以及亲手把它跑一遍。

本章与第 34 章里的所有数字(行数、文件数、注册表条目数)都是在同一次快照上跑命令数出来的。这个实现还在持续演进,如果你手上的版本数字对不上,按第 34 章 B.1 节给的命令重数一遍即可——结论不会变,数字会。

## 31.1 为什么用 Python 写

选语言这件事在"实现一个数据库"的语境里是**唯一一次最重要的取舍**,先说清楚。

Python 换来的是三件东西。

**读得懂。** 整个项目没有模板元编程、没有宏、没有手动内存管理。`IColumn::filter` 在真实 ClickHouse 里是模板特化加 SIMD 的产物,在这里就是一个列表推导。你可以从 SQL 文本一路读到磁盘字节,中间不跳任何一层。

**跑得起来。** 不需要编译、不需要装 ClickHouse、不需要起服务(单机 `Context` 就够)。一行 `python -c` 就能执行 SQL。

**能验证。** 每个设计决策都对应一段可直接执行的代码,所以你不需要"相信"文档——你可以改一行、重跑、看输出变了没有。第 34 章的能力矩阵就是靠这个方式逐条核出来的。

代价也必须写清楚,否则你一定会误用它的数字。

**它不可能快,而且不是"慢一点"的量级。** 真实 ClickHouse 的 `INSERT` 吞吐是每秒数百万行;mini-clickhouse 在同一台机器上插入 10 万行需要 **2.23 秒**,也就是每秒约 4.5 万行。`GROUP BY` 的差距更大,因为真实 ClickHouse 里一次 `plus(a, b)` 调用是一条向量化指令处理几千行,而这里是一行一次 Python 函数调用加一次对象装箱。**差的是两个数量级,而且这个差距不会因为"优化"而缩小——它是解释型 Python 与向量化 C++ 之间的结构性差距。**

所以本项目里所有性能讨论**只能定性**。它能告诉你"列裁剪在机制上减少 IO",但它**不能**告诉你"列裁剪带来 30 倍加速"。凡是本章出现具体秒数的地方,唯一合法的读法是"这一步在纯 Python 里要花多久",而不是"ClickHouse 要花多久"。第 28 章讨论性能时用的是真实 ClickHouse 的数字,不要拿这一章的秒数去对照。

它也没有试图"装作很快":`EXPLAIN PIPELINE` 的输出第一行就直接写着"(mini-clickhouse 分段执行,不是一张完整的 processor 图)",第 10 章已经引过这段输出。

最后交代一个版本坐标:mini-clickhouse 对标的真实 ClickHouse 版本是 **25.3**,这个值写在 `minich/__init__.py` 的 `TARGET_CLICKHOUSE_VERSION` 里,并且 `SELECT version()` 会返回 `25.3.1.1`。书中所有"真实 ClickHouse 的行为"都以 25.3 为准。版本不同的地方差异可能很大(比如 24.x 之后 `JSON`、`Variant`、`Dynamic` 这几个类型的地位一直在变),所以这个坐标必须钉住。

## 31.2 架构总览:一个列式数据库的部件清单

把 `minich/` 的包结构摊开,得到下面这张自上而下的图。它回答本章最核心的问题:**一个列式数据库最少需要哪些部件?**

```text
┌─────────────────────── ① 前端(Parsers/)────────────────────────────┐
│  minich/sql/lexer.py     文本 → Token                              │
│  minich/sql/parser.py    Token → AST(递归下降 + 有界回溯)          │
│  minich/sql/ast_.py      AST 节点定义 + to_sql() 回打              │
└──────────────────────────────┬─────────────────────────────────────┘
                               ▼
┌─────────────────────── ② 编排(Interpreters/)───────────────────────┐
│  minich/execution/interpreter.py                                    │
│      get_interpreter(ctx, ast) → IInterpreter → QueryResult         │
│  minich/execution/analyzer.py    表达式分三段:聚合前/聚合/聚合后   │
│  minich/execution/optimizer.py   四条确定性改写规则                 │
│  minich/execution/explain.py     六种 EXPLAIN 的输出                │
└──────────────────────────────┬─────────────────────────────────────┘
                               ▼
┌─────────────────────── ③ 执行(Processors/)─────────────────────────┐
│  minich/execution/processors.py                                    │
│      IProcessor + Port + Status 四态 + 各类 Transform/Source/Sink   │
│  minich/execution/expression.py   AST 表达式 → IColumn             │
│  minich/execution/joins.py        哈希连接 / strictness / ASOF      │
│  minich/execution/window.py       窗口函数                          │
└──────────────────────────────┬─────────────────────────────────────┘
                               ▼
┌─────────────────────── ④ 存储(Storages/)───────────────────────────┐
│  minich/storages/base.py    IStorage + StorageFeatures + 引擎注册表 │
│  minich/storages/merge_tree/   ← 唯一"真正的"存储引擎              │
│      disk.py / marks.py / part.py / key_condition.py /             │
│      skip_index.py / merge_policy.py / merger.py /                 │
│      mutations.py / ttl.py / merge_tree.py / engines.py            │
│  minich/storages/{memory,log,null,set,join,buffer,merge,view,…}.py  │
└──────────────────────────────┬─────────────────────────────────────┘
                               ▼
┌─────────────────────── ⑤ 支撑层(横向)────────────────────────────┐
│  minich/types/           DataType:类型给"元数据"                   │
│  minich/columns/         IColumn:列给"存储"                        │
│  minich/block.py         Block / Chunk / ColumnWithTypeAndName      │
│  minich/serialization.py ISerialization:列 → 字节                  │
│  minich/compression.py   ICompressionCodec:字节 → 更小的字节       │
│  minich/formats/         FORMAT:Block → 文本/二进制                │
│  minich/functions/       标量函数库(注册表)                       │
│  minich/aggregates/      聚合函数 + 组合器                          │
│  minich/system/          43 张 system 表                            │
│  minich/settings.py      124 条设置                                 │
│  minich/keeper.py        迷你 Keeper;minich/replication.py 复制    │
│  minich/dictionaries.py  外部字典;minich/access.py 访问控制        │
│  minich/util/            哈希/键/转义/计时/日志(不依赖任何上层)     │
└─────────────────────────────────────────────────────────────────────┘
```

这张图里有一个**层级纪律**,它比图本身更重要:箭头只向下和横向,**没有向上的箭头**。`types` 不知道 `storages` 存在,`util` 不知道有"表"和"查询"这回事。所以当你改动一个部件时,你不知道该看哪里,就沿着箭头**往下**找——被破坏的东西一定在你的下游。

包结构与真实 ClickHouse 的源码目录是**刻意对齐**的,这样读者拿真实源码对照时不会有割裂感:

| mini-clickhouse | 真实 ClickHouse(`src/`) | 职责 |
| --- | --- | --- |
| `minich/errors.py` | `Common/ErrorCodes.cpp` | 错误码与异常 |
| `minich/types/` | `DataTypes/` | 类型的元数据 |
| `minich/columns/` | `Columns/` | 列的物化 |
| `minich/block.py` | `Core/Block.h` / `Chunk.h` | 数据块 |
| `minich/serialization.py` | `DataTypes/Serializations/` | 列的内存/磁盘布局 |
| `minich/compression.py` | `Compression/` | 编解码器 |
| `minich/sql/` | `Parsers/` | 词法、语法、AST |
| `minich/execution/` | `Processors/` + `Interpreters/` | 执行与编排 |
| `minich/functions/` | `Functions/` | 标量函数 |
| `minich/aggregates/` | `AggregateFunctions/` | 聚合函数 |
| `minich/storages/` | `Storages/` | 表引擎 |
| `minich/system/` | `Storages/System/` | system 表 |
| `minich/keeper.py` | `Coordination/` | Keeper |
| `minich/access.py` | `Access/` | 权限 |

真实 ClickHouse 里还有几块这里**没有**对应物:向量化执行与 SIMD、多线程调度器、`QueryPlan` 这层逻辑计划、S3/HDFS 的 `IDisk` 实现、代价模型。它们不是"以后补",而是本项目明确排除的范围(§31.14 逐条列)。

## 31.3 关键设计一:DataType 与 IColumn 的两段式

**真实 ClickHouse 为什么这么做。** 一个数据库里"类型"这个词承担了两份工作:一是**元数据**(这个值占几字节、能不能为 NULL、怎么跟另一个类型相加),二是**存储**(这些值放在哪里、怎么增删改)。把两份工作塞进一个类里,会出现一个绕不过去的矛盾:**同一个类型可以有多种物化方式**。`UInt64` 的一列,普通情况下是一个连续的 `UInt64` 数组;但如果它取值区间很窄、大量重复,真实 ClickHouse 会用 `ColumnSparse` 把它存成"值 + 下标"两段;如果它只有几个不同值,`LowCardinality` 会存成"字典 + 索引"。这些物化方式的**类型语义完全相同**,但内存布局差很多。

所以 ClickHouse 把它切成两个继承体系:`IDataType` 只回答"我是什么",`IColumn` 只回答"我怎么存"。`IDataType::createColumn()` 是两者之间的唯一桥梁——类型负责造出它自己的默认物化。

**我们怎么实现。** 同样的切分,一对一:

- `minich/types/base.py` 定义 `DataType` 抽象基类,`minich/types/scalars.py`、`minich/types/composite.py` 是具体类型;`minich/types/__init__.py` 是**类型工厂**,把 `"Array(Nullable(UInt8))"` 这样的字符串解析成类型对象,对应真实的 `DataTypeFactory`。注册表里有 **56 个类型名**,从 `UInt8` 到 `AggregateFunction`、`Variant`、`JSON`。
- `minich/columns/base.py` 定义 `IColumn` 抽象基类与 `ColumnVector`,另有 `ColumnString`/`ColumnFixedString`(在 `string.py`)和 `Nullable`/`Array`/`Tuple`/`Map`/`LowCardinality`/`Sparse`/`Const`/`AggregateFunction`(在 `composite.py`)。

类型工厂有个细节值得注意:**参数不一定是类型**。`Array(UInt8)` 的参数是类型,`FixedString(16)` 的参数是整数,`DateTime('UTC')` 是字符串,`Enum8('a' = 1)` 是"名字 = 整数",`AggregateFunction(quantiles(0.5), UInt8)` 的第一个参数是**带参数的函数名**。所以工厂函数 `get_data_type(name)` 拿到的是原始参数文本(`list[str]`),由每个类型自己决定怎么解析——这也是真实 `DataTypeFactory::get` 的做法。

**差异在哪。** 真实 ClickHouse 的列有两类实现并列存在:列级的编码列(`ColumnLowCardinality`、`ColumnSparse`)和序列化级的编码(`SerializationLowCardinality`)。mini-clickhouse **只做前者**,因为本项目的教学重点是"数据长什么样",不是"字节怎么写"。所以如果你在 `minich/columns/composite.py` 里看到 `ColumnSparse`,不要以为 `minich/serialization.py` 里也有对应的 `SerializationSparse`——没有。

## 31.4 关键设计二:Block 与 Chunk

**真实 ClickHouse 为什么这么做。** 早期版本只有 `Block`:它同时带着表头(列名 + 类型)和数据。问题出在数据块的数量级上——一次大查询会流过上百万个块,如果每个块都自带一份完整的表头(几十个列的 `ColumnWithTypeAndName` 对象),光表头的内存和构造开销就很可观。于是引入了 `Chunk`:表头被提出来,由一个 `Block` 持有并在所有块之间**复用**,`Chunk` 只带一个指向它的引用加自己的数据列。

**我们怎么实现。** `minich/block.py`(546 行)里三个类:

- `ColumnWithTypeAndName`——名字 + 类型 + 列,最小的三元组;
- `Block`——一个 `ColumnWithTypeAndName` 列表,天然就是一个**表头**(零行)+ 数据(有行)。它提供 `clone_empty()`(复制表头、不带数据)、`clone_empty_without_columns()`、`merge()`、`filter()`、`permute()`、`slice()`、`take()` 等一整套列级操作。
- `Chunk`——`__init__(header, columns, num_rows)`,提供 `get_header()` / `get_columns()` / `get_block_with_data()`。

**差异在哪。** 这个差异很重要,因为它直接对应第 10 章讲过的执行模型差异。

真实 ClickHouse 的流水线上**全程只传 `Chunk`**,`Block` 退化成"只在需要在边界上携带表头时临时构造"的东西。mini-clickhouse 因为采用**分段执行**(§31.8),阶段之间需要把中间结果物化下来,而物化的自然形态是 `Block`(表头 + 数据一体)。所以你在 `interpreter.py` 的 `_execute_pipeline` 里会看到一句明确的注释:"mini-clickhouse 的分段执行需要在阶段之间传递 Block,所以这里做一次转换"。转换本身是 `chunk.get_block_with_data()`,而**这次转换就是内存峰值的来源之一**——真实 ClickHouse 没有这一步。

真实 ClickHouse 还有一个 `Block` 携带的东西:`BlockInfo`(溢出标记、bucket 号,供分布式查询与 `GROUP BY` 溢出写盘时用)。mini-clickhouse 里有 `BlockInfo` 这个类,但流水线里**根本不用它**——本项目既不做两级哈希表,也不做聚合溢出写盘。分组数超过 `max_rows_to_group_by` 时它直接抛 `TOO_MANY_ROWS`(见 `processors.py` 里 `_groups_overflow` 那段),而不是像真实 ClickHouse 那样切桶、写盘、或将 `group_by_overflow_mode` 设成 `any` 后接受近似结果。

## 31.5 关键设计三:列存的三次裁剪

**真实 ClickHouse 为什么这么做。** 列存的第一性原理是"IO 量与用到的数据量成正比,而不是与表的大小成正比"。这句话要成立,需要三级筛选依次收敛,而且**便宜的必须排在贵的之前**:

1. **分区裁剪**——先看目录名。分区键写在 part 的目录名里(`202401_1_1_0`),所以"只需要 2024 年的数据"这一步连 part 里的文件都不用打开,纯粹是字符串比较。
2. **granule 裁剪**——再看稀疏主键索引。每个 granule 的第一行键值存在 `primary.idx` 里,二分一次就知道"该读第 7 到第 12 个 granule"。这一步付出的代价是解压几十 KB 的索引,换来的是跳过几 GB 的数据。
3. **列裁剪**——最后看用了哪几列。这一步是列存的"底价":`SELECT count() FROM t WHERE d >= '2024-03-01'` 在一张 20 列的表上,只碰 `d` 这一列的 `.bin`,另外 19 列的字节**一个都不读**。

**我们怎么实现。** 三次裁剪的**输入**在接口上是一个 dataclass:`ReadContext`(`minich/storages/base.py`)。它的字段按"谁来填"分三类,而这个区分比字段本身重要:

```python
ctx: Any = None                            # 会话

# ① 执行器填进来的 —— 真正决定"读多少"
where:    Any = None       # 查询的过滤条件(AST 原样传,引擎自己去分析)
prewhere: Any = None       # 同上,来自 PREWHERE
settings: Any = None       # 查询级设置(use_skip_indexes 这类开关从这里进)
final:    bool = False     # FROM t FINAL

# ② 执行器**可以**填、引擎也认,但当前执行器留空的"透传位"
columns:       Optional[List[str]] = None   # 列裁剪:要读哪些列(None = 全部)
key_analysis:  Any = None                   # 已经算好的主键分析结果(MergeTree 是 KeyCondition)
partition_ids: Optional[set] = None         # 已经算好的分区集合
limit:         Optional[int] = None         # LIMIT 下推

# ③ 其余
sample: Any = None
query_info: Any = None
```

存储层三条通路都是实的,而且**都从 `where` / `prewhere` 自己分析**:

- 列裁剪:`MergeTreeData._plan_columns(requested, where)` 算出"请求的列 ∪ 谓词依赖的列 ∪ 家族列",决定打开哪些 `.bin`;`ReadContext.wanted_columns(available)` 再与表里真有的列求一次交(这一步专门用来挡掉 ALIAS 列)。它的输入只有 `columns` 这一个透传位,而执行器目前传 `None` —— 所以从 SQL 这一侧看,它读的永远是全部列。
- 分区裁剪:`MergeTreeData._prune_partitions(where, rc, events)` 从 `where` 里抠出分区键上的比较,返回一个分区 ID 集合,随后 `parts = [p for p in self.get_data_parts() if selected_partitions is None or p.get_partition_id() in selected_partitions]`。如果 `rc.partition_ids` 非空,它就直接用那个集合、自己不再分析。
- granule 裁剪:`MergeTreeData._mark_ranges_for_part(part, where, rc, events)` 先看 `rc.key_analysis`;它是空的,于是就地 `KeyCondition(...)` + `add(where)` 现造一个,再去 `primary.idx` 上二分出 `MarkRange`。`minich/storages/merge_tree/key_condition.py`(1 375 行)就是这段逻辑——它是"ClickHouse 为什么快"的那一半答案,也是 `merge_tree/` 这个子包里除引擎本体之外最厚的一个文件。

于是**三类字段的语义完全不同**,值得分开记:`columns` / `key_analysis` / `partition_ids` / `limit` 是"**如果调用方已经算好了,就省得我再算一遍**"的捷径位;`where` / `prewhere` 是"**我什么都没算,你自己看着办**"的原始输入。前者空着只是少走一条捷径,后者空着才是**功能性的**失效——因为引擎手里没有条件,三条裁剪一条也做不了。

**差异在哪:接线状态随实现演进。** 三次裁剪的机制在存储层里是完整的,但"解释器有没有把 `where` / `prewhere` 交给 `ReadContext`"是另一回事,而且**这条接线在实现长起来的过程中改动过多次**——这不是谁改错了,是这个实现一直在长。所以这一节给的不是结论,是一个**方法**。

先看接口。解释器构造 `ReadContext` 的地方(`interpreter.py` 的 `_read_storage_table`)长这样:

```python
read_ctx = ReadContext(
    ctx=self.ctx,
    columns=None,                    # 列裁剪:不填 → 引擎按"全部列"读
    settings=self.ctx.settings,      # 让 use_skip_indexes 这类开关能到引擎
)
read_ctx.where = self._pending_where          # ← 这两行才是关键:把谓词交给引擎
read_ctx.prewhere = self._pending_prewhere
self.read_context = read_ctx

chunks = _call_read(engine, self.ctx, read_ctx, final=ref.final)
```

需要改掉的是一个很自然、但**是错的**心智模型:"`columns` / `key_analysis` / `partition_ids` / `limit` 四个字段填了几个,三次裁剪就生效几条"。事实不是这样:

- `key_analysis` 与 `partition_ids` 在**整个仓库里从来没有被赋过值**——它们只在 `ReadContext` 里声明、在 `_call_read()` 里被原样转发给引擎。它们不是"开关",是"**给调用方留的加速通道**":谁已经算好了 key range / 分区集合,谁就可以填进来让引擎免于重算。执行器选择不填,于是引擎自己从 `where` 算。
- `limit` 同理——填了引擎会提前停,不填就一直读完。所以"`LIMIT` 不生效"这个现象的真正原因不是"引擎不支持",而是**执行器没往这个字段里写值**。
- `columns` 也是透传位,只是它控制的是列裁剪。

所以正确的说法是:**真正驱动三次裁剪的是 `where` / `prewhere` 这两个字段**(以及 `settings` 里的开关)。解释器把谓词 AST 原样交给引擎,引擎再自己决定"哪些分区可以扔、哪几段 granule 可以跳"。`columns` / `key_analysis` / `partition_ids` / `limit` 是四个**空的透传位**,留空意味着"我不替你算,你按条件自己来"——**它们为空不等于裁剪失效**。这也是为什么前面那张字段表把 `where` / `prewhere` 和那四个透传位分开列:混在一起看,就一定会得出错误的结论。

**怎么量。** `ctx.profile_events` 里有一组专门给读路径用的计数器:`SelectedParts`、`SelectedMarks`、`ReadCompressedBytes`、`SelectedRows`。取查询前后的差值就是这条查询真实读了多少:

```python
import tempfile, time
import minich as ch

ctx = ch.Context(data_path=tempfile.mkdtemp())
ctx.execute("CREATE TABLE k (id UInt64, v String) ENGINE = MergeTree ORDER BY id")
ctx.execute("INSERT INTO k SELECT number, toString(number) FROM numbers(200000)")

def measure(q):
    before = ctx.profile_events.snapshot()
    t0 = time.time(); ctx.execute(q); dt = time.time() - t0
    after = ctx.profile_events.snapshot()
    delta = {k: after.get(k, 0) - before.get(k, 0)
             for k in ("SelectedMarks", "ReadCompressedBytes", "SelectedRows")}
    print("%-44s %5.2fs %s" % (q, dt, delta))

measure("SELECT count() FROM k")                    # 平凡 count:该读 0
measure("SELECT id FROM k WHERE id = 100000")       # 排序键等值:该读 1 个 mark
measure("SELECT id FROM k LIMIT 10")                # LIMIT 下推:该提前停
measure("SELECT count() FROM k WHERE v = 'x'")      # v 不在排序键上:该读全部
```

最后一次跑(表 20 万行、25 个 mark)的结果是:

```text
SELECT count() FROM k                        0.00s {'SelectedMarks': 0, 'ReadCompressedBytes': 0, 'SelectedRows': 0}
SELECT id FROM k WHERE id = 100000           0.02s {'SelectedMarks': 1, 'ReadCompressedBytes': 16384, 'SelectedRows': 8192}
SELECT id FROM k LIMIT 10                    0.43s {'SelectedMarks': 25, 'ReadCompressedBytes': 400000, 'SelectedRows': 200000}
SELECT count() FROM k WHERE v = 'x'          0.61s {'SelectedMarks': 25, 'ReadCompressedBytes': 400000, 'SelectedRows': 200000}
```

**怎么读这四行。** 每一行都对应一条应该生效(或应该不生效)的裁剪:

- 第一行**生效了**:一个 mark 都没读。`optimizer.py` 的 `optimize_trivial_count_query` 让解释器直接去看 part 的 `count.txt`。
- 第二行**生效了**:25 个 mark 收到了 **1** 个,读了 16 KB 而不是 400 KB——`KeyCondition` 在 `primary.idx` 上二分之后只解压了一个 granule。**这正是 `WHERE id = 100000` 在 20 万行上只要 0.02 秒的原因。**
- 第三行**没生效**:`LIMIT 10` 仍然读完 25 个 mark 才截断。存储层其实**支持**提前停(`MergeTreeData.read()` 里有 `if rc.limit is not None and produced >= rc.limit: return`),只是执行器没往 `read_ctx.limit` 里写值。
- 第四行**正确地没生效**:`v` 不是排序键的前缀,本来就不该能裁剪。**如果一个改动让这一行的 `SelectedMarks` 也降下来了,那就是 bug**——那意味着不该下推的条件被下推了。这是第 24 章要讲的那类"优化器误判引擎能力会产生错误结果而不是慢查询"的活例子。

另外,这四次测量里还看到了 `SelectedRows` 的变化:早先它是 20 万的两倍(源被读了两遍:一次拿表头、一次进流水线),现在是 20 万的一倍——**这个浪费也被修掉了**。这是"分段执行"的副作用之一,真实 ClickHouse 只读一次。

**所以结论该怎么说。** 这里不给"这个实现的三次裁剪是否生效"的定论,因为它随时会变;能给的是:**机制是真的(可以逐个函数读),而生效与否只有 `profile_events` 说了算**(读代码只能看到"函数写没写""参数有没有被填")。本书正文里凡是引用"列裁剪 / granule 裁剪能省多少 IO"的数字,**来源都是真实 ClickHouse**(第 05 章、第 11 章、第 28 章),不是这个实现——这一点在你自己的实验里也必须守住。同样的道理适合本章后面每一个"实测"段落:**先跑,再信。**

还有一条只属于本节的时效事实:这四条裁剪并非一开始就都生效,它们是被逐条接进执行路径的——平凡 count 由 `_try_trivial_count()` 生效,主键 granule 裁剪紧随其后生效(25 个 mark → 1 个),分区裁剪也在补测中确认生效(69 个分区的表上查两个月只读了 3 个 part);而到定稿时,**列裁剪与 `LIMIT` 下推仍未接线**(两个透传位 `columns` / `limit` 还空着)。所以本节描述的是**测量那一刻的实现**,不是 ClickHouse,也不是"这个项目的设计"——你跑的时候大概率又不一样。

这几次测量之间**没有任何人"改错了什么"**,只是实现又长了一段。这正是 `tools/check_docs.py` 要专门加一项"实现规模数字必须与注册表一致"的原因(见第 34 章 §B.5.3):**在一个还在长的项目上写文档,"内容没错但已经过期"是最大的一类错误,而且它靠人眼抓不住。**

## 31.6 关键设计四:granule 与稀疏主键索引

**真实 ClickHouse 为什么这么做。** 如果给每一行都建索引,索引本身就和数据一样大——那不是索引,那是第二份数据。稀疏索引的思路是:**每 8 192 行只记一行的键值**。于是索引大小约等于数据的 `键宽 / 8192`,对 `UInt64` 主键就是 0.1%。这个量级的索引可以整个放进内存,所以定位一个键值是一次内存二分,不是一次磁盘查找。

8192 这个数字是**索引大小与读放大的折中**,不是随便挑的:索引太大放不进内存;granule 太大则二分定位之后还要多读几千行。真实 ClickHouse 还允许自适应粒度(`index_granularity_bytes`,按字节切而不是按行切),因为"8192 行"对一行 1 KB 的日志是 8 MB(合理),对一行 1 MB 的文档是 8 GB(灾难)。

**我们怎么实现。** `minich/storages/merge_tree/marks.py`(568 行)定义了三个概念,模块 docstring 里有一张很好的图,这里复述关键结论:

- **granule** = "若干连续的行",大小由 `index_granularity` 决定。默认常量 `DEFAULT_INDEX_GRANULARITY = 8192`,和真实 ClickHouse 一致;自适应粒度的上限是 `DEFAULT_INDEX_GRANULARITY_BYTES = 10 MiB`,同样一致。
- **mark** = "一个 granule 在**某个列文件**里的位置",三元组 `(offset_in_compressed_file, offset_in_decompressed_block, rows_in_granule)`,存在 `<列>.mrk2` 里。
- **primary.idx** = "每个 granule 的第一行的排序键值"。注意它**列优先**存放:对 `ORDER BY (date, id)` 的四 granule part,文件里先排 `date` 列的全部 granule 值,再排 `id` 列的全部 granule 值。这样读第 1 个键列时是一次连续的读。

`key_condition.py` 负责把 `WHERE` 变成 `MarkRange`。它有一条贯穿全章的铁律,写在模块开头:**能用索引加速的过滤列,必须是排序键的前缀**。`ORDER BY (date, id)` 的表上,`WHERE date >= X` 能用索引,`WHERE id = X` 不能用(因为 `id` 在每个 `date` 段里都从 0 开始),`WHERE url = X` 更不能用。这不是实现缺陷,是稀疏索引的定义决定的——真实 ClickHouse 完全一样。

**差异在哪。** 两处。

第一处是 part 格式里的:真实 ClickHouse 允许**多个 granule 共用一个压缩块**(由 `min/max_compress_block_size` 控制),mini-clickhouse 是"**一个 granule 一个压缩块**"。所以它的 `.mrk2` 三元组里 `offset_in_decompressed_block` **恒为 0**——你读代码时会觉得这个字段没用,这是因为压缩块边界和 granule 边界对齐了。真实实现里它们不对齐,所以这个字段才有意义。

第二处是一个**时效性**的提醒:§31.5 的实验量到 `WHERE id = 100000` 只读了 1 个 mark——也就是说 `KeyCondition` 已经被**接进了 SELECT 路径**,现在真的在生效。它内部的做法是:读 part 时若 `where` 非空,就构造 `KeyCondition` 并据此算出 `MarkRange`(见 `merge_tree.py` 里 `has_usable_ranges()` 那一段),只解压选中的那几段。

这件事的教训与 §31.5 同一条,**这里不再展开**:**"机制存在"和"机制生效"是两件事**,在一个还在演进的项目上后者会变,判断它有没有生效的唯一办法是量(§31.5 的 `profile_events` 脚本),不是读代码。

## 31.7 关键设计五:part 的不可变性、临时 part 与原子改名

**真实 ClickHouse 为什么这么做。** 这是整个存储层最根本的一条决定:**已经写好的 part 永远不改**。`DELETE` 不是删,`UPDATE` 不是改——它们生成一条 mutation 记录,由一条**独立的 mutation 处理路径**(后台资源池的空闲线程,不必等某个合并发生)挑出受影响的 part、重写出一份"改过的"新 part(见第 18 章)。mini-clickhouse 在这里做了简化:它没有那条独立路径,只在 part **参与合并**或执行 `OPTIMIZE` 时才顺带把变更套用上去(§31.15 再提)。

换来的是读路径上的一连串简化。既然文件不会被改写,读的时候就不需要写锁、不需要 MVCC 的版本链、不需要 undo log;正在被查询持有引用的 part 可以放心地被合并任务在后台读取;同一个文件甚至可以被多个查询同时 mmap。**"不可变"不是一个优化,它是让读路径可以做到那么激进的前提条件。**

但不可变带来一个新问题:**一个正在被写入的 part,读到一半怎么办?** 如果写到第 7 个文件时崩了,磁盘上留着一个"缺了几列"的目录;如果它已经出现在目录列表里,下一个查询就会去读它,读到一半报错或者给出错误结果。

真实 ClickHouse 的解法是**先写在谁也看不见的地方,再原子改名**。mini-clickhouse 把这条做成了九个步骤(`minich/storages/merge_tree/part.py` 的模块 docstring 里有完整流程图),关键在最后两步:

```text
⑦ 全部文件写进一个临时目录  tmp_insert_<随机>
⑧ disk.move(tmp, 最终名)     ← 这一步是原子的
```

写 part 的代码在 `part.py` 里是这样的:

```python
tmp_path = disk.temp_path_in(path, prefix="tmp_insert")
disk.create_dir(tmp_path)
try:
    _write_part_files(disk, tmp_path, block, context, granularity, granularities, ...)
    disk.move(tmp_path, path)
except Exception:
    disk.remove_dir(tmp_path)
    raise
```

**我们怎么实现。** `disk.move()` 在 `DiskLocal` 上落到 `os.replace()`,在同一个文件系统里它是原子的。`DiskLocal.atomic_write(path, data)` 是同一个模式的文件级版本:先写 `tmp_<随机>`,再 `os.replace`。为什么要专门有这么一个原语?因为 part 里有一批**小的元数据文件**(`count.txt`、`checksums.txt`、`primary.idx`、`serialization.json`),它们要么全在、要么全不在——半个 `checksums.txt` 比没有 `checksums.txt` 更糟。

part 的名字本身也编码了信息:`format_part_name(partition_id, min_block, max_block, level)` 产出的形如 `202401_1_3_2` 的名字,三段数字分别是"最小块号、最大块号、合并层级"。于是"这个 part 覆盖了哪些写入"和"它被合并过几次"这两件事,不需要任何元数据文件就能从目录名读出来——`system.parts` 里的 `min_block_number` / `max_block_number` / `level` 三列就是靠解析目录名还原的(`_block_range()` 优先用 part 自己暴露的字段,没有才回退到解析名字)。

**差异在哪。** 崩溃恢复这一半这里做得不错,而且做法和真实 ClickHouse 一致:`_load_existing_parts()` 启动时扫数据目录,**先看目录名再打开文件**——`tmp` 开头的目录一律删掉(对应真实的 `clearOldTemporaryDirectories`)。因为提交是原子的,所以"存在但没提交"的目录**一定**是垃圾,这个判断是安全的。`detached/` 目录也有,DETACH PARTITION 把 part 挪出去、ATTACH PARTITION 挪回来,和真实行为的形状一样。

真正的差异在**旧 part 的延迟删除**。真实 ClickHouse 合并后不会立刻删掉被取代的旧 part,默认还要留 480 秒(`old_parts_lifetime`),因为可能有别的查询正持有对它的引用——这就是"磁盘占用比数据量大"最常见的那个原因。mini-clickhouse 里 `old_parts_lifetime` **作为设置项声明了(默认 480)但没有任何地方读它**:被取代的 part 会立刻从磁盘上删掉,同时把它的元数据对象塞进 `obsolete_parts` 字典。这个字典**只增不减**,所以长时间跑很多次合并的进程会持续累积 part 元数据对象。要观察这一点,`describe_stats()["obsolete_parts"]` 会给出现在还挂着多少个。

还有一处差异是格式层面的,写在 `part.py` 的模块 docstring 里:跳数索引在真实 ClickHouse 里是**合并时**才建的(`INSERT` 不建),而这里是**写入时就建**——为的是演示更直观(插入完就能立刻在 `system.parts` 或 `EXPLAIN` 里看到它)。

## 31.8 关键设计六:推送模型与四态状态机

**真实 ClickHouse 为什么这么做。** 教科书里的执行模型是**火山模型**:上层算子调用 `child->next()` 拿一行,一层层递归到底,一个查询就是一条调用栈。它简单,但每次只处理一行,虚函数调用的开销远大于计算本身,而且整条栈跑在一个线程上。

ClickHouse 换成了**推送模型 + 处理器图**:每个 processor 实现一个 `work()`,从输入端口取一个 `Chunk`,算完推到输出端口,返回四个状态之一。第 10 章详细讲了这套模型的收益(向量化、背压、算子级并行、`LIMIT` 反向传播),这里只讲它在本项目里的落地形态。

**我们怎么实现。** `minich/execution/processors.py`(2 094 行)是执行层最大的一个文件,核心是四个类加一个枚举:

- `Status` —— 四态枚举,取值字面量刻意与真实 ClickHouse 一致:`NEED_DATA = "NeedData"`、`PORT_FULL = "PortFull"`、`FINISHED = "Finished"`、`READY = "Ready"`。
- `Port` / `InputPort` / `OutputPort` —— 处理器之间的连接点。端口有两个状态位:`is_finished` 和"有没有数据"。**推送模型下"结束"必须是显式信号**,否则下游无法区分"暂时没有"和"永远没有"——这个区分就是死锁与正常结束的分界。
- `IProcessor` —— 基类,提供 `add_input` / `add_output` / `prepare` / `work` / `has_input` / `has_output` / `all_inputs_finished` / `all_outputs_finished`。
- `ISource` / `ISink` / `ISimpleTransform` —— 三类最常见的形态;具体的处理器有 `SourceFromChunks`、`SourceFromBlocks`、`NullSink`、`CollectingSink`、`ExpressionTransform`、`FilterTransform`、`LimitTransform`、`OffsetTransform`、`MaterializingTransform`、`ArrayJoinTransform`,以及聚合、排序、JOIN 相关的若干处理器。

还有一个细节值得注意:`OutputPort.push()` 返回的不是 `bool` 而是 `PushResult` 枚举,有三个值 `NotNeeded` / `PortFull` / `NeedMoreData`。这不是多此一举——真实 ClickHouse 的 `push()` 也是这么设计的,因为"下游不需要这个块了"和"下游满了"是两种完全不同的处理:前者可以丢弃,后者必须卡住上游。

四态里最容易被低估的是 `PORT_FULL`。它是**背压的载体**:上游想推、下游满,上游就自然卡住。没有它,一个 `SELECT * FROM huge` 会一路把结果堆在内存里;有了它,内存峰值被下游的处理速度自动限制住。

**差异在哪。** 这是本项目与真实 ClickHouse 差距最大的一处,也是第 10 章反复强调的那一点:**mini-clickhouse 的流水线是"分段执行"的**。聚合、排序、JOIN、窗口这些阻塞算子会把中间结果物化成 Block,然后另起一段流水线,而不是串成一整张图。后果有三条:

- **内存峰值更高**。真实 ClickHouse 全程只传 `Chunk`,内存里同时只有 `max_block_size` 行数量级的数据;这里的中间 Block 是整份的。
- **看不到"边算边出"**。真实 ClickHouse 的 `SELECT` 能在扫完第一个 granule 之后就开始往客户端推行结果,第一行延迟极低;这里必须等阻塞算子做完。
- **单线程**。真实 ClickHouse 的并行度来自 `max_threads` 条泳道加 `Resize` 节点的扇出扇入;这里是顺序驱动,`EXPLAIN PIPELINE` 里每个算子都是 `× 1`,并且会打印出 `max_threads = 0`。

不过,四态状态机本身、`Port` 的双向连接、`PushResult` 的三值语义都是照真实实现抄的。所以第 10 章讲的"四态解决了什么问题",在这里可以一行行对着代码看。

## 31.9 关键设计七:两阶段聚合与聚合状态

**真实 ClickHouse 为什么这么做。** 分布式查询里,`GROUP BY` 必须在每个分片上先算一遍,再把结果汇总到发起节点。这就给聚合函数的**中间状态**提了一个硬要求:**状态必须可合并**。

`avg` 是最好的例子。直觉上 `avg` 的状态是"到目前为止的平均值",但那个状态**不可合并**:

```text
分片 A: [10, 20]        平均值 = 15
分片 B: [30, 40, 200]   平均值 = 90

(15 + 90) / 2 = 52.5    ✗ 错
(10+20+30+40+200) / 5 = 60   ✓ 对
```

所以 `avg` 真正的状态是 **(和, 计数)**——两个分量各自可加,合并之后重新做一次除法就对了。这不是 `avg` 的怪癖,而是**所有**聚合函数的共同要求:`sum` 的状态是部分和,`uniq` 的状态是 HyperLogLog 的桶数组,`quantile` 的状态是 TDigest 的草图,`groupArray` 的状态是一个列表。只有 `min` / `max` 这种状态恰好等于最终值的函数是例外。

这条约束还解释了 ClickHouse 一个看起来很奇怪的设计:`AggregateFunction` 类型和 `-State` / `-Merge` 组合器。既然状态是一个可以序列化的值,那你完全可以**把状态存进表里**——`AggregatingMergeTree` 干的就是这件事,它把每天每小时的聚合状态直接落盘,查询时再 `-Merge` 起来。这是 ClickHouse 物化视图提速的核心机制(见第 12 章、第 18 章)。

**我们怎么实现。** `minich/aggregates/base.py`(835 行)定义了 `IAggregateFunction`,它的接口就是围绕"状态"长出来的:

```python
def get_return_type(self) -> DataType: ...
def create_state(self) -> Any: ...          # 造一个空状态
def clone_state(self, state: Any) -> Any: ...
def add(self, state, columns, row) -> None:  # 把第 row 行并进状态
def merge(self, dst_state, src_state) -> None:  # 合并两个状态
def insert_result_into(self, state, column) -> None:  # 状态 → 最终值
def serialize_state(self, state) -> bytes: ...
def deserialize_state(self, data) -> Any: ...
```

`minich/aggregates/` 一共注册了 **65 个基础聚合函数名**(从 `count`、`sum`、`avg` 到 `quantileTDigest`、`uniqCombined`、`groupArrayInsertAt`),另有 **9 个组合器后缀**:`Merge`、`State`、`Resample`、`OrNull`、`OrDefault`、`Distinct`、`Array`、`ForEach`、`If`。组合器在 `get_aggregate_function()` 内部按后缀解析,所以**一个 `sum` 就派生出 `sumIf` / `sumArray` / `sumState` / `sumMerge` / `sumDistinct` / `sumOrNull`……十几个可用的名字**——这正是真实 ClickHouse 里"函数总数看起来上万个"的来源。

`minich/aggregates/average.py` 里 `avg` 的状态就是 `[0, 0]` 这个列表:

```python
def add(self, state, columns, row):
    value = columns[0].get(row)
    if value is None:      # avg 忽略 NULL,而且**不把它计入分母**
        return
    state[0] += value
    state[1] += 1

def merge(self, dst_state, src_state):
    dst_state[0] += src_state[0]
    dst_state[1] += src_state[1]
```

**差异在哪。** 语义上这里和真实 ClickHouse 是一致的,包括几个容易踩的细节:空分组的 `avg` 返回 `nan` 而**不是** 0 也不是 NULL(想要 NULL 得用 `avgOrNull`);`avg` 忽略 NULL 且不把它计入分母。

不一致的地方在**执行形态**上。真实 ClickHouse 的 `AggregatingTransform` 是真的流式的:数据块流过它就累积状态,到 `MergingAggregatedTransform` 才产出结果,中间不需要把整份数据留在内存里。mini-clickhouse 的聚合是**分段执行**的:先把源全部物化成 Block 列表,再逐个 chunk 喂给聚合处理器累计状态,最后单独产出一遍(见 `processors.py` 的 `_drive()`)。结果一样,内存峰值不一样。真实 ClickHouse 的两级哈希表(分组数超 `group_by_two_level_threshold` 时切 256 个桶,让每个桶能被独立线程处理)这里也没有——本项目只有单级哈希表,且不溢出写盘。

## 31.10 关键设计八:表达式求值的分阶段与别名展开

**真实 ClickHouse 为什么这么做。** 一条 `SELECT` 里的表达式并不是都在同一个时刻求值的。看这个查询:

```sql
SELECT toDate(ts) AS d, uniqExact(u) AS uu, uu * 2
FROM hits
WHERE status = 200
GROUP BY d
HAVING uu > 100
ORDER BY uu DESC
LIMIT 10
```

`status = 200` 必须在**原始行**上算——那时分组还没发生,`d` 和 `uu` 都不存在。`toDate(ts)` 要在**分组前**算,因为它是分组键的输入。`uniqExact(u)` 是**聚合**阶段。`uu * 2` 和 `uu > 100` 只能在**聚合之后**算,因为 `uu` 那时才有值。

真实 ClickHouse 用一个 `ExpressionActions` 链来做这件事:分析期算出"哪些表达式在哪一步算",执行期按步插入对应的 `ExpressionTransform`,而 `uu` 这种别名只是一个**指向已算好列的引用**。它还顺手做了一个反直觉但很实用的决定:**别名可以在 `WHERE` 里用**,因为分析期就把别名展开了(`d` → `toDate(ts)`),而不是像 SQL 标准那样禁止。

**我们怎么实现。** `minich/execution/analyzer.py`(791 行)的模块 docstring 里有一张和上面一模一样的三段式图——它不是装饰,它就是这个类要产出的东西。`QueryAnalysis` 里装着三组表达式:`prewhere` / `where`(聚合前)、分组键与聚合参数(聚合)、`select` / `having` / `order_by` / `limit`(聚合后)。

求值器是 `minich/execution/expression.py`(1 623 行)里的 `Evaluator`,它的构造函数签名就是"分阶段求值"的全部秘密:

```python
def __init__(self, ctx, scope, rows=1,
             aggregate_result=None,       # 聚合索引 → 状态列名
             subquery_executor=None,
             lambda_scope=None,
             expression_lookup=None):     # 表达式文本 → 已算好的列
```

`expression_lookup` 是关键的那颗螺丝。聚合后的阶段里,`sum(x)` 不该也不能被重新求值——它要到这张表里取"聚合阶段已经算好的那一列"。`Evaluator.evaluate()` 的第一件事就是查这张表:

```python
if self.expression_lookup is not None:
    hit = self.expression_lookup.get(node)
    if hit is not None:
        return self._fit(hit, getattr(hit, "name", "expression"))
```

名字解析靠 `Scope` —— 一条**链式**的作用域:

```text
Scope(lambda 形参)          ← 最内层,只有 x
     │ parent
Scope(SELECT 的别名 a AS b)  ← 中间层:别名与其原始表达式
     │ parent
Scope(表的列)                ← 最外层:表结构
```

查找从内往外走,**内层优先**。这就是为什么 `arrayMap(x -> x + 1, x)` 里的两个 `x` 含义不同:第一个是 lambda 形参,第二个是表列。

**差异在哪。** 有一处是**放宽**的:真实 ClickHouse 不允许 lambda 形参遮蔽外层列名(会报 `AMBIGUOUS_COLUMN_NAME`),mini-clickhouse 允许。这个差异写在 `Scope` 的 docstring 里,例子在第 07 章。

有一处是**没做**的:真实 ClickHouse 的 `EXPLAIN SYNTAX` 展示的是**优化后**的查询文本(常量折叠、别名展开、谓词下推都会体现在文本上)。mini-clickhouse 的优化器改的是执行计划而不是语法树,所以 `EXPLAIN SYNTAX` 的输出和输入一样,并且它**自己把这件事印在了输出里**:

```text
-- mini-clickhouse: the optimizer rewrites the *plan*, not the AST, so SYNTAX matches the input here
```

这句话的态度值得学:**实现没做到的地方,让输出自己承认**。

## 31.11 关键设计九:序列化——内存布局 ≠ 磁盘布局

**真实 ClickHouse 为什么这么做。** 内存里的列和磁盘上的列是**两个不同的东西**,而这一点最容易被忽略。内存里的 `ColumnString` 是"一个偏移数组 + 一大块字符",因为要支持 O(1) 随机访问;磁盘上的 `String` 列是"每个值前面一个 varint 长度再跟字节",因为要支持流式写入和只读一部分。内存里的 `Nullable(T)` 是"一个 null 位图 + 一个 T 列";磁盘上也是,但多一条规矩:`null` 位置上的 T 值**仍然是合法值**(通常是 0),这样 `filter` 之后不需要重排。

真实 ClickHouse 把这一层单独抽成 `ISerialization`(`src/DataTypes/Serializations/`),而且允许**同一个类型有多种序列化**——`LowCardinality` 有专门的序列化,选中的哪一种会写进 part 的元数据(`SerializationInfo`),读的时候据此反序列化。这就是为什么同一个 `String` 列在不同的 part 里字节布局可能不同。

**我们怎么实现。** `minich/serialization.py`(780 行)里 `ISerialization` 的接口:

```python
def serialize_binary(self, column, out, start, count) -> None: ...
def deserialize_binary(self, data, num_rows, pos=0) -> tuple: ...
def serialize_text(self, value, settings=None) -> str: ...
def deserialize_text(self, text, settings=None) -> Any: ...
def serialize_binary_bulk_prefix(self, ...) -> Optional[SerializeBinaryBulkState]: ...
def serialize_binary_bulk_suffix(self, ...) -> None: ...
def create_bulk_state(self) -> Optional[SerializeBinaryBulkState]: ...
def has_fixed_size(self) -> bool: ...
def fixed_size(self) -> int: ...
```

具体实现是 `SerializationNumber`、`SerializationBigInt`、`SerializationUUID`、`SerializationFixedString`、`SerializationString`、`SerializationNullable`、`SerializationArray`、`SerializationTuple`、`SerializationMap`、`SerializationLowCardinality`、`SerializationAggregateFunction`、`SerializationNothing`。`get_serialization(dtype)` 按类型取一个——**一个类型一种序列化**,没有真实 ClickHouse 那层 `SerializationInfo` 的多态选择。

模块 docstring 里有四条设计约束,它们解释了 ClickHouse 文件"为什么长得那么怪":

1. 按**列**序列化,不按行——这就是"列存"的字面含义。
2. 序列化是**流式**的,而且**可以只读一部分**——`from` / `to` / `n` 这套参数允许"跳过前 100 万个值,只解出后 10 个",因为 granule 定位之后这真的会发生。
3. 变长结构用**偏移数组**而不是分隔符——`String` 写成 `[长度][字节]`,`Array` 写成 `[累积偏移][元素...]`,所以反序列化不需要扫完整个文件就能跳到第 i 个值。
4. 格式由**类型**决定,不由数据决定——同一个列类型在任何 part 里的布局完全相同,这是"零拷贝 mmap 读"的前提。

**把"内存布局 ≠ 磁盘布局"落到字节上。** 光说不够,下面这段是可以直接跑的,它把同一列在内存里和磁盘上的样子都打出来:

```python
from minich.serialization import serialize_binary
from minich.types import get_data_type
from minich.columns import ColumnString, ColumnVector, ColumnNullable
import numpy as np

c = ColumnString()
for v in ("ab", "", "xyz"):
    c.append_data(v.encode())
print("String 内存: offsets=%s chars=%r" % (list(c.offsets), bytes(c.chars)))
print("String 磁盘:", serialize_binary(get_data_type("String"), c).hex(" "))

n = ColumnVector(np.uint64, [1, 300])
print("UInt64 磁盘:", serialize_binary(get_data_type("UInt64"), n).hex(" "))

nn = ColumnNullable(ColumnVector(np.uint64, [1, 0, 3])); nn.null_map = [False, True, False]
print("Nullable 磁盘:", serialize_binary(get_data_type("Nullable(UInt64)"), nn).hex(" "))
```

实测输出:

```text
String 内存: offsets=[2, 2, 5] chars=b'abxyz'
String 磁盘: 02 61 62 00 03 78 79 7a
UInt64 磁盘: 01 00 00 00 00 00 00 00 2c 01 00 00 00 00 00 00
Nullable 磁盘: 01 00 00 00 00 00 00 00  00 00 00 00 00 00 00 00  03 00 00 00 00 00 00 00  00 01 00
```

这三个例子,把上面四条约束各演示了一遍:

- **`String`**:内存里是 `offsets=[2, 2, 5]`(三个值各自的**累积末尾偏移**)+ `chars=b'abxyz'`(全部字节挤成一块)。磁盘上换成"每个值前面一个 varint 长度再跟字节"的**交错**布局:`02 61 62` 是 `"ab"`,`00` 是空串(长度 0,一个字节),`03 78 79 7a` 是 `"xyz"`。同一列,两种形态,一共 8 字节。
- **`UInt64`**:内存和磁盘都是小端 8 字节,所以 `1` 是 `01 00 00 00 00 00 00 00`,`300` 是 `2c 01 ...`(`0x012c = 300`)。定长列的两种布局**恰好重合**——这也是它能整列 `tobytes()` 一次写出、并支持"随机位置直接读"的原因(约束 2、4)。
- **`Nullable(UInt64)`**:磁盘上是**先嵌套列、再空位图**。前 24 字节是三份 `UInt64`(`1`、`0`、`3`——注意 `NULL` 位置的嵌套值是合法的 `0`,不是空洞),最后 3 字节 `00 01 00` 是空位图(第 1 个位置为 1 表示 `NULL`)。位图放最后,是为了让"数据块"和"位图块"在磁盘上各自连续、都是顺序 IO——顺序与直觉相反,但和真实 ClickHouse 一致(这正是 `SerializationNullable` docstring 里那段说明的由来)。

**差异在哪。** 两处,都在 `minich/compression.py` 的模块 docstring 里有明确警告,这里必须复述:

**第一,`LZ4` / `ZSTD` 不是真的 LZ4 / ZSTD。** 真实 ClickHouse 直接调 lz4 库和 libzstd,字节格式有公开规范,别的系统能解;mini-clickhouse 出于教学目的**自己写**了同名的算法——`LZ4` 是自研的 LZ77 块压缩器(哈希链匹配 + 长度/距离对),它在 LZ4 的 token 约定之上**多加了 6 字节魔数 + 4 字节原始长度**的头,而真正的 LZ4 块格式没有这个头。所以:本模块的输出喂给 lz4 库一定报错,反过来也一定被魔数挡回去。`ZSTD` 是自研的 LZ77 + **自适应算术编码**,与真实 zstd(其熵编码是 FSE/Huffman)算法同源但格式完全不同。**不要拿它做跨系统交换。**

例外是那几个数值列专用的编码器——`Delta`、`DoubleDelta`、`Gorilla`、`T64`、`FPC`——它们**是真的算法**;但要说清楚:具体到 `Delta`,它的**字节布局并没有照抄**真实 ClickHouse——真实 CH 在整数值上做一阶差分,这里是在**逐字节平面**上做,两者**字节不兼容**,属一处**已知偏差**(`compression.py` 模块 docstring 明说"不要期待与官方库字节兼容")。它们只在 mini-clickhouse 内部一读一写,不做兼容性声明也不影响正确性。另外 `NONE`(方法字节 `0x02`)、`LZ4`(`0x82`)、`Delta`(`0x83`)、`T64`(`0x84`)、`DoubleDelta`(`0x85`)、`Gorilla`(`0x86`)、`FPC`(`0x87`)、`ZSTD`(`0x90`)这些方法字节是照真实约定用的。

**第二,校验码不同。** 真实 ClickHouse 的 `checksums.txt` 里每个文件存的是一个 **128 位的 CityHash128**(`MergeTreeDataPartChecksum.h` 里 `using uint128 = CityHash_v1_0_2::uint128;`),压缩块头用的也是同一个 16 字节哈希;这里换成 4 字节的 CRC-32(zlib)。**两边差的不只是算法,还有宽度** —— 128 位对 32 位,碰撞概率不在一个量级。(早先本书在这里写的是"真实用 CRC-32C",那是错的:CRC-32C 在 ClickHouse 里是另一个东西,不是 part 文件的校验。)细节见第 05 章 §05.4。

顺带说一个**已知缺口**,它最能说明"设计里有"和"当前生效"是两回事。真实 ClickHouse 会拒绝 `CODEC(Delta)` 这种**只有专用编解码器**的写法——专用编码器只改布局、不压缩,后面必须跟一个通用编码器,否则数据既没变小又读不回来(第 20 章)。这条规则 mini-clickhouse **也写了**:`minich/compression.py` 的 `CompressionCodecFactory.parse_codec_chain` 经它内部调用的 `_validate_chain` 做校验(注意它抛的是 `ILLEGAL_TYPE_OF_ARGUMENT`,即 **Code 24**,不是 49)。但**这条校验根本没接进建表路径**:`parse_codec_chain` 没有任何生产调用方(只有测试与示例直接调它),建表时只把 `CODEC(...)` 当一段原始字符串存下来,压缩时按名字逐个取单个编解码器(`part.py` 的 `_named_codec` 走的是 `CompressionCodecFactory.get`,从不解析整条链,也就轮不到校验)。所以**在当前实现上,`CREATE TABLE ... CODEC(Delta) ENGINE = MergeTree ORDER BY v` 不会报错,插入也不会报错**——这是一段"**设计里有、但当前未接线(死代码)**"的校验,属已知缺口。别拿它去对照真实 ClickHouse 的拒绝行为。

## 31.12 关键设计十:JOIN——哈希连接的 build 与 probe

**真实 ClickHouse 为什么这么做。** ClickHouse 的 JOIN 有两组**正交**的选项,绝大多数困惑都来自没把这两组分开:一组是**方向**(`INNER`/`LEFT`/`RIGHT`/`FULL`/`CROSS`),另一组是**匹配策略**(strictness:`ALL`/`ANY`/`ASOF`/`SEMI`/`ANTI`)。这里有一条必须澄清的常见误解——**默认严格度是 `ALL`,不是 `ANY`**:"ClickHouse 默认 ANY"是流传很广的说法,它在很早的版本里确实如此,但早已不是。

执行上,`ALL`/`ANY`/`SEMI`/`ANTI` 走的是同一套**哈希连接**:把**右表**(build side)按 join key 建成哈希表,再用**左表**的每一行(probe side)去查。这条路径的代价模型是把右表**整份装进内存**,所以"小表放右边"是 ClickHouse 里一条硬经验(第 15 章把它列为最常见的生产事故之一)。为了不让右表撑爆内存,真实 ClickHouse 还提供 `partial_merge`(排序归并,可外存)与 `grace_hash`(分区落盘)两个不那么吃内存的算法,并由 `max_bytes_in_join` 兜底。

还有两处语义细节值得先讲清楚,因为它们是"结果对不对"而非"快不快":其一,**非等值匹配条件并不只是"匹配之后再过滤"**。`ON a.k = b.k AND a.x > b.y` 里那个 `a.x > b.y`,真实 ClickHouse 在 25.3 上能让它**参与匹配本身**——`allow_general_join_planning` 默认开启时,`hash` 与 `grace_hash` 支持来自**不同表**的列之间的不等值条件(口径与边界见 §15.7)。与之对照,mini-clickhouse 走的是老式的"等值键建表、非等值条件留到**匹配之后**逐行求值",这类被推迟求值的条件叫 **残余谓词(residual predicates)**——那是本实现的简化路线,**不是**真实 CH 在 25.3 上的行为。其二,`ASOF JOIN` 是给时序场景的,语义是"给左表每一行配上**不晚于它的最近一行**右表记录"(把最新报价贴到每笔成交上),它需要右表按匹配列有序。

**我们怎么实现。** 全部在 `minich/execution/joins.py`(656 行)里,核心是 `execute_join` 的 build/probe 两段:

```python
# ① build:右表建哈希表 —— key → 匹配到的行号列表
table: Dict[Any, List[int]] = {}
for row in range(right_rows):
    key = build_key(right_key_cols, row)
    bucket = table.get(key)
    if bucket is None:
        table[key] = [row]
    elif settings.strictness != "ANY":   # ANY:只留第一个匹配
        bucket.append(row)

# ② probe:左表逐行查表,产出配对的行号
for row in range(left_rows):
    candidates = table.get(build_key(left_key_cols, row))
    ...
```

四个实现要点,每一项都对应上面的一条语义:

- **键的抽取与残余谓词的分流**由 `extract_join_keys` 完成,它返回 `(左键, 右键, 残余谓词)`。第三个返回值不是可有可无的补充:`ON l.k = r.k AND r.w = 12` 里,`l.k = r.k` 进哈希表,`r.w = 12` 只能进残余谓词。残余谓词在**装配结果块之前**先过滤候选配对:`_filter_pairs_by_residual` 把候选的 `(左行号, 右行号)` 对交给 `_eval_residual_mask` 逐对求值,只留下通过的配对,再交给 `_build_result_block` 拼行——**如果这一步没做,查询会返回本该被过滤掉的行,而且不报错**。
- **strictness 就是哈希表怎么建**。`ANY` 在建表时只保留每个键的第一个匹配(`ALL` 则全留);`SEMI`/`ANTI` 根本不关心匹配了几行,只关心"有没有匹配",所以建表后把每个桶截成一行,probe 时 `SEMI` 只保留有匹配的左行、`ANTI` 只保留没匹配的左行。
- **`USING` 的键列只出现一次**(用左表那一份),同名的非键列加表别名前缀(`a.id`/`b.id`)——这一点和真实 ClickHouse 一致。
- **未匹配的右侧列填什么由 `join_use_nulls` 决定,默认是 `0`**:填**类型默认值**(0/空串),不是 `NULL`。于是你**分不清**"匹配上了且值恰好是 0"和"根本没匹配上";要区分必须显式 `SET join_use_nulls = 1`。这与 SQL 标准相反,是从很早的版本沿用下来的行为。

`CROSS JOIN` 走一条独立路径(笛卡尔积,没有键可言,`O(n·m)`,只该在小表上用)。

**差异在哪。** 语义上,哈希连接的主干是照真实实现抄的——build 右侧、probe 左侧、strictness 决定桶怎么留、残余谓词在装配结果块之前过滤。差异全在**代价与算法覆盖**上,三条:

- **右表整份进内存,而且不做 `max_bytes_in_join` 检查**。真实 ClickHouse 至少有三种算法可选(内存哈希、可外存的 `partial_merge`、可落盘的 `grace_hash`),mini-clickhouse 只有 hash 一种,"右表多大就吃多少内存"——这正是第 15 章那条"把大表放右边就 OOM"的事故在最小实现里的本来面目:它连内存上限都不拦。
- **不做 join 顺序重排**。真实 ClickHouse 也不做(它相信用户把小表放右边),但少了内存兜底之后,"不做"在这里更危险。
- **`ASOF` 是有意简化**。真实 ClickHouse 的 `ASOF JOIN` 要显式声明不等值条件(`ON a.k = b.k AND a.ts >= b.ts`)并按它排序;mini-clickhouse 只按**等值键**建表,于是 `ASOF` 退化成"取该键下最后一个匹配"——只有当右表本身按时间有序时才等价。这个差异明确写在 `_pick_asof` 的 docstring 里。

排查 join 的实战(结果多行、`NULL` 与默认值、`GLOBAL JOIN`)在第 15 章与附录 D。

## 31.13 跑一遍:建表、插入 10 万行、看 system.parts、GROUP BY、OPTIMIZE

前面十一节都是"读",这一节是"跑"。下面每一段输出都是真的执行结果,不是示意。

**怎么跑。** 不需要装任何东西,`cd` 进 `mini-clickhouse/` 然后直接用 `python`:

```bash
cd mini-clickhouse
python -c "import minich as ch; print(ch.Context().execute('SELECT version()').text())"
```

包级导出的是 `__version__`、`TARGET_CLICKHOUSE_VERSION`、`Context`、`Client`、`QueryResult`、`MiniChError` 六个名字——`import minich as ch` 之后 `ch.Context()` 就能用,和 `minich/__init__.py` 模块 docstring 里的示例一致。(如果你读到的版本里 `ch.Context` 不存在,用 `from minich.execution.context import Context` 是等价写法。)

**第一段:建库建表、插入 10 万行。**

```python
import tempfile, time
import minich as ch

ctx = ch.Context(data_path=tempfile.mkdtemp())  # 一个"服务端":数据落到这个目录
print(ctx.execute("SELECT version()").text())

ctx.execute("CREATE DATABASE IF NOT EXISTS demo")
ctx.execute("""CREATE TABLE demo.hits
  (id UInt64, url String, ts DateTime, region String)
  ENGINE = MergeTree ORDER BY id PARTITION BY toYYYYMM(ts)""")

t0 = time.time()
ctx.execute("""
  INSERT INTO demo.hits
  SELECT number AS id,
         concat('/page/', toString(number % 100)) AS url,
         toDateTime('2024-01-01 00:00:00') + number AS ts,
         ['cn','us','eu'][number % 3 + 1] AS region
  FROM numbers(100000)""")
print("INSERT 100000 rows: %.2f s" % (time.time() - t0))
print(ctx.execute("SELECT count() FROM demo.hits").text())
```

真实输出:

```text
┌─version()─┐
│ 25.3.1.1  │
└───────────┘

INSERT 100000 rows: 2.23 s
┌─count()─┐
│  100000 │
└─────────┘
```

**关于那四个 `AS` 别名。** `INSERT INTO ... SELECT` 的目标列对齐由 `InterpreterInsertQuery._align_block` 负责,而它按哪种规则对齐**取决于插入的形态**:`select is not None or format is not None` 时走**按位置对齐**(`positional=True`),否则(VALUES)走**按名字对齐**(`block.try_get_by_name(name)`,有显式列清单时按列清单)。也就是说 `INSERT ... SELECT` 与真实 ClickHouse 一样是**按位置**的,别名只影响结果集的列名、不影响落到哪一列。实测下面两句结果一致:

```text
INSERT INTO d SELECT number, toDateTime('2024-01-01 00:00:00') + number FROM numbers(3)
  读回来:[(0, '2024-01-01 00:00:00'), (1, '2024-01-01 00:00:01'), (2, '2024-01-01 00:00:02')]

INSERT INTO e SELECT number AS id, toDateTime('2024-01-01 00:00:00') + number AS ts FROM numbers(3)
  读出来:[(0, '2024-01-01 00:00:00'), (1, '2024-01-01 00:00:01'), (2, '2024-01-01 00:00:02')]
```

上面例子里的 `AS id` / `AS ts` 因此是**可读性**上的建议,而不是正确性的要求。但既然这两种对齐规则在实践中各有各的坑,值得记住一条**通用纪律**:动手之前先查清这条插入路径是"按名字"还是"按位置"——"按名字对齐"在列名对不上时的失败方式可能是**静默填默认值**,这类错误不报错、只出错数据,是最贵的一类 bug;"按位置对齐"则会在列数对不上时当场报错。这份实现里 `VALUES` 按名字,`SELECT` / `FORMAT` 按位置。

**第二段:看 part。** 10 万行切成了 13 个 granule(10 万 ÷ 8 192 = 12.2,向上取整 13),一个 part,属于分区 `202401`。

```python
print(ctx.execute(
  "SELECT partition, name, rows, marks, part_type, bytes_on_disk "
  "FROM system.parts WHERE active").text())
```

```text
┌─partition─┬─name─────────┬─rows───┬─marks─┬─part_type─┬─bytes_on_disk─┐
│ 202401    │ 202401_1_1_0 │ 100000 │    13 │ Wide      │        815513 │
└───────────┴──────────────┴────────┴───────┴───────────┴───────────────┘
```

`marks = 13` 是这个实现里最能"一眼验证理论"的数字:它同时印证了 granule 大小是 8 192、`marks` 就是 granule 个数、以及 `primary.idx` 的条目数与 granule 数相同。`part_type = Wide` 表示每列一个文件(真实 ClickHouse 由 `min_bytes_for_wide_part` 决定,默认 10 MiB 以下用 `Compact`;这里直接是 `Wide`)。

**第三段:`GROUP BY`。**

```python
print(ctx.execute("SELECT region, count() AS c, round(avg(id)) AS avg_id "
                  "FROM demo.hits GROUP BY region ORDER BY region").text())
```

```text
┌─region─┬─c─────┬─avg_id──┐
│ cn     │ 33334 │ 50000.0 │
│ eu     │ 33333 │ 50000.0 │
│ us     │ 33333 │ 49999.0 │
└────────┴───────┴─────────┘
```

三个分组的平均 id 都落在 50 000 附近,因为 id 是 0..99999 顺序生成的、按 `region` 三取一取模分配——这个结果本身就说明了 `GROUP BY` 的语义是对的。

**第四段:多次插入 → 多个 part → `OPTIMIZE` 合并。**

```python
ctx.execute("CREATE TABLE t (id UInt64, url String) ENGINE = MergeTree ORDER BY id")
for i in range(3):
    ctx.execute("INSERT INTO t SELECT number + %d*10000, "
                "concat('/p/', toString(number %% 50)) FROM numbers(10000)" % i)
print(ctx.execute("SELECT name, rows, level, marks FROM system.parts "
                  "WHERE active ORDER BY name").text())
ctx.execute("OPTIMIZE TABLE t FINAL")
print(ctx.execute("SELECT name, rows, level, marks FROM system.parts "
                  "WHERE active ORDER BY name").text())
```

```text
┌─name──────┬─rows──┬─level─┬─marks─┐
│ all_1_1_0 │ 10000 │     0 │     2 │
│ all_2_2_0 │ 10000 │     0 │     2 │
│ all_3_3_0 │ 10000 │     0 │     2 │
└───────────┴───────┴───────┴───────┘

┌─name──────┬─rows──┬─level─┬─marks─┐
│ all_1_3_1 │ 30000 │     1 │     4 │
└───────────┴───────┴───────┴───────┘
```

三个 part 的块区间分别是 `1_1`、`2_2`、`3_3`,合并后变成 `1_3`,`level` 从 0 变成 **1** —— 这正好符合第 05 章写的规则:**合并结果的 `level` = 参与合并的源 part 里的最大 `level` 再加 1**(三个源 part 的 level 都是 0,所以结果 level = 1)。`marks` 从 2+2+2 变成 4 也符合预期:三个 part 合并后重新按 8 192 行切,30 000 行只需要 4 个 granule(3.66 向上取整)。

**第五段:`EXPLAIN PIPELINE`。** 这一段直接暴露了"分段执行"的骨架:

```python
print(ctx.execute("EXPLAIN PIPELINE SELECT url, count() FROM t "
                  "WHERE id > 100 GROUP BY url").text())
```

```text
┌─explain────────────────────────────────────┐
│ (mini-clickhouse 分段执行,不是一张完整的 processor 图) │
│ Pipeline 0:                                │
│   (ReadFromStorage × 1)                    │
│   (Filter(WHERE) × 1)                      │
│ Pipeline 1:                                │
│   (Expression × 1)                         │
│   (Aggregating × 1)                        │
│   (MergingAggregated × 1)   ← 阻塞点:必须看到全部输入 │
│ Pipeline 2:                                │
│   (Expression(Projection) × 1)             │
│   (Limit × 1) → (CollectingSink × 1)       │
│                                            │
│ max_threads = 0                            │
│ max_block_size = 65409                     │
└────────────────────────────────────────────┘
```

请把它和第 10 章展示的真实 `EXPLAIN PIPELINE` 输出对照着读。真实版本会打印**一张**图、每个 processor 有独立编号、同一个算子出现多份(`Expression × 4`)、还会出现 `Resize` 节点。这里每个算子都是 `× 1`,而且被切成了三段——**三段之间的边界就是阻塞点**。`max_threads = 0` 是单线程的坦白,`max_block_size = 65409` 与真实 ClickHouse 的默认值一致。这个非 2 的幂的数并非随手取的:源码把它写成 `65536 − PADDING_FOR_SIMD − (PADDING_FOR_SIMD − 1)`(其中 `PADDING_FOR_SIMD` 取 64),算下来正好是 `65536 − 127`。官方文档正文没有写出这个算式,想核实便翻到源码里 `max_block_size` 默认值的定义处。

**第六段:客户端与服务端的一次往返。** 前面五段都在同一个进程里跑(`Context` 直接执行)。再往前走一步,`minich/client.py` 与 `minich/protocol.py` 让"两个进程隔着 socket 聊天"这件事也跑得起来——这正是 `clickhouse-client` 与服务端之间那根线的缩影:

```python
import threading
from minich.client import Client

server = Client.create_server(host="127.0.0.1", port=0)   # 0 = 让系统挑端口
threading.Thread(target=server.serve_forever, daemon=True).start()
c = Client.connect("127.0.0.1", server.server_address[1])  # 握手:发 Hello、收 Hello

print("ping:", c.ping())
c.execute("CREATE TABLE t (id UInt64, s String) ENGINE = MergeTree ORDER BY id")
c.execute("INSERT INTO t VALUES (1,'a'),(2,'b'),(3,'c')")
r = c.execute("SELECT id, s FROM t ORDER BY id")
print("row_count:", r.row_count())
print(r.text())
c.close(); server.shutdown(); server.server_close()
```

实测输出:

```text
ping: True
row_count: 3
┌─id─┬─s─┐
│  1 │ a │
│  2 │ b │
│  3 │ c │
└────┴───┘
```

同一个 `Client` 也有 **in-process 模式**,入口是 `Client.local()`——它不连 socket,内部自己建一个 `Context` 直接执行(`Client.local(database=…, data_path=…, settings=…)`,参数里没有 host/port)。文档给的默认用法就是它:`Client` 类的 docstring 第一段示例写的是 `c = Client.local()`,第二段才是 `Client.connect(...)`;直接裸调 `Client()` 并不是文档推荐的写法。两种模式的方法签名**故意完全一致**,结果也必须一致——这是模块的一条隐性契约(真实 ClickHouse 没有 in-process 模式,它要么连 TCP 9000、要么连 HTTP 8123)。

消息流长什么样,`protocol.py` 的模块 docstring 有一张"一次会话的消息序列"图:客户端发 `Hello` → 服务端回 `Hello` → 客户端发 `Query` → (INSERT 时)客户端的 `Data` 跟着来 / (SELECT 时)服务端的 `Data` 一条条回 → 服务端夹着发 `Progress` → `EndOfStream` 收尾 → `Ping`/`Pong` 心跳。`Progress` 之所以是独立的一条消息而不是塞进 `Data`,是因为"进度"和"数据"是两个不同的变化频率:100 万行的结果可能只有 8 个 Data 块,但 Progress 可以每 10 万行发一次。这一段可以直接看字节:一条 `Ping` 帧是 `04 00`(`0x04` = `PING`,`0x00` = 载荷长度 0,长度前缀是本实现加的简化),一条 `Progress(read_rows=100)` 帧是 `07 04 64 00 00 00`(`0x07` = `PROGRESS`,载荷 4 字节,四个 varint 里第一个是 100)。

**但它的报文与真实 ClickHouse 不兼容**:多了长度前缀、没有 revision 协商与压缩、`ClientInfo` 的二十来个字段被压成一个字符串、块用的是本实现的 `Native`。所以协议这一层是**结构上忠实、字节上不兼容**——它演示的是"客户端与服务端在聊什么",不是"你能用官方驱动连上来"(见第 34 章 B.6.1 关于 `Native` 格式的警告)。

## 31.14 它明确不做什么

一个教学实现的价值有一半在于**它划的边界**:知道什么没做,才知道哪些结论不能从它这里推。这份"缺口清单"的**完整版与可扫读版在第 34 章 B.6**(那里把它分成"会给出错误结果""会显得没生效""结构性缺失""语义细节差异""工具链小坑"五类,并统计了 `minich/` 里全部 `NOT_IMPLEMENTED` / `NOT_IMPLEMENTED_IN_MINICH` 抛出点)。这里不把它整张搬过来,只挑出**会影响你下判断**的几条,每条写清"会影响哪些结论";要查全,去 B.6。

**没有真正多线程的流水线。** 执行是单线程顺序驱动(`EXPLAIN PIPELINE` 会打印 `max_threads = 0`,每个算子都是 `× 1`)。真实 ClickHouse 的 `ReadFromMergeTree` 会按 part/granule 拆出多条泳道,`Resize` 节点负责扇出扇入。**影响:不能拿它演示并行带来的加速,也不能观察"processor 数远多于线程数"这件事**——第 10 章讲的那些并行机制在这里全看不到。

**没有代价模型。** 优化器是纯规则式的(`optimizer.py` 顶层 docstring 明确写了这是刻意的选择:规则式可解释、不需要统计信息,代价是可能错过更优计划),一共只有四条规则,真实 ClickHouse 有上百条。而且**它的效果是分层的**:四条规则里只有"平凡 count"(`optimize_trivial_count_query`)真正改变了读的字节数——它让 `SELECT count()` 一个数据块都不读;"主键裁剪"确实生效,但它**不是优化器规则**,走的是存储层自己的 `KeyCondition`;`limit_push_down` 会把 LIMIT 算出来记在优化结果里,可那个值没有继续传到存储层,所以 `LIMIT` 仍然读满;`move_to_prewhere` 与 `optimize_read_in_order` 这两条更彻底——它们要靠"表的排序键列名"来判断能不能改写,而取列名的那个辅助函数找的是引擎上的 `order_by_sql` / `sorting_key_sql` / `partition_by_sql` 属性,这几个属性目前**没有任何引擎定义过**,于是这两条规则实际上一次都不会触发(实测:`optimize_select()` 对一条带 `WHERE id > 100` 的查询返回的 `applied` 里只有 `limit_push_down`,而 `_sorting_key_columns(table)` 返回 `[]`)。哪些在跑、哪些没在跑,§31.5 给了测量脚本——**别记结论,记方法**。**影响:不能讨论"为什么选了这条计划"这类问题,那是第 24 章的范围,得用真实 ClickHouse。**

**没有 `Distributed` 引擎。** 注册的 23 个存储引擎里没有它。分片相关的只有三个"钩子":表函数 `cluster` / `clusterAllReplicas` / `remote` 会把请求委托给 `ctx.remote_executor`,以及 `ctx.clusters` 这个拓扑字典——但**谁来填这两个东西,代码里没有实现**。**影响:第 23 章讲的分布式查询在这份实现里跑不起来,`SAMPLE` 采样也没有(`StorageFeatures.sampling` 默认 `False`)。**

**没有 S3 / 对象存储。** 磁盘抽象 `IDisk` 只有 `DiskLocal` 一个实现,`disk.py` 的 docstring 里画的 `DiskS3` / `DiskHDFS` 是**说明用的示意图,不是代码**。表函数 `s3`、`s3Cluster`、`hdfs` 都注册了名字但一调用就抛精确的 `NOT_IMPLEMENTED`。**影响:不能演示"冷热分层到 S3",第 29 章讲的存储层选型只能讲理论。**

**没有投影。** `PROJECTION` 子句能被解析(`sql/ast_.py` 里有对应节点),MergeTree 也会把声明存进 `self.projections`,但**读路径完全不看它**。`StorageFeatures.projections = True` 只是个声明位。同样地,`optimize_use_projections` 这条设置存在于注册表里,但没有任何代码读它。**影响:不能演示"投影把点查变快"这件事。**

**没有窗口帧的 `RANGE` 语义。** `minich/execution/window.py` 把 `RANGE` **当成 `ROWS` 处理**。两者只在有并列值时不同:`ORDER BY k` 里有两行 `k = 5` 时,`ROWS ... CURRENT ROW` 给这两行不同的累计值,`RANGE` 给相同的。**影响:在有并列值的数据上做窗口聚合,会和真实 ClickHouse 给出不同的结果。** 这个差异写在 `_frame_bounds` 的注释里,第 16 章有例子。窗口执行也不做真实 ClickHouse 的两项优化(可增量帧的专门处理、"数据已有序则跳过排序")。

**没有真正的 LZ4 / ZSTD 兼容。** §31.11 已详述:同名算法、不同格式,输出与 lz4 / libzstd 互不兼容;`checksums.txt` 用的是 4 字节的 CRC-32,而真实 ClickHouse 用的是 16 字节的 CityHash128(见 §05.4)。**影响:任何"压缩后的 part 能不能被别的系统读"的问题,答案都是不能。**

**没有并发写。** 引擎用一把 `threading.RLock` 串行化写入;后台合并只有一个线程(`start_merges()` 里那一个 `threading.Thread`,名字叫 `MergeTree-merge-<表名>`),而真实 ClickHouse 用 `background_pool_size`(默认 16)的线程池。**影响:写入速率超过单线程合并吞吐时,part 数增长得更快,更容易撞到 `parts_to_throw_insert`。这条被写进了 `start_merges()` 的 docstring,态度是"可预测性优先于吞吐"。**

**没有通用的后台调度器。** 有的只是 MergeTree 自己那一个合并线程(它顺带处理 mutation 与 TTL)。真实 ClickHouse 有独立的 mutation 池、TTL 调度、`SYSTEM` 后台任务。**影响:TTL 和 mutation 的推进速度取决于"有没有 part 参与合并",独立的时间驱动行为观察不到。**

**没有真正的向量化执行框架。** 定长标量列确实用 `np.ndarray` 承载(`ColumnVector` 的处理是照 `PaddedPODArray` 的容量倍增 + padding 做的),一部分标量函数也走 NumPy 运算。但**框架层没有向量化**:聚合、排序、JOIN、窗口都是 Python 层的循环。**影响:所有性能讨论只能定性(§31.1 已说)。**

**网络协议是通的,但别拿它当"兼容 ClickHouse"的证据。** `minich/client.py` / `protocol.py` / `cli.py` 都在,§31.13 第六段给了一次可跑的 socket 往返(握手、建表、插入、查询、`ping()`)。但它**报文的字节布局与真实 ClickHouse 不兼容**(长度前缀、无 revision 协商、无压缩、块是自实现的 `Native`),所以这一层是"结构上忠实、字节上不兼容"——要真正互通得实现完整的 native 协议(第 19 章、第 34 章 B.6.1)。

**还有一些零散的缺口**,都注册了名字、调用时报精确的 `NOT_IMPLEMENTED` 并说明原因:表函数 `dictionary` / `executable` / `jdbc` / `mysql` / `odbc` / `postgresql` / `sqlite` / `timeSeries*`、`INSERT INTO FUNCTION`、以及 `GraphiteMergeTree` 的 rollup 规则(引擎注释里直接写着 "registered but NOT applied")。(`CREATE USER` / `DROP USER` / `CREATE ROLE` / `GRANT` / `CREATE QUOTA` 这些权限 DDL 已经能用,见第 26 章;尚未接线的权限语句才抛 `NOT_IMPLEMENTED`。)

最后一处**不是"没实现",而是一条容易搞错的插入规则**,同样会影响结论:这份实现里 `INSERT INTO ... SELECT` 按**位置**对齐(§31.13 有实测),与真实 ClickHouse 一致;只有 `VALUES` 路径才按**名字**对齐。所以从 `SELECT` 插入时列名对不上不会报错,只会按顺序落到目标列——写实验脚本前先查清走的是哪条路径,别把列序写乱。

## 31.15 读代码的建议路径

八万多行不可能顺着目录读。下面这条路径的目标是:**两小时内建立完整的脑内地图,之后按需下钻。** 顺序是有讲究的——每一步只依赖前面已经读过的东西。(每个文件的精确行数在第 34 章 B.1 节;这里只给顺序和理由。)

**第一站:先把"数据长什么样"钉死(约 40 分钟)。**

1. `minich/block.py`。只读三个类:`ColumnWithTypeAndName`、`Block`、`Chunk`。把 `clone_empty()` 和 `get_block_with_data()` 这两个方法的区别想清楚,后面到处都是它们。
2. `minich/types/base.py` 的 `DataType` 类 + `minich/columns/base.py` 的 `IColumn` 类。**只读接口,不读实现**——理解"类型给元数据、列给存储"这个两段式就够了。
3. `minich/columns/base.py` 里的 `ColumnVector`:看它怎么用 `np.ndarray` 做容量倍增。这是"列存到底是什么"最具体的一个答案。

**第二站:一条 SQL 怎么变成结果(约 50 分钟)。**

4. `minich/execution/context.py`。`Context` 与 `QueryResult` 是全部对外 API。读完你就知道怎么用它写实验脚本。
5. `minich/execution/interpreter.py` 的 `InterpreterSelectQuery._run_inner`——这一个方法就是全书的目录:① UNION → ② FROM → ③ 分析 → ④ 流水线。**其他地方先别读。**
6. `minich/execution/analyzer.py` 的模块 docstring,那张"聚合前 / 聚合 / 聚合后"的三段图。这是整章最像编译器的一层。
7. `minich/execution/processors.py` 的 `Status`、`Port`、`IProcessor`、`ISource`、`ISink`、`ISimpleTransform` 六个类的**类 docstring**,以及 `PushResult`。具体的处理器实现跳过——需要时按名字回查。

**第三站:存储层(约 60 分钟,值得)。**

8. `minich/storages/base.py` 的 `StorageFeatures`、`ReadContext`、`IStorage`。三个类加起来四百多行,但它们定义了"一个表引擎要提供什么"。
9. `minich/storages/merge_tree/` —— **按它自己 docstring 给的学习顺序读**:`disk` → `marks` → `part` → `key_condition` → `skip_index` → `merge_policy` → `merger` → `mutations` → `ttl` → `merge_tree` → `engines`。其中 `part.py` 和 `marks.py` 的**模块 docstring** 必须完整读,那是本项目写得最好的两段 ASCII 图;`merge_tree.py`(全包最大的一个文件)可以只读"三个支柱"那一段和 `read()` 方法。

**第四站:横向支撑层(按需)。**

10. `minich/serialization.py` 与 `minich/compression.py` 的 docstring + 类名列表。压缩那两个自研算法的具体实现(`_lz77_parse`、`_RangeEncoder`)除非你专门想研究压缩,**跳过**。
11. `minich/aggregates/average.py`——全项目最短的模块之一,且最能说明"状态必须可合并"。读完再看 `aggregates/base.py` 的 `IAggregateFunction`。
12. `minich/formats/base.py` 的注册表与两个入口函数。
13. `minich/system/base.py` 的 `ISystemTable` + `minich/system/__init__.py` 的 docstring(它把全部 system 表分成"真实有数据 / 结构对但来源可选 / 结构对但故意恒空"三类)。

**这些可以先不读,甚至永远不读:**

- `minich/functions/` —— 19 个文件,是全项目最大的一个包(约占五分之一的行数)。它没有对外 API,唯一的作用是"导入即注册"。要知道某个函数存不存在,用 `registered_functions()` 查,不要去读源码。
- `minich/keeper.py`(约 2 400 行)、`minich/replication.py`(约 2 400 行)、`minich/dictionaries.py`(约 2 400 行)、`minich/access.py`(约 1 400 行)—— 前三个各两千余行、`access.py` 一千余行,是四套**相对独立**的子系统。它们只在你专门研究复制 / 字典 / 权限时才需要,而且它们的模块 docstring 已经把设计讲清楚了。
- `minich/types/scalars.py`、`minich/types/composite.py`、`minich/columns/composite.py`、`minich/sql/ast_.py`、`minich/sql/parser.py` —— 这些是**量大但结构平铺**的文件:一个类型一个类,一个 AST 节点一个类。当参考手册用,别通读。
- `minich/util/` —— 它不知道表与查询为何物,读起来最轻松,也最没有信息量。

**一个通用的读法。** 每个模块的 **docstring 就是这一块的说明书**,而且里面写了"真实 ClickHouse 怎么做的 / 我们怎么做的 / 差异在哪"。跨文件的 ASCII 图(架构图、流程图、对比表)几乎全在 docstring 里,实现代码里只有零散注释。所以:先读 docstring,再用 `grep` 找关键函数,最后才读函数体。

**如果你要读的是真实 ClickHouse 的 C++ 源码。** 上面这条路径走完,你手里已经有了"一个列式数据库需要哪些部件"的地图;真实仓库的目录与这张地图刻意对齐(§31.2 的对照表),所以下钻顺序可以照搬,只是每一站的文件换成 C++ 的那几个。跟着**一条 `SELECT` 的一次旅程**走,顺序是:

1. **解析** —— `src/Parsers/`。入口是 `parseQuery.cpp` 与 `ParserSelectQuery.cpp`;表达式解析器大量使用模板与宏展开,不追细节,知道它产出 `src/Parsers/AST*.h` 里的 AST 节点即可。
2. **分析** —— 新路径在 `src/Analyzer/`,AST 在这里被建成 QueryTree,`QueryTreeBuilder.cpp` 与 `Analyzer/Passes/` 是各类改写规则的落点;旧路径在 `src/Interpreters/InterpreterSelectQuery.cpp`(体量巨大),它的 analyze 与 execute 两步就是本书反复讲的"分析 → 流水线"。
3. **计划与优化** —— `src/Planner/Planner.cpp` 与 `src/Processors/QueryPlan/`(优化规则在 `Optimizations/` 子目录里,基本一条规则一个文件)。**第 24 章讲的那条"规则链"在这里能逐个对上号**,比读文档直观。
4. **流水线** —— `src/Processors/`:四态状态机在 `IProcessor.h`,调度在 `PipelineExecutor.cpp`,Source / Sink / Transform 基类都在这一层。第 10 章讲的东西全在这里。
5. **读取与裁剪** —— `src/Storages/MergeTree/`。`MergeTreeDataSelectExecutor.cpp` 是"读一个 part"的主干(算 mark 范围、决定读哪些 granule),`MergeTreeRangeReader.cpp` 与 `MergeTreeReaderStream.cpp` 是真正读字节的地方,`MergeTreeData.cpp` 是表的实体(极大,按方法名跳读)。第 05 章与第 11 章的内容对应这一站。
6. **聚合与函数** —— `src/AggregateFunctions/` 与 `src/Functions/`,注册集中在各自的 `register*.cpp`。
7. **格式与序列化** —— `src/Formats/` 与 `src/DataTypes/Serializations/`。

读法上还有三条经验。**一,按"一次查询的旅程"读,不要按目录读**:同一个目录里往往混着多年以前的实现和上个月的重构。**二,想知道某个行为的确切期望,去 `tests/queries/`**——那里的每对 SQL 与 reference 文件就是一条断言,比文档和注释都可靠,也比这份教程权威。**三,文件会随版本拆并改名**(`Processors/Formats/` 与 `Formats/` 就并存过一段时间),所以上面给的是"入口级"路径,确切文件名以你手上那份 release tarball 为准;本书凡是还标着"未确认"的地方,多半也是同一个原因——那个版本的确凿依据没拿到,请以你手上的实例为准。

## 31.16 小结

- 为什么用 Python:读得懂、跑得起来、能验证。代价是**结构性慢两个数量级**(10 万行 `INSERT` 2.23 秒),所以它的所有性能结论**只能定性**。对标版本是 ClickHouse 25.3。
- 规模:123 个 `.py` 文件、86 242 行。包结构与真实 ClickHouse 的 `src/` 目录刻意对齐。
- 分层纪律:前端 `sql/` → 编排 `execution/` → 执行 `processors.py` → 存储 `storages/` → 横向支撑 `types`/`columns`/`serialization`/`compression`/`formats`/`system`。箭头只向下,**没有向上的箭头**。
- 十个关键设计里,有七个是**照真实实现抄的语义**:两段式的 `DataType`/`IColumn`、`Block` 当表头 + `Chunk` 带数据、granule 8 192 与稀疏主键索引、part 不可变 + 临时目录 + 原子改名、四态状态机与 `PushResult` 三值、`avg` 的状态是 `(sum, count)`、以及哈希连接"build 右表 / probe 左表 + strictness 决定桶怎么留 + 残余谓词在装配结果块之前过滤"。
- 有三个**必须知道的落差**:流水线是**分段执行**(阻塞算子物化中间 Block,内存峰值更高、看不到边算边出、单线程);`LZ4`/`ZSTD` 是**同名不同格式**的自研算法,不能跨系统交换;窗口帧的 `RANGE` **被当成 `ROWS`**;此外哈希连接**只有一种算法**、右表整份进内存且不检查 `max_bytes_in_join`,所以第 15 章那条"右表放错边就 OOM"在这里会毫无缓冲地发生。
- 还有一个**最需要自己动手验证**的落差:三次裁剪(分区 / 主键 / 列)的代码都在存储层里,而决定它们是否生效的是**解释器有没有把 `where` / `prewhere` 交给 `ReadContext`**——`ReadContext` 上的 `columns` / `key_analysis` / `partition_ids` / `limit` 只是四个"如果调用方已经算好了就填进来"的透传位,它们空着**不等于**裁剪失效。这条接线随实现演进而变。最后一次量到的状态是:平凡 count 生效(0 个 mark)、主键 granule 裁剪生效(25 个 mark → 1 个)、`LIMIT` 下推与列裁剪仍不生效。**这类事实读文档永远读不出来,只能靠跑**;而既然它会变,该记住的是 §31.5 那个 `profile_events` 测量脚本,不是某一次的数字。
- 顺带记住一个实操纪律:插入路径的对齐规则要**先查再写**。这份实现里 `VALUES` 按**名字**对齐(`_align_block` 先 `try_get_by_name`,有列清单时按列清单),而 `INSERT ... SELECT` / `FORMAT` 按**位置**对齐(与真实 ClickHouse 一致)。"按名字对齐"的路径在列名对不上时可能**静默填默认值**——不报错、只出错数据。
- 这一章是导览。要查"这个实现里 X 是怎么做的",翻第 34 章;那里有真实的文件树(带行数)、逐功能的能力矩阵、照源码抄下来的 API 签名、三个"怎么加一个新 X"的步骤模板,以及九个由易到难、每个都能跑出可见结果的练习。
