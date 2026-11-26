# 第 50 章 综合实战（一）：从业务目标到监控设计

前面 49 章分别讲了语义、查询、存储、抓取、告警、规模化、Mini 实现和生产专题。这一章把这些零件装成一台机器：给定一个真实业务场景，从用户旅程出发，推导 SLI/SLO、指标模型、采集拓扑、存储保留、查询与告警，最后用 Mini-Prometheus 跑通端到端链路。

本章不引入新 API，也不假设你已经有生产集群。它是一张"设计图纸"：每一节都对应前面某几章的方法，并给出可以直接复制的配置与查询。如果你要把同一套方法迁移到支付、流媒体、SaaS 或 IoT，可对照附录 AH 的行业案例；LLM 推理场景见附录 AI。

## 50.1 场景与约束

假设你在维护一个订单系统：

```text
用户 → API Gateway → Order Service → Payment Service
                          │              │
                          ├→ Inventory   └→ Notify
                          └→ Notify
```

约束：

- 5 个核心服务，Kubernetes 部署，3 套环境（dev/staging/prod）；
- 每服务 20 个 Pod，滚动发布，版本每周 1～2 次；
- 业务目标：下单主链路月可用性 99.9%，支付成功率不低于 99.5%；
- 平台团队 2 人，既要搭监控，也要负责 on-call 工具链；
- 本地保留 15 天，远端保留 13 个月；
- 监控系统本身不能成为新的故障源。

明确非目标：

- 不把日志/追踪塞进 Prometheus（第 3 章）；
- 不做跨地域双活（第 19 章）；
- 不为每个业务团队建独立 Prometheus（先共享，规模上来再拆，第 19 章）。

先写清楚约束，后面的每个设计决策才有依据。没有约束的"最佳实践"通常只是别人的场景。

## 50.2 从用户旅程推导 SLI

监控设计的起点不是"有哪些指标"，而是"用户在做什么、什么算失败"。

下单主链路的用户旅程：

```text
浏览商品 → 加入购物车 → 提交订单 → 支付 → 收到确认
```

对每一段问三个问题：

1. 用户看到的成功是什么？
2. 失败如何被观测到？
3. 失败的比例/延迟阈值是多少？

### 50.2.1 SLI 表

| SLI | 定义（good / total） | 数据来源 | 目标 |
|---|---|---|---|
| 可用性 | 非 5xx 请求 / 全部请求 | Gateway 访问日志或 `http_requests_total` | 99.9% |
| 延迟 | `duration < 300ms` 的请求 / 全部请求 | Histogram | 99% |
| 支付成功率 | 支付成功 / 支付尝试 | Payment 业务 counter | 99.5% |
| 订单新鲜度 | 60s 内进入终态的订单 / 全部订单 | 订单事件时间戳 | 99% |
| 饱和度 | 队列深度 / 队列容量 | 队列 exporter | < 80% |

注意：

- **可用性在边缘测**：Gateway 的 5xx 才是用户看到的失败；服务内部的 5xx 可能被重试吞掉。
- **延迟用"好请求比例"**，不要直接对 P99 设阈值；P99 是结果，不是 SLI 本身（第 25 章）。
- **业务 SLI 要能和工程 SLI 对上**：支付成功率下降时，应有对应的错误率/延迟指标能解释。
- **分母要写清楚**：是否包含健康检查、静态资源、机器人流量？排除项要固定，否则 SLO 会漂移。

### 50.2.2 错误预算

99.9% 月可用性对应：

```text
30 天 × 24 小时 × 60 分钟 × (1 - 0.999) = 43.2 分钟/月
```

99.5% 支付成功率对应：

```text
假设每月 100 万次支付尝试，允许 5000 次失败
```

错误预算不是"可以随便故障 43 分钟"，而是**发布/变更的风险预算**：预算消耗速度决定是否暂停发布、是否 rollback、是否加派 on-call。

## 50.3 指标模型与基数预算

### 50.3.1 三层指标

