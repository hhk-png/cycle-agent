# 第 27 章 · 验证工程：如何证明一个调度器实现是对的

> 本章讨论的不是"怎么让测试通过"，而是"怎么知道测试通过意味着什么"。它适合两类读者：正在实现自己的调度器/配额控制器的人，以及要评审 Kueue 或 MiniKueue 行为是否可信的人。

## 27.1 为什么"测试全绿"不等于正确

一个控制面实现最容易制造的安全感是：

```text
gofmt 无输出
go vet 通过
go test ./... 全部 ok
覆盖率 80%+
```

这些指标只能证明"现有断言没有被违反"，不能证明"语义是对的"。原因有三类。

### 27.1.1 测试会编码 bug

如果断言是照着当前输出写的，那么错误行为也会被固化成"期望值"。例如：

- 只断言"第一次超时会驱逐"，没有断言"第几次超时应该停用"，就抓不到重试计数的 off-by-one；
- 只断言"重新预留后状态是 QuotaReserved"，没有断言 `retryCount` 仍然存在，就抓不到重试状态被清空；
- 只断言"回收后配额下降"，没有断言 topology assignment 也下降，就抓不到两套账本分叉。

这类测试不是"错"，而是**断言停在了一个不会暴露缺陷的粒度上**。

### 27.1.2 覆盖率的盲区

覆盖率统计的是"代码被执行过"，不是"状态组合被验证过"。以下路径经常有高覆盖率但没有有效断言：

- 错误分支只执行、不检查错误类型；
- 回滚路径执行了，但只检查最终状态，不检查中间账本；
- 多资源/多 Flavor 的组合只走了一条；
- 并发读路径执行了，但没有 race detector；
- map 迭代顺序导致的非确定性从未被重复运行暴露。

### 27.1.3 控制面的"正确"是多层性质

一个调度器至少要在四个层面正确：

| 层面 | 问题 | 典型验证 |
|---|---|---|
| 功能 | 这个场景能不能跑通 | 场景测试 |
| 语义 | 边界条件下行为是否符合文档 | 单元/边界测试 |
| 不变量 | 任何操作序列后账本是否自洽 | 随机化属性测试 |
| 工程 | 重启、并发、故障、升级后是否仍自洽 | 故障注入/差分/race |

"能跑通"只是第一层。第 14 章的场景测试主要覆盖第一层，第 20 章的不变量目录覆盖第二、三层，本章补第四层。

## 27.2 控制面测试的分层

把测试按"能抓什么/抓不到什么"分层，比按框架分层更有用。

| 层 | 典型形式 | 成本 | 能抓 | 抓不到 |
|---|---|---|---|---|
| 单元 | 纯函数、调度决策 | 低 | 排序、fit、limit、优先级 | 控制器状态机、账本漂移 |
| 不变量 | 随机操作后检查性质 | 低 | 超额、泄漏、重复计费、拓扑越界 | 具体决策是否符合业务预期 |
| 场景 | 固定输入/固定 tick | 中 | 生命周期、两阶段准入、抢占 | 未枚举的边界组合 |
| 差分 | 真实 Kueue vs 模拟器 | 高 | 语义偏差、字段默认值 | 两边都没有实现的行为 |
| 故障注入 | 停控制器、慢 informer、冲突 | 中高 | 恢复、幂等、回滚 | 真实网络/etcd 的所有故障 |
| 性能 | benchmark、压测 | 中 | 复杂度回归、分配 | 语义正确性 |
| Race | `-race`、重复/乱序 | 中 | 数据竞争、顺序依赖 | 业务逻辑错误 |
| 形式化 | TLA+/Alloy/模型检查 | 很高 | 死锁、循环、活性 | 实现与模型的一致性 |

对教学模拟器，最划算的组合是 **单元 + 不变量 + 场景**；对生产控制器，必须再加 **差分 + 故障注入 + race + 性能**。

## 27.3 不变量优先：比"期望输出"更稳的断言

不变量是"无论执行什么操作序列都必须成立的性质"。它不依赖具体输出，因此不会把 bug 固化成期望值。

