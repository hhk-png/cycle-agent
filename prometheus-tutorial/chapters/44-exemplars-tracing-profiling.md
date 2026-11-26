# 第 44 章 Exemplars、追踪关联与 Profiling

## 44.1 指标回答"哪里不对"，exemplar 回答"哪一次不对"

指标擅长聚合：`http_request_duration_seconds` 的 P99 从 120ms 涨到 900ms，能告诉你"哪个服务、哪个接口、哪个版本变慢了"。但它不告诉你具体是哪一次请求、哪个用户、哪条 SQL。日志和追踪能回答请求级问题，代价是数据量大、查询慢、采样后不完整。

Exemplar 是两者之间的"指针"：它把一条指标样本与一个请求级标识（通常是 `trace_id`）关联起来。你在 Grafana 的延迟直方图上看到一个高延迟桶，点击 exemplar 就能跳到对应 trace，看到这次请求的完整调用链。

```text
指标：http_request_duration_seconds_bucket{le="0.5",service="api"} 129389
exemplar：{trace_id="4bf92f3577b34da6a3ce929d0e0e4736"} 0.42 1700000000.123
```

这不是"把 trace 存进 Prometheus"。Prometheus 只保存少量 exemplar（每个序列一个短环形缓冲），trace 本身仍在 Tempo/Jaeger/商业 APM 中。理解这个分工，才能避免把 exemplar 当成"全量请求记录"。

## 44.2 Exemplar 的数据模型

一个 exemplar 由三部分组成：

- **标签集**：至少包含一个可用于关联的标识，常见是 `trace_id`、`span_id`；也可以带 `user_id`、`request_id`，但要谨慎；
- **值**：被观测的样本值，例如这次请求的延迟 0.42 秒；
- **时间戳**：观测时间。OpenMetrics 里是秒（可带小数），Prometheus 内部转成毫秒。

在 OpenMetrics 文本中，exemplar 跟在样本行之后，用 ` # ` 分隔：

```text
http_request_duration_seconds_bucket{le="0.5",service="api"} 129389 # {trace_id="abc123",span_id="def456"} 0.42 1700000000.123
```

规则：

- exemplar 只出现在 OpenMetrics 方言中；经典 Prometheus 文本格式没有标准 exemplar 语法；
- exemplar 必须紧跟在它所属的样本行后；
- exemplar 的标签名/值同样需要转义；
- 一个样本行最多带一个 exemplar；
- exemplar 的时间戳是**秒**，不是毫秒；这是实现里最容易写错的地方之一。

OpenMetrics 还允许 `# {trace_id="..."} 0.42` 省略时间戳，此时由抓取时间填充。生产 exporter 应显式带时间戳，避免抓取延迟造成关联偏差。

## 44.3 客户端如何产生 exemplar

以 Go 客户端为例，直方图实现了 `ExemplarObserver`：

```go
histogram := prometheus.NewHistogramVec(
    prometheus.HistogramOpts{
        Name:    "http_request_duration_seconds",
        Help:    "HTTP request latency.",
        Buckets: prometheus.DefBuckets,
    },
    []string{"method", "route", "code"},
)

func observe(method, route, code, traceID string, seconds float64) {
    observer := histogram.WithLabelValues(method, route, code)
    if exemplarObserver, ok := observer.(prometheus.ExemplarObserver); ok {
        exemplarObserver.ObserveWithExemplar(
            seconds,
            prometheus.Labels{"trace_id": traceID},
        )
        return
    }
    observer.Observe(seconds)
}
```

关键点：

- **只有被采样的请求才有 trace_id**。如果追踪采样率是 1%，exemplar 也只能覆盖 1% 的请求；这是预期行为，不是缺陷。
- exemplar 标签不要放高基数业务字段。`trace_id` 本身基数极高，但 exemplar 存储有上限，且不进入倒排索引；`user_id`、`url` 则可能既贵又无关联价值。
- exemplar 应该加在**直方图/摘要**上，而不是所有 counter/gauge；最有价值的是延迟和请求大小。
- 一致性：exemplar 的 `trace_id` 必须与追踪系统里的 ID 格式一致（通常是 32 位十六进制，W3C Trace Context）。

