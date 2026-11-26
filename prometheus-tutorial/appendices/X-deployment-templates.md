# 附录 X 部署清单与配置模板

本附录是第 46 章的配套模板。模板用于教学/起步，生产要按环境调整镜像版本、资源、存储、认证和网络策略。

## X.1 Docker Compose：单机演示栈

```yaml
services:
  prometheus:
    image: prom/prometheus:v3.13.5
    command:
      - --config.file=/etc/prometheus/prometheus.yml
      - --storage.tsdb.path=/prometheus
      - --storage.tsdb.retention.time=15d
      - --web.enable-lifecycle
    ports:
      - "9090:9090"
    volumes:
      - ./prometheus.yml:/etc/prometheus/prometheus.yml:ro
      - ./rules:/etc/prometheus/rules:ro
      - prometheus-data:/prometheus
    restart: unless-stopped

  alertmanager:
    image: prom/alertmanager:v0.28.0
    command:
      - --config.file=/etc/alertmanager/alertmanager.yml
    ports:
      - "9093:9093"
    volumes:
      - ./alertmanager.yml:/etc/alertmanager/alertmanager.yml:ro
    restart: unless-stopped

  node-exporter:
    image: prom/node-exporter:v1.8.2
    ports:
      - "9100:9100"
    restart: unless-stopped

  grafana:
    image: grafana/grafana:11.3.0
    ports:
      - "3000:3000"
    environment:
      GF_SECURITY_ADMIN_PASSWORD: change-me
    volumes:
      - grafana-data:/var/lib/grafana
    restart: unless-stopped

volumes:
  prometheus-data:
  grafana-data:
```

启动：

```bash
docker compose up -d
curl -s http://localhost:9090/-/ready
curl -s http://localhost:9090/api/v1/targets
```

生产不要把 Grafana 管理员密码写在 compose 文件里；用 secret/环境变量管理。

## X.2 Prometheus 配置基线

```yaml
global:
  scrape_interval: 15s
  scrape_timeout: 10s
  evaluation_interval: 15s
  external_labels:
    cluster: prod-cn-north
    environment: prod
    replica: a

alert_relabel_configs:
  - regex: replica
    action: labeldrop    # HA 对告警去重

rule_files:
  - /etc/prometheus/rules/*.yml

alerting:
  alertmanagers:
    - static_configs:
        - targets:
            - alertmanager:9093

scrape_configs:
  - job_name: prometheus
    static_configs:
      - targets: ["localhost:9090"]

  - job_name: node
    static_configs:
      - targets: ["node-exporter:9100"]

  - job_name: api
    metrics_path: /metrics
    static_configs:
      - targets: ["api:8080"]
        labels:
          team: api
          service: api
```

要点：

- `scrape_timeout` 必须小于 `scrape_interval`；
- `external_labels` 用于跨集群区分，不要和 target labels 冲突；
- 规则文件用 glob，但 CI 要检查每个文件；
- 生产用服务发现/Operator 替代静态目标。

## X.3 Alertmanager 配置基线

```yaml
global:
  resolve_timeout: 5m

route:
  receiver: default
  group_by: ["alertname", "cluster", "service"]
  group_wait: 30s
  group_interval: 5m
  repeat_interval: 4h
  routes:
    - matchers:
        - severity="critical"
      receiver: critical
      group_wait: 10s
      repeat_interval: 1h

receivers:
  - name: default
    webhook_configs:
      - url: http://webhook:8080/alerts
  - name: critical
    webhook_configs:
      - url: http://webhook:8080/critical

inhibit_rules:
  - source_matchers:
      - severity="critical"
    target_matchers:
      - severity="warning"
    equal: ["alertname", "cluster", "service"]
```

要点：

- `group_by` 决定通知聚合；不要把 `instance` 放进去导致每个实例一条通知；
- `repeat_interval` 太短会骚扰，太长会漏提醒；
- 抑制规则要 `equal` 到业务维度，避免误抑制；
- 生产用邮件/Slack/PagerDuty 等 receiver，并测试模板。

## X.4 systemd 单元

