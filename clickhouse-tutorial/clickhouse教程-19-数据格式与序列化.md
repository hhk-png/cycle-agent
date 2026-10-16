# 第 19 章 数据格式与序列化

`FORMAT` 是 ClickHouse 里最容易被低估的一块。大多数人以为它只管"把结果打印得好看一点",其实它承担的是**数据进出系统的唯一通道**:`INSERT` 的数据从格式里来,`SELECT` 的结果从格式里走。任何一个字段转义错了,都会变成"导出的数据再导回来就变了"这种最难查的 bug。这一章讲清楚两件事:格式决定了"能不能无损往返",以及列存格式为什么在"只读几列"的查询上是数量级的差别。

## 19.1 什么才算"无损往返"

一个格式"能不能无损"不是感觉,而是四条可以逐条检查的判据:

- **类型信息**:能不能从数据本身还原出列的类型。`TSV` 里光看 `1` 无法知道它是 `UInt8` 还是 `String`,所以它**必须**配一个外部 schema;`RowBinaryWithNamesAndTypes` 和 `Native` 把类型写进了文件。
- **`NULL` 与空串的区分**:这是最容易被忽视的一条。`NULL`、空串 `""`、字符串 `"NULL"` 三者必须能分别往返。
- **转义**:分隔符、引号、反斜杠、换行出现在值里时怎么办。转义表里多一条或少一条,往返立刻失败。
- **精度**:`Decimal(38,18)` 的 `99999999999999.9999` 一旦被中转成 `float64`,就只剩约 15 位有效数字,再写回去就不等了。凡是经过"通用 JSON 解析器"的数据,都要检查它有没有把数字转成浮点。

一个格式只要在四条上有任意一条不满足,它就不是无损的——这时正确做法是在文档里**写清楚哪一步丢了信息**,而不是假装它能往返。

## 19.2 格式从哪里指定,以及 FORMAT 的位置语义

同一个格式可以从三个地方指定,优先级与作用范围不同:

```sql
-- ① 查询级:只影响这一条 SELECT 的输出
SELECT * FROM t FORMAT JSONEachRow;

-- ② 语句级:指定 INSERT 的输入格式
INSERT INTO t FORMAT CSV;

-- ③ 客户端级:clickhouse-client 的 --format 参数,作为默认值
--    clickhouse-client --format TSV
```

前两种写在 SQL 里,是自描述的、可复制的;第三种是会话默认,适合交互式使用。做脚本时优先用前两种,因为把格式写进 SQL 就不用依赖客户端的配置。客户端一侧,`clickhouse-client --format` 设定默认输出格式(非交互的 `--query` 默认就是 `TSV`);有些配套工具还会用 `--input-format` / `--output-format` 把读写分开指定——但 `clickhouse-client` 官方文档里明确列出的格式选项是 `--format`(`-f`,设定默认输出格式)与 `--vertical`,**`--input-format` / `--output-format` 是否被接受我没能找到确证**(我检索到的各语言文档只写 `--format`,不排除较新版本另行加了这两个名字)。以你所用版本 `clickhouse-client --help --verbose` 的实际输出为准。

**`FORMAT` 的位置是硬的:它必须在整条语句的最末尾。** 这一点比看起来更常出错——`SETTINGS` 要排在 `FORMAT` **前面**:

```sql
SELECT * FROM t SETTINGS max_threads = 4 FORMAT JSON;   -- ✔
SELECT * FROM t FORMAT JSON SETTINGS max_threads = 4;   -- ✘ 解析失败
```

因为解析器读到 `FORMAT` 之后的第一个 token 就当格式名,后面再冒出来的东西一律算多余。`INTO OUTFILE` 同样排在 `FORMAT` 之前:

```sql
SELECT * FROM t INTO OUTFILE '/tmp/out.tsv' FORMAT TSV;
```

`INTO OUTFILE` 让**服务端**把结果写到它自己磁盘上的路径,而不是回给客户端,所以路径必须在服务器进程可写的位置(填客户端机器的路径没有意义);返回给客户端的只剩一行"写了多少行/多少字节"的统计。在副本或分布式部署里,这个文件落在**发起这次查询的那台**机器上——要收集导出结果,先确认是哪一台。

**HTTP 接口另有自己的"默认格式"机制。** 用查询参数 `default_format=JSON` 或请求头 `X-ClickHouse-Format: JSONEachRow`,可以规定"这条请求没写 `FORMAT` 时用什么";HTTP 的默认是 `TabSeparated`,与客户端一致。要注意 `X-ClickHouse-Format` 的语义在版本之间变过:**在本书基线 25.3 上,它只是一个默认值**——一旦 SQL 里显式写了 `FORMAT`,头里的设置就被覆盖;较新的版本把它改成了"强制指定响应格式、连 `FORMAT` 子句都能盖过"。依赖这个头做导出时,先在你的版本上验一下到底谁优先。

## 19.3 文本、按行:TSV 家族

`TSV`(TabSeparated)是 ClickHouse 的默认输出格式。字段之间用制表符分隔,值里的制表符、反斜杠、换行用**反斜杠转义**。`NULL` 用一个两字符哨兵 `\N` 表示。

```text
1<TAB>hello<TAB>\N
2<TAB>a\tb<TAB>world          ← 值里的制表符写成了 \t
```

为什么 `NULL` 用 `\N` 而不是空字段?因为**空字段是一个合法的值**(空串)。如果 `NULL` 写成空字段,`NULL` 和 `""` 就再也分不开了。用哨兵之后,三者的往返是明确的:`NULL` → `\N` → `None`;`""` → 空字段 → `""`;而字符串 `"\N"`(两个字符)→ `\\N`(反斜杠被转义)→ 读回来还是 `\N`。**第三行是关键**:正是反斜杠转义保证了字面量永远不会和哨兵撞车。这也是为什么反斜杠一层在 TSV 里不能省。

`TSVWithNames` 在第一行输出列名,`TSVWithNamesAndTypes` 在第二行再输出列类型。后者自带了**类型信息**,是"TSV 家族里最接近无损"的变体——拿着它,读取端不需要外部 schema 就能重建结构。

## 19.4 CSV 与"为什么 CSV 难以无损"

`CSV` 按 RFC 4180:字段用逗号分隔,值里出现逗号、引号或换行时用双引号包起来,引号本身写成两个引号。`CSVWithNames` 第一行是列名。

