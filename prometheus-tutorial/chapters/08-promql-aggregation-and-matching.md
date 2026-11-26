# 第 8 章 PromQL 聚合、二元运算与向量匹配

## 8.1 聚合运算

聚合把多条序列折叠成更少的序列：

```promql
sum(http_requests_total)
sum by (job) (http_requests_total)
sum without (instance) (http_requests_total)
avg by (job, code) (rate(http_requests_total[5m]))
```

支持的聚合：

```text
sum, avg, min, max, count, group,
stddev, stdvar,
topk, bottomk,
quantile,
count_values,
limitk, limit_ratio
```

### by 与 without

- `by (job)`：只保留 `job`，丢弃其他标签；`__name__` 默认也被丢弃。
- `without (instance)`：保留除 `instance` 和 `__name__` 之外的所有标签。

聚合结果默认没有指标名。但显式写 `sum by (__name__)` 会保留指标名，这是 Prometheus 官方支持的用法；`without` 则始终丢弃 `__name__`。需要稳定保留名字时，仍建议用 recording rule 重新命名。

### topk/bottomk

`topk` 与 `bottomk` 是"选择"而不是"折叠"：

```promql
topk(3, http_requests_total)
topk(2, sum by (instance) (rate(http_requests_total[5m])))
```

- 输出保留原始标签（包括指标名）。
- `k` 是每个分组内的数量；无 `by/without` 时是全局。
- `topk` 和 `bottomk` 都优先返回有限值；只有当有限值不足 k 个时才会选到 NaN。`sort`/`sort_desc` 都把 NaN 放在末尾。

### count_values

```promql
count_values("code", http_requests_total)
```

输出标签 `code="<值的字符串>"`，值为该值出现的次数。适合快速看分布，不适合长期存储（标签值动态）。

### quantile

```promql
quantile(0.9, http_request_duration_seconds)
```

对**每个分组内的标量值**求分位数，和 `histogram_quantile` 不同：前者需要原始观测值向量，后者需要桶。

## 8.2 二元运算优先级

从低到高：

```text
or
and, unless
==, !=, <=, <, >=, >
+, -
*, /, %, atan2
^（右结合）
```

例子：

```promql
1 + 2 * 3 ^ 2      # 1 + (2 * (3^2)) = 19
2 ^ 3 ^ 2          # 2^(3^2) = 512
```

一元负号低于 `^`：`-2^2 = -(2^2) = -4`。

## 8.3 标量与向量

```promql
up * 100
100 - up
up > 0
up > bool 0
```

规则：

- 算术：每个样本与标量运算，**丢弃指标名**。
- 比较（无 `bool`）：过滤，保留满足条件的样本及其值。
- 比较（`bool`）：每个样本变成 0/1，**丢弃指标名**。
- 标量在左在右只影响运算顺序，不影响匹配。

## 8.4 向量与向量：默认匹配

默认匹配规则：

```text
除 __name__ 外的所有标签都相同 → 匹配
```

```promql
sum by (job) (rate(http_requests_total[5m]))
/
sum by (job) (rate(http_requests_total{code=~"5.."}[5m]))
```

两边都只有 `job`，可以一一匹配。

若某一边同一组标签有多条序列，会报：

```text
found duplicate series for the match group ...
```

因为默认是 one-to-one，Prometheus 不知道如何配对。

## 8.5 on 与 ignoring

```promql
a / on(job) b
a / ignoring(instance) b
```

- `on(job)`：只用 `job` 匹配，其他标签不参与。
- `ignoring(instance)`：除 `instance`（和 `__name__`）外都参与。

一对一运算的结果标签也受这两个修饰符约束：`on(job)` 只保留 `job`，`ignoring(instance)` 删除 `instance`，两者都会丢弃 `__name__`。比较运算（过滤与 `bool`）和算术运算使用同一套结果标签规则；`group_left/right` 的 `include` 标签在比较运算中同样生效。

`on`/`ignoring` 不能与 set 运算符的 `group_left/right` 混用（set 运算不允许 group 修饰）。

## 8.6 group_left / group_right

当"多"的一侧需要匹配"一"的一侧时：

```promql
# 每个实例的请求速率，附加 deployment 的 version 标签
rate(http_requests_total[5m])
/ on(job, instance) group_left(version)
  kube_pod_labels{label_version=~".+"}
```

- `group_left(version)`：左侧是多，右侧是一；结果保留左侧标签，并从右侧拷贝 `version`。
- `group_right(instance)`：右侧是多，左侧是一；结果保留右侧标签。
- 括号里是"额外拷贝的标签"，这些标签不能同时出现在 `on(...)` 中。
- 没有 `group_*` 时，多对多会报错。

### 基数方向

```text
group_left  : many (left)  → one (right)
group_right : one (left)   → many (right)
```

