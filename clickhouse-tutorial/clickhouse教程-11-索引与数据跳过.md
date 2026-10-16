# 第 11 章 索引与数据跳过

从 MySQL 转过来的人,第一件想做的事往往是"给这个查询列建个索引"。在 ClickHouse 里这句话没有对应操作——它没有 B+ 树,没有可随意 `CREATE INDEX` 的二级索引。但 ClickHouse 在几十亿行上做实时过滤靠的不是"索引不够多",而是**另一套完全不同的思路**:用有序性把数据切成可跳过的大块,再用轻量的"摘要"跳过更多块。这一章讲清这套机制的边界:什么时候你能靠索引把扫描量砍到千分之一,什么时候你以为建了索引其实一点用没有。

## 11.1 先破除一个错误期待:没有 B+ 树

ClickHouse 里能加速过滤的东西有三样,它们解决的问题完全不同,不能互相替代:

- **稀疏主键索引**:大约每 `index_granularity`(默认**最多** 8192 行;自适应粒度会提前切,见 §11.2.1)一个键值,靠"数据物理有序"在索引上二分,定位一段 granule;
- **分区**:整块数据的物理划分,用于**整分区删除**与粗粒度裁剪;
- **数据跳过索引(skip index)**:给**非主键列**装的一双眼睛,只能回答"这一块里**可能**有吗",不能定位到行。

三者的共同点是:**它们都只能把范围收缩到 granule 级别**。没有哪个机制能像 B+ 树那样"直接跳到那一行"。所以 ClickHouse 的过滤性能下限是"读一个 granule + 线性扫这一 granule 里的行"(一个 granule 最多 8192 行,宽行表上会更少,见 §11.2.1);上限则由这三层裁剪决定。期望"给 `user_id` 建索引就能点查一行"是对这套体系的根本误解——正确的做法是让 `user_id` 成为排序键的一部分,或者接受点查本身就不该是 ClickHouse 的主场(它擅长的是扫描与聚合)。

## 11.2 稀疏主键索引:排序键的前缀规则

第 05 章讲了 `primary.idx` 是每 granule 一个键值。这里要讲它的**使用规则**,这是最容易踩坑的地方:

**`WHERE` 里能用索引的列,必须是排序键的前缀(而且中间不能断)。**

```sql
CREATE TABLE t (ts DateTime, uid UInt64, url String)
ENGINE = MergeTree ORDER BY (ts, uid);

SELECT ... WHERE ts = '2024-03-01' AND uid = 42;  -- ✓ 两列都用上
SELECT ... WHERE ts = '2024-03-01';               -- ✓ 用了前缀
SELECT ... WHERE uid = 42;                        -- ✗ 用不上,全表扫
SELECT ... WHERE ts > '2024-03-01' AND uid = 42;  -- △ 只有 ts 用上
```

为什么 `WHERE uid = 42` 用不上?因为数据先按 `ts` 排、`ts` 相同的才按 `uid` 排。同一个 `uid` 可能散布在每一个 `ts` 段里,索引上没有一段连续的键值对应它,二分的两边都可能是答案。这正是"**前缀**"两个字的含义——索引的有序性是**字典序**的,只有从第一列开始连续约束,才能切出一段连续区间。

最后一行 `ts > ... AND uid = 42` 只有 `ts` 用上,也是同一个道理:`ts` 上的**不等号**让 `uid` 落进了"每一段 `ts` 内部"而不是"某一段 `ts`",于是 `uid` 无法定位。这也是 `!=` 特别伤索引的原因:它在代码里被翻译成"两段区间"(`(-∞, v)` 与 `(v, +∞)`),把一次顺序读拆成两次,而且"点"的判定失败,后面的键列也就用不上了。

排序键的选择因此有一条经验法则:**把最常出现在 `WHERE` 里做等值/范围过滤的列放前面**(通常是时间),把用于"同一组内排序"的列放后面。如果某个高基数 ID 是高频过滤条件,考虑把它提到排序键里,而不是给它单独建跳数索引。

### 11.2.1 自适应粒度:granule 不总是 8192 行

前面反复出现的"每 8192 行一个键值"是个**过简化**的说法,只在窄行表上成立。真实 ClickHouse **默认开启自适应粒度(adaptive granularity)**,granule 的行数不是固定的:

```text
index_granularity            默认 8192       —— 一个 granule 的**行数上限**
index_granularity_bytes      默认 10485760   —— 一个 granule 的**字节上限**(10 MiB);> 0 即启用自适应
min_index_granularity_bytes  默认 1024       —— 字节下限(index_granularity_bytes 的保险丝)
```

切 granule 的规则是"**行数到顶**或**字节到顶**,谁先到就切":当一行的平均宽度很大时,一个 granule 在攒到 8192 行之前就已经吃满了 10 MiB,于是在第几千行(甚至几十行)就提前封口。极端例子是一行一个几百 KB 的宽 String 或大数组,一个 granule 可能只有十几行。反过来,窄行(几个 UInt 列)会先撞上 8192 行这个上限,granule 就是整整齐齐的 8192 行 —— 这也是"8192"这个印象的来源。

那 `min_index_granularity_bytes` 是干什么的?按官方 merge-tree-settings 的定义,它是**一个 granule 允许的最小字节数**,作用就是给 `index_granularity_bytes` 当**保险丝**:拦住“把上限设得比它还低、从而切出无数个字节数几乎为 0 的碎 granule”这种危险配置。所以这两个量一上一下,把 granule 的字节规模夹在区间里——`index_granularity_bytes` 是**上限**(默认 10 MiB),`min_index_granularity_bytes` 是**下限**(默认 1024 字节)。granule 仍按“行数到 `index_granularity`(默认 8192)或字节到 `index_granularity_bytes`,谁先到就切”的规则生成;`min_index_granularity_bytes` 不参与逐个 granule 的取舍,只在配置层面兜底。

**对索引大小和裁剪的影响是直接的**:一个 part 里有几个 granule,`primary.idx` 里就有几个键值(回想第 05 章:mark 里存的是每个 granule 的"压缩偏移、解压偏移、**行数**"—— 正因为 granule 不等大,行数才必须逐个记)。所以:

- **行越宽 → 每个 granule 行数越少 → 同样数据量的 granule 数越多 → 索引越大**;一个几百列的大宽表,索引本身就可能是几百 MB。
- **granule 越细 → 裁剪越精确**:`WHERE` 定位到的区间里要线性扫的行更少,点查/范围查更容易受益。
- 正因如此,调 `index_granularity` 时别只盯着"8192 行",也要想到 `index_granularity_bytes` 会不会先切;判断实际粒度应该去看 `system.parts` 里 `marks` 与 `rows` 的比值,而不是假设它就是 8192。

### 11.2.2 PRIMARY KEY 与 ORDER BY 不是一回事

