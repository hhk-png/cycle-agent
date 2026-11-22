# 第 36 章 调度器与控制器实现

## 36.1 调度器代码结构

`mini_k8s/scheduler/scheduler.py`：

- `Scheduler`：线程、循环、`schedule_once`。
- `NodeInfo`：节点、已分配 Pod、资源使用。
- `pod_requests`：requests 计算。
- 过滤器函数：`_selector_matches`、`_node_affinity_matches`、`_taints_match`、`_resources_fit`、`_volumes_fit`、`_ports_fit`、`_interpod_affinity_matches`、`_topology_spread_matches`。
- `_score`：LeastAllocated + 亲和 + 污点惩罚 + 软反亲和。
- `_preempt`：低优先级驱逐。

## 36.2 schedule_once

```python
def schedule_once(self):
    pods = store.list(pods)
    nodes = store.list(nodes)
    assigned = group pods by nodeName
    node_infos = build NodeInfo
    pending = [p for p in pods if not nodeName and not terminal]
    pending.sort(by priority desc, creationTimestamp, name)
    for pod in pending:
        candidates = [info for info in node_infos if self._fits(pod, info, node_infos)]
        if not candidates:
            if preemption: self._preempt(pod, node_infos)
            else: event FailedScheduling
            continue
        chosen = max(candidates, key=score)
        update pod.spec.nodeName
        event Scheduled
```

注意：

- `assigned` 包含已绑定但未运行的 Pod，避免超卖。
- 每轮重新计算，没有缓存。
- 绑定通过 `store.update` 写整个 Pod；真实实现写 Binding 子资源。
- **`schedulerName` 过滤**：只处理 `spec.schedulerName` 为 `mini-scheduler` 或 `default-scheduler` 的 Pending Pod；其他名字留给“另一个调度器”，MiniK8s 不会碰。若没有可行节点，会写 `PodScheduled=False` 的 Unschedulable condition 并产生 `FailedScheduling` 事件。
- 默认调度器名字可通过 `--scheduler-name` 配置；测试覆盖了“非本调度器的 Pod 保持 Pending”。

## 36.3 资源计算

```python
def pod_requests(pod):
    app = {}
    for c in pod.spec.containers:
        app[key] = app.get(key, 0) + requests[c]
    init = {}
    for c in pod.spec.initContainers:
        init[key] = max(init.get(key, 0), requests[c])
    # 有效请求是 max(所有 app 之和, 单个 init 的最大值)，
    # 不是 max(init) + sum(app)。
    return {k: max(app.get(k, 0), init.get(k, 0))
            for k in set(app) | set(init)}
```

初始化容器顺序执行，所以一个 init 的请求不会与 app 容器叠加；这是常见的实现错误，测试 `test_init_container_requests_use_max_not_sum` 专门覆盖它。

`NodeInfo.allocatable` 从 `status.allocatable` 或 `capacity` 读取，支持 `pods` 数量限制。资源键是开放的，但语义分两类：

- **CPU/内存/临时存储**：可压缩或按字节计量，内部用浮点数量表示。
- **扩展资源**（如 `nvidia.com/gpu`）：整数计数。关键规则是：**节点没有声明某扩展资源，就等于该资源为 0**，调度器必须拒绝，而不是跳过。否则 GPU Pod 会被放到没有 GPU 的节点上。示例 16 演示了这一语义，测试 `test_unadvertised_extended_resource_stays_pending` 专门覆盖它。

## 36.4 过滤器

### 节点状态

- `Ready` 条件必须 True。
- `spec.unschedulable` 为 true 则拒绝。

### 污点

```python
for taint in node.spec.taints:
    if effect in ("NoSchedule", "NoExecute") and not tolerates:
        return False
```

`PreferNoSchedule` 不阻塞，只在打分时扣分。

### 卷

