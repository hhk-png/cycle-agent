# 第 32 章 · 高可用、灾备与一致性

> 队列系统一旦成为平台的准入闸门，它的可用性就等同于"作业能不能提交"。本章讨论 Kueue 控制面的故障域、HA 拓扑、备份恢复顺序、升级回滚和可演练的故障场景。

## 32.1 先画故障域

Kueue 不是单个进程，而是"控制器 + webhook + CRD + API server/etcd + 被管理的作业控制器"的组合。故障域至少包括：

```text
用户 kubectl
   │
   ▼
kube-apiserver ────── etcd
   │                    │
   ├── kueue-webhook    │  CRD 对象（Workload/CQ/LQ/...）
   │                    │
   └── kueue-controller-manager
          │
          ├── QueueManager / Cache / Scheduler（进程内）
          ├── Job/JobSet/Ray/Kubeflow 集成控制器
          └── AdmissionCheck 控制器（ProvisioningRequest、MultiKueue 等）
```

| 故障 | 直接影响 | 是否阻断新作业 | 是否影响已运行作业 |
|---|---|---|---|
| controller-manager 挂 | 不再准入/驱逐/更新状态 | 是 | 通常否（Pod 已在跑） |
| webhook 挂 | 创建/更新 Kueue 对象失败 | 是（取决于 failurePolicy） | 否 |
| apiserver/etcd 挂 | 所有 API 操作失败 | 是 | Pod 仍运行，但无法上报状态 |
| kube-scheduler 挂 | Pod 不被调度 | 已准入也无法运行 | 是 |
| Job 集成控制器挂 | Workload 不创建/不暂停/不清理 | 是 | 部分 |
| AdmissionCheck 控制器挂 | Workload 停在 QuotaReserved | 是 | 已 Admitted 的不受影响 |

关键结论：**Kueue HA 不等于平台 HA**。只给 controller-manager 加副本，不能解决 webhook、etcd、Job 集成或 AdmissionCheck 控制器的单点。

## 32.2 控制器高可用

### 32.2.1 多副本与 leader election

Kueue 的 controller-manager 支持多副本，但同一时刻只有一个 leader 执行 reconcile。其余副本热备。部署要点：

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: kueue-controller-manager
  namespace: kueue-system
spec:
  replicas: 2
  template:
    spec:
      affinity:
        podAntiAffinity:
          requiredDuringSchedulingIgnoredDuringExecution:
            - labelSelector:
                matchLabels:
                  control-plane: controller-manager
              topologyKey: kubernetes.io/hostname
      topologySpreadConstraints:
        - maxSkew: 1
          topologyKey: topology.kubernetes.io/zone
          whenUnsatisfiable: ScheduleAnyway
          labelSelector:
            matchLabels:
              control-plane: controller-manager
```

同时要检查：

- `leaderElection` 的 lease 名称/namespace；
- `--leader-elect-resource-lock`（新版本默认 leases）；
- PodDisruptionBudget；
- 反亲和/拓扑打散；
- 资源 requests/limits，避免 leader 因 OOM 频繁切换。

### 32.2.2 leader 切换时会发生什么

```text
旧 leader 失联
  → lease 过期
  → 新 leader 获取 lease
  → informer 重新同步
  → QueueManager/Cache 重建
  → 重新计算可准入集合
