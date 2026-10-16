# 第 09 章 DDL、ALTER 与运维语句

在 MySQL 里,`ALTER TABLE` 通常意味着一件事:等。加个索引要重建,改个类型要拷贝全表,业务方得提前发公告。ClickHouse 的 `ALTER` 看起来语法更像,但它的行为逻辑完全不同 —— 有些 `ALTER` 是**毫秒级**的纯元数据操作,有些是**几分钟到几小时**的后台重写,而它返回给你的时间点,往往跟"数据改完了没有"没有关系。

这一章要解决的问题是:拿到一条 `ALTER` 语句,你怎么在写之前就知道它是哪一类、要等多久、失败之后会留下什么状态;以及 `DETACH` / `ATTACH` / `EXCHANGE` / `SYSTEM` 这些运维语句,分别对应生产环境里的哪个具体动作。

## 09.1 先把 ALTER 分成两类

这是本章最重要的一件事。ClickHouse 的 `ALTER` 可以按"要不要动数据文件"一刀切:

**第一类:元数据操作。** 只改表的定义,已经写好的 part **一个字节都不动**,秒级返回。包括 `ADD COLUMN`、`DROP COLUMN`、`RENAME COLUMN`、`COMMENT COLUMN`、`MODIFY SETTING`、`ADD/DROP INDEX`、`ADD/DROP PROJECTION`、`ADD/DROP CONSTRAINT`、`MODIFY ORDER BY`(受限,见 §09.2)。**`MODIFY TTL` 不在这一列里** —— 它长得像改元数据,默认却会连带排一次 mutation,理由见 §09.2。

**第二类:数据重写操作。** 要重新产出数据文件,异步执行,耗时取决于数据量。包括 `UPDATE`、`DELETE`(通过 `ALTER` 走的 mutation)、`MODIFY COLUMN`(改类型或改默认表达式)、`MODIFY COLUMN ... REMOVE TTL`、`CLEAR COLUMN`,以及 §09.5 那一组 `MATERIALIZE`。

判断方法很简单:问一句"这条语句会不会改变**已有数据的值、类型或物理布局**"。会,就是第二类。`CLEAR COLUMN` 是个容易归错类的例子:它**保留列的定义、把这一列的数据清空(填回默认值)**,因为要动数据,所以被实现成 mutation,属于第二类(§09.2 会再确认一次)。

**但"改数据"在 25.x 上其实是三件不同的事,不能一律叫 mutation。** 它们的机制与代价差得很远,先分清再动手:

- **重量级 mutation** —— `ALTER TABLE t UPDATE ... WHERE ...` / `ALTER TABLE t DELETE WHERE ...`。它**重写受影响的整个 part**(读出 → 套谓词与赋值 → 写一个新 part → 把老 part 标记删除),改一行和改一百万行的代价差不多,而且**要排队等后台线程来推进**(§09.3、第 18 章 §18.8)。这是最"重"的一类。
- **轻量级 delete** —— 不带 `ALTER` 的 `DELETE FROM t WHERE ...`(lightweight delete, LWD)。它**不重写 part**,而是把命中的行在一个隐藏的 `UInt8` 掩码列 `_row_exists` 上标成 `0`;`SELECT` 会自动补一个对 `_row_exists` 的 `PREWHERE` 过滤,于是被删的行**立刻**从查询结果里消失,而物理清除仍留给后台合并。它早已不是实验特性(22.8 引入、23.3 起默认可用),用"按条件删几行、又不想付重写整 part 的代价"时用它;代价是被删数据在合并清掉前一直占空间。完整语义在第 18 章 §18.9。
- **轻量级 update** —— 语法形如 `UPDATE t SET ... WHERE ...`(同样不带 `ALTER`),靠 **patch part**(只存被改的列,外加 `_part`、`_part_offset` 之类的定位信息)在读取时叠加,而不是重写 part。**它比前两者新得多,本书对标的 25.3 上还没有** —— 上游要到 **25.8** 才让轻量级 update 随开关 `enable_lightweight_update`(别名 `allow_experimental_lightweight_update`)进入 Beta,所以 25.3 上确实没有这个能力。这里只作名词区分,别照搬语法建生产流程。

`ADD/DROP INDEX` 归在第一类,但它有一个必须单独说明的陷阱:返回时**变的只是元数据**,跳数索引真正的**摘要数据**只对**之后写入的 part** 生效 —— 已经躺在盘上的老 part,要么等后续合并顺手带上,要么显式跑一次 `MATERIALIZE INDEX`(§09.5)。所以对索引而言,"返回即完成"只对元数据成立,对索引数据不成立。

为什么这个区分如此重要?因为两者的**返回语义**完全不同。第一类返回的时候,事情已经做完了。第二类返回的时候,事情**才刚开始**:

```sql
ALTER TABLE hits DELETE WHERE ts < '2024-01-01';
-- 语句返回了,但数据还在。删除是异步进行的。
```

把第二条当成第一条来用(比如脚本里"删完数据立刻插入新数据,靠查询结果验证"),是生产环境里非常常见的一次事故。

## 09.2 元数据操作:为什么 ADD COLUMN 不用重写

`ADD COLUMN` 是"不重写 part"这件事最典型的例子。它的实现方式可以概括成一句话:**老 part 里没有这一列的文件,读出来的时候现补默认值。**

```sql
ALTER TABLE hits ADD COLUMN country String DEFAULT 'unknown';
ALTER TABLE hits ADD COLUMN score UInt32;      -- 没写 DEFAULT
```

第一条给了一个 `DEFAULT 'unknown'`,以后对老数据的读取都会得到 `'unknown'`。第二条没给 `DEFAULT`,那么补上的是**列类型的默认值** —— `UInt32` 就是 `0`,日期类型是 `1970-01-01`。也就是说,没写 `DEFAULT` 时 `ADD COLUMN` 不会报错,而是**静默地给老数据补 0/空串**,这件事必须你自己知道。想避免这种默认值污染,加 `MATERIALIZED` 列比普通列更合适(§09.5)。

也正因为读的时候才补值,`DROP COLUMN` 同样只是改了元数据 —— 数据文件被标记为待删除,后台慢慢清理,不阻塞任何事。`RENAME COLUMN`、`COMMENT COLUMN`、`MODIFY SETTING` 属于同一类:它们只改定义,盘上的 part 一个字节都不动。

**但 `MODIFY TTL` 是个例外,而且是最容易猜错的那一个。** 直觉上你会说"TTL 改了只影响**之后**的合并,已经躺在盘上的 part 不会立刻被裁剪" —— 在默认设置下**这句话是错的**。会话设置 `materialize_ttl_after_modify` 的默认值就是 **`1`**(25.3 的 `Settings.cpp` 里写作 `DECLARE(Bool, materialize_ttl_after_modify, true, ...)`),所以 `ALTER TABLE ... MODIFY TTL` 在改完定义后会**顺手排一次 mutation**:读列数据、算新的 TTL 表达式、重写受影响的 part。把 TTL 从"一年"收紧成"一个月",代价和一次全表 mutation 是同一个量级,不是秒级返回。

想要"只改定义、不动存量"的便宜路径,得显式把这一步关掉:`SET materialize_ttl_after_modify = 0` 之后再 `ALTER TABLE ... MODIFY TTL`,存量 part 就只会在**之后的合并**里按新 TTL 处理;想让某个时刻的存量立刻追平,再显式跑一次 `ALTER TABLE ... MATERIALIZE TTL`(§09.5 讲的 `MATERIALIZE` 系语句正是把这一步显式化的工具)。两条路的取舍很清楚:**留着默认值 = 一次重写换全表口径立刻一致;关掉它 = 立即返回,但新旧两套 TTL 会共存到下一次合并为止。** 选哪条取决于你是否能接受"这段时间里旧数据还在"。这也解释了为什么 §09.1 那张分类表要把 `MODIFY TTL` 摘出来单独说 —— 它是一句**语义上像元数据、代价上像 mutation**的语句。