| 层 | 方法 | 例子 | 用途 |
|---|---|---|---|
| 服务层 | RED | `http_requests_total`、`http_request_duration_seconds` | SLO、告警、面板 |
| 资源层 | USE | `container_cpu_usage_seconds_total`、`node_memory_MemAvailable_bytes` | 容量、饱和度 |
| 业务层 | 事件计数 | `orders_created_total`、`payments_succeeded_total` | 业务 SLI、对账 |

不要把所有指标都做成业务指标；也不要用资源指标冒充用户体验。三层各司其职。

### 50.3.2 命名与标签

统一命名：

```text
<namespace>_<subsystem>_<name>_<unit>[_total]
```

示例：

```text
http_requests_total{service="order",route="/orders/{id}",method="POST",status="500"}
http_request_duration_seconds_bucket{service="order",route="/orders/{id}",method="POST",le="0.3"}
orders_created_total{service="order",region="cn-north",status="paid"}
payments_attempts_total{service="payment",provider="stripe",status="failed"}
```

标签规范：

| 标签 | 基数 | 说明 |
|---|---|---|
| `job` | 每类目标 1 个 | 抓取层自动附加 |
| `instance` | Pod/IP 级 | 抓取层自动附加 |
| `service` | 5 | 稳定服务名 |
| `version` | 每服务 2～3 | 发布版本，低基数 |
| `region` | 2～3 | 区域 |
| `route` | 20～50 | **模板化路径**，不能带 ID |
| `method` | 5 | HTTP 方法 |
| `status` | 6～10 | 状态码/枚举 |
| `env` | 3 | 环境 |

禁止进入标签：

```text
order_id, user_id, request_id, session_id, trace_id,
完整 URL（带查询参数）, 错误文本, Pod UID
```

`trace_id` 应进入 exemplar（第 44 章），而不是标签。

### 50.3.3 基数预算

粗算一条服务指标的序列数：

```text
5 services × 20 pods × 3 env × 20 routes × 5 methods × 6 statuses = 180,000
```

这已经接近单机 Prometheus 的舒适上限。压缩维度：

- `route` 只保留核心 10 条，其余归为 `route="other"`；
- `method` 只区分 GET/POST，其余归为 `other`；
- `status` 用 `2xx/3xx/4xx/5xx` 而不是每个具体码；
- Pod 级 `instance` 保留（排障需要），但不要额外加 `pod_ip`、`node_ip`、`container_id`。

压缩后：

```text
5 × 20 × 3 × 10 × 2 × 5 = 30,000
```

再叠加 histogram 的桶数：

```text
30,000 × 12 个桶 = 360,000 条序列
```

所以 histogram 的桶数也是基数预算的一部分。桶设计见第 4 章与附录 W；不要用默认的几十个桶覆盖微秒到小时。

给每个服务设预算并监控：

```promql
# 每个 service 的活跃序列数（需要 Prometheus 自监控指标）
sum by (job) (prometheus_tsdb_head_series)
```

更细的按指标名统计需要 `promtool tsdb analyze` 或远端存储的 series API（第 27 章）。

## 50.4 采集设计

### 50.4.1 抓取间隔

| 目标 | 间隔 | 超时 | 理由 |
|---|---|---|---|
| 核心服务 `/metrics` | 15s | 5s | SLO 告警需要足够分辨率 |
| KSM | 30s | 10s | 对象状态变化不需要 15s |
| node-exporter | 15s | 5s | 资源饱和度 |
| blackbox 探针 | 30s | 10s | 外部可用性 |
| 批处理 Pushgateway | 60s | 10s | 任务结束后最后值 |
| 控制面 | 30s | 10s | 变化慢、指标多 |

抓取间隔与 `rate()` 窗口的关系：

```text
窗口内至少 2 个样本；推荐窗口 ≥ 4 × scrape_interval
15s 抓取 → rate(...[1m]) 是下限，[2m] 更稳
```

第 7 章与第 12 章解释了外推和边界；不要用 `[15s]` 这种刚好一个点的窗口。

### 50.4.2 服务发现

Kubernetes 内用 ServiceMonitor（第 24 章、附录 P）：