记忆方法：`group_left` 表示"以左边为主，右边提供附加标签"。

## 8.7 集合运算

```promql
a and b        # 保留 a 中在 b 里有匹配的序列
a unless b     # 保留 a 中在 b 里没有匹配的序列
a or b         # a 的全部 + b 中不匹配 a 的序列
```

- 匹配默认忽略 `__name__`。
- 结果保留左侧序列的标签和值；`or` 对右侧未匹配序列保留右侧标签。
- `and`/`unless`/`or` 不支持 `group_left/right`。
- 集合运算适合"存在性"过滤，不适合数值计算。

## 8.8 比较的特殊性

```promql
up == 0
up == bool 0
up > 0.5
```

- `up == 0`：只返回值为 0 的序列，值仍是 0，指标名保留。
- `up == bool 0`：返回所有序列，值为 1/0，指标名丢弃。
- 向量-向量比较（无 bool）：保留左侧值，要求匹配成功且比较为真。

比较不满足时不会返回 0，而是"过滤掉"。这经常导致 `count(up == 0)` 与 `count(up == bool 0)` 的差异。

## 8.9 指标名丢弃规则

| 运算 | 指标名 |
|---|---|
| 聚合 | 丢弃 |
| 算术二元 | 丢弃 |
| 比较（无 bool） | 保留左侧 |
| 比较（bool） | 丢弃 |
| 集合运算 | 保留左侧 |
| `group_left/right` 算术 | 丢弃 |
| `topk/bottomk` | 保留 |

需要指标名时，用 `label_replace` 或 `label_join` 显式加回：

```promql
label_replace(rate(http_requests_total[5m]), "__name__", "http_requests:rate5m", "", ".*")
```

## 8.10 子查询与 @ 的组合

```promql
# 过去 6 小时，每 5 分钟计算一次 1 小时错误率，再取最大值
max_over_time(
  (
    sum(rate(http_requests_total{code=~"5.."}[1h]))
    /
    sum(rate(http_requests_total[1h]))
  )[6h:5m]
)
```

注意：

- 子查询内部的时间范围是相对于外层求值点的；
- `@` 可以固定子查询的结束时间；
- 子查询的 resolution 越小，计算越贵；
- 子查询不是"预计算"，每次查询都会重新执行。

## 8.11 常见陷阱

- **在 `sum` 之前丢了标签**：先 `sum` 再 `histogram_quantile` 会丢失 `le`。
- **除零**：分母为 0 得到 NaN/Inf，用 `clamp_min` 或 `and` 保护。
- **标签不对齐**：两边标签集合不同，结果为空；用 `on`/`ignoring` 明确匹配。
- **多对多**：忘记 `group_left/right`，或分组标签选错。
- **`or` 误当加和**：`a or b` 不是 `a + b`，不会相加。
- **`bool` 误用**：告警表达式用 `bool` 会让所有序列都返回值，导致误报。
- **聚合后指标名消失**：需要指标名时用 `label_replace`。

## 8.12 一个完整的 SLO 查询

```promql
# 过去 5 分钟，按服务计算可用性（非 5xx 比例）
sum by (service) (rate(http_requests_total{code!~"5.."}[5m]))
/
sum by (service) (rate(http_requests_total[5m]))
```

如果某些服务没有 5xx 序列，`sum by (service)` 的分子分母仍然对齐（因为分母存在），结果为 1。这是正确行为：没有错误就是 100% 可用。

若分子用 `{code=~"5.."}` 且某服务没有 5xx，分子为空，比值也为空。注意 `or vector(0)` **不能**按服务补零：`vector(0)` 没有 `service` 标签，只会补一条无标签序列。正确做法是用分母的零值序列补齐：

```promql
sum by (service) (rate(http_requests_total{code=~"5.."}[5m]))
or
sum by (service) (rate(http_requests_total[5m])) * 0
```

右侧保留 `service` 标签、值全为 0，`or` 会为左侧缺失的服务补上零。这是 SLO 查询最常见的坑之一。

## 本章小结

- 聚合默认丢弃指标名；`by` 保留列出的标签（包括显式列出的 `__name__`），`without` 删除列出的标签并总是丢弃 `__name__`。
- 默认向量匹配忽略 `__name__`，要求其余标签完全一致。
- 多对多用 `group_left/right`，并明确额外拷贝的标签。
- 比较无 `bool` 是过滤，有 `bool` 是 0/1。
- 集合运算只做存在性，不做数值运算。
- 标签对齐、除零和指标名丢失是绝大多数 PromQL 事故的根源。

## 延伸阅读

- Prometheus 官方 "Operators" 与 "Aggregation operators"。
- 本教程第 6、7 章与第 38 章（Mini 执行器）。
- 附录 A（函数速查）。