### 27.3.1 一份可执行的不变量目录

第 20.9 节给出了完整目录。下面是最值得先实现的几组：

```text
配额
  Q1  usage >= 0
  Q2  无 Cohort：usage <= effective nominal
  Q3  显式 borrowingLimit：usage <= nominal + limit
  Q4  Cohort 总量 <= Σ CQ nominal + Σ Cohort shared
  Q5  Cohort borrowingLimit：Σ max(0, member usage - member nominal) <= limit
  Q6  Admission.Usage == Σ PodSet request × effective count
  Q7  DRA：同一逻辑资源取 max(extended, device)，不得重复累加

队列
  S1  StrictFIFO 下，不可放下的队头阻塞后续
  S2  BestEffortFIFO 下，后续可跳过队头
  S3  requeueAt 未到的 Workload 不参与调度
  S4  inadmissible 的 Workload 不阻塞可调度队列

抢占/公平
  P1  提交抢占后 preemptor 必须能放下
  P2  victim 集合最小
  P3  同优先级有全序，不能 A↔B 循环
  P4  被抢占者必须重排队或停用

生命周期
  L1  QuotaReserved/Admitted 的 Workload 必须在 Cache 中
  L2  Cache 中的 Workload 必须 QuotaReserved/Admitted
  L3  驱逐/完成后不得继续占用 Cache
  L4  active=false 不再重排队
  L5  admission gate 未清除时不占 Cache
  L6  Concurrent 迁移只向更小 Flavor 索引，累计执行时间不清零

TAS
  T1  每个 topology/level/domain 的 usage <= 聚合容量
  T2  Required 的 PodSet 在同一个 domain
  T3  Group 的 PodSet 共享 domain
  T4  抢占模拟移除 victim 后，拓扑容量正确恢复
  T5  slice 的 assignment.PodCount 之和 == EffectiveCount
  T6  Spreading 选择的 domain 当前用量不高于任何同样可行的 domain
```

### 27.3.2 把不变量写成 Go 函数

以 MiniKueue 的 `internal/scenario/invariant_test.go` 为例，核心不是"跑某个场景"，而是"每 tick 后检查账本"：

```go
func checkInvariants(t *testing.T, e *controller.Engine) {
    for _, cq := range e.Store.ListClusterQueues() {
        for _, group := range cq.Spec.ResourceGroups {
            for _, fq := range group.Flavors {
                for r, nominal := range fq.Nominal {
                    usage := e.Cache.FlavorUsage(cq.Name, fq.Name, r)
                    if usage < 0 {
                        t.Fatalf("negative usage")
                    }
                    if cq.Spec.CohortName == "" && usage > nominal {
                        t.Fatalf("non-cohort overuse")
                    }
                }
            }
        }
    }
    // ...
}
```

真正有价值的是那些"两套账本必须一致"的断言：

```go
// Admission.Usage 必须等于当前有效请求
if !sameResources(w.Status.Admission.Usage, w.TotalUsage()) {
    t.Fatalf("admission usage drifted")
}
// TAS assignment 的 Pod 数必须覆盖有效 Pod 数
for _, ps := range w.Spec.PodSets {
    if got := assignedPods[ps.Name]; got > 0 && got != w.EffectiveCount(ps) {
        t.Fatalf("topology assignment drifted")
    }
}
```

这两条断言直接抓到了本轮审计中的 TAS 回收缺陷：配额账本减了，拓扑账本没减。

### 27.3.3 断言"撤销路径"，不只断言"前进路径"

控制面的 bug 大多发生在状态回退时：

- 准入 → 抢占 → 重排队；
- 预留 → Retry → 重新预留；
- 运行 → 缩容回收 → 再准入；
- 迁移 → 重新准入；
- 停用 → 重新提交。

每条路径都要问："哪些状态必须保留？哪些必须清零？"例如：

