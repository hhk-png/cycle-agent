# 附录 P 字段语义、默认值与 YAML 陷阱

> Kubernetes 的 YAML 看起来“所见即所得”，实际上经过默认值、准入变更、类型转换和控制器补全。很多“配置没生效”不是 Bug，而是字段语义或默认值在起作用。本附录按资源整理最常用的默认值与可变性规则，并列出高频 YAML 陷阱。版本基线 v1.31；不同版本/发行版可能不同，**以 `kubectl explain` 和目标集群的 OpenAPI 为准**。

## P.1 为什么默认值会咬人

一个只有 `image` 的 Pod，实际提交到 API Server 后会被补上：

```yaml
spec:
  restartPolicy: Always
  terminationGracePeriodSeconds: 30
  dnsPolicy: ClusterFirst
  schedulerName: default-scheduler
  enableServiceLinks: true
  serviceAccountName: default
  containers:
    - name: app
      image: nginx:1.27
      imagePullPolicy: IfNotPresent
      terminationMessagePath: /dev/termination-log
      terminationMessagePolicy: File
      resources: {}
```

这些默认值决定了行为：

- `restartPolicy: Always` 让普通 Pod 永远不会 `Succeeded`。
- `dnsPolicy: ClusterFirst` 让 Pod 走 CoreDNS，而不是节点 DNS。
- `imagePullPolicy: IfNotPresent` 配合可变 tag 会复用旧镜像。
- 没有 requests/limits 的 Pod 是 BestEffort，最先被驱逐。

## P.2 数量与单位

Kubernetes 的 `resource.Quantity` 有一套独立语法：

| 写法 | 含义 | 常见错误 |
|---|---|---|
| `500m` | 0.5 个 CPU 核 | 误以为 500 MB |
| `0.5` | 0.5 核，合法但推荐 `500m` | 精度/可读性 |
| `1` | 1 核 | |
| `1Gi` | 1024³ 字节 | 与 `1G` 不同 |
| `1G` | 1000³ 字节 | 内存通常用 `Gi` |
| `1Mi` | 1024² 字节 | |
| `1M` | 1000² 字节 | |
| `1e3` | 1000，科学计数法合法 | 易读性差 |

内存/临时存储建议统一用二进制单位（`Mi`/`Gi`）。CPU 用 millicore（`m`）。`0.1` 与 `100m` 等价，但后者在日志和对比中更清晰。

## P.3 Pod 与容器默认值

| 字段 | 默认值 | 说明 |
|---|---|---|
| `spec.restartPolicy` | `Always` | Job 必须显式设为 `OnFailure`/`Never` |
| `spec.terminationGracePeriodSeconds` | `30` | 超时后 SIGKILL |
| `spec.dnsPolicy` | `ClusterFirst` | hostNetwork 常用 `ClusterFirstWithHostNet` |
| `spec.schedulerName` | `default-scheduler` | 自定义调度器需改 |
| `spec.enableServiceLinks` | `true` | 大量 Service 时会注入很多环境变量 |
| `spec.serviceAccountName` | `default` | 由准入补全 |
| `spec.priority` | `0` | 由 PriorityClass 决定 |
| `spec.hostNetwork/hostPID/hostIPC` | `false` | 开启后隔离性显著下降 |
| `containers[].imagePullPolicy` | `IfNotPresent`；tag 为 `latest` 时 `Always` | 用 digest 可稳定复现 |
| `containers[].terminationMessagePath` | `/dev/termination-log` | 退出信息写入位置 |
| `containers[].terminationMessagePolicy` | `File` | 可选 `FallbackToLogsOnError` |
| `containers[].stdin/tty` | `false` | |
| `containers[].resources` | `{}` | 即 BestEffort 的一部分 |
| `containers[].ports[].protocol` | `TCP` | |
| `volumes[].emptyDir.medium` | `""`（节点磁盘） | 设为 `Memory` 会计入内存 |
| `volumes[].hostPath.type` | `""`（不检查） | 建议显式 `DirectoryOrCreate` 等 |
| ConfigMap/Secret 卷 `defaultMode` | `0644` | 可用 `defaultMode` 调整 |
| projected SA token `expirationSeconds` | `3600` | 由 kubelet 轮换 |

