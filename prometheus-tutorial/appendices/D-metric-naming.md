# 附录 D 指标命名与标签规范

> 指标名是 API，标签是 API 的参数。改名/改标签的代价通常高于改代码：面板、告警、记录规则、外部集成都会一起坏。本附录给一套可直接用于评审的命名、单位、标签与迁移规范。

## D.1 命名结构

```text
<namespace>_<subsystem>_<name>_<unit>[_total]
```

例子：

```text
myapp_http_requests_total
myapp_http_request_duration_seconds
myapp_queue_depth
myapp_cache_hits_total
myapp_build_info
```

- `namespace`：应用/团队前缀，避免跨项目冲突；
- `subsystem`：模块/协议/资源类型，如 `http`、`grpc`、`db`、`queue`；
- `name`：被测量的对象/动作，如 `request`、`response`、`connections`；
- `unit`：基本单位（seconds/bytes/ratio），放在名字末尾、`_total` 之前；
- Counter 以 `_total` 结尾；
- Histogram 生成 `_bucket/_sum/_count`，不要手工拼；
- Info 指标用 `_info`，值恒为 1。

命名原则：

- 用**基本单位**，不要用毫秒/微秒/千字节；
- 用**复数**表示可计数的对象（`requests`、`errors`、`connections`）；
- 不要重复 `prometheus_`/`node_` 等保留前缀；
- 不要让指标名携带高基数信息（如 `http_requests_user_123_total`）；
- 名字要能回答"这是什么、单位是什么、方向是什么"。

## D.2 单位

| 量 | 单位 | 指标后缀 | 说明 |
|---|---|---|---|
| 时间 | 秒 | `_seconds` | 不要用 `_ms`/`_us`/`_minutes` |
| 字节 | 字节 | `_bytes` | 不要用 `_kb`/`_mb` |
| 比例 | 0～1 | `_ratio` | 百分比先除以 100 |
| 计数 | 无 | `_total` | Counter 专用 |
| 温度 | 摄氏度 | `_celsius` | 华氏度先转换 |
| 电压 | 伏特 | `_volts` | |
| 电流 | 安培 | `_amperes` | |
| 功率 | 瓦特 | `_watts` | |
| 能量 | 焦耳 | `_joules` | |
| 长度 | 米 | `_meters` | |
| 质量 | 克 | `_grams` | |
| 信息量 | 比特 | `_bits` | 网络吞吐常用 |

派生单位：

- 每秒速率：`_per_second`（gauge 型速率），不要写成 `_rate` 除非语义明确；
- 每请求成本：`_cost_per_request`，单位写进 HELP；
- 分位数：不要放进指标名（`_p99`），用 Histogram + `histogram_quantile`；
- 百分比：优先 `_ratio`（0～1），告警里再乘 100 展示。

## D.3 类型命名

| 类型 | 命名 | 例子 | 注意 |
|---|---|---|---|
| Counter | `_total` | `http_requests_total` | 只增；重启归零由 `rate` 处理 |
| Gauge | 无后缀或 `_current`/`_depth`/`_size` | `queue_depth` | 可增可减 |
| Histogram | `_seconds`/`_bytes` | `request_duration_seconds` | 自动生成 `_bucket/_sum/_count` |
| Summary | 同 Histogram | `request_duration_seconds` | 客户端算分位数，不可聚合 |
| Info | `_info` | `build_info` | 值恒为 1，标签带元数据 |

类型选择的常见错误：

- 用 Gauge 记录"累计请求数"，再套 `rate()`：会得到错误结果；
- 用 Counter 记录"当前连接数"：重启/回收会破坏单调性；
- 用 Summary 做跨实例聚合：分位数不可加；
- 用 Histogram 但不设计桶：分位数误差大；
- 用 Info 指标承载高频变化的值：应该用 Gauge。

## D.4 标签规范

推荐标签（按需选择，不要全加）：

```text
job, instance, service, namespace, pod, container,
method, status_code, region, zone, version, environment,
team, owner, route, protocol
```

避免：

```text
user_id, request_id, session_id, order_id,
url_with_ids, error_message, timestamp, ip, pod_ip,
trace_id, span_id, query, raw_path, user_agent
```

规则：

