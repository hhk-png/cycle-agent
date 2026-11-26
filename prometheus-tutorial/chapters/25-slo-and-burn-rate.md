# 第 25 章 SLO、错误预算与 Burn-Rate 告警

## 25.1 从"阈值告警"到"用户体验"

传统告警：

```promql
cpu_usage > 90%
```

问题：

- CPU 高不一定影响用户；
- 阈值是拍脑袋的；
- 告警多但无法回答"服务现在是否可靠"。

SLO（Service Level Objective）把告警锚定在用户体验：

```text
SLI：可测量的指标（成功率、延迟、可用性）
SLO：目标值（99.9% 成功 / 30 天内）
错误预算：1 - SLO（0.1% = 43.2 分钟/30 天）
Burn rate：错误消耗速度相对于预算的倍数
```

## 25.2 SLI 的定义

好的 SLI：

- 从用户视角定义（请求成功/失败）；
- 有明确的分子/分母；
- 可观测、低延迟、低基数；
- 覆盖关键用户旅程。

例子：

```promql
# 成功率（非 5xx）
sum(rate(http_requests_total{code!~"5.."}[5m]))
/
sum(rate(http_requests_total[5m]))

# 延迟（P90 < 300ms 的比例）
sum(rate(http_request_duration_seconds_bucket{le="0.3"}[5m]))
/
sum(rate(http_request_duration_seconds_count[5m]))
```

注意：

- 分母要包含所有请求，包括失败的；
- 4xx 是否算失败取决于业务（客户端错误通常不算服务端 SLI）；
- 延迟 SLI 用"好请求比例"而不是直接 P90，便于计算预算。

## 25.3 错误预算

30 天 99.9%：

```text
总分钟 = 30 × 24 × 60 = 43,200
预算 = 0.1% × 43,200 = 43.2 分钟
```

预算消耗：

```text
已用预算 = 1 - 实际成功率
剩余 = 1 - 已用/预算
```

预算策略：

- 预算充足：正常发布；
- 预算消耗过快：冻结高风险发布；
- 预算耗尽：只修可靠性，不加功能。

## 25.4 Burn Rate

Burn rate = 错误率 / 错误预算比例。

SLO 99.9%（预算 0.1%）：

```text
错误率 0.1% → burn rate 1（30 天耗尽）
错误率 1%   → burn rate 10（3 天耗尽）
错误率 10%  → burn rate 100（7.2 小时耗尽）
```

多窗口告警（Google SRE Workbook）：

```promql
# 1h 窗口 burn rate > 14.4（2% 预算在 1h 内消耗）
(
  sum(rate(http_requests_total{code=~"5.."}[1h]))
  /
  sum(rate(http_requests_total[1h]))
) / 0.001 > 14.4

# 5m 窗口确认（避免短脉冲）
and
(
  sum(rate(http_requests_total{code=~"5.."}[5m]))
  /
  sum(rate(http_requests_total[5m]))
) / 0.001 > 14.4
```

常见组合：

| 长窗口 | 短窗口 | burn rate | 用途 |
|---|---|---|---|
| 1h | 5m | 14.4 | 快速 page |
| 6h | 30m | 6 | 中速 ticket |
| 3d | 6h | 1 | 慢速趋势 |

短窗口用于"确认仍在发生"，避免长窗口被历史错误拖住。

## 25.5 记录规则设计

```yaml
groups:
  - name: slo
    interval: 30s
    rules:
      - record: job:slo_requests:ratio_5m
        expr: |
          sum by (job) (rate(http_requests_total{code!~"5.."}[5m]))
          /
          sum by (job) (rate(http_requests_total[5m]))
      - record: job:slo_requests:ratio_1h
        expr: |
          sum by (job) (rate(http_requests_total{code!~"5.."}[1h]))
          /
          sum by (job) (rate(http_requests_total[1h]))
      - alert: SLOBurnRateHigh
        expr: |
          job:slo_requests:ratio_1h < 0.999 - 14.4 * 0.001
          and
          job:slo_requests:ratio_5m < 0.999 - 14.4 * 0.001
        for: 2m
        labels:
          severity: page
        annotations:
          summary: "{{ $labels.job }} is burning error budget fast"
```

注意：

- 记录规则先算 ratio，告警再比较，避免重复计算；
- 分母为 0 时 ratio 为 NaN，需要 `and` 保护；
- 告警表达式要能处理"没有流量"（无数据不应 page）；
- SLO 阈值应参数化（不同服务不同 SLO）。

## 25.6 无流量与低流量

低流量服务：

- 错误率波动大；
- 短窗口可能只有几个请求；
- burn rate 不稳定。

处理：

- 用更长窗口；
- 加最小请求数条件；
- 对"零流量"单独告警（可用性探测）；
- 用合成探针（blackbox exporter）补充。

```promql
sum(rate(http_requests_total[5m])) > 1
and
(错误率表达式)
```

## 25.7 SLO 与告警分层

