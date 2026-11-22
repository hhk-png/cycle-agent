# 第 2 章 架构总览：控制面、节点面与 API 驱动

## 2.1 一张图看懂 Kubernetes

```text
┌──────────────────────────── Control Plane ────────────────────────────┐
│                                                                        │
│   kubectl / CI / Operator                                             │
│          │ HTTPS + JSON/YAML                                          │
│          ▼                                                            │
│   ┌─────────────┐     watch/list      ┌──────────────────────────┐    │
│   │ API Server  │◀───────────────────▶│ etcd (Raft, MVCC)        │    │
│   │ authn/authz │                     └──────────────────────────┘    │
│   │ admission   │                                                     │
│   │ REST/watch  │◀──── watch ────┐                                   │
│   └──────┬──────┘                 │                                   │
│          │                        │                                   │
│   ┌──────▼──────┐          ┌──────▼──────────┐                       │
│   │ Scheduler   │          │ Controller      │                       │
│   │ filter/score│          │ Manager         │                       │
│   └─────────────┘          │ Deploy/RS/Job…  │                       │
│                            └─────────────────┘                       │
└──────────────────────────────────────────────────────────────────────┘
                                  │ watch Pods bound to node
       ┌──────────────────────────┼──────────────────────────┐
       ▼                          ▼                          ▼
┌─────────────┐            ┌─────────────┐            ┌─────────────┐
│ Node A      │            │ Node B      │            │ Node C      │
│ kubelet     │            │ kubelet     │            │ kubelet     │
│ CRI runtime │            │ CRI runtime │            │ CRI runtime │
│ kube-proxy  │            │ kube-proxy  │            │ kube-proxy  │
│ CNI plugin  │            │ CNI plugin  │            │ CNI plugin  │
└─────────────┘            └─────────────┘            └─────────────┘
```

这不是一张“组件清单”，而是一张“数据流图”。所有组件都在读写 API Server，只有 API Server 直接读写 etcd。

## 2.2 控制面组件

### API Server

API Server 是唯一暴露 Kubernetes API 的组件，职责包括：

- **认证**：TLS 客户端证书、Bearer Token、OIDC、ServiceAccount JWT、Webhook。
- **授权**：RBAC、Node、Webhook、ABAC（历史遗留）。
- **准入**：Mutating/Validating Admission，内置插件与外部 webhook。
- **REST**：资源 CRUD、子资源（status/scale/logs/exec/eviction）、watch、discovery。
- **一致性**：`resourceVersion` 乐观并发、`metadata.uid`、finalizer、删除语义。
- **缓存与限流**：watch cache、API Priority and Fairness（APF）、请求超时。

真实 API Server 是无状态的（状态在 etcd），可以水平扩展；但每个实例都维持 watch cache，因此扩容会带来内存和 etcd 连接成本。

### etcd

etcd 是强一致、基于 Raft 的键值存储。Kubernetes 把对象序列化后存入 etcd，并利用：

- **MVCC**：每个事务获得全局 revision，支持历史版本读取。
- **watch**：API Server 通过 etcd watch 感知变化，再分发给客户端。
- **compaction**：清理旧 revision；不做 compaction 会撑爆磁盘。
- **defrag**：回收碎片空间。
- **quorum**：3 节点容忍 1 台故障，5 节点容忍 2 台；偶数节点不增加容错。

etcd 的延迟和容量直接决定控制面体验。大规模集群的首要瓶颈通常不是调度器，而是 etcd 的写放大、对象大小和 watch 事件量。

### Scheduler

调度器 watch 未绑定的 Pod，执行：

1. **过滤（Filter）**：节点是否 Ready、资源是否足够、污点/容忍、亲和性、端口冲突、卷拓扑。
2. **打分（Score）**：资源均衡、亲和性偏好、拓扑分布、镜像本地性。
3. **绑定（Bind）**：写 `spec.nodeName`（真实实现通过 Binding 子资源）。

调度器不启动容器，也不修改 Pod 的其他字段。它只回答“放哪台机器”。

### Controller Manager

Controller Manager 是多个控制器的宿主进程，包括 Deployment、ReplicaSet、StatefulSet、DaemonSet、Job、CronJob、Node、Namespace、ServiceAccount、EndpointSlice、PV、HPA、PDB、GC 等。

每个控制器是一个“调谐循环”：

