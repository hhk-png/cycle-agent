# 附录 K etcd 深入：一致性、备份、恢复与调优

> Kubernetes 的所有“期望状态”最终都落在 etcd。API Server 可以无状态地扩缩容，调度器和控制器可以重启，但 etcd 一旦丢失 quorum 或数据，集群就失去了事实来源。本附录把 etcd 从“一个键值存储”讲到可运维的层面：一致性从哪里来、写一次对象经过什么、为什么会慢、如何备份、如何恢复、哪些操作绝不能做。

## K.1 为什么单独讲 etcd

很多 Kubernetes 故障的表面现象是“Pod 起不来”“kubectl 超时”“控制器不工作”，根因却在 etcd：

- 磁盘 fsync 变慢，API Server 写请求超时，控制器重试风暴。
- etcd 数据目录写满，触发 NOSPACE，所有写操作失败。
- 3 节点集群坏掉 2 台，quorum 丢失，读也可能受影响。
- 备份从未恢复演练，真出事时发现快照不完整或证书不匹配。
- compaction/defrag 长期不做，数据库膨胀到 quota 上限。

理解 etcd 不是为了背命令，而是为了在故障时能回答三个问题：

1. **现在还有 quorum 吗？**
2. **写不进去是磁盘、网络、quota 还是配置？**
3. **最坏情况下，恢复到哪个时间点、丢多少数据、需要多久？**

## K.2 etcd 在 Kubernetes 中的位置

```text
kubectl / controller / kubelet
              │ HTTPS
              ▼
        kube-apiserver
              │ gRPC/HTTP + protobuf
              ▼
        etcd (Raft + MVCC + WAL)
              │
        ┌─────┴─────┐
        ▼           ▼
     follower    follower
```

关键事实：

- **只有 API Server 直接访问 etcd**。其他组件都通过 API Server，不能绕过它写 etcd。
- API Server 是**无状态**的；它缓存 watch 事件，但事实来源是 etcd。
- etcd 保存的是序列化后的对象（默认 protobuf），不是“Kubernetes 数据库表”。
- 每个 API Server 都与 etcd 建立连接；API Server 扩容会增加 etcd 的连接与 watch 压力。
- etcd 的可用性直接决定**控制面写能力**，但不直接决定已运行 Pod 的数据面。

## K.3 Raft 速成：term、log、commit、quorum

etcd 用 Raft 复制日志。理解四个词就够了：

- **term（任期）**：逻辑时钟。每次选举产生新 term；旧 term 的 leader 不能再提交。
- **log（日志）**：所有写操作先追加到 leader 的日志，再复制给 follower。
- **commit（提交）**：一条日志被多数节点持久化后，才能应用到状态机并返回成功。
- **quorum（法定人数）**：`floor(N/2) + 1`。3 节点需要 2 票，5 节点需要 3 票。

由此得到几条硬约束：

| 集群大小 | quorum | 容忍故障 | 说明 |
|---|---|---|---|
| 1 | 1 | 0 | 仅实验/单点 |
| 2 | 2 | 0 | 比单节点更差：任何一台故障都失去 quorum |
| 3 | 2 | 1 | 生产最小推荐 |
| 5 | 3 | 2 | 跨 AZ/更高容灾 |
| 6 | 4 | 2 | 不增加容错，只增加成本 |
| 7 | 4 | 3 | 写延迟与运维成本更高 |

**偶数节点不增加容错**：4 节点仍需 3 票，只能坏 1 台；5 节点能坏 2 台。跨地域部署时，quorum 的延迟由最慢的多数派决定，不要为了“看起来对称”把节点平均撒在三个高延迟地域。

## K.4 一次写请求的完整路径

以 `kubectl apply` 更新一个 Deployment 为例：

