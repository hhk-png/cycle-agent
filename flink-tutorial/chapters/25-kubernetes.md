# 第25章 Flink on Kubernetes 与 Operator

## 25.1 为什么用 Kubernetes

Kubernetes 提供：

- 声明式部署与滚动升级；
- 资源配额、调度、隔离；
- 服务发现与网络策略；
- 与云存储/密钥/监控集成；
- 弹性伸缩。

Flink 在 K8s 上有两种主流方式：

- **原生 Kubernetes 部署**：Flink 自己创建 JM/TM Pod；
- **Flink Kubernetes Operator**：用 CRD 管理 Flink 作业生命周期。

```text
原生：flink run --target kubernetes-application → Flink 创建 JM/TM Pod
Operator：FlinkDeployment CR → Operator 创建/管理 JM/TM/Job
```

## 25.2 原生部署

大致流程：

1. 构建包含作业 jar 的镜像；
2. 配置 `config.yaml`；
3. 用 `kubernetes-session.sh` 或 `flink run --target kubernetes-application` 提交；
4. Flink 动态创建 JobManager/TaskManager Pod；
5. 配置 Service、Ingress、PVC、Secret。

关键配置：

```yaml
kubernetes.cluster-id: flink-orders
kubernetes.namespace: realtime
kubernetes.jobmanager.service-account: flink
kubernetes.taskmanager.cpu: 2
taskmanager.memory.process.size: 8g
kubernetes.rest-service.exposed.type: ClusterIP
state.checkpoints.dir: s3://bucket/checkpoints
high-availability.type: kubernetes
high-availability.storageDir: s3://bucket/ha
```

```bash
./bin/flink run \
  --target kubernetes-application \
  -Dkubernetes.cluster-id=orders-realtime \
  -Dkubernetes.namespace=realtime \
  -Dkubernetes.container.image=registry/acme/flink-orders:1.4.0 \
  local:///opt/flink/usrlib/orders.jar
```

### 原生部署的注意事项

- Flink 需要 RBAC 权限创建/删除 Pod、ConfigMap、Service；
- 镜像必须包含作业 jar 和依赖；
- checkpoint 必须放对象存储，不能只放 Pod 本地；
- Pod 漂移后本地状态丢失，要靠远程 checkpoint 恢复；
- 日志/指标要输出到外部，否则 Pod 删除就丢。

## 25.3 Flink Kubernetes Operator

Operator 用 CRD 表达作业：

```yaml
apiVersion: flink.apache.org/v1beta1
kind: FlinkDeployment
metadata:
  name: orders-realtime
spec:
  image: registry/acme/flink-orders:1.4.0
  flinkVersion: v2_3   # 取值以 Operator 支持矩阵为准
  flinkConfiguration:
    taskmanager.numberOfTaskSlots: "4"
    state.checkpoints.dir: s3://bucket/checkpoints
    execution.checkpointing.interval: 30s
  serviceAccount: flink
  jobManager:
    resource:
      memory: 2g
      cpu: 1
  taskManager:
    resource:
      memory: 8g
      cpu: 2
  job:
    jarURI: local:///opt/flink/usrlib/orders.jar
    parallelism: 8
    upgradeMode: savepoint
    state: running
```

Operator 负责：

- 创建/更新 JM/TM Deployment；
- 触发 savepoint；
- 作业升级/回滚；
- 状态监控；
- 与 Kubernetes HA 集成；
- 暴露 metrics/status。

常见 CRD：

- `FlinkDeployment`：会话集群或应用作业；
- `FlinkSessionJob`：向会话集群提交作业；
- `FlinkStateSnapshot`：savepoint/checkpoint 快照；
- `FlinkSessionJob`/autoscaler 相关注解：按版本提供。

会话集群 + 多作业的形状：

```yaml
apiVersion: flink.apache.org/v1beta1
kind: FlinkDeployment
metadata:
  name: shared-session
spec:
  flinkVersion: v2_3
  mode: session
  serviceAccount: flink
---
apiVersion: flink.apache.org/v1beta1
kind: FlinkSessionJob
metadata:
  name: orders-job
spec:
  deploymentName: shared-session
  job:
    jarURI: local:///opt/flink/usrlib/orders.jar
    parallelism: 8
    upgradeMode: savepoint
```

手动触发 savepoint/快照：

```yaml
apiVersion: flink.apache.org/v1beta1
kind: FlinkStateSnapshot
metadata:
  name: orders-savepoint
spec:
  jobReference:
    kind: FlinkSessionJob
    name: orders-job
  savepoint: {}
```

Operator 的深度排障要看：

- `FlinkDeployment.status` 的 `jobStatus`/`reconciliationStatus`；
- Operator 日志里的 reconcile 错误；
- JM/TM Pod 事件、镜像拉取、ServiceAccount/RBAC；
- savepoint 路径和权限；
- Operator 与 Flink 版本的支持矩阵（两者独立发版）。

