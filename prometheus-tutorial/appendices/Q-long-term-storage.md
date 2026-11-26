# 附录 Q 长期存储架构：Thanos、Mimir 与 VictoriaMetrics

第 18 章讲了 remote write、remote read 和联邦的协议；本附录进入架构层：当单机 Prometheus 的 15 天 retention 不够、当多个集群需要全局查询、当团队/租户需要隔离时，Thanos、Mimir、VictoriaMetrics 各自怎么工作、在哪里失败、成本从哪里来。三者都能"存很久、查很大"，但设计哲学完全不同，选型不能只看功能表。

## Q.1 问题定义：单机 Prometheus 缺什么

单机 Prometheus 的边界：

- 存储只在本地磁盘，retention 以天/周计；
- 查询只覆盖本地数据；
- 没有跨副本/跨集群的全局视图；
- 没有租户隔离和配额；
- 扩容只能垂直或功能分片；
- 故障恢复依赖本地快照。

长期存储要补齐的能力：

| 能力 | 说明 | 难点 |
|---|---|---|
| 无限保留 | 对象存储/冷热分层 | 索引与查询延迟 |
| 全局查询 | 多集群/多副本合并 | 去重、fan-out、部分失败 |
| 高可用 | 写入/查询无单点 | 复制、ring、一致性 |
| 多租户 | 租户隔离/配额 | 认证、限流、数据布局 |
| 降采样 | 长期数据降精度 | 查询语义/精度声明 |
| 成本可控 | 对象存储 + 缓存 | 索引缓存/出口流量 |
| 兼容性 | PromQL/remote write | 语义差异/函数覆盖 |

## Q.2 三种架构哲学

```text
Thanos:          Prometheus 本地 TSDB + sidecar 上传 block → 对象存储 → Store/Query 查询
Mimir:           remote write → distributor → ingester → 对象存储 blocks → querier/store-gateway
VictoriaMetrics: remote write/agent → vminsert → vmstorage（自有存储）→ vmselect 查询
```

- **Thanos**：尽量复用 Prometheus 的 block 格式和本地 TSDB，sidecar 把 block 上传对象存储；查询层 fan-out 到 sidecar/store/receive。适合"已有 Prometheus，想加长期存储和全局视图"。
- **Mimir**：完整的水平扩展指标后端，写入走 remote write，读走 querier/store-gateway；多租户和限额是一等公民。适合"平台化、多租户、大规模"。
- **VictoriaMetrics**：自有存储引擎 + 高压缩，单机/集群两种形态；组件少、成本低、PromQL 兼容（MetricsQL 超集）。适合"成本敏感、想少运维"。

三者都不是"Prometheus 的集群版"；它们是独立系统，Prometheus 仍然是采集/规则/告警的边缘层（或 agent）。

## Q.3 Thanos：对象存储上的全局视图

### 组件

| 组件 | 作用 |
|---|---|
| Sidecar | 与 Prometheus 同 Pod，上传 2h block，暴露 StoreAPI |
| Receiver | 接收 remote write，写入本地 TSDB 并上传对象存储 |
| Querier | 实现 Prometheus API，fan-out 到 Sidecar/Store/Receiver，去重 |
| Store Gateway | 从对象存储读取历史 block，提供 StoreAPI |
| Compactor | 压缩/降采样/retention，单例运行 |
| Ruler | 长期规则求值（可选） |
| Bucket Web | 查看对象存储 bucket（可选） |

### 写入路径（Sidecar 模式）

```text
Prometheus 本地 TSDB
  → 每 2h 生成 block
  → Sidecar 发现新 block，上传到对象存储
  → Sidecar 通过 StoreAPI 暴露本地近期数据
```

要点：

- Sidecar 只上传**不可变 block**，不上传 head；
- 上传后本地 block 可以按本地 retention 删除，对象存储保留长期；
- Sidecar 与 Prometheus 共享 Pod/网络，延迟低；
- 如果 Prometheus 启用本地 compaction，Sidecar 上传的 block 可能被本地重写；Thanos 通常建议关闭本地 compaction，让 Compactor 统一处理（或接受重复上传/覆盖）；
- 对象存储需要版本/生命周期策略，避免误删。

### 写入路径（Receive 模式）

