# 附录 N Alertmanager 配置参考

第 10 章讲了告警生命周期的概念；本附录给出可直接对照的 Alertmanager 配置骨架、路由语义、抑制/静默、模板、HA 和校验方法。目标是让读者能独立完成一次"从 Prometheus 规则到值班通知"的配置评审，而不是只会复制一段 YAML。

## N.1 配置结构总览

Alertmanager 的配置是一个 YAML 文档，顶层字段：

```yaml
global:
  resolve_timeout: 5m

route:
  receiver: default
  group_by: [alertname, cluster, service]
  group_wait: 30s
  group_interval: 5m
  repeat_interval: 4h
  routes: []

receivers:
  - name: default
    webhook_configs:
      - url: http://alert-sink.monitoring.svc:8080/alerts

inhibit_rules: []

templates: []
```

处理顺序：

```text
Prometheus 规则 firing
  → Alertmanager 接收（/api/v2/alerts）
  → 去重（按标签 fingerprint）
  → 路由匹配（route tree）
  → 分组（group_by）
  → 抑制/静默过滤
  → 模板渲染
  → 通知（receiver）
  → 重试/记录通知日志
```

关键认知：**路由决定"发给谁"，分组决定"几条合成一条"，抑制/静默决定"发不发"**。三者不要混在一起调。

## N.2 路由树

### 根路由与子路由

```yaml
route:
  receiver: default
  group_by: [alertname, cluster]
  group_wait: 30s
  group_interval: 5m
  repeat_interval: 4h
  matchers:
    - severity=~"critical|warning|info"
  routes:
    - receiver: critical-pager
      matchers:
        - severity="critical"
      group_wait: 10s
      repeat_interval: 1h
      continue: false
    - receiver: db-team
      matchers:
        - team="db"
      group_by: [alertname, cluster, service]
      continue: true
    - receiver: fallback
      matchers:
        - team=~".+"
      continue: false
```

语义：

- 从根路由开始，深度优先匹配子路由；
- 一个告警可以匹配多个兄弟路由，但默认在**第一个匹配后停止**；
- `continue: true` 表示继续匹配后续兄弟路由（用于"同时通知平台和业务团队"）；
- 子路由未设置的字段继承父路由（`group_by`、`group_wait`、`group_interval`、`repeat_interval`）；
- `matchers` 是完整匹配列表，空列表匹配所有；
- 旧字段 `match`/`match_re` 仍可用但已弃用，新配置统一用 `matchers`。

### group_by

`group_by` 决定哪些标签相同的告警合成一条通知：

```yaml
group_by: [alertname, cluster, service]
```

- 粒度太细：一次故障产生几十条通知；
- 粒度太粗：不同服务/集群的告警混在一条，值班人员难以判断；
- `group_by: ['...']` 按所有标签分组，通常过细；
- 常见组合：`[alertname, cluster, service]` 或 `[cluster, namespace, severity]`。

### 时间参数

| 参数 | 默认 | 作用 |
|---|---|---|
| `group_wait` | 30s | 新组首次通知前的等待，用于聚合同一故障的告警 |
| `group_interval` | 5m | 同一组新增告警时，两次通知的最小间隔 |
| `repeat_interval` | 4h | 同一组持续 firing 时，重复通知的间隔 |

调参经验：

- `group_wait` 10～30s：给同一批告警聚合时间，但不要超过故障响应的容忍度；
- `group_interval` 5m：避免通知风暴；
- `repeat_interval` 1～24h：按严重级别区分，critical 可以更短；
- 所有时间都要大于 Prometheus 的 `evaluation_interval` 和抓取延迟，否则会出现"告警还没稳定就通知"。

### time_intervals

`time_intervals` 定义可复用的日历区间，路由用 `mute_time_intervals` 或 `active_time_intervals` 引用：

```yaml
time_intervals:
  - name: business-hours
    time_intervals:
      - weekdays: ['monday:friday']
        times:
          - start_time: '09:00'
            end_time: '18:00'
        location: 'Asia/Shanghai'
  - name: weekend-maintenance
    time_intervals:
      - weekdays: ['saturday', 'sunday']
        times:
          - start_time: '01:00'
            end_time: '05:00'

route:
  receiver: default
  routes:
    - matchers: ['severity="warning"']
      receiver: slack-warning
      mute_time_intervals: [weekend-maintenance]
    - matchers: ['team="data"']
      receiver: pager-data
      active_time_intervals: [business-hours]
```

- `mute_time_intervals`：区间内抑制**通知**，告警本身仍然存在；
- `active_time_intervals`：只在区间内通知；
- `location` 用 IANA 时区名；不写时用 Alertmanager 进程时区；
- 跨午夜/边界语义随版本变化，上线前用 `amtool` 或测试环境验证。

## N.3 接收器

### Webhook

```yaml
receivers:
  - name: alert-sink
    webhook_configs:
      - url: http://alert-sink.monitoring.svc:8080/alerts
        send_resolved: true
        max_alerts: 0
```

