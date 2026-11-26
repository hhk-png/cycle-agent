# 附录 AG OTLP 接收与 Delta Temporality 实战

> 第 20 章从概念上比较了 Prometheus 与 OpenTelemetry 的数据模型；本附录进入 OTLP 接收端的协议、名称翻译、资源属性、delta→cumulative 状态机，以及 Mini-Prometheus 的可运行实现。重点不是"把 OTLP 转成 Prometheus"，而是**在转换中保住身份、时间性和基数边界**。

## AG.1 两条接入路径

OpenTelemetry 指标进入 Prometheus 有两条常见路径：

```text
应用 --OTLP--> Collector --prometheusremotewrite/otlphttp--> Prometheus
应用 --OTLP-------------------------------→ Prometheus OTLP receiver
```

| 维度 | Collector 中转 | Prometheus 直收 |
|---|---|---|
| 转换位置 | Collector processor/exporter | Prometheus receiver |
| 灵活性 | 高（过滤、重命名、批量、重试） | 低（只做内建翻译） |
| 运维面 | 多一个组件 | 少一个组件 |
| 适合 | 生产默认、跨团队、需要治理 | 小规模、边缘、快速验证 |

直收的启动方式：

```bash
prometheus \
  --config.file=prometheus.yml \
  --web.enable-otlp-receiver
```

端点：

```text
POST /api/v1/otlp/v1/metrics
Content-Type: application/x-protobuf   # 常见
Content-Type: application/json         # OTLP/HTTP 规范允许，支持度随版本
```

Mini 只实现 `application/json`；发送 OTLP/protobuf 会得到明确的 400，而不是静默丢数据。

## AG.2 翻译策略与开关

Prometheus 3.x 用配置文件的 `otlp:` 块选择指标名翻译方式与资源属性提升：

```yaml
otlp:
  translation_strategy: UnderscoreEscapingWithSuffixes  # 上游默认
  promote_resource_attributes: []                       # 默认不提升
  keep_identifying_resource_attributes: false           # 默认不写进 target_info
```

| 策略 | 行为 | 适用 |
|---|---|---|
| `UnderscoreEscapingWithSuffixes` | 非 `[a-zA-Z0-9_:]` 字符替换为 `_`，再追加后缀；**上游默认** | 兼容旧后端/旧 Dashboard |
| `NoUTF8EscapingWithSuffixes` | 保留 UTF-8 名字，追加单位/类型后缀 | 全链路支持 UTF-8 |
| `NoTranslation` | 不改名、不加后缀（版本相关） | 由后端自行处理 |
| `UnderscoreEscapingWithoutSuffixes` | 只做下划线转义，不加单位/`_total` | 特殊迁移场景 |

Mini 的对应实现是 CLI flag `--storage.otlp.translation-strategy`，默认 `NoUTF8EscapingWithSuffixes`；这是教学实现的简化，**不是上游默认**。Mini 也没有 `otlp.promote_resource_attributes`/`target_info` 机制（见 AG.5）。

与 delta 相关的特性开关：

| 开关 | 作用 |
|---|---|
| `--enable-feature=otlp-deltatocumulative` | 在接收端把 delta 转成 cumulative |
| `--enable-feature=otlp-native-delta-ingestion` | 保留 delta 语义，交给支持 delta 的后端 |

默认行为是**拒绝或丢弃 delta 指标**，因为 Prometheus 的 counter 语义是 cumulative，`rate()`/`increase()` 依赖"累计值 + reset 检测"。不要假设"开了 OTLP receiver 就自动兼容 delta"。

## AG.3 一个 OTLP/JSON 请求的骨架

```json
{
  "resourceMetrics": [
    {
      "resource": {
        "attributes": [
          {"key": "service.name", "value": {"stringValue": "checkout"}},
          {"key": "service.instance.id", "value": {"stringValue": "checkout-0"}},
          {"key": "deployment.environment", "value": {"stringValue": "prod"}}
        ]
      },
      "scopeMetrics": [
        {
          "scope": {"name": "checkout.http", "version": "1.4.0"},
          "metrics": [
            {
              "name": "http.server.requests",
              "unit": "1",
              "sum": {
                "aggregationTemporality": 2,
                "isMonotonic": true,
                "dataPoints": [
                  {
                    "timeUnixNano": "1700000000000000000",
                    "asInt": "120",
                    "attributes": [
                      {"key": "http.request.method", "value": {"stringValue": "GET"}}
                    ]
                  }
                ]
              }
            }
          ]
        }
      ]
    }
  ]
}
```

三层结构决定了标签来源：

