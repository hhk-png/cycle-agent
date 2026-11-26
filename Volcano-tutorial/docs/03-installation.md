# 第 3 章 安装与快速上手

## 3.1 版本与兼容性

Volcano 的发布节奏大约是每 6–9 个月一个 minor，patch 线维护多个版本。写这本教程时（2026-11）的事实基线：

| 版本 | 发布时间 | 关键内容 | Kubernetes |
|---|---|---|---|
| **v1.16.0** | 2026-10-28 | NamespaceQueue、fairshare、PodGroup 拓扑反亲和、独立 HyperNode controller、Unschedulable Job Cache | 1.36（依赖 1.36.1，Go 1.26 构建） |
| v1.15.4 | 2026 下半年 | gang 粒度抢占/回收、DRA 队列配额、多分片策略、Benchmark 框架、调度门控 | 1.35 |
| v1.14.7 | 2026 上半年 | 动态节点分片、Agent Scheduler Alpha、HyperNode binpack/子组、Job partition、混部增强、Volcano Global 0.3 | 1.34 |
| v1.13.3 | 2025-09 起 | LWS、Cron VolcanoJob、Ray、HCCL、标签式 HyperNode 自动发现、ResourceStrategyFit | 1.33 |

安全提示（CVE 编号与受影响范围以官方 advisory 为准，这里用于说明升级策略）：

- **CVE-2026-44247**（CVSS 6.8）：webhook 对无界 HTTP body 的处理可导致 OOM。受影响范围：`< 1.12.4`、`1.13.0–1.13.2`、`1.14.0–1.14.1`；修复在 v1.12.4、v1.13.3、v1.14.2 及后续 patch 线。
- **GHSA-j38h-7pfq-cxmw**（CVSS 6.5）：DRA 容量核算的过度迭代可阻塞调度，v1.15.2 修复。

生产环境不要停留在旧的 patch 线。升级前读目标版本的 release notes，尤其是 CRD schema 变化。

## 3.2 前置条件

- 一个 Kubernetes 集群（v1.16 建议 1.36；旧版本 Volcano 对应较低 K8s）。
- `kubectl` 与集群管理员权限。
- 如果要跑 GPU 作业：NVIDIA GPU Operator 或 device plugin、`nvidia.com/gpu` 资源、节点标签/污点按需配置。
- 如果要跑 RDMA/拓扑作业：HyperNode CR 与相应发现机制（UFB/RoCE/节点标签）已配置。
- Helm 3（推荐）或直接 apply 仓库里的 installer 清单。

最小验证集群可以是 kind/minikube，但**没有 GPU**。CPU 作业可以完整验证调度流程；GPU 作业只会 Pending。想在无 GPU 环境验证调度决策，可以用 KWOK 模拟节点，但 Pod 不会真的运行。

## 3.3 用 Helm 安装

```bash
helm repo add volcano-sh https://volcano-sh.github.io/helm-charts
helm repo update

helm install volcano volcano-sh/volcano \
  -n volcano-system --create-namespace \
  --version 1.16.0
```

验证：

```bash
kubectl get pods -n volcano-system
# 期望看到 volcano-admission-*、volcano-controllers-*、volcano-scheduler-*

kubectl get crd | grep -E 'volcano|batch.volcano|flow.volcano|topology.volcano'
kubectl get queue
# 至少应有 default；层级模式下还会有 root
```

如果要使用 v1.16 的 NamespaceQueue（Alpha，默认关闭）：

```bash
helm upgrade volcano volcano-sh/volcano -n volcano-system \
  --set custom.namespace_queue_enable=true
```

它要求调度器启用层级 capacity，且不要与 proportion 同时使用；卸载前先备份 NamespaceQueue（该 CRD 在 v1.16 没有 `helm.sh/resource-policy: keep` 注解，卸载时可能被一并删除）。

如果 `volcano-admission` 起不来，先看证书 init Job：

