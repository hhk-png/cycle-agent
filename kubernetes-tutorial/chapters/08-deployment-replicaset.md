# 第 8 章 Deployment 与 ReplicaSet：滚动发布的艺术

## 8.1 三层关系

```text
Deployment (期望副本数、发布策略、模板)
    │ owns
    ▼
ReplicaSet (某个模板版本的副本数)
    │ owns
    ▼
Pod (实际运行的容器组)
```

- **Deployment**：管理发布流程和版本历史。
- **ReplicaSet**：保证某个模板版本的 Pod 数量。
- **Pod**：实际运行单元。

为什么不直接用 ReplicaSet？因为 ReplicaSet 不会滚动更新。修改它的模板只会影响新 Pod，旧 Pod 不会被替换。Deployment 通过创建新 RS、逐步缩放新旧 RS 来实现发布。

## 8.2 Pod 模板哈希

Deployment 对 `spec.template` 做哈希，生成 `pod-template-hash`：

```text
web-7d9f8c6b5
    └── hash
```

这个 hash 被加到：

- 新 ReplicaSet 的名字后缀。
- ReplicaSet 的 selector。
- Pod 模板的 labels。

作用：

- 不同模板版本对应不同 RS，不会互相认领 Pod。
- 滚动更新时可以同时存在新旧 RS。
- 回滚时可以找到旧模板。

hash 只对模板计算，不包括 replicas。所以只改副本数不会创建新 RS。

## 8.3 滚动更新参数

```yaml
spec:
  replicas: 10
  strategy:
    type: RollingUpdate
    rollingUpdate:
      maxSurge: 25%
      maxUnavailable: 25%
```

- `maxSurge`：最多超出期望副本数多少。可以是整数或百分比（向上取整）。
- `maxUnavailable`：最多有多少副本不可用。百分比向下取整。
- 两者不能同时为 0。

其他常用字段：

| 字段 | 默认 | 作用 |
|---|---|---|
| `replicas` | 1 | 期望副本数 |
| `progressDeadlineSeconds` | 600 | 超过后 `Progressing=False/ProgressDeadlineExceeded` |
| `revisionHistoryLimit` | 10 | 保留多少个旧 ReplicaSet（回滚用） |
| `minReadySeconds` | 0 | Pod Ready 后还要稳定多久才算 available |
| `paused` | false | 暂停滚动，只改模板不推进 |
| `selector` | 必填 | 创建后不可变 |

以 replicas=10、surge=25%（3）、unavailable=25%（2）为例：

```text
期望 10
最多总 Pod 13
最少可用 8
```

滚动过程：

```text
旧 RS 10, 新 RS 0
→ 新 RS 3, 旧 RS 10（总 13）
→ 新 Ready 1, 旧缩到 9（可用仍 >= 8）
→ ...
→ 新 RS 10, 旧 RS 0
```

真实算法比这更复杂，要处理：

- 新 Pod 未 Ready 时不能缩旧。
- 旧 RS 的 available 统计有延迟。
- 多个旧 RS（多次发布未完成）。
- `minReadySeconds`。
- 暂停（paused）。

## 8.4 Deployment 状态

```yaml
status:
  observedGeneration: 5
  replicas: 10
  updatedReplicas: 10
  readyReplicas: 10
  availableReplicas: 10
  unavailableReplicas: 0
  conditions:
    - type: Available
      status: "True"
      reason: MinimumReplicasAvailable
    - type: Progressing
      status: "True"
      reason: NewReplicaSetAvailable
```

发布完成时 `Progressing=True`，reason 为 `NewReplicaSetAvailable`（表示新 RS 已可用），不是 False。失败时才是：

```yaml
conditions:
  - type: Progressing
    status: "False"
    reason: ProgressDeadlineExceeded
    message: ReplicaSet "web-7d9c" has timed out progressing.
```

判读：

- `observedGeneration < metadata.generation`：控制器还没处理最新 spec。
- `updatedReplicas < replicas`：还有旧 Pod。
- `availableReplicas < replicas - maxUnavailable`：可用性不足。
- `Progressing=False, reason=ProgressDeadlineExceeded`：发布超时（默认 `progressDeadlineSeconds: 600`）。
- `Available=False`：可用副本不足。

`kubectl rollout status` 就是轮询这些字段。

## 8.5 ReplicaSet 控制器

ReplicaSet 的核心逻辑非常朴素：

```text
desired = spec.replicas
current = 拥有 ownerReference 且未终止的 Pod 数
if current < desired: create (desired - current) pods
if current > desired: delete (current - desired) pods
update status
```

细节：

- **收养（adoption）**：没有 controller owner 但匹配 selector 的 Pod 会被收养。
- **释放（release）**：selector 改变时，旧 Pod 可能被释放（实际 selector 不可变）。
- **删除顺序**：优先删除未调度、Pending、较新的 Pod；真实控制器有更复杂的排序。
- **status**：replicas、fullyLabeledReplicas、readyReplicas、availableReplicas、observedGeneration。

MiniK8s 的 `ReplicaSetController` 实现了创建、删除、收养和状态汇总。测试 `test_replicaset_adopts_orphan_pods` 验证收养。

## 8.6 滚动更新算法（简化版）

MiniK8s 的 `DeploymentController._rollout` 使用如下策略：

