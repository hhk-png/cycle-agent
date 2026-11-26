# 第 16 章 可观测性与运维

## 16.1 三个问题

运维 Volcano 时，你反复回答三个问题：

1. **作业为什么 Pending？**
2. **调度器现在健康吗？**
3. **资源分配符合配额设计吗？**

对应的工具是：事件/状态、指标、日志/追踪。

## 16.2 状态与事件

### 16.2.1 三层状态

```bash
kubectl get vcjob <job> -o yaml
kubectl get podgroup <pg> -o yaml
kubectl get queue <q> -o yaml
kubectl get pods -l volcano.sh/job-name=<job> -o wide
```

要看的不只是 phase，还有：

- `status.conditions` 的 reason/message；
- `status.running/succeeded/failed`；
- PodGroup 的 `minMember/minResources`；
- Queue 的 `allocated/deserved/capability`；
- Pod 的 `schedulerName`、`nodeName`、events。

### 16.2.2 事件

```bash
kubectl get events --sort-by=.lastTimestamp | tail -50
kubectl get events --field-selector involvedObject.name=<job>
kubectl get events --field-selector reason=Evicted
```

Volcano 的关键事件：

- `Inqueue`、`Scheduled`、`Allocated`；
- `Evicted`、`Reclaimed`；
- `RestartJob`、`RestartTask`；
- `Completed`、`Failed`、`Aborted`；
- `SLAViolated`（如果启用 SLA 插件）。

### 16.2.3 describe 的顺序

```bash
kubectl describe vcjob <job>
kubectl describe podgroup <pg>
kubectl describe pod <pod>
kubectl describe queue <q>
```

先 Job 后 PodGroup 后 Pod，最后 Queue。大多数“不调度”能在这个顺序里定位。

## 16.3 指标

### 16.3.1 暴露方式

Volcano 组件通常通过 `/metrics` 暴露 Prometheus 指标。端口/Service 名称随 Helm chart 版本变化：

```bash
kubectl -n volcano-system get svc
kubectl -n volcano-system port-forward svc/volcano-scheduler 8080:8080
curl localhost:8080/metrics | grep '^volcano_'
```

具体端口以 `values.yaml`/Service 为准。

### 16.3.2 常见指标族

以下名称来自 v1.16.0 的 `pkg/scheduler/metrics`，**仍以实际 `/metrics` 为准**（发行版可能增加/改名）：

| 指标 | 类型 | 含义 |
|---|---|---|
| `volcano_schedule_attempts_total{result}` | counter | 调度尝试数；`result` 区分成功/不可调度/错误 |
| `volcano_e2e_scheduling_latency_milliseconds` | histogram | 单次 Session 端到端耗时 |
| `volcano_open_session_duration_milliseconds` | histogram | 打开 Session（快照+初始化）耗时 |
| `volcano_plugin_scheduling_latency_milliseconds` / `volcano_action_scheduling_latency_milliseconds` | histogram | 插件/Action 耗时 |
| `volcano_queue_allocated_{milli_cpu,memory_bytes,scalar_resources}` | gauge | 队列已分配资源；标签 `queue_name`、`resource` |
| `volcano_queue_deserved_{...}` / `volcano_queue_capacity_{...}` / `volcano_queue_inqueue_{...}` | gauge | 应得/上限/排队需求 |
| `volcano_eviction_transactions_total{action}` | counter | 已提交的驱逐事务；`action` 为 `preempt`/`reclaim`/`gangpreempt`/`gangreclaim` |
| `volcano_job_retry_counts{job_id}` | counter | 作业重试次数 |
| `volcano_pod_preemption_victims` | gauge | 最近一次抢占选中的受害者数 |
| `volcano_namespace_share` / `volcano_namespace_decayed_usage` / `volcano_namespace_weighted_share` | gauge | fairshare 份额与衰减用量（v1.16） |
| `volcano_queue_share` / `volcano_job_share` | gauge | DRF/份额 |
| `volcano_unschedulable_job_cache_{skips,wakeups,watchdog_expirations}_total` | counter | Unschedulable Job Cache 调试指标，默认关闭 |

注意：没有通用的 `volcano_job_pending`、`volcano_podgroup_*` 或 `volcano_scheduler_session_total`。作业/PodGroup 的状态计数要么从 `kubectl get`/kube-state-metrics 取，要么用队列的 `inqueue` 资源指标间接观察。

### 16.3.3 建议的告警