建表时 `ORDER BY` 和 `PRIMARY KEY` 是两个子句,很多人以为写哪个都一样 —— 大多数时候确实一样,但它们可以不同。

`ORDER BY` 声明的是**排序键(sorting key)**,决定 part 内数据的**物理顺序**。`PRIMARY KEY` 声明的是**主键**,决定 `primary.idx` 这个稀疏索引里**存哪些列**。规则只有一条:**主键必须是排序键的前缀**。省略 `PRIMARY KEY` 时,主键默认就等于排序键(所以索引覆盖排序键的全部列)。

```sql
-- 索引只覆盖 ts 一列,索引更小,但只能按 ts 裁剪
CREATE TABLE t (...) ENGINE = MergeTree
ORDER BY (ts, uid, url)
PRIMARY KEY ts;

-- 省略 PRIMARY KEY:索引覆盖 (ts, uid, url) 三列,索引更大、裁剪更细
CREATE TABLE t (...) ENGINE = MergeTree
ORDER BY (ts, uid, url);
```

什么时候该显式写一个**更短**的主键?当排序键很长(比如为了让同组行聚集,后面接了若干列),而索引本身成了负担的时候 —— 用 `PRIMARY KEY` 截一段前缀,索引就只存这几列,`primary.idx` 显著变小、加载更快;代价是**排在主键之外的排序列不参与裁剪**(它们仍然有序,只是索引里没有它们的键值)。反过来,如果你希望排序列都能被裁剪,就别写 `PRIMARY KEY`,让主键默认等于排序键。

一句话:**排序键管"数据怎么排",主键管"索引记什么",后者是前者的一段前缀。** 不写 `PRIMARY KEY` 是最常见的正确选择;只有在"排序键很长、又愿意用粗一点的裁剪换更小的索引"时才去显式截前缀。

## 11.3 SAMPLE BY:与主键配合的采样

`SAMPLE BY` 让表支持"随机但可复现"的抽样。它不是查询里的临时选项,而要先在**建表**时声明采样表达式,查询里才能用它:

```sql
CREATE TABLE metrics (
    ts    DateTime,
    uid   UInt64,
    value UInt64
) ENGINE = MergeTree
ORDER BY (intHash32(uid), ts)      -- 采样表达式必须出现在主键里(主键默认即整个排序键)
SAMPLE BY intHash32(uid);
```

建好之后,`SELECT ... FROM metrics SAMPLE 0.1` 大致取 10% 的行,`SAMPLE 1000` 大致取 1000 行(`SAMPLE 1/10` 与 `SAMPLE 0.1` 同义)。

它的原理是**按采样键的取值分桶**:`SAMPLE BY` 的表达式把每一行映射成一个非负整数,同一个采样键值的行在物理上彼此靠近(因为采样键是排序键的一部分),查询时按比例圈定一部分键值、整段整段地取,落在桶外的行根本不读。这解释了两条硬约束。其一是**采样表达式必须出现在主键里**——注意这并**不是**"必须是排序键的前缀":主键默认就等于整个排序键,所以放在前缀或末尾都可以,上游的范例恰恰把它放在 `ORDER BY (CounterID, EventDate, intHash32(UserID))` 的**末尾**;只有当你显式声明了一个更短的 `PRIMARY KEY` 时,采样表达式才需要落在那个更短的主键之内(引擎的报错原文就是 `Sampling expression must be present in the primary key`)。其二是它**必须是非负整数类型**(要当"桶号")。也正因为圈的是"哪些键值"而不是"随机挑哪些行",采样才是**确定性**的——同一个查询跑两次结果一致。`uid` 这种真实业务列直接当采样键会让桶太细(几乎每个 uid 一个桶),比例失去意义,所以实践中几乎总是用一个**哈希列**(如 `intHash32(uid)`)当采样键。

三条容易忽略的边界。其一,`SAMPLE` 的表达式**必须与表上的 `SAMPLE BY` 一致**,写别的列会报"采样表达式不匹配"。其二,`SAMPLE 0.1` 只保证"大致比例",**不保证精确行数**(桶的大小不齐,取的是一批整数桶),要精确比例得靠别的手段。其三,采样发生在 `WHERE` **之前**(见第 07 章 §07.1 的求值顺序),所以"采样比例"和"过滤命中率"是**相乘**的:`SAMPLE 0.1` 再配一个命中率 50% 的 `WHERE`,最终留下的行大约只有 5%——而且更要紧的是,采样没圈到的桶里**就算有匹配行也不会被看到**,所以对"全表精确统计"不能靠采样。

mini-clickhouse **没有实现采样**(`StorageFeatures` 里 `sampling=False`)。

## 11.4 跳数索引:它只跳 granule,不做定位

当过滤列不是排序键前缀时,主键索引帮不上忙。这时你可以用一个**跳数索引**:它在每个"粒度组"上记一份摘要,查询时先问摘要"这组里**可能**有满足条件的行吗",答"一定没有"就整组跳过,答"可能有"就照常读出来过滤。

跳数索引最核心的一条不变量,值得单独强调:

```text
may_be_true 允许"错报真"(false positive),
但绝不允许"错报假"(false negative)。
```

返回"可能"只是让查询多读一点;返回"没有"就意味着整组被跳过——如果那里其实有匹配行,结果就**错了**。错的代价比慢高一个数量级,所以每个跳过索引的 `may_be_true` 默认都返回"可能":不确定就放行。**理解这条,就能理解下面每种索引的保守之处都是从哪来的。**

## 11.5 minmax:最便宜、最通用

`INDEX name expr TYPE minmax` 在每个粒度组里存该表达式的 `(min, max)`(粒度组的大小随自适应粒度浮动,见 §11.2.1),这是所有跳数索引里结构最简单、也最便宜的一种:每组只多存两个值,索引文件通常小到可以忽略。它最适合**按时间递增写入**的列——新数据的时间戳只会越来越大,每个 granule 的 `(min, max)` 因此是一个窄区间;相反,如果列的值在写入顺序里随机跳动(自增 ID 反着写、或时间戳被乱序导入),每个组的 `(min, max)` 都会覆盖全域,`minmax` 就退化成毫无用处。

它的判定逻辑就是"**组区间与查询区间有没有交集**"。以 `INDEX idx_ts ts TYPE minmax GRANULARITY 1`、`WHERE ts >= '2024-06-01'` 为例,一个 part 里逐 granule 看:

```text
granule    存的 (min_ts, max_ts)          判定
   1       ('2024-01-01', '2024-01-31')   完全落在查询区间左侧 → 整组跳过
   2       ('2024-05-01', '2024-05-31')   完全落在左侧         → 跳过
   3       ('2024-06-01', '2024-06-15')   与查询区间相交       → 读出来逐行过滤
   ⋮
```

只有组区间**完全落在查询区间之外**才跳;边界一重叠就保守放行——这正是 §11.4 那条"允许错报真、绝不允许漏读"的最直白实现。