```yaml
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
metadata:
  name: order-service
  namespace: monitoring
  labels:
    release: kube-prometheus-stack
spec:
  namespaceSelector:
    matchNames: ["prod"]
  selector:
    matchLabels:
      app: order
  endpoints:
    - port: metrics
      path: /metrics
      interval: 15s
      scrapeTimeout: 5s
      relabelings:
        - sourceLabels: [__meta_kubernetes_pod_name]
          targetLabel: pod
        - sourceLabels: [__meta_kubernetes_namespace]
          targetLabel: namespace
      metricRelabelings:
        - sourceLabels: [__name__]
          regex: "go_.*|process_.*"
          action: drop
```

集群外用 `file_sd` 或 `static_configs`（第 11 章）：

```yaml
scrape_configs:
  - job_name: external-blackbox
    file_sd_configs:
      - files: ["/etc/prometheus/targets/blackbox/*.json"]
        refresh_interval: 30s
    metrics_path: /probe
    params:
      module: [http_2xx]
    relabel_configs:
      - source_labels: [__address__]
        target_label: __param_target
      - source_labels: [__param_target]
        target_label: instance
      - target_label: __address__
        replacement: blackbox-exporter:9115
```

### 50.4.3 批处理

短生命周期任务用 Pushgateway（附录 T），但要设置清理策略：

```bash
# 任务结束时推送最后值
echo "batch_last_success_timestamp_seconds $(date +%s)" | \
  curl --data-binary @- http://pushgateway:9091/metrics/job/nightly_billing/instance/batch-01
```

风险：

- Pushgateway 不会自动让指标消失；任务删除后必须 DELETE 对应分组；
- `push_time_seconds` 用于判断"最后推送时间"，不是任务执行时间；
- 不要把高频业务指标推到 Pushgateway，它只是最后值缓存。

### 50.4.4 限制与保护

```yaml
scrape_configs:
  - job_name: order
    sample_limit: 50000
    label_limit: 30
    label_name_length_limit: 200
    label_value_length_limit: 500
    body_size_limit: 50MB
```

这些限制防止一个坏 exporter 拖垮整个 Prometheus。触发时会记录 `scrape_samples_post_metric_relabeling` 与 `up`，并产生抓取错误（第 12 章）。

## 50.5 存储、保留与远程写

### 50.5.1 本地保留

这些是**启动参数**，不是 `prometheus.yml` 字段：

```bash
prometheus \
  --storage.tsdb.path=/var/lib/prometheus \
  --storage.tsdb.retention.time=15d \
  --storage.tsdb.retention.size=50GB \
  --storage.tsdb.min-block-duration=2h
```

建议：

- `retention.time` 覆盖最长查询窗口与告警恢复时间；
- `retention.size` 是磁盘保护，不要等到磁盘满才删；
- `min-block-duration` 影响 compaction 与查询；默认 2h，不要随意调大；
- 这些参数修改通常需要重启；3.15 的 `storage.tsdb.chunk_encoding` 是少数可 reload 的存储配置，见第 22 章。

### 50.5.2 远程写

```yaml
remote_write:
  - url: https://mimir.example/api/v1/push
    name: primary
    remote_timeout: 30s
    queue_config:
      capacity: 10000
      max_shards: 50
      max_samples_per_send: 2000
      batch_send_deadline: 5s
      min_backoff: 30ms
      max_backoff: 5s
    write_relabel_configs:
      - source_labels: [__name__]
        regex: "go_.*|process_.*"
        action: drop
```

关键点：

- `queue_config` 的容量与分片要按样本速率调，不要用默认值硬扛百万样本/秒（第 18、22 章）；
- `write_relabel_configs` 可以在发送前丢弃低价值指标，降低远端成本；
- external labels 用于 HA 去重与集群区分：

```yaml
global:
  external_labels:
    cluster: prod-cn
    region: cn-north
    replica: a
```

### 50.5.3 容量估算

```text
活跃序列        20,000
抓取间隔        15s
样本/秒         20,000 / 15 ≈ 1,333
样本/天         1,333 × 86,400 ≈ 115,000,000
压缩后字节/样本  ~1.5–2.5（含索引/块开销）
原始日增        ~0.2–0.3 GB
15 天本地       ~3–5 GB（压缩后，含 WAL/临时空间另计）
13 个月远端     ~80–120 GB（取决于降采样与保留）
```

