# 第 38 章 Mini 实现（七）：PromQL 求值器

## 38.1 Engine 接口

```python
engine = Engine(querier, lookback_ms=300_000, default_step_ms=60_000, max_samples=50_000_000)
result = engine.query("rate(x[5m])", timestamp=1_700_000_000_000)
range_result = engine.query_range("x", start, end, step)
```

`Querier` 只需要 `select(matchers, mint, maxt)`；`TSDB` 和 `InMemoryQuerier` 都实现它。

## 38.2 求值状态

`_EvalState`：

```python
start, end          # 查询范围（用于 @ start()/end()）
warnings            # 函数告警
samples, max_samples
```

每个查询一个 state；子查询复用同一个 state（累计样本数）。

## 38.3 即时选择器

```python
evaluation_time = at if at else ts
evaluation_time -= offset
mint = evaluation_time - lookback_ms
maxt = evaluation_time
series = querier.select(matchers, mint + 1, maxt)  # lookback 左开
for labels, samples in series:
    latest = 最新的 <= maxt 的样本
    if latest is None or latest.timestamp <= mint: skip
    if is_stale(latest.value): skip
    emit(labels, latest.value, timestamp=ts, source=latest.timestamp)
```

输出时间戳是查询时间 `ts`，source timestamp 用于 `timestamp()`。排序按 labels，保证结果稳定。

## 38.4 区间选择器

```python
evaluation_time = at if at else ts
evaluation_time -= offset
mint = evaluation_time - range
maxt = evaluation_time
series = querier.select(matchers, mint + 1, maxt)  # 区间左开右闭
for labels, samples:
    points = [(t, v) for ... if not is_stale(v) and mint < t <= maxt]
return Matrix(series, range_ms=range, start_time=mint, end_time=maxt)
```

**关键设计**：Matrix 携带 `range_ms`/`start_time`/`end_time`。函数（rate/increase/delta）用这些字段做外推，而不是用 `ctx.timestamp`。

为什么重要：

- `rate(x[5m] offset 1h)` 的有效窗口结束时间是 `t-1h`，不是 `t`；
- `@` 固定时间后同理；
- 如果函数用查询时间外推，结果会偏移。

Mini 的测试 `test_rate`/`test_offset` 覆盖了这一点。

## 38.5 子查询

```python
end = effective_time(node.at, node.offset_ms, ts)
start = end - range
step = node.step_ms or default_step
first = (start // step + 1) * step          # 绝对 step 网格 + 左开
for moment in range(first, end + 1, step):
    value = evaluate(inner, moment)
    append vector/scalar
return Matrix(series, range_ms=..., start_time=start, end_time=end)
```

- 内层必须是 instant vector（解析期检查）；
- 标量内层生成空标签序列；
- 网格按 Unix 毫秒的**绝对** `step` 倍数对齐，不是从 `start` 相对起算；因此 `[1m:1m]` 只产生一个点，`rate()` 会无数据（与 Prometheus 3 一致）。

## 38.6 二元运算

### 标量-标量

```python
value = apply(op, lhs, rhs)
if bool: Scalar(1/0)
else: Scalar(value)
```

### 向量-标量

```python
for item:
    left = scalar if scalar_on_left else item.value
    right = item.value if scalar_on_left else scalar
    if comparison:
        if bool: emit 1/0 (drop name)
        elif true: emit item
    else:
        emit apply(op) (drop name)
```

### 向量-向量

1. 按签名分组（`on`/`ignoring`）；
2. 默认 one-to-one：两边同签名最多一条，否则报错；
3. `group_left`：左侧可多条（many），右侧每个匹配组必须唯一（one）；
4. `group_right`：右侧可多条（many），左侧每个匹配组必须唯一（one）；
5. 结果标签：
   - 算术：左侧标签（drop name）+ group include；
   - 比较无 bool：左侧标签/值；
   - 比较 bool：drop name，值 0/1；
   - set：左侧标签/值。

Mini 的 `_match_pair()` 在比较为假时返回 `None`，调用方过滤。

## 38.7 聚合

```python
groups = {}
for item in vector.items:
    key = aggregation_key(item.labels, grouping, without)
    groups[key].append(item)
for key, items:
    sum/avg/count/min/max/group/stddev/stdvar/...
```

