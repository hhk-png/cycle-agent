# 第 32 章 可观测性：Trace、Metric、Log 与 API 分析

## 32.1 可观测性不是“多打日志”

可观测性回答：**系统内部发生了什么，为什么？**

它至少包括：

- 指标（Metrics）：聚合趋势；
- 日志（Logs）：离散事件；
- 追踪（Traces）：请求链路；
- 事件（Events）：部署、配置变更、告警；
- 剖析（Profiles）：资源热点。

API 可观测性还要回答业务问题：

- 哪个租户/端点/版本最慢？
- 哪个错误码在上升？
- 哪个客户端版本还在调用旧接口？
- 限流是否误伤？
- 重试是否放大故障？
- 一次请求经过了哪些服务？
- 哪个字段导致响应变大？
- 哪个租户的用量异常？

### 可观测性 vs 监控

- 监控：你预先知道要看的指标；
- 可观测性：你可以回答未预先定义的问题。

API 设计应让系统“可被提问”：

- 每个请求有 ID；
- 每个操作有 span；
- 每个错误有 code；
- 每个资源有租户；
- 每个响应有大小；
- 每个依赖有延迟。

## 32.2 黄金信号与三套信号框架

### RED

适合请求驱动服务：

- **Rate**：请求速率；
- **Errors**：错误率；
- **Duration**：延迟分布。

### USE

适合资源：

- **Utilization**：使用率；
- **Saturation**：饱和度；
- **Errors**：错误。

### 四个黄金信号

Google SRE 提出：

- 延迟；
- 流量；
- 错误；
- 饱和度。

## 32.3 Metrics

### 关键指标

```text
http_server_requests_total{method,route,status,tenant_tier}
http_server_request_duration_seconds{method,route,status}
http_server_active_requests{route}
http_server_response_size_bytes{route}
http_client_requests_total{peer,status}
http_client_request_duration_seconds{peer}
rate_limit_exceeded_total{tenant_tier,route}
idempotency_replay_total{endpoint}
```

### 标签基数

高基数标签（用户 ID、请求 ID、完整 URL）会炸掉时序数据库。

建议：

- 路由模板而非完整路径：`/orders/{id}`；
- 租户分层（free/pro/enterprise）而非每个租户；
- 错误码而非错误消息；
- 限制标签数量；
- 使用日志/追踪查个体。

### 基数估算

```text
method (5) × route (100) × status (20) × tenant_tier (3) = 30,000 序列
```

如果加 `tenant_id`（10,000），变成 3 亿序列——监控系统会崩溃。

### 直方图

延迟应使用直方图，而不是平均值：

```text
http_server_request_duration_seconds_bucket{le="0.1"} 12345
...
http_server_request_duration_seconds_bucket{le="+Inf"} 13000
```

关注 P50/P95/P99/P99.9，而不是平均。

### 摘要 vs 直方图

- Summary：客户端计算分位数，不能聚合；
- Histogram：服务端分桶，可聚合，适合多实例。

### Exemplars

直方图可以关联 trace：

```text
http_server_request_duration_seconds_bucket{le="0.5"} 1234 # {trace_id="..."}
```

从慢请求指标直接跳到 Trace。

## 32.4 Traces

一个 Trace 表示一次端到端请求：

```text
Trace: req_123
├── HTTP GET /orders/ord_123        120ms
│   ├── auth.validate                8ms
│   ├── db.query orders             35ms
│   ├── db.query items              20ms
│   └── http customer-service       45ms
└── ...
```

### OpenTelemetry

统一 API/SDK/协议：

```python
from opentelemetry import trace

tracer = trace.get_tracer("orders")

with tracer.start_as_current_span("get_order") as span:
    span.set_attribute("order.id", order_id)
    span.set_attribute("tenant.id", tenant_id)
    result = repo.get(order_id)
    span.set_attribute("order.status", result.status)
```

### Span 属性

