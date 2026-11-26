# 第 20 章 Volcano Global 与多集群

## 20.1 为什么需要多集群

单集群会遇到：

- **规模上限**：etcd/API Server/调度器容量；
- **故障域**：一个集群挂了，所有训练停摆；
- **地域/合规**：数据不能出境，GPU 分散在多地；
- **异构资源**：不同云、不同型号、不同网络；
- **租户隔离**：大客户要求独立集群。

多集群调度要解决：

- 作业提交到哪个集群？
- 跨集群的 gang 怎么保证？
- 数据在哪，作业应该去哪？
- 配额是集群内还是全局？
- 一个集群失败，作业如何迁移？

## 20.2 Volcano Global

Volcano Global 是 Volcano 的多集群编排层，v0.3.0 随 v1.14 发布。核心能力：

### 20.2.1 HyperJob

HyperJob 用于大规模跨集群训练：

- 与 Karmada 集成；
- 自动生成 PropagationPolicy；
- 统一状态聚合；
- 一个 HyperJob 可以分发到多个成员集群。

### 20.2.2 数据感知调度

- `DataDependencyController`；
- `DataSourceClaim` / `DataSource` CRD；
- 根据数据位置自动注入亲和性；
- 让作业靠近数据，减少跨集群传输。

### 20.2.3 统一视图

- 成员集群注册；
- 全局队列/配额；
- 跨集群状态聚合；
- 故障转移。

### 20.2.4 队列的 extendClusters 与 job-forwarding

主仓库的 Queue 有一个多集群字段：

```yaml
spec:
  extendClusters:
    - name: cluster-a
      weight: 2
      capacity:
        nvidia.com/gpu: "64"
    - name: cluster-b
      weight: 1
```

它不是“本地调度器把 Pod 发到远端”，而是一个**转发标记**：

1. Job controller 检查作业所属 Queue 的 `extendClusters` 是否非空；
2. 非空时给 Job 和它创建的 Pod 打上 `volcano.sh/job-forwarding: "true"`（annotation 与 label）；
3. Volcano Global 或外部转发器识别这个标记，把作业/Pod 分发到成员集群。

注意：主仓库的 `vc-scheduler` 不消费这个标记，它仍会在本地尝试调度；如果转发器与本地调度器同时工作，可能出现“本地起一份、远端又起一份”。生产组合要明确谁负责转发、谁负责本地兜底，并避免两边同时创建同名 Pod。`weight`/`capacity` 是给全局层做集群选择的提示，不是本地配额。

具体 CRD 与字段以 Volcano Global 版本为准；它与主仓库的版本节奏不完全同步。

## 20.3 多集群架构模式

### 20.3.1 联邦式（Karmada）

```text
控制面集群
  ├── Karmada control plane
  ├── Volcano Global
  └── 全局队列/策略
        │
  ┌─────┼─────┐
  ▼     ▼     ▼
集群 A  集群 B  集群 C
（各自 Volcano scheduler）
```

- 全局层决定“去哪个集群”；
- 成员集群的 Volcano 决定“去哪个节点”；
- 两层调度，需要处理配额与状态同步。

### 20.3.2 独立集群 + 外部编排

- 每个集群独立 Volcano；
- 上层用 Argo/Kubeflow/自研调度器分发；
- 简单，但没有全局 gang/配额。

### 20.3.3 单集群多 AZ

- 不是真正的多集群；
- 用 HyperNode 表达 AZ/机架；
- 调度器在一个集群内做拓扑；
- 适合同云同区域。

## 20.4 多集群的难点

### 20.4.1 跨集群 gang

一个 128 卡作业分到两个集群，每个 64 卡：

- 两个集群的调度器无法原子地一起提交；
- 一个集群成功、另一个失败 → 部分启动；
- 需要全局协调器（HyperJob/上层编排）做两阶段提交或回滚。

### 20.4.2 数据位置

