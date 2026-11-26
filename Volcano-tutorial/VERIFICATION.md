# 验证记录

验证日期：2026-11-26

## 1. 环境

| 项 | 值 |
|---|---|
| OS | Windows 11 Home China 10.0.26200 |
| Go | go1.27.0 windows/amd64 |
| Python | 3.14.6（PyYAML 6.0.3，仅用于文档/示例检查） |
| 集群 | 未连接真实 Kubernetes；调度语义由 MiniVolcano 模拟 |
| 外部依赖 | 无（MiniVolcano 仅标准库） |

## 2. 代码质量

```bash
cd mini-volcano
gofmt -l .
go vet ./...
go build ./...
go test ./... -count=1
go run ./cmd/minivolcano -all
```

结果：

| 命令 | 结果 |
|---|---|
| `gofmt -l .` | 无输出 |
| `go vet ./...` | 通过 |
| `go build ./...` | 通过 |
| `go test ./... -count=1` | 9 个包全部 `ok` |
| `go test ./... -shuffle=on` | 通过（测试无顺序依赖） |
| `go run ./cmd/minivolcano -all` | `28 scenarios, 28 passed, 0 failed` |
| `go test -race` | **未运行**：当前环境 `CGO_ENABLED=0`，`-race` 需要 cgo |

## 3. 测试计数

```bash
go test ./... -count=1 -v | grep -c '^--- PASS'
# 129

go test ./... -count=1 -v | grep -c '^    --- PASS'
# 12（TestRandomizedInvariants 的 12 个 seed）

go run ./cmd/minivolcano -all
# 28 scenarios, 28 passed, 0 failed
```

合计 **141 个 PASS 行**（129 顶层 + 12 随机不变量子测试）。

按包：

| 包 | 顶层 PASS |
|---|---:|
| `internal/api` | 21 |
| `internal/cache` | 20 |
| `internal/framework` | 10 |
| `internal/plugins` | 29 |
| `internal/actions` | 20 |
| `internal/controller` | 12 |
| `internal/metrics` | 5 |
| `internal/scheduler` | 6 |
| `internal/scenario` | 6（+12 子测试） |
| **合计** | **129（+12）** |

## 4. 场景

28 个场景全部通过：

```text
basic, gang, task-gang, min-success, priority, reclaim, proportion, drf, binpack,
topology, group-topology, task-topology, reservation, sla, fairshare,
hierarchy, numa, tdm, retry, cron, jobflow, backfill, heterogeneous,
throughput, shuffle, pdb, resourcequota, nodegroup
```

每个场景都有 `Check` 断言，不是只打印结果。`-all` 的退出码在失败时非 0。

## 5. 确定性

`TestDeterminism` 对 `basic/priority/topology/fairshare` 各运行两次，比较：

- `World.Summary()` 文本；
- 事件数量。

结果一致。随机不变量测试用固定 seed，也在重复运行时保持一致。

## 6. what-if 与 explain

```bash
go run ./cmd/minivolcano -scenario proportion \
  -what-if team-a/nvidia.com/gpu=2 -no-check -events 0
```

结果：team-a capability=2，6 卡 a-job 保持 Pending；team-b 正常拿到 2 卡。说明：

- capability 是硬上限；
- gang 在 cap < minAvailable 时完全回滚；
- what-if 从零重放场景。

```bash
go run ./cmd/minivolcano -scenario drf -explain default/a2
```

输出包含：

- `phase=Inqueue`；
- `insufficient idle resources`；
- `gang not ready ... rolled back 2 tentative bindings`。

## 7. 开发期实际修复的缺陷

详见教程第 25.7 节。摘要：

1. nil capability 被克隆成空 map，导致所有队列拒绝入队。
2. 部分 capability 被当作“其他维度为 0”，阻断 CPU 请求。
3. Prometheus gauge 残留旧标签（Pending 计数不消失）。
4. 完成 Pod 清空 NodeName，破坏放置历史/断言。
5. SLA 全局默认值污染所有作业，排序退化为 FIFO。
6. 驱逐消耗 maxRetry，导致被抢占作业直接 Failed。
7. capacity 的 root 自递归风险。
8. what-if 解析把资源名中的 `/` 当分隔符。
9. 队列清空后 gauge 序列消失，监控误判为未采集。
10. rescheduling 的受害者选择依赖 `node.Pods` 的 map 遍历顺序，同一节点上可能选中不同副本。

