# 第 31 章 · kueuectl 与自动化运维

> 本章把"知道 Kueue 有哪些对象"变成"每天能安全地操作它们"。重点不是命令大全，而是查询、变更、自动化和审计的组合方式。

## 31.1 kueuectl 的定位

`kueuectl` 是 Kueue 提供的命令行工具，面向批处理平台管理员和租户。它和 `kubectl` 的关系可以概括为：

| 维度 | kubectl | kueuectl |
|---|---|---|
| 对象范围 | 所有 Kubernetes 资源 | Kueue 资源 + 队列操作 |
| 常用动作 | get/describe/apply/patch/delete | list/create/get/describe/edit/patch/delete/stop/resume；复杂字段仍可用 kubectl |
| 语义封装 | 通用 CRUD | 把"暂停队列""停止 Workload"等操作封装成意图 |
| 输出 | 通用表格/JSONPath | 面向队列和 Workload 的表格 |
| 依赖 | kubeconfig | kubeconfig + Kueue CRD |

两者并不互斥。生产脚本里常见的是：

- **声明式变更**用 `kubectl apply` / GitOps；
- **日常查询和紧急操作**用 `kueuectl`；
- **复杂字段和批量操作**用 `kubectl` + JSONPath/`jq`。

安装方式见第 3.2.2 节。先确认版本与集群匹配：

```bash
kueuectl version --client
kubectl version --short
kubectl get crd | grep kueue
```

## 31.2 命令地图

`kueuectl` 的命令分两类：

- **Kueue 特有动作**：`list`、`create`、`stop`、`resume`、`delete`（主要面向 Workload）；
- **kubectl 透传**：`get`、`describe`、`edit`、`patch`，对 Kueue 资源提供与 kubectl 一致的字段访问。

常用别名：

```text
cq  = clusterqueue
lq  = localqueue
rf  = resourceflavor
kwl = workload / kueueworkload
po  = pods
```

| 命令 | 支持的对象 | 典型用途 |
|---|---|---|
| `create` | clusterqueue、localqueue、resourceflavor | 快速建队列/Flavor（`--dry-run` 先看 YAML） |
| `list` | workload、pods、clusterqueue、localqueue | 面向 Kueue 的列/过滤（状态、队列、selector） |
| `stop` / `resume` | workload、clusterqueue、localqueue | 冻结/恢复，不删除对象 |
| `delete` | workload | 删除 Workload（可能提示连带删除 owner Job） |
| `get` / `describe` / `edit` / `patch` | Kueue 资源 | kubectl 透传，适合看/改任意字段 |
| `version` | — | 客户端/服务端版本 |

具体子命令和 flag 以 `kueuectl <cmd> --help` 与目标版本为准；不同版本的支持面会变化。

### 31.2.1 查看队列

```bash
# ClusterQueue 列表
kueuectl list clusterqueue

# 某个 CQ 的详情（describe/get 是 kubectl 透传）
kueuectl describe clusterqueue cluster-queue
kubectl get clusterqueue cluster-queue -o yaml

# LocalQueue 列表（按 namespace）
kueuectl list localqueue -n team-a

# Cohort（如果版本提供）
kubectl get cohorts
```

`kueuectl list clusterqueue` 的典型输出：

```text
NAME            COHORT   STRATEGY         PENDING WORKLOADS   ADMITTED WORKLOADS   ACTIVE   REASON
cluster-queue            BestEffortFIFO   3                   12                   True     Ready
```

排障时先看三列：

1. `PENDING WORKLOADS` 是否持续增长；
2. `ADMITTED WORKLOADS` 是否接近容量上限；
3. `ACTIVE/REASON` 是否因为 stopPolicy 或错误配置变为 False。

### 31.2.2 查看 Workload

```bash
# 当前 namespace 的 Workload
kueuectl list workload

# 指定 namespace 和队列
kueuectl list workload -n team-a --localqueue team-a-queue

# 详情（get/describe 是 kubectl 透传）
kueuectl describe workload sample-job -n team-a
kubectl get workload sample-job -n team-a -o yaml
```

Workload 名字通常与 Job 同名。Job 集成会给 Workload 打上 `kueue.x-k8s.io/job-uid` 等标签；不要手动创建同名 Workload 去"接管"一个 Job，集成控制器会把它当成重复对象。

### 31.2.3 暂停、恢复与停止

```bash
# 停止一个 Workload（不再排队；已准入会被驱逐）
kueuectl stop workload sample-job -n team-a

# 恢复
kueuectl resume workload sample-job -n team-a

# 停止整个 LocalQueue（只影响该队列的 Workload）
kueuectl stop localqueue team-a-queue -n team-a

# 恢复 LocalQueue
kueuectl resume localqueue team-a-queue -n team-a
```

