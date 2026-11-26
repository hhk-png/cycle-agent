# 附录 AF Prometheus 3.x UTF-8 指标名与转义完全指南

> 第 3、20 章介绍了 UTF-8 名称的动机；本附录给出可直接落地的语法、转义协商、Mini 实现和迁移检查表。核心结论先放在前面：**Prometheus 3.0 起指标名和标签名都可以是任意 UTF-8，但"能存"不等于"能查、能写、能迁移"；不在旧字符集里的名字必须用引号，跨系统边界还要协商转义方案。**

## AF.1 两套字符集：legacy 与 UTF-8

Prometheus 数据模型同时定义了两套"合法名字"：

| 类别 | legacy（无需引号） | UTF-8（必须引号） |
|---|---|---|
| 指标名 | `[a-zA-Z_:][a-zA-Z0-9_:]*` | 任意非空 UTF-8 |
| 标签名 | `[a-zA-Z_][a-zA-Z0-9_]*` | 任意非空 UTF-8 |
| 标签值 | 一直允许任意 UTF-8 | 同左 |

几个容易忽略的边界：

- `__name__` 仍是保留标签，用来存指标名；`__` 前缀仍保留给内部标签。
- 空标签值等于"标签不存在"，UTF-8 名称不改变这条语义。
- 名字必须是非空、合法的 UTF-8；孤立代理项（lone surrogate）不是合法 UTF-8。
- 冒号 `:` 在 legacy 指标名里合法，但在标签名里不合法；UTF-8 标签名用引号后可以包含冒号。
- 指标名和标签名区分大小写；`http.requests` 与 `HTTP.Requests` 是两条序列。

为什么不全放开成"点号"？因为点号在 PromQL、exporter、Grafana 变量、URL、日志管道里到处有特殊含义；Prometheus 3 选择"保留字节、查询加引号"，而不是在摄入时静默改写。这个设计让 OTel 的 `http.server.request.duration` 可以原样落地，但也把兼容性责任交给了生态。

## AF.2 PromQL 中的引号语法

经典写法不变：

```promql
http_requests_total{job="api",method="GET"}
```

非 legacy 指标名必须放在花括号里，作为第一个元素，且不带 `=`：

```promql
{"http.server.request.duration", job="api"}
```

非 legacy 标签名也要加引号：

```promql
{"http.server.request.duration", "service.name"="checkout", "http.request.method"="GET"}
```

聚合的分组标签同样支持引号：

```promql
sum by ("service.name", "k8s.namespace.name") (
  {"http.server.request.duration_count"}
)
```

二元匹配的 `on`/`group_left` 也接受引号标签：

```promql
{"http.server.request.duration_count"}
* on ("service.name") group_left ("deployment.environment")
{"service.target_info"}
```

`__name__` 的两种等价写法：

```promql
{"http.server.request.duration"}
{__name__="http.server.request.duration"}
```

注意：

- PromQL 字符串字面量遵循 Go 转义（`\\`、`\n`、`\t`、`\"`、`\xNN`、`\uNNNN` 等）；exposition 的标签值只允许 `\\`、`\n`、`\"`，两套规则不要混用。
- 引号指标名只能出现一次；同时写 `{"a.b", __name__="a.b"}` 会报"指标名重复"。
- 选择器仍要求至少一个不匹配空串的 matcher；`{"a.b"}` 自带 `__name__="a.b"`，满足约束。
- `offset`、`@`、区间选择器放在花括号之后：`{"a.b"}[5m] offset 1h`。
- 记录规则/告警规则的 `record`/`alert` 名字如果含非 legacy 字符，也要按同样规则书写；规则名不是指标名，但查询引用时同样需要引号。

## AF.3 exposition 文本格式

Prometheus 3 的文本 exposition 支持带引号的 family 名和标签名：

```text
# HELP "http.server.request.duration" Request duration in seconds.
# TYPE "http.server.request.duration" histogram
{"http.server.request.duration_bucket", "service.name"="checkout", le="0.1"} 4
{"http.server.request.duration_bucket", "service.name"="checkout", le="0.5"} 8
{"http.server.request.duration_bucket", "service.name"="checkout", le="+Inf"} 10
{"http.server.request.duration_sum", "service.name"="checkout"} 2.5
{"http.server.request.duration_count", "service.name"="checkout"} 10
```

规则：

