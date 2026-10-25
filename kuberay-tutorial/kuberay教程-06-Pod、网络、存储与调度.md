# 第 6 章 Pod、网络、存储与调度

KubeRay 最终要生成 Pod 和 Service。本章解释这些对象长什么样、为什么这样设计，以及生产环境最容易踩的坑。

## 6.1 一个 Ray Pod 的解剖

以 head Pod 为例：

```text
demo-head
├── labels
│   ├── ray.io/is-ray-node=yes
│   ├── ray.io/cluster=demo
│   ├── ray.io/node-type=head
│   ├── ray.io/group=headgroup
│   └── ray.io/identifier=demo-head
├── spec.containers[0] = ray
│   ├── command: /bin/bash -c
│   ├── args: ulimit -n 65536; ray start --head ...
│   ├── ports: 6379, 8265, 10001, 8080, 8000
│   ├── env: RAY_CLUSTER_NAME, RAY_ADDRESS, ...
│   └── resources: cpu/memory/gpu
├── spec.containers[1] = autoscaler (optional)
├── spec.containers[2] = history collector (optional)
├── volumes: /dev/shm (memory emptyDir), ray-logs
└── ownerReferences: RayCluster
```

### 6.1.1 标签

| 标签 | 值 | 用途 |
|---|---|---|
| `ray.io/is-ray-node` | `yes` | 通用选择器 |
| `ray.io/cluster` | 集群名 | 选择同一集群的所有 Pod |
| `ray.io/node-type` | `head`/`worker` | 区分 head/worker |
| `ray.io/group` | group 名 | 选择 worker group |
| `ray.io/identifier` | `<cluster>-head` | head Service 选择器 |
| `ray.io/serve` | `true`/`false` | Serve Service 选择器 |
| `ray.io/headless-worker-svc` | `true` | 多主机 headless Service |
| `app.kubernetes.io/name` | `kuberay` | 通用应用标签 |

用户可以在 Pod template 加标签，但 `ray.io/cluster`、`ray.io/node-type`、`ray.io/group` 不能被覆盖，否则控制器会失去对子资源的选择能力。

### 6.1.2 注解

| 注解 | 作用 |
|---|---|
| `ray.io/ft-enabled` | 旧式 GCS FT 开关（新版本用 `gcsFaultToleranceOptions`） |
| `ray.io/external-storage-namespace` | 旧式 Redis 命名空间 |
| `ray.io/overwrite-container-cmd` | 不覆盖用户 command，但仍注入 `KUBERAY_GEN_RAY_START_CMD` |
| `ray.io/enable-serve-service` | 让 RayCluster 直接创建 Serve Service |
| `ray.io/disable-provisioned-head-restart` | SidecarMode 下禁止重建已 provision 的 head |
| `ray.io/initializing-timeout` | RayService 初始化超时（Go duration 或秒） |
| `ray.io/gcs-ft-deletion-timeout` | GCS FT finalizer 强制移除的超时（整数秒，默认 300） |

## 6.2 KubeRay 生成的命令

### 6.2.1 head

```bash
ulimit -n ${RAY_START_ULIMIT_OPEN_FILES:-65536}; \
ray start --head --port=6379 \
  --dashboard-host=0.0.0.0 \
  --metrics-export-port=8080 \
  --block
```

`rayStartParams` 会逐项变成 `--key=value`。例如：

```yaml
rayStartParams:
  num-cpus: "8"
  object-store-memory: "8000000000"
```

变成：

```text
--num-cpus=8 --object-store-memory=8000000000
```

### 6.2.2 worker

```bash
ulimit -n ${RAY_START_ULIMIT_OPEN_FILES:-65536}; \
ray start --address=demo-head-svc.default.svc.cluster.local:6379 \
  --block
```

worker 的 `RAY_ADDRESS` 指向 head Service 的 GCS 端口（6379）：

```text
demo-head-svc.default.svc.cluster.local:6379
```

### 6.2.3 用户自定义命令

