# 第 6 章 PromQL 基础：类型、选择器与时间修饰符

## 6.1 四种表达式类型

| 类型 | 例子 | 用途 |
|---|---|---|
| scalar | `1`、`time()` | 单个浮点数 |
| string | `"hello"` | 函数参数/展示 |
| instant vector | `up` | 每个序列一个当前样本 |
| range vector | `up[5m]` | 每个序列一个时间窗样本列表 |

PromQL 的表达式类型在解析期确定。`rate()` 只接受 range vector，`sum()` 只接受 instant vector，`1 + up` 是 vector，`1 + 2` 是 scalar。类型错误在解析阶段就应报错，而不是运行到一半才失败。

## 6.2 即时向量选择器

```promql
up
http_requests_total
{job="api"}
{__name__=~"http_.*_total", job="api"}
```

规则：

- 至少有匹配器时才能省略指标名；
- 至少有一个匹配器**不匹配空字符串**，否则 `{job=~".*"}` 这类选择器会在解析时被拒绝（报 "vector selector must contain at least one non-empty matcher"），而不是扫描全部序列；
- 标签名/值区分大小写；
- 正则默认全匹配（`^(?:...)$`），不是子串匹配。

匹配器：

```promql
job="api"          # 等于
job!="api"         # 不等于（缺失标签视为 ""）
code=~"5.."        # 正则匹配
code!~"5.."        # 正则不匹配
```

`job!="api"` 会匹配没有 `job` 标签的序列，因为缺失值按 `""` 处理。这是常见误判。

## 6.3 区间向量选择器

```promql
http_requests_total[5m]
http_requests_total{code="500"}[1h]
```

区间向量不能直接用于图表（API 会返回 matrix），只能作为函数参数：

```promql
rate(http_requests_total[5m])
max_over_time(queue_depth[1h])
```

窗口是**左开右闭**的：`[5m]` 在时间 `t` 选择 `(t-5m, t]` 范围内的样本。Prometheus 3.0 起，恰好落在左边界 `t-5m` 的样本会被排除；2.x 则是双闭区间 `[t-5m, t]`。升级到 3.x 后，`count_over_time` 可能少一个点，`[1m:1m]` 子查询甚至只产生一个点，导致 `rate()` 无数据——这是常见的迁移故障。

### 窗口长度的选择

经验：

```text
窗口 >= 4 × scrape_interval
```

抓取间隔 15s，至少用 `[1m]`；抓取间隔 1m，用 `[5m]`。窗口太短会导致样本不足、`rate()` 为空或剧烈抖动；窗口太长会掩盖突发事件。

### 扩展区间选择器：smoothed 与 anchored

Prometheus 3.7 引入了两个区间选择器修饰符（实验），3.16 起稳定并默认开启：

```promql
rate(http_requests_total[5m] smoothed)
rate(http_requests_total[5m] anchored)
```

- **`smoothed`**：在窗口两端做线性插值，需要窗口结束之后还有样本；对抓取抖动/丢抓取更稳，但会"用未来样本"。
- **`anchored`**：用窗口外最近的样本作为锚点，不做外推；结果更确定，但不会补偿丢抓取。
- 两者都只作用于区间选择器；**子查询不支持**扩展修饰符。
- 在 recording/alerting 规则里用 `smoothed` 时，规则求值时刻必须晚于窗口结束，否则没有"未来样本"可插值。给规则组设置 `query_offset`（至少一个抓取间隔）：

```yaml
groups:
  - name: slo
    interval: 1m
    query_offset: 1m
    rules:
      - record: job:http_requests:rate5m
        expr: sum by (job) (rate(http_requests_total[5m] smoothed))
```

`query_offset` 把求值查询时间整体后移，代价是告警延迟增加；先在小范围规则验证，再替换关键 SLO 规则。

## 6.4 时间偏移：offset

```promql
http_requests_total offset 1h
rate(http_requests_total[5m] offset 1h)
```

`offset` 把整个选择器的时间基准向前/向后移动。负 offset（`offset -1h`）表示"看向未来"，在部分版本/场景中可用，但依赖未来数据，通常应避免。

常见用途：

- 与上周同日对比：`sum(rate(x[5m])) / sum(rate(x[5m] offset 7d))`
- 避免抓取延迟导致的窗口错位。

## 6.5 固定时间：@

```promql
http_requests_total @ 1609746000
rate(http_requests_total[5m] @ 1609746000)
http_requests_total @ start()
http_requests_total @ end()
```

`@` 把选择器的求值时间固定到某个 Unix 秒（或查询的 start/end），常用于：

