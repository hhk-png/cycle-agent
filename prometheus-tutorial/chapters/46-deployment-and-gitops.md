# 第 46 章 部署拓扑、配置管理与 GitOps

## 46.1 先选拓扑，再选工具

Prometheus 的部署问题不是"用什么 YAML 写"，而是"把抓取、存储、查询、告警放在哪里"。同一个 `prometheus` 二进制，可以有完全不同的拓扑：

- **单机全功能**：一个进程抓取、存储、查询、告警；适合小团队/单集群/边缘。
- **HA 对**：两个相同配置的 Prometheus 同时抓取，靠外部去重/告警去重；避免单点。
- **功能分片**：按集群/业务/租户拆多个 Prometheus，各自本地存储。
- **Agent + 中心**：边缘 agent 只抓取 + remote write，中心负责长期存储/查询。
- **Sidecar/Receiver + 全局层**：Prometheus 本地短期存储，Thanos sidecar/Mimir remote write 提供长期与全局查询。
- **托管**：云厂商/ Grafana Cloud 负责存储与扩展，用户只配置抓取/规则。

选型顺序：

1. 数据要不要出集群/出区域？（合规）
2. 需要多长的历史？本地 15 天够不够？
3. 需要全局查询吗？跨集群/跨租户？
4. 团队能不能运维存储/对象存储/查询层？
5. 预算更偏向人力还是云服务？

不要从"装一个 Operator"开始，也不要默认"HA 就是两个副本"。先画数据流和失败域。

## 46.2 典型拓扑

### 46.2.1 单机

```text
targets → Prometheus (scrape + TSDB + rules + UI/API) → Alertmanager
```

优点：简单、可调试、成本低。缺点：单点、容量有限、升级有停机/风险。适用：开发/测试、小规模生产、边缘节点。

单机也要做：

- 独立数据盘；
- 快照备份；
- systemd/容器重启策略；
- 磁盘/内存/序列数告警；
- 配置版本化。

### 46.2.2 HA 对

```text
                 ┌→ Prometheus A ─┐
targets ─────────┤                ├→ 查询层/去重 → Grafana
                 └→ Prometheus B ─┘
                         ↓
                   Alertmanager 集群
```

两个 Prometheus 配置相同、各自抓取、各自存储。查询时：

- 简单做法：Grafana 配两个数据源，人工切换；
- 生产做法：Thanos Querier/Mimir/VictoriaMetrics 去重，或负载均衡 + 一致哈希；
- 告警：Alertmanager 集群用 gossip 去重，两个 Prometheus 都发送相同告警不会重复通知。

HA 对不解决容量问题：两个副本都存全量数据，成本翻倍。它解决的是单实例故障。

### 46.2.3 功能分片

按集群/业务/区域拆多个 Prometheus：

```text
cluster-a targets → Prometheus A ─┐
cluster-b targets → Prometheus B ─┼→ 全局查询层
cluster-c targets → Prometheus C ─┘
```

优点：故障域隔离、单机容量可控、团队自治。缺点：全局查询/告警需要聚合层；跨分片 recording rules 复杂；标签/命名要统一。

分片键选择：

- 按 Kubernetes 集群；
- 按区域/可用区；
- 按业务线/租户；
- 按指标类型（基础设施 vs 业务）。

不要按"抓取目标数量平均"分片而不考虑故障域和团队边界。

### 46.2.4 Agent + 中心

```text
edge targets → Prometheus agent (no local query) → remote write → 中心存储
```

Agent 模式只做抓取、relabel、WAL、remote write，不做本地查询/告警。适合：

- 大量边缘集群；
- 中心化长期存储；
- 降低边缘磁盘/运维成本。

代价：

- 边缘失去本地查询能力；
- 网络中断时依赖 WAL 缓冲，容量有限；
- 中心接收端成为关键路径；
- 告警规则通常移到中心，延迟增加。

### 46.2.5 Sidecar/Receiver + 全局层

