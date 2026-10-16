# 附录 F:端到端实战(可观测性平台)

把前面三十几章的知识落到一个具体系统上,最好的靶子是**可观测性平台**:它天然包含三类数据(日志、指标、追踪)、天然要处理"写入洪流 + 时间范围查询 + 预聚合"这件 ClickHouse 最擅长的事,也天然踩得到分区、排序键、TTL、物化视图、预聚合状态这些坑。这一章从零搭一个最小可用方案,每一节都给出**可执行 SQL**,并在旁边注明"在真实集群上跑之前需要改什么"。如果只记住一件事,那就是:**每张表的排序键怎么选,决定了它以后是快是慢**,其余都能慢慢调。

## F.1 目标与总体架构

这一节先把全章的目标、边界和"读完你会得到什么"交代清楚,后面每一节都对应这里的一个承诺。

**目标。** 用一套**单集群、可复制**的最小方案,把可观测性平台最核心的三条链路跑通:应用侧的日志、指标、追踪从几个入口送进来(实时流走 Kafka 或 OpenTelemetry/OTLP,指标可由 Prometheus 抓取,历史数据用文件回灌),ClickHouse 落库、按维度预聚合、按保留策略清理,查询侧能回答"错误率随时间变化""Top N 慢接口""P99 延迟随时间变化""按用户维度的错误分布""追踪树还原""漏斗/留存"这几类最常用的分析。

**范围。** 本章**做**的是:一个数据库里的表设计(排序键、分区键、TTL、跳数索引、投影)、指标的多级预聚合(原始/分钟/小时)、四条接入路径(Kafka、文件、OTLP、Prometheus)、一批典型查询,以及保留与冷热分层、容量估算、监控指标、告警与仪表盘、基数治理、模式演进这些运维面。本章**不做**:分片与多集群查询(第 23 章)、Keeper 与多副本的高可用(第 22 章,这里只在 F.8 给一段"把表改成复制表"的最小示例)、告警的发送通道本身(它在库外),也不替读者决定硬件与集群规模——容量是估出来的,必须按你自己的数据重估。

**读完你会得到什么。** 一套可直接跑的 DDL:`logs`(日志)、`metrics_raw` / `metrics_1m` / `metrics_1h`(指标原始点与两级预聚合)、`spans`(追踪)、`events`(行为事件)六张落库表,外加两条预聚合 MV;以及一套可复用的决策方法——每张表的排序键为什么这样选、多级预聚合该定在什么粒度、哪些查询能走预聚合、哪些必须回原始表。这些决定比 SQL 本身更容易选错,也更难事后修改。**(`events` 这张表由应用侧业务埋点直接写入,不在 F.5 的四条接入路径里——详见 F.5 开头。)**

数据流是标准的三段式:

```text
应用 →(Kafka / OTLP / 文件)→ 源表(引擎即入口)→ MATERIALIZED VIEW → 目标表(MergeTree)
                                                     │
                                             预聚合 MV(AggregatingMergeTree)
                                                     │
                                           查询(聚合 + -Merge)
```

三个设计决定贯穿全章,先讲清楚:

- **只有 `MergeTree` 家族落库**。Kafka 引擎表是"门"不是"仓库"(第 04、30 章);真正的数据必须落到 MergeTree,否则没有分区裁剪、没有 TTL、没有副本。
- **物化视图是"插入触发器"**,不是定时任务(第 04 章)。它的作用是把源表的每一次写入即时加工进目标表——这是"为什么必须配 MV"的答案(F.5)。
- **预聚合表存的是"状态"**,不是结果(第 12 章)。写用 `-State`,读用 `-Merge`。

**在真实集群上跑之前需要改什么:** 本章的表名(`logs`/`metrics_raw`/`spans`/`events` 等)、集群名(`'<集群>'`)、Kafka 地址与 topic、TTL 的天数、`storage_policy` 的卷名——这些都是占位符,按你的环境替换。库名统一用 `observability`,可先 `CREATE DATABASE observability ENGINE = Atomic;`。

## F.2 数据建模:日志表

日志表的数据形态是"时间戳 + 若干低基数维度(服务、级别、主机)+ 一段大文本(消息体)"。消息体几乎从不参与过滤,维度恰恰是过滤的主力,所以**排序键要把维度放在前面、时间放在后面**。

```sql
CREATE DATABASE IF NOT EXISTS observability ENGINE = Atomic;

CREATE TABLE observability.logs
(
    ts       DateTime64(3),
    service  LowCardinality(String),
    level    LowCardinality(String),   -- 'info' / 'warn' / 'error'
    host     LowCardinality(String),
    msg      String,
    -- 给日志正文建一个跳过索引,让按关键词的检索不必全扫(见下文)
    INDEX idx_msg msg TYPE tokenbf_v1(256, 2, 0) GRANULARITY 4
)
ENGINE = MergeTree
PARTITION BY toYYYYMMDD(ts)
ORDER BY (service, level, ts)
TTL ts + INTERVAL 30 DAY DELETE
SETTINGS index_granularity = 8192;
```

几处需要解释的取舍:

**分区键用 `toYYYYMMDD(ts)`(按天)。** 天是最常用的查询粒度,也是 TTL 删除的自然单位。别用小时——那会制造海量小分区与海量小 part(第 18 章)。至于**天还是月**,判据是"查询的时间跨度"与"分区数量"两者的取舍:如果查询大多带"最近一天/最近几天"的范围,按天才能让分区裁剪真正生效(按月时单个分区太大,裁不掉多少);只有当查询几乎总是跨整月、且按月得到的分区数仍在可控范围(几万以内)时,按月才更省 part。默认**按天**是安全的折中——真到了"日分区过多"的量级,再考虑按月或按周。

**排序键用 `(service, level, ts)`。** 查询几乎总是"某个服务 + 某个级别 + 时间范围",把 `service`、`level` 这两个低基数维度放在前面,`WHERE service='api' AND level='error' AND ts >= ...` 就能同时裁掉分区和主键 granule(第 11 章)。代价是"只看时间、不看服务"的查询裁剪不到 `service`,但这类查询在日志场景里很少。**如果你们的查询里 `ts` 几乎总是主过滤条件、服务是次要的,就把顺序改成 `(service, ts)` 或 `(ts, service)`——这是本章最需要你按自己负载重新决策的一处。**

**`msg` 放最后、不进排序键。** 大文本列进排序键会让索引与比较都变贵,得不偿失。

**给 `msg` 建一个跳数索引(`tokenbf_v1`)来解决"按关键词搜日志"。** 日志场景里"搜某个错误关键词"是很常见的需求,但 `msg` 不在排序键上,单靠主键裁剪帮不了它。`INDEX idx_msg msg TYPE tokenbf_v1(256, 2, 0) GRANULARITY 4` 给每个 granule 建一个"分词布隆过滤器":查询里的 `WHERE hasToken(msg, 'timeout')` 或 `msg LIKE '%timeout%'` 可以先被它用来整块跳过不含该词的 granule,把"全表扫文本"缩到几个百分点(第 11 章)。三个参数依次是布隆过滤器字节数、哈希函数个数、随机种子;要给大文本调这些参数,以及理解"布隆索引会误报(说有、其实没有)但不会漏报"的语义,见第 11、28 章。**注意它换来的是"少读",不是"精确匹配"**——命中之后仍然要真正比较文本。

**`LowCardinality` 用在 `service`/`level`/`host` 上。** 这三列取值高度重复,字典编码能显著省空间(第 03 章)。

**在真实集群上跑之前需要改什么:** `30 DAY` 改成你的保留期;`Atomic` 若不支持可用默认库引擎;若数据量小,`toYYYYMMDD` 可换成 `toYYYYMM` 减少 part;`tokenbf_v1` 的三个参数按你的日志长度与词分布调;不想加跳数索引就去掉 `INDEX idx_msg` 这一行。

## F.3 数据建模:指标表与预聚合

这一节是全章的**核心**。指标数据的特点是"写入量大、维度固定、查询几乎全是'按维度 + 时间粒度聚合'"。如果每次查 P99 都去扫原始点,代价随数据量线性增长;正确做法是**在写入时就把聚合做掉一半**——这就是 `AggregatingMergeTree` + 物化视图 + `-State` 的意义。

### F.3.1 原始指标表

```sql
CREATE TABLE observability.metrics_raw
(
    ts      DateTime,
    service LowCardinality(String),
    metric  LowCardinality(String),   -- 'cpu' / 'latency_ms' / 'qps'
    host    LowCardinality(String),
    value   Float64
)
ENGINE = MergeTree
PARTITION BY toYYYYMMDD(ts)
ORDER BY (service, metric, ts)
TTL ts + INTERVAL 7 DAY DELETE;      -- 原始点只留短时间,长期靠预聚合
```