两个性质要记牢。其一,**参数**:`minmax` 是唯一**没有可选参数**的跳数索引类型,写法就是 `TYPE minmax`(唯一的修饰是 `GRANULARITY`),不像 `set(N)`、`bloom_filter(p)` 那样要给一个参数。其二,**类型**:它对所有**可比较**类型都成立,`Date`/`DateTime` 列的 `min`/`max` 就按时间戳语义存下来,`WHERE ts >= '2024-06-01'` 的区间比较不需要额外转换;变长列(`String`)上则有额外的空间控制,配套实现与真实 ClickHouse 在这一处的口径不同(见 §11.17)。

再看谓词形态:`!=` 只有在"整组所有值都等于 `v`"时才能跳过(`min == max == v`),否则排除不掉任何一组;`IN` 则是在 `(min, max)` 里逐个判断,只要有一个落在组区间内就得读。所以 `minmax` 天然适配**范围谓词**(`>`、`<`、`BETWEEN`),对等值与 `IN` 也有用,但对 `!=`、`NOT IN` 基本无能为力。

## 11.6 set(N):低基数列的精确摘要

`INDEX name expr TYPE set(N) GRANULARITY 1` 存的是"这组里出现过的**去重取值集合**",`N` 是集合最多保留多少个不同值,由建表语句直接给出;与集合规模相关的两个会话设置 `max_rows_in_set` 与 `max_bytes_in_set` 默认都是 **0**(不限制)。它适合**低基数**列——比如 `region`、`status` 这种取值几十上百个的。低基数意味着集合能完整装下,`WHERE region = 'us'` 就能精确回答"组里有没有 `us`"。

关键的代价在"**装不下**"时:`set` 一旦因为值太多而**截断**,摘要就不再完备,`may_be_true` **必须放行一切**(否则会漏读)。所以给高基数列(比如 `url`、`request_id`)建 `set` 是纯浪费——索引占空间、写入要维护,却因为截断而永远返回"可能",一点都跳不掉。需要高基数列过滤时,应该用 `bloom_filter` 或 `ngrambf_v1`。

## 11.7 bloom_filter:只会误报真,不会漏报

`INDEX name expr TYPE bloom_filter` 是一个概率型摘要:插入元素只置位,查询时"只要有一位是 0,就**一定**没出现过;全为 1,则**可能**出现过"。这正好满足 §11.4 的不变量:

```text
命中(全 1) → 可能真 → 读出来过滤    (假阳性:多读)
未命中(有 0) → 一定假 → 整组跳过     ← 这一侧永远正确
```

布隆过滤器适合**高基数**的等值/`IN` 查询(`WHERE url = '...'`),因为它的空间开销与"元素个数"无关,只与"预期插入量 `n` 和误判率 `p`"有关,位数组大小按 `m = -n·ln p /(ln 2)²` 算。误判率越低,索引越大——这是唯一要权衡的地方。它**回答不了范围查询**(`>`、`<`)和"包含子串"(`LIKE '%x%'`),遇到这类条件只能保守放行。

`bloom_filter` 的唯一可选参数是误判率 `p`,**默认 0.025** —— 也就是说不写参数时,索引按 2.5% 的误报率来配位数组大小。写法是 `TYPE bloom_filter(0.01)` 这样显式给 `p`;`p` 越小索引越大、误报越少,要不要压到 0.01 以下取决于该列上有多少查询真的能靠它跳块。

## 11.8 ngrambf_v1 与 tokenbf_v1:字符串的子串与分词

这两个索引都基于布隆过滤器,但把"一个字符串"拆成多个 key 再分别置位,于是能回答"包含某个子串/词"的问题:

**`tokenbf_v1(size, hashes, seed)`** 按**非字母数字字符**切词。`"http://example.com/path"` 切成 `["http", "example", "com", "path"]`。它对 `WHERE url LIKE '%example%'` 有效(因为 `example` 是一个完整 token),但对 `WHERE url LIKE '%xample%'` **无效**(`xample` 不是任何 token)。这是使用 `tokenbf_v1` 最容易踩的坑——**只有整词匹配才有效**。

**`ngrambf_v1(n, size, hashes, seed)`** 把字符串切成 n-gram。它对任意子串(只要长度 ≥ n)都能给出"可能/不可能",因此比 token 版通用,但索引更大。**这里先纠一个顺口就会写错的地方:`n` 数的是 UTF-8 的"字符"(code point),不是字节。** 上游 `src/Interpreters/ITokenExtractor.cpp` 里 `NgramTokenExtractor` 是按 `UTF8::seqLength(...)` 一个字符一个字符往前走的,所以 `n=3` 表示"三个字符",在纯 ASCII 上刚好等于三字节,在中文上就是九字节——**同一份配置在两种文本上的行为不一样**,拿它给多语言字段建索引时要意识到这一点。

n 的取值是权衡,但方向和很多人想的不一样:**n 越大,单个字符串切出的 gram 越少、每个 gram 越"具体",于是每个 granule 里的 key 越不容易撞满,误判率越低**;代价是它**只对长度 ≥ n 的子串有效**——查询串比 n 短时这个索引帮不上忙,而**索引文件本身的物理大小由 `size` 决定、跟 n 无关**(见下面那条参数说明)。所以"n 越大索引越大"是错的:大 n 省的是 key 的数量与误判,费的是"能覆盖的查询"的范围。

**参数到底是什么意思:** 这是全书最容易被照抄坏的一处,值得把每个名字拆开。上游 `BloomFilterParameters` 的构造签名就是 `(filter_size, filter_hashes, seed)`,两者对号入座即可:

- **`size`(字节)** —— **每个索引块**的布隆过滤器有多大,单位是**字节**,不是行数、也不是比特数(内部是这些字节 × 8 个比特)。它是个**固定值**:引擎不看这一块里究竟有几个不同的 token/n-gram,就按你给的字节数给每块各分一个位数组。这一点和 §11.7 的 `bloom_filter(p)` **根本不同** —— 后者只给一个误判率 `p`,引擎据此反推出"每行多少比特",乘上这块的行数才得到实际的位数组大小;`*bf_v1` 把大小**交给你定死**,"装不装得下"于是成了你的责任:同一块里不同 key 越多,固定的位数组越容易被**塞满**,误判率就往 1 涨。`size` 与 `hashes` 都不许为 0(为 0 直接报错),上限是 `1 << 30` 字节(1 GiB)。
- **`hashes`(个数)** —— 用几个哈希函数对每个 key 置位。多了,每个 key 置更多位、位数组更快饱和、写入与查询都更贵;少了,每个 key 只置一两位、误判率偏高。对给定的位数组大小,存在一个接近最优的值,大致是 `(位数组比特数 / 块内不同 key 数) × ln 2`(§11.7 那条 `m = -n·ln p /(ln 2)²` 的同一套推导)。上游实现用双哈希:`pos = hash1 + i·hash2 + i² (mod 8·size)`,`hashes` 就是那条式子里 `i` 的循环次数。
- **`seed`(随机种子)** —— 生成那组哈希函数用的种子。它只决定"哪些 key 落到哪些位",**不改变误判率的量级**;换一个非 0 的值完全没问题,但一旦定下就**别改** —— seed 变了,老 part 的索引位图与新建的**对不上**,只能靠 `MATERIALIZE INDEX` 重建。
- n-gram 版多出来的第一个参数 **`n`** —— 每个 gram 包含**几个字符**(UTF-8 code point,不是字节)。`n=3` 时 `"example"` 会展开成 `"exa" "xam" "amp" "mpl" "ple"` 五个 key。n 越大,一个字符串展开出的 key 越少、每个 key 越具体(误判越少),但对**短于 n 的查询子串就无能为力** —— `ngrambf_v1(3, …)` 对 `LIKE '%ab%'`(只有 2 个字符)说不出话,只能保守放行。