| 告警 | 条件（示意） | 含义 |
|---|---|---|
| 调度器无尝试 | `rate(volcano_schedule_attempts_total[5m]) == 0` 且有待调度 Pod | 调度器卡死/未 leader |
| 调度错误率高 | `rate(volcano_schedule_attempts_total{result="error"}[5m]) > 0` | 配置/资源/内部错误 |
| Inqueue 资源持续上涨 | `sum(volcano_queue_inqueue_scalar_resources{resource="nvidia.com/gpu"})` 持续上升 | 容量不足或故障 |
| 队列长期低于 deserved | `allocated/deserved < 0.5` 持续 30m | 饥饿/配额错误 |
| 抢占/回收风暴 | `rate(volcano_eviction_transactions_total[5m]) > N` | 抖动 |
| Webhook 错误 | admission 5xx | 证书/副本问题 |

PromQL 的具体指标名要按你的版本调整；不要直接复制到生产。

## 16.4 日志

```bash
kubectl -n volcano-system logs deploy/volcano-scheduler --tail=500
kubectl -n volcano-system logs deploy/volcano-controllers --tail=500
kubectl -n volcano-system logs deploy/volcano-admission --tail=500
```

调度器日志常见关键词：

- `preempt`、`reclaim`、`evict`；
- `gang`、`minAvailable`；
- `queue`、`overused`；
- `predicate`、`node`；
- `bind`。

建议：

- 集中采集（Loki/ELK）；
- 按 job/queue 建索引；
- 对 ERROR 级别告警；
- 保留至少 7 天，便于复盘。

## 16.5 可视化

- **Volcano Dashboard**（v0.2，v1.14）：PodGroup 全景、Job/Queue 生命周期管理；
- Grafana：调度延迟、队列份额、Pending 趋势；
- 自建：从 CRD status 生成租户视图。

Dashboard 的版本与主版本要匹配，避免 CRD schema 不兼容。

## 16.6 Benchmark

v1.15 引入 Volcano Benchmark 框架：

- 一键在 Kind/KWOK 或现有集群上搭建压测环境；
- 生成作业负载；
- 输出调度吞吐、延迟、公平性报告。

使用场景：

- 升级前回归；
- 调参（binpack 权重、tier 顺序）；
- 容量规划；
- 对比不同调度器。

注意：KWOK 只模拟调度，不跑真实容器；真实 GPU 性能要另测。

## 16.7 SLO 与容量

### 16.7.1 调度 SLO

| SLO | 定义 | 目标示例 |
|---|---|---|
| 调度延迟 | 从 Pod 创建到绑定的 P95 | < 5s |
| 入队延迟 | 从 Job 创建到 PodGroup Inqueue 的 P95 | < 10s |
| 高优先级启动 | 高优先级作业从提交到 Running 的 P95 | < 2min |
| 公平偏差 | 队列 allocated/deserved 的 1h 均值偏差 | < 20% |
| 抢占率 | 每小时被驱逐的作业数 | 业务可接受 |

### 16.7.2 容量

不要只看 GPU 利用率。建议同时看：

- 可调度 gang 的最大尺寸；
- 碎片率（空闲但无法满足大 gang 的比例）；
- 队列 borrow/lend 的时长；
- 因配额被拒的作业数；
- 节点故障域冗余。

## 16.8 运维 Runbook

### 16.8.1 Pending 激增

1. `kubectl get vcjob -A` 看 Pending 作业；
2. 抽查一个：schedulerName、queue state、PodGroup phase；
3. 对比队列 allocated/capability；
4. 看节点空闲形状与污点；
5. 看调度器日志的 predicate/queue 原因；
6. 判断是容量、配额、拓扑还是故障。

### 16.8.2 调度延迟升高

1. 看 session 数与 action 耗时；
2. 是否有大量作业/节点导致 cache 变大；
3. 是否有插件做重计算（fairshare/拓扑）；
4. 是否开启 unschedulable job cache（v1.16）；
5. 考虑动态节点分片（v1.14+）。

### 16.8.3 抢占风暴

1. 统计 Evicted 事件；
2. 看是否两个队列互相回收；
3. 检查 guarantee/deserved/reclaimable；
4. 检查优先级是否配置错误；
5. 临时关闭 preempt/reclaim action（重启 scheduler）止血；
6. 长期：调整配额或启用 gangPreempt。

### 16.8.4 Webhook 故障

1. `kubectl get pods -n volcano-system`；
2. 证书是否过期；
3. Service/Endpoints 是否正常；
4. failurePolicy 是否阻断提交；
5. 回滚/重启 admission。

### 16.8.5 重调度/碎片整理异常

症状：

- 作业反复被驱逐，但集群利用率没有上升；
- shuffle 事件频繁，节点布局来回变化；
- rescheduling 提名了节点，但 Pod 一直 Pending。

排查：

1. 看 `Shuffled`/`Evicted` 事件的时间分布，判断是周期性还是持续；
2. 对比驱逐前后的节点布局：Pod 是否被放回原节点；
3. 检查 `rescheduling` 的阈值与 `allocate` 的打分方向是否冲突；
4. 检查受害者是否被 gang/PDB 保护（保护过多会导致“提名了但没驱逐”）；
5. 检查被驱逐 Pod 的 `PodEvicted` 策略，避免 `RestartJob` 放大影响；
6. 如果是训练作业，确认是否有 checkpoint，否则重调度代价可能高于收益。

