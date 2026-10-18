# 第 17 章 生产运行手册

本章把前面章节落到运维动作：安装、升级、容量、监控、故障排查、变更流程。目标是让 on-call 在凌晨三点也能按步骤定位问题。

## 17.1 上线前检查

- [ ] Kubernetes 版本、CNI、CSI、GPU device plugin 正常
- [ ] 节点池按 CPU/GPU/机型隔离，taint/toleration 配置正确
- [ ] 镜像仓库、ImagePullSecret、镜像扫描/签名
- [ ] operator namespace、RBAC、ResourceQuota
- [ ] CRD 版本与 operator 版本匹配
- [ ] 监控/日志/告警接入
- [ ] 备份策略（GCS RocksDB、PVC、对象存储）
- [ ] 租户 onboarding 流程
- [ ] 混沌测试/故障演练计划

## 17.2 安装

### 17.2.1 Helm

```bash
helm repo add kuberay https://ray-project.github.io/kuberay-helm/
helm repo update

helm install kuberay-operator kuberay/kuberay-operator \
  --version 1.7.1 \
  --namespace kuberay-system --create-namespace \
  -f feature-gates.yaml  # featureGates: [{name: RayCronJob, enabled: true}]
```

多租户/最小权限安装：用 `watchNamespace` 限制监听范围，并让 chart 生成 namespace 级 RBAC：

```bash
helm install kuberay-operator kuberay/kuberay-operator \
  --version 1.7.1 \
  --namespace team-a-kuberay --create-namespace \
  --set 'watchNamespace={team-a}' \
  --set singleNamespaceInstall=true
```

注意：

- `watchNamespace` 是 list；多个 namespace 用 `{team-a,team-b}`；
- 改监听范围后必须重启 operator；
- 每个租户一套 operator 时，CRD 仍然是集群级的，升级要统一规划（第 3.2.3、14.9.2 节）。

### 17.2.2 验证

```bash
kubectl -n kuberay-system rollout status deploy/kuberay-operator
kubectl get crd | grep ray.io
kubectl apply -f manifests/samples/ray-cluster.yaml
kubectl get raycluster
kubectl describe raycluster raycluster-sample
```

### 17.2.3 常见安装错误

| 错误 | 原因 | 处理 |
|---|---|---|
| `no matches for kind RayCluster` | CRD 未安装 | 安装 crds |
| operator CrashLoopBackOff | RBAC/镜像/参数 | 看 logs |
| webhook 超时 | 开启了可选 validating webhook 但证书/Service 未就绪 | 关闭 `ENABLE_WEBHOOKS` 或修复证书；核心路径不依赖 webhook（NodeLabelDelivery 属于 v1.8+，不在 v1.7.1） |
| Pod 无法创建 | ResourceQuota/PodSecurity/GPU | 看事件 |
| CRD 过大 apply 失败 | client-side apply 注解限制 | 用 server-side apply |

## 17.3 升级

### 17.3.1 升级顺序

1. 读 release notes / breaking changes；
2. 备份 CR/配置；
3. 升级 CRD（**Helm 不会自动升级 `crds/`**）；
4. 升级 operator；
5. 观察 reconcile 错误/重启；
6. 升级 Ray 镜像/版本（可选）；
7. 回滚预案。

CRD 升级：

```bash
kubectl apply --server-side -f https://raw.githubusercontent.com/ray-project/kuberay/v1.7.1/ray-operator/config/crd/bases/ray.io_rayclusters.yaml
# 其余三个 CRD 同理
```

或从 Helm chart 的 `crds/` 目录取。

### 17.3.2 升级 operator

```bash
helm upgrade kuberay-operator kuberay/kuberay-operator --version 1.7.1
kubectl -n kuberay-system rollout status deploy/kuberay-operator
```

如果开启 leader election，滚动升级期间会有短暂切换。leader 租约、缓存同步、探针和升级期间的控制面 SLO 见[第 54 章](54-operator-reliability-control-plane-slo.md)。

### 17.3.3 升级 Ray