- `http.request.method`；
- `url.path`；
- `http.response.status_code`；
- `server.address`；
- `tenant.id`；
- `enduser.id`（注意隐私）；
- `error.type`；
- `messaging.system`；
- `db.system`；
- `db.operation`。

### 上下文传播

- W3C `traceparent`；
- `tracestate`；
- Baggage；
- 跨服务、跨队列、跨异步任务。

异步任务要：

- 在消息中携带 trace context；
- 消费者创建新 span；
- 关联到原 trace。

### 采样

- 头部采样：入口决定；
- 尾部采样：根据错误/延迟决定；
- 比例采样；
- 关键租户全采样；
- 避免采样偏差导致误判。

### 尾部采样的价值

只保留：

- 错误请求；
- 慢请求；
- 关键租户；
- 异常路径。

可以大幅降低存储成本，同时保留最有价值的 Trace。

## 32.5 Logs

### 结构化日志

```json
{
  "timestamp": "2026-11-26T10:15:30.123Z",
  "level": "error",
  "service": "orders",
  "route": "/orders/{id}",
  "status": 503,
  "errorCode": "DEPENDENCY_UNAVAILABLE",
  "requestId": "req_123",
  "traceId": "...",
  "tenantId": "t_123",
  "message": "payment dependency timeout"
}
```

### 原则

- 结构化；
- 包含 trace/request ID；
- 不记录敏感数据；
- 分级；
- 采样；
- 保留策略；
- 可查询；
- 字段稳定。

### 不要记录

- 密码；
- 完整卡号；
- 令牌；
- 身份证号；
- 完整请求体（除非脱敏）；
- 个人信息；
- 加密密钥；
- 内部堆栈（对外）。

### 日志级别

- DEBUG：开发调试；
- INFO：关键业务事件；
- WARN：可恢复异常；
- ERROR：需要关注；
- FATAL：服务不可用。

生产环境通常 INFO 起步，避免 DEBUG 洪水。

## 32.6 关联

- `requestId`：单次请求；
- `traceId`：端到端链路；
- `spanId`：单个操作；
- `tenantId`：租户；
- `idempotencyKey`：幂等；
- `eventId`：事件；
- `jobId`：异步任务。

这些 ID 应贯穿日志、指标、追踪、错误响应和支持工单。

### 错误响应中的关联

```json
{
  "type": "https://errors.example.com/dependency",
  "title": "依赖服务不可用",
  "status": 503,
  "requestId": "req_123",
  "traceId": "4bf92f3577b34da6a3ce929d0e0e4736"
}
```

支持人员只需一个 ID，就能找到完整链路。

## 32.7 SLO

SLO 是可靠性目标：

```text
可用性：99.95%（对应 0.05% 错误预算）
延迟：P95 < 300ms
错误率：< 0.05%（与可用性同一分母时）
数据新鲜度：< 5s
```

### SLI 定义

```text
SLI = 成功请求数 / 有效请求总数
```

需要明确：

- 什么算成功？
- 哪些请求计入分母？
- 客户端错误是否计入？
- 限流是否计入？
- 维护窗口？
- 健康检查？
- 异步任务？

### 错误预算

```text
30 天 99.95% → 允许 0.05% 不可用 ≈ 21.6 分钟
```

错误预算用于：

- 发布决策；
- 稳定性投入；
- 告警阈值。

### 错误预算的计算

```text
每月分钟数 = 30 × 24 × 60 = 43,200
99.95% → 不可用 0.05% × 43,200 = 21.6 分钟
99.9%  → 43.2 分钟
99.99% → 4.32 分钟
```

## 32.8 告警

好的告警：

- 基于症状（用户可见）；
- 可操作；
- 有 runbook；
- 避免噪音；
- 分级；
- 抑制/聚合。

不要：

- 每个 500 都告警；
- 只基于 CPU；
- 没有上下文；
- 没有负责人；
- 告警风暴。

### 告警分级

- P0：全站不可用；
- P1：核心功能不可用；
- P2：部分用户/功能受影响；
- P3：需要关注，不影响用户。

### 告警疲劳