- PVC 必须存在。
- 已 Bound 的 PV 如果有 nodeAffinity，节点必须匹配。
- 未 Bound 但 StorageClass 是 WFFC，允许调度。
- 其他未 Bound 情况拒绝。

### hostPort

检查同节点已分配 Pod 的 hostPort/protocol/hostIP 冲突。

### 亲和性

- nodeSelector：标签全匹配。
- nodeAffinity required：任意 term 匹配；term 内 matchExpressions AND。
- podAffinity/antiAffinity：按 topologyKey 分组，检查同域是否有匹配 Pod。
- topologySpread：计算各域计数，检查 maxSkew。

## 36.5 打分

```python
score = 0
score += 50 * (1 - (cpu_used + pod_cpu_request) / cpu_alloc)
score += 50 * (1 - (mem_used + pod_mem_request) / mem_alloc)
score += preferred_node_affinity
score -= prefer_no_schedule_penalty
score -= soft_anti_affinity_penalty
```

注意分子**包含当前 Pod 的 requests**，这是“放置后剩余率”而不是“当前剩余率”。选择最高分；相同分数用 `max(key=(score, name))`，即选**字典序最大**的节点名（确定性，但不是“最小”）。测试若断言具体节点，必须知道这个 tie-break。

真实调度器还有：

- BalancedAllocation：CPU/内存使用率差越小越高。
- ImageLocality。
- 拓扑打分。
- 插件权重。

## 36.6 抢占

```python
def _preempt(pod, nodes):
    for node in nodes:
        victims = [p for p in node.pods if p.priority < pod.priority]
        if not victims: continue
        simulate removing victims
        if resources fit:
            delete victims
            event Preempted
            return True
```

简化点：

- 选择一个“最小受害者前缀”：按优先级从低到高累加，直到模拟资源刚好容纳高优 Pod。
- 跳过 PDB 已耗尽（`disruptionsAllowed < 1`）的 Pod，不把它们当作受害者。
- **优先级 ≤ 0 的 Pod 不会发起抢占**（即使面对更低优先级受害者）；`preemptionPolicy: Never` 同样直接返回。真实 Kubernetes 默认只允许非负优先级抢占，这一边界在测试里很重要。
- 不设置 `nominatedNodeName`，也不保证抢占后一定调度成功（其他 Pod 可能竞争）。

真实抢占还需要：

- 更精确的最小受害者集合（组合优化/拓扑约束）。
- Nominated Node 机制与抢占失败回退。
- 与调度器缓存、PDB、优先级策略的完整协调。

## 36.7 DeploymentController

核心流程：

1. 计算模板 hash。
2. 确保 RS 存在（名字 `deploy-hash`）。
3. 更新 revision annotation。
4. 根据策略缩放新旧 RS。
5. 汇总 status。
6. 清理历史 RS。

滚动算法见第 8 章。代码关键点：

```python
new_ready = min(new_rs.spec.replicas, new_rs.status.availableReplicas)
old_floor = max(0, desired - max_unavailable - new_ready)
old_target = min(old_total, old_floor)
new_target = min(desired, max(new_replicas, desired + max_surge - old_target))
```

局限：

- 没有 progressDeadlineSeconds 的完整超时标记。
- 没有 paused 的完整语义（简化）。
- 没有多旧 RS 的精确比例缩放。
- Recreate 已实现“旧 Pod（含 terminating）全部消失后才扩新 RS”，但没有真实的进度条件/超时事件。

## 36.8 ReplicaSetController

- 统计 owned Pods。
- 收养无 controller owner 且匹配 selector 的 Pod。
- 不足则创建；超出则删除。
- 汇总 status（replicas/ready/available）。
- 通过 `AdmissionChain` 创建 Pod（如果传入 admission）。

删除策略：

```python
rank = (bool(nodeName), phase_rank, ready, creationTimestamp, name)
victims = sorted(active, key=rank)  # 升序：未调度/未就绪/更旧优先
```

