# 第 18 章 分区、合并、Mutation 与 TTL

第 05 章说"part 写了就不改",把优雅的读路径建立在不可变性上。代价全都集中到这一章:既然不能改,那么"删数据""改数据""让数据过期""把旧 part 变少"这四件事都得另想办法。答案是四条互相配合的后台流水线——**合并(merge)**把碎 part 收敛成大 part,**partition 操作**做整块的秒级搬运与删除,**mutation** 靠重写 part 来表达 `UPDATE`/`DELETE`,**TTL** 让过期数据自己消失。它们共享同一批后台线程,也共享同一套"异步、最终一致"的脾气。不懂这一点,你就会在"为什么 `DELETE` 完还能查到""为什么表突然报 too many parts""为什么磁盘满了却删不掉数据"这些问题上反复踩坑。

## 18.1 后台合并:为什么必须合并

`INSERT` 只做一件事:产出一个新的、排好序的小 part。写入很快,但 part 会越来越多。为什么 part 多了不行?三笔账:

- **查询要打开的文件数**:读一张表要把每个 part 的每列文件都打开一次。10 万个 part × 20 列 = 两百万次打开,光目录遍历和文件句柄就够了;真实 ClickHouse 会因为 part 太多直接拒绝写入;
- **元数据内存**:每个 part 的 `primary.idx`、标记、min/max 都要常驻,part 越多内存吃得越多;
- **索引效率**:每个 part 单独二分,part 越碎,"每 part 至少读一个 granule"的浪费越显著。

所以后台合并的职责就是把"很多小 part"逐步收敛成"少数大 part"。合并过程本身是 **k 路归并**:每个 part 内部有序,用最小堆弹出全局最小的行。复杂度 O(N log k),内存 O(k)——因为每个流只缓存当前那个最小元素,而不是把全部数据读进内存。这条能力成立的前提,还是第 05 章那一次"写入前排序"。

**但"k 路归并"这只说了一半:一次合并的内存是多少,取决于它按行还是按列搬。** 上面那段描述的是**横向合并(horizontal merge)**:把每一列的多个流**同时**摆开,一次推进一"行块",所有列一起往前走。它的内存占用和**列数成正比**——宽表(几十上百列)一次合并,峰值内存是"列数 × 每列那一块的字节数"。一张 200 列、每列块又不小的表,横向合并光是缓冲各列的流就能把内存顶穿。

ClickHouse 的另一条路叫**纵向合并(vertical merge)**,它把"一次搬所有列"换成"**一列一列地搬**":

```text
横向合并:同时打开所有列的流,一行一行地推进
   col_1 ─┐
   col_2 ─┤  一次归并,k 个源 part 的每一列都在内存里有缓冲
   ...   ─┼─► 所有列同步前进 → 输出行块
   col_N ─┘
   峰值内存 ≈ 列数 × 块大小

纵向合并:先定"行的顺序",再一列一列地各走各的
   ① 先只归并主键/排序键列(以及跳过索引列),定出合并后每个源 part 的行序
   ② 对每一个非主键列,单独读、按①算好的行序重排(引擎里叫 gather)、单独写
   ③ 一列写完、缓冲释放,再拿下下一列
   峰值内存 ≈ 1 列 × 块大小(外加一份行序映射)
```

所以纵向合并的收益是**把峰值内存从"列数倍"压到"常数倍"**,代价是每一列都要独立地读一遍、写一遍(更多的打开文件、更多的寻道),固定开销比横向高。引擎的实现就写在 `MergeAlgorithm` 这个枚举里(`src/Storages/MergeTree/MergeAlgorithm.h`),只有三个值:`Undecided`(还没定)、`Horizontal`(所有列一起按行归并)、`Vertical`(主键列按行归并,非主键列逐列 gather)。

**它默认就开着,但有门槛——一次合并只有同时满足若干条件才会走纵向。** 源码 `src/Storages/MergeTree/MergeTreeSettings.cpp` 里 `enable_vertical_merge_algorithm` 默认 **1**(开),真正决定走不走的是这三个阈值:

```text
vertical_merge_algorithm_min_rows_to_activate     默认 16 * 8192 = 131072
vertical_merge_algorithm_min_bytes_to_activate    默认 0(不设字节门槛)
vertical_merge_algorithm_min_columns_to_activate  默认 11
```

这三个数不是随手写的,每一个都对着一笔账:

- **行数门槛(131072 = 16 × 8192)**:`8192` 是默认的索引粒度(一个 granule 的行数),`16 * 8192` 就是"这次合并涉及的行数至少得攒够 16 个 granule"。纵向合并要先把行序算出来、再逐列重排,这套"逐列"的固定开销(多开文件、多排序、行序映射的内存)只有 part 足够大时才摊得薄;小合并走纵向,省下的内存抵不过多花的 IO。
- **列数门槛(11)**:纵向省的是"列数倍"的内存,列数不够多就没什么可省的——一列一列搬和一次搬 5 列差别微乎其微,却要付"逐列读写"的额外代价。**只有宽表(非主键列 ≥ 11)才给它机会。** 注意判据数的是**非主键列**(源码里叫 `gathering_columns`):主键列无论如何都得先归并出来定行序,所以不计入这个门槛。
- **字节门槛默认 0**:默认不按大小卡,只按行数与列数卡;要设的话它是第三个旋钮。

**除了这三个阈值,还有一组"硬前提"。** 源码 `src/Storages/MergeTree/MergeTask.cpp` 的 `chooseMergeAlgorithm()` 里,下面任何一条不满足,一律退回横向:

- **part 必须是 Wide 形态。** Compact part 把所有列挤在同一个文件里,根本没有"逐列读写"的物理基础(第 05 章 §05.10);默认阈值(`min_bytes_for_wide_part` 约 10 MiB)之下的小 part 是 Compact,所以**那些小 part 的合并永远是横向的**——这是"行数门槛"之外的另一重保护。另有一个开关 `allow_vertical_merges_from_compact_to_wide_parts`(默认 `true`,跨副本必须一致),管的是"允许不允许从 Compact 源 part 纵向合并出一个 Wide 结果 part"。
- **存储模式得是 `Ordinary`/`Collapsing`/`Replacing`/`VersionedCollapsing` 之一**;`Summing`/`Aggregating` 这类"合并时要改行集"的引擎不在其中(§18.4)。
- **不能带 `DEDUPLICATE`、不能带 TTL 的过期删除、也不能是 `CLEANUP` 合并。** 这些动作需要"把所有列摆在一起看",纵向的分列处理会和它们打架。
- **参与的 part 数不能超上限**(源码里的 `RowSourcePart::MAX_PARTS`),part 太多时纵向的多路归并会把开销放大。