- 标签名优先 `[a-zA-Z_][a-zA-Z0-9_]*`；UTF-8 名称可用，但查询要加引号，生态兼容性差；
- 值必须低基数、可枚举、稳定；
- `job`/`instance` 由抓取层自动附加，不要在应用里重复埋；
- `service`/`team`/`environment` 用统一词表，不要混用 `env`/`environment`、`svc`/`service`；
- `status_code` 用 `200`/`500` 或 `2xx`/`5xx` 统一口径，不要一处精确一处分类；
- `method` 用大写 HTTP 方法；`route` 用模板化路径（`/users/{id}`），不要原始 URL；
- 单位放指标名，不放标签值（`unit="ms"` 是反模式）；
- `le`/`quantile` 是 Histogram/Summary 保留标签，不要手工使用；
- `__name__`/`__address__`/`__meta_*` 是内部标签，不要写进应用指标；
- 标签值不要包含换行、制表符、超长文本；exposition 会转义，但查询/存储成本高。

标签基数预算：

```text
单指标序列数 = 各标签取值数的乘积（再乘 Histogram 桶数）
```

例：`route` 50 × `method` 5 × `status` 6 × `version` 3 × `instance` 20 = 90,000 条序列；如果 Histogram 有 12 个桶，就是 108 万条。命名评审必须和基数预算一起做。

## D.5 高基数反模式

```text
http_requests_total{path="/user/12345"}       # 动态 path
http_requests_total{error="dial tcp ..."}     # 错误文本
process_open_fds{pid="1234"}                  # PID
kube_pod_info{pod="api-7d9f-abc"}             # 每次发布变化
http_requests_total{trace_id="..."}           # 追踪 ID
http_requests_total{user_id="..."}            # 用户 ID
http_requests_total{url="https://...?q=..."}  # 完整 URL/查询串
```

治理：

- path 归一化（`/user/:id`、`/order/{id}`）；
- 错误分类（`error_type="timeout"`/`"connect"`/`"tls"`），原始错误进日志；
- 去掉 PID、容器 ID、Pod UID；Pod 名只在排障面板保留；
- 版本用语义化版本或发布标签，不要用 commit SHA（除非发布频率可控）；
- 用日志/追踪存高基数细节，指标只留可聚合维度；
- 对第三方 exporter 做 `metric_relabel_configs`，drop 掉不需要的高基数标签；
- 新指标上线前跑基数估算，设置 `sample_limit`/`label_limit` 兜底。

## D.6 命名审查清单

- [ ] 有 namespace 前缀，且不与现有指标冲突？
- [ ] 单位正确、后缀正确、使用基本单位？
- [ ] Counter 以 `_total` 结尾，且语义单调？
- [ ] Histogram 桶覆盖 SLO，且包含 `+Inf`？
- [ ] 标签低基数、可枚举、命名统一？
- [ ] 没有用户/请求/追踪 ID、错误文本、完整 URL、IP、PID？
- [ ] 没有把单位/类型/状态塞进指标名？
- [ ] 与现有指标不重复、不近义？
- [ ] 有 HELP 文本、owner/team、生命周期状态？
- [ ] 有基数预算与降级方案？
- [ ] 有记录规则/告警/面板的使用方清单？
- [ ] 有弃用/迁移计划？

## D.7 示例：一个服务的指标集

```text
# 请求
api_http_requests_total{method="GET",route="/orders/{id}",status_code="200"}
api_http_request_duration_seconds_bucket{le="0.1"}
api_http_request_duration_seconds_sum
api_http_request_duration_seconds_count
api_http_inflight_requests
api_http_request_size_bytes
api_http_response_size_bytes

# 依赖
api_db_query_duration_seconds
api_db_errors_total{operation="select",error_type="timeout"}
api_cache_hits_total
api_cache_misses_total
api_queue_depth{queue="orders"}
api_queue_publish_total{result="ok"}

# 构建/元数据
api_build_info{version="1.2.3",revision="abc",goversion="go1.24"}
api_target_info{service="api",team="platform",environment="prod"}
```

注意：

- `route` 用模板，不用真实 ID；
- `status_code` 与 `error_type` 分开：状态码是协议层，错误类型是应用层；
- `build_info`/`target_info` 是低基数元数据，适合 join；
- `queue` 是有界枚举，不要用 topic 全名/消费者 ID。

## D.8 与 OTel 命名/语义约定的映射