- 数据集在集群 A，作业去集群 B 需要传输；
- 传输时间可能超过计算时间；
- 数据感知调度要把“数据亲和”作为一等约束。

### 20.4.3 配额

- 全局配额与集群配额是两套账；
- 一个集群的空闲不能自动等于另一个集群的可用；
- 需要全局账本 + 成员集群账本同步。

### 20.4.4 网络

- 跨集群 RDMA/IB 通常不可用；
- 训练流量跨云成本高；
- 拓扑感知只在集群内有效。

### 20.4.5 故障转移

- 作业状态在成员集群；
- 迁移需要 checkpoint/重启；
- 存储要可跨集群访问。

### 20.4.6 跨集群 gang 的两阶段提交

跨集群 gang 的本质是分布式事务。一个可操作的模型：

```text
Phase 1: Reserve
  全局协调器向每个成员集群申请“预留”（节点/配额，带 TTL）
  任一成员失败 → 向已预留的成员发 Release → 作业失败

Phase 2: Commit
  所有成员预留成功 → 发 Commit
  成员把预留转成真实绑定
  任一成员 Commit 失败 → 已 Commit 的成员执行补偿（驱逐 + 回滚）
```

伪代码：

```text
reserved = []
for cluster in placement:
    ok = cluster.Reserve(job, ttl=10m)
    if not ok:
        for r in reserved: r.Release()
        return Failed
    reserved.append(cluster)

for r in reserved:
    if not r.Commit():
        for c in committed: c.Compensate()
        return Failed
return Running
```

五个工程细节：

1. **TTL 必须存在**：协调器崩溃时，预留要能自动过期，否则集群资源被永久锁住。
2. **Commit 不是原子的**：跨集群没有两阶段提交的数据库，必须接受“部分 Commit + 补偿”的窗口。
3. **幂等**：Reserve/Commit/Compensate 都要能重复调用；网络重试是常态。
4. **状态存储**：全局事务日志要持久化，协调器重启后能继续或补偿。
5. **数据位置**：如果作业需要读集群 A 的数据，跨集群 gang 的收益可能被传输成本吃掉。

这也是 Volcano Global/HyperJob 与普通单集群调度最大的区别：单集群的 Statement 可以回滚内存事务，跨集群只能靠预留 + 补偿。

## 20.5 与 Kueue MultiKueue 的对比

| | Volcano Global | Kueue MultiKueue |
|---|---|---|
| 定位 | 多集群批调度 | 多集群准入/分发 |
| 调度 | 成员集群 Volcano | 成员集群 kube-scheduler + 本地 Kueue |
| 全局 gang | HyperJob 协调 | 依赖 Worker 集群 |
| 数据感知 | DataSourceClaim | 无 |
| 成熟度 | v0.3，较新 | MultiKueue 较成熟 |

两者都在快速演进，选型要看版本与社区支持。

## 20.6 适用场景

- **跨云训练**：数据/算力分散，需要统一提交。
- **灾备**：主集群故障，作业迁移到备集群。
- **合规**：数据不出境，作业就近。
- **多团队多集群**：统一配额与视图。

不适合：

- 单集群足够；
- 跨集群网络太差；
- 团队没有多集群运维能力。

## 20.7 MiniVolcano 的差距

MiniVolcano 是单集群模拟器：

- 没有成员集群/全局队列；
- 没有跨集群 gang；
- 没有数据感知；
- 没有 Karmada 集成。

它的价值是把单集群调度语义讲清楚。多集群是另一个数量级的工程问题，不能靠一个模拟器覆盖。

## 20.8 HyperJob 的状态机与故障处理

跨集群作业的难点不在“把 YAML 发到多个集群”，而在“部分成功时怎么办”。HyperJob 的状态机就是为这个问题存在的。下面是**概念状态机**；实际 CRD 的 phase 名称与取值以 Volcano Global 版本为准。

### 20.8.1 状态

