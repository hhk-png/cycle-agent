# 第 13 章 物化视图与增量聚合

先给一句会被反复引用的判断:**ClickHouse 的物化视图不是缓存,是一条挂在源表上的 INSERT 触发器。** 它不是"定时把查询结果算好存起来",而是"源表每收到一次插入,就把你写的那条 SELECT 在**这一批新行**上跑一遍,把输出写进目标表"。这一句话推得出它的全部性质,也推得出你迟早会踩的每一个坑:为什么建视图之前的数据看不到、为什么结果是一个劲儿往后追加而不是原地更新、为什么增量聚合必须存**状态**而不是存结果、为什么两次插入可能互相"去重"掉。这一章就顺着这一条机制往下走。

第 04 章 §04.8 已经给过物化视图的简写版,第 12 章 §12.10 给过一个 `-State`/`-Merge` 的最小例子。本章是它们的展开:把"触发"这条机制推到底,把增量聚合的完整链路、幂等、链式视图、回填、可刷新视图、和投影/普通视图/手搓预计算表的取舍,以及源表是 `Distributed`/`Buffer` 这类特殊表时的边界情况与排障,都补齐。

## 13.1 物化视图到底是什么 —— 从一个机制推出全部性质

写下一句:

```sql
CREATE MATERIALIZED VIEW mv TO dest AS SELECT ... FROM src;
```

ClickHouse 做的**不是**"现在去跑一遍这个 SELECT 并把结果存下来"。它做的是:**把这条 SELECT 编译并保存起来,注册到 `src` 上**。此后 `src` 每被写入一次,这条 SELECT 就被执行一次。

```text
                     ┌──────────────┐
   INSERT INTO src ──►│   src 的 part │
                     └──────┬───────┘
                            │ 同一批新行(一个 block)
                            ▼
                    ┌───────────────────┐
                    │  mv 的 SELECT     │   ← 就在这条 INSERT 的同步路径上
                    └─────────┬─────────┘
                              │ 输出(若干行 / 若干状态)
                              ▼
                     ┌──────────────┐
                     │  dest 的 part │
                     └──────────────┘
```

从这唯一一条机制出发,可以推出下面五条性质,每一条都是一个日常故障的根因。

**第一,它只对"触发之后写进来的行"负责。** 视图注册的那一刻就是起跑线:之前已经躺在 `src` 里的数据,`mv` 从来没"看"过,自然也不会出现在 `dest` 里。这不是"延迟",而是**根本没有这件事发生**。所以"我建了物化视图,怎么 `dest` 里只有一半数据"这个抱怨,十次里有九次是这一条。唯一的例外是建视图时写 `POPULATE`(§13.3),而 `POPULATE` 又只存在于不带 `TO` 的形态里。

**第二,它没有"看到删除和更新"的通道。** ClickHouse 的 `src` 上可能发生 `ALTER TABLE ... DELETE`、`TTL` 过期、`ReplacingMergeTree` 的后台去重 —— 这些都是**后台**行为,不产生"插入事件"。触发器只听插入。所以 `dest` 里的数据**永远不会**跟随 `src` 的删除而收缩。这是"物化视图不是缓存"的最硬证据:缓存可以失效,物化视图只会越积越多。要做"删了源表也得跟着删"的联动,得自己在 `src` 上再挂一条以 `sign` 表达删除的写入路径(第 18 章 §18.4 的 `CollapsingMergeTree` 一族就是为这件事准备的;注意它的读侧必须显式用 `sum(sign)` 收口,不然查到的数会偏,见 §18.4)。

**第三,它的输出是"追加",不是"覆盖"。** 因为触发单位是"一批新行",不是"表的新状态"。同一分钟的数据分十次插入,`dest` 里就会留下十批输出;即便这十批描述的是同一分钟、同一个用户。**目标表里的同一 key 会有很多行,这是常态,不是 bug。** 怎么把它们收敛,是本章最核心的问题(§13.4、§13.6),也是目标表引擎必须精挑细选的原因。

**第四,触发粒度是 block,不是 row。** 一次 `INSERT` 里的所有行作为**一批**进入 SELECT。这一条同时是好消息和坏消息:好消息是"同一批里按 key 聚合"能在这一批内部先做掉,省掉大量重复行;坏消息是**批与批之间不聚合**,所以"每批算出来的结果"拼起来不等于"全量算出来的结果"。这正是 §13.4 那组数字的来源,也正是"物化视图做增量聚合必须存状态"的根据。

**第五,它在 INSERT 的同步路径上,所以会让写入变慢、也会让写入失败。** 写 `src` 的那一刻,`mv` 的 SELECT 就要跑完、输出就要落进 `dest`,这条 INSERT 才算成功。于是源表的写入延迟里包含了物化视图的全部计算与写入开销(写放大)。更要紧的是**错误会传导**:如果 `mv` 的 SELECT 抛错、或者 `dest` 写不进去(比如 part 太多触发了第 18 章 §18.6 的闸门),那么**往源表插入会直接失败**。一个下游视图出问题,能让整条上游数据管道停摆 —— 这是物化视图最容易被低估的一笔账。而“源表与目标表的写入”**并不落在同一个事务边界里**:官方明确说,源表的 INSERT 与物化视图目标表的 INSERT 是**两次独立的写入**、不构成一个原子整体,INSERT 进行期间并发客户端可能看到“源表已有、物化视图还没有”(或反过来)的中间态。所以“错误会连带源表插入失败”这个方向是确定的,但别指望“要么都进、要么都不进”。

**还有一条机制,只有当"同一张源表上挂了不止一条视图"时才显形:这些视图默认是一条一条串着跑的。** 这一次 `INSERT` 的那个 block,会被**依次**推给每一个挂在源表上的视图——第一个视图的 SELECT 跑完、结果写进它的目标表,才开始第二个。默认就是这种"串行",因为控制它的会话级设置 `parallel_view_processing` 在 25.3 上默认为 `false`(源码 `src/Core/Settings.cpp`:`DECLARE(Bool, parallel_view_processing, false, "Enables pushing to attached views concurrently instead of sequentially.")`)。把它打开,这些视图才会**并发**各跑各的 SELECT。

这条机制把上面第五点那笔账算得更细:**源表一次 `INSERT` 的延迟,默认是挂在它上面的所有视图开销之和,而不是其中最大的那一个。** 一条源表挂了三条视图,写入延迟就是三段计算加三段写目标表的和;打开 `parallel_view_processing` 之后,这几段可以并行,关键路径上的延迟才有希望从"和"降到"最大者"。

开它之前要把三件事想清楚。一是并发意味着这一次 `INSERT` 会同时占多个线程、多份内存,视图多、目标表大时峰值资源会明显上去。二是并发之后,视图之间**没有先后保证**——如果你有"A 视图的输出必须先于 B 视图"这类隐含依赖(比如两条视图都往同一张目标表写、而你又依赖写入次序),并发会把这个次序打乱,默认的串行反而是更稳的那一档。三是最重要的一条:**它改不了上面第五点那条最硬的账**——视图依然跑在 `INSERT` 的同步路径上,任何一个视图报错,源表插入照样失败;并发只是把"串行地等"换成"并发地等、一起承担失败",并不会把物化视图从写入路径上摘下来。

所以判据是:**只有当"这张源表上挂了多条互不依赖的视图、且写入延迟确实是串行等待堆出来的"时,才去开 `parallel_view_processing`**;开之前先确认视图之间没有次序依赖,开之后盯一下 `INSERT` 的线程占用与内存峰值。

关于源表 schema 变化还有一条要提前记住:**`mv` 里写的 SELECT 是在建视图时被解析并固化的。** 所以源表上 `ALTER TABLE ... ADD COLUMN` 之后,`mv` **不会**自动把新列带上——它跑的还是建视图那一刻那份列清单。它的机制是:**输出列随 `CREATE` 语句文本一起被持久化**,所以 `SELECT *` 也当场**展开成一份固定列清单**——源表后来新增的列不会自动出现在视图里。稳妥的做法只有一条:**物化视图的 SELECT 里永远显式写列名,不要用 `*`**。

## 13.2 两种形态:插入触发器与可刷新任务

"物化视图"这个名字在 ClickHouse 里盖着两种**语义完全不同**的东西,`第 04 章 §04.8` 已经点过,这里把它彻底切开:

