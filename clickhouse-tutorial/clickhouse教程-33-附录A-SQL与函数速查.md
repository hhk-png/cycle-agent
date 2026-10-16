# 附录 A:SQL 与函数速查

这一份速查是为"写到一半忘了某个语句怎么写、某个函数叫什么"这种时刻准备的:它不解释原理,只回答"名字是什么、默认值是多少、最小可用写法长什么样"。原理在对应的正文章节里;这里只保证你能在三十秒内找回语法,并且知道下一步该去第几章看细节。所有内容以 **ClickHouse 25.3** 为准;凡是拿不准的默认值,一律标 **未确认** 并说明去哪自查——自查的前两个入口是 **`system.settings`**(会话级)与 **`system.merge_tree_settings`**(表级),第三个是 **`system.settings_changes`**。这张表记录"某个设置的默认值何时变过",列为 `type`(`Session` / `Core` / `MergeTree`)、`version`、`changes`(`Array(Tuple(name, previous_value, new_value, reason, compatibility_mode))`,其中 `compatibility_mode` 取 `Apply`(允许回滚)或 `Ignore`(禁止回滚))。它是把默认值钉到版本的唯一一手手段,官方给的查法形如:

```sql
SELECT version, change.1 AS name, change.2 AS previous_value,
       change.3 AS new_value, change.4 AS reason
FROM system.settings_changes
ARRAY JOIN changes AS change
WHERE change.1 = 'use_query_condition_cache'
ORDER BY version DESC;
```

两个局限要一起记住:**(a)** 它只覆盖 `compatibility` 机制引入**之后**的改动——更老的设置若默认值从未变过,表里查不到;**(b)** 它**不覆盖服务端配置**(`config.xml` / `config.d/*.xml` 那一层),服务端配置只能查文档与配置模板。所以"拿不准就标未确认"这条纪律,在会话级设置上尤其有 `system.settings_changes` 兜底。

## A.1 怎么用这份速查

先按"你要解决的问题"定位到小节,再在表格里按关键词找行。

- 忘了某条 DDL/DML 怎么写 → A.2;
- 忘了函数名或参数顺序 → A.4 至 A.13(标量函数)与 A.14/A.15(聚合与组合器);
- 忘了某个 `SETTINGS` 的默认值 → A.16;
- 忘了类型的字节宽或取值范围 → A.17;
- 忘了 `FORMAT` 名字 → A.18;
- 忘了 `ENGINE` 名字与适用场景 → A.19;
- 忘了某个表函数的参数顺序 → A.20;
- 忘了窗口函数的写法、帧语义或函数族 → A.21;
- 想知道"配套的 mini-clickhouse 里有没有这个" → A.22。

**两条贯穿全篇的约定:** 其一,函数名**大小写不敏感**(`LENGTH`、`length` 是同一个),下表一律小写;其二,示例里的类型转换(`toUInt32` 等)只是为了把结果类型固定下来,真实查询里通常不需要。

## A.2 SQL 语句速查

### A.2.1 SELECT 的子句

子句在语句里出现的**顺序是有约束的**(大体是 `WITH → SELECT → FROM → JOIN → PREWHERE → WHERE → GROUP BY → HAVING → WINDOW → QUALIFY → ORDER BY → LIMIT`),写反了会报语法错。每条给一句"它干什么"。

| 子句 | 作用 | 注意 |
|---|---|---|
| `WITH` | 定义 CTE 或标量别名 | 可放在 `SELECT` 前或 `FROM` 前;两个位置语义略不同 |
| `SELECT [DISTINCT]` | 投影列 | `DISTINCT` 对整个 `SELECT` 行去重 |
| `DISTINCT ON (cols)` | 按指定列去重 | 保留每组第一行,顺序不确定 |
| `FROM` | 数据源(表、子查询、表函数) | 子查询需带别名 |
| `SAMPLE k` | 采样读 | 仅对 MergeTree 且声明了 `SAMPLE BY` 时 |
| `ARRAY JOIN` | 把数组列炸成多行 | 与 `LEFT ARRAY JOIN` 的区别是空数组要不要保留行 |
| `JOIN` | 连接 | 引擎与算法选择见第 15 章 |
| `PREWHERE` | 前置过滤 | 默认自动从 `WHERE` 提升,一般不用手写 |
| `WHERE` | 过滤 | 在第 11 章的索引裁剪里最关键 |
| `GROUP BY` | 分组 | 支持 `WITH ROLLUP` / `CUBE` / `TOTALS`、`GROUPING SETS`、`GROUP BY ALL`,详见 A.2.6 |
| `HAVING` | 对分组结果过滤 | 这里才能用聚合结果做条件 |
| `WINDOW` | 具名窗口定义 | `OVER w` 引用 |
| `QUALIFY` | 对窗口函数结果过滤 | 相当于窗口版的 `HAVING` |
| `ORDER BY` | 排序 | 支持 `WITH FILL [FROM/TO/STEP]` |
| `LIMIT n [OFFSET m]` | 截断 | `OFFSET` 可写在 `LIMIT` 里或独立 |
| `LIMIT n BY expr` | 每组取前 n 行 | 常与 `ORDER BY` 搭配做 Top-N |
| `SETTINGS` | 查询级设置 | 覆盖会话与服务端默认 |
| `FORMAT` | 输出格式 | 见 A.18 |
| `INTO OUTFILE` | 结果写文件 | 路径是**服务端**文件系统 |

`SELECT` 的列修饰符(写在 `SELECT` 列表里):

| 修饰符 | 作用 | 例子 |
|---|---|---|
| `FINAL` | 读取时即时合并(去重/取最新) | `SELECT ... FROM t FINAL` |
| `COLUMNS('regex')` | 按正则批量选列 | `SELECT COLUMNS('^m_') FROM t` |
| `APPLY(f)` | 对选中的每列套函数 | `SELECT * APPLY(sum) FROM t` |
| `EXCEPT (cols)` | 排除若干列 | `SELECT * EXCEPT (secret) FROM t` |
| `REPLACE (expr AS col)` | 替换某列表达式 | `SELECT * REPLACE (x*2 AS x) FROM t` |
| `* EXCEPT(...)` | 同上,但用于 `*` | — |

### A.2.2 INSERT 的写法

| 写法 | 干什么 | 例子 |
|---|---|---|
| `INSERT ... VALUES` | 内联字面量 | `INSERT INTO t (a,b) VALUES (1,'x'),(2,'y')` |
| `INSERT ... SELECT` | 从查询结果写入 | `INSERT INTO t SELECT * FROM src` |
| `INSERT ... FORMAT` | 按格式写入原始数据 | `INSERT INTO t FORMAT JSONEachRow {"a":1}` |
| `INSERT ... INFILE` | 从服务端文件读入 | `INSERT INTO t FROM INFILE '/data/x.csv' FORMAT CSV` |
| `INSERT ... TABLE FUNCTION` | 经表函数写入 | `INSERT INTO FUNCTION remote('h', db, t) SELECT ...` |
| `INSERT ... SETTINGS` | 带设置写入 | `INSERT INTO t SETTINGS async_insert=1 VALUES ...` |

要点:`VALUES` 与 `FORMAT` **不能同一条里混用**;`INSERT SELECT` 是最容易被后台 merge 拖慢的写法(第 08 章)。

### A.2.3 CREATE

| 语句 | 干什么 | 最小例子 |
|---|---|---|
| `CREATE DATABASE` | 建库 | `CREATE DATABASE db ENGINE = Atomic` |
| `CREATE TABLE` | 建表 | `CREATE TABLE t (ts DateTime, v UInt64) ENGINE=MergeTree ORDER BY ts` |
| `CREATE TABLE ... AS` | 复制结构 | `CREATE TABLE b AS a` |
| `CREATE TABLE ... AS SELECT` | 建表并灌数据 | `CREATE TABLE b ENGINE=MergeTree ORDER BY x AS SELECT ...` |
| `ALTER TABLE ... ADD INDEX` | 给已有表加数据跳过索引(**ClickHouse 没有独立的 `CREATE INDEX`**;索引只能建表时声明或事后 `ALTER ... ADD INDEX`) | `ALTER TABLE t ADD INDEX i v TYPE minmax GRANULARITY 4`(见 A.2.7) |
| `CREATE VIEW` | 普通视图(不存数据) | `CREATE VIEW v AS SELECT a,b FROM t` |
| `CREATE MATERIALIZED VIEW` | 插入触发器 | `CREATE MATERIALIZED VIEW mv TO dst AS SELECT ... FROM src` |
| `CREATE MATERIALIZED VIEW ... REFRESH EVERY ...` | **可刷新**物化视图(按周期重跑整段查询写回,**不再需要实验开关**) | `CREATE MATERIALIZED VIEW mv REFRESH EVERY 1 HOUR TO dst AS SELECT ...`(见 A.2.7、A.19) |
| `CREATE LIVE VIEW` | 实时视图(配 `WATCH`) | `CREATE LIVE VIEW lv AS SELECT count() FROM t` |
| `CREATE DICTIONARY` | 建字典 | `CREATE DICTIONARY d (...) PRIMARY KEY k SOURCE(...) LAYOUT(...) LIFETIME(...)` |
| `CREATE FUNCTION` | SQL 用户定义函数 | `CREATE FUNCTION f AS x -> x + 1` |
| `CREATE USER` / `ROLE` | 账号与角色 | `CREATE ROLE r` |
| `CREATE ROW POLICY` | 行级策略 | `CREATE ROW POLICY p ON t USING uid = currentUser()` |
| `CREATE QUOTA` / `SETTINGS PROFILE` | 配额与设置档 | `CREATE SETTINGS PROFILE p SETTINGS max_memory_usage=1e9` |
| `CREATE NAMED COLLECTION` | 命名凭据集合 | `CREATE NAMED COLLECTION c AS user='u'` |

字典四要素(`PRIMARY KEY` + `SOURCE` + `LAYOUT` + `LIFETIME`)见第 21 章。

