# 第 23 章 分片与分布式查询

单机容量有上限:磁盘装不下、内存装不下、写入吞吐也顶到天花板。当数据量跨过那个上限,唯一的方向是**把数据切开、分到多台机器上**——这就是分片。但数据一旦切开,查询就得跨机器:一条 SQL 怎么变成"N 个子查询 + 一次汇总"?汇总时哪些聚合是对的、哪些会算错?这一章讲 Distributed 引擎和它带来的全部问题。

## 23.1 分片与副本是两件事

这两个词在第 22 章 §22.1 已经分过,这里只回指一句、不再重复:分片是**切数据**(要容量与写入吞吐),副本是**复制数据**(要高可用与读吞吐),两者正交,集群通常是"分片 × 副本"的组合。分清之后,分布式查询的本质就清楚了——它要问遍**每个分片的一个副本**、再把结果汇总;Distributed 表做的就是把这件事自动化。

## 23.2 集群配置:remote_servers

分片和副本都在 `remote_servers` 里声明:

```xml
<remote_servers>
    <my_cluster>
        <shard>
            <internal_replication>true</internal_replication>
            <replica><host>ch01</host><port>9000</port></replica>
            <replica><host>ch02</host><port>9000</port></replica>
        </shard>
        <shard>
            <internal_replication>true</internal_replication>
            <replica><host>ch03</host><port>9000</port></replica>
            <replica><host>ch04</host><port>9000</port></replica>
        </shard>
    </my_cluster>
</remote_servers>
```

一个 `<shard>` 是一个分片,里面每个 `<replica>` 是一台机器。

关键参数 **`internal_replication`**,它决定 INSERT 的语义:

- `true`(配合 `Replicated*` 本地表):每条 INSERT **只写给分片里的一个副本**,其余副本靠复制自己同步。这是推荐、也是几乎唯一正确的生产配法。
- `false`:INSERT 写给分片里的**每一个**副本。只在本地表不是复制表时才需要,很容易写出重复数据。

集群拓扑可以用 `system.clusters` 表查看。

**`<replica>` 里不止 host/port。** 常见配置项还有几个:`<user>` / `<password>`——分布式查询在远端以哪个账号执行(远端权限就按这个身份判);`<secure>1</secure>`——走 TLS,通常配合 `<port>9440</port>`;`<compression>true</compression>`——副本间传输开压缩,省内网带宽但费 CPU。分片/副本还能各带一个名字:`<shard>` 一级的 `<shard_name>`、`<replica>` 一级的 `<replica_name>`,它们会被 `system.clusters` 读出来。注意这些名字和 `macros` 里的 `{shard}`/`{replica}` 是**两个来源**:`macros` 是给复制表的引擎路径用的(第 22 章 §22.3),这里是给**集群拓扑**用的,别混为一谈。

**看拓扑用 `system.clusters`。** 它每个(分片, 副本)一行,关键列:`cluster`、`shard_num`、`replica_num`、`host_name`、`port`、`user`、`is_local`、`errors_count`。其中两列最有用:`is_local = 1` 标记"当前这条查询正是跑在这台机器上"——排查"我的查询到底发给了谁"时先看它;`errors_count` 记录"从本机看,该副本连接失败了几次",不为 0 就说明那条副本链路在报错。`system.clusters` 是 `remote_servers` 的**运行时视图**,改完配置用 `SYSTEM RELOAD CLUSTERS` 重新加载(§23.14)。

**分片可以带权重。** 给 `<shard>` 加一个 `<weight>`(默认 1),可以让某个分片按比例承担更多数据——适合"新分片机器更好、想让它多存一点"的场景:

```xml
<shard>
    <weight>2</weight>
    <internal_replication>true</internal_replication>
    <replica><host>ch01</host><port>9000</port></replica>
</shard>
```

权重影响的是写入时的**分片分配比例**,算法在官方 Distributed 文档里写得很具体:**先对分片键表达式求值,再对"所有分片权重之和"取模;余数落在哪个分片的半开区间 `[前面各分片权重累加和, 前面各分片权重累加和 + 本分片权重)` 内,就写到哪个分片**(分片按配置里的顺序编号)。例如两片权重 9 与 10(总权重 19),余数落在 `[0, 9)` 的写第一片、落在 `[9, 19)` 的写第二片,于是两片按 9:10 承担数据。由此有两点实用推论:权重只能按比例调节,**改不了"该由谁承担"**;而把某个分片的权重设为 **0** 是把它**下线的标准手法**——新写入不再落过去,读仍照常从它取数(集群重分布时可以借它渐进切流)。权重和"用 sharding_key 精确控制落点"是两条路,别指望权重能修好一个选错的分片键。

## 23.3 Distributed 表:一个不存数据的代理

```sql
CREATE TABLE events_all AS events_local
ENGINE = Distributed(my_cluster, db, events_local, cityHash64(user_id));
```

`Distributed(cluster, database, local_table[, sharding_key[, policy_name]])` 的参数:

- `cluster`:引用 `remote_servers` 里的名字;
- `database`、`local_table`:每个分片上**真正存数据的本地表**;
- `sharding_key`(可选):写入时按它决定数据落到哪个分片;
- `policy_name`(可选):在分片内挑副本的负载均衡策略。

要点:**Distributed 表自己不存数据。** 查询时它把请求发给各分片的本地表;数据始终在本地表里。所以典型部署是"每个分片一张同名的 `ReplicatedMergeTree` 本地表 + 一张 Distributed 表把它们聚起来",查 Distributed 表 = 查全集群。

## 23.4 分布式 DDL 与 ON CLUSTER

在集群里建表,最大的痛点是"每台机器都要建一遍,还不能建漏"。`ON CLUSTER` 就是解决它的:

```sql
CREATE TABLE events ON CLUSTER my_cluster (...) ENGINE = ReplicatedMergeTree(...);
CREATE DATABASE analytics ON CLUSTER my_cluster ENGINE = Atomic;
ALTER TABLE events ON CLUSTER my_cluster ADD COLUMN os String;
DROP TABLE events ON CLUSTER my_cluster;
```

`ON CLUSTER name` 里的名字是 `remote_servers` 里的一个集群(§23.2)。它的执行模型是**"一个发起者 + 一条 Keeper 上的任务队列"**:

1. 你在**任意一台**机器上发出这条语句(发起节点);
2. 发起节点把这**整条 DDL** 写进 Keeper 上一个约定路径(`<distributed_ddl><path>`,默认 `/clickhouse/task_queue/ddl`)下的**顺序节点**;
3. 该集群里**每台机器上的一个后台 worker** 都盯着这个队列,**各自**把没执行过的任务取出来、在本地执行,再把结果写回队列;
4. 发起节点**轮询**这些结果节点,直到所有机器都报成功或失败,才返回。

几个必须知道的点:

- **它不是原子的。** 某台机器执行失败、别的机器却已经成功时,集群会进入**部分建表**的状态。发起节点返回的是一张"每台机器各自成功/失败"的表;失败的那几台要**手工补**。这是"分布式 DDL 卡住"的常见来源之一。
- **发起节点会等,但有上限。** 等待时长由查询设置 `distributed_ddl_task_timeout` 控制(默认 180 秒)。超时后语句返回"未全部完成"——注意,这**不代表任务被取消**,队列里的任务仍会被各机继续执行,只是你的客户端不等了。
- **没有一条独立的"等待 DDL"语句。** 想知道进度,正确做法是**轮询 `system.distributed_ddl_queue`**:

```sql
SELECT entry, host, status, exception_text
FROM system.distributed_ddl_queue
WHERE status != 'Finished'
ORDER BY entry;
```

  每行是"某台机器上、某个任务的执行状态",`Finished` 才算过。卡住时这里能看到是哪台机器、哪一步没过。
- **DDL 队列自己也有容量与清理**:服务器配置 `<distributed_ddl>` 下的官方名字是 `pool_size`(同时能跑几条 `ON CLUSTER`,示例配置取 1,所以 `ON CLUSTER` 天生**串行**)、`max_tasks_in_queue`(队列最多留多少任务,**默认 1000**)、`task_max_lifetime`(旧任务多久后清掉,**默认 604800 秒,即一周**)、`cleanup_delay_period`(**默认 60 秒**,距上次清理至少隔多久才再次触发)。有的资料把 `max_tasks_in_queue` 写作 `task_queue_size` 或 `task_queue_max_size`,这两个别名能否被解析我未能确认,以官方文档与 `system.*` 的实测为准。调大 `pool_size` 能让多条 DDL 并行,但会给 Keeper 加压,别盲目调。

**和 `Replicated` 数据库引擎(§22.9)的关系**:两者都能"让一条 DDL 走到所有副本",但走的路不同——`ON CLUSTER` 靠上面这条 **DDL 任务队列**;`Replicated` 库靠**库引擎**自己把 DDL 传播出去(连 `ON CLUSTER` 都不用写)。用 `Replicated` 库时推荐配 `distributed_ddl_output_mode = null_status_on_timeout`,免得等待超时把语句判成失败。

**`distributed_ddl_output_mode` 的取值。** 这个查询级设置决定 `ON CLUSTER` 的发起节点**怎么汇报结果**——具体说,是"某台机器没在超时前完成"时抛不抛错:

| 取值 | 发起节点的行为 |
| --- | --- |
| `throw`(默认) | 有机器未在超时前完成就**抛错**(最严格) |
| `null_status_on_timeout` | 超时未完成的机器返回 `NULL` 状态,不抛错;适合"等不等得起无所谓"的批量 DDL |
| `never_throw` | 有机器失败或未完成也**不抛错**,只把状态返回给你自己判断 |
| `none` | 不返回各机状态 |

