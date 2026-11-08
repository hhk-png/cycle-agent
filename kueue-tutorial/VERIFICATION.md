# 验证记录

验证日期：2026-11-08

环境：

- Windows 11 Home China 10.0.26200
- Go 1.27.0 windows/amd64
- CPU：Intel Core Ultra 9 285H
- Python 3.14 + PyYAML（仅用于示例 YAML 解析）

## 1. 代码质量

```bash
cd mini-kueue
gofmt -l .
go vet ./...
go build ./...
go test ./... -count=1
```

结果：

- `gofmt -l .`：无输出（所有 Go 文件已格式化）。
- `go vet ./...`：通过，无告警。
- `go build ./...`：通过。
- `go test ./... -count=1`：所有包通过。

```text
?   	example.com/minikueue/cmd/minikueue	[no test files]
ok  	example.com/minikueue/internal/api
ok  	example.com/minikueue/internal/cache
ok  	example.com/minikueue/internal/controller
ok  	example.com/minikueue/internal/metrics
ok  	example.com/minikueue/internal/multikueue
ok  	example.com/minikueue/internal/queue
ok  	example.com/minikueue/internal/scenario
ok  	example.com/minikueue/internal/scheduler
ok  	example.com/minikueue/internal/store
```

## 2. 测试数量

```bash
go test ./... -count=1 -v | grep -c '^--- PASS'
# 145 个顶层测试

go test ./internal/scenario -count=1 -v | grep -c '^    --- PASS'
# 26 个场景子测试
```

合计 `171` 个 PASS（145 顶层 + 26 场景子测试，沿用上一版的计数口径）。如果按 `--- PASS` 全量统计，还包含 scheduler selector 12 个子测试和 multikueue 4 个子测试；不同口径不要混用。

本轮新增的回归测试（用于固定对抗性审计发现）：

- `TestRetryCountPersistsAcrossReservations`
- `TestWaitForPodsReadyMaxRetriesCountsOnlyTimeouts`
- `TestTASReclaimReleasesTopologyUsage`
- `TestInadmissibleClearedWhenSelectorMatches`
- `TestStrictFIFOInadmissibleHeadDoesNotBlockQueue`
- `TestMaximumExecutionTimeIsCumulativeAcrossPreemption`
- `TestConcurrentMigrationRespectsQueueHold`
- `TestDynamicQuotaMissingResourceIsZero`
- `TestSubmitResetsStaleLifecycle`
- `TestReclaimFinishedWorkloadRejected`
- `TestCohortBorrowingLimitNotMaskedByUnderusedSibling`
- `TestTopologyUnconstrainedIgnoresUnknownUsageDomain`
- `TestTopologyGroupSharesDomain`（改用 Preferred + Group 的合法组合）
- `TestRefreshUpdatesStatusWithoutChangingUsageOrSequence`
- `TestConcurrentAdmissionValidatesLastAcceptableFlavor`
- `TestValidateFairSharingWeightsRejectsNonFinite`
- `TestValidateClusterQueueAdmissionChecks`
- `TestValidateWorkloadTopologyModes`
- `TestValidateWorkloadRejectsNegativeExecutionAndDuplicateGates`
- `TestAllAtOnceSelectsFirstAdmittingWorkerAndCleansLosers`（新增 Nominated 断言）
- `TestIncrementalExpandsUntilAdmission`（新增 Nominated 断言）

第二轮审计新增的回归测试：

- `TestTopologyRackReservationBlocksHostnamePlacement`
- `TestTopologyMixedLevelsInsideOneWorkload`
- `TestReclaimReleasesPodsQuota`
- `TestSpecObjectsAreCloned`
- `TestUpdateWorkloadPreservesUIDAndSpec`
- `TestUpsertTopologyRejectsIncompatibleLevels`
- `TestUpsertWorkloadRejectsMissingQueue`
- `TestValidateReferencesChecksFlavorTopology`
- `TestDeviceMappingValidation`
- `TestDeviceMappingToPodsDoesNotDoubleCount`
- `TestValidateRejectsEmptyResourceNames`
- `TestDuplicateWorkerNamesStillCleanUpLosers`
- `TestUnknownDispatcherDoesNotBroadcast`
- `TestDeletedSelectedCopyIsRedispatched`
- `TestConcurrentMigrationPreservesChecksAndReadiness`
- `TestRetryCountSurvivesPreemption`
- `TestCohortSharedPool`

本轮实现补全新增的测试：

- `TestResourceTransformationsMatchHamiExample`：`multiplyBy` 资源名、`Replace`、空 outputs 忽略；
- `TestResourceTransformationsRetainAndFixedMultiplier`：`Retain` 与固定倍率；
- `TestResourceTransformationMissingMultiplierContributesZero`：倍率资源缺失时输出 0；
- `TestValidateResourceTransformationsRejectsReservedName`：拒绝 `pods` 与重复 input；
- `TestValidatePreemptionConfig`：trigger/scope/comparison/numeric label 校验；
- `TestConfigurablePreemptionSelectorMatching`：五种 scope、priority、Boosted、numeric label、Workload/CQ selector；
- `TestQuotaFeasibleButTopologyInfeasible`：quota 够但拓扑放不下，验证 trigger 判定所依赖的两种分配；
- `TestConfigurablePreemptionAuthorizesEviction`：Classic 全关时规则授权抢占；
- `TestFairSharingPreemptionReclaimsBorrowedQuota`：Fair Sharing 收回借用；
- `TestFairShareCohortWeight`：Cohort 权重进入 DRS；
- `TestProvisioningRequestLiteRetriesThenAdmits`：5 tick 置备/重试时间线；
- `TestExponentialBackoffOverridesFixedPreemptionDelay`：Backoff 覆盖固定抢占延迟。

本版 MultiKueue failover 新增的回归测试：

- `TestDownWorkerIsNotNominated`：`Down` worker 不进入提名、不接收副本、状态显示 Down；
- `TestFailoverAfterWorkerLostTimeout`：超时前保持选中，超时后切换，恢复后清理陈旧副本；
- `TestNoAvailableWorker`：所有 worker 都 Down 时产生 `NoAvailableWorker`；
- `TestMultiKueueFailoverScenario`：场景级验证 worker-1 初始 Down → worker-2 选中 → 切换 worker-1。

