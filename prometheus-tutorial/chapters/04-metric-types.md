# 第 4 章 指标类型：Counter、Gauge、Histogram、Summary 与原生直方图

## 4.1 类型不是存储格式

Prometheus 本身在存储层只看到 float64 样本。指标类型主要表达：

- 客户端如何维护值；
- 查询/告警应如何解释值；
- exposition 中的 `# TYPE` 提示；
- 直方图的桶布局（决定分位数精度）。

类型错误不会让 Prometheus 拒收数据，却会让 `rate()`、`histogram_quantile()` 产生系统性错误。这是最隐蔽的一类监控缺陷。

## 4.2 Counter

Counter 是**单调递增**的累计值，只增不减（进程重启除外）。

```text
http_requests_total{code="200"} 1024
http_requests_total{code="500"} 3
```

正确用法：

```promql
rate(http_requests_total[5m])          # 每秒速率
increase(http_requests_total[1h])      # 一小时内增量
```

错误用法：

```promql
http_requests_total                    # 直接展示累计值，重启后跳变
avg_over_time(http_requests_total[5m]) # 对累计值求平均通常没意义
```

### 重启与重置

进程重启后 counter 归零，下一次抓取会看到值变小。`rate()`/`increase()` 会把"变小"解释为一次重置，并把重置前的值补上：

```text
t0: 100
t1: 120
t2: 5     ← 重启
t3: 15
```

`increase` 的结果是 `(120-100) + 5 + (15-5) = 35`，而不是 `15-100 = -85`。这依赖样本覆盖完整窗口；窗口内样本不足时，Prometheus 会外推（第 7 章）。

Counter 命名建议以 `_total` 结尾。OpenMetrics 要求 counter 的样本名带 `_total`，而 `# TYPE` 行使用不带 `_total` 的名字：

```text
# TYPE http_requests counter
http_requests_total{code="200"} 10
```

## 4.3 Gauge

Gauge 是可增可减的瞬时值：

```text
node_memory_MemAvailable_bytes 1.2e10
queue_depth 42
temperature_celsius 36.5
```

用法：

```promql
queue_depth
max_over_time(queue_depth[1h])
delta(temperature_celsius[1h])   # gauge 的差值，不是 rate
```

常见错误：

- 对 gauge 用 `rate()`：得到的是"每秒变化量"，可能为负，语义通常不对。
- 对 gauge 用 `increase()`：同样不适合。
- 把"累计计数"暴露成 gauge：丢失重置语义，无法正确算速率。

## 4.4 Histogram（经典直方图）

直方图把观测值分到固定桶中。exposition 形式：

```text
# TYPE http_request_duration_seconds histogram
http_request_duration_seconds_bucket{le="0.05"} 24054
http_request_duration_seconds_bucket{le="0.1"} 33444
http_request_duration_seconds_bucket{le="0.25"} 100392
http_request_duration_seconds_bucket{le="0.5"} 129389
http_request_duration_seconds_bucket{le="1"} 133988
http_request_duration_seconds_bucket{le="+Inf"} 144320
http_request_duration_seconds_sum 53423.0
http_request_duration_seconds_count 144320
```

关键规则：

- `le` 表示"小于等于该上界"，桶是**累积**的。
- 必须有 `+Inf` 桶，其值等于 `_count`。
- `_sum` 是观测值总和，`_count` 是观测次数。
- 桶边界由客户端固定，事后不能改变。

### 分位数计算

`histogram_quantile(φ, ...)` 在桶内做线性插值：

```text
rank = φ × total
找到第一个 cumulative_count >= rank 的桶 [lower, upper]
value = lower + (upper - lower) × (rank - prev_count) / (count - prev_count)
```

例子：`le=0.1` 有 50 个，`le=0.5` 有 90 个，总 100。`φ=0.9` 时 `rank=90`，落在 `[0.1,0.5]`：

```text
0.1 + (0.5-0.1) × (90-50)/(90-50) = 0.5
```

注意：

- 分位数是**近似值**，误差由桶边界决定。
- 桶太少，长尾被抹平；桶太密，序列数和存储成本上升。
- 跨实例聚合必须先 `sum by (le)` 再 `histogram_quantile`，不能对分位数求平均。
- `histogram_quantile` 的输入是瞬时向量，通常配合 `rate(..._bucket[5m])`。

