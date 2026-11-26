# 第 3 章 数据模型：指标、标签、样本与序列

## 3.1 四个名词

- **指标名（metric name）**：描述"测量什么"，如 `http_requests_total`。
- **标签（label）**：描述"哪个对象/维度"，如 `job="api"`、`code="500"`。
- **样本（sample）**：一个 `(timestamp, float64 value)` 对。
- **时间序列（time series）**：一个指标名 + 一组标签的唯一组合，按时间排列的样本流。

形式化地：

```text
series = (__name__, {(k1,v1), (k2,v2), ...})
sample = (t, v),  t ∈ int64 milliseconds, v ∈ float64
```

Prometheus 把指标名存成特殊标签 `__name__`。因此：

```text
http_requests_total{job="api",code="200"}
```

与

```text
{__name__="http_requests_total",job="api",code="200"}
```

是同一条序列。

## 3.2 标签是身份，不是元数据

两个常见误解：

1. **"标签只是展示用的"**。错。标签组合决定序列身份；改一个标签就是另一条序列，历史数据不会自动继承。
2. **"标签越多信息越全"**。错。每个不同组合都占用独立序列、索引项、内存和存储。高基数标签是 Prometheus 最常见的生产事故来源。

一个实用的判断：**标签值应该是低基数、可枚举、对聚合有意义的**。

推荐：

```text
job, instance, service, namespace, pod, container, method, status_code, region, version
```

谨慎：

```text
user_id, request_id, session_id, url_path_with_ids, error_message, timestamp, ip
```

`instance` 已经是高基数但必要的维度；如果再叠加 `pod_ip`、`node_ip`，基数会相乘。第 27 章会给出基数预算方法。

## 3.3 基数（cardinality）的算术

假设指标 `http_requests_total` 有：

- `job` 10 个值；
- `instance` 每 job 20 个；
- `method` 5 个；
- `code` 10 个；
- `version` 5 个。

最大序列数：

```text
10 × 20 × 5 × 10 × 5 = 50,000
```

如果再加一个 `user_id`（10 万），理论上是 50 亿条。实际不会全部出现，但只要出现 1%，就是 5000 万条活跃序列，足以让单机 Prometheus OOM。

**活跃序列**（head 中最近有样本的序列）比历史序列更关键：TSDB 的 head 索引、postings、查询内存都与活跃序列强相关。历史 block 的序列可以靠压缩和 retention 控制，head 不能。

## 3.4 标签命名与语义

Prometheus 保留 `__` 前缀：

- `__name__`：指标名；
- `__address__`、`__scheme__`、`__metrics_path__`、`__param_*`：抓取目标内部标签，不会写入样本；
- `__meta_*`：服务发现元数据，只在 relabel 阶段可见；
- `__tmp_*`：临时标签，约定在 relabel 后删除。

业务标签不应使用 `__` 前缀。`job` 和 `instance` 是抓取层自动附加的两个关键标签：

- `job`：抓取配置的 `job_name`，表示"同一类目标"；
- `instance`：目标地址（默认 `__address__`），表示"具体实例"。

不要手工覆盖 `job`/`instance`，除非你明确知道 relabel 的后果。

## 3.5 时间戳与采样

Prometheus 内部时间戳是**毫秒整数**。API 返回秒（浮点），例如：

```json
{"metric": {"__name__": "up"}, "value": [1700000000.123, "1"]}
```

关键语义：

- 抓取时如果 exporter 没有带时间戳，Prometheus 用**抓取开始时间**；
- `honor_timestamps: true`（默认）时使用 exporter 提供的时间戳；
- 同一序列同一时间戳重复写入不同值会报错；
- 时间戳倒退（out-of-order）在 head 中默认拒绝，超出允许窗口的乱序样本会被丢弃或进入 out-of-order head（取决于版本与配置）。

Mini 的实现严格拒绝乱序，并在 `append_batch` 中逐条报告错误，便于观察。

## 3.6 浮点数的坑

样本值是 IEEE-754 float64，意味着：

- 计数器超过 `2^53` 后整数精度丢失；
- `NaN`、`+Inf`、`-Inf` 是合法值；
- Prometheus 用特殊 NaN 位模式表示 **staleness**，与普通 NaN 不同。

普通 NaN 表示"测量到了非数"（如 0/0），staleness 表示"这条序列在当前时间点不应再被查询到"。两者在 `rate()`、`absent()`、instant selector 中的行为不同。Mini 在 `util/timeutil.py` 中用位模式区分：

```python
STALE_NAN_BITS = 0x7FF0000000000002
```

第 12 章会讲 staleness 的完整生命周期。

## 3.7 指标名与 UTF-8

传统指标名必须匹配：