即：未调度的先删，其次 Pending/Unknown，再其次未 Ready；同类中更旧的先删。仍有简化：没有完整的优先级/本地存储/finalizer 组合判断。

## 36.9 StatefulSetController

- 按 ordinal 管理 Pod。
- OrderedReady：前一个 Ready 才创建下一个。
- volumeClaimTemplates：为每个 ordinal 创建 PVC，改写 Pod 卷引用。
- 滚动更新：从高 ordinal 开始，每次 reconcile 只推进一个 Pod；`partition` 控制只更新 ordinal ≥ partition 的 Pod。
- status：`replicas`、`readyReplicas`、`currentReplicas`、`updatedReplicas`、`availableReplicas`、`observedGeneration`；`currentRevision`/`updateRevision` 由 Pod 标签重建。

局限：

- 没有 PVC retention policy。
- 没有 OnDelete 的完整语义。
- 没有 ControllerRevision 对象；`currentRevision` 是从旧 Pod 的模板标签推出来的，不是真实历史。
- 更新时删除/重建，不保留旧 Pod 的 status。
- 不实现 `maxUnavailable`/`maxSurge` 的完整滚动语义，也不支持 `Recreate` 策略。

## 36.10 DaemonSetController

- 找出符合条件的节点。
- 每个节点一个 Pod（名字 `ds-node`）。
- 模板变化时逐个重建。
- 汇总 desired/current/ready。

局限：

- 支持 nodeSelector、required nodeAffinity、默认/自定义 toleration；cordon 节点保留 DaemonSet Pod。
- 没有 maxUnavailable 的严格限制。
- 没有完整的 preferred affinity/拓扑打分。
- Pod 直接设置 nodeName，绕过调度器。

## 36.11 JobController

- 统计 active/succeeded/failed。
- 达到 completions → Complete。
- 超过 backoffLimit → Failed，并写入 `completionTime`，让 TTL 控制器能清理失败 Job。
- 创建 Pod 直到 parallelism/completions。
- `suspend`：暂停创建新 Pod。
- TTL 删除：`ttlSecondsAfterFinished` 到点后删除 Job 及其 Pod。

局限：

- Indexed 模式会分配唯一索引，并在 Pod 失败后复用其索引；但没有 `backoffLimitPerIndex`/`maxFailedIndexes`。
- 没有 podFailurePolicy/successPolicy。
- 没有 activeDeadlineSeconds。
- 没有 Job 的 finalizer；清理靠 TTL 控制器，不是完整的级联/保留策略。

## 36.12 CronJobController

- 可注入时钟（测试）。
- 首次运行只记录 lastScheduleTime，不补历史。
- 每次最多创建一个 Job。
- concurrencyPolicy Allow/Forbid/Replace。
- 历史清理。

局限：

- 没有 startingDeadlineSeconds 的完整错过计数。
- 没有 timeZone。
- 没有 suspend 的完整状态。
- 调度精度取决于 reconcile 间隔。

## 36.13 HPAController

- 读取目标 scale。
- 统计 Pod metrics（CPU 或内存）。
- 计算 utilization：实际用量 / requests。
- desired = ceil(current * utilization / target)。
- clamp min/max。
- 更新目标 replicas。
- 写 status/conditions。

局限：

- `behavior` 扩缩策略未实现。
- 多指标**已支持取最大**（每个指标算一个候选 desired，取最大值），并实现了 10% 容差；未实现稳定窗口、Object/External 指标和自定义指标聚合。
- metrics 缺失时保守保持。
- 不实现 `scaleToZero`、容器级指标选择等高级语义。

### MetricsController：指标从哪里来

HPA 读取的不是 cAdvisor，而是 `metrics.k8s.io/v1beta1` 的 `PodMetrics`/`NodeMetrics` 对象。MiniK8s 用 `MetricsController` 每 1 秒生成它们：

