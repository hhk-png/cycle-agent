# 附录 O 网络取证手册：从 DNS 到数据包

> 第 13、15、31 章分别讲 Service、CNI/NetworkPolicy 和排障方法。本附录把它们收拢成一套**可执行的取证流程**：先定位失败发生在哪一层，再用最小代价拿到证据，最后把“猜”变成“验证”。命令以 Linux 节点和常见 CNI 为例；不同发行版、CNI、云网络的路径会不同。

## O.1 先建立数据包路径模型

一个 Pod 访问另一个 Pod 的 Service，通常经过：

```text
客户端 Pod
  │ veth pair
节点 A 的 bridge/CNI 设备
  │ 路由 / overlay 封装（VXLAN/Geneve/IPIP）或 underlay 路由
节点 B 的 CNI 设备
  │ veth pair
服务端 Pod

Service 流量额外经过：
  │ 节点 A 的 iptables/nftables/IPVS/eBPF
  │ DNAT: ClusterIP:port → PodIP:targetPort
  │ conntrack 记录反向 NAT
```

失败点可以粗分为：

1. **名字**：DNS 没解析、解析到错地址、search/ndots 干扰。
2. **策略**：NetworkPolicy、云安全组、节点防火墙、mTLS 拒绝。
3. **路由**：Pod CIDR 不通、overlay 未建立、跨 AZ/跨 VPC 路由缺失。
4. **NAT/Service**：Endpoints 为空、targetPort 错、kube-proxy 规则未更新、conntrack 冲突。
5. **传输**：MTU/分片、重传、连接队列、端口耗尽。
6. **应用**：监听地址、协议、TLS、超时、限流。

每次只验证一层。先 `curl` Pod IP，再 `curl` Service IP，再 `curl` 域名；顺序颠倒会把 DNS 问题误判成网络问题。

## O.2 工具地图

| 层 | 工具 | 能看到什么 |
|---|---|---|
| 名字 | `dig`、`getent`、`nslookup`、CoreDNS 日志 | 解析结果、search 域、上游失败 |
| 套接字 | `ss`、`netstat`、`lsof` | 监听、连接状态、队列、重传 |
| 策略 | `kubectl get networkpolicy`、CNI 工具 | 允许/拒绝、命中规则 |
| 转发 | `iptables-save`、`nft list ruleset`、`ipvsadm` | Service/NodePort 规则、计数 |
| 连接跟踪 | `conntrack`、`/proc/net/nf_conntrack` | DNAT/SNAT、超时、表满 |
| 路由 | `ip route`、`ip rule`、`ip route get` | 下一跳、策略路由、VRF |
| 链路 | `ip -br link`、`ethtool`、`bridge` | veth、MTU、丢包、offload |
| 包 | `tcpdump`、`tshark` | 握手、RST、重传、ICMP |
| 内核 | `nstat`、`ss -ti`、`bpftrace`、`perf` | 重传、丢包、软中断、延迟 |
| eBPF CNI | `hubble observe`、`cilium monitor`、`bpftool` | 策略 verdict、Service 转换 |

## O.3 进入正确的命名空间

很多“网络问题”其实是**在错误的 netns 里看**。Pod 的网卡不在节点默认命名空间。

### 在 Pod 内取证

```bash
# 优先用镜像自带的工具；没有就注入临时容器
kubectl debug -it <pod> --image=nicolaka/netshoot --target=<container>

# 容器内
ip -br addr
ip route
cat /etc/resolv.conf
ss -lntup
```

`--target` 让临时容器共享目标容器的进程命名空间；网络命名空间本来就在 Pod 级共享。若集群禁止临时容器，可在节点上进入：

```bash
crictl ps
crictl inspect <container-id> | jq '.info.pid'
nsenter -t <pid> -n -m -u -i -p -- ip -br addr
```

### 在节点上取证