**观测:一次合并到底走了哪条。** 它落在两处的 `merge_algorithm` 列上——`system.merges` 和 `system.part_log` 的 `MergeParts` 事件(第 27 章 §27.8、§27.9)。取值就是上面那三个词(`Undecided`/`Horizontal`/`Vertical`)。另外,纵向合并时 `system.merges` 的 `columns_written` 列会**一列一列往上跳**——因为引擎正是逐列写出去的,而横向合并不会这样。这就给了"我的合并为什么吃那么多内存"一个可查的答案:**宽表合并吃内存,先看它走的是不是横向;明明宽表却总走横向,就回去查上面那组前提**(part 是不是 Compact、是不是带了 TTL、非主键列够不够 11)。还有一个只跟远程盘有关的旋钮 `vertical_merge_remote_filesystem_prefetch`(默认 `true`):纵向合并本来就是"一列一列读",正好可以在读当前列的时候**预取下一列**——这个开关就是为此开的。

## 18.2 合并选谁:连续、大小相近、总量受限

真实 ClickHouse 的默认策略 `SimpleMergeSelector` 只回答一个问题:下一步合并哪几个 part。答案不是"全部",也不是"最小的两个",而是**一段连续的、大小相近的小 part**。它有三条约束:

**连续**——合并的是排序后的一段,不是随便挑几个。段越小,归并的内存与 IO 越可控。

**大小相近**——段内最大 part 不能超过最小 part 的 `max_size_ratio` 倍(默认 3)。**要注意 `max_size_ratio` 是 `SimpleMergeSelector` 的**内部参数**,未必是一条可调的 MergeTree 设置**——想调整合并行为,以 `system.merge_tree_settings` 里真实存在的条目为准,别假设它一定是能 `SET` 的那个名字。这条是控制**写放大**的核心:如果让 1 KB 的 part 和 100 MB 的 part 一起合并,合并结果的"贡献"几乎全来自大 part,小数据等于白搬一遍;更糟的是,下次再来一个小 part,它又会和大 part 合并一次。只和同等量级合并,才能让每份数据被重写的次数可控。

**总量受限**——段内字节总和不超过 `max_bytes_to_merge_at_max_space_in_pool`(默认 **150 GiB**),并且受"可用空间 × 0.1"约束。一次合并要占内存和临时磁盘,合一个 500 GB 的任务会瞬间吃满空间。在这三条下,策略**优先选总量最小的那段**,这保证小 part 先被消化掉。

mini-clickhouse 的 `SimpleMergePolicy` 完整复刻了这三条,参数一一对应(`max_size_ratio=3.0`、`max_bytes_to_merge_at_max_space_in_pool=150 GiB`、`max_parts_to_merge_at_once=100`、可用空间比 `0.1`),`select_for_partitions` 在多个分区里挑"候选总量最小"的那个优先服务。

**"除了 `SimpleMergeSelector`,还有别的选择器吗?"实际上几乎没有。** 开源 ClickHouse 里承担合并选择的**基本上就是这一个**策略,你能调的只是它的几个旋钮(都是 MergeTree 级别的设置,以 `system.merge_tree_settings` 为准):

```text
min_age_to_force_merge_seconds          part 至少要"老"到多少秒,才允许为了收敛而强制合并
min_age_to_force_merge_on_partition_only  上面那条是否只对整个分区生效(而不是单个 part)
max_bytes_to_merge_at_min_space_in_pool  磁盘/空间紧张时,单次合并允许的总字节上限
number_of_free_entries_in_pool_to_lower_max_size_of_merge  后台池空闲名额低于它时,临时下调单次合并上限
```

它们解决的是不同问题:`min_age_to_force_merge_*` 管"要不要为了收敛去合一个不够大的段",后两个管"资源吃紧时别发起太大的合并"。**但请记住它们都只是微调** —— 合并跟不上写入,根因几乎总是写入方式(§18.6),不是选择器调得不好。

**观测合并的唯一主力是 `system.part_log`。** 每一次 part 的诞生与消亡都会记一条:`NewPart`(写入产生)、`MergeParts`(合并)、`MutatePart`(mutation)、`DownloadPart`(副本拉取)、`RemovePart`(删除)。它比 `system.merges` 多了**历史维度** —— `system.merges` 只反映**此刻正在跑**的合并,而 `part_log` 留下的是每一次合并的耗时、源 part、合并前后的行数与字节数,以及记录合并来由的 `merge_reason` 列。**`merge_reason` 这一列确实存在**,类型是 `Enum8('NotAMerge' = 1, 'RegularMerge' = 2, 'TTLDeleteMerge' = 3, 'TTLRecompressMerge' = 4)`,只对 `event_type = 'MergeParts'` 的事件有意义:`NotAMerge` 表示"这条不是合并事件",`RegularMerge` 是普通合并,后两个分别对应 TTL 的过期删除与带 TTL 的重压缩。(**一条版本边界**:25.3 的 `src/Interpreters/PartLog.h` 里 `MergeReasonType` 只有上面**四项**;`TTLDropMerge` = 5 是更晚的版本才加的,别把新版的枚举抄到这一版上。)**枚举里没有 `OPTIMIZE` 这一项**——`OPTIMIZE` 触发的合并也是普通合并事件,记为 `RegularMerge`,不会单列一类。枚举名早期是全大写(`NOT_A_MERGE`/`REGULAR_MERGE` 等),后来的版本改成了现在的驼峰式;另外,`system.part_log` 只有在服务端配置了 `part_log` 段时才会生成,通常在第一次向 MergeTree 表写入后才建起来(列名与取值以目标版本 `DESCRIBE system.part_log` 为准)。排查"合并为什么一直不收敛""这次 TTL 究竟删掉了多少",查它。

## 18.3 合并是异步的:最终一致而非立即一致

这是本章最该背下来的一条:**合并发生在后台,何时发生不确定**。于是:

```text
INSERT 3 行(其中 2 行主键相同) → 立刻 SELECT → 看到 3 行
   ……等后台合并跑完(或手工 OPTIMIZE)……
                              SELECT → 看到 1 行
```

这不是 bug,是设计。还要注意这半句话**藏着前提**:之所以能"看到 1 行",是因为引擎在合并时**按排序键去重** —— 那是 `ReplacingMergeTree` 的行为;**普通 `MergeTree` 合并后仍然是 3 行**(§18.4 专门讲这件事)。要"立刻看到合并后的结果"有两条路:`OPTIMIZE TABLE t FINAL`(强制合并,会阻塞、很重),或者 `SELECT ... FROM t FINAL`(查询时归并)。后者的代价要知道:它放弃了读路径上最值钱的性质——每个 granule 可以独立、并行地读,因为"要读什么"开读前就算清楚了;带上 `FINAL` 就**必须先按排序键把同一键的行对齐到同一个线程**,并行度从"按 granule"降到"按 key 的分片"。所以官方建议:能靠后台合并收敛的就别用 `FINAL`,必须实时的场景用 `GROUP BY` + `argMax` 之类的写法替代。

