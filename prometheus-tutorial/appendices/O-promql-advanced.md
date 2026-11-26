# 附录 O PromQL 高级函数与查询模式

附录 A 是"查得到"的速查表；本附录解决"查得对、查得省、查得可维护"的问题。PromQL 的函数数量不算多，但真正决定结果正确性的是**类型、边界、标签和代价**：同一个 `rate()` 写法在 15s 抓取和 1m 抓取下结果可能差一倍；同一个 `topk()` 在高基数上可能把 Prometheus 打满；同一个 `label_replace()` 可能悄悄制造百万条新序列。

本附录按"函数族 → 语义 → 模式 → 性能陷阱"组织，所有例子都给出可解释的结果或失败模式。Mini-Prometheus 的支持范围在 O.14 单独列出，避免把教程实现当成完整 PromQL。

## O.1 函数分类与类型签名

PromQL 的值类型只有 scalar、string、instant vector、range vector 四种。函数签名决定参数类型，解析期就会拒绝错误组合。

| 类别 | 代表函数 | 输入 | 输出 | 关键语义 |
|---|---|---|---|---|
| 速率/重置 | `rate`、`increase`、`irate`、`delta`、`idelta` | range vector | instant vector | 外推、重置补偿 |
| 区间聚合 | `avg_over_time`、`count_over_time`、`quantile_over_time`、`mad_over_time` | range vector | instant vector | 窗口内统计 |
| 趋势 | `deriv`、`predict_linear`、`double_exponential_smoothing` | range vector（+ scalar） | instant vector | 线性回归/平滑 |
| 直方图 | `histogram_quantile`、`histogram_fraction`、`histogram_count` | instant vector | instant vector/scalar | 桶插值/计数 |
| 标签 | `label_replace`、`label_join` | instant vector + string | instant vector | 重塑标签身份 |
| 选择/排序 | `sort`、`sort_desc`、`topk`、`limitk` | instant vector | instant vector | 输出裁剪 |
| 缺失 | `absent`、`absent_over_time`、`present_over_time` | vector/range | instant vector | 存在性 |
| 数学 | `clamp`、`round`、`abs`、`ln`、`exp` | instant vector/scalar | 同类型 | NaN/Inf 传播 |
| 时间 | `time`、`timestamp`、`year`、`day_of_week` | 无/instant vector | scalar/instant vector | UTC 时间 |
| 元数据 | `info`（实验） | instant vector | instant vector | 资源标签 join |

实验函数（`info`、`limitk`、`limit_ratio`、`sort_by_label`、`mad_over_time`、`double_exponential_smoothing` 等）需要 `--enable-feature=promql-experimental-functions`。版本升级时要先确认接收端/查询端都开启，否则规则会在加载或求值时报错。

## O.2 标签重塑：label_replace 与 label_join

### label_replace

签名：

```promql
label_replace(v instant-vector, dst_label string, replacement string, src_label string, regex string)
```

行为：

1. 用 `regex` 匹配 `src_label` 的值；Prometheus 的 regex 默认**两端锚定**，等价于 `^(?:regex)$`。
2. 匹配成功时，把 `replacement` 展开后写入 `dst_label`；`$1`、`${name}`、命名捕获组都可用。
3. 匹配失败时，序列原样返回，不报错。
4. 写入空字符串等价于删除该标签；Prometheus 中"空值标签"与"不存在"是同一种身份。

例子：把 `instance="10.0.0.7:9100"` 的主机部分提取出来：

```promql
label_replace(up, "host", "$1", "instance", "([^:]+):.*")
```

因为 regex 锚定，`([^:]+):.*` 必须覆盖整个值；如果你写 `([^:]+)`，它只能匹配到第一个冒号前的部分，但整串不匹配，函数会原样返回，容易误以为"函数没生效"。

例子：把路径中的数字 ID 归一化：

```promql
label_replace(
  http_requests_total,
  "path",
  "/api/v1/users/:id",
  "path",
  "/api/v1/users/[0-9]+"
)
```

这个写法会把所有匹配的路径折叠成同一标签值，**降低基数**；但如果你的本意是"每个用户一条序列"，那它就是错误的方向。

### label_join

签名：