`CLEAR COLUMN` 是一个容易和 `DROP COLUMN` 混淆的语句:它保留列的定义,但把这一列的数据全部清空(填回默认值)。它**要动数据**,所以属于第二类 —— 它其实是作为 mutation 执行的。

`MODIFY ORDER BY` 需要特别提醒。排序键决定 part 内部数据的物理顺序,而 part 是不可变的 —— 一旦改了排序键,所有已有 part 的有序性假设就失效了。所以 ClickHouse 只允许一种很窄的修改:**新的排序键必须在旧排序键的基础上追加列,而这些追加的列只能是同一条 `ALTER` 里用 `ADD COLUMN` 新增的列**。也就是说,不能把一张有数据的表的排序键从 `(a)` 改成 `(a, b)`,除非 `b` 恰好是这次 `ALTER` 刚加上的新列;任何会打乱已有前缀顺序、或者引用已有列的修改都会被拒绝。

错误做法 → 后果 → 正确做法:

```text
✗ 错误做法:以为 MODIFY ORDER BY 像改个索引一样随意,直接在
           生产表上把 ORDER BY (a) 改成 ORDER BY (b)。
→ 后果:语句被拒绝(改了前缀);如果绕过限制硬来,已有 part 的
        有序性被破坏,任何依赖主键裁剪的查询都会返回错误结果 ——
        注意是"错误结果"而不是"慢查询"。
✓ 正确做法:排序键在设计阶段就定好;事后只能追加同一条 ALTER 里
        ADD COLUMN 新增的列。真要换,新建一张表 + INSERT SELECT 重灌,
        再用 EXCHANGE TABLES 原子切换(见 §09.9)。
```

## 09.3 数据重写:mutation 的机制

不带 `ALTER` 的 `DELETE FROM`(轻量级删除,§09.1)不走这条路;本节讲的是**通过 `ALTER` 走的 `UPDATE` / `DELETE`**,ClickHouse 里叫 **mutation**(变更)。它最核心的一条事实是:**不是就地改,而是重写受影响的 part**。

```text
ALTER TABLE t UPDATE x = 1 WHERE id = 5
   │
   ├── ① 立刻返回:登记一条 mutation(带递增编号)
   │        system.mutations 里立刻能看到:
   │          mutation_id  command            is_done  parts_to_do
   │          0000000001  UPDATE x = 1 …      0        3
   │
   └── ② 之后,后台池的线程把受影响的 part 逐个挑出来,
          把谓词和赋值表达式应用一遍,产出"改过的"新 part
          parts_to_do 逐个递减 → 归零 → is_done = 1
```

也就是说,mutation 的正确心智模型是"**排队 + 由后台线程池推进**",而不是"一条 DML 语句":一条 `DELETE` 可能几秒生效,也可能几小时,取决于后台池有没有空、以及有多少受影响的 part 要重写。真实 ClickHouse 有一条**独立的执行路径**:后台线程池(`background_pool_size`)里空闲的线程会主动去挑待处理的 mutation 任务来跑,它和合并**共用同一个资源池**,但**并不需要等某个具体的合并发生**;所以"表已经合并干净、又没有新写入"并不意味着 mutation 会卡住,只要池子里还有空闲线程,它就会继续推进。

需要说明的是,本书配套的 mini-clickhouse 走的是**简化路径**:mini 只在"part 参与合并"或 `OPTIMIZE` 时顺带套用 mutation,缺少真实 ClickHouse 里那套"空闲线程主动挑 mutation 执行"的独立路径(见 §09.14、第 18 章 §18.8)。读本章时请把"靠合并推进"理解为 **mini 的行为**,而不是真实 ClickHouse 的行为。

它为什么必须这么慢(列压缩 + 不可变 part 意味着只能"读出→改→写新 part"),以及后台池与合并、TTL、副本拉取如何互相排队,属于"执行时机与机制",完整展开在第 18 章 §18.8,本节只记结论和怎么操作它。

如果确实需要同步语义,`mutations_sync` 让语句等到 mutation 真正做完再返回:

```sql
ALTER TABLE t DELETE WHERE id < 0 SETTINGS mutations_sync = 1;
-- 1 = 等到当前副本完成;2 = 等到所有副本都完成
```

它的默认值是 0(完全异步)。注意设成 2 在复制表上意味着要等所有副本,慢的副本会拖住整条语句(第 22 章)。

**另有一个直接针对"得等后台重写、不能立刻看到结果"这个痛点的开关:`apply_mutations_on_fly`。** 打开它(默认关闭)后,那些**还没落到 part 上的 mutation 会在读的时候现算**——查询在读取阶段顺手把尚未应用的谓词/赋值套上,于是 `ALTER ... UPDATE/DELETE` 之后能**立刻**读到新结果,而不必等后台线程把 part 重写完。它把"最终一致"变成"读时一致",代价是每次查询都要多算一层尚未物化的 mutation(读路径变重);它主要面向点查/轻量场景,大批量重写时不如直接等后台把 part 重写完,或者干脆重建表。它的默认值在上游源码里是 `false`(`src/Core/Settings.cpp` 里就是 `DECLARE(Bool, apply_mutations_on_fly, false, ...)`),也就是**默认关闭**;至于它在各发行版与 Cloud 上的差异,以及它在读路径上的稳定性,**本条未确认**,用之前请以你那一版的 `system.settings` 与文档为准。

## 09.4 观察与干预 mutation

`system.mutations` 是唯一能看清 mutation 状态的地方(它为什么由后台线程池推进、和合并共用哪个池子,见第 18 章 §18.8):

```sql
SELECT database, table, mutation_id, command, create_time,
       is_done, parts_to_do, latest_fail_reason
FROM system.mutations
WHERE is_done = 0;
```

关键列就三个:

`is_done` —— 是不是全部完成。0 表示还在推进或已经失败。

`parts_to_do` —— 还有几个 part 没被改过。这是**最有价值的观测点**:它不为 0 就说明"数据还没改完"。而它由后台线程池推进,所以当你看到它不动时,正确的下一步是判断后台池是不是被别人占满了——去 `system.merges` 看有没有合并任务在跑、跑得动没有(合并与 mutation 共用同一个池子,一堆大合并会把池子占满、mutation 只能等),同时看一眼那个池的容量设置。

`latest_fail_reason` —— 最近一次失败的原因,空字符串表示没失败过。这一列非空的时候,`is_done` 一定是 0,而且**不会自动恢复**:后续的后台重写会反复重试并反复失败。这是排查"mutation 卡住"的第一现场。

`KILL MUTATION` 用来中止一条 mutation:

```sql
KILL MUTATION WHERE database = 'default' AND table = 'hits';
```

它的效果是把这条 mutation 标记为"被杀掉",之后它不再被应用到新的 part 上。`system.mutations` 里有一列 `is_killed`,可以直接按它筛——这一列不是近期才有的东西,从 20.x 起就已经存在,可以放心地写进排障脚本。

mutation 卡住的常见原因,按出现频率排:

