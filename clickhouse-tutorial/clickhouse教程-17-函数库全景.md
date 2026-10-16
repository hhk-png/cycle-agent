# 第 17 章 函数库全景

写查询时真正卡住你的通常不是语法,而是"这件事该用哪个函数"。ClickHouse 的函数有一千多个,背下来既不可能也没必要。需要的是两张地图:一张告诉你**每一族函数管哪一类问题**,另一张告诉你**每一族里最容易踩的坑**。这一章就是这两张地图;按名字排列的完整速查表在附录 A(第 33 章)。

## 17.1 函数系统的四块地基

**函数名大小写不敏感。** `Length`、`LENGTH`、`length` 是同一个函数——名字统一按小写存放,查询时也小写化再查。

**运算符就是函数。** `a + b`、`a = b`、`a AND b` 在解析后全部变成函数调用:`plus(a, b)`、`equals(a, b)`、`and(a, b)`。所以凡能写函数的地方都能写运算符,下文讨论"比较的语义"时,说的其实是 `equals` 在 `NULL` 上的行为。

**求值单位是列,不是行。** 函数的执行接口拿到的是"一整列",不是"一行几个值"。好处是整列可以一次算完;可见的代价是:函数对 `NULL`、对越界、对类型不匹配的处理必须**对整个块一致**——所以 `arrayElement(arr, 999)` 不抛异常,而是返回该元素类型的默认值(§17.11)。

**结果类型由参数类型推出。** `UInt8 + UInt8 = UInt16`,`Date + 1 = Date`,`0 - 0 = Int16`。推错不是"算错一个值",而是下游的序列化、压缩、比较**全部错位**;这一族实现里九成代码都在做类型推导。

**先说清楚本章的范围。** 这一章讲的是**标量函数**——一行进、一行出的表达式。有两类东西被有意排除在外,免得你翻半天找不到:**聚合函数**(两级聚合、`-State`/`-Merge` 组合子、物化视图增量聚合)属于第 12 章;把外部数据源当成表的**表函数**(`numbers`、`file`、`s3`、`remote`…)只在 §17.19 点到名字,细节看第 08 章与第 19 章。知道这个边界,才不会在"为什么这里找不到 `uniq` 和 `numbers`"上浪费时间。

## 17.2 函数名的组合子:一个基函数拼出几十个变体

把 `sum`、`uniq`、`count` 放大成几百个"函数"的,不是更多算法,而是**后缀组合子**(combinator):在基函数名后面接一个后缀,就得到一件新事。这套机制第 12 章 §12.7 在聚合那一层已经列过,放到标量/聚合函数这一层同样成立 —— 读函数表时,凡是看到"没见过的名字",先拆后缀,往往就不必单独记了。

出现频率最高的一组:

- **`-If`** 追加一个"条件列",为假的行跳过。`countIf(cond)`、`sumIf(x, cond)`、`uniqIf(x, cond)`、`avgIf`… 是日常里出现最多的一族。
- **`-Array`** 让聚合函数接收 `Array(T)` 型实参,并把**所有数组的全部元素并进同一次聚合**——跨元素,也跨行。`SELECT sumArray(arr) FROM t` 读的是"整列每一行的数组元素之和",它把 N 行的数组当成一个大池子来累加;虽然与逐行 `sum(arraySum(arr))` 结果相同,但这里只发生一次聚合。更能说明"跨行"这层的是 `uniqArray(arr)`:它对**所有行**的数组元素做一次全局去重计数,而不是每行各数一遍。别把普通数组函数误当成组合子:`has(arr, x)`(数组是否含某元素)、`arrayMap(x -> abs(x), arr)`(逐元素取绝对值)、`arrayFilter` 都是**普通的数组函数**,`-Array` 并不派生出 `absArray`、`hasArray` 这样的名字。
- **`-State` / `-Merge` / `-SimpleState` / `-MergeState`** 这一族是增量聚合的地基:`-State` 把结果降级成 `AggregateFunction(...)` 类型的中间状态,`-Merge` 把多份状态结算回结果,`-SimpleState` 用普通类型存"状态恰好就是结果"的那类聚合,`-MergeState` 只合并状态、不改回结果。四个后缀的语义差别与为什么要这么分,第 12 章 §12.7 从聚合那一侧讲透了,这里只登记它们属于这一族。

其余组合器一句一个:

```text
-OrNull         无结果 / 空分组时返回 NULL(结果类型变 Nullable)
-OrDefault      无结果 / 空分组时返回该类型的默认值
-Resample       配合时序表按时间轴分桶后聚合(功能有限,很多版本只记住参数)
-ForEach        把"逐行"的聚合升级成"逐位置"的聚合,返回数组
-Distinct       先按整参数元组去重再聚合
-Map            参数是 Map 时按 key 分别聚合(如 sumMap)
-ArgMin/-ArgMax 用一个额外实参把参与聚合的行限定为"该实参取最小/最大值"的那些行
```

这里要专门澄清 `-ArgMin`/`-ArgMax`:它们和**聚合函数** `argMin`/`argMax` 不是一回事。`argMin`/`argMax` 返回的是"极值所在那一行的某个列值";`-ArgMin`/`-ArgMax` 则是**组合子**,用一个额外实参把**参与聚合的行限定为"该实参取最小(最大)值"的那些行**,再对别的实参聚合——`sumArgMin(x, expr)` 是"在 `expr` 取最小值的那些行上对 `x` 求和",`countArgMin(expr)` 是数这些行的条数。

叠加的解析**是从右往左逐层剥后缀**(附录 A §A.15 讲的正是这套剥法),但后缀之间还有一条**位置约束**:`-Array` 必须**紧贴基函数名**,其余组合子只能在它之后接。所以"`uniq` + `-Array` + `-If`"的正确拼写是 **`uniqArrayIf(arr, cond)`**,不是 `uniqIfArray`——`-Array` 顶在 `uniq` 后面、`-If` 接在它后面。反过来读也一致:从 `sumArrayIfOrNull` 的右端开始剥,先剥掉 `-OrNull`、再剥 `-If`,剩下 `sumArray`,最后剥 `-Array` 得基名 `sum`;它表达的是"对全部数组元素求和、只算满足条件的行、空结果给 `NULL`"。记住"`-Array` 紧贴基名"这一条,就不会再写出顺序错位的名字。另有一条名字冲突规则:凡是**本身就被注册过的名字**(`sumMap`、`groupUniqArray`、`countDistinct`)优先命中注册表,不会被当成"基函数 + 组合子",否则 `sumMap` 会被误拆成不存在的"`sum` 套 `-Map`"。完整且随版本更新的组合器清单,直接查 `system.aggregate_function_combinators`;状态语义与例子见第 12 章 §12.7,这里不再重复。

## 17.3 参数化函数:常量实参决定了什么

有一类函数不走"普通实参"这条路,而是把**一部分实参在分析期就取出来当"参数"**,用它决定返回类型或算法行为,写成 `func(param)(arg)` 或 `func(arg, param)` 两种形态。它分两族:

**参数化的聚合函数**。`quantile(0.9)(x)`、`topK(5)(x)`、`histogram(3)(x)`、`sequenceMatch('(?1)(?2)')(ts, event)` —— 第一层括号里是"配置",第二层才是数据列。它们必须分开,是因为**参数要参与返回类型的推导**:`quantile(0.9)` 返回 `Float64`,而 `topK(5)` 返回的是数组 `Array(T)`;这些类型在查询**分析期**就要定死,不能等扫到数据再决定。

**类型参数化的标量函数**。`toDecimal64(x, 4)` 的第二个参数是 scale,直接决定返回 `Decimal(18, 4)` 还是别的精度;`toFixedString(s, 10)` 里的 `10` 决定长度;`toStartOfInterval(t, INTERVAL 5 MINUTE)` 的 `INTERVAL` 决定步长(注意它和"粒度别名" `toStartOfFiveMinutes` 是两条路);`CAST(x, 'UInt8')` 更直白 —— 第二个实参是一段**类型文本**,分析期解析成目标类型。

