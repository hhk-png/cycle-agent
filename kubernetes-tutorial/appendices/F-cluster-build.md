# 附录 F 集群搭建与生产化手册

> 本附录给出从本地实验到生产高可用集群的搭建路径。命令以 Linux + kubeadm 为主，版本以 v1.31 为基线；不同发行版、云厂商和 CNI/CSI 的细节会不同，执行前请核对官方文档。
>
> **v1.31 已结束社区维护（2025-10-28）**。下面用 v1.31 是为了与正文示例一致、便于复现；新建生产集群请改用当前受支持的 minor，并核对 CNI/CSI/运行时的兼容矩阵。跨多个 minor 升级必须逐个小版本进行，不能从 1.31 直接跳到最新版。

## F.1 先选形态

| 形态 | 适用 | 优点 | 代价 |
|---|---|---|---|
| kind | 本地/CI、学习/测试 | 启动快、可重复、无需云 | 节点是容器，非生产 |
| minikube | 单机学习/插件实验 | addon 丰富、简单 | 单节点、非生产 |
| k3s | 边缘/开发/资源受限 | 单二进制、依赖少 | 默认 SQLite、生态差异 |
| kubeadm | 自建生产/理解控制面 | 标准、可控、可 HA | 需要自己运维 |
| 托管 K8s | 大多数生产 | 控制面 SLA、省运维 | 成本/锁定/云绑定 |

学习建议：kind 做实验，kubeadm 理解生产，托管服务做真实业务。三者不冲突。

## F.2 kind：可重复的实验集群

安装：

```bash
# 需要 Docker 或 Podman
go install sigs.k8s.io/kind@latest
kind version
```

创建带 Ingress 端口映射的集群：

```yaml
# kind-config.yaml
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
nodes:
  - role: control-plane
    kubeadmConfigPatches:
      - |
        kind: InitConfiguration
        nodeRegistration:
          kubeletExtraArgs:
            node-labels: "ingress-ready=true"
    extraPortMappings:
      - containerPort: 80
        hostPort: 8080
        protocol: TCP
      - containerPort: 443
        hostPort: 8443
        protocol: TCP
  - role: worker
  - role: worker
```

```bash
kind create cluster --name lab --config kind-config.yaml
kubectl cluster-info --context kind-lab
kubectl get nodes -o wide
```

加载本地镜像（避免每次拉取）：

```bash
docker build -t app:dev .
kind load docker-image app:dev --name lab
```

删除：

```bash
kind delete cluster --name lab
```

kind 的节点是 Docker 容器，不能模拟多 AZ、云 LB、真实云盘；它适合验证 API/控制器/Ingress/NetworkPolicy 的基本行为。

## F.3 kubeadm 生产集群：前置条件

每个节点：

- Linux 内核 4.x+（生产建议 5.10+），cgroup v2 可用。
- 2 CPU / 2GiB 以上（控制面建议 4 CPU / 8GiB）。
- 唯一 hostname、MAC、product_uuid。
- 时间同步（chrony/ntpd）。
- 关闭 swap（或明确配置 kubelet 的 swap 支持）。
- 防火墙放行：6443、2379–2380、10250、10257、10259、NodePort 范围、CNI 端口（如 VXLAN 8472、BGP 179）。
- 安装 containerd（或 CRI-O），配置 systemd cgroup driver。
- 安装 kubeadm/kubelet/kubectl，并 `apt-mark hold` 锁定版本。

containerd 最小配置：

```toml
# /etc/containerd/config.toml
version = 2
[plugins."io.containerd.grpc.v1.cri".containerd.runtimes.runc.options]
  SystemdCgroup = true
```

```bash
systemctl restart containerd
```

内核模块与参数：

```bash
cat <<'EOF' | sudo tee /etc/modules-load.d/k8s.conf
overlay
br_netfilter
EOF

cat <<'EOF' | sudo tee /etc/sysctl.d/k8s.conf
net.bridge.bridge-nf-call-iptables = 1
net.bridge.bridge-nf-call-ip6tables = 1
net.ipv4.ip_forward = 1
EOF

sudo sysctl --system
```

## F.4 高可用控制面拓扑

```text
                    ┌──────────────┐
    kubectl/CI ────▶│ L4 LB / VIP  │
                    │ 6443         │
                    └──────┬───────┘
          ┌────────────────┼────────────────┐
          ▼                ▼                ▼
   ┌────────────┐   ┌────────────┐   ┌────────────┐
   │ cp-1       │   │ cp-2       │   │ cp-3       │
   │ apiserver  │   │ apiserver  │   │ apiserver  │
   │ etcd       │   │ etcd       │   │ etcd       │
   │ controller │   │ controller │   │ controller │
   │ scheduler  │   │ scheduler  │   │ scheduler  │
   └────────────┘   └────────────┘   └────────────┘
```