这是量级估算，不是承诺。真实值用 `promtool tsdb analyze`、`prometheus_tsdb_compaction_chunk_size_bytes` 和远端存储的用量指标校准（附录 F、Y）。

## 50.6 查询与记录规则

### 50.6.1 关键查询

可用性（边缘）：

```promql
sum(rate(http_requests_total{job="gateway",status!~"5.."}[5m]))
/
sum(rate(http_requests_total{job="gateway"}[5m]))
```

延迟（好请求比例）：

```promql
sum(rate(http_request_duration_seconds_bucket{job="gateway",le="0.3"}[5m]))
/
sum(rate(http_request_duration_seconds_count{job="gateway"}[5m]))
```

支付成功率：

```promql
sum(rate(payments_attempts_total{status="succeeded"}[5m]))
/
sum(rate(payments_attempts_total[5m]))
```

饱和度：

```promql
max by (queue) (queue_depth / queue_capacity)
```

业务量：

```promql
sum(rate(orders_created_total{status="paid"}[5m]))
```

### 50.6.2 记录规则

命名约定：

```text
level:metric:operation
```

示例：

```yaml
groups:
  - name: slo
    interval: 30s
    rules:
      - record: job:http_requests:rate5m
        expr: sum by (job) (rate(http_requests_total[5m]))

      - record: job:http_request_errors:ratio5m
        expr: |
          sum by (job) (rate(http_requests_total{status=~"5.."}[5m]))
          /
          sum by (job) (rate(http_requests_total[5m]))

      - record: job:http_request_duration:good_ratio5m
        expr: |
          sum by (job) (rate(http_request_duration_seconds_bucket{le="0.3"}[5m]))
          /
          sum by (job) (rate(http_request_duration_seconds_count[5m]))
```

原则：

- 记录规则只做"可复用的中间结果"，不要把整个面板逻辑都塞进规则；
- 规则组之间不要形成长依赖链；依赖层次控制在 2～3 层；
- 规则求值间隔与 `for` 配合：`for: 10m` 的规则不要用 1m 求值间隔制造噪音；
- 需要 `smoothed` 时设置 `query_offset`（第 20 章、附录 AE）。

### 50.6.3 查询性能

- 面板查询限定时间范围，不要默认 30 天；
- 高基数 `by` 维度先做 recording rule；
- 避免在告警里用 `topk` 后再聚合；
- 大范围 `query_range` 用降采样/长期存储，不要直接打单机（第 17、22 章）。

## 50.7 告警设计

### 50.7.1 症状告警优先

| 告警 | 类型 | 表达式 | 级别 |
|---|---|---|---|
| 下单可用性快速燃烧 | 症状 | 多窗口 burn rate | page |
| 支付成功率下降 | 症状 | 业务 SLI | page |
| 延迟 SLO 燃烧 | 症状 | 好请求比例 | page |
| 磁盘 4 天满 | 原因/容量 | 预测 | ticket |
| 证书 30 天过期 | 原因 | `probe_ssl_earliest_cert_expiry` | ticket |
| 目标 down | 原因 | `up == 0` | ticket（核心目标 page） |

不要给每个 `up == 0` 都 page；核心链路的目标 down 才 page，其余进工单。

### 50.7.2 多窗口 Burn Rate

```yaml
groups:
  - name: slo-burn
    interval: 1m
    rules:
      - alert: CheckoutAvailabilityBudgetBurnFast
        expr: |
          (
            job:http_request_errors:ratio5m{job="gateway"} > (14.4 * 0.001)
          )
          and
          (
            job:http_request_errors:ratio1h{job="gateway"} > (14.4 * 0.001)
          )
        for: 2m
        labels:
          severity: page
          slo: checkout-availability
        annotations:
          summary: "checkout availability is burning budget fast"
          runbook_url: "https://runbooks.example/checkout-availability"

      - alert: CheckoutAvailabilityBudgetBurnSlow
        expr: |
          (
            job:http_request_errors:ratio30m{job="gateway"} > (6 * 0.001)
          )
          and
          (
            job:http_request_errors:ratio6h{job="gateway"} > (6 * 0.001)
          )
        for: 15m
        labels:
          severity: ticket
          slo: checkout-availability
        annotations:
          summary: "checkout availability is burning budget slowly"
```

