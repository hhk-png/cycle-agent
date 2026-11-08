# 第 18 章 · 生产落地手册

把 Kueue 装进集群只是第一步。真正困难的是：**如何让配额、队列和抢占成为平台与租户之间可解释、可运营的合同。** 本章给出一套从试点到规模化落地的工程方法，包括容量模型、迁移路径、SLO、变更管理和排障 runbook。

## 18.1 落地路线图

不要一次性把所有作业切到 Kueue。推荐四个阶段：

```text
阶段 0：评估
  ├── 盘点作业类型（Job/JobSet/Ray/MPI/推理）
  ├── 盘点资源池（GPU 型号、spot/on-demand、节点池）
  └── 明确目标（公平、隔离、利用率、多集群）

阶段 1：影子模式
  ├── 安装 Kueue，但只管理少量新作业
  ├── 不改变现有 ResourceQuota/调度器行为
  └── 观察 Workload、指标、队列长度

阶段 2：试点租户
  ├── 选 1～2 个愿意配合的团队
  ├── 建立 LocalQueue → ClusterQueue → Cohort
  ├── 定义配额合同与优先级
  └── 跑通暂停/放行、抢占、回收

阶段 3：规模化
  ├── 迁移存量作业（按团队/框架分批）
  ├── 建立配额变更流程
  ├── 接入告警、仪表盘、成本分摊
  └── 评估多集群/TAS/DRA 等高级能力
```

每个阶段都要有**退出条件**，例如：

- 试点阶段：连续两周无异常的准入延迟、无意外驱逐；
- 规模化阶段：pending 队列可解释、配额变更可回滚、租户投诉有 runbook。

## 18.2 容量模型：从物理容量到 nominalQuota

Kueue 的配额是逻辑配额，不是物理容量。两者之间需要有意识地留出 gap。

### 18.2.1 一个可用的计算框架

对每个资源池（Flavor）和资源 `r`：

```text
物理可调度容量 C_r
  = 节点 allocatable 总和
  - 系统组件预留
  - DaemonSet/基础设施占用
  - 高优先级非 Kueue 作业预留

逻辑配额总和 N_r
  = Σ ClusterQueue.nominalQuota(r)

安全余量
  = C_r - N_r
```

经验规则：

- **N_r 不应长期显著大于 C_r。** 否则大量作业会"逻辑上可准入、物理上 pending"，Kueue 的队列会失去解释力。
- **N_r 可以略小于 C_r**，留出碎片、系统组件和故障恢复空间。
- **Cohort 共享额度**可以提高利用率，但要让 `lendingLimit` 和抢占策略留出回收空间。
- **GPU 要同时看卡数、显存、节点数和拓扑。** 只看卡数会低估碎片；TAS/DRA 场景尤其如此。

### 18.2.2 一个 GPU 池的例子

```text
32 台 8 卡 A100 节点
物理 GPU：256
系统/驱动/监控预留：约 8 卡
可调度：248

三个团队：
  team-a nominal 96
  team-b nominal 64
  team-c nominal 64
合计：224

安全余量：24 卡（约 10%）
```

如果三个团队同时满载，224 < 248，物理上仍有 24 卡余量；如果某个团队临时借用，Cohort 总量仍然是 224，不会超过逻辑上限。真正的物理碎片由 `WaitForPodsReady`、TAS 和调度器兜底。

### 18.2.3 超卖与借用的区别

- **nominal 超卖**：N_r > C_r。逻辑上允许，但物理上可能排队。适合"不是所有队列会同时满载"的场景，但要有监控和回收策略。
- **Cohort 借用**：N_r 不增加，只是把空闲配额临时给别的 CQ。相对安全，但需要 `lendingLimit` 和抢占策略。

生产上更推荐"nominal 保守 + Cohort 借用"，而不是大幅超卖 nominal。

> 容量模型不只是算总和：还要算碎片、队头阻塞和等待时间分布。第 29 章给出了 λ/S/c/ρ、Erlang C 的局限、碎片概率和可运行的离散事件模拟器，可以直接用来做配额评审。

### 18.2.4 一个可复用的容量模型模板

把容量模型写成可评审的表格，而不是散落在 wiki：

```text
资源池：a100-prod
  节点：32 × 8 卡
  物理 GPU：256
  系统/驱动/监控预留：8
  可调度：248
  安全余量：10%（24）
  可分配逻辑配额：224

租户配额：
  team-a  nominal 96   borrowingLimit 32   lendingLimit 16
  team-b  nominal 64   borrowingLimit 32   lendingLimit 16
  team-c  nominal 64   borrowingLimit 16   lendingLimit 8
  合计 nominal：224

Cohort 共享池：0（先不设，避免双重计算）
```

评审时问五个问题：

