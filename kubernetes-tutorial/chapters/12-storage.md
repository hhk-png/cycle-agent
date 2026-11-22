# 第 12 章 存储：Volume、PV、PVC、StorageClass 与 CSI

## 12.1 为什么容器存储很难

容器的可写层是临时的：

- 容器重启，可写层丢失。
- Pod 被删除，emptyDir 丢失。
- Pod 被调度到另一节点，本地盘可能不可用。
- 多个副本访问同一份数据需要共享语义。

Kubernetes 用 Volume 抽象把存储接入 Pod，用 PV/PVC 把“存储供给”和“存储消费”解耦，用 StorageClass/CSI 把存储实现开放给厂商。

## 12.2 Volume 类型

常见：

- `emptyDir`：Pod 生命周期内的临时目录；容器崩溃不丢，Pod 删除丢。
- `hostPath`：挂载宿主机路径；危险，通常只给系统组件。
- `configMap` / `secret`：配置注入。
- `projected`：多个来源合并。
- `persistentVolumeClaim`：使用持久卷。
- `nfs`、`cephfs`、`iscsi`：传统网络存储。
- `csi`：通过 CSI 驱动接入。
- `downwardAPI`：Pod 元数据。

```yaml
spec:
  volumes:
    - name: cache
      emptyDir:
        sizeLimit: 1Gi
    - name: data
      persistentVolumeClaim:
        claimName: data
```

`emptyDir` 也受节点 ephemeral-storage 限制。`sizeLimit` 不是 cgroup 硬限制：写入不会在达到上限的瞬间被阻塞，而是由 kubelet 周期性统计用量，超过后**驱逐 Pod**。因此它是“最终强制”，不是“精确配额”；需要硬隔离时用独立卷/配额。

## 12.3 PV 与 PVC

```text
PVC（用户想要什么） ──绑定──▶ PV（集群有什么）
```

PVC：

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: data
spec:
  accessModes: [ReadWriteOnce]
  resources:
    requests:
      storage: 10Gi
  storageClassName: fast
```

PV：

```yaml
apiVersion: v1
kind: PersistentVolume
metadata:
  name: pv-001
spec:
  capacity:
    storage: 100Gi
  accessModes: [ReadWriteOnce]
  persistentVolumeReclaimPolicy: Retain
  storageClassName: fast
  csi:
    driver: ebs.csi.aws.com
    volumeHandle: vol-0123
```

绑定条件：

- storageClassName 匹配。
- accessModes 满足。
- 容量 >= 请求。
- selector（可选）匹配 PV 标签。
- PV 未被占用。

## 12.4 Access Modes

访问模式是**存储插件声明的能力**，不是 Kubernetes 自己实现的锁。PV 的 `accessModes` 与 PVC 的请求必须匹配，实际语义由 CSI 驱动/后端存储决定。

| 模式 | 作用域 | 典型后端 | 常见故障 |
|---|---|---|---|
| `ReadWriteOnce`（RWO） | 单节点；同节点多个 Pod 可同时挂载 | 云盘、本地盘、大部分块存储 | 两个 Pod 被调度到不同节点 → Multi-Attach 错误 |
| `ReadOnlyMany`（ROX） | 多节点只读 | NFS、CephFS、对象存储网关 | 后端实际允许写，应用误写导致数据损坏 |
| `ReadWriteMany`（RWX） | 多节点读写 | NFS、CephFS、EFS、Azure Files、SMB | 后端不支持；应用没有多写协调 |
| `ReadWriteOncePod`（RWOP） | 单 Pod；同一节点也不允许第二个 Pod | 支持 RWOP 的 CSI 驱动 | 驱动不支持；滚动更新时新 Pod 等旧 Pod 释放 |

RWO 最典型的故障是 **Multi-Attach**：一个有状态应用扩到 2 副本，两个 Pod 被调度到不同节点，第二个 Pod 卡在 `ContainerCreating`，事件里出现：

```text
Multi-Attach error for volume "pvc-..." Volume is already exclusively attached
to one node and can't be attached to another
```

这不是调度器 bug，而是 RWO 的语义。解决办法：

1. 有状态服务保持单副本，或用支持 RWX 的后端并让应用自己协调写。
2. 用 `ReadWriteOncePod` 明确“只能一个 Pod”，让第二个副本直接无法挂载，而不是悄悄共享。
3. 用拓扑约束/反亲和把需要同一卷的 Pod 放到同一节点（仅适用于确实能共享的场景，风险高）。

RWX **不等于**应用可以安全多写。它只表示存储层允许多节点挂载；文件锁、数据库页缓存、写入顺序仍需要应用或数据库自己处理。把数据库 PVC 设为 RWX 并不会让数据库变成分布式数据库。

## 12.5 回收策略

`persistentVolumeReclaimPolicy`：

- `Retain`：PVC 删除后，PV 保留为 Released，需要人工清理。
- `Delete`：删除底层存储和 PV。
- `Recycle`：已废弃。

生产数据卷通常用 `Retain`，防止误删 PVC 导致数据丢失。代价是要有清理流程。

PVC 删除流程：

1. PVC 进入 Terminating（有 pvc-protection finalizer）。
2. 如果没有 Pod 使用，移除 finalizer。
3. 根据 PV 回收策略处理底层卷。

PVC 保护：

- 被 Pod 使用时，PVC 不会被删除。
- kubelet 挂载期间，finalizer 阻止删除。

## 12.6 StorageClass 与动态供给

StorageClass 描述“如何创建存储”：

```yaml
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: fast
  annotations:
    storageclass.kubernetes.io/is-default-class: "true"