```text
形态 A:插入触发器(默认)
    CREATE MATERIALIZED VIEW mv TO dest AS SELECT ... FROM src;
    └─ 对「新写进 src 的行」负责
    └─ 事件驱动、增量、便宜
    └─ 触发之后的数据才有;历史数据要另想办法(§13.7)

形态 B:可刷新任务(带 REFRESH)
    CREATE MATERIALIZED VIEW mv REFRESH EVERY 10 MINUTE TO dest
    AS SELECT ... FROM src;
    └─ 对「src 当前的整个状态」负责
    └─ 时间驱动、全量、昂贵
    └─ 每次刷新重跑整条 SELECT,拿到的是一个自洽快照
```

判据只有一句话:**形态 A 对"新的行"负责,形态 B 对"整个源表的现在"负责。** 选错的症状非常有辨识度:你需要的是"整张源表的当前汇总",却建成了形态 A,于是报表里永远缺着建视图之前的那一段,而且缺得**静默无声**。反过来,你要是只需要"新来的数据顺手加工一下",却建成了形态 B,那就白白每十分钟重扫一遍全表。

这两种形态在语法上分属两套机制:A 是 `CREATE MATERIALIZED VIEW ... AS SELECT` 的默认形态,`TO` 决定落点;B 必须显式写 `REFRESH` 子句,它的调度、`APPEND`/覆盖、依赖、观测都自成一套,§13.8 单独讲。还要说明的是,本节与 §13.8 之外,视图族里还有一个更边缘的 `WindowView`(按时间窗口聚合、支持 `WATCH`):25.3 的文档给它挂的是 **Experimental(实验特性)徽标,不是废弃**;`第 04 章 §04.8` 给过结论:**不要在新设计里用它**,这里不再展开。这里最容易记混的是——真正被文档标为 **Deprecated(已废弃)** 的是另一个东西:**Live View**(它当年和 `WATCH` 是搭档)。别把"实验"的 `WindowView` 和"已废弃"的 `Live View` 记成一回事。

## 13.3 DDL 的三种落点:`TO` 目标表、自带内部表、`POPULATE`

形态 A 的 `CREATE MATERIALIZED VIEW` 语句里,真正决定"结果写哪儿"的是三种写法。

**写法一:`TO db.dest`。** 结果写进一张**已经存在的**表 `dest`。这个形态的关键性质是:**物化视图自己没有存储**——它只是一段"触发器 + SELECT + 一个指向 `dest` 的引用",数据全在 `dest` 里,而 `dest` 的引擎(`AggregatingMergeTree`、`SummingMergeTree`、`ReplacingMergeTree` 还是普通 `MergeTree`)由你自己挑。这是唯一一个你能**指定目标表引擎**的形态,所以它也几乎是所有严肃用法的选择。带 `TO` 的物化视图,`DROP` 掉视图时 `dest` 会**原样保留**(它是一张独立的表);反过来 `DROP` 了 `dest`,视图还在、但下一次源表插入会因为"写不进目标表"而失败。

**写法二:不带 `TO`,自己带 `ENGINE`。**

```sql
CREATE MATERIALIZED VIEW mv
ENGINE = AggregatingMergeTree
ORDER BY (minute, user_id)
AS SELECT toStartOfMinute(ts) AS minute, user_id, uniqState(user_id) AS uv
FROM src GROUP BY minute, user_id;
```

这时 ClickHouse 会替视图**自动创建一张内部表**来存结果,列由 SELECT 的输出决定,引擎由 `ENGINE` 子句(以及 `ORDER BY`/`PARTITION BY`/`PRIMARY KEY`)决定。内部表的名字形如 `.inner.<视图名>` 或 `.inner_id.<uuid>`,在 `system.tables` 里能看见它——但**不要依赖它的确切命名**,版本之间变过。这个形态的好处是"一句话建完,不用先建目标表",代价是:引擎藏在视图的 DDL 里、目标表不能单独 `ALTER`、多个视图也不能共用同一张目标表。生产上更推荐写法一。

**写法三:加一句 `POPULATE`。** 它在建视图的同时,**把源表里已有的数据一次性灌进目标表**:

```sql
CREATE MATERIALIZED VIEW mv ENGINE = ... AS SELECT ... FROM src POPULATE;
```

`POPULATE` 有两个必须钉死的点。

第一,**它和 `TO` 互斥**。两句一起写,建表时直接报错(大意是"创建物化视图时不能同时声明 `TO` 和 `POPULATE`")。原因藏在实现里:`POPULATE` 的回填走的是"**视图自己的内部表**"那条路——先在内部表上把历史数据算一遍,而 `TO` 形态**根本没有内部表**,两套机制在结构上就不相容。这不是一个"暂时没实现的功能",而是两种设计互斥。所以"`TO` 形态下怎么补历史数据"这个问题,答案是 §13.7 那句:先建视图,再手动 `INSERT INTO dest SELECT ... FROM src`。

第二,**它带竞态**。回填是一趟读,而回填的**同时**源表可能还在被写。那些"回填开始之后、内部表建好之前"写进来的行,`POPULATE` 可能漏掉;而"回填读取的范围内、又同时被视图触发写了一遍"的行,可能被算两次。所以官方口径是"尽量在低峰期建视图",而更工程化的答案是 §13.7 的分时间段做法。**把 `POPULATE` 当成"方便但不可靠的一次性回填"**,不要把它当成"补历史数据的正确姿势"。

## 13.4 为什么增量聚合必须存状态

现在把 `第 12 章 §12.10` 那个例子接过来,讲透它为什么**必须**存状态。先把机制说清:物化视图按插入块触发(§13.1 第四点),所以同一个月、同一个用户,分五次插入就会在目标表里留下**五份"部分结果"**。目标表里躺着的是五份碎片而不是一个答案。怎么把这五份碎片拼成一个正确答案?有两条路。

**第一条路:存"结果",外层再聚合一次。** 这条路只在少数聚合上侥幸能对。

```text
假设某个 (minute, user) 的明细分两批插入:

  第一批: amount = [10, 20]        第二批: amount = [30, 40, 200]

  存 sum:
       第一批结果 = 30            第二批结果 = 270
       外层 sum(30, 270) = 300    ← 对。因为加法本身可合并。

  存 avg:
       两个「平均值」再平均一次 → 必然错
       (完整推演见第 12 章 §12.1:正确答案是 60,不是 52.5;本章不重算)

  存 uniq(user_id):
       第一批 {a, b} → 2          第二批 {a, c} → 2
       外层 uniq(2, 2) = 1        ← 错得离谱。真实是 3 个不同用户。

  存 quantile(0.5):
       第一批的中位数、第二批的中位数,都没法还原「全体数据的中位数」。
```

`sum` 侥幸能对,是因为**加法本身可合并**;`avg` 一定错,是因为平均值不可合并(`第 12 章 §12.1` 那个 52.5 的例子);`uniq` 和 `quantile` 一定错,是因为"对两个估计值再估计一次"不是"对两份数据进行一次估计"。**你能靠外层聚合救回来的,只有那些"状态恰好就是结果本身"的聚合。**

**第二条路:存"状态",用 `-Merge` 结算。** 这条路对**所有**可合并聚合都成立。它不去存"平均值"这个终值,而是存 `avg` 真正的中间状态 `(和, 计数)`;不去存"2 个不同值"这个估计值,而是存 `uniq` 的采样摘要。到了读的时候,把同一 key 的**所有碎片状态合并成一份**,再算一次结果。

```sql
-- 源表(明细)
CREATE TABLE raw_events (
    ts      DateTime,
    user_id UInt64,
    amount  Decimal(18, 2)
) ENGINE = MergeTree ORDER BY ts;

-- 目标表:列的类型是「聚合状态」,不是普通数值
CREATE TABLE events_by_minute (
    minute      DateTime,
    user_id     UInt64,
    amount_sum  AggregateFunction(sum,  Decimal(18, 2)),   -- 存 (和) 这个状态
    amount_avg  AggregateFunction(avg,  Decimal(18, 2)),   -- 存 (和, 计数) 两个分量
    user_uniq   AggregateFunction(uniq, UInt64)            -- 存采样摘要
) ENGINE = AggregatingMergeTree
ORDER BY (minute, user_id);

-- 物化视图:写入时存「状态」
CREATE MATERIALIZED VIEW mv_events_minute TO events_by_minute AS
SELECT
    toStartOfMinute(ts) AS minute,
    user_id,
    sumState(amount)    AS amount_sum,   -- -State:把结果降级成状态
    avgState(amount)    AS amount_avg,
    uniqState(user_id)  AS user_uniq
FROM raw_events
GROUP BY minute, user_id;

-- 查询:把状态「结算」回结果
SELECT
    minute,
    sumMerge(amount_sum) AS total_amount,
    avgMerge(amount_avg) AS avg_amount,
    uniqMerge(user_uniq) AS users
FROM events_by_minute
GROUP BY minute
ORDER BY minute;
```