OpenTelemetry 的指标 SDK 也支持 exemplar：当当前 span context 存在时，SDK 会把 `trace_id`/`span_id` 附加到聚合点。通过 OTel Collector 的 `prometheusremotewrite` exporter 或 Prometheus 的 OTLP receiver，exemplar 可以进入 Prometheus 生态。具体支持程度取决于 SDK、Collector 版本和 exporter 配置，接入前要验证。

Python 客户端的 exemplar 支持随版本变化，部分版本只提供底层 `Exemplar` 对象而不自动注入 trace context。生产接入应以所用客户端版本文档为准，不要假设"换了语言行为一样"。

## 44.4 抓取、协商与存储

Prometheus 只有在抓取协议协商到 OpenMetrics 时才会解析 exemplar。典型配置：

```yaml
global:
  scrape_protocols:
    - OpenMetricsText1.0.0
    - PrometheusText0.0.4

scrape_configs:
  - job_name: api
    static_configs:
      - targets: ["api:8080"]
```

抓取时 `Accept` 头大致是：

```text
application/openmetrics-text; version=1.0.0,
text/plain; version=0.0.4
```

如果 exporter 只返回 `text/plain`，Prometheus 不会凭空产生 exemplar；如果 exporter 返回 OpenMetrics 但缺少 `# EOF`，抓取会失败。协议协商、`# EOF` 和 exemplar 时间戳是三个独立的坑，见第 5、12 章。

存储层的关键事实：

- exemplar 存在 TSDB 的**全局环形缓冲**里，不写入 block，也不参与 compaction；
- 缓冲区大小由 `storage.exemplars.max_exemplars` 控制（默认 100000），是**所有序列共享**的上限，不是每个序列固定几条；
- Prometheus 3.x 的较新版本已把 `--enable-feature=exemplar-storage` 标记为 deprecated（默认开启）；2.x 需要显式开启，3.x 早期版本仍建议检查 feature flags。缓冲区耗尽时旧 exemplar 被覆盖，保留时间取决于写入速率；
- exemplar 会随样本写入 WAL，重启后可从 WAL 恢复，但同样受全局上限约束；
- 因此 exemplar 的保留时间远短于样本，通常只适合"最近出问题，点进去看"；
- Remote Write 1.0 的 `TimeSeries` 已有 `exemplars` 字段，设置 `send_exemplars: true` 即可发送；Remote Write 2.0 进一步把 exemplar 一等化并降低编码开销。跨后端长期保留 exemplar 仍需要接收端支持。

这也解释了为什么"exemplar 查不到"通常不是查询写错，而是：抓取没协商到 OpenMetrics、exemplar 已被环形缓冲覆盖、或者查询范围超出了保留窗口。

## 44.5 查询 API 与 Grafana

Prometheus 提供专门的 exemplar 查询端点：

```bash
curl -sG 'http://localhost:9090/api/v1/query_exemplars' \
  --data-urlencode 'query=http_request_duration_seconds_bucket{le="0.5"}' \
  --data-urlencode 'start=1700000000' \
  --data-urlencode 'end=1700000600'
```

响应大致是：

```json
{
  "status": "success",
  "data": [
    {
      "seriesLabels": {"__name__": "http_request_duration_seconds_bucket", "le": "0.5"},
      "exemplars": [
        {"labels": {"trace_id": "abc123"}, "value": "0.42", "timestamp": 1700000000.123}
      ]
    }
  ]
}
```

约束：

- `query` 必须是**向量选择器**（可以带标签匹配），不能是任意 PromQL 表达式；
- `start`/`end` 是秒级时间戳；
- 返回的 exemplar 按序列分组；
- 查询范围过大或序列过多时，结果会被限制；这不是分页 API。

Grafana 的 Prometheus 数据源支持 exemplar：在查询选项里开启 `Exemplars`，并配置 trace 数据源（Tempo/Jaeger）和字段映射（`trace_id` → trace ID）。点击 exemplar 后，Grafana 用 `trace_id` 跳转到 trace 视图。字段映射因 Grafana 版本和数据源而异，常见配置是：

```text
Trace ID label name: trace_id
Span ID label name: span_id
```

如果点击后跳转失败，先检查三件事：exemplar 查询是否返回数据、`trace_id` 是否与追踪系统一致、Grafana 的 trace 数据源是否配置了正确的字段名。

## 44.6 追踪关联的设计模式

