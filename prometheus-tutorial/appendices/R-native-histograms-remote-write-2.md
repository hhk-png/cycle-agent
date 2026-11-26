# 附录 R 原生直方图与 Remote Write 2.0 深入

第 4、20 章介绍了原生直方图（native histogram）和 Remote Write 2.0 的概念；本附录进入数据模型、协议和迁移细节。它们经常一起出现，但不是同一件事：

- **原生直方图**改变的是"一个直方图如何表示成一条序列"；
- **Remote Write 2.0**改变的是"样本/元数据/直方图如何从发送端到接收端"。

可以只用原生直方图而继续用 RW 1.0（不推荐，元数据/直方图表达不完整），也可以只用 RW 2.0 而继续发送经典直方图。生产迁移要分别评估。

## R.1 经典直方图的结构性成本

经典直方图：

```text
http_request_duration_seconds_bucket{le="0.1"} 50
http_request_duration_seconds_bucket{le="0.5"} 90
http_request_duration_seconds_bucket{le="1"}   95
http_request_duration_seconds_bucket{le="+Inf"} 100
http_request_duration_seconds_sum 42.3
http_request_duration_seconds_count 100
```

问题：

- 每个 `le` 是一条独立序列；10 个桶 × 10 个标签组合 = 100 条序列；
- 桶边界由客户端预先固定，事后不能改；
- 宽动态范围（微秒到分钟）需要很多桶，序列数线性增长；
- 跨实例聚合要 `sum by (le)`，桶边界必须完全一致；
- 最高桶以上的分位数不可精确。

原生直方图把整个分布放在**一条序列**里，桶是稀疏的、指数增长的，能显著降低高基数直方图的序列数。

## R.2 原生直方图的数据模型

一个原生直方图样本包含：

- `count`：观测总数（包括零桶、正/负桶、NaN 观测）；
- `sum`：观测值总和；
- `zero_count`：落在零桶的观测数；
- `zero_threshold`：零桶的绝对边界；
- 正桶数组：每个桶有 `index` 和 `count`；
- 负桶数组：镜像的负值桶；
- 可选的 exemplar。

### schema 与桶边界

标准指数 schema 用整数 `n` 表示，常见范围 `-4 ≤ n ≤ 8`：

```text
相邻桶边界比 = 2^(2^-n)
```

- `n` 越大，桶越细（分辨率越高）；
- `n = 0` 时，边界按 2 的幂增长；
- `n = 3` 时，每个 2 倍区间被分成 2^3 = 8 个桶；
- 每个桶的索引 `i` 对应一个边界区间；正桶和负桶分别向两侧延伸。

概念上：

```text
positive bucket i ≈ (base^i, base^(i+1)]
base = 2^(2^-n)
zero bucket = [-zero_threshold, zero_threshold]
negative bucket i ≈ [-base^(i+1), -base^i)
```

具体边界/索引用的是 Prometheus 的规范定义；不同客户端库的默认 `zero_threshold` 可能不同（client_golang 的默认零桶阈值很小，通常是 `2^-128` 量级）。不要自己手算桶边界来写查询；用 `histogram_*` 函数。

### 为什么能省序列

```text
经典：10 个桶 → 10 条序列（加 sum/count 共 12 条）
原生：1 条序列（内部含稀疏桶）
```

如果直方图有 10 个标签组合：

```text
经典：10 × 12 = 120 条序列
原生：10 × 1 = 10 条序列
```

省下的不只是存储，还有 head 索引、postings、查询 fan-out 和 remote write 的序列数。

### NHCB：带自定义桶的原生直方图

NHCB（Native Histograms with Custom Buckets）把经典直方图的固定桶转换成原生直方图表示，兼顾：

- 保留原有桶边界（兼容旧 SLO/面板）；
- 用一条序列承载整个分布；
- 支持原生直方图的部分查询能力。

迁移时常见的混合状态：

- 新服务用指数 schema 的原生直方图；
- 老服务继续经典直方图；
- 中间态用 NHCB 把经典桶"原生化"；
- 查询/告警要能同时处理三种形态。

