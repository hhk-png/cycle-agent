# 附录 T Windows 容器与混合操作系统集群

Kubernetes 最初是 Linux 容器编排器，但生产集群经常同时承载 Linux 与 Windows 工作负载：.NET Framework 应用、IIS、Windows 服务、依赖 COM/注册表的遗留系统。Windows 节点不是“换个 kubelet 二进制”这么简单，它的隔离模型、网络、存储和安全边界都与 Linux 不同。本附录给出混合集群的工程边界与落地清单。

## T.1 什么时候需要 Windows 节点

适合：

- 现有 .NET Framework/IIS/Windows Service 无法短期容器化到 Linux；
- 依赖 COM、注册表、Windows 认证（Kerberos/NTLM）、组托管服务账号（gMSA）；
- 需要 Windows 上的 GPU（DirectX/CUDA on Windows）或特定驱动；
- 合规/供应商只支持 Windows。

不适合：

- 只是“团队熟悉 Windows”，但应用本身跨平台；
- 需要 Linux 内核特性（eBPF、overlayfs、cgroup v2、seccomp、AppArmor、SELinux）；
- 追求最高 Pod 密度或最低授权成本。

混合集群的复杂度是真实的：镜像要 multi-arch、DaemonSet 要按 OS 选择、网络/存储插件要双栈支持、监控/日志代理要两套、升级窗口要协调。先确认收益大于运维成本。

## T.2 Windows 容器的隔离模型

Windows 容器有两种隔离级别：

| 模式 | 机制 | 特点 |
|---|---|---|
| Process isolation | 共享宿主内核，容器进程是宿主上的进程 | 启动快、开销小；要求宿主与容器 OS 版本/补丁兼容 |
| Hyper-V isolation | 每个容器（或 Pod sandbox）跑在轻量 VM 中 | 兼容性更好、隔离更强；启动慢、开销大 |

关键差异：

- **没有 Linux 内核**。Windows 容器不能运行 Linux 二进制；Linux 容器也不能运行 Windows 二进制。多架构镜像（manifest list）让同一 tag 在不同节点拉取不同层。
- **基础镜像必须匹配**。`ltsc2022`、`ltsc2025` 等 tag 与宿主版本绑定；process isolation 下不匹配会启动失败，Hyper-V isolation 可缓解但不能完全消除。
- **没有 privileged 容器**。Windows 不支持 Linux 的 privileged/capabilities。需要宿主级访问时使用 **HostProcess 容器**（1.26 GA）：以宿主进程身份运行，常用于 CNI、CSI、日志代理等 DaemonSet。
- **用户模型不同**。`securityContext.windowsOptions.runAsUserName` 使用 `DOMAIN\user` 或 `NT AUTHORITY\SYSTEM`；没有 Linux 的 UID/GID、supplementalGroups、fsGroup 语义（有有限的替代）。
- **文件系统语义不同**。大小写不敏感、路径分隔符、ACL 与 Linux 权限模型不同；挂载卷的权限行为也不同。

## T.3 节点组件

Windows 节点通常运行：

- **kubelet**（Windows 二进制）；
- **kube-proxy**（winkernel 模式，基于 HNS/Windows 网络栈）；
- **容器运行时**：containerd（推荐）；Docker Engine 已不是主流路径；
- **CNI 插件**：win-overlay、l2bridge 等，具体取决于网络方案；
- **CSI 插件**：通过 CSI Proxy 访问 Windows 存储；
- **日志/监控代理**：以 HostProcess 或普通 DaemonSet 运行。

控制面（apiserver/etcd/scheduler/controller-manager）仍运行在 Linux。不要把 Windows 节点当控制面。

加入集群的流程与 Linux 类似：准备容器运行时、CNI、kubelet 配置、bootstrap token，然后 `kubeadm join`（或发行版工具）。注意：

