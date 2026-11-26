# 第 18 章 远程读写、联邦与长期存储

## 18.1 为什么需要远程写

单机 Prometheus 的本地存储有边界：

- retention 有限；
- 无法跨集群查询；
- 无法水平扩展；
- 副本之间数据不共享。

Remote write 把样本发送到远端存储（Thanos Receive、Mimir、VictoriaMetrics、云托管），实现：

- 长期保留；
- 全局查询；
- 多租户；
- 与中心可观测性平台集成。

## 18.2 Remote Write 协议

请求：

```http
POST /api/v1/write
Content-Type: application/x-protobuf
Content-Encoding: snappy
X-Prometheus-Remote-Write-Version: 0.1.0
```

protobuf 结构（简化）：

```protobuf
message WriteRequest {
  repeated TimeSeries timeseries = 1;
  repeated MetricMetadata metadata = 3;
}
message TimeSeries {
  repeated Label labels = 1;
  repeated Sample samples = 2;
  repeated Exemplar exemplars = 3;
  repeated Histogram histograms = 4;
}
message Label { string name = 1; string value = 2; }
message Sample { double value = 1; int64 timestamp = 2; }
```

Snappy 是块压缩（不是帧格式）。接收端必须支持。

Mini 的 `remote.py` 手写 protobuf wire 解码 + Snappy 解压，不依赖第三方库；`encode_write_request()` 用于测试/发送。

## 18.3 队列、重试与顺序

Prometheus 的 remote write 是一个队列系统：

```text
scrape append → WAL watcher → queue → shard → HTTP sender
```

特性：

- 每个 remote write 配置独立的 queue；
- 批量发送（batch size）；
- 失败重试（指数退避）；
- 4xx（429 除外）丢弃（毒消息）；
- 5xx/网络错误/429 重试；
- 队列满时丢弃并计数；
- 多 shard 并行发送，但同一序列的顺序由 shard 保证。

监控：

```promql
prometheus_remote_storage_samples_pending
prometheus_remote_storage_samples_failed_total
prometheus_remote_storage_samples_dropped_total
prometheus_remote_storage_queue_highest_sent_timestamp_seconds
```

`highest_sent_timestamp` 落后于现在，说明队列积压。

## 18.4 去重与 external labels

多个 Prometheus 副本写同一远端时，会产生重复样本。去重依赖：

- 相同 `external_labels`（cluster/replica 之外的标签）；
- 远端存储的 dedup 逻辑（Thanos/Mimir 按 replica label 去重）；
- 时间窗口（如 Thanos 的 `--query.replica-label`）。

如果两个副本的 external labels 不同，远端会视为不同序列，无法去重。

## 18.5 Remote Write 2.0

RW 2.0 的目标：

- 支持 native histograms；
- 支持 exemplars；
- 支持 metadata；
- 更明确的符号表/字符串复用；
- 更好的乱序/部分发送语义。

截至 Prometheus 3.x，RW 2.0 仍在演进，生态支持不一。选型时要确认：

- 发送端/接收端版本；
- 是否支持 native histogram；
- 是否支持 metadata/exemplar；
- 回退到 1.0 的行为。

## 18.6 Remote Read

Remote read 让 Prometheus 查询远端历史数据：

```yaml
remote_read:
  - url: http://thanos-query:10901/api/v1/read
    read_recent: true
```

流程：

```text
PromQL query → 本地 TSDB + remote read → 合并结果
```

限制：

- 延迟高；
- 远端必须支持 read API；
- 查询下推有限（matcher/time range）；
- 不适合高频/大范围查询；
- `read_recent` 决定**近期数据**是否也发给远端：默认 `false` 时，只有本地存储覆盖不到的时间范围才查远端；设 `true` 时，所有查询（包括本地保留期内的近期数据）也会发给远端。本地存储始终参与查询与合并。

Mini 实现 JSON remote read；真实 Prometheus 用 protobuf。

## 18.7 联邦（federation）

Prometheus 的 `/federate` 端点允许上游抓取下游的聚合指标：

```yaml
- job_name: federate
  honor_labels: true
  metrics_path: /federate
  params:
    match[]:
      - '{job="prometheus"}'
      - 'job:http_requests:rate5m'
  static_configs:
    - targets: ["prometheus-shard-0:9090", "prometheus-shard-1:9090"]
```

