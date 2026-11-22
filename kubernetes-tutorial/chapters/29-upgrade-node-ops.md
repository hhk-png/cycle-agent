# 第 29 章 升级与节点运维

## 29.1 版本偏斜（version skew）

Kubernetes 组件不要求版本完全一致，但有支持范围：

- kube-apiserver 与 kubelet：kubelet 通常最多落后 3 个 minor（以官方 skew policy 为准）。
- kube-apiserver 与 controller-manager/scheduler：通常同一 minor 或落后 1 个。
- kubectl 与 API Server：通常 ±1 minor。
- kube-proxy 与 kubelet：同一节点通常同版本。

升级前必须查当前版本的官方 skew policy。超出范围可能导致：

- API 不兼容。
- 节点注册失败。
- 控制器行为异常。
- 难以回滚。

## 29.2 升级顺序

控制面优先：

1. 备份 etcd。
2. 升级第一个控制面节点（API Server/etcd/controller/scheduler）。
3. 验证 API、调度、控制器。
4. 逐个升级其余控制面节点。
5. 升级节点（kubelet/kube-proxy）。
6. 升级附加组件（CNI、CSI、CoreDNS、Ingress、metrics-server）。
7. 验证工作负载。

不要先升级 kubelet 到高于 API Server 太多。

## 29.3 节点升级策略

### 滚动 drain

```bash
kubectl cordon node-1
kubectl drain node-1 --ignore-daemonsets --delete-emptydir-data
# 升级 OS/kubelet/runtime
systemctl restart kubelet
kubectl uncordon node-1
```

优点：简单，逐节点。

缺点：

- 有状态 Pod 可能卡住。
- PDB 可能阻止。
- 大规模慢。

### 节点池替换（immutable infrastructure）

1. 创建新节点池（新镜像/新版本）。
2. 将工作负载迁移/调度到新池。
3. 缩容旧池。
4. 删除旧节点。

优点：

- 可回滚。
- 节点状态干净。
- 适合云环境。

缺点：

- 成本翻倍（过渡期）。
- 需要节点池管理。

### 蓝绿集群

新建集群，部署应用，切换 DNS/LB，旧集群保留回滚。

优点：隔离好。

缺点：数据层同步复杂，成本高。

## 29.4 升级前检查

- 弃用 API：`kubectl api-resources`、`pluto`、`kube-no-trouble`。
- CRD 版本/转换。
- admission webhook 兼容性。
- CNI/CSI/Ingress 版本支持矩阵。
- 节点 OS/内核/容器运行时版本。
- 证书有效期。
- etcd 健康/大小/碎片。
- 备份可用性。
- 容量缓冲（升级期间可能需要额外节点）。
- PodDisruptionBudget。
- 关键应用的就绪/优雅终止。

### 29.4.1 1.35–1.37 的升级阻断项

升级到 1.35+ 前，至少确认这些“硬阻断”：

| 变化 | 影响 | 检查 |
|---|---|---|
| cgroup v1 移除（1.35） | 节点可能 NotReady | `stat -fc %T /sys/fs/cgroup` 必须是 `cgroup2fs`；OS/运行时支持 cgroup v2 |
| IPVS 弃用（1.35） | 新集群不应选 IPVS；存量要迁移 | kube-proxy 模式、CNI 是否绕过 kube-proxy |
| SELinuxMount 默认（1.37） | 共享卷/多租户 Pod 可能卡在 ContainerCreating | 盘点 RWX/hostPath/NFS；准备 `seLinuxChangePolicy: Recursive` |
| cAdvisor 旧标志/指标移除（1.37） | 监控仪表盘静默变空 | 对比 `/metrics`，检查 Prometheus rule/Grafana |
| DRA API v1（1.34 GA） | 旧 v1beta1/v1beta2 客户端/驱动失败 | 驱动与 controller 的 served-version 发现 |
| `scheduling.k8s.io/v1alpha2` 等旧 API 移除 | 清单/控制器 404 | `pluto`/`kubent` + `--dry-run=server` |
| 原地扩缩容 GA（1.35） | 运行时/控制器需处理 resize conditions | 先在预发验证 `PodResizePending` 路径 |