这些问题都由场景/指标输出/人工复核暴露，修复后有对应回归测试或场景断言。

## 8. 新增 shuffle/rescheduling 的验证

2026-11-26 在原有验证之外新增：

| 项 | 结果 |
|---|---|
| `TestReschedulingVictimsRespectUtilizationAndGang` | PASS（fragmentation 提名 + gang 保护） |
| `TestReschedulingLowNodeUtilizationDirection` | PASS（LNU 从高利用率节点驱逐，按 targetThreshold 选数量） |
| `TestShuffleSkipsWhenNoBetterTarget` | PASS（无更优目标时拒绝驱逐，防止活锁） |
| `TestShuffleEvictsThenAllocateRepacks` | PASS（驱逐 2 个 Pod、事件存在、重打包后账本守恒） |
| 场景 `shuffle` | PASS（碎片 → 疏散 → 单节点装满） |
| `-all` | `26 scenarios, 26 passed` |
| 测试计数 | 118 顶层 + 12 子测试 = 130 |

实现/复核期修复的两类问题：rescheduling 的受害者选择不能依赖 `node.Pods` 的 map 遍历顺序（第 25.7.10 节）；shuffle 必须先证明存在严格更优的目标节点，否则 binpack 会原地折返形成活锁。

### 8.1 pdb / resourcequota / nodegroup 的验证

2026-11-26 第二轮新增三个治理插件与三个场景：

| 项 | 结果 |
|---|---|
| `TestPDBAllowsOneDisruptionThenBlocks` | PASS（minAvailable=3 时 4→3 允许、3→2 拒绝） |
| `TestPDBMaxUnavailable` | PASS（maxUnavailable=1 时只允许一次中断） |
| `TestResourceQuotaGatesEnqueue` | PASS（2+3 CPU 超过 4 CPU 配额被拒绝并记录原因） |
| `TestNodeGroupRestrictsAndCaps` | PASS（组外节点被拒；组容量 2 CPU 上限生效） |
| `TestReclaimNeverBreaksGuarantee` | PASS（8 卡大 Pod 不会被回收，避免 guarantee 4 被击穿） |
| `TestDisruptableVictimsAppliesPDBCumulatively` | PASS（3 个候选中只放行 1 个，防止合并后跌破 PDB） |
| `TestGovernanceObjectsCloneDeeply` | PASS（PDB/Quota/NodeGroup 深拷贝隔离） |
| `TestNamespaceAdmittedUsageAndLabels` | PASS（仅已准入作业计入配额；标签传播） |
| `TestQueueNodeGroupAllocated` | PASS（按节点组统计队列用量） |
| `TestValidateQueueNodeGroup` | PASS（空组、重复名、负容量被拒） |
| 场景 `pdb` | PASS（1 次允许 + 2 副本抢占回滚） |
| 场景 `resourcequota` | PASS（2+1 准入、3 拒绝） |
| 场景 `nodegroup` | PASS（队列被限制在标签组内） |
| `-all` | `26 scenarios, 26 passed` |

### 8.2 group-topology-affinity 的验证

2026-11-26 第三轮新增 v1.16 的 PodGroup 级拓扑反亲和：

| 项 | 结果 |
|---|---|
| `TestGroupTopologyRequiredAntiAffinity` | PASS（只在 a 上声明规则，b 仍被对称约束；无匹配对端时 no-op；backfill 绕过） |
| `TestGroupTopologyPreferredPenalty` | PASS（双方声明只计一次，同 rack 扣 50×插件 weight 2 = -100；跨 namespace 不计分） |
| 场景 `group-topology` | PASS（只由 serve-a 声明规则；serve-a 在 rack-1、serve-b 在 rack-2，rack-1 仍有一个空节点） |
| `-all` | `26 scenarios, 26 passed` |
| 测试计数 | 118 顶层 + 12 子测试 = 130 |

实现取舍（独立复核发现的问题与修复见教程第 25.7.13 节）：