止血：

- 临时从 `actions` 移除 `shuffle`；
- 调低 `rescheduling` 频率/阈值；
- 对关键队列关闭重调度，只保留手动运维。

### 16.8.6 复盘：一次 guarantee 配置导致的 Pending 风暴

**时间线**

```text
09:00  平台把 dev 队列的 guarantee 从 0 调到 120（集群 256 卡）
09:03  多个训练队列开始出现 Inqueue 但不分配
09:10  Pending 作业从 12 涨到 87，告警触发
09:20  值班确认 scheduler 正常、节点有空闲
09:35  发现所有队列的 guarantee 之和 = 280 > 256
10:05  回滚 dev 的 guarantee，作业恢复
```

**证据链**

```bash
kubectl get queue -o custom-columns='NAME:.metadata.name,GUARANTEE:.spec.guarantee,ALLOC:.status.allocated,CAP:.spec.capability'
kubectl get podgroup -A | awk '$4=="Inqueue"'
kubectl -n volcano-system logs deploy/volcano-scheduler | grep -i 'guarantee\|overused\|allocatable'
```

关键现象：

- 节点 `allocatable` 还有空闲，但 PodGroup 停在 Inqueue；
- 队列的 `allocated` 没有超 `capability`，但 `guarantee` 总和超过集群容量；
- scheduler 日志没有崩溃，只是反复记录“队列不可分配/超出保证”。

**根因**

guarantee 的语义是“其他队列不能占用”。当所有队列的 guarantee 之和超过集群容量时，调度器无法同时兑现这些保底，结果是谁都拿不到完整份额。它不是“配置得越大越安全”，而是“把未来锁死”。

**修复**

1. 立即回滚 dev 的 guarantee；
2. 给 Queue 准入加校验：`Σ guarantee ≤ 可用容量 × 0.9`；
3. 把 guarantee 变更纳入配额评审，要求提交容量计算；
4. 增加告警：`sum(volcano_queue_guarantee) / cluster_capacity > 0.9`。

**预防**

- 用 GitOps 管理 Queue，变更走 PR；
- 在 staging 用相同队列配置跑一遍 `-what-if`/容量脚本；
- 每次调整后观察 30 分钟的 Inqueue 数与 `allocated/deserved`；
- 保留“一键回滚上一个 Queue 版本”的路径。

这个案例的教训不是“不要调 guarantee”，而是：**调度器不会报错，它只会安静地不调度**。可观测性和准入校验必须补在它前面。

## 16.9 升级与回滚

升级顺序（一般）：

1. 备份 etcd/CRD/配置；
2. 读 release notes 与 CRD diff；
3. 升级 CRD；
4. 升级 controller manager；
5. 升级 scheduler；
6. 升级 admission；
7. 验证默认队列、示例作业、指标。

回滚：

- Helm rollback；
- CRD 回滚要谨慎（可能丢字段）；
- 保留旧镜像 tag。

## 16.10 MiniVolcano 的可观测性

MiniVolcano 提供：

- `-metrics`：Prometheus 文本；
- `-events N`：最近事件；
- `-explain job`：证据链；
- `-what-if queue/resource=value`：反事实。

指标示例：

```text
minivolcano_scheduler_sessions_total
minivolcano_scheduler_events_total{type="Scheduled"}
minivolcano_queue_allocated{queue="team-a",resource="nvidia.com/gpu"}
minivolcano_jobs{phase="Running"}
minivolcano_node_idle{node="gpu-node",resource="nvidia.com/gpu"}
```

这些指标不是要替代真实 Volcano 的指标，而是演示“调度器应该暴露什么”。

## 16.11 一组可落地的告警与仪表盘

指标名会随版本变化，但告警的“问题结构”是稳定的。下面给出一组起点，落地时先把指标名替换成你环境 `/metrics` 里实际存在的。

### 16.11.1 必须告警的四类问题

| 类别 | 症状 | 告警条件（示意） | 处理 |
|---|---|---|---|
| 调度器停摆 | 没有新调度尝试、Pending 持续上涨 | `rate(volcano_schedule_attempts_total[5m]) == 0` 且 Pending > 0 | 看 leader、Pod 状态、日志 |
| 调度延迟恶化 | P99 绑定延迟升高 | `histogram_quantile(0.99, rate(volcano_e2e_scheduling_latency_milliseconds_bucket[5m])) > 5` | 看节点/Pod 规模、插件耗时、API Server |
| 公平性失衡 | 某队列长期低于 deserved | `allocated/deserved < 0.5` 持续 30m | 看 Pending、受害者保护、配额 |
| 抖动 | 驱逐速率异常 | `rate(volcano_eviction_transactions_total[10m]) > N` 持续 30m | 看回收/抢占/重调度配置 |