## 18.4 合并语义由引擎家族决定

§18.3 那个"插入 3 行(其中 2 行键相同)→ 合并后剩 1 行"的例子,只有在**特定引擎**上才成立——它是 `ReplacingMergeTree` 的行为。**普通 `MergeTree` 在合并时只做一件事:把几份有序 part 归并成一份有序 part,一行都不丢**,上面那个例子若建普通 `MergeTree`,合并后**仍是 3 行**。"合并会去重"这个印象是错的。

真正"合并时改变行集"的是 MergeTree 的几个子族,它们的差别**只在"merge 时怎么处理同一排序键的多行"**。一张表对照(引擎能力的完整矩阵与选型见第 04 章 §04.4,这里只列合并这一维):

| 子族 | 合并时对同一排序键的多行做什么 |
|---|---|
| `ReplacingMergeTree` | 保留**版本最大**的一行(不给 `ver` 则保留最后写入的一份) |
| `SummingMergeTree` | **非排序键的数值列相加**,非数值列取其中一行(取哪个不确定) |
| `AggregatingMergeTree` | 合并**聚合的中间状态**(物化视图增量预聚合的底座,第 12 章 §12.10) |
| `CollapsingMergeTree(sign)` | 用 `sign = 1` / `-1` 的一对反向行**成对抵消** |
| `VersionedCollapsingMergeTree(sign, ver)` | 同上,但多了 `ver`,**插入顺序不再影响结果** |
| `GraphiteMergeTree` | 按 `graphite_rollup` 规则把老数据**降采样** |

三个和日常最相关的细节各补一句。`ReplacingMergeTree` 想主动触发去重,用 `OPTIMIZE TABLE t FINAL` 或 `... FINAL DEDUPLICATE`(见 §18.5);带墓碑列(`is_deleted`)时是否把墓碑行也物理清掉,得看专门的开关——**`clean_deleted_rows` 这个设置已经废弃**:它自 v23.12 起进入 OBSOLETE、被固定为 `Never`,设置它**静默无效**,别再拿它当可调项。25.3 上真正"把墓碑行物理清掉"的机制是表级开关 `allow_experimental_replacing_merge_with_cleanup = 1` 配合 `OPTIMIZE TABLE ... FINAL CLEANUP`(前者是实验特性、默认不开,以目标版本文档为准)。`SummingMergeTree` **不能替代聚合**:没合并过的 part 仍是多行,读的时候照样要 `sum(...) GROUP BY`。`CollapsingMergeTree` 的经典毛病是"插入顺序错了就抵消不掉",要修就得改用带版本列的 `VersionedCollapsingMergeTree`。

**`CollapsingMergeTree` 的读侧协议:不写 `sum(sign)`,你查到的就是错的数。** 这是本章最容易漏、后果又最严重的一条,必须单说。先把机制钉死:**`sign` 的成对抵消只在合并(merge)时发生,不在写入时发生。** 你写入一对 `(+1, -1)` 行,它们只是两行普通数据待在那里;只有后台合并真的把这两个 part 合到一起、两行的排序键相同、恰好配了对,那一对才会一起消失。于是:

```text
写入 (id=1, x=10, sign=+1)        ← 表达"这条存在"
写入 (id=1, x=10, sign=-1)        ← 表达"把刚才那条撤回"
   │
   ├─ 立刻 SELECT ... FROM t      → 两行都在,(10,+1) 和 (10,-1) 都查得到
   └─ 等后台合并跑完再 SELECT     → 这一对消失了(前提是它真被合到了一起)
```

合并是异步的、还未必覆盖到(§18.3),所以"我刚撤回的行怎么还在"是常态,不是 bug;而"数字怎么多了一份"更是常态。要让读侧拿到正确答案,必须**显式地把 `sign` 用起来**——按排序键分组,把 `sign` 当权重:

```sql
-- 正确读法:把 sign 当成 +1 / -1 的权重
SELECT
    id,
    sum(x * sign) AS x,      -- 被撤回那条的值被减掉
    sum(sign)     AS cnt     -- 这个键净剩几行
FROM t
GROUP BY id
HAVING sum(sign) > 0;        -- 把已经撤回干净的键整个滤掉
```

三件都不能省:`sum(x * sign)` 让撤回生效,`sum(sign)` 是"净剩几行",`HAVING sum(sign) > 0` 则滤掉"已被撤回干净"的键——漏了它,你会看到 `cnt = 0` 乃至 `x` 为负的幽灵行。等价地,`SELECT ... FROM t FINAL` 也能在查询时把抵消做掉,代价就是 §18.3 说的那笔(放弃按 granule 的并行读)。**关键是别把这张表当普通 `MergeTree` 直接 `SELECT`**:它返回的是"尚未合并的、带 `sign` 的原始行",裸读的数字要么翻倍、要么对不上。本书第 13 章 §13.1 建议用它来表达"删了源表也得跟着删"的联动,那条建议要配合这条读侧协议一起用才对。

**为什么还会有 `VersionedCollapsingMergeTree`:因为"成对抵消"依赖插入顺序。** 上面 `(+1, -1)` 能抵消,前提是它们**以正确的顺序到达、并被合并到一起**。分布式重试、多线程写入、客户端重放,都会让顺序乱掉:一条 `-1` 可能比它要抵消的 `+1` 先落盘,合并时配不上对,两行就都留下了。`VersionedCollapsingMergeTree(sign, ver)` 多一列 `ver`:抵消不再看"谁先来",而是按 `(排序键, ver)` 把同一版本的 `+1`/`-1` 配对——**写入顺序乱了也能正确抵消**。所以选型判据很干脆:**写入可能乱序,就用带 `ver` 的那一个。** 这条读侧协议在 mini-clickhouse 里也有对应:`minich/storages/merge_tree/engines.py` 里 **`StorageCollapsingMergeTree`** 的类文档就写着 `SELECT ..., SUM(x*sign) ... GROUP BY key` 那套写法(读源码时留意别找错类 —— 带 `Versioned` 的那个讲的是 `(version, sign)` 怎么配对,不含读侧协议)。

结论不变:**"合并会把同键的重复行收敛"是这几个子族的特性,不是 `MergeTree` 的通用行为。** 设计表时先问"我要的收敛语义是哪一个",再挑引擎;挑错了,后台合并跑得再勤也不会替你消掉重复。

## 18.5 OPTIMIZE 的变体与代价

`OPTIMIZE TABLE` 是"手工催一次合并"的入口,但它不止一个形态:

```sql
OPTIMIZE TABLE t FINAL;                        -- 把该表所有 part 合到不能再合
OPTIMIZE TABLE t PARTITION 202403 FINAL;       -- 只合一个分区
OPTIMIZE TABLE t FINAL DEDUPLICATE;            -- 合并 + 去掉完全相同的行
OPTIMIZE TABLE t DEDUPLICATE BY user_id, ts;   -- 只按指定列判重
```

几个必须知道的点:

- **`OPTIMIZE ... FINAL` 是阻塞且昂贵的。** 它会**绕过合并的大小限制**,把整张表(或整个分区)压成一个 part —— 对大表可能是一次持续几分钟到几小时、读写几百 GB 的重写。它属于**运维动作**,不是常规流程的一环;别把它写进定时任务(第 28 章的反模式里专门点了"小批量高频写 + 定时 `OPTIMIZE`")。
- **能加 `PARTITION p` 就加**:它把"重写整张表"降级成"重写一个分区",是唯一的收窄手段。
- **`DEDUPLICATE BY` 的列应当涵盖排序键(以及分区键)** —— 判重靠有序性,漏掉排序键语义容易不对。不过要标一句:**"必须涵盖排序键"这条硬要求我未在官方文档里找到明确依据**,它更像一条经验规则而非被明确写出的校验,请以目标版本的实测为准;`BY *` 表示按所有列。
- **它是同步等待的**:`OPTIMIZE` 语句会等到这次合并真正做完才返回(不像 mutation 那样立刻返回),所以对超大表执行要留足超时。

这也解释了为什么 `ReplacingMergeTree` 的"最终一致"没有免费解:要么忍受后台合并的不确定时机,要么用 `OPTIMIZE FINAL`/`SELECT ... FINAL` 承担代价(§18.3)。

## 18.6 part 数量的闸门:too many parts

写入速率长期大于合并速率时,part 数会**无界增长**,直到表不可用。ClickHouse 的选择是"宁可拒绝写入,也不让表退化",于是在插入路径上放了两道闸门:

- `parts_to_delay_insert`:单个分区的活跃 part 数超过它,**主动放慢** `INSERT`(按超额比例睡眠);
- `parts_to_throw_insert`:超过它,**直接报错**(`TOO_MANY_PARTS`);
- `max_parts_in_total`:整张表(所有分区合计)的上限。

25.3 里这两个值分别是 **1000** 与 **3000**(上游源码 `MergeTreeSettings.cpp` 里这两条的 `DECLARE(...)` 当前默认值就是 1000/3000,这点没有争议)。有争议的是它们**各自在哪个版本被上调**:`parts_to_throw_insert` 从 300 提到 3000 有官方版本标注——`parts_to_*` 设置页写明"23.6 之前该值为 300",**这条可以确定**;**`parts_to_delay_insert` 从 150 提到 1000 落在哪个版本,各家来源冲突、没有可靠定论**——一边是官方 23.6 的变更条目里有"分区内 part 数超过 1000 时增强整体韧性"这类表述(其中出现的 1000 正是 delay 的新值),支持"它也在 23.6";另一边是托管版 ClickHouse 的 API 文档把它系于 25.1(写成"25.1 及以上为 1000、24.12 及以下为 150"),且官方设置页**只给 `parts_to_throw_insert` 加了版本标注、`parts_to_delay_insert` 条目没有任何版本标注**(若它同为 23.6 所改,文档多半会一并标注)。两方证据我无法在本环境裁决,故这条标**未确认**。**别把两个阈值混成"23.6 之前都是 150/300"一句话**——它们是否同一版本变更本身就没有定论;最稳的做法是**直接读你那一版的实际值**(`system.merge_tree_settings` 里就能看到),因为老集群的配置里可能还残留旧值。`max_parts_in_total` 默认 **100000**,同样建议现场核对。

**减速还有个前置条件:`max_avg_part_size_for_too_many_parts`(默认 1 GiB)。** 只有当"相关分区的平均 part 大小"**不超过**这个阈值时,`parts_to_delay_insert` 的减速才会生效;如果 part 虽然多、但平均下来单 part 都超过了 1 GiB,那更像"表本来就巨大",而不是"写入太碎",插入不会被减速。所以判断"插入会不会被限速"要看两个量:分区 part 数**超过** `parts_to_delay_insert`,**且**平均 part 大小**低于** `max_avg_part_size_for_too_many_parts`。

**但请记住:撞上 too many parts,根因永远是写入方式,不是参数太小。** 正确的反应是"是不是在高频小批量 `INSERT`"——每秒一次单行 `INSERT`,一天就是八万多个 part,再大的闸门也拦不住。修法是**攒批**:客户端攒够几万行或几百 KB 再发一次,让每次插入产出少数几个甚至一个 part。把 `parts_to_throw_insert` 调大只是把崩溃往后推,治标不治本。

如果"攒批"不方便在客户端做(比如上游是一堆各自发单行的小服务),ClickHouse 提供了**服务端攒批**:

```text
async_insert                     默认 0    打开后,INSERT 先落内存缓冲,攒够再合批落盘
wait_for_async_insert            默认 1    客户端是否等到"真正落盘、产生了 part"才返回
async_insert_max_data_size       攒批的字节阈值
async_insert_busy_timeout_ms     攒批的时间阈值(到点即使没攒够也刷)
```

打开 `async_insert` 后,多个小 `INSERT` 会被服务端攒进同一批、一次产出少数几个 part,从源头把 part 数量压下来。默认 `wait_for_async_insert = 1` 时,客户端仍能感知落盘错误、也仍能等到数据可见,只是 part 数降下来了;设成 0 会更快,但客户端拿到 OK 时数据可能还没落盘,服务端在刷盘前崩溃就可能丢 —— 这跟 `Buffer` 表"可能丢数据"是同一类取舍(第 04 章 §04.6)。所以原则是:**能改客户端就优先改客户端**(可控、语义清楚);确实改不动,再用 `async_insert` 兜底。

**part 太多之所以致命,还有一个物理层面的原因:文件数。** 每个 part 在磁盘上是一组文件,这组文件有多少个,取决于它落盘成 **Wide** 还是 **Compact** 形态——Wide part 每列一组文件(数据 `.bin` 加标记 `.mrk2`,与 §05.4、§05.5 的写法一致),Compact part 把所有列挤进同一个文件。默认阈值(`min_bytes_for_wide_part` 约 10 MiB、`min_rows_for_wide_part` 为 0)之下的小 part 写成 Compact,所以高频小批量写入制造的那些小 part 默认并不会立刻膨胀成"列数 × 2"倍的文件数。但要看清它**只是把每次写入的文件数压下来,并不能阻止 part 数增长**:part 数撞上闸门时,总量仍然是灾难,Compact 救不了你。两种形态的完整对照(布局、列裁剪上的代价、part 长大之后什么时候转回 Wide)在第 05 章 §05.10,这里不重复。