### F.3.2 预聚合表:存"状态"

```sql
CREATE TABLE observability.metrics_1m
(
    minute  DateTime,
    service LowCardinality(String),
    metric  LowCardinality(String),
    value_sum AggregateFunction(sum, Float64),   -- 求和用的中间状态
    samples   AggregateFunction(count),          -- 样本数(count 的状态不带参数)
    hosts     AggregateFunction(uniq, String)    -- 去重主机数的状态
)
ENGINE = AggregatingMergeTree
PARTITION BY toYYYYMM(minute)
ORDER BY (service, metric, minute)
TTL minute + INTERVAL 30 DAY DELETE;             -- 分钟级保留 30 天(F.3.6)
```

**注意列类型是 `AggregateFunction(...)`,不是 `Float64`/`UInt64`。** 这里存的是"怎么算出来的、还没算完"的中间态(第 12 章)。选 `AggregatingMergeTree` 而不是普通 `MergeTree`,是为了在后台合并时把同一 `(service, metric, minute)` 的多份状态**自动合并**——这正是增量聚合能成立的前提。

`AggregateFunction(...)` 的括号里,**第一个参数是聚合函数的名字,后面的参数是它接收的参数类型**(`sum` 吃一个 `Float64`,`uniq` 吃一个 `String`)。这里有一处很容易写错:**`count` 没有参数**,所以它的状态类型是 `AggregateFunction(count)`(**不带类型参数**),`countState()` 写出来的就是它。写成 `AggregateFunction(count, UInt8)` 是把"函数的参数类型"和"状态在内存里的表示类型"混为一谈了——前者才是这里该写的东西。要确认自己那个版本需要哪种写法,最稳的一条是 `SELECT toTypeName(countState())`(或看 `SHOW CREATE TABLE` 回读出来的列类型)。

### F.3.3 物化视图:写入时增量聚合

```sql
CREATE MATERIALIZED VIEW observability.metrics_1m_mv
TO observability.metrics_1m
AS
SELECT
    toStartOfMinute(ts)   AS minute,
    service,
    metric,
    sumState(value)       AS value_sum,
    countState()          AS samples,
    uniqState(host)       AS hosts
FROM observability.metrics_raw
GROUP BY minute, service, metric;
```

`sumState`/`countState`/`uniqState` 把聚合的中间态写进目标表。**关键约束:`GROUP BY` 的粒度就是预聚合的粒度**(这里是分钟级),它一旦定下来,就只能回答"分钟及更粗"的查询——这正是 F.3.6 要权衡的代价。

### F.3.4 查询:用 `-Merge` 出结果

```sql
SELECT
    minute,
    service,
    metric,
    sumMerge(value_sum)  AS total,
    countMerge(samples)  AS n,
    uniqMerge(hosts)     AS host_cnt
FROM observability.metrics_1m
WHERE minute >= now() - INTERVAL 6 HOUR
GROUP BY minute, service, metric
ORDER BY minute, service, metric;
```

**写入用 `-State`、查询用 `-Merge`,是本章最该记住的一对**(第 12、33 章)。少写 `-Merge`,拿到的是二进制状态列,不是数值;多写或写错,则报类型不匹配。

### F.3.5 多级粒度:分钟级再"往上报"小时级

分钟级表本身就是一张表,可以在它之上再挂一个 MV,把分钟状态合并成小时状态——注意这里用的是 `sumMergeState`(`-Merge` 再 `-State`,把子状态合并成父状态):

```sql
CREATE TABLE observability.metrics_1h
(
    hour    DateTime,
    service LowCardinality(String),
    metric  LowCardinality(String),
    value_sum AggregateFunction(sum, Float64),
    samples   AggregateFunction(count)          -- 与 metrics_1m 同一套状态类型
)
ENGINE = AggregatingMergeTree
PARTITION BY toYYYYMM(hour)
ORDER BY (service, metric, hour)
TTL hour + INTERVAL 1 YEAR DELETE;               -- 小时级保留 1 年(F.3.6)

CREATE MATERIALIZED VIEW observability.metrics_1h_mv
TO observability.metrics_1h
AS
SELECT
    toStartOfHour(minute)      AS hour,
    service,
    metric,
    sumMergeState(value_sum)   AS value_sum,
    countMergeState(samples)   AS samples
FROM observability.metrics_1m
GROUP BY hour, service, metric;
```

**`sumMergeState` / `countMergeState` 里的 `-MergeState` 是一个真实存在的组合器**(`-Merge` 再 `-State`:把子状态合并成一个新的父状态),所以上面这段 SQL 在你那个版本上应当能直接跑;要自己核一遍:
```sql
SELECT name FROM system.functions
WHERE name IN ('sumMergeState','countMergeState','uniqMergeState');
```
查得到就用它;万一某个名字在你版本里查不到(叠加组合器**并非任意两两都存在**),**退路是不要用链式 MV**:把 1 小时那一层从"物化的表"降级成"查询时的重聚合"——直接 `SELECT toStartOfHour(minute) ... FROM metrics_1m GROUP BY ...`,在查询里对分钟状态做 `sumMerge`/`countMerge`。叠加规则与这条自查方法见附录 A 的 A.15。

### F.3.6 粒度的选择与代价

粒度是"查询能力"与"存储成本"的交换,必须在建 MV 时就拍板。

- **越细(分钟):** 能回答的问题越多(分钟级曲线、短时间窗的尖峰都能看),但状态行数越多——`(service, metric, minute)` 的唯一组合数 × 保留天数。保留 30 天、5 个服务、50 个指标(250 条时间线),分钟级就是 `30 × 1440 分钟 × 250 = 10 800 000 ≈ 1080 万` 行状态;小时级只有 `30 × 24 × 250 = 180 000 ≈ 18 万` 行——正好是前者的 **1/60**。这个比例说明一件事:**颗粒度每细一档,状态行数就按档位倍数往上翻**;选分钟还是小时,本质上是在"能回答多细的问题"和"要多存多少行"之间称重。
- **越粗(小时):** 存储省、查询快,但**低于该粒度的查询只能回原始表**,而原始表通常保留期很短——于是"看过去 30 天某小时的 P99"很快,"看过去 30 天某一分钟"却无数据可查。

实践做法是**多级并存**:原始点保留 7 天(F.3.1 的 TTL),分钟级保留 30 天,小时级保留 1 年(F.3.5 的链式 MV)。查询按时间范围选表:近 1 小时打原始表或分钟表,近 30 天打分钟表,超过 30 天打小时表。

**另一个容易忽略的代价是"写放大":** 每级 MV 都是一条额外的写入路径——一条原始指标写进来,会先落 `metrics_raw`,再触发 `metrics_1m_mv` 写 `metrics_1m`,又触发 `metrics_1h_mv` 写 `metrics_1h`。级数越多,单条写入的端到端成本越高。**级数一般不超过三级。**

**最需要提前想清楚的一点:维度集合一旦写进 MV 就固定了。** 后来想"再加个按 `host` 维度看"就得重建 MV(第 12 章)。

**在真实集群上跑之前需要改什么:** 粒度(分钟/小时)、级数、各表 TTL、`GROUP BY` 里的维度集合;`AggregateFunction(sum, Float64)` 的参数类型要与 `value` 的实际类型一致(整数指标要换成 `sum, UInt64`)。

## F.4 数据建模:追踪表

追踪(span)的数据形态是"一棵以 `trace_id` 聚簇的树":每个 span 有 `span_id`、`parent_span_id`、起止时间与服务/操作名。难点在**排序键的取舍**:按 `service, operation, ts` 排,查"某服务慢接口"很快,但还原一棵 trace 要跨很多 granule;按 `trace_id` 排,还原 trace 快,但按服务过滤就慢了。

```sql
CREATE TABLE observability.spans
(
    ts             DateTime64(3),
    trace_id       UUID,
    span_id        UInt64,
    parent_span_id UInt64,          -- 根 span 约定为 0
    service        LowCardinality(String),
    operation      LowCardinality(String),
    duration_us    UInt64,
    status         LowCardinality(String),   -- 'OK' / 'ERROR'
    user_id        UInt64,
    -- 给"按 trace 还原"预建一份按 trace_id 排的副本(投影)
    PROJECTION by_trace (SELECT * ORDER BY (trace_id, ts))
)
ENGINE = MergeTree
PARTITION BY toYYYYMMDD(ts)
ORDER BY (service, operation, ts)
TTL ts + INTERVAL 14 DAY DELETE;
```

