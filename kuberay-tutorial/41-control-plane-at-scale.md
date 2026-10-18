# 第 41 章 控制面规模化与多集群运营：从 10 到 1000 个 RayCluster

单个 RayCluster 的调和是“几百毫秒”的问题；一千个 RayCluster 的调和是“API Server、缓存、队列、状态写入、分片”的问题。很多平台在 10 个集群时一切正常，到 200 个集群开始出现：

- operator 重启后要几分钟才能恢复；
- `kubectl get rayclusters` 变慢；
- reconcile 延迟升高，但 CPU 看起来不高；
- API Server 的 429 增多；
- 一个租户的批量创建拖慢所有租户；
- 升级 operator 时出现“调和风暴”。

本章给出规模化的资源模型、容量公式、分片策略和一个可运行的基准实验。

> 说明：`MiniKubeRay` 的 `Operator.run_once()` 是**扫描式**的（每轮遍历所有对象），与 controller-runtime 的 informer + workqueue 不同。它的基准数字用于理解“对象数量如何影响控制面”，不能直接当作真实 operator 的性能上限。真实 KubeRay 的吞吐要高得多，但瓶颈结构相同。

## 41.1 规模到底在哪里增长

一个 RayCluster 不是一个对象，而是一棵对象树：

```text
RayCluster
├── head Pod
├── worker Pod × N
├── head Service
├── Serve Service（可选）
├── EndpointSlice
├── autoscaler ServiceAccount/Role/RoleBinding（可选）
├── NetworkPolicy × 2（可选）
├── PVC（可选）
├── Secret（可选）
└── Event × M
```

四个 CRD、每个 CR 平均 3–20 个 Pod、每个 Pod 若干 Event，规模上来后对象数量通常是 CR 数量的 20 倍量级；多主机、Serve、History/Event 密集时还会更高（本章 41.10 的算例是 22.3 倍）。不要用“5 倍”做容量下限。

| 规模维度 | 10 个集群 | 1000 个集群 |
|---|---|---|
| RayCluster CR | 10 | 1,000 |
| Pod（每集群 10 个） | 100 | 10,000 |
| Service/EndpointSlice | 20 | 2,000 |
| Event（每集群 20 条） | 200 | 20,000 |
| 每秒 watch 事件（估算） | 个位数 | 数百到数千 |
| informer 缓存对象 | 数百 | 数万 |

**结论**：控制面容量不能只按“CR 数量”算，要按**对象总数 × 事件速率 × 每对象调和成本**算。

## 41.2 控制面资源模型

### 41.2.1 四类资源

| 资源 | 消耗 | 规模上来的表现 |
|---|---|---|
| 内存 | informer cache、workqueue、事件日志 | OOM、GC 抖动 |
| CPU | reconcile、序列化、deepcopy | 调和延迟 |
| API QPS | list/watch/write | 429、超时 |
| 连接/文件描述符 | watch 连接、HTTP 客户端 | `too many open files` |

### 41.2.2 一次调和的最小成本

对每个被调和的 CR，KubeRay 通常要：

1. 从 cache 读 CR；
2. list 子资源（Pod/Service/Job）；
3. 计算默认值/校验；
4. 比较期望与实际；
5. 写 status（如果变化）；
6. 可能写子资源；
7. 返回 `RequeueAfter`。

即使“没有变化”，第 1–4 步也要做。**空闲集群不是零成本**：resync（默认 300 秒）会让每个 CR 周期性重新调和。

### 41.2.3 写放大

一次用户变更可能产生多次写入：

```text
用户 patch replicas
  -> CR MODIFIED (1)
  -> operator 创建 Pod × N (N)
  -> Pod 状态变化 (N × 阶段数)
  -> EndpointSlice 更新 (1)
  -> status 更新 (1)
  -> Event (M)
```

在 1000 个集群的场景里，一次批量扩缩容可能产生数万次写入。API Server 的写 QPS 和 etcd 的 fsync 才是真瓶颈。

## 41.3 内存估算