如果模板里已有 `ray start` 命令，KubeRay 不会覆盖。否则默认把用户 command 放在 `ray start` 前面：

```bash
<user command> && ulimit -n 65536; ray start ...
```

如果设置 `ray.io/overwrite-container-cmd: "true"`，KubeRay 完全不改 command，只把生成的命令放进 `KUBERAY_GEN_RAY_START_CMD` 环境变量，供用户自行引用。

## 6.3 环境变量

KubeRay 注入的关键变量：

| 变量 | head | worker | 说明 |
|---|---|---|---|
| `RAY_CLUSTER_NAME` | 是 | 是 | 集群名 |
| `RAY_CLUSTER_NAMESPACE` | 是 | 是 | 命名空间 |
| `RAY_ROLE` | `head` | `worker` | 节点角色（KubeRay 注入） |
| `RAY_NODE_TYPE_NAME` | group 名 | group 名 | 节点组名（KubeRay 注入） |
| `RAY_IP` / `FQ_RAY_IP` | — | head Service | worker 用来定位 head；head 不需要 |
| `RAY_PORT` | 6379 | 6379 | GCS 端口 |
| `RAY_ADDRESS` | 本地 | `<head-svc>:6379` | worker 的 GCS 地址（Ray 内部 `ray.init()` 可用；外部 Ray Client 仍是 10001） |
| `RAY_USAGE_STATS_KUBERAY_IN_USE` | 1 | 1 | 使用统计 |
| `RAY_DASHBOARD_ENABLE_K8S_DISK_USAGE` | 1 | 1 | Dashboard 磁盘指标 |
| `RAY_enable_autoscaler_v2` | true/false | - | autoscaler 版本（注意小写） |
| `RAY_AUTH_MODE` / `RAY_AUTH_TOKEN` | 可选 | 可选 | token auth |
| `RAY_USE_TLS` / 证书路径 | 可选 | 可选 | mTLS |

不要手动覆盖 `RAY_CLUSTER_NAME`、`RAY_CLUSTER_NAMESPACE`；它们是控制器与 Ray 的契约。`RAY_NODE_TYPE` 是 MiniKubeRay 的教学扩展，真实 KubeRay 使用 `RAY_ROLE` + `RAY_NODE_TYPE_NAME`。

## 6.4 Service 拓扑

### 6.4.1 head Service

名字：

- 默认 `<cluster>-head-svc`；
- 自定义 `spec.headGroupSpec.headService.metadata.name` 可覆盖。

默认类型：

- `ClusterIP: None`（headless）；
- `publishNotReadyAddresses: true`；
- 端口：client 10001、dashboard 8265、gcs-server 6379、metrics 8080、serve 8000。

为什么 headless？

- 一个 RayCluster 只有一个 head，不需要负载均衡；
- worker 需要稳定的 DNS 名加入 GCS；
- head 未 Ready 时 worker 仍应能解析地址（`publishNotReadyAddresses`）。

例外：如果设置 operator 环境变量 `ENABLE_RAY_HEAD_CLUSTER_IP_SERVICE=true`，head Service 会变成普通 ClusterIP（仍然只有一个后端）。这适合：

- 需要通过 Service IP 而不是 Pod IP 访问 Dashboard/Client 的平台；
- 某些 Ingress/网关只接受 ClusterIP Service；
- `status.head.serviceIP` 需要稳定地址的场景。

代价是 headless 的一些语义会变化（例如 DNS 直接解析到 Pod IP 的行为），升级前先确认客户端和网络策略。

### 6.4.2 Serve Service

RayService 会额外创建 `<service>-serve-svc`：

- selector：`ray.io/cluster=<active cluster>` + `ray.io/serve=true`；
- 端口：8000；
- `ray.io/serve=true` 是控制器维护的“可接收 Serve 流量”资格标签，不是健康探针；实际能否接流量还取决于 Pod readiness、EndpointSlice 和 Serve 应用状态（第 11.5、35.8.5 节）；
- 升级时切换 selector 指向新集群。