- **一条 mutation 与后续 mutation 串行。** 同一个 part 上不能同时应用两条 mutation,所以同一张表上的 mutation 是排队的。你连发十条 `ALTER ... DELETE`,第十条要等前九条全部推完。用 `number_of_mutations_to_delay` / `number_of_mutations_to_throw` 这两个 **MergeTree 表级设置**可以给待处理队列加限:`number_of_mutations_to_delay` 默认 **500**(未完成 mutation 达到这个数就开始人为放慢)、`number_of_mutations_to_throw` 默认 **1000**(达到就抛 `Too many mutations`),都设成 0 表示关闭。
- **part 太大。** mutation 要重写的粒度是 part。一个 200 GB 的 part 上做一次单行 `UPDATE`,也要把 200 GB 重写一遍 —— 这是"列存 + 不可变文件"模型下必然的代价。
- **磁盘满。** 重写需要额外的空间(新的 part 先落盘,成功了才替换旧的)。磁盘水位不够时合并会主动停下,mutation 也就跟着停。
- **`latest_fail_reason` 里有真实错误。** 最常见的是表达式求值失败(比如除以零、类型转换失败),这类问题不改数据本身是不会自己好的。

## 09.5 MATERIALIZE:让 ALTER 新加的结构追平老 part

前面说 `ADD COLUMN` 是元数据操作、不重写 part。这条规则有一个容易漏掉的反面:**有些东西加上去之后,老 part 里是"空"的,得靠一次显式操作才能补齐。** 这类操作统一叫 `MATERIALIZE`。

最典型的是跳数索引。`ALTER TABLE t ADD INDEX idx ...` 只改元数据(§09.1 已说明),之后写入的 part 会带上索引数据,但**已经存在的 part 不会** —— 除非后续合并顺手把它们带上,或者你显式补:

```sql
ALTER TABLE t MATERIALIZE INDEX idx;                          -- 给所有老 part 补索引数据
ALTER TABLE t MATERIALIZE INDEX idx IN PARTITION '2024-03';   -- 只补一个分区
```

projection 同理:`ALTER TABLE t ADD PROJECTION p (...)` 之后,只有**在这条语句之后写入的 part** 才带投影,老 part 要用 `MATERIALIZE PROJECTION p` 补。这解释了为什么"加完 projection 查询没变快"是常见误报 —— 索引/投影的元数据有了,但老 part 里还没有数据,优化器看不到可用的投影。

`MATERIALIZE COLUMN` 是另一件事:给一个带 `DEFAULT`/`MATERIALIZED`/`ALIAS` 表达式的列,把**已有行**的值按表达式重算一遍。它是 mutation,会重写 part:

```sql
ALTER TABLE t ADD COLUMN x UInt64 MATERIALIZED a + b;
ALTER TABLE t MATERIALIZE COLUMN x;   -- 把历史行的 x 也按 a + b 算出来(走 mutation)
```

列统计也一样有 `MATERIALIZE STATISTICS`(§09.6)。

一句话记住这组:**凡是"给每个 part 存一份摘要或副本"的结构(跳数索引、projection、列统计),`ADD` 只是登记,`MATERIALIZE` 才是把摘要真正写进老 part。** 因为 `MATERIALIZE` 走的是 mutation 那条流水线,它的进度能在 `system.mutations` 里看到(§09.4)——这也从反面确认了它属于本章的"第二类"。

## 09.6 列统计与 CHECK 约束

**列统计(column statistics)** 是一个较新、默认关闭的特性:给某一列预算好摘要(分位数草图、频次草图、去重计数),让优化器在估算基数、选择执行方案时有依据。语法走 `ALTER`:

```sql
ALTER TABLE t ADD STATISTICS col TYPE tdigest;    -- 分位数草图
ALTER TABLE t ADD STATISTICS col TYPE count_min;  -- 频次草图(count-min sketch)
ALTER TABLE t ADD STATISTICS col TYPE uniq;       -- 去重计数
ALTER TABLE t MATERIALIZE STATISTICS col;         -- 给老 part 补齐(同 §09.5)
ALTER TABLE t DROP STATISTICS col;
```

它受 `allow_experimental_statistics` 控制,该开关自 24.6 引入,**默认 0** —— 不打开这条设置,`ADD STATISTICS` 会被直接拒绝(更早的写法 `allow_experimental_statistic` 是它的别名)。可用的统计类型有 `MinMax`(范围)、`Uniq`/`uniq_v2`(基数)、`TDigest`(分位)与 `CountMin`(等值选择率);但**每种类型具体在哪个版本可用,我未确认** —— 上游把统计推向 GA 是更晚的事(26.3 起主体改用 `allow_statistics`)。用之前请核对你的发行版文档,不要照搬上面的类型名建生产表的统计。

**CHECK 约束**是表定义的一部分,用来在写入时拒绝不合格的行:

```sql
ALTER TABLE t ADD CONSTRAINT positive_amount CHECK amount > 0;
ALTER TABLE t MODIFY CONSTRAINT positive_amount CHECK amount >= 0;   -- 改约束的表达式
ALTER TABLE t DROP CONSTRAINT positive_amount;
```

它有两条必须记住的性质。第一,`ALTER ... ADD CONSTRAINT` **只校验之后写入的行**,已经存在的数据不会被回溯检查 —— 想让老数据也满足约束是另一回事(得自己清洗)。第二,加约束是**元数据操作**,秒级返回、不重写 part,所以它属于本章的"第一类"。约束在 `INSERT` 时求值,不满足就整批写入失败。

## 09.7 分区级操作:最实用的一组运维语句

分区是 ClickHouse 里"可以整体搬动的最小单位",围绕它的几个 `ALTER` 是最常用的运维手法(执行时机与后台机制见第 18 章 §18.7)。

`DROP PARTITION` 删除整个分区。它比 `DELETE WHERE partition_col = x` 快得多——前者只需要把分区对应的 part 标记为删除,后者要走 mutation 重写。**能按分区删就永远按分区删。**

`DETACH PARTITION` 把分区对应的 part 目录从表的目录里**移走**、放进 `detached/`,`ATTACH PARTITION` 把它移回来;两者合起来是 ClickHouse 里少有的"**秒级数据搬运**"手法——直接操作 part 目录,不复制一个字节:

```sql
-- 把 2024-03 这个分区的数据从 a 表挪到 b 表(两张表的表结构必须完全一致)
ALTER TABLE a DETACH PARTITION '2024-03';
ALTER TABLE b ATTACH PARTITION '2024-03' FROM a;
```

`MOVE PARTITION ... TO DISK|VOLUME|TABLE` 是同一类操作的正规写法(跨卷是拷贝、跨表是改名);`REPLACE PARTITION` 用另一张表的同分区数据**原子替换**当前表的分区,常用于"重建一张表的数据再原子切回去";`FREEZE PARTITION` 给分区做快照(用硬链接,落在 `shadow/` 里),配合 `SYSTEM UNFREEZE WITH NAME '<backup>'` 收尾释放空间(§09.11)。

上面都是**分区级**;同一家族还有一组**part 级**语句,粒度细到单个 part:`ALTER TABLE t DETACH PART 'all_1_1_0'` 把**一个** part 移进 `detached/`、`ATTACH PART '<part_name>'` 移回来。注意 part 名(形如 `all_1_1_0`,含 block 编号)和分区 ID 是两套东西:`DETACH PARTITION '2024-03'` 作用于整个分区,`DETACH PART 'all_1_1_0'` 只作用一个 part——手工删坏了一个 part 目录、想从 `detached/` 里捞回来时,用的是后者。

还有一条跨机器的:`ALTER TABLE t FETCH PARTITION '2024-03' FROM '/clickhouse/tables/.../replicas/...'` —— 它按 Keeper 里的路径,把**另一个副本**上某个分区的 part 拉过来挂到本地,是"某个副本的本地 part 坏了、别的副本还有"时的救援手段(第 22 章)。

## 09.8 OPTIMIZE TABLE ... FINAL 的真实代价