(默认就是最严格的 `throw`;除上表列出的取值外,还有 `none_only_active`、`null_status_on_timeout_only_active`、`throw_only_active` 这些"不等 `Replicated` 库里不活跃副本"的变体。实践中配 `Replicated` 库最常见的选择是 `null_status_on_timeout`——它把"超时"和"真失败"区分开,不会因为一次等待超时就把整条 DDL 判死。)

**`ON CLUSTER` 与复制**:如果你用的是普通(非 `Replicated`)本地表 + `Distributed` 表,`ON CLUSTER` 只保证"每台机器都建了表",**不保证数据一致**——数据一致性是复制层的事(§23.2 的 `internal_replication`)。

## 23.5 Distributed 异步写队列:运维与诊断

§23.6 会讲清"默认 `INSERT` 是异步的"。异步意味着**数据先落在发起节点的本地磁盘队列里**,再由一个后台线程发往各分片。这个队列的运维直接决定"写的到底丢没丢",有一张表必须会看:

```sql
SELECT database, table, is_blocked, error_count, data_files,
       data_compressed_bytes, last_exception, last_exception_time
FROM system.distribution_queue;
```

- **`is_blocked = 1`** 说明这个表的发送**被卡住了**(通常是反复失败后放弃);此时新数据还在往队列里堆,却发不出去。
- **`data_files` / `data_compressed_bytes`** 是队列里堆了多少待发文件/字节;只涨不落就是"发不出去"。
- **`error_count`、`last_exception`、`last_exception_time`** 是失败次数与最近一次的错误——**定位根因看这里**(最常见是目标分片不可达、磁盘满,或两端表结构不一致)。

后台发送的节奏由一组设置控制,名字值得记:

- `distributed_background_insert_sleep_time_ms`(旧名 `distributed_directory_monitor_sleep_time_ms`):发送线程的基础休眠间隔;**出错时间隔会指数增长**以退避。
- `distributed_background_insert_max_sleep_time_ms`(旧名 `distributed_directory_monitor_max_sleep_time_ms`):上述指数退避的**上限**,免得退避成天文数字。
- `distributed_background_insert_batch`(旧名 `distributed_directory_monitor_batch_inserts`):把队列里的多个小批次**合并成一批**发送,减少往返。
- `fsync_after_insert`:写入本地队列后是否立刻 `fsync`(保证崩溃不丢,代价是慢)。

诊断一个"卡住的队列"的顺序:**先看 `system.distribution_queue` 的 `is_blocked` 与 `last_exception` 定位根因**(多半是某分片连不上或表结构不一致),修好后看队列是否开始下降。**别在积压时直接 `DROP` Distributed 表**——队列里的文件不会跟着消失,反而更难收拾。旧名与新名都还能用,但新名(`distributed_background_insert_*`)是趋势,写监控时优先用新名。

## 23.6 写入路径:数据怎么被分到分片

INSERT 到 Distributed 表时,接收插入的那台(coordinator)按 `sharding_key` 给数据算哈希,决定每一份属于哪个分片,再发过去。

**没有 sharding_key 会怎样?这是最容易踩的坑**:多分片集群上,`INSERT` 会被**直接拒绝**,报错形如 `Method write is not supported by storage Distributed with more than one shard and no sharding key provided`。要允许这种插入,得开 `insert_distributed_one_random_shard=1`(默认 **0**),数据才会被随机分摊到各分片。单分片集群无所谓。

**`insert_distributed_sync`**(默认 **0**):

- `0`(默认,异步):INSERT 把数据先写进 coordinator 的**本地磁盘队列**,立刻返回"成功",后台再发给各分片。→ 经典问题:**写进 Distributed 表后立刻查却查不到**,因为数据还在队列里没发出去。
- `1`(同步):INSERT 等到数据**真的发到所有分片并落到本地表**才返回。慢,但"写完立即可查"。

"写完查不到"的解法有三个:开 `insert_distributed_sync=1`;或者**直接写本地表**(不经 Distributed);或者接受异步、在查询端容忍延迟。生产上大流量写入通常**直接写本地表**,Distributed 表主要用来读。

sharding_key 怎么选:目标是**分布均匀**。常见做法是对高基数列做哈希,例如 `cityHash64(user_id)`。不要用**低基数**的列(`country`、`status` 这种只有几个值的)——它们会让数据倾斜到少数分片。关于"高基数"的澄清见 §23.12。

## 23.7 读取路径:一次查询怎么变成 N 次

查 Distributed 表时,coordinator(收到查询的那台)做四件事:

1. 把这个查询**改写**成对每个分片的子查询;
2. 并行发给各分片的本地表(用连接池,见 §23.10);
3. 收上各分片的部分结果——通常已经是**部分聚合状态**,不是原始行;
4. 再做一次最终合并(聚合、排序、LIMIT)。

**关键优化是把聚合拆成两段。** 与其把各分片的原始行全拉到 coordinator 再聚合(网络量 = 全部数据量),不如让各分片**先做部分聚合**(`sum`/`count`/`uniq` 的中间态很小),coordinator 只合并这些**中间态**——网络量从"原始行数"降到"分组数 × 中间态大小"。这是分布式聚合默认走的路。