**投影(`by_trace`)是"排序键取舍"的解法。** 上面的排序键服务"按服务/操作分析"(这类查询量最大),但它让 `WHERE trace_id = ...`(还原一棵 trace)裁剪不到——`trace_id` 不在排序键里,于是每次都要扫很多 granule。`PROJECTION by_trace (SELECT * ORDER BY (trace_id, ts))` 让同一份数据**再按 `trace_id` 存一遍**,优化器在识别到 `WHERE trace_id = ...` 这类查询时可以自动改用这份副本,把它变成一次点查(第 05、28 章)。给已存在的表补投影要显式回填:

```sql
ALTER TABLE observability.spans ADD PROJECTION by_trace (SELECT * ORDER BY (trace_id, ts));
ALTER TABLE observability.spans MATERIALIZE PROJECTION by_trace;   -- 对已有 part 生成副本
```

代价是存储与写入放大(每个投影都要存一份、每次写入都要维护它),所以"该不该加投影"的判据是"这类查询是否够多、够慢"。是否命中由 `optimize_use_projections` 控制(**默认值以 `system.settings` 为准**),验证方法是用 `EXPLAIN` 看计划里有没有走投影、以及 `system.query_log` 的读字节数是否下降。

这里选了**面向"按服务/操作分析"的排序键**(因为这类查询量最大);还原 trace 是低频操作,用 `WHERE trace_id = ...` 过滤虽然裁剪不理想,但单条 trace 的 span 数很少、可接受。**如果你们的负载以"还原 trace"为主,就把排序键改成 `(trace_id, ts)`**,或为 `(trace_id, ts)` 建一个 projection 让优化器按需切换(第 05、28 章)。

**在真实集群上跑之前需要改什么:** 排序键方向(见上);`duration_us` 的单位;`parent_span_id` 根值约定(这里用 0);TTL 天数;以及是否保留 `PROJECTION by_trace`(不需要就删掉,它是额外的存储与写放大)。

## F.5 数据接入:Kafka、OTLP、Prometheus 与文件,以及"为什么必须配 MV"

这一节给四条接入路径,外加一条"不走接入路径"的例外(`events`)。**先记住"哪张表由谁来喂",否则 F.6 的查询会查不到数据:**

- **Kafka(F.5.1)落到 F.2 的 `logs`。** `logs_kafka` 是 Kafka 引擎的"门",`logs_kafka_mv` 把流即时推给手写的 `logs`。这条路径与 F.6 的日志查询**直接对得上**。
- **文件(F.5.2)落到 F.4 的 `spans`。** 离线回灌的 `spans_file` 只是"把外部数据当表"的示意,真正落库走 `INSERT ... SELECT` 进 `spans`。这条路径也与 F.6 对得上。
- **OTLP(F.5.3)落到 exporter 自己建的 `otel_traces`,不是 F.4 的 `spans`。**
- **Prometheus(F.5.4)落到 exporter 自己建的 `otel_metrics`,不是 F.3 的 `metrics_raw`。**
- **`events`(F.6 的漏斗与留存要用的行为事件表)不在上面四条路径里。** 它由**应用侧的业务埋点直接写库**——和产生 `spans`/`logs` 的是同一批应用进程,通常就是一条 `INSERT INTO observability.events FORMAT JSONEachRow`(或经同一层适配写入),既不经过 Kafka 那条日志链路,也不经过 OTLP/Prometheus 的 exporter。**这意味着:只按 F.5.1–F.5.4 把四条路径配好,`events` 是空的**,F.6 里凡读 `events` 的查询(漏斗、留存)都会返回 0 行。要让它有数据,得让应用侧把事件写起来;也可以照 F.5.1 给 `events` 再接一条同款 Kafka 链路(`events_kafka` 源表 + MV 落到 `events`),这里不重复。

后两条的表名、列名与类型由 **exporter 决定**,默认与本章手写的那套未必一致,**所以照抄 F.5.3/F.5.4 的配置跑起来,数据会落进 exporter 自己的表,而不是 F.6 查询所用的手写表**。要弥合这个缝,有两条路:**其一**,把 F.6 的查询按 exporter 实际建出的列名改写(先 `SHOW CREATE TABLE` 看清它的列名与类型);**其二**,按 F.2/F.3/F.4 的 schema 自己建表,并让 OTLP / Prometheus 的数据经一层适配写进那套表。**本章 F.6 的所有查询一律假设数据在手写的 `logs` / `metrics_raw` / `metrics_1m` / `spans` / `events` 上**,也就是前两条路径、你自己搭好的适配,或应用侧埋点(对 `events`)所服务的那套表。

### F.5.1 从 Kafka 接入(完整链路)

```sql
CREATE TABLE observability.logs_kafka
(
    ts      DateTime64(3),
    service String,
    level   String,
    host    String,
    msg     String
)
ENGINE = Kafka
SETTINGS
    kafka_broker_list = 'kafka:9092',
    kafka_topic_list  = 'app-logs',
    kafka_group_name  = 'ch-logs-01',
    kafka_format      = 'JSONEachRow';

-- 物化视图:把流即时落到持久表
CREATE MATERIALIZED VIEW observability.logs_kafka_mv
TO observability.logs
AS
SELECT ts, service, level, host, msg
FROM observability.logs_kafka;
```

`Kafka` 引擎表**不是存储**:它读一次就推进消费 offset,数据"取走即消失"。所以:

- **不配 MV,就没有任何东西消费这张表,消息会一直堆在 topic 里;就算有人 `SELECT`,读出来的数据也是"路过",不会被保存。**
- **配了 MV 之后,MV 作为插入触发器,每当 Kafka 表产出一个 block,就把加工过的行推给目标 `logs` 表**(第 04 章)。这才是"流落库"的正确姿势。

**注意 MV 只处理"建视图之后"到达的数据**——Kafka 场景下这点通常无所谓(流是持续的),但如果你先往 `logs` 灌了历史数据再建 MV,那些历史不会自动同步(除非用 `POPULATE`,而它对 Kafka 源表并不适用)。

**在真实集群上跑之前需要改什么:** 把 `kafka_broker_list`/`kafka_topic_list`/`kafka_group_name` 换成你的值;新版本更推荐用 named collection 承载 Kafka 连接参数,**具体语法以你版本的文档与 `system.settings` 为准**;`kafka_format` 与上游实际格式必须一致。

### F.5.2 从文件接入(离线回灌)

历史数据回灌用文件 + `INSERT ... SELECT`:

```sql
CREATE TABLE observability.spans_file
(
    ts DateTime64(3), trace_id UUID, span_id UInt64, parent_span_id UInt64,
    service String, operation String, duration_us UInt64, status String, user_id UInt64
)
ENGINE = File(JSONEachRow);   -- 仅示意"外部数据当表";生产回灌直接用 INSERT SELECT

-- 直接把本地文件灌进目标表(客户端命令,路径在客户端侧)
-- clickhouse-client --query "INSERT INTO observability.spans FORMAT JSONEachRow" < spans.jsonl
```

大批量回灌时,`INSERT INTO spans SELECT ... FROM file(...)` 一次几百万行,注意别一次太大触发 `Too many parts`(第 08、18 章),分批灌。

### F.5.3 从 OpenTelemetry / OTLP 接入

现代可观测性栈里,应用侧发出的通常是 **OpenTelemetry** 的 OTLP 流量(trace/metric/log),而落库这一步**一般不自己写适配器**,而是用 **OpenTelemetry Collector 的 ClickHouse exporter** 把 OTLP 直接写进 ClickHouse。链路是:`应用 →(OTLP)→ OTel Collector →(ClickHouse exporter)→ ClickHouse`。Collector 侧的一份最小配置:

```yaml
receivers:
  otlp:
    protocols:
      grpc: { endpoint: 0.0.0.0:4317 }
      http: { endpoint: 0.0.0.0:4318 }
processors:
  batch: {}                     # 攒批,减少写入次数
exporters:
  clickhouse:
    endpoint: tcp://clickhouse:9000
    database: observability
    traces_table_name: otel_traces
    create_schema: true         # 让 exporter 自己建表(省得手写 schema)
    timeout: 10s
service:
  pipelines:
    traces:
      receivers: [otlp]
      processors: [batch]
      exporters: [clickhouse]
```