详细版本时间线与验收矩阵见[附录 R](../appendices/R-version-evolution-1.32-1.37.md)。

## 29.5 升级中的观察

- API Server 5xx/延迟。
- etcd leader 变化/fsync。
- 节点 NotReady。
- Pod 驱逐/重新调度。
- Deployment 可用副本。
- PDB 阻塞。
- 镜像拉取失败。
- 证书错误。

准备好回滚计划：

- 控制面回滚（降级）通常不受支持；更多是修复/重建。
- 节点可以回退镜像（如果 kubelet 版本兼容）。
- 应用回滚用 Deployment undo。
- 集群级变更用 GitOps 回滚。

## 29.6 节点运维

### 节点健康

- Node Ready 条件。
- kubelet 心跳。
- 容器运行时健康。
- 磁盘压力/内存压力/PID 压力。
- 内核日志（OOM、网络、文件系统）。

### 节点维护

- cordon/drain。
- 优雅终止。
- PDB。
- 本地存储处理。
- DaemonSet 处理。
- 静态 Pod 处理。

### 节点清理

- 删除旧镜像。
- 清理日志。
- 清理 orphaned volumes。
- 重置 kubelet 数据（谨慎）。
- 重新加入集群。

### 29.6.1 Node Problem Detector 与节点条件

kubelet 只上报它自己能感知的问题（运行时、磁盘、内存、PID）。内核死锁、硬件故障、文件系统只读、NTP 漂移等需要额外检测。Node Problem Detector（NPD）以 DaemonSet 运行，把节点问题写成：

- Node condition（如 `KernelDeadlock`、`ReadonlyFilesystem`）。
- Event。
- 可选的 taint（配合自愈/隔离）。

```yaml
# 概念性配置：NPD 的自定义检查
apiVersion: v1
kind: ConfigMap
metadata:
  name: node-problem-detector-config
  namespace: kube-system
data:
  kernel-monitor.json: |
    {
      "plugin": "kmsg",
      "logPath": "/dev/kmsg",
      "rules": [
        {
          "type": "KernelDeadlock",
          "reason": "AUFSUmountHung",
          "pattern": "task .* blocked for more than .* seconds"
        }
      ]
    }
```

落地要点：

- NPD 只负责“发现并上报”，是否驱逐/隔离由控制器或运维策略决定。
- 自定义 condition 要配套告警与 runbook，否则只是噪声。
- 节点条件与 taint **可以**触发自动替换，但不是默认行为：Cluster API 的 MachineHealthCheck/Karpenter 的 disruption/repair 都要显式配置，受 disruption budget、`nodeRepair`（Karpenter 新版本为 alpha，默认关闭）等门控。没有配置时，节点坏了也不会自动换；配置过激则会批量重建。先做 dry-run/小范围验证。
- 硬件问题（ECC、GPU Xid、网卡）通常由厂商 agent 上报，NPD 不是唯一来源。

### 29.6.2 优雅节点关闭

云上 Spot/抢占实例、计划维护、OS 升级都会让节点“消失”。优雅节点关闭（Graceful Node Shutdown）让 kubelet 在系统关机前：

1. 检测到关机信号（systemd inhibitor）。
2. 停止接受新 Pod。
3. 按优先级终止 Pod，执行 preStop/grace。
4. 上报 Node 状态。

配置：

```yaml
# kubelet 配置片段
shutdownGracePeriod: 60s
shutdownGracePeriodCriticalPods: 20s
```

注意：

- 总预算要覆盖最长的 preStop + 应用退出时间。
- 关键 Pod 的 grace 单独预留。
- 不是所有节点/发行版都支持；Spot 回收可能不给足时间。
- 与 PDB 配合：PDB 保护自愿驱逐，但关机是节点级事件，不能完全依赖 PDB。

### 29.6.3 节点生命周期 taint

NodeLifecycleController 会根据条件打 taint：

