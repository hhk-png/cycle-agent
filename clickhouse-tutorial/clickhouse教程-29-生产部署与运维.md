# 第 29 章 生产部署与运维

这一章解决什么问题:前面二十几章都在讲"在一台已经跑起来的 ClickHouse 上,怎么把查询写对、把数据管好"。但真正半夜把人叫醒的,通常不是 SQL 的写法,而是部署形态一开始就选错了、磁盘没算够、升级到一半副本追不上。这一章把"能跑"变成"能长期稳定运行"需要做的决定补齐:先判断你到底需不需要集群,再算硬件和容量,再调操作系统和配置,最后落到备份、恢复、扩容、升级、分层存储和日常巡检。事故清单在 §29.16,可以当值班手册翻。

## 29.1 先问要不要集群

在讨论"怎么搭集群"之前,必须先泼一盆冷水:**大多数 ClickHouse 场景根本不需要集群**。ClickHouse 的单机能力非常强:一台配两块 NVMe、256 GB 内存的机器,在典型的日志/事件分析负载上可以每秒扫描几十亿行、灌入百万行级的数据。如果你的数据总量在几 TB 以内、查询并发不高、又不需要容灾,那么"单机 + 一块大 SSD + 定期备份"就是最优解——它没有分布式协调、没有副本延迟、没有跨分片聚合,运维成本几乎为零。

真正需要集群的信号只有三类:

- **数据量单机装不下**。这里说的"装不下"是压缩后的数据量加上未来一两年的增长,超过了单机可挂磁盘的上限。这时要**分片(sharding)**,把数据切开分散到多台机器。
- **需要高可用**。单机故障意味着服务中断。如果你的查询是面向线上业务的仪表盘,不能容忍任何停机,就需要**副本(replication)**,同一份数据放在多台机器上。副本同时还能分摊读查询的压力。
- **单机吞吐/内存不够**。聚合和 JOIN 吃内存,单机内存再大也有上限;并发查询一多,CPU 更是先到瓶颈。这时可以用副本做读扩展,或者用分片做写和扫描的横向扩展。

反过来说,下面这些理由都**不足以**让你上集群:老板觉得"分布式比较先进"、数据量其实只有几百 GB、查询量一天不到几十次。把单机能解决的问题做成集群,只会换来更多的故障面:网络分区、副本失步、Keeper 抖动、分片间 JOIN 退化。分布式是拿运维复杂度换容量和可用性,没到这个需求就别付这个代价。

至于**托管云**(ClickHouse Cloud 以及各家云厂商的托管服务),它的取舍是另一条轴:你付出更高的单位成本,换来免运维、弹性伸缩和存算分离。适合没有专职 DBA、负载波动大、或者愿意用钱换时间的团队。要注意的是,托管环境里你能调的参数、能看的内核细节都比自建少——第 24 章讲的那些执行计划级别的调优,在托管环境里往往只能靠 SQL 层面完成。

## 29.2 硬件与容量规划

### CPU

ClickHouse 的并行度上限基本由核数决定:`max_threads` 受设置与物理核数共同约束(通常是两者取小,具体默认组合未确认,以目标版本的 `system.settings` 为准)。查询的每个算子在 pipeline 里会切分成多个并行分支,核越多、单查询越快。所以选 CPU 时**核数比单核主频更重要**,而且通常不需要超线程带来的"虚核",物理核更实在。

从核数到"能同时服务几个查询",中间隔着两层换算,写出来才能算:

- **一个查询能用几个核**:上限是 `max_threads`,但**大多数查询吃不满**。只有扫描型的大查询(扫几亿行、带大 `GROUP BY`)才会把并行分支铺满;点查、小查询、被 `LIMIT` 早停的查询往往只用到一两个核。所以别按 `max_threads` 全额预留,要按"你自己的典型查询实际占几个核"估。
- **峰值并发查询数**:要配的是"峰值时同时在跑的查询",不是总请求数。可以从 `system.query_log` 里数某个时间窗内 `event_time` 落在其中的查询行数,或看 `system.metrics` 里 `Query` / `RunningQueries` 一类 gauge 在高峰的读数(第 27 章)。

一个够用的起步式子是:

```
所需物理核数 ≈ 峰值并发查询数 × 每查询平均占用核数
```

"每查询平均占用核数"要用自己的负载去量:先按 4–8 当默认值(典型分析查询的常见区间),上线后用 `system.query_log` 的 `query_duration_ms` 与 `ProfileEvents`(如 `OSCPUVirtualTimeMicroseconds`)反推,再回来修这个数。最后提醒一句:**核数给的只是"CPU 不成为瓶颈"的下限**;磁盘 IO 和内存常常先于 CPU 触顶,所以核数算得再准也不能单独拿它定机型。另外,负载若以"多而小的查询"为主,单核主频的权重会高于核数——堆核只对能吃满并行的大查询有接近线性的收益。

### 内存

内存是 ClickHouse 最容易被低估的资源。以下几件事都靠内存:

- **聚合**:`GROUP BY` 的哈希表,基数越高越吃内存。`max_memory_usage` 是单查询上限,`max_bytes_before_external_group_by` 控制何时落盘。
- **JOIN**:build 侧(通常是右表)默认整体装载进内存,大表 JOIN 大表是灾难。
- **排序 / 去重 / 窗口**:`ORDER BY` 大结果集、`DISTINCT`、窗口函数的帧缓冲都驻在内存里。
- **字典**:每个字典可以整体常驻内存。
- **mark 缓存与其它缓存**:热数据的标记(mark)驻留在 `mark_cache_size` 里;`uncompressed_cache_size`(默认关)与操作系统的页缓存也各占一份。

把它写成可以代入的算式:

```
所需内存 ≈ 最坏并发查询数 × 单查询最坏工作集
          + mark 缓存 + 后台合并/mutation 预留 + 操作系统与其它进程余量
```

三块怎么取:

- **单查询最坏工作集**:要抓"最大的那几条查询",不是平均值。最省事的量法是把这几条查询的 `max_memory_usage` 单独设大、跑一遍压力,看 `system.query_log` 里 `memory_usage` 列的峰值;或直接盯 `system.metrics` 的 `MemoryTracking` 在压测中的最高点。大基数的那次 `GROUP BY` 和最大的那次 `JOIN` 要各抓一次。
- **mark 缓存**:量级是"总 mark 数 × 每条 mark 的字节数"。总 mark 数 ≈ 全部表的总行数 ÷ `index_granularity`(默认 8192),每条约十几到二十几字节。10 亿行的一张表约 12 万个 mark,合起来在个位数 MB——多数负载上它不是内存大头,别为它预留太多。
- **后台合并与 mutation 的预留**:并发的 merge 各自持有读写缓冲,`background_pool_size`(默认 16)越大预留越多;合并的中间驻留难以精确预估,经验上给总内存留 15%–25% 的余量比较稳。

几条边界要一起记住:**并发是乘数**,第一项必须用最坏并发而不是平均并发,否则会在高峰期 OOM;**能落盘的开关要打开**(`max_bytes_before_external_group_by` / `max_bytes_in_join`),它们把"内存不够就崩"换成"内存不够就慢";**进程内存上限要对齐 cgroup 限额**(§29.3),让 ClickHouse 在 OOM killer 之前先自己限流。余量宁可留多不可留少——设小了查询会频繁落盘、变慢,而且没有任何一个参数能把"物理内存不够"补回来。

### 磁盘

ClickHouse 是**顺序写、批批写**的引擎:每次写入落成一个不可变的 part,后台合并再顺序读写大文件。这决定了两个结论:

- **顺序写性能比随机写重要得多**,而顺序写在机械盘上也能跑得不错。真正让 ClickHouse 起飞的是 NVMe:合并、mutation、冷数据读取都受益明显。SSD 不是必须,但 NVMe 的收益在写入密集和合并密集的负载上很实在。
- **RAID 的选择**。如果已经用副本做冗余,本地盘可以做 **RAID0**(纯提升吞吐,不做冗余),因为数据冗余由副本层负责。如果单机就得自己保证不丢数据,用 **RAID10**。**避免 RAID5/6**——校验写带来的写放大,恰恰打在 ClickHouse 最依赖的顺序写上。这是一条被反复验证的经验,不是猜测。

### 网络

如果用了分片或副本,机器之间的带宽就是硬约束:

- 写入端,分布式表(`Distributed`)要把数据从接入节点转发到各分片,网络是瓶颈之一。
- 副本之间,新 part 要在副本间传输(通过 interserver 端口),带宽不足会直接表现为"副本延迟暴涨"。
- 跨分片聚合和 `remote()` 查询会把中间结果拉回发起节点。

经验上,集群内网至少应该万兆起步;`interserver_http_port` 那一路(见第 22 章、第 23 章)最好独占,不要和被业务查询挤在一起。

### 按数据量估算磁盘

一个可用的起步公式是:

```
所需磁盘 ≈ (原始数据量 ÷ 压缩比) × 副本数 × 余量
```

比如你有 10 TB 原始日志,压缩比按 5 倍算,就是 2 TB 压缩后;两副本就是 4 TB;再乘 1.3 的余量(合并时的临时空间、系统表、临时文件、预留水位),约 5.2 TB。

如果手上没有"原始数据量"、只有**行数**,把公式换一个形状一样能算:

```
压缩后数据量 ≈ 行数 × 每行原始字节数 ÷ 压缩比
```

"每行原始字节数"可以按 schema 估(定长列的宽度直接相加,`String` 取平均长度来估),也可以直接量:灌一小批真实数据,用 `system.parts` 的 `data_uncompressed_bytes ÷ rows` 得到实测值。两条口径指向同一个量,关键是别拍脑袋。