OTel 语义约定常用点号，Prometheus 用下划线。映射不是简单替换，要处理单位、类型和后缀。

| OTel 指标（示例） | Prometheus 目标 | 说明 |
|---|---|---|
| `http.server.request.duration` | `http_server_request_duration_seconds` | Histogram，单位秒 |
| `http.server.request.count` | `http_server_requests_total` | Counter，复数 + `_total` |
| `http.server.active_requests` | `http_server_active_requests` | Gauge/UpDownCounter |
| `http.client.request.duration` | `http_client_request_duration_seconds` | Histogram |
| `rpc.server.duration` | `rpc_server_duration_seconds` | Histogram |
| `db.client.operation.duration` | `db_client_operation_duration_seconds` | Histogram |
| `messaging.client.publish.duration` | `messaging_client_publish_duration_seconds` | Histogram |
| `system.cpu.utilization` | `system_cpu_utilization_ratio` | 0～1 ratio |
| `system.memory.usage` | `system_memory_usage_bytes` | 字节 |
| `process.cpu.time` | `process_cpu_seconds_total` | Counter |

映射时要：

- 统一单位（OTel 常用秒/字节，仍要检查）；
- 统一 counter 后缀（OTel 的 `.count` 常映射为 `_total`）；
- 去掉重复前缀（`http_` 不要出现两次）；
- 把 resource attributes 映射成低基数标签（`service.name` → `service`）；
- 固定语义约定版本，属性名会演进（如 `gen_ai.system` → `gen_ai.provider.name` 一类变化）；
- 不要把所有 scope/resource 属性都摊平到每条指标，优先 `target_info` 或 allowlist。

## D.9 指标契约（Metric Contract）

每个新指标至少记录：

| 字段 | 示例 |
|---|---|
| 名称 | `api_http_requests_total` |
| 类型 | Counter |
| 单位 | requests |
| 标签 | `method`、`route`、`status_code`（各 ≤ 20/50/10） |
| 基数预算 | ≤ 5,000 序列 |
| 所有者 | team-platform |
| 用途 | SLO、错误率、容量 |
| 保留期 | 本地 15d，远端 13m |
| 弃用策略 | 双写 90d，记录规则过渡 |

契约要进仓库、进评审、进 CI（命名/标签/基数 lint），否则半年后没人记得为什么有这个指标。

## D.10 重命名与迁移

指标名/标签是外部 API，改名要走迁移：

1. **双写**：新旧指标同时暴露，旧指标标 `_deprecated` 或用 recording rule 生成；
2. **双查**：面板/告警先切到新指标，保留旧指标对照；
3. **观察**：至少一个保留周期，确认无查询方依赖旧指标；
4. **下线**：删除旧指标，更新文档/契约；
5. **回滚**：保留旧指标一个版本，防止遗漏。

记录规则做兼容层：

```yaml
groups:
  - name: compatibility
    rules:
      - record: api_http_requests_total
        expr: api_http_server_requests_total
```

注意：recording rule 生成的指标名可能与原指标冲突；用 `metric_relabel_configs` 或独立 job 隔离。

## D.11 好/坏命名对照

| 坏 | 问题 | 好 |
|---|---|---|
| `http_requests` | 没有单位/类型/后缀 | `http_requests_total` |
| `request_duration_ms` | 毫秒单位 | `http_request_duration_seconds` |
| `cpu_usage_percent` | 百分比 | `node_cpu_usage_ratio` |
| `http_requests_200_total` | 状态码进名字 | `http_requests_total{status_code="200"}` |
| `errors_total{message="..."}` | 错误文本高基数 | `errors_total{error_type="timeout"}` |
| `user_requests_total{user_id="..."}` | 用户 ID 高基数 | 聚合到 `service`/`plan` |
| `api_latency_p99` | 分位数进名字 | `api_request_duration_seconds` + `histogram_quantile` |
| `service="API"` vs `service="api"` | 值不统一 | 统一小写词表 |
| `env="prod"` vs `environment="production"` | 标签名/值漂移 | 统一 `environment` + 词表 |

## D.12 延伸阅读

- Prometheus 官方 "Metric and label naming"、"Instrumentation"。
- OpenTelemetry semantic conventions（指标部分）。
- 本教程第 3、5、27、47 章与附录 Z（治理清单）、附录 W（埋点示例）。
