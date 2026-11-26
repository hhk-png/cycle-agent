# 附录 AD 自建 Exporter 开发指南

现成的 exporter 覆盖了操作系统、数据库、中间件和云服务的大部分需求。但内部系统、专有协议、业务指标通常需要自己写 exporter。本附录给出一套可复用的工程流程：先设计指标，再写采集，再做测试、打包和运维。

## AD.1 什么时候需要自建

适合自建：

- 内部服务/中间件没有官方 exporter；
- 需要把业务语义（订单、库存、队列深度）暴露为指标；
- 需要把专有协议/文件/API 转换成 Prometheus 指标；
- 需要在采集侧做聚合/缓存/限流。

不适合自建：

- 已经有成熟 exporter，只是"想统一"；
- 需要日志/追踪能力（用 OTel/日志系统）；
- 需要高频（亚秒级）采样（Prometheus 不是为这个设计的）；
- 需要强一致事务/计费（指标是近似值）。

先检查 [prometheus/exporters](https://github.com/prometheus/exporters)、[awesome-prometheus-alerts](https://github.com/samber/awesome-prometheus-alerts) 和厂商文档，避免重复造轮子。

## AD.2 设计指标模型

写代码前先填一张表：

| 指标名 | 类型 | 标签 | 单位 | 语义 | 基数估算 |
|---|---|---|---|---|---|
| `orders_created_total` | counter | `region`, `status` | 个 | 订单创建累计数 | 5×4=20 |
| `orders_processing_seconds` | histogram | `region` | 秒 | 处理耗时 | 5 |
| `inventory_items` | gauge | `warehouse`, `sku_class` | 个 | 当前库存 | 20×10=200 |
| `queue_depth` | gauge | `queue` | 条 | 当前积压 | 10 |

规则：

- counter 用 `_total` 后缀，只增不减；重置由客户端/重启处理；
- gauge 表示瞬时值，可增可减；
- histogram 用 `_bucket/_sum/_count`，桶要覆盖 SLO 边界；
- 标签值必须低基数、可枚举；不要把 ID、URL、错误文本放进标签；
- 单位放指标名（`_seconds`、`_bytes`），不要放标签；
- 同一指标不要混用多种单位。

基数估算要写进设计文档。一个"看起来无害"的 `path` 标签可能让序列数从 100 变成 100 万。

## AD.3 Go：client_golang

### AD.3.1 最小 exporter

```go
package main

import (
	"log"
	"net/http"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promhttp"
)

var (
	ordersCreated = prometheus.NewCounterVec(
		prometheus.CounterOpts{
			Name: "orders_created_total",
			Help: "Total number of created orders.",
		},
		[]string{"region", "status"},
	)
	queueDepth = prometheus.NewGaugeVec(
		prometheus.GaugeOpts{
			Name: "queue_depth",
			Help: "Current number of messages in the queue.",
		},
		[]string{"queue"},
	)
	processingSeconds = prometheus.NewHistogramVec(
		prometheus.HistogramOpts{
			Name:    "orders_processing_seconds",
			Help:    "Order processing latency in seconds.",
			Buckets: []float64{0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10},
		},
		[]string{"region"},
	)
)

func init() {
	prometheus.MustRegister(ordersCreated, queueDepth, processingSeconds)
}

func main() {
	http.Handle("/metrics", promhttp.Handler())
	http.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(http.StatusOK)
		_, _ = w.Write([]byte("ok"))
	})
	log.Fatal(http.ListenAndServe(":9100", nil))
}
```

### AD.3.2 自定义 Collector

当指标需要按请求现算（而不是在事件发生时累加），实现 `Collector`：

```go
type inventoryCollector struct {
	items *prometheus.Desc
}

func newInventoryCollector() *inventoryCollector {
	return &inventoryCollector{
		items: prometheus.NewDesc(
			"inventory_items",
			"Current inventory items.",
			[]string{"warehouse", "sku_class"}, nil,
		),
	}
}

func (c *inventoryCollector) Describe(ch chan<- *prometheus.Desc) {
	ch <- c.items
}

func (c *inventoryCollector) Collect(ch chan<- prometheus.Metric) {
	for _, row := range readInventory() { // 你的数据源
		ch <- prometheus.MustNewConstMetric(
			c.items, prometheus.GaugeValue, row.Value,
			row.Warehouse, row.SKUClass,
		)
	}
}
```

注册：

```go
prometheus.MustRegister(newInventoryCollector())
```

注意：

- `Collect` 会被每次抓取调用，必须快且有超时；
- 不要在 `Collect` 里做无界查询/全表扫描；
- 采集失败时暴露 `exporter_scrape_error`，而不是让整个 `/metrics` 500；
- 用 `promhttp.HandlerFor(registry, promhttp.HandlerOpts{...})` 控制并发/超时。

### AD.3.3 自带指标

每个 exporter 都应暴露自己的健康度：

```text
exporter_build_info{version="1.2.3",revision="abc"} 1
exporter_scrape_duration_seconds 0.42
exporter_scrape_error 0
exporter_last_success_timestamp_seconds 1700000000
```

这些指标能回答"exporter 自己是否正常"，避免把 exporter 故障误判为被监控系统故障。

## AD.4 Python：prometheus_client

```python
from prometheus_client import Counter, Gauge, Histogram, start_http_server

ORDERS = Counter(
    "orders_created_total",
    "Total number of created orders.",
    ["region", "status"],
)
QUEUE = Gauge("queue_depth", "Current queue depth.", ["queue"])
LATENCY = Histogram(
    "orders_processing_seconds",
    "Order processing latency.",
    ["region"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)


def handle_order(region: str, status: str, seconds: float) -> None:
    ORDERS.labels(region=region, status=status).inc()
    LATENCY.labels(region=region).observe(seconds)


if __name__ == "__main__":
    start_http_server(9100)
    while True:
        QUEUE.labels(queue="orders").set(read_queue_depth())
```

多进程（gunicorn/uwsgi）要用 `prometheus_client.multiprocess`：

```python
from prometheus_client import CollectorRegistry, multiprocess, generate_latest

registry = CollectorRegistry()
multiprocess.MultiProcessCollector(registry)
data = generate_latest(registry)
```

否则每个 worker 各记一份，指标会重复/不一致。环境变量 `PROMETHEUS_MULTIPROC_DIR` 要指向共享目录，并在启动前清理旧文件。

## AD.5 exposition 细节

### AD.5.1 文本格式

```text
# HELP orders_created_total Total number of created orders.
# TYPE orders_created_total counter
orders_created_total{region="cn-north",status="paid"} 1234
```

规则：

- HELP/TYPE 在样本之前；
- 同一指标名的 TYPE 只出现一次；
- 标签值要转义 `\`、`"`、换行；
- 结尾换行；
- 不要把时间戳写进文本（除非你明确要 `honor_timestamps`）。

### AD.5.2 OpenMetrics

OpenMetrics 增加 `# EOF`、exemplar、`# UNIT`、更严格的类型。抓取端通过 `Accept` 协商：

```text
Accept: application/openmetrics-text; version=1.0.0, text/plain; version=0.0.4
```

exemplar 示例：

```text
orders_processing_seconds_bucket{region="cn-north",le="0.5"} 100 # {trace_id="abc123"} 0.42 1700000000
```

exemplar 只保留少量样本，适合"指标 → 追踪"跳转，不是完整分布。

### AD.5.3 压缩与超时

- 大指标集用 gzip（`Content-Encoding: gzip`），抓取端默认支持；
- `/metrics` 要有超时/并发限制，避免慢客户端拖垮 exporter；
- 不要把 `/metrics` 和业务请求放在同一个无隔离的线程池；
- 返回 200 且内容完整；部分失败用 `exporter_scrape_error` 表达。

## AD.6 健康检查与自监控

```text
GET /healthz      → 200 表示进程存活（liveness）
GET /readyz       → 200 表示能采集（readiness）
GET /metrics      → 指标
```

不要把 `/metrics` 当健康检查：它可能因为采集慢而超时，导致 Pod 被杀。分开 liveness/readiness，并给 `/metrics` 单独的 timeout。

自监控告警：

```promql
# exporter 采集失败
exporter_scrape_error == 1

# 太久没有成功采集
time() - exporter_last_success_timestamp_seconds > 300

# 抓取变慢
exporter_scrape_duration_seconds > 5
```

## AD.7 测试

### AD.7.1 单元测试

Go：

```go
func TestOrdersCounter(t *testing.T) {
	reg := prometheus.NewRegistry()
	c := prometheus.NewCounterVec(prometheus.CounterOpts{
		Name: "orders_created_total",
		Help: "test",
	}, []string{"status"})
	reg.MustRegister(c)
	c.WithLabelValues("paid").Inc()
	// 用 testutil.CollectAndCompare 对比文本
}
```

Python：

```python
from prometheus_client import CollectorRegistry, generate_latest


def test_orders_counter():
    registry = CollectorRegistry()
    counter = Counter("orders_created_total", "test", ["status"], registry=registry)
    counter.labels(status="paid").inc()
    assert b'orders_created_total{status="paid"} 1.0' in generate_latest(registry)
```

### AD.7.2 格式与语义检查

```bash
curl -s http://localhost:9100/metrics | promtool check metrics
```

CI 中再加：

- golden file：固定输入 → 固定 exposition；
- 基数测试：构造最坏输入，断言序列数不超过预算；
- 并发测试：并行抓取不 panic/不死锁；
- 故障注入：数据源超时/返回错误时仍能返回 200 + `exporter_scrape_error`。

### AD.7.3 集成测试

起一个真实 Prometheus 抓取 exporter，断言：

```promql
up{job="my-exporter"} == 1
my_metric > 0
```

用 `testcontainers`/docker compose 或 pytest fixture 管理生命周期。

## AD.8 打包与部署

### AD.8.1 容器

```dockerfile
FROM golang:1.24 AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/exporter ./cmd/exporter

FROM gcr.io/distroless/static:nonroot
COPY --from=build /out/exporter /exporter
USER nonroot:nonroot
EXPOSE 9100
ENTRYPOINT ["/exporter"]
```

要点：

- 静态编译、非 root、只读根文件系统；
- 暴露 9100，但不要默认绑定 0.0.0.0（按需）；
- 加 `securityContext.readOnlyRootFilesystem`、`runAsNonRoot`；
- 资源 requests/limits 按采集量设置；
- 用 `PodMonitor`/`ServiceMonitor` 或 static config 抓取。

### AD.8.2 部署清单

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: inventory-exporter
spec:
  replicas: 1
  selector:
    matchLabels:
      app: inventory-exporter
  template:
    metadata:
      labels:
        app: inventory-exporter
    spec:
      containers:
        - name: exporter
          image: registry.example/inventory-exporter:1.2.3
          ports:
            - name: metrics
              containerPort: 9100
          readinessProbe:
            httpGet:
              path: /readyz
              port: metrics
          livenessProbe:
            httpGet:
              path: /healthz
              port: metrics
          resources:
            requests:
              cpu: 50m
              memory: 64Mi
            limits:
              cpu: 200m
              memory: 128Mi
```

抓取配置：

```yaml
scrape_configs:
  - job_name: inventory-exporter
    scrape_interval: 30s
    scrape_timeout: 10s
    static_configs:
      - targets: ["inventory-exporter:9100"]
    metric_relabel_configs:
      - source_labels: [__name__]
        regex: "go_.*"
        action: drop
```

## AD.9 性能、基数与安全

### AD.9.1 性能

- `Collect` 的时间应远小于 `scrape_timeout`；
- 缓存昂贵查询（例如数据库统计），用 TTL + 后台刷新；
- 不要在每次抓取时做全表扫描；
- 限制并发采集，避免抓取风暴打垮被监控系统；
- 大响应加 gzip，但不要压缩过头（CPU 换带宽要权衡）。

### AD.9.2 基数

- 标签值来自枚举/白名单，不来自用户输入；
- 对未知值做 `other` 桶；
- 定期统计每个指标的序列数；
- 在 exporter 侧加 `max_series` 保护，超过时丢弃并暴露错误。

### AD.9.3 安全

- exporter 常持有数据库/API 凭证：用 Secret 挂载，不要写进镜像；
- `/metrics` 可能泄露内部信息：加认证/网络策略；
- 防止 SSRF：不要让用户控制 exporter 请求的 URL；
- 不要执行用户输入；
- 依赖定期扫描漏洞；
- 只读账号，最小权限。

## AD.10 版本与兼容

- 遵循 SemVer；指标名/标签变更视为 breaking change；
- 删除指标前先标记 deprecated，保留至少一个发布周期；
- 在 `exporter_build_info` 暴露版本/commit；
- CHANGELOG 写清指标新增/删除/语义变化；
- 抓取配置与 dashboard 的变更要同步评审。

## AD.11 最小交付清单

```text
[ ] 指标设计表（名称/类型/标签/单位/基数）
[ ] /metrics、/healthz、/readyz
[ ] exporter_build_info、scrape_duration、scrape_error、last_success
[ ] 单元测试 + promtool check metrics + 集成测试
[ ] 容器镜像（非 root、静态、只读）
[ ] 部署清单 + 抓取配置 + 告警规则
[ ] 基数预算与压测结果
[ ] 凭证/网络/认证方案
[ ] CHANGELOG 与升级说明
[ ] Runbook（采集失败、数据源不可用、凭证过期）
```

## 延伸阅读

- Prometheus 官方 "Writing exporters" 与 "Instrumentation"。
- `prometheus/client_golang`、`prometheus/client_python` 文档。
- OpenMetrics 规范。
- 本教程第 5 章（exposition/客户端库）、附录 AA（exporter 生态）、附录 W（埋点示例）。