特点：

- 只抓取 matcher 匹配的序列；
- 返回最新样本（通常是 5m 内）；
- 适合"分层聚合"：边缘做 recording rules，中心抓聚合结果；
- 不适合全量数据汇聚（会把中心压垮）。

最佳实践：

- 联邦只抓 recording rules 的结果，不抓原始高基数指标；
- 用 `honor_labels: true` 保留源标签；
- 控制 `match[]` 数量；
- 注意重复/去重。

## 18.8 长期存储方案对比

| 方案 | 定位 | 特点 |
|---|---|---|
| Thanos | 对象存储 + 全局查询 | Sidecar/Receive/Query/Store；成熟；依赖对象存储 |
| Mimir | 多租户水平扩展 | Cortex 后继；高可用；成本较高 |
| VictoriaMetrics | 高性能单机/集群 | 压缩率高；PromQL 兼容；生态略不同 |
| Cortex | 多租户 | 已被 Mimir 取代 |
| 云托管 | 托管 Prometheus | 低运维；成本/锁定需评估 |

选择维度：

- 数据量/保留期；
- 查询模式（最近 vs 历史）；
- 多租户/权限；
- 成本；
- 运维能力；
- 迁移/退出成本。

三者的写入/读取路径、去重、降采样、多租户和故障模式见附录 Q；Remote Write 2.0 的协议与迁移见附录 R。

## 18.9 Agent 模式

`--agent` 让 Prometheus 只抓取 + remote write，不提供查询/规则/本地查询：

```bash
prometheus --agent \
  --config.file=agent.yml \
  --storage.agent.path=./data-agent
```

优点：

- 边缘资源占用小；
- 无需本地长期存储；
- 中心统一查询。

限制：

- 本地不能查询；
- 规则/告警需在中心做；
- 依赖 remote write 可用性；
- 需要处理队列积压。

## 18.10 Mini 的实现

- `remote.py`：Snappy 编解码、protobuf wire 解析、WriteRequest 编解码；
- `remote_client.py`：队列、批量、重试、JSON/protobuf 发送；
- `api/server.py`：`/api/v1/write` 接收 JSON/protobuf，`/api/v1/read` 返回 JSON；
- `TSDB.add_watcher()`：样本写入后通知 remote writer；
- `App`：根据配置启动 remote writer。

Mini 不支持：

- RW 2.0；
- native histogram 传输；
- 多 shard 并发；
- WAL watcher 的精确 offset（Mini 用内存队列，重启会丢未发送样本）。

这些差异在附录 G 中列出。

## 18.11 队列调优参数

```yaml
remote_write:
  - url: https://mimir.example/api/v1/push
    remote_timeout: 30s
    queue_config:
      capacity: 10000
      min_shards: 1
      max_shards: 20
      max_samples_per_send: 2000
      batch_send_deadline: 5s
      min_backoff: 30ms
      max_backoff: 5s
      retry_on_http_429: true
      sample_age_limit: 0s
    metadata_config:
      send: true
      send_interval: 1m
      max_samples_per_send: 500
```

含义：

- `capacity`：每个 shard 的队列容量（样本数），太小会丢；
- `max_shards`：并发发送上限，太大增加内存/远端压力；
- `max_samples_per_send`：单批样本数，受远端请求大小限制；
- `batch_send_deadline`：攒批最长时间，影响延迟；
- `min_backoff`/`max_backoff`：重试退避；
- `retry_on_http_429`：429 是否重试（远端限流时应重试）；
- `sample_age_limit`：超过年龄的样本丢弃（避免无限补发）。

调优思路：

```text
队列积压 → 先看远端错误/限流，再看 max_shards/网络
内存高 → 降 capacity/max_shards/max_samples_per_send
延迟高 → 降 batch_send_deadline，但会增加请求数
```

## 18.12 联邦的语义与限制

联邦端点：

```text
GET /federate?match[]=up&match[]=job:http_requests:rate5m
```

语义：

- 只返回**即时向量**（最近样本），不返回历史；
- 每个匹配序列返回一个样本（最近值）；
- 支持 `match[]` 多个选择器；
- 输出文本格式；
- 抓取端可以用 `honor_labels` 处理冲突。

限制：