```text
observe desired (spec) + actual (status / cluster state)
        ↓
compute difference
        ↓
issue API writes
        ↓
repeat
```

控制器可以多副本部署，但同一时刻只有一个 leader 执行（Lease 选举），否则会重复创建资源。

### Cloud Controller Manager

在云上，节点生命周期、负载均衡器、路由表、云盘等逻辑通常由 Cloud Controller Manager（CCM）承担。它把云厂商 API 与 Kubernetes API 解耦。不同发行版可能把 CCM 合并进控制面或由厂商托管。

## 2.3 节点面组件

### kubelet

kubelet 是节点上的“小控制面”：

- 向 API Server 注册 Node、上报心跳和资源。
- watch 绑定到本节点的 Pod。
- 通过 CRI 调用容器运行时（containerd、CRI-O）。
- 挂载卷（CSI）、配置网络（CNI）、拉镜像。
- 执行 startup/readiness/liveness 探针。
- 管理容器重启、Pod 状态、日志。
- 驱逐（eviction）资源压力下的 Pod。

kubelet 不是“被 API Server 远程执行的 agent”。它主动 watch，拥有本地状态和重试逻辑。API Server 挂了，已运行的容器继续跑，但新的调度和状态更新会暂停。

### 容器运行时

通过 CRI（Container Runtime Interface）接入：

- **containerd**：最常见。
- **CRI-O**：偏 OpenShift/轻量。
- **Docker Engine**：通过 cri-dockerd 适配，已不是默认路径。
- **沙箱运行时**：gVisor、Kata Containers、Firecracker 等，通过 RuntimeClass 选择。

运行时负责镜像、容器生命周期、日志、exec、端口转发。Kubernetes 不直接调 OCI runtime（runc/crun），而是通过 CRI shim。

### kube-proxy 与数据面

kube-proxy 实现 Service 的虚拟 IP 转发，常见模式：

- **iptables**：规则随 Service/Endpoint 数量增长，大规模下有性能与更新延迟问题。
- **IPVS**：内核四层负载均衡，规则更高效，但连接跟踪和调度算法有细节。**1.35 起 IPVS 模式被弃用**，新集群不要再用；存量集群按 nftables/eBPF 方向规划迁移。
- **nftables**：新一代规则引擎，1.33 起 GA，作为 iptables/IPVS 的继任方向；多数发行版仍以 iptables 为默认，切换前要做 Service/DNS/NetworkPolicy 回归。
- **eBPF**：Cilium 等直接在内核完成 Service、NetworkPolicy、可观测性，绕过 kube-proxy。

网络插件（CNI）负责 Pod IP 分配、Pod 间连通、网络策略执行。Service 是虚拟概念，真正转发由 kube-proxy/eBPF/云 LB 完成。

### 附加组件

- **CoreDNS**：集群 DNS。
- **metrics-server**：资源指标 API。
- **Ingress Controller / Gateway**：南北向流量。
- **CNI / CSI**：网络与存储。
- **Cert Manager、External DNS、Policy Engine** 等平台组件。

这些不是 Kubernetes 核心二进制，但生产集群几乎必备。

## 2.4 一次请求的完整路径

以 `kubectl apply -f deployment.yaml` 为例：

```text
kubectl
  │ 1. discovery: GET /api, /apis, /apis/apps/v1
  │ 2. GET existing object (apply 需要 resourceVersion)
  │ 3. PATCH/PUT/POST
  ▼
API Server
  │ TLS → authentication → authorization → admission
  │ 4. 写入 etcd，分配 resourceVersion，产生 watch 事件
  ▼
DeploymentController (watch)
  │ 5. 计算 pod-template-hash，创建/更新 ReplicaSet
  ▼
ReplicaSetController (watch)
  │ 6. 创建 Pod（ownerReference 指向 RS）
  ▼
Scheduler (watch)
  │ 7. 过滤/打分，写 spec.nodeName
  ▼
Kubelet (watch)
  │ 8. 拉镜像、创建 sandbox、启动容器、执行探针
  │ 9. 写 status.phase / conditions / containerStatuses
  ▼
EndpointsController / EndpointSliceController
  │ 10. Ready Pod IP 写入 Endpoints/EndpointSlice
  ▼
kube-proxy / eBPF
  │ 11. 更新转发规则，Service 流量可达
```