RayCluster 自身也可以创建 Serve Service：注解 `ray.io/enable-serve-service: "true"`。

### 6.4.3 多主机 headless Service

当 `numOfHosts > 1` 时，创建 `<cluster>-headless`：

- `clusterIP: None`；
- 选择 worker Pod；
- 用于同一 replica 内多个 host 的互相发现。

### 6.4.4 DNS 形式

```text
<service>.<namespace>.svc.<cluster-domain>
```

默认 `cluster.local`。可以通过 operator 环境变量 `CLUSTER_DOMAIN` 修改。跨命名空间访问要用完整 FQDN。

## 6.5 Init Container 与探针

### 6.5.1 Init Container

KubeRay 可以注入一个名为 `wait-gcs-ready` 的 init container：

- 环境变量 `ENABLE_INIT_CONTAINER_INJECTION=true`（默认）；
- 使用与 Ray 容器相同的镜像；
- 循环执行 `ray health-check --address <head-fqdn>:6379`，成功后退出；
- 前 120 秒每 5 秒重试，之后继续重试并打印排查提示；
- 避免 worker 在 head 还没起来时疯狂重启。

生产建议保留。若镜像中没有 `ray` CLI，需要自定义 init container。

### 6.5.2 Readiness/Liveness

KubeRay 会为 Ray 容器注入：

- readiness：`api/local_raylet_healthz` 或统一健康检查；
- liveness：Raylet/GCS 健康；
- head 的 timeout 更长（要查两个端点）；
- 可通过 `ENABLE_PROBES_INJECTION=false` 关闭。

自定义探针时不要只看进程存在：Raylet 存活但 GCS 不可达时，Pod 仍可能无法工作。

不同 KubeRay 版本注入的探针路径不同（常见 `/api/ray/healthz`、`/api/local_raylet_healthz` 或 `ray health-check`），**以 `kubectl get pod <pod> -o yaml` 里实际注入的定义为准**。排查时按这个顺序：

| 现象 | 先看 | 常见原因 |
|---|---|---|
| Pod Running 但长期不 Ready | `kubectl describe pod` 的 readiness 失败信息 | 探针路径/端口不对、Raylet 未起、GCS 不可达 |
| 探针频繁重启容器 | liveness 事件、`kubectl logs --previous` | timeout 太短、启动慢、head 过载 |
| 关掉 `ENABLE_PROBES_INJECTION` 后“正常” | 业务是否真的可用 | 探针失败可能是真实问题，不是噪声 |
| init container 卡住 | `kubectl logs <pod> -c wait-gcs-ready` | head DNS/6379/NetworkPolicy、镜像缺 `ray` CLI |

探针的 timeout/failureThreshold 要覆盖镜像启动、模型加载和 Ray 注册时间；head 通常比 worker 需要更长 timeout。

RayService 还有一个常见陷阱：某些 KubeRay 版本会给 worker 注入本地 Serve proxy（8000）的健康检查。如果 `proxy_location: HeadOnly`，worker 上没有 proxy，可能长期 `NotReady`，但 Raylet 本身是健康的（上游 issue #4994）。遇到“Ray 正常、所有 worker NotReady”时，先看实际注入的 readiness 路径/端口，再决定是否改成 raylet-only 探针或调整 `proxy_location`（第 11.2.3 节）。

### 6.5.3 原生 sidecar

Kubernetes 1.33+ 的原生 sidecar 写在 `initContainers` 里，并加 `restartPolicy: Always`：

```yaml
spec:
  initContainers:
    - name: log-shipper
      image: fluent/fluent-bit:3.1
      restartPolicy: Always
      readinessProbe:
        httpGet: {path: /api/v1/health, port: 2020}
      volumeMounts:
        - {name: ray-logs, mountPath: /var/log/ray}
  containers:
    - name: ray
      image: rayproject/ray:2.59.0
```

对 KubeRay 的三个影响：