窗口与倍数的选择见第 25 章；核心是"短窗口确认紧迫性，长窗口确认持续性"。

### 50.7.3 路由与抑制

```yaml
route:
  receiver: default
  group_by: [alertname, service, slo]
  routes:
    - matchers: ['severity="page"']
      receiver: pager
      continue: false
    - matchers: ['severity="ticket"']
      receiver: ticketing
inhibit_rules:
  - source_matchers: ['severity="page"']
    target_matchers: ['severity="ticket"']
    equal: [service, slo]
```

原则：

- 同一服务同一 SLO 的 page 抑制 ticket；
- `group_by` 不要带 `instance`/`pod`，否则无法分组；
- 静默要有到期时间与 owner；
- 路由和抑制规则要纳入 Git 与测试（第 10 章、附录 N）。

### 50.7.4 Runbook

每个 page 告警必须有 runbook，至少包含：

```text
含义：用户在哪个旅程受影响
影响：SLO/收入/范围
立即检查：/targets、关键面板、最近发布、依赖状态
止血：回滚/降级/切流/扩容
根因：指标/日志/追踪入口
升级：何时找谁
恢复验证：SLI 恢复、队列清空、无数据缺口
```

没有 runbook 的 page 告警，等同于把压力转嫁给值班人。

## 50.8 用 Mini-Prometheus 跑通端到端

下面用一个最小配置复现上面的核心链路。它不需要 Kubernetes，也不需要真实 exporter。

### 50.8.1 配置

```yaml
global:
  scrape_interval: 5s
  scrape_timeout: 2s
  evaluation_interval: 5s
  external_labels:
    cluster: capstone-lab
    region: local

scrape_configs:
  - job_name: demo
    static_configs:
      - targets: ["127.0.0.1:9100"]
        labels:
          service: checkout
          env: lab
    metric_relabel_configs:
      - source_labels: [__name__]
        regex: "go_.*|process_.*"
        action: drop

  - job_name: mini-prometheus
    static_configs:
      - targets: ["127.0.0.1:9090"]

rule_files:
  - "../rules/capstone.yml"
```

### 50.8.2 规则

```yaml
groups:
  - name: capstone
    interval: 5s
    rules:
      - record: job:demo_requests:rate1m
        expr: sum by (job) (rate(demo_http_requests_total[1m]))

      - record: job:demo_request_errors:ratio1m
        expr: |
          sum by (job) (rate(demo_http_requests_total{code=~"5.."}[1m]))
          /
          sum by (job) (rate(demo_http_requests_total[1m]))

      - alert: DemoCheckoutAvailabilityBurn
        expr: job:demo_request_errors:ratio1m{job="demo"} > 0.2
        for: 10s
        labels:
          severity: page
          slo: checkout-availability
        annotations:
          summary: "demo checkout 5xx ratio is {{ $value }}"
          runbook_url: "https://runbooks.example/demo-checkout"
```

### 50.8.3 启动与验证

```bash
# 终端一：demo exporter
python examples/demo_exporter.py --port 9100

# 终端二：Mini-Prometheus
python -m mini_prometheus \
  --config.file=examples/config/capstone.yml \
  --storage.tsdb.path=./data-capstone \
  --web.listen-address=127.0.0.1:9090

# 终端三：验证
curl -s 'http://127.0.0.1:9090/api/v1/query?query=up' | python -m json.tool
curl -s 'http://127.0.0.1:9090/api/v1/query?query=job:demo_requests:rate1m' | python -m json.tool
curl -s 'http://127.0.0.1:9090/api/v1/rules' | python -m json.tool
curl -s 'http://127.0.0.1:9090/api/v1/alerts' | python -m json.tool
```

预期：

