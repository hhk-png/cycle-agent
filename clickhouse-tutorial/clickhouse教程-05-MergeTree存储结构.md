# 第 05 章 MergeTree 存储结构

同一张一亿行的表,`SELECT count(*)` 几毫秒返回,`SELECT * WHERE id = 42` 也要几毫秒,而 `SELECT max(price)` 可能比前两者都慢——这些差异不是运维调出来的,是**数据在磁盘上的排布方式**决定的。这一章把 MergeTree 的 part 拆开看:一个 part 目录里有哪些文件、每个文件回答什么问题、`primary.idx` 与 `<col>.mrk2` 里到底存了什么。读懂这些,你就能在看执行计划之前,先在心里估算一条查询"该读多少字节"。

## 05.1 读性能来自三层裁剪

ClickHouse 的读路径上没有"全表扫描"这个词的立足之地,因为它有三层互相独立的筛选,一层比一层精细:

```text
WHERE ts >= '2024-03-01' AND id = 42
   │
   ├─ ① 分区裁剪    用 PARTITION BY 表达式,整块丢掉不相关的分区目录
   │                 粗,但省得最多(一次扔几百 GB)
   ├─ ② 主键裁剪    在 primary.idx 上二分,得到一段连续的 granule(mark 区间)
   │                 细,决定"读哪几个 8192 行"
   └─ ③ 列裁剪      只打开 SELECT/WHERE/ORDER BY 用到的列的 .bin 文件
                     横向,决定"每段读几个文件"
```

三层里,**前两层是物理基础的直接结果**,第三层是列存的红利。读懂本章,你就能解释:为什么 `WHERE id = 42` 只碰一个 granule,为什么给一张表加一个不用的列几乎不影响 `count(*)`,以及为什么宽表(几千列)在小范围查询上反而占便宜。

## 05.2 part:写入的最小单元

一次 `INSERT` 在 MergeTree 里产出的是一个 **part**。它是一个目录,里面是这一批数据的全部列文件与索引文件。part 有两条铁律:

**第一,part 一旦提交就是不可变的。** 写入的流程是"先写进临时目录 `tmp_insert_*`,全部写完再**原子改名**成正式 part 名"。改名的原子性意味着崩溃恢复极其简单——磁盘上任何 `tmp_` 开头的目录都是上次写到一半留下的垃圾,启动时删掉即可(`MergeTreeData._load_existing_parts` 里就是这么做的)——并且读路径可以**无锁并发**:一个长时间运行的查询持有某个 part 的引用,后台合并写出新 part、把老 part 标记删除,查询读到的仍是老 part 的完整数据。这条写入路径的完整拆解(为什么必须先写临时目录、跨文件系统 rename 为什么会退化)见第 08 章 §08.1–§08.2。

**第二,part 内部按排序键有序。** 写 part 前会先按 `ORDER BY` 排一次序。这条保证了后面两个关键能力:`primary.idx` 可以二分(有序才能二分的),多个 part 可以用 k 路归并(各自有序才能 O(N log k) 合并,而不是 O(N log N) 重排)。

## 05.3 part 名的四个字段

part 的目录名形如 `202403_5_5_0`,四个下划线分隔的字段分别是:

```text
202403_5_5_0
│      │ │ │
│      │ │ └── level:合并层级,每 merge 一次 +1
│      │ └──── max_block:这个 part 覆盖的最大 block 号
│      └────── min_block:最小 block 号
└───────────── partition_id:分区键取值的序列化文本
```

`partition_id` 是分区键的值(这里是 `toYYYYMM(ts) = 202403`);没有 `PARTITION BY` 时它就是字符串 `all`。

**`block` 号最容易看不懂**,它不是"有几个数据块",而是"这个 part 覆盖了输入流的哪一段"。每次 `INSERT` 领一个**递增**的 block 号,一个 part 记录自己是由哪些 block 合并来的,于是 `min_block..max_block` 唯一标识了一批数据。它的用途是**幂等**:客户端重试 `INSERT` 时,服务端靠 block 号识别"这一段我写过了",直接把重复的 part 丢掉,这就是 ClickHouse 宣称"至少一次写入也能得到不重复数据"的机制。

但要补一句**默认条件下才成立的限定**:这套去重**默认只对 `Replicated*` 表生效**。普通(非复制)`MergeTree` 表上,负责它的设置 `non_replicated_deduplication_window` **默认是 `0`**,也就是**关着**——把同一个 block 重放一遍,表里会真的多出一份数据。想让非复制表也去重,得显式把 `non_replicated_deduplication_window` 调成大于 0(它记的是"最近多少个 block 参与判重"),代价是一份常驻内存的去重记录。所以"至少一次写入得到不重复数据"这句话,在 25.3 上请读成"**复制表默认如此,普通表默认不如此**";确认的方式是查 `system.merge_tree_settings` 里这个设置在你实例上的实际取值。

**`level` 是合并层级**,合并结果的 level 等于源 part 中最大的 level 加一。它的作用是让合并策略倾向于**只合并层级相近的 part**:两个 `level=0` 合并成 `level=1`,一个 `level=1` 与一个 `level=0` 合并成 `level=2`。这样数据被"一层层均匀地重写",而不是某一个小 part 被反复卷进各种合并里(那叫写放大,第 18 章细讲)。

## 05.4 part 目录:每个文件回答一个问题

一个 wide part(每列独立文件)的目录长这样,以 `ORDER BY id`、`PARTITION BY toYYYYMM(ts)` 为例:

```text
202403_1_1_0/
  columns.txt        有哪些列、什么类型            ← 打开 part 的第一件事
  count.txt          多少行                        ← count() 不用扫数据
  partition.dat      这一整块的分区键取值
  primary.idx        稀疏主键索引(每 granule 一条)
  id.bin   id.cmrk2   id 列的数据 + 每个 granule 在文件里的位置
  ts.bin   ts.cmrk2   ts 列……
  url.bin  url.cmrk2  url 列……
  minmax_id.idx      列级 (min,max),分区/part 裁剪用
  minmax_ts.idx      分区键列也有
  skp_idx_<name>.idx 跳数索引(建了才有)          ← 第 11 章
  checksums.txt      每个文件的大小 + 128 位哈希   ← 自检时核对这些哈希,不是 CRC32
  serialization.json 每列的序列化类型(Default/Sparse),不是压缩 codec
  uuid.txt           part 的唯一 ID
  metadata_version.txt          part 元数据布局的版本号
  default_compression_codec.txt 列没单独写 CODEC 时的默认压缩 codec
  ttl.txt            TTL 元数据(声明了 TTL 才有)
  mutation_<n>.txt   已应用到本 part 的 mutation 记录(ALTER 过才有)
  txn_version.txt    事务版本(启用事务性写入时才有)
```

