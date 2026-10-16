# 第 07 章 SQL 语法全貌:SELECT

写惯了 MySQL 的人第一次用 ClickHouse,最容易出的错不是"函数名忘了",而是**把一个子句放错了位置**——把过滤条件写进 `HAVING` 而不是 `WHERE`,在 `WHERE` 里引用聚合结果,以为 `WITH` 会像 PostgreSQL 的 CTE 那样只跑一次,或者没意识到 `SELECT` 里的别名会反向覆盖掉 `WHERE` 里的真实列。这些错误的共同点是:它们都能通过语法检查,都能跑出**结果**,只是结果不是你想要的。

这一章讲的是 `SELECT` 的**骨架**:子句之间的求值顺序、别名的作用域、以及各子句之间的组合规则。每一处细节都已经有专门的章节(聚合见第 12 章、JOIN 见第 15 章、排序窗口与 `LIMIT BY` 见第 16 章、函数见第 17 章、格式见第 19 章、执行管线见第 10 章),这里只讲"它们怎么拼在一起"。

## 07.1 逻辑求值顺序

`SELECT` 的书写顺序和求值顺序**无关**。ClickHouse 在逻辑上按下面这个顺序处理一条查询:

```text
WITH  →  FROM  →  SAMPLE  →  ARRAY JOIN  →  JOIN  →  PREWHERE  →  WHERE
      →  GROUP BY  →  HAVING  →  WINDOW  →  QUALIFY  →  SELECT  →  DISTINCT
      →  ORDER BY  →  LIMIT BY  →  LIMIT / OFFSET  →  SETTINGS / FORMAT
```

链里有两个位置最容易记漏。`SAMPLE` 排在 `FROM` 之后、`JOIN` **之前**——它先按 `SAMPLE BY` 键把源数据裁成一个子集,再拿这个子集去参与后续的连接;把采样当成"连接之后对结果抽样"是常见的误解,那样得到的分子分母都不是采样子集。`ARRAY JOIN` 排在 `JOIN` **之前**——先把左侧源表里的数组列展开成多行,再拿展开后的结果去连接;反过来想"先连接再展开"会得到完全不同的行数与代价。这条顺序也写进了 SELECT 的书写顺序(`FROM t ARRAY JOIN ... JOIN ...`)。至于 25.3 的执行计划里 `ArrayJoinStep` 究竟落在 `JoinStep` 的哪一侧,取决于优化器把 `ARRAY JOIN` 下推到了哪一步(见 §07.10),**本书未逐条核对 25.3 的算子排布**,不要把"计划里必然先 `ArrayJoin` 后 `Join`"当成定理。

这不是"执行计划里算子的顺序",而是一张**依赖关系表**:排在后面的阶段可以看见前面阶段产出的东西,反之不行。它一口气解释了好几件看起来奇怪的事:

`WHERE` 里不能引用 `SELECT` 的聚合结果,因为 `WHERE` 在 `GROUP BY` 之前。这不是 ClickHouse 的刁难,而是逻辑上的必然——过滤发生在"分组"之前,那时根本还没有"每个组的总和"这个对象。真正的答案在 `HAVING` 里,或者用子查询把聚合包起来再在外层过滤。

`WHERE` 里不能引用窗口函数的结果:窗口是在 `WHERE`/`GROUP BY`/`HAVING` **之后**、但在 `SELECT` 的表达式求值**之前**算出来的(看上链里 `WINDOW`/`QUALIFY` 的位置)。所以 `WHERE` 看不到窗口值,而 `SELECT` 与 `QUALIFY` 都能看到。

`DISTINCT` 在 `ORDER BY` 之前,于是 `SELECT DISTINCT a FROM t ORDER BY b` 这种写法在语义上是可疑的——去重之后 `b` 可能已经不是原来那个 `b` 了。

`LIMIT BY` 在 `ORDER BY` 之后、`LIMIT` 之前。这一条决定了"每个用户取最近 N 条"为什么必须写成 `ORDER BY ts DESC LIMIT 3 BY user_id`:先排序,再按组截取,最后才应用总的 `LIMIT`。

`QUALIFY` 紧贴在 `WINDOW` 之后,是**窗口版的 `HAVING`**——它过滤的是窗口函数算出来的结果:

```sql
SELECT user_id, ts, row_number() OVER (PARTITION BY user_id ORDER BY ts DESC) AS rn
FROM hits
QUALIFY rn <= 3;
```

因为它排在窗口求值之后,所以"`WHERE` 里用不了窗口结果、`QUALIFY` 里可以"——`WHERE` 在 `WINDOW` 之前、看不到窗口值,而 `QUALIFY` 在它之后。它和 `LIMIT BY`(§07.8)是"每个分组取前 N 行"的两种写法:语义等价,但 `LIMIT BY` 不需要真的维护窗口,通常便宜得多。它与 `LIMIT BY` 的代价对照、以及 `QUALIFY` 的完整语法(别名与内联两种写法、它在求值链上的位置)在第 16 章 §16.7–§16.8;`QUALIFY` 自身的速查条目见附录 A。

## 07.2 别名的作用域

**ClickHouse 允许 `WHERE` / `GROUP BY` / `HAVING` / `ORDER BY` 引用 `SELECT` 里定义的别名**,这与 SQL 标准不同。实现方式是别名被**内联展开**成它背后的表达式:`SELECT toDate(ts) AS d ... WHERE d > '2025-01-01'` 会被改写成在 `WHERE` 里直接算 `toDate(ts) > '2025-01-01'`。

方便是有代价的,最大的那个坑是:**别名与真实列同名时,默认由别名胜出**。控制这件事的设置是 `prefer_column_name_to_alias`,文档给出的默认值是 `0`(别名优先),把它设成 `1` 则列名优先。**这个默认值有过变动的传闻**(有报告称某次 24.x 小版本升级中默认值变过),所以不要背它,直接查:

```sql
SELECT name, value, default, changed
FROM system.settings
WHERE name = 'prefer_column_name_to_alias';
```

一个能让你亲眼看到差别的例子(取自官方文档):

```sql
SET prefer_column_name_to_alias = 0;
SELECT avg(number) AS number, max(number) FROM numbers(10);
-- 报错:ILLEGAL_AGGREGATION(Code 184)

SET prefer_column_name_to_alias = 1;
SELECT avg(number) AS number, max(number) FROM numbers(10);
-- 正常返回
```