若这些 `CREATE` 语句是从 MySQL/PostgreSQL 移植过来的,除了语法,还要做**类型映射**(源端的 `VARCHAR`/`TINYINT`/`DATETIME` 等各自该换成什么)与几处 DDL 差异——那份映射收在**第 37 章附录 E**(MySQL/PostgreSQL 迁移 playbook),这里不重复。

### A.2.4 ALTER

| 语句 | 干什么 |
|---|---|
| `ALTER TABLE ... ADD COLUMN` | 加列(默认对旧 part 用默认值/`DEFAULT` 表达式) |
| `DROP COLUMN` / `RENAME COLUMN` / `COMMENT COLUMN` | 删/改名/加注释 |
| `MODIFY COLUMN` | 改类型、改默认表达式 |
| `MODIFY ORDER BY` | 改排序键(只对新 part 生效,是**元数据**改动) |
| `MODIFY TTL` / `REMOVE TTL` | 增改/移除 TTL(第 18 章) |
| `MODIFY SETTING` / `RESET SETTING` | 改表级 `SETTINGS` |
| `ADD/DROP/MATERIALIZE/CLEAR INDEX` | 数据跳过索引(第 11 章) |
| `ALTER TABLE ... UPDATE` / `DELETE` | **mutation**:异步重写 part(第 18 章)。**注意与下面的轻量级 `DELETE FROM`/`UPDATE ... SET` 是两回事** |
| `DROP/DETACH/ATTACH PARTITION` | 整分区操作 |
| `MOVE PARTITION` | 在磁盘/卷/表之间搬分区 |
| `FREEZE` / `UNFREEZE` | 做可备份的硬链接快照 |
| `CLEAR COLUMN` | 把某列重置为默认值 |

`ALTER` 在 ClickHouse 里分成两类:**改元数据的**(加列、改注释,秒级)和**改数据的**(`UPDATE`/`DELETE`/`MODIFY COLUMN` 换类型,即 mutation,异步、昂贵)。混淆这两类是"为什么 `ALTER` 突然卡住"的常见根因,详见第 09 章与第 18 章。

### A.2.5 其余常用语句

| 语句 | 干什么 | 最小例子 |
|---|---|---|
| `DROP TABLE/DATABASE/VIEW/DICTIONARY/USER/ROLE` | 删除对象 | `DROP TABLE IF EXISTS t` |
| `DETACH TABLE` / `ATTACH TABLE` | 卸载/挂载(元数据还在) | `DETACH TABLE t` |
| `RENAME TABLE a TO b` | 改名(原子) | `RENAME TABLE a TO a_bak` |
| `TRUNCATE TABLE t` | 清空数据保留结构 | `TRUNCATE TABLE IF EXISTS t` |
| `DELETE FROM t WHERE ...` | **轻量级删除**(与 `ALTER ... DELETE` 的 mutation **并列的第三类**):写一个 `_row_exists` 标记列,读时过滤,立刻可见、不立即重写 part(第 18 章) | `DELETE FROM t WHERE id < 0` |
| `UPDATE t SET c = expr WHERE ...` | **轻量级更新**:语法与轻量级删除同族(靠 patch part 在读取时叠加),但**本书对标的 25.3 上尚未提供**——它到 25.8 才进入 Beta | `UPDATE t SET v = 0 WHERE id = 1` |
| `WATCH lv` | 对 `LiveView` 持续推送新数据(流式) | `WATCH my_live_view` |
| `OPTIMIZE TABLE t [PARTITION p] [FINAL] [DEDUPLICATE]` | 强制合并 | `OPTIMIZE TABLE t PARTITION 202501 FINAL` |
| `SHOW TABLES/DATABASES/DICTIONARIES/PROCESSLIST/CREATE` | 查元数据 | `SHOW CREATE TABLE t` |
| `DESCRIBE TABLE t` | 看列定义(等价 `DESC`) | `DESCRIBE TABLE t` |
| `EXISTS TABLE t` / `EXISTS DATABASE db` | 是否存在(返回 0/1) | `EXISTS TABLE t` |
| `SET name = value` | 设置会话级参数 | `SET max_threads = 8` |
| `USE db` | 切换当前库 | `USE analytics` |
| `KILL QUERY WHERE ...` | 杀查询 | `KILL QUERY WHERE query_id='x' SYNC` |
| `KILL MUTATION WHERE ...` | 杀 mutation | `KILL MUTATION WHERE mutation_id='...'` |
| `GRANT` / `REVOKE` | 授权/收权 | `GRANT SELECT ON db.* TO r` |
| `SYSTEM ...` | 运维命令(见下) | `SYSTEM RELOAD DICTIONARIES` |
| `CHECK TABLE t` | 校验 | `CHECK TABLE t` |

`EXPLAIN` 有**六种**:`EXPLAIN AST`(语法树)、`EXPLAIN SYNTAX`(改写后的 SQL)、`EXPLAIN QUERY TREE`(新分析器 pass 之后的查询树)、`EXPLAIN PLAN`(逻辑计划)、`EXPLAIN PIPELINE`(执行管线)、`EXPLAIN ESTIMATE`(行数与 part 估算);只写 `EXPLAIN`(不带子句)默认等价于 `EXPLAIN PLAN`,不算第七种。另有一个**修饰符** `EXPLAIN indexes = 1 SELECT ...`(或 `EXPLAIN indexes = 1, json = 1 ...`),它不新增 EXPLAIN 种类,而是让输出附带"这一查询用了哪些主键/分区/跳过索引、裁掉了多少 granule"——判断"索引到底有没有生效"就靠它。排查的思路见第 24 章。

**集合运算**:`UNION ALL`(直接拼接,不去重)、`UNION DISTINCT`(拼接后去重)。**裸写 `UNION`(既不写 `ALL` 也不写 `DISTINCT`)在 `union_default_mode` 为空(默认)时会直接报错**,不会静默等同于 `DISTINCT`——该设置的默认值就是"空 = 必须显式写",报错文案形如 `Expected ALL or DISTINCT, because setting (union_default_mode) is empty`;把它设成 `'ALL'`/`'DISTINCT'` 后,裸 `UNION` 才按该模式解释(第 07 章)。`EXCEPT`(差集)、`INTERSECT`(交集)各有 `ALL`/`DISTINCT` 两种形态,**且默认模式都是 `ALL`(结果里可能保留重复行),与标准 SQL 相反**;要标准 SQL 那种去重语义须显式写 `INTERSECT DISTINCT`/`EXCEPT DISTINCT`(第 07 章)。用哪支列名由第一个 `SELECT` 决定。

**两类"改数据"要分清**:**轻量级 `DELETE FROM`**(本节上表)是"标记式"的读时过滤(靠隐藏的 `_row_exists` 掩码列),立刻可见、不立即重写 part;**`ALTER TABLE ... UPDATE/DELETE`** 才是 mutation(异步重写整份 part)。就本书对标的 25.3 而言:**轻量级 `DELETE FROM` 已经可用;轻量级 `UPDATE`(同族的 patch part 方案)尚未提供,它到 25.8 才进入 Beta**——所以批量改动生产上更稳的仍是分区替换或 `ReplacingMergeTree` 墓碑,原理见第 18 章。

`SYSTEM` 常用子命令:`FLUSH LOGS`(立刻刷系统日志)、`RELOAD DICTIONARIES [d]`、`RESTART REPLICAS` / `RESTART REPLICA t`、`SYNC REPLICA t`(等副本追平)、`DROP REPLICA t`、`STOP/START MERGES t`、`STOP/START TTL MERGES t`、`STOP/START MOVES`、`STOP/START FETCHES`、`FLUSH DISTRIBUTED t`、`DROP DNS CACHE` / `DROP MARK CACHE` / `DROP UNCOMPRESSED CACHE` / `DROP QUERY CACHE`、`UNFREEZE`、`SHUTDOWN`(谨慎)。

### A.2.6 GROUP BY 的扩展

`GROUP BY` 后能挂的"超级聚合"修饰符与产出行数(语义与坑的完整讲法见第 12 章 §12.8):

```sql
GROUP BY a, b                      -- 只出 n 行
GROUP BY a, b WITH TOTALS          -- n + 1 行:多一行"总计"
GROUP BY a, b WITH ROLLUP          -- 每个前缀一级小计
GROUP BY a, b WITH CUBE            -- 2^k 个组合(含空集)
GROUP BY GROUPING SETS ((a), (b), ())
GROUP BY ALL                       -- 把 SELECT 里所有非聚合表达式都当分组键
```

被"卷起来"的键列默认填类型默认值,要用 `GROUPING(x)` 或 `group_by_use_nulls` 才能把"总计行"和"键真是 0 的行"分开——为什么,见第 12 章 §12.8。

### A.2.7 数据跳过索引、投影与物化视图的三种形态

这三样都能让某类查询变快,但机制不同,别混用。原理见第 11、28 章与第 04 章;这里只留语法骨架。

```sql
-- 数据跳过索引:建表时声明,或事后 ALTER 加上并回填(没有独立的 CREATE INDEX)
INDEX idx_msg msg TYPE tokenbf_v1(256, 2, 0) GRANULARITY 4
ALTER TABLE t ADD INDEX idx_msg msg TYPE tokenbf_v1(256, 2, 0) GRANULARITY 4;
ALTER TABLE t MATERIALIZE INDEX idx_msg;   -- 对已有 part 生效
ALTER TABLE t DROP INDEX idx_msg;

-- 投影:按另一个排序键再存一份,优化器可自动改写命中
ALTER TABLE t ADD PROJECTION p (SELECT * ORDER BY (b, ts));
ALTER TABLE t MATERIALIZE PROJECTION p;
```

物化视图按"数据什么时候进来"分三种:**普通 MV(插入触发器)** `CREATE MATERIALIZED VIEW mv TO dst AS SELECT ...`(源表每写一块加工一块,建视图前的历史不自动补,**不是**定时刷新);**可刷新 MV** `CREATE MATERIALIZED VIEW mv REFRESH EVERY 1 HOUR TO dst AS SELECT ...`(按周期重跑整段查询、整份替换;不再需要实验开关,旧的 `allow_experimental_refreshable_materialized_view` 自 24.10 起已废弃);**`AggregatingMergeTree` + MV** 增量预聚合(写 `-State`、读 `-Merge`,见 §F.3)。跳过索引的具体种类与能力、投影是否命中(`optimize_use_projections`,默认以 `system.settings` 为准)分别见第 11、28 章。

