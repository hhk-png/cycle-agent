# 第 25 章 · 生态集成实战

第 8 章讲了集成层的职责和契约。本章把它落到具体框架：**JobSet、KubeRay、Kubeflow Training Operator、LeaderWorkerSet、裸 Pod、Deployment/StatefulSet，以及自研框架**。每个小节都给出对象关系、清单骨架、暂停/放行机制、常见坑和验收方法。

> 版本提示：各框架的字段名和暂停机制随版本变化。本章的 YAML 用于说明**集成关系**，不保证在任意版本可直接 apply。上线前用目标版本的 CRD schema 和 Kueue 集成文档核对。

## 25.1 先复习集成契约

一个合格的 Kueue 集成要满足九条：

| 责任 | 失败表现 |
|---|---|
| 发现带 `kueue.x-k8s.io/queue-name` 的对象 | 作业不受管理，或误管系统作业 |
| 把 Pod 模板映射成 PodSet | 资源汇总错误，gang 语义被破坏 |
| 准入前暂停用户对象 | Pod 提前创建，配额账本和实际不一致 |
| 准入后放行 | 作业永远不开始 |
| 驱逐后暂停/清理 | 被抢占的作业继续跑 |
| 完成/失败同步 | Workload 永远不 Finished，配额不释放 |
| 资源变化经 `reclaimablePods` 报告 | 缩容后仍按旧请求计费 |
| 注入 Flavor 的 nodeSelector/tolerations | Pod 落到错误节点或无法调度 |
| 幂等 reconcile | 重复 Workload、重复 Pod、状态抖动 |

后面每个框架都围绕这张表展开。

## 25.2 batch/v1 Job：最小闭环

### 对象关系

```text
Job (parallelism=N, suspend)
  └── Workload (1 个 PodSet, count=N)
        └── Admitted 后 suspend=false
              └── Job 控制器创建 N 个 Pod
```

### 清单

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: train
  namespace: team-a
  labels:
    kueue.x-k8s.io/queue-name: team-a-queue
spec:
  parallelism: 8
  completions: 8
  template:
    spec:
      containers:
      - name: worker
        image: example/trainer:latest
        resources:
          requests:
            cpu: "4"
            memory: 16Gi
            nvidia.com/gpu: "1"
      restartPolicy: Never
```

用户通常不需要写 `suspend: true`；集成控制器在创建 Workload 后会设置。手工写 `suspend: false` 并和集成争夺这个字段，会导致 Pod 提前创建。

### 关键点

- `count = parallelism`；`completions` 影响完成判定，但 Kueue 的资源汇总是按 Pod 模板 × parallelism；
- `backoffLimit`、`activeDeadlineSeconds` 仍由 Job 控制器负责，Kueue 不接管重试；
- Job 完成 → Workload `Finished=True` → 配额释放；
- Job 被删除 → Workload 由 ownerReference 回收；
- 不要手工改已准入 Workload 的 `podSets`。

### 验收

```bash
kubectl get workload train -n team-a -o yaml
# QuotaReserved=True, Admitted=True
kubectl get job train -n team-a -o jsonpath='{.spec.suspend}'
# false
kubectl get pods -n team-a
# 8 个 Pod
```

## 25.3 JobSet：多 Job 编排

JobSet 用一个对象管理多个 replicatedJob，适合"coordinator + workers"或"参数服务器 + worker"。

### 对象关系

```text
JobSet
  ├── replicatedJob "workers"  replicas=4  -> PodSet workers (count=4)
  └── replicatedJob "leader"   replicas=1  -> PodSet leader  (count=1)
```

Kueue 集成通常把每个 replicatedJob 映射成一个 PodSet，并整体准入。

### 清单骨架

```yaml
apiVersion: jobset.x-k8s.io/v1alpha2
kind: JobSet
metadata:
  name: distributed-train
  namespace: team-a
  labels:
    kueue.x-k8s.io/queue-name: team-a-queue
spec:
  replicatedJobs:
  - name: workers
    replicas: 4
    template:
      spec:
        parallelism: 1
        completions: 1
        template:
          spec:
            containers:
            - name: worker
              image: example/trainer:latest
              resources:
                requests:
                  cpu: "8"
                  nvidia.com/gpu: "1"
            restartPolicy: Never
  - name: leader
    replicas: 1
    template:
      spec:
        parallelism: 1
        completions: 1
        template:
          spec:
            containers:
            - name: leader
              image: example/trainer:latest
              resources:
                requests:
                  cpu: "4"
            restartPolicy: Never
