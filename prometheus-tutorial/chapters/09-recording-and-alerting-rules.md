# 第 9 章 记录规则与告警规则

## 9.1 规则文件

规则通过 `rule_files` 加载：

```yaml
rule_files:
  - "rules/*.yml"
  - "/etc/prometheus/rules/*.yaml"
```

一个规则文件：

```yaml
groups:
  - name: api
    interval: 30s
    limit: 10000
    rules:
      - record: job:http_requests:rate5m
        expr: sum by (job) (rate(http_requests_total[5m]))
        labels:
          team: platform

      - alert: APIHighErrorRatio
        expr: |
          sum by (job) (rate(http_requests_total{code=~"5.."}[5m]))
          /
          sum by (job) (rate(http_requests_total[5m])) > 0.05
        for: 10m
        keep_firing_for: 5m
        labels:
          severity: page
        annotations:
          summary: "{{ $labels.job }} error ratio {{ $value | humanizePercentage }}"
          runbook_url: "https://runbooks.example/api-errors"
```

约束：

- 每条规则必须有且只有一个 `record` 或 `alert`；
- `expr` 必须能解析；
- 记录规则的表达式必须是 instant vector 或 scalar；
- 告警规则必须是 instant vector；
- `for`、`keep_firing_for` 必须是非负 duration；
- 规则标签不能设置 `__name__`；
- 同一文件内规则组名不能重复（Mini 强制；真实 Prometheus 允许跨文件重复但会告警）。

## 9.2 记录规则

记录规则把查询结果写成新的时间序列：

```yaml
- record: job:http_requests:rate5m
  expr: sum by (job) (rate(http_requests_total[5m]))
  labels:
    team: platform
```

结果标签：

```text
原结果标签（去掉 __name__）
+ 规则 labels（覆盖同名）
+ __name__ = record 名称
```

写入时间戳是**规则求值时间**，不是抓取时间。求值间隔由 `group.interval`（默认 `global.evaluation_interval`）决定。

### 命名约定

推荐：

```text
level:metric:operations
```

例子：

```text
job:http_requests:rate5m
instance:node_cpu:rate5m
service:request_duration_seconds:p90
```

冒号是保留字符，业务指标不应使用。这个命名让"预聚合结果"和"原始指标"一眼可分。

### 使用场景

- 高频查询的预聚合（降低查询成本）；
- 跨实例聚合后的告警输入；
- 统一命名/标签，屏蔽底层 exporter 差异；
- 长期保留聚合结果（原始数据可缩短 retention）。

### 反模式

- 记录规则做复杂 join，导致级联依赖；
- 记录规则名与原始指标同名，造成循环/覆盖；
- 记录规则输出高基数标签，放大存储；
- 记录规则表达式引用自身。

## 9.3 告警规则与状态机

告警状态：

```text
inactive --(表达式首次为真)--> pending --(持续 for)--> firing
   ^                              |                       |
   |                              +----(表达式变假)------+
   |                                                      |
   +------------------(firing + keep_firing_for 到期)-----+
```

- `pending`：表达式为真，但未达到 `for`。
- `firing`：达到 `for`，发送给 Alertmanager。
- `inactive`：表达式为假或序列消失。
- `keep_firing_for`：表达式变假后仍保持 firing 一段时间，避免抖动导致频繁 resolve/re-fire。

标签身份：

```text
告警标签 = 序列标签（去掉 __name__）
          + 规则 labels
          + alertname = 规则名
```

任一标签变化都会被视为新告警，`for` 重新计时。这是"实例重启导致告警重新 pending"的常见原因。

## 9.4 模板

告警标签和注解都支持 Go template。常用变量：

```text
$labels.job
$labels.instance
$value
$externalLabels.cluster
$externalURL
```

常用函数：

```text
humanize, humanizePercentage, humanizeDuration
printf "%.2f" $value
```

例子：

```yaml
annotations:
  summary: "{{ $labels.instance }} CPU > 90% (current {{ $value | humanizePercentage }})"
  description: "cluster={{ $externalLabels.cluster }} value={{ printf \"%.3f\" $value }}"
```

Mini 只实现 `$labels.*`、`$value`、`$externalLabels.*` 和简单 `printf`，足以演示；生产请使用完整 Alertmanager 模板并在 `amtool` 中校验。

## 9.5 规则组求值