Webhook 是最通用的集成方式：把告警交给自建网关，再由网关分发到 IM/工单/短信。`max_alerts: 0` 表示不限制单条通知内的告警数。

### Email

```yaml
global:
  smtp_smarthost: smtp.example.com:587
  smtp_from: alertmanager@example.com
  smtp_auth_username: alertmanager
  smtp_auth_password_file: /etc/alertmanager/smtp-password
  smtp_require_tls: true

receivers:
  - name: email-oncall
    email_configs:
      - to: oncall@example.com
        send_resolved: true
        headers:
          Subject: '[{{ .Status | toUpper }}] {{ .CommonLabels.alertname }}'
```

密码不要写进 YAML；用 `*_file` 或外部 secret 注入。邮件适合低优先级/摘要，不适合作为 critical 的唯一通道。

### Slack

```yaml
receivers:
  - name: slack-platform
    slack_configs:
      - api_url_file: /etc/alertmanager/slack-webhook
        channel: '#alerts-platform'
        send_resolved: true
        title: '{{ .Status | toUpper }} {{ .CommonLabels.alertname }}'
        text: >-
          {{ range .Alerts }}{{ .Annotations.summary }}
          {{ .Annotations.description }}{{ end }}
```

### PagerDuty

```yaml
receivers:
  - name: pagerduty-critical
    pagerduty_configs:
      - routing_key_file: /etc/alertmanager/pagerduty-key
        severity: critical
        send_resolved: true
        description: '{{ .CommonLabels.alertname }} on {{ .CommonLabels.cluster }}'
```

严重级别与通道要匹配：critical → 呼叫/分页；warning → IM；info → 工单/邮件。把 warning 发到 PagerDuty 是最常见的告警疲劳来源。

### 其他通道

Alertmanager 还内置 `opsgenie_configs`、`victorops_configs`、`wechat_configs`、`telegram_configs`、`pushover_configs`、`sns_configs`、`webex_configs`、`msteams_configs`、`discord_configs`。原则相同：secret 外置、`send_resolved` 明确、消息模板简洁。

## N.4 抑制规则

抑制用于"上游故障时，不要再报下游的衍生告警"：

```yaml
inhibit_rules:
  - source_matchers:
      - severity="critical"
    target_matchers:
      - severity=~"warning|info"
    equal: [alertname, cluster, service]

  - source_matchers:
      - alertname="ServiceDown"
    target_matchers:
      - alertname=~"HighErrorRate|HighLatency"
    equal: [cluster, service]
```

语义：

- 当存在匹配 `source_matchers` 的 firing 告警，且 `equal` 中的标签与目标告警完全相同时，目标告警被抑制；
- `equal` 为空表示只按 matcher 匹配，容易误抑制；
- `equal` 中的标签缺失时按空字符串比较；如果这些标签在源和目标告警里**都不存在**，规则会匹配。用 `label=~".+"` 或保证标签始终存在；
- 抑制不是静默：源告警恢复后，目标告警会自动重新出现（如果仍 firing）；
- 抑制规则要可解释，避免"为什么这条告警没发"成为排障黑洞。

常见错误：

- `equal` 漏了 `cluster`，导致 A 集群的故障抑制 B 集群的告警；
- source 用 `severity="critical"` 但 critical 告警本身也被路由到同一通道，造成"源和目标都没通知"；
- 抑制链过长，真正的根因告警被另一条告警抑制。

## N.5 静默

静默是人工维护窗口，通过 UI 或 API 创建：

```json
{
  "matchers": [
    {"name": "alertname", "value": "HighErrorRate", "isRegex": false, "isEqual": true},
    {"name": "cluster", "value": "prod-a", "isRegex": false, "isEqual": true}
  ],
  "startsAt": "2026-11-26T10:00:00Z",
  "endsAt": "2026-11-26T12:00:00Z",
  "createdBy": "oncall",
  "comment": "planned database migration"
}
```

命令行：

```bash
amtool silence add alertname=HighErrorRate cluster=prod-a \
  --duration=2h --comment="planned database migration" --author=oncall

amtool silence query
amtool silence expire <silence-id>
```

要点：

- 静默必须有过期时间；永久静默等于删除告警；
- 静默要写 comment 和 createdBy，便于审计；
- 静默匹配的是告警标签，不是 PromQL；
- 静默不会停止规则求值，只停止通知；
- 维护窗口结束后要复查是否还有未恢复的告警。

## N.6 模板

Alertmanager 使用 Go `text/template`。模板文件通过 `templates` 加载：

```yaml
templates:
  - /etc/alertmanager/templates/*.tmpl
```

一个最小模板：

```gotmpl
{{ define "platform.title" -}}
[{{ .Status | toUpper }}] {{ .CommonLabels.alertname }} ({{ .CommonLabels.cluster }})
{{- end }}

{{ define "platform.text" -}}
{{ range .Alerts -}}
- {{ .Labels.service }}: {{ .Annotations.summary }}
  runbook: {{ .Annotations.runbook_url }}
{{ end -}}
{{- end }}
```

