# 第 25 章 运行时安全与节点加固

> 第 23 章讲“谁能进入集群”（认证、授权），第 24 章讲“进入时检查什么”（准入与供应链）。本章讲“进入之后如何限制、检测和响应”。三层缺一不可：只做准入，容器一旦运行就可以为所欲为；只做运行时检测，没有前置约束会疲于奔命。

## 25.1 威胁模型：从镜像到内核

先列出资产与攻击路径，再选工具：

| 资产 | 攻击路径 | 典型后果 |
|---|---|---|
| 镜像/构建链 | 依赖投毒、CI 凭据泄露、标签覆盖 | 恶意代码以合法镜像运行 |
| 集群凭据 | kubeconfig 泄露、SA token 窃取 | 横向移动、删除工作负载 |
| 容器 | 应用漏洞、反序列化、命令注入 | 容器内 RCE |
| 容器逃逸 | 特权容器、hostPath、内核漏洞、capabilities | 节点 root、横向到其他 Pod |
| 节点 | SSH 弱口令、未打补丁、kubelet 暴露 | 整节点失守 |
| 数据 | Secret 明文、PV 未加密、备份泄露 | 数据泄露 |
| 供应链 | 镜像仓库、Helm chart、Operator | 大规模植入 |
| 内部人/误操作 | 过宽 RBAC、kubectl apply 手滑 | 配置漂移、删除生产 |

一个常见的真实攻击链：

```text
泄露的 CI token
  → 推送恶意镜像（覆盖 tag）
  → 部署到集群（准入未校验签名）
  → 容器内 RCE
  → 读取投射的 SA token
  → 调用 API Server（RBAC 过宽）
  → 创建 privileged Pod
  → 挂载宿主 / 逃逸
  → 窃取 etcd 备份/云凭据
```

每一环都可以被独立阻断。安全设计的目标不是“绝对防住”，而是**增加攻击成本、缩短驻留时间、限制爆炸半径**。

## 25.2 容器隔离的真相

容器不是虚拟机。它依赖 Linux 的几组机制：

- **namespace**：PID、网络、挂载、IPC、UTS、user、cgroup、time。
- **cgroup**：CPU、内存、IO、pids 限制。
- **capabilities**：把 root 的特权拆成细粒度能力。
- **seccomp**：过滤系统调用。
- **LSM**：AppArmor、SELinux 等强制访问控制。
- **user namespace**：把容器内 root 映射到宿主机非 root。

默认配置下，这些机制并不“最强”：

- 容器默认可能拥有 `CAP_NET_RAW` 等能力。
- 默认 seccomp 可能是 `Unconfined`（取决于运行时/配置）。
- `privileged: true` 几乎关闭所有隔离。
- `hostPath` 可以把宿主机目录挂进容器。
- `hostNetwork`/`hostPID` 让容器看到节点。

因此，运行时安全的第一原则是：**默认最小权限，显式放开**。

### 25.2.1 用户命名空间：`hostUsers: false`

传统容器里，容器内 UID 0 往往就是宿主机 UID 0（只是被 capabilities 限制）。用户命名空间把容器内 UID/GID 映射到宿主机的一段非特权 ID，容器 root 在节点上只是一个普通用户，能显著降低容器逃逸、卷 UID 冲突和“以 root 写 hostPath”的影响。

Kubernetes 通过 Pod 级字段启用：

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: userns-demo
spec:
  hostUsers: false
  containers:
    - name: app
      image: demo:1.0
      securityContext:
        runAsNonRoot: true
