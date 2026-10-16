# 第 03 章 数据类型体系

绝大多数从行存数据库过来的人,都把"类型"理解成一件事:**约束**——它规定这一列能存什么,存不下就报错。ClickHouse 的类型不扮演这个角色。它同时决定了数据在磁盘上排成什么形状、压缩器看到什么模式的字节、这一列能用哪些函数,以及它能不能进排序键。

所以这一章不会只把类型清单列一遍(那是 `第 33 章` 附录 A 的事),它要讲的是**每个类型背后的代价**:`Decimal` 的宽度为什么不会跟着标度一起长、`Nullable` 到底多出了什么、`LowCardinality` 在什么情况下反而更大、字符串为什么不校验编码、时区与夏令时会从哪里咬进来。

这一章也是全书"靠直觉猜"失效最密集的地方。这里给出的每一条规则,在后面解释查询行为与磁盘占用的章节里都会再碰到——**先把它们记牢,比先学会写法更值**。

## 03.1 类型不只是声明

在 MySQL 里把字段从 `INT` 改成 `BIGINT`,你担心的主要是"会不会溢出"。在 ClickHouse 里把 `String` 改成 `LowCardinality(String)`,你同时改变了**存储布局**(每行几字节、列文件怎么排)、**序列化方式**、**可用函数**、**压缩编码**四件事。

还有一条更隐蔽的:**类型参与查询计划的类型推导**。两个 `UInt8` 相加得到 `UInt16`,这个结果类型会一路传播到聚合、JOIN、物化视图的建表语句里。类型选错不一定报错,但会在你没想到的地方改变行为。

所以这一章不是速查表,而是逐个讲清楚每个类型**省了什么、贵在哪里**。速查表在 `第 33 章`。

## 03.2 整数族

整数有 `UInt8/16/32/64/128/256` 和 `Int8/16/32/64/128/256` 共 12 种,位宽直接决定字节数和值域:`UInt8` 占 1 字节、值域 0–255;`Int8` 占 1 字节、值域 -128–127;`UInt256` 占 32 字节。

**字面量会挑"能装下就最小"的类型**:

```sql
SELECT toTypeName(1), toTypeName(256), toTypeName(65536), toTypeName(-1);
-- UInt8            UInt16          UInt32               Int8
```

`1` 是 `UInt8` 而不是 `Int32`,因为它能被 8 位无符号装下。也就是说 `WHERE id = 200` 和 `WHERE id = 300` 里,那个字面量是两种不同的类型。多数情况下无害,但在建物化视图、做类型推导时会显现出来。

**溢出是回绕,但"什么时候回绕"比传言复杂。** 先澄清一个流传很广的错误说法:

```sql
SELECT toUInt8(250) + toUInt8(10);
-- 260
```

结果是 260,**不是** 4。因为 `UInt8 + UInt8` 的结果类型会"进一档"变成 `UInt16`,260 装得下。这是刻意的设计:算术运算的默认目标是不溢出。

```text
UInt8  + UInt8   → UInt16
UInt8  + UInt16  → UInt32
Int8   + UInt8   → Int16
UInt16 * UInt16  → UInt32
UInt64 * UInt64  → UInt64     ← 64 位封顶,这一档之外才回绕
```

减法的结果类型**永远有符号**,否则 `0 - 1` 会变成 65535。

那什么时候真的回绕?三种。一是位宽封顶:`toUInt64(18446744073709551615) + toUInt64(1)` 得到 0。二是显式转型,转型**不做**范围检查,超范围按位截断:

```sql
SELECT toUInt8(256), toUInt8(-1), toInt8(1000);
-- 0                255         -24
```

三是那些**把"结果类型 = 输入类型"写进契约的函数**。典型是 `sumWithOverflow`——它不参与提升,把和按输入宽度直接回绕:

```sql
SELECT sumWithOverflow(x) FROM (SELECT toUInt8(200) AS x UNION ALL SELECT 200);
-- 144     (400 按 8 位回绕)

SELECT toTypeName(sumWithOverflow(x)), toTypeName(sum(x))
FROM (SELECT toUInt8(200) AS x UNION ALL SELECT 200);
-- UInt8                              UInt64
```

顺便纠正一个常被引用的错误说法:**"数组聚合函数不参与提升,所以会回绕"是错的**。`arraySum` 在真实 ClickHouse 里同样提升——`arraySum([toUInt8(200), toUInt8(200)])` 得到的是 `UInt64` 的 **400**,不是回绕后的 144。真正会回绕的,是像 `sumWithOverflow` 这样**显式把结果类型钉死成输入类型**的函数,而不是某一大类函数的通病。

把上面两行放在一起看,分工就清楚了:`sumWithOverflow` 用"愿意回绕"换来"结果还是 8 位",适合你确知不会溢出的窄列;`sum()` 则默认把无符号整数提升到 `UInt64`(有符号到 `Int64`、浮点到 `Float64`),先扩宽再累加,所以一列 `UInt8` 求和不会回绕,哪怕总和远大于 255。

结论是:"整数溢出会回绕"这句话对,但**不是所有整数运算都会回绕**;判断方法只有一个——看结果类型。

**选型建议。** 别一律 `Int64`——`UInt32` 省一半空间,在几十亿行上是实打实的 I/O,但整张模型里**尽量统一是否带符号**,因为混用会触发位宽加倍(见 §03.17)。

## 03.3 浮点

`Float32`(4 字节)和 `Float64`(8 字节),遵循 IEEE 754。除了精度,有两点必须知道。

**`NaN` 不参与相等。** `nan = nan` 是**假**,`nan != nan` 是**真**,与 IEEE 一致。判断 NaN 要用 `isNaN(x)`,不要写 `x = nan`。官方文档写明了 NaN 的排序位置:**NaN 被视作比所有其他数值都大**——升序时它排在最后,降序时排在最前。即便如此,业务若要依赖这个顺序,仍建议用 `isNaN()` 显式排(例如 `ORDER BY isNaN(x), x`),把意图写进查询本身,而不是隐式依赖排序实现。

**钱不要用 Float。** `0.1 + 0.2 != 0.3` 是必然的——不是精度不够,而是二进制小数表示不了 `0.1`。涉及金额、对账、比率必须精确的地方用 `Decimal`,浮点只适合测量值、统计结果这类本来就有噪声的数据。

## 03.4 Decimal(P, S)

`Decimal(P, S)` 是**定点数**:总共 P 位十进制数字,小数点后 S 位。`1234567.89` 在 `Decimal(9, 2)` 里存的是整数 `123456789` 配 scale = 2。

底层整数宽度由 P 决定,这决定了这一列占几字节:

| P 范围 | 底层整数 | 别名 | 字节 |
| --- | --- | --- | --- |
| 1–9 | Int32 | `Decimal32(S)` | 4 |
| 10–18 | Int64 | `Decimal64(S)` | 8 |
| 19–38 | Int128 | `Decimal128(S)` | 16 |
| 39–76 | Int256 | `Decimal256(S)` | 32 |

`Decimal32(2)` 只是 `Decimal(9, 2)` 的别名——P 固定,所以它只接受一个参数。

**参数可以省,省略有确定的含义。** 只给一个参数时,`Decimal(P)` 等价于 `Decimal(P, 0)`——标度默认 0,也就是一列只能存整数;一个参数都不给时,`Decimal` 等价于 `Decimal(10, 0)`。这两个简写在建表时合法且语义明确,不是"参数没写全"。从别的数据库搬表定义时容易撞上:`DECIMAL(10)` 在 MySQL 里是"10 位、标度默认 0",语义恰好一致;但如果本意是"10 位里留 2 位小数",就必须显式写成 `Decimal(10, 2)`,否则小数部分会被直接截掉。

算术的结果类型有两条**彼此独立**的规则:**scale 怎么变**、**位宽落到哪一档**。把这两件事混成一句话("scale 和精度一起相加"),是这一节最常见的错误来源。

**加减法沿用较大的 scale。** 结果 scale 是 `max(S1, S2)`:

```sql
SELECT toTypeName(toDecimal32(1.23, 2) + toDecimal32(4.5, 1));
-- Decimal(9, 2)      (max(2, 1) = 2)
```

