# 附录 D:FAQ 与排错

这份附录按**症状**组织,不按功能。你碰到的从来不是"我想了解 mutation 机制",而是"我 `ALTER DELETE` 了半天数据还在";排错时想找的也不是"聚合函数那一章",而是"结果怎么比预期多"。所以每一条的写法是固定的四段:**症状 → 最可能的原因(按概率排序,每条给判据)→ 怎么确认 → 怎么修**。里面的命令都可直接粘贴执行(表名、库名按你的环境替换),命令的真实性优先于篇幅。

排错有一条总原则:**先确认"现象"本身**,再谈原因。很多"查询慢"最后发现是客户端在等网络、"数据没删"最后发现是查错了表。凡是能用一个 `SELECT ... FROM system.*` 问清楚的,先问。

## D.1 怎么用这份排错表

- 先按症状关键词定位到小节,再从上到下读"最可能的原因"——它们是按概率排的,别跳。
- 每条都附了**判据**:没有判据的原因等于没写,看到"判据成立"再往下走。
- 症状按域分组:查询(D.2)、写入(D.3)、表与存储(D.4)、**物化视图与建模(D.5)**、复制与分布式(D.6)、**集成与数据接入(D.7,即 Kafka / 字典 / 外部库 / 对象存储 / 分布式 DDL)**、权限与连接(D.8)、监控与日志(D.9)、**备份与恢复(D.11)**、**版本升级与兼容(D.12)**;另有 D.10 的"五分钟定位一条慢查询"。
- 命令里的核心自查对象是 `system.*` 表,体系的讲法在第 27 章;定位慢查询的标准流程单独收在 D.10。
- 真到生产事故,先按第 29 章的"常见事故清单"止血,再回这里找根因。

## D.2 查询相关

### 1. 查询很慢

**最可能的原因(按概率):**
1. **扫描量太大**——`WHERE` 没命中分区或主键裁剪,全表扫描。判据:`system.query_log` 里 `read_rows` 远大于返回行数。
2. **读了太多列**——`SELECT *` 在宽表上代价极高。判据:`read_columns` 或 `ProfileEvents.ReadBytes` 很大。
3. **part 太多**——每个 part 都要单独打开。判据:`system.parts` 里 active part 数成千上万。
4. **JOIN 顺序/算法不当**——小表没在右、用了默认 hash join。判据:`EXPLAIN PIPELINE` 里 join 算子。
5. **聚合键太多**——高基数 `GROUP BY` 撑爆哈希表。判据:内存指标飙升。

**怎么确认:**
```sql
SELECT query_duration_ms, read_rows, read_bytes, result_rows,
       memory_usage, ProfileEvents['SelectedParts'] AS parts
FROM system.query_log
WHERE type = 'QueryFinish' ORDER BY event_time DESC LIMIT 20;
```
再对慢查询跑 `EXPLAIN ESTIMATE` 看要读多少 part/granule。

**怎么修:** 把过滤条件对齐分区键与排序键(第 11 章);显式列名替代 `SELECT *`;对小表改用 `LEFT ANY JOIN` 或字典(第 15、21 章);必要时用 projections/物化视图预聚合。

### 2. 查询卡住不动

**最可能的原因:** ①真的在算大聚合(判据:CPU 持续打满);②在等锁(判据:`system.processes` 里 `ProfileEvents` 不涨);③在等副本/远端(判据:网络与 `Distributed` 相关);④socket 断了但服务端还在跑(判据:客户端 `progress` 不再更新)。

**怎么确认:**
```sql
SELECT query_id, elapsed, read_rows, memory_usage, ProfileEvents['SelectedParts'] AS p
FROM system.processes ORDER BY elapsed DESC;
```
反复看 `read_rows` 是否增长:不增长就多半卡在锁或网络。

**怎么修:** 确认是锁就查 `system.processes` 里的 `lock` 情况(第 25 章);确认是误判就 `KILL QUERY WHERE query_id='...' SYNC`(第 09 章);客户端断开可以设 `send_progress_in_http_headers` / 用 `clickhouse-client --progress`。

### 3. `Memory limit exceeded`

**最可能的原因:** ①高基数聚合/排序;②大 JOIN;③`GROUP BY` 键太多;④单条查询上限设得过低。判据:报错信息里带 "would use X bytes"。

**怎么确认:** 先看报错本身——它带 `would use X bytes`,那个 X 就是压垮的算子申请到的峰值。再按"是哪一步吃的"往下定位:
```sql
-- 1) 报错的那条查询:看它的 memory_usage
SELECT query, memory_usage, exception
FROM system.query_log
WHERE type = 'ExceptionWhileProcessing' AND exception != ''
ORDER BY event_time DESC LIMIT 5;
-- 2) 若查询还在跑(不是 OOM 崩掉而是逼近上限),看它此刻吃多少
SELECT query_id, elapsed, memory_usage FROM system.processes ORDER BY memory_usage DESC LIMIT 5;
-- 3) 逐级的阈值各是多少:单查询 / 单用户 / 服务端
SELECT name, value FROM system.settings
WHERE name IN ('max_memory_usage','max_memory_usage_for_user',
               'max_bytes_before_external_group_by','max_bytes_before_external_sort');
SELECT metric, value FROM system.metrics WHERE metric = 'MemoryTracking';
```
判据是"阈值小"还是"查询真的贵":若 `MemoryTracking` 远低于 `max_server_memory_usage` 而单查询就超了,多半是 `max_memory_usage` 设得过低;若两者都贴顶,是查询本身在爆炸。

**怎么修:** 设 `max_bytes_before_external_group_by`/`max_bytes_before_external_sort` 让中间结果溢写磁盘(第 25 章);降低 `max_rows_to_group_by` 或改用近似聚合(`uniq` 替代 `uniqExact`、`quantile` 替代精确);把 `max_memory_usage` 调到与机器匹配;实在不行拆查询。

### 4. `Too many parts`

**最可能的原因:** ①高频小批量插入(最常见的头号原因);②分区键选错,制造了海量小分区;③后台 merge 跟不上。判据:每个分区 active part 数远超正常。

**怎么确认:**
```sql
SELECT partition, count() AS parts, sum(rows) AS rows
FROM system.parts WHERE active GROUP BY partition ORDER BY parts DESC LIMIT 10;
```

**part 家族的错误不止这一句**,别只认 `Too many parts` 这个字面:有的是"**单分区** part 太多"(撞 `parts_to_throw_insert`,直接拒插),有的是"**全表** part 太多"(撞 `max_parts_in_total`);伴随 `system.metrics.DelayedInserts` 不为 0 的那一类其实是**被 throttle**(`parts_to_delay_insert` 在人为拖慢、还没拒);措辞上 `Too many parts`、`TOO_MANY_PARTS`、"Merges are processing significantly slower than inserts" 都是同一族(错误码相同)。判据一律回到上面这条按分区的 `system.parts` 计数。

**怎么修:** 改为批量插入(每批 ≥ 数千行,或上游攒批);重选分区键(别用高基数表达式);临时可 `OPTIMIZE TABLE ... PARTITION ... FINAL` 压一次。注意 `parts_to_throw_insert` 默认 **3000**(23.6 前为 300),`parts_to_delay_insert` 默认 **1000**(旧版曾为 150)——具体以 `system.merge_tree_settings` 为准。

### 5. `TOO_MANY_SIMULTANEOUS_QUERIES`

**最可能的原因:** 并发查询数超过 `max_concurrent_queries`(这是**服务端配置项**,写在 `config.xml` 里,**不在 `system.settings`**——那里放的是会话/查询级设置)。判据:错误码 202。

**怎么确认:**
```sql
SELECT count() FROM system.processes;   -- 当前实际并发
-- max_concurrent_queries 是 config.xml 的服务端项,改在配置文件里查;
-- system.settings 里能查到的是会话级的这两项:
SELECT name, value FROM system.settings
WHERE name IN ('max_concurrent_queries_for_user','max_concurrent_queries_for_all_users');
```

**怎么修:** 提升 `max_concurrent_queries`(在 `config.xml` 的服务端配置里改,需重启/重载;**代码默认 `0` 表示不限,而官方发行版的 `config.xml` 模板预设为 `1000`**——别把这两个数字混为一谈,见附录 A);或在客户端/代理层限流;或用 `max_concurrent_queries_for_user` 给单用户设限(第 25 章)。

### 6. `Timeout exceeded`

**最可能的原因:** ①查询超过 `max_execution_time`;②等锁超时;③分布式查询某分片超时。判据:报错里带超时的具体设置名。