`create_schema: true` 时,exporter 会建一张 `otel_traces` 表(**注意它并不是 F.4 那张手写的 `spans`——两者是并列的两张表,列名与排序键各由自己的来源决定**),它的列基本一一对应 OTLP 的 span 模型:`Timestamp`、`TraceId`、`SpanId`、`ParentSpanId`、`SpanName`、`SpanKind`、`ServiceName`、`SpanAttributes`(`Map`)、`ResourceAttributes`(`Map`)、`Duration`、`StatusCode`、`StatusMessage`,以及嵌套的 `Events` / `Links`。**exporter 的具体列名、类型与默认 `ORDER BY` 随版本变化,本书未确认**——以你所用 exporter 版本的 schema 为准(或照着它建的表 `SHOW CREATE TABLE` 看)。它的排序键一般是 `(ServiceName, SpanName, toDateTime(Timestamp))` 这类"先服务、再操作、再时间"的形状,理由和 F.4 完全一样。

**但 `create_schema: true` 有代价,而且代价恰好落在这本书最强调的那几个决定上。** exporter 建表时用的是它**自己的默认 `ORDER BY`**;这个形状通常就是上面那条 `(ServiceName, SpanName, toDateTime(Timestamp))`,**和本章 F.4 推导出的 `(service, operation, ts)` 其实是同一形状**(都是"先服务、再操作、再时间"),所以**排序键的形状本身未必是问题**。真正的差异在别处:**它只给出一条通用 `ORDER BY`,不会替你设 `PARTITION BY`、TTL 与跳数索引**——而这三样恰恰是本章反复强调"建表时就该拍板、事后很难改"的东西;此外**列名与类型也由它自己决定**(很可能和 F.2/F.4 手写的那套对不上,尤其时间列的精度)。等于把"这张表以后是快是慢、留存多久"交给了一个通用默认值。所以上面那份配置只当"先跑通"用;**生产上应当把 `create_schema` 设成 `false`**,自己按 §F.2(日志)、§F.4(追踪)的 schema 把表建好,exporter 只管往里写。关掉它之后要做三件事:**其一**,按本章的 schema 手工建出目标表——最稳的路径是先让它建一次、`SHOW CREATE TABLE` 抄下来,再按你自己的负载改 `ORDER BY` 与 `PARTITION BY`(注意列名与类型要和 exporter 写出的那套对得上);**其二**,确认 exporter 指向的就是你建的那张表(`traces_table_name` / `metrics_table_name` 填你的表名);**其三**,自己补上分区键与 TTL 以及跳数索引——exporter 的默认 schema 一般不会替你按业务设分区粒度和保留期。

两点必须提前想的:**其一,`SpanAttributes`/`ResourceAttributes` 是 `Map`,往 `Map` 里塞高基数键会同时吃掉存储和查询性能**(见 F.8 的基数管理);**其二,OTLP 的 `Timestamp` 是纳秒精度(`DateTime64(9)`),这比 F.4 里手写的 `DateTime64(3)` 精度高得多**,存储成本要按此重估。

**ClickHouse 自身是否内置了 OTLP 接收端(不经过 Collector 直接 `/v1/traces`)在版本间变化,本书未确认。** 生产上稳妥、可移植的做法始终是"Collector 收 OTLP,ClickHouse exporter 落库"——它把"协议适配"和"存储"两件事解耦,换后端不动应用。

### F.5.4 从 Prometheus 抓取指标

指标这条线有两种常见来源,别混:**一是"采集被测服务的 Prometheus 指标"**(应用/中间件暴露 `/metrics`,每 15 秒被抓一次);**二是"Prometheus 抓 ClickHouse 自己"**(监控 CH 的健康度)。前者是"进湖",后者是"监控 CH",用途不同。

"进湖"最省事的做法还是在 Collector 里加一个 `prometheus` receiver,和 OTLP 共用同一个 ClickHouse exporter:

```yaml
receivers:
  prometheus:
    config:
      scrape_configs:
        - job_name: my-services
          scrape_interval: 15s
          static_configs:
            - targets: ['app-1:9100', 'app-2:9100']
exporters:
  clickhouse:
    endpoint: tcp://clickhouse:9000
    database: observability
    metrics_table_name: otel_metrics     # 与 traces 分开的表
    create_schema: true
service:
  pipelines:
    metrics:
      receivers: [prometheus]
      processors: [batch]
      exporters: [clickhouse]
```

抓来的每条样本会落成"度量名 + 标签集 + 时间 + 值"的行,落在 exporter 自己的 `otel_metrics` 上——**注意它不是 F.3 的 `metrics_raw`**。要套用 F.3 那套"分钟/小时预聚合"来收敛存储,得先把样本弄进 `metrics_raw`:或者按 F.3 的 schema 自己建表、让样本经一层适配写进去,或者照 F.3 的思路给 `otel_metrics` 另建一套同形状的 MV。**这里最容易翻车的是标签基数**:Prometheus 的每个不同标签组合都是一条独立时间线,`user_id`、`request_id`、URL 路径里带 ID 这类标签会把时间线数炸到百万级——见 F.8 的基数管理。

如果你要的只是"监控 ClickHouse 自己",那不必经过 Collector:让 Prometheus 直接抓 CH 的 `9363`(`/metrics`),或用 `system.*` 表经 exporter 暴露。这一段属于"监控 CH",F.8 列了要盯的指标。

## F.6 典型查询

下面每一条都是可观测性平台真正会跑的查询,**一律打在 F.2–F.4 手写的 `logs` / `metrics_raw` / `metrics_1m` / `spans` / `events` 上**(不是 F.5.3/F.5.4 里 exporter 自建的 `otel_traces` / `otel_metrics`,原因见 F.5 开头);其中 `events` 由**应用侧埋点写入**(F.5 开头说明),不在那四条接入路径里——**只配好 F.5 不会自动有它的数据**;真实使用请把时间范围、`LIMIT` 改成你的值。每条查询后面都附一个**预期结果的形状**——它的用处是:如果返回 0 行、列名对不上、或某列恒为 0,多半不是 SQL 写错了,而是表没灌进去、或数据接错了路径。样例里的数值是**示意**,用于辨认列与量级,不是你机器上的实测结果。

**错误率随时间变化(日志):**
```sql
SELECT
    toStartOfMinute(ts)                        AS minute,
    countIf(level = 'error') / count()         AS error_rate,
    count()                                    AS total
FROM observability.logs
WHERE ts >= now() - INTERVAL 1 HOUR
  AND service = 'api'
GROUP BY minute
ORDER BY minute;
```

预期是一串按分钟递增的行(一小时约 60 行),`error_rate` 落在 0 到 1 之间,`total` 是那一分钟的请求数:

```text
minute                error_rate    total
2026-10-16 09:15:00          0.032     1840
2026-10-16 09:16:00          0.028     1902
2026-10-16 09:17:00          0.051     1755
```

返回 0 行,通常不是 SQL 的问题,而是这一小时里 `service='api'` 没有日志——先确认 `service` 的取值与 Kafka→`logs` 那条链路在不在跑。

**Top N 慢接口(追踪):**
```sql
SELECT
    operation,
    count()                            AS calls,
    quantile(0.99)(duration_us)        AS p99_us,
    avg(duration_us)                   AS avg_us
FROM observability.spans
WHERE ts >= now() - INTERVAL 1 HOUR
  AND service = 'api'
GROUP BY operation
ORDER BY p99_us DESC
LIMIT 20;
```

预期 1–20 行,每行一个 `operation`;`calls` 是该操作被调用的次数,`p99_us` / `avg_us` 都是微秒,且**每一行都应当 `p99_us >= avg_us`**(P99 是高分位,不会低于均值):

```text
operation          calls    p99_us    avg_us
POST /orders       12480   1824000    154200
GET /items          9821    481000     38200
GET /search         3302    905000    210400
```

如果某一行 `p99_us < avg_us`,那是聚合写错了(例如把 `quantile` 的参数换成了别的东西);如果整个结果为空,先确认这段时间有没有 span 落进 `spans`。

**P99 延迟随时间变化(追踪,分位数按分钟):**
```sql
SELECT
    toStartOfMinute(ts)          AS minute,
    quantile(0.99)(duration_us)  AS p99_us,
    quantile(0.50)(duration_us)  AS p50_us
FROM observability.spans
WHERE ts >= now() - INTERVAL 3 HOUR
  AND service = 'api'
GROUP BY minute
ORDER BY minute;
```

预期是 3 小时、按分钟递增的行(约 180 行),同样**每行都应 `p99_us >= p50_us`**;两条曲线随流量波动,但不会交叉:

```text
minute                p99_us    p50_us
2026-10-16 06:15:00   920000     41000
2026-10-16 06:16:00   884000     39500
2026-10-16 06:17:00  1520000     52000
```