1. 规则同时从“声明方”和“被选中方”两个方向评估，避免只在一侧声明时另一侧绕过；这比上游按对象逐一评估更严格。场景和 required 测试都只在一侧声明规则。
2. 用数字 `topologyTier`，不实现 `topologyTierName`；HyperNode 模型本身没有 `tierName` 字段。省略 tier 时按 tier 1 处理。
3. preferred 惩罚按对端去重；双方都声明 50 时只扣 50，再乘插件 `weight`。
4. 只实现 PodGroup 反亲和，默认同 namespace，不实现 namespace selector、`requiredDuringSchedulingIgnoredDuringExecution` 的完整 CRD 校验。
5. Predicate 在 Statement 的 trial cache 上运行，所以同一轮 allocate 中后一个 PodGroup 能看到前一个的绑定；非 allocate action 跳过插件。

这一轮同时发现并修复了两个语义问题：

1. **PDB 累计保护**：`DisruptableFn` 如果只看已提交 cache，三个受害者各自都“再驱逐一个还安全”，合并后会违反 PDB。修复方式是在 shuffle 中按 Statement 重放驱逐，每次检查都看到前一次的结果；对应测试 `TestDisruptableVictimsAppliesPDBCumulatively`。
2. **reclaim 击穿 guarantee**：只检查“驱逐前 Overused”不够，一个 8 卡受害者会把 guarantee=4 的队列打到 0。修复方式是驱逐前检查 `allocated - victim.Requests ≥ guarantee`；对应测试 `TestReclaimNeverBreaksGuarantee`。

## 9. 独立审计

2026-11-26 做了一轮只读对抗性审计，重点检查资源账本、gang/抢占/回收、层级队列、拓扑、控制器状态和确定性。审计时 22 个场景通过（当时版本）；除一个已知的 metrics 失败（见 25.7.9）外，另发现 10 项单测未覆盖的问题。合计 11 项，已全部修复并补回归测试：

1. preempt/reclaim 在没有成功绑定抢占者 Pending Pod 时提交驱逐（高）。
2. CompleteJob 不释放其他运行副本；Cron GC 删除 Pod 不释放节点（高）。
3. NUMA 只声明 CPU/内存时，节点级 GPU 请求被误拒（中）。
4. HyperNode tier fixture 反转、缺少单调性/环校验（中）。
5. nodeorder 浮点累加依赖 map 顺序，分数不稳定（中）。
6. TaskCompleted 策略重复触发（低）。
7. 队列清空后 gauge 序列消失（中）。
8. 预留重复扣除已使用资源（中）。
9. 队列/HyperNode 环可导致递归崩溃（中）。
10. minAvailable=0 仍按全量副本入队（中）。
11. Cache.Clone 后 node.Pods 与 Pods 身份漂移（潜在）。

对应测试：`TestPreemptDoesNotEvictWhenPreemptorCannotBind`、`TestCompleteJobReleasesOtherReplicas`、`TestNUMAIgnoresNodeLevelDeviceDimensions`、`TestHyperNodeRejectsNonMonotoneTier`、`TestNodeOrderDeterministic`、`TestTaskCompletedFiresOnce`、`TestReservationOnlyHoldsUnusedCapacity`、`TestValidateQueueParentCycle`、`TestGangDisabledJobPartiallyAdmits`、`TestCloneKeepsSinglePodIdentity`。

### 9.1 第二轮对抗性复核（shuffle/rescheduling）

同日对新增的 `shuffle`/`rescheduling` 做了第二轮只读复核，发现并修复：

| # | 发现 | 严重度 | 修复 |
|---:|---|---|---|
| 1 | shuffle 驱逐后 binpack 立刻把 Pod 放回原节点，每 tick 重复，形成活锁 | 高 | `canRepack` 干跑：目标节点分数必须严格高于源节点；`TestShuffleSkipsWhenNoBetterTarget` |
| 2 | Mini 的 `lowNodeUtilization` 方向与上游相反（上游从高利用率驱逐到低利用率） | 高 | 拆成 `lowNodeUtilization`（上游方向）与 `fragmentation`（教学碎片整理）；`TestReschedulingLowNodeUtilizationDirection` |
| 3 | 首个候选节点的 Pod 全被 gang 保护时直接返回空，不再尝试下一个候选 | 中 | 遍历候选直到找到安全受害者 |
| 4 | gang 保护使用 `ReadyCount`，把 Succeeded 也算进去，可能驱逐最后两个 Running 副本 | 中 | 改为统计仍占用节点的 Pod；新增 LNU 方向测试覆盖 phase |
| 5 | 同 tier 的多个供应商各自判断“再驱逐一个安全”，合并后可能跌破 minAvailable | 中 | shuffle action 层再做一次合并后的 gang 校验 |
| 6 | 文档错误：PDB 与 rescheduling 不同 tier、backfill 顺序、附录 preempt 的 VictimTasks、示例 binpack 权重 | 中 | 已逐条修正并复跑 `check_docs.py` |

