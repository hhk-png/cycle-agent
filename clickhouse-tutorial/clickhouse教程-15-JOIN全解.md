# 第 15 章 JOIN 全解

JOIN 是 ClickHouse 里最容易写错、也最容易把机器搞挂的一类查询。写错的原因往往不是不懂 SQL,而是 ClickHouse 的 JOIN 有两组**互相正交**的选项,很多人只记住了其中一组;搞挂机器的原因则几乎总是一句话:**右表放错了边**。这一章先把这两组选项拆干净,再逐个讲清楚每种组合的语义、输出列和典型用途,最后给出那些"不用 JOIN 反而快一个数量级"的替代手段。

## 15.1 两组正交的选项

```text
kind(方向)                    strictness(严格度)
─────────────────────────    ──────────────────────────────────────
INNER / LEFT / RIGHT /       ALL     匹配多少行就产出多少行(笛卡尔)
FULL / CROSS / PASTE         ANY     右表只取第一个匹配
                             ASOF    取"键 ≤ 左表键"里最大的那个
                             SEMI    只保留左表有匹配的行,不输出右表列
                             ANTI    只保留左表没有匹配的行
```

这两组**可以组合,但不是“任意组合”都行** —— 能不能拼,既取决于最终选中的 join 算法(§15.7),也有一条方向矩阵。像 `LEFT ANY JOIN`、`INNER ASOF JOIN` 这类是稳妥的常见组合。官方矩阵里,`ALL`/`ANY`/`ASOF`/`SEMI`/`ANTI` 都能配 `INNER`/`LEFT`/`RIGHT`(`SEMI`/`ANTI` 在 `RIGHT` 方向也有对应写法,即 `RIGHT SEMI`/`RIGHT ANTI`),**但 `FULL` 方向没有配套的 `SEMI`/`ANTI` —— `FULL SEMI`、`FULL ANTI` 这类不合法**。真正会让“矩阵条目”落空的是**具体算法**:例如 `partial_merge` 把 `RIGHT`/`FULL` 限制成只接受 `ALL`,于是 `RIGHT SEMI` 在它上面也跑不了。所以**别把某个组合当成“一定合法”照抄**,拿不准的就用你那版实际跑一次。绝大多数困惑来自把它们混成一维的清单去背。

写法的形式是:

```sql
SELECT ... FROM left_table AS l
[INNER | LEFT | RIGHT | FULL | CROSS] [ALL | ANY | ASOF | SEMI | ANTI] JOIN right_table AS r
ON l.k = r.k [AND l.ts >= r.ts]      -- 或 USING (k)
```

## 15.2 默认严格度:ALL,以及一个广为流传的误解

`JOIN` 没写严格度时用的是 `join_default_strictness`:

```text
join_default_strictness   默认 ALL   可选 ALL / ANY / ASOF
```

这里要专门纠正一个流传很广的说法:**"ClickHouse 默认是 ANY"是错的**。默认值是 `ALL`,与标准 SQL 的 `JOIN` 行为一致 —— 右表有多行匹配就产出多行。这一点在 ClickHouse 的源码(`Settings.cpp` 里 `join_default_strictness` 的默认值是 `JoinStrictness::All`)和官方文档(`Default value: ALL`)里都写得很明确;本仓库的 mini-clickhouse 也照此对齐(`minich/settings.py` 的 `join_default_strictness` 默认值就是 `ALL`)。这里把来龙去脉讲透,**后面(§15.15、小结)只带结论、不再展开**。

这个误解之所以普遍,是因为 `ANY` 确实是 ClickHouse **推荐你显式写出来**的那个:

- 在宽表拼接的场景里,“左表一行匹配右表多行”通常意味着**右表数据有重复**,是数据质量问题;
- `ALL` 会把这种重复**放大成结果行数爆炸**(1 行 × 3 个匹配 = 3 行,三层 JOIN 之后就是几十行);
- `ANY` 让查询“随便取一个”,从而**把结果行数锁死在左表行数量级**。

代价是结果**不确定**:哪一行被选中取决于哈希表构建顺序,不保证跨版本、跨并发度稳定。所以它只适合“右表按 join key 唯一(或至少你确信重复行之间没有语义差别)”的场景。要消掉不确定性,用 `argMax(...)` 之类的聚合把“取哪一行”显式表达出来。

`ANY` 还有一个管**语义对称性**的老设置:`any_join_distinct_right_table_keys`(默认 `0`)。在 `0` 下,`t1 ANY LEFT JOIN t2` 与 `t2 ANY RIGHT JOIN t1` 结果**对称**,`ANY INNER JOIN` 对每个键只产出一行;把它设成 `1` 会退回 ClickHouse 早期的旧语义 —— 左右两种写法**不再等价**(允许多对一的键映射),`ANY INNER JOIN` 会像 `SEMI LEFT JOIN` 那样把左表每个键的行都留下。这个开关只为”迁移老代码时保住行为”而存在,新查询不要动它。

上面说 `ANY`”取哪一行不确定”,那是默认行为;有一个设置专门用来**把这个不确定性驯服**:**`join_any_take_last_row`**(默认 `0`,取 `0/1`)。置 `1` 时,`ANY` 取**右表最后插入**的那一行(而不是哈希表里碰巧第一个命中的那些行),于是”取最新一条”这类诉求有了确定的表达手段。**注意它与上面那个老开关互斥:`join_any_take_last_row` 不能和 `any_join_distinct_right_table_keys` 同时开启**(需要后者保持 `0`),两者同时置 `1` 会直接报错。想用 `ANY` 稳定地表达”取右表最新一行”,它就是那个缺失的旋钮。

## 15.3 五种严格度:语义、输出列、用途

**`ALL`**。右表每一个匹配行都产出一行。输出包含左右表的全部列。

**`ANY`**。右表只取**第一个**匹配行,每个左表行最多产出一行。输出列与 `ALL` 相同。

**`ASOF`**。"as of"即"截至某一时刻":对每个左表行,在右表里找**键相等**且**匹配列不大于左表**的那些行里最大的一个。它可以带**任意多个等值条件**,但**恰好一个不等值条件**:

```sql
SELECT t.sym, t.ts, t.price, q.bid
FROM trades AS t
ASOF JOIN quotes AS q
ON t.sym = q.sym AND t.exchange = q.exchange AND t.ts >= q.ts;
```

这里的等值条件(`t.sym = q.sym`、`t.exchange = q.exchange`)可以有**任意多个**,它们一起定义"按什么分桶";不等值条件(`t.ts >= q.ts`)则**只能有一个**,它定义"桶内怎么取最近一条"。这是时序数据的标准需求:把"不晚于这笔成交的最近一条报价"贴到每笔成交上。实现正是"按全部等值键分桶 + 在桶内按匹配列有序查找",所以**多一个不等值条件就表达不了** —— `ON t.ts >= q.ts AND t.price <= q.price` 这类写法会被拒绝。输出包含左右表全部列。

**`SEMI`**。只保留**左表中存在匹配**的行,**右表一列都不输出**。它的语义是"筛出左表里有一张名单上的人",而不是"把人名拼过来"。

**`ANTI`**。反过来,只保留左表中**没有匹配**的行,同样不输出右表列。它常用来做"差集"和"找孤儿数据"。

`SEMI`/`ANTI` 不输出右表列这一点要特别注意:选了 `SEMI` 又在 SELECT 里写 `r.some_col` 会直接报错 —— 这不是 bug,是它们本来就是**过滤**而非**连接**。

## 15.4 用一个小数据集看差别

左表 `l(id, v)` 三行,右表 `r(id, w)` 三行,注意 `r` 里 `id = 1` 有**两行**:

```text
l:  (1,'a')  (2,'b')  (3,'c')
r:  (1,'x')  (1,'y')  (2,'z')
```

以 `ON l.id = r.id` 为例:

```text
INNER ALL      (1,a,x) (1,a,y) (2,b,z)                 3 行
INNER ANY      (1,a, ?) (2,b,z)    ? 是 x 或 y,不确定   2 行
LEFT ALL       (1,a,x) (1,a,y) (2,b,z) (3,c, 默认值)    4 行
LEFT ANY       (1,a, ?) (2,b,z) (3,c, 默认值)           3 行
RIGHT ALL      同 INNER ALL + 未匹配的右表行(本例没有)   3 行
FULL ALL       同 LEFT ALL                              4 行
CROSS          3 × 3 = 9 行(笛卡尔积)
SEMI           (1,a) (2,b)         只有左表列           2 行
ANTI           (3,c)               只有左表列           1 行
```

把 `ANY` 和 `ALL` 的差别记牢:差别**只出现在右表有重复键的那些行上**。反过来说,如果你的右表在 join key 上确实唯一,`ANY` 和 `ALL` 结果完全一样,这时 `ANY` 是免费的性能保险。

## 15.5 ON 与 USING

```sql
-- ON:两侧各写一遍列名,结果里会出现两列 id
SELECT * FROM l JOIN r ON l.id = r.id;      -- 列: id, v, id, w  (重名的右侧加前缀)

-- USING:只写一次,结果里键列只出现一次
SELECT * FROM l JOIN r USING (id);          -- 列: id, v, w
```

`USING` 的可读性明显更好,而且它天然承认"这两个 `id` 是同一个东西"。`ON` 更灵活(允许条件左右不同名、允许多个不同条件)。**`USING` 下键列只出现一次,用的是左表那一份** —— 所以在 `FULL JOIN` 里,这一列在"只有右表匹配"的行上会是默认值,这一点在写 `FULL JOIN` 时要留意。

## 15.6 join_use_nulls:一个偏离 SQL 标准的默认值

```text
join_use_nulls   默认 0
```

这是本章最有价值的一条。

默认 `0` 时,外连接里**未匹配到的右表列填的是该类型的默认值**(`UInt8` 是 `0`,字符串是空串,`DateTime` 是某个纪元时刻),而**不是 NULL**。

于是这个 SQL 表现出诡异的行为:

```sql
SELECT l.id, r.w
FROM l LEFT JOIN r ON l.id = r.id;   -- l.id = 3 在 r 里没有匹配
```

`id = 3` 那一行的 `w` 会是 `0`。现在问题来了:

**错误做法**:用 `WHERE` 或 `HAVING` 之外的方式去判断"到底有没有匹配上",比如：

```sql
SELECT l.id, r.w
FROM l LEFT JOIN r ON l.id = r.id
WHERE r.w = 0;          -- 想找「没匹配上的行」
```

**后果**:你分不清 `w = 0` 是"匹配上了、右表那一行的 w 恰好是 0",还是"根本没匹配上、被填了默认值"。两种情况**混在一起**,而这个查询还会把"匹配上了但 w 真是 0"的行也一并选出来。更糟的是,如果 `w` 是 `UInt8` 而默认值 `0` 在你的业务里是合法取值(比如"状态码 0 = 正常"),这个 bug 会安静地活很久。

**正确做法**有两种,选一种并把注释写清楚:

```sql
-- 写法 A:显式打开标准语义,让未匹配的行拿到 NULL
SET join_use_nulls = 1;
SELECT l.id, r.w
FROM l LEFT JOIN r ON l.id = r.id
WHERE r.w IS NULL;      -- 现在语义清楚了:没匹配上

-- 写法 B:不改设置,用一个只存在于右表、且不可能为默认值的"哨兵列"判断
SELECT l.id
FROM l LEFT JOIN r ON l.id = r.id
WHERE r.id = 0;         -- 右表的主键不可能为 0 时才可用
```

`SET join_use_nulls = 1` 的代价:所有未匹配侧的列类型都被转成 `Nullable`,`Nullable` 在 ClickHouse 里是要额外开销的(多一个 null 位图,部分优化路径会失效)。所以它更适合"这一次查询我要精确语义"的场景,而不是全局默认打开。

`join_use_nulls` 管的是**输出侧**——未匹配的行填什么;另有一条与它无关的**匹配侧**规则——键里的 `NULL` 永远配不上任何行,包括另一张表里同样为 `NULL` 的行 —— 放在 §15.16。

## 15.7 算法:并行哈希、表交换,以及"右表要放小的一侧"