## A.3 函数速查:总览与命名

真实的 ClickHouse 有一千多个函数名,但其中很大一部分是**组合器**生成出来的(见 A.15)。速查的用法是先在下面各族里找代表函数,找不到再去 `system.functions` 全量搜:

```sql
SELECT name, is_aggregate, case_insensitive
FROM system.functions
WHERE name ILIKE '%arrayJoin%'
ORDER BY name;
```

两件事先说清楚:**函数名大小写不敏感**(`is_aggregate` 为 `0` 的就都是标量函数);**运算符就是函数**,`a + b` 在解析后就是 `plus(a, b)`,所以下面列 `plus` 时也是在说 `+`。函数族的原理与坑在第 17 章,这里只给名字与用途。

## A.4 算术与位运算

| 函数 | 用途 |
|---|---|
| `plus(a,b)` / `minus` / `multiply` | `+` `-` `*` |
| `divide(a,b)` | 除法,**返回浮点**;除零给 `inf`/`nan` |
| `intDiv(a,b)` / `intDivOrZero` | 整除(向零截断);除零时报错 / 给 0 |
| `modulo(a,b)` / `moduloOrZero` | 取余;余数符号跟随被除数 |
| `negate(x)` / `abs(x)` | 取负 / 绝对值 |
| `bitAnd` / `bitOr` / `bitXor` / `bitNot` | 位与/或/异或/取反(按位宽取反) |
| `bitShiftLeft` / `bitShiftRight` | 移位(有符号数也是逻辑右移) |
| `bitRotateLeft` / `bitRotateRight` | 循环移位 |
| `bitCount(x)` / `bitTest(x,n)` | 置位个数 / 第 n 位是否为 1 |
| `gcd` / `lcm` | 最大公约数 / 最小公倍数 |

## A.5 比较

| 函数 | 用途 |
|---|---|
| `equals`(`=`) / `notEquals`(`!=` `<>`) | 相等 / 不等(`NULL` 参与 → `NULL`) |
| `less` / `greater` / `lessOrEquals` / `greaterOrEquals` | `<` `>` `<=` `>=` |
| `isNotDistinctFrom`(`<=>`) | **不区分**式相等:`NULL` 与 `NULL` 视为相等,恒返回 0/1 |
| `isDistinctFrom` | `<=>` 的逻辑反(Null-safe 不等);**25.3 上尚未提供**,别照搬新版函数名 |
| `greatest(a,b,...)` / `least(...)` | 取最大/最小,**忽略 `NULL`**(自 24.12 起) |
| `ifNull(x, alt)` / `nullIf(a,b)` | 空值兜底 / 相等时置 `NULL` |

比较结果类型是 `UInt8`(不是布尔)。先把方向记对:`<=>` 是"不区分"式相等——`NULL` 与 `NULL` 视为相等,和"两列真不一样"恰好相反。

两条版本边界要记牢:**在 25.3 上,`<=>`(`isNotDistinctFrom`)只能出现在 `JOIN ON` 段**,写进 `WHERE` 等通用位置会抛 `NOT_IMPLEMENTED`;它的对偶 `isDistinctFrom`(逻辑反)在 25.3 上**根本不存在**。所以在 25.3 上要找"两列真的不一样"的行,只能用显式处理 `NULL` 的写法:`WHERE (a != b) OR (isNull(a) != isNull(b))`(`!=` 遇 `NULL` 会给 `NULL`,要靠 `isNull` 补齐)。

版本锚点**未确认**:两者通用支持的落地版本只有上游 PR 的表述(`isNotDistinctFrom` 记在 25.10 附近、`isDistinctFrom` 记在 25.11 附近),**未从官方 changelog 复核**,请以目标版本文档为准。这条边界的正文讲法见第 17 章 §17.5。

## A.6 逻辑与条件

| 函数 | 用途 |
|---|---|
| `and(a,b)` / `or(a,b)` / `not(x)` / `xor(a,b)` | 三值逻辑:`NULL AND 0 = 0`、`NULL OR 1 = 1` |
| `if(cond, then, else)` | 三元(分支惰性求值) |
| `multiIf(c1,v1,c2,v2,...,else)` | 扁平多分支,**参数个数为奇数** |
| `coalesce(a,b,...)` | 返回第一个非 `NULL`(把 `Nullable` 拉回不可空) |
| `ifNull(x, alt)` | `coalesce` 的二元形式 |
| `nullIf(a,b)` | `a=b` 时返回 `NULL`,结果类型总为 `Nullable` |
| `assumeNotNull(x)` | 只摘 `Nullable` 标记、**不检查值** |
| `isNull(x)` / `isNotNull(x)` | 判空 |

## A.7 取整与数学

| 函数 | 用途 |
|---|---|
| `floor` / `ceil` / `trunc` / `round` | 向下 / 向上 / 向零 / 四舍五入(半远离零) |
| `roundBankers(x[,n])` | 银行家舍入(半取偶) |
| `roundToExp2` / `roundDuration` / `roundAge` | 按 2 的幂 / 时长 / 年龄区间取整 |
| `exp` / `log` / `log2` / `log10` / `sqrt` / `cbrt` | 指数与对数、平方/立方根 |
| `pow(a,b)` / `intExp2` / `intExp10` | 幂 |
| `sin`/`cos`/`tan`/`asin`/`acos`/`atan`/`atan2` | 三角函数 |
| `sign(x)` / `erf(x)` / `factorial(n)` | 符号 / 误差函数 / 阶乘 |
| `isNaN(x)` / `isInfinite(x)` / `isFinite(x)` | 浮点判定 |
| `pi()` / `e()` | 常数 |

`floor/ceil/trunc/round` 在负数上不一致、"半"是二进制意义上的半——见第 17 章。

## A.8 字符串与正则

| 函数 | 用途 |
|---|---|
| `length` / `lengthUTF8` | 字节数 / 码点数 |
| `empty` / `notEmpty` | 空串判定 |
| `lower` / `upper` | ASCII 大小写(不改字节长度) |
| `lowerUTF8` / `upperUTF8` | Unicode 大小写(可能改变长度,如 `ß`→`SS`) |
| `substring(s,off[,len])` / `substringUTF8` | 取子串,**1-based**,负 off 从尾部数 |
| `reverse` / `reverseUTF8` | 反转 |
| `concat` / `concatWithSeparator` | 拼接(`concat` 遇 `NULL` 返回 `NULL`) |
| `position(hay,needle)` / `positionUTF8` | 首次出现位置,找不到返回 **0** |
| `trim` / `trimLeft` / `trimRight` / `trimBoth` | 去空白 |
| `replaceAll` / `replaceOne` | **字面量**替换 |
| `replaceRegexpAll` / `replaceRegexpOne` | **正则**替换 |
| `match(hay,pat)` / `extract` / `extractAll` | 正则匹配 / 提取 / 全提取(RE2 语法) |
| `like` / `ilike` / `notLike` | SQL 通配符匹配 |
| `splitByChar` / `splitByString` / `splitByRegexp` / `splitByWhitespace` | 切分(分隔符在**第一**参数) |
| `startsWith` / `endsWith` / `hasSubstr` | 前后缀与子串包含 |
| `format` / `formatPattern` / `leftPad` / `rightPad` | 格式化、补位 |
| `repeat(s,n)` / `space(n)` | 重复、空白 |

正则走 **RE2**(线性时间),**不支持反向引用与环视**。

## A.9 日期时间

| 函数 | 用途 |
|---|---|
| `now()` / `now64([精度])` / `today()` / `yesterday()` | 当前时刻(秒 / 亚秒精度,如 `now64(3)`)/ 今日 / 昨日 |
| `toDate` / `toDateTime` / `toDateTime64` | 类型转换 |
| `toStartOfYear/Month/Week/Day/Hour/Minute` | 截断到区间起点 |
| `toStartOfFiveMinute` / `toStartOfFifteenMinutes` / `toStartOfInterval(t, INTERVAL n unit)` | 固定/任意步长截断 |
| `toYear` / `toMonth` / `toDayOfMonth` / `toHour` / `toMinute` / `toSecond` | 取字段(按列时区) |
| `toYYYYMM` / `toYYYYMMDD` / `toYYYYMMDDhhmmss` | 取整数形式的日期 |
| `dateDiff(unit, a, b)` | 跨过了几个单位边界 |
| `age(unit, a, b)` | 完整的单位个数 |
| `dateAdd(unit,n,t)` / `dateSub` / `addMonths` / `addDays` | 日历感知加减 |
| `formatDateTime(t, fmt)` | 按格式串渲染 |
| `toTimeZone(t, tz)` / `timeZone(t)` | 换/取时区(存储值不变) |
| `toUnixTimestamp(t)` / `fromUnixTimestamp(n)` | Unix 时间戳互转 |
| `parseDateTime` / `parseDateTimeBestEffort` | 解析字符串 |
| `timeSlots` / `toMonday` / `toStartOfISOYear` | 其他取整 |

时区是**列类型**的属性(`DateTime('Asia/Shanghai')`)而非会话属性,见第 17 章。

## A.10 数组与高阶函数

