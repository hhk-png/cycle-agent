# 第 16 章 排序、窗口函数与 LIMIT BY

排序是查询里最"贵"的一步:它必须看到全部输入之后才可能输出第一行,所以内存峰值等于整个结果集。窗口函数和 `DISTINCT` 站在排序的肩膀上,代价只高不低。而 `LIMIT BY` 恰恰相反,它是"每组取前 N 行"的专用算子,单趟、近乎零额外内存 —— 很多人用窗口函数写它,其实是花了一个数量级的代价去买同一份结果。这一章把这三件事的代价和语义讲清楚,重点是最容易踩的两个坑:`NULL` 的位置和 `lagInFrame` 这个名字。

## 16.1 ORDER BY 是阻塞算子

"阻塞算子"(blocking operator)的意思是:**在读完最后一行之前,它一行都输出不了**。`ORDER BY` 就是典型:

- 内存峰值 = 整个结果集(排序键加上所有输出列),不是"一行";
- 没有流式体验:`SELECT ... ORDER BY x` 拿不到"第一行很快"的感觉;
- 除非数据本来就按排序键有序(§16.4),否则全量排序躲不掉。

落盘逃生门:

```text
max_bytes_before_external_sort         默认 0(不落盘)
max_bytes_ratio_before_external_sort   按「可用内存的比例」触发溢出
```

把绝对阈值设成比如 10 GB,排序数据超过这个量就溢写到磁盘做外部排序(排序片段各自有序,最后归并)。它能救回大排序,但代价是磁盘 I/O 和一轮归并;经验上设成可用内存的一半左右。和聚合侧一样,这里也有一对:绝对阈值的问题是“换台机器就得重设”,于是另有一个**按比例**的 `max_bytes_ratio_before_external_sort`,把触发点定义成“可用内存的某个百分比”、随实例内存自动缩放,一套配置在大小机器上都说得过去。绝对值与比例值一般二选一(与聚合侧的 `max_bytes_before_external_group_by` / `max_bytes_ratio_before_external_group_by` 同理,第 12 章 §12.4);它的引入版本与默认值我**未确认**,请以 `SELECT name, value, default FROM system.settings WHERE name LIKE 'max_bytes_ratio_before_external_sort'` 的实际输出为准。

有一条容易被误读的性质:`ORDER BY ... LIMIT n` 比裸 `ORDER BY` 便宜,但**便宜的是"要保留多少行",不是"要看多少行"** —— 排序仍然要看到全部输入,只是不需要把完整结果集留在内存里。所以 `ORDER BY ts LIMIT 10` 在 10 亿行的表上依然是一次全量排序(除非命中§16.4 的读时有序)。

## 16.2 NULL 的位置规则:它被当成最大值

ClickHouse 把 `NULL` 当作**最大的值**来排序。于是:

```text
ORDER BY x ASC    NULL 排在最后
ORDER BY x DESC   NULL 排在最前
```

这是"NULL 是最大值"这一条规则的直接推论,不是两套规则。要精确控制就显式写:

```sql
ORDER BY x ASC NULLS FIRST
ORDER BY x DESC NULLS LAST
```

看一个能看出差别的例子。数据 `(ts, v)`:

```text
(1, 10)  (2, NULL)  (3, 30)
```

```text
ORDER BY v ASC                        → 10, 30, NULL
ORDER BY v ASC NULLS FIRST            → NULL, 10, 30
ORDER BY v DESC                       → NULL, 30, 10
ORDER BY v DESC NULLS LAST            → 30, 10, NULL
```

**这里最常见的坑是“升序查询里 NULL 出现在最后,于是被分页切掉了”**:报表按 `ORDER BY score ASC` 取前 1000 行,NULL 分数的记录永远看不到。**正确做法是显式写 `NULLS FIRST`/`NULLS LAST`**,别依赖默认。

还有一条容易被跳过的前提:上面这套规则**只在“`Nullable` 列真的能进排序键”时才有意义**,而默认情况下 `Nullable` **不能**进排序键——`allow_nullable_key` 默认 `0`,写 `ORDER BY nullable_col` 会直接报错(第 03 章 §03.9)。要放进排序键,得显式 `SETTINGS allow_nullable_key = 1`,或在排序列上先用 `coalesce`/`ifNull` 把 NULL 消掉。真放进去了,`MergeTree` 的主键/索引里的比较规则与 `ORDER BY` 一致(NULL 仍按最大值比较,第 05 章),所以“默认值 vs NULL”的区分在存储层同样重要——但前提是这张表已经被允许带 NULL 排序列。

## 16.3 COLLATE:按语言习惯排序,以及它为什么只该出现在最后一步

`COLLATE` 用来做**按语言习惯**的字符串排序(而不是按字节序):

```sql
SELECT name FROM users ORDER BY name COLLATE 'tr';   -- 土耳其语:区分 i / ı
```

它挂在 `ORDER BY` 的表达式上,把该列的比较规则从"逐字节"换成指定的**排序规则(collation)**。排序规则由 ICU 提供,所以可用列表取决于你的构建是否带 libicu,名字是 `'tr'` 这类短 locale 码。

几条必须记住的限制:

- **只对字符串有意义,也只作用于"排序"这一件事**。它是 `ORDER BY` 上下文里的修饰;`=`、`GROUP BY`、`JOIN` 的**相等性判断**用的是另一套规则。
- 由此带来一个反直觉但重要的后果:**两个在 `COLLATE` 下"相等"的字符串,仍然是两行不同的数据** —— `COLLATE` 只改变谁排前谁排后,不把它们合并。别指望拿它做"大小写/变音归一化去重",那是 `lower`/`normalizeUTF8` 这类函数的活。
- 它是 `ORDER BY` 专属:写进别的上下文、或指望它参与主键比较、参与 `MergeTree` 的排序键,都不成立。
- **有性能代价**:ICU 比较比逐字节 `memcmp` 慢,而且它会**打断"读时有序"的优化**(§16.4)—— 表的物理顺序是按字节排的,一旦要求按 `COLLATE` 排,引擎就没法再靠"数据本来就排好"免掉排序,只能退回全量排序。

**可用清单从哪来**:ClickHouse 的排序规则直接沿用 **ICU** 的 locale,清单就在系统表 `system.collations` 里(列是 `name` 与 `language`,`name` 大小写不敏感),`SELECT * FROM system.collations` 就能列全。它的**适用范围很窄,只有一处**:`ORDER BY ... COLLATE '...'`,也就是只影响字符串的**排序**;相等性判断、`GROUP BY`、`JOIN`、主键比较都不受它影响(原因见上一条)。

工程建议:排序规则属于**展示层**(产品需求里的"按拼音排""按土耳其语排")的事,尽量在应用侧做;如果一定要在 SQL 里做,就把它**限定在最后一步的 `ORDER BY`** 上,不要让它影响任何中间结果的等价性判断(`DISTINCT`、`GROUP BY` 的分组)—— 原因见上:它根本不改变相等性,放错地方只会误导。

## 16.4 读时有序:什么时候排序几乎不花钱

如果查询的 `ORDER BY` 正好是表排序键(sorting key)的**前缀**,而且过滤条件能用上主键,ClickHouse 可以**按主键顺序读**、**跳过一次显式排序**:

```text
optimize_read_in_order                        默认 1
optimize_sorting_by_input_stream_properties   默认 1
```

判据(全部满足才有收益):

- 表的 `ORDER BY`(排序键)是 `(a, b, c)`,而查询的 `ORDER BY` 是 `a`、`(a, b)` 或 `(a, b, c)` 这样的**前缀**;
- 方向一致:查询用 `DESC` 时,只有在排序键也允许反向扫描时才成立;
- 过滤条件能形成连续的主键区间(`WHERE a = ... AND b > ...`),否则读出来的顺序会被过滤打乱;
- 这条路径不能和大范围聚合/`GROUP BY` 冲突(聚合会重排数据)。

**这就是"按排序键排序几乎不花钱、随便排要全量排序"的全部原因**:前者是"读的时候顺便就是有序的",后者必须把全部数据搬进内存再排一遍。

实用推论:**把最常用来排序/分页的那一两个列放在排序键最左边**。一个“按 `ts` 倒序翻页的日志表”,排序键就该以 `ts` 开头 —— 这一条设计决定比任何查询调优都值钱(第 05 章)。

## 16.5 ORDER BY ... WITH FILL 与 INTERPOLATE:把时间轴补齐

报表场景里最常见的抱怨不是"查不出来",而是结果**缺行**:按天统计,某天没有数据,那一天就整个不出现,前端折线图直接断掉。SQL 标准没有解药,通常得在应用侧补点。ClickHouse 的 `ORDER BY ... WITH FILL` 就是把这件"补齐"搬进查询。这一族"扩展版 `ORDER BY`/`LIMIT`"的基础写法(`WITH FILL` 与 `LIMIT ... WITH TIES`)在第 07 章 §07.9 点到过,这里讲**完整行为**:

```sql
SELECT toStartOfDay(ts) AS day, count() AS n
FROM events
GROUP BY day
ORDER BY day WITH FILL
    FROM toStartOfDay(now()) - INTERVAL 30 DAY
    TO   toStartOfDay(now())
    STEP INTERVAL 1 DAY;
```

逐个子句:

- **`WITH FILL`** 让 `ORDER BY` 在排序键的**缺口**里自动补行。补出来的行,没被填的列是**类型默认值**(数值 `0`、字符串空串),不是 `NULL`。
- **`FROM ... TO ... STEP ...`** 指定填充范围与步长,`STEP` 默认 1(`INTERVAL` 也可以)。三者都能省略,但省略 `FROM` 就从最小值开始、省略 `TO` 就到最大值为止 —— **两端不写就不会向外扩**,所以要一条固定的 30 天轴,`FROM`/`TO` 必须显式给。
- **`STALENESS n`** 是允许的最长连续填充长度:缺口一旦超过 n 个步长就停止填充。它用来避免"中间断了一年的数据,却给你补出一年的空点"。它是 **24.11** 才引入的较新子句,而且有一个硬限制:**`STALENESS` 不能与 `WITH FILL ... FROM` 同时使用** —— 官方明确二者互斥,同一个 `ORDER BY` 列上写了 `FROM` 就不能再写 `STALENESS`(所以上面那个"固定 30 天轴"的例子特意没带 `STALENESS`;要 `STALENESS`,就得去掉 `FROM`、从最小实际值开始填)。
- **`INTERPOLATE`** 让补出来的行里**指定列按一个表达式取值**,而不是填类型默认值:`INTERPOLATE (col AS expr)`。表达式里可以用填充列(`WITH FILL` 作用的那一列)以及被填列的**生成值**,例如官方示例里的 `INTERPOLATE (inter AS inter + 1)` —— 被填的 `inter` 列取"它的默认值 + 1"。要清楚它**不负责前向填充**(沿用上一个真实值):那件事仍要靠窗口函数先算好,`INTERPOLATE` 只解决"补出来的行该显示什么数"。

