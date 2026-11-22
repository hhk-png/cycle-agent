# 第 41 章 Kubernetes 作为通用计算平台：虚拟化、Serverless、Wasm 与边缘

> Kubernetes 最初是容器编排器。今天它更像一个“分布式控制面”：用 API、调谐循环和调度器管理容器、虚拟机、函数、Wasm 模块，甚至边缘设备。本章讨论这些扩展形态的架构、适用边界和代价，避免把“能跑在 Kubernetes 上”误当成“应该跑在 Kubernetes 上”。

## 41.1 从容器编排到通用控制面

Kubernetes 的可复用部分不只是容器运行时，而是：

- **声明式 API**：用户描述期望状态。
- **调谐循环**：控制器不断逼近期望。
- **调度**：把工作放到合适的节点。
- **身份/RBAC/准入**：统一的访问控制。
- **网络/存储抽象**：Service、PV/PVC、CNI/CSI。
- **可观测/审计**：事件、指标、日志。

只要一个系统能表达成“期望状态 + 控制器 + 调度 + 生命周期”，就可以套进这个模型。于是出现了：

```text
容器 Pod
├── 虚拟机（KubeVirt）
├── 函数/请求驱动（Knative）
├── Wasm 模块（runwasi/SpinKube）
├── 边缘设备（KubeEdge/OpenYurt）
└── 专用硬件（GPU/DPU/FPGA）
```

但每增加一种形态，都会引入新的运行时、网络、存储、安全和运维模型。统一控制面的收益与复杂度要一起算。

## 41.2 KubeVirt：在 Kubernetes 里跑虚拟机

KubeVirt 让 VM 成为 Kubernetes 原生对象：

- `VirtualMachine`（VM）：长期存在的虚拟机定义，类似 Deployment。
- `VirtualMachineInstance`（VMI）：一次运行实例，类似 Pod。
- `VirtualMachineInstanceMigration`：热迁移。
- `VirtualMachineSnapshot`：快照。

架构：

```text
VirtualMachine (CRD)
  └── VirtualMachineInstance (CRD)
        └── virt-launcher Pod
              ├── libvirt
              ├── QEMU
              └── VM 进程
```

`virt-launcher` 是一个 Pod，里面运行 libvirt/QEMU。VM 的磁盘通常是 PVC，网络通过 CNI 接入：

```yaml
apiVersion: kubevirt.io/v1
kind: VirtualMachine
metadata:
  name: legacy-app
spec:
  running: true
  template:
    metadata:
      labels:
        kubevirt.io/domain: legacy-app
    spec:
      domain:
        cpu:
          cores: 4
        memory:
          guest: 8Gi
        devices:
          disks:
            - name: root
              disk:
                bus: virtio
            - name: cloudinit
              disk:
                bus: virtio
      volumes:
        - name: root
          persistentVolumeClaim:
            claimName: legacy-root
        - name: cloudinit
          cloudInitNoCloud:
            userData: |
              #cloud-config
              users:
                - name: app
                  sudo: ALL=(ALL) NOPASSWD:ALL
      networks:
        - name: default
          pod: {}
```

网络模式：

- **masquerade**：VM 通过 NAT 使用 Pod IP，最常用。
- **bridge**：VM 直接桥接到节点/二层网络，需要 CNI 支持。
- **SR-IOV**：直通网卡，高性能/低延迟。
- **passt**：用户态网络，较新方案。

存储：

- `containerDisk`：镜像内嵌，适合无状态/临时。
- PVC：持久盘，支持 live migration（共享存储）。
- `DataVolume`：从镜像/URL 导入磁盘。

热迁移要求：

- 共享存储（RWX 或支持 live migration 的块存储）。
- 目标节点资源足够。
- 网络可迁移（masquerade 较容易）。
- 集群版本/feature gate 支持。

适用场景：

- 遗留应用无法容器化。
- 需要强隔离的多租户。
- Windows 工作负载。
- 需要 VM 形态的合规/授权软件。

代价：

- 启动慢（秒级到分钟级）。
- 资源开销大。
- 运维需要虚拟化知识。
- GPU 直通/热迁移/快照有硬件与存储限制。

## 41.3 Knative：请求驱动的 Serverless

Knative Serving 提供 request-driven 的工作负载：

```text
Service
├── Configuration
│   └── Revision（不可变版本）
└── Route（流量分配）
```

一个 Service：

```yaml
apiVersion: serving.knative.dev/v1
kind: Service
metadata:
  name: payments-api
spec:
  template:
    metadata:
      annotations:
        autoscaling.knative.dev/minScale: "1"
        autoscaling.knative.dev/maxScale: "50"
        autoscaling.knative.dev/target: "80"
    spec:
      containers:
        - image: example/payments@sha256:...
          env:
            - name: APP_ENV
              value: prod
```

数据面：