**乘法把 scale 相加,位宽只取两者里更宽的那一档。**

```sql
SELECT toTypeName(toDecimal32(1.23, 2) * toDecimal32(4.56, 2));
-- Decimal(9, 4)      ← scale 相加,但仍是 Decimal32
```

两位小数乘两位小数需要四位小数,所以 scale 相加(2 + 2 = 4)。但底层整数宽度**不跟着相加**:两个 `Decimal32` 相乘,结果仍落在 `Decimal32` 这一档,只是 scale 吃掉了一部分可用位数(P 还是 9,整数部分只剩 5 位)。所以"乘出来精度也相加、得到 `Decimal(18, 4)`"是错的——`Decimal(18, 4)` 已经属于 `Decimal64`,只有乘数里本来就有 `Decimal64` 时才可能出现。

scale 相加会撞天花板:

```sql
SELECT toDecimal32(4.2, 8) * toDecimal32(4.2, 8);
-- 报错:Scale is out of bounds.    (8 + 8 = 16,超过 Decimal32 的 scale 上限 9)
```

这正是"乘法链"的隐患:每次相乘 scale 都翻倍,`Decimal32` 的两三位小数很快就会越界。四档类型的 scale 上限分别是 9、18、38、76,连乘之前先想清楚会落在哪个区间。

**除法沿用被除数的 scale,不放大。**

```sql
SELECT toDecimal64(1, 2) / toDecimal64(3, 2);          -- 0.33
SELECT toDecimal64(1, 2)::Decimal(18, 6) / toDecimal64(3, 2);  -- 0.333333
```

结果 scale 就是被除数的 `S1`。想要更多小数位,必须先显式转型成 scale 更大的类型。

**混合位宽只升到较宽的那一档。** `Decimal64(S1) op Decimal32(S2)` 的结果是 `Decimal64(...)`,不会一路升到 `Decimal256`;只有一边本身就是 `Decimal256` 时结果才落在 `Decimal256`。

**和整数混合运算,结果跟着那个 Decimal 走。** `Decimal` 与整数(`Int*` / `UInt*`)做同类运算时,结果仍是 **Decimal**,宽度和标度都取自那个 Decimal 操作数。关键在标度怎么算:**整数被当成"标度为 0"的 Decimal**(它本身没有小数位,也就不贡献标度),于是乘法规则 `S1 + S2` 里的 `S2` 是 0。所以 `Decimal(9,2)` 乘整数 6,scale 仍是 `2 + 0 = 2`,结果是 `Decimal(9,2)`,而不是两个"两位小数"相乘会得到的 `Decimal(9,4)`;一旦积超出 Decimal32 能装的范围,就撞上后面的溢出检查。

**`multiplyDecimal()` 走的是另一套规则,别和 `*` 混。** 普通 `*` 用 `S1 + S2`,而 `multiplyDecimal(a, b)` 用的是 `max(S1, S2)`,并且**总是返回 `Decimal256`**。同一个算式写成 `a * b` 和写成 `multiplyDecimal(a, b)`,类型和小数位都不一样:前者会放大 scale,后者不放大、却被撑到 256 位。需要"不放大 scale 的乘法"就选它,但要知道结果的宽度是固定的。

把上面几条并成一张表,这是本节最该抄下来的一页:

| 运算 | 结果 scale | 结果位宽 |
| --- | --- | --- |
| `+`、`-` | `max(S1, S2)` | 取两边里较宽的那一档 |
| `*` | `S1 + S2` | 取两边里较宽的那一档 |
| `/` | `S1`(被除数的) | 取两边里较宽的那一档 |
| 与整数 `Int*` / `UInt*` 运算 | 整数按 `S = 0` 参与,即 `S1 + 0` | 跟那个 Decimal 走 |
| `multiplyDecimal(a, b)` | `max(S1, S2)` | 恒为 `Decimal256` |

看这张表要抓住的是:**scale 怎么变、位宽落到哪一档,是两条彼此独立的规则**。"精度和标度一起相加"这类总结之所以错,就是因为把它们混成了一句话。接下来两段处理这条规则的两个后果——小数位超出上限怎么办,整数位超出上限又怎么办。

**小数位超出 S 时怎么处理?** 真实 ClickHouse 不做舍入,而是直接把多余的末位**丢弃(截断)**——25.3 的 Decimal 文档原话是 "least significant digits are discarded (not rounded)"。乘法结果也一样:文档里 `multiplyDecimal(toDecimal64(-12.647, 3), toDecimal32(2.1239, 4))` 把 -26.8609633 截到 4 位,得到 -26.8609,而不是"四舍五入"的 -26.8610。想要真正舍入,得自己显式调用 `round` / `roundBankers`。mini-clickhouse 用普通的 `ROUND_HALF_UP`,属于已知差异(见 §03.18)。

**溢出检查:窄 Decimal 会报错,宽 Decimal 会给你一个安静的错误值。** 这是本节最该记住的坑,因为它决定了"算错了会不会有人提醒你"。先分清两种溢出:**小数部分**的溢出被丢弃(上一段的截断),**整数部分**的溢出才会触发检查。`SELECT toDecimal32(4.2, 8) AS x, 6 * x` 会直接抛 `DB::Exception: Decimal math overflow.`。

这个检查由设置 **`decimal_check_overflow`** 控制,**默认打开**,而且管的范围不止算术——**比较**也在内(比如 `toDecimal32(1, 8) < 100` 会抛 `Can't compare.`)。它有性能开销,所以确知不会溢出时可以 `SET decimal_check_overflow = 0` 关掉;代价是溢出会**静默给出错误结果**——官方文档给的例子是 `toDecimal32(4.2, 8)` 乘 6 得到 `-17.74967296`。除非有十足把握,别关。

真正阴的地方在于:**这个检查对 `Decimal128` / `Decimal256` 根本没有实现。** 官方文档原话是 "Overflow check is not implemented for Decimal128 and Decimal256. In case of overflow, incorrect result is returned, no exception is thrown"——一旦你用宽 Decimal 装大数、又算超了,**不会报错,会直接给你一个错的值**,而且 `decimal_check_overflow` 开还是关都救不了。(这也顺带解释了宽 Decimal 为什么慢:CPU 原生不支持 128/256 位整数,运算是模拟出来的。)

**怎么自查。** 三条习惯能把这颗雷拆掉。一是**先估值域**:乘法和连乘最危险,先算结果会不会超过"P 减去 S"剩下的整数位数。二是**主动验算**:用 `isDecimalOverflow(x)` 判断一个值有没有越过所属 Decimal 的整数位范围,用 `countDigits(x)` 数整数位数,再和 P、S 对一对。三是**对宽 Decimal 别指望设置**:真要"溢出就报错",用会报错的转换(`accurateCast` 一类),而不是把 `decimal_check_overflow` 当保险。一句话:**Decimal32/64 溢出会抛异常,Decimal128/256 溢出会给你一个安静的错值。**

## 03.5 String 与 FixedString(N)

**`String` 是任意字节序列,二进制安全,不校验编码。** 所以它只知道"这是 N 个字节",不知道"这是几个字符":

```sql
SELECT length('中文'), lengthUTF8('中文');
-- 6                2
```

`length` 返回**字节数**,`lengthUTF8` 返回**字符数**。要按"字数"限制标题,必须用 `lengthUTF8`;要估算存储占用,用 `length`。同理 `substring` 按字节切,可能把多字节字符切成半个,按字符切要用 `substringUTF8`。

好处是它能存任意二进制(压缩 blob、protobuf);代价是按字符的操作都得显式指明。

**比较和排序是按字节走的,没有排序规则(collation)。** 这是本章最该记住的一句话之一,也是迁移时栽跟头最多的地方:ClickHouse 的 `String` 比较就是**逐字节比较**,`WHERE`、`ORDER BY`、`GROUP BY` 的去重、`DISTINCT` 全部照这条走,`LowCardinality(String)` 也一样。

```sql
SELECT 'Z' < 'a', 'a' = 'A', 'item10' < 'item9';
-- 1            0              1
```

