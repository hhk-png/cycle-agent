# 第 22 章 复制与 Keeper

一台 ClickHouse 服务器宕机,你存在上面的数据就全没了——没有生产系统能接受这个单点。复制(replication)解决的就是这件事:同一份数据在多个节点上各存一份,挂掉一个不影响整体。但这个看似简单的目标在 ClickHouse 里的实现方式相当特别:副本之间**几乎不互相通信**,而是共同依赖一个位于架构正中的独立服务——Keeper。这一章讲清楚这套机制为什么长这样,以及运维时该盯哪些指标。

## 22.1 先分清:复制解决高可用,分片解决容量

这两个词经常被混着用,但它们是正交的两件事,分清它是后面一切的前提。

**复制**是同一份数据存多份:三个副本存的是同一批 part,数据总量不变,买到的是**高可用**(坏一台不丢数据)和**读扩展**(多台一起扛查询)。

**分片(sharding)**是把一份数据切成互不重叠的几块,分到不同节点,每台只存一部分。它买到的是**容量与写入吞吐**(数据总量被摊开)。

生产集群通常两者都要:每个分片内部再放两三个副本,"2 分片 × 3 副本"就是 6 台机器。这一章只讲副本。

## 22.2 副本之间为什么不直接通信

如果你来自 MySQL 或 Redis 的世界,直觉可能是"主副本把 binlog 推给从副本"。ClickHouse**不是这样**:所有副本都连到**同一个独立的一致性服务**(Keeper,或老一代的 ZooKeeper),发生了什么就写进这棵共享的树,别的副本从树里读回来。副本之间**从不直接协调状态**。

为什么这么设计,有三个理由:

1. **把最难的问题外包出去。** "全局顺序""多数派确认""分裂了听谁的"是分布式共识的硬骨头,这个一致性服务已经替你解决了。这里要把两代实现分清:**老的 ZooKeeper 用的是 Zab 协议**,**ClickHouse Keeper 用的是 Raft**(§22.12);两者都提供"外部看起来一样"的一致顺序节点接口,但内部共识算法不是一套。副本只要信任"Keeper 里的顺序就是全局顺序",就不必自己再实现一套共识。
2. **连接数是 O(N) 而不是 O(N²)。** 副本两两通信是 N² 份状态;中心化到 Keeper,每个副本只跟 Keeper 说话。
3. **"谁先谁后"有唯一答案。** Keeper 的顺序节点天然给出全局递增的编号,复制日志的顺序不需要争论。

代价是引入了新依赖:复制表的写入依赖 Keeper,Keeper 挂了就**写不进去**(还能读,见 §22.13)。这也解释了 Keeper 自己为什么必须是奇数节点的 Raft 集群——你不能把一个高可用系统建立在另一个单点上。

## 22.3 建一张复制表:DDL、macros 与默认路径

复制表不是"换个引擎名"那么简单,它还要说清"我跟谁是一组"。最小写法:

```sql
CREATE TABLE events ON CLUSTER '{cluster}' (
    ts      DateTime,
    user_id UInt64,
    url     String
)
ENGINE = ReplicatedMergeTree(
    '/clickhouse/tables/{uuid}/{shard}',   -- Keeper 路径:同一分片内所有副本必须完全相同
    '{replica}'                            -- 本副本名:同一分片内必须唯一
)
ORDER BY ts;
```

两个引擎参数就是复制表的身份:

- **第一个(zk 路径)**:这一组副本在 Keeper 上的**共同路径**,**同一分片内所有副本必须一字不差**;不同分片用不同路径(靠 `{shard}` 区分),否则两片会互相"串数据"。
- **第二个(副本名)**:本节点在这组里的名字,**同一组内唯一**。复制日志靠它区分"这条我处理过没有"。

路径里的 `{uuid}`、`{shard}`、`{replica}` 是**宏**,由服务器配置替换:`{shard}`、`{replica}` 来自 `config.xml` 的 `<macros>` 段,`{uuid}` 取表的 UUID:

```xml
<macros>
    <shard>01</shard>
    <replica>ch01</replica>
</macros>
```

`{uuid}` 是关键设计:用表的 **UUID** 而不是库名表名做路径,这样**重命名表、移动表都不会丢复制状态**。老文档常见的 `/clickhouse/tables/{shard}/{table}` 语义相同,但表一改名路径就断,不是好选择。

**可以让引擎参数省略**——不写这两项时用服务器默认:

```xml
<default_replica_path>/clickhouse/tables/{uuid}/{shard}</default_replica_path>
<default_replica_name>{replica}</default_replica_name>
```

于是 `ENGINE = ReplicatedMergeTree ORDER BY ts` 就够用了,路径与名字自动填。**但要注意 `{uuid}` 的前提**:它要求同一张表在所有副本上拿到**同一个 UUID**。手写 `CREATE TABLE`(不带 `ON CLUSTER`、也不指定 `UUID`)时,每个节点各自生成随机 UUID,于是各副本路径不同、根本不成组。所以省写引擎参数、又用 `{uuid}` 时,建表语句要经 `ON CLUSTER`(由发起节点生成同一个 UUID),或者干脆用 `Replicated` 数据库引擎(§22.9)——后者会自动管好 UUID。

`ON CLUSTER` 的机制在第 23 章(分布式 DDL);这里先记住:复制表的"成员关系"完全由上面这两个参数确定,配错它们(两片同路径、或同组重名)会得到最隐蔽的复制故障。

## 22.4 复制日志:一条按序执行的待办清单

复制的骨架是**一条日志**。在 Keeper(以及它兼容的 ZooKeeper)里,这些状态都存在一棵共享的树里——**树上每一个节点都叫一个 znode**,它是一段带**数据**(一个字节串)和**版本号**(每次写递增,用来做乐观并发控制)的路径,节点下面还可以再挂子节点。每个复制表在 Keeper 上都有自己的路径,路径下是这样一棵树:

```text
/clickhouse/tables/{uuid}/{shard}/
  ├── log/                    ← 复制日志:顺序节点,一条 = 一个操作
  │   ├── log-0000000001
  │   ├── log-0000000002
  │   └── log-0000000003
  ├── replicas/<replica>      ← 每个副本一个临时节点,data = "我处理到第几号了"
  ├── leader_election/        ← 选主用的临时顺序节点
  ├── blocks/<hash>           ← 插入去重标记
  ├── block_numbers/<part>/   ← 每个分区的"取号机"
  └── quorum/<entry>          ← quorum 插入的确认名单
```

这条路径从哪来、为什么用 `{uuid}`,§22.3 已经讲清;下面只看这棵树里每个节点的作用。

按职责把这棵树分个类,一眼就能看出哪块管什么:**`/log` 是复制日志本体**——一串顺序 znode,每一条代表一个待执行/已执行的操作(合并、拉 part、mutation 等),它是"各副本照着做的清单";**`/replicas/<name>` 是每个副本自己的状态位**——一条临时(ephemeral)znode,data 里存着这个副本的队列指针,副本的会话一断,它就被 Keeper 自动回收;**`/leader_election` 是选主用的临时顺序节点**,编号最小者即当前合并 leader(§22.7);**`/blocks` 与 `/block_numbers` 服务于插入去重**——前者是"这个块我见过没有"的标记,后者是每个分区的取号机(§22.5);**`/quorum/<entry>` 是 quorum 插入的确认名单**——发起副本先把名字写进去,别的副本应用完对应 part 后再各自追加,凑够 N 个才算成功(§22.6)。

