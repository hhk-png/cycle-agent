# 附录 N 实时数仓建模手册

第 13 章讲 CDC 与湖仓，第 14 章讲 Table/SQL，第 15 章讲窗口 TVF，第 19 章讲双流 Join。它们回答“用什么技术把数据算出来”；本附录回答一个更靠前、也更容易被低估的问题：

> 实时链路应该按什么粒度组织事实和维度？哪些指标能在流上直接聚合，哪些必须回到湖仓重算？当业务口径变化、维度历史需要追溯、事实迟到或更正时，模型还能不能自洽？

实时数仓不是把离线数仓的 SQL 加一个 `Kafka` source。它继承了维度建模的核心思想，但必须重新回答“完整性、更新、保留和成本”四个问题。建模错了，后面再强的 Flink 调优也只是把错误算得更快。

```text
本附录的位置
  第 13 章   CDC、湖仓与实时数仓分层
  第 14 章   动态表、DDL、CTAS/RTAS
  第 15 章   窗口 TVF、聚合与 changelog
  第 19 章   Regular/Interval/Temporal Join
  第 36 章   对账、回放与数据质量
  本附录     事实/维度建模、SCD、粒度、SQL 模板与反模式
```

## N.1 流式建模与离线建模的五个差异

| 维度 | 离线数仓 | 实时数仓 |
|---|---|---|
| 完整性 | 分区到齐即可计算 | 由 watermark/业务边界定义“到齐” |
| 更新 | 整分区重写 | 持续 changelog/upsert，必须定义主键 |
| 时间 | 以分区/批次时间为主 | 事件时间、处理时间、摄入时间并存 |
| 历史 | 原始数据可长期重放 | 状态有保留期，湖表有快照/compaction |
| 成本 | 存储便宜、算力弹性 | 状态、网络、常驻算力持续计费 |

由此得到三条建模原则：

1. **先定粒度（grain），再定指标**。一行事实代表什么，必须在 DDL 和主键里写清楚。
2. **先定时间语义，再定窗口**。用事件时间算业务事实，用处理时间做运维/新鲜度监控，不要混用。
3. **先定更新协议，再定存储**。append、upsert、retract 三种 changelog 对应不同的 sink 能力和下游查询方式。

## N.2 分层：ODS、DWD、DWS、ADS 与 DIM

实时数仓常用五层。层数不是目的，**每层要有明确的粒度和契约**才是。

| 层 | 典型内容 | 粒度 | 关键约束 |
|---|---|---|---|
| ODS | 原始 CDC/日志/埋点 | 一条源事件/一条变更 | 保留原始字段、offset、摄入时间；只做最少清洗 |
| DWD | 清洗、去重、标准化、打宽 | 一个业务事件/一个业务实体 | 主键稳定、时间语义明确、可追溯 |
| DWS | 按主题/维度聚合 | 用户/商品/门店 × 时间窗口 | 指标可加或可重算，口径有文档 |
| ADS | 面向应用/看板的宽表 | 查询所需粒度 | 低延迟、易查询，允许冗余 |
| DIM | 维度与历史 | 维度实体/版本 | 一致性、版本有效期、跨域共享 |

### N.2.1 每层的最小契约

```text
ODS：source、topic/table、offset、ingest_time、raw payload、schema version
DWD：business key、event_time、dedup key、standardized fields、quality flags
DWS：dimension keys、window_start/window_end、measure、unit、version
ADS：query key、freshness、serving index、owner
DIM：surrogate key、natural key、valid_from/valid_to、is_current、source
```

没有这些字段，后面一定会出现“同一个 GMV 三个团队算出三个数”。

### N.2.2 分层不是越多越好

小团队常见的过度设计是：ODS → DWD → DWM → DWS → DWT → ADS，六层每层一个 Flink 作业。结果是：

- 每层都增加延迟和状态；
- 中间层没人用，却要维护；
- 故障定位要跨五个作业；
- 成本随层数线性上升。

一个更现实的起点是 **ODS → DWD → DWS/ADS** 三层；当出现多个下游共享同一中间口径时，再抽出 DWM/DWS。

## N.3 事实表建模

