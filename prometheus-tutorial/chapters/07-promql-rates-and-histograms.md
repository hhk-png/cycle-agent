# 第 7 章 PromQL 计算：rate、increase 与直方图分位数

## 7.1 为什么 rate 不是简单的除法

最朴素的速率：

```text
(last - first) / (t_last - t_first)
```

但 Prometheus 的 `rate()` 要解决三个问题：

1. 窗口内样本没有覆盖整个区间（首尾有空白）；
2. counter 在窗口内发生重置；
3. 不同抓取间隔、抖动导致采样不均匀。

因此 `rate()` 的语义是"**每秒平均增长率的估计值**"，包含外推和重置补偿。

## 7.2 一个可手算的例子

抓取间隔 15s，counter 序列：

```text
t=0s    0
t=15s   2
t=30s   4
t=45s   6
t=60s   8
```

查询 `rate(c[1m])` 在 `t=60s`：

```text
样本覆盖 0..60s，增长 8
rate = 8 / 60s = 0.1333/s
```

查询 `increase(c[1m])`：

```text
8（一小时内总增量）
```

如果样本是 10s、25s、40s、55s，值 0、15、30、45：

```text
sampled_interval = 45s
avg_between_samples = 15s
duration_to_start = 10s
duration_to_end = 5s
均未超过 1.1 × avg，因此保留
factor = (45 + 10 + 5) / 45 = 60/45
increase = 45 × 60/45 = 60
```

这就是"外推到窗口边界"的效果。若首尾空白超过平均间隔的 1.1 倍，Prometheus 只外推平均间隔的一半，避免过度猜测：

```text
duration_to_start >= 1.1 × avg  →  duration_to_start = avg / 2
```

## 7.3 计数器重置

```text
t=0    10
t=15   12
t=30   2     ← 进程重启
t=45   4
```

增量：

```text
(12-10) + 2（重置后从 0 涨到 2）+ (4-2) = 6
```

Prometheus 的算法：

```text
result = last - first
for each sample:
    if value < previous:
        result += previous
```

得到 `4 - 10 + 12 = 6`。注意它假设重置后从 0 开始；如果进程重启后先写了一个非零值（例如从持久化状态恢复），这个假设会低估增量。

## 7.4 rate vs irate vs increase vs delta

| 函数 | 输入 | 语义 | 适用 |
|---|---|---|---|
| `rate(v[5m])` | counter | 窗口内平均每秒增长率，含外推 | 告警、趋势 |
| `irate(v[5m])` | counter | 最后两个样本的瞬时速率 | 快速变化的图表 |
| `increase(v[5m])` | counter | 窗口内总增量，含外推 | 计费/容量 |
| `delta(v[5m])` | gauge | 窗口首尾差值，含外推 | gauge 变化 |
| `idelta(v[5m])` | gauge | 最后两个样本差值 | 瞬时变化 |
| `deriv(v[5m])` | gauge | 线性回归斜率（每秒） | 趋势预测 |
| `predict_linear(v[5m], 3600)` | gauge | 按斜率预测未来值 | 容量预警 |

`irate()` 只使用最后两个样本，对重置敏感：若最后两个点跨越重置，结果会被截断为"当前值/时间差"，可能严重偏差。告警通常用 `rate()`，因为它更平滑。

## 7.5 直方图分位数

经典直方图：

```text
bucket{le="0.1"} 50
bucket{le="0.5"} 90
bucket{le="1"}   95
bucket{le="+Inf"} 100
```

`histogram_quantile(0.9, ...)`：

```text
rank = 0.9 × 100 = 90
第一个 count >= 90 的桶是 le=0.5
lower=0.1, prev=50
value = 0.1 + (0.5-0.1) × (90-50)/(90-50) = 0.5
```

`histogram_quantile(0.95, ...)`：

```text
rank = 95，落在 le=1 桶
lower=0.5, prev=90
value = 0.5 + (1-0.5) × (95-90)/(95-90) = 1.0
```