- **“Ray 必须是 `containers[0]`”仍然成立**；sidecar 在 `initContainers`，不抢 Ray 的位置；
- sidecar 先于 Ray 启动，readiness 通过后 Ray 才启动；退出时 Ray 先退、sidecar 后退，适合日志/指标 flush；
- sidecar 的 requests 计入 Pod 总请求，会影响调度与状态统计；Ray 的逻辑容量仍只由 Ray 容器决定，不要把 sidecar 写得太贪心。

完整语义、Job 完成行为和实验见[第 58.2 节](kuberay教程-58-Kubernetes新原语与KubeRay-原生sidecar、调度门、拓扑感知与Pod级资源.md)；终止预算见[第 52.2.1 节](kuberay教程-52-生命周期、驱逐与优雅终止.md)。

## 6.6 共享内存与 ulimit

### 6.6.1 `/dev/shm`

KubeRay 默认挂载：

KubeRay 会挂载一个 `medium: Memory` 的 `emptyDir` 到 `/dev/shm`，其 `sizeLimit` **根据 Ray 容器的主容器 memory request/limit 推导**（不是固定 2Gi）。MiniKubeRay 为简化固定为 2Gi，仅用于模拟。

```yaml
volumes:
  - name: shared-mem
    emptyDir:
      medium: Memory
      sizeLimit: 2Gi   # MiniKubeRay 模拟值；真实值由 KubeRay 按 Ray 容器 memory 推导
volumeMounts:
  - name: shared-mem
    mountPath: /dev/shm
```

如果不挂载，容器默认 `/dev/shm` 只有 64MB，Ray 对象存储会频繁 spill 或 OOM。

如果设置了 `rayStartParams["plasma-directory"]`，KubeRay 不再自动挂载 `/dev/shm`，由用户负责目录容量。

### 6.6.2 ulimit

Ray 需要大量文件描述符。KubeRay 默认：

```bash
ulimit -n ${RAY_START_ULIMIT_OPEN_FILES:-65536}
```

可以通过环境变量 `RAY_START_ULIMIT_OPEN_FILES` 调整。Kubernetes 的 Pod/container API **没有**通用的 `ulimits` 字段；需要更高上限时，用容器 `command`/entrypoint 里的 `ulimit -n`、运行时配置（containerd/docker 的默认 limits）或节点级 `fs.file-max`，并确认 kubelet/运行时不会覆盖。

## 6.7 存储

### 6.7.1 用户数据

通过 Pod template 的 `volumes`/`volumeMounts` 挂载：

- PVC（模型、数据集）；
- hostPath（仅限受控节点）；
- CSI（S3/GCS/NFS）；
- emptyDir（临时缓存）。

### 6.7.2 GCS RocksDB PVC

`gcsFaultToleranceOptions.backend: rocksdb` 时：

- operator 创建 `<cluster>-gcs-pvc`（除非用户提供 `claimName`）；
- 挂载到 head 的 `/data/gcs`；
- `deletionPolicy: Retain` 可保留 PVC 供恢复；
- 该 PVC 不能被多个 head 同时写。

### 6.7.3 对象存储与数据本地性

Ray 的对象存储是**节点本地**的。数据在 worker A 上，worker B 需要时通过网络传输。因此：

- 尽量让数据与计算同节点/同可用区；
- 大对象会增加网络压力；
- `object-store-memory` 太小会导致 spill；
- 使用 Ray Data 的 locality-aware 调度。

## 6.8 GPU 与调度

### 6.8.1 GPU 资源

```yaml
resources:
  requests:
    nvidia.com/gpu: "1"
  limits:
    nvidia.com/gpu: "1"
```

需要：

- GPU 节点安装 device plugin；
- 节点有对应标签/taint；
- 镜像包含 CUDA 驱动兼容的运行时；
- `rayStartParams.num-gpus` 与 Pod GPU 数一致（或使用顶层 `resources`）。

### 6.8.2 调度约束