`OPTIMIZE TABLE t FINAL` 把每个分区的 part **强制合并成一个**;它会等合并完成才返回,而且是一条重查询。它的各种变体(`PARTITION p`、`FINAL DEDUPLICATE [BY ...]`)与代价分析在第 18 章 §18.5,这里只说**什么时候真该用**——三种场景:

一是**导入完成后的一次性收敛**。刚用 `INSERT ... SELECT` 从一个临时表搬了一亿行、产生了几百个 part 时,跑一次把它们合成一个,后续查询会明显更快。

二是**让 `ReplacingMergeTree` 的去重立刻生效**,这样查询就不必加 `FINAL`(等后台合并自己收敛也行,只是时间不确定)。

三是**强制去重**。`OPTIMIZE TABLE t DEDUPLICATE [BY expr]` 对**所有** MergeTree 家族的表都有效(不只是 `Replacing`),会按排序键(或 `BY` 指定的表达式)把完全相同的行去掉——一次导入脚本写重了、又不想重建表时很有用。

反过来说,把它当"每天的定时任务"是反模式:它每次都把已经合并好的数据再重写一遍,IO 白烧,合并该交给后台的合并策略(第 18 章)。

另有一点必须提醒:`OPTIMIZE` 在没有事情可做时**默认静默返回**。若希望"没生效就报错"(排查"为什么我的 OPTIMIZE 没起作用"),打开 `optimize_throw_if_noop`。

## 09.9 TRUNCATE、RENAME 与 EXCHANGE TABLES

`TRUNCATE TABLE t` 清空一张表。它不产生 mutation,直接删除所有 part,所以很快;它也不可撤销。复制表上的 `TRUNCATE` 会通过 Keeper 同步到所有副本(第 22 章)。

`RENAME TABLE a TO b, c TO d` 可以一次改多张表的名字。在 `Atomic` 数据库引擎下,这组改名是**原子的**:要么全部生效,要么全不生效。这一点很有用 —— 你在改名字的时候,不会有查询看到"表已经没了但新名字还没出现"的中间态。

`EXCHANGE TABLES a AND b` 是**原子交换**两张表的名字(连带它们的数据)。这是本章里最值得记住的一个运维技巧:

```sql
-- 场景:物化视图的定义要改,但表里有数据不能丢
-- ① 建一张结构正确的新表 v2,把历史数据灌进去
-- ② 原子的那一刻:
EXCHANGE TABLES mv_old AND mv_new;
-- ③ 老表改名后可以慢慢删
```

如果没有 `EXCHANGE`,这类"重建 + 切换"要么靠 `RENAME` 两步(中间有一瞬间表名不存在),要么靠 `REPLACE PARTITION`(只对分区级别的替换有效)。`EXCHANGE TABLES` 把这一瞬间消掉了。

**这两条语句都有前置条件:先看数据库引擎。** `EXCHANGE TABLES`(以及 `RENAME TABLE` 的原子性)**只在 `Atomic` 数据库引擎上受支持**(Cloud 侧对应的 `Shared` 引擎同理);老的 `Ordinary` 引擎既不支持 `EXCHANGE TABLES`,`RENAME TABLE` 也不是原子的。用之前先确认:`SELECT name, engine FROM system.databases WHERE name = currentDatabase()`。还有一点容易误会:**`EXCHANGE TABLES` 的"原子"只覆盖一对表**——写成 `EXCHANGE TABLES a AND b, c AND d` 一次交换多对时,它是**逐对顺序执行**的,不是"要么全换、要么全不换";中途出错,前面几对已经换完了。要整体原子,就一次只交换一对。

## 09.10 补齐的表级 DDL

有几条表级 DDL 平时不常写、排障和运维却离不开,这里一并补齐(都很短)。

`DROP TABLE [IF EXISTS] t [SYNC]` 删表。加 `SYNC` 会同步等待所有副本都完成删除;复制表上默认是"登记一下、后台删"。

`DETACH TABLE t` 把一张表的元数据从服务端"摘下来",表立刻不存在、但数据目录一个字节不动;`ATTACH TABLE t` 再装回去。`DETACH TABLE t PERMANENTLY` 则让它在服务重启后**不会**自动重新挂载 —— 用于"临时下线一张表、但保留它的数据"。

`UNDROP TABLE t` 撤销最近一次 `DROP TABLE`(该语句自 23.3 起提供),依赖 `Atomic` 数据库引擎(默认引擎)。它并不是"永久回收站":普通的 `DROP` 是异步的,被删的表进 `system.dropped_tables`,数据在磁盘上保留一段**保留窗口**,窗口内可以 `UNDROP`(按名字找回最近删掉的那张,或用 `UNDROP TABLE db.t UUID '...'` 指定某一张);窗口过后 part 才被物理删除,只能靠备份。窗口长度由服务端设置 `database_atomic_delay_before_drop_table_sec` 控制,**默认 480 秒(8 分钟)**;给 `DROP` 加上 `SYNC` 会跳过这个窗口、立刻删干净,也就没有 `UNDROP` 的机会。(Cloud 上 `Shared` 库引擎用的是另一条设置 `database_shared_drop_table_delay_seconds`,窗口更长。)

`CREATE OR REPLACE TABLE t ...` 是"建表 + 替换"的一步写法,同样只在 `Atomic` 引擎下提供原子语义:表不存在就直接建,已存在就整张替换。它省掉了"先 `DROP` 再 `CREATE`"中间那个**表不存在的窗口**。

`CREATE TABLE t CLONE AS src` 是比 `AS src` 更强的一种建表来源:`AS src` 只借**结构**,`CLONE AS` 连**数据**一起复制成一张独立的新表。它和 `AS SELECT` 的差别在于**不经过查询引擎把数据重读重写一遍**,因此在 MergeTree 家族上比"建空表 + `INSERT SELECT`"快。要"把一张表整份复制出来、又不想走重灌"时用它。建表来源(`AS t1` / `AS SELECT` / `AS table_function()` / `EMPTY AS SELECT`)的完整对照在第 06 章 §06.15。CLONE 在哪些引擎上支持、依赖哪个库引擎、复制表上的副本关系怎么处理,**本书未逐条核对**,用前以你的版本文档为准。

改物化视图的 `SELECT` 也有专门的一条 `ALTER`:**`ALTER TABLE mv MODIFY QUERY SELECT ...`**。它换掉视图固化的那条查询、且不打断写入,但**只改 `SELECT`、不改视图的列定义**——所以带 `TO` 的形态要求改后的输出对得上目标表的列,不带 `TO` 的形态很受限、不能借它新增列。想改的是输出列的**结构**(增删列),仍然只能 `DROP` + 重建(第 13 章 §13.11)。

数据库一级还有几条:`RENAME DATABASE a TO b` 改库名;`ALTER DATABASE db MODIFY COMMENT '...'`(以及库级 `MODIFY SETTING`)改库的注释与设置;`DROP DATABASE [IF EXISTS] db [SYNC]` 删库。字典也能原子交换:`EXCHANGE DICTIONARIES a AND b` 与 `EXCHANGE TABLES` 同形,用在"改字典定义、但不想让查询看到半成品"的场景(第 21 章)。

最后是 `CHECK TABLE t`,校验表的 part 是否损坏(逐块比对 checksum)。它**不是** `OPTIMIZE` 的替代品 —— 只做完整性检查、不改数据;发现坏 part 之后的修复是另一套流程(第 29 章)。

## 09.11 SYSTEM 命令族

`SYSTEM` 是一组运维开关,平时用不到,出事时是救命的。下面按"你想干什么"分成几组,每组里每条只说清"什么时候用"。