```bash
kubectl get job -n volcano-system
kubectl logs -n volcano-system job/volcano-admission-init
```

## 3.4 用清单安装（离线/受控环境）

仓库里提供了开发清单：

```bash
git clone --depth 1 --branch v1.16.0 https://github.com/volcano-sh/volcano.git
kubectl apply -f volcano/installer/volcano-development.yaml
```

离线环境需要提前同步镜像：

```text
volcanosh/vc-scheduler:v1.16.0
volcanosh/vc-controller-manager:v1.16.0
volcanosh/vc-webhook-manager:v1.16.0
```

并用私有 registry 覆盖 Helm values 中的 image repository/tag。

## 3.5 第一个 Volcano Job

保存为 `hello-vcjob.yaml`：

```yaml
apiVersion: batch.volcano.sh/v1alpha1
kind: Job
metadata:
  name: hello
  namespace: default
spec:
  schedulerName: volcano
  queue: default
  minAvailable: 2
  maxRetry: 2
  tasks:
    - name: worker
      replicas: 2
      template:
        spec:
          restartPolicy: OnFailure
          containers:
            - name: worker
              image: busybox:1.36
              command: ["sh", "-c", "echo hello from $(hostname); sleep 5"]
              resources:
                requests:
                  cpu: "500m"
                  memory: 256Mi
```

提交并观察：

```bash
kubectl apply -f hello-vcjob.yaml
kubectl get vcjob hello -w
kubectl get podgroup
kubectl get pods -l volcano.sh/job-name=hello -o wide
```

预期：

1. `vcjob` 保持 `Pending`，直到 Running Pod 数达到 `minAvailable`；
2. PodGroup 从 `Pending` 到 `Inqueue` 再到 `Running`；
3. 两个 Pod 同时被绑定（gang），而不是一个先跑一个等；
4. 5 秒后 Job `Completed`。

看事件与状态：

```bash
kubectl describe vcjob hello
kubectl describe podgroup hello
kubectl get events --sort-by=.lastTimestamp | tail -20
```

## 3.6 vcctl

`vcctl` 是 Volcano 的 CLI。可以从 release 资产下载，或从源码安装：

```bash
go install volcano.sh/volcano/cmd/cli@v1.16.0
vcctl job list
vcctl job view hello
vcctl queue list
```

不同版本的子命令略有差异，以 `vcctl --help` 为准。日常排障用 `kubectl get vcjob/podgroup/queue -o yaml` 通常更直接。

附录 C 会给出 vcctl/kubectl 的完整命令速查；本教程的主线仍是 Kubernetes API。

## 3.7 GPU 作业的前置检查

```bash
kubectl get nodes -o json | jq '.items[].status.allocatable["nvidia.com/gpu"]'
kubectl get pods -n kube-system | grep -E 'nvidia|gpu-operator'
```

一个 GPU 节点应该满足：

- `status.allocatable` 里有 `nvidia.com/gpu`；
- GPU Operator/device plugin Pod 正常；
- 如果节点有污点（如 `nvidia.com/gpu=present:NoSchedule`），作业要加 toleration；
- 作业 requests 与 limits 都写 GPU（Kubernetes 对扩展资源要求相等）。

异构 GPU 用节点标签选择：

```yaml
nodeSelector:
  nvidia.com/gpu.product: NVIDIA-A100-SXM4-80GB
```

具体标签键取决于 GPU Operator 版本，先用 `kubectl get node --show-labels` 确认。

## 3.8 常见安装/启动故障

