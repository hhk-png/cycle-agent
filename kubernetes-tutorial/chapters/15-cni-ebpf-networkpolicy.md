# 第 15 章 CNI、kube-proxy、eBPF 与 NetworkPolicy

## 15.1 Kubernetes 网络模型

Kubernetes 对 Pod 网络有三条基本要求：

1. 每个 Pod 有自己的 IP。
2. Pod 之间可以直接通信，不经过 NAT（同一集群内）。
3. 节点上的进程可以与所有 Pod 通信。

这被称为“扁平网络模型”。它不规定如何实现，只规定可观测行为：

- Pod 内看到的是自己的 IP。
- 两个 Pod 通信时，源 IP 是发送方 Pod IP。
- Service 是虚拟 IP，由转发规则实现。

实现这些要求的组件叫 CNI 插件。

## 15.2 CNI 是什么

CNI（Container Network Interface）是一个规范：

- 容器运行时在创建/删除网络命名空间时调用 CNI 插件。
- 插件负责：
  - 分配 IP（IPAM）。
  - 创建 veth pair。
  - 配置路由。
  - 配置网络策略（部分插件）。
  - 返回结果给运行时。

CNI 配置示例：

```json
{
  "cniVersion": "1.0.0",
  "name": "mynet",
  "type": "bridge",
  "bridge": "cni0",
  "isGateway": true,
  "ipMasq": true,
  "ipam": {
    "type": "host-local",
    "ranges": [[{"subnet": "10.244.0.0/16"}]]
  }
}
```

CNI 插件通常是二进制文件，放在 `/opt/cni/bin`，配置在 `/etc/cni/net.d`。

## 15.3 常见 CNI 实现

- **Calico**：BGP 路由、IPAM、NetworkPolicy、eBPF 模式。
- **Cilium**：eBPF 数据面、L3-L7 策略、Service、可观测性、Service Mesh。
- **Flannel**：简单 overlay（VXLAN/host-gw），策略能力弱。
- **Weave**：较早的 overlay，逐渐被替代。
- **Antrea**：基于 Open vSwitch，VMware/Project Antrea。
- **AWS VPC CNI**：Pod 直接使用 VPC IP（ENI），与安全组集成。
- **Azure CNI**：Pod 使用 VNet IP 或 overlay。
- **GKE Dataplane V2**：基于 Cilium/eBPF。

选择维度：

- 是否需要 NetworkPolicy。
- 是否可以接受 overlay 封装开销。
- IP 地址空间是否足够（VPC-native 会消耗 VPC IP）。
- 性能（eBPF vs iptables）。
- 多集群/混合云。
- 可观测性和排障工具。
- 团队熟悉度。

## 15.4 Overlay 与 Underlay

### Overlay

Pod 包封装在节点网络包中（VXLAN、Geneve、IP-in-IP）：

- 优点：不依赖底层网络，跨子网简单。
- 缺点：封装开销、MTU 变小、排障多一层。

### Underlay / 路由

Pod IP 直接在底层网络路由（BGP、云路由表、ENI）：

- 优点：性能好、无封装、源 IP 保留。
- 缺点：需要底层网络配合、IP 规划、跨子网限制。

### 云厂商 VPC-native

Pod 直接从 VPC 子网分配 IP：

- 与安全组、路由表、负载均衡集成好。
- 消耗 VPC IP，需要规划大子网。
- ENI 数量/每 ENI IP 数有限制。

## 15.5 Service 转发与 kube-proxy

Service 的 ClusterIP 是虚拟的，没有网卡。实现方式：

- **iptables**：DNAT 到后端 Pod IP。
- **IPVS**：内核虚拟服务器。
- **nftables**：新规则引擎。
- **eBPF**：在 socket/TC 层直接重定向。

kube-proxy watch Service/EndpointSlice，更新规则。它不转发数据包本身（除了少量模式），只是编程内核。

大规模问题：

- iptables 规则线性增长，更新抖动。
- conntrack 表压力。
- Service 数量多时，规则同步慢。
- 节点上 `iptables-save` 巨大，排障困难。

eBPF 方案把转发逻辑放进内核程序，使用哈希表，避免线性规则。

## 15.6 eBPF 是什么

eBPF 允许在内核中运行受限程序，挂载到：

- socket（connect、sendmsg）。
- TC ingress/egress。
- XDP（驱动层）。
- kprobe/tracepoint（可观测性）。

Cilium 用 eBPF 实现：

- Pod 网络连接。
- Service 负载均衡。
- NetworkPolicy（L3/L4/L7）。
- 可观测性（Hubble）。
- 加密（WireGuard/IPsec）。
- 部分 Service Mesh 功能（Sidecarless）。

优势：

- 高性能、低延迟。
- 绕过 iptables/conntrack。
- 细粒度策略和可观测性。

代价：

- 内核版本要求。
- 排障工具链不同。
- 团队需要理解 eBPF/CTF。
- 某些云环境限制加载 eBPF 程序。

## 15.7 NetworkPolicy 语义