- **`distributed_aggregation_memory_efficient`**(默认 1):coordinator 侧**边收边合、按桶分批溢出**地合并各路中间态,而不是把全部分片的部分状态一次性堆进内存——高基数聚合时它把 coordinator 的内存峰值从"全量中间态"压到"单批"。它优化的是上一段那条路径的**收尾阶段**,不改变"分片先部分聚合再回传"这个前提。
- **coordinator 上的两阶段聚合**:当分组数很多、要合并的中间态超过阈值时,最终合并**自己**也会退化成两阶段(先按 hash 分桶部分聚合、再合并),阈值是 `group_by_two_level_threshold`(按分组数)与 `group_by_two_level_threshold_bytes`(按字节)。所以"各分片先聚合"之外还有第二层"coordinator 内部的两阶段";单机聚合的两阶段见第 12 章。
- **`distributed_group_by_no_merge`**(默认 0):让各分片各自**完整**完成 `GROUP BY`、coordinator 不再二次合并。只有"每个分组完全落在一个分片里"(比如按分片键分组)时才安全;设错得到的是**错误结果**,不是变慢。
- **`distributed_push_down_limit`**(默认 1):把 `LIMIT` 尽量下推到各分片,减少回传量。它下的"每个分片最多取这么多",coordinator 再取全局的;有 `ORDER BY` 时下推要对排序语义正确。
- **`optimize_skip_unused_shards`**(默认 0):把"扫全集群"缩成"扫几个分片",但前提不少——见下。

**`optimize_skip_unused_shards` 的前提,比"开个开关"复杂得多。** 打开后,coordinator 会从 `WHERE` 里**静态推断**哪些分片可能命中,只把子查询发给那几个分片。前提是:

- `WHERE` 里必须出现**分片键**(sharding_key)上的条件,且是**等值或 `IN`**;
- 该条件必须是分片键表达式的**确定性函数**——也就是能静态算出"这个值属于哪个分片"。像 `cityHash64(user_id) % N` 这种分片键,`WHERE user_id = 5` 就**推不出来**(哈希不可逆),而 `cityHash64(user_id) = 12345` 反而可以;涉及非确定性函数(`rand()` 等)一律推不出。
- `IN` 的右侧若是子查询,需要 `optimize_skip_unused_shards_rewrite_in`(默认 1)允许把它**改写**成对分片键的直接过滤。
- 想**强制**它生效(做不到就报错,而不是静默扫全集群),用 `force_optimize_skip_unused_shards`:`0` 关闭强制、`1` 在有分片键却推不出时报错、`2` 无条件报错。报错信息形如 `Cannot skip unused shards in distributed query`。

换句话说,只有当"`WHERE` 直接约束了分片键、且能静态求解"时它才有用。想让它经常生效,分片键应当尽量是**可静态求值**的表达式(或用 `IN` 直接列出分片)。

## 23.8 GLOBAL JOIN 与 GLOBAL IN:不是优化,是正确性问题

这是本章最容易出错的地方。

**问题**:普通 `JOIN` / `IN` 的右表会被**下发到每个分片执行**,而每个分片只看到**自己那一份局部数据**。如果右表的分布和左表不一致,右表在每个分片上都是不完整的——结果错误,而不只是慢。

先记住**默认行为**:在两表都是分布式表时,裸的跨分片 `JOIN`/`IN` **默认会直接报错**,不是静默出错。报错形如 **Error 288 `Double-distributed IN/JOIN subqueries is denied`**,根因就是下面那个 `distributed_product_mode` 的默认值 `deny`。这一步很关键:很多人以为自己看到的是"结果悄悄少了",其实**默认配置下这条查询根本跑不起来**。

那"静默丢结果"是怎么来的?得先把门打开。假设集群两个分片,左表 `dist_events` 每个分片都有各自的事件,右表 `dist_users` 的数据**全部落在分片 1**(比如它恰好被 sharding_key 分到了那边),分片 2 上这张表是空的。执行:

```sql
SET distributed_product_mode = 'allow';   -- 默认是 deny,不先打开这条查询会直接报 Error 288
SELECT count() FROM dist_events INNER JOIN dist_users USING user_id;
```

打开 `allow` 之后,行为才是"静默出错":

- coordinator 把 JOIN 原样下发;
- 分片 1 用自己那份 users 做 JOIN,能匹配上;
- 分片 2 拿到的本地 users 是**空的** → 它这边所有 JOIN 结果都被丢掉;
- 最终 count 偏小,而且**不报错**。

这正是 `deny` 默认值的意义:它逼你在"可能悄悄少行"和"显式承担广播代价"之间做选择,而不是默认掉进后者。

**正确做法**是用 `GLOBAL`:

- `GLOBAL JOIN` / `GLOBAL IN` 的语义是:先在 coordinator 上把右表**整体**算出来,做成一张临时表,再**分发到每个分片**,各分片用**完整**的右表来 JOIN。
- 代价是右表要复制到每个分片(网络和内存开销),但结果**正确**。

