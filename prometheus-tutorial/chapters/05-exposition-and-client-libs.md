# 第 5 章 指标暴露：文本格式、OpenMetrics 与客户端库

## 5.1 抓取协议栈

一次抓取涉及：

1. HTTP GET（可带认证、TLS、代理）；
2. `Accept` 协商（text/OpenMetrics/protobuf）；
3. 响应体解析；
4. 标签合并、metric relabel；
5. 追加到 TSDB。

Prometheus 3.x 支持 `scrape_protocols` 配置，按优先级协商。Mini 默认只解析文本/OpenMetrics，并在响应含 `# EOF` 或 Content-Type 为 OpenMetrics 时启用 OpenMetrics 模式。

## 5.2 文本格式语法

一行一个样本：

```text
metric_name{label="value",label2="value2"} 12.5 1700000000000
```

要素：

- 指标名：经典字符集 `[a-zA-Z_:][a-zA-Z0-9_:]*`；Prometheus 3 的非 legacy 名字写成 `"metric.name"` 或 `{"metric.name", ...}`（有标签时用后者）；
- 标签集：可选；legacy 标签名 `[a-zA-Z_][a-zA-Z0-9_]*`，非 legacy 标签名要加引号；值用双引号；
- 值：十进制/科学计数法、`+Inf`、`-Inf`、`NaN`；
- 时间戳：可选；Prometheus 文本格式是**毫秒整数**，OpenMetrics 是**秒（可带小数）**；省略时由抓取时间填充；
- 行尾可带 exemplar（OpenMetrics）。

转义只允许：

```text
\\  → 反斜杠
\"  → 双引号
\n  → 换行
```

其他转义应视为错误。很多 exporter 在这里偷懒，导致标签值静默损坏。

## 5.3 HELP、TYPE 与 UNIT

```text
# HELP http_requests_total Total HTTP requests.
# TYPE http_requests_total counter
# UNIT http_request_duration_seconds seconds
http_requests_total{code="200"} 10
```

规则：

- `HELP` 与 `TYPE` 必须出现在该指标的第一个样本之前；
- 同一指标只能有一个 `HELP` 和一个 `TYPE`；
- `TYPE` 取值按方言区分：Prometheus text 0.0.4 是 `counter`/`gauge`/`histogram`/`summary`/`untyped`；OpenMetrics 额外允许 `info`/`stateset`/`unknown`/`gaugehistogram`，并把 `untyped` 改称 `unknown`；
- `UNIT` 是 OpenMetrics 扩展；
- `# EOF` 是 OpenMetrics 的结束标记，缺失应报错。

Mini 的解析器严格执行这些规则，并把元数据暴露到 `/api/v1/metadata`。

## 5.4 OpenMetrics 与文本格式的差异

| 维度 | Prometheus text 0.0.4 | OpenMetrics 1.0 |
|---|---|---|
| 结束标记 | 无 | 必须有 `# EOF` |
| 单位 | 无 | `# UNIT` |
| Exemplar | 非标准 | 标准 |
| Counter 命名 | 可 `_total` | `_total` 约定更严格 |
| Content-Type | `text/plain; version=0.0.4` | `application/openmetrics-text; version=1.0.0` |
| 时间戳单位 | 毫秒整数 | 秒（可为小数） |
| Created 时间戳 | 无 | 有 |

时间戳单位是最容易踩的兼容性坑：同一行 `x 1 1700000000`，在文本格式里是 1970-01-20，在 OpenMetrics 里是 2023-11-15。解析器必须先根据 `Content-Type`/`# EOF` 判定方言，再解释时间戳；exemplar 的时间戳同样遵循该方言。

迁移建议：

- 新 exporter 优先输出 OpenMetrics；
- 同时兼容文本格式，避免老 Prometheus/抓取器；
- 用 `promtool check metrics` 或 Mini 的 `check` 子命令验证。

## 5.5 客户端库的职责

以 Go 客户端为例：

```go
var (
    requests = prometheus.NewCounterVec(
        prometheus.CounterOpts{
            Name: "http_requests_total",
            Help: "Total HTTP requests.",
        },
        []string{"method", "code"},
    )
    latency = prometheus.NewHistogramVec(
        prometheus.HistogramOpts{
            Name:    "http_request_duration_seconds",
            Help:    "HTTP request latency.",
            Buckets: []float64{0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5},
        },
        []string{"method"},
    )
)

func init() {
    prometheus.MustRegister(requests, latency)
}

func handler(w http.ResponseWriter, r *http.Request) {
    timer := prometheus.NewTimer(latency.WithLabelValues(r.Method))
    defer timer.ObserveDuration()
    requests.WithLabelValues(r.Method, "200").Inc()
}
```