`log/` 下每一条是一个操作条目,类型包括 `GET_PART`(把一个 part 拉过来)、`MERGE_PARTS`(合并)、`MUTATE_PART`(跑一次 mutation)、`DROP_RANGE`、`ALTER_METADATA` 等等。每个副本在 `/replicas/<自己的名字>` 上存一个**队列指针**(`log_pointer`),表示"我已经处理到第几号了"。副本的工作循环就是:读编号大于自己指针的条目,按序**应用**,然后推进指针。

**最关键的不变量:应用必须幂等。** 同一条条目可能因为重试、崩溃重启而被应用很多次。所以 `GET_PART` 要先检查"这个 part 我是不是已经有了",有就跳过;`MERGE_PARTS` 要先检查"目标 part 是不是已经在了"。正因为每条操作都能安全地重放,这套"纯追加日志"才成立。

另一点容易忽略:**Keeper 只负责达成一致,不搬数据。** part 内容走副本之间的 HTTP(`interserver_http_port`,叫 fetch):日志里写"我有了 part X",别的副本读到后自己去那个副本的 HTTP 端口把 X 拉过来。

最后收一个常被问到的疑点:ReplicatedMergeTree 有没有一个叫 `async_replication` 的**表级**开关?结论是没有——这个名字属于 Keeper 的 `coordination_settings`,不描述一个独立的复制模式。完整澄清放在本章末尾 §22.17,这里先不打断正文。

## 22.5 插入的幂等与去重

最实际的问题:客户端 `INSERT` 超时了,它不知道到底写成功没有,于是重试。如果每次重试都产生一份新数据,就重复了。

ClickHouse 的解法是给每个**块(block)**一个 `block_id`(由分区和块内容的哈希构成),插入时:

1. 查 `/blocks/<hash(block_id)>` 在不在。在 → 认定为重复,直接返回成功,什么都不写。
2. 不在 → 在 `/block_numbers/<partition>/` 下取一个**顺序号**(block number),part 名就由它确定:`<partition>_<min>_<max>_<level>`。
3. 把数据写成本地 part,然后把"去重标记"和"日志条目"写进**同一个 Keeper 事务**——这两步必须原子。如果出现"有日志、没标记",重试时找不到标记,就会写重复数据。

去重的**作用范围**要精确理解:它只在**同一副本组**内生效(靠这个 (shard, table) 共享的 `/blocks` 节点);而且只有 `block_id` **内容完全相同**才算同一块——改一个字节就是新块。这一点在任何版本都一样。但"内容相同"只是必要条件之一,另一个是**这个块还在去重窗口里**。

**去重靠两个窗口,先到先淘汰。** 计数窗决定 `/blocks` 下最多保留多少个去重标记,由 `replicated_deduplication_window` 控制,基线 25.3 的默认值是 **1000**(25.9 起提到 `10000`);时间窗决定标记最多保留多久,由 `replicated_deduplication_window_seconds` 控制,默认 **604800 秒(7 天)**(更新的版本里被下调过)。两个窗口**同时生效**:一个块只有在"最近 1000 个"且"最近 7 天"之内重发,才会被判重;一旦被任一窗口淘汰,再发同一个块就会被当成**全新数据**写进去。所以"隔很久再重放同一批数据"不再受保护,备份恢复、跨天补数这类操作要特别小心。

**想去重彻底关掉,用总开关 `insert_deduplicate`。** 它是会话/查询级设置,默认 **1**(开)。设成 `0` 后,写入不再查 `/blocks`,同一个块重发就作为新数据插入——适合"我就是要按原样重放"的导入或校验场景,也能省一次 Keeper 往返。把它写在 `INSERT` 的 `SETTINGS` 里或整条会话上。注意它和窗口是两回事:关掉的是"要不要查标记",不是"标记留多久"。

**想自己定义"什么算同一个块",用 `insert_deduplication_token`。** 默认判重靠块内容哈希,前提是"重试时块内容和分块方式跟第一次完全一致"(下面那个坑说的就是它)。`insert_deduplication_token` 让你**自带一个幂等键**:在这次 `INSERT` 的 `SETTINGS` 里写 `insert_deduplication_token = 'batch-0007'`,ClickHouse 就按这个令牌而非内容哈希判重——两次带同一令牌的插入只有第一次真正落盘。它把幂等的控制权交回给你,尤其适合"上游会模糊重试、块边界不稳定"的管道,只要上游能给每个批次一个稳定 ID。令牌的有效期同样受该表的去重窗口约束,而且是**按表**隔离的。

补充一句版本纪律:`replicated_deduplication_window` 这个默认值在更高版本里被调大过,所以建表或调参前,先跑 `SELECT name, value FROM system.merge_tree_settings WHERE name LIKE 'replicated_deduplication%'`(以及 `system.settings` 里的会话级设置)看你手上这个版本到底是几个,不要照搬本章的数字。

一个典型错误:把一条大 `INSERT` 拆成两条小 `INSERT` 重发,以为去重能拦住——**拦不住**,去重粒度是块而非"这条 SQL"。默认每 `max_insert_block_size`(1048449,即 2^20 − 127;这个"减 127"的由来见第 08 章 §08.7)行切一块,重试时要保证**分块方式和第一次一致**。

## 22.6 quorum 插入:让"客户端重试"真正安全

`insert_quorum=N` 的语义是:这条 `INSERT` 在 **N 个副本确认已经拥有这份数据**之前,不返回成功;超过 `insert_quorum_timeout`(默认 600000 毫秒,即 600 秒)还没凑够就报错。

**先纠正一个常见误解:这不是默认就开的开关。** `insert_quorum` 的默认值是 **0**,也就是默认**不启用** quorum;而且**值小于 2 都等于关闭**——写 0、写 1、不写,行为完全一样,都是普通的异步复制插入。要让 quorum 真正起作用,得写 ≥ 2。25.3 还支持 `insert_quorum = 'auto'`,意思是"按**多数**算"——对 N 个副本的组自动取 `floor(N/2)+1`,省得手算。

很多人以为它的价值只是"多写几份、更不容易丢",那只是表面。它真正的价值是**把 INSERT 变成一个可以安全重试的操作**。

机制上,发起副本在 `/quorum/<条目名>` 写入自己的名字,别的副本应用完对应的 `GET_PART` 后各自追加自己的名字,发起副本轮询名单直到长度达到 N。

**没凑够 quorum 时会发生什么,才是这套设计的要点。** 一旦超过 `insert_quorum_timeout` 仍未达到 N,发起副本会报错,并且——关键在这里——它会**把这次插入已经写进各个副本的那个块删掉**。这样"失败"就是一个**干净**的状态:既然 `INSERT` 报了失败,集群里就不该留下它的数据。于是客户端重试有两种干净的结局:如果上一次其实已达成了 quorum,`/blocks` 里的去重标记还在,重试被 §22.5 判重、直接返回成功而不重复写;如果上一次没达成,块已被删掉、不留标记,重试就作为一次全新插入正常落下。两条路都不会产生重复数据——这正是"可安全重试"的实现方式。

**`insert_quorum_parallel` 决定重试会不会被"前面那次"堵死。** 默认 **1**,允许多个 quorum 插入同时等待、互不干扰。设成 `0` 就变成**串行**:同一个表上,一次新的 quorum 插入在开始之前会先检查"上一次 quorum 插入成了没有",若上一次还没成功,这次直接抛 **`UNSATISFIED_QUORUM_FOR_PREVIOUS_WRITE`**。这是串行模式的代价——它是典型的**队头阻塞(head-of-line blocking)**:一台上游短暂抖动让某次插入迟迟凑不齐 quorum,后面所有 quorum 插入都被它卡住,直到超时。所以除非你明确需要串行带来的顺序语义(§22.11 的 `select_sequential_consistency` 就依赖它),否则保持默认的并行。