`p99_us` 出现尖峰的那几分钟,正是要回 `spans` 里 `GROUP BY operation` 看"谁在慢"的时刻。

**按用户维度的错误分布(追踪):**
```sql
SELECT
    user_id,
    count()                     AS errors,
    uniqExact(trace_id)         AS traces,
    max(ts)                     AS last_seen
FROM observability.spans
WHERE ts >= now() - INTERVAL 1 DAY
  AND status = 'ERROR'
GROUP BY user_id
ORDER BY errors DESC
LIMIT 50;
```

预期按 `errors` 从多到少排列(最多 50 行);**`traces` 应当始终小于等于 `errors`**——`errors` 数的是错误 span 条数,一条 trace 里可能有好几个错误 span:

```text
user_id    errors    traces    last_seen
100234         37        12    2026-10-16 09:14:02
100987         21        21    2026-10-16 08:51:37
101002          9         4    2026-10-16 09:02:11
```

如果 `traces` 反而大于 `errors`,说明这一列的被聚合对象写错了(例如把 `user_id` 与 `trace_id` 弄反)。

**追踪树还原。** 先取根 span,再列出整条 trace 的所有 span(按时间排序即可看出父子层级,因为每个 span 都带 `parent_span_id`):

```sql
-- 根 span(parent_span_id = 0)
SELECT service, operation, duration_us, ts
FROM observability.spans
WHERE trace_id = toUUID('11111111-1111-1111-1111-111111111111')
  AND parent_span_id = 0;

-- 整条 trace 的全部 span
SELECT span_id, parent_span_id, service, operation, duration_us, status, ts
FROM observability.spans
WHERE trace_id = toUUID('11111111-1111-1111-1111-111111111111')
ORDER BY ts;
```

预期:第一条**恰好返回 1 行**(那条 trace 的根 span,`parent_span_id = 0`);第二条返回这条 trace 的全部 span(几行到几十行),`ts` 严格递增,其中恰有一行的 `parent_span_id` 为 0:

```text
-- 根 span:1 行
service   operation        duration_us   ts
api       POST /orders         1824000   2026-10-16 09:13:12.004

-- 整条 trace:按 ts 升序
span_id   parent_span_id   service   operation        duration_us   status   ts
8421                 0     api       POST /orders         1824000   OK       2026-10-16 09:13:12.004
8422              8421     api       SELECT orders         410000   OK       2026-10-16 09:13:12.431
8423              8421     api       SELECT items          210000   OK       2026-10-16 09:13:12.512
```

如果第一条返回 0 行,先怀疑两件事:`trace_id` 抄错了,或者这批 span 其实是从 OTLP 通道进了 exporter 的 `otel_traces` 而不是 `spans`(见 F.5 的路径对照)。

想直接看"父子配对",用自连接:

```sql
SELECT
    p.operation AS parent_op,
    c.operation AS child_op,
    c.duration_us AS child_us
FROM observability.spans AS c
LEFT JOIN observability.spans AS p
    ON c.parent_span_id = p.span_id AND p.trace_id = c.trace_id
WHERE c.trace_id = toUUID('11111111-1111-1111-1111-111111111111')
ORDER BY c.ts;
```

预期每行一个子 span。根 span 那一行的 `parent_op` 是空串(`join_use_nulls` 默认 0,未匹配的 `String` 列填类型默认值而不是 NULL),其余行 `child_us` 小于其父的 `duration_us`:

```text
parent_op       child_op         child_us
                POST /orders      1824000   -- 根 span,左侧没有父节点
POST /orders    SELECT orders      410000
POST /orders    SELECT items       210000
```

**`ON` 里那个 `p.trace_id = c.trace_id` 不能省。** `span_id` 是普通 `UInt64`、**不是全局唯一**——只在 `c` 侧过滤 `trace_id`,`p` 便可能匹配到**别的 trace** 里编号相同的 span,于是父节点整个错掉;而且 `p` 侧没有被过滤,按 trace 做的投影裁剪(§F.4 给 `spans` 建的 `PROJECTION by_trace`)对 `p` 也用不上,这个自连接会退化成大面积扫描。把 `trace_id` 的等式补进 `ON`,既保证配对只在同一条 trace 内发生,又让两份副本都能按 trace 裁到。

自连接在 ClickHouse 里代价不低(第 15 章);单条 trace 的 span 数少时无所谓,若要批量还原很多 trace,优先用 `groupArray` 把 span 聚成数组再在应用层组树:

```sql
SELECT
    trace_id,
    groupArray((span_id, parent_span_id, operation, duration_us)) AS span_list
FROM observability.spans
WHERE trace_id IN (SELECT trace_id FROM observability.spans
                   WHERE status = 'ERROR' AND ts >= now() - INTERVAL 1 HOUR LIMIT 10)
GROUP BY trace_id;
```

预期最多 10 行,每行一条 trace;`span_list` 是一个 `Array(Tuple(...))`,每个元组是 `(span_id, parent_span_id, operation, duration_us)`,应用层再据此组树:

```text
trace_id                               span_list
11111111-1111-1111-1111-111111111111   [(8421,0,'POST /orders',1824000),(8422,8421,'SELECT orders',410000),...]
```

**漏斗(用户行为转化)。** 漏斗与留存要的是一张**行为事件表**:一行一个"某人在某时刻做了某件事"。它的建模和日志表同构(时间 + 低基数维度 + 事件名),排序键按"先用户、再时间"来,让"某个用户的事件序列"和"某个时间段的某类事件"都能裁到:

```sql
CREATE TABLE observability.events
(
    ts      DateTime64(3),
    user_id UInt64,
    event   LowCardinality(String),   -- 'view' / 'cart' / 'pay'
    props   Map(LowCardinality(String), String)   -- 事件的少量附加属性
)
ENGINE = MergeTree
PARTITION BY toYYYYMMDD(ts)
ORDER BY (user_id, ts)
TTL ts + INTERVAL 90 DAY DELETE;
```

这张 `events` 表由**应用侧埋点写入**(与产生 `spans`/`logs` 的是同一批应用进程),**不在 F.5 的四条接入路径里**——没有哪条 Kafka/OTLP/Prometheus 链路会自动喂它(见 F.5 开头)。

有了它,漏斗要用 **`windowFunnel`**——**不能**三段 `uniqIf` 并列:

```sql
SELECT
    level,
    count() AS users
FROM (
    SELECT
        user_id,
        -- 窗口 86400 秒(1 天);后三个条件按"顺序"排列,统计每人在窗口内按序命中的最大步数
        windowFunnel(86400)(ts, event = 'view', event = 'cart', event = 'pay') AS level
    FROM observability.events
    WHERE ts >= toDate(now()) - 1
    GROUP BY user_id
)
GROUP BY level
ORDER BY level;
```

预期最多 4 行(`level` 只可能取 0..3)。下表里的 `users` 是**每个 `level` 各自的确切人数**(即外层 `GROUP BY level` 的分组计数),**不是"到达第 k 步"的累加值,所以它随 k 不单调**:

```text
level    users
0          420
1          680
2          210
3           96
```

`level = 0` 是"这一步都没走的用户"。判断漏斗对不对,看累加值:`countIf(level >= 1)`(= 680+210+96)是有过 view 的人数,`countIf(level >= 3)`(= 96)才是走完全程的人。

`windowFunnel` 返回一个 0..N 的整数:某人**按序、连续**完成了几步。要"到达第 k 步"的人数,就对 `level >= k` 求 `count`(例如 `countIf(level >= 2)` 是走了 view→cart 的人,`countIf(level >= 3)` 才是真正走完的付费用户)。

**为什么不能三段 `uniqIf(user_id, event='view')` 这样并列?** 因为它们各数各的、**彼此之间没有任何时序约束**——一个"只 pay、从没 view"的用户也会被算进 `step_pay`,甚至可能出现 `step_pay` 大于 `step_view` 的荒唐结果。它算的是"做过某动作的人数",不是"按序完成了漏斗的人数",会把转化**系统性高估**。`windowFunnel`/`retention`/`sequenceMatch` 这一族的定义见附录 A 的 A.14 与第 12 章 §12.6、第 16 章。

**这里做了简化,得说清楚:** `windowFunnel` 用的是**一个全局时间窗**(示例里 1 天),要求从"第一步"起,后续各步在这么多秒内按序出现;它不刻画"每一步各自一个时间窗"(比如 view→cart 须 10 分钟内、cart→pay 须 1 小时内)。需要那种更严格的每步窗口时,用 `sequenceMatch` 或分步 `JOIN` 加时间条件自己拼。