所以规则是:`GLOBAL` 不是"加速开关",而是**跨分片 JOIN/IN 的正确性开关**。右表越大、复制成本越高——这正是 ClickHouse 反复强调"少 JOIN、宽表化"的又一个原因。

还有一个相关设置 **`distributed_product_mode`**(默认 `deny`),它决定"分布式子查询出现在 `IN`/`JOIN` 里"时的行为:**`deny`(默认)直接拒绝**,报 `Double-distributed IN/JOIN subqueries is denied`(Error 288);`local` 把子查询换成该分片的本地表(只在两表按同一分片键共置时才对);`global` 自动改成 `GLOBAL IN`/`GLOBAL JOIN`;`allow` 放行不带 `GLOBAL` 的子查询——也就是上面用来复现"静默丢结果"的那一档。这个 `deny` 默认值,是很多人第一次跨表 JOIN 就报错的原因。

## 23.9 remote() / cluster() 表函数

不建 Distributed 表也能直接读远端:

- `remote('host:9000', database, table[, user[, password]])`——直接连**一台**机器(走 TCP 9000);
- `remoteSecure(...)`——TLS 版(通常 9440);
- `cluster('name', database, table)`——走集群配置,问每个分片的一个副本;
- `clusterAllReplicas('name', db, table)`——问**每个分片的每个副本**,能把同一分片的多个副本都用起来。

区别在于:`remote()` 直接给地址、不依赖配置;`cluster()`/`clusterAllReplicas()` 依赖 `remote_servers`。`clusterAllReplicas` 常用于"遍历所有副本"的校验、运维查询。

**失败语义要分清**:`cluster()`/`clusterAllReplicas()` 遇到不可达节点时的行为,受 `skip_unavailable_shards` 控制(默认 0)。默认下,某个分片(或副本)不可达**会让整条查询失败**;把它设成 1 则**跳过**不可达的节点、继续返回其余节点的结果——但结果可能**不完整**,所以要清楚自己在放宽什么。`clusterAllReplicas` 尤其敏感:它**遍历每个分片的每个副本**,因此哪怕只挂一个副本,也可能让整条查询失败——这也是它更适合运维校验、而不适合在线查询的原因。对 `remote()`,则没有任何"跳过"可言,连不上就是失败。

**慢和连不上是两类失败。** 连不上会立刻报错,而"连上了、却迟迟不返回"会被时限兜住:`connect_timeout_with_failover_ms` 管连接(以及在同一分片内换副本重连)的等待毫秒数,`receive_timeout` / `send_timeout` 管连接建立之后读 / 写的空闲时限。这几个值默认都偏宽松(设计取向是"宁可等,也不误杀一条长查询"),所以一个假死的远端可能让查询卡很久才报错,而不是立刻失败——"查询不报错、就是一直不返回"时,先想到这几个时限。还要注意,`remote()` 只认你给的那一个地址,§23.10 里 `connections_with_failover_max_tries` 那套"失败后换同分片另一副本"对它**不生效**;要跨副本容错,得走 `cluster()` 或干脆用 `Distributed` 表。

## 23.10 负载均衡、容错与常用设置

- **`load_balancing`**:coordinator 在某个分片的多个副本之间挑哪个来读。取值有 `random`(默认)、`nearest_hostname`、`in_order`、`first_or_random`、`round_robin`。
- **`prefer_localhost_replica`**(默认 1):如果本机就是该分片的一个副本,**优先读本机**——省一次网络往返。分布式查询里这通常是想要的;只有在"本机副本很忙、想把读压力摊出去"时才考虑关掉。
- **`max_replica_delay_for_distributed_queries`**(默认 300 秒)与 **`fallback_to_stale_replicas_for_distributed_queries`**(默认 1):挑副本时,某副本的 `absolute_delay`(滞后)超过前者就被排除;如果**所有**候选都太滞后,后者决定是否回退到用滞后副本(1=允许,宁可读旧数据也要出结果;0=宁可报错)。
- **`skip_unavailable_shards`**(默认 0):某分片不可达时,是让整条查询失败(0),还是跳过它、用其余分片的结果(1)。设 1 换来"总能出结果",代价是结果**可能不完整**,要自己想清楚能不能接受。

**连接相关的设置**(连接池与重试)决定了 coordinator 能同时开多少条到分片的连接:

- **`max_distributed_connections`**:**单条查询**能同时打开的最大连接数,默认 **1024**;
- **`distributed_connections_pool_size`**:连接池大小——与各分片之间**复用**的连接上限,默认 **1024**(它是"所有查询合计"的口径,与上一条"单条查询"不同,别混);
- **`connections_with_failover_max_tries`**(默认 3):一个副本失败后,发往"同分片的下一个副本"重试几次。

这几条很少需要改,但在"集群很大、分片很多、查询很宽"时,连接数会撞到它们,表现为"查询排队/超时"。两个连接数默认值都以 `SELECT * FROM system.settings` 的实测为准。

**写入与跨分片查询的另外两条**:

- **`insert_distributed_timeout`**(默认 0):写 Distributed 表时等待分片确认的超时(0 表示不超时)。异步写(`insert_distributed_sync=0`)时它影响不大,同步写时它是保护。
- **`parallel_distributed_insert_select`**(默认 0):`INSERT INTO distributed SELECT … FROM distributed` 时,让 `SELECT` 和 `INSERT` **在分片本地成对完成**,而不是把数据先拉到 coordinator 再分发出去。分片之间做数据搬运时,它能省掉一次中心化往返——是"Distributed 对 Distributed 做 ETW/重分布"时值得开的设置。

## 23.11 并行副本:让一个分片的多个副本一起读

到目前为止,"加副本"提升的是**可用性**;**并行副本(parallel replicas)** 改变这一点:一条查询可以**同时下发给同一个分片的多个副本**,每个副本只读一部分数据再合并,于是"加副本"也能提升**读吞吐**。它在"副本多、单副本读不过来"的宽表扫描场景里收益明显。**机制——coordinator 怎么把 granule 切给各副本、为什么依赖 analyzer——在第 10 章讲;这一节只讲在集群里怎么开、怎么调、怎么看。**

- **`max_parallel_replicas`**:限定"一个分片最多用几个副本"。注意它**不是开关**,而且 25.3 上它的默认值是 **1000** —— **25.1** 由 `1` 抬上来的(`SettingsChangesHistory.cpp` 里那条改动落在 25.1 的块内,25.3 的 `DECLARE(NonZeroUInt64, max_parallel_replicas, 1000, ...)` 也印证;这本书早先把它标成"未确认",是因为二手资料在 24.12 与 25.4 之间打架,现在可以从源码定案)。也正因为默认值早就大于 1,"设成大于 1 就启用"的老经验已经失效,真正的开关是下面那条。以你那一版 `SELECT value FROM system.settings WHERE name = 'max_parallel_replicas'` 的实测值为准。
- **功能开关**:`enable_parallel_replicas` 与 `allow_experimental_parallel_reading_from_replicas` 是**同一个设置的两个名字**,取值 0/1/2(`0` 关 / `1` 开、失败时静默降级 / `2` 开、失败时抛错)。**谁是规范名,25.3 的源码给了明确答案**:声明长这样 —— `DECLARE(UInt64, allow_experimental_parallel_reading_from_replicas, 0, ...) BETA) ALIAS(enable_parallel_replicas)`,也就是说**长名是声明名、短名是它的 `ALIAS`**,而且这个特性在 25.3 已是 **BETA** 档(本书早先在这一条上标的是"规范名与改名方向未确认",现在可以按源码定案)。用 `SELECT name, value, alias_to FROM system.settings WHERE name LIKE '%parallel_replicas%'` 可以当场看到 `enable_parallel_replicas` 的 `alias_to` 指向长名。要跑并行副本还需:集群版本 23.3+、同一个分片里得有多个副本,并配套设好 `max_parallel_replicas` 与 `cluster_for_parallel_replicas`。
- **它要求开启 analyzer**(`parallel_replicas_only_with_analyzer` 默认 1),analyzer 关着时会回退成本地执行。
- **`parallel_replicas_custom_key`**:用一个**自定义的整数表达式**决定"怎么把工作切给各副本"。默认按 `SAMPLE` 键切;给了 custom key 就按它切——在**单分片多副本**的集群上,这个 key 会把多个副本**当成虚拟分片**处理。使用前确认各副本的数据切分不重叠。
- **监控**:`system.query_log` 里的 `ParallelReplicasReadAssignedMarks` 之类的 ProfileEvent 能看出到底有几个副本真的参与了读。

并行副本是 25.x 里还比较"新"的能力,配置项和默认值在版本间仍在动——上线前务必在目标版本上实测,别照搬别处的配置。

## 23.12 分片键选错了怎么补救

分片键一旦定下,数据就按它散开了。要改,只能**重分布**:

1. 建一张新的 Distributed 表(新的 `sharding_key`)指向新的本地表;
2. `INSERT INTO new_dist SELECT * FROM old_dist`——数据被按新键重新散到各分片;
3. 校验无误后 `RENAME TABLE` 把新表换成正式名字(或让应用切过去)。

这里要澄清一个流传很广的混淆:常听人说"不要用高基数列做分片键",但那句话其实说的是**分区键(partition key)**——分区键基数太高会炸出成千上万个分区/part(第 18 章)。**分片键恰恰相反**:它需要**分布均匀**,而对高基数列做哈希(`cityHash64(user_id)`)正是获得均匀分布的标准手法。真正该避免的分片键是**低基数或倾斜**的列(`country`、`status`),它们会把数据挤到少数几个分片。把"分区键要低基数、分片键要均匀"记成一句,就不会再混。

**重分布的两条工具**:

- **`clickhouse-copier`**:官方提供的"分片间/集群间搬数据"工具。它把每条搬运任务(哪个源表、按什么键、搬到哪个目标)写进 **Keeper**,因此支持**失败恢复**——中断了能从断点接着做,不用从头再来。数据量很大、又要确保"一条不落"时,它是首选,而不是手写 `INSERT … SELECT`。
- **`ALTER TABLE … MOVE PART … TO`**:把个别 part 迁到另一个分片,适合"少量数据的手工腾挪"。注意语法是 **`MOVE PART`(不是 `MOVE PARTITION`)`TO SHARD`** 到目标分片的 Keeper 路径,例如 `ALTER TABLE t MOVE PART 'xxx' TO SHARD '/clickhouse/tables/02/…'`(关键字 `TO SHARD` 不能省);它受设置 `part_moves_between_shards_enable`(默认 0)控制,要显式打开,进度可查 `system.part_moves_between_shards`。**`MOVE PARTITION … TO SHARD` 这种写法是无效的**:解析器只在 `MOVE PART` 分支接受 `TO SHARD`,而 `MOVE PARTITION` 只接受 `TO DISK`/`TO VOLUME`/`TO TABLE`(`MOVE PART … TO TABLE` 同样无效)。别记错。

给正在运行的集群做重分布,通常先用 `clickhouse-copier` 把历史数据搬过去,再把新写入切到新表,最后下线旧表——把"搬迁"和"切流"分开,才有回退的余地。

## 23.13 替代方案

Distributed 表不是唯一选择,常见的替代有:

- **把 Distributed 当"只读聚合层"**:写入走本地表(应用自己路由),Distributed 表只用于跨分片查询。这样绕开了异步队列那个坑。**什么时候选它**:写入延迟敏感、又不愿接受 §23.5 的异步队列延迟,且应用本就能按分片键路由;**代价**:路由逻辑进了应用、写端必须自己知道分片规则,应用与集群拓扑耦合。
- **Replicated 表 + 应用层路由**:每个分片一张复制本地表,由应用决定写哪个分片、查哪个分片。**什么时候选它**:吞吐压力大、对"写去哪、从哪读"要有完全控制权(比如按租户固定落片)的场景;**代价**:控制力最强的同时也是复杂度最高的——分片规则、故障时的重路由、跨分片聚合都得自己写,业务代码里会长出一层"迷你查询层"。
- **云上的 `SharedMergeTree`**:存算分离,扩容和副本同步由存储层负责,少了很多 Distributed 层的手工活。**什么时候选它**:在 ClickHouse Cloud 上想弹性扩缩、不愿手工管副本;**代价**:它是云托管的、自建环境基本用不到,等于把一部分控制权交给平台。

## 23.14 Distributed 的运维:虚拟列与两条 SYSTEM 命令

**两个虚拟列,查"数据从哪来"很有用。** 对 Distributed 表做 `SELECT` 时,可以带上 `_shard_num` 和 `_replica_num`——它们是结果所在分片/副本的编号,取值对应 `system.clusters` 里的 `shard_num` / `replica_num`(§23.2)。`_shard_num` 让你一眼看出"这行结果从哪个分片来",调试数据倾斜、核对 `optimize_skip_unused_shards` 到底跳没跳过某些分片时都靠它:

```sql
SELECT _shard_num, count() FROM events_all GROUP BY _shard_num ORDER BY _shard_num;
```

(提醒:`_shard_num` 是**集群拓扑里**的分片号,不是"由分片键算出的桶号";两者分布是否吻合,恰恰是分片键选得好不好的体现。)

**`SYSTEM FLUSH DISTRIBUTED`——把队列立刻推空。** §23.5 讲过默认 `INSERT` 是异步的:数据先落本地队列、后台再发。`SYSTEM FLUSH DISTRIBUTED db.dist_table` 就是"**现在立刻把队列里的数据同步发往各分片、等发完才返回**",不管 `insert_distributed_sync` 是不是 0:

```sql
SYSTEM FLUSH DISTRIBUTED db.events_all;
```

用法很明确:测试里"写完立刻查"要先 flush;下线一张 Distributed 表之前要先 flush,否则队列里没发出去的数据会随表一起被清;`system.distribution_queue` 显示有积压、确认某分片恢复后想立刻重推,也用它。它和 `insert_distributed_sync = 1` 的区别是:**这是一条一次性命令,不改变后续 INSERT 的默认异步行为**。

**`SYSTEM RELOAD CLUSTERS`——改完拓扑不用重启。** 改完 `remote_servers` 配置后,让它重新加载集群拓扑:

```sql
SYSTEM RELOAD CLUSTERS;
```

加载完用 `system.clusters` 核对新拓扑(§23.2)。它只重载**拓扑**(有哪些分片/副本、编号),不动数据,也不影响在跑的查询;配置语法有错时它会报错并保留旧拓扑——所以"改了配置怎么没生效"的第一步就是查它。

## 23.15 与 mini-clickhouse 的对照

要**诚实说明**:mini-clickhouse **没有真正的分片实现**——它是**单进程**的,没有多节点、没有 `remote_servers` 拓扑、没有 Distributed 引擎的行为,也没有 `GLOBAL JOIN`。