provisioner: ebs.csi.aws.com
parameters:
  type: gp3
  encrypted: "true"
reclaimPolicy: Delete
allowVolumeExpansion: true
volumeBindingMode: WaitForFirstConsumer
```

字段：

- `provisioner`：CSI 驱动名。
- `parameters`：驱动特定参数。
- `reclaimPolicy`：动态创建的 PV 的回收策略。
- `allowVolumeExpansion`：允许扩容。
- `volumeBindingMode`：
  - `Immediate`：PVC 创建时立即绑定/供给。
  - `WaitForFirstConsumer`：等到 Pod 调度后再创建，保证卷与节点拓扑匹配。

### WaitForFirstConsumer 的重要性

云盘通常绑定可用区。如果先创建卷再调度，可能出现：

```text
PVC 在可用区 A 创建
Pod 只能调度到可用区 B（资源/亲和性）
→ Pod Pending
```

WFFC 让调度器先选节点，再在对应可用区创建卷。生产云盘 StorageClass 通常默认 WFFC。

## 12.7 CSI

CSI（Container Storage Interface）是存储厂商的标准接口：

- `Identity`：驱动信息、能力。
- `Controller`：创建/删除卷、attach/detach、快照、扩容。
- `Node`：挂载/卸载到节点。

部署形态：

- **CSI Controller**：Deployment/StatefulSet，通常多副本。
- **CSI Node**：DaemonSet，每个节点一个。
- **sidecars**：external-provisioner、external-attacher、external-resizer、external-snapshotter、node-driver-registrar。

流程：

```text
PVC 创建
  → external-provisioner 调 CSI CreateVolume
  → PV 创建
  → external-attacher 调 ControllerPublish/Attach
  → kubelet 调 NodeStage/NodePublish 挂载
  → Pod 启动
```

MiniK8s 用 hostPath 模拟动态供给，不实现 CSI。真实集群要选择经过验证的 CSI 驱动，并关注：

- 驱动版本与 Kubernetes 版本兼容性。
- 多可用区拓扑。
- 快照/克隆。
- 加密。
- 性能限制（IOPS、吞吐）。
- 挂载并发与节点限制。

## 12.8 卷扩容

```yaml
# StorageClass
allowVolumeExpansion: true

# PVC
spec:
  resources:
    requests:
      storage: 20Gi   # 从 10Gi 改成 20Gi