三条容易忘的边界:补出来的行是**查询末端追加**的,不参与 `GROUP BY`、也不改变聚合结果;`WITH FILL` 只作用在 `ORDER BY` 的**列**上,多列排序要逐列写;`INTERPOLATE` 的表达式对**每一个**填出来的行都求值。

`WITH FILL` 常常和窗口函数配合:`WITH FILL` 先把时间轴补齐,窗口函数再在**完整的**轴上算滑动指标 —— 否则"第一个真实数据点之前那段空窗口"根本不存在,移动平均会从第一个点才开始。

## 16.6 LIMIT、OFFSET、LIMIT BY 与 LIMIT ... WITH TIES

`LIMIT BY` 的**语法与基础语义**在第 07 章 §07.8 已经讲过(那一节是它的语法主场),这里只补它**与窗口函数的代价差别**。四种限制写法的形状:

```sql
SELECT ... LIMIT 10;                  -- 前 10 行
SELECT ... LIMIT 20, 10;              -- 跳过 20 行,取 10 行(等价于 OFFSET 20 LIMIT 10)
SELECT ... LIMIT 10 WITH TIES;        -- 第 10 名并列的行一并带上
SELECT ... LIMIT 3 BY user_id;        -- 每个 user_id 取前 3 行
SELECT ... LIMIT 2, 3 BY user_id;     -- 每个 user_id 跳过 2 行再取 3 行
```

`LIMIT ... WITH TIES` 是给排行榜用的:如果结果的最后一名与后面的行在 `ORDER BY` 键上**并列**,那些并列行会被一起返回,所以**实际行数可能超过 n**。它必须配 `ORDER BY`(否则"并列"无从谈起),用来避免"第 10 名有 5 个人、却只显示了其中一个"。

`LIMIT BY` 是"每组取前 N 行"的**专用算子**,写法 A 与写法 B 结果等价,代价差很远:

```sql
-- 写法 A(推荐):单趟扫描,额外内存只有"每个分组一个计数器"
SELECT * FROM events ORDER BY ts DESC LIMIT 3 BY user_id;

-- 写法 B:要先把整个结果集分区 + 全排序,再回表过滤
SELECT * FROM (
    SELECT *, row_number() OVER (PARTITION BY user_id ORDER BY ts DESC) AS rn
    FROM events
) WHERE rn <= 3;
```

`LIMIT BY` **依赖输入顺序**:它只是"按分组键计数,数到 N 就丢",本身不会排序。所以**必须先 `ORDER BY` 才有确定结果**,否则你拿到的是"任意 3 行"。真实 ClickHouse 与 mini-clickhouse 在这条语义上一致。

`OFFSET` 本身要小心:跳过 20 万行再取 10 行,**前面的行仍然被读取、被排序、被丢弃**。深分页在 ClickHouse 里最好用"键集分页"(`WHERE ts < 上一页最后一个 ts ORDER BY ts DESC LIMIT 10`)代替。

## 16.7 窗口函数:语法总览

```sql
SELECT
    ...,
    func(args) OVER (
        PARTITION BY expr [, ...]      -- 可选:分组
        ORDER BY expr [ASC|DESC] [NULLS FIRST|LAST] [, ...]   -- 可选:组内排序
        [ROWS | RANGE | GROUPS BETWEEN <frame_start> AND <frame_end>]  -- 可选:帧
    ) AS alias
FROM t
WINDOW w AS (PARTITION BY ... ORDER BY ...);   -- 命名窗口,可复用(完整语法见 §16.8)
```

**`QUALIFY`:按窗口结果过滤,不必再包一层子查询。** 窗口函数不能写在 `WHERE` 里(原因见 §16.8),ClickHouse 为此有一等的 **`QUALIFY`** 子句——它写在 `WINDOW` 之后、`ORDER BY`/`LIMIT` 之前,过滤的正是窗口函数算出来的值:

```sql
SELECT k, ts, v,
       row_number() OVER (PARTITION BY k ORDER BY ts DESC) AS rn
FROM events
QUALIFY rn <= 3;      -- 每个 k 取最近 3 行
```

它和 §16.6 那种"把 `row_number()` 包进子查询、外层 `WHERE rn <= 3`"是**同一件事的两种写法**,但 `QUALIFY` 省掉了外层子查询、读起来更直。**代价提醒:`QUALIFY` 过滤的是窗口结果,所以它照旧要先把整个分区排序、维护窗口——它买的是可读性,不是性能。** 真要"每组取前 N",`LIMIT BY`(§16.6)单趟、低内存,通常便宜得多;只有当"需要同时看到排名号"或"过滤条件本身就是个窗口表达式"时,`QUALIFY` 才更顺手。它和 `LIMIT BY` 的代价对照,就是 §16.6 与本节这两处。

执行分两遍,所以它也是**阻塞算子**:① 按 `PARTITION BY` 分桶、按 `ORDER BY` 排好;② 逐分区扫一遍,按帧算出每个位置的值。

窗口函数分三族:

```text
排名族   row_number / rank / dense_rank / percent_rank / cume_dist / ntile
偏移族   lagInFrame / leadInFrame / first_value / last_value / nth_value
聚合族   sum / avg / count / min / max / any / ... (在帧上算)
```

## 16.8 命名窗口与完整帧语法

§16.7 给的是骨架,这一节补齐几块最容易用错的东西:命名窗口的复用与作用域、帧边界的完整写法、跨语言最容易被误认的 `GROUPS` 与 `EXCLUDE`,以及用来按窗口结果过滤的 `QUALIFY`。

### 命名窗口:WINDOW 子句