- kubelet 的 `--windows-service`、`--pod-infra-container-image` 等参数与 Linux 不同；
- 节点需要正确配置 HNS、防火墙、NAT；
- 节点标签必须包含 `kubernetes.io/os=windows`（kubelet 通常自动设置，仍建议核对）。

## T.4 调度与混合集群

用标准调度原语把 Pod 放到正确的 OS：

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: iis
spec:
  replicas: 2
  selector:
    matchLabels:
      app: iis
  template:
    metadata:
      labels:
        app: iis
    spec:
      nodeSelector:
        kubernetes.io/os: windows
      containers:
        - name: iis
          image: mcr.microsoft.com/windows/servercore/iis:windowsservercore-ltsc2022
          resources:
            requests:
              cpu: 500m
              memory: 512Mi
```

实践建议：

- **用 nodeSelector/affinity 显式选择 OS**，不要依赖“镜像拉取失败再重试”。
- 可以给 Windows 节点加 taint（如 `os=windows:NoSchedule`），只让带 toleration 的工作负载进入；这能防止 Linux Deployment 误调度过去。
- **DaemonSet 必须按 OS 区分**：一个 DaemonSet 的 Pod 模板只能有一种 OS；用 `nodeSelector` 或两个 DaemonSet。
- **镜像必须 multi-arch**：CI 构建 `linux/amd64` 与 `windows/amd64` 的 manifest list；不要在 Pod 里写死单平台 digest，除非明确只跑一种节点。
- **PDB/HPA/滚动更新**在混合集群同样适用，但 Windows 节点重启更慢，`maxUnavailable` 要给足余量。

## T.5 网络

Windows 网络栈与 Linux 差异很大：

- **没有 iptables**。kube-proxy 使用 winkernel/HNS 实现 Service 转发；不要照搬 Linux 的 iptables/IPVS 排障命令。
- **CNI 选择**：win-overlay（VXLAN）、l2bridge 等；跨 Linux/Windows 的 Pod 网络需要 CNI 同时支持两端，并正确配置 VXLAN/NAT/路由。
- **NetworkPolicy**：是否支持取决于 CNI；语义与 Linux 实现可能有差异，必须做跨 OS 的正向/负向验收。
- **DNS**：CoreDNS 通常运行在 Linux；Windows Pod 通过 Service/DNS 访问，注意 DNS 策略与 search domain。
- **hostNetwork**：Windows 支持 hostNetwork，但端口冲突与 NAT 行为需要单独验证。
- **负载均衡**：云 LB 对 Windows 节点的健康检查、NodePort、hostPort 支持要逐项确认。

网络排障顺序：

```text
1. Pod IP 是否分配？        kubectl get pod -o wide
2. 同节点 Pod 互通？        跨节点呢？
3. DNS 是否解析？           nslookup / Resolve-DnsName
4. Service ClusterIP 是否通？ kube-proxy/HNS 规则
5. NetworkPolicy 是否误拦？  临时移除策略对照
6. 宿主防火墙/WinNAT？       Get-NetFirewallRule / Get-NetNat
```

## T.6 存储

Windows 节点的存储路径：

- **CSI Proxy**：CSI 插件通过它调用 Windows 存储 API；没有 CSI Proxy，很多 CSI 驱动无法在 Windows 工作。
- **SMB/CIFS**：`ReadWriteMany` 常用 SMB 文件共享；注意 ACL、凭据、挂载选项。
- **云盘**：Azure Disk/File 等 CSI 驱动支持 Windows，但挂载语义、盘符、格式化行为与 Linux 不同。
- **local/hostPath**：可用于缓存/临时数据，不适合持久化业务数据。
- **卷权限**：Windows ACL 与 Linux `fsGroup` 不通用；应用要以正确的 `runAsUserName` 运行，否则会“挂载成功但读写拒绝”。

有状态工作负载在 Windows 上要额外验证：卷挂载、盘符稳定性、快照/克隆、扩容、节点 Drain 后的重挂载。

## T.7 安全

Windows 的安全基线不同于 Linux：

- **HostProcess 容器**是强能力：能访问宿主网络/文件系统/服务。只给基础设施 DaemonSet 使用，并用 PSA/策略限制。
- **PSA**：Pod Security Standards 的很多控制项是 Linux 中心的（privileged、capabilities、seccomp、AppArmor、SELinux）。Windows 侧要依赖 `windowsOptions`、HostProcess 限制、镜像签名与最小权限账号。
- **gMSA**：组托管服务账号用于访问 AD 资源；需要域控、CredentialSpec、节点配置配合。
- **镜像**：优先使用 Microsoft 维护的基础镜像与受信任 registry；固定 digest，扫描漏洞。
- **网络**：限制 hostNetwork/HostProcess；对 3389/5985/5986 等管理端口做严格隔离。
- **审计**：Windows 事件日志、ETW、容器日志要进入统一管道；不要只依赖 `kubectl logs`。

## T.8 可观测性与运维

- **日志**：stdout/stderr 可由 kubelet 采集；Windows Event Log、IIS 日志、ETW 需要额外代理（常以 HostProcess 运行）。
- **指标**：cAdvisor 在 Windows 上暴露的指标子集与 Linux 不同；CPU/内存/磁盘语义要重新校准告警阈值。
- **exec/debug**：`kubectl exec` 在 Windows 容器可用，但 shell 是 PowerShell/cmd，不是 bash；调试镜像要包含相应工具。
- **升级**：Windows 节点重启慢、补丁窗口长；先 Drain、验证 CNI/CSI、再升级 kubelet/运行时/OS。控制面与 Windows 节点的 version skew 仍受 Kubernetes 支持策略约束。
- **授权成本**：Windows Server 按核心授权，容器密度低；TCO 要把 OS 授权、节点数、运维人力算进去（见附录 N）。

## T.9 限制清单

- [ ] 控制面必须是 Linux；Windows 只作为工作节点。
- [ ] 容器 OS 版本与宿主匹配（process isolation），或接受 Hyper-V isolation 的开销。
- [ ] 没有 privileged/capabilities/seccomp/AppArmor/SELinux；用 HostProcess/PSA/账号策略替代。
- [ ] CNI/CSI/kube-proxy 必须明确支持 Windows；不要假设 Linux 插件可复用。
- [ ] 镜像 multi-arch；CI 同时构建/扫描两个平台。
- [ ] DaemonSet 按 OS 拆分；调度用 nodeSelector/taint 显式约束。
- [ ] 日志/指标/追踪代理有 Windows 版本。
- [ ] 升级/补丁/授权成本纳入 TCO。

## T.10 MiniK8s 的边界

MiniK8s 的节点标签包含 `kubernetes.io/os: linux`，调度器可以按标签选择节点，但**不模拟 Windows 运行时**：

- 没有 process/Hyper-V isolation、HNS、CSI Proxy、HostProcess；
- 不会拒绝 Linux-only 字段（privileged、seccomp 等）；
- 不能验证 Windows 镜像/网络/存储行为。

可以用 MiniK8s 练习“按 OS 标签调度”和“混合节点池”的控制面逻辑；真实 Windows 行为要在 Windows 节点或云托管 Windows 节点池上验证。扩展练习：给调度器增加 OS 兼容性检查，拒绝把声明 `kubernetes.io/os: windows` 的 Pod 放到 Linux 节点，并为 Windows 专用字段加准入校验。

## 延伸阅读

- [第 7 章 容器运行时、资源与 QoS](../chapters/07-runtime-resources-qos.md)
- [第 15 章 CNI、kube-proxy、eBPF 与 NetworkPolicy](../chapters/15-cni-ebpf-networkpolicy.md)
- [第 25 章 运行时安全与节点加固](../chapters/25-runtime-security-node-hardening.md)
- [附录 F 集群搭建与生产化手册](F-cluster-build.md)
- [附录 N 成本工程](N-cost-engineering.md)