**怎么确认:** `SELECT name, value FROM system.settings WHERE name LIKE 'max_execution_time%' OR name='lock_acquire_timeout';`

**怎么修:** 提高对应超时;把长查询拆小;若是锁问题,查明是谁持锁(第 25 章)。不要盲目把超时调到很大——那往往掩盖了真正"查询本身太贵"的问题。

### 7. 结果不对(聚合)

**最可能的原因:** ①用了 `SummingMergeTree`/`AggregatingMergeTree` 却没在查询侧 `sum()`/`-Merge`,以为写进去就是最终值;②在 `Nullable` 上聚合,`NULL` 被当成 0/被跳过;③`FINAL` 没加,`ReplacingMergeTree` 还没合完;④`uniq` 是近似。判据:结果比精确值偏小或偶发波动。

**怎么确认:** 拿同一份数据分别在全量表和预聚合表算一遍对比;`SELECT count(), uniq(x), uniqExact(x) FROM t` 看近似偏差。

**怎么修:** 预聚合表查询必须配 `sumMerge`/`uniqMerge` 等(第 12、37 章);`ReplacingMergeTree` 按需加 `FINAL` 或 `argMax`;需要精确就换精确函数。

### 8. `JOIN` 结果比预期多

**最可能的原因:** ①右表键不唯一(一个键对多行),这正是 JOIN 的正常语义,常被误当 bug;②`NULL` 在 join key 上的行为与预期不符;③笛卡尔式条件(只写了非等值条件)。判据:行数膨胀倍数 ≈ 右表平均重复度。

**怎么确认:**
```sql
SELECT key, count() FROM right_table GROUP BY key HAVING count() > 1 LIMIT 10;
```
再看 `EXPLAIN` 确认 join 类型是 `INNER`/`LEFT` 还是 `CROSS`。

**怎么修:** 右表先按业务去重(或改用字典,天然一键一值,第 21 章);显式写 `ANY`/`ALL`(第 15 章);确认外连接里 `NULL` 的处理符合预期(`join_use_nulls`)。

### 9. `NULL` 与默认值分不清

**最可能的原因:** ①列非 `Nullable`,外部数据里的"空"被填成了类型默认值(0 或空串);②结果集里 `NULL` 与 `''`/`0` 混在一起看不出;③`join` 未匹配列在 `join_use_nulls=0` 时给默认值而非 `NULL`。判据:本该有 `NULL` 的地方出现 0。

**怎么确认:** `SELECT countIf(col = 0), countIf(isNull(col)), countIf(col = '') FROM t;` 以及 `DESCRIBE TABLE t` 看列是否 `Nullable`。

**怎么修:** 需要区分"空"就把列声明成 `Nullable(T)`(第 03 章);`JOIN` 场景按需设 `join_use_nulls=1`;导入时用 `input_format_null_as_default`/格式选项控制空值的落法(第 08、19 章)。

### 10. 标量子查询报错

**最可能的原因:** ①子查询返回多列(标量子查询只能一列);②相关子查询在本版本不被支持或需改写;③把"返回多行的 IN 子查询"写成了标量位置。判据:报错指向子查询的结构。

**怎么确认:** 单独跑子查询,确认它返回的列数与行数:
```sql
SELECT x FROM (SELECT 1 AS x, 2 AS y);  -- 多列会报错
```

**怎么修:** 标量位置只放一列(用 `LIMIT 1` 或聚合保证单行);多行判定改用 `IN`/`EXISTS`/`JOIN`;万不得已用 `GLOBAL` 前缀处理分布式(第 23 章)。

### 11. 别名与列名冲突

**最可能的原因:** ①`SELECT` 里定义了与某列同名的别名,后续 `WHERE`/`GROUP BY` 里指代不清;②同一层里别名与列名同名。判据:报 "Cyclic aliases" 或结果与直觉不符。

**怎么确认:** 看报错是否点名某个别名;把别名改成不冲突的名字再跑。

**怎么修:** 避免别名与列同名;需要引用别名的地方注意 ClickHouse 别名默认可在 `WHERE`/`GROUP BY` 里使用——它会把别名**内联进任何子句**,起决定作用的是 `prefer_column_name_to_alias`(**默认 0,即别名优先**),语义因此可能与 SQL 标准不同(第 07 章 §07.2);分两层子查询消除歧义。

### 12. `WHERE` 里带函数不走索引

**最可能的原因:** 对主键(排序键)列套了函数,让主键裁剪失效。ClickHouse 的键条件分析**能**沿着单调函数链折叠——所以 `toDate(ts)` 在 `ORDER BY ts` 的键列上**有时**能用上裁剪,`toStartOfHour(ts)`、`toYYYYMM(ts)` 这类保序日期函数同理。但**这条路径脆弱,依赖具体的类型对与版本**:上游为它修过多次,像 `toDate(Date32)`、`toDateTime(DateTime64)` 这类**窄化转换**(因溢出会回绕)是被**故意**取消裁剪资格的。至于 `toString(ts)`,以及最粗也只对上"一整年"范围的 `WHERE toYear(ts) = 2025`,更别指望。判据:扫描量等于全表。

**怎么确认:** 用 `EXPLAIN indexes = 1` 看索引到底用没用上,或用 `EXPLAIN ESTIMATE` 对比两个版本:
```sql
EXPLAIN indexes = 1 SELECT count() FROM t WHERE ts >= '2025-01-01' AND ts < '2026-01-01';
EXPLAIN indexes = 1 SELECT count() FROM t WHERE toYear(ts) = 2025;
```
后一条的 `PrimaryKey`/`Partition` 那两个块里若显示 "Condition ... not applicable"、或估算 part/granule 数接近全表,就说明没裁到。

**怎么修:** 别赌单调函数能不能折叠——把它挪到常量侧,写成对**原始列**的**区间条件**(`ts >= ... AND ts < ...`)才稳定;确需按表达式过滤,考虑**物化列**(把表达式存成一列并放进排序键)或数据跳过索引。函数被折叠的**确切生效条件未确认**,以 `EXPLAIN` 实测为准(裁剪原理见第 11 章,`EXPLAIN` 怎么读见第 24 章 §24.8–§24.9)。

### 13. `ORDER BY` 特别慢

**最可能的原因:** ①排序量大于内存,退化成外存排序;②`ORDER BY` 的列不是读取顺序,无法省去重排;③没有 `LIMIT` 兜底。判据:内存指标与 `ProfileEvents` 里的外部排序事件。

**怎么确认:**
```sql
EXPLAIN PLAN SELECT ... ORDER BY big_col;
SELECT name, value FROM system.settings WHERE name = 'max_bytes_before_external_sort';
```

**怎么修:** 让 `ORDER BY` 与表排序键一致(`optimize_read_in_order`,第 24 章);加 `LIMIT`;设 `max_bytes_before_external_sort` 让溢写可控;对 Top-N 用 `LIMIT n BY`(第 16 章)。

### 14. `FINAL` 特别慢

**最可能的原因:** `FINAL` 会对涉及的每个 part 做读时合并,part 越多越慢;在 `ReplacingMergeTree` 上它等于"在线做本该后台做的合并"。判据:去掉 `FINAL` 后快很多。

**怎么确认:** `EXPLAIN ESTIMATE SELECT ... FROM t FINAL` 看读的 part 数;对比有无 `FINAL` 的耗时。

**怎么修:** 先去重写入、让后台尽量合完;把 `FINAL` 限制在已合并的小范围(配合分区裁剪);用 `argMax` 手写"取最新"替代 `FINAL`;或考虑 `do_not_merge_across_partitions_select_final` 等设置(确切行为以自检为准)。详见第 28 章。

### 15. `DISTINCT` 特别慢

**最可能的原因:** ①基数极高,去重哈希表撑爆内存;②`SELECT DISTINCT *` 把宽行整体作为键;③本可用 `GROUP BY` + 近似去重却用了精确去重。判据:内存飙高、`uniqExact` 慢。

**怎么确认:** `EXPLAIN PLAN SELECT DISTINCT ...`;`SELECT uniq(x), uniqExact(x) FROM t` 比较开销。

**怎么修:** 只 `DISTINCT` 必要的列;高基数场景用 `uniq`/`uniqCombined`(近似)替代精确;把去重交给 `GROUP BY` + 聚合函数(第 12 章)。

### 16. 单条查询本身没错,但集群整体慢

**最可能的原因:** 后台 merge/mutation 抢了资源;有大量并发查询互相挤占。判据:`system.metrics` 里后台任务数长期贴顶,`system.processes` 行数很多。

