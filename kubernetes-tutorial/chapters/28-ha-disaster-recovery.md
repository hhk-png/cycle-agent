# 第 28 章 高可用与灾难恢复

## 28.1 先定义故障模型

“高可用”不是一个布尔值。先问：

- 要防什么故障？进程崩溃、节点故障、可用区故障、区域故障、人为误删、勒索软件？
- RTO（恢复时间目标）是多少？
- RPO（恢复点目标）是多少？
- 降级是否可接受？

不同目标对应不同架构和成本：

| 故障 | 典型措施 | RTO/RPO |
|---|---|---|
| 单 Pod 崩溃 | Deployment/RS + 探针 | 秒级 / 0 |
| 节点故障 | 多副本 + PDB + 重新调度 | 分钟级 / 0（无状态） |
| 控制面实例故障 | 多 API Server + etcd quorum | 秒级 / 0 |
| 可用区故障 | 多 AZ 节点/存储/etcd | 分钟级 / 取决于存储复制 |
| 区域故障 | 多集群/备份恢复 | 小时级 / 取决于备份 |
| 误删 Namespace | RBAC + 备份 + 回收站 | 小时级 / 备份点 |

## 28.2 控制面高可用

### API Server

- 至少 3 个实例。
- 前置 L4/L7 负载均衡。
- 无状态，可水平扩展。
- 每个实例有 watch cache，注意内存。
- 证书/配置一致。
- 健康检查 `/readyz`。

### etcd

- 3 节点容忍 1 台故障，5 节点容忍 2 台。
- 奇数节点；偶数不增加容错。
- 跨 AZ 部署，权衡延迟。
- 专用磁盘（SSD），避免与其他 I/O 竞争。
- 监控：leader 变化、fsync 延迟、DB 大小、碎片。
- 定期 defrag + compaction。
- 备份：定期全量快照 + 异地副本；WAL 仅用于本机崩溃恢复，**不是可独立恢复的备份**。RPO 约等于快照间隔，除非使用存储层卷快照/持续归档。

### Scheduler/Controller Manager

- 多副本，但只有 leader 工作。
- Lease 选举。
- 保证 leader 切换后能快速恢复。
- 监控 leader 变化频率。

### 负载均衡

- API Server 前面的 LB 要支持健康检查。
- kubelet/controller 使用 VIP/域名，不直连单个 API Server。
- 证书 SAN 包含 LB 地址。

## 28.3 etcd 备份与恢复

快照：

```bash
etcdctl snapshot save /backup/etcd-$(date +%F).db \
  --endpoints=https://127.0.0.1:2379 \
  --cacert=/etc/kubernetes/pki/etcd/ca.crt \
  --cert=/etc/kubernetes/pki/etcd/server.crt \
  --key=/etc/kubernetes/pki/etcd/server.key
```

恢复（单节点示例）：

```bash
# etcd 3.6+：snapshot restore 已从 etcdctl 移到 etcdutl
etcdutl snapshot restore /backup/etcd.db \
  --data-dir=/var/lib/etcd-restore
```

etcd 3.5 及更早用 `etcdctl snapshot restore`；3.6+ 请用 `etcdutl`。kubeadm 的 etcd 静态 Pod 镜像不一定包含 `etcdutl`，恢复时通常需要在宿主机安装同版本 etcd 工具，或用容器挂载快照执行。然后修改 etcd 配置指向新 data-dir，重启控制面。

注意：

- 恢复会丢失快照之后的数据。
- 恢复后集群的证书、ServiceAccount token、Secret 可能过期/不一致。
- 多节点 etcd 恢复要重建集群。
- 定期演练恢复，不要只备份。

备份策略：

- `etcdctl snapshot save` 是**全量快照**；etcd 没有原生“增量快照”或 WAL 时间点恢复（PITR）。WAL 只用于本节点崩溃恢复，不是可独立恢复的备份。
- 要降低 RPO，只能提高全量快照频率，或使用存储层卷快照/持续归档方案；不要假设“备份了 WAL 就能恢复到任意时刻”。
- 异地/跨账号存储。
- 加密。
- 保留多个版本。
- 监控备份成功/失败。

## 28.4 应用高可用

### 无状态服务

- 多副本。
- 反亲和/拓扑分布。
- readinessProbe。
- PDB。
- HPA。
- 优雅终止。
- 多 AZ。

### 有状态服务

- 应用层复制（数据库、Kafka、etcd）。
- 每副本独立 PVC。
- 拓扑感知。
- 备份/快照。
- 故障切换演练。
- 不要依赖单个 PVC 的可用性。

### 关键依赖

- DNS。
- 入口 LB。
- 证书。
- 镜像仓库。
- 密钥管理。
- 监控/日志（故障时仍需要）。

这些依赖也要高可用，否则“K8s 高可用”没有意义。

## 28.5 PDB 与维护

PDB 保护自愿驱逐：