要点：

- etcd 用 3 或 5 个成员，奇数；跨 AZ 时关注 Raft 延迟。
- API Server 前置 L4 LB，健康检查 `/readyz`；证书 SAN 包含 VIP/域名。
- controller-manager/scheduler 多副本，通过 Lease 选主。
- 控制面节点加 taint，避免业务 Pod 占用资源。
- 生产建议 etcd 使用独立磁盘，避免与控制面日志/镜像竞争 I/O。

## F.5 kubeadm init 与 join

第一个控制面：

```bash
sudo kubeadm init \
  --control-plane-endpoint "k8s-api.example.com:6443" \
  --pod-network-cidr "10.244.0.0/16" \
  --service-cidr "10.96.0.0/12" \
  --upload-certs \
  --apiserver-advertise-address "10.0.0.11"
```

记录输出中的：

- `kubeadm join ... --control-plane --certificate-key ...`
- `kubeadm join ... --worker`

配置 kubectl：

```bash
mkdir -p $HOME/.kube
sudo cp /etc/kubernetes/admin.conf $HOME/.kube/config
sudo chown $(id -u):$(id -g) $HOME/.kube/config
```

安装 CNI（示例：Calico）：

```bash
kubectl apply -f https://raw.githubusercontent.com/projectcalico/calico/v3.28.0/manifests/calico.yaml
```

或 Cilium：

```bash
helm repo add cilium https://helm.cilium.io/
helm install cilium cilium/cilium --namespace kube-system \
  --set k8sServiceHost=k8s-api.example.com \
  --set k8sServicePort=6443
```

加入其余控制面/工作节点，按 init 输出执行。加入后：

```bash
kubectl get nodes -o wide
kubectl -n kube-system get pods
```

## F.6 证书与 TLS Bootstrap

kubeadm 生成的证书在 `/etc/kubernetes/pki`，默认有效期约 1 年（可配置）。检查：

```bash
sudo kubeadm certs check-expiration
sudo kubeadm certs renew all
```

kubelet 客户端证书通常通过 TLS bootstrap 自动签发：

```yaml
# kubelet 配置片段
apiVersion: kubelet.config.k8s.io/v1beta1
kind: KubeletConfiguration
authentication:
  x509:
    clientCAFile: /etc/kubernetes/pki/ca.crt
  anonymous:
    enabled: false
authorization:
  mode: Webhook
rotateCertificates: true
```

对应的 RBAC 允许 kubelet 创建 CSR，并由 controller-manager 自动批准 `system:node` 相关请求。禁用匿名认证、启用 Webhook 授权是节点安全基线。

### PKI 层级与文件

kubeadm 集群的证书不是一堆平级文件，而是一棵有明确用途的树：

| 文件 | 用途 | 谁签 | 过期影响 |
|---|---|---|---|
| `ca.crt/key` | 集群根 CA | 自签 | 所有客户端/服务端信任失效 |
| `apiserver.crt/key` | API Server 服务端证书 | 集群 CA | kubectl/LB 无法验证 API Server |
| `apiserver-kubelet-client.crt/key` | API Server → kubelet 客户端 | 集群 CA | logs/exec/port-forward 失败 |
| `front-proxy-ca.crt/key` | 聚合层（APIService）CA | 自签 | metrics-server、聚合 API 失败 |
| `front-proxy-client.crt/key` | API Server → 聚合层客户端 | front-proxy CA | 聚合 API 502/证书错误 |
| `sa.key/sa.pub` | ServiceAccount token 签名 | 自签 | 旧版 token 校验失败；BoundToken 也依赖 |
| `etcd/ca.crt/key` | etcd 专用 CA | 自签 | etcd 集群无法建立 |
| `etcd/server.crt/key` | etcd 服务端 | etcd CA | apiserver 无法连 etcd |
| `etcd/peer.crt/key` | etcd 节点间 | etcd CA | etcd 无法选主/复制 |
| `etcd/healthcheck-client.crt/key` | etcd 健康检查 | etcd CA | 探针失败 |
| `apiserver-etcd-client.crt/key` | API Server → etcd | etcd CA | apiserver 无法读写 |

要点：

- **服务端证书的 SAN 必须覆盖所有访问入口**：每个控制面节点 IP、VIP/LB DNS、`kubernetes`、`kubernetes.default`、`kubernetes.default.svc`、`kubernetes.default.svc.cluster.local`。用 `kubeadm init --control-plane-endpoint` 固定入口，避免扩容后证书不匹配。
- **CA 私钥是最高敏感资产**：能签出任意身份；生产应离线保管或使用外部 CA/KMS，不要放在可被工作负载挂载的节点目录。
- **`sa.key` 泄漏等于可以伪造 ServiceAccount token**（在未启用 BoundToken 校验的项目/场景）；按密钥管理。