```

要点：

- 需要 Linux 内核、容器运行时（containerd/crun/runc）和文件系统支持 idmap；不是所有 CSI/volume 类型都兼容。
- `hostUsers: false` 与 `hostNetwork`/`hostPID` 等宿主命名空间组合要谨慎；部分组合不支持。
- **版本**：Pod user namespaces 在 1.36 GA；`UserNamespacesSupport` 从 1.33 起逐步默认开启。PSA 对 `hostUsers: false` 的 `restricted` 检查也随版本变化，以目标集群的 `kubectl explain`/PSA 文档为准。
- 前提：Linux 内核（idmapped mounts）、containerd/runc/crun 支持；hostPath/NFS/部分 CSI 卷可能不兼容。
- 它**不能替代** seccomp/AppArmor/PSA：用户命名空间缩小 UID 权限，不限制系统调用和文件路径。
- 验证：`crictl inspect <container>` 看 `user`/UID 映射；或在容器内 `id`，再在节点上确认对应宿主 UID 不是 0。

对多租户集群，这是比“只设 runAsNonRoot”更强的隔离层；对需要 hostPath、特权设备或嵌套运行时的系统组件，通常不能启用。

## 25.3 Pod Security Standards 与 PSA

Kubernetes 定义了三档 Pod 安全标准：

| 级别 | 目标 | 典型限制 |
|---|---|---|
| `privileged` | 完全不限制 | 允许特权、hostPath、hostNetwork |
| `baseline` | 阻止已知提权 | 禁止 privileged、hostPath、hostPID、hostNetwork、危险 capabilities |
| `restricted` | 强化隔离 | 非 root、禁止提权、drop ALL capabilities、seccomp、限制卷类型、禁止特权/宿主命名空间 |

注意：`restricted` **不要求** `readOnlyRootFilesystem: true`。它是很强的额外加固建议（见 25.5），但不是 PSA 的准入条件；不要为了“过 restricted”而误以为必须只读 rootfs，也不要以为设了 restricted 就自动只读。

Pod Security Admission（PSA）在命名空间上执行：

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: payments
  labels:
    pod-security.kubernetes.io/enforce: restricted
    pod-security.kubernetes.io/enforce-version: latest
    pod-security.kubernetes.io/audit: restricted
    pod-security.kubernetes.io/warn: restricted
```

三个模式：

- `enforce`：违反则拒绝。
- `audit`：允许但记录审计注解/日志。
- `warn`：允许但向客户端返回警告。

`enforce-version` 要显式管理：可以固定到**受支持的 minor**（例如 `v1.37`），升级时随集群一起升；也可以用 `latest`，但必须接受“集群升级后标准变化导致意外拒绝”的风险，并先用 `warn`/`audit` 观察。不要依赖默认值，也不要把已经 EOL 的 `v1.31` 当成安全基线。

PSA 的边界：

- 它只检查 Pod 的**字段**，不理解镜像内容、运行时行为或网络。
- 它不替代准入 webhook、策略引擎或运行时检测。
- 它不处理已有 Pod；切换 enforce 不会驱逐旧工作负载。

## 25.4 seccomp、AppArmor 与 SELinux

### seccomp

seccomp 过滤系统调用，是缩小内核攻击面的高性价比手段。

```yaml
securityContext:
  seccompProfile:
    type: RuntimeDefault
```

- `RuntimeDefault`：使用容器运行时默认 profile，通常已屏蔽危险调用。
- `Localhost`：引用节点上的自定义 profile（`profiles/audit.json` 等）。
- `Unconfined`：不限制，除非有明确理由，否则不要用。

排查 seccomp 拒绝：

```bash
# 查看容器进程的系统调用（需要特权/调试工具）
kubectl exec -it pod -- sh -c 'grep Seccomp /proc/self/status'
# 节点上查看审计日志（取决于运行时）
journalctl -u containerd | grep -i seccomp
```

### AppArmor（1.34 起弃用，迁移到 seccomp/PSS）

> Kubernetes 1.34 **弃用 AppArmor**，官方方向是 seccomp + Pod Security Standards。新集群不要把 AppArmor 当主加固层；已有 profile 要逐步迁移，不能假设未来版本继续增强。

AppArmor 按路径/能力限制程序行为。较新版本支持 `securityContext.appArmorProfile`：

```yaml
securityContext:
  appArmorProfile:
    type: RuntimeDefault
```

旧版本使用注解：

```yaml
metadata:
  annotations:
    container.apparmor.security.beta.kubernetes.io/app: runtime/default
```