事实表描述**业务过程**，包含外键、度量、时间戳。流式事实表还要额外回答“更新如何表达”。

### N.3.1 三种事实表

| 类型 | 一行代表 | 流式典型来源 | 更新方式 |
|---|---|---|---|
| 事务事实表 | 一次业务事件 | 订单创建、支付、点击 | append，必要时按事件 ID 去重 |
| 周期快照事实表 | 一个实体在一个周期末的状态 | 库存、余额、账户额度 | upsert/覆盖，按实体 + 周期 |
| 累积快照事实表 | 一个实体从开始到结束的多个里程碑 | 订单履约、工单流转 | 按实体 upsert，里程碑逐步补齐 |

选择错误会直接导致指标不可加。例如把“每日库存快照”当成事务事实表求和，会把库存重复累加；把“订单支付”做成累积快照，又会让每次支付覆盖前一次。

### N.3.2 事件事实 vs 状态事实

```text
事件事实：发生了什么（payment_succeeded、order_created）
状态事实：现在是什么（order_status=PAID、inventory_on_hand=120）
```

两者的流式处理不同：

- 事件事实通常 append，按 `event_id` 去重；
- 状态事实通常 upsert，按实体主键保留最新版本；
- 状态事实若需要历史，应进入 SCD2/版本表或湖表快照，而不是只保留当前值；
- 事件事实若需要“当前状态”，用窗口/去重/累积聚合派生，不要反向覆盖原始事件。

### N.3.3 迟到事实与更正

迟到有三种来源：

1. **事件时间乱序**：记录在 watermark 之后到达；
2. **上游补发/重放**：历史事件重新进入；
3. **业务更正**：金额、状态被人工或下游系统修正。

对应策略：

| 场景 | 策略 | 注意 |
|---|---|---|
| 小范围乱序 | watermark + allowed lateness | 结果可能多次发射，下游要能 upsert |
| 长尾迟到 | 侧输出 + 离线回补/湖表 merge | 不要无限放大状态 |
| 重复事件 | 按业务主键去重，保留版本/offset | 去重状态要设 TTL 或落湖 |
| 业务更正 | 以更正事件/版本号为准，保留审计 | 不要用“最后到达即正确” |

一个常见错误是“迟到就丢弃”。如果业务要求准确，丢弃必须被计数、告警，并能通过回放补齐。第 36 章的对账与回放是这里的配套机制。

### N.3.4 累积快照：订单生命周期

订单是典型的累积快照：创建、支付、发货、签收、退款，多个里程碑写同一行。流上可以用“事件标准化 + 按订单聚合”实现：

```sql
CREATE VIEW dwd_order_lifecycle AS
SELECT
  order_id,
  MIN(CASE WHEN event_type = 'created' THEN event_time END) AS created_at,
  MIN(CASE WHEN event_type = 'paid' THEN event_time END) AS paid_at,
  MIN(CASE WHEN event_type = 'shipped' THEN event_time END) AS shipped_at,
  MIN(CASE WHEN event_type = 'delivered' THEN event_time END) AS delivered_at,
  MAX(event_time) AS last_event_at
FROM ods_order_events
GROUP BY order_id;
```

这个视图的输出是 changelog：每个订单一行，随着里程碑到达不断更新。sink 必须支持 upsert（Paimon/JDBC/Redis 等），并且主键是 `order_id`。

这里的 `MIN` 取的是“最早一次”里程碑。如果业务允许撤销、重做或更正（例如先支付后撤销、发货后拦截），只看 `MIN` 会把已撤销的事件当成有效；此时应保留事件版本/作废标记，或改为“取当前有效事件”的聚合，不能用一个 `MIN` 掩盖状态机。

如果直接写到只支持 append 的 Kafka，需要把 changelog 编码成带 `+I/-U/+U` 的格式，并让下游理解撤回；否则会出现重复计数。

## N.4 维度表建模

维度表描述“谁、什么、哪里、何时”，是分析的上下文。实时维度比离线维度多两个问题：**版本**和**查找延迟**。

### N.4.1 星型、雪花与宽表