```

暂停字段由 JobSet 版本决定：有的版本用 JobSet 级 `suspend`，有的通过每个 Job 的 `suspend`。集成控制器负责在 Workload 未 Admitted 前阻止 Pod 创建。

### 常见坑

- **只暂停了部分 replicatedJob**：workers 已启动、leader 还在等，gang 语义被破坏；
- **replicas 与 PodSet count 不一致**：资源汇总少算；
- **successPolicy 与 Kueue 的 Finished 判定不一致**：Workload 已完成但 JobSet 还在重试；
- **网络拓扑**：JobSet 常和 TAS/MPI 一起用，PodSet 的拓扑注解要写在 Pod 模板上。

### 验收

```bash
kubectl get workload distributed-train -n team-a -o yaml
kubectl get jobsets -n team-a
kubectl get jobs -n team-a
```

## 25.4 KubeRay：RayJob / RayCluster

Ray 的作业形态有两类：

- `RayJob`：提交一个 entrypoint，跑完即结束；
- `RayCluster`：长期集群，RayJob 或外部客户端提交任务。

### 对象关系

```text
RayJob
  ├── head PodSet (count=1)
  └── workerGroup PodSets (每个 group 一个 PodSet)
```

Kueue 集成把 head 和 worker groups 映射成 PodSet，整体准入。

### 清单骨架

```yaml
apiVersion: ray.io/v1
kind: RayJob
metadata:
  name: ray-train
  namespace: team-a
  labels:
    kueue.x-k8s.io/queue-name: team-a-queue
spec:
  entrypoint: python /home/ray/train.py
  shutdownAfterJobFinishes: true
  rayClusterSpec:
    headGroupSpec:
      rayStartParams: {}
      template:
        spec:
          containers:
          - name: ray-head
            image: rayproject/ray:2.40.0
            resources:
              requests:
                cpu: "4"
                memory: 8Gi
    workerGroupSpecs:
    - groupName: gpu-workers
      replicas: 4
      minReplicas: 0
      maxReplicas: 8
      template:
        spec:
          containers:
          - name: ray-worker
            image: rayproject/ray:2.40.0
            resources:
              requests:
                cpu: "8"
                memory: 32Gi
                nvidia.com/gpu: "1"
```

### 关键点

- **head 和 worker 资源差异大**：不要把它们合成一个 PodSet；
- **Ray 自动扩缩**：`minReplicas/maxReplicas` 会影响实际 Pod 数，Kueue 的配额账本要和集成报告的 `reclaimablePods` 对齐；
- **RayCluster 长期运行**：没有天然"完成"，要明确什么算 Finished（删除、缩容到 0、还是外部标记）；
- **Dashboard/Service**：不是 Kueue 管的事，但要考虑它们是否占用节点资源；
- **Suspend**：RayJob 的暂停字段随版本不同，集成负责；用户不要手工重启 RayCluster 绕过准入。

### 验收

```bash
kubectl get workload ray-train -n team-a -o yaml
kubectl get rayjobs -n team-a
kubectl get pods -n team-a -l ray.io/cluster
```

## 25.5 Kubeflow Training Operator

Training Operator 提供 PyTorchJob、TFJob、MPIJob、XGBoostJob、JAXJob 等。它们的共同结构是"角色 + 副本数"。

### 对象关系

```text
PyTorchJob
  ├── Master PodSet (count=1)
  └── Worker PodSet (count=N)

MPIJob
  ├── Launcher PodSet (count=1)
  └── Worker PodSet (count=N)
```

### PyTorchJob 骨架

```yaml
apiVersion: kubeflow.org/v1
kind: PyTorchJob
metadata:
  name: pytorch-train
  namespace: team-a
  labels:
    kueue.x-k8s.io/queue-name: team-a-queue
spec:
  pytorchReplicaSpecs:
    Master:
      replicas: 1
      template:
        spec:
          containers:
          - name: pytorch
            image: example/pytorch:latest
            resources:
              requests:
                cpu: "8"
                nvidia.com/gpu: "1"
    Worker:
      replicas: 4
      template:
        spec:
          containers:
          - name: pytorch
            image: example/pytorch:latest
            resources:
              requests:
                cpu: "8"
                nvidia.com/gpu: "1"
```

### MPIJob 骨架

```yaml
apiVersion: kubeflow.org/v1
kind: MPIJob
metadata:
  name: mpi-train
  namespace: team-a
  labels:
    kueue.x-k8s.io/queue-name: team-a-queue
