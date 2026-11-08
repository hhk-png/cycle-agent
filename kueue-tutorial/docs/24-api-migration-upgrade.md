# 第 24 章 · API 迁移与版本升级手册

Kueue 迭代很快：API 从 `v1beta1` 走到 `v1beta2`，Cohort 从字符串分组变成一等 CRD，feature gate 的默认值几乎每个 minor 版本都在变。很多生产事故不是"Kueue 不会调度"，而是**升级时字段写错、CRD 版本没切、gate 依赖没对齐**。

本章给出一套可执行的迁移方法。它不替代目标版本的 release notes 和 API reference，但能帮你把"升级"从一次性冒险变成可评审、可回滚的流程。

## 24.1 为什么迁移要单独讲

一次 Kueue 升级至少涉及五个层面：

```text
1. CRD schema        served/storage version、字段增删、默认值
2. 控制器镜像        调度语义、gate 默认值、bug fix
3. Webhook           默认值、校验、不可变字段、证书
4. 客户端/清单       kubectl、GitOps、kueuectl、自研集成
5. 运行时状态        Workload 条件、Cache 重建、finalizer
```

只升级镜像不升级 CRD，或只升级 CRD 不检查 gate，都会出现"控制器起来了但行为不对"的状态。迁移必须把这五层作为一个整体来验证。

## 24.2 API 组与版本格局

截至 2026 年 11 月（稳定线 v0.20.x，最新 patch v0.20.5；v0.21 仅有 devel），常见 API 组：

| API 组/版本 | 典型对象 | 说明 |
|---|---|---|
| `kueue.x-k8s.io/v1beta2` | Workload、LocalQueue、ClusterQueue、Cohort、ResourceFlavor、AdmissionCheck、Topology、ProvisioningRequestConfig | 主线 API |
| `kueue.x-k8s.io/v1beta1` | 旧版对象 | 逐步退场；v0.20 一代已不再 served，升级前必须迁移到 v1beta2 |
| `kueue.x-k8s.io/v1alpha1` | PreemptionConfig、CapacityProvider、DynamicQuotaOrchestrator | alpha 能力 |
| `config.kueue.x-k8s.io/v1beta2` | Configuration | 控制器配置 |
| `visibility.kueue.x-k8s.io/v1beta1` | 查询 API | 按安装/版本启用 |

升级前先确认三件事：

```bash
# 1. 当前 CRD 提供哪些版本，哪个是 storage
kubectl get crd clusterqueues.kueue.x-k8s.io -o yaml \
  | yq '.spec.versions[] | {name, served, storage}'

# 2. 集群里实际存的是哪个 apiVersion
kubectl get clusterqueues -o json \
  | jq -r '.items[].apiVersion' | sort -u

# 3. 控制器镜像版本
kubectl -n kueue-system get deploy kueue-controller-manager \
  -o jsonpath='{.spec.template.spec.containers[0].image}'
```

如果 `served` 里还有旧版本，说明可以双版本过渡；如果只剩新版本，旧清单会在 apply 时直接失败。

## 24.3 v1beta1 → v1beta2 变更地图

下面列出最常见的迁移点。**字段名和默认值必须以你目标版本的 API reference 为准**；这里给的是"迁移时该去哪里找"和"容易漏什么"。

### 24.3.1 ClusterQueue

| v1beta1 常见写法 | v1beta2 方向 | 注意 |
|---|---|---|
| `spec.cohort: shared` | `spec.cohortName: shared` | 字段改名；同名 Cohort 对象可选 |
| `spec.admissionChecks: [check-a]` | `spec.admissionChecksStrategy.admissionChecks[]` | v1beta2 已移除顶层 `spec.admissionChecks`；策略对象还支持 `onFlavors` |
| `spec.queueingStrategy: StrictFIFO` | 同名 | 值不变，默认仍是 `BestEffortFIFO` |
| `spec.preemption.*` | 同名 | `withinClusterQueue` 的值是 `LowerOrNewerEqualPriority` |
| `spec.namespaceSelector` | 同名 | `null` 不允许任何 namespace，`{}` 允许全部 |
| 无 | `spec.fairSharing` | 新增；CQ/Cohort 权重 |
| 无 | `spec.admissionScope.admissionMode` | Admission Fair Sharing（对象字段，不是标量） |
| 无 | `spec.concurrentAdmissionPolicy` | alpha |
| 无 | `spec.flavorFungibility` | Flavor 切换策略 |

