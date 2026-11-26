# 第 10 章 告警生命周期与 Alertmanager

## 10.1 为什么告警要独立进程

Prometheus 负责"判断表达式是否为真"，Alertmanager 负责"把告警变成通知"。拆开的原因：

- **去重与分组**：同一个故障会命中多个实例/规则，Alertmanager 按标签聚合。
- **抑制与静默**：维护窗口、上游故障时压制下游告警。
- **路由**：不同团队/严重级别走不同渠道。
- **HA**：多个 Alertmanager 组成集群，避免单点。
- **通知集成**：邮件、Slack、PagerDuty、Webhook、Opsgenie 等。

Prometheus 只向 Alertmanager 发送告警事件；它不直接发邮件，也不维护路由树。

## 10.2 告警事件的生命周期

```text
Prometheus 规则求值
  → firing（首次达到 for）
  → 每次求值刷新（endsAt 延长）
  → 表达式变假 → resolved（endsAt = 解决时间）
  → 发送到 Alertmanager
  → 分组/抑制/静默/路由
  → 通知
```

关键字段（v2 API）：

```json
{
  "labels": {"alertname": "APIHighErrorRatio", "severity": "page", "job": "api"},
  "annotations": {"summary": "api error ratio 10%"},
  "startsAt": "2026-11-26T10:00:00.000Z",
  "endsAt": "2026-11-26T10:15:00.000Z",
  "generatorURL": "http://prometheus:9090/graph?g0.expr=...",
  "fingerprint": "9c8f..."
}
```

- `startsAt` 是告警首次 active 的时间（`activeAt`），不是首次 firing。
- firing 时 `endsAt` 通常是"现在 + resolve timeout"，用于表达"如果不再刷新就过期"。
- resolve 时 `endsAt` 是解决时间。
- `fingerprint` 由标签集合哈希得到（Alertmanager 自己也会计算）。

## 10.3 Prometheus 侧配置

```yaml
alerting:
  alertmanagers:
    - static_configs:
        - targets: ["alertmanager-0:9093", "alertmanager-1:9093"]
      scheme: http
      timeout: 10s
      api_version: v2
      path_prefix: ""
      basic_auth:
        username: prometheus
        password_file: /etc/prometheus/am-password

alert_relabel_configs:
  - source_labels: [severity]
    regex: info
    action: drop

external_labels:
  cluster: prod-cn
  # HA 对不要在这里放 replica：告警标签不同会让 Alertmanager 无法去重。
  # 如果 remote write 确实需要 replica，用 remote_write.write_relabel_configs 添加。
```

要点：

- `alert_relabel_configs` 在发送前修改告警标签（不是指标标签）。
- `external_labels` 会附加到所有出站数据（包括 remote write、federation、告警），用于区分集群；**HA 对必须保持一致**。
- `path_prefix` 用于 Alertmanager 部署在子路径时。
- 多个 Alertmanager 目标时，Prometheus 会向所有目标发送；Alertmanager 集群内部去重。

## 10.4 通知重试与失败语义

Prometheus 的 notifier：

- 批量发送告警（`batch_size`，默认 64？不同版本不同）；
- 2xx：成功；
- 4xx（除 429）：永久失败，丢弃该批（避免毒消息无限重试）；
- 5xx/网络错误/429：重试，带退避；
- 队列有容量上限，满了会丢弃并计数。

监控 notifier 自身：

```promql
rate(prometheus_notifications_errors_total[5m]) > 0
prometheus_notifications_queue_length
prometheus_notifications_dropped_total
```

如果 `dropped_total` 增长，说明 Alertmanager 不可达或处理不过来，告警可能丢失。

## 10.5 Alertmanager 路由树

```yaml
route:
  receiver: default
  group_by: [alertname, cluster, service]
  group_wait: 30s
  group_interval: 5m
  repeat_interval: 4h
  routes:
    - matchers: ['severity="critical"']
      receiver: pagerduty
      group_wait: 10s
      repeat_interval: 1h
      continue: false
      routes:
        - matchers: ['service="payments"']
          receiver: payments-oncall
    - matchers: ['team="data"']
      receiver: slack-data
      continue: true
    - matchers: ['environment="dev"']
      receiver: "null"
```

- `group_by`：相同标签组合的告警合并成一条通知。
- `group_wait`：首次通知前的等待，给同组告警聚合时间。
- `group_interval`：同组新增告警时的通知间隔。
- `repeat_interval`：未解决的告警重复提醒间隔。
- `continue`：匹配后是否继续匹配后续兄弟路由。
- `matchers` 是新的写法；老版本用 `match`/`match_re`。