**把内存里攒着的东西落盘 / 清掉缓存。** `SYSTEM FLUSH LOGS` 把内存里积攒的日志表(`query_log`、`part_log` 等)强制刷到磁盘——查 `query_log` 前先跑一次,否则最近几秒的查询还没落表。`SYSTEM FLUSH ASYNC INSERT QUEUE` 把 `async_insert` 攒着的那一批立刻落盘(第 08 章)。`SYSTEM SYNC FILE CACHE` 把文件系统缓存刷下去——迁移机器、准备断电之前用它确认数据真在盘上。清缓存一族:`SYSTEM DROP DNS CACHE`、`DROP MARK CACHE`、`DROP UNCOMPRESSED CACHE`、`DROP FILESYSTEM CACHE` 分别清掉 DNS、mark、未压缩数据与本地磁盘缓存,`DROP QUERY CACHE` 清空查询结果缓存(第 24 章);怀疑"改了配置/换了机器但旧结果被缓存住了"时用。

**让配置或元数据重新读一遍。** `SYSTEM RELOAD CONFIG` 重读服务端配置;`SYSTEM RELOAD CLUSTERS` 在 `remote_servers` 改了之后重载集群定义(第 23 章);`SYSTEM RELOAD FUNCTIONS` 重新加载可执行的用户定义函数(executable UDF),`SYSTEM RELOAD USERS` 重读用户/权限配置;字典这边是 `SYSTEM RELOAD DICTIONARIES` 重载全部(第 21 章)、`SYSTEM RELOAD DICTIONARY db.dict` 只重载一个、`SYSTEM RELOAD EMBEDDED DICTIONARIES` 重载内置字典;`SYSTEM RELOAD ASYNCHRONOUS METRICS` 在改了异步指标采集周期后强制重算一次。

**把某个后台动作单独捏在手里。** `SYSTEM STOP/START MERGES` 停/启后台合并(可只针对一张表、甚至一个分区),典型场景是"跑大批量导入、不想让合并来抢 IO";`SYSTEM STOP/START TTL MERGES` 停/启 TTL 触发的合并(可 `ON VOLUME` 限定);`SYSTEM STOP/START FETCHES` 停/启副本之间的**拉取**(fetch),用在"某台机器磁盘要满、先别拉数据过来";`SYSTEM STOP/START REPLICATED SENDS` 停/启向其它副本**发送** part,用在"下游副本跟不上、先别推"(第 22 章);`SYSTEM STOP/START VIEW db.mv` 停掉可刷新物化视图的自动刷新(第 13 章)。这一族都在排障时短暂用,别当常规手段。

**复制同步与副本修复。** `SYSTEM SYNC REPLICA db.t` 阻塞到当前副本追上复制队列——在复制表上做"先写入再查"的验证前先跑,否则可能读到旧数据;`SYSTEM RESTART REPLICA db.t` 重新初始化复制(重拉复制线程、重拉队列),但**不删除**本地数据、有问题时尝试复用,用来修"副本卡在只读、复制队列坏了";`SYSTEM RESTORE REPLICA db.t` 用于"本地还有数据、但 Keeper 里的副本元数据丢了、副本卡在只读"的情形,它尝试**用本地 part 重建**副本元数据而不是重新拉全量;`SYSTEM DROP REPLICA` 从 Keeper 里删掉某个副本的元数据,用在机器永久下线、或副本目录被手工删掉之后(第 22 章有完整流程);`SYSTEM WAIT LOADING PARTS` 阻塞到所有 part 加载完成——副本重启后 part 是后台异步加载的,你要马上查、又怕读到"还没加载完"的状态时用它;`SYSTEM SYNC DATABASE REPLICA db` 让 `Replicated` 数据库的元数据变更追平。

**分布式队列与快照收尾。** `SYSTEM FLUSH DISTRIBUTED` 强制把 `Distributed` 表的本地队列发出去,`SYSTEM STOP/START DISTRIBUTED SENDS` 暂停/恢复发送,排"数据卡在分布式表里没发出去"时用(第 23 章);`SYSTEM UNFREEZE WITH NAME '<backup>'` 是 `FREEZE PARTITION`(§09.7)的收尾,把那次快照留下的引用和元数据删掉、释放空间。

**排障专用与危险操作。** `SYSTEM RESTART DISK <name>` 重新初始化一块配置里定义的磁盘,用来处理"盘挂载点变了 / 临时不可用"——要留意:**这个命令在较长一段时间里解析器认得、执行分支却没落地**,25.3 上它是否真的生效**存疑**,用之前先在小环境上试一次。`SYSTEM JEMALLOC PURGE` 让内存分配器把空闲页还给操作系统,是 `top` 里 RSS 居高不下时的第一手验证手段。`SYSTEM ENABLE/DISABLE FAILPOINT` 是故障注入点,**只在测试环境用**。最后两个要特别小心:`SYSTEM SHUTDOWN` 是**停掉整个服务进程**——一条能把自己连的服务器关掉的语句;`SYSTEM KILL` 是它更暴烈的近亲,两者**不是等价写法**,`SHUTDOWN` 走正常关闭(相当于进程停掉之前的收尾流程),`KILL` 相当于直接 `kill -9`、不给收尾机会,后者只在进程已经不响应、连 `SHUTDOWN` 都下不去的时候才轮到它。

这份分组是按"你大概会在什么情况下想起它"组织的,不是穷举。SYSTEM 语句的数量本身也在涨,**完整名单以你所用版本的 SYSTEM 文档为准**。

## 09.12 KILL、SHOW、DESCRIBE、EXISTS 的常用组合

`KILL QUERY` 停掉正在跑的查询:

```sql
SELECT query_id, user, elapsed, read_rows, query FROM system.processes;
KILL QUERY WHERE query_id = 'xxxx';          -- 默认 ASYNC:发出取消信号就返回
KILL QUERY WHERE user = 'etl' SYNC;          -- 等它真的停,逐条回报 kill_status
KILL QUERY WHERE user = 'etl' TEST;          -- 只列出会被杀的查询,不真杀
```

这里有一个容易记反的地方:**不写模式时默认是 `ASYNC`,不是 `SYNC`。** 所以敲完 `KILL QUERY` 立刻去 `system.processes` 里看,那条查询还在是正常的——它只是收到了取消信号,还要等执行线程跑到自己的检查点才会退出。要做"确认已经停掉再往下走"的脚本,必须显式写 `SYNC` 并检查返回的 `kill_status`。取消的协作语义、`kill_status` 的取值、以及"为什么杀了没反应"的解释,见第 25 章 §25.6。

定位"是哪条查询把机器拖死了",永远是先看 `system.processes`,再决定 kill 谁。`KILL MUTATION` 见 §09.4。

`SHOW` 系列**大部分**是 `system` 表的薄封装——`SHOW DATABASES`、`SHOW TABLES`、`SHOW COLUMNS`、`SHOW GRANTS` 都能在对应的 `system.*` 表上找到等价查询,所以能像查 `system` 表一样加 `LIKE`、`WHERE` 之类的过滤(第 27 章)。但**并非全部**:`SHOW CREATE TABLE` 是把表定义重新渲染成一条建表语句(不对应任何单张 `system` 表),`SHOW PROCESSLIST` 与 `system.processes` 内容同源、却仍是一条独立语句。所以"`SHOW` 全是 `system` 表的查询"只是一句方便的简化,不是字面事实:

```sql
SHOW DATABASES;
SHOW TABLES FROM default LIKE 'hits%';
SHOW CREATE TABLE hits;                 -- 拿到完整建表语句(含引擎参数)
SHOW COLUMNS FROM hits;                 -- = DESCRIBE hits
SHOW PROCESSLIST;
SHOW GRANTS FOR CURRENT_USER;
```

`DESCRIBE TABLE t` 是查表结构的主力;它还有一个不太为人所知的开关:

```sql
DESCRIBE TABLE hits SETTINGS describe_include_subcolumns = 1;
```

