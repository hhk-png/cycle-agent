# 第 17 章 PromQL 执行模型与查询优化

## 17.1 解析与类型检查

PromQL 查询先经过：

1. 词法分析：识别标识符、数字、duration、字符串、运算符；
2. 语法分析：构造 AST；
3. 类型检查：scalar/string/vector/matrix；
4. 语义校验：函数参数、聚合参数、向量匹配、范围正数。

类型错误在解析期失败，避免运行时才发现"rate 收到 scalar"。

## 17.2 求值模型

查询在时间点 `t` 求值：

```text
eval(expr, t) -> Value
```

递归下降：

- `NumberLiteral` → Scalar；
- `VectorSelector` → 在 `[t-lookback, t]` 找每个序列的最新样本；
- `MatrixSelector` → 在 `[t-range, t]` 收集样本；
- `Call` → 递归求值参数，调用函数；
- `AggregateExpr` → 求值子表达式，按标签分组；
- `BinaryExpr` → 求值两侧，按匹配规则组合；
- `SubqueryExpr` → 在内部时间网格上重复求值。

Range query 对外层每个 step 重复上述过程。

## 17.3 即时选择器的细节

```text
eval_time = at ? at : t
query_time = eval_time - offset
lookback_start = query_time - lookback_delta
select(matchers, lookback_start, query_time)
for each series:
    latest = 最新的 <= query_time 的样本
    if latest < lookback_start: skip
    if latest is stale: skip
    emit(labels, latest.value, timestamp=t, source=latest.t)
```

注意：

- 输出时间戳是查询时间 `t`（API 返回），source timestamp 用于 `timestamp()`；
- `@` 固定 eval_time，`offset` 再偏移；
- stale 标记优先于 lookback；
- 普通 NaN 是合法值，不会跳过。

## 17.4 区间选择器与函数

`MatrixSelector`：

```text
query_time = (at ? at : t) - offset
mint = query_time - range
select(matchers, mint, query_time)
emit series with samples in [mint, query_time]
```

函数拿到的是 Matrix。`rate()` 需要知道窗口边界，因此 Mini 的 `Matrix` 携带 `range_ms`/`start_time`/`end_time`，而不是只用查询时间推算。这解决了 `offset`/`@` 下的外推错误。

## 17.5 聚合求值

```text
input = eval(expr)
groups = {}
for item in input:
    key = group_labels(item.labels)   # by/without
    groups[key].append(item)
for key, items in groups:
    output[key] = aggregate(items)
```

聚合输出时间戳是求值时间，标签是分组标签（不含 `__name__`）。

`topk/bottomk` 特殊：输出原始序列（保留全部标签），不是分组标签。

## 17.6 二元运算求值

### 标量-标量

直接运算，返回 Scalar。比较需要 `bool`（否则解析期报错）。

### 向量-标量

对每个 item 运算：

- 算术：丢弃指标名；
- 比较无 bool：过滤；
- 比较 bool：0/1，丢弃指标名。

### 向量-向量

1. 计算匹配签名（`on`/`ignoring`）；
2. 按签名分组；
3. 检查基数（one-to-one/many-to-one/one-to-many）；
4. 对每对执行运算；
5. 处理指标名和 group 附加标签。

多对多且没有 `group_left/right` 时报错，而不是随便配对。

## 17.7 子查询求值

```text
end = (at ? at : t) - offset
start = end - range
step = resolution or default
for ts in range(start, end+1, step):
    value = eval(inner, ts)
    append value to matrix at ts
```

子查询的代价与 `range/step × inner_cost` 成正比。内层如果是高基数聚合，成本会爆炸。

## 17.8 查询内存与取消

Prometheus 在求值过程中累计样本数：

- 超过 `--query.max-samples` 返回错误；
- 查询 context 可取消（客户端断开/超时）；
- 矩阵/向量/聚合中间结果都占内存；
- 并发查询共享内存上限。

优化：

- 减少命中序列；
- 缩短 range；
- 避免无谓的子查询；
- 用 recording rules 预聚合；
- 把大面板拆成多个小查询。

## 17.9 Prometheus 没有成本优化器

PromQL 执行是"语法树直接求值"，没有：

- 基于统计信息的 join 重排；
- 谓词下推（除了 selector 的 matcher/时间范围）；
- 公共子表达式消除（除非 recording rules）；
- 物化视图。

因此查询性能主要取决于：

1. matcher 选择性；
2. 时间范围；
3. 序列基数；
4. 函数复杂度；
5. 聚合分组数。

## 17.10 常见慢查询模式

```promql
# 全匹配 + 大范围 + 高基数
{job=~".+"}[30d]

# 在超大基数上做正则
{instance=~".*api.*"}

# 子查询套子查询
max_over_time(rate(x[5m])[1h:1m])[7d:1h]

# 高基数 group by
sum by (pod, instance, path) (rate(http_requests_total[5m]))

# 对 summary 分位数聚合
avg(http_request_duration_seconds{quantile="0.9"})
```

## 17.11 优化清单