这张清单的关键在开头那几行:**前四个文件加起来只有几 KB,却回答了"这张 part 有哪些列、多少行、分区值是什么、键范围是什么"。** 所以 ClickHouse 启动时能在几秒内"认识"几百万个 part——它只读这些 KB 级的元数据文件,一行数据都不碰。`load_part()` 的注释把这批文件列得很清楚。

**这里先给 `checksums.txt` 补一条勘误。** 早先的讲法把它描述成"每个文件的大小与 CRC32"、一份能直接读懂的文件清单,这两点都不对。真实记录里,每个文件存的是:`file_size`、一个 **128 位**的 `file_hash`(由 `HashingWriteBuffer` 按 CityHash128 算出,不是 32 位的 CRC32);若该文件是压缩的,再各存 `uncompressed_size` 与 `uncompressed_hash`,用来核对**解压之后**的数据。落盘形式也不是文本清单:文件开头是一行**文本头** `checksums format version: N\n`,紧跟的是**二进制 + 压缩**的记录体(`MergeTreeDataPartChecksums::write` 写出的是 version 4,记录体走 `CompressedWriteBuffer`)。所谓"能逐行读懂的文件清单",只属于很老的 part —— 读取器至今仍兼容 version 2 那种纯文本形式,但 25.3 写出的 part 不长这样。所以自检时是拿这些 **128 位哈希**逐文件核对,不是"核对 CRC"。

后半段那几行容易被忽略,却各有明确职责。**先把 `serialization.json` 说准:它记的是每列的"序列化类型"(`Default` / `Sparse` 两种布局策略),不是压缩 codec** —— 编解码器在 `default_compression_codec.txt` 与列自身的元数据里(§05.15 会再对上一次)。(25.3 上这个枚举**只有这两个取值**,`src/DataTypes/Serializations/ISerialization.h` 里写着 `DEFAULT = 0, SPARSE = 1`;更新的版本才又加了 `Detached` / `Replicated` 之类。)

**变长列在盘上不止一条"流",这条机制值得单独说。** 一个 `Array(String)` 会被拆成一条"每个数组有几个元素"的 `.size0` 子流加一条真正放元素的 `.data` 子流,`Map(K,V)` 拆成 `keys` 与 `values` 两条(各自还可能带 `.size0`),`Nested` 的每个子列同理。好处是直接的:`arr.size0`(数组长度)、`m.keys`(Map 的键)这类查询只读那一条**窄**子流,不必把整列数据都解开——**这也正是 MRK3 相对老的 MRK2 要优化的地方**:MRK2 把"整列"当成一条流、mrk 里只有一套位置,MRK3 让每条子流各有一份 mark,于是"只读某条子流"才有可能。`default_compression_codec.txt` 是列上没写 `CODEC` 时的兜底压缩算法;`metadata_version.txt` 是元数据布局的**版本号**(升级后新旧布局的 part 能靠它共存);`mutation_<n>.txt` 记录哪些 `ALTER UPDATE/DELETE` 已经作用到这个 part(第 18 章);`txn_version.txt` 只在启用事务性写入时出现。**这份清单不是一个封闭集合**——它随版本与 `SETTINGS` 变化,两个方向都要留意。一个方向是**变少**:compact part 用一个 `data.bin` + `data.cmrk3` 取代全部 `.bin`/`.cmrk2`(见 §05.10),而历史上出现过的 `delete-on-destroy.txt` 标记文件早已不再创建 —— 上游 PR #49181(`get_rid_of_delete_on_destroy_marker`)把它标为 deprecated,源码里的注释写得很直白:"It is no longer being created, only is removed",随后读取它的那段逻辑也被删掉。这次改动远早于本书对标的 25.3,所以 25.3 上不会有它。另一个方向是**变多**:上游后来往 wide part 里加过一个 `columns_substreams.txt`(把上面那条"一列拆成几条子流"的事实用文件固化下来,好让读取器不必靠猜)—— 它是 **25.3 之后**才合入的,25.3 的 part 目录里没有这个文件,上面那条子流事实在这一版里仍然成立、只是没有单独一份清单文件记录它。**所以这类清单要当成"心里有数的一批文件",而不是"磁盘上只可能有这些文件";以你那一版实际的 part 目录为准。**

`minmax_<col>.idx` 不是每列都有:只有**分区键列**和**主键列**才有。原因也很直白——分区裁剪只关心分区键列,part 级裁剪关心主键列,其它列的极值没人用。

## 05.5 granule 与 mark:索引与读取的最小单位

part 里的数据不是一行行存,而是切成一段段的**连续行**,每一段叫一个 **granule(粒度)**。切分的边界由 `index_granularity` 决定,默认 **8192 行**——也就是说从第 0 行起,每 8192 行算一个 granule(最后一个通常不满)。

granule 是"读写的最小单位"。要读第 5 行,你没法只读第 5 行,只能把第 0~8191 行那一整个 granule 读出来再丢掉多余的。这个"放大"是所有索引裁剪精度的上限:**索引能裁到 granule,裁不到行**。

每个 granule 在**每个列文件**里的位置,记在 `<col>.mrk2` 里,一条 mark 是一个三元组:

**先说清文件名** —— 这里有个容易踩空的地方。MergeTree 的表级设置 `compress_marks` **自 23.5 起默认就是 `true`**,而 marks 文件的扩展名是从它推出来的:`w="wide"` 时压缩版叫 **`.cmrk2`**、不压缩版才叫 `.mrk2`,compact part 则是 `.cmrk3` / `.mrk3`。所以**在 25.3 的默认配置下,你在 part 目录里看到的是 `id.cmrk2`,不是 `id.mrk2`**。本章下面为了可读性统一写作 `.mrk2`,读到真实目录时请按这条换算;要复现下面那个"字节数正好对得上"的实验,得先把 `compress_marks` 设成 `0`,否则文件是被压过的,尺寸不会等于 `条数 × 24`。