打开后,`Array`、`Tuple`、`Map` 这类复合类型的**子列**也会被列出来(比如 `arr.size`、`map.keys`)。排查"为什么某个子列读起来很慢"时非常有用(第 03 章)。

`EXISTS` 用来在脚本里做条件判断:

```sql
EXISTS TABLE default.hits;      -- 返回 0/1
EXISTS DATABASE default;
```

配合 `CREATE TABLE IF NOT EXISTS` / `DROP TABLE IF EXISTS`,可以让幂等的建表脚本写得很干净。

## 09.13 事务:`BEGIN TRANSACTION` / `COMMIT` / `ROLLBACK` 的边界

ClickHouse 支持**事务语法**——`BEGIN TRANSACTION`、`COMMIT`、`ROLLBACK`,但它们描述的能力和 MySQL/PostgreSQL 那种 OLTP 事务**不是一回事**,用错了会得到一个巨大的反直觉。这一节只讲清它的边界。

先说清楚 ClickHouse 默认提供的"原子性"是什么。前面几章反复强调:一次 `INSERT` 是**按 part/块原子的**——一个块要么整体变成一个可见的 part、要么整体不可见(靠 §08.2 的"写临时目录 + 原子改名")。但这只是**单条写入的原子性**:它不覆盖"多条语句要么全成功、要么全失败",也没有跨语句的**隔离**(并发查询看不到半完成的中间态,靠的是 part 不可变,而不是 MVCC)。换句话说,默认的 ClickHouse 里并没有"事务"这个对象,只有"一次写入是原子的"。

`BEGIN TRANSACTION` 就是在这个基础上加的一层**实验性**外壳:把若干条语句圈在一起,最后 `COMMIT`(一起生效)或 `ROLLBACK`(一起丢弃)。但它的能力边界很窄:

- **能进事务的语句非常有限。** 主要是 `INSERT`、`SELECT` 这类**数据语句**;**DDL(`CREATE`/`ALTER`/`DROP`)不在事务里**——在事务中间执行 DDL 会直接报错。
- **只面向一部分表引擎。** 复制表(`ReplicatedMergeTree`)**不支持**事务;它依赖非复制的 MergeTree 家族,以及 `Atomic` 库与 Keeper。
- **没有嵌套事务**;而且事务内一旦有语句抛错,整个事务就"中毒"了——后续语句只会继续报错,只能 `ROLLBACK`。
- **它不提供 OLTP 意义上的隔离与并发控制。** 没有行级锁、没有多版本读一致性;它保证的是"这组操作最终一起提交或一起放弃",而不是"并发事务之间互不可见"。

**为什么它不是 OLTP 事务?** 因为 ClickHouse 的存储模型(不可变 part + 列式 + 后台合并)本来就是为**批量分析**优化的:它的并发建立在"读到的 part 不会被就地改"之上,而不是"多版本 + 锁"之上。硬要在它上面模拟频繁的小事务和行级更新,只会撞上 §09.3 里 mutation 的全部代价。要真正的事务性负载,该用行存数据库。

**怎么开启。** 它是**实验特性、默认关闭**;不打开就写 `BEGIN TRANSACTION` 会报"不支持事务"的错误(`Code 48`,NOT_IMPLEMENTED)。开启要改**服务端**配置——开关名是 `allow_experimental_transactions`(写进 `config.xml` 或 `config.d/*.xml` 的根节点下、设为非 0 即启用,默认关闭;它是服务端配置项,不是普通用户设置),而且还要有 Keeper/ZooKeeper、`Atomic` 库引擎与非复制表配合。一句话:**把事务当成"实验性的多语句批处理",而不是"ClickHouse 也能 OLTP"。**

## 09.14 与 mini-clickhouse 的对照

mini-clickhouse 把 `ALTER` 的**接口**做得与真实 ClickHouse 一致,但**实现深度**差别很大,正好对照"哪些是元数据、哪些要动数据"。本节分"对齐点"与"简化点"两部分。

**对齐点。** 解释器侧是 `minich/execution/interpreter.py` 里的 `InterpreterAlterQuery`,它只做一件事:按 `cmd.kind` 分派给存储引擎的方法。这个分派表本身就是一份"ALTER 分类表":

| `cmd.kind` | 调用的引擎方法 | 性质 |
|---|---|---|
| `ADD_COLUMN` | `engine.alter_add_column(col, after)` | 元数据 |
| `DROP_COLUMN` | `engine.alter_drop_column(name)` | 元数据 |
| `RENAME_COLUMN` | `engine.alter_rename_column(old, new)` | 元数据 |
| `COMMENT_COLUMN` | 直接改 `col.comment` | 元数据 |
| `MODIFY_SETTING` / `RESET_SETTING` | `engine.modify_setting(...)` | 元数据 |
| `MODIFY_ORDER_BY` | `engine.modify_order_by(expr)` | 元数据(受限) |
| `MODIFY_TTL` | `engine.modify_ttl(ttl)` | 元数据(影响后续合并) |
| `MODIFY_COLUMN` | `engine.alter_modify_column(...)` | **重写数据** |
| `UPDATE` / `DELETE` | `engine.mutate(MutationCommand)` | **重写数据** |
| `ADD_INDEX` / `DROP_INDEX` / `MATERIALIZE_INDEX` | **显式抛 `NOT_IMPLEMENTED`(Code 48)** | 未实现(跳数索引只能在 `CREATE TABLE` 里声明) |
| `DROP_PARTITION` | `engine.drop_partition(value)` | 分区级 |
| `START_MERGES` / `STOP_MERGES` | `engine.start_merges()` / `stop_merges()` | 开关 |

"元数据操作不重写数据"这一条在基类里写得很明确:`IStorage.alter_add_column`(`minich/storages/base.py`)只在 `self.table.columns` 上追加一个 `ColumnDefinition`,而它旁边的 `_reorder_column` 文档直接写道 —— "真实 ClickHouse 里列顺序体现在 `columns.txt` 里,而**已有 part 的物理列顺序不变**(新列只在读的时候补默认值)",与本章 9.2 节讲的机制完全一致。

mutation 那一侧在 `minich/storages/merge_tree/mutations.py`,这个文件的模块文档把"为什么不做原地改"讲得很透。实现上有三个类:`MutationCommand`(一条 UPDATE/DELETE 命令,含谓词与赋值列表)、`MutationEntry`(一条 mutation 记录)、`MutationRegistry`(一张表的 mutation 清单)。`MutationEntry.status_row()` 生成的行**就是** `system.mutations` 的一行,列名与真实 ClickHouse 对齐:`mutation_id`、`command`、`create_time`、`block_numbers`、`parts_to_do`、`parts_to_do_names`、`is_done`、`latest_failed_part`、`latest_fail_time`、`latest_fail_reason`。mutation_id 的格式也是零填充到 10 位(`0000000001`),文档里解释了原因:字符串排序要等于数值排序,否则客户端取"最新一条"会取错。

**这正是 mini 相对真实 ClickHouse 的简化点**:真实 ClickHouse 里 mutation 有一条由后台池空闲线程主动挑任务执行的独立路径(§09.3),mini 则把 mutation 挂在合并流水线上、只在 part 参与合并或 `OPTIMIZE` 时顺带推进。所以真正推进 mutation 的逻辑落在同一个包的 `merger.py` 里 —— mutation 和合并走**同一条流水线**:归并排序的时候顺手把谓词套用一遍,产出已改过的新 part。这正是 9.3 节那张图的代码形态。mutation 池的两个参数也照搬了真实名字,放在 `merge_tree.py` 的 `DEFAULT_SETTINGS` 里:`number_of_free_entries_in_pool_to_execute_mutation`(默认 20)与 `finished_mutations_to_keep`(默认 100)。