```text
Prometheus remote write → Thanos Receiver（hashring）
  → 本地 TSDB/WAL → 上传对象存储
```

适合无法用 sidecar 的场景（跨集群、跨网络、多租户软隔离）。Receiver 支持 hashring 和复制因子；查询层把 Receiver 当作 StoreAPI 数据源。

### 读取路径

```text
Querier
  ├─ Sidecar（近期本地数据）
  ├─ Store Gateway（对象存储历史 block）
  └─ Receiver（remote write 写入的数据）
        ↓
  合并 + 去重 + PromQL 求值
```

`Querier` 对每个 StoreAPI 发查询，合并结果；支持部分响应（`--query.partial-response`），某个 store 失败时返回部分数据并带 warning。

### 去重

HA Prometheus 对会写相同序列（external labels 除 `replica` 外一致）。Thanos 去重：

```bash
thanos query \
  --query.replica-label=replica \
  --query.replica-label=prometheus_replica
```

前提：

- 两个副本的序列标签完全一致（除 replica）；
- `replica` 标签值不同；
- 查询层配置了正确的 replica label。

如果 external labels 不一致，Thanos 会把它们视为不同序列，无法去重，查询出现双倍/抖动。

### 降采样与保留

Compactor 的两级降采样：

```text
raw（原始分辨率）
  → 超过 40h：5m 分辨率
  → 5m 超过 10d：1h 分辨率
```

保留策略：

```bash
thanos compact \
  --retention.resolution-raw=30d \
  --retention.resolution-5m=180d \
  --retention.resolution-1h=2y
```

要点：

- Compactor 必须是**单例**（同一 bucket 同时只有一个 compactor），否则会竞争/损坏；
- 降采样是不可逆的；查询时 Thanos 会选择合适分辨率；
- 删除/保留以 block 为单位，实际保留时间可能略长；
- 对象存储生命周期策略要和 Thanos retention 对齐，避免"Thanos 还没删，bucket 先删"。

### 多租户

Thanos 原生不是多租户系统。Receiver 可以通过 tenant header/label 做软隔离，但配额、查询隔离、每租户 retention 不如 Mimir。多租户需求强时，优先 Mimir 或每租户独立 Thanos。

### 常见故障

| 故障 | 表现 | 排查 |
|---|---|---|
| Sidecar 上传失败 | 对象存储无新 block | Sidecar 日志、bucket 权限、网络 |
| Compactor 积压 | bucket 小 block 多、查询慢 | compaction 进度、磁盘/CPU、单例冲突 |
| Store Gateway OOM | 查询大时间范围失败 | index cache、block 大小、并发 |
| Querier 部分失败 | 结果缺失 + warning | store 健康、网络、超时 |
| 去重失效 | 双倍序列/抖动 | replica label、external labels |
| 对象存储延迟 | 查询慢/超时 | bucket 限流、缓存、区域 |

## Q.4 Mimir：多租户水平扩展后端

### 组件

| 组件 | 作用 |
|---|---|
| Distributor | 接收 remote write，校验租户限额，HA 去重，按 ring 分片到 ingester |
| Ingester | 内存 TSDB + WAL，定期 flush block 到对象存储 |
| Querier | 执行 PromQL，fan-out 到 ingester/store-gateway，合并 |
| Query Frontend | 查询拆分/缓存/队列（可选） |
| Query Scheduler | 查询队列/公平调度（可选） |
| Store Gateway | 从对象存储读取历史 block |
| Compactor | 压缩/去重/retention（按租户） |
| Ruler | 规则求值（可选） |
| Alertmanager | 告警路由（可选） |

Mimir 是"modular monolith"：所有组件编译进一个二进制，用 `-target` 选择运行哪些组件；可以单体、读写分离或微服务部署。

### 写入路径

```text
remote write（X-Scope-OrgID: tenant）
  → Distributor
      - 认证/租户校验
      - 限额（samples/s、series、metadata）
      - HA 去重（cluster/replica label）
      - ring hash 到 ingester
  → Ingester（内存 TSDB + WAL）
      - 定期 flush block 到对象存储
  → Compactor（后台）
```

要点：