- 同一组内规则**串行**求值；不同组可以并行（受 `--rules.max-concurrent-evals` 等限制）。
- `group.interval` 控制求值频率；过短会导致 CPU/查询压力，过长会导致告警延迟。
- `group.limit` 限制单条规则产生的样本数；超过会报错并跳过该规则。
- 规则求值失败不会清空已有告警状态；健康状态变为 `err`，可通过 `/api/v1/rules` 查看。
- 规则求值使用的是 Prometheus 本地 TSDB；如果依赖 remote read 数据，延迟和一致性要单独评估。

## 9.6 规则单元测试

Prometheus 自带 `promtool test rules`，测试文件格式：

```yaml
rule_files:
  - rules.yml

evaluation_interval: 1m

tests:
  - interval: 1m
    input_series:
      - series: 'http_requests_total{job="api",code="500"}'
        values: "0+10x10"
      - series: 'http_requests_total{job="api",code="200"}'
        values: "0+90x10"
    promql_expr_test:
      - expr: job:http_requests:rate5m
        eval_time: 5m
        exp_samples:
          - labels: 'job:http_requests:rate5m{job="api"}'
            value: 1.6666666666666667
    alert_rule_test:
      - eval_time: 10m
        alertname: APIHighErrorRatio
        exp_alerts:
          - exp_labels:
              severity: page
              job: api
            exp_annotations:
              summary: "api error ratio 10%"
```

要点：

- `values` 语法：`0+10x10` 表示从 0 开始，每次 +10，共 10 次。
- `eval_time` 是相对测试起点的 duration。
- `exp_samples` 的标签写作序列字符串。
- 浮点比较有容差；不要写无限精度。
- 测试应覆盖：pending→firing、resolve、keep_firing_for、标签变化、空结果、除零。

Mini 提供 `python -m mini_prometheus.promtool check rules` 做结构与表达式校验，并实现了 `promtool test rules` 的教学子集（`check rules`/`test rules`）。完整差异与限制见第 42 章和 `VERIFICATION.md`。

## 9.7 规则设计原则

1. **告警表达式只输出需要告警的序列**。用 `> 阈值` 过滤，不要把所有序列都发给 Alertmanager。
2. **告警标签保持低基数**。不要带 `pod`/`instance` 之外的动态标签，除非确实需要按实例路由。
3. **一个告警一个原因**。不要把多个独立故障塞进一个表达式。
4. **记录规则命名稳定**。下游面板/告警依赖名字，改名是破坏性变更。
5. **规则依赖分层**。原始指标 → 记录规则 → 告警规则，避免告警直接做重查询。
6. **为规则写测试**。尤其是 `for`、重置、空数据、标签变化。
7. **限制基数**。记录规则输出前先 `sum by` 到业务维度。

## 9.8 规则与抓取的时间关系

假设抓取间隔 15s，规则组间隔 30s：

```text
t=0    抓取样本
t=15   抓取样本
t=30   规则求值（看到 t=15 的样本）
t=45   抓取
t=60   规则求值（看到 t=45 的样本）
```

规则求值时间戳是 `t=30`，而不是样本时间 `t=15`。因此：

- 记录规则序列的时间戳可能落在两次抓取之间；
- `rate()` 在规则内部看到的是抓取样本，输出时间戳为求值时间；
- 告警的 `activeAt` 是规则首次看到表达式为真的求值时间。

这也解释了为什么规则间隔与抓取间隔应对齐（或成整数倍），否则会出现"有的求值点看到 2 个样本，有的看到 1 个"的抖动。

## 9.9 规则组的执行、依赖与 limit

9.5 是速览；本节展开组内顺序、依赖、limit 与失败健康，这些细节决定规则在真实负载下是否可预测。

### 9.9.1 组内顺序

同一个 rule group 内的规则按 YAML 顺序**依次**求值：

```yaml
groups:
  - name: derived
    interval: 30s
    rules:
      - record: job:requests:rate5m
        expr: sum by (job) (rate(http_requests_total[5m]))
      - alert: NoRequests
        expr: job:requests:rate5m == 0
```

第二条能用到第一条本轮写入的 recording 结果（因为顺序执行、写入 TSDB 后再求值下一条）。但不要依赖"跨组顺序"：不同 group 可能并行/按各自 interval 求值，group A 的 recording 不一定在 group B 求值前完成。