```text
kubectl
  │ 1. GET 现有对象（apply 需要 resourceVersion）
  ▼
kube-apiserver
  │ 2. 认证 → 授权 → 准入（默认值/校验/webhook）
  │ 3. 构造新的对象版本，检查 resourceVersion 前置条件
  ▼
etcd
  │ 4. leader 追加 WAL，复制到 quorum
  │ 5. 应用到 MVCC 状态机，分配新 revision
  │ 6. 返回成功
  ▼
kube-apiserver
  │ 7. 更新 watch cache，向 watcher 广播事件
  │ 8. 返回对象给 kubectl
```

几个容易忽略的点：

- **写成功不等于所有 follower 都已应用**，只代表 quorum 已持久化；落后 follower 会追赶。
- **读也可能需要 quorum**。线性一致读要确认 leader 仍合法；API Server 的很多读走 watch cache，未必每次打 etcd。
- **resourceVersion 前置条件**是乐观并发：如果对象在读取后被别人改过，API Server 返回 409 Conflict，客户端必须重新读取再试。
- **事务**：单个 API 写通常对应 etcd 的一个事务；跨对象的强一致事务不是 Kubernetes API 的通用能力。控制器需要自己处理“部分成功”的中间状态。

## K.5 数据布局与 MVCC

etcd 的 key 是字节串，Kubernetes 使用带前缀的层级路径。常见布局：

```text
/registry/pods/<namespace>/<name>
/registry/services/<namespace>/<name>
/registry/deployments/<namespace>/<name>
/registry/secrets/<namespace>/<name>
/registry/configmaps/<namespace>/<name>
/registry/serviceaccounts/<namespace>/<name>
/registry/namespaces/<name>
/registry/minions/<node-name>          # Node 的历史前缀，版本不同以实际为准
/registry/events/<namespace>/<name>
/registry/leases/<namespace>/<name>
```

每个 key 的 value 是对象序列化结果。etcd 的 MVCC 为每次事务分配一个全局递增的 **revision**：

- 修改一个 key 不会覆盖旧值，而是产生新 revision；旧 revision 仍可读，直到 compaction。
- `resourceVersion` 与 etcd revision 有对应关系，但客户端不应假设它是连续整数，也不应跨对象比较大小。
- 删除是 tombstone；watch 能收到 DELETE 事件。
- 历史版本是 watch 能“从过去某点续接”的基础，也是磁盘膨胀的来源。

查看实际 key（只读，生产上谨慎）：

```bash
ETCDCTL_API=3 etcdctl \
  --endpoints=https://127.0.0.1:2379 \
  --cacert=/etc/kubernetes/pki/etcd/ca.crt \
  --cert=/etc/kubernetes/pki/etcd/server.crt \
  --key=/etc/kubernetes/pki/etcd/server.key \
  get /registry/pods --prefix --keys-only | head
```

**绝不要**用 `etcdctl del` 直接删 Kubernetes 对象。绕过 API Server 会跳过准入、finalizer、watch 通知和控制器协调，造成对象与真实资源不一致。需要删对象就用 `kubectl delete`。

## K.6 watch、compaction 与 defrag

### watch

API Server 通过 etcd watch 感知变化，再通过 HTTP watch 分发给客户端。关键行为：

- 客户端从某个 revision 开始 watch；如果该 revision 已被 compaction，etcd 返回“版本过旧”，客户端必须重新 List。
- API Server 维护 watch cache，减少对 etcd 的重复 watch；缓存大小与对象数量会影响内存。
- `BOOKMARK` 事件用于推进客户端进度，不携带完整对象。
- watch 连接断开是常态，客户端要能重连、重 List、去重。

### compaction

compaction 删除某个 revision 之前的历史版本：

```bash
# 查看当前 revision
etcdctl endpoint status -w json | python -c 'import json,sys; print(json.load(sys.stdin)[0]["header"]["revision"])'

# 压缩到指定 revision（示例，值以实际为准）
etcdctl compact 123456
```