每一步都可能失败，而且失败是正常的：

- 准入拒绝：返回 4xx，对象不落库。
- 调度失败：Pod 保持 Pending，产生 `FailedScheduling` 事件。
- 镜像拉取失败：容器 `Waiting`，Pod 仍可能 Running/Pending。
- 探针失败：Pod 不 Ready，Endpoints 不收录。
- 节点失联：Node NotReady，Pod 可能被驱逐或重新调度。

Kubernetes 的设计假设“组件会挂、网络会抖、磁盘会满”，所以每个环节都要能重试和观测。

## 2.5 watch、informer 与缓存

如果每个控制器都轮询 API Server，规模一大就会打爆控制面。Kubernetes 使用：

1. **List**：获取全量对象和当前 `resourceVersion`。
2. **Watch**：从该版本开始接收增量事件（ADDED/MODIFIED/DELETED/BOOKMARK）。
3. **本地缓存（Store）**：控制器在内存中维护对象索引。
4. **Informer**：封装 List/Watch、重连、DeltaFIFO、事件处理器。
5. **Workqueue**：把对象 key 放入队列，去重、限速、重试。

```text
API Server ──list/watch──▶ Reflector ──▶ DeltaFIFO ──▶ Indexer (cache)
                                              │
                                              ▼
                                        EventHandler ──▶ Workqueue ──▶ Reconcile
```

关键点：

- 控制器读缓存，不直接读 API Server，降低延迟和负载。
- 写操作仍然走 API Server，并处理冲突重试。
- watch 断线后从最后 `resourceVersion` 续接；如果版本已被 compaction，则重新 List。
- `resourceVersion` 是字符串，不应假设是连续整数；MiniK8s 用整数模拟。

MiniK8s 简化了 informer：控制器每轮直接 list store，再对比状态。这在教学规模下足够，但第 36 章会说明它与真实 informer 的差距。

## 2.6 高可用控制面

生产控制面通常至少 3 个 API Server、3 个 etcd 节点：

```text
             ┌────────────┐
   LB/VIP ──▶│ API Server │──┐
             └────────────┘  │
             ┌────────────┐  ├──▶ etcd cluster (3/5)
             │ API Server │──┤
             └────────────┘  │
             ┌────────────┐  │
             │ API Server │──┘
             └────────────┘
```

- API Server 无状态，前面放 L4/L7 负载均衡。
- etcd 用奇数节点保证 quorum；跨可用区部署要权衡延迟与容灾。
- Controller Manager、Scheduler 多副本，通过 Lease 选主，只有 leader 工作。
- kubelet 和 kube-proxy 在每个节点本地运行，不依赖控制面实时可用。

控制面故障时：

- 已运行的 Pod 继续运行。
- 不能创建/修改对象，不能调度新 Pod。
- kubelet 仍会重启本地容器，但无法上报或获取新 Pod。
- 网络数据面（已下发的规则）通常继续工作。

这就是为什么“控制面高可用”与“数据面高可用”要分开设计。

## 2.7 MiniK8s 的组件映射

| 真实组件 | MiniK8s 实现 | 说明 |
|---|---|---|
| kube-apiserver | `mini_k8s/api/server.py` | HTTP、discovery、watch、准入、认证 |
| etcd | `mini_k8s/store/store.py` | 内存、resourceVersion、watch 历史 |
| scheduler | `mini_k8s/scheduler/scheduler.py` | filter/score/preemption |
| controller-manager | `mini_k8s/controllers/` | 多个独立线程 |
| kubelet | `mini_k8s/kubelet/kubelet.py` | 模拟运行时、探针、日志 |
| kube-proxy | `mini_k8s/network/proxy.py` | 进程内 Service 转发模拟 |
| CoreDNS | `mini_k8s/network/dns.py` | 名称解析 |
| CNI | `mini_k8s/network/policy.py` | 策略判定，不操作内核 |
| CSI | `mini_k8s/controllers/storage.py` | hostPath 模拟动态供给 |
| controller leader election | `mini_k8s/coordination/leader.py` | Lease CAS |

启动一个集群：

```bash
python -m mini_k8s --port 8080 --nodes 2
```

观察组件日志（开发模式）：

```bash
python -m mini_k8s --port 8080 --verbose
```

## 2.8 常见误解

**“API Server 会调度 Pod。”** 不会。它只存对象；调度器写绑定。

