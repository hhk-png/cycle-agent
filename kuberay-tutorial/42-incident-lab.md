# 第 42 章 可运行故障实验室：用 MiniKubeRay 复现生产事故

第 37 章给了 14 个生产故障案例，但“读案例”和“亲手让它坏一次”是两回事。本章把其中最典型的 11 个场景做成可运行实验：`examples/incident_lab.py` 用确定性时钟、故障注入注解和真实的控制器代码，断言控制器在失败路径上的行为。

```bash
python examples/incident_lab.py
python examples/incident_lab.py --list
python examples/incident_lab.py --scenario worker-crash
```

每个场景都是独立的：创建对象、推进时间、断言状态，不依赖网络/Kubernetes/Ray。

## 42.1 为什么需要“可运行”的故障实验室

生产故障的难点不是“知道应该看哪里”，而是：

- 故障发生时，信号是否可见？
- 控制器会不会自愈？多久？
- 自愈失败时，状态是否停在可诊断的位置？
- 重试/清理是否有幂等性？

这些问题可以用控制面模型精确验证。实验室的价值：

1. **可重复**：同一个场景每次结果一致；
2. **可断言**：不是“看起来好了”，而是检查 status/对象/事件；
3. **可回归**：改控制器后，跑一遍实验室就知道有没有破坏失败路径；
4. **可教学**：每个场景对应一个真实故障模式。

> 边界：实验室验证的是**控制面状态机**，不是真实 Ray 进程、网络、存储或 GPU。真实集群实验见第 26 章，生产案例见第 37 章。

## 42.2 实验装置

### 42.2.1 确定性时钟

所有控制器只通过 `Clock` 取时间。实验室使用 `ManualClock`：

```python
from minikuberay.clock import ManualClock

clock = ManualClock()          # 固定从 2026-01-01T00:00:00Z 开始
clock.advance(5)               # 推进 5 秒，不 sleep
```

这意味着：

- TTL、deadline、idle timeout、Serve 部署延迟都可以精确控制；
- 同一场景在任何机器上结果一致；
- 不需要等待，也不会 flaky。

### 42.2.2 故障注入注解

MiniKubeRay 通过 Pod/RayJob 注解注入故障：

| 注解 | 作用 | 作用对象 |
|---|---|---|
| `minikuberay.io/crash-after-seconds` | Pod 运行 N 秒后 Failed | Pod template |
| `minikuberay.io/job-duration-seconds` | Ray job 运行时长 | RayJob metadata |
| `minikuberay.io/job-exit-code` | Ray job 退出码 | RayJob metadata |
| `minikuberay.io/serve-deploy-seconds` | Serve 部署延迟 | RayService metadata |
| `minikuberay.io/pod-start-delay-seconds` | Pod 启动延迟 | Pod template |
| `minikuberay.io/job-complete-after-seconds` | Job Pod 完成延迟 | Job/Pod |

这些注解以 `minikuberay.io/` 开头，真实 KubeRay 会忽略它们，不会污染生产清单。

### 42.2.3 断言什么

每个场景至少断言三层：

```text
对象层：Pod/Job/Service 是否存在、数量、UID 是否变化
状态层：status.state / conditions / jobDeploymentStatus
事件层：是否发出 CreatedWorkerPod / AutoscalerScaleDown / ValidationFailed
```

只断言“最终 Ready”不够；很多 bug 是“中间状态卡住”或“重复创建”。

## 42.3 场景 1：worker 崩溃后重建

**对应案例**：第 13 章的 worker 故障语义。第 37 章案例 9 是“多主机 worker 反复重建”，不是单 host 崩溃；不要把它当成同一故障。

```bash
python examples/incident_lab.py --scenario worker-crash
```

配置：

```yaml
workerGroupSpecs:
  - groupName: workers
    replicas: 2
    template:
      metadata:
        annotations:
          minikuberay.io/crash-after-seconds: "5"
```

观察：

- 5 秒后 worker Pod 变成 `Failed`；
- 控制器删除 Failed Pod，并按索引重建；
- 新 Pod 的 UID 与旧的不同；
- 集群回到 `ready`，worker 数量仍是 2（不是 1）。