这里顺便分清一个容易混的地方:**错误码的符号名**和**报错文案**是两回事。你在客户端看到的文本形如 `Aggregate function ... is found inside another aggregate function in query` —— 那是这条错误的**消息(message)**,用来给人看;错误码 184 对应的**枚举常量(符号名)**才是 `ILLEGAL_AGGREGATION`,`Code 184` 里的 `Code` 就是那个编号。脚本里按名字匹配错误时要用枚举名让匹配稳定,别把消息文本当成码名——消息的措辞会变,枚举名不会。

原因是别名 `number` 覆盖了列 `number`,于是 `max(number)` 被展开成 `max(avg(number))`——聚合套聚合,**抛的就是上面那个 184**。**错误做法**:给别名起一个与列同名的名字。**后果**:整条查询的语义可能被静默改写,甚至报出你完全看不懂的错误码。**正确做法**:别名加前缀(`avg_number`),并且不要在客户端里全局改这个设置。

另一条相关规则是**别名是"全局"的**:它的作用域覆盖整个查询(包括子句与子表达式),所以同一个别名不能在不同分支里表达不同含义。别名还**不能形成环**——`SELECT a + 1 AS b, b + 1 AS a` 会报循环别名错误。

## 07.3 WITH 的三种形态

```sql
-- 形态一:表达式别名(常量或表达式)
WITH toDate(now()) AS today, 1.5 AS factor
SELECT ...;

-- 形态二:命名子查询
WITH recent AS (SELECT * FROM hits WHERE ts > now() - INTERVAL 1 HOUR)
SELECT count() FROM recent;
```

形态一只是文本替换,没有额外成本。

形态二需要特别注意:ClickHouse **不会**把命名子查询物化成一张临时结果表,它更接近于**内联展开**。于是同一段 `WITH` 被引用两次,底层就可能被扫两遍:

```sql
WITH recent AS (SELECT * FROM hits WHERE ts > now() - INTERVAL 1 HOUR)
SELECT (SELECT count() FROM recent), (SELECT max(id) FROM recent);
```

这与 PostgreSQL 的 CTE 语义不同(PostgreSQL 12 之前默认物化 CTE,之后按代价选择),也解释了为什么"CTE 没有变快"。如果你确实需要"只算一次",做法是把它写进一张临时表,或者依赖 `WITH` 之外的机制(`IN` 的集合缓存、物化视图等)。

**形态三:递归 CTE(`WITH RECURSIVE`)。** 这是三种形态里最年轻的一种,也是从 PostgreSQL / MySQL 迁过来的人最容易想当然的一种——展开组织树、BOM(物料清单)、路径枚举这类"层级递归"问题,写惯了递归 CTE 的人会直接照搬标准写法:

```sql
WITH RECURSIVE tree AS (
    SELECT id, parent_id, 1 AS depth FROM nodes WHERE parent_id = 0
    UNION ALL
    SELECT n.id, n.parent_id, t.depth + 1
    FROM nodes AS n JOIN tree AS t ON n.parent_id = t.id
)
SELECT * FROM tree ORDER BY depth, id;
```

形状与标准 SQL 一致:**锚点查询 + `UNION ALL` + 引用自己的递归项**,通常还要在递归项里带一个终止条件——ClickHouse 侧还有一道兜底:递归深度超过 `max_recursive_cte_evaluation_depth`(默认 1000)会直接报 `TOO_DEEP_RECURSION`,而不是一直算下去。

要提醒三件事。其一,**它比前两种形态晚得多才落地,而且是在新分析器里实现的**,所以老分析器(`enable_analyzer = 0`)下这条语法用不了——迁移旧代码时如果发现它不认,先查分析器开关,而不是怀疑语法(新分析器的现状与迁移见第 24 章 §24.17)。其二,**它在 25.3 上的确切支持面本书未逐条核对**:递归项里能写多复杂、深度上限由哪个设置控制、`UNION`(去重)形式是否支持,都请以目标版本的文档与实测为准,别照着 PostgreSQL 的完整能力去假设。其三,**在 ClickHouse 里它常常不是最优解**:层级本来就适合用数组表达,`groupArray` + `arrayJoin`、或者把层级预先物化成一张带 `path` 列的维表,往往比每查一次就递归一次更符合这个引擎的脾气。递归 CTE 的价值在于**把已有的 SQL 迁过来时不必重写**,而不是"ClickHouse 推荐这么建模"。

## 07.4 SELECT * 的变形

`SELECT *` 可以带修饰,这是一族"列变换":

```sql
SELECT * EXCEPT (payload, raw) FROM hits;           -- 去掉几列
SELECT * REPLACE (lower(url) AS url) FROM hits;     -- 替换几列的取值
SELECT * APPLY (sum) FROM hits;                     -- 对每一列都套一遍函数
SELECT COLUMNS('^metric_') FROM hits;               -- 按正则匹配列名
```

`COLUMNS('regex')` 在宽表上非常有用:几十个同名族的指标列可以用一条正则一次性选出来。**`APPLY(func)` 则是对展开出来的每一列都套用 `func`**——`SELECT * APPLY (sum)` 等价于对每一列写 `sum(col)`,但这正是它的危险之处:`sum` 在 `String`、`Array` 这类非数值列上没有实现,于是 `SELECT * APPLY (sum)` 一旦遇到非数值列就会**直接报类型不匹配的错误**。因此 `APPLY` 必须配合 `COLUMNS('正则')` 或 `EXCEPT` 限定列,不能真的"应用到全部列";它常和这两者组合着用:先用 `EXCEPT`/`COLUMNS` 选出要处理的列,再 `APPLY` 统一变换(`SELECT COLUMNS('^metric_') APPLY (max) FROM hits`)。这四种修饰可以叠加,也可以带 `EXCEPT` / `REPLACE` 一起用。**这些变形不适合写进长期存在的代码**:加了一列就可能改变 `SELECT *` 的列集合,下游按位置取列的客户端会无声错位。

## 07.5 PREWHERE 与 WHERE

`PREWHERE` 是过滤,但它**先于 `WHERE` 执行**,而且是**按列推进**的:ClickHouse 先只用 `PREWHERE` 涉及的那几列把数据读进来并过滤,只把存活的行送去读剩余的列。这就是它收益的来源——如果 `PREWHERE` 能筛掉 99% 的行,那 99% 的行就不必读宽列了。

```sql
SELECT user_id, payload
FROM hits
PREWHERE ts BETWEEN '2025-03-01 00:00:00' AND '2025-03-01 01:00:00'
WHERE status = 200;
```

`WHERE` 的作用是在 `PREWHERE` 之后对**已经读出来的行**继续过滤,它没有"少读列"的收益。