各条 `ALTER` 的真实实现都在 `minich/storages/merge_tree/merge_tree.py` 的 `MergeTreeData` 上,有几处细节正好印证本章的观点:

- `alter_add_column` 的文档直接写着"**纯元数据操作,不重写任何 part**",并点明机制:旧 part 里没有这一列,靠 `MergeTreePart.missing_columns` 记着"表里有、part 里没有"的列,读的时候补零值。它还补了一句很实用的建议 —— 真实 ClickHouse 推荐加 **`MATERIALIZED`** 列而不是普通列,因为物化列读时按表达式算,能给出"当时应该有的值",而普通列只能给默认值。
- `alter_drop_column` 会**拒绝**删除参与排序键的列,报 `ILLEGAL_COLUMN`。
- `alter_modify_column` 在 mini 里**只改元数据**,老 part 的数据不做转换;文档里明确标注这是与真实的差异 —— 真实 ClickHouse 会为此跑一次 mutation 把数据转换过去。这也从反面说明:改类型属于本章的"第二类"。
- `alter_rename_column` 为了读路径简单,顺手把 part 里列的 `.bin` / `.mrk2` **物理改名**;真实 ClickHouse 不这么做,它靠 `columns.txt` 与实际文件名的映射来读,所以重命名是纯元数据操作。
- `modify_order_by` 在 mini 里比 §09.2 那条官方规则**更严**:它只允许在**空表**上改排序键,一旦表里已有 part 就抛 `NOT_IMPLEMENTED`(理由写在方法 docstring 里:已有 part 按旧顺序排好,继续用旧顺序读会让新的 `primary.idx` 与数据不符、裁剪给出错误结果)。官方"只追加同一条 `ALTER` 里 `ADD COLUMN` 新增的列"那种细粒度规则,mini 没有实现 —— 想改就重建表、重新灌数据。

**简化点。** mini 没有实现、或只实现了一部分的那些:

- **分区级操作在引擎侧做了一半,SQL 路径也只开了一部分。** `MergeTreeData` 上有 `drop_partition`、`drop_part`、`detach_partition`、`attach_partition`、`freeze`;`detach_partition` 就是 `disk.move(f"{data_path}/{part}", f"{data_path}/detached/{part}")`,`attach_partition` 反过来从 `detached/` 里按分区 ID 挑出 part 移回来并重新登记(§09.7 讲的"操作目录、不复制字节")。但 SQL 入口 `InterpreterAlterQuery._apply` 只接了 `DROP_PARTITION` 与 `FREEZE`,`DETACH_PARTITION` / `ATTACH_PARTITION` / `DROP_PART` / `MOVE_PARTITION` 都会抛 `NOT_IMPLEMENTED`,`REPLACE PARTITION` 则连解析都过不去(报 `Unsupported ALTER command: 'REPLACE'`)。
- **`freeze` 是真的:它会在 `shadow/<backup>/` 下建出 part 的一份拷贝。** 与真实 ClickHouse 的唯一差别是**用"拷贝"代替"硬链接"**(硬链接在 Windows 上需要管理员权限、跨平台行为也不一致),代价是快照耗时与数据量成正比、并占额外磁盘(真实 CH 是 O(part 数) 的硬链接);而"快照生成后源 part 再变也不影响快照"这条语义完全一样。
- **`MODIFY SETTING` 会校验键名,且不是 `DEFAULT_SETTINGS` 里的每一项都被接受。** `MergeTreeData.modify_setting` 先用 `DEFAULT_SETTINGS` 对照键名,不认识的直接抛 `UNKNOWN_SETTING`;认识的键里还有一批被明确拒绝(`_KNOWN_BUT_IGNORED_SETTINGS` 里的 `background_pool_size`、`number_of_free_entries_in_pool_to_execute_mutation`、`merge_with_ttl_timeout`、`min_insert_block_size_rows`/`min_insert_block_size_bytes`、`old_parts_lifetime`、`finished_mutations_to_keep`、`optimize_read_in_order` 抛 `NOT_IMPLEMENTED`,理由是 mini 没有对应子系统,接受它们等于"沉默的谎言")。所以真正能被改的表级 setting 是 `DEFAULT_SETTINGS`(共 24 项)**减去**这 8 项剩下的那一批,其中 `index_granularity` 8192、`parts_to_delay_insert` 1000、`parts_to_throw_insert` 3000 与真实默认值一致。
- **`CLEAR COLUMN` 也抛 `NOT_IMPLEMENTED`**(它没有出现在分派表里)。
- **`MATERIALIZE` 系列、`ADD/DROP STATISTICS`、`ADD/DROP CONSTRAINT` 都没有实现**:`MATERIALIZE INDEX` / `MATERIALIZE PROJECTION` 在分派表里直接抛 `NOT_IMPLEMENTED`(跳数索引本来就只能在 `CREATE TABLE` 里声明);`MATERIALIZE COLUMN`、`ADD/DROP STATISTICS`、`ADD/DROP CONSTRAINT` 既没有解析、也没有引擎方法,写下去就是语法错误。§09.10 那批表级 DDL 同理:`UNDROP TABLE`、`EXCHANGE DICTIONARIES`、`CHECK TABLE` 都不在 mini 的语句集里——它们全都依赖"老 part 需要被追平/被校验"这类机制,而 mini 的 part 既不可变、也不做摘要补齐。
- **事务(§09.13)没有实现**:mini 里 `BEGIN TRANSACTION` / `COMMIT` / `ROLLBACK` 不进语法。
- **`SYSTEM` 命令是空壳。** `InterpreterSystemQuery` 只认 `FLUSH LOGS`、`RELOAD DICTIONARIES`、`STOP/START MERGES`;`SYNC REPLICA` / `RESTART REPLICA` / `DROP REPLICA` 被解析到之后**直接返回成功**,不做任何事(因为 mini 的复制是单机的,见 `minich/replication.py`)。**`KILL QUERY` 是空操作**(mini 是单线程执行,不存在"取消另一条查询")。**`OPTIMIZE` 的分工则与真实一致**:`InterpreterOptimizeQuery` 把 `final`、`deduplicate`、`partition` 三个参数原样传给 `engine.optimize()`,由引擎决定怎么合并。
- **分布式 DDL(`ON CLUSTER`)没有实现(§09.15)。** mini 没有集群拓扑:`CREATE ... ON CLUSTER c` 里的 `ON CLUSTER c` 只被**解析并回打**成一个字段(`ast_.py` 的 `self.cluster`、`parser.py` 里 `parse_identifier` 收下集群名),执行侧不消费它、也不广播到任何节点;`system.distributed_ddl_queue` 这张表在 mini 里不存在。所以 §09.15 讲的"发起者 + Keeper 队列"那套在 mini 上完全没有对应物。

## 09.15 分布式 DDL:`ON CLUSTER` 与 system.distributed_ddl_queue

前面所有 DDL 都默认"只在一个节点上生效"。集群里要让一条 `CREATE`/`ALTER`/`DROP`/`RENAME` 在**每个节点上各执行一次**,靠的是 `ON CLUSTER`:

```sql
CREATE TABLE hits ON CLUSTER my_cluster (...) ENGINE = ReplicatedMergeTree(...) ORDER BY id;
ALTER TABLE hits ON CLUSTER my_cluster ADD COLUMN country String;
DROP TABLE hits ON CLUSTER my_cluster SYNC;
```