断言：

```text
workers=2 replaced=2 ready=2 state=ready
```

**关键点**：崩溃不能让副本数“少一个”。控制器必须区分“期望副本数”和“当前可用数”。

## 42.4 场景 2：head 崩溃（无 GCS FT）

**对应案例**：第 37 章案例 2。

```bash
python examples/incident_lab.py --scenario head-crash
```

配置：

```yaml
headGroupSpec:
  template:
    metadata:
      annotations:
        minikuberay.io/crash-after-seconds: "5"
```

观察：

- head Pod 变成 `Failed`；
- 控制器删除并重建 head；
- worker 可能被重建/重新注册；
- 集群重新回到 `ready`，但**正在运行的作业状态不会恢复**（没有 GCS FT）。

断言：

```text
head_replaced=True state=ready workers=1
```

**关键点**：控制面能重建 Pod，不等于 Ray 运行时能恢复。GCS FT 解决的是元数据恢复，不是任务状态恢复（第 13 章）。

## 42.5 场景 3：RayJob 失败与 backoff

**对应案例**：第 10.5 节的重试/超时语义。第 37 章案例 4 只讲“卡在 Initializing”，不覆盖 backoff。

```bash
python examples/incident_lab.py --scenario rayjob-retry
```

配置：

```yaml
spec:
  entrypoint: python -c "raise SystemExit(1)"
  submissionMode: K8sJobMode
  shutdownAfterJobFinishes: true
  backoffLimit: 1
metadata:
  annotations:
    minikuberay.io/job-duration-seconds: "1"
    minikuberay.io/job-exit-code: "1"
```

观察：

- 第一次作业失败 -> `Retrying`；
- 删除旧集群，创建新集群重跑；
- 第二次仍失败 -> `Failed`（`failed=2`）；
- `jobStatus=FAILED`、`jobDeploymentStatus=Failed`。

断言：

```text
deployment=Failed job=FAILED failed=2
```

**关键点**：

- `backoffLimit=1` 表示最多重试 1 次，总尝试 2 次；
- 每次重试创建新集群，entrypoint 必须幂等；
- `DeadlineExceeded` 不重试（见场景 4）。

## 42.6 场景 4：activeDeadlineSeconds

**对应案例**：第 10.5.2 节 `activeDeadlineSeconds`；第 37 章没有对应案例。

```bash
python examples/incident_lab.py --scenario deadline
```

配置：

```yaml
spec:
  entrypoint: python -c "import time; time.sleep(3600)"
  activeDeadlineSeconds: 5
metadata:
  annotations:
    minikuberay.io/job-duration-seconds: "3600"
```

观察：

- 作业运行超过 5 秒；
- 控制器停止作业；
- `jobDeploymentStatus=Failed`、`reason=DeadlineExceeded`；
- 不进入 `Retrying`。

断言：

```text
deployment=Failed reason=DeadlineExceeded
```

**关键点**：deadline 是硬终止，必须覆盖在 `Running` 和 `Initializing` 两个阶段；否则集群起不来时会永远卡住。

## 42.7 场景 5：Serve 部署慢导致长期不 Ready

**对应案例**：第 37 章案例 6、第 28 章冷启动。

```bash
python examples/incident_lab.py --scenario serve-slow-deploy
```

配置：

```yaml
metadata:
  annotations:
    minikuberay.io/serve-deploy-seconds: "30"
```

观察：

- 前 30 秒：RayService `Ready=False`、`numServeEndpoints=0`；
- Serve 应用 `DEPLOYING`；
- 30 秒后：应用 `RUNNING`、Pod 的 `ray.io/serve=true`、endpoint 出现；
- `Ready=True`。

断言：

```text
early_ready=False late_ready=True endpoints=2
```

**关键点**：

- “Pod Running”不等于“Serve Ready”；
- 模型加载/权重下载是冷启动的主要成本；
- 扩容/升级的 SLO 要把这段时间算进去。

## 42.8 场景 6：升级中回滚

**对应案例**：第 11 章、第 37 章案例 5/6。