### 41.3.1 informer cache

controller-runtime 的 cache 会为每种被 watch 的类型保存对象。粗略估算：

```text
cache_bytes ≈ Σ (对象数 × 单对象序列化大小 × 放大系数)
```

放大系数来自 Go 结构体、指针、map、索引，通常是 JSON 大小的 2–5 倍。

| 对象 | JSON 大小（典型） | 数量（1000 集群 × 10 Pod） | 估算 |
|---|---:|---:|---:|
| RayCluster | 5–20 KiB | 1,000 | 5–20 MiB |
| Pod | 10–30 KiB | 10,000 | 100–300 MiB |
| Service | 1–3 KiB | 2,000 | 2–6 MiB |
| EndpointSlice | 2–10 KiB | 2,000 | 4–20 MiB |
| Event | 1–2 KiB | 20,000 | 20–40 MiB |

加上 Go 运行时/索引/队列，**1000 个集群、1 万 Pod 的 operator 常驻内存可能在 1–3 GiB**。如果 operator 还把 Event 全量缓存或做复杂索引，会更高。

### 41.3.2 控制面自身的对象

- CRD 很大（每个约 1 MB），但只存一份；
- Helm release/ConfigMap 很小；
- Prometheus 指标基数可能比对象更吃内存（见 41.6）。

### 41.3.3 降低缓存压力

- 用 `--watch-namespace` 分片；
- 只 watch 需要的类型（不要把 Event 全量 watch）；
- 用 label/field selector 缩小 list；
- 避免把大对象塞进 CR（例如把整个模型配置写进 annotation）；
- 控制 Event 保留时间/数量。

## 41.4 API Server 压力与限流

### 41.4.1 list/watch 的成本

- 每个 informer 对每种类型建立 watch；
- 首次 LIST 可能很大，使用分页（`limit` + `continue`）；
- watch 的 `resourceVersion` 过期会返回 410 Gone，客户端必须重新 LIST；
- 大量短命对象（Pod/Event）会让 watch 事件速率很高。

### 41.4.2 API Priority and Fairness（APF）

Kubernetes 1.20+ 的 APF 按 flow schema 给请求分优先级：

- operator 的 LIST/watch 可能被归入低优先级；
- 大量租户同时创建 CR 会挤占 API Server；
- 可以用 `FlowSchema`/`PriorityLevelConfiguration` 给 operator 单独配额；
- 监控 `apiserver_flowcontrol_*` 指标。

### 41.4.3 客户端限流

controller-runtime 的 client 有 QPS/burst 限制。KubeRay v1.7.1 把默认值覆盖为 **`DefaultQPS=100`、`DefaultBurst=200`**（不是 controller-runtime 的 20/30），并通过 operator 的 `--qps`/`--burst`（以版本为准）或代码常量暴露。

```go
// KubeRay v1.7.1 默认
cfg.QPS = 100
cfg.Burst = 200
```

调高会加重 API Server 压力；调低会让调和变慢。要结合 API Server 容量和 `MaxConcurrentReconciles` 一起调，不要照搬 controller-runtime 的示例值。

### 41.4.4 写入冲突

- status 写入频繁冲突时，会重试并放大 QPS；
- 只写变化（比较旧/新 status）是必须的；
- 多控制器写同一对象时，用 SSA/字段所有权或拆分对象。

## 41.5 WorkQueue 与并发

### 41.5.1 队列语义

controller-runtime 的 workqueue：

- 同一 key 去重；
- 失败指数退避；
- `MaxConcurrentReconciles` 控制并发；
- 没有优先级（除非自定义队列）。

### 41.5.2 并发的副作用

- 并发高：吞吐高，但 API Server 压力和冲突增加；
- 并发低：稳定，但大租户的批量操作会阻塞其他租户；
- 每个 controller 独立队列，RayCluster/RayJob/RayService 的并发要分别设置。

### 41.5.3 热点 key

一个 RayCluster 的 Pod 频繁变化会不断触发它的调和。如果某个集群有 1000 个 worker，短时间大量扩缩容会让它成为热点：

