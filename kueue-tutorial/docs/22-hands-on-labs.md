# 第 22 章 · 真实集群实验手册

前面的章节以"读"和"模拟"为主。本章把 Kueue 放进一个真实的 kind 集群，用 12 个核心实验 + 8 个可选实验把概念变成可观察的行为。每个实验都包含目标、清单、命令、预期结果和排障提示。

> 约定：实验使用 kind + Kueue v0.20.5（2026-11 示例版本，可替换）。所有命令都在 `kueue-tutorial/` 目录下执行；示例清单在 `examples/kueue/`。如果集群已有 Kueue，跳过 Lab 1 的安装，但要确认版本和 CRD 一致。

## 22.0 实验环境

### 依赖

- Docker（或 Podman）
- kind
- kubectl
- Helm 3（可选）
- curl / jq

### 创建集群

```bash
kind create cluster --name kueue-lab
kubectl cluster-info --context kind-kueue-lab
kubectl get nodes
```

### 目录约定

```text
examples/kueue/
  quick-start-setup.yaml      # Flavor + CQ + LQ
  quick-start-job.yaml        # 示例 Job
  cohort.yaml                 # Cohort 借用/共享
  preemption.yaml             # 抢占
  fair-sharing.yaml           # 公平分享
  topology-aware.yaml         # TAS
  workload-priority.yaml      # 优先级
  admission-check-provisioning.yaml
```

每个实验结束时执行对应的 `kubectl delete`，避免互相干扰。也可以直接重建集群：

```bash
kind delete cluster --name kueue-lab
kind create cluster --name kueue-lab
```

## 22.1 Lab 1：安装与验收

### 目标

安装 Kueue，确认控制器、CRD、webhook、metrics 都可用。

### 步骤

```bash
export KUEUE_VERSION=v0.20.5
kubectl apply --server-side -f \
  "https://github.com/kubernetes-sigs/kueue/releases/download/${KUEUE_VERSION}/manifests.yaml"

kubectl wait deploy/kueue-controller-manager -n kueue-system \
  --for=condition=available --timeout=5m

kubectl get pods -n kueue-system
kubectl get crd | grep kueue
```

### 验收

```bash
# 1. 控制器 Running
kubectl -n kueue-system get deploy kueue-controller-manager

# 2. webhook 拒绝非法 CQ
kubectl apply -f - <<'EOF'
apiVersion: kueue.x-k8s.io/v1beta2
kind: ClusterQueue
metadata:
  name: invalid
spec: {}
EOF
# 期望：resourceGroups 相关校验错误

# 3. 指标
kubectl -n kueue-system port-forward \
  svc/kueue-controller-manager-metrics-service 8443:8443 &
curl -k https://localhost:8443/metrics | grep '^kueue_' | head
```

### 排障

- Pod 没起来：看 `kubectl -n kueue-system describe pod` 和 events；
- webhook 报证书错误：看 `kueue-webhook-service` 和 Secret；
- CRD 没装上：`kubectl apply --server-side` 是否用了 `--server-side`。

## 22.2 Lab 2：最小队列与 Job 生命周期

### 目标

观察 Workload 从 Pending → QuotaReserved → Admitted → Finished。

### 清单

```bash
kubectl apply -f examples/kueue/quick-start-setup.yaml
kubectl apply -f examples/kueue/quick-start-job.yaml
```

### 观察

```bash
kubectl get clusterqueue cluster-queue -o wide
kubectl get localqueue user-queue -n default -o wide
kubectl get workloads -n default -w
```

另一个终端：

```bash
kubectl get jobs -n default
kubectl get pods -n default
```

预期：

- CQ/LQ `ACTIVE=True`；
- Workload 先出现，`RESERVED IN=cluster-queue`，随后 `ADMITTED=True`；
- Job 从 `suspend` 变为运行，Pod 创建；
- 30 秒后 Workload `FINISHED=True`，配额释放。

### 深入

```bash
kubectl get workload sample-job -n default -o yaml
```

重点字段：

```yaml
status:
  conditions:
  - type: QuotaReserved
    status: "True"
  - type: Admitted
    status: "True"
  - type: Finished
    status: "True"
  admission:
    clusterQueue: cluster-queue
    podSetAssignments: [...]
```

### 思考题

1. 如果把 `parallelism` 改成 10，会发生什么？
2. Workload 的 `spec.podSets[0].count` 和 Job 的 `parallelism` 是什么关系？
3. `pods` 资源有没有被计入？在哪里看？

