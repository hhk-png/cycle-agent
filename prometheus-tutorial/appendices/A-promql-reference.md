# 附录 A PromQL 速查

> 本附录是第 6～8 章的压缩版，侧重"写查询时随手可查"：类型、选择器、修饰符、匹配、聚合、函数签名与语义陷阱。版本基线为 3.13 LTS / 3.16；实验函数的开关状态以目标版本的 `CHANGELOG.md` 与 `promtool` 为准。

## A.1 表达式类型与字面量

PromQL 只有四种表达式类型：

| 类型 | 例子 | 说明 |
|---|---|---|
| Scalar | `1`、`-0.5`、`+Inf`、`NaN` | 单个浮点数 |
| String | `"GET"` | 只能作为函数参数（`label_replace` 等），不能独立求值 |
| Instant vector | `up`、`http_requests_total{code="500"}` | 每个序列一个当前样本 |
| Range vector | `up[5m]`、`rate(x[1m])` 的输入 | 每个序列一个时间窗内的样本列表 |

```promql
1                     # scalar
"text"                # string
up                    # instant vector
up[5m]                # range vector
1h30m                 # duration（只能出现在 range/offset/@ 等位置）
+Inf, -Inf, NaN       # 特殊浮点
```

要点：

- Range vector 不能直接参与二元运算，必须先经过 `rate()`/`avg_over_time()` 等函数变成 instant vector；
- String 不能出现在顶层，`"x"` 单独查询会报类型错误；
- `+Inf`/`NaN` 是合法浮点字面量，但比较/排序行为与普通数不同（见 A.9）。

## A.2 选择器与匹配器

```promql
metric
metric{label="value"}
{__name__="metric",label="value"}
metric{label!="value"}
metric{label=~"regex"}
metric{label!~"regex"}
metric{label="a",other=~"b|c"}
{"utf8.metric",label="value"}
{"utf8.metric","utf8.label"="value"}
```

规则：

- 正则匹配是**全匹配**（等价于 `^(?:regex)$`），不是子串匹配；
- 选择器至少要有一个**不匹配空串**的 matcher。`{job=~".*"}` 非法，`{job=~".+"}` 合法但可能昂贵；
- `__name__` 是保留标签，可用 `{__name__=~"job:.*"}` 批量选指标；
- 3.x 支持 UTF-8 指标名/标签名，含点号、非 ASCII 字符时必须用引号；
- 标签值用双引号，转义规则与 Go 字符串一致：`\\`、`\"`、`\n`、`\t`、`\xNN`、`\uNNNN`；
- 同一个选择器里重复的标签名是错误；`__name__` 与花括号外的指标名同时出现时也必须一致。

## A.3 时间修饰符

### A.3.1 offset

```promql
metric offset 5m
metric offset -5m          # 负 offset 表示"看向未来"，慎用
rate(metric[5m] offset 1h)
sum(rate(metric[5m] offset 1d)) / sum(rate(metric[5m] offset 7d))
```

`offset` 修饰整个选择器；在函数内部时写在 range 后面。它改变的是**数据窗口**，不改变查询的求值时间。

### A.3.2 @

```promql
metric @ 1609746000        # Unix 秒
metric @ 2021-01-04T06:20:00Z
metric @ start()           # 仅 range query
metric @ end()             # 仅 range query
rate(metric[5m] @ 1609746000)
```

`@` 把求值时间钉在某个时刻；`start()`/`end()` 只在 `query_range` 上下文有值。`@` 与 `offset` 可以组合，但语义容易混淆，建议先用小查询验证。

### A.3.3 子查询

```promql
rate(metric[5m])[1h:5m]       # 1 小时窗口，5 分钟步长
max_over_time(rate(metric[5m])[1h:])
```

- 语法：`<instant-query>[<range>:<step>]`；
- 省略 step 时使用全局 `evaluation_interval`（不是外层查询的 step）；
- 子查询结果是 range vector，必须再套函数或聚合；
- 3.x 的区间选择是左开右闭，子查询也按绝对 step 网格对齐；边界用例要用 `count_over_time` 验证。