不变量测试也同步加强：

- Admission.Usage 必须等于 Workload 的 TotalUsage；
- 回收后 Cache 的 `pods` 计数必须与 Admission.Usage 一致；
- TAS assignment 覆盖的 Pod 数必须等于有效 Pod 数；
- TAS 跨层账本：rack 级占用必须反映到 hostname 子域，反之亦然（`Scheduler.TopologyUsage`）；
- Cohort 级 borrowingLimit 必须按每个成员的正借用求和，不能被空闲成员抵消。

## 3. 覆盖率

```bash
go test ./... -cover -count=1
```

最终结果：

| 包 | 覆盖率 |
|---|---:|
| `internal/api` | 52.6% |
| `internal/cache` | 72.9% |
| `internal/controller` | 80.8% |
| `internal/metrics` | 100.0% |
| `internal/multikueue` | 90.9% |
| `internal/queue` | 82.5% |
| `internal/scenario` | 94.1% |
| `internal/scheduler` | 73.8% |
| `internal/store` | 59.4% |
| `cmd/minikueue` | 0.0%（无单元测试） |

说明：`store` 的许多路径由 controller/scenario 集成测试间接覆盖，但 `go test -cover` 只统计本包测试；覆盖率不是本项目的唯一目标。`controller` 的轻微下降来自新增的 `Explain` 分支（只测主路径与控制器层覆盖）；`scheduler` 因新增 `evaluateFlavor`/`Explain` 测试略有上升。没有为了数字去堆无意义断言。

## 4. 基准

```bash
go test ./internal/scheduler -bench BenchmarkTryAdmit -benchmem -run '^$' -count=3
```

结果：

```text
goos: windows
goarch: amd64
pkg: example.com/minikueue/internal/scheduler
cpu: Intel(R) Core(TM) Ultra 9 285H
BenchmarkTryAdmit-16    	   67629	     15215 ns/op	     880 B/op	       8 allocs/op
BenchmarkTryAdmit-16    	   76164	     16823 ns/op	     880 B/op	       8 allocs/op
BenchmarkTryAdmit-16    	   37468	     29763 ns/op	     880 B/op	       8 allocs/op
PASS
```

条件：1 个 CQ、500 个已准入 Workload、不触发抢占。本轮数值在 ~15–30 µs/op 之间，分配次数稳定在 880 B/op、8 allocs/op；机器负载高时会出现更高离群值。新增的 Configurable/Fair Sharing 代码只在直接准入失败后执行，不改变直接准入路径的分配次数。

## 4.1 稳定性

```bash
go test ./... -count=10
go test ./... -count=1 -shuffle=on
```

结果：均通过。测试不依赖执行顺序，重复运行未出现 flake。

## 5. 场景

```bash
go run ./cmd/minikueue -list
# 28 行（26 个引擎场景 + multikueue + multikueue-failover）

for s in $(go run ./cmd/minikueue -list | awk '{print $1}'); do
  go run ./cmd/minikueue -scenario "$s" -ticks 5 > /dev/null
  echo "OK $s"
done
```

结果：

```text
OK basic
OK cohort-borrow
OK preemption
OK configurable-preemption
OK fair-sharing-preemption
OK reclaim
OK admission-check
OK provisioning-request
OK strict-fifo
OK best-effort-fifo
OK tas
OK pods-ready
OK dynamic-reclaim
OK dynamic-quota
OK label-selector
OK localqueue-hold
OK retry-backoff
OK dra
OK resource-transform
OK tas-slice
OK admission-fair-sharing
OK concurrent-admission
OK admission-gate
OK hierarchical-limits
OK implicit-cohort
OK cohort-shared-pool
OK multikueue
OK multikueue-failover
```

关键场景的验证点：

- `dra`：两个各 4 卡的 Job 在 6 卡配额下，第一个 Admitted、第二个 Pending；GPU 用量为 4。
- `tas-slice`：`one-pod` 落在 h1，`spread-two` 选择用量最少的 h2，`slice-four` 切成 h3/h4 两条 assignment。
- `admission-fair-sharing`：`heavy` 先积累历史，`light` 历史更低，下一轮 `light-next` 先准入。
- `concurrent-admission`：tick 1 落在 spot；提高 reserved 配额后 tick 2 迁移到 reserved。
- `admission-gate`：tick 1 `AdmissionGated=True` 且不占配额；清除两个 gate 后 tick 2 Admitted。
- `hierarchical-limits`：`within-limit` 6 CPU（借 2）Admitted；`over-limit` 7 CPU 因 org-a 的 Cohort 级 borrowingLimit=2 Pending。
- `implicit-cohort`：只有 `cohortName`、没有 Cohort 对象时，6 CPU 作业仍能从 4 CPU nominal 借到 2。
- `cohort-shared-pool`：两个成员 CQ 的 nominal 都是 0，第一个 3 CPU 作业从显式 Cohort 的 4 CPU 共享池借到 3；第二个 3 CPU 作业使总量超过共享池并 Pending。
- `multikueue`：worker-2 先 Admitted，worker-1 的副本被删除；`Manager.Nominated` 记录两个 worker。
- `multikueue-failover`：worker-1 初始 Down，worker-2 选中；worker-2 失联后第 1 tick 等待、第 2 tick 超过 timeout 切换到 worker-1；worker-2 恢复后清理陈旧副本。
- `resource-transform`：每个 Pod 的 `gpu=2, gpumem=1024, gpucores=20` 折算为 `total-gpumem=2048, total-gpucores=40`；前两个 Admitted，第三个 Pending。
- `provisioning-request`：tick 1 第一次置备失败并释放配额，tick 3 重新预留，tick 5 Admitted。
- `configurable-preemption`：Classic Preemption 全关，`PreemptionConfig` 规则允许 hero 驱逐带 `workload-type=batch` 的低优先级作业。
- `fair-sharing-preemption`：`team-a` 借用 2 CPU 后，空闲的 `team-b` 通过 Fair Sharing 收回容量。