## 18.7 分区操作:DROP / DETACH / ATTACH / MOVE / REPLACE / FREEZE

分区最大的价值不是查询裁剪,而是**整块删除的 O(1)**:

```sql
ALTER TABLE t DROP PARTITION 202403;      -- 删掉整个分区(毫秒级)
ALTER TABLE t DETACH PARTITION 202403;    -- 把分区挪到 detached/ 目录
ALTER TABLE t ATTACH PARTITION 202403;    -- 把 detached/ 里的分区挂回来
ALTER TABLE t FREEZE PARTITION 202403;    -- 对分区做硬链接快照(备份用)
```

`DETACH` + `ATTACH` 的手法值得记住:**它能把一个分区"秒级"地搬到另一张表**——`DETACH PARTITION` 后,`detached/` 目录里的 part 目录可以被移动到另一张表的 `detached/` 下,再 `ATTACH` 挂载。这是一个接近零成本、不经过 SQL 层的批量数据搬运/迁移手段(前提是两张表结构兼容)。

还有 `MOVE PARTITION TO TABLE`(把分区移到另一张表,SQL 层封装了上面的搬运)与 `REPLACE PARTITION`(用另一张表的同名分区**原子替换**当前分区)。`REPLACE PARTITION` 是"分区级原子 swap",做 A/B 表切换、蓝绿发布很有用。

## 18.8 Mutation:不是就地改,而是重写 part

`ALTER TABLE ... UPDATE` / `DELETE` 在 ClickHouse 里叫 **mutation**,它的执行模型与行存数据库完全不同:

```text
ALTER TABLE t UPDATE x = 1 WHERE id = 5
   │
   ├─ ① 立刻返回:生成一条 mutation 记录(带递增编号)
   │        system.mutations 里立刻能看到:
   │          mutation_id   command        is_done  parts_to_do
   │          0000000001    UPDATE x=1 …   0        3
   │
   └─ ② 后台池的空闲线程挑出受影响的 part,把它重写一遍,
          产出"改过的"新 part;parts_to_do 递减,到 0 则 is_done = 1
```

三条性质必须记牢:

**不是就地改。** 它读出整个 part → 套用谓词/赋值 → 写一个全新 part → 把老 part 标记删除。改一行和改一百万行的代价差不多,都是"重写整个 part"。

**异步。** 提交后可能几秒生效,也可能几小时——取决于后台池是否被合并和其它待办占满。`OPTIMIZE TABLE ... FINAL` 能立刻催它。

**串行。** 同一张表上的 mutation 按**创建顺序**应用,后面的等前面的完成。所以一个卡住的 mutation 会堵住后面所有的。

观测点在 `system.mutations`:`parts_to_do` 不为 0 就说明"还没生效完",而它**由后台池的线程推进**(与合并共用同一个池,但不必等某个合并发生)——你在等它的时候,除了看 `system.mutations` 的 `parts_to_do`,还应该看后台池是不是被合并占满了(§18.12)。卡住了可以用 `ALTER TABLE t KILL MUTATION WHERE mutation_id = '...'` 停掉。**结论:大批量的 `UPDATE` 不如重建表**——`INSERT INTO t_new SELECT ... FROM t` 再换名,比让 mutation 一条条重写所有 part 快得多,也更可控。

`number_of_free_entries_in_pool_to_execute_mutation` 控制"后台池至少留几个空闲名额才执行 mutation",避免 mutation 把合并线程全占满。

## 18.9 轻量级删除:`DELETE FROM` 是怎么做到不重写 part 的

ClickHouse 支持不带 `ALTER` 的 `DELETE FROM t WHERE ...` 语法,它被实现成**轻量级删除**(lightweight delete, LWD)。它早已不是实验特性:**22.8 引入(实验),23.3 起 GA(转正并默认可用)**;转正时开关也更名:`allow_experimental_lightweight_delete` 变成 `enable_lightweight_delete`(**默认 1**,旧名保留为别名),所以 23.3 之后不再需要手动打开它。

原理很直接:**不重写 part**,而是登记一次"按行打标"的修改,把被删的行在一个隐藏的 `UInt8` 掩码列 **`_row_exists`** 上标成 `0`(`1` = 可见)。语义上等价于

```sql
ALTER TABLE t UPDATE _row_exists = 0 WHERE <你的条件>
```

但**执行路径不同**:`SELECT` 会被自动补上一个对 `_row_exists` 的 `PREWHERE` 过滤,于是**被删的行立刻从查询结果里消失** —— 这正是它与普通 mutation 的最大区别。而**数据的物理删除仍是异步的**,留给后台合并去清。`_row_exists` 这一列只在"这个 part 里确实发生过删除"时才存在,不是每张表都凭空多一列。

几个必须知道的点:

- **和 `FINAL` 的关系**:`SELECT ... FINAL` 同样尊重这个掩码、把已删行过滤掉。但掩码与"按排序键合并去重"是两套机制,它在不同引擎(尤其 `ReplacingMergeTree`)下与 `FINAL` 的交互要**在你那一版上实测确认**。
- **删除之后 `INSERT` 进来的行不受影响**:掩码是**按 part** 的,新写入的 part 天然"全可见",所以不会把后插入的、键相同的新行误删 —— 这也正是它比"重跑一次 `ALTER DELETE`"更安全的场景。
- **同步程度由设置控制**:

```text
lightweight_deletes_sync   默认 2
```

`0` = 提交后立即返回、删除在后台慢慢体现;`1` = 等本机执行完;`2`(默认)= 等所有副本都执行完;`3` 在部分共享存储引擎上表示"只等活跃副本"。**取值的精确语义随版本变化,以你那一版的文档为准。**
- **代价仍在磁盘上**:被删的行在合并清掉之前一直占空间。想让它尽快消失,可以手动催合并(`OPTIMIZE ... FINAL`,§18.5),或依赖 `min_age_to_force_merge_seconds`(§18.2)带来的自动合并;合规场景需要"立即、不可恢复地物理删除",仍应使用 `ALTER TABLE ... DELETE`。

mini-clickhouse **没有实现**轻量级删除(没有 `DELETE FROM` 的专门处理)。

## 18.10 TTL:四类动作

TTL 是 ClickHouse 里最省运维的特性——"少查就删",声明式,无 cron:

```sql
-- ① 行级 TTL:30 天前的明细自动删除
TTL d + INTERVAL 30 DAY DELETE

-- ② 冷热分层:7 天前的数据挪到慢盘
TTL d + INTERVAL 7 DAY TO DISK 'slow_disk'

-- ③ 重新压缩:3 天前的数据换更省空间的编码
TTL d + INTERVAL 3 DAY RECOMPRESS CODEC(ZSTD(9))

-- ④ 列级 TTL:某个列单独过期(只清空该列)
raw String TTL d + INTERVAL 1 DAY DELETE
```

四个动作对应四种资源维度:`DELETE` 回收**行数**,`TO DISK`/`TO VOLUME` 回收**盘位**,`RECOMPRESS` 回收**磁盘字节**。还可以给 TTL 加 `WHERE`(只对满足条件的行生效),或者用 `GROUP BY` 做**聚合式 TTL**(`TTL time + INTERVAL 1 MONTH GROUP BY key SET x = sum(x)`)——把老数据按 key 聚合成一行,这是"用空间换时间"的降采样。

执行上,TTL 走的是**后台合并那条流水线**:合并时顺带把过期行删掉。所以 TTL 生效也是异步的,和合并频率绑定。`merge_with_ttl_timeout`(默认 **4 小时**,请现场核对)是"至少隔多久才为 TTL 触发一次合并"。

## 18.11 ttl_only_drop_parts:最重要的一个 TTL 优化

TTL 到期时,ClickHouse 有两条路:

```text
路线 A(逐行删):读出整份 part → 只丢掉过期的那几行 → 把剩下全部写回
                代价 = 重写整个 part(可能几百 MB IO)

路线 B(整 part 丢):如果 part 里**所有**行都过期 →
                直接把 part 目录删掉,元数据操作,O(1)
```

路线 B 快上百倍,而 `ttl_only_drop_parts = 1` 就是"**只在整 part 过期时才丢它,否则不做任何事**"——宁可让数据多留一会儿,也不做那个昂贵重写。它的默认是 **0**(即允许路线 A 的部分重写)。

**但根本的杠杆不是这个开关,而是把 TTL 和分区配合。** 如果 `PARTITION BY toYYYYMM(d)` + `TTL d + INTERVAL 6 MONTH DELETE`,那么每个月的 part 会在"月末 + 6 个月"之后**整体**过期,天然走路线 B,一次元数据操作回收几十 GB;而如果只有行级 TTL、没有分区,就永远是路线 A,反复重写。**这就是"分区是为运维而不是为查询准备的"那句话的又一层含义**。

## 18.12 后台任务的资源竞争

合并、mutation、TTL、副本拉取(fetch)这几件事**共享同一个后台线程池**(`background_pool_size`,默认 16)。这意味着它们会互相排队:一堆大合并把池子占满,mutation 就得等;这又会让 `DELETE` 迟迟不生效,积压的 mutation 反过来又加重后续合并的负担。所以监控不能只看一个:`system.merges` 看正在跑的合并、`system.mutations` 看 mutation 积压、`system.replication_queue` 看复制任务积压,三者要一起看,才能判断瓶颈在哪。想复盘"过去这段时间合并都干了什么、有没有哪次特别慢",再叠加 `system.part_log`(§18.2)。

**池子占满之后会发生什么?** 不是报错,而是**排队**:新的后台任务(合并、mutation、TTL 老化、副本 fetch)拿不到名额就等着,落到各自的任务队列里。表现是——`system.merges` 里"正在跑"的任务数长期贴着 `background_pool_size` 的上限(默认 16),同时 `system.mutations` 的 `parts_to_do` 迟迟不降、`system.replication_queue` 的 `queue_size` 只涨不落。

**怎么判断"是谁在堵谁"?** 队列本身不告诉你谁在占着池子,要把 `background_pool_size`(池子有多大)和上面几张表的**队列长度**并起来看:池子已经满、而 `system.merges` 里挂着几个超大合并任务,那多半是合并在压着 mutation。**"合并 / 变更 / TTL / fetch 抢的是同一个池"这句话的实际含义就在这儿**:调大 `parts_to_delay_insert`、调大 `max_bytes_to_merge_at_max_space_in_pool` 之类的旋钮,都只是在这一个**固定大小**的池子里重新分配名额,治标不治本;要真正缓解,要么把 `background_pool_size` 调大(受 CPU 核数约束),要么从源头减少后台要处理的工作量(§18.6 的攒批、§18.11 的 TTL 配分区)。

## 18.13 冷热分层存储

冷热分层的配置在服务端 `config.xml` 的 `storage_configuration` 段里,分三层:`disk`(物理盘)→ `volume`(一组盘)→ `policy`(卷的排序与搬移规则)。**这套配置的完整 XML——本地多盘,以及把冷盘换成对象存储的 S3 磁盘——在第 29 章 §29.13 逐段给出,本节不重复抄写**,只把"它靠什么驱动"讲清楚。

**卷的书写顺序就是数据的下沉方向。** policy 的 `volumes` 从上到下排列,后台的 move 任务只会把数据往**后面的**卷搬,不会往回搬——所以"热卷在前、冷卷在后"这个顺序本身就是策略的一部分。

**`move_factor` 决定"什么时候开始往下搬"。** 当某个卷的用量超过 `1 - move_factor` 时,后台就开始把该卷上的 part 挪到下一个卷。它的**默认值是 `0.1`**,也就是"用到 90% 就开始下沉";调大它会让搬移更早发生,而真正干活的线程池是 `background_move_pool_size`(第 25 章)。

**触发搬移的是两条独立路径,别混为一谈。** 一条**按数据年龄**:靠 TTL 的 `TO DISK` / `TO VOLUME` 动作,数据到点后整个 part 被搬到指定盘或卷。另一条**按 part 大小**:volume 上的 `max_data_part_size_bytes` 在**写入时**就把超过阈值的 part 直接分到下一卷——它管的是"大 part 别挤占小卷",与年龄无关。两条可以同时存在。

建表时用 `SETTINGS storage_policy = 'tiered'` 选策略,再用 TTL 把数据往冷卷赶:

```sql
TTL d + INTERVAL 7 DAY TO VOLUME 'cold'
```

于是热数据留在 SSD、冷数据自动沉到机械盘或对象存储,而查询仍能透明地访问全部数据。要把冷卷换成**对象存储**,只需把冷盘的 `<path>` 换成 `<type>s3</type>` 加 `endpoint` / `access_key_id` / `secret_access_key` 这几个连接字段(bucket 写进 `endpoint` 的路径里)——这份配置本身、以及 `region`、走环境变量凭据、超时重试等随版本演化的字段,见第 29 章 §29.13;更完整的对象存储磁盘参数与存算分离的取舍,见第 30 章 §30.4(对象存储与数据湖)。

## 18.14 磁盘不够了怎么办

一条从轻到重的处置清单,每一步都给出"先看什么、再动什么"。