CSV 的问题在于:**单靠 RFC 4180 做不到无损**。在 RFC 4180 里,"空字段"和"值是一个空串"是同一个字节串(都是两个逗号之间什么都没有),而 RFC 4180 根本没有"`NULL`"这个概念。于是数据库的 `NULL`、空串、以及字符串 `"NULL"` 会全部塌成同一个空字段:

```text
没有哨兵时(纯 RFC-4180):        有了哨兵之后:
a,,b        → ?, "", ?            a,\N,b     → ?, None, ?
a,"",b      → ?, "", ?            a,"",b     → ?, "", ?
↑ 前两行的输出完全相同,信息已经丢了
```

ClickHouse 的做法是引入和 TSV 一样的 `\N` 哨兵。于是 CSV 变成"RFC 4180 引号 + `\N` 哨兵"两层规则。给别的程序吃(表格软件、pandas)时用 CSV;要在 ClickHouse 之间往返时,用 TSV 更省心,因为它的转义规则更少、更单纯。

## 19.5 Values:与 INSERT ... VALUES 同一套语法

`Values` 格式把每一行写成一个 SQL 元组:`(1,'hello',NULL),(2,'a\tb',[1,2])`。它的价值在于**和 `INSERT INTO ... VALUES (…),(…)` 用的是同一套语法**——所以"导出即导入"这件事变得字面成立:

```sql
SELECT * FROM t FORMAT Values;   -- 输出
INSERT INTO t2 FORMAT Values;    -- 同样的文本可以直接喂回去
```

它靠的是 SQL 字面量这一层转义:字符串写成 `'…'`、`NULL` 写成 `NULL`、数组写成 `[…]`、Map 写成 `{…}`。引号层的代价是"值里的引号与反斜杠要转义",所以写和读必须共用同一张转义表——多一条少一条往返测试立刻抓得住。

## 19.6 JSON 族

JSON 格式有两套排布方式,区别很重要:

- **按行(EachRow 系列)**:每一行是一个独立的 JSON 文档。`JSONEachRow` 是 `{"id":1,"name":"a"}` 一行一个对象;`JSONCompactEachRow` 是 `[1,"a"]` 一行一个数组(省掉列名,更省字节);`JSONStringsEachRow` 把**所有值都写成字符串**,接收方不必关心字段是数字还是字符串。这族适合日志/管道:一行一条,追加即得,可以边算边发。
- **整体(JSON 系列)**:整个结果集是**一个** JSON 文档,形如 `{"meta":[…],"data":[…],"rows":N}`。`meta` 里带着每一列的 `name` 与 `type`,`data` 是行的数组。`JSON` 与 `JSONCompact` 的差别**在行的形状,不在空白**:`JSON` 的每个数据行是一个**对象**(`{"num":1}`,带列名),`JSONCompact` 的每个数据行是一个**数组**(`[1]`,省掉重复的列名)——所以"`JSONCompact` 只是不换行不缩进"是**错的**,那只是顺带的排版差异。这族适合"给前端一个完整对象",代价是必须把整个结果集数完才知道 `"rows"` 是多少,不能流式。(mini-clickhouse 的 `JSON` 目前把 `data` 的行也输出成**数组**,即 `SELECT 1 AS num FORMAT JSON` 会看到 `"data": [[1]]`——那其实是 `JSONCompact` 的形状,与真实 ClickHouse 的 `JSON`(对象行)不同,属有意的简化点。)

**`output_format_json_quote_64bit_integers` 默认是 1**,这是 JSON 族最著名的"惊喜":

```json
{"id":"18446744073709551615"}
```

64 位整数默认被输出成**字符串**,因为 JavaScript 的 `Number` 只有 53 位精度,`18446744073709551615` 到了浏览器里会变成 `18446744073709552000`。后果是下游的 `typeof x === "number"` 会失败,无数人在这里浪费过一个下午。要拿到裸数字,设 `output_format_json_quote_64bit_integers = 0`;读取时两种写法都应接受(字符串按类型解析回数字)。另外 `nan`/`inf` 是"非法 JSON",ClickHouse 默认照原样写(`output_format_json_quote_denormals` 默认 0),严格的解析器会拒绝——跨系统传输前先确认对方的容忍度。

**还要分清 JSON 的两种身份。** 这一节讲的全是 JSON 作为**格式**——结果怎么写成 JSON 文本、又怎么从 JSON 文本读进来。ClickHouse 25.3 起还有一个同理不同物的 **`JSON` 数据类型**:它管的是"一列半结构化数据怎么在磁盘上存成子列、怎么按路径索引",和 `FORMAT JSON` 是两码事。同一条查询可以既把某列声明成 `JSON` 类型、又用 `FORMAT JSONEachRow` 输出,两者互不影响。类型侧的细节在第 03 章 §03.13,函数侧的取舍(原生类型 vs `JSONExtract*`)在第 17 章 §17.21。

## 19.7 二进制:RowBinary 族与 Native

二进制格式的分工与文本格式一句话说清:文本格式给**人和别的程序**看,二进制格式给**ClickHouse 自己**看。前者有转义、有引号,体积大 2–5 倍;后者只有字节,但"值 → 字节 → 值"必须逐字节一致,体积小、还能直接内存映射。

- `RowBinary` 按**行**写:`[r0c0][r0c1][r0c2][r1c0]…`,**没有任何元数据**,所以读取端**必须**知道 schema。它最省字节,但也就最不"自描述"。
- `RowBinaryWithNames` 在前面多一段"列数 + 列名";`RowBinaryWithNamesAndTypes` 再加一段列类型,于是**读取端不需要外部 schema**——列名与类型都在文件里。注意 `Nullable` 的布局是"1 字节空标志 + 没空才写负载",所以同一列不同行的字节数不一样,想按固定行宽 mmap 是做不到的。
- `Native` 是 ClickHouse 的**内部格式**:块头带列数与行数,然后**按列**写(一列写完整列再写下一列)。按列不是为了好看——整数列可以整块拷贝、压缩器能在整列上找重复、读取时可以只解压需要的列。它是**网络传输(队列)与 `INSERT` 输入端**共用的格式。这里要澄清一个常见的过度概括:**part 落盘用的不是"一个 `Native` 块",而是与 `Native` 相通的列序列化**——part 里每一列各自是一个独立的 `.bin` 数据文件,用同一套"整列连续存"的规则,但文件布局与 `Native` 的块头并不是一回事(part 另有一整套 `checksums`、`primary.idx`、`.mrk2` 等文件)。所以"`Native` 就是磁盘格式"这句话只对了一半:**序列化相同,容器不同**。