```text
resource attributes  → job / instance / 资源标签
scope                → otel_scope_name / otel_scope_version / otel_scope_schema_url
data point attributes→ 业务标签
```

时间字段：

- `timeUnixNano`：样本时间，纳秒字符串；
- `startTimeUnixNano`：累计起点，delta 转换和 reset 检测的关键；
- 转换成 Prometheus 毫秒时间戳时做整数除法，不要先转浮点再乘，否则会丢精度。

## AG.4 名称与单位翻译

OTel 的命名惯例是 `namespace.component.measure` + `unit` 字段；Prometheus 的惯例是把单位放进名字后缀。典型映射：

| OTel 名称 | unit | Prometheus（NoUTF8） | Prometheus（Underscore） |
|---|---|---|---|
| `http.server.requests` | `1` | `http.server.requests_total` | `http_server_requests_total` |
| `http.server.duration` | `s` | `http.server.duration_seconds` | `http_server_duration_seconds` |
| `process.memory.usage` | `By` | `process.memory.usage_bytes` | `process_memory_usage_bytes` |
| `network.io` | `By/s` | `network.io_bytes_per_second` | `network_io_bytes_per_second` |
| `queue.depth` | `1` | `queue.depth` | `queue_depth` |

规则：

1. **单位后缀**只在 unit 不是 `1`、不是 `{...}` 注释单位时追加。常见 UCUM：`s`→`_seconds`、`ms`→`_milliseconds`、`By`→`_bytes`、`By/s`→`_bytes_per_second`、`%`→`_percent`。
2. **单调 sum** 追加 `_total`；已经以 `_total` 结尾的不重复追加。非单调 sum 不追加 `_total`，但仍要谨慎：它在 Prometheus 里通常按 gauge 语义使用。
3. **直方图** 的 family 名先加单位后缀，再派生 `_bucket`、`_sum`、`_count`。上游 Prometheus 3 通常把 explicit-bucket histogram 转成 **custom-bucket native histogram（NHCB）**；Mini 没有原生直方图存储，因此展开成经典 `_bucket` 序列。两种表示在 `histogram_quantile` 上结果接近，但序列数和存储格式不同。
4. **指数直方图** 直接对应原生直方图；Mini 明确拒绝，避免把分布压成一条无意义的 float。
5. **summary** 展开为 `<name>_count`、`<name>_sum` 和带 `quantile` 标签的 series。summary 的 quantile 不可跨实例聚合，生产上优先用直方图。
6. **空名字/纯符号名字** 必须报错或丢弃并计数；不要生成 `_` 这种无法区分来源的指标。

命名冲突：`a.b` 与 `a_b` 在 underscore 策略下会撞名。上游翻译器会检测冲突；Mini 在标签层记录 warning，在指标层则要求调用方保证唯一性。生产迁移应维护显式映射表（见附录 AF）。

## AG.5 资源属性与 scope

Prometheus 的数据模型没有"resource"这一层，必须摊平成标签或单独的 `target_info`。

上游 3.x 的默认行为：

- `service.name` → `job`；
- `service.instance.id` → `instance`；
- `service.namespace` + `service.name` → `job` 的 `namespace/name` 形式；
- 其余 resource attributes **默认不进入每条指标**，而是聚合成 `target_info{...} 1`；
- 只有 `otlp.promote_resource_attributes` 显式列出的属性才追加为指标标签；
- `keep_identifying_resource_attributes: true` 会把身份属性也写进 `target_info`（默认 `false`）。

`target_info` 的查询方式是 join：

```promql
http_requests_total
* on(job, instance) group_left(service_version)
  target_info
```

Mini 的实现（教学简化，与上游不同）：

- `service.name`、`service.namespace`、`service.instance.id` 不保留为同名标签，只映射成 `job`/`instance`；
- **其余 resource attributes 直接提升为指标标签**，不做 `promote_resource_attributes` 过滤；
- 属性名按 `translation_strategy` 处理：`NoUTF8EscapingWithSuffixes` 保留 `k8s.pod.name` 这类 UTF-8 标签名，`UnderscoreEscapingWithSuffixes` 转成 `k8s_pod_name`；
- `__name__` 和 `__` 前缀属性直接丢弃，防止 OTLP 生产者覆盖序列身份；
- 属性名清洗后撞名时保留第一个并记录 warning，不静默合并；
- scope 只保留 `otel_scope_name`、`otel_scope_version`、`otel_scope_schema_url`，不把 scope attributes 摊平（基数风险）；
- **不生成 `target_info`**。

因此 Mini 的标签集通常比真实 Prometheus 更宽。生产上如果资源属性很多，优先用 Collector 生成 `target_info` 或显式 allowlist，而不是给每条指标复制 10 个资源标签。