迁移建议：优先 `seccompProfile: RuntimeDefault` + PSA `restricted`；确实需要路径级约束时，再评估 AppArmor/SELinux 的节点支持与生命周期。注解形式已过时，不要在新清单中使用。

### SELinux

SELinux 通过标签做强制访问控制，常见于 OpenShift/RHEL：

```yaml
securityContext:
  seLinuxOptions:
    level: "s0:c123,c456"
```

多租户场景下，不同租户使用不同 MCS 标签可以阻止跨租户访问卷。配置错误会导致 Permission denied，需要结合 `ausearch`/`audit2why` 排查。

**SELinuxMount（1.37 默认启用）**：Kubernetes 用 `mount -o context=<label>` 挂载卷，而不是给每个文件重打标签。升级风险：

- 同一节点上不同 `seLinuxOptions` 的 Pod 共享同一个卷时，可能只有一种标签能挂载，另一个卡在 `ContainerCreating`。
- 需要回退时用 `seLinuxChangePolicy: Recursive`，但这会退回逐文件重打标签，挂载变慢。
- 升级前盘点 RWX/共享卷、hostPath、NFS 和多租户标签策略；先在预发验证。

## 25.5 securityContext：一张最小权限清单

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: hardened
spec:
  automountServiceAccountToken: false
  securityContext:
    runAsNonRoot: true
    runAsUser: 10001
    runAsGroup: 10001
    fsGroup: 10001
    seccompProfile:
      type: RuntimeDefault
  containers:
    - name: app
      image: example/app@sha256:...
      securityContext:
        allowPrivilegeEscalation: false
        readOnlyRootFilesystem: true
        capabilities:
          drop: ["ALL"]
      volumeMounts:
        - name: tmp
          mountPath: /tmp
  volumes:
    - name: tmp
      emptyDir: {}
```

逐项解释：

- `runAsNonRoot`：阻止以 UID 0 运行。
- `allowPrivilegeEscalation: false`：阻止 setuid/文件能力提权。
- `readOnlyRootFilesystem`：应用无法写镜像层；需要写的目录用 emptyDir/PVC。
- `drop: ALL`：只加回真正需要的能力（如绑定 80 端口的 `NET_BIND_SERVICE`，更好的做法是监听 8080）。
- `automountServiceAccountToken: false`：不需要 API 访问的 Pod 不投射 token。
- `seccompProfile: RuntimeDefault`：默认屏蔽危险系统调用。

例外：需要 `hostNetwork`、`hostPath`、`privileged` 的系统组件（CNI、CSI、日志 agent）必须放在独立命名空间、独立节点池，并用准入策略限制谁能创建。

## 25.6 沙箱运行时：更强的隔离边界

当容器隔离不够（多租户、不可信代码、CI runner），可以用沙箱运行时：

| 运行时 | 隔离方式 | 开销 | 适用 |
|---|---|---|---|
| runc | 常规 namespace/cgroup | 低 | 受信工作负载 |
| gVisor | 用户态内核拦截 syscall | 中高 | 不可信代码、CI |
| Kata Containers | 轻量 VM | 高 | 强隔离多租户 |
| Firecracker | microVM | 高 | Serverless/函数 |
| Confidential Containers | TEE + 加密内存 | 很高 | 机密数据/合规 |

通过 RuntimeClass 选择：

```yaml
apiVersion: node.k8s.io/v1
kind: RuntimeClass
metadata:
  name: gvisor
handler: runsc
---
apiVersion: v1
kind: Pod
metadata:
  name: untrusted
spec:
  runtimeClassName: gvisor
  containers:
    - name: app
      image: example/untrusted:v1
