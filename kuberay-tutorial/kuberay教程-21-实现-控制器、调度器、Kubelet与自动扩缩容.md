# 第 21 章 实现：控制器、调度器、Kubelet 与自动扩缩容

本章沿着 RayCluster 的生命周期，拆解 `base.py`、`builders.py`、`batch.py`、`raycluster.py` 和 `autoscaler/policy.py`。

## 21.1 WorkQueue 与 RateLimiter

`minikuberay/controllers/base.py`：

- `WorkQueue`：堆 + 去重集合 + 延迟时间；
- `RateLimiter`：per-key 失败次数，指数退避 `base * 2^(n-1)`，上限 1000s；
- `Controller`：`reconcile` 抽象、`reconcile_all` 扫描、错误分类、指标；
- `Operator`：组装控制器、`run_once`、`settle`、后台线程。

### 21.1.1 为什么保留队列

即使 `Operator.run_once` 是扫描式，队列仍然：

- 演示真实 controller-runtime 的语义；
- 提供 `add_rate_limited` 的退避实现，便于扩展成 watch 驱动；
- 但**当前 `run_once` 不 drain 队列**：冲突/失败会在下一轮全量扫描时自然重试。不要把队列当成当前的重试主路径。

### 21.1.2 run_once 的顺序

```text
before = store.resource_version
kubelet.step()
endpoints.reconcile_all()
raycluster.reconcile_all()
rayjob.reconcile_all()
rayservice.reconcile_all()
raycronjob.reconcile_all()
batch.reconcile_all()
gc.run()
return store.resource_version != before
```

顺序的原因：

1. kubelet 先把 Pod 推进一个阶段；
2. EndpointSlice 跟随 Service/Pod；
3. RayCluster 创建/更新 Pod/Service；
4. RayJob/RayService/RayCronJob 依赖 RayCluster；
5. Batch Job 处理 submitter；
6. GC 清理孤儿。

`settle` 反复 `run_once` 直到没有 resourceVersion 变化，或达到上限。

## 21.2 Controller 基类

```python
class Controller:
    def reconcile_all(self):
        for obj in store.list(self.kind, namespace):
            try:
                changed = self.reconcile(obj) or changed
            except Conflict:
                queue.add_rate_limited(key)
            except (ApiError, ValueError, KeyError) as exc:
                recorder.warning(obj, "ReconcileError", str(exc))
```

`ensure(kind, obj)`：

- 不存在 -> create；
- 存在 -> 去掉 metadata 易变字段/status 后比较；
- 不同 -> update，保留 status；
- 相同 -> 不写。

这是“幂等”的核心：重复调和不会产生无意义的写入。

## 21.3 RayCluster 调和

`minikuberay/controllers/raycluster.py` 的 `reconcile`：

1. 删除中 -> `_cleanup`；
2. `managedBy` 外部（MultiKueue）-> 直接 skip，**不加 finalizer**；
3. 加 RayCluster finalizer；Redis GCS FT 还要加/移除 cleanup finalizer；
4. `apply_defaults`，spec 变化 -> 更新并返回；
5. `validate_raycluster` -> 失败 `_invalid`；
6. feature gate 检查（MultiHost/mTLS/NetworkPolicy/History/RocksDB/WAS）；
7. suspend -> `_reconcile_suspend`；否则清理 suspend conditions；
8. 计算 spec hash，处理 Recreate/None；
9. `_ensure_auxiliary`（RBAC/PVC/Secret/NetworkPolicy/ServiceAccount 等）；
10. `_ensure_services`；
11. `_prune_auxiliary`（删除不再需要的辅助对象）；
12. `_ensure_pods`；
13. `_autoscale`；
14. `_write_status`；
15. `_remember_hash`（写回 `minikuberay.io/spec-hash` 注解）。

### 21.3.1 spec hash

```python
payload = deep_copy(spec)
for group in payload["workerGroupSpecs"]:
    group.pop("replicas", None)
    group.pop("minReplicas", None)
    group.pop("maxReplicas", None)
    group.get("scaleStrategy", {}).pop("workersToDelete", None)
hash = stable_hash(payload, 16)
```

排除 `replicas`、`minReplicas`、`maxReplicas` 和 `workersToDelete`，避免 autoscaler 的日常变更触发“模板升级”。真实 KubeRay 的 spec hash 也排除这些自动扩缩容拥有的字段；否则每次扩容都会被误判为“模板变化”。

### 21.3.2 Recreate/None