**“kubelet 由 API Server 远程调用。”** kubelet 主动 watch，API Server 不知道 kubelet 的内部状态。

**“Service 是一个进程。”** Service 是虚拟 IP + 转发规则，没有“Service 容器”。

**“etcd 挂了容器会停。”** 已运行容器通常继续，控制面操作受阻。

**“控制器越多副本越好。”** 同一控制器多副本需要选主，否则会重复创建资源；扩容控制器通常无收益。

## 2.9 组件端口、证书与启动参数

排障时只知道“组件在哪”不够，还要知道它们监听什么端口、用什么证书、启动参数影响什么行为。以下是生产集群最常见的端口与证书清单。

### 端口

| 组件 | 端口 | 用途 |
|---|---|---|
| kube-apiserver | 6443 | HTTPS API（kubectl/控制器/kubelet） |
| etcd | 2379 | 客户端 API |
| etcd | 2380 | peer 通信（Raft） |
| kubelet | 10250 | kubelet API（logs/exec/port-forward 经此） |
| kubelet | 10248 | 本地 healthz |
| kube-scheduler | 10259 | HTTPS metrics/health（旧版 10251） |
| kube-controller-manager | 10257 | HTTPS metrics/health（旧版 10252） |
| kube-proxy | 10249 | metrics（默认绑定 127.0.0.1） |
| kube-proxy | 10256 | healthz（默认绑定 0.0.0.0） |
| NodePort | 30000–32767 | 默认 Service NodePort 范围 |

注意：

- 10250 必须只对控制面/监控网络开放；它不是“内部端口”就可以裸露。
- 旧版 `--insecure-port` 已移除；任何“HTTP 8080 管理 API”的说法都已过时。
- 云 LB 健康检查通常打 API Server 的 `/readyz` 或 kubelet 的 `/healthz`，要确保安全组放行。

### 证书

kubeadm 生成的 PKI 是理解控制面身份的好材料：

```text
/etc/kubernetes/pki/
├── ca.crt / ca.key                     # 集群 CA
├── apiserver.crt / apiserver.key       # API Server 服务端证书
├── apiserver-kubelet-client.crt        # API Server → kubelet 客户端证书
├── front-proxy-ca.crt                  # 聚合 API（如 metrics-server）前端代理
├── front-proxy-client.crt
├── sa.key / sa.pub                     # ServiceAccount token 签名
└── etcd/
    ├── ca.crt
    ├── server.crt / server.key
    ├── peer.crt / peer.key
    └── healthcheck-client.crt
```

要点：

- **服务端证书 SAN** 必须包含所有 API Server 地址：VIP、DNS 名、节点 IP。否则 kubelet/kubectl 会报 `x509: certificate is valid for ...`。
- **front-proxy** 用于聚合 API（APIService），例如 metrics-server、custom.metrics.k8s.io。CRD 的 conversion webhook 走的是 API Server 作为客户端的 webhook 调用，不使用 front-proxy；两者容易混淆。
- **sa.key** 泄露等于可以伪造任意 ServiceAccount token；它不在普通 Secret 里，必须按根密钥保护。
- kubelet 证书通常由 TLS bootstrap 自动签发和轮转；手动签发时要设置 `--rotate-certificates`。

### 关键启动参数

API Server：

```text
--authorization-mode=Node,RBAC
--enable-admission-plugins=NodeRestriction,PodSecurity
--audit-policy-file=/etc/kubernetes/audit-policy.yaml
--encryption-provider-config=/etc/kubernetes/encryption-config.yaml
--etcd-servers=https://127.0.0.1:2379
--service-account-issuer=https://kubernetes.default.svc
--service-account-key-file=/etc/kubernetes/pki/sa.pub
```

kubelet：

```text
--bootstrap-kubeconfig=/etc/kubernetes/bootstrap-kubelet.conf
--kubeconfig=/etc/kubernetes/kubelet.conf
--container-runtime-endpoint=unix:///run/containerd/containerd.sock
--cgroup-driver=systemd
--rotate-certificates=true
--eviction-hard=memory.available<100Mi,nodefs.available<10%
```

这些参数不是“背下来”的知识点，而是故障定位的线索：证书 SAN、admission 插件顺序、etcd 地址、cgroup driver、驱逐阈值，任何一个配置错误都会表现为“集群看起来在，但 Pod 起不来”。