1. 三个团队同时满载时，物理上会不会有作业 pending？预期是多少？
2. 谁可以借谁？最坏情况下借出多少？原主回收时会不会抢占？
3. GPU 碎片/拓扑是否会让"卡数够"仍然放不下？
4. spot 中断时，哪些队列会被优先牺牲？
5. 容量模型变化时，谁审批、如何回滚？

## 18.3 配额合同

把配额当成产品合同，而不是一组 YAML 数字。一份可运营的配额合同至少包含：

| 项目 | 内容 |
|---|---|
| 租户 | namespace / 团队 / 负责人 |
| 入口 | LocalQueue 名称、允许的作业类型 |
| 保底 | ClusterQueue nominalQuota（按 Flavor/资源） |
| 借用 | 可借入上限、可借出上限、Cohort |
| 优先级 | 允许使用的 WorkloadPriorityClass / Pod PriorityClass |
| 抢占 | 是否可被抢占、可抢占谁 |
| 公平 | FairSharing weight |
| SLO | 期望排队时间、最大等待时间 |
| 变更 | 谁审批、如何回滚、观察窗口 |

### 18.3.1 命名约定

建议统一命名，降低排障成本：

```text
namespace:        team-a
LocalQueue:       team-a-queue
ClusterQueue:     team-a-cq
Cohort:           org-ai / root
ResourceFlavor:   a100-spot / a100-ondemand / l4-inference
WorkloadPriority: critical / standard / best-effort
```

标签建议：

```yaml
metadata:
  labels:
    platform.example.com/tenant: team-a
    platform.example.com/cost-center: ai-001
    platform.example.com/environment: prod
```

这些标签可以用于成本分摊、审计和自动化。

### 18.3.2 不要用配额表达所有策略

Kueue 只管理它覆盖的资源。以下需求不应该只靠 Kueue：

- 安全隔离 → RBAC、NetworkPolicy、Pod Security；
- 镜像可信 → 供应链策略；
- 节点隔离 → taint/toleration、专用节点池；
- 预算/成本 → 财务系统或成本分摊工具；
- 作业依赖 → Argo/JobSet 等工作流引擎。

## 18.4 迁移路径

### 18.4.1 从 ResourceQuota 迁移

ResourceQuota 是 namespace 硬上限，Kueue 是作业级队列。迁移时注意：

1. **不要立即删除 ResourceQuota。** 先让 Kueue 管理新作业，保留 ResourceQuota 作为兜底。
2. **把 ResourceQuota 的额度映射成 CQ nominal**，但要按 Flavor/资源拆分。
3. **处理没有 queue-name 的作业。** 默认不管理；如果开启 `manageJobsWithoutQueueName`，要确保系统 Job 不会被误管。
4. **观察一段时间后再收紧 ResourceQuota**，避免双重限制导致困惑。

### 18.4.2 从 Volcano/YuniKorn 迁移

这些项目可能替代或扩展 kube-scheduler。迁移时要明确：

- 哪些能力由 Kueue 接管（队列、配额、公平、抢占）；
- 哪些能力仍由调度器负责（gang、拓扑、节点放置）；
- 是否存在两套队列语义冲突；
- 是否需要保留旧的 PriorityClass/Queue 对象。

建议先在非关键团队试点，避免两套调度器同时争抢同一批节点。

### 18.4.3 从自研 Job 控制器迁移

自研框架需要实现集成层：

- 创建/更新 Workload；
- 暂停/放行真实作业；
- 同步完成/失败/驱逐；
- 处理 Flavor 注入；
- 报告 `reclaimablePods`。

第 8 章给出了最小伪代码；第 15 章列出了从模拟器到真实控制器的路径。

## 18.5 SLO 与指标

### 18.5.1 建议的 SLO

| SLO | 指标 | 示例目标 |
|---|---|---|
| 准入延迟 | Workload 创建到 QuotaReserved | P99 < 30s（小集群） |
| 排队时间 | Pending 到 QuotaReserved | 按优先级分层设定 |
| 抢占率 | 每小时被抢占作业数 | 低于阈值，异常时告警 |
| 队列可解释性 | pending 有明确 Reason 的比例 | > 99% |
| 控制器可用性 | 调度循环持续运行 | 99.9% |
| AdmissionCheck 延迟 | Pending 到 Ready | 按外部系统 SLA |

注意：**不要用平均值评估队列健康。** 大作业的等待时间分布通常长尾，P95/P99 更有意义。

### 18.5.2 必须有的仪表盘

1. **队列视角**：每个 CQ 的 pending、admitted、usage/nominal、borrowing；
2. **租户视角**：每个 LocalQueue 的等待时间、驱逐次数、优先级分布；
3. **Cohort 视角**：子树配额、借入/借出、公平份额；
4. **控制器视角**：reconcile 延迟、workqueue 深度、错误率、重启次数；
5. **物理视角**：节点利用率、GPU 分配率、Pod pending 原因。