实际使用中很少手写 `PREWHERE`:设置 `optimize_move_to_prewhere`(默认 `1`)会让优化器自动把 `WHERE` 里合适的一部分条件挪到 `PREWHERE`。所以真正的建议是——**先看 `EXPLAIN` 里条件去哪儿了**,再决定要不要手写(第 24 章)。另有一个 `optimize_move_to_prewhere_if_final`,它控制带 `FINAL` 的查询是否也做这个下推(25.3 上默认是 `0`,即默认不开启;它会在哪个版本改为默认开启的**本条未确认**,用前查 `system.settings`)。

## 07.6 GROUP BY 的修饰符

在 `GROUP BY` 后面可以加修饰符,它们会产生**额外的行**:

- `WITH TOTALS`:在结果末尾附加一行总计;
- `WITH ROLLUP`:按分组键的层级逐级上卷(类似"每个省、每个国家、全局"各一行);
- `WITH CUBE`:所有键的组合各来一行,行数是指数级的;
- `GROUPING SETS ((a), (a, b))`:显式列出你要哪几组;
- `GROUP BY ALL`:按 `SELECT` 里所有非聚合表达式分组。

除了这些修饰符,`GROUP BY` 与 `ORDER BY` 还接受**序数位置**:`GROUP BY 1, 2` 表示"按 `SELECT` 列表的第 1、2 个表达式分组",`ORDER BY 1 DESC` 同理(与 MySQL 的写法同源,`SELECT` 里的位置从 1 数起)。它在临时查询里省事——不必把一长串表达式抄两遍——但**不要写进长期代码**,毛病和 `ORDER BY ALL` 一模一样:一旦有人在 `SELECT` 列表里插了一列,位置就整体挪位,分组键或排序键会在没人改 SQL 的情况下变掉。要稳定,就显式写出表达式或别名。

`GROUP BY ALL` 还有两个同伴,同样属于"用 `ALL` 指代一整组列"的便利语法,但落点不在 `GROUP BY` 上,容易漏掉:**`ORDER BY ALL`**(按 `SELECT` 输出的**所有列**依次排序,严格按列在输出里的顺序排,等价于把这些列名逐个写进 `ORDER BY`)。它在"对两张结果集做肉眼比对""先按整行去重再确认顺序"这类一次性场景里很省事(若你那一版不认这个写法,回退成显式列名即可,语义完全一样),但**不要写进生产查询**:一旦上游给 `SELECT` 加了列,排序键就跟着变,结果顺序会在没人改 SQL 的情况下变化。生产查询里排序键应当是显式写死的。另外 `LIMIT n BY ALL` 也是同一族的写法(按所有非聚合输出列分组取前 n 行),见 §07.8。`ORDER BY` 本身的语义、`NULL` 的位置与代价在第 16 章 §16.1 与 §16.2。

这里有一个观感上的坑:**小计行里被"卷起来"的那些键列填什么,取决于设置 `group_by_use_nulls`**。它默认为 `0` 时,被上卷的键列填的是**该类型的默认值**而不是 `NULL`(数字是 `0`、字符串是空串;只有列本身是 `Nullable` 时才自然是 `NULL`);把它设成 `1` 时,被上卷的键列**一律填 `NULL`,与列自身是不是 `Nullable` 无关**(这也是 SQL 标准期望的行为)。同一条 `WITH ROLLUP` 查询,两种设置下小计行的长相完全不同,所以"小计列是不是 `NULL`"这个问题没有唯一答案。也正因如此,用 `IS NULL` 判断"这一行是不是小计行"并不可靠,应该用 `GROUPING(...)` 函数——它对两种设置都成立。各修饰符产生的行数公式与它们在聚合阶段的完整填充规则在第 12 章展开;`group_by_use_nulls` 的默认值若与你的版本不符,以 `system.settings` 为准。

## 07.7 窗口函数与帧语法

窗口函数(`OVER`)在 `SELECT` 阶段求值,所以它能"看见"分组后的行、又能在其中按顺序做滑动计算——这是 `GROUP BY` 做不到的(`GROUP BY` 把行塌缩成组,窗口则保留每一行再附上计算结果)。语法骨架:

```sql
SELECT
    user_id,
    ts,
    row_number() OVER (PARTITION BY user_id ORDER BY ts)        AS seq,
    sum(amount)  OVER (PARTITION BY user_id ORDER BY ts
                       ROWS BETWEEN 2 PRECEDING AND CURRENT ROW) AS rolling_3
FROM orders;
```

三个部件各有含义:`PARTITION BY` 把行切成窗口组(类似 `GROUP BY`,但不塌缩);`ORDER BY` 决定组内顺序;`ROWS BETWEEN ... AND ...` 是**帧(frame)**——指定"当前行要参与计算的是哪一段"。帧省略时的默认值**随是否写 `ORDER BY` 而变**(有 `ORDER BY` 时默认"从组首到当前行",无则整组),这个差异是窗口函数最常见的坑之一。

这里只记住两件事:其一,`OVER (...)` 是**独立于 `GROUP BY` 的求值阶段**(见 §07.1 里 `WINDOW` 的位置),所以 `WHERE` 里不能引用窗口函数的别名;其二,窗口函数**保行数**,想"每个用户取最近 3 条"用 `LIMIT 3 BY user_id`(§07.8)通常比 `row_number()` 便宜得多。帧语法、命名窗口(`WINDOW w AS (...)`)、各函数的边界行为都在第 16 章。

## 07.8 LIMIT n BY

`LIMIT n BY expr` 是"每个 `expr` 组合最多保留 n 行":

```sql
-- 每个用户最近 3 条记录(注意 ORDER BY 在 LIMIT BY 之前求值)
SELECT user_id, ts, url
FROM hits
ORDER BY user_id, ts DESC
LIMIT 3 BY user_id;
```

它等价于窗口函数 `row_number() OVER (PARTITION BY user_id ORDER BY ts DESC) <= 3`,但实现代价低得多,不需要真的维护窗口。

语法上,`LIMIT BY` 有两种等价的偏移写法,文档给出的首选形态是**把偏移写在行数前面**:

```sql
LIMIT [offset_value, ]n BY expressions
LIMIT n OFFSET offset_value BY expressions
```

例如 `LIMIT 1, 2 BY id` 与 `LIMIT 2 OFFSET 1 BY id` 等价(每组都跳过第 1 行、再取 2 行)。`n` 也可以写负数,表示"从组尾数起"(如 `LIMIT -2 BY id`)。再叠一层总行数上限就写成 `LIMIT n BY expr LIMIT total`;`LIMIT n BY ALL` 则是"按 `SELECT` 里所有非聚合表达式分组"的简写。细节见第 16 章。