**怎么确认:**
```sql
SELECT value FROM system.metrics WHERE metric = 'BackgroundMergesAndMutationsPoolTask';
SELECT count(), sum(memory_usage) FROM system.processes;
```

**怎么修:** 调后台池大小/合并限流(第 18、25 章);给关键查询设更高优先级或独立用户配额(第 25 章)。

### 17. 时间"差 8 小时"(时区问题)

这是可观测性平台的头号坑——日志、追踪、指标来自不同系统、用不同时区生成,一入库就可能整体平移。**它尤其该在附录 F 的场景里第一时间想到。**

**最可能的原因:** ①`DateTime` 的**时区是列类型的一部分**(`DateTime('Asia/Shanghai')`),同一串内部时间戳在不同时区列里会被渲染成不同的墙上时间;②**写入与读出用了不同口径**:`date_time_input_format` 默认 `basic`,它只认 `YYYY-MM-DD hh:mm:ss` 这种不带时区偏移的写法、按**列/服务端时区**解释字符串,源系统若按别的时区生成,就会整体差几个整数小时(`best_effort` 能认带偏移或 `Z` 的写法);③`Date` / `DateTime` / `DateTime64` 混用,或顺手 `toTimeZone` 把值改来改去;④数据其实没错,只是**客户端渲染**用的时区不同。

**怎么确认:** 把**与时区无关的时间戳**和**墙上时间**分开看——时间戳对、墙上时间差,就是时区问题;连时间戳都差,才是数据本身写错了。
```sql
SELECT now(), timezone(), toTimeZone(now(), 'UTC') AS utc_now, toUnixTimestamp(now());
SELECT ts, timeZone(ts) AS col_tz FROM observability.logs LIMIT 1;   -- 看列自带的时区
SELECT name, value FROM system.settings WHERE name ILIKE '%timezone%';   -- 会话/服务端时区设置
SELECT name, value FROM system.settings WHERE name = 'date_time_input_format';
```

**怎么修:** 统一口径——**存 UTC、展示时再转**:入库一律用 `DateTime('UTC')`(或全链路统一某个时区),跨系统比对前先 `toTimeZone` 对齐;写入侧确认 `date_time_input_format` 与源系统一致("差 8 小时"最常见就是这里不一致);`DateTime64` 别丢精度。语义(时区是列属性)见第 17 章。

### 18. 结果数值对不上(精度丢了 / 数字回绕了)

这是本书自己标榜的头号陷阱——`Decimal(P,S)` 的标度规则,老手也常栽跟头。上面第 7 条只覆盖"聚合结果不对",这一条覆盖**算出来的数不对**。

**最可能的原因:** ①`Decimal(P,S)` 的**标度规则**被想当然:`Decimal(9,2) * Decimal(9,2)` 的结果是 `Decimal(9,4)`——**标度(小数位)相加,宽度却纹丝不动**,于是很容易撞上溢出检查;②`Decimal` 与整数混合运算时忘了"整数按标度为 0 处理",结果宽度/标度跟着那个 `Decimal` 走;③整数运算**在结果类型上回绕**:算术会"进一档"提升类型,可到顶之后仍按同宽度回绕,而显式 `CAST`/`toXxx` 超范围时按位截断;④拿 `Float` 当钱用,累积误差被当成"算错了"。判据:数值恰好差一位小数或一个数量级,或出现符号突跳的"天文数字"。

**怎么确认:** 用 `toTypeName()` 把**每一步的结果类型**打出来——`Decimal` 的规则是"标度相加、宽度取自入参",整数则看有没有提升、有没有封顶:

```sql
SELECT toTypeName(CAST(1.5, 'Decimal(9,2)') * CAST(1.5, 'Decimal(9,2)'));  -- Decimal(9, 4)
SELECT toTypeName(toUInt8(200) + toUInt8(200));        -- UInt16:算术进一档,不回绕
SELECT toUInt64(18446744073709551615) + toUInt64(1);   -- 0:位宽封顶后回绕
```

**怎么修:** 别去猜类型——**显式扩宽**。`Decimal` 相乘前先 `CAST` 到足够宽的类型(`Decimal(38, ...)` 或 `toDecimal128`);整数求和若可能超出当前宽度,先 `toInt128`/`toUInt128`(或转 `Decimal`)再算;钱一律用 `Decimal`,不用 `Float`。标度怎么算、什么时候报 `Scale is out of bounds.`、溢出怎么检查,见第 03 章 §03.4;整数提升与回绕见第 03 章 §03.2。

## D.3 写入相关

### 1. 插入慢

**最可能的原因:** ①每批行太少(单行插入是最坏情况);②每个批次落成一个新 part,合并跟不上;③`INSERT SELECT` 被源查询拖住;④同步写入 `Distributed` 表,各分片串行确认。判据:写入速率与批大小正相关。

**怎么确认:** 看 `system.parts` 的 part 增长速度与 `system.metrics` 的 `DelayedInserts`。

**怎么修:** 客户端攒批(数千行/批)或用 `async_insert`;`INSERT SELECT` 拆成多段;写 `Distributed` 时用本地表先落盘(第 23 章);参考第 08 章的写入手册。

### 2. 插入被拒(part 太多)

**最可能的原因:** 单分区 active part 数超过 `parts_to_throw_insert`(默认 3000,23.6 前 300)。判据:报错 "Too many parts (N). Merges are processing significantly slower than inserts."

**怎么确认:** 同 D.2『Too many parts』里的 `system.parts` 查询。

**怎么修:** 减少插入频率、增大批量;检查分区键是否制造小分区;临时 `OPTIMIZE` 压合并;必要时(理解代价后)调高阈值。

### 3. `INSERT` 后立刻查不到

**最可能的原因:** ①写的是 `Distributed` 表,数据还在某分片、查询却打在本地表;②写了 `Buffer` 表,还没刷入目标表;③`async_insert` 尚未落盘;④查错了库/表。判据:稍等片刻或换查询目标就出现了。

**怎么确认:** `SELECT count() FROM system.parts WHERE table='t' AND active;` 再对比两张表(`Buffer` 与其目标表)的行数。

**怎么修:** `Distributed` 上查询要落在分布式表;`Buffer` 表把"目标表"也纳入查询(或改架构);`async_insert` 下等 `wait_for_async_insert` 落地(第 08 章)。

### 4. 数据重复

**最可能的原因:** ①客户端重试导致同一批写了两遍(非复制表不会自动去重);②`ReplacingMergeTree` 还没合完,查到了两个版本;③上游本身发了两次。判据:重复行内容完全一致、时间戳接近。

**怎么确认:** `SELECT count(), count(DISTINCT key) FROM t;` 或按业务唯一键分组找重复。

**怎么修:** 复制表打开 `insert_deduplicate`(默认 1,按块去重,第 22 章);写入链路加幂等键;`ReplacingMergeTree` 用 `FINAL`/`argMax` 取最新(第 04 章)。

### 5. 格式解析失败

**最可能的原因:** ①分隔符/引号与格式不匹配;②行尾符(CRLF)问题;③字段数不一致;④编码不是 UTF-8。判据:报错指出具体行号与字段。

**怎么确认:** 取出错行附近的数据单独用 `clickhouse-local` 或 `INSERT ... FORMAT` 试。

**怎么修:** 换更稳的格式(如 `JSONEachRow` 容错更好);设置 `format_csv_delimiter`、`input_format_skip_unknown_fields`、`input_format_allow_errors_num` 等(第 08、19 章);统一换行与编码。

### 6. 类型转换失败

**最可能的原因:** 输入字符串无法按目标类型解析(如把 `'abc'` 写进 `UInt32`)。判据:报错定位到列与值。

**怎么修:** 用 `CAST`/`toInt64OrNull` 在 SQL 层做容错转换;或调整 `input_format_null_as_default`;或让上游保证类型。宁可显式转换,不要依赖隐式。

### 7. `NULL` 进非可空列

**最可能的原因:** 目标列不是 `Nullable`,而输入是 `NULL`/空值,被替换成类型默认值(0/空串),或直接报错(取决于设置)。判据:`DESCRIBE TABLE` 显示列非 `Nullable`。

**怎么修:** 把列声明为 `Nullable(T)`,或让导入把空值当默认值(`input_format_null_as_default=1`,确切默认以自检为准);更推荐在业务上保证非空,避免全表 `Nullable`。

## D.4 表与存储

### 1. 磁盘涨得比预期快