- **Page**：burn rate 高、用户可见、需要立即处理。
- **Ticket**：burn rate 中、趋势问题、工作时间处理。
- **Dashboard**：预算剩余、趋势、Top 错误。
- **Postmortem**：预算耗尽/重大故障后复盘。

不要把所有 SLO 违规都 page；否则告警疲劳。

## 25.8 Mini 的演示

Mini 可以：

- 计算 ratio/rate；
- 记录规则；
- 告警规则；
- 但需要真实流量/历史数据才能验证 SLO。

`examples/05_alerting.py` 演示了 pending/firing；把表达式换成 burn rate 即可。

## 25.9 SLO 类型与选择

SLO 不只适用于"HTTP 成功率"。常见类型：

| 类型 | SLI 示例 | 适用 | 注意 |
|---|---|---|---|
| 可用性 | 非 5xx 请求比例 | API/Web | 4xx 是否算失败要定义 |
| 延迟 | P90 < 300ms 的请求比例 | API/DB | 用"好请求比例"而非直接 P90 |
| 吞吐 | 每秒处理订单数 / 目标 | 队列/批处理 | 低流量时波动大 |
| 新鲜度 | `now - last_success < 5m` | 数据管道/ETL | 需要成功时间戳 |
| 正确性 | 对账一致的比例 | 支付/计费 | 需要业务对账数据 |
| 持久性 | 成功写入/读取比例 | 存储 | 需要端到端探测 |

选择原则：

- 从**用户旅程**出发，而不是从现有指标出发；
- 每个 SLO 要有明确的分子/分母和排除项；
- 优先选**可行动**的 SLI：坏了团队能修；
- 不要给每个服务都定 99.99%；成本与收益要匹配；
- 内部服务可以用较宽松的 SLO，但要有依赖链意识。

### 25.9.1 请求型 vs 窗口型

```text
请求型：成功请求 / 总请求          （适合 API）
窗口型：好时间窗 / 总时间窗        （适合批处理/低频）
```

请求型更敏感、更常用；窗口型适合"每分钟是否成功"这类离散事件。两者不要混在一个 SLO 里。

## 25.10 错误预算策略

错误预算不是"用完了就停"，而是**事先约定的决策规则**：

```text
预算剩余 > 50%：正常发布，允许实验
预算剩余 20%～50%：谨慎发布，需要回滚计划
预算剩余 < 20%：冻结高风险变更，优先可靠性
预算耗尽：只修可靠性，直到预算恢复
```

策略要写清：

- 谁有权批准例外；
- 什么算"高风险变更"；
- 预算恢复后如何解除冻结；
- 多个服务共享预算时如何分配；
- 预算耗尽是否影响发布/ SLA 赔偿。

常见错误：

- 预算只用于 dashboard，不进入发布流程；
- 预算耗尽后仍然"业务优先"，规则形同虚设；
- 用预算惩罚团队，导致隐瞒故障；
- 没有区分"可预防"和"外部依赖"故障。

## 25.11 多窗口多燃烧率

单窗口 burn rate 容易误报/漏报。Google SRE Workbook 推荐多窗口组合：

| 长窗口 | 短窗口 | burn rate | 消耗预算 | 用途 |
|---|---|---|---|---|
| 1h | 5m | 14.4 | 2% / 1h | 快速 page |
| 6h | 30m | 6 | 5% / 6h | 中速 page/ticket |
| 3d | 6h | 1 | 10% / 3d | 慢速 ticket |

公式：

```text
burn_rate = error_ratio / (1 - SLO)
error_ratio = bad_events / total_events
```

记录规则分层：

```yaml
groups:
  - name: slo-api
    interval: 30s
    rules:
      - record: slo:api_error_ratio:5m
        expr: |
          sum(rate(http_requests_total{job="api",code=~"5.."}[5m]))
          /
          sum(rate(http_requests_total{job="api"}[5m]))
      - record: slo:api_error_ratio:1h
        expr: |
          sum(rate(http_requests_total{job="api",code=~"5.."}[1h]))
          /
          sum(rate(http_requests_total{job="api"}[1h]))
      - record: slo:api_error_ratio:6h
        expr: |
          sum(rate(http_requests_total{job="api",code=~"5.."}[6h]))
          /
          sum(rate(http_requests_total{job="api"}[6h]))
      - record: slo:api_error_ratio:3d
        expr: |
          sum(rate(http_requests_total{job="api",code=~"5.."}[3d]))
          /
          sum(rate(http_requests_total{job="api"}[3d]))
```

告警：

```promql
# 快速燃烧：1h + 5m 同时超阈值
(
  slo:api_error_ratio:1h > (1 - 0.999) * 14.4
  and
  slo:api_error_ratio:5m > (1 - 0.999) * 14.4
)
```

注意：