公式本身简单,**难的是压缩比那一项**。压缩比高度依赖数据本身的特征和你选的 CODEC(第 20 章):

- 高度重复、顺序性强的数据(带时间戳的日志、有序的 ID),LZ4 常见在 5–10 倍,ZSTD 更高;
- 高基数、近乎随机的数据(随机 UUID、加密哈希),可能只有 1.5–2 倍;
- 特殊编码(`Delta`、`DoubleDelta`、`Gorilla`、`T64`)能把时序数据的比值再抬一截。

所以不要凭感觉填压缩比。正确的做法是:灌一小批真实数据,查 `system.parts` 里的 `data_uncompressed_bytes` 和 `data_compressed_bytes` 之比,用这个实测值去套公式。

余量(那个 1.3)也不能随手填。它要覆盖的不只是"文件系统别写满",而是三块**并存**的空间:一是**合并的瞬时占用**——一次合并要把合并结果和新旧 part 同时留在盘上,峰值可能接近参与合并数据量的两倍;二是**快照与临时文件**——`FREEZE` 的 `shadow/`、`tmp/` 里的中间 part 各占一份;三是**系统日志表**——`query_log` / `part_log` 这类表也在同一批盘上长。磁盘**永远不要用满**:一般留 20%–30% 的水位,水位告警线设在 80% 附近比较稳妥。

### IOPS 与吞吐预算

容量算够了,下一个卡人的是**带宽**与**随机读能力**。ClickHouse 的 IO 分三类,预算方式各不相同。

**顺序扫描(读查询)。** 查询读的是压缩后的列文件,基本是顺序大块读。如果有一条硬性延迟目标,能反推出所需吞吐:

```
所需读吞吐 ≈ 目标查询要扫的压缩后字节数 ÷ 目标延迟
```

例:一条要扫 2 TB 压缩数据的报表,要求 60 秒出结果,就需要 30 GB/s 量级的持续读吞吐——这远超单块 NVMe 的 3–7 GB/s,意味着要么减少要扫的量(更好的分区裁剪 / 排序键 / 预聚合),要么靠多块盘或多个分片并行。这个式子经常先告诉你:**瓶颈不在盘,在数据布局**。

**后台合并(写路径)。** 合并会反复重写数据——一行数据每升一层就被重写一遍,所以落到盘上的总写量是插入量的若干倍。经验上按**插入吞吐的 2–4 倍**预估盘的持续写能力,再留出合并与写入争抢的余量。这个放大倍数取决于分区粒度、写入批次与合并策略,**准确倍数要在目标环境实测(未确认)**,别当成常数。

**随机小读(点查 / 小范围查询)。** 按主键定位要先读索引、再解压命中的 granule,每次都是随机小 IO。所需 IOPS 约等于"查询 QPS × 每查询的随机读次数"。这一档正是机械盘与 NVMe 的分水岭:HDD 只有百次级 IOPS,SATA SSD 在几万级,NVMe 可到几十万级。所以 **HDD 只适合存冷数据或做顺序扫描**——把它放在高并发随机读的位置上,盘会先于 CPU 饱和。

观测面:`system.events` 里的 `ReadBufferFromFileDescriptorReadBytes` / `WriteBufferFromFileDescriptorWriteBytes` 给出进程级的读写字节增量,`system.part_log` 里每条 merge 的耗时说明合并吃了多少带宽,操作系统侧用 `iostat -x` 看 `%util` 与 `await`。这些是**起步预算**,不是精确模型——真实值取决于你的查询形状与合并压力,最终仍以实测为准。

## 29.3 操作系统调优

这些调整没有一条是"玄学",每一条都有明确的失效场景。

- **`vm.swappiness` 调到 1(或更低)**。为什么:ClickHouse 的内存访问模式是"大块、热、频繁访问",一旦被换到 swap 再换回来,延迟会从微秒级跳到毫秒级,整机吞吐崩掉。把 swappiness 压到接近 0 是让内核尽量避免换出。
- **关闭透明大页(THP)**。为什么:THP 会在运行时把 4 KB 页合并成 2 MB 页,合并动作本身有停顿,而且对 ClickHouse 这种自己管理内存池的程序,大页带来的收益不稳定、抖动却很真实。把它设为 `never`。
- **调大 `vm.max_map_count`**。为什么:ClickHouse 会为每个 part 的每个列文件做 mmap,part 和列一多,映射数量很容易超过默认上限(常见默认 65530),超了就直接报错或行为异常。官方给的推荐值是 **`vm.max_map_count = 262144`**(约 26 万);把它写进 `/etc/sysctl.d/` 下的一个 `*.conf` 文件,重启或 `sysctl --system` 后生效,别只敲一次 `sysctl -w`(重启就没了)。
- **调大 `ulimit -n`(`nofile`)**。为什么:每个 part、每个连接、每个后台任务都占文件描述符,默认的 1024 太小,合并一多就"too many open files"。生产上设到几十万量级。
- **`nproc`**。为什么:ClickHouse 按核数起线程池,进程级线程上限要够,否则线程创建失败。
- **文件系统:XFS 与 ext4 的取舍**。ClickHouse 处理的是"数量多、体积中等的文件"(每个 part 每列一个文件),XFS 在这类负载上的扩展性通常更好,官方也更倾向推荐 XFS。ext4 也能用,但在超大目录和大量并发读写下的表现略逊。两者都要注意挂载参数:`noatime` 避免读操作产生写。
- **CPU governor 设为 `performance`**。为什么:省电模式会在负载波动时降频升频,带来延迟抖动;分析型负载要的是稳定吞吐。

除了内核参数,还有几项"进程级"的运维卫生:

- **systemd 的资源上限**。官方的 `clickhouse-server` 由 systemd 拉起,`LimitNOFILE` / `LimitNPROC` 要写在 **unit 文件**里(`/etc/systemd/system/clickhouse-server.service.d/limits.conf` 之类的 drop-in),而不是只在 shell 里 `ulimit`——后者重启就没了。官方包会设一部分,用 `systemctl show clickhouse-server | grep -E 'LimitNOFILE|LimitNPROC'` 确认实际生效值,不够就加 drop-in。
- **cgroup / 容器限额**。在容器里跑时,要给容器留足内存与 CPU;更要紧的是让**cgroup 的内存限额和 ClickHouse 自己的 `max_server_memory_usage` 对齐**——如果 cgroup 限额比 ClickHouse 的软上限还小,内核 OOM killer 会在 ClickHouse 自保之前就把进程干掉,`MEMORY_LIMIT_EXCEEDED` 那条友好路径根本走不到。
- **日志轮转**。服务端自己的日志写到文件时(见 §29.4 的 `<logger>`),要用 logrotate 之类工具轮转。不轮转的日志会把磁盘写满——这是"磁盘无声无息满掉"除了系统日志表之外的另一个常见来源。
- **命令行运维工具**。除了 `clickhouse-client`,还有几个常备工具:`clickhouse-local`(不开服务端、对文件直接跑 SQL,做一次性 ETL)、`clickhouse-compressor`(压缩/解压 `.lz4` 等格式,排查损坏的 part 文件)、`clickhouse-obfuscator`(生成脱敏数据集,第 28 章 §28.10)。把它们放进运维工具箱,很多"要写个脚本"的事一条命令就够。

这些设置应当在部署镜像里固化,而不是"上线后手工改一遍"——手工改的迟早会漂移(见 §29.5)。

## 29.4 配置文件结构:`config.xml` 与 `users.xml`

ClickHouse 有**两个**主配置文件,职责不同,先分清:

- **`config.xml`(服务端配置)**:进程级的东西——监听端口、路径、缓存大小、线程池、日志、集群拓扑、存储策略。改了要 `SYSTEM RELOAD CONFIG`(或重启)才生效。
- **`users.xml`(用户配置)**:用户、设置档(profile)、配额、来源网络限制。**这一层正被 SQL RBAC 取代**(第 26 章),但完全不用 XML 的部署很少——它仍是默认档与出厂账号的落点。

### `config.xml` 里最该认识的块

- **`<listen_host>`**:监听哪些网卡。默认只监听 localhost(§26.15);写 `0.0.0.0` / `::` 才对外。
- **`<http_port>` / `<tcp_port>`**:HTTP(默认 8123)与原生 TCP(默认 9000)端口。
- **`<interserver_http_port>`**:节点间复制/分发端口(第 22 章)。加 TLS 时对应 `<interserver_https_port>`(§26.12)。
- **`<max_connections>`**:TCP 连接数上限;**`<keep_alive_timeout>`**:连接保活超时(秒)。
- **`<logger>`**:文本日志的路径与级别(配 `log` / `errorlog` 文件名、`level`)。它和系统日志表(§27.4)是两套东西——对文件要配轮转(§29.3)。
- **`<mark_cache_size>` / `<uncompressed_cache_size>`**:两个**最常被 mis-size** 的缓存。`mark_cache_size` 缓存主键的 mark,太小会让读频繁回磁盘;`uncompressed_cache_size` 缓存解压后的块,只在 `use_uncompressed_cache = 1` 时生效(该开关默认是关的),所以它常常可以设成 0。两者都别盲目调大,先看命中。
- **`<max_server_memory_usage>`**:进程级内存上限(第 25 章 §25.2)。
- **`<background_pool_size>`**:后台 merge/mutation 线程数,以及 `<background_schedule_pool_size>` / `<background_fetches_pool_size>` / `<background_move_pool_size>` 等专用池(第 25 章 §25.7)。
- **`<query_log>` 等日志表段**:数据保留策略(§27.4)。
- **`<path>` / `<tmp_path>` / `<user_files_path>`**:数据根、临时目录(必须和 `path` 在同一个文件系统,见 §29.8)、`file()` 表函数的根目录。
- **`<users_config>`**:指向 `users.xml`(默认 `users.xml`)。