## 22.3 Lab 3：配额耗尽与两种 FIFO

### 目标

对比 StrictFIFO 和 BestEffortFIFO 的队头阻塞行为。

### 清单

```bash
kubectl apply -f - <<'EOF'
apiVersion: kueue.x-k8s.io/v1beta2
kind: ResourceFlavor
metadata:
  name: default-flavor
---
apiVersion: kueue.x-k8s.io/v1beta2
kind: ClusterQueue
metadata:
  name: fifo-cq
spec:
  namespaceSelector: {}
  queueingStrategy: StrictFIFO
  resourceGroups:
  - coveredResources: ["cpu", "memory"]
    flavors:
    - name: default-flavor
      resources:
      - name: cpu
        nominalQuota: 2
      - name: memory
        nominalQuota: 4Gi
---
apiVersion: kueue.x-k8s.io/v1beta2
kind: LocalQueue
metadata:
  namespace: default
  name: fifo-q
spec:
  clusterQueue: fifo-cq
EOF
```

提交一个 3 CPU 的 "big" 和一个 1 CPU 的 "small"：

```bash
kubectl apply -f - <<'EOF'
apiVersion: batch/v1
kind: Job
metadata:
  name: big-head
  namespace: default
  labels:
    kueue.x-k8s.io/queue-name: fifo-q
spec:
  parallelism: 1
  completions: 1
  template:
    spec:
      containers:
      - name: c
        image: busybox
        command: ["sleep", "3600"]
        resources:
          requests:
            cpu: "3"
      restartPolicy: Never
---
apiVersion: batch/v1
kind: Job
metadata:
  name: small-tail
  namespace: default
  labels:
    kueue.x-k8s.io/queue-name: fifo-q
spec:
  parallelism: 1
  completions: 1
  template:
    spec:
      containers:
      - name: c
        image: busybox
        command: ["sleep", "3600"]
        resources:
          requests:
            cpu: "1"
      restartPolicy: Never
EOF
```

### 观察

```bash
kubectl get workloads -n default
```

StrictFIFO 下：

- `big-head` Pending（放不下）；
- `small-tail` 也 Pending（被队头阻塞）。

把策略改成 `BestEffortFIFO`：

```bash
kubectl patch clusterqueue fifo-cq --type=merge \
  -p '{"spec":{"queueingStrategy":"BestEffortFIFO"}}'
```

等一个调度周期：

```bash
kubectl get workloads -n default -w
```

预期：`small-tail` 被准入，`big-head` 仍 Pending。

### 排障

如果 `small-tail` 仍不跑，检查：

- 是否还在同一个 tick/调度周期；
- `big-head` 是否已经占了 CPU；
- Workload 的 `QuotaReserved` 条件 Reason。

## 22.4 Lab 4：Cohort 借用与 lendingLimit

### 目标

观察 A 借用 B 的空闲配额，以及 `lendingLimit` 如何限制借用。

### 清单

```bash
kubectl apply -f examples/kueue/cohort.yaml
```

先看文件里的 CQ/Cohort 结构：

```bash
grep -E 'kind:|name:|cohortName|nominalQuota|lendingLimit|borrowingLimit' \
  examples/kueue/cohort.yaml
```

示例文件自带两个 Job。为了单独观察借用，先把它们删掉，让 Cohort 处于空闲状态：

```bash
kubectl delete job team-a-train -n team-a --ignore-not-found
kubectl delete job team-b-train -n team-b --ignore-not-found
kubectl get workloads -A
```

提交一个超过自己 nominal 的 Job（team-a nominal 8 CPU，这里请求 12 CPU，需要借 4）：

```bash
kubectl apply -f - <<'EOF'
apiVersion: batch/v1
kind: Job
metadata:
  name: borrow-job
  namespace: team-a
  labels:
    kueue.x-k8s.io/queue-name: team-a-queue
spec:
  parallelism: 1
  completions: 1
  template:
    spec:
      containers:
      - name: c
        image: busybox
        command: ["sleep", "600"]
        resources:
          requests:
            cpu: "12"
      restartPolicy: Never
EOF
```

### 观察

```bash
kubectl get clusterqueue -o wide
kubectl get workloads -n team-a
kubectl get workload borrow-job -n team-a -o yaml
```

重点：

- `status.admission.clusterQueue`；
- CQ 的 usage 是否超过 nominal；
- 另一个 CQ 的 `lendingLimit` 是否成为瓶颈。

