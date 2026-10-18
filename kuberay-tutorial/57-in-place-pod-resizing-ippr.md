# 第 57 章 垂直弹性：KubeRay IPPR 与原地 Pod 扩缩

前面所有关于 autoscaler 的章节都默认一个方向：**加 Pod、删 Pod**。但很多 Ray 负载的瓶颈不是“Pod 数量不够”，而是“每个 Pod 太小”：一个 worker 只有 1 核，pending task 需要 3 核；横向扩到 3 个 Pod 能解决问题，却带来 3 份启动时间、3 份对象存储、3 份镜像拉取和更碎的调度。

In-Place Pod Resizing（IPPR）把另一条路打开：**不重建 Pod，直接把运行中的容器 CPU/内存调大**。Kubernetes 1.35 把原地 Pod 扩缩（In-Place Pod Resize）转为 stable，KubeRay 随之在 autoscaler v2 中加入 IPPR。它是 2026 年 KubeRay 最值得关注的新能力之一，也是“Ray 的弹性”从水平走向二维的分水岭。

本章先讲 Kubernetes 的原地扩缩语义，再讲 KubeRay 的 IPPR 控制回路、注解契约、决策顺序、失败回滚和生产限制，最后把 MiniKubeRay 的实现逐段拆开，并用 `examples/ippr_lab.py` 跑出可断言的数字。

> 版本基线：KubeRay v1.7.1、Ray 2.59.0、Kubernetes 1.35+。IPPR 在 KubeRay 侧仍是 **alpha**，注解形状和实现细节可能在上游 GA 前调整；生产采用前请以目标 tag 的文档和 CRD 为准。

## 57.1 为什么需要垂直弹性

### 57.1.1 横向扩容的四个成本

假设一个 Ray worker 模板是 1 CPU / 1Gi，某个阶段突然出现 3 CPU 的 pending demand。水平方案创建 3 个新 Pod：

1. **启动成本**：每个 Pod 都要拉镜像、启动 raylet、向 GCS 注册；Ray 的节点注册不是瞬时的。
2. **调度成本**：3 个 Pod 可能落到 3 个节点，产生跨节点对象传输。
3. **碎片成本**：每个 Pod 自带一份 Raylet/对象存储/共享内存预留，资源利用率下降。
4. **机会成本**：集群可能没有 3 个可调度位置，但有一个节点还剩 3 核，只是没有“整 Pod”形状。

垂直方案只改一个正在运行的 Pod：

- 容器不重启，Raylet 进程不重启；
- 不需要重新拉镜像、重新注册节点；
- 已有的对象、actor、连接都保留；
- 调度器只需在一个节点上腾出 cgroup 配额（前提是节点还有余量）。

### 57.1.2 IPPR 不是 HPA/VPA 的替代品

三者容易混淆：

| 机制 | 决策对象 | 触发信号 | 是否重建 Pod | 谁来做 |
|---|---|---|---|---|
| HPA | 副本数 | CPU/内存/自定义指标 | 否（加/删 Pod） | Kubernetes HPA |
| VPA | Pod 资源 | 历史用量推荐 | 默认重建（`Recreate`），新版可 `InPlaceOrRecreate` | VPA |
| KubeRay autoscaler | worker `replicas` | Ray 的 pending demand | 否（加/删 Pod） | autoscaler sidecar |
| **IPPR** | **运行中 Pod 的 requests** | **Ray 的 pending demand** | **否（原地 resize）** | **autoscaler v2 + kubelet** |

关键区别是**信号**：IPPR 看的是 Ray 视角的“还差多少资源才能放下 pending task/actor”，不是容器的历史 CPU 利用率。一个 Pod CPU 利用率 5% 但 Ray 没有 pending demand，IPPR 不会动它；反过来，CPU 利用率不高但 Ray 明确报告缺 3 核，IPPR 才会尝试扩。

### 57.1.3 为什么 Ray 特别适合原地扩缩

Ray 的资源是**逻辑资源**：Raylet 启动时会根据 `num-cpus`/`num-gpus`/`memory` 向 GCS 注册自己能调度多少。这个数字和 cgroup 限额不是硬绑定的。如果 Pod 的 requests 变了而 Raylet 的逻辑资源没变，Ray 仍然认为只有 1 核，扩容就白做了。因此 IPPR 必须同时完成两半：

1. Kubernetes 侧：改变容器的 cgroup 限额；
2. Ray 侧：把 Raylet 的逻辑资源从 1 核更新到 3 核。

KubeRay 的 IPPR 正是围绕这两半设计的。第 35 章讲过 Raylet/GCS 的资源模型；本章把它和 Kubernetes 的 resize 子资源接起来。