```bash
# 调试节点：host 文件系统挂到 /host
kubectl debug node/<node> -it --image=nicolaka/netshoot

# 进入节点默认命名空间
nsenter -t 1 -m -u -i -n -p -- bash
```

注意：节点级调试权限很高，生产环境要走审批与审计。用完即删。

## O.4 DNS 取证

### 先看 resolv.conf

```bash
kubectl exec <pod> -- cat /etc/resolv.conf
```

典型内容：

```text
search default.svc.cluster.local svc.cluster.local cluster.local
nameserver 10.96.0.10
options ndots:5
```

`ndots:5` 意味着：查询名中点数少于 5 时，先依次拼接 search 域。`api.example.com` 有 2 个点，会先尝试 `api.example.com.default.svc.cluster.local` 等，再查绝对名。这会放大 DNS 查询量和延迟，也是“外部域名解析慢”的常见原因。

### 分层验证

```bash
# 1. 服务名是否解析
kubectl exec <pod> -- getent hosts <service>
kubectl exec <pod> -- dig +search <service>

# 2. 绝对名是否解析
kubectl exec <pod> -- dig <service>.<namespace>.svc.cluster.local

# 3. CoreDNS 是否健康
kubectl -n kube-system get pods -l k8s-app=kube-dns -o wide
kubectl -n kube-system logs -l k8s-app=kube-dns --tail=100

# 4. 53 端口是否可达（策略/网络）
kubectl exec <pod> -- dig @10.96.0.10 <service>

# 5. 在 Pod 的 netns 抓 DNS
kubectl exec <pod> -- tcpdump -ni any -vv port 53
```

### 常见结论

- 解析到 `NXDOMAIN`：名字/namespace 错，或 Service 不存在。
- 解析到 ClusterIP 但连接失败：进入 Service 层。
- 只有短名失败、FQDN 成功：search/ndots 或 DNS 策略。
- `dig @10.96.0.10` 成功、默认 resolver 失败：resolv.conf 或 dnsPolicy。
- 间歇超时：CoreDNS 负载、上游 DNS、conntrack、UDP 丢包。

`dnsPolicy` 的选择见第 13 章：`ClusterFirst` 是默认，`Default` 继承节点，`None` 完全自定义，`ClusterFirstWithHostNet` 用于 hostNetwork Pod。

## O.5 Service 与 conntrack

### 先确认 Endpoints

```bash
kubectl get svc <svc> -o wide
kubectl get endpointslice -l kubernetes.io/service-name=<svc> -o wide
kubectl get endpoints <svc> -o yaml   # 兼容旧工具
```

Endpoints 为空时，不要抓包：先查 selector、Pod Ready、targetPort。

### 再看规则

iptables 模式：

```bash
iptables-save | grep -E 'KUBE-(SERVICES|SVC|SEP|NODEPORTS)' | head -50
iptables -t nat -L KUBE-SERVICES -n -v
```

规则链的典型结构：

```text
KUBE-SERVICES → KUBE-SVC-<hash> → KUBE-SEP-<hash> (DNAT 到 PodIP:targetPort)
```

IPVS 模式：

```bash
ipvsadm -Ln
ipvsadm -Ln --stats
```

nftables 模式：

```bash
nft list ruleset | grep -A20 'kube-proxy'
```

eBPF 模式（Cilium 等）：没有 KUBE-SVC 规则，查 CNI 自己的 map/命令：

```bash
cilium service list
cilium bpf lb list
hubble observe --pod <namespace>/<pod> --verdict DROPPED
```

### conntrack

```bash
# 查看某条连接的双向 NAT
conntrack -L -p tcp --dport 80
conntrack -L | grep <pod-ip>

# 实时观察新建/销毁
conntrack -E -p tcp --dport 80

# 表容量与丢包
sysctl net.netfilter.nf_conntrack_count net.netfilter.nf_conntrack_max
dmesg | grep -i conntrack
```