### A.3.4 时长表达式（3.14+ 默认开启）

```promql
rate(http_requests_total[5m * 2])
rate(http_requests_total[step() * 4])
rate(http_requests_total[min_of(5m, 10m)])
rate(http_requests_total[max_of(5m, range())])
```

- 时长表达式里不再使用聚合函数名 `min()`/`max()`，改为 `min_of()`/`max_of()`（3.13 起）；
- `step()`/`range()` 只在 range query 中有确定值；
- NaN、Inf、负值、越界时长会被拒绝；
- 动态窗口会让记录规则的输出随时间变化，生产规则优先用字面量窗口。

### A.3.5 smoothed / anchored（3.16 稳定）

```promql
rate(http_requests_total[5m] smoothed)
increase(http_requests_total[1h] anchored)
```

- 只作用于 range selector，不能用于子查询；
- `smoothed` 在窗口边界插值，减少"窗口末端样本不足"的锯齿；
- `anchored` 把窗口锚定到固定边界，适合日历/计费周期；
- 在规则中使用 `smoothed` 时，规则组要设 `query_offset`（至少一个抓取间隔），否则窗口末端没有未来样本可插值；代价是告警延迟增加。

## A.4 运算符与优先级

```promql
+ - * / % ^ atan2
== != < <= > >=
and or unless
```

优先级（高→低）：

| 级别 | 运算符 | 结合性 |
|---|---|---|
| 1 | `^` | 右结合 |
| 2 | `* / % atan2` | 左结合 |
| 3 | `+ -` | 左结合 |
| 4 | `== != <= < >= >` | 左结合 |
| 5 | `and unless` | 左结合 |
| 6 | `or` | 左结合 |

建议：复杂表达式用括号；不要依赖 `^` 与一元负号的隐式结合。

`bool` 修饰符：

```promql
x > 0              # 过滤：只保留条件为真的序列
x > bool 0         # 不过滤：所有序列返回 0/1
x > bool on(job) y # bool 必须在 on/ignoring/group 之前
```

## A.5 向量匹配

### A.5.1 默认匹配

```promql
a / b
a / on(job) b
a / ignoring(instance) b
a / on(job) group_left(version) b
a / on(job) group_right(instance) b
```

- 默认按**除 `__name__` 外的所有标签**匹配；
- `on(...)` 只按列出的标签匹配；`ignoring(...)` 排除列出的标签；
- 匹配两侧必须唯一，否则报 many-to-many 错误（集合运算 `and/or/unless` 除外）；
- `group_left`：左侧是"多"的一侧，右侧必须唯一；`group_right` 相反；
- `group_left(version)` 会把右侧的 `version` 带到结果里；同名标签冲突时保留左侧值（`group_left`）；
- 结果标签来自"多"的一侧，指标名通常被丢弃。

### A.5.2 集合运算

```promql
a and b        # 保留同时存在于两侧的 a 序列
a unless b     # 保留不在 b 中的 a 序列
a or b         # a 的全部 + b 中不与 a 匹配的序列
```

集合运算按匹配规则判断"是否存在"，不要求一一对应；`or` 保留左侧标签，右侧补集保留右侧标签。

## A.6 聚合

```promql
sum(x)
sum by (job) (x)
sum without (instance) (x)
avg/min/max/count/group/stddev/stdvar
topk(3, x)
bottomk(3, x)
quantile(0.9, x)
count_values("code", x)
limitk(3, x)          # 实验
limit_ratio(0.5, x)   # 实验，取值 [-1, 1]
```

语义：

- 聚合默认丢弃 `__name__`；`by (__name__)` 显式保留，`without` 总是丢弃；
- `topk/bottomk` 保留原始标签，不做聚合；
- `limitk/limit_ratio` 按序列哈希做确定性采样，适合降采样/探索，不适合精确计数；
- `quantile(φ, x)` 是聚合分位数，不是 `histogram_quantile`；
- `count_values` 把某个标签的值变成新序列的标签值，基数等于该标签的取值数；
- `stddev/stdvar` 是总体标准差/方差（除以 N），不是样本估计；
- `group` 对每组返回 1，常用于"是否存在"；
- NaN 参与 `min/max/avg/sum` 的行为要按版本验证；`topk/bottomk` 通常把 NaN 排在有限值之后。