## 57.2 Kubernetes 侧：In-Place Pod Resize

### 57.2.1 版本与前置条件

| Kubernetes | In-Place Pod Resize 状态 | 说明 |
|---|---|---|
| 1.27 | alpha | 默认关闭 |
| 1.33 | beta | 默认开启，`resizePolicy` 可用 |
| 1.35 | **stable** | `InPlacePodVerticalScaling` 锁定为开启，不能关闭 |

生产前提：

- **cgroup v2**；cgroup v1 节点不支持；
- 只能用 **CPU 和 memory**；GPU、TPU、扩展资源不能原地改；
- 不支持 Linux swap、static CPU Manager、static Memory Manager 的某些组合；
- 容器运行时必须支持；containerd/CRI-O 的较新版本可用，老旧运行时可能降级为重启；
- Pod 的 QoS 类别可能影响可调范围（Guaranteed/Burstable/BestEffort）。

### 57.2.2 `resize` 子资源

原地扩缩不是普通 `kubectl apply`。它走 **`resize` 子资源**：

```bash
# kubectl >= 1.32
kubectl patch pod raycluster-ippr-small-group-0 \
  --subresource resize \
  --patch '{"spec":{"containers":[{"name":"ray","resources":{"requests":{"cpu":"4","memory":"4Gi"},"limits":{"cpu":"4","memory":"4Gi"}}}]}}'
```

普通 `apply` 修改 Pod 的 resources 会被 API Server 拒绝（Pod 的大多数字段不可变），只有 `resize` 子资源允许改 CPU/内存。

### 57.2.3 `resizePolicy`

每个容器可以声明每种资源的调整方式：

```yaml
resizePolicy:
  - resourceName: cpu
    restartPolicy: NotRequired     # 默认；原地改
  - resourceName: memory
    restartPolicy: NotRequired
```

- `NotRequired`：尽量原地改，不重启容器；
- `RestartContainer`：改完后重启容器；许多运行时无法动态调整 memory limit，只能重启；
- **IPPR 只接受 `NotRequired`**。如果用户写了 `RestartContainer`，KubeRay 的 IPPR 前置校验会拒绝，因为“重启容器”已经违背了不中断的初衷。
- `resizePolicy` 在 Pod 创建后不可修改，必须在模板里预先声明。

### 57.2.4 状态与事件

Kubernetes 用两个 condition 表达进度：

- `PodResizePending`：请求已接受但还不能执行（例如节点资源不足、需要等待）；
- `PodResizeInProgress`：kubelet 正在改 cgroup。

完成后 condition 消失，`status.containerStatuses[*].resources` 反映实际生效的资源。常见事件：

```text
Normal  ResizeStarted   ...
Normal  ResizeCompleted ...
Warning ResizeFailed    ...
Warning ResizeDeferred  ...
```

排障时先看 condition，再看事件，最后看 `containerStatuses[].resources` 是否等于 spec。**spec 是期望值，status 才是实际值**；两者不一致说明 resize 还在进行或失败了。

### 57.2.5 原地扩缩不会改变什么

- **Pod UID 不变**：这是和重建最本质的区别；
- Pod IP 不变（网络命名空间不重建）；
- 容器进程不重启（`NotRequired` 时）；
- emptyDir/`/dev/shm` 的内容保留；
- ownerReference、labels、annotations 保留。

会变的只有容器的 requests/limits、cgroup 限额，以及（在 IPPR 中）Raylet 的逻辑资源。

## 57.3 KubeRay IPPR 的配置契约

### 57.3.1 `ray.io/ippr` 注解

KubeRay 用 RayCluster 上的注解声明每个 worker group 的上限：

```yaml
# RayCluster 片段
metadata:
  name: raycluster-ippr
  annotations:
    ray.io/ippr: '{"groups":{"small-group":{"max-cpu":"4","max-memory":"4Gi","resize-timeout":60}}}'
spec:
  enableInTreeAutoscaling: true
  autoscalerOptions:
    version: v2
  # ...
```

JSON 结构：

```json
{
  "groups": {
    "<workerGroupName>": {
      "max-cpu": "4",          // string 或 number；支持 "1500m"
      "max-memory": "4Gi",     // string 或整数 bytes
      "resize-timeout": 60     // 整数秒，必填
    }
  }
}
```

三个字段都必填：

- `max-cpu`：该 group 每个 Pod 能扩到的 CPU 上限；
- `max-memory`：每个 Pod 能扩到的内存上限；
- `resize-timeout`：一次 resize 的等待上限；超时后 autoscaler 认为失败并回滚/重试。