```promql
histogram_quantile(
  0.9,
  sum by (le) (rate(http_request_duration_seconds_bucket[5m]))
)
```

### 桶设计

经验法则：

- 覆盖 SLO 边界：如 SLO 是 300ms，必须有 `le="0.3"`。
- 覆盖典型值与长尾：0.005、0.01、0.025、0.05、0.1、0.25、0.5、1、2.5、5、10。
- 不要超过 10～20 个桶；每个桶都是一条序列。
- 单位写进指标名：`_seconds`、`_bytes`，不要用毫秒和秒混搭。

## 4.5 Summary

Summary 在客户端计算分位数：

```text
# TYPE http_request_duration_seconds summary
http_request_duration_seconds{quantile="0.5"} 0.05
http_request_duration_seconds{quantile="0.9"} 0.12
http_request_duration_seconds{quantile="0.99"} 0.4
http_request_duration_seconds_sum 53423.0
http_request_duration_seconds_count 144320
```

特点：

- 客户端维护滑动窗口（如 Go 的 `prometheus.Summary`），直接输出分位数。
- 分位数**不可跨实例聚合**：不能 `sum by (quantile)`，也不能 `avg`。
- 只能查询客户端预先配置的分位数（如 0.5/0.9/0.99）。
- 计算开销在客户端，且窗口语义依赖实现。

适用场景：单实例内部延迟分布、无法预知桶边界、且不需要跨实例聚合。绝大多数服务端指标应优先用 Histogram。

## 4.6 原生直方图（Native Histogram）

经典直方图的桶是固定边界；原生直方图（native histogram）使用**稀疏的指数桶**：

- 桶边界按 `2^(2^-schema)` 增长；
- 只存储有样本的桶；
- 一次抓取只产生一条序列（而不是每个桶一条）；
- 支持动态范围与高精度分位数；
- 可通过 `histogram_count`、`histogram_sum`、`histogram_quantile` 等函数查询。

Prometheus 3.8（2025-11）起原生直方图成为稳定特性；3.9（2026-01）起 `--enable-feature=native-histograms` 已是 no-op。抓取端需要在配置中显式开启 `scrape_native_histograms: true`（默认 `false`，可全局或按 job 设置），remote write 发送还需要 `send_native_histograms: true`。它的优势在高基数、宽动态范围（延迟从微秒到分钟）场景非常明显；代价是协议、存储和查询复杂度上升，且客户端/后端要全链路支持。

Mini-Prometheus **不实现原生直方图**，只实现经典直方图。教程在第 20 章讨论原生直方图的协议与迁移，附录 G 对比生态支持。

## 4.7 Exemplar

Exemplar 是"某个样本的代表性追踪 ID"：

```text
http_request_duration_seconds_bucket{le="0.5"} 129389 # {trace_id="abc123"} 0.42 1700000000.123
```

它把指标与追踪连接起来：在 Grafana 中点击高延迟桶，可以跳到对应 trace。Exemplar 存储量很小（每个序列保留少量），但要求：

- 客户端支持 OpenMetrics 或 Prometheus 原生 exemplar；
- 抓取协议协商到支持 exemplar 的版本；
- 存储层保留 exemplar（Prometheus 默认在 head 中保留一段时间）。

Mini 解析并把 exemplar 挂在抓取目标上，`/api/v1/query_exemplars` 可查询。完整的 exemplar 协议、追踪关联、Grafana 配置与 profiling 见第 44 章、附录 M（Grafana）与附录 V（API）。

## 4.8 类型选择决策树

```text
值是否单调递增（进程内只增）？
  是 → Counter（命名 _total）
  否 → 是否瞬时值？
        是 → Gauge
        否 → 是否要分位数/分布？
              是 → 能否接受固定桶？
                    是 → Histogram
                    否 → 单实例且不需聚合 → Summary
                        否则 → 评估 Native Histogram
```

## 4.9 常见反模式

- **用 gauge 记录请求数**：无法处理重启，`rate()` 无意义。
- **用 counter 记录队列长度**：队列会下降，重置补偿会算出荒谬结果。
- **对 summary 分位数做 `avg`**：数学上错误。
- **直方图没有 `+Inf`**：`histogram_quantile` 返回 NaN。
- **把 `le` 当普通标签过滤**：`{le="0.5"}` 只取单桶，不能直接算分位数。
- **桶边界与 SLO 不匹配**：SLO 300ms，却只有 100ms 和 1s 两个桶。
- **指标名不带单位**：`request_duration` 无法判断毫秒还是秒。