> LocalQueue 的 `stop/resume` 在部分版本里依赖额外的 API 支持；如果 `kueuectl` 报 unknown command，直接用 `kubectl patch localqueue` 修改 `spec.stopPolicy`（见 31.4.3）。

"停止"不是删除。它保留对象和状态，适合：

- 租户临时让出容量；
- 事故期间冻结某个队列；
- 验证某个 Workload 是否是问题源。

如果要彻底不再调度，删除 Job（让集成删除 Workload）比长期 stop 更干净。

### 31.2.4 删除

```bash
# kueuectl delete 主要封装 Workload（会提示是否连带删除 owner Job）
kueuectl delete workload sample-job -n team-a

# LocalQueue/ClusterQueue 等对象用 kubectl 删除
kubectl delete localqueue team-a-queue -n team-a
```

删除 LocalQueue 前先确认没有活跃 Workload 指向它；否则这些 Workload 会停在 "LocalQueue not found" 一类的状态，需要重新创建队列或改 `queueName`（后者通常需要重建 Workload）。`kueuectl delete workload` 可能会连带删除 owner Job，先读提示再确认。

## 31.3 观察：条件和事件比状态字段更重要

Kueue 的真相在 `status.conditions` 里。常用的查询：

```bash
# QuotaReserved / Admitted 条件
kubectl get workload sample-job -n team-a \
  -o jsonpath='{range .status.conditions[*]}{.type}={.status} {.reason}{"\n"}{end}'

# 最近事件
kubectl get events -n team-a \
  --field-selector involvedObject.name=sample-job \
  --sort-by=.lastTimestamp

# 等待原因
kubectl get workload sample-job -n team-a \
  -o jsonpath='{.status.conditions[?(@.type=="QuotaReserved")].reason}{"\n"}'
```

常见组合：

| 条件组合 | 含义 | 下一步 |
|---|---|---|
| `QuotaReserved=False/Inadmissible` | 结构性不可准入（selector、队列引用） | 查 CQ/namespaceSelector |
| `QuotaReserved=False/InsufficientQuota` | 配额不足 | 查 usage/nominal、借用、抢占 |
| `QuotaReserved=True/Admitted=False` | 已预留，等 AdmissionCheck | 查 `status.admissionChecks` |
| `Admitted=True/PodsReady=False` | 已准入，Pod 未就绪 | 查 Job/Pod/节点/镜像 |
| `Evicted=True/Preempted` | 被抢占 | 查抢占者、优先级、策略 |
| `QuotaReserved=False/Reason=Inadmissible` 或 `Deactivated` | Workload 被停用/结构性不可准入 | `active` 是否为 false；看 Reason |

### 31.3.1 watch 与自定义列

```bash
# 持续观察状态变化
kubectl get workloads -n team-a -w

# 自定义列：队列、状态、预留
kubectl get workloads -n team-a \
  -o custom-columns='NAME:.metadata.name,QUEUE:.spec.queueName,RESERVED:.status.admission.clusterQueue,ADMITTED:.status.conditions[?(@.type=="Admitted")].status'
```

`status.conditions` 是数组，JSONPath 过滤在复杂场景下容易写错；需要稳定报表时优先用 `kubectl get -o json` 加 `jq`：

```bash
kubectl get workloads -n team-a -o json |
  jq -r '.items[] | [.metadata.name, .spec.queueName,
         ([.status.conditions[]? | select(.type=="Admitted") | .status][0] // "Unknown")] | @tsv'
```

## 31.4 变更操作的安全顺序

### 31.4.1 修改配额

配额变更的风险不是"改错数字"，而是"改完触发大规模抢占/驱逐"。推荐顺序：

```text
1. 记录变更前的 usage/nominal/pending
2. 在 staging 用相同清单验证
3. 先加后减：扩容直接改；缩容先 drain 或先 stop LocalQueue
4. 观察 15–30 分钟的 Evicted/Preempted 和 pending
5. 回滚清单与变更记录一起保存
```

缩小 `nominalQuota` 不会自动驱逐已准入 Workload，但会让新 Workload 进不来；缩小 `borrowingLimit`/`lendingLimit` 会影响后续借用。真正会触发驱逐的是：

- `stopPolicy: HoldAndDrain`；
- 抢占/回收策略；
- `maximumExecutionTimeSeconds`；
- WaitForPodsReady 超时；
- 管理员显式 stop/deactivate。

### 31.4.2 修改 CQ 的 stopPolicy