## 6. 随机不变量测试

```bash
go test ./internal/scenario -run TestRandomizedStressPreservesQuotaInvariants -count=20
```

结果：通过。

该测试随机生成 80 个 Workload、3 个共享 Cohort 的 CQ、40 个 tick，并随机完成作业；每个 tick 检查：

- usage 非负；
- 无 Cohort 时不超过 effective nominal；
- 显式 borrowingLimit 不被突破；
- Cohort 树总 usage 不超过总 nominal；
- Cohort 级 borrowingLimit 按每个成员的正借用求和，不被空闲成员抵消；
- TAS domain/level 的 usage 不超过聚合容量（通过 `Scheduler.TopologyUsage` 做跨层检查）；
- TAS assignment 的 Pod 数等于有效 Pod 数；
- Admission.Usage 等于 TotalUsage；
- Cache 与 Store 的预留状态一致。

## 7. 对抗性审计：绿测试掩盖的缺陷

本轮对 MiniKueue 做了一次独立的对抗性审计。以下缺陷在修复前都是"`go test ./...` 全绿"的状态；除特别说明外，每一条都有对应的回归测试。

| # | 缺陷 | 症状 | 根因 | 修复 |
|---|---|---|---|---|
| 1 | retryCount 跨重新预留丢失 | 每次 Retry 都显示 1 | `releaseReservation` 清空 `AdmissionChecks`，重建时从 0 开始 | 释放时保留检查列表；重新预留按名字继承 retryCount |
| 2 | TAS 回收只释放配额 | domain 永久占用 | `applyReclaim` 只减 FlavorAssignment | 新增 `releaseTopologyPods`，从尾部缩 assignment |
| 3 | WaitForPodsReady 重试计数错误 | 被抢占一次后一次超时就停用 | 复用 `Requeues`（含抢占），且 off-by-one | 按 `PodsReadyTimeout` 的 eviction stats 计数 |
| 4 | MultiKueue 提名不可观测 | `Nominated` 永远为空 | `nominate()` 返回结果未写回 Manager | Tick 时写入 `Nominated` |
| 5 | Cohort borrowingLimit 被空闲成员抵消 | 子树超额借用 | 用 `Σusage - Σnominal` 净额 | 改为 `Σ max(0, usage_member - nominal_member)` |
| 6 | Cache 保存准入前快照 | 抢占用旧状态覆盖 Store | `Cache.Add` 在 `admitIfReady` 之前，快照 `AdmittedAt=0` | 新增 `Cache.Refresh`；`evict` 重新读取 Store |
| 7 | 最大执行时间不跨抢占累计 | 反复被抢占但永不过期 | 只有一个 `AdmittedAt`，重新准入即重置 | 新增 `ExecutionTicks`，迁移/抢占只结束当前周期 |
| 8 | StrictFIFO 被 inadmissible 队头阻塞 | 合法作业永远排不上 | namespace 不匹配时 `break` | inadmissible 移出队头语义，改为 `continue` |
| 9 | group + Unconstrained 语义未定义 | 共享 domain 约束不生效 | 校验允许非法组合 | 校验要求 group 搭配 Required/Preferred |
| 10 | MatchLabels 缺失键等于空值 | 空值 selector 误匹配 | `labels[k] != v` 对缺失键返回 "" | 要求键存在 |
| 11 | TAS unconstrained 可落到无容量 domain | 幽灵 domain 被放置 | `orderedDomains` 合并了 usage 中已不存在的 domain | 无 capacity 的 domain 直接跳过 |
| 12 | DQO 漏报资源回退静态配额 | provider 不报 memory 时仍按旧值 | `nominal()` 对缺失资源回退 spec | provider 已报告 Flavor 的缺失资源按 0 |
| 13 | 重新 Submit 保留旧生命周期 | 旧 RequeueAt/Evictions/Cache 残留 | Submit 未清理旧状态 | 清理 Cache、计数、条件、ExecutionTicks |
| 14 | 回收失败回滚不一致 | 失败后状态说回收了、账本没回收 | 回滚只重加 Cache，未恢复 ReclaimablePods | 保存旧 Admission/ReclaimablePods 并恢复；该路径需要构造 Store/Cache 不一致，暂无独立单元测试，由不变量测试兜底 |
| 15 | 校验边界缺失 | 非法输入静默生效 | LastAcceptableFlavorName、重复 AdmissionCheck、NaN 权重、负执行时间、重复 gate 未校验 | 补齐校验与测试 |

审计方法、每个缺陷的"为什么旧测试没抓到"以及可复用的验证流程见[第 27 章](docs/27-verification-engineering.md)。

## 7.1 第二轮对抗性审计（本次修复）

在第一轮之后又做了一轮独立审计，重点放在"跨层账本"和"对象隔离"。以下 15 类缺陷同样在修复前 `go test ./...` 全绿：