```

Kueue 的调度状态主要来自 CRD/Workload status，不在进程内持久化；新 leader 会从 API server 重建 Cache。需要注意：

- 切换期间不产生新的准入决策；
- 已在跑作业不受影响；
- 如果 API server 也不可用，新 leader 无法重建，平台整体阻塞；
- 大量对象时重建有时间，切换后短期 pending 可能上升。

### 32.2.3 webhook 高可用

webhook 通常是 controller-manager 的一部分（或独立 deployment）。它的故障策略取决于 `failurePolicy`：

| failurePolicy | webhook 不可用时 | 适用 |
|---|---|---|
| `Fail` | 创建/更新被拒绝 | 生产默认，保证非法对象进不来 |
| `Ignore` | 请求放行 | 可用性优先，但可能写入非法对象 |

生产建议：

- 至少 2 副本 + PDB；
- 证书轮换自动化；
- 监控 webhook 延迟和失败率；
- 不要把 webhook 的 `timeoutSeconds` 设得过大；
- 升级时先确保新 webhook 就绪，再滚动控制器。

## 32.3 一致性、幂等与状态归属

### 32.3.1 单一事实来源

Kueue 的事实来源是 API server 中的对象：

- Workload 的 `spec` 由 Job 集成创建/维护；
- `status` 由 Kueue 控制器和 AdmissionCheck 控制器更新；
- Job 的 `spec.suspend` 由 Job 集成控制器根据 Workload 状态修改；
- 配额账本（Cache）是**进程内派生状态**，不持久化。

因此：

- 重启后 Cache 从 Workload status 重建；
- 不要手工编辑 `status` 来"修复"队列；
- 不要手工改 `spec.podSets` 来绕过配额；
- 任何手工修改都可能被下一次 reconcile 覆盖。

### 32.3.2 幂等

reconcile 可能重复执行，控制器必须幂等：

```text
observe(workload)
  if workload already admitted: return
  if quota already reserved: continue checks
  else: reserve
```

常见不幂等错误：

- 每次 reconcile 都创建新的 ProvisioningRequest；
- 每次 reconcile 都向 Job 追加 annotation；
- 每次 reconcile 都递减配额；
- 用时间戳而不是状态判断"是否已处理"。

### 32.3.3 乐观并发

API server 用 `resourceVersion` 做乐观并发。控制器更新 status 时要处理冲突：

```text
update status
  → 409 Conflict
  → 重新 get
  → 重新计算
  → retry
```

不要用 `--force` 或直接覆盖整个对象来"解决"冲突。

### 32.3.4 finalizer

Kueue/Job 集成可能给 Workload 加 finalizer，确保删除前清理：

- 释放配额；
- 删除 ProvisioningRequest；
- 通知 MultiKueue worker；
- 更新 Job 状态。

finalizer 卡住时，先恢复对应控制器，而不是强删。强删会让 Cache 与真实对象不一致，后续重建时可能出现"配额泄漏"或"重复准入"。

## 32.4 备份

### 32.4.1 备份什么

| 类别 | 内容 | 频率 |
|---|---|---|
| CRD | Kueue 所有 CRD 定义 | 每次升级前 |
| 配置 | `Configuration`、feature gate、controller flags | 每次变更 |
| 队列对象 | ClusterQueue/LocalQueue/Cohort/ResourceFlavor/Topology | 每日 + 变更前 |
| 策略对象 | AdmissionCheck/ProvisioningRequestConfig/PreemptionConfig | 每日 |
| RBAC | ClusterRole/RoleBinding/ServiceAccount | 变更前 |
| 证书 | webhook cert/key、CA | 轮换前 |
| 凭据 | MultiKueue secret、云凭据 | 变更前（加密） |
| 状态 | Workload/Job 状态 | 视 RPO；通常不备份运行态 |

### 32.4.2 etcd 快照

```bash
ETCDCTL_API=3 etcdctl snapshot save /backup/etcd-$(date +%F-%H%M).db \
  --endpoints=https://127.0.0.1:2379 \
  --cacert=/etc/kubernetes/pki/etcd/ca.crt \
  --cert=/etc/kubernetes/pki/etcd/server.crt \
  --key=/etc/kubernetes/pki/etcd/server.key

etcdctl snapshot status /backup/etcd-$(date +%F-%H%M).db --write-out=table
```

注意：

- 快照是集群级，不是 Kueue 级；
- 恢复 etcd 是重操作，会影响整个集群；
- 托管 Kubernetes 通常不暴露 etcd，需用云厂商备份或 Velero。

### 32.4.3 对象级备份

```bash
kubectl get clusterqueues,localqueues,cohorts,resourceflavors,topologies,admissionchecks \
  -A -o yaml > kueue-objects-$(date +%F).yaml