- `up{job="demo"}` 为 1；
- `job:demo_requests:rate1m` 有值；
- `/api/v1/rules` 中规则 `health` 为 `ok`；
- 注入 5xx 后，`/api/v1/alerts` 出现 `DemoCheckoutAvailabilityBurn` 的 pending/firing。

### 50.8.4 规则单元测试

```yaml
rule_files:
  - capstone.yml
evaluation_interval: 5s
tests:
  - interval: 5s
    input_series:
      - series: 'demo_http_requests_total{job="demo",code="200"}'
        values: '0+100x20'
      - series: 'demo_http_requests_total{job="demo",code="500"}'
        values: '0+10x20'
    promql_expr_test:
      - expr: job:demo_request_errors:ratio1m
        eval_time: 1m
        exp_samples:
          - labels: 'job:demo_request_errors:ratio1m{job="demo"}'
            value: 0.09090909090909091
```

运行：

```bash
python -m mini_prometheus.promtool test rules examples/rules/capstone_test.yml
```

Mini 的规则测试器支持 `promql_expr_test` 与告警断言；完整语义与上游 `promtool` 的差异见第 42 章与附录 AC。

### 50.8.5 Mini 的边界

这个实验覆盖了"抓取 → 存储 → 规则 → 告警 → API"的主链路，但没有覆盖：

- Kubernetes SD/ServiceMonitor；
- 多副本 HA 与去重；
- remote write 到真实长期存储；
- 原生直方图/OTel；
- 生产级性能和故障恢复。

这些在第 18～24、43～49 章与附录 P/Q/R 中展开。Mini 的价值是让你在几分钟内看到语义闭环，而不是替代生产验证。

## 50.9 设计评审清单

```text
[ ] SLI 是否从用户旅程出发，而不是从现有指标出发？
[ ] 每个 SLI 的 good/total 定义是否写清楚，分母是否固定？
[ ] 指标名/单位/类型是否符合规范？
[ ] 标签是否可枚举、可聚合、无 PII/ID/自由文本？
[ ] 基数预算是否按服务/指标分配，是否有告警？
[ ] histogram 桶是否覆盖 SLO 阈值，桶数是否计入预算？
[ ] 抓取间隔与 rate 窗口是否匹配？
[ ] 是否有限制（sample_limit/label_limit/body_size_limit）？
[ ] 本地保留与远端保留是否覆盖查询/合规需求？
[ ] remote write 队列是否按样本速率配置？
[ ] 记录规则是否只做可复用中间结果，依赖层次是否可控？
[ ] 告警是否症状优先、可行动、有 owner、有 runbook？
[ ] 路由/抑制/静默是否纳入 Git 与测试？
[ ] 是否有容量/成本/故障演练计划？
```

最可能先出问题的三处：

1. **histogram 桶数与标签组合相乘**，序列数远超预算；
2. **Pushgateway 残留**，批任务删除后指标永久存在；
3. **告警没有 runbook/owner**，第一次真实故障就变成噪音。

## 本章小结

- 监控设计从用户旅程和 SLI 出发，而不是从可用指标出发。
- 指标分服务/资源/业务三层，标签要可聚合、低基数、无 PII。
- 基数预算要把 histogram 桶数算进去，并为每个服务设上限。
- 采集拓扑要匹配抓取间隔、服务发现方式和目标生命周期。
- 本地保留 + 远端长期存储是常见组合，队列与 external labels 要提前设计。
- 告警以症状为主、多窗口 burn rate、路由/抑制/runbook 缺一不可。
- Mini-Prometheus 可以在几分钟内跑通"抓取 → 规则 → 告警 → API"的语义闭环。

## 延伸阅读

- 本教程第 3 章（数据模型）、第 4 章（指标类型）、第 9～12 章（规则/告警/发现/抓取）。
- 本教程第 22 章（容量）、第 25 章（SLO）、第 26 章（告警工程）、第 27 章（基数）。
- 本教程第 47 章（指标治理）、附录 C（规则库）、附录 F（容量模型）、附录 Z（治理清单）。
- 第 51 章（综合实战二）把本章设计推进到平台化、压测、演练与复盘。
