# 第 18 章 性能与规模化

## 18.1 调度器的时间花在哪

一次 Session 的主要成本：

```text
Snapshot 克隆        O(nodes + pods + jobs)
插件 OnSessionOpen   O(queues + jobs)（有状态插件可能更贵）
enqueue              O(jobs)
allocate             O(jobs × tasks × nodes × plugins)
preempt/reclaim      O(jobs × victims × nodes)
Bind 写回            O(bound pods)
```

当节点数到几千、Pod 数到几万时，`tasks × nodes` 是主要瓶颈。

## 18.2 规模维度

| 维度 | 典型量级 | 压力点 |
|---|---|---|
| 节点 | 1k–10k | Predicate 遍历、cache 大小 |
| Pod | 10k–100k | snapshot 克隆、绑定写回 |
| Job | 1k–10k | 排序、gang 事务 |
| Queue | 10–1k | 份额计算、层级递归 |
| 拓扑域 | 10–1k | HyperNode 遍历 |

Volcano 的官方 benchmark 与社区实践通常覆盖“千节点/万 Pod”量级；更大规模需要分片/多调度器。

## 18.3 关键优化

### 18.3.1 快照与缓存

- 每次 Session 复制快照；大集群下克隆成本高；
- 减少不必要的字段复制；
- 使用 informer 的增量更新，而不是全量重建；
- 避免插件在每次 hook 里遍历所有 Pod。

### 18.3.2 动态节点分片（v1.14 Alpha）

Sharding Controller 把节点分成多个分片，每个 scheduler 只负责一部分：

- 新增 `NodeShard` CRD；
- 当前策略基于 CPU 利用率，可扩展；
- 多调度器并行，降低单实例压力；
- 需要处理跨分片 gang/拓扑的一致性。

v1.15 支持可插拔多分片策略（ConfigMap 配置、热加载）。

### 18.3.3 Agent Scheduler

v1.14 引入面向 AI Agent 的低延迟调度器：

- 多 worker 并行消费队列；
- Conflict-Aware Binder 处理乐观并发；
- 与批调度器通过 Sharding Controller 协调节点端口；
- 适合短时、海量、低延迟的 agent 任务。

它与批调度器并存，不是替代。

### 18.3.4 Unschedulable Job Cache（v1.16）

默认关闭。开启后，调度器会缓存“暂时不可调度”的作业，在最大跳过时间（默认 5 分钟）内不再重复检查：

- 降低大量 Pending 作业的重复 predicate 成本；
- 需要失效机制：资源变化、队列变化、拓扑变化时清除；
- 配置不当会导致作业“晚调度”。

### 18.3.5 Scheduling Gates（v1.15）

区分“队列配额不足”与“集群资源不足”：

- 避免 autoscaler 因为配额问题误扩容；
- 让 Pending 原因更准确；
- 与 Kubernetes SchedulingGates 集成。

### 18.3.6 批量派发与负载估计

v1.16 引入 load estimation for bulk Pod dispatch，减少大批 Pod 同时绑定时的 API Server 压力。

### 18.3.7 上游 PreFilter 集成

v1.16 开始与 kube-scheduler 的 PreFilter 扩展点集成，复用部分过滤逻辑，减少重复计算。

### 18.3.8 重调度与 usage 的额外成本

两个“看起来只是策略”的组件，在大集群上会显著增加成本：

**shuffle + rescheduling**：

- 每个 Session 都要遍历节点计算利用率，O(nodes + pods)；
- 驱逐会触发 API Server 写、控制器 reconcile、Pod 重建、镜像/模型加载；
- 如果重调度后 Pod 被放回原节点，下一轮再驱逐，形成**控制面抖动**；
- 训练作业的驱逐可能触发 checkpoint 保存/恢复，成本远超调度本身。

控制手段：