## A.7 函数速查

> "类型"列中 `v` 是 instant vector，`r` 是 range vector，`s` 是 scalar。函数签名以官方文档为准；标"实验"的函数可能需要 `--enable-feature=promql-experimental-functions`。

### A.7.1 计数器与速率

| 函数 | 签名 | 返回 | 语义 |
|---|---|---|---|
| `rate` | `rate(r)` | v | 每秒平均增长率，处理 counter reset，并按窗口外推 |
| `irate` | `irate(r)` | v | 用最后两个样本算瞬时速率，对突变敏感 |
| `increase` | `increase(r)` | v | 窗口内的外推增量，等价于 `rate * 窗口秒数` |
| `resets` | `resets(r)` | v | 窗口内 counter 重置次数 |
| `changes` | `changes(r)` | v | 窗口内值变化次数（NaN 到 NaN 不算变化） |

`rate`/`increase` 的外推规则：

- 窗口内至少两个样本；
- 用样本间隔估算窗口覆盖比例，外推到整个窗口；
- 外推上限约为平均采样间隔的 1.1 倍；
- counter 重置时把下降视为"从 0 重新开始"，不产生负速率；
- 窗口太短/样本太少时可能返回空或 0，告警要区分"无数据"和"速率为 0"。

### A.7.2 Gauge、趋势与预测

| 函数 | 签名 | 返回 | 语义 |
|---|---|---|---|
| `delta` | `delta(r)` | v | gauge 在窗口内的差值，按窗口外推 |
| `idelta` | `idelta(r)` | v | 最后两个样本的差值 |
| `deriv` | `deriv(r)` | v | 最小二乘线性回归斜率（每秒） |
| `predict_linear` | `predict_linear(r, s)` | v | 按当前斜率预测 s 秒后的值 |
| `double_exponential_smoothing` | `double_exponential_smoothing(r, sf, tf)` | v | 双指数平滑（Holt 线性趋势），`sf`/`tf` 为 0～1；实验函数 |
| `holt_winters` | `holt_winters(r, sf, tf)` | v | 旧名称；3.0 起已移除，继续写会解析失败，改用 `double_exponential_smoothing` |

### A.7.3 区间聚合

| 函数 | 签名 | 说明 |
|---|---|---|
| `avg_over_time` | `avg_over_time(r)` | 窗口平均 |
| `sum_over_time` | `sum_over_time(r)` | 窗口求和 |
| `min_over_time` / `max_over_time` | `min_over_time(r)` | 窗口最小/最大，NaN 处理按版本 |
| `count_over_time` | `count_over_time(r)` | 窗口样本数 |
| `last_over_time` | `last_over_time(r)` | 窗口最后一个样本；保留指标名 |
| `present_over_time` | `present_over_time(r)` | 窗口内存在样本则返回 1 |
| `stddev_over_time` / `stdvar_over_time` | `stddev_over_time(r)` | 窗口总体标准差/方差 |
| `quantile_over_time` | `quantile_over_time(φ, r)` | 窗口分位数，φ 为 scalar |
| `mad_over_time` | `mad_over_time(r)` | 窗口中位数绝对偏差；实验函数 |
| `first_over_time` | `first_over_time(r)` | 窗口第一个样本（3.14 起稳定） |
| `ts_of_first_over_time` | `ts_of_first_over_time(r)` | 窗口第一个样本的时间戳（3.14 实验） |
| `integral` | `integral(r)` | 窗口内对时间积分（3.16 实验） |

### A.7.4 直方图

经典直方图：

```promql
histogram_quantile(0.9, sum by (le) (rate(http_request_duration_seconds_bucket[5m])))
```