### 实验

先清理上一个 Job，避免它继续占用 Cohort：

```bash
kubectl delete job borrow-job -n team-a
```

把 `team-b-cq` 的 CPU `lendingLimit` 改成 1：

```bash
kubectl patch clusterqueue team-b-cq --type=json -p \
  '[{"op":"add","path":"/spec/resourceGroups/0/flavors/0/resources/0/lendingLimit","value":1}]'
```

再提交一个需要借 4 CPU 的 Job，观察是否 Pending（A 最多只能借到 B 愿意出借的 1 CPU）。恢复：

```bash
kubectl patch clusterqueue team-b-cq --type=json -p \
  '[{"op":"remove","path":"/spec/resourceGroups/0/flavors/0/resources/0/lendingLimit"}]'
```

### 思考题

1. 如果 `borrowingLimit` 是 null，能借多少？
2. 如果 `lendingLimit` 是 0，B 自己还能用多少？
3. Cohort 的共享 `nominalQuota` 是额外的还是汇总的？

## 22.5 Lab 5：抢占与回收

### 目标

观察同 CQ 抢占和 Cohort 回收。

### 清单

```bash
kubectl apply -f examples/kueue/preemption.yaml
kubectl get clusterqueue -o wide
```

按文件里的优先级提交，观察：

```bash
kubectl get workloads -n default -w
kubectl describe workload <被抢占的> -n default
```

预期条件：

```yaml
status:
  conditions:
  - type: Evicted
    status: "True"
    reason: Preempted
  - type: Preempted
    status: "True"
    reason: InClusterQueue   # 或 InCohort
```

以及：

```bash
kubectl get workload <被抢占的> -n default \
  -o jsonpath='{.status.requeueState}'
```

### 回收

使用 `examples/kueue/cohort.yaml` 或 `preemption.yaml` 中的 reclaim 配置，让原主提交一个在 nominal 内的 Job，观察借用者被驱逐。

### 排障

- 没触发抢占：检查 `withinClusterQueue`/`reclaimWithinCohort` 是否非 `Never`；
- 抢了但不够：看 victim 资源是否覆盖 preemptor 的需求；
- 被抢占者没重排队：看 `active`/`requeueState`；
- 反复抢占：看优先级和退避配置。

## 22.6 Lab 6：namespaceSelector 与 matchExpressions

### 目标

验证 `null`、`{}`、`matchLabels`、`matchExpressions` 四种语义。

### 清单

```bash
kubectl create namespace team-blue
kubectl create namespace team-red
kubectl label namespace team-blue team=blue environment=production
kubectl label namespace team-red team=red environment=production

kubectl apply -f - <<'EOF'
apiVersion: kueue.x-k8s.io/v1beta2
kind: ResourceFlavor
metadata:
  name: selector-flavor
---
apiVersion: kueue.x-k8s.io/v1beta2
kind: ClusterQueue
metadata:
  name: selector-cq
spec:
  namespaceSelector:
    matchLabels:
      environment: production
    matchExpressions:
    - key: team
      operator: In
      values: ["blue", "green"]
  resourceGroups:
  - coveredResources: ["cpu"]
    flavors:
    - name: selector-flavor
      resources:
      - name: cpu
        nominalQuota: 4
---
apiVersion: kueue.x-k8s.io/v1beta2
kind: LocalQueue
metadata:
  namespace: team-blue
  name: q
spec:
  clusterQueue: selector-cq
---
apiVersion: kueue.x-k8s.io/v1beta2
kind: LocalQueue
metadata:
  namespace: team-red
  name: q
spec:
  clusterQueue: selector-cq
EOF
```

提交两个 Job（分别在 team-blue/team-red）：

```bash
for ns in team-blue team-red; do
kubectl apply -f - <<EOF
apiVersion: batch/v1
kind: Job
metadata:
  name: selector-job
  namespace: $ns
  labels:
    kueue.x-k8s.io/queue-name: q
spec:
  parallelism: 1
  completions: 1
  template:
    spec:
      containers:
      - name: c
        image: busybox
        command: ["sleep", "60"]
        resources:
          requests:
            cpu: "1"
      restartPolicy: Never
EOF
done
```

### 观察

```bash
kubectl get workloads -A
kubectl describe workload selector-job -n team-red
```

预期：

