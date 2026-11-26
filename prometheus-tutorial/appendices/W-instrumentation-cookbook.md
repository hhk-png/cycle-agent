# 附录 W 客户端埋点示例集

本附录是第 5、47 章的配套代码。目标是给出"能直接改成生产代码"的骨架，而不是完整框架。所有示例都遵循同一组原则：

- 指标名/单位/类型稳定；
- 标签值有界；
- 不在请求路径上做慢操作；
- 失败/取消也要被观测；
- 埋点本身有测试。

## W.1 先决定"观测什么"

在写代码前，先选模型：

- **RED**（服务）：Rate、Errors、Duration；
- **USE**（资源）：Utilization、Saturation、Errors；
- **四个黄金信号**：延迟、流量、错误、饱和度；
- **业务指标**：订单、支付、库存、token 成本。

一个 HTTP 服务的最小集合：

```text
http_requests_total{method,route,code}
http_request_duration_seconds{method,route}   # histogram
http_requests_in_flight{method}                # gauge
http_response_size_bytes{method,route}         # histogram（可选）
```

不要一开始就埋几十个指标。先覆盖 SLO 和排障最常用的 5～10 个。

## W.2 Go 客户端

### W.2.1 注册与暴露

```go
package main

import (
    "net/http"

    "github.com/prometheus/client_golang/prometheus"
    "github.com/prometheus/client_golang/prometheus/promhttp"
)

var (
    requests = prometheus.NewCounterVec(
        prometheus.CounterOpts{
            Namespace: "myapp",
            Subsystem: "http",
            Name:      "requests_total",
            Help:      "Total HTTP requests.",
        },
        []string{"method", "route", "code"},
    )
    duration = prometheus.NewHistogramVec(
        prometheus.HistogramOpts{
            Namespace: "myapp",
            Subsystem: "http",
            Name:      "request_duration_seconds",
            Help:      "HTTP request latency.",
            Buckets:   []float64{0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10},
        },
        []string{"method", "route"},
    )
    inFlight = prometheus.NewGaugeVec(
        prometheus.GaugeOpts{
            Namespace: "myapp",
            Subsystem: "http",
            Name:      "requests_in_flight",
            Help:      "Current in-flight HTTP requests.",
        },
        []string{"method"},
    )
)

func init() {
    prometheus.MustRegister(requests, duration, inFlight)
}

func main() {
    mux := http.NewServeMux()
    mux.Handle("/metrics", promhttp.Handler())
    mux.Handle("/", instrument(http.HandlerFunc(handler)))
    _ = http.ListenAndServe(":8080", mux)
}

func handler(w http.ResponseWriter, r *http.Request) {
    w.WriteHeader(http.StatusOK)
}
```

### W.2.2 中间件

```go
func instrument(next http.Handler) http.Handler {
    return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
        route := routeTemplate(r) // 必须是有限集合，不能用原始 URL
        method := r.Method

        inFlight.WithLabelValues(method).Inc()
        defer inFlight.WithLabelValues(method).Dec()

        timer := prometheus.NewTimer(duration.WithLabelValues(method, route))
        recorder := &statusRecorder{ResponseWriter: w, status: 200}
        defer func() {
            timer.ObserveDuration()
            requests.WithLabelValues(method, route, statusClass(recorder.status)).Inc()
        }()

        next.ServeHTTP(recorder, r)
    })
}

type statusRecorder struct {
    http.ResponseWriter
    status int
}

func (r *statusRecorder) WriteHeader(code int) {
    r.status = code
    r.ResponseWriter.WriteHeader(code)
}
```

关键点：

- `route` 用路由模板（`/users/:id`），不要用原始 URL；否则 `user_id` 进入标签，基数爆炸；
- `code` 可以按类聚合（`2xx`/`4xx`/`5xx`），也可以保留精确 code；两者基数不同；
- `defer` 保证 panic/提前返回也记录；
- `statusRecorder` 要处理 `WriteHeader` 只调用一次。

### W.2.3 Exemplar

```go
func observeWithTrace(route, method, traceID string, seconds float64) {
    observer := duration.WithLabelValues(method, route)
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

Exemplar 只覆盖被追踪的请求；不要在没有 trace context 时伪造 ID。

### W.2.4 自定义 Collector

当指标来自共享数据结构时，用 Collector 比在业务代码里到处 `Inc()` 更干净：

```go
type QueueCollector struct {
    depth *prometheus.Desc
    queue func() map[string]int
}

func NewQueueCollector(queue func() map[string]int) *QueueCollector {
    return &QueueCollector{
        depth: prometheus.NewDesc(
            "myapp_queue_depth",
            "Current queue depth.",
            []string{"queue"},
            nil,
        ),
        queue: queue,
    }
}

func (c *QueueCollector) Describe(ch chan<- *prometheus.Desc) {
    ch <- c.depth
}