- 复现某个时刻的查询；
- 对齐多个查询的时间点；
- 在 range query 中让某个子表达式只取固定时刻。

`@` 与 `offset` 可以组合：先固定时间，再偏移。

## 6.6 子查询

子查询把任意 instant vector 表达式变成 range vector：

```promql
rate(http_requests_total[5m])[1h:5m]
max_over_time(rate(http_requests_total[5m])[1h:5m])
```

语法：

```text
<instant_query>[<range>:<resolution>]
```

- `range` 是外层窗口；
- `resolution` 是内层求值步长，省略时用全局 `evaluation_interval`；
- 子查询内部不能是 range vector 或 string；
- 内层求值点对齐到 Unix 毫秒的**绝对** `resolution` 倍数，不是从窗口起点相对起算；
- 窗口同样是左开右闭。因此 `foo[1m:1m]` 在 3.x 只产生一个点，`rate()` 会无数据；需要两个点时写成 `foo[2m:1m]`。

子查询的代价是"每个外层点都要重新计算内层表达式"，高基数/长窗口时非常昂贵。能写成一条 PromQL 的，不要用子查询。

## 6.7 查找窗口（lookback）

即时向量选择器不是"取最近一个样本"，而是：

```text
在 (t - lookback_delta, t] 内找最新的样本
```

默认 `lookback_delta = 5m`，通过 CLI flag `--query.lookback-delta` 调整（它不是 YAML 的全局配置字段；Operator 对应 `spec.query.lookbackDelta`）。Mini 为了教学方便额外接受 `global.query_lookback_delta`，但真实 Prometheus 不认这个字段，迁移时要改回 flag/Operator 字段。Prometheus 3.0 起 lookback 也是左开的：恰好落在 `t-5m` 的样本不再算"新鲜"。

后果：

- 目标停止上报后，`up` 等指标在 5 分钟内仍可能返回最后一个值；
- 5 分钟或更久没有样本，instant query 不再返回该序列；
- stale 标记会立即让序列消失，不受 5 分钟限制。

range selector 不受 lookback 影响，只受窗口限制。

## 6.8 转义与 UTF-8

经典写法：

```promql
http_requests_total{path="/api/v1/users"}
```

UTF-8 指标名/标签名需要引号：

```promql
{"http.requests.total", "service.name"="api"}
```

字符串转义：

```promql
{path="/a\"b"}
{path="/a\\b"}
{path="/a\nb"}
```

PromQL 字符串支持 Go 风格转义（`\n`、`\t`、`\\`、`\"`、`\xNN`、`\uNNNN`）。反引号原始字符串不做转义。

## 6.9 常见语法陷阱

- `up[5m]` 合法；`sum(up)[5m]` 非法（range 只能作用于选择器或子查询）。
- `rate(up)` 非法（需要 range vector）。
- `{job="api"}[5m]` 合法；`{job=~".*"}` 单独使用非法（全匹配空串）。
- `foo{__name__="bar"}` 冲突；指标名只能出现一次。
- `foo offset 5m [5m]` 顺序错误；应为 `foo[5m] offset 5m`。
- `@` 只能用于选择器/子查询，不能用于 `(a+b) @ 100`。
- 注释用 `#`，会一直持续到行尾。

## 6.10 用 Mini 观察解析结果

```bash
python - <<'PY'
from mini_prometheus.promql.parser import parse_expr
for q in ["up", "rate(http_requests_total[5m])", "sum by (job) (up)",
          'foo{job="api"} offset 5m @ 1609746000', 'rate(x[5m])[1h:5m]']:
    e = parse_expr(q)
    print(q, "->", type(e).__name__, e.value_type)
PY
```

输出会显示 `VectorSelector`、`Call`、`AggregateExpr`、`SubqueryExpr` 等节点。第 37 章会逐个解释这些 AST 节点。

## 本章小结

- PromQL 有 scalar/string/instant vector/range vector 四类，类型在解析期检查。
- 选择器匹配器区分 `=`/`!=`/`=~`/`!~`，正则全匹配，缺失标签按空串处理。
- range vector 只能作为函数输入；窗口至少覆盖 4 个抓取间隔。
- `offset` 平移时间，`@` 固定时间，子查询把 instant 表达式变成 range。
- lookback 决定 instant selector 的"新鲜度"，stale 标记会覆盖它。

## 延伸阅读

- Prometheus 官方 "Querying basics" 与 "Operators"。
- 本教程第 7、8 章（计算与匹配）和第 37 章（Mini 解析器）。
- 附录 A（PromQL 函数速查）。