- 多租户通过 `X-Scope-OrgID` 头；开启多租户后，缺头直接拒绝；
- 生产通常在认证代理/网关层注入/校验租户头，避免客户端伪造；
- Distributor 做 HA 去重：同一租户/序列/时间戳的副本只保留一个；
- ingester 有 replication factor；ring 负责故障转移；
- 限额是租户级，超限返回 4xx/429，客户端要重试/降载。

### 读取路径

```text
Querier
  ├─ Ingester（近期数据）
  └─ Store Gateway（对象存储 blocks）
        ↓
  合并 + PromQL
```

Query Frontend 可以做：

- 查询拆分（按时间/分片）；
- 结果缓存；
- 队列/公平调度；
- 慢查询隔离。

### 多租户与限额

Mimir 的租户模型：

- 每个请求带 `X-Scope-OrgID`；
- 每租户独立的 limits（摄入速率、series 数、查询并发、查询长度、retention）；
- 查询只能看到本租户数据；
- compactor 按租户 shard 处理；
- 认证/授权通常由外部网关完成。

这比"每租户一个 Prometheus"省资源，但要求平台团队维护租户生命周期、配额和账单。

### 架构演进：classic 与 ingest storage

Mimir 3.0 起提供基于 Kafka 的 **ingest storage** 架构，把写入路径与 ingester 解耦；经典架构（ingester 本地 WAL + ring）仍然广泛部署。选型/升级时要确认：

- 当前版本默认/推荐哪种架构；
- Kafka/对象存储的运维成本；
- 读写路径的延迟/一致性；
- 回滚/迁移路径。

不要把某一版本的默认架构当成所有版本的通用事实。

### 常见故障

| 故障 | 表现 | 排查 |
|---|---|---|
| 租户超限 | 429/4xx，数据缺口 | limits 配置、租户用量、客户端重试 |
| Ingester OOM | 写入失败/重启 | series 基数、flush 延迟、内存限额 |
| Ring 不稳定 | 写入失败/重复 | ring 状态、网络、副本数 |
| Store Gateway 慢 | 历史查询超时 | index cache、block 布局、并发 |
| Compactor 积压 | 对象存储 block 多、retention 不生效 | compactor 副本/租户 shard、磁盘 |
| 查询 fan-out 过大 | 查询慢/OOM | query frontend 拆分、限制时间范围、shard |

## Q.5 VictoriaMetrics：高压缩、少组件

### 组件

| 组件 | 作用 |
|---|---|
| vmstorage | 存储数据，按时间范围/标签返回；节点间不通信 |
| vminsert | 接收写入（remote write/Influx/OTLP 等），consistent hash 分片 |
| vmselect | 查询，fan-out 到 vmstorage，合并/聚合/缓存 |
| vmagent | 轻量采集/转发，磁盘缓冲、relabel、stream aggregation |
| vmalert | 规则/告警 |
| vmauth | 认证/路由/负载均衡 |
| vmctl | 迁移工具 |

单机版把 vminsert/vmselect/vmstorage 合在一个进程，适合中小规模；集群版水平扩展。

### 写入/读取

```text
vmagent / Prometheus remote write
  → vminsert（hash by metric name + labels）
  → vmstorage（本地 parts，定期 merge）
  → vmselect（fan-out + merge）
```

要点：

- vmstorage 节点不互相通信，靠 vminsert 的 consistent hashing 分片；
- 节点故障/扩缩容会触发数据重路由；副本因子提高 durability；
- `-dedup.minScrapeInterval` 控制去重窗口；集群模式下 vmselect/vmstorage 要一致；
- `-retentionPeriod` 按 vmstorage 配置；过期数据自动删除；
- 降采样是企业版能力；开源版靠 retention/聚合 recording rules；
- MetricsQL 是 PromQL 的超集，函数更多，但跨后端迁移要验证差异。

### 多租户

集群版通过 URL 中的 `accountID/projectID` 做多租户：

```text
/insert/<accountID>/prometheus/api/v1/write
/select/<accountID>/prometheus/api/v1/query
```

配额/隔离通常由 vmauth/网关和资源配额实现；不如 Mimir 的租户 limits 细，但组件更少。

### 常见故障