`histogram_quantile(0.99, ...)` 的 rank=99，落在 `+Inf` 桶。Prometheus **不会**返回 `+Inf`：由于最高桶没有有限上界，它返回**次高有限桶的上界**（这里是 `le=1`）。这是一个保守近似，也说明：**直方图无法给出最高有限桶以上的精确分位数**；如果 SLO 落在这一区间，应增加覆盖该区间的桶，而不是相信这个近似值。

### 单调性修正

如果桶计数不是单调递增（数据损坏或并发采集），Prometheus 会把当前桶计数抬到不低于前一个桶。Mini 也实现了这个修正。

### 聚合顺序

正确：

```promql
histogram_quantile(
  0.9,
  sum by (le) (rate(http_request_duration_seconds_bucket[5m]))
)
```

错误：

```promql
avg(histogram_quantile(0.9, rate(http_request_duration_seconds_bucket[5m])))
```

后者对分位数求平均，数学上不成立，且会掩盖长尾。

## 7.6 用 rate 还是 increase

- `rate()` 输出单位是"每秒"，适合阈值（错误率、QPS）。
- `increase()` 输出单位是"窗口内总量"，适合"过去一小时新增多少"。
- 两者都做外推，因此不要把 `increase()` 的结果当成精确计数用于计费。
- 计费/审计应使用后端原始计数器差值，并明确窗口边界。

## 7.7 直方图 + rate 的完整告警

```promql
# 5xx 比例
sum(rate(http_requests_total{code=~"5.."}[5m]))
/
sum(rate(http_requests_total[5m])) > 0.05

# P90 延迟超过 300ms
histogram_quantile(
  0.9,
  sum by (le) (rate(http_request_duration_seconds_bucket[5m]))
) > 0.3
```

注意分母为 0 时结果为 NaN/Inf，需要 `and` 保护或使用 `clamp_min`：

```promql
sum(rate(x{code=~"5.."}[5m]))
/
clamp_min(sum(rate(x[5m])), 1)
```

## 7.8 常见错误

- **窗口内样本不足**：`rate()` 返回空。抓取间隔 1m 时用 `[1m]` 只有 1～2 个点。
- **对 gauge 用 rate**：结果可能是负数，语义错误。
- **对 counter 用 delta**：重置时会出现巨大负值。
- **直方图缺 `+Inf`**：分位数为 NaN。
- **对 summary 分位数聚合**：数学错误。
- **忘记 `sum by (le)`**：每个实例一个分位数，无法得到全局分位数。
- **桶边界不覆盖 SLO**：分位数被插值到边界，误导判断。

## 7.9 Mini 的实现位置

- `rate`/`increase`/`delta`/`irate`/`idelta`/`resets`/`changes`：`mini_prometheus/promql/functions.py`
- 外推算法：`_extrapolated()`
- `histogram_quantile`：`_bucket_quantile()`
- 直方图桶解析：按 `le` 分组，`+Inf` 用 `float("inf")`
- 测试：`tests/test_promql_eval.py` 中的 rate/reset/histogram 用例

Mini 的 `rate()` 使用 matrix selector 的**有效结束时间**（考虑 `offset`/`@`），而不是查询时间；这是很多手写实现容易错的地方。

## 本章小结

- `rate()` 是带外推和重置补偿的估计，不是简单除法。
- 边界外推有阈值：空白超过平均间隔 1.1 倍时只补一半。
- `irate` 看最后两点，适合图表不适合告警。
- 直方图分位数是桶内线性插值；落在 `+Inf` 桶时返回次高有限桶的上界，无法精确。
- 聚合必须先 `sum by (le)` 再 `histogram_quantile`。

## 延伸阅读

- Prometheus 官方 "Query functions" 中 `rate`、`increase`、`histogram_quantile`。
- Prometheus 源码 `promql/functions.go` 的 `extrapolatedRate`。
- 本教程第 4 章（直方图）与第 38 章（Mini 执行器）。