```text
(offset_in_compressed_file, offset_in_decompressed_block, rows_in_granule)
```

前两个偏移是必须的,因为压缩块与 granule 不是一一对应的。真实的 ClickHouse 会把**多个 granule 塞进同一个压缩块**(`min_compress_block_size` 默认 64 KiB、`max_compress_block_size` 默认 1 MiB),这样块头开销和"解压一次"的固定成本被摊薄。代价是定位一个 granule 需要两步:先按 `offset_in_compressed_file` 找到压缩块,再从 `offset_in_decompressed_block` 这个**解压后**的字节偏移开始读——第一个偏移解决"块在哪",第二个偏移解决"块里的哪一截"。第三个字段 `rows_in_granule` 是这一段的实际行数,**它不是常数**(最后一段不满,自适应粒度下更是段段不同),任何"granule 大小恒等于 8192"的假设写进代码都会出 bug。

`.mrk2` 里每条 mark 是三个 64 位无符号整数(24 字节)。这不是巧合,可以用一个实验直观验证:`ORDER BY id`、`index_granularity = 1024`、插入 5000 行,`ceil(5000/1024) = 5` 个 granule,此时 **`id.mrk2` 正好是 `5 × 24 = 120` 字节**,而 `primary.idx` 是 `5 × 8 = 40` 字节(每个 granule 一个 64 位键值)。**做这个实验前记得 `SETTINGS compress_marks = 0`**(或在建表时写进 `SETTINGS`)——按上面那条,默认是压缩的,你看到的会是 `id.cmrk2` 且尺寸对不上 120;只有关掉压缩,MARK 文件才是一条条裸的 24 字节记录,那个乘法关系才成立。这本身就是个有用的对照:**`120` 与"压缩后明显更小"两个数之间的差距,就是 marks 文件被压缩省下来的量。**

## 05.6 主键与稀疏索引

`primary.idx` 存的是**每个 granule 第一行的排序键值**。5000 行、1024 行一 granule 时,它里面是 `[(0), (1024), (2048), (3072), (4096)]`——五行数据,覆盖五千行的表。这就是"**稀疏**"的含义:索引不是每行一条,而是每 granule 一条,条数只有行数的 1/8192。一个百万行的 part,索引可能就一百多行,小到能常驻内存。

查找时的动作是"**在索引上二分,得到一段,再顺序读**":

```text
WHERE id = 2500   (granule = 1024)
   │
   ├─ 在 primary.idx 上二分:2500 落在 (2048) 与 (3072) 两个键之间
   ▼
   mark 区间 [2, 3)   ← 只读第 2 个 granule(行 2048..3071)
   │
   ├─ 查 id.mrk2 的第 2 条 → (压缩偏移, 解压偏移, 1024)
   ▼
   解压这一段,逐行过滤,挑出 id == 2500 的那一行
```

二分的边界推导要注意两处细节:下界是"第一个键值 ≥ lo 的 mark **减一**"(因为前一个 granule 的末尾行也可能满足条件),上界是"第一个键值 > hi 的 mark"(它以及之后的都不必读)。`id = 2500` 得到的是**一个** mark,而 `id > 4000` 得到的是 `[3, 5)`——两段 mark。这也暴露了稀疏索引的代价:**granule 内部必须线性扫**。索引帮你跳过大段无关数据,但在命中的那一 granule 里,过滤仍是逐行的。

## 05.7 自适应粒度:granule 什么时候不按行切

前面一直说"每 8192 行(或 1024 行)一个 granule",这个简化只在**行窄**的时候成立。行一宽,它就崩:假设一行的 `payload` 有 100 KB,8192 行就是 800 MB 一个 granule——稀疏索引会粗到几乎不起作用(一张 1 亿行的表,一个 granule 只覆盖全表的约 0.008%),而且"读一个 granule"意味着解压 800 MB 再丢掉 99.99%。

所以真实 ClickHouse 默认按**字节**切,而不是按行切:`index_granularity_bytes` 默认 **10 MiB(10485760)**。一条 granule 在"攒够 `index_granularity` 行"或"攒够约 10 MiB 未压缩数据"里**先到先切**。行窄时,8192 行远不到 10 MiB,于是按行切(和固定粒度一致);行宽时,几百行就够 10 MiB,于是按字节切。这就是**自适应粒度(adaptive granularity)**。

它改变的是稀疏索引的**密度**,也就是"裁剪精度 ↔ 索引体积"这条换算关系:

- 窄行表:granule ≈ 8192 行,索引条目数 ≈ 行数 / 8192,`primary.idx` 与 `.mrk2` 都很小。
- 宽行表:granule ≈ 10 MiB,条目数 ≈ 总字节 / 10 MiB。一张 1 TB 的宽表,索引条目约十万条——还是小到能常驻内存(§05.8),但比"按 8192 行"多了几个数量级。

代价是**读放大的粒度变粗**:一次命中要解压的是"约 10 MiB 未压缩数据"这一段,而不是"8192 行"。对宽表来说这仍是净赚——读 10 MiB 远好过读 800 MB。

有几个配套旋钮要认得。`index_granularity_bytes` 默认 **10 MiB(10485760)**,它是一段 granule 的**字节上限**;把它设成 `0` 表示**退回固定粒度**(只按行数切),官方文档明确写着不推荐。`min_index_granularity_bytes` 默认 **1 KiB(1024)**,它**不是**"granule 的最小字节数",而是一道**下限 / 安全阀**:官方给的定义就是"一段 granule 允许的最小字节数,用来防止误把 `index_granularity_bytes` 设得过低"。至于它和 `index_granularity_bytes` 之间**确切的比较语义(是小于还是小于等于)、以及它在什么条件下才真正拦下建表,本条未确认**——官方文档只给了上面那句定义,没写判定条件,请以你所用版本的 `system.merge_tree_settings` 与实测为准。`use_const_adaptive_granularity`(默认 0)可以强制整个 part 统一用常量粒度,`enable_index_granularity_compression`(默认 1)则在内存里压缩这些粒度值。

**实践含义**:表里经常存长 `String`/`JSON`/`Array` 时,别再用"一个 granule 8192 行"心算裁剪精度——`system.parts` 里的 `marks` 会告诉你真实条目数,`marks` 明显小于 `rows / 8192` 就说明自适应粒度生效了。窄行表上,两个机制几乎不可区分。