```yaml
apiVersion: policy/v1
kind: PodDisruptionBudget
metadata:
  name: web
spec:
  minAvailable: 2
  selector:
    matchLabels: {app: web}
```

节点维护：

```bash
kubectl drain node-1 --ignore-daemonsets --delete-emptydir-data
```

drain 会：

- 标记节点 unschedulable。
- 驱逐 Pod（遵守 PDB）。
- 等待 Pod 终止。

常见卡住原因：

- PDB 不允许驱逐。
- 本地存储/emptyDir 数据。
- DaemonSet（需要 --ignore-daemonsets）。
- 静态 Pod（需要 --force，谨慎）。
- 优雅期太长。

## 28.6 多集群

动机：

- 灾备/区域隔离。
- 数据驻留/合规。
- 多租户/业务隔离。
- 边缘/混合云。
- 避免单集群规模上限。

模式：

- **主备**：一个集群服务，另一个待命；DNS/LB 切换。
- **双活**：两个集群同时服务；数据复制/冲突解决。
- **分片**：按用户/区域分片。
- **多集群编排**：Karmada（跨集群调度/故障转移）、Argo CD（多集群 GitOps）。
- **集群生命周期**：Cluster API 管理集群的创建/升级/扩缩容，不是流量联邦。

挑战：

- 服务发现/跨集群网络。
- 数据复制一致性。
- 配置漂移。
- 证书/身份。
- 监控聚合。
- 成本。

多集群不是免费的高可用；数据层才是难点。Cluster API、Karmada、多集群网络与虚拟集群的完整讨论见第 33 章。

## 28.7 灾备演练

没有演练的 DR 计划等于没有：

1. 定义场景（删 namespace、etcd 损坏、区域故障）。
2. 定义 RTO/RPO。
3. 在隔离环境演练。
4. 记录步骤、耗时、问题。
5. 自动化恢复脚本。
6. 定期重复。

常见发现：

- 备份没加密/无法恢复。
- 恢复后镜像拉不到。
- DNS/证书配置缺失。
- 云资源配额不足。
- 团队不知道谁负责。

## 28.8 控制面故障时的行为

- 已运行 Pod 继续运行。
- kubelet 继续重启本地容器。
- 不能创建/修改对象。
- 不能调度新 Pod。
- Service 转发规则通常继续工作。
- DNS 可能继续工作（CoreDNS 已运行）。

设计应用时要考虑：

- 控制面不可用时，数据面能否继续？
- 应用是否依赖 API Server（Operator/controller）？
- 证书/Token 过期会不会导致数据面失败？

## 28.9 单点清单

检查：

- API Server LB 是否单点？
- etcd 是否偶数节点/跨 AZ？
- CoreDNS 副本数/反亲和？
- Ingress Controller 副本/PDB？
- 镜像仓库是否单点？
- 证书签发/轮换是否依赖单个服务？
- 监控/日志是否与控制面同故障域？
- 备份是否在同一账号/区域？
- 云盘是否跨 AZ？
- 是否有容量缓冲？

## 28.10 MiniK8s 的 HA 组件

MiniK8s 是单进程模拟，但包含：

- Lease 选举（`coordination/leader.py`）。
- 多节点 kubelet。
- PDB/Eviction。
- NodeLifecycleController。

测试：

- `test_single_leader_and_renewal`
- `test_failover_after_expiry_and_transition`
- `test_node_heartbeat_marks_not_ready`

它不模拟 etcd quorum/网络分区/多控制面，但可以演示 leader 选举和节点故障检测。

## 28.11 etcd 恢复演练与 Velero 恢复

备份的价值只有在恢复时才能证明。建议把恢复分成两条线，并分别演练。

### 线路一：etcd 快照恢复

适用：控制面数据损坏、误删对象、升级失败回滚。

```bash
# 1. 停止所有控制面组件（或至少停止本节点 etcd/apiserver）
# 2. 在干净目录恢复（etcd 3.6+ 用 etcdutl；3.5 及更早用 etcdctl）
etcdutl snapshot restore /backup/etcd.db \
  --name cp-1 \
  --initial-cluster cp-1=https://10.0.0.11:2380 \
  --initial-cluster-token k8s-etcd \
  --initial-advertise-peer-urls https://10.0.0.11:2380 \
  --data-dir /var/lib/etcd-restore

# 3. 修改 etcd 静态 Pod/配置的 data-dir 与 initial-cluster
# 4. 启动 etcd，再启动 apiserver/controller/scheduler
# 5. 验证：kubectl get nodes, get pods -A, 关键 CRD
```

关键点：

- 恢复前先备份当前（损坏）数据，保留现场。
- 多节点 etcd 要重建成员关系；单节点恢复不能直接加入旧集群。
- 恢复后 API Server 的 ServiceAccount 签名密钥、证书、加密密钥必须与快照匹配，否则 token/Secret 不可用。
- 快照之后创建的对象会丢失；RPO 取决于快照频率。
- 恢复后检查 Controller/Operator 是否在追赶状态，避免大量重复创建。

