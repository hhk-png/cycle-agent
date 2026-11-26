# 附录

## A. API 速查

### A.1 Volcano Job（`batch.volcano.sh/v1alpha1`）

| 字段 | 类型 | 说明 |
|---|---|---|
| `spec.schedulerName` | string | 通常为 `volcano`；省略时由 admission 补默认调度器 |
| `spec.queue` | string | 队列名 |
| `spec.priorityClassName` | string | 引用 PriorityClass |
| `spec.minAvailable` | int32 | gang 最小副本数；省略/0 由 webhook 补成 Σ task 有效 minima（0 不是关闭 gang） |
| `spec.minSuccess` | *int32 | 成功副本门槛；达到即 Completed，全部结束仍不足则 Failed |
| `spec.maxRetry` | int32 | 作业级重试次数 |
| `spec.volumes[]` | []VolumeSpec | 作业级卷；`mountPath` + `volumeClaimName`/`volumeClaim` |
| `spec.tasks[]` | []TaskSpec | 任务列表 |
| `spec.tasks[].name` | string | 任务名，唯一 |
| `spec.tasks[].replicas` | int32 | 副本数 |
| `spec.tasks[].minAvailable` | *int32 | 任务级 gang 门槛；省略=replicas（有 `PartitionPolicy.MinPartitions>0` 时=MinPartitions×PartitionSize），写入 `minTaskMember` |
| `spec.tasks[].maxRetry` | int32 | 任务级容器重启上限；默认 3，-1 不限制 |
| `spec.tasks[].dependsOn` | object | `name[]` + `iteration: any\|all`，控制 Pod 创建时机 |
| `spec.tasks[].topologyPolicy` | string | `none`/`best-effort`/`restricted`/`single-numa-node` |
| `spec.tasks[].partitionPolicy` | object | `totalPartitions`/`partitionSize`/`minPartitions`/`networkTopology` |
| `spec.tasks[].template` | PodTemplateSpec | Pod 模板 |
| `spec.tasks[].policies` | []LifecyclePolicy | 任务级策略 |
| `spec.policies` | []LifecyclePolicy | 作业级策略 |
| `spec.plugins` | map[string][]string | 作业插件（env/svc/ssh/pytorch/...） |
| `spec.networkTopology` | object | `mode: hard\|soft`，`highestTierAllowed` |
| `spec.ttlSecondsAfterFinished` | int32 | 完成后自动 GC 的秒数；0=立即，不设=保留 |

### A.2 PodGroup（`scheduling.volcano.sh/v1beta1`）

| 字段 | 说明 |
|---|---|
| `spec.minMember` | 最小成员数 |
| `spec.minTaskMember` | 每个 task 的最小成员数（由 task `minAvailable` 派生） |
| `spec.subGroupPolicy[]` | 子组级 gang/拓扑：`name`/`subGroupSize`/`minSubGroups`/`labelSelector`/`matchLabelKeys`/`networkTopology` |
| `spec.minResources` | 最小资源 |
| `spec.queue` | 队列 |
| `spec.priorityClassName` | 优先级 |
| `spec.topologyAffinity.podGroupAntiAffinity.required[]` | 必须不同域的规则（省略 weight） |
| `spec.topologyAffinity.podGroupAntiAffinity.preferred[]` | 尽量不同域的规则（weight 1–100） |
| `topologyTierName` / `topologyTier` | 规则作用的 HyperNode 域（二选一） |
| `podGroupSelector.labelSelector` | 选择对端 PodGroup 的标签 |
| `status.phase` | Pending/Inqueue/Running/Unknown/Completed |

裸 Pod 关联：

```yaml
metadata:
  annotations:
    scheduling.k8s.io/group-name: <podgroup-name>
spec:
  schedulerName: volcano
```

### A.3 Queue（`scheduling.volcano.sh/v1beta1`）

| 字段 | 说明 |
|---|---|
| `spec.weight` | 权重，默认 1 |
| `spec.capability` | 硬上限 |
| `spec.deserved` | capacity 插件显式份额 |
| `spec.guarantee` | 保底 |
| `spec.reclaimable` | 是否可回收，默认 true |
| `spec.parent` | 父队列 |
| `spec.priority` | 队列优先级 |
| `spec.dequeueStrategy` | fifo/traverse（默认 traverse） |
| `spec.affinity.nodeGroupAffinity` | 节点组亲和：required/preferred 名单 |
| `spec.affinity.nodeGroupAntiAffinity` | 节点组反亲和：required/preferred 名单 |
| `spec.type` | 队列类型（上层平台可用来区分租户/用途） |
| `spec.extendClusters[]` | Volcano Global 的扩展集群 |
| `spec.allowedNamespaces[]` | NamespaceQueue 挂载授权；空=拒绝，`["*"]`=全部 |
| `status.state` | Open/Closed/Closing/Unknown（控制器维护；开/关用 `vcctl queue operate`） |

### A.4 CronJob（`batch.volcano.sh/v1alpha1`）

| 字段 | 说明 |
|---|---|
| `spec.schedule` | cron 表达式 |
| `spec.timeZone` | 时区 |
| `spec.concurrencyPolicy` | Allow/Forbid/Replace |
| `spec.startingDeadlineSeconds` | 错过启动的截止 |
| `spec.successfulJobsHistoryLimit` | 成功历史 |
| `spec.failedJobsHistoryLimit` | 失败历史 |
| `spec.jobTemplate.spec` | Volcano JobSpec |

### A.5 HyperNode（`topology.volcano.sh/v1alpha1`）

| 字段 | 说明 |
|---|---|
| `spec.tier` | 层级，越小越近 |
| `spec.tierName` | 层级名称 |
| `spec.members[]` | 成员 |
| `members[].type` | Node/HyperNode |
| `members[].selector.exactMatch.name` | 精确名称 |
| `members[].selector.regexMatch` | 正则 |
| `members[].selector.labelMatch` | 标签 |

### A.6 NamespaceQueue（`scheduling.volcano.sh/v1beta1`，v1.16 Alpha）

| 字段 | 说明 |
|---|---|
| `metadata.namespace` | 队列所在 namespace |
| `spec.parent` | `cluster/<name>`、同 namespace 名字、或省略（默认 `cluster/default`） |
| `spec.capability` | 硬上限 |
| `spec.deserved` | 应得份额 |
| `spec.guarantee` | 保底 |
| `spec.reclaimable` | 是否可回收 |
| `spec.priority` | 队列优先级 |
| `status.conditions` | `Ready` / `Authorized` |

作业引用：`spec.queue: namespace/<name>`。需要 feature gate `NamespaceQueue=true`，且只能与层级 capacity 一起用。

### A.7 PodDisruptionBudget（`policy/v1`，Kubernetes 原生）

| 字段 | 说明 |
|---|---|
| `spec.minAvailable` | 最少可用副本数（与 maxUnavailable 二选一） |
| `spec.maxUnavailable` | 最多不可用副本数 |
| `spec.selector` | 匹配 Pod 标签 |
| `status.disruptionsAllowed` | 当前允许的中断数 |

Volcano 的 `pdb` 插件读取它来过滤 preempt/reclaim/shuffle 的受害者；不启用插件就没有保护。

### A.8 JobFlow / JobTemplate（`flow.volcano.sh/v1alpha1`）

| 字段 | 说明 |
|---|---|
| `spec.jobRetainPolicy` | retain/delete |
| `spec.flows[].name` | DAG 节点名，也是 JobTemplate 名 |
| `spec.flows[].dependsOn.targets` | 上游 |
| JobTemplate `spec` | Volcano JobSpec |

### A.9 ResourceQuota 与 NodeGroup（MiniVolcano 治理模型）

`ResourceQuota` 是 Kubernetes 原生对象：

| 字段 | 说明 |
|---|---|
| `metadata.namespace` | 配额作用域 |
| `spec.hard` | 上限；可写 `requests.cpu`、`requests.memory`、`requests.nvidia.com/gpu` |
| `status.used` | API Server 统计的已用配额 |

MiniVolcano 的 `resourcequota` 插件只读取 `hard`，用“已准入作业的请求总和 + 当前作业 MinResources”做准入判断；不实现 `status.used` 与 API Server 的原子扣减。写入时会去掉 `requests.` 前缀，因此 `requests.cpu` 与 `cpu` 都能被识别。

NodeGroup 在上游通过 **Queue `spec.affinity` + `volcano.sh/nodegroup-resource-limits` 注解 + 节点标签**三件套配置；MiniVolcano 用类型字段：

```yaml
# MiniVolcano 的 Queue 模型（不是上游 CRD）
spec:
  nodeGroup:
    names: ["a100"]
    capability:
      a100:
        nvidia.com/gpu: "32"
```

节点侧使用 `volcano.sh/nodegroup-name` 标签，与上游示例保持一致；上游队列侧用 `spec.affinity` 决定“能落在哪些组”，用 `volcano.sh/nodegroup-resource-limits` 注解决定“每组最多用多少”，Mini 用类型字段同时模拟两者。`nodegroup` 插件默认 `strict: true`：队列没有 affinity 时**任何节点都不落**（带标签节点也会被 `affinity not specified` 拒绝）；`strict: false` 才放行不带标签的节点。迁移时不要直接复制这个 YAML。

## B. 调度器配置参考

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: volcano-scheduler-configmap
  namespace: volcano-system
data:
  volcano-scheduler.conf: |
    actions: "enqueue, allocate, backfill, preempt, reclaim"
    tiers:
    - name: tier-1
      plugins:
      - name: priority
      - name: gang
        enablePreemptable: true
        enableJobStarving: true
      - name: conformance
    - name: tier-2
      plugins:
      - name: drf
      - name: predicates
      - name: proportion
        enablePreemptable: true
      - name: nodeorder
      - name: binpack
        arguments:
          binpack.weight: "1"
```

常用插件参数（以版本为准）。**plugin option 与 `name` 同级；`arguments` 在 `arguments:` 下**，两者写错位置通常不会报错，只会静默不生效：

| 插件 | 位置与参数 | 说明 |
|---|---|---|
| `gang` | plugin option：`enablePreemptable`, `enableJobStarving` | 抢占/饥饿 |
| `proportion` | plugin option：`enablePreemptable` | 回收/抢占 |
| `capacity` | plugin option：`enableHierarchy`；arguments：`capacity.DynamicResourceAllocationEnable`, `capacity.DRAConsumableCapacityEnable` | 层级/DRA |
| `binpack` | `binpack.weight`, `binpack.cpu`, `binpack.memory` | 装箱 |
| `nodeorder` | arguments：`nodeaffinity.weight`, `podaffinity.weight`, `leastrequested.weight`, `balancedresource.weight`, `mostrequested.weight`, `tainttoleration.weight`, `imagelocality.weight`, `podtopologyspread.weight` | 打分 |
| `lifecycle` | `lifecycle.MaxGrade`, `lifecycle.MaxScore`, `lifecycle.SaturatedTresh`, `lifecycle.WindowSize` | 生命周期聚拢/缩容（发行版扩展，非 upstream v1.16） |
| `tdm` | `tdm.revocable-zone.<zone>`, `tdm.evict.period` | 时分复用 |
| `overcommit` | `overcommit-factor` | 超卖 |
| `sla` | `sla-waiting-time` | SLA |
| `fairshare` | `fairshare.targetQueues`, `fairshare.resourceKey`, `fairshare.halfLifeMinutes`, `fairshare.enableEnqueueGate`, `fairshare.persistState` | 历史公平（v1.16） |
| `rescheduling` | arguments：`interval`, `metricsPeriod`, `strategies[].name`, `strategies[].params.{thresholds,targetThresholds,...}` | 重调度；tier 短路（策略是嵌套列表，不是扁平的 `rescheduling.*` 键） |
| `pdb` | 无关键参数 | PDB 保护；需 Kubernetes PDB 对象 |
| `usage` | plugin option：`enablePredicate`；arguments：`usage.weight`, `cpu.weight`, `memory.weight`, `thresholds.{cpu,mem}`, `estimator.*` | 基于真实利用率 |
| `resourcequota` | 无 | 检查 namespace ResourceQuota |
| `nodegroup` | plugin option：`enableHierarchy`；arguments：`strict`（默认 true）、`enablePreferredOrder`；队列 `spec.affinity` + 注解 `volcano.sh/nodegroup-resource-limits` | 节点组亲和/反亲和、层级继承与分组上限（v1.16） |
| `cdp` | 无关键参数；Pod/Job 注解 `volcano.sh/cooldown-time` | 冷却保护，避免刚启动的 Pod 被反复抢占 |
| `deviceshare` | arguments：`deviceshare.GPUSharingEnable`, `deviceshare.VGPUEnable`, `deviceshare.SchedulePolicy`, `deviceshare.NodeLockEnable` | GPU 共享账本 |

## C. 命令速查

```bash
# 安装
helm repo add volcano-sh https://volcano-sh.github.io/helm-charts
helm install volcano volcano-sh/volcano -n volcano-system --create-namespace --version 1.16.0

# 状态
kubectl get pods -n volcano-system
kubectl get vcjob,podgroup,queue -A
kubectl describe vcjob <job>
kubectl describe podgroup <pg>
kubectl get events --sort-by=.lastTimestamp

# 队列
vcctl queue operate --name <q> --action close
vcctl queue operate --name <q> --action open

# 配置
kubectl -n volcano-system get configmap volcano-scheduler-configmap -o yaml
kubectl -n volcano-system rollout restart deploy/volcano-scheduler

# 日志
kubectl -n volcano-system logs deploy/volcano-scheduler --tail=500
kubectl -n volcano-system logs deploy/volcano-controllers --tail=500
kubectl -n volcano-system logs deploy/volcano-admission --tail=500