| 机制 | 用途 |
|---|---|
| `nodeSelector` | 选择 GPU 型号/可用区 |
| `affinity.nodeAffinity` | 复杂节点规则 |
| `podAffinity`/`podAntiAffinity` | 数据本地性、打散 |
| `topologySpreadConstraints` | 多可用区均衡 |
| `tolerations` | 容忍 GPU/专用节点 taint |
| `priorityClassName` | 抢占/优先级 |
| `schedulerName` | Volcano/YuniKorn/Kueue |

### 6.8.3 GPU Feature Discovery 标签

NVIDIA GPU Operator/GFD 会给节点打标签。常见：

| 标签 | 示例 | 用途 |
|---|---|---|
| `nvidia.com/gpu.product` | `NVIDIA-A100-SXM4-80GB` | 选型号（注意 `NVIDIA-` 前缀） |
| `nvidia.com/gpu.family` | `ampere`/`hopper` | 选代际 |
| `nvidia.com/gpu.memory` | `81920` | 选显存 |
| `nvidia.com/mig.config` | `all-1g.10gb` | MIG 配置 |
| `nvidia.com/gpu.count` | `8` | 节点 GPU 数 |

用 `kubectl get nodes --show-labels` 确认实际值；不同驱动/Operator 版本的标签值会变。不要凭记忆写 `A100-SXM4-80GB`，GFD 实际值通常带 `NVIDIA-` 前缀。

### 6.8.4 MIG、time-slicing 与拓扑

| 方式 | 隔离 | 显存 | 适用 |
|---|---|---|---|
| 整卡 | 最好 | 完整 | 大模型训练/推理 |
| MIG | 硬件分区 | 固定切片 | 小模型/多租户 |
| time-slicing | 时间片共享 | 共享 | 开发/低优先级 |
| vGPU | 厂商方案 | 分区 | 特定环境 |

拓扑：

- 同一 Pod 的多卡尽量同 NVLink 域；
- 多机训练需要 IB/RoCE；
- `topologySpreadConstraints` 控制跨区/跨机架分布；
- 大 GPU Pod 容易碎片化，预留整机/整组。

### 6.8.5 常见错配

- Pod 请求 1 GPU，但 `rayStartParams.num-gpus=0`：Ray 看不到 GPU。
- Pod 请求 8 CPU，Ray `num-cpus=4`：Ray 调度不足。
- Pod **不请求** GPU（或绕过 device plugin）时，容器可能看到节点上的全部 GPU；请求 `nvidia.com/gpu: 1` 时，运行时会把可见设备限制到分配的那一张（通常通过 `NVIDIA_VISIBLE_DEVICES`/`CUDA_VISIBLE_DEVICES` 注入）。
- 多租户共用 GPU 节点，没有 taint/配额：互相抢占。

## 6.9 安全上下文

生产建议：

```yaml
securityContext:
  runAsNonRoot: true
  runAsUser: 1000
  fsGroup: 1000
  seccompProfile:
    type: RuntimeDefault
containers:
  - name: ray
    securityContext:
      allowPrivilegeEscalation: false
      capabilities:
        drop: [ALL]
      readOnlyRootFilesystem: false  # Ray 需要写 /tmp/ray
```

Ray 需要写 `/tmp/ray`、`/dev/shm`、用户工作目录；不要把整个根文件系统设为只读而不挂载可写卷。

## 6.10 Ingress 与外部访问

`headGroupSpec.enableIngress: true` 会让 operator 创建 Ingress，把 Dashboard 暴露到集群外。也可以自己写 Ingress/Gateway 指向 head Service。

安全建议：

- 不要直接暴露 GCS 6379 到公网；
- Dashboard/Client 端口加认证（token/mTLS）；
- 外部入口用 Ingress/Gateway 做 TLS、限流、IP allowlist；
- Ray Serve 用单独的 Serve Service，不要复用 Dashboard。

## 6.11 简版实现中的 Pod/Service 生成

`minikuberay/controllers/builders.py` 实现了：