```bash
# 暂停新准入，但让已准入作业跑完
kubectl patch clusterqueue cluster-queue --type merge \
  -p '{"spec":{"stopPolicy":"Hold"}}'

# 连同已准入作业一起排空
kubectl patch clusterqueue cluster-queue --type merge \
  -p '{"spec":{"stopPolicy":"HoldAndDrain"}}'

# 恢复
kubectl patch clusterqueue cluster-queue --type merge \
  -p '{"spec":{"stopPolicy":"None"}}'
```

`Hold` 与 `HoldAndDrain` 的区别一定要在变更单里写清楚。`Hold` 只影响未准入/已预留未准入的 Workload；`HoldAndDrain` 会驱逐已准入 Workload，可能造成训练任务中断。

### 31.4.3 修改 LocalQueue

LocalQueue 的 `stopPolicy` 是租户级紧急刹车，只影响通过该队列提交的 Workload。平台管理员可以用它隔离问题租户，而不影响同 CQ 的其他 LocalQueue。

### 31.4.4 删除与 finalizer

Workload 可能带 finalizer。删除卡住时：

```bash
kubectl get workload sample-job -n team-a -o jsonpath='{.metadata.finalizers}'
```

先恢复控制器/集成，让 finalizer 正常走完；不要直接 `kubectl patch ... -p '{"metadata":{"finalizers":[]}}'`，否则可能留下没有清理的配额账本或集成状态。

## 31.5 自动化脚本

### 31.5.1 等待 Workload 准入

```bash
#!/usr/bin/env bash
set -euo pipefail

ns="${1:?namespace required}"
name="${2:?workload required}"
timeout="${3:-600}"

deadline=$(( $(date +%s) + timeout ))
while true; do
  admitted="$(kubectl get workload "$name" -n "$ns" \
    -o jsonpath='{.status.conditions[?(@.type=="Admitted")].status}' 2>/dev/null || true)"
  if [ "$admitted" = "True" ]; then
    echo "admitted"
    exit 0
  fi
  if [ "$(date +%s)" -ge "$deadline" ]; then
    echo "timeout waiting for $ns/$name" >&2
    kubectl get workload "$name" -n "$ns" -o yaml >&2 || true
    exit 1
  fi
  sleep 5
done
```

要点：

- `set -euo pipefail`；
- 超时必须打印现场（conditions/events）；
- 不要用固定 `sleep 60` 代替条件等待；
- 轮询间隔不要小于 2 秒，避免给 API server 加压。

### 31.5.2 配额日报

```bash
#!/usr/bin/env bash
set -euo pipefail

kubectl get clusterqueues -o json |
jq -r '
  .items[] |
  [ .metadata.name,
    ([.status.flavorsReservation[]?.resources[]? | select(.name=="cpu") | .total][0] // "-"),
    ([.status.flavorsUsage[]?.resources[]? | select(.name=="cpu") | .total][0] // "-"),
    ([.status.pendingWorkloads][0] // 0)
  ] | @tsv
' | column -t
```

v1beta2 的 ClusterQueue status 用 `flavorsReservation`（预留）和 `flavorsUsage`（使用），不是 `status.flavors[]`；每个 `ResourceUsage` 的数量字段是 `total`，借用量在可选的 `borrowed` 里。不同版本的 status 字段仍有差异；脚本要先在目标版本上 `kubectl get cq -o yaml` 确认路径，不要照搬。

### 31.5.3 抢占审计

```bash
kubectl get events -A \
  --field-selector reason=Preempted \
  --sort-by=.lastTimestamp |
tail -50
```

结合 Workload 条件：

```bash
kubectl get workloads -A -o json |
jq -r '.items[] | select(any(.status.conditions[]?; .type=="Preempted" and .status=="True")) |
  [.metadata.namespace, .metadata.name, .spec.priority, .spec.queueName] | @tsv'
```

审计要回答：谁抢了谁、在哪个 CQ/Cohort、优先级差多少、是否符合策略、是否反复发生。

### 31.5.4 幂等与 dry-run

```bash
kubectl apply --dry-run=server -f queue.yaml
kubectl diff -f queue.yaml
```

`--dry-run=server` 会经过 webhook 校验，比客户端 dry-run 更接近真实结果。批量变更前至少做一次。

## 31.6 kueuectl、kubectl 与 API 的边界

| 场景 | 推荐 |
|---|---|
| 查看 Workload/队列 | `kueuectl list` |
| 复杂字段/批量 JSONPath | `kubectl get -o json` + `jq` |
| 声明式配额 | GitOps + `kubectl apply` |
| 紧急 stop/resume | `kueuectl` |
| 跨版本迁移 | `kubectl` + API conversion 验证 |
| 审计 | API server audit log + Events |

kueuectl 不替代 API。它不提供原子事务，也不能保证"查询后变更"之间没有竞态；需要强一致的操作仍然要用 resourceVersion/乐观锁或控制器 reconcile。