- `team-blue` Admitted；
- `team-red` Pending，`QuotaReserved=False`，Reason `Inadmissible`（message 指向 namespaceSelector 不匹配）。

### 实验

1. 把 `namespaceSelector` 改成 `{}`，两个 namespace 都能进；
2. 删掉 `namespaceSelector`，两个都不能进；
3. 把 `operator` 改成 `NotIn`，观察反转。

### 纳管边界实验（可选）

目标：验证"默认 LocalQueue 补标签"与"VAP 拒绝未声明队列"的先后顺序。

```bash
kubectl apply -f examples/kueue/enforce-management.yaml
kubectl get workloads -n team-managed
kubectl get job defaulted-job -n team-managed -o jsonpath='{.metadata.labels}'
```

观察：

- `defaulted-job` 的 labels 里是否出现 `kueue.x-k8s.io/queue-name: default`；
- 是否创建了对应 Workload，且 `spec.queueName` 为 `default`；
- 把 `ValidatingAdmissionPolicyBinding` 的 `validationActions` 改成 `["Warn"]`，再提交一个未打标签的 Job，观察警告事件；
- 删除 LocalQueue `default` 后再提交未打标签的 Job，确认 VAP 会拒绝（或在 `Warn` 模式下只告警、作业直接绕过 Kueue）；
- 检查 `managedJobsNamespaceSelector` 生效时，未打 namespace 标签的 namespace 中的作业是否"静默不受管理"。

清理：

```bash
kubectl delete -f examples/kueue/enforce-management.yaml
```

## 22.7 Lab 7：AdmissionCheck

### 目标

观察"配额已预留但未准入"的两阶段状态，以及"检查控制器不 Active 时 CQ 会 Inactive"。

### 7.1 先看反例：没有控制器的 AdmissionCheck

```bash
kubectl apply -f - <<'EOF'
apiVersion: kueue.x-k8s.io/v1beta2
kind: ResourceFlavor
metadata:
  name: check-flavor
---
apiVersion: kueue.x-k8s.io/v1beta2
kind: ClusterQueue
metadata:
  name: check-cq
spec:
  namespaceSelector: {}
  admissionChecksStrategy:
    admissionChecks:
    - name: example-check
  resourceGroups:
  - coveredResources: ["cpu"]
    flavors:
    - name: check-flavor
      resources:
      - name: cpu
        nominalQuota: 4
---
apiVersion: kueue.x-k8s.io/v1beta2
kind: LocalQueue
metadata:
  namespace: default
  name: check-q
spec:
  clusterQueue: check-cq
---
apiVersion: kueue.x-k8s.io/v1beta2
kind: AdmissionCheck
metadata:
  name: example-check
spec:
  controllerName: example.com/no-controller
EOF
```

观察：

```bash
kubectl get admissioncheck example-check -o yaml
kubectl get clusterqueue check-cq -o wide
kubectl describe clusterqueue check-cq
```

预期：

- AdmissionCheck 没有 `Active=True`；
- ClusterQueue `ACTIVE=False`，condition/message 会指出 AdmissionCheck 不存在或未 Active；
- 此时提交 Job，Workload 不会获得 QuotaReserved。

这说明：**AdmissionCheck 不是"写个名字"就生效，必须有控制器把它标为 Active。**

### 7.2 使用内置 ProvisioningRequest

`examples/kueue/admission-check-provisioning.yaml` 提供了一个可用的配置：

```bash
kubectl apply -f examples/kueue/admission-check-provisioning.yaml
kubectl get admissioncheck prov-test -o yaml
kubectl get clusterqueue gpu-cq -o wide
```

如果 CQ `ACTIVE=True`，提交带 queue-name 的 Job，观察：

```bash
kubectl get workloads -n default -o yaml
kubectl get provisioningrequests -A
```

预期：

- Workload `QuotaReserved=True`；
- `Admitted=False`，`status.admissionChecks` 为 Pending/Retry；
- 配额已经被占用（其他 Job 不能用）；
- ProvisioningRequest 会被创建，名字类似 `<workload>-<check>-<attempt>`。

没有 Cluster Autoscaler/云控制器时，检查不会变成 Ready，Workload 可能一直 Pending 或进入 Retry/Rejected。这正好演示"预留不等于准入"。

### 7.3 清理

```bash
kubectl delete -f examples/kueue/admission-check-provisioning.yaml
kubectl delete clusterqueue check-cq --ignore-not-found
kubectl delete localqueue check-q -n default --ignore-not-found
kubectl delete admissioncheck example-check --ignore-not-found
kubectl delete resourceflavor check-flavor --ignore-not-found
```