NetworkPolicy 是命名空间资源：

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: api-allow-trusted
  namespace: default
spec:
  podSelector:
    matchLabels:
      app: api
  policyTypes: [Ingress]
  ingress:
    - from:
        - podSelector:
            matchLabels:
              app: trusted
      ports:
        - protocol: TCP
          port: 8080
```

关键规则：

- `podSelector` 选择被保护的 Pod；`{}` 选中 namespace 内所有 Pod。
- `policyTypes` 声明 Ingress/Egress；不写时由规则推断（有 ingress 规则含 Ingress，有 egress 规则含 Egress，都没有则默认 Ingress）。
- `ingress` 省略或为 `[]`：拒绝所有入站；`ingress: [{}]`：允许所有入站。`from` 省略或为 `[]`：该规则允许所有来源。
- 同一 namespace 的 `podSelector` 默认匹配本 namespace；跨 namespace 用 `namespaceSelector`。
- `ipBlock` 用 CIDR，`except` 排除子网。
- 多个策略是**并集**：只要有一个策略允许，流量就允许。
- 没有策略选择某 Pod 时，该 Pod 默认全通；但 CNI 可能有集群级默认拒绝（如 Cilium 的 `CiliumClusterwideNetworkPolicy`）。
- 集群级策略不是核心 NetworkPolicy 的一部分：Kubernetes 的 `ClusterNetworkPolicy`（network-policy-api）仍是 alpha/out-of-tree，Cilium/Calico 的 clusterwide CRD 也各不相同。跨命名空间/全集群隔离要按所选 CNI 的 API 设计，不能假设可移植。

端口范围、命名端口与 CIDR 例外：

```yaml
ingress:
  - from:
      - ipBlock:
          cidr: 10.0.0.0/8
          except: ["10.1.0.0/16"]
    ports:
      - protocol: TCP
        port: 8000
        endPort: 9000          # 8000-9000，1.25 GA；需 CNI 支持
      - protocol: TCP
        port: http             # 命名端口，匹配容器 ports[].name
```

`endPort` 只能与数字 `port` 搭配；命名端口由 CNI 在运行时解析为具体端口。`ipBlock` 的 `except` 必须包含在 `cidr` 内。

空值语义容易混：

| 写法 | 含义 |
|---|---|
| `ingress` 省略，`policyTypes` 含 Ingress | 拒绝所有入站 |
| `ingress: []` | 同上 |
| `ingress: [{}]` | 允许所有入站 |
| `ingress: [{from: []}]` | 允许所有来源（该规则） |
| `from: [{}]` | 空 selector，等同允许所有来源 |
| `from: [{namespaceSelector: {}, podSelector: {}}]` | 所有 namespace 的所有 Pod（同一元素内 AND） |

`namespaceSelector` 与 `podSelector` 写在**同一个** `from` 元素内是 AND（某 namespace 中带某标签的 Pod）；写成两个元素是 OR（该 namespace 所有 Pod，或本 namespace 带标签的 Pod）。

默认拒绝示例：

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: default-deny
  namespace: team-a
spec:
  podSelector: {}
  policyTypes: [Ingress, Egress]
```

这会拒绝所有进出流量，包括 DNS。需要额外允许 DNS：

```yaml
egress:
  - to:
      - namespaceSelector:
          matchLabels:
            kubernetes.io/metadata.name: kube-system
        podSelector:
          matchLabels:
            k8s-app: kube-dns
    ports:
      - protocol: UDP
        port: 53
      - protocol: TCP
        port: 53
```

## 15.8 NetworkPolicy 的盲区

NetworkPolicy 不是防火墙的全部：

- 不控制节点到 Pod 的流量（取决于 CNI）。
- 不控制 Pod 到节点/外部的流量，除非 Egress 策略。
- 不提供 L7 策略（Cilium 扩展除外）。
- 不提供身份认证。
- 不记录日志（需要 CNI 支持）。
- 策略执行依赖 CNI；不支持的 CNI 会静默忽略。

生产建议：

- 先监控（audit）再强制。
- 从默认拒绝开始，逐条放行。
- 明确 DNS、监控、日志、Ingress Controller 的访问。
- 用策略测试工具（如 Cilium `policy trace`、np-guard）验证。
- 把 NetworkPolicy 纳入 GitOps 和代码审查。

## 15.9 多租户网络隔离

仅靠 namespace 不隔离网络。需要：

- 默认拒绝策略。
- 只允许同 namespace 通信（可选）。
- 明确跨 namespace 的服务依赖。
- 控制面/节点访问限制。
- 出口网关（Egress Gateway）做白名单。

注意：

- 同节点 Pod 可能通过 host 网络/本地路由绕过某些策略，取决于 CNI。
- 多集群需要集群间网络策略/服务网格。
- 云 LB 健康检查来源 IP 要放行。

## 15.10 调试网络

工具：

```bash
kubectl run nettest --image=nicolaka/netshoot -- sleep 3600
kubectl exec -it nettest -- ip addr
kubectl exec -it nettest -- ip route
kubectl exec -it nettest -- nslookup web
kubectl exec -it nettest -- curl -v http://web
kubectl exec -it nettest -- tcpdump -i any -n port 80
```