**留存(cohort 留存)** 用"首次活跃日 + 回访日"两段聚合:

```sql
WITH first_seen AS (
    SELECT user_id, min(toDate(ts)) AS cohort_day
    FROM observability.events
    GROUP BY user_id
)
SELECT
    f.cohort_day,
    dateDiff('day', f.cohort_day, toDate(e.ts)) AS day_n,
    uniqExact(e.user_id)                        AS users
FROM observability.events AS e
JOIN first_seen AS f USING (user_id)
WHERE e.ts >= toDate(now()) - 30
GROUP BY f.cohort_day, day_n
ORDER BY f.cohort_day, day_n;
```

预期是一张"每个 `cohort_day` 先有一行 `day_n = 0`,再按回访天数展开"的长表。同一个 cohort 里 `day_n` 递增、`users` 不增(人只会流失,不会凭空多出来),`day_n = 0` 那一行就是这个 cohort 的规模:

```text
cohort_day    day_n    users
2026-09-20        0      520
2026-09-20        1      310
2026-09-20        2      245
2026-09-21        0      488
2026-09-21        1      296
...
```

**为什么这些查询能预聚合加速:** 前四条如果打到 `spans`/`logs` 原始表,扫描量随保留期增长;把 `quantile`/`count`/`uniq` 换成对 `metrics_1m` 的 `-Merge` 查询(能对的维度),延迟就从"扫原始行"变成"扫分钟状态"。哪些查询适合走预聚合、哪些必须回原始表,由 F.3.6 的粒度决定。

**两道"少扫"的闸,分别对应两类查询:** 上面"追踪树还原"那几句 `WHERE trace_id = ...` 之所以能不像全表扫,靠的是 F.4 给 `spans` 建的 `PROJECTION by_trace`;而"在日志里搜关键词"那类查询之所以能跳过大部分 granule,靠的是 F.2 给 `logs` 建的 `tokenbf_v1` 跳数索引。**投影解决"另一种排序顺序"的问题,跳数索引解决"某列不在排序键上、但我要按它过滤"的问题**——两者都不改变结果,只减少读的量,验证方法都是 `EXPLAIN` + `system.query_log` 里的读字节数(第 11、28 章)。

## F.7 保留策略:TTL 与冷热分层

保留策略要同时回答两个问题:**多久的数据该删**(TTL delete),以及**近期的热数据与历史冷数据该放哪块盘**(分层)。两者都通过 TTL 表达。TTL 在**合并时**生效,不是定时任务(第 18 章)。

先定义存储策略(在服务端配置里,给卷命名、绑定磁盘):

```xml
<storage_configuration>
    <disks>
        <ssd><path>/var/lib/clickhouse/ssd/</path></ssd>
        <hdd><path>/var/lib/clickhouse/hdd/</path></hdd>
    </disks>
    <policies>
        <hot_cold>
            <volumes>
                <hot><disk>ssd</disk></hot>
                <cold><disk>hdd</disk></cold>
            </volumes>
        </hot_cold>
    </policies>
</storage_configuration>
```

然后在表上声明"7 天后移到冷卷、30 天后删除":

```sql
ALTER TABLE observability.logs
    MODIFY TTL
        ts + INTERVAL 7 DAY  TO VOLUME 'cold',
        ts + INTERVAL 30 DAY DELETE;

ALTER TABLE observability.logs MODIFY SETTING storage_policy = 'hot_cold';
```

改表级设置必须用 `ALTER TABLE ... MODIFY SETTING name = value`(如上面最后一条);写成 `ALTER TABLE ... SETTINGS name = value` 是**语法错误**(附录 A、第 09 章)。另外要注意:**新策略必须包含旧策略里用到的所有磁盘**,否则这条 `MODIFY SETTING` 会因新策略缺盘而报错——本例 `hot_cold` 里已同时含 `ssd`、`hdd` 两块盘,所以切换是安全的。

**两条规则的顺序有意义:** 先 `TO VOLUME` 再 `DELETE`,表示"先在热卷待 7 天,再挪到冷卷,满 30 天删"。`TO VOLUME` 会触发后台的 part 移动。若目标盘是 S3,把 `<hdd>` 换成 `<s3>` 类型的磁盘即可实现"归档到对象存储"。

**如果不想把 S3 挂成磁盘,还有两种"显式归档"的写法**,它们不依赖存储策略,适合"按时间把一段数据搬去对象存储后即可从本地删掉"的场景。其一是**导出**一段数据到 S3 上的列存文件(Parquet 等),再把本地过期分区删掉:

```sql
-- 把上个月的分区导成 S3 上的 Parquet(路径按日期分目录)
INSERT INTO FUNCTION s3(
    'https://<endpoint>/<bucket>/archive/logs/2025-09/*.parquet',
    '<access_key>', '<secret_key>', 'Parquet'
)
SELECT * FROM observability.logs
WHERE ts >= '2025-09-01 00:00:00' AND ts < '2025-10-01 00:00:00';

-- logs 表是 PARTITION BY toYYYYMMDD(ts):分区 ID 形如 20250901,
-- **没有** '202509' 这样一个分区——9 月是 30 个日分区。先确认要删哪些:
SELECT DISTINCT partition_id
FROM system.parts
WHERE database = 'observability' AND table = 'logs' AND active
  AND partition_id LIKE '202509%'
ORDER BY partition_id;
-- 把上一条的结果拼成 DROP 语句(真实运维里就这么批量生成,再逐条执行)
SELECT concat('ALTER TABLE observability.logs DROP PARTITION ''', partition_id, ''';') AS stmt
FROM (SELECT DISTINCT partition_id FROM system.parts
      WHERE database = 'observability' AND table = 'logs' AND active
        AND partition_id LIKE '202509%')
ORDER BY stmt;
```

确认导出可读之后,再执行上面生成的那些 `ALTER TABLE observability.logs DROP PARTITION '<日分区 ID>';`(如 `DROP PARTITION '20250901'`)。ClickHouse **没有"按前缀删一个月"的语法**,`DROP PARTITION` 一次只吃一个分区 ID,所以按天分区表删一整月,要么像上面这样从 `system.parts` 里把 30 条语句生成出来批量跑,要么在客户端写个小循环。这也顺带说明了按天 vs 按月的一个运维差别:**按月分区删一个月是一次 `DROP PARTITION`,按天分区则是 30 次**——分区粒度是一笔要提前算清的账(F.2、第 18 章)。

其二是**备份**(整表/整分区快照到 S3),它是 ClickHouse 内置的 `BACKUP` 命令,而**不是** TTL 的一部分:

```sql
BACKUP TABLE observability.logs TO S3(
    'https://<endpoint>/<bucket>/backups',
    '<access_key>', '<secret_key>'
);
-- 需要时恢复
RESTORE TABLE observability.logs FROM S3('https://<endpoint>/<bucket>/backups', '<access_key>', '<secret_key>');
```

两者的分工要说清:**导出(`INSERT INTO FUNCTION s3`)产出的是一份"能直接被 `SELECT`/`s3()` 读回来的数据文件",用于把冷数据移出集群**;**备份(`BACKUP`)产出的是可整体恢复的快照,用于容灾**。凭据都别写死在 SQL 里——用 named collection 承载(第 26、30 章)。S3 相关的报错排查见附录 D 的 D.7。

**一个常见误区:** 以为设了 TTL 数据就会立刻少。真相是——**只有发生合并,TTL 才会被执行**。数据量小的表可能几天都不合并一次,于是"过期数据迟迟不删"。需要时可手动 `OPTIMIZE TABLE observability.logs FINAL` 验证 TTL 是否配置正确(生产上别对大表常跑)。实践里还常配 `ttl_only_drop_parts`(整分区一次删,比逐行删高效得多;**其默认值与确切行为以 `system.merge_tree_settings` 为准**)。

**在真实集群上跑之前需要改什么:** 卷名与磁盘路径;`INTERVAL 7 DAY`/`30 DAY` 的具体天数;对象存储场景换磁盘类型与凭据(named collection)。

## F.8 容量、性能与运维

**容量估算(先估,再建)。** 按"行数 × 每行压缩后字节"估,但落库后用实测更可靠:

```sql
SELECT
    table,
    formatReadableSize(sum(bytes_on_disk)) AS disk,
    sum(rows)                              AS rows,
    formatReadableSize(sum(bytes_on_disk) / sum(rows)) AS bytes_per_row
FROM system.parts
WHERE database = 'observability' AND active
GROUP BY table ORDER BY sum(bytes_on_disk) DESC;
```

