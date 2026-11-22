# 第 9 章 StatefulSet：稳定身份与有状态工作负载

## 9.1 Deployment 为什么不够

Deployment 的 Pod 是“牛群”：名字随机、可互换、可以随时被替换。对有状态服务，这不够：

- 数据库主从需要稳定网络标识（`db-0`、`db-1`）。
- 每个副本需要自己的持久卷。
- 启动/停止顺序可能重要（先主后从，或先高序号后低序号）。
- 客户端需要能区分具体实例。

StatefulSet 提供：

- **稳定 Pod 名**：`<name>-0`、`<name>-1`…
- **稳定网络标识**：通过 headless Service 的 DNS。
- **稳定存储**：volumeClaimTemplates 为每个 ordinal 创建独立 PVC。
- **有序操作**：默认按 0→N 启动，按 N→0 缩容。
- **分区更新**：只更新一部分 ordinal。

代价：

- 比 Deployment 慢（等待 Ready）。
- 删除/重建的语义更复杂。
- 不自动处理数据复制、选主、脑裂；这些仍需应用或 Operator。

## 9.2 稳定网络身份

```yaml
apiVersion: v1
kind: Service
metadata:
  name: db
spec:
  clusterIP: None        # headless
  selector: {app: db}
  ports:
    - port: 5432
---
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: db
spec:
  serviceName: db        # 必须指向 headless Service
  replicas: 3
  selector:
    matchLabels: {app: db}
  template:
    metadata:
      labels: {app: db}
    spec:
      containers:
        - name: postgres
          image: postgres:16
          ports: [{containerPort: 5432}]
```

DNS 记录：

```text
db-0.db.default.svc.cluster.local
db-1.db.default.svc.cluster.local
db-2.db.default.svc.cluster.local
```

headless Service 本身没有 ClusterIP，DNS 直接返回 Pod IP。客户端可以解析单个 Pod 名，也可以解析 `db.default.svc` 获取所有 IP。

注意：

- `serviceName` 必须指向 headless Service；否则稳定 DNS 不成立。
- Pod 的 hostname 是 Pod 名（如 `db-0`），`subdomain` 由控制器设为 **`spec.serviceName`**，不一定是 StatefulSet 名。若 StatefulSet 叫 `db`、`serviceName: db-headless`，FQDN 是 `db-0.db-headless.default.svc.cluster.local`。
- headless Service 的 selector 必须选中这些 Pod；selector 不匹配时，DNS 记录为空。
- 如果 Service 不是 headless，StatefulSet 仍能运行，但没有逐 Pod DNS。

## 9.3 稳定存储

```yaml
spec:
  volumeClaimTemplates:
    - metadata:
        name: data
      spec:
        accessModes: [ReadWriteOnce]
        resources:
          requests: {storage: 10Gi}
  template:
    spec:
      containers:
        - name: postgres
          volumeMounts:
            - name: data
              mountPath: /var/lib/postgresql/data
```

注意：**不要再显式声明同名的 `volumes` 条目**。StatefulSet 控制器会为 `volumeClaimTemplates` 中的每个模板自动创建 PVC（命名 `<模板名>-<StatefulSet 名>-<ordinal>`，如 `data-db-0`），并在 Pod 中注入同名卷。手写 `persistentVolumeClaim.claimName: data` 会指向一个不存在的 PVC，导致 Pod 无法启动。

StatefulSet 为每个 Pod 创建：

```text
data-db-0
data-db-1
data-db-2
```

关键点：

- PVC 名字由 `模板名-StatefulSet名-ordinal` 组成。
- 缩容时，PVC **默认保留**（不会被自动删除），以便扩容时恢复数据。
- 删除 StatefulSet 时，PVC 也默认保留；需要手动清理。
- 重新创建同名 StatefulSet 会复用同名 PVC，从而恢复数据。
- `persistentVolumeClaimRetentionPolicy`（1.27 beta，1.32 GA；v1.31 上字段可用但阶段为 beta）可以配置自动删除策略：

```yaml
persistentVolumeClaimRetentionPolicy:
  whenDeleted: Retain
  whenScaled: Delete
```