- 存 hash 在 RayCluster 的 `minikuberay.io/spec-hash` 注解，并镜像到所有 Ray Pod 的 `ray.io/spec-hash` 标签；
- `_stored_hash` 优先读注解，注解不存在时回退到 Pod 标签（兼容旧对象）；
- hash 变化 + Recreate + 有 Pod -> 删除所有 Pod，下一轮重建；
- hash 变化 + None -> 只写 status，不创建新 Pod；
- 其他 -> 正常 ensure；收敛时 `_remember_hash` 写回注解。

> 真实 KubeRay 把 Recreate 的 hash 放在 head Pod 的 `ray.io/upgrade-strategy-recreate-hash` 注解；MiniKubeRay 为了让所有 worker 也能参与比较，使用集群注解 + 所有 Pod 标签，行为等价但字段不同。

### 21.3.3 Pod ensure

head：

- 0 个 -> 创建；
- >1 -> 删除多余的；
- Failed/Succeeded -> 删除，下一轮重建；但若 `ray.io/disable-provisioned-head-restart=true` 且 `RayClusterProvisioned=True`，则保留现状、不重建。SidecarMode 的 RayJob 会设置这个注解，避免 submitter 失败后反复重建已 provision 的 head（第 22 章）。

worker：

- 按 group 选择 Pod；
- 删除 Failed/Succeeded；
- 处理 `workersToDelete`；
- desired = clamp(replicas, min, max) * numOfHosts；
- diff > 0 -> 创建缺失索引；
- diff < 0：
  - autoscaling 且未允许随机删除 -> 交给 autoscaler；
  - 否则删除多余；
- 删除时同时 `runtime.unregister_pod`。

### 21.3.4 状态

`_write_status`：

- 统计 head/worker；
- `state = ready` 当 head Ready 且 worker ready >= desired；
- 计算 desired/min/max/ready/available；
- 计算 CPU/Memory/GPU；
- endpoints 从 head Service 取；
- conditions：HeadPodReady、Provisioned（粘性）、Suspending/Suspended；
- 只在 status 变化时写，`lastUpdateTime` 只在变化时更新。

### 21.3.5 suspend

`_reconcile_suspend`：

- 有 Pod：设置 Suspending=True，删除 Pod，Suspended=False；
- 无 Pod：Suspending=False，Suspended=True，state=suspended；
- 恢复：清除 conditions，重新创建。

## 21.4 Builders

`minikuberay/controllers/builders.py`：

- `ray_start_command`：head/worker 的 `ray start` 参数；
- `build_head_pod`：
  - 命令、端口、env；
  - autoscaler sidecar；
  - history collector；
  - GCS FT env/PVC；
  - auth env；
  - `/dev/shm`；
- `build_worker_pod`：
  - head address；
  - 多主机标签；
  - GCS 重连超时；
- `build_head_service`：
  - headless；
  - 端口；
  - 自定义 headService；
- `build_serve_service`：`ray.io/serve=true`；
- `build_headless_service`；
- `build_network_policies`；
- `build_gcs_pvc`；
- `build_auth_secret`；
- `build_tls_secret`（占位证书，用于验证 volume/env 接线）；
- `build_autoscaler_rbac`。

这些 builder 是“KubeRay 的 Pod 模板逻辑”的教学版：字段少，但结构、标签、端口、env 与真实实现对应。

## 21.5 模拟调度器

`minikuberay/controllers/batch.py` 的 `SimulatedScheduler`：

- 节点：node-a（8 CPU/32Gi）、node-b（8 CPU/32Gi/1 GPU）、node-c（16 CPU/64Gi/2 GPU）；
- filter：
  - `nodeSelector`；
  - taint/toleration；
  - CPU/内存/GPU 容量；
- score：已用 CPU + GPU*1000，选最小；
- 找不到 -> Pending + Unschedulable condition。

资源单位：

- CPU 用毫核（1000 = 1 核）；
- 内存用字节；
- GPU 用整数。

这与 `parse_cpu`/`parse_memory` 一致。

## 21.6 模拟 Kubelet

`Kubelet.step`：

1. 列出 Pod；
2. 删除没有 finalizer 的 terminating；有 finalizer 的保持 Terminating 并继续占用资源；
3. Pending：
   - 没 nodeName -> 调度；
   - 调度失败 -> Unschedulable；
   - 第二次观察 -> Running；
   - 分配 podIP、hostIP、containerStatuses、conditions；
   - 注册 Runtime 节点；
4. Running：
   - crash 注解 -> Failed + unregister；
   - submitter 完成注解 -> Succeeded/Failed；
   - Ready 修正；
5. Succeeded/Failed -> unregister。

### 21.6.1 为什么要“第二次观察”

模拟环境没有真实 kubelet 的异步。用“第一次调度、第二次运行”保证：

- Pod 先出现在 store；
- 控制器/Endpoint 有机会看到 Pending；
- 测试可以断言中间状态。