# 指标
kubectl -n volcano-system port-forward svc/volcano-scheduler 8080:8080
curl localhost:8080/metrics | grep '^volcano_'
```

MiniVolcano：

```bash
cd mini-volcano
go test ./... -count=1
go run ./cmd/minivolcano -list
go run ./cmd/minivolcano -scenario basic
go run ./cmd/minivolcano -scenario priority -explain default/train-high
go run ./cmd/minivolcano -scenario proportion -what-if team-a/nvidia.com/gpu=2 -no-check
go run ./cmd/minivolcano -scenario basic -metrics -events 20
go run ./cmd/minivolcano -all
```

## D. 术语表

| 术语 | 含义 |
|---|---|
| Gang | 一组任务一起启动或都不启动 |
| minAvailable | gang 最小副本数 |
| PodGroup | 调度器看到的作业组 |
| Queue | 集群级队列，承载配额 |
| proportion | 按权重分队列份额的插件 |
| capacity | 层级队列/配额插件 |
| DRF | 主导资源公平 |
| fairshare | 历史用量公平（v1.16） |
| preempt | 队列内优先级抢占 |
| reclaim | 跨队列回收 |
| HyperNode | 网络拓扑树 |
| TDM | 时分复用 |
| SLA | 等待时间预算 |
| DRA | Kubernetes 动态资源分配 |
| MIG | NVIDIA 多实例 GPU |
| vGPU/vNPU | 虚拟 GPU/NPU 共享 |
| LWS | LeaderWorkerSet |
| JobFlow | Job 级 DAG |
| JobTemplate | 可复用 Volcano JobSpec |
| conformance | 只管理 Volcano 作业的插件 |
| overcommit | 超卖因子 |
| binpack | 装箱打分 |
| PDB | PodDisruptionBudget，约束自愿中断时的最少可用副本 |
| ResourceQuota | namespace 级资源上限，API Server 与调度器插件双重检查 |
| NodeGroup | 按节点标签划分的资源组，可对队列设置分组上限 |
| NUMA | 非统一内存访问 |
| DP / TP / PP / EP | 数据/张量/流水/专家并行；世界大小通常是它们的乘积 |
| MFU | Model FLOPs Utilization，有效算力占峰值的比例 |
| KV cache | 推理时缓存的 key/value，长上下文下会主导显存 |
| TTFT / TPOT | 首 token 延迟 / 每输出 token 延迟 |
| P/D 分离 | prefill 与 decode 分池部署，分别优化算力与显存带宽 |

## E. 文件与验证

| 项 | 数量 |
|---|---:|
| 教程章节 | 35 |
| 附录小节 | 23（A–W） |
| 示例 YAML/JSON | 37 |
| 辅助脚本 | 3（`check_docs.py`、`verify_all.sh`、`capacity_model.py`） |
| MiniVolcano Go 文件 | 61（50 源码 + 11 测试） |
| MiniVolcano 包 | 9 个测试包 |
| 场景 | 28 |
| 顶层测试 | 129 |
| 随机不变量子测试 | 12 |
| PASS 行合计 | 141 |
| 调度器插件 | 23 |
| 调度 action | 6 |
| hook 类型 | 15 |

验证命令：

```bash
cd mini-volcano
gofmt -l .
go vet ./...
go build ./...
go test ./... -count=1
go run ./cmd/minivolcano -all
```

## F. 已知限制（MiniVolcano）

- 不是 CRD，不兼容真实 API。
- 无 informer/API Server/乐观并发。
- 无 webhook/RBAC/安全。
- 无 DRA/MIG/vGPU/多集群。
- `Reservation` 是教学抽象，真实 Volcano 无此 CRD。
- Cron 用 tick，不是 cron 表达式。
- JobFlow 内联 JobSpec，不引用 JobTemplate。
- 实现了 task 级 `minTaskMember` 与 `minSuccess`，但不实现 `tasks[].dependsOn`、作业级 `volumes`、Queue `spec.affinity`、`subGroupPolicy`/`partitionPolicy` 与作业插件。
- `minSuccess` 只实现“达到阈值即 Completed 并终止未启动副本”，没有模拟“全部终止后仍不足 minAvailable 则 Failed”的第二阶段。
- 显式 `minAvailable: 0` 在 Mini 中表示关闭 gang（教学扩展）；上游 webhook 会把 0 补成 Σ task 有效 minima。
- `MinAvailableSet` 是 `json:"-"`：通过 JSON 反序列化 JobSpec 时无法区分“未设置”与“显式 0”，`MutateJob` 会重新默认。Mini 的 Cron/JobFlow 目前用 Go 构造，未触发；作为 SDK 使用时要注意。
- shuffle/rescheduling 的受害者保护已加入角色门槛（`OccupyingCountByTask`），但没有针对“多角色受害者合并”的专门测试；相关路径由 preempt/allocate 测试间接覆盖。
- TDM 不自动驱逐窗口结束后的 Pod。
- 有简化的 PDB、resourcequota、nodegroup 和 group-topology-affinity；仍缺 usage、deviceshare、extender、cdp、gangPreempt/gangReclaim（`reserve` 已从 v1.16 移除）。
- PDB 没有控制器状态（`disruptionsAllowed`）、Eviction API 与健康探测；ResourceQuota 只在 enqueue 检查已准入作业；NodeGroup 用类型字段而非上游注解。
- `group-topology-affinity` 用数字 tier 而不是上游的 `topologyTierName`，只实现 PodGroup 级反亲和；默认同 namespace，没有 namespace selector，也没有完整的 CRD 校验。
- 重调度没有真实五分钟冷却与分维策略，也没有 checkpoint 成本模型。
- 无真实性能/并发测试。
- `-race` 未运行（CGO 关闭）。
- `scripts/capacity_model.py` 是工程估算模型，不建模网络拥塞、存储 IO、故障率和调度开销；输入默认值不是 benchmark。

## G. 参考资料

官方文档与发布：

- Volcano 官网：https://volcano.sh/
- 架构：https://volcano.sh/docs/home/architecture/
- 调度器配置：https://volcano.sh/docs/userguide/user_guide_how_to_configure_scheduler/
- v1.16 发布（CNCF）：https://www.cncf.io/blog/2026/10/28/volcano-v1-16-released-namespacequeue-fairshare-and-a-broader-hypernode-ecosystem/
- v1.15 发布：https://volcano.sh/blog/volcano-1.15.0-release/
- v1.14 发布：https://volcano.sh/blog/volcano-1.14.0-release/
- v1.13 发布：https://volcano.sh/blog/volcano-1.13.0-release/
- 网络拓扑用户指南：https://volcano.sh/zh-hans/docs/v1.15.0/userguide/user_guide_how_to_use_network_topology_aware_scheduling/
- HyperNode API：https://pkg.go.dev/volcano.sh/apis@v1.15.0/pkg/apis/topology/v1alpha1
- DRA 队列配额：https://github.com/volcano-sh/volcano/blob/main/docs/user-guide/how_to_use_dra_quota.md
- JobFlow 示例：https://github.com/volcano-sh/volcano/tree/master/example/jobflow
- JobFlow patch 覆盖（PR #4411）：https://github.com/volcano-sh/volcano/pull/4411
- Job TTL：https://volcano.sh/docs/v1.16.0/userguide/user_guide_how_to_use_job_ttl/
- Cron VolcanoJob：https://volcano.sh/zh-hans/docs/v1.8.2/concepts/cronvolcanojob/
- PriorityClass：https://volcano.sh/en/docs/user-guide/how_to_configure_priorityclass_for_job/
- 队列文档：https://volcano.sh/zh/docs/v1-9-0/queue/
- 队列状态设计：https://github.com/volcano-sh/volcano/blob/master/docs/design/queue/queue-state-management.md
- Volcano Global/HyperJob：https://volcano.sh/blog/volcano-1.14.0-release/
- Kthena：https://kthena.volcano.sh/
- iFLYTEK 案例：https://volcano.sh/blog/iflytek_case_study/
- CNCF 项目状态：https://www.cncf.io/projects/volcano/
- NamespaceQueue 用户指南：https://volcano.sh/docs/userguide/user_guide_how_to_use_namespace_queue/
- NamespaceQueue 设计：https://github.com/volcano-sh/volcano/blob/master/docs/design/namespace-queue.md
- fairshare 用户指南：https://github.com/volcano-sh/volcano/blob/master/docs/user-guide/how_to_use_fairshare_plugin.md
- shuffle action 设计/PR：https://github.com/volcano-sh/volcano/pull/2174
- rescheduling 插件：https://pkg.go.dev/volcano.sh/volcano/pkg/scheduler/plugins/rescheduling
- PDB 插件设计：https://github.com/volcano-sh/volcano/blob/master/docs/design/pdb-plugin.md
- PodGroup 反亲和：https://github.com/volcano-sh/volcano/blob/master/docs/user-guide/how_to_use_podgroup_anti_affinity.md
- NodeGroup 插件：https://github.com/volcano-sh/volcano/blob/master/docs/user-guide/how_to_use_nodegroup_plugin.md
- lifecycle 插件（华为云 CCE 文档）：https://support.huaweicloud.com/intl/en-us/usermanual-cce/cce_10_0193.html
- vGPU 用户指南：https://volcano.sh/docs/userguide/user_guide_how_to_use_volcano_vgpu/

LLM 工作负载（第 34 章）：

- Megatron-LM：https://github.com/NVIDIA/Megatron-LM
- DeepSpeed：https://www.deepspeed.ai/
- vLLM：https://docs.vllm.ai/
- TorchFT：https://github.com/pytorch/torchft
- Kubeflow Trainer：https://www.kubeflow.org/docs/components/trainer/
- KServe：https://kserve.github.io/website/
- NIXL（KV cache 传输）：https://github.com/ai-dynamo/nixl

竞争与市场：

- Kueue：https://kueue.sigs.k8s.io/
- YuniKorn：https://yunikorn.apache.org/
- KAI Scheduler：https://github.com/kai-scheduler/KAI-scheduler
- Slurm：https://slurm.schedmd.com/
- Run:ai：https://www.run.ai/
- scheduler-plugins：https://github.com/kubernetes-sigs/scheduler-plugins
- LWS：https://github.com/kubernetes-sigs/lws
- KubeRay：https://github.com/ray-project/kuberay
- Kubeflow Trainer：https://www.kubeflow.org/

市场数据（口径不同，谨慎比较）：

- The Business Research Company, AI Infrastructure Market Report 2026.
- Mordor Intelligence, AI Infrastructure-as-a-Service Market.
- Meticulous Research / ResearchAndMarkets, AI IaaS Market.
- Synergy Research/Gartner 等云资本开支汇总（媒体引用）。

安全：

- CVE-2026-44247（webhook OOM，CVSS 6.8）。
- GHSA-j38h-7pfq-cxmw（DRA 容量核算，CVSS 6.5）。
- 订阅 volcano-sh/volcano 的 security advisory。

## H. 版本事实表

| 事实 | 值 |
|---|---|
| 最新稳定版 | v1.16.0（GitHub tag 2026-10-14；CNCF 公告 2026-10-28） |
| v1.16 依赖 | Kubernetes 1.36.1，Go 1.26 |
| CNCF 状态 | Incubating（截至 2026-11） |
| 开源协议 | Apache-2.0 |
| 前身 | kube-batch |
| 开源时间 | 2019-06 |
| 进入 CNCF Sandbox | 2020 |
| 进入 Incubating | 2022-04 |

## I. 如何使用本教程

- 想快速体验：第 3 章 + `mini-volcano -scenario basic`。
- 想理解 gang/公平/抢占：第 6–9 章。
- 想二次开发：第 7（源码地图）、21、22（shuffle/PDB 走读）、24 章。
- 想动手做实验：第 27 章（14 个实验，13 个离线 + 1 个真实集群）。
- 想补形式化/不变量：第 26 章。
- 想做选型/迁移/容量：第 1、19、28 章。
- 想做 LLM 平台：第 13、34 章（异构设备与训练/推理/Agent 工作负载），再读第 19.13 节的平台 API。
- 想做商业分析：第 30–32 章；算容量/ROI 用附录 U 的 `capacity_model.py`。
- 想验证：第 25 章 + `VERIFICATION.md`；重调度看场景 `shuffle`，中断保护看 `pdb`，分组反亲和看 `group-topology`。
- 想直接抄配置：附录 L（七种配置配方）。
- 想查常见问题：附录 M（FAQ）。
- 想接平台/自动化提交：附录 N。
- 想升级/评估覆盖度：附录 O、P。
- 想补 Kubernetes 调度基础：附录 Q。
- 想按错误信息排障：附录 R。
- 想把机制串成一条完整轨迹：附录 T（14 tick 混合负载复盘）。
- 想算容量/排队/ROI：附录 U（可运行计算器）。
- 想演练生产故障：附录 V（七个剧本）。
- 想复刻/扩展 task 级 gang：附录 W（设计、事务、状态机、测试矩阵与差异表）。
- 想自测：附录 S（35 题，含 LLM 与容量模型）。

教程中的数字、版本、CRD 字段以 2026-11 的公开资料为准；Volcano 迭代快，升级前请查阅目标版本的 release notes 与 CRD。

## J. Action / Plugin 全表与源码地图

### J.1 Actions

| Action | 范围 | 主要扩展点 | 源码目录 | Mini |
|---|---|---|---|---|
| `enqueue` | 作业准入 | JobValid、JobEnqueueable | `actions/enqueue/` | ✅ |
| `allocate` | 作业内放置 | Predicate、NodeOrder、JobOrder、TaskOrder、JobReady | `actions/allocate/` | ✅ |
| `backfill` | 空闲填充 | Predicate、NodeOrder | `actions/backfill/` | ✅ |
| `preempt` | 同队列 | Preemptable（gang/pdb） | `actions/preempt/` | ✅ 简化 |
| `reclaim` | 跨队列 | Reclaimable、Overused、Allocatable | `actions/reclaim/` | ✅ 简化 |
| `shuffle` | 集群布局 | VictimTasks（rescheduling/tdm）+ Disruptable（pdb） | `actions/shuffle/` | ✅ 简化（rescheduling 提名 + pdb 过滤） |
| `reserve` | 预留（旧） | reservation 插件 | 已移除（v1.16 无该目录） | ❌ |
| `gangpreempt` | 作业粒度抢占 | gang、拓扑、放置模拟 | `actions/gangpreempt/` | ❌ |
| `gangreclaim` | 作业粒度回收 | gang、拓扑、放置模拟 | `actions/gangreclaim/` | ❌ |

### J.2 Plugins（按职责分组）

| 组 | 插件 | 关键扩展点 | Mini |
|---|---|---|---|
| 排序/硬语义 | `priority` | JobOrder、TaskOrder、Preemptable | ✅ |
| 排序/硬语义 | `gang` | JobOrder、JobReady、JobPipelined、JobValid、JobStarving、Preemptable | ✅ |
| 排序/硬语义 | `conformance` | JobValid | ✅ |
| 公平/配额 | `drf` | JobOrder、TaskOrder | ✅ |
| 公平/配额 | `proportion` | QueueOrder、Allocatable、Overused、Reclaimable | ✅ |
| 公平/配额 | `capacity` | QueueOrder、Allocatable、Overused、Reclaimable | ✅ |
| 公平/配额 | `fairshare` | JobOrder、JobEnqueueable | ✅ 简化 |
| 公平/配额 | `sla` | JobOrder、JobEnqueueable | ✅ |
| 公平/配额 | `overcommit` | JobEnqueueable | ✅ |
| 公平/配额 | `resourcequota` | JobEnqueueable | ✅ 简化 |
| 公平/配额 | `nodegroup` | Predicate、NodeOrder | ✅ 简化（Predicate） |
| 节点选择 | `predicates` | Predicate | ✅ |
| 节点选择 | `nodeorder` | NodeOrder | ✅ |
| 节点选择 | `lifecycle` | NodeOrder | ❌（发行版扩展） |
| 节点选择 | `binpack` | NodeOrder | ✅ |
| 节点选择 | `resource-strategy-fit` | NodeOrder | ✅ |
| 节点选择 | `usage` | Predicate、NodeOrder | ❌ |
| 节点选择 | `numa-aware` | Predicate、NodeOrder | ✅ 简化 |
| 拓扑 | `task-topology` | Predicate、NodeOrder | ✅ |
| 拓扑 | `network-topology-aware` | Predicate、NodeOrder、JobOrder | ✅ 简化 |
| 拓扑 | `group-topology-affinity` | Predicate、NodeOrder | ✅ 简化（数字 tier；成对评估） |
| 拓扑 | `tdm` | Predicate、NodeOrder、VictimTasks | ✅ 简化（未实现 VictimTasks） |
| 设备 | `deviceshare` | Predicate、NodeOrder | ❌ |
| 重调度 | `rescheduling` | VictimTasks | ✅ 简化 |
| 重调度 | `cdp` | Preemptable、Reclaimable（受害者过滤） | ❌ |
| 重调度 | `pdb` | Preemptable、Reclaimable、Disruptable | ✅ 简化 |
| 扩展 | `reservation` | JobOrder、Predicate | ✅（教学抽象） |
| 扩展 | `extender` | 多个 | ❌ |

### J.3 源码入口

| 想读什么 | 从哪里开始 |
|---|---|
| 调度器主循环 | `pkg/scheduler/scheduler.go` |
| 集群快照/账本 | `pkg/scheduler/cache/cache.go` |
| 作业/队列/节点视图 | `pkg/scheduler/api/{job_info,queue_info,node_info}.go` |
| Session/扩展点 | `pkg/scheduler/framework/session.go` |
| 事务 | `pkg/scheduler/framework/statement.go` |
| 分配主循环 | `pkg/scheduler/actions/allocate/allocate.go` |
| gang | `pkg/scheduler/plugins/gang/gang.go` |
| 队列份额 | `pkg/scheduler/plugins/proportion/proportion.go` |
| 重调度 | `pkg/scheduler/plugins/rescheduling/` |
| shuffle | `pkg/scheduler/actions/shuffle/shuffle.go` |
| PDB | `pkg/scheduler/plugins/pdb/` |
| 配置解析 | `pkg/scheduler/conf/` |
| task 级 gang（minTaskMember） | `pkg/scheduler/api/job_info.go` 的 `ParseMinMemberInfo` / `CheckTaskValid` |
| 作业插件接口 | `pkg/controllers/job/plugins/interface/interface.go` |
| 作业插件实现 | `pkg/controllers/job/plugins/{env,svc,ssh}/`、`distributed-framework/` |
| 作业插件调用点 | `pkg/controllers/job/job_controller_plugins.go` |

### J.4 MiniVolcano 源码对照

| 想读什么 | 文件 |
|---|---|
| 资源算术/偏序 | `internal/api/resource.go` |
| 对象与深拷贝 | `internal/api/types.go` |
| 校验与 admission 默认值 | `internal/api/validate.go` |
| 账本/快照/预留 | `internal/cache/cache.go` |
| Session/hook/tier | `internal/framework/session.go` |
| Statement 事务 | `internal/framework/statement.go` |
| 插件注册表 | `internal/plugins/registry.go` |
| gang/DRF/proportion/capacity | `internal/plugins/{gang,drf,proportion,capacity}.go` |
| 拓扑/NUMA/设备 | `internal/plugins/{network_topology,group_topology,task_topology,numa}.go` |
| 重调度/PDB/配额/节点组 | `internal/plugins/{rescheduling,pdb,resourcequota,nodegroup}.go` |
| action 流水线 | `internal/actions/*.go` |
| 控制器/Cron/JobFlow | `internal/controller/*.go` |
| task 级 gang / minSuccess | `internal/plugins/gang.go`、`internal/actions/allocate.go`、`internal/controller/manager.go` |
| 场景与断言 | `internal/scenario/*.go` |
| CLI | `cmd/minivolcano/main.go` |

## K. 故障排查索引

| 症状 | 优先看 | 常用命令 |
|---|---|---|
| Job 一直 Pending | schedulerName → PodGroup phase → Queue state | `kubectl get pod -o yaml`、`kubectl describe podgroup`、`kubectl get queue` |
| Pod 已创建但未绑定 | gang 是否满足、队列 capability、节点形状 | `kubectl describe vcjob`、`kubectl logs deploy/volcano-scheduler` |
| 作业部分运行 | minAvailable < 总副本、资源不足 | `kubectl get vcjob -o yaml`、`kubectl get podgroup -o yaml` |
| 某个角色没启动但作业 Running | task `minAvailable` 是否被作业门槛覆盖（`job.minAvailable >= Σ task.minAvailable`） | `kubectl get podgroup -o jsonpath='{.spec.minTaskMember}'` |
| 下游 task 一直不创建 | `dependsOn` 的上游是否达到其 `minAvailable`（Running 且容器 Ready） | `kubectl get pods -l volcano.sh/job-name=<job>`、`kubectl describe vcjob` |
| Pod 卡在创建前 | 作业插件 `OnPodCreate` 报错、插件名未注册 | `kubectl describe vcjob` Events、controller 日志 `grep -i plugin` |
| 该抢不抢 | preempt 是否启用、优先级、PDB、gang、cdp 冷却 | `kubectl get events`、scheduler 日志 `grep -i preempt` |
| PDB 阻止驱逐 | selector 是否匹配、minAvailable 是否已到下限、插件是否启用 | `kubectl describe pdb`、`kubectl get pdb -o yaml` |
| 配额未超却被拒 | ResourceQuota 统计的是已准入作业；检查 namespace 与 hard 维度 | `kubectl get resourcequota -n <ns> -o yaml`、scheduler 日志 `grep -i quota` |
| 节点组作业 Pending | 节点标签、队列 `spec.affinity`、`strict`、组容量 | `kubectl get node --show-labels`、`kubectl get queue -o yaml`、`grep -i nodegroup` |
| 节点组作业落到错误池 | required 是否排除了目标组、antiAffinity 是否命中、preferred 是否同分 | `kubectl get queue <name> -o yaml` |
| 反复驱逐 | 配额/权重抖动、shuffle 与打分方向冲突 | `kubectl get events --field-selector reason=Evicted` |
| shuffle 后没变好 | 是否被放回原节点、PDB/gang 保护 | `kubectl get events`、`kubectl describe pdb` |
| 队列关不掉 | 仍有 PodGroup、Closing | `kubectl get queue -o yaml` |
| 提交被拒 | webhook/证书/队列不存在 | `kubectl describe vcjob`、admission 日志 |
| 调度器 CrashLoop | configmap 语法、插件名、tier 重复 | `kubectl logs deploy/volcano-scheduler --previous` |
| GPU 不可用 | device plugin、资源名、污点、MIG/DRA | `kubectl describe node`、`kubectl get resourceclaim` |
| NamespaceQueue 作业被拒 | Ready/Authorized condition、allowedNamespaces | `kubectl describe namespacequeue -n <ns>` |
| 公平性异常 | fairshare 顺序、DRF/proportion tier、usage | `kubectl get cm volcano-scheduler-configmap -o yaml` |

一个通用原则：**先确认对象在哪一层被拒绝**（API Server / admission / enqueue / allocate / bind / kubelet），再看该层的证据。跨层猜测是排障时间的主要浪费。

## L. 调度器配置配方

以下配置是**起点**，不是经过生产验证的“最佳实践”。复制后先在测试集群跑一遍，确认插件名、tier 顺序与参数在当前版本存在。

### L.1 最小 gang

适合：只想要 all-or-nothing，不需要队列公平/抢占。

```yaml
actions: "enqueue, allocate, backfill"
tiers:
- name: tier-1
  plugins:
  - name: priority
  - name: gang
  - name: conformance
- name: tier-2
  plugins:
  - name: predicates
  - name: nodeorder
```

注意：没有 `preempt`/`reclaim` 时，高优先级作业也只能等；`gang` 的 `enableJobStarving` 默认 true（plugin option），显式写上更清楚。

### L.2 多租户扁平队列

适合：部门/团队数量固定，按权重公平，允许借用与回收。

```yaml
actions: "enqueue, allocate, backfill, preempt, reclaim"
tiers:
- name: tier-1
  plugins:
  - name: priority
  - name: gang
    enablePreemptable: true
  - name: conformance
- name: tier-2
  plugins:
  - name: drf
  - name: predicates
  - name: proportion
  - name: nodeorder
```

Queue 侧至少配置 `weight`；需要保底时加 `guarantee`，需要硬上限时加 `capability`。

### L.3 层级 capacity

适合：部门 → 团队 → 项目，父队列控制总盘子。

```yaml
actions: "enqueue, allocate, backfill, preempt, reclaim"
tiers:
- name: tier-1
  plugins:
  - name: priority
  - name: gang
  - name: conformance
- name: tier-2
  plugins:
  - name: drf
  - name: predicates
  - name: capacity
    enableHierarchy: true
  - name: nodeorder
```

不要同时启用 `proportion`。父队列通常不直接承载作业；子队列的 `guarantee` 之和不要超过父队列 `capability`。

### L.4 拓扑/HPC

适合：MPI、超大 gang、RDMA 网络、NUMA 敏感。

```yaml
actions: "enqueue, allocate, backfill, preempt, reclaim"
tiers:
- name: tier-1
  plugins:
  - name: priority
  - name: gang
  - name: conformance
- name: tier-2
  plugins:
  - name: predicates
  - name: task-topology
  - name: network-topology-aware
  - name: numa-aware
  - name: proportion
  - name: nodeorder
```

`network-topology-aware` 依赖 HyperNode 对象；没有拓扑数据时不要启用 hard 模式，否则所有作业都会 Pending。

### L.5 推理/LWS

适合：多副本 serving、PDB 保护、组间故障域打散。

```yaml
actions: "enqueue, allocate, backfill, preempt, reclaim, shuffle"
tiers:
- name: tier-1
  plugins:
  - name: priority
  - name: gang
  - name: pdb
  - name: conformance
- name: tier-2
  plugins:
  - name: predicates
  - name: group-topology-affinity
  - name: binpack
    arguments: {binpack.weight: "2"}
  - name: nodeorder
```

推理通常不希望 `shuffle` 频繁搬动副本；如果要开，先把 PDB 和冷却间隔配好，并从低优先级队列灰度。

### L.6 Kueue + Volcano

适合：Kueue 管准入/配额，Volcano 只管 gang/拓扑/设备。

```yaml
actions: "enqueue, allocate, backfill"
tiers:
- name: tier-1
  plugins:
  - name: priority
  - name: gang
  - name: conformance
- name: tier-2
  plugins:
  - name: predicates
  - name: network-topology-aware
  - name: nodeorder
```

关键：Volcano 的 Queue 不要再配一套与 Kueue ClusterQueue 冲突的 `capability`/`guarantee`，否则会出现两本账。只让一层做配额决策。没有 HyperNode 数据时不要启用 `network-topology-aware`，否则 hard 模式会让作业 Pending。

### L.7 LLM 三池四队列

适合：训练、微调、推理、Agent 分池分队列。队列清单见 `examples/queues/llm-queues.yaml`，工作负载细节见第 34 章。

```yaml
actions: "enqueue, allocate, backfill, preempt, reclaim"
tiers:
- name: tier-1
  plugins:
  - name: priority
  - name: gang
    enablePreemptable: true
  - name: pdb
  - name: conformance
- name: tier-2
  plugins:
  - name: drf
  - name: predicates
  - name: proportion
  - name: network-topology-aware
  - name: group-topology-affinity
  - name: nodeorder
  - name: binpack
    arguments: {binpack.weight: "5"}
```

三个注意点：

1. 训练队列不要启用 `shuffle`/`rescheduling`；驱逐一个 rank 可能触发整个作业重启。
2. 推理队列的 PDB 必须在 tier 1，并且 `minAvailable` 不要等于副本数。
3. 没有 HyperNode 数据时不要启用 `network-topology-aware` 的 hard 模式；`group-topology-affinity` 也要先给 PodGroup 打标签。

## M. 常见问题（FAQ）

**Q1：Volcano 和 Kueue 是替代关系吗？**
不是。Kueue 管“作业何时准入/配额”，Volcano 管“Pod 放哪/如何 gang”。可以组合，但只能让一层管配额。

**Q2：作业一直 Pending，第一步看什么？**
按顺序：Pod 的 `schedulerName` 是否为 `volcano` → PodGroup 的 phase → Queue 是否 Open/有配额 → 节点形状/污点 → scheduler 日志。80% 的问题在前三步。

**Q3：`minAvailable` 省略和设为 0 有什么区别？**
在 upstream 没有区别：创建时 mutating webhook 都会把 0 补成“所有 task 的 minAvailable/replicas 之和”，CRD 的 int32 无法区分省略与显式 0。0 不是关闭 gang 的开关；要弱化 gang 用 1。MiniVolcano 用额外的 `MinAvailableSet` 位区分，是教学模型，不要套到真实 CRD。

**Q4：`capability` 和 `guarantee` 哪个是“保证拿到”？**
`guarantee` 是保底，其他队列不能通过回收击穿；`capability` 是硬上限，不保证拿得到。空闲时其他队列可以借用，但 `guarantee` 部分不能被回收。

**Q5：preempt 和 reclaim 有什么区别？**
preempt 在同队列内按优先级抢；reclaim 跨队列按 deserved/overused 回收。混用会让“高优先级但低配额”的作业抢走别人的保底。

**Q6：配置了 PDB，为什么抢占还是把副本打掉了？**
PDB 保护由 `pdb` 插件提供，不启用插件就没有保护；还要确认 selector 匹配 Pod、`disruptionsAllowed > 0`、以及 PDB 和 gang 的 minAvailable 没有互相矛盾。

**Q7：gang 作业为什么部分 Running？**
先看 `minAvailable` 是否小于总副本；这是弹性语义，不是故障。如果 `minAvailable` 等于总副本却部分 Running，检查是否有人绕过了 Volcano（`schedulerName` 写错）或 gang 插件未启用。

**Q8：Queue 改成 Closed 后一直关不掉？**
状态是 `Closing`，说明还有 PodGroup 未完成。等作业结束或先终止作业；只有 `status.state=Closed` 才能删除。

**Q9：scheduler CrashLoopBackOff 最常见的原因？**
scheduler ConfigMap 的 YAML 语法、插件名拼写、tier 重复/为空、feature gate 与 CRD 不匹配。先看 `--previous` 日志。

**Q10：必须替换 kube-scheduler 吗？**
不必。不同 Pod 可以用不同 `schedulerName` 共存；但两个调度器不共享配额账本，必须用节点池/taint 隔离，或只让一层调度批作业。

**Q11：GPU 共享用 deviceshare 还是 MIG？**
要硬件隔离选 MIG；要显存/算力比例共享选 vGPU（HAMi/Volcano deviceshare）。两者资源名和账本不同，不要混算。共享模式需要配套 device plugin，光开调度器插件不够。

**Q12：DRA 的队列配额怎么算？**
v1.15+ 的 capacity 插件支持 `deviceclass/<class>` 与 `<dim>.deviceclass/<class>` 键；共享 claim 只计一次。升级前确认 feature gate、驱动版本和部分分配模式是否覆盖。

**Q13：NamespaceQueue 和普通 Queue 怎么选？**
队列树稳定、变更少 → 普通 Queue；需要“平台管总账、租户管子树” → NamespaceQueue（v1.16 Alpha，默认关闭，必须配层级 capacity）。注意不要与 proportion 同时启用。

**Q14：升级 Volcano 先升什么？**
一般顺序：备份 → CRD → controller → scheduler → admission。升级前读 release notes 和 CRD diff；NamespaceQueue 的 CRD 在 v1.16 没有 keep 注解，卸载可能连带删除。

**Q15：MiniVolcano 能直接上生产吗？**
不能。它是语义模拟器：没有 API Server、informer、并发、RBAC、DRA、多集群。它的价值是验证算法与测试方法，不是替代 Volcano。

**Q16：为什么测试全绿还会有语义 bug？**
测试只能证明已覆盖的路径；断言可能编码了错误行为。需要独立构造反例、检查账本守恒、做随机不变量与确定性对比。第 25 章有 12 个真实复盘。

**Q17：task 的 `minAvailable` 为什么没生效？**
先看 `job.minAvailable` 是否 ≥ 所有 task 的 `minAvailable` 之和。小于时上游会跳过逐角色检查，只按总数判断；此时 `minTaskMember` 仍在，但不会约束调度。其次确认 Pod 有 `volcano.sh/task-spec` 标签/注解（裸 Pod 场景）。

**Q18：Queue 写了 `required` 节点组，为什么还能落到别的组？**
v1.16 的实现把 `required` 与 `preferred` 合并成“允许集”，`required` 只是高分；把目标组写进 `preferred` 也会允许它。要排他，只保留 `required` 且不要把其他组写进 `preferred`。反亲和也有同类怪癖：同组同时出现在 `antiAffinity.required` 与 `antiAffinity.preferred` 时，最终是“允许但 -1 分”。另外 `strict` 默认 true，**队列没有 affinity 时任何节点都不落**（带标签节点也会被 `affinity not specified` 拒绝）；只有在 `strict: false` 时才放行不带标签的节点。

## N. 提交作业的三种方式

### N.1 kubectl + YAML

最通用，适合 GitOps：

```bash
kubectl apply -f examples/jobs/vcjob-pytorch.yaml
kubectl get vcjob,podgroup -w
```

### N.2 vcctl

适合交互式查看与简单操作：

```bash
vcctl job list
vcctl job view pytorch-dist
vcctl queue list
```

不同版本的子命令可能不同，以 `vcctl --help` 为准。复杂排障仍建议用 `kubectl get ... -o yaml`。

### N.3 client-go / volcano clientset

适合平台后端自动提交。示意代码（import 路径以 `volcano.sh/apis` 版本为准）：

```go
import (
    batchv1alpha1 "volcano.sh/apis/pkg/apis/batch/v1alpha1"
    volcano "volcano.sh/apis/pkg/client/clientset/versioned"
    corev1 "k8s.io/api/core/v1"
    metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
    "k8s.io/client-go/rest"
)

func submit(ctx context.Context, podTemplate corev1.PodTemplateSpec) error {
    cfg, err := rest.InClusterConfig()
    if err != nil { return err }
    client, err := volcano.NewForConfig(cfg)
    if err != nil { return err }

    job := &batchv1alpha1.Job{
        ObjectMeta: metav1.ObjectMeta{Name: "train", Namespace: "default"},
        Spec: batchv1alpha1.JobSpec{
            SchedulerName: "volcano",
            Queue:         "team-a",
            MinAvailable:  2,
            Tasks: []batchv1alpha1.TaskSpec{{
                Name:     "worker",
                Replicas: 2,
                Template: podTemplate,
            }},
        },
    }
    _, err = client.BatchV1alpha1().Jobs("default").Create(ctx, job, metav1.CreateOptions{})
    return err
}
```

平台侧要注意：

- 用 ServiceAccount + RBAC，不要用个人 kubeconfig；
- 提交前校验 queue/priority 是否在租户授权范围内；
- 记录提交人与作业的映射，便于审计与计费；
- 对 `AlreadyExists` 做幂等处理。

### N.4 非 Volcano 工作负载

Deployment/Job 也可以接入，只要创建 PodGroup 并给 Pod 加注解：

```yaml
metadata:
  annotations:
    scheduling.k8s.io/group-name: web-gang
spec:
  schedulerName: volcano
```

注意滚动更新：新 Pod 等旧 Pod 释放，gang 语义可能让更新卡住。

## O. 升级与 CRD 兼容性检查清单

升级前：

- [ ] 读目标版本 release notes、CRD diff、deprecation 列表。
- [ ] 备份 CRD、Queue/PriorityClass/HyperNode/NamespaceQueue 对象、scheduler ConfigMap。
- [ ] 确认目标版本支持的 Kubernetes 版本（v1.16 对应 1.36）。
- [ ] 检查 feature gates：NamespaceQueue、DRA、Sharding、Agent Scheduler 是否变化。
- [ ] 在 staging 跑一遍示例作业与 `-all`（Mini 只验证语义，不验证升级）。
- [ ] 准备回滚镜像与 Helm values。

升级顺序：

```text
1. CRD（先 apply，保持向后兼容）
2. controller-manager
3. scheduler
4. admission / webhook 证书
5. 验证：默认队列、示例作业、指标、PDB、NamespaceQueue condition
```

升级后验证：

```bash
kubectl get pods -n volcano-system
kubectl get crd | grep volcano
kubectl get queue
kubectl apply -f examples/jobs/vcjob-pytorch.yaml
kubectl get vcjob,podgroup
kubectl -n volcano-system logs deploy/volcano-scheduler --tail=100
```

回滚条件：

- CRD 升级后旧控制器无法读写；
- webhook 证书/端口不兼容导致所有提交失败；
- 调度结果出现账本不守恒；
- P99 延迟超过基线 2 倍。

注意：CRD 回滚可能丢字段；NamespaceQueue 在 v1.16 卸载时可能被一并删除。生产升级前先做对象导出。

## P. 功能覆盖矩阵

这张表回答“教程讲了吗、Mini 实现了吗、和真实 Volcano 差在哪”。状态含义：完整 = 语义与主要边界都覆盖；简化 = 核心语义可运行但有明确取舍；未实现 = 只在文档中讲。

| 能力 | 教程章节 | MiniVolcano | 说明 |
|---|---|---|---|
| Job / PodGroup / Queue | 2、4、5 | 语义完整 | 自定义 Go 结构，不是 CRD |
| gang / minAvailable / minTaskMember | 6 | 简化 | 单事务 all-or-nothing，无跨 Session Pipelined；实现 task 级角色门槛与生效条件 |
| proportion | 5、8 | 简化 | 不做剩余份额再分配 |
| capacity 层级 | 5、8 | 简化 | 单继承树，无跨树借用 |
| DRF | 8 | 简化 | 当前分配 + pending 需求 |
| fairshare | 8 | 简化 | tick 半衰期，不按小时 |
| SLA | 8、12 | 简化 | 只排序，不 pipeline |
| preempt | 9 | 简化 | 同队列、优先级 + 创建时间排序 |
| reclaim | 9 | 简化 | 有 guarantee 击穿保护 |
| gangPreempt / gangReclaim | 6、9 | 未实现 | 文档讲语义与取舍 |
| shuffle / rescheduling | 9 | 简化 | LNU + 教学 fragmentation，无五分钟冷却 |
| PDB | 9 | 简化 | 无 disruptionsAllowed/Eviction API |
| ResourceQuota | 5 | 简化 | 仅 enqueue 检查已准入作业 |
| NodeGroup | 5 | 简化 | 类型字段模拟 affinity + 分组上限，非上游 CRD |
| task-topology | 11 | 简化 | 同节点/不同节点 |
| HyperNode hard/soft | 11 | 简化 | 数字 tier，无自动发现/LCA |
| group-topology-affinity | 11 | 简化 | 数字 tier、成对评估、默认同 namespace |
| NUMA | 10、13 | 简化 | 单 NUMA 适配，无设备拓扑 |
| usage（真实利用率） | 10 | 未实现 | 只有 requests 账本 |
| lifecycle（生命周期聚拢） | 10 | 未实现 | 发行版扩展（如 CCE），不在 upstream v1.16 |
| deviceshare / MIG / vGPU | 13 | 未实现 | 只有标量资源 |
| DRA | 13 | 未实现 | 文档讲配额与生命周期 |
| CronJob | 14 | 简化 | tick 周期，不是 cron 表达式 |
| JobFlow | 14 | 简化 | 内联 JobSpec，不引用 JobTemplate |
| 作业插件（svc/ssh/pytorch） | 4、14、33.6 | 未实现 | 控制面注入；不参与调度决策，但可改 Pod spec 约束放置；第 33.6 节给出开发骨架 |
| task 级 gang（minTaskMember） | 4、6、11 | 简化 | 实现角色门槛、生效条件与场景；不实现 dependsOn/SubGroupPolicy |
| minSuccess 提前完成 | 4 | 简化 | 达到阈值即 Completed 并终止未启动副本；不模拟“全部终止后不足 minAvailable 则 Failed” |
| Queue × NodeGroup affinity | 5 | 简化 | Mini 只有 `nodeGroup.names` 名单，无 required/preferred/strict |
| cdp 冷却保护 | 9 | 未实现 | 文档讲受害者过滤语义与注解 |
| NamespaceQueue | 5 | 未实现 | 文档讲授权/父子/配额 |
| Volcano Global / 多集群 | 20 | 未实现 | 单集群模拟器 |
| 安全 / RBAC / Webhook | 17 | 未实现 | 无 API Server |
| HA / 性能 / 分片 | 18、19 | 未实现 | 单进程、深拷贝 |
| 可观测性 | 16 | 简化 | 事件、指标、explain、what-if |
| 非 NVIDIA 加速器（昇腾/AMD/国产） | 13 | 未实现 | 依赖 device plugin/驱动与资源名 |
| LLM 训练/微调/推理/Agent | 34 | 文档 + 示例 | 用 Volcano 原语组合，不是新调度语义 |
| 混合负载综合轨迹 | 附录 T | 文档 | 由现有场景组合推演，不是新场景 |
| 容量/排队/ROI 计算器 | 32、附录 U | `scripts/capacity_model.py` | 工程估算，不是 benchmark |
| 生产故障演练 | 附录 V | 文档 | 需要真实集群/受控环境 |

这张表也是后续扩展的路线图：优先补 `usage`、`deviceshare`、`gangPreempt`，再考虑 DRA 与 NamespaceQueue；每补一项都要同步场景、测试与文档计数。

## Q. Kubernetes 调度基础与 Volcano 对照

这一节给不熟悉 Kubernetes 调度的读者一个最小背景。已经熟悉 kube-scheduler 的读者可以跳过。

### Q.1 一次 Pod 调度发生了什么

```text
1. 控制器创建 Pod，spec.nodeName 为空
2. kube-scheduler 的 informer 收到 Pod
3. 调度周期：PreFilter → Filter → PostFilter → Score → Reserve → Permit → PreBind → Bind → PostBind
4. 选中节点后，写 Binding（设置 nodeName）
5. 目标节点的 kubelet 看到 Pod，拉镜像、启动容器
```

关键点：**调度器只决定 nodeName，不启动容器**。Pod 是否 Running 取决于 kubelet、镜像、卷、网络。

### Q.2 Filter 与 Score

- **Filter**：硬过滤，不满足的节点直接排除（资源、污点、亲和、端口、卷）。
- **Score**：软打分，在所有可行节点中选最高分（空闲、均衡、拓扑、镜像本地性）。
- **Bind**：写回 API Server。

Volcano 的 Predicate/NodeOrder 与这两个阶段一一对应，只是决策单位从 Pod 提升到 PodGroup。

### Q.3 优先级与抢占

Kubernetes 有 `PriorityClass` 和 `preemptionPolicy`。默认调度器在 Pod Pending 时可以驱逐低优先级 Pod。但它：

- 以 Pod 为单位，不理解 gang；
- 不理解队列份额；
- 不做跨租户回收。

Volcano 复用 PriorityClass，但把抢占限制在同队列（preempt），跨队列用 reclaim。

### Q.4 污点、容忍与亲和

| 机制 | 作用 | Volcano 中的位置 |
|---|---|---|
| Taint/Toleration | 节点排斥 Pod | `predicates` 插件 |
| nodeSelector | 简单标签匹配 | `predicates` 插件 |
| NodeAffinity | 表达式匹配 | `predicates` 插件 |
| PodAffinity/AntiAffinity | Pod 间位置 | 部分由 `predicates`/`task-topology` 覆盖 |
| TopologySpreadConstraints | 打散 | 与 binpack/nodeorder 有交互 |

Volcano 不重新实现所有 Kubernetes 亲和语义；它通过 predicates 插件复用一部分，并在 PodGroup 级别补充拓扑。

### Q.5 PDB 与 ResourceQuota

- **PDB**：约束自愿中断时的最少可用副本；Volcano 的 `pdb` 插件在驱逐前检查。
- **ResourceQuota**：namespace 级资源上限；API Server 在创建 Pod 时执行，Volcano 的 `resourcequota` 插件在 enqueue 时提前检查。

两者都不是“调度器配额”，不要和 Queue 的 capability/guarantee 混为一谈。

### Q.6 概念对照表

| Kubernetes | Volcano | 差异 |
|---|---|---|
| Pod | Pod | 相同 |
| Deployment/Job | Volcano Job | Volcano Job 有 tasks/minAvailable/policies |
| 无 | PodGroup | 调度器的作业视图 |
| Namespace | Queue | Queue 是集群级资源池，不是安全边界 |
| PriorityClass | PriorityClass | 复用 |
| ResourceQuota | Queue capability + resourcequota 插件 | 两套账 |
| PDB | pdb 插件 | 需显式启用 |
| kube-scheduler | vc-scheduler | 独立调度循环 |
| 无 | HyperNode | 网络拓扑树 |
| 无 | JobFlow/CronJob | 作业编排 |

### Q.7 为什么默认调度器不够

默认调度器可以很好地回答“这个 Pod 放哪”，但无法回答：

- 一组 Pod 能否一起启动（gang）；
- 两个租户谁该先用（队列公平）；
- 跨队列如何回收（reclaim）；
- 整个 gang 是否在同一网络域（拓扑）；
- 作业失败后整体重启还是部分重启（生命周期）。

这些问题的共同点是：**决策单位大于一个 Pod**。Volcano 的价值就在这个“大于”。

### Q.8 读源码的入口

| 想理解 | kube-scheduler | Volcano |
|---|---|---|
| 调度循环 | `pkg/scheduler/scheduler.go` | `pkg/scheduler/scheduler.go` |
| 框架扩展点 | `pkg/scheduler/framework` | `pkg/scheduler/framework` |
| 过滤/打分 | `pkg/scheduler/framework/plugins` | `pkg/scheduler/plugins` |
| 绑定 | `pkg/scheduler/framework/runtime` | `pkg/scheduler/cache` |
| 配置 | KubeSchedulerConfiguration | `volcano-scheduler.conf` |

两者都采用“快照 + 插件 + 绑定”的结构，但 Volcano 多了 Session/Statement 和作业级事务。

## R. 错误信息与事件索引

日志/事件文本会随版本变化，下表按“消息片段”索引，便于 `grep`。

### R.1 调度与队列

| 消息片段 | 含义 | 优先检查 |
|---|---|---|
| `pod group is not ready` | gang 未满足 | `minAvailable`、节点形状、队列配额 |
| `tasks in gang unschedulable` | gang 中部分任务无节点 | Predicate 原因、拓扑域、污点 |
| `queue ... is not open` | 队列 Closed/Closing | `kubectl get queue` |
| `queue ... capability would be exceeded` | 队列硬上限 | capability 与 allocated |
| `insufficient idle resources` | 节点空闲不足 | 节点 requests/allocatable、碎片 |
| `no feasible node` | 所有节点被 Predicate 排除 | 污点、selector、NUMA、拓扑 |
| `nodegroup ... resource limit exceeded` | 节点组上限 | Queue 注解、节点标签 |
| `namespace ... quota` | ResourceQuota 拒绝 | `kubectl get resourcequota` |
| `no preemptable victims` | 没有合法受害者 | 优先级、gang、PDB、reclaimable |
| `preemption insufficient` | 抢了但 gang 仍不满足 | 受害者数量、拓扑、配额 |
| `not eligible due to preemptionPolicy=Never` | 该 PriorityClass 不主动抢占 | PriorityClass 配置 |
| `shuffle: no better target` | 重调度没有更优节点 | 打分方向、binpack/spread |
| `job ... exceeds maxRetry` | 重试耗尽 | 失败原因、maxRetry、策略 |

### R.2 拓扑与设备

| 消息片段 | 含义 | 优先检查 |
|---|---|---|
| `cannot find a hypernode` | 没有可用域 | HyperNode 覆盖、tier、容量 |
| `topology ... conflict` | 反亲和/拓扑冲突 | PodGroup labels、selector、域 |
| `NUMA ... cannot fit` | 单 NUMA 放不下 | `Numatopology`（numatopo）、NUMA 拓扑、requests |
| `device ... insufficient` | 设备不足 | device plugin、资源名、MIG/DRA |
| `claim ... allocation failed` | DRA claim 失败 | DeviceClass、ResourceSlice、driver |
| `ContainerCreating` 长时间 | 设备 Prepare/卷挂载 | DRA driver、CSI、kubelet 事件 |

### R.3 控制面与 Webhook

| 消息片段 | 含义 | 优先检查 |
|---|---|---|
| `failed calling webhook` | admission 不可达 | Service/Endpoints、副本、网络 |
| `x509: certificate has expired` | 证书过期 | admission-init Job、Secret |
| `no matches for kind` | CRD 未安装/版本不匹配 | `kubectl get crd` |
| `metadata.resourceVersion: Invalid value` | 并发写冲突 | 重试、Patch/status |
| `object has been modified` | 乐观并发 | 重新 Get 后重算 |
| `finalizer ...` 卡住 | 清理未完成 | 控制器日志、外部资源 |

### R.4 常见事件

| 事件 | 含义 | 备注 |
|---|---|---|
| `Inqueue` | 作业进入调度队列 | 不代表有资源 |
| `Scheduled` | Pod 已绑定节点 | 不代表容器已启动 |
| `Allocated` | 作业 gang 已分配 | 可能仍有副本 Pending（弹性） |
| `Evicted` | Pod 被驱逐 | 看 reason：preempt/reclaim/shuffle |
| `Reclaimed` | 跨队列回收 | 检查 guarantee 是否被击穿 |
| `Preempted` | 同队列抢占 | 检查受害者优先级 |
| `RestartJob` / `RestartTask` | 生命周期动作 | 检查 policies/maxRetry |
| `Completed` / `Failed` | 终态 | 结合 Pod 日志 |

### R.5 排障顺序

```text
1. 对象层：schedulerName、queue、PodGroup phase
2. 准入层：webhook、ResourceQuota、PriorityClass
3. 调度层：enqueue → allocate → preempt/reclaim
4. 绑定层：API Server、Binder、网络
5. 运行层：kubelet、镜像、卷、设备
```

先确定“在哪一层被拒绝”，再看该层证据。跨层猜测是排障时间的主要浪费。

## S. 自测题与参考答案

用这张表检查自己是否真的理解，而不是记住术语。建议先遮住答案。

| # | 问题 | 参考答案 |
|---:|---|---|
| 1 | gang 的本质是什么？ | 一组 Pod 的绑定是一个事务：要么达到 `minAvailable` 一起提交，要么整体回滚，不出现部分可见绑定。 |
| 2 | `minAvailable` 省略和设为 0 有什么区别？ | upstream 无区别：webhook 把 0 补成 Σ task 有效 minima；Mini 用 `MinAvailableSet` 区分，是教学扩展。 |
| 3 | proportion 和 capacity 能同时启用吗？ | 不要。两者都定义 deserved，钩子会叠加；upstream 不强制拒绝，但结果难解释。扁平用 proportion，层级用 capacity + `enableHierarchy`。 |
| 4 | capability 和 guarantee 分别保证什么？ | capability 是硬上限，不保证拿得到；guarantee 是保底，回收不能击穿。 |
| 5 | 借用的资源什么时候会被收回？ | 借出队列有 Pending 需求且借入队列超过 deserved 时，reclaim 会回收；回收的底线是借出队列的 guarantee，不能把借出方打到 guarantee 以下。 |
| 6 | 为什么回收可能“一张也收不回”？ | 受害者是整 Pod，驱逐后可能击穿 guarantee；没有更小受害者时只能放弃。 |
| 7 | preempt 和 reclaim 的区别？ | preempt 同队列按优先级；reclaim 跨队列按 deserved/overused。 |
| 8 | PDB 为什么没阻止抢占？ | `pdb` 插件未启用、selector 不匹配、`disruptionsAllowed=0`，或驱逐走了非 PDB 路径。 |
| 9 | shuffle 和 preempt 的区别？ | shuffle 不比较优先级、不绑定，只按插件提名驱逐；重打包由下一轮 allocate 完成。 |
| 10 | hard 拓扑为什么不能只靠 NodeOrder？ | NodeOrder 只是偏好，不能阻止跨域；hard 必须用 Predicate/域选择。 |
| 11 | HyperNode 的 tier 越大越近还是越远？ | 越小越近（tier 1 通常是叶子/ToR）。 |
| 12 | group-topology-affinity 的规则挂在哪？ | PodGroup 的 labels 与 `spec.topologyAffinity`，不是 Pod labels。 |
| 13 | DRA 的队列配额键长什么样？ | `deviceclass/<class>` 或 `<dim>.deviceclass/<class>`，共享 claim 只计一次。 |
| 14 | MIG 和 vGPU 的核心区别？ | MIG 是硬件隔离切片；vGPU 是显存/算力比例共享，隔离较弱。 |
| 15 | Volcano 有 Reservation CRD 吗？ | 没有。预留靠 Queue guarantee + reservation 插件/action + SLA 表达。 |
| 16 | MiniVolcano 的 Statement 和真实 Volcano 有什么不同？ | Mini 用 cache 深拷贝；真实用操作日志。前者回滚简单但 O(cluster)，后者可扩展但每个 operation 要实现 Discard。 |
| 17 | 为什么 MiniVolcano 的测试全绿仍可能有 bug？ | 测试只覆盖已知路径，断言可能编码错误行为；需要独立反例、随机不变量、账本复核。 |
| 18 | 13 条不变量里哪条最难线上监控？ | I5（事务隔离）和 I11（确定性）通常只能测试/回放，不能直接从指标推导。 |
| 19 | Kueue + Volcano 组合的最大风险？ | 两套配额账本不一致：Kueue 准入与 Volcano 调度各算各的，导致死锁或超卖。 |
| 20 | 从默认调度器迁移，第一步做什么？ | 把工作负载的“组”显式化：PodGroup + `scheduling.k8s.io/group-name` + `schedulerName: volcano`，并隔离节点池避免双调度器争抢。 |
| 21 | 为什么 GPU 利用率提升不等于成本下降？ | 如果需求固定且不能缩容，提升利用率只是“同样成本做更多事”；只有能减少卡数/采购时才直接省钱。 |
| 22 | 512 卡、$2/卡时、55% 利用率，年 GPU 成本约多少？ | `512 × 2 × 8760 ≈ $8.97M`；提升到 65% 后每有效卡时约 $3.08（约降 15.4%）。 |
| 23 | 队列长期低于 deserved，先看什么？ | 是否有 Pending 作业、施害队列是否超额、是否 reclaimable、受害者是否被 PDB/gang 保护、是否会被打到 guarantee 以下。 |
| 24 | 作业 Running 但有 Pod Pending，是故障吗？ | 不一定；`minAvailable < 总副本` 时是弹性语义。若 `minAvailable = 总副本`，检查是否绕过 Volcano 或 gang 未生效。 |
| 25 | 为什么调度器不应该负责重启 Pod？ | 放置与生命周期解耦：调度器只决定 nodeName；重启/重试由 Job controller 按 policies 执行，避免调度器重启导致状态混乱。 |
| 26 | LLM 训练的世界大小怎么算？rank 布局的原则是什么？ | `world_size = DP × PP × TP × CP`（按模型/框架可能还有 EP）；通信越密集的维度放得越近：TP 在节点内 NVLink，PP/DP/EP 跨节点。 |
| 27 | 为什么弹性 gang 的 `minAvailable` 要满足整除约束？ | 框架按 `TP×PP×CP` 划分 rank；世界大小不是乘积的倍数时，rank 分配/分片会失败，调度器本身不会报错。 |
| 28 | 70B 模型混合精度 + Adam 的模型状态大约多少显存？ | 约 16 bytes/param（bf16 权重/梯度 + fp32 master + Adam m/v），70B 约 1.12TB，不含激活；靠 ZeRO/FSDP 切分。 |
| 29 | 推理的 KV cache 怎么估？为什么长上下文贵？ | `2 × 层数 × KV 头数 × head_dim × 序列长度 × 并发 × 字节数`；序列长度线性增长，长上下文会迅速吃满显存。 |
| 30 | 为什么训练队列不该开 shuffle？推理 PDB 为什么不能设成副本数？ | 训练驱逐一个 rank 可能触发整个作业重启，代价远高于碎片整理；PDB 等于副本数会禁止任何自愿中断，节点无法 drain、抢占被全阻。 |
| 31 | 高优先级作业为什么有时一张卡也抢不到？ | 同队列可抢占的“余量”不足：gang 作业动一个副本就跌破 minAvailable，弹性作业的 surplus 又不够抢占者的 minAvailable；Statement 必须整体回滚，所以不会出现“抢了一半”。 |
| 32 | reclaim 为什么可能不选“超额最多”的队列？ | 回收看的是“能否驱逐后仍满足 guarantee 与 gang”，不是单纯的超额比例。一个大 Pod 或严格 gang 可能让最超额的队列一张也动不了，只能选余量足够、可中断的受害者。 |
| 33 | 如何从 QPS 估算推理需要多少 GPU？ | `副本 = ceil(QPS × 输出 token / (单副本 tokens/s × 目标利用率))`，再加 N+1 冗余；还要用 KV cache 检查显存，并用 TTFT/TPOT 的 P99 验收。 |
| 34 | 为什么“固定需求省 $1.38M”和“固定集群多做 18.2%”不能相加？ | 它们是两种互斥的会计口径：前者假设可以缩容，后者假设卡数不变。相加会得到“既省钱又多干活”的虚假收益。 |
| 35 | 生产故障演练和 MiniVolcano 实验的区别是什么？ | 实验验证语义（可重复、断言最终状态）；演练验证团队在压力下的取证、止血和恢复能力，包含证书、DRA、etcd、leader 切换等模拟器不覆盖的层。 |

评分建议：

- 30–35 题：可以独立设计和运维 Volcano/LLM 平台；
- 25–29 题：能排障，但需要补队列/拓扑/设备或 LLM 工作负载的细节；
- 20–24 题：建议重读第 2、5、6、9、34 章；
- 20 题以下：先跑第 27 章的实验，再回读概念。

## T. 综合调度轨迹：一次混合负载的 14 tick 复盘

前面各章分别讲 gang、队列、抢占、回收、拓扑和 PDB。真实集群里这些机制**同时**发生，排障困难往往不是因为不懂单个机制，而是因为看不清它们在同一时刻如何互相否决。这一节用一条手工推演、但每一步都可单独复现的轨迹，把它们串起来。

> 这不是一个新的 `-scenario`，而是由 MiniVolcano 现有场景组合出的**概念轨迹**。表中的资源数字是手工推演，用来解释决策顺序；每个单点语义都有对应的场景或测试。不同版本/配置下的受害者排序可能不同，结论应落在“为什么”而不是“第几个 tick”。

### T.1 场景设定

集群 32 张 GPU，4 个节点，每节点 8 卡：

```text
rack-a: node-a1 (8 GPU), node-a2 (8 GPU)
rack-b: node-b1 (8 GPU), node-b2 (8 GPU)
HyperNode: 叶子 tier1 = 每节点；tier2 = rack-a / rack-b；tier3 = root
```

三个队列：

| 队列 | weight | guarantee | capability | reclaimable | 说明 |
|---|---:|---:|---:|---|---|
| `train` | 4 | 12 | 24 | true | 训练，允许借用 |
| `infer` | 2 | 8 | 12 | false | 在线推理，不被回收 |
| `batch` | 1 | 0 | 32 | true | 离线批处理，最低优先级 |

总权重 7，可用容量 32，按权重 deserved 约为 `train 18.3 / infer 9.1 / batch 4.6`。三个数相加是 32；实际实现可能用整数截断，排序结论不变。

六个作业：

| 作业 | 队列 | 形状 | minAvailable | 优先级 | 拓扑/中断 | 时长 |
|---|---|---|---|---:|---|---|
| `T1` | train | 8 Pod × 2 GPU = 16 | 8（严格） | normal | hard tier2（rack-a） | 8 tick |
| `T2` | train | 4 Pod × 1 GPU = 4 | 2（弹性） | normal | 无 | 10 tick |
| `I1` | infer | 4 Pod × 1 GPU = 4 | 3（弹性） | normal | PDB minAvailable=3 | 长期 |
| `I2` | infer | 4 Pod × 1 GPU = 4 | 3（弹性） | normal | PDB minAvailable=3 | 长期 |
| `B1` | batch | 8 Pod × 1 GPU = 8 | 4（弹性） | low | 无 `RestartJob` | 12 tick |
| `H1` | train | 4 Pod × 1 GPU = 4 | 4 | high | 无 | 4 tick（t6 到达） |

两个关键前提：

1. `B1` 没有配置 `PodEvicted -> RestartJob`，所以被回收的副本保持 Pending，之后还能重新绑定；如果配置了，驱逐 4 个副本会让整个作业重启，轨迹会完全不同（见 T.5）。
2. `T1` 的 8 个副本恰好等于 minAvailable，任何单副本驱逐都会打破 gang；这就是它“看起来占着 16 卡，却动不了”的原因。

### T.2 逐 tick 轨迹

| tick | 事件 | train 分配 | infer 分配 | batch 分配 | 空闲 | 作业状态 |
|---:|---|---:|---:|---:|---:|---|
| t0 | `T1` 入队并占 rack-a 的 a1+a2；`T2` 占 b1 的 4 卡 | 20 | 0 | 0 | 12 | T1/T2 Running |
| t1 | `I1` 占 b1 剩余 4 卡 | 20 | 4 | 0 | 8 | I1 Running |
| t2 | `B1` 占 b2 的 8 卡，集群满 | 20 | 4 | 8 | 0 | B1 Running |
| t4 | `I2` 到达；从 `B1` 回收 4 卡 | 20 | 8 | 4 | 0 | B1 4 Running + 4 Pending |
| t6 | `H1` 到达；同队列抢占只凑到 2 卡，回滚 | 20 | 8 | 4 | 0 | H1 Pending |
| t8 | `T1` 完成释放 16；`H1` 拿 4；`B1` 的 4 个 Pending 重新绑定 | 8 | 8 | 8 | 8 | T1 Completed |
| t10 | `T2` 完成释放 4 | 4 | 8 | 8 | 12 | T2 Completed |
| t12 | `H1` 完成释放 4 | 0 | 8 | 8 | 16 | H1 Completed |
| t14 | `B1` 完成释放 8 | 0 | 8 | 0 | 24 | 只剩 I1/I2 |

每个 tick 的账本都满足 `train + infer + batch + 空闲 = 32`。这不是巧合，而是 T.4 要检查的第一条不变量。

### T.3 五个转折点的判断过程

**t0：拓扑先于打分。** `T1` 声明 `networkTopology: hard, highestTierAllowed: 2`，它必须整体落在 rack-a。rack-a 只有 a1/a2 两台、共 16 卡，恰好等于 `T1` 的世界大小；如果 rack-a 只有一台节点，`T1` 一张卡也拿不到，而不是“先放 8 卡再说”。这就是第 11 章说的“先选域、再选节点”。`T2` 没有拓扑约束，落在 rack-b 的 b1。

**t4：回收优先找“可中断的余量”，而不是“最超额的队列”。** 此时 `train` 分配 20，超过 deserved 18.3；`batch` 分配 8，超过 deserved 4.6。两个队列都超额，但：

- `T1` 是 8/8 的严格 gang，动一个副本就打破 minAvailable；
- `T2` 是 4 副本、minAvailable=2，只有 2 卡余量；
- `B1` 是 8 副本、minAvailable=4，有 4 卡余量，且优先级最低。

`I2` 需要 4 卡。`B1` 是唯一能**单独**提供 4 卡余量而不打破自己 gang 的作业；从 `T2` 取 2、再从 `B1` 取 2 也可行，但会同时打断两个作业。回收通常优先选单一、低优先级的受害者，所以本例从 `B1` 取。这不是“batch 一定先被回收”，而是“只有 batch 的可中断余量足够且代价最低”。如果 `B1` 的 minAvailable 也是 8，`I2` 就只能等到 `T1` 完成（见 T.5）。

**t6：高优先级不等于一定能抢占。** `H1` 是 high，`T2` 是 normal，同属 `train`，看起来应该抢占。但 `T2` 只有 2 卡余量；`T1` 是 8/8 的 gang，受保护。preempt 最多凑到 2 卡，`H1` 的 minAvailable 是 4，Statement 必须回滚。结果不是“抢了一半”，而是“一张也没动”。这正是第 9 章强调的事务语义：**要么抢占后 gang 能跑，要么不驱逐**。

**t8：完成事件会同时改变多个队列的可行域。** `T1` 释放 16 卡后，`H1` 拿到 4 卡；`B1` 的 4 个 Pending 副本也重新绑定。为什么不是 `B1` 先拿满 8 卡？因为队列顺序按 `allocated/deserved`：`train` 此时只有 4/18.3，`batch` 是 4/4.6，`train` 更“欠服务”，先服务 `H1`。这就是 proportion 的 share 排序，而不是“谁先提交谁先拿”。

**t14：空闲 24 卡不等于能放下任何作业。** 只剩 `I1/I2` 各 4 卡，空闲 24。一个需要 16 卡、hard tier2 的新训练作业仍然能放进 rack-a（a1+a2 全空）；一个需要 24 卡单域的新作业则放不下，因为没有任何 rack 有 24 卡。**容量判断永远要带形状和域。**

### T.4 四条账本与四条不变量

把这条轨迹当成一次手工对账，至少检查四件事：

```text
1. 节点账本：每个节点 allocated = 该节点上占用 Pod 的 requests 之和
2. 队列账本：每个队列 allocated = 队列内占用 Pod 的 requests 之和
3. 总量守恒：train + infer + batch + 空闲 = 32
4. gang 不变量：任何 Running 作业的 ready ≥ minAvailable
```

在 t4 这个最复杂的时刻：

```text
T1: 16（8 Pod × 2 GPU，全部在 rack-a）
T2: 4（4 Pod × 1 GPU，b1）
I1: 4（b1）
I2: 4（从 B1 回收，落在 b2）
B1: 4（b2 剩余 4 Pod 运行；另外 4 个 Pending，不占资源）
合计 = 16 + 4 + 4 + 4 + 4 = 32
```

`B1` 的 4 个 Pending Pod **不计入** allocated，这是账本最常见的错点：把“作业还存在”当成“资源还被占用”。MiniVolcano 的随机不变量每个 tick 都重算这条（第 26.10 节 I1/I2）。

### T.5 四个变体：同一时刻，不同配置

| 变体 | 改动 | t4 的结果 | 说明 |
|---|---|---|---|
| 严格 gang | `T2 minAvailable=4`、`B1 minAvailable=8` | `I2` 无法回收，等到 t8 `T1` 完成 | 没有“可中断余量”，回收只能等 |
| 整体重启 | `B1` 配 `PodEvicted -> RestartJob` | 回收 4 个 Pod 触发 `B1` 整体重启，8 卡全释放再重来；`I2` 占 4 卡后，`B1` 可能只恢复到 4 Running | 回收成本被放大；训练队列尤其危险 |
| PDB 介入 | 在已有 PDB 上新增 `H2`（3 卡，high，infer） | `H2` 最多拿走 2 卡；要 3 卡就回滚 | PDB 把“可中断量”压到每作业 1 个 |
| 重调度 | t14 后开 `shuffle` + `binpack` | `I1/I2` 被逐步搬到同一节点；PDB 每轮只允许搬 1 个 | 重调度不是免费的，PDB 会限制收敛速度 |

**严格 gang 变体**是很多“资源明明空着却不回收”投诉的根因。回收的安全性要求“驱逐后仍不低于 guarantee”，gang 的安全性要求“不打破 minAvailable”；当两者叠加，调度器可能真的找不到合法受害者。这个变体假设回收遵守 gang 保护；如果实现允许“整组驱逐并重启”，I2 可以拿到资源，但 B1 会全部重来——那是另一种代价，不是免费的回收。解法不是“更激进的回收”，而是给可中断负载留余量（弹性 minAvailable、独立队列、checkpoint）。

**PDB 变体**可以用第 23.3.6 节的 `pdb` 场景复现：4 副本、`minAvailable=3`，第一次驱逐允许，第二次阻断。区别在于集成轨迹里 PDB 同时约束两个作业，所以“总共能拿 2 卡”而不是“能拿 1 卡”。

**重调度变体**对应第 23.3.5 节的 `shuffle` 场景。集成轨迹里 `I1/I2` 都有 PDB，`shuffle` 一次最多从每个作业拿走 1 个副本，重打包需要更多轮；如果没有 PDB，一次可以驱逐更多，但是否能全部搬完仍取决于 `rescheduling` 提名、`pdb` 过滤、`allocate` 打分三者是否一致。PDB 在这里不是“阻止重调度”，而是“把重调度的速度压到 SLO 允许的范围”。

### T.6 用现有场景复现每个单点

集成轨迹本身不提供一条命令，但每个判断都有可执行的对应场景：

```bash
cd mini-volcano

# t0：gang + 拓扑
go run ./cmd/minivolcano -scenario gang -events 12
go run ./cmd/minivolcano -scenario topology -events 12

# t4：跨队列回收与 guarantee 底线
go run ./cmd/minivolcano -scenario reclaim -events 12
go run ./cmd/minivolcano -scenario proportion -events 12

# t6：同队列抢占与回滚
go run ./cmd/minivolcano -scenario priority -events 20
go run ./cmd/minivolcano -scenario pdb -events 20

# t14：重调度与中断保护
go run ./cmd/minivolcano -scenario shuffle -events 20

# 层级队列：把扁平 proportion 换成 capacity
go run ./cmd/minivolcano -scenario hierarchy -events 12
```

读这些场景时，不要只看 `PASS`，用 `-events` 看决策顺序，用 `-explain` 看失败原因。集成轨迹的价值是训练你把“单点场景”拼成“同一时刻的多约束”。

### T.7 复盘记录模板

把一次真实事故或一次演练按下面的模板记录，比只写“调度器有问题”有用得多：

```text
时间窗口：
集群规模/拓扑：
队列配置（weight/guarantee/capability/reclaimable）：
作业形状（replicas/minAvailable/priority/PDB/拓扑）：
关键 tick 的 allocated 表：
被拒绝的层（admission / enqueue / allocate / preempt / reclaim / bind / kubelet）：
候选受害者与最终受害者：
是否满足四类不变量：
根因分类（配置 / 容量 / 碎片 / 保护 / 实现 bug）：
止血动作与恢复时间：
长期修复与回归测试：
```

最后一行“根因分类”决定后续动作：配置问题改队列/优先级，容量问题扩容或限流，碎片问题调 binpack/拓扑，保护问题调 PDB/gang，实现 bug 才改代码。**不要用改代码解决配置问题。**

## U. 容量、排队与 ROI 计算器

第 32 章给了公式，这一节给一个可以真正跑起来的计算器：`scripts/capacity_model.py`，只依赖 Python 标准库。它把“GPU 成本、队列份额、训练时间、KV cache、推理副本、ROI”放进同一个可复算的报告，避免在 PPT 里手算错一位小数点。

> 计算器输出是**工程估算**，不是报价或 benchmark。MFU、tokens/s、KV cache 大小都随模型、框架、网络和批大小变化；上线前用自己的数据替换默认值。

### U.1 运行

自检（不依赖任何输入文件）：

```bash
cd Volcano-tutorial
python scripts/capacity_model.py --selftest
# capacity model selftest: 17 checks passed
```

一个 512 卡、70B 训练 + 20 QPS 推理的例子：

```bash
python scripts/capacity_model.py \
  --gpus 512 --price 2 --hours 8760 \
  --baseline 0.55 --improved 0.65 --annual-cost 500000 \
  --queue train:3:120:400 --queue infer:1:60:160 \
  --train-params 70e9 --train-tokens 1e12 --world-size 512 \
  --peak-tflops 989 --mfu 0.4 --tp 8 --pp 2 --cp 1 \
  --kv-layers 80 --kv-heads 8 --head-dim 128 --seq-len 4096 \
  --concurrency 32 --qps 20 --out-tokens 500 \
  --tokens-per-sec-replica 2000 --target-util 0.7 \
  --redundancy 1 --gpus-per-replica 8
```

`--queue` 的格式是 `名字:权重:guarantee:capability`，资源单位统一为“GPU 当量”。可以重复传多个 `--queue`。

### U.2 输出怎么读

上面的命令会输出五段。关键数字：

| 段 | 指标 | 本例结果 | 用途 |
|---|---|---|---|
| GPU 经济性 | 年 GPU 成本 | `$8.970M` | 512 × $2 × 8760 |
| GPU 经济性 | 每有效 GPU-hour | `$3.64 → $3.08` | 利用率 55% → 65% |
| GPU 经济性 | 固定需求年省 | `$1.380M` | 可缩容口径 |
| GPU 经济性 | 固定集群多做 | `+18.2%` | 增量口径 |
| 队列 | 可用容量（预留 10%） | `460.8` | 512 × 0.9 |
| 队列 | train/infer deserved | `345.6 / 115.2` | 3:1 权重 |
| 队列 | guarantee 总和 | `180 ≤ 460.8` | 可同时兑现 |
| 训练 | 70B × 1T token、512 卡、MFU 40% | `24.00 天` | `6PD`（P=参数量，D=token 数）/ 有效算力 |
| 训练 | TP×PP×CP 整除 | `512 ÷ 16 = 32` | rank 布局可行 |
| KV cache | 单序列 4096 token | `1.342 GB` | 长上下文显存 |
| KV cache | 32 并发 | `42.95 GB` | 单副本预算 |
| 推理 | 基础副本 / 含冗余 | `8 / 9` | 20×500/(2000×0.7) |
| 推理 | GPU 数 / 年成本 | `72 / $1.261M` | 9 × 8 卡 |

两个口径不能相加：`$1.380M` 是“同样的活少买卡”，`+18.2%` 是“同样的卡多干活”。把它们相加会得到“既省钱又多干活”的虚假结论。

### U.3 公式与口径

| 计算 | 公式 | 说明 |
|---|---|---|
| 年 GPU 成本 | `GPU 数 × 单价 × 年小时` | 不含电力/机房/网络/人力 |
| 有效 GPU-hour | `GPU 数 × 年小时 × 利用率` | 利用率是“实际忙时/可用时” |
| 每有效 GPU-hour | `年成本 / 有效 GPU-hour` | 单位成本，不是单价 |
| 固定需求节省 | `年成本 × (1 - 基线/改进)` | 需求不变、可缩容 |
| 固定集群增量 | `改进/基线 - 1` | 集群不变、需求增长 |
| 打平利用率 | `基线 / (1 - 程序年成本/年成本)` | 程序成本相对 GPU 成本 |
| deserved | `可用容量 × 权重 / Σ权重` | 一次性加权切分 |
| 训练 FLOPs | `6 × 参数量 × token 数` | dense 估算；MoE 需另算 |
| 训练时间 | `FLOPs / (GPU 数 × 峰值 × MFU)` | MFU 用实测 |
| KV cache | `2 × 层数 × KV 头数 × head_dim × 序列 × 并发 × 字节` | 2 表示 K 和 V |
| 推理副本 | `QPS × 输出 token / (单副本 tokens/s × 目标利用率)` | 向上取整，再加冗余 |

口径提醒：

- **分配率 ≠ 实际利用率**。队列 allocated 90% 不代表 GPU 忙 90%，作业可能占着卡等数据（第 32.2.1 节）。
- **MIG/vGPU 的逻辑卡不能直接和物理卡相加**。计算器里的 `--gpus` 是你要统一的那个口径，先定义清楚。
- **deserved 是一次性切分**。层级 capacity、借用、回收会改变稳态；计算器只给起点，不模拟时间演化。
- **训练时间是下界**。故障、checkpoint、数据加载、通信气泡都会让它变长；`MFU` 已经把一部分算进去，但不是全部。

### U.4 三个常见追问

**“为什么 train 的 deserved 是 345.6，而 capability 设 400？”** 因为 3:1 权重把 460.8 分成 345.6/115.2；capability 只要不低于 deserved 就不会截断，设 400 是给借用留空间。如果 capability 设成 300，计算器会标 `deserved>capability`，提示配置自相矛盾。

**“为什么推理要 9 个副本而不是 8 个？”** 8 个是满足 70% 利用率下的理论值；第 9 个是 N+1 冗余。如果 SLO 允许单副本故障时降级，可以把 `--redundancy 0`，但要在 PDB 和容量里体现这个选择。

**“为什么 KV cache 42.95 GB 还能放进 8×80GB？”** 70B 权重 bf16 约 140GB，8 卡每卡 17.5GB；KV 42.95GB 是整个副本的 32 条并发，分摊到 8 卡约 5.4GB/卡（如果按 TP 切分），剩下给激活和碎片。真实引擎的切分方式不同，必须用 `nvidia-smi`/框架指标验证。

### U.5 五个使用陷阱

1. **把 `--price` 当成合同价**。它只是一个输入；实际价格按型号/地区/合同变化。
2. **用峰值 TFLOPS 代替实测 MFU**。峰值是理论值，MFU 40% 和 60% 会让训练时间差 1.5 倍。
3. **忽略网络**。计算器只算 FLOPs 和显存，不算跨 spine 带宽、拥塞和 NCCL 退化。
4. **把队列 capability 当 guarantee**。capability 是上限，不保证拿到；guarantee 才是保底。
5. **只算 GPU 成本**。电力、机房、网络、存储、人力和闲置都会改变 ROI；第 32 章的模型明确排除了这些。

### U.6 扩展计算器

脚本的结构是“纯函数 + 报告 + 自检”。要加一个新指标：

1. 在核心公式区加一个纯函数（例如 `goodput`、`P/D` 分离的 prefill 时间）；
2. 在 `build_report` 里加一段输出；
3. 在 `selftest` 里加已知值的断言；
4. 跑 `python scripts/capacity_model.py --selftest` 和 `bash scripts/verify_all.sh`。

不要在报告函数里直接写公式；否则自检无法覆盖。这个结构和 MiniVolcano 的“纯函数 + 场景 + 不变量”是同一个思路。

## V. 生产故障演练剧本库

第 27 章是“语义实验”，这一节是“生产演练”。区别在于：实验验证“调度器应该怎样”，演练验证“出事后团队能不能在压力下按证据链行动”。

> 所有剧本都假设在**测试集群**或**隔离的 staging 队列**执行。不要在生产共享集群上直接制造抢占风暴、证书过期或 DRA 泄漏。每个剧本都给出“止血”和“根治”两段，先止血再定位。

### V.1 剧本一：guarantee 总和超过容量

| 项 | 内容 |
|---|---|
| 触发 | 把 `dev` 队列的 guarantee 从 0 调到 120，集群 256 卡 |
| 症状 | 多个训练队列 Inqueue 但不分配；Pending 从 12 涨到 87；节点仍有空闲 |
| 取证 | `kubectl get queue -o custom-columns=...` 对比 guarantee/allocated/capability；`kubectl get podgroup -A` 看 Inqueue；scheduler 日志 `grep -i 'guarantee\|overused\|allocatable'` |
| 根因 | `Σ guarantee > 可用容量`，调度器无法同时兑现所有保底；它不会报错，只会安静地不调度 |
| 止血 | 回滚最近一次 guarantee 变更；暂停配额变更与 reclaim，避免进一步驱逐 |
| 根治 | 准入校验 `Σ guarantee ≤ 容量 × 0.9`；配额变更走 PR + 容量计算 |
| 预防 | 告警 `sum(guarantee)/capacity > 0.9`；每月对账；第 16.8.6 节有完整时间线 |
| 复盘题 | 为什么“节点有空闲”和“作业不调度”可以同时成立？ |

### V.2 剧本二：Webhook 证书过期

| 项 | 内容 |
|---|---|
| 触发 | 证书过期，或 `volcano-admission-init` 未完成就升级 |
| 症状 | 所有 vcjob 提交失败；错误含 `failed calling webhook` 或 `x509: certificate has expired`；严重时整个集群的 Pod 创建都被 `failurePolicy: Fail` 阻断 |
| 取证 | `kubectl -n volcano-system get job,pod \| grep admission`；`logs job/volcano-admission-init`；`openssl x509 -noout -dates` 看 Secret |
| 根因 | admission 证书没有轮换；`rollout restart` 不会重新生成证书 |
| 止血 | 删除 init Job 与 Secret，重新 apply/upgrade；或临时把 `failurePolicy` 改为 `Ignore`（只作为最后手段，记录风险） |
| 根治 | 证书过期告警；升级流程包含证书检查；保留一键重建脚本 |
| 预防 | 第 33.8.2 节的排查顺序；每季度演练一次 |
| 复盘题 | 为什么 `kubectl rollout restart` 修不好证书问题？ |

### V.3 剧本三：抢占/回收抖动

| 项 | 内容 |
|---|---|
| 触发 | 两个队列权重/优先级频繁调整，或 guarantee/deserved 配置让双方都长期“欠服务” |
| 症状 | `Evicted`/`Reclaimed` 事件周期性出现；作业反复重启；利用率没有上升 |
| 取证 | 统计事件时间分布；对比驱逐前后节点布局；检查 `reclaimable`、guarantee、PDB、gang；看 shuffle 是否与 binpack 方向冲突 |
| 根因 | 配额震荡、受害者选择不稳定、重调度与打分方向相反，或 `PodEvicted -> RestartJob` 放大影响 |
| 止血 | 临时从 `actions` 移除 `preempt`/`reclaim`/`shuffle`；冻结配额变更 |
| 根治 | 固定权重与 guarantee；让 allocate 打分方向与重调度策略一致；用 gangPreempt 做放置模拟 |
| 预防 | 告警驱逐速率；变更窗口；第 9.7 节的六个缓解手段 |
| 复盘题 | 为什么“公平”配置会导致抖动？ |

### V.4 剧本四：hard 拓扑死锁

| 项 | 内容 |
|---|---|
| 触发 | 64 卡作业设 `highestTierAllowed=2`，但每个 rack 只有 4 台 8 卡节点 |
| 症状 | 作业永远 Pending；节点/域看起来有空闲；`cannot find a hypernode` 或 `topology conflict` |
| 取证 | `kubectl get hypernode`；按 tier 统计每个域的空闲；`kubectl describe podgroup` 看 minResources；scheduler 日志 `grep -i topology` |
| 根因 | 拓扑约束比作业世界大小更紧；总空闲够，但没有单个域够 |
| 止血 | 把 `highestTierAllowed` 放宽到 3，或把作业拆小；不要直接关拓扑插件 |
| 根治 | 上线前用“最大可调度 gang”而不是“总空闲 GPU”做容量检查；拓扑与容量一起规划 |
| 预防 | 第 11.10.5 节的设计建议；soft 模式先灰度 |
| 复盘题 | 为什么“集群有 64 张空闲卡”仍可能放不下 64 卡作业？ |

### V.5 剧本五：DRA claim 泄漏

| 项 | 内容 |
|---|---|
| 触发 | 高频驱逐/抢占，DRA driver 的 Prepare/Unprepare 失败或延迟 |
| 症状 | Pod 删除后设备仍被占用；新 Pod 卡在 `ContainerCreating`；队列配额未超但 claim 分配失败 |
| 取证 | `kubectl get resourceclaim -A`；`kubectl describe resourceclaim`；`kubectl get resourceslice`；driver 日志；scheduler 日志 `grep -i dra` |
| 根因 | claim 未释放、仍被其他 Pod 引用，或 driver Prepare 失败 |
| 止血 | 找到引用该 claim 的 Pod；确认没有共享引用后删除 claim；重启 driver（谨慎） |
| 根治 | 驱逐前检查 claim 生命周期；配额统计覆盖共享 claim；升级到修复版本 |
| 预防 | 第 13.6.3 节的生命周期检查；高频抢占场景压测 |
| 复盘题 | 为什么“Pod 已删除”不等于“设备已释放”？ |

### V.6 剧本六：PDB 阻止节点 drain

| 项 | 内容 |
|---|---|
| 触发 | 推理副本 PDB `minAvailable = replicas`，或 selector 写错 |
| 症状 | 节点无法 drain；抢占找不到受害者；`disruptionsAllowed=0` |
| 取证 | `kubectl get pdb -A`；`kubectl describe pdb`；确认 selector 匹配 Pod；检查 `pdb` 插件是否启用 |
| 根因 | PDB 设成“绝不中断”；或 selector 不匹配导致“以为有保护，实际没有” |
| 止血 | 临时调整 PDB（留 1 个冗余）；或先把副本扩到 `minAvailable+1` 再 drain |
| 根治 | PDB 规则：`minAvailable` 不要等于副本数；selector 纳入模板/审计 |
| 预防 | 第 9.6.3、34.8.3 节；上线前用 `kubectl describe pdb` 验证 |
| 复盘题 | PDB 保护的是“自愿中断”还是“所有中断”？ |

### V.7 剧本七：调度器 leader 抖动 / 陈旧 cache

| 项 | 内容 |
|---|---|
| 触发 | 控制面节点资源不足、etcd 抖动、网络分区，或 leader election 配置不当 |
| 症状 | 调度延迟升高；Pod 已创建但长时间不绑定；leader 频繁切换；不同副本看到的 cache 不一致 |
| 取证 | `kubectl -n volcano-system get pods`；scheduler 日志中的 leader 切换；`volcano_schedule_attempts_total` 速率；API Server 延迟；etcd 健康 |
| 根因 | 控制面资源争抢、etcd 慢、网络抖动，或副本数/租约配置不合理 |
| 止血 | 给控制面独立节点池；重启异常副本；必要时缩容业务提交 |
| 根治 | 控制面 HA + PDB + 反亲和；etcd 容量规划；监控 leader 切换 |
| 预防 | 第 19.6 节；故障演练每季度一次 |
| 复盘题 | 多副本 + leader election 解决的是可用性还是吞吐？ |

### V.8 演练记录模板

```text
剧本编号/名称：
执行环境（集群/命名空间/队列）：
开始/结束时间：
触发动作与回滚动作：
观察到的症状（按层：API Server / admission / enqueue / allocate / bind / kubelet）：
关键证据（命令 + 输出摘要）：
止血是否成功、耗时：
根因（配置 / 容量 / 保护 / 实现 / 基础设施）：
长期修复与负责人：
回归测试/告警是否补齐：
下次演练要改什么：
```

演练的目标不是“一次成功”，而是让团队在真实压力下形成**按层取证**的习惯。每次演练至少留下一条可以自动化的检查或告警，否则只是一次表演。

## W. MiniVolcano 任务级 gang 设计笔记

这一节把第九轮新增的 task 级 gang 与 `minSuccess` 当成一次完整的工程设计复盘：需求如何变成数据模型、事务如何保持可见性、状态机如何收口、验证如何覆盖边界。它不是 API 文档，而是“如果让你在真实 Volcano 里改这块，应该先想清楚什么”。

### W.1 需求与失败模式

用户要的不是“多一个字段”，而是三种失败模式：

| 失败模式 | 只检查总数的后果 | 角色门槛要回答的问题 |
|---|---|---|
| 参数服务器没起来 | 8 个 worker 全 Running，作业仍然不能训练 | `ps` 角色是否达到 1？ |
| 某个角色被挤掉 | 总数仍 ≥ minAvailable，但关键角色为 0 | 每个角色是否达到自己的下限？ |
| 提前完成 | 所有副本都必须结束，浪费队列配额 | 是否已有 N 个副本成功，可以终止其余？ |

因此设计目标有三个：

1. **总数与角色同时满足**才允许 gang 提交；
2. 角色门槛必须有明确的**生效条件**，不能把作业静默锁死；
3. 提前完成必须**释放配额**，不能留下 Pending 副本。

### W.2 数据模型：从 Job 到 PodGroup

```text
Job.spec.tasks[i].minAvailable  (nil = 无角色门槛；显式值 = 角色下限)
        │
        ├─ TaskSpec.EffectiveMinAvailable()
        │
        ├─ Job.MinTaskMember() ──────────────► PodGroup.spec.minTaskMember
        │
        └─ Job.SumTaskMinAvailable()
                    │
                    └─ Job.RoleGatesActive() = minAvailable > 0 && minAvailable >= sum
```

`MinResources` 也走这条链：角色门槛生效时，先按每个角色的 `EffectiveMinAvailable` 预留资源，再按声明顺序补足剩余 `minAvailable`。否则一个声明在前、请求更大的 worker 角色会吃掉全部准入预算，把本可放下的作业挡在 enqueue 之外。

三个函数的分工：

- `EffectiveMinAvailable`：把 nil/0 归一为“无门槛”，把超过 replicas 的值截断（admission 仍会拒绝非法值）；
- `MinTaskMember`：生成 PodGroup 的角色表，供调度器和排障读取；
- `RoleGatesActive`：决定角色表是否参与判定。它把“配置错误”与“合法但门槛不生效”区分开。

Mini 与上游的一个刻意差异：上游 `tasks[].minAvailable` 省略时默认 replicas，并写入 `minTaskMember`；Mini 省略时视为“无角色门槛”，由 job 级 `minAvailable` 覆盖常见场景。这样 `minAvailable=4, replicas=8` 的弹性作业不会因为默认角色门槛而永远凑不齐。真实 API 的默认值见 4.2.4，迁移时不要直接照搬 Mini 的 nil 语义。

### W.3 事务可见性：为什么不能用 `ssn.JobReady`

`gang.JobReady` 读的是 session cache，而 `allocate` 的试绑定写在 Statement 的 trial cache。两者不是同一个视图：

```text
session cache    本轮开始时的只读快照
   │ Clone
   ▼
statement.trial  本作业的 tentative 绑定
   │ Commit
   ▼
session cache    下一个作业可见
```

如果 `allocateJob` 在绑定 5 个 Pod 后调用 `ssn.JobReady(job)`，它看到的仍是绑定前的 0 个 Ready，会把一个本可提交的 gang 回滚。正确做法是：

```go
ready := st.ReadyCount(job.UID)
byTask := st.ReadyCountByTask(job.UID)
if ready >= job.Spec.MinAvailable && taskGatesSatisfied(byTask, job) {
    st.Commit()
} else {
    st.Discard()
}
```

`Statement.ReadyCountByTask` 只是 `trial.ReadyCountByTask` 的转发；没有它，角色门槛在事务内不可判定。这个细节在真实 Volcano 里对应 `Statement` 操作日志上的 `JobInfo` 副本：插件必须读事务视图，而不是全局 cache。

### W.4 判定顺序与短路

`taskGatesSatisfied` 的顺序是刻意的：

```text
1. if !job.RoleGatesActive(): return true
2. if total ready < minAvailable: return false
3. for each task with want > 0:
       if ready(task) < want: return false
4. return true
```

先判 active，再判总数，最后逐角色。这样：

- 门槛不生效时，不会因为某个角色为 0 而误拒；
- 总数不足时，不必遍历角色；
- 错误信息只在真正失败时生成，避免为每个 Pod 构造字符串。

还有一个不在 `taskGatesSatisfied` 里、但决定它能否被满足的维度：分配轮次与 `TaskOrderFn`。Mini 的 allocate 分两轮：第一轮只满足每个 active 角色的门槛，第二轮才按 TaskOrder 填额外副本。TaskOrder 的顺序是“有角色门槛 > 无门槛；门槛小 > 门槛大；声明顺序；副本序号”。只做单轮排序会活锁：门槛小的角色先占满所有槽位，另一个角色永远拿不到门槛，即使 `a1+b2` 的可行解存在。`task-gang` 场景刻意把 worker 声明在 master 前面，`TestAllocateSatisfiesAllRoleGatesBeforeExtraReplicas` 则覆盖“每个角色都有门槛、容量刚好”的形态。

`gangReason` 复用同一顺序，并在失败时指出第一个缺口的角色：

```text
gang role gate not ready: task=master ready=0/1 totalReady=4/5; rolled back 4 tentative bindings
```

排障时这句话比“gang not ready”有用得多：它直接告诉你缺的是角色，不是总量。

### W.5 状态机：Running 与 Completed 的两个收口

控制器有两条路径需要角色门槛：

1. **Running 判定**：`ready >= min && running > 0 && taskGatesReady(job)`。只看 `ready` 会把“5 个 worker 已 Running、master 仍 Pending”误标成 Running。
2. **Completed 判定**：`minSuccess` 达到即完成；`completeJob` 对占用节点的 Pod 调 `Finish(true)`，对仍 Pending 的 Pod 直接标 `Succeeded` 并写 `FinishTick`。

第二条容易被忽略：`Finish` 只接受占用节点的 Pod，直接对 Pending 调用会返回错误；如果忽略错误，作业 Completed 后就会留下 Pending 副本，队列配额永远不释放。Mini 的选择是“标记为成功并终止”，真实 Volcano 则是删除/终止 Pod；两者的账本结果一致，Pod 历史不同。

### W.6 不变量 I13 与测试矩阵

I13：**角色门槛生效时，Running 作业的每个角色 ready ≥ minTaskMember。**

```text
前提：job.RoleGatesActive()
断言：∀ task with want>0: ReadyCountByTask(task) >= want
      且 ReadyCount >= minAvailable
```

测试矩阵：

| 层级 | 用例 | 断言 |
|---|---|---|
| 单元 | `TestGangTaskMinMemberReadiness` | 5 worker 无 master 不 Ready；补 master 后 Ready |
| 单元 | `TestMutateJobDefaultsToTaskMinima` | 省略 job min 时取 Σ task min |
| 单元 | `TestRoleGatesInactiveWhenJobMinBelowTaskSum` | 低于之和时合法且门槛不生效 |
| 单元 | `TestValidateMinSuccess` | minSuccess 超过 replicas 被拒 |
| 单元 | `TestMinResourcesReservesRoleGatesFirst` | 角色门槛先于额外副本计入 minResources |
| 单元 | `TestGangDisableJobStarvingDoesNotRegister` | `enableJobStarving=false` 时无 starving hook |
| 单元 | `TestAllocateSatisfiesAllRoleGatesBeforeExtraReplicas` | 两轮分配避免“先填满一个角色”活锁 |
| 单元 | `TestAllocateRollsBackWhenRoleUnavailable` | 总数够但角色不可调度时整个 Statement 回滚 |
| 单元 | `TestBackfillDoesNotPartiallyCommitGang` | backfill 不提交角色残缺的 gang |
| 单元 | `TestPreemptRequiresRoleGates` | 角色门槛未满足时 preempt 不驱逐 |
| 单元 | `TestRoleGatesBlockRunning` | 总数够但角色不全时控制器不标 Running |
| 集成 | `task-gang` | 1 master + 4/8 worker，剩余 4 Pending |
| 集成 | `min-success` | 2/4 成功即 Completed，4 个 Pod 全 Succeeded，CPU 归零 |

缺少任何一层都会留下盲区：只测单元会漏掉事务可见性；只测场景会漏掉生效条件的边界。

### W.7 八个实现期语义缺陷

| # | 缺陷 | 为什么会发生 | 修复 |
|---:|---|---|---|
| 1 | 把 job min < Σ task min 当成非法 | 把“无法满足所有角色”误当成“schema 非法” | `RoleGatesActive()`，合法但跳过角色检查 |
| 2 | `minSuccess` 完成后留下 Pending | `Finish` 拒绝非占用 Pod，错误被忽略 | `completeJob` 标记未启动副本为 Succeeded |
| 3 | `allocate` 用 session cache 判角色 | 没意识到 Statement 的 trial 视图 | `Statement.ReadyCountByTask` + trial 判定 |
| 4 | `enableJobStarving=false` 仍注册 hook | 把“关闭饥饿判定”实现成“换阈值” | 按 option 注册；无 hook 时 fallback 返回 false |
| 5 | preempt/reclaim 只检查总数 | 提交条件没有复用角色门槛 | 统一 `gangSatisfied`；`TestPreemptRequiresRoleGates` |
| 6 | backfill 逐 Pod 提交 | 把 BestEffort 当成无 gang 语义 | 每作业一个 Statement；`TestBackfillDoesNotPartiallyCommitGang` |
| 7 | TaskOrder 单轮贪心活锁 | 门槛小的角色占满槽位 | allocate 两轮：先门槛、后额外副本；`TestAllocateSatisfiesAllRoleGatesBeforeExtraReplicas` |
| 8 | shuffle/rescheduling 只保护总数 | 受害者选择没看角色占用 | `OccupyingCountByTask` + 角色门槛；无专门合并测试 |

八条都不是编译错误，而是“测试可能全绿”的语义错误。第 25.7.14 节记录了它们的复盘。

另外两个不是缺陷、但决定边界是否成立的设计选择：

1. **TaskOrder 的角色优先**：有门槛 > 无门槛，门槛小 > 门槛大，再按声明顺序。没有它，容量恰好等于 `minAvailable` 时 worker 可能占满槽位，master 永远排不上。
2. **MinResources 的角色优先**：先按角色门槛预留，再按声明顺序补足。没有它，声明在前、请求更大的角色会让准入预算虚高，作业在 enqueue 就被误拒。

### W.8 与上游的差异表

| 语义 | 上游 v1.16 | MiniVolcano | 迁移注意 |
|---|---|---|---|
| task `minAvailable` 省略 | 默认 replicas，写入 minTaskMember | 无角色门槛 | 不要照搬 nil 语义 |
| 显式 job `minAvailable: 0` | webhook 补成 Σ task min | 关闭 gang（教学扩展） | 生产不能用 0 关 gang |
| job min < Σ task min | 跳过角色检查 | 同左（`RoleGatesActive=false`） | 门槛静默失效，必须监控 |
| `dependsOn` | 控制 Pod 创建时机 | 一次性物化全部 Pod | 严格 gang + dependsOn 会死锁 |
| `minSuccess` 全部终止后不足 | Failed | 未实现第二阶段 | 不要把 Mini 当完整判定 |
| `subGroupPolicy` / `partitionPolicy` | 支持子组/分片 gang | 未实现 | 大模型分片作业需真实 Volcano |

### W.9 如果继续扩展：dependsOn 与 subGroup

**dependsOn** 的最小实现需要改三处：

1. `MaterializeJob` 只创建依赖满足的 task；依赖判定用上游 task 的 `Succeeded/Running+Ready` 计数；
2. `Job.TotalReplicas`/`MinResources` 不能把未创建的下游算进去，否则 enqueue 永远不满足；
3. `gang.JobValid` 必须允许“当前阶段”的 Pod 集合，否则 `ValidTaskNum` 看不到下游会死锁。

这正是 4.2.4 说的“dependsOn 与严格 gang 叠加会死锁”。如果要做，建议先实现 JobFlow 路径，而不是在单个 Job 内做阶段 DAG。

**subGroupPolicy** 的最小实现需要：

- `PodGroup.SubGroupPolicy` 与 `minSubGroups`；
- 按 label/matchLabelKeys 分组；
- 子组级拓扑约束与 gang 门控；
- 与 `minTaskMember` 的优先级/互斥规则。

它比 `minTaskMember` 多一层“组”的维度，不能简单地把 task 名换成 group 名；第 11.6 节给出了字段与选型。

### W.10 设计检查清单

如果你要在自己的调度器里实现角色门槛：

- [ ] 角色下限与作业下限的关系是否定义清楚？低于之和时是拒绝还是跳过？
- [ ] 未设置的 task 下限是“无门槛”还是“等于 replicas”？两种都要有文档。
- [ ] 判定读的是事务视图还是全局快照？试绑定是否可见？
- [ ] Running/Completed 判定是否都用了同一套门槛？
- [ ] 提前完成/失败终止是否释放了未启动副本的配额？
- [ ] 错误信息是否指出具体角色与缺口数量？
- [ ] 是否有单测覆盖“总数够但角色不够”的反例？
- [ ] 是否有场景覆盖“配置改小后门槛静默失效”的边界？

这八个问题答不上来，角色门槛大概率会在生产里以“资源够但作业不跑”的形式出现。