### Operator 的调谐循环

```text
观察 CR → 对比期望/实际 → 创建/更新资源 → 更新 status
   ↑                                              │
   └──────────────── 周期重试 ←───────────────────┘
```

Operator 不是“一次部署就完事”，它会持续调谐：

- Pod 挂了会重建；
- 配置变了会滚动升级；
- 作业失败会按策略重启；
- savepoint 状态会更新到 CR status。

## 25.4 升级模式

- `stateless`：不保留状态，适合无状态作业；
- `savepoint`：先 savepoint，再升级，再恢复；
- `last-state`：从最近 checkpoint 恢复，速度快但依赖 checkpoint 可用。

生产关键作业通常用 `savepoint`，并保留旧版本和快照。

```yaml
job:
  upgradeMode: savepoint
  state: running
```

升级流程：

```text
1. Operator 触发 savepoint
2. 停止旧 Job
3. 用新镜像/新配置启动
4. 从 savepoint 恢复
5. 更新 status
```

失败时：

- savepoint 失败 → 不升级；
- 新作业启动失败 → 回滚到旧镜像；
- 恢复失败 → 用旧 savepoint/旧版本。

### Operator 自身的升级

Operator 和 Flink 是两个独立发版的组件。升级 Operator 时，先分清三件事：

1. **CRD 版本**：`FlinkDeployment`/`FlinkSessionJob`/`FlinkStateSnapshot` 的 schema 是否新增/废弃字段；旧 CR 在新 CRD 下是否仍能解析；
2. **Controller 版本**：Operator Deployment 的镜像与 RBAC；新 controller 是否仍支持旧 CRD 的字段语义；
3. **Flink 版本**：Operator 的兼容矩阵通常只覆盖若干 Flink 小版本，不能假设“最新 Operator 支持所有 Flink 版本”。

推荐顺序：

```text
1. 备份 CR/CRD 与 Operator 配置
2. 先升级 CRD（保持向后兼容），再升级 controller
3. 观察 reconcile 错误、webhook 证书、leader election
4. 小流量升级一个 FlinkDeployment，验证 savepoint/restore
5. 全量升级，保留旧 controller 镜像用于回滚
```

注意：

- CRD 删除/改名会级联影响所有 CR，升级前先看 CRD 的 conversion/validation；
- webhook 证书过期会导致 CR 创建/更新失败，升级时要轮换；
- Operator 多副本需要 leader election，确认 Lease/RBAC 权限；
- 回滚 Operator 不等于回滚作业状态：已升级的 Flink 作业仍要从 savepoint 恢复，必要时先回滚作业再回滚 Operator；
- 把 Operator 版本、CRD 版本、Flink 版本一起写进发布记录，不要只记 controller 镜像。

## 25.5 高可用

K8s 上 HA 需要：

- `high-availability.type: kubernetes`；
- `high-availability.storageDir` 指向持久存储；
- RBAC 允许 Flink 读写 ConfigMap/Lease；
- JobManager 副本/leader 选举；
- PVC 或对象存储保存 checkpoint。

```yaml
high-availability.type: kubernetes
high-availability.storageDir: s3://bucket/flink/ha
```

注意：JobManager Pod 重启后，新 leader 从 HA 存储恢复元数据；TaskManager 可能重新调度。

### K8s HA 的坑

- ConfigMap/Lease 权限不足，leader 选举失败；
- HA 存储不可写，JobManager 反复重启；
- 多集群同名 cluster-id，互相干扰；
- 网络策略阻止 JM/TM 通信；
- 节点时间不同步，Lease 过期判断异常。

## 25.6 弹性与自动伸缩

- **Reactive Mode**：作业并行度跟随可用 slot 自动调整（部分版本/场景）；
- **KEDA/Operator autoscaler**：根据 Kafka lag/CPU 调整 TaskManager 副本；
- **垂直扩容**：调整内存/CPU；
- **水平扩容**：增加并行度/slot，但要考虑状态重分配和 checkpoint。

```yaml
spec:
  taskManager:
    replicas: 4
  job:
    parallelism: 16
  flinkConfiguration:
    job.autoscaler.enabled: "true"
    job.autoscaler.metrics.window: 5m
    job.autoscaler.target.utilization: "0.7"
    job.autoscaler.stabilization.interval: 5m
```

自动伸缩不是免费的：每次扩缩容都可能触发 savepoint/restart，状态大时很慢。要设置冷却时间和上限。还要注意：

- autoscaler 需要稳定的指标（lag/利用率），指标抖动会导致反复扩缩；
- source 并行度受分区数限制，扩到超过分区数没有收益；
- 状态 rescale 可能触发大 checkpoint/恢复；
- 下游连接池/数据库连接数要同步扩容。

