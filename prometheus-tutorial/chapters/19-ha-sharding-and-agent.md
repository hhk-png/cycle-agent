# 第 19 章 高可用、分片与 Agent 模式

## 19.1 Prometheus 没有集群模式

Prometheus 单机不做：

- 数据分片；
- 副本复制；
- 一致性协议；
- 自动故障转移。

它通过"外部组合"实现高可用：

```text
Prometheus A ─┐
              ├─► 远端存储（去重）─► 查询层
Prometheus B ─┘
```

两个副本抓同样的目标，写同样的远端；查询层按 replica label 去重。

## 19.2 HA 对

部署两个（或更多）完全相同的 Prometheus：

- 相同配置；
- 除 `replica` 外相同的 external labels；
- 不同的 `replica` 标签（用于远端去重）；
- 抓相同目标。

```yaml
global:
  external_labels:
    cluster: prod-cn
    replica: prom-0      # 远端写去重用

alert_relabel_configs:
  - regex: replica
    action: labeldrop    # 告警发往 Alertmanager 前丢掉 replica
```

注意：

- `replica` 不要进入告警标签，否则 Alertmanager 无法去重；必须用 `alert_relabel_configs` 在发送前删掉；
- 如果两个副本的 external labels 不同，远端会视为不同序列；
- 两个副本的抓取时间略有差异，查询时会出现"同时间点两个值"，去重层负责选一个。

## 19.3 去重

Thanos/Mimir 的去重逻辑：

- 按 `replica` label 分组；
- 同一时间戳/序列只保留一个样本；
- 查询时合并。

前提：

- 两个副本的序列标签完全一致（除 replica）；
- 抓取时间戳一致或足够接近；
- 远端支持去重配置。

如果抓取时间戳不同，去重可能保留两个点，导致 `rate()` 抖动。

## 19.4 告警去重

Prometheus HA 对都会求值告警并发送到 Alertmanager：

- Alertmanager 按告警标签（不含 replica）去重；
- 只发一条通知；
- 如果 external labels 包含 replica，告警标签不同，会重复通知。

因此：

- `replica` 只应出现在远端存储的序列标签中，不应进入告警；
- 或者用 `alert_relabel_configs` 删除 replica；
- 确保两个副本的告警标签完全一致。

## 19.5 抓取分片

当单实例抓不下所有目标时：

### hashmod 分片

```yaml
relabel_configs:
  - source_labels: [__address__]
    modulus: 4
    target_label: __tmp_shard
    action: hashmod
  - source_labels: [__tmp_shard]
    regex: "0"
    action: keep
```

每个实例负责一个 shard。问题：

- 目标变化时 shard 移动，短暂重复/缺口；
- 需要全局查询层；
- 容量规划要留余量。

### 功能分片

按业务/集群拆：

```text
prometheus-k8s-prod
prometheus-k8s-staging
prometheus-node
prometheus-app
```

优点：故障域小、配置独立；缺点：跨域查询需要中心层。

### 联邦

边缘聚合 → 中心抓取聚合结果。适合"中心只看 SLO"，不适合全量。

## 19.6 Agent 模式

Agent 模式（`--agent`）：

- 只抓取 + remote write；
- 不加载规则、不提供查询、不保留本地 block；
- 本地 WAL 仍然存在（用于缓冲）；
- 资源占用小，适合边缘/大规模。

```yaml
global:
  scrape_interval: 30s

scrape_configs:
  - job_name: node
    static_configs:
      - targets: ["localhost:9100"]

remote_write:
  - url: https://mimir.example/api/v1/push
    queue_config:
      capacity: 10000
      max_shards: 20
```

注意：

- Agent 不做规则/告警，所有计算在中心；
- 网络中断时 WAL 会增长，需要磁盘上限；
- 重启后未发送的 WAL 会继续发送（取决于版本）；
- 不适合需要本地查询的边缘场景。

## 19.7 容量与故障域

HA + 分片后的容量：

```text
总抓取能力 ≈ 实例数 × 单实例能力 × 0.6（留余量）
```

故障域：

- 单实例故障：另一个副本继续抓取/写入；
- 远端故障：本地 WAL 缓冲，恢复后补发；
- 查询层故障：Grafana 不可用，但抓取/告警继续；
- Alertmanager 故障：告警队列积压，可能丢失。

## 19.8 常见错误

- **两个副本 external labels 不同**：无法去重。
- **replica 进入告警标签**：重复通知。
- **分片后没有全局查询**：Grafana 只能看单 shard。
- **Agent 配置了 rule_files**：不生效/报错。
- **remote write 队列满**：丢弃样本，监控 `dropped_total`。
- **HA 对抓取时间不同步**：远端出现双点，查询抖动。

## 19.9 Mini 的对应能力

Mini 不实现 HA/分片/agent，但提供了可组合的积木：

- `RemoteWriter` 可以把样本转发到中心；
- `external_labels` 已进入配置模型（告警/remote write 可扩展）；
- `hashmod` relabel 已实现，可用于分片实验；
- `scrape` 管理器支持多目标并发（线程）。

第 39 章的 relabel 示例展示了 hashmod 的用法。

## 19.10 去重的实现细节与精度

去重不是"把两个副本的样本求平均"，而是**按序列 + 时间戳选一个**：

```text
group by (all labels except replica)
for each timestamp:
    pick one sample from replicas
```

不同系统的策略：

- **Thanos Query**：`--query.replica-label=replica`，同一时间戳只返回一个；冲突值通常取第一个到达/字典序靠前的副本，具体以实现为准；
- **Mimir/Cortex**：querier 按 replica 去重，支持多个 replica label；
- **VictoriaMetrics**：`-dedup.minScrapeInterval` 按时间窗口去重。