| 路径 | 必须保留 | 必须清零/重建 |
|---|---|---|
| 抢占后重排队 | 累计执行时间、retryCount | 当前 Admission、Cache 占用 |
| AdmissionCheck Retry | retryCount | 配额预留、当前周期 |
| 动态回收 | 历史执行时间 | 被回收 Pod 的配额与拓扑占用 |
| Concurrent 迁移 | 累计执行时间 | 旧 Flavor 的预留、Pod 就绪状态 |
| 重新 Submit | 无（新生命周期） | 所有旧状态、Cache、回退计时 |

## 27.4 随机化与性质测试

### 27.4.1 随机化要覆盖"路径"，不只是"数量"

MiniKueue 的 `TestRandomizedStressPreservesQuotaInvariants` 生成：

- 3 个共享 Cohort 的 CQ；
- 80 个随机 Workload；
- 随机优先级、Pod 数、CPU/内存；
- 40 个 tick，随机完成作业；
- 每 tick 检查配额、Cohort、TAS 和 Cache 不变量（当前生成器没有 TAS 对象，TAS 部分为空；扩展生成器后才会真正覆盖）。

它的价值不在"随机数"，而在**随机操作序列**：提交、完成、抢占、借用交替发生，容易暴露账本泄漏。

扩展时要确保生成器能走到这些路径：

- 至少一个 CQ 会借用；
- 至少一次抢占；
- 至少一次 StrictFIFO 队头阻塞；
- 至少一个 TAS 作业被切片/打散；
- 至少一次 reclaim/migration/retry；
- 至少一个 inadmissible Workload。

否则随机测试会退化成"大量简单拒绝"的重复。

### 27.4.2 确定性

随机测试必须可复现：

```go
rng := rand.New(rand.NewSource(42))
```

并且所有排序都要有稳定的 tie-breaker（优先级 → 创建时间 → namespace/name）。否则同一 seed 在不同 Go 版本/map 迭代顺序下可能产生不同 trace。

### 27.4.3 缩小失败用例

Go 标准库没有内建 shrinking。一个实用做法是：

1. 失败时打印完整操作序列（提交顺序、tick、完成/驱逐）；
2. 用固定 seed 重跑；
3. 逐步减少 Workload 数/tick 数，直到最小复现；
4. 把最小复现固化成场景测试。

不要把"随机测试失败"直接改成"跳过"或"放宽断言"。

## 27.5 差分测试：和真实 Kueue 对拍

模拟器最大的风险是"自己和自己一致，但和真实语义不一致"。差分测试是解药。

### 27.5.1 对拍什么

不要比较时间戳、UID、事件顺序等实现细节，比较**决策和账本**：

| 维度 | 比较内容 |
|---|---|
| 准入 | 哪些 Workload 被 Admitted/QuotaReserved |
| Flavor | 每个 PodSet 分配到哪个 Flavor |
| 配额 | usage/nominal/borrow 的最终值 |
| 抢占 | 哪些 Workload 被驱逐、victim 数量 |
| 条件 | QuotaReserved/Admitted/Evicted 的 Reason |
| 拓扑 | TAS assignment 的 domain/PodCount |

### 27.5.2 一个最小对拍流程

```text
1. 固定 Kueue 版本和配置（gate、Configuration）
2. 用 kind 建集群，安装 Kueue
3. apply 同一组 CQ/LQ/Job YAML
4. 等系统稳定，导出 Workload 状态
5. 用相同 YAML 构造 MiniKueue 场景
6. 归一化（去掉时间戳/UID/事件文本）
7. diff 决策和账本
8. 对差异分类：已知近似 / 实现 bug / 文档 bug
```

### 27.5.3 已知差异要显式登记

MiniKueue 与真实 Kueue 的差异不是"都要修"，但要登记：

- 共享池按根树扁平聚合；
- Cohort lendingLimit 逐成员收紧；
- TAS group 视为硬约束、slice 固定最空优先；
- 没有 AlmostLCA 与逐层 Cohort 账本；
- ProvisioningRequest 与 Configurable Preemption 是 lite，不是完整语义；
- 已有 `BackoffConfig` 指数退避；固定 2 tick 只是抢占的默认延迟，不是唯一重排队策略。