`Native` 的关键性质是:**它是内部格式,不保证跨大版本兼容**。所以拿它做长期归档、或者在不同版本的 ClickHouse 之间传数据,是有风险的;要长期存储选 `Parquet`、要跨版本交换选文本格式。这一点在"格式选型"里经常被忽略。

## 19.8 展示用格式

`Pretty`/`PrettyCompact`/`PrettySpace` 是给人看的表格:`Pretty` 每行之间有分隔线,`PrettyCompact` 没有(它是交互式客户端的默认),`PrettySpace` 用空格代替框线。`Vertical` 是一行一块的竖排,列特别多时好用。`Markdown` 输出 GitHub 风格的表格,方便贴进文档或 issue。`Null` 什么都不输出,但**查询照常执行**——它是压测时用来排除"格式化开销"的对照组。

这一族还有几个变体,名字不常出现,遇到时要知道区别。`PrettyMonoBlock` / `PrettyCompactMonoBlock` 是"**先在内存里把整个结果集攒成一个 block,再一次性画出来**"的版本:普通 `PrettyCompact` 流式输出时,每个 block 都会重复画一遍表头,`MonoBlock` 只画一次——代价是结果集要全部缓冲在内存里,大结果别用它。`PrettyNoEscapes` 不把 ANSI 转义序列转义掉,终端里的颜色还能保留;`PrettyJSONEachRow` 每行是一个 JSON 对象、外面套一层表格对齐,方便把结果贴给程序;`VerticalRaw` 是 `Vertical` 的不转义版本;`SingleValue` 只输出那一个值、连列名都省掉。

这些格式**只能输出**:一旦画上框线,列边界就依赖等宽字体,解析它比解析 HTML 还难,所以它们不能作为输入。它们的共同前提是"终端里有人在看"——把 `Pretty` 家族当成数据管道的一环,是把显示层当成了存储层。§19.11 的表把这一族里没在本节点名的名字,连同别的格式一并列齐。

## 19.9 外部生态格式

要在 ClickHouse 与别的系统之间搬数据,会用到这些格式:

- **列存交换格式**:`Parquet`、`ORC`。它们按列组织、自带压缩与字典编码,是数据湖(Spark、Hive、S3 上的湖仓)的事实标准。ClickHouse 读写它们时能享受列存的"只读需要的那几列"。
- **内存列存**:`Arrow`。它是进程间交换列数据的**通用标准**,和 pandas、DuckDB、各类查询引擎互通。这里要澄清一个流行的说法:ClickHouse 读写的 Arrow 数据**并不保证零拷贝**——它一般要通过 Arrow 库把列数据拷成/拷出 Arrow 的内存布局,"零拷贝"是这套格式的**目标**而非现状(同进程内、且内存布局恰好一致时才有机会免拷贝)。所以别指望"用 Arrow 传输"就自动省掉一次复制。
- **行存序列化**:`Avro`(带 schema,常用于 Kafka)、`Protobuf`(强类型、需 schema 文件)、`MsgPack`(紧凑的 JSON 替代)。它们按行组织,适合流式消息,不适合"只读几列"的分析查询。

选型的一条直觉:**分析场景优先列存格式(Parquet/ORC/Arrow),消息与流式场景优先行存格式(Avro/Protobuf/MsgPack)**。把一张宽表存成行存格式,再只查三列,等于放弃了列存的全部好处(§19.14)。

## 19.10 模板类格式

`Template` 与 `CustomSeparated` 是一族**可配置格式**:你自己指定行分隔、字段分隔、值的转义方式、甚至每一列的输出模板。它们解决的是"我的下游系统只认某种奇怪的文本布局"这类需求。代价是要维护一份模板/转义说明,属于"配置一个格式"而不是"用一个现成格式"。能用 `TSV`/`CSV`/`JSONEachRow` 解决就别上模板类。

举个最典型的场景:对面系统只认"每条记录一行、字段用 `|#|` 分隔、行尾要 `\r\n`、整份结果还得加一个固定前缀"这种古怪布局,任何现成格式都对不上。这时用 `Template` 把布局"画"出来即可——两条设置各描述一层:`format_template_resultset_format` 描述整个结果集怎么套(用 `${data}` 占位放所有行,它必须是模板里的第一个占位符),`format_template_row_format` 描述一行的排布(`${列名:转义规则}` 按列取值):

```sql
SELECT id, name FROM t
FORMAT Template
SETTINGS format_template_resultset_format = 'BATCH|${data}\n',
         format_template_row_format = '${id:Raw}|#|${name:Raw}\r\n';
```

行模板里的转义规则可取 `CSV`/`JSON`/`XML`/`Escaped`(类似 TSV)/`Quoted`(类似 Values)/`Raw`/`None`,不写就是 `None`;把 `${列名:...}` 留成空 `${}` 表示跳过该列。这两条 `..._format` 是 24.2 起才有的**内联**写法(旧写法是把模板写进文件、用 `format_template_resultset` / `format_template_row` 指过去)。`CustomSeparated` 走同一思路但更结构化:分隔符拆成几个独立设置——`format_custom_field_delimiter`(字段间隔)、`format_custom_row_after_delimiter`(行尾)、`format_custom_result_before_delimiter` / `..._after_delimiter`(结果集前后缀)、`format_custom_escaping_rule`(转义规则),不必手写整条模板。两者能做的事高度重合:`Template` 更自由(能塞固定前缀、做非对称排版),`CustomSeparated` 更省心(转义交给设置项管)。但**它们都不自描述**——对面拿到的字节流并不知道哪一列是什么类型,所以要么配套一份 schema,要么用带表头的变体;模板与转义表一旦写错,往返立刻失败,维护成本全压在你这边。

## 19.11 格式全表:把正文没讲到的补上

前面按"家族"讲了 TSV/CSV/JSON/二进制/展示这几大块,但 ClickHouse 实际注册的格式远不止这些,其中不少在集成和运维里真会用到。下表把本章正文没逐个展开的名字补齐。**输入 / 输出**两列说明这个格式能不能当 `INSERT` 的源、能不能当 `SELECT` 的出口;**自描述**指数据里带没带列名与类型;**可流式**说明能不能"来一块处理一块",而不必先把整个结果集收齐(收不齐的那些,正是 §19.8 说的"框线靠等宽字体"这类)。