## 22.8 Lab 8：TAS

### 目标

观察"配额够但拓扑放不下"。

### 前置

kind 节点需要打上拓扑标签。单节点 kind 只能模拟一个 host；多节点集群需要 `kind` 配置。以下用单节点 + 两个 domain 的简化方式不可行——TAS 的 capacity 来自真实节点。因此本实验有两种做法：

1. **多节点 kind**：创建 4 个 worker，打上 rack/hostname 标签；
2. **只读示例**：用 `examples/kueue/topology-aware.yaml` 观察对象结构，把实际运行留到有真实节点的环境。

多节点 kind 配置示例：

```yaml
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
nodes:
- role: control-plane
- role: worker
  labels:
    topology.kubernetes.io/zone: zone-a
    cloud.provider.com/topology-block: block-a
    cloud.provider.com/topology-rack: rack-a
    cloud.provider.com/node-group: tas-gpu
- role: worker
  labels:
    topology.kubernetes.io/zone: zone-a
    cloud.provider.com/topology-block: block-a
    cloud.provider.com/topology-rack: rack-a
    cloud.provider.com/node-group: tas-gpu
- role: worker
  labels:
    topology.kubernetes.io/zone: zone-a
    cloud.provider.com/topology-block: block-b
    cloud.provider.com/topology-rack: rack-b
    cloud.provider.com/node-group: tas-gpu
- role: worker
  labels:
    topology.kubernetes.io/zone: zone-a
    cloud.provider.com/topology-block: block-b
    cloud.provider.com/topology-rack: rack-b
    cloud.provider.com/node-group: tas-gpu
```

创建后确认节点标签（kind 默认已有 `kubernetes.io/hostname`），再创建 Topology/ResourceFlavor：

```bash
kubectl get nodes --show-labels
kubectl apply -f examples/kueue/topology-aware.yaml
kubectl get topology
kubectl get resourceflavor tas-gpu -o yaml
```

提交带 TAS 注解的 Job（示例文件里已经包含）：

```yaml
metadata:
  annotations:
    kueue.x-k8s.io/podset-required-topology: cloud.provider.com/topology-rack
```

### 观察

```bash
kubectl get workload <name> -n default -o yaml
```

重点看 `status.admission.podSetAssignments[*].topologyAssignment`（字段名以版本为准），以及 Pod 是否落在同一 rack。

### 排障

- Flavor 没有 `topologyName`：不会走 TAS；
- 注解 level 不在 Topology 里：该 PodSet/Workload 会 Inadmissible/Pending，Flavor 本身不受影响；排障先看 Workload 条件，不要删 Flavor；
- 节点没有对应 label：domain 容量为 0；
- `required` 放不下：不会降级，直接 Pending；
- 示例 Job 请求 `nvidia.com/gpu`。普通 kind 节点没有该资源的 allocatable，TAS 会把它算成 domain 容量 0，Workload 在 **Kueue 层**就会 Pending/Inadmissible，拿不到 `topologyAssignment`；这不是 kube-scheduler 的问题。要观察 TAS 分配，先把 Job 的 requests 改成 `cpu`/`memory`（或安装 fake device plugin，让节点声明这个扩展资源）。

### 观察 TAS 分配

```bash
kubectl get workload tas-job -n default -o yaml
kubectl get pods -n default -o wide
```

如果 Workload 已 Admitted 但 Pod 没起来，先用 `kubectl describe pod` 区分是"TAS 放不下"还是"节点没有资源"。

## 22.9 Lab 9：DRA（可选）

### 目标

理解 DRA 配额计费，而不是真的分配设备。

### 前置

- Kubernetes 1.34+；
- DRA driver（例如示例 NVIDIA GPU driver 或模拟 driver）；
- Kueue 的 `KueueDRAIntegration` 开启。

### 观察

先把 `examples/config/dra-device-mapping.yaml` 的 `resources.deviceClassMappings` 合并进 `kueue-manager-config` 并重启控制器（流程见 3.6.1），再应用示例：

```bash
kubectl apply -f examples/kueue/dra.yaml
kubectl get deviceclasses
kubectl get resourceslices
kubectl get resourceclaims -A
kubectl get workload dra-job -n default -o yaml
```