## 05.8 缓存层:让索引"留在内存"的其实是这几块内存

前面反复说"稀疏索引小到能常驻内存"。这件事其实分成两条不同的路径,混起来讲就说不清"我明明把 `index_granularity` 调小了,为什么查询没变快";而比这更常见的误读,是以为主键索引始终被某个 5 GiB 的上限保护着——这一节要把这句话纠正过来。

**第一条是主键索引的驻留 —— 而且要先纠正一个很容易讲反的地方:它默认不是"attach 时就装进来"的。** 25.3 的 MergeTree **表级**设置 `primary_key_lazy_load` 默认是 **`true`**(`src/Storages/MergeTree/MergeTreeSettings.cpp`: "Load primary key in memory on first use instead of on table initialization")。也就是说 part 加载时**并不**读 `primary.idx`;索引是在**第一次真的要用它**时(第一次需要按主键裁剪的查询)才被载入,此后**一直留着**。所以准确的表述是"**懒加载 + 载入后常驻**":不是"命中才留"(命中后它会保留),也不是"attach 时就装"(attach 时不会)。这个区别有可观测的后果:`system.parts.primary_key_bytes_in_memory` 在一张刚 attach 完、还没被查过的表上是 **0**,查过一次之后才变成索引的实际体积。把它设成 `false` 就恢复成"表初始化时全量载入"的老行为 —— 那是给"启动后立刻要跑大量点查"的场景用的,代价是启动变慢、且所有索引的内存立刻计账。`system.parts` 里的 `primary_key_bytes_in_memory` 记录的就是它占了多少 RAM。**关键在:默认情况下这些内存既不进缓存、也不被任何上限约束**——它是一份普通的常驻结构,不是"放不下就淘汰"的缓存。这一点由**表级**设置 `use_primary_key_cache`(**默认 `false`**)决定:只有当你在**某张表**上显式把它设成 `true`,这张表的主键索引才改走缓存路径,此后才受**服务端**设置 `primary_index_cache_size`(默认 **5 GiB**,策略 `SLRU`,`primary_index_cache_size_ratio` 默认 0.5)的约束。

所以有一个必须说清的后果:**默认配置下,主键索引占用的 RAM 不由那个 5 GiB 约束。** 一个很宽的排序键(几十列、或长 `String`)会把每个 part 的键值都压在内存里,单表悄悄钉住几个 GB 而不触发任何淘汰——你以为有 5 GiB 兜底,其实那道兜底是 `use_primary_key_cache = true` 之后才生效的,而且它是**按表**开的。真正约束这份内存的手段因此有三类:一是**把主键截短**(§11.2.2 用 `PRIMARY KEY` 只覆盖排序键的一段前缀,键窄了索引就小);二是**显式给这张表打开 `use_primary_key_cache`**,让它走可淘汰的缓存;三是运维动作——`SYSTEM UNLOAD PRIMARY KEY` / `SYSTEM LOAD PRIMARY KEY`(按表把主键索引从内存卸下/装回),以及 `prewarm_primary_key_cache`(默认 `false`)与 `min_bytes_to_prewarm_caches`(默认 0,即不预热)控制的"写入/合并产生新 part 时,要不要顺手把它的索引预热进缓存"。宽键大表上,是这几个名字在管内存,而不是 §05.6 那句"索引很小"。

**第二条是 mark 与解压块的缓存。** 与主键索引不同,`.mrk2` 的内容(mark)**不是一加载就驻留**,而是走一组**按需的 LRU 缓存**:读路径每次定位 granule 都要查 mark,命中就省掉一次磁盘 IO、不命中才落盘读。所以这块缓存的命中率直接决定"主键裁剪"能不能白省一次 IO——通常它是收益最大的一块。

这一族里有两块 mark 缓存,名字分别是 `mark_cache_size` 与 `index_mark_cache_size`(都默认 5 GiB):前者缓存普通列的 `.mrk2`,后者服务**跳数索引**(`skp_idx_*`)的 mark 与 granule 内容(第 11 章);角色相同,只是服务的对象不同。跳数索引还另有一块**独立的解压缓存** `index_uncompressed_cache_size`(**服务端**设置,默认 **0**,即关闭)——**它和普通列的解压缓存 `uncompressed_cache_size` 是两块不同的东西**,别混。

**这里有一个必须纠正的认识**:上面这些 "cache_size" 设置**不是 `system.settings` 里那种能用 `SET` 在运行时会话里改的东西**——它们是写在 `config.xml` 里的**服务端级设置**,只出现在 `system.server_settings` 里。也就是说,要改它们得改服务端配置并让它生效(多数靠重载配置即可,是否需要重启随具体项与版本而异,**以你那一版本机 `system.server_settings` 的说明为准**),而不是在 SQL 里 `SET` 一下。

`uncompressed_cache_size` 是这组里最特殊的一个:它缓存**解压后的列块**,而且**默认基本用不到**——除了它自身的取值,还要查询级设置 `use_uncompressed_cache`(**默认 0**)显式打开才会生效。它的默认值在文档里**有两种口径**:随包 `config.xml` 模板里给的是 8 GiB(8589934592),而"不写时"的代码默认接近关闭,两种说法相冲突——**一律以本机 `system.server_settings` 的取值为准**。它的甜点是"同几个热块被反复读"(高频点查),纯分析型负载上通常没必要开,开了反而与操作系统页缓存重复。

怎么调:

- 先**量**,再**调**。`system.metrics` 的 `MarkCacheBytes`、`UncompressedCacheBytes` 给出当前占用;`system.events` 的 `MarkCacheHits`/`MarkCacheMisses`(以及 `IndexMarkCacheHits`/`IndexMarkCacheMisses`)给出命中率。命中率长期偏低、占用又顶到上限,才是该加的信号。
- **主键索引要先量它到底占了多少**:加总 `system.parts.primary_key_bytes_in_memory` 才是真实读数。只有对打开了 `use_primary_key_cache` 的表,`primary_index_cache_size` 才是它的上限;否则这个数字只由"主键有多宽 × part 有多少"决定,机器内存不够时先想到 `SYSTEM UNLOAD PRIMARY KEY` 或截短主键。
- 目标不是"越大越好",而是**装下热表的索引**。mark 缓存服务的对象本身很小(§05.6),`mark_cache_size` 的 5 GiB 默认值对绝大多数单机场景够用——真正会失控的是上面那条主键索引,而不是这几块 LRU。
- 这几块缓存**互相独立、也互相竞争内存**,还和查询缓冲区、合并缓冲区、操作系统页缓存抢同一池 RAM。把 4~8 GB 的机器照搬默认值,结果就是几块都装不下、互相驱逐,再叠上 swap。这组设置的名称与默认值在新版本里调整过,**以本机 `system.server_settings` 为准**。

