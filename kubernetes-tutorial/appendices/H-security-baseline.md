# 附录 H 安全加固基线

> 这是一份可执行的基线清单，不是合规认证。不同行业/云厂商的要求不同；先用它找出明显缺口，再按威胁模型细化。

## H.1 威胁模型先于工具

先回答：

- 攻击者是谁：外部用户、租户、被攻陷的 Pod、恶意镜像、内部人员、供应链？
- 资产是什么：数据、凭据、算力、网络、集群控制权？
- 信任边界在哪：节点、namespace、集群、云账号、CI/CD？
- 最坏情况：容器逃逸、Secret 泄露、集群接管、数据外泄、挖矿？

没有威胁模型，加固会变成“堆工具”。

## H.2 控制面基线

### API Server

```text
--anonymous-auth=false
--authorization-mode=Node,RBAC
--enable-admission-plugins=NodeRestriction,PodSecurity
--audit-log-path=/var/log/kubernetes/audit.log
--audit-policy-file=/etc/kubernetes/audit-policy.yaml
--encryption-provider-config=/etc/kubernetes/encryption-config.yaml
--tls-min-version=VersionTLS12
--service-account-issuer=https://kubernetes.default.svc
--service-account-key-file=/etc/kubernetes/pki/sa.pub
```

检查：

- 没有匿名访问；`system:anonymous` 无权限。
- `system:masters` 只给极少数应急账号；日常用 OIDC + RBAC。
- 审计覆盖 Secret、RBAC、exec、port-forward、webhook 变更。
- Secret 静态加密 + KMS 高可用。
- API Server 只暴露给控制面/负载均衡，不对公网开放。
- 证书 SAN 正确，过期监控。

### etcd

- 只监听控制面网络，客户端/peer 均 TLS。
- 专用磁盘，监控 fsync/leader 变化。
- 快照备份加密、异地保存、定期恢复演练。
- 限制谁可以读 etcd；etcd 的读权限等价于集群 root。

### Controller/Scheduler

- 多副本 + Lease 选主。
- 只通过 API Server 读写，不直接访问 etcd。
- 自定义控制器最小 RBAC，禁止 `cluster-admin`。
- webhook/控制器不要有外部网络写权限，除非明确需要。

## H.3 节点基线

- kubelet `anonymous.enabled=false`，`authorization.mode=Webhook`。
- 关闭只读端口（旧版 10255，已移除）。
- TLS bootstrap + 证书轮转。
- 容器运行时使用 systemd cgroup driver，开启 seccomp/AppArmor。
- 节点 OS 最小化、定期打补丁；限制 SSH，使用堡垒机/审计。
- 节点上不放长期云凭据；使用 Workload Identity/实例角色。
- 磁盘加密（云盘/节点盘）。
- 限制 hostPath、hostNetwork、hostPID、privileged；PSA/策略强制执行。
- 节点池按租户/工作负载隔离，taints 防止跨池调度。

## H.4 工作负载基线

Pod/容器 securityContext：

```yaml
spec:
  securityContext:
    runAsNonRoot: true
    runAsUser: 1000
    fsGroup: 2000
    seccompProfile:
      type: RuntimeDefault
  containers:
    - name: app
      securityContext:
        allowPrivilegeEscalation: false
        readOnlyRootFilesystem: true
        capabilities:
          drop: ["ALL"]
      resources:
        requests: {cpu: 100m, memory: 128Mi}
        limits: {cpu: 1, memory: 512Mi}
```

检查：

- 禁止 `privileged: true`、`hostNetwork`、`hostPID`、`hostIPC`。
- 禁止挂载 docker.sock、宿主根目录、敏感 hostPath。
- 只读根文件系统；需要写时挂 emptyDir/PVC。
- drop 所有 capabilities，按需 add（通常不需要）。
- 设置 requests/limits，避免 noisy neighbor。
- 禁止 latest 标签，使用 digest；镜像来自可信仓库。
- 每个工作负载独立 ServiceAccount，`automountServiceAccountToken: false`（不需要时）。
- 网络策略默认拒绝，按需放行。
- PDB/多副本/反亲和用于关键服务。

## H.5 身份与权限基线

- 人员用 OIDC/SSO，不用长期客户端证书；启用 MFA。
- 禁用/限制 `cluster-admin`；按 namespace 给 `view`/`edit`。
- CI/CD 使用独立 ServiceAccount + 短期 token，最小权限。
- 禁止给 `system:authenticated`/`system:serviceaccounts` 绑定高权限。
- 审计 RoleBinding/ClusterRoleBinding 变更。
- 限制 `create pods`、`create daemonsets`、`create rolebindings`、`get secrets` 等高危权限。
- 使用 `kubectl auth can-i --list`、SubjectAccessReview 验证实际权限。
- 定期扫描：rbac-police、KubiScan、kube-bench。

## H.6 网络基线