```text
[a-zA-Z_:][a-zA-Z0-9_:]*
```

Prometheus 3.0 起默认支持 UTF-8 指标名和标签名，查询时用引号：

```promql
{"http.requests.total", job="api"}
{"http.requests.total", "service.name"="checkout"}
```

非 legacy 标签名同样要引号；引号内的反斜杠、换行和双引号需要转义（`\\`、`\n`、`\"`）。

但生态兼容性仍是问题：Grafana、告警模板、remote write 接收端、老版本客户端未必都支持。生产建议：

- 指标名保持 `[a-zA-Z_:][a-zA-Z0-9_:]*`；
- 标签名保持 `[a-zA-Z_][a-zA-Z0-9_]*`；
- 把 UTF-8 能力留给确实无法转换的第三方指标，并在采集边界做规范化。

Mini 同时接受经典与引号形式；实现与迁移清单见第 32、37 章与附录 AF。

## 3.8 序列的生命周期

一条序列会经历：

1. **发现**：服务发现产生目标；
2. **首次抓取**：第一次出现样本，head 创建序列；
3. **持续抓取**：样本追加到 chunk；
4. **消失**：抓取失败（失败抓取视为空抓取）、序列从成功抓取中消失、目标移除时写入 stale NaN，instant query 立即不再返回；
5. **lookback 兜底**：没有 stale 的场景（远端写、显式时间戳、崩溃前未写标记）在 `lookback`（默认 5m）后不再返回；
6. **落盘**：head 被切块，序列进入 block；
7. **保留/删除**：retention 或 tombstone 最终移除。

理解这条链，才能解释"为什么目标已经下线，Grafana 上还有一条平线"——通常是 exporter 仍在返回旧值、数据来自 remote write（没有 stale），或查询窗口包含旧样本；不要一律归因于 5 分钟 lookback。

## 3.9 一个建模例子

假设要监控订单服务：

```text
orders_created_total{region="cn-north",status="paid"}
orders_created_total{region="cn-north",status="failed"}
orders_amount_sum{region="cn-north",currency="CNY"}
orders_processing_seconds_bucket{le="0.1",region="cn-north"}
```

不要：

```text
orders_created_total{order_id="202611260001"}
orders_created_total{user_id="u123"}
orders_created_total{error="dial tcp 10.0.0.7:5432: connection refused"}
```

订单/用户维度应进入日志或追踪；错误文本应作为日志字段或低基数 `error_type`。指标只保留可聚合的维度。

## 3.10 指标命名与单位约定

命名是数据模型的一部分，不是"起个好听的名字"。推荐结构：

```text
<namespace>_<subsystem>_<name>_<unit>[_total]
```

例子：

```text
http_requests_total                   # counter，HTTP 请求数
http_request_duration_seconds         # histogram，秒
node_memory_used_bytes                # gauge，字节
process_cpu_seconds_total             # counter，CPU 秒
kube_pod_info                         # info 型 gauge，值为 1
```

约定：

- **单位放指标名**：`_seconds`、`_bytes`、`_meters`、`_celsius`；不要 `_ms` 混用；
- **base unit**：秒、字节、米、伏特；展示层再换算；
- **counter 用 `_total`**；`_count`/`_sum`/`_bucket` 是 histogram/summary 的保留后缀；
- **ratio 用 `_ratio`**，值域 0～1；百分比在展示层乘 100；
- **timestamp 用 `_timestamp_seconds`**，值是 Unix 秒；
- **info 型指标**值为 1，信息放标签（如 `kube_pod_info`）；
- 避免 `count`/`total` 歧义：`_total` 是累计，`_count` 是当前数量时用 gauge + 明确后缀。

命名空间/子系统帮助避免冲突：

```text
myapp_http_requests_total
myapp_db_connections
myapp_queue_depth
```

不要用 `prometheus_`/`go_`/`process_` 等保留前缀命名业务指标。

## 3.11 标签设计模式

标签分两类：

- **身份标签**：`job`、`instance`（抓取层自动附加）；
- **维度标签**：`service`、`region`、`method`、`status_code`、`version`。

设计原则：

1. **可聚合**：标签值应该能用于 `sum by`/`avg by` 回答业务问题；
2. **低基数**：值域可枚举，避免 ID/自由文本；
3. **一致**：同一含义在不同指标中用同名标签（`service` 不要一会儿 `app` 一会儿 `svc`）；
4. **可连接**：需要 join 的指标共享标签（如 `service`、`region`），但不要为了 join 复制高基数标签；
5. **稳定**：标签值不要随部署/版本频繁变化（`pod` 会变，`service` 稳定）；
6. **不冗余**：`instance` 已含 IP/端口，不要再加 `pod_ip`/`host_ip`，除非有明确查询需求。