| 函数 | 签名 | 说明 |
|---|---|---|
| `histogram_quantile` | `histogram_quantile(φ, v)` | 从 `le` 桶估算分位数；输入必须按 `le` 聚合 |
| `histogram_avg` | `histogram_avg(v)` | 原生直方图平均值 |
| `histogram_count` | `histogram_count(v)` | 原生直方图观测数 |
| `histogram_sum` | `histogram_sum(v)` | 原生直方图观测值之和 |
| `histogram_fraction` | `histogram_fraction(lo, hi, v)` | 原生直方图落在 `[lo, hi)` 的观测比例 |
| `histogram_stddev` | `histogram_stddev(v)` | 原生直方图标准差 |
| `histogram_stdvar` | `histogram_stdvar(v)` | 原生直方图方差 |

要点：

- 经典直方图的 `le` 桶是累积的，必须 `sum by (le)` 后再算分位数；
- `histogram_quantile` 在桶边界之间线性插值，桶设计决定误差；
- 分位落在 `+Inf` 桶时，真实 Prometheus 使用次高有限上界；只有 `+Inf` 桶时结果可能是 `+Inf`/`NaN`，面板要显式处理；
- 原生直方图函数只接受原生直方图样本；与经典直方图混用会返回空/错误。

### A.7.5 标签与缺失

| 函数 | 签名 | 说明 |
|---|---|---|
| `label_replace` | `label_replace(v, dst, replacement, src, regex)` | 用 `src` 匹配 `regex`，把 `replacement` 写入 `dst`；Go 展开 `$1`/`${1}` |
| `label_join` | `label_join(v, dst, sep, src...)` | 把多个源标签用 `sep` 拼成 `dst` |
| `absent` | `absent(v)` | 输入为空时返回一条 1；标签来自等值 matcher |
| `absent_over_time` | `absent_over_time(r)` | 窗口内无样本时返回 1 |
| `info` | `info(v, [label...])` | 关联 `target_info` 等信息指标（实验状态随版本） |
| `sort` / `sort_desc` | `sort(v)` | 按值升/降序；NaN 通常排在末尾 |
| `sort_by_label` / `sort_by_label_desc` | `sort_by_label(v, "l1", ...)` | 按标签自然排序（实验） |

`label_replace` 细节：

- 正则全匹配；不匹配时原序列不变；
- `replacement` 为空会删除目标标签（不是保留空值）；
- 捕获组不存在时展开为空串；
- `$1x` 按最长字母数字名解析为未定义组，不会当成 `$1` + `x`；需要拼接时用 `${1}x`。

### A.7.6 时间、日期与数学

```promql
time()                 # 当前求值时间，Unix 秒
timestamp(v)           # 样本时间戳，Unix 秒
year() month() day_of_month() day_of_week() day_of_year()
days_in_month() hour() minute()
```

日期函数可带参数（如 `year(v)`）或省略（用当前求值时间），按 UTC 计算。

数学函数：

```promql
abs ceil floor exp ln log2 log10 sqrt
sin cos tan asin acos atan
sinh cosh tanh asinh acosh atanh
deg rad pi sgn
clamp(v, min, max)
clamp_min(v, min)
clamp_max(v, max)
round(v)
round(v, 0.1)
```

- 三角函数用弧度；`deg`/`rad` 做角度转换；
- `clamp*` 对 NaN 的传播遵循 Go `math.Max/Min` 语义；不要让 NaN 静默变成边界值；
- `round(v, to_nearest)` 的 `to_nearest` 必须为正；非有限值/0 的行为按版本验证；
- 一元负号、`abs`/`ceil`/`floor`/`round`/`clamp*` 等转换类操作通常丢弃指标名。

### A.7.7 转换与选择

```promql
vector(1)              # scalar -> 单元素 vector
scalar(v)              # 单元素 vector -> scalar；多元素返回 NaN
absent(v)
absent_over_time(v[5m])
```

### A.7.8 实验/新特性（3.10–3.16）