把差异写进第 15 章的差距矩阵，差分测试只对"应该一致"的维度报警。否则你会陷入"每个差异都像 bug"的泥潭。

## 27.6 变形测试：改一个不影响语义的东西，结果必须不变

变形测试（metamorphic testing）不需要 oracle，只需要"变换前后应满足的关系"。

### 27.6.1 可用的变换

| 变换 | 期望 |
|---|---|
| 重命名 CQ/LQ/Workload | 决策不变（ID 只影响稳定 tie-breaker） |
| 调整提交顺序（优先级/创建时间相同） | 结果由稳定 tie-breaker 决定，不随 map 顺序变化 |
| 增加一个完全空闲的兄弟 CQ | 借用者的准入不应变差 |
| 增加一个不相关的 namespace | 现有队列的决策不变 |
| 把一个 Workload 拆成两个同资源 Workload | 总用量不变，准入行为可按定义不同 |
| 重复投递同一个事件 | 不产生重复 Workload/重复扣费 |
| 重放同一 tick | 不产生状态漂移 |
| 交换两个 Flavor 的声明顺序 | 只有 Flavor 偏好变化，配额边界不变 |

### 27.6.2 一个真实的例子

本轮审计中，Cohort borrowingLimit 的实现最初用：

```text
subtreeBorrow = max(0, Σ usage - Σ nominal)
```

如果 `org-a` 下 a1 空闲 4、a2 借用 3，净额是 0，a2 可以继续借，突破 Cohort limit。变形测试"增加一个空闲兄弟"会让结果变松，违反"空闲兄弟不应让借用者更容易突破上限"的直觉。修正后改成：

```text
subtreeBorrow = Σ max(0, usage_member - nominal_member)
```

并用 `TestCohortBorrowingLimitNotMaskedByUnderusedSibling` 固化。

## 27.7 故障注入与混沌

### 27.7.1 控制面常见故障

| 故障 | 真实表现 | 最小模拟 |
|---|---|---|
| webhook 不可用 | 创建/更新失败 | 停控制器、直接改 Store |
| 控制器重启 | 缓存重建、短时间不调度 | 新建 Engine、从 Store 重建 Cache |
| informer 延迟 | 状态更新滞后 | 先改 Store，晚几个 tick 再 Tick |
| API 冲突 | 更新 409、重试 | 并发提交/重复 Update |
| AdmissionCheck 卡住 | QuotaReserved 但不 Admitted | 不调用 ApproveCheck |
| Retry/Rejected | 释放/停用 | `ApproveCheckRetry`/`ApproveCheck` |
| worker 失联 | MultiKueue 副本残留 | 停止 worker Tick |
| Pod 部分就绪 | WaitForPodsReady 超时 | 只对部分 Workload 调 `MarkPodsReady` |
| 节点/设备故障 | TAS/DRA 放置失败 | 删除/缩小 domain 容量 |

### 27.7.2 幂等与重复事件

控制面必须能承受重复事件：

- 同一个 Workload 不能重复 `Cache.Add`；
- 同一个 AdmissionCheck 不能重复扣配额；
- 同一个 ProvisioningRequest 不能重复创建；
- 同一个 MultiKueue Workload 副本不能重复提交。

MiniKueue 用 key 去重、`Cache.Add` 重复检查、`Manager.Workload` 存在性检查来模拟。真实控制器还要处理 resourceVersion 冲突和 SSA 字段所有权。

### 27.7.3 恢复路径要断言"最终一致"

故障注入的断言不应只看"没 panic"，而要看：

- 控制器恢复后，Cache 与 Store 的预留状态一致；
- 已 Admitted 的 Workload 不被重新调度；
- 被驱逐的 Workload 正确重排队；
- 重复事件不产生重复对象；
- Inadmissible/Retry 的原因可解释。

## 27.8 并发、确定性与 flake

### 27.8.1 三条命令