三条读法:大小写**不会被忽略**(`'a'` 和 `'A'` 是两个不同的值,`'Z' < 'a'` 为真是因为 `Z` 的字节 0x5A 比 `a` 的 0x61 小);重音、全半角同理,**没有"不区分大小写/重音"的排序规则可供选择**,MySQL 那种默认 `utf8mb4_..._ci` 的语义在这里不存在;而"字典序"也和直觉不同——字符串按字节比,所以 `'item10' < 'item9'`。要模拟大小写不敏感的比较,只能显式写 `WHERE lower(col) = lower('x')`,同时接受它让这一列上的索引失效。**把这条记住的价值在于:它是"结果对不上"而不是"报错"——迁过来的报表少几行、多几行,往往就出在这里。**

**大小写转换默认只处理 ASCII。** `lower()` / `upper()` 只改 ASCII 的 `A`–`Z` / `a`–`z`,非 ASCII 字节原样不动:`lower('CAFÉ')` 得到的是 `cafÉ`(只有 `C` 变小写,`É` 的两个字节没被碰)。要按 Unicode 规则转换,用 `lowerUTF8()` / `upperUTF8()`。它们**不区分语言**:做的是标准 Unicode 的大小写映射,土耳其语里 `I` / `ı` 那种特例仍会与本地习惯不同;输入的字节序列不是合法 UTF-8 时结果未定义。注意这两个函数改变的是**字符**,不是比较语义——转完之后仍按字节比较,别指望用它们换来"按字母序排序"。

**从别的数据库迁过来的表定义里,这些"类型名"全是 `String` 的别名:`CHAR`、`VARCHAR`、`TEXT`、`BLOB`,以及 `LONGTEXT`、`MEDIUMBLOB` 这一堆。** 关键的一点是:**长度参数会被忽略**。`VARCHAR(255)` 在 ClickHouse 里和 `String` 完全等价——不截断、不报错、也不省空间,列的容量依然是任意长度。所以迁移时看到"长度已经从 `VARCHAR(255)` 带过来了"是错觉,该用 `FixedString` 的地方得手动改。

**`CHAR(N)` 也不等于 `FixedString(N)`。** 在多数数据库里 `CHAR(N)` 是定长类型,但在 ClickHouse 里 `CHAR` 只是 `String` 的别名,`CHAR(16)` 给不了你固定 16 字节。要定长必须显式写 `FixedString(N)`。两者连补齐方式都不一样:传统 `CHAR` 用空格补齐,而 `FixedString` 用**零字节**补齐。

**`FixedString(N)` 是恰好 N 字节的定长字符串。** 两个行为要和 `String` 区分:**不足 N 字节时右侧补零字节**,所以比较时尾部的零字节会影响相等判断;**超过 N 字节直接报错**,而不是截断。它适合长度固定的东西(MD5 摘要 16、IPv6 地址 16),省掉了长度偏移数组,读写更快。长度不确定时不要用。

## 03.6 日期与时间

- **`Date`**:底层是 `UInt16` 的天数,范围 `1970-01-01` 到 `2149-06-06`。
- **`Date32`**:底层是 `Int32` 的天数,范围 `1900-01-01` 到 `2299-12-31`。需要 1970 年前或 2149 年后的日期时用它。
- **`DateTime`**:底层是 `UInt32` 的秒数,范围 `1970-01-01 00:00:00` 到 `2106-02-07 06:28:15`(UTC)。**只有秒精度**。
- **`DateTime64(P)`**:底层是 `Int64` 的刻度数,P 是小数位数(0–9)。P=3 毫秒,P=6 微秒,P=9 纳秒。

`DateTime` 只有秒精度不是因为懒:它是 32 位,想装毫秒就得 64 位,那就是 `DateTime64`。反过来说,**P 越大可表示的范围越小**——纳秒精度大约只能覆盖 1677 到 2262 年。

**`DateTime64` 的两个参数顺序是固定的:精度在前、时区在后。** `DateTime64(3, 'Asia/Shanghai')` 才是"毫秒精度、按上海时区解析和显示";写成 `DateTime64('Asia/Shanghai', 3)` 是错的。只写精度(`DateTime64(3)`)就用服务器配置的时区;和 `DateTime` 一样,时区是**列的属性**,换一个时区显示不会改变底层刻度。转换函数也照同一个顺序写:`toDateTime64(now(), 3, 'UTC')`。这是迁移时的高频笔误——两个参数的类型不同(一个整数、一个字符串),所以写反了通常当场报错,但一旦某处把它写成了字符串常量再传,报错信息就不会指向参数顺序。

**超出值域时会发生什么?** 上面给的范围不是装饰:`Date` 只到 `2149-06-06`,`DateTime` 只到 `2106-02-07`。字符串解析、数值转换、以及窄类型之间的转换都可能越界,而这件事由设置 **`date_time_overflow_behavior`** 决定。它的**默认值是 `ignore`——静默忽略,结果是未定义的**:典型表现是回绕成一个看起来合理的日期,而不是报错。另外两个取值是 `saturate`(钳到目标类型能表示的最小 / 最大值)和 `throw`(直接抛异常)。所以"把一个越界的日期导进来"**默认不会有人提醒你**,要抓到它得显式 `SET date_time_overflow_behavior = 'throw'`(或者用 `'saturate'` 做保护性截断),再把导入流程跑一遍。这条设置同时作用于**文本解析**和**数值转换**两条路径。它还解释了一件看起来无关的事:`第 24 章 §24.8` 提到,把时间列包进窄化转换(`DateTime64` → `Date` 这种)之后稀疏索引就裁剪不了数据了——原因正是越界回绕会让"映射后有序"与"映射前有序"脱节,于是引擎干脆不承认这种转换的单调性。

**时区是列的属性,不是查询的属性。** 这一点和多数数据库不同:

```sql
CREATE TABLE t (
    ts_utc DateTime('UTC'),
    ts_sh  DateTime('Asia/Shanghai')
)
ENGINE = MergeTree ORDER BY ts_utc;
```

两列存的是同一个 UTC 秒数,但**显示和解析时用的时区不同**。不写时区参数时用服务器配置的时区。后果是:同一份数据,在时区配置不同的两台服务器上 `SELECT ts` 打印出来不一样,但底层值相同。跨时区团队一定要在建表时显式写出时区。

时间分桶用 `toStartOfHour`、`toStartOfDay`、`toYYYYMM` 这类函数,它们的结果类型也有差别(`toStartOfDay` 返回 `DateTime`,`toDate` 返回 `Date`)。分区键几乎总是基于它们,见 `第 18 章`。

**时区唯一真正咬人的地方:夏令时(DST)。** 时区不只是一个固定偏移量,它带**夏令时规则**:用 `America/New_York`、`Europe/Berlin`、`America/Santiago` 这类时区,一年里会有一天只有 23 小时、另一天有 25 小时,而且规则本身被各国改过不止一次(靠系统的 tzdata 更新)。于是"按本地日分桶"在切换日附近会给出反直觉的结果:同一个本地日期在 UTC 上的跨度不是 24 小时;回拨那天还会有一段时间在本地钟面上**出现两次**——美国规则下重复的是 01:00–02:00,欧洲规则下是 02:00–03:00,**具体区间取决于该时区自己的切换时刻**;把**本地时间字符串**当输入去解析时,落在这一段上就存在歧义(同一串文本对应两个 UTC 时刻)。修法不是背规则,而是**把"怎么存"和"怎么看"分开**:存一律用 UTC(`DateTime('UTC')`,或者时间戳列不写时区、统一按 UTC 语义写入),分区键与聚合也走 UTC;需要"本地日"时到**查询或展示层**再算——先 `toTimeZone(ts, 'Asia/Shanghai')` 再 `toDate`,或者干脆把"时区"做成一个显式维度列。这样某个国家的 DST 政策下次再变时,你要动的是展示逻辑,而不是重写历史分区。具体时区的切换日期由 tzdata 决定、随系统更新,本书**未在本机核对**任何具体时区的切换日程,别把任何一处写死的日期当依据。

## 03.7 Enum8 / Enum16

`Enum8('a' = 1, 'b' = 2)` 在磁盘上存**整数**(Int8),显示和比较时用**名字**:

```sql
SELECT CAST('a', 'Enum8(\'a\' = 1, \'b\' = 2)');
-- a
```

它和 `LowCardinality(String)` 的区别是取值集合在建表时**固定**,新增取值要 `ALTER TABLE ... MODIFY COLUMN`。集合固定既是限制也是优势:能做更强的校验,插入集合外的值会直接报错,而不是悄悄存进去。

选 `Enum8` 还是 `Enum16`,依据是**成员个数**,不是成员映射到的整数大小——这两件事常被混为一谈:

- `Enum8` 映射到 `Int8`,值域 `[-128, 127]`,但**最多能装 256 个成员**;
- `Enum16` 映射到 `Int16`,值域 `[-32768, 32767]`,**最多能装 65536 个成员**。

所以"枚举项超过 127 个就得换 `Enum16`"是错的:只要不超过 256 个,`Enum8` 都塞得下;而每个成员映射到的整数只要落在 `Int8` 值域内(哪怕全用负数)就更没问题。取值经常变用 `LowCardinality(String)`,取值稳定且需要强校验用 Enum。还有一点容易忘:排序和比较时 Enum 按**底层整数**走,不按名字的字母序——`ORDER BY` 出来的顺序和你写成员时的顺序没有关系。

## 03.8 复合类型

**`Array(T)`。** 一列变长数组,两个必须记住的语义:**下标从 1 开始**;以及**越界不报错,返回元素类型的默认值**——`SELECT [1, 2, 3][5]` 得到 `0`,`Array(String)` 越界得到空字符串。这与多数语言"越界抛异常"相反,是排查"为什么结果是 0"时的常见坑。存储上数组是"偏移量数组 + 拼接的元素列",对压缩友好,但单个元素的随机访问要过偏移量计算。

**`Tuple`。** 与数组的关键区别:**数组是一列多值,元组是多列各一个值**。所以元组的"行数"等于表的行数,数组则是每行一个变长列表。

```sql
SELECT toTypeName(tuple(1, 'a')),
       toTypeName(CAST((1, 'a'), 'Tuple(a UInt8, b String)'));
-- Tuple(UInt8, String)        Tuple(a UInt8, b String)
```

具名元组可以用名字访问 `t.b`,匿名元组只能用 `t.1`(下标从 1 开始)。`Tuple` 很少直接用,主要是 `Nested`、`Map`、`AggregateFunction` 的构件。

**`Map(K, V)`。** 存储上等价于 `Array(Tuple(K, V))`。这里有一个必须先纠正的直觉:**Map 的键在内部并不有序**——ClickHouse 不会为了加速查找把它排好,所以 `m[k]` 的一次查找是**线性扫描**,从头比到尾,代价与这一行里键的个数成正比。它甚至**不要求键唯一**,同一行里出现重复的键是合法的。

后果很直接:`Map` 不是"能被稀疏索引加速的键值存储"。它只适合"每行只有寥寥几个键、而且几乎总是整体取出"的场景,比如一行事件带几个标签。要按 key 快速命中,只有两条路:把经常查的键**提升成真正的列**、让它进排序键;或者用**字典**(第 21 章),它才有真正的查找结构。读键和值可以走子列 `m.keys` / `m.values`,但那只是把内部数组整段取出来,不是查找加速(见 §03.11)。

**`Nested`。** 老写法 `Nested(a UInt8, b String)` 在存储上等价于 `a Array(UInt8), b Array(String)` 这两个平行数组,也等价于 `Array(Tuple(a UInt8, b String))`。**推荐用 `Array(Tuple(...))` 写法**:老 `Nested` 语法在长度、类型推导和函数支持上都有历史包袱,`Array(Tuple)` 才是一等公民。

**复合类型进不进排序键,要看它是不是变长容器。** `Array`、`Map` 这类本身就变长的列写进 `ORDER BY`(或主键)会被直接拒绝;含 `Array`/`Map` 的元组也一样。原因不神秘:排序键要能被稀疏索引按字节切成 granule,而变长容器给不出稳定的全序。`Tuple` 则不同——只要**每个元素自身都可排序**(全由标量组成),它就能进 `ORDER BY` / 主键,`ORDER BY tuple(...)` 也因此是一种惯用法;被拒的只是**元素里含 `Array`/`Map` 的元组**。这也顺带解释了 ClickHouse 为什么偏爱"拍平"的模型——**真正参与裁剪的字段必须是定长的标量列**,一旦塞进变长容器,就失去了被索引加速的机会。

## 03.9 Nullable

`Nullable(T)` 在 `T` 之外多出"空"这个取值。写法简单,代价不简单:

- **每行多一个字节**——`Nullable(UInt8)` 实际占 2 字节。
- **关掉一整类优化**——比较和聚合要先判空再走正常路径,向量化的紧循环被打断。
- **默认不能当主键 / 排序键**——建表时直接被拒绝(有逃生口,见下)。
- **类型会传染**——很多函数遇到 `Nullable` 会返回 `Nullable`,一路传播到上层。

```sql
-- 错误做法:用 Nullable 表示"这个字段可能没有"
-- 后果:每行多一字节;聚合变慢;无法参与排序键
CREATE TABLE t (device Nullable(String)) ENGINE = MergeTree ORDER BY device;

-- 正确做法:用类型自带的零值表示缺失,空字符串即"未知"
CREATE TABLE t (device LowCardinality(String)) ENGINE = MergeTree ORDER BY device;
```

有一个逃生口:`CREATE TABLE ... ORDER BY device SETTINGS allow_nullable_key = 1` 会允许把 `Nullable` 列放进排序键。但它只是把报错挪走,并没有把代价消掉——排序键上带 `NULL`,稀疏索引的比较、分区裁剪和一批基于 min/max 的优化都要为此退化,还要额外给 `NULL` 定义在全序里的位置。所以 `allow_nullable_key` 的定位是"迁移旧表时先让它建起来",不是"新设计里就该这么写";默认拒绝才是对的方向。

官方建议是**能不用就不用**,只有"零值和缺失必须区分"时才用——"温度 0 度"和"没测到温度"是两回事,这才需要 `Nullable`。另外 `Nullable(Nullable(T))` 非法,`Array(Nullable(T))` 合法。

**代价到底有多大:先看它按什么比例增长,再自己量一遍。** 机制上,`Nullable` 不是"在值里塞一个特殊值",而是**另存一列"空值位图"**(每行一个字节,不是一位)。所以它的相对开销是可算的:与被包裹的列宽成**反比**——`Nullable(UInt8)` 从 1 字节变 2 字节,**翻了一倍**;而 `Nullable(UInt32)` 是 4 变 5,只多 25%;对 `Nullable(String)` 这种本身就不定长的列,额外的一列通常只占个位数百分比。**"Nullable 让查询慢 3 倍"这类说法只在窄列上才可能成立**,别把它当成对任意列都适用的经验值。

要量出自己表上的真实数字,做一组**只改类型、其余全同**的对照表(同样的行、同样的排序键与 CODEC),然后看两处:

```sql
-- ① 存储侧:列级压缩后的字节数
SELECT table, column, column_data_compressed_bytes, column_data_uncompressed_bytes
FROM system.parts_columns WHERE active AND column = 'device';

-- ② 计算侧:同一查询扫过的行数与字节数(只看它,别只看墙钟)
SELECT query, read_rows, read_bytes, ProfileEvents['SelectedRows'] AS selected
FROM system.query_log WHERE type = 'QueryFinish' ORDER BY event_time DESC LIMIT 10;
```

**①有一个前提,不写清就会量出一片 0。** `system.parts_columns` 的 `column_data_compressed_bytes` **对 Compact part 恒为 0**。这里的 Compact 不是压缩算法,而是 part 的**物理布局**:小 part 默认把所有列的数据挤在一个文件里(Compact),列与列不分开,于是"这一列压完多少字节"根本没有单独的数字可报,只有表级的 `system.parts.data_compressed_bytes`。拿这套配方量 `Nullable` / `LowCardinality` 的收益时,如果表里全是 Compact part,你会读到一列 0,然后误以为"改了类型什么都没变"——那只是量错了地方。两个对策:建对照表时加 `SETTINGS min_bytes_for_wide_part = 0, min_rows_for_wide_part = 0` 强制写成 Wide part(列各占自己的文件);或者先 `OPTIMIZE TABLE ... FINAL` 把 part 合到足够大再量。`system.parts` 的 `part_type` 一列会告诉你每个 part 是 `Wide` 还是 `Compact`,量之前先看它一眼。