一个典型错误:3 副本的集群把 `insert_quorum` 设成 3(等于"全部"),一旦挂一台,`INSERT` 会一直等到超时才失败。**quorum 应按"多数"设**——3 副本设 2,或干脆写 `insert_quorum = 'auto'`,而不是"全部"。

## 22.7 合并的领导权

后台合并也必须一致。如果两个副本各自决定合并哪些 part 且选择不同,就会合出名字冲突、内容不同的 part,去重失效、副本永久不一致。

解法是**每个副本组选一个 leader**:`leader_election/` 下每个副本建一个**临时顺序节点**,编号最小的就是 leader。**只有 leader 决定"合并哪些 part"**,写一条 `MERGE_PARTS` 到日志;其他副本只是**执行**它写的条目。决策权收敛到一个副本,各副本的合并结果必然一致。

leader 挂掉时,它的临时节点随会话消失,第二小的节点自动成为新 leader。整个过程**没有任何"选举消息"**——就是"看谁最早到"。

## 22.8 盯住 system.replicas

复制出了问题,第一个要看的就是这张表:

```sql
SELECT database, table, is_leader, is_readonly, absolute_delay,
       queue_size, inserts_in_queue, merges_in_queue,
       log_max_index, log_pointer, total_replicas, active_replicas
FROM system.replicas
```

几个关键列:

- `absolute_delay`:队列里**最老的、还没执行的条目已经等了多久(秒)**。这是复制健康度最重要的指标——它一旦不为 0,就说明这个副本落后了。
- `queue_oldest_time` / `queue_newest_time`:队列里**最老**和**最新**一条条目各自的**时间戳**(注意是绝对时间,不是"等了多久")。`absolute_delay` 实际上就是"当前时间减去 `queue_oldest_time`"得到的秒数;把这两个时间戳和 `queue_size` 一起看,能区分积压是"老账一直没清掉"(oldest 很旧)还是"刚刚堆起来"(oldest 很新、size 在涨)。
- `queue_size`、`inserts_in_queue`、`merges_in_queue`:积压总量及其中插入、合并各占多少(分开看能区分"写入压力"还是"合并压力")。
- `log_max_index` 与 `log_pointer`:Keeper 里最新的日志编号 / 这个副本处理到哪一号,两者之差就是滞后量。
- `is_leader`、`is_readonly`:是不是合并 leader;是不是进了只读(会话断了、队列积压、本地 part 丢失都会导致只读)。
- `total_replicas`、`active_replicas`:两个数**不是一回事,不要混**。`total_replicas` 是 `/replicas` 下**已知的副本节点总数**(这棵子树下有几个孩子);`active_replicas` 在此基础上**还要求该副本的会话是活的**(有活跃的 ephemeral 会话标记)。所以一个副本刚宕机、Keeper 还没来得及回收它的临时节点时,会出现 `total_replicas` 仍然高、`active_replicas` 已经掉下来的**中间窗口**——`active_replicas < total_replicas` 就是"有副本掉线"的告警信号。要等到 Keeper 的会话超时(§22.12 的 `session_timeout_ms`)真正清掉那条临时节点,`total_replicas` 才会跟着降。
- `zookeeper_exception`:最近一次与 Keeper 交互的异常——**不为空就是有问题**。

维护命令有四种:

```sql
SYSTEM SYNC REPLICA db.tbl;                     -- 阻塞直到本副本队列追平(等待追进度)
SYSTEM RESTART REPLICA db.tbl;                  -- 重新建立 Keeper 会话、重挂队列(会话死了用它)
SYSTEM RESTART REPLICAS;                        -- 同上,但对本机所有复制表一次做
SYSTEM DROP REPLICA 'name' FROM TABLE db.tbl;   -- 删掉别的副本残留的元数据
```

`RESTART REPLICA` 是**无损**的一档:让副本重新连 Keeper、重新解析队列,本地已有的 part 保留,只按 Keeper 的共识把缺的任务补回来;带 `S` 的 `RESTART REPLICAS` 只是把同样的动作一次性施加到本机**所有**复制表——排障时"不确定是哪张表出问题、先整体重挂一遍"就用它。别和 §22.10 的 `RESTORE REPLICA` 混:那个是"本地元数据已经坏了、要以 Keeper 为准重建",更激进。

`DROP REPLICA` 用得不多但很关键:副本下线又重建(或换了副本名)后,`/replicas` 里可能**留下旧节点的元数据**,`total_replicas` 就会一直偏高;用它把残留清掉。它不删数据,只删元数据。更细的恢复手段见 §22.10。

## 22.9 Replicated 数据库引擎:让"跟着 DDL 走"成为默认

逐个表手写 `ReplicatedMergeTree(...)` 参数、还要保证每个副本的 UUID 一致,麻烦且容易错。**`Replicated` 数据库引擎**把这件事变成默认:

```sql
CREATE DATABASE r ON CLUSTER '{cluster}'
ENGINE = Replicated('/clickhouse/databases/{uuid}', '{shard}', '{replica}');
```

引擎的三个参数和复制表是一一对应的:`zoo_path`(这一组副本在 Keeper 上的共同路径)、`shard_name`、`replica_name`。它带来几件在集群里很值钱的事:

- **库里的表自动变成复制表。** 在 `r` 库里 `CREATE TABLE` 时,`ReplicatedMergeTree` 的路径与副本名**自动填好**(由库的参数派生),不必每张表手写。
- **UUID 自动统一。** §22.3 说的 `{uuid}` 前提(所有副本同一个 UUID)由引擎保证——这正是它和 `{uuid}` 路径天然配合的原因。
- **DDL 自动跟着走。** 用 `Replicated` 库时,建表语句即使不写 `ON CLUSTER`,也会**通过 Keeper 传播到该库的所有副本**——相当于库一级的"分布式 DDL"。它和 `ON CLUSTER`(第 23 章)是两条并行的路径,前者靠库引擎、后者靠 DDL 队列。
- **自动在 `system.clusters` 里注册一个同名集群**,可以直接用来建 `Distributed` 表。

代价是它依赖 Keeper:库的 DDL 传播、表结构都对 Keeper 有要求;Keeper 不可用时,这个库的建表/改表也走不动(读不受影响)。**推荐的生产配法就是"一台机器一个 `Replicated` 库"**:表的复制、UUID、DDL 传播一次配好,后面建表都是普通写法。

## 22.10 副本数据丢失后的恢复

副本的本地数据丢了(磁盘坏、误删、`/var/lib/clickhouse` 被清),或者 Keeper 里的副本元数据与本地对不上,恢复手段有几种,按"丢了多少"选:

**先讲清"什么时候副本根本不可能靠补日志追上"。** §22.4 说副本的工作循环是"读编号大于自己指针的条目、按序应用";它成立的前提是**那些条目还在 `/log` 里**。而 `/log` 会被清:leader 上的清理线程(`ReplicatedMergeTreeCleanupThread`)只保留最后一段日志,这段有多长由 `max_replicated_logs_to_keep` 决定——**默认 1000 条**(源码 `src/Storages/MergeTree/MergeTreeSettings.cpp`:`max_replicated_logs_to_keep` 默认 1000,注释是 "How many records may be in log, if there is inactive replica. Inactive replica becomes lost when this number exceed.")。