kube-apiserver 默认会周期性 compaction（`--etcd-compaction-interval`，常见默认 5 分钟）。如果这个参数被关掉或组件异常，etcd 会持续膨胀。

### defrag

compaction 只是标记旧版本可回收，**文件不会立即变小**；defrag 才回收碎片空间。defrag 会阻塞该成员的读写，必须**逐个成员**执行：

```bash
# 0. 先确认集群健康、有新鲜快照，并记录当前 leader
etcdctl endpoint health --endpoints=https://10.0.0.11:2379
etcdctl endpoint status --endpoints=https://10.0.0.11:2379 -w table

# 1. 对单个成员执行（不要用 --cluster；那会同时 defrag 所有成员）
etcdctl defrag \
  --endpoints=https://10.0.0.11:2379 \
  --cacert=/etc/kubernetes/pki/etcd/ca.crt \
  --cert=/etc/kubernetes/pki/etcd/server.crt \
  --key=/etc/kubernetes/pki/etcd/server.key

# 2. 等该成员恢复健康、leader 稳定后，再处理下一个
etcdctl endpoint health --endpoints=https://10.0.0.11:2379
```

注意：

- **不要执行 `etcdctl defrag --cluster`**：它会对所有成员同时 defrag，可能让整个集群短暂不可用；这正是下面警告的反例。
- defrag leader 可能触发一次 leader election；在生产窗口执行并观察。
- defrag 前确认有可用快照，且集群健康；defrag 期间不要同时做 compact/恢复/升级。
- 如果数据库很大，defrag 可能持续数分钟；预留维护窗口，监控 `etcd_disk_defrag_inflight` 和成员健康。

## K.7 性能：磁盘是第一瓶颈

etcd 的写路径需要 **fsync**。这意味着：

- 网络存储、低 IOPS 云盘、共享盘会让 p99 延迟飙升。
- 磁盘 fsync 延迟通常应稳定在个位数毫秒；持续超过 10–20ms 就会影响 API Server。
- 写放大来自 Raft 复制、WAL、boltdb 页分配和快照。
- CPU 通常不是第一瓶颈，除非加解密/压缩/大量 watch。

常见调优方向：

| 方向 | 做法 | 风险 |
|---|---|---|
| 磁盘 | 本地 NVMe/SSD，独立盘，避免与高 IO 负载混布 | 成本、数据丢失需靠副本 |
| 网络 | 控制面同 AZ/低延迟，避免跨地域 quorum | 容灾半径变小 |
| 批量 | 减少高频小写入，合并状态更新 | 控制器逻辑复杂 |
| 对象 | 控制 ConfigMap/Secret/CRD 大小，避免超大对象 | 业务约束 |
| 事件 | 降低 Event 保留与重复上报 | 可观测性下降 |
| 快照 | 定期 snapshot，监控耗时与大小 | 磁盘/网络开销 |
| 参数 | `--quota-backend-bytes`、`--snapshot-count` 等按规模调整 | 误配可能放大故障 |

**不要**把 etcd 与高 IO 的工作负载混布；也不要用 NFS 承载 etcd 数据目录。

## K.8 容量规划

需要监控的 etcd 指标：

- `etcd_server_has_leader`：是否有 leader。
- `etcd_server_leader_changes_seen_total`：leader 频繁切换说明网络/磁盘/CPU 有问题。
- `etcd_disk_wal_fsync_duration_seconds`：WAL fsync 延迟。
- `etcd_disk_backend_commit_duration_seconds`：后端提交延迟。
- `etcd_mvcc_db_total_size_in_bytes`：数据库大小。
- `etcd_mvcc_db_total_size_in_use_in_bytes`：实际使用大小（与文件大小的差距反映碎片）。
- `etcd_server_slow_apply_total`、`etcd_server_slow_read_indexes_total`。
- `etcd_network_peer_round_trip_time_seconds`：peer 往返延迟。
- `etcd_server_proposals_failed_total`、`etcd_server_proposals_pending`。