**为什么 `ngrambf_v1` 的索引"大得多"、也更要小心配。** 同样是 8 个字符的字符串,`tokenbf_v1` 通常只切出 1~2 个 token,而 `ngrambf_v1(3, …)` 会切出 6 个 3-gram。**同一个固定大小的位数组,要装的 key 多了一个数量级,自然更快饱和、误判率更高。** 要让两者达到同样的误判率,`ngrambf_v1` 的 `size` 就得显著更大;这正是"给它配小了等于没建、配大了索引文件反超列数据"的来源(§11.14)。所以估 `size` 的正确姿势是:**先估每块里会有多少个不同的 n-gram**,再套布隆过滤器的标准公式反推位数组大小 —— 官方文档直接给了几条可粘贴的 UDF 干这件事:`bfEstimateBmSize(总 gram 数, 目标误判率)` 给出需要的比特数、`bfEstimateFunctions(…)` 给出哈希函数个数(例:一块里约 4300 个 n-gram、想把误判率压到 0.0001,算出来约 **10304 字节**、**13** 个哈希函数)。文档里那句"`size` 可以用 256 或 512 这种较大值"要连着看:**这里的 256/512 指的是字节**(如官方示例 `tokenbf_v1(512, 3, 0)`),别把它读成"256 个元素"。最后补一句索引大小的直觉:索引文件 ≈ **索引块数 × `size`** —— `GRANULARITY = 1`、每 8192 行一块、`size = 256 KiB` 时,十亿行就是约 12 万个块,光这一份索引就约 30 GB;所以 `size` 从来不是"越大越保险"。

两个索引的共同前提:数值列上用不了,只对 `String` / `FixedString`(以及 25.3 起也接受的 `IPv6`,和 `LowCardinality(String)`、`Array(String)` 这类包装)有意义。也正因为它们是为文本检索设计的,在**做全文检索**的新场景里正逐步被下面的全文索引取代 —— 词级匹配这件事,专门结构做得比"把 token 塞进布隆过滤器"更省、更准。

## 11.9 跳数索引的完整名单、全文索引与向量索引

先把"到底有哪些类型"钉死。**官方文档当前列出的跳数索引类型就是这几种:`minmax`、`set`、`bloom_filter`、`ngrambf_v1`、`tokenbf_v1`,以及较新的全文索引。** 判断一个名字是不是真的跳数索引类型(对包括全文索引、向量索引在内的所有候选类型都适用),有两个**当场可做**的动作:**`CREATE TABLE ... INDEX x expr TYPE <名字> GRANULARITY g` 能不能被接受**,以及 **`system.data_skipping_indices` 里认不认它**,或者干脆去你版本文档的 "Data Skipping Indexes" 页核对一遍类型清单 —— 别从二手博客里抄类型名。