- `build_head_pod`：ray 容器、autoscaler sidecar、collector、`/dev/shm`、GCS PVC、auth env；
- `build_worker_pod`：地址、资源、多主机标签；
- `build_head_service`：headless、端口、自定义 Service；
- `build_serve_service`：`ray.io/serve=true` selector；
- `build_headless_service`：多主机；
- `build_network_policies`、`build_gcs_pvc`、`build_auth_secret`、`build_autoscaler_rbac`。

它不实现真实 init container/探针，但保留了端口、标签、env、volume 和 selector 这些对控制平面最重要的部分。

## 6.12 深入：GPU、MIG 与 DRA

本章的 GPU 部分只覆盖了 KubeRay 字段和调度约束。生产 GPU 集群还要理解：

- device plugin 如何注册 `nvidia.com/gpu`；
- GFD 标签如何选型号；
- MIG/time-slicing/MPS 的隔离与代价；
- DRA（`ResourceClaim`/`DeviceClass`）如何表达结构化设备请求；
- NUMA/NVLink/IB 拓扑如何影响多机训练；
- GPU 碎片与 gang scheduling 的关系。

这些内容见[第 36 章](kuberay教程-36-GPU与异构资源调度-device-plugin、MIG、DRA与拓扑.md)。一句话原则：**Kubernetes 资源决定 Pod 能否调度，Ray 资源决定 task/actor 能否调度，两者必须显式对齐。**

## 6.13 Pod 启动时序与探针调优

### 6.13.1 从 Pending 到 Ray Ready

| 阶段 | 发生什么 | 典型耗时 | 失败信号 |
|---|---|---|---|
| 调度 | kube-scheduler 绑定节点 | <1s–分钟 | Pending + Unschedulable |
| 拉镜像 | kubelet 拉取镜像层 | 秒–分钟 | ImagePullBackOff |
| 创建沙箱 | CNI 分配 IP/路由 | 秒 | FailedCreatePodSandBox |
| init container | `wait-gcs-ready` 等 head 6379 | 秒–分钟 | init 卡住/重试 |
| 主容器启动 | `ray start --block` | 秒 | CrashLoopBackOff |
| Raylet 注册 | 连 GCS、上报资源 | 秒 | Ray 日志注册错误 |
| 探针通过 | readiness | 秒 | Pod Ready=False |

时间预算要覆盖完整链路。扩容 SLO 如果要求 30 秒，必须预热镜像/节点，否则几乎不可能。

### 6.13.2 三类探针

> 终止阶段的 preStop/SIGTERM/grace/PDB 不在本节展开，见[第 52 章](kuberay教程-52-生命周期、驱逐与优雅终止.md)；节点容量不足导致的 Pending 见[第 51 章](kuberay教程-51-Kubernetes弹性伸缩栈-从Pending-Pod到节点池.md)。

| 探针 | 问题 | 建议 |
|---|---|---|
| startupProbe | 容器是否已经启动完成？ | 给慢启动的 Ray/模型加载留足 `failureThreshold × periodSeconds` |
| readinessProbe | 是否可以把流量/worker 接进来？ | 检查 Raylet/GCS/Dashboard，而不只是进程 |
| livenessProbe | 是否已经死锁/失联？ | 不要过严；误杀 head 会导致整个集群重建 |

常见错误：

- liveness 用 `/healthz` 但 Ray 尚未初始化，导致反复重启；
- readiness 只检查端口，Raylet 活着但 GCS 不可达；
- `initialDelaySeconds` 太短，GPU 驱动/模型加载还没完成；
- 探针超时太短，GC/大对象传输时误判。

### 6.13.3 init container 的价值

注入机制见 6.5.1。补充两点排障价值：

- 把“等待 head”集中在 init 阶段，worker 不会进入 CrashLoop，`kubectl logs -c wait-gcs-ready` 是明确卡点；
- 如果镜像里没有 `ray` CLI，需要自定义 init container；不要直接关掉注入而不提供替代方案。

## 6.14 容量推导：`/dev/shm`、内存与对象存储

### 6.14.1 KubeRay 的 `/dev/shm` 规则

