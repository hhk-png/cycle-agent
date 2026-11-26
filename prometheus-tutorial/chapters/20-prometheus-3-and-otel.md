# 第 20 章 Prometheus 3.x、原生直方图与 OpenTelemetry

## 20.1 Prometheus 3.0 的变化

Prometheus 3.0 于 2024 年 11 月发布，主要变化：

1. **UTF-8 指标名/标签名**：支持非传统字符，查询时用引号；经典命名仍是兼容性最好的选择。
2. **原生直方图**：3.8 起稳定；3.9 起 `--enable-feature=native-histograms` 为 no-op，抓取改为显式 `scrape_native_histograms: true`。
3. **OTLP 接收**：`--web.enable-otlp-receiver` 允许直接接收 OTLP metrics。
4. **Remote Write 2.0**：更完整的 metadata/exemplar/native histogram 支持；截至 2026-11 仍是 `2.0-rc.4` 实验规范，发送端/接收端/托管后端的支持程度不一。
5. **scrape_protocols**：显式协商抓取协议。
6. **移除/弃用一批旧 flag 与行为**：升级前必须读 release notes。
7. **PromQL 行为修正**：如 `holt_winters` 在 3.0 被移除并改名 `double_exponential_smoothing`（无别名，需实验函数开关）、部分函数边界行为调整。
8. **UI/API 调整**：默认 UI 现代化，部分旧端点变化。

> 状态会随小版本变化：原生直方图、Remote Write 2.0、OTLP receiver、delta temporality 等能力在不同 3.x 版本和不同后端上的可用性并不一致。本文描述语义与迁移方法，不把某个小版本的能力当成永久事实；上线前用你的版本做一次端到端验证。

3.x 的小版本持续加入：delta temporality 转换、native histogram 查询函数、OTLP 改进、性能优化。生产升级应：

- 先在 staging 用相同数据量验证；
- 检查 recording/alerting 规则；
- 检查 Grafana 面板的 PromQL；
- 检查 remote write 接收端兼容性；
- 备份 TSDB。

## 20.2 原生直方图深入

经典直方图：

```text
le=0.1 → 50
le=0.5 → 90
le=1   → 95
le=+Inf → 100
```

原生直方图：

- 桶边界按指数增长：`base = 2^(2^-schema)`；
- 只存非空桶；
- 一条序列包含 count/sum/桶集合；
- 支持动态范围；
- 查询时用 `histogram_quantile`、`histogram_count`、`histogram_sum`、`histogram_fraction` 等。

优势：

- 高基数场景序列数大幅减少（每个直方图一条，而不是每个桶一条）；
- 宽动态范围（微秒到小时）不需要预先设计桶；
- 更精确的分位数。

代价：

- 客户端/协议/存储/查询全链路支持；
- 调试工具较少；
- 与经典直方图共存时，查询要区分；
- 原生直方图本身在 3.8 已稳定，但客户端/后端/查询函数的支持程度仍取决于版本；抓取默认关闭，需要显式开启。

迁移建议：

- 新服务可直接用支持 native histogram 的客户端；
- 老服务保留经典直方图；
- 用 recording rules 统一成经典桶，供旧面板使用；
- 在 Grafana/告警中逐步切换。

## 20.3 OTLP 接收

启动：

```bash
prometheus \
  --web.enable-otlp-receiver \
  --config.file=prometheus.yml
```

OTLP/HTTP 端点：

```text
POST /api/v1/otlp/v1/metrics
Content-Type: application/x-protobuf    # 常见
Content-Type: application/json          # OTLP/HTTP 规范允许
```

Prometheus 会把 OTLP metrics 转成内部样本：

- 身份属性固定映射：`service.name`→`job`、`service.instance.id`→`instance`，`service.namespace` 拼进 `job`；
- 其余 resource attributes 默认**只进入 `target_info`**；只有 `otlp.promote_resource_attributes` 显式列出的属性才追加到每条指标；
- scope name/version/schema → `otel_scope_*` 标签（是否保留取决于版本/配置）；
- metric name 按 `otlp.translation_strategy` 规范化，并追加单位后缀与 `_total`（上游默认 `UnderscoreEscapingWithSuffixes`）；
- counter/gauge/histogram/summary 映射；explicit-bucket histogram 通常转成 custom-bucket native histogram；
- delta temporality 默认不能直接摄入，需要 `--enable-feature=otlp-deltatocumulative` 转换为 cumulative，或用 `otlp-native-delta-ingestion` 保留 delta。