```

代价：

- 启动更慢、系统调用兼容性更差、GPU/高性能网络支持受限。
- 节点需要安装对应运行时与 shim。
- 可观测性/调试工具可能不兼容。

不要给所有工作负载默认上沙箱；按信任级别分层。

## 25.7 运行时检测与响应

预防之外，还需要检测“已经发生”的行为：

- **Falco**：基于系统调用/审计日志的规则引擎，检测 shell 启动、敏感文件读取、异常网络连接。
- **Tetragon**：eBPF 原生，做进程/网络可观测与策略执行。
- **KubeArmor**：LSM/eBPF 策略，限制容器行为。
- **auditd**：节点层审计，补充容器外行为。

一条 Falco 规则的概念：

```yaml
- rule: Terminal shell in container
  desc: A shell was spawned in a container
  condition: spawned_process and container and shell_procs
  output: "Shell spawned (user=%user.name container=%container.name image=%container.image.repository)"
  priority: WARNING
```

检测到之后要有**响应剧本**：

1. 确认范围：单个 Pod、节点、命名空间还是整个集群。
2. 隔离：cordon/drain 节点，给 Pod 打隔离标签，暂停控制器。
3. 取证：保留容器文件系统、内存、日志、审计记录；不要急着重启。
4. 清除：删除恶意工作负载，轮转凭据，修补入口。
5. 复盘：更新检测规则、准入策略和 RBAC。

没有响应剧本的检测只会制造告警疲劳。

### 25.7.1 规则设计：从“能检测”到“可运营”

运行时检测的难点不是装 Agent，而是规则质量。一个可运营的规则集要满足：

- **可解释**：告警能说明“谁、在哪个容器、做了什么、为什么可疑”。
- **低噪声**：误报率可接受；否则值班会关掉告警。
- **可验证**：有测试用例/模拟攻击，能证明规则会触发。
- **有响应**：每条高危规则对应 runbook 与自动化动作。

规则分层：

| 层 | 例子 | 处理 |
|---|---|---|
| 高危行为 | 容器内启动 shell、写入 `/etc`、读取云凭据 | page + 隔离 |
| 异常网络 | 连接矿池、扫描内网、DNS 隧道 | 调查/限流 |
| 提权/逃逸 | `setns`、加载内核模块、访问 `/proc/1` | page + 节点隔离 |
| 合规/基线 | 特权容器、hostPath、latest 镜像 | ticket/准入阻断 |

Falco 规则示例（概念）：

```yaml
- rule: Read cloud metadata credentials
  desc: Process inside a container read the cloud metadata endpoint
  condition: >
    spawned_process and container and
    (proc.cmdline contains "169.254.169.254" or
     proc.cmdline contains "metadata.google.internal")
  output: "Cloud metadata access (pod=%k8s.pod.name ns=%k8s.ns.name cmd=%proc.cmdline)"
  priority: CRITICAL
  tags: [cloud, credential-access]