把 exemplar 用好的关键不是"打开开关"，而是设计关联链路：

1. **入口生成/传播 trace context**：HTTP 入口读取 `traceparent`，没有就生成；下游通过 header 传播。
2. **在观测点注入 exemplar**：在请求结束时，把当前 span 的 `trace_id` 附加到延迟直方图。
3. **采样策略一致**：exemplar 只覆盖被采样的请求；如果追踪用尾部采样，注意采样决策发生在请求结束后，exemplar 仍可携带已生成的 trace_id。
4. **trace 存储可查**：Tempo/Jaeger 必须能按 trace_id 查询；如果 trace 只保留 1 小时，exemplar 再长也没意义。
5. **标签命名统一**：`trace_id`/`span_id` 是最常见的约定；不要一半服务用 `traceID`、一半用 `trace_id`，否则 Grafana 无法统一跳转。

一个完整的请求链路：

```text
client
  → gateway (生成 trace_id)
  → api (读取 trace_id，直方图 ObserveWithExemplar)
  → db (span)
  → trace 存储 (Tempo)
  → Grafana 指标面板 (exemplar) → 点击 → Tempo trace
```

注意：exemplar 关联的是**采样后的 trace**，不是全量请求。做容量/错误分析时，不能把 exemplar 当成无偏样本；它的价值是"从异常指标跳到具体案例"，不是统计推断。

## 44.7 Profiling：从 trace 再往下

Trace 告诉你"哪一次请求慢"，profile 告诉你"慢在哪个函数"。连续 profiling（continuous profiling）把 CPU/内存/锁竞争的采样持续收集起来，常见实现有 Pyroscope、Grafana Alloy、Parca、商业 APM 的 profiler。

Prometheus 不存储 profile；它提供的是**关联维度**：

- 用 `service`、`version`、`pod`、`region` 等标签把指标、trace、profile 对齐；
- 在 Grafana 中从指标面板跳到 trace，再从 trace 跳到 profile；
- 用 exemplar 的 `trace_id` 作为跨系统的关联键；
- profile 的标签基数要控制，避免把 `request_id` 放进 profile 标签。

典型工具链：

```text
应用 (pprof / OTel profiles)
  → Grafana Alloy / Pyroscope agent
  → Pyroscope / Tempo profiles
  → Grafana 关联查询
```

Profiling 的成本主要是 CPU/内存开销和存储。生产建议：

- 低频率采样（如 10～100 Hz），不要全量；
- 只在关键服务开启，或按需开启；
- 为 profile 设置保留期和租户隔离；
- 不要把 profile 当成指标存储的替代品。

## 44.8 生产实践与成本

Exemplar 的存储成本远低于 trace，但不是零：

- 每个序列的 exemplar 数量有上限，序列数越多，总内存越大；
- exemplar 标签值（尤其 trace_id）虽然不进倒排索引，但仍占内存；
- 抓取频率越高、目标越多，exemplar 产生速率越高；
- Remote Write 2.0 会放大网络与接收端压力。

治理建议：

- 只在延迟/大小等"值得跳转"的指标上开启 exemplar；
- 统一 `trace_id` 标签名和格式；
- 控制 exemplar 标签数量（1～3 个），不要放业务高基数；
- 为 trace 存储设置足够的保留期，否则 exemplar 是"死链接"；
- 监控 exemplar 查询的延迟和错误率；
- 多租户环境下，exemplar 也要按租户隔离，避免跨租户 trace_id 泄漏。

### 44.8.1 Exemplar 容量与配置

单机 Prometheus 的 exemplar 存在**全局环形缓冲**里，关键配置：

```yaml
storage:
  exemplars:
    max_exemplars: 100000   # 默认值，所有序列共享
```

它不按序列配额，也没有 per-tenant 隔离；写入速率高时，旧 exemplar 会被快速覆盖。一个粗算：

```text
保留时间 ≈ max_exemplars / exemplar 写入速率
例：100000 / 200 个/秒 ≈ 500 秒 ≈ 8 分钟
```

如果 trace 存储保留 7 天，而 exemplar 只保留 8 分钟，点进 exemplar 的链接很快会失效；这不是查询错误，而是容量模型没对齐。

容量与治理要点：