```text
join_algorithm   默认 "direct,parallel_hash,hash"
```

它是一张**按顺序取第一个适用**的算法清单。**25.3 的默认值就是 `direct,parallel_hash,hash`**;`parallel_hash` 自 **24.12** 起被排到 `hash` 前面,同一次改动还把旧的 `'default'` 取值废弃了(所以 `'default'` 现在等于 `direct,hash`,只是不该再写)。要特别点一句:**`ie_join` 不在 25.3 的默认列表里** —— 它是到 **26.8** 才被追加到清单末尾的(排在最后,只在其它算法都不适用时才轮到它,专门吃“无等值键、只有两条不等式”的 join)。判断某个算法到底会不会被选中,**以你那版 `system.settings` 里 `join_algorithm` 的实际值为准**。几个成员:

- `hash`:**右表整体进内存**建立哈希表,左表流式探测。这是最经典、最快、也最吃内存的一种。
- `parallel_hash`:同样是哈希连接,但**把右表(构建侧)按 bucket 切成若干份、用多个线程并行建表与探测**。构建侧越大、核越多,它相对 `hash` 的优势越明显 —— 代价是切桶时会同时持有输入缓冲与多个子哈希表,**内存峰值比 `hash` 更高**,所以它和§15.9 的 `max_bytes_in_join` 一起用时更要留余量。
- `partial_merge`:**排序归并**,两侧各自排序后归并,支持**落盘**,所以能吃下超大表,代价是排序开销。
- `grace_hash`:按 join key 哈希分区,每区轮流做哈希连接,分区可以落盘 —— 内存与 `hash` 相比更可控。**25.3 上它不需要任何前置开关**:写 `join_algorithm = 'grace_hash'` 就直接生效,内存上限由 `max_bytes_in_join`(默认 `0` = 不限)加 `join_overflow_mode`(默认 `THROW`)决定;溢写分桶由 `grace_hash_join_initial_buckets`(25.3 默认 `1`)和 `grace_hash_join_max_buckets`(默认 `1024`)控制。(**勘误**:本书早先在这里写过"要先设一个非零的 `max_bytes_before_external_join` 才会启用"——那条是错的。`max_bytes_before_external_join` / `max_bytes_ratio_before_external_join` **在 25.3 上并不存在**(25.3 的 `Settings.cpp` 里没有这两个声明,附录 A §A.16 也记着这一点),它们是更晚版本才引入的、用于让 `hash` **自动转** `grace_hash` 的阈值;而显式指定 `grace_hash` 本来就不看它们。) 
- `direct`:不走"右表建表"这条路,而是**直接拿左表的键去查右表**(适合右表很小、或者右表本身是个带索引的引擎,如 `Join` 引擎表、字典)。它常常是省内存的首选。
- `full_sorting_merge`:两侧全排序后归并。

两个**已废弃**的历史取值要认得,免得照抄老配置:`auto`(行为约等于 `direct,hash`)与 `prefer_partial_merge`(优先 `partial_merge`)。它们仍能被解析,但不该出现在新配置里。

**各算法能跑的 JOIN 形状并不一样**,这比"快不快"更容易踩坑:

- `hash` / `parallel_hash` 覆盖方向与严格度的**绝大多数组合**,是兜底选项;`parallel_hash` 只是 `hash` 的并行化,支持面与它相同。`hash` / `parallel_hash` 属于**支持非等值匹配条件**的几族之一(见下)。
- `partial_merge` / `full_sorting_merge` 属于排序归并族:两侧各自按匹配列排序后归并,所以能吃超大表,但方向(`RIGHT`/`FULL` 这类要两侧都有序)对它们有额外限制。**注意:排序归并族做不了非等值匹配条件**——恰恰相反,别再把"排序归并"当成"只有它才能做非等值匹配"。
- `grace_hash` 支持 `INNER`/`LEFT`/`RIGHT`/`FULL` 的 `ALL` 与 `ANY`,同样**支持非等值匹配条件**(见下)。
- `direct` 只支持 `INNER` 与 `LEFT`,且只认**单列等值键** —— 它按 key 点查,配置不了更复杂的条件。
- **`ASOF` 对算法有要求**:它只被 `hash` 与 `full_sorting_merge` 支持,所以 `ASOF` **同样受 `join_algorithm` 约束、走上面这套"选算法"的逻辑**,不是另起一条路(`join_algorithm` 里没放这两个、却写了 `ASOF JOIN`,就会失败)。而 `SEMI`/`ANTI` 是**一等的连接严格度**,和 `ALL`/`ANY` 并列,由正常的算法路径处理。

**非等值匹配条件单说。** `ON t1.key = t2.key AND t1.a < t2.a` 里那个不等值条件,由 **`hash`、`parallel_hash` 与 `grace_hash`** 三族支持 —— 要和上面几条对齐看,**排序归并族(`partial_merge`/`full_sorting_merge`)不在其中**。25.3 的官方 join 文档专门有一节 "Join with inequality conditions for columns from different tables":`ALL`/`ANY`/`SEMI`/`ANTI` 在 `INNER`/`LEFT`/`RIGHT`/`FULL` 下都可用,**前提是不等式两侧的列必须来自不同的表**,而且**不能和 `join_use_nulls` 一起用**。相关开关 `allow_general_join_planning` 的默认值已从 `false` 改为 `true`,它处理“更复杂的连接条件、但**只对 hash join 有效**”。

要提醒的是:**这仍是相对较新的能力、有边界**(列必须分属两表、排序归并族算法不支持、与 `join_use_nulls` 互斥);表达“最近一条”仍优先用下面 §15.3 的 `ASOF`,而不是“非等值 `ON` + `ANY`”。

这张对应关系随版本调整(新算法如 `ie_join` 仍在加入:它先在上游以显式取值加入,再到 26.8 被放进默认清单;**25.3 上既没有这一项、也不认识这个名字**)。**具体到某个组合是否可用,以你那一版的 `system.settings` 描述与文档为准**,拿不准的就实际跑一次,不要照搬。

怎么选:

- 右表能装进内存 → 让默认清单自己挑,最快;构建侧偏大时 `parallel_hash` 往往更划算。
- 右表装不进内存、但两侧都能排 → `partial_merge`。
- 右表很大且不该被复制到每个节点 → 优先考虑§15.11 的替代手段,而不是硬上 JOIN。

**构建侧的分块**:`max_joined_block_size_rows`(默认约 65409 行,以你那一版的 `system.settings` 为准)控制 join 处理右表匹配数据时的块大小 —— 把大块切成小块,避免单块一次性撑爆内存;设成 0 表示不限。数据倾斜严重时,再配合 `max_joined_block_size_bytes` 一起用。

**"右表放小的一侧"仍然对,但不再是铁律。** 早期 ClickHouse 确实**总是**把右表当构建侧、绝不做任何交换,所以把大表写在右边一定吃亏 —— "ClickHouse 不会替你重排"的说法就是这么来的。较新的版本在查询计划层加了一个开关:

```text
query_plan_join_swap_table   默认 auto   可选 auto / false / true
```

它只作用于 **`ALL` 严格度、且条件写在 `ON` 段**的 join:设成 `auto`,规划器**自己决定哪一侧当构建侧**(左表估算行数更小时就交换);`false` 恢复"永不交换"的旧行为;`true` 则强制交换。也就是说,即使你把大表写在右边,新规划器也可能替你换到左边。**它的引入版本现在能定准**:上游 `SettingsChangesHistory.cpp` 里写着 `{"query_plan_join_swap_table", "false", "auto", "New setting. Right table was always chosen before."}`,这条落在 **24.12** 的块内 —— 不是 24.9。25.3 上可用。

但**不要依赖它**:交换决策依赖的是**规划器对两侧行数的估算**,不是那套默认关闭的列统计(第 24 章 §24.16 讲清楚了 ClickHouse 不维护传统统计信息,§24.19 讲列统计在 25.3 上仍是实验性、默认关)—— 估算可能不准或缺失;而且它不覆盖 `ANY`/`ASOF` 等其它严格度。**实践建议不变 —— 仍然主动把行数少的那张表放右边**,这样无论规划器怎么选都不会太差。需要左边当"驱动表"时,改写顺序用 `RIGHT JOIN`,或换成半连接/`IN`,别为了可读性把大表放右边。

## 15.8 PASTE JOIN 与 CROSS JOIN:不需要键的连接

前面所有连接都要一个"键"来决定谁配谁。有两个方向连键都不要:

**`CROSS JOIN`(笛卡尔积)** 产出左右表的**全部组合**,行数 = `|左| × |右|`。它几乎只在两种场合是对的:一是把一张多行的小表(比如一组阈值、一组参数)广播到每一行上做参数化,二是你已经用极小的表或 `LIMIT` 把乘法卡死。除此之外,`CROSS JOIN` 意味着结果行数随两个输入相乘,**两侧都过千就已经是百万行**,是 OOM 与超时的常客。

**`PASTE JOIN`(粘贴连接)** 更冷门:它**按行号对齐**,把右表的第 i 行直接贴到左表第 i 行后面,不比较任何值。两侧行数相同时一一对应;**行数不同时,结果取较短的那一侧** —— 长的那一侧尾部多余的行被**直接截掉**,**不是**给短的一侧补默认值(这一点容易记反,长边截断、短边不补)。它解决的是"两份已经按同一顺序排好的结果,我想把它们并排拼成一张宽表"——比如两个各自 `ORDER BY ts` 的指标序列要合成一张宽表。**它完全不检查行之间的对应关系**:两边只要行序不同,拼出来的就是错的,而且不报错。所以用它的前提是你能**确证**两侧行序一致(通常意味着两侧都显式 `ORDER BY` 同一个键),否则请老实用 `ON` 或 `ASOF` 把对应关系写出来。

## 15.9 内存与保护

```text
max_bytes_in_join    默认 0(不限)   哈希表最多占多少字节
max_rows_in_join     默认 0(不限)   哈希表最多多少行
join_overflow_mode   默认 throw      超限时 throw / break / any
```

和 `group_by_overflow_mode` 一样,`break`/`any` 会给出**静默不完整的结果**。生产上建议显式设置 `max_bytes_in_join` 并保持 `throw`:查询失败是好事,查询悄悄给了错误结果才是事故。设了上限之后,如果查询因此失败,说明你该去改 SQL(常见解法就是§15.11 的字典或 `IN`)。

**那 `max_bytes_in_join` 到底该设多大?先估再设。** 哈希连接的内存大头是**构建侧(通常是右表)**:整张右表进内存建哈希表,占用量的量级是

```text
  构建侧内存 ≈ 行数 × (键列 + 需要参与匹配 / 输出的载荷列的宽度) × 放大常数
```

那个放大常数来自哈希表自身的开销(桶、指针、对齐填充),经验上在 **1.2–2 倍**之间:列越窄、行越碎、去重后桶越多,放大越明显。举个量级例子:右表 **1000 万行**、每行是 `(UInt64 键, UInt64 载荷)` 共 16 字节,裸数据约 160 MB,乘上哈希表开销,峰值大致落在 **200–320 MB** 这一档。参考一下口径:相邻 §15.7 提到的 `max_joined_block_size_rows` 默认约 **65409** 行,按同样 16 字节算,单块也就 1 MB 级——**真正能把内存撑爆的是右表总量,不是单块大小**,这正说明要盯的是"整张右表进不进得了内存"。把 `max_bytes_in_join` 设在这个估算的 1.5–2 倍上,给探测侧、以及并发跑的其它查询留出余量。

**超限时的两种行为。** 达到 `max_bytes_in_join` 之后,`join_overflow_mode` 决定怎么收场:默认 `throw` **直接报错**(推荐);`break`(以及 `any`)则**停下来、返回不完整的结果**——这就是上面说的"静默给错",生产上要避免。如果你的诉求是"**超了内存也要跑完**",那靠的不是 `join_overflow_mode`,而是**换算法**:在 `join_algorithm` 里让 `partial_merge` 或 `grace_hash` 接手,它们把数据**落盘**、以磁盘 IO 换内存——效果上等价于把"报错"换成"转排序归并 / 落盘哈希"。换句话说,`throw` 逼你面对问题,`partial_merge`/`grace_hash` 给你一条能跑完但不快的退路,两者不要在同一个查询里指望既省内存又全内存跑。

