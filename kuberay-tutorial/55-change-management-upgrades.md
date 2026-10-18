# 第 55 章 变更管理：升级、重启与不丢状态

前 54 章分别讲了字段、控制器、故障和运维。本章把它们收束成一个日常问题：**一个正在承载训练的 RayCluster，怎么改才不会把正在跑的东西弄丢？**

答案不是“小心一点”，而是三件可验证的事：

1. 知道每个字段变化会让控制器做什么（spec hash 与 `upgradeStrategy`）；
2. 知道哪些操作会同时影响 head 与 worker（版本升级、模板变更）；
3. 知道失败时用什么证据回滚（status、Pod UID、Ray 状态、日志）。

本章的实验室 `examples/maintenance_lab.py` 会把这三件事跑成断言。

## 55.1 变更的三个平面

不要把所有“改东西”都叫升级。至少分成三个平面：

| 平面 | 例子 | 决策者 | 典型爆炸半径 | 回滚方式 |
|---|---|---|---|---|
| 声明式 spec | replicas、镜像、资源、`rayStartParams` | 用户 / GitOps | 取决于 hash 与策略 | 改回 spec + 等调和 |
| 控制面 | KubeRay operator、CRD、Helm values | 平台团队 | 所有 RayCluster | Helm 回滚 + CRD 前向兼容 |
| 数据面 | Ray 版本、Python 依赖、CUDA、驱动 | ML 平台 / 用户 | 单个集群或作业 | 新镜像 + 重建/蓝绿 |

三条规则：

- **一次只动一个平面**。operator 升级和 Ray 镜像升级同时做，出了问题无法归因。
- **数据面变更不能用控制面回滚解决**。把 CR 改回旧镜像不会让已经用新版本跑起来的作业回退。
- **CRD 升级通常不可逆**。字段可以加，不能假设可以安全删除；回滚 operator 不等于回滚 CRD。

## 55.2 一次 spec 变更会发生什么

KubeRay 不会对每个字段都做“原地修改”。Pod 是不可变的，绝大多数 spec 变化最终只有两种结果：**改子对象（Service/RBAC/Secret）**，或者**重建 Pod**。

### 55.2.1 变更影响矩阵

下表以 v1.7.1 的 RayCluster 控制器语义为准；具体字段是否进入 hash 以目标 tag 的源码为准。

| 变更 | 是否改 Pod 模板 | `Recreate` 下 | `None`/未设置 | 备注 |
|---|---|---|---|---|
| `metadata.labels/annotations`（CR 本身） | 否 | 不重建 | 不重建 | 只影响选择器/审计 |
| `workerGroupSpecs[].replicas` | 否 | 只增删 Pod | 只增删 Pod | hash 排除 autoscaler 拥有的字段 |
| `minReplicas`/`maxReplicas` | 否 | 只影响边界 | 只影响边界 | 不触发重建 |
| `scaleStrategy.workersToDelete` | 否 | 只删指定 Pod | 只删指定 Pod | 删除后必须清空 |
| `numOfHosts` | 是 | 整组替换 | 新 Pod 用新模板 | 多主机组按 replica 成组 |
| 镜像 / `rayVersion` | 是 | 全量重建 | **混合版本** | 最危险的组合 |
| `resources` / `env` / `command` / `args` | 是 | 全量重建 | 新 Pod 用新模板 | 资源变化会改变调度 |
| `rayStartParams` | 是 | 全量重建 | 新 Pod 用新模板 | 逻辑资源与 requests 要对齐 |
| Pod template 的 labels/annotations | 是 | 全量重建 | 新 Pod 用新模板 | 常被忽略的 hash 输入 |
| `autoscalerOptions` | 是 | 全量重建 | 新 Pod 用新模板 | sidecar 参数在 head 上 |
| `gcsFaultToleranceOptions` | 是 | 全量重建 | 新 Pod 用新模板 | 还要准备 Redis/PVC |
| `authOptions` / `tlsOptions` | 是 | 全量重建 | 新 Pod 用新模板 | 同时更新 Secret/证书 |
| `networkPolicy` | 是 | 全量重建 | 新 Pod 用新模板 | 同时创建 NP 对象 |
| `historyServerOptions` | 是 | 全量重建 | 新 Pod 用新模板 | collector sidecar 注入 |
| `headGroupSpec.serviceType` / `headService` | 否 | 更新 Service | 更新 Service | 端口/选择器要一起看 |
| `enableInTreeAutoscaling` | 是 | 全量重建 | 新 Pod 用新模板 | head 增加 autoscaler sidecar |
| `suspend` | 特殊 | 删所有 Pod | 删所有 Pod | 保留 CR，见第 7 章 |
| `upgradeStrategy` 本身 | 是（进入 hash） | 可能触发一次重建 | 不重建 | 从 `None` 切到 `Recreate` 要预期一次全量 |
| `managedBy` | — | 拒绝/忽略 | 拒绝/忽略 | 不可变字段，交给外部控制器 |