客户端库负责：

- 线程安全的计数器/直方图；
- 标签组合的懒创建（注意：每个新标签组合都会创建新序列）；
- `/metrics` 输出与 Content-Type；
- 进程指标（Go runtime、GC、文件描述符）；
- exemplar 注入接口。

Python 客户端：

```python
from prometheus_client import Counter, Histogram, start_http_server

REQUESTS = Counter("http_requests_total", "Total requests", ["method", "code"])
LATENCY = Histogram(
    "http_request_duration_seconds",
    "Request latency",
    ["method"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5),
)

start_http_server(9101)
REQUESTS.labels(method="GET", code="200").inc()
LATENCY.labels(method="GET").observe(0.042)
```

Java（Micrometer/Java client）、Rust、.NET 的 API 类似，核心都是"注册指标 + 标签 + 观测"。

## 5.6 自定义 Collector

当指标来自现有数据结构时，实现 `Collector` 比在业务代码里到处 `Inc()` 更干净：

```python
from prometheus_client.core import GaugeMetricFamily, REGISTRY, CollectorRegistry

class QueueCollector:
    def __init__(self, queues):
        self.queues = queues

    def collect(self):
        gauge = GaugeMetricFamily("queue_depth", "Queue depth", labels=["queue"])
        for name, depth in self.queues.items():
            gauge.add_metric([name], depth)
        yield gauge

REGISTRY.register(QueueCollector({"ingest": 12, "export": 3}))
```

原则：

- `collect()` 要快速、无阻塞；慢采集会拖垮整个 `/metrics` 请求。
- 不要在 `collect()` 里做远程调用；用缓存/后台刷新。
- 标签值来自有限集合，避免动态字符串。

## 5.7 指标命名规范

推荐：

```text
<namespace>_<subsystem>_<name>_<unit>[_total]
```

例子：

```text
myapp_http_requests_total
myapp_http_request_duration_seconds
myapp_queue_depth
myapp_cache_hits_total
```

规则：

- 单位用基本单位：秒、字节、米、伏特；不要 `_milliseconds`。
- Counter 以 `_total` 结尾；Gauge 不加 `_total`。
- Histogram/Summary 的 `_bucket`、`_sum`、`_count` 由客户端生成，不要手工写。
- 同一指标不要混用标签集合（有的样本有 `code`，有的没有），否则查询和聚合会出现"标签缺失"语义。
- 避免把版本号写进指标名（`http_requests_v2_total`），用 `version` 标签。

## 5.8 `/metrics` 端点的工程问题

- **认证**：`/metrics` 常被内部网络访问，但仍应加 basic auth/TLS 或网络策略。
- **暴露面**：不要把 `/metrics` 暴露到公网；它包含进程、版本、路径等信息。
- **成本**：每次抓取都序列化全部指标；大指标集可能导致 CPU/内存尖峰。
- **超时**：抓取超时由 Prometheus 控制；exporter 应支持快速返回。
- **gzip**：Prometheus 默认请求 gzip；exporter 可选择不支持。
- **并发**：多个 Prometheus 实例同时抓取时，exporter 要能承受。

## 5.9 推送场景

Prometheus 默认拉取，但以下场景需要推送：

- 批处理作业（几分钟内完成）；
- Serverless/短生命周期容器；
- 无法被 Prometheus 主动访问的网络。

方案：

1. **Pushgateway**：作业结束时 `POST` 指标；Prometheus 抓取 Pushgateway。注意：Pushgateway 不会让指标自动过期，必须显式删除或设置 `push_time_seconds` 告警。
2. **OTLP**：应用通过 OTel SDK 推送到 Collector，再由 Collector 转成 Prometheus remote write。
3. **Agent 模式**：Prometheus agent 在边缘抓取，remote write 到中心。

Pushgateway 的标签设计尤其重要：`job`/`instance` 由 push 请求决定，错误的 `instance` 会让序列永久累积。Pushgateway 的 PUT/POST/DELETE 语义、`push_time_seconds` 监控和清理策略见附录 T。

## 5.10 Mini 的解析器实现要点

`mini_prometheus/exposition.py` 做了几件容易被忽略的事：