`nf_conntrack: table full, dropping packet` 会表现为随机超时/连接失败，尤其在高并发短连接场景。扩容表要同时评估内存；更好的做法是减少短连接、启用 keep-alive、检查应用连接池。

### 一个经典陷阱：Pod 访问自身 Service

Pod 通过 Service 访问自己时，流量可能被 DNAT 回同一 Pod，形成 hairpin。不同 CNI/kube-proxy 模式对 hairpin masquerade 的处理不同，表现为“有时通、有时不通”。验证方法：直接访问 Pod IP 对比 Service IP，并在两端抓包看源地址。

## O.6 抓包：抓哪里、抓什么

### 抓包位置

| 位置 | 命令 | 能看到 |
|---|---|---|
| Pod netns | `kubectl exec ... -- tcpdump -ni any` | 应用视角的包 |
| 节点默认 ns | `tcpdump -ni any` | veth、overlay、物理网卡 |
| 物理网卡 | `tcpdump -ni eth0` | underlay/封装后的包 |
| 网关/云 LB | 云流量镜像/VPC Flow Logs | 跨 VPC、LB 健康检查 |

同一连接在不同位置看到的源/目的地址不同（NAT、封装），要带着拓扑看。

### 安全的抓包方式

```bash
# 只抓头部，避免把业务数据落盘
tcpdump -ni any -s 96 -nn 'tcp port 8080'

# 环形缓冲，限制磁盘
tcpdump -ni any -s 96 -C 100 -W 5 -w /tmp/cap.pcap 'host 10.0.0.5'

# 只看握手/重置
tcpdump -ni any -nn 'tcp[tcpflags] & (tcp-syn|tcp-rst|tcp-fin) != 0'

# 看 ICMP（MTU/不可达）
tcpdump -ni any -nn icmp
```

生产注意：

- 抓包有 CPU/磁盘成本，限制 snaplen、过滤器和时长。
- pcap 可能含凭据/token，按敏感数据管理，用完删除。
- 在容器里抓包需要 `CAP_NET_RAW`；受限 Pod 用临时容器或节点侧抓。

### 读包顺序

1. SYN 发出去了吗？没有 → 路由/策略/应用没发起。
2. 有 SYN-ACK 吗？没有 → 对端没收到、被策略拒绝、端口没监听。
3. 有 ACK 吗？没有 → 回程路径/NAT 问题。
4. 有 RST 吗？→ 端口关闭、连接被拒、LB 主动断开。
5. 有重传/dup ACK 吗？→ 丢包、MTU、拥塞。
6. 有 ICMP Fragmentation Needed 吗？→ PMTUD/MTU。

## O.7 Overlay、MTU 与重传

### 封装开销

| 封装 | 额外开销（约） | 1500 MTU 下建议 Pod MTU |
|---|---|---|
| VXLAN | 50 字节 | 1450 |
| Geneve | 50–70 字节 | 1450 或更低 |
| IPIP | 20 字节 | 1480 |
| WireGuard | 60–80 字节 | 1420 左右 |

MTU 不匹配的典型症状：小请求正常，大响应卡住；`ping` 小包通、大包不通；TLS 握手在证书较大时失败。

```bash
# 在 Pod 内测试 DF（禁止分片）：1472 = 1500 - 20(IP) - 8(ICMP)
ping -M do -s 1472 <peer-ip>

# 逐步减小直到通，估算路径 MTU
ping -M do -s 1400 <peer-ip>

# 查看接口 MTU
ip link show
```

修复方向：统一 CNI/节点 MTU；云网络对封装的支持；避免在应用层发送超过路径 MTU 的 UDP 包。

### 重传与队列

```bash
ss -ti dst <peer-ip>
nstat -az | grep -E 'TcpRetrans|TcpExtTCPLostRetransmit|TcpExtListenOverflows|TcpExtListenDrops'
netstat -s | grep -E 'retrans|overflow|drop'
```