kubectl -n kueue-system get configmap kueue-manager-config -o yaml \
  > kueue-config-$(date +%F).yaml
```

对象级备份适合跨集群迁移和 GitOps 对账；不适合高频状态备份。

### 32.4.4 恢复顺序

```text
1. CRD
2. Namespace / RBAC / ServiceAccount
3. Configuration / feature gates
4. ResourceFlavor / Topology（TopologyDomain 由节点标签推导，不是独立 CRD）
5. Cohort（从根到叶）
6. ClusterQueue（引用 Cohort/Flavor）
7. AdmissionCheck / ProvisioningRequestConfig / PreemptionConfig
8. LocalQueue
9. 控制器/webhook
10. Job/Workload（按需）
```

顺序错了会出现"引用不存在"的校验失败。恢复后要做：

```bash
kubectl get clusterqueues
kubectl get localqueues -A
kubectl auth can-i --list --as=system:serviceaccount:kueue-system:kueue-controller-manager
```

并提交一个测试 Job，确认端到端准入。

### 32.4.5 备份一致性：先定 RPO，再决定是否 quiesce

Kueue 的状态不是单一事务写：Workload 的 `QuotaReserved`、`Admitted`、`status.admission`、Cache（内存）和外部 Job 的 suspend 状态可能在不同时刻落盘。备份/恢复时要回答四个问题：

| 问题 | 选项 | 影响 |
|---|---|---|
| 备份时是否停止准入？ | quiesce / 在线快照 | quiesce 一致性最好，但有停机成本 |
| in-flight 的 `QuotaReserved` 是否保留？ | 保留 / 回滚为 Pending | 保留可能重复占配额；回滚可能重复准入 |
| Cache 是否备份？ | 不备份，重启重建 / 备份 | Cache 是派生状态，通常从 Workload 重建 |
| 外部资源（PR、DRA、远端副本）是否一起备份？ | 是 / 否 | 否会留下孤儿 |

推荐：

- **RPO=0 的强一致恢复**：维护窗口内停止 Job 提交和 Kueue 准入（CQ `stopPolicy: Hold`），等 in-flight Workload 收敛后再备份；
- **在线备份**：接受 RPO>0，恢复后把所有 `QuotaReserved/Admitted` 的 Workload 视为"待对账"，先重建 Cache，再逐项核对 Job/Pod/PR 是否存在；
- **不要把 Cache 当事实来源**。它可以从 Workload 的 admission 重建；真正要保的是 CRD 对象和外部资源的映射关系。

### 32.4.6 恢复后的外部资源对账

恢复 Kueue 对象不等于恢复世界。至少要对账：

| 外部资源 | 风险 | 对账动作 |
|---|---|---|
| Job/Pod | Workload 说 Admitted，但 Job 已被删/已 suspend | 按 UID/标签查找；不存在则把 Workload 置为 Pending 或删除 |
| ProvisioningRequest | PR 已 Ready，但 Workload 已回滚 | 删除孤儿 PR；确认节点池不会被重复扩容 |
| DRA ResourceClaim | claim 已绑定，但 Workload 重建 | 按 claim 的 owner/标签核对；避免重复分配 |
| MultiKueue 远端副本 | manager 恢复后重复提名，worker 上已有旧副本 | 按 origin/UID 清理陈旧副本；先确认 worker 健康 |
| 本地队列 status | 旧的 `pendingWorkloads`/`admittedWorkloads` 与对象不符 | 等控制器 reconcile；不要手改 status |

对账脚本的骨架：

```bash
# Workload 说 Admitted，但 Job 不存在
kubectl get workloads -A -o json |
jq -r '.items[] | select(.status.conditions[]? | select(.type=="Admitted" and .status=="True"))
       | [.metadata.namespace, .metadata.name] | @tsv' |