## 4.10 histogram_quantile 的边界与误差

`histogram_quantile` 的公式简单，但边界行为经常被写错：

```text
rank = φ × total
找到第一个 cumulative_count >= rank 的桶 [lower, upper]
value = lower + (upper - lower) × (rank - prev_count) / (count - prev_count)
```

### 4.10.1 φ 越界

- `φ < 0`：返回 `-Inf`；
- `φ > 1`：返回 `+Inf`；
- `φ = NaN`：返回 `NaN`；
- 空向量：返回空。

不要在告警里用 `histogram_quantile(1.2, ...)` 表达"最慢"，它的语义是 `+Inf`，不是"最大值"。

### 4.10.2 落在 +Inf 桶

如果目标分位数落在 `le="+Inf"` 桶，无法在 `[lower, +Inf]` 上做线性插值。Prometheus 的行为是返回**次高有限桶的上界**（例如 `le="10"` 的 10），而不是 `+Inf`。这仍然是近似值：真实分位数可能远大于 10。

这也是"桶上限要覆盖 SLO"的原因：如果 SLO 是 300ms，而最高有限桶是 100ms，P99 落在 +Inf 桶时你只能知道"大于 100ms"，无法判断是否超过 300ms。

### 4.10.3 单调性与重复桶

- 累积桶必须单调不减；客户端通常保证，但手工拼接的 exposition 可能违反；
- `le` 值重复会导致歧义；解析器/查询可能报错或取其中一个；
- 桶边界不要用浮点相等做匹配，`0.1` 在二进制里不是精确值；
- 跨实例聚合必须先 `sum by (le)`，再 `histogram_quantile`；
- 对 `rate(..._bucket[5m])` 聚合时，`le` 标签必须保留。

### 4.10.4 误差来源

- 桶内线性插值假设值均匀分布，真实长尾可能严重偏斜；
- 桶太少：误差大；桶太密：序列/存储成本高；
- 抓取间隔/窗口太短：`rate()` 样本不足，分位数抖动；
- 计数器重置：`rate()` 会补偿，但窗口跨越多次重置时误差增大；
- 跨实例聚合：先聚合桶，不能聚合分位数。

一个实用的检查：

```promql
# 每个桶的增量是否单调
sum by (le) (rate(http_request_duration_seconds_bucket[5m]))
```

如果某个桶的增量出现负值，说明计数器重置/标签冲突/数据质量问题。

## 4.11 Summary 的滑动窗口与聚合限制

4.5 已给出 Summary 的 exposition 与适用场景；这里只展开窗口语义和聚合边界：

关键限制：

- **分位数不可跨实例聚合**：`avg(summary_quantile)`、`sum by (quantile)` 都没有统计意义；
- **只能查询预配置的分位数**：客户端没输出的 φ 无法事后计算；
- **窗口语义依赖实现**：Go 客户端默认滑动窗口（如 10 分钟）和 age bucket 数，窗口外的观测被丢弃；
- **计算成本在客户端**：高基数标签会让每个序列维护一个窗口，CPU/内存上升；
- **重启会重置**：和 counter 一样，进程重启后窗口清空。

可以聚合的是 `_sum` 和 `_count`：

```promql
# 平均延迟可以跨实例聚合
sum(rate(http_request_duration_seconds_sum[5m]))
/
sum(rate(http_request_duration_seconds_count[5m]))
```

但平均延迟会掩盖长尾；要分位数就用 Histogram（或原生直方图）。这也是"服务端指标优先 Histogram"的核心原因。

## 本章小结

- Counter 表达累计，必须用 `rate`/`increase`；Gauge 表达瞬时值。
- 经典直方图是累积桶 + `_sum`/`_count`，分位数是桶内插值近似。
- Summary 分位数不可跨实例聚合，选型时要先问"要不要聚合"。
- 原生直方图用稀疏指数桶解决宽动态范围与高基数问题；3.8 起稳定，抓取需显式开启 `scrape_native_histograms`。
- Exemplar 把指标与追踪连接起来。

## 延伸阅读

- Prometheus 官方文档 "Metric types" 与 "Histograms and summaries"。
- Prometheus 3.x native histogram 设计文档与 `promql` 函数文档。
- 本教程第 7 章（rate 与 histogram_quantile）与第 20 章（3.x/OTel）。