“改 Pod 模板”不等于“立刻重建”：只有 `upgradeStrategy: Recreate` 才会在 hash 变化时删除现有 Pod。`None` 的语义是“**不主动替换**”，不是“拒绝变更”。

### 55.2.2 为什么 replicas 不进入 hash

如果 `replicas` 进入 hash，那么每次自动扩缩容都会让所有 Pod 被判定为“模板过期”，在 `Recreate` 下直接全量重启。这会把一次正常的扩容变成一次集群重启。

KubeRay 因此把 autoscaler 拥有的字段排除在 hash 之外：

- `replicas`；
- `minReplicas` / `maxReplicas`；
- `scaleStrategy.workersToDelete`。

MiniKubeRay 的 `_spec_hash` 明确做了同样的排除（`minikuberay/controllers/raycluster.py`）：

```python
payload = deep_copy(cluster["spec"])
for group in payload.get("workerGroupSpecs") or []:
    group.pop("replicas", None)
    group.pop("minReplicas", None)
    group.pop("maxReplicas", None)
    group.get("scaleStrategy", {}).pop("workersToDelete", None)
return stable_hash(payload, 16)
```

这带来一个重要推论：**缩容/扩容永远不应该触发 Recreate**。如果你的集群在扩缩容时反复重建，先检查 hash 是否被不稳定字段污染（时间戳、随机注解、默认值漂移）。

### 55.2.3 hash 存在哪里

真实 KubeRay 把 Recreate 的 hash 记录在 head Pod 的注解 `ray.io/upgrade-strategy-recreate-hash` 中；Pod 上还有 `ray.io/kuberay-version` 标记，用于区分“模板真的变了”和“只是 operator 升级了”，避免后者导致全部集群重建。

MiniKubeRay 为了在 head 被删除后仍能保留基线，把 hash 持久化在 RayCluster 的注解 `minikuberay.io/spec-hash` 上，并在 Pod 标签 `ray.io/spec-hash` 上保留一份。这是教学差异，但解决的正是同一个问题：

> 如果基线只存在于 head Pod 上，删除 head 后新一轮调和会认为“没有旧 hash”，从而跳过本应发生的 Recreate。

生产排障时可以直接看：

```bash
kubectl get pod -l ray.io/cluster=<name>,ray.io/node-type=head \
  -o jsonpath='{.items[0].metadata.annotations}'
```

## 55.3 `Recreate` 与 `None`：两种完全不同的升级观

### 55.3.1 `Recreate`：一次性替换

```yaml
spec:
  upgradeStrategy:
    type: Recreate
```

行为：

1. 计算当前 spec hash；
2. 与记录的 hash 比较；
3. 不同则删除**所有** head/worker Pod；
4. 下一轮调和用新模板重新创建。

特点：

- 简单、确定、不会出现混合版本；
- **有停机**：head 删除后 Dashboard/GCS/autoscaler 都不可用，worker 也会被删；
- 适合开发集群、可重试的 RayJob 集群、维护窗口内的整集群升级；
- 不适合“必须保持在线”的 RayService active 集群。

### 55.3.2 `None`：只影响未来

```yaml
spec:
  upgradeStrategy:
    type: None   # 或省略
```

行为：

- 现有 Pod 不动；
- 因扩容、故障重建、手动删除而**新建**的 Pod 使用当前模板；
- 旧 Pod 继续用旧模板，直到它们自己消失；
- 控制器**不会停止调和**：Service/RBAC/status 仍会修复，缺失的 head/worker 仍会按新模板补回。

这非常适合“先验证一个新镜像，再逐步替换”的手工流程，但也制造了一个经典陷阱：**混合版本集群**。

Ray 要求 head 与 worker 使用相同版本。`None` 下把镜像从 2.59 改到 2.60，然后扩容一个新 worker，新 worker 很可能无法加入旧 head：

```text
raylet: version mismatch: head=2.59.0 worker=2.60.0
```

因此：