| 形态 | 优点 | 代价 | 适用 |
|---|---|---|---|
| 星型 | 事实与维度解耦、口径清晰 | 查询时 join 多 | 中台、跨主题共享 |
| 雪花 | 维度规范化、节省冗余 | join 层数多、流上状态大 | 维度层次稳定、更新频繁 |
| 宽表 | 查询快、下游简单 | 冗余大、更新放大 | ADS、固定看板、实时大屏 |

流式建模的实用策略是：

- **DWD 保持星型/规范化**，保证口径和复用；
- **ADS 做宽表**，把高频查询的维度打平；
- 不要在 DWD 阶段就打成 200 列宽表，否则任何维度更新都会重算大量历史。

### N.4.2 SCD1、SCD2、SCD3

| 类型 | 做法 | 历史 | 流式实现 |
|---|---|---|---|
| SCD1 | 直接覆盖 | 不保留 | 主键 upsert |
| SCD2 | 新增版本行，维护有效期 | 完整 | 版本表 + temporal join / 湖表 merge |
| SCD3 | 保留 previous/current 两列 | 仅一层 | 固定列 upsert |

选择建议：

- **纠错型变更**（地址写错、编码修正）用 SCD1，避免制造无意义历史；
- **业务属性变化**（客户等级、价格档位）且分析需要“当时值”用 SCD2；
- **只关心当前/上一个值**用 SCD3；
- 合规审计要求“任何时点可还原”时，SCD2 + 不可变原始变更流。

SCD2 的核心字段：

```text
natural_key   业务键，如 customer_id
surrogate_key 版本代理键（可选）
valid_from    版本生效时间
valid_to      版本失效时间（当前版本可用 9999-12-31）
is_current    当前版本标记
source_ts     源系统变更时间
```

边界规则要写死：`valid_from <= t < valid_to`。否则同一时刻会匹配到两个版本。

### N.4.3 退化维度、迷你维度与桥接表

- **退化维度（degenerate dimension）**：订单号、支付流水号这类“没有独立维度表、但需要出现在事实表”的键。直接放事实表。
- **迷你维度（mini-dimension）**：把变化频繁、组合多的属性（年龄段、消费档、活跃度）拆成小维度，避免主维度爆炸。
- **桥接表（bridge table）**：多值维度（一个订单多个促销、一个商品多个标签）。流上做桥接会放大行数，必须评估状态和下游去重。
- **角色扮演维度**：同一张日期/门店维度在事实表中扮演“下单时间”“支付时间”“发货时间”等角色，用视图或别名区分。

## N.5 时间语义与版本

### N.5.1 事件时间、处理时间、摄入时间

| 时间 | 含义 | 用途 | 风险 |
|---|---|---|---|
| 事件时间 | 业务实际发生时间 | 窗口、事实、as-of 维度 | 乱序、迟到、时钟错误 |
| 处理时间 | 算子处理记录的本地时间 | 运维监控、lookup、缓存 | 不可重放、受反压影响 |
| 摄入时间 | source 写入/读取时间 | 新鲜度、延迟分析 | 不等于业务发生时间 |

建模规则：

- **事实表的业务时间用事件时间**；金额、风控、结算不要用处理时间。
- **新鲜度/SLO 用摄入时间或处理时间**，不要拿事件时间判断“系统是否延迟”。
- **维表 as-of 用事实的事件时间**；如果业务要“当前最新”，明确用 lookup/processing time。
- 同一张表不要混用两种时间做窗口，否则结果不可解释。

### N.5.2 时区与夏令时

Flink SQL 中常见的两种时间类型：

- `TIMESTAMP(3)`：不带时区的本地时间；
- `TIMESTAMP_LTZ(3)`：带时区语义的时间戳（内部以 epoch 表示）。

落地建议：

1. 源端统一用 UTC epoch 或 `TIMESTAMP_LTZ`；
2. 展示层再转业务时区；
3. 分区字段显式用统一时区计算，例如 `DATE_FORMAT(ts, 'yyyy-MM-dd')` 前先确认会话时区；
4. 对夏令时（DST）的 gap/overlap 做测试，尤其是按本地日期分区的报表；
5. 对账时统一到 UTC，避免“同一天差一小时”。

第 14 章的时间类型小节给出了类型选择和 DST 的具体陷阱；建模时要把“存储时区、计算时区、展示时区”写成文档。