### `users.xml` 的结构

`users.xml`(及它的 `users.d/*.xml`)是几个并列的段:

- **`<profiles>`**:设置档。每个 profile 是一个命名的设置集合,可带 `readonly`、`min`/`max` 约束;`<default>` 这个 profile 是所有用户的兜底。
- **`<quotas>`**:配额(跨查询累积的限制,第 25 章 §25.5),按 `<interval>` 写窗口与上限。
- **`<users>`**:每个用户一段,引用它用的 `profile` / `quota` / `networks`,以及认证方式。
- **`<networks>`**:允许连入的 IP/网段,可定义在 profile 里、被用户引用。
- **`<default_profile>`**:新用户的默认 profile 名。

### XML 用户与 SQL RBAC 如何共存

`config.xml` 的 **`user_directories`** 段决定"从哪里读用户",而且**可以并存多个来源**:

```xml
<user_directories>
    <users_xml>
        <path>users.xml</path>                     <!-- 只读的 XML 用户 -->
    </users_xml>
    <local_directory>
        <path>/var/lib/clickhouse/access/</path>   <!-- SQL 创建的用户 -->
    </local_directory>
</user_directories>
```

`users_xml` 里的用户是**只读**的(要改得动 XML 并 reload),`local_directory` 里的用户是 SQL 建的、可即时改。查找时按 `user_directories` 里的**先后顺序**匹配,先命中的来源说了算。所以迁移策略通常是:XML 里保留出厂账号作应急通道,新权限全部走 SQL,逐步把 XML 用户迁空。**别在两边同时维护同一个用户**——那会导致"我改了 SQL 里的设置为什么不生效"(XML 那份先生效)。

## 29.5 配置管理

ClickHouse 的主配置是 `config.xml`(服务端)和 `users.xml`(用户与配额)。但**不要直接改这两个文件**,原因是升级时它们会被覆盖,而且多个来源的配置互相覆盖很难排查。

25.3 的加载顺序是:先读 `config.xml` / `users.xml` 里的内容,再按**文件名字典序**依次读取 `config.d/*.xml` / `users.d/*.xml`,后面的覆盖前面的。于是"用配置管理工具生成 `config.d/` 下的小文件"成了标准做法——你把配置切成若干块,每块一个文件,按前缀数字控制覆盖顺序,git 里能 diff,Ansible/Chef 一推就行。

关键配置块的位置和用途:

- **`<remote_servers>`**:定义集群拓扑(分片、副本),供 `Distributed` 表和 `remote()` 使用(第 23 章)。
- **`<macros>`**:定义每台机器在集群里的身份,例如 `{shard}` / `{replica}`。`ReplicatedMergeTree` 的 `ZooKeeper` 路径里写 `{replica}` 时,就是靠它展开成实际副本名(第 22 章)。
- **`<zookeeper>` / `<keeper_server>`**:Keeper 客户端连接串;以及开启内置 Keeper 服务时的服务端配置。
- **`<storage_configuration>`**:磁盘(disk)、卷(volume)、存储策略(policy)的定义,见 §29.13。

用 Ansible 这类工具统一分发时,一个常见的坑是:**每台机器的 `macros` 必须不同**,但其余配置应当完全一致。所以模板里要把 `macros` 留成按主机变量渲染的部分,其余部分用同一份模板。

## 29.6 部署形态

三种形态各有适用面。

**裸机 / 虚拟机。** 最直接,性能最好,IO 可控。生产上的默认选择。VM 的唯一代价是 IO 会被 hypervisor 隔一层,选盘和队列深度要留意。

**Docker。** 适合快速起测试实例或做 CI。生产上用容器也可以,但 `data/` 必须挂到宿主机卷上——写在容器可写层里,重启就丢。

**Kubernetes(`clickhouse-operator`)。** 这里必须讲清楚"**为什么有状态数据库上 K8s 要小心**"。ClickHouse 的可靠性建立在"读写本地盘 + part 不共享"之上,而 K8s 的调度假设是"Pod 可以随便漂移"。要把两者拼起来,至少得解决三件事:

- **本地 PV 与调度**。数据在本地盘上,Pod 就不能漂到没有那块盘的节点上。这需要 `local` 类型的 PV、`nodeAffinity`,并且接受"节点挂了不能自动漂移"这个现实。
- **亲和性**。同一分片的多个副本不能被调度到同一节点,否则节点一挂全挂;`podAntiAffinity` 是必须的。
- **磁盘 IO 与资源**。容器里做 IO 调优受限,宿主机上的 `vm.swappiness`、THP 等设置要么通过特权容器改,要么依赖节点镜像。

结论是:**K8s 能跑 ClickHouse,但它给你带来的弹性收益有限**(数据本身不弹性),运维复杂度却是实打实增加的。如果没有统一的 K8s 平台约束,裸机 + 配置管理往往是更省心的选择。

## 29.7 clickhouse-operator:用 CRD 管理集群

要在 K8s 上跑 ClickHouse,真正落地的做法是用 **operator**——它把"一个 ClickHouse 集群"变成 K8s 里的一个**自定义资源(CRD)**,你声明想要的状态,operator 负责把 Pod、Service、配置、以及 Keeper 都建出来并维持。最成熟的是**Altinity 的 `clickhouse-operator`**(开源);官方侧也有自己的 operator / Cloud 路径(§29.1 提过)。

它定义了两个核心 CRD,一一对应两类组件:

- **`ClickHouseInstallation`(简称 `chi`)**:一个 ClickHouse 集群(分片、副本、模板、配置文件、用户)。
- **`ClickHouseKeeperInstallation`(简称 `chk`)**:一个 Keeper 集群(第 22 章 §22.9 讲过 Keeper 在复制里的位置)。以前 Keeper 常和 CH 挤在同一个 `chi` 里,拆成独立 CRD 是更清晰的部署。

一个最小的 `chi` 长这样(结构示意,字段细节以你装的 operator 版本文档为准):

```yaml
apiVersion: clickhouse.altinity.com/v1
kind: ClickHouseInstallation
metadata:
  name: ch
spec:
  configuration:
    clusters:
      - name: main
        layout:
          shardsCount: 2      # 两个分片
          replicasCount: 2    # 每个分片两个副本
        templates:
          podTemplate: ch-pod
    users:
      admin/password: secret
  templates:
    podTemplates:
      - name: ch-pod
        spec:
          containers:
            - name: clickhouse
              image: clickhouse/clickhouse-server:25.3
              volumeMounts:
                - name: data
                  mountPath: /var/lib/clickhouse
```

**operator 带来的能力**(和"自己写 Deployment"比):

- **声明式扩缩容**:改 `shardsCount` / `replicasCount`,`chi` 里的 `layout` 一变,operator 就按需加/减 Pod。加**副本**它会自动起新实例指向同一 Keeper 路径、自动追平(对应 §29.11 的"加副本最省事");加**分片**仍要你规划数据重分布。
- **operator 管理的配置与升级**:用户、profile、集群拓扑都写进 CR,operator 负责渲染成 `config.d`/`users.d` 并分发——这正是 §29.5 讲的"配置走小文件",只是由控制器代劳。升级时改镜像 tag,operator 滚动更新(仍要遵守 §29.12 的副本顺序)。
- **本地盘 + 反亲和**:operator 把 §29.6 里那些"本地 PV、`nodeAffinity`、`podAntiAffinity`"的样板替你生成,不用手写。

**升级与回滚(operator 版)。** 改 `chi` 里的镜像 tag,operator 会按它自己的滚动策略逐 Pod 替换;**但"逐个副本、永远保留一个好副本"这条纪律仍要你自己确认**——副本数决定了 operator 能做到多平滑,单副本的 `chi` 改镜像等于整体重启一次。回滚同样是改回旧 tag,但和 §29.12 一样,**operator 回滚不了数据**:新版本若已用它自己的格式重写过 part,退回旧镜像可能读不了,所以升级前那份全量备份仍是唯一后路。稳妥做法是在一个非生产的 `chi` 上先把新 tag 跑通,再改生产。

**Keeper 拆分成 `chk` 之后的要点。** 把 Keeper 从 `chi` 里拆成独立的 `ClickHouseKeeperInstallation`(`chk`)是更清晰的部署,但有几件事随之改变。其一,`chk` 要自己声明**副本数**,推荐 3 或 5(奇数、保多数派),并且把它的数据目录(本地盘或 PVC)纳入备份(§29.9)。其二,`chi` 的 `configuration.zookeeper.nodes` 里要**指向 `chk` 暴露出来的 Service**,而不是本机,否则拆开之后 `chi` 找不到协调层。其三,两个 CR 的**升级顺序**沿用 §29.12 的铁律:**先滚 `chk`、确认 Keeper 仍是多数派可用,再滚 `chi`**——反过来会出现"新 server 想用新协议、旧 Keeper 还不认识"的窗口。拆开的好处是 Keeper 的生命周期与 ClickHouse 解耦:升 CH 不必顺带动协调层,升 Keeper 也不必碰数据节点。

**和官方 operator / Cloud 的取舍**:Altinity operator 是自托管场景里事实上的标准,生态与文档最全;官方的 operator/Cloud 路径与托管服务绑定更紧、更省运维但可控参数更少(§29.1)。**无论哪条路,注意一点**:operator 让"起集群"变简单了,但 ClickHouse 的容量、Keeper、备份、扩容这些**数据层面的决定它替你做不了**——operator 管的是"Pod 该不该在",不是"数据该放哪、备份够不够"。别把 operator 当成"上了 K8s 就不用手动运维"的许可证。