| 现象 | 可能原因 | 排查 |
|---|---|---|
| Pod 一直 Pending，`nodeName` 为空 | schedulerName 不是 volcano；队列 Closed；gang 未满足；scheduler 未运行 | `kubectl get pod -o yaml` 看 `schedulerName`；`kubectl get queue`；scheduler 日志 |
| 报 `pod group is not ready, N minAvailable` | 资源不足、亲和性/污点、队列配额不足 | `kubectl describe podgroup`；`vcctl job view` |
| 提交被 webhook 拒绝 | 队列不存在/Closed、minAvailable 非法、CRD 版本不匹配 | 看 `kubectl describe vcjob` 的 Events |
| scheduler CrashLoopBackOff | scheduler configmap 语法错误、插件名拼错、tier 重复 | `kubectl logs deploy/volcano-scheduler -n volcano-system` |
| 所有作业都不调度 | admission 证书过期、scheduler 与 controller 版本 skew | 检查 webhook、Pod 日志 |
| GPU 资源为 0 | device plugin 未装/节点未打标 | `kubectl describe node` |

一条通用原则：**先看 Pod 的 `schedulerName`，再看 PodGroup 的 phase，最后看 Queue 的 state 和 scheduler 日志。** 三者能覆盖 80% 的“不调度”问题。

## 3.9 卸载与重装

```bash
helm uninstall volcano -n volcano-system
kubectl get crd | grep volcano   # 确认是否残留 CRD
```

多数 CRD 默认可能保留，但 NamespaceQueue 是例外：它没有 `helm.sh/resource-policy: keep`，`helm uninstall` 可能连带删除。重装前先确认：

```bash
kubectl get crd namespacequeues.scheduling.volcano.sh -o yaml | grep -A2 annotations
```

如果要彻底清理：

```bash
kubectl delete crd \
  jobs.batch.volcano.sh \
  cronjobs.batch.volcano.sh \
  podgroups.scheduling.volcano.sh \
  queues.scheduling.volcano.sh \
  hypernodes.topology.volcano.sh \
  jobflows.flow.volcano.sh \
  jobtemplates.flow.volcano.sh
```

删除 CRD 会删除所有对应对象，生产环境慎用。

## 3.10 先跑 MiniVolcano（不依赖集群）

教程配套实现只需要 Go：

```bash
cd mini-volcano
go test ./... -count=1
go run ./cmd/minivolcano -list
go run ./cmd/minivolcano -scenario basic
go run ./cmd/minivolcano -scenario gang -explain default/train
```

它不连接 Kubernetes，也不创建真实 Pod；它用离散事件模拟调度器、控制器和 kubelet 的关键行为。适合在没有集群、没有 GPU 的机器上理解语义，也适合作为二次开发的试验田。

## 3.11 托管 Kubernetes 与自建集群的五个差异

EKS、GKE、AKS、ACK 等托管集群上，Volcano 的 Helm 安装命令与自建集群相同，但下面五处不同。它们不是 Volcano 的配置问题，而是云环境的前置条件。

### 3.11.1 节点初始化与 device plugin

托管节点池的 GPU 驱动、container runtime、device plugin 由 AMI/GPU Operator/托管插件提供，不保证 `nvidia.com/gpu` 自动出现在 `allocatable`：

```bash
kubectl get node <gpu-node> -o jsonpath='{.status.allocatable.nvidia\.com/gpu}'
kubectl get pods -n kube-system | grep -i -E 'nvidia|device-plugin|gpu-operator'
```

如果资源名是 `nvidia.com/gpu.shared`、`amd.com/gpu` 或 `huawei.com/Ascend910`，队列 capability、ResourceQuota 和作业 requests 都要用同一个名字。托管环境对 privileged DaemonSet、hostPath、驱动安装可能有额外限制；受限模式（例如 GKE Autopilot）不确定时先用 Standard 节点池验证。

### 3.11.2 污点、标签与节点池

GPU 节点常带污点，Volcano 默认不会自动补 toleration：

```yaml
tolerations:
  - key: nvidia.com/gpu
    operator: Exists
    effect: NoSchedule
```

节点组/队列 affinity 依赖的标签也必须落在节点上；托管节点池的标签传播方式不同（节点池模板、autoscaler 标签、手动 label），升级/扩容后要重新确认。一个常见故障是“手工给节点打了 `volcano.sh/nodegroup-name`，autoscaler 新扩的节点没有”，于是新节点对 nodegroup 队列不可用。