判据是这样的:清理线程先按 `max_replicated_logs_to_keep` 算出"这一轮裁下去,最旧会保留到第几号",再逐个看**不在活跃状态**(没有 `is_active` 临时节点)的副本——**只要某个副本的 `log_pointer` 落在"即将被删掉的那段区间"里,就判定它追不上了**:给它打上 `is_lost = 1`,并在服务端日志里留一句

```text
Will mark replica <name> as lost, because it has stale log pointer: log-0000001234
```

(源码 `src/Storages/MergeTree/ReplicatedMergeTreeCleanupThread.cpp` 的 `markLostReplicas()`。)这句就是"某副本掉线太久、日志已被清光、它再也补不回来"的**直接证据**——排查"副本重启后一直追不平、`absolute_delay` 始终不为 0"时,先在服务端日志里搜这一句。

一旦被打上 `is_lost = 1`,副本再启动时走的就是**另一条恢复路径**:不再是"从自己的指针接着补日志",而是 `cloneReplicaIfNeeded()` 挑一个**健康的源副本**(不是 lost、队列最短、log pointer 最大),把本地数据**整份换成从源副本克隆/拉取的那一份**。所以"stale log pointer"正是"补日志追平"和"clone 重灌"这两条路径的**分水岭**:指针还新就补日志,指针太旧就整份重来。这也给 §22.13 那句"副本宕机后追平是自动的"补上一个**隐含前提**——掉线不能太久,久到日志被清光,就不再是"追平"而是"重灌"。

三条边界要记住:

- **它是表级设置**(`max_replicated_logs_to_keep` 是 MergeTree 设置,不是会话设置),写在表 `SETTINGS` 里或用 `ALTER TABLE ... MODIFY SETTING` 改。调大它,能让掉线很久的副本仍有更大机会靠补日志追上,代价是 `/log` 在 Keeper 里留得更久(占空间、清得更晚)。
- **别和 `min_replicated_logs_to_keep`(默认 10)混。** 后者是清理时"无论如何至少留这么多条"的地板,源码注释明说它"主要用于清理前诊断 ZooKeeper 日志,不影响表的正常工作";真正决定"谁会被判 lost"的是 `max_replicated_logs_to_keep`。
- **还有一层保护:** 如果**所有**副本的指针都旧了,清理线程会直接抛 `ALL_REPLICAS_LOST`、**拒绝把任何一个标为 lost**——因为此时已经没有健康副本可作克隆源,标记 lost 只会让全组一起完蛋。

**① 本地 part 还在、只是 Keeper 元数据没了 → `SYSTEM RESTORE REPLICA`。**

```sql
SYSTEM RESTORE REPLICA db.tbl;   -- 用本地已有的 part 重建 Keeper 里的副本元数据
```

适用场景:副本被 `DROP` 掉、或 Keeper 数据被清之后,本地数据其实还在。它**从本地 part 反推**出 Keeper 里该有的元数据,把副本重新挂回复制组,不需要从别的副本重拉全量——这是最省的一次恢复。前提是表当前处于"没有 Keeper 元数据"的状态(通常是只读或未激活)。

**② 只想快速追平、不等合并 → `SYSTEM SYNC REPLICA ... LIGHTWEIGHT`。**

```sql
SYSTEM SYNC REPLICA db.tbl LIGHTWEIGHT;
```

普通的 `SYSTEM SYNC REPLICA` 会等队列**彻底**清空(包括合并、mutation);`LIGHTWEIGHT` **只等** `GET_PART`/`ATTACH_PART`/`DROP_RANGE`/`DROP_PART` 这类"数据到位/删除"的条目,**不等合并**。所以"我只想让这个副本能查到最新数据"用它就够,不必等后台合并跑完。(还有 `PULL` 只从 Keeper 拉新条目、不等待,`STRICT` 更严;细节以版本文档为准。)

**③ Keeper 里残留了一个不存在的旧副本 → `SYSTEM DROP REPLICA ... FROM ZKPATH`。**

```sql
SYSTEM DROP REPLICA 'old_replica' FROM ZKPATH '/clickhouse/tables/{uuid}/{shard}';
```

副本节点被重建过(换了副本名)时,Keeper 里可能留着**旧名字的残留节点**,`total_replicas` 会一直偏高(§22.8)。这条命令按 Keeper 路径删掉那个残留节点,不碰任何数据。它和 `SYSTEM DROP REPLICA 'name' FROM TABLE db.tbl` 的区别是:前者给**路径**(可以点名一个别的表的副本残留),后者给**表**。

**④ 彻底重来:detach / attach + 从别的副本重灌。**

最"暴力"也最保险的一条路是:把表 `DETACH`、干净地删掉它的 Keeper 元数据,再 `ATTACH`——attach 时它发现本地没有有效数据,就会**从别的副本 fetch 全量 part**(§22.4 的 part 传输);本地 part 还在时,也可以用 `RESTORE REPLICA` 就地重建元数据(见 ①)。这在"本地数据已损坏、只想跟另一副本对齐"时最直接。**①与④共用的那条恢复链,完整语序是**:`DETACH TABLE` → 用 `DROP REPLICA ... FROM ZKPATH` 清掉 Keeper 里的路径 → `ATTACH TABLE`(此时是空的/只读)→ `SYSTEM RESTORE REPLICA db.table`(用本地 part 重建 Keeper 元数据)或让它从别的副本拉数据 → `SYSTEM SYNC REPLICA` 追平。每一步都在动复制元数据,**操作前先在一个非生产副本上演练一遍**。

**`RESTORE REPLICA` 的前置条件很明确:表必须处于"Keeper 里没有它的元数据、因而只读"的状态**(`system.replicas` 里 `is_readonly = 1`),Keeper 本身要活着、可达(哪怕它的 `/clickhouse` 数据是空的),本地还要留着这张表的定义与磁盘上的 part。它**对一张正常可写的表会直接拒绝执行**——如果它已经是活跃副本,得先按要求把它从组里摘出来(完整语序见上面第 ④ 条)。要注意去重与块号会被重建、复制队列与进行中的 DDL 不会恢复,所以 schema 要以"最完整的那份本地定义"为准。它首次可用的版本,常被写作"21.7 起",但**未能确认**——可靠的参照是 21.8 前后的发布说明,以你那一版的文档为准。

## 22.11 一致性读:默认会读到旧数据

必须记住:**默认情况下副本是最终一致,不是强一致。** 你写到副本 A 的数据,副本 B 可能过一会儿才看到,此刻去查 B 会读到**旧的**数据。

`select_sequential_consistency=1` 可以改变这一点:它让查询带上"至少读到某个已提交位置"的约束,保证"别人写成功的数据我一定能看到"。但这里有一个**几乎是决定性的前提,本章原先漏讲、必须补上**——它**只在 `insert_quorum_parallel = 0` 时才真正提供顺序一致性**;而 `insert_quorum_parallel` 在 25.3 **默认是 1**。也就是说,**默认配置下打开 `select_sequential_consistency` 并不生效**:并行模式允许多次 quorum 插入交叉进行,根本不存在一个"所有已提交写入的全局位置"供查询对齐,这个设置于是失去了约束力。要让它工作,必须先 `SET insert_quorum_parallel = 0`,而这又会带来 §22.6 说的**队头阻塞**。

所以它的代价是双重的:每次查询要额外去 Keeper 查一次副本状态(延迟上升),以及一串串行 quorum 插入的队头阻塞;并且它只对 quorum 插入过的数据有效。大多数分析场景**不需要**它,只有在"写完马上要读到刚写的那条、且能接受串行 quorum"时才考虑。

