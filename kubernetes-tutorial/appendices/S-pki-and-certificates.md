# 附录 S PKI、证书与身份生命周期

Kubernetes 的很多“神秘故障”最终都落在证书上：API Server 起不来、kubelet NotReady、`kubectl` 报 `x509: certificate has expired`、聚合 API 503、ServiceAccount token 被拒绝。本附录把集群 PKI 拆成可检查的对象和流程，而不是一张“证书清单”。

## S.1 四套信任关系

一个典型集群至少有四套相互独立、又彼此校验的信任关系：

| 信任域 | 参与方 | 用途 |
|---|---|---|
| 控制面 CA | apiserver、controller-manager、scheduler、admin、kubelet 客户端 | 组件与用户对 apiserver 的 mTLS |
| kubelet 服务端 CA | apiserver → kubelet | apiserver 代理 logs/exec/port-forward 时校验 kubelet |
| etcd CA | apiserver ↔ etcd、etcd peer | etcd 客户端与服务端 mTLS |
| 聚合层 CA | apiserver ↔ 聚合 apiserver | APIService 的 front-proxy 信任 |
| ServiceAccount 密钥 | apiserver 签发、apiserver 校验 | Pod 内 token 的身份 |

“四套”不是绝对：kubeadm 默认把控制面与 kubelet 客户端放在同一个 `ca.crt` 下，但 etcd、front-proxy 通常各有自己的 CA。生产集群可以把它们拆得更细，也可以统一到一个受控的 CA 体系；关键是**知道哪张证书被谁校验**。

## S.2 一张证书地图

kubeadm 默认目录：

```text
/etc/kubernetes/pki/
├── ca.crt / ca.key                         # 控制面 CA
├── apiserver.crt / apiserver.key           # apiserver serving
├── apiserver-kubelet-client.crt/.key       # apiserver → kubelet 客户端
├── front-proxy-ca.crt/.key                 # 聚合层 CA
├── front-proxy-client.crt/.key             # apiserver → 聚合 apiserver
├── etcd/
│   ├── ca.crt / ca.key
│   ├── server.crt / server.key
│   ├── peer.crt / peer.key
│   └── healthcheck-client.crt/.key
├── sa.key / sa.pub                         # ServiceAccount 签名密钥
└── apiserver-etcd-client.crt/.key          # apiserver → etcd
```

kubeconfig：

```text
/etc/kubernetes/admin.conf
/etc/kubernetes/controller-manager.conf
/etc/kubernetes/scheduler.conf
/etc/kubernetes/kubelet.conf
```

检查有效期：

```bash
sudo kubeadm certs check-expiry
sudo openssl x509 -in /etc/kubernetes/pki/apiserver.crt -noout -text | \
  grep -E "Not Before|Not After|Subject:|DNS:|IP Address:"
```

`kubeadm` 生成的 CA 默认 10 年，叶子证书默认 1 年（以版本为准）。托管控制面通常由云厂商轮换，用户只需要管理节点与工作负载证书。

## S.3 证书里到底有什么

一张 X.509 证书的身份由以下字段决定：

- **Subject CN**：在 Kubernetes 的客户端证书认证中，CN 是**用户名**。
- **Subject O**：组织字段被映射为**组**。例如 `O=system:masters` 直接获得集群管理员权限（绕过 RBAC）。
- **SAN**：服务端证书必须包含客户端使用的名字。apiserver 证书至少要覆盖：
  - `kubernetes`
  - `kubernetes.default`
  - `kubernetes.default.svc`
  - `kubernetes.default.svc.cluster.local`
  - Service CIDR 的第一个 IP（通常是 `10.96.0.1`）
  - 节点名、负载均衡器 DNS/IP
- **Key Usage / Extended Key Usage**：`serverAuth`、`clientAuth` 必须与用途匹配；拿 client 证书当 server 证书会握手失败。
- **签名链**：客户端必须信任签发者；自签或链不完整会报 `x509: certificate signed by unknown authority`，过期则报 `x509: certificate has expired or is not yet valid`。两者要分开排查。