## 29.8 数据目录布局

ClickHouse 的数据目录有几个关键子目录,理解它们有助于诊断:

- **`data/`**:数据根目录(`path` 配置项)。默认在 `/var/lib/clickhouse/`。
- **`metadata/`**:每个表的**元数据**(建表语句),通常是 `.sql` 文件。表结构变更是通过重写这里的文件 + 原子改名完成的。
- **`store/<uuid>/`**:实际的 part 数据。目录名是**表的 UUID**,不是表名——所以"直接去目录里找表名"这件事在 25.3 上是不成立的。
- **`preprocessed_configs/`**:服务端启动时把 `config.xml` 加 `config.d/` 合并后落下的快照,排查"我明明改了配置为什么不生效"时看这里。
- **`tmp/`**:临时文件目录(`tmp_path`)。part 在"写完再原子改名"之前的落点就在这里,所以它必须在**同一个文件系统**上——跨文件系统的 rename 会退化成拷贝加删除,原子性没了(这条在 `mini-clickhouse` 的 `storages/merge_tree/disk.py` 里被反复强调)。

相关配置项:`path`(数据根)、`tmp_path`(临时目录)、`user_files_path`(`file()` 表函数和 `File` 引擎的相对根)、`user_directories`(SQL 定义的用户/角色/行策略的存放位置,第 26 章)。

## 29.9 备份

先说最重要的一条:**直接拷贝 `data/` 目录是不安全的**。原因在于 `data/` 目录里既有 part 数据,又有正在被后台合并、mutation 改动的中间状态;还有 `metadata/` 里的表定义。你 `cp -r` 的那一刻,某张表可能正处在"合并把两个 part 换成一个新 part"的中途,拷出来的副本里元数据和 part 状态不一致,恢复时表打不开。备份必须用 ClickHouse 自己感知状态的方式做。

**现代做法:`BACKUP` / `RESTORE` 语句**(22.x 引入,25.3 已可用)。它由服务端执行,自己处理一致性:

```sql
BACKUP TABLE events TO Disk('backups', 'events-2026-10-15.zip');
BACKUP TABLE events TO S3('https://bucket.s3.amazonaws.com/backups/events', 'id', 'key') ASYNC;
```

要点:

- `TO Disk(...)` 用 `storage_configuration` 里定义的盘;`TO S3(...)` 直接写对象存储。
- **`ASYNC`**:备份是异步任务,立刻返回,进度看 `system.backups` 表(注意它是**内存表、进程重启就清空**——真正持久的备份记录在 `system.backup_log`,排障要看那一张,见下)。默认是同步等待。
- **增量**:用 `SETTINGS base_backup = ...` 指向一份已有备份,只备份变化的部分。这是"全量 + 增量"策略的基础。具体参数名与语义请以目标版本文档为准,**部分细节未确认**。
- 备份粒度可以是库、表、分区。

**老做法:`FREEZE` + 硬链接。** `ALTER TABLE ... FREEZE` 会在 `shadow/` 目录下为当前 part 建立硬链接快照(几乎不占额外空间,因为 part 不可变),然后用 rsync 把 `shadow/` 拷走。它在没有 `BACKUP` 语句的老版本上是主流,现在仍可用于"把整个实例冷备走"这种场景。

**逻辑备份:`remote()` + `INSERT SELECT`,以及 `clickhouse-copier`。** 把源表的数据读出来写进目标表。优点是**可跨版本**(只要两边都能解析类型),还能顺便改表结构(重新分区、换排序键)。缺点是慢、吃源库资源。`clickhouse-copier` 是专门做"分片间/集群间搬数据"的工具(第 23 章)。

**备份策略:频率、保留,以及"到底要备什么"。** 三个决定:**多久备一次**(由 RPO 决定——你能容忍丢多少数据)、**保留多久**(最近的几份要能覆盖误操作窗口,比如"任意一天的数据能回到 7 天内任一时点")、以及**备哪些东西**。第三点最容易被忽略——**光备 part 不够**:

- **表数据(part)与表结构(metadata)**:`BACKUP DATABASE` / `BACKUP TABLE` 会一并带上表定义;只拷 part 是恢复不出来的。
- **SQL 创建的访问实体(用户 / 角色 / 行策略 / 设置配置文件 / 配额)**:它们不在任何表里,而是以文件形式落在 `user_directories` 的 **`local_directory` 下的 `access/` 子树**里——默认就在数据根目录下、也就是本章刚警告"不能直接拷"的那个目录旁边(§29.8)。**只拷 part 会丢掉这一整块**:用户、角色、行策略、配额、设置档一起消失,重放完库结构后,查询是按"没有主体、没有约束"在跑,行策略与配额这类安全约束是**静默失效**的,最危险。备法有两条:**(1) 用 `BACKUP ALL`**——它除了库 / 表,还能带上**SQL 建的**访问实体(而 `BACKUP DATABASE` / `BACKUP TABLE` 不带),恢复时由还原设置控制这部分要不要还原——25.3 的口径是 **`structure_only`**(以及 `create_access` 一类:`create_access` 决定"要恢复的访问实体已存在时怎么办");旧稿里提到的 `restore_access_entities` 在 25.3 上**并不存在**——它是更晚版本才加入的一组 `RESTORE` 设置(与 `restore_table_data` / `restore_functions` 同批引入),别照抄新版的参数名;**(2) 单独备份 `access/` 目录**,或者对每个对象导出 `SHOW CREATE USER / ROLE / QUOTA / ROW POLICY / SETTINGS PROFILE` 的 SQL 存进版本库。注意一个边界:走 `users.xml` / 外部目录(如 LDAP)的实体**既不在 `access/`、也不被 `BACKUP` 捕获**,只能随下面的配置备份一起处理;若对不支持导出的访问存储发起备份,会直接报 `ACCESS_STORAGE_DOESNT_ALLOW_BACKUP`。**命名集合(NAMED COLLECTION)也要单独备**:25.3 里它**不在被 `BACKUP` 捕获的访问实体之列**(没有对应的 backup 逻辑,文档的访问实体清单也只列 users / roles / row_policies / settings_profiles / quotas),得靠导出 `SHOW CREATE NAMED COLLECTION` 一类手段另行备份。
- **Keeper 上的元数据**:复制表的队列、分片拓扑等状态在 Keeper 里(第 22 章)。Keeper 自己的数据目录要单独做快照——Keeper 丢了,即使 part 还在,复制表也可能对不上。**具体做法**:Keeper(无论内置 `clickhouse-keeper`,还是 ZooKeeper)把状态放在它的数据目录下,里面是 `snapshots/`(周期性全量快照)与 `log/`(自上次快照以来的事务日志);Keeper 会按 `<snapshot_distance>`(每若干条事务落一次快照,项名以目标版本 Keeper 配置为准)自动写全量快照。你要做的是**周期性地把整份数据目录冷拷走**,而且**优先在一个 follower 节点上做**(停进程或打文件系统快照后再 rsync),别为了备份去动正在协调写入的 leader;`snapshots/` 与 `log/` 两份一起留最稳(只留 `snapshots/` 也能恢复,但会回到最近一次快照的时点;两份都留才能恢复到最新事务)。恢复时把目录原样放回、起进程,节点会从快照加日志重放。丢单个节点不致命(多数派仍在),但**绝不能所有节点的快照与日志同时丢光**。
- **服务端配置**:`config.xml` / `config.d/` / `users.xml`(§29.4)。它们不在 `data/` 里,但拿它恢复一个实例时没有配置根本起不来。这类文件通常走 Git 或专门的配置备份,不和 part 混在一起。

**看备份任务的状态:`system.backups` 与 `system.backup_log`——这是两张必须分清的表。** `system.backups` 只保存**当前进程里**的备份/恢复任务,每行一个,关键列:`id`(备份标识)、`name`、`status`、`error`(失败原因)、`start_time` / `end_time`,以及若干进度/大小列(`num_files` / `total_size` 一类,列名以目标版本 `system.backups` 的实际 schema 为准)。`status` 是一组固定枚举:`CREATING_BACKUP`、`BACKUP_CREATED`、`BACKUP_FAILED`、`RESTORING`、`RESTORED`、`RESTORE_FAILED`、`BACKUP_CANCELLED`、`RESTORE_CANCELLED`——**注意没有单独的 `CREATING`,`CREATING_BACKUP` 才是"正在创建"**;想确认成功就看它有没有走到 `BACKUP_CREATED` / `RESTORED`,失败则看 `BACKUP_FAILED` / `RESTORE_FAILED` 与 `error` 列。

这里有一个**排障时最容易踩的坑**:`system.backups` 是**内存表,进程一重启就清空**。也就是说,"昨晚那次失败的 `ASYNC` 备份"在你重启服务端之后,从 `system.backups` 里**再也查不到了**。真正**持久**的备份记录在 **`system.backup_log`**——它由 MergeTree 支撑、像其它系统日志表一样落盘(受 `<backup_log>` 配置段的 ttl / 大小上限控制,需要 `SYSTEM FLUSH LOGS` 才能立刻看到最新行),逐条记录每一次 `BACKUP` / `RESTORE` 的起止与结果,`status` 用上面那套枚举。**所以规矩是:现跑的任务看 `system.backups`,事后追查与审计看 `system.backup_log`**——只盯 `system.backups` 的人,会在重启之后误以为"从来没有过那次备份"。用 `ASYNC` 发起的备份一定要确认它走到成功状态,而不是"发了就当备好了":备份失败是静默的重灾区,而重启会把唯一的现场从内存表里抹掉。