- 只对目标队列/节点标签开启（rescheduling 用 queueSelector/labelSelector；fairshare 才用 targetQueues）；
- 设置冷却间隔（真实 Volcano 默认 5 分钟）；
- 让 allocate 的打分方向与策略一致：LNU 配 nodeorder/spread，碎片整理配 binpack；
- 用 PDB/gang 限制一次驱逐的规模；
- 监控 `Shuffled`/`Evicted` 事件的速率，而不是只看调度延迟。

**usage 插件**：

- 需要定期拉取 metrics-server/Prometheus，增加 API/QPS 与网络开销；
- 指标有延迟；刚启动的节点没有历史；
- 指标缺失时，不同实现的降级行为不同，可能导致大面积 Pending；
- 高频刷新会放大 metrics 后端压力。

建议：只在混部/推理等“request 与 usage 偏离大”的队列启用；给指标链路设 SLO；在压测中单独测 usage 的 P99，而不是只看平均值。

### 18.3.9 有状态插件的内存成本

fairshare、rescheduling、tdm 都在插件实例里保存跨 Session 状态：

- fairshare 为每个 namespace 保存 usage/decay，规模是 O(namespaces)；
- rescheduling 保存上次重调度时间/节点分数，规模是 O(nodes)；
- tdm 保存窗口与驱逐记录。

这些状态在分片/多副本场景下需要额外的同步或分区策略；`fairshare.persistState` 写 ConfigMap 也会增加 API 写压力。扩容 scheduler 前，先确认有状态插件是否支持分片。

## 18.4 Benchmark 方法

### 18.4.1 指标

- **吞吐**：每秒调度 Pod 数；
- **延迟**：P50/P95/P99 从创建到绑定；
- **会话时长**：一次 Session 的墙钟时间；
- **公平偏差**：队列 allocated/deserved；
- **碎片率**：无法满足的最大 gang；
- **抢占/回收次数**；
- **API Server QPS/延迟**。

### 18.4.2 负载设计

- 作业大小分布：小作业多、大 gang 少（真实分布通常长尾）；
- 队列数：10–100；
- 优先级分布；
- 拓扑约束比例；
- 故障注入（Pod 失败、节点 NotReady）。

### 18.4.3 工具

- v1.15 的 Volcano Benchmark 框架（Kind/KWOK/现有集群）；
- kwok 模拟大规模节点；
- Prometheus + Grafana；
- pprof（scheduler 开启 profiling）。

## 18.5 容量规划

### 18.5.1 调度器副本

- 单 scheduler 的吞吐有上限；
- 超过上限用分片/多调度器；
- 多副本 + leader election 只提供 HA，不提升吞吐（同一时刻只有一个 leader）。

### 18.5.2 API Server

调度器绑定 Pod 是写操作。大规模下 API Server 可能先于 scheduler 成为瓶颈：

- 控制绑定 QPS；
- 使用批量绑定（如果版本支持）；
- 监控 API Server 的 request latency。

### 18.5.3 etcd

- Pod/Node/CRD 数量影响 etcd 大小；
- 频繁更新 status 会增加写压力；
- 控制事件频率。

## 18.6 调参清单

| 参数 | 影响 | 建议 |
|---|---|---|
| `actions` | 流水线长度 | 只启用需要的 action |
| tier 插件数 | 每次 hook 的成本 | 移除不用的插件 |
| binpack 权重 | 碎片/装箱 | 按作业形状调 |
| 拓扑插件 | 域计算成本 | 只对大作业启用 |
| unschedulable cache | 重复检查 | 大规模 Pending 时开启 |
| sharding | 单实例压力 | 超过吞吐上限时启用 |
| 快照频率 | 一致性/成本 | 按业务延迟要求 |

## 18.7 性能问题排查

### 18.7.1 症状：调度延迟高

1. 看 session 数与 action 耗时；
2. 看节点数/Pod 数是否增长；
3. 看是否有插件做 O(n²) 遍历；
4. 看 API Server 延迟；
5. 看是否有大量 Pending 作业重复 predicate。

### 18.7.2 症状：吞吐上不去