没有出现在 `groups` 里的 worker group 不会被垂直扩缩，仍然只走水平路径。

### 57.3.2 前置校验

KubeRay 在启动 Pod 前会检查以下条件；任何一条不满足，autoscaler 会报错并拒绝启动对应 worker group：

1. **autoscaler v2**：`autoscalerOptions.version: v2`（或等价环境变量）；v1 不支持 IPPR。
2. **in-tree autoscaling 开启**：`enableInTreeAutoscaling: true`。
3. **`rayStartParams` 里不能有 `num-cpus` 或 `memory`**：Ray 必须从 Pod requests 推导逻辑资源；如果用户硬编码了 `num-cpus: "2"`，resize 后 Ray 仍然只看到 2 核。
4. **Ray 容器必须同时声明 CPU 和 memory requests**：没有 request 就没有 resize 的基准。
5. **`resizePolicy` 必须是 `NotRequired`**（或省略，默认即 `NotRequired`）；`RestartContainer` 被拒绝。
6. **`max-cpu`/`max-memory` 不能低于当前 request**；否则第一次 resize 就是缩容，而 IPPR 只做扩容。
7. **group 名必须存在**于 `workerGroupSpecs`；拼错会被拒绝，而不是静默忽略。
8. **字段类型严格**：三个键必填；布尔值、非整数 `resize-timeout` 和未知键（例如把 `max-cpu` 拼成 `max-gpu`）都会被拒绝。JSON 的 `true` 在 Python 里是 `int`，不显式拒绝就会被当成 1。

这些检查也是排查 “为什么 IPPR 没生效” 的第一张清单。

### 57.3.3 为什么禁止 `num-cpus`/`memory`

这是 IPPR 最容易踩的坑。考虑：

```yaml
workerGroupSpecs:
  - groupName: small
    rayStartParams:
      num-cpus: "1"          # 错误：Ray 永远只看到 1 核
    template:
      spec:
        containers:
          - name: ray
            resources:
              requests: {cpu: "1"}
```

autoscaler 把容器扩到 4 核，cgroup 也变成 4 核，但 Raylet 启动参数仍然是 `--num-cpus=1`，GCS 里这个节点只有 1 核可调度。结果是“机器变大了，Ray 还是放不下 task”。KubeRay 选择直接拒绝这种配置，而不是让用户在生产里困惑。

正确的写法是只写 requests，让 Ray 的默认资源探测读取容器限额：

```yaml
workerGroupSpecs:
  - groupName: small
    rayStartParams: {}
    template:
      spec:
        containers:
          - name: ray
            resources:
              requests: {cpu: "1", memory: "1Gi"}
              limits:   {cpu: "4", memory: "4Gi"}
```

### 57.3.4 一个完整的可运行样例

仓库中的 `manifests/samples/ray-cluster-ippr.yaml` 是可直接对照的版本：

```yaml
apiVersion: ray.io/v1
kind: RayCluster
metadata:
  name: raycluster-ippr
  annotations:
    ray.io/ippr: '{"groups":{"small-group":{"max-cpu":"4","max-memory":"4Gi","resize-timeout":60}}}'
spec:
  rayVersion: "2.59.0"
  enableInTreeAutoscaling: true
  autoscalerOptions:
    version: v2
    upscalingMode: Default
  headGroupSpec:
    rayStartParams:
      num-cpus: "0"
    template:
      spec:
        restartPolicy: Never
        containers:
          - name: ray
            image: rayproject/ray:2.59.0
            resources:
              requests: {cpu: "1", memory: 4Gi}
              limits:   {cpu: "1", memory: 4Gi}
  workerGroupSpecs:
    - groupName: small-group
      replicas: 1
      minReplicas: 1
      maxReplicas: 10
      rayStartParams: {}
      template:
        spec:
          restartPolicy: Never
          containers:
            - name: ray
              image: rayproject/ray:2.59.0
              resources:
                requests: {cpu: "1", memory: 1Gi}
                limits:   {cpu: "4", memory: 4Gi}
              resizePolicy:
                - resourceName: cpu
                  restartPolicy: NotRequired
                - resourceName: memory
                  restartPolicy: NotRequired
```

注意 head 的 `num-cpus: "0"` 是允许的：IPPR 的校验只针对被配置的 worker group。head 不参与垂直扩缩。

## 57.4 决策顺序：先装、再扩、最后横着长

KubeRay 的 IPPR 不是“看到 pending 就 resize”。它把一次决策拆成四步，顺序很重要。

### 57.4.1 四步算法