MiniK8s 用单进程 HTTP 服务模拟 API Server，不涉及 TLS/证书；第 23 章和第 35 章分别讲认证/授权与 API Server 实现，附录 F 给出真实集群的安装与证书配置。

## 2.10 API Server 的请求管线与 watch cache

第 2.4 节给了请求的“黑盒路径”。这一节打开盒子，看一个 GET/PUT/WATCH 在 API Server 内部经过什么。理解它，才能解释“为什么 LIST 很贵”“为什么 watch 会 410”“为什么一致读有时慢”。

### 2.10.1 一条写请求

```text
HTTP/TLS
  → 认证（x509/OIDC/SA token/webhook）
  → 审计（RequestReceived）
  → 授权（RBAC/Node/webhook）
  → 准入（mutating → validating，含 webhook/VAP）
  → 解码/默认值/校验（schema、field validation）
  → 存储（etcd 事务）
  → watch cache 更新 + 事件分发
  → 审计（ResponseComplete）
  → 响应
```

关键点：

- **认证/授权/准入顺序不可反**：未认证不进授权，未授权不进准入。
- **mutating 先于 validating**：mutating 可能改对象，validating 看到最终形态。
- **webhook 是外部调用**：超时/失败策略直接影响可用性；失败的 webhook 可能拒绝所有写。
- **默认值/校验在存储前**：写入 etcd 的是“服务器认为合法的对象”。
- **watch cache 更新在存储后**：事件可能略晚于写响应，客户端不能假设“写完立刻 watch 到”。

### 2.10.2 watch cache 与 LIST

API Server 在每个资源上维护 watch cache：

- 缓存最近的对象与事件。
- LIST 可以从 cache 读，避免每次打 etcd。
- WATCH 从 cache 的事件环形缓冲重放，再切换到实时事件。
- cache 有大小/历史窗口；落后太多的 watch 会收到 410（too old resource version），客户端必须重新 LIST。

这解释了几个现象：

- **LIST 快，但不要把 cache 读等同于陈旧读**：开启 `ConsistentListFromCache`（1.31 beta，默认开启）后，watch cache 通过 progress notify 提供线性一致语义；`resourceVersion=0` 才是“任意版本、可能陈旧”的快路径。两者不要混用。
- **watch 断连后要 relist**：410 不是错误，而是“你落后了，重新同步”。
- **cache 不是无限历史**：高频变更 + 慢客户端 = 被断开。

`resourceVersion` 的语义（简化）：

| 请求 | 含义 |
|---|---|
| LIST 不带 RV | 一致性读；通常由 watch cache 提供，语义上仍是最新 |
| LIST `resourceVersion=0` | 任意版本，通常从 cache，可能陈旧 |
| LIST 指定 RV | 至少与该 RV 一致（可能等待/走 etcd） |
| WATCH 不带 RV | 从当前时刻开始 |
| WATCH `resourceVersion=0` | 先发当前对象（ADDED），再发后续事件 |
| WATCH 指定 RV | 从该 RV 之后重放；太旧则 410 |

MiniK8s 的 `ResourceStore` 用事件 deque 模拟这套语义：保留最近事件、410、`send_initial` 快照。它没有真正的 cache/etcd 分层，但契约可观察。

### 2.10.3 一致读与性能的权衡

强一致读通常要：

- 等到 etcd 的某个 revision 被应用。
- 或直接读 etcd（绕过/校验 cache）。

代价是延迟与 etcd 压力。大规模控制器/列表通常接受“足够新”的 cache 读，再用 RV/条件收敛。不要把“强一致”当成默认需求；先问业务能否接受秒级陈旧。

### 2.10.4 聚合 API 与请求路由

API Server 还负责：

- `/api`、`/apis` 发现。
- 聚合 API（APIService）反向代理到外部 apiserver（如 metrics-server）。
- CRD 的动态路由。
- 子资源（status/scale/logs/exec/eviction）。

聚合 API 的故障模式：

- 外部 apiserver 不可用 → 该 group 的请求失败，但核心 API 仍可用。
- 证书/网络/服务发现错误 → 发现列表正常但请求 503。
- 聚合层与 CRD 的版本/转换边界容易混淆（见第 4 章）。

### 2.10.5 限流、APF 与审计