**什么时候该上第三方工具 `clickhouse-backup`。** 官方 `BACKUP` 已覆盖大多数场景;在下面几种情况下第三方工具(最流行的是 `clickhouse-backup`)才值得引入:要对**整实例**做统一、可调度的备份;要更丰富的**保留/分层**策略(把旧备份搬到冷存储);要在**不依赖服务端版本**的前提下做备份(它是独立二进制);要与既有的备份系统(如 K8s velero、对象存储生命周期规则)集成。代价是多一个外部组件要维护,且它的产物格式与 `BACKUP` 语句不同(不能互相恢复)。**先问"官方 `BACKUP` 够不够",不够再上工具。**

三者怎么选:日常用 `BACKUP` 到对象存储(简单、一致、支持增量);大版本迁移用逻辑备份(跨版本安全);整实例冷备用 `FREEZE`。

## 29.10 恢复:演练比备份更重要

`BACKUP` 的另一半是 `RESTORE`:

```sql
RESTORE TABLE events FROM Disk('backups', 'events-2026-10-15.zip');
```

- **部分恢复单表**:`RESTORE` 天然支持单表粒度,不需要恢复整个实例。这是它相对"冷拷整盘"的明显优势。
- **恢复目标可改名**:可以把一张表恢复到另一个库/另一个表名下,用于比对或补救。
- **跨版本恢复的限制**:`RESTORE` 出来的数据要能被目标版本的表引擎读懂。从旧版本恢复到新版本通常可以,反过来(新到旧)风险大,尤其是用了新类型或新编码的数据。逐条规则以官方文档为准,**跨版本矩阵未确认**。

恢复之后一定要做两件事:核对行数(`SELECT count()` 对源表)、跑一遍关键查询确认语义没变。

**恢复演练:备份的价值只有演练过才算数。** "备份了"和"能恢复"是两回事——真正的问题是"给我一个空实例和这些备份文件,我多久能把服务恢复?"。演练流程要落到可执行步骤:

1. **还原表结构并恢复数据**:`RESTORE ... FROM ...` 恢复到演练环境(可恢复到另一个库名,避免动到生产)。
2. **核对行数与总量**:`SELECT count()` 对源表,再比对 `system.tables.total_rows` / `total_bytes` 的量级是否合理(不要求逐字节一致,但量级不对说明没恢复全)。
3. **跑关键查询**:把业务最常用的几条报表 SQL 在恢复出来的表上跑一遍、和结果比对——**这一步能抓住"行数对了但语义变了"的问题**(排序键、编解码、类型在跨版本时出过偏差)。
4. **验证依赖**:涉及复制表就确认 Keeper 路径、副本能起来并追平;用了字典或 MV,确认它们能加载。
5. **记录耗时**:整个演练花多久,就是真实的 RTO。达不到预期,要么改备份策略(加增量、把备份放近一点),要么明确接受。

建议**定期(比如每季度)做一次全流程演练**,并把步骤写进值班手册。"备份了但从没演练过",是最典型的翻车方式——事故当天才发现恢复不了。

## 29.11 扩容

三种扩容方式,成本完全不同:

- **加副本**:在 `remote_servers`/`macros` 里加一台,起一个新实例指向同一个 Keeper 路径,`ReplicatedMergeTree` 会**自动追平**已有数据——不需要人工搬数据。这是最省事的一种扩容,主要用来提升读吞吐和可用性。
- **加分片**:要把已有数据**重新分布**到新分片,因为分片规则变了。这需要离线规划数据搬迁(第 23 章讲的重分布方法:`clickhouse-copier` 或 `INSERT SELECT`)。这是最麻烦的一种,通常要停机窗口。
- **垂直扩容**:给机器加 CPU、内存、磁盘,改 `max_threads`/`max_memory_usage` 等配置后重启。最简单,但受单机上限约束。

**加一个副本的最小步骤(最省事的一种)。** 以 `remote_servers` 加 `macros` 驱动的手工部署为例:

1. 在新机器上装好**同版本**的 server,数据目录保持空。
2. 在配置里给它**唯一**的 `macros`:`{shard}` 与目标分片一致、`{replica}` 是新名字,并让它连上**同一个 Keeper**。
3. 把新机器加进 `remote_servers` 里该分片的副本列表,并让**所有节点**都能看到这份更新后的拓扑。
4. 起进程,**不要**手工灌数据——`ReplicatedMergeTree` 表会自己从 Keeper 拿到队列、从已有副本拉 part。
5. 盯着它追平:`SELECT database, table, absolute_delay, is_readonly, queue_size FROM system.replicas`;`absolute_delay` 归零、`queue_size` 回落到 0 即完成,期间可用 `SYSTEM SYNC REPLICA` 主动催一把。

**加分片的最小步骤**要麻烦得多,而且**必须先想清楚分片键**:新分片的写入要靠新的分片规则路由,已有数据要离线重分布(第 23 章的 `clickhouse-copier` 或 `INSERT SELECT`),通常需要一个停机窗口——这也是为什么"加副本"和"加分片"的成本差着量级。

## 29.12 升级与回滚

升级的正确姿势是**逐个副本滚动**,永远保留一个"好"的副本:

1. 从某个分片的**一个副本**开始,把它从负载里摘出(或直接停)。
2. 升级、启动,确认能起来、能读。
3. 在这个副本上执行 `SYSTEM SYNC REPLICA db.table`,等它把落后的 part 追平。
4. 确认追平后,再换下一个副本。
5. 一个分片内全部升完,再动下一个分片。

**升级前的准备。** 动任何机器之前:先**做一份全量备份**(§29.9)——它是回滚的唯一后路;再看 release notes 里有没有破坏性变更(尤其是你用到的高级特性);最后确认新版本的**配置兼容性**(新版可能移除某些配置项或改默认值,拿目标版本的二进制先用你的 `config.xml` 校验一遍,不清楚的键会被拒或警告)。

**机械步骤(包管理 / 二进制)。** 用官方仓库时,基本就是 `apt-get install --only-upgrade clickhouse-server clickhouse-client`(或对应发行版命令)后重启;用 tarball 就是换二进制、重启。**Keeper 与 server 的升级顺序**:需要升 Keeper 时,**先升 Keeper、再升 server**——先让协调层是新版本且保持多数派,再滚动升级 server,避免"server 想用新协议、Keeper 还不认识"的窗口。

**能不能跳过大版本?** 官方通常支持**相邻大版本**的升级路径(如 24.x → 25.x),但**跨多个大版本直跳往往不被支持**——中间版本可能有 part 格式或元数据的迁移步骤必须经过。要跨多版,就一个版本一个版本地升。具体支持矩阵以官方升级文档为准(**未确认**)。

**版本里到底编了什么:`system.build_options`。** 升级后想知道"这个二进制是用什么编译的"(开了哪些特性、对应哪个 git 提交),查它——它列出一串编译期开关,排查"为什么这个函数在我这个版本没有"时有用。

**升级后的健康检查查询。** 起来了不等于好了,按顺序确认:`SELECT version()` 看版本对不对;`SELECT * FROM system.warnings` 看有没有启动期警告;`SELECT * FROM system.replicas WHERE is_readonly = 1` 确认副本没变只读;`SELECT * FROM system.mutations WHERE is_done = 0` 与 `SELECT * FROM system.merges` 看有没有积压后台任务;`SELECT count() FROM system.errors` 看错误计数有没有异常攀升。

大版本升级的风险点要提前想清楚:

- **表结构兼容**:新版本对某些类型/表达的默认行为可能变了。
- **`AggregatingMergeTree` 等有状态引擎的状态格式**:聚合状态的二进制格式在跨大版本时可能变化,`-State` 列的数据要能被新版本解析。升级前务必确认官方 release notes 里有没有相关变更。
- **回滚**:滚回旧版本**不保证可行**——如果新版本已经用它自己的格式重写过 part,旧版本可能读不了。所以升级前的备份(§29.9)是回滚的唯一后路,不要依赖"降级二进制"。

## 29.13 多盘与分层存储

`storage_configuration` 把物理盘抽象成三层:

- **disk(盘)**:一块具体存储(本地目录、S3、HDFS……),名字随意取。
- **volume(卷)**:一组盘,可以配 `max_data_part_size_bytes`(单个 part 超过这个大小就不往这个卷写)。注意**配置里的元素名带 `_bytes` 后缀**,而 `system.storage_policies` 里对应的**列名**是 `max_data_part_size` —— 两处差一个后缀,照着列名去写 `config.xml` 会静默不生效。
- **policy(策略)**:一组卷,以及一个 `move_factor`——当某个卷的磁盘用量超过 `(1 - move_factor)` 时,后台开始把数据往下一个卷搬。

在此基础上用 TTL 做**冷热分层**:

```sql
ALTER TABLE events MODIFY TTL ts + INTERVAL 7 DAY TO VOLUME 'cold';
```

配合上面"SSD 卷在前、HDD 或 S3 卷在后"的策略,就得到"最近 7 天在 SSD、之后自动搬到廉价存储"的效果。

**一份完整的多卷分层 `storage_configuration`。** 下面把"SSD 热卷 + HDD 温卷 + S3 冷卷"三层写全(结构示意,字段以你版本为准):