| 函数 | 用途 |
|---|---|
| `array(1,2,3)` / `[1,2,3]` | 构造数组 |
| `arr[i]` / `arrayElement(arr,i)` | 取元素,**1-based**,越界给默认值 |
| `indexOf(arr,x)` / `has(arr,x)` | 位置(找不到 0)/ 是否含 |
| `arrayConcat` / `arraySlice` / `arrayReverse` | 拼接 / 切片 / 反转 |
| `arrayDistinct` / `arrayUniq` / `arrayJoin` | 去重 / 去重计数 / **炸成多行**(改行数) |
| `arraySort([f,]arr)` / `arrayReverseSort` | 排序(可带 lambda 排序键) |
| `arrayCumSum` / `arraySum` / `arrayAvg` / `arrayMin` / `arrayMax` | 前缀和 / 统计 |
| `arrayFlatten` / `arrayEnumerate` / `arrayZip` | 压平 / 下标数组 / 拉链 |
| `arrayMap(f, arr...)` | 逐元素映射(等长) |
| `arrayFilter(f, arr)` | 保留断定式为真的元素 |
| `arrayCount` / `arrayExists` / `arrayAll` / `arrayFirst` | 计数 / 存在 / 全真 / 首个匹配 |
| `arrayFold((acc,x)->..., arr, init)` | 归约(累加器是**最后**参数) |
| `range(n)` / `arrayReduce(f, arr)` | 生成区间 / 把聚合函数作用到数组 |

lambda 语法 `x -> expr` 或 `(x,y) -> expr`;**多数组长度不等时,真实 ClickHouse 会直接报错 `SIZES_OF_ARRAYS_DONT_MATCH`,不会补默认值**——`arrayMap(f, [1,2], [3])` 在真实版上失败。配套的 mini-clickhouse 出于教学便利**故意**把这条改成"短的按元素默认值补齐到最长",那是它与上游的一处**已知偏差**,别拿它反推真实行为(第 17 章 §17.12 讲的是真实语义)。

## A.11 元组、Map 与 JSON

| 函数 | 用途 |
|---|---|
| `tuple(a,b,...)` | 构造元组(每位置保留自身类型) |
| `tupleElement(t,i)` / `t.1` / `t.name` | 取成员(1-based,负从尾) |
| `untuple(t)` | 展开为多列 |
| `map(k1,v1,...)` / `{k:v}` | 构造 Map |
| `mapKeys(m)` / `mapValues(m)` / `mapContains(m,k)` | 键列表 / 值列表 / 是否含键 |
| `visitParamExtractString/Int/Float` / `visitParamHas` | **快速扫描**式 JSON(不解码转义) |
| `JSONExtractString/Int/UInt/Float/Bool/Raw` | 真正的 JSON 解析(支持路径) |
| `JSONExtract(keys...)` / `JSONHas` / `JSONLength` | 通用提取 / 存在 / 长度 |
| `JSON_VALUE` / `JSON_QUERY` / `JSON_EXISTS` | SQL/JSON 风格接口,25.3 上可用;返回值受 `function_json_value_return_type_allow_nullable` 等设置影响。**引入版本未确认**:它们随 24.x 的 JSON 重构一同成形,不是 21.x——"`JSON_EXISTS` 自 v21.8、`JSON_VALUE` 自 v21.11"这类旧锚点与这次重构对不上,以目标版本文档为准 |
| `dictGet('db.dict', 'attr', key)` | 按主键取字典属性;返回类型由字典 `ATTRIBUTE` 的声明固定 |
| `dictGetOrDefault('db.dict', 'attr', key, default)` | 同上,键不存在时返回给定的默认值 |
| `dictHas('db.dict', key)` | 键是否存在于字典(返回 `UInt8`) |
| `dictGetString` / `dictGetUInt64` / `dictGetInt64` / `dictGetFloat64`(`*OrDefault` 变体同理) | 显式指定返回类型的取属性变体 |
| `dictGetChildren('db.dict', key)` / `dictGetDescendants('db.dict', key[, level])` | 层级字典:取直接子节点 / 全部后代 |
| `dictGetHierarchy('db.dict', key)` / `dictIsIn('db.dict', child, ancestor)` | 层级字典:取自根到该键的路径 / 判断祖先关系 |

JSON 两族的区别是一条硬规则:输入含转义序列就**一定**用 `JSONExtract*`,见第 17 章。字典那几族函数(`dictGet*` / `dictHas` / `dictGetChildren` 等)在此一并列出,是因为它们的调用形态最像"查 Map 的键";完整语义(布局、`LIFETIME`、层级字典)见第 21 章。

## A.12 URL、IP、编码与哈希

| 函数 | 用途 |
|---|---|
| `domain(url)` / `domainWithoutWWW` / `path` / `pathFull` / `queryString` | 取 URL 组件(已解析) |
| `extractURLParameter(url,name)` / `extractURLParameters` | 取查询参数 |
| `protocol` / `topLevelDomain` / `firstSignificantSubdomain` | 协议 / 顶级域 / 主域 |
| `toIPv4` / `toIPv6` / `IPv4NumToString` / `IPv6NumToString` | IP 与数值/字符串互转 |
| `isIPAddressInRange(addr, 'cidr')` | 是否落在网段 |
| `hex(x)` / `unhex(s)` | 十六进制(整数按**大端可读序**、省略前导零字节、大写输出;小端只有 `Float`/`Decimal` 才是那样) |
| `bin(x)` / `base64Encode` / `base64Decode` | 二进制串 / Base64 |
| `bitmaskToList` / `bitmaskToArray` | 位掩码 |
| `sipHash64` / `sipHash128` / `xxHash64` / `cityHash64` / `intHash64` | 整数哈希(做分桶/去重键) |
| `MD5` / `SHA1` / `SHA256` | 密码学摘要(返回**原始字节** `FixedString`,要十六进制用 `hex(...)`) |
| `halfMD5` / `farmHash64` / `murmurHash3_64` | 其他哈希 |

哈希输出**可能跨版本变化**,跨系统比对前先确认两边版本一致(不确定的情形按"未确认"处理)。

## A.13 类型转换与元函数

| 函数 | 用途 |
|---|---|
| `toInt8/16/32/64/128/256` / `toUInt*` | 整数转换 |
| `toFloat32/64` / `toDecimal32/64/128/256` | 浮点 / 定点 |
| `toString` / `toDate` / `toDateTime` / `toUUID` | 常用转换 |
| `CAST(x AS T)` / `accurateCast` | 标准转换(失败报错或给默认值) |
| `toTypeName(x)` | 取类型名(**结果类型是 `String`**) |
| `reinterpretAsString/UInt64/...` | **按位重解释**(不换值,换读法) |
| `toNullable` / `toLowCardinality` | 加标记 |
| `materialize(x)` | 把常量列展开成完整列 |
| `blockSize()` | 当前块行数 |
| `bar(x, min, max, w)` | 画进度条 |
| `formatReadableSize(x)` / `formatReadableQuantity(x)` | 把字节数 / 数量渲染成 `'1.50 MiB'` / `'1.23 million'`(看 `system.parts` 的体积与行数时常用) |
| `defaultValueOfTypeName(T)` / `nullIf` | 取某类型默认值 |
| `rand()` / `rand64([n])` | 伪随机数(`UInt32`/`UInt64`),可选上限 |
| `randConstant()` | 每条查询一个常量随机值(可作缓存键里固定的一环) |
| `generateUUIDv4()` / `generateUUIDv7()` | 生成随机 UUID |

**非确定性函数的代表就是 `rand`/`rand64`/`generateUUIDv4`/`now`**:同输入不保证同输出,所以既不能被常量折叠,也**不能进查询缓存**——这正是 A.16 讲 query cache 的前提概念。它们与"确定性函数"的边界见第 17 章 §17.20。

## A.14 聚合函数

聚合函数的参数是"一整列",与标量函数的调用形态一样,但语义是"折叠成一行"。

| 函数 | 用途 |
|---|---|
| `count()` / `count(x)` / `countIf(cond)` | 行数 / 非 `NULL` 计数 / 条件计数 |
| `sum` / `sumIf` / `sumKahan` / `sumWithOverflow` | 求和(含补偿求和/溢出检测) |
| `avg` / `avgWeighted` / `avgIf` | 平均 |
| `min` / `max` / `argMin(v,k)` / `argMax(v,k)` | 极值 / 取 k 极值处的 v |
| `any` / `anyLast` / `anyHeavy` | 任取一个 / 取最后一个 / 重频率取样 |
| `uniq` / `uniqExact` / `uniqCombined` / `uniqHLL12` / `uniqUpTo` | 去重计数(精度/内存不同) |
| `groupArray(x)` / `groupUniqArray` / `groupArrayInsertAt` | 聚成数组 |
| `quantile(q)(x)` / `quantiles(q1,q2)(x)` | 分位数(近似) |
| `quantileExact` / `quantileTDigest` / `quantileTiming` | 精确 / TDigest / 计时专用 |
| `median` / `medianExact` | 中位数(= q=0.5 的分位数) |
| `stddevPop` / `stddevSamp` / `varPop` / `varSamp` | 标准差与方差 |
| `corr` / `covarPop` / `covarSamp` / `rankCorr` | 相关系数与协方差 |
| `skewPop` / `skewSamp` / `kurtPop` / `kurtSamp` | 偏度与峰度 |
| `topK(n)(x)` / `topKWeighted` | 最高频 n 个 |
| `entropy(x)` | 熵 |
| `histogram(n)(x)` | 直方图 |
| `sumMap(k,v)` / `maxIntersections` / `sequenceCount` | Map 求和 / 区间最大重叠 / 序列计数 |
| `windowFunnel(window)(ts, cond1, cond2, ...)` | 滑动窗口内**按序**命中了几个步骤(漏斗) |
| `retention(cond1, cond2, ...)` / `sequenceMatch(pattern)(ts, cond...)` | 留存(首事件之后各步是否回访)/ 图案匹配 |
| `groupBitAnd` / `groupBitOr` / `groupBitXor` | 按位聚合 |

序列/漏斗这一族(`windowFunnel`/`retention`/`sequenceMatch`/`sequenceCount`)见第 12 章 §12.6 与第 16 章。**手写转化漏斗时正是它们该上场的地方**:用条件聚合 `uniqIf(user_id, event='view')` 这样三段并列**丢掉事件时序**,会把"只 pay 没 view"的用户也算进来,**系统性高估转化**;附录 F 的 §F.6 给了 `windowFunnel` 的正确写法,两处对照看。