读法:①告诉你**存**贵了多少,②告诉你**算**贵了多少。两者要分开看——`Nullable` 常常是"存上几乎没变、算上掉了几个百分点",而不是"两头都翻倍"。这也是为什么"用零值代替空值"的正确判据不是"省了多少字节",而是"**零值与缺失在业务上是否真的不需要区分**":能区分就值得付这点代价,不能区分就该老老实实换成非空类型。

## 03.10 LowCardinality

`LowCardinality(T)` 用**字典**存这一列:每个不同的值在字典里出现一次,数据部分只存下标。取值重复率高时(比如 `country` 只有几十个值),列会变得很小,比较的也是整数下标。

**适合**取值个数少(几十到几千)、重复率高、又总出现在 `WHERE`/`GROUP BY` 里的字符串列。**不适合**高基数列(比如把 `user_id` 做成字符串)——字典会膨胀到比原始数据还大,每次插入还要查字典。这类值直接存整数类型。

**嵌套顺序只有一种合法写法:** `LowCardinality(Nullable(T))`。也就是说,"可空"写在里层、`LowCardinality` 在外层;反过来写 `Nullable(LowCardinality(String))` 是**非法**的,建表会被直接拒绝(报错大意是"该类型不能放进 Nullable 里")。原因不神秘:ClickHouse 把低基数可空列定义成"字典里存 `Nullable` 值",而不是"把整列包进 `Nullable`"。要把一列改造成可空的低基数列,照 `LowCardinality(Nullable(String))` 写,别凭直觉把 `Nullable` 套在最外层。

**"自动降级"要理解准确。** 这里的关键设置是 `low_cardinality_max_dictionary_size`,**文档默认值是 8192**(行)。它的行为不是"一超过上限就整表退回普通列",而是**按 part 判定**:某个 part 里这一列的不同取值数一旦超过上限,该 part 的**这一列就退化成普通编码**(不再用字典,按原类型逐值存),压缩率随之回落到普通列的水平。之所以强调"按 part",是因为同一张表的不同 part 可以处在不同状态——取值数没超限的老 part 仍在用字典,新写入、已超限的 part 已经退回普通编码。还有一点最常被踩:**这个设置是服务端 / 用户级的,不是列级的**——你没法给某一列单独设,要改只能改用户配置(`users.xml` 或 `users.d/` 里的 profile);而且**改完通常要写进用户配置(`users.xml` 或 `users.d/` 里的 profile)并重启服务端才生效**——会话级 `SET` 往往不改变已有列的行为,以目标版本为准。实践中的建议是干脆别碰它:它属于面向整机 / 用户的底层旋钮,而按前面那条经验法则,字典本身超过一万个不同值就不该用 `LowCardinality` 了。

所以把 `LowCardinality` 用在高基数列上**不会报错,只会变大变慢**——选错类型时它不会救你。

**"取值少"不是唯一的判据,还要看这一列在磁盘上是不是已经被聚簇。** `LowCardinality` 的收益来自**压缩之前先去重**:它把"重复的字符串"换成"重复的小整数",从而让后面的通用压缩有更少的东西要处理。但如果这一列**本身就是排序键的一部分**(或者数据恰好按它有序),相同值早已物理相邻,通用压缩器本来就能把那一长串相同字节压到近乎为零——这时 `LowCardinality` 能挤出的空间就少得多,极端情况下甚至可能因为"字典 + 下标列"两层结构的固定开销而略亏。这正是它最常被用错的场景:在 `ORDER BY (country, ts)` 的表上再给 `country` 套一层 `LowCardinality`,收益远不如给一个**不收进排序键、却在 `WHERE` / `GROUP BY` 里反复出现**的列套上它。

判断只能靠量,而且是**同一列的前后对照**,配方与 §03.9 给的那两条查询完全一样(改类型用 `ALTER TABLE ... MODIFY COLUMN` 触发后台重写,重写完成后 `system.parts_columns` 上那一列的 `column_data_compressed_bytes` 才是新值;老 part 没重写完之前,你会同时看到两组数)。这里要连着 §03.9 那条**Compact part 恒为 0** 的提醒一起读:量之前先确认这些 part 是 Wide 布局,否则你比的是两组 0。**不要把任何一处给出的倍数当结论**,它随数据分布、CODEC 与排序键三个因素一起变。

## 03.11 子列访问

复合类型的"内部字段"可以像列一样读,这叫**子列(subcolumn)**,写法是 `列名.子列名`。它不是额外的存储,而是类型系统在读取时从父列里切出来的一段——读 `arr.size0` 不必把数组元素本身拉出来,这正是它比"先取整列再算长度"划算的地方。

**`Array`**:`arr.size0` 给出每行的元素个数(结果是一列 `UInt64`)。多维数组按维度往下取,`.size0` 是第一维长度、`.size1` 是第二维,以此类推。

**`Tuple`**:具名元组按名字取 `t.name`,匿名元组按位置取 `t.1`、`t.2`……(下标从 1 开始)。子列的类型就是那个元素的类型,还可以继续往下钻(`t.a.b`)。

**`Nested`**(老写法):在默认的 `flatten_nested = 1` 下被拆成平行数组,于是 `n.a`、`n.b` 都是 `Array(...)`,而 `n.size0` 是每个内嵌数组的长度。换成 `Array(Tuple(...))` 写法后只有一个列 `n`,子列照样按名字取。

**`Map`**:`m.keys` / `m.values` 分别读出键数组和值数组,等价于 `mapKeys(m)` / `mapValues(m)`。它们省掉的是"把整张 map 读出来"的开销,但**顺序取决于存储序列化**(普通序列化是写入顺序,分桶序列化是哈希桶顺序),既不保证是插入顺序、更不保证有序,也不能当查找来用。

**`JSON`**:`json.a.b` 是真正的子列,详见 §03.13。

`DESCRIBE TABLE` 默认**不**列子列。想看,得先 `SET describe_include_subcolumns = 1`,这时输出会多出子列行,并用 `is_subcolumn = 1` 标出来,列名就是父列名加 `.子列名`。排查"`t.a` 明明能查、`DESCRIBE` 里却没有 `a`"时,先想起这一条。

子列和 `ALIAS` 列长得像、语义不同:子列这个名字由**类型**派生出来,`ALIAS` 列的名字由**表达式**定义。区别在 `DESCRIBE` 里看得最清楚——前者带 `is_subcolumn` 标记,后者带 `default_type = ALIAS`,而且读子列不会触发任何表达式求值。

## 03.12 AggregateFunction 与 SimpleAggregateFunction

这两个是**能直接写进建表语句的列类型**,但它们存的东西和你心里想的"值"不一样。

`AggregateFunction(func, 参数类型...)` 存的是聚合的**中间状态**(partial state),不是聚合结果。`AggregateFunction(sum, UInt64)` 这一列里躺着的是一段序列化后的求和状态——具体编码属于内部实现,不该被依赖。

关键推论是:这种列**既不能直接读也不能直接写**,必须成对地使用两个组合子:

- 写入用 `-State`:`sumState(x)` 产生状态,例如 `INSERT INTO agg SELECT d, sumState(v) FROM raw GROUP BY d`;
- 读取用 `-Merge`:`sumMerge(state)` 把若干状态合并成最终结果。

直接 `SELECT state` 只会看到二进制中间态。它是 `AggregatingMergeTree`(§04.3)与增量物化视图的底层机制——**预聚合的结果以状态形式落盘,查询时再合并**,所以同一个分组被反复插入也不会被覆盖。状态与组合子的完整体系在 `第 12 章`。