三个名字必须对上:`sumState` 返回 `AggregateFunction(sum, Decimal(18,2))` 类型的**状态**;列的类型必须写成对应状态类型;读的时候用 `sumMerge`(而不是 `sum`)把状态合并成结果。`-State` 把结果降级成状态,`-Merge` 再把状态结算回结果 —— 这两个组合器的机制在 `第 12 章 §12.7` 讲过,这里是它们的头号应用场景。

**为什么目标表要选 `AggregatingMergeTree`。** 上面那张 `events_by_minute` 里,同一个 `(minute, user_id)` 会有很多行——每一批插入产生一行,每行是一份**部分状态**。`AggregatingMergeTree` 的职责就是在后台合并 part 时,把排序键相同的多行**按状态合并**成一行(`第 18 章 §18.4`)。随着合并推进,同一分钟的碎片会逐渐收敛——但**这是优化,不是正确性前提**:哪怕合并一次都没跑过、同一分钟摊着几十行,上面那条带 `-Merge` 和 `GROUP BY` 的查询**照样给出正确答案**。请把这句话背下来:**增量聚合的正确性来自 `-Merge`,不来自后台合并**;后台合并只是让它更省、更快。至于 `SummingMergeTree`,它只在 `sum` 这类语义下够用,而且对非排序键列是"随便取其中一行"(取哪个不确定,`第 18 章 §18.4`),所以除非目标表**全部**是"相加即可"的数值列,不要用它做一般意义的增量聚合。

**`SimpleAggregateFunction` 的简化路径。** 对"状态就是结果本身"的那一小撮聚合(`sum`、`min`、`max`、`any`、`anyLast` 这些),可以省掉 `-State`/`-Merge` 这一整套,直接用 `SimpleAggregateFunction`:

```sql
CREATE TABLE daily_simple (
    d        Date,
    req_cnt  SimpleAggregateFunction(sum, UInt64),   -- 列里存的就是个 UInt64
    max_rt   SimpleAggregateFunction(max, UInt32)
) ENGINE = AggregatingMergeTree
ORDER BY d;

CREATE MATERIALIZED VIEW mv_simple TO daily_simple AS
SELECT toDate(ts) AS d, count() AS req_cnt, max(rt) AS max_rt
FROM raw GROUP BY d;                       -- 注意:没有 -State

SELECT d, sum(req_cnt), max(max_rt) FROM daily_simple GROUP BY d;   -- 也没有 -Merge
```

它和 `AggregateFunction` 的差别,一句话:普通 `AggregateFunction` 列在查询时**必须**用 `-Merge`(`sumMerge`),`SimpleAggregateFunction` 列则用**普通聚合函数**(`sum`/`max`)——因为它存的本来就是最终值。而 `avg`、`uniq`、`quantile`、`argMin`/`argMax`、`groupArray` 这些**不能**用 `SimpleAggregateFunction`,原因回到 `第 12 章 §12.1`:**它们的状态不是结果本身**。选哪条路,判据只有这一个。

## 13.5 幂等与去重

分布式的世界里重试躲不掉,而重试会给"插入触发器"型的数据管道带来一个具体的问题:重发的这一批,会不会既写进源表、又触发一次物化视图、最后在目标表里算两遍?前一半由源表的块级去重管,后一半要靠一个专门的开关。`第 08 章 §08.6` 讲过前置知识,这里只把和物化视图强相关的那几条接上。

**源表侧:块级去重。** 复制表(`ReplicatedMergeTree`)默认 `insert_deduplicate = 1`,同一个块重发时会被服务端认出来并跳过。普通 `MergeTree` 也有一套非复制去重窗口,但 `non_replicated_deduplication_window` 在 25.3 上默认是 `0`,也就是**默认关闭**。

```text
insert_deduplicate                              默认 1(但非复制表要窗口 > 0 才真有作用)
non_replicated_deduplication_window             25.3 默认 0 → 普通表默认不去重
replicated_deduplication_window                 复制表的块指纹窗口,25.3 默认 1000
replicated_deduplication_window_seconds         时间窗版本,25.3 默认 604800(1 周)
insert_deduplication_token                      自定义去重键,按分区生效
deduplicate_blocks_in_dependent_materialized_views   默认 0(关)
```

**`insert_deduplication_token`** 是你自己给的去重键。不设它的时候,服务端拿"整块数据的校验和"当指纹,于是只有重发的块**逐字节相同**才算重复;设成 `insert_deduplication_token = 'batch-2025-03-01-001'` 之类,就用**你的语义**表达"这是一个逻辑批次":只要每次重试都带上同一个 token,即使块内容因为时间戳之类的差异并不完全一致,也能被认出来。它**按分区生效**。

**`deduplicate_blocks_in_dependent_materialized_views`** 决定“去重 token 要不要传播到下游物化视图的目标表”,**默认关闭**,且依赖 `insert_deduplicate = 1`。打开后,源表这次插入被判为重复时,物化视图写进目标表的那一份也会按同一 token 去重,这才谈得上“源表 + 物化视图一起 exactly-once”。

不过“传播的判据到底是什么”各方说法不一致,拆开看有三点:

- 官方口径:机制是“把源块的 `insert_deduplication_token` 往下游传”;旧版另有一条已废弃的 `update_insert_deduplication_token_in_dependent_materialized_views`,用来在每一层把 token 与目标表标识再组合一次,免得不同来源的块撞上同一个 token。
- 社区实测到的副作用:表现为“两次不同的源表插入只要产出逐字节相同的输出块,第二份就被丢弃”(见下一段)。
- 两条并不完全吻合,所以**“判据是输出块指纹还是继承 token”、以及链式视图上会传到第几层,我持保留态度**,请以你目标版本的实际行为为准。

这里必须讲清**那个已知副作用**,它是很多人实打实踩过的坑:

**错误做法**:为了"源表和物化视图一起 exactly-once",打开 `deduplicate_blocks_in_dependent_materialized_views`,同时物化视图是**聚合型**的(输出行数远少于输入行数)。

**后果**:只要两次**不同的**源表插入产出了**逐字节相同**的物化视图输出块,第二份就会被判为重复而**丢掉**。举一个真实会发生的形状:物化视图是 `SELECT toStartOfMinute(ts) AS m, count() AS n FROM src GROUP BY m`,两个不同的批次恰好落在同一分钟、且各有 100 行,于是两次输出都是 `(m, 100)` 这一行 —— 字节完全相同,第二次被去重丢弃。**结果是计数少了一半,而且不报任何错。**

**正确做法**:分情况处理。

- 聚合型视图需要精确计数:不要打开这个传播开关,改用别的方式兜重复——源表侧用 `ReplacingMergeTree` + `FINAL`/`argMax` 收敛,或接受“重试窗口内由 `insert_deduplication_token` 兜住”。
- 确实需要传播:让输出块带上一个**区分性的字段**。但这会削弱去重本身的意义,是一个真实的取舍,没有免费的解。

还要提醒一点:这个副作用要成立,前提是块级去重**真的在生效**。普通 `MergeTree` 的窗口默认是 0,可能测试环境怎么都复现不出来,一搬到生产(复制表)就中招。这个“环境相关”的推断我**未在目标版本上实测**,标为未确认;但方向是明确的——**在“复制表 + 聚合型物化视图 + 打开传播开关”这个组合上,格外小心。**

## 13.6 物化视图链:MV → MV → 目标表

因为"写 `dest` 也是一次 INSERT",而 `dest` 上同样可以挂物化视图,链式结构天然成立:往源表插一次,会沿着链条一路往下传播。

```text
INSERT INTO raw
      │
      ▼
   [ raw ] ──trigger──► mv_minute ──写──► [ by_minute ]      (AggregatingMergeTree,存状态)
                                                  │
                                        trigger──►mv_hour ──写──► [ by_hour ]
                                                                  (AggregatingMergeTree,存状态)
```

这就是**分层 rollup** 的标准做法:分钟层先聚一遍,小时层再从分钟层聚上去,天层再从小时层聚上去。它的价值在于**每层只读下一层**,而不是每层都去重扫全量明细;层数一多,省下来的扫描量是数量级的。