```promql
label_join(v instant-vector, dst_label string, separator string, src_label_1 string, src_label_2 string, ...)
```

行为：

- 按顺序拼接源标签的值，缺失标签按空串处理；
- 结果写入 `dst_label`；结果为空则删除该标签；
- 常用于把 `namespace`/`pod` 拼成稳定的 `workload` 标签，或把多个维度拼成路由键。

```promql
label_join(
  kube_pod_info,
  "pod_key",
  "/",
  "namespace",
  "pod"
)
```

注意：`label_join` 只改变标签，不改变序列数量；如果 `dst_label` 的组合仍然高基数，存储成本不会下降。真正降基数要在 **recording rule 里先聚合**，再写新序列。

### 什么时候不该用标签函数

- 不要在查询时对高基数指标做 `label_replace` 后再 `sum by`：计算量仍然发生在原始序列上。
- 不要把动态值（请求 ID、用户 ID、完整 URL）提取成标签；这会制造新的高基数维度。
- 不要依赖"空值标签"做存在性判断；用 `absent()`/`present_over_time()` 更清晰。

## O.3 缺失与存在性：absent 家族

即时选择器在序列消失后会返回空，而不是 0。要区分"值为 0"和"没有数据"，用：

```promql
# 目标完全消失时返回 1；目标存在但 down 时返回空
absent(up{job="api"})

# 5 分钟内没有任何样本时返回 1
absent_over_time(up{job="api"}[5m])

# 5 分钟内至少有一个样本时返回 1
present_over_time(up{job="api"}[5m])
```

`absent()` 的标签合成规则很重要：它会把选择器中的**等值匹配器**（`=`）变成结果标签；正则/不等匹配器不会合成。例如：

```promql
absent(up{job="api", instance=~"10\\..*"})
```

结果只带 `job="api"`，不带 `instance`。如果你需要完整标签，用 `label_replace` 或 recording rule 补。

组合模式：

```promql
# 目标不存在或 down 都告警，并保留 job 标签
absent(up{job="api"}) or up{job="api"} == 0
```

左边在目标消失时返回 1；右边在目标存在且为 0 时返回 0。两边标签集合可能不同，面板/告警应按 `job` 聚合或补标签。

另一个常见模式是"缺失检测"：

```promql
metric_a unless metric_b
```

表示"有 A 但没有 B 的序列"，适合检测"应该有配套指标但没上报"的场景。注意 `unless` 只做存在性匹配，不做数值比较。

## O.4 排序、TopN 与采样

### sort / sort_desc

```promql
sort(node_memory_MemAvailable_bytes)
sort_desc(sum by (instance) (rate(node_cpu_seconds_total{mode!="idle"}[5m])))
```

规则：

- 只对 instant vector 排序；
- NaN 始终排在最后，`sort_desc` 也不会把 NaN 翻到最前；
- 输出保留原始标签和指标名。

### topk / bottomk

```promql
# 全局 Top 5
topk(5, sum by (instance) (rate(http_requests_total[5m])))

# 每个 namespace 的 Top 3
topk by (namespace) (3, sum by (namespace, pod) (rate(http_requests_total[5m])))
```

关键点：

- `topk` 是"选择"而非"折叠"：输出保留原始序列的标签；
- 没有 `by/without` 时是全局 TopN；
- `topk` 仍然要**先求值所有输入序列**，不会因为 `k` 小就少扫描；
- NaN 被视为最小值，通常落在末尾。

### limitk / limit_ratio

```promql
limitk(10, http_requests_total)
limit_ratio(0.1, http_requests_total)
```

它们是**确定性采样**，按标签哈希选择，不是按值排序：

- `limitk` 选 k 条；
- `limit_ratio(0.1, v)` 选约 10%；
- 负 ratio 选择补集（约 90%），适合做"两个互补采样"；
- 同一个查询在相同输入上结果稳定，适合调试/降载，不适合替代 `topk`。

实验函数需要 feature flag；在生产告警里使用前要确认所有 Prometheus/Thanos/Mimir 查询端都支持。

## O.5 区间聚合函数族

PromQL 的 `*_over_time` 把 range vector 折成 instant vector：