探针默认值：

| 字段 | 默认 |
|---|---|
| `initialDelaySeconds` | `0` |
| `periodSeconds` | `10` |
| `timeoutSeconds` | `1` |
| `successThreshold` | `1`（liveness/startup 必须为 1） |
| `failureThreshold` | `3` |

这意味着一个没有调优的 liveness 探针：启动 0 秒后开始、每 10 秒一次、1 秒超时、连续 3 次失败就重启。对慢启动应用非常危险，应使用 `startupProbe` 或调大 `initialDelaySeconds`/`failureThreshold`。

## P.4 工作负载控制器默认值

### Deployment

| 字段 | 默认值 |
|---|---|
| `spec.replicas` | `1` |
| `spec.strategy.type` | `RollingUpdate` |
| `spec.strategy.rollingUpdate.maxSurge` | `25%` |
| `spec.strategy.rollingUpdate.maxUnavailable` | `25%` |
| `spec.revisionHistoryLimit` | `10` |
| `spec.progressDeadlineSeconds` | `600` |
| `spec.minReadySeconds` | `0` |
| `spec.paused` | `false` |
| `spec.selector` | 必填，且创建后不可变 |

`maxSurge`/`maxUnavailable` 不能同时为 0；百分比按 `replicas` 向上/向下取整，副本很少时行为可能与直觉不同。

### StatefulSet

| 字段 | 默认值 |
|---|---|
| `spec.replicas` | `1` |
| `spec.podManagementPolicy` | `OrderedReady` |
| `spec.updateStrategy.type` | `RollingUpdate` |
| `spec.updateStrategy.rollingUpdate.partition` | `0` |
| `spec.revisionHistoryLimit` | `10` |
| `spec.minReadySeconds` | `0` |
| `spec.persistentVolumeClaimRetentionPolicy` | `Retain`/`Retain` |
| `spec.serviceName` | 必填 |

`partition` 是“只更新序号 ≥ partition 的 Pod”，常用于金丝雀；不是“更新前 N 个”。

### DaemonSet

| 字段 | 默认值 |
|---|---|
| `spec.updateStrategy.type` | `RollingUpdate` |
| `spec.updateStrategy.rollingUpdate.maxUnavailable` | `1` |
| `spec.updateStrategy.rollingUpdate.maxSurge` | `0` |
| `spec.revisionHistoryLimit` | `10` |
| `spec.minReadySeconds` | `0` |

### Job / CronJob

| 字段 | 默认值 |
|---|---|
| Job `spec.completions` | `1` |
| Job `spec.parallelism` | `1` |
| Job `spec.backoffLimit` | `6` |
| Job `spec.completionMode` | `NonIndexed` |
| Job `spec.suspend` | `false` |
| Job `spec.ttlSecondsAfterFinished` | 不设置（永久保留） |
| CronJob `spec.concurrencyPolicy` | `Allow` |
| CronJob `spec.successfulJobsHistoryLimit` | `3` |
| CronJob `spec.failedJobsHistoryLimit` | `1` |
| CronJob `spec.suspend` | `false` |
| CronJob `spec.startingDeadlineSeconds` | 不设置 |
| CronJob `spec.timeZone` | 不设置（使用 controller-manager 本地时区） |

`concurrencyPolicy: Allow` 可能让同一 CronJob 的多个实例重叠运行；有状态/重任务通常改 `Forbid` 或 `Replace`。`startingDeadlineSeconds` 不设置时，错过太多调度会记录 `TooManyMissedTimes` 并跳过。

## P.5 Service、Ingress 与 NetworkPolicy

### Service