Kueue 会把 `gpu.example.com` 映射成逻辑资源 `nvidia.com/gpu` 并计费；`status.admission` 里看到的是逻辑资源名，不是 DeviceClass。没有 DRA driver 时，本实验只能做对象/配置观察，Workload 可能停在 Pending/Inadmissible。

### 注意

- `ResourceClaimTemplate` 路径和扩展资源路径不要对同一个 DeviceClass 同时配置；
- 配额够不代表设备分得到；
- `WaitForPodsReady` 是最终兜底。

## 22.10 Lab 10：MultiKueue（可选）

### 目标

理解 manager/worker 的提名与选中。

### 前置

- 两个 kind 集群：`mk-manager`、`mk-worker`；
- 两个集群都安装 Kueue；
- worker 的 kubeconfig 存 Secret（不要用 Path）。

### 步骤概要

```bash
kind create cluster --name mk-manager
kind create cluster --name mk-worker
# 分别在两个集群安装 Kueue

# 在 manager 创建 MultiKueueConfig / MultiKueueCluster / Secret
# 创建带 MultiKueue AdmissionCheck 的 CQ
# 提交 Job，观察 status.nominatedClusterNames / status.clusterName
```

### 观察

```bash
kubectl get workload <name> -n default -o yaml
kubectl get workloads -n default --context kind-mk-worker
```

### 注意

- manager 的 quota 应约等于 worker 总容量；
- worker kubeconfig 用 Secret/ClusterProfile；
- AllAtOnce 会复制到所有 worker，Incremental 分批；
- 只允许一个 worker 最终 Admitted，其他副本会被清理。

## 22.11 Lab 11：可观测性

### 目标

把指标、事件、Visibility、KueueViz 串起来。

### 指标

```bash
kubectl -n kueue-system port-forward \
  svc/kueue-controller-manager-metrics-service 8443:8443 &
curl -k https://localhost:8443/metrics | grep -E 'kueue_(pending|admitted|evicted|cluster_queue)' | head -30
```

### 事件与条件

```bash
kubectl describe workload <name> -n default
kubectl get events -n default --sort-by=.lastTimestamp | tail -20
```

### Visibility

```bash
kubectl get --raw \
  "/apis/visibility.kueue.x-k8s.io/v1beta1/clusterqueues/<cq>/pendingworkloads" | jq .
```

### KueueViz（可选）

```bash
helm upgrade kueue oci://registry.k8s.io/kueue/charts/kueue \
  --namespace kueue-system --set enableKueueViz=true
kubectl -n kueue-system port-forward svc/kueue-kueueviz-frontend 8080:8080
```

### 练习

1. 制造一次配额耗尽，观察 `kueue_pending_workloads`；
2. 制造一次抢占，观察 `kueue_preempted_workloads_total`；
3. 用 `histogram_quantile` 算 P99 等待时间（需要样本量）。

## 22.12 Lab 12：故障演练

### 12.1 webhook 不可用

```bash
kubectl -n kueue-system scale deploy kueue-controller-manager --replicas=0
kubectl apply -f examples/kueue/quick-start-setup.yaml
# 预期：webhook 不可用，创建失败
kubectl -n kueue-system scale deploy kueue-controller-manager --replicas=1
```

### 12.2 控制器重启

```bash
kubectl -n kueue-system rollout restart deploy/kueue-controller-manager
kubectl -n kueue-system rollout status deploy/kueue-controller-manager
kubectl get workloads -A
```

观察已 Admitted 的 Workload 是否保持，pending 是否恢复。

### 12.3 finalizer 卡住

```bash
kubectl delete clusterqueue <name>
kubectl get clusterqueue <name> -o yaml
# 如果卡在 Terminating，先恢复控制器，不要 --force 删 CRD
```

### 12.4 配额突变

```bash
kubectl patch clusterqueue <name> --type=merge \
  -p '{"spec":{"stopPolicy":"HoldAndDrain"}}'
kubectl get workloads -n default -w
# 观察驱逐；完成后改回 None
```

### 12.5 验收

每次故障演练后：

```bash
kubectl get clusterqueues -o wide
kubectl get workloads -A
kubectl -n kueue-system logs deploy/kueue-controller-manager --tail=100
```

确认没有残留的 Pending/Terminating 对象和异常驱逐。

## 22.13 可选实验（Lab 13～20）

### 22.13.1 层级 Cohort 与 Cohort 级 limit

```bash
kubectl apply -f examples/kueue/hierarchical-cohort.yaml
kubectl get cohorts
kubectl get clusterqueues -o wide
```