### N.5.3 版本化维表与 temporal join

事件时间版本化维表适合“按事实发生时的维度版本打标”：

```sql
CREATE TABLE dim_customer_current (
  customer_id STRING,
  segment STRING,
  update_time TIMESTAMP(3),
  PRIMARY KEY (customer_id) NOT ENFORCED,
  WATERMARK FOR update_time AS update_time - INTERVAL '5' SECOND
) WITH (
  'connector' = 'kafka',
  'topic' = 'dim_customer',
  'properties.bootstrap.servers' = 'kafka:9092',
  'format' = 'debezium-json'
);

SELECT
  o.order_id,
  o.amount,
  c.segment
FROM dwd_order_detail AS o
LEFT JOIN dim_customer_current FOR SYSTEM_TIME AS OF o.order_time AS c
  ON o.customer_id = c.customer_id;
```

语义是“取 `o.order_time` 时刻已知的最新版本”，不是“取当前最新”。如果支付事件晚于下单、但维表在两者之间变更，结果会不同；这正是 as-of 建模要表达的业务含义。

两种常见错误：

1. **用处理时间做历史事实的维度打标**：回放时结果会变；
2. **把 SCD2 历史表直接当版本表**：版本表通常要求主键唯一标识“当前版本”，历史表一行一个版本，语义不同。生产上常用“当前维表（SCD1/最新）+ 历史表（SCD2，供审计/批量）”双表设计，或在湖仓侧生成当前视图。

## N.6 Flink SQL 建模模板

下面给出一条从 ODS 到 ADS 的最小链路。语法以 Flink 2.3 的 Table/SQL 为主；连接器选项以所用版本为准。模板刻意省略了 Paimon 的 `scan.mode`、Kafka 的启动位置、对象存储凭据等环境相关选项，落地时要补全，否则“看起来是流作业、实际跑成了有界批”。

### N.6.1 ODS：原始订单流

```sql
CREATE TABLE ods_orders (
  order_id STRING,
  user_id STRING,
  amount DECIMAL(18, 2),
  currency STRING,
  order_time TIMESTAMP(3),
  update_time TIMESTAMP(3),
  `offset` BIGINT METADATA FROM 'offset' VIRTUAL,
  ingest_time TIMESTAMP_LTZ(3) METADATA FROM 'timestamp' VIRTUAL,
  WATERMARK FOR order_time AS order_time - INTERVAL '5' SECOND
) WITH (
  'connector' = 'kafka',
  'topic' = 'orders',
  'properties.bootstrap.servers' = 'kafka:9092',
  'properties.group.id' = 'rt-dw-orders',
  'scan.startup.mode' = 'group-offsets',
  'format' = 'json',
  'json.ignore-parse-errors' = 'false'
);
```

要点：

- 原始层保留 `offset`/`ingest_time`，便于回溯和新鲜度分析；
- watermark 建在业务事件时间 `order_time` 上，而不是摄入时间；`update_time` 只用于去重排序；
- 解析失败不要静默跳过；要么失败，要么进死信/侧输出；
- 生产环境应把 schema 版本写入元数据或使用 Schema Registry（第 10 章）。

### N.6.2 DWD：去重与标准化

```sql
CREATE VIEW v_orders_dedup AS
SELECT
  order_id,
  user_id,
  amount,
  currency,
  order_time,
  update_time,
  `offset`,
  ingest_time
FROM (
  SELECT
    *,
    ROW_NUMBER() OVER (
      PARTITION BY order_id
      ORDER BY update_time DESC, `offset` DESC
    ) AS rn
  FROM ods_orders
)
WHERE rn = 1;
```

去重键的选择是业务决策：

- 订单表用 `order_id`；
- 支付表用 `payment_id` 或 `(order_id, attempt_no)`；
- 埋点用 `event_id`；
- 没有稳定业务键时，不要假装去重，应该保留重复并让下游按版本处理。

`ROW_NUMBER` 会维护按 key 的状态；长期运行时必须设置状态 TTL 或把去重下沉到湖表的 primary key merge。

### N.6.3 DWD：事实打宽

支付表通常是 upsert changelog：