**没有 `ORDER BY` 的 `LIMIT BY` 是随机的**:多线程处理下行的顺序不保证,留下的 n 行是哪 n 行不确定。

同一个原因也落在更朴素的 `LIMIT`/`OFFSET` 上,而且更常被忽略:**只写 `LIMIT n`(不带 `ORDER BY`)返回的"是哪 n 行"同样不确定**。读取按 part、按线程并行推进,同一张表这次给你这 10 行、下次可能是另外 10 行;`LIMIT 10000, 10` 这种深分页更糟——每次查询取到的是不同的 10010 行,翻页时既可能漏行、也可能重复,而结果集本身"看起来"没错。要分页稳定,必须给一个**全序**的排序键(`ORDER BY` 里带上能区分并列的列,比如主键),并且优先用键集分页(记住上一页最后一行的键值、从它后面接着取)而不是靠 `OFFSET` 跳过前 m 行。`LIMIT BY` 的随机性与 `LIMIT` 的随机性,是同一件事的两处表现。

## 07.9 结果补全与尾随:`LIMIT ... WITH TIES` 与 `ORDER BY ... WITH FILL`

有两个"扩展版"子句值得单独点出,它们都改变了朴素语义。

`LIMIT n WITH TIES` 与普通 `LIMIT n` 只差一点:取满 n 行后,把**与第 n 行排序键并列**的后续行也一并返回。它**必须配合 `ORDER BY`**——否则"并列"无从定义。

```sql
SELECT user_id, score
FROM leaderboard
ORDER BY score DESC
LIMIT 3 WITH TIES;   -- 第 3 名若并列,后续并列者也返回
```

它的用途是"取前三名"这类业务口径:若第 3、4、5 名同分,你希望都拿到,而不是按物理顺序随机截断。

`ORDER BY ... WITH FILL` 则反过来——**补全缺失的行**。想要一条连续的时间序列(哪怕某些时间点没有数据)时,普通 `ORDER BY` 只返回存在的行:

```sql
SELECT toDate(ts) AS d, metric
FROM daily
ORDER BY d WITH FILL FROM toDate('2025-01-01') TO toDate('2025-01-07') STEP 1
INTERPOLATE (metric AS 0);
```

`WITH FILL` 按 `FROM`/`TO`/`STEP` 在排序键上补齐空缺行,`INTERPOLATE` 决定补出来的行各列取什么值(不写 `INTERPOLATE` 就填类型默认值,写了则用指定表达式)。这一对的完整行为、`STALENESS` 的语义、以及它在聚合后序列上的表现,都在第 16 章。

## 07.10 ARRAY JOIN 与 LEFT ARRAY JOIN

`ARRAY JOIN` 把数组列"展开成多行"——这是 ClickHouse 里替代 `UNNEST` 的写法:

```sql
SELECT user_id, tag
FROM events
ARRAY JOIN tags AS tag;
```

数组为空的行会被**丢弃**;`LEFT ARRAY JOIN` 则保留它们(用默认值填展开列)。数组列也可以与普通列混着写,普通列会被复制到展开出的每一行上。展开后行数会变化,所以在 §07.1 那张逻辑依赖表里,它排在 `FROM` 之后。

这里有两处不能当定理。其一,**"ARRAY JOIN 执行在 `PREWHERE`/`WHERE` 之前"只是逻辑顺序的一种写法,不是执行计划的硬约束**:官方文档明确说明,优化器既可以把 `ARRAY JOIN` 放在 `WHERE`/`PREWHERE` **之前**执行,也可以放在它们**之后**执行(取哪种更省);真正可靠的边界只有"它在 `FROM` 之后"。其二,**一个查询里只允许一个 `ARRAY JOIN` 子句**——一个子句里可以并列展开多个数组(`ARRAY JOIN a, b`),但不能把 `ARRAY JOIN` 写两遍再叠起来。

**`arrayJoin()` 函数 vs `ARRAY JOIN` 子句**是另一个常被混的点。二者都做"数组展开成多行",但作用范围完全不同:`ARRAY JOIN` 是**子句**,作用于写进它的那些数组列(每行按数组长度裂开),可以同时展开多列;`arrayJoin(arr)` 是**函数**,出现在 `SELECT`/`WHERE` 的表达式里,把传入的**那一个**数组就地展开——效果相当于一个隐式的 `ARRAY JOIN`:

```sql
-- 子句:把已有的数组列 tags 展开成多行
SELECT user_id, tag FROM events ARRAY JOIN tags AS tag;

-- 函数:在 SELECT 表达式里就地展开一个数组
SELECT arrayJoin([1, 2, 3]) AS x;      -- 得到 3 行
```

经验法则:要在**表里已有的数组列**上展开,用 `ARRAY JOIN` 子句;要对**表达式算出来的数组**(如 `arrayJoin(splitByChar(',', csv))`)展开,或要在 `WHERE` 里对数组做展开过滤,用 `arrayJoin()` 函数。两者的展开时机都在 `FROM` 之后;至于与 `PREWHERE`/`WHERE` 的先后,如上一段所说,由优化器决定,不是一条固定顺序。混用时尤其要留意行数会变化。

## 07.11 DISTINCT 与 DISTINCT ON

`SELECT DISTINCT` 对**整行结果**去重。`SELECT DISTINCT ON (a, b)` 则只对指定的列组合去重,效果等价于 `LIMIT 1 BY a, b`:

```sql
SELECT DISTINCT ON (user_id) user_id, ts, url FROM hits;
```

`DISTINCT ON (...)` 与 `LIMIT BY` 同时出现时的行为**本条未确认** —— 官方文档分别给出了 `DISTINCT`(去重,`DISTINCT ON (...)` 按指定列去重但返回全部字段)与 `LIMIT n BY 列`(每个列值各取前 n 行)的定义,也明确说 `LIMIT BY` 与 `LIMIT` 互不相干、可以同用,但没有为 `DISTINCT ON (...)` 与 `LIMIT BY` 混写给出专门语义或示例,所以请避免混写。要理解 `DISTINCT` 与 `GROUP BY` 的差别,得看它们各自留住什么中间状态:**`DISTINCT` 的机制是把所有出现过的值放进一个哈希集合**,它要记住的是"去重后一共有哪些值",所以内存正比于**去重值的个数**;而 `GROUP BY` 可以只保留聚合状态——配合 `AggregatingMergeTree` 或近似函数时,一个聚合状态列远比它代表的那些去重值本身小,于是同样一件事往往能用更少的内存做完。也正因如此,在需要"去重计数"的场合,`uniq` 系列这类"只留一个近似状态"的函数比 `count(DISTINCT ...)`(后者本质上还是要维护那个哈希集合)划算得多(第 12 章)。