- `by`：只保留列出的标签（丢弃 `__name__`）；
- `without`：删除列出的标签 + `__name__`；
- `topk/bottomk`：输出原始 item（保留标签/名字）；
- `count_values`：输出 `label=格式化值`；
- `limitk/limit_ratio`：按标签哈希选择；
- `quantile`：线性插值。

## 38.8 函数调用

```python
args = [evaluate(arg) for arg in node.args]
ctx = EvalContext(ts, start, end, warnings)
ctx.range_hint = range_from_ast(arg0) or args[0].range_ms
result = FUNCTIONS[node.func].handler(args, ctx)
```

特殊：

- `absent`/`absent_over_time`：先求值，若为空则从 AST 的等值 matcher 合成标签，返回 1；
- `timestamp`：用 source_timestamp；
- `histogram_quantile`：按 `le` 分组、单调修正、插值；
- `first_over_time`/`last_over_time`：保留 `__name__`；其他 `*_over_time` 丢名；
- `ts_of_first_over_time`/`ts_of_last_over_time`：返回样本时间（秒），丢名；
- `integral`：梯形积分，NaN 按 0 处理，单样本返回 0；
- `rate/increase/delta`：用 Matrix 的 end_time/range_ms 外推。

## 38.9 rate 外推实现

```python
def _extrapolated(samples, range_start, range_end, is_counter, is_rate, range_ms):
    result = last - first
    if is_counter:
        for prev, cur in zip(samples, samples[1:]):
            if cur < prev: result += prev
    duration_to_start = (first_t - range_start)/1000
    duration_to_end = (range_end - last_t)/1000
    sampled_interval = (last_t - first_t)/1000
    avg = sampled_interval/(n-1)
    if is_counter and result > 0 and first_v >= 0:
        duration_to_zero = sampled_interval * first_v/result
        if duration_to_zero < duration_to_start:
            duration_to_start = duration_to_zero
    if duration_to_start >= avg*1.1: duration_to_start = avg/2
    if duration_to_end >= avg*1.1: duration_to_end = avg/2
    factor = (sampled_interval + duration_to_start + duration_to_end)/sampled_interval
    if is_rate: factor /= range_ms/1000
    return result * factor
```

与 Prometheus `extrapolatedRate` 对齐。测试覆盖线性序列、重置、边界空白。

## 38.10 histogram_quantile

- 按 `le` 分组（去掉 `le` 和 `__name__`）；
- 排序、检查 `+Inf`；
- 单调修正；
- `rank = q * total`；
- 只在有限桶中查找 rank；
- 落在 `+Inf` 桶时返回**次高有限桶的上界**（不是 `+Inf`）；
- 缺 `le`/非单调/总数为 0/只有 `+Inf` 桶的行为。

Mini 的 `_bucket_quantile()` 与测试覆盖 q=0/0.5/0.9/0.99、`+Inf` 桶、缺 `+Inf`、总数为 0、只有 `+Inf` 桶。

## 38.11 query_range

```python
timestamps = range(start, end+1, step)
for ts:
    value = evaluate(expr, ts)
    Scalar → labels 空
    Vector → 按 labels 收集
    Matrix → 报错
return Matrix(series)
```

- 输出点时间戳是 step 时间；
- 不强制包含 end（除非 step 整除）；
- 标量查询返回无标签序列；
- 字符串查询报错。

## 38.12 内存限制

`state.count()` 累计：

- selector 选中的样本；
- matrix 点数；
- 函数结果；
- 聚合结果。

超过 `max_samples` 抛 `PrometheusError(EXECUTION)`。这是教学版保护；真实 Prometheus 有更细的统计/取消。

## 38.13 测试

`tests/test_promql_eval.py`（60+ 用例）：

- 选择器/lookback/stale；
- rate/increase/irate/delta/resets/changes；
- 聚合/分位数/stddev；
- 标量/向量运算；
- 匹配/group/set；
- 子查询/@/offset；
- absent/histogram/label_replace/label_join；
- query_range；
- 边界/错误。

## 本章小结

- 选择器输出评估时间戳，source timestamp 供 `timestamp()`。
- Matrix 携带窗口边界，rate 外推依赖它。
- 二元运算的匹配/基数/标签规则是求值器最复杂的部分。
- 聚合丢名，topk 保留；histogram 分位数是插值。
- max_samples 是教学版内存保护。

## 延伸阅读

- 本教程第 7、8、17 章（PromQL 语义/执行）。
- `mini_prometheus/promql/engine.py`、`functions.py`。
- 真实 Prometheus `promql/engine.go`。