## 22.12 Keeper:ClickHouse 自己实现的 ZooKeeper

Keeper 是 ClickHouse 用 C++ 重写的 ZooKeeper,**协议兼容**——ZooKeeper 客户端能连它,ClickHouse 也能连真 ZooKeeper。要强调一点:它不只"接口像",连**共识算法**都换成了 **Raft**(§22.2);真 ZooKeeper 走的是 Zab。

为什么自己实现而不是直接用?ZooKeeper 是 Java 的,生产里跑它得带上 JVM 并调 GC,与 ClickHouse 的技术栈格格不入;Keeper 的内存占用和延迟更优;它还能和 ClickHouse 一起演进(比如加了持久 watch,而 ZooKeeper 的 watch 是著名的一次性"坑")。

**部署方式有两种:**

- **独立进程**:单独跑一个 Keeper 进程(新版可执行文件常叫 `clickhouse-keeper`),多个节点组成 Raft 集群。生产推荐这种方式。
- **嵌入 server**:在配置里写 `keeper_server` 段,让 server 进程顺带跑 Keeper。适合小规模或测试,但把数据库和 Keeper 的生命周期绑在了一起,关键生产不推荐。

Raft 集群的配置要点:节点数取**奇数**(3 或 5);`keeper_server` 段里有 `server_id`、`tcp_port`、`log_storage_path`(Raft 日志)、`snapshot_storage_path`(快照)、`raft_configuration`(列出每个 server 的 id/hostname/port)。客户端一侧由 `zookeeper` 段指向这些节点的地址。

**运维调优:`coordination_settings`。** Keeper 自身的行为由 `keeper_server` 下的 `coordination_settings` 段控制,几组常用的:

- **会话与心跳**:`session_timeout_ms` 是客户端会话的超时上限(老文档的默认值是 **30000** ms,新版本上调到了 **100000** ms;`operation_timeout_ms` 默认 10000)——太短会让瞬时抖动就掉会话,太长会让故障副本的临时节点迟迟不回收,**直接影响 §22.8 里 `total_replicas` 与 `active_replicas` 的差异窗口有多久**。`heart_beat_interval_ms`(**默认 500**)是 leader 向 follower 发心跳的间隔;`dead_session_check_period_ms`(**默认 500**)是"多久检查一次哪些会话死了",它决定一次宕机后过多久才真正清掉那个副本的临时节点。
- **日志与快照**:Raft 日志不会无限增长。每 `snapshot_distance` 条日志落一次快照(默认 100000);`reserved_log_items` 决定压缩后**至少保留多少条**历史日志(默认 100000),好让落后太多的 follower 仍能靠日志追、而不是被迫收一份整的快照;`snapshots_to_keep`(默认 3)决定留几份快照。集群写入量大时要相应调大,否则落后副本追进度会退化成"整份快照同步",更慢也更占带宽。要特别点明:**`reserved_log_items` 管的是 Keeper 自己那条 Raft 共识日志**——它让落后的 follower 仍能靠日志追 Keeper 的状态机;这和 §22.10 讲的 `max_replicated_logs_to_keep` 是**两层不同的东西**:后者是**某张复制表**的表级设置,决定"这张表的副本落后多少条**复制日志**才会被判 lost"。一句话,Keeper 的这个保留量管的是"你能不能追上 Keeper 的共识",表级的 `max_replicated_logs_to_keep` 管的是"你能不能追上别的副本的数据"——**别拿 Raft 层这个旋钮去当复制表的追平开关。**
- **强制落盘**:`force_sync`(默认开启)控制日志写入是否 `fsync`。关掉能换性能,但崩溃时可能丢最近几条已确认的日志——不知道自己在换什么就别关。
- **日志级别**:`raft_logs_level`(生产建议 `warning` 以上)控制 Raft 日志的啰嗦程度;排查复制问题时才临时调低。

**可观测性。** 在 `<keeper_server>` 段里加一段 `<prometheus><endpoint>/metrics</endpoint><port>…</port></prometheus>`,Keeper 就会暴露一个 Prometheus 端点供采集。

**四字母命令**是直接向 Keeper/ZooKeeper 的客户端端口发四个字节、拿回一段纯文本诊断信息的老办法,形如 `echo mntr | nc <host> <client_port>`。它们**受一份白名单限制、超出白名单的命令默认不应答**,要放开就得显式改这个参数;参数名在版本之间改过——旧名 `four_letter_word_white_list`,新名 **`four_letter_word_allow_list`**(旧名作为别名仍被解析)。**这份名单的长度是被低估的**:25.3 上游 `src/Coordination/CoordinationSettings.cpp` 里,默认值由两段拼成 —— jemalloc 构建下先是 `jmst,jmfp,jmep,jmdp`,然后是主体那串 `conf,cons,crst,envi,ruok,srst,srvr,stat,wchs,dirs,mntr,isro,rcvr,apiv,csnp,lgif,rqld,rclc,clrs,ftfl,ydld,pfev`。**要按你那一版现场的 `keeper_server.four_letter_word_allow_list` 为准** —— 早期资料里流传的那份短名单(`…stat,wchc,wchs,dirs,mntr,isro`)**既不完整也不准确**:`wchc` 其实一直不在名单里,而 `rcvr` / `csnp` / `lgif` / `rqld` / `rclc` / `clrs` 这些是默认就允许的。六个命令各回答一个问题:

- **`ruok`**——**活着吗?** 正常时只回一个 `imok`。适合做最轻量的存活探针;回别的、或连不上,就是没起来。
- **`srvr`**——**我是谁、跑的是什么版本?** 给出这个节点在集群里的**角色**(`leader` / `follower` / `observer`)、`Zookeeper version`,以及连接数、znode 数等概要。排查"哪个是 leader""各节点版本是否一致"看它。
- **`mntr`**——**健康度怎么样?** 输出一大串 `键\t值`,信息量最大:存活会话数(`zk_num_alive_connections`)、znode 总数(`zk_znode_count`)、**快照与日志的体积 / 近似数据大小**、**请求延迟**(`zk_avg_latency` / `zk_max_latency`)、watch 数,以及角色。判断"Keeper 是不是快爆磁盘、是不是变慢了"看它。
- **`stat`**——**连接与 znode 统计的浓缩版?** 除角色、版本外,还给出客户端连接(`Clients`)与 `Outstanding`(未处理请求数)、`Node count`。想知道"谁连着、有没有请求积压"看它。
- **`conf`**——**这台节点的配置是什么?** 把生效的配置项(端口、`snapshot_distance`、`session_timeout` 等)列出来,用来核对"我改的配置到底生效没有"。
- **`cons`**——**都谁连着我?** 列出每个客户端连接的地址、会话 id 与最近一次操作的延迟(`queued` / `recved` / `sent`),定位"哪个客户端在拖慢我"。

**四字母命令有两个入口,而且 25.3 上它们其实是通的 —— 这一点本书早先写反过。** `clickhouse-keeper-client` 的 `-q` 参数当然接受 znode 操作(`ls` / `get` / `create` / `rm` 那一族),但它**还有一条兜底规则**:命令名在已知命令表里找不到、且**长度正好是 4 个字符**时,keeper-client 就把它**当成四字母命令**处理 —— 上游 `programs/keeper-client/Parser.cpp` 里就是 `if (command_name.size() == 4)` 那一支,它造一个 `FourLetterWordCommand`,把命令名换成四字母命令自己的名字,后面剩下的文本继续当参数解析。所以 `clickhouse-keeper-client -q "mntr"` **是能拿到监控输出的**,不会被当成语法错误;`flwc ruok` 那种显式写法也成立。真正要知道的是那条规则的**边界**:它只认"长度正好 4 且不在已知命令表里"这一种情况,所以拼错了长度(多一个字符、少一个字符)就会掉出这条分支,变成一条普通的"未知命令"。除了这个入口,照上面那条 `echo ... | nc <host> <client_port>` 直接打客户端端口同样可行。