```

Tetragon 的优势是 eBPF 原生，可以在内核层做进程/网络可观测与策略执行（如阻止特定二进制执行）。代价是对内核版本/配置有要求，规则模型与 Falco 不同。

规则治理：

- 用“攻击模拟”定期验证（在测试集群执行无害的模拟命令）。
- 对规则做版本控制、review、灰度。
- 按 namespace/团队设置不同阈值。
- 记录告警的处置结果，反推规则是否需要调整。

### 25.7.2 机密容器与更强隔离

普通容器共享宿主机内核；强隔离方案：

- **Kata Containers**：轻量 VM，每 Pod 一个内核。
- **gVisor**：用户态内核，拦截 syscall。
- **机密容器（Confidential Containers）**：基于 TDX/SEV-SNP 的硬件可信执行环境，保护内存中的敏感数据；生态仍在演进，采用前确认 CPU/云/运行时支持。

选择依据：

- 威胁模型：多租户不可信代码？合规要求？
- 性能预算：VM/用户态内核有启动与 I/O 开销。
- 运维能力：节点配置、镜像、调试工具链是否成熟。
- 兼容性：GPU/特殊设备在沙箱中的支持。

### 25.7.3 seccomp/AppArmor/SELinux 的 profile 生命周期

安全 profile 不是“写一次就完”：

1. **生成**：从应用行为/审计日志生成初始 profile（如 seccomp 的 `log` 模式）。
2. **验证**：在测试环境跑完整业务，收集被拒绝的 syscall/路径。
3. **收紧**：把必要项加入白名单，其余默认拒绝。
4. **发布**：通过 RuntimeClass/securityContext/节点配置分发。
5. **轮换**：应用升级可能引入新 syscall/文件路径；profile 要随版本更新。
6. **回滚**：profile 错误会导致应用启动失败；保留上一版。

常见错误：

- 直接套用别人的 seccomp profile，导致应用随机失败。
- AppArmor profile 只覆盖主容器，sidecar/init 容器未覆盖。
- SELinux 上下文与卷挂载不匹配，Pod 无法读写。
- profile 更新没有灰度，全量节点同时生效。

### 25.7.4 节点加固的验证

“配置了”不等于“生效”。验证清单：

- `kube-bench` 跑 CIS Benchmark，记录失败项与例外。
- 扫描节点端口/服务，确认只有必要端口开放。
- 检查 kubelet 参数：匿名认证关闭、只读端口关闭、授权 Webhook。
- 检查容器运行时 socket 权限（`/var/run/containerd/containerd.sock`）。
- 检查 hostPath/privileged Pod 的准入是否真的拒绝。
- 检查节点镜像签名/漏洞扫描。
- 检查审计日志是否包含节点登录、sudo、容器运行时操作。
- 定期做“逃逸/提权”演练（在隔离环境）。

## 25.8 节点加固

节点是容器的信任根。节点失守，Pod 安全策略意义有限。

### 操作系统

- 使用最小化、不可变或只读根文件系统（Bottlerocket、Talos、Flatcar、Container-Optimized OS）。
- 关闭不需要的服务与端口；SSH 仅限堡垒机/SSM，禁用密码登录。
- 内核、容器运行时、kubelet 定期打补丁；建立节点镜像流水线。
- 启用审计、完整性监控、磁盘加密。

### kubelet

```text
--anonymous-auth=false
--authorization-mode=Webhook
--client-ca-file=...
--read-only-port=0
--protect-kernel-defaults=true
--rotate-certificates=true
--event-qps=...
```

关键点：

- 10250 只对控制面/监控网络开放。
- 禁止匿名访问；授权走 Webhook/RBAC。
- 证书自动轮转；kubeconfig 权限 0600。
- 限制 kubelet 可挂载的卷类型与 hostPath（通过准入）。

### 网络

- 节点防火墙只放行必要端口：API Server、etcd peer/client、kubelet、Pod CIDR、CNI。
- 禁止节点直接访问公网；镜像/包管理走代理。
- 管理网与业务网分离；BMC/IPMI 独立网段。

### 凭据

- 云节点使用实例角色/Workload Identity，不在节点上放长期 AK。
- etcd 静态加密 + KMS；备份加密。
- 定期轮转证书、SA signing key、镜像仓库凭据。

## 25.9 Secret 与 ServiceAccount token

Secret 默认只是 base64，不是加密。运行时安全要关注：

- **静态加密**：API Server 配置 `EncryptionConfiguration`，用 KMS 加密 etcd 中的 Secret。
- **投射 token**：使用 `projected` volume，设置 audience、expiration、path；不要用永久 token。
- **最小权限**：每个工作负载独立 ServiceAccount，RBAC 只给需要的 verb/resource。
- **外部密钥**：External Secrets Operator、Vault、云 KMS；避免把密钥写进 Git。
- **轮转**：密钥、token、证书都要有轮转流程；轮转后验证应用能重新加载。

```yaml
spec:
  serviceAccountName: payments-api
  automountServiceAccountToken: false
  containers:
    - name: app
      volumeMounts:
        - name: token
          mountPath: /var/run/secrets/tokens
  volumes:
    - name: token
      projected:
        sources:
          - serviceAccountToken:
              audience: payments-api
              expirationSeconds: 3600
              path: token
```

## 25.10 事件响应与取证

Kubernetes 提供了几个关键工具：

```bash
# 进入一个正在运行的容器（不新增容器）
kubectl exec -it pod -- sh

# 用临时容器调试（不重启 Pod）
kubectl debug -it pod --image=nicolaka/netshoot --target=app