| # | 缺陷 | 症状 | 根因 | 修复/回归测试 |
|---|---|---|---|---|
| 1 | TAS 跨层级双计 | rack 作业占满后，hostname 作业仍能放进去 | 每个 level 只读自己的账本 | `Scheduler.TopologyUsage` 把粗层分配下钻、细层分配上卷；`TestTopologyRackReservationBlocksHostnamePlacement`、`TestTopologyMixedLevelsInsideOneWorkload` |
| 2 | 回收不释放 `pods` | CPU/GPU 降了，pod 计数仍占满 | `PodSetPerPodUsage` 不含自动 `pods` | `applyReclaim` 显式扣 `pods`；`TestReclaimReleasesPodsQuota` |
| 3 | Store 返回活指针 | Get 后改 nominal，store 被改 | spec 对象未克隆 | 所有 spec 类型加 `Clone`，Store 读写都克隆；`TestSpecObjectsAreCloned` |
| 4 | Store 不校验 workload 引用 | 缺 LQ/CQ/priority/DRA 映射也能写入 | `UpsertWorkload` 只做结构校验 | 写入前 `ValidateWorkloadRefs`；`TestUpsertWorkloadRejectsMissingQueue`、`TestValidateReferencesChecksFlavorTopology` |
| 5 | DRA 映射到空/未覆盖资源 | 设备请求不计费，配额被绕过 | 只检查 key 存在 | CQ/Workload 两侧校验目标非空、非 `pods`、被 CQ 覆盖；`TestDeviceMappingValidation`、`TestDeviceMappingToPodsDoesNotDoubleCount` |
| 6 | MultiKueue 重名 worker | 输家副本不清理 | 用 name 比较 winner | 用 `*Worker` 指针选主/清理；`TestDuplicateWorkerNamesStillCleanUpLosers` |
| 7 | 未知 dispatcher 静默广播 | typo 被当成 AllAtOnce | `default` 分支广播 | 只接受 AllAtOnce/Incremental；`TestUnknownDispatcherDoesNotBroadcast` |
| 8 | `UpdateWorkload` 可改 UID/spec | 已创建对象被改 pod 数 | Update 整对象替换 | 保留 UID/spec，仅允许 `active`/`admissionGates`；`TestUpdateWorkloadPreservesUIDAndSpec` |
| 9 | Topology 更新静默删域 | 改 levels 后叶子域消失 | 更新时删除不兼容域 | 拒绝不兼容更新并回滚；`TestUpsertTopologyRejectsIncompatibleLevels` |
| 10 | 空资源名被接受 | `Requests{"":...}`、provider 空 key | 缺少校验 | 校验空 resource key；`TestValidateRejectsEmptyResourceNames` |
| 11 | MultiKueue `Selected` 粘住 | 选中副本被删后永远显示旧状态 | 只检查字符串 | 重新检查副本，缺失则重新分发；`TestDeletedSelectedCopyIsRedispatched` |
| 12 | CLI 边界 | `-ticks=-2` 静默退出；multikueue 忽略 `-query`；`-list` 吞错误 | 缺少参数校验 | `main.go` 校验 ticks、提示 query、处理 list 错误 |
| 13 | Concurrent 迁移重置 AdmissionCheck | 已准入作业迁移后回到 QuotaReserved | `reserveWith` 重建 Pending checks | 迁移后保留 Ready 并重新 Admitted；`TestConcurrentMigrationPreservesChecksAndReadiness` |
| 14 | 迁移清空 PodsReady | 迁移后被 WaitForPodsReady 误驱逐 | `reserveWith` 重置 readiness | 迁移保留 PodsReady/PodsReadyAt；同上 |
| 15 | `evict` 清空 retryCount | 抢占/排空后重试计数归零 | evict 置 `AdmissionChecks=nil` | 保留检查列表并置 Pending；`TestRetryCountSurvivesPreemption` |

这轮的共同模式是：**一个语义同时写在两套账本里**（配额 vs 拓扑、Workload vs Store、名称 vs 指针），而旧测试只验证其中一套。第 27 章把这些模式整理成了可复用的检查清单。

## 8. 文档与示例校验

```bash
python scripts/verify_docs.py
```

结果：

```text
OK: 36 chapters, 19 YAML files, links/fences valid
```

YAML 文件：

```text
examples/kueue/admission-check-provisioning.yaml: 6 document(s) OK
examples/kueue/admission-fair-sharing.yaml: 6 document(s) OK
examples/kueue/admission-gate.yaml: 4 document(s) OK
examples/kueue/cohort.yaml: 10 document(s) OK
examples/kueue/concurrent-admission.yaml: 5 document(s) OK
examples/kueue/fair-sharing.yaml: 16 document(s) OK
examples/kueue/hierarchical-cohort.yaml: 8 document(s) OK
examples/kueue/preemption.yaml: 7 document(s) OK
examples/kueue/quick-start-job.yaml: 1 document(s) OK
examples/kueue/quick-start-setup.yaml: 3 document(s) OK
examples/kueue/topology-aware.yaml: 5 document(s) OK
examples/kueue/workload-priority.yaml: 7 document(s) OK
examples/kueue/resource-transformations.yaml: 5 document(s) OK
examples/kueue/provisioning-request.yaml: 5 document(s) OK
examples/kueue/configurable-preemption.yaml: 7 document(s) OK
examples/kueue/fair-sharing-preemption.yaml: 7 document(s) OK
examples/kueue/multikueue.yaml: 10 document(s) OK
examples/kueue/custom-admission-check.yaml: 6 document(s) OK
examples/kueue/inference-workloads.yaml: 7 document(s) OK
```

校验内容：

- 38 个章节文件存在；
- README/章节中的本地 Markdown 链接存在；
- 代码围栏配对；
- 所有示例 YAML 可被 PyYAML 解析。

## 9. 未执行的验证