观察：

- `org-research` 的 Cohort 级 `borrowingLimit: 8` 是否成为瓶颈；
- CQ 级 borrowingLimit 与 Cohort 级 limit 的叠加；
- 删除子 Cohort 对象后，CQ 的 `cohortName` 是否退化为隐式 Cohort（按版本行为验证）。

### 22.13.2 Admission Fair Sharing

```bash
kubectl apply -f examples/kueue/admission-fair-sharing.yaml
kubectl get localqueues -A -o wide
kubectl get localqueue queue -n team-light -o jsonpath='{.status.fairSharing}'
```

观察：

- 两个队列的历史用量/加权份额；
- 在 CQ 容量不足时，哪个队列先被尝试；
- 调整 `fairSharing.weight` 后排序是否变化。

### 22.13.3 Admission Gate

```bash
kubectl apply -f examples/kueue/admission-gate.yaml
kubectl get workload gated-job -n default -o yaml
```

观察：

- `AdmissionGated=True` 且 `QuotaReserved=False`；
- Workload 是否占用 CQ 配额（不应占用）；
- 手动移除注解中的一项 gate 后，是否恢复准入；
- 事件 `AdmissionGated` / `AdmissionGateCleared`。

### 22.13.4 Concurrent Admission

```bash
kubectl apply -f examples/kueue/concurrent-admission.yaml
kubectl get workload concurrent-job -n default -o yaml
```

观察：

- 初始 Flavor 是否是 `gpu-spot`；
- 给 `gpu-reserved` 增加配额后是否迁移；
- `status.admission.podSetAssignments[*].flavors` 的变化；
- 需要 `ConcurrentAdmission` gate，且 CQ 必须满足 BestEffortFIFO/单资源组/≤32 Flavor。

### 22.13.5 资源变换

```bash
# 1. 导出当前配置（ConfigMap 的值是一个 YAML 字符串）
kubectl -n kueue-system get cm kueue-manager-config \
  -o jsonpath='{.data.controller_manager_config\.yaml}' > kueue-config.yaml
# 2. 把 examples/config/resource-transformations.yaml 的 resources.transformations
#    合并进 kueue-config.yaml（手工编辑或用 yq），不要 apply 成第二个对象
$EDITOR kueue-config.yaml
# 3. 整体替换 ConfigMap 的键并重启
kubectl -n kueue-system create cm kueue-manager-config \
  --from-file=controller_manager_config.yaml=kueue-config.yaml \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl -n kueue-system rollout restart deploy/kueue-controller-manager
kubectl -n kueue-system rollout status deploy/kueue-controller-manager
# 4. 再 apply 只包含 CR 的示例文件（它已不再包含 Configuration）
kubectl apply -f examples/kueue/resource-transformations.yaml
```

观察：

- Pod 的 `example.com/gpumem/gpucores/gpu` 请求是否被折算成 `total-gpumem/total-gpucores`；
- `kubectl get workload -o yaml` 中 Workload 的资源请求是否只包含输出资源；
- 如果 `multiplyBy` 指定的资源缺失，outputs 是否为 0；
- `Replace` + 空 outputs 是否真的让 input 被忽略。

### 22.13.6 ProvisioningRequest

```bash
kubectl apply -f examples/kueue/provisioning-request.yaml
kubectl get admissionchecks
kubectl get provisioningrequests -A
kubectl get workloads -n default -w
```

观察：

- Workload 预留后是否出现 `ProvisioningRequest`；
- 云容量不足时 AdmissionCheck 是否进入 Retry、配额是否释放；
- `retryCount` 与 ProvisioningRequest 名字的 attempt 后缀；
- 云扩容成功后是否自动 Admitted。

没有云 autoscaler 时可以用 `check-capacity` 或模拟驱动验证状态机。

### 22.13.7 Configurable Preemption

```bash
# 需要开启 ConfigurablePreemptions gate
kubectl apply -f examples/kueue/configurable-preemption.yaml
kubectl get preemptionconfigs
kubectl get clusterqueue shared-gpu -o yaml | grep preemption-config-name
```

观察：

- Classic `spec.preemption` 全关时，规则是否仍能授权抢占；
- 去掉 victim 的 `workload-type=batch` 标签后，规则是否失效；
- 把 scope 改成 `AnyClusterQueue` 后的影响；
- 对称规则是否会造成 A↔B 抢占循环（不要在生产尝试）。