注意 `first_value` / `last_value` / `nth_value` **不是聚合函数,而是窗口函数**(必须带 `OVER (...)`;默认帧下 `last_value` 拿到的是当前行、不是分区末尾)。帧语义见第 16 章 §16.10、§16.12 与 A.21;想做"分组内取第一条",用 `argMin`/`argMax` 这类真正的聚合函数。

选 `uniq` 还是 `uniqExact`、`quantile` 还是 `quantileExact`,是"近似换内存"的权衡,第 12 章。

## A.15 聚合组合器

组合器把基函数**套壳**扩成一族。理解它,`system.functions` 里那一大片名字就都解释得通了(第 12 章有原理)。

| 组合器 | 含义 | 例子 |
|---|---|---|
| `-If` | 追加一个条件列,只在条件成立的行上聚合 | `sumIf(x, cond)` |
| `-Array` | 参数由 `Array(T)` 变 `T`,对数组元素聚合 | `sumArray(arr)` |
| `-State` | 返回**聚合中间状态**而非结果(类型 `AggregateFunction(...)`) | `sumState(x)` |
| `-Merge` | 吃一个 `-State` 产生的状态,合并出结果 | `sumMerge(state)` |
| `-Distinct` | 先对参数去重再聚合 | `sumDistinct(x)` |
| `-OrNull` | 结果类型变 `Nullable`,空输入给 `NULL` | `sumOrNull(x)` |
| `-OrDefault` | 空输入给该结果类型的默认值 | `sumOrDefault(x)` |
| `-Resample` | 按时间轴分桶,参数形态为 `aggResample(start, end, step)(值, 时间戳列)`(具体以文档为准) | `sumResample(0, 3600, 10)(value, ts)` |
| `-ForEach` | 参数与结果都变成数组,逐元素聚合 | `sumForEach([1,2],[3,4])` = `[4,6]` |

**叠加规则:** 组合器可以再叠加,常见的有 `sumIfState`、`sumIfMerge`、`quantileIf`、`uniqState`。解析时实现**从右往左剥离后缀**,基函数名剩下的部分才是真正的聚合。两点提醒:第一,`-State`/`-Merge` 是**成对**出现的——写进 `AggregatingMergeTree` 用 `-State`,查出来用 `-Merge`(第 12、37 章);第二,组合器**并非任意两两都能叠**,具体哪些名字存在以 `system.functions` 为准,别凭直觉拼。

```sql
-- 预聚合写入:存的是"状态"
INSERT INTO daily SELECT d, sumState(v), uniqState(uid)
FROM raw GROUP BY d;
-- 查询:合并状态
SELECT d, sumMerge(sum_v), uniqMerge(u_uid) FROM daily GROUP BY d;
```

## A.16 常用设置速查

按类分组。**默认值凡不确定一律标"未确认"**;判断某个设置是"会话级"还是"表级(MergeTree)",看它在 `system.settings` 还是 `system.merge_tree_settings`(第 27 章)。

读取与查询:

| 设置 | 默认 | 作用 |
|---|---|---|
| `max_threads` | `0`(= 自动,按核数) | 单查询最大线程数 |
| `max_block_size` | `65409` | 处理块行数。这个数是**有出处的**:`src/Core/Defines.h` 里 `DEFAULT_BLOCK_SIZE = 65409` 并附注释 `65536 - PADDING_FOR_SIMD - (PADDING_FOR_SIMD - 1)`(即 `65536 − 64 − 63`),详见第 10 章 §10.4 |
| `max_query_size` | `262144` | 解析用最大查询字节数 |
| `max_ast_depth` | `1000` | 语法树最大深度 |
| `max_parser_depth` | `1000` | 递归下降解析最大深度 |
| `max_subquery_depth` | `100` | 子查询嵌套深度 |
| `readonly` | `0` | `1` 禁写、`2` 连设置都禁改 |
| `use_uncompressed_cache` | `0` | 未压缩块缓存(已不推荐) |
| `optimize_read_in_order` | `1` | 能按 `ORDER BY` 顺序读就不排序 |
| `allow_experimental_analyzer`(别名 `enable_analyzer`) | `1`(自 24.3 默认开) | 是否走新查询分析器。**声明名是长名,`enable_analyzer` 是 24.8 加上的别名**(见第 24 章 §24.17);`system.settings.alias_to` 可当场验证 |
| `distributed_aggregation_memory_efficient` | `1`(以自检为准) | 分布式聚合时各分片先做部分合并,降低发起节点内存 |
| `use_skip_indexes` | `1`(以 `system.settings` 为准) | 是否使用数据跳过索引(第 11 章);关掉它可用来对比"索引到底省了什么" |

结果与格式:

| 设置 | 默认 | 作用 |
|---|---|---|
| `max_result_rows` | `0`(不限) | 结果行数上限 |
| `max_result_bytes` | `0`(不限) | 结果字节上限 |
| `result_overflow_mode` | `throw` | 超限时 `throw` 还是 `break` |
| `output_format_json_quote_64bit_integers` | `1` | JSON 输出里 64 位整数是否加引号。25.3 上是 `1`;有说法称更晚版本(25.8 附近)会改成 `0` 的**破坏性变更**,但该说法的**版本锚点未确认**(晚于本书基线且未见权威出处),升级前用 `system.settings_changes` 自查 |
| `date_time_input_format` | `basic` | 日期输入解析风格(可设 `best_effort`) |

聚合:

| 设置 | 默认 | 作用 |
|---|---|---|
| `group_by_two_level_threshold` | `100000` | 超过这么多键改用两级聚合 |
| `group_by_two_level_threshold_bytes` | `50000000` | 按字节数的两级触发阈值 |
| `max_rows_to_group_by` | `0`(不限) | 聚合前唯一键上限 |
| `group_by_overflow_mode` | `throw` | 超限行为 |
| `max_bytes_before_external_group_by` | `0`(关) | 超此值溢写磁盘做外存聚合 |
| `optimize_aggregation_in_order` | `0` | 按排序键顺序聚合以减少内存 |

JOIN:

| 设置 | 默认 | 作用 |
|---|---|---|
| `join_use_nulls` | `0` | 未匹配时右表列是默认值还是 `NULL` |
| `join_default_strictness` | `ALL` | 不写 `ANY`/`ALL`/`ASOF` 时 `JOIN` 采用的严格度;**默认是 `ALL` 不是 `ANY`**(可选 `ALL`/`ANY`/`ASOF`/空) |
| `join_algorithm` | `direct,parallel_hash,hash` | 允许的 join 算法集合。25.3 的默认列表**不含 `ie_join`**——它是 26.8 才追加到列表末尾的(只在其它算法都不适用时生效,专治 `ON` 里只有不等值条件的情况);`parallel_hash` 优先于 `hash`、旧的 `'default'` 取值废弃,都发生在 24.12。另:`grace_hash` 需要非零的 `max_bytes_before_external_join`(或 `max_bytes_ratio_before_external_join`)才会被启用,否则会被跳过 |
| `max_bytes_in_join` | `0`(不限) | join 用内存上限 |
| `join_overflow_mode` | `throw` | 超限行为 |
| `max_rows_in_set` / `max_bytes_in_set` | `0`(不限) | `IN` 右集合上限 |
| `max_bytes_before_external_join` | 25.3 **不含此设置** | 让 hash join 超阈值时自动转可溢写的 grace hash;官方文档记为 26.4 才引入,25.3 上不要依赖它 |

内存与超时:

| 设置 | 默认 | 作用 |
|---|---|---|
| `max_memory_usage` | `0`(不限) | 单查询内存上限 |
| `max_memory_usage_for_user` | `0`(不限) | 每用户内存上限 |
| `max_execution_time` | `0`(不限) | 查询最大秒数 |
| `timeout_overflow_mode` | `throw` | 超时行为 |
| `max_bytes_before_external_sort` | `0`(关) | 排序溢写磁盘阈值 |
| `max_download_threads` | `4` | 远端下载线程数 |
| `lock_acquire_timeout` | `120` | DDL 锁等待秒数 |
| `mutations_sync` | `0` | `ALTER ... UPDATE/DELETE`(mutation)是否同步等待:0=完全异步、1=等本副本完成、2=等所有副本(第 09 章) |

写入:

| 设置 | 默认 | 作用 |
|---|---|---|
| `max_insert_block_size` | `1048449`(= 2^20 − 127) | 插入时聚合块行数;与 `max_block_size`(`65536 − 127`)同一条"减 127"规则(第 08 章) |
| `insert_deduplicate` | `1` | 同分块重复插入去重(复制表) |
| `async_insert` | `0` | 服务端异步攒批插入 |
| `wait_for_async_insert` | `1` | `async_insert` 下客户端是否等落盘 |
| `max_insert_threads` | `0`(自动) | `INSERT SELECT` 的并行写入线程数上限 |
| `async_insert_max_data_size` | `10485760`(约 10 MiB) | 攒批到这么大就刷;以 `system.settings` 为准 |
| `async_insert_busy_timeout_max_ms`(旧名 `async_insert_busy_timeout_ms`) | OSS 默认 `200` ms(Cloud 预设 `1000`) | 攒批的最长等待;超时即刷,与大小阈值取先到者;配合 `async_insert_busy_timeout_min_ms`(默认 `50`)构成自适应区间 |
| `async_insert_use_adaptive_busy_timeout` | `1`(以自检为准) | 让等待时间随负载自适应 |

分布式与复制:

| 设置 | 默认 | 作用 |
|---|---|---|
| `distributed_product_mode` | `deny` | 跨分片 JOIN 是否允许及形态 |
| `insert_distributed_sync` | `0` | `Distributed` 插入是否同步等各分片 |
| `insert_quorum` | `0` | 写入需确认的副本数(0=关) |
| `insert_quorum_timeout` | `600000` | quorum 等待毫秒数 |
| `select_sequential_consistency` | `0` | 是否读"已确认"的一致快照 |
| `max_parallel_replicas` | `1000` | 单分片并行副本数。25.3 的默认值是 **1000**,**25.1** 由 `1` 抬上来(`SettingsChangesHistory.cpp` 里那条改动落在 25.1 块内;25.3 的 `Settings.cpp` 声明为 `NonZeroUInt64`、默认 `1000`)。早先本书把它标成"未确认",是因为二手资料在 24.12 / 25.4 之间打架;现在以源码为准。**"设成大于 1 就启用"是正文 §23.11 点名作废的旧经验**;它**不接受 `0`**;它不是开关,真正的开关见下一行 |
| `allow_experimental_parallel_reading_from_replicas`(别名 `enable_parallel_replicas`) | `0`(关) | 并行副本的功能开关,取 0/1/2(2=强制)。**25.3 上声明名是长名、短名是它的 `ALIAS`**(`system.settings.alias_to` 可当场验证);该特性在 25.3 里是 **BETA** 档。它**不是**"设成 >1 才启用"的那种开关,见第 23 章 §23.11 |

MergeTree(MergeTree 表设置,查 `system.merge_tree_settings`):

| 设置 | 默认 | 作用 |
|---|---|---|
| `index_granularity` | `8192` | 主键索引每多少行一个标记 |
| `index_granularity_bytes` | `10485760` | 按字节的粒度(自适应粒度用,默认 10 MiB) |
| `parts_to_delay_insert` | `1000`(旧版曾为 `150`) | 单分区 part 超此数则人为拖慢插入。现值 `1000` 无争议(源码 `MergeTreeSettings.cpp` 的 `DECLARE` 行可证);但 **`150 → 1000` 的改动版本来源冲突**——官方 changelog 的 23.6 条目提到过 `1000`,托管版文档却把该设置系于 25.1,而官方设置页**没给它加版本标注**——所以"何时改的"在此**标未确认**,以你版本的 `system.merge_tree_settings` 为准 |
| `parts_to_throw_insert` | `3000`(23.6 前为 `300`) | 超此数直接拒绝插入(官方设置页明确标注:23.6 之前该值是 300) |
| `max_parts_in_total` | `100000` | 全表 part 总数上限 |
| `merge_max_block_size` | `8192` | merge 时处理块行数 |
| `min_bytes_for_wide_part` | `10485760` | 低于此字节数的 part 存紧凑格式 |
| `min_rows_for_wide_part` | `0` | 按行数触发宽格式(0=关) |
| `max_bytes_to_merge_at_max_space_in_pool` | `161061273600` | 单个 merge 的最大字节(150 GiB) |
| `ttl_only_drop_parts` | `0` | TTL 是否允许整分区 drop |

日志与其他:

| 设置 | 默认 | 作用 |
|---|---|---|
| `log_queries` | `1` | 是否把查询记入 `system.query_log` |
| `send_logs_level` | `fatal` | 服务端把多低级别的日志发回客户端 |
| `max_columns_to_read` | `0`(不限) | 单查询可读列数 |
| `allow_experimental_*` | 各自不同 | 开关实验特性(如 window view) |

查询缓存(整体默认关闭,要逐查询或会话显式开启;第 28 章):

| 设置 | 默认 | 作用 |
|---|---|---|
| `use_query_cache` | `0`(关) | 是否允许这条查询走结果缓存;**不开这个,其余参数都没意义** |
| `query_cache_ttl` | `60`(以自检为准) | 缓存条目存活秒数 |
| `query_cache_max_size_in_bytes` | `1073741824`(1 GiB) | 缓存的字节上限 |
| `query_cache_max_entries` | `1024`(以自检为准) | 缓存条目数上限 |
| `query_cache_min_query_runs` | `0` | 同一条查询跑够几次才缓存 |
| `query_cache_share_between_users` | `0` | 缓存条目是否跨用户共享 |
| `query_cache_nondeterministic_function_handling` | `throw` | 含 `now()` 等非确定函数的查询怎么处理,取 `throw`(默认,拒绝缓存)/ `save`(照样缓存)/ `ignore` |

只有**确定性的 `SELECT`**(不带 `now()`/`rand()` 这类非确定函数)才可缓存,且默认按查询文本 + 设置做键——所以加个空格、换个常量都可能不命中。命中情况看 `system.query_cache` 与查询日志里的缓存事件。

输入/输出格式(第 08、19 章):

| 设置 | 默认 | 作用 |
|---|---|---|
| `input_format_skip_unknown_fields` | `1` | 输入里多出的列直接跳过,不报错 |
| `input_format_null_as_default` | `1` | 输入里的 `NULL` 落到非可空列时用默认值 |
| `input_format_defaults_for_omitted_fields` | `1` | 缺失字段用列默认表达式补齐(默认开) |
| `input_format_allow_errors_num` | `0` | 容忍的解析错误条数(超过才中断) |
| `input_format_allow_errors_ratio` | `0` | 容忍的错误比例 |
| `format_csv_delimiter` | `,` | CSV 分隔符(`input_/output_format_csv_delimiter` 亦可覆盖单向) |

(`date_time_input_format` 与 `output_format_json_quote_64bit_integers` 同属这一类,记在前面"结果与格式"表里,不重复列。)

**两条常被误当"会话设置"的服务端配置**(写在 `config.xml` / `config.d/*.xml`,一般不能靠查询级 `SETTINGS` 改;改动多半要重启或重载):

| 配置项 | 默认 | 作用 |
|---|---|---|
| `max_concurrent_queries` | `0`(代码默认;发行版 `config.xml` 预设 `1000`) | 进程级并发查询总数上限;撞到上限默认是**排队**而不是立刻拒绝,队列长度由服务端配置 `max_waiting_queries` 控制(第 25 章) |
| `max_server_memory_usage` | `0`(按物理内存自动推算) | 服务端可用内存上限,逼近它就有 OOM 风险(第 25、29 章) |

它们属于服务端层,别和会话级设置混在一起找;是否也出现在 `system.settings` 里随版本而异,以实际查询结果为准。以 `max_concurrent_queries` 为例,上表那两栏说的是两件事:**代码默认是 `0`(= 不限)**,而**官方发行版的 `config.xml` 模板把它预设为 `1000`**——所以在一台按发行版默认安装的机器上,实际生效的往往就是 1000 而非 0(第 25 章)。

**怎么自查:** `system.settings` 有 `name`、`value`、`changed`、`default`、`type` 五列,直接查就是了:

```sql
SELECT name, value, changed, default FROM system.settings
WHERE name IN ('max_threads','max_block_size') ORDER BY name;
SELECT name, value, default FROM system.merge_tree_settings
WHERE name LIKE 'parts_to%' ORDER BY name;
```

## A.17 类型速查

| 类型 | 字节 | 范围 / 含义 | 文本表示 |
|---|---|---|---|
| `Int8/16/32/64/128/256` | 1/2/4/8/16/32 | 有符号整数 | `-123` |
| `UInt8/16/32/64/128/256` | 1/2/4/8/16/32 | 无符号整数 | `123` |
| `Float32` / `Float64` | 4 / 8 | IEEE 浮点 | `1.5`,`nan`,`inf` |
| `BFloat16` | 2 | 低精度浮点(1 符号 + 8 指数 + 7 尾数,**自 24.11 引入**,初为 `allow_experimental_bfloat16_type` 实验开关、后转正,故 25.3 可用) | `1.5` |
| `Bool` | 1 | 布尔(底层 `UInt8`) | `true`/`false` |
| `Decimal32(S)` / `64` / `128` / `256` | 4/8/16/32 | 定点数,分别是 `Decimal(9,S)` / `Decimal(18,S)` / `Decimal(38,S)` / `Decimal(76,S)` 的别名 | `1.50` |
| `String` | 变长 | 任意字节序列(**二进制安全**) | `'abc'` |
| `FixedString(N)` | N | 定长字节 | `'abc'` |
| `UUID` | 16 | 通用唯一标识 | `'61f0c404-...'` |
| `Date` | 2 | 天(1970-01-01 起) | `'2025-03-01'` |
| `Date32` | 4 | 天,范围更大 | `'1925-01-01'` |
| `DateTime([tz])` | 4 | 秒 | `'2025-03-01 10:00:00'` |
| `DateTime64(P[,tz])` | 8 | 亚秒精度(0–9 位) | `'2025-03-01 10:00:00.123'` |
| `Enum8` / `Enum16` | 1 / 2 | 具名枚举 | `'red'` |
| `Array(T)` | 变长 | 数组,**1-based** | `[1,2,3]` |
| `Tuple(T1,...)` | 变长 | 元组(可具名) | `(1,'a')` |
| `Map(K,V)` | 变长 | 映射(= `Array(Tuple(K,V))`) | `{'a':1}` |
| `Nested(...)` | 变长 | 嵌套(展开为多个数组列) | — |
| `Nullable(T)` | +1 | 可空 | `NULL` |
| `LowCardinality(T)` | 变长 | 字典编码列 | `'x'` |
| `AggregateFunction(...)` | 变长 | 聚合中间状态 | 二进制 |
| `SimpleAggregateFunction(...)` | 依内层 | 可简单合并的聚合状态 | — |
| `IPv4` / `IPv6` | 4 / 16 | IP(`IPv4` 内部是 `UInt32`) | `'1.2.3.4'` |
| `Point`/`Ring`/`Polygon`/`MultiPolygon` | 变长 | 几何类型 | `(1,2)` |
| `JSON` / `Variant` / `Dynamic` | 变长 | 半结构化类型。原生 `JSON` 走完了一条完整曲线:**24.10 引入(实验)、24.12 转 beta、25.3 生产就绪**;开关 `enable_json_type` 自 **25.3 起默认 `true`**,更晚的版本甚至已废弃该开关,可直接使用。**路径推断、子列上限这类内部细节逐版本在调**,以 `system.data_type_families` 与目标版本文档自检 | 依结构 |

整数范围:`Int8` 是 `-128..127`,`UInt8` 是 `0..255`,每升一档乘 256。类型推导与转换规则见第 03 章。

