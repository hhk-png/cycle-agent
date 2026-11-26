# 附录 B 配置参考

## B.1 全局

```yaml
global:
  scrape_interval: 15s
  scrape_timeout: 10s
  evaluation_interval: 15s
  external_labels:
    cluster: prod-cn
    # HA 对不要把 replica 放进 external_labels，否则告警无法去重；
    # remote write 需要副本标识时用 write_relabel_configs。
  scrape_protocols:
    - OpenMetricsText1.0.0
    - PrometheusText0.0.4
  rule_query_offset: 0s    # 2.53+：规则组 query_offset 的全局默认值
```

约束：

- `scrape_timeout <= scrape_interval`；
- duration 单位：`ms/s/m/h/d/w/y`；
- `external_labels` 进入 remote write/federation/告警；HA 对必须一致；
- lookback 不是 `global` 字段，用 CLI `--query.lookback-delta`（Operator：`spec.query.lookbackDelta`）；
- `scrape_protocols` 排序即优先级，具体可用值随版本变化；
- `rule_query_offset`（2.53+）是规则组级 `query_offset` 的默认值；组里显式设置时覆盖全局。它把规则的**查询时间**后移，写入时间仍是求值时间，适合 remote write/抓取延迟。

## B.2 scrape_configs

```yaml
scrape_configs:
  - job_name: api
    scrape_interval: 15s
    scrape_timeout: 5s
    metrics_path: /metrics
    scheme: https
    params:
      module: [http_2xx]
    static_configs:
      - targets: ["api-0:8080", "api-1:8080"]
        labels:
          env: prod
    file_sd_configs:
      - files: ["/etc/prometheus/sd/*.yml"]
        refresh_interval: 30s
    http_sd_configs:
      - url: https://cmdb.example/targets
        refresh_interval: 1m
    dns_sd_configs:
      - names: ["api.internal"]
        type: A
        port: 8080
    honor_labels: false
    honor_timestamps: true
    sample_limit: 50000
    label_limit: 30
    label_name_length_limit: 200
    label_value_length_limit: 500
    body_size_limit: 10485760
    basic_auth:
      username: prometheus
      password_file: /etc/prometheus/secrets/api-password
    bearer_token_file: /etc/prometheus/secrets/token
    tls_config:
      ca_file: /etc/prometheus/tls/ca.crt
      cert_file: /etc/prometheus/tls/client.crt
      key_file: /etc/prometheus/tls/client.key
      server_name: api.internal
      insecure_skip_verify: false
    relabel_configs: []
    metric_relabel_configs: []
```

## B.3 relabel

```yaml
relabel_configs:
  - source_labels: [__address__]
    separator: ";"
    regex: "([^:]+):(.*)"
    target_label: host
    replacement: "$1"
    action: replace

  - source_labels: [__meta_kubernetes_pod_annotation_prometheus_io_scrape]
    regex: "true"
    action: keep

  - source_labels: [__meta_kubernetes_namespace]
    regex: "kube-system"
    action: drop

  - source_labels: [__address__]
    modulus: 4
    target_label: __tmp_shard
    action: hashmod

  - source_labels: [__tmp_shard]
    regex: "0"
    action: keep

  - regex: "__meta_kubernetes_pod_label_(.+)"
    replacement: "k8s_$1"
    action: labelmap

  - regex: "__meta_.*"
    action: labeldrop

  - source_labels: [environment]
    target_label: env
    action: lowercase
```

动作：`replace/keep/drop/hashmod/labelmap/labeldrop/labelkeep/lowercase/uppercase/keepequal/dropequal`。

## B.4 认证/TLS

```yaml
basic_auth:
  username: user
  password: pass
  password_file: /path

authorization:
  type: Bearer
  credentials: token
  credentials_file: /path

oauth2:
  client_id: id
  client_secret: secret
  token_url: https://auth/token
  scopes: ["read"]

tls_config:
  ca_file: ca.crt
  cert_file: client.crt
  key_file: client.key
  server_name: host
  insecure_skip_verify: false
  min_version: TLS12
```