```text
1. 用当前容量装箱
   for each worker group (priority 高到低):
       把 pending demand 与已有 Pod 的 current requests 对齐
       能放下的部分从 demand 中扣除

2. 用最大容量装箱
   对仍有 demand 的 group：
       对每个不在 resize 中的 Pod，计算 headroom = max - current
       把 demand 装进 headroom（先装 CPU，再装 memory）
       生成 resize 请求

3. 仍然装不下的 demand
   按模板容量计算还需要多少副本
   水平扩容（受 maxReplicas 约束）

4. 下一轮 reconciliation
   成功：更新 Raylet 逻辑资源
   超时/失败：回滚，并把 demand 留给下一轮（可能转水平）
```

为什么顺序是“先当前、再最大、最后水平”？

- 先当前：已经在运行的容量优先，避免不必要的 resize；
- 再最大：能靠“把现有 Pod 变大”解决的，不新建 Pod；
- 最后水平：垂直上限用尽后才加副本，避免“既 resize 又加 Pod”造成过度供给。

### 57.4.2 一个手算例子

配置：

- worker group `small`：1 个 Pod，当前 1 CPU / 1Gi；
- IPPR 上限：4 CPU / 4Gi；
- pending demand：3 CPU。

第一轮：

```text
current capacity = 1 CPU
remaining = 3 - 1 = 2 CPU
headroom = 4 - 1 = 3 CPU
absorb = min(2, 3) = 2 CPU
target = 1 + 2 = 3 CPU
remaining = 0
=> resize 1 -> 3，不新建 Pod
```

如果 demand 是 6 CPU：

```text
current = 1 CPU, remaining = 5
headroom = 3, absorb = 3 -> target 4, remaining = 2
required replicas = ceil(2 / 1) = 2
target replicas = 1 + 2 = 3
=> 1 个 Pod 扩到 4 CPU，另建 2 个 1 CPU Pod，总容量 6
```

这正是 `examples/ippr_lab.py` 的场景 3。

### 57.4.3 多主机 group

`numOfHosts > 1` 时，一个 replica 由多个 Pod 组成。IPPR 的 `max-cpu`/`max-memory` 是**每个 Pod** 的上限，不是整个 replica 的上限：

```text
replica capacity = numOfHosts × per-Pod max
```

resize 时只改能装下 demand 的那些 Pod；多主机组的一致性由 Ray 的 placement group/多主机协调保证。生产上不要把多主机组的部分 Pod 扩得比同组其他 Pod 大太多，否则集合通信的瓶颈会落在最小的那个 Pod 上。

### 57.4.4 GPU 不参与

IPPR 只支持 CPU/memory。一个 Pod 需要更多 GPU 时只能：

- 水平加 Pod；
- 换更大的 GPU 节点池并重建 Pod；
- 使用 MIG/time-slicing 改变可见 GPU 形状（第 36 章），但那不是 IPPR。

因此 `max-cpu` 配置并不能让一个 1 GPU 的 Pod 变成 2 GPU。GPU 的 pending demand 仍然走水平路径。

## 57.5 状态、超时与回滚

### 57.5.1 `ray.io/ippr-status`

autoscaler 在 Pod 上写 `ray.io/ippr-status`，记录一次 resize 的生命周期。上游字段可能随版本变化；MiniKubeRay 使用以下教学子集：

```json
{
  "state": "resizing",
  "group": "small",
  "resizing_at": "2026-10-18T12:00:00Z",
  "timeout_seconds": 60,
  "previous_cpu": "1",
  "previous_memory": "1Gi",
  "target_cpu": "3",
  "target_memory": "1Gi",
  "attempts": 1
}
```

状态机：

```text
             issue resize
   (none) ───────────────► resizing
                              │
                 kubelet ok   │   timeout / error
                 ┌────────────┴────────────┐
                 ▼                         ▼
             succeeded                   failed
                                           │ autoscaler rollback
                                           ▼
                                      rolled-back
                                           │ retry window
                                           └──► resizing (attempts+1)
```

上游还会写 `last_failed_at` 等字段；排查时用 `kubectl describe pod` 看 condition/events，比直接解析注解更稳。

### 57.5.2 为什么需要回滚

resize 失败的原因很多：

- 节点没有足够的可分配资源（cgroup 限额改不上去）；
- 容器运行时不支持动态 memory；
- Pod 的 QoS/limit 约束不允许；
- kubelet 超时；
- 节点被 drain 或 kubelet 重启。

失败后如果 Pod 的 spec 停在“目标值”而实际 cgroup 还是旧值，会出现“spec 说 3 核、Ray 看到 1 核”的撕裂。IPPR 的做法是：