注意：

- `service.name` 映射为 `job`，不是 `service_name`；`keep_identifying_resource_attributes` 只影响这些身份属性是否也写进 `target_info`；
- 属性名可能包含点号；提升为标签时是否做下划线规范化取决于 `translation_strategy`（`NoUTF8EscapingWithSuffixes` 可保留 UTF-8 标签名，`UnderscoreEscapingWithSuffixes` 转 `_`）；
- OTLP 的 exemplar 在支持 exemplar 存储的后端可以保留；
- 高基数 resource attributes 会放大序列数，优先在 Collector 做 allowlist，而不是全量提升；
- delta 状态按序列维护，并用 `startTimeUnixNano` 检测 reset；进程重启后的第一段只能当新基线。

完整的名称/单位映射、delta 状态机、Mini 实现与生产检查表见附录 AG。

## 20.4 OpenTelemetry 与 Prometheus 的语义差异

| 维度 | Prometheus | OTel |
|---|---|---|
| 指标模型 | 名称 + 标签 + 样本 | instrument + attributes + data point |
| 时间性 | cumulative（counter） | delta 或 cumulative |
| 直方图 | 经典桶/原生 | explicit bucket/exponential |
| 资源 | target labels | resource attributes |
| 单位 | 名字后缀 | unit 字段 |
| 作用域 | job/instance | instrumentation scope |

映射时的关键决策：

1. **谁做转换**：应用直接暴露 Prometheus 格式，还是 OTel SDK → Collector → Prometheus？
2. **时间性**：delta 转 cumulative 会引入状态和延迟；保留 delta 需要后端支持。
3. **命名**：OTel 语义约定（`http.server.request.duration`）与 Prometheus 命名（`http_server_request_duration_seconds`）不同；需要统一。
4. **资源属性**：`service.name`、`k8s.pod.name`、`deployment.environment` 如何变成标签。
5. **基数**：OTel 默认属性更多，直接映射会放大序列数。

## 20.5 推荐的 OTel 接入方式

### 方式 A：应用直接暴露 Prometheus

- 用 Prometheus client library；
- 最简单、最可控；
- 失去 OTel 的统一 SDK/追踪关联。

### 方式 B：OTel SDK → Collector → Prometheus remote write

```text
App (OTel SDK)
  → OTLP → Collector
  → prometheusremotewrite exporter
  → Mimir/Thanos/Prometheus
```

优点：

- 统一 traces/metrics/logs；
- Collector 做批处理、重试、属性转换；
- 可以用 `transform`/`metricstransform` 处理器治理基数。

缺点：

- 多一跳，运维复杂；
- 语义转换需要配置；
- delta/cumulative 处理。

### 方式 C：OTLP 直接推 Prometheus

- `--web.enable-otlp-receiver`；
- 适合小规模/实验；
- 生产更推荐 Collector（缓冲/重试/治理）。

## 20.6 target_info 与资源标签

OTel 的资源属性通常转换成一个 `target_info` 指标：

```text
target_info{service_name="api",service_version="v2",k8s_pod_name="api-0"} 1
```

查询时用 `info()` 或 join：

```promql
http_requests_total
* on(job, instance) group_left(service_version)
  target_info
```

Mini 不实现 `info()`，但支持 `group_left` join，可用于演示。

## 20.7 迁移检查表

- [ ] 指标名/标签名是否仍兼容旧查询？
- [ ] 单位是否统一（秒/字节）？
- [ ] counter 是否有 `_total`？
- [ ] delta 是否转换/保留？
- [ ] histogram 桶是否覆盖 SLO？
- [ ] exemplar 是否保留？
- [ ] resource attributes 是否进入高基数标签？
- [ ] recording/alerting 规则是否验证？
- [ ] remote write 接收端是否支持 native histogram/RW2？
- [ ] 回滚路径是否可用？

## 20.8 Mini 的边界

Mini 实现：