```text
请求 → Activator（冷启动时缓冲）→ Queue-Proxy（每个 Revision 的 sidecar）
     → 用户容器
```

- **Queue-Proxy**：并发控制、指标、超时、请求缓冲。
- **Activator**：scale-to-zero 时接收请求，等副本起来再转发。
- **Autoscaler（KPA）**：按并发/请求速率扩缩容。
- **Route**：按百分比/标签切流量，支持 canary。

冷启动是核心问题：

- 镜像拉取、进程启动、依赖初始化、模型加载都会增加延迟。
- 解决：最小副本、镜像预热、快启动运行时、懒加载、快照恢复。
- scale-to-zero 适合低频/突发；高频服务保留最小副本更稳。

Knative Eventing 提供事件驱动：

- `Broker`/`Trigger` 路由 CloudEvents。
- `Source` 接入 Kafka、GitHub、Cron 等。
- 适合事件驱动、异步任务、轻量函数。

Knative 与 HPA/KEDA 的区别：

- HPA 按资源指标扩缩容，通常不 scale-to-zero。
- KEDA 按事件源扩缩容，可 scale-to-zero，但仍需工作负载控制器。
- Knative 是完整的请求驱动 Serving 层，包含路由、Revision、activator、queue-proxy。

## 41.4 Wasm：更轻的沙箱

WebAssembly（Wasm）在 Kubernetes 中有两种角色：

1. **工作负载**：用 Wasm 模块替代容器。
2. **插件**：Envoy/Istio、Cilium、OPA 用 Wasm 扩展，不重启进程。

### Wasm 工作负载

运行时：

- **runwasi**：containerd 的 Wasm shim，让 containerd 像跑容器一样跑 Wasm。
- **SpinKube**：基于 Spin 的 Kubernetes 集成，提供 `SpinApp` CRD。
- **wasmCloud**：actor 模型，强调分布式能力。
- **WasmEdge**：边缘/轻量运行时。

优势：

- 启动毫秒级，密度高。
- 体积小，分发快。
- 默认沙箱，能力受限。
- 多语言（Rust/C/Go/JS/Python 等）。

限制：

- WASI 能力有限：网络、文件系统、线程、GPU 支持不完整。
- 生态/工具链不成熟，调试/可观测性弱。
- 不适合长运行、重计算、需要内核特性的工作负载。
- 与容器镜像/OCI 生态的集成仍在演进。

适用：

- 边缘/Serverless 函数。
- 插件/过滤器。
- 轻量、短生命周期、强隔离任务。
- 多租户的不可信代码。

### Wasm 插件

Wasm 插件在数据面很成熟：

- Envoy/Istio：Wasm filter 做认证、限流、转换。
- Cilium：Wasm 扩展网络策略/可观测。
- OPA：Wasm 编译策略，嵌入应用做本地决策。

特点是“安全、可热加载、语言无关”，但性能/能力受沙箱限制。

## 41.5 边缘计算

边缘场景与数据中心相反：

- 节点资源小（ARM、低内存）。
- 网络不稳定/断网。
- 站点多、无人值守。
- 物理安全弱。
- 需要本地自治。

### K3s

- 单二进制，轻量。
- 默认 SQLite，可选 etcd。
- 内置 Traefik、local-path、metrics-server 等。
- 适合边缘/CI/开发。

### KubeEdge

- `cloudcore` 在云端，`edgecore` 在边缘。
- 边缘自治：断网时本地继续运行。
- Device twin/Device CRD 管理设备。
- 边缘应用/配置同步。

### OpenYurt

- `YurtHub` 缓存 API 访问，断网自治。
- 节点池/边缘单元管理。
- 与原生 Kubernetes 兼容性较好。

### Akri

- 发现边缘设备（OPC UA、ONVIF、USB 等），把设备暴露为 Kubernetes 资源。

边缘的关键问题：

1. **镜像分发**：站点多、带宽小；需要 P2P/增量/预置镜像。
2. **断网自治**：控制面不可达时，边缘节点要能继续运行并本地决策。
3. **安全更新**：远程升级、回滚、签名验证。
4. **可观测**：日志/指标要本地缓冲，网络恢复后上传。
5. **设备管理**：协议多样，需要设备插件/CRD。
6. **资源约束**：控制面组件要轻量，不能按数据中心规格部署。

## 41.6 Serverless Kubernetes 与虚拟节点

云厂商提供“无需管理节点”的 Kubernetes：

- **AWS Fargate**：Pod 运行在托管微 VM，按 Pod 计费。
- **GKE Autopilot**：托管节点与配置，按 Pod 资源计费。
- **Azure Container Apps / ACI**：容器/无服务器。
- **阿里云 ECI**：弹性容器实例。

特点：

- 不需要节点运维、容量规划。
- 冷启动/启动延迟、daemonSet/特权容器限制。
- 单价通常高于自管节点，但省人力。
- 网络/存储/GPU 支持因厂商而异。