| 条件 | taint | 效果 |
|---|---|---|
| NotReady | `node.kubernetes.io/not-ready:NoExecute` | 驱逐不容忍的 Pod |
| Unreachable | `node.kubernetes.io/unreachable:NoExecute` | 网络失联 |
| MemoryPressure | `node.kubernetes.io/memory-pressure:NoSchedule` | 不再调度新 Pod |
| DiskPressure | `node.kubernetes.io/disk-pressure:NoSchedule` | 不再调度新 Pod |
| PIDPressure | `node.kubernetes.io/pid-pressure:NoSchedule` | 不再调度新 Pod |
| NetworkUnavailable | `node.kubernetes.io/network-unavailable:NoSchedule` | 网络未就绪 |

默认 toleration 由 DaemonSet/系统组件配置；业务 Pod 通常不 tolerate NotReady，因此节点失联后会在一段时间后被重新调度。`tolerationSeconds` 控制“失联后多久驱逐”，太短会因为网络抖动误驱逐，太长会延长故障恢复。

### 29.6.4 kubelet 证书轮转

kubelet 使用 TLS bootstrap 向 API Server 申请客户端证书。生产要确认：

- `--rotate-certificates=true`。
- 证书到期前自动轮转。
- `kubelet.conf` 中的用户属于 `system:nodes` 组。
- CSR 审批策略（自动批准仅限 bootstrap 的合理范围）。
- 监控证书剩余时间，避免“集群运行一年后突然全部 NotReady”。

```bash
# 查看 kubelet 客户端证书到期时间
openssl x509 -in /var/lib/kubelet/pki/kubelet-client-current.pem -noout -dates
```

### 29.6.5 节点维护 runbook

一个可执行的维护顺序：

1. 确认维护窗口与 PDB/副本余量。
2. `kubectl cordon <node>`，阻止新调度。
3. 检查本地存储/静态 Pod/DaemonSet 的特殊需求。
4. `kubectl drain <node> --ignore-daemonsets --delete-emptydir-data`（谨慎）。
5. 执行 OS/运行时/kubelet 升级。
6. 重启 kubelet/运行时，确认 Node Ready。
7. `kubectl uncordon <node>`。
8. 观察业务指标、Pod 分布、PDB 状态。
9. 记录耗时、异常、回滚点。

drain 失败时不要直接 `--force`；先看 PDB、本地卷、静态 Pod、finalizer。强制删除可能造成数据丢失或副本不足。

## 29.7 OS/内核/运行时升级

容器运行时升级可能影响：

- 已有容器（通常不影响，但 CRI 兼容性）。
- 镜像格式。
- cgroup 版本（v1 → v2）。
- seccomp/AppArmor。
- 网络插件。

内核升级可能影响：

- eBPF 程序。
- 网络性能。
- 文件系统。
- 安全模块。

建议：

- 先在非生产节点池验证。
- 保留回滚镜像。
- 监控节点上的关键 DaemonSet。
- 避免同时升级所有节点。

## 29.8 证书与凭据

- API Server 证书。
- etcd 证书。
- kubelet 证书（通常自动轮换）。
- ServiceAccount 签名密钥。
- Ingress TLS 证书。
- Webhook 证书。
- 云凭据/IRSA。

监控到期时间，提前轮换。证书过期可能导致：

- API Server 不可用。
- kubelet 无法注册。
- webhook 拒绝所有请求。
- Ingress TLS 失败。

## 29.9 容量与成本

升级期间可能需要：

- 额外节点（surge）。
- 额外存储（快照/备份）。
- 双倍集群（蓝绿）。

预算和配额要提前准备。升级后：

- 清理旧节点/旧池。
- 清理旧镜像/快照。
- 更新成本标签。

## 29.10 变更管理

- 所有集群变更走 GitOps/流水线。
- 变更前 dry-run/diff。
- 变更窗口/审批。
- 回滚演练。
- 变更后验证（SLO、关键业务）。
- 记录事故/复盘。

不要让“临时 kubectl edit”成为常态；它会导致配置漂移和不可复现。

## 29.11 MiniK8s 的边界

MiniK8s 不模拟集群升级。但它的版本字符串、API 资源注册表、CRD 版本可以用于理解：

- 版本发现。
- API 弃用。
- CRD served/storage 版本。