日志表通常每行几十字节(压缩后),追踪每行几十字节,指标点更小;LZ4 下一般能到 5–10 倍压缩率(第 20 章)。**先按"日增行数 × 保留天数 × 每行字节"估总量,再乘 1.5–2 倍安全余量。**

**排序键如何影响查询延迟。** 维度前缀选对了,一次带过滤的查询只读几个百分点的 granule(第 11 章);选错了,同样的 SQL 会退化成全扫。可以这样验证一张表"裁剪是否生效":

```sql
EXPLAIN ESTIMATE
SELECT count() FROM observability.logs
WHERE service = 'api' AND level = 'error' AND ts >= now() - INTERVAL 1 HOUR;
```

看估算的 part 数与 granule 数是否远小于全表。**这是本章唯一一件"建表时就该拍板、事后很难改"的事。**

**要监控的指标。** 盯这几条(第 27 章):`system.metrics` 的 `DelayedInserts`(写入是否被打压)、`MemoryTracking`(是否逼近 `max_server_memory_usage`)、`BackgroundMergesAndMutationsPoolTask`(后台是否贴顶);`system.events` 的 `FailedQuery` 增量(错误率);再加 `system.parts` 的 part 数增长、`system.disks` 的剩余空间。

```sql
SELECT metric, value FROM system.metrics
WHERE metric IN ('DelayedInserts','MemoryTracking','BackgroundMergesAndMutationsPoolTask');
SELECT name, formatReadableSize(free_space) AS free FROM system.disks;
SELECT database, table, sum(rows) AS rows FROM system.parts WHERE active GROUP BY database, table;
```

**为什么这份清单里没有 `system.replicas.absolute_delay` 和 `ReadonlyReplica`:** 本章为了聚焦单机,**所有落库表都是非复制的 `MergeTree`**(F.2/F.3/F.4 的建表语句都没写 `Replicated`;本章另有两张**不落库**的表——Kafka 引擎的 `logs_kafka` 与 File 引擎的 `spans_file`,它们不涉及复制),而 `system.replicas` 只对复制表产生行——**在这套单机方案上它是空的**,盯它只会得到一个空结果。一旦你上了复制,副本延迟与副本只读立刻变成必须盯的指标(排查入口见附录 D 的 D.6,即第 36 章)。把一张表改成复制表,最小改动就是换引擎并补上 Keeper 路径与副本名:

```sql
CREATE TABLE observability.logs_repl ON CLUSTER '{cluster}'
(
    ts DateTime64(3), service LowCardinality(String), level LowCardinality(String),
    host LowCardinality(String), msg String
)
ENGINE = ReplicatedMergeTree('/clickhouse/tables/{uuid}/{shard}', '{replica}')
PARTITION BY toYYYYMMDD(ts)
ORDER BY (service, level, ts)
TTL ts + INTERVAL 30 DAY DELETE;

-- 上了复制之后,这两条才成为"本方案的指标":
SELECT database, table, absolute_delay FROM system.replicas WHERE absolute_delay > 0;
SELECT metric, value FROM system.metrics WHERE metric = 'ReadonlyReplica';
```

这条语句有两点前提必须说清。**其一,路径里的 `{uuid}`、`{shard}`、`{replica}` 都是宏**:`{shard}` 与 `{replica}` 由服务端 `config.xml` 的 `<macros>` 段提供(第 22 章),没在服务端定义这两项,上面这条 `CREATE` 根本跑不起来。**其二,复制表要在所有副本上成组建出**,建表语句必须带 `ON CLUSTER '{cluster}'`——由发起节点统一生成各副本共用的 UUID,`{uuid}` 才能取到同一个值;若手写 `CREATE` 又不带 `ON CLUSTER`,每个节点各自生成随机 UUID,路径互不相同,表根本不成组(第 22 章 §22.3)。这里刻意用了第 22 章推荐的 `{uuid}` 风格,而不是老文档里 `/clickhouse/tables/{shard}/{库}/{表}` 那种写法——后者**"表一改名路径就断",不是好选择**。

(改复制表会牵动本章多处的建表语句与集群配置,所以这里只给"最小改动示例",不把全章的表都换掉。)

**备份。** 用 `BACKUP` 命令(22.x 起内置),目标走**本章一直在用的 S3**,而不是某个没定义过的本地磁盘:

```sql
-- 备份到 F.7 里用的同一处 S3(凭据用 named collection 承载,别写死在 SQL 里)
BACKUP TABLE observability.logs TO S3(
    'https://<endpoint>/<bucket>/backups',
    '<access_key>', '<secret_key>'
);
-- 恢复
RESTORE TABLE observability.logs FROM S3(
    'https://<endpoint>/<bucket>/backups',
    '<access_key>', '<secret_key>'
);
```

**为什么不用 `Disk('backups', ...)`:本章的存储配置(F.7)只定义了 `ssd` 与 `hdd` 两块本地盘,并没有名为 `backups` 的磁盘**——直接写 `BACKUP ... TO Disk('backups', ...)` 会因为找不到这块磁盘而失败。要落到本地盘,得先在 `storage_configuration` 里把 `backups` 定义出来(并保证它挂在一个不在同一块物理盘上的目录);本章既然已经用 S3 做归档,就统一走 S3,让"备份到哪"前后一致。

也可用 `ALTER TABLE ... FREEZE` 做硬链接快照再配合外部工具。备份策略、恢复演练、扩容与升级的完整流程在第 29 章。

**常见故障怎么处置。** 写入被拒(part 太多)→ 减小批量/查分区键;TTL 不删数据 → 确认是否发生合并;副本延迟 → 查 Keeper 与网络;`Distributed` 查不到数据 → 核对集群配置与写入路径。逐条的症状→原因→确认→修复在附录 D(第 36 章),生产事故的止血清单在第 29 章。

### F.8.1 告警与通知

告警的本质是"把一条判定查询的结果,按周期送到某个通道"。它有两条路线:

**路线一(推荐):判定在 ClickHouse,发送在外部告警器。** 用 Grafana Alerting / Prometheus Alertmanager / 自研定时任务定期跑一条"判定查询",返回非空就通知。比如"过去 5 分钟错误率超过 5%":

```sql
SELECT countIf(level = 'error') / count() AS error_rate
FROM observability.logs
WHERE ts >= now() - INTERVAL 5 MINUTE AND service = 'api'
HAVING error_rate > 0.05;
```

**路线二:让 ClickHouse 自己产出告警行。** 用**可刷新物化视图**(`REFRESH EVERY`;**不再需要实验开关**——旧的 `allow_experimental_refreshable_materialized_view` 自 24.10 起已废弃,与 A.2.7、A.19、C.7 口径一致)或 `window view` 定时把判定结果写进一张"告警表",外部轮询这张表;或用普通物化视图触发外部脚本。这里有一条最容易被忽视的坑:**用物化视图做"写时告警"只在有数据写入时才会触发——数据一断流,任何基于 MV 的告警都不会响。**"指标停了"本身是一种故障,而发现它**只能用定时机制**(在库外按周期检查"最近 N 分钟有没有数据"),不能用 MV。其中 `window view` 一直是实验特性(要开 `allow_experimental_window_view`),而官方文档现已把它标记为**已弃用(deprecated)**、并计划在后续版本移除,所以别在新设计里依赖它;可刷新 MV 则在本节对标的 25.3 上已直接可用。

两条工程纪律:告警查询必须**轻**(带分区裁剪、时间窗很窄,别让告警把库打垮);必须有**抑制与恢复**逻辑(连续 N 次命中才报、连续 M 次正常才恢复),否则会被抖动淹没。具体集成方式随版本与你的监控栈而变。

### F.8.2 仪表盘(Grafana)

主流做法是 Grafana 加 ClickHouse 数据源插件,直接查本附录里的表。设计要点:

- **每块面板先想"它查哪张表"**:趋势类("错误率随时间")查 `logs`;瞬时类("当前 QPS")查最近一分钟;Top-N 类("最慢接口")查 `spans`。
- **时间范围决定打原始表还是预聚合表**:近 1 小时打 `logs`/`metrics_raw`,近 30 天打 `metrics_1m`,超过 30 天打 `metrics_1h`(F.3.6 的分级)。这条决定了面板是"秒级"还是"转圈"。
- **把时间宏接对**:Grafana 会把面板的时间范围替换成类似 `$__timeFilter(ts)` 的片段——确认它落在**分区键列**(`ts`)上,否则分区裁剪失效,面板随数据量变慢。
- **用模板变量**:服务和主机做成模板变量,生成 `WHERE service IN ($service)`,而不是为每种组合建一个面板。
- **别用 `SELECT *`**,也别在仪表盘上挂全表聚合——每块面板都是一条真查询,面板越多、刷新越快,并发越大。