| 故障 | 表现 | 排查 |
|---|---|---|
| vmstorage 磁盘满 | 写入只读/失败 | retention、磁盘、`-storage.minFreeDiskSpaceBytes` |
| 重路由抖动 | 写入延迟/重复 | vminsert 日志、节点负载、`-disableRerouting` |
| 去重配置不一致 | 双倍计数/rate 抖动 | `-dedup.minScrapeInterval` 全链路一致 |
| vmselect OOM | 大查询失败 | 查询并发、时间范围、`-search.max*` 限制 |
| vmagent 缓冲满 | 数据丢失 | 磁盘、remote write 队列、重试 |

## Q.6 对比矩阵

| 维度 | Thanos | Mimir | VictoriaMetrics |
|---|---|---|---|
| 写入方式 | Sidecar 上传 block / Receive remote write | remote write | remote write / vmagent |
| 存储 | 对象存储 + 本地近期 | 对象存储 + ingester | 自有存储（本地盘；对象存储用于备份） |
| 查询层 | Querier fan-out | Querier + Frontend | vmselect fan-out |
| 多租户 | 弱（Receiver 软隔离） | 强（租户/限额） | 中（accountID + 网关） |
| 降采样 | 内置（5m/1h） | 支持（按租户/规则） | 企业版 |
| 运维复杂度 | 中高（组件多） | 高（微服务/ring/限额） | 中（组件少） |
| 成本 | 对象存储便宜，查询/缓存另算 | 对象存储 + 计算，规模大 | 压缩率高，通常成本低 |
| PromQL 兼容 | 高（Prometheus API） | 高 | 高（MetricsQL 超集，注意差异） |
| 适合 | 已有 Prometheus、多集群 | 多租户平台、大规模 | 成本敏感、少运维 |

没有"绝对更好"：Thanos 的组件多但复用 Prometheus block；Mimir 的多租户强但运维重；VictoriaMetrics 简单高效但生态/语义差异要验证。

## Q.7 容量与成本模型

长期存储的成本不只是"每 GB 对象存储"：

```text
总成本 =
  对象存储（保留数据 + 版本/副本）
  + 索引缓存（Store Gateway/Querier 的 SSD/内存）
  + 查询计算（Querier/vmselect 的 CPU/内存）
  + 写入计算（Distributor/Ingester/vminsert）
  + 网络（跨 AZ/跨区域/出口）
  + 运维人力
```

容量估算：

```text
每日新增样本 = 活跃序列 × 采样率 × 86400
原始字节 ≈ 每日样本 × 每样本字节 × 保留天数
索引/缓存 ≈ 原始字节的 5%～20%（取决于基数/查询）
查询节点 ≈ 并发查询数 × 每查询内存
```

注意：

- 高基数会放大索引和查询内存，不只是原始字节；
- 对象存储的 GET 请求数/延迟会影响查询成本；
- 跨区域查询的出口流量可能比存储更贵；
- 降采样能显著降低长期查询成本，但会损失精度；
- HA 副本/复制因子会让写入成本翻倍。

## Q.8 数据布局、保留与删除

### Thanos

```text
bucket/
  <tenant>/               # 如果启用租户
    01J.../               # block ULID
      meta.json
      index
      chunks/
    01J.../
  thanos.shipper.json
```

- 保留由 Compactor 按分辨率执行；
- 删除通常通过 retention 或对象存储生命周期；
- 手工删除 block 前要确认没有查询/compactor 在使用；
- 删除是不可逆的，先备份 meta/索引。

### Mimir

- 每租户独立的 block 前缀；
- Compactor 按租户 shard 执行 retention；
- 删除租户数据要通过 API/运维流程，不能直接删 bucket 前缀（会破坏索引）；
- 合规删除要确认备份/缓存/索引都已清理。

### VictoriaMetrics

- 数据按 time series/part 存储，retention 由 vmstorage 自动删除；
- 手工删数据要用官方 API/工具，不要直接删目录；
- 集群扩缩容会触发数据重分布，提前规划磁盘/网络。

## Q.9 迁移路径

### 从单机 Prometheus 到长期存储

1. **先 remote write 双写**：保留本地 TSDB，新增远端；
2. **验证写入**：对比两边的序列数、样本数、查询结果；
3. **切查询**：Grafana 数据源切到全局查询层；本地保留只读；
4. **调 retention**：本地缩短，远端延长；
5. **切规则/告警**：确认规则查询的是全局层还是本地；
6. **回滚**：保留本地数据和旧数据源，至少一个 retention 周期。