```xml
<storage_configuration>
    <disks>
        <hot_ssd>
            <type>local</type>
            <path>/mnt/ssd/clickhouse/</path>
        </hot_ssd>
        <warm_hdd>
            <type>local</type>
            <path>/mnt/hdd/clickhouse/</path>
        </warm_hdd>
        <cold_s3>
            <type>s3</type>
            <endpoint>https://bucket.s3.amazonaws.com/data/</endpoint>
            <access_key_id>...</access_key_id>
            <secret_access_key>...</secret_access_key>
        </cold_s3>
    </disks>

    <policies>
        <tiered>
            <volumes>
                <hot>
                    <disk>hot_ssd</disk>
                    <!-- 单个 part 超过 1 GiB 就不往这块卷写,直接跳到下一卷 -->
                    <max_data_part_size_bytes>1073741824</max_data_part_size_bytes>
                </hot>
                <warm>
                    <disk>warm_hdd</disk>
                    <max_data_part_size_bytes>10737418240</max_data_part_size_bytes>
                </warm>
                <cold>
                    <disk>cold_s3</disk>
                </cold>
            </volumes>
            <!-- 卷用到 (1 - move_factor),即 90% 就开始往下搬 -->
            <move_factor>0.1</move_factor>
        </tiered>
    </policies>
</storage_configuration>
```

建表时用 `SETTINGS storage_policy = 'tiered'`,再用上面那条 `ALTER TABLE ... MODIFY TTL ... TO VOLUME 'cold'` 把过期数据往冷卷赶。三层里的 `max_data_part_size_bytes` 决定"多大的 part 才配放进这一卷"——它和 TTL 的 `TO VOLUME` 是两条不同的搬运触发路径:前者按 part 大小在写入时就分流,后者按数据年龄在后台搬。

**还有一条属性只管"别把盘写满",而且它不在 policy 里、在 disk 里:** `<disks>` 下每块盘可以写 `<keep_free_space_bytes>`,ClickHouse 会把这部分空间**从可写空间里扣掉**——`system.disks.free_space` 报的就是扣完之后的数。上面 §29.2 反复讲"留 20%–30% 的水位",讲的正是这件事:水位要么靠监控告警(§29.15)靠人来守,要么靠这个属性让**引擎自己**守住。它和 `move_factor` 不是一回事:**`move_factor` 决定"卷用到多少就开始往下搬",`keep_free_space_bytes` 决定"这块盘永远留多少不碰"** —— 前者是搬运的触发线,后者是硬底线,而且它设在 **disk** 上、不在 volume 上,所以对不参与分层的单块盘同样有效(卷选盘时的判据就是"`未预留空间 − keep_free_space_bytes` 还装不装得下这个 part")。发行版模板里 `<keep_free_space_bytes>` 的两处默认都写成 `0`(等于不设防;同一段里作为对照的 `<move_factor>` 示例值是 `0.2`)。有一个例外要记住:配置模板自己的注释里写明了,在给**临时文件**用的那段策略(`<tmp_policy>`)下,`keep_free_space_bytes` **会被忽略**(同一句里被忽略的还有 `move_factor` 与 `max_data_part_size_bytes`,并且要求那段策略**只能有一个卷**)。

**用系统表核对现状。** 配好的策略和盘到底什么样、各用了多少,查这两张表:

```sql
-- 每块盘的路径与剩余空间
SELECT name, path, formatReadableSize(free_space) AS free,
       formatReadableSize(total_space) AS total
FROM system.disks;

-- 每个策略由哪些卷组成、卷里有哪些盘
SELECT policy_name, volume_name, disks, move_factor
FROM system.storage_policies;
```

`system.storage_policies` 的 `disks` 列给出该卷里的盘,`move_factor` 就是那个搬移阈值——这两个正是排查"数据为什么没按预期搬到冷卷"的第一站。

要点:

- **`move_factor` 默认值是 `0.1`**(即卷用到 90% 就开始往下一个卷搬);这个数值随版本可能调整,以 `system.storage_policies` 的实际输出为准,别硬背。
- 搬数据是后台动作(由 §25.7 的 `background_move_pool_size` 驱动),会产生 IO 和临时空间,别把水位线设得太紧。
- 分层存储只解决"存在哪",不解决"查得快不快"——查询命中冷卷时该慢还是慢。
- 后台搬移的事件会记进 `system.part_log` 的 `MovePart`(第 27 章 §27.8),排查"数据没搬走"看它。
- **冷卷一旦是对象存储,就必须同时解决"读"的问题**:MergeTree 的读是大量离散小随机读,压在毫秒级延迟的远端盘上会不可用。补法是在冷盘外面包一层本地缓存盘(`type = cache`),见 §29.14——**"把数据放上 S3"和"把数据放上 S3 还能查"是两件事**,只做完前者等于把查询性能抵押了出去。

## 29.14 对象存储的本地缓存:filesystem cache

§29.13 把冷数据放到了 S3 上,解决了"存得便宜"。但这个决定立刻带来下一个问题:**那读呢?**

对象存储和本地盘之间隔着一个数量级以上的**延迟**——本地 NVMe 是微秒级,对象存储的一个请求是毫秒级。而 MergeTree 的读取姿势恰恰是"**大量小而离散的随机读**":按列、按 granule、按 mark 区间去 seek(§05.13 的第 ④ 步)。把这种访问模式直接压在对象存储上,结果不是"慢一点",而是"慢到不可用"——一次查询里的成百上千次往返,延迟全部叠加。

所以从对象存储上读数据,**必须有本地缓存**。这一节讲的就是它:**filesystem cache**。

**它是什么:包在远端盘外面的一块本地盘。**

配置上它不是单独的组件,而是 **Disk 的一种类型**:`<type>cache</type>` 的盘,内部通过 `<disk>` 指向**被它包住的那块远端盘**。查询看到的是这块缓存盘,读请求先落到本地 NVMe 上,没命中才回源到 S3、拉回来的同时写进本地盘。

```xml
<storage_configuration>
    <disks>
        <s3_plain>
            <type>s3</type>
            <endpoint>https://bucket.s3.amazonaws.com/data/</endpoint>
            <access_key_id>...</access_key_id>
            <secret_access_key>...</secret_access_key>
        </s3_plain>

        <!-- 本地缓存盘:包在 s3_plain 外面 -->
        <s3_cached>
            <type>cache</type>
            <disk>s3_plain</disk>
            <path>/mnt/nvme/clickhouse_cache/</path>
            <max_size>500Gi</max_size>
            <cache_on_write_operations>1</cache_on_write_operations>
            <cache_policy>SLRU</cache_policy>
            <slru_size_ratio>0.5</slru_size_ratio>
        </s3_cached>
    </disks>

    <policies>
        <s3_tiered>
            <volumes>
                <cold>
                    <disk>s3_cached</disk>
                </cold>
            </volumes>
        </s3_tiered>
    </policies>
</storage_configuration>
```

建表时 `SETTINGS storage_policy = 's3_tiered'` 即可。除了上面这几个,还有一组细粒度参数(单段最大尺寸、边界对齐、后台下载线程数、元数据加载线程数、保留空闲空间等),**它们各自的名字与默认值以 `system.filesystem_cache_settings` 的输出为准**(见下文)——这个子系统的配置项还在增补,别照抄某一版的文档。

**缓存的是什么:压缩后的字节段,不是解压后的数据。**

这一点决定了它的价值边界。缓存以**几 MB 一段**(segment)为单位,存的是**压缩状态**的字节——也就是对象存储上那些 `.bin` 文件的原始片段。所以它省下的是**网络往返**,**解压仍然要花 CPU**。含义很清楚:配了缓存之后,瓶颈会从 IO 侧挪回 CPU 侧;它不会让 CPU 凭空变快,只会让 IO 不再是短板。

**默认是读穿透(read-through):第一次访问照样慢。**

缓存的填充是**按需**的:第一次读某个段,先回源把整段拉下来,既给查询用、也写进本地盘;第二次读同一段,直接命中。所以有一条必须记住的推论:**缓存不会让"第一次读"变快,它只让"重复读"变快。** 冷启动后跑的第一条查询依然是慢的。

这条性质决定了它的适用面:**它只能加速有局部性的访问模式**。反复查最近几天、反复扫同一批热分区、反复点查同一批键——这些能显著受益;而"每次查询扫的都是从没读过的数据""纯随机点查"这类负载,缓存帮不上忙,甚至会把本地盘填满却几乎不产生命中。

`cache_on_write_operations` 把默认行为改成**写穿透**:写入时就把刚写下的数据放进缓存。它针对的是一个非常常见的场景——**刚灌完数据马上要验证/查询**,如果等第一次读才回源,那次查询就要付全额网络延迟。代价是本地盘占用上升,还可能缓存了根本不会再读的数据(比如刚写完就被合并掉的 part)。

**淘汰策略:LRU 与 SLRU,选哪个看你的扫描习惯。**

默认是 LRU(最近最少使用)。但 LRU 有一个经典的弱点,恰好是分析型负载的重灾区:**一次大范围扫描会带来一大票"只读一次"的段**,它们把真正的工作集挤出缓存,等扫描结束、下一次要读热数据时,发现全被冲掉了。

SLRU(分段 LRU)正是为此设计的:缓存被分成**试用段**与**受保护段**,新进来的段先进试用段,**再被访问一次才晋升**到受保护段;`slru_size_ratio` 决定两段的比例。效果是**一次性扫描的数据永远进不了受保护段**,冲不掉热工作集。所以取舍很好记:**负载里有大量一次性大扫描、同时又有一小撮反复查的热数据,用 SLRU;访问分布比较均匀、或者热数据会自然被反复访问,LRU 就够。**

**一条最容易踩的语义:`enable_filesystem_cache` 不是开关。**

这是本节最反直觉的一处,也是最常见的一次误判。