- UTF-8 引号指标名与引号标签名（PromQL、text exposition、序列化）；
- 经典直方图；
- OTLP/HTTP **JSON** 接收（名称/单位翻译、资源属性、scope、delta→cumulative）；
- native histogram 不实现；
- OTLP/protobuf 不实现；
- `scrape_protocols` 配置字段存在但只解析文本/OpenMetrics。

与上游的两处有意差异（附录 AG 详述）：

- Mini 把其余 resource attributes **直接提升为指标标签**，且不生成 `target_info`；上游默认只放进 `target_info`，需 `otlp.promote_resource_attributes` 才提升。
- Mini 的 `--storage.otlp.translation-strategy` 是自己的 CLI flag，默认 `NoUTF8EscapingWithSuffixes`；上游用 `otlp.translation_strategy` 配置，默认 `UnderscoreEscapingWithSuffixes`。对应地，Mini 默认保留 `k8s.pod.name` 这类 UTF-8 标签名，切到 underscore 策略才转成 `k8s_pod_name`。

这是有意的：教程用 Mini 讲清"经典语义 + 可运行的最小翻译层"，用本章讲"新特性与迁移"。生产选型不要把 Mini 的能力当成 Prometheus 的上限。原生直方图的 schema/插值、Remote Write 2.0 的符号表/版本协商与迁移步骤见附录 R；OTLP 的完整翻译规则、delta 状态机与 Mini 代码走读见附录 AG；UTF-8 的转义协商与迁移见附录 AF。

## 20.9 UTF-8 指标名的转义与兼容

Prometheus 3.0 起默认允许 UTF-8 指标名/标签名，但"允许"不等于"建议"。

### 20.9.1 查询语法

经典指标名：

```promql
http_requests_total{job="api"}
```

UTF-8 指标名用引号：

```promql
{"http.requests.total", job="api"}
```

非 legacy 标签名同样要引号：

```promql
{"http.server.duration_bucket", "service.name"="checkout", "k8s.namespace.name"="prod"}
sum by ("service.name") ({"http.server.duration_bucket"})
```

注意：

- 引号内的指标名/标签名不再受 legacy 字符集限制（指标名 `[a-zA-Z_:][a-zA-Z0-9_:]*`、标签名 `[a-zA-Z_][a-zA-Z0-9_]*`），但仍必须是合法 UTF-8；
- 标签名在存储层也允许 UTF-8，但生态兼容性比指标名更差，能沿用经典字符集就沿用；
- 某些函数/聚合对 `__name__` 的处理可能不同；
- 旧版 Prometheus/Grafana/告警模板可能无法解析；
- 抓取/remote write 还要协商 `escaping`（`allow-utf-8`/`underscores`/`dots`/`values`），详见附录 AF。

### 20.9.2 兼容性风险

- remote write 接收端/长期存储是否支持 UTF-8；
- 告警模板/URL 编码/日志管道；
- 客户端库是否真的输出 UTF-8 名字；
- 仪表盘/查询变量；
- 监控即代码的工具链（promtool、lint、schema）。

生产建议：

- 新指标仍用经典命名；
- 只有第三方指标无法转换时才用 UTF-8；
- 在采集边界做规范化/映射；
- 升级前用真实查询集验证。

## 20.10 OTLP 的 delta、资源属性与作用域

OTLP 与 Prometheus 的模型差异不只是命名，时间性和资源属性才是难点。

### 20.10.1 Delta vs Cumulative

OTel counter 可以是：

- **cumulative**：从进程启动累计；
- **delta**：只报告区间增量。

Prometheus 的 counter 语义是 cumulative（`rate()` 依赖重置补偿）。如果直接摄入 delta：

- `rate()`/`increase()` 会把它当 cumulative，结果错误；
- 需要后端支持 delta 或做 cumulative 转换；
- 转换需要状态（每个序列的累计值）和延迟；
- 进程重启/序列消失时，转换状态要处理。

较新 Prometheus 版本提供两条路径：

```bash
# 在接收端把 delta 累加成 cumulative（推荐给普通 Prometheus 存储）
--enable-feature=otlp-deltatocumulative

# 保留 delta 语义，交给明确支持 delta 的后端
--enable-feature=otlp-native-delta-ingestion
```