```

流程：

1. 修改 PVC 请求大小。
2. external-resizer 调 CSI ControllerExpandVolume。
3. 文件系统扩容（可能需要重启 Pod，取决于驱动和文件系统）。
4. PVC status.capacity 更新。

不是所有驱动都支持在线扩容；有些需要重启 Pod 或卸载卷。

## 12.9 快照与克隆

VolumeSnapshot：

```yaml
apiVersion: snapshot.storage.k8s.io/v1
kind: VolumeSnapshot
metadata:
  name: data-snap
spec:
  volumeSnapshotClassName: csi-snapclass
  source:
    persistentVolumeClaimName: data
```

克隆：

```yaml
spec:
  dataSource:
    kind: PersistentVolumeClaim
    name: data
```

用途：

- 备份。
- 快速创建测试环境。
- 数据库分支。

快照不是备份的完整替代：它依赖存储系统，跨区域/跨云需要额外复制。

## 12.10 存储拓扑

云盘、本地盘都有拓扑约束：

- 可用区。
- 节点。
- 机架。

调度器需要知道 PV 的节点亲和性：

```yaml
spec:
  nodeAffinity:
    required:
      nodeSelectorTerms:
        - matchExpressions:
            - key: topology.kubernetes.io/zone
              operator: In
              values: [us-east-1a]
```

WFFC + CSI topology 让调度器在正确位置创建卷。

本地盘（Local PV）通常绑定具体节点，Pod 必须调度到该节点；节点故障时数据不可用，需要应用层复制。

### 12.10.1 CSI 的内部契约与常见故障

CSI 驱动不是一个进程，而是一组 sidecar 与控制器/节点组件的协作：

```text
用户创建 PVC
  → external-provisioner（Controller）
       → CSI CreateVolume（幂等，带 parameters/topology）
       → 创建 PV
  → external-attacher（Controller）
       → ControllerPublishVolume（attach 到节点）
       → VolumeAttachment 对象
  → kubelet + CSI Node（DaemonSet）
       → NodeStageVolume（格式化/挂载到全局路径）
       → NodePublishVolume（bind mount 到 Pod 目录）
  → Pod 启动
```

几个容易忽略的契约：

- **幂等**：每个 CSI 调用都可能重试，驱动必须用 `volume_id`/请求参数保证幂等。
- **attachment 上限**：云盘通常有“每节点最多挂多少块”“每卷只能 attach 到一个节点”的限制；调度器通过 `CSINode.spec.drivers[].allocatable.count` 感知（驱动从 CSI `NodeGetInfo.max_volumes_per_node` 上报）。
- **拓扑**：`CreateVolume` 的 `accessibility_requirements` 决定卷在哪个可用区；WFFC 让调度先选节点再建卷。
- **Stage vs Publish**：Stage 是节点级准备（格式化、挂载），Publish 是 Pod 级 bind mount；不是所有驱动都实现 Stage。
- **Secret**：驱动可能需要云凭据；通过 `storage.k8s.io/v1` 的 Secret 引用，注意轮换与权限。
- **块设备**：`volumeMode: Block` 直接把设备给容器，不格式化；数据库/分布式存储常用。

常见故障与定位：

| 现象 | 检查 |
|---|---|
| PVC Pending，`no capacity`/`waiting for first consumer` | StorageClass、拓扑、WFFC、配额 |
| `FailedAttachVolume` | attach 上限、节点状态、云 API 限流、VolumeAttachment |
| `FailedMount` | NodeStage/Publish 日志、文件系统、网络存储可达性 |
| 扩容后容量不变 | `allowVolumeExpansion`、驱动支持、文件系统扩容 |
| 快照恢复慢/失败 | snapshot class、跨区、驱动版本、VolumeSnapshotContent 状态 |
| 删除 PVC 后卷仍在 | reclaimPolicy、finalizer、VolumeAttachment 残留 |

### 12.10.2 块设备、通用临时卷与 VolumeAttributesClass

- **Block 卷**：`volumeMode: Block`，Pod 里以设备节点出现（如 `/dev/xvda`），由应用自己管理文件系统。适合数据库、Ceph、部分 AI 训练缓存。
- **Generic Ephemeral Volume**：PVC 生命周期跟随 Pod，适合“每个 Pod 一块临时盘但需要 StorageClass 动态供给”的场景。它不是 emptyDir，数据在卷上，但 Pod 删除后卷按策略删除。
- **VolumeAttributesClass**（1.34 GA）：在不重建 PVC 的情况下调整卷的属性（如 IOPS/吞吐），依赖驱动支持；与扩容（容量）是不同维度。它改的是 `volumeAttributesClassName`，不是 `resources.requests.storage`。
- **Volume Health**：部分 CSI 驱动会上报卷健康事件/条件，用于发现“卷不可用但 Pod 还 Running”的情况。

```yaml
# 通用临时卷
spec:
  volumes:
    - name: scratch
      ephemeral:
        volumeClaimTemplate:
          spec:
            accessModes: ["ReadWriteOnce"]
            storageClassName: fast-ssd
            resources:
              requests:
                storage: 100Gi