```bash
go test ./... -count=10          # 重复运行，抓顺序依赖/全局状态
go test ./... -count=1 -shuffle=on  # 打乱测试顺序
CGO_ENABLED=1 go test -race ./...   # 需要 C 工具链
```

没有 C 工具链时，`-race` 会报 `-race requires cgo`，这是环境限制，不是代码通过。

### 27.8.2 确定性的敌人

- map 迭代顺序：所有对外可见的集合都要排序；
- 时间：不要断言墙钟时间，只用逻辑 tick；
- 并发：共享状态要么加锁，要么单线程化；
- 浮点：公平份额比较要有 epsilon，或统一整数化；
- 随机：固定 seed；
- 测试顺序：不要依赖包级全局变量。

MiniKueue 用整数资源、单 Engine 锁、离散 tick 和稳定排序来保证确定性；真实控制器则要靠 workqueue 单 key 串行、快照和乐观并发。

## 27.9 性能与回归

### 27.9.1 benchmark 的正确用法

- 比较同一台机器上的相对变化，不要跨机器比较绝对值；
- 同时看 `ns/op`、`B/op`、`allocs/op`；
- 固定已准入 Workload 数、Flavor 数、是否触发抢占；
- 用 `-count` 多次运行，记录中位数/最小值，忽略负载离群值；
- 性能回归门禁建议用"相对基线 + 阈值"，不要用固定微秒数。

### 27.9.2 复杂度检查

MiniKueue 的已知成本：

- `TryAdmit`：Flavor × 资源组 × 资源；
- Cohort 汇总：扫描成员；
- 抢占：扫描已准入 Workload；
- 每次 Tick 重建 View/队列；
- Cache 快照克隆所有已准入 Workload。

这些在 500 个 Workload 下是微秒级，但不能外推到生产。真实 Kueue 的成本还在 API watch、序列化、etcd、网络和并发上。

## 27.10 第一轮审计：十个被绿测试掩盖的缺陷

下面每一项都满足"测试全绿但行为错误"。它们不是靠读代码想出来的，而是靠"换一个断言角度"找出来的。

| # | 缺陷 | 症状 | 为什么旧测试没抓到 | 回归测试 |
|---|---|---|---|---|
| 1 | retryCount 被重新预留清空 | 每次 Retry 都显示 1 | 只断言释放前的 count | `TestRetryCountPersistsAcrossReservations` |
| 2 | TAS 回收只减配额不减拓扑 | domain 永久占用 | 只断言 Cache 用量 | `TestTASReclaimReleasesTopologyUsage` |
| 3 | WaitForPodsReady 重试计数复用 Requeues | 被抢占一次后一次超时就停用 | 只测一次超时 | `TestWaitForPodsReadyMaxRetriesCountsOnlyTimeouts` |
| 4 | MultiKueue Nominated 从未赋值 | 提名不可观测 | 只断言最终选中 | `TestAllAtOnceSelectsFirstAdmittingWorkerAndCleansLosers` |
| 5 | Cohort borrowingLimit 被空闲兄弟抵消 | 子树超额借用 | 没有空闲兄弟场景 | `TestCohortBorrowingLimitNotMaskedByUnderusedSibling` |
| 6 | Cache 保存准入前快照 | 抢占用旧状态覆盖 Store | 只查配额，不查状态 | `TestRefreshUpdatesStatusWithoutChangingUsageOrSequence` |
| 7 | 最大执行时间不跨抢占累计 | 作业反复被抢占但永不过期 | 只测单段准入 | 同上 |
| 8 | StrictFIFO 被 inadmissible 队头阻塞 | 合法作业永远排不上 | 只测配额不足的队头 | `TestStrictFIFOInadmissibleHeadDoesNotBlockQueue` |
| 9 | group + Unconstrained 语义未定义 | 共享 domain 约束不生效 | 没有非法组合测试 | `TestValidateWorkloadTopologyModes` |
| 10 | MatchLabels 缺失键等于空值 | 空值 selector 误匹配 | 只测存在的标签 | `TestLabelSelector` |

