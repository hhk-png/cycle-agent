# 附录 G. Helm Values 与安装矩阵

本附录把 KubeRay 的安装参数集中到一页，方便在 dev/kind、生产 HA、多租户和离线环境之间切换。具体 values 键名以你使用的 chart 版本为准；安装前先：

```bash
helm repo add kuberay https://ray-project.github.io/kuberay-helm/
helm repo update
helm search repo kuberay --versions | head
helm show values kuberay/kuberay-operator --version 1.7.1 > operator-defaults.yaml
```

## G.1 Chart 清单

| Chart | 作用 | 典型 namespace |
|---|---|---|
| `kuberay-operator` | 核心 operator + CRD | `kuberay-system` |
| `kuberay-apiserver` | REST API v1/v2 | `kuberay-system` |
| `ray-cluster` | 示例 RayCluster chart | 业务 namespace |

> 官方 Helm 仓库通常只有 operator、apiserver 和 ray-cluster；Dashboard/History Server 由独立 manifests 或 operator chart 的选项部署。不要假设存在 `kuberay-dashboard`/`kuberay-history-server` chart；以 `helm search repo kuberay` 为准。

## G.2 Operator 常用 values

```yaml
# 镜像
image:
  repository: quay.io/kuberay/operator
  tag: v1.7.1
  pullPolicy: IfNotPresent
imagePullSecrets:
  - name: registry-credential

# 副本与选主
replicas: 2
leaderElectionEnabled: true

# 监听范围（list；空 = 所有 namespace）
watchNamespace: []
# watchNamespace: [team-a]   # 只监听一个/多个 namespace
# singleNamespaceInstall: true
# crNamespacedRbacEnable: true

# 功能门控
featureGates:
  - name: RayCronJob
    enabled: true

# 指标（端口由 operator 参数/默认值决定，chart 未必有 metrics.port 键）
metrics:
  enabled: true
  serviceMonitor:
    enabled: false
    interval: 30s

# 资源
resources:
  requests:
    cpu: 500m
    memory: 512Mi
  limits:
    cpu: "2"
    memory: 2Gi

# 调度
nodeSelector: {}
tolerations: []
affinity: {}
priorityClassName: ""

# 安全
serviceAccount:
  create: true
  name: ""
rbacEnable: true
crNamespacedRbacEnable: true
podSecurityContext: {}
securityContext: {}

# 日志/探针（键名以 chart 为准，常见为 logging.*）
logging:
  stdoutEncoder: json
readinessProbe: {}
livenessProbe: {}
```

### G.2.1 关键键说明

| 键 | 作用 | 建议 |
|---|---|---|
| `watchNamespace` | 限制监听范围 | 多租户时按需设置 |
| `replicas` | operator 副本 | 生产 ≥2 |
| `leaderElectionEnabled` | 选主（默认 true） | 生产开启 |
| `featureGates` | alpha/beta 功能 | 一次只开一个 |
| `metrics.enabled` | Prometheus | 生产开启 |
| `serviceMonitor.enabled` | Prometheus Operator | 有 operator 时开启 |
| `resources` | operator 资源 | 按规模调 |
| `rbacEnable` / `crNamespacedRbacEnable` | 创建 RBAC | 生产按最小权限；多 namespace 时注意 CR 级 RBAC |
| `imagePullSecrets` | 私有仓库 | 离线/私有仓库必填 |

### G.2.2 watchNamespace 的坑

- 设置后，其他 namespace 的 RayCluster 不会被调和；
- autoscaler 的 RBAC 仍可能需要跨 namespace；
- 多租户通常“一租户一 operator”或“ClusterRole + namespace 过滤”；
- 改 `watchNamespace` 后要重启 operator。

### G.2.3 Operator 启动参数

区分**命令行参数**和**环境变量**；两者都常通过 Helm values 的 `args`/`extraArgs`/`env` 或 Deployment 注入（以 `kuberay-operator --help` 和镜像为准）：