1. autoscaler 记录 `previous_cpu`/`previous_memory`；
2. 超时后把 Pod spec 恢复到旧值；
3. 在 `retry-after` 窗口内不再对同一 Pod 发起 resize；
4. 把未满足的 demand 交给水平扩容或其他 Pod。

这保证系统至少能退回到“水平可用”的状态，而不是卡在半个 resize 上。

### 57.5.3 可观测性

关键信号：

```bash
# Pod 的 resize 状态
kubectl get pod <pod> -o jsonpath='{.metadata.annotations.ray\.io/ippr-status}'

# condition 与事件
kubectl describe pod <pod> | grep -A5 -i resize

# 容器实际生效的资源
kubectl get pod <pod> -o jsonpath='{.status.containerStatuses[?(@.name=="ray")].resources}'

# Raylet 是否真的看到了新容量
kubectl exec <head> -- ray status
```

四个值要对上：

1. Pod spec 的 requests；
2. `status.containerStatuses[].resources`；
3. `ray status` 里该节点的 CPU；
4. KubeRay 的 autoscaler 日志/指标。

任何一个对不上，就是 IPPR 没有完整完成。

### 57.5.4 指标

MiniKubeRay 把 resize 动作计入已有的 autoscaler 指标：

```text
kuberay_autoscaler_actions_total{action="resize-up",...}
kuberay_autoscaler_actions_total{action="resize-rollback",...}
```

生产上建议额外观察：

- resize 成功率（succeeded / issued）；
- resize 延迟（`completed_at - resizing_at`）；
- 回滚率；
- 因 IPPR 失败而转为水平扩容的比例；
- 节点可分配资源与 `max-cpu` 的差距（避免“配置了 4 核但节点只剩 1 核”）。

## 57.6 限制、边界与生产陷阱

### 57.6.1 只扩不缩

当前 IPPR 是 **resize-up only**：可以把 1 核扩到 4 核，但不会因为需求下降把 4 核缩回 1 核。缩容仍然通过删除 Pod/副本完成。

这意味着：

- 一个经历过高峰的集群会保留“变大的 Pod”，直到 Pod 被替换；
- 成本不会因为需求下降自动回落；
- 不要用 IPPR 替代 capacity planning，它只是“应急变宽”。

上游把“gradual resize / downsizing”列为未来工作。生产上如果需要缩容，使用水平缩容或滚动替换。

### 57.6.2 只有 CPU 和 memory

GPU、TPU、FPGA、自定义扩展资源都不能原地改。需要更多 GPU 只能加 Pod 或换节点池。

### 57.6.3 节点必须有空间

resize 请求只改变 Pod 的 requests；**调度器不会重新调度 Pod**。如果节点已经分配不出 3 核，cgroup 改不上去，resize 会失败并回滚。

因此 IPPR 的前提是节点池保留一定的 allocatable 余量，或者 Cluster Autoscaler/Karpenter 能及时加节点。但注意：**节点扩容发生在 Pod 调度时，而 IPPR 不触发重新调度**。一个已经 Running 的 Pod resize 失败，不会自动把 Pod 迁到新节点；只能等回滚后水平扩容，新 Pod 才会被调度到新节点。

### 57.6.4 与 QoS/limit 的交互

- `Guaranteed` Pod 的 requests == limits；resize 时两者都要改；
- `Burstable` Pod 可以只改 requests；
- `BestEffort` 没有 requests，无法作为 IPPR 的基准（校验会失败）；
- 如果 limit 小于目标 request，Kubernetes 会拒绝；MiniKubeRay 在 apply 时自动抬高 limit，但真实集群要自己保证。

### 57.6.5 与 HPA/VPA 的冲突

不要让 VPA 和 IPPR 同时管理同一个 Pod 的资源：

- VPA 可能根据历史用量把 Pod 改小；
- IPPR 可能根据 pending demand 把 Pod 改大；
- 两者都写 `resize` 子资源时会产生竞争和抖动。

选一个：

- 用 IPPR 做 Ray 感知的弹性，关闭 VPA；
- 或用 VPA 做通用资源推荐，关闭 IPPR。

HPA 管副本数，与 IPPR 不直接冲突，但两者都根据负载做决策时可能叠加过度扩容。生产上建议 HPA 只用于非 Ray 工作负载，Ray 的副本数交给 Ray autoscaler。

### 57.6.6 与节点自动扩缩容的时序

```text
Ray pending demand
   -> IPPR 尝试原地扩
   -> 节点没有余量，resize 失败/回滚
   -> Ray autoscaler 水平加副本
   -> 新 Pod Pending
   -> Cluster Autoscaler/Karpenter 加节点
   -> 新 Pod Running
```