### 18.5.3 告警

建议从以下告警开始：

```promql
# pending 持续增长
sum(kueue_pending_workloads{status="active"}) > 100
for: 30m

# CQ 长期过载
kueue_cluster_queue_resource_usage / kueue_cluster_queue_nominal_quota > 0.95
for: 1h

# 驱逐率异常
rate(kueue_evicted_workloads_total[10m]) > 5

# AdmissionCheck 等待过长
histogram_quantile(0.99,
  sum by (le) (rate(kueue_admission_checks_wait_time_seconds_bucket[10m]))
) > 600

# 控制器重启
increase(kube_pod_container_status_restarts_total{namespace="kueue-system"}[1h]) > 0
```

阈值要按集群规模和业务基线调整，不要直接抄。

## 18.6 变更管理

配额和抢占策略的变更，应该像代码一样管理：

1. **配置即代码**：CQ/Cohort/Flavor 放在 Git，经过 review 后 apply；
2. **变更窗口**：大配额调整避开业务高峰；
3. **观察指标**：变更后至少观察一个完整作业周期；
4. **回滚预案**：保留旧配置，明确回滚命令；
5. **审计**：记录谁改了哪个 CQ、为什么改、影响了谁。

### 18.6.1 高风险变更清单

- 降低 nominalQuota；
- 提高 `reclaimWithinCohort` 到 `Any`；
- 开启 Fair Sharing 抢占；
- 修改 `namespaceSelector`；
- 开启 `manageJobsWithoutQueueName`；
- 启用 Configurable Preemption；
- 修改 Cohort 树层级或新增 Cohort 级 borrowingLimit；
- 开启 Admission Fair Sharing 或调整采样窗口/半衰期（真实 Kueue 的 entry penalty 由公式自动计算，不是可配置字段）；
- 开启 Concurrent Admission；
- 修改 DRA `deviceClassMappings` 或资源变换；
- 修改 admission gate 的 owner/清除条件；
- 升级 Kueue 版本/CRD。

这些变更都可能引发大规模驱逐或准入失败，必须在 staging 验证。

## 18.7 Runbook

### 18.7.1 Pending 作业突然增多

```text
1. 看是全局还是单个 CQ/LQ
2. 看 pending 的 Reason（配额/Cohort/Selector/AdmissionCheck/Inactive）
3. 看物理资源是否空闲
4. 看是否有 CQ 被 Hold/Inactive
5. 看是否有大量借用导致总量满
6. 看控制器是否重启/卡住
7. 必要时临时提高配额或暂停低优先级提交
```

### 18.7.2 抢占风暴

```text
1. 看驱逐率和受害 CQ/优先级分布
2. 检查是否开启了 Fair Sharing 抢占或 borrowWithinCohort
3. 检查优先级是否配置过低/过高
4. 检查是否出现对称的 Configurable Preemption 规则
5. 临时把 reclaimWithinCohort 降为 Never，止血
6. 调整权重/配额，而不是只调优先级
```

### 18.7.3 队列 Active=False

```text
1. kubectl describe clusterqueue <name>
2. 检查引用的 Flavor/Cohort/AdmissionCheck 是否存在
3. 检查 namespaceSelector
4. 检查资源组/Flavor schema
5. 检查 webhook 是否正常
6. 修复后等待 controller 重新协调
```

### 18.7.4 控制器重启后长时间不调度

```text
1. 确认 leader election 是否完成
2. 看 informer 是否完成全量 list
3. 看 workqueue 是否积压
4. 看 Cache 重建是否报错
5. 看 apiserver watch/QPS 是否受限
6. 大规模集群可能需要几分钟，不要急于重启
```

### 18.7.5 租户投诉"排队慢"

```text
1. 确认是单个 LQ/CQ 还是全局
2. 看 pending 原因分布（配额/Inadmissible/AdmissionCheck/退避）
3. 看该租户的 usage/nominal、borrowed、weightedShare
4. 看是否有其他 CQ 长期借用、lendingLimit 是否锁住
5. 看是否有 StrictFIFO 队头阻塞
6. 看是否有高优先级/抢占把该租户挤掉
7. 给出"等、调配额、调权重、调优先级"中的明确建议
```

不要用"集群满了"一句话打发；Kueue 的价值就是让"为什么慢"可解释。

### 18.7.6 容量模型漂移

```text
1. 对比 spec nominal 总和与物理可调度容量
2. 看 effectiveQuotas（如果启用 DQO）
3. 看长期 usage 与 nominal 的比值
4. 看 pending 是否集中在特定 Flavor/资源
5. 重新跑容量模型，更新配额合同
6. 变更走 GitOps + review，不要手工 patch
```