- 修改 `spec.rayVersion` 和镜像 tag；
- 先在一个测试集群验证；
- 注意 Python/依赖/驱动兼容；
- RayCluster 不会原地滚动升级 Ray；通常需要重建 Pod；
- RayService 用新集群升级；
- RayJob 用新模板跑新作业。

### 17.3.4 回滚

- operator 回滚到上一版本；
- CRD 通常只增不减，回滚前确认旧 operator 能读新 schema；
- 业务 CR 回滚到 Git 中的上一版本；
- 如果新 operator 写了新字段，旧 operator 可能忽略（取决于 schema）。

## 17.4 容量与配额

### 17.4.1 节点池

- CPU 池、GPU 池、高内存池分开；
- taint GPU 节点，只有 GPU Pod 容忍；
- 每池设置 Cluster Autoscaler/Karpenter 上下限；
- 预留系统组件资源。

### 17.4.2 namespace 配额

```yaml
apiVersion: v1
kind: ResourceQuota
metadata:
  name: team-a
  namespace: team-a
spec:
  hard:
    requests.cpu: "200"
    requests.memory: 400Gi
    requests.nvidia.com/gpu: "16"
    pods: "200"
```

### 17.4.3 Kueue 队列

- 每团队一个 ClusterQueue/LocalQueue；
- 设置 nominalQuota/borrowing；
- RayJob/RayCluster 通过 label 选择队列；
- 监控 pending workloads。

## 17.5 多集群

| 模式 | 说明 |
|---|---|
| 单集群多 namespace | 最常见；RBAC/配额隔离 |
| 多集群按环境 | dev/staging/prod |
| 多集群按地域 | 数据本地性/容灾 |
| Kueue MultiKueue | 跨集群调度 |
| 每租户 operator | 强隔离，成本高 |

多集群要注意：

- CRD/operator 版本一致；
- 镜像仓库可达；
- 对象存储/数据位置；
- 监控聚合；
- 跨集群网络/延迟；
- 灾备切换流程。

## 17.6 监控与告警

### 17.6.1 必看指标

- operator reconcile errors/duration；
- RayCluster Ready；
- RayJob 成功率/失败率/时长；
- RayService Ready/endpoints/延迟；
- autoscaler 扩容失败/抖动；
- Pending Pod 数；
- GPU 利用率/显存；
- 节点压力（磁盘/内存/PID）；
- API Server 限流。

### 17.6.2 告警分级

| 级别 | 例子 | 响应 |
|---|---|---|
| P1 | 所有 RayService Ready=0 | 立即 |
| P1 | operator 无法调和 | 立即 |
| P2 | 单集群 head 重启 | 15 分钟 |
| P2 | RayJob 失败率 > 10% | 30 分钟 |
| P3 | GPU 利用率低 | 工作时间 |
| P3 | 单 Pod 重启 | 观察 |

### 17.6.3 PrometheusRule 示例

```yaml
apiVersion: monitoring.coreos.com/v1
kind: PrometheusRule
metadata:
  name: kuberay
  namespace: monitoring
spec:
  groups:
    - name: kuberay.rules
      rules:
        - alert: KubeRayOperatorReconcileErrors
          expr: sum(rate(controller_runtime_reconcile_errors_total[5m])) > 0.1
          for: 10m
          labels: {severity: warning}
          annotations:
            summary: "KubeRay operator reconcile error rate is high"
        # 不要用 kuberay_cluster_condition_provisioned：它是粘性的，
        # 曾经 Ready 的集群即使当前 head 挂了也不会变回 0。
        - alert: RayClusterHeadNotReady
          # head Pod 名默认带随机后缀，不能用 pod=~".+-head$"。
          # 用 ray.io/node-type=head 标签做 join。
          expr: |
            kube_pod_status_ready{condition="true"} == 0
            and on(namespace, pod) kube_pod_labels{label_ray_io_node_type="head"}
          for: 15m
          labels: {severity: critical}
        - alert: RayServiceNotReady
          expr: kuberay_service_condition_ready == 0
          for: 5m
          labels: {severity: critical}
        - alert: RayJobFailed
          expr: kuberay_job_deployment_status{deployment_status="Failed"} == 1
          for: 5m
          labels: {severity: warning}
```