**Karpenter** 是另一种思路：不管理节点组，而是根据 Pod 需求直接创建最合适的实例，缩容时合并/回收。它仍然是“节点”模型，但调度与供给更紧密。

选择：

- 低频/突发/小团队：Serverless 容器。
- 稳定高负载/需要 daemonSet/GPU：自管节点 + Karpenter/Cluster Autoscaler。
- 混合：核心自管，突发溢出到虚拟节点。

## 41.7 选择矩阵

| 形态 | 启动 | 隔离 | 密度 | 适用 |
|---|---|---|---|---|
| 容器 | 秒级 | 中 | 高 | 大多数服务 |
| VM（KubeVirt） | 秒到分钟 | 强 | 低 | 遗留/强隔离/Windows |
| Wasm | 毫秒 | 强（能力受限） | 很高 | 函数/插件/边缘 |
| Knative | 毫秒到秒（冷启动） | 中 | 高 | 请求驱动/突发 |
| 边缘 K3s | 秒级 | 中 | 中 | 站点/设备 |
| Serverless 容器 | 秒级 | 强（厂商） | 高 | 免运维/突发 |

决策问题：

1. 工作负载需要内核/GPU/特殊设备吗？
2. 需要强隔离还是多租户共享？
3. 启动延迟要求是多少？
4. 负载是常驻还是请求驱动？
5. 团队有能力运维哪种运行时？
6. 成本模型是节点、Pod 还是请求？

## 41.8 统一控制面的代价

把 VM、函数、Wasm、边缘都塞进 Kubernetes 的收益：

- 统一 API/RBAC/策略/可观测。
- 复用调度、网络、存储、GitOps。
- 平台团队只维护一套控制面。

代价：

- **API 蔓延**：CRD 越多，升级/兼容/文档越难。
- **运行时碎片化**：每种运行时都有自己的调试/监控/安全模型。
- **调度复杂**：不同资源维度、拓扑、生命周期。
- **版本偏斜**：Kubernetes、KubeVirt、Knative、CNI、CSI 的兼容矩阵。
- **安全边界**：共享内核的隔离弱于独立集群/VM。
- **组织复杂度**：平台团队要同时懂容器、虚拟化、Serverless、边缘。

判断标准：**统一控制面是否降低了总复杂度**。如果只是为了“都在 Kubernetes 上”而增加三层抽象，可能不如分开用成熟方案。

## 41.9 MiniK8s 的边界

MiniK8s 只实现容器 Pod 模型，不实现：

- KubeVirt/VM。
- Knative/请求驱动。
- Wasm 运行时。
- 边缘自治。
- 虚拟节点/Serverless 供给。

但它的组件边界（API/调度/控制器/kubelet）可以作为理解这些系统的骨架：KubeVirt 的 virt-launcher 是一个 Pod，Knative 的 Revision 是 Pod 模板，Wasm 运行时是 CRI 之外的 shim。理解 Pod 生命周期和控制器模式，迁移到这些形态会容易得多。

## 41.10 生产清单

- [ ] 选择 VM/容器/Wasm/函数有明确依据（隔离、启动、生态、成本）。
- [ ] KubeVirt 的存储支持热迁移/快照；网络模式与 CNI 兼容。
- [ ] Knative 的冷启动、最小副本、并发目标经过压测。
- [ ] Wasm 工作负载的能力边界（网络/文件/GPU）已验证。
- [ ] 边缘有断网自治、镜像分发、远程升级、安全启动方案。
- [ ] Serverless 容器的限制（daemonSet/特权/GPU/网络）已确认。
- [ ] 统一控制面的版本兼容矩阵与升级顺序明确。
- [ ] 多运行时环境的可观测/安全/成本口径统一。
- [ ] 不为“技术栈统一”牺牲可靠性或增加不可控复杂度。

## 本章小结

- Kubernetes 的可复用核心是声明式 API、调谐、调度和统一治理，不是容器本身。
- KubeVirt 把 VM 变成 Pod 背后的 CRD；Knative 提供请求驱动与 scale-to-zero；Wasm 提供更轻的沙箱；边缘方案强调自治。
- 每种形态都有运行时、网络、存储、安全和运维代价。
- 统一控制面只有在降低总复杂度时才成立；否则分开用成熟方案更好。
- MiniK8s 只覆盖容器模型，但可作为理解这些系统的骨架。

## 延伸阅读

- [第 6 章 Pod 与生命周期](06-pod-lifecycle.md)
- [第 13 章 Service、Endpoints 与 DNS](13-service-endpoints-dns.md)
- [第 16 章 服务网格与零信任](16-service-mesh-zero-trust.md)
- [第 20 章 自动伸缩](20-autoscaling.md)
- [第 42 章 云原生未来方向](42-future.md)
- [附录 L 云原生生态地图](../appendices/L-ecosystem-map.md)：虚拟化、Serverless、边缘与运行时选型。