一句话:"稀疏索引小"解释的是"为什么值得常驻";但主键索引默认**根本不是缓存**、也**不受 `primary_index_cache_size` 约束**(要走缓存路径得**按表**打开 `use_primary_key_cache`),mark 与解压块才靠按需 LRU(`mark_cache_size` / `index_mark_cache_size` / `uncompressed_cache_size`,以及跳数索引专属的 `index_uncompressed_cache_size`)——这两条路径不是一回事。

## 05.9 列裁剪

`SELECT id FROM hits WHERE id = 42` 只会打开 `id.bin`——`url.bin`、`ts.bin` 根本不会被打开。这不是"读进来再丢",而是**从文件系统层面就不读**。这是列存相对行存最大的红利:表有 100 列而你只用 3 列,IO 大致只剩 3%。

实现上要注意一个细节:列裁剪的"需要哪些列"是**请求的列 ∪ 谓词的依赖列 ∪ 家族列**。`SELECT count() FROM hits WHERE url = 'x'` 里 `url` 没出现在结果里,但谓词要用,所以必须读;`ReplacingMergeTree` 的版本列即便用户没选,合并去重也要读。把"谓词依赖列"漏掉,就会出现"过滤结果对不上"的诡异 bug。

**它和 `PREWHERE` 是先后关系,不是一回事。** 列裁剪决定"这一列要不要被打开";`PREWHERE` 决定"用少数几列先把行筛掉、再去读剩下的列"(§07.5)。两者叠在一起才是完整收益:先用便宜列(`PREWHERE`)筛掉 99% 的行,再对存活的行裁剪出真正要读的宽列——所以 `PREWHERE` 里的列越省、过滤率越高,列裁剪省下的 IO 越多。反过来,若过滤发生在宽度量上、又没下推到 `PREWHERE`,那一整列就还是得读。

**`count()` 为什么快得离谱?** 因为 `SELECT count() FROM t` 既不用读数据列、也不用 `primary.idx`——part 目录里那个只有几行的 `count.txt`(§05.4)就写着这个 part 有多少行,把各活跃 part 的行数一加就得到答案。这正是"列裁剪到极致"的样子:**需要的列集合是空集,一个 `.bin` 都不碰**。(`count(col)` 则不同,它要读 `col` 判断非 `NULL`,走的是正常读路径。)

想确认列裁剪到底生效没有,有三条现成的观测手段:

- **`EXPLAIN indexes = 1`** 打印 `ReadFromMergeTree` 步,里面有 `SelectedParts`/`SelectedMarks`(裁掉了多少)、各索引段(MinMax/Partition/PrimaryKey)的命中情况,以及这一步实际要读的列。
- **`system.query_log`** 里一次查询的 `read_rows`、`read_bytes`,以及 `ProfileEvents` 列里的计数,能还原这次读取碰了多少字节。同一张表、只改 `SELECT` 的列集合,对比 `read_bytes` 就能看出列裁剪省了多少。
- **`ProfileEvents`**(`SYSTEM FLUSH LOGS` 后在 `system.query_log` 的 `ProfileEvents` 列里查)还有 `ReadCompressedBytes`、`OSReadBytes`、`RealTimeMicroseconds` 之类的计数,用来区分"裁剪没生效"和"裁剪生效了、但命中的段本身就很大"。

一条实践提醒:验证列裁剪时要**显式点名少数几列**——`SELECT *` 会把所有列都拉进来,`read_bytes` 自然下不去。

## 05.10 compact part 与 wide part

part 有两种物理格式。**wide part** 每列一个 `<col>.bin` 文件——列裁剪友好,但文件多。**compact part** 把全部列塞进**一个** `data.bin`,只有 `data.bin` + `data.mrk3` 加几个元数据文件。

为什么要 compact?因为小 part 太多会摧毁文件系统:一次小 `INSERT` 如果产生 100 个小文件,几十万次插入就是几千万个文件,目录遍历、inode、打开文件数全部爆炸。compact 把"每列一个文件"压缩成"整个 part 一个文件",小 part 的元数据开销小了一个数量级。

切换由两个阈值决定:

```text
rows  < min_rows_for_wide_part  或  bytes < min_bytes_for_wide_part
    → compact(全部列挤进 data.bin)
否则
    → wide(每列一个 <col>.bin)
```

真实 ClickHouse(开源发行版)里 `min_bytes_for_wide_part` 默认 **10 MiB(10485760)**、`min_rows_for_wide_part` 默认 **0**,即"未压缩数据小于 10 MiB 的 part 用 compact"。(Cloud 版把 `min_bytes_for_wide_part` 的默认抬到了 1 GiB,口径不同,这也是"照抄别人的参数失效"的又一例。)

**"compact part 长大会自动转 wide"这句话必须补上机制,否则它会误导你以为 part 会被就地改写。** 真相是:**part 不可变,根本没有"原地长大"这回事** —— compact 与 wide 的抉择发生在**每一次写出 part** 的那一刻,而不是在 part 存活期间。写出一个 part 只有两个来源:一次 `INSERT`(产出一个新 part),或一次**后台合并**(把若干老 part 归并成一个新 part)。无论哪种,引擎都在动手前拿"**这个即将写出的 part 的未压缩数据有多大**"去和上面两个阈值比,据此决定新 part 用哪种格式。所以所谓"compact 转 wide"的真实过程是:一个小于 10 MiB 的 compact part 被卷进一次合并,而合并**产出的新 part** 超过了 10 MiB,于是这个**新 part 直接以 wide 格式落盘**,原来那个 compact part 随后被标记删除 —— **旧 part 没被改,是新 part 用了新格式**。反过来,wide 也可能"变回"compact:如果一次合并的输出因为 TTL 删行或 `ReplacingMergeTree` 去重而大幅缩小、掉到 10 MiB 以下,或用建表后改过的阈值重写,新 part 照样会是 compact —— 抉择永远只看"**这次写出来的 part 有多大**",与老 part 是什么格式无关。