插件与 Grafana 的对接细节(宏名、变量语法)随插件版本变化,**未确认**,以插件文档为准。

### F.8.3 指标基数管理(标签管理)

这是可观测性平台最隐蔽的"慢性病"。在指标场景里,**每一个不同的标签组合就是一条独立的时间线**:如果把 `user_id`、`session_id`、`request_id`、带 ID 的 URL 路径当标签,时间线数会从几十条炸到几十万上百万条——写入的每一行都带一份标签,预聚合表的 `GROUP BY` 键数随之暴增(F.3.6 那套行数估算会被瞬间推翻),查询与内存一起垮。

治理手段,按优先级:

- **把"天然高基数"的值从标签里拿掉**,放进**值/列**而不是标签:`user_id`、`request_id`、原始 URL 都别当标签。标签只放**低基数**维度(服务、主机、区域、方法、状态码)。
- **在采集侧就收敛**:用 OTel Collector 的属性/度量处理器或 Prometheus 的 `metric_relabel_configs`,在数据**进库前**删掉或合并高基数标签——别等落到 ClickHouse 再补救(那时成本已经付过了)。
- **给 `Map` 属性列设边界**:OTLP 的 `SpanAttributes`/`ResourceAttributes` 是 `Map`,往里塞高基数是同一类问题;限制只在需要时抓取,或做采样。
- **监控基数本身**:定期 `uniq` 数一下唯一时间线数,把它当成一个容量指标盯。
- **设上限与降级**:采集链路配"每个指标的最大 series 数",超了就丢弃或降采样,保护后端。

判据只有一句话:**任何一个你打算当标签的列,先问"它的不同取值会不会随时间无限增长"**;会,那它是数据,不是标签。

### F.8.4 元可观测性:把平台自己也纳入监控

前面盯的都是"被观测的应用";而平台自己(ClickHouse)也是一套要被观测的系统——**把自己的 `system.*` 表当监控对象**,才能发现"数据断流了""写入变慢了""查询变慢了"这类平台侧故障。这正是 F.8.1 里那个"MV 只在有写入时才响"死角的补位:应用侧的静默,得靠平台侧的"心跳"来发现。

几条最该固化成定期采集的(第 27 章):

```sql
-- 1) 平台自己的查询:谁在把库拖慢?把慢查询与错误率也当成一类指标
SELECT toStartOfMinute(event_time)                                    AS m,
       countIf(type = 'QueryFinish')                                 AS ok,
       countIf(type = 'ExceptionWhileProcessing')                    AS err,
       quantile(0.99)(query_duration_ms)                             AS p99_ms
FROM system.query_log
WHERE event_time >= now() - INTERVAL 1 HOUR
GROUP BY m ORDER BY m;

-- 2) 摄入是否在走:各源表的行/字节(差分看增速)
SELECT database, table, sum(rows) AS rows, sum(bytes_on_disk) AS bytes
FROM system.parts WHERE active AND database = 'observability'
GROUP BY database, table ORDER BY bytes DESC;

-- 3) 被观测链路的"心跳":最近 5 分钟每张源表有没有新数据(发现"断流")
SELECT 'logs' AS src, count() AS n FROM observability.logs  WHERE ts >= now() - INTERVAL 5 MINUTE
UNION ALL
SELECT 'spans',        count()      FROM observability.spans WHERE ts >= now() - INTERVAL 5 MINUTE;
```

要点:**对 counter 一律差分算速率**(`system.events`/`ProfileEvents` 只增,直接看绝对值没意义);把 `system.errors`、`system.metrics`、`system.asynchronous_metrics` 各挑几条固化成面板(第 27、29 章)。**`system.replicas`、`system.distributed_ddl_queue` 这两张先别急着纳入**——本章所有**落库**表都是非复制的 `MergeTree`(见上文),`system.replicas` 在这套单机方案上是空的;`system.distributed_ddl_queue` 也只有在用过 `ON CLUSTER` 之后才会有条目。等你确实上了复制、或用上了 `ON CLUSTER`,再把这两张表加进来。另外别忘了给 `query_log` 自己也设保留期与 TTL——别让"监控数据自身的监控数据"把盘吃满。

### F.8.5 采样与降采样:先想清楚"要精确到什么程度"

数据量到一定规模,"全量全精度"既不必要也存不下。省的路子有三档,代价与用途不同:

- **聚合降采样(首选)**:就是 F.3 的分钟/小时预聚合——把原始点聚成状态,行数按粒度倍数下降(见 F.3.6 的估算)。它**只对"可合并的聚合"成立**(求和、计数、分位数近似的状态),而且一旦降采样,就答不出更细的粒度。
- **采样(近似)**:建表时声明 `SAMPLE BY`(必须是排序键的前缀),查询里 `SELECT ... FROM t SAMPLE 0.1` 只读约 10% 的行(第 07、11 章)。它便宜,但**只对"按采样键分布均匀"的量无偏**——估"每分钟量级""大体分布"可以,数"精确的错误条数"不行。`uniq`/`quantile` 这类**近似聚合**是另一层面的省:不丢行,但结果本身是估计(第 12 章)。
- **入库前采样/丢弃**:在采集侧(OTel Collector、Prometheus relabel)就按比例丢弃,拦在最前面最省资源,但**丢掉的再也回不来**。

**边界要写进产品说明:** 哪块面板是精确的、哪块是近似的。可观测性里"P99 差一点"通常无所谓,但"错误计数其实是估计值"会让值班的人判断失误。

### F.8.6 Schema 演进:给 `logs`/`spans` 加列时会发生什么

平台的表结构和接入字段一定会变。加列本身好办(`ALTER TABLE ... ADD COLUMN`,元数据级、秒回,第 09 章),但**它和已建好的物化视图之间有个容易踩的坑**:

- **给源表加列,已建的 MV 不会自动带上新列。** MV 的 `SELECT` 是**建成那一刻的查询文本**,往后源表加列,它照旧只把原来那几列加工进目标表。想让新列进目标,得重建 MV(或 `ALTER TABLE ... MODIFY QUERY` 改它的查询,但要保证与目标表结构匹配)。
- **给源表加列,历史 part 用默认值补。** 老 part 没有这一列,读出来是列类型的默认值(或 `DEFAULT` 表达式)——不是错,但要意识到"新列在旧数据上是 0/空"。
- **`AggregatingMergeTree` 目标表加列最难**:状态列是 `AggregateFunction(...)`,旧状态里没有新聚合、没法凭空合并出新值。这类改动通常只能"建新表 + 重新物化 + 换名"。
- **改列类型或改排序键**要评估物化视图、投影是否依赖它(第 09 章)。

**纪律:** 结构大改优先走"建新表 → 双写/回填 → `RENAME` 切换",别在生产表上原地重写;每改一次 MV,都在文档里记下"它的查询文本"和"它依赖哪些列",免得下次加列时踩同一个坑。

## F.9 结语

到这里全书就收尾了,共 39 篇:00–32 是三十三篇正文,后面是附录 A–F 六篇。如果你是**从老系统搬过来的**,附录 E(第 37 章)给的是"怎么搬"的 playbook,本附录给的是"搬过来之后搭什么"的样子——一个把日志、指标、追踪三条链路都走通的端到端例子。搬与建是两件事,合起来才是把 ClickHouse 落到生产上的完整路径。

**接下来读什么。** 想继续深入,三个来源最值:**官方文档**(`clickhouse.com/docs`,每个函数的边界条件写得比任何二手资料都准)、**release notes**(`github.com/ClickHouse/ClickHouse/releases`,新特性与行为变更多在这里首播)、**社区**(Telegram 的 ClickHouse 群与 GitHub Discussions,踩坑经验的密度最高)。最权威的"文档"其实是 `system.*` 系统表——它是你那个版本的**运行时真相**,任何二手答案与它冲突时,以它为准。

最后一句诚实的提醒:这份教程以 **ClickHouse 25.3** 为基准,而 ClickHouse 的迭代速度在数据库领域里属于极快的一档——默认值会调、实验特性会转正、函数会改名或废弃。**凡是本文给出的默认值、行为与语法,在用于生产前,请一律以你所用版本的官方文档与 `system.*` 自查的结果为准**;本文里已经标明"未确认"的地方,更是必须先自查再落地。教程能给你的是地图与直觉,真正的坐标永远在你自己的服务器上。