这一轮说明：**“测试全绿”仍然不能替代独立复核**。新增的 2 个测试只覆盖了复核发现的路径；`-race` 仍未运行，真实 PDB/usage/多调度器场景仍未验证。

## 10. 文档与示例检查

```bash
python scripts/check_docs.py
```

检查项：

- 35 章齐全、编号连续；
- 每章有唯一 H1；
- README 链接到全部章节；
- 文档内部链接可解析；
- 代码围栏成对；
- 无 TODO/FIXME 占位；
- 36 个示例文件可解析（YAML 多文档/JSON）；
- 示例 kind/apiVersion/关键字段；
- README/VERIFICATION/附录中的计数与实际文件一致；
- MiniVolcano 场景数、插件数、action 数、测试计数与文档一致。

## 11. 未验证项

- 真实 Kubernetes 集群上的安装/调度（本环境无集群）。
- GPU/MIG/vGPU/DRA 设备行为。
- 多集群/Volcano Global。
- 并发/race（CGO 关闭）。
- 生产规模性能。
- Webhook/RBAC/网络安全。
- 真实 Volcano v1.16 的 CRD schema 逐字段校验（示例按公开文档编写，未对集群 apply）。
- 真实 PDB 控制器语义（`status.disruptionsAllowed`、Eviction API）与 API Server 级 ResourceQuota 准入；Mini 只验证了调度器侧的简化模型。
- 上游 NodeGroup 的 `spec.affinity`/`strict` 与 `nodegroup-resource-limits` 注解已按 v1.16.0 源码核对，但未在真实集群验证；Mini 使用自己的 `Queue.spec.nodeGroup` 字段，不能直接作为迁移依据。
- Mini 实现了作业级 `minAvailable`、task 级 `minTaskMember` 与 `minSuccess`；`dependsOn`、作业级 `volumes`、Queue `spec.affinity`/`strict`、`subGroupPolicy`/`partitionPolicy` 与作业插件未实现，也未在 Mini 中验证。
- 第 34 章的 LLM 示例未在真实 GPU 集群上运行：训练脚本、vLLM 多机启动参数、NCCL 网卡名、PVC、HyperNode 数据都是占位；显存/算力/KV cache/排队公式是估算模型。
- 非 NVIDIA 加速器（昇腾/AMD/国产 NPU）的资源名、HCCL/RCCL、vNPU 行为未在真实硬件上验证。

## 12. 复现步骤

```bash
cd Volcano-tutorial/mini-volcano
gofmt -l .
go vet ./...
go build ./...
go test ./... -count=1
go run ./cmd/minivolcano -all

cd ..
python scripts/check_docs.py
python scripts/capacity_model.py --selftest
```

预期：全部通过；`-race` 需要另装有 cgo 的环境。

## 13. 结论

- MiniVolcano 在其声明的范围内（单集群、确定性、标量资源、简化控制器）通过了单元、场景、随机不变量和确定性验证。
- 它**不**等价于生产 Volcano；API、并发、设备、多集群、安全均未覆盖。
- 文档与示例的数量/链接由 `check_docs.py` 守护。
- 已知限制在第 24、25 章与附录 F 中列明。

## 14. 第四轮文档修订复核（2026-11-26）

第四轮只扩充文档，不改 MiniVolcano 代码、示例或计数。修订后重新执行完整验证：

```bash
bash scripts/verify_all.sh
```

结果：

| 项 | 结果 |
|---|---|
| `gofmt` / `go vet` / `go build` | 通过 |
| `go test ./... -count=1` | 9 个包全部 `ok` |
| 顶层 PASS | 118 |
| 随机不变量子测试 PASS | 12 |
| 合计 PASS 行 | 130 |
| `-all` 场景 | 26/26 passed |
| `check_docs.py` | 通过 |
| `-race` | 未运行（`CGO_ENABLED=0`） |

本轮新增的文档内容：

- 19 个章节的深度小节（第 2、4、5、6、7、9、10、11、13、14、15、16、17、18、19、20、21、26、29 章）；
- 附录 Q（Kubernetes 调度基础）、R（错误信息索引）、S（25 道自测题）；
- README 第四轮修订说明与附录索引。