### 自动伸缩的决策

```text
Kafka lag 持续 > 阈值 且 资源未满 → 扩容
CPU 持续 < 阈值 且 lag 低 → 缩容
状态大、扩缩容慢 → 保守/手动
```

扩容前要确认：

- 分区数足够（source 并行度上限）；
- 状态可以 rescale；
- checkpoint 存储带宽足够；
- 下游能承受更高并发。

## 25.7 存储与网络

- checkpoint/savepoint 用对象存储（S3/OSS/GCS）或高可靠 PVC；
- RocksDB 本地盘用 SSD，避免网络盘；
- Kafka/数据库走内网，配置 NetworkPolicy；
- 镜像仓库、Secret、ServiceAccount 权限最小化；
- 日志/指标输出到外部系统，避免 Pod 磁盘打满。

```yaml
# 本地 SSD
volumeMounts:
  - name: rocksdb
    mountPath: /data/flink/rocksdb
volumes:
  - name: rocksdb
    emptyDir: {}
```

`emptyDir` 在 Pod 删除后丢失，只能做缓存；持久化仍靠 checkpoint。

## 25.8 2.x 的应用级生命周期

Flink 2.3 通过 FLIP-549/FLIP-560 引入“集群 → 应用 → 作业”的层级：

```text
Flink Cluster
  └─ Application
       ├─ Job 1
       └─ Job 2
```

好处：

- 一个应用可以包含多个作业；
- 应用级配置/依赖共享；
- 应用级启停/升级；
- Web UI 有 Applications 视图。

它适合“一个业务域多个作业”的平台化场景，但要小心故障爆炸半径：应用级重启会影响所有作业。

## 25.9 GitOps 与多环境

- 作业镜像、CRD、配置全部进 Git；
- Argo CD/Flux 同步；
- 环境用 Kustomize/Helm overlay；
- 变更走 PR；
- 回滚用 Git revert + Operator 快照；
- 禁止手工改生产 CRD。

```text
base/flink-deployment.yaml
overlays/dev/kustomization.yaml
overlays/prod/kustomization.yaml
```

GitOps 的价值是**可审计、可复现**。但 Flink 的状态不在 Git 里，升级仍要管理 savepoint；Git revert 不等于状态回滚。

## 25.10 排障

| 现象 | 排查 |
|---|---|
| Pod 一直 Pending | 资源不足/亲和性/污点 |
| JM 启动失败 | 镜像/配置/RBAC/HA 存储 |
| TM 无法注册 | 网络策略/DNS/端口 |
| 作业反复重启 | 用户异常/checkpoint 失败/OOM |
| savepoint 失败 | 存储权限/状态太大/超时 |
| 升级后状态不兼容 | uid/schema/并行度 |

## 25.11 生产检查清单

- [ ] Flink 版本与 Operator 版本在支持矩阵内，升级前查过兼容性；
- [ ] `FlinkDeployment`/`FlinkSessionJob`/`FlinkStateSnapshot` 全部进 Git；
- [ ] ServiceAccount/RBAC 最小权限，能创建/更新 Pod、ConfigMap、Service、Lease；
- [ ] HA 使用 `high-availability.type: kubernetes` + 可靠的 `storageDir`；
- [ ] checkpoint/savepoint 放对象存储，不依赖 Pod 本地盘；
- [ ] 镜像不可变，jar/连接器/配置版本与镜像 tag 绑定；
- [ ] 升级模式（`savepoint`/`last-state`/`stateless`）与业务风险匹配；
- [ ] 升级失败有回滚镜像和旧 savepoint；
- [ ] 自动伸缩有冷却、上限、指标稳定性检查；
- [ ] 网络策略限制 JM/TM/外部系统访问；
- [ ] 日志/指标/trace 输出到集群外，Pod 删除不丢；
- [ ] 有 Pod Pending/JM 启动失败/TM 注册失败/savepoint 失败的排障 runbook；
- [ ] 多环境（dev/staging/prod）用 Kustomize/Helm overlay，不手工改生产；
- [ ] 定期演练节点漂移、镜像拉取失败、HA 切换。

Operator 负责声明式生命周期；当平台需要自己封装发布 API、savepoint 轮询和回滚时，REST 的异步语义、2.x 端点变化与安全边界见[附录 M](../appendix/M-rest-operations.md)。围绕 Operator 的发布策略、兼容性门禁、标准升级 Runbook 与回滚决策，见[附录 P](../appendix/P-release-change-management.md)。

## 本章小结

K8s 让 Flink 更云原生，但也把状态、存储、网络、权限的复杂度暴露出来。Operator 把作业生命周期声明式化，升级模式决定风险。

下一章讨论容量规划、SLO 与成本：如何在满足延迟和正确性的前提下，算出需要多少资源、花多少钱。