另外还有 5 个同类问题（幽灵 domain、DQO 漏报资源、重新 Submit 残留、回收回滚不一致、校验边界）记录在 [VERIFICATION.md](../VERIFICATION.md) 第 7 节。它们的共同点和下面四个案例一致。

### 27.10.1 案例一：retryCount 的生命周期归属

旧实现：

```go
func releaseReservation(...) {
    w.Status.AdmissionChecks = nil // 把 RetryCount 一起丢了
}
```

重新预留时再从 CQ 的检查列表重建，`RetryCount` 自然是 0。修复不是"在重建时 +1"，而是明确 **retryCount 属于 Workload，不属于某一次预留**：

- 释放时保留检查列表，状态改为 Pending；
- 重新预留时按名字继承 RetryCount；
- Retry 再次发生时递增。

教训：**状态的归属要写清楚**。凡是"跨预留周期"的字段，都不能跟着预留对象一起清空。

### 27.10.2 案例二：两套账本

TAS 有配额账本（FlavorAssignment）和拓扑账本（TopologyAssignment）。回收 Pod 时只减前者：

```go
fa.Resources[resource] -= delta
// 忘了 ta.PodCount / ta.Resources
```

结果配额释放了，domain 仍然显示被占用。修复是增加 `releaseTopologyPods`，从尾部 assignment 开始缩，PodCount 归零就删除。

教训：**每个"账本"都要有不变量**。只断言其中一个，另一个就会静默漂移。第 27.3 节的 T5 就是为此设计的。

### 27.10.3 案例三：快照、提交与状态覆盖

Cache 在预留时保存 Workload 的克隆，之后 `admitIfReady` 只更新 Store。于是 Cache 里的 `State` 还是 QuotaReserved、`AdmittedAt` 还是 0。抢占 victim 从 Cache 取出，`evict` 用它覆盖 Store，把最新的准入时间/累计执行时间/条件冲掉。

修复分两步：

1. `Cache.Refresh` 在准入后刷新状态快照，但保留 usage 和 sequence；
2. `evict` 按 key 重新读取 Store 的最新对象（并复制到调用者指针，保证后续写入不回退）。

教训：**"读快照做决策"和"写最新对象"必须分开**。快照可以旧，但写回不能旧。

### 27.10.4 案例四：净额与逐成员

Cohort limit 用净额计算时，空闲成员会抵消借用者：

```text
max(0, (usage_a1 + usage_a2) - (nominal_a1 + nominal_a2))
```

这不是"子树借用总量"，而是"子树净超额"。正确写法是逐成员取正借用再求和。教训：**聚合公式要能解释每个项的业务含义**，不能只看总量是否超。

### 27.10.5 其余缺陷的共同模式

- **只测主路径**：Retry 只测第一次，超时只测一次，selector 只测存在的标签；
- **只测一个账本**：配额 vs 拓扑；
- **只测最终状态**：不测中间状态和回滚；
- **把"能跑"当"正确"**：MultiKueue 选中了，但提名列表是空的；
- **共享计数器**：用一个 `Requeues` 承载所有原因；
- **非法组合没有校验**：group + Unconstrained 在代码里"碰巧"工作。

### 27.10.6 第二轮审计：跨层账本与对象隔离

第二轮审计换了角度：不再只看"某个场景的输出"，而是问"同一个语义是否被写进了两套账本，而测试只验证了其中一套"。这次又找到 15 类全绿缺陷，最有代表性的四组是：

1. **TAS 跨层级双计**：rack 级分配和 hostname 级分配各自维护账本，rack 占满后 hostname 仍能放。修复不是改某个 if，而是引入 `Scheduler.TopologyUsage`，把粗层分配下钻、细层分配上卷，并在同一个 Workload 的多个 PodSet 之间共享物理账本。
2. **回收漏掉自动资源**：`TotalUsage` 自动加 `pods`，但 `applyReclaim` 只扣 `PodSetPerPodUsage` 里的资源。测试只比较 `Admission.Usage` 与 `TotalUsage`，没比较 Cache 里的 `pods`。
3. **Store 返回活指针**：`GetClusterQueue` 返回内部指针，调用方改 nominal 会直接改 store；`SetLocalQueueStopPolicy` 甚至在验证失败前就改了对象。修复是所有 spec 类型克隆读写，`UpdateWorkload` 只允许改 `active`/`admissionGates`。
4. **迁移重置状态**：Concurrent Admission 迁移会重建 AdmissionChecks 为 Pending、清空 PodsReady，导致已准入作业被"降级"或被 WaitForPodsReady 误驱逐。修复是迁移后保留 Ready 检查和就绪状态。