### 3.11.3 自动扩缩容不理解 gang

Cluster Autoscaler / Karpenter / 托管节点自动扩缩容看到的是 **Pending Pod**，不是 **PodGroup 的 minAvailable**：

```text
gang 需要 8 个 Pod，集群只有 3 个节点的空位
→ autoscaler 为 8 个 Pending Pod 扩 5 个节点
→ 扩容完成前 gang 一直 Pending
→ 如果 autoscaler 只为“当前可放下”的 Pod 扩容，可能永远凑不齐
```

对策：

- 节点池保留 headroom，或用低优先级 placeholder Pod 预留容量；
- 给大 gang 单独的节点池，设置最小容量；
- 接受 1–5 分钟的扩容延迟，把它算进 SLA；
- 缩容时确认 PDB/gang/checkpoint 能承受驱逐；`cdp` 只能防抢占，不防节点 drain。

### 3.11.4 存储与多机网络

多 Pod 共享数据要用 `ReadWriteMany`：块存储通常是 `ReadWriteOnce`，托管文件存储（EFS/Filestore/Azure Files 等）才支持多挂载。作业级 `volumes` 会挂到每个 Pod，访问模式选错会在第二个 Pod 启动时才暴露。

多机训练还要确认：

- NCCL 走什么网络（IB/RDMA/EFA/普通 TCP）；
- 节点是否在同一放置组/低延迟域；
- CNI 是否支持所需 MTU 与多网卡；
- HyperNode 的 tier 是否与云的网络层次对齐（机架/放置组/AZ）。

这些属性直接影响 `networkTopology` 与 `topologyPolicy` 的效果；调度器只能按标签/CRD 约束，不能替云网络做保证。

### 3.11.5 控制面权限与云 API

托管控制面的 webhook、admission、私有端点、镜像拉取策略由云平台管理：

- 私有集群要确认 webhook 能被 API Server 访问，证书 SAN 包含服务名；
- 作业插件如果要创建云资源（对象存储、日志、密钥），用 IRSA/Workload Identity 等最小权限身份，不要把 AK/SK 写进 Pod env；
- 节点池的 IAM 权限与 Pod 的 IAM 权限分开；作业插件跑在 controller 的 ServiceAccount 下，不是用户 Pod 的身份。

### 3.11.6 上线前检查清单

| 检查 | 命令/证据 |
|---|---|
| GPU 资源名与数量 | `kubectl get node -o jsonpath='{.status.allocatable}'` |
| device plugin 就绪 | `kubectl get pods -n kube-system`、GPU Operator 状态 |
| GPU 污点与 toleration | `kubectl describe node`、作业 YAML |
| 节点组标签 | `kubectl get nodes -L volcano.sh/nodegroup-name` |
| autoscaler 行为 | 扩一个 gang，观察 Pending→节点增加→Running 的时间线 |
| 共享存储访问模式 | PVC `accessModes` 与多 Pod 挂载测试 |
| 多机网络 | NCCL 测试/厂商工具，确认 RDMA 设备与拓扑标签 |
| 控制面可达性 | webhook 证书、私有端点、admission 日志 |

## 3.12 本章小结

- 用 Helm 安装 Volcano，确认三个 Deployment 与 CRD 就绪。
- 第一个作业要同时观察 vcjob、PodGroup、Pod 三种对象。
- GPU 作业的前置条件是 device plugin、资源名、标签/污点。
- 排障顺序：schedulerName → PodGroup phase → Queue state → scheduler 日志。
- 托管集群的差异主要在节点初始化、污点/标签、autoscaler 与 gang、共享存储/网络、控制面权限五处。
- MiniVolcano 可以脱离集群验证调度语义。

下一章深入 Volcano Job 的字段、生命周期与作业插件。