- 升 Ray 版本不要用 `None` + 扩容的方式“滚动”；
- 要滚动，必须自己按 worker group 逐组删除，并接受“新旧 worker 不能同时加入”的现实；
- 最稳妥的跨版本方式仍是**新集群 + 迁移作业/流量**，而不是原地滚动。

### 55.3.3 没有 `RollingUpdate`

截至 v1.7.1，RayCluster 的 `upgradeStrategy.type` 只有 `Recreate` 和 `None`，没有 Deployment 式的 `RollingUpdate`。原因不是实现偷懒，而是 Ray 的版本/集群身份约束：

- head 只有一个，无法“滚动”；
- worker 与 head 必须同版本；
- GCS 元数据绑定集群身份，不能简单地把新 head 挂到旧 worker 上。

所以“滚动升级 RayCluster”通常意味着：

1. 新建一个 RayCluster（新版本）；
2. 把 RayJob/RayService 指向新集群；
3. 观察新集群健康；
4. 删除旧集群。

RayService 的 `NewCluster`/增量升级就是把这个过程产品化；RayJob 则通过重新提交实现。

## 55.4 升级 Ray 版本的完整顺序

### 55.4.1 先确认“版本”有四个来源

| 来源 | 位置 | 作用 |
|---|---|---|
| `spec.rayVersion` | CR | 控制器默认值/校验/autoscaler 版本推断 |
| 镜像 tag/digest | 容器 image | 实际运行的 Ray |
| Python `ray` 包 | runtime_env/镜像 | driver 与 worker 的客户端 |
| 客户端环境 | 提交机器/Notebook | `ray job submit`、SDK |

四者不一致时，最常见的是“镜像升级了，但提交端还是旧 SDK”，表现为序列化/协议错误。

### 55.4.2 升级检查表

```text
[ ] 新镜像与旧镜像的 Ray minor 版本差异已确认
[ ] Python minor 版本一致（3.11 vs 3.12 不能混）
[ ] CUDA/驱动/GPU 架构与节点池一致
[ ] rayVersion、镜像 tag、runtime_env 中的 ray 版本一致
[ ] 入口脚本/依赖在 staging 跑通
[ ] RayJob 用新模板重新提交，而不是复用旧 submitter
[ ] RayService 用 NewCluster 或增量升级，不原地改 active 集群
[ ] 共享 RayCluster 建新集群并迁移，不原地滚动
[ ] 旧集群保留到新集群通过验收
```

### 55.4.3 三种工作负载的升级路径

| 工作负载 | 推荐路径 | 停机 | 说明 |
|---|---|---|---|
| RayJob | 用新 `rayClusterSpec` 重新提交 | 作业级 | 旧作业跑完再删；新作业天然用新版本 |
| RayService | `NewCluster` 或增量升级 | 接近零 | active/pending 双集群，流量切换 |
| 共享 RayCluster | 新建集群 + 迁移 | 迁移窗口 | 不要依赖 `None` 混合版本 |

RayService 的升级细节见[第 11 章](11-rayservice.md)；这里只需要记住：**RayService 的升级单位是集群，不是 Pod**。

### 55.4.4 GCS FT 不是版本升级工具

GCS FT 解决的是“head 挂了，元数据还在”，不是“新旧 Ray 版本可以混跑”。即使 Redis 里保留了旧版本的元数据，新版本 head 也未必能兼容旧 worker 的协议。

所以：

- 同版本 head 故障恢复：GCS FT 有用；
- 跨版本升级：GCS FT 不能替代新集群；
- 跨版本回滚：不要把 Redis 里的状态当作版本回滚的依据。

## 55.5 head 维护：单点、状态与“按下去”

### 55.5.1 head 删除意味着什么

head Pod 承载：

- GCS（集群元数据）；
- Dashboard/Job Submission API；
- autoscaler sidecar；
- head Service 的 endpoints。

删除 head 后：

- worker 进程可能还在，但新任务无法提交；
- Dashboard 不可用；
- 没有 GCS FT 时，集群元数据随 head 消失，恢复后是一个“新集群”；
- 有 GCS FT 时，新 head 从 Redis 恢复元数据，worker 重新注册。

因此 head 维护不是“删掉等它自己回来”这么简单，要先决定：

1. 能不能接受一次 GCS 恢复？
2. 正在跑的 task/actor 是否可重试？
3. driver 在哪里？RayJob 的 submitter 是否会因 head 消失而失败？
4. 是否有 Redis/PVC 等外部依赖需要一起维护？

### 55.5.2 用注解把 head“按住”

KubeRay 支持注解：

```yaml
metadata:
  annotations:
    ray.io/disable-provisioned-head-restart: "true"
```