### 历史数据回填

- Thanos：把旧 Prometheus 的 block 上传到 bucket（需元数据/ULID 兼容）；
- Mimir：用 `mimirtool`/remote write 回填，注意乱序/重复/租户；
- VictoriaMetrics：用 `vmctl` 导入，注意时间戳/标签/去重；
- 回填前评估价值：历史原始数据很贵，通常只保留聚合/关键指标。

### 兼容性验证

- PromQL 函数/语义差异（尤其实验函数、native histogram）；
- remote write 版本/压缩/元数据；
- 告警规则查询路径；
- Grafana 数据源/变量/exemplar；
- 多租户 header/认证；
- 删除/retention 行为。

## Q.10 常见失败模式

1. **Compactor 单例冲突**：两个 compactor 同时写同一 bucket，block 损坏/重复。
2. **Store Gateway 索引缓存太小**：历史查询反复读对象存储，延迟爆炸。
3. **Querier fan-out 过多**：一次查询打到几十个 store，尾延迟不可控。
4. **去重标签不一致**：HA 对写出双份数据，rate/分位数抖动。
5. **租户头缺失/伪造**：写入失败或数据串租户。
6. **对象存储限流**：上传/查询被 429，Sidecar/Compactor 积压。
7. **retention 与 bucket 生命周期冲突**：数据提前消失或永远不删。
8. **降采样后精度误解**：用 1h 分辨率数据判断 5m 级 SLO。
9. **跨区域查询**：出口流量/延迟超预算。
10. **升级不兼容**：block 格式/索引版本/协议变化，回滚失败。

## Q.11 选型决策树

```text
已有多个 Prometheus 集群，想加长期存储和全局查询？
  是 → 已有对象存储、团队能维护多组件 → Thanos
       需要强多租户/配额/平台化 → Mimir
  否 → 单集群/中小规模
       成本敏感、想少运维 → VictoriaMetrics 单机/集群
       已经在云上、想托管 → 云厂商 Managed Prometheus
       只需要短期 → 单机 Prometheus + 快照
```

补充维度：

- 是否需要每租户 retention/限额；
- 是否需要降采样；
- 是否有对象存储/ Kafka 运维能力；
- 查询延迟/并发要求；
- 现有 Grafana/告警/规则迁移成本；
- 退出/数据导出要求。

## Q.12 与 Mini-Prometheus 的关系

Mini 的 `remote_client.py` 演示了 remote write 的队列/批量/重试，`remote.py` 演示了 protobuf/Snappy 编解码；它足以理解"边缘 Prometheus 如何把样本送到中心"，但**不是长期存储**：

- 没有对象存储/block 上传；
- 没有全局查询/去重；
- 没有多租户/限额；
- 没有降采样/retention 分层；
- 没有 compactor/索引缓存。

把 Mini 当作"remote write 发送端"的教学模型，把本章的三个系统当作生产选型；不要用 Mini 的 JSON remote read 推断生产后端的协议/性能。

## 本章小结

- Thanos 复用 Prometheus block + 对象存储，组件多但迁移自然；Compactor 单例、降采样、去重是关键。
- Mimir 是多租户水平扩展后端，Distributor/Ingester/Querier/Store Gateway/Compactor 分工明确；租户头和限额是核心。
- VictoriaMetrics 组件少、压缩率高、成本低；vminsert/vmstorage/vmselect 的 hash/去重/retention 要配置一致。
- 成本由存储、索引缓存、查询计算、网络、人力共同决定，不只看每 GB 单价。
- 迁移先双写/双跑，再切查询/规则；历史回填要评估价值与兼容性。
- 选型从规模、多租户、降采样、运维能力、退出成本出发，没有单一赢家。

## 延伸阅读

- Thanos 官方文档：Sidecar、Receive、Querier、Compactor、Downsampling。
- Grafana Mimir 官方文档：Architecture、Multi-tenancy、Limits、Compactor。
- VictoriaMetrics 官方文档：Cluster、vmagent、vmalert、Retention/Dedup。
- 本教程第 18 章（remote write/read/federation）、第 19 章（HA/分片）、第 55 章（选型）。