- 标签值转义严格校验；
- 同一序列同一时间戳重复写入：值相同则忽略，值不同则报错；
- `TYPE` 必须早于样本；
- 支持引号指标名/标签名 `{"http.requests.total", "service.name"="api"}`，并用 `# HELP "name"`/`# TYPE "name"`；
- OpenMetrics 的 `# EOF`、`# UNIT`、exemplar；
- `max_samples` 在解析阶段就截断，避免把超大响应读入内存。

这些规则与生产 Prometheus 的文本解析器一致到"足够迁移"的程度，差异主要在性能与 protobuf 支持。

## 5.11 压缩、分块与 protobuf 抓取

文本格式简单，但在大指标集下体积/解析成本高。生产抓取栈通常还有两层优化：

### 5.11.1 gzip

Prometheus 默认发送：

```text
Accept-Encoding: gzip
```

exporter 可以返回 `Content-Encoding: gzip`。收益：

- 网络传输减少（文本压缩率通常很高）；
- 解析前需要解压，CPU 成本上升；
- 抓取超时包含解压时间；
- 不要对已压缩的 protobuf 再套 gzip（除非协议明确）。

如果 exporter 不支持 gzip，Prometheus 会退回明文；不是错误，但大响应会变慢。

### 5.11.2 protobuf exposition

Prometheus 生态历史上支持 protobuf exposition（`MetricFamily` delimited）。它的优点是：

- 类型/元数据更明确；
- 解析更快、体积更小；
- 适合大指标集/高抓取频率。

代价：

- 调试不如文本直观；
- 客户端/代理支持不齐；
- 与 OpenMetrics 的语义映射需要小心（counter 命名、timestamp、exemplar）。

Prometheus 3.x 的 `scrape_protocols` 可以按优先级协商文本/OpenMetrics/protobuf。生产建议：

- 默认用 OpenMetrics 文本，兼容性最好；
- 只有在指标集很大、解析成为瓶颈时评估 protobuf；
- 切换协议后验证类型/单位/exemplar/时间戳；
- 保留回退路径。

### 5.11.3 抓取内容协商

```text
Accept: application/openmetrics-text; version=1.0.0,
        text/plain; version=0.0.4
```

服务端返回的 `Content-Type` 决定解析方言。不要在响应体里"猜"格式；`# EOF` 可以作为 OpenMetrics 的辅助判断，但 Content-Type 才是主依据。

## 5.12 Exemplar 的暴露与限制

Exemplar 只在 OpenMetrics 方言中有标准语法：

```text
http_request_duration_seconds_bucket{le="0.5"} 129389 # {trace_id="abc123"} 0.42 1700000000.123
```

要点：

- exemplar 必须紧跟样本行；
- 时间戳是秒（可带小数），不是毫秒；
- 一个样本行最多一个 exemplar；
- 经典文本格式不支持 exemplar；
- 抓取必须协商到 OpenMetrics，否则 exemplar 被忽略/解析失败（取决于实现）。

客户端侧：

- Go 客户端通过 `ExemplarObserver.ObserveWithExemplar` 注入 `trace_id`；
- OTel SDK 在 span context 存在时自动附加 exemplar；
- Python/Java/Node 的支持程度随版本变化，接入前验证。

存储侧：

- exemplar 不进入 block，只在 head 的环形缓冲中保留很短时间；
- Remote Write 1.0 的 `TimeSeries` 已含 `exemplars` 字段（需 `send_exemplars: true`）；Remote Write 2.0 把 exemplar 一等化并降低开销，但不是"从无到有"；
- 查询用 `/api/v1/query_exemplars`，只接受向量选择器。

完整的关联设计、Grafana 配置、profiling 见第 44 章、附录 M（Grafana）与附录 V（API）。

## 本章小结

- 文本格式简单但规则严格：转义、TYPE/HELP 顺序、时间戳、重复样本。
- OpenMetrics 增加 EOF、UNIT、exemplar 和更严格的 counter 语义。
- 客户端库负责线程安全、标签组合和 `/metrics` 输出；自定义 Collector 适合桥接现有数据。
- 命名、单位、桶边界和标签集合是长期可维护性的关键。
- Pushgateway 只适合短生命周期任务，且必须管理指标过期。

## 延伸阅读

- Prometheus 官方 "Exposition formats" 与 "Writing exporters"。
- OpenMetrics 规范。
- 本教程第 4 章（类型）、第 32 章（Mini 解析器实现）、附录 AA（exporter/探针的指标语义）与附录 AD（自建 exporter 开发指南）。