- 队列里反复出现同一个 key；
- 其他集群的调和被延迟；
- API 写入集中在一个 namespace。

缓解：

- 限制单集群规模；
- 用 `MaxConcurrentReconciles` 和分片；
- 把大集群拆到独立 operator；
- 避免频繁修改模板（会触发 Recreate）。

## 41.6 调和成本与指标基数

### 41.6.1 每轮成本

以一个 RayCluster 为例：

```text
list Pods (1 API/cache 查询)
list Services (1)
list Jobs/Secrets/PVC (若干)
calculateStatus (O(Pods))
status 写入 (0 或 1)
```

在 1000 个集群、1 万 Pod 的情况下，单轮全量扫描是 `O(集群数 × Pod 数)` 级别；真实 operator 用 workqueue 只处理变化的 key，但 resync 仍会周期性全量。

### 41.6.2 指标基数

以下标签会导致高基数：

- `pod`、`task_id`、`actor_id`、`request_id`；
- 每个 RayCluster 一个 label + 每个 Pod 一个 label；
- 历史 Pod 的指标没有及时删除。

高基数会让 Prometheus 内存/磁盘暴涨，也会拖慢抓取。建议：

- 指标只保留 `namespace`、`cluster`、`group` 等稳定标签；
- 用 relabel 删除高基数标签；
- 对 task/actor 指标做聚合或采样；
- 分开抓取 operator 和 Ray。

### 41.6.3 事件

Kubernetes Event 默认保留 1 小时（可配置）。大量 Event 会：

- 增加 etcd 写入；
- 增加 watch 事件；
- 让 `kubectl get events` 变慢。

生产建议：

- 控制 Event 频率（同一 reason 去重）；
- 用日志/追踪补充长期信息；
- 不要把大对象写进 Event message。

## 41.7 分片与多 operator

### 41.7.1 为什么要分片

单 operator 的容量有上限：

- 内存（cache）；
- API QPS；
- 单进程 GC/锁；
- 故障爆炸半径。

分片把对象分配给多个 operator 实例。

### 41.7.2 分片策略

| 策略 | 做法 | 优点 | 缺点 |
|---|---|---|---|
| namespace 分片 | 每个 operator `--watch-namespace` | 简单、隔离好 | 租户数多时 operator 多 |
| label 分片 | 只处理带特定 label 的 CR | 灵活 | 需要准入保证 label |
| hash 分片 | 按 namespace/name hash 取模 | 均衡 | 需要自定义控制器 |
| 集群分片 | 每个 K8s 集群一个 operator | 最强隔离 | 运维成本高 |
| 管理集群 + 多工作集群 | 中心控制面 + 每集群 agent | 统一视图 | 跨集群网络复杂 |

### 41.7.3 leader election 与分片

> 本章关注“规模”；leader election 的租约参数、脑裂/fencing、readiness 语义和控制面 SLO 见[第 54 章](54-operator-reliability-control-plane-slo.md)。

- 每个分片内仍需 leader election（多副本高可用）；
- 不要让两个分片 watch 同一 namespace；
- 分片变更（例如新增 operator）需要迁移对象，期间要避免双写；
- 用 `Lease` 监控 leader 切换。

### 41.7.4 多租户隔离

- 每租户一个 namespace + 一个 operator（强隔离，成本高）；
- 共享 operator + 准入策略 + 配额（常见）；
- 大租户单独分片，小租户共享；
- 用 `ResourceQuota`/Kueue 限制单租户对象数量。

## 41.8 多集群运营

### 41.8.1 模式

| 模式 | 适用 | 关键点 |
|---|---|---|
| 单集群多 namespace | 起步 | 配额/网络/升级 |
| 多集群按环境 | dev/staging/prod | 版本一致、镜像可达 |
| 多集群按地域 | 数据本地性/容灾 | 对象存储/网络延迟 |
| Kueue MultiKueue | 跨集群调度 | Workload 准入、队列 |
| 管理集群 + 工作集群 | 平台化 | 跨集群 API、身份 |