1. **先量:是谁把盘占掉了,别急着删。** `system.parts` 的 `bytes_on_disk`(带上 `active = 1`)按表、按分区聚合,就能把"磁盘被谁吃掉"还原成一张排行:

   ```sql
   SELECT database, table, partition, sum(bytes_on_disk) AS b
   FROM system.parts
   WHERE active
   GROUP BY database, table, partition
   ORDER BY b DESC
   LIMIT 20;
   ```

   想再往下钻到**列**,用 `system.parts_columns` 的 `column_data_compressed_bytes` / `column_data_uncompressed_bytes`(§05.14、§20.6),哪一个列明显偏大,就是"该换 `CODEC` 了"的信号。磁盘涨得快往往不是"数据变多了",而是"某列选错了编码"或"某张表没配 TTL"——不看这一眼就删数据,经常删错地方。
2. **删能整块删的:先删分区。** `ALTER TABLE t DROP PARTITION 202403` 是 O(1) 的元数据操作,毫秒级回收几十 GB(§18.7)。它是所有手段里唯一"又快又便宜"的一条,所以排在最前面。
3. **确认 TTL 真的在工作。** 观测点有两个:`system.parts` 里老分区还在不在(在,就说明还没删掉),以及 `system.merges` 里有没有 `merge_reason` 属于 TTL 的合并在跑(§18.2)。最常见的根因是 **TTL 没和分区配合**——只有行级 TTL、没有 `PARTITION BY`,数据就只能走"逐行重写整个 part"那条慢路线(§18.11);补上分区、让每个 part 能整体过期,才是治本的一步。
4. **清 `detached/` 里的垃圾。** `DETACH PARTITION` 与坏 part 隔离都会把 part 挪进 `detached/`,而它们**不会**被自动回收,时间一长能堆出实实在在的占用。`system.detached_parts` 列出这些孤儿,再用 `ALTER TABLE t DROP DETACHED PARTITION '202403'` 或 `ALTER TABLE t DROP DETACHED PART 'all_1_1_0'` 把它们物理删掉。清一次常常能再腾出一大块。
5. **配冷热分层,把冷数据赶到便宜的介质上。** 前几步是"让数据变少",这一步是"让剩下的数据变便宜"(§18.13、第 29 章 §29.13)。
6. **最后才是加盘。** 扩 `storage_configuration` 里的 disk、给 policy 加一个卷。

顺序不能反——先加盘只是把问题往后推,而前面几步才是让数据**自己变少、变便宜**。还有一条要提前做:真到了"删数据"这一步,先确认**备份**在(第 29 章 §29.9),`DROP PARTITION` 没有回收站。

## 18.15 运维开关:轻量级 UPDATE、mutations_sync 与 SYSTEM STOP/START MERGES

§18.9 讲了轻量级删除的同步设置 `lightweight_deletes_sync`;这一节把配套的几件事补齐。

**mutation 也有同步开关:`mutations_sync`。** 它是 `lightweight_deletes_sync` 的对照:`ALTER TABLE ... UPDATE/DELETE` 默认(0)提交后立即返回、在后台慢慢推进;设成 1 时等**本机**执行完再返回,设成 2 时等**所有副本**都执行完。它解决的是"脚本里提交完 mutation 就想接着读到新数据"这类需求,代价是阻塞——对大批量 mutation 用 2 会长时间挂住,别放进常规流程。

**不带 `ALTER` 的 `UPDATE t SET ... WHERE ...` 也走一条轻量路径,但它是很新的能力:25.7 才引入,25.8 从实验转 Beta,所以在 25.3 上尚不存在。** 它和轻量级删除同源:不重写 part,而是登记一次"按行改值",查询时把新值叠加上去、物理实体化留给后台。但它的**前置条件比轻量级删除多**,必须点名:**会话/查询级要打开 `enable_lightweight_update`(25.8 起默认 1;旧名 `allow_experimental_lightweight_update` 保留为别名);表级还要打开 `enable_block_number_column` 与 `enable_block_offset_column` 两个设置**——它们把 `_block_number` / `_block_offset` 这两个虚拟列物化出来,轻量更新正是靠它们定位到具体哪一行。表级那两个没开时不会"悄悄退化",而是**直接报错**:典型文本是 `Lightweight updates are not supported. Lightweight updates are supported only for tables with materialized _block_number column`。它的物理实体是 **patch part**:写出一份只含被改列的补丁 part,查询时叠加上去,之后才在合并/变更里物化——所以它**不是**"单纯登记一次按行改值"。要用之前先在目标版本上确认这几个开关是否已被接受、名字是否一致(该特性 25.8 仍是 Beta,以目标版本文档为准)。

**`SYSTEM STOP MERGES` / `SYSTEM START MERGES` 暂停与恢复后台合并。** 可以指定 `db.table`(只停一张表),也可以停整库或整实例。用途集中在"想在一个稳定快照上做某件事"的时候:排查合并风暴、执行 `FREEZE` 备份期间冻结合并,避免"备份做一半 part 又变了"。配套的还有只管 TTL 触发合并的 `SYSTEM STOP/START TTL MERGES`、只管 TTL 跨卷搬移的 `SYSTEM STOP/START MOVES`(**它们的确切名字与作用范围以目标版本文档为准**)。

要提醒的是:**停得越久,恢复后越容易一次性爆发大量合并**。停合并不是"关掉合并",只是把欠账攒起来;它适合短时操作,不适合当常态。真正让 part 数量降下来的办法,还是 §18.6 的攒批。

## 18.16 与 mini-clickhouse 的对照

配套实现的四个模块对着这一章,都在 `minich/storages/merge_tree/` 下:`merger.py`(992 行,合并本体与家族折叠)、`merge_policy.py`(242 行,`SimpleMergePolicy`)、`mutations.py`(403 行)、`ttl.py`(475 行)。

**对齐点**:合并策略的三条约束与参数(`max_size_ratio=3.0`、`150 GiB`、`max_parts_to_merge_at_once=100`、可用空间比 `0.1`)与真实默认值一一对应;合并结果的 part 名规则(`level = max+1`、block 区间取并集)与 `MergeTreePartInfo` 一致;`system.mutations` 的列(`mutation_id` 零填充到 10 位、`command`、`is_done`、`parts_to_do`、`latest_fail_reason`)与真实一致;mutation "被重写的 part 会顺带套用变更"这一层与真实一致(但**触发时机**不同:真实 CH 由后台池独立推进,mini 只在 part 参与合并或 `OPTIMIZE` 时顺带做);`ttl.txt` 只记每个动作的**极值时间**(`delete_at = max(ttl_expr)`)而不是逐行过期时间,与真实一致。实测:`level=0` 的三个 part 经 `OPTIMIZE FINAL` 合成 `all_1_3_1`(300 行,`level=1`,`min_block=1`,`max_block=3`,即"合并结果 level = 源 part 最大 level + 1");engine 的 `mutate()` 登记一条 mutation 后,`system.mutations` 立刻显示 `is_done = 0`、`parts_to_do = 当前 part 数`,经 `OPTIMIZE` 后递减到 0。

