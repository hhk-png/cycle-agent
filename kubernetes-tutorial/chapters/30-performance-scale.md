# 第 30 章 性能与规模化

## 30.1 规模维度

“大集群”不只是节点数：

- 节点数。
- Pod 数。
- API 对象总数（ConfigMap/Secret/Event 也会压 etcd）。
- watch 客户端数量。
- 每秒钟变更数（churn）。
- 服务/Endpoint 数量。
- CRD 数量。
- 自定义控制器数量。

一个 100 节点但每秒创建/删除上千 Pod 的集群，可能比 5000 节点稳定集群更难。

## 30.2 API Server

瓶颈：

- 认证/授权（webhook 调用）。
- 准入 webhook。
- JSON 编解码。
- etcd 读写。
- watch 事件分发。
- 请求限流/排队。

优化：

- 减少 webhook 调用（缓存、合并、避免外部依赖）。
- 控制对象大小（不要塞大 ConfigMap/annotations）。
- 使用 protobuf（控制器/客户端）。
- 合理分页/list limit。
- 使用 APF 保护关键流量。
- 避免高频全量 List。
- 使用 informer 缓存。

关键指标：

- `apiserver_request_duration_seconds`
- `apiserver_request_total{code}`
- `apiserver_current_inflight_requests`
- `apiserver_flowcontrol_*`
- `etcd_request_duration_seconds`

## 30.3 etcd

瓶颈：

- 磁盘 fsync 延迟。
- 写放大。
- 大对象。
- 历史版本未 compaction。
- 碎片。
- 网络延迟（Raft）。

优化：

- 专用 SSD/NVMe。
- 控制对象大小（<100KB 较安全，硬上限约 1.5MiB）。
- 定期 compaction/defrag。
- 避免高频更新同一对象（例如 status 每秒写）。
- 监控 DB 大小、碎片、fsync、leader 变化。
- 大规模考虑**拆分集群**（按租户/环境/地域），而不是“给 etcd 分片”。Kubernetes 不会把一个 API Server 的 etcd 自动分片；每个集群有自己的 etcd，规模上限靠减少对象/写入和拆集群解决。

关键指标：

- `etcd_server_leader_changes_seen_total`
- `etcd_disk_wal_fsync_duration_seconds`
- `etcd_disk_backend_commit_duration_seconds`
- `etcd_mvcc_db_total_size_in_bytes`
- `etcd_server_has_leader`

## 30.4 Watch 与控制器

问题：

- 每个控制器 List/Watch 全量对象。
- 大量 CRD 控制器重复 watch。
- 高频 status 更新导致事件风暴。
- 控制器串行处理慢。

优化：

- 使用 informer + 索引。
- 只 watch 需要的 namespace/label。
- 合并状态更新（避免每 tick 写）。
- 限速重试。
- 分片控制器（按 namespace/对象哈希）。
- 避免在控制器中做慢外部调用。

MiniK8s 的控制器每 50–250ms 全量 List，适合教学，不适合大规模。真实控制器用缓存和事件驱动。

## 30.5 调度器

瓶颈：

- 节点数 × Pod 数的过滤。
- PodAffinity/TopologySpread 组合。
- 抢占计算。
- Binding 写 QPS。

优化：

- 控制 PodAffinity 使用。
- `percentageOfNodesToScore` 限制打分比例。
- 调度器 profile/分片。
- 避免频繁创建大量小 Pod。
- 使用 PriorityClass 让关键 Pod 先调度。

## 30.6 kubelet 与节点

瓶颈：

- PLEG（Pod Lifecycle Event Generator）延迟。
- 容器运行时操作（创建/删除）。
- 镜像拉取。
- 卷挂载。
- 日志写入。
- 节点上的 Pod 密度。

优化：

- 使用高性能运行时/存储。
- 预拉镜像。
- 控制 Pod 密度。
- 日志轮转。
- 监控 PLEG/运行时延迟。
- 避免频繁探针（periodSeconds 太小）。

## 30.7 网络

瓶颈：

- iptables 规则数量。
- conntrack 表。
- Service/Endpoint 数量。
- 跨节点流量。
- overlay MTU。
- DNS QPS。

优化：

- eBPF/IPVS。
- 减少 Service 数量/使用 Gateway。
- 本地流量优先（topology-aware routing）。
- DNS 缓存/NodeLocal DNSCache。
- 合理 MTU。
- 监控丢包/重传。

## 30.8 存储

瓶颈：