生产数据库通常保留 PVC，避免误删数据。

## 9.4 启动与终止顺序

默认 `podManagementPolicy: OrderedReady`：

- 创建：0 → 1 → 2，前一个 Ready 后才创建下一个。
- 删除：2 → 1 → 0，从高序号开始。
- 更新：从高序号向低序号，等前一个 Ready。

`Parallel` 则同时创建/删除，适合无顺序依赖的集群（如某些缓存）。

有序启动的用途：

- `db-0` 先成为主节点，后续从节点加入。
- 某些分布式系统需要按序号初始化。

缺点：

- 启动慢；一个 Pod 卡住，后续都不启动。
- 对无状态/对等系统是浪费。

## 9.5 StatefulSet 的更新策略

```yaml
updateStrategy:
  type: RollingUpdate
  rollingUpdate:
    partition: 2
```

- `RollingUpdate`：从最高 ordinal 开始，逐个删除并重建。删除后，控制器用新模板创建同名 Pod。
- `OnDelete`：控制器不自动更新；用户手动删除 Pod 后才用新模板重建。
- `partition`：只更新 ordinal >= partition 的 Pod。例如 partition=2、replicas=5，只更新 2/3/4，0/1 保持旧版本。

partition 常用于：

- 金丝雀：先更新高序号副本（通常是从节点）。
- 分阶段升级数据库集群。

常用字段与默认值：

| 字段 | 默认 | 作用 |
|---|---|---|
| `replicas` | 1 | 副本数 |
| `serviceName` | 必填 | 提供逐 Pod DNS 的 headless Service |
| `podManagementPolicy` | `OrderedReady` | 顺序创建/删除；`Parallel` 并发 |
| `updateStrategy.type` | `RollingUpdate` | `OnDelete` 需手动删 Pod |
| `rollingUpdate.partition` | 0 | 只更新 ordinal ≥ partition 的 Pod |
| `rollingUpdate.maxUnavailable` | 1 | 同时不可用的 Pod 数；受 `MaxUnavailableStatefulSet` 门控（1.24 alpha、1.35 beta；1.35.x/1.36 曾因回滚死锁默认关闭，1.37 修复后恢复） |
| `revisionHistoryLimit` | 10 | 保留的 ControllerRevision 数 |
| `minReadySeconds` | 0 | Ready 后稳定多久才算 available |
| `persistentVolumeClaimRetentionPolicy` | Retain/Retain | 删除/缩容时是否删 PVC（1.27 beta，1.32 GA） |

`maxUnavailable` 默认 1，所以看起来“必须逐个更新”；调大它可以加速，但会同时打断多个 ordinal，对有状态集群通常不安全。字段是否生效取决于 `MaxUnavailableStatefulSet` 门控和目标版本，升级前先用 `kubectl explain statefulset.spec.updateStrategy.rollingUpdate` 确认；1.35/1.36 的部分补丁版本默认关闭它。

更新期间，StatefulSet 控制器会等待：

- 更高 ordinal 的 Pod 都 Ready 且已更新。
- 才删除当前 ordinal 的 Pod。

## 9.6 删除与级联

删除 StatefulSet 默认级联删除 Pod，但保留 PVC。Pod 的 ownerReference 指向 StatefulSet；PVC 通常不带 ownerReference（或由 retention policy 控制），因此不会被 GC 自动删除。

如果你删除了 StatefulSet 但保留 PVC，再创建同名 StatefulSet：

- 新 Pod 会挂载原来的 PVC。
- 数据恢复。
- 但如果副本数/顺序变化，要确认数据一致性。

这也是“重建有状态服务”必须谨慎的原因。

## 9.7 数据库与有状态服务的现实

StatefulSet 只提供“身份 + 存储 + 顺序”，不提供：

- 主从选举。
- 数据复制。
- 故障切换。
- 备份恢复。
- 脑裂防护。
- 版本升级编排。

这些需要：

- 应用自身支持（PostgreSQL 流复制、MySQL Group Replication、Kafka KRaft、etcd Raft）。
- Operator（Zalando Postgres Operator、Vitess、Strimzi、CloudNativePG）。
- 外部托管服务（RDS、Cloud SQL、PolarDB）。

