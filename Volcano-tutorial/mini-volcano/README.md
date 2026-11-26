# MiniVolcano

一个不依赖 Kubernetes 的确定性 Volcano 语义模拟器：Session、Statement、23 个调度器插件、6 个 action、Job/PodGroup/Queue 控制器、Cron、JobFlow、指标、28 个自检查场景。

它用于教学与算法原型，不是生产调度器。

## 快速开始

需要 Go 1.24+（本教程在 Go 1.27 上验证）。

```bash
go test ./... -count=1
go run ./cmd/minivolcano -list
go run ./cmd/minivolcano -scenario basic
go run ./cmd/minivolcano -all
```

典型输出：

```text
PASS basic
PASS gang
PASS task-gang
PASS min-success
...
28 scenarios, 28 passed, 0 failed
```

## CLI

```bash
go run ./cmd/minivolcano -scenario basic
go run ./cmd/minivolcano -scenario priority -explain default/train-high
go run ./cmd/minivolcano -scenario proportion -what-if team-a/nvidia.com/gpu=2 -no-check
go run ./cmd/minivolcano -scenario basic -metrics -events 20
go run ./cmd/minivolcano -scenario task-gang -events 20
go run ./cmd/minivolcano -scenario min-success -events 20
go run ./cmd/minivolcano -scenario pdb -events 20
go run ./cmd/minivolcano -scenario resourcequota -events 20
go run ./cmd/minivolcano -scenario gang -config ../examples/config/minivolcano-scheduler.json
go run ./cmd/minivolcano -scenario basic -json
```

| 参数 | 说明 |
|---|---|
| `-list` | 列出场景 |
| `-scenario` | 场景名 |
| `-all` | 运行全部场景与断言 |
| `-ticks` | 覆盖 tick 数（配合 `-no-check`） |
| `-config` | 调度器 JSON 配置 |
| `-explain` | 解释作业（逗号分隔） |
| `-what-if` | 覆盖队列 capability，`queue/resource=value` |
| `-metrics` | Prometheus 文本 |
| `-events` | 最近事件数 |
| `-json` | JSON 摘要 |
| `-no-check` | 跳过场景断言 |

## 包结构

```text
cmd/minivolcano        CLI
internal/api           Resource、Job、Pod、PodGroup、Queue、HyperNode…
internal/cache         快照、绑定/驱逐/完成、预留、拓扑
internal/framework     Session、hook、tier、Statement
internal/plugins       23 个调度器插件
internal/actions       enqueue/allocate/preempt/reclaim/backfill/shuffle
internal/controller    Job/PodGroup/Queue、Cron、JobFlow
internal/scheduler     Session 编排、配置、指标
internal/sim           World（tick 驱动）
internal/scenario      28 个场景 + 测试
internal/metrics       Prometheus 文本 registry
```

## 支持的插件

`gang`、`priority`、`drf`、`proportion`、`capacity`、`predicates`、`nodeorder`、`binpack`、`task-topology`、`network-topology-aware`、`group-topology-affinity`、`tdm`、`reservation`、`sla`、`fairshare`、`numa-aware`、`overcommit`、`conformance`、`resource-strategy-fit`、`rescheduling`、`pdb`、`resourcequota`、`nodegroup`。

## 支持的 action

`enqueue`、`allocate`、`preempt`、`reclaim`、`backfill`、`shuffle`。

## 场景

| 场景 | 验证点 |
|---|---|
| `basic` | 主链路 |
| `gang` | all-or-nothing |
| `task-gang` | task 级 minTaskMember |
| `min-success` | minSuccess 提前完成 |
| `priority` | 同队列抢占 |
| `reclaim` | 跨队列回收 |
| `proportion` | 加权份额 |
| `drf` | 主导资源公平 |
| `binpack` | 装箱 |
| `topology` | HyperNode hard |
| `group-topology` | PodGroup 级拓扑反亲和 |
| `task-topology` | 角色亲和 |
| `reservation` | 时间窗预留（教学抽象） |
| `sla` | 等待预算 |
| `fairshare` | 历史用量 |
| `hierarchy` | 层级队列 |
| `numa` | 单 NUMA |
| `tdm` | 时分复用 |
| `retry` | RestartJob |
| `cron` | 周期作业 |
| `jobflow` | DAG |
| `backfill` | BestEffort |
| `heterogeneous` | A100 选择 |
| `throughput` | 批量完成 |
| `shuffle` | 低利用率节点疏散 + binpack 重打包 |
| `pdb` | PDB 允许一次中断、阻断第二次 |
| `resourcequota` | namespace 配额准入 |
| `nodegroup` | 队列限定节点组与组容量 |

## 配置

`-config` 接受 JSON（真实 Volcano 用 YAML）：

```json
{
  "actions": ["enqueue", "allocate", "preempt", "reclaim", "backfill", "shuffle"],
  "tiers": [
    {"name": "tier-1", "plugins": [{"name": "priority"}, {"name": "gang"}]},
    {"name": "tier-2", "plugins": [{"name": "predicates"}, {"name": "proportion"}]}
  ]
}
```

完整示例见 `../examples/config/minivolcano-scheduler.json`。

## 测试

```bash
gofmt -l .
go vet ./...
go build ./...
go test ./... -count=1
go test ./... -count=1 -v | grep -c '^--- PASS'   # 129
go run ./cmd/minivolcano -all                     # 28 passed
```

`-race` 需要 CGO，当前 Windows 环境未运行。

## 限制

- 不是 CRD，不兼容真实 API；
- 无 informer/API Server/并发；
- 无 DRA/MIG/vGPU/多集群/安全；
- `Reservation` 是教学抽象，真实 Volcano 无此 CRD；
- Cron 用 tick，JobFlow 内联 JobSpec；
- TDM 不自动驱逐窗口结束后的 Pod；
- 实现了 task 级 `minTaskMember` 与 `minSuccess`；不实现 `dependsOn`、作业级 `volumes`、Queue affinity、`subGroupPolicy`/`partitionPolicy` 与作业插件。
- 有简化的 PDB/resourcequota/nodegroup/group-topology-affinity；仍缺 usage/deviceshare/extender、gangPreempt/gangReclaim、cdp。
- `group-topology-affinity` 用数字 tier 而不是上游的 `topologyTierName`；默认同 namespace；只在 allocate 生效；没有实现完整 CRD 校验。
- PDB 没有 `disruptionsAllowed`/Eviction API；ResourceQuota 只在 enqueue 检查已准入作业；NodeGroup 用类型字段而非上游注解。

详见教程第 24、25 章。

`shuffle` 只做“按插件提名驱逐”，真正的重打包由下一轮 `allocate` 完成；`rescheduling` 提供 `lowNodeUtilization`（负载均衡）与 `fragmentation`（教学碎片整理）两种受害者策略，但不含真实 Volcano 的五分钟冷却与分维阈值配置。