注意 IPPR 不会“等待节点扩容后重试同一个 Pod”：它失败后把需求交给水平路径。这个设计避免了“一个 Pod 卡住整个扩缩容”。

### 57.6.7 与 RayService 增量升级

IPPR 改的是 worker Pod 的 requests。RayService 的 revision hash 包含 Pod 模板；如果用户把 `max-cpu` 写进模板（而不是注解），改它会触发 pending 集群。正确做法是把上限放在 `ray.io/ippr` 注解里，让模板保持稳定。

增量升级期间，pending 集群的 autoscaler 也会执行 IPPR；但流量迁移的容量判断基于 `target_capacity` 和副本数，不直接感知“Pod 变大了”。压测时要同时看 `ray status` 的 CPU 和 Serve 的副本数。

另外，`rayClusterConfig` 只是 spec 片段，不能携带 RayCluster 的 `metadata.annotations`。MiniKubeRay 的 RayService/RayJob/RayCronJob 控制器会逐层把父对象上的 `ray.io/ippr` 转发给子对象（`rayservice.py`、`rayjob.py`、`raycronjob.py`），并有测试覆盖；真实 KubeRay 是否做同样的转发以目标版本为准，不要假设在 RayService 上写注解就一定生效。若要在 Serve 集群上使用 IPPR，先查看 operator 行为，或在 fork 里显式传播注解（第 38 章）。

## 57.7 生产采用路径

IPPR 还是 alpha，建议分阶段：

### 阶段 1：只读观察

- 开启 autoscaler v2 的日志；
- 配置 `ray.io/ippr`，但把 `max-cpu` 设成等于当前 request（等价于禁用扩容）；
- 观察 autoscaler 是否报告校验错误、是否有意外行为。

### 阶段 2：单租户 canary

- 选一个非关键 RayCluster；
- 设置温和的上限（例如 1 -> 2 核）；
- 观察 resize 成功率、延迟、Raylet 资源是否同步；
- 对比“IPPR + 较少副本”与“纯水平”的成本/延迟。

### 阶段 3：生产默认

- 把 IPPR 用于启动慢、状态重的 worker（例如加载大模型的推理 worker）；
- 水平路径仍然保留，作为上限用尽后的兜底；
- 在 runbook 中加入 “resize 失败/回滚” 的排查步骤；
- 监控节点 allocatable 余量，避免 IPPR 长期失败。

### 采用检查表

- [ ] Kubernetes 1.35+，节点 cgroup v2；
- [ ] 容器运行时支持原地 resize；
- [ ] autoscaler v2；
- [ ] worker 的 `rayStartParams` 没有 `num-cpus`/`memory`；
- [ ] Ray 容器同时有 CPU/memory requests；
- [ ] `resizePolicy` 为 `NotRequired`；
- [ ] `max-cpu`/`max-memory` 高于当前 request，且低于节点可分配余量；
- [ ] 关闭同一批 Pod 的 VPA；
- [ ] 有 resize 成功/失败/回滚的指标与告警；
- [ ] 知道 IPPR 只扩不缩，缩容仍走水平路径；
- [ ] 演练过“resize 失败 -> 水平扩容”的降级路径。

## 57.8 MiniKubeRay 的实现

MiniKubeRay 把 IPPR 拆成四个可测试的边界，和真实控制面一一对应。

### 57.8.1 模型与校验：`minikuberay/models/ippr.py`

`parse_ippr(cluster)` 负责：

- 解析 `ray.io/ippr` JSON；
- 校验 group 存在、三个字段齐全、数值合法，并拒绝未知键、布尔值和带小数的 `resize-timeout`；
- 校验 autoscaler v2、in-tree autoscaling；
- 校验 `rayStartParams` 没有 `num-cpus`/`memory`；
- 校验 Ray 容器有 CPU/memory requests；
- 校验 `resizePolicy` 为 `NotRequired`；
- 返回 `{group: IPPRGroupConfig}`。

`IPPRGroupConfig` 把 CPU 转成 millicores、memory 转成 bytes，后续决策全部用整数，避免浮点误差。

`apply_resize()` 做三件事：

1. 改 Ray 容器的 requests（以及必要时抬高 limits）；
2. 写 `ray.io/ippr-status`；
3. 给 Pod 加 `PodResizeInProgress` condition。

它**不**改 Pod UID、不删 Pod、不重启容器。

`complete_resize()` / `fail_resize()` / `rollback()` 分别对应 kubelet 成功、kubelet 失败、autoscaler 回滚。状态机与 57.5 一致。