- CSI attach/detach 限流。
- 云盘 IOPS/吞吐。
- 文件系统元数据。
- 快照/克隆。
- 节点挂载数限制。

优化：

- 选择合适的卷类型。
- 控制每节点卷数量。
- 使用本地缓存/分层存储。
- 监控卷使用率和延迟。
- 避免大量小文件。

## 30.9 对象与标签设计

- 不要让每个 Pod 的 label 包含高基数随机值（影响选择器和指标）。
- 不要用 annotations 存大配置。
- 控制 Event 频率（重复事件会聚合，但仍占资源）。
- 控制 CRD 数量。
- 控制 Secret/ConfigMap 数量。
- 使用 namespace 分片。

## 30.10 性能测试

方法：

1. 定义 SLO（API 延迟、调度延迟、Pod 启动时间）。
2. 建立基线（小规模）。
3. 逐步增加负载（Pod churn、对象数量、watch 客户端）。
4. 观察瓶颈（API/etcd/调度/网络/存储）。
5. 优化一个变量，重复。
6. 记录结果和配置。

工具：

- kube-burner：大规模创建/删除对象。
- clusterloader2：Kubernetes 官方规模测试。
- kubemark：模拟 kubelet，测试控制面规模。
- Prometheus + Grafana。
- etcd 自带 benchmark。

不要在生产集群做破坏性压测。

## 30.11 官方规模目标（参考）

Kubernetes 社区有规模 SLO（例如 5000 节点、15 万 Pod），但：

- 取决于版本、配置、工作负载。
- 不是“开箱即用”的保证。
- 需要调优 etcd/API Server/控制器。
- 网络/存储/云厂商限制可能先成为瓶颈。

把官方数字当作参考，不要当作承诺。

## 30.12 规模化检查清单

- etcd 专用盘、监控 fsync/DB 大小/碎片。
- API Server 多实例 + APF。
- 控制面 webhook 低延迟/高可用。
- 控制器使用 informer/缓存/限速。
- 避免高频 status 写入。
- 控制对象大小和数量。
- 网络数据面选择（iptables vs eBPF）。
- DNS 缓存。
- 节点池/调度策略。
- 监控高基数指标。
- 定期压测和容量规划。

## 30.13 规模压测方案：从基线到拐点

压测的目标不是“跑出一个漂亮数字”，而是找到瓶颈和拐点，并验证控制面在故障时仍可恢复。

### 分层压测

```text
1. etcd 基线：fio + etcd benchmark，测 fsync/吞吐
2. API Server：单资源 CRUD/list/watch 的延迟与 QPS
3. 调度：大量 Pending Pod 的调度吞吐
4. 控制器：Deployment/Job 大规模收敛
5. 数据面：Service/DNS/网络吞吐
6. 端到端：混合工作负载 + 故障注入
```

不要一开始就跑“5000 节点”。先在小规模建立基线，再逐步放大。

### kube-burner 示例

```yaml
# kube-burner 配置（示意）
jobs:
  - name: cluster-density
    jobIterations: 1000
    qps: 50
    burst: 100
    objects:
      - objectTemplate: deployment.yml
        replicas: 2
      - objectTemplate: service.yml
        replicas: 1
```

关注：

- 创建/删除耗时分布，不只是平均值。
- API Server 5xx/429、APF 排队。
- etcd fsync/DB 大小/leader 变化。
- 调度 Pending 时长。
- 控制器 reconcile 延迟/队列深度。
- 节点 kubelet PLEG/运行时延迟。

### 拐点判据

- API 延迟 P99 超过 SLO（例如 1s）。
- 错误率/限流比例持续上升。
- etcd fsync P99 超过磁盘能力。
- 调度队列持续增长。
- 控制器队列积压不下降。

找到拐点后，固定其他变量，只改一项（对象大小、watch 数、控制器并发、API 副本），验证是否改善。

### 故障注入

- 杀掉一个 API Server/etcd 成员，观察恢复时间。
- 注入网络延迟/丢包。
- 模拟 webhook 超时。
- 模拟节点 NotReady/大量 Pod 驱逐。
- 模拟镜像仓库不可用。

压测环境要与生产隔离，避免影响真实业务；结果要记录版本、配置、硬件和负载模型，否则不可复现。

MiniK8s 的规模在数百对象级别，适合验证语义而不是性能；这一节用于真实集群的容量规划。

## 30.14 控制面容量模型：从指标到节点数

容量规划不是“官方说 5000 节点，所以我们也能”。要把工作负载的变更率、对象数、watch 数和控制面资源放在一个模型里。