## 07.12 UNION / INTERSECT / EXCEPT

这三个是集合运算符,都要求两边**列数相同、顺序相同、类型相容**。

`UNION ALL` 只是把两个结果流拼起来,廉价。`UNION DISTINCT` 还要对合并结果**去重**,通常意味着一次落盘或一次大的哈希聚合,代价高一个量级。

这里有一个与直觉相反的事实:**裸写 `UNION`(既不写 `ALL` 也不写 `DISTINCT`)在 ClickHouse 里不会静默等同于 `DISTINCT`,而是直接报错**——除非设置了 `union_default_mode`。该设置的默认值是**空**,报错文案是 `Expected ALL or DISTINCT, because setting (union_default_mode) is empty`;把它设成 `'ALL'` 或 `'DISTINCT'` 之后,裸 `UNION` 才会按那个模式解释。

三个集合运算符的默认模式要分两套记,别混成一句:`union_default_mode` 的默认值是"空",也就是**必须显式写 `ALL` 或 `DISTINCT`**,否则报错;而紧邻的 `INTERSECT`/`EXCEPT`,默认模式由 `intersect_default_mode`、`except_default_mode` 决定,两者的默认值都是 `'ALL'`(见下一段)。所以同一句话里"默认"的含义不同——`UNION` 默认拒绝执行,`INTERSECT`/`EXCEPT` 默认按 `ALL` 执行。实际写法上:**"两个结果不会重复"时一定写 `ALL`**,`DISTINCT` 留给确实需要去重的场合。

`INTERSECT` 取两边都有的行,`EXCEPT` 取"只在左边有的行"。**这两个的默认语义与标准 SQL 相反**:标准 SQL 里 `INTERSECT`/`EXCEPT` 隐含去重,而 ClickHouse **默认为 `ALL`**——结果里**可能保留重复行**。要拿到标准 SQL 那种去重后的集合,必须显式写 `INTERSECT DISTINCT` / `EXCEPT DISTINCT`。默认模式也由设置 `intersect_default_mode`、`except_default_mode` 控制。

```sql
SELECT id FROM a INTERSECT DISTINCT SELECT id FROM b;   -- 去重后的交集
SELECT id FROM a EXCEPT ALL        SELECT id FROM b;    -- 保留重复的差集
```

还有一条优先级规则:`INTERSECT` 比 `UNION`/`EXCEPT` **结合得更紧**(后两者优先级相同、从左到右结合)。所以 `a UNION b INTERSECT c` 会被算成 `a UNION (b INTERSECT c)`,而不是 `(a UNION b) INTERSECT c`;要后者就显式加括号。

另外,`EXCEPT(col1, col2)` 这种带列清单的写法**不是差集**——它来自 BigQuery 风格,是 `SELECT * EXCEPT (...)`,用于从星号展开里去掉几列(§07.4),两者别混。

## 07.13 SAMPLE

```sql
SELECT count() * 10 FROM hits SAMPLE 0.1;      -- 按比例
SELECT count() * 10 FROM hits SAMPLE 100000;   -- 按行数
```

`SAMPLE` 只对 **MergeTree 家族**有意义,因为它依赖建表时的 `SAMPLE BY`——采样键把数据划成互不重叠的块,取一部分块就等于取了一个可复现的随机子集。**在没有声明 `SAMPLE BY` 的 MergeTree 表上写 `SAMPLE` 子句会直接报错**(ClickHouse 找不到采样键,没法把行映射到桶),而不是退化成"随机抽一部分行";非 MergeTree 引擎上同理也会报错,因为"随机抽一部分行"本就不是它们的读语义(第 04 章 §04.1)。

把"按桶取"讲具体一点。假设建表时声明 `SAMPLE BY cityHash64(user_id) % 100`(一个 0–99 的整数桶号),那么每一行在写入时就被算进某个桶,数据在物理上也按桶号聚在一起。`SAMPLE 0.1` 不是"随机挑 10% 的行",而是**按桶号圈定一批桶**(大致是桶号落进一个宽度为 0.1 的区间的那些),把落在这几个桶里的行整桶取走——所以同一个查询跑两次,圈中的是同一批桶、结果一致;而"每个桶有多少行"取决于数据分布,桶大小不齐,因此"大致 10%"并不等于"精确 10%"。可以这样亲手验证:

```sql
CREATE TABLE hits (user_id UInt64, url String, ts DateTime)
ENGINE = MergeTree
ORDER BY (cityHash64(user_id) % 100, ts)   -- 采样表达式必须出现在主键里(主键默认即整个排序键,放前缀或末尾都行)
SAMPLE BY cityHash64(user_id) % 100;

INSERT INTO hits SELECT number, concat('/p/', toString(number)), now() FROM numbers(100000);

SELECT count() FROM hits SAMPLE 0.1;        -- 约 10%:取中的那批桶里的行
```

反过来,如果直接把业务列 `user_id` 当桶号(不套哈希),几乎每个用户自己就是一个桶,`SAMPLE 0.1` 圈中的"10% 桶"其实只剩极少数行、比例完全失去意义——这正是实践中采样键几乎总是一个哈希列的原因。

## 07.14 FINAL

`FINAL` 是加在表引用后的一个修饰:`SELECT ... FROM t FINAL`。它的语义是"**返回结果前,把同一排序键上的多份数据按引擎家族自身的规则归并成最终形态**"——`ReplacingMergeTree` 保留版本最大(`ver` 列)的那条、`SummingMergeTree` 把数值列按排序键求和、`CollapsingMergeTree` 按 **sign 列**折叠:同一排序键下 `sign = +1` 与 `sign = -1` 的行两两抵消,配不成对的行按各自规则保留,而不是"按某个折叠标记做配对删除"。没有 `FINAL`,这些引擎的合并是**后台异步**发生的,你查到的是"合并到哪一步就是哪一步"的中间状态。对没有家族合并逻辑的普通 `MergeTree`,`FINAL` 基本是空操作。

```sql
SELECT * FROM users_replacing FINAL;   -- 保证看到去重后的最终行
```

