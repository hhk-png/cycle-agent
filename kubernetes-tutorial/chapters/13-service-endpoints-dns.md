# 第 13 章 Service、Endpoints 与 DNS

## 13.1 Pod IP 为什么不能直接用

Pod IP 是临时的：

- Pod 重建后 IP 变化。
- Deployment 滚动时新旧 Pod 并存。
- 节点故障后 Pod 可能迁移。
- 客户端不应该知道具体副本。

Service 提供稳定的虚拟 IP 和 DNS 名，把流量转发到一组 Ready 的 Pod。

## 13.2 Service 类型

```yaml
apiVersion: v1
kind: Service
metadata:
  name: web
spec:
  selector:
    app: web
  ports:
    - name: http
      port: 80
      targetPort: 8080
      protocol: TCP
  type: ClusterIP
```

### ClusterIP

默认类型。分配一个集群内部虚拟 IP：

```text
10.96.0.10:80 → Endpoints 中的 Pod:8080
```

只能集群内访问。

### NodePort

在每个节点上开放一个端口：

```yaml
type: NodePort
ports:
  - port: 80
    targetPort: 8080
    nodePort: 30080
```

访问 `NodeIP:30080` 即可。端口范围默认 30000–32767。生产不推荐直接暴露 NodePort：

- 端口管理混乱。
- 没有 L7 能力。
- 节点 IP 变化/安全组问题。
- 通常只用于临时调试或自建 LB 后端。

### LoadBalancer

云厂商创建外部负载均衡器：

```yaml
type: LoadBalancer
```

`status.loadBalancer.ingress` 会填充外部 IP/域名。行为取决于 cloud provider/CCM：

- AWS NLB/CLB。
- GCP TCP/UDP LB。
- Azure LB。
- 自建 MetalLB。

成本、健康检查、跨区、源 IP 保留等细节因实现而异。

### ExternalName

把 Service 映射到外部 DNS 名：

```yaml
type: ExternalName
externalName: db.example.com
```

不分配 ClusterIP，不代理流量；只是 DNS CNAME。适合迁移期或访问外部依赖。

### Headless

```yaml
clusterIP: None
```

不分配虚拟 IP，DNS 直接返回 Pod IP。StatefulSet 用它提供逐 Pod DNS。客户端自己做负载均衡/选主。

## 13.3 Endpoints 与 EndpointSlice

Service 本身不维护后端列表；Endpoints/EndpointSlice 控制器根据 selector 找到 Ready Pod：

```yaml
apiVersion: v1
kind: Endpoints
metadata:
  name: web
subsets:
  - addresses:
      - ip: 10.244.1.5
        targetRef: {kind: Pod, name: web-abc}
    ports:
      - port: 8080
```

EndpointSlice 是新一代 API：

```yaml
apiVersion: discovery.k8s.io/v1
kind: EndpointSlice
metadata:
  labels:
    kubernetes.io/service-name: web
addressType: IPv4
endpoints:
  - addresses: [10.244.1.5]
    conditions:
      ready: true
ports:
  - port: 8080
```

为什么有 EndpointSlice：

- Endpoints 是单个大对象，大规模下更新开销大。
- EndpointSlice 按 100 个端点分片，减少 watch 和写入压力。
- 支持拓扑信息、terminating 状态、服务网格元数据。

Service 的 selector 为空时，Endpoints 不会被控制器管理，可以手工维护（用于外部服务）。

## 13.4 就绪与流量

默认只有 Ready 的 Pod 会进入 Endpoints（`publishNotReadyAddresses: true` 是例外，见 13.12）。就绪由：

- readinessProbe。
- `minReadySeconds`（影响 available，不直接影响 endpoint readiness）。
- Pod 的 Ready condition。

Pod 终止时：

- EndpointSlice 把 `terminating` 标记为 true，`ready` 可能变 false。
- kube-proxy 逐步摘除。
- preStop/优雅退出给连接排空时间。

常见 502：

- Pod 已停止但 Endpoint 还没摘除。
- readinessProbe 太宽松。
- 应用不处理 SIGTERM。
- 负载均衡器健康检查与 Pod readiness 不一致。

## 13.5 kube-proxy 模式

