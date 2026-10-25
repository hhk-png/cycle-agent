# 第 25 章 练习、扩展路线与真实 KubeRay 的差距

本教程的目标不是让你“背完 KubeRay”，而是让你能改、能测、能判断。本章给出练习、扩展路线和与真实实现的差距。

## 25.1 基础练习（读完 1–8 章，以及 19–20 章的模型/Store）

### 练习 1：给 RayCluster 加一个 condition

- 新增 `WorkerPodsReady` condition；
- 规则：`readyWorkerReplicas == desiredWorkerReplicas` 时为 True；
- 在 status 中维护 `lastTransitionTime`；
- 补测试：扩缩容/失败/suspend。

验收：

- 连续 `run_once` 不产生无意义 status 写入；
- suspend 时 condition 合理。

### 练习 2：扩展 custom head Service 的端口合并

`builders.py` 的 `build_head_service` 已实现基础端口合并，并有测试。在此基础上扩展：

- 端口名冲突时支持显式覆盖/拒绝两种策略；
- 校验 `targetPort` 必须指向已有容器端口；
- 支持 `appProtocol`/`nodePort` 的边界检查；
- 测试重复/冲突/缺失端口。

### 练习 3：给 scheduler 加 topology spread

- 实现 `topologySpreadConstraints` 的最简版本；
- 按 zone 打散 worker；
- 测试 3 节点/3 可用区（模拟器 node-a/b/c；如果要测 2 区，先改 `SimulatedScheduler.nodes`）。

## 25.2 控制器练习（读完 9–12 章，以及 21–22 章的控制器实现）

### 练习 4：autoscaler Conservative 的精确语义

当前实现限制“一次最多增加当前规模”。真实 Ray 的 Conservative 语义更细：

- 待扩容 Pod 数不超过当前集群规模；
- 结合 pending demand/节点容量；
- 测试 0 -> 1、1 -> 2、10 -> 11。

### 练习 5：RayJob 的 submitter 重试

`submitterConfig.backoffLimit` 已经实现（默认 2），并已有“K8sJobMode submitter 失败 -> SubmissionFailed”的测试。在此基础上扩展：

- 注入 submitter 首次失败、第二次成功，验证 Kubernetes Job 的 backoff 语义；
- 区分“submitter 重试”与“RayJob `backoffLimit` 重建集群”；
- 验证重试不会改变 `submission_id`/jobId。

### 练习 6：RayService 的 canary

- 支持按 header/weight 把流量切到 pending；
- 用 HTTPRoute 的权重模拟；
- 测试 5%/50%/100%。

### 练习 7：RayCronJob 的并发策略

上游 v1.7 没有 `concurrencyPolicy`。你可以：

- 在 MiniKubeRay 扩展 `Forbid`/`Replace`；
- 明确标注为扩展字段；
- 测试重叠调度。

## 25.3 平台练习（读完 13–18 章）

### 练习 8：NetworkPolicy 生成器

- 根据 `spec.networkPolicy` 生成 head/worker 规则；
- 自动允许 DNS egress；
- 测试 DenyAll/DenyAllIngress/DenyAllEgress。

### 练习 9：GCS FT 恢复模拟

- 在 Runtime 中模拟 Redis 持久化；
- head 删除后从 Redis 恢复节点/作业；
- 测试“有 FT/无 FT”的差异。

### 练习 10：扩展 History Server

事件记录、快照和回放已经实现。把练习改成扩展：

- 增加保留策略（按时间/事件数淘汰）；
- 增加按 reason/kind 的索引和查询；
- 把 JSON 持久化换成对象存储/SQLite，并测试并发读写；
- 删除集群后从快照 + 事件重建状态，并与原状态做 diff。

### 25.3.1 用现有实验做扩展练习

`examples/rayjob_modes.py`、`examples/cluster_lifecycle.py`、`examples/cronjob_lab.py` 本身就是可改的“题面”，比从零写测试更容易验证。它们都由 `tests/test_examples.py` 在进程内运行。

### 练习 11：给 `rayjob_modes.py` 加故障注入

- 给 K8sJobMode 的 submitter Pod 注入失败；
- 观察 `SubmissionFailed` 与 `backoffLimit` 的交互；
- 对比其他三种模式在同一故障下的行为；
- 断言“失败不会误判为 SUCCEEDED”。

### 练习 12：给 `cluster_lifecycle.py` 加 `upgradeStrategy: None`