### 18.7.7 升级失败/回滚

```text
1. 停止新的配额变更
2. 保留旧 CRD/Deployment 清单
3. 按 release notes 回滚控制器
4. 检查 CRD schema 是否兼容
5. 验证 webhook 证书
6. 观察队列恢复情况
```

## 18.8 多集群与多租户

### 18.8.1 多集群

MultiKueue 适合：

- 多个可用区/区域；
- 本地集群 + 云上突发；
- 多团队各自管理 worker 集群；
- 数据 locality 或合规要求。

落地要点：

- manager 的配额应约等于 worker 总可用量，避免过度提名；
- worker kubeconfig 用 Secret/ClusterProfile，不用 Path；
- dispatcher 的批量/间隔要按网络延迟调优；
- 明确 manager 故障时的行为；
- 监控跨集群状态同步延迟。

### 18.8.2 多租户治理

- 平台管理员管理 CQ/Cohort/Flavor/AdmissionCheck；
- 租户只管理自己的 LocalQueue 和作业；
- namespace 标签由平台控制，租户不能随意修改；
- 优先级类由平台审批；
- 监控每个租户的借用、抢占、等待时间；
- 定期回顾配额合同，按实际使用调整。

## 18.9 成本与利用率

Kueue 提高利用率的手段：

- Cohort 借用；
- 抢占/回收；
- 动态回收（reclaimablePods）；
- 多 Flavor（spot/on-demand）；
- 多集群调度；
- 公平分享。

但利用率不是唯一目标。要同时看：

- 租户等待时间；
- 抢占导致的重复计算；
- spot 中断成本；
- GPU 碎片；
- 运维复杂度。

一个常见的误区是"把 nominal 压得很低，靠借用提高利用率"。短期看利用率高，长期会导致：

- 大作业频繁等待；
- 抢占/重排队抖动；
- 租户无法规划容量；
- 排障困难。

更稳健的做法是：**保底配额给足，借用和公平分享处理峰谷。**

## 18.10 上线检查清单

### 安装前

- [ ] 明确 Kueue 版本与 API 版本（v1beta2）；
- [ ] 盘点作业框架，确定启用哪些 integration；
- [ ] 规划 Flavor（GPU 型号、spot/on-demand、节点池）；
- [ ] 规划 Cohort 树和配额合同；区分隐式/显式 Cohort；
- [ ] 确定优先级体系；
- [ ] 决定是否启用 Admission Fair Sharing、TAS、DRA、DQO、Concurrent Admission；
- [ ] 确认这些 gate 的依赖链和默认值；
- [ ] 准备 staging 集群。

### 试点前

- [ ] ResourceFlavor/CQ/LQ/AdmissionCheck 通过 webhook 校验；
- [ ] `kubectl get cq -o wide` 全部 Active；
- [ ] 提交测试 Job，验证暂停/放行；
- [ ] 验证配额不足时的 pending 行为；
- [ ] 验证抢占/回收；
- [ ] 验证指标和仪表盘；
- [ ] 验证卸载/finalizer 流程。

### 规模化前

- [ ] 容量模型和 nominal 总和对齐；
- [ ] SLO/告警就绪；
- [ ] 变更流程和回滚预案；
- [ ] 租户文档和 runbook；
- [ ] 性能基准（CQ 数、Flavor 数、pending 数）；
- [ ] 升级/备份策略。

### 上线后

- [ ] 每周回顾 pending/借用/抢占；
- [ ] 每月回顾配额合同；
- [ ] 监控控制器资源与 apiserver 压力；
- [ ] 定期演练控制器重启/leader 切换；
- [ ] 跟踪 Kueue release notes 和 feature gate 变化。

## 18.11 本章小结

- 落地要分阶段：评估 → 影子 → 试点 → 规模化。
- 容量模型要区分物理容量、逻辑配额和安全余量；nominal 不宜大幅超卖。
- 配额是合同，不是一组数字；要有命名、标签、SLO、变更和回滚。
- 迁移从 ResourceQuota/Volcano/自研控制器时，要保留兜底并分批切换。
- SLO 看 P95/P99，不要只看平均值；告警覆盖 pending、过载、驱逐、AdmissionCheck 和控制器健康。
- Runbook 要覆盖 pending 激增、抢占风暴、CQ Inactive、重启恢复和升级回滚。
- 多集群/多租户要把凭据、配额、优先级和标签治理一起设计。

---

[← 上一章：Kueue 控制器内部原理](17-kueue-internals.md) | [返回目录](../README.md) | [下一章：MiniKueue 扩展实验室 →](19-mini-kueue-extension-lab.md)