- CPU 用量：Pod 注解 `mini-k8s.io/simulate.cpu-usage`（如 `120m`），否则取 requests 的 60%；无 requests 时按 100m。
- 内存用量：注解 `mini-k8s.io/simulate.memory-usage`（如 `200Mi`），否则取 requests 的 70%；无 requests 时按 64Mi。
- 多容器 Pod 的 PodMetrics 会按各容器的 requests 比例拆分总用量，保证容器用量之和等于 Pod 总量；否则 HPA 会把多容器 Pod 重复计数。
- NodeMetrics 是该节点所有 Running Pod 的汇总。

HPA 优先读取这些对象；对象缺失时才回退到同样的模拟函数。这条链路解释了为什么实验里改一个 annotation 就能改变扩缩结果，也解释了“metrics-server 没装时 HPA 不工作”的真实原因。

## 36.14 控制器共同模式

所有控制器：

- 继承 `Controller`。
- `reconcile()` 遍历对象。
- 设置 ownerReference（`base.py` 里有 `create_owned` helper，但当前控制器都通过 `owner_reference`/`make_pod_from_template` 内联构造，`create_owned` 实际没有调用点；读代码时不要找不存在的抽象）。
- 用 `update_status` 写状态（避免 generation 变化）。
- 用 `event` 记录。
- 捕获异常不退出线程。

这种结构让代码可读，但缺少 workqueue/限速/缓存。工程化差距要明确：

- 许多控制器用 `try/except` 包住单个对象，异常被吞掉或只写 Event；没有 rate-limited retry，也没有把失败对象重新入队。
- 轮询全量 list 的复杂度是 O(对象数)，没有 informer 的增量缓存与索引。
- 冲突（409）后的重试多为简单重读，没有指数退避与抖动。
- 控制器没有独立的指标/日志，排障依赖 Event。

真实控制器必须做到：幂等调谐、冲突重试、可观测、可限速。第 40 章讨论替换路线。

## 36.15 测试如何覆盖

- 调度：资源、扩展资源（GPU）、标签、污点、亲和、hostPort、抢占。
  - `test_unadvertised_extended_resource_stays_pending`：没有声明 GPU 的节点不接收 GPU Pod。
  - `test_preemption_policy_never_does_not_evict`：`preemptionPolicy: Never` 不驱逐。
  - `test_required_anti_affinity_respects_zone_domain`：同 zone 跨节点也受 anti-affinity 约束。
  - `test_preemption_deletes_minimal_prefix`：只删除刚好够用的低优 Pod 前缀（等大 Pod 下等价于最小集合；不等大时不保证全局最优）。
  - `test_scheduler_honours_scheduler_name_and_reports_unschedulable`：自定义 scheduler 不抢活，失败写 Unschedulable 条件。
- 控制器：副本收敛、滚动、收养、StatefulSet、DaemonSet、Job、CronJob、HPA。
  - `test_replicaset_scale_down_prefers_unscheduled_pod`：缩容先删未调度 Pod。
  - `test_recreate_waits_for_old_pods_to_disappear`：Recreate 等旧 Pod 完全消失。
  - `test_pdb_eviction_spends_budget_even_without_controller`：驱逐同步扣减 PDB 预算。
  - `test_cronjob_active_tracks_all_running_jobs`：active 列出所有未完成 Job。
- 测试优先使用 `wait_until` 等待最终状态；少数“不应该发生”的否定断言仍用短 sleep，因为轮询无法证明条件永远为假。

## 本章小结

- MiniK8s 调度器实现过滤/打分/抢占，但简化为轮询和全量计算。
- 控制器实现 Deployment/RS/STS/DS/Job/CronJob/HPA 的核心收敛逻辑。
- 每个控制器都有明确的简化边界，文档与测试对应。
- 真实实现需要 informer/workqueue/限速/缓存/更完整的策略。

## 延伸阅读

- [第 17 章 调度器](17-scheduler.md)
- [第 18 章 控制器模式](18-controller-pattern.md)
- [第 39 章 测试策略](39-testing.md)