while IFS=$'\t' read -r ns name; do
  kubectl -n "$ns" get job "$name" >/dev/null 2>&1 || echo "orphan workload: $ns/$name"
done
```

恢复演练要把"对象恢复 + 外部对账 + 提交测试 Job + 观察一个完整生命周期"作为验收，而不是只看 `kubectl get cq` 是否 Active。

## 32.5 灾备拓扑

### 32.5.1 RTO/RPO 先于技术选型

| 场景 | 建议 RTO | 建议 RPO | 方案 |
|---|---|---|---|
| 单集群控制器故障 | < 1 分钟 | 0 | 多副本 + leader election |
| 单集群 webhook 故障 | < 5 分钟 | 0 | 多副本 + PDB |
| 控制面 namespace 误删 | < 1 小时 | 最近一次变更 | GitOps + 对象备份 |
| 集群级灾难 | < 4 小时 | < 15 分钟 | 多集群 + etcd/云备份 |
| 区域灾难 | < 24 小时 | < 1 小时 | 异地集群 + MultiKueue/联邦 |

RTO/RPO 是业务决定的，不是 Kueue 决定的。先和平台/业务确认，再设计。

### 32.5.2 同集群 HA

```text
3 控制面节点
  ├── apiserver × 3
  ├── etcd × 3
  └── kueue-controller-manager × 2（leader + 热备）