**最可能的原因:** ①没设 TTL,历史数据无限增长;②`ReplacingMergeTree` 未合并时数据膨胀;③mutation/`ALTER` 留下未清理的旧 part;④一个宽表开了很多跳过索引与 projections;⑤低效 codec。判据:按表排序的磁盘占用。

**怎么确认:**
```sql
SELECT table, sum(bytes_on_disk) AS bytes, sum(rows) AS rows, count() AS parts
FROM system.parts WHERE active GROUP BY table ORDER BY bytes DESC LIMIT 20;
```

**怎么修:** 建 TTL(第 18 章);开 `ttl_only_drop_parts` 让 TTL 走整分区 drop(默认以自检为准);检查 `system.mutations` 的残留;审视索引与编码(第 20 章)。

### 2. 磁盘满

**最可能的原因:** ①日志/临时文件堆积;②freeze 备份残留;③mutation 期间新旧 part 并存;④系统表(`query_log`/`text_log`/`trace_log`)无上限增长。判据:报错 `No space left on device`。

**怎么确认:**
```sql
SELECT name, free_space, total_space FROM system.disks;
SELECT database, table, sum(bytes_on_disk) FROM system.parts WHERE active GROUP BY database, table ORDER BY sum(bytes_on_disk) DESC LIMIT 10;
```
再查 `system.query_log` 等系统表的大小与 TTL。

**怎么修:** 给系统表加 TTL/清理;`SYSTEM UNFREEZE` 清残留;清理临时目录;短期可删可重建的旧分区(第 29 章)。

### 3. part 数量异常

**症状:** 某表 part 数远超其他表。**最可能的原因:** 分区键高基数 + 小批量写入。判据:`system.parts` 里该表 active part 数与分区数都异常高。

**怎么修:** 见 D.2『Too many parts』。

### 4. `OPTIMIZE` 没用

**最可能的原因:** ①对 `ReplacingMergeTree` 执行普通 `OPTIMIZE`,期望它去重,但只有 `OPTIMIZE ... FINAL` 才做最终合并;②对超出 merge 上限的大 part,`FINAL` 也合不动;③`OPTIMIZE` 是异步的,命令返回不代表完成。判据:`system.merges` 里还有任务,`system.parts` 未变少。

**怎么确认:**
```sql
SELECT * FROM system.merges WHERE table = 't';
SELECT count(), sum(rows) FROM system.parts WHERE table='t' AND active;
```

**怎么修:** `ReplacingMergeTree` 去重用 `OPTIMIZE TABLE t FINAL`(并对已合并范围才有效);大表别指望 `OPTIMIZE` 替代落盘设计;必要时用 `Optimize ... DEDUPLICATE BY`(第 09、18 章)。

### 5. `ALTER DELETE` 不生效

**最可能的原因:** ①mutation 是异步的,命令返回时还没跑完;②mutation 失败了(判据:`system.mutations` 里 `latest_fail_reason` 非空);③删的是"当前查询快照",旧快照/别的副本上还在。判据:`is_done` 为 0 或有失败原因。

**怎么确认:**
```sql
SELECT table, mutation_id, command, is_done, latest_fail_reason
FROM system.mutations WHERE table = 't' ORDER BY create_time DESC;
```

**怎么修:** 等 mutation 跑完或查失败原因(第 18 章);不要在常规流程里用 `ALTER DELETE` 删大批量数据——那是 mutation,大表上非常贵,该用分区或 `ReplacingMergeTree` 的墓碑列。

### 6. TTL 没删数据

**最可能的原因:** ①TTL 只在**合并**时生效,没有合并就不删;②TTL 表达式基于 `DateTime`,而列是 `Date`/字符串时算错;③`ttl_only_drop_parts` 与 TTL 语义配合不当,本该整分区 drop 的走了逐行删。判据:`system.parts` 里过期数据仍在。

**怎么确认:** `SHOW CREATE TABLE t` 看 TTL 表达式;`SELECT * FROM system.merge_tree_settings WHERE name LIKE 'ttl%';`

**怎么修:** 确保有合并发生(数据量小的表可能长期不合并,可手动 `OPTIMIZE ... FINAL` 验证);修正 TTL 表达式的类型;理解 TTL 删除是"合并副产品"而非定时任务(第 18 章)。

### 7. 表结构改不了

**最可能的原因:** ①改了排序键/分区键等**物理结构**(这类改动受限,常只对新 part 生效或干脆不支持);②集群里某副本的 DDL 队列卡住(复制表);③列被投影/物化视图依赖。判据:报错说明原因,或 `system.distributed_ddl_queue` 有未完成任务。

**怎么确认:**
```sql
SELECT * FROM system.distributed_ddl_queue WHERE status != 'Finished' LIMIT 10;
SELECT * FROM system.mutations WHERE is_done = 0 AND latest_fail_reason != '';
```

**怎么修:** 认清"哪些改动是元数据级、哪些要重写数据"(第 09 章);复制表先让 DDL 队列排空;结构大改优先"建新表 + 灌数据 + 换名"。

### 8. `SHOW CREATE` 与预期不符

**最可能的原因:** ①引擎参数被省略(如 `ReplicatedMergeTree` 里磁盘路径没显示);②`ORDER BY`/`SETTINGS` 里被规范化;③看的是 `Distributed` 表而非本地表。判据:`SHOW CREATE` 输出与手写不一致的部分通常无害。

**怎么确认:** `SELECT engine_full, sorting_key, partition_key, primary_key FROM system.tables WHERE name='t';`

**怎么修:** 以 `system.tables`/`system.columns` 的**结构化字段**为准,别死抠 `SHOW CREATE` 的字符串(第 06、27 章)。

## D.5 物化视图与建模

这一组问题有个共同点:**症状出现在汇总表上,根因却几乎总在"写入路径"或"当初的建表决定"上。** 机制在 `第 13 章`,方法论在 `第 14 章`。

### 1. 物化视图建好了,目标表却几乎是空的

**最可能的原因:** ①**建视图之前**已经写进源表的数据不会自动进来(头号根因);②建视图之后往源表写的数据其实很少;③`TO` 目标表是 `SummingMergeTree`/`AggregatingMergeTree`,**同一分组的多行要等后台合并才收敛**,刚插完看不到"汇总后"的样子;④写入的列名与视图 SELECT 的**输出列名对不上**,匹配不上就被忽略。

**怎么确认:** 分别数源表与目标表的行数,再按时间切一刀,看"建视图那一刻"前后有没有断崖。目标表用 `SELECT ... FINAL` 或 `OPTIMIZE TABLE ... FINAL` 强制合并后再看一次,如果数字变了,就是 ③。

**怎么修:** `TO` 形式下没有 `POPULATE` 可用,正确做法是**先建视图、再手动把历史灌一遍**:`INSERT INTO 目标 SELECT ... FROM 源`(选在停写窗口或上游可重放时做)。细节见 `第 13 章 §13.3` 与 `§13.7`。

### 2. 汇总表的数字比源表大,或者明细被重复计算

**最可能的原因:** ①`-State` 用错了 —— 中间层存的是**结果**而不是**状态**,下一层又把它当输入聚合了一遍;②物化视图链上的**中间层不是 `AggregatingMergeTree`**,后台合并没有把同一分组的状态行收敛掉;③同一批数据被重复投递,而聚合型 MV 的去重没有生效。

**怎么确认:** 看中间层表的列类型 —— 应该是 `AggregateFunction(...)` 或 `SimpleAggregateFunction(...)`,**不是** `UInt64`/`Decimal` 这类结果类型。再查 `system.parts` 里中间层是不是长期有大量小 part 没合并。

**怎么修:** 中间层一律存状态(`sumState`/`uniqState`,查询侧用 `sumMerge`/`uniqMerge`);链上每一级的引擎都用 `AggregatingMergeTree`;去重看 `第 13 章 §13.5`。**`avg`、`uniq`、`quantile` 这类函数存结果再聚合一定是错的**,`sum`/`max`/`min` 只是侥幸能对。

### 3. 物化视图链某一级不更新了

**最可能的原因:** ①上游那一级的源表写不进去(MV 是**触发器**,没有插入就没有触发);②链上的顺序与预期不一致 —— 传播不是事务性的;③中间表被 `TRUNCATE`/`DROP` 过,视图还在但目标没了。

**怎么确认:** 逐级比较行数与最近更新时间;查 `system.tables` 里每一级 MV 的定义与目标表是否都还在。