```text
Prometheus (local TSDB)
  ├→ Thanos sidecar → 对象存储 → Thanos Querier/Store
  └→ remote write → Mimir/VictoriaMetrics
```

本地 Prometheus 保留短期数据用于快速查询/告警；长期数据上传对象存储；全局层负责跨集群查询。这是中大型组织最常见的形态。

## 46.3 配置仓库的布局

配置即代码，第一步是让配置有清晰的目录和所有权：

```text
observability/
  prometheus/
    base/
      prometheus.yml
      rules/
        recording.yml
        alerts.yml
      targets/
        kubernetes.yml
    environments/
      dev/
        prometheus.yml
      staging/
        prometheus.yml
      prod/
        prometheus.yml
    tests/
      rules_test.yml
  alertmanager/
    alertmanager.yml
    templates/
  dashboards/
    api.json
  README.md
```

原则：

- **base + overlay**：公共配置放 base，环境差异放 overlay；不要复制三份几乎相同的文件。
- **规则与抓取分离**：规则文件按域拆分，避免一个巨大 `rules.yml`。
- **测试与规则同仓**：规则改动必须带单元测试（第 28 章、附录 Y）。
- **Secret 不入库**：密码/token 用 `*_file` 或 External Secrets。
- **生成物可复现**：Jsonnet/Kustomize/Helm 的渲染结果要能 diff。
- **变更可审计**：PR、review、CI、部署记录。

## 46.4 配置校验流水线

在合并前拦截错误，比在生产重载失败便宜得多。

### 46.4.1 官方工具

```bash
promtool check config prometheus.yml
promtool check rules rules/*.yml
promtool test rules tests/rules_test.yml
amtool check-config alertmanager.yml
```

`check config` 会解析配置、检查规则文件、验证 relabel/SD 字段；`check rules` 只校验规则语法/表达式；`test rules` 跑规则单元测试。

### 46.4.2 Mini 的对应工具

本教程的 Mini 提供：

```bash
python -m mini_prometheus.promtool check rules rules/demo.yml
python -m mini_prometheus.promtool test rules examples/rules/demo_test.yml
```

它复用真实的 `RuleManager`/PromQL/TSDB，适合在 CI 中快速验证规则语义。与官方 promtool 的差异见第 28 章。

### 46.4.3 CI 流水线

```text
pull request
  → lint YAML / render manifests
  → promtool check config/rules
  → promtool test rules
  → schema/policy checks (可选)
  → review
  → merge
  → GitOps 同步
  → 部署后 smoke test (/–/ready, up, rules health)
```

CI 要固定工具版本（promtool 与 Prometheus 版本一致），否则会出现"本地通过、生产失败"。

## 46.5 重载与配置生命周期

Prometheus 支持两种重载方式：

```bash
# 方式一：信号
kill -HUP <pid>

# 方式二：HTTP（需要 --web.enable-lifecycle）
curl -X POST http://localhost:9090/-/reload
```

语义：

- 重载是**全量**的：抓取配置、规则、Alertmanager 配置都会重新加载；
- 解析失败时，旧配置继续生效；不会"半新半旧"；
- 重载成功不保证抓取立即可用：新目标要经过 SD/relabel；
- 规则重载可能导致规则状态重新计算（pending/firing 是否保留取决于版本与规则身份）；
- 频繁重载会带来抖动，批量变更应合并。

验证：

```bash
curl -s http://localhost:9090/api/v1/status/runtimeinfo | python -m json.tool
```

看 `reloadConfigSuccess`、`lastConfigTime`、`corruptionCount`。如果重载失败，日志里会有明确错误；不要只看 HTTP 200。

生产建议：

- 用配置管理工具生成配置，不手工编辑生产文件；
- 重载前先 `promtool check config`；
- 重载后检查 `/targets`、`/rules`、`up`；
- 为重载失败加告警（日志/探针）；
- 大版本升级不要依赖热重载，按升级流程走。