其余包括：DRA 映射到空/未覆盖资源绕过计费、MultiKueue 重名 worker 的输家不清理、未知 dispatcher 静默广播、Topology 更新静默删域、空资源名、`Selected` 粘住、CLI 参数边界、`evict` 清空 retryCount。

共同模式可以总结成一句话：**凡是"两个地方都记了同一件事"，就要有不变量证明它们一致；否则测试只会验证你碰巧断言的那一边。** 具体缺陷和回归测试见 [VERIFICATION.md](../VERIFICATION.md) 第 7.1 节。

## 27.11 验收清单与 CI 门禁

### 27.11.1 每次提交

```bash
cd mini-kueue
gofmt -l .                         # 必须无输出
go vet ./...
go test ./... -count=1
```

### 27.11.2 合并前

```bash
go test ./... -count=10
go test ./... -count=1 -shuffle=on
go test ./internal/scheduler -bench BenchmarkTryAdmit -benchmem -run '^$'
cd ..
python scripts/verify_docs.py
bash scripts/verify_all.sh
```

### 27.11.3 Nightly

```bash
# 有 C 工具链时
CGO_ENABLED=1 go test -race ./...

# 随机不变量多轮
go test ./internal/scenario -run TestRandomizedStress -count=50

# 所有场景多轮
for i in 1 2 3; do
  for s in $(go run ./cmd/minikueue -list | awk '{print $1}'); do
    go run ./cmd/minikueue -scenario "$s" -ticks 10 >/dev/null
  done
done
```

### 27.11.4 上线前（真实 Kueue）

- [ ] 目标版本 release notes / gate 默认值已读；
- [ ] staging 做过 CRD round-trip；
- [ ] 准入、借用、回收、抢占、AdmissionCheck 全生命周期跑通；
- [ ] 控制器重启/leader 切换后队列恢复；
- [ ] webhook 证书和可用性有监控；
- [ ] 关键不变量有指标/告警；
- [ ] 回滚清单和演练完成。

## 27.12 真实 Kueue 的测试栈

MiniKueue 的测试是"模型内"的。要改真实 Kueue、写 Job 集成或 AdmissionCheck 控制器，需要另一套分层：

| 层 | 工具 | 测什么 | 常见入口（以仓库 Makefile 为准） |
|---|---|---|---|
| 单元 | `go test` | scheduler/cache/webhook/控制器的纯逻辑 | `go test ./pkg/...`、`make test` |
| 集成 | envtest（真实 kube-apiserver + etcd） | CRD schema、默认值、webhook、reconcile 与真实 API 的交互 | `make test-integration` |
| 端到端 | kind + ginkgo | Job → Workload → Pod → 完成/驱逐的完整链路 | `make test-e2e` |
| 升级 | 双版本 CRD + conversion | v1beta1 ↔ v1beta2 转换、`storedVersions` | 版本升级 job / 手工 round-trip |
| 性能 | benchmark + 集群压测 | 调度延迟、吞吐、API 压力 | 见第 12 章 |

envtest 的最小用法：

```bash
# 安装/定位 envtest 二进制（目标名以仓库为准）
make envtest
export KUBEBUILDER_ASSETS="$(bin/setup-envtest use -p path)"

# 跑集成测试；测试进程会启动真实的 apiserver/etcd
go test ./test/integration/... -count=1
```

写集成测试时，优先覆盖这些"静态检查抓不到"的行为：