**它的代价要按 25.3 的现状说,不能照搬老经验。** 早期(21.x 及更早)的实现确实容易退化:那时 `FINAL` 把每个分区的数据整体读出来、在内存里按键归并,`WHERE` 里的键条件常常来不及参与裁剪,"大表上慢一个数量级"的说法由此而来。**后来版本里的一次 `FINAL` 算法重写**换掉了这条路(这次重写的引入与稳定版本**本条未确认**,不再给出具体版本号):现在的 `FINAL` 仍然会先读 `primary.idx`、按排序键把要读的 part 与 granule 先裁掉,只对裁剩下的部分做归并;也就是说,**"`FINAL` 一律让稀疏索引失效、`WHERE` 一定用不上键裁剪"这句老话不再普遍成立**。它依然比不带 `FINAL` 的查询贵——归并要额外读排序键列、多占内存,并行度上限由 `max_final_threads` 单独控制(不跟随 `max_threads` 自动放大)——但代价的量级取决于"同一个排序键被拆在多少个 part 里、这些 part 又跨了多少个分区":**同一个键的版本越分散,`FINAL` 越贵。**

落到几种具体场景上,各有各的行为:

- **带 `WHERE` 的 `FINAL`**:键上的条件照常进裁剪(见上),所以"先按范围过滤、再 `FINAL`"是有效写法;真正贵的是那些必须把所有版本拉齐才能定的列。
- **`max_final_threads`**:控制 `FINAL` 归并阶段的并行线程数,默认值随版本变过,判断本机取值以 `system.settings` 为准;调大能加快大表 `FINAL`,代价是内存。
- **`Distributed` 表上的 `FINAL`**:`FINAL` 会被**下推到每个分片**、由各分片先在自己的本地表上做,再在发起节点归并各分片的结果,而不是把全部原始数据集中到一处再合并。这让它比"全量拉过来再归并"省得多,但**同一个排序键若跨分片,仍要靠发起节点收口**——分片键与排序键不一致时要留意这一点。
- **`do_not_merge_across_partitions_select_final`**:设成 1 时,`FINAL` 只在**各分区内部**分别归并,不再跨分区把同一个键的不同版本拼到一起。好处是各分区能更大程度并行、内存更省;代价是它**只有在"同一个排序键不可能跨分区"时才正确**,也就是分区键是排序键前缀(或至少是排序键的一部分)的时候。默认值是 0(仍然跨分区归并),不过它还有一个配套开关 `enable_automatic_decision_for_merging_across_partitions_for_final`(默认 `1`),会**自动判断能否安全地开启这项优化、在满足条件时替你打开**——所以"默认值 0"并不等于"默认一定跨分区归并"。它是"确认过数据分布之后再开"的优化开关;这个配套开关在 25.3 上是否存在、以及两者在各发行版上是否还有进一步的默认值调整,**本条未确认**,用前查 `system.merge_tree_settings`。
- **软删除列 `is_deleted`**:`ReplacingMergeTree(ver, is_deleted)` 里的 `is_deleted`(ClickHouse 23.8 起,配套实现 `engines.py` 里也是这个版本口径)让 `is_deleted = 1` 的行在归并时**整组丢弃**,于是"删一行"变成"追加一条 `is_deleted = 1`、`ver` 更大的行"。查询带 `FINAL` 时这条删除立刻生效,不必等后台合并;它和第 09 章 §09.3 的轻量级 delete 是两条不同的实现路径(后者用 `_row_exists` 掩码,不重写 part)。

也正因为它现在没那么"污染全表"了,更需要说清它**为什么通常仍不是答案**:

- **它掩盖了真正的问题。** 如果一条查询**必须**加 `FINAL` 才正确,往往说明上游写入没有去重,或者这条查询本就该走物化视图/预聚合。把 `FINAL` 当常规写法,等于放弃了 ClickHouse 的写入模型。
- **代价仍在,只是换了个形状。** 即便 23.x 之后键裁剪照常,`FINAL` 依然是"读时才收敛":同一个键的版本越分散越贵,`WHERE` 之外的列也要参与归并。把它当默认写法,大表上迟早撞到延迟。

正确的替代路线(按优先级):让写入幂等(块级去重、`insert_deduplication_token`,第 08 章 §08.6);用 `ReplacingMergeTree` 并接受"最终一致",只对**必须精确**的那条查询加 `FINAL`;或者把去重结果物化成表/物化视图。`FINAL` 的合并算法细节、它与后台合并 / TTL 如何互相排队,在第 18 章;相关的调参与排障视角在第 28 章。

## 07.15 SETTINGS 与 FORMAT

语句末尾的 `SETTINGS` 子句只作用于**这一条查询**:

```sql
SELECT count() FROM hits SETTINGS max_threads = 2;
```

这个"只作用于这一条查询"包括它内部的各个子查询:写在子句尾部的 `SETTINGS` 是这条查询(以及它展开出来的子查询)的局部设置,不会渗到会话里的其他语句,也不会影响别的连接。它同时**覆盖服务端配置与用户 profile 里的默认值**:同一个设置名,语句级的 `SETTINGS` 最高,压过会话级的 `SET`,而会话级的 `SET` 又压过服务端 `config.xml` / 用户 profile 里的默认值。所以"我明明在配置里改了默认值,查询却还按旧值走"时,先看这条查询自己有没有 `SETTINGS`。

查某个设置**当前**生效的值,最直接的是 `system.settings`:

```sql
SELECT name, value, changed FROM system.settings WHERE name = 'max_threads';
```

`changed` 这一列会告诉你它是被显式改过、还是仍取默认。要留意 `EXPLAIN` 的输出里**看不到** `SETTINGS`(它只打印查询计划),所以"这条查询到底用了哪些设置"不能靠 `EXPLAIN`,得看 `system.settings` 的当前值,或去 `system.query_log` 里查它执行时真正记录下来的那一份。

它的优先级高于会话级的 `SET`。**错误做法**:用 `SET` 全局改一个会影响所有查询的设置(比如 `max_threads`)。**后果**:别的查询莫名其妙变慢或抢不到资源。**正确做法**:默认用 `SET` 只改当前会话,针对单条语句的调整写在该语句的 `SETTINGS` 里。

`FORMAT` 决定结果的序列化形式,必须是语句的最后一部分(第 19 章)。

## 07.16 SELECT ... INTO OUTFILE

`SELECT` 的结果除了回给客户端,还能直接落文件——`INTO OUTFILE` 写在语句末尾(和 `FORMAT` 一类):