spec:
  slotsPerWorker: 8
  mpiReplicaSpecs:
    Launcher:
      replicas: 1
      template:
        spec:
          containers:
          - name: launcher
            image: example/mpi:latest
            resources:
              requests:
                cpu: "2"
    Worker:
      replicas: 4
      template:
        spec:
          containers:
          - name: worker
            image: example/mpi:latest
            resources:
              requests:
                cpu: "8"
                nvidia.com/gpu: "1"
```

### 常见坑

- **角色名/副本数变化**：集成要重新计算 PodSet，不能只更新一次；
- **gang**：所有角色必须一起准入；只启动 Master 没有意义；
- **MPI 的 slotsPerWorker**：它影响进程数，不一定改变 Pod 资源请求；配额按 Pod 请求算；
- **暂停机制**：Training Operator 的 suspend 字段随版本不同，以集成文档为准；
- **完成判定**：有的框架会保留 Pod 用于日志，Kueue 的 Finished 和 Pod 退出不是同一件事。

### 验收

```bash
kubectl get workload pytorch-train -n team-a -o yaml
kubectl get pytorchjobs -n team-a
kubectl get pods -n team-a -l training.kubeflow.org/job-name
```

## 25.6 LeaderWorkerSet（LWS）

LWS 用"leader + worker group"表达多节点推理/训练，适合需要稳定身份和组的场景。

### 对象关系

```text
LeaderWorkerSet
  └── group (size=N)
        ├── leader Pod
        └── worker Pods (N-1)
```

Kueue 集成通常把 leader 和 worker 映射成两个 PodSet，或把整个 group 作为一个 PodSet，取决于版本。

### 清单骨架

```yaml
apiVersion: leaderworkerset.x-k8s.io/v1
kind: LeaderWorkerSet
metadata:
  name: lws-inference
  namespace: team-a
  labels:
    kueue.x-k8s.io/queue-name: team-a-queue
spec:
  replicas: 2
  leaderWorkerTemplate:
    size: 4
    leaderTemplate:
      spec:
        containers:
        - name: leader
          image: example/inference:latest
          resources:
            requests:
              cpu: "4"
              nvidia.com/gpu: "1"
    workerTemplate:
      spec:
        containers:
        - name: worker
          image: example/inference:latest
          resources:
            requests:
              cpu: "8"
              nvidia.com/gpu: "1"
```

### 关键点

- **group 语义**：leader 和 worker 必须一起启动；配额要按整个 group 汇总；
- **长期运行**：推理服务没有"完成"，配额释放依赖缩容/删除；
- **滚动更新**：LWS 的 rollout 可能创建额外 Pod，配额账本要能反映；
- **拓扑**：多节点推理对网络敏感，考虑 TAS 和 Flavor 的网络维度。

## 25.7 Pod、Deployment、StatefulSet

### 裸 Pod

Pod 没有 suspend 字段，集成通常用 **scheduling gate** 阻止调度：

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: single
  namespace: team-a
  labels:
    kueue.x-k8s.io/queue-name: team-a-queue
spec:
  schedulingGates:
  - name: kueue.x-k8s.io/admission
  containers:
  - name: main
    image: busybox
    command: ["sleep", "3600"]
    resources:
      requests:
        cpu: "1"
```

准入后移除 gate。注意 gate 移除时机：过早会让 Pod 在配额预留前被调度。

### Deployment / StatefulSet

它们没有"作业完成"概念。特别注意：**Deployment 不是"一个 Deployment 一个 Workload"**，Kueue 通过 Pod 模板注入 + Pod 集成逐 Pod 管理；StatefulSet 在 v0.20 的集成以 Plain Pod Group 管理成员，**官方明确不支持 StatefulSet 的扩缩操作**，不要用 `replicas=0` 作为暂停机制。上线前必须定义：

- 什么算 Finished？
- 缩容时如何释放配额？
- 滚动更新期间新旧 Pod 是否同时计入？
- 副本数变化如何通过 `reclaimablePods` 或受支持的更新路径报告？

如果回答不了，不要把长期服务放进批处理队列；它会把"配额释放"变成永远不收敛的状态。推理/弹性负载的完整接入边界见[第 36 章](36-inference-elastic-workloads.md)。

## 25.8 自研框架集成

### 推荐状态机