## R.3 抓取协议与开关

原生直方图**不能**用 Prometheus 文本格式表达；它需要 protobuf exposition 或 remote write。抓取侧要点：

```yaml
# 全局/抓取配置中启用原生直方图抓取（名称/默认值随版本变化）
scrape_configs:
  - job_name: app
    scrape_native_histograms: true
    # 3.0 起旧字段 scrape_classic_histograms 更名为
    # always_scrape_classic_histograms
    always_scrape_classic_histograms: false
```

关键：

- 3.8 起原生直方图稳定；3.9 起 `--enable-feature=native-histograms` 是 **no-op**，不要再依赖它；
- 抓取端在 global 或 `scrape_config` 设 `scrape_native_histograms: true`（默认 `false`）；remote write 发送设 `send_native_histograms: true`；
- 客户端/ecosystem 需要支持 protobuf exposition 或 OpenMetrics 的 native histogram 扩展；
- `always_scrape_classic_histograms` 决定是否在原生直方图之外**同时**保留经典桶序列（兼容旧面板，但会增加序列数）；
- 文本格式/老客户端只能发经典直方图；
- 抓取协商失败时，Prometheus 可能退回经典格式，表现为"原生直方图没出现"。

## R.4 查询函数与插值

### histogram_quantile

经典直方图：

```promql
histogram_quantile(
  0.9,
  sum by (le) (rate(http_request_duration_seconds_bucket[5m]))
)
```

原生直方图：

```promql
histogram_quantile(
  0.9,
  rate(http_request_duration_seconds[5m])
)
```

差异：

- 原生直方图不需要 `le`，也不应该 `sum by (le)`；
- 非零桶使用**指数插值**（标准指数 schema）；
- 零桶/NHCB 使用线性插值；
- `φ < 0` 返回 `-Inf`，`φ > 1` 返回 `+Inf`，`φ = NaN` 返回 `NaN`；
- 观测数为 0 时返回 `NaN`；
- NaN 观测计入 count，但不在任何桶中；查询结果可能带 annotation 提示偏差。

### 其他函数

```promql
histogram_count(rate(http_request_duration_seconds[5m]))
histogram_sum(rate(http_request_duration_seconds[5m]))
histogram_avg(rate(http_request_duration_seconds[5m]))
histogram_fraction(0.1, 0.5, rate(http_request_duration_seconds[5m]))
histogram_stddev(rate(http_request_duration_seconds[5m]))
histogram_stdvar(rate(http_request_duration_seconds[5m]))
```

用途：

- `histogram_fraction(0.1, 0.5, v)` 估算落在 [0.1, 0.5] 的观测比例，适合"好请求比例"型 SLI；
- `histogram_avg` = `histogram_sum / histogram_count`；
- `histogram_stddev/stdvar` 用于分布离散度；
- 这些函数在 3.x 中的稳定性/实验状态随版本变化，规则上线前要在目标版本验证。

### 与经典桶混用的陷阱

- 对原生直方图写 `sum by (le)`：没有 `le` 标签，结果为空/不符合预期；
- 对经典桶调用 `histogram_count`：经典桶没有原生直方图结构，函数无法工作；
- 同一个指标同时有经典和原生序列：`histogram_quantile` 可能对两种输入分别求值，结果重复/混淆；
- recording rule 里混用两种形态：下游面板要能区分。

## R.5 存储与成本

原生直方图的存储特点：

- 一条序列内部包含稀疏桶；桶数随数据分布变化；
- schema 越高，桶越细，存储/查询成本越高；
- 零桶/负桶/正桶分别编码；
- exemplar 仍按样本附加，数量受限制；
- head 内存、chunk 编码、remote write 带宽都受益于"序列数减少"，但单样本体积变大。

成本判断不能只看序列数：

```text
成本 ≈ 序列数 × 每序列开销 + 样本数 × 每样本桶开销
```

对高基数、宽动态范围的直方图，原生通常更省；对桶很少、标签组合很少的直方图，经典可能更简单。