```promql
avg_over_time(queue_depth[5m])
max_over_time(node_memory_MemAvailable_bytes[1h])
count_over_time(up[1h])
last_over_time(metric[5m])
present_over_time(metric[5m])
stddev_over_time(metric[1h])
quantile_over_time(0.9, request_duration_seconds[5m])
mad_over_time(request_duration_seconds[1h])
```

用途与陷阱：

| 函数 | 用途 | 陷阱 |
|---|---|---|
| `avg_over_time` | 平滑 gauge | 对 counter 求平均通常无意义 |
| `max_over_time` | 峰值/容量 | 窗口过长会掩盖当前尖峰 |
| `count_over_time` | 样本数/存活 | 与抓取间隔强相关，不等于请求数 |
| `last_over_time` | 取窗口最后值 | 可能取到 stale 前的旧值 |
| `present_over_time` | 存在性 | 只判断"有没有"，不判断值 |
| `quantile_over_time` | 原始观测分位数 | 与 `histogram_quantile` 的输入不同 |
| `mad_over_time` | 中位数绝对偏差 | 实验函数；对异常值稳健 |

`first_over_time`、`last_over_time`、`ts_of_first_over_time`、`ts_of_last_over_time`、`ts_of_max_over_time`、`ts_of_min_over_time` 在 3.x 中可用于"值 + 发生时间"的联合分析。例如找到磁盘最满的时刻：

```promql
ts_of_max_over_time(node_filesystem_avail_bytes[24h])
```

注意：`ts_of_*` 返回的是 Unix 秒；与 `timestamp()` 的区别是后者作用于 instant vector 的最新样本。

## O.6 趋势、导数与预测

```promql
deriv(node_filesystem_avail_bytes[1h])
predict_linear(node_filesystem_avail_bytes[6h], 4*24*3600)
double_exponential_smoothing(queue_depth[1h], 0.5, 0.5)
```

- `deriv`：窗口内线性回归的斜率，单位是"每秒"；对噪声敏感。
- `predict_linear(v[range], t)`：按当前斜率预测 `t` 秒后的值；`t` 可为负（回看）。
- `double_exponential_smoothing`（旧名 `holt_winters`，3.0 起移除）：带趋势的指数平滑，实验函数；参数是平滑因子，取值不当会过拟合或滞后。

容量预警的经典写法：

```promql
predict_linear(node_filesystem_avail_bytes[6h], 4*24*3600) < 0
```

它回答"按过去 6 小时的趋势，4 天后是否会写满"，比固定阈值更早。但要注意：

- 只在趋势稳定时有意义；业务周期/批处理会造成误报；
- 样本不足或全为 NaN 时返回 NaN，不会触发比较；
- 预测不是保证，阈值仍要结合磁盘绝对余量。

## O.7 数学、钳制与除零

```promql
clamp(v, 0, 1)
clamp_min(v, 0)
clamp_max(v, 100)
round(v, 0.1)
abs(v)
sgn(v)
exp(v) ln(v) log2(v) log10(v) sqrt(v)
```

用途：

- `clamp_min(x, 0)` 把负值抬到 0，适合"速率不可能为负"的场景；
- `clamp(v, 0, 1)` 把比例限制在 [0,1]，避免浮点误差导致 1.0000001；
- `round(v, 0.1)` 用于展示/分桶，不建议在告警里改变原始值；
- `sgn(v)` 返回 -1/0/1，适合判断方向。

除零保护有两种常见写法，语义不同：

```promql
# 分母为 0 时得到 NaN/Inf，用 clamp_min 抬高分母
sum(rate(errors_total[5m])) / clamp_min(sum(rate(requests_total[5m])), 1)

# 分子为空时补 0（更常用于"没有错误就是 0"）
sum(rate(errors_total[5m])) or vector(0)
```

第一种改变分母，会低估比例；第二种只处理"没有序列"，不处理"有序列但值为 0"。SLO 查询通常需要两者结合，具体见 O.12。

## O.8 时间与日期函数

```promql
time()
timestamp(up)
year() month() day_of_month() day_of_week() day_of_year()
days_in_month() hour() minute()
```

要点：