### 线路二：Velero 应用级恢复

适用：误删 namespace、迁移集群、选择性恢复。

```bash
velero backup create app-20261121 --include-namespaces team-a
velero restore create --from-backup app-20261121 \
  --namespace-mappings team-a:team-a-restored
```

恢复顺序：

```text
CRD → namespace → RBAC/ServiceAccount → Secret/ConfigMap
→ PVC/PV → Deployment/StatefulSet/Service → Ingress/Gateway
```

注意：

- 目标集群的 StorageClass/CSI 必须存在，否则 PVC 无法绑定。
- 镜像仓库凭据、KMS、证书要提前准备。
- CRD 版本不一致时，先升级 CRD 再恢复 CR。
- Velero 的卷备份需要 CSI 快照或 node-agent；只备份对象不备份数据。
- 恢复后做业务校验（数据一致性、连接、延迟），不要只看 Pod Running。

### 演练脚本化

把恢复步骤写成可执行脚本/Playbook，并在隔离环境定期跑：

1. 从备份恢复一个测试 namespace。
2. 验证 Service/DNS/Ingress 可达。
3. 验证数据库一致性/迁移版本。
4. 记录耗时，更新 RTO。
5. 清理测试资源。

没有演练的备份，只是“以为有备份”。

## 28.12 托管控制面与多区域灾备

托管 Kubernetes 把控制面的 etcd/升级/可用区交给厂商，但灾备责任并没有消失。

### 28.12.1 托管控制面的 SLA 与边界

厂商通常承诺控制面可用性（例如 99.95%），但：

- SLA 是月度可用性，不是“你的业务不中断”。
- 控制面故障时，已运行 Pod 可能继续运行，但无法调度/变更。
- 节点、网络、存储、Ingress、DNS 仍由你负责。
- 跨区域故障可能不在同一 SLA 内。

你仍要设计：

- 节点多 AZ/多池。
- 工作负载多副本/PDB。
- 数据备份/跨区复制。
- DNS/流量切换。
- 密钥/证书/镜像仓库的区域冗余。

### 28.12.2 多区域模式

| 模式 | 说明 | RTO/RPO | 代价 |
|---|---|---|---|
| 备份恢复（冷备） | 区域故障后在新区域重建 | 小时级/RPO 取决于备份 | 成本低，恢复慢 |
| 主动-被动（温备） | 备用区域有集群/数据副本，切流量 | 分钟级 | 资源闲置 |
| 主动-主动 | 两个区域同时服务 | 秒级 | 数据一致性/流量调度复杂 |
| 单元化 | 按用户/租户分片到区域 | 故障影响局部 | 架构复杂 |

选择取决于业务 RTO/RPO 与预算。大多数团队从“冷备 + 定期演练”开始，而不是直接上主动-主动。

### 28.12.3 数据与流量

多区域最难的是数据：

- 数据库：跨区复制、读写分离、冲突处理、切换后的脑裂。
- 对象存储：跨区复制/版本化。
- 缓存：区域本地，切换后冷启动。
- 消息队列：跨区复制/重放。
- 配置/Secret：GitOps/密钥管理要覆盖所有区域。

流量切换：

- DNS/GSLB 的 TTL 决定切换速度；太短增加查询压力。
- 健康检查要能区分“区域故障”与“应用故障”。
- 切流前确认目标区域容量足够（不要把一个区域的全量流量压到半容量）。
- 回切同样要演练。

### 28.12.4 灾备演练清单

1. 定义场景：区域故障、控制面不可用、数据库主库故障、DNS 故障。
2. 定义成功标准：RTO/RPO、错误率、数据校验。
3. 在隔离环境演练，记录每一步耗时。
4. 验证依赖：镜像仓库、KMS、IdP、证书、DNS、外部 API。
5. 验证回切：数据反向同步、流量恢复、旧区域清理。
6. 更新 runbook，明确决策人/通信渠道。
7. 定期复演，人员变动后重新培训。

没有演练的灾备方案，只是文档。

## 本章小结

- 高可用要先定义故障模型、RTO/RPO 和降级策略。
- 控制面 HA 的关键是 etcd quorum、API Server 多实例和 leader 选举。
- 应用 HA 需要多副本、探针、PDB、优雅终止和依赖治理。
- 备份必须演练；多集群增加隔离也增加数据一致性复杂度。
- 控制面故障时数据面通常继续，但新调度/变更停止。
- etcd 快照恢复与 Velero 应用恢复是两条不同线路，都要脚本化并定期演练。

## 延伸阅读

- [第 2 章 架构总览](02-architecture.md)
- [第 29 章 升级与节点运维](29-upgrade-node-ops.md)
- [第 30 章 性能与规模化](30-performance-scale.md)
- [附录 K etcd 深入：一致性、备份、恢复与调优](../appendices/K-etcd-operations.md)
- [附录 M 生产事故案例库](../appendices/M-incident-casebook.md)