```sql
SELECT
    ts,
    sum(v) OVER w1 AS running,
    avg(v) OVER w1 AS running_avg,
    rank() OVER w2 AS rk
FROM events
WINDOW
    w1 AS (PARTITION BY user_id ORDER BY ts ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW),
    w2 AS (PARTITION BY user_id ORDER BY ts DESC);
```

一个 `WINDOW` 子句里可以定义多个命名窗口,后面用名字引用(`OVER w1`);还能**基于已有窗口再扩展** —— `OVER (w1 ORDER BY ts2)` 表示"用 `w1` 的定义、但把 `ORDER BY` 换成 `ts2`"。复用的价值不只是少写字:多个聚合函数共享同一个窗口定义时,`PARTITION BY`/`ORDER BY` 只排一次序。

**一条硬限制**:窗口定义里的 `PARTITION BY`/`ORDER BY` **不能引用同一个 `SELECT` 里定义的别名**:

```sql
SELECT toStartOfHour(ts) AS h,
       count() OVER (ORDER BY h)     -- 报错:h 是同一层的别名
FROM events;
```

因为窗口在第一遍执行时排序键就必须确定,而别名要到表达式求值后才存在。**绕法是把别名那一层包进子查询**,让它在窗口可见的层级里已经是一个真实列 —— 这和本层其它别名作用域规则一致(第 07 章 §07.2)。

### 帧边界的完整语法

```text
{ROWS | RANGE | GROUPS} BETWEEN <frame_start> AND <frame_end>
<frame_start|frame_end> :=
      UNBOUNDED PRECEDING | <expr> PRECEDING | CURRENT ROW
    | <expr> FOLLOWING   | UNBOUNDED FOLLOWING
```

- `UNBOUNDED PRECEDING` / `UNBOUNDED FOLLOWING` 是分区两端;
- `CURRENT ROW` 是当前行(在 `RANGE`/`GROUPS` 下还包含"与当前行同组的并列行");
- `n PRECEDING` / `n FOLLOWING` 往前/往后数 n —— 在 `ROWS` 下数**物理行**,在 `RANGE`/`GROUPS` 下数**值/组**。

这里有一条 `RANGE` 特有的**硬约束**,是最容易撞的坑:**一旦给 `RANGE` 带上偏移量(`n PRECEDING` / `n FOLLOWING`),那个窗口的 `ORDER BY` 就只能有「一个」表达式,而且它必须是数值或可做加减的类型(整型、浮点、`Date`/`DateTime` 之类)**。原因很直接:`RANGE` 算边界靠的是**值**的加减(`当前行的排序键值 ± n`),多个排序表达式时"拿谁减 n"无从定义,字符串列更没有"减 n"这回事。所以下面两种写法都会直接报错:

```sql
-- ✗ 错:对 string 列做 RANGE 偏移
... OVER (ORDER BY name RANGE BETWEEN 1 PRECEDING AND CURRENT ROW)

-- ✗ 错:ORDER BY 有两个表达式(报"RANGE OFFSET 需要恰好一个 ORDER BY 列")
... OVER (ORDER BY a, b RANGE BETWEEN 1 PRECEDING AND CURRENT ROW)

-- ✓ 对:单个数值/时间列
... OVER (ORDER BY ts RANGE BETWEEN 60 PRECEDING AND CURRENT ROW)
```

还有两个与之配套的细节。其一,`RANGE` 的偏移量写的是**裸数值** —— 上面的 `60` 按**秒**解释(时间列就是"往前 60 秒"),**不要**写成 `INTERVAL 60 SECOND`,ClickHouse 的 `RANGE OFFSET` 不接受 `INTERVAL` 语法。其二,`RANGE` **不带偏移量**时(只有 `UNBOUNDED PRECEDING`/`CURRENT ROW`/`UNBOUNDED FOLLOWING`)**没有**这个单列限制,多列 `ORDER BY` 一样能用 —— 所以"想按值聚合、又有多列排序键"时,要么改用 `ROWS`(按物理行),要么先把排序键合成一个数值列表达。要的若是"按行距"而不是"按值距",直接用 `ROWS` 就好。

三种模式一句话记牢:`ROWS` 按行号、`RANGE` 按排序键的值、`GROUPS` 按"并列组"的个数。`GROUPS` **理论上**的行为是"`1 PRECEDING` 表示前一个**完整的并列组**",即使两组之间隔了很远的取值也算相邻一组。**但 ClickHouse 目前并不支持 `GROUPS`**:官方窗口函数文档的帧支持矩阵里它标的是 ❌,写下去会报 `Window frame 'GROUPS' is not implemented`(上游有一个把它加进去的 PR 仍在推进,尚未落地)。**所以在目标版本上,`GROUPS` 和下一节的 `EXCLUDE` 一样,当前都别写** —— 上面语法骨架里把它列出来,是为了让你在别处(如 PostgreSQL)看到它时知道"ClickHouse 这一格是空的",而不是"照着写就行"。

### EXCLUDE:标准里有、ClickHouse 没有

SQL 标准的帧还允许一条 `EXCLUDE` 子句,把某些行从帧里排除:

```text
EXCLUDE CURRENT ROW | EXCLUDE GROUP | EXCLUDE TIES | EXCLUDE NO OTHERS
```

**ClickHouse 不支持这条子句**,写上去会报错。所以"算移动平均时把当前行排除掉""排名时把并列的同伴排除掉"这类需求,在这里没有声明式写法,只能在表达式层面绕:要么用 `ROWS BETWEEN 1 PRECEDING AND 1 PRECEDING` 之类**用边界把当前行挡在外面**,要么先用窗口算出总量、再用标量表达式减掉当前行。这一段的意义不在于"教你写 `EXCLUDE`",而在于**别把 PostgreSQL 的例子直接照抄过来**。

