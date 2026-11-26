# 附录 M Grafana 与可视化

Prometheus 负责采集、存储和查询，但大多数人不直接读 `/api/v1/query`。告警、排障、容量评审和业务复盘最终都落在可视化上。本附录补齐"查询结果如何变成可用的面板"这一段工程实践，重点不是 Grafana 的按钮位置，而是**数据源、查询、变量、面板、告警和性能之间的契约**。

## M.1 角色分工：Prometheus 存查，Grafana 展示

一个常见的误区是把 Grafana 当成监控系统。更准确的边界是：

- Prometheus：指标语义、抓取、TSDB、PromQL、记录/告警规则；
- Alertmanager：告警去重、分组、抑制、静默、路由；
- Grafana：查询编排、可视化、变量、面板权限、跨数据源关联、Grafana 托管告警；
- 长期存储（Thanos/Mimir/VictoriaMetrics）：历史数据与全局查询。

Grafana 不存储 Prometheus 样本（除非使用 Grafana Mimir 等后端），也不应该成为唯一的告警真相来源。把"面板能看"当成"监控可用"是典型的假安全感。

## M.2 数据源配置

### 手工配置的等价 YAML

Grafana 的 provisioning 配置比 UI 更适合版本化：

```yaml
apiVersion: 1

datasources:
  - name: Prometheus
    type: prometheus
    access: proxy
    url: http://prometheus.monitoring.svc:9090
    isDefault: true
    editable: false
    jsonData:
      timeInterval: 30s
      httpMethod: POST
      manageAlerts: true
      prometheusType: Prometheus
      prometheusVersion: 3.5.0
      exemplarTraceIdDestinations:
        - name: trace_id
          datasourceUid: tempo
```

要点：

- `timeInterval` 应不小于抓取间隔；Grafana 用它推导 `$__rate_interval`；
- `httpMethod: POST` 避免超长 PromQL 被 URL 长度限制；
- `exemplarTraceIdDestinations` 把 exemplar 的 `trace_id` 链到 Tempo/Jaeger；
- `editable: false` 防止 UI 漂移，配置以 Git 为准；
- 数据源 UID 应稳定，否则面板和告警引用的 `datasourceUid` 会失效。

### 认证与网络

- 内网：直接访问 Prometheus；
- 跨集群：通过反向代理/网关，启用 TLS 和认证；
- 多租户：按团队配置多个数据源，或在代理层注入 `X-Scope-OrgID`；
- 不要把 Prometheus 的 admin API 暴露给 Grafana。

## M.3 面板与查询

### 面板类型的选择

| 需求 | 首选面板 | 说明 |
|---|---|---|
| 随时间变化的趋势 | Time series | 默认选择；支持多查询、图例、阈值 |
| 当前值/单指标 | Stat | 大数字 + 趋势 sparkline |
| 目标完成度 | Gauge/Bar gauge | 适合 SLO、容量百分比 |
| 多维明细 | Table | 排序、分页、链接到详情 |
| 直方图分布 | Heatmap | 按 `le` 桶聚合后的分布随时间变化 |
| 分布对比 | Histogram | 单时间点的桶分布 |
| 日志/追踪 | Logs/Traces | 与指标联动，不替代指标 |

### 即时查询与区间查询

Grafana 面板通常发 `query_range`，但 Stat/Table 可以使用 instant query。选择错误会导致：

- 用 instant query 画趋势：只得到一个点；
- 用 range query 算"当前值"：返回一串点，Stat 取最后一个，可能掩盖抖动；
- 在 Table 中对 range 结果做聚合：语义不清。

### `$__interval` 与 `$__rate_interval`

Grafana 内置变量：

- `$__interval`：根据时间范围和面板宽度推导的步长；
- `$__rate_interval`：在 `$__interval` 基础上保证至少包含 4 个抓取点，专门给 `rate()`/`increase()` 用；
- `$__range`：当前面板的整个时间范围。

推荐写法：

```promql
sum by (code) (
  rate(http_requests_total{job="$job"}[$__rate_interval])
)
```

不要在所有面板里硬编码 `[5m]`：在 30 天视图里，5m 窗口会产生大量点，既慢又容易触发 Grafana 的 `max data points` 降采样。

### 查询的 `min step` 与 `max data points`

- `min step` 限制最小步长，避免高分辨率查询打爆 Prometheus；
- `max data points` 控制返回点数，Grafana 会按需增大 step；
- 两者共同决定查询成本。面板越宽、时间范围越长，越应该提高 `min step`。

经验值：抓取间隔 15s 时，`min step` 不低于 30s；长周期容量面板可以到 5m～1h。

### 图例与单位

- 图例使用 `{{label}}` 模板，例如 `{{code}} - {{method}}`；
- 单位必须设置（seconds、bytes、percent、requests/sec），否则读者会误判数量级；
- 多查询面板用图例区分数据源/查询；
- 对 `NaN`/无数据设置 `No value: 0` 或 `Null`，不要用 0 掩盖缺失。

## M.4 变量与模板化面板

### 查询变量

```text
名称: job
类型: Query
数据源: Prometheus
查询: label_values(up, job)
```

链式变量：

```text
名称: instance
查询: label_values(up{job="$job"}, instance)
```

注意：

- 变量查询本身也是 Prometheus 查询，高基数 `label_values` 可能很慢；
- 用 `label_values(metric{...}, label)` 而不是全量 `label_values(label)`，减少扫描；
- 变量值要转义，避免 `$`、引号、反斜杠破坏 PromQL；
- 多选变量用 `=~"$instance"`，此时 Grafana 会展开为 `(a|b|c)`；确保标签值里没有正则元字符，或在数据建模时避免。