### 证书轮换

```bash
# 检查所有证书到期时间
sudo kubeadm certs check-expiration

# 续期全部（不会续 CA）
sudo kubeadm certs renew all

# 续期后重启静态 Pod（kubeadm 会提示）
sudo systemctl restart kubelet
```

- `kubeadm certs renew` **不轮换 CA**。CA 轮换需要重新签发所有证书并分发新 CA，属于高风险变更，需专门演练。
- 托管集群的控制面证书由厂商负责；节点 kubelet 证书仍可能由你维护。
- 升级 kubeadm 集群时，`kubeadm upgrade apply` 会自动续期由 kubeadm 管理的证书；仍要检查剩余天数。
- 监控建议：把证书到期时间导出为指标（如 `kubeadm certs check-expiration -o json` + textfile collector），至少提前 30/7 天告警。

### CSR API：把签发变成工作流

除了 kubeadm 的静态证书，Kubernetes 提供 `CertificateSigningRequest`：

```yaml
apiVersion: certificates.k8s.io/v1
kind: CertificateSigningRequest
metadata:
  name: alice-client
spec:
  request: <base64(PEM-encoded PKCS#10 CSR)>
  signerName: kubernetes.io/kube-apiserver-client
  usages:
    - client auth
```

流程：

```bash
# 生成私钥与 PEM 格式 CSR（不要用 -outform DER）
openssl genrsa -out alice.key 2048
openssl req -new -key alice.key -subj "/CN=alice/O=developers" -out alice.csr
openssl req -in alice.csr -noout -subject   # 确认 CN/O

# spec.request 是 base64(PEM CSR)，先得到单行 base64
base64 -w0 alice.csr > alice.csr.b64        # macOS: base64 -i alice.csr | tr -d '\n'

# 把 base64 填进 alice-csr.yaml 的 spec.request，然后提交
kubectl create -f alice-csr.yaml
# 审批/拒绝
kubectl certificate approve alice-client
kubectl certificate deny alice-client
# 取出签好的证书（status.certificate 本身就是 base64(PEM)）
kubectl get csr alice-client -o jsonpath='{.status.certificate}' | base64 -d > alice.crt
```

常见 `signerName`：

- `kubernetes.io/kube-apiserver-client`：普通客户端证书。
- `kubernetes.io/kube-apiserver-client-kubelet`：kubelet 客户端（TLS bootstrap）。
- `kubernetes.io/kubelet-serving`：kubelet 服务端证书。
- `kubernetes.io/legacy-unknown`：兼容旧工具，慎用。

审批是安全边界：不要把 `system:masters` 或宽泛的 CSR 审批权限给普通用户；`signerName` 与 `usages` 要校验。

### cert-manager：工作负载与 webhook 证书

kubeadm 管的是控制面 PKI；Ingress、webhook、Operator 的证书通常交给 cert-manager：

```yaml
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: letsencrypt-prod
spec:
  acme:
    server: https://acme-v02.api.letsencrypt.org/directory
    email: platform@example.com
    privateKeySecretRef:
      name: letsencrypt-prod
    solvers:
      - http01:
          ingress:
            class: nginx
```

- `Certificate` 资源声明 DNS 名、`secretName`、`renewBefore`；cert-manager 自动续期并更新 Secret。
- webhook 常用 CA Injector 把 CA bundle 注入 `ValidatingWebhookConfiguration`。
- 私有环境可用自建 CA Issuer 或 Vault Issuer。
- 证书更新后，Ingress Controller/webhook 需要重载；确认组件监听 Secret 变化。

### 证书类故障的症状

| 症状 | 可能原因 |
|---|---|
| `x509: certificate has expired or is not yet valid` | 证书过期/节点时间漂移 |
| `x509: certificate signed by unknown authority` | CA 不匹配、客户端未带 CA |
| `remote error: tls: bad certificate` | SAN 不含访问地址、证书链不完整 |
| `Unable to connect to the server: EOF` | apiserver 证书/端口/LB 问题 |
| `logs/exec` 失败但 `get pod` 正常 | apiserver → kubelet 客户端证书问题 |
| webhook 调用失败 | webhook 证书过期、CA bundle 未注入、SAN 不匹配 |

## F.7 网络与存储规划

### CIDR

```text
节点网络：10.0.0.0/24
Pod CIDR：10.244.0.0/16（每节点 /24）
Service CIDR：10.96.0.0/12
```

规划原则：