| 命令行参数 | 作用 |
|---|---|
| `--enable-leader-election` | 开启 leader election |
| `--watch-namespace` | 只监听指定 namespace |
| `--feature-gates` | 开启/关闭 feature gate |
| `--batch-scheduler` | `volcano`/`yunikorn`/`scheduler-plugins`/`was` |
| `--metrics-addr` | 指标监听地址 |
| `--health-probe-bind-address` | 健康探针地址（默认 `:8082`） |
| `--zap-log-level` | 日志级别 |
| `ENABLE_WEBHOOKS`（env） | 可选 validating webhook（v1.7）；不是 CLI flag，也不是 Helm `webhook.enabled` |

| 环境变量 | 作用 |
|---|---|
| `ENABLE_GCS_FT_REDIS_CLEANUP` | Redis cleanup |
| `ENABLE_INIT_CONTAINER_INJECTION` | 注入 `wait-gcs-ready` |
| `ENABLE_PROBES_INJECTION` | 注入探针 |
| `ENABLE_RANDOM_POD_DELETE` | 允许随机删 Pod（谨慎） |

不要同时用 Helm values 和手动 patch Deployment 改同一个参数；以 GitOps 为唯一来源。

## G.3 APIServer/Dashboard/History Server

### G.3.1 APIServer

```yaml
image:
  repository: quay.io/kuberay/apiserver
  tag: v1.7.1
replicas: 1
service:
  type: ClusterIP
  port: 8888
resources:
  requests:
    cpu: 100m
    memory: 128Mi
```

注意：

- APIServer 自己的 ServiceAccount 需要 CRD/Pod/Service 权限；
- 前端认证由 Gateway/代理负责；
- 不要让 APIServer 用 cluster-admin；
- v2 仍是 alpha，API 可能变化。

### G.3.2 Dashboard

```yaml
image:
  repository: quay.io/kuberay/dashboard
  tag: v1.7.1
service:
  type: ClusterIP
  port: 8080
```

Dashboard 是只读视图，不替代 Ray Dashboard（8265）。

### G.3.3 History Server

```yaml
image:
  repository: quay.io/kuberay/historyserver
  tag: v1.7.1
storage:
  type: s3
  bucket: my-ray-history
  region: us-west-2
service:
  type: ClusterIP
  port: 8080
```

需要与 collector 使用同一个 bucket/prefix；权限/生命周期要单独配置。

## G.4 Feature Gates

| Gate | 默认 | 影响 |
|---|---|---|
| `RayClusterStatusConditions` | true | conditions |
| `RayJobDeletionPolicy` | true | deletion rules |
| `RayMultiHostIndexing` | true | 多主机 |
| `RayServiceIncrementalUpgrade` | 按 tag 确认 | 增量升级；v1.7 分支有 promote/revert |
| `RayCronJob` | false | RayCronJob |
| `SidecarSubmitterRestart` | false | sidecar 重启 |
| `RayClusterNetworkPolicy` | false | NetworkPolicy |
| `GCSFaultToleranceEmbeddedStorage` | false | RocksDB |
| `RayClusterMTLS` | false | mTLS |
| `RayClusterHistoryServer` | false | collector |
| `KubernetesWAS` | false | WAS |

安装时开启：

```yaml
featureGates:
  - name: RayCronJob
    enabled: true
  - name: RayClusterNetworkPolicy
    enabled: true
```

或：

```bash
helm upgrade --install kuberay-operator kuberay/kuberay-operator \
  --version 1.7.1 -n kuberay-system \
  --set 'featureGates[0].name=RayCronJob' \
  --set 'featureGates[0].enabled=true'
```

生产建议一次只开一个，观察事件/指标/CRD 行为。

## G.5 安装矩阵

### G.5.1 本地/kind

```yaml
replicas: 1
leaderElectionEnabled: false
metrics:
  enabled: true
resources:
  requests: {cpu: 100m, memory: 128Mi}
```

```bash
helm install kuberay-operator kuberay/kuberay-operator \
  --version 1.7.1 -n kuberay-system --create-namespace \
  -f values-kind.yaml
```

### G.5.2 生产 HA