Mini 支持 basic_auth/bearer/tls 客户端（CA/client cert/insecure）；不支持 oauth2。

## B.5 规则与告警

```yaml
rule_files:
  - "rules/*.yml"

alerting:
  alertmanagers:
    - static_configs:
        - targets: ["alertmanager:9093"]
      scheme: http
      timeout: 10s
      api_version: v2
      path_prefix: ""
      basic_auth:
        username: prometheus
        password_file: /etc/prometheus/secrets/am

alert_relabel_configs:
  - source_labels: [severity]
    regex: info
    action: drop
```

规则组还可以设置 `query_offset`（2.53+；早期以 `evaluation_delay` 提案出现），把查询时间整体后移；未设置时继承 `global.rule_query_offset`：

```yaml
groups:
  - name: slo
    interval: 1m
    query_offset: 1m      # 配合 [5m] smoothed，保证窗口末端有未来样本
    rules:
      - record: job:http_requests:rate5m
        expr: sum by (job) (rate(http_requests_total[5m] smoothed))
      - alert: APIHighErrorRatio
        expr: job:http_errors:ratio5m > 0.05
        for: 5m
        labels:
          severity: page
```

`query_offset` 会增加告警延迟；只在需要 `smoothed`/`anchored` 的组使用。

## B.6 remote write/read

```yaml
remote_write:
  - url: https://mimir.example/api/v1/push
    name: mimir
    remote_timeout: 30s
    headers:
      X-Scope-OrgID: tenant-a
    queue_config:
      capacity: 10000
      max_shards: 20
      max_samples_per_send: 2000
      batch_send_deadline: 5s
      min_backoff: 30ms
      max_backoff: 5s
    write_relabel_configs:
      - source_labels: [__name__]
        regex: "go_.*"
        action: drop

remote_read:
  - url: https://mimir.example/api/v1/read
    read_recent: true
```

Mini 的 remote_write 配置字段较简化：

```yaml
remote_write:
  - url: http://127.0.0.1:9201/api/v1/write
    protocol: json        # 或 prometheus
    batch_size: 500
    max_retries: 5
```

## B.7 Mini 的完整示例

```yaml
global:
  scrape_interval: 15s
  scrape_timeout: 5s
  evaluation_interval: 15s
  external_labels:
    cluster: demo

scrape_configs:
  - job_name: demo
    static_configs:
      - targets: ["127.0.0.1:9100"]
        labels:
          env: lab
    metric_relabel_configs:
      - source_labels: [__name__]
        regex: "go_.*"
        action: drop

rule_files:
  - "../rules/demo.yml"

alerting:
  alertmanagers:
    - static_configs:
        - targets: ["127.0.0.1:9093"]

remote_write:
  - url: http://127.0.0.1:9201/api/v1/write
    protocol: json
```

## B.8 校验

```bash
promtool check config prometheus.yml
promtool check rules rules/*.yml
python -m mini_prometheus check config prometheus.yml
python -m mini_prometheus check rules rules/*.yml
```

## B.9 常见配置错误

- `scrape_timeout` > `scrape_interval`；
- 重复 `job_name`；
- relabel regex 忘记 `.*`（全匹配）；
- `__address__` 被 relabel 删除；
- `file_sd` 路径相对工作目录；
- `rule_files` glob 无匹配；
- `external_labels` 在 HA 对不一致；
- remote write URL 缺少协议/路径。

## B.10 scrape_config 完整字段

按"抓取行为 / 认证 / 限制 / 发现"分组：