没有新增示例、插件、action 或场景，因此 `check_docs.py` 的期望计数保持不变。

## 15. 第五轮文档修订复核（2026-11-26）

第五轮新增第 33 章“从教程到上游：扩展开发与工程化”，附录顺延为第 34 章；同时补深第 12 章的 SLA 预算、TDM 日周期、节点侧混部隔离与机制选型。未改 MiniVolcano 代码、示例、插件、action 或场景。

```bash
bash scripts/verify_all.sh
```

结果：

| 项 | 结果 |
|---|---|
| `gofmt` / `go vet` / `go build` | 通过 |
| `go test ./... -count=1` | 9 个包全部 `ok` |
| 顶层 PASS | 118 |
| 随机不变量子测试 PASS | 12 |
| 合计 PASS 行 | 130 |
| `-all` 场景 | 26/26 passed |
| `check_docs.py` | 通过（34 章、29 个示例、26 个场景、23 个插件、6 个 action） |
| `-race` | 未运行（`CGO_ENABLED=0`） |

本轮文档变更：

- 新增第 33 章（33.1–33.12）：扩展层选择、仓库与构建、CRD/API 变更链、生产级插件与 Action、Extender/Kueue 边界、控制器/Webhook 证书、测试金字塔、feature gate、构建发布与 CVE、上游协作；
- 附录 `33-appendix.md` → `34-appendix.md`，H1 与 A–S 索引不变；
- 第 12 章新增 12.4.1、12.5.1、12.6.2、12.7，原 12.7–12.9 顺延为 12.8–12.10；
- README 的章节表、知识地图、目录结构、参考资料与 `check_docs.py` 的章节数同步为 34。

本轮没有新增示例、插件、action 或场景，因此 29/26/23/6/130 这些计数保持不变。真实 Kubernetes 集群、GPU/DRA、多集群、并发/race、Webhook/RBAC 仍未验证（见第 11 节）。

## 16. 第六轮文档修订复核（2026-11-26）

第六轮新增第 34 章“大模型工作负载：训练、推理与 Agent 的调度工程”，附录顺延为第 35 章；新增 5 个 LLM 示例，并补深五个工程主题。未改 MiniVolcano 代码、插件、action 或场景。

```bash
bash scripts/verify_all.sh
```

结果：

| 项 | 结果 |
|---|---|
| `gofmt` / `go vet` / `go build` | 通过 |
| `go test ./... -count=1` | 9 个包全部 `ok` |
| 顶层 PASS | 118 |
| 随机不变量子测试 PASS | 12 |
| 合计 PASS 行 | 130 |
| `-all` 场景 | 26/26 passed |
| `check_docs.py` | 通过（35 章、34 个示例、26 个场景、23 个插件、6 个 action） |
| `-race` | 未运行（`CGO_ENABLED=0`） |

本轮文档变更：

- 新增第 34 章（34.1–34.14）：三类负载、并行策略与 rank 布局、Pod 粒度、gang 尺寸、预训练/微调/RLHF、框架/operator 边界、checkpoint/弹性、推理三层调度、Agent、容量与排队模型、参考架构、20 条反模式；
- 新增 5 个示例：`queues/llm-queues.yaml`、`jobs/vcjob-llm-pretrain.yaml`、`jobs/vcjob-llm-finetune.yaml`、`jobs/vcjob-torchft-elastic.yaml`、`jobs/vcjob-vllm-serving.yaml`；示例 29→34（YAML 28→33）；
- 补深第 13.12（非 NVIDIA 加速器）、17.12（威胁模型）、18.10（热路径优化）、19.13（平台 API）、26.13（排队论）节；
- 附录 `34-appendix.md` → `35-appendix.md`，新增 L.7（LLM 配方）、D 的 LLM 术语、P 的两行覆盖、S 的 5 道自测题（25→30）；
- 第 1、4、6、12、14、25、29、33 章增加指向第 34 章的衔接；
- README 的章节表、知识地图、目录结构、参考资料与 `check_docs.py` 的章节/示例数同步为 35/34。

本轮新增的 LLM 示例只做了 YAML/JSON 结构检查，未在真实 GPU 集群 apply；训练脚本、vLLM 多机启动参数、NCCL 网卡名、PVC、HyperNode 数据都是占位。显存/算力/KV cache/排队公式是工程估算，不是 benchmark。非 NVIDIA 加速器的资源名与 HCCL/RCCL 行为未在真实硬件上验证。

