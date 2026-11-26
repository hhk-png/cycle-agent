# 第 29 章 未来方向

## 29.1 版本节奏与 v1.17

截至 2026 年 11 月：

- v1.16.0 是稳定线（2026-10-28）；
- v1.16.x patch 持续；
- v1.17 处于 alpha（`v1.17.0-alpha` 伪版本已出现）。

v1.16 roadmap 中明确推迟到 v1.17 的事项包括：

- `gangPreempt`/`gangReclaim` 的进一步过滤逻辑，以及是否合并回传统 preempt/reclaim；
- 面向 kube-scheduler 的 HyperNode 插件（在 HyperNode controller 独立之后）；
- 调度器性能/可扩展性继续优化；
- Unschedulable Job Cache 设计的完整实现（提案 #5644）；
- NamespaceQueue 从 Alpha 走向 Beta/GA（CRD 稳定性、卸载保留、配额审计）；
- fairshare 的 per-user 维度、与计费/Showback 的对接；
- group-topology-affinity 与 SubGroup/多级 gang 的组合；
- usage-aware 调度与真实利用率指标的标准化。

读 roadmap 的方法：

- GitHub issue（如 v1.16 roadmap #5365）；
- release notes；
- KubeCon 演讲；
- 设计文档（docs/design）。

不要只依赖博客标题。

## 29.2 2026 的主题：从批处理到统一 AI 调度

v1.14 的发布标题是“Unified AI Scheduling”，v1.15/v1.16 继续这个方向。可以看到四条主线：

### 29.2.1 训练 + 推理 + Agent

- 训练：gang、拓扑、DRA、checkpoint。
- 推理：LWS、Kthena、KV cache 感知、token 吞吐。
- Agent：AgentCube/Agent Scheduler，低延迟、短任务、海量并发。

传统批调度器只关心“作业何时开始”；推理/Agent 还关心“请求如何路由”“KV cache 在哪”“延迟预算”。这要求调度器从“作业级”扩展到“请求级/会话级”。

### 29.2.2 异构设备统一抽象

- GPU/MIG/vGPU/vNPU；
- DRA DeviceClass/ResourceClaim；
- 队列配额按设备类别；
- 统一账本，避免“整卡/切片/共享”三套账。

v1.15 的 DRA 队列配额是重要一步，但部分分配模式、跨驱动的一致性仍在演进。

### 29.2.3 拓扑从“机架”到“超节点”

- HyperNode 支持多级、子组、partition；
- 标签式自动发现，降低 IB 依赖；
- 与 gangPreempt 结合，抢占前模拟放置；
- 未来可能接入交换机带宽/拥塞信息。

这反映了一个趋势：大模型训练的性能瓶颈越来越在网络上，调度器必须理解网络。

### 29.2.4 多集群与数据感知

- Volcano Global 的 HyperJob；
- Karmada 集成；
- DataSourceClaim/DataSource；
- 跨集群配额与故障转移。

多集群不是“把 Pod 发到另一个集群”这么简单，它涉及数据、网络、配额、状态的一致性。

## 29.3 推理调度：下一个战场

训练作业通常是“长时间、大 gang、少变”；推理是“短请求、弹性、延迟敏感”。两者的调度模型不同：

| 维度 | 训练 | 推理 |
|---|---|---|
| 单位 | Job/PodGroup | 请求/副本/会话 |
| 目标 | 尽快开始、不中断 | 延迟、吞吐、成本 |
| 资源 | 整卡/多卡 | 共享/MIG/分时 |
| 扩缩 | 作业级 | 副本级/HPA |
| 拓扑 | 强（RDMA） | 弱（但 KV cache 有亲和） |

Volcano 的 Kthena 子项目试图把推理纳入统一调度：

- KV cache 感知路由；
- token 吞吐自动扩缩；
- 多机多卡 serving；
- 与 LWS/HyperNode 结合。

这块仍在早期，生产采用要谨慎评估成熟度。可落地的 serving 组建模、PDB/反亲和、P/D 分离与容量模型，见第 34.8 节。

## 29.4 Agent 调度

AI Agent 工作负载的特点：

- 大量短时任务；
- 对冷启动敏感；
- 需要沙箱/隔离；
- 可能突发。

v1.14 的 Agent Scheduler 是 Alpha：

- 多 worker 并行消费；
- Conflict-Aware Binder；
- 与批调度器通过 Sharding Controller 协调。