### QUALIFY:不写外层子查询的窗口过滤

"窗口函数不能进 `WHERE`"是很多人第一次用窗口时的拦路虎:

```sql
SELECT k, ts, v
FROM events
WHERE row_number() OVER (PARTITION BY k ORDER BY ts DESC) = 1;   -- ✗ 报错:WHERE 看不到窗口值
```

标准解法有两种。其一是把含窗口的那一层包进子查询、在外层过滤(§16.6 的写法 B);其二是用 ClickHouse 的 **`QUALIFY`**——它正是为"按窗口结果过滤"设计的子句,写在 `WINDOW` 之后、`ORDER BY`/`LIMIT` 之前:

```sql
SELECT k, ts, v,
       row_number() OVER (PARTITION BY k ORDER BY ts DESC) AS rn
FROM events
QUALIFY rn = 1;                 -- 每个 k 的最近一行,不必再包一层
```

`QUALIFY` 里**既可以直接引用 `SELECT` 里定义的窗口别名**(上面的 `rn`),也可以**内联写整个窗口表达式**——`QUALIFY row_number() OVER (PARTITION BY k ORDER BY ts DESC) = 1` 与前一种等价。它能这么写,是因为求值顺序是 `WHERE → GROUP BY/HAVING → WINDOW → QUALIFY → SELECT → DISTINCT → ORDER BY`:`QUALIFY` 排在 `WINDOW` 之后,所以看得见窗口值;`WHERE` 排在窗口之前,所以看不见。**它的作用域与 `HAVING` 平行**:`HAVING` 过滤聚合结果,`QUALIFY` 过滤窗口结果。

要注意 `QUALIFY` 与 `HAVING`、`WHERE` 的分工,别互相替代:能用 `WHERE` 预过滤掉的行一定要用 `WHERE`(它在窗口之前,越早筛掉代价越低,见 §16.13);`QUALIFY` 只负责"过滤条件本身就是窗口值"的那一层。

## 16.9 排名族:并列时的行为

用同一个含重复值的数据集 `k = [1, 1, 2]` 看差别:

```text
row_number     1, 2, 3     纯行号,永远并列也各有各的号
rank           1, 1, 3     并列同名,下一个**跳号**(跳掉 2)
dense_rank     1, 1, 2     并列同名,下一个**不跳号**
percent_rank   0, 0, 1     (rank - 1) / (行数 - 1)
cume_dist      2/3, 2/3, 1 值 ≤ 当前值的行数 / 总行数
ntile(2)       1, 1, 2     分成 2 桶,前面的桶多分一行
```

具体说明:

- **`row_number`** 严格按位置编号,与值无关。想要"每组取前 N"时它是最直观的,但那件事该用 `LIMIT BY`(§16.6)。
- **`rank`** 是"竞技排名"(1,1,3):并列占掉了名次。想知道"我排第几"用它。
- **`dense_rank`** 是"密集排名"(1,1,2):并列不占名次,结果在 `1..k` 里没有空洞。统计"一共有几档"用它。
- **`percent_rank`** = `(rank - 1) / (n - 1)`,把 `rank` 归一化到 `[0, 1]`。
- **`cume_dist`** = "值不大于当前值的行数 / 总行数",是**累积分布**。$P_{95}$ 分位所在的档位可以用它粗筛。
- **`ntile(n)`** 把分区切成 n 桶,行数不能整除时**前面的桶多分一行**(与 SQL 标准一致)。注意它按**位置**切,不按值切 —— 并列的两行可能被分到不同桶里。

`row_number`/`rank`/`dense_rank`/`ntile` 的结果类型是 `UInt64`,`percent_rank`/`cume_dist` 是 `Float64`。

## 16.10 偏移族:lagInFrame 这个名字最容易踩

**ClickHouse 的窗口函数名带 `InFrame` 后缀**:

```sql
lagInFrame(expr, offset, default)
leadInFrame(expr, offset, default)
```

**ClickHouse 文档里列出的窗口偏移函数就是 `lagInFrame`/`leadInFrame`(以及 `first_value`/`last_value`/`nth_value`)。** 不带后缀的 `lag`/`lead` 是**更晚才注册的别名**(具体引入版本**本条未确认**:流传的说法是"大约 25.6 起",但未在 25.3 上核对到对应的上游改动):在被支持的版本里,它们等价于同名的 `InFrame` 版,但**强制使用整帧(`ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING`)、且不允许再显式写帧**。**在 25.3 上它们还不能当窗口函数用**,所以写 `lag`/`lead` 会当作普通函数、进不了窗口。稳妥起见,窗口里一律写带后缀的名字,需要时以 `system.functions` 实测。

保守做法:**窗口里一律写 `lagInFrame`/`leadInFrame`**,这个名字与行为都稳定。

两者的关键差别是"**从哪里看**":

- `lagInFrame`/`leadInFrame` 只在**当前帧内**按物理偏移取行;
- **帧默认不是整个分区** —— 有 `ORDER BY` 时默认帧是 `RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW`,**帧的右边界就是当前行**。

于是有一个著名的陷阱:

```sql
-- 想取"下一行的时间",这个查询会永远拿到 default
SELECT ts, leadInFrame(ts, 1, toDateTime(0)) OVER (ORDER BY ts) FROM events;
```

**错误做法**:指望 `leadInFrame` 在默认帧下能看到下一行。**后果**:下一行在当前帧之外,函数**静默返回默认值**(上例里是 `1970-01-01`),不报错。**正确做法**:显式把帧写成能看到目标行,想要标准 `lag`/`lead` 的行为就写:

```sql
SELECT ts,
       lagInFrame(ts, 1, toDateTime(0)) OVER w AS prev_ts,
       leadInFrame(ts, 1, toDateTime(0)) OVER w AS next_ts
FROM events
WINDOW w AS (
    PARTITION BY user_id
    ORDER BY ts
    ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING
);
```

同理,`last_value(x)` 在**默认帧**下返回的是**当前行**的 `x`,而不是分区最后一行的 —— 因为帧的右边界就是当前行。想拿分区最后一行,用 `last_value(x) OVER (... ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING)`,或者干脆 `any(x) OVER (...)`。

`first_value`/`last_value`/`nth_value` 的语义就一句话:取帧内第 1 / 最后 / 第 n 个位置的值。

## 16.11 聚合族与默认帧

任何能在帧上增量计算的聚合都能当窗口函数用:`sum`、`avg`、`count`、`min`、`max`、`any` 等。**不是所有聚合都能上窗口** —— 像 `uniq` 这种"状态不可减"的近似算法,滑动窗口里没法把离开帧的行"减掉",所以它不能作为窗口函数使用。这条限制在实现层面是硬约束。

默认帧规则(两条,必须记住):

```text
OVER (ORDER BY ...)        →  RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
OVER (PARTITION BY ...)     →  整个分区(没有 ORDER BY 时,帧默认覆盖全分区)
OVER ()                     →  整个结果集(没有 PARTITION BY,整张结果就是一个分区)
```

第一条对应"累计到当前行"这个最常见的需求。后两条对应"整个分区/整个结果集的聚合值"(比如"这一行占分区总额的百分比"),关键点是:**默认帧是不是整个分区,取决于有没有 `ORDER BY`** —— 只要写了 `ORDER BY`,默认帧的右边界就收窄到当前行,不再是整个分区;`OVER ()` 之所以是"全体",是因为它既没有 `PARTITION BY` 也没有 `ORDER BY`。

## 16.12 ROWS 与 RANGE:差别在有并列排序键时才暴露

这是窗口函数里最微妙的一处。

```text
ROWS  BETWEEN ...  按**物理行号**算边界
RANGE BETWEEN ...  按**值**算边界 —— 所有与当前行排序键"相等"的行算作同一位置
```

数据 `(k, v)`:

```text
(1, 10)  (1, 20)  (2, 30)
```

```sql
-- ROWS:三行分别是 10, 30, 60
SELECT k, v, sum(v) OVER (ORDER BY k ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)
FROM t;

-- RANGE:前两行拿到同一个数 30,第三行 60
SELECT k, v, sum(v) OVER (ORDER BY k RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)
FROM t;
```

原因是 `RANGE` 把 `k` 相等的两行看成"同时到达",所以两行的累计值一样(都包含 `10 + 20`);`ROWS` 把它们当成两个不同的物理位置,于是第二行比第一行多加了一个 `20`。

**什么时候必须在意**:任何"累计求和 / 移动平均 / 排名之外的累计统计"只要排序键有重复值,`ROWS` 和 `RANGE` 就会给出不同答案。默认帧是 `RANGE`,所以"默认行为"往往是你没预期的那一种。**建议:只要用了帧,就显式写 `ROWS` 或 `RANGE`**,别依赖默认。

## 16.13 执行代价与常见优化点

窗口函数两遍实现意味着:分区 + 排序(阻塞)+ 逐位置计算。三个优化点,按收益排序:

1. **数据已经按 `PARTITION BY ... ORDER BY ...` 有序时,可以跳过排序**。这是最常见、最容易拿到的一项收益 —— 只要把窗口的 `PARTITION BY`/`ORDER BY` 对齐表的排序键,或者对齐上一步的 `ORDER BY`,代价立刻降下来。相反,**一个多余的 `ORDER BY` 冲突就能让这个优化失效**。
2. **对"可增量"的帧做专门优化**。`ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW` 等价于前缀和,可以单趟算出全部分区;真实的 ClickHouse 会为这类帧走增量路径,而 `ROWS BETWEEN 2 PRECEDING AND CURRENT ROW` 这种滑动窗口只需要加减法。相比之下,**每个位置都重算一遍帧内聚合**是最朴素的实现,复杂度是 O(n²) 级别。
3. **尽量让窗口函数作用在小结果集上**。窗口是在 `WHERE`、`GROUP BY` 之后的阶段执行的,所以先用 `WHERE` 把数据筛小,窗口的代价立刻下降。

**内存峰值与落盘:窗口不是靠 `max_bytes_before_external_sort` 兜底的。** 窗口算子的两笔内存开销要分开看:一笔是**阻塞排序**把整个输入物化出来,另一笔是**窗口算子自己按分区持有行与帧状态**。`max_bytes_before_external_sort`(以及 §16.1 那个按比例的兄弟)只作用于**排序那一笔**——它能让排序溢写到磁盘,却**不给窗口算子提供溢写自身帧状态的手段**。所以一个“单分区极大”的窗口(`OVER ()`、`PARTITION BY` 键几乎各不相同、或某个分区键值上堆了上千万行)照样能把内存吃光,而排序落盘救不了它。真正的降内存手段是**把分区变小**:让 `PARTITION BY` 的键把数据切成大小可控的分区,或先用 `WHERE`/`GROUP BY` 把进入窗口的行数压下来(第 3 点)。**(窗口算子自身有没有独立的溢写设置、以及 25.3 上的确切行为,我未确认;这里只说明“排序侧的落盘阈值不覆盖窗口状态”这一点。)**