### 16.11.2 PrometheusRule 示例

```yaml
apiVersion: monitoring.coreos.com/v1
kind: PrometheusRule
metadata:
  name: volcano-slo
  namespace: volcano-system
spec:
  groups:
    - name: volcano.scheduler
      rules:
        - alert: VolcanoSchedulerNoAttempts
          expr: |
            rate(volcano_schedule_attempts_total[5m]) == 0
            and on() (sum(kube_pod_status_phase{phase="Pending"}) > 0)
          for: 10m
          labels:
            severity: critical
          annotations:
            summary: "Volcano scheduler is not attempting to schedule while pods are pending"

        - alert: VolcanoHighSchedulingLatency
          expr: |
            histogram_quantile(0.99,
              sum by (le) (rate(volcano_e2e_scheduling_latency_milliseconds_bucket[5m]))
            ) > 5000
          for: 10m
          labels:
            severity: warning
          annotations:
            summary: "P99 scheduling latency above 5s"

        - alert: VolcanoQueueStarved
          expr: |
            (sum by (queue_name) (volcano_queue_allocated_milli_cpu)
              / sum by (queue_name) (volcano_queue_deserved_milli_cpu)) < 0.5
            and on(queue_name)
            (sum by (queue_name) (volcano_queue_deserved_milli_cpu) > 0)
          for: 30m
          labels:
            severity: warning
          annotations:
            summary: "Queue {{ $labels.queue_name }} is far below its deserved share"

        - alert: VolcanoEvictionStorm
          expr: rate(volcano_eviction_transactions_total[10m]) > 5
          for: 30m
          labels:
            severity: warning
          annotations:
            summary: "Eviction rate is abnormally high"
```

三个注意点：

1. **`and on()` 的写法**依赖 Prometheus 版本；先在测试环境验证表达式。
2. **Pending 条件依赖 kube-state-metrics**（`kube_pod_status_phase`）。没有 KSM 时，改成 `sum(volcano_queue_inqueue_milli_cpu) > 0`——它衡量排队资源，不是 Pod 个数。
3. **不要对“Pending > 0”直接 page**。Pending 是正常排队；要 page 的是“调度器不动”或“SLO 燃烧”。
4. **阈值要按队列/业务分层**。训练队列可以等 30 分钟，推理队列不能。

### 16.11.3 仪表盘布局

一个够用的 Grafana 看板分四行：

```text
第一行：健康
  - scheduler/controller/admission up
  - 调度尝试速率、错误率、leader
第二行：吞吐与延迟
  - 每秒绑定 Pod 数
  - 调度延迟 P50/P95/P99
  - 各 action 耗时
第三行：队列与公平
  - 每队列 allocated/deserved/capability
  - Pending 作业数（按队列/优先级）
  - 借用/回收事件
第四行：异常
  - Evicted/Reclaimed/Shuffled 速率
  - webhook 5xx
  - 节点 NotReady 数
```

### 16.11.4 记录规则

把复杂查询固化成 recording rule，避免每次刷新都扫原始指标：

```yaml
groups:
  - name: volcano.recording
    rules:
      - record: volcano:queue_share:ratio
        expr: |
          sum by (queue_name) (volcano_queue_allocated_milli_cpu)
          / sum by (queue_name) (volcano_queue_deserved_milli_cpu)
      - record: volcano:scheduling_latency:p99
        expr: |
          histogram_quantile(0.99,
            sum by (le) (rate(volcano_e2e_scheduling_latency_milliseconds_bucket[5m]))
          )
```

这样告警和看板用同一套口径，减少“看板正常、告警在响”的争议。

### 16.11.5 指标基数控制

调度器指标最容易踩的坑是标签爆炸：

- 不要给每个 Pod/Job 都打 label；用聚合计数。
- 队列 label 数量可控（几十个），作业 label 不可控（上万）。
- 对高基数维度做 recording rule 聚合，原始指标只保留短保留期。
- 如果必须按作业追踪，用日志/追踪，不要用 Prometheus。

## 16.12 本章小结

- 排障顺序：Job → PodGroup → Pod → Queue → 日志。
- 指标关注会话、分配错误、延迟、队列份额、Pending/Evicted。
- SLO 要覆盖延迟、公平、抢占率，而不只是利用率。
- Benchmark 与 Dashboard 是 v1.14/v1.15 之后的运维利器。
- MiniVolcano 用 explain/metrics/what-if 演示了可观测性的核心。

下一章讲安全与多租户：当多个团队共享一个集群时，边界在哪里。