| 格式 | 输入 | 输出 | 自描述 | 可流式 |
|---|---|---|---|---|
| `TSKV` | ✔ | ✔ | 否 | 是 |
| `TabSeparatedRaw`(`TSVRaw`) | ✔ | ✔ | 否 | 是 |
| `TabSeparatedRawWithNames` / `…AndTypes` | ✔ | ✔ | 名字 / 名字+类型 | 是 |
| `CSVWithNamesAndTypes` | ✔ | ✔ | 名字+类型 | 是 |
| `JSONColumns` / `JSONCompactColumns` | ✔ | ✔ | 否 | 否 |
| `JSONColumnsWithMetadata` | ✔ | ✔ | 是 | 否 |
| `JSONEachRowWithProgress` | ✘ | ✔ | 否 | 是 |
| `JSONAsString` / `JSONAsObject` | ✔ | ✘ | 否 | 是 |
| `JSONObjectEachRow` | ✔ | ✔ | 否 | 是 |
| `BSONEachRow` | ✔ | ✔ | 部分 | 是 |
| `Regexp` | ✔ | ✘ | 否 | 是 |
| `LineAsString` | ✔ | ✔ | 否 | 是 |
| `RawBLOB` | ✔ | ✔ | 否 | 否 |
| `SQLInsert` | ✘ | ✔ | 否 | 是 |
| `XML` | ✘ | ✔ | 否 | 否 |
| `CapnProto` / `ProtobufSingle` / `ProtobufList` | ✔ | ✔ | 需外部 schema | 是 |
| `ArrowStream` | ✔ | ✔ | 是 | 是 |
| `ParquetMetadata` | ✔ | ✘ | 是 | 是 |
| `ODBC` / `MySQLWire` / `PostgreSQLWire` | ✘ | ✔ | 否 | 是 |
| `SingleValue` | ✘ | ✔ | 否 | 否 |
| `PrettyMonoBlock` / `PrettyNoEscapes` / `PrettyJSONEachRow` | ✘ | ✔ | 否 | 否 |
| `VerticalRaw` | ✘ | ✔ | 否 | 是 |
| `Null` | ✘ | ✔ | 否 | 是 |

按用途记几个最容易被漏掉、或最容易被误解的:

- **`TSKV`** 是"键=值"的文本格式(`key=value`),适合日志管道,和 TSV 一样要外部 schema 才知道类型;**`TabSeparatedRaw`(`TSVRaw`)** 不转义,值里真出现制表符或换行就会破坏分列,只在你能保证值干净时用。
- **`JSONColumns` / `JSONCompactColumns` 与 `JSONCompact` 不是一回事**:`JSONCompact` 是"一行一个数组",`JSONColumns` 是"一列一个数组"(整个结果是 `{"col1":[…],"col2":[…]}`),两者都必须把整份结果缓冲起来;`JSONColumnsWithMetadata` 还会带上 `meta`。
- **`JSONAsString` / `JSONAsObject`** 把进来的整行原样塞进目标列(字符串 / JSON 值),适合"先落地、之后再用 `JSONExtract*` 解析"的管道;**`JSONObjectEachRow`** 则是"一个顶层对象、每个键是一行"(配套设置 `format_json_object_each_row_column_for_object_name`)。
- **`LineAsString`** 把每行读成一个 `String`;**`RawBLOB`** 把整份输入读成**一个**值(连分隔符都不认);**`Regexp`** 用正则把每行拆成列,**只能输入**,但离了设置跑不起来——它单独放到下面讲。
- **`JSONEachRowWithProgress`** 在正常行之间穿插进度事件,**只能输出**——给流式消费方看进度,不是给 `INSERT` 用的。
- **`SQLInsert`** 把结果输出成一批 `INSERT INTO … VALUES …` 语句、可直接回灌;**`ParquetMetadata`** 只读 Parquet 文件的元数据当成一张表看,排查"这个 Parquet 长什么样"时很趁手。
- **`ODBC` / `MySQLWire` / `PostgreSQLWire`** 是按某种线路协议把结果吐出去(给对应驱动直连);**`ArrowStream`、`BSONEachRow`、`MsgPack`、`Avro`** 是自描述或半自描述的行/列式二进制,适合跨系统传;**`CapnProto` / `Protobuf*`** 需要外部 `.proto` / schema 文件,靠 `format_schema` 指定(见 §19.12)。

**`Regexp`:一个"表里有名字、却离了设置跑不起来"的格式。** 上面的表里只给了它一个名字,正文也没交代怎么用——照着写会直接失败。这一点正好和 §19.10 的模板类格式形成对照:后者明明白白列出了自己的 `format_template_*`,而 `Regexp` 需要的三个设置此前一个都没出现。补齐如下(三条都取自上游 `FormatFactorySettings.h`,默认值为 25.3):

- **`format_regexp`**——正则本体,**RE2 语法**,**默认 `""`(空)**。空正则在打开这个格式时立刻抛 `BAD_ARGUMENTS`("The regular expression is not set"),所以它**不是可选项**:不配就没法用。
- **`format_regexp_escaping_rule`**——每个捕获子组按哪种**文本转义规则**解析成列值,**默认 `Raw`**。`Raw` 的行为是"原样取子串、**不做任何反转义**"(同 `TSVRaw`),所以值里若带 `\t`、`\\` 这类转义序列,要用 `Escaped`(同 `TSV`,会把转义还原)才不会当成两个普通字符。官方 `Regexp` 文档列出的可选值是 `Escaped` / `Quoted`(同 `Values`)/ `CSV` / `JSON` / `Raw`。
- **`format_regexp_skip_unmatched`**——某行**不匹配**正则时是跳过(1)还是报错(0),**默认 `false`(报错)**,即默认遇到不匹配的行就抛 `INCORRECT_DATA`。

**匹配是"按位置",不是"按名字"。** 它以**行为单位**匹配(行尾 `\n` 或 `\r\n`),正则里的**每个捕获子组按出现顺序对应一列**,且子组**个数必须等于目标列数**——个数对不上会抛"the number of matched fields in line doesn't match the number of columns"。捕获子组**有没有名字都不影响落列**:落哪一列只由它在正则里排第几决定,想调换列顺序只能改正则,不能像 `WithNames` 那样靠列名对齐。一个能跑的例子(表 `t (id UInt32, arr Array(UInt32), name String)`,每行输入形如 `id: 1 arr: [1,2,3] name: a`):