```ini
[Unit]
Description=Prometheus
Wants=network-online.target
After=network-online.target

[Service]
User=prometheus
Group=prometheus
Type=simple
ExecStart=/usr/local/bin/prometheus \
  --config.file=/etc/prometheus/prometheus.yml \
  --storage.tsdb.path=/var/lib/prometheus \
  --storage.tsdb.retention.time=15d \
  --web.listen-address=127.0.0.1:9090 \
  --web.enable-lifecycle
ExecReload=/bin/kill -HUP $MAINPID
Restart=on-failure
RestartSec=5s
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
```

要点：

- 独立用户/组，数据目录权限最小化；
- `LimitNOFILE` 要足够大（大量目标/连接）；
- 监听 127.0.0.1，前面放反向代理；
- `ExecReload` 用 HUP，配合 `--web.enable-lifecycle` 也可用 API。

## X.5 Kubernetes 原生清单

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: prometheus-config
  namespace: monitoring
data:
  prometheus.yml: |
    global:
      scrape_interval: 30s
      evaluation_interval: 30s
    rule_files:
      - /etc/prometheus/rules/*.yml
    scrape_configs:
      - job_name: prometheus
        static_configs:
          - targets: ["localhost:9090"]
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: prometheus-data
  namespace: monitoring
spec:
  accessModes: ["ReadWriteOnce"]
  resources:
    requests:
      storage: 100Gi
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: prometheus
  namespace: monitoring
spec:
  replicas: 1
  selector:
    matchLabels:
      app: prometheus
  template:
    metadata:
      labels:
        app: prometheus
    spec:
      securityContext:
        runAsNonRoot: true
        runAsUser: 65534
        fsGroup: 65534
      containers:
        - name: prometheus
          image: prom/prometheus:v3.13.5
          args:
            - --config.file=/etc/prometheus/prometheus.yml
            - --storage.tsdb.path=/prometheus
            - --storage.tsdb.retention.time=15d
            - --web.enable-lifecycle
          ports:
            - name: web
              containerPort: 9090
          resources:
            requests:
              cpu: "1"
              memory: 4Gi
            limits:
              memory: 8Gi
          volumeMounts:
            - name: config
              mountPath: /etc/prometheus
            - name: data
              mountPath: /prometheus
          readinessProbe:
            httpGet:
              path: /-/ready
              port: web
          livenessProbe:
            httpGet:
              path: /-/healthy
              port: web
      volumes:
        - name: config
          configMap:
            name: prometheus-config
        - name: data
          persistentVolumeClaim:
            claimName: prometheus-data
---
apiVersion: v1
kind: Service
metadata:
  name: prometheus
  namespace: monitoring
spec:
  selector:
    app: prometheus
  ports:
    - name: web
      port: 9090
      targetPort: web
```

要点：

- 单副本没有 HA；生产要两个副本 + 去重/长期存储；
- PVC 的 IOPS 要满足 WAL/compaction；
- resources 只是示例，必须按容量测试调整；
- ConfigMap 不适合放 secret；用 Secret/External Secrets；
- 安全上下文尽量非 root、只读根文件系统（需额外挂载临时目录）。

## X.6 ServiceMonitor 与 PrometheusRule

```yaml
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
metadata:
  name: api
  namespace: monitoring
  labels:
    release: kube-prometheus-stack
spec:
  selector:
    matchLabels:
      app: api
  endpoints:
    - port: metrics
      interval: 30s
      path: /metrics
      relabelings:
        - sourceLabels: [__meta_kubernetes_pod_node_name]
          targetLabel: node
          action: replace
---
apiVersion: monitoring.coreos.com/v1
kind: PrometheusRule
metadata:
  name: api-alerts
  namespace: monitoring
  labels:
    release: kube-prometheus-stack
spec:
  groups:
    - name: api
      interval: 30s
      rules:
        - alert: ApiHighErrorRatio
          expr: |
            sum by (service) (rate(http_requests_total{code=~"5.."}[5m]))
            /
            sum by (service) (rate(http_requests_total[5m])) > 0.05
          for: 10m
          labels:
            severity: warning
          annotations:
            summary: "{{ $labels.service }} 5xx ratio > 5%"
```

要点：

- ServiceMonitor 的 `selector` 要匹配 Service 的 labels；
- `port` 必须是 Service 端口名，不是容器端口号；
- `PrometheusRule` 的 labels 要被 Prometheus 的 `ruleSelector` 匹配；
- relabel 里不要放入高基数 pod IP；`node` 可以。
- 未安装 Operator CRD 时，这两个 YAML 无法 apply；用 Helm/Operator 时才有意义。

## X.7 反向代理 + TLS

Nginx 片段：

```nginx
server {
    listen 443 ssl;
    server_name prometheus.example.com;

    ssl_certificate     /etc/nginx/tls/server.crt;
    ssl_certificate_key /etc/nginx/tls/server.key;

    location / {
        auth_basic           "Prometheus";
        auth_basic_user_file /etc/nginx/htpasswd;

        proxy_pass         http://127.0.0.1:9090;
        proxy_set_header   Host $host;
        proxy_set_header   X-Real-IP $remote_addr;
        proxy_set_header   X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;

        # 大查询/长连接
        proxy_read_timeout 300s;
    }

    location /api/v1/admin/ {
        deny all;
    }
}
```

要点：

- 管理端点单独限制/拒绝；
- 认证放在代理层，Prometheus 自身只监听 127.0.0.1；
- 如果 Grafana 走代理，注意 CORS/WebSocket；
- 不要让公网直接访问 `/api/v1/write`/`/read`/`/federate`。

## X.8 备份 CronJob

```yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: prometheus-snapshot
  namespace: monitoring
spec:
  schedule: "0 3 * * *"
  jobTemplate:
    spec:
      template:
        spec:
          restartPolicy: OnFailure
          containers:
            - name: snapshot
              image: curlimages/curl:8.10.1
              command:
                - sh
                - -c
                - |
                  set -eu
                  curl -fsS -X POST http://prometheus:9090/api/v1/admin/tsdb/snapshot
              # 生产还应上传到对象存储、校验、清理旧快照
```

生产备份要：

- 快照 + 对象存储 + 生命周期；
- 定期恢复演练；
- 记录 RPO/RTO；
- 备份失败告警；
- 加密/访问控制。

## X.9 容量与资源清单

| 项 | 起步建议 | 说明 |
|---|---|---|
| CPU | 2～4 核/10 万活跃序列 | 取决于抓取/查询/规则 |
| 内存 | 8～16 GiB/10 万序列 | head + 查询 + 规则；以压测为准 |
| 数据盘 | SSD，IOPS ≥ 3000 | WAL/compaction 对 IOPS 敏感 |
| 保留 | 15 天本地 | 长期用 remote write/对象存储 |
| 副本 | 2（HA） | 查询去重/告警去重 |
| 文件描述符 | ≥ 65536 | 大量目标/连接 |
| 抓取超时 | ≤ 抓取间隔的 2/3 | 避免堆积 |
| 查询超时 | 30～120s | 按 SLO 调整 |
| 样本限制 | 按内存压测 | `query.max-samples` |

这些数字只是起点，必须用第 48 章的基准方法验证。

## X.10 上线检查表

- [ ] 镜像/版本固定，升级流程明确？
- [ ] 配置在 Git，CI 校验，Secret 未入库？
- [ ] 数据盘独立、容量/IOPS 足够、备份/恢复演练过？
- [ ] 监听地址/认证/TLS/网络策略正确？
- [ ] 管理/remote/联邦端点未暴露公网？
- [ ] 资源 requests/limits 合理，PDB/反亲和配置？
- [ ] `/-/healthy`、`/-/ready`、`up`、规则健康有监控？
- [ ] retention/remote write/长期存储策略明确？
- [ ] 告警路由/抑制/静默测试过？
- [ ] 容量模型/压测报告归档？

## 延伸阅读

- Prometheus 官方 "Configuration"、"Storage"、"Migration"。
- Prometheus Operator/kube-prometheus-stack 文档。
- 本教程第 23 章（运维）、第 24 章（Kubernetes）、第 46 章（部署/GitOps）与附录 P。