- 改镜像后保持 `upgradeStrategy: None`，确认 Pod 不被重建；
- 再切到 `Recreate`，确认全量替换；
- 解释为什么 `None` 不是“暂停升级”，而是“交给人工”。

### 练习 13：给 `cronjob_lab.py` 加 `concurrencyPolicy`

- 在 MiniKubeRay 中新增扩展字段 `concurrencyPolicy: Forbid`；
- 同一计划时刻已有子作业运行时，不创建新子作业；
- 明确注释“这是教学扩展，不是 KubeRay v1.7 字段”；
- 测试 `Allow`/`Forbid`/`Replace` 三种语义。

### 练习 14：给附录 J 补一张时序图

- 选一个正文没有图、但步骤明确的流程（例如 RayJob TTL、GCS FT cleanup、History Server 采集）；
- 按“参与者 → 消息 → 证据”画 Mermaid 时序图；
- 用第 42 章的实验室或现有测试验证每一步都真实发生；
- 在图中标出最容易卡住的一步。

### 练习 15：把一次故障复盘变成新场景

- 从第 37 章选一个案例；
- 在 `examples/incident_lab.py` 加场景；
- 断言状态/事件/子资源，而不是只打印日志；
- 在 `tests/test_examples.py` 或专门测试中固定下来。

## 25.4 与真实 Kubernetes 集成

### 25.4.1 适配真实 client

把 `LocalClient` 替换成：

- `kubernetes` Python client；
- 或 `controller-runtime` 的 Go 实现。

步骤：

1. 实现 `KubeClient` 协议；
2. 把 `ApiStore` 的 watch 换成 `watch.Watch`；
3. 用 CRD 的 status subresource；
4. 用真实 scheduler/kubelet；
5. 用 envtest 跑控制器测试。

### 25.4.2 适配真实 Ray

把 `RayRuntime` 换成：

- Ray Dashboard HTTP API；
- `ray job submit`；
- Serve REST API；
- Prometheus 指标。

需要处理：

- 网络/认证；
- 超时/重试；
- job ID 幂等；
- Serve 状态轮询。

## 25.5 与真实 KubeRay 的差距

| 领域 | MiniKubeRay | 真实 KubeRay |
|---|---|---|
| CRD schema | 教学子集 | 完整 OpenAPI/默认/校验 |
| 升级 | Recreate/None 简化 | 更细的 Pod 模板 hash/scale expectations |
| 调度 | 3 节点模拟 | kube-scheduler + 批调度器 |
| Kubelet | 简化状态机 | 真实容器生命周期/探针 |
| Ray | 模拟 | 真实 GCS/Raylet/object store |
| autoscaler | 简化 policy | Ray autoscaler v1/v2 |
| GCS FT | 字段/PVC/env + cleanup Job/finalizer | Redis/RocksDB 数据面 |
| NetworkPolicy | 对象 | CNI 执行 |
| mTLS | 字段/校验 | cert-manager/证书轮换 |
| Gateway API | Gateway/HTTPRoute 对象 + 权重 | 真实数据面/流量迁移 |
| 多主机 | replica/host 索引与整组替换（确定性名） | 随机 replica 名/完整 DNS |
| 批调度 | PodGroup/schedulerName/queue | 真实 gang scheduling |
| Kueue | 仅跳过 MultiKueue 的 `managedBy` | 本地 Workload/配额/抢占 + MultiKueue |
| APIServer | 子集 | 完整 API v1/v2 |
| Dashboard | 单页 | React/完整 API |
| History Server | 事件/快照/回放（JSON） | collector sidecar/对象存储/完整 UI |
| 多集群 | 未实现 | Kueue MultiKueue |
| 性能 | 不模拟 | 真实吞吐/扩展 |

## 25.6 推荐的下一步

移植的完整工程路线（CRD 类型、client/cache、reconciler、envtest、Helm、灰度）见[第 38 章](kuberay教程-38-从MiniKubeRay到生产Operator-移植与二次开发.md)。下面是最小顺序：

1. 先补“真实 Kubernetes client”适配；
2. 用 envtest 跑 RayCluster 控制器；
3. 接真实 Ray 的 Job API；
4. 补 Kueue 的 Workload 准入/配额（目前只有 `managedBy` 跳过）；
5. 补 Gateway API 数据面（目前只有对象/权重）；
6. 补 mTLS/cert-manager 与 NetworkPolicy 数据面；
7. 补多主机随机 replica 名/完整 DNS；
8. 最后再考虑多集群/成本分摊/完整 History Server。