`SimpleAggregateFunction(func, T)` 是它的轻量版:只适用于那些**中间状态就等于最终值**的聚合,列的实际类型直接就是 `T`,写入和读取都跟普通列一样,**不需要 `-State` / `-Merge`**。代价是它只覆盖一部分聚合函数。能用 `SimpleAggregateFunction` 就别用 `AggregateFunction`:前者更小、合并更直接、查起来也更顺手。**判据只有一条:凡是能用一个中间值就直接合并完的聚合才适合它**——典型的一族是 `sum`、`min`、`max`、`any`、`anyLast`、`count` 这类"两个值合并成一个值"的;而需要多值状态的(`uniq` 的 HLL 草图、`quantile` 的分位结构)必须用 `AggregateFunction`。这一族可能随版本增补,清单若无法穷举,同样以目标版本文档为准。

## 03.13 半结构化:JSON、Variant、Dynamic

半结构化数据以前只有一种存法:丢进 `String`,查询时用 `JSONExtract*` 现场解析——既慢,又建不了索引。25.3 起原生类型把这条链路补齐了。

**`Dynamic` 让类型由值决定。** 一列 `Dynamic` 里可以同时放整数、字符串、数组,类型按**每一行的实际值**推断。它用起来最省事,代价是类型不再是列的属性:写错了没有建表期的校验,而且每种动态子类型都要维护一段独立存储。

**它有一个子类型数上限。** 单个数据块(对 MergeTree 来说就是一个 part)里,只有这么多**不同**的类型能各占一段独立子列;超出之后剩下的类型会被塞进一块共享结构——仍能读,但更慢、更耗内存。这个上限用 `Dynamic(max_types = N)` 调整,可写范围是 0–254,**文档默认值是 32**;同族的 `JSON` 类型里对应参数是 `max_dynamic_types`,**默认值同样是 32**。调大它会让更多类型各占一段、读得更快,代价是占用更大。

调整之前先记住两件事。一是**这个默认值随版本变过**,换版本时以目标版本文档为准。二是别把文档里的示例值当默认值:有些示例会把它特意调到 16(比如嵌套 `Array(JSON(...))` 的例子),那是示例为控制子列数量做的收紧,不是默认。

**`Variant(T1, T2, ...)` 把可能性逐一列出。** 值必须是列举类型之一,类型集合在建表时就固定,比 `Dynamic` 更受控。读某一支用子列:`v.UInt64` 得到"这一行确实是 `UInt64` 时它的值,否则为 `NULL`"。列举的类型个数有上限(实现上判别列占用一个字节,分支数因此有个不大的天花板),但**官方文档没有承诺一个确切数字,具体上限未确认**——分支很多时,先在目标版本上实测。

**`JSON` 是三者里最像"一列半结构化数据"的。** 它在 `Dynamic` 之上把每个路径提升成真正的子列,于是 `json.user.id` 既能类型安全地读、又能建跳数索引。声明时可以**显式给出路径和类型**:

```sql
CREATE TABLE t (
    id   UInt64,
    data JSON(a UInt32, b String, SKIP c.d)
)
ENGINE = MergeTree ORDER BY id;
```

- 写进声明的路径(`a`、`b`)按指定类型存,读 `data.a` 得到 `UInt32`;
- `SKIP`(以及 `SKIP REGEXP`)声明"这些路径永远不查",直接省掉它们的存储;
- 没声明的路径按 `max_dynamic_paths`(默认 **1024**)存成动态子列,超出的部分落进一块共享结构,仍然能读但更慢——所以不建议把这个值调得很大;
- 读一条不存在的路径得到 `NULL` 或默认值,不会报错。

想看清一个 `JSON` 列里到底有哪些路径,用内省函数:`JSONAllPaths(json)` 返回所有路径,`JSONAllPathsWithTypes(json)` 连类型一起给,`JSONDynamicPaths(json)` 只看动态路径,`JSONSharedDataPaths(json)` 则是溢出到共享结构里的那部分。

有一条版本事实值得记住:**控制它的开关从 25.3 起默认为开**。在这之前的版本里 `JSON` 是实验特性,得先 `SET` 成 `1` 才能用,而且行为可能和现在不同。这里顺带纠一个顺着名字就能踩的坑:这个开关有两个名字,平时最常写的 **`enable_json_type` 是别名**,25.3 源码里声明的那个才是 **`allow_experimental_json_type`**(写作 `DECLARE(Bool, allow_experimental_json_type, true, ...) ALIAS(enable_json_type)`)—— 同一套命名在 `enable_analyzer` / `enable_dynamic_type` / `enable_parallel_replicas` 上重复出现,规律见第 24 章 §24.17。写哪个都能跑,但**别因为名字里没有 `experimental` 就以为它不是实验档**,成熟度得看 `system.settings` 与文档徽标,不看前缀。

另一个坑是旧写法 `Object('json')`——它已被废弃,新表不要再用,它和原生 `JSON` 在语义上并不等价。

`JSON` 的路径参数、类型推断规则、跳数索引支持都还在演进,**具体默认值以目标版本文档为准**。

**老办法还是新类型,看你怎么用它。** 在原生 `JSON` 之前,半结构化数据只有一条路:整份原文塞进 `String`,查询时用 `JSONExtractInt` / `JSONExtractString` / `JSONExtract` 这类函数现场解析。这条路今天仍然有用,判据是**你打算怎么用它**:如果结构**极不稳定**(每个来源、每个版本的字段都不一样)、而且你**只是偶尔从里面抠一两个字段**出来看,那 `String` 就够了——反正也不指望按路径过滤或聚合,解析开销摊在少量查询上可以接受;反过来,如果**要按路径过滤、聚合,或者要让团队里多个人反复查它**,那就该迁到原生 `JSON`:路径被提升成子列后,读 `data.user.id` 只碰这一路径对应的一段存储,而不是把整份原文重新解析一遍,也才谈得上给子列建跳数索引。一句话,`JSONExtract*` 是"查询时付费",原生 `JSON` 是"写入时付费、查询时收割"——字段少、查询少,前者划算;字段多、查询多、还要下推过滤,后者才回本。

**摄入侧怎么写,取决于数据进来时是什么形状。** 最常用的是 `JSONEachRow`:每行一个独立的 JSON 对象,一行落成一行,适合"一行一条记录"的日志 / 事件流;它并不要求目标列是 `JSON` 类型,字段按列名映射到普通列即可——反过来,拿它去灌"整个文件是一个大数组 / 大对象"的数据就会解析失败,那是形状对不上。`JSONAsString` 把每个输入值**原样塞进一个 `String` 列**,不做解析,适合"先把原文存下来、之后再慢慢处理"的落地链路(常见做法是配一张物化视图,把原文投喂出去再解析)。`JSONAsObject` 要求目标表**只有一列、且类型是 `JSON`**(或旧的 `Object('json')`),把每个输入值直接当成那一列——适合"整份文档本身就是一列半结构化数据"的模型,目标列不是 `JSON` 时会直接报错。挑错写法的典型症状就是这两条:形状对不上、目标列类型不对。

**热路径要不要"落实"成列,有两种手段,含义不同。** 一种是**物化列**(老办法):建一个 `MATERIALIZED` 列,表达式里用 `JSONExtract*` 把那个路径抠出来。它一定是个真列,能进排序键、也能建跳数索引,代价是每次写入都要算一遍,而且结构一变就得改表。另一种是 **`JSON` 的类型提示(hint)**:在列声明里写 `JSON(a.b UInt32)`,让这个路径按指定类型存成子列;它比物化列轻,不用单独维护表达式。经验法则:**既要按它排序、又要在它上面建索引的路径,用物化列最稳**;只是想让读取快一点、类型明确一点,用 hint 就够。要留意的是,`JSON` 子列对排序键 / 跳数索引的支持程度还在演进(见下面"以实测为准"那条),别把 hint 直接当成"等于真列"。

**`Map` / `Nested` / `JSON` 怎么选。** 三者都能表达"一个键对应一个值",但形状不同:`Map(K, V)` 要求所有键同类型、所有值同类型,适合"同质的标签集合";`Nested` 是**字段在建表时写死的结构数组**,适合"每行挂一组固定字段的记录",每个子字段还能按列名点出来;`JSON` 则把路径完全放开,适合"字段集合事先说不清、每个文档还不一样"。可以记成:结构越确定、越规整,越往 `Map` / `Nested` 靠;结构越散、越不可预知,越该用 `JSON`——因为 `JSON` 的灵活是有代价的,路径多到失控时,动态子列与共享结构都会拖慢它。