### 41.8.2 版本一致性

多集群最容易出问题的是版本漂移：

- operator/CRD 版本不一致；
- Ray 镜像 tag 不一致；
- Gateway API/Kueue 版本不一致；
- 监控 agent 版本不一致。

建议用 GitOps 的 `ApplicationSet`/`Kustomization` 按集群生成，并在 CI 中做版本矩阵检查。

### 41.8.3 跨集群网络

- 管理集群到工作集群的 API 连通性；
- 镜像仓库/对象存储的跨区可达性；
- 跨集群 DNS/服务发现；
- 灾备切换的 DNS/Gateway 权重；
- 不要把 Ray 的 GCS 跨集群暴露。

## 41.9 跑一次控制面基准

`examples/scale_benchmark.py` 用 MiniKubeRay 创建 N 个 RayCluster，测量：

- 创建对象的时间；
- 收敛轮数/时间；
- resourceVersion 写入数；
- 对象数量与近似缓存大小；
- 全量扩缩容的写入/时间；
- 删除 + GC 的时间。

运行：

```bash
python examples/scale_benchmark.py --clusters 100 --workers 2
python examples/scale_benchmark.py --clusters 200 --workers 1 --json
```

本机一次示例输出（100 集群 × 2 worker）：

```text
create (store only)     :    0.007s
converge                :    5.709s / 6 rounds
resourceVersion writes  : 2100
objects                 : 900 ({'RayCluster': 100, 'Pod': 300, 'Service': 100, 'EndpointSlice': 100, 'Event': 300})
approx cached JSON      : 1.60 MiB (16.4 KiB/cluster)
scale-up patch          : 100 writes
scale-up reconcile      : 11.597s / 4 rounds
scale-up writes         : 440
delete + GC             : 2.573s / 2 rounds
```

从这组数字能读出三件事：

1. **对象数是 CR 数的 9 倍**（100 个 CR -> 900 个对象，含 Event）；容量规划要按对象算。
2. **收敛写入是 21 次/集群**（2100 / 100）；其中大部分是 Pod/EndpointSlice/status/Event。
3. **扩缩容写入是 4.4 次/集群**（440 / 100）；真实 operator 还会更多（Pod 状态、EndpointSlice、Event）。

> 这些数字来自 MiniKubeRay 的扫描式实现，CPU 时间远高于真实 controller-runtime；但**写入数量和对象比例**有参考价值。

### 41.9.1 如何解读基准

- **不要只看总时间**：把 `rounds`、`writes`、`objects` 分开看；
- 时间随机器变化，写入/对象数量在固定输入下是确定的；
- 线性增长是正常的；超线性通常意味着某个 O(N²) 的扫描/序列化；
- 如果写入数远大于对象数，检查 status 抖动/Event 风暴。

### 41.9.2 用基准做回归

把 `--json` 输出存进 CI 基线，对以下指标做阈值：

```text
objects_total
resource_versions_to_converge
scale_writes
delete_rounds
```

时间指标只做趋势，不做硬门禁（机器差异太大）。

## 41.10 1000 个 RayCluster 的容量算例

假设：

- 1000 个 RayCluster；
- 平均每个 1 head + 8 worker = 9 Pod；
- 每个 Pod 1 条 EndpointSlice 记录；
- 每集群 2 个 Service；
- 每集群每天 200 条 Event；
- operator 每个对象平均 20 KiB JSON。

计算：

```text
Pod 对象         = 1000 × 9 = 9,000
Service 对象     = 1000 × 2 = 2,000
EndpointSlice    = 2,000
CR 对象          = 1,000
Event（1 小时）  = 1000 × 200 / 24 ≈ 8,300
总对象           ≈ 22,300

缓存 JSON 大小   ≈ 22,300 × 20 KiB ≈ 435 MiB
Go 放大 3×       ≈ 1.3 GiB
加上队列/索引    ≈ 1.5–2.5 GiB
```

API 写入：