`enable_filesystem_cache` 是一个查询/会话级设置,**默认值是 1**,但它**不负责启用缓存**——缓存能不能用,完全由**盘配置**决定(有没有配出 `type = cache` 的盘、表有没有落在用它的存储策略上)。这个设置的真实用途只有一个:**在单条查询里主动绕过缓存**。比如你想量"没有缓存时到底有多慢",或者怀疑缓存给出的数字不真实,就把它设成 0 跑一次。

所以"我明明把 `enable_filesystem_cache` 开着了,怎么没生效"这个问题的答案,**永远**是:**去看盘配了没有**。反过来,"我想让某条查询不走缓存"才轮到它上场。搞混这一点,会在两个方向各错一次。

**观测:三张系统表 + 两组指标。**

`system.filesystem_cache` 是逐条缓存条目的明细。关键列有:`cache_name`(哪一份缓存)、`cache_path`、`key`(被缓存的源对象标识)、`file_segment_range_begin` / `file_segment_range_end`(这一段覆盖源文件的哪个字节区间)、`size`、`state`、`cache_hits`、`references`、`downloaded_size`、`kind`、`segment_type`、`file_size`。其中两处最值得看:

- **`state`** 有 `DOWNLOADED` / `DOWNLOADING` / `PARTIALLY_DOWNLOADED` 三个值。出现 `PARTIALLY_DOWNLOADED` 意味着"这一段的某一部分被读了就停了"——配合 `file_segment_range_*` 一起看,能判断读的粒度是不是太碎(该读的区间和实际拉取的区间对不上时,缓存效率会大幅下降)。
- **`references` 与 `cache_hits`** 是"这块该不该留在缓存里"的**证据**。`references` 高说明这段被反复用到,是真正的热数据;`references` 长期是 1、`cache_hits` 一直是 0 的段,就是在白占空间。

`system.filesystem_cache_settings` 是**当前生效的缓存配置快照**,包含 `max_size`、`max_elements`、`max_file_segment_size`、`boundary_alignment`、`cache_on_write_operations`、`cache_policy`、`slru_size_ratio`、`background_download_threads`、`load_metadata_threads`,以及当前实际占用的 `current_size` / `current_elements_num`。**改完配置想确认生效没有,查这张表**——它比回头翻 `config.xml` 快,也比"重新读一遍配置文件"可靠。

`system.filesystem_cache_log` 是缓存交互的日志,和别的 system 日志表一样需要 `SYSTEM FLUSH LOGS` 才能立刻看到最新行。

指标侧分两组:`system.metrics` 里的 `FilesystemCacheSize` / `FilesystemCacheElements`(缓存的字节数与条目数),以及 `system.asynchronous_metrics` 里的 `FilesystemCacheBytes` / `FilesystemCacheFiles`。监控命中率就是把这两组和查询侧的读量放在一起看。

**两条运维命令。**

`SYSTEM DROP FILESYSTEM CACHE [<name>]` 清空缓存。它最典型的用途**不是优化,而是救急**:缓存盘写满时,写入会报 `CANNOT_WRITE_TO_FILE_DESCRIPTOR` 一类的错误,清一次缓存能立刻恢复。但要记住:清完只是把问题推迟——**真正要做的是回头调 `max_size` 与保留空闲空间**,否则同样的错误过一阵还会来。

`SYSTEM DESCRIBE FILESYSTEM CACHE <name>` 把某份缓存的配置打出来,用来确认服务端**实际**加载到的是什么(排查"配置改了但没生效"时用它对齐)。

**容量怎么定:装下热工作集,而不是装下数据集。**

把 `max_size` 设成"整个数据集的大小"通常是浪费:冷数据本来就不会被读第二次,占着缓存盘只会挤掉真正的热数据,命中率反而下降。合理的起点是"最近被反复查询的那部分数据",然后**用 `system.filesystem_cache` 里的 `cache_hits` / `references` 去校准**——哪些段在被反复读,才值得给它们留空间。

同时要给本地盘留出**空闲空间**(配置项见 `system.filesystem_cache_settings` 里的 `keep_free_space_*` 一族)。缓存盘写满的后果不只是"缓存不工作了",还可能连带影响同一块盘上的其它写入。

**与第 05 章那几块缓存的区别:别混着调。**

这是最容易出错的地方,因为名字里都有 "cache"。§05.8 讲的 `mark_cache_size` / `uncompressed_cache_size` 和本节讲的 filesystem cache **不是一回事**,三者在读路径上处在**不同层**:

- **filesystem cache(本节)**:位于**盘的最下层**,缓存的是**远端盘上的压缩字节**。解决"网络往返太慢"。
- **mark cache**(§05.8):在**内存里**,缓存 `.mrk2` 的索引内容。解决"定位 granule 要读盘"。
- **uncompressed cache**(§05.8):在**内存里**,缓存**解压后**的列块。解决"同一块被反复解压"。

从下往上排:远端对象存储 → **filesystem cache** → 读缓冲 → 解压 → **uncompressed cache** → 送进流水线;而主键索引与 **mark cache** 在旁路供定位。前者是**本地 SSD 上的容量**(改 `max_size`),后两者是**服务端内存**(改 `system.server_settings` 里的 `*_cache_size`)。同一个"调大缓存"的动作,在三处对应的资源、成本与收益完全不同——**先想清楚你要"少走网络"还是"少读盘"还是"少解压",再决定动哪一个。**

**最后一条必须说清的取舍。**

缓存让"存算分离"**变得可行**,但没有让它**变得和本地盘一样**。即使配好了缓存,你仍然要面对三件事:缓存未命中时的高延迟(冷启动、新分区、新查询模式),缓存盘本身的成本与故障率(多了一块盘、就多了一处会坏的地方),以及**多副本各自维护一份缓存带来的命中率摊薄**——每个副本的缓存是**独立**的,同一份冷数据可能被 N 个副本各拉一次,缓存盘的实际利用率因此低于单机直觉。

所以本节的结论不是"配了缓存就可以放心把数据全放 S3",而是:**缓存把对象存储从"不能用于交互式查询"变成了"可以,前提是你的访问模式有局部性"**。真正随机、每次扫不同数据的负载,靠缓存救不回来——那类负载要么把热数据留在本地盘,要么从查询侧改(更粗粒度的预聚合、更少的列),这两条路分别在第 14 章和第 20 章。

## 29.15 日常运维清单

**监控什么:去第 27 章,别在本章重复维护清单。** 指标的选取与解读全部放在第 27 章——`system.metrics` / `system.events` / `system.asynchronous_metrics` 一族的分工、"哪些值得配阈值告警"在 §27.12,"按症状找入口"的速查在 §27.18。本章只负责"看到异常之后怎么处置",不另列一份会过期的指标数组。

**`OPTIMIZE` 要不要定期跑?答案是:不需要。** 这是新手最常见的误区。ClickHouse 的后台合并线程会持续把小的 part 合并成大的 part,`OPTIMIZE TABLE ... FINAL` 只是**强制立刻做一次"合并到单个 part"**——它在大表上可能非常慢、非常吃 IO,而且后台本来就会做到同样的事。`OPTIMIZE` 只在少数明确场景才用:刚灌了一小批数据想立刻收敛成单 part、或者分区有异常 part 需要修复。

**例行巡检项**:磁盘水位是否越过告警线;part 数是否在缓慢异常增长(通常意味着后台合并跟不上写入);副本延迟是否有增长趋势;有没有卡住很久的 mutation 或 merge;`system.errors` 是否在累积。

**磁盘水位处置**:先确认是不是某张表的历史分区没配 TTL 在无限增长;再看有没有临时文件没被清理;都不行再考虑卸掉最老的分区(`ALTER TABLE ... DROP PARTITION`)或把它 `FREEZE` 到慢盘。

**part 数异常增长处置**:查 `system.parts` 找到 part 数最多的表;看是不是写入批次过小(小批高频写入会产生海量小 part);看后台合并是不是被 `max_bytes_to_merge_at_max_space_in_pool` 之类的限制卡住了。

## 29.16 常见事故清单

每条按"症状 → 根因 → 处置"写。

**磁盘满**

- 症状:写入报 `No space left on device`,或者 ClickHouse 进入只读、拒绝写入。
- 根因:没留水位;某分区无 TTL 无限增长;临时文件或 `shadow/` 堆积;合并需要额外空间但已无余地。
- 处置:先腾空间(删旧分区、清 `shadow/`、清 `tmp/`)恢复可写,再补 TTL 和水位告警。这是最快让人停服的事故,所以 80% 水位就得告警。

**too many parts**

- 症状:写入报 `Too many parts (N). Merges are processing significantly slower than inserts`。
- 根因:写入批次太小、频率太高,后台合并的速度追不上小 part 的产生速度。
- 处置:把写入改成**批量**(每次几十万行级别),或者先用 `Buffer` 表/异步插入聚合;不要靠调大 `parts_to_throw_insert` 硬扛,那只是把爆炸推迟。

**副本延迟暴涨**

- 症状:`system.replicas` 里某副本 `absolute_delay` 持续增长,数据落后。
- 根因:副本间网络带宽不足;磁盘 IO 跟不上合并;副本上下文里队列积压(某条 entry 卡住)。
- 处置:看 `system.replication_queue`,找卡住或重试的条目;查网络;查磁盘 IO。带宽不足要单独给 interserver 分流量。网络与磁盘都正常、队列却推不动时,可以手工催一把:**`SYSTEM SYNC REPLICA db.table`** 让该副本主动追平队列(它会等到欠下的 part 拉齐才返回,长队列上会很慢,别当成"立刻见效"的开关)。