```yaml
scrape_configs:
  - job_name: api
    # 抓取行为
    scrape_interval: 15s
    scrape_timeout: 10s
    metrics_path: /metrics
    scheme: https
    params:
      module: [http_2xx]
    honor_labels: false
    honor_timestamps: true
    track_timestamps_staleness: false
    scrape_protocols: [OpenMetricsText1.0.0, PrometheusText0.0.4]
    scrape_native_histograms: true
    scrape_classic_histograms: false
    always_scrape_classic_histograms: false
    convert_classic_histograms_to_nhcb: false
    native_histogram_bucket_limit: 0
    native_histogram_min_bucket_factor: 0

    # HTTP 客户端
    follow_redirects: true
    enable_http2: true
    enable_compression: true
    proxy_url: http://proxy:3128
    proxy_from_environment: false
    proxy_connect_header:
      X-Proxy-Auth: ["secret"]
    http_headers:
      X-Scope-OrgID:
        values: [tenant-a]

    # 认证
    basic_auth:
      username: prometheus
      password_file: /etc/prometheus/secrets/api-password
    authorization:
      type: Bearer
      credentials_file: /etc/prometheus/secrets/token
    oauth2:
      client_id: prometheus
      client_secret_file: /etc/prometheus/secrets/oauth-secret
      token_url: https://auth.example/token
      scopes: [metrics.read]
      endpoint_params:
        audience: [prometheus]
    tls_config:
      ca_file: /etc/prometheus/tls/ca.crt
      cert_file: /etc/prometheus/tls/client.crt
      key_file: /etc/prometheus/tls/client.key
      server_name: api.internal
      insecure_skip_verify: false
      min_version: TLS12

    # 限制
    body_size_limit: 10MiB
    sample_limit: 50000
    label_limit: 30
    label_name_length_limit: 200
    label_value_length_limit: 500
    target_limit: 1000
    keep_dropped_targets: 100

    # 发现
    static_configs: []
    file_sd_configs: []
    http_sd_configs: []
    dns_sd_configs: []
    kubernetes_sd_configs: []
    relabel_configs: []
    metric_relabel_configs: []
```

> 字段名/默认值随版本变化，以上示例请以 `promtool check config` 和官方文档为准。

限制的语义：

- `sample_limit`：单次抓取最多样本数，超过则整次抓取失败（不是截断）；
- `label_limit`：单样本最多标签数；
- `label_name_length_limit` / `label_value_length_limit`：标签名/值长度；
- `body_size_limit`：响应体大小；
- `target_limit`：该 job 最多目标数，超过丢弃并记录；
- `keep_dropped_targets`：保留多少被丢弃目标用于调试。

这些限制是保护 Prometheus 的闸门，不是"建议值"。高基数事故中，`sample_limit` 往往是最后一道防线。

## B.11 TLS / HTTP 客户端细节

### B.11.1 TLS 版本与证书

```yaml
tls_config:
  ca_file: ca.crt
  ca_inline: |
    -----BEGIN CERTIFICATE-----
    ...
    -----END CERTIFICATE-----
  cert_file: client.crt
  key_file: client.key
  server_name: api.internal
  insecure_skip_verify: false
  min_version: TLS12
  max_version: TLS13
```

注意：

- `server_name` 用于 SNI/证书校验，IP 直连时尤其重要；
- `insecure_skip_verify: true` 只应在测试使用；
- 证书轮换要重启/重载；
- `ca_inline`/`cert_inline`/`key_inline` 适合 Secret 注入，避免文件权限问题。

### B.11.2 OAuth2

```yaml
oauth2:
  client_id: prometheus
  client_secret_file: /etc/prometheus/secrets/oauth
  token_url: https://auth.example/token
  scopes: [metrics.read]
  endpoint_params:
    audience: [prometheus]
  tls_config:
    ca_file: ca.crt
```

OAuth2 token 会缓存/刷新；`token_url` 不可达会导致抓取失败。Mini 不支持 OAuth2。

### B.11.3 代理

```yaml
proxy_url: http://proxy:3128
proxy_from_environment: false
proxy_connect_header:
  Proxy-Authorization: ["Basic ..."]
```