- 不是全量数据复制；
- 不适合高基数原始指标；
- 不保证样本时间戳精确对齐；
- 大规模联邦会拖慢源 Prometheus；
- 跨网络/跨集群要考虑延迟和失败。

适用：

- 中心只看聚合 SLO/容量；
- 分层监控（边缘聚合 → 中心）；
- 小规模多集群汇总。

不适用：

- 需要下钻到原始序列；
- 需要历史数据；
- 需要精确计费。

## 18.13 Remote Read 的代价与替代

Remote read 的流程：

```text
PromQL → 本地 querier + remote querier → 合并
```

代价：

- 每次查询都走网络；
- protobuf 序列化/反序列化；
- 远端可能扫描大量数据；
- 查询超时/部分失败处理复杂；
- 不能像本地 TSDB 那样利用缓存。

`read_recent: true` 让远端也返回最近数据，避免本地/远端边界缺口；但会增加重复。

替代：

- 用 remote write 把数据集中到全局存储，查询直接打全局层；
- 用 Thanos Query/Mimir 的全局查询，而不是 Prometheus remote read；
- 用 recording rules 预聚合，减少跨端查询。

Remote read 适合"偶尔查历史"，不适合"所有查询都走远端"。

## 18.14 远端存储选型与容量

| 方案 | 特点 | 适用 |
|---|---|---|
| Thanos | 对象存储 + sidecar/query/store/compact | 多 Prometheus 全局查询 |
| Mimir | 多租户、水平扩展、对象存储 | 大规模/多租户 |
| VictoriaMetrics | 高压缩、单机/集群、PromQL 兼容 | 成本敏感/中小规模 |
| 云托管 | 运维少、按量计费 | 无自建团队 |
| 自建 Cortex | 多租户、复杂 | 有平台团队 |

容量：

```text
远端摄入 = 本地摄入 × 副本数
远端存储 = 样本数 × 保留 × 每样本字节（通常比本地更省）
查询成本 = 扫描字节 × 查询频率
```

注意：

- 去重不减少摄入；
- 多租户/HA 副本放大；
- 对象存储请求成本；
- 查询层缓存/索引成本；
- retention 由后端配置，不是 Prometheus 本地 retention。

## 18.15 故障与排障

| 症状 | 可能原因 | 处理 |
|---|---|---|
| 队列积压 | 远端 429/5xx/慢 | 看远端、扩容 shards、降速 |
| 样本被丢弃 | 队列满/4xx | 增大 capacity、修请求/租户头 |
| 远端重复 | 多副本 external labels 不同 | 统一 labels/去重 |
| 时间戳乱序 | 多写者/时钟漂移 | 单写者、NTP、OOO 窗口 |
| 查询缺数据 | remote read 超时/部分失败 | 看远端、缩小范围、read_recent |
| 租户错误 | `X-Scope-OrgID` 缺失/错误 | 检查 header/配置 |
| 内存高 | 队列/批太大 | 降 capacity/max_shards |

监控：

```promql
prometheus_remote_storage_samples_pending
prometheus_remote_storage_samples_dropped_total
prometheus_remote_storage_samples_failed_total
prometheus_remote_storage_queue_highest_sent_timestamp_seconds
```

## 18.16 Mini 的边界与实验

Mini 实现：

- JSON/protobuf remote write（1.0 子集）；
- JSON remote read；
- 队列/批量/重试（内存队列）；
- Snappy 编解码；
- 无 RW2/native histogram/多 shard/持久化 offset。

实验：

```bash
# 启动 Mini + 接收端
python examples/06_remote_write.py
```

观察：

- 队列发送/重试；
- protobuf 编码；
- 远端查询；
- 重启后未发送样本丢失（Mini 限制）。

## 本章小结

- Remote write 是长期存储/全局查询的主通道，协议是 protobuf+snappy。
- 队列、重试、去重、external labels 是可靠性的关键。
- Remote read 适合历史查询，不适合高频大范围。
- 联邦适合分层聚合，不是全量汇聚。
- Agent 模式把边缘变成"抓取+转发"。

## 延伸阅读

- Prometheus 官方 "Remote write" 与 "Federation"。
- Remote Write 2.0 规范。
- Thanos/Mimir/VictoriaMetrics 文档。
- 本教程第 41 章（Mini API/远程）、附录 G（对比）、附录 Q（长期存储架构）与附录 R（RW2/原生直方图）。