## 21.7 EndpointController

- 为每个 Service 生成同名 EndpointSlice；
- 计算 clusterIP（非 headless）；
- endpoint 的 ready/serving；MiniKubeRay 在生成前过滤 terminating Pod，因此 `terminating` 恒为 false（真实 EndpointSlice 会保留 `terminating=true` 的端点）；
- 空 Service -> 空 endpoints。

真实 KubeRay 用 EndpointSlice 计算 `numServeEndpoints`；MiniKubeRay 直接数 Pod，但保留了 EndpointSlice 对象。

## 21.8 BatchJobController

- 为 Kubernetes Job 创建 submitter Pod；
- Pod 成功/失败 -> Job status；
- backoffLimit；
- suspend；
- 删除时清理 Pod。

RayJob 的 K8sJobMode 依赖它；RayJob 控制器在作业结束时给 Pod 打完成注解。

## 21.9 自动扩缩容

`minikuberay/autoscaler/policy.py`：

```text
demand = runtime.pending_demand
for group sorted by priority:
    capacity = group 的单个 Pod requests
    replica_capacity = capacity * numOfHosts
    # 先扣掉当前 group 已有副本满足的需求
    remaining = max(0, remaining - current * replica_capacity)
    required = max(ceil(remaining.cpu / replica_capacity.cpu),
                   ceil(remaining.gpu / replica_capacity.gpu))
    target = clamp(current + required, min, max)
    if target > current:
        delta = target - current
        if Conservative: delta = min(delta, max(1, current))
        desired = current + delta
        scale up; 从 remaining 中扣除新增副本的容量
    elif target < current:
        idle = nodes idle >= timeout
        remove = min(len(idle), current - max(min, required))
        scale down with workersToDelete
```

扩容写 `replicas`；缩容写 `replicas` + `workersToDelete`。`RayClusterController._ensure_pods` 只删除指定 Pod。

## 21.10 指标与事件

- `Controller.reconcile_all` 记录 `kuberay_operator_reconcile_total` 和 duration；
- RayCluster 状态变化记录 `kuberay_raycluster_ready`；
- autoscaler 记录 `kuberay_autoscaler_actions_total`；
- EventRecorder 写 Event 对象。

## 21.11 测试

- `tests/test_scheduler_kubelet.py`：调度、GPU、nodeSelector、crash、删除、EndpointSlice；
- `tests/test_raycluster.py`：就绪、扩缩、suspend、删除、升级、autoscaler、多主机、GCS FT、NetworkPolicy、auth；
- `tests/test_e2e.py`：多组件收敛、幂等。

## 21.12 多主机、Ingress、批调度与 GCS FT 清理

### 21.12.1 多主机

`_ensure_multi_host_pods` 以 replica 为原子单位：

- 按 `ray.io/worker-group-replica-name` 分组；
- 任一 host 失败/缺失 -> 删除整个 replica；
- 缩容删除整个 replica；
- 扩容按 host 创建，标签 `replica-index`/`host-index` 分开。

Pod 名：`<cluster>-<group>-<replica>-<host>`；单主机仍是 `<cluster>-<group>-<index>`。

### 21.12.2 Ingress

`build_ingress` 在 `headGroupSpec.enableIngress=true` 时创建 `networking.k8s.io/v1` Ingress：

- host/path/pathType 来自 `ingressOptions`；
- backend 指向 head Service 的 8265；
- 支持 `ingressClassName`/`tls`/`annotations`；
- 关闭后 `_prune_auxiliary` 删除。

### 21.12.3 批调度

`build_podgroup`：

- Volcano：`scheduling.volcano.sh/v1beta1`；
- scheduler-plugins：`scheduling.x-k8s.io/v1alpha1`；
- `minMember` = head + worker Pod 数；
- queue 从标签读取；
- Pod 的 `schedulerName` 由 `build_head_pod`/`build_worker_pod` 注入。

### 21.12.4 GCS FT 清理

`_cleanup_gcs_ft`：

- 只删除 Ray 节点 Pod；
- 创建 `<cluster>-redis-cleanup` Job；
- Job Complete 后移除 `ray.io/gcs-ft-redis-cleanup-finalizer`；
- 超时（默认 300s）强制移除并发 Warning。

### 21.12.5 ReplicaFailure

`_ensure_pods` 捕获创建异常，`_write_status` 写 `ReplicaFailure=True`（对应上游 Go 常量 `RayClusterReplicaFailure`，序列化 type 为 `ReplicaFailure`）；恢复后写 `False/ReplicaFailureResolved`。

## 21.13 一次调和的资源版本轨迹