**简化点**:

- **后台合并是单线程,不是线程池。** 尽管 `DEFAULT_SETTINGS` 里登记了 `background_pool_size = 16`,`start_merges` 也只起**一个** `threading.Thread` 跑 `_merge_loop`;而且 `background_pool_size` 被列在 `modify_setting` 的 `_KNOWN_BUT_IGNORED_SETTINGS` 里,`ALTER ... MODIFY SETTING background_pool_size = N` 会直接抛 `NOT_IMPLEMENTED`(理由:只有一个合并线程,改了也不会多出并发)。后果是合并吞吐上限比真实低,更容易撞上 `parts_to_throw_insert`——代码把这标为"可预测性优先于吞吐"。
- **mutation 的触发面更窄**:只在"part 参与合并"或 `OPTIMIZE` 时推进,没有真实 CH 里那套"空闲线程主动挑 mutation 执行"的独立路径。
- **TTL 做了合并两条路线的简化**:当"整个 part 都过期"时,mini **无论 `ttl_only_drop_parts` 如何**都直接丢弃整个 part;真实 CH 在 `ttl_only_drop_parts = 0` 下会先重写出一个空 part 再删掉它(多一次无谓 IO)。结果一致,少了一次重写。
- **合并只有横向一种。** 不管 part 多宽,`merger.py` 都先把所有列读进内存(`self.data[name] = part.read_column(...)`),再一行一行地归并——没有 §18.1 那套"逐列 gather"的纵向合并,也没有 `vertical_merge_algorithm_*` 那几个阈值。它因此复现不了一次纵向合并的内存行为;`system.merges` 里虽然有一列 `merge_algorithm`(类型是 `String`),但引擎从不填它,默认恒为空串,不会出现真实现里的 `Horizontal` / `Vertical`。
- **没有实现**:`ALTER ... MOVE PARTITION`(语法能被解析,引擎无对应方法)、`REPLACE PARTITION`、轻量级删除(`DELETE FROM`)、`storage_configuration`(即 `TO DISK`/`TO VOLUME` 这些动作在 `TTLDescription` 里**能被描述**,但引擎不执行真正的跨盘搬移)。`FREEZE` **真的会建快照**:它把 part 文件拷进 `shadow/<backup>/`(用**拷贝**代替真实 ClickHouse 的**硬链接**),而且 SQL 入口 `ALTER TABLE ... FREEZE [PARTITION p]` 也已接通——不是空操作。

通过 DDL 触发 mutation 这条路是通的:实测 `ALTER TABLE t UPDATE b = 9 WHERE a = 1` 正常执行,`OPTIMIZE FINAL` 之后能查到新值,`system.mutations` 里那条 `is_done` 变成 1;直接调用 `engine.mutate(MutationCommand(...))` 同样正常。

## 18.17 小结

这四件事都由"part 不可变"一条推出。

**合并**把碎 part 收敛成大 part,三条约束是**连续、大小相近、总量受限**(默认 `max_size_ratio=3`、上限 150 GiB);它异步、可被 `OPTIMIZE FINAL` 催,所以查询可能读到重复行——但**"合并会去重"只在 `ReplacingMergeTree`/`SummingMergeTree`/`AggregatingMergeTree`/`CollapsingMergeTree` 这些子族上成立,普通 `MergeTree` 一行都不丢**(§18.4)。其中 `CollapsingMergeTree` 的 `sign` **只在合并时才抵消**,裸读 `SELECT` 拿到的是未合并的原始行,必须用 `GROUP BY 排序键 ... sum(sign) ... HAVING sum(sign) > 0`(或 `FINAL`)收口才不会读到翻倍或对不上的数;写入可能乱序时,改用带版本列的 `VersionedCollapsingMergeTree`。合并选择基本只有 `SimpleMergeSelector` 一个,能调的是 `min_age_to_force_merge_*` 等几个旋钮;看历史用 `system.part_log`,看当前用 `system.merges`。`OPTIMIZE` 有 `PARTITION p FINAL`、`DEDUPLICATE [BY ...]` 等变体,`FINAL` 绕过合并大小限制、阻塞且昂贵,属运维动作。合并**怎么合**还分两条算法:默认的横向合并内存与**列数**成正比,宽表在大 part 上会切到**纵向合并**(先归并主键列定行序、再逐列 gather,把峰值内存从"列数倍"压到常数倍),门槛是 `enable_vertical_merge_algorithm`(默认 1)再配行数(131072 = 16×8192)与非主键列数(≥ 11)两条;**走哪条看 `system.merges` / `system.part_log` 的 `merge_algorithm` 列**(取值 `Undecided`/`Horizontal`/`Vertical`),它也是"宽表合并为什么吃内存"的第一手答案(§18.1)。

**mutation** 不是就地改而是重写 part,异步、串行、由后台线程池排队推进(它**和合并共用资源池,但不必等某个合并**;只有 mini-clickhouse 走的是"靠合并推进"的简化路径),`system.mutations` 的 `parts_to_do` 是唯一的进度条,大批量改不如重建表。**轻量级删除**(`DELETE FROM`,23.3 起默认可用)用 `_row_exists` 掩码让删除**立刻可见**、物理清理仍异步,同步程度由 `lightweight_deletes_sync` 控制,它的对照是 mutation 侧的 `mutations_sync`(§18.15)。**TTL** 有 `DELETE`/`TO DISK`/`TO VOLUME`/`RECOMPRESS` 四类动作,`ttl_only_drop_parts` 让"整 part 过期就走 O(1) 删除",而**真正的杠杆是 TTL 与分区配合**。**分区操作**里 `DETACH`+`ATTACH`、`MOVE`/`REPLACE PARTITION` 是秒级数据搬运与原子切换的手段。

**后台的合并 / mutation / TTL / fetch 共享同一个池子**(`background_pool_size` 默认 16),会互相排队;短时冻结后台合并用 `SYSTEM STOP/START MERGES`,别当常态。撞上 "too many parts" 永远先查写入方式(小批量高频 `INSERT`):客户端攒批治本,实在改不动再用 `async_insert` 做服务端攒批;"延迟插入"是否生效还取决于 `max_avg_part_size_for_too_many_parts`(默认 1 GiB,平均 part 超过它就不再减速)。磁盘紧张时按 §18.14 的顺序走:**先量谁占盘,再删分区 → 修 TTL → 清 detached → 冷热分层 → 最后才加盘**。