为什么这件事值得单讲?因为**参数必须是常量**。`toDecimal64(x, scale_col)` 里如果 `scale_col` 是一列,分析期拿不到值、推不出返回类型,会直接报错。这就回答了那个常被问的疑问:"为什么第二个参数不能是列"—— 不是实现偷懒,而是**列存引擎要求每一列的返回类型在扫描任何一行之前就定死**(§17.1 的第四块地基)。同一个函数常常**两种写法都可用**(`toDecimal64(x, 4)` 与 `toDecimal64(4)(x)`),但在系统表或别的库里可能只认其中一种,写跨库脚本时要留意。

## 17.4 算术与位运算

代表函数:`plus`/`minus`/`multiply`/`divide`/`intDiv`/`modulo`,以及 `bitAnd`/`bitOr`/`bitXor`/`bitNot`/`bitShiftLeft`/`bitRotateLeft`/`bitCount`/`bitTest`。

**小整数相加会变宽,64 位封顶后回绕。** `UInt8 + UInt8` 的结果类型是 `UInt16`,所以 `toUInt8(250) + toUInt8(10) = 260` 而不是 4——"进一档"挡住了绝大多数平凡溢出。但它只挡一档:`toUInt64(2**64-1) + toUInt64(1)` 回绕成 0。减法结果**永远有符号**(`toTypeName(0 - 0)` 是 `Int16`),否则 `0 - 1` 没法表示。

**`divide` 与 `intDiv` 是两个世界。**

```sql
SELECT divide(1, 2), intDiv(1, 2), intDiv(-7, 2), modulo(-7, 3), divide(1, 0);
-- 0.5   0   -3   -1   inf
```

**两个整数相除,`divide` 也返回浮点**(`Float64`);`intDiv` 是整除,向**零**截断(`intDiv(-7,2) = -3`,不是 `floor` 的 -4);`modulo` 的余数符号跟着被除数(C++ 的 `%`,不是 Python 的)。除数为 0 时 `divide` 给 `inf`/`nan`(不报错),`intDiv` 抛 `ILLEGAL_DIVISION`(Code 153)。不想让整条查询因分母为 0 挂掉,用 `intDivOrZero`/`moduloOrZero`(遇 0 返回 0)。

**位运算盯住位宽。** `bitNot(toUInt8(0)) = 255`,而 `bitNot(toUInt16(0)) = 65535`——取反是在该类型位宽内取反。`bitShiftRight` 对**有符号**数是**逻辑右移**:`bitShiftRight(toInt8(-1), 1) = 127`,不是 -1。

**算占比又取整时,别绕浮点。** 若先用 `divide(a, b)` 得到浮点再取整,会多一次浮点往返、并在边界处漂移;直接写 `intDiv(a, b)` 更准也更快,要容忍除零就用 `intDivOrZero`。

## 17.5 比较

代表函数:`equals`(`=`)、`notEquals`(`!=`/`<>`)、`less`/`greater`/`lessOrEquals`/`greaterOrEquals`、`isNotDistinctFrom`(`<=>`)、`greatest`/`least`。(两点版本提醒:一是在本书对标的 25.3 上,`isNotDistinctFrom`(`<=>`)还**只能出现在 `JOIN ON` 段**,当成通用表达式用会报错,通用支持的落地版本**未确认**(一说在 25.10 附近);二是它的**对偶** `isDistinctFrom` 在 25.3 上还没有,别照搬更新版本的函数名。详见本节末尾。)

**比较返回 `UInt8`,不是布尔类型。** `toTypeName(1 = 1)` 是 `UInt8`,所以比较结果可以直接求和(这就是 `countIf` 的写法),也能当位掩码用。

**`isNotDistinctFrom` 与 `=` 在 `NULL` 上的差别最重要。**

```text
a       b       a = b      a <=> b
─────   ─────   ────────   ────────
1       1       1          1
1       2       0          0
NULL    2       NULL       0        ← 能区分
NULL    NULL    NULL       1        ← 不能区分
```

`=` 回答"相等吗",`NULL` 表示"不知道",结果就是 `NULL`;`<=>` 回答"能区分开吗",`NULL` 也是可观察的值,结果确定是 0/1。

**但这里有个必须记住的版本边界(本书对标 25.3)**:在 25.3 上,`isNotDistinctFrom`/`<=>` **只能在 `JOIN ON` 段里用**,写进 `WHERE` 等通用表达式会直接报 `NOT_IMPLEMENTED: Function isNotDistinctFrom can be used only in the JOIN ON section`。**通用支持的落地版本本条未确认**(流传的说法是"25.10 才引入",但未从官方 changelog 复核,也拿不到可确证的 PR 号;与附录 A §A.5 的口径一致)。所以在 25.3 上想找"两列真的不一样的行",只能显式处理 `NULL`,例如 `... WHERE (s1.v != s2.v) OR (isNull(s1.v) != isNull(s2.v))`(注意 `!=` 遇 `NULL` 会给 `NULL`,还得配合 `isNull` 补齐);升级到 25.10+ 之后,才可以放心写 `... WHERE NOT isNotDistinctFrom(s1.v, s2.v)`。