一个常见的 SAN 事故：集群通过 `https://k8s.example.com:6443` 访问，但 apiserver 证书只签了节点 IP。`kubectl` 报：

```text
x509: certificate is valid for 10.0.0.10, not k8s.example.com
```

修复不是“跳过校验”（`--insecure-skip-tls-verify` 只应作为临时诊断），而是把域名加入 SAN 并重新签发。

## S.4 CertificateSigningRequest API

Kubernetes 把证书签发也做成了 API：`certificates.k8s.io/v1` 的 `CertificateSigningRequest`（CSR）。

```yaml
apiVersion: certificates.k8s.io/v1
kind: CertificateSigningRequest
metadata:
  name: alice
spec:
  request: LS0tLS1CRUdJTiBDRVJUSUZJQ0FURSBSRVFVRVNU...
  signerName: kubernetes.io/kube-apiserver-client
  usages:
    - client auth
```

流程：

```bash
# 1. 生成私钥与 CSR（CN=用户名，O=组）
openssl genrsa -out alice.key 2048
openssl req -new -key alice.key -subj "/CN=alice/O=developers" -out alice.csr

# 2. base64 后填入 spec.request
base64 -w0 alice.csr

# 3. 创建并批准
kubectl apply -f alice-csr.yaml
kubectl certificate approve alice

# 4. 取回证书
kubectl get csr alice -o jsonpath='{.status.certificate}' | base64 -d > alice.crt
```

常用 signer：

| signerName | 用途 |
|---|---|
| `kubernetes.io/kube-apiserver-client` | 用户/组件客户端证书 |
| `kubernetes.io/kube-apiserver-client-kubelet` | kubelet 加入集群的客户端证书 |
| `kubernetes.io/kubelet-serving` | kubelet 服务端证书 |
| `kubernetes.io/legacy-unknown` | 历史用途，签发者不保证行为 |

CSR 的 `status.conditions` 记录 `Approved`/`Denied`/`Failed`。**批准 CSR 等于签发身份**：任何能 `update certificatesigningrequests/approval` 的人都能造出一个集群用户。生产环境要限制该权限，或使用外部 signer（cert-manager、云 KMS、SPIFFE）而不是让人工随便批准。

## S.5 kubelet TLS bootstrapping

新节点加入时不应该手工拷贝长期证书。标准流程是 **TLS bootstrapping**：

```text
kubelet 启动
  │ 使用 bootstrap kubeconfig（内含 bootstrap token）
  ▼
apiserver 认证 bootstrap token
  │ kubelet 创建 CSR（signer: kubernetes.io/kube-apiserver-client-kubelet）
  ▼
controller-manager 的 csrapproving 控制器
  │ 校验 token 的 RBAC 是否允许该请求
  ▼
自动批准（或管理员手动批准）
  │ kubelet 取回证书，写入 --cert-dir 并生成 kubelet.conf
  ▼
后续轮换：--rotate-certificates，到期前重新申请
```

bootstrap token 存在 `kube-system` 的 Secret 中，格式为：

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: bootstrap-token-abcdef
  namespace: kube-system
type: bootstrap.kubernetes.io/token
stringData:
  token-id: abcdef
  token-secret: 0123456789abcdef
  usage-bootstrap-authentication: "true"
  usage-bootstrap-signing: "true"
  auth-extra-groups: system:bootstrappers:worker
```

自动批准依赖 RBAC：`system:bootstrappers` 组被绑定到允许创建特定 CSR 的 ClusterRole。不要让所有 bootstrap token 都能无限期使用；设置 `expiration` 并定期轮换。

kubelet 服务端证书（`kubernetes.io/kubelet-serving`）默认**不会**自动批准，因为批准它等于信任节点对外声称的身份。可以：

- 手工审核 CSR 的 SAN（节点名/IP）；
- 用 `kubelet-serving-cert-approver` 等控制器按节点名自动批准；
- 在受控网络中接受风险，但仍要监控异常 CSR。

## S.6 控制面证书轮换

kubeadm 集群的年度动作：

```bash
# 检查
sudo kubeadm certs check-expiry