```sql
SELECT user_id, count()
FROM hits
GROUP BY user_id
INTO OUTFILE '/tmp/agg.csv' FORMAT CSV;
```

有一个**关键事实**容易搞反:文件写在**客户端**,不是服务端。这个子句只在命令行客户端 `clickhouse-client` 与 `clickhouse-local` 里生效——**走 HTTP 接口执行会失败**。所以"把结果存到服务器机器上"不是它的用途;要那样做得另行设计导出路径。

完整语法:

```text
INTO OUTFILE file_name
    [AND STDOUT]
    [APPEND | TRUNCATE]
    [COMPRESSION type [LEVEL level]]
```

几个可选项:`AND STDOUT` 让结果同时打到标准输出(压缩时打到 stdout 的是明文);目标文件已存在时**必须**显式二选一——`APPEND` 追加或 `TRUNCATE` 覆盖,否则报错;`COMPRESSION type [LEVEL n]` 指定压缩算法(如 `COMPRESSION 'gzip' LEVEL 9`,不写时按扩展名推断),并且 **`APPEND` 不能与压缩同时用**。不写 `FORMAT` 时按默认输出格式 `TabSeparated` 落盘(第 19 章)。

## 07.17 IN 的右操作数、GLOBAL IN 与 = ANY / = ALL

`IN` 的右边可以是三种东西:

- **元组/列表**:`WHERE id IN (1, 2, 3)`;
- **子查询**:`WHERE id IN (SELECT id FROM allowed)`;
- **表名**:`WHERE id IN allowed_ids`——按那张表当作集合来用(典型是 `Set` 引擎的表,见第 04 章)。

三者的执行方式不同,但都要注意一件事:**右边的集合会被整体读进来**,所以拿"一百万行的表"当 `IN` 的右操作数,代价远高于把它 `JOIN` 起来。反过来,对小的白名单用 `Set` 表或 `IN (subquery)`,比 `JOIN` 划算。

**`GLOBAL IN` / `GLOBAL NOT IN`** 是针对**分布式**查询的一个变体。普通的 `IN (subquery)` 会连着子查询一起发给每个分片、在每片上各自算一遍右表;`GLOBAL IN` 则先把右表**在发起节点算成一份临时集合**,再分发到各分片复用。所以右表小、分片多时 `GLOBAL IN` 往往更省;右表很大时分发本身的代价又会反噬。哪种更优取决于分布形态与数据量,细节与实测在第 23 章。

**`= ANY(subquery)` / `= ALL(subquery)`** 是同一件事的另一种写法。语义上:`a = ANY (subquery)` 等价于 `a IN (subquery)`,`a != ANY (subquery)` 等价于 `a NOT IN (subquery)`;`a = ALL (subquery)` 要求右边**所有**行都满足 `=`,`a != ALL (subquery)` 等价于 `a NOT IN`。用法示例:

```sql
SELECT * FROM t WHERE x = ANY (SELECT id FROM allow_list);
SELECT * FROM t WHERE x > ALL (SELECT threshold FROM bounds);
```

注意两点:`ANY` 有一个标准 SQL 的同义词 `SOME`;而当右操作数是**返回多行的子查询**、你却漏写了 `ANY`/`ALL` 时,ClickHouse 会直接报 `Expected ALL or ANY qualifier in comparison with subquery`(`EXPECTED_ALL_OR_ANY`),而不是替你猜。

## 07.18 子查询

**标量子查询**用在需要一个值的位置:`SELECT (SELECT max(ts) FROM hits) AS latest`。这里有一条与 SQL 标准不同的行为:子查询返回**零行**时,它不报错,而是给出"默认值"。准确地说,ClickHouse 会把标量子查询的结果类型变成 `Nullable`,空结果就是 `NULL`;如果该类型**无法转成 `Nullable`**(比如 `Array`、`Tuple`),才会抛出 `INCORRECT_RESULT_OF_SCALAR_SUBQUERY` 错误。想给一个具体默认值就用 `coalesce((SELECT ...), 'none')`。

**`IN (subquery)`** 是半连接,语义清楚、性能可控。

**`EXISTS (subquery)`** 用于"存在性判断"。

最后是一条硬限制:**相关子查询不支持**——子查询内部**不能引用外层的列**。写出来的结果是"未知标识符"这类错误,而不是"能跑但慢"。需要相关语义时,正确的替代是 `JOIN`、`IN` 或者窗口函数。

## 07.19 与 mini-clickhouse 的对照

`mini-clickhouse` 的 `SelectQuery`(`mini-clickhouse/minich/sql/ast_.py`)字段与真实 ClickHouse 的 `ASTSelectQuery` 同构:`with_exprs`、`distinct`、`selects`、`from_`、`array_joins`、`prewhere`、`where`、`group_by`、`having`、`windows`、`order_by`、`limit_by`、`limit`、`offset`、`settings`、`unions` + `union_distinct`、`into_outfile`、`format`、`final`,并且 `to_sql()` 能回打成原文。

**对齐点。** 解析器(`parser.py`)覆盖了 `WITH`、`SELECT * EXCEPT/REPLACE`、`ARRAY JOIN`(`ArrayJoinExpr`,带 `left` 标志)、`PREWHERE`、`WHERE`、`GROUP BY`(含 `WITH ROLLUP` / `WITH CUBE` / `WITH TOTALS` / `GROUPING SETS` / `GROUP BY ALL`,见 `GroupByClause`)、`HAVING`、`WINDOW`、`ORDER BY`(含 `COLLATE`)、`LIMIT BY`(`LimitByClause`)、`LIMIT`/`OFFSET`、`SETTINGS`、`UNION [ALL|DISTINCT]`、`FORMAT`、`SAMPLE`、`FINAL`,`EXISTS (subquery)` 被解析成一个名为 `exists` 的函数调用。别名机制是 §07.2 里"别名被内联展开"的字面实现:`analyzer.py` 的 `extract_aliases()` 先收 `WITH` 里的别名再收 `SELECT` 里的(后收集者覆盖同名者),`expand_aliases()` 递归地把别名替换回表达式,并用一个深度上限(超过 100 层抛 `LOGICAL_ERROR: Cyclic aliases detected`)防环。`LIMIT BY` 与 `ORDER BY` 的执行次序与真实 ClickHouse 一致:`InterpreterSelectQuery._execute_pipeline()` 的顺序是 `ARRAY JOIN → PREWHERE → WHERE → 聚合 → 窗口 → SELECT 表达式 → DISTINCT → ORDER BY → LIMIT BY → OFFSET/LIMIT`(窗口函数**在 SELECT 表达式之前**求值,因为 SELECT 列表里的窗口节点要等窗口算完才能换成列引用),所以 `ORDER BY ts DESC LIMIT 3 BY user_id` 这个标准写法能得到与真实 ClickHouse 相同的结果(第 16 章 §16.6)。`_apply_query_settings()` 把语句末尾的 `SETTINGS` 应用到本次查询,与 `SET` 的会话级设置分开存放,这条语义也一致。