### 30.14.1 控制面的请求预算

API Server 的 CPU 主要花在：

- TLS/认证/授权。
- JSON/protobuf 编解码。
- 准入 webhook（网络等待）。
- etcd 读写。
- watch 事件分发。
- 审计写入。

一个粗略的请求成本模型：

```text
CPU_required ≈ QPS_read × cost_read + QPS_write × cost_write + watch_fanout
```

`cost_write` 通常远高于 `cost_read`，因为要经过准入、etcd 事务、watch 通知。具体系数必须在你的对象大小/准入链/版本上实测。

### 30.14.2 etcd 的写路径

每次写都要：

1. API Server 序列化对象。
2. etcd 提议（Raft）。
3. 多数派 fsync 到 WAL。
4. 应用到状态机、返回 revision。
5. API Server 更新 watch cache、通知 watcher。

因此：

- **fsync 延迟**是写 QPS 的硬约束。云盘/网络存储的 p99 fsync 比吞吐更重要。
- 对象越大，序列化/网络/存储成本越高；大 ConfigMap/Secret/CRD 会放大写成本。
- 同一对象的频繁 status 更新会产生大量 revision；控制器应合并/限流。
- compaction/defrag 不及时会导致 DB 膨胀、查询变慢。

### 30.14.3 watch 与 LIST 风暴

- 每个 controller/agent 的 informer 都会 LIST + WATCH。
- LIST 是重操作：大对象、全 namespace、无分页会压垮 API Server/etcd。
- 大量短命 watch（例如每次请求都 watch）会消耗连接/内存。
- 控制器重启会触发全量 LIST；滚动重启大量控制器会形成“LIST 风暴”。

治理：

- 用 informer 缓存，不要每次 reconcile 都 LIST。
- 限制 LIST 的 namespace/label/field。
- 分页 `limit` + `continue`，避免一次返回巨量对象。
- 控制器分片，降低单个 LIST 的范围。
- 对关键控制器用 APF 保护。

### 30.14.4 队列模型：为什么 p99 会突然变差

API Server 可以近似为一个排队系统：

- 到达率 λ（QPS）。
- 服务率 μ（每核可处理的 QPS）。
- 利用率 ρ = λ / μ。

当 ρ 接近 1，排队时间非线性上升。这就是“平时 50ms，突发到 500ms”的原因。扩容要在 ρ 达到 0.7–0.8 前做，而不是等 p99 爆掉。

### 30.14.5 一个 worked example（假设）

假设：

- 1000 节点、30,000 Pod。
- 每节点 kubelet 每 10s **检查**一次 Node status，但未变化的状态默认每 5 分钟（`nodeStatusReportFrequency`）才上报一次；Pod status 在变化时写。
- 20 个控制器，平均每秒 200 次写。
- 1000 个 informer watch。
- API Server 单核可持续 300 读 QPS / 80 写 QPS（示例值，必须实测）。

估算写压力（稳态）：

```text
kubelet node status: 1000 / 300s ≈ 3.3 写/s
pod status/事件（假设）:        100 写/s
controllers:                    200 写/s
其他（HPA/Job/Event）:           50 写/s
合计:                        ≈ 353 写/s
所需核数 ≈ 353 / 80 ≈ 4.4 核（仅写路径）
```

**不要用“1000 / 10s = 100 写/s”估算 kubelet 稳态写入**：那是“每次检查都写”的最坏突发。真正的主导项通常是 Pod status 变化、事件和控制器写入；用实际指标（`apiserver_request_total` 按 verb/resource、etcd fsync）校准，而不是拍脑袋乘节点数。再叠加读/watch/准入/审计，实际可能需要 8–16 核，并留 2x 余量。etcd 需要评估 fsync 能否支撑 p99。

### 30.14.6 规模化检查点

| 规模信号 | 可能瓶颈 | 先做什么 |
|---|---|---|
| LIST 延迟上升 | etcd/序列化/网络 | 分页、减少对象大小、控制器分片 |
| 写 p99 上升 | etcd fsync/raft | 专用盘、减少写、compaction |
| watch 延迟/断连 | API Server 分发/慢消费者 | 限制 watch、优化控制器、扩容 |
| 调度 Pending | 调度器/节点资源 | 打分比例、队列、节点池 |
| 节点 NotReady 增多 | kubelet/运行时/网络 | PLEG、运行时、心跳参数 |
| 429 增多 | APF/限流 | 调整 FlowSchema、隔离客户端 |
| 内存上涨 | watch cache/对象数 | 减少对象、分片、扩容 |