## 46.6 Secret 管理

Prometheus 配置里常见的敏感信息：

- `basic_auth.password`；
- `authorization.credentials`；
- `tls_config` 的证书/私钥；
- remote write 的认证；
- Alertmanager 的 SMTP/Slack/Webhook 凭证。

原则：

- 用 `password_file`/`credentials_file` 指向挂载的 secret，不要写明文；
- 文件权限 0400，属主为 Prometheus 用户；
- Kubernetes 用 Secret + volume mount，或用 External Secrets/Vault；
- 不要把 secret 放进 Git、镜像、ConfigMap、日志；
- 轮换要有流程：更新 secret → 重载 → 验证 → 吊销旧值；
- 审计 secret 的读取权限。

示例：

```yaml
scrape_configs:
  - job_name: secure-api
    static_configs:
      - targets: ["api:8443"]
    scheme: https
    basic_auth:
      username: prometheus
      password_file: /etc/prometheus/secrets/api-password
    tls_config:
      ca_file: /etc/prometheus/secrets/ca.pem
      server_name: api.internal
```

## 46.7 GitOps 工作流

GitOps 的核心不是"用 Argo CD"，而是"集群状态由 Git 中的期望状态驱动"。

推荐流程：

1. **提案**：新增指标/抓取/规则，写清目的、基数预算、owner。
2. **开发**：在分支修改配置/规则，补单元测试。
3. **CI**：渲染、校验、测试、策略检查。
4. **Review**：平台团队 + 服务 owner；重点看基数、标签、告警可执行性。
5. **合并**：主分支是唯一真相。
6. **同步**：Argo CD/Flux 把变更应用到集群；失败可见、可回滚。
7. **验证**：`/-/ready`、`up`、规则健康、关键面板。
8. **回滚**：revert commit + 同步；必要时恢复快照。

GitOps 的边界：

- 数据库/TSDB 数据不在 Git 里；配置回滚不等于数据回滚；
- Secret 要用 sealed secret/external secret，不要明文；
- 生成式配置（Jsonnet/Helm）要把渲染结果纳入 diff；
- 多集群要区分"全局配置"与"集群覆盖"。

## 46.8 Kubernetes 部署：Operator 还是 Helm

### 46.8.1 Prometheus Operator

适合：

- 多团队、多 ServiceMonitor/PodMonitor；
- 需要 CRD 管理抓取/规则/告警；
- 需要自动生成配置、分片、RBAC；
- 已经在用 kube-prometheus-stack。

优点：声明式、集成度高、社区成熟。缺点：抽象层多、排障要理解 Operator 生成的配置、CRD 升级有耦合。

### 46.8.2 Helm/原生清单

适合：

- 配置简单、团队希望完全控制；
- 非 Kubernetes 环境或混合环境；
- 不想引入 Operator CRD。

优点：透明、可移植。缺点：ServiceMonitor 等生态集成要自己做；多团队自助能力弱。

### 46.8.3 资源与存储

无论哪种方式，都要明确：

- CPU/内存 requests/limits；
- 数据盘大小与 IOPS；
- retention 与本地存储；
- 是否需要 remote write；
- 副本数/反亲和；
- PodDisruptionBudget；
- securityContext（非 root、只读根文件系统）；
- Service/Ingress/TLS；
- 备份（PVC snapshot 或 TSDB snapshot）。

示例（原生 Deployment 片段）：

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: prometheus
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
      containers:
        - name: prometheus
          image: prom/prometheus:v3.13.5
          args:
            - --config.file=/etc/prometheus/prometheus.yml
            - --storage.tsdb.path=/prometheus
            - --storage.tsdb.retention.time=15d
            - --web.enable-lifecycle
          ports:
            - containerPort: 9090
          volumeMounts:
            - name: config
              mountPath: /etc/prometheus
            - name: data
              mountPath: /prometheus
      volumes:
        - name: config
          configMap:
            name: prometheus-config
        - name: data
          persistentVolumeClaim:
            claimName: prometheus-data