一个迁移前后的对照：

```yaml
# v1beta1（示意）
apiVersion: kueue.x-k8s.io/v1beta1
kind: ClusterQueue
metadata:
  name: team-a-cq
spec:
  cohort: shared
  admissionChecks: ["capacity-check"]
  namespaceSelector: {}
  resourceGroups:
  - coveredResources: ["cpu", "memory"]
    flavors:
    - name: a100
      resources:
      - {name: cpu, nominalQuota: 128}
      - {name: memory, nominalQuota: 1Ti}
```

```yaml
# v1beta2
apiVersion: kueue.x-k8s.io/v1beta2
kind: ClusterQueue
metadata:
  name: team-a-cq
spec:
  cohortName: shared
  admissionChecksStrategy:
    admissionChecks:
    - name: capacity-check
  namespaceSelector: {}
  resourceGroups:
  - coveredResources: ["cpu", "memory"]
    flavors:
    - name: a100
      resources:
      - {name: cpu, nominalQuota: 128}
      - {name: memory, nominalQuota: 1Ti}
```

### 24.3.2 Cohort

v1beta1 的 Cohort 通常只是 `cohort` 字符串；v1beta2 引入 `Cohort` 对象：

```yaml
apiVersion: kueue.x-k8s.io/v1beta2
kind: Cohort
metadata:
  name: shared
spec:
  parentName: root
  fairSharing:
    weight: "3"
```

迁移时：

1. 先创建所有 `Cohort` 对象（从根到叶）；
2. 再把 CQ 的 `cohortName` 指过去；
3. 如果暂时不需要共享池/层级/权重，可以只保留 `cohortName`，使用隐式 Cohort；
4. 删除旧 Cohort 对象前确认没有 CQ 依赖它的层级语义。

### 24.3.3 Workload

Workload 的主线字段变化相对少，但状态字段在增加：

| 字段 | 迁移注意 |
|---|---|
| `spec.podSets[]` | 结构基本稳定；资源按 `count × request` 汇总 |
| `spec.priority` / `priorityClassRef` | v1beta2 用 `priorityClassRef{group,kind,name}` 引用 `WorkloadPriorityClass`；v1beta1 的 `priorityClassName`/`priorityClassSource` 已移除。优先级来源要和 Pod PriorityClass 区分 |
| `spec.active` | 停用语义不变；不要用它做"暂停队列" |
| `status.conditions` | 条件类型增加；不要只读单一条件 |
| `status.admission` | Flavor 分配/Topology 分配结构可能扩展 |
| `status.admissionChecks` | 增加 `requeueAfterSeconds`、`retryCount`、`podSetUpdates` |
| `status.requeueState` | 退避重排队 |
| `status.schedulingStats` | 驱逐原因统计 |
| `status.nominatedClusterNames` / `clusterName` | MultiKueue |
| `status.reclaimablePods` | 动态回收 |

自定义集成要特别注意：**不要写死 status 的完整结构**，只更新自己拥有的字段；否则升级后新增字段可能被覆盖。

### 24.3.4 其他对象