- **APF** 在认证之后、处理之前对请求分类/排队，保护 API Server。
- **审计**在每个阶段记录事件；`RequestResponse` 级别会记录 body，成本高。
- **metrics** 暴露请求延迟/错误/inflight，是容量与故障定位的第一手数据。

一条实用的排障顺序：

```text
429? → APF/限流
401/403? → 认证/授权
4xx schema? → 准入/校验
5xx/超时? → etcd/webhook/聚合层
watch 410? → 客户端落后，relist
LIST 慢? → cache/etcd/对象大小/分页
```

## 2.11 流式 API：logs、exec、attach 与 port-forward

大多数 API 请求是“请求-响应”：客户端发 JSON，服务端返回对象。排障与运维还需要四类**长连接流式请求**，它们走的是另一条路径：

| 子资源 | 方向 | 典型客户端 | 授权资源 |
|---|---|---|---|
| `pods/log` | 服务端 → 客户端 | `kubectl logs -f` | `pods/log` |
| `pods/exec` | 双向 | `kubectl exec -it` | `pods/exec` |
| `pods/attach` | 双向 | `kubectl attach` | `pods/attach` |
| `pods/portforward` | 双向 | `kubectl port-forward` | `pods/portforward` |

它们不是普通 REST 的“返回一个大 JSON”，而是把 TCP 连接升级为多路复用流。理解这条路径，才能解释“为什么 exec 卡住”“为什么 logs 看不到”“为什么 port-forward 绕过了 Service”。

### 2.11.1 请求路径

```text
kubectl
  │ GET  /api/v1/namespaces/ns/pods/pod/log?follow=true
  │ POST /api/v1/namespaces/ns/pods/pod/exec?command=sh&stdin=true&stdout=true&tty=true
  ▼
kube-apiserver
  │ 认证 → 授权（pods/exec 等子资源）→ 审计
  │ 反向代理 / 连接劫持（hijack）
  ▼
kubelet（该 Pod 所在节点）
  │ /exec/{namespace}/{pod}/{container}
  │ /attach/...、/portForward/...、/logs/...
  ▼
CRI / 容器运行时
  │ Exec / Attach / PortForward / 读取日志文件
  ▼
容器进程
```

关键点：

- **API Server 不执行命令**。它只做认证、授权、审计和代理；真正的 exec 由 kubelet 调用 CRI 运行时完成。
- **kubelet 必须可达**。API Server 通过 kubelet 的 10250 端口（或配置的端口）建连；kubelet 证书、节点网络、防火墙都会影响流式请求。
- **子资源授权是独立的**。一个用户可能有 `get pods`，但没有 `create pods/exec`；RBAC 要显式授予 `pods/exec`。
- **审计记录请求，不一定记录输出**。命令参数可能出现在审计事件/URL 中，但 stdout/stderr 通常不进入审计日志。

### 2.11.2 协议：SPDY 与 WebSocket

早期 `kubectl exec/attach/port-forward` 使用 SPDY 多路复用；较新版本逐步转向 WebSocket（KEP-4006），因为 WebSocket 更容易穿过代理、被浏览器/网关支持，也更符合 HTTP/2 之后的生态。

无论哪种传输，语义都是“在一个连接上开多个逻辑流”：

```text
stream 0: stdin
stream 1: stdout
stream 2: stderr
stream 3: error / exit status
stream 4: terminal resize（TTY 时）
```

WebSocket 子协议通常是 `v4.channel.k8s.io`；`v5.channel.k8s.io` 增加了关闭/错误语义。客户端与服务端版本不匹配时，`kubectl` 会回退到 SPDY 或报错。生产排障时先确认：

```bash
kubectl version --client
kubectl exec ... -v=8   # 看 negotiating protocol / Upgrade 请求
```

### 2.11.3 logs 的特殊性

`kubectl logs` 看似简单，实际有三条路径：

1. **容器日志文件**：kubelet 读取 `/var/log/pods/<ns>_<pod>_<uid>/<container>/<n>.log`（CRI 日志格式：时间、流、tag、消息）。
2. **`--previous`**：读取上一次容器实例的日志文件；容器已被 GC 时读不到。
3. **聚合日志**：生产通常由 Fluent Bit/Vector/Fluentd 采集到 Loki/Elasticsearch，`kubectl logs` 只用于即时排障。

常见限制：