```promql
first_over_time(v[5m])          # 3.14 起稳定
ts_of_first_over_time(v[5m])    # 3.14 实验
integral(v[5m])                 # 3.16 实验
a + fill(0) b                   # 3.10 实验，需 promql-binop-fill-modifiers
a + fill_left(0) b              # 只填左侧
a + fill_right(0) b             # 只填右侧
```

实验特性的语义/开关可能在下个小版本变化：

- 不要写进 page 级告警或 SLO 规则；
- 升级前用 `promtool` 和 staging 查询集回归；
- 记录规则里使用时要固定版本并写测试。

## A.8 指标名丢弃规则

二元运算/聚合/函数的指标名行为经常导致"查询结果对，但面板图例空"：

| 表达式 | `__name__` 结果 |
|---|---|
| `v1 + v2`、`v1 * v2` | 丢弃 |
| `v1 == v2`（过滤比较） | 保留 LHS 名称 |
| `v1 == bool v2` | 丢弃 |
| `v1 == on(...) v2` | 丢弃（只保留 `on` 列出的标签） |
| `v1 == on(..., __name__) v2` | 保留名称 |
| `sum(v)` | 丢弃 |
| `sum by (__name__) (v)` | 保留 |
| `sum without (__name__) (v)` | 丢弃 |
| `abs(v)`、`ceil(v)`、`-v` | 丢弃 |
| `rate(v[5m])` | 保留输入名称（通常仍是 counter 名） |
| `last_over_time(v[5m])` | 保留 |
| `label_replace(v, ...)` | 保留 |

需要稳定名称时：

```promql
# 显式保留
sum by (__name__, job) (rate(http_requests_total[5m]))

# 或写记录规则固定名称
- record: job:http_requests:rate5m
  expr: sum by (job) (rate(http_requests_total[5m]))
```

## A.9 语义陷阱速查

| 写法/现象 | 问题 | 正确做法 |
|---|---|---|
| `rate(gauge[5m])` | gauge 没有 counter 语义 | 用 `delta`/`deriv`/`avg_over_time` |
| `rate(counter)` | 需要 range vector | `rate(counter[5m])` |
| `rate(x[1m])` 在 15s 抓取下 | 窗口只有 4 个点，抖动大 | 窗口至少 4 倍抓取间隔 |
| `increase(x[5m]) / 300` | 与 `rate` 近似但外推边界不同 | 直接用 `rate` 或统一口径 |
| `sum(histogram_quantile(...))` | 分位数不能求和 | 先 `sum by (le)` 再算分位数 |
| `avg(histogram_quantile(...))` | 分位数不能平均 | 用总桶重新算，或记录规则 |
| `histogram_quantile(0.9, buckets)` 未 `sum by (le)` | 每个实例一个结果 | `sum by (le)` 或按需分组 |
| 桶不覆盖 SLO | 分位数落在 `+Inf`，误差大 | 桶上界覆盖 SLO 的 1.5～2 倍 |
| `{job=~".*"}` | 匹配空串，非法/昂贵 | `{job=~".+"}` 或具体 matcher |
| `a / b` 标签不一致 | 空结果/many-to-many | `on`/`ignoring`/`group_left` |
| `a > bool 0` | 所有序列返回 0/1 | 需要过滤时去掉 `bool` |
| `foo[5m] + 1` | matrix 不能参与二元运算 | 先 `rate`/`avg_over_time` |
| `sum(x)` 后依赖指标名 | 聚合默认丢名 | `by (__name__)` 或记录规则 |
| `x / 0` | 产生 `+Inf`/`NaN`，面板断线 | 用 `clamp_min(denominator, ...)` 或 `or vector(0)` |
| `absent(x)` 当"x=0" | `absent` 只在**完全没有序列**时返回 1 | 区分"无数据"与"值为 0" |
| `topk(5, rate(...))` 在 Grafana 多 step | 每个 step 的 TopN 不同，图例跳变 | 先按固定时间窗聚合，或接受动态 TopN |
| `sum(rate(x[5m])) by (pod)` | Pod 名高基数/频繁变化 | 聚合到 service/version，Pod 仅排障 |
| `offset` 与 `@` 混用 | 语义叠加，难以推理 | 拆成子查询或用记录规则 |
| 子查询 step 省略 | 使用全局 evaluation interval，可能过密/过疏 | 显式写 `[1h:1m]` |
| 记录规则用 `smoothed` 但无 `query_offset` | 窗口末端无未来样本 | 组级 `query_offset` ≥ 抓取间隔 |
| 规则用动态时长表达式 | 输出随运行时参数变化 | 生产规则用字面量窗口 |