### iptables

为每个 Service/Endpoint 写 iptables 规则：

- 随机选择后端。
- 规则数量随 Service/Endpoint 增长。
- 大规模下同步慢、排查难。

### IPVS

内核四层负载均衡：

- 哈希表，规则更多时性能更好。
- 支持 rr、wrr、lc 等调度算法。
- 需要 ipvsadm 和内核模块。
- 连接跟踪、会话保持有细节。
- **1.35 起 IPVS 模式被弃用**，官方计划在后续版本禁用并移除；新集群不要选 IPVS，存量集群按 nftables/eBPF 方向迁移。

### nftables

1.33 起 GA，作为 iptables/IPVS 的继任方向：规则更新更高效，不再依赖线性 iptables 链。不同发行版/版本的支持程度不同，切换前要做 Service、DNS、NetworkPolicy 和 conntrack 的回归。

### eBPF

Cilium 等直接在内核完成 Service 转发，绕过 kube-proxy：

- 性能好，可绕过 iptables 链。
- 可做 L7 策略、可观测性。
- 排障模型不同，需要熟悉 eBPF 工具。

MiniK8s 用 `ServiceProxy` 在进程内选择 Endpoint，并检查 NetworkPolicy，不实现内核转发。

## 13.6 会话亲和

```yaml
spec:
  sessionAffinity: ClientIP
  sessionAffinityConfig:
    clientIP:
      timeoutSeconds: 10800
```

按客户端 IP 粘滞。问题：

- NAT 后大量客户端共享一个 IP。
- 扩缩容时分布不均。
- 不适合有状态会话；应使用外部 session store。

大多数无状态服务不需要 sessionAffinity。

## 13.7 拓扑感知路由

旧字段 `spec.topologyKeys` 在 1.21 弃用、**1.22 移除**，现代集群上写了会校验失败。当前的替代是 `trafficDistribution`：

```yaml
spec:
  trafficDistribution: PreferSameZone
```

`ServiceTrafficDistribution` 在 1.33 GA。取值包括：

- `PreferSameZone`：优先同可用区后端，减少跨区延迟和流量成本。
- `PreferSameNode`：优先同节点后端，进一步降低延迟；没有本地后端时回退。
- `PreferClose`：旧名称，1.34 起被弃用，逐步迁移到 `PreferSameZone`。

实现依赖 kube-proxy/CNI/云 LB；不同数据面的支持程度不同，采用前确认目标版本与 CNI/kube-proxy 能力。

节点本地转发还有两个独立字段：

- `spec.internalTrafficPolicy: Local`：集群内访问优先只路由到本节点 Pod；没有本地 Endpoint 时会丢流量。
- `spec.externalTrafficPolicy: Local`：NodePort/LoadBalancer 保留客户端源 IP，只转发到本节点 Pod；同样有“本地无 Endpoint 即失败”的代价。

不要把 `trafficDistribution` 当成强保证：它是偏好，不是拓扑硬约束；需要硬约束时用拓扑感知的 EndpointSlice 消费方或应用层路由。

## 13.8 DNS

CoreDNS 为 Service 提供 DNS：

```text
web.default.svc.cluster.local
```

Pod 的 `/etc/resolv.conf` 通常包含：

```text
nameserver 10.96.0.10
search default.svc.cluster.local svc.cluster.local cluster.local
options ndots:5
```

因此：

- `web` → `web.default.svc.cluster.local`
- `web.default` → 同上
- `web.default.svc` → 同上

`ndots:5` 意味着名字中少于 5 个点会先尝试 search 域，可能产生额外 DNS 查询。跨命名空间访问建议使用 FQDN（末尾加 `.`）或至少 `service.namespace`。

StatefulSet 的 Pod 还有逐 Pod 记录（前提是 `serviceName` 指向 headless Service）：

```text
db-0.db.default.svc.cluster.local
db-1.db.default.svc.cluster.local
```

MiniK8s 的 `ClusterDNS` 同时解析 Service 名与这种 `<pod>.<headless-service>` 记录；没有逐 Pod DNS 时，客户端只能拿到 Service 的整体 A 记录，无法稳定定位某个 ordinal。

### dnsPolicy