`proxy_from_environment` 会读 `HTTP_PROXY`/`HTTPS_PROXY`/`NO_PROXY`；在容器里要确认环境变量没有意外继承。

## B.12 storage / tracing / otlp

### B.12.1 storage

```yaml
storage:
  tsdb:
    out_of_order_time_window: 30m
    exemplars:
      max_exemplars: 100000
```

- `out_of_order_time_window`：0 表示关闭；开启后接受窗口内乱序；
- `max_exemplars`：0 表示关闭 exemplar 存储；
- 其他 TSDB 参数（retention、block duration）主要是 CLI flag，不是 YAML。

### B.12.2 tracing

```yaml
tracing:
  endpoint: otel-collector:4317
  client_type: grpc
  headers:
    x-api-key: secret
  insecure: false
  sampling_fraction: 1.0
```

Prometheus 自身可以导出 tracing（抓取/查询/规则）；采样率过高会影响性能。

### B.12.3 otlp

```yaml
otlp:
  promote_resource_attributes:
    - service.instance.id
    - service.name
    - service.namespace
    - service.version
  keep_identifying_resource_attributes: true
  translation_strategy: UnderscoreEscapingWithSuffixes
```

`translation_strategy` 控制 OTel 指标名/标签到 Prometheus 的转换（下划线转义、后缀等）。不同版本对 OTLP 接收端的默认开关不同，以 release notes 为准。

## B.13 规则组字段

```yaml
groups:
  - name: api
    interval: 30s
    query_offset: 1m
    limit: 1000
    rules:
      - record: job:http_requests:rate5m
        expr: sum by (job) (rate(http_requests_total[5m]))
        labels:
          team: platform
      - alert: APIHighErrorRatio
        expr: job:http_errors:ratio5m > 0.05
        for: 5m
        keep_firing_for: 5m
        labels:
          severity: page
        annotations:
          summary: "{{ $labels.job }} error ratio high"
          runbook_url: "https://example.invalid/runbooks/api"
```

字段：

- `name`：组名，出现在 `/api/v1/rules`；
- `interval`：求值间隔，默认 `global.evaluation_interval`；
- `query_offset`：把查询时间整体后移，适合 `smoothed`/`anchored` 或延迟数据；未设置时用 `global.rule_query_offset`，写入时间仍是求值时间；
- `limit`：单次求值最多返回的告警/样本数，超过则规则失败；
- `for`：持续多久才 firing；
- `keep_firing_for`：条件消失后保持 firing 的时间，避免抖动；
- `labels`/`annotations`：模板可用 `$labels`/`$value`/`$externalLabels`。

## B.14 版本差异与迁移

配置不是永远向后兼容的。升级前检查：

- `scrape_protocols` 的默认顺序（3.x 默认 OpenMetrics 优先）；
- native histogram 的旧 flag 是否已 no-op；
- `--enable-feature` 是否被移除/转正；
- remote write 2.0 的配置字段是否变化；
- rule group 的 `query_offset`/`limit` 是否支持；
- OTLP 接收端默认开关；
- UTF-8 指标名校验开关。

做法：

1. 用目标版本的 `promtool check config` 校验；
2. 在测试环境启动，观察日志/warning；
3. 对比 `/api/v1/status/config`（注意脱敏）；
4. 灰度升级，保留回滚；
5. 更新配置仓库与 runbook。

## B.15 一个校验脚本

```bash
#!/usr/bin/env bash
set -euo pipefail

CONFIG=${1:-prometheus.yml}
RULES_DIR=${2:-rules}

promtool check config "$CONFIG"
find "$RULES_DIR" -name '*.yml' -o -name '*.yaml' | while read -r f; do
  promtool check rules "$f"
done
```

CI 中再加：

- `promtool test rules`；
- `pint lint`（规则质量）；
- YAML lint；
- Secret 扫描（避免密码进 Git）。

## 延伸阅读

- Prometheus 官方 "Configuration"。
- 本教程第 11、12、18 章。