**对齐点**(代码里留着的扩展点形状):`minich/execution/context.py` 的 `Context` 有 `clusters`(集群拓扑,默认空字典)与 `remote_executor`(远端执行器,默认 `None`)两个字段;`minich/storages/table_functions.py` 里 `remote` / `cluster` / `clusterAllReplicas` 是**委托桩**——取到 `remote_executor` 就交给它,取不到就抛 `NOT_IMPLEMENTED`;`minich/storages/__init__.py` 的引擎清单里 **`Distributed` 仍是注释里的占位**(写着"见第 21 章与第 32 章 B.6")。

**简化点**:`Replicated*` 倒是**已经注册**(`ReplicatedMergeTree`、`ReplicatedReplacingMergeTree`,后者映射到前者),所以在 mini 里"复制表建不出来"是误解——**建不出来的是 `Distributed` 表**。要支持这一章,得在两层动手:在 `minich/storages/` 加一个 `Distributed` 引擎(按 sharding_key 分发写入、把查询下发给各分片),再给 `remote_executor` 钩子接一个真实执行器。**这两件都还没做**——本章讲到 Distributed 的行为,依据的是真实 ClickHouse,不是 mini-clickhouse。

## 23.16 小结

- 分片是**切数据**(要容量),副本是**复制数据**(要高可用),两者正交;`<shard>` 还能带 `<weight>` 调分配比例。
- `remote_servers` 描述拓扑;`internal_replication=true` 时 INSERT 只写分片里的一个副本,其余靠复制同步。`<replica>` 里还能配 `<user>`/`<password>`/`<secure>`/`<compression>`,分片/副本名字用 `<shard_name>`/`<replica_name>`;拓扑用 `system.clusters` 看,`is_local` 标本机、`errors_count` 记连接失败次数。
- `Distributed(cluster, db, local_table[, sharding_key[, policy]])` 是**不存数据的代理**,数据永远在本地表里;`_shard_num`/`_replica_num` 两个虚拟列告诉你结果从哪个分片/副本来(§23.14)。
- **分布式 DDL 用 `ON CLUSTER`**:靠 Keeper 上的一条 DDL 任务队列(§23.4);它**非原子**、发起节点按 `distributed_ddl_task_timeout`(默认 180s)等待、进度看 `system.distributed_ddl_queue`(没有独立的"等待 DDL"语句);结果格式由 `distributed_ddl_output_mode` 控制(§23.4);`Replicated` 数据库引擎是另一条路(§22.9)。
- **异步写队列**(§23.5):`INSERT` 先落本地队列,发不出去看 `system.distribution_queue` 的 `is_blocked`/`last_exception`;节奏由 `distributed_background_insert_*`(旧名 `distributed_directory_monitor_*`)、`fsync_after_insert` 控制。要立刻推空用 `SYSTEM FLUSH DISTRIBUTED`(§23.14)。
- 多分片下 INSERT **必须**有 `sharding_key`,否则被拒(除非开 `insert_distributed_one_random_shard`)。
- `insert_distributed_sync` 默认 0(异步):数据先进本地队列再后台发,这是"写完查不到"的根源。
- 跨分片 JOIN/IN 要正确,得用 **`GLOBAL`**;**`distributed_product_mode` 默认 `deny`——裸的双分布式 JOIN 直接报 Error 288,而不是静默出错**;只有 `SET distributed_product_mode='allow'` 之后才会出现"静默丢结果"(§23.8)。
- 读取侧:聚合默认"分片先部分聚合、coordinator 再合并"(§23.7);`optimize_skip_unused_shards` 只在 `WHERE` 直接约束分片键且能**静态求解**时才有用(配 `force_optimize_skip_unused_shards`、`optimize_skip_unused_shards_rewrite_in`)。
- 常用设置:`prefer_localhost_replica`、`skip_unavailable_shards`、`max_distributed_connections`、`distributed_connections_pool_size`、`connections_with_failover_max_tries`、`insert_distributed_timeout`、`parallel_distributed_insert_select`(连接池两个的默认值都是 **1024**,以 `system.settings` 实测为准,§23.10)。
- **并行副本**(§23.11)让一个分片的多个副本一起读,靠 `max_parallel_replicas`(25.3 默认 **1000**,**25.1** 起;以 `SELECT value FROM system.settings WHERE name = 'max_parallel_replicas'` 实测为准)与功能开关控制;机制见第 10 章,是 25.x 里较新的能力。`parallel_replicas_custom_key` 决定数据怎么在副本间切。
- 分片键要**均匀**(高基数列做哈希),别和"分区键要低基数"弄混;重分布用 `clickhouse-copier`(可断点续传)或 `MOVE PART … TO`(注意不是 `MOVE PARTITION TO SHARD`)。
- 改完 `remote_servers` 用 `SYSTEM RELOAD CLUSTERS` 重新加载(§23.14);mini-clickhouse **没有**分片实现,只在上下文里留了 `remote_executor` 钩子。
