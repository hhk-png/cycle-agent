# 第 24 章 从 Mini 到生产

## 24.1 差距地图

MiniVolcano 证明了语义，不是生产实现。差距可以分成五类：

| 类别 | MiniVolcano | 生产 Volcano | 影响 |
|---|---|---|---|
| API | 自定义 Go 结构 | CRD + API Server | 无法直接替换 |
| 控制面 | 手写 Manager | controller-runtime + informer | 无 watch/重试/leader election |
| 并发 | 单线程 | 多 informer + 工作队列 | 需要处理竞态 |
| 设备 | 标量资源 | GPU/MIG/vGPU/DRA | 异构能力缺失 |
| 多集群 | 无 | Volcano Global | 规模/容灾缺失 |

## 24.2 逐项差距

### 24.2.1 API 兼容

Mini 的 `api.Job` 不是 CRD。它没有：

- `metadata`/`ownerReferences`/`finalizers`；
- OpenAPI schema；
- conversion webhook；
- status subresource；
- RBAC。

**迁移建议**：不要试图把 Mini 的结构直接变成 CRD。用 Volcano 的 `volcano.sh/apis` 类型，Mini 只作为算法原型。

### 24.2.2 控制器运行时

Mini 的 `Manager.Sync` 是显式调用。生产需要：

- informer 缓存；
- 工作队列 + rate limiter；
- Reconcile 幂等；
- leader election；
- 事件记录；
- finalizer。

**迁移建议**：用 controller-runtime 重写 Manager；把 `Sync` 拆成 Reconcile 函数；用 envtest 测试。

### 24.2.3 调度器框架

Mini 的 Session/Statement/hook 是简化版。生产 Volcano：

- Statement 是操作日志，不是深拷贝；
- cache 有增量更新；
- 多调度器/分片；
- 插件通过 factory 注册；
- 有 metrics/tracing。

**迁移建议**：

- 如果只是加策略，写 Volcano 插件，而不是改 Mini；
- 如果要独立调度器，参考 Volcano 的 framework 结构，但用操作日志实现事务。

### 24.2.4 预留

Mini 的 `Reservation` 是一个虚构 CRD。真实 Volcano：

- 没有 Reservation CRD；
- 用 Queue guarantee + reservation 插件/elect/reserve + SLA；
- 不同版本行为不同。

**迁移建议**：不要在生产创建 `Reservation` 对象；用 Queue guarantee 和 SLA 插件表达需求。

### 24.2.5 拓扑

Mini 做两件事：单作业的“选域 + 限域”（network-topology-aware），以及 PodGroup 级反亲和（group-topology-affinity）。生产还需要：

- HyperNode 自动发现（标签/UFM/RoCE）；
- SubGroup/partition；
- LCA/多级 gang；
- HyperNode binpack；
- 拓扑抢占模拟；
- `topologyTierName`、namespace selector 与完整的 CRD 校验。

**迁移建议**：用真实 HyperNode CRD + network-topology-aware + group-topology-affinity；先在测试集群验证域容量、碎片与规则匹配。

### 24.2.6 设备

Mini 只有标量资源和 nodeSelector。生产需要：

- GPU Operator/device plugin；
- MIG/vGPU/vNPU；
- DRA DeviceClass/ResourceClaim；
- NUMA/PCIe 拓扑；
- 队列 DRA 配额。

**迁移建议**：不要让 Mini 的 `nvidia.com/gpu` 模型误导你；真实设备有型号、切分、共享、claim 生命周期。

### 24.2.7 作业生态

Mini 的 Cron 是 tick 周期，JobFlow 内联 JobSpec。生产：

- Cron 用 cron 表达式/时区/历史限制；
- JobFlow 引用 JobTemplate；
- LWS/Ray/Kubeflow/Spark 集成；
- 作业插件 svc/ssh/pytorch。

**迁移建议**：用真实 CRD；把 Mini 的 Cron/Flow 只当状态机教学。

### 24.2.8 性能

Mini 的深拷贝/全量遍历不适合大集群。生产需要：

- 操作日志；
- 增量 cache；
- 分片；
- 批量绑定；
- unschedulable cache；
- 并发。

**迁移建议**：如果规模超过单 scheduler 吞吐，先上 Volcano 的分片/agent scheduler，而不是自己优化 Mini。

### 24.2.9 安全

Mini 没有 RBAC/webhook/网络策略。生产必须补齐（第 17 章）。

### 24.2.10 高可用

Mini 是单进程。生产需要：

- 多副本 + leader election；
- PDB；
- 反亲和；
- 备份/恢复；
- 故障演练。

### 24.2.11 重调度与中断保护

Mini 实现了 `shuffle` + `rescheduling`，但只是一个“教学级”的重调度器：

- 有简化 PDB（selector + minAvailable/maxUnavailable + 累计保护），但没有控制器维护的 `disruptionsAllowed`、Eviction API 与 Pod 健康探测；
- `resourcequota` 只在 enqueue 时检查已准入作业，不是 API Server 级准入；
- `nodegroup` 用类型字段而不是上游的 Queue 注解，配置形态不兼容；
- 没有真实 Volcano 的五分钟冷却与多策略；
- 没有 checkpoint/重启成本模型；
- 没有多调度器/分片下的协调；
- 没有 `usage` 指标，阈值只能基于 request 利用率；
- `group-topology-affinity` 用数字 tier、成对评估，没有 `topologyTierName`、namespace selector 与完整 CRD 校验。