```sql
INSERT INTO t SETTINGS
    format_regexp = 'id: (.+?) arr: (.+?) name: (.+?)',
    format_regexp_escaping_rule = 'Escaped',
    format_regexp_skip_unmatched = 0
FORMAT Regexp;
-- 数据从标准输入喂进来,例如:
-- cat data.tsv | clickhouse-client --query "INSERT INTO t SETTINGS ... FORMAT Regexp"
```

三点提醒:一是**子组个数与列数要严格对齐**,多一个少一个都直接失败;二是 `format_regexp_skip_unmatched` 默认是 `0`(报错),面对脏数据要么显式设成 `1`,要么先把正则写严谨,别指望它默默跳过坏行;三是这里的顺序完全由子组顺序决定——和 §19.10 一样,`Regexp` 属于"**配置一个格式**"而不是"用一个现成格式",只不过模板类格式把转义规则逐列写在模板里,而它把转义规则收敛成 `format_regexp_escaping_rule` 一条。

只存在于某几个版本、或需要额外依赖的名字(各种 `Hadoop*`、`Npy` 等)不逐一列;要用之前先在 `SELECT * FROM system.formats` 里确认你的版本注册了它。(表中 `SingleValue` 是确定存在的;**曾见于别处的格式名 `One` 我核不到**,已略去——这类"单值"格式名请以 `system.formats` 为准。)

## 19.12 输入侧的设置

导入时那些"数据脏"的现实问题,靠一组设置处理。逐个说清它们解决什么:

- `input_format_skip_unknown_fields`:输入里出现了目标表没有的列,是跳过(1)还是报错(0)。默认是 **`1`** —— 也就是说**默认就会静默跳过**多余的列(`22.6` 从 `0` 改成 `1`,依据是上游 `SettingsChangesHistory.cpp` 里那条改动,以及 `FormatFactorySettings.h` 里 `DECLARE(Bool, input_format_skip_unknown_fields, true, ...)`)。这条默认值的方向很容易记反:**它意味着"多给字段"不会报错**,于是列名拼错、上游 schema 漂移都不会被拦住 —— 想要严格模式,得显式 `SET input_format_skip_unknown_fields = 0`。第 08 章 §08.5 的批量导入清单里也提了这一条。
- `input_format_allow_errors_num` / `input_format_allow_errors_ratio`:最多允许多少行(或多大比例)解析失败。适合"几百万行里坏几行"的批量导入,别指望它处理系统性错误。
- `input_format_defaults_for_omitted_fields`:输入里缺省的列是否用列的默认值填充(默认 1)。JSON 里少一个可空字段时,靠它决定那列是 `NULL` 还是报错。
- `input_format_null_as_default`:把输入里的 `NULL` 当成该列的默认值(默认 1)。给"用 `NULL` 表示缺失、但列本身不可空"的数据兜底。
- `input_format_tsv_empty_as_default`:TSV 里的空字段当成列的默认值。CSV 侧的对应设置是 `input_format_csv_empty_as_default`。它们处理的是"空字段到底该解释成空串还是默认值"这个歧义。
- `date_time_input_format`:解析输入里日期时间字符串的宽松程度。`basic` 只认严格格式;当输入里出现了 ISO 8601 带 `T`、带时区偏移、或各种非标准分隔符时,需要 `best_effort`(还有一个更宽松的 `best_effort_us`)。这是"为什么我的时间列导入全变成 1970"最常见的原因。

**JSON 族另有一组 `input_format_json_*` 开关,它们直接决定"JSON 里的一个值会被读成什么类型"——§19.1 列的那几条往返陷阱,十有八九是在这里翻车的。** 最要紧的三个:

- `input_format_json_read_numbers_as_strings`(**默认 1**):JSON 里的数字**默认被当作字符串读进来**。好处正是 §19.1 的"精度"那条——`{"ts": 18446744073709551615}` 这种超出双精度的大整数不会被中转成浮点;代价是目标列若是数值类型,这一次读入会走一遍**字符串→数值**的宽松转换,而带小数、科学计数法或非数字尾巴的值在这条转换里可能被截断或报错。要"数字就按数字读",把它设成 `0`。
- `input_format_json_read_bools_as_numbers`(**默认 1**):JSON 的 `true`/`false` 默认允许读进**数值列**(`true` → `1`、`false` → `0`)。这会让"把布尔塞进 `UInt8`"悄悄成立,因此更适合当兜底而不是设计。
- `input_format_json_read_bools_as_strings`(**默认 1**):允许把 JSON 的 `true`/`false` 读进**字符串列**——**解析输入**时把布尔当作 `"true"`/`"false"` 两个字符串处理。它和上一条是一对,只是规定了布尔值的另一种落点。

同一族里还有 `input_format_json_read_objects_as_strings`(把嵌套对象整个当字符串塞进 `String` 列)与 `input_format_json_try_infer_numbers_from_strings`(schema 推断时尝试从字符串字段里认出数字,§19.17)等;这组设置的**名字与默认值随版本有增删**,上面给的默认值请以你那一版 `system.settings` 为准。

自描述格式(Parquet/ORC/Arrow/Avro/Protobuf 这些)另有一组以格式名开头的设置来调行为:`input_format_parquet_allow_missing_columns`(默认 1)决定"文件里缺了目标表某列"时是否用该列默认值补齐,而不是报错;`input_format_parquet_case_insensitive_column_matching` 控制列名匹配是否忽略大小写(默认 **0**,即默认**区分**大小写);模式演进基本都靠这两个开关。Protobuf/CapnProto 这类需要外部类型的格式,用 `format_schema 'file:MessageName'` 指定 schema 文件与消息名(schema 文件放在 `format_schema_path` 指向的目录下);Avro Confluent 格式则用 `format_avro_schema_registry_url` 连 Schema Registry。**Avro 的 union 坑**要单独提一句:Avro 允许一个字段是多种类型的联合(union),写入时用一个分支下标标明当前是哪种;换 schema、增删 union 分支会让同一批数据里出现不同的下标,读取端必须按它当时那份 schema 解释——所以"A 端用 v1 schema 写、B 端用 v2 schema 读"很容易读出串位或类型错误,跨系统传 Avro 前务必对齐 schema 版本。