| 字段 | 默认值 |
|---|---|
| `spec.type` | `ClusterIP` |
| `spec.sessionAffinity` | `None` |
| `spec.sessionAffinityConfig.clientIP.timeoutSeconds` | `10800`（3 小时，仅 ClientIP） |
| `spec.externalTrafficPolicy` | `Cluster`（NodePort/LoadBalancer） |
| `spec.internalTrafficPolicy` | `Cluster` |
| `spec.ipFamilyPolicy` | `SingleStack` |
| `spec.publishNotReadyAddresses` | `false` |
| `spec.allocateLoadBalancerNodePorts` | `true` |
| `ports[].protocol` | `TCP` |
| `ports[].targetPort` | 等于 `port` |
| `ports[].nodePort` | 自动分配（默认 30000–32767） |

`clusterIP` 创建后不可变；`type` 可以改，但从 `ExternalName` 切换有限制。`externalTrafficPolicy: Local` 保留客户端源 IP，但没有本地 Endpoint 的节点会丢流量。

### Ingress

| 字段 | 默认值/要求 |
|---|---|
| `spec.rules[].http.paths[].pathType` | 必填（`Exact`/`Prefix`/`ImplementationSpecific`） |
| `spec.ingressClassName` | 可由默认 IngressClass 补全 |
| `path` | 通常 `/`，但建议显式 |
| backend `port.name`/`number` | 二选一 |

`pathType` 的匹配语义由控制器实现；`Prefix` 按路径段匹配，不是字符串前缀。不同 Ingress Controller 的注解不可移植。

### NetworkPolicy

| 字段 | 默认/语义 |
|---|---|
| `spec.podSelector` | 必填；`{}` 选中 namespace 内所有 Pod |
| `spec.policyTypes` | 省略时由 ingress/egress 是否存在推断 |
| `ingress[].ports[].protocol` | `TCP` |
| `ports[].endPort` | 不设置时只匹配单个 `port` |

NetworkPolicy 是允许列表；一旦选中 Pod，未被允许的方向默认拒绝。只写 ingress 不会自动放行 egress 的 DNS，需要显式允许 53。

## P.6 存储

| 字段 | 默认值/要求 |
|---|---|
| PVC `spec.accessModes` | 必填 |
| PVC `spec.volumeMode` | `Filesystem` |
| PVC `spec.storageClassName` | 不设置时使用默认 StorageClass（若有） |
| PVC `spec.resources.requests.storage` | 必填 |
| PV `spec.persistentVolumeReclaimPolicy` | 静态创建通常 `Retain`；动态供给由 StorageClass 决定 |
| StorageClass `reclaimPolicy` | `Delete` |
| StorageClass `volumeBindingMode` | `Immediate` |
| StorageClass `allowVolumeExpansion` | `false` |
| CSIDriver `attachRequired` | `true` |
| CSIDriver `podInfoOnMount` | `false` |
| 卷 `defaultMode` | `0644` |

`Immediate` 会在 PVC 创建时立即绑定，可能导致 Pod 被调度到不满足卷拓扑的节点；多可用区块存储通常用 `WaitForFirstConsumer`。`ReadWriteOnce` 是“单节点读写”，不是“单 Pod 读写”；需要单 Pod 语义用 `ReadWriteOncePod`。

## P.7 伸缩、驱逐与优先级

| 字段 | 默认值 |
|---|---|
| HPA `spec.minReplicas` | `1` |
| HPA `spec.maxReplicas` | 必填 |
| HPA 扩容稳定窗口 | `0s` |
| HPA 缩容稳定窗口 | `300s` |
| HPA 默认扩容策略 | 每 15 秒最多 4 个 Pod 或 100% |
| HPA 默认缩容策略 | 每 15 秒最多 100% |
| PDB `spec.minAvailable`/`maxUnavailable` | 二选一必填 |
| PDB `spec.unhealthyPodEvictionPolicy` | `IfHealthyBudget` |
| PriorityClass `spec.value` | 必填，≤ 1e9 |
| PriorityClass `spec.globalDefault` | `false` |
| PriorityClass `spec.preemptionPolicy` | `PreemptLowerPriority` |

HPA 的 10% 容忍度是内部行为：目标利用率 50% 时，实际在 45%–55% 内不动作。缩容默认有 5 分钟稳定窗口，避免抖动。

## P.8 Secret、ConfigMap 与 RBAC