- `ClusterFirst`（默认）：先集群 DNS，再上游。
- `Default`：继承节点 resolv.conf。
- `ClusterFirstWithHostNet`：hostNetwork Pod 使用集群 DNS。
- `None`：自定义 dnsConfig。

### DNS 排障

```bash
kubectl run dnsutils --image=registry.k8s.io/e2e-test-images/jessie-dnsutils:1.3 -- sleep 3600
kubectl exec -it dnsutils -- nslookup web.default.svc.cluster.local
kubectl exec -it dnsutils -- cat /etc/resolv.conf
kubectl -n kube-system logs -l k8s-app=kube-dns
```

常见问题：

- CoreDNS Pod 不 Ready。
- NetworkPolicy 阻止 UDP/TCP 53。
- Service 没有 Endpoints。
- 名字拼错/命名空间错。
- ndots 导致解析慢。
- 节点本地 DNS 缓存/上游 DNS 故障。

MiniK8s 的 `ClusterDNS` 支持短名、`svc`、FQDN、headless 多 A 记录。测试 `test_dns_short_fqdn_and_headless`。

### 13.8.1 NodeLocal DNS 与 CoreDNS 调优

大规模集群里，DNS 常是“看起来无关但影响全局”的瓶颈：

- 每个 Pod 的解析都打 CoreDNS，QPS 随 Pod 数/请求数增长。
- `ndots:5` 让短名产生多次查询，放大 QPS。
- CoreDNS 副本少/资源不足时，解析超时导致应用级联失败。
- conntrack 表满、UDP 丢包会让 DNS 表现为随机超时。

**NodeLocal DNSCache**：

- 每个节点运行一个 DNS 缓存（DaemonSet），Pod 的 resolv.conf 指向节点本地 IP。
- 缓存命中不走 CoreDNS；未命中由本地缓存转发。
- 减少跨节点 DNS 流量和 CoreDNS 压力，降低解析延迟。
- 需要 CNI/iptables 配合；hostNetwork Pod 的 dnsPolicy 要正确设置。

**CoreDNS 调优**：

```yaml
# 概念性 Corefile 片段
.:53 {
    errors
    health
    ready
    kubernetes cluster.local in-addr.arpa ip6.arpa {
        pods insecure
        fallthrough in-addr.arpa ip6.arpa
    }
    prometheus :9153
    forward . /etc/resolv.conf {
        max_concurrent 1000
    }
    cache 30
    loop
    reload
    loadbalance
}
```

关注：

- `cache` TTL 与业务解析变化频率。
- `forward` 的上游并发/超时；上游 DNS 故障会拖垮 CoreDNS。
- CoreDNS 副本数/资源 requests；用 HPA 或固定多副本。
- `max_concurrent` 防止上游过载。
- 监控 `coredns_dns_request_duration_seconds`、`coredns_dns_responses_total`、丢包/超时。

**ndots 的取舍**：

- `ndots:5` 方便短名，但增加查询次数。
- 应用使用 FQDN（末尾 `.`）可减少 search 尝试。
- 调整 `ndots` 要评估现有应用对短名的依赖。

**排查顺序**：

1. `nslookup`/`dig` 从 Pod 内解析 FQDN 与短名。
2. 看 `/etc/resolv.conf` 的 nameserver/search/ndots。
3. 看 CoreDNS/NodeLocal 日志与指标。
4. 看 NetworkPolicy 是否放行 53/UDP/TCP。
5. 看 conntrack/内核丢包。
6. 看上游 DNS 是否故障。

## 13.9 Service 与 NetworkPolicy

NetworkPolicy 可以限制：

- 谁能访问 Service 后端 Pod（Ingress）。
- 后端 Pod 能访问谁（Egress）。

注意：

- NetworkPolicy 作用于 Pod，不是 Service 的虚拟 IP。
- 策略按 namespace + podSelector 选择 Pod。
- 默认拒绝需要显式策略；没有策略时通常全通（取决于 CNI）。
- Service 的 ClusterIP 流量在策略中通常表现为 Pod IP。

## 13.10 无头服务与客户端负载均衡

Headless Service 返回所有 Pod IP，客户端可以选择：