**怎么修:** 把链重建一遍(先建下游、再建上游,或反过来一次性建好),然后手动回填各级历史数据。链的顺序语义见 `第 13 章 §13.6`。

### 4. "我的查询很慢,是不是该建个物化视图?"

**先别急着建。** 先分清三种加速手段的分工:只读几列、过滤窄,靠的是**排序键 + 跳数索引**(`第 11 章`);同一条聚合查询被反复跑、且代价集中在聚合上,才轮到**投影**(表内部,优化器会自动用,只服务单表查询)或**物化视图**(独立表,通常要显式查)。如果是 JOIN 慢,那多半是模型问题(`第 15 章`),不是缺预聚合。

**怎么修:** 按"先改排序键 → 再考虑投影 → 最后才上物化视图"的顺序试,因为三者的**运维代价依次上升**:改排序键要重建表,加投影要多写一份数据,物化视图则要在写入路径上永久多跑一条 SELECT。选型见 `第 14 章 §14.7` 与 `第 13 章 §13.9`。

### 5. 排序键选错了,还能改吗

**能,但很贵。** 改 `ORDER BY` 是对**存量数据**的物理重排,不是元数据改动 —— 它会重写整张表。加列、改注释是秒级;改类型、改排序键是"重建"。所以建模时把排序键当**一次性决定**来对待,理由是 `第 14 章 §14.3` 讲的那套:它同时决定稀疏索引的裁剪能力和压缩率。

**怎么修:** 建一张新表(新排序键)→ 把数据灌进去 → 用 `EXCHANGE TABLES` 原子切换 → 观察无误后删旧表。整个过程见 `第 14 章 §14.10` 与 `第 09 章 §09.9`。

### 6. 给表加了一列,历史数据的值是 0 / 空串,不是 NULL

**这不是 bug,是设计。** ClickHouse 用**类型的默认值**表示"缺失",而不是 `NULL`;加列时旧数据填的就是默认值。三种默认值语义(`DEFAULT`/`MATERIALIZED`/`ALIAS`)的差别见 `第 06 章`,而"要不要用 `Nullable`"本身是个要付性能代价的决定。

**怎么修:** 需要"区分缺失"就显式声明 `Nullable` 并接受它的开销;不需要就保持默认值,但要在下游报表里把它解释清楚。相关取舍见 `第 03 章` 与 `第 14 章 §14.5`。

## D.6 复制与分布式

### 1. 副本延迟

**最可能的原因:** ①网络慢或副本宕;②Keeper 慢;③该副本 `fetch` part 慢。判据:`absolute_delay` 持续增长。

**怎么确认:**
```sql
SELECT database, table, is_leader, absolute_delay, queue_size,
       inserts_in_queue, merges_in_queue
FROM system.replicas ORDER BY absolute_delay DESC;
```

**怎么修:** 查网络与 Keeper(第 22 章);加大 fetch 线程;必要时 `SYSTEM RESTART REPLICA`。

### 2. 副本只读

**最可能的原因:** 该副本与 Keeper 失联(最常见),或被显式设为只读。判据:`system.replicas.is_readonly` 为 1。

**怎么确认:** `SELECT database, table, is_readonly, zookeeper_exception FROM system.replicas WHERE is_readonly = 1;` 以及 `SELECT * FROM system.zookeeper WHERE path='/';`(验证 Keeper 可达)。

**怎么修:** 恢复与 Keeper 的连接;`SYSTEM RESTART REPLICA t`;检查 Keeper 日志与磁盘。

### 3. Keeper 连不上

**最可能的原因:** ①Keeper 进程挂/被 OOM;②网络或端口不通;③Keeper 所在盘满导致会话失效。判据:多条复制表同时只读。

**怎么确认:** `SELECT * FROM system.zookeeper WHERE path = '/';`;查 Keeper 进程与端口连通性。

**怎么修:** 拉起/扩容 Keeper 集群;清理 Keeper 磁盘;配置里确认 `zookeeper` 段(第 22 章)。

### 4. `Distributed` 表查不到数据

**最可能的原因:** ①本地表为空,数据其实在别的分片;②集群配置里分片指向的主机名解析不到;③写入走的是本地表、查询走的是分布式表,而分片键让数据不在你预期的分片。判据:各分片本地表行数之和 ≠ 分布式表行数。

**怎么确认:**
```sql
SELECT * FROM system.clusters WHERE cluster = '<你的集群>';
SELECT hostName(), count() FROM clusterAllReplicas('<集群>', db.t);
```

**怎么修:** 核对集群配置(第 23 章);直接查各分片本地表定位数据在哪;确认写入路径。

### 5. `GLOBAL JOIN` 结果不对

**最可能的原因:** 在分布式场景下用了普通 `JOIN`/`IN`,右表只在本地分片可见,导致 join 丢行/多行;改用 `GLOBAL` 才正确。判据:分片间右表数据不一致时结果错误。

**怎么确认:** 对比 `JOIN` 与 `GLOBAL JOIN` 的结果行数;查右表是否在所有分片一致。

**怎么修:** 分布式场景优先 `GLOBAL JOIN`/`GLOBAL IN`(第 23 章);或把右表改成字典(天然全局,第 21 章)。

## D.7 集成与数据接入

这一节补上整份排错表最大的一个洞:**与外部系统打交道时出的问题**。生产上的痛有一大半发生在这里——不是 SQL 写错,而是"消息没进来""字典没加载""DDL 卡住了"。它们跨越第 21、22、30 章的内容,但排错时你想要的是一次能照着走的流程。

### 1. Kafka 表不消费 / 消息像丢了

**最可能的原因(按概率):** ①**没有配物化视图**——`Kafka` 引擎表是"门"不是"仓库",不配 MV 就没有任何东西消费它,消息一直堆在 topic 里(判据:目标表行数不涨);②MV 建得晚,建之前到达的消息不会回填(判据:MV 的建立时间晚于问题时段);③消费组/offset 行为不符预期(同 group 多消费者会瓜分分区,或 offset 被重置导致重复/跳过);④解析失败的行被跳过或拖垮整批。

**怎么确认:**
```sql
-- Kafka 引擎表不在 system.parts 里,这是正常的:它不存数据
SELECT database, table, engine FROM system.tables WHERE engine = 'Kafka';
-- 目标表在不在涨?
SELECT count() FROM <目标表>;
-- 消费相关指标(名字随版本变化,以 system.metrics / events 为准)
SELECT metric, value FROM system.metrics WHERE metric ILIKE '%Kafka%';
SELECT event, value FROM system.events  WHERE event  ILIKE '%Kafka%';
```

**怎么修:** 给源表配一个 `TO <目标表>` 的 MV(完整链路见 §F.5.1);核对 `kafka_group_name`(换组名等于换起点,具体取决于 offset 策略);解析失败时用 `kafka_handle_error_mode='stream'` 把坏行送进 `_error` / `_raw_message` 虚拟列,而不是让整批挂掉;重复消费要靠目标表去重(复制表的 `insert_deduplicate`,或 `ReplacingMergeTree`)或让上游带幂等键。`kafka_handle_error_mode` 的**默认值是 `default`**——解析失败时直接抛异常,而且会让这张源表卡住,而不是自动跳过;想让它把坏行连同错误信息送进 `_error` / `_raw_message` 两个虚拟列,得把它设成 `stream`。(还有一档 `dead_letter_queue`,把错误写进内置死信表,但要 25.8 起才有,25.3 上用不到。)

### 2. 字典加载失败 / `dictGet` 报错

**最可能的原因:** ①源(外部库、HTTP、文件)连不上或超时;②`LIFETIME` 到期后重载失败,字典进入不可用;③`SOURCE`/`LAYOUT` 与键、取值类型不匹配;④**静默失败**——初始加载就没成功,但直到有人 `dictGet` 才暴露。

**怎么确认:**
```sql
SELECT name, status, last_successful_update_time, last_exception, element_count
FROM system.dictionaries;
```
`status != 'LOADED'` 或 `last_exception` 非空,就是它。

**怎么修:** 修源连通性与凭据;`SYSTEM RELOAD DICTIONARY <d>` 手动重载并看新的报错;必要时 `SYSTEM RELOAD DICTIONARIES`;核对 `SOURCE`/`LAYOUT`/`LIFETIME` 四要素(第 21 章)。记住字典加载是**后台**行为,别指望"建完立刻可用"。

### 3. `MaterializedPostgreSQL` 卡住(以及 `MaterializedMySQL` 已不再可用)