```bash
python examples/incident_lab.py --scenario serve-rollback
```

过程：

1. 创建 RayService，等 active Ready；
2. 修改镜像 -> 创建 pending 集群；
3. 在 pending 还没 Ready 时把镜像改回；
4. 控制器检测到 `desired hash == active revision`，删除 pending；
5. active 保持不动，流量不切换。

断言：

```text
pending_seen=True pending_after=- active_stable=True
```

**关键点**：

- 回滚不是“再部署一次”，而是“取消 pending”；
- 回滚窗口受 `rayClusterDeletionDelaySeconds` 限制；
- 如果流量已经切到 pending，回滚需要把权重切回。

## 42.9 场景 7：GCS FT 清理卡住与超时

**对应案例**：第 37 章案例 8。

```bash
python examples/incident_lab.py --scenario gcs-ft-cleanup-timeout
```

配置：

```yaml
metadata:
  annotations:
    ray.io/gcs-ft-deletion-timeout: "5"
spec:
  gcsFaultToleranceOptions:
    backend: redis
    redisAddress: redis.default.svc.cluster.local:6379
```

观察：

- 删除 RayCluster 后对象进入 `Terminating`；
- 控制器先删除 Ray 节点 Pod，再创建 `<cluster>-redis-cleanup` Job；
- 如果 Job 卡住，finalizer 不会移除；
- 超过 5 秒后控制器强制移除 finalizer，并发 Warning 事件（可能留下 Redis 垃圾）。

断言：

```text
finalizers=[]（或 RayCluster deleted）
```

**关键点**：

- finalizer 是保护，不是障碍；
- 超时释放是“带风险的兜底”，要配合人工清理；
- 生产要监控 finalizer 停留时间。

## 42.10 场景 8：多主机 replica 整组替换

**对应案例**：第 37 章案例 9、第 5 章多主机。

```bash
python examples/incident_lab.py --scenario multihost-replacement
```

配置：

```yaml
workerGroupSpecs:
  - groupName: workers
    replicas: 2
    numOfHosts: 2
```

观察：

- 2 个 replica × 2 host = 4 个 Pod；
- 删除其中一个 host；
- 控制器删除同 replica 的另一个 host；
- 重新创建整个 replica（两个 Pod 的 UID 都变化）；
- 最终仍是 4 个 Pod、2 个 replica。

断言：

```text
workers=4 replicas=2 whole_replica_replaced=True
```

**关键点**：

- replica 是原子单位，不能按 Pod 扩缩；
- 缩容删整个 replica，不会留下半个切片；
- 节点池必须能同时放下整个 replica。

## 42.11 场景 9：非法 spec 与恢复

**对应案例**：第 7.3 节的校验与拒绝路径。第 37 章案例 7 是 CRD 未升级导致字段被裁剪，不是 spec 校验失败。

```bash
python examples/incident_lab.py --scenario invalid-recover
```

配置：

```yaml
workerGroupSpecs:
  - groupName: workers
    minReplicas: 5
    maxReplicas: 2
```

观察：

- 控制器不创建 Pod；
- `status.reason=ValidationFailed`，condition `RayClusterProvisioned=False`；
- 修正 spec 后，控制器清除失败原因，重新创建 Pod；
- 集群回到 `ready`。

断言：

```text
before=ValidationFailed after_state=ready after_reason=-
```

**关键点**：

- 非法对象可以进入 etcd（KubeRay 不用 webhook）；
- 校验错误必须通过 status/event 可见；
- 修复后要能自动恢复，不能卡在 `ValidationFailed`。

## 42.12 场景 10：clusterSelector 不删除共享集群

**对应案例**：第 10.7 节 `clusterSelector`；第 37 章没有对应案例。

```bash
python examples/incident_lab.py --scenario selector-no-delete
```

配置：

```yaml
spec:
  clusterSelector:
    ray.io/cluster: shared
  submissionMode: HTTPMode
```

观察：