# 续期全部叶子证书
sudo kubeadm certs renew all

# 重启静态 Pod 使新证书生效
sudo systemctl restart kubelet
# 或按发行版方式重启 apiserver/controller-manager/scheduler
```

注意：

- `renew all` **不会**续 CA；CA 过期需要更复杂的迁移（新旧 CA 交叉信任、逐组件切换）。
- 续期后 kubeconfig 里的客户端证书也要更新；`admin.conf` 等文件由 kubeadm 重写。
- 托管控制面通常由云厂商负责；自建集群要把证书到期纳入监控与变更日历。
- etcd 证书独立：`/etc/kubernetes/pki/etcd` 下的证书也要检查；etcd 起不来时 apiserver 会报 `connection refused` 或 `context deadline exceeded`。

证书过期的典型症状：

```text
Unable to connect to the server: x509: certificate has expired or is not yet valid
kubelet: Failed to run Kubelet: unable to load client CA file: ...
apiserver: http: TLS handshake error ... remote error: tls: bad certificate
```

排障顺序：先看**哪个组件对谁**握手失败，再看对应证书的 SAN/EKU/有效期，最后才怀疑网络。

## S.7 ServiceAccount token：从 Secret 到投影卷

历史版本中，创建 ServiceAccount 会自动生成一个长期 token Secret，并自动挂载到 Pod。现代 Kubernetes 默认使用 **bound service account token**：

- 由 kubelet 通过 TokenRequest API 申请；
- 有明确 audience、过期时间（默认 1 小时，可配置）；
- 绑定到 Pod/ServiceAccount，Pod 删除后 token 失效；
- 以投影卷（projected volume）挂载，路径通常为 `/var/run/secrets/kubernetes.io/serviceaccount/token`。

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: api-client
spec:
  serviceAccountName: api-client
  containers:
    - name: app
      image: example/api-client:1.0
      volumeMounts:
        - name: token
          mountPath: /var/run/secrets/kubernetes.io/serviceaccount
          readOnly: true
  volumes:
    - name: token
      projected:
        sources:
          - serviceAccountToken:
              path: token
              audience: https://kubernetes.default.svc
              expirationSeconds: 3600
```

要点：

- **audience 不是装饰**：apiserver 会校验 token 的 audience；给错误 audience 的 token 会被拒绝。云厂商 Workload Identity 也依赖 audience 做联邦。
- **`automountServiceAccountToken: false`**：不需要访问 API 的 Pod 应关闭自动挂载，减少凭据暴露面。
- **TokenReview**：外部服务可以用 `authentication.k8s.io/v1` 的 TokenReview 校验 token；但不应把集群 token 直接传给不可信服务。
- **OIDC discovery**：`/.well-known/openid-configuration` 与 JWKS 让外部系统验证 ServiceAccount token；托管集群通常提供 issuer URL。
- **镜像拉取凭据**：`imagePullSecrets` 是另一套凭据，不要和 ServiceAccount token 混为一谈。

## S.8 聚合层与 front-proxy

APIService 把请求反向代理到外部 apiserver（metrics-server、自定义 API）。这条链路使用 **front-proxy** 证书：

- apiserver 用 `front-proxy-client.crt` 作为客户端证书访问聚合 apiserver；
- 聚合 apiserver 用 `front-proxy-ca.crt` 校验 apiserver；
- apiserver 通过 `--requestheader-client-ca-file`、`--requestheader-allowed-names` 等参数信任聚合层传来的用户身份头。

常见故障：

- front-proxy CA 不匹配 → 聚合 API 503/`x509`；
- `--requestheader-allowed-names` 配错 → 用户身份头被忽略或拒绝；
- 聚合 apiserver 的服务证书 SAN 不包含 Service DNS → apiserver 校验失败。

聚合层的证书问题不会影响核心 API，但会让 `kubectl top`、自定义指标、部分 Operator 失效。

## S.9 工作负载身份与 Pod 证书

把长期证书塞进 Secret 是反模式。更现代的做法是**短期、可轮换、绑定身份**的凭据：