```sql
CREATE TABLE dwd_payment (
  order_id STRING,
  payment_id STRING,
  pay_amount DECIMAL(18, 2),
  pay_time TIMESTAMP(3),
  update_time TIMESTAMP(3),
  PRIMARY KEY (order_id) NOT ENFORCED,
  WATERMARK FOR update_time AS update_time - INTERVAL '5' SECOND
) WITH (
  'connector' = 'kafka',
  'topic' = 'payments',
  'properties.bootstrap.servers' = 'kafka:9092',
  'properties.group.id' = 'rt-dw-payments',
  'scan.startup.mode' = 'group-offsets',
  'format' = 'debezium-json'
);
```

按事件时间做 as-of 打宽。这里有一个容易踩的细节：`ROW_NUMBER` 去重会改变时间属性的传播，因此 **temporal join 要在原始 ODS 上做，再对 join 结果去重**，而不是先 `v_orders_dedup` 再 join：

```sql
CREATE VIEW v_order_payment AS
SELECT
  order_id,
  user_id,
  amount,
  currency,
  order_time,
  payment_id,
  pay_amount,
  pay_time
FROM (
  SELECT
    o.order_id,
    o.user_id,
    o.amount,
    o.currency,
    o.order_time,
    p.payment_id,
    p.pay_amount,
    p.pay_time,
    ROW_NUMBER() OVER (
      PARTITION BY o.order_id
      ORDER BY o.update_time DESC, o.`offset` DESC
    ) AS rn
  FROM ods_orders AS o
  LEFT JOIN dwd_payment FOR SYSTEM_TIME AS OF o.order_time AS p
    ON o.order_id = p.order_id
)
WHERE rn = 1;
```

去重后的视图同样可能丢失时间属性。生产上通常把 DWD 落成一张带 watermark 声明的表，再从表里读：

```sql
CREATE TABLE dwd_order_detail (
  order_id STRING,
  user_id STRING,
  amount DECIMAL(18, 2),
  currency STRING,
  order_time TIMESTAMP(3),
  payment_id STRING,
  pay_amount DECIMAL(18, 2),
  pay_time TIMESTAMP(3),
  PRIMARY KEY (order_id) NOT ENFORCED,
  WATERMARK FOR order_time AS order_time - INTERVAL '5' SECOND
) WITH (
  'connector' = 'paimon',
  'path' = 's3://warehouse/rt/dwd_order_detail'
);

INSERT INTO dwd_order_detail
SELECT * FROM v_order_payment;
```

注意：

- 左表 `o.order_time` 必须是水位线支撑的事件时间属性；`v_orders_dedup` 经过 `ROW_NUMBER` 后不保证仍是 time attribute；
- `dwd_order_detail` 在 DDL 中重新声明 `WATERMARK FOR order_time`，读回时才恢复事件时间属性；如果连接器不持久化 watermark，读取侧 DDL 必须显式声明；
- `dwd_payment` 的版本时间由它的水位线列 `update_time` 决定，`AS OF o.order_time` 比较的是“事实时间”与“版本时间”；
- 若支付发生在下单之后，as-of `order_time` 可能读到 null；这是“下单时点已知的支付状态”，不是“最终支付结果”；
- 要“最终支付结果”，应该用累积快照/regular join + TTL，或等支付流到达后再更新订单宽表；
- 不要把两种语义混在一个视图里，否则指标口径会漂移。

`v_orders_dedup` 仍可用于不依赖时间属性的普通查询或写入；关键是不要在它上面直接做 event-time temporal join。

### N.6.4 DWS：窗口聚合

```sql
CREATE TABLE dws_user_sales_1m (
  user_id STRING,
  window_start TIMESTAMP(3),
  window_end TIMESTAMP(3),
  order_cnt BIGINT,
  gmv DECIMAL(18, 2),
  PRIMARY KEY (user_id, window_start) NOT ENFORCED
) WITH (
  'connector' = 'paimon',
  'path' = 's3://warehouse/rt/dws_user_sales_1m'
);

INSERT INTO dws_user_sales_1m
SELECT
  user_id,
  window_start,
  window_end,
  COUNT(*) AS order_cnt,
  SUM(amount) AS gmv
FROM TABLE(
  TUMBLE(TABLE dwd_order_detail, DESCRIPTOR(order_time), INTERVAL '1' MINUTE)
)
GROUP BY user_id, window_start, window_end;
```