**内存 OOM 被杀**

- 症状:进程被内核 OOM killer 杀掉,日志里有 `Killed`,重启后日志停在半路。
- 根因:大 `GROUP BY` 或大 JOIN 把内存吃光;没设 `max_memory_usage` 或配额太宽。
- 处置:给查询加 `max_memory_usage` 和 `max_bytes_before_external_group_by`;把大 JOIN 改成预聚合;降低并发。

**merge 卡住**

- 症状:`system.merges` 里某个 merge 长时间不动。
- 根因:磁盘 IO 瓶颈;参与 merge 的 part 被别的操作占用;合并池大小不足。
- 处置:查磁盘 IO;确认它是不是在等一笔卡住的 mutation(`system.mutations` 里 `is_done = 0` 且 `parts_to_do` 不动的那些)——是的话用 **`KILL MUTATION WHERE database='...' AND table='...'`** 干掉这笔 mutation,别让 merge 陪着它一起耗;确实需要一个安静的维护窗口时,可以 **`SYSTEM STOP MERGES db.table`** 暂停该表的后台合并,处理完再用 **`SYSTEM START MERGES db.table`** 恢复(停太久会让 part 只增不减,恢复后要盯 part 数回落)。

**Keeper 不可用**

- 症状:`ReplicatedMergeTree` 表变只读,日志报连不上 ZooKeeper/Keeper;`Distributed` 表 DDL 失败。
- 根因:Keeper 集群失去多数派;磁盘/网络故障;会话超时设置过小导致频繁重连。
- 处置:Keeper 是复制的单点依赖,必须按集群部署(通常是 3 或 5 节点,第 22 章)。恢复 Keeper 后,副本会自动重新同步,不需要人工干预数据。

## 29.17 启动、part 加载与崩溃恢复

§29.9 讲了"怎么备",§29.10 讲了"怎么恢复"。这一节补上被夹在中间的、值班时最常撞见的一段:**服务端重启时它到底做了什么,以及"没正常关"之后凭什么还能一致。** 它和 §29.8 的数据目录布局、第 05 章的 part 概念直接相连,读的时候对照着看。

**启动时做了什么:扫目录、读元数据。** 服务端起来后,会遍历 `data/`(`<path>`)下的每个表目录,把每个表的 **part 清单**读进内存(§05.2)。数据目录里每个表的存储目录名是**表的 UUID**、不是表名(§29.8),表名与 UUID 的映射在 `metadata/` 的 `.sql` 文件里。读一个 part 要打开它的 `checksums` 与索引文件、拿到行数 / 大小 / 主键范围 / granule 数(§05.4、§05.5)——**这些就是 `system.parts` 里那些列的来源**。所以"启动要多久"基本和"要加载多少个 part"成正比:表多、碎 part 多、`metadata/` 里的定义多,启动就慢。**这也是为什么 part 数失控不只是读慢,还会让重启变成一场灾难**:几万个 part 的表,启动时要逐个打开、校验、登记,期间服务不可用。§25.8 与 §29.15 都把 part 数当巡检项,根子就在这里。

**`detached/` 是什么、什么时候出现、怎么处理。** 每张表的目录下有一个 `detached/` 子目录,存放**被"摘下来"的 part**:它们还在磁盘上,但**不参与查询**,也不在 `system.parts` 的活跃清单里。它通常在几种情况下产生——后台检测到某个 part **损坏 / 校验失败**而主动把它隔离、手工 `ALTER TABLE ... DETACH PARTITION|PART`、或某些修复流程的中间产物。处理姿势是:先 `SELECT * FROM system.detached_parts` 看**为什么**被摘下来(`reason` 列),再决定——确认是垃圾就删掉(`ALTER TABLE ... DROP DETACHED PART`,或对整段分区 `DROP DETACHED PARTITION`),确认数据还要就 `ALTER TABLE ... ATTACH PARTITION|PART` 挂回去。**不要一看到 `detached/` 占空间就 `rm -rf`**:里面可能是"只差一次修复"的数据。

**part 损坏怎么被发现,`SYSTEM` 能修到什么程度。** part 的每个列文件都带**校验和**(写在 part 的 `checksums` 文件里,§05.4)。读取时若校验不过、或文件缺失 / 大小不符,ClickHouse 会判定这个 part 损坏。它能做的是**隔离**(把坏 part 移进 `detached/`、让查询绕开它继续跑),以及**从副本重新拉一份好的**——对复制表,这靠从 Keeper 侧重新同步(见下)。但它**不能**凭空"修"坏掉的数据:校验和存在的前提就是"内容无法被静默篡改后还原",`SYSTEM` 里没有一条命令能让一个校验失败的 part 变回好数据。所以"part 坏了"的正确处置是:先定位它(看 `system.part_log` 里下载 / 校验失败的记录,或启动日志里的 checksum 报错)→ 复制表就从健康副本重新同步;单副本表只能用备份恢复这一部分。`clickhouse-compressor`(§29.3)能帮你**人工确认**某个 part 文件是不是真坏了,但它也只是"看",不是"修"。

**非正常关闭之后,一致性从哪来。** 断电、OOM kill 这类"没走正常关闭流程"的中断,ClickHouse 还能重起来、数据还对,靠的是几条设计(第 05 章 §05.11 讲过的**不可变性**在这里兑现):

- **part 一旦写好就不可变**。写入流程是"先在 `tmp/` 把整个 part 写完 → 再**原子改名**搬进正式目录"(§29.8),所以磁盘上任何一个 part **要么完整存在、要么根本不存在**,不存在"写一半的 part"被读到。崩溃最多丢掉那个还没搬进来的新 part 所代表的**这一批插入**,不会让已有 part 变半截。
- **元数据也是原子落盘**。建表 / 改表写在 `metadata/` 的 `.sql` 里,同样是"写临时文件 + 原子改名",所以表定义不会处于"改了一半"的状态。
- **协调状态放在 Keeper 里**(复制表)。"该有哪些 part、队列排到哪"记在 Keeper,重启后副本拿本地 part 清单与 Keeper 的共识一比对,缺什么补什么(第 22 章)。所以非正常关闭的复制表重启后**能自愈**:本地领先 / 落后于 Keeper 共识的部分会被对齐,而不是"谁最后写谁对"。

需要留意的边界:上面保证的是**"存在即完整"**,不保证"最近一次写入一定还在"——崩溃点之后还没落稳的那一批插入,丢就丢了。这正是 §28.2.1 强调"攒批"、以及第 22 章 §22.5 强调"插入去重"的动机之一:只有做到幂等,重试那些可能没落稳的批次才是安全的。

**`SYSTEM RESTART REPLICA` 与 `RESTORE REPLICA` 的选择。** 两者都用于"复制表的本地状态与 Keeper 对不上"时,但适用面不同:`RESTART REPLICA` 是**谨慎**的一档——让副本重新连 Keeper、重新拉队列,本地已有 part 保留,通常无损,是"Keeper 抖动过、或副本状态可疑"时的第一步;`RESTORE REPLICA` 是**激进**的一档——用于本地元数据已经坏了(表打不开、或本地 part 清单与 Keeper 严重不符)时,它把本地状态**按 Keeper 的共识重建**(本地多出来的 part 可能被判为垃圾)。两者各自的精确语义、前置条件与风险,第 22 章 §22.10 已经展开,**这里不重复**;你只要记住那条区分:**"疑似状态可疑"先 `RESTART`,"本地表已坏、要以 Keeper 为准重建"才 `RESTORE`**。

**启动慢,怎么查。** 常见原因按概率排:一是 **part 太多**(上面讲的启动扫描量);二是 **表 / 分区太多**(§28.6.1,每个分区都要登记元数据);三是 **`metadata/` 里堆积了大量历史定义**(反复建删表);四是**磁盘慢或冷**,首次读 part 元数据时的随机 IO 拖长;五是 **Keeper 慢或不可用**,复制表在启动时等协调层响应。诊断入口:看服务端启动日志里"加载表 / part 花了多久"的那几行;起来之后对照 `system.parts` 的 part 总数与 `system.tables` 的表数(§27.2、§27.3),再回到 §29.15 的巡检项。治本方向通常落到**写入侧**(别碎片写、别过度分区),而不是"给启动加线程"。

## 29.18 小结

生产运维的核心判断,其实只有一个:**先判断需不需要分布式**。单机 + 大 SSD + 定期备份能覆盖大多数场景;只有容量、可用性或吞吐真正越界了,才值得付出集群的复杂度。

在这之上,几件事有明确的"对/错":容量规划要按公式算而不是拍脑袋——磁盘用"原始量 ÷ 实测压缩比 × 副本数 × 余量"(只有行数时换成"行数 × 每行字节数 ÷ 压缩比"),内存用"最坏并发 × 单查询最坏工作集 + 缓存与合并预留",CPU 用"峰值并发 × 每查询占用核数",再给顺序吞吐与随机 IOPS 单独留预算,而压缩比与合并放大倍数都要实测不要猜;操作系统调优里 swappiness、THP、`max_map_count`、`ulimit` 每条都有失效场景;配置必须走 `config.d/`/`users.d/` 而不是改主文件;备份用 `BACKUP`,绝对不要直接拷 `data/`;升级要滚动、保留好后路;`OPTIMIZE` 不需要定期跑。把 §29.16 的事故清单贴到值班文档里,比背参数更有用。另外记住重启这一段(§29.17):启动要多久基本由 part 数决定,`detached/` 不是垃圾堆,而"断电后还能一致"靠的是 part 不可变加原子改名——这些决定了你遇到"重启慢 / 表打不开 / 副本状态可疑"时该怎么下手。