- 长窗口负责"确实在烧"，短窗口负责"现在还在烧"；
- 两个窗口都超才 page，降低误报；
- 短窗口太小会抖动，太大则反应慢；
- 记录规则的 `interval` 要小于短窗口；
- 没有流量时 ratio 为 NaN，需要 `and` 流量条件或 `absent` 保护。

### 25.11.1 生成器与工具

手写多窗口规则容易出错。可以用：

- **Sloth**：从 SLO 规格生成 Prometheus 规则；
- **Pyrra**：SLO 即代码，生成规则/ dashboard；
- **OpenSLO**：厂商中立的 SLO 规范；
- **Grafana SLO**：托管/自建 SLO 管理。

无论用哪个工具，都要：

- 把生成的规则纳入 GitOps；
- 用 `promtool check rules`/`promtool test rules` 验证；
- 定期检查规则是否与实际服务匹配；
- 避免"生成了一堆规则但没人看"。

## 25.12 批处理、流式与低流量 SLO

### 批处理

指标：

```promql
# 任务成功率
sum(rate(batch_job_success_total[1h]))
/
sum(rate(batch_job_total[1h]))

# 新鲜度：最后一次成功距今
time() - max(last_success_timestamp_seconds{job="etl"})
```

SLO 可以是"每天 99% 的任务在 02:00 前完成"或"数据延迟 < 15m"。注意：

- 任务可能一天只跑几次，ratio 波动大；
- 用窗口型/事件型 SLI，不要硬套请求比例；
- 失败重试要区分"最终成功"和"首次成功"；
- 补数据/回填要单独计算。

### 流式

指标：

```promql
# 消费延迟
max(kafka_consumergroup_lag{group="orders"})

# 处理成功率
sum(rate(stream_records_processed_total{status="ok"}[5m]))
/
sum(rate(stream_records_processed_total[5m]))
```

SLO 可以是"P99 端到端延迟 < 5s"或"积压 < 10 万条"。注意分区/消费者组维度，避免高基数。

### 低流量

低流量服务的 ratio 在短窗口内可能只有几个请求：

```promql
# 至少 100 个请求才计算 ratio
sum(rate(http_requests_total[5m])) > 100/300
and
(
  sum(rate(http_requests_total{code=~"5.."}[5m]))
  /
  sum(rate(http_requests_total[5m]))
) > 0.01
```

补充手段：

- 合成探针（blackbox exporter）产生稳定流量；
- 更长窗口；
- 绝对错误数阈值（例如 5 分钟内 > 10 个错误）；
- 对"零流量"单独告警（`absent`/`up`）。

## 25.13 SLO 评审与组织

SLO 是**产品/业务/运维的共同契约**，不是 SRE 的单方面指标。评审问题：

- SLI 是否覆盖关键用户旅程？
- 分母是否包含所有请求？排除项是否合理？
- SLO 目标是否基于历史数据/业务预期/成本？
- 错误预算策略是否被团队和业务方接受？
- 告警是否可行动？runbook 是否最新？
- 是否有 dashboard 展示预算剩余/燃烧率/Top 错误？
- 是否每季度校准？服务变更后是否更新？
- 多个服务共享预算时，如何归因？

组织建议：

- 每个 SLO 有明确 owner；
- SLO 目录与指标目录关联（第 47 章）；
- 预算评审进入发布流程；
- 重大故障后复盘"预算消耗是否合理"；
- 不要把 SLO 当成绩效考核，否则会诱发数据操纵。

## 25.14 Mini 的 SLO 实验

用 Mini 和 `promtool test rules` 可以验证 burn-rate 逻辑：

```yaml
rule_files:
  - ../rules/slo.yml
evaluation_interval: 1m
tests:
  - interval: 1m
    input_series:
      - series: 'http_requests_total{job="api",code="200"}'
        values: '0+100x60'
      - series: 'http_requests_total{job="api",code="500"}'
        values: '0+10x60'
    promql_expr_test:
      - expr: slo:api_error_ratio:5m
        eval_time: 10m
        exp_samples:
          - labels: 'slo:api_error_ratio:5m'
            value: 0.09090909090909091
```

练习：

1. 把 SLO 从 99.9% 改成 99.5%，观察 burn rate 阈值；
2. 加入"无流量"场景，验证告警不触发；
3. 用 `for`/`keep_firing_for` 模拟恢复；
4. 把规则拆成 recording + alert 两层，比较查询成本。

Mini 没有真实流量/长期数据，但可以验证**表达式语义和状态机**；真实 SLO 需要生产数据和业务对账。

## 本章小结

- SLO 把告警锚定在用户体验，错误预算量化"可接受的失败"。
- Burn rate = 错误率 / 预算比例；多窗口避免误报/漏报。
- 记录规则先算 SLI ratio，告警再比较。
- 低流量/零流量要特殊处理。
- SLO 需要评审和校准，不是一次性的数字。

## 延伸阅读

- Google SRE Workbook：Alerting on SLOs。
- Prometheus 官方 "Alerting" 与 recording rules。
- 本教程第 26 章（告警工程）。