```text
Pending
  → Scheduling（选择成员集群/分区）
  → Propagating（下发 MemberJob/PropagationPolicy）
  → Running（至少一个成员在运行）
  → Succeeded / Failed / PartiallySucceeded
  → Terminating（删除传播）
```

| 状态 | 含义 | 关键动作 |
|---|---|---|
| Pending | 等待配额/成员集群/数据 | 检查成员注册、全局队列 |
| Scheduling | 计算放置 | 数据位置、容量、亲和 |
| Propagating | 正在创建成员资源 | 幂等创建、冲突处理 |
| Running | 成员作业运行中 | 聚合状态、故障检测 |
| PartiallySucceeded | 部分成员成功、部分失败 | 按策略保留/重试/清理 |
| Failed | 达到重试上限或不可恢复 | 清理、告警、人工介入 |

### 20.8.2 Reconcile 顺序

```text
1. 读取 HyperJob 与成员集群列表
2. 如果未调度：计算 placement，写入 status
3. 对每个目标成员：确保 MemberJob 存在（幂等）
4. 聚合成员状态：Running/Succeeded/Failed
5. 根据策略决定：继续等待、重试失败成员、整体失败
6. 删除路径：先清理成员资源，再摘 finalizer
```

三个幂等要求：

- **MemberJob 创建**：已存在就跳过，不要重复创建；
- **状态聚合**：成员缺失/不可达时标记 Unknown，不要当成 Failed；
- **清理**：成员集群不可达时保留 finalizer 并告警，不要静默丢弃。

### 20.8.3 五类故障

| 故障 | 表现 | 处理 |
|---|---|---|
| 成员集群不可达 | 状态 Unknown、无法下发 | 重试 + 告警；不要盲目重建 |
| 部分成员调度失败 | 一个集群 Running，另一个 Pending | 按策略：等待/重试/整体回滚 |
| 网络分区 | 两侧都认为自己是主 | 依赖 Karmada/协调器的 leader 机制；避免双写 |
| 数据不可达 | 成员 Running 但读不到数据 | 数据感知调度应提前排除；否则作业失败重试 |
| 删除泄漏 | HyperJob 删了，成员资源还在 | finalizer + 成员侧 ownerReference/标签清理 |

### 20.8.4 跨集群 gang 的边界

HyperJob 能协调“每个成员集群起一个子作业”，但**不能保证所有成员同时启动**：

- 成员集群的 Volcano 各自做本地 gang；
- 全局层没有跨集群的原子绑定；
- 两阶段预留/补偿只能缩小窗口，不能消除。

因此跨集群训练要么：

- 接受“部分启动 + 重试”，在框架层做容错；
- 用同一集群的多 AZ/多 HyperNode 代替跨集群；
- 只在推理/批处理等可独立重试的场景使用多集群。

### 20.8.5 运维命令与检查

```bash
kubectl get hyperjob -A
kubectl describe hyperjob <name>
kubectl get memberjob -A
kubectl get propagationpolicy -A
kubectl -n volcano-global-system logs deploy/volcano-global-controller
```

检查清单：

- [ ] 成员集群已注册且心跳正常。
- [ ] 全局队列/配额与成员配额不冲突。
- [ ] MemberJob 有 ownerReference 或清理标签。
- [ ] 成员不可达时有告警，不是静默 Unknown。
- [ ] 删除 HyperJob 后成员资源确实被清理。
- [ ] 数据位置与 placement 一致。

## 20.9 本章小结

- 多集群解决规模、故障域、合规、异构资源问题。
- Volcano Global 提供 HyperJob、Karmada 集成、数据感知；Queue 的 `extendClusters` 只是转发标记，本地调度器仍会尝试调度。
- 跨集群 gang、配额、数据、网络、故障转移是核心难点。
- 与 Kueue MultiKueue 是不同层次的方案。
- MiniVolcano 不覆盖多集群。

下一章开始实现 MiniVolcano：先把设计目标、对象模型和架构定下来。