## 17. 第七轮文档修订复核（2026-11-26）

第七轮不改变章节数（35）与 MiniVolcano 代码、插件、action、场景、测试计数；新增附录 T（综合调度轨迹）、U（容量/排队/ROI 计算器）、V（生产故障演练剧本），新增 `scripts/capacity_model.py`，补深第 34.10.6 节与第 32.2.2 节，并修复两处渲染问题。

```bash
bash scripts/verify_all.sh
```

结果：

| 项 | 结果 |
|---|---|
| `gofmt` / `go vet` / `go build` | 通过 |
| `go test ./... -count=1` | 9 个包全部 `ok` |
| 顶层 PASS | 118 |
| 随机不变量子测试 PASS | 12 |
| 合计 PASS 行 | 130 |
| `-all` 场景 | 26/26 passed |
| `check_docs.py` | 通过（35 章、附录 A–V 关键小节、34 示例、26 场景、23 插件、6 action） |
| `capacity_model.py --selftest` | `17 checks passed` |
| `-race` | 未运行（`CGO_ENABLED=0`） |

本轮新增的验证：

- `check_docs.py` 检查附录 T/U/V 的标题存在，并检查 `scripts/capacity_model.py` 的关键函数（`selftest`/`build_report`/`queue_plan`）。
- `verify_all.sh` 新增 `capacity model` 段，运行计算器自检；失败会使整体验证失败。
- 计算器自检用 17 个已知数值钉住公式：年 GPU 成本、有效 GPU-hour、单位成本、固定需求节省、固定集群增量、打平利用率、队列 deserved、训练 FLOPs/天数、KV cache、推理副本。

**仍然未验证**：

- 计算器的默认值/公式是工程估算，未用真实报价、真实 GPU 或生产负载校准；它不能替代 benchmark。
- 附录 T 是手工推演的概念轨迹，不是新的 `-scenario`；每个单点语义由现有场景覆盖，但整条轨迹没有一条命令直接复现。
- 附录 V 的七个演练剧本没有在真实集群执行；命令和根因来自前文的机制，不是一次实际事故记录。
- 附录 A.1 的 `hard\|soft` 与第 23 章表格只做了渲染修复，未改变任何 API 语义。

**结论**：第七轮新增的是文档、一个可运行计算器和一个自检，不改变 MiniVolcano 的语义边界；原有“未验证项”仍然有效。

## 18. 第八轮文档修订复核（2026-11-26）

第八轮不改变章节数（35）与 MiniVolcano 代码、插件、action、场景、测试计数（23/6/26/130），新增 2 个示例（34→36），并按 v1.16.0 源码补齐 API 语义与作业插件开发。

```bash
bash scripts/verify_all.sh
```

结果：

| 项 | 结果 |
|---|---|
| `gofmt` / `go vet` / `go build` | 通过 |
| `go test ./... -count=1` | 9 个包全部 `ok` |
| 顶层 PASS | 118 |
| 随机不变量子测试 PASS | 12 |
| 合计 PASS 行 | 130 |
| `-all` 场景 | 26/26 passed |
| `check_docs.py` | 通过（35 章、36 示例、26 场景、23 插件、6 action；新增 API/作业插件 token 检查） |
| `capacity_model.py --selftest` | `17 checks passed` |
| `-race` | 未运行（`CGO_ENABLED=0`） |

本轮文档/示例变更：