常见错误：

- 把单机数据库直接套 StatefulSet，以为它自动高可用。
- 多个副本共享同一个 RWX 卷写同一份数据。
- 没有 PDB，节点维护时同时驱逐多个副本。
- 没有备份，PVC 损坏就丢数据。
- 用 `ReadWriteMany` 跑数据库（多数数据库不支持多写）。

## 9.8 Headless Service 与客户端发现

客户端可以：

```text
# 解析所有 Pod IP
db.default.svc.cluster.local

# 解析单个 Pod
db-0.db.default.svc.cluster.local
```

很多数据库客户端需要显式种子节点：

```text
db-0.db:5432,db-1.db:5432,db-2.db:5432
```

Kubernetes 不会自动把“主节点”告诉你。Operator 通常维护一个 `-primary` Service 或 ConfigMap，指向当前主节点。

## 9.9 PDB 与维护

```yaml
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: db
spec:
  minAvailable: 2
  selector:
    matchLabels: {app: db}
```

PDB 保护自愿驱逐（drain、eviction），不保护：

- 节点故障。
- OOM。
- 手动 `kubectl delete pod`（默认不是 eviction）。
- Deployment 缩容。

`minAvailable` 与 `maxUnavailable` 互斥；两者都写会被准入拒绝。PDB 的实际预算在 `status.disruptionsAllowed`，不是 spec 里的数字。例如 3 副本、`minAvailable: 2`、其中一个 Pod 处于 CrashLoopBackOff 且不 Ready 时，`disruptionsAllowed` 可能是 0，`kubectl drain` 会一直卡住并提示 “Cannot evict pod as it would violate the pod's disruption budget”。

从 1.31 起 `unhealthyPodEvictionPolicy` 已 GA，默认 `IfHealthyBudget`：不健康 Pod 也受预算约束。维护场景可以设为 `AlwaysAllow`，允许先把不健康的 Pod 驱逐掉，避免 drain 死锁：

```yaml
spec:
  minAvailable: 2
  unhealthyPodEvictionPolicy: AlwaysAllow
```

排障顺序：`kubectl get pdb db -o yaml` 看 `status.disruptionsAllowed` → 看 `status.currentHealthy`/`desiredHealthy` → 找到不健康 Pod → 决定是修 Pod、临时调 PDB，还是用 `AlwaysAllow`。不要为了“让 drain 过去”直接删除 PDB；那会让所有副本同时被驱逐。

数据库集群通常设置 `minAvailable: N-1` 或 `maxUnavailable: 1`，并确保维护窗口有足够时间完成副本同步。

## 9.10 MiniK8s 的实现

`mini_k8s/controllers/workloads.py::StatefulSetController` 实现：

- 按 ordinal 创建/删除 Pod。
- OrderedReady：前一个 Ready 才创建下一个。
- 为 volumeClaimTemplates 创建 PVC，并把 Pod 卷引用改写为实际 PVC 名。
- 滚动更新：从高 ordinal 开始，partition 支持，等待更高 ordinal Ready。
- 状态：replicas/readyReplicas/currentRevision/updateRevision。

测试：

- `test_statefulset_creates_ordered_pods_and_pvcs`
- `test_statefulset_rolling_update_recreates_pods`
- `test_statefulset_partition_reports_distinct_revisions`：分区更新时 `currentRevision != updateRevision`。
- `test_statefulset_pod_dns_records`：`db-0.db` 与 FQDN 解析到 Pod IP。

未实现：

- `persistentVolumeClaimRetentionPolicy` 的自动删除。
- `rollingUpdate.maxUnavailable`（MiniK8s 仍逐个更新）。
- OnDelete 策略的完整语义。
- 真实的数据复制/选主。

## 9.11 实操

```bash
python -m mini_k8s.cli apply -f examples/03-statefulset.yaml
python -m mini_k8s.cli get statefulsets
python -m mini_k8s.cli get pods -l app=db
python -m mini_k8s.cli get pvc
python -m mini_k8s.cli exec db-0 -- psql -c "select 1"
```