```

### 12.10.3 CSI 迁移与版本兼容

CSI migration 把树内（in-tree）存储插件迁移到 CSI，同时保持旧 API 兼容。平台要注意：

- 迁移是否完成、是否还有树内卷。
- 迁移后的 StorageClass/参数是否等价。
- 旧 PV 的 `volume.beta.kubernetes.io/storage-provisioner` 注解。
- 驱动升级与 Kubernetes 版本的兼容矩阵。
- 回滚路径：迁移通常不可逆，先在预发验证。

### 12.10.4 Local PV、本地盘与调度约束

Local PV 把节点上的磁盘/目录暴露为 PV，绕过网络存储，适合数据库、AI 训练缓存等需要本地 NVMe 的场景。

```yaml
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: local-nvme
provisioner: kubernetes.io/no-provisioner
volumeBindingMode: WaitForFirstConsumer
---
apiVersion: v1
kind: PersistentVolume
metadata:
  name: local-pv-node-a
spec:
  capacity:
    storage: 1Ti
  accessModes: ["ReadWriteOnce"]
  storageClassName: local-nvme
  local:
    path: /mnt/nvme/vol1
  nodeAffinity:
    required:
      nodeSelectorTerms:
        - matchExpressions:
            - key: kubernetes.io/hostname
              operator: In
              values: ["node-a"]