```

生产不要把配置放在 ConfigMap 里手工维护；用 Operator/Helm/GitOps 生成。上面的片段只用于说明字段。

## 46.9 升级与回滚

Prometheus 升级的主要风险：

- 配置文件字段弃用/行为变化；
- TSDB 格式/索引版本变化（通常向后兼容，但降级不一定）；
- PromQL 语义修正导致面板/告警变化；
- UI/API 端点变化；
- 与 remote write 接收端/Thanos sidecar 的兼容性。

流程：

1. 读 release notes，标记 breaking changes；
2. 在 staging 用相同数据量/查询集验证；
3. `promtool check config`/`test rules`；
4. 备份（TSDB snapshot + 配置）；
5. 一次升级一个小版本，不要跨多个 minor；
6. 观察 `up`、查询延迟、规则健康、TSDB 指标；
7. 保留回滚镜像/配置；
8. 确认降级是否支持；不支持则准备重建/恢复。

回滚：

- 配置回滚：Git revert + 重载；
- 二进制回滚：切回旧镜像；注意 TSDB 是否已被新版本迁移；
- 数据回滚：从快照恢复；会丢失快照之后的样本；
- 规则回滚：恢复规则文件 + 重载；告警状态会重算。

## 46.10 多环境与多集群

标签约定：

```yaml
global:
  external_labels:
    cluster: prod-cn-north
    region: cn-north
    environment: prod
    replica: a
alert_relabel_configs:
  - regex: replica
    action: labeldrop    # HA 对告警去重
```

用途：

- 全局查询区分集群；
- 告警路由；
- 去重（Thanos/Mimir 用 replica 标签）；
- 成本归因。

注意：

- `external_labels` 会附加到所有向外发送的数据（remote write/federation），但**不会**改变本地序列身份；
- 两个 HA 副本用相同 `cluster`、不同 `replica`；
- 不要让 `environment` 同时出现在 target labels 和 external labels 里，避免冲突；
- 多租户环境用租户标签/接收端隔离，不能只靠查询过滤。

环境策略：

- dev：单机、短保留、宽松限制；
- staging：接近生产拓扑、相同规则、较小数据量；
- prod：HA/分片、长期存储、严格变更流程；
- 所有环境用同一套规则测试。

## 46.11 检查表

- [ ] 拓扑是否按合规/历史/全局查询/人力选择，而不是默认单机或 Operator？
- [ ] 是否有明确的失败域和容量上限？
- [ ] 配置是否在 Git 中，能 review、diff、回滚？
- [ ] CI 是否运行 `promtool check config/rules` 和规则单元测试？
- [ ] Secret 是否用文件/外部 secret，未入库？
- [ ] 重载失败是否可见、是否会保留旧配置？
- [ ] 升级是否备份、逐 minor、验证降级路径？
- [ ] 多集群 external labels 是否统一？
- [ ] 是否演练过恢复/回滚？

## 本章小结

- 拓扑选择由合规、历史、全局查询和人力决定；HA 对解决单点，不解决容量。
- Agent/分片/Sidecar 是规模化的不同答案，不是互相替代。
- 配置仓库要 base+overlay、规则与抓取分离、测试同仓、Secret 不入库。
- CI 用 promtool（或 Mini promtool）校验配置和规则，再进入 GitOps。
- 重载是全量的，失败保留旧配置；升级要备份、逐 minor、验证回滚。
- Kubernetes 用 Operator 还是 Helm 取决于团队自治与抽象偏好，不是非此即彼。

## 延伸阅读

- Prometheus 官方文档 "Configuration"、"Storage"、"Migration"。
- Prometheus Operator 文档与 kube-prometheus-stack。
- Argo CD/Flux 的 GitOps 模式。
- 本教程第 23 章（运维）、第 24 章（Kubernetes）、第 28 章（测试）与附录 P/X。