- 默认拒绝 ingress/egress，显式放行 DNS、监控、日志、Ingress Controller。
- 跨 namespace 访问用 namespaceSelector，不用大网段。
- 出口网关/白名单，限制 Pod 访问公网/元数据服务。
- 禁止 Pod 访问云元数据 `169.254.169.254`（除非明确需要）。
- Ingress 终止 TLS，启用 HSTS/安全套件；内部服务 mTLS（按需）。
- NetworkPolicy 由支持的 CNI 执行；不支持的 CNI 会让策略“看起来存在但不生效”。
- 监控 DNS 隧道/异常外联（Falco/Tetragon/Cilium Hubble）。

## H.7 供应链基线

1. 构建：最小基础镜像、多阶段、锁依赖、可重复构建、隔离构建环境。
2. 扫描：镜像/依赖/配置扫描（Trivy/Grype/Snyk），阻断高危。
3. 签名：cosign/Notation 签名，准入验证签名和 digest。
4. SBOM：生成并保存 SPDX/CycloneDX，便于漏洞追溯。
5. 准入：只允许可信仓库/签名镜像；禁止 latest。
6. 运行时：检测异常 exec、反向 shell、敏感文件访问、挖矿。
7. 响应：镜像撤回、凭证轮换、Pod 隔离、取证。

注意：

- 签名密钥用 KMS/HSM，定期轮换；签名验证服务要高可用。
- SBOM 不是“生成就安全”，要能关联漏洞和实际运行镜像。
- 供应链安全是流程，不是单个工具。

## H.8 运行时与检测

- seccomp `RuntimeDefault` 或自定义 profile。
- AppArmor/SELinux 按需启用。
- 只读根文件系统、非 root、drop capabilities。
- 运行时检测：Falco/Tetragon，关注：
  - 容器内 shell/异常 exec。
  - 读取 `/etc/shadow`、云元数据、ServiceAccount token。
  - 网络扫描/外联异常。
  - 挂载/逃逸尝试。
  - 加密货币挖矿。
- 检测规则先 audit，再阻断，避免误伤。

## H.9 审计与响应

- API Server 审计至少 Metadata；Secret/RBAC/exec 记录 Request。
- 审计日志集中存储、防篡改、保留满足合规。
- 节点/容器日志、网络流日志、云审计（CloudTrail/ActionTrail）关联。
- 告警：匿名访问、异常 RBAC 变更、Secret 批量读取、privileged Pod、异常外联。
- 预案：凭证泄露、镜像供应链、容器逃逸、集群接管、勒索软件。
- 定期演练：隔离节点、轮换密钥、恢复备份、取证。

## H.10 工具与检查

| 工具 | 用途 |
|---|---|
| kube-bench | CIS Kubernetes Benchmark 检查 |
| kube-hunter | 集群攻击面探测 |
| Kubescape | NSA/CISA、MITRE 基线扫描 |
| Polaris | 配置最佳实践 |
| rbac-police / KubiScan | RBAC 高危权限 |
| Trivy / Grype | 镜像/依赖漏洞 |
| cosign / Sigstore | 签名/透明日志 |
| Falco / Tetragon | 运行时检测 |
| Kyverno / Gatekeeper / VAP | 准入策略 |

工具输出要落到“谁在什么时候修哪个对象”，否则扫描报告只是噪音。

## H.11 常见“假安全”

- 只开 PSA `warn`，没有 `enforce`。
- NetworkPolicy 存在但 CNI 不支持。
- Secret 用了 base64 就以为加密。
- 只限制镜像 tag，不验证 digest/签名。
- RBAC 只按 namespace 分，但给了 `cluster-admin`。
- 审计日志开了但没人看、没有告警。
- 备份成功但从未恢复。
- 运行时检测只告警不阻断，也没有响应流程。
- 把安全责任全部推给云厂商；共享责任模型下，工作负载/身份/网络策略仍是客户责任。

## H.12 最小上线检查表

- [ ] 匿名 API 访问关闭，OIDC/RBAC 生效。
- [ ] Secret 静态加密 + KMS 高可用 + 轮换演练。
- [ ] 审计日志采集/告警/保留。
- [ ] kubelet 认证/授权/证书轮转。
- [ ] PSA restricted 或等效策略强制执行。
- [ ] 默认拒绝 NetworkPolicy + DNS/监控放行。
- [ ] 镜像 digest/签名/扫描准入。
- [ ] 工作负载非 root/只读根/drop capabilities/seccomp。
- [ ] RBAC 最小权限，高危权限有审批/审计。
- [ ] 备份/恢复/故障演练有记录。
- [ ] 运行时检测与响应 runbook。

## 延伸阅读

- [第 23 章 认证、授权与 RBAC](../chapters/23-authn-authz-rbac.md)
- [第 24 章 准入控制与供应链安全](../chapters/24-admission-supply-chain.md)
- [第 26 章 多租户与资源治理](../chapters/26-multitenancy-governance.md)
- [附录 C 故障排查清单](C-troubleshooting-checklist.md)
- [附录 F 集群搭建与生产化手册](F-cluster-build.md)