未来可能出现“批调度器 + 低延迟调度器”的双引擎架构：批处理走 Volcano，Agent/推理走专用调度器，共享节点池。

## 29.5 DRA 的成熟

DRA 仍在快速演进。未来要关注：

- 设备属性/约束的表达能力；
- 共享 claim 的配额与回收；
- 部分分配（consumable capacity）；
- 与 topology manager 的协同；
- 跨厂商驱动的一致性；
- 调度器对 claim 生命周期的处理。

Volcano v1.15 的 DRA 队列配额是起点，不是终点。生产上要验证：

- claim 去重是否正确；
- 驱逐后 claim 是否释放；
- 队列配额与实际设备用量是否一致；
- 故障恢复时 claim 是否泄漏。

## 29.6 弹性与 checkpoint

训练作业的弹性（elastic training）要求：

- 作业可以在运行时增减 worker；
- 失败后从 checkpoint 恢复；
- 调度器支持“部分重启”；
- gang 语义从“全有全无”变成“最小可用 + 弹性扩缩”。

Volcano 的 `minAvailable < replicas` 提供了基础，但完整的弹性训练还需要框架层（PyTorch Elastic、Ray、Kubeflow）配合。

Checkpoint 感知调度是另一个方向：优先把作业调度到能访问最近 checkpoint 的节点/存储。

## 29.7 公平性的下一步

当前公平插件：

- proportion：静态权重；
- DRF：当前份额；
- fairshare：历史衰减（v1.16，按 namespace）；
- SLA：等待时间。

未来可能：

- fairshare 扩展到 per-user/per-team，而不只是 namespace；
- 多资源维度的加权公平；
- 考虑抢占成本的公平（被抢占的作业获得补偿）；
- 跨集群公平；
- 与计费/Showback 结合；
- 可解释的公平决策（为什么这个作业被选中）。

一个现实的工程判断：公平插件越多，排序越难解释。生产上不要同时开四五个公平插件；选定 1–2 个主公平维度（如 fairshare + DRF），其余用队列配额表达。

## 29.8 可观测性与可解释性

调度器的决策越来越复杂，运维需要：

- 为什么这个作业 Pending？
- 为什么这个队列被回收？
- 为什么这个 Pod 去了这个节点？
- 如果改配额会怎样？

Volcano 已有事件/日志/指标，但“决策链”仍不够完整。未来的方向：

- 结构化决策日志；
- what-if API；
- 调度 trace；
- 与 OpenTelemetry 集成；
- 公平性/利用率的可视化。

## 29.9 安全与多租户

随着 GPU 成为稀缺资源，安全攻击面扩大：

- 抢占/回收可能被滥用；
- 优先级提升可能绕过配额；
- 共享 GPU 的侧信道；
- DRA claim 的权限；
- webhook 的 DoS。

未来需要：

- 更细粒度的 RBAC；
- 配额/优先级的策略即代码；
- 租户级审计；
- 设备隔离（MIG/ confidential computing）。

## 29.10 标准与生态

Kubernetes 社区在推进：

- 原生 Workload/PodGroup API（Kueue 参与的 WG）；
- DRA；
- Topology API；
- Scheduling Framework。

Volcano 与这些标准的关系：

- 短期：Volcano 自己的 CRD；
- 中期：与原生 API 双轨；
- 长期：可能收敛到原生 API，Volcano 作为调度实现。

这对采用者的含义：

- 不要过度绑定某个 CRD 的细节；
- 把队列/配额/优先级抽象成平台 API；
- 保留迁移路径。

## 29.11 对采用者的建议

1. **跟 patch 线**：CVE 修复优先。
2. **不要把 alpha 当稳定**：gangPreempt、Agent Scheduler、DRA 配额都要在测试集群验证。
3. **保留抽象层**：队列/优先级/作业模板不要直接暴露给用户。
4. **投资可观测性**：调度器的决策链比 YAML 更重要。
5. **关注生态**：Kueue、LWS、Ray、Kubeflow 的集成可能比 Volcano 自身功能更影响体验。
6. **定期复盘**：每季度看利用率、等待时间、抢占率、成本。

## 29.12 Kthena、Agent 与推理调度的技术细节

第 29.3/29.4 节给了方向，这一节把“为什么推理/Agent 不是训练调度的简单复用”拆开。本节只讨论趋势与未解问题；可执行的 YAML、并行策略、checkpoint/弹性和容量公式见第 34 章。

### 29.12.1 推理的三层调度