## 15.10 分布式下的 JOIN:为什么常常必须写 GLOBAL

在分片集群里,普通 JOIN 的执行方式取决于两侧表的分布式结构。

- **local join**(不写 `GLOBAL`):ClickHouse 可能把右表**在每个分片各自取一份**(只取本分片的右表数据),然后和本分片的左表数据连接。只有当**左右表用相同的分片键分片**时,这样得到的结果才正确。
- **`GLOBAL JOIN`**:右表先在一处被取全,然后**广播**到所有分片,每个分片用同一份完整的右表去连接自己的左表数据。结果是正确的,代价是广播。

规则化的说法是:**当右表是一个分布式表、而左表的每一行可能对应右表任意分片上的行时,必须用 `GLOBAL JOIN`**;否则你会得到"部分匹配"的结果 —— 每一行只在本地分片里找匹配,找不到就算没匹配。这个错误非常隐蔽,因为结果看起来"只是少了一些行"。

`prefer_global_in_and_join`(默认 `0`)可以把普通的 `IN`/`JOIN` 一律当成 `GLOBAL` 处理,省得每次记得写。

另一个开关管的是"分布式子查询出现在 `IN`/`JOIN` 里":`distributed_product_mode`(默认 `deny`,可选 `deny` / `allow` / `local` / `global`)。默认 `deny` 会直接报 `Double-distributed IN/JOIN subqueries is denied`;其余取值分别是"放行"、"把子查询换成各分片的本地表"、"自动改写成 `GLOBAL`"。很多人第一次跨表 JOIN 就撞上那个报错,根因就是这个 `deny` 默认值。

这一节的完整版 —— `GLOBAL JOIN`/`GLOBAL IN` 为什么是**正确性问题而不是优化**、以及 `distributed_product_mode` 每个取值的效果 —— 放在第 23 章 §23.8;这里只保留"什么时候必须写 `GLOBAL`"这一条判断,不重复那边的推演。

## 15.11 替代 JOIN 的手段:往往快一个数量级

在很多场景里,JOIN 是在用"最通用的重武器"解决"很窄的问题"。按场景选工具:

**维表映射** → **字典**(`dictGet`)。`dictGet('dict_name', 'attr', key)` 走内存哈希表或直接查外部系统,**没有建表开销,也没有右表放错边的风险**,还能在表达式里用。这是 ClickHouse 里做维表补全的**首选**,详见第 21 章。

**"某列的值在不在名单里"** → **`IN` + `Set` 引擎表**。它本质是半连接,不产出右表列,哈希表只存“存在性”,比 `SEMI JOIN` 更省内存,还支持 `GLOBAL IN` 的分布式语义。

**"贴最近一条"** → **`ASOF JOIN`** 或窗口函数(第 16 章)。不要用“非等值 join + 外层再取 max”去模拟 —— 那个写法会产生中间笛卡尔积,数据量一大就废。

**小维度映射(几十到几千行)** → **`arrayJoin` + `groupArray`**:把维度的键和值各收成一个数组再按下标映射。前提是维度**小到可以整体放进数组**,别拿它做通用 JOIN。

**反复连接同一张右表** → **`Join` 引擎表**。`ENGINE = Join(...)` 把右表预先建成持久化的内存哈希表,之后用 `joinGet('db.table', 'column', key)` **按 key 点查**。它**只按 key 取单列**、不做连接,所以没有右表放错边的风险,也几乎不吃额外内存。

## 15.12 反模式清单

**反模式一:大表 join 大表。** 两侧都是 TB 级明细时,任何算法都要么吃内存要么吃磁盘。先问“这个 JOIN 真的必要吗”,再问“能不能先各自聚合到很小的规模再连”。

**反模式二:右表放错边。** 见 §15.7,这是生产 OOM 的头号原因。

**反模式三:`LEFT JOIN` 之后在 `WHERE` 里过滤右表列,让"保留左表全部行"的意图落空。**

```sql
SELECT l.id, r.name
FROM l LEFT JOIN r ON l.id = r.id
WHERE r.name != 'closed';
```

这条查询的结局**取决于 `join_use_nulls`,而两种结局都不是你想要的**:

- **`join_use_nulls = 1`、或该列本身是 `Nullable` 时**,未匹配行的 `r.name` 是 `NULL`,而 `NULL != 'closed'` 在三值逻辑里是 `UNKNOWN`,不会被 `WHERE` 保留 —— 所有未匹配行被筛掉,结果等价于 `INNER JOIN`,**没有任何报错**。这才是那个广为流传的"外连接退化成内连接"版本,但它**只在未匹配值是 `NULL` 时成立**。
- **默认的 `join_use_nulls = 0` 时**(见 §15.6),未匹配行的 `r.name` 是**类型默认值** `''`,而 `'' != 'closed'` 求值为 **`TRUE`** —— 未匹配行**被保留**了,并没有"退化"。这里换上的是一个更安静的错误:判断"有没有匹配上"时,那个默认值 `''` 冒充真实数据参与了比较,"右表真有一行 `name = ''`"和"这一行压根没匹配上"被混为一谈。

所以两种语义下都不该这么写。

**正确做法**:既要过滤右表、又要保留左表所有行,就把条件写进 `ON`:

```sql
SELECT l.id, r.name
FROM l LEFT JOIN r ON l.id = r.id AND r.status != 'closed';
```

`ON` 里的条件只决定“这一行算不算匹配”,不匹配的行仍以“未匹配”的形态保留 —— 这才是外连接的本意。

**反模式四:用 JOIN 做维度补全而不用字典。** 每次都把维表搬进内存建哈希表,换来的只是"写法更像 SQL"。有字典就用字典。