## R.6 Remote Write 2.0 协议

### 与 1.0 的差异

| 维度 | RW 1.0 | RW 2.0 |
|---|---|---|
| protobuf message | `prometheus.WriteRequest` | `io.prometheus.write.v2.Request` |
| Content-Type | `application/x-protobuf` | `application/x-protobuf; proto=io.prometheus.write.v2.Request` |
| 版本头 | `X-Prometheus-Remote-Write-Version: 0.1.0` | `X-Prometheus-Remote-Write-Version: 2.0.0` |
| 字符串复用 | 无 | `symbols` 符号表 |
| metadata | 可选/有限 | 每个 series 附带 type/help/unit |
| native histogram | 有限 | 原生支持 |
| exemplar | 支持 | 支持 |
| created/start timestamp | 有限 | 明确字段（版本间有重命名） |
| 响应 | 2xx | 2xx + 写入计数头 |

### 符号表

RW 2.0 的核心优化是 `symbols`：

- 所有标签名/值、metadata 字符串、exemplar 标签都引用符号表索引；
- 符号表必须去重；
- 第一个元素必须是空字符串；
- 引用越界视为无效请求，接收端应返回不可重试的 4xx。

这让重复的标签名/值只传一次，显著降低大 batch 的带宽。

### 消息结构（简化）

```protobuf
message Request {
  repeated string symbols = 1;
  repeated TimeSeries timeseries = 2;
  repeated Metadata metadata = 3;
}

message TimeSeries {
  repeated uint32 labels_refs = 1;
  repeated Sample samples = 2;
  repeated Histogram histograms = 3;
  repeated Exemplar exemplars = 4;
  Metadata metadata = 5;
}

message Sample {
  double value = 1;
  int64 timestamp = 2;
  int64 start_timestamp = 3;
}

message Histogram {
  int64 timestamp = 1;
  int64 start_timestamp = 2;
  HistogramProto histogram = 3;
}
```

字段编号/命名以官方 spec 为准；不同 RC 版本有过重命名（例如 created timestamp → start timestamp），发送端/接收端要匹配。

### 发送端配置

```yaml
remote_write:
  - url: https://mimir.example/api/v1/push
    protobuf_message: io.prometheus.write.v2.Request
    send_native_histograms: true
    send_exemplars: true
    headers:
      X-Scope-OrgID: platform
    queue_config:
      capacity: 10000
      max_shards: 20
      max_samples_per_send: 2000
```

要点：

- `protobuf_message` 选择 2.0；接收端必须支持；
- metadata 在 2.0 中总是发送；
- `send_native_histograms`/`send_exemplars` 仍分别控制；
- 队列/重试/去重语义与 1.0 类似，但 payload 不同；
- 接收端成功响应会带 `X-Prometheus-Remote-Write-Samples-Written`、`-Histograms-Written`、`-Exemplars-Written`，用于核对是否真的写入。

### 兼容性

- 1.0 接收端不能解析 2.0 body；发送端要按接收端能力选择；
- 2.0 的低字段编号与 1.0 保留字段可能让旧解码器把 2.0 请求读成"空请求"；必须显式协商，不能靠自动探测；
- 中间代理要透传 `Content-Type` 的 `proto` 参数和版本头；
- 云托管/后端的 RW2 支持进度不同，迁移前查文档/实测。

### R.6.1 接收端现实检查

RW2 的规范状态仍是 `2.0-rc.4`（2026-11），不同接收端的支持程度差异很大。上线前逐项确认：