需要跨组依赖时：

- 合并到一个 group（牺牲并行/隔离）；
- 或让下游容忍一个 interval 的延迟；
- 或用 `for`/`keep_firing_for` 吸收延迟。

### 9.9.2 interval 与 for

- 组 `interval` 决定求值频率；不设置时用全局 `evaluation_interval`；
- `for` 按**求值次数/时间差**累计，不是按墙钟精确到毫秒；
- 如果一次求值被跳过/延迟，`for` 的满足时间会顺延；
- `keep_firing_for` 从"条件首次消失"的求值开始计时，避免早一个 interval 失效。
- 如果规则使用 3.7+ 的 `smoothed` 区间选择器，必须给规则组设置 `query_offset`（至少一个抓取间隔），否则求值时刻窗口末端没有"未来样本"可插值；代价是告警延迟增加；
- `query_offset` 是组级字段（2.53+），未设置时继承全局 `rule_query_offset`；它只后移**查询时间**，写入/告警计时仍用求值时间。

### 9.9.3 limit

组级 `limit` 限制单条规则一次求值产生的样本/告警数：

```yaml
groups:
  - name: guarded
    limit: 1000
    rules:
      - record: job:requests:rate5m
        expr: sum by (job) (rate(http_requests_total[5m]))
```

超过 limit 时该规则本轮失败（health=err），不会静默截断。limit 是防爆炸闸门，不是容量规划工具；正常值应远低于它。

### 9.9.4 失败与健康

规则失败不会让 Prometheus 崩溃：

- 规则 `health=err`，`lastError` 记录原因；
- 组 `health=err`，`lastError` 记录最后一个错误；
- 其他规则继续求值；
- 告警状态可能停留在上一次结果。

所以必须监控：

```promql
prometheus_rule_group_last_evaluation_timestamp_seconds
prometheus_rule_group_last_duration_seconds
prometheus_rule_group_interval_seconds
```

（指标名随版本可能变化。）`last_duration` 接近 `interval` 就说明规则过载。

## 9.10 用 promtool/Mini 做规则单元测试

9.6 给了最小测试文件；这里补充可直接运行的完整例子和常见失败模式。规则是代码，应该像代码一样测试。

```yaml
rule_files:
  - rules.yml
evaluation_interval: 1m
tests:
  - name: alert fires after two minutes
    interval: 1m
    input_series:
      - series: 'up{job="api"}'
        values: '1 1 0 0 0'
    alert_rule_test:
      - eval_time: 3m
        alertname: InstanceDown
        exp_alerts:
          - exp_labels:
              severity: warning
              job: api
            exp_annotations:
              summary: "api is down"
```

对应的 `rules.yml`：

```yaml
groups:
  - name: api
    rules:
      - alert: InstanceDown
        expr: up == 0
        for: 1m
        labels:
          severity: warning
        annotations:
          summary: "{{ $labels.job }} is down"
```

运行：

```bash
promtool test rules examples/rules/demo_test.yml

# 本教程的 Mini 实现
python -m mini_prometheus.promtool test rules examples/rules/demo_test.yml
```

测试能抓住的典型错误：

- 表达式写错（`=~` vs `=`、标签名拼错）；
- `for` 太短/太长；
- 聚合丢标签导致告警标签不对；
- 模板渲染出空字符串；
- 规则依赖的 recording 没生效；
- 阈值在边界数据上不触发。

测试不能替代生产验证：合成数据不等于真实流量，告警的可执行性/路由/值班流程仍要人工评审。第 28 章给出更完整的测试策略，附录 W/Y 给出模板与工具链。

## 本章小结

- 记录规则把查询结果写成新序列，命名用 `level:metric:operations`。
- 告警规则有 pending/firing/inactive 三态，`for` 和 `keep_firing_for` 控制抖动。
- 告警身份由完整标签集合决定，任一标签变化都会重置 `for`。
- 模板支持 `$labels`、`$value`、`$externalLabels` 和函数。
- 规则必须测试：状态迁移、空数据、重置、标签变化、除零。

## 延伸阅读

- Prometheus 官方 "Recording rules" 与 "Alerting rules"。
- `promtool test rules` 文档与示例。
- 本教程第 10 章（Alertmanager）与第 40 章（Mini 规则实现）。
