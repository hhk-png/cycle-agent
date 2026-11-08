# 示例清单

这些 YAML 以 `kueue.x-k8s.io/v1beta2` 为主。应用前请确认：

- 已安装 Kueue；
- 目标版本支持对应字段/feature gate；
- 镜像地址在你的环境中可拉取；
- GPU/拓扑/云扩容示例需要相应的节点和控制器。

## kueue/quick-start-setup.yaml + quick-start-job.yaml

最小队列 + 一个 batch Job。

```bash
kubectl apply -f examples/kueue/quick-start-setup.yaml
kubectl apply -f examples/kueue/quick-start-job.yaml
kubectl get workloads -n default
```

## kueue/enforce-management.yaml

纳管边界示例：默认 LocalQueue、默认 WorkloadPriorityClass、ValidatingAdmissionPolicy。

- namespace `team-managed` 同时带 `managed-by-kueue` 和 `kueue-managed` 标签；
- LocalQueue 和 WorkloadPriorityClass 都命名为 `default`，由 Kueue 的 mutating webhook 给未声明队列/优先级的 Job 补标签；
- VAP 只绑定到 `kueue-managed: "true"` 的 namespace，并排除 JobSet 创建的子 Job；
- `defaulted-job` 故意不写 queue-name，用来观察"默认队列先补标签、VAP 后校验"的顺序。

```bash
kubectl apply -f examples/kueue/enforce-management.yaml
kubectl get workloads -n team-managed
kubectl get job defaulted-job -n team-managed -o jsonpath='{.metadata.labels}'
```

注意：VAP 会真实拒绝未打标签的 Job。先在测试集群验证；只想观察时把 `validationActions` 改成 `["Warn"]` 或 `["Audit"]`。默认 LocalQueue 只对新建 Job 生效，已存在的 Job 不会被补标签。

## kueue/cohort.yaml

两个 namespace、两个 ClusterQueue、一个 Cohort、两个 Job。演示：

- namespaceSelector；
- Cohort 借用；
- GPU/CPU/内存多资源；
- borrowingLimit/lendingLimit。

```bash
kubectl apply -f examples/kueue/cohort.yaml
kubectl get workloads -A
kubectl get clusterqueues
```

## kueue/preemption.yaml

两个 PriorityClass、一个 CQ、两个 Job。先提交 `low-batch`，再提交 `high-batch`，观察低优先级被抢占。

```bash
kubectl apply -f examples/kueue/preemption.yaml
kubectl get workloads -n default -w
```

注意：两个 Job 同时 apply 时，创建顺序可能影响谁先准入；可以先单独 apply low-batch，等它 Running 后再 apply high-batch。

## kueue/fair-sharing.yaml

层级 Cohort（root → org-a/org-b）+ 加权 Fair Sharing + 三个 CQ/Job。演示：

- `Cohort.spec.parentName` 分层；
- `fairSharing.weight` 在 Cohort 和 CQ 上的组合；
- 多团队竞争同一 Flavor。

注意：Fair Sharing 只仲裁可借用容量，不改变 nominal 保底。权重不是硬配额。

```bash
kubectl apply -f examples/kueue/fair-sharing.yaml
kubectl get cohorts
kubectl get clusterqueues -o wide
```

## kueue/workload-priority.yaml

两个 `WorkloadPriorityClass` + 一个 CQ + 两个 Job。演示：

- `WorkloadPriorityClass` 只影响 Kueue 排队/抢占；
- Job 通过 `kueue.x-k8s.io/priority-class` 标签选择优先级；
- 它不会改变 Pod 在 `kube-scheduler` 中的优先级。

```bash
kubectl apply -f examples/kueue/workload-priority.yaml
kubectl get workloads -n default -o wide
```

## kueue/admission-check-provisioning.yaml

ProvisioningRequest AdmissionCheck 示例。需要：

- Cluster Autoscaler；
- 支持 `check-capacity.autoscaling.x-k8s.io` provisioning class；
- 云提供商支持 GPU 扩容。

没有这些依赖时，Workload 会停在 QuotaReserved，等待 AdmissionCheck。

## kueue/hierarchical-cohort.yaml

层级 Cohort（root → org-research/org-product）+ Cohort 级 borrowing/lending limit。演示：