**还有一组专管"导入有多快"的设置,它们和上面那些管"读成什么"的不是一回事。** `input_format_parallel_parsing`(默认开启)允许把**可按行切分**的输入(TSV / CSV / JSONEachRow 这类文本格式)交给多个线程**并行解析**——灌大文件时,它省下的是 CPU 把字节变成行的那段时间,受 `max_threads` 约束;格式不可按行切分时(比如整个文件是一个 JSON 对象)它不会生效。从 `url()` / S3 这类**远端源**读进来时,下载侧另有一组旋钮(`max_download_threads`、`max_download_buffer_size` 这一族)决定并行度与缓冲大小。分清这一点很有用:**"导入慢"可能是下载慢、读盘慢,也可能是解析慢,三者调的地方完全不同**——先看是哪一段,再动手,别把解析的开关用在网络瓶颈上。(这几个设置的默认值随版本调整过,以你那一版 `system.settings` 为准。)

## 19.13 输出侧的设置

输入侧的那组开关管"怎么读进来",输出侧另有一组管"怎么写出去"。最常踩的几个:

- `output_format_json_array_of_rows`(默认 0):置 1 后,`JSONEachRow` 不再一行一个对象,而是把所有行包进**一个** JSON 数组 `[{…},{…}]`——下游如果是"要一个数组、不要一堆行"的解析器,就不用自己拼了。
- `output_format_json_quote_64bit_integers`(默认 1):64 位整数默认输出成字符串(§19.6 讲过原因),设 0 才是裸数字。
- `output_format_json_validate_utf8`:是否校验输出里的 UTF-8 合法性;`JSON`/`JSONCompact`/`JSONColumnsWithMetadata` 这几个格式**无论如何都校验**。含非法字节的 `String` 输出到 JSON 前先想清楚是想要原样字节还是想要报错。
- `output_format_json_quote_denormals`(默认 0):`nan`/`inf` 是不是加引号,让它们变成合法的 JSON 字符串。
- `output_format_csv_crlf_end_of_line`(默认 0):CSV 用 `\r\n` 而不是 `\n` 作行尾,喂给某些 Windows 侧程序时要用。
- `output_format_parquet_compression_method`:Parquet 输出用哪种列压缩,取值为 `snappy`、`lz4`、`brotli`、`zstd`、`gzip`、`none` 之一。**默认值在版本间改过**(早期是 `snappy`,自 **23.3 起改为 `lz4`**),跨工具读这份 Parquet 前先显式指定,别赌默认——这是**Parquet 文件内部的压缩**,和 §19.15 讲的列级 `CODEC` 不是一回事。
- `output_format_parquet_row_group_size`(默认 1000000):每个 row group 多少行,影响下游读 Parquet 的并行粒度。

日期时间的输出格式同样可调:各文本格式里时间列默认按 `YYYY-MM-DD hh:mm:ss` 写出,但可以用 `date_time_output_format` 一类的设置换成别的写法(比如 `iso`、`unix_timestamp`)——需要"给下游一个 ISO 串"或"给下游一个 epoch 整数"时不必靠字符串拼接。

**导出侧的对应开关是 `output_format_parallel_formatting`**(默认开启):把结果集分给多个线程**并行序列化**,把行再变回文本;导出大结果集到文件时它省的就是这段时间,同样受 `max_threads` 约束,也同样只对**可按行切分**的文本格式有效(二进制格式走的是另一条路径)。把这两个开关放在一起记就清楚了:**它们调的是"CPU 侧字节与行互相转换"的速度;而 §19.15 讲的列级 `CODEC` 调的是"IO 侧压什么"——两边都叫"格式化",但一层在 CPU、一层在磁盘,别混。**

## 19.14 列存格式的物理含义

为什么列式格式(Parquet、ORC、Native)对"只读几列"的查询是数量级的差别?用"从 S3 读一张 100 列的表里的 3 列"这个场景说明。

在**行存**格式里,一行 100 列是连续存的。要读其中 3 列,你必须把每一行的 100 个字段**全部读进来、再丢掉 97 个**——网络传输量和读取量都是全量的,CPU 还要为丢弃而工作。在**列存**格式里,每列是独立的一块。只读 3 列意味着:只从 S3 拉取这 3 列的数据块,剩下 97 列的字节**根本不下载**。于是 I/O 从"100 列"降到"3 列",网络费与延迟都成比例下降。这就是为什么 ClickHouse 在 S3 上跑分析查询时,列存格式(以及 ClickHouse 自己的 part 格式)是默认选择。

**机制上为什么能做到"只拉 3 列"。** Parquet/ORC 把数据先按 **row group**(一批行)分块,块内再**按列**存成一段连续的 column chunk,而每个 chunk 的字节区间与统计信息记在**文件尾部**的元数据里。读取端因此能做两件事:用**字节区间请求**只取需要的那几个 chunk 的字节;用 chunk 的 `min`/`max` 统计**整块跳过**不匹配的 row group。被跳过的列不但不下载,连解压都不做——省掉的是网络、CPU 与内存三笔。ClickHouse 自己的 part 格式用的是同一套思路(§05.9 的列裁剪),所以"直读 S3 上的 Parquet"和"查本地 MergeTree"共享同一个优化。

**把"数量级"落到具体数字上,有两条路可走。**

- **算一遍。** 一张 100 列、1 亿行的表,若每列平均 8 字节,裸数据约 80 GB;只读 3 列就是约 2.4 GB,即 3%。在按字节计费、按字节限流的对象存储上,这个比例直接换算成网络费与下载延迟;换成行存,这个比例就是 100%。
- **量一遍。** 同一条显式点名 3 列的 `SELECT`,分别打在同一个数据集的 Parquet 与 CSV 上,对比 `system.query_log` 里的 `read_bytes`(以及 `ProfileEvents` 中与 S3 取数相关的计数)就能看出差距。前提有两条:两次查询的**列集合完全一致**,且都**显式点名**——`SELECT *` 会把全部列拉进来,列存的优势立刻消失(§05.9 讲的"验证列裁剪时别用 `SELECT *`"在这里同样成立)。

## 19.15 压缩与格式的关系

这里有一个反复被讲错的地方,值得单独澄清:**`CODEC(...)` 作用在 MergeTree(以及 `File` 这类按 part 落盘的引擎)的列数据存储上,它不作用于 `FORMAT` 的输出。** 抓住这条,格式和压缩的关系就顺了:

- `SELECT ... FORMAT Parquet` 输出的是一份完整的 Parquet 文件,列上写没写 `CODEC` 对它**没有任何影响**;这份文件的内部压缩由 `output_format_parquet_compression_method` 决定(§19.13)。
- `INSERT ... FORMAT Parquet` 进来的数据,先被解析回列值,再由列的 `CODEC` 重新编码落盘。**先解 Parquet 自己的压缩,再按列 `CODEC` 压一次**,这两次发生在不同层,不是"双重压缩"。

那"格式自带压缩"到底指谁?要分清:

- **`Native` 不自带压缩。** 它按列写的是**未压缩的列字节**(§19.7)。所以 `SELECT ... INTO OUTFILE 'x.native' FORMAT Native` 落下来的文件是裸字节,不经过任何 codec——想让它变小,得在**外层**压(HTTP 响应可以用 gzip,LZ4/ZSTD 是存储层的事)。既然 Native 里根本没有那一层,"给 Native 列加 `CODEC` 是双重压缩"就是错的;给 MergeTree 列写 `CODEC(...)` 恰恰是**正常且预期**的路径(第 20 章的主题)。
- **`Parquet`/`ORC`/`Avro` 才自带编解码。** 它们的文件内部就有列压缩、块压缩或块校验。要调它们的压缩,用对应的 `output_format_*` 设置,而不是列的 `CODEC`——因为你导出的是文件,不是 part。

**唯一真该避免的"双重压缩"是一个与格式无关的场景**:你把**已经压过的字节原样存进一个列**——一个 `String` 列里放着 gzip 包、JPEG、加密 blob——再给这列套 `ZSTD`。压缩器对着高熵字节再压一遍几乎压不动,却白烧 CPU。判断依据是"**这一列的字节本身是不是已经压过了**",而不是"它来自哪种格式"。

## 19.16 格式与 schema

格式要正确解析数据,前提是知道"每一列是什么类型"。schema 有三个来源:

- **表结构**:`INSERT INTO t FORMAT …` 时,`t` 的列定义就是 schema。
- **显式声明**:用 `input()` 表函数时,可以在括号里写明列名与类型。
- **格式自带的表头**:`TSVWithNamesAndTypes`、`RowBinaryWithNamesAndTypes`、`Native`、以及 `JSON` 的 `meta` 都自带名字与类型。
- **运行时推断**:用 `file()`/`s3()`/`url()` 这类表函数直接读外部文件、又没显式给结构时,ClickHouse 会**采样数据自己猜**列名与类型。这是第四条来源,也是这四条里最容易出错的一条,§19.17 单独讲。

**列顺序的规则**:不带表头的格式(纯 `TSV`、`CSV`、`RowBinary`)按**位置**匹配——第 1 个字段进表的第 1 列,以此类推;带表头/带名字的格式按**名字**匹配(允许顺序不同、允许缺列)。所以"我调换了 CSV 里两列的顺序,结果数据串位了"是位置匹配的必然结果——要按名字匹配就用带表头的变体。

## 19.17 Schema 推断:让 file()/s3() 自己猜类型

读外部文件时,若目标类型没定,就得**推断**。最常用的一条命令是 `DESCRIBE`,它能让你先看清楚"推断成了什么":

```sql
DESCRIBE TABLE file('events.csv', CSV);
DESCRIBE TABLE s3('https://bucket/events/2025/*.parquet', 'Parquet');
```

`DESCRIBE` 对 `file()`/`s3()`/`url()` 都能用,打印出的列名与类型就是推断结果。做 ETL 时先 `DESCRIBE` 一遍再决定要不要 `CAST`,比"导进去发现类型不对再返工"省事得多。

推断由一组 `schema_inference_*` 设置控制:

- **`schema_inference_make_columns_nullable`** 决定推断出的列可不可空。**它的默认值在版本间变过,是个真坑**:25.3 上默认是 `1`——推断出的列**一律可空**(`Nullable`);25.10 起默认改成 `3`——**遵循 Parquet/ORC/Arrow 文件里的可空元数据**,文本格式(CSV/TSV)仍一律可空。取值还有 `0`(永不可空,配合 `input_format_null_as_default` 处理 `NULL`)和 `2`/`auto`(只有样本里真出现 `NULL` 才可空)。选 `1` 还是 `3` 会直接影响下游:一列全被推成 `Nullable` 会多一点存储、还会削弱压缩(§20.8 会再提)。**因为默认值在 25.3 与 25.10 之间不一样,务必用 `SELECT * FROM system.settings WHERE name = 'schema_inference_make_columns_nullable'` 看你自己的版本。**
- **`input_format_*_use_best_effort_in_schema_inference`**:按格式各有一条,如 `input_format_csv_use_best_effort_in_schema_inference`、`input_format_json_*`、`input_format_tsv_*`。文本格式没有类型信息,推断只能"猜",猜不准时要么改用更严的策略、要么退回"整列当字符串"——这些开关就是调这个拿捏。
- **缓存**:`schema_inference_use_cache_for_file` / `_for_s3` / `_for_url`,以及 `schema_inference_cache_max_element_size`。推断要采样、要读元数据,对"S3 上几千个文件"这种场景开销不小;开着缓存能省掉重复推断,但代价是**源文件 schema 变了而缓存没失效**时你拿到旧的列定义。
- **提示**:`schema_inference_hints` 可以手工钉死"某几列是什么类型",其余仍交给推断——这是"大部分靠猜、个别关键列必须准"的折中。
- **Parquet 的列缺失**:`input_format_parquet_allow_missing_columns`(默认 1)让"文件缺了目标表某列"时用默认值补,而不是报错。

推断的常见后果,要提前知道:

- **`INSERT INTO ... SELECT * FROM file(...)` 是怎么定类型的?** 它是"两边协商":`SELECT *` 先从文件推断出一组列(按上面那些设置),这份推断结果被当成这次的输入 schema;`INSERT` 端再按**位置**把这份 schema 对到目标表的列上(除非是 `WithNames` 类格式,才按名字对,见 §19.16)。所以"文件推成了 `String`、目标列是 `UInt64`"并不会报错——ClickHouse 会尝试转换,但转换宽松,脏数据可能被静默塞进去。稳妥做法是显式给结构:`file('x.csv', CSV, 'id UInt64, name String')`。
- **文本格式的推断特别脆**。CSV 里前三行是数字、第四行是 `abc`,推断会把整列当字符串;而只采前几行、后面突变的情况,推断给的类型就"看起来对、其实错"。拿 `DESCRIBE` 看过再决定,是唯一稳的办法。
- **推断和 `Nullable` 是连着的**(见上),推断出的可空性会一路带进目标表,影响存储与优化。