1. 单 scheduler CPU 是否打满；
2. 绑定 QPS 是否受限；
3. 是否被 leader election/锁竞争限制；
4. 是否需要分片。

### 18.7.3 症状：调度结果抖动

1. 节点打分是否有随机性；
2. 插件是否依赖 map 遍历顺序；
3. 是否有并发更新导致快照不一致；
4. 受害者选择是否稳定。

### 18.7.4 症状：驱逐很多但利用率不升

1. 看 Pod 是否被放回原节点（打分方向冲突）；
2. 看 rescheduling 阈值是否选错了源节点；
3. 看受害者是否被 PDB/gang 保护，导致“提名但不驱逐”；
4. 看是否有多个 scheduler/分片各自重调度，互相拆台；
5. 看 API Server/控制器是否成为瓶颈，驱逐后长时间 Pending。

这类问题的根因往往不在 scheduler 本身，而在“重调度策略 + 打分策略 + 控制器策略”三者不一致。

## 18.8 MiniVolcano 的性能视角

MiniVolcano 是教学实现，性能不是目标：

- 每次 Statement 深拷贝整个 cache；
- Predicate 遍历所有节点；
- 单线程；
- 场景规模几十个 Pod。

但它的 `throughput` 场景（10 个 2 卡作业、20 tick）展示了：

- 调度器如何按波次推进；
- 资源释放后如何重新分配；
- 指标如何累计。

`shuffle` 场景则展示了重调度的**成本结构**：

- 一次驱逐 2 个 Pod，下一轮 allocate 重新绑定；
- 节点账本在驱逐/绑定前后守恒；
- 如果没有 binpack 配合，Pod 会被放回原节点，形成“驱逐—重绑—再驱逐”的循环；
- 场景用 4 个 tick 就稳定，但真实集群还要加上镜像/模型加载/checkpoint 的时间。

如果要把它变成生产原型，需要：

- 用操作日志替代深拷贝；
- 增量 cache；
- 并发绑定；
- 分片；
- 真实 API Server 的乐观并发。

这些在第 24 章展开。

## 18.9 pprof 与压测实操

性能章节最容易停在“应该分片、应该增量缓存”。这一节给出一套可以真正定位瓶颈的操作顺序。

### 18.9.1 先确认瓶颈在哪一层

```text
用户看到 Pending 变多
  ├─ 调度器没跑：看 session 速率、leader、Pod 状态
  ├─ 调度器跑了但慢：看 session 时长、action 耗时、CPU
  ├─ 调度器决策了但绑定慢：看 API Server QPS/延迟、Binder 队列
  └─ 绑定成功但 Pod 不 Running：看 kubelet/镜像/卷，不是调度器
```

只有第二类才值得做 pprof。先排除“调度器根本没运行”和“kubelet 慢”。

### 18.9.2 采集 profile

如果目标版本提供 pprof 端点（常见通过 profiling 参数或 debug 端口暴露），可以：

```bash
# 端口转发到 scheduler 的 debug/pprof 端口（端口以部署为准）
kubectl -n volcano-system port-forward deploy/volcano-scheduler 6060:6060

# CPU profile，30 秒
go tool pprof -http=:8080 http://localhost:6060/debug/pprof/profile?seconds=30

# 堆内存
go tool pprof -http=:8080 http://localhost:6060/debug/pprof/heap

# goroutine（看是否卡在锁/通道）
go tool pprof -http=:8080 http://localhost:6060/debug/pprof/goroutine
```

看什么：

| Profile | 典型信号 | 可能原因 |
|---|---|---|
| CPU | `Predicate`/`NodeOrder` 占比高 | 节点多、插件多、钩子重复计算 |
| CPU | `runtime.mapiter` 高 | 大量 map 遍历未排序/未索引 |
| Heap | `Snapshot`/`Clone` 占比高 | 每轮深拷贝、对象未复用 |
| Goroutine | 大量阻塞在 channel | 工作队列/绑定器瓶颈 |
| Mutex/Block | 锁竞争 | 多 worker 共享 cache |