```text
每次扩缩容 = 1 CR + 8 Pod + 2 EndpointSlice + 1 status + M Event
           ≈ 12–20 次写入
100 个集群同时扩缩容 ≈ 1,200–2,000 次写入
```

如果 1 分钟内完成，写 QPS 约 20–33；看似不高，但 etcd fsync、API Server 的 watch 广播和冲突重试会放大成本。

**结论**：

- 单 operator 2 GiB 内存 + 50 QPS 可以服务中小规模；
- 1000 集群建议分片（namespace/租户）或每集群一个 operator；
- 升级/重启要分批，避免所有对象同时 resync。

## 41.11 大规模故障模式

| 现象 | 原因 | 缓解 |
|---|---|---|
| operator 重启后长时间不工作 | 全量 LIST + resync 风暴 | 分批重启、缩小 watch 范围 |
| API 429 | 写入/ LIST 超过 APF 配额 | 调 QPS、分片、APF |
| operator OOM | cache/Event/指标基数 | 限制对象、分片、降基数 |
| 调和延迟高 | 单 key 热点/并发低 | 调并发、拆大集群 |
| 升级后大量 Pod 重建 | 默认值/spec hash 变化 | 先 staging、灰度 |
| 租户互相影响 | 共享队列/API | 分片、配额、优先级 |
| watch 410 | resourceVersion 过期 | 客户端重新 LIST（controller-runtime 自动） |
| CRD apply 失败 | 对象太大/注解太大 | server-side apply、拆分 |

### 41.11.1 升级风暴

operator 升级时，所有 CR 会被重新调和。如果新版本改了默认值/哈希，可能触发大量 Pod 重建：

- 先在 staging 用 10–50 个集群验证；
- 用 `--watch-namespace` 分批；
- 观察 `controller_runtime_reconcile_total` 和 Pod 重建数；
- 保留回滚。

### 41.11.2 启动风暴

operator 启动时：

1. informer 全量 LIST；
2. 每个对象入队；
3. 并发调和。

如果 1000 个集群同时启动，API Server 会被打满。缓解：

- 分片；
- 降低 `MaxConcurrentReconciles`；
- 用 `--watch-namespace` 缩小范围；
- 错峰重启。

## 41.12 调优清单

- [ ] 按对象数（不是 CR 数）做容量规划
- [ ] operator 内存/CPU 有 request/limit
- [ ] `--watch-namespace` 或分片策略明确
- [ ] `MaxConcurrentReconciles` 与 API QPS 匹配
- [ ] 只写变化的 status，避免 watch 风暴
- [ ] 控制 Event 频率/保留
- [ ] 指标标签基数可控
- [ ] 升级/重启分批，有回滚
- [ ] 多集群版本矩阵一致
- [ ] 有控制面基准/回归

## 41.13 练习

1. 用 `scale_benchmark.py --clusters 10/50/100/200` 画一张对象数/写入数/时间曲线，判断是否线性。
2. 把 `Operator.run_once` 的全量扫描改成“只处理有变化的 key”，比较写入数和时间。
3. 设计一个 namespace 分片方案：3 个 operator、100 个租户，如何分配？如何处理新增租户？
4. 估算你的生产规模需要多少 operator 内存/副本，写出计算过程。
5. 模拟 operator 重启：创建 200 个集群后新建 Operator 并复用 store，观察第一轮调和的对象数。

## 41.14 小结

- 规模增长的主要来源是子对象、watch 事件和 status 写入，不是 CR 数量本身。
- 控制面容量要按“对象数 × 每对象成本 + 事件/QPS”估算，并留出重启/故障余量。
- 分片（namespace/租户/地域）比单纯加副本更有效；分片键要稳定且可迁移。
- 只写变化的 status、控制 Event 频率、限制指标标签基数，是低成本的规模化手段。
- 用 `scale_benchmark.py` 做确定性回归；挂钟时间只做趋势，不做硬门禁。

→ [第 42 章 可运行故障实验室](42-incident-lab.md)