- 第 4 章新增 4.2.4：task 级 `minAvailable` → `minTaskMember`、`dependsOn`、task 级 `maxRetry`、`minSuccess`、`volumes`；
- 第 6 章新增 6.2.2：角色门槛的生效条件与 `subGroupPolicy` 的关系；
- 第 5 章新增 5.7.3：Queue `spec.affinity`、`strict`、`enablePreferredOrder`，原 5.7.3 顺延为 5.7.4；
- 第 9 章新增 9.7.1：`cdp` 冷却保护；
- 第 3 章新增 3.11：托管 Kubernetes 的五个差异，原 3.11 顺延为 3.12；
- 第 10 章新增 10.6.1：task `topologyPolicy`；
- 第 20 章新增 20.2.4：`extendClusters` 与 job-forwarding；
- 第 11 章重写 11.6：`subGroupPolicy` 与 `partitionPolicy` 的字段、约束与选型；
- 第 14 章新增 14.2.4：task `dependsOn` 与 JobFlow 的边界，原 14.2.4 顺延为 14.2.5；
- 第 33 章新增 33.6：作业插件开发，原 33.6–33.12 顺延为 33.7–33.13；
- 第 34 章 34.3.2 增加多角色作业的角色门槛；
- 修正 Queue 状态操作：删除不存在的 `spec.state`，改为 `vcctl queue operate` + `status.state`；修正 Job/PodGroup phase 混淆（`Inqueue` 属于 PodGroup）；修正 `minAvailable: 0` 的“关闭 gang”说法（上游 webhook 会补成总副本）；修正 NUMA 拓扑 CRD 名称（`Numatopology`/`numatopo`）；修正调度器 ConfigMap 键为 `volcano-scheduler.conf`；修正 plugin option 与 `arguments` 的混淆（`enablePreemptable`/`enableHierarchy`/`enableJobStarving`）；修正 action 名大小写（`gangpreempt`/`gangreclaim`）并标注 `lifecycle` 为发行版扩展；把指标名改为 v1.16 的 `volcano_schedule_attempts_total`/`volcano_e2e_scheduling_latency_milliseconds`/`volcano_eviction_transactions_total`/`volcano_queue_*`；修正 nodegroup `strict` 的语义（队列无 affinity 时任何节点都不落）；修正 lifecycle 事件/动作（`TerminateTask` 不存在，`JobUnknown` 应为 `Unknown`）；修正 `rescheduling` 的嵌套 `strategies` 结构、`usage.thresholds` 与 `deviceshare.*` 参数；`check_docs.py` 新增对 plugin option 错误位置与 rescheduling 扁平键的校验；澄清 dependsOn 与严格 gang 的死锁、PodGroup 命名 `<job>-<uid>`、`minSuccess` 的终止判定、`subGroupPolicy` 由 `partitionPolicy` 生成且 `minSubGroups=0` 不做 gang 门控；
- 附录 A/B/J/K/P 同步新增字段、插件、源码入口与排障项；
- 新增 `examples/jobs/vcjob-task-gang.yaml`、`examples/queues/queue-nodegroup-affinity.yaml`。

**独立复核**：两路只读复核（API 章节 + 作业插件/示例）发现并修复了以下问题：`dependsOn` 与严格 gang 的死锁、`minSuccess` 终止判定、`subGroupPolicy` 由 `partitionPolicy` 生成且 `minSubGroups=0` 不做门控、nodegroup `strict` 的“无 affinity 时任何节点都不落”、`enableJobStarving` 默认值、PodGroup 命名、作业插件 `OnJobDelete` 未清标记/未加 ownerReference/参数未解析，以及作业插件可通过 Pod spec 约束放置。示例与文档已同步；这些修复仍未在真实集群验证。

**仍然未验证**：

- 新示例只在 `check_docs.py` 中做结构/字段校验，未在真实 Kubernetes + Volcano v1.16 上 apply；`minTaskMember`、`dependsOn`、`minSuccess`、Queue affinity 的集群行为未实测。
- 本文的字段语义来自 v1.16.0 tag 源码阅读（`staging/src/volcano.sh/apis`、`pkg/scheduler/api/job_info.go`、`pkg/scheduler/plugins/{nodegroup,cdp}`、`pkg/controllers/job`），不是一次真实调度的取证。
- 第 33.6 节的作业插件示例是工程骨架，未在仓库中编译；接口签名以目标版本为准。

**结论**：第八轮是“API 语义补深 + 作业插件工程化”，不改变 MiniVolcano 的语义边界；原有“未验证项”仍然有效。

## 19. 第九轮代码修订复核（2026-11-26）

第九轮首次改变 MiniVolcano 的调度语义：从“只有作业级 gang”推进到“task 级角色门槛 + `minSuccess` 提前完成”。插件/action 数量不变（23/6），场景 26→28，测试 130→141（顶层 118→129），示例 36→37。

```bash
bash scripts/verify_all.sh
```

结果：

| 项 | 结果 |
|---|---|
| `gofmt` / `go vet` / `go build` | 通过 |
| `go test ./... -count=1` | 9 个包全部 `ok` |
| 顶层 PASS | 129 |
| 随机不变量子测试 PASS | 12 |
| 合计 PASS 行 | 141 |
| `-all` 场景 | 28/28 passed |
| `check_docs.py` | 通过（35 章、37 示例、28 场景、23 插件、6 action） |
| `capacity_model.py --selftest` | `17 checks passed` |
| `-race` | 未运行（`CGO_ENABLED=0`） |