- `TcpRetransSegs` 持续增长：丢包/MTU/链路问题。
- `ListenOverflows`/`ListenDrops`：accept 队列满，应用处理不过来。
- `ss -lnt` 的 `Recv-Q`/`Send-Q`：监听队列与发送缓冲。
- `ss -s`：连接数总览。

## O.8 NetworkPolicy 判定

NetworkPolicy 是**允许列表**：一旦某个 Pod 被某条策略选中，未被允许的方向默认拒绝。没有策略选中时默认全通。

快速判定：

```bash
kubectl get networkpolicy -n <ns>
kubectl describe networkpolicy <name> -n <ns>
```

用标签模拟选择器：

```bash
kubectl get pods -n <ns> --show-labels
kubectl get ns --show-labels
```

常见错误：

- 只写了 ingress，忘了 egress 默认拒绝后 DNS 不通。
- `namespaceSelector` 与 `podSelector` 写在同一个 `from` 元素里是 AND；分开写是 OR。
- 空 `podSelector: {}` 选中 namespace 内所有 Pod。
- 策略只作用于被选中的 Pod 的**入站/出站方向**，不作用于“谁访问谁”的另一端。
- 云安全组/节点防火墙在 CNI 之外，策略放行不代表链路放行。

CNI 工具：

```bash
# Cilium
hubble observe --namespace <ns> --verdict DROPPED --last 50
cilium policy trace --src-k8s-pod <ns>/<pod> --dst-k8s-pod <ns>/<pod> --dport 80

# Calico
calicoctl get networkpolicy -n <ns> -o yaml
kubectl -n kube-system logs -l k8s-app=calico-node --tail=100
```

## O.9 六个案例

### 案例 1：Service 有 Endpoints，但连接被拒

症状：`curl <clusterIP>:80` 立即 `Connection refused`。

排查：

```bash
kubectl get endpointslice -l kubernetes.io/service-name=web -o yaml
kubectl exec <client> -- curl -v http://<pod-ip>:<targetPort>
kubectl exec <server> -- ss -lntp
```

结论通常是 `targetPort` 与容器实际监听端口不一致，或应用只监听 `127.0.0.1`。Service 的 `port` 是虚拟端口，`targetPort` 才是容器端口。

### 案例 2：只有跨节点失败

症状：同节点 Pod 互访正常，跨节点超时。

排查：

```bash
# 两端 Pod IP 与所在节点
kubectl get pod -o wide
# 节点上抓封装包
tcpdump -ni any -nn 'udp port 4789 or udp port 6081'
# 检查路由与 CNI 状态
ip route
kubectl -n kube-system get pods -o wide | grep -E 'calico|cilium|flannel'
```

常见根因：overlay 未建立、VXLAN 端口被防火墙拦截、Pod CIDR 路由缺失、MTU 不匹配、云安全组未放行封装协议。

### 案例 3：高并发下随机超时

症状：低负载正常，高负载偶发超时；`dmesg` 有 conntrack 表满。

排查：

```bash
sysctl net.netfilter.nf_conntrack_count net.netfilter.nf_conntrack_max
conntrack -S
ss -s
nstat -az | grep -E 'TcpRetrans|ListenOverflows'
```

处理：扩大 conntrack 表、缩短 TIME_WAIT、启用连接池/keep-alive、减少 NAT 跳数、评估 IPVS/eBPF 模式。

### 案例 4：DNS 间歇失败

症状：应用偶发 `UnknownHostException`，CoreDNS 日志有超时。

排查：

```bash
kubectl -n kube-system top pod -l k8s-app=kube-dns
kubectl -n kube-system logs -l k8s-app=kube-dns --tail=200
kubectl exec <pod> -- cat /etc/resolv.conf
kubectl exec <pod> -- tcpdump -ni any port 53
```