"我这一列该换成什么类型"的答案取决于**源端**是什么类型(`VARCHAR`、`TINYINT`、`DATETIME`、`AUTO_INCREMENT` 各自落到哪),这类源类型映射不在这张速查里展开,见**第 37 章附录 E**(MySQL/PostgreSQL 迁移 playbook)。

## A.18 格式速查

"可读"= 能作为 `INSERT` 输入,"可写"= 能作为 `SELECT ... FORMAT` 输出。

| 格式 | 可读 | 可写 | 典型用途 |
|---|---|---|---|
| `TSV` / `TSVWithNames` / `TSVWithNamesAndTypes` | ✅ | ✅ | 文本往返的默认选择 |
| `CSV` / `CSVWithNames` / `CSVWithNamesAndTypes` | ✅ | ✅ | 给别的程序吃 |
| `Values` | ✅ | ✅ | 与 `INSERT ... VALUES` 同一套语法 |
| `JSON` / `JSONCompact` | ✅ | ✅ | 整个结果集一个对象(meta+data) |
| `JSONEachRow` / `JSONStringsEachRow` | ✅ | ✅ | 一行一个 JSON 文档(流式) |
| `JSONCompactEachRow[WithNames]` | ✅ | ✅ | 每行是数组而非对象 |
| `JSONObjectEachRow` | ✅ | ✅ | 以主键名为顶层 key |
| `TSKV` | ✅ | ✅ | key=value 逐行 |
| `RowBinary` / `RowBinaryWithNames[AndTypes]` | ✅ | ✅ | 逐值二进制,最省字节 |
| `Native` | ✅ | ✅ | ClickHouse 的队列/part 格式,最快 |
| `Pretty` / `PrettyCompact` / `PrettySpace` | ❌ | ✅ | 给人看的表格 |
| `Vertical` | ❌ | ✅ | 一行一块的竖排(列多时) |
| `Markdown` | ❌ | ✅ | Markdown 表格 |
| `SQLInsert` | ❌ | ✅ | 生成 `INSERT` 脚本 |
| `Null` | ❌ | ✅ | 只测性能不看结果 |
| `Parquet` / `ORC` / `Avro` / `Arrow` | ✅ | ✅ | 列存/外部生态交换 |
| `Protobuf` / `CapnProto` / `MsgPack` / `BSONEachRow` | ✅ | ✅ | 二进制序列化协议 |
| `Template` / `CustomSeparated` | ✅ | ✅ | 可配置模板格式 |
| `MySQLDump` / `PostgreSQLDump` | ✅ | ❌ | **只读**(仅输入):解析 `mysqldump` / `pg_dump` 导出的 `INSERT`/`COPY` 脚本 |

细节与"哪种格式能无损往返"见第 19 章。`MySQLDump` / `PostgreSQLDump` 是**纯输入格式**:它们只为"读一份 SQL dump 文件"而存在(`SELECT * FROM file('dump.sql', MySQLDump)`),**没有输出/序列化那一侧**——把它们当 `SELECT ... FORMAT` 的输出去写是错的。这一点从相关设置的 `input_` 前缀也能看出来:从多表 dump 里挑一张表用 `input_format_mysql_dump_table_name`。读 dump 时**跳过 DDL 语句**,并按 `input_format_skip_unknown_fields`(默认开)跳过 dump 里多于目标列的字段;想先看一张表会被推成什么 schema,用 `DESCRIBE file('shop.sql', MySQLDump)` 就能看到推断出的列结构(推断的细节见第 19 章)。

## A.19 表引擎速查

| 引擎 | 适用场景 | 主要限制 |
|---|---|---|
| `MergeTree` | 绝大多数表的默认答案 | 只追加;改数据=重写 part |
| `ReplacingMergeTree([ver[,is_deleted]])` | 同主键保留最新一条 | 只是**最终一致**,查询可能要 `FINAL` |
| `SummingMergeTree([cols])` | 同主键数值列自动相加 | 不能替代聚合,合完前仍是多行 |
| `AggregatingMergeTree` | 增量预聚合(配 MV) | 列是聚合状态,读要用 `-Merge` |
| `CollapsingMergeTree(sign)` | 用成对行抵消旧记录 | 顺序错就抵消不掉 |
| `VersionedCollapsingMergeTree(sign,ver)` | 同上但顺序无关 | 写法更繁琐 |
| `GraphiteMergeTree` | 时序降采样/rollup | 依赖 `graphite_rollup` 配置 |
| **Replicated 子族**:`ReplicatedMergeTree` / `ReplicatedReplacingMergeTree` / `ReplicatedSummingMergeTree` / `ReplicatedAggregatingMergeTree` / `ReplicatedCollapsingMergeTree` / `ReplicatedVersionedCollapsingMergeTree` / `ReplicatedGraphiteMergeTree` | 上述各引擎的多副本版(靠 Keeper 复制) | 依赖 Keeper;建表参数带 `{shard}`/`{replica}` 宏 |
| `SharedMergeTree`(族) | 计算存储分离 | **仅 Cloud 提供** |
| `Log` / `TinyLog` / `StripeLog` | 本地日志、一次性导出 | 无索引、无分区,只能全扫 |
| `Memory` | 小维表、会话内中间结果 | 重启即失 |
| `Set` | `IN` 的半连接集合 | 只存第一列去重值 |
| `Join` | `joinGet` 用的内存哈希表 | 重启即失 |
| `EmbeddedRocksDB` | 嵌入式 KV,做 `IN`/`JOIN` 的查找源 | 依赖 RocksDB,体量受限 |
| `KeeperMap` | 把 Keeper 当小型 KV 读写 | 数据量小,别当正式数据表 |
| `Buffer` | 攒批再刷入目标表 | **可能丢数据** |
| `Null` | 压测基线、只要副作用的落点 | 什么都不存 |
| `Merge` | 同构多表当一张只读表 | 不去重、不保证顺序 |
| `File` / `URL` | 文件/网络即表 | 每次写=整份重写 |
| `Values` | 字面量即表 | 主要以内联形式用 |
| `Alias` | 给一张表起别名(不存数据) | 只是个改名入口 |
| `GenerateRandom` | 按 schema 生成随机行 | 压测/演示;`generateRandom` 表函数的引擎版 |
| `Distributed` | 跨分片的查询入口 | **不存数据** |
| `Kafka` | 消费流实时入湖 | 是队列不是存储 |
| `RabbitMQ` / `NATS` | 从消息队列消费入湖 | 同 `Kafka`:是"门"不是"仓库" |
| `S3Queue` | 把对象存储上的文件按到达顺序当**队列**消费 | 要配 MV 才落库 |
| `Redis` / `MongoDB` | 把外部键值/文档库当表读写 | 查询推到远端 |
| `MySQL` / `PostgreSQL` / `SQLite` / `ODBC` / `JDBC` | 外部表投影 | 查询推到远端,外部大表 JOIN 慢;`SQLite` 引擎在 Cloud 上不可用 |
| `S3` / `HDFS` / `AzureBlobStorage` | 读远端对象/文件存储 | 只读或低效写 |
| `Iceberg` / `DeltaLake` / `Hudi` | 读开放湖表(数据文件 + 元数据/事务日志) | 主要通过表函数/引擎**读**;写能力较新 |
| `View` | 查询时重跑子查询 | 不存数据 |
| `MaterializedView` | **插入触发器** | 建视图前的历史数据不进来 |
| `MaterializedView ... REFRESH` | 可刷新物化视图(按周期重跑整段查询) | 不再需实验开关(见 A.2.7) |
| `LiveView` | `WATCH` 实时推送 | 25.3 文档挂的是 **Deprecated(已废弃)**徽标,不是实验特性 |
| `WindowView` | 时间窗聚合,配 `WATCH` 推送 | 25.3 文档挂的是 **Experimental(实验特性)**,需 `allow_experimental_window_view = 1`;但上游已在拆除它,同样不要在新设计里用 |

除了表引擎,还有一层**数据库引擎**(`CREATE DATABASE ... ENGINE = ...`):默认的 `Atomic`,老式的 `Ordinary`、`Lazy`、`Replicated`,以及把外部库整体映射过来的 `MySQL` / `PostgreSQL` / `SQLite`,与做镜像复制的 `MaterializedPostgreSQL`(仍是**实验特性**,要 `allow_experimental_database_materialized_postgresql`,且 Cloud 上不支持;行为随版本变化,第 30 章)。注意那个名字更顺口的 `MaterializedMySQL` **在 22.8 被标为弃用、24.1 起默认禁用、随后被移除**,**在 25.3 上不应再指望它**。它们和表引擎不是一个层级,别混着数。

选型顺序见第 04 章:先问"要承受什么写、回答什么查、允不允许暂时不一致",再挑引擎。

## A.20 表函数速查

表函数是"用在 `FROM` 里、把外部数据当表读"的函数;同一个名字也能出现在 `INSERT INTO FUNCTION` 的写入侧(`INSERT INTO FUNCTION s3(...) SELECT ...`)。与表引擎的区别是:表函数**不建表**,临时把某个来源投影成表。原理见第 30 章。