```

要点：

- Local PV **必须**带 nodeAffinity；没有它，Pod 可能被调度到没有这块盘的节点。
- 用 `volumeBindingMode: WaitForFirstConsumer`，让调度器先选节点再绑定，避免“PV 绑定了但 Pod 调度不上去”。
- 本地盘没有跨节点冗余；节点故障 = 数据不可用。要靠副本/备份/应用层复制解决。
- 静态供给适合少量大盘；大量节点盘可以用 local-volume-provisioner 或 OpenEBS LocalPV 自动发现。
- `hostPath` 不是 Local PV 的替代品：它绕过 PV/PVC 生命周期、调度和配额，只适合单节点调试。

### 12.10.5 卷健康与可观测性

卷“挂载成功”不代表“I/O 健康”。生产要关注：

- CSI 驱动的 `VolumeCondition`/事件（取决于驱动）。
- 节点上的 `dmesg`、块设备错误、文件系统只读重挂。
- `container_fs_*`、`kubelet_volume_stats_*` 指标；后者可以提前发现容量耗尽。
- 应用层错误率与延迟；存储故障经常先表现为 P99 抖动，而不是 Pod 重启。

## 12.11 StatefulSet 与存储

StatefulSet 的 volumeClaimTemplates 为每个 ordinal 创建 PVC。缩容/删除时 PVC 默认保留。

生产注意：

- 数据库卷使用 Retain。
- 定期备份，不要只依赖 PVC。
- 节点维护时，有状态 Pod 的迁移需要卷先 attach 到新节点。
- 多副本数据库需要应用层复制，不要共享同一个 RWO 卷。
- 使用 PDB 控制维护期间的可用性。

## 12.12 MiniK8s 的存储实现

`mini_k8s/controllers/storage.py`：

- 为 PVC 添加 `kubernetes.io/pvc-protection` finalizer。
- 默认 StorageClass 为 `standard`，动态创建 hostPath PV。
- 支持 `WaitForFirstConsumer`：Pod 调度后才绑定。
- 静态 PV 匹配：容量、accessModes、storageClass、selector。
- PVC 删除：被 Pod 使用时保留；否则移除 finalizer，按 reclaimPolicy 删除/保留 PV。
- 调度器检查 PVC 存在且 Bound，WFFC 例外。

测试：

- `test_dynamic_provisioning_binds_pvc_and_pod`
- `test_static_pv_binding_and_retain`
- `test_wait_for_first_consumer`
- `test_pvc_finalizer_protects_mounted_claim`

示例：

```bash
python -m mini_k8s.cli apply -f examples/07-storage.yaml
python -m mini_k8s.cli get pvc
python -m mini_k8s.cli get pv
```

## 12.13 常见坑

- 用 hostPath 跑生产应用：节点故障/迁移后数据丢失。
- StorageClass 默认值不唯一：PVC 行为不确定。
- 不做 WFFC：Pod 因卷拓扑无法调度。
- PVC 删除后数据被自动删除：回收策略没设 Retain。
- 以为 RWO 是单 Pod：同节点多 Pod 可同时挂载，可能冲突。
- 忘记扩容需要文件系统 resize。
- 快照放在同一故障域，灾难时一起丢失。
- 没有监控卷使用率，磁盘写满导致数据库崩溃。

## 12.14 数据保护：快照、备份与恢复

PV/PVC 只保证“卷存在”，不保证“数据可恢复”。生产需要区分三层：

1. **基础设施快照**：云盘/CSI VolumeSnapshot。快照在存储侧，恢复快，但通常绑定区域/厂商。
2. **应用一致性备份**：数据库需要 quiesce/事务日志/专用工具（pgBackRest、Percona XtraBackup、Velero 的 pre/post hook）。
3. **集群资源备份**：Namespace、Deployment、Service、PVC、CRD 等对象，用 Velero 等工具导出。

### Velero 的工作方式

```bash
velero backup create app-backup --include-namespaces team-a
velero backup describe app-backup --details
velero restore create --from-backup app-backup
```

- 资源对象通过 API 导出到对象存储。
- 卷数据通过 CSI 快照或文件级备份（Kopia/Restic）处理。
- 支持 schedule、TTL、hook、namespace 映射、选择性恢复。
- 恢复时要考虑 StorageClass/镜像/Secret 是否仍然存在，以及 CRD 先于 CR 恢复。

### 恢复演练要验证什么

- **RTO**：从开始恢复到服务可用需要多久。
- **RPO**：丢失多少数据；快照频率与数据库 WAL 是否匹配。
- **依赖**：镜像仓库、KMS、DNS、证书、Ingress、外部数据库是否可用。
- **顺序**：CRD → namespace → RBAC/Secret → 工作负载 → 数据。
- **一致性**：恢复后的数据是否通过应用校验，而不只是 Pod Running。

### 常见误区

- 只备份 etcd，不备份 PV：集群对象回来了，数据没了。
- 只做卷快照，不做应用一致性：数据库恢复后需要长时间 crash recovery，甚至损坏。
- 备份和集群在同一区域/账号：区域故障或账号被删时一起丢失。
- 从未恢复过：备份任务成功不等于可恢复。

MiniK8s 的存储是 hostPath 模拟，没有真实数据；它验证的是 PVC 绑定/保留语义，不是备份。备份策略应在真实存储层和应用层分别设计。

## 本章小结

- Volume 是 Pod 的存储抽象，PV/PVC 解耦供给与消费。
- AccessMode 是节点级语义，不是应用级锁。
- StorageClass + CSI 提供动态供给、拓扑、快照和扩容。
- WFFC 解决云盘拓扑与调度顺序问题。
- 有状态数据仍需备份、复制和故障演练，Kubernetes 不替你保证数据安全。

## 延伸阅读

- [第 9 章 StatefulSet](09-statefulset.md)
- [第 17 章 调度器](17-scheduler.md)
- [第 28 章 高可用与灾难恢复](28-ha-disaster-recovery.md)