**先确认引擎还在不在。** 名字更顺口的 `MaterializedMySQL` **已经不在了**:它在 22.8 被标为弃用、24.1 起默认禁用,随后被移除(上游 PR #73879),所以**在 25.3 上 `CREATE DATABASE ... ENGINE = MaterializedMySQL` 会直接报 "Unknown database engine"**。做 MySQL 的持续同步,现在的路径是:轻量场景用 `mysql()` 表函数或 `MySQL` 表引擎按需拉取;要持续同步就用**外部 CDC**(如 Debezium、Flink CDC)把变更写进 ClickHouse。`MaterializedPostgreSQL` 则是**仍在的实验特性**——要显式打开 `allow_experimental_database_materialized_postgresql` 才能建库,且 Cloud 上不支持、行为随版本变化。

**`MaterializedPostgreSQL` 卡住的常见原因:** ①复制账号权限不足(需要相应复制权限与复制槽);②源端 WAL 被清理,复制点丢失;③网络断开后没有自动恢复;④源端改了结构,目标端不兼容而停下。

**怎么确认:**
```sql
SELECT name, engine FROM system.databases;                 -- 找用该引擎的库
SELECT database, table FROM system.tables WHERE database = '<该库>' LIMIT 10;
```
再看服务端日志(若开了 `text_log`,见 D.9)里该库相关的报错。

**怎么修:** 补齐复制权限、重建复制槽、修好网络后重挂(`DETACH`/`ATTACH DATABASE` 或重建)。细节以官方文档与你的版本实测为准。

### 4. S3 / 对象存储报错

**最可能的原因:** ①凭据错或过期(判据:Access Denied);②endpoint/region/路径风格配错(判据:连接超时、301、`No such key`);③把密钥直接写进每一条 DDL 而不是用 named collection;④私有桶缺 `ListBucket` 之类权限。

**怎么确认:**
```sql
SELECT * FROM s3('https://<endpoint>/<bucket>/<key>', 'CSV', 'a UInt8');  -- 单独试一条
SHOW NAMED COLLECTIONS;
SELECT * FROM system.disks;      -- 对象存储挂成 disk 时
SELECT * FROM system.errors WHERE name ILIKE '%S3%';   -- 以 system.errors 可用为准
```

**怎么修:** 用 `CREATE NAMED COLLECTION` 收纳凭据并在 DDL 里引用,**别把密钥写进 SQL 或代码**;核对 endpoint/region 与路径风格(path-style vs virtual-host);备份相关报错先单独跑 `BACKUP ... TO S3(...)` 定位。

### 5. `async_insert` 的困惑(写进去了又查不到 / 没变快)

**最可能的原因:** ①`wait_for_async_insert=1`(默认)时客户端在等服务端攒批落盘,看起来"卡";设成 0 则客户端立刻返回、但数据可能还没落(判据:稍后才出现);②攒批阈值/超时都还没到,小块迟迟不刷;③误以为开了 `async_insert` 就等于"去重"——它只攒批,不去重。

**怎么确认:**
```sql
SELECT name, value FROM system.settings
WHERE name LIKE 'async_insert%' OR name = 'wait_for_async_insert';
-- system.asynchronous_insert_log 记录每次异步插入的落库(需在服务端开启对应日志段):
SELECT status, count() FROM system.asynchronous_insert_log GROUP BY status;
```

**怎么修:** 要"读到自己写"就保持 `wait_for_async_insert=1`;用 `async_insert_max_data_size` / 超时把批调得更合身;真正的去重要靠幂等键或复制表的 `insert_deduplicate`。排错思路见第 08 章。

### 6. `ON CLUSTER` / 分布式 DDL 卡住

**最可能的原因:** ①某个节点的 DDL 队列没跑完(判据:`system.distributed_ddl_queue` 里有非 `Finished` 的条目);②某节点离线或 Keeper 不可达;③同一对象上有冲突的 DDL 锁。

**怎么确认:**
```sql
SELECT entry, host, status, exception_text FROM system.distributed_ddl_queue
WHERE status != 'Finished' ORDER BY query_create_time DESC LIMIT 20;
-- 复制表:等副本把已入队的 DDL 追平
SYSTEM SYNC REPLICA <表>;
```

**怎么修:** 修好离线节点 / Keeper 后重试;确认队列排空再继续(是否有专门的"等待 DDL"命令以官方 `SYSTEM` 文档为准,本书**未确认**);冲突时先分清是"改元数据"还是"改数据"(第 09 章)。核心认知是:**`ON CLUSTER` 是"每个节点各跑一遍",任何一个节点卡住,整条语句就卡住**。

### 7. `Too many open files` / 文件描述符耗尽

**最可能的原因:** ①part 太多——每个 part 的每个列文件都要占一个 fd;②并发查询太多;③进程的 `ulimit -n` 太低。

**怎么确认:**
```sql
SELECT metric, value FROM system.metrics
WHERE metric IN ('OpenFileForRead','OpenFileForWrite');
SELECT count() AS parts FROM system.parts WHERE active;
SELECT count() FROM system.processes;
```
系统层面再数一下进程持有的 fd(`/proc/<pid>/fd` 的条目数)。

**怎么修:** 事前把 `ulimit -n` 调大(服务启动前);减少 part 数量(批量写入、合并小 part、重选分区键)与并发;必要时拆分。这条几乎总是"part 太多"的另一个马甲,修法回到 D.2『Too many parts』。

### 8. `Keeper session expired` / 副本反复进入只读

**最可能的原因:** ①Keeper 侧 GC 压力或负载导致会话超时;②网络抖动;③Keeper 磁盘满或被 OOM。判据:多条复制表**同时**只读,且日志里出现会话过期。

**怎么确认:**
```sql
SELECT database, table, is_readonly, zookeeper_exception FROM system.replicas WHERE is_readonly = 1;
SELECT * FROM system.zookeeper WHERE path = '/';
SELECT metric, value FROM system.metrics
WHERE metric IN ('KeeperAliveConnections','KeeperOutstandingRequests');
```

**怎么修:** 恢复 Keeper(扩容、清盘、修网络);按需调大会话超时;恢复后 `SYSTEM RESTART REPLICA`。这和 D.6 的"副本只读"同源,只是特指"会话过期"这一形态——先看 Keeper 本身,而不是先动副本。

### 9. JOIN 相关 OOM

**最可能的原因:** ①大表放到了右边、又被整份读进内存(判据:报错带 `Memory limit exceeded` 且 SQL 里有 `JOIN`);②join 键高基数、匹配数炸开;③多表 join 层层放大。

**怎么确认:**
```sql
SELECT query, memory_usage FROM system.query_log
WHERE type = 'ExceptionWhileProcessing' AND query ILIKE '%JOIN%'
ORDER BY event_time DESC LIMIT 10;
```

**怎么修:** 把**小表放右边**;大表 join 考虑显式指定可溢写的 `join_algorithm`——`'grace_hash'`(分桶溢写)与 `'partial_merge'`(排序归并)**都是存在的算法名**——并设 `max_bytes_in_join`、选 `join_overflow_mode`;右表先在子查询里聚合/去重降低行数;能用字典就别用 join(第 15、21 章)。注意 `join_algorithm` 的**默认值是 `direct,parallel_hash,hash` 这一串候选集**(见 A.16),**不是单值**,所以要写成 `SETTINGS join_algorithm = 'grace_hash'` 才会真的切过去;而"让 hash join 在超阈值时自动转 grace hash"的 `max_bytes_before_external_join` **在 25.3 上并不存在**——它是很晚(官方记为 26.4)才引入的开关,25.3 的 `system.settings` 里根本没有这一项(同见 A.16)。它和 D.2『Memory limit exceeded』是同一族,这里只是把范围收到 join。

### 10. Query cache 不命中

**最可能的原因:** ①**根本没开** `use_query_cache`(默认 0);②查询含**非确定函数**(`now()`/`rand()`),按设计不可缓存;③查询文本或设置变了(缓存键对文本很敏感);④相关的表发生了写入,条目失效。

**怎么确认:**
```sql
SELECT name, value FROM system.settings
WHERE name LIKE 'query_cache%' OR name = 'use_query_cache';
SELECT * FROM system.query_cache;                 -- 现存条目
```
再在查询日志里看这条查询的缓存相关 `ProfileEvents` 是否命中。

**怎么修:** 会话/查询级 `SETTINGS use_query_cache = 1`(可另给 `query_cache_ttl`);把非确定函数提出去或改成可缓存的写法;保持查询文本稳定;理解"表一写就失效"是设计而非 bug(第 28 章)。

## D.8 权限与连接

### 1. `ACCESS_DENIED`

**最可能的原因:** 当前用户没有对应表的 `SELECT`/`INSERT` 权限;或行级策略拦掉了。判据:错误码 497。

**怎么确认:** `SHOW GRANTS FOR <user>;` 与 `SELECT * FROM system.grants WHERE user_name = '<user>';`

**怎么修:** `GRANT` 所需权限;核查 `system.row_policies`(第 26 章)。

### 2. 连接被拒

**最可能的原因:** ①服务未启动;②`listen_host` 只绑了 `127.0.0.1`;③防火墙;④端口被占。判据:`Connection refused`。

**怎么确认:** 服务端进程状态;`ss -lntp | grep -E '8123|9000'`;从客户端 `telnet <host> 8123`。

**怎么修:** 启动服务;配置 `listen_host`;放通端口(注意安全边界,第 26 章)。

### 3. 认证失败

**最可能的原因:** ①密码错;②用户不存在;③`default` 用户被限制来源;④客户端缓存了旧凭据。判据:`Authentication failed`。

**怎么确认:** `SELECT name, auth_type FROM system.users;`

**怎么修:** 重设密码/新建用户;核对 `<users.xml>` 与 SQL 用户的一致性(第 26 章)。

### 4. 端口不通

**最可能的原因:** HTTP 端口(默认 `8123`)与原生端口(默认 `9000`)用错;或走了 TLS(默认 `8443`/`9440`)。判据:客户端报协议/握手错。

**怎么修:** 确认客户端连的是对的端口与协议(第 02 章);TLS 场景配置 CA。

### 5. 行级策略把该看的行"过滤没了"

**最可能的原因:** 该用户命中了 `CREATE ROW POLICY` 定义的行过滤,只"看得见"一部分行;或策略 `USING` 表达式里引用的列/函数在当前上下文里恒假。判据:同一条查询换一个没挂策略的用户,行数就回来了。

**怎么确认:**
```sql
SELECT name, table, filter, users, roles FROM system.row_policies;
SELECT currentUser();
```

**怎么修:** 核对策略的 `USING`(行可见性)与 `WITH CHECK`(可写性)表达式;需要时补 `GRANT`,或把用户从策略的 `TO`/`roles` 里摘出去(第 26 章)。注意行策略是按**行**过滤而不只是"能否读表",所以症状常是"表能读,但数字偏小"。

### 6. 撞上配额(`QUOTA_EXCEEDED`)

**最可能的原因:** 该用户在时间窗口内的查询数 / 读写行数 / 执行时间用完了 `CREATE QUOTA` 给的上限。判据:错误码 `QUOTA_EXCEEDED`,或错误里点名被触发的配额项。

**怎么确认:**
```sql
SELECT * FROM system.quotas;
SELECT * FROM system.quota_limits;
SELECT * FROM system.quota_usage WHERE quota_key != '';   -- 当前窗口已用多少
```

**怎么修:** 调大或重置该配额;把长/重查询挪到别的窗口,或给关键用户单开一个更宽的配额档(第 25、26 章)。配额按**时间窗口**滚动,"等一会儿就好"有时确是正解——先确认窗口长度再改。

### 7. `KILL QUERY` 杀不掉 / 杀完还在跑

**最可能的原因:** ①命令默认**异步**,发出后查询还在一段时间内收尾(判据:`system.processes` 里该 `query_id` 还在);②`WHERE` 没匹配到任何查询(`query_id` 写错,或那条查询已经结束);③查询卡在不可中断的阶段(阻塞式的 merge / 网络等待);④分布式查询只杀到一个分片上的副本。

**怎么确认:**
```sql
KILL QUERY WHERE query_id = '<id>' SYNC;     -- SYNC 会等到真的停下
SELECT query_id, elapsed FROM system.processes WHERE query_id = '<id>';
SELECT * FROM system.processes ORDER BY elapsed DESC LIMIT 5;   -- 先确认真有这条
```

**怎么修:** 加 `SYNC` 让命令等到结束;先确认 `query_id` 存在;分布式查询按分片分别杀;实在不掉再看是不是卡在锁上(同 D.2『查询卡住不动』,第 25 章)。

## D.9 监控与日志

### 1. `system.query_log` 里查不到刚跑的查询

**最可能的原因:** ①日志**异步刷盘**,刚跑完还没写进来(`system.query_log` 默认就是开的,`flush_interval_milliseconds` 默认 7.5 秒,先 `SYSTEM FLUSH LOGS` 再查);②查询本身失败且只记了 `ExceptionWhileProcessing`;③如果确实被显式关过,才是"没启用";④连错了服务器/库。判据:查老一点的查询能查到,或先刷盘再查。

**怎么确认:**
```sql
SELECT count() FROM system.query_log;
SYSTEM FLUSH LOGS;
SELECT query, type, event_time FROM system.query_log ORDER BY event_time DESC LIMIT 5;
```

**怎么修:** 开 `query_log`(第 27 章);先 `SYSTEM FLUSH LOGS` 再查;注意 `type` 有 `QueryStart`/`QueryFinish`/`ExceptionWhileProcessing` 几种。

### 2. `system.text_log` 是空的

**最可能的原因:** `text_log` 默认**不开启**,必须在服务端配置 `<text_log>` 段(带级别与路径)。判据:`system.tables` 里能看到表但无行,或配置里没这段。

**怎么确认:** `SELECT count() FROM system.text_log;` 与检查 `config.xml` 的 `<text_log>`。

**怎么修:** 配置并开启 `text_log`(第 27、29 章);注意开启它会带来持续写入开销。

### 3. 指标看不懂

**最可能的原因:** 把 `system.metrics`(瞬时值)与 `system.events`/`ProfileEvents`(累计计数)混用——对 counter 直接看绝对值没意义。判据:数值只增不减。

**怎么确认:**
```sql
SELECT metric, value FROM system.metrics WHERE metric LIKE '%Memory%';
SELECT event, value FROM system.events WHERE event IN ('FailedQuery','Query');
```

**怎么修:** 对 counter 采样两次相减算速率;分清"量"与"计数"(第 27 章);用 `system.asynchronous_metrics` 看周期性系统度量。

### 4. 不用开 `text_log`,用 `system.errors` 看"服务端在报什么错"

`system.errors` 是 `system.events` 里那批**错误事件**的带名字版本:每个 `(name, code, remote)` 组合一行(同一个错误码若本地与远端都发生过会出两行,靠 `remote` 区分),含 `name`(错误名)、`code`(数字码)、`value`(自启动以来的**累计**次数,不是速率)、`last_error_time` 与 `last_error_message`。它比翻文本日志好用得多——**`text_log` 默认不开,而 `system.errors` 一直在**。

```sql
SELECT name, code, value, last_error_time, last_error_message
FROM system.errors
WHERE value > 0
ORDER BY value DESC LIMIT 20;
```

**怎么读:** 和 `events` 一样,`value` 是**累计计数**——大不代表现在还在报,要看 `last_error_time` 是不是新的;要"这段时间报了多少",得取两次快照相减。常客有格式解析失败类(如 `CANNOT_PARSE_INPUT_ASSERTION_FAILED`)、`TIMEOUT_EXCEEDED`、`MEMORY_LIMIT_EXCEEDED`、`TOO_MANY_PARTS`、`KEEPER_EXCEPTION` 等(具体 `name` 以你 `system.errors` 里的实际行为准)。定位到某一类后,再回本附录对应小节找原因。体系讲法见第 27 章 §27.13。

## D.10 五分钟定位一条慢查询

按顺序执行,别跳步。目标是五步内把"慢"收敛到一个具体原因。**本节用到的几条查询在 D.2 已经给过,这里只引用、不整段照抄**——这样两处不会各自漂移。

**第 1 步——从查询日志里捞出它。**
```sql
SELECT query_id, type, query_duration_ms, read_rows, read_bytes,
       result_rows, memory_usage, ProfileEvents['SelectedParts'] AS parts,
       query
FROM system.query_log
WHERE type = 'QueryFinish'
ORDER BY query_duration_ms DESC
LIMIT 10;
-- 若日志没开:先 SYSTEM FLUSH LOGS; 仍为空则去开 query_log
```

**第 2 步——看它读了多少。** 若 `read_rows` 远大于 `result_rows`,基本锁定"扫描量过大";若 `parts` 很大,锁定"part 太多"。

```sql
SELECT query_id, ProfileEvents FROM system.query_log
WHERE query_id = '<上面挑出的 query_id>' AND type = 'QueryFinish' FORMAT Vertical;
```

**第 3 步——估算它要读多少 part/granule。** 把那条 SQL 原样拿来跑估算:
```sql
EXPLAIN ESTIMATE <你的 SQL>;
```
若估算的 part 数接近全表,说明裁剪没生效 → 去查分区键/排序键与 `WHERE` 条件是否匹配(第 11 章)。

**第 4 步——看它此刻在干什么(若是还在跑)。** 用 D.2『查询卡住不动』给过的那条 `system.processes` 查询即可(反复看 `read_rows`:不涨 = 卡在锁/网络;疯涨 = 真在扫数据),不重复。

**第 5 步——看表的状态是否健康。** part 计数用 D.2『Too many parts』那条按分区的 `system.parts` 查询(把表名换成出问题的表);再补看 mutation 与副本延迟:
```sql
SELECT table, is_done, latest_fail_reason FROM system.mutations WHERE is_done = 0;
SELECT table, absolute_delay FROM system.replicas WHERE absolute_delay > 0;
```

五步走完,结论通常落在四类之一:**扫描量大(查裁剪)、part 太多(查写入/分区)、卡在锁或网络(查并发/Keeper)、表本身不健康(查 mutation/复制)**。每一类回本附录对应小节即可。

## D.11 备份与恢复

这一组的共同点是**数据/结构没在预期的时刻出现在预期的位置**;它们横跨第 18、29 章,但排错入口都是 `system.backups`(25.x 里备份是**异步**过程)与磁盘上的 `detached/`、`shadow/`。

### 1. `BACKUP` 一直没结束 / 报 timeout

**最可能的原因:** ①备份是**异步**的,命令返回不等于备份完成(判据:`system.backups` 里该 `id` 的 `status` 还是 `CREATING`);②目标端(尤其 S3 一类远端)慢或凭据错;③备份期间写入多,快照口径比预期慢;④并发备份太多被排队。

**怎么确认:**
```sql
SELECT id, name, status, error, start_time, end_time, num_files, total_size
FROM system.backups ORDER BY start_time DESC LIMIT 10;
```

**怎么修:** 用 `ASYNC` 起、再轮询 `system.backups` 的 `status` 到 `BACKUP_CREATED`;远端目标先单独跑 `BACKUP ... TO S3(...)` 定位连通性;备份期间对目标表的写入要接受"快照时刻"的口径。

### 2. 恢复之后数据不对 / 少了表

**最可能的原因:** ①恢复默认**不覆盖**已存在的同名表(冲突就失败或跳过);②备份时带了 `EXCEPT`/表清单,目标表根本不在里面;③备份与恢复的库名不一致,表落到了别处;④`RESTORE` 到复制表时副本配置对不上。

**怎么确认:** 查 `system.backups` 里该恢复 `id` 的 `status` / `error` / `num_tables`,再把目标库 `system.tables` 的实际清单与备份清单比对。

**怎么修:** 需要覆盖时显式写 `RESTORE ... AS`,或先 `DROP` 再恢复(理解代价);把 `RESTORE` 拆成"先验证清单、再逐表恢复";复制表恢复前确认 Keeper 可用与 `{replica}` 宏能解析。

### 3. `freeze`/`shadow` 残留把盘吃满

**最可能的原因:** `ALTER TABLE ... FREEZE` 产生的硬链接快照(`shadow/`)在备份完成后**不会自动删**,要 `SYSTEM UNFREEZE`。

**怎么确认:** 数磁盘上的 `shadow/` 目录;`SELECT name, free_space FROM system.disks;` 看剩余空间。

**怎么修:** `SYSTEM UNFREEZE` 清理,并把它纳进备份流程的收尾步骤。同见 D.4『磁盘满』。

### 4. 备份跑到一半失败,留下半份产物

**最可能的原因:** 目标端中途写满 / 掉线,或网络抖动。判据:`system.backups.error` 非空。

**怎么修:** 备份不是原子的——失败后清理目标端的半份产物再重跑,不要把半份当可恢复的备份。

## D.12 版本升级与兼容

升级(或"照着新文档写、却跑在本机旧版上")引发的症状往往隐蔽:**不是报错,而是默认值悄悄变了**。入口是 `system.settings_changes` 与 `version()`(附录 A 讲了怎么用它查默认值)。

### 1. 升级后行为变了,但没报错

**最可能的原因:** 某个设置的**默认值**在版本间翻转过(破坏性变更)。判据:同一份 SQL 在新旧版本上结果不同、且差别只落在该设置上。

**怎么确认:**
```sql
SELECT version();
SELECT version, change.1 AS name, change.2 AS previous_value, change.3 AS new_value
FROM system.settings_changes
ARRAY JOIN changes AS change
ORDER BY version DESC LIMIT 50;
```

**怎么修:** 用 `compatibility` 会话设置回退到旧行为(值取旧版本号),或显式 `SET` 每个受影响项。注意 `system.settings_changes` **只覆盖 `compatibility` 机制引入之后**的改动,更老的设置查不到。

### 2. 写好的 SQL 在新版本建不出来(语法/引擎被移除)

**最可能的原因:** 用的是**已被移除**的引擎或语法——最典型的是 `MaterializedMySQL`(已移除,见 D.7),以及改名过多轮的 `allow_experimental_*` 设置。

**怎么确认:** 报错文案通常是 `Unknown database engine` / `Unknown setting` / `Unknown function`;再对目标版本查 `system.table_engines`、`system.settings`、`system.functions`。

**怎么修:** 换到当前版本支持的写法(`MaterializedMySQL` → `mysql()` 表函数 / `MySQL` 表引擎 / 外部 CDC);"某个名字这一版在不在"一律交给 `system.*` 现场问,别照搬旧文档。

### 3. 跨版本比对指标/哈希值时对不上

**最可能的原因:** 哈希输出**可能跨版本变化**(见附录 A 的 A.12);`ProfileEvents` / 指标的命名与口径也会增删。

**怎么修:** 比对前先确认两边版本一致(`version()`);指标名以 `system.metrics` / `system.events` / `system.asynchronous_metrics` 的实际列为准。

## D.13 小结

- 排错从"确认现象"开始,别从原因猜起;能用 `system.*` 问清楚的一律先问。
- 查询类问题的四根支柱:扫描量(裁剪)、part 数、内存/溢写、卡在锁或网络。
- 写入类问题的头号根因是"小批量高频插入";part 相关的错误先看 `system.parts`,再谈阈值。
- 表与存储类问题记住两条:**`OPTIMIZE` 是异步的**,**TTL 只在合并时生效**;`ALTER DELETE` 是 mutation,不是"就地删"。
- 复制/分布式类问题先看 `system.replicas` 与 Keeper 可达性;跨分片 JOIN 优先 `GLOBAL` 或用字典。
- 集成/接入类问题(D.7)的头号坑是"忘了给 Kafka 源表配 MV"——`Kafka` 是门不是仓库;其次是字典静默加载失败(`system.dictionaries` 的 `last_exception`)与 `ON CLUSTER` 被某个离线节点拖住(`system.distributed_ddl_queue`)。`Too many open files` 多半还是"part 太多"的马甲。
- 监控/日志类问题先分清 `CurrentMetrics`(量)与 `ProfileEvents`(计数);两个日志表的默认态**不一样**:`query_log` 默认**是开的**(只是异步刷盘,刚跑完要 `SYSTEM FLUSH LOGS` 才看得到),`text_log` 默认**是关的**(要在服务端配置里显式开)。想快速知道服务端在报什么错,先看 `system.errors`(累计计数,认 `last_error_time`),它不像 `text_log` 那样默认关着。
- **时间的"差 8 小时"是独立的一类**,别混进"数据写错":`DateTime` 的时区是列属性,先比对照 `toUnixTimestamp`(与时区无关),再看 `date_time_input_format` 与列/会话时区是否一致(第 17 章)。
- 慢查询定位就一条流水线:`query_log` → 读了多少 → `EXPLAIN ESTIMATE` → `system.processes` → 表健康度。
- 备份与恢复类问题(D.11):先分清"命令返回"与"真的做完"——`BACKUP`/`RESTORE` 是异步的,查 `system.backups` 的 `status`;别忘了 `freeze`/`shadow` 的残留要 `SYSTEM UNFREEZE` 清。
- 版本升级类问题(D.12):症状常常不是报错而是"默认值悄悄变了"——用 `system.settings_changes` 找翻转过默认值的设置;"某个名字这一版在不在"一律交给 `system.*` 现场问。