## 29.12 kubeadm 集群升级实操序列

以下以“单控制面 → 多控制面 → 工作节点”的典型 kubeadm 集群为例。版本号用 `v1.37.x` 表示（截至 2026-11 的受支持 minor），实际执行时替换为目标补丁版本；如果当前还在 1.34 或更早，必须按 minor 逐个升级，不能直接跳到 1.37。

### 升级前

```bash
# 备份
etcdctl snapshot save /backup/etcd-pre-upgrade.db ...
kubectl get nodes -o wide
kubectl get pods -A | grep -v Running
kubeadm upgrade plan

# 检查弃用 API / webhook / CNI / CSI 兼容性
pluto detect-all-in-cluster
kubectl get validatingwebhookconfigurations,mutatingwebhookconfigurations
```

### 第一个控制面节点

```bash
# 1. 升级 kubeadm
apt-mark unhold kubeadm
apt-get update && apt-get install -y kubeadm=1.37.x-*
apt-mark hold kubeadm

# 2. 应用升级（会升级静态 Pod/证书）
kubeadm upgrade apply v1.37.x

# 3. 升级 kubelet/kubectl
apt-mark unhold kubelet kubectl
apt-get install -y kubelet=1.37.x-* kubectl=1.37.x-*
apt-mark hold kubelet kubectl
systemctl daemon-reload && systemctl restart kubelet

kubectl get nodes   # 该节点版本应更新
```

### 其余控制面节点

```bash
# 在每个节点上
apt-get install -y kubeadm=1.37.x-*
kubeadm upgrade node
# 再升级 kubelet/kubectl 并重启
```

### 工作节点

```bash
# 控制面执行
kubectl drain <node> --ignore-daemonsets --delete-emptydir-data

# 节点上
apt-get install -y kubeadm=1.37.x-* kubelet=1.37.x-* kubectl=1.37.x-*
kubeadm upgrade node
systemctl daemon-reload && systemctl restart kubelet

# 控制面执行
kubectl uncordon <node>
```

### 升级后

```bash
kubectl get nodes -o wide
kubectl get pods -A | grep -v Running
kubectl -n kube-system get pods
kubectl get --raw /metrics | grep apiserver_request_total | head
```

### 常见问题

- **kubelet 版本高于 apiserver**：违反 skew policy，先升级控制面。
- **drain 卡住**：PDB、本地存储、DaemonSet、静态 Pod；逐项排查，不要直接 `--force`。
- **CNI/CSI 不兼容**：升级前查兼容矩阵；升级后 Pod 网络/卷挂载失败。
- **证书过期**：`kubeadm certs check-expiration`，升级前续期。
- **控制面回滚困难**：kubeadm 不支持降级；要么修复，要么从备份重建。
- **节点池替换优先**：云环境用新节点池 + 缩旧池，比原地升级更可控。

MiniK8s 不模拟升级；这一节是真实集群的操作基线，配合附录 F 的安装步骤一起看。

## 本章小结

- 升级顺序：控制面 → 节点 → 附加组件，遵守版本偏斜。
- 优先不可变节点池/蓝绿，减少原地升级风险。
- 升级前检查弃用 API、CRD、webhook、CNI/CSI、证书和备份。
- 节点运维的核心是 cordon/drain/PDB/优雅终止。
- 变更管理和回滚演练比升级命令本身更重要。
- kubeadm 升级要按“第一个控制面 → 其余控制面 → 工作节点”执行，并保留 etcd 备份与回滚预案。

## 延伸阅读

- [第 28 章 高可用与灾难恢复](28-ha-disaster-recovery.md)
- [第 30 章 性能与规模化](30-performance-scale.md)
- [第 31 章 故障排查方法论](31-troubleshooting.md)
- [附录 I 版本、特性门控与弃用路线](../appendices/I-version-and-deprecation.md)
- [附录 K etcd 深入](../appendices/K-etcd-operations.md)：升级前的快照与恢复演练。
- [附录 S PKI、证书与身份生命周期](../appendices/S-pki-and-certificates.md)：控制面证书续期、kubelet 轮换与过期故障。