- 指标名在花括号内时必须是第一个元素，写作 `{"name", label="value"}`，不要写 `"name"{label="value"}`。
- 标签名不在 legacy 集合里时必须加引号；legacy 标签名可加可不加，但同一文件里建议统一。
- `# HELP`/`# TYPE`/`# UNIT` 的 family 名用引号包裹，后面接空格和文本。
- 标签值仍然用双引号并转义 `\\`、`\n`、`\"`。
- OpenMetrics 1.0 的 `# EOF`、exemplar、`_total` 约定不变；exemplar 的标签名同样遵循 UTF-8 规则。

反例（解析器会拒绝）：

```text
http.server.request.duration{job="api"} 1        # 未加引号的非 legacy 指标名
{"http.server.request.duration", job=api} 1      # 标签值未加引号
{"http.server.request.duration" job="api"} 1     # 缺少逗号
{"http.server.request.duration", "job"="api"}    # 缺少样本值
```

## AF.4 抓取时的转义协商

生产者与消费者对 UTF-8 的支持并不同步，所以 Prometheus 在 `Accept`/`Content-Type` 上使用 `escaping` 参数协商名字的传输形式。常见取值：

| `escaping` | 行为 | 示例 `metric.name/with/slashes` |
|---|---|---|
| `allow-utf-8` | 原样传输合法 UTF-8 | `metric.name/with/slashes` |
| `underscores` | 非 legacy 字符替换为 `_` | `metric_name_with_slashes` |
| `dots` | `.` → `_dot_`，已有 `_` → `__` | `metric_dot_name_with_slashes` |
| `values` | `U__` 前缀 + 非 legacy 字符的十六进制码点；单个 `_` → `__` | `U__metric_2E_name_2F_with_2F_slashes` |

未指定 `escaping` 时，`underscores` 是默认行为；显式写 `allow-utf-8` 才能端到端保留原名。

生产含义：

1. **抓取端声明自己能吃什么**。支持 UTF-8 的 Prometheus 3 可以在 `Accept` 里协商 `allow-utf-8`；老版本只懂 legacy，收到 UTF-8 名字会报错或被下游改写。具体默认值随版本/exporter 变化，升级时用抓取页面的 `Content-Type` 和实际名字核对。
2. **exporter 端声明自己能吐什么**。SDK 不一定支持 UTF-8；不支持时用 `underscores`/`dots` 做确定性转换。
3. **转换必须在链路边界一次完成**。抓取端、remote write、长期存储、Grafana 各转一次，会得到不同的名字，查询永远对不上。
4. **`dots` 与 `values` 是可逆方案**。`dots` 保留点号语义但会改变原始下划线；`values` 可无损还原，但名字更长、可读性差。

选择建议：

```text
全链路 Prometheus 3+ / 后端支持 UTF-8     → allow-utf-8
必须兼容 2.x/旧后端/旧 Dashboard          → underscores（最稳，但可能撞名）
需要从名字还原 OTel 原名                  → values（或 dots + 约定）
只在自己可控的 exporter 内部               → legacy 命名，别引入 UTF-8
```

## AF.5 命名迁移与撞名风险

把 `http.server.duration` 转成 `http_server_duration` 时，可能和已有的 `http_server_duration` 撞名；`a.b` 与 `a_b` 也会。撞名不是理论问题：同一标签集、同一时间戳的两条样本会被 TSDB 判为重复，或者两条语义不同的序列被静默合并。

迁移步骤：

1. 盘点现有指标名/标签名，找出所有非 legacy 名字及其来源（OTel、第三方 exporter、业务自定义）。
2. 为每个名字确定唯一目标名；用 `values` 或显式映射表，不要依赖"看起来差不多"的替换。
3. 在抓取边界做一次转换（Collector `transform`/`metricstransform`，或 exporter 配置），并记录映射表。
4. 双写/双查过渡：旧名字与新名字并存一个保留期，Dashboard 和告警逐批切换。
5. 用 `promtool check metrics`、规则单测和真实查询集回归；确认没有 `le`、`quantile`、`job`、`instance` 等保留标签被改写。
6. 最后才删除旧名字；保留映射表作为审计材料。

撞名检测的 PromQL 思路（找出同一目标名下的多个来源）：

```promql
count by ("__name__") (
  count by ("__name__", job, instance) ({__name__=~".+"})
) > 1
```

更直接的办法是在转换工具里维护 `源名 -> 目标名` 的双向表，并在 CI 里跑唯一性检查。

## AF.6 Mini-Prometheus 的实现

Mini 在三个层面实现 UTF-8：