- `Cohort.spec.parentName` 分层；
- Cohort 级 `borrowingLimit` 是子树借用总量上限；
- `lendingLimit` 限制子树出借能力；
- CQ 的 `cohortName` 指向子 Cohort。

```bash
kubectl apply -f examples/kueue/hierarchical-cohort.yaml
kubectl get cohorts
kubectl get clusterqueues -o wide
```

## kueue/admission-fair-sharing.yaml

Usage-based Admission Fair Sharing。演示：

- CQ `admissionScope.admissionMode: UsageBasedAdmissionFairSharing`；
- LocalQueue `fairSharing.weight`；
- 同 CQ 下两个队列的历史用量排序。

注意：AFS 需要 `AdmissionFairSharing` gate；没有历史数据时两个队列的行为接近普通 FIFO。

```bash
kubectl apply -f examples/kueue/admission-fair-sharing.yaml
kubectl get localqueues -A -o wide
```

## kueue/concurrent-admission.yaml

Concurrent Admission（alpha）。演示：

- CQ 两个 Flavor，reserved 初始 0 配额，spot 有配额；
- `concurrentAdmissionPolicy.migration`；
- Workload 先跑 spot，reserved 可用后迁移。

需要 `ConcurrentAdmission` gate；只支持 BestEffortFIFO、单资源组、最多 32 个 Flavor。

## kueue/admission-gate.yaml

Admission Gate。演示：

- Job 注解 `kueue.x-k8s.io/admission-gated-by`；
- gate 未清除时 Workload 不占配额；
- 清除 gate 后恢复正常准入。

注意：gate 在配额预留之前，AdmissionCheck 在之后，不要混用。

## kueue/topology-aware.yaml

TAS 示例。需要：

- 节点带 `topology.kubernetes.io/zone`、`cloud.provider.com/topology-block`、`cloud.provider.com/topology-rack`、`kubernetes.io/hostname` 标签；
- 节点带 `cloud.provider.com/node-group: tas-gpu`；
- `TopologyAwareScheduling` 开启（当前多为默认开启，按版本确认）。

如果节点标签不匹配，Workload 会 Pending/Inadmissible，先检查节点标签。

## kueue/jobset.yaml

一个最小 JobSet：1 个 driver + 4 个 worker。Kueue 把每个 `replicatedJob` 映射成一个 PodSet，整个 Workload 一起准入。

```bash
kubectl apply -f examples/kueue/jobset.yaml
kubectl get workloads -n default
kubectl get workload jobset-train -n default -o yaml
```

重点核对 `status.admission.podSetAssignments`：driver 的 `count=1`、worker 的 `count=4`，资源分别是 2 CPU / 4 CPU + 4 GPU。需要 JobSet 控制器和 `jobset.x-k8s.io/jobset` integration。

## kueue/dra.yaml

DRA 端到端对象骨架：DeviceClass、ResourceClaimTemplate、ClusterQueue、LocalQueue 和 Job。配额账本按 Configuration 映射出的逻辑资源 `nvidia.com/gpu` 计费，而不是按 DeviceClass 名。

前置：

- Kubernetes 1.34+ 且启用 DRA；
- 注册 `gpu.example.com` 的 DRA driver；
- `KueueDRAIntegration` gate；
- `examples/config/dra-device-mapping.yaml` 已合并进 `kueue-manager-config` 并重启控制器。

没有 DRA driver 时只能观察对象结构；Workload 可能停在 Pending/Inadmissible。

```bash
kubectl apply -f examples/kueue/dra.yaml
kubectl get deviceclass gpu.example.com
kubectl get workloads -n default
go run ./cmd/minikueue -scenario dra -ticks 2
```

## kueue/resource-transformations.yaml

资源变换示例。Pod 请求 `example.com/gpu/gpumem/gpucores`，配额账本按 `total-gpumem/total-gpucores` 计费，vGPU 计数被忽略。

注意：`resources.transformations` 属于 Kueue 的 Configuration，放在 `examples/config/resource-transformations.yaml`；先按第 3.6.1 节把它合并进 `kueue-manager-config` 并重启控制器，再 `kubectl apply -f examples/kueue/resource-transformations.yaml`（该文件现在只包含 ResourceFlavor/ClusterQueue/LocalQueue/Job）。