### 18.9.3 压测设计

一个可复现的压测至少固定六件事：

```text
1. 集群形状：节点数、每节点资源、拓扑
2. 作业分布：大小、副本数、minAvailable、优先级
3. 提交速率：一次性提交 vs 持续提交
4. 调度器配置：actions、tiers、插件参数
5. 测量窗口：预热多久、采样多久
6. 重复次数：至少 3 次，报告均值/方差/P99
```

用 KWOK 模拟大规模节点时，注意：

- KWOK 只模拟调度与 Pod 生命周期，不跑容器；
- 它不能验证镜像、卷、GPU、网络；
- 它适合测调度器吞吐、cache 行为、插件复杂度；
- 不要用 KWOK 的结果推断真实 GPU 作业的启动时间。

### 18.9.4 一个最小压测流程

```bash
# 1) 创建 KWOK 集群/节点（按你的工具链）
# 2) 安装 Volcano
# 3) 提交一批固定形状的作业
kubectl apply -f examples/jobs/vcjob-gang-minavailable.yaml

# 4) 观察调度器
kubectl -n volcano-system logs deploy/volcano-scheduler --tail=200
kubectl get podgroup -w

# 5) 采集指标
curl -s localhost:8080/metrics | grep -E 'session|latency|allocate'

# 6) 改变一个变量（插件/权重/节点数），重复
```

### 18.9.5 读结果的三个原则

1. **看尾延迟，不只看平均**。P99 决定用户体验；平均快 10ms 但每 5 分钟卡 30 秒，仍然是问题。
2. **区分“调度慢”和“绑定慢”**。Session 时长是调度器成本，Binding 延迟是 API Server/网络成本。
3. **不要用一次运行下结论**。调度受 map 顺序、缓存状态、并发影响；至少跑 3 次，报告分布。

### 18.9.6 优化优先级

按投入产出排序：

```text
1. 去掉不用的插件/action（最大、最安全）
2. 修掉 O(n²) 的钩子（常见于拓扑/公平插件）
3. 增量 cache / 减少快照拷贝
4. 操作日志替代深拷贝（如果自研）
5. 分片/多调度器（最后才做，复杂度最高）
```

先做 1–2，往往比直接上分片更有效。

## 18.10 调度器热路径的代码级优化

18.9 讲的是“怎么定位瓶颈”。这一节讲“定位到调度器内部之后，代码上通常改什么”。这些模式不依赖具体版本，但每一条都要用 pprof/benchmark 验证收益，不要凭直觉优化。

### 18.10.1 热路径在哪里

一次 Session 的主要成本按顺序是：

```text
Snapshot/Clone         O(N + P + J)
插件 OnSessionOpen     O(Q + J)（有状态插件可能更贵）
allocate 主循环        O(J × T × N × H)
Predicate/NodeOrder    上面循环里的常数项
排序                    O(J log J + N log N)（如果每轮都排）
Bind/Evict             O(操作数) + API Server 写
```

优化要先回答：**是循环次数多，还是单次循环贵？** 前者靠剪枝/缓存/分片，后者靠减少分配、提前退出、整数化。

### 18.10.2 十个代码模式

**1. 每轮只建一次索引。** 不要在每个 Predicate 里遍历所有节点找标签/拓扑域；在 `OnSessionOpen` 建 `label -> nodes`、`node -> domain`、`queue -> jobs` 的索引，循环里 O(1) 查。

**2. 排序一次，不要比较时排序。** `JobOrderFn` 每次比较都做昂贵计算（例如 DRF 份额）会让排序变成 O(J log J × cost)。可以接受的做法是：每轮先算一次排序键并缓存，或者保证 hook 是纯函数且常数小。

**3. 便宜的过滤先做。** Predicate 里先查节点是否 Ready/可调度、资源是否够，再查污点/亲和/拓扑；把 O(1) 的检查放前面，避免为明显不可行的节点做昂贵计算。