```yaml
replicas: 2
leaderElectionEnabled: true
metrics:
  enabled: true
  serviceMonitor:
    enabled: true
resources:
  requests: {cpu: "1", memory: 1Gi}
  limits: {cpu: "4", memory: 4Gi}
affinity:
  podAntiAffinity:
    preferredDuringSchedulingIgnoredDuringExecution:
      - weight: 100
        podAffinityTerm:
          topologyKey: kubernetes.io/hostname
          labelSelector:
            matchLabels:
              app.kubernetes.io/name: kuberay-operator
```

### G.5.3 多租户

方案 A：每租户一个 operator（强隔离，成本高）

```yaml
watchNamespace: [team-a]
singleNamespaceInstall: true
crNamespacedRbacEnable: true
```

方案 B：共享 operator + ClusterRole + 准入策略（常见）

- operator 管理所有 namespace；
- 租户只能操作自己的 CR；
- ResourceQuota/LimitRange/NetworkPolicy；
- Kueue 队列；
- 审计。

### G.5.4 离线/air-gapped

```yaml
image:
  repository: registry.local/kuberay/operator
  tag: v1.7.1
imagePullSecrets:
  - name: registry-credential
```

准备：

- operator 镜像；
- Ray 镜像（CPU/GPU/ML）；
- collector/APIServer/Dashboard；
- Helm chart `.tgz`；
- CRD YAML；
- 依赖镜像（Redis/批调度器）。

## G.6 CRD 管理

Helm 的 `crds/` 目录：

- 安装时创建；
- `helm upgrade` **不会**升级；
- 必须单独 apply；
- 不要删除 storage version。

```bash
# 备份
kubectl get crd rayclusters.ray.io -o yaml > rayclusters.crd.backup.yaml

# 升级
kubectl apply --server-side -f \
  https://raw.githubusercontent.com/ray-project/kuberay/v1.7.1/ray-operator/config/crd/bases/ray.io_rayclusters.yaml
```

四个 CRD：

- `rayclusters.ray.io`
- `rayjobs.ray.io`
- `rayservices.ray.io`
- `raycronjobs.ray.io`

## G.7 升级与回滚

```bash
helm history kuberay-operator -n kuberay-system
helm upgrade kuberay-operator kuberay/kuberay-operator \
  --version 1.7.1 -n kuberay-system -f values-prod.yaml
kubectl -n kuberay-system rollout status deploy/kuberay-operator

# 回滚
helm rollback kuberay-operator <revision> -n kuberay-system
```

注意：

- 先升级 CRD 再升级 operator（通常）；
- 旧 operator 要能容忍新 schema；
- 新字段旧 operator 可能忽略；
- 回滚前确认 CRD 兼容；
- 灰度 namespace/租户。

## G.8 调试 Helm

```bash
# 渲染
helm template kuberay-operator kuberay/kuberay-operator \
  --version 1.7.1 -n kuberay-system -f values-prod.yaml > rendered.yaml

# 对比
helm diff upgrade kuberay-operator kuberay/kuberay-operator \
  --version 1.7.1 -n kuberay-system -f values-prod.yaml

# 查看 release
helm get values kuberay-operator -n kuberay-system
helm get manifest kuberay-operator -n kuberay-system | head -50

# 检查渲染结果
kubectl apply --dry-run=server -f rendered.yaml
```

## G.9 Values 优先级

```text
chart defaults
  < values.yaml (-f)
  < --set
  < --set-string / --set-file
```

不要把 Secret 放进 values；用 External Secrets/Sealed Secrets。

## G.10 安装检查清单

- [ ] Kubernetes 版本、CNI/CSI/GPU 正常
- [ ] CRD 已安装/版本正确
- [ ] operator 镜像可拉取
- [ ] RBAC 最小化
- [ ] leader election/副本
- [ ] feature gates 按需
- [ ] 指标/日志/告警
- [ ] watchNamespace 符合租户模型
- [ ] 资源/调度约束
- [ ] 备份/回滚
- [ ] 最小 RayCluster/RayJob smoke test

← [返回目录](README.md)