语义：当集群已经 provisioned 后，即使 head Pod 不存在，控制器也不自动创建新的 head。它适合：

- 计划内维护：先按住 head，避免控制器和你抢；
- 故障隔离：保留现场，等待人工确认；
- 迁移：先把 head 停掉，再处理存储/网络。

注意：

- 这是“按住”，不是“删除保护”；Pod 仍可能因节点故障消失；
- 维护完成后要移除注解，否则集群永远缺 head；
- 注解在 CR 上，不在 Pod 上；改完要等调和。

MiniKubeRay 实现了同样的行为，`examples/maintenance_lab.py` 会演示“按住 → head 不被重建 → 放开 → head 恢复”。

### 55.5.3 head 维护的推荐顺序

```text
1. 确认没有不可重试的 driver 正在 head 上运行
2. 记录当前状态：status、Pod UID、Ray 节点、运行中的 job
3. 若使用 GCS FT，确认 Redis/PVC 健康且最近有恢复演练
4. 给 CR 加 disable-provisioned-head-restart
5. 删除 head Pod（或等待节点维护）
6. 完成维护（镜像、节点、网络、存储）
7. 移除注解
8. 观察新 head：GCS 恢复、worker 重新注册、Dashboard/job API
9. 跑一条最小作业验证
```

不要在第 5 步之前删 head Service。Service 是稳定的 DNS/端口入口，删了会让 worker 的 `--address` 解析失败，恢复更慢。

## 55.6 worker 维护：按组、按名字、按证据

### 55.6.1 三种删除方式

| 方式 | 谁决定删哪个 | 是否受 PDB 保护 | 适用 |
|---|---|---|---|
| 改 `replicas` 缩容 | 控制器 | 否 | 非关键、无状态 worker |
| `scaleStrategy.workersToDelete` | 你/autoscaler | 否 | 精确删除、缩容 |
| `kubectl drain` / eviction | kubelet/eviction API | 是 | 节点维护 |

注意：**PDB 不会保护控制器直接删除的 Pod**。KubeRay 缩容走的是删除 Pod，不是 eviction API。第 52 章有完整时序。

### 55.6.2 精确删除一个 worker

```yaml
spec:
  workerGroupSpecs:
    - groupName: workers
      replicas: 3
      scaleStrategy:
        workersToDelete:
          - raycluster-sample-workers-1
```

控制器会：

1. 校验 Pod 属于该集群、该 group、node-type=worker；
2. 删除指定 Pod；
3. 从 `workersToDelete` 中移除；
4. 如果 `replicas` 仍大于当前数量，创建替代 Pod。

如果 `replicas` 同时降到 2，就不会创建替代；这正是“缩容”和“替换”的区别。

### 55.6.3 多主机组

`numOfHosts > 1` 时，一个 replica 对应多个 Pod（host index）。删除一个 replica 会删整组，不能只删其中一个 host。维护时要按 replica 规划，而不是按 Pod 规划。

### 55.6.4 删除前确认空闲

```bash
HEAD=$(kubectl get pod -l ray.io/cluster=<name>,ray.io/node-type=head -o jsonpath='{.items[0].metadata.name}')
kubectl exec "$HEAD" -- ray status
kubectl exec "$HEAD" -- ray list actors --filter "state=ALIVE"
kubectl exec "$HEAD" -- ray list tasks --filter "state=RUNNING"
```

把 Pod IP 与 actor/task 的 `node_ip` 对照，确认没有不可迁移的 actor。Serve 副本还要看 `target_capacity` 和副本分布。

## 55.7 变更窗口与回滚

### 55.7.1 变更前

```text
[ ] 备份 CR（kubectl get raycluster <name> -o yaml > backup.yaml）
[ ] 记录 Pod UID、镜像 digest、Ray 版本、运行中的 job
[ ] 确认容量余量（Recreate 会短暂释放再申请全部资源）
[ ] 确认 PDB/节点池/finalizer 不会让重建卡住
[ ] 通知用户维护窗口
[ ] 准备回滚 CR（旧镜像/旧 spec）
```

### 55.7.2 变更后

```text
[ ] status.state=ready，observedGeneration 追上 generation
[ ] head + 所有 worker Pod Running/Ready
[ ] ray status 节点数与 Pod 数一致
[ ] 新 Pod 的镜像 digest 正确
[ ] RayJob/RayService 没有异常状态迁移
[ ] autoscaler 没有反复扩缩
[ ] 事件里没有 Invalid/Failed 循环
```