## AG.6 Delta → Cumulative 的状态机

### 为什么不能直接累加

delta 点只表示"区间增量"。要变成 Prometheus counter，需要维护每个序列的累计值：

```text
cumulative(t) = cumulative(t-1) + delta(t)
```

但以下情况必须重置：

- `startTimeUnixNano` 变化（进程重启、SDK 重建流）；
- 时间戳回退（乱序/重放）；
- 序列消失后重新出现（无法区分"新流"与"旧流继续"）；
- 计数器重置（delta 本身为负或后端检测到 reset）。

### 一个可实现的判定

```text
key = labels                       # metric name is part of labels (__name__)
state = {start_ns, last_ts_ns, cumulative, ...}

if no state:
    init state; cumulative = delta
elif point.start_ns != state.start_ns:
    reset state; cumulative = delta          # new stream
elif point.ts < state.last_ts:
    drop the point                           # do not double count
else:
    cumulative += delta
```

关键点：

1. **start time 是流身份的一部分**，不是可有可无的元数据；缺失时无法可靠区分 reset。
2. **乱序点不能简单累加**，否则会重复计数；应丢弃或按上游的窗口折叠规则处理。
3. **状态要有上限**。高基数 + 长保留会吃掉内存；Mini 用 `max_delta_series` 显式失败。
4. **状态不持久化**。进程重启后第一段 delta 只能作为新基线；这是可接受的近似，但要在监控里标注。
5. **直方图的每个桶、count、sum 都要分别累计**，且桶布局变化必须报错，不能 zip 截断；只比较桶数量不够，必须比较完整的 `explicitBounds`（同数量但边界变化同样会污染累计桶）。

### Mini 的 delta 实现

`mini_prometheus/otlp.py`：

- `_delta_value`：标量 sum 的累计与 reset；
- `_delta_histogram`：bucketCounts/count/sum 的累计；
- `_delta_key`：以 labels 作为状态键，`startTimeUnixNano` 存在状态里用于 reset 检测；
- `_store_delta`：超过 `max_delta_series` 抛错；
- 未开启 `delta_to_cumulative` 时抛 `OTLPError`，HTTP 层映射为 400。

测试覆盖：

- delta 默认拒绝；
- 两次 delta 累加为 `5 → 12`；
- start time 变化后重置为 `2`；
- delta 直方图桶累计；
- 缺失 start time 时按新基线处理并给 warning。

## AG.7 直方图、summary 与 exemplar

### explicit-bucket histogram

OTLP 的 `bucketCounts` 是**每桶增量**，不是累计值；`explicitBounds` 不含 `+Inf`。转换时：

```text
cumulative[i] = sum(bucketCounts[0..i])
le = explicitBounds[i]
+Inf bucket = count（或 bucketCounts 的总和）
```

Mini 输出：

```text
<name>_bucket{le="0.1"} 4
<name>_bucket{le="0.5"} 8
<name>_bucket{le="+Inf"} 10
<name>_sum 2.5
<name>_count 10
```

这样 `histogram_quantile(0.5, ...)` 可以直接使用。

### exponential histogram

字段包含 `scale`、`zeroCount`、`positive`/`negative` 的 `offset` 与 `bucketCounts`。它对应原生直方图的稀疏指数桶，不能无损展开成经典桶。Mini 的选择是**拒绝并报错**；生产上要么用支持原生直方图的后端，要么在 Collector 里转成 explicit buckets。

### summary

summary 的 quantile 是客户端预计算的，不能跨实例聚合。转换后：

```text
<name>_count
<name>_sum
<name>{quantile="0.5"}
<name>{quantile="0.99"}
```

summary 在 OTLP 里没有 `aggregationTemporality` 字段，按累计值处理；Mini 直接展开它的 count/sum/quantile，不参与 delta 状态机。

### exemplar

OTLP 的 exemplar 可以携带 trace/span ID。Prometheus 3.x 的较新版本默认启用 exemplar 存储（`exemplar-storage` flag 已 deprecated；早期 3.x/2.x 仍需确认或显式开启）；Mini 没有 exemplar TSDB，因此**计数但不存储**，并在文档中明确。需要 exemplar 时用真实 Prometheus/后端，或在 Collector 里转到支持 exemplar 的链路。

## AG.8 HTTP 层与安全

Mini 的端点实现（`mini_prometheus/api/server.py`）：