> 指标名/标签随 KubeRay 版本变化；上线前用 `kubectl exec`/`curl /metrics` 核对，避免“规则永远不触发”。

## 17.7 故障排查手册

### 17.7.1 RayCluster 不 Ready

```bash
kubectl get raycluster <name> -o yaml
kubectl describe raycluster <name>
kubectl get pods -l ray.io/cluster=<name> -o wide
kubectl -n kuberay-system logs deploy/kuberay-operator --tail=200
```

按顺序：

1. `status.conditions`：HeadPodReady/Provisioned/ReplicaFailure；
2. head Pod 是否存在/Ready；
3. worker Pod 是否 Pending；
4. Service 是否存在/selector；
5. 事件：InvalidSpec/FailedToCreate/RBAC；
6. 资源/配额/GPU；
7. 镜像/拉取；
8. 节点 affinity/taint。

### 17.7.2 Pod Pending

```bash
kubectl describe pod <pod>
```

常见：

- `0/N nodes available: insufficient cpu/memory/gpu`；
- `node(s) had untolerated taint`；
- `node affinity/selector mismatch`；
- `ResourceQuota exceeded`；
- `PodSecurity`；
- `volume node affinity conflict`。

### 17.7.3 ImagePullBackOff

- 镜像名/tag 错；
- 私有仓库 Secret 缺失；
- 节点无法访问仓库；
- 镜像架构不匹配（arm64/amd64）；
- 限流。

### 17.7.4 head 反复重启

- liveness 探针过严；
- GCS/Redis 不可达；
- OOM；
- 磁盘满；
- 证书/认证失败；
- 节点问题。

### 17.7.5 worker 无法加入

- head Service DNS 不通；
- 6379 被 NetworkPolicy 拦；
- init container 卡住；
- token/mTLS 不匹配；
- Ray 版本不一致。

### 17.7.6 RayJob 卡在 Initializing

- 集群不 Ready；
- 镜像拉取；
- autoscaler 无容量；
- `preRunningDeadlineSeconds` 未设置；
- Dashboard 不可达（HTTPMode）。

### 17.7.7 RayService 不 Ready

- Serve app DEPLOY_FAILED；
- deployment unhealthy；
- `ray.io/serve` 标签未变 true；
- Serve Service selector 不匹配；
- endpoint 数为 0；
- 网络策略阻断 proxy。

### 17.7.8 删除卡住

```bash
kubectl get raycluster <name> -o jsonpath='{.metadata.finalizers}'
kubectl get rayjob <name> -o jsonpath='{.metadata.finalizers}'
kubectl get rayservice <name> -o jsonpath='{.metadata.finalizers}'
```

- GCS FT Redis cleanup 卡住；
- RayService 等待 RayCluster 删除；
- RayJob 等待停止作业；
- 子资源 ownerReference 循环。

处理：

1. 查 operator 日志；
2. 手动清理卡住的 Job/Pod；
3. 确认 Redis cleanup 完成；
4. 必要时按文档移除 finalizer（谨慎，可能泄漏资源）。

## 17.8 备份与恢复

- RayCluster/RayJob/RayService YAML 存 Git；
- GCS RocksDB PVC 快照；
- Redis 持久化/备份；
- 对象存储版本/生命周期；
- 模型/checkpoint 定期备份；
- 恢复演练。

## 17.9 变更管理

- CRD/operator 升级单独变更；
- 一次只开一个 alpha feature gate；
- 先在 staging 跑 RayJob/RayService；
- 灰度 namespace/租户；
- 观察 24 小时；
- 准备回滚。

## 17.10 SLO 建议

| SLI | 目标 |
|---|---|
| RayCluster 创建到 Ready | < 5 分钟（视镜像） |
| RayJob 提交成功率 | > 99% |
| RayService 可用性 | > 99.9% |
| 升级中断时间 | < 30 秒 |
| operator reconcile 错误率 | < 0.1% |

## 17.11 On-call 决策树