### 55.7.3 回滚矩阵

| 变更 | 回滚动作 | 注意 |
|---|---|---|
| 镜像/模板（Recreate） | 改回旧模板，等待再次 Recreate | 会再停一次 |
| 镜像/模板（None） | 改回模板；旧 Pod 本来没变 | 已经新建的新版本 Pod 需要单独处理 |
| replicas | 改回原值 | 缩容后重建不保证同节点 |
| 调度器/亲和性 | 改回 spec | Pending Pod 可能需要删除才会重新调度 |
| operator | Helm rollback | CRD 不会自动回滚 |
| Ray 版本 | 新集群回滚/重建 | 不能靠 GCS FT |

## 55.8 实验：把变更语义跑成断言

```bash
python examples/maintenance_lab.py
```

输出：

```text
baseline        pods=3 images=['rayproject/ray:2.59.0']
image/None      pods=3 images=['rayproject/ray:2.59.0'] changed=0
scale +1/None   workers=3 images=['rayproject/ray:2.59.0', 'rayproject/ray:2.60.0']
recreate        pods=4 images=['rayproject/ray:2.60.0'] replaced=4
scale under Recreate workers=4 kept=4/4
head held       heads=0 workers=4
head restored   heads=1 workers=4

OK: upgrade strategy, spec-hash scope, mixed-version risk and head maintenance are observable
```

逐行解释：

1. **baseline**：head + 2 worker，全部 2.59；
2. **image/None**：只改镜像，`None` 不替换现有 Pod，UID 不变；
3. **scale +1/None**：新 worker 用新模板，出现 2.59/2.60 混合集群——这是警告，不是成功；
4. **recreate**：切到 `Recreate` 后 hash 变化，全部 Pod 被替换为 2.60；
5. **scale under Recreate**：只改 replicas，hash 不变，已有 Pod 全部保留，只新增一个；
6. **head held**：注解生效，head 不再自动重建，worker 仍在；
7. **head restored**：移除注解，head 恢复。

这个实验覆盖了最容易在真实集群里踩的五个误区：

- 把 `None` 当成“谨慎升级”；
- 以为改 replicas 会触发重建；
- 以为切到 `Recreate` 只影响下一个 Pod；
- 以为删 head 后控制器一定会立刻补；
- 以为混合版本可以慢慢滚。

## 55.9 常见事故模式

| 现象 | 根因 | 证据 | 修复 |
|---|---|---|---|
| 扩缩容时全部 Pod 重建 | hash 包含 replicas/时间戳/默认值 | head 注解 hash 每轮变化 | 排除 autoscaler 字段；固定默认值 |
| 改镜像后新旧版本混跑 | `upgradeStrategy: None` | Pod 镜像集合有两个 tag | 用 Recreate 或新集群 |
| Recreate 后集群起不来 | 新镜像依赖不兼容/资源不足 | Pod Pending/ImagePullBackOff | 回滚镜像；检查 requests |
| 删 head 后一直不恢复 | `disable-provisioned-head-restart` 还在 | CR 注解 | 移除注解 |
| 升级 operator 后集群全重建 | hash/版本注解处理不当 | 事件 RecreateUpgrade 集中出现 | 暂停调和；核对 operator 版本注解 |
| `workersToDelete` 删错 Pod | 手工写错名字/标签 | Pod 属于别的 group | 控制器应校验 owner/group；清理列表 |
| CRD 降级后字段丢失 | 只回滚 operator 未回滚 CRD | CR 字段被裁剪 | CRD 前向兼容；不要盲目降级 |

## 55.10 小结

- 变更的单位不是“字段”，而是“控制器会做什么”：改子对象、增删 Pod、全量重建、进入 suspend。
- spec hash 排除 autoscaler 拥有的字段；replicas 变化不应触发 Recreate。
- `Recreate` 是全量替换，`None` 只影响未来创建的 Pod；没有 RollingUpdate。
- Ray 版本升级的最小安全单位是新集群，不是混合版本的原地滚动。
- head 是单点：维护前先决定 GCS 恢复、driver 和存储；用注解可以把它“按住”。
- worker 维护要按组/按名字，PDB 不保护控制器直接删除的 Pod。
- 每次变更都要有前置检查、后置验证和明确的回滚矩阵。

下一章换一个方向：当变更已经出问题，如何用 Ray 的 State API、CLI 和日志建立证据链，而不是靠猜。

→ [第 56 章 Ray 活体诊断：State API、ray CLI 与证据链](56-live-diagnostics-state-api.md)