### group_by 的选择

- 按 `alertname + cluster + service` 分组，通常能平衡"聚合"与"可定位"。
- 按 `instance` 分组会导致每个实例一条通知，告警风暴。
- 按 `pod` 分组会在扩缩容时不断产生新组。
- 不分组（`group_by: []`）会把所有告警合成一条，难以定位。

### 通知节奏

- `group_wait`：新组第一次通知前等待多久，用于收集同批告警；
- `group_interval`：同一组有变化时，两次通知的最小间隔；
- `repeat_interval`：没有变化时，重复提醒的间隔。

调参经验：

- critical：`group_wait` 短（10～30s），`repeat_interval` 短（1h）；
- warning：`group_wait` 长（1～5m），`repeat_interval` 长（4～24h）；
- `group_interval` 不要小于 `group_wait`，否则会连续轰炸；
- 选择原则是"接收者需要一起处理的最小集合"，不是越细越好。

### matchers 语法

不同版本支持 `=`/`!=`/`=~`/`!~`，以及 `severity="critical"` 或 `severity=critical` 的写法。升级时检查：

- 旧版 `match`/`match_re` 是否已迁移到 `matchers`；
- 正则是否锚定/转义；
- 空值 matcher 的语义（标签不存在 vs 空字符串）。

## 10.6 抑制（inhibition）

```yaml
inhibit_rules:
  - source_matchers: ['severity="critical"', 'alertname="ClusterDown"']
    target_matchers: ['severity=~"warning|critical"']
    equal: [cluster]
  - source_matchers: ['alertname="ServiceDown"']
    target_matchers: ['alertname=~"HighErrorRate|HighLatency"']
    equal: [service]
```

语义：当 source 告警 firing 时，抑制所有匹配 target 且 `equal` 标签相同的告警。

用途：

- 集群级故障时压制实例级告警；
- 服务不可用时压制错误率/延迟告警；
- 维护窗口用 silence 而不是 inhibition（inhibition 是动态的，silence 是人为的）。

陷阱：

- `equal` 中的标签在缺失时按**空字符串**比较；如果 `equal` 列出的标签在源告警和目标告警里**都不存在**，规则反而会匹配。用 `label=~".+"` 或保证标签存在，避免误抑制。
- 抑制是单向的；不要写出环。
- 抑制不会阻止告警进入 Alertmanager，只是不通知；UI 仍能看到。

## 10.7 静默（silence）

```bash
amtool silence add 'alertname=~"High.*"' \
  --duration=2h \
  --comment="maintenance window" \
  --author="oncall"
```

- 基于 matcher，支持正则；
- 有开始/结束时间；
- 需要作者与注释（审计）；
- 过期自动失效；
- 不要用静默掩盖长期问题；应修复或调整阈值。

## 10.8 模板与通知

Alertmanager 模板例子：

```text
{{ define "slack.title" }}
[{{ .Status | toUpper }}] {{ .CommonLabels.alertname }} ({{ .Alerts | len }})
{{ end }}

{{ define "slack.text" }}
{{ range .Alerts }}
{{ .Labels.instance }}: {{ .Annotations.summary }}
{{ end }}
{{ end }}
```

关键概念：

- `.Alerts` 是当前通知中的告警列表；
- `.CommonLabels`/`.CommonAnnotations` 是组内公共部分；
- `.Status` 是 firing/resolved；
- `.ExternalURL` 用于生成回链；
- 模板错误会导致通知失败，需在部署前用 `amtool template` 校验。

## 10.9 HA 与去重

Alertmanager 集群：

- 节点通过 gossip 同步 fingerprint、silence 和 notification log；
- 同一告警只由一个节点发送通知（去重）；
- Prometheus HA 对（两个副本抓同样目标）会向所有 Alertmanager 发送相同告警，Alertmanager 按标签去重；
- 如果两个 Prometheus 副本的 `external_labels` 不同（例如各自带 `replica: prom-0/1`），告警标签不同，无法去重。HA 对必须使用相同 external labels，或者只让一个副本发送告警；
- 网络分区时两边可能各自通知；`--cluster.*` 配置错误会导致集群不成立。

Prometheus 自身不做告警去重；去重发生在 Alertmanager。

## 10.10 时间区间：time_intervals 与 mute_timeouts