要点：

- 窗口聚合的输出是 changelog；Paimon 主键表能吸收更新；
- `COUNT(*)` 与 `SUM(amount)` 对重复敏感，前置去重必须可靠；
- `window_start` 是主键的一部分，保证同一用户不同窗口不互相覆盖；
- 这里统计的是按 `order_time` 归属的**下单金额**；如果业务定义 GMV 为支付成功金额，应以支付事实表的 `pay_time` 建窗口，并过滤未支付/退款，不能直接复用本视图；
- 若窗口允许迟到，下游要接受同一窗口的多次更新。

### N.6.5 ADS：可加指标的汇总

```sql
CREATE VIEW ads_gmv_realtime AS
SELECT
  window_start,
  SUM(order_cnt) AS order_cnt,
  SUM(gmv) AS gmv
FROM dws_user_sales_1m
GROUP BY window_start;
```

这里只汇总**可加指标**。如果 ADS 需要 UV：

```sql
COUNT(DISTINCT user_id)
```

不能简单把各 DWS 窗口的 UV 相加；跨窗口去重需要：

- 在更粗粒度重新聚合（保留 user_id）；
- 使用 HyperLogLog/Theta Sketch 等可合并结构；
- 或在 OLAP/湖仓侧用明细去重。

这是实时数仓最常见的口径错误之一。

### N.6.6 维表：当前 + 历史双表

```sql
-- 当前维表：用于 as-of / lookup，主键唯一
CREATE TABLE dim_customer_current (
  customer_id STRING,
  segment STRING,
  city STRING,
  update_time TIMESTAMP(3),
  PRIMARY KEY (customer_id) NOT ENFORCED,
  WATERMARK FOR update_time AS update_time - INTERVAL '5' SECOND
) WITH (
  'connector' = 'kafka',
  'topic' = 'dim_customer',
  'properties.bootstrap.servers' = 'kafka:9092',
  'format' = 'debezium-json'
);

-- 历史维表：用于审计/批量回溯，按 (customer_id, valid_from) 保留版本
CREATE TABLE dim_customer_history (
  customer_id STRING,
  segment STRING,
  city STRING,
  valid_from TIMESTAMP(3),
  valid_to TIMESTAMP(3),
  is_current BOOLEAN,
  PRIMARY KEY (customer_id, valid_from) NOT ENFORCED
) WITH (
  'connector' = 'paimon',
  'path' = 's3://warehouse/rt/dim_customer_history'
);
```

当前表用于低延迟 as-of/lookup；历史表用于“某时刻客户属于哪个等级”的审计与离线重算。两者由同一份 CDC 派生，必须有对账，避免“当前表更新了、历史表没写”。

## N.7 宽表、星型与湖仓：怎么选

| 需求 | 推荐形态 | 原因 |
|---|---|---|
| 固定看板、实时大屏 | ADS 宽表 | 查询简单、延迟低 |
| 多主题共享维度 | DWD 星型 + DIM | 口径统一、复用 |
| 即席分析、跨主题 | 湖仓明细 + 星型 | 灵活、可回溯 |
| 高并发点查 | ADS + OLAP/KV | 避免查询时 join |
| 合规审计 | 原始 changelog + SCD2 | 可还原历史 |
| 快速试点 | ODS → ADS 宽表 | 层数少、上线快 |

三条经验：

1. **宽表是服务层产物，不是唯一真相**。真相应在 DWD/湖仓明细中。
2. **维度更新频繁时，不要把维度打平进历史事实**。否则一次维表变更会触发大规模重写。
3. **湖仓不是“更大的数据库”**。它的优势是开放格式、快照、增量读和批流共用；查询性能仍要靠分区、bucket、compaction 和索引设计。

## N.8 状态、保留与成本

实时建模的每个选择都会变成状态或存储：