**有几件事本书不下结论,以目标版本实测为准。** 第一,`JSON` 路径类型推断的稳定性——同一条路径这次推断成 `Int64`、下次可能不同;第二,动态子列在后台合并时会不会重排、重排对读取有什么影响;第三,跳数索引对 `JSON` 子列的支持到什么程度。这三点都属于"演进中、没有稳定承诺"的地方(第 32 章 §32.6 把它们一并标为"未确认"),要用它们做设计,先在目标版本上跑一遍,别照搬任何一版的文档。

## 03.14 地理类型

一组薄封装的几何类型,底层就是嵌套数组:

- `Point` = `Tuple(Float64, Float64)`,常按"经度、纬度"解释,但顺序是约定而非强制;
- `Ring` = `Array(Point)`,首尾点不写全也视为闭合;
- `Polygon` = `Array(Ring)`,第一个环是外边界,其余是内洞;
- `MultiPolygon` = `Array(Polygon)`。

**它们就是嵌套数组,只多了一层约定。** 逐层拆开看:一个 `Point` 是两元素元组(两个 `Float64`),`Ring` 是若干 `Point` 组成的数组,`Polygon` 是若干 `Ring` 组成的数组,`MultiPolygon` 再套一层。所以"一个 `Polygon` 列"在存储上和你手写的 `Array(Array(Tuple(Float64, Float64)))` 没有本质区别,地理类型只是给这套嵌套定下几条语义:第一层是外环、其余是内洞、环默认视为闭合。

**怎么用。** 最典型的场景是"判断一个点落不落在某块围栏内"。围栏可以直接写成数组字面量,不必先建一张表:

```sql
SELECT pointInPolygon((3., 3.), [(6, 0), (8, 4), (5, 8), (0, 2)]);
-- 1   (点落在四边形内)
```

落到表上就是逐行调用:

```sql
SELECT count() FROM sites
WHERE pointInPolygon(site_point, [(6, 0), (8, 4), (5, 8), (0, 2)]);
```

常用的函数分三类:**位置判断**(`pointInPolygon`、`pointInPolygonCartesian`、`pointInEllipses`)、**距离与角度**(`greatCircleDistance`、`geoDistance`、`greatCircleAngle`)、**多边形之间的关系**(`polygonsIntersect`、`polygonsWithin`)。这一族的名单随版本增补,具体以目标版本 `system.functions` 里的实际条目为准。用法上它们几乎总是作为**输入参数**出现在查询里,计算发生在查询期,和这一列怎么存关系不大——函数不关心列里存的是什么形状,只按位置解读那串数字。

**四条边界,决定了它不能当空间索引使。** 第一,**没有空间索引**:本质是变长数组,既进不了排序键(§03.8 那条规则同样适用),也没有 R 树那类空间结构,`pointInPolygon` 依然逐行、逐个多边形地判断,大表上照样全扫。第二,**顺序是约定**:`Point` 里先经度还是先纬度取决于你和调用方的约定,写反了不报错,只会给出错误结果。第三,**它是近似的**:球面或平面的简化计算,不是投影坐标系里的精确测量。第四,**要按空间快速裁剪得另想办法**:把包围盒的经纬度拆成独立数值列、放进排序键或跳数索引做一次粗筛,把候选行缩小,再对这几行调用几何函数。

**成熟度。** 官方 CHANGELOG 把 `Point` / `Ring` / `Polygon` / `MultiPolygon` 明确标为 **production-ready(生产就绪)**,在 25.3 上是稳定类型而非实验特性——早年(21.x–22.x 那批)才需要的 `allow_experimental_geo_types` 开关如今已不再需要。

## 03.15 其余类型速览

**`UUID`** 是 128 位、存 16 字节,比较和排序按数值而不是字符串。

**`IPv4` / `IPv6`** 在磁盘上分别是 4 字节的 `UInt32` 和 16 字节的定长值。用专门类型而不是 `String`,省空间、能用 `IPv4NumToString` 这类函数,比较也是数值比较。

**`Bool`** 底层就是 `UInt8`,`true`/`false` 是显示层的糖,别指望它只占一位。

**`Interval`** 是一组类型(`IntervalSecond`、`IntervalDay` 等),底层是 `Int64` 计数,主要用于日期运算,几乎不会出现在表定义里。

**`Nothing`** 只有一个值的类型,用于表达"永远为空"。最实际的用途是:字面量 `NULL` 的类型就是 `Nullable(Nothing)`,所以 `SELECT toTypeName(NULL)` 得到的不是 `String` 之类。

此外还有 `BFloat16`、`QBit`、`Function` 三类,它们各有专门的用途,但日常建表很少直接碰到;这三类在 mini-clickhouse 里都没有实现,用到它们的例子在 mini 上跑不了(地理类型同样没实现,见 §03.18)。

## 03.16 类型转换

**`CAST(x AS T)` 与 `toXxx(x)`。** 多数情况下等价,但 `CAST` 支持的类型转换更多,`toXxx` 写起来短且对同类型输入会优化成空操作(如 `toString(String)`);`x::T` 是 `CAST` 的简写。

**`accurateCast`** 在转换失败或超范围时**报错**而不截断:

```sql
SELECT toInt8(1000);                     -- -24    (按位截断)
SELECT accurateCast(1000, 'Int8');       -- 报错:Value is out of range
SELECT accurateCastOrNull(1000, 'Int8'); -- NULL
```

需要"宁可失败也不静默给错值"的地方用它。反过来说,凡是用 `toXxx` 又没做范围检查的地方,都要问一句:这个值真的装得下吗?

**`reinterpretAsXxx`** 不做数值转换,而是**按位重新解释同一段内存**:

```sql
SELECT reinterpretAsString(toUInt32(1));
```

`toUInt32(1)` 是 4 个字节,二进制为 `00000000 00000000 00000000 00000001`。重新解释成 4 字节字符串时,在**小端机器**上低位字节排在最前,所以结果的 4 个字节依次是 `0x01 0x00 0x00 0x00`——一个值为 1 的字节,后面三个零字节:

```sql
SELECT hex(reinterpretAsString(toUInt32(1)));
-- 01000000
```

在终端里直接看这个结果会像"一个奇怪的字符",因为显示层把不可见字节渲染成了控制字符。这正是它最常见的用处:把数值按内存布局喂给需要二进制的下游(哈希、加密、网络协议)。反向的 `reinterpretAsUInt32` 做同样的重解释。

要注意它的结果**依赖主机字节序**——`reinterpretAs*` 用的是机器本机字节序(主流平台是小端),所以把它当可移植的序列化格式是错的。需要确定的字节序时,别指望"给转换加个字节序参数"(`toUInt32` 之类根本没有这种签名):正确做法是用 **`byteSwap*` 家族显式翻转字节**,比如 `byteSwap32(x)` 把 32 位整数在大端 / 小端之间来回翻;需要可移植的文本表示则走 `hex()` / `toString()`。`reinterpretAs*` 本身只做"按本机字节序重解释",不提供字节序选项。

## 03.17 类型推导

**字面量数组的公共类型。** 数组元素必须能归到一个公共类型,否则报错:

```sql
SELECT [1, 2, 3];   -- Array(UInt8)
SELECT [1, 'a'];    -- 报错:no supertype for types UInt8, String
SELECT [toUInt8(1), toUInt8(2)];   -- 显式统一
SELECT [1::String, 'a'];           -- Array(String),1 变成 '1'
```

**二元运算的类型提升。** 两个不同类型做算术时结果按"更宽的那个"定,大方向是:浮点压整数,同族(同为无符号或同为有符号)取更宽者再进一档(封顶 64 位)——所以 `UInt8 + UInt16` 得到 `UInt32`(§03.2 那张表)。**有符号与无符号混用**是另一条规则:真实 ClickHouse 统一**转为有符号**,位宽仍按上面那条走,于是 `UInt64 + Int8` 得到 `Int64`——无符号那侧的 64 位放不进有符号的 64 位,所以这里**可能溢出**。mini-clickhouse 在这里做了一个保守偏离:`UInt64 + Int8` 会得到 `Int128`,理由与代码出处见 §03.18。