| 对象 | 变化方向 |
|---|---|
| LocalQueue | `spec.clusterQueue`、`stopPolicy`、`fairSharing.weight` |
| ResourceFlavor | `nodeLabels`、`tolerations`、`nodeTaints`、`topologyName` |
| AdmissionCheck | `controllerName` + `parameters`；v1beta2 用策略引用 |
| Topology | `levels[].nodeLabel` |
| WorkloadPriorityClass | 独立优先级，不影响 Pod priority |
| ProvisioningRequestConfig | `provisioningClassName`、`managedResources`、`retryStrategy` |
| MultiKueueConfig/Cluster | 多集群配置与凭据 |

## 24.4 CRD 升级机制

### 24.4.1 served 与 storage

一个 CRD 可以同时提供多个版本。下面是**通用双版本示意**；注意从 Kueue v0.20 起，v1beta1 已经不再 served，不能再按"保留旧 served"的流程升级（见 24.4.4）：

```yaml
spec:
  versions:
  - name: v1beta1
    served: true     # 通用示意；Kueue v0.20+ 为 false
    storage: false
  - name: v1beta2
    served: true
    storage: true
```

- `served`：客户端能否用这个版本读写；
- `storage`：etcd 里实际存哪个版本；
- 多个 served 版本之间需要 conversion（转换 webhook 或 `None` 策略）。

升级顺序通常是：

```text
1. 安装新 CRD（保留旧 served 版本）
2. 部署新控制器（读写新版本）
3. 确认 conversion/webhook 正常
4. 把 storage 切到新版本
5. 客户端清单切到新版本
6. 观察一个完整作业周期
7. 再移除旧 served 版本
```

不要反过来：先删旧版本再升级控制器，会让旧清单和旧客户端直接失败。

### 24.4.2 Conversion webhook

如果新旧版本字段不是一一对应，Kueue 可能依赖 conversion webhook。检查：

```bash
kubectl get crd clusterqueues.kueue.x-k8s.io -o yaml \
  | yq '.spec.conversion'
```

常见故障：

- webhook Service 不可达 → 读写 CRD 失败；
- 证书过期 → conversion 报 TLS 错误；
- 转换逻辑丢失字段 → apply 成功但语义变了；
- 旧版本对象没有 round-trip → 读出来再写回去字段漂移。

升级前在 staging 做一次 round-trip：

```bash
kubectl get clusterqueues -o yaml > before.yaml
kubectl apply -f before.yaml --dry-run=server
kubectl get clusterqueues -o yaml > after.yaml
diff <(yq 'del(.items[].metadata.resourceVersion)' before.yaml) \
     <(yq 'del(.items[].metadata.resourceVersion)' after.yaml)
```

### 24.4.3 不要删除 CRD

删除 CRD 会级联删除所有自定义资源，包括 Workload、ClusterQueue、Cohort。即使你有 GitOps，也可能丢失运行时状态（status、finalizer、队列历史）。升级不是卸载；只在确认没有任何对象依赖时才考虑删除 CRD。

### 24.4.4 storedVersions 与 v1beta1 移除

从 v0.19 升级到 v0.20 时，v1beta1 会变成 `served: false`。这不等于 etcd 里已经没有 v1beta1 数据；`status.storedVersions` 仍可能包含它：

```bash
kubectl get crd clusterqueues.kueue.x-k8s.io \
  -o jsonpath='{.status.storedVersions}{"\n"}'
```

在移除旧版本前，必须确保：

1. 所有对象都已经用 v1beta2 重新写入（读一次再写一次，或使用官方迁移工具）；
2. `storedVersions` 列表里只剩 v1beta2；
3. 没有旧客户端仍在用 v1beta1 写入。

清理顺序：

```text
1. 备份 CRD 与对象
2. 把 storage version 切到 v1beta2
3. 用 v1beta2 重写所有存量对象
4. 验证 storedVersions 只含 v1beta2
5. 从 CRD 的 versions 列表中移除 v1beta1
6. 观察一个完整作业周期
```

如果跳过第 3/4 步直接删版本，etcd 里可能残留旧版本编码的数据，读取时报 conversion 错误。回滚窗口内不要删除旧控制器镜像和 CRD 清单。