**SQL 侧看树。** `system.zookeeper` 把 Keeper 里的 znode 呈现成一张表(`path`、`name`、`value`、`czxid`、`mzxid`、`version`、`numChildren` 等列——正是 znode "数据 + 版本号"的形状)。一条能直接跑的查询,是列出一个路径下的所有子节点:

```sql
SELECT name, value FROM system.zookeeper WHERE path = '/clickhouse/tables';
```

把 `path` 指到某张复制表的路径(如 `/clickhouse/tables/<uuid>/<shard>`)下,就能看到 §22.4 那棵 `log` / `replicas` / `blocks` 的 znode 树,`value` 列里就是各节点存的数据(例如副本的队列指针)。另外两张表配合着用:**`system.zookeeper_connection`** 告诉你**本机当前连的是哪个 Keeper 节点(host/port)、会话 id、连接建立了多久、连没连上**——多节点集群里排查"这台 server 是不是连到了某个慢的/挂掉的 Keeper"先看它;**`system.zookeeper_log`** 是**客户端请求日志**,逐条记下本机向 Keeper 发出的每一次请求(类型、path、耗时、有无异常),用来回答"这次复制卡住时,究竟是哪条 Keeper 请求在慢、在报错"。

**该采哪些指标、对什么告警。** 上面那个 Prometheus 端点上,重点盯四类:**①存活会话数**——骤降说明大批客户端掉线;**②快照与日志体积**——Raft 日志/快照持续膨胀、逼近磁盘上限就该告警(本节前面讲的 `snapshot_distance` / `reserved_log_items` 正是它的旋钮);**③集群里有没有 leader**(`is_leader` 一类的指标 / leader 计数)——**长时间没有 leader 就是共识层失去了多数派,这是最该告警的一条**(此时复制表的写入会失败);**④uptime**——重启清零说明进程崩过。把这四类接进常规监控,基本能覆盖"Keeper 层面导致的复制问题"的绝大多数情形。

## 22.13 故障处理

三种最常见的故障:

- **副本宕机后追平。** 副本重启会重开会话、重建临时节点,从自己的 `log_pointer` 继续读日志、按序应用,缺的 part 从别的副本 fetch 过来。**这个过程是自动的**,不需要人工干预;想确认进度就跑 `SYSTEM SYNC REPLICA`。
- **Keeper 不可用。** 复制表进入只读:**写入被拒**(既取不到号也追加不了日志),但**查询还能读本地已有的 part**(数据在本地,读不依赖 Keeper)。所以典型表现是"读得了、写不了"。
- **副本只读(`is_readonly=1`)。** 常见原因:会话过期、队列积压超过限制、本地 part 丢失。排查入口是 `system.replicas` 的 `zookeeper_exception` 列和服务端日志。**本地 part 丢失导致的只读,恢复手段见 §22.10**。

**副本落后时,先分清"是没得拉,还是拉不过来"。** 副本追平有两半:对应的 `GET_PART` 条目还在 `/log` 里(有得拉,§22.10 刚讲了"日志被清光"这一种反面情况),以及把 part 从源副本**按 HTTP 拉过来**的那段网络传输(`interserver_http_port` 上的 fetch,§22.4)。当"有得拉、却迟迟拉不完"成了瓶颈——比如新加的副本要从零补齐几 TB、或某个副本掉线很久后要追一大批 part——真实瓶颈就落在网络带宽上。这时有两件工具。

**一是把"正在拉的那个 part"看见。** 只盯 `system.replication_queue`(§22.15)只能看到"有一条 `GET_PART` 在等",看不到传输本身。**`system.replicated_fetches` 正是为这件事准备的**:它**一行一次正在进行中的 part 拉取**,列有 `database`/`table`、`elapsed`(这次拉取已经进行了多久)、`progress`(0~1 的完成度)、`result_part_name`、`total_size_bytes_compressed` 与 `bytes_read_compressed`(总量与已读),以及 `source_replica_hostname`/`source_replica_port`(从哪个副本拉)。"它正在拉一个几 GB 的大 part"这个判断,靠这张表才看得见:`elapsed` 一直涨而 `progress` 不动,就是"传卡住了";两条都在动,才是"正常但慢"。

**二是给复制流量限速。** 复制流量默认**不设上限**,一个副本补数据能把整条链路占满、拖垮同机上别的查询。要让路,有表级和服务端两级旋钮:

```text
max_replicated_fetches_network_bandwidth               表级(MergeTree 设置),默认 0 = 不限速
max_replicated_sends_network_bandwidth                 表级(MergeTree 设置),默认 0 = 不限速
max_replicated_fetches_network_bandwidth_for_server    服务端设置,默认 0 = 不限速
max_replicated_sends_network_bandwidth_for_server      服务端设置,默认 0 = 不限速
```

前两个是**表级**的:分别是"这张表在本副本上**拉取** part 的速度上限"和"向别的副本**发送** part 的速度上限",单位是字节/秒,默认都是 0(不限速),写在表 `SETTINGS` 里或用 `ALTER TABLE ... MODIFY SETTING` 改。后两个是**服务端**的,给整台实例的复制流设总闸,写在服务端配置里。**这里有一条 25.3 的版本纪律**:`..._for_server` 这两个名字在**会话/查询**那套 `system.settings` 里已经被标成 `MAKE_DEPRECATED_BY_SERVER_CONFIG`(源码 `src/Core/Settings.cpp`)——意思是它们已经**从"会话设置"搬到"服务端配置"**,由 `ServerSettings`(源码 `src/Core/ServerSettings.cpp`,默认 0)承载。所以限复制带宽要先分清层:**按表限用表级设置,按实例限用服务端配置**,别再去会话里 `SET` 那两个 `_for_server`。

调它们要有分寸:**限得太松没意义,限得太紧会让副本永远追不上**。它适合"追平期间别把带宽吃满"这种临时场景,不适合当常态——常态下如果副本老是追不上,根因多半是**写入量本身超过了单副本能承载的复制速率**,该做的是加副本、分片,或者压低副本间要搬的数据量(比如控制 part 数,§18.6)。

**Keeper 抖动时的客户端重试。** Keeper 短暂抖动(选主、网络闪断)时,server 侧对 Keeper 的操作不会立刻失败,而是按一组**会话设置**重试:`keeper_max_retries`(默认 **10**)控制"普通 Keeper 操作最多重试几次";两次重试之间的退避从 `keeper_retry_initial_backoff_ms`(默认 **100** ms)起步、按倍数增长到 `keeper_retry_max_backoff_ms`(默认 **5000** ms)封顶;这一族自 24.3 起提供。写入路径另有 `insert_keeper_*` 一组(如 `insert_keeper_max_retries`,默认 **20**)专管 `INSERT` 到复制表时对 Keeper 请求的重试。它们就是"多短的一次抖动会被自动吸收、多长才暴露给业务"的旋钮:调得过大,一次真故障时写入会长时间闷头重试而不快速失败;调得过小,Keeper 的正常选主抖动就会打到上层。具体默认值以你版本 `system.settings` 的实测为准。