- 只接受 `POST`；
- receiver 未启用时返回 404（与 Prometheus 未注册端点一致）；
- `application/x-protobuf` 返回 400 并提示用 JSON；
- 支持 `Content-Encoding: gzip`，解压后再次检查 64 MiB 上限，防 zip bomb；
- JSON 解析失败返回 400 `bad_data`；
- 成功返回裸 200 空 body（OTLP/HTTP 约定，不是 Prometheus JSON envelope）；
- 成功不代表每个样本都被 TSDB 接受：重复/乱序样本计入 `prometheus_otlp_append_errors_total`；
- `/metrics` 暴露 `prometheus_otlp_received_samples_total`、`prometheus_otlp_received_metric_points_total`、`prometheus_otlp_delta_resets_total`、`prometheus_otlp_append_errors_total`。

生产上还要加：

- TLS 与认证（mTLS、Bearer、OAuth2），通常在反向代理/网关终止；
- 请求体大小、单请求点数、每秒请求数限制；
- 资源属性 allowlist/denylist，防止高基数标签；
- 丢弃 `__name__`、`__*` 和与 `job`/`instance` 冲突的属性；
- 对 delta 状态做内存上限和过期清理；
- 接收端自身的延迟、拒绝数、状态数纳入监控。

## AG.9 与 Collector 的分工

推荐的生产分层：

```text
应用 SDK
  → OTel Collector（batch、memory_limiter、resource/attributes、transform、deltatocumulative）
  → Prometheus remote write / OTLP receiver
```

Collector 负责：

- 批量与重试；
- 资源属性裁剪、重命名、脱敏；
- delta→cumulative（`deltatocumulative` processor）；
- 多租户路由、采样、限流；
- 把 OTel 语义约定映射成团队命名规范。

Prometheus 负责：

- 存储与查询；
- 规则/告警；
- 保留与 compaction；
- 自身指标与 SLO。

直收适合"边缘节点只有一个 Prometheus、数据量小、没有 Collector 运维能力"的场景；一旦需要治理，尽早引入 Collector。

## AG.10 实验：从 OTLP 到 PromQL

```bash
python examples/10_otlp_ingest.py
```

预期输出（时间戳随运行变化）：

```text
OTLP counter -> {'__name__': 'http.server.requests_total', 'deployment.environment': 'prod', 'http.request.method': 'GET', ...} [..., '120']
OTLP gauge   -> {'__name__': 'queue.depth', ...} [..., '7']
OTLP p50 (s) -> {...} [..., '0.2']
service labels -> {'deployment.environment': 'prod', 'job': 'checkout'} [..., '1']
prometheus_otlp_received_samples_total 8
prometheus_otlp_received_metric_points_total 3
```

手工启动：

```bash
python -m mini_prometheus \
  --config.file=examples/config/prometheus.yml \
  --storage.tsdb.path=./data-otlp \
  --web.listen-address=127.0.0.1:9091 \
  --web.enable-otlp-receiver \
  --enable-feature=otlp-deltatocumulative
```

然后：

```bash
curl -s -X POST http://127.0.0.1:9091/api/v1/otlp/v1/metrics \
  -H 'Content-Type: application/json' \
  --data-binary @payload.json

curl -s 'http://127.0.0.1:9091/api/v1/query?query=%7B%22http.server.requests_total%22%7D'
```

注意 URL 编码：UTF-8 指标名里的点号、引号、花括号都要编码，直接用 shell 拼字符串容易出错。

## AG.11 生产检查表

- [ ] 明确走 Collector 还是直收，以及为什么。
- [ ] 固定 OTLP 编码（protobuf/JSON）与 content-type，发送端/接收端一致。
- [ ] 固定 translation strategy，并在全链路只转换一次。
- [ ] 资源属性有 allowlist；`service.name`/`service.instance.id` 映射到 `job`/`instance`。
- [ ] 丢弃 `__name__`、`__*`、与约定标签冲突的属性。
- [ ] delta 已转换或后端明确支持；`startTimeUnixNano` 存在且稳定。
- [ ] 直方图类型（explicit/exponential）与后端能力匹配；summary 不作为聚合分位数来源。
- [ ] exemplar 是否需要 trace 关联；Mini 不存储 exemplar。
- [ ] 请求体、点数、状态数、速率有限制；gzip 解压有上限。
- [ ] 接收端自身指标、拒绝原因、delta reset 有告警。
- [ ] 用真实 OTel SDK + Collector 做端到端验证，而不是只发手写 JSON。

## AG.12 延伸阅读

- Prometheus OTLP receiver 文档与 `--web.enable-otlp-receiver`。
- OpenTelemetry 规范 "Prometheus and OpenMetrics compatibility"。
- OTLP/HTTP 协议与 JSON mapping。
- 本教程第 4、20、41 章；附录 R（原生直方图）、AF（UTF-8 名称）、Q（长期存储）。