由此得到一条对上机实测至关重要的推论:**随包默认(10 MiB / 0 行)之下,大多数新写入的 part 一开始就是 compact** —— 因为一次普通 `INSERT` 很难一次就写够 10 MiB。而 compact 会让"每列一个文件"的经典目录、以及按列 seek 的读放大实验全部失去意义。所以那些"量 `id.mrk2` 是不是 120 字节""看单列文件里的偏移""按列裁剪省了多少字节"的实测配方,**都得先 `SETTINGS min_bytes_for_wide_part = 0` 把 part 强制成 wide 才作数**——这正是"做实测前先把阈值归零"这条经验动作的由来。

注意 compact 的代价:**列裁剪失效**——所有列都在一个文件里,读一列也得碰这个文件(所以 compact 只对小 part 有意义)。

## 05.11 不变性:所有性能特征的根源

把前面几条串起来,会看到一条贯穿全章的约束:**part 写了就不改。** 于是:

- **物理层面**的删数据只能整块进行:删**整个分区**(`DROP PARTITION`)或丢掉**整个 part**,是 O(1) 的元数据操作。想按行删就只能绕开"就地改"这条路——要么走轻量级删除(把命中行在一个掩码列上标掉、物理清理仍留给后台合并,第 18 章 §18.9),要么靠 mutation 重写 part(下一条);
- `ALTER UPDATE/DELETE` 不能就地改,只能"读出来、改完、写一个新 part,再把老 part 标记删除";
- `ReplacingMergeTree` 之类的"最终一致"语义,靠后台 merge 慢慢收敛,而不是写入时立即生效;
- 读路径敢做无锁并发、敢让查询持有老 part 的引用,前提正是"没人会改它"。

反过来,正因为不可变,才需要**merge**:`INSERT` 只不停地产生新 part,把"很多小 part"收敛成"少数大 part"是后台合并的职责。第 18 章整章都在讲这条流水线。

## 05.12 虚拟列:调试 part 级问题的探针

排查"这张表底下躺着哪些 part、某个 part 是不是被删过、这一行来自哪个 part"时,不需要加索引也不需要加列——MergeTree 自带一组**虚拟列(virtual column)**。它们不在 `DESCRIBE TABLE` 里、不占存储、不出现在 `SELECT *` 的结果里,只在查询里**显式点名**时才求值:

```sql
SELECT _part, _part_index, _partition_id, count()
FROM hits
GROUP BY _part, _part_index, _partition_id
ORDER BY _part_index;
```

常用的几个:

- **`_part`** —— 这一行来自哪个 part,取值就是 part 目录名(`202403_1_1_0`)。它回答"这一行现在是哪个 part 的一份",但**不保证唯一**——同一批数据在合并前后可能同时被多个 part 覆盖。
- **`_partition_id`** —— 分区 ID(`202403`;没有 `PARTITION BY` 时是 `all`)。
- **`_partition_value`** —— `PARTITION BY` 表达式的**取值(一个元组)**,也就是分区键"本来算出来是什么"。它和 `_partition_id` 是一对:后者是分区键序列化后的目录名(`202403`),前者是那个值的真身;分区键是元组表达式(比如 `PARTITION BY (toYYYYMM(ts), region)`)时,`_partition_id` 把两段拼成一个字符串,而 `_partition_value` 直接把两段各自的值给你——想确认"这一行到底归到哪个分区、按什么算的",用它能省掉对 `_partition_id` 的反解析。
- **`_part_index`** —— 这个 part 在**本次查询结果**里的序号(0,1,2…),按 part 被读取的顺序编号。它只对"这次查询"有意义,换一条查询可能不同。
- **`_part_uuid`** —— part 的唯一 ID,只有打开 MergeTree 设置 `assign_part_uuids` 才会赋值(25.3 上该设置默认 `false`,即默认取到的是空 UUID)。
- **`_sample_factor`** —— 这条查询所用的**采样系数**,只在建表写了 `SAMPLE BY`、且查询用了 `SAMPLE` 子句时才有意义。`SAMPLE` 只读了一部分 granule,拿到的聚合值是"样本口径";用它就能把样本还原成整表口径。**这里的方向不能弄反:它存的是采样率的倒数,不是采样率本身。** 源码里就是 `used_sample_factor = 1.0 / relative_sample_size` —— `SAMPLE 0.1` 得到的 `_sample_factor` 约等于 **10**,`SAMPLE 1/10` 同样约等于 10。所以把样本折算回全表,**是乘不是除**:`count() * _sample_factor`(样本里 100 行 × 系数 10 ≈ 全表 1000 行)。本书第 07 章 §07.13 给的也是这个乘法口径,两处一致(勘误:本节早先写的是除法,那会得到"样本的样本",是错的)。

还有两个与删除机制绑定的:

- **`_part_offset`** —— 行在 part 内的偏移(第几行)。它主要服务于新的 patch-part 更新路径,普通的"改一行"用不到它。
- **`_row_exists`** —— **轻量级删除(lightweight delete)的掩码**:`1` 表示这行可见,`0` 表示已被 `DELETE FROM ... WHERE ...` 标记删除、但还没被后台合并物理清掉。只有当这个 part 上确实有行被轻量删除时,该列才出现。查询里可以手动 `PREWHERE _row_exists` 把"逻辑已删、物理还在"的行过滤掉,看清表里到底还留着多少行数据。

虚拟列的典型用途是**对账**:

```sql
-- 每个分区下每个 part 的行数
SELECT _partition_id, _part, count() AS rows
FROM hits
GROUP BY _partition_id, _part
ORDER BY _partition_id, _part;
```

把它和 `system.parts` 的 `rows` 对一遍,能立刻看出"查询看到的总行数"和"part 元数据声称的行数"是否一致——轻量删除会让二者不一致,这是正常的。虚拟列的取值来自 part 元数据与读取位置,不要求额外的表列;但具体某项开销多少,**以 `EXPLAIN` 为准**。

## 05.13 读路径的完整链路

把上面全部串成一条流水线:

```text
read(WHERE ts >= '2024-03-01' AND id = 42)
  │
  ├─ ① 列裁剪    算出需要读的列 = {id, ts}(谓词依赖也算了进来)
  ├─ ② 分区裁剪  PartitionPruner 用分区键表达式筛出要读的分区
  ├─ ③ 逐 part:· KeyCondition 在 primary.idx 上二分 → mark 区间
  │             · 与跳数索引筛出的区间求交(may_be_true 为假的整段丢掉)
  ├─ ④ 对选中的 mark:按列 seek 到对应的压缩块,解压
  ├─ ⑤ 逐行过滤  把 WHERE 落在解压出来的行上
  └─ ⑥ 切块送进流水线
```

每一步"能裁掉多少"要心里有数:分区裁剪一次能扔掉几百 GB,主键裁剪能精确到 mark,列裁剪决定读几个文件。三层里**任何一层失效,都会让 IO 上一个数量级**,这也是第 28 章"反模式"反复回到这里的根本原因。

## 05.14 怎么观察

**`system.parts`** 是查看 part 状态的主入口。`rows`/`marks` 让你一眼看出 `marks ≈ ceil(rows/8192)`;`bytes_on_disk` 与 `data_compressed_bytes` 用来算压缩率;`level` 反映数据被合并过几次;`part_type` 是 `Wide` 还是 `Compact`;`active` 区分"当前有效"与"已被旧 part 覆盖、等待回收"。排查"表里到底有几个 part"要始终带上 `active`。

**`EXPLAIN`** 看裁剪结果。真实 ClickHouse 的 `EXPLAIN indexes = 1` 会在 `ReadFromMergeTree` 步里打印 `Indexes:` 段,逐项列出 `MinMax`/`Partition`/`PrimaryKey` 的 condition、命中前的 parts/granules 数与**命中后的数**(这就是 `SelectedParts`/`SelectedMarks` 的来源)。

**`clickhouse-client --send_logs_level=trace`** 能看到读取期间到底打开了哪些 mark、读了哪些字节。当 EXPLAIN 显示"裁剪得很少",trace 日志能帮你确认瓶颈是在分区、主键还是跳数索引上。

**`system.part_log`** 是 part 级别的**事件日志**:`event_type` 取 `NewPart`(新写入)、`MergeParts`(合并完成)、`MutatePart`、`MovePart`、`RemovePart`、`MergePartsStart`、`MutatePartStart` 等。前提是服务端配置里开了 `part_log` 段——官方的默认 `config.xml` 里就带着这一段,所以随包安装的实例默认是开着的(配置里若去掉了它,这张表就不存在);表在第一次向 MergeTree 表写入后才出现。它比 `system.parts` 多出来的信息是**时间与因果**:这个 part 是哪次 merge 产出的、`merged_from` 是哪些源 part、`merge_reason` 是普通合并还是 `TTLDeleteMerge`/`TTLRecompressMerge`、`duration_ms` 花了多久、`peak_memory_usage` 峰值内存多少。查"为什么半夜磁盘 IO 飙高"时,直接按 `event_type = 'MergeParts'` 聚合 `duration_ms`,就能定位到是哪张表的合并在跑。

**`system.parts_columns`** 把 `system.parts` 的每一行再**按列拆开**:一个 part 有几列就有几行,列名放在 `column` 里(**不是** `column_name`,这是最容易写错的一处),额外的元信息分两层——part 级的 `data_compressed_bytes` / `data_uncompressed_bytes`,以及真正的**列级** `column_data_compressed_bytes` / `column_data_uncompressed_bytes`。想知道"这个 part 里到底是哪一列占了大头""`marks` 是不是突然变多",要用的正是带 `column_` 前缀的那一对;`system.parts` 上的总量回答不了这个问题。列名随版本增删过,**以你那一版 `DESCRIBE system.parts_columns` 的输出为准**。

**`CHECK TABLE db.t`** 主动校验一个表的 part 完整性——按 `checksums.txt` 逐文件核对**128 位哈希**(不是 CRC32;§05.4 已勘误),报出损坏的 part。它是一次真实的读盘操作,大表上不便宜,所以只在**怀疑损坏**时用(例如查询报 `CHECKSUM_DOESNT_MATCH`,或合并后结果诡异)。复制表上损坏的 part 可以先 `ALTER TABLE ... DETACH PART` 摘掉、再由副本自动重新拉取(第 22 章)。

## 05.15 与 mini-clickhouse 的对照

配套实现 `mini-clickhouse/minich/storages/merge_tree/` 里,`part.py`(约 2066 行)与 `marks.py`(568 行)对着这一章写。本节分"对齐点"与"简化点"两部分。

**对齐点。** mini 的 `Mark` 三字段在**磁盘上的排布**与真实 MRK2 一致 —— 每条 mark 三个小端 `UInt64`(`struct.pack("<QQQ", ...)`,24 字节)。不过有个结构上的细节要说准:真实的 `MarkInCompressedFile`(`src/Formats/MarkInCompressedFile.h`)**本身只有两个字段**(`offset_in_compressed_file` 与 `offset_in_decompressed_block`,16 字节),第三个值(该 granule 的行数)**不是这个结构体的成员**,而是紧跟在它后面单独追加的一个 `UInt64`;`MergeTreeIndexGranularityInfo::getAdaptiveMrkSizeWide()` 之所以是 `sizeof(UInt64) * 3`,就是因为这两块加起来。mini 把它们合并成一个三字段的结构体写了下去 —— **省掉了这层区分,但落盘字节完全一样**,所以按 24 字节读是两边都成立的。另外要注意 mini 写的是**未压缩**的 `.mrk2`,而真实默认是压缩的 `.cmrk2`(见 §05.5 那条),两者尺寸不可直接比。`index_granularity` 默认 8192、`index_granularity_bytes` 默认 10 MiB、`min_index_granularity_bytes` 默认 1 KiB;`IndexGranularity` 同时实现了固定粒度与自适应粒度;`primary.idx` **列优先**存放(先写完第一个键列的所有 granule 值,再写第二个),与真实 `loadIndex` 的顺序一致;part 名解析(`parse_part_name`)的四个字段与错误消息形状也照搬。实测一组输出:`index_granularity=1024`、插入 5000 行 → `system.parts` 显示 `marks = 5`、`primary.idx` 40 字节、`id.mrk2` 120 字节;`primary_index` 读回来是 `[(0,), (1024,), (2048,), (3072,), (4096,)]`;`WHERE id = 2500` 经 `KeyCondition` 得到的 mark 区间是 `MarkRange(2, 3)`。目录清单是:`checksums.txt`、`columns.txt`、`count.txt`、`partition.dat`、`primary.idx`、`serialization.json`、`uuid.txt`,加上每列的 `<col>.bin`+`<col>.mrk2`,以及分区键列/主键列各自的 `minmax_<col>.idx`(`minmax_url.idx` 不会生成,因为 `url` 既不是分区键也不是主键)。