### 间隔变量

```text
名称: interval
类型: Interval
值: 30s,1m,5m,15m,1h
```

查询里使用 `[$interval]` 或 `$__rate_interval`。间隔变量适合让值班人员在高负载时手动降采样。

### 重复面板

按变量重复面板（Repeat by variable）可以快速生成"每个 job 一行"的布局。代价是面板数量 = 变量基数 × 查询数，容易把浏览器和 Prometheus 一起拖慢。重复维度应限制在个位数。

## M.5 Dashboard as Code

Dashboard 的 JSON 模型可以纳入 Git，但要注意：

- UI 编辑会产生大量无关 diff（位置、ID、版本号）；
- 用 provisioning 固定数据源 UID 和面板 UID；
- 对 JSON 做格式化/排序，减少噪声；
- 用 CI 校验 JSON 可解析、引用的数据源存在；
- 大规模团队可以用 Terraform Grafana provider 或 Grafonnet 生成。

```yaml
apiVersion: 1

providers:
  - name: platform
    orgId: 1
    folder: Platform
    type: file
    disableDeletion: false
    updateIntervalSeconds: 30
    allowUiUpdates: false
    options:
      path: /var/lib/grafana/dashboards
```

`allowUiUpdates: false` 表示 UI 修改不会保存；团队必须走 Git 流程。若团队还没有这个纪律，先不要打开，否则会出现"UI 改了、Git 没改、重启后丢失"的事故。

## M.6 Grafana 告警与 Prometheus 告警

两条路径：

1. **Prometheus 规则 + Alertmanager**：规则可移植、可 `promtool test rules`、与 PromQL 同仓库；适合平台团队统一管理。
2. **Grafana 托管告警**：跨数据源、UI 配置、与面板同处；适合业务团队快速迭代或非 Prometheus 数据源。

选择原则：

- 同一症状只保留一个告警源，避免重复通知；
- 记录规则和 SLO 告警优先放在 Prometheus/规则仓库；
- Grafana 告警适合"面板级、跨源、临时"的告警；
- 无论哪条路径，都要有 runbook、抑制策略和演练。

Grafana 告警的关键配置：

- Contact point：与 Alertmanager 的 receiver 类似；
- Notification policy：路由树，支持继续匹配；
- Mute timing：维护窗口；
- Silence：临时静默；
- Alert rule：查询 + 条件 + `for` + 标签/注解。

不要把 Grafana 的 `for` 与 Prometheus 的 `for` 叠加使用而不清楚语义：两层延迟会相加。

## M.7 Exemplar、追踪与日志联动

Exemplar 把指标样本关联到具体 trace/日志：

```promql
histogram_quantile(
  0.99,
  sum by (le) (rate(http_request_duration_seconds_bucket[$__rate_interval]))
)
```

前提：

- 客户端在 histogram 观测时附带 `trace_id` exemplar；
- Prometheus 开启 exemplar 存储（较新 3.x 默认；早期版本确认 `exemplar-storage` flag，受 retention 限制）；
- Grafana 数据源配置 `exemplarTraceIdDestinations`；
- 后端 Tempo/Jaeger 能按 `trace_id` 查询。

常见坑：

- exemplar 只保留最近一段时间，历史面板看不到；
- `trace_id` 标签名不一致；
- 高基数 exemplar 增加存储压力；
- 把 exemplar 当成完整追踪，忽略采样。

## M.8 性能与成本

面板慢通常不是 Grafana 的问题，而是查询模式：

- 避免 `sum(rate(high_cardinality_metric[5m]))` 不带 `by` 的全量扫描；
- 用记录规则预聚合高频面板；
- 限制时间范围，默认 1h/6h，而不是 30d；
- 设置 `min step` 和 `max data points`；
- 减少重复面板和变量基数；
- 对 `label_values` 变量查询加过滤；
- 使用 Grafana 查询缓存（企业版/Cloud）或后端缓存；
- 大屏（TV mode）固定刷新间隔，避免多人同时高頻刷新。

一个可操作的指标：面板的 `Query time` 中位数应低于 1s；超过 5s 的面板应优化或改为记录规则。

## M.9 常见反模式

- **一个面板塞 50 条查询**：图例不可读，查询成本高；
- **对分位数求平均**：`avg(histogram_quantile(...))` 数学错误；
- **用 `[5m]` 画 30 天趋势**：点数过多，降采样后失真；
- **没有单位/阈值**：读者无法判断"高不高"；
- **无数据当 0**：掩盖抓取失败；
- **面板即告警**：没人看的面板报警等于没报警；
- **Dashboard sprawl**：每个团队一套重复面板，维护成本失控；
- **UI 直接改生产面板**：重启/迁移后丢失。

## M.10 上线检查清单

- [ ] 数据源 UID 稳定，provisioning 管理；
- [ ] 抓取间隔与 `timeInterval` 一致；
- [ ] `rate()` 使用 `$__rate_interval` 或明确的窗口；
- [ ] 每个面板有单位、图例、阈值；
- [ ] 变量查询有过滤，基数可控；
- [ ] 高频面板有记录规则或缓存；
- [ ] 告警源唯一，runbook 可点击；
- [ ] exemplar → trace 链路可用；
- [ ] Dashboard JSON 在 Git 中，CI 可校验；
- [ ] 有"无数据/抓取失败"的独立面板。

## 延伸阅读

- Grafana 官方文档：Prometheus data source、Variables、Provisioning、Alerting。
- Prometheus 官方文档：Exemplars、Recording rules。
- 本教程第 9、10 章（规则/告警）、第 24 章（Kubernetes 监控）、附录 N（Alertmanager 配置）。