| 能力 | 检查方式 | 常见问题 |
|---|---|---|
| 接受 2.0 body | 发送端 `protobuf_message: prometheus.WriteRequest` 2.0 后看响应头/错误 | 旧接收端把 2.0 读成空请求，返回 200 但没写入 |
| 写入计数 | `X-Prometheus-Remote-Write-Samples-Written` 等响应头 | 缺失或为 0，说明接收端没真正消费 |
| 原生直方图 | 发送 `send_native_histograms: true` 后查接收端序列 | 接收端接受请求但丢弃 histogram 字段 |
| exemplar | 发送 `send_exemplars: true` 后查 exemplar API/存储 | 接收端没有 exemplar 存储，静默丢弃 |
| 元数据 | 查接收端 metadata | metadata 未持久化，重启后丢失 |
| 重试/幂等 | 注入 5xx/超时，观察是否重复写入 | 重试导致重复样本或计数翻倍 |
| 代理/网关 | 检查 `Content-Type: application/x-protobuf;proto=...` 与版本头 | 代理改写/剥离头，接收端解析失败 |

排查顺序：

1. 发送端看 `prometheus_remote_storage_*` 队列指标：`succeeded`、`failed`、`retried`、`dropped`；
2. 接收端看写入计数与错误日志；
3. 用最小 payload（1 条样本、1 个 histogram、1 个 exemplar）分别验证；
4. 对比响应头计数与实际查询结果；
5. 再逐步放大到生产流量。

RW2 的失败语义与 1.0 类似：4xx 通常不可重试（配置/协议错误），5xx/超时可重试；但"接收端返回 200 却丢弃字段"是最危险的情况，必须用响应头 + 查询双重验证。迁移阶段建议保留 1.0 发送端作为回滚路径，直到接收端的 RW2 写入计数、查询结果和告警都稳定。

## R.7 迁移路线

### 阶段 1：只开 RW2，不发原生直方图

- 发送端 `protobuf_message` 切 2.0；
- 接收端支持 2.0；
- 继续发经典直方图/普通样本；
- 验证写入计数、查询结果、告警无变化。

### 阶段 2：原生直方图与经典并存

- 客户端开始暴露原生直方图；
- 抓取端开启 native histogram；
- `always_scrape_classic_histograms: true` 保留经典桶，供旧面板；
- 新面板/告警逐步切到 `histogram_quantile` 原生写法；
- 监控序列数/存储/查询延迟。

### 阶段 3：关闭经典桶

- 确认所有面板/规则/下游都支持原生；
- 关闭 `always_scrape_classic_histograms`；
- 观察序列数下降、查询语义变化；
- 保留回滚开关。

### 迁移检查表

- [ ] 客户端库支持原生直方图；
- [ ] 抓取端 `scrape_native_histograms: true`（3.9+ 旧 flag 已无效）；
- [ ] 抓取协议协商成功；
- [ ] 接收端支持 RW2 + native histogram；
- [ ] `histogram_quantile` 查询改写并验证；
- [ ] 旧面板有兼容层（经典桶/NHCB/recording rule）；
- [ ] 序列数/存储/查询延迟有对比；
- [ ] 回滚路径可用（关闭 native、回到经典桶）。

### R.7.1 原生直方图的 SLO/告警配方

直方图最终要落到 SLI/SLO 上。经典直方图与原生直方图的写法不同，迁移时不能只改指标名：

**经典直方图**（有 `_bucket`/`le`）：

```promql
# 300ms 以内的请求比例（好事件比例）
sum(rate(http_request_duration_seconds_bucket{le="0.3"}[5m]))
/
sum(rate(http_request_duration_seconds_count[5m]))
```

**原生直方图**（无 `_bucket`/`le`）：

```promql
# histogram_fraction(lo, hi, v)：落在 [lo, hi) 的观测比例
histogram_fraction(0, 0.3, rate(http_request_duration_seconds[5m]))

# P99 延迟
histogram_quantile(0.99, rate(http_request_duration_seconds[5m]))

# 平均延迟（sum/count）
histogram_sum(rate(http_request_duration_seconds[5m]))
/
histogram_count(rate(http_request_duration_seconds[5m]))
```

基于 SLO 的 burn-rate 告警（目标 99.9%，300ms 内为好事件）：

```promql
# 1h 错误预算燃烧率；>14.4 对应 1h 内烧掉 2% 月预算
(
  1 - histogram_fraction(0, 0.3, rate(http_request_duration_seconds[1h]))
) / (1 - 0.999) > 14.4
```