要让链能正确工作,有一条硬性要求:**中间层必须用 `AggregatingMergeTree`(或等价的、能存状态的引擎),并且中间层存的是状态。** 理由还是 §13.4 那一条:上一层写进中间层的是"碎片状态",下一层要做的是"把这些碎片状态再合并、聚到更粗的粒度",所以它需要的输入是**状态**,不是终值。

这里有一个最常见的写法错误:**第二层的 SELECT 要用 `-MergeState`,不是 `-Merge`。** 两者都吃状态,差别全在出口 —— `-Merge` 把状态**结算成终值**(`sumMerge` 出来是 `Decimal`,`uniqMerge` 出来是 `UInt64`),而小时层的目标列类型是 `AggregateFunction(...)`;把终值塞进状态列,会命中 `Cannot convert UInt64 to AggregateFunction(uniq, UInt64)` 这类报错。真正需要的是 `-MergeState`:它**把多份子状态合并、但仍然返回状态**(`第 12 章 §12.7` 讲过这一对变体)。

```sql
-- 小时层:从分钟层的「状态」再往上聚
CREATE MATERIALIZED VIEW mv_hour TO by_hour AS
SELECT
    toStartOfHour(minute) AS hour,
    user_id,
    sumMergeState(amount_sum) AS amount_sum,   -- 合并子状态,出口仍是状态
    uniqMergeState(user_uniq) AS user_uniq
FROM by_minute
GROUP BY hour, user_id;
```

`-State`/`-Merge`/`-MergeState` 三者各自的机制见 `第 12 章 §12.7`,这里只说**沿 MV 链怎么套用**:链上**每一级**都用 `-MergeState`(合并子状态、出口仍是状态),只有**最外层**那次面向用户的查询才用 `-Merge` 收口。附录 F 的 `§F.3.5` 是一个同构的两级例子,可以对照着看。

这里还有一个容易被忽略的前提:`by_minute` 上**未被合并的碎片行**会在第二层被读到。也就是说第二层要能正确处理"同一 `(minute, user_id)` 有好几行部分状态"这种输入 —— 而这正是 `-MergeState` 配 `GROUP BY` 能处理的(`第 12 章 §12.10` 的结论)。所以第二层的 SELECT 里**必须**带 `-MergeState` 和 `GROUP BY`,不能想当然地以为"上一层已经聚合过了,这一层直接取就行"。

链式结构要注意的地方:

- **执行顺序是"沿插入传播",每一层的 SELECT 都在同一条 INSERT 路径上同步跑完。** 所以链条越长,源表那一次 INSERT 就越慢,链上任何一层报错都会让源表插入失败(§13.1 第五点)。另外,挂在**同一张**中间层表上的多条视图之间,默认也是**串行**处理的,由会话设置 `parallel_view_processing` 决定(§13.1)。
- **层数不要贪多。** 每加一层,排查成本、错误传播面、以及"某层的一段数据缺失导致下游全歪"的风险都在叠加。两到三层(明细 → 分钟/小时 → 天)通常够用;真需要更多层,先想想是不是该用可刷新视图或调度式预计算(§13.9)。
- **中间层的引擎选择只有 `AggregatingMergeTree` 是安全的。** 用 `SummingMergeTree` 只在纯 sum 场景成立(§13.4);用普通 `MergeTree` 则第二层会读到一堆没合并的碎片行,而且没有任何引擎帮你收敛。
- `deduplicate_blocks_in_dependent_materialized_views`(§13.5)在链上怎么传播到第二层、第三层,**判据本身就有歧义**(见 §13.5),不要在链式场景里依赖它。

## 13.7 回填历史数据:三种做法与各自的代价

"物化视图只对触发之后的行负责"(§13.1 第一点)意味着:任何实际落地都会撞上"历史数据怎么办"。有三条路。

**做法一:`POPULATE` 一次性回填。** 只在**不带 `TO`** 的形态里可用(§13.3),建视图时顺手把历史数据算一遍。它的代价很清楚:带竞态(回填期间的写入可能漏、可能重),而且回填是**一次性的、不可重来**(再建视图也不会为你补;`POPULATE` 是建视图时的一个开关,不是一条能重跑的命令)。适合"源表此刻基本静默、数据量不大"的场景。

**做法二(推荐):先建视图,再手动 `INSERT ... SELECT` 回填。** 这是 `TO` 形态下唯一正确的做法,因为 `TO` 形态根本没有内部表,`POPULATE` 用不了。顺序很关键:

```text
时刻 T0:  建好 dest 表,并建好带 TO 的物化视图  ← 此刻起,新写入会被自动捕获
时刻 T0+: INSERT INTO dest SELECT ... FROM src  WHERE <只覆盖 T0 之前的数据>
```

要点有三个:

- **视图必须先建。** 先回填再建视图,会漏掉"回填结束到建视图之间"写进来的数据;而且这段时间如果不长,漏掉的量还小到不容易被发现,属于最阴的一种错。
- **手动回填的 `WHERE` 必须把"视图已经开始捕获的那一段"排除掉**,否则同一批数据既被回填又来一次触发,会被算两遍。对于按时间分区的表,这个 `WHERE` 就是一条时间边界;对于没有自然时间边界的表,**必须选一个停写窗口来做**。
- **回填要分批。** 一次 `INSERT INTO dest SELECT ... FROM src` 写得太多,会在 `dest` 上瞬间产生一大批 part,触发 `第 18 章 §18.6` 的"too many parts"闸门。按时间或按 key 范围切成多次插入,给后台合并留出消化的时间。

**做法三:用可刷新视图。** 因为可刷新视图(§13.8)每次刷新都**重跑整条 SELECT**、并把结果覆盖进目标表,所以它天然"自带历史"——历史数据会被下一次刷新重新算进去,不用你手动回填。代价是每次都要全量扫一遍源表,适合"数据量可控、延迟可容忍"的场景。**如果你的核心诉求就是"历史也要算进去",而且扛得住全量重扫,做法三往往比做法二省心得多。**

一句话选择:**能接受全量重扫就用做法三;要走增量、就必须用做法二,而且把"先建视图、再带边界回填、分批写"这三件事都做对。**

## 13.8 可刷新物化视图:`REFRESH EVERY`

形态 B(§13.2)的完整能力。它是一条**定时任务**:按周期把整条 SELECT 重跑,把结果写进目标表。

```sql
-- 替换语义(默认):每次刷新重算整条 SELECT,结果覆盖目标表
CREATE MATERIALIZED VIEW mv_stats
REFRESH EVERY 10 MINUTE TO stats
AS SELECT toDate(ts) AS d, count() AS n FROM raw GROUP BY d;

-- 追加语义:每次刷新把新结果追加到目标表尾部
CREATE MATERIALIZED VIEW snap
REFRESH EVERY 10 MINUTE APPEND TO stats_snap
AS SELECT now() AS captured_at, toDate(ts) AS d, count() AS n FROM raw GROUP BY d;
```

**调度。** 周期写在 `REFRESH EVERY <interval>` 里,`<interval>` 是 `10 MINUTE`、`1 HOUR`、`1 DAY` 这样的写法。两个修饰:

- `OFFSET <interval>`:把刷新时刻错开整点,比如 `REFRESH EVERY 1 HOUR OFFSET 15 MINUTE` 让它在每小时的第 15 分钟刷。
- `RANDOMIZE FOR <interval>`:给刷新时刻加随机抖动,避免几十个视图在同一秒一起开跑、把资源打满。

改周期用 `ALTER TABLE mv MODIFY REFRESH ...`(不是 `DROP` 重建)。手工触发用 `SYSTEM REFRESH VIEW mv`;暂停/恢复用 `SYSTEM STOP VIEW mv` / `SYSTEM START VIEW mv`。

**`EVERY` 还是 `AFTER`:两套周期语义,别混。** 上面讲的是 `REFRESH EVERY <interval>`,它是**对齐钟点**的周期——按"从某个基准时刻起、整数个周期"触发,配 `OFFSET` 可以把触发点错到每小时的第 15 分钟。与之并列的还有 **`REFRESH AFTER <interval>`**:它是"**上一次刷新跑完之后**再等这么久",**不与钟点对齐**,刷新本身耗时长短会累积进周期。所以判据很干脆:**要对齐整点、方便和别的定时任务对表,用 `EVERY`;只想"跑完歇一会儿、别太频繁",用 `AFTER`**——后者在刷新单次开销大、时间有波动时更稳。