字符串按**字节序**比较:`'Z' < 'a'` 是 1,`'10' < '9'` 是 1。`greatest`/`least` 是**标量函数**(不是聚合的 `max`/`min`),在一组参数里取最大/最小。它们**对 `NULL` 的处理在 24.12 改过一次**(PR #65519):现在**忽略 `NULL` 参数**,所以 `greatest(1, 2, NULL) = 2`;而在 **24.12 之前,只要有一个参数是 `NULL`,结果就是 `NULL`**。这个新默认与 PostgreSQL 一致,旧行为与 MySQL 一致。想回到旧行为,把设置 `least_greatest_legacy_null_behavior` 设成 `true`(该设置默认 `false`,PR #73344 引入)。从 MySQL 往 ClickHouse 搬的用户尤其要留意这条 —— 同一段 SQL 在 24.12 前后、或在不同设置下,`NULL` 的处理是反的。

## 17.6 逻辑与三值逻辑

代表函数:`and`、`or`、`not`、`xor`。这一节解释那个经典面试题——`NULL AND 0` 为什么是 0。

```text
  AND  │  F    NULL   T            OR   │  F    NULL   T          NOT
  ─────┼────────────────────         ─────┼────────────────────      ────────
  F    │  F     F     F              F    │  F    NULL   T          T → F
  NULL │  F    NULL  NULL            NULL │ NULL  NULL   T          F → T
  T    │  F    NULL   T              T    │  T     T     T          NULL → NULL
```

逐条读:`NULL AND 0 = 0`、`NULL AND 1 = NULL`、`NULL OR 1 = 1`、`NULL OR 0 = NULL`、`NOT NULL = NULL`。原因:逻辑里 `NULL` 代表"**未知**",「未知 且 假」无论未知那半是什么结论都是假,「未知 或 真」已经有一个真结论必真。**一个确定的值能吃掉另一边的不确定性。** 这也是短路求值的理论基础,以及优化器能把 `WHERE a = 1 AND b = 2` 拆成两个独立过滤条件的前提(真实 ClickHouse 有默认开启的 `short_circuit_function_evaluation`)。

## 17.7 条件

代表函数:`if`、`multiIf`、`coalesce`、`ifNull`、`nullIf`、`assumeNotNull`。

**`NULL` 条件算"假"。** `if(NULL, 'a', 'b')` 得到 `'b'`——条件被当 `UInt8` 用,`NULL` 转 `UInt8` 就是 0。所以 `if(x > 0, ...)` 在 `x` 是 `NULL` 时不会整行消失,而是走 `else`。想区分"`NULL`"和"假",得显式写 `isNull(x)`。

**分支是惰性的。** 真实的短路求值按块只算被选中的那一支,所以 `if(x != 0, 1 / x, 0)` 在 `x = 0` 的块里不会算出 `inf`。把"可能非法"的表达式塞进 `if` 的分支是安全写法。

`multiIf(c1, v1, c2, v2, ...)` 是平的 `if`,**参数个数必须是奇数**(最后一个参数是默认值)。`coalesce(a, b, ...)` 返回第一个非 `NULL`,是把 `Nullable` 列**安全拉回不可空**的标准手段(`coalesce(name, '')`)。`ifNull(x, alt)` 是它的二元形式。`nullIf(a, b)` 在 `a = b` 时返回 `NULL`,用来把哨兵值变成真正的 `NULL`,结果类型**总是** `Nullable`。`assumeNotNull` 不是转换而是**承诺**:只摘 `Nullable` 标记、不检查值,遇到真的 `NULL` 会拿到零值(未定义行为),正确用法只有一种——紧跟判空过滤之后。

## 17.8 取整与数学

代表函数:`floor`/`ceil`/`trunc`/`round`/`roundBankers`,以及 `exp`/`log`/`log2`/`sqrt`/`cbrt`/`pow`/`sign`/`erf`/`factorial`。

**`floor`、`ceil`、`trunc`、`round` 在负数上各不相同。** `floor(-1.5) = -2`(向 -inf),`ceil(-1.5) = -1`(向 +inf),`trunc(-1.5) = -1`(向零),`round(-1.5) = -2`(半远离零)。"trunc 与 floor 在正数上一样、负数上不一样"是最常见的笔误来源。

**`round` 与 `roundBankers` 差在"正好一半"时往哪走。** `round(2.5) = 3`(远离零),`roundBankers(2.5) = 2`(取偶)。`round` 的"半"是**二进制**意义上的半:`round(2.675, 2)` 得到 `2.67`,因为 `2.675` 在二进制里其实是 `2.67499…`;要十进制精确就用 `Decimal` 列。

**第二个参数 `N` 可以是负数(小数点左边第几位清零)。** `floor(1234, -2) = 1200`。**`Decimal` 取整后类型不变**:`floor(toDecimal64(1.99, 2))` 得到 `1.00`,类型仍是 `Decimal(18, 2)`——定点数的 scale 不该因一次取整而改变。

**数学函数:整数参数先转 `Float64`。** `sqrt(2)`、`exp(1)` 的返回类型都是 `Float64`(`Float32` 入参保持 `Float32`),因为结果类型必须在编译期定下来;要整数平方根用 `intSqrt`(向下取整)。几个边界都不报错:`sqrt(-1) = nan`、`log(0) = -inf`、`log(-1) = nan`;`factorial(20)` 是 `UInt64` 装得下的最大阶乘,`factorial(21)` 开始回绕。

## 17.9 字符串

中文用户在这一族踩坑最多,因为几乎所有函数都要先在"**字节**"和"**字符**"之间选边站。ClickHouse 的 `String` 是**二进制安全**的——它不校验编码,内部就是一串字节。

**`length` 是字节数,`lengthUTF8` 是码点数。**

```sql
SELECT length('你好'), lengthUTF8('你好');
-- 6   2
```

同理有 `substring`/`substringUTF8`、`lower`/`upper` 与 `lowerUTF8`/`upperUTF8`、`position` 与 `positionUTF8`。`lower` 只动 ASCII(`'Ä'` 原样保留),`lowerUTF8` 走真正的 Unicode 映射(`upperUTF8('ß')` 展开成 `'SS'`,**长度变了**)。这条区分是刻意做的:`lower` 用在正则预处理、索引比较这类必须极快且不能改字节长度的地方。

`substring(s, offset[, length])` 是 **1-based**;`offset` 为负从尾部数(`-1` 是最后一个字符),`offset = 0` 返回空串,`length` 超末尾截到串尾。`position(haystack, needle)` 找不到返回 **0**(不是 -1)。

**正则是 RE2 语法,不支持反向引用与回溯。** `match`、`replaceRegexpAll`、`replaceRegexpOne`、`extract`、`extractAll`、`splitByRegexp` 都走 RE2(线性时间,不支持 `\1` 与环视)。`like`/`ilike` 是 SQL 通配符不是正则;`replaceAll`/`replaceOne` 是**字面量**替换,`replaceRegexpAll`/`replaceRegexpOne` 才是正则替换。`splitByChar(sep, s)` 的**分隔符在第一个参数**,空串输入返回**空数组**而不是 `['']`。`format` 支持 `{}` 与 `{0}` 占位。

**`concat` 遇 `NULL` 返回 `NULL`**(整行),不跳过它:规避办法是 `concat(ifNull(a, ''), ifNull(b, ''))`。

## 17.10 日期时间

**时区是"列的属性",不是会话的属性。** MySQL 用 `SET timezone` 给整个会话定时区,ClickHouse 把时区写在**列类型**上(`DateTime('Asia/Shanghai')`),同一张表里两列可以有两个时区。所以"取字段"函数内部都是三步:读存储值 → 加该列时区偏移得到本地时间 → 取字段;漏掉第二步,`toYear` 在跨年时刻会差一天。`toTimeZone(dt, tz)` 只换时区,存储值不变。

**`toStartOf*` 族把时间截断到区间起点**:`toStartOfYear`/`...Month`/`...Week`/`...Day`/`...Hour`/`...Minute`,以及固定步长的 `toStartOfFiveMinutes`/`toStartOfFifteenMinutes`。注意命名不整齐:5 分钟的规范名是**复数** `toStartOfFiveMinutes`(`22.6` 引入,单数 `toStartOfFiveMinute` 只是它的**别名**;mini-clickhouse 侧注册的是单数那个),而 10/15 分钟也带复数。日期粒度及以上通常返回 `Date`,小时及以下返回 `DateTime`。

**`dateDiff` 数"跨过了几个单位边界",`age` 数"完整的单位个数"。** `dateDiff('day', '2021-01-01 23:59', '2021-01-02 00:01') = 1`(只差两分钟也跨了一天);`age('month', '2020-08-15', '2021-03-01') = 6`(不足一个月不算)。两者混用是"报表月份数对不上"的常见原因。`dateAdd(unit, n, dt)`/`dateSub(unit, n, dt)` 是日历感知的加减(`addMonths` 日号超界时钳到月末)。`toYYYYMM` 给出 `YYYYMM` 整数,是月度分区与 `GROUP BY` 的常用武器。`formatDateTime(ts, '%Y-%m-%d %H:%i:%S')` 按格式串渲染。

**`now()` 在一条查询内是恒定的**,这是一个有文档、且被广泛依赖的性质:官方把它当作**常量表达式**处理 —— "不同时刻跑的查询拿到不同值,但同一次查询里它是常量"。所以 `WHERE ts >= now() - INTERVAL 1 HOUR` 在整条结果集里用的是同一个时刻,基于 `now()` 的表达式也能配合主键做裁剪。两点要注意:一是它仍被标记为**非确定性函数**(§17.20),因此默认不能进查询缓存、也不会被折叠成字面量;二是**物化视图按批处理时每一批可能各自取一次 `now()`**,跨批并不保证同一时刻。需要跨行绝对一致的时间基准,最稳的仍是从数据列取时间戳。

## 17.11 数组

**下标从 1 开始,越界不报错。**

```text
arr = ['a', 'b', 'c']
arr[1]  = 'a'     ← 1-based
arr[-1] = 'c'     ← 负数从尾部数
arr[0]  = ''      ← 0 不是越界,返回默认值
arr[9]  = ''      ← 越界返回默认值
indexOf(arr, 'b') = 2      ← 1-based,找不到是 0
```

"越界给默认值"是为了**不打断向量化**。同族还有 `has`(是否含某元素)、`arrayConcat`、`arrayDistinct`(去重,保留首次出现顺序)、`arrayCumSum`(前缀和)、`arraySort`(`arraySort(x -> -x, arr)` 用 lambda 指定排序键)。

再补几个常用的,它们合起来覆盖了"把一列数组当成一个小列式引擎来算"的绝大多数需求:`arrayZip(a, b)` 把多个等长数组按位置叉成 `Array(Tuple(...))`;`arrayEnumerate(arr)` 返回 `[1, 2, ..., len]`(下标数组),而 `arrayEnumerateUniq(arr)` 返回"每个元素在它之前出现过几次 + 1",做组内编号很好用;`arrayDifference(arr)` 是相邻元素之差(长度不变,首个差为 0),`arrayCumSum` 的前缀和对偶;`arrayResize(arr, n[, ext])` 把数组拉到长度 `n`(长了截断、短了用 `ext` 或默认值补);`arrayReverse(arr)` 反转;`arrayCompact(arr)` 只压掉**相邻重复**(与 `arrayDistinct` 的"全局去重"不是一回事);`arrayReduce('sum', arr)` 拿一个聚合名在数组上做一次归约;`arraySlice(arr, offset[, length])` 按 1-based 切片(支持负 offset、越界给空数组)。

**`arrayMap`、`arrayFilter`、`arrayJoin` 是三件不同的事。** 前两个是标量函数,一行进一行出:`arrayMap(f, arr)` 逐元素映射得等长数组,`arrayFilter(f, arr)` 保留断定式为真的元素。**`arrayJoin(arr)` 改变的是行数**——它把一行里的数组炸成多行:

```sql
-- 炸开:一行数组 → 多行
SELECT id, arrayJoin(tags) AS tag FROM events;
-- 收回:多行 → 一行数组
SELECT id, groupArray(tag) AS tags
FROM (SELECT id, arrayJoin(tags) AS tag FROM events) GROUP BY id;
```

`arrayJoin` 是引擎在解释阶段做的一次**改写**,不是普通标量函数。它会让**同一行的其它列被复制多份**,统计前先想清楚这次复制是否符合预期。它与聚合函数 `groupArray` 互逆,是"把反范式的一行数组展开成关系表、再收回去"的标准套路。

**两个 `arrayJoin` 写在同一句 `SELECT` 里是笛卡尔积,不是 zip。** 这是全章乃至全书最容易被低估的一个行数陷阱,而且它直接决定你估的行数对不对——估计错,内存、耗时、聚合基数会一起错。机制在于:改写是**逐个**做的,引擎先炸开第一个数组,把行数乘成 `|a|`,再把结果里的每一条新行拿去炸开第二个数组,又乘成 `|a| × |b|`。它**不会**把两个数组按位置配对,而是让第一个数组的每个元素再对第二个数组的每个元素各复制一份。

```sql
-- 3 × 2 = 6 行(到达任何聚合之前)
SELECT arrayJoin([1, 2, 3]) AS x, arrayJoin(['a', 'b']) AS y;
-- 1,a   1,b   2,a   2,b   3,a   3,b
```

如果你想要的其实是"按位置 zip"(第 i 个元素只配第 i 个元素),上面这 6 行里有 4 行是多余的;更要命的是笛卡尔积是**乘性**的,`|a| × |b| × |c| …` 会在聚合之前就把行数炸到灾难级——一个几百元素的数组叠两三个,轻易就是几百万行。三种正确写法,按场景挑:

- **多个等长数组按位置配对**:用查询级的 **`ARRAY JOIN a AS x, b AS y`**(§07.10)。一个 `ARRAY JOIN` 子句里并列写多个数组时做的是 **zip 而不是笛卡尔积**,各数组**长度必须一致**,展开后的行数等于单个数组的长度。
- **在表达式里想把两个数组 zip 起来再炸**:先 `arrayZip(a, b)` 成 `Array(Tuple)`,再 `arrayJoin` **一次**:`arrayJoin(arrayZip(a, b))` 得到 `(a_i, b_i)` 这样的元组,行数仍是单数组长度(§17.12)。
- **确实要笛卡尔积**:那就写清楚它是笛卡尔积,先按上面的乘法估一遍行数,必要时先用 `LIMIT` / 聚合把参与相乘的数组缩到可接受的规模,再让它相乘。

还有一个反直觉的细节:若两个 `arrayJoin` 的参数表达式**完全相同**(比如两处都写 `arrayJoin(tags)`),优化器可能把它当成同一个表达式、只算一次,于是**根本不相乘**。官方文档专门提醒过这一点,建议真要相乘时给重复的表达式做一点"无害的改动"来逼它重新求值(例如把其中一处写成 `arrayJoin(arrayConcat(tags, []))`)。所以"两个一模一样的 `arrayJoin` 跑出来不是笛卡尔积"不是 bug,而是这一层表达式级优化的副作用——**要笛卡尔积就让两个表达式不同,要 zip 就别指望优化器帮你**。

## 17.12 高阶函数

高阶函数的参数里有一个 **lambda**:语法是 `x -> expr` 或 `(x, y) -> expr`。

```sql
SELECT arrayMap(x -> x * 2, [1, 2, 3]);                   -- [2,4,6]
SELECT arrayMap((x, y) -> x + y, [1,2,3], [10,20,30]);    -- [11,22,33]
```

两个要点。**lambda 能捕获外层列**:`arrayMap(x -> x + outer_col, arr)` 里的 `outer_col` 不在形参里,而是被闭包带进来。

**第二个要点更重要:多个数组时,真实 ClickHouse 要求它们等长。** 长度不一致会直接抛 `SIZES_OF_ARRAYS_DONT_MATCH`,**不是**“短的补齐、也不是 `zip` 那样静默截断”。所以 `arrayMap((x, y) -> x + y, [1,2,3], [10])` 在真实 ClickHouse 里是**报错**的,写不出 `[11,12,13]` 这种结果。mini-clickhouse 的行为则不同——它把短的数组**按元素类型的默认值补齐**到最长,所以同一个查询在这里会得到 `[11,2,3]`:第三个元素 `3` 配补出来的 `0`,于是 `3 + 0 = 3`。要跨两边都正确,就始终传**等长**的数组;只在 mini 里试手时,记着它的结果与真实 ClickHouse 对不上。

同族还有 `arrayCount`/`arrayExists`/`arrayAll`/`arrayFirst`,以及归约 `arrayFold((acc, x) -> acc + x, arr, 0)`——**累加器是最后一个参数**。

## 17.13 元组与 Map

`tuple(a, b, ...)` 构造元组,**每个位置保留自己的类型**(`tuple(1, 2.5)` 是 `Tuple(UInt8, Float64)`,不做公共提升,这与 `array(1, 2.5)` 得到 `Array(Float64)` 相反)。取成员用 `tupleElement(t, i)`(1-based、负数从尾部数、越界给该成员类型的默认值);`t.1` 与 `t.name`(具名元组)是解析器的语法糖,最终改写成同一个函数。

`Map(K, V)` 在存储上**就是** `Array(Tuple(K, V))`——键值对个数逐行不同,所以用和 `Array` 一样的变长偏移布局。由此 `mapKeys(m)` 与 `arrayMap(x -> x.1, m)` 是同一件事的两种写法,`mapValues`、`mapContains(m, key)` 同理。理解这个等价关系,就能把 Map 当成"带标签的数组"。

**元组与 Map 的几个坑。** 元组是**结构化**的:`tupleElement(t, i)` 的 `i` 以及 `t.1`、`t.name` 都要求是**字面量**,不能用一列来动态选位;`t.name` 只对具名元组成立(名字不是类型的一部分,只是解析器的语法糖)。元组的比较是**按位置逐元素的字典序**(`(1, 'b') > (1, 'a')`),所以元组能直接进 `ORDER BY`,但要留意位置顺序。Map 这一侧:键值对**逐行不同**,取值时要接受"这一行可能没有这个键";构造上除 `map(k1, v1, ...)` 外还有 `mapFromArrays(keys, values)`(两个等长数组拼一张 Map),两张 Map 相加/相减(同键的值相加/相减、键取并集)用 `mapAdd`/`mapSubtract`,逐键过滤用 `mapFilter`。要知道 `mapKeys(m)` **给的是存储里的顺序、并不保证按 key 排序**,别把它的次序当成"已排好序"。

## 17.14 JSON

**作为函数输入的 JSON,有三族彼此不同的接口,先分清它们。**

`visitParamExtractString`/`visitParamExtractInt`/`visitParamHas` 是**快速的正则式扫描**:只找 `"name":` 这串字符再按最简单的方式切出来,**不处理转义、不校验嵌套、不管重复键**。所以 `visitParamExtractString('{"a":"x\\ny"}', 'a')` 得到的是反斜杠加 `n` 两个字符,没被解码。它比 `JSONExtract*` 快一个数量级,代价是"看起来能用"和"真的合法"之间有缝隙。

`JSONExtractString`/`JSONExtractInt`/`JSONExtractUInt`/`JSONExtractFloat` 是**真正的 JSON 解析**:处理转义、支持多级路径、按目标类型取值。判据很简单:输入里有转义序列(字符串里带 `\n`、`\"`)就**一定**不能用 `visitParam*`。

**第三族是 SQL/JSON 风格接口:`JSON_VALUE`、`JSON_QUERY`、`JSON_EXISTS`。** 它们吃的是 **JSONPath**(如 `$.a.b`),而不是 `JSONExtract*` 那样的"路径片段参数";返回形态也不同——`JSON_VALUE` 取一个**标量**、`JSON_QUERY` 取一个 **JSON 子结构**(数组或对象)、`JSON_EXISTS` 判断路径在不在(返回 0/1)。它们是 21.x 就有的老函数(`JSON_EXISTS` 自 v21.8、`JSON_VALUE` 自 v21.11 起,见附录 A §A.11),25.3 上可用;返回值受 `function_json_value_return_type_allow_nullable` 等设置影响,同一条路径取不到值时的默认表现要看这些设置。三者与 `JSONExtract*` 的取舍一句话:**要 JSONPath、要标准 SQL 的取值形态就用它们,要按目标类型直接取标量就用 `JSONExtract*`**。一个历史坑:`JSON_EXISTS` 的实参顺序在 **21.11 之前是反的**(旧写法 `JSON_EXISTS(path, json)`),看到老资料要先对版本。

**哈希函数要分清输出格式。** `sipHash64`/`xxHash64`/`cityHash64` 返回整数,适合做分桶、去重键;`MD5`/`SHA*` 返回**原始摘要字节**(`FixedString(N)`),不是十六进制串,想看十六进制要写 `hex(MD5(x))`。要提醒的是:**`MD5`/`SHA*` 这类标准算法输出稳定**(`hex()` 给的是大写十六进制,想对齐 `md5sum` 的输出要写 `lower(hex(...))`),但**非加密哈希里有些是明确不保证跨版本/跨平台稳定的** —— 比如 `farmHash64`(官方把它归为“可能变”的一类);真要一个稳定的 64 位指纹,用同一族的 `farmFingerprint64`。所以跨系统比对哈希值前,先确认两边用的是哪一类。

## 17.15 URL 与 IP

URL 不是规整字符串(`https://user:pass@www.example.com:8080/a/b?x=1#frag` 里冒号两次、斜杠三次),所以正确做法是先解析出组件再取:`domain` 只要主机名(不含端口与 userinfo),`path` 是路径(不含查询串),`queryString` 是查询串(不含 `?`),`extractURLParameter(url, name)` 取某个查询参数的值。`isIPAddressInRange(addr, '192.168.0.0/24')` 判断地址是否落在 CIDR 网段里。把 IP 存成整数(`IPv4` 内部是 `UInt32`、`IPv6` 是 16 字节)后,网段运算(`bitAnd` 取掩码)与排序都变成纯数值操作。

**URL 侧的三个坑。** 一是 `path` 与 `pathFull` 不是一回事:`path` 只到路径、**不含查询串与片段**,`pathFull` 才把它们带上;要单独拿查询串用 `queryString`(不含 `?`)、要片段用 `fragment`。二是百分号编码:URL 组件里的 `%E4%B8%AD` 之类是转义形式,`extractURLParameter` 取出的值**是否已解码要当心**——需要自己解码时用 `decodeURLComponent`,反向用 `encodeURLComponent`(表单语境另有 `encodeURLFormComponent`/`decodeURLFormComponent`)。三是"取域名"有多个粒度:`domain` 是主机名,`domainWithoutWWW` 再去掉开头的 `www.`,更粗的层级有 `topLevelDomain`/`firstSignificantSubdomain`——按你要的口径选,别拿 `domain` 直接当"站点"。

**IP 侧的两个坑。** 一是**族别不能混**:`isIPAddressInRange(addr, '10.0.0.0/8')` 里 addr 与 CIDR 必须是同一族(IPv4 配 IPv4、IPv6 配 IPv6),拿 IPv4 去套 IPv6 网段不会得到你想要的答案。二是**存成整数还是文本**,直接决定排序对不对:IPv4 存成 `UInt32`、IPv6 存成 16 字节(`IPv6` 内部是 `FixedString(16)`,大端),按数值或字节序排才是正确的地址序;若存成点分或带冒号的文本再排序,顺序是错的(`'9.1.1.1'` 与 `'10.1.1.1'` 按字符串比较会给出相反的顺序)。与之配套:`IPv4NumToString`/`IPv4StringToNum`、`IPv6NumToString`/`IPv6StringToNum` 负责文本与数值互转。`toIPv4` 遇到非法输入时的返回值、以及 `isIPAddressInRange` 在族别不匹配时是返回 0 还是报错,以你那一版的文档为准(**未确认**)。

## 17.16 编码

代表函数:`hex`/`unhex`/`bin`/`base64Encode`。

**`hex` 对整数用的是"高位在前的可读序",并且省略前导零字节 —— 不是内存里的字节串。**

```text
hex(255)             = 'FF'      ← 255 是 UInt8,1 个字节
hex(toUInt16(256))   = '0100'    ← 高位在前:256 = 0x0100
hex(toUInt32(1))     = '01'      ← 省略前导零字节后,只剩 1 个字节
hex('ab')            = '6162'    ← 字符串按字节转:'a'=0x61、'b'=0x62
```

一个很常见的误解是把整数的 `hex` 当成"内存字节串":以为 x86 是小端,`hex(toUInt16(256))` 就该得到 `'0001'`。**不对。** 对整数,`hex` 打的是**大端(可读)序**,并且**省略前导零字节**(但保留每个字节的两位,即使最高位是 `0`)——所以 `hex(toUInt32(1))` 是 `'01'` 而不是 `'01000000'`,输出用**大写**十六进制。

那"小端内存字节串"到底该怎么写?要先 `reinterpretAsString` 再 `hex`。同书第 03 章 §03.16 给的正是这个写法:`hex(reinterpretAsString(toUInt32(1)))` 得到 `'01000000'`(4 个字节、低位在前)。**只有 `Float` / `Decimal` 例外**:它们没有"可读的十六进制数"这种自然写法,`hex` 就按**内存表示**(小端)输出。

所以 `unhex` 也逆不回类型信息:`unhex('01')` 得到的是**长度 1** 的 `String`,**不是** `UInt32`。要从字节还原成整数得配 `reinterpretAsUInt32` 之类——这正好用上"`String` 是二进制安全的"。(mini-clickhouse 的 `hex` 实现的是**旧**的"整数按小端内存字节串"行为,所以在 mini 里跑 `hex(toUInt16(256))` 会看到 `'0001'`,与真实 ClickHouse 的 `'0100'` 不同——这是有意保留的简化点。)

## 17.17 其他:类型转换与"元函数"

类型转换常见的有 `toString`/`toInt64`/`toUInt32`/`toFloat64` 等 `toXxx`、`CAST(x AS T)`、`toTypeName(x)`。

**这一族里最容易产生真实 bug 的,是"带兜底的解析"和"静默回绕"。** 先把默认行为记死:**普通的 `CAST` / `toXxx` 在数值越界时会"回绕或截断",而不报错。** 例如 `toInt8(1000)` 得到 `-24`(按位截断)、`toUInt8(-1)` 回绕成 `255`——数据脏了它不会跳出来提醒你,只会**静默**给一个错值。凡是不确定数据是否干净,就绕开默认行为,四条路:

- **`toXxxOrZero` / `toXxxOrNull`——解析失败时给 `0` 还是给 `NULL`。** `toInt64OrZero(s)` 遇到 `'abc'` 返回 `0`,`toInt64OrNull(s)` 返回 `NULL`。这条选择正是"数据脏了就静默出错"的根源:用了 `OrZero`,你事后**分不清**"这个值真的是 0"还是"解析失败了"。需要区分时一律用 `OrNull`,它的结果类型变成 `Nullable(...)`。
- **`accurateCast` / `accurateCastOrNull`——越界时报错,或给 `NULL`。** 它们不像 `CAST` 那样静默回绕:`accurateCast(x, 'Int8')` 在越界时**直接报错**,`accurateCastOrNull(x, 'Int8')` 则**给 `NULL`**。要把"静默错误"变成"立刻失败"用前者;要"越界就置空、别中断整条查询"用后者。
- **`parseDateTime` 家族**(`parseDateTimeBestEffort`、`parseDateTime64BestEffort` 等)——把格式杂乱的日期时间字符串**按尽量宽松的规则**解析成 `DateTime`/`DateTime64`。来源不可控、格式不统一时,它们比死抠格式的 `toDateTime` 实用得多;但同样要知道**解析不了时它不会抛错**,而是给默认值或 `0`(视具体函数而定)。
- **`toDate`/`toDateTime` 的截断陷阱。** 从 `DateTime64` 降到 `DateTime`/`Date` 是**截断**、不是四舍五入:`toDate(toDateTime64('2024-03-01 23:59:59.999', 3))` 会把时分秒连同小数一起砍掉,只剩 `2024-03-01`。跨精度、跨时区地做转换时,要意识到秒以下、乃至整天都被吞掉了。

判据一句话:**默认的 `CAST`/`toXxx` 越界会回绕或截断而不报错;要安全就用 `accurateCast*` 或 `*OrNull`,要"脏数据就失败"就绝不要用 `*OrZero`。**还有一族"按位重解释"的函数,以 `reinterpretAs` 开头——它们**不转换数值**,而是把同一段内存按另一种类型读出来。以 `reinterpretAsString(toUInt32(1))` 为例:`toUInt32(1)` 在内存里是小端 `01 00 00 00`,重解释后得到一个**长度 4 的字符串**,第一字节 `0x01`、后三个字节 `0x00`——打印出来几乎全是不可见字符,这正是"重解释"与"转换"的区别:不改变字节,只改变怎么读。另一类"元函数"影响执行本身:`materialize(x)` 把常量列展开成完整列,`blockSize()` 返回当前块行数,`bar` 画进度条。

## 17.18 剩下来的几族:地理、加密、机器学习与累积

这几族不常上主查询,但各自解决一类硬需求,值得知道它们存在、该往哪找。

**地理**。`pointInPolygon((x, y), polygon)` 判断点是否落在多边形内,是"电子围栏""区域归属"的常用手段:第一个参数是点的 `(x, y)`,第二个是多边形顶点数组 `Array(Tuple(Float64, Float64))`,返回 `UInt8`。`greatCircleDistance(lon1, lat1, lon2, lat2)` 算球面距离——经纬度以**度**为单位、返回米,也接受两个 `(lon, lat)` 元组的形式;它与 `geoDistance` 系列同族。注意函数名是 `greatCircleDistance`(球面距离),别和取极值的 `greatest` 混。这些是纯标量计算,输入通常来自第 03 章的 `Array(Tuple(Float64, Float64))`。

**加密**。`encrypt(mode, key, iv, data)` 与 `decrypt(mode, key, iv, encrypted)` 在 SQL 里做对称加解密(最常用的是 `'aes-256-gcm'`,算法名作为第一个常量参数给出,也还有别的算法名可用),用途是把敏感字段落库前加密、查询时按需解密。要提醒的是:**密钥一旦出现在 SQL 文本里,就会被日志、查询历史、`system.query_log` 记录下来**,所以生产上更稳妥的是在应用侧加密。另外,如果你的目的只是**校验、分桶、去重键**而不是保密,别动用加密——用哈希族的 `sipHash64(x)` / `xxHash64(x)`(返回整数,见 §17.14)更快也更安全。

**机器学习**。ClickHouse 内置了几种可当聚合函数用的模型训练:`stochasticLinearRegression`、`stochasticLogisticRegression`,训练出的状态用 `evalMLMethod(model, features)` 对行打分。**它们的形态是聚合函数**:在 `GROUP BY` 上调用 `stochasticLinearRegression(features, target)` 一路累积、训练出一个模型(那份模型状态可以按第 12 章那套 `-State`/`-Merge` 组合器存进列、再合并),随后用 `evalMLMethod(model, features)` 对行**打分**、返回预测值。它们适合"在库内做轻量打分",不适合替代真正的训练框架 —— 别指望在这里跑深度学习。

**累积与差分**。`runningDifference(x)`(当前行减上一行)与 `runningAccumulate(state)`(把状态沿行累积)在时间序列里常用。**`runningAccumulate` 的实参是一个聚合状态列**(比如 `sumState(x)` 写出来的那种),它沿行把状态逐行累积、返回"到这里为止的累计值"——注意它吃的是**状态**而不是普通列。要记住 `runningDifference` **依赖行的物理顺序**,所以必须先 `ORDER BY`;它们与窗口函数语义有重叠,新写法更推荐用窗口函数(§16.10)把帧显式写出来。

这些函数的具体签名与边界条件以你那一版的文档为准;本节只负责让你知道"该去哪个族找"。

## 17.19 表函数:把外部数据当表

前面讲的都是**标量函数**(进列、出列)。还有一类"函数"返回的不是值,而是一张**表**,可以直接写在 `FROM` 里,这叫**表函数**:

```sql
SELECT * FROM numbers(10);                                     -- 生成 0..9
SELECT * FROM generateRandom('a UInt8, b String', 1, 10) LIMIT 5;
SELECT * FROM file('data.parquet', 'Parquet');
SELECT * FROM s3('https://bucket/key.csv', 'CSV') LIMIT 10;
SELECT * FROM url('https://example.com/data.tsv', 'TSV');
SELECT * FROM remote('host:9000', 'db', 'table');
SELECT * FROM values('id UInt8, name String', (1, 'a'), (2, 'b'));
SELECT * FROM merge('db', '^events_\\d+$');                    -- 一批同构表看成一张
```

用途一句话概括:**在不建表的前提下,让"某个数据源"直接变成一个可查询的表**。`numbers`/`generateRandom` 用来造测试数据;`file`/`url`/`s3` 直接读外部文件或对象存储;`remote`/`cluster` 走远端(`cluster` 依赖 `remote_servers` 配置);`values` 把字面量当表(经常以内联表形式出现);`merge` 把一批匹配的表名拢成一张虚拟表;`input` 则让查询主动向客户端索要一段输入数据。

这里**只列名字、不展开** —— 它们的格式参数、路径写法、压缩与鉴权,分别在第 08 章(数据写入与导入)与第 19 章(数据格式与序列化)展开;`remote`/`cluster` 的分布式语义在第 23 章 §23.7。

## 17.20 非确定性函数

`rand`、`rand64`、`now`、`generateUUIDv4` 这一族是**非确定性**的,它们**不能被常量折叠**。这条约束常被忽略:若把 `now()` 折叠成常量,整条查询里所有 `now()` 都会变成同一时刻——看起来"更一致",实际与真实行为不符,长查询里会明显偏差。查询优化器决定"这个表达式能不能在分析期算好"时,会先看函数的确定性标记。

```sql
-- rand() 逐行(严格说逐块)各取一个值,所以这两列通常不相等
SELECT rand() AS a, rand() AS b FROM numbers(5);
-- randConstant() 整条查询只取一个值,所有行相同
SELECT randConstant() AS c FROM numbers(5);
```

三个实际后果:**不能进查询缓存**(缓存要求确定性,见附录 A §A.16);**不能参与主键/跳数索引的裁剪**(分析期拿不到常量去估);**在 `WHERE` 里做随机抽样会逐行重算**,既不可重现、也不省扫描——要按比例随机抽样,优先用表上已声明的采样键配 `SAMPLE`,而不是 `WHERE rand() < 0.01`。`now()` 是这一族里的特例:它**非确定**、默认也不能被缓存或折叠,但在**一条查询内恒定**(见 §17.10),所以它比一般随机函数"可依赖"一些。

## 17.21 JSON 数据类型与 Dynamic:半结构化的类型侧

§17.14 讲的是 JSON 作为**函数输入**——把一个 `String` 丢给 `visitParam*`/`JSONExtract*`/SQL/JSON 三族函数现场解析。25.3 起还有另一半:JSON 作为**数据类型**。两者名字相同、作用完全不同:**格式(以及 `JSONExtract*` 这类函数)**决定"一段文本怎么解析或输出",而**类型**决定"一列半结构化数据怎么存、怎么按路径索引"。别把它们混为一谈。

`JSON` 类型在 `Dynamic` 之上工作:`Dynamic` 让**一列里每行可以是不同类型**(类型由值决定),`JSON` 则把每条记录的**路径提升成真正的子列**,于是 `json.user.id` 既能类型安全地读,也能建跳数索引。声明时可以固定一部分路径的类型,其余按动态路径处理——动态路径有个上限 `max_dynamic_paths`(默认 **1024**),超出的落进一块共享结构、仍能读但更慢。完整的类型语法(`JSON(a UInt32, b String, SKIP c.d)`、`SKIP REGEXP`、`enable_json_type`)在第 03 章 §03.13,这里不重复。

只看**函数**这一侧,新类型带来三件事:

- **按路径访问**:`json.a.b` 是子列语法,直接取某个路径的值(子列机制见第 03 章 §03.11)。读一个不存在的路径得到 `NULL` 或默认值,不报错。
- **内省函数**:`JSONAllPaths(json)` 列出所有路径,`JSONAllPathsWithTypes(json)` 连类型一起给,`JSONDynamicPaths` / `JSONSharedDataPaths` 分别看"动态路径"和"溢出到共享结构"的那部分——排查"这一列到底有哪些字段"时用它们。
- **从 `Dynamic` 里取某个变体**:`dynamicElement(dyn, 'UInt64')` 把一列 `Dynamic` 中"确实是 `UInt64` 的那些行"取出来凑成一列(其余行给 `NULL`)。这是 `Dynamic` / `Variant` 的类型化读取手段。

**和 `JSONExtract*` 怎么取舍?** 一句话:**重复查的路径用类型,一次性碰的路径用函数**。新 `JSON` 类型适合"结构不固定、但**每条记录很长、公共路径重复率高**"的场景——公共路径被物理化成列,读写都快,还能建索引;而纯 `String` + `JSONExtract*` 每次查询都要**重新解析整段字符串**,没有任何索引可用。反过来,如果每条记录的路径几乎都不一样(高度稀疏),物理化会造出大量几乎全空的子列,反而更费,这时 `String` + 按需 `JSONExtract*` 更省。`Dynamic` 的每一行都带类型标签、单值开销大于定类型列,所以它也不是"普通列的通用替代"——该定类型的列仍然要定类型。

**几个具体数字**(可在目标版本上用 `DESCRIBE` 与 `system.settings` 复核):`JSON` 类型默认最多把 **1024** 条路径物化成独立子列(`max_dynamic_paths=1024`),超出后新路径落进共享结构,官方建议这个上限别超过 10000(本地盘)/ 1024(远程盘);每条动态路径最多承载 **32** 种类型(`max_dynamic_types=32`),而 `Dynamic(N)` 的 N 取值 0~254、默认也是 **32**。`dynamicElement` 的签名是 `dynamicElement(dynamic_column, 'TypeName')`:按类型名取出一列与之匹配的行、其余行补 `NULL`(等价于子列语法 `d.TypeName`)。

## 17.22 从 MySQL / PostgreSQL 过来:同名不等价的那批函数

本书反复把"从 MySQL / PostgreSQL 迁移"当成一个独立主题(第 02 章 §02.8 讲心态,第 07 章讲子句语义),但真正每天卡人的其实是**函数层**:你以为自己会写,于是不查文档,结果函数**存在、名字也对,语义却是另一回事**。这一节不列全量对照表——那种表三天就过期——而是把"**同名不等价**"的几类挑出来,并给出当场自查的方法。

**先给方法,因为它比清单耐用。** 真实 ClickHouse 把函数注册表放在 `system.functions` 里,任何"这个函数到底存不存在、叫什么、返回什么"的问题都能当场问它:

```sql
-- 有没有这个函数?有哪些别名?
SELECT name, alias_to, case_insensitive FROM system.functions
WHERE name ILIKE '%date%diff%' OR alias_to ILIKE '%diff%';

-- 拿不准语义时,直接在小数据上试,永远比查二手资料快
SELECT toTypeName(dateDiff('day', toDate('2024-03-01'), toDate('2024-03-05')));
```

函数名**大小写不敏感**(`IFNULL`、`ifNull` 是同一个),所以迁移时写惯大写的不用改;真正的坑都在**语义**上,不在字母上。(配套实现里的对应物是 `registered_functions()`,见附录 B。)

**第一类:同名但语义不同,而且不会报错。**

- **`length` 数的是字节,不是字符。** MySQL 的 `CHAR_LENGTH` 数字符,`LENGTH` 数字节;ClickHouse 的 `length` 只数**字节**,要数字符得用 `lengthUTF8`。一整列中文用它算"字数",结果会大出两三倍而**不报任何错**。
- **`position(haystack, needle)` 的实参顺序与 MySQL 的 `LOCATE(needle, haystack)` 相反。** 两者都是"找不到返回 0",所以顺序写反时最常见的表现是**恒返回 0**,而不是报错。
- **`dateDiff(unit, start, end)` 与 MySQL 的 `DATEDIFF(a, b)` 不是一回事。** 前者第一个实参是**单位字符串**、返回**带符号**的差值(谁大谁小决定正负);后者的单位固定为天、且顺序写反就是另一个符号。**还有一个更隐蔽的差别:同一对实参下两家的正负号是反的**——`dateDiff('day', a, b)` 算的是 `b - a`,而 MySQL 的 `DATEDIFF(a, b)` 算的是 `a - b`。跨年做差、或者把"两个时间的先后"当成业务判断(比如 `WHERE dateDiff(...) > 0`)时,这一处最容易静默出错。
- **字符串比较与排序按字节,不按排序规则。** MySQL 的默认排序规则常常不区分大小写,`'a' = 'A'` 可能为真;ClickHouse 按**字节序**比较,`'Z' < 'a'`,大小写永远不等。所以"不区分大小写的去重 / 分组"从 MySQL 迁过来时**不会报错,只会多出几组**——要么显式 `lower()` / `upper()`,要么用 `COLLATE`(第 16 章 §16.3)。
- **日期格式化是两个词汇表。** 都写 `%Y-%m-%d` 这类占位符,但**可用的指令并不完全重合**(有些在一边有、在另一边没有,行为也可能不同)。迁移时别只换函数名,格式化串要逐条对着看。
- **隐式类型转换基本没有。** MySQL 会默默把字符串转成数字来比较;ClickHouse 不做这种"贴心"转换,类型对不上通常直接报错或按明确规则处理。**这一条总体上是好事**(错误暴露在迁移期而不是上线后),但意味着依赖隐式转换的老 SQL 会成批失败,要一条条显式 `CAST` / `toUInt64` 写清楚。

**第二类:名字或写法要换,但语义等价(改一下就行)。**

- **`GROUP_CONCAT` 有对应物,但要留意版本。** ClickHouse 在 **24.8** 之后有了 `groupConcat`,当时的别名只有 `group_concat`(`string_agg` 这个别名要等到 **26.5** 才加上——25.3 上写 `string_agg` 是不认的),分隔符作为**参数**给出、而不是 MySQL 那样的 `SEPARATOR` 子句:`groupConcat(', ')(x)`(不带参数时分隔符是空串)。24.8 之前的写法是两步走——`arrayStringConcat(groupArray(x), ',')`。所以老资料里"ClickHouse 没有 `GROUP_CONCAT`"的说法,在 25.3 上已经过期了。
- **`IFNULL(x, y)` / `COALESCE(...)`** 有对应物(`ifNull` / `coalesce`),但只在参数真为 `Nullable` 时才有意义——非空类型上的 `ifNull` 是多余的,而 `Nullable` 本身有代价(第 03 章 §03.9)。
- **分页写法可以照搬**:`LIMIT m, n`(跳过 m 行、取 n 行)在两家是**同一个意思**,`LIMIT n OFFSET m` 也等价,不必改。但 ClickHouse 在同一句里还多了 `LIMIT n BY expr`(每组取前 n 行),以及"先分组限量、再叠一个总行数上限"的写法,几段拼在一起时**顺序不能随手调换**(第 07 章 §07.8、第 16 章 §16.6)。另外深分页在这里同样要**读、排序、再丢掉**前面 m 行,迁过来不会变快——该用键集分页就得改写法。
- **`SUBSTRING` 的负偏移、`LEFT` / `RIGHT` 的边界**这类细节,以本章 §17.9 与附录 A 为准,别按 MySQL 的记忆写。

**第三类:根本没有对应语义,需要换思路。**

- **`INSERT ... ON DUPLICATE KEY UPDATE`、`REPLACE INTO`、`INSERT ... ON CONFLICT`** 这类"按主键 upsert"的写法在 ClickHouse 里**没有直接对应物**。它的替代是**引擎层面的去重**:`ReplacingMergeTree` 保留版本最大的一行、`CollapsingMergeTree` 用 `sign` 成对抵消、或者干脆查询时 `FINAL`(第 04 章、第 18 章)。要把"更新"当成"再插一条更晚的版本"来建模,而不是当成一条语句。
- **`UPDATE ... WHERE` / `DELETE ... WHERE`** 语法上存在,但机制是**异步重写 part**(mutation)或轻量级删除标记(第 09 章 §09.3、第 18 章 §18.8 与 §18.9),不是 MySQL 那种"立即生效、锁定行"的语义。把高频小批量的 `UPDATE` 迁过来,是这个引擎上最典型的用法错位。
- **事务**只有很窄的适用范围(第 09 章 §09.13),没有 MySQL 那套隔离级别与行锁可供依赖。
- **递归 CTE** 是较新的能力(§07.3 形态三),老分析器下用不了;而层级数据在 ClickHouse 里通常用数组更自然。

**怎么用这一节。** 不要把上面任何一条当成"完整对照表"——它是**筛选器**:迁移时先把你的 SQL 扫一遍,凡是命中"字符串长度 / 位置查找 / 日期差 / 大小写比较 / 格式化串 / upsert / 更新"这几处的,逐条在目标版本上跑一次对照。剩下的绝大多数函数(upper、lower、trim、replace、concat、abs、round、min、max、sum、avg 这类)**名字与语义都和 MySQL 基本一致**,不必逐个怀疑。**判断某一条到底像不像,永远以当场 `SELECT` 的结果为准**,而不是以它名字长得像为准。

## 17.23 与 mini-clickhouse 的对照

配套的 mini-clickhouse 注册了 **658 个函数名**(含别名,全部归在 `Regular` 类),`registered_functions()` 是这份清单的数据源——它也同时喂给 `system.functions`,所以你可以直接查库确认:

```sql
SELECT count() FROM system.functions WHERE is_aggregate = 0;
SELECT name FROM system.functions WHERE name LIKE '%Hash%';
```

模块与函数族的对应是:`arithmetic`(算术与位运算)、`math_funcs`、`rounding`、`comparison`、`logical`、`conditional`、`type_funcs`(类型转换与 `reinterpretAs*`)、`string`、`datetime`、`array`、`higher_order`、`hashing`、`encoding`、`json_funcs`、`url_ip`、`tuple_map`、`misc`。这里是**17 个"注册函数的模块"**;`minich/functions/` 目录下还有两个不登记函数名的文件——`base.py`(函数基类与注册表)和 `__init__.py`(包入口,负责 `import` 各模块触发注册),所以按**文件**数是 19 个。附录 B 里"`functions/` 19 个模块"用的是文件口径,**与这里的 17 个并不矛盾**。

**简化点与近似实现**(必须点出来):

- `cityHash64` 在这里**就是 `sipHash64` 的别名**——真实 ClickHouse 用 CityHash 1.0.2,输出与任何其它版本都不同,重新实现既费事又极易错,所以实现选了"分布一样好、但值完全不同"。要与真实 ClickHouse 对齐请用 `sipHash64` 或 `xxHash64`。
- `intHash64` 用 splitmix64 变体,分布更好但**值与真实 ClickHouse 不同**;`intHash32` 取它的高 32 位。
- 字符串里的正则走 **Python 的 `re`** 而不是 RE2,所以 `\1` 反向引用、环视在这里能跑、在真实 ClickHouse 里会报错。
- `toWeek`/`toYearWeek` 的跨年周用启发式,极少数 1 月的日期可能差 1;`roundAge` 按**官方修订后**口径实现;`roundDuration` 的返回类型取 `UInt64` 是**有意的推断**(官方没写明);`arrayFlatten` **递归压到底**,而真实 ClickHouse 只压一层。
- 有一批函数的返回类型**要由常量的值来决定**(`CAST('UInt8')`、`toDecimal64(x, 4)`、`defaultValueOfTypeName('UInt8')`)。真实 ClickHouse 在分析期就能拿到列的值,所以在 SQL 里直接写成第二个实参即可;而 mini 的 `get_return_type` 只拿到**类型**。实现里有两条路走通它:**参数形态**(`toDecimal64(4)(x)`)与一层兼容垫片——如果一次调用因**实参个数**失败、而尾部恰好是常量,就把它们挪成参数重试一次。所以 `toDecimal64(x, 4)` 和 `toDecimal64(4)(x)` 在这个实现里**都能用**。这个垫片写在 `minich/execution/expression.py` 的 `_retry_with_trailing_params` 里,函数库本身没有为它做特化。

**没实现的就是没实现。** 有一批函数是**注册了名字但会抛 `NOT_IMPLEMENTED`**,因为它们改变的是行数或列数、必须由执行引擎而不是函数来实现:`arrayJoin`(把一行数组炸成多行)、`untuple`(把一列元组炸成多列)。它们各自带一句"为什么"。原则很直白:**函数表里查不到的名字,就是没有实现** —— 而查得到的名字也可能明确告诉你"这一件事我做不了、为什么"。不要凭"别的数据库有"去臆测它存在。

## 17.24 小结

- 四块地基:名字大小写不敏感、运算符就是函数、求值单位是列、结果类型由参数类型推出。本章只讲**标量函数**——聚合函数归第 12 章,表函数只在本章 §17.19 点到。
- **函数名的组合子**把几十个基函数放大成几百个:`-If`/`-Array`/`-State`/`-Merge`/`-OrNull`/`-OrDefault`/`-Resample`/`-ForEach`/`-Distinct`/`-Map`/`-ArgMin`/`-ArgMax`/`-SimpleState`/`-MergeState` 等;`-Array` 必须紧贴基函数名(如 `uniqArrayIf`,不是 `uniqIfArray`),其余后缀往后接(完整清单见 `system.aggregate_function_combinators` 与第 12 章 §12.7)。
- **参数化函数**的常量实参决定返回类型:`quantile(0.9)(x)`、`topK(5)(x)` 是"参数 + 数据列"两段式,`toDecimal64(x, 4)`、`CAST(x, 'UInt8')` 则靠第二个实参定类型 —— **参数必须是常量**,因为列类型要在分析期定死。
- 算术里最要紧的是位数与溢出:`divide` 返回浮点、`intDiv` 向零截断并报除零、`intDivOrZero` 给 0、64 位封顶后回绕。
- 比较返回 `UInt8`;`NULL` 上该用 `isNotDistinctFrom`(`<=>`)而不是 `=`(但 25.3 上它只在 `JOIN ON` 段可用,通用支持的落地版本未确认,一说 25.10 附近;它的对偶 `isDistinctFrom` 在 25.3 上**尚未提供**,别照搬新版函数名)。`greatest`/`least` **自 24.12 起忽略 `NULL`**(旧版或开 `least_greatest_legacy_null_behavior = true` 时返回 `NULL`)。逻辑是三值逻辑:`NULL AND 0 = 0`、`NULL OR 1 = 1`、`NULL AND 1 = NULL`。
- 字符串第一坑是 `length`(字节)与 `lengthUTF8`(码点);正则走 RE2,不支持反向引用。
- 时区是列属性;`dateDiff` 数边界、`age` 数完整单位;`now()` 在**一条查询内恒定**(但物化视图按批各取一次)。数组 1-based、越界给默认值;`arrayJoin` 改行数、`groupArray` 与它互逆——**两个 `arrayJoin` 写在同一句里是笛卡尔积(行数相乘),不是 zip**;要按位置 zip 就用 `ARRAY JOIN a, b` 子句(§07.10)或 `arrayJoin(arrayZip(a, b))`,且注意两个参数完全相同的 `arrayJoin` 会被优化器合并、不相乘。
- JSON 有两条路:函数侧分 `visitParam*`(快、不解码转义)、`JSONExtract*`(正确解析)与 SQL/JSON 的 `JSON_VALUE`/`JSON_QUERY`/`JSON_EXISTS`(吃 JSONPath)三族(§17.14);类型侧是 25.3 起的 `JSON`/`Dynamic` **数据类型**(§17.21)——路径提升成子列、能按路径索引,重复查的路径用类型、一次性碰的路径用函数。`hex` 对整数按**大端可读序**输出、省略前导零字节(小端只有 `Float`/`Decimal` 才是那样,或者自己套 `reinterpretAsString`)。
- mini-clickhouse 里 `cityHash64`/`intHash64` 是近似实现,`type_funcs`/`misc` 是占位——函数表里没有的就是没实现。