- **`go test -race`**：本机 Go 报错 `-race requires cgo`，未启用 CGO/缺少 C 编译器，因此没有运行 race detector。代码中的并发保护是 `sync.RWMutex`/`sync.Mutex`；如需严格验证，请在安装 C 工具链后执行 `CGO_ENABLED=1 go test -race ./...`。
- **真实 Kubernetes 清单 apply**：当前环境没有可用集群，示例 YAML 只做了语法解析，没有做 CRD schema 验证或实际 apply。
- **真实 Kueue 版本行为**：教程以 v1beta2 文档为主线；没有在具体 Kueue release 上逐字段验证。生产使用前请以目标版本的 API reference/release notes 为准。
- **DRA/MultiKueue/ProvisioningRequest/Concurrent Admission 的真实实现**：MiniKueue 只有 lite 版本；示例和章节用于说明语义，不代表真实设备分配或跨集群网络行为。
- **资源变换的真实 Configuration 行为**：MiniKueue 只实现 `Replace`/`Retain`、整数倍率和固定倍率；没有验证真实 `resource.Quantity`、webhook 默认化、Configuration 热更新和 CQ 覆盖校验。示例 YAML 只做了语法解析。
- **Configurable Preemption 的真实 CRD 行为**：MiniKueue 只实现 trigger/scope/priority/numericLabels/labelSelector/clusterQueueSelector；没有验证 preemptor selector、规则优先级、逐层 ParentCohort、v1alpha1 CRD/webhook 和 annotation 解析。
- **Fair Sharing 抢占的真实算法**：MiniKueue 用借用式 DRS + 两种 strategy 近似；没有验证 AlmostLCA、目标 CQ 排序、预抢占、采样窗口和精确锚点。
- **ProvisioningRequest 的云扩容**：ProvisioningConfig 只模拟 delay/retry/Ready；没有 ProvisioningRequest 对象、Cluster Autoscaler、`ProvisioningRequestConfig.retryStrategy` 或 `podSetUpdates`。
- **TAS 的真实节点/设备模型**：MiniKueue 只实现显式 domain 容量、slice、partial slice、打散和四种放置模式，没有验证真实节点标签、非 TAS Pod 占用、设备可行性和热替换。跨层账本用"按容量比例下钻"近似粗层分配，能防止 rack/hostname 双计，但不是精确的 host 级装箱。
- **AFS 的采样窗口/计量锚点**：MiniKueue 只实现历史用量、半衰期、Entry Penalty 和 LocalQueue 权重，没有实现真实 AFS 的采样窗口和精确锚点结算。
- **Cohort 级 limit 的逐层精确账本**：MiniKueue 用子树借用总量和逐成员 lending 上限近似，未验证真实 Kueue 的逐层借出账本；共享池按根树扁平聚合，兄弟子树共享池可见。
- **容量模拟器的真实校准**：`scripts/capacity_model.py` 只用合成分布做了自检，没有用生产 trace 校准，也没有模拟 TAS/DRA/多资源维度；第 29 章明确把它定位为评审工具。
- **第 30 章的 controller-runtime 代码**：是迁移骨架，需要在一个真实的 kubebuilder 项目中编译；本仓库没有引入 controller-runtime 依赖，因此没有编译/运行验证。
- **形式化验证**：没有做 TLA+/Alloy 模型检查；第 27 章只给出方法，不声称已验证。
- **差分测试**：没有在真实 Kueue 集群上做自动化对拍；第 27 章给出流程和已知差异清单。
- **MultiKueue failover**：MiniKueue 的 `Down` 是进程内状态，不模拟网络分区、连接退避、watch 中断和"worker 仍在运行但 manager 看不见"导致的重复执行；只验证状态机主干（超时、切换、恢复清理）。第 34 章明确标注了这个边界。
- **第 35 章扩展代码**：AdmissionCheck controller / GenericJob / client-go 片段是骨架，仓库没有引入 controller-runtime 依赖，未编译、未跑 envtest；使用前需在真实 kubebuilder 项目中补齐 imports、RBAC 和 webhook。
- **第 36 章推理/弹性对象**：Deployment/StatefulSet/LWS/PodGroup、Partial Admission、Elastic Workload Slices 的 YAML 只做了语法解析，没有在真实 Kueue 集群上验证；MiniKueue 未实现 `minCount`/Elastic Slices，只覆盖 dynamic reclaim、TAS slice/spreading、DRA 和 MultiKueue failover。
- **TAS spreading 注解**：`podset-topology-spreading` 的份额语义来自 KEP/文档，示例未在真实集群验证；不同版本的 gate 默认值可能不同。

## 10. 复现

```bash
cd kueue-tutorial
bash scripts/verify_all.sh
```

或手动执行：

```bash
cd kueue-tutorial/mini-kueue
gofmt -l .
go vet ./...
go test ./... -count=1
go test ./... -cover
go test ./... -count=10
go test ./... -count=1 -shuffle=on
go test ./internal/scheduler -bench BenchmarkTryAdmit -benchmem -run '^$'
go run ./cmd/minikueue -scenario basic -ticks 1 -get workloads
go run ./cmd/minikueue -scenario basic -ticks 1 -describe default/job-a
go run ./cmd/minikueue -scenario basic -ticks 1 -stop default/job-b
go run ./cmd/minikueue -scenario basic -ticks 1 -resume default/job-b
go run ./cmd/minikueue -scenario multikueue-failover -ticks 5
cd ..
python scripts/verify_docs.py
python scripts/capacity_model.py --nodes 12 --gpus-per-node 8 \
  --arrival-rate 0.5 --mean-duration 6 --job-size 8 --hours 2160 --json
```

容量模拟器的确定性检查：固定 `--seed 42` 时，同一组参数的 JSON 输出应逐字段一致。`verify_all.sh` 会把两组代表性参数跑一遍作为冒烟测试。

## 11. 上一轮（第 31～36 章）变更摘要

### 11.1 历史修复（保留）

- 第一轮修复 15 类语义/账本/校验缺陷，新增 `ExecutionTicks`、`Cache.Refresh`、`releaseTopologyPods`、`evictionCount`、`validWeight` 等机制；
- 第二轮修复 15 类跨层账本/对象隔离/迁移状态缺陷，新增 `Scheduler.TopologyUsage`、spec 克隆、DRA 映射校验、`addPlacement` 等机制；
- 新增 `cohort-shared-pool` 场景，覆盖显式 Cohort 的独立共享 nominal 池。

### 11.2 上一轮实现补全

- 资源变换：`api.ResourceTransformation`、`ApplyResourceTransformations`、`Workload.Spec.ResourceTransformations`、`Engine.SetResourceTransformations`；`PodSetPerPodUsage` 在 DRA 之后应用；校验拒绝 `pods`、重复 input、负 output。
- ProvisioningRequest-lite：`controller.ProvisioningConfig`；`reserveWith` 写 `LastTransitionTick`；`evaluateChecks` 驱动 Retry/Ready；重试计数跨预留保留。
- 指数退避：`controller.BackoffConfig` + `requeueDelay`；用于抢占/驱逐和未显式延迟的 AdmissionCheck Retry。
- Configurable Preemption-lite：`api.PreemptionConfig`/`PreemptionRule`/`PreemptionCandidateSelector`；`Scheduler.PreemptionConfigs`；trigger 区分 `InsufficientQuota` 与 `QuotaFeasibleAndInsufficientTopology`；候选与 Classic 合并、去重、最小化。
- Fair Sharing 抢占-lite：`Engine.SetFairSharingPreemption`；`findFairSharingPreemption`；`fairShareValue` 支持假设准入与排除集合；`fairShareWeight` 乘上 Cohort 权重。
- CLI：`-get`、`-describe`、`-stop`、`-resume`；`Engine.Activate`。
- 场景：`resource-transform`、`provisioning-request`、`configurable-preemption`、`fair-sharing-preemption`。