```text
告警：RayCluster NotReady
├─ CR 存在吗？
│   └─ 否 -> 被删除/GitOps 漂移 -> 查审计/Argo CD
├─ conditions 哪一项 False？
│   ├─ HeadPodReady=False -> 看 head Pod 调度/镜像/日志
│   ├─ Provisioned=False, HeadPodReady=True -> worker 不足
│   └─ ReplicaFailure=True -> RBAC/配额/API
├─ Pod 存在吗？
│   ├─ 否 -> operator 日志/事件
│   └─ 是 -> Pending/ImagePull/CrashLoop?
└─ Pod Running 但 Ray 看不到？
    ├─ init container 卡住 -> DNS/6379
    ├─ raylet 注册失败 -> token/mTLS/版本/资源
    └─ 节点资源为 0 -> num-cpus/num-gpus 错配
```

告警：RayService NotReady

```text
1. Ready condition / pendingServiceStatus
2. Serve application 状态
3. ray.io/serve 标签 / Serve Service endpoints
4. pending 集群资源/配额
5. Gateway/HTTPRoute 权重
6. 回滚/切回 active
```

告警：RayJob 失败/卡住

```text
1. jobDeploymentStatus（Initializing/Running/Retrying/Failed）
2. RayCluster 是否 Ready
3. submitter 是否成功
4. Ray job 状态/日志
5. deadline/backoff/cleanup
```

告警：operator 错误率

```text
1. 最近是否升级 CRD/operator/Ray？
2. RBAC/leader election
3. API Server 限流
4. CRD schema/默认值冲突
5. 回滚
```

更完整的 14 个案例见[第 37 章](37-production-incident-postmortems.md)，事件/错误对照见[附录 H](appendix-h-events-errors-status.md)。

## 17.12 应急响应分级与沟通

### 17.12.1 级别定义

| 级别 | 定义 | 响应 | 例子 |
|---|---|---|---|
| P1 | 全平台/多租户不可用 | 立即，全员 | operator 不工作、所有 RayService 503 |
| P2 | 单租户/单区域故障 | 15 分钟 | 单集群 head 反复重启 |
| P3 | 容量/性能/单作业 | 工作时间 | GPU 利用率低、单作业失败 |
| P4 | 趋势/优化 | 排期 | 成本上升、告警噪音 |

### 17.12.2 角色

- **Incident Commander（IC）**：决策、协调、对外沟通；
- **Operator**：执行命令、采集证据；
- **Communicator**：更新状态页/群公告；
- **Scribe**：记录时间线（复盘用）。

小团队可以一人多角，但要明确“谁决策”。

### 17.12.3 沟通模板

```text
[P1][Ray 平台] 当前状态：调查中
影响：所有 RayService 请求 503；RayJob 提交失败
开始时间：2026-10-18 02:10 UTC
当前动作：回滚 operator 到 v1.7.0；检查 CRD/网络
下一次更新：02:40 UTC
负责人：@oncall
```

要点：

- 先给影响面和下一次更新时间；
- 不要在没有证据时给根因；
- 缓解后更新“已恢复/观察中”；
- 复盘时保留沟通记录。

### 17.12.4 升级路径

```text
on-call -> 平台负责人 -> 网络/存储/GPU 团队 -> 厂商支持
```

提前写好联系方式/时区/升级条件，不要故障时再找。

### 17.12.5 止血与取证

- 先止血（回滚/扩容/摘流量）；
- 同时保留证据（CR/Pod/Event/日志/指标）；
- 不要为了“复现”而扩大影响面；
- 止血后 24 小时内完成初步复盘。

## 17.13 小结

- 生产化的核心是：CRD/operator 升级流程、容量/配额、监控告警、故障排查、备份恢复。
- Helm 不升级 CRD；升级要单独处理。
- 大多数“KubeRay 问题”最终落在资源、网络、镜像、RBAC、配额。
- 准备 runbook 和演练，比记住字段更重要。
- 离线安装、v1alpha1→v1 迁移、跨集群灾备的完整流程见[第 31 章](31-upgrade-migration-dr.md)。

下一章看未来方向。

→ [第 18 章 未来方向](18-future.md)