### 57.8.2 决策：`AutoscalerPolicy`

`decide(cluster, pods=...)` 在原有水平逻辑前插入垂直阶段：

```python
# 1. 用实时 Pod 的 requests 计算已有容量
if config and group_pods:
    existing_cpu = sum(pod_resources(pod).cpu for pod in group_pods)
    # 缺失的 Pod 按模板的“每 Pod”容量计，注意 numOfHosts 的倍数
    missing_pods = max(0, group_desired_pods(group) - len(group_pods))
    existing_cpu += missing_pods * capacity["cpu"]
else:
    existing_cpu = current * replica_cpu

remaining_cpu = max(0, remaining_cpu - existing_cpu)

# 2. 用 max 容量装剩余 demand
for pod in group_pods:
    headroom = config.max_cpu - pod_resources(pod).cpu
    absorb = min(headroom, remaining_cpu)
    if absorb > 0:
        emit resize-up(pod, target=current+absorb)
        remaining_cpu -= absorb

# 3. 剩余 demand 走水平
required = ceil(remaining_cpu / replica_cpu)
```

三个细节决定了它是否“像真的”：

- **已有容量用实时 Pod 求和**，而不是 `replicas × 模板容量`。否则一个已经扩到 4 核的 Pod 会被重复计算成 1 核，导致过度扩容。
- **正在 resize 的 Pod 跳过**，避免同一轮重复发请求。
- **回滚后的 Pod 有 retry window**，窗口内不再垂直重试，demand 转水平。

### 57.8.3 应用：`RayClusterController._autoscale`

控制器只做三件事：

- `resize-up`：取 Pod，调用 `apply_resize()`，写回 store，发 `AutoscalerResizeUp` 事件；
- 失败回滚不走决策队列：kubelet 把 Pod 标成 `failed` 后，控制器在每轮调和开始时调用 `_recover_failed_resizes()`，即使 IPPR 注解被删、autoscaling 被关或新 spec 校验失败也会把 Pod 恢复到 previous resources，并发 `AutoscalerResizeRolledBack` 事件；
- 其余动作（scale-up/scale-down）保持原逻辑。

它不直接改 Runtime 的 NodeRecord；那是 kubelet 成功后的职责。

### 57.8.4 完成：`Kubelet._reconcile_ippr`

模拟 kubelet 在 Running Pod 上看到 `state=resizing`：

- 如果 `minikuberay.io/ippr-result=timeout/failure`，推进到 `failed`；
- 否则等待 `minikuberay.io/ippr-delay-seconds`（默认 0.2s）后推进到 `succeeded`；
- 成功时调用 `runtime.update_pod_resources(pod)`，把 Raylet 的逻辑 CPU/memory 更新为新的 requests。

这两个测试注解只属于 MiniKubeRay，真实集群不会认识它们；它们让“超时/失败”变成确定性测试，而不是靠等真实 kubelet。

### 57.8.5 指标与事件

- `kuberay_autoscaler_actions_total{action="resize-up"}`；
- `kuberay_autoscaler_actions_total{action="resize-rollback"}`；
- Normal 事件 `AutoscalerResizeUp`；
- Warning 事件 `AutoscalerResizeRolledBack`。

`python -m minikuberay describe raycluster <name>` 会把这些事件列出来。

### 57.8.6 与真实实现的差距

| 能力 | 真实 KubeRay | MiniKubeRay |
|---|---|---|
| 注解解析/校验 | Go | Python，规则对齐 |
| resize 请求 | Kubernetes `resize` 子资源 | 直接改 Pod spec |
| cgroup 生效 | kubelet/runtime | 状态机 + 延迟 |
| Raylet 逻辑资源 | autoscaler 调 Ray | `NodeRecord` 更新 |
| 超时/回滚 | 真实 kubelet 事件 | 测试注解驱动 |
| 装箱算法 | Ray autoscaler v2 完整实现 | 简化的按组贪心 |
| GPU/多资源 | 只支持 CPU/memory | 同上 |
| 指标 | 上游指标名 | 复用 autoscaler 指标 |

MiniKubeRay 不模拟真实 cgroup，也不跑真实 Ray；它模拟的是**控制流和状态机**。这正是第 19 章说的“控制平面实验台”。

## 57.9 实验：把 IPPR 跑成断言

```bash
python examples/ippr_lab.py
```

预期输出：