### 11.3 上一轮测试与文档

- 上一轮测试：顶层 119 → 135，场景子测试 22 → 26，合计 161 PASS；新增 16 个针对性回归测试（含 MultiKueue failover 4 个）。
- 文档：新增第 31 章（kueuectl 运维）、第 32 章（HA/灾备）、第 33 章（实现补全）、第 34 章（MultiKueue 深入）、第 35 章（扩展开发）、第 36 章（推理/弹性）；更新第 5/7/9/13/14/15/16/19/20/22/23 章与 README、MiniKueue README。
- 示例：新增 7 个 YAML（资源变换、ProvisioningRequest、Configurable Preemption、Fair Sharing 抢占、MultiKueue、自定义 AdmissionCheck、推理/弹性负载），共 19 个 YAML 通过解析。
- 上一版验证：36 章、19 个 YAML、28 个 CLI 场景、161 个 PASS 全部通过；容量模拟器可复现。本版结果见第 12 节。

### 11.4 未改变的事实

- MiniKueue 仍然是教学模拟器，不是 Kueue fork，不能接管 Kubernetes 集群。
- 新增能力均为 lite：资源变换不支持 Quantity，ProvisioningRequest 不连接云扩容，Configurable Preemption 无 preemptor selector，Fair Sharing 抢占无 AlmostLCA。
- 真实 Kueue 的字段、默认值和 feature gate 以目标版本 API reference/release notes 为准。

### 11.5 上一轮新增：MultiKueue failover、扩展与推理章节

- **MultiKueue failover**：
  - `multikueue.Worker` 新增 `Down`；
  - `multikueue.Config` 新增 `WorkerLostTimeout`；
  - `Manager` 新增 `downTicks`、`SetWorkerDown`、`cleanupStale`；
  - `Tick` 在选中 worker 失联时按 timeout 等待/切换；健康 worker 的陈旧副本立即清理，失联 worker 的副本恢复后清理；
  - `Nominated` 在每次 Tick 前剔除 Down worker，避免 selected-healthy 早返回时继续展示不可达 worker；
  - Incremental 使用独立的 `rounds` 计数，失联等待 tick 不消耗扩张预算；
  - 新增 `scenario.MultiKueueFailover` 与 CLI `-scenario multikueue-failover`；
  - 新增 4 个测试：`TestDownWorkerIsNotNominated`、`TestFailoverAfterWorkerLostTimeout`、`TestNoAvailableWorker`、`TestMultiKueueFailoverScenario`；
  - 三个协调器修复各有回归子测试：Down worker 从 `Nominated` 剔除、失联等待不消耗 Incremental 轮次、重复名字只标记一行、空名字不被误标。
- **新增章节**：
  - 第 34 章 MultiKueue 多集群调度深入；
  - 第 35 章 Kueue 扩展开发；
  - 第 36 章 推理、弹性与混合负载。

（以上为上一版记录；本版新增第 37～38 章，见第 12 节。）
- **新增示例**：
  - `examples/kueue/multikueue.yaml`（10 docs）；
  - `examples/kueue/custom-admission-check.yaml`（6 docs）；
  - `examples/kueue/inference-workloads.yaml`（7 docs）。
- **文档修复**：
  - 第 19 章实验编号从重复的 `19.8.1`–`19.8.7` 修正为 `19.9`–`19.16`，测试/验收/小结顺延为 `19.17`–`19.19`；
  - 第 16 章补充 2026 路线图（WAS、MultiKueue 弹性 Ray/长运行服务、TAS 资源变换/弹性、hero workloads、preemption cost、API v1）；
  - 第 14/23 章补充 failover 场景、命令、练习和 MultiKueue/弹性字段速查。
- **审计修正**：
  - `examples/kueue/provisioning-request.yaml` 的 `spec.admissionChecks` 改为 v1beta2 的 `admissionChecksStrategy.admissionChecks`；注释改为"Pending 保留配额，Retry 释放配额"；
  - 第 35 章的 `Workload.status.admissionCheckStates` 改为 `status.admissionChecks`，`GenericJob` 签名改为 `RunWithPodSetsInfo(...) error`、`RestorePodSetsInfo(...) bool`、`Finished() (message string, success, finished bool)`、`GVK()`；
  - 第 35 章的 `WaitAdmitted` 不再检查不存在的 `Inadmissible` 条件，改为检查 `QuotaReserved=False` 且 `Reason=Inadmissible`；
  - 全书把"`Inadmissible` 是独立条件类型"的表述改为"`QuotaReserved=False` 的 Reason"，并说明 MiniKueue 的 `Inadmissible` 条件是教学扩展；
  - MultiKueue 指标改为 `multikueue_workloads_dispatched_total`，抢占 gate 条件改为 `BlockedOnPreemptionGates`；
  - 第 19 章把孤立的 `19.2.5` 改为 `19.2.1`，TAS 模式表述改为"四种放置模式 + 两个扩展能力"，实验十二/十三/十四/十五的标签不再重复。

## 12. 本版（第 37～38 章、诊断能力与一致性修复）

> 历史记录：这一节的测试/YAML/章节计数反映的是上一轮的状态；当前计数以第 13 节为准。

验证日期：2026-11-08。

### 12.1 新增代码

- `internal/scheduler/explain.go`
  - `Explain`：只读的准入诊断，输出逐资源 `FitCheck`、`QuotaFeasible`/`DirectFeasible`、拓扑 domain 容量、抢占候选/ victim。
  - `evaluateFlavor`：诊断与 `canFitFlavor` 共用的唯一配额检查实现；`detailed=false` 时跳过 checks 分配，保持热路径 8 allocs。
  - `explainTopology`：按叶子 domain 输出 capacity/usage/free。