MiniK8s 的 exec 是模拟的，不会真的连数据库；真实集群中需要容器内有对应客户端。

观察 DNS：

```python
print(cluster.dns.resolve("db"))
print(cluster.dns.resolve("db-0.db"))
```

## 9.12 在 Kubernetes 上运行数据系统：从 StatefulSet 到 Operator

StatefulSet 只是“有状态工作负载的骨架”。真正把 PostgreSQL、Kafka、ClickHouse 跑在生产上，需要回答一组 StatefulSet 不负责的问题：成员如何发现彼此、谁做主、复制是否同步、备份是否可恢复、升级时先动哪个副本、磁盘 IOPS 够不够。本节给出一套选型与验收框架。

### 9.12.1 四种运行形态

| 形态 | 典型场景 | 优点 | 代价 |
|---|---|---|---|
| 托管服务（RDS、Cloud SQL、MSK、Confluent Cloud） | 大多数业务数据库 | 备份/HA/升级由厂商负责 | 成本、锁定、网络延迟、功能边界 |
| Operator | 需要自助、跨云、特殊版本/插件 | 自动化运维、Kubernetes 原生 | Operator 质量参差，故障模式要自己承担 |
| 裸 StatefulSet + 脚本 | 学习、简单单实例、非关键数据 | 可控、依赖少 | 选主/备份/升级都要自己写 |
| 虚拟机/裸金属 | 超大规模、特殊硬件、强隔离 | 性能与运维模型成熟 | 失去调度与声明式优势 |

决策顺序不是“能不能上 K8s”，而是：

1. 数据能否接受托管？能托管优先托管。
2. 必须自助时，是否有成熟 Operator 和社区？
3. 团队是否能承担“数据库运维”而不仅是“YAML 运维”？
4. 业务是否真的需要 Kubernetes 的调度/多租户/弹性？

把生产数据库交给一个没有备份演练、没有故障切换测试的 Operator，比放在虚拟机上更危险，因为 Kubernetes 给了“已经自动化”的错觉。

### 9.12.2 Operator 的职责边界

一个合格的数据系统 Operator 通常要处理：

- **成员管理**：创建/删除副本，维护成员列表，处理 PVC 与 ordinal 的对应关系。
- **拓扑与角色**：主从、分片、副本分布、机架/可用区感知。
- **故障检测与切换**：探活、 fencing（防止旧主继续写）、提升新主、更新 Endpoints/Service。
- **配置管理**：生成配置文件、滚动生效、版本兼容。
- **备份与恢复**：定期备份、WAL/binlog 归档、PITR、恢复到新集群。
- **升级编排**：先升级哪个角色、何时切换、失败如何回滚。
- **证书与凭据**：TLS、用户密码、轮换、与 Secret/KMS 集成。
- **可观测性**：暴露指标、事件、状态条件，能让运维判断“集群是否健康”。

常见 Operator：

- **CloudNativePG**（PostgreSQL）：主备、流复制、备份到对象存储、PITR、故障切换。
- **Zalando Postgres Operator**：Patroni + Spilo，成熟但组件多。
- **Vitess**（MySQL 分片）：大规模分片、在线 resharding。
- **Strimzi**（Kafka）：KRaft/ZooKeeper、机架感知、滚动升级。
- **Redis Operator / Sentinel**：主从、哨兵、集群模式。
- **ClickHouse Operator**：分片/副本、Keeper、扩容。
- **Elastic Cloud on Kubernetes (ECK)**：Elasticsearch/Kibana/Beat。
- **MinIO Operator**：分布式对象存储、租户。

评估 Operator 时，重点不是 CRD 字段多少，而是：

1. 故障切换是否有 fencing，是否可能双写。
2. 备份是否应用一致，恢复是否演练过。
3. 升级是否支持 minor/major，失败能否回退。
4. 是否支持你需要的拓扑（多 AZ、跨地域、只读副本）。
5. 社区活跃度、版本支持窗口、商业支持。
6. 是否会把关键状态只放在内存/单点。

### 9.12.3 存储与 I/O：数据库的第一瓶颈