# 复制 Pod 并改变命令/权限
kubectl debug pod --copy-to=debug --container=app --image=busybox -- sleep 3600

# 节点级调试
kubectl debug node/mini-node-1 -it --image=busybox
```

取证注意事项：

- 临时容器会改变 Pod 状态，生产环境要先评估。
- 容器文件系统可能是 overlay，删除容器会丢失证据。
- 节点隔离优先于删除 Pod；先 cordon，再决定 drain。
- 保留 API Server 审计日志、CNI 流日志、运行时日志、Falco 告警。
- 轮转所有可能泄露的凭据；不要假设攻击者只在一个命名空间。

## 25.11 从准入到运行时的分层防线

一个可落地的分层：

```text
1. 供应链：签名、SBOM、漏洞扫描、构建隔离
2. 准入：PSS、策略引擎、镜像校验、RBAC 最小权限
3. 调度：专用节点池、taint/toleration、RuntimeClass
4. 运行时：seccomp、AppArmor/SELinux、capabilities、只读 rootfs
5. 检测：Falco/Tetragon、审计日志、异常网络
6. 响应：隔离、取证、轮转、复盘
7. 节点：最小 OS、补丁、kubelet 加固、网络分区
```

每一层都要有**默认值**和**例外流程**。例外必须可审计、有到期时间、有补偿控制。

## 25.12 MiniK8s 的边界

MiniK8s 实现了：

- Pod `securityContext` 的部分字段解析与 PSA 检查（见第 24 章）。
- ServiceAccount token 的签发与 RBAC。
- 简化的准入链。
- 网络策略判定。

不实现：

- 真实 seccomp/AppArmor/SELinux 执行。
- 沙箱运行时、gVisor/Kata。
- 运行时检测、审计日志、节点加固。
- Secret 静态加密。

因此，MiniK8s 可以用来验证“策略对象是否正确表达”，但不能验证“策略是否真正阻止了行为”。后者必须在真实集群用真实运行时测试。

## 25.13 生产清单

- [ ] 命名空间默认 `restricted`（或至少 `baseline`），例外有审批与到期时间。
- [ ] 所有工作负载 `runAsNonRoot`、`allowPrivilegeEscalation: false`、`drop: ALL`。
- [ ] 默认 `seccompProfile: RuntimeDefault`；高敏感工作负载用 Localhost profile。
- [ ] 不需要 API 的 Pod 关闭 token 自动挂载。
- [ ] 特权/宿主命名空间工作负载集中在专用节点池与命名空间。
- [ ] 不可信工作负载使用 gVisor/Kata 等沙箱运行时。
- [ ] 运行时检测覆盖 shell、敏感文件、异常网络、提权行为。
- [ ] 节点最小化、补丁、kubelet 加固、防火墙、证书轮转。
- [ ] Secret 静态加密、外部密钥管理、定期轮转。
- [ ] 有事件响应剧本并至少演练过一次。

## 本章小结

- 容器隔离依赖 namespace、cgroup、capabilities、seccomp、LSM 和 user namespace，默认并不等于安全。
- Pod Security Standards 提供基线，但只是字段检查；需要准入策略、沙箱运行时和运行时检测补齐。
- 节点是信任根，OS、kubelet、网络、凭据加固不可省略。
- 安全是分层防线：供应链、准入、调度、运行时、检测、响应、节点。
- MiniK8s 只能验证策略表达，不能替代真实运行时安全测试。

## 延伸阅读

- [第 23 章 认证、授权与 RBAC](23-authn-authz-rbac.md)
- [第 24 章 准入控制与供应链安全](24-admission-supply-chain.md)
- [第 26 章 多租户与资源治理](26-multitenancy-governance.md)
- [附录 H 安全加固基线](../appendices/H-security-baseline.md)
- [附录 S PKI、证书与身份生命周期](../appendices/S-pki-and-certificates.md)：证书、token 与工作负载身份。
- [附录 T Windows 容器与混合操作系统集群](../appendices/T-windows-and-mixed-os.md)：HostProcess、Windows 安全边界。