- **日志轮转**：kubelet 的 `--container-log-max-size`/`--container-log-max-files` 控制单文件与保留数量；轮转后旧内容可能不在 `kubectl logs` 中。
- **只有 stdout/stderr**：写文件、syslog、Windows Event Log 的日志不会被 `kubectl logs` 采集。
- **follow 绑定当前容器实例**：容器重启后流会结束，需要重新执行（或用 stern 自动重连）；要排查崩溃前的日志用 `--previous`。
- **多容器必须 `-c`**：不指定时 `kubectl logs` 会直接报错要求选择容器（`--all-containers` 可看全部），不会默默选择第一个。

### 2.11.4 exec、attach 与 port-forward

- **exec**：在已有容器里启动新进程。它不经过 Pod 的 entrypoint，也不受镜像默认 CMD 限制；但受容器文件系统、用户、capability、seccomp 限制。`kubectl exec` 不是“SSH”，容器里通常没有 sshd，也不需要。
- **attach**：连接到容器的主进程 stdin/stdout/stderr，适合前台进程调试；容器主进程退出后 attach 也结束。
- **port-forward**：把本地端口转发到 **Pod** 的端口，不经过 Service、Endpoints、kube-proxy，也不做负载均衡。它适合调试单个 Pod，但不能验证 Service/DNS/NetworkPolicy 路径。

`kubectl debug` 是更高层的封装：它可能创建 ephemeral container、复制 Pod 或创建调试节点 Pod。ephemeral container 不参与调度、不能加资源、不能改镜像，但能共享进程命名空间（`shareProcessNamespace`）观察目标容器。

### 2.11.5 安全与运维边界

流式 API 是“把任意命令送进生产容器”的通道，风险高于普通读写：

- **RBAC 最小化**：不要把 `pods/exec` 授予所有开发者；用 namespace 级 Role + 审批/临时凭证。
- **审计**：记录谁在什么时候 exec 了哪个 Pod；命令参数可能含敏感信息，审计后端要脱敏。
- **准入**：PSA 不覆盖 exec；但 admission webhook 可以用 `rules[].operations: ["CONNECT"]` 匹配 `pods/exec` 并拒绝。不要依赖“exec 是子资源所以不会被拦”的假设。PSA 本身仍用于限制 privileged、hostPath、hostNetwork。
- **网络**：API Server 到 kubelet 的 10250 必须通；kubelet 服务端证书要能被 API Server 校验；否则表现为 `error: unable to upgrade connection` 或超时。
- **超时**：kubelet 的 `--streaming-connection-idle-timeout` 默认 4 小时；长连接要关注空闲断开与客户端重连。
- **替代方案**：生产调试优先用日志/指标/追踪/`kubectl debug` 临时容器；exec 作为受控的例外，而不是默认运维方式。

### 2.11.6 MiniK8s 的映射

MiniK8s 不实现真实的多路复用连接，但保留了可观察契约：

- `kubectl logs` 读取 kubelet 的模拟日志缓冲区，支持容器选择与 tail。
- `kubectl exec` 由 `Kubelet.exec` 解析命令，支持读取 ConfigMap/Secret/fieldRef 环境变量，返回 stdout/stderr/exitCode。
- 没有 attach、port-forward、TTY/resize、流式 follow；`examples/` 与测试验证的是“命令解析 + 状态返回”，不是字节流。

这条差距是刻意的：流式协议属于传输层实现，核心学习目标是理解“授权子资源 → API Server 代理 → kubelet → CRI”的责任链。

## 本章小结

- 控制面由 API Server、etcd、Scheduler、Controller Manager 组成；节点面由 kubelet、CRI、CNI、kube-proxy 组成。
- 所有协调通过 API 对象与 watch 完成，组件之间不直接 RPC。
- 请求路径长且异步，任何一步失败都应通过 status/event 观测。
- 高可用要分别设计控制面与数据面；etcd quorum 是控制面的关键约束。

## 延伸阅读

- [第 4 章 API 对象模型](04-api-object-model.md)
- [第 18 章 控制器模式](18-controller-pattern.md)
- [第 28 章 高可用与灾难恢复](28-ha-disaster-recovery.md)
- [第 35 章 API Server 与存储实现](35-apiserver-store.md)
- [附录 F 集群搭建与生产化手册](../appendices/F-cluster-build.md)