**每帧的代价:`RANGE` 通常比 `ROWS` 贵。** `ROWS` 的帧边界是**位置**(第几行),定位是常数时间;`RANGE` 的边界靠**排序键的值**算,要么在有序分区里按值搜索边界、要么做值的加减(§16.8 那条“带偏移量的 `RANGE` 只能有一个数值/时间 `ORDER BY` 列”正是这个代价的副产品)。所以同样是滑动窗口,`ROWS 2 PRECEDING` 一般比等价的 `RANGE` 便宜;而 `RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW`(默认帧)因为是“从分区头累计到当前值”,反倒能走增量。结论不变(§16.12):**帧一律显式写**,既是为了行为确定,也是因为 `ROWS` 往往更快。

## 16.14 一个实用组合

**相邻事件的时间间隔**(会话行为分析里最常用的一个):

```sql
SELECT
    user_id,
    ts,
    dateDiff('second',
             lagInFrame(ts, 1, ts) OVER w,
             ts) AS gap_seconds
FROM events
WINDOW w AS (PARTITION BY user_id ORDER BY ts
             ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING);
```

**会话切分**(间隔超过 30 分钟就开一个新会话):

```sql
SELECT user_id, ts,
       sum(is_new_session) OVER (PARTITION BY user_id ORDER BY ts) AS session_id
FROM (
    SELECT user_id, ts,
           if(dateDiff('minute', lagInFrame(ts, 1, ts) OVER (
                  PARTITION BY user_id ORDER BY ts), ts) > 30, 1, 0) AS is_new_session
    FROM events
);
```

**这属于"手搓会话切分"。** 一旦需求从"相邻间隔"上升到"一段时间窗内按顺序完成了若干步",就该换用专门的**会话化聚合函数**:`windowFunnel`(漏斗:窗口内按序命中了几个步骤)、`sequenceMatch`(图案匹配:事件序列是否满足某个模式)、`retention`(留存:某天来的用户后续几天是否回来)。它们把"排序 + 相邻间隔判断"收敛成一个聚合状态,比手写 `lagInFrame` 再套外层 `sum(is_new_session)` 更省事、也更不容易错在这条会话边界上。这几个函数的定义与例子见第 12 章 §12.6。

**分组内 TOP-N**:这个需求优先用 `LIMIT BY`(§16.6);只有在"需要同时看到排名号"的时候才用 `row_number()`——这时过滤别硬塞进 `WHERE`(窗口函数不能写在 `WHERE` 里),写成 **`QUALIFY`** 最干净:

```sql
SELECT user_id, ts, amount,
       row_number() OVER (PARTITION BY user_id ORDER BY ts DESC) AS rn
FROM events
QUALIFY rn <= 3;             -- 每个用户最近 3 笔,省掉外层子查询
```

这和"把窗口那层包进子查询、外层 `WHERE rn <= 3`"结果相同,代价也一样(两者都要先把分区排好、维护窗口);`QUALIFY` 买的是可读性,不是省掉排序。别忘了它和 `LIMIT BY` 的取舍:`LIMIT BY` 单趟、低内存,只要不需要排名号就别用窗口。

## 16.15 DISTINCT 的实现与代价

`SELECT DISTINCT a, b, c` 在语义上等价于 `SELECT a, b, c FROM t GROUP BY a, b, c` —— 它要维护一份"见过的整行"的集合,内存随**不同行的数量**增长。`DISTINCT` 与 `DISTINCT ON (...)` 的**语法**在第 07 章 §07.11 已经讲过,这一节只谈**代价**;另外要说明:本章**不覆盖 `DISTINCT ON`** —— 它只对指定列组合去重、效果等价于 `LIMIT 1 BY`(也是第 07 章 §07.8 的内容),和这里"整行去重"的代价模型不同。

**为什么高基数列的 `DISTINCT` 慢**:代价由不同值的数量决定,不由扫描量决定。对一个有 5 亿个不同值的 `request_id` 做 `DISTINCT`,就是在内存里放 5 亿个键;而对 5 个取值的 `status` 做 `DISTINCT`,几乎是免费的。这跟 `uniq` 面对的问题一模一样 —— 区别是 `DISTINCT` 要输出**完整的行**,所以连"用 HyperLogLog 换内存"这条路都走不通。

实用推论:

- 只想**数**有多少个不同值,用 `uniq`/`uniqExact`,不要 `SELECT DISTINCT ... ` 之后再 `count()`;
- 只想**看几个例子**,加 `LIMIT`;
- 真要去重导出,先 `WHERE` 把数据筛到可控规模。

## 16.16 与 mini-clickhouse 的对照

**排序**。`minich/execution/processors.py` 的 `SortingTransform` 注释里直接把自己标成 blocking operator,实现是"累积所有 block,最后拼起来一次全排"。真实 ClickHouse 是"分块排序 + 多路归并"(`SortingTransform` 接 `MergingSortedTransform`),每块内部可以并行、归并是流式的;mini-clickhouse 直接全排,**结果一样,内存峰值更高**。

多列排序用的是经典技巧(`_stable_sort_indexes`):**从最后一列往前排,每趟用稳定排序**,于是不需要构造复合键。真实 ClickHouse 把这个思路做到更底层:把排序键序列化成一段字节然后 `memcmp`。

§16.2 的 `NULL` 规则在这个文件里能直接读到:

```python
# 默认:NULL 是"最大值",所以 ASC 时在最后、DESC 时在最前
nulls_first.append(getattr(e, "direction", "ASC") == "DESC")
```

**LIMIT BY**。`LimitByTransform` 每个分组键维护一个计数器,数到 `offset + limit` 就不再放行 —— 单趟、O(1) 额外空间,和文档描述完全对得上,注释里也写明了"依赖输入顺序"。