**简化点。**

- **一个 granule 一个压缩块。** `part.py` 里每个 granule 独立调一次 `write_compressed_block`,所以 `.mrk2` 第二列 `offset_in_decompressed_block` 恒为 0;真实 ClickHouse 会把多个 granule 打包进一个压缩块,那一列才有意义(`marks.py` 的模块注释标出了这个差异)。
- **compact 默认关闭。** `DEFAULT_MIN_BYTES_FOR_WIDE_PART = 0`、`DEFAULT_MIN_ROWS_FOR_WIDE_PART = 0`,即默认永远 wide(不是真实 ClickHouse 的 10 MiB)。要触发 compact 得显式设这两个 setting(实测把 `min_bytes_for_wide_part` 调到 100 MiB 后,3 行的小表产出的 part 目录变成 `data.bin` + `data.mrk3` 加几个元数据文件)。
- **`data.mrk3` 不是真实 MRK3 格式。** 真实格式用 varint 组合的列 substream 表 + delta 编码,mini 用一个直白布局,不能与真实 CH 互读。
- **`minmax_<col>.idx` 布局不同。** 真实 CH 是"整个 min 列一次序列化 + 整个 max 列一次序列化",mini 逐 granule 加长度前缀(`varint(len) + min + varint(len) + max`)。
- **读列时读整个 `.bin` 文件。** `part.read_column` 先 `read_file` 把整列文件读进内存,再按偏移解压需要的块;真实 CH 用带 seek 的 `ReadBuffer` 只读需要的字节。
- **写进 `serialization.json` 而非单独文件。** 真实 CH 的编解码器存在 `default_compression_codec.txt` 与列的元数据里,mini 把 `codec` 收进 `serialization.json`(`load_part` 里就是从这里读回的)。
- **没有一批元数据文件。** §05.4 里那些文件——`metadata_version.txt`、`columns_substreams.txt`、`mutation_<n>.txt`、`txn_version.txt` 等——mini 一概不产出,因为它没有实现 MRK3 子流表、mutation 记录与事务版本这些机制;compact 分支里它用一个 `data.bin` + `data.mrk3`。
- **没有索引/解压缓存。** §05.8 的 `mark_cache_size`、`primary_index_cache_size`、`index_mark_cache_size`、`uncompressed_cache_size`、`index_uncompressed_cache_size`(以及按表决定主键索引走不走缓存的 `use_primary_key_cache`)在 mini 里都不存在:它读列时把整个 `.bin` 读进内存,无所谓缓存命中率。
- **没有实现** projection、`SAMPLE BY`(`StorageFeatures` 里 `sampling=False`)、`ALTER ... MOVE/REPLACE PARTITION`;`FREEZE` 用**拷贝**代替真实 ClickHouse 的**硬链接**——它真的会在 `shadow/<backup>/` 下建出 part 的一份完整拷贝,不是空操作(第 09 章 §09.7 会展开)。

## 05.16 小结

MergeTree 的读性能建立在三层裁剪上:分区裁剪用 `PARTITION BY` 扔掉整块数据,主键裁剪用 `primary.idx` 的稀疏索引(每 `index_granularity = 8192` 行一个键值)二分出 mark 区间,列裁剪只打开用到的列的 `.bin`。`marks` 里存的是每个 granule 的 `(压缩偏移, 解压偏移, 行数)`;`.mrk2` 的第二个偏移只在"多个 granule 共用一个压缩块"时才有意义。part 是不可变的写入单元,名字里的 `level` 给合并策略提供"只合并相近层级"的依据,`min_block..max_block` 给**复制表**提供写入幂等(普通 `MergeTree` 的 `non_replicated_deduplication_window` 默认为 `0`,即默认不去重)。宽行表靠自适应粒度(`index_granularity_bytes` 默认 10 MiB)避免 granule 撑爆,`min_index_granularity_bytes` 只是防止把 `index_granularity_bytes` 设得过小的一道安全阀。小 part 用 compact 格式(`min_bytes_for_wide_part` 默认 10 MiB)省文件数,代价是列裁剪失效。

两件容易被忽略的事支撑着上面这套机制。其一,**"索引常驻内存"要分两条路**:主键索引默认**懒加载(`primary_key_lazy_load` 为 `true`,首次用到才载入)然后常驻**,而且默认它**不归缓存管**、也**不受 `primary_index_cache_size`(5 GiB)约束**——那是**按表**打开 `use_primary_key_cache` 之后才生效的上限,默认配置下一条宽排序键能把单表钉住几个 GB(想收回就 `SYSTEM UNLOAD PRIMARY KEY` 或截短主键);而 mark 与解压块走**按需 LRU**——`mark_cache_size`、`index_mark_cache_size`(各 5 GiB)、`uncompressed_cache_size`(取值以本机 `system.server_settings` 为准,且要 `use_uncompressed_cache = 1` 才生效)以及跳数索引专属的 `index_uncompressed_cache_size`(默认 0)。它们都是 `config.xml` 级的**服务端设置**、不在 `system.settings` 里,不能用 `SET` 改;调它们的依据是 `system.events` 里的命中率,不是"越大越好"。其二,**排查 part 级问题时先想到虚拟列**(`_part`、`_partition_id`、`_partition_value`、`_part_index`、`_sample_factor`、`_part_offset`、`_row_exists`)与 `system.part_log`/`system.parts_columns`/`CHECK TABLE`,它们能直接回答"这行来自哪个 part""这一行归到哪个分区、按什么算的""合并花了多久""这个 part 坏没坏"。

**"写后不改"这条约束,是后面所有特性的根源**——`ALTER` 要重写 part、合并要后台跑、家族引擎只能最终一致,全部由它推导而来。