## 22.14 冷热分层与共享存储

复制表同样能做冷热分层:在存储策略里定义多块磁盘(热盘用 SSD、冷盘用大容量 HDD 或对象存储卷),再用 TTL 的 `<ttl>` 把老数据从热盘搬到冷盘。它和复制是**正交**的——每个副本各自按 TTL 搬自己那份数据,搬的是**位置**不是语义。三个要点:一是搬运是**后台动作**(由后台 move 池驱动,见第 25 章与第 29 章),会占 IO 和临时空间;二是"何时搬"由 `move_factor` 与磁盘水位线决定,设得太紧会让数据在盘之间来回倒;三是热冷盘上的 part 对查询是同一张表,`system.parts` 里能看到它落在哪个卷/盘。

至于"多个副本共享底层文件系统",要纠正一个常见但**并不存在**的说法:**没有**一个叫 `remote_fs` 的普通存储选项。真实存在的是 **zero-copy replication(零拷贝复制)**:当底层是支持"给文件做硬链接或云盘快照"这类语义的共享文件系统(典型是 S3 风格的远程文件系统)时,副本之间不真的各拷一份 part,而是共享同一份底层文件,靠一组 `remote_fs_*` 设置(如 `remote_fs_zero_copy_path_compatible_mode` 等)、以及 MergeTree 侧的 `allow_remote_fs_zero_copy_replication` 来启用。

**这里必须给出明确警示:这套能力是实验性的,不要用在生产。** 它自 **22.8 起默认关闭**——要开启得显式放开 `allow_remote_fs_zero_copy_replication`(默认 0),而 ClickHouse 源码里给它的注释写得很直白:**"Don't use this setting in production"**。官方自己都没把它当成可上线的功能;它会引入"某个副本的 part 丢了、但底层文件还在""元数据对不上"这类难收拾的状态,和 §22.10 的恢复手段凑在一起尤其危险。真要用共享存储做存算分离,今天该走的是云上的 **`SharedMergeTree`**:副本一致性由存储层负责,是 ClickHouse Cloud 的基础,自建基本用不到。

## 22.15 `system.replication_queue`:复制积压的第一手证据

§22.8 的 `system.replicas` 给的是**汇总**——队列多长、最老等了多久。想知道"到底是哪一条在卡、卡了几次、为什么",得看 `system.replication_queue`:它是复制日志在**本副本**上的待办队列,一行一个尚未执行完的条目。

```sql
SELECT table, type, node_name, num_tries, is_currently_executing,
       create_time, last_attempt_time, last_exception_time,
       postpone_reason, last_exception
FROM system.replication_queue
WHERE database = 'analytics'
ORDER BY create_time;
```

几个必须会读的列:

- `type`:这个动作是什么——`GET_PART`(拉一个 part)、`MERGE_PARTS`(合并)、`MUTATE_PART`、`DROP_RANGE`、`ALTER_METADATA` 等。积压里 `GET_PART` 占多数,说明这个副本在**补数据**(落后);`MERGE_PARTS` 占多数,说明瓶颈在**合并**(对照后台合并池)。
- `num_tries` / `last_exception` / `last_exception_time`:这条条目**试了几次、最近一次为什么失败、什么时候**。定位根因最直接的三个字段——`num_tries` 一直涨而 `is_currently_executing = 0`,说明它在"试 → 失败 → 退避 → 再试"的循环里打转。
- `is_currently_executing`:此刻是不是真的在被执行。为 0 又不消失,通常是在等重试或等前置条件。
- `postpone_reason` / `last_postpone_time`:这条条目为什么被**推迟**、以及**最后一次**被推迟是什么时候(`num_postponed` 是累计次数)。`MERGE_PARTS` 被推迟,常常是它依赖的 part 还没到——这是"合并总差一步"的常见答案。(注意列名:是 `last_postpone_time`,**不是** `postponed_at`。)
- `source_replica`:这个 part 该从哪个副本拉。目标副本不可达时,`GET_PART` 会一直重试,`last_exception` 里写的是连接错误。

排查顺序是:**先用 `system.replicas` 确认"有没有积压"**(`queue_size`、`absolute_delay` 不为 0),**再用 `system.replication_queue` 定位"卡在哪条、为什么"**。前者回答"是不是有问题",后者回答"问题在哪"。和 §22.8 一样,把它接进监控时不要只盯 `queue_size` 的绝对值(写多的大表队列本来就长),要盯的是 `absolute_delay` 与 `last_exception` 是否非空。不过还有一半信息不在这张表里:`system.replication_queue` 只反映**待办**,不反映"正在传的那一份有多大、传了多久"——一条 `GET_PART` 迟迟不消失时,去 `system.replicated_fetches`(§22.13)里看它是不是正在传一个几 GB 的大 part:那里有 `progress` 与 `elapsed`,`source_replica_hostname` 还会告诉你它卡在从哪个副本拉。两张表一个答"还差多少"、一个答"此刻在传什么",合起来才是完整的复制落后图景。

## 22.16 Keeper 的迁移路径

生产落地时最常被问的一句话是:"我手上是 ZooKeeper(或嵌入式 `keeper_server`),怎么搬到独立 Keeper?"下面三条路径,对应的正是 §22.12 讲的那几种部署形态之间的迁移。

**① ZooKeeper → ClickHouse Keeper。** 因为 Keeper **协议兼容** ZooKeeper(§22.12),迁移不必"停机重来":用官方工具 **`clickhouse-keeper-converter`** 把 ZooKeeper 的数据目录(或一份 snapshot)转成 Keeper 的存储格式,再把各节点原来的 `zookeeper` 客户端配置指向新的 Keeper 集群。三个要点:(a) 转换前让 ZooKeeper 处于**静止**状态(停写、确认没有未落盘的日志),否则会漏掉最近的变更;(b) 逐个节点滚动切换客户端地址,期间**不能**让新旧两套一致性服务被不同节点同时当成权威,否则会脑裂;(c) 切换后逐一核对 `system.replicas` 的 `is_readonly` 与 `zookeeper_exception`,再逐步恢复写入。

**② 嵌入式 `keeper_server` → 独立 `clickhouse-keeper` 进程。** §22.12 说过嵌入式把数据库和 Keeper 的生命周期绑在一起,生产要拆开。做法是把原 `config.xml` 里的 `keeper_server` 段(含 `server_id`、`raft_configuration`、`coordination_settings`)独立成 Keeper 进程的配置,起一个或多个 `clickhouse-keeper` 进程组成 Raft 集群,再**滚动地**把各 server 的 `zookeeper` 客户端配置从"连自己的嵌入实例"改成"连新的 Keeper 集群"。顺序很重要:**先把 Keeper 集群起稳、再逐台切 server**,期间任何时刻都必须有一个可用的一致性服务——Keeper 是复制的唯一权威。

**③ 单节点 Keeper → Raft 集群。** 如果是"先单节点跑、后来要扩成 3 节点",本质是给现有数据加副本:把现有节点的存储目录拷到一个空节点,按同一个 `raft_configuration` 启动,让 Raft 把日志补全,确认 follower 追上后再增到第 3 个。§22.12 那条"节点数取奇数"的约束在这里同样成立。

三条路径有一个共同纪律:**每一步都在动复制/共识的元数据,先在非生产环境把整套流程演练一遍,并准备好回退到旧配置的路径。** Keeper 的部署与升级顺序(先升 Keeper、再升 server)在部署章节(第 29 章 §29.12)有更细的安排,这里不重复。