**反模式五:把 `ON` 里的非等值条件当成"匹配之后再剔除"的残余谓词。** 很多人以为 `ON t1.a < t2.a` 这种条件哈希连接根本处理不了、只在等值键匹配上之后再过滤一遍,**这个印象是错的**:25.3 上 `hash`/`grace_hash` 会让非等值条件**参与匹配本身**(见 §15.7),而不是事后剔除。真正该警惕的是两件事:其一,这类一般化的连接条件依赖新查询规划器(`allow_general_join_planning`),**边界是明确的:列必须分属两表、排序归并族算法不支持、且与 `join_use_nulls` 互斥**,别假设某个组合一定被支持;其二,把复合条件和 `ANY` 叠加时语义会微妙——既然非等值条件参与了匹配,能匹配上的候选集合变了,`ANY` "取第一个"的规则也跟着变。所以:要精确表达"某键下匹配列最大的那一行"(即"最近一条"),用 `ASOF`(它把这个语义写死),而不是"非等值 `ON` + `ANY`"。

## 15.13 FINAL 与 JOIN:顺序、可用性与代价

这一章前面一次都没提 `FINAL`,但它和 JOIN 撞上的概率不低 —— 只要参与连接的表是 `ReplacingMergeTree`(或 `Summing`/`Aggregating`/`Collapsing`/`VersionedCollapsing` 这几族带"收敛语义"的变体),你大概率就想"先去重再连"。先把 `FINAL` 是什么记牢(详见第 07 章 §07.14 与第 05 章):它是**读时**的去重/归并 —— 读取时就把同一排序键下的多个版本按引擎规则合并成一行,把"最终一致"变成"立即一致"。

**语法位置**。`FINAL` 是**表引用**上的修饰,必须写在 `table [AS alias]` **之后**、`ON`/`WHERE` 之前:

```sql
-- 两个表各自去重后连接
SELECT ...
FROM orders AS o FINAL
INNER JOIN users AS u FINAL ON o.user_id = u.id;

-- WHERE 要写在 FINAL 之后
SELECT ... FROM orders AS o FINAL WHERE o.status = 'paid';
```

写错位置(`FROM orders FINAL AS o`)是语法错误。**每一张需要去重的表都要显式写 `FINAL`**,别指望"只写一个就全表生效"。