```text
观察 MyJob + Workload
  ├── Workload 不存在
  │     → 构造 Workload（确定性名称 + ownerReference）
  │     → 暂停 MyJob
  │     → 创建 Workload
  ├── Workload 存在，spec 与 MyJob 不一致
  │     → 只更新允许变化的字段（count/priority）
  ├── QuotaReserved=False / Admitted=False
  │     → 确保 MyJob 暂停
  ├── Admitted=True
  │     → 注入 Flavor 的 nodeSelector/tolerations
  │     → 取消暂停
  ├── Evicted=True
  │     → 暂停、清理 Pod
  └── MyJob 完成/失败
        → 标记 Workload Finished（或由 Pod 状态推导）
```

### 最小 reconciler

```go
func reconcileMyJob(ctx context.Context, job *MyJob) error {
    wl := buildWorkload(job) // deterministic name, ownerReference
    if err := ensureWorkload(ctx, wl); err != nil {
        return err
    }
    switch {
    case admitted(wl):
        applyFlavorInjection(job, wl)
        job.Spec.Suspend = false
    case evicted(wl):
        job.Spec.Suspend = true
        cleanupPods(ctx, job)
    default:
        job.Spec.Suspend = true
    }
    return updateJob(ctx, job)
}
```

真实实现还要处理：

- status 只写自己拥有的字段；
- 并发 reconcile 的幂等；
- 作业完成后的 Workload 清理；
- 资源变化的 `reclaimablePods`；
- 失败重试和退避。

### 测试矩阵

| 测试 | 目的 |
|---|---|
| Workload 创建幂等 | 重复 reconcile 不产生重复对象 |
| 暂停/放行 | Admitted 前无 Pod，Admitted 后有 Pod |
| 驱逐 | Evicted 后 Pod 被清理，Workload 重排队 |
| 完成 | Job 完成后 Workload Finished，配额释放 |
| 资源变化 | 缩容后 `reclaimablePods` 生效 |
| Flavor 注入 | nodeSelector/tolerations 出现在 Pod 上 |
| 并发 | 两个 reconciler 不会互相覆盖 |
| 升级 | v1beta1/v1beta2 字段兼容 |

## 25.9 集成验收清单

- [ ] 带 queue-name 的作业能创建 Workload；
- [ ] 没有 queue-name 的作业不被误管；
- [ ] Admitted 前不创建 Pod；
- [ ] Admitted 后 Pod 正常创建；
- [ ] 多 PodSet 整体准入，不出现半启动；
- [ ] Evicted 后 Pod 被清理，Workload 重排队；
- [ ] 完成后 Workload Finished，配额释放；
- [ ] Flavor 的 nodeSelector/tolerations 被注入；
- [ ] 缩容通过 `reclaimablePods` 报告；
- [ ] 重复 reconcile 不产生重复对象；
- [ ] 指标/事件/条件能解释"为什么没开始"。

## 25.10 常见坑总表

| 坑 | 后果 | 修复 |
|---|---|---|
| 只暂停部分角色 | 半启动，gang 破坏 | 整体暂停，等 Admitted 再放行 |
| Workload 名称不确定 | 重复 Workload | 确定性名称 + ownerReference |
| 修改已准入 PodSet | 账本漂移 | 只改允许字段，缩容走 reclaimablePods |
| 忽略 Flavor 注入 | Pod 落错节点 | 在放行前注入 nodeSelector/tolerations |
| 不处理驱逐 | 被抢占作业继续跑 | 监听 Evicted，暂停并清理 |
| 长期服务无 Finished | 配额永不释放 | 定义缩容/删除语义 |
| 框架暂停字段混用 | 集成与用户互相覆盖 | 只让集成管理 suspend |
| 不做并发幂等 | 重复创建/覆盖 | 乐观并发 + 确定性 key |

## 25.11 本章小结

- batch/Job 是最小闭环；JobSet、KubeRay、Kubeflow、LWS 的核心都是"角色 → PodSet → 整体准入"。
- 多 PodSet 作业必须整体暂停、整体放行，不能只启动一部分。
- 长期运行对象要定义"什么算 Finished"，否则配额永远不会释放。
- 自研集成的核心是：幂等创建 Workload、正确暂停/放行、监听条件、维护账本一致。
- 集成验收要覆盖暂停、放行、驱逐、完成、资源变化、Flavor 注入和并发。
- 框架字段随版本变化，生产前以目标版本 CRD/Kueue 集成文档为准。

---

[← 上一章：API 迁移与版本升级](24-api-migration-upgrade.md) | [返回目录](../README.md) | [下一章：端到端案例 →](26-end-to-end-case-study.md)