- 告警太多 → 被忽略；
- 阈值太敏感 → 噪音；
- 没有 runbook → 无法处理；
- 没有负责人 → 无人处理。

## 32.9 API 分析

业务/产品指标：

- 每个端点的调用量；
- 每个客户端的版本分布；
- 每个租户的用量；
- 错误码分布；
- 首次调用时间；
- 弃用端点使用量；
- SDK 版本；
- 地理分布；
- 限流/配额命中；
- 从注册到首次调用的时间；
- 每个客户的留存/扩展；
- 实验变体与首次曝光（按变体对比成功率、延迟与业务指标）；
- 开关生效版本与翻转时间（用于解释指标突变）；
- 沙箱的 TTFC、错误分布与到生产的转化。

开关与实验的指标不能只看平均值：必须按变体、租户、地区、客户端版本拆分，并同时看护栏指标。开关翻转和实验曝光要能和 Trace/日志关联，否则事故复盘时无法回答“这次变化是谁在什么时候打开的”。具体方法见[附录 AK](../appendices/ak-feature-flags-experiments-progressive-delivery.md)；沙箱度量见[附录 AJ](../appendices/aj-sandbox-mock-test-data.md)。

这些数据驱动：

- 产品决策；
- 弃用；
- 容量规划；
- 定价；
- 支持。

## 32.10 隐私与合规

- 最小化采集；
- 脱敏；
- 聚合；
- 保留期；
- 访问控制；
- 数据驻留；
- 用户同意；
- 删除权。

可观测性系统本身也可能成为泄露源。

### 敏感数据

- 请求体；
- 响应体；
- 头部；
- 查询参数；
- 用户 ID；
- IP；
- 位置；
- 设备标识。

对敏感字段：

- 不采集；
- 哈希；
- 截断；
- 脱敏；
- 加密。

## 32.11 工具

- OpenTelemetry Collector；
- Prometheus；
- Grafana；
- Jaeger/Tempo；
- Loki；
- Elasticsearch/OpenSearch；
- Datadog/New Relic；
- Honeycomb；
- Sentry。

选择取决于规模、成本、团队能力。

### 工具链组合

```text
应用 → OTel SDK → Collector → Prometheus/Tempo/Loki → Grafana
```

Collector 可以做：

- 批处理；
- 采样；
- 脱敏；
- 路由；
- 重试；
- 多后端。

## 32.12 反模式

- 只有日志，没有指标/追踪；
- 高基数标签；
- 记录敏感数据；
- 平均延迟而不是分位数；
- 告警无 runbook；
- SLO 没有 SLI；
- 采样导致错误不可见；
- 追踪不跨服务；
- 没有请求 ID；
- 监控只覆盖基础设施；
- 追踪只覆盖入口，不覆盖数据库/消息；
- 日志没有结构化。

## 32.13 本章小结

- 可观测性包括指标、日志、追踪、事件、剖析。
- RED/USE/黄金信号提供起点。
- 直方图与分位数比平均值更有意义。
- Trace 要跨服务传播上下文。
- 日志要结构化、脱敏、可关联。
- SLO/错误预算驱动发布与告警。
- API 分析连接技术与商业。
- 可观测性本身需要隐私和成本治理。
- 用“监控为什么没拦住”检查告警设计，可参考[附录 U](../appendices/u-incident-postmortems.md) 的事故复盘。

## 设计练习

为一个“多租户订单 API”设计可观测性：

- 关键指标；
- Trace 属性；
- 日志字段；
- SLO；
- 告警；
- 租户维度分析；
- 隐私保护。

写出仪表盘布局与告警规则。然后回答：

1. 哪些指标不能用租户 ID 做标签？
2. 如何从慢请求指标跳到 Trace？
3. 错误预算耗尽时，发布策略如何变化？
4. 如何发现某个客户端版本仍在调用旧接口？

---

上一章：[API 测试：从单元到契约、模糊与混沌](31-testing.md)  
下一章：[性能与容量：尾延迟、吞吐与成本](33-performance.md)