## 24.5 Feature gate 迁移

### 24.5.1 gate 默认值会变

beta gate 可能在某个版本默认开启，alpha gate 可能默认关闭。升级后行为变化通常来自这里，而不是调度器代码。

迁移时做三件事：

1. 从旧版本和新版本的安装清单里提取 `--feature-gates`；
2. 逐项对比默认值；
3. 对依赖链做闭包检查。

```bash
# 示意：从两个 release 的 manifests 提取 gate
# 截至 2026-11，稳定 patch 是 v0.20.5；不要引用尚不存在的 v0.21.0
curl -sL .../v0.20.4/manifests.yaml | grep -o 'feature-gates=[^"]*' | sort -u
curl -sL .../v0.20.5/manifests.yaml | grep -o 'feature-gates=[^"]*' | sort -u
```

### 24.5.2 依赖链

Kueue 的 gate 不是独立的。常见的依赖关系：

| 能力 | 相关 gate | 迁移风险 |
|---|---|---|
| TAS | `TopologyAwareScheduling` + `TAS*` | 只关总开关、不关依赖 gate，控制器可能拒绝启动 |
| AFS | `AdmissionFairSharing` + `AdmissionFairSharingAnchorAtQuotaReservation` | 关闭前者时要同时处理后者 |
| DRA | `KueueDRAIntegration` + ExtendedResource/ConsumableCapacity | Kubernetes 侧 DRA gate 也要满足 |
| MultiKueue | `MultiKueue` + dispatcher 相关 gate | worker 与 manager 版本要兼容 |
| Concurrent Admission | `ConcurrentAdmission` | 只支持 BestEffortFIFO/单资源组 |
| DQO | `DynamicQuotaOrchestration` | alpha，默认关 |

配置错误时，控制器可能在启动阶段直接退出。升级后先看启动日志，不要只看 Pod 是否 Running。

### 24.5.3 一个 gate 迁移检查脚本

```bash
#!/usr/bin/env bash
set -euo pipefail
before="$1"   # 旧版本 manifests
after="$2"    # 新版本 manifests
extract() { grep -o 'feature-gates=[^"]*' "$1" | tr ',' '\n' | sort -u; }
diff <(extract "$before") <(extract "$after") || true
```

把 diff 结果贴到变更单里，逐项确认"为什么变、影响谁、如何回滚"。

## 24.6 分阶段迁移方案

### 阶段 0：盘点

- 列出所有使用 Kueue API 的清单、控制器、脚本；
- 标出 `apiVersion` 和字段；
- 找出自定义集成、MultiKueue worker、DRA/ProvisioningRequest 依赖；
- 记录当前 gate 和默认值。

### 阶段 1：staging 双版本

- 如果目标版本仍提供旧 served，安装新 CRD 并保留旧 served；**从 v0.20 起 v1beta1 已不再 served，这一步改为 storedVersions 清理**（见 24.4.4）；
- 部署新控制器；
- 用旧清单和新清单各跑一遍；
- 做 conversion round-trip；
- 验证 pending/admitted/evicted/finished 全生命周期。

### 阶段 2：试点租户

- 选 1～2 个非关键队列；
- 切换清单到 v1beta2；
- 观察准入延迟、驱逐率、AdmissionCheck 等待；
- 保留旧清单作为回滚。

### 阶段 3：全量切换

- 按队列/团队分批切；
- 每次只改一类对象（先 CQ/Cohort，再 Job 集成）；
- 每批观察一个完整作业周期；
- 更新文档和 runbook。

### 阶段 4：清理

- 移除旧 served 版本；
- 删除旧字段和兼容代码；
- 更新客户端和 kueuectl；
- 复盘迁移中的意外。

## 24.7 迁移检查清单

### CRD 与控制器