节点侧：

```bash
ip link
ip route
iptables-save | grep <service>
conntrack -L
crictl pods
crictl inspectp <pod-id>
```

常见问题：

- Pod 没有 IP：CNI/IPAM 失败。
- 跨节点不通：路由/BGP/overlay 隧道问题。
- 同节点通、跨节点不通：节点路由/防火墙。
- Service 不通但 Pod IP 通：kube-proxy/Endpoint/iptables。
- DNS 不通：CoreDNS 或 53 端口策略。
- MTU 问题：大包超时，小包正常（overlay 封装）。
- conntrack 表满：新连接失败。

## 15.11 MiniK8s 的实现

MiniK8s 不创建真实网络，而是实现策略判定：

`mini_k8s/network/policy.py`：

- `NetworkSimulator.can_connect(source, target, port, protocol)`。
- 计算 egress 策略是否允许。
- 计算 ingress 策略是否允许。
- 支持 podSelector、namespaceSelector、ipBlock、ports。

`ServiceProxy.connect`：

- 解析 Endpoints。
- 轮询选择后端。
- 调用策略判定。
- 返回模拟 HTTP 响应。

测试：

- `test_proxy_enforces_network_policy`
- `test_network_policy_egress_and_namespace_selector`

## 15.12 多网卡、CNI 链与双栈

生产集群很少只有“一个 CNI、一个网卡”。以下扩展会影响 IP 规划、策略和排障。

### CNI 链（chaining）

CNI 配置可以是一个列表，运行时按顺序调用：

```json
{
  "cniVersion": "1.0.0",
  "name": "mynet",
  "plugins": [
    {"type": "calico", "ipam": {"type": "host-local"}},
    {"type": "bandwidth", "capabilities": {"bandwidth": true}},
    {"type": "portmap", "capabilities": {"portMappings": true}}
  ]
}
```

常见组合：

- 主 CNI 负责连通/IPAM，`portmap` 实现 hostPort，`bandwidth` 做限速。
- `tuning` 调整 MTU/sysctl。
- `dummy`/`bridge` 用于特殊场景。

链式插件失败会导致整个 Pod sandbox 创建失败；排障要看 kubelet/CRI 日志和 `/etc/cni/net.d` 的配置顺序。

### Multus：多网卡

Multus 作为“meta CNI”，为 Pod 附加多个网络：

```yaml
apiVersion: k8s.cni.cncf.io/v1
kind: NetworkAttachmentDefinition
metadata:
  name: sriov-net
spec:
  config: |
    {
      "cniVersion": "1.0.0",
      "type": "sriov",
      "master": "eth1",
      "ipam": {"type": "whereabouts", "range": "192.168.10.0/24"}
    }
```

```yaml
metadata:
  annotations:
    k8s.v1.cni.cncf.io/networks: sriov-net
```

用途：

- 数据面/存储面分离（业务网 + 高速网）。
- SR-IOV/DPDK、RDMA。
- 多租户独立网络。

代价：

- IP 规划复杂，策略要覆盖多张网卡。
- 不是所有 CNI 都支持多网络。
- 排障需要区分 `eth0` 与 `net1` 的路由/策略。

### 双栈

- 集群可同时分配 IPv4/IPv6：`ipFamilyPolicy`、`ipFamilies`。
- 需要 CNI、kube-proxy、CoreDNS、云 LB 全链路支持。
- 应用要监听正确地址族（`::` vs `0.0.0.0`），否则“IPv6 不通”可能只是 bind 错。
- 双栈不是安全边界；策略仍按 IP/CIDR 生效。

### MTU 与性能

- Overlay 封装会减少可用 MTU（VXLAN 通常减 50 字节），导致“小包通、大包丢”。
- 跨节点/跨区路径的 MTU 可能不同；用 `ping -M do -s` 验证。
- eBPF/DPDK/SR-IOV 的性能优势需要端到端调优，单点替换未必有效。

MiniK8s 只有单一模拟网络，不区分网卡/地址族；这些是真实集群网络设计的内容。

## 本章小结

- Kubernetes 网络模型要求 Pod 扁平 IP 可达；CNI 负责实现。
- Overlay 简单但有开销，underlay/VPC-native 性能好但需要网络规划。
- Service 转发由 kube-proxy/eBPF 实现；大规模下 iptables 有瓶颈。
- NetworkPolicy 是 L3/L4 白名单，依赖 CNI 执行，默认行为要明确。
- 网络排障要分层：Pod IP、节点路由、Service 规则、DNS、策略、MTU。

## 延伸阅读

- [第 13 章 Service、Endpoints 与 DNS](13-service-endpoints-dns.md)
- [第 16 章 服务网格与零信任](16-service-mesh-zero-trust.md)
- [第 31 章 故障排查方法论](31-troubleshooting.md)
- [附录 O 网络取证手册](../appendices/O-network-forensics.md)：从 Pod netns 到 overlay 的抓包路径。