- Pod/Service CIDR 不要与节点网络、VPC、办公网重叠。
- 预留足够 IP：Pod 密度 × 节点数 × 增长。
- 云厂商 VPC-native CNI 会消耗 VPC IP，需要大子网。
- 双栈要同时规划 IPv4/IPv6，并确认 CNI/负载均衡支持。

### 存储

- 云上：安装对应 CSI 驱动（EBS/PD/云盘），设置默认 StorageClass。
- 自建：Rook/Ceph、NFS、Local PV；生产数据库优先用云盘/专用存储。
- 默认 StorageClass 只能有一个；`WaitForFirstConsumer` 用于拓扑绑定。
- 快照/备份（Velero/CSI snapshot）单独规划。

## F.8 必备附加组件

| 组件 | 作用 | 备注 |
|---|---|---|
| CoreDNS | 集群 DNS | 默认安装，至少 2 副本 |
| metrics-server | 资源指标/HPA | 需要 TLS 配置 |
| CNI | Pod 网络/策略 | Calico/Cilium/Antrea |
| CSI | 持久卷 | 云厂商/自建 |
| Ingress/Gateway Controller | 南北向流量 | NGINX/Envoy Gateway |
| cert-manager | 证书签发/轮换 | 与 Ingress/webhook 配合 |
| Policy Engine | 准入策略 | Kyverno/Gatekeeper/VAP |
| 可观测 | Prometheus/Grafana/Loki/OTel | 独立故障域 |
| 备份 | Velero | 对象 + 卷 |

安装顺序建议：CNI → CoreDNS 验证 → metrics-server → CSI → Ingress/Gateway → cert-manager → 策略 → 可观测 → 备份。

## F.9 安装后验收

```bash
kubectl get nodes -o wide
kubectl get pods -A
kubectl run smoke --image=nginx:1.27 --port=80
kubectl expose pod smoke --port=80
kubectl run curl --image=curlimages/curl --rm -it --restart=Never -- curl -s http://smoke
kubectl delete pod smoke svc/smoke

kubectl get --raw='/readyz?verbose'
kubectl get --raw='/livez?verbose'
kubectl auth can-i '*' '*' --as=system:serviceaccount:default:default
```

检查：

- Node Ready，系统 Pod 全部 Running/Ready。
- DNS 解析、Service 访问、Ingress 访问。
- 默认 StorageClass 能动态供给。
- NetworkPolicy 是否生效（取决于 CNI）。
- 证书有效期、etcd 健康、API 延迟。
- 审计/日志/监控是否开始采集。

## F.10 常见安装故障

| 症状 | 常见原因 | 排查 |
|---|---|---|
| kubelet 无法启动 | cgroup driver 不一致、swap、运行时未启动 | `journalctl -u kubelet`、`crictl info` |
| 节点 NotReady | CNI 未安装/未就绪 | `kubectl describe node`、CNI Pod 日志 |
| Pod 一直 ContainerCreating | CNI/IPAM/卷挂载失败 | 事件、`crictl`、CNI 日志 |
| API Server 起不来 | 证书 SAN、etcd 地址、端口占用 | `crictl logs`、静态 Pod 日志 |
| join 失败 | token 过期、证书 key 错误、时间不同步 | 重新生成 token、检查 chrony |
| DNS 不通 | CoreDNS 未 Ready、NetworkPolicy、iptables | `kubectl -n kube-system logs`、`nslookup` |
| 存储 PVC Pending | 无默认 StorageClass、拓扑、容量 | `describe pvc`、CSI 日志 |

安装阶段最常见的根因不是 Kubernetes 本身，而是 **容器运行时、cgroup、CNI、证书、时间同步**。先把这五项做成检查脚本，再谈业务部署。

## F.11 生产化检查清单

- 控制面 3 副本、etcd 3/5 节点、跨 AZ。
- API Server 证书 SAN 完整，LB 健康检查正确。
- 审计日志、静态加密、OIDC/RBAC 最小权限。
- kubelet 匿名认证关闭、Webhook 授权、证书轮转。
- CNI/CSI/Ingress/策略/可观测/备份全部高可用。
- 节点池按工作负载隔离，资源预留/驱逐阈值配置。
- 升级/备份/恢复/故障演练有 runbook 并演练过。
- 成本标签、配额、告警、容量规划有负责人。

## 延伸阅读

- [第 2 章 架构总览](../chapters/02-architecture.md)
- [第 3 章 实验环境与快速开始](../chapters/03-lab-and-quickstart.md)
- [第 28 章 高可用与灾难恢复](../chapters/28-ha-disaster-recovery.md)
- [第 29 章 升级与节点运维](../chapters/29-upgrade-node-ops.md)
- [附录 H 安全加固基线](H-security-baseline.md)