## 25.7 代码阅读清单

完整的九步阅读顺序见 [19.7](kuberay教程-19-MiniKubeRay架构与设计取舍.md)，这里不重复。如果时间有限，先读三个文件：

- `minikuberay/controllers/raycluster.py`：核心调和；
- `minikuberay/controllers/rayjob.py`：状态机；
- `minikuberay/store/store.py`：API 语义。

然后再按练习需要跳到 `rayservice.py`、`batch.py`、`autoscaler/policy.py` 和 `tests/`。

## 25.8 结业项目：选一个“缺失能力”做完整实现

练习是单点的；结业项目要求你走完“设计 → 实现 → 测试 → 文档 → 演示”的完整链路。下面三个项目按难度排序。

### 项目 A：Kueue Workload 准入（中等）

目标：当前实现只识别 `managedBy: kueue.x-k8s.io/multikueue` 并跳过调和；请在它之外新增“本地队列准入”的最小模型（两者是不同的 Kueue 能力，不要混为一谈）。

要求：

- 新增 `Workload`/`LocalQueue`/`ClusterQueue` 对象（可以是简化 schema）；
- RayJob 创建时根据 `kueue.x-k8s.io/queue-name` 标签创建 Workload；
- ClusterQueue 配额不足时，RayJob 停在 `Initializing`，并发 `QuotaReserved=False` 事件；
- 配额释放后自动准入；
- 测试覆盖：准入/排队/抢占/释放；
- 文档写清楚与真实 Kueue 的差距。

验收：

```bash
python -m pytest -q
python examples/incident_lab.py
python check_docs.py
```

### 项目 B：真实 Ray Job HTTP 提交（中等）

目标：把 `HTTPMode` 从“Runtime 模拟提交”改成“可选的 HTTP 提交器”。

要求：

- 用标准库 `urllib`/`http.client` 实现 `POST /api/jobs/`；
- 支持 `submission_id` 幂等、超时、重试；
- 失败时写 `SubmissionFailed`，不破坏现有模拟路径；
- 用本地 stub server 做测试，不依赖真实 Ray；
- 默认行为不变（模拟），通过配置/CLI 开启。

验收：

- 单测覆盖 2xx/4xx/5xx/超时/重复提交；
- 真实集群上能用 `ray job submit` 对照；
- 文档说明“哪些是真实、哪些是模拟”。

### 项目 C：多集群控制面（困难）

目标：实现一个“管理集群 + 两个工作集群”的最小多集群视图。

要求：

- 每个工作集群一个 `LocalClient`/Store；
- 管理面聚合 `RayCluster`/`RayJob` 状态；
- 创建 CR 时按 label/租户路由到目标集群；
- 删除/升级不跨集群误操作；
- 测试覆盖：路由、故障隔离、跨集群查询；
- 文档写清楚一致性/延迟/失败语义。

验收：

- 两个集群的 Store 独立；
- 管理面能看到聚合状态；
- 工作集群故障不影响另一个；
- 有明确的“不支持”的边界。

### 项目交付物

- 代码 + 测试；
- 一页设计文档（动机/API/失败/兼容）；
- 演示脚本/命令；
- 文档章节或附录；
- 与真实 KubeRay 的差距说明。

## 25.9 结语

KubeRay 的复杂度不在 YAML，而在“分布式运行时 + Kubernetes 控制器 + 多种生命周期”的交汇处。把 MiniKubeRay 跑起来、改坏、修好，比只看文档更能建立直觉。

当你能回答下面这些问题，就真正掌握了这套系统：

- 为什么 head Service 是 headless？
- 为什么 autoscaler 要写 workersToDelete？
- 为什么 RayService 需要 pending cluster？
- 为什么 GCS FT 不能解决所有 head 故障？
- 为什么 `lastTransitionTime` 不能每次都更新？
- 为什么 Helm 升级不会升级 CRD？
- 为什么 RayJob 的 JobStatus 和 JobDeploymentStatus 是两套状态？

## 25.10 参考资料

见 [附录 E](kuberay教程-63-附录E-参考资料.md)。

← [第 24 章 测试、验证与故障注入](kuberay教程-24-测试、验证与故障注入.md)

→ [第 26 章 真实集群实验：从 kind 到第一个 RayService](kuberay教程-26-真实集群实验-从kind到第一个RayService.md)