- `internal/controller/explain.go`
  - `Engine.Explain`：在调度器结论上叠加 inactive/gate/stopPolicy/AdmissionCheck/Admitted 等控制器层原因。
- `cmd/minikueue/main.go`
  - `-explain ns/name`：渲染结构化报告。
  - `-what-if clusterqueue/flavor/resource=value`：从 tick 0 重放两次，对比 Workload 状态。
- `scripts/verify_all.sh`：新增 explain/what-if 的 CLI 冒烟（basic、cohort-borrow、tas、reclaim）。

### 12.2 新增测试

顶层测试 135 → 145（+10）：

- scheduler：`TestExplainReportsClusterQueueNominal`、`TestExplainMatchesAdmissionDecision`、`TestExplainExcludesOwnReservation`、`TestExplainBorrowCheckDetails`、`TestExplainReportsAdmissionGateAtControllerLevel`；
- controller：`TestEngineExplainPendingWorkload`、`TestEngineExplainAdmittedWorkload`、`TestEngineExplainControllerBlockers`、`TestEngineExplainMissingWorkload`；
- scheduler：`TestTASPartialSlice`（补上 partial slice 之前缺失的回归测试）。

结果：

```text
go test ./... -count=1        # 全部通过
go test ./... -list '.*'      # 145 个顶层 Test
go test ./internal/scenario -v # 26 个场景子测试
```

### 12.3 覆盖率与基准

```bash
go test ./... -cover -count=1
```

| 包 | 覆盖率 |
|---|---:|
| `internal/api` | 52.6% |
| `internal/cache` | 72.9% |
| `internal/controller` | 80.8% |
| `internal/metrics` | 100.0% |
| `internal/multikueue` | 90.9% |
| `internal/queue` | 82.5% |
| `internal/scenario` | 94.1% |
| `internal/scheduler` | 73.8% |
| `internal/store` | 59.4% |

基准（1 CQ、500 个已准入 Workload、不触发抢占）：

```text
BenchmarkTryAdmit-16    91039    13916 ns/op    880 B/op    8 allocs/op
BenchmarkTryAdmit-16    95763    30348 ns/op    880 B/op    8 allocs/op
BenchmarkTryAdmit-16    35900    34021 ns/op    880 B/op    8 allocs/op
```

`evaluateFlavor` 的 `detailed` 参数保证诊断路径才分配 `FitCheck` 切片；直接准入路径仍是 880 B/op、8 allocs/op。

### 12.4 文档

- 新增第 37 章（调度决策的可解释性）与第 38 章（原生 WAS 与 gang scheduling）。
- `verify_docs.py` 的 expected 列表更新为 38 章；实际输出 `OK: 38 chapters, 19 YAML files, links/fences valid`。
- 修正的事实/一致性问题：
  - 版本：v0.20.x（最新 patch v0.20.5；v0.21 仅 devel）；README/第 3/22/24 章示例统一。
  - API：Workload 优先级改为 `priorityClassRef`；Workload 状态字段为 `status.admissionChecks`；v1beta2 的 ClusterQueue 使用 `spec.admissionChecksStrategy.admissionChecks`（顶层 `spec.admissionChecks` 已移除）。
  - MultiKueue：`MultiKueueOrchestratedPreemption` 改为 Beta 默认开、timeout 可配置；补 External dispatcher 差距。
  - DRA：Kubernetes 1.34 GA、1.37 `DRAWorkloadResourceClaims` Beta；AdminAccess/DeviceTaintRule 更新；补 Kueue 侧 partitionable/consumable gate。
  - StatefulSet：改为 Plain Pod Group、v0.20 不支持扩缩，删除 `replicas=0` 说法。
  - 指数退避、Fair Sharing 抢占、Cohort 权重、ProvisioningRequest-lite、Configurable Preemption-lite 的"未实现"表述改为"已实现 lite/完整差异"。
  - 第 14 章 `TotalUsage`/`canFitFlavor`/DRA 计费/`AdmittedAt` 走读片段按代码重写。
  - 第 22 章可选实验编号从 `22.12.x` 改为 `22.13.x`；实验数量统一为 12 核心 + 8 可选。
  - 第 26 章补 4 个 Namespace、Topology/`topologyName`、32 卡作业"28 保底 + 4 借用"说明、`--dry-run=server` 验收。
  - 第 28 章决策树 `Admitted=False` 分支修正；新增 CA/Karpenter 与 scheduling gate 小节。
  - 第 31 章配额日报改用 `status.flavorsReservation`/`status.flavorsUsage`。
  - 第 32 章补 `storedVersions`（24.4.4）、备份一致性、外部资源对账；恢复顺序删除不存在的 `Domain`。
  - 指标：`kueue_pending_workloads{status="active"}`、PromQL 保留 `flavor`、补 LocalQueue 指标。

### 12.5 未验证/边界

- `-explain` 的解释与 `TryAdmit` 共用 `evaluateFlavor`，但抢占诊断是"再走一遍"搜索，不保证与真实 Kueue 的 victim 选择完全一致。
- `-what-if` 只支持单变量 nominal 覆盖；它不是生产预测，不能替代第 29 章的容量模型。
- 第 37/38 章的 Kubernetes/Kueue 版本事实来自公开 release notes、KEP/issue 与 WebSearch；本机没有 Kubernetes 集群，未做真实 WAS/PodGroup 联调。
- 真实 Kueue 的 `status.admissionChecks`、`priorityClassRef`、`MultiKueueOrchestratedPreemption` 默认值仍建议在目标 release 上复核。

## 13. 本轮增补（纳管边界、配置工程与三轮审计修复）

验证日期：2026-11-08。环境同第 1 节（Windows 11、Go 1.27.0、Python 3.14 + PyYAML）。

### 13.1 代码与语义修复