```

要点：

- 控制面跨可用区；
- webhook 至少 2 副本；
- 监控 leader 切换次数；
- 定期演练 kill leader。

### 32.5.3 多集群与 MultiKueue

MultiKueue 的 manager 集群是新的故障域：

- manager 挂：不能提名/分发，但 worker 上已运行的作业继续；
- worker 挂：manager 需要重新提名，已选中副本可能失败；
- 网络分区：manager 与 worker 状态不一致，可能出现重复副本；
- 凭据泄漏/过期：无法访问 worker。

DR 设计要回答：

- manager 是否也要多副本/多区域？
- worker 故障时作业是否自动在其他 worker 重建？
- 状态以 manager 还是 worker 为准？
- 恢复后如何清理重复副本？

### 32.5.4 运行态与声明态

灾备通常只恢复"声明态"（CQ/LQ/Flavor/配置），不恢复"运行态"（Workload/Job）。原因是：

- 运行态恢复后，Pod 可能已经在别的集群运行；
- 重复的 Workload 会导致重复准入；
- Job 的 UID/状态与旧集群绑定。

更常见的做法是：

1. 恢复队列与策略；
2. 让业务重新提交作业；
3. 用 MultiKueue/Karmada 等做跨集群调度；
4. 对关键作业做 checkpoint/重启，而不是试图恢复进程状态。

## 32.6 升级与回滚

升级顺序与第 24 章一致，这里只强调 HA 视角：

```text
1. 备份 CRD/配置/对象
2. 在 staging 做 CRD conversion round-trip
3. 先升级 CRD（served/storage）
4. 再滚动升级 controller-manager（maxUnavailable=0）
5. 观察 leader 切换、webhook 错误率、pending、驱逐
6. 再升级 Job 集成/AdmissionCheck 控制器
7. 最后清理旧版本字段/gate
```

回滚前要确认：

- CRD 是否已经写入新版本对象；
- conversion webhook 是否还能读旧版本；
- 新 controller 写入的 status 旧 controller 是否能理解；
- 是否有不可逆的 schema 变更。

## 32.7 故障演练矩阵

| 演练 | 方法 | 预期 | 观察 |
|---|---|---|---|
| kill leader | 删除 leader pod | 30s 内新 leader | lease、pending、事件 |
| webhook 全挂 | scale 到 0 | 创建失败（Fail） | 错误率、恢复时间 |
| etcd 延迟 | 注入网络延迟 | API 变慢、reconcile 重试 | apiserver 指标 |
| apiserver 限流 | 压测/低 QPS | 429、队列积压 | 客户端重试 |
| Job 集成挂 | scale 到 0 | 新 Job 不被暂停/创建 Workload | pending、Job 状态 |
| AdmissionCheck 挂 | scale 到 0 | Workload 停在 QuotaReserved | check 状态、配额占用 |
| 配额突变 | 改小 nominal | 新作业进不来；是否驱逐取决于策略 | Evicted/Preempted |
| 缓存不一致 | 手工改 status + 重启 | 重启后从 API 重建 | usage、重复准入 |

演练要在非生产或隔离 namespace 做，并准备好回滚。

## 32.8 Runbook

### 32.8.1 leader 频繁切换

```bash
kubectl -n kueue-system get lease
kubectl -n kueue-system logs deploy/kueue-controller-manager --tail=200
kubectl -n kueue-system get pods -o wide
```

常见原因：OOM、etcd/apiserver 延迟、网络分区、lease 配置错误、节点压力。先保 leader 稳定，再查根因。

### 32.8.2 webhook 超时

```bash
kubectl get validatingwebhookconfigurations | grep kueue
kubectl -n kueue-system get endpoints
kubectl -n kueue-system logs deploy/kueue-controller-manager --tail=200
```

短期可以把 `failurePolicy` 改为 `Ignore` 恢复可用性，但必须记录为临时变更并尽快恢复 `Fail`。

### 32.8.3 finalizer 卡住

```bash
kubectl get workload <name> -n <ns> -o jsonpath='{.metadata.finalizers}'
kubectl get events -n <ns> --sort-by=.lastTimestamp | tail -30
```

先恢复对应控制器/集成；确认没有外部资源（ProvisioningRequest、worker 副本）后再考虑移除 finalizer。

### 32.8.4 恢复后重复准入

症状：同一作业在多个集群/多个 Workload 上运行。

排查：

```bash
kubectl get workloads -A -o json |
jq -r '.items[] | [.metadata.namespace, .metadata.name, .metadata.uid, .spec.queueName] | @tsv'
```

检查：

- Job UID 是否重复；
- MultiKueue 的 nominated/selected 副本；
- 是否有旧集群的 Workload 未删除；
- manager 是否在恢复后重新提名了所有 worker。

## 32.9 MiniKueue 的视角

MiniKueue 是单进程、无持久化、无 leader election 的模拟器。它刻意不实现 HA，但保留了可扩展的接口：

- `Store` 是内存 API server，可替换为持久化实现；
- `Cache` 是派生账本，可在启动时从 Workload status 重建；
- `Engine.Tick` 是确定性协调循环，可替换为 controller-runtime 的事件驱动循环；
- `Engine.Activate/Deactivate` 对应管理员的 resume/stop。

如果要把 MiniKueue 推向"可恢复"：

1. 给 Store 加 WAL/快照；
2. 启动时从快照重建 Cache；
3. 给写操作加 resourceVersion/乐观锁；
4. 用 lease 做 leader election；
5. 给 Tick 加幂等键，避免重复提交；
6. 增加"进程被杀后重启"的恢复测试。

这些正是第 30 章从模拟器到生产控制器的一部分。

## 32.10 本章小结

- Kueue 的故障域不止 controller-manager；webhook、API server/etcd、Job 集成和 AdmissionCheck 控制器同样关键。
- 控制器多副本 + leader election 解决"控制器单点"，不解决"平台单点"。
- Cache 是派生状态，重启从对象重建；不要手工改 status。
- 备份要覆盖 CRD、配置、队列对象、RBAC、证书和凭据；恢复有严格顺序。
- 灾备先定 RTO/RPO，再选同集群 HA、对象备份或多集群方案。
- 升级/回滚要围绕 CRD conversion、feature gate 和状态兼容性设计。
- 故障演练要可重复、可观察、可回滚；MiniKueue 可作为恢复逻辑的单元测试床。

---

[← 上一章：kueuectl 与自动化运维](31-kueuectl-operations.md) | [返回目录](../README.md) | [下一章：实现补全 →](33-implementation-completion.md)