**LIMIT BY vs 窗口函数**:`LimitByTransform` 的注释里把两种写法并列写出来做对比,结论与 §16.6 一致。

**窗口函数**。`minich/execution/window.py` 实现了:

- 排名族:`row_number`、`rank`、`dense_rank`、`percent_rank`、`cume_dist`、`ntile`(全在 `RANKING_FUNCTIONS` 里);
- 偏移族:`lag`、`lead`、`lagInFrame`、`leadInFrame`、`first_value`、`last_value`、`nth_value`,以及 `first_value_respect_nulls`/`last_value_respect_nulls`(在 `OFFSET_FUNCTIONS` 里);
- 聚合族:`sum`、`avg`、`count`、`min`、`max`、`any`、`anyLast`、`groupArray`、`groupArrayLast`、`sumKahan`、`median`、`stddevPop/Samp`、`varPop/Samp`(在 `WINDOW_AGGREGATES` 里)。

三处必须诚实标注的差异:

1. **`RANGE` 被当成 `ROWS` 处理**。`_frame_bounds` 的注释写得很明确:默认帧按规范返回 `0..slot`,但对显式的 `RANGE` 帧不做"按值扩展帧"的处理,所以**在有并列排序键的数据上会与真实 ClickHouse 不一致** —— §16.12 那个 `(1,10) (1,20) (2,30)` 的例子,在 mini-clickhouse 里 `RANGE` 和 `ROWS` 会给出同一组结果。这是本模块最大的语义偏差。
2. **窗口聚合是逐位置重算**(`_aggregate` 的注释直接写了"O(n) 每个位置 = O(n²) 总量"),没有前缀和/滑窗增量优化。
3. **不做"数据已有序则跳过排序"的优化**,一律全分区排序。
4. mini-clickhouse 里 `lag`/`lead`(不带 `InFrame`)也是可用的窗口函数,`_value_at` 里两者走同一条分支。这比真实 ClickHouse 宽松 —— 真实 ClickHouse 文档只列 `lagInFrame`/`leadInFrame`(见 §16.10)。

**DISTINCT**。`DistinctTransform` 用一个 Python `set` 对**整行**做去重,新出现的行打上 `True` 保留。它的注释里点出了真实 ClickHouse 的做法:"把 `DISTINCT` 实现成全列 `GROUP BY`,复用两级聚合、可以下推到分布式节点";mini-clickhouse 单独写一个处理器只是因为更好读。

## 16.17 小结

- `ORDER BY` 是**阻塞算子**:内存峰值是整个结果集;`max_bytes_before_external_sort`(及按比例的 `max_bytes_ratio_before_external_sort`)是落盘逃生门。`ORDER BY ... LIMIT n` 省钱的是"保留多少行",不是"要看多少行"。
- `NULL` 被当成**最大值**:`ASC` 时在最后、`DESC` 时在最前。要确定行为就写 `NULLS FIRST`/`NULLS LAST`。
- `COLLATE` 只用于字符串的按语言排序,属展示层,别让它影响分组/去重的等价判断。
- `ORDER BY` 命中排序键前缀 + `optimize_read_in_order` 时,排序几乎不要钱;这是表设计层面能拿到的最值钱的一项优化。
- `ORDER BY ... WITH FILL [FROM ... TO ... STEP ...]` 把时间轴上的**缺口补齐**(补出的行是类型默认值),`STALENESS`(24.11 引入,且**与 `FROM` 互斥**)限制连续填充长度,`INTERPOLATE (col AS expr)` 决定补出的行显示什么;它不参与 `GROUP BY`。
- `LIMIT BY` 是"每组取前 N"的专用算子,单趟、低内存,**但依赖输入顺序**;能用它就别用 `row_number()`。真要用 `row_number()`(比如还要看排名号),过滤写成 **`QUALIFY rn <= N`**(一等子句,不必包外层子查询),或以子查询在外层 `WHERE` 过滤——两者代价相同。`LIMIT ... WITH TIES` 会把最后一名的并列行一并带回(行数可能超过 n)。深分页用键集分页代替 `OFFSET`。
- 排名族的并列行为:`row_number` 1,2,3 / `rank` 1,1,3 / `dense_rank` 1,1,2。
- **ClickHouse 文档列的窗口偏移函数是 `lagInFrame`/`leadInFrame`**,默认帧的右边界是当前行,所以 `leadInFrame` 在默认帧下看不到下一行,会静默返回默认值 —— 必须显式写帧。
- 命名窗口用 `WINDOW w AS (...)` 复用,且窗口定义里的 `PARTITION BY`/`ORDER BY` **不能引用同层 `SELECT` 别名**;帧模式 ClickHouse 只支持 `ROWS`(行)与 `RANGE`(值),**`GROUPS`(并列组)与标准里的 `EXCLUDE` 当前都不支持**,写上去会报错。
- 默认帧:有 `ORDER BY` 时是 `RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW`,没有时是整个分区。**帧一旦有并列排序键,`ROWS` 与 `RANGE` 就会分道扬镳**,建议永远显式写。
- **带偏移量的 `RANGE` 有硬约束**:`ORDER BY` 只能有**一个**数值/时间类型的表达式(字符串列或多列会直接报错),偏移量写**裸数值**(时间列按秒)、不能写 `INTERVAL`;不带偏移量的 `RANGE` 没这个限制。
- 窗口函数是阻塞算子;最大的优化点是"让数据本来就按 `PARTITION BY ... ORDER BY ...` 有序"。
- `DISTINCT` 等价于全列 `GROUP BY`,代价由**不同值的数量**决定 —— 只想计数就用 `uniq`/`uniqExact`。