精度问题：

- 两个副本抓取时间不同 → 时间戳不完全一致 → 去重后可能仍有双点；
- 样本值因抓取时刻不同而不同（counter 在增长）→ 去重选一个，查询可能有微小差异；
- `rate()` 对双点敏感，可能出现尖刺；
- 去重只影响查询，不影响存储/计费。

实践：

- 两个副本的 `scrape_interval`/`scrape_timeout` 完全一致；
- 用 NTP 同步时钟；
- 不要把 `replica` 放进业务标签；
- 查询层显式配置 replica label；
- 对关键 SLI 接受"副本间微小差异"，不要试图让两个副本完全一致。

## 19.11 分片一致性与再平衡

hashmod 分片的本质：

```text
shard = MD5(__address__)[0:8] as uint64 % N
```

特点：

- 同一个目标总是落到同一个 shard（目标地址不变时）；
- 目标增删/地址变化会改变分片；
- 没有自动再平衡；
- 滚动重启/配置变更期间可能出现短暂重复抓取或缺口。

减少抖动：

- 用稳定的 `__address__`（避免 Pod IP 频繁变化）；
- 分片数大于实例数（例如 8 shard / 4 实例，每实例 2 shard），扩容时迁移少；
- 用功能分片（按 cluster/namespace）代替纯 hashmod；
- 全局查询层去重，容忍短暂重复；
- 监控每个 shard 的目标数/序列数，避免热点。

注意：

- 分片后每个实例只看到部分目标，告警规则如果依赖全局聚合会出错；
- 规则要么在每个 shard 上算局部再聚合，要么在中心层算；
- `external_labels` 要能区分 shard，便于查询/排障。

## 19.12 Agent 模式深入

Agent 模式（`--agent`）的定位是"采集器"：

```text
scrape → WAL → remote write
（不加载规则、不查询、不落 block）
```

常用参数：

```text
--agent
--storage.agent.path=/var/lib/prometheus-agent
--storage.agent.wal-segment-size=128MB
--storage.agent.retention.min-time=...
--storage.agent.retention.max-time=...
```

行为：

- 本地只有 WAL，没有 block/compaction；
- WAL 在成功发送后可截断（取决于版本/配置）；
- 网络中断时 WAL 增长，磁盘满会丢数据；
- 重启后继续发送未发送的 WAL（取决于版本）；
- 不支持 `/api/v1/query`、规则、告警、UI 的完整功能。

适用：

- 边缘/多集群采集，中心统一存储；
- 大规模抓取，查询/规则集中；
- 需要降低单机资源占用。

不适用：

- 需要本地查询/告警；
- 网络长期不可靠且磁盘有限；
- 需要本地长期保留。

## 19.13 全局查询层

HA/分片后，Grafana 需要一个全局视图：

| 方案 | 能力 | 代价 |
|---|---|---|
| Thanos Query | 多 Prometheus/对象存储联邦查询、去重 | 组件多、延迟 |
| Mimir/Cortex | 多租户、长期存储、全局查询 | 运维复杂 |
| VictoriaMetrics | 单机/集群、PromQL 兼容 | 生态差异 |
| Prometheus remote read | 查询远端 | 性能/协议限制 |
| 中心 Prometheus + federation | 只聚合指标 | 丢失细节 |

选择要点：

- 查询延迟/并发；
- 去重语义；
- 多租户/权限；
- 长期存储成本；
- 故障域/部分响应；
- 与现有 Grafana/告警集成。

联邦不是"全局查询"：它只抓取聚合后的指标，适合中心看 SLO，不适合下钻到单个 Pod。

## 19.14 故障演练

| 故障 | 预期 | 检查 |
|---|---|---|
| 杀一个副本 | 另一个继续抓取/写入 | 远端去重后无缺口 |
| 网络分区 | 本地 WAL 缓冲 | 恢复后补发、无重复 |
| remote write 远端 500 | 重试/队列增长 | `dropped_total`、队列延迟 |
| 队列满 | 丢弃/降级 | 告警、容量 |
| 时钟漂移 | 时间戳不一致 | NTP、去重双点 |
| Alertmanager 挂 | 告警队列积压 | 恢复后是否补发 |
| 全局查询层挂 | 抓取/告警继续 | Grafana 不可用但不丢数据 |

演练要记录：

- 检测时间；
- 恢复时间；
- 数据缺口/重复；
- 告警是否触发；
- runbook 是否有效。

## 19.15 扩展决策树

```text
单实例够用？
├─ 是 → 单机 + 备份 + 监控
└─ 否
   ├─ 只需要冗余 → HA 对 + 远端去重
   ├─ 抓取容量不足 → hashmod/功能分片 + 全局查询
   ├─ 边缘/多集群 → Agent + 中心存储
   ├─ 需要长期存储 → Thanos/Mimir/VM
   └─ 需要多租户 → Mimir/Cortex/托管服务
```

先做基数治理和预聚合，再考虑加机器。很多"容量不足"其实是标签建模问题。

## 本章小结

- Prometheus 单机不做 HA；HA 靠双副本 + 远端去重。
- external labels 必须一致，replica 不能进入告警标签。
- 抓取分片有 hashmod/功能分片/联邦三种模式。
- Agent 模式适合边缘抓取 + 中心存储，但不做本地查询/规则。
- 监控 remote write 队列和 dropped 指标是 HA 运维的核心。

## 延伸阅读

- Prometheus 官方 "High availability" 与 "Federation"。
- Thanos/Mimir 的 HA/去重文档。
- 本教程第 18 章（远程读写）与第 22 章（容量）。