默认既不转换也不保留；delta 点会被拒绝或丢弃。转换状态按序列和 `startTimeUnixNano` 维护，乱序点、start time 变化、进程重启都要有明确策略。接入前用实际版本做端到端验证，不要假设"OTLP 一开就兼容"；实现细节见附录 AG。

### 20.10.2 资源属性

OTel 的 resource attributes（`service.name`、`k8s.pod.name`、`deployment.environment`）有三条去向：

- **身份属性**：`service.name` → `job`，`service.instance.id` → `instance`，`service.namespace` 拼进 `job`（`namespace/name`）；
- **`target_info`**：其余资源属性默认聚合成一条 `target_info{...} 1`，避免每条指标重复携带资源标签；
- **显式提升**：只有 `otlp.promote_resource_attributes` 列出的属性才追加到每条指标；默认列表为空。

`keep_identifying_resource_attributes` 控制身份属性是否也写进 `target_info`，默认 `false`。不要把 `service.name` 再保留成同名标签，否则同一身份会有两套写法。

`target_info` 示例：

```text
target_info{service_name="api",service_version="v2",k8s_pod_name="api-0"} 1
```

查询时 join：

```promql
http_requests_total
* on(job, instance) group_left(service_version)
  target_info
```

风险：

- 资源属性太多/值太高基数 → 序列爆炸；
- 全量提升资源属性会让每条指标复制一份标签，优先用 `target_info` + join；
- `service.name`/`service.instance.id` 的映射固定，不要在不同链路里改成 `service_name`；
- 不同 SDK 的属性名不一致；
- 资源属性变更（Pod 重建）会产生新序列。

### 20.10.3 Instrumentation scope

OTel 的 scope（instrumentation library name/version/schema）如果映射为标签，会增加基数。常见做法是保留 `otel_scope_name`、`otel_scope_version`、`otel_scope_schema_url` 这三个固定标签，scope 的自定义 attributes 默认不摊平：

- 三个固定标签能回答"这个指标由哪个库产生"，基数可控；
- scope attributes 可能每个实例不同，容易放大序列数；
- 需要时在 Collector 里显式选择少量 scope 属性加前缀保留；
- 不要把 scope version 无差别地放进所有指标。

### 20.10.4 迁移检查

- [ ] delta 是否已转换/后端支持？
- [ ] `service.name`/`job`/`instance` 映射是否一致？
- [ ] 资源属性是否进入高基数标签？
- [ ] `target_info` 是否保留？
- [ ] exemplar 是否保留？
- [ ] 指标名/单位是否规范化？
- [ ] 旧面板/告警是否仍能查询？
- [ ] 回滚路径是否验证？

## 20.11 版本基线：3.8–3.16 与 LTS

本章涉及的能力在 3.x 小版本中变化很快。写生产方案时，先固定一条基线：

| 版本 | 时间 | 与本章相关的状态 |
|---|---|---|
| 3.0 | 2024-11 | UTF-8、OTLP receiver、RW2 rc、原生直方图实验 |
| 3.5 | 2025-07 | LTS 分支，支持到 2026-07 |
| 3.7 | 2025-10 | `smoothed`/`anchored` 区间选择器实验 |
| 3.8 | 2025-11 | **原生直方图稳定**；新增 `scrape_native_histograms` |
| 3.9 | 2026-01 | `native-histograms` feature flag 变为 no-op |
| 3.13 | 2026-07 | **LTS**，支持到 2027-07；`min()`/`max()` 时长函数改名 `min_of()`/`max_of()` |
| 3.14 | 2026-08 | PromQL 时长表达式默认开启；`stats` 参数收紧 |
| 3.15 | 2026-09 | `runtime.log_level`、XOR2、Unix socket、OM2.0、zstd 抓取 |
| 3.16 | 2026-11 | `smoothed`/`anchored` 稳定；查询成本控制实验；exemplar 校验收紧 |

实践建议：