| 字段 | 默认/语义 |
|---|---|
| Secret `type` | `Opaque` |
| Secret `immutable` | `false` |
| Secret `data` | base64 编码；`stringData` 是写入便利字段，不回显 |
| ConfigMap `immutable` | `false` |
| ConfigMap `binaryData` | base64 |
| RBAC `rules[].apiGroups` | 核心组用 `""` |
| RBAC `rules[].verbs` | 必填 |
| RoleBinding `roleRef` | 创建后不可变 |

`stringData` 只在写入时生效，读取时只看到 `data`。Secret 默认只是 base64，不是加密；静态加密与 KMS 见第 11 章。

## P.9 可变性矩阵

“改不了”通常不是权限问题，而是字段不可变。

| 资源 | 可原地修改 | 不可变/需重建 |
|---|---|---|
| Pod | `containers[].image`、`activeDeadlineSeconds`、`terminationGracePeriodSeconds`、新增 tolerations、原地扩缩容支持的 resources | 大部分 `spec` 字段、`nodeName`、`restartPolicy`、卷类型 |
| Deployment | `replicas`、`template`、`strategy`、`minReadySeconds`、`revisionHistoryLimit`、`progressDeadlineSeconds` | `selector` |
| StatefulSet | `replicas`、`template`、`updateStrategy`、`minReadySeconds`、`revisionHistoryLimit`、PVC 保留策略 | `serviceName`、`selector`、`volumeClaimTemplates`（多数情况） |
| DaemonSet | `template`、`updateStrategy`、`minReadySeconds`、`revisionHistoryLimit` | `selector` |
| Job | `parallelism`、`completions`（有限制）、`suspend`、`backoffLimit`、`activeDeadlineSeconds`、`ttlSecondsAfterFinished` | `template` 多数字段、`selector` |
| CronJob | `schedule`、`suspend`、`concurrencyPolicy`、`jobTemplate`、历史保留数、`startingDeadlineSeconds`、`timeZone` | `schedule` 之外的少量字段随版本 |
| Service | `selector`、`ports`、`type`（有限制）、`sessionAffinity`、流量策略 | `clusterIP`/`clusterIPs`（创建后） |
| PVC | `resources.requests.storage`（StorageClass 允许扩容时） | `accessModes`、`volumeMode`、`storageClassName`（绑定后） |
| PV | `persistentVolumeReclaimPolicy`、容量（后端支持时） | `capacity` 缩小、`claimRef` 多数情况 |
| StorageClass | `allowVolumeExpansion` | `parameters`、`reclaimPolicy`、`volumeBindingMode`（多数情况） |

需要改不可变字段时，标准做法是“创建新对象 → 迁移 → 删除旧对象”；StatefulSet 的 `volumeClaimTemplates` 变更通常要手工迁移 PVC。

## P.10 空值、省略与 null

- **省略**：使用默认值。
- **显式 `null`**：在 strategic merge patch 中表示删除字段；在 SSA 中表示放弃该字段所有权。
- **空对象 `{}`**：通常表示“使用默认”，但在 selector 中可能是“选中所有”。
- **空数组 `[]`**：表示显式清空，例如 `tolerations: []`。
- **空字符串 `""`**：是合法值，不等于省略。

`kubectl apply` 的 last-applied-configuration 与 SSA 的 managedFields 对“谁拥有字段”的处理不同。混用 `apply`、`edit`、`patch` 和控制器写入时，冲突往往来自字段所有权，而不是值本身。

## P.11 高频 YAML 陷阱