- 随机。
- 轮询。
- 一致性哈希。
- 按拓扑。
- 主节点（通过 Operator 维护的 Service/ConfigMap）。

gRPC 长连接场景常见：

- 用 headless + 客户端负载均衡，避免 kube-proxy L4 每次连接固定后端。
- 使用服务网格 sidecar 做 L7 负载均衡。
- 使用 Gateway API/Envoy 做 L7。

## 13.11 外部访问模式对比

| 方式 | 层级 | 优点 | 缺点 |
|---|---|---|---|
| NodePort | L4 | 简单、无云依赖 | 端口管理、节点暴露 |
| LoadBalancer | L4 | 云原生、自动 LB | 成本、每服务一个 LB |
| Ingress | L7 | 共享 LB、按域名/路径路由 | 需要 controller、能力有限 |
| Gateway API | L7/L4 | 角色分离、可扩展 | 生态成熟度因实现而异 |
| Service Mesh | L7 | 流量治理、mTLS、可观测 | 复杂度、资源开销 |

第 14 章展开 L7。

## 13.12 服务暴露的细节

### externalTrafficPolicy

NodePort/LoadBalancer Service 可以设置：

```yaml
spec:
  externalTrafficPolicy: Local
```

- `Cluster`（默认）：流量可以转发到任意节点的后端，可能多一跳，源 IP 可能被 SNAT。
- `Local`：只转发到本节点的 Pod；保留源 IP，但节点没有后端时流量失败。

`Local` 常用于需要真实客户端 IP 的场景，但会带来：

- 节点间负载不均。
- 节点没有 Ready 后端时健康检查/流量失败。
- 需要配合 DaemonSet/节点本地副本。

### 其他容易忽略的 Service 字段

| 字段 | 作用 | 代价/注意 |
|---|---|---|
| `internalTrafficPolicy` | 集群内流量是否只走本节点（`Local`/`Cluster`） | `Local` 可能丢流量、负载不均 |
| `loadBalancerClass` | 指定由哪个 LB 实现处理该 Service | 设置后其他控制器不应接管 |
| `allocateLoadBalancerNodePorts` | LoadBalancer 是否同时分配 NodePort | 设为 `false` 可减少端口占用，但依赖 LB 直接转发 |
| `healthCheckNodePort` | `externalTrafficPolicy: Local` 时 LB 健康检查端口 | 由控制器分配；需在防火墙/安全组放行 |
| `publishNotReadyAddresses` | 是否把未 Ready 的 Pod 写入 Endpoints | 仅用于有状态集群的 peer 发现等特殊场景 |
| `sessionAffinity` | 客户端 IP 粘滞 | NAT 后大量客户端共享 IP，分布不均 |

### 无 selector 的 Service

```yaml
kind: Service
spec:
  ports: [{port: 80}]
---
kind: Endpoints
metadata: {name: external-db}
subsets:
  - addresses: [{ip: 10.0.0.10}]
    ports: [{port: 5432}]
```

用于把外部服务映射成集群内 DNS 名，或由 Operator 手工维护后端（例如数据库主节点）。没有 selector 时，Endpoints 控制器不会覆盖你写的 Endpoints。

### 双栈

集群可以同时支持 IPv4/IPv6：

- Pod 可以有 IPv4/IPv6 地址。
- Service 可以指定 `ipFamilyPolicy`（SingleStack/PreferDualStack/RequireDualStack）。
- DNS 返回 A/AAAA 记录。

前提：

- CNI/节点/云网络支持。
- kube-proxy/数据面支持。
- 应用监听正确地址族。

双栈会增加地址规划/排障复杂度，不要为了“先进”而启用。

## 13.13 MiniK8s 的实现

- `ServiceController` 分配 ClusterIP（`10.96.0.0/16`）和 NodePort（30000–32767），设置 LoadBalancer status。
- `EndpointsController` 根据 Ready Pod 生成 Endpoints 和 EndpointSlice，端口使用 targetPort。
- `ClusterDNS` 解析短名/FQDN/headless。
- `ServiceProxy` 选择 Endpoint，检查 NetworkPolicy。

示例：