容量经验：

- 默认 backend quota 常见为 2 GiB；达到上限会触发 NOSPACE，所有写失败。
- 数据库大小应长期保持在 quota 的 50%–60% 以下，留出 compaction/defrag 与突发空间。
- 对象数量、对象大小、事件量、Secret/ConfigMap 体积、CRD 数量都会影响容量。
- 大集群要单独评估 Event、EndpointSlice、Lease、Pod 对象的增长。
- 快照大小通常接近数据库大小；备份存储要按保留策略规划。

## K.9 备份：快照不是“复制数据目录”

正确做法是用 etcd 快照 API，而不是 `cp -r` 数据目录（运行中的 boltdb 文件可能不一致）。

```bash
etcdctl snapshot save /backup/etcd-$(date +%Y%m%d%H%M).db \
  --endpoints=https://127.0.0.1:2379 \
  --cacert=/etc/kubernetes/pki/etcd/ca.crt \
  --cert=/etc/kubernetes/pki/etcd/server.crt \
  --key=/etc/kubernetes/pki/etcd/server.key

etcdutl snapshot status /backup/etcd-202601010000.db -w table
```

要点：

- 快照是**某一时刻**的 consistent snapshot；备份期间集群仍可读写。
- 快照包含 keyspace 与 revision，不包含证书、etcd 配置、API Server 配置。
- 备份要离开节点：对象存储/另一台机器，并加密、限权、校验完整性。
- 保留策略要覆盖“发现故障前”的时间窗；只留最近一份可能已被污染。
- 定期做**恢复演练**；没有演练的备份只能算“有文件”。

## K.10 恢复：把快照变成可启动的集群

恢复不是一条命令，而是一个变更流程。单成员/实验环境的最小步骤：

1. **停止写入**：停 kube-apiserver，避免恢复期间新写入丢失。
2. **停止 etcd**：所有成员停止，确认没有旧进程占用数据目录/端口。
3. **恢复快照到新目录**：

```bash
etcdutl snapshot restore /backup/etcd-202601010000.db \
  --name etcd-1 \
  --initial-cluster etcd-1=https://10.0.0.11:2380 \
  --initial-advertise-peer-urls https://10.0.0.11:2380 \
  --data-dir /var/lib/etcd-restore
```

4. **检查恢复结果**：用新数据目录在隔离端口启动 etcd，`endpoint health`、`get /registry --prefix --keys-only` 抽样。
5. **切换**：把 etcd 的 `--data-dir` 指向恢复目录，按正确顺序启动 etcd、API Server、controller-manager、scheduler。
6. **验证**：`kubectl get --raw /readyz?verbose`、核心对象、控制器状态、Pod 状态、Service/Endpoints。
7. **观察**：客户端重连、watch 重新 List、控制器是否重复创建资源、是否有旧成员仍在运行造成 split-brain。

多成员集群的恢复更复杂：

- 可以恢复所有成员到同一个快照，再组成新集群；也可以只恢复一个成员并重建其他成员。
- 必须保证 `--initial-cluster`、peer URL、member ID/name 一致，否则成员无法加入。
- **旧成员绝不能带着旧数据重新加入**，否则可能形成双 leader/数据回滚。
- kubeadm 的 stacked etcd 还要处理 static Pod manifest、证书、`/etc/kubernetes/etcd` 配置。
- 恢复后所有客户端持有的 resourceVersion 可能失效，重连后会重新 List；不要手工“修” resourceVersion。

## K.11 静态加密与 KMS

Secret 默认只是 base64，etcd 磁盘上可能是明文。生产应启用 encryption at rest：

```yaml
apiVersion: apiserver.config.k8s.io/v1
kind: EncryptionConfiguration
resources:
  - resources:
      - secrets
      - configmaps
    providers:
      - kms:
          apiVersion: v2
          name: kms-provider
          endpoint: unix:///var/run/kms/kms.sock
          timeout: 3s
      - identity: {}
```