KubeRay 会注入一个 `medium: Memory` 的 emptyDir 到 `/dev/shm`，大小**从 Ray 容器的 memory resources 推导**：优先 `limits.memory`，没有 limit 时回退到 `requests.memory`；v1.7 的实现通常把 `sizeLimit` 设为该值（约 1:1），而不是固定 30%–50%。以实际 Pod 为准。

这意味着：

```text
Pod memory limit 4Gi
  -> /dev/shm sizeLimit ≈ 4Gi（上限，不是预留）
  -> tmpfs 用量计入 Pod memory cgroup，超限会 OOM
```

Ray 自己的 object store 默认保留约 30% 的可用内存；这个 30% 是 Ray 的预算，不是 KubeRay 给 `/dev/shm` 的比例。两个数字不要混用。

最可靠的做法是直接看生成的 Pod：

```bash
kubectl get pod <pod> -o jsonpath='{.spec.volumes[?(@.name=="shared-mem")].emptyDir.sizeLimit}'
```

`/dev/shm` 不是“额外内存”：它会计入 Pod 的内存限制。设置过大可能导致：

- 容器内存超限被 OOMKilled；
- 节点内存压力；
- 其他进程被挤占。

### 6.14.2 Ray 的对象存储上限

Ray 默认会预留“启动时可用内存的约 30%”作为对象存储上限，并受 `/dev/shm` 大小和 `object-store-memory` 参数约束。推荐显式设置：

```yaml
rayStartParams:
  memory: "3500000000"          # Ray 可见内存，留出 headroom
  object-store-memory: "1000000000"
```

原则：

```text
memory + object-store-memory + Raylet/Dashboard/worker 开销
  < 容器 memory limit
```

如果 `memory + object-store-memory` 接近 limit，OOM 只是时间问题。

### 6.14.3 一个容量算例

假设 `/dev/shm` 与 memory limit 1:1：

```text
节点：8 CPU / 64 GiB / 1 GPU
Pod：8 CPU / 48 GiB / 1 GPU
  /dev/shm sizeLimit = 48 GiB（上限，按实际 Pod 为准）
  ray memory = 36 GiB
  object-store-memory = 8 GiB
  headroom = 48 - (36 + 8) = 4 GiB（给 Raylet/Python/OS cache）
```

注意 `/dev/shm` 的 48 GiB 是 tmpfs 上限，不是预分配；对象多时它会和 Ray 堆内存竞争同一个 cgroup。真正要保证的是 `object-store-memory + 堆 + Raylet 开销 < memory limit`。

如果实际对象很多：

- 增大 `/dev/shm`/limit；
- 减少 batch/对象大小；
- 用流式处理/Ray Data；
- 监控 `ray_object_store_memory`（按 `Location` 区分 spilled/共享内存）和 spill 速率；对象存储的“上限”没有稳定的 `ray_object_store_memory_limit` 指标，用 `ray_resources{Name="object_store_memory"}` 或 `ray status` 对照。

> 第 37 章案例 1 就是“`/dev/shm` 太小 -> Plasma 满 -> 重试放大”的完整时间线。成本/容量模型见[第 15 章](kuberay教程-15-性能、容量与成本.md)；autoscaler 的容量推导见[第 34 章](kuberay教程-34-Autoscaler-v2内部机制与容量模型.md)。

## 6.15 小结

- 一个 Ray 节点是一个 Pod；head 额外有 GCS/Dashboard/Client。
- head Service 默认 headless，worker 通过 6379 加入。
- `/dev/shm`、ulimit、资源对齐是性能与稳定性的三大基础。
- GPU 调度依赖 nodeSelector/toleration/device plugin，且 Ray 资源要与 Pod 资源一致。
- Service selector 是流量切换的关键：head Service 选 head，Serve Service 选 `ray.io/serve=true`。
- 多主机用 headless Service + replica/host 标签。

下一章进入控制器的调和循环，看看这些对象是如何被创建、更新、删除的。

→ [第 7 章 控制器调和循环](kuberay教程-07-控制器调和循环-从CR到Pod.md)