- 估算 exemplar 写入速率：每目标每秒新增多少个 exemplar，乘以目标数；
- `max_exemplars` 不是越大越好：每条 exemplar 带标签和 trace_id，内存随规模增长；
- 只对关键延迟/大小指标开启 exemplar，不要全量开启；
- 高基数序列会加速环形缓冲淘汰，优先治理标签；
- 多租户/长期存储场景用 Mimir 等支持 per-tenant exemplar 的后端，单机 Prometheus 没有租户隔离；
- 监控 exemplar 自身指标（名称以你使用的版本为准）：`prometheus_tsdb_exemplar_exemplars_in_storage`、`prometheus_tsdb_exemplar_exemplars_appended_total`、`prometheus_tsdb_exemplar_out_of_order_exemplars_total`、`prometheus_tsdb_exemplar_too_old_exemplars_total`；
- exemplar 随样本写入 WAL，重启后可恢复，但仍受全局上限约束；Remote Write 发送 exemplar 会增加接收端压力。

安全注意：exemplar 标签是用户可控数据。不要在 exemplar 里放密码、token、完整 URL、SQL；`trace_id` 本身不是秘密，但可以用于关联内部请求。对外暴露 `/api/v1/query_exemplars` 时要加认证和授权。

## 44.9 Mini 的实现与边界

Mini-Prometheus 实现了 exemplar 的解析、抓取挂载和查询：

- `exposition.py` 解析 OpenMetrics exemplar，并按方言解释时间戳（秒 vs 毫秒）；
- `ScrapePayload.exemplars` 保存 `(series_labels, Exemplar)`；
- `scrape/manager.py` 把 exemplar 合并 target labels 后挂到 `TargetState.exemplars`；
- `api/server.py` 的 `/api/v1/query_exemplars` 遍历目标上的 exemplar，按时间范围和标签过滤。

它没有实现：

- exemplar 的 TSDB 环形缓冲与 WAL 持久化；
- exemplar 的全局去重/限流；
- Remote Write 2.0 的 exemplar 传输；
- Grafana 的 trace 跳转（需要外部 Tempo/Jaeger）。

因此 Mini 的 exemplar 是"每个目标最近一次抓取"的快照，不是生产级 exemplar 存储。用它验证协议、时间戳和查询形状足够；生产请以真实 Prometheus 的 exemplar 行为为准。

运行示例：

```bash
python -m pytest tests/test_exposition.py -q
python -m pytest tests/test_api.py -q -k exemplar
```

## 44.10 检查表

- [ ] 指标是否用 OpenMetrics 暴露，抓取是否协商到 OpenMetrics？
- [ ] exemplar 是否只加在延迟/大小等关键指标上？
- [ ] `trace_id`/`span_id` 命名是否与追踪系统一致？
- [ ] 追踪采样率是否已知，exemplar 是否被误当成全量样本？
- [ ] trace 存储保留期是否覆盖排查窗口？
- [ ] exemplar 标签是否避免了密码/token/URL 等高基数敏感信息？
- [ ] 是否监控了 exemplar 查询延迟和存储压力？
- [ ] 多租户下 exemplar 是否隔离？

## 本章小结

- Exemplar 是指标到请求级证据的指针，不是全量 trace 存储。
- 它通过 OpenMetrics 暴露，抓取协商、`# EOF` 和秒级时间戳是三个关键细节。
- 客户端在直方图上注入 `trace_id`，Grafana 用字段映射跳转到 Tempo/Jaeger。
- Exemplar 只覆盖被采样的请求，适合"从异常指标跳到案例"，不适合统计推断。
- Profiling 通过 `service`/`version`/`pod` 等标签与指标、trace 关联，Prometheus 不存储 profile。
- Mini 实现了解析/抓取/查询，但没有生产级 exemplar 存储与 RW2 传输。

## 延伸阅读

- OpenMetrics 规范中的 exemplar 章节。
- Prometheus 官方文档 "Exemplars" 与 `/api/v1/query_exemplars`。
- Grafana 文档：Prometheus data source 的 exemplar 与 trace 关联。
- OpenTelemetry 规范：metrics exemplar 与 trace context。
- 本教程第 4 章（类型）、第 5 章（exposition）、第 12 章（抓取）、第 18 章（remote write）、第 20 章（OTel）与附录 R（RW2）。