## A.10 常用查询模板

```promql
# 可用性（按 job）
avg by (job) (up)

# 错误率（5xx / 全部）
sum by (service) (rate(http_requests_total{status=~"5.."}[5m]))
/
sum by (service) (rate(http_requests_total[5m]))

# P90/P99 延迟
histogram_quantile(0.90, sum by (le, service) (rate(http_request_duration_seconds_bucket[5m])))
histogram_quantile(0.99, sum by (le, service) (rate(http_request_duration_seconds_bucket[5m])))

# 按服务聚合
sum by (service) (rate(http_requests_total[5m]))

# Top N 实例（按 CPU 非 idle）
topk(5, sum by (instance) (rate(node_cpu_seconds_total{mode!="idle"}[5m])))

# 预测磁盘何时耗尽
predict_linear(node_filesystem_avail_bytes[6h], 4*24*3600) < 0

# 缺失检测
absent(up{job="api"})

# 同比/环比
sum(rate(http_requests_total[5m]))
/
sum(rate(http_requests_total[5m] offset 7d))

# SLO burn rate（99.9% SLO，1h 窗口）
(
  sum(rate(http_requests_total{status=~"5.."}[1h]))
  /
  sum(rate(http_requests_total[1h]))
) / (1 - 0.999)

# 饱和度（队列深度 / 容量）
max by (queue) (queue_depth) / max by (queue) (queue_capacity)
```

## A.11 Mini-Prometheus 的支持矩阵

`mini_prometheus/promql/functions.py` 注册了 75 个函数、`ast.AGGREGATORS` 注册了 14 个聚合。与上游的差异：

| 能力 | Mini | 说明 |
|---|---|---|
| 选择器/offset/@/子查询 | 支持 | 3.x 左开右闭与绝对 step 网格 |
| `rate`/`increase`/`irate` | 支持 | 含 counter reset 与外推 |
| 经典直方图 `histogram_quantile` | 支持 | 含最高桶边界处理 |
| 原生直方图函数 | 不支持 | Mini 不存原生直方图 |
| UTF-8 引号名称 | 支持 | 指标名/标签名/分组标签 |
| `smoothed`/`anchored` | 不支持 | 文档说明语义，Mini 不实现插值 |
| 时长表达式 | 不支持 | 使用字面量窗口 |
| `first_over_time`、`ts_of_first_over_time`、`ts_of_last_over_time`、`integral` | 支持 | `integral` 为 3.16 实验函数；Mini 不模拟 feature flag |
| 其他 3.x 新函数（`histogram_*`、`info` 等） | 不支持 | 原生直方图/资源标签相关，见附录 O/R |
| `query_offset` | 支持 | 组级 + 全局 `rule_query_offset` |
| 子查询默认 step | 使用规则/查询 step | 与上游的全局默认可能有差异 |

验证命令：

```bash
python -m pytest tests/test_promql_parser.py tests/test_promql_eval.py -q
python -m mini_prometheus.promtool test rules examples/rules/demo_test.yml
```

## A.12 进一步阅读

- Prometheus 官方 "Querying basics"、"Querying operators"、"Querying functions"。
- 本教程第 6～8 章（选择器、速率、聚合与匹配）与第 17 章（执行与优化）。
- 附录 O（高级函数与查询模式）、附录 C（规则库）、附录 AE（3.14–3.16 语义变化）。