## 22.17 澄清:ReplicatedMergeTree 与 `async_replication`

§22.4 留了一个疑点,这里收掉:不少资料说较新的 ReplicatedMergeTree 提供了一个**表级设置** `async_replication`,走"本地先返回、副本推进顺序放松"的异步复制路径。**这个说法不准确。**

真相是:**`async_replication` 确实存在,但它是 ClickHouse Keeper 的一个选项,不是 ReplicatedMergeTree 的表设置。** 它写在 `keeper_server` 的 `coordination_settings` 段里(§22.12),控制的是 **Raft 的提交是否异步应用**——follower 收到 leader 的日志后,是"先应答、再异步把状态机推进到已提交位置",还是同步完成。这是 Keeper 内部共识层的一个吞吐与延迟权衡,和"某张复制表怎么同步"毫无关系;它是个 `Bool`,**默认 `false`(关闭)**——官方注释说这个默认是为了不破坏向后兼容,要用得在 `<coordination_settings>` 里显式写 `<async_replication>1</async_replication>`。

为什么会有人把它当成表设置?因为两个概念被名字混在了一起。要说清:**ClickHouse 的表级复制本来就是异步的**——副本只是按序追赶日志(§22.11),写成功不代表所有副本都已应用。所以"异步复制"这个说法并不描述一个可供开关的独立模式,真实引擎里也没有这个表设置。真正可用、含义明确的复制流控开关是 `SYSTEM STOP/START REPLICATED SENDS`(停/启向别的副本发送日志条目)与 `SYSTEM STOP/START FETCHES`(停/启从别的副本拉取 part),语义就是字面意思,见第 09 章 §09.11。

结论:建表时**不要**去写 `async_replication`(它不是表设置,写了没用或报未知设置);要调复制的发送/拉取行为,用上面那两条 `SYSTEM` 命令。

## 22.18 与 mini-clickhouse 的对照

对应源码是 `minich/keeper.py` 与 `minich/replication.py`。

**对齐点**(mini 能跑通的):`keeper.py` 里 `ReplicatedTablePaths` 把复制协议翻译成 znode 操作——日志追加、队列指针读写、选主、块号、去重标记、quorum 名单,它就是 §22.4 那棵树的代码形式;`replication.py` 的 `LogEntry`(17 种条目类型,`EMPTY` 到 `KILL`)、`Replica`(日志指针与 part 状态)和 `LocalCluster`(在一个进程里跑 N 个副本、共享一个 `KeeperServer`)能把"插入 → 传播 → 选主 → 合并 → 收敛"演一遍。

**简化点**(mini 代替不了的):Keeper 是**单节点、没有 Raft**,只有一个内存实例加一把 `RLock`,因而**演示不了脑裂与丢失多数派**——这两个场景需要"网络能分区、节点各自选举"的前提;part 传输从 HTTP 简化成内存对象拷贝;副本并发靠调用方显式调 `process_log_entries` 推进,而不是多线程。这些差异不影响协议语义,只是无法复现真实的时序竞争。

## 22.19 小结

- 复制解决**高可用**,分片解决**容量**,两者正交(分片见第 23 章)。
- 副本之间**不互相通信**,都跟 Keeper 说——共识外包,副本只负责"按序应用幂等日志"。ZooKeeper 用 Zab、ClickHouse Keeper 用 Raft,是两套共识。
- 建复制表靠两个引擎参数:**Keeper 路径**(同分片内一致)与**副本名**(同分片内唯一);`<macros>` 与 `default_replica_path`/`default_replica_name` 提供默认值,`{uuid}` 要求各副本同 UUID,所以通常配 `ON CLUSTER` 或 `Replicated` 数据库引擎。
- 复制日志(`/log`)是一串顺序 znode,每条一个操作;应用必须**幂等**,这是整套设计成立的前提。
- 去重靠 `/blocks` 上的标记,受**两个窗口**约束——`replicated_deduplication_window`(计数,25.3 默认 **1000**,**更高版本被调大到 10000,以你的 `system.merge_tree_settings` 为准**)与 `replicated_deduplication_window_seconds`(时间,默认 7 天),并只对**内容完全相同**的块生效;`insert_deduplicate`(默认 1)是总开关,`insert_deduplication_token` 让你用显式幂等键取代内容哈希。
- `insert_quorum` **默认 0、小于 2 等于关闭**,`'auto'` 表示多数;**未达成 quorum 时会删掉已写入各副本的那个块**(保证重试安全);`insert_quorum_parallel=0` 走串行,会让后续插入抛 `UNSATISFIED_QUORUM_FOR_PREVIOUS_WRITE`(队头阻塞)。
- 合并只由 **leader** 决策,其余副本执行,保证各副本结果一致。
- `system.replicas` 里 `total_replicas` 是 `/replicas` 下的已知节点数,`active_replicas` 还要求会话活跃——`active_replicas < total_replicas` 就是"有副本掉线"的信号;`queue_oldest_time`/`queue_newest_time` 给出积压的绝对时间,细节用 `system.replication_queue`(§22.15)逐条定位。
- **`Replicated` 数据库引擎**让库里表自动复制、UUID 统一、DDL 自动传播,是集群建表的最省心方式。
- 数据丢失后按程度恢复:状态可疑先试无损的 `SYSTEM RESTART REPLICA`(单表)/ `SYSTEM RESTART REPLICAS`(本机全部)重挂队列;本地 part 还在用 `SYSTEM RESTORE REPLICA`、只等数据到位不等合并用 `SYSTEM SYNC REPLICA … LIGHTWEIGHT`、清残留元数据用 `SYSTEM DROP REPLICA … FROM ZKPATH`、彻底重来用 detach/attach 从别的副本重灌。
- 默认**最终一致**;`select_sequential_consistency` **只在 `insert_quorum_parallel = 0` 时才提供顺序一致性**,而后者 25.3 默认 1——所以默认配置下开它**不生效**,大多数分析场景也不需要。
- **副本"追不平"先分清是"没得拉"还是"拉不过来"。** 副本的 `log_pointer` 若掉进 `/log` 将被清理的区间里,leader 会把它标成 `is_lost = 1`(服务端日志里那句 `Will mark replica ... as lost, because it has stale log pointer`),从此不再是"补日志追平"、而是"从健康副本整份 clone 重灌";决定这条分水岭的是**表级** `max_replicated_logs_to_keep`(默认 1000),别和 Keeper 自己那条 Raft 日志的 `reserved_log_items`(§22.12)混为一层(§22.10)。
- **看复制落后要两张表一起看**:`system.replication_queue`(§22.15)答"待办里卡了哪条",`system.replicated_fetches`(§22.13)答"此刻正在传哪个 part、传了多少"(`progress`/`elapsed`/`source_replica_hostname`)。要限复制流:表级用 `max_replicated_fetches_network_bandwidth` / `max_replicated_sends_network_bandwidth`,实例级用对应的 `_for_server`(25.3 已改由服务端配置承载),默认都不限速。
- Keeper 是 C++ 的 ZooKeeper;独立或嵌入部署;`coordination_settings` 调会话、日志/快照、`force_sync`、`raft_logs_level`,并可开 Prometheus 端点;它挂掉时**读得了、写不了**,短暂抖动则靠 `keeper_max_retries` / `keeper_retry_*` 一族会话设置吸收重试(§22.13)。从 ZooKeeper / 嵌入式迁移到独立 Keeper 用 `clickhouse-keeper-converter`(§22.16);`async_replication` 是 Keeper 的 `coordination_settings` 选项,不是表设置(§22.17)。