**4. 避免热路径分配。** 每次 Predicate 都 `append` 新 slice、构造 map、格式化错误字符串，会给 GC 压力。错误信息可以惰性构造：先返回一个错误码/结构，只在需要记录时格式化。

**5. 用整数/定点分数。** 浮点累加的顺序会影响结果，也更容易出现 `NaN/Inf`。把分数归一化到整数区间（例如 0–10000），按固定顺序累加，既稳定又便于断言。

**6. 稳定排序 + 显式 tie-break。** 不要依赖 map 遍历顺序。节点按名字、Pod 按 namespace/name/UID 排序；平手时用确定的 key，保证回放可复现。

**7. 拓扑域预计算。** HyperNode 的 LCA/域查询如果每次递归，会很贵。Session 打开时把每个节点在每层的域 ID 算好，Predicate 只做整数比较。

**8. 批量绑定，控制 QPS。** 逐 Pod 同步 Bind 会把调度延迟绑在 API Server 上。使用 Binder 队列/批量接口（版本支持时），并给 API Server 设 QPS/限流；调度器快不等于绑定快。

**9. 剪枝不可调度作业。** v1.16 的 Unschedulable Job Cache 就是这一类：记录“这组条件下不可调度”，在资源/队列/拓扑变化时失效。自研实现要注意失效条件，否则会“晚调度”。

**10. 分片是最后手段。** 分片会带来跨分片 gang/拓扑/配额的一致性成本。先把插件/循环/分配优化做掉，再考虑 NodeShard/Agent Scheduler。

### 18.10.3 一个 benchmark 骨架

不要用“跑一遍感觉快了”证明优化。给热路径写 benchmark：

```go
func BenchmarkPredicate(b *testing.B) {
    ssn := fixtureSession(b, 1000, 10000) // 1000 节点、10000 Pod
    pod := fixturePod(b)
    node := ssn.Nodes[0]
    b.ReportAllocs()
    b.ResetTimer()
    for i := 0; i < b.N; i++ {
        _ = ssn.Predicate(pod, node)
    }
}
```

看三个数：`ns/op`（单次成本）、`allocs/op`（分配次数）、`B/op`（分配字节）。优化前后用同一 fixture、同一 CPU、同一编译参数；否则数字没有意义。

### 18.10.4 优化的顺序

```text
1. 去掉不用的插件/action           （收益最大、风险最低）
2. 修掉 O(N) 的 hook / 重复遍历     （常见于拓扑/公平插件）
3. 预计算索引/域/排序键             （收益稳定）
4. 减少分配/错误格式化              （GC 压力）
5. 批量绑定/限流                    （API Server 瓶颈）
6. 分片/多调度器                    （复杂度最高，最后做）
```

一个常见反模式：一上来就加分片，结果发现单分片内 still O(J×T×N)，只是把问题复制了 N 份。**先让单实例的常数项合理，再谈水平扩展。**

### 18.10.5 优化不能改变语义

性能优化最容易引入的语义 bug：

- 缓存了 Predicate 结果，但没在 Statement 绑定后失效，导致超卖；
- 为了少排序，跳过了某些作业，破坏了公平性；
- 用浮点近似分数，平手规则变了，回放不一致；
- 批量绑定失败后没有回滚，出现“部分绑定”。

每次优化后至少跑：单元测试、场景、随机不变量、两次确定性 diff。第 26 章的 12 条不变量就是这条底线。

## 18.11 本章小结

- 调度成本主要是 snapshot + tasks×nodes 的 predicate/score。
- 规模化手段：分片、agent scheduler、unschedulable cache、scheduling gates、批量派发。
- 多副本只解决 HA，不解决吞吐。
- Benchmark 要看延迟、吞吐、公平、碎片，而不只是 QPS。
- 热路径优化先剪枝/索引/排序，再减少分配和批量绑定，最后才分片；每次优化都要跑语义回归。
- MiniVolcano 不是性能实现，但能演示调度波次与资源回收。

下一章讲生产落地：从测试集群到多租户平台的完整路径。