| 建模选择 | 状态/存储影响 | 控制手段 |
|---|---|---|
| Regular Join | 两侧历史全保留 | `table.exec.state.ttl`、拆链路、改 temporal/interval |
| 去重 `ROW_NUMBER` | 按 key 保留排名状态 | TTL、湖表主键合并、限制 key 基数 |
| 窗口聚合 | 窗口 bucket + 迟到缓冲 | 窗口大小、allowed lateness、清理策略 |
| 维表 lookup | 缓存条目 | 缓存 TTL/大小、外部 KV |
| SCD2 | 版本行持续增长 | 分区、compaction、冷热分层 |
| ADS 宽表 | 冗余存储 | 只保留服务所需列/时间范围 |

容量评估至少要回答：

- 状态里保留多少个 key？每个 key 多少字节？
- 最坏情况下的乱序/迟到会把状态放大多少？
- checkpoint 间隔与状态大小的乘积是否超过存储带宽？
- 湖表每天产生多少小文件？compaction 能否在窗口内完成？
- 保留 7 天、30 天、1 年分别需要多少存储和多少回放时间？

第 26 章给出的容量工作表可以直接用于这里；附录 J 给出压测方法。

## N.9 数据质量与对账

建模再漂亮，没有对账就不知道对不对。实时数仓至少要对五类不变量：

| 不变量 | 检查 | 典型 SQL/指标 |
|---|---|---|
| 唯一性 | 业务主键是否重复 | `COUNT(*)` vs `COUNT(DISTINCT key)` |
| 完整性 | 关键字段是否为空 | `SUM(CASE WHEN k IS NULL THEN 1 ELSE 0 END)` |
| 及时性 | 数据新鲜度 | `NOW() - MAX(ingest_time)` |
| 一致性 | 层间汇总是否相等 | DWD 金额和 = DWS 金额和 |
| 有效性 | 金额/状态是否合法 | `amount < 0`、非法枚举 |

一个跨层对账例子：

```sql
SELECT 'dwd' AS layer, COUNT(*) AS cnt, SUM(amount) AS amt
FROM dwd_order_detail
WHERE order_time >= TIMESTAMP '2026-11-01 00:00:00'
UNION ALL
SELECT 'dws', SUM(order_cnt), SUM(gmv)
FROM dws_user_sales_1m
WHERE window_start >= TIMESTAMP '2026-11-01 00:00:00';
```

实际落地要注意：

- 两边的时间边界要一致（事件时间 vs 窗口时间）；
- 去重口径要一致（DWD 已去重，DWS 不能再按原始流算）；
- 金额单位/币种要统一；
- 迟到数据会让 T+0 对账不等于 T+1 对账，必须定义“结算时点”；
- 对账差异要能下钻到 key，而不是只报一个总数。

第 36 章给出了更完整的对账、回放、影子链路和事故复盘方法。

## N.10 案例：订单实时数仓

### N.10.1 业务问题

- 实时看板：分钟级 GMV、订单数、支付转化率；
- 运营分析：按用户/商品/门店的 1 分钟、1 小时销售；
- 风控：订单生命周期中的异常停留；
- 审计：任意时点的客户等级、价格档位。

### N.10.2 分层设计

```text
ODS
  ods_orders        订单创建/变更
  ods_payments      支付/退款
  ods_shipments     发货/签收
  ods_customers     CDC 客户资料

DWD
  dwd_order_detail       订单 + 支付 as-of/最终态（明确语义）
  dwd_order_lifecycle    订单累积快照
  dim_customer_current   当前客户维表
  dim_customer_history   SCD2 历史

DWS
  dws_user_sales_1m
  dws_product_sales_1h
  dws_store_sales_1h

ADS
  ads_gmv_realtime
  ads_conversion_funnel
  ads_risk_order_monitor
```

### N.10.3 关键决策

1. **GMV 用事件时间**，按支付成功时间归属；退款用负值或独立指标，不直接改历史 GMV。
2. **订单宽表区分“下单时点支付状态”和“最终支付状态”**，分别服务不同分析。
3. **客户等级用 as-of temporal join**，保证历史报表可复现；当前看板用当前维表。
4. **去重键写入 DWD 契约**，DWS 不再猜测。
5. **ADS 只放可加指标**；UV/转化率回到明细或使用可合并 sketches。
6. **所有层记录口径版本**，口径变更走新字段/新表，不静默改历史。