常见维度：

```text
service      服务名（稳定）
version      版本/镜像 tag（低基数）
region/zone  区域
method       HTTP 方法
status_code  HTTP 状态码（枚举，不要带文本）
quantile     summary 的分位数（固定几个值）
le           histogram 的桶上界（固定）
```

基数预算：给每个指标设一个活跃序列上限（第 27 章），超过就报警/拒绝。不要等 OOM 才发现。

## 3.12 元数据、类型与帮助文本

每个指标应有：

```text
# HELP http_requests_total Total number of HTTP requests.
# TYPE http_requests_total counter
# UNIT http_request_duration_seconds seconds
```

- `HELP`：一句话说明"测量什么"，不要写实现细节；
- `TYPE`：counter/gauge/histogram/summary；类型错误会导致查询/告警语义错误；
- `UNIT`：OpenMetrics 支持；文本格式常用命名后缀表达。

元数据 API：

```text
GET /api/v1/metadata
GET /api/v1/targets/metadata
```

用途：

- 指标目录/治理（第 47 章）；
- Grafana 的指标浏览器；
- 自动生成文档/告警模板；
- 检查"同名指标类型冲突"。

注意：

- 元数据来自抓取，target 消失后可能保留一段时间；
- 不同 target 对同一指标可能给出不同 HELP/TYPE，需要治理；
- 类型冲突是严重问题：同一指标名不能既是 counter 又是 gauge。

## 3.13 常见建模反模式

| 反模式 | 问题 | 替代 |
|---|---|---|
| `order_id`/`user_id` 进标签 | 基数爆炸 + PII | 日志/追踪；聚合维度 |
| `error="dial tcp ..."` | 自由文本，基数无限 | `error_type="timeout"` |
| `path="/orders/123"` | 每个 ID 一条序列 | `path="/orders/{id}"` |
| `quantile="0.9"` 作为普通标签 | 聚合语义错误 | summary 的固定 quantile 或 histogram |
| `timestamp` 作为标签 | 每秒新序列 | 用样本时间戳 |
| 动态标签名（`user_123="x"`） | 标签名爆炸 | 固定标签名 + 值 |
| 把版本/构建号放进高频指标 | 版本一换就翻倍 | 只放低基数 `version`，或 info 指标 |
| 重复 `env`/`cluster` 到每个标签 | 冗余/不一致 | 统一由抓取层附加 |
| 一个指标混多种单位 | 查询无法比较 | 拆指标或统一单位 |
| 用 gauge 模拟 counter | 重置/速率错误 | counter + `rate` |

一个检查方法：把标签值全列出来，问"这个标签的每个值都能在 dashboard/告警里按业务意义聚合吗？"如果不能，它可能不该是指标标签。

## 3.14 指标、日志、追踪的边界

| 问题 | 用什么 | 原因 |
|---|---|---|
| 服务错误率是否升高？ | 指标 | 聚合、低成本、可告警 |
| 哪一次请求失败了？ | 追踪/exemplar | 个体级、带上下文 |
| 错误的具体堆栈？ | 日志 | 高基数、非结构化 |
| 哪个版本/区域变慢？ | 指标（标签） | 多维聚合 |
| 单个用户的行为？ | 日志/追踪/分析系统 | 隐私/基数 |
| 长期趋势/容量？ | 指标 + 聚合 | 可降采样 |

指标不是"万能数据源"。把个体级信息放进指标，既贵又难查；把聚合问题交给日志，则成本高且慢。建模时先选对信号类型。

## 3.15 建模评审清单

```text
[ ] 指标名符合命名/单位约定
[ ] 类型正确（counter/gauge/histogram/summary）
[ ] 标签值可枚举、低基数、可聚合
[ ] 没有 PII/自由文本/ID
[ ] 与其他指标共享必要标签，可 join
[ ] 有 HELP/UNIT，元数据可查
[ ] 基数预算与 owner 明确
[ ] 有对应的查询/告警/面板用例
[ ] 废弃计划（如果需要）
```

## 本章小结

- 序列身份 = 指标名 + 标签集合；标签变化就是新序列。
- 基数由标签值的笛卡尔积决定，活跃序列数比历史总量更危险。
- 时间戳是毫秒；普通 NaN 与 stale NaN 语义不同。
- `__` 前缀标签属于内部/发现层，不应进入业务指标。
- 指标建模要围绕"可聚合的维度"，而不是"所有可用的元数据"。

## 延伸阅读

- Prometheus 官方文档 "Data model" 与 "Metric and label naming"。
- 本教程第 4 章（指标类型）与第 27 章（基数治理）。
- 附录 D（指标命名规范）。