```text
1 config      max-cpu=4 max-memory=4Gi timeout=60s groups=['small']
2 resize-up   pods=1 replicas=1 cpu=3000m uid_same=True state=succeeded
3 spill-over  pods=3 replicas=3 cpu=[1000, 1000, 4000] total=6000m
4 rollback    pods=2 replicas=2 cpu=1000m state=rolled-back
5 events      resize_actions=['AutoscalerResizeRolledBack', 'AutoscalerResizeUp']
6 validation  reason=ValidationFailed message="IPPR group 'small' max-cpu (500m) is below the c" events=['InvalidRayClusterSpec']
OK: IPPR resized a running Pod in place, fell back to horizontal capacity, rolled back a timed-out resize, and rejected an invalid annotation.
```

每个断言证明什么：

1. **配置解析**：注解被正确解析成 4 核上限；
2. **原地扩容**：Pod 数不变、replica 不变、UID 不变、CPU 从 1 核变 3 核、状态 `succeeded`；
3. **水平兜底**：3 个 Pod 的总容量正好 6 核，其中第一个 Pod 是 4 核；
4. **回滚**：超时后第一个 Pod 回到 1 核，第二个 Pod 补足需求；
5. **事件**：rollback 和 resize-up 都留下了可查询的事件；
6. **校验**：`max-cpu` 低于当前 request 时，集群进入 `ValidationFailed`，不会创建 worker。

对应的 pytest：

```bash
python -m pytest tests/test_ippr.py -q
```

19 个测试覆盖解析、严格类型校验、原地扩容、内存/多主机、水平兜底、超时回滚、注解删除后的恢复、RayService/RayJob 注解传播和收敛幂等。

### 57.9.1 自己改一改

1. 把 `max-cpu` 改成 `8`，demand 改成 6：应该只 resize 一次到 6 核（现有 1 核 + 剩余 5 核），不新建 Pod。
2. 把 `max-memory` 改成 `512Mi`：校验应失败（低于当前 1Gi）。
3. 在 `rayStartParams` 加 `num-cpus: "1"`：校验应失败。
4. 把 `resizePolicy` 改成 `RestartContainer`：校验应失败。
5. 把 `minikuberay.io/ippr-result` 改成 `failure`：观察 rollback 与水平扩容的时序。
6. 把 demand 的 `memory_mb` 设成 2048：观察 `target_memory` 是否增长，以及 Raylet 的 memory 是否同步。

## 57.10 练习题

1. **为什么 IPPR 只扩不缩？** 如果允许缩容，autoscaler 需要处理哪些新问题（正在运行的 task、对象存储、Raylet 重新注册、节点回收）？
2. **为什么必须禁止 `rayStartParams.num-cpus`？** 如果用户坚持要限制 Ray 可见 CPU（例如留出系统预留），IPPR 还能用吗？有没有替代方案？
3. **一个 4 核 Pod resize 到 3 核失败，回滚到 4 核；下一轮 demand 仍然是 3 核。** 系统会怎么走？会不会抖动？
4. **IPPR 与 Cluster Autoscaler 的时序**：节点没有余量时，为什么 IPPR 不等待新节点，而是转水平？这个选择有什么代价？
5. **多主机组**：`numOfHosts=4`，每个 Pod 1 核，demand 6 核。IPPR 会怎么分配？会不会出现 4 个 Pod 大小不一致？
6. **可观测性**：设计一个告警，能在“IPPR 连续失败但水平扩容成功”时提示平台团队。应该看哪些指标/事件？
7. **实现练习**：给 MiniKubeRay 加一个 `max-gpu` 字段，并让校验明确拒绝它（说明为什么不能原地改 GPU）。这比假装支持更有教学价值。

## 57.11 小结

- IPPR 是 Ray 感知的垂直弹性：看 pending demand，不看 CPU 利用率。
- Kubernetes 1.35 提供 stable 的原地 Pod resize；KubeRay 用 `ray.io/ippr` 注解把上限告诉 autoscaler。
- 决策顺序是“当前容量 -> 最大容量 -> resize -> 水平扩容”；垂直优先，水平兜底。
- 成功的关键是两半都完成：cgroup 变了，Raylet 的逻辑资源也要变。
- 当前限制：只扩不缩、只支持 CPU/memory、alpha、需要节点余量、与 VPA 冲突。
- MiniKubeRay 用 4 个模块（模型、策略、控制器、kubelet）复现了状态机，并用 19 个测试和 1 个实验把它变成可断言的数字。

下一章换一个角度：Kubernetes 自身的新原语——原生 sidecar、调度门、拓扑感知路由、Pod 级资源——如何改变 KubeRay 的行为边界。

→ [第 58 章 Kubernetes 新原语与 KubeRay：原生 sidecar、调度门、拓扑感知与 Pod 级资源](58-kubernetes-primitives.md)