### N.10.4 SLO 与新鲜度

| 指标 | 目标 | 度量 |
|---|---|---|
| ODS 摄入延迟 | P99 < 5s | `ingest_time - source_ts` |
| DWD 可见延迟 | P99 < 30s | `now - order_time` |
| ADS 刷新延迟 | P99 < 60s | 物化表/湖表提交时间 |
| 数据完整率 | > 99.9% | 对账差异/总事件数 |
| 重复率 | < 0.01% | 主键重复计数 |

SLO 要按业务定义，不要所有表都写“1 秒”。风控看板和大屏的实时性要求不同，成本也应不同。

## N.11 反模式

| 反模式 | 后果 | 修正 |
|---|---|---|
| 不定义粒度，先写 SQL | 指标重复/不可加 | 先写“一行代表什么” |
| 所有层都用 append | 更新无法表达，下游重复 | 明确 upsert/changelog |
| 用处理时间算财务事实 | 回放结果不一致 | 事件时间 + watermark |
| 无界 Regular Join | 状态无限增长 | TTL、temporal/interval、拆链路 |
| 维表 lookup 无缓存 | 外部系统被打爆 | 本地/部分缓存、限流 |
| `COUNT(DISTINCT)` 跨窗口相加 | UV 虚高 | 明细去重或 sketch |
| 时区混用 | 日切/小时窗错位 | 统一 UTC 存储、展示层转换 |
| 不去重 | GMV/订单数虚高 | 业务主键 + 版本 |
| 状态 TTL 太短 | 迟到/关联丢失 | 按业务乱序窗口设定 |
| 状态 TTL 太长 | checkpoint/恢复慢 | 冷热分层、湖表承接 |
| 过度分层 | 延迟和成本翻倍 | 从三层开始，按需增加 |
| 没有对账 | 错了不知道 | 唯一性/完整性/一致性检查 |

## N.12 检查清单

- [ ] 每个事实表都写清楚 grain（一行代表什么）和主键；
- [ ] 事实时间用事件时间，运维新鲜度用摄入/处理时间；
- [ ] ODS 保留 offset、摄入时间、schema 版本和原始 payload；
- [ ] DWD 的去重键、标准化规则、质量标记有文档；
- [ ] 维度明确 SCD 类型、有效期边界和当前/历史表分工；
- [ ] as-of 与 latest 的语义在表名/字段名/文档中可区分；
- [ ] DWS 指标可加性或可重算性经过验证；
- [ ] UV/转化率等非可加指标没有跨窗口硬加；
- [ ] Regular Join、去重、窗口状态有 TTL/清理策略；
- [ ] 湖表分区、bucket、compaction 与小文件治理有监控；
- [ ] 层间对账覆盖行数、金额、主键唯一性和新鲜度；
- [ ] 时区、币种、单位、口径版本统一；
- [ ] ADS 宽表只服务查询，不作为唯一真相；
- [ ] 口径变更走版本化发布，不静默覆盖历史。

## N.13 小结

实时数仓的难点不在 SQL 语法，而在**在持续变化的数据上维持稳定的业务语义**：

- 粒度决定指标能不能加；
- 时间语义决定结果能不能复现；
- 更新协议决定下游看到的是事实还是重复；
- 维度版本决定历史报表是否可信；
- 状态与保留策略决定成本与可恢复性；
- 对账决定你是在“实时”还是在“实时地算错”。

把本附录的模型与[第 13 章](../chapters/13-cdc-lakehouse.md)的 CDC/湖仓、[第 14 章](../chapters/14-table-sql.md)的动态表、[第 15 章](../chapters/15-window-tvf.md)的窗口聚合、[第 19 章](../chapters/19-dual-stream-join.md)的 Join 语义、[第 36 章](../chapters/36-correctness-reconciliation.md)的对账方法结合使用，才能形成一条既实时又可解释、可回滚、可审计的数据链路。

事实/维度建模的经典参考是 Kimball 的 *The Data Warehouse Toolkit*；流式场景下的新鲜度、迟到与状态保留，则要结合本书第 4、7、8、15 章和[附录 J](J-performance-engineering.md)一起判断。