```bash
python -m mini_k8s.cli apply -f examples/01-hello.yaml
python -m mini_k8s.cli get svc
python -m mini_k8s.cli get endpoints
python -m mini_k8s.cli get endpointslices
```

测试：

- `test_service_allocates_cluster_ip_and_endpoints`
- `test_dns_short_fqdn_and_headless`
- `test_node_port_and_load_balancer`
- `test_proxy_enforces_network_policy`

## 13.14 一个数据包的路径与 conntrack

理解 Service 排障，最好能在脑子里走一遍数据包路径。以 iptables 模式、客户端 Pod 访问 `web:80` 为例：

```text
客户端 Pod (10.244.1.7)
  │ 1. DNS 解析 web.default.svc.cluster.local → 10.96.0.10
  │ 2. 发往 10.96.0.10:80，出 veth，进入宿主 network namespace
  ▼
节点内核
  │ 3. PREROUTING/OUTPUT 命中 KUBE-SERVICES
  │ 4. DNAT 到某个 Ready Pod IP:targetPort（例如 10.244.2.9:8080）
  │ 5. conntrack 记录五元组映射
  ▼
目标节点（跨节点时经 CNI/overlay）
  │ 6. 到达目标 Pod，回包源地址是 Pod IP
  ▼
回程
  │ 7. conntrack 反向转换，客户端看到来自 10.96.0.10:80 的响应
```

关键点：

- **DNAT 只发生在连接建立时**，后续包依赖 conntrack；conntrack 表满会导致新连接失败，而老连接可能仍正常。
- **hairpin（同 Pod/同节点回环）**需要额外 MASQUERADE，否则回包可能绕过 DNAT；不同 kube-proxy 模式处理不同。
- **externalTrafficPolicy: Local** 不做跨节点转发，源 IP 保留，但没有本地后端的节点会直接失败。
- **EndpointSlice terminating** 让数据面在新连接选择时避开正在终止的 Pod；老连接仍可排空。
- **NetworkPolicy** 通常在 DNAT 之后按 Pod IP 判定，所以你看到的是后端 Pod 的地址，而不是 ClusterIP。
- **会话亲和**本质是 conntrack/哈希选择，不是应用层 cookie；客户端 IP 经 NAT 后可能全部相同。

排障命令：

```bash
# 节点上观察 Service 规则与命中计数
iptables-save | grep -A5 KUBE-SERVICES
iptables -t nat -L KUBE-SERVICES -n -v
conntrack -L | grep 10.96.0.10

# 直接验证后端，绕过 Service
kubectl exec -it <client> -- curl -v http://<pod-ip>:<target-port>

# 判断是 DNS、Service 还是 Pod 的问题
kubectl exec -it <client> -- getent hosts web
kubectl get endpointslice -l kubernetes.io/service-name=web -o wide
```

MiniK8s 的 `ServiceProxy` 只模拟“选择 Endpoint + 策略判定”，不产生 conntrack；真实集群中大量“偶发超时/新连接失败”都与 conntrack、NAT、MTU 或 endpoint 摘除时序有关，而不是 Service 对象本身。

## 本章小结

- Service 提供稳定虚拟 IP/DNS，EndpointSlice 维护 Ready 后端。
- ClusterIP/NodePort/LoadBalancer/ExternalName/Headless 解决不同访问场景。
- kube-proxy 有 iptables/IPVS/nftables/eBPF 多种实现，排障模型不同。
- DNS 问题常来自 CoreDNS、NetworkPolicy、Endpoints 和 ndots。
- 长连接/L7 场景要考虑 headless + 客户端 LB、Ingress/Gateway 或服务网格。
- 数据包路径依赖 DNAT + conntrack；大量“偶发超时/新连接失败”要查 conntrack、MTU 和 endpoint 摘除时序。

## 延伸阅读

- [第 14 章 Ingress 与 Gateway API](14-ingress-gateway-api.md)
- [第 15 章 CNI、eBPF 与 NetworkPolicy](15-cni-ebpf-networkpolicy.md)
- [第 38 章 网络、存储与安全实现](38-network-storage-security.md)
- [附录 O 网络取证手册](../appendices/O-network-forensics.md)：DNS、conntrack 与规则级取证。