- `time()` 返回查询求值时间的 Unix 秒；
- `timestamp(v)` 返回样本的**源时间戳**（秒），在 `@`/`offset` 下仍是原始样本时间；
- 日期函数按 UTC 计算；需要本地时区时在展示层转换，不要在 PromQL 里硬编码时区偏移；
- `day_of_week()` 返回 0（周日）到 6（周六）；
- 日期函数不带参数时使用求值时间；带 instant vector 参数时按每个样本的源时间戳计算。

常见模式：只在工作时间告警。

```promql
# 周一到周五 9:00-18:00 UTC；用 bool 把 scalar 比较转成 0/1，
# 再用 vector(1) 变成可与告警向量做 and 的向量。
(告警表达式)
and on() (
  vector(1)
  * (day_of_week() >= bool 1)
  * (day_of_week() <= bool 5)
  * (hour() >= bool 9)
  * (hour() < bool 18)
)
```

注意：

- `day_of_week()`/`hour()` 是 **scalar**；scalar 比较不加 `bool` 会报错，scalar 之间也不能直接 `and`；
- `vector(1)` 把布尔标量变成无标签向量，`and on()` 按空标签匹配，结果保留告警向量的标签；
- 时区是 UTC；本地时区要在展示层处理，或直接用 Alertmanager 的 `time_intervals`（支持 `location`），复杂排班优先用后者。

## O.9 直方图函数族

### 经典直方图

```promql
histogram_quantile(
  0.9,
  sum by (le) (rate(http_request_duration_seconds_bucket[5m]))
)
```

规则回顾：

- 必须存在 `+Inf` 桶；少于两个桶返回 NaN；
- 分位数落在 `+Inf` 桶时，返回**次高有限桶的上界**，不是 `+Inf`；
- 桶内线性插值；桶边界不覆盖 SLO 时，结果只是保守近似；
- 跨实例必须先 `sum by (le)`，不能对分位数求平均。

### 原生直方图

原生直方图一条序列包含 count/sum/桶集合，查询写法不同：

```promql
histogram_quantile(0.9, rate(http_request_duration_seconds[5m]))
histogram_fraction(0.1, 0.5, rate(http_request_duration_seconds[5m]))
histogram_count(rate(http_request_duration_seconds[5m]))
histogram_sum(rate(http_request_duration_seconds[5m]))
histogram_avg(rate(http_request_duration_seconds[5m]))
```

差异：

- 非零桶使用**指数插值**（标准指数 schema），零桶/NHCB 使用线性插值；
- `φ < 0` 返回 `-Inf`，`φ > 1` 返回 `+Inf`，`φ = NaN` 返回 `NaN`；
- 观测数为 0 时返回 `NaN`；
- 部分函数（`histogram_count/sum/avg/stddev/stdvar`）在 3.x 中仍需确认版本/开关。

不要混用经典桶和原生直方图的查询写法：对原生直方图写 `sum by (le)` 不会得到你期望的 `le` 标签；对经典桶直接调用 `histogram_count` 也不会工作。迁移时先看 exposition 的 `# TYPE` 和实际序列形态。

## O.10 info() 与资源标签

`info()` 是 3.x 引入的实验函数，用来把 `target_info` 之类的 info 指标标签附加到业务指标上，替代手写 `group_left` join。

```promql
# 默认识别 target_info
info(rate(http_requests_total[5m]))

# 只要部分标签
info(rate(http_requests_total[5m]), {k8s_cluster_name=~".+"})

# 指定其他 info 指标
info(up, {__name__="build_info"})
```

它解决的问题：OTel 资源属性会随时间变化（pod 重建、版本滚动），手写 join 容易出现 many-to-many；`info()` 会选择每个识别标签组合下最新的 info 样本。

限制：

- 需要 `--enable-feature=promql-experimental-functions`；
- MVP 默认只用 `job`/`instance` 作为识别标签；对 `kube_pod_labels` 这类以 `namespace`/`pod` 为身份的 info 指标，仍要手写 join；
- 高基数 info 指标（例如带 pod UID）会把标签带到结果里，注意基数。

手写 join 的等价写法：

```promql
rate(http_requests_total[5m])
* on (job, instance) group_left (service_version)
  target_info
```

## O.11 子查询与 @ 的高级模式

子查询把 instant vector 表达式变成 range vector：