## 19.18 与 mini-clickhouse 的对照

mini-clickhouse 注册了 **24 个格式**,数据源是 `registered_formats()`。其中**可读可写**的有:`TSV`/`TSVWithNames`/`TSVWithNamesAndTypes`、`CSV`/`CSVWithNames`、`Values`、`JSONEachRow`/`JSONStringsEachRow`/`JSONCompactEachRow`/`JSONCompactEachRowWithNames`、`JSON`/`JSONCompact`、`RowBinary`/`RowBinaryWithNames`/`RowBinaryWithNamesAndTypes`、`Native`。**只能输出(不能读回)**的有:`Pretty`/`PrettyCompact`/`PrettyCompactMonoBlock`/`PrettySpace`、`Vertical`、`Markdown`、`SQLInsert`、`Null`——这正是 §19.8 说的"画了框线就不可解析"。

**格式层与序列化层的分工**,是这个实现里必须讲清的一点。`minich/serialization.py` 回答的是"**一列变成了哪些字节**"(磁盘/内存布局);`minich/formats/` 回答的是"**一个 Block 与外部文本或字节怎么互转**"。二进制格式(`RowBinary`、`Native`)复用序列化层;文本格式走的是另一套 `render_value_text`/`parse_rendered_value`,与序列化层**互相独立**。这个分工本身与真实 ClickHouse 一致,但有一处落差值得记住:**内存布局 ≠ 磁盘布局**。比如 `String`,内存里(`ColumnString`)用 "offsets + chars" 以便随机访问,磁盘上却用 `[varint 长度][字节]` 以便流式写与逐值跳过。理解这个落差是理解 ClickHouse 存储格式的关键。

还有几处**简化点**,读代码时不要当成 bug:

- `CSV` 保留了与 `TSV` 相同的**反斜杠转义**(真实 ClickHouse 的 CSV 只做 RFC-4180 引号)。这让它可往返,但读"别人用真实 CH 导出的、值里含反斜杠的 CSV"时会被当成转义序列。
- `Native` 是**这个实现自己的一套**:没有写 BlockInfo 的 5 个字节、没有 revision/版本号、没有 custom-serialization 标志字节,还多写了一个每列行数的 varint。所以**它生成的 Native 文件真实 ClickHouse 读不了,反过来也一样**。
- 输入里**没有**注册 `input_format_tsv_empty_as_default`(TSV 侧),只注册了 `input_format_csv_empty_as_default`;`date_time_input_format`、`output_format_json_quote_64bit_integers` 这些都有,且默认值与真实 ClickHouse 对齐。

一句话总结对照方法:**`registered_formats()` 列出来的就是实现了的**;表里没出现的格式(如 `Parquet`、`ORC`、`Avro`、`Arrow`、`Protobuf`、`MsgPack`、`Template`、`CustomSeparated`)在实现里**不存在**,不要假设它们能用。

## 19.19 小结

- 格式是数据的唯一进出通道;"无损"要用四条判据逐条查:类型信息、`NULL` 与空串、转义、精度。
- `FORMAT` 必须在语句末尾,`SETTINGS` 排在它前面;`INTO OUTFILE` 也是服务端落盘;客户端的默认格式与 HTTP 的 `default_format` / `X-ClickHouse-Format` 各管一摊。
- `TSV` 是默认且可往返的;`TSVWithNamesAndTypes` 自带类型信息;CSV 靠 `\N` 哨兵才能区分 `NULL` 与空串。
- `Values` 与 `INSERT ... VALUES` 同一套语法,"导出即导入"。
- JSON 族:EachRow 可流式、JSON 是整体;`output_format_json_quote_64bit_integers` 默认为 1,64 位整数会变成字符串。注意这里是 JSON 作为**格式**;25.3 的 `JSON` **数据类型**是另一回事(第 17 章 §17.21)。
- `RowBinary` 最省字节但需外部 schema,`RowBinaryWithNamesAndTypes` 自描述,`Native` 是内部格式、不保证跨大版本兼容。
- 正文没展开的格式有一整张清单(§19.11):`TSKV`、`TabSeparatedRaw`、`JSONAsString`/`JSONAsObject`、`JSONObjectEachRow`、`BSONEachRow`、`Regexp`、`LineAsString`、`RawBLOB`、`SQLInsert`、`ParquetMetadata`、`ArrowStream` 等;展示用格式只能输出;模板类格式是"配置格式"而不是"用格式",`Regexp` 同理——它必须先配 `format_regexp`(默认空,不配就报错)外加 `format_regexp_escaping_rule`(默认 `Raw`)与 `format_regexp_skip_unmatched`(默认报错)三条才能用(§19.11),且捕获子组按**位置**对应列、个数必须等于列数。
- 输入侧与输出侧各有自己的设置组;JSON 族输入侧还有一组决定"一个值被读成什么类型"的 `input_format_json_*`(`read_numbers_as_strings`、`read_bools_as_numbers` 等,§19.12),§19.1 那几条往返陷阱多半出在这里;`output_format_parquet_compression_method` 管 Parquet 文件内部压缩,和列级 `CODEC` 是两层事。
- 列存格式的价值在"只读几列"时是数量级的:行存要读全行再丢,列存可以只拉那几列的字节。
- **`CODEC` 作用于 MergeTree 的列存储,不作用于 `FORMAT` 输出**;`Native` 本身不压缩,给它的列写 `CODEC` 是正常路径;真正该避免的是给"已装着压缩数据的列"再叠通用压缩(`Parquet`/`ORC`/`Avro` 才自带编解码)——详见第 20 章。
- 用 `file()`/`s3()` 读外部文件时,类型靠**推断**(§19.17);`DESCRIBE` 先看清结果,`schema_inference_make_columns_nullable` 的默认值在 25.3 与 25.10 之间变过。
- mini-clickhouse 实现了 24 个格式,其中展示类只输出;`Native` 与真实 ClickHouse 不互通;`registered_formats()` 没有的就是没实现。