**一条版本差异,必须先说清楚。** 在**较早的版本**里,把 `FINAL` 写在 JOIN 的**最左表**上,会“波及”到所有被连接的表(等于给每个表都加了 `FINAL`)。该行为直到 **26.6** 才被修掉(上游 PR #108979:改成“**`FINAL` 只作用于它自己那张表**”),并留了一个兼容设置 `analyzer_compatibility_apply_final_to_all_joined_tables` 用来退回旧语义。**所以在 25.3 上,这条旧语义仍然生效** —— 如果发现“只给左表加了 `FINAL`,右表却也被去重了”,那不是错觉;稳妥写法始终是**在每张需要去重的表上显式写 `FINAL`**。

**可用性限制**(和 JOIN 无关、但一起会踩):`FINAL` 只对支持收敛语义的那几个引擎有意义,标准 `MergeTree` 用不了;`FINAL` 也不能和 `arrayJoin` 一起用。至于"**CTE / 子查询里能不能写 `FINAL`**",这条**不能一刀切**:`ILLEGAL_FINAL`(错误码 181)确实存在,但它是为**引擎 / 位置**问题抛的,**并不意味着"任何 CTE / 子查询都禁止 `FINAL`"**——上游反而有过"`FINAL` 写在 CTE 里被接受、却没真正生效"这类 bug 的修复历史。稳妥的说法是:**它在 CTE / 子查询里的行为在不同版本上变过(出现过"接受了却不生效"的 bug),用之前务必在你自己的版本上先验一遍——此处按未确认处理。**

**"FINAL 后再 JOIN" 与 "JOIN 后再 FINAL" 的差别。** 这两句话听起来对称,实际不对称:

- **FINAL 后再 JOIN**(上例那种写法)是**能直接表达、也通常是你想要的**:先在各自表内把版本收敛成一行("每个键的最新状态"),再拿收敛后的行去连接。好处是**进入连接的行数更少**,哈希表更小、内存更省;那个被去重的右表维表尤其受益。
- **JOIN 后再 FINAL** 却**没法直接写出来**:`FINAL` 只能贴在**表引用**上,而连接的结果是一段**子查询**。这里说的限定是——**"连接结果"这个位置**贴不上 `FINAL`,别读成"任何子查询里都不许写 `FINAL`"(本节前面已标:CTE/子查询里能不能写 `FINAL` 本身**未确认**,`ILLEGAL_FINAL` 是为引擎/位置问题抛的,不是一刀切禁止)。所以"先连起来、再把连接结果整体去重"这个诉求,只能靠别的语义逼近 —— 用 `GROUP BY` + `argMax`/`any` 把每个键留一行,而不是 `FINAL`。

实践结论很直接:**需要去重时,去重要发生在连接之前**(把 `FINAL` 加在每张需要去重的源表上)。这也符合直觉 —— 连接是**放大**行数的操作(尤其 `ALL` 严格度),先放大再去重,等于把最贵的几步都做在了最大的数据上。

**代价。** `FINAL` 是**查询时**归并,并不廉价:它要在读取阶段实时合并 part、额外读排序键列、并占用更多内存(并行度由 `max_final_threads` 控制)。在宽表、大表上,`SELECT ... FINAL` 往往比不带 `FINAL` 慢一个量级。**能用 `OPTIMIZE TABLE ... FINAL` 在写入侧把 part 合并掉(或干脆改用 `AggregatingMergeTree` 边写边收敛)、从而避免读时 `FINAL` 的,就别把 `FINAL` 留在查询里** —— 第 07 章 §07.14 也讲过同一条:`FINAL` 通常不是正解。

## 15.14 `Join` 引擎表与 `joinGet`:把右表驻留在内存

§15.11 把"反复连接同一张右表"列为 `Join` 引擎表的用武之地,这一节把它的建表语法与 `joinGet` 的返回类型约束补全。

`Join` 引擎把它管理的表**预先建成一份常驻内存的哈希表,同时把数据落盘**:`INSERT` 时写进磁盘目录,**启动时再载回内存**(这一持久化由设置 `persistent` 控制,默认 `1` = 开;关掉它能省 I/O,代价是重启即丢)。于是它既可以被普通 `JOIN` 当右表用,也可以用 `joinGet` **按 key 直接点查**:

```sql
CREATE TABLE id_to_name (
    id   UInt64,
    name String
) ENGINE = Join(ANY, LEFT, id);      -- Join(严格度, 方向, 键列…)

INSERT INTO id_to_name VALUES (1, 'alice'), (2, 'bob');

-- ① 当右表参与 JOIN
SELECT l.id, r.name
FROM events AS l
LEFT JOIN id_to_name AS r ON l.id = r.id;

-- ② 按 key 点查单列(不写 JOIN)
SELECT joinGet('default.id_to_name', 'name', toUInt64(42)) AS name;
```

三个参数的含义:

- **严格度**(第一个):`ANY` / `ALL` / `ASOF` 等,决定"同一个键有多行"时怎么处理 —— 例如 `ANY` 表示同键只保留一行(与 `ANY JOIN` 一致)。
- **方向**(第二个):`LEFT` / `INNER` 这类,说明这张表打算被放在连接的哪一侧(通常写成它会被当右表用的那个方向);允许的取值以你那版文档为准。
- **键列**(第三个及以后):`JOIN ON` 与 `joinGet` 都会用到的键。键列要在建表时逐一声明(`Join(..., k1, k2)` 表示多列复合键);它们有类型约束,可空列一般不能当键(具体限制以文档为准)。

**`joinGet` 的返回类型约束**是关键,也是最容易踩的地方:

- `joinGet('db.table', 'column', key...)` **只取一列**,所以它**不做连接、不产出右侧整行** —— 这正是它比 JOIN 更安全、也几乎不吃额外内存的原因。
- 它**只按 `key` 做等值点查**,不支持范围或任意条件。
- **返回类型就是那一列的类型**(严格按列定义),因此 `key` 的**类型、个数、顺序**必须与建表时的键列**逐一对上**;类型不匹配会直接报错,而不是替你隐式转换(常踩的坑:`key` 传了 `UInt64`,键列却是别的整型)。
- 严格度会影响结果:在 `ANY` 语义下同键只留一行,`joinGet` 返回的就是那一个值;若用 `ALL` 语义、同键多行,单列点查就没有唯一答案 —— 所以 `joinGet` 的典型搭配是 `ANY`。
- 找不到 key 时,`joinGet` 返回该类型的默认值;要区分"键不存在"和"值恰好是默认值",用 `joinGetOrNull`,它返回 `Nullable(...)`。

一个诚实的边界:`Join` 引擎表把数据**整份放进内存**,表一大就会吃掉可观的内存;它适合**小维表**(能常驻内存的那一档),不是给大表用的。这也正是它相对"每次现建哈希表"的普通 JOIN 的优势 —— 建表一次、复用多次,代价转为内存常驻。两条与它绑定很深的边界也要记:一是那份落盘副本在**非正常重启(硬杀进程、断电)**时可能损坏,必要时得手动删掉坏文件重建;二是 `Join` 表**不能参与 `GLOBAL JOIN`**,而且建表与查询两侧的 `join_use_nulls` 必须一致 —— 这两点是它区别于"独立字典对象"的地方。

**与 mini-clickhouse 的关系**:mini 没有 `Join` 引擎表,也没有 `joinGet`;它每次 JOIN 都在查询期现建哈希表(§15.15),所以"小维表反复连接、把哈希表常驻下来"这个优化在 mini 上用不上。

## 15.15 与 mini-clickhouse 的对照

`mini-clickhouse/minich/execution/joins.py` 的结构很清楚地实现了本章前半部分:

- `JoinSettings` 里的三个字段正好对应本章的三组开关:`kind`、`strictness`、`use_nulls`。
- `execute_join` 的 build 阶段就是"右表进内存建哈希表":`table[key] = [行号...]`。**`ANY` 的实现只有一行** —— 遇到同键第二行直接 `continue`,不追加进桶。SEMI/ANTI 则把每个桶截成"只留第一个",因为它们只关心"有没有"。
- **哈希连接只处理等值条件**:`extract_join_keys` 把 `ON` 拆成"左键列表 / 右键列表"两组,非等值条件被忽略(代码里明确注释了这一点)。找不到任何等值条件时,非 CROSS JOIN 会直接报错 `NOT_IMPLEMENTED`。
- **`USING` 的键列只输出一次**:`_build_result_block` 里用 `if c.name.lower() in using: continue` 跳过了右表的键列,与 §15.5 描述的一致。
- **`join_use_nulls` 默认是 0**(`JoinSettings.use_nulls=False`):未匹配的右侧列填**类型的默认值**。`_build_result_block` 里用 `missing_value = None if settings.use_nulls else dtype.default_value()` 区分两种语义 —— 与真实 ClickHouse 的默认行为一致。实测:未匹配的右侧 `UInt8` 列得到的是 **`0`** 而不是 `NULL`(`mini-clickhouse/examples/08_joins.py` 的 `LEFT JOIN` 输出也印着这一点)。

必须诚实标注的两条差异(模块文档里也写了):

1. **只有 hash 一种算法,而且不检查内存上限**。`max_bytes_in_join` / `max_rows_in_join` / `join_overflow_mode` 在这里只是注册了名字 —— 右表多大就吃多少内存。`partial_merge`、`grace_hash`、`direct` 都没有实现。
2. **不做 join 顺序重排**。这里比真实 ClickHouse 更极端:真实 ClickHouse 在新版里会通过 `query_plan_join_swap_table`(`auto`)在 `ALL` join 上考虑交换构建侧,而 mini 永远把右表当构建侧;又因为它连内存上限都不检查,§15.7 的“右表放错边”在这里**后果更严重**。

**ASOF 是简化的**:`_pick_asof` 只按等值键建表,然后取"该键下最后一个匹配",**并没有解析不等值条件、也没有真的按匹配列排序**。所以只有当右表本身按时间有序时,结果才与真实 ClickHouse 等价。教程正文里给出的 `ON t.sym = q.sym AND t.ts >= q.ts` 才是正确用法。

**对齐点**:`joins.py` 的模块注释、`minich/settings.py` 的 `join_default_strictness` 默认值、以及 `JoinSettings.default_strictness` 三处一致,默认严格度都是 `ALL`,与真实 ClickHouse 的 `join_default_strictness` 默认值对齐(§15.2 已把"默认不是 ANY"讲清,此处不再重复)。

## 15.16 JOIN 键里的 NULL:永远配不上任何行

这里补上一条容易被 §15.6 的讨论盖过去的**匹配侧**规则,它和 `join_use_nulls` 取什么值无关:

**键为 `NULL` 的行,永远配不上任何行 —— 包括另一张表里键同样是 `NULL` 的行。**

原因还是三值逻辑:`ON l.k = r.k` 里的条件本质是 `equals`(`=`),而 `NULL = NULL` 求值为 `UNKNOWN` 而不是 `TRUE`,JOIN 只接受使条件为真的配对。于是两张表里各有一行 `k = NULL` 时,它们**不会**被配到一起:`LEFT JOIN` 下,左表那一行仍然保留(右表列按 `join_use_nulls` 填默认值或 `NULL`),但永远处于"未匹配"形态;`INNER JOIN` 下它则被直接丢掉。两个容易踩的连带后果:

- **别拿 `NULL` 当键里的"缺失值"。** 维表里把没归类的那批行写成 `NULL` 键,是"数据明明在、JOIN 却少行"的常见根因,而且不报错。要么把缺失键归一成一个真实的哨兵值(`0`、`'__unknown__'`),要么在连接前用 `coalesce` 把 `NULL` 显式替掉。
- **要让 `NULL` 键能互相配对,只能用 `<=>`。** 把 `ON l.k = r.k` 换成 `ON isNotDistinctFrom(l.k, r.k)`(`<=>`)时,`NULL` 与 `NULL` 被判定为"不可区分",就能配对。注意版本边界:在本书对标的 25.3 上,`isNotDistinctFrom`/`<=>` **只能出现在 `JOIN ON` 段**(§17.5),而 JOIN 恰好就是它唯一能用的地方;要写进 `WHERE` 等通用表达式得等 25.10+。

这条规则对 `USING`、以及对 `ASOF` 里的**等值条件**部分一视同仁:凡是用 `=` 表达的键相等,`NULL` 都不参与。

## 15.17 小结

- JOIN 的选项是**两组正交**的:方向(`INNER`/`LEFT`/`RIGHT`/`FULL`/`CROSS`/`PASTE`)与严格度(`ALL`/`ANY`/`ASOF`/`SEMI`/`ANTI`)。这两组**可组合但并非任意组合都可用** —— `SEMI`/`ANTI` 能配 `INNER`/`LEFT`/`RIGHT`(含 `RIGHT SEMI`/`RIGHT ANTI`),但**没有 `FULL SEMI`/`FULL ANTI`**;再往下还受最终选中的算法约束,拿不准就实测。
- `join_default_strictness` 默认是 **`ALL`**(不是 ANY);`ANY` 是推荐**显式写出**的优化手段,它把结果行数锁在左表量级,代价是结果不确定。
- `SEMI`/`ANTI` **不输出右表任何列**;`ASOF` 支持**任意多个等值条件 + 恰好一个不等值条件**。`CROSS` 是笛卡尔积、`PASTE` 按行号平贴,这两个方向不需要键。
- **`join_use_nulls` 默认 0**:未匹配的右表列填类型默认值而不是 NULL。默认值本身可能就是合法数据,所以判断"有没有匹配上"要靠 `SET join_use_nulls = 1` 或一个不可能的哨兵值。
- **键里的 `NULL` 永远配不上任何行**(`NULL = NULL` 不为真),这与 `join_use_nulls` 无关;要让它俩配上,`ON` 里得改用 `isNotDistinctFrom`(`<=>`,25.3 上仅限 `ON` 段)。
- 算法按 `join_algorithm` 清单挑(`hash`/`parallel_hash` 支持面最广,`partial_merge` 能落盘,`direct` 只认单列等值),**右表仍要主动放小的一侧**;新版的 `query_plan_join_swap_table`(`auto`)会在 `ALL` join 上考虑替你交换构建侧,但别依赖它。内存保护靠 `max_bytes_in_join` + `join_overflow_mode = throw`。
- 分布式下 `GLOBAL JOIN` 常常是必需的,否则结果会静默地少行;`distributed_product_mode`(默认 `deny`)决定分布式子查询出现在 `IN`/`JOIN` 里时是放行还是报错(细节见第 23 章)。
- 大量场景里**字典 / `IN` + `Set` / `ASOF` / `Join` 引擎表 / `joinGet`** 比 JOIN 更快也更安全。
- 最隐蔽的反模式是"`LEFT JOIN` 之后在 `WHERE` 里过滤右表列":`join_use_nulls = 1` 时它把外连接悄悄退化成内连接,默认 `0` 时它让类型默认值冒充真实数据参与比较——两种语义下都该把条件移进 `ON`。
- **`FINAL` 与 JOIN**:`FINAL` 写在 `table AS alias` 之后,**每张需要去重的表都要显式写**。“最左表的 `FINAL` 会波及所有被连表”这条旧语义直到 **26.6** 才被改成只作用于自己那张(25.3 上仍是旧语义)。**去重必须发生在连接之前** —— 连接会放大行数;而"JOIN 后再 `FINAL`"写不出来 —— 这里说的是**"连接结果"这个位置**贴不上 `FINAL`(`FINAL` 只能贴在表引用上),不等于"任何子查询都不行"(CTE/子查询里的 `FINAL` 行为**未确认**,见 §15.13)。`FINAL` 是查询时归并、代价高,能靠 `OPTIMIZE ... FINAL` 或 `AggregatingMergeTree` 在写入侧收敛,就别把它留在查询里。
- **`Join` 引擎表**:`ENGINE = Join(严格度, 方向, 键列…)` 把右表常驻内存;`joinGet('db.table','column',key…)` **只按 key 点查单列**、返回该列的原类型(键的类型/个数/顺序必须与建表一致),找不到键时用 `joinGetOrNull` 拿 `Nullable`。它适合小维表被反复连接的场景。