**`EMPTY`:抑制建表那一次初始刷新。** 默认情况下可刷新视图**建好的瞬间就会先跑一次初始刷新**(把源表当前状态算进目标表)。加一句 **`EMPTY`** 就能跳过这一次,让视图先以**空的目标表**存在、等第一个周期才真正开始算——用在"不想在建表那一刻就对着整张源表全量跑一遍"的场合(源表此刻正忙,或你想手动挑个低峰时刻跑第一次)。

**刷新任务自己的 `SETTINGS`。** `REFRESH` 子句那一侧还可以挂一个 `SETTINGS` 槽位,用来给**这条刷新任务**单独设执行参数(内存上限、超时之类),它和建表语句里作用于查询的设置是分开的两回事——对一条特别重的刷新做限流、而不影响别处,靠的就是它。**这几项(以及 `SETTINGS` 到底能放哪些键、`EMPTY` 与 `AFTER` 在链式依赖下的确切行为)在不同版本上有出入,请以你目标版本的 `SHOW CREATE TABLE` 与官方文档为准,拿不准的按未确认处理。**

**`APPEND` 与覆盖的区别,是两种语义而不是两种写法。** 不带 `APPEND` 时(默认)是**替换**:每次刷新重算整条 SELECT,把结果**原子地**换成新的。带 `APPEND` 时是**追加**:每次刷新把这次算出来的一批行**接在目标表尾部**,目标表于是变成一串"定期快照"的时间序列(所以上面那个例子顺手加了一列 `captured_at`,否则你不知道哪一行是哪一次快照的)。

这条差别决定了**一致性**:替换语义下,目标表在任何一个时刻都是"某一个完整快照",查询**看不到半成品**——刷新是把新快照算好、再换上去,中间态不外露。`APPEND` 语义则不是:刷新过程中,目标表会**逐步长出**新快照的行,查询可能读到一个"快照只写了一半"的中间态。所以"刷新期间能不能查、查到什么"这个问题,答案取决于你用的是哪种语义:**要一致性就默认(替换),要留历史快照才用 `APPEND`,并且接受读到中间态。**

**`DEPENDS ON` 与刷新依赖。** 一个可刷新视图可以依赖另一个:

```sql
CREATE MATERIALIZED VIEW mv_hourly
REFRESH EVERY 1 HOUR TO hourly
DEPENDS ON mv_minutely
AS SELECT ... FROM ...;
```

`DEPENDS ON` **必须显式写出依赖**——不要指望引擎从 `TO`/`FROM` 的引用关系里替你推断出刷新顺序。名字写错(比如拼错一个视图名)的后果是**灾难性的安静**:这个视图会永远停在"等待依赖"的状态、**永远不刷新**,而不会有任何明显的报错跳出来。排查这类问题,第一站就是 §13.11 的 `system.view_refreshes`。

**副本之间怎么分工。** 集群里刷新任务在多个副本上的分派由刷新设置里的 `all_replicas` 控制。**它的默认值是 `false`**(即协调开启):在 Replicated 数据库里,同一时刻**只有一个副本**真正执行这次刷新,协调靠 Keeper(znode 路径由 `default_replica_path` 决定);把 `all_replicas = 1` 打开,才会让每个副本各刷各的(主要在 `APPEND` 形态下这么用)。要注意它**建完之后不能再改**。查某次刷新落在哪个副本上,看 `system.view_refreshes` 的实况。

**最后一句状态提醒:** 可刷新视图如今**直接建就能用,不需要任何 `allow_experimental_*` 开关**;那个老的 `allow_experimental_refreshable_materialized_view` **自 24.10 起已被标记为废弃**(`MAKE_OBSOLETE`——24.9 时它还是一个正常的、默认 `false` 的设置,只是那一版新增了 `APPEND`),这与 `第 32 章 §32.2` 的能力速查表口径一致。但有一个容易踩的错位要提醒:**25.3 的官方文档上,可刷新物化视图仍挂着 `experimental` 标记**,尽管代码里已经不再用开关门控它——"文档还写实验、代码已放行"是真实存在的一段错位。**它究竟从哪个版本起算"正式转正",各方资料口径不一,本项目按 `未确认` 处理**:请一律以你目标版本的 `system.settings` 现场核对;凡是本书提到"实验特性"的地方,都请照此办理。

## 13.9 物化视图 vs 投影 vs 普通视图 vs 预计算表

四样东西经常被混着问"该用哪个",它们的差别其实很干净:**各自"对谁负责""由谁触发""结果放在哪"。**

**普通视图(`CREATE VIEW`)** 是个**宏**:不存数据,查询时把子查询展开重跑。它解决的问题是"复用/简化查询逻辑",**完全不解决性能**——你查视图和手写那条子查询的开销一模一样。别指望它加速。DDL 形态见 `第 06 章 §06.18`。

**投影(projection)** 是**主表内部**的另一份数据,通常带不同的排序键或预聚合。它的独门性质是**对用户完全透明**:你照常查主表,优化器自己判断"这个投影的排序/聚合正好适合",然后去读投影。代价是它成倍增加存储、每次 INSERT 都要多维护一份(写放大),而且有两道硬限制:**投影只服务单表查询、不能跨表 `JOIN`**,并且**带 `FINAL` 的查询不会去用投影**。原理与开关见 `第 11 章 §11.12`。

**物化视图** 是**显式命名的表**,你要**显式查它**(或者靠查询改写命中)。它能做的事比投影宽:可以跨表、可以 `JOIN`、可以做多层 rollup、可以存聚合状态。代价是它挂在源表的 INSERT 路径上,写入放大、错误传导(§13.1 第五点),而且"只对新数据负责"这条特性逼着你在落地时处理回填(§13.7)。

**手搓预计算表**(自己写一条 `INSERT INTO agg SELECT ... FROM raw`,配一个调度器定时跑)是**完全掌控**的一档:没有写入放大(源表写入不受影响)、可以任意重算、可以补历史、可以跨表——代价是你要自己解决"调度、幂等、以及"重算窗口怎么切"这三件事,而且**延迟由调度周期决定**,不是实时的。

决策路径可以顺着下面这几个问题走:

```text
问题 1:你要省的是「读聚合」还是「读另一种排序 / 过滤」?
   ├─ 另一种排序 / 过滤,而且查询只碰这一张表、不带 FINAL
   │     → 投影(透明、无需改查询)
   └─ 读聚合(按某维度预汇总)
         往下走

问题 2:预汇总需要跨表 / JOIN / 分层 rollup 吗?
   ├─ 需要            → 物化视图
   └─ 不需要,只是单表换种排序 → 先回去看投影

问题 3:数据是「实时进来、要求跟着走」,还是「按批定期算」?
   ├─ 实时、跟着写入走        → 物化视图(插入触发器)
   ├─ 定期全量重算、能容忍延迟 → 可刷新物化视图(§13.8)
   └─ 要完全掌控、能重算、能补历史 → 手搓预计算表 + 调度

问题 4:历史数据也要算进去吗?
   └─ 要 → 手搓回填(§13.7),或直接选可刷新视图
```

有两条**反直觉**的提醒值得单独说。第一,**别拿物化视图去"修正已经写错的历史数据"**——它只对新数据负责,你 `ALTER` 那条 SELECT 并不会让老数据重算。要重算就得回填(§13.7)。第二,**投影和物化视图不是"谁替代谁",而是"透明度换能力"**:投影透明但只服务单表、不带 `FINAL`;物化视图要显式查但几乎什么都能做。选错了的典型症状是"我加了投影,可这条带 `FINAL` 的查询还是慢"(投影不生效)或者"我加了物化视图,可查询根本没变快"(没人去查它)。

## 13.10 常见坑清单

**坑一:建了物化视图,以为历史数据会自动进来。**
**错误做法**:直接在已有数据的源表上建形态 A 的视图,不写 `POPULATE`、也不回填。
**后果**:目标表里只有"建视图之后"写入的那部分,数字**静默地**少一大截,而且少得不易察觉(建视图之后写入量越大,缺的那部分占比越小,越难发现)。
**正确做法**:要么按 §13.7 做法二(先建视图、再带时间边界手动 `INSERT ... SELECT`、分批写)回填,要么改用可刷新视图(§13.8)让它全量重算。

**坑二:同时写 `TO` 和 `POPULATE`。**
**错误做法**:`CREATE MATERIALIZED VIEW mv TO dest AS SELECT ... FROM src POPULATE`。
**后果**:建表直接报错(大意是两者不能同时声明),你得返工。
**正确做法**:认清两者互斥的根源——`POPULATE` 靠内部表回填,`TO` 形态没有内部表(§13.3)。要 `TO` 就手动回填,要 `POPULATE` 就别写 `TO`。