- **保守生产**：3.13 LTS；需要原生直方图时确认 3.8+，需要 `smoothed` 时确认 3.16+（或 3.7+ 加 flag）。
- **原生直方图**：抓取侧设 `scrape_native_histograms: true`；要保留经典桶兼容旧面板时设 `always_scrape_classic_histograms: true`；remote write 发送设 `send_native_histograms: true`。
- **Remote Write 2.0**：截至 2026-11 仍是实验规范（`2.0-rc.4`），不是"稳定版"。发送端/接收端要锁定同一 rc，并确认内容协商与错误码。
- **`smoothed`/`anchored`**：会改变 `rate`/`increase`/`delta` 在窗口边界的行为；在规则里用 `smoothed` 时必须给规则组设置 `query_offset`（至少一个抓取间隔），否则窗口末端没有"未来样本"可插值。
- **升级顺序**：先 staging 同数据量验证 → 规则/面板回归 → remote write 接收端兼容 → 再滚动升级。

> 版本号和日期会继续变化；上表用于建立"能力从哪个版本开始可用"的判断框架，具体补丁版本以官方 release notes 与 LTS 表为准。

## 20.12 3.14–3.16 的增量

3.13 之后的小版本继续改变默认行为，生产升级不能只看"3.x"这个大版本。

### 20.12.1 3.14：PromQL 时长表达式默认开启

- `promql-duration-expr` 特性开关变为 no-op，`rate(x[5m * 2])`、`rate(x[step() * 4])` 这类表达式默认可用；
- 时长表达式里的 `min()`/`max()` 改名 `min_of()`/`max_of()` 发生在 **3.13.0**（不是 3.14）；3.14 只是默认开启，升级时不要漏掉这一步；
- `stats` 查询参数只建议使用 `true`/`all`，其他值已弃用（仍可用但会警告）。

影响：规则里的窗口长度可能依赖运行时参数，稳定性下降；生产规则仍建议用字面量窗口。

### 20.12.2 3.15：运行时可调与协议扩展

- `--log.level` 弃用，改用 `runtime.log_level`，reload 即可调整日志级别；
- XOR2 浮点 chunk 编码稳定，改用 `storage.tsdb.chunk_encoding.floats: xor2`；写入前确认 Thanos/远端读取端支持；
- 支持 Unix Domain Socket 抓取、OpenMetrics 2.0、zstd 压缩抓取响应；
- `--auto-gomemlimit.refresh-interval` 周期性更新 `GOMEMLIMIT`，适合 VPA；
- Docker SD 增加镜像标签，Targets/SD 页面展示生效配置。

### 20.12.3 3.16：稳定与收紧

- `smoothed`/`anchored` 区间选择器稳定且默认可用；规则中使用时要配组级 `query_offset`（或全局 `rule_query_offset`），它只后移查询时间，写入仍用求值时间；
- `--enable-feature=query-cost` 提供查询成本估算/实际成本/执行上限（实验）；
- 基于 start timestamp 的 counter reset 检测，可用 `X-Prometheus-Use-Start-Timestamps` 按查询覆盖；
- 重复标签名的 exemplar 被拒绝，不再返回重复 JSON key；
- 子查询样本的查询统计口径有调整（计入存储读取、排除物化重复计数），升级后需重新基线化成本面板；
- 新增 `convert_nhcb_to_classic` 远程写选项，便于把 NHCB 发给旧后端。

3.14–3.16 的逐条变化、迁移矩阵、回滚边界与兼容性清单见附录 AE。升级时先读 changelog 的 `[CHANGE]` 段，再用 staging 的同数据量回归；不要在生产直接跳过多个小版本。

## 本章小结

- Prometheus 3.x 带来 UTF-8、native histogram、OTLP、RW2 等变化，升级需验证。
- 原生直方图用稀疏指数桶，适合宽动态范围/高基数；3.8 稳定、3.9 起旧 flag 失效，抓取需 `scrape_native_histograms`。
- OTel 与 Prometheus 的模型差异主要在时间性、资源属性、命名和直方图。
- Collector 是生产 OTel→Prometheus 的推荐路径；OTLP 直收适合小规模。
- 迁移的核心是语义与基数，不是协议本身。

## 延伸阅读

- Prometheus 3.0 release notes 与 3.x changelog。
- Prometheus native histogram 文档。
- OpenTelemetry 语义约定与 Collector `prometheusremotewrite` exporter。
- 本教程第 18 章（remote write）、附录 G（生态对比）、附录 R（原生直方图/RW2 深入）、附录 AE（3.14–3.16 新特性与迁移）、附录 AF（UTF-8 名称与转义）与附录 AG（OTLP 接收与 delta 转换）。