Alertmanager 除了 silence，还支持**计划性的时间区间**：按星期/时段/时区定义，在路由上引用。它适合"工作时间才通知 warning""周末维护窗口不打扰"这类固定策略。

```yaml
time_intervals:
  - name: business-hours
    time_intervals:
      - weekdays: ['monday:friday']
        times:
          - start_time: '09:00'
            end_time: '18:00'
        location: 'Asia/Shanghai'
  - name: maintenance-window
    time_intervals:
      - weekdays: ['saturday']
        times:
          - start_time: '02:00'
            end_time: '04:00'
        location: 'Asia/Shanghai'

route:
  receiver: default
  routes:
    - matchers: ['severity="warning"']
      receiver: slack-warning
      mute_time_intervals: [maintenance-window]
    - matchers: ['team="data"']
      receiver: pager-data
      active_time_intervals: [business-hours]
```

语义：

- `mute_time_intervals`：在这些区间内**不发送通知**，告警仍然存在，UI/API 仍能看到；
- `active_time_intervals`：只在这些区间内发送通知；
- `weekdays` 支持 `monday:friday` 区间和列表；`location` 使用 IANA 时区；
- 时间区间是**通知策略**，不是 silence；它不能替代修复，也不改变告警状态；
- 跨午夜、边界包含关系（start/end 是否闭区间）以你使用的 Alertmanager 版本文档为准，上线前用 `amtool` 验证。

与另外两个机制的分工：

| 机制 | 适用 | 是否动态 |
|---|---|---|
| `time_intervals` | 固定值班时间、维护窗口 | 按日历重复 |
| silence | 临时屏蔽某个已知问题 | 人工创建/过期 |
| inhibition | 上游故障时压制下游告警 | 由告警状态驱动 |

## 10.11 告警设计检查表

- 告警是否对应**用户可见的症状**，而不是原因？
- 是否有 `runbook_url`？
- `for` 是否足够长以过滤抖动，又不至于延迟故障响应？
- 阈值是否基于 SLO/历史数据，而不是拍脑袋？
- 是否有对应的 silence/inhibition/time_intervals 策略？
- 是否会在故障时产生告警风暴？
- 通知渠道是否与严重级别匹配？
- 是否定期演练"告警到处置"的全链路？

## 10.12 Mini 的实现

Mini 包含三部分：

1. `rules/manager.py`：告警状态机、`for`、`keep_firing_for`、模板、firing/resolved 负载。
2. `rules/notifier.py`：批量、重试、4xx 丢弃、后台 worker。
3. `examples/alertmanager_lite.py`：接收 v2 告警、按 fingerprint 去重、按 alertname 分组、critical 抑制同 service 的 warning。

Mini 的 Alertmanager-lite **不实现**：路由树、silence、time_intervals、gossip HA、真实通知渠道。生产必须使用官方 Alertmanager。

## 10.13 常见故障

| 现象 | 可能原因 |
|---|---|
| 告警不通知 | 路由不匹配、receiver 配置错、silence、抑制、time_intervals、Alertmanager 不可达 |
| 重复通知 | HA 未去重（external_labels 不一致）、group_by 太细、fingerprint 变化、通知重试 |
| 通知延迟 | group_wait/group_interval 太长、队列积压、receiver 慢 |
| 恢复通知缺失 | `endsAt`/resolve 语义、Alertmanager 重启、通知失败 |
| 静默无效 | matchers 不匹配、silence 过期、时区/时间格式 |
| 维护窗口仍通知 | time_intervals 未挂到路由、location/星期写错 |

完整配置字段与模板见附录 N；告警设计与 on-call 见第 26 章。

## 本章小结

- Prometheus 判断告警，Alertmanager 负责去重、分组、抑制、静默、路由和通知。
- 告警身份由标签决定；external labels 必须与 HA 策略一致。
- `group_by` 决定通知聚合粒度，`group_wait`/`group_interval`/`repeat_interval` 决定节奏。
- inhibition 是动态抑制，silence 是人工窗口，time_intervals 是计划性通知窗口；三者都不能替代修复。
- notifier 的队列、重试、丢弃指标本身也需要监控。

## 延伸阅读

- Prometheus 官方 "Alerting" 与 Alertmanager 文档。
- Alertmanager 配置、模板、`amtool` 文档。
- 本教程附录 N（Alertmanager 配置参考）与第 26 章（告警工程）。
- 本教程第 9 章（告警规则）与第 40 章（Mini 告警实现）。