### 30.14.7 压测的正确姿势

- 先建立基线：空集群、固定对象大小、固定 QPS。
- 分层压：API 写、LIST、watch、调度、kubelet 上报。
- 逐步加负载，记录 p50/p95/p99、错误率、CPU/内存/etcd fsync。
- 找到拐点后，回退到 70% 负载做稳定性测试。
- 注入故障（etcd leader 切换、节点失联）看恢复时间。
- 用生产同构的对象大小/标签基数/准入链，否则结果不可迁移。

### 30.14.8 API Server 与 etcd 的关键旋钮

调优前先明确：大多数“API Server 慢”不是靠加副本解决，而是减少写放大、控制对象大小、隔离客户端。下面这些旋钮应按“一次只改一个、测量 p99、可回滚”的顺序使用。

| 旋钮 | 默认/常见值 | 作用 | 风险 | 观察指标 |
|---|---|---|---|---|
| `--max-requests-inflight` | 400 | 非变更请求总并发上限（APF 之前的总闸门） | 太低会 429；太高会把 etcd/CPU 打满 | `apiserver_request_total{code="429"}`、排队时长 |
| `--max-mutating-requests-inflight` | 200 | 变更请求总并发上限 | 同上；写路径更容易压垮 etcd | 写 p99、etcd fsync |
| `--watch-cache-sizes` | 按资源有内部默认 | 指定资源 watch cache 大小（如 `pods#5000`） | 内存上涨；过大导致 GC/初始化慢 | `apiserver_cache_list_total`、进程 RSS |
| `--goaway-chance` | 0 | 主动让客户端重连，均衡长连接 | 过于频繁会造成重连风暴 | watch 断连率、重连 QPS |
| `--min-request-timeout` | 1800s | watch 请求最短超时 | 太长会让慢消费者占用资源 | 活跃 watch 数、内存 |
| `--event-ttl` | 1h | Event 保留时间 | 太长会堆积大量小对象 | Event 数量、etcd 大小 |
| etcd `--quota-backend-bytes` | 2GiB | etcd 数据库配额 | 超过后集群进入只读/告警 | `etcd_mvcc_db_total_size_in_bytes` |
| etcd `--auto-compaction-retention` | 0（关闭） | 自动 compaction 窗口 | 不 compaction 会持续增长；太激进增加 I/O | compaction 失败、DB 大小 |
| etcd `--max-request-bytes` | 1.5MiB | 单个请求上限 | 大对象（Secret/CRD）可能被拒 | 413 错误、对象大小分布 |

watch cache 内存的粗算：

```text
内存 ≈ 缓存对象数 × 单对象序列化大小 × 副本/索引开销
例如 50,000 Pods × 8KB × 2.5 ≈ 1GB
```

这只是量级估算；对象标签、注解、managedFields 会让实际值更大。用 `kubectl get --raw /metrics` 的 cache 指标和进程 RSS 校准，不要凭对象数拍脑袋。

etcd 侧还要区分“容量”和“延迟”：`--quota-backend-bytes` 管的是能存多少，fsync 管的是写多快。磁盘 IOPS 不足时，加 API Server 副本只会把更多请求压到同一个 etcd。变更前记录基线（写 QPS、p99、fsync、DB 大小、compaction 历史），变更后至少观察一个完整 compaction 周期。

## 本章小结

- 规模是多维的：节点、Pod、对象、churn、watch、控制器。
- etcd 和 API Server 是控制面主要瓶颈；网络/存储是数据面瓶颈。
- informer、缓存、限速、对象大小控制是通用优化。
- 官方规模数字是参考，实际要压测和调优。
- 压测要分层、找拐点、做故障注入，并记录可复现的配置与负载模型。
- 容量模型从写 QPS/fsync/watch/LIST 推导，不要直接套官方节点数；p99 在利用率接近 1 时会非线性恶化。

## 延伸阅读

- [第 2 章 架构总览](02-architecture.md)
- [第 27 章 可观测性](27-observability.md)
- [第 31 章 故障排查方法论](31-troubleshooting.md)
- [第 22 章 GPU、加速器与 AI 推理平台](22-gpu-ai-inference.md)
- [附录 K etcd 深入](../appendices/K-etcd-operations.md)：容量、fsync 与瓶颈定位。
- [附录 N 成本工程](../appendices/N-cost-engineering.md)：把容量与利用率换算成成本。