func (c *QueueCollector) Collect(ch chan<- prometheus.Metric) {
    for name, value := range c.queue() {
        ch <- prometheus.MustNewConstMetric(c.depth, prometheus.GaugeValue, float64(value), name)
    }
}
```

`Collect` 必须快速、无阻塞；慢采集会拖垮整个 `/metrics`。

## W.3 Python 客户端

### W.3.1 基础

```python
from prometheus_client import Counter, Gauge, Histogram, start_http_server

REQUESTS = Counter(
    "myapp_http_requests_total",
    "Total HTTP requests.",
    ["method", "route", "code"],
)
DURATION = Histogram(
    "myapp_http_request_duration_seconds",
    "HTTP request latency.",
    ["method", "route"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)
IN_FLIGHT = Gauge(
    "myapp_http_requests_in_flight",
    "Current in-flight HTTP requests.",
    ["method"],
)

start_http_server(9100)
```

### W.3.2 中间件（WSGI/ASGI 思路）

```python
import time
from prometheus_client import Counter, Histogram

REQUESTS = Counter(
    "myapp_http_requests_total", "Total HTTP requests.",
    ["method", "route", "code"],
)
DURATION = Histogram(
    "myapp_http_request_duration_seconds", "HTTP request latency.",
    ["method", "route"],
)

def instrument(handler):
    def wrapped(request):
        route = request.route_template  # 有限集合
        method = request.method
        started = time.perf_counter()
        status = 500
        try:
            response = handler(request)
            status = response.status_code
            return response
        finally:
            DURATION.labels(method, route).observe(time.perf_counter() - started)
            REQUESTS.labels(method, route, str(status)).inc()
    return wrapped
```

Python 的 GIL/多进程要注意：

- 每个进程有自己的 registry；多 worker 时需要 `prometheus_client.multiprocess` 或聚合；
- `start_http_server` 在 fork 后可能重复绑定端口；
- 不要在请求路径上做 `collect()` 重活。

### W.3.3 Summary 与 Histogram

```python
from prometheus_client import Summary

LATENCY = Summary(
    "myapp_request_latency_seconds",
    "Request latency.",
    ["route"],
)
LATENCY.labels("/api").observe(0.042)
```

Summary 的分位数不可跨实例聚合；服务端指标优先用 Histogram。

## W.4 Java / Micrometer

```java
import io.micrometer.core.instrument.Counter;
import io.micrometer.core.instrument.MeterRegistry;
import io.micrometer.core.instrument.Timer;
import io.micrometer.core.instrument.DistributionSummary;

public final class Metrics {
    private final Counter requests;
    private final Timer latency;
    private final DistributionSummary responseSize;

    public Metrics(MeterRegistry registry) {
        this.requests = Counter.builder("myapp.http.requests")
            .description("Total HTTP requests.")
            .tag("method", "GET")
            .tag("route", "/api/users")
            .register(registry);
        this.latency = Timer.builder("myapp.http.request.duration")
            .description("HTTP request latency.")
            .publishPercentileHistogram()
            .register(registry);
        this.responseSize = DistributionSummary.builder("myapp.http.response.size")
            .baseUnit("bytes")
            .register(registry);
    }

    public void observe(long nanos, int bytes, int status) {
        requests.increment();
        latency.record(java.time.Duration.ofNanos(nanos));
        responseSize.record(bytes);
    }
}
```

Micrometer 会做命名转换（点号/下划线、`_total` 后缀等），接入 Prometheus 前要验证最终指标名。`publishPercentileHistogram()` 会生成桶；桶边界要覆盖 SLO。

## W.5 Node.js / prom-client

```javascript
const client = require('prom-client');

client.collectDefaultMetrics();

const requests = new client.Counter({
  name: 'myapp_http_requests_total',
  help: 'Total HTTP requests.',
  labelNames: ['method', 'route', 'code'],
});

const duration = new client.Histogram({
  name: 'myapp_http_request_duration_seconds',
  help: 'HTTP request latency.',
  labelNames: ['method', 'route'],
  buckets: [0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10],
});

function middleware(req, res, next) {
  const route = req.route ? req.route.path : 'unmatched';
  const end = duration.startTimer({ method: req.method, route });
  res.on('finish', () => {
    end();
    requests.inc({
      method: req.method,
      route,
      code: String(res.statusCode),
    });
  });
  next();
}

// /metrics
app.get('/metrics', async (req, res) => {
  res.set('Content-Type', client.register.contentType);
  res.end(await client.register.metrics());
});
```

Node 的 `route` 同样要用模板，不要用 `req.url`。

## W.6 OpenTelemetry 接入

### W.6.1 OTel SDK → Collector → Prometheus

```text
App (OTel SDK)
  → OTLP (gRPC/HTTP) → Collector
  → prometheusremotewrite exporter
  → Prometheus/Mimir/Thanos
```

Collector 负责：

- 批处理/重试/缓冲；
- delta → cumulative（如需）；
- 属性 → 标签的转换；
- 基数治理（filter/transform）；
- 多后端 fan-out。

### W.6.2 直接暴露 Prometheus

如果只需要指标，Prometheus client library 更简单、可控；如果已有 OTel tracing/logs，Collector 路径更统一。不要为了"统一"把简单指标埋点复杂化。

### W.6.3 命名映射

OTel 语义约定常用点号：

```text
http.server.request.duration
http.server.active_requests
```

Prometheus 习惯下划线 + 单位后缀：

```text
http_server_request_duration_seconds
http_server_active_requests
```

映射规则要在 Collector/客户端统一，避免同一指标出现两个名字。

## W.7 RED/USE 埋点清单

### W.7.1 服务（RED）

- `*_requests_total{method,route,code}`；
- `*_request_duration_seconds{method,route}`（histogram）；
- `*_requests_in_flight{method}`；
- `*_response_size_bytes{route}`（可选）。

### W.7.2 资源（USE）

- CPU：`process_cpu_seconds_total`、`container_cpu_usage_seconds_total`；
- 内存：`process_resident_memory_bytes`、`container_memory_working_set_bytes`；
- 磁盘：`node_filesystem_avail_bytes`、`node_disk_io_time_seconds_total`；
- 网络：`node_network_receive_bytes_total`、`node_network_transmit_bytes_total`；
- 饱和度：队列深度、线程池、连接池、GC 暂停。

### W.7.3 业务

- 订单/支付/库存；
- token 使用/成本；
- 缓存命中/回源；
- 第三方依赖延迟/错误。

业务指标要能映射到 SLO/收入，否则容易变成"好看但没用"。

## W.8 Histogram 桶设计

原则：

- 覆盖 SLO 边界（如 300ms 必须有 `le="0.3"`）；
- 覆盖典型值和长尾；
- 桶数 10～20；
- 单位统一（秒）；
- 不要事后改桶（历史数据不可重算）。

一个通用起点（秒）：

```text
0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10
```

如果 SLO 是 P99 < 300ms，至少加 `0.3`；如果延迟可能到分钟级，加 `30`、`60`。桶太密会让序列数/存储上升；桶太疏会让分位数失真。

## W.9 埋点测试

### W.9.1 单元测试

- 调用业务函数后，检查 counter 增量；
- 检查 histogram 的 `_count`/`_sum`；
- 检查标签值是否在允许集合；
- 检查异常路径也记录。

Go 示例：

```go
func TestHandlerRecordsMetrics(t *testing.T) {
    before := testutil.ToFloat64(requests.WithLabelValues("GET", "/api", "200"))
    // call handler
    after := testutil.ToFloat64(requests.WithLabelValues("GET", "/api", "200"))
    if after != before+1 {
        t.Fatalf("counter did not increment: %v -> %v", before, after)
    }
}
```

### W.9.2 Exposition 检查

```bash
curl -s http://localhost:8080/metrics > /tmp/metrics.txt
promtool check metrics < /tmp/metrics.txt
```

检查：命名、单位、类型、重复、格式。

### W.9.3 基数测试

- 用 fuzz/随机输入跑中间件，断言标签值集合不增长；
- 在 staging 观察 `scrape_series_added`；
- 对 `route`/`code`/`method` 做枚举校验。

## W.10 常见反模式

- 用原始 URL 做标签：`/users/12345` → 每个用户一条序列。
- 用错误文本做标签：无限基数。
- 用 `time.Now()` 做标签：每条样本新序列。
- 在 `/metrics` 里查数据库：抓取超时、级联故障。
- 只埋 counter 不埋 histogram：无法回答"慢在哪"。
- 对 gauge 用 `rate()`：语义错误。
- 单位混用：毫秒/秒、字节/比特。
- 多进程 registry 冲突：指标重复/丢失。
- 没有 owner/目录：指标腐烂。

## W.11 检查表

- [ ] 指标名含 namespace/subsystem/unit，counter 以 `_total` 结尾？
- [ ] 标签值有界，路由用模板，code 可控？
- [ ] 延迟用 histogram，桶覆盖 SLO？
- [ ] 异常/取消路径也记录？
- [ ] `/metrics` 快速、无阻塞、无远程调用？
- [ ] 多进程/多线程 registry 正确？
- [ ] exemplar 的 trace_id 与追踪系统一致？
- [ ] 有单元测试、exposition 检查、基数测试？
- [ ] 指标有 owner/目录/面板/告警？

## 延伸阅读

- Prometheus 官方 "Instrumentation"、"Writing exporters"。
- Go/Python/Java/Node 客户端文档。
- OpenTelemetry 语义约定与 Collector。
- 本教程第 5 章（exposition）、第 27 章（基数）、第 44 章（exemplar）与第 47 章（治理）。