本轮代码变更：

- `api.TaskSpec.MinAvailable`、`Job.MinTaskMember()`、`Job.SumTaskMinAvailable()`、`Job.RoleGatesActive()`、`JobSpec.MinSuccess`；
- `PodGroup.MinTaskMember` 与其深拷贝；
- `cache.ReadyCountByTask` / `RunningCountByTask`，`Statement.ReadyCountByTask`；
- `gang` 插件的 `JobValid/JobReady/JobStarving` 同时检查总数与角色，`TaskOrder` 优先调度门槛小/有门槛的角色；
- `MinResources` 先按角色门槛预留、再按声明顺序补足，避免准入预算被某个角色独占；
- `allocate` 分两轮：第一轮满足每个 active 角色门槛，第二轮填额外副本；`gangSatisfied` / `gangReason` 在 trial cache 上判定；
- `preempt`/`reclaim` 的提交条件复用 `gangSatisfied`，不再只看总数；`backfill` 改为每作业一个 Statement，角色门槛不满足不提交；
- `shuffle`/`rescheduling` 的受害者保护加入 `OccupyingCountByTask`，避免把角色最后一个占用 Pod 驱逐；
- `controller.taskGatesReady` 参与 Running 判定，`completeJob` 终止未启动副本；
- 新增场景 `task-gang`、`min-success`，新增测试 `TestGangTaskMinMemberReadiness`、`TestMutateJobDefaultsToTaskMinima`、`TestRoleGatesInactiveWhenJobMinBelowTaskSum`、`TestValidateMinSuccess`、`TestMinResourcesReservesRoleGatesFirst`、`TestGangDisableJobStarvingDoesNotRegister`、`TestAllocateSatisfiesAllRoleGatesBeforeExtraReplicas`、`TestBackfillDoesNotPartiallyCommitGang`、`TestPreemptRequiresRoleGates`、`TestRoleGatesBlockRunning`、`TestAllocateRollsBackWhenRoleUnavailable`。

**独立复核**：一轮只读对抗性审计（在临时副本上复现，不改仓库）发现并修复了 8 类问题：preempt/reclaim 提交不带角色门槛、backfill 逐 Pod 绕过 gang、单轮 TaskOrder 在“a1+b2”形态下活锁、`task-gang` 场景因名称排序而“假绿”、`enableJobStarving=false` 仍注册 hook、`MinResources` 按声明顺序高估准入预算、shuffle/rescheduling 受害者只保护总数、`ValidateQueue` 错误信息随 map 顺序变化。新增 5 个针对性测试（两轮分配、角色不可调度回滚、backfill、preempt、控制器 Running 门槛），并补 `TestMinResourcesReservesRoleGatesFirst`、`TestGangDisableJobStarvingDoesNotRegister`。审计同时确认 `minSuccess` 完成不泄漏配额、trial cache 判定正确、角色门槛生效条件与上游一致。

**语义边界**：

- 角色门槛只在 `job.minAvailable >= Σ task.minAvailable` 时生效；低于时与上游一样跳过，不拒绝作业。
- Mini 的 `minAvailable: 0` 仍表示“关闭 gang”；上游 webhook 会把 0 补成 Σ task 有效 minima，这是刻意的教学差异。
- `minSuccess` 只实现“达到阈值即 Completed 并终止未启动副本”，没有模拟“全部终止后仍不足 minAvailable 则 Failed”的第二阶段。
- `dependsOn`、作业级 `volumes`、Queue `spec.affinity`、`subGroupPolicy`/`partitionPolicy`、作业插件仍未实现。

**仍然未验证**：

- 新增的角色门槛/`minSuccess` 只在 MiniVolcano 的确定性模拟器上验证，未在真实 Volcano v1.16 集群 apply。
- 上游 `minTaskMember` 对省略/显式 0 的默认值、`dependsOn` 与 gang 的交互、`minSuccess` 的终止观测仍未做真实调度取证。
- `-race` 未运行；并发/多调度器下的角色门槛一致性未覆盖。

**结论**：第九轮是“MiniVolcano 语义补齐”，扩展了可运行实现的覆盖面，但仍是教学模型；原有“未验证项”继续有效。