```text
new_ready = min(new_rs.spec.replicas, new_rs.status.availableReplicas)
old_total = sum(old_rs.spec.replicas)
old_floor = max(0, desired - maxUnavailable - new_ready)
old_target = min(old_total, old_floor)
new_target = min(desired, max(new_replicas, desired + maxSurge - old_target))
scale old to old_target
scale new to new_target
```

直觉：

- 新副本 Ready 多少，旧副本就可以退多少，同时保证可用数不低于 `desired - maxUnavailable`。
- 新副本可以占用 `maxSurge` 的额外容量。
- 每轮只推进一小步，下一轮根据最新 status 继续。

真实 Deployment 控制器还处理：

- Recreate 策略：先缩旧到 0，再扩新。
- 多旧 RS 的按比例缩容。
- `minReadySeconds`：Pod Ready 后还要稳定一段时间才算 available。
- `progressDeadlineSeconds`：超时设置 Progressing=False。
- `paused`：暂停发布。
- 冲突重试与事件。

## 8.7 Recreate 策略

```yaml
strategy:
  type: Recreate
```

先删除所有旧 Pod，再创建新 Pod。会有停机窗口。

适用：

- 不能同时运行两个版本（数据库 schema 不兼容、单例锁）。
- 开发/测试环境追求简单。

不适用：

- 需要零中断的服务。

## 8.8 发布失败与回滚

常见失败：

- 新镜像拉取失败 → 新 Pod ImagePullBackOff → 发布卡住。
- 新版本启动即崩 → CrashLoopBackOff。
- readiness 永远不通过 → 新 Pod 不 Ready，旧 Pod 不被缩。
- 资源不足 → 新 Pod Pending。

Deployment 默认不会自动回滚。`progressDeadlineSeconds` 只是标记失败，不会自动恢复。

回滚：

```bash
kubectl rollout undo deployment/web
kubectl rollout undo deployment/web --to-revision=3
```

回滚也是滚动更新：把旧模板写回 Deployment，创建/扩展对应 RS，逐步替换当前版本。它不能修复“旧版本也依赖了已删除的数据库列”这类外部状态问题。

## 8.9 minReadySeconds 与可用性

```yaml
spec:
  minReadySeconds: 10
```

Pod Ready 后还要持续 10 秒才算 available。防止“刚启动就 Ready，但 2 秒后崩溃”的 Pod 被计入可用。

与 readinessProbe 配合：

- readiness 负责“能否接流量”。
- minReadySeconds 负责“稳定多久才算发布成功”。

对关键服务，建议设置 5～30 秒，视启动特性而定。

## 8.10 Paused 与金丝雀

```bash
kubectl rollout pause deployment/web
# 修改模板、观察新 RS
kubectl rollout resume deployment/web
```

暂停后，Deployment 不会推进滚动，但可以创建新 RS。常用于：

- 先创建一个新版本 Pod 做验证。
- 配合手工流量切换做简单金丝雀。

生产级金丝雀通常用 Argo Rollouts、Flagger、Service Mesh 或 Gateway API，而不是手工 pause。

## 8.11 水平扩容与 HPA 的交互

HPA 修改 Deployment 的 `spec.replicas`。Deployment 再把副本数分配给当前 RS。

注意：

- 发布期间 HPA 可能同时调整副本数，导致滚动计算复杂。
- GitOps 如果也管理 replicas，会与 HPA 冲突（字段所有权问题）。
- 建议：HPA 管理 replicas 时，GitOps 忽略该字段，或使用 HPA 的 `scaleTargetRef` 精确绑定。

## 8.12 常见坑

**selector 写错**：Deployment 创建成功但 Pod 不被管理，或误管其他 Pod。

**没有 readinessProbe**：容器进程启动就算 Ready，流量打到还没初始化完的实例。

**maxUnavailable 太大**：发布期间容量不足，雪崩。

**maxSurge 太大**：节点资源不足，新 Pod Pending，发布卡住。

**revisionHistoryLimit 太小**：无法回滚到更早版本。

**镜像 tag 可变**：同一个 tag 指向不同 digest，回滚不可复现。使用 digest。

**资源 requests 太小**：滚动时新 Pod 调度不上去，旧 Pod 又不敢缩。

**PDB 与滚动更新冲突**：PDB 限制自愿驱逐，不限制 Deployment 缩容；不要误以为 PDB 能保护发布。

## 8.13 MiniK8s 实操

```bash
python -m mini_k8s.cli apply -f examples/02-rollout.yaml
python -m mini_k8s.cli rollout status deployment rollout-demo
python -m mini_k8s.cli set image deployment/rollout-demo app=demo:v2
python -m mini_k8s.cli get rs
python -m mini_k8s.cli rollout history deployment rollout-demo
python -m mini_k8s.cli rollout undo deployment rollout-demo --to-revision=1
```

观察：

- 新 RS 出现，旧 RS 保留但 replicas=0。
- Deployment status 的 updated/available 逐步追上。
- 回滚后 revision 继续递增（不是回到旧编号）。

## 本章小结

- Deployment 通过多个 ReplicaSet 实现版本管理与滚动发布。
- pod-template-hash 隔离不同模板的 Pod。
- maxSurge/maxUnavailable 共同约束发布期间的容量。
- 状态字段是判断发布是否成功的依据；rollout status 只是轮询这些字段。
- 回滚是另一次发布，不修复外部状态。

## 延伸阅读

- [第 9 章 StatefulSet](09-statefulset.md)
- [第 20 章 自动伸缩](20-autoscaling.md)
- [第 36 章 调度器与控制器实现](36-scheduler-controllers.md)