注意：

- `histogram_fraction` 只对原生直方图有效；经典桶用上面的 `le` 比例写法；
- 对原生直方图写 `sum by (le)` 不会得到预期结果（没有 `le` 标签）；
- `rate()` 作用在原生直方图上会按桶/计数分别做重置补偿；不要先 `sum` 再 `rate` 成混合类型；
- NHCB（自定义桶原生直方图）与经典桶共存时，查询要明确走哪条路径；`always_scrape_classic_histograms` 是过渡期的兼容开关，不是长期方案；
- 规则里用 `smoothed` 时配组级 `query_offset`（至少一个抓取间隔），否则窗口末端没有未来样本可插值；
- burn-rate 告警仍建议多窗口（1h/5m、6h/30m），单窗口容易在流量突变时误报/漏报。

迁移验证时，至少对同一段时间用经典/原生两套查询对比分位数与好事件比例；差异超过桶宽/误差预期时，先查桶布局和 `histogram_fraction` 的边界语义。

## R.8 容量与验证

验证原生直方图是否真的生效：

```bash
# 看序列形态：原生直方图通常没有 _bucket 后缀
curl -s 'http://prometheus:9090/api/v1/query?query=http_request_duration_seconds' | jq

# 看 metadata/类型
curl -s 'http://prometheus:9090/api/v1/metadata?metric=http_request_duration_seconds' | jq

# 看抓取是否协商到 native
curl -s 'http://prometheus:9090/api/v1/targets' | jq '.data.activeTargets[].lastError'
```

容量对比：

```text
迁移前：序列数 S_classic，每序列桶数 B
迁移后：序列数 S_native，每序列桶数随分布变化
```

不要只看序列数下降，还要看：

- 每样本字节数；
- head 内存；
- 查询 fan-out/延迟；
- remote write 带宽；
- 接收端索引/查询成本。

## R.9 Mini-Prometheus 的边界

Mini 只实现经典直方图：

- exposition 解析 `_bucket`/`_sum`/`_count`；
- `histogram_quantile` 按 `le` 分组、单调修正、线性插值；
- 不解析 protobuf exposition；
- 不实现 native histogram 数据模型；
- remote write 只实现 1.0 子集（JSON/protobuf+Snappy），不支持 RW2 符号表/原生直方图。

这是有意的教学边界：先用经典直方图讲清"分位数是插值近似"，再用本附录讲"新模型如何改变成本与语义"。不要把 Mini 的 `histogram_quantile` 行为当成原生直方图的规范。

## 本章小结

- 原生直方图用一条序列承载稀疏指数桶，schema 决定分辨率；零桶/正负桶分别编码。
- 原生直方图不能走文本格式，需要 protobuf exposition/remote write；3.8 稳定、3.9 起旧 flag 无效，抓取需 `scrape_native_histograms`，RW 发送需 `send_native_histograms`。
- `histogram_quantile` 对原生直方图使用指数插值；不要写 `sum by (le)`，也不要和经典桶混用。
- RW2 用 `io.prometheus.write.v2.Request`、符号表、metadata、native histogram/exemplar 支持；版本头/Content-Type 必须协商。
- 接收端支持矩阵不一致：用响应头写入计数 + 查询结果验证，警惕"200 但静默丢 histogram/exemplar"。
- SLO 配方要用 `histogram_fraction`（原生）或 `le` 比例（经典），不要混写；burn-rate 仍用多窗口。
- 迁移分阶段：先 RW2、再原生/经典并存、最后关闭经典桶；每阶段都要有回滚和容量对比。
- Mini 只覆盖经典直方图与 RW1 子集，不能用来推断生产原生直方图行为。

## 延伸阅读

- Prometheus 官方 native histograms 规范与 `histogram_*` 函数文档。
- Prometheus Remote Write 2.0 规范与 `remote_write` 配置。
- Prometheus 3.0 release notes（native histogram/OTLP/UTF-8 的默认状态）。
- 本教程第 4、7、18、20 章与附录 O（PromQL 高级函数）。