- 用 `__name__` 等值 + 高选择性标签；
- 缩小时间范围；
- 先过滤后聚合（`sum(rate(x{...}[5m]))` 而不是 `sum(rate(x[5m]))` 后再过滤）；
- 用 recording rules 把高频重查询物化；
- 避免对高基数标签 `group by`；
- 避免在告警规则里做跨天查询；
- 用 `topk`/`limitk` 限制输出；
- 用 `@` 固定时间做对比，避免重复计算不同时间点。

## 17.12 Mini 的执行器

`promql/engine.py`：

- `Engine.query()`：instant；
- `Engine.query_range()`：按 step 循环；
- `_eval_vector_selector()` / `_eval_matrix_selector()`；
- `_eval_binary()` / `_vector_scalar()` / `_vector_vector()`；
- `_eval_aggregate()`；
- `_eval_subquery()`；
- `_eval_call()`；
- `_EvalState`：warnings、sample 计数、max_samples。

Mini 的求值是递归 + Python 对象，性能远低于 Go，但语义与真实 Prometheus 对齐：

- lookback 5m；
- stale 跳过；
- rate 外推；
- 向量匹配；
- 聚合丢名；
- 子查询。

## 17.13 求值器内部的迭代器

真实 Prometheus 的求值器不是"递归返回列表"，而是围绕迭代器构建：

```text
vectorSelector
  → SeriesSet（惰性）
  → 每个 series 的 chunk iterator
  → 按 step 取样本

matrixSelector
  → 每个 series 的 range iterator
  → 函数消费矩阵

aggregation
  → 输入 vector
  → 按分组哈希
  → 输出 vector
```

几个关键点：

- **step 对齐**：range query 的每个 step 独立求值，但 selector 只扫描一次数据，避免重复 IO；
- **@ 固定时间**：`@` 让所有 step 使用同一个 eval time，适合跨面板对比；
- **offset**：在 `@` 之后偏移，语义是"看过去某个时间点"；
- **lookback**：instant selector 在每个 step 向前看 `lookback-delta`（默认 5m）；
- **stale**：遇到 stale NaN 立即跳过，不再回退到更早样本。

Mini 的 `Engine._eval_*` 是递归 + Python 列表，语义对齐但每个 step 重新 `TSDB.select()`；真实实现会复用迭代器/缓存，差距主要在常数和内存。

## 17.14 函数分类与代价

PromQL 函数可以按代价分档：

| 类别 | 例子 | 代价 | 注意 |
|---|---|---|---|
| 标量/数学 | `abs`、`ceil`、`sqrt`、`ln` | 低 | 逐样本 |
| 时间/日期 | `hour`、`day_of_week` | 低 | 依赖时区 |
| 标签变换 | `label_replace`、`label_join` | 低/中 | 正则、字符串分配 |
| 区间聚合 | `avg_over_time`、`max_over_time` | 中 | 窗口内全扫 |
| 速率/增量 | `rate`、`increase`、`irate` | 中 | 外推、counter reset |
| 分位数 | `histogram_quantile` | 中/高 | 桶数 × 序列数 |
| 预测/平滑 | `predict_linear`、`double_exponential_smoothing` | 中/高 | 回归/迭代 |
| 排序/选择 | `topk`、`sort`、`limitk` | 高 | 全量排序/堆 |
| 存在性 | `absent`、`present_over_time` | 低/中 | 依赖序列集 |
| 实验 | `mad_over_time`、`sort_by_label`、`limit_ratio` | 中/高 | 特性开关 |

代价不是绝对的：`label_replace` 在高基数上也会很贵；`topk` 在 `k` 很小时可以用堆，但仍要扫描全部输入。

## 17.15 查询统计与 per-step 分析

range query 的代价可以拆成：

```text
总代价 ≈ Σ_step (命中序列 × 每序列窗口样本 + 函数计算 + 聚合分组)
```

Prometheus 的查询 API 在较新版本返回 `stats`：

```json
{
  "stats": {
    "timings": {
      "evalTotalTime": 0.42,
      "resultSortTime": 0.01,
      "queryPreparationTime": 0.02,
      "innerEvalTime": 0.30,
      "execQueueTime": 0.01,
      "execTotalTime": 0.45
    },
    "samples": {
      "totalQueryableSamples": 1200000,
      "peakSamples": 50000
    }
  }
}
```

解读：

- `totalQueryableSamples` 高：输入太大，优化 matcher/范围/step；
- `peakSamples` 高：中间结果大，检查聚合/排序/子查询；
- `innerEvalTime` 高：函数/子查询重；
- `execQueueTime` 高：并发排队，查 `--query.max-concurrency`。

部分版本提供 per-step stats 的特性开关，可定位"哪个 step 最慢"。没有该开关时，用二分法缩小时间范围/step，或把查询拆成 recording rule 分段。

## 17.16 慢查询诊断流程