常见根因：CoreDNS 副本不足/被限流、上游 DNS 慢、ndots 导致查询放大、UDP 丢包、NetworkPolicy 拦 53、节点 conntrack 满。

### 案例 5：Ingress 502/504

症状：外部访问 Ingress 返回 502（bad gateway）或 504（timeout）。

排查：

```bash
kubectl -n ingress-nginx logs <controller-pod> --tail=200
kubectl get ingress <name> -o yaml
kubectl get endpointslice -l kubernetes.io/service-name=<backend>
kubectl exec <controller-pod> -- curl -v http://<backend-pod-ip>:<port>
```

502 多为后端拒绝/协议不匹配/后端提前断开；504 多为后端超时、keepalive 配置、上游地址不可达。检查 `proxy-read-timeout`、`proxy-body-size`、TLS 后端、以及 Ingress 到 Pod 的 NetworkPolicy。

### 案例 6：NodePort 本地通、外部不通

症状：节点上 `curl <nodeIP>:<nodePort>` 通，外部客户端不通。

排查：

```bash
ss -lntp | grep <nodePort>
iptables-save | grep KUBE-NODEPORTS
kubectl get svc <svc> -o yaml   # externalTrafficPolicy
# 云安全组/防火墙是否放行 NodePort 范围
```

`externalTrafficPolicy: Local` 只把流量转到本节点有 Endpoint 的 Pod；没有本地 Endpoint 的节点会拒绝。跨节点访问还要看 LB/安全组/路由。

## O.10 抓包与取证的安全边界

- 只在授权范围内抓包，遵守数据合规与隐私要求。
- 优先抓头部（`-s 96`），避免落盘业务载荷。
- pcap 文件按敏感数据加密存储，设置保留期。
- 节点调试容器权限高，使用后立即删除，并记录审计。
- 不要把生产抓包文件直接贴到工单/聊天；先脱敏。
- 大规模抓包可能触发内核/网卡丢包，必要时用镜像端口或 eBPF 聚合。

## O.11 一页命令卡

```bash
# 身份与命名空间
kubectl get pod -o wide
crictl ps
crictl inspect <id> | jq '.info.pid'
nsenter -t <pid> -n -m -u -i -p -- bash

# 名字
cat /etc/resolv.conf
getent hosts <svc>
dig +search <svc>
dig @<dns-ip> <fqdn>

# 监听与连接
ss -lntup
ss -ti dst <ip>
ss -s

# 策略
kubectl get networkpolicy -A
hubble observe --verdict DROPPED --last 50

# 转发
iptables-save | grep KUBE-
ipvsadm -Ln
nft list ruleset | grep -A20 kube-proxy
conntrack -L -p tcp --dport <port>

# 路由与链路
ip -br addr
ip route get <ip>
ip link show
ping -M do -s 1472 <ip>

# 包
tcpdump -ni any -s 96 -nn 'host <ip> and tcp port <port>'
tcpdump -ni any -nn 'tcp[tcpflags] & (tcp-syn|tcp-rst|tcp-fin) != 0'

# 内核计数
nstat -az | grep -E 'TcpRetrans|ListenOverflows|IPInHdrErrors'
dmesg | grep -iE 'conntrack|nf_|vxlan|mtu'
```

## 延伸阅读

- [第 13 章 Service、Endpoints 与 DNS](../chapters/13-service-endpoints-dns.md)：Service 语义、kube-proxy 模式与 DNS 策略。
- [第 15 章 CNI、kube-proxy、eBPF 与 NetworkPolicy](../chapters/15-cni-ebpf-networkpolicy.md)：数据面与策略模型。
- [第 31 章 故障排查方法论](../chapters/31-troubleshooting.md)：分层排障与事故处理。
- [附录 C 故障排查清单](C-troubleshooting-checklist.md)：命令级检查表。
- [附录 M 生产事故案例库](M-incident-casebook.md)：网络类事故的完整复盘。