1. **幂等**：同一个 Job 触发两次 reconcile，只应有一个 Workload，且冻结字段不变；
2. **默认化**：默认 LocalQueue/默认 WorkloadPriorityClass 是否在 mutating 阶段补上标签，VAP 是否随后通过；
3. **暂停/放行**：Workload 未 Admitted 时 Job 必须 suspended，Admitted 后必须放行；
4. **驱逐/完成**：Evicted 后暂停并重排队，Finished 后释放配额且不再重建；
5. **finalizer**：删除 Job/Workload 时不会卡住，也不会留下孤儿对象；
6. **conversion**：v1beta1 对象读成 v1beta2 再写回，字段不丢；
7. **webhook 失败**：`failurePolicy: Fail` 时非法对象被拒绝，`Ignore` 时的行为有明确测试。

给新 Job 框架写 conformance 测试时，最小闭环是：

```text
创建框架对象
  → 断言 Workload 存在且 queueName 正确
  → 断言框架对象被暂停
  → 手工/控制器让 Workload Admitted
  → 断言框架对象被放行、Pod 被创建
  → 标记完成
  → 断言 Workload Finished、配额释放、对象被清理
```

不要只测"happy path 能创建 Workload"；真正的集成缺陷几乎都在暂停/放行、驱逐、完成和清理这四个边界上。

## 27.13 未来：从测试到证据

更高阶的验证手段：

1. **形式化不变量**：用 TLA+ 描述"配额预留不会超额""抢占不会循环""迁移必然终止"，再用模型检查器枚举小规模状态空间。
2. **实现与模型对拍**：把 MiniKueue 的决策序列导出，和 TLA+ 模型的 trace 比较。
3. **真实负载回放**：用生产 Workload 的到达时间、资源、完成时间做 replay，而不是随机数。
4. **差分 fuzzing**：随机生成 CQ/Workload，喂给真实 Kueue 和模拟器，自动分类差异。
5. **Mutation testing**：故意改坏一行（例如把 `>=` 改成 `>`），检查测试是否能杀死突变；杀不死的测试就是盲区。
6. **故障注入框架**：在 controller-runtime 层注入 API 延迟、冲突、watch 断流，验证恢复逻辑。

这些手段的成本很高，但它们的输出是"证据"而不是"感觉"：你能说清楚"这个实现在哪些性质上被验证过，在哪些边界上仍是近似"。

## 27.14 本章小结

- 绿测试只证明"现有断言没有被违反"，不证明语义正确；断言粒度决定了测试能抓到什么。
- 控制面验证要分层：单元、不变量、场景、差分、故障注入、race、性能。
- 不变量优先：配额、队列、抢占、生命周期、TAS 的一致性比单次输出更稳。
- 随机化要覆盖路径而不是堆数量；固定 seed，保留最小复现。
- 差分测试和真实 Kueue 对拍决策与账本，已知近似要登记，不要把所有差异都当 bug。
- 变形测试用"改一个不该影响结果的东西"来暴露聚合公式和顺序依赖。
- 故障注入要断言最终一致，不只是"没 panic"。
- 第一轮审计的十个缺陷有共同模式：只测主路径、只测一个账本、只测最终状态、复用计数器、非法组合缺校验。
- 第二轮审计把重点转向"同一语义写进两套账本"：TAS 跨层、配额 vs `pods`、Workload vs Store、名称 vs 指针；修复后补了对应的跨层/隔离/迁移回归测试。
- 每次提交跑基础命令，合并前跑重复/乱序/基准，nightly 跑 race 和随机不变量；上线前做真实集群验收。
- 改真实 Kueue/写扩展时，用 envtest 覆盖默认化、幂等、暂停/放行、驱逐/完成和 conversion；这些是静态检查和纯单测抓不到的边界。
- 更高阶的方向是形式化、模型检查、trace 回放、差分 fuzzing 和 mutation testing。

---

[← 上一章：端到端案例](26-end-to-end-case-study.md) | [返回目录](../README.md) | [下一章：物理落地 →](28-physical-scheduling-layer.md)