## kueue/provisioning-request.yaml

ProvisioningRequest AdmissionCheck 示例。需要 ProvisioningRequest 控制器/云 autoscaler；没有时 Workload 会停在 QuotaReserved。

## kueue/configurable-preemption.yaml

Configurable Preemption（alpha）示例。需要 `ConfigurablePreemptions` gate；CQ 的 Classic Preemption 全关，由 `PreemptionConfig` 规则授权。

注意：规则是对称风险的来源，生产要只保留单向规则。

## kueue/fair-sharing-preemption.yaml

Fair Sharing 抢占示例。`fairSharing.preemptionStrategies` 在 `examples/config/fair-sharing-preemption.yaml`；先合并进 `kueue-manager-config` 并重启，再 `kubectl apply -f examples/kueue/fair-sharing-preemption.yaml` 应用 Cohort/CQ/LocalQueue。

## kueue/multikueue.yaml

MultiKueue 对象骨架：两个 worker Secret/MultiKueueCluster、MultiKueueConfig、AdmissionCheck、带 `admissionChecksStrategy` 的 CQ、LocalQueue 和 Job。

注意：

- Secret 必须在 `kueue-system`，key 必须是 `kubeconfig`；
- 示例 kubeconfig 是占位值，不能直接连接真实 worker；
- 需要每个 worker 已安装 Kueue，且 manager 能访问 worker API server；
- 真实使用前先确认 `MultiKueueCluster` 的 `Active` 条件。

## kueue/custom-admission-check.yaml

自定义 AdmissionCheck 控制器的对象骨架：AdmissionCheck、参数 CRD（`LicenseCheckConfig`）、CQ、LocalQueue、Job。

注意：`LicenseCheckConfig` 不是 Kueue 内置 CRD；必须先安装你的控制器和 CRD，否则对象无法创建/检查永远 Pending。

## kueue/inference-workloads.yaml

推理/长运行/弹性负载骨架：

- Deployment：逐 Pod 通过 Pod 集成管理，队列标签注入 Pod 模板；
- StatefulSet：副本组/PodGroup 管理；
- 裸 Pod：`kueue.x-k8s.io/admission` scheduling gate；
- Job：`kueue.x-k8s.io/job-min-parallelism` 部分准入；
- Deployment 的 TAS 打散注解。

注意：需要替换镜像、资源请求和拓扑标签；Partial Admission/TAS spreading 需要对应 gate。

## 清理

```bash
kubectl delete -f examples/kueue/quick-start-job.yaml
kubectl delete -f examples/kueue/quick-start-setup.yaml
kubectl delete -f examples/kueue/enforce-management.yaml

kubectl delete -f examples/kueue/cohort.yaml
kubectl delete -f examples/kueue/preemption.yaml
kubectl delete -f examples/kueue/fair-sharing.yaml
kubectl delete -f examples/kueue/workload-priority.yaml
kubectl delete -f examples/kueue/admission-check-provisioning.yaml
kubectl delete -f examples/kueue/topology-aware.yaml
kubectl delete -f examples/kueue/hierarchical-cohort.yaml
kubectl delete -f examples/kueue/admission-fair-sharing.yaml
kubectl delete -f examples/kueue/concurrent-admission.yaml
kubectl delete -f examples/kueue/admission-gate.yaml
kubectl delete -f examples/kueue/jobset.yaml
kubectl delete -f examples/kueue/dra.yaml
kubectl delete -f examples/kueue/resource-transformations.yaml
kubectl delete -f examples/kueue/provisioning-request.yaml
kubectl delete -f examples/kueue/configurable-preemption.yaml
kubectl delete -f examples/kueue/fair-sharing-preemption.yaml
kubectl delete -f examples/kueue/multikueue.yaml
kubectl delete -f examples/kueue/custom-admission-check.yaml
kubectl delete -f examples/kueue/inference-workloads.yaml
```

删除顺序建议：先删 Job，再删 LocalQueue/ClusterQueue，最后删 Flavor/Cohort/AdmissionCheck/Topology。Kueue 的 finalizer 需要控制器清理。