**`NULL` 的类型**是 `Nullable(Nothing)`。它和任何类型组合都会得到那个类型的 `Nullable` 版本,这也是"为什么加了一个 `NULL` 字面量,整个表达式就变成 Nullable"的原因。

## 03.18 与 mini-clickhouse 的对照

mini-clickhouse 的类型系统在 `mini-clickhouse/minich/types/` 下:`base.py` 是基类与注册表,`scalars.py` 是标量类型,`composite.py` 是复合类型。它刻意照搬了真实 ClickHouse 的两段式设计——**`DataType` 回答"这是什么类型",`IColumn` 回答"值存在哪里"**,两者由 `DataType.create_column()` 连接(见 `第 31 章`)。

**它实现了**全部 12 种整数、`Float32/64`、`String`、`Bool`、`UUID`、`IPv4`、`IPv6`、`Date`、`Date32`、`DateTime`(带时区)、`DateTime64`(精度 + 时区)、`Decimal(P,S)` 及 `Decimal32/64/128/256` 别名、`Enum8/16`、`Interval` 家族、`Nothing`、`Dynamic`、`JSON`,以及 `FixedString(N)`、`Nullable`、`Array`、`LowCardinality`、`Tuple`、`Map`、`Nested`、`Variant`、`AggregateFunction`、`SimpleAggregateFunction`。

**它没有实现** `BFloat16`、地理类型、`QBit`、`Function`;用到这些类型的例子在 mini 上跑不了。

**几处刻意保留的差异:**

- **整数溢出的语义基本一致**。`DataTypeNumber.wrap()` 就是按位回绕,算术的"进一档"和 `sum` 的类型提升也都实现了,所以 §03.2 里"算术会进一档、`sum` 不溢出"的结论可以在 mini 上复现。`sumWithOverflow` 也在 `minich/aggregates/sum.py` 里按"结果类型 = 输入类型"实现,§03.2 里那个 144 的例子能在 mini 上直接跑出来。**对不上的是 `arraySum`**:mini 的 `arraySum` 返回元素类型(不做提升),所以 `arraySum([toUInt8(200), toUInt8(200)])` 拿到的不是 400,而是按 §03.2、本节讲的 `DataTypeNumber.wrap()` 按位回绕后的 **144**(`400 & 0xFF`);真实 ClickHouse 的 `arraySum` 走 `sum` 的提升语义,得到 `UInt64` 的 400。
- **`Decimal` 算术的位宽推导与真实 ClickHouse 不同**。mini 给 `+`/`-`/`*` 的结果都把两边精度(P)相加:`toDecimal32(1.23, 2) * toDecimal32(4.56, 2)` 得到 `Decimal(18, 4)`、`toDecimal32(1, 2) - toDecimal32(1, 2)` 得到 `Decimal(18, 2)`、`toDecimal64(1, 2) - toDecimal32(1, 2)` 得到 `Decimal(27, 2)`。真实 ClickHouse 位宽只取较宽的那一档,同样三个算式分别是 `Decimal(9, 4)`、`Decimal(9, 2)`、`Decimal(18, 2)`。**scale 的规则两边是一致的**:`+`/`-` 取 `max(S1, S2)`、`*` 取 `S1 + S2`、`/` 取 `S1`。另外 `multiplyDecimal()` mini 未实现;§03.4 里 `Decimal32(8) * Decimal32(8)` 抛 `Scale is out of bounds.` 是真实 ClickHouse 的行为,mini 不会报这个错。
- **浮点转整数是截断(向零)**,与真实 ClickHouse 一致。
- **`UInt64 + Int8` → `Int128`**,而真实 ClickHouse 给 `Int64`。这是 mini 为了不溢出做的保守偏离,写在 `get_wider_numeric_type` 的文档字符串里(也见 §03.17)。
- **`Decimal` 多余小数位的处理不同**:mini 用普通的 `ROUND_HALF_UP` 四舍五入,真实 ClickHouse 则**直接截断丢弃**(文档原话 "discarded (not rounded)"),所以凡是"末位需要进位"的地方,两边结果会差 1。
- **子列访问只实现了一部分**。mini 支持 Tuple 的位置/名字访问(`t.1`、`t.a`),但 **`Array.size0` 与 `Map.keys` / `m.values` 未实现**——`.size0` 会被当成 `tupleElement`,报 `ILLEGAL_TYPE_OF_ARGUMENT`。所以 §03.11 举的那些子列里,只有 Tuple 一条能在 mini 上跑。
- **`AggregateFunction` / `SimpleAggregateFunction` 作为列类型注册了**(`minich/types/__init__.py`),配套的 `-State` / `-Merge` 组合子在 `minich/aggregates/combinators.py` 里也注册了;§03.12 说的"状态存列、查询时合并"这条链路对应的是 `AggregatingMergeTree`,示例在 `第 12 章`。
- **`Object('json')` 映射到新 `JSON` 类型**,而真实 ClickHouse 把它视作已废弃的旧写法;`String` 在 Python 层只是 `str`,不做字节级存储。

## 03.19 小结

类型在 ClickHouse 里同时决定存储、序列化、函数和压缩,选型不是填声明那么轻。

整数按值域选最小够用的;溢出会回绕,但算术会提升类型所以不轻易回绕,`sum()` 也自动提升所以不溢出——真正会回绕的是 `sumWithOverflow` 这种把结果类型钉死的函数,而不是"数组聚合"之类的一大类。浮点只用于本来就有噪声的场景,钱用 `Decimal`——乘法**只**放大 scale、位宽取较宽的那一档,除法沿用被除数的 scale,scale 相加撞到上限就报 `Scale is out of bounds.`,是最容易算错的几条;只写一个参数的 `Decimal(P)` 标度默认 0(只能存整数),别忘了补上标度。

`String` 是任意字节,`CHAR`/`VARCHAR`/`TEXT`/`BLOB` 都只是它的别名、长度参数被忽略,`length` 是字节数、要字符数得用 `lengthUTF8`;比较与排序**按字节走、没有排序规则**(`'Z' < 'a'`、`'a' ≠ 'A'`),`lower()` / `upper()` 只处理 ASCII,要按 Unicode 转换得用 `lowerUTF8()` / `upperUTF8()`;`FixedString(N)` 补零、超长报错。时区是列的属性,`DateTime64` 的参数顺序是**精度在前、时区在后**;越界的时间转换默认**静默回绕**(`date_time_overflow_behavior` 默认 `ignore`),要报错得显式设成 `throw`。`Array` 下标从 1 开始、越界返回默认值。变长复合类型(`Array`/`Map`,以及含它们的元组)不能进排序键,而元素全为可排序标量的 `Tuple` 可以(`ORDER BY tuple(...)` 是惯用法);`Map` 的键无序、查找是线性扫描。这些内部字段可以走**子列**读:`arr.size0`、`t.1`、`m.keys`,而 `DESCRIBE` 默认不列子列,要看先开 `describe_include_subcolumns`。

`Enum` 按底层整数排序,`Enum8` 上限是 256 个成员而不只是 127 个值。`Nullable` 每行多一字节且关掉优化,默认不能进排序键(`allow_nullable_key` 能绕开但代价仍在)。`LowCardinality` 只适合低基数列。`AggregateFunction` / `SimpleAggregateFunction` 是给物化视图存中间状态的列类型,必须配 `-State` / `-Merge` 使用。半结构化数据从 `String` + `JSONExtract*` 升级到了原生的 `JSON` / `Variant` / `Dynamic`,但默认值和上限随版本变,用之前查目标版本。

转换上,`toXxx` 截断、`accurateCast` 报错、`reinterpretAs*` 按位重解释。类型推导上,字面量挑最小够用的类型,`NULL` 是 `Nullable(Nothing)`。

下一章换一个维度:数据放在什么样的表里。引擎决定"怎么写、怎么读、后台做什么",而十几个引擎里 `MergeTree` 一家占了绝大多数生产场景。