要点：

- `identity` 作为最后 fallback 会让未加密对象继续可读；新写入应使用前面的 provider。
- KMS v2 使用信封加密，密钥轮换由 KMS 管理；版本与插件支持以目标集群为准。
- 轮换密钥时，先把新密钥加入配置，重写所有 Secret（读一次写一次），再移除旧密钥；顺序反了会导致旧对象无法解密。
- 加密配置错误可能让 API Server 无法启动或无法读取 Secret；变更前备份配置与 etcd 快照。
- 加密保护的是**磁盘/备份泄露**，不防有 API 权限的攻击者；RBAC 与审计仍然必要。

## K.12 故障模式与处理

### K.12.1 NOSPACE

**症状**：所有写请求失败，错误包含 `etcdserver: mvcc: database space exceeded`；`etcdctl alarm list` 显示 NOSPACE。

**处理**：

1. 确认磁盘真实剩余空间与 quota 使用。
2. 暂停大规模写入/控制器重试。
3. compact 到当前 revision，再逐成员 defrag。
4. 如仍不足，评估提高 `--quota-backend-bytes`（需重启）或扩容磁盘。
5. `etcdctl alarm disarm` 解除告警，观察写入恢复。
6. 根因：事件/对象增长、compaction 失效、快照/日志占满磁盘。

**不要**直接删数据目录或 `etcdctl del` 大批 key。

### K.12.2 慢磁盘/请求超时

**症状**：API Server 5xx/超时、leader 频繁切换、`etcd_disk_wal_fsync_duration_seconds` 升高。

**处理**：检查磁盘 IOPS/吞吐、是否有其他进程抢 IO、是否用了网络盘；临时降载（停止批量创建、降低控制器频率）；必要时迁移到本地 SSD。不要用“加大超时”掩盖磁盘问题。

### K.12.3 quorum 丢失

**症状**：`etcd_server_has_leader=0`，读写失败；API Server 无法写入。

**处理**：

- 若故障成员可恢复，优先恢复成员/网络，恢复 quorum。
- 若多数成员永久损坏，只能从快照恢复；恢复意味着**丢失快照之后的数据**。
- 不要为了让单个旧成员“重新选主”而随意改 `--initial-cluster`；先确认成员状态与数据新旧。

### K.12.4 证书过期

**症状**：API Server 日志 `x509: certificate has expired`；`etcdctl endpoint health` 失败；kubelet 无法连接 API Server。

**处理**：检查 etcd server/peer/client 证书与 CA 的有效期；用 kubeadm 时执行证书续期并重启控制面；确认 SAN 包含所有访问地址。证书问题常与“集群突然整体不可用”同时出现，排障时先看时间与证书。

### K.12.5 数据目录损坏

**症状**：etcd 启动失败，日志包含 boltdb/WAL 错误。

**处理**：不要反复重启写坏更多数据；保留现场；从快照恢复到新目录；验证后再切换。若没有快照，评估从其他成员恢复或重建集群。

## K.13 运维命令速查

```bash
# 健康与成员
etcdctl endpoint health --cluster
etcdctl endpoint status --cluster -w table
etcdctl member list -w table

# 告警与空间
etcdctl alarm list
etcdctl endpoint status -w json

# 快照
etcdctl snapshot save /backup/etcd.db
etcdutl snapshot status /backup/etcd.db -w table

# 压缩与碎片（注意：defrag 必须逐成员执行，不要用 --cluster）
etcdctl compact <revision>
etcdctl defrag --endpoints=https://<one-member>:2379

# 只读检查 key
etcdctl get /registry/namespaces --prefix --keys-only
```

所有命令在生产上都要带 TLS 参数，并确认 `ETCDCTL_API=3`（3.5 默认 v3）。不要在未确认影响的情况下执行 `defrag --cluster`、`compact`、`del`。