```promql
# 过去 6 小时，每 5 分钟算一次 1 小时错误率，取最大值
max_over_time(
  (
    sum(rate(http_requests_total{code=~"5.."}[1h]))
    /
    sum(rate(http_requests_total[1h]))
  )[6h:5m]
)
```

在 3.x 中要注意：

- 子查询窗口左开右闭；`[1m:1m]` 只产生一个点，`rate()` 会无数据；
- 内层 step 对齐到 Unix 毫秒的绝对倍数，不是从窗口起点相对起算；
- `@ start()`/`@ end()` 可以固定子查询的求值边界；
- 每个外层点都会重新计算内层表达式，成本与 `range/step × inner_cost` 成正比。

`@` 的典型用途是对齐多个查询：

```promql
sum(rate(http_requests_total[5m] @ end()))
/
sum(rate(http_requests_total[5m] @ end() offset 7d))
```

这样分子和分母都固定在查询结束时间，不会因为两个子查询的求值时刻不同而错位。

## O.12 查询模式库

### SLO 可用性比例

```promql
sum by (service) (rate(http_requests_total{code!~"5.."}[5m]))
/
sum by (service) (rate(http_requests_total[5m]))
```

如果某个服务没有 5xx 序列，分子仍然存在（因为 `code!~"5.."` 会匹配 2xx/3xx/4xx），比例是 1。若分子用 `{code=~"5.."}`，没有错误时分子为空，比值也为空；需要补零：

```promql
(
  sum by (service) (rate(http_requests_total{code=~"5.."}[5m]))
  or
  sum by (service) (rate(http_requests_total[5m])) * 0
)
/
sum by (service) (rate(http_requests_total[5m]))
```

### Burn rate 多窗口

```promql
# 1h 长窗口 + 5m 短窗口，SLO 99.9%
(
  sum(rate(http_requests_total{code=~"5.."}[1h]))
  /
  sum(rate(http_requests_total[1h]))
) / 0.001 > 14.4
and
(
  sum(rate(http_requests_total{code=~"5.."}[5m]))
  /
  sum(rate(http_requests_total[5m]))
) / 0.001 > 14.4
```

长窗口判断"预算消耗是否显著"，短窗口确认"现在仍在发生"，避免历史错误拖住告警。

### 容量预测

```promql
predict_linear(node_filesystem_avail_bytes{job="node"}[6h], 4*24*3600) < 0
```

配合绝对余量：

```promql
node_filesystem_avail_bytes / node_filesystem_size_bytes < 0.1
or
predict_linear(node_filesystem_avail_bytes[6h], 4*24*3600) < 0
```

### TopN 与分组 TopN

```promql
topk(10, sum by (service) (rate(http_requests_total[5m])))
topk by (namespace) (3, sum by (namespace, pod) (rate(container_cpu_usage_seconds_total[5m])))
```

### 变更检测

```promql
changes(process_start_time_seconds[1h]) > 0
resets(http_requests_total[1h]) > 0
```

`changes` 统计值变化次数（NaN 语义要小心）；`resets` 统计 counter 重置次数，适合发现进程重启。

### 缺失检测

```promql
absent(up{job="api"})
absent_over_time(up{job="api"}[10m])
```

### 标签归一化

```promql
label_replace(
  sum by (path) (rate(http_requests_total[5m])),
  "path",
  "/api/v1/users/:id",
  "path",
  "/api/v1/users/[0-9]+"
)
```

先聚合再归一化，避免对原始高基数序列做函数运算。

## O.13 性能与正确性陷阱

1. **`count by (__name__) ({__name__=~".+"})` 很贵**：它会扫描全部序列。生产用 `/api/v1/status/tsdb` 或 recording rule。
2. **`topk` 不减少输入**：它要先求值所有序列，只是减少输出。`limitk` 也减少输出，不减少扫描。
3. **高基数标签上的正则**：`{instance=~".*api.*"}` 会枚举大量 label value；能用等值就用等值。
4. **子查询套子查询**：成本相乘；能写成一条表达式就不要嵌套。
5. **`label_replace` 在 recording rule 里制造新序列**：如果目标标签仍高基数，存储会增长。
6. **`@` 不是缓存**：它只固定求值时间，重复查询仍会重新计算。
7. **`sort` 大向量**：排序本身有成本，面板只展示 TopN 时用 `topk`。
8. **`absent` 与 `absent_over_time` 的标签合成不同**：前者用等值 matcher，后者同样只合成等值；不要假设正则标签会出现在结果里。
9. **实验函数的可移植性**：Thanos/Mimir/VictoriaMetrics 对 `info`、`limitk`、`sort_by_label` 的支持进度不同；跨后端查询前先验证。
10. **`histogram_quantile` 的 `+Inf` 近似**：SLO 落在最高有限桶以上时，返回值是次高桶上界，不是真实分位数；补桶比调查询更重要。