| 表函数 | 用途 | 备注 |
|---|---|---|
| `numbers(N[, start, step])` | 生成 `0..N-1` 的一列 | 最常用的造数入口;`numbers_mt` 是并行版 |
| `generateRandom('name Type, ...'[, seed])` | 按给定 schema 造随机行 | 压测/演示 |
| `file(path[, format][, structure])` | 把**服务端**文件当表读 | 路径以服务端文件系统为准 |
| `url(URL[, format][, structure])` | 把 HTTP(S) 资源当表读 | 只读 |
| `s3(url[, ...])` / `s3Cluster(...)` | 读对象存储上的文件 | 凭据用 named collection 更稳 |
| `remote(addr, db, table[, user, pass])` | 读远端节点上的一张表 | 分布式查询的地基;addr 可用集群定义里的主机 |
| `remoteSecure(...)` | `remote` 的 TLS 版 | 端口通常 9440 |
| `cluster(cluster, db, table)` | 读集群里各分片的同名表 | 依赖 `remote_servers` 配置(第 23 章) |
| `clusterAllReplicas(cluster, db, table)` | 读集群**所有副本**上的表 | 能逐台机器看,排查副本差异时有用 |
| `values('header', (v1, v2), ...)` | 把内联字面量当表 | 与 `INSERT ... VALUES` 同一套语法 |
| `format(structure, data)` | 把一段字节按格式解析成表 | 如 `format(JSONEachRow, '{"a":1}')` |
| `input(structure)` | 把查询里跟随的数据当表 | 主要用于 `clickhouse-local` / stdin |
| `merge(db, 'regex')` | 把名字匹配的表当一张只读表 | 等价 `Merge` 引擎;不去重、不保证顺序 |
| `mysql(...)` / `postgresql(...)` | 把外部 MySQL/PG 表当表读 | 需网络与凭据(第 30 章) |
| `jdbc(...)` / `odbc(...)` | 经 JDBC/ODBC bridge 读外部源 | 需先起桥接进程 |
| `executable(script, format[, structure])` | 跑外部程序,把它的 stdout 当表 | **在服务端执行**,注意安全边界 |
| `hdfs(uri, format[, structure])` | 把 HDFS 上的文件当表读 | 需 HDFS 配置 |
| `azureBlobStorage(...)` | 读 Azure Blob 上的文件 | 与 `s3` 同属对象存储族 |
| `iceberg(...)` / `deltaLake(...)` / `hudi(...)` | 把 Iceberg / Delta Lake / Hudi 湖表当表读 | 以读为主;写能力较新(第 30 章 §30.4) |
| `generate_series(start, stop[, step])` | 生成等差序列 | 与 `numbers` 的区别是起点/步长任意,且接受日期时间与 `INTERVAL`(造"连续日期轴"很顺手) |
| `sqlite('file.db'[, 'table'])` | 把 SQLite 文件里的表当表读 | 路径在**服务端**;也可作字典的源 |
| `redis(...)` / `mongodb(...)` | 把 Redis / MongoDB 当表读写 | 需网络与凭据 |
| `zeros(N)` / `null(...)` | 生成 N 行零值 / 一个丢弃写入的空表 | 压测、以及"只要副作用"的落点 |
| `view(SELECT ...)` | 把一段子查询当表(给它起名) | — |
| `dictionary('db.dict')` | 把字典当表读(一行一键值) | 字典定义见第 21 章 |

一点提醒:`cluster` / `clusterAllReplicas` / `remote` / `remoteSecure` 这一族只有在**真正配了集群**时才有数据(依赖 `remote_servers`);否则它们只是"名字在、跑不起来"。配套的 mini-clickhouse 注册了 30 个表函数名,但 `s3`/`hdfs`/`mysql`/`postgresql` 等一调用就抛精确的 `NOT_IMPLEMENTED`(见 A.22 与第 34 章)。

**这张表是"常用"清单,不是穷举。** 表函数的数量随版本在涨,而且其中有几个是较新的能力,本书**只点名、不给细节**——比如列式传输入口 `arrowFlight(...)`(第 30 章 §30.7)、湖表入口 `paimon(...)`(第 30 章 §30.4),以及造测试数据用的 `fuzzJSON(...)`。它们的**存在性、开关名与签名都未逐条核对**,用之前请先当场问实例:

```sql
SELECT name FROM system.table_functions WHERE name ILIKE '%flight%' OR name ILIKE '%paimon%';
```

这条查询同时也是"某个表函数在我这一版到底叫不叫这个名字"的通用答案,比任何速查表都可靠。

## A.21 窗口函数速查

语法骨架,以及三族函数。语义与坑的完整讲法在第 16 章(尤其 §16.7 起的窗口语法与 §16.8、§16.11、§16.12 的帧语义)。

```sql
func(args) OVER (
    [PARTITION BY expr [, ...]]      -- 分组(可选)
    [ORDER BY expr [ASC|DESC] [NULLS FIRST|LAST] [, ...]]   -- 组内排序(可选)
    [ROWS | RANGE BETWEEN <start> AND <end>]                -- 帧(可选)
)
-- 具名窗口,可复用
WINDOW w AS (PARTITION BY ... ORDER BY ...)
```

三族函数:

| 族 | 函数 | 说明 |
|---|---|---|
| 排名族 | `row_number` / `rank` / `dense_rank` / `percent_rank` / `cume_dist` / `ntile(n)` | `row_number` 纯行号;`rank` 并列跳号(1,1,3);`dense_rank` 并列不跳号(1,1,2);后两个是归一化排名与累积分布 |
| 偏移族 | `lagInFrame` / `leadInFrame` / `first_value` / `last_value` / `nth_value` | 取帧内相对位置的值;**默认帧右边界是当前行**,所以 `leadInFrame`/`last_value` 在默认帧下看不到后面的行,要显式扩帧 |
| 聚合族 | `sum` / `avg` / `count` / `min` / `max` / `any` / `argMin` / `argMax` 等 | 任何"可在帧上增量计算"的聚合都能当窗口用;`uniq` 这类"状态不可减"的近似函数**不能**作窗口函数 |

关键语义三条:

- **默认帧**:`OVER (ORDER BY ...)` 等价于 `RANGE BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW`(累计到当前行);`OVER ()` 则是整个分区。想"整个分区一个值"就必须显式写全帧。
- **`ROWS` 与 `RANGE` 的差别只在有并列排序键时暴露**:`ROWS` 按物理行号算边界,`RANGE` 把排序键相等的行算作同一位置。
- **函数名带 `InFrame` 后缀**:在 25.3 上优先用 `lagInFrame`/`leadInFrame`;不带后缀的 `lag`/`lead` 是后来才作为窗口函数加入的(第 16 章测到在 25.6),但**这个版本锚点未能从官方 changelog 复核到**——检索到的信号只是某些镜像构建里缺了这两个函数,不构成上游引入版本的证据,以目标版本实测为准。

`row_number`/`rank`/`dense_rank`/`ntile` 返回 `UInt64`,`percent_rank`/`cume_dist` 返回 `Float64`。

## A.22 这套速查与 mini-clickhouse 的关系

配套的 `mini-clickhouse` 是一个**教学实现**,它的函数、格式、引擎名都是为了讲清机制而裁剪过的。**真实 ClickHouse 里有而它没有的东西远多于它有的**,所以:凡是这份速查里列出、而实现里查不到的名字,就以真实 ClickHouse 的文档为准,不要拿实现去反推 ClickHouse 的行为。

配套实现的注册表规模(类型族、标量函数、聚合函数、表引擎、表函数、格式、设置、system 表各有多少个名字)**唯一出处是第 34 章 §B.2 与 §B.5**——那些数字是快照,会随实现迭代而变,本书刻意不在速查里重复抄录(也不要背)。

**怎么现场查一个名字存不存在?** 与其背清单,不如直接问注册表。配套实现的注册表是"活"的,可以逐个列出来自查:

```python
from minich.functions.base import function_exists            # 函数名在不在(大小写不敏感)
from minich.storages.base import registered_storage_engines  # [(引擎名, 是否废弃, 说明), ...]
from minich.formats.base import registered_formats           # [(格式名, 可读, 可写, 说明), ...]
from minich.settings import registered_settings              # 设置名 → 类型 / 默认值 / 约束
from minich.system.base import system_table_names            # [system 表名, ...]
```

**怎么自己查(真实 ClickHouse 版本):** 不必把这本速查当成纸面清单——`system.functions`、`system.data_type_families`、`system.formats`、`system.table_engines`、`system.settings` 这几张系统表就是"活的自查手册",正文第 27 章 §27.16 列了清单与用法,这里不重复。

一句话原则:**`system.functions` / `system.settings` 里查不到的名字,就是你那个版本不存在的东西**,不要凭"别的数据库有"或"文档里见过"去臆测。

## A.23 小结

- SQL 的写法:`SELECT` 子句顺序有约束,列修饰符(`FINAL`/`COLUMNS`/`APPLY`/`EXCEPT`/`REPLACE`)能省很多手写;`INSERT` 有 `VALUES`/`SELECT`/`FORMAT`/`INFILE` 等写法;`ALTER` 要分清"改元数据"和"改数据(mutation)"两类。
- 函数按族记:算术盯位宽与溢出、比较返回 `UInt8` 且 `NULL` 上用 `<=>`、字符串第一坑是 `length`(字节)与 `lengthUTF8`(码点)、日期时区是列属性、数组 1-based、JSON 分 `visitParam*` 与 `JSONExtract*` 两族。
- 聚合组合器是"基函数 + 后缀"的套壳:`-If`/`-Array`/`-State`/`-Merge`/`-Distinct`/`-OrNull`/`-OrDefault`/`-Resample`/`-ForEach`,可叠加,以 `-State`/`-Merge` 成对使用最典型。`first_value`/`last_value`/`nth_value` **是窗口函数不是聚合函数**。`GROUP BY` 的扩展(`ROLLUP`/`CUBE`/`GROUPING SETS`/`TOTALS`/`ALL`,A.2.6)用类型默认值填"卷起来的"键列,要区分超级聚合行得靠 `GROUPING()`。
- 表函数(A.20)把外部来源临时投影成表:`numbers`/`generateRandom`/`file`/`url`/`s3`/`remote`/`remoteSecure`/`cluster`/`clusterAllReplicas`/`values`/`format`/`input`/`merge`/`mysql`/`postgresql`/`jdbc`/`odbc`/`executable`;窗口函数(A.21)分排名/偏移/聚合三族,帧默认只到当前行。
- 默认值一律以 `system.settings`、`system.merge_tree_settings` 与 `system.settings_changes` 为准;本表里**还标着"未确认"**的项,以及 `max_parallel_replicas` 这类**跨版本会翻转的默认值**,上线前务必自查。(并行副本那两行本轮已经从"未确认"改成有源码依据的定论,别再按旧印象把它当没定论的东西。)
- 这份速查对应的是真实 ClickHouse;配套的 mini-clickhouse 只是它的一个裁剪版子集,查不到的名字以真实版本为准。