数据库对存储的要求通常不是“容量”，而是：

- **IOPS**：随机读写、fsync。
- **吞吐**：顺序写 WAL、备份、合并。
- **延迟**：p99 fsync 延迟直接决定事务提交延迟。
- **持久性**：掉电后数据是否还在。
- **拓扑**：卷必须与 Pod 同可用区。

实践要点：

- 数据盘与 WAL 盘分开（如果存储层支持多 PVC），避免 WAL 顺序写和數據随机读互相干扰。
- 使用 `ReadWriteOnce`，不要把数据库数据目录放在 `ReadWriteMany` 上；多数数据库不支持多写。
- 云盘关注 `iops`/`throughput` 参数，很多默认盘在突发额度用完后性能骤降。
- 本地 NVMe 性能最好，但节点故障会丢数据；必须依赖副本/备份，而不是本地盘本身。
- 文件系统与挂载参数（`noatime`、`discard`、`nobarrier` 的风险）要按数据库厂商建议配置。
- 卷扩容不等于文件系统自动扩容；确认 CSI 与文件系统支持在线扩容。
- 快照是 crash-consistent 还是 application-consistent，要在恢复演练中验证。

一个常见的 PostgreSQL 存储布局：

```yaml
volumeClaimTemplates:
  - metadata: {name: data}
    spec:
      accessModes: ["ReadWriteOnce"]
      storageClassName: fast-ssd
      resources: {requests: {storage: 500Gi}}
  - metadata: {name: wal}
    spec:
      accessModes: ["ReadWriteOnce"]
      storageClassName: fast-ssd
      resources: {requests: {storage: 100Gi}}
```

不是所有 Operator 都支持多卷；如果只支持一个数据目录，至少把 WAL 与数据放在同一高性能盘，并监控 fsync 延迟。

### 9.12.4 典型系统的关键约束

**PostgreSQL**

- 主备流复制，关注 `synchronous_commit` 与复制槽。
- 连接数受 `max_connections` 限制，通常需要 PgBouncer。
- 备份用 pgBackRest/WAL-G，恢复要验证 PITR。
- 故障切换需要 fencing，避免旧主恢复后双写。
- 大版本升级通常不能原地回退，必须逻辑复制或 dump/restore。

**MySQL**

- Group Replication / InnoDB Cluster 需要奇数成员与正确网络。
- binlog 是复制与 PITR 的基础，注意保留周期。
- 分片方案（Vitess）复杂度高，先确认是否真的需要。

**Redis**

- Sentinel 提供故障切换但不分片；Cluster 分片但运维复杂。
- RDB/AOF 的持久性有限，不能把 Redis 当唯一数据源。
- 内存淘汰策略要与业务语义匹配，避免把“缓存”当“数据库”。

**Kafka**

- 副本因子 ≥ 3，`min.insync.replicas` 与生产者 `acks` 一起决定持久性。
- 机架感知（`broker.rack`）避免同一机架放多个副本。
- 每 broker 独立 PVC；不要多个 broker 共享一个卷。
- KRaft 模式减少 ZooKeeper 依赖，但控制器 quorum 仍是关键。
- 扩容 broker 后要做分区重分配，否则新节点空闲。

**ClickHouse**

- 分片（shard）与副本（replica）是两个维度；Keeper 负责协调。
- 本地盘 + 副本是常见高性能方案；共享存储不一定更好。
- Merge 后台任务会吃 I/O，容量规划要留余量。
- 大表变更（ALTER）是重操作，避免在高峰做。

**Elasticsearch**

- master/data/ coordinating 角色分离，避免大数据节点参与选举。
- JVM heap 不超过物理内存的一半，剩余留给文件系统缓存。
- PVC 容量与 shard 分布要一起规划，避免磁盘水位触发只读。
- 集群引导配置（`cluster.initial_master_nodes`）错误会导致脑裂/无法启动。

### 9.12.5 备份、恢复与演练

数据系统的备份要分三层（详见 12.14）：

1. **基础设施快照**：卷快照，快但可能 crash-consistent。
2. **应用一致性备份**：数据库原生工具 + WAL/binlog 归档。
3. **资源清单备份**：CRD/Secret/ConfigMap/PVC 等对象。