- **Fair Sharing 分母对齐真实 Kueue**：新增 `Cache.FairShareLendable`，用"其他 CQ 的 nominal 总和（受 lendingLimit 截断）"作为 DRS 分母；`Cache.LendableExcluding` 保留原语义（其他 CQ 当前未使用的 nominal），继续供 `-explain` 的"现在还能借多少"使用。`TestFairShareWeight` 期望从 0.5 改为 0.25；新增 `TestFairShareLendableDoesNotDependOnSiblingUsage` 同时锁定两种语义。
- 回归测试仍全绿；没有改变 `BenchmarkTryAdmit` 的直接准入路径。

### 13.2 文档事实修复

- `status.admissionCheckStates` → `status.admissionChecks`（v1beta2）；第 4 章保留一句"旧拼写"说明。
- v1beta2 ClusterQueue 的 `spec.admissionChecks` 已移除，统一为 `spec.admissionChecksStrategy.admissionChecks`（第 23/24/34 章）。
- `deviceClassMappings` 明确为 **Configuration `resources` 字段**，不是 ClusterQueue spec；MiniKueue 的 CQ 字段标注为教学简化。
- `priorityClassRef` 保留 `group/kind/name`（已核对官方示例），补允许的 group/kind。
- `admissionScope.admissionMode` 统一为对象写法。
- kueuectl 命令地图补 `get/describe/edit/patch/delete`，删除说明改为"LocalQueue 用 kubectl 删除"；安装章节引用改为 3.2.2。
- 配额日报 jq 从 `.totalQuota` 改为 `.total`，并说明 `.borrowed`。
- 原生 PodGroup 字段 `minMember` → `minCount`；补"原生 Workload（模板）/PodGroup（实例）/Kueue Workload（配额）"三者分工。
- 原生 WAS 是 in-tree API，没有 CRD 可删；回滚说明改为排空 Pod、关闭 gate、清理 v1alpha2。
- README 的 WAS 版本改为 1.36 Alpha / 1.37 Beta。
- 第 20 章 MultiKueue-lite 补 Worker.Down/WorkerLostTimeout/failover；第 26 章 Cohort 叙事改为 root 树下的 product/research 子树，修正 32 卡借用说明与推理"Cohort"措辞。
- 第 8 章自定义集成伪代码移除 `if Suspend return` 死锁；补 JobSet 真实清单与 Workload 映射。
- 第 5 章 Flavor fungibility 补 worked example；`parentName` 缺失改为"引用无效/Inactive"。
- 第 6 章权重 0 的合法语义与 MiniKueue 偏差写明；第 13 章重试上限表述修正。
- 第 22 章 Lab 8 修正"GPU 缺失时 Kueue 层 Pending"，TAS 注解错误归因修正；资源变换/Fair Sharing 实验改为先合并 ConfigMap、再 apply 纯 CR 文件。
- 第 27 章新增 envtest/集成/e2e 测试栈；第 31 章补 kueuectl 命令表。

### 13.3 新增内容与工程资产

- 第 8.5 节扩展为五种纳管机制（显式标签、默认 LocalQueue、默认 WorkloadPriorityClass、manageJobsWithoutQueueName + namespace selector、VAP），含执行顺序、选型矩阵、失败模式。
- 第 3.2.4 节离线/气隙安装；第 3.6.1 节 Configuration 安全变更流程；第 3.6.2 节静态检查器。
- 第 10.4.1 控制器/Webhook 故障行为；第 11.3.1 ResourceQuota 共存；第 11.3.2 namespace/LQ 生命周期与孤儿配额；第 21.8.1 Showback/Chargeback 口径。
- 新增示例：`examples/kueue/enforce-management.yaml`、`jobset.yaml`、`dra.yaml`；`examples/config/` 下的完整配置与片段（`kueue-config.yaml`、`resource-transformations.yaml`、`fair-sharing-preemption.yaml`、`dra-device-mapping.yaml`）。
- 新增 `scripts/check_kueue_config.py`：检查 gate 依赖反转、未知 gate/framework、危险纳管组合、保留资源名 `pods`、非法 duration、重复 transformation input 等；`scripts/testdata/invalid-config.yaml` 作为反例。
- `verify_docs.py` 新增：章节标题编号校验、README 章节/YAML 计数校验、`admissionCheckStates`/`.totalQuota`/`minMember` 过期术语检查。
- `verify_all.sh` 新增 `go build`、CLI 场景数 = 28 断言、`-get localqueues/cohorts/flavors/topologies` 冒烟、配置检查器正反例。

### 13.4 执行结果

```text
gofmt -l .                        无输出
go vet ./...                      pass
go build ./...                    pass
go test ./... -count=1            9 个包全部 ok
go test ./... -v                 146 个顶层 PASS
internal/scenario -v              26 个场景子测试 PASS
go run ./cmd/minikueue -list      28 行（26 引擎场景 + 2 MultiKueue）
verify_docs.py                    OK: 38 chapters, 26 YAML files, links/fences valid
check_kueue_config.py --strict    OK: 0 errors, 0 warnings
check_kueue_config.py invalid     exit 1（按预期拒绝）
verify_all.sh                     all checks passed
```

覆盖率（`go test ./... -cover`）：`api 52.6%`、`cache 71.4%`、`controller 80.8%`、`metrics 100%`、`multikueue 90.9%`、`queue 82.5%`、`scenario 94.1%`、`scheduler 73.7%`、`store 59.4%`。

### 13.5 未验证/边界

- 仍在 Windows 上验证，没有真实 Kubernetes 集群；新示例 YAML 只做了 PyYAML 解析和静态引用检查，没有做 CRD schema 校验或真实 apply。
- 默认 LocalQueue/WorkloadPriorityClass/VAP 的行为依据上游文档/KEP 与 WebSearch 整理；目标 release 的 gate 默认值仍需在集群上复核。
- 离线安装的镜像名/资产文件名以 manifests 与 release 页面为准；示例中的 `registry.internal.example.com` 是占位。
- Showback 的 PromQL 使用队列级指标，不能替代 Workload 级账单；成本中心归因仍需业务标签或账单系统。