## K.14 恢复演练 runbook

建议每季度至少一次，在隔离环境执行：

1. **准备**：选定一个快照，记录其 revision、大小、时间；准备同版本 etcd 与空白数据目录。
2. **恢复**：`etcdutl snapshot restore` 到新目录，启动单节点 etcd（隔离端口）。
3. **校验**：`endpoint health`；抽样读取 Namespace/Deployment/Secret；对比对象数量与关键字段。
4. **接入 API Server**：在隔离环境启动 API Server，验证 `/readyz`、discovery、核心对象读取。
5. **业务验证**：启动 controller-manager/scheduler（或最小控制器），确认不会重复创建资源；验证一个 Deployment 能否调谐。
6. **记录**：RTO（从决定恢复到服务可用）、RPO（快照时间点到故障点的数据丢失）、失败步骤、改进项。
7. **清理**：隔离环境销毁，快照按保留策略归档。

验收标准不是“命令成功”，而是“能在目标时间内恢复到一个可用的控制面，并知道丢了什么”。

## K.15 MiniK8s 对照

MiniK8s 的 `ResourceStore` 是 etcd 的教学替身：

| etcd 能力 | MiniK8s | 差距 |
|---|---|---|
| 持久化 | 内存 dict | 重启丢失 |
| Raft/quorum | 无 | 单进程，无多副本 |
| MVCC revision | 递增整数 + 历史 | 简化，无 compaction |
| watch | 内存事件历史 | 无断线续接的完整语义 |
| 事务/CAS | `RLock` + 版本检查 | 单进程锁，无跨节点事务 |
| 快照/恢复 | 无 | 需要自己实现 |
| 加密 | 无 | 无静态加密 |
| 规模 | 单机 | 无 watch cache/限流 |

可以在 MiniK8s 上做的练习：

1. 给 `ResourceStore` 增加 `snapshot()`/`restore()`，要求恢复后 resourceVersion 继续递增且 watch 不丢事件。
2. 实现简单的 compaction：删除旧 revision，验证旧 resourceVersion 的 watch 会要求重新 List。
3. 用文件/WAL 模拟持久化，崩溃后恢复对象与 revision。
4. 给 store 增加写前日志与 fsync 开关，测量写入延迟。

这些练习不能替代真实 etcd，但能把“一致性、版本、恢复”从名词变成代码。

## K.16 检查清单

**日常**

- [ ] 3 或 5 个 etcd 成员，奇数，跨故障域合理。
- [ ] 数据目录在本地 SSD，独立于高 IO 负载。
- [ ] 监控 leader、fsync、DB 大小、告警、慢请求。
- [ ] compaction 正常运行；定期 defrag。
- [ ] 快照按计划生成、离开节点、加密、可校验。
- [ ] 证书有效期与轮换计划有监控。

**变更前**

- [ ] 有最近快照，并验证过 `snapshot status`。
- [ ] 知道 quorum 当前状态与成员健康。
- [ ] 变更窗口内避免大规模创建/删除。
- [ ] 有回滚路径与恢复 runbook。

**故障时**

- [ ] 先确认 quorum、磁盘、证书、告警，再动手。
- [ ] 不直接删数据目录、不 `etcdctl del` 业务 key。
- [ ] 恢复时隔离旧成员，防止 split-brain。
- [ ] 恢复后验证 API、控制器、业务与数据一致性。

## 延伸阅读

- [第 2 章 架构总览](../chapters/02-architecture.md)
- [第 28 章 高可用与灾难恢复](../chapters/28-ha-disaster-recovery.md)
- [第 30 章 性能与规模化](../chapters/30-performance-scale.md)
- [第 35 章 API Server 与存储实现](../chapters/35-apiserver-store.md)
- [附录 F 集群搭建与生产化手册](F-cluster-build.md)
- [附录 M 生产事故案例库](M-incident-casebook.md)