## O.14 Mini-Prometheus 的支持矩阵

Mini 实现了 PromQL 的一个可运行子集，足以演示语义，但不是完整实现。

| 函数/聚合 | Mini | 说明 |
|---|---|---|
| `rate`/`increase`/`irate`/`delta`/`idelta` | 支持 | 含外推、重置补偿 |
| `deriv`/`predict_linear` | 支持 | 线性回归 |
| `double_exponential_smoothing`/`holt_winters` | 支持 | Mini 教学别名；上游 3.0 已移除 `holt_winters`，只保留前者且需实验开关 |
| `resets`/`changes` | 支持 | 含 NaN 边界 |
| `sum/avg/min/max/count/group/stddev/stdvar` | 支持 | 聚合 |
| `topk`/`bottomk`/`quantile`/`count_values` | 支持 | 参数聚合 |
| `limitk`/`limit_ratio` | 支持 | 确定性哈希采样 |
| `*_over_time` 家族 | 支持 | 含 `mad_over_time`、`quantile_over_time`、`first_over_time` |
| `ts_of_first_over_time`/`ts_of_last_over_time` | 支持 | 返回样本时间（秒），丢弃 `__name__` |
| `integral` | 支持 | 梯形积分；NaN 按 0 处理，单样本为 0 |
| `histogram_quantile` | 支持 | 经典直方图；含 `+Inf` 语义 |
| `label_replace`/`label_join` | 支持 | 正则全匹配/替换 |
| `sort`/`sort_desc`/`sort_by_label` | 支持 | NaN 置末 |
| `clamp`/`clamp_min`/`clamp_max`/`round` | 支持 | 数学函数 |
| `time`/`timestamp`/日期函数 | 支持 | UTC |
| `absent`/`absent_over_time` | 支持 | 特殊求值 |
| `histogram_count/sum/avg/fraction` | 不支持 | 原生直方图函数 |
| `info()` | 不支持 | 3.x 实验函数 |
| 原生直方图 | 不支持 | 经典直方图 only |

对照代码时，先看 `mini_prometheus/promql/functions.py` 的 `FUNCTIONS` 注册表和 `mini_prometheus/promql/ast.py` 的 `AGGREGATORS`；以测试 `tests/test_promql_eval.py` 的行为为准，而不是以本表为唯一依据。

## 本章小结

- `label_replace`/`label_join` 改变标签身份；regex 默认全匹配，空值等于删除。
- `absent` 家族区分"值为 0"和"没有数据"；组合 `or`/`unless` 才能覆盖两种故障。
- `topk` 保留原始标签，`limitk/limit_ratio` 是确定性采样，二者都不减少输入扫描。
- `*_over_time` 与 `rate` 的输入/语义不同；不要对 counter 求平均。
- `predict_linear` 适合稳定趋势的容量预警，不适合周期性/突发负载的精确预测。
- 直方图分位数的正确性取决于桶覆盖；原生直方图的查询写法和经典桶不同。
- `info()` 是 3.x 实验函数，仍需 feature flag，且识别标签有限。
- 子查询在 3.x 左开右闭、绝对 step 网格；`[1m:1m]` 可能只有一个点。
- 性能优化的顺序是：减少输入序列 → 缩小时间范围 → 预聚合 → 最后才调硬件。

## 延伸阅读

- Prometheus 官方 "Querying functions" 与 "Operators"。
- Prometheus 3.x 实验函数文档与 `--enable-feature=promql-experimental-functions`。
- 本教程第 6～8 章（PromQL 语义）、第 17 章（执行与优化）、附录 A（速查）。
- `mini_prometheus/promql/functions.py`、`engine.py` 与 `tests/test_promql_eval.py`。