常用字段：

- `.Status`：firing/resolved；
- `.Alerts`：当前通知中的告警列表；
- `.CommonLabels` / `.CommonAnnotations`：组内公共标签/注解；
- `.GroupLabels`：分组标签；
- `.ExternalURL`：Alertmanager 外部地址，用于回链；
- `.Alerts.Firing` / `.Alerts.Resolved`：按状态过滤。

注意事项：

- 模板中的标签值可能包含特殊字符，IM 渲染要做转义；
- 不要在模板里做复杂逻辑，复杂逻辑应放在告警表达式或 recording rule；
- 模板错误会导致通知发送失败，必须用 `amtool check-config` 和测试告警验证；
- `runbook_url` 应指向可访问的页面，而不是内网 wiki 的裸链接。

## N.7 高可用

Alertmanager 通过 gossip 组成集群：

```bash
alertmanager \
  --config.file=/etc/alertmanager/alertmanager.yml \
  --storage.path=/alertmanager \
  --cluster.listen-address=0.0.0.0:9094 \
  --cluster.advertise-address=10.0.0.11:9094 \
  --cluster.peer=10.0.0.12:9094 \
  --cluster.peer=10.0.0.13:9094
```

语义：

- 静默、通知日志通过 gossip 复制；
- 同一告警只由一个节点发送通知（去重）；
- Prometheus HA 对的两个副本会向所有 Alertmanager 发送相同告警，Alertmanager 按标签去重；
- 所有节点应使用相同配置；配置不一致会导致行为漂移；
- 集群端口要限制在内网，gossip 不是公网协议。

Prometheus 侧配置多个 Alertmanager：

```yaml
alerting:
  alertmanagers:
    - static_configs:
        - targets:
            - alertmanager-0:9093
            - alertmanager-1:9093
            - alertmanager-2:9093
```

## N.8 API 与运维端点

| 端点 | 用途 |
|---|---|
| `GET /-/healthy` | 进程健康 |
| `GET /-/ready` | 是否可接收告警 |
| `GET /api/v2/status` | 集群/配置状态 |
| `GET /api/v2/alerts` | 当前告警 |
| `POST /api/v2/alerts` | 接收告警（Prometheus 使用） |
| `GET /api/v2/silences` | 静默列表 |
| `POST /api/v2/silences` | 创建静默 |
| `DELETE /api/v2/silence/{id}` | 删除静默 |

运维建议：

- 监控 `alertmanager_notifications_failed_total`、`alertmanager_notification_latency_seconds`、队列长度；
- 监控 Alertmanager 集群成员数；
- 定期检查 silence 是否有永久/过期未清理；
- 升级前备份 `--storage.path`（silence/通知日志）；
- 配置 `--web.external-url`，确保通知里的回链正确。

## N.9 校验与测试

### 配置语法

```bash
amtool check-config /etc/alertmanager/alertmanager.yml
```

### 路由测试

```bash
amtool config routes test \
  --config.file=/etc/alertmanager/alertmanager.yml \
  severity=critical cluster=prod-a team=db
```

输出会显示匹配的路由和最终 receiver。每次改路由树都应跑一遍关键标签组合。

### 告警规则测试

Prometheus 侧用 `promtool test rules` 验证表达式和 `for`：

```bash
promtool test rules tests/rules_test.yml
```

Alertmanager 侧没有等价的"通知内容单元测试"，但可以：

- 用 `amtool alert add` 注入测试告警；
- 用 webhook sink 捕获真实 payload；
- 在 staging 配置一个测试 receiver，走完整链路；
- 对模板做渲染测试（把模板当普通 Go template 执行）。

## N.10 常见错误与检查清单

- [ ] 路由树是否有默认 receiver，避免"匹配不到就不发"；
- [ ] `continue` 是否符合预期，是否造成重复通知；
- [ ] `group_by` 是否与故障域一致；
- [ ] `group_wait`/`group_interval`/`repeat_interval` 是否形成合理节奏；
- [ ] critical/warning/info 是否路由到不同通道；
- [ ] 抑制规则的 `equal` 是否包含集群/服务等关键标签；
- [ ] 静默是否都有过期时间和注释；
- [ ] secret 是否通过文件/外部注入，未写入 Git；
- [ ] 模板是否用 `amtool check-config` 校验；
- [ ] HA 集群成员是否互相可达，配置是否一致；
- [ ] 通知失败/延迟是否有监控；
- [ ] 是否定期演练"告警→通知→处置"全链路。

## 延伸阅读

- Prometheus 官方 Alertmanager 文档：Configuration、Routing、Inhibition、Silences、Templates。
- `amtool` 文档与 `promtool test rules`。
- 本教程第 10 章（告警生命周期）、第 26 章（告警工程）、附录 M（Grafana 告警）。