不过 mini 有一条**已知缺陷**要如实记下:`HAVING` 子句被**整体忽略**。`analyzer.py` 会把 `HAVING` 收进 `QueryAnalysis.having`(并放进"聚合后求值"的表达式清单),但 `_execute_pipeline()` 里**没有任何阶段消费它** —— 也就是说 `SELECT b, count() FROM t GROUP BY b HAVING count() >= 2` 会把所有组都返回,连 `HAVING 0` 都拦不住(仓库里有一条 `xfail` 测试明确标注"已知缺陷:`HAVING` 子句被整体忽略")。上面那句"聚合 → 窗口 → SELECT 表达式"的顺序里**没有 HAVING 这一步**,正是这个原因。顺带说一句:mini 的 `EXPLAIN` 里虽然会打印一行 `Filter (HAVING ...)`,但那**只是一行装饰、并不真的执行**。

**简化点。** 几处刻意的取舍:

- **窗口函数与 `FINAL` 是真实现了**:`WindowFunction` / `WindowSpec` / `WindowFrame` 在 AST 里齐全,`InterpreterSelectQuery._apply_windows()` 用 `WindowTransform` 求值(§07.7 的例子能跑);`FROM t FINAL` 也实现了(`ReadContext.final`,读取时就地归并去重,代价与真实一样大)。
- **`LIMIT ... WITH TIES`、`ORDER BY ... WITH FILL` / `INTERPOLATE`、`INTERSECT` / `EXCEPT` 都没有实现**——`parser.py` 的模块文档只把其中的 `WITH FILL` 与 `LIMIT ... WITH TIES` 明确列进"不做"的清单(那份清单里还一并列了 `ASOF JOIN` 的完整语义与 `SETTINGS` 里的子查询),`INTERPOLATE`、`INTERSECT`/`EXCEPT` 不在清单上但同样不进语法。遇到这些写法都会抛带位置信息的 `SYNTAX_ERROR`,而不是解析成一棵错的树。
- **`QUALIFY`(§07.1)与 `APPLY`(§07.4)也没有实现**:两者都不在 `parser.py` 的语法里,写下去会被当成语法错误。
- **`INTO OUTFILE` 只在 `SelectQuery.into_outfile` 里被解析成一个字符串字段**,没有落文件的行为。
- **`DISTINCT ON (...)` 没有实现**:`SelectQuery.distinct` 只是一个布尔值(`query.distinct = self.accept_keyword("DISTINCT")`),没有带列清单的形态,因此会解析失败。
- **`SELECT` 里的聚合判定是粗判**:`SelectQuery.is_aggregating()` 只在语法层探测"有没有 `GROUP BY`、`SELECT` 里有没有聚合函数名",真正的判定在 `analyzer.py`(那里才知道 `HAVING`/`ORDER BY` 里是否出现聚合,并会为无 `GROUP BY` 的聚合查询报错)。别名与真实列同名时的优先级设置(`prefer_column_name_to_alias`)只在 `settings.py` 里作为一个设置项存在,展开逻辑本身是按"别名赢"来做的。
- **`SAMPLE` 被解析成表引用的属性**(`TableReference.sample`),是否真的按采样键裁剪取决于存储引擎;`ReadContext.sample` 是留给引擎的接口。

## 07.20 小结

`SELECT` 的书写顺序与求值顺序无关:先 `WITH`、`FROM`、`SAMPLE`、`ARRAY JOIN`、`JOIN`,再 `PREWHERE`/`WHERE`,再聚合与 `HAVING`,再窗口、`QUALIFY` 与 `SELECT`,然后 `DISTINCT`、`ORDER BY`、`LIMIT BY`、`LIMIT`。这条依赖链解释了"为什么 `WHERE` 里不能引用聚合结果""为什么 `LIMIT BY` 要写在 `ORDER BY` 后面",以及"采样发生在连接之前"。但它是**依赖关系表,不是执行计划的算子顺序**:`ARRAY JOIN` 相对 `WHERE`/`PREWHERE` 的先后由优化器决定(§07.10),计划里各算子的排布也要单独看。别名在 ClickHouse 里可以被多个子句引用,代价是它与真实列同名时默认由别名胜出(`prefer_column_name_to_alias` 控制)。`WITH` 的命名子查询是内联展开,不会只算一次。`PREWHERE` 的收益来自"少读列",通常由 `optimize_move_to_prewhere` 自动完成。

几个易混点值得单独记:窗口函数(`OVER (... PARTITION BY ... ORDER BY ... ROWS BETWEEN ...)`,§07.7)在 `SELECT` 阶段求值,`WHERE` 用不到它;`LIMIT n BY`(§07.8)通常比等价的窗口函数便宜,而 `ORDER BY` 与 `LIMIT`/`OFFSET` 若不给全序排序键,结果顺序(乃至"留下哪几行")都是不确定的;`LIMIT ... WITH TIES` 与 `ORDER BY ... WITH FILL`/`INTERPOLATE`(§07.9)分别做"尾随并列"与"补齐空缺";`arrayJoin()` 函数与 `ARRAY JOIN` 子句(§07.10)是同一件事的两种入口,且一个查询只允许一个 `ARRAY JOIN` 子句;`GROUP BY`/`ORDER BY` 里的**序数位置**(§07.6)能用但别写进生产;`INTERSECT`/`EXCEPT`(§07.12)在 ClickHouse 里**默认 `ALL` 而非去重**,与标准 SQL 相反,而裸 `UNION` 会直接报错;`FINAL`(§07.14)能把"最终一致"变"立即一致",23.x 算法重写后它仍会用 `primary.idx` 裁剪、代价取决于同一个键被拆散的程度,但通常仍不是正解;`INTO OUTFILE`(§07.16)写的是**客户端**文件、HTTP 接口用不了;`GLOBAL IN` 与 `= ANY`/`= ALL`(§07.17)是 `IN` 的两种扩展。

少数几条限制要记牢:相关子查询不支持,标量子查询空结果返回 `NULL` 而不是报错,`SAMPLE` 只对带 `SAMPLE BY` 的 MergeTree 有意义。