1. **`500m` 不是 500 MB**：CPU 的 `m` 是千分之一核；内存用 `Mi`/`Gi`。
2. **`1G` ≠ `1Gi`**：前者 10⁹，后者 2³⁰。
3. **YAML 1.1 布尔**：`yes`、`no`、`on`、`off` 可能被解析为布尔；环境变量值加引号。
4. **tag 用 `latest` 会强制 `Always` 拉取**：生产用不可变 tag 或 digest。
5. **只设 limit 不设 request**：该资源的 request 会被设为 limit（无 LimitRange 时），QoS 可能变 Guaranteed。
6. **requests=limits 才是 Guaranteed**：只对设置了两者的所有容器/资源成立。
7. **emptyDir `medium: Memory` 计入内存**：可能触发 OOM/驱逐，且节点重启丢失。
8. **ConfigMap/Secret 环境变量不会热更新**：卷会更新，但 `subPath` 挂载不会。
9. **Deployment selector 不可变**：改标签选择器只能新建 Deployment。
10. **selector 必须匹配 template labels**：不匹配会拒绝创建。
11. **`targetPort` 默认等于 `port`**：容器监听 8080、Service port 80 时必须显式写。
12. **命名端口**：Service `targetPort` 用名字时，容器 `ports[].name` 必须一致。
13. **`hostPort` 是节点级独占**：副本可能因端口冲突无法调度。
14. **`command` 覆盖 ENTRYPOINT，`args` 覆盖 CMD**：不是“追加参数”。
15. **shell 形式与 exec 形式**：`command: ["sh","-c","..."]` 才会展开变量/管道。
16. **Secret `data` 要 base64**：`stringData` 可直接写明文，但只写不读。
17. **`readinessProbe` 失败只摘流量，`livenessProbe` 失败会重启**：不要用 liveness 检查下游依赖。
18. **`startupProbe` 成功后才启用 liveness/readiness**：慢启动应用的正确解法。
19. **`terminationGracePeriodSeconds` 要覆盖 preStop + 应用退出**：否则被 SIGKILL。
20. **`dnsPolicy: None` 必须配 `dnsConfig`**：否则没有 nameserver。
21. **`automountServiceAccountToken: false` 仍可用 projected token 显式挂载**：最小权限常用。
22. **`runAsNonRoot: true` 需要镜像有非 root USER 或显式 `runAsUser`**：否则启动失败。
23. **`fsGroup` 会递归改卷属组**：大卷可能很慢；`fsGroupChangePolicy` 可调。
24. **`readOnlyRootFilesystem: true` 需要为可写路径挂 emptyDir**：否则应用写临时文件失败。
25. **`subPath` 挂载的 ConfigMap/Secret 不更新**：需要重启或换挂载方式。
26. **多文档 YAML 用 `---` 分隔**：`kubectl apply -f` 会逐个处理；顺序影响 CRD/CR 依赖。
27. **`List` kind**：`kind: List` 是聚合容器，不是普通资源；`kubectl get -o yaml` 常见。
28. **`metadata.generateName`**：创建时生成唯一名字，不能用 `apply` 反复创建。
29. **`ownerReferences` 跨 namespace 无效**：垃圾回收不会跨 namespace。
30. **`finalizers` 会阻塞删除**：对象卡在 `Terminating` 时先查 finalizer 与控制器。

## P.12 如何自己查默认值

```bash
# 字段文档与默认值（部分字段会显示）
kubectl explain deployment.spec.strategy --recursive
kubectl explain pod.spec.containers.resources

# 服务端默认值：真正提交后再看
kubectl create deployment demo --image=nginx --dry-run=server -o yaml
kubectl run demo --image=nginx --dry-run=server -o yaml

# OpenAPI v3
kubectl get --raw /openapi/v3/apis/apps/v1 | jq '.components.schemas["io.k8s.api.apps.v1.DeploymentSpec"]'

# 对比“我写的”和“服务器存的”
kubectl get deployment demo -o yaml
kubectl diff -f deployment.yaml
```

`kubectl explain` 的 `default` 注释并不覆盖所有准入默认值；最可靠的方法是 `--dry-run=server` 后读取返回对象。

## 延伸阅读

- [第 4 章 API 对象模型](../chapters/04-api-object-model.md)：GVK、metadata、resourceVersion 与 SSA。
- [第 5 章 声明式工作流与 kubectl](../chapters/05-declarative-workflow.md)：apply/patch/diff 的语义。
- [第 6 章 Pod 与生命周期](../chapters/06-pod-lifecycle.md)：探针、终止与 sidecar。
- [第 7 章 容器运行时、资源与 QoS](../chapters/07-runtime-resources-qos.md)：requests/limits 与驱逐。
- [附录 A API 资源速查](A-api-resources.md)：资源、作用域与子资源。