恢复演练至少验证：

- 能否在空集群恢复出一个可读写的实例。
- RPO 是多少：最后一次可恢复事务的时间点。
- RTO 是多少：从决策到服务恢复的墙钟时间。
- 恢复后的数据是否通过业务校验（行数、校验和、关键查询）。
- 依赖是否齐全：KMS、对象存储、DNS、证书、连接串。

备份不是“有任务成功”就结束。没有演练的备份，只是心理安慰。

### 9.12.6 升级、扩缩容与故障演练

有状态系统的变更顺序通常与无状态相反：

- 先备份，再升级一个副本，观察复制延迟与错误率。
- 主节点最后升级，或先做受控切换。
- 大版本升级要读厂商的 upgrade path，不能跳版。
- 缩容前确认数据已迁移/副本已同步，避免同时失去多个副本。
- 使用 PDB 限制自愿驱逐，但 PDB 不能保护非自愿故障。

演练清单：

- 杀掉一个副本：服务是否无感，副本是否自动重建。
- 杀掉主节点：切换耗时、是否双写、客户端是否自动重连。
- 断网/分区：是否脑裂，恢复后如何处理冲突。
- 磁盘写满：是否只读、是否影响其他节点。
- 备份恢复：能否在目标时间点恢复。

### 9.12.7 调度与性能

- 用节点池/污点把数据库与普通业务隔离，避免 noisy neighbor。
- 对延迟敏感的系统考虑 CPU Manager `static` 策略与巨页，但要确认运行时/内核支持。
- 用 podAntiAffinity/topologySpreadConstraints 把副本分散到不同节点/可用区。
- 本地盘方案要把 Pod 固定到节点（nodeAffinity 或 local PV），并接受节点故障。
- 资源 requests 要按峰值设，不要用 HPA 自动扩数据库副本（扩副本不等于扩写能力）。
- 监控 fsync 延迟、复制延迟、连接数、慢查询、磁盘水位、JVM/GC（如适用）。

### 9.12.8 反模式清单

- 单副本数据库 + 没有备份，却认为 StatefulSet 提供了高可用。
- 多个副本共享同一个 RWX 卷写同一份数据。
- 用 Deployment 跑数据库，Pod 重建后身份/存储错乱。
- 没有 PDB，节点维护时同时驱逐主备。
- 把 Operator 当黑盒，从不看它的状态条件和事件。
- 在同一个节点上放多个高 I/O 数据库副本。
- 只监控 Pod Running，不监控复制延迟与备份年龄。
- 用 `latest` 镜像跑数据库，升级不可复现。

### 9.12.9 MiniK8s 的边界

MiniK8s 的 StatefulSet 实现了身份、PVC、顺序与 partition，足以观察控制器契约；它不实现真实数据复制、选主、备份和 I/O。第 12 章的存储模拟同样只验证绑定/回收语义。把本节当作“上生产前的检查表”，不要在 MiniK8s 上验证数据库性能。

## 本章小结

- StatefulSet 提供稳定名字、稳定 DNS、稳定 PVC 和有序操作。
- 它不提供数据复制、选主和备份；有状态系统的核心复杂度仍在应用/Operator。
- 运行数据库/缓存/消息队列要先选形态（托管/Operator/StatefulSet/VM），再评估存储 I/O、备份、升级与故障切换。
- Operator 的价值在成员管理/选主/备份/升级编排，不在 CRD 字段多少；没有恢复演练的 Operator 不比 VM 更安全。
- PVC 默认保留是保护数据的关键设计，也要求运维有清理流程。
- 更新策略（RollingUpdate/OnDelete/partition）要结合集群角色设计。

## 延伸阅读

- [第 10 章 DaemonSet、Job 与 CronJob](10-daemonset-job-cronjob.md)
- [第 12 章 存储](12-storage.md)
- [第 19 章 CRD 与 Operator](19-crd-operator.md)
- [第 50 章 事件驱动、工作流与数据管道](50-event-driven-workflows.md)：Kafka/CDC/流处理与有状态数据系统。