**坑三:把物化视图当"行对行"的 ETL,却没评估写放大。**
**错误做法**:写一条不带 `GROUP BY` 的 `SELECT ... FROM src`(也许带个 `WHERE` 做过滤),以为"插 1 行就出 1 行、很轻"。
**后果**:目标表行数与源表持平甚至更多,而每次源表插入都要在**同步路径**上再写一遍目标表,写放大直接翻倍;源表那种小批量高频的写入方式(`第 08 章 §08.4`)会被原样放大到目标表上,目标表很快堆出大量 part。
**正确做法**:先想清楚这条视图的**粒度**——能聚合的就把 `GROUP BY` 明确写出来,把行数压下去;纯过滤转发(MV 的 SELECT 里是可以写 `WHERE` 的)是合法用法,但要评估目标表的 part 增长与合并压力(`第 18 章 §18.2`)。

**坑四:目标表用普通 `MergeTree` 存增量聚合结果。**
**错误做法**:建 `events_by_minute` 时 `ENGINE = MergeTree`,然后指望"查询时它自己会对齐"。
**后果**:同一 `(minute, user_id)` 长期摊着几十上百行碎片,每次查询都要自己 `GROUP BY` 兜底;忘了带 `GROUP BY` 就直接看到重复的"部分答案"。
**正确做法**:增量聚合的目标表用 `AggregatingMergeTree`(存状态)或 `SummingMergeTree`(纯 sum 语义),让后台合并替你收敛(`第 18 章 §18.4`)。即便如此,查询侧仍要写 `-Merge` + `GROUP BY`,因为合并是异步的(§13.4)。

**坑五:该存状态的地方存了结果。**
**错误做法**:物化视图里写 `avg(amount)`、`uniq(user_id)`、`quantile(0.5)(rt)`,把终值存进目标表。
**后果**:外层再怎么聚合都还原不出正确答案——`avg` 平均出 52.5 而不是 60(第 12 章 §12.1 那组数字),`uniq` 把两个估计值又估计一次。
**正确做法**:凡是"状态不是结果本身"的聚合,一律用 `-State` 写入、`-Merge` 读出(`sumState`/`avgState`/`uniqState` + `sumMerge`/`avgMerge`/`uniqMerge`),列类型写成对应的 `AggregateFunction(...)`。只有 `sum`/`min`/`max`/`any` 这类才可以用 `SimpleAggregateFunction` 走简化路径。

**坑六:`AggregateFunction` 列上用了普通聚合函数。**
**错误做法**:列是 `AggregateFunction(uniq, UInt64)`,查询却写 `uniq(user_uniq)` 或 `sum(amount_sum)`。
**后果**:报类型错误,或者(在某些写法下)得到毫无意义的结果——因为 `uniq` 期待的是 `UInt64` 明细值,不是一份状态。
**正确做法**:记住这条配对规则——`AggregateFunction` 列配 `-Merge`(`uniqMerge`/`sumMerge`),`SimpleAggregateFunction` 列配普通聚合(`uniq` 不行、`sum`/`max` 可以)。写错方向就一定是错的。

**坑七:打开 `deduplicate_blocks_in_dependent_materialized_views` 之后聚合型视图丢行。**
**错误做法**:为了"源表和物化视图一起 exactly-once"就打开这个开关,而视图是聚合型的(输出行远少于输入行)。
**后果**:两次**不同的**源表插入只要产出逐字节相同的输出块(比如同一分钟各 100 行),第二份就被判为重复而**丢弃**,计数少一半且不报错(§13.5)。
**正确做法**:精确计数场景不要开这个传播开关;确实要传播,就得接受"输出要能区分"的取舍,并且在复制表上重点回归(普通表因为窗口默认 0,测试环境可能复现不出来)。

**坑八:`DROP` 掉了 `TO` 目标表,却留着物化视图。**
**错误做法**:`DROP TABLE dest` 之后忘了视图还在。
**后果**:物化视图下一次触发时写不进目标表,于是**源表的 INSERT 直接失败**——一个已经不该存在的下游,把上游写挂了(§13.1 第五点)。确切的报错形态我未确认,但"写不进就连带源表插入失败"这个方向是确定的。
**正确做法**:`dest` 如果要重建,先 `DETACH TABLE mv` 把触发器摘下来,重建完 `dest` 再 `ATTACH`;或者干脆先 `DROP TABLE mv`。反过来也要知道:`DROP TABLE mv` **不会**删掉 `dest`(它是一张独立的表)。

## 13.11 监控与运维

**物化视图在 `system.tables` 里长什么样。** 它是一条普通的表记录,`engine` 列显示为 `MaterializedView`。如果你用的是不带 `TO` 的形态,还会多出一张内部表:名字形如 `.inner.<视图名>` 或 `.inner_id.<uuid>`,`engine` 显示为视图 DDL 里那个引擎(`AggregatingMergeTree` 之类)。带 `TO` 的形态没有内部表,目标表就是一张你早就建好的普通表。**依赖关系有个容易记反的地方(以下未确认)。** `system.tables` 里确实有 `dependencies_database` / `dependencies_table` 两列,有说法称它们给的是**反方向**的信息——当前这张表**被哪些视图依赖**(即下游依赖方),而**不是**“这条物化视图指向哪个 `TO` 目标”。**这两列的语义我没有在 25.3 上核实,标为未确认**;拿不准时,想知道一条 MV 的 `TO` 目标,直接用确定的两条路:`SHOW CREATE TABLE <mv>` 或 `system.tables.create_table_query`。`system.tables` 的整体用法见 `第 27 章 §27.3`。

```sql
-- 看这台实例上有哪些物化视图、各自指向哪儿
SELECT database, name, engine, create_table_query
FROM system.tables
WHERE engine = 'MaterializedView';

-- 看某张表的完整 DDL(含 TO 目标、内部表名)
SHOW CREATE TABLE mv;
```

**`DROP` 与 `ALTER`。** `DROP TABLE mv` 删掉视图;带 `TO` 的形态下目标表**保留**,不带 `TO` 的形态下内部表**随视图一起删**。`ALTER` 方面要留个心眼:物化视图的 SELECT 是在建视图时固化下来的(§13.1),所以"改这条 SELECT"往往**不是**一条 `ALTER` 能解决的事——**查询体是可以 `ALTER` 的**——用 `ALTER TABLE ... MODIFY QUERY` 换掉那条 SELECT,而且不会打断写入;但它**只改 SELECT、不改视图的列定义**:带 `TO` 的形态支持得较好(改 SELECT 时要让输出对得上目标表的列),**不带 `TO` 的形态很受限、不能借它新增列**。所以改结构(加/减输出列)时,**稳妥姿势仍是 `DROP` + 重建**(重建时记得按 §13.7 处理数据衔接)。

**物化视图阻塞写入怎么办。** 最常见的形态是:目标表(或链上某一层)的 part 攒得太多,触发 `第 18 章 §18.6` 的闸门,于是**源表的 INSERT 开始报错或变慢**。应急手段是先解耦——把触发器摘下来,让源表写入恢复:

```sql
DETACH TABLE mv;      -- 触发器摘下,源表写入不再经过它
-- …… 处理目标表的 part 问题:等合并、或 OPTIMIZE、或补写……
ATTACH TABLE mv;      -- 重新挂上
```

治本要去看目标表是不是"写得多、合得少":根因通常在**源表写入太碎**(小批量高频插入,`第 08 章 §08.4`)被放大到了目标表上,或者目标表的排序键/分区键选得让合并难以收敛(`第 18 章 §18.2`)。写入侧的限流闸门是 `parts_to_delay_insert` / `parts_to_throw_insert`(`第 25 章 §25.8`);`SYSTEM STOP MERGES` / `SYSTEM START MERGES`(`第 18 章 §18.15`)只在排查时短暂用,别当常规手段。

**可刷新视图的观测。** 它的运行状态在自己的系统表里:

```sql
SELECT database, view, status, last_refresh_time, next_refresh_time,
       last_success_time, last_refresh_replica, exception
FROM system.view_refreshes;
```

排错顺序很固定:先看 `status`——停在 `MissingDependencies` 一类"依赖未满足"的状态,回去查 `DEPENDS ON` 有没有写错名字(§13.8);是 `Disabled` 就看是不是被 `SYSTEM STOP VIEW` 停了;`status` 正常但 `next_refresh_time` 一直不动,再去看 `last_refresh_replica` 与各副本。`last_success_time` 是"上一次真正成功"的时间戳、`exception` 是上一次失败的原因——这两个一起看,**"最后一次成功是什么时候"比"最后一次尝试是什么时候"有用得多**。`system.view_refreshes` 的存在与列名在 25.3 上我**未逐一核对**,以 `DESCRIBE` 的结果为准。