```text
1. 确认是 Prometheus 慢，还是 Grafana/网络慢
   - 直接 curl API，看耗时
2. 看查询本身
   - matcher 是否过宽？
   - 时间范围是否过大？
   - step 是否过小？
   - 是否有子查询/正则/排序？
3. 看 stats
   - totalQueryableSamples / peakSamples / timings
4. 看系统
   - CPU/内存/IO、并发查询数、规则求值是否重叠
5. 看存储
   - block 数、head series、postings 大小、磁盘延迟
6. 优化
   - 缩小 matcher/范围、增大 step、预聚合、拆面板
7. 回归
   - 用同一查询对比优化前后 stats
```

常用命令：

```bash
# 直接查 API，排除 Grafana
time curl -sG 'http://prometheus:9090/api/v1/query_range' \
  --data-urlencode 'query=sum(rate(http_requests_total[5m]))' \
  --data-urlencode 'start=2026-11-26T00:00:00Z' \
  --data-urlencode 'end=2026-11-26T01:00:00Z' \
  --data-urlencode 'step=15s'

# 看并发/超时
curl -s http://prometheus:9090/metrics | grep -E 'prometheus_engine_(queries|query_duration)'
```

## 17.17 优化案例：从 30 秒到 300 毫秒

原始查询：

```promql
sum by (pod, path) (
  rate(http_requests_total{path=~"/api/.*"}[5m])
)
```

问题：

- `path` 高基数（含 ID），分组输出爆炸；
- `path=~"/api/.*"` 枚举大量 label values；
- 范围 7 天、step 15s，step 数 4 万；
- 每个 step 重新扫描。

优化：

1. **规范化 path**：在埋点/relabel 阶段把 ID 替换成模板，降低基数；
2. **缩小 matcher**：加 `job`/`cluster`，避免全量；
3. **预聚合**：recording rule 按 `job`/`path` 每分钟算一次；
4. **增大 step**：面板用 1m，而不是 15s；
5. **拆查询**：TopN 用 `topk(10, ...)`，不要返回全部 pod；
6. **固定时间**：跨面板对比用 `@`，避免每个面板算不同时间点。

优化后：

```promql
# recording rule: job:http_requests:rate5m
sum by (job, path) (rate(http_requests_total[5m]))

# 面板查询
topk(10, job:http_requests:rate5m)
```

代价从"原始序列 × 7 天"降到"预聚合序列 × 面板范围"。这不是 PromQL 技巧，而是**数据建模 + 预聚合**。

## 17.18 实验特性与版本差异

PromQL 仍在演进。常见特性开关/版本差异：

- `--enable-feature=promql-experimental-functions`：启用实验函数（如 `mad_over_time`、`sort_by_label`、`limitk`/`limit_ratio`，以版本为准）；
- `--enable-feature=promql-delayed-name-removal`：改变二元运算丢指标名的时机，影响兼容性；
- `smoothed`/`anchored` 区间修饰符：3.16 起稳定，用于改善窗口边界样本；
- 原生直方图函数：`histogram_quantile`、`histogram_count`、`histogram_sum` 等对 native histogram 的支持随版本演进；
- `@`/`offset`/子查询：3.x 语义稳定，但边界（左开右闭）与 2.x 不同；
- UTF-8 指标名：3.0 起支持，查询需引号。

生产建议：

- 先在测试环境验证实验特性；
- 记录版本与 flag；
- 不要在生产依赖未稳定的函数；
- 升级前用 `promtool test rules` 回归规则；
- 对照 release notes 检查语义变更。

## 17.19 Mini 的求值器扩展练习

Mini 的 `promql/engine.py` + `functions.py` 是可扩展的。建议练习：

1. **加一个函数**：实现 `mad_over_time`（中位数绝对偏差），注册到 `FUNCTIONS`，补测试；
2. **加一个聚合**：实现 `limitk`（按标签排序取前 k），注意输出保留原始标签；
3. **加一个修饰符**：实现 `@ start()`/`@ end()`（如果尚未支持），验证 range query 的每个 step；
4. **加 per-step 统计**：在 `_EvalState` 累计每个 step 的样本数，输出到 API；
5. **加原生直方图桩**：定义 `NativeHistogram` 值类型，先只支持 `histogram_count`/`histogram_sum`，再扩展 `histogram_quantile`。

每个练习都要：

- 对照真实 Prometheus 语义（文档/源码/实验）；
- 补边界测试（NaN、空输入、重复标签、负时间戳）；
- 更新 `check_docs.py` 的计数（如果新增测试/章节）。

## 本章小结

- PromQL 是递归求值，没有成本优化器；性能由 matcher/时间/基数决定。
- 即时选择器有 lookback，range selector 没有；stale 优先。
- 聚合/二元运算/子查询的语义细节决定结果正确性。
- max-samples/timeout/concurrency 是保护机制，不是性能优化。
- 优化优先做"减少输入"，再做"预聚合"，最后才考虑硬件。

## 延伸阅读

- Prometheus 源码 `promql/engine.go`、`promql/functions.go`。
- Prometheus 官方 "Querying" 与 "Query optimization" 相关文章。
- 本教程第 38 章（Mini 执行器）。