`ON CLUSTER cluster_name` 几乎能挂在任何 DDL 语句后,`cluster_name` 是 `remote_servers` 里的一个集群名(建表时的完整用法见第 06 章 §06.14)。它的执行模型是**"一个发起者 + 一条 Keeper 上的任务队列"**,而不是"执行一次、全局生效":发起节点把整条 DDL 写进 Keeper 的约定路径,每个节点各自从队列里取出来、在本地执行一遍。这条队列的机制是第 23 章 §23.4 的主题,本节只补它对本章"ALTER 分两类"的两层影响。

**第一层:等待有上限,超时不等于取消。** 发起节点按查询设置 `distributed_ddl_task_timeout`(默认 180 秒)等各节点回报;超时后语句返回"未全部完成",但**队列里的任务仍会被各节点继续执行**,只是你的客户端不等了。没有一条独立的"等待 DDL"语句,想知道某次集群 DDL 到底成没成,只能轮询 `system.distributed_ddl_queue`(逐列判读见第 27 章 §27.11):

```sql
SELECT entry, host, status, exception_text
FROM system.distributed_ddl_queue
WHERE status != 'Finished'
LIMIT 10;
```

`status` 里出现非 `Finished` 的项、或 `exception_text` 非空,就说明**有的节点成功、有的失败**——分布式 DDL 最常见的情形,得按 `host` 逐个去修。它也不承诺原子性:各节点各执行一次,中途某个节点报错,前面已经执行过的节点不会回滚。

**第二层:"返回即完成"在集群上更不成立。** 一条 `ALTER TABLE t ON CLUSTER c DELETE WHERE ...` 会在**每台机器上各登记一条 mutation**,于是某个节点的 `system.mutations` 只反映它自己那一份;要判断"整个集群改完了没有",得把每个节点的 `system.mutations` 都看一遍,或者靠 `mutations_sync = 2`(§09.3)在复制组内等到位。同理,`ALTER TABLE ... ADD INDEX ON CLUSTER` 的元数据是各节点各写一份,索引数据仍要各自 `MATERIALIZE`(§09.5)。

**两条并行的路径,别混。** `ON CLUSTER` 走上面这条 **DDL 队列**;而 `Replicated` 数据库引擎走**库引擎本身**——用 `Replicated` 库时,建表**不写 `ON CLUSTER` 也会自动扩散到该库的所有副本**(第 22 章 §22.9)。两者不是替代关系,一个集群可以同时具备。

区分到最后只剩一句:**`ON CLUSTER` 是"每个节点各跑一遍",任何一个节点卡住,整条语句对你的客户端就卡住**;它给的是"不用在十台机器上敲十次"的便利,不是跨节点的原子性。队列容量(`pool_size` 天生把 `ON CLUSTER` 排成串行、`max_tasks_in_queue` 等)与 `distributed_ddl_output_mode` 的汇报口径,都在第 23 章 §23.4。

## 09.16 小结

- `ALTER` 必须分成两类:**元数据操作**(秒级、返回即完成)与**数据重写操作**(异步、返回只是排队)。判断标准是"会不会改变已有数据的值、类型或物理布局"。
- `ADD COLUMN` 不重写 part —— 老 part 读出来时补默认值;没写 `DEFAULT` 就补类型的默认值(`0` / `''` / `1970-01-01`)。`MODIFY ORDER BY` 只能追加**同一条 `ALTER` 里 `ADD COLUMN` 新增的列**(旧排序键必须保持前缀),其余一概拒绝。
- mutation 不是就地改,而是**重写受影响的 part**,并且**由后台线程池排队推进**(与合并共用同一个池子,但不必等某个具体的合并;mini-clickhouse 里才是"靠合并推进"的简化路径)。所以"`ALTER ... DELETE` 返回了"不等于"数据删完了"。改数据其实分三类,别一律叫 mutation:通过 `ALTER` 的**重量级 mutation**(重写 part)、不带 `ALTER` 的**轻量级 delete**(`DELETE FROM`,`_row_exists` 掩码、不重写 part、删掉的行立刻从查询里消失)、以及更晚才有的**轻量级 update**(patch part,25.8 才进入 Beta,本书对标的 25.3 上还没有)。想让尚未落盘的 mutation 立刻可见,可用 `apply_mutations_on_fly`(读时现算,上游源码里默认 `false`,即默认关闭)。
- **事务**(`BEGIN TRANSACTION` / `COMMIT` / `ROLLBACK`)是**实验特性、默认关闭**:它只圈 `INSERT`/`SELECT` 这类数据语句,不支持复制表与 DDL、无嵌套、出错即"中毒",而且只提供"一起提交或一起放弃",不提供 OLTP 的隔离与并发控制。它加在"单次写入按 part 原子"之上——**别把 ClickHouse 当 OLTP**。开启它的服务端开关是 `allow_experimental_transactions`(写进 `config.xml`,默认关闭)。
- 用 `system.mutations` 的 `is_done` / `parts_to_do` / `latest_fail_reason` 三列观察;`KILL MUTATION` 中止;卡住的常见原因是串行排队、part 太大、磁盘满。
- 分区级操作是最实用的运维手法:`DROP PARTITION` 远比 `DELETE WHERE 分区列` 便宜;`DETACH` + `ATTACH ... FROM` 是秒级的跨表数据搬运。
- `OPTIMIZE ... FINAL` 会真的重写数据,只在"导入后收敛""让去重立即生效""强制 `DEDUPLICATE`"这三种场景用。
- `EXCHANGE TABLES` 提供原子的表切换,是"重建物化视图 / 重建宽表后无缝上线"的标准手法。
- 跳数索引、projection、列统计这三类"按 part 存摘要"的结构,`ADD` 只改元数据、只对**之后写入的 part** 生效;要把摘要补进老 part,必须跑一次 `MATERIALIZE`(它属于"第二类",能在 `system.mutations` 里看到进度)。`MATERIALIZE COLUMN` 则重算带表达式的列的历史值。
- 列统计(`ADD STATISTICS` / `MATERIALIZE STATISTICS`)默认关闭(`allow_experimental_statistics = 0`);`ADD CONSTRAINT` 加的是写入时的校验,只约束**之后**写入的行、不回溯检查老数据。
- 表级 DDL 不难但容易漏:`DROP`/`DETACH`/`ATTACH`/`UNDROP`/`CREATE OR REPLACE TABLE`、`CREATE TABLE ... CLONE AS`(连数据一起复制)、`ALTER TABLE mv MODIFY QUERY`(换物化视图的 `SELECT`,不改列定义)、`RENAME`/`ALTER`/`DROP DATABASE`、`EXCHANGE DICTIONARIES`、`CHECK TABLE` —— `UNDROP` 与 `CREATE OR REPLACE` 的原子性都依赖 `Atomic` 数据库引擎。
- **集群里的 DDL(§09.15)** 用 `ON CLUSTER`:它是"发起者 + 一条 Keeper DDL 队列",**每个节点各执行一次、非原子**;等待在 `distributed_ddl_task_timeout`(默认 180 秒)后超时但**任务不取消**,进度只能轮询 `system.distributed_ddl_queue`;`Replicated` 数据库引擎是另一条路径(库引擎自己传播,不必写 `ON CLUSTER`)。一条 `... ON CLUSTER` 的 `ALTER ... DELETE` 会在每台机器上各生成一条 mutation。
- mini-clickhouse 的 `InterpreterAlterQuery` 分派表覆盖了元数据类操作与 mutation;分区搬运在引擎侧只做了一半(detach/attach/freeze,其中 `freeze` 用拷贝代替硬链接、真的会建快照),SQL 路径开放了 `DROP PARTITION` 与 `FREEZE`,`CLEAR COLUMN` 与 `MOVE` / `REPLACE PARTITION` 没实现,大部分 `SYSTEM` 命令和 `KILL QUERY` 是空壳 —— 这些差异本身就是"哪些能力依赖真实的 part 目录管理与后台线程"的一份清单。