**一句运维原则:** 物化视图把"读的开销"换成了"写的开销",所以**它的健康度要挂在写入侧的指标上**——`system.part_log` 里目标表的 `NewPart` 频率、系统表里目标表的 part 数、以及源表 INSERT 的延迟。目标表 part 数长期不下降,是物化视图要出事的第一信号。

## 13.12 与 mini-clickhouse 的对照

`mini-clickhouse/minich/storages/view.py` 把物化视图的**语义骨架**实现了一遍,更重要的是 `minich/execution/interpreter.py` 把最关键的那根**接线**也接上了 —— 在这份实现里,"插入触发器"是真的会触发的。这正是本章最想让你看清的一件事:**物化视图 = 存储层接口 + 解释器接线,少了任何一件,触发器都不会响。**

**触发器这条链路,在 mini 里是通的三段:**

- **建视图。** `Interpreter._create_view`(约 1664 行)按 `params` 字典构造 `StorageView` / `StorageMaterializedView`,并把 `subquery_executor` **两个分支都注入** —— 普通视图的 `read` 与物化视图的 `populate` 都靠它取数。带 `TO` 时还会把目标表解析出来交给 `set_target`。
- **登记源表。** `_mv_source`(约 1720 行)从视图的 `SELECT ... FROM src` 里取出那张源表存进引擎(`set_source`),之后靠 `matches_source(table)` 判断"这张表上挂了我没有"。
- **触发。** `InterpreterInsertQuery` 在 `table.write(...)` **之后**紧接着调 `_trigger_materialized_views(table, block)`(约 1355–1360 行)。它遍历 catalog 里所有 `StorageMaterializedView`,命中源表就把**刚插入的那个 block** 喂给视图的 SELECT,再把结果 `engine.push(ctx, out)` 写进 `TO` 目标表。空 block 直接跳过。

**这里有一条值得单独看的细节:触发器挂在解释器的 `INSERT` 上,不是存储引擎的 `write` 上。** 所以由引擎自己发起的写(`Buffer` 到点刷盘之类)不会触发物化视图 —— 不是偷懒,而是那些写路径本来就没有一个"刚插入的 block"可以喂进去,与 §13.1 那条"触发器靠的是写入动作"完全自洽。

`POPULATE` 的幂等由 `_populated` 标志保证:`populate()` 只在第一次真正跑,重复调用直接返回 —— 正是真实 ClickHouse "`POPULATE` 是建视图时的开关、不会重复触发"的语义。

`AggregatingMergeTree`、`AggregateFunction(...)` 列类型、`ColumnAggregateFunction`、`-State`/`-Merge` 组合器、以及 `SimpleAggregateFunction` 类型在这份实现里都是真的,所以 §13.4 那条链路在**组件层**是齐的。

**能力边界必须说清楚,否则你会在错的地方期望它:**

- **只做一轮传播。** MV 的输出是直接 `push` 进目标引擎的,**不**再经过 INSERT 解释器。所以 `MV1 → 表 T → MV2(T 上的 MV)` 这种链式结构,在 mini 里**只有第一环会动**。这是它的**简化**,而 `§13.6` 讲的那套级联语义(真实 ClickHouse 用 `-MergeState` 一层层把状态合并上去)在这份实现上只能验证到第一级,再往上得靠 `POPULATE` 或手动回填。
- **只认 `FROM` 位置的单张源表。** `_mv_source` 在 `FROM` 是 `JOIN` 或子查询时返回 `(None, None)`,那条 MV **永远不会响应 INSERT**,只能靠 `POPULATE` 回填。这是写进代码的能力边界,不是静默失效。
- **`REFRESH` 整套不支持。** lexer/parser 里没有 `refresh` 关键字,`system.view_refreshes` 不存在,`SYSTEM REFRESH VIEW` / `SYSTEM STOP VIEW` 不存在。`§13.8` 讲的形态 B 在这份实现里是**完全空白**,不是"简化实现"。
- **不带 `TO` 时的内部表只是内存里的一个 `list[Block]`**(`_owned`):不落盘、不参与合并、不出现在 `system.parts` 里,也没有 `.inner.*` 那种命名与"`DROP` 时清理内部表"的后台逻辑。`has_inner_table()` 的判据就是"没有 `TO` 就算有内部表"。
- **列对齐是宽松的。** `push` 调 `_align_block(..., strict=False)`,列对不上时会容忍而不是报错;真实 ClickHouse 在 `TO` 目标列不匹配时是要报错的。这是语义偏差,别当成"更宽容的真实现"。
- **依赖关系没有建模**:没有 `DEPENDS ON`,也没有链式视图的执行顺序保证(与"只做一轮传播"同源)。
- **落盘的 `AggregateFunction` 状态列序列化目前是坏的**:`uniqState` 写进落盘的 `AggregatingMergeTree` 时报 `HyperLogLog` 对象转换失败、`sumMerge` 读回时报序列化被截断、`SimpleAggregateFunction(sum, UInt64)` 的列直接送进 `sum()` 会被当成非数值类型 —— 都是 `tests/test_execution_aggregates.py` 里 `xfail` 标注的已知项。**后果很实际**:`AggregatingMergeTree` + `-State` 这条链路只能在内存引擎上跑通。所以想在 mini 上复现 §13.4 的例子,目标表要用 `Memory` 引擎;这不是本章的写法有问题,而是这份教具的一处待修缺陷。

一句话总结这次对照:**mini-clickhouse 不只有物化视图的"语义词汇表"(`push` / `populate` / 内部表 / `TO` / `POPULATE`),而且真的把它们接成了一条会触发的链路** —— 建得出、插得进、`POPULATE` 幂等,源表登记与一轮传播都实现了。它缺的是**可刷新形态**、**链式传播**和**状态列的落盘序列化**这三块:前两块是刻意的简化,第三块是待修的缺陷。把这三块分开说,比笼统地讲"实现了物化视图"诚实得多。

## 13.13 边界场景:源表是 `Distributed`/`Buffer`,以及触发失败怎么查

前面默认源表是一张普通的本地 `MergeTree`。换成 `Distributed` 或 `Buffer` 这类"不是真正落盘点"的表,触发行为会变,值得单独说清。

**挂在 `Distributed` 上。** `Distributed` 表本身不存数据,它把 `INSERT` 转发给各分片的本地表(第 23 章 §23.6)。由此有两条实践准则:

- **把物化视图建在每张本地表上,而不是只建在那张 `Distributed` 表上。** 本书反复建议"写入直接写本地表,`Distributed` 表只用来读"(第 23 章 §23.6、第 14 章 §14.9)。既然数据是写进本地表的,触发器就必须挂在本地表上;只挂在 `Distributed` 表上的视图,在这些(推荐的)写入路径下**根本不会触发**。反过来说,如果你确实从 `Distributed` 表写入,那触发器挂在哪儿就必须与写入入口对齐。
- **每张分片各自维护一份视图。** 本地表是分片的,视图也要在每条分片上建(可以用集群 DDL 统一建),否则只有部分分片的预聚合表会被填上。

至于把物化视图**写向**一张 `Distributed` 目标表:语法上成立,但等于在写入路径上再加一跳网络转发和一层异步队列,写放大与延迟都会叠上去;通常不如"各分片写各自的本地目标表、读取时再经 `Distributed` 汇聚"。

**挂在 `Buffer` 上。** `Buffer` 引擎把行先攒在内存里、到点或攒满才批量刷进目标表(第 08 章 §08.8)。这意味着"插入 `Buffer`"与"数据落进实质目标表"是**两个时刻**——触发器要挂在"真正接收这次 `INSERT` 的那张表"上才符合直觉。`Buffer` 本身已属较边缘的引擎,把它放进"实时增量聚合"的主链路通常弊大于利。**(`Buffer` 与物化视图配合的确切行为,我没有在 25.3 上逐项核实,标为未确认;稳妥做法是别让物化视图的触发路径穿过 `Buffer`。)**

**触发失败怎么查:看 `system.query_log`。** 物化视图的 SELECT 是**作为源表那次 `INSERT` 的一部分**被执行的(§13.1 第五点),所以它会在 `system.query_log` 里留下**自己的**一条记录——它属于同一条初始查询,可以顺着 `initial_query_id` 回到触发它的那条 `INSERT`,出错的 SQL 与异常则落在 `exception` / `exception_code` 一类列里。排查时两条线索都从这儿来:

- **“插入没报错,但目标表没数据”**:去 `query_log` 里找那次 `INSERT` 对应的 MV 查询,确认它到底跑了没有、写出几行。
- **“插入直接失败”**:错误往往先冒在 `INSERT` 上,根因却在 MV 的 SELECT 或目标表的写入;在同一条 `initial_query_id` 下能找到那张出问题的视图查询,而不是被 `INSERT` 自己的报错牵着走。

`system.processes` 能看到正在跑的 MV 查询(同样挂在初始查询的 id 链上),`system.part_log` 能看出目标表有没有产出 `NewPart`——两者与 §13.11 的指标配合,足以判断"触发器响没响、卡在哪儿"。**这几个系统表的确切列名与 `query_kind` 取值我没有在 25.3 上逐一核对,以 `DESCRIBE` 的实际结果为准。**

**挂在"被服务端攒批"的写入上:`async_insert` 会改掉触发粒度。** 前面默认一次客户端 `INSERT` 触发一次视图。打开 `async_insert`(第 08 章与第 28 章 §28.17 都把它当小批量高频写入的标准解法)之后,客户端那次 `INSERT` 只是把数据交进服务端的内存缓冲,真正的落盘发生在**服务端把多个客户端 `INSERT` 攒成一批、合成一个 block 之后**(第 18 章 §18.6)。为什么还是只触发一次?**因为触发器挂在"执行一条 `INSERT` 查询"这件事上,而刷盘那一步本身就是一条内部的 `INSERT` 查询。** 这一点要和 §13.12 那条区分开:触发器**不**挂在存储引擎的 `write` 上,所以引擎自己发起的写(比如 `Buffer` 到点刷盘)不会触发视图;而 `async_insert` 攒批之后是用一条正规的内部 `INSERT` 把合成好的 block 写进去的,它走的是同一条解释器路径,于是视图照常触发 —— **区别只是触发次数变成了"每个刷出去的 block 一次",不再是"客户端每次 `INSERT` 一次"**。

这条机制推出一串很实际的后果:

- **目标表里的碎片会变少、变规整。** 客户端发 100 次单行 `INSERT`,视图不再产出 100 份输出块,而是按服务端的攒批产出少数几份——这正是把小批量高频写入带来的写放大往下压的办法(§13.11 那条"目标表 part 涨太快"的根因之一)。
- **但"一批"的定义被你改掉了,视图的语义会跟着漂。** 任何"一批一批"的假设都会变样:如果视图依赖"一批 = 一次客户端提交"(例如视图里用 `now()` 打时间戳、或者把 `count()` 当作"这一批多少行"写进目标表),那么开了 `async_insert` 之后,一批可能横跨好几个客户端提交,时间戳与计数都跟着变。**"我开了 `async_insert`,物化视图的输出形状就不一样了"** 的根因就在这里,而它没有专门的章节可查,只能回到这条机制本身。
- **去重也跟着换判据。** 块级去重(§13.5)认的是**落盘的那个 block** 的指纹;攒批之后 block 边界变了,指纹自然也变了。服务端攒批还有自己的去重开关 `async_insert_deduplicate`(25.3 默认 `false`,见源码 `Settings.cpp`),它和复制表的 `insert_deduplicate` 是两回事——前者管"攒批时要不要按 token 去重",后者管"落盘时查不查去重标记"。要精确的"重发即去重",还是带上 `insert_deduplication_token`(§13.5)最稳。
- **第五点那条账仍然成立。** `wait_for_async_insert` 默认是 1(第 18 章 §18.6),客户端仍会等到"真正落盘"才返回,所以"错误会连带源表插入失败"没有变——攒批只是把"批"做大,并没有把视图从写入路径上摘下来。

一句话:`async_insert` 和物化视图不冲突,但它们作用在同一个"批"上——`async_insert` 决定"批"有多大,物化视图决定"批"怎么被加工。**上 `async_insert` 之前,先想清楚你那几条视图有没有"以客户端 `INSERT` 为单位"的假设;有,就得连假设一起改。**

## 13.14 小结

- **物化视图是一条挂在源表上的 INSERT 触发器,不是缓存。** 由这一条推出全部性质:只对新写入的行负责;看不到删除与更新;输出是**追加**而非覆盖;触发粒度是 block 不是 row;同步跑在写入路径上,所以既拖慢写入、又会因报错而**连带源表插入失败**。
- **两种形态,判据是"对什么负责"。** 默认形态(插入触发器)对**新行**负责,便宜、增量、但只有触发之后的数据;带 `REFRESH EVERY` 的形态对**源表的当前状态**负责,贵、全量、但每次都是自洽快照。选错的症状是"报表里静默地少了一半数据"。
- **三种落点。** `TO db.dest` 写进一张已存在的表、视图自己没有存储(唯一能指定目标表引擎的形态);不带 `TO` 时 ClickHouse 自动建内部表 `.inner.*`;`POPULATE` 一次性回填历史,**与 `TO` 互斥**——因为 `POPULATE` 走内部表那条路,而 `TO` 形态没有内部表。`POPULATE` 还带竞态。
- **增量聚合必须存状态,不是存结果。** 按 block 触发意味着同一 key 会有多份碎片。`sum` 侥幸能靠外层相加救回来,`avg`(52.5 vs 60)、`uniq`、`quantile` **一定错**。正解是 `AggregatingMergeTree` + `AggregateFunction(...)` 列 + 写入 `-State` / 读出 `-Merge`。`sum`/`min`/`max`/`any` 可用 `SimpleAggregateFunction` 走简化路径(存的就是值,查询用普通聚合函数,不需要 `-Merge`)。**正确性来自 `-Merge`,不来自后台合并;后台合并只是让它更省。**
- **幂等靠三样东西。** 块级去重(`insert_deduplicate`,非复制表还要 `non_replicated_deduplication_window > 0`)、自定义去重键 `insert_deduplication_token`(按分区生效)、以及把去重传播到下游的 `deduplicate_blocks_in_dependent_materialized_views`(**默认关**)。打开传播开关有一个已知副作用:**聚合型物化视图两次插入产出逐字节相同的输出块时,第二份会被误判重复而丢弃,且不报错**。
- **链式视图做分层 rollup 时,中间层必须是 `AggregatingMergeTree` 且存状态**;下一层的 SELECT 要用 `-Merge` + `GROUP BY` 处理"上一层还没合并的碎片"。链越长,源表那次 INSERT 越慢、错误传播面越大,二到三层通常够用。
- **回填历史数据的正确姿势是"先建视图、再带时间边界手动 `INSERT ... SELECT`、分批写"**;`POPULATE` 只在无 `TO` 时可用且带竞态;可刷新视图则天然自带历史(每次全量重算)。
- **可刷新视图的要点**:`REFRESH EVERY` + `OFFSET`/`RANDOMIZE FOR` 调度;默认**替换**(原子、无中间态),`APPEND` 是**追加**(会读到半成品,适合做定期快照);`DEPENDS ON` 必须**显式**写依赖,名字写错会让视图永远不刷;状态看 `system.view_refreshes`,`last_success_time` 比"最后一次尝试"有用。
- **取舍**:投影透明但只服务单表、不带 `FINAL`(`第 11 章 §11.12`);普通视图是宏、不谈性能;物化视图能力强但要显式查、并承担写入放大与错误传导;手搓预计算表最可控、要自己解决调度与幂等。**别拿物化视图去修正已经写错的历史数据**——它只对新数据负责。
- **挂在同一张源表上的多条视图默认串行执行**,由会话设置 `parallel_view_processing`(25.3 默认 `false`)控制:串行时源表一次 `INSERT` 的延迟是所有视图开销**之和**,打开后才并发、把关键路径压到"最大者",但视图之间就此失去先后保证,且"任一视图报错即源表插入失败"这条不变(§13.1)。
- **`async_insert` 会改掉触发粒度**:触发器认的是"服务端刷出的 block",不是客户端 `INSERT`。攒批后目标表碎片变少,但"一批"的边界、以及块级去重的指纹都跟着变;视图里若藏着"一批 = 一次客户端提交"的假设,要连假设一起改(§13.13)。
- **最大的坑永远是第一条**:建视图之前的数据不会自动进来。**先建视图、再回填、分批写**,这三件事做对,物化视图才算用对。