MiniKubeRay 的 `Operator.run_once()` 用 `store.resource_version` 判断“这一轮有没有变化”：

```python
before = self.store.resource_version
kubelet.step()
endpoints.reconcile_all()
for controller in self.controllers:
    controller.reconcile_all()
gc.run()
return self.store.resource_version != before
```

一个 RayCluster 从创建到收敛，大致会产生这些写入：

| 轮次 | 写入 | resourceVersion | generation |
|---|---|---:|---:|
| 0 | create RayCluster | 1 | 1 |
| 1 | add finalizer | 2 | 1 |
| 2 | apply defaults | 3 | 2 |
| 2 | create autoscaler RBAC/SA（`_ensure_auxiliary` 先于 Service） | 4–6 | 2 |
| 2 | create head Service | 7 | 2 |
| 2 | create head Pod | 8 | 2 |
| 2 | create worker Pods | 9–10 | 2 |
| 2 | write status | 11 | 2 |
| 3 | kubelet schedules head/worker | 12–14 | 2 |
| 3 | EndpointSlice | 15 | 2 |
| 3 | status（Ready） | 16 | 2 |
| 4 | 无变化 | 16 | 2 |

要点：

- **每次写入都会增加 resourceVersion**，但 `generation` 只在 spec 变化时增加；
- status 写入不改变 generation；
- finalizer/annotation 等 metadata 写入**不增加** generation；默认值如果写回 `spec` 则增加 generation（MiniKubeRay 的 store 按 spec 是否变化判断）；
- `settle()` 反复 `run_once`，直到没有变化；
- 如果某一轮还在创建 Pod，下一轮会看到新对象，继续调和。

### 21.13.1 为什么“收敛后不再写”

`Controller.ensure` 会剥离 `resourceVersion`/`uid`/`creationTimestamp`/`status` 后比较；相同就不写。`_commit_status` 也比较除 `lastUpdateTime` 外的 status。这样：

- 不会产生无意义的 watch 事件；
- GitOps 不会看到持续漂移；
- 测试可以断言 `run_once()` 返回 `False`。

### 21.13.2 调试资源版本

```python
before = operator.store.resource_version
operator.run_once()
print("rv", before, "->", operator.store.resource_version)
```

如果 rv 一直增长：

- 某处每次都在写不同内容；
- 时间戳/随机值进入了对象；
- status 的 `lastUpdateTime` 没有排除；
- 子资源 selector/端口顺序不稳定。

这是“调和不收敛”的最常见原因。

## 21.14 调试控制器的五个手法

### 21.14.1 看“这一轮改了什么”

```python
before = {kind: operator.store.list(kind) for kind in kinds}
operator.run_once()
after = {kind: operator.store.list(kind) for kind in kinds}
# 对比 canonical_json，打印新增/修改/删除
```

不要只看返回值；要定位具体对象和字段。

### 21.14.2 冻结时间

```python
clock = ManualClock()
# 不调用 advance，所有超时都不会触发
```

如果 bug 只在时间推进后出现，用 `step(n, advance=...)` 精确复现。

### 21.14.3 只跑一个控制器

```python
operator.raycluster.reconcile_all()
```

跳过 kubelet/其他控制器，隔离问题。

### 21.14.4 打开事件

```python
for event in store.list(C.KIND_EVENT):
    print(event["reason"], event.get("message"))
```

事件往往比 status 更早暴露问题（例如创建 Pod 失败）。

### 21.14.5 断言“不写”

```python
assert operator.run_once() is False
```

收敛后仍返回 True，说明有对象在持续变化；这是最常见的“隐性 bug”。

### 21.14.6 常见误区

- 用 `time.sleep` 等异步：测试会 flaky；
- 只看最终状态：错过中间错误；
- 直接改 store 里的对象：绕过 resourceVersion/事件；
- 在控制器里捕获所有异常：错误被吞掉；
- 把 `lastUpdateTime` 纳入比较：永远不收敛。

## 21.15 小结

- WorkQueue/RateLimiter/Controller 提供了真实控制器的骨架。
- RayCluster reconcile 的顺序、spec hash、Pod 差异比较、status 计算是核心；真实 operator 的 scale expectations 在 MiniKubeRay 中由“每轮全量对比 + 确定性命名”替代。
- Builders 负责把 CR 变成 Pod/Service。
- 模拟调度器/kubelet 让 Pod 生命周期可控。
- Autoscaler 通过 replicas + workersToDelete 参与调和。

下一章拆 RayJob/RayService/RayCronJob 的状态机。

→ [第 22 章 实现：RayJob、RayService 与 RayCronJob](kuberay教程-22-实现-RayJob、RayService与RayCronJob.md)