**迁移建议**：生产上先用真实 Volcano 的 `rescheduling` + `pdb`，从小范围低优先级队列灰度；不要直接把 Mini 的阈值/间隔映射到生产。训练作业开启 shuffle 前，先确认 checkpoint 与 `PodEvicted` 策略。

### 24.2.12 角色门槛、dependsOn 与提前完成

Mini 现在实现了：

- task 级 `minAvailable` → `PodGroup.minTaskMember`；
- `job.minAvailable >= Σ task.minAvailable` 时逐角色检查，低于时跳过（与上游一致）；
- `minSuccess` 达到即 Completed，未启动副本被终止。

仍未实现：

- `tasks[].dependsOn`：Mini 一次性物化所有 Pod，没有“上游 Ready 前不创建下游”的阶段；
- `minSuccess` 与 `minAvailable` 在**全部终止后的失败判定**：Mini 只有“成功数达到 minSuccess 就完成”，没有模拟上游“全部结束后仍不足 minAvailable 则 Failed”；
- 角色门槛与 `subGroupPolicy`/`partitionPolicy` 的组合；
- 真实控制器删除/终止 Pod 的观测差异（Mini 用 `Succeeded` 标记模拟）。

**迁移建议**：用 dependsOn 时先按 4.2.4 的死锁规则设计 `minAvailable`；生产上优先用 JobFlow 表达阶段 DAG。`minSuccess` 适合分片批处理，不建议用于训练——部分 worker 成功就结束通常不是想要的结果。

## 24.3 三条迁移路径

### 路径 A：写 Volcano 插件（推荐）

适合“只缺一个调度策略”的团队：

1. 用真实 Volcano 集群；
2. 在 `pkg/scheduler/plugins` 加插件；
3. 注册 factory；
4. 在 scheduler config 启用；
5. 写单元测试 + e2e；
6. 向上游提 PR 或维护 fork。

优点：复用 Volcano 的 cache/session/statement/生态。
缺点：受上游版本/评审节奏影响。

### 路径 B：自研调度器

适合“Volcano 的模型不满足，需要深度定制”的团队：

1. 用 controller-runtime + client-go；
2. 定义自己的 CRD（或复用 PodGroup）；
3. 实现 informer/cache；
4. 实现 session/statement；
5. 实现 actions/plugins；
6. 用 envtest/kwok/e2e 测试；
7. 灰度/回滚。

MiniVolcano 可以作为算法原型，但不要直接上线。

### 路径 C：Kueue + Volcano 组合

适合“准入/配额用 Kueue，放置用 Volcano”的团队：

- Kueue 管 ClusterQueue/LocalQueue/Admission；
- Volcano 管 gang/拓扑/设备；
- 注意两套配额账本同步；
- 用 Kueue 的 admission check 触发 Volcano。

## 24.4 测试策略迁移

| 层级 | Mini | 生产 |
|---|---|---|
| 单元 | go test | go test + table-driven |
| 框架 | Session 单测 | envtest + fake client |
| 调度 | 场景 | kwok + benchmark |
| 控制器 | Manager 单测 | envtest + reconcile 测试 |
| e2e | 无 | kind + 真实 CRD + 作业 |
| 性能 | 场景 | benchmark + pprof |
| 混沌 | 故障注入 | 节点 NotReady/网络分区/证书过期 |

## 24.5 生产化检查清单

- [ ] 用真实 CRD，不用 Mini 结构。
- [ ] 控制器用 controller-runtime，幂等 + 退避。
- [ ] 调度器用操作日志事务，不用全量深拷贝。
- [ ] 有 informer/cache 失效机制。
- [ ] 有 leader election/HA。
- [ ] 有 webhook/RBAC/网络策略。
- [ ] 有设备/DRA 支持（如需）。
- [ ] 有真实拓扑发现（如需）。
- [ ] 有 benchmark/压测。
- [ ] 有 explain/诊断能力。
- [ ] 有升级/回滚/备份。
- [ ] 有配额评审流程。

## 24.6 Mini 教会我们什么

- gang 的本质是事务，不是“一起创建 Pod”。
- 队列公平是“份额 + 借用 + 回收”的组合。
- 抢占/回收必须有回滚，否则会出现“抢了但没跑”。
- 重调度要把“提名受害者”和“执行驱逐”分开，并且保护 gang/PDB。
- 拓扑必须在选节点前选域。
- 控制器和调度器的边界不能混。
- 指标/explain 和算法同样重要。

这些结论与具体实现无关，是迁移时真正要保留的东西。

## 24.7 本章小结

- Mini 与生产在 API、控制面、并发、设备、多集群上有本质差距。
- 迁移有三条路径：写 Volcano 插件、自研调度器、Kueue+Volcano。
- 不要直接上线 Mini 的结构；保留它的语义与测试方法。
- 生产化清单覆盖 CRD、控制器、事务、HA、安全、设备、测试。

下一章讲验证工程：141 个测试通过意味着什么，以及它不能证明什么。