- [ ] 新旧 CRD 的 served/storage 已确认；
- [ ] conversion webhook 可用，证书未过期；
- [ ] 控制器启动日志没有 gate 冲突；
- [ ] CRD 没有被误删；
- [ ] finalizer 能正常清理。

### 清单与客户端

- [ ] 所有清单的 `apiVersion` 已切到目标版本；
- [ ] `cohort` → `cohortName`、顶层 `admissionChecks` → `admissionChecksStrategy.admissionChecks` 已替换；
- [ ] 没有混用 v1beta1/v1beta2 字段；
- [ ] 自研集成只写自己拥有的 status 字段；
- [ ] kubectl/kueuectl 版本与 API 兼容。

### 运行时

- [ ] 旧 Workload 能正常转换和调度；
- [ ] 新 Workload 的条件、admission、checks 正常；
- [ ] MultiKueue worker 版本兼容；
- [ ] DRA/ProvisioningRequest 的外部依赖满足；
- [ ] 指标、事件、日志没有异常增长。

### 回滚

- [ ] 保留旧控制器镜像和 CRD 清单；
- [ ] 有明确回滚命令；
- [ ] 回滚后 storage version 仍可读；
- [ ] 已切换到新字段的对象有反向转换方案；
- [ ] 回滚窗口和观察指标已定义。

## 24.8 常见迁移错误

1. **只升级镜像，不升级 CRD。** 新控制器引用新字段，但 API server 不认识。
2. **先删旧 CRD 版本，再切客户端。** 旧清单直接失败，甚至对象不可读。
3. **忽略 conversion 的 round-trip。** apply 成功，但字段被静默丢弃。
4. **gate 只关一半。** 控制器启动失败，或部分能力行为不一致。
5. **把 `cohort` 和 `cohortName` 混用。** 两个字段同时存在时，以目标版本 schema 为准；不要凭记忆写。
6. **把 `admissionChecks` 列表直接搬到 v1beta2。** 要用 `admissionChecksStrategy`。
7. **忘记改 Webhook。** 默认值/校验仍按旧 schema，导致对象创建失败。
8. **MultiKueue manager/worker 版本跨太多。** CRD 和状态字段不兼容。
9. **在升级窗口做配额调整。** 两个高风险操作叠加，排障时无法区分原因。
10. **不回滚。** 发现问题后继续"再升一个版本试试"，会把问题放大。

## 24.9 版本矩阵模板

把下面这张表放进 Git，每次升级前填一遍：

```yaml
cluster: prod-gpu
current:
  kueue: v0.20.4
  kubernetes: v1.36
  api: v1beta2
target:
  kueue: v0.20.5        # 下一版若为 v0.21.x，请先确认已 GA 再填
  kubernetes: v1.37
  api: v1beta2
gates:
  changed: []           # 逐项从两个 release 的 manifests diff 出来，不要凭记忆填
  removed: []
  added: []
controllers:
  manager: v0.20.4 -> v0.20.5
  multikueueWorkers: [worker-a, worker-b]
verification:
  stagingWindow: 2026-11-01 -> 2026-11-03
  roundTrip: pass
  lifecycle: pass
  rollbackTested: true
```

## 24.10 本章小结

- 升级是 CRD、控制器、webhook、客户端和运行时状态的联合迁移，不是换镜像。
- v1beta1→v1beta2 的重点：`cohortName`、`admissionChecksStrategy`、Cohort 对象、AFS/Concurrent/DRA 等新字段。
- 先保留双版本、验证 conversion，再切 storage 和客户端，最后清理旧版本。
- gate 依赖链和默认值变化是"升级后行为不对"的高发原因。
- 迁移要分阶段、可回滚、有指标和清单；不要和配额调整叠加。
- 目标版本的 release notes 和 API reference 是最终依据。

---

[← 上一章：附录](23-appendix.md) | [返回目录](../README.md) | [下一章：生态集成实战 →](25-integration-cookbook.md)