```text
第一层：副本放置（Volcano 已有）
  一个 serving 组需要几张卡、是否同超节点、是否跨故障域
第二层：请求路由（Kthena/KServe 等）
  请求去哪个副本、哪个 KV cache 命中、是否需要排队
第三层：扩缩容（HPA/KEDA/自定义）
  副本数、模型实例数、GPU 共享比例
```

Volcano 的强项在第一层；第二、三层通常由推理框架/网关负责。把三者混在一个控制器里，会导致“调度器不理解 token，网关不理解节点”。

### 29.12.2 推理与训练的调度差异

| 维度 | 训练 | 推理 |
|---|---|---|
| 调度单位 | Job/PodGroup | 副本/请求/会话 |
| 时间尺度 | 小时–天 | 毫秒–分钟 |
| 资源 | 整卡、长期占用 | 共享/MIG、弹性 |
| 关键指标 | 开始时间、不中断 | 延迟 P99、吞吐、成本 |
| 拓扑 | 强（RDMA/NVLink） | 弱，但 KV cache 有亲和 |
| 抢占 | 可 checkpoint | 会丢请求/破坏 SLO |

因此推理调度需要：

- **PDB/最小副本**：不能为了装箱把 serving 组打散；
- **组间反亲和**：多组副本跨故障域（`group-topology-affinity`）；
- **共享 GPU 账本**：vGPU/MIG 的显存/算力配额；
- **快速扩缩**：副本级 HPA 与 gang 的冲突要处理；
- **请求感知**：KV cache 命中、模型加载时间、冷启动。

### 29.12.3 Kthena 的定位

Kthena 是 Volcano 生态里面向 LLM 推理编排的子项目（具体能力以项目文档为准）。公开方向包括：

- 多机多卡 serving 的 gang/角色编排；
- KV cache 感知的路由/放置；
- token 吞吐驱动的扩缩；
- 与 LWS/HyperNode 结合。

它补的是“推理编排层”，不是替代 Volcano 的批调度。一个现实的架构：

```text
用户请求
  → 推理网关（路由/KV cache）
  → Kthena/推理 operator（副本/模型生命周期）
  → Volcano（PodGroup 放置、拓扑、队列）
  → device plugin/DRA（GPU/MIG）
```

### 29.12.4 Agent 调度的特殊性

Agent 工作负载通常是：

- 大量短时任务（秒级）；
- 突发、不可预测；
- 需要沙箱/隔离；
- 对冷启动敏感；
- 可能访问外部工具/网络。

这与批调度的“大 gang、长运行”相反。Agent Scheduler 的方向是：

- 多 worker 并行消费，降低单次调度延迟；
- Conflict-Aware Binder 处理并发绑定冲突；
- 与批调度器通过分片协调节点，避免互相抢节点；
- 更细粒度的资源（CPU/内存小块）和快速回收。

### 29.12.5 还有哪些没解决

- **请求级抢占**：为了一个高优请求驱逐整个 serving 副本，通常不划算；
- **KV cache 迁移**：副本迁移后 cache 失效，调度器是否感知；
- **模型加载时间**：冷启动可能几十秒，调度器只保证绑定，不保证就绪；
- **GPU 共享的 SLO**：共享卡上的噪声邻居如何隔离；
- **跨集群推理**：数据/模型在哪，请求去哪。

这些问题不是 Volcano 一个项目能全部解决的；采用时要明确“哪一层负责什么”，避免把推理 SLO 寄托在调度器上。

### 29.12.6 采用建议

1. 先把训练/批处理的队列、gang、拓扑跑稳；
2. 推理先用 LWS/Deployment + PDB + `group-topology-affinity`，验证故障域；
3. 需要 KV cache/请求路由时，再引入 Kthena/KServe 等推理层；
4. 不要把推理副本放进会频繁 shuffle/抢占的队列；
5. 用真实流量压测，不要只看 GPU 利用率。

## 29.13 本章小结

- v1.17 在 alpha，gangPreempt/HyperNode 插件/性能是重点。
- 2026 的主线是训练+推理+Agent 的统一调度、异构设备、拓扑、多集群。
- 推理和 Agent 是下一个战场，调度模型与训练不同。
- DRA、弹性、公平、可解释性、安全都还在演进。
- 采用者要跟 patch、验证 alpha、保留抽象层、投资可观测性。
- 第 34 章把这些方向落成可执行的训练/推理/Agent 作业形态、并行策略、checkpoint/弹性与容量模型。

下一章开始商业分析：这个市场有多大，价值链在哪里。