`hypothesis` 是一个必须点破的坑。它曾是 ClickHouse 内部一个**实验性**的跳数索引类型(基于每个 granule 的布尔断言、用比较图/CNF 做逻辑推理,比 `minmax`/`set` 这类数值摘要"更懂逻辑"),但**上游已在 26.3 把它整体移除**(PR #96874,不兼容变更),自那以后写 `TYPE hypothesis` 会直接报错(错误码 127 `ILLEGAL_INDEX`);在 26.3 之前的版本(含 25.3)它仍在,但始终只是实验性类型,不要照搬进生产。所以它既不是文档里的常规类型,也不是能照搬进生产的选项。还要注意**别**把它和名字相近、但完全是另一回事的**"假设索引"**(hypothetical index,`CREATE HYPOTHETICAL INDEX` + `EXPLAIN WHATIF`)搞混 —— 后者是一种"我要是真在这列上建个索引会怎样"的**假设性诊断**,不落地任何真实索引数据。

**全文索引**是这份名单里唯一还在演进、也最值得多花几句的。它面向"自然语言 / 长文档的全文检索",底层是一份**反向结构(倒排结构)**:把文本按词或 n-gram 切开,建立"词 → 出现它的**行**"的映射。查询时拿谓词里的词去这份映射里查,对不上的行直接不读。它加速的是**词级谓词** —— `hasToken(s, 'x')`、`hasAllTokens(...)`、`hasAnyTokens(...)` 这一类"串里含这个/这些**整词**"的写法,而不是 `LIKE '%x%'` 那种任意子串。它和 `tokenbf_v1` 解决的是**同一类问题(词级匹配)**,但用的是更专门的索引结构:bloom 版把 token 塞进一个位数组、只能回答"可能/不可能"且受误判率牵连、还会因截断而失准;倒排版直接把 token 映射到行区间,既更省空间也更精确。正因如此,它在**文本检索**这一块正逐步取代 `tokenbf_v1` / `ngrambf_v1` —— 新表如果目标就是全文检索,优先考虑它;老表上的 bloom 版索引可以保留,但不必再往新建的宽表上铺。

**它挂在"跳数索引"名下,但不是"按 granule 跳"那一套。** 它和别的跳数索引一样写在 `INDEX` 子句里、也被 `system.data_skipping_indices` 收录,但官方文档明说:和别的二级索引不同,全文索引(目前)**映射到行号(row id)而不是 granule 号**(设计动机是性能)。所以建表语句里那个 `GRANULARITY` 参数**对它没有意义** —— 位置还得写,值会被忽略(官方也说这个参数将来可能从语法里去掉)。**别把 §11.10 那套"一个索引块覆盖几个 granule、值越大裁剪越粗"的说法套到全文索引上**;真正决定它粒度的是内部的段与 postings 列表(受 `max_digestion_size_per_segment` 一类设置影响),而不是那句 `GRANULARITY 1`。这也是它和 `minmax`/`bloom_filter` 这些真·跳数索引最本质的区别:后者按 granule 组织,它按 part / 行组织。

**类型名与开关分版本,不要照抄。** 25.3 上它叫 `full_text`,是**实验特性**,要 `SET allow_experimental_full_text_index = true` 才可用,当时也不在 Cloud 上支持。这个名字在上游被改过三次,但**这三次不落在同一个版本,别混成一句**:最早叫 `inverted`,**2024 年**改成 `full_text`——所以 25.3 这个基线看到的已经是 `full_text`;之后才在 **v25.6 那条 changelog 线**上连着改了两回,`full_text` → `gin`(上游 PR #79024)、`gin` → `text`(上游 PR #80855)。也就是说,"三次改名都落在 25.6"是错的:`inverted` → `full_text` 发生在 2024 年、早于 25.3,只有后两次落在 25.6。旧名仍可被加载,但会抛一个指向新名的异常。

与之配套,分词配置也在改名的那一版起从位置参数换成 `tokenizer = '...'` 的键值语法(如 `text(tokenizer = 'splitByNonAlpha')`),可选分词器是 **`default` / `ngram` / `noop`**,其中 `default` 就是"按非字母数字字符切分"。这条"按版本分叉"的事实,**本节是全书唯一给出完整版本链与自检动作的地方**——第 01 章 §01.8 与第 30 章 §30.12 只保留结论并指向这里,不再各自重述一遍(第 32 章 §32.7 从"演化"一侧另有一节,同样以本节为准)。

**它加速检索,但不负责打分。** BM25 / TF-IDF 这类**相关度打分**在 ClickHouse 上是否原生支持,**本条未确认**:官方把它定位成"检索/过滤"而不是"相关度引擎",且这个索引不存位置信息,所以本书给不出"有/没有内置 BM25"的确切结论。稳妥的设计是**不依赖内置打分**——要相关度排序就自己在应用侧算,别指望索引给出 `score`。同样的口径也适用于"**只能靠一个实验开关才够得到**"的索引能力(见下面的向量索引):它们"是不是原生支持、开关叫什么、参数怎么变"一律按**未确认**对待,以你那一版的文档与实测为准。

**向量索引(`vector_similarity`)。** 上面那份官方名单之外还有一类:它**也写在 `INDEX` 子句里、也被 `system.data_skipping_indices` 收录**,但机制与上面全部不同,是给一列向量(`Array(Float32)` / `Array(Float64)`)建的**近似最近邻(ANN)索引**——把"最相似的 N 条"写成普通的 `ORDER BY 距离函数(向量列, 参考向量) LIMIT N`,索引让这个排序不必逐一向量算距离。它的底层是否就是**HNSW 近邻图**,**本条未确认**(未在 25.3 上核实其图结构)。与全文索引"映射到行号而非 granule"的定位相反,它据称是**真正按 granule 裁剪**的跳数索引,但这一点在 25.3 上**同样未确认**。两条硬边界:一是它是**近似**检索,召回率与延迟要拿索引参数权衡,拿不到精确答案;二是它在**写入与合并时构建**,所以写入会明显变慢,面向的是"读多写少、向量基本不变"的负载。25.3 上它是**实验特性**,要显式打开对应开关才能建,类型名与开关名都随版本变过(**具体名字与参数语义本条未确认**,以你的版本文档为准)。建法与参数在第 30 章 §30.12,版本演化在第 32 章 §32.7;配套实现没有它(§11.17)。

## 11.10 GRANULARITY 的含义

建跳数索引时必须给一个 `GRANULARITY`:

```
每个索引块覆盖的行数 = index_granularity × GRANULARITY
```

`GRANULARITY = 1` 时,索引的粒度与数据 granule 一致(每个 granule 一条摘要;而一个 granule 最多 8192 行,宽表上更少,见 §11.2.1),裁剪最精细、索引最大。把它调大(比如 4),一个索引块覆盖 4 个 granule,索引条数变成 1/4、占用更小,但**一次命中要连带读出这 4 个 granule**——裁剪变粗。

把"索引块覆盖几个 granule"和"索引大小 / 裁剪精度"的对照讲具体一点。假设一个 part 里有一百万个 granule(数据很宽,每个 granule 的行数很少),跳数索引在每个索引块上存一条摘要:

- **`GRANULARITY = 1`**:索引里有 100 万条摘要。每条摘要按几十字节算,整份索引就可能有几十上百 MB;好处是每个索引块**恰好对应一个 granule**,`WHERE` 判"这一块里没有"就是精确地跳掉一个 granule,一点不浪费。
- **`GRANULARITY = 8`**:索引只剩 12.5 万条摘要,体积缩到约 1/8(可能从上百 MB 降到十几 MB),加载和读索引的 IO 也同比例下降;代价是命中的那个索引块**连带罩住 8 个 granule**,`WHERE` 一旦落进这一块,就要把这 8 个 granule 都读出来再逐行过滤——哪怕真正命中的只有其中一个。

所以取舍的判据是谓词的"**命中密度**":如果每个索引块里最多只有一个 granule 会命中(命中很稀),把 `GRANULARITY` 调大纯属亏掉裁剪、白读一堆无关 granule;反过来,如果一个索引块里几乎每个 granule 都会命中(命中成片),那本来也没什么可跳的,这时把 `GRANULARITY` 调大来省索引空间才是划算的。

所以任务很明确:**过滤条件稀疏、希望极致裁剪时用 `GRANULARITY = 1`;列很宽、但只是希望粗粒度跳过大段时,可以适当调大以省索引空间。** 不要无脑设大。

## 11.11 三个开关:use_skip_indexes、use_skip_indexes_if_final 与 force_data_skipping_indices

`use_skip_indexes` 默认 **1**(开启)。验证一个查询能不能用上跳数索引时,把它临时 `SET use_skip_indexes = 0`,对比两次的扫描量,是最直接的证据。

`use_skip_indexes_if_final` 控制在**带 `FINAL` 的查询**里是否也应用跳数索引。它是较新引入的设置,**默认值改过一次**:早期默认 `0`(关闭,顾虑是跳过的 granule 里可能藏着需要参与归并的最新版本),**25.6 起默认 `1`**(上游 PR #81331),同一次改动还让配套的 `use_skip_indexes_if_final_exact_mode`(25.5 引入)也默认开启,以保证“跳过”之后结果仍精确。所以 **25.3 上它的默认值是 0**;要判断你那版的实际取值,查 `system.settings` / `system.merge_tree_settings` 最直接。需要说明的是,`FINAL` 场景下跳过索引的收益是有条件的:反正要去重,跳过的 granule 也得参与归并,只有配合 exact-mode 之类的机制,才能在“跳过”的同时保住结果的正确性。

`force_data_skipping_indices` 是一条**诊断/守护**用的设置,和上面两条方向相反:它接收一串跳数索引名(逗号分隔),要求查询**必须**用上它们。只要其中任何一个因为谓词形态不对、列不对而用不上,查询就直接报 `INDEX_NOT_USED` 失败,而不是悄悄地退化成全表扫描:

```sql
SET force_data_skipping_indices = 'idx_url_bf';
SELECT count() FROM t WHERE url = '/p/7777';   -- 用上了 idx_url_bf → 正常
SELECT count() FROM t WHERE url LIKE '%7777';  -- bloom 用不上 → 直接报错
```

为什么需要它?因为"索引还在,但查询已经不再命中它"是一种**静默的性能回归** —— 你改了 `WHERE` 的写法、或者换了列的类型,索引悄悄失效,查询照常返回正确结果,只是从毫秒变成了全表扫。把 `force_data_skipping_indices` 写进 CI 的回归查询里,"我以为它用了索引"就变成了"它必须用,否则测试挂"。这是除 §11.13 那三种验证手段之外,唯一能把"索引生效"变成**断言**的开关。

## 11.12 projection:透明的另一种排序

**projection(投影)** 是表**内部**维护的另一份数据,通常带**不同的排序键或预聚合**。它的关键在于**对用户完全透明**:你照常写 `SELECT ... FROM t`,优化器自己判断"这张表有个投影的排序方式正好适合这个查询",然后去读投影而不是主表。

这带来一个必须分清的区别:**projection 与物化视图不是一回事。**

- **物化视图**是你显式创建的、有独立名字的**表**;你想用到它,得**显式查那张表**(或者靠查询改写命中);
- **projection** 是主表的一部分,查询**不需要改**,优化器自动选择;`ALTER TABLE ... ADD/DROP PROJECTION`,数据随主表一起合并维护。

代价:投影是**按它自己的排序键再存一份数据**,存储开销正比于**它自己选择保留的列**,而且每次 `INSERT` 都要多维护一份(写入放大)。所以"成倍增加存储"只在"投影恰好保留了基表的全部列"时才成立——一个只选了几列的投影完全可能比基表小得多;真正要权衡的是"这份投影值不值它占的那点空间 + 写放大"。它适合"少数几条固定形状的重查询",不适合给每条查询都配一个。

还有两条限制必须知道,否则会在错的地方用投影:**投影只服务单表查询**,它的 `SELECT` 不能跨表 `JOIN`(想跨表预计算,那是物化视图的活);而且**带 `FINAL` 的查询不会去用投影**(去重语义要合并原始 part,优化器不冒险改走投影)。所以"用投影给宽表加速"这个念头,在"必须带 `FINAL`"或"要 join 别的表"的场景里直接落空。

控制它用不用、以及"必须用"的两个开关,和 §11.11 的跳数索引开关一一对应:`optimize_use_projections`(**默认 1**)允许优化器在合适时改用投影;`force_optimize_projection`(**默认 0**)设成 1 之后,如果某条查询**没能**用上投影,就直接报错而不是退回读主表 —— 这是把"投影生效"变成断言的手段。

投影的**建表/`ALTER` DDL、`ADD` 只改元数据的回填语义、`system.projections` 与 `system.projection_parts` 两张系统表,以及怎么用 `EXPLAIN` 确认它真的被选中**,与跳数索引的验证是同一套工具,集中放在第 24 章 §24.10;这一节只保留三个判断:**它是什么、什么时候该用、什么时候用不上。**

## 11.13 怎么验证索引真的生效

不要"建了就信"。三个手段:

**对比 `SelectedMarks`。** 同一个查询,`SET use_skip_indexes = 1` 与 `= 0` 各跑一次,命中 mark 数(`SelectedMarks`)应当有数量级差异。没有差异就是没生效。

**看 `EXPLAIN indexes = 1`。** 它在 `ReadFromMergeTree` 步的 `Indexes:` 段里逐项列出每个裁剪来源(`MinMax`/`Partition`/`PrimaryKey`/`Skip`)的 condition,以及"应用前/应用后的 parts 数与 granules 数",是裁剪效果的权威视图。这一段的**逐行读法**(怎么从 `Parts`/`Granules` 比值判断是哪一层帮了忙)在第 24 章 §24.9 专门讲,这里不重复。

**看系统表:`system.data_skipping_indices` 与 `system.parts`。** `system.data_skipping_indices` 列出每张表上已定义的跳数索引——库、表、索引名、类型(`type`)、表达式与 `GRANULARITY`;先在这里确认"我建的类型 / 表达式 / 粒度到底是不是我以为的那样"(它同时也是判断某个类型名认不认的当场动作,§11.9),再往下谈生效与否。`system.parts` 侧看的是索引**数据**有没有真的落到活跃 part 上:索引文件名为 `skp_idx_<name>.idx`,能确认它存在、有多大。如果索引文件比它对应的列数据还大,那就是明显的反模式信号。

## 11.14 反模式

**给高基数列建 `bloom_filter` 且误判率设得过低。** 后果:索引文件可能比列数据还大,磁盘与 IO 都被索引吃掉,而收益只体现在少数查询上。正确做法:先估算查询的选择性,选择 `bloom_filter`(高基数等值)还是 `set`(低基数),并把误判率控制在"索引开销可接受"的范围。**同一形状的坑在 `tokenbf_v1` / `ngrambf_v1` 上更常见**,因为它们的 `size` 是**固定字节数**、不随块内 key 数自适应(§11.8):`ngrambf_v1(3, …)` 每块塞进去的 key 比 token 版多一个数量级,`size` 配小了位数组直接饱和、索引形同虚设,配大了又让索引文件反超列数据 —— 两头都是纯浪费。给它们估 `size` 要先估"每块有多少个不同的 n-gram",而不是先抄一个数字(§11.8 给了公式与官方 UDF)。

**期望用跳数索引做点查。** 后果:你以为 `WHERE id = 42` 会"直接跳到那一行",实际它仍然要解压一整个 granule 再线性扫。正确做法:点查的列应该是排序键前缀;跳数索引解决的是"大范围扫描里能不能少读几块",不是"定位到行"。

**把跳数索引当二级索引理解。** 后果:你以为它像 MySQL 的二级索引一样"建在列上就能用",却不知道它只对特定谓词形态(等值/`IN`/部分 `LIKE`)有效,而且只能跳 granule。正确做法:先确认查询的谓词形态,再选索引类型。

**忘了 `GRANULARITY` 的作用。** 后果:设成 4、8 之后惊讶地发现"裁剪没以前细了"。正确做法:理解"一个索引块覆盖 `GRANULARITY` 个 granule"(行数 = granule 行数 × `GRANULARITY`,而 granule 本身最多 8192 行),按需要的裁剪精度来定。

## 11.15 跳数索引的 DDL:加、物化、删

前面都在讲"跳数索引是什么",这一节补上**怎么在表上增删它**。和 projection 一样,跳数索引既可以写在 `CREATE TABLE` 里,也可以事后用 `ALTER` 加:

```sql
-- 建表时定义
CREATE TABLE t (
    id  UInt64,
    url String,
    INDEX idx_url url TYPE bloom_filter(0.01) GRANULARITY 1
) ENGINE = MergeTree ORDER BY id;

-- 事后给已有表追加
ALTER TABLE t ADD INDEX idx_url url TYPE bloom_filter(0.01) GRANULARITY 1;

-- 给已有 part 回填这份索引的数据
ALTER TABLE t MATERIALIZE INDEX idx_url;

-- 只删索引数据、保留定义(便于之后重建)
ALTER TABLE t CLEAR INDEX idx_url;

-- 连定义一起删
ALTER TABLE t DROP INDEX idx_url;
```

这里有一条和 projection **一模一样**的规律,也是最容易踩的:**`ADD INDEX` 只改元数据,索引数据是"按 part"存的**。新加的跳数索引只从**这条语句之后写入的 part** 开始生效;已经存在的 part 里没有它的摘要,得靠 `MATERIALIZE INDEX`(或后续的合并/重写)补算出来。所以在**已经装满历史数据的表**上"加了索引却没变快",第一反应应该是"part 还没被打上这份索引",去 `system.data_skipping_indices` 看定义、去 `system.parts`(或对应的索引文件)确认索引数据是否真的落到了活跃 part 上,而不是怀疑优化器没选它。

另外几个细节:`ADD INDEX` 只是**增量元数据变更**,代价很小;而 `MATERIALIZE INDEX` 会**重写涉及的所有 part**,是一次实打实的大 IO,生产上建议**按分区**分批做(`ALTER TABLE ... MATERIALIZE INDEX ... IN PARTITION ...`),别在高峰时段对一张大表整体物化。`CLEAR INDEX` 只清数据、留定义,适合"这份索引先别用、之后还要重建"的场合;`DROP INDEX` 才是彻底移除。这一族 `ALTER` 在有副本的集群上走的是和别的 `ALTER` 一样的复制协议(第 22 章)。

还要注意写法本身:**`INDEX` 子句必须写在列定义之后**、和 `ORDER BY`/`PRIMARY KEY`/`SAMPLE BY` 这些子句并列(建表语法见第 06 章)。索引表达式通常就写成"列名",也可以写成"列上的一个函数"(如 `lower(url)`)—— 而后者正是下一节的引子。

## 11.16 比 GRANULARITY 更常踩的坑:谓词必须字面匹配

上一节的建表例子 `INDEX idx_url url` 里,索引表达式就是那一列。但跳数索引更常出问题的地方在于:**索引表达式和查询里的谓词表达式必须"字面匹配",引擎才能对上号。**

最典型的例子是**把函数套在索引列上**。假设你建了 `INDEX idx_lower lower(url) TYPE bloom_filter`,想加速"不看大小写地查 URL":

```sql
-- ✓ 生效:谓词里也写了 lower(url),表达式与索引一致
SELECT count() FROM t WHERE lower(url) = 'a.com';

-- ✗ 不生效:谓词里先转换了常量、没转换列,表达式对不上
SELECT count() FROM t WHERE url = lower('A.CoM');
```

第二句在语义上和第一句几乎等价,但引擎在"这份索引能不能用"这一步,是**按表达式结构去匹配的**:它只认"谓词里出现了和索引一模一样的表达式 `lower(url)`"。而 `url = lower('A.CoM')` 里左边是裸列 `url`、右边是个常量,匹配不到 `lower(url)`,于是这份索引**整条被跳过**,查询退化成全表扫 —— 而且**不报错**,结果照样正确,只是慢。这正是"索引还在,但查询已经命中不了它"这类**静默性能回归**的典型来源。

所以有两条实践建议:**其一,索引表达式尽量就用"裸列",把函数搬进查询而不是搬进索引**(`INDEX idx_url url` 配 `WHERE url = 'x'`,最不容易对不上);**其二,如果确实要在函数上建索引,就保证查询里也写同一个函数**(`lower(url) = ...` 对 `lower(url)`)。

谓词还有一层"形态"约束:`tokenbf_v1`/`ngrambf_v1` 只对 `LIKE '%...%'`、`match(...)` 这类含子串的形态起作用(见 §11.8);你写成 `=` 它们反而帮不上。**"索引建得对"和"查询写得对"是两件事,缺一不可。**

排查"到底是索引没生效、还是谓词对不上"时,有一个反向的开关可用:`ignore_data_skipping_indices` 接收一串跳数索引名,**让这次的查询故意不使用**它们。把它和 `force_data_skipping_indices`(§11.11)对照着用:`force_...` 是"必须用、用不上就报错",用来**证明**某个索引本该生效;`ignore_...` 是"这次别用",用来**排除** —— 在 `EXPLAIN indexes = 1` 的输出里,把它加上去前后各看一次,就能确认某个 `Skip` 块究竟是不是这个谓词命中的、以及它对 `Granules` 到底有没有贡献。

## 11.17 与 mini-clickhouse 的对照

配套实现里,`key_condition.py`(1375 行)实现主键/分区裁剪,`skip_index.py`(1554 行)实现跳数索引。**实现了五种**:`minmax`、`set`、`bloom_filter`、`ngrambf_v1`、`tokenbf_v1`(`supported_index_types()` 的返回值就是这五个,正好是 §11.9 里那份官方名单去掉全文索引后的部分)。**没有实现**:全文索引(§11.9),以及做向量检索的那一类 ANN 索引;projection **没有实现**(`PROJECTION` 语法能被解析,但 MergeTree 引擎里没有任何投影代码,`system.projections` / `system.projection_parts` 这两张表也不存在)。

`may_be_true` 的保守性在代码里处处可见,与 §11.4 的不变量严格对齐:`_EmptyGranule` 一律返回 `True`;`set` 在**被截断**时返回 `True`;`bloom_filter` 对 `like`/`match` 返回 `True`;`MinMaxGranule` 的 `!=` 只在 `min == max == v` 时返回"可跳过";`key_condition` 的 `always_true_in_range` **在非平凡路径上一律返回 `False`**,并且键上**完全没有条件**时返回 `True`(键上无条件,对键而言当然恒真;这是 `_usable_node_count() == 0` 的早返回分支,它让"没有键条件"的情况直接跳过逐行过滤)。除此之外它从不声明"恒真"——那是"可以不做逐行过滤"的强声明,判错会直接产生错误结果,实现选择"只保证正确"。`GRANULARITY` 的落地是 `group_rows = granularity × 平均 granule 行数`(`IndexHelper.build_all`),与 §11.10 的公式一致。

实测一组真实输出:20000 行、`index_granularity = 1024`、`bloom_filter(0.01)` 建在 `url` 上 → 该 part 20 个 mark,写出 `skp_idx_bf.idx` 24145 字节;`WHERE url = '/p/7777'` 返回正确的 1。主键裁剪方面:5000 行、5 个 mark 的 part 上,`WHERE id = 2500` 得到 `MarkRange(2, 3)`(只读一个 granule),`WHERE id > 4000` 得到 `MarkRange(3, 5)`,`WHERE id != 0` 得到 `MarkRange(0, 5)`——最后这个正好演示了 §11.2 说的"`!=` 让索引失效":它产生的两段区间合并后覆盖了全表。

几个必须如实说明的简化:布隆过滤器用 `siphash64` 做双重哈希,**哈希的字节输入与真实 ClickHouse 不同**(代码注释明说),所以同一个值在两个系统里位图不同——这只影响互操作性,不影响各自系统内的正确性。`minmax` 跳数索引存**完整值**而非真实 ClickHouse 的截断前缀,更占空间但更好懂。

**通过 DDL 建跳数索引这条路是通的,建表写法与真实 ClickHouse 对齐。** 在 `CREATE TABLE` 里写 `INDEX` 子句即可:`_resolve_definition` 会先用 `_make_index_description()` 把解析出的 `IndexDefinition` 转成引擎期望的 `IndexDescription`(带 `data_type` 字段),再交给 `MergeTreeData._bind_index_types`,于是建表时写 `INDEX` 子句就能建出索引(下面那组实测就是这么建的)。**简化点**在于:mini 没有 `ALTER TABLE ... ADD/MATERIALIZE/DROP INDEX` 这一族运维语句(§11.15),索引只能在建表时一次定好;真实 ClickHouse 支持事后增改,但要注意 `ADD` 只改元数据、老 part 要再 `MATERIALIZE`。一个可直接跑的完整例子:

```python
import tempfile, minich as ch
from minich.sql import parse_expression_text

ctx = ch.Context(data_path=tempfile.mkdtemp())
ctx.execute("CREATE TABLE t (id UInt64, url String, "
            "INDEX bf url TYPE bloom_filter(0.01) GRANULARITY 1) "
            "ENGINE = MergeTree ORDER BY id SETTINGS index_granularity = 1024")
ctx.execute("INSERT INTO t SELECT number, concat('/p/', toString(number)) "
            "FROM numbers(20000)")
eng = ctx.catalog.get_table("default", "t").engine
# ① 索引落盘:该 part 的目录里出现 skp_idx_bf.idx
# ② 引擎读路径上它真的裁剪 mark(解释器不会把 WHERE 推进存储层,见 §11.13):
for _ in eng.read(ctx, where=parse_expression_text("url = '/p/7777'")):
    pass
print(eng.last_read_events.get("SelectedMarks"))   # 1 —— 20 个 mark 里只读了 1 个
```

另外要提醒:mini-clickhouse 的 `EXPLAIN` **不打印 `Indexes:` 段**(它只输出 `Sorting key`/`Partition key`/`Primary key`),所以 §11.13 里"看 EXPLAIN 的 Indexes 段"这条在 mini 上要换成"看 `system.parts` 里的 `skp_idx_*` 文件 + 直接读 `may_be_true_for_mark_range` 的返回"。

## 11.18 小结

ClickHouse 没有 B+ 树。它有的是三套互补的机制:**稀疏主键索引**(一个 granule 一个键值,靠物理有序二分出一段 mark)、**分区**(整块删除与粗裁剪)、**跳数索引**(给非主键列装的摘要,只能跳 granule)。主键索引只对**排序键前缀**有效,`!=` 和"前缀被不等号打断"都会让后面的键列失效;`ORDER BY`(排数据)与 `PRIMARY KEY`(索引记哪些列,必须是前者的前缀)是两个子句,不写 `PRIMARY KEY` 时它默认等于排序键。一个 granule 的行数**不是固定的 8192**:默认开启的自适应粒度让 granule 在"行数到顶"或"字节到顶(`index_granularity_bytes`,10 MiB)"时就封口,行越宽 granule 越小、索引越大、裁剪越细。跳数索引的铁律是"**允许误报真,绝不允许漏读**",所有索引类型的保守行为都从这条推出:`minmax` 适合递增写入的列,`set` 适合低基数,`bloom_filter` 适合高基数等值(误判率默认 0.025),`ngrambf_v1`/`tokenbf_v1` 适合字符串子串(且只有 `tokenbf_v1` 的整词、`ngrambf_v1` 的 n-gram 才有效)——后两者的 `size` 是**固定字节数**(每块一个,不随块内 key 数自适应),配小了位数组饱和失效、配大了索引反超列数据,`ngrambf_v1` 因为每块的 key 多一个数量级而尤其吃 `size`(参数含义与估算见 §11.8);做全文检索应改用全文索引,它正取代这两种 bloom 索引;它是名单里唯一还在演进的类型——25.3 上叫 `full_text`、由实验开关 `allow_experimental_full_text_index` 把关,后续的 `full_text` → `gin` → `text` 两次改名才落在 25.6(逐版本链条见 §11.9)。官方跳数索引名单是 `minmax`/`set`/`bloom_filter`/`ngrambf_v1`/`tokenbf_v1` 加全文索引 —— **没有 `hypothesis`**(它曾是实验类型,已被上游移除)。名单之外还有两类要认得:实验性的**向量索引 `vector_similarity`**(ANN;是否真·按 granule 裁剪在 25.3 上未确认,见 §11.9),以及仅供诊断、不落地数据的**"假设索引"**(`CREATE HYPOTHETICAL INDEX` + `EXPLAIN WHATIF`)。`GRANULARITY` 决定索引块覆盖几个 granule,值越大索引越小、裁剪越粗。projection 是"透明的另一种排序",数据**按 part**存、老 part 要 `MATERIALIZE` 才有,只服务单表、且带 `FINAL` 的查询用不上,别和物化视图混为一谈。验证索引是否生效,顺序是:先用 `system.data_skipping_indices` 确认**定义**、用 `system.parts` 里的 `skp_idx_*` 文件确认**数据**,再用"开关 `use_skip_indexes` 前后的 `SelectedMarks` 差异"与 `EXPLAIN indexes = 1` 确认**它真的被用上**;而 `force_data_skipping_indices`(以及 projection 的 `force_optimize_projection`)能把"我以为用了索引"变成一条会失败的断言。最后,跳数索引不是二级索引——它只减少"读多少块",从不负责"定位到行"。它的 DDL 与 projection 同构(`ALTER TABLE ... ADD INDEX` 只改元数据,**老 part 必须 `MATERIALIZE INDEX` 才带上索引**,否则查询不会变快);而且索引表达式要与查询谓词**字面匹配**(`INDEX ... lower(url)` 只对谓词里的 `lower(url) = ...` 生效,写成 `url = lower(...)` 就**静默失效**)——`ignore_data_skipping_indices` 是 `force_data_skipping_indices` 的反向开关,用来排查某条谓词到底有没有命中索引。