### 22.13.8 Fair Sharing 抢占

```bash
# 1. 导出、合并 examples/config/fair-sharing-preemption.yaml 的 fairSharing 块
kubectl -n kueue-system get cm kueue-manager-config \
  -o jsonpath='{.data.controller_manager_config\.yaml}' > kueue-config.yaml
$EDITOR kueue-config.yaml
# 2. 整体替换并重启
kubectl -n kueue-system create cm kueue-manager-config \
  --from-file=controller_manager_config.yaml=kueue-config.yaml \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl -n kueue-system rollout restart deploy/kueue-controller-manager
kubectl -n kueue-system rollout status deploy/kueue-controller-manager
# 3. 再 apply 只包含 CR 的示例文件
kubectl apply -f examples/kueue/fair-sharing-preemption.yaml
```

观察：

- `team-a` 借用后，`team-b` 的新作业是否触发抢占；
- 被抢占者的 `Evicted/Preempted` 条件与 `schedulingStats`；
- 两种 strategy 对 victim 集合的影响；
- 关闭 Fair Sharing 后，同样负载是否还发生抢占。

## 22.14 清理与复现

```bash
kubectl delete jobs --all -n default
kubectl delete workloads --all -n default
kubectl delete localqueues --all -n default
kubectl delete clusterqueues --all
kubectl delete cohorts --all
kubectl delete resourceflavors --all
kubectl delete admissionchecks --all
kubectl delete topologies --all
kubectl delete -f examples/kueue/dra.yaml --ignore-not-found
kind delete cluster --name kueue-lab
```

如果只想保留集群：

```bash
bash scripts/verify_all.sh   # 只验证 MiniKueue/文档，不触碰集群
```

## 22.15 实验清单

| Lab | 主题 | 难度 | 依赖 |
|---|---|---|---|
| 1 | 安装与验收 | 低 | kind |
| 2 | 最小生命周期 | 低 | kind |
| 3 | FIFO 对比 | 低 | kind |
| 4 | Cohort 借用 | 中 | kind |
| 5 | 抢占/回收 | 中 | kind |
| 6 | selector | 低 | kind |
| 7 | AdmissionCheck | 中 | kind |
| 8 | TAS | 高 | 多节点 kind |
| 9 | DRA | 高 | DRA driver |
| 10 | MultiKueue | 高 | 双集群 |
| 11 | 可观测性 | 中 | kind + Prometheus（可选） |
| 12 | 故障演练 | 中 | kind |
| 13 | 层级 Cohort / limit | 中 | kind |
| 14 | Admission Fair Sharing | 中 | kind + gate |
| 15 | Admission Gate | 低 | kind |
| 16 | Concurrent Admission | 高 | kind + gate |
| 17 | Resource Transformations | 中 | kind + Configuration |
| 18 | ProvisioningRequest | 高 | kind + autoscaler/gate |
| 19 | Configurable Preemption | 高 | kind + gate |
| 20 | Fair Sharing 抢占 | 中 | kind + Configuration |

建议顺序：1 → 2 → 3 → 6 → 4 → 5 → 7 → 11 → 12，最后再做 8/9/10。

## 22.16 本章小结

- 真实集群实验能把"条件/事件/指标"与对象状态对上，是读文档无法替代的。
- Lab 2/3/6 是最小闭环：安装、生命周期、两种 FIFO、selector。
- Lab 4/5 是配额与抢占的核心；Lab 7 是两阶段准入。
- Lab 8/9/10 依赖额外环境（多节点、DRA driver、双集群），可以按需进行。
- Lab 12 的故障演练要在非生产集群做，重点是观察 finalizer、重启恢复和 StopPolicy。
- Lab 13～20 是可选的进阶实验，覆盖层级 Cohort/limit、AFS、admission gate、Concurrent Admission、资源变换、ProvisioningRequest、Configurable Preemption 和 Fair Sharing 抢占；需要相应 feature gate。
- Lab 6 末尾的"纳管边界实验"用 `examples/kueue/enforce-management.yaml` 验证默认 LocalQueue、默认 WorkloadPriorityClass 与 ValidatingAdmissionPolicy 的执行顺序。
- 每次实验后清理对象，避免残留影响下一次观察。

---

[← 上一章：平台队列与配额设计模式](21-platform-design-patterns.md) | [返回目录](../README.md) | [下一章：附录 →](23-appendix.md)