## 31.7 MiniKueue 的 CLI 映射

MiniKueue 没有真实 API server，但提供了同样的操作形状：

```bash
cd mini-kueue

# 查询
go run ./cmd/minikueue -scenario basic -ticks 2 -get workloads
go run ./cmd/minikueue -scenario basic -ticks 2 -get clusterqueues
go run ./cmd/minikueue -scenario basic -ticks 2 -describe default/job-a
go run ./cmd/minikueue -scenario basic -ticks 2 -query basic

# 操作
go run ./cmd/minikueue -scenario basic -ticks 2 -stop default/job-b
go run ./cmd/minikueue -scenario basic -ticks 2 -resume default/job-b
```

映射关系：

| 真实操作 | MiniKueue |
|---|---|
| `kueuectl list workload` | `-get workloads` |
| `kubectl describe workload` | `-describe ns/name` |
| Visibility API | `-query <clusterqueue>` |
| `kueuectl stop workload` | `-stop ns/name` → `Engine.Deactivate` |
| `kueuectl resume workload` | `-resume ns/name` → `Engine.Activate` |
| `kubectl get cq/lq/flavors/topologies` | `-get clusterqueues/localqueues/flavors/topologies` |

`-describe` 会打印 conditions、admissionChecks、admission、TopologyAssignments、reclaimablePods 和 evictionStats。这正好覆盖第 10 章排障时最常看的字段（MiniKueue 内部字段名仍叫 `AdmissionChecks`）。

## 31.8 运维脚本模板

### 31.8.1 Pending 分诊

```bash
#!/usr/bin/env bash
set -euo pipefail

ns="${1:-default}"
kubectl get workloads -n "$ns" -o json |
jq -r '
  .items[]
  | select([.status.conditions[]? | select(.type=="Admitted") | .status][0] != "True")
  | [ .metadata.name,
      .spec.queueName,
      ([.status.conditions[]? | select(.type=="QuotaReserved") | .reason][0] // "Unknown"),
      ([.status.conditions[]? | select(.type=="QuotaReserved" and .reason=="Inadmissible") | .message][0] // "-")
    ] | @tsv
'
```

按 `QuotaReserved.reason` 分组，再决定是查配额、查 selector 还是查 AdmissionCheck。

### 31.8.2 每日检查清单

```text
1. 所有 CQ Active=True，REASON=Ready
2. 无 CQ 长期 usage > nominal（除非预期借用）
3. Pending 数量与上周同期相比无异常
4. Evicted/Preempted 速率无突增
5. AdmissionCheck 等待 P99 在 SLO 内
6. 控制器无重启/leader 切换
7. 关键队列的 LocalQueue 未被误 stop
8. 配额合同与实际 nominal 一致
```

### 31.8.3 变更后验证

```bash
kubectl get clusterqueues -o wide
kubectl get localqueues -A
kubectl get workloads -A -o wide
kubectl get events -A --sort-by=.lastTimestamp | tail -50
```

变更后的 30 分钟内，至少看一次这四条；不要只看 `kubectl apply` 的返回码。

## 31.9 常见错误

1. **把 stop 当 delete**：stop 后 Workload 还在，配额/队列关系还在；delete 才会触发集成清理。
2. **直接改已经准入 Workload 的资源**：集成/控制器可能拒绝或覆盖；要改就重建 Workload。
3. **在 CQ 上同时用 stopPolicy 和大量抢占**：HoldAndDrain + 抢占会让驱逐原因难以区分。
4. **脚本只判断 phase**：Kueue 没有单一 phase，必须看 conditions。
5. **轮询过密**：1 秒一次的全量 list 会给 API server 造成不必要的压力。
6. **忽略 finalizer**：强删会留下账本/集成不一致。
7. **把 kueuectl 当原子事务**：查询和变更之间可能被其他控制器改变。

## 31.10 本章小结

- kueuectl 负责"意图级"操作，kubectl 负责"字段级"操作，两者组合使用。
- 观察 Kueue 的核心是 `status.conditions`、Events、`status.admissionChecks` 和 `schedulingStats`。
- 变更配额/stopPolicy 前要评估驱逐影响；先加后减、先 staging 后生产。
- 自动化脚本要条件等待、超时打印现场、幂等、限速。
- MiniKueue 用 `-get`/`-describe`/`-stop`/`-resume`/`-query` 映射同一组操作，便于在没有集群时练习排障流程。

---

[← 上一章：从 MiniKueue 到生产控制器](30-mini-to-production-controller.md) | [返回目录](../README.md) | [下一章：高可用、灾备与一致性 →](32-high-availability-dr.md)