- RayJob 复用 `shared` 集群，不创建新集群；
- 作业完成后，共享集群仍然存在；
- 即使 RayJob 有 `shutdownAfterJobFinishes`，也不能删除别人的集群（控制器会跳过共享集群的删除；这不是所有字段都会被 admission 拒绝，v1.7.1 主要拒绝 suspend/Sidecar/backoffLimit>0/DeleteCluster/DeleteWorkers）。

断言：

```text
job=Complete shared_cluster=True
```

**关键点**：

- 共享集群的容量/隔离由使用者负责；
- `clusterSelector` 与 `backoffLimit`/`suspend`/deletion rules 互斥；
- 不要让一个作业的清理策略越权删除共享资源。

## 42.13 场景 11：autoscaler 扩容与 idle 缩容

**对应案例**：第 9 章、第 34 章。

```bash
python examples/incident_lab.py --scenario autoscaler-idle
```

观察：

- 初始 `replicas=0`；
- 注入 6 CPU 的 pending demand -> `replicas=3`（每副本 2 CPU）；
- 清除 demand，推进超过 `idleTimeoutSeconds=15` -> `replicas=0`；
- 缩容通过 `workersToDelete` 指定牺牲者。

断言：

```text
replicas_up=3 replicas_down=0
```

**关键点**：

- 扩容看的是待调度资源，不是 CPU 利用率；
- 缩容要等 idle timeout，且不能低于 `minReplicas`；
- 随机删除 Pod 会破坏任务，autoscaler 必须指定 victim。

## 42.14 一次运行全部场景

```text
$ python examples/incident_lab.py
[PASS] worker-crash               workers=2 replaced=2 ready=2 state=ready
[PASS] head-crash                 head_replaced=True state=ready workers=1
[PASS] rayjob-retry               deployment=Failed job=FAILED failed=2
[PASS] deadline                   deployment=Failed reason=DeadlineExceeded
[PASS] serve-slow-deploy          early_ready=False late_ready=True endpoints=2
[PASS] serve-rollback             pending_seen=True pending_after=- active_stable=True
[PASS] gcs-ft-cleanup-timeout     timeout_event=True RayCluster deleted
[PASS] multihost-replacement      workers=4 replicas=2 whole_replica_replaced=True
[PASS] invalid-recover            before=ValidationFailed after_state=ready
[PASS] selector-no-delete         job=Complete shared_cluster=True
[PASS] autoscaler-idle            replicas_up=3 replicas_down=0

11/11 scenarios passed (deterministic ManualClock)
```

退出码：

- 全部通过：0；
- 任一失败：1；
- 未知场景：2。

## 42.15 如何扩展实验室

### 42.15.1 新增一个场景

1. 在 `examples/incident_lab.py` 写一个 `scenario_xxx()`，返回 `Result`；
2. 用 `Lab` 创建对象、推进时间、断言；
3. 注册到 `SCENARIOS`；
4. 在 `README`/本章加一行说明；
5. 跑 `python examples/incident_lab.py --scenario xxx`。

### 42.15.2 新增故障注入

如果要模拟新的故障，优先加“注解 + 运行时检查”，不要在生产路径里加 `if test`：

```python
annotations = pod["metadata"].get("annotations") or {}
if annotations.get("minikuberay.io/network-delay-ms"):
    # 在模拟层记录延迟，而不是真的 sleep
    ...
```

### 42.15.3 从实验室到真实集群

实验室验证控制面行为后，到第 26 章的 kind 集群上用真实故障注入（删 Pod、填 `/dev/shm`、断网）验证数据面；两者互补，不能互相替代。

## 42.16 小结

- 故障实验室把“读案例”变成“跑状态机”。
- 确定性时钟让 TTL/deadline/idle/部署延迟可精确复现。
- 断言要覆盖对象、状态、事件三层，不只看最终 Ready。
- 11 个场景覆盖了 head/worker 故障、RayJob 重试/超时、RayService 慢部署/回滚、GCS FT 清理、多主机、校验恢复、共享集群和 autoscaler。
- 实验室是控制面回归测试，真实数据面仍要在 kind/生产集群验证。

← [第 41 章 控制面规模化](41-control-plane-at-scale.md)

→ [第 43 章 从 Issue 到 Release](43-contribution-release-engineering.md)