1. **数据模型**：`labels.py` 的 `is_legacy_metric_name`/`is_legacy_label_name` 判断是否需要引号；`Labels` 本身不限制字符集，避免 relabel 的中间结果被误拒。
2. **解析**：
   - `exposition.py` 的 `_scan_label_set` 用"引号后是否跟 `=`"区分引号指标名与引号标签名；
   - `_split_metric_and_text` 支持 `# HELP "name" ...`；
   - `promql/parser.py` 的 `parse_selector` 同时接受 `IDENT` 与 `STRING` 作为标签名，`parse_grouping` 接受引号分组标签。
3. **序列化**：`labels.format_labels` 对非 legacy 名称统一输出 `{"metric", "label"="value"}`，并转义反斜杠、换行和双引号；exposition 回写与 `/federate` 共用这一函数。API JSON 保留原始 key；`/metrics` 的自身指标是 legacy 名称，直接拼接。

对应的回归测试在 `tests/test_utf8_names.py`：

- legacy/UTF-8 分类与格式化；
- exposition 的 quoted metric/label、HELP/TYPE、roundtrip；
- PromQL 选择器、分组、重复指标名、空标签名；
- `sum by ("service.name")` 的端到端求值；
- `label_replace` 生成 UTF-8 标签后再分组。

可以看到 Mini 的选择：**解析严格、存储宽松、输出统一**。严格解析让 exporter 的格式错误尽早暴露；存储宽松让 relabel 可以临时产生中间标签；输出统一保证 API、联邦和日志不会各写一套转义。

## AF.7 兼容性检查表

升级到 UTF-8 前逐项确认：

- [ ] Prometheus server、remote write 接收端、长期存储都支持 UTF-8 或已配置 `escaping`。
- [ ] 所有 exporter/SDK 明确声明 `escaping`，且转换是确定性的。
- [ ] Grafana 版本、变量、面板查询能解析带引号的指标/标签名。
- [ ] 告警规则、记录规则、`promtool`、单元测试、CI lint 都能处理引号语法。
- [ ] URL/日志/审计管道不会把引号或点号转义坏；API 查询参数要做 URL 编码。
- [ ] 名称映射表已评审，且没有 `a.b`/`a_b` 这类撞名。
- [ ] `job`、`instance`、`le`、`quantile` 等约定标签没有被资源属性覆盖。
- [ ] 回滚路径存在：旧名字保留期、旧 server 能读的转义方案、Dashboard 双查。
- [ ] 有真实查询集回归，而不只是"服务能启动"。

## AF.8 常见错误

**错误 1：在 PromQL 里写 `http.server.duration` 不加引号。** 解析器会把它当成 `http` 后跟非法 token，报语法错误。正确写法是 `{"http.server.duration"}`。

**错误 2：把引号指标名写在花括号外。** `"http.server.duration"{job="api"}` 不是 PromQL 语法；引号名必须在 `{}` 内。

**错误 3：只改指标名，不改标签名。** OTel 的 `service.name`、`k8s.pod.name` 也是非 legacy 标签名；查询时必须写 `"service.name"`。很多团队只处理了指标名，结果 group by 永远为空。

**错误 4：在摄入端把点号替换成下划线，却在查询端用点号。** 转换必须在一处完成并记录；不要指望 Prometheus 帮你猜。

**错误 5：以为 UTF-8 名字能自动解决基数问题。** 名字变长了、标签变多了，基数只会更高；UTF-8 是兼容手段，不是治理手段。

**错误 6：忽略 `__name__` 注入。** 从 OTLP/第三方数据导入属性时，必须丢弃 `__name__` 和 `__` 前缀，否则生产者可以覆盖序列身份。Mini 的 `_sanitize_label_name` 和上游 3.10 的修复都做了这件事。

## AF.9 验证命令

```bash
python -m pytest tests/test_utf8_names.py -q
python -m pytest -q
python check_docs.py
```

手工验证：

```bash
python - <<'PY'
from mini_prometheus.exposition import parse_exposition, format_exposition
payload = parse_exposition('{"http.server.duration_bucket", "service.name"="api", le="+Inf"} 4\n')
print(format_exposition(payload), end="")
PY
```

预期输出：

```text
{"http.server.duration_bucket",le="+Inf","service.name"="api"} 4.0
```

## AF.10 延伸阅读

- Prometheus 数据模型：<https://prometheus.io/docs/concepts/data_model/>
- UTF-8 转义方案：<https://prometheus.io/docs/instrumenting/escaping_schemes/>
- PromCon 2024 "Why Not Just Dots? UTF-8 Support in Prometheus 3.0"：<https://promcon.io/2024-berlin/talks/why-not-just-dots-utf8-support-in-prometheus-30/>
- 本教程第 3、5、20、32、37 章；附录 AG（OTLP 资源属性如何映射成 UTF-8/下划线标签）。