- **云 Workload Identity**：EKS IRSA、GKE Workload Identity、Azure AD Workload Identity 通过 projected token + OIDC 联邦，把 Kubernetes ServiceAccount 映射为云 IAM 身份。
- **SPIFFE/SPIRE**：用 `spiffe://` 身份签发短期 SVID 证书，服务网格（Istio/Linkerd）常内置类似机制。
- **cert-manager + csi-driver**：把证书以 CSI 卷挂载，自动续期，应用无需重启。
- **Pod 证书（1.37 GA）**：通过 `PodCertificateRequest` 为 Pod 签发短期证书，减少对 Secret 的依赖；采用前核对目标集群版本与签名者配置。

设计原则：

1. 凭据有效期越短越好，应用要能热加载。
2. 身份绑定到工作负载（ServiceAccount/Pod），而不是绑定到节点。
3. 私钥不落盘到镜像；用投影卷/内存文件系统。
4. 轮换要有监控：证书剩余寿命、签发失败率、握手错误率。

## S.10 故障速查

| 症状 | 常见原因 | 检查 |
|---|---|---|
| `x509: certificate has expired` | 叶子证书过期 | `kubeadm certs check-expiry`、`openssl x509 -dates` |
| `x509: certificate signed by unknown authority` | CA 不匹配/链不全 | 对比 CA bundle 与签发者 |
| `x509: certificate is valid for ... not ...` | SAN 缺名字 | `openssl x509 -text` 看 SAN |
| kubelet NotReady，日志有 `tls: bad certificate` | kubelet 客户端证书/服务端证书问题 | `kubelet.conf`、CSR、`--rotate-certificates` |
| `kubectl top` 503 | metrics-server/聚合层证书 | APIService、front-proxy CA |
| Pod 访问 API 401 | token audience/过期/挂载错误 | 投影卷配置、`TokenReview` |
| CSR 一直 Pending | 没有批准者/RBAC 不允许 | `kubectl describe csr`、approver 日志 |
| etcd 启动失败 | etcd 证书/peer 信任 | etcd 日志、`etcdctl endpoint health` |

## S.11 检查清单

- [ ] 所有证书到期时间进入监控与变更日历。
- [ ] apiserver 证书 SAN 覆盖所有访问入口（DNS、VIP、LB）。
- [ ] kubelet 开启客户端证书轮换；服务端证书有明确的批准策略。
- [ ] CSR 批准权限最小化，关键 signer 有审计。
- [ ] ServiceAccount token 使用投影卷，设置 audience 与过期时间；不需要的 Pod 关闭自动挂载。
- [ ] 聚合层/front-proxy 证书独立监控。
- [ ] 工作负载使用短期身份（Workload Identity/SPIFFE/cert-manager），不把长期私钥放进 Secret。
- [ ] 有证书轮换演练：续期、重启、回滚、故障注入。

## S.12 MiniK8s 的映射

MiniK8s 不实现真实 TLS/PKI，但保留了身份链的关键抽象：

- `StaticTokenAuthenticator` 模拟 bearer token → 用户/组；
- `ServiceAccountTokenIssuer` 用 HMAC 签发/校验 token，支持 audience；
- `TokenReview` 资源（`authentication.k8s.io/v1`，集群级）返回认证结果；
- RBAC 的 `system:masters` 直接放行，对应真实集群的组证书语义。

没有实现的部分：CSR API、证书轮换、kubelet TLS bootstrap、front-proxy、Pod 证书。它们是“传输层与信任层”的实现，适合在真实集群或 kind 中练习；对照阅读第 23 章（认证/授权）、第 29 章（升级与证书）、附录 H（安全基线）与附录 R（版本演进）。

## 延伸阅读

- [第 23 章 认证、授权与 RBAC](../chapters/23-authn-authz-rbac.md)
- [第 29 章 升级与节点运维](../chapters/29-upgrade-node-ops.md)
- [附录 H 安全加固基线](H-security-baseline.md)
- [附录 R 版本演进与特性采用](R-version-evolution-1.32-1.37.md)
