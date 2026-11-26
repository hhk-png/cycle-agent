# 第 17 章 安全与多租户

## 17.1 队列不是安全边界

Queue 解决的是**资源公平与配额**，不是**安全隔离**。两个队列的 Pod 仍然：

- 共享节点内核；
- 共享容器运行时；
- 可能共享 GPU；
- 可以互相访问 Pod IP（取决于 NetworkPolicy）；
- 共享 etcd/API Server。

真正的多租户隔离需要：

- Namespace + RBAC；
- NetworkPolicy；
- Pod Security Standards；
- 节点池隔离；
- RuntimeClass/gVisor/Kata；
- 镜像与供应链安全；
- 审计。

Queue 只是“资源账本”，不要把它当“安全沙箱”。

## 17.2 RBAC

### 17.2.1 谁能提交 Volcano Job

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: vcjob-submitter
  namespace: team-a
rules:
  - apiGroups: ["batch.volcano.sh"]
    resources: ["jobs", "cronjobs"]
    verbs: ["create", "get", "list", "watch", "delete"]
  - apiGroups: ["scheduling.volcano.sh"]
    resources: ["podgroups"]
    verbs: ["create", "get", "list", "watch", "delete"]
  - apiGroups: [""]
    resources: ["pods", "pods/log"]
    verbs: ["get", "list", "watch"]
```

注意：

- 用户通常**不应该**能创建/修改 Queue；
- 用户不应该能改 `priorityClassName` 到高优先级，除非被授权；
- 用户不应该能创建 HyperNode；
- 如果允许用户创建 PodGroup，要限制 queue 字段（否则可能绕过配额）。

### 17.2.2 谁能管理队列

Queue 是集群级对象。给平台管理员：

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: volcano-queue-admin
rules:
  - apiGroups: ["scheduling.volcano.sh"]
    resources: ["queues"]
    verbs: ["create", "get", "list", "watch", "update", "patch", "delete"]
  - apiGroups: ["scheduling.volcano.sh"]
    resources: ["podgroups"]
    verbs: ["get", "list", "watch"]
```

### 17.2.3 NamespaceQueue 的权限边界

NamespaceQueue 的目标是“租户自治”，这带来新的权限问题：

- 允许租户创建 NamespaceQueue，不等于允许他们修改集群 Queue；
- 租户可以在自己的 namespace 内建子树，但所有 `parent` 必须最终落在管理员授权的集群 Queue 下；
- `allowedNamespaces` 是授权边界；不要给业务 namespace `*`，除非你接受它接入任何队列；
- 租户可以把子队列的 `guarantee` 设得很高，挤压同 namespace 的其他队列；平台要限制 `guarantee/capability` 比例；
- NamespaceQueue 的 CRD 是集群级安装的，租户只需要 namespace 内的 RBAC。

一个最小授权：

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: namespacequeue-admin
  namespace: team-a
rules:
  - apiGroups: ["scheduling.volcano.sh"]
    resources: ["namespacequeues"]
    verbs: ["create", "get", "list", "watch", "update", "patch", "delete"]
```

注意：不要同时授予 `queues`（集群级）的写权限，否则 NamespaceQueue 的隔离形同虚设。

### 17.2.4 ServiceAccount

控制器和调度器需要较高的权限（watch 所有 Pod/Node/CRD）。限制：

- 只授予必要 API group；
- 不要给 `cluster-admin`；
- 定期审计 ClusterRoleBinding；
- 使用 namespace 隔离控制面。

## 17.3 多租户模型

### 17.3.1 软多租户

- 共享集群与节点；
- 每团队一个 namespace + Queue；
- NetworkPolicy 隔离；
- Pod Security Standards；
- 资源配额。

适合内部团队，彼此信任但需要防止误用。

### 17.3.2 硬多租户

- 节点池隔离（taint/toleration + nodeSelector）；
- 每租户独立 GPU 节点；
- RuntimeClass 隔离；
- 独立网络域；
- 甚至独立集群（Volcano Global/Karmada）。

成本更高，但满足合规/对抗性租户。

### 17.3.3 混合

- 在线服务与离线训练共享节点，但用 TDM/混部隔离时间窗；
- GPU 训练用独占节点池；
- 推理用共享 GPU + MIG。

## 17.4 准入控制

### 17.4.1 限制优先级

用户可能给自己加 `high-priority`，抢走别人的资源。用 ValidatingAdmissionPolicy 或 webhook 限制：

```yaml
# 示意：只允许特定 ServiceAccount 使用 high-priority
apiVersion: admissionregistration.k8s.io/v1
kind: ValidatingAdmissionPolicy
metadata:
  name: restrict-high-priority
spec:
  failurePolicy: Fail
  matchConstraints:
    resourceRules:
      - apiGroups: ["batch.volcano.sh"]
        resources: ["jobs"]
        operations: ["CREATE", "UPDATE"]
  validations:
    - expression: >-
        !has(object.spec.priorityClassName) ||
        object.spec.priorityClassName != "high-priority" ||
        request.userInfo.username.startsWith("system:serviceaccount:platform:")
```

具体 CEL 以 Kubernetes 版本为准；这只是思路。

### 17.4.2 限制队列

确保 Job 的 `queue` 在用户被授权的集合内。可以用 webhook 或 Policy。

### 17.4.3 限制资源

- Namespace ResourceQuota；
- LimitRange；
- Queue capability；
- 二者要一起用：ResourceQuota 管 namespace 总量，Queue 管调度份额。

注意：ResourceQuota 与 Queue 是两套账。ResourceQuota 拒绝创建 Pod，Queue 拒绝调度；配置不一致会出现“Pod 创建了但永远 Pending”。

## 17.5 网络安全

- 默认拒绝的 NetworkPolicy；
- 只允许同 namespace/同 job 的 Pod 互通；
- 控制面与 kubelet 的访问；
- SSH 插件生成的 Secret 要限制读取；
- 不要在生产 Job 里暴露 22 端口到集群外。

## 17.6 镜像与供应链

- 只允许签名镜像（Cosign/Notation）；
- 私有 registry + imagePullSecret；
- 固定 digest，不用 `latest`；
- 扫描 CVE；
- 限制 privileged/hostPath/hostNetwork。

Volcano 的作业插件会创建 Secret（SSH key）、Service、ConfigMap。审计这些资源的权限。

## 17.7 已知漏洞与修复

| 漏洞（以官方 advisory 为准） | 影响 | 修复 |
|---|---|---|
| CVE-2026-44247 | webhook 无界 body → OOM，CVSS 6.8 | v1.12.4、v1.13.3、v1.14.2 及后续 patch 线 |
| GHSA-j38h-7pfq-cxmw | DRA 容量核算过度迭代可阻塞调度，CVSS 6.5 | v1.15.2 |
| 历史 webhook/CRD 校验问题 | 非法对象进入集群 | 跟随 patch 线 |

安全策略：

- 订阅 volcano-sh/volcano 的 security advisory；
- 每月检查 patch；
- 在 staging 先验证；
- 保留回滚镜像。

## 17.8 审计

开启 Kubernetes audit log，关注：

- 谁创建/修改了 Queue；
- 谁使用了高优先级；
- 谁删除了 PodGroup/Job；
- 谁修改了 scheduler configmap；
- 谁读取了 SSH Secret。

Volcano 自身的事件（Evicted/Reclaimed）也要进入审计/告警。

## 17.9 加固清单

- [ ] 每团队独立 namespace + Queue。
- [ ] RBAC 最小权限，用户不能改 Queue/HyperNode。
- [ ] 限制高优先级类的使用。
- [ ] NetworkPolicy 默认拒绝。
- [ ] Pod Security Standards（restricted/baseline）。
- [ ] 镜像签名/扫描，禁用 latest。
- [ ] 控制面多副本 + 证书轮换。
- [ ] 升级到含 CVE 修复的 patch。
- [ ] 审计日志 + 告警。
- [ ] 定期演练：队列关闭、抢占、webhook 故障。

## 17.10 MiniVolcano 的安全视角

MiniVolcano 没有安全模型：

- 没有 API Server/RBAC；
- 没有 namespace 隔离；
- 没有 webhook；
- 没有镜像/网络。

它只模拟调度语义。用它讲安全会误导；第 24 章会明确这一点。

## 17.11 Pod Security Admission 与 NetworkPolicy 示例

这一节给两组可以直接改的基线配置。它们不替代第 17.2–17.6 节的权限设计，但能挡住大部分“误配置导致提权/横向移动”的问题。

### 17.11.1 Namespace 的 Pod Security 标签

Kubernetes 内置 Pod Security Admission（PSA），用 namespace 标签启用：

```yaml
apiVersion: v1
kind: Namespace
metadata:
  name: team-a
  labels:
    pod-security.kubernetes.io/enforce: restricted
    pod-security.kubernetes.io/enforce-version: latest
    pod-security.kubernetes.io/audit: restricted
    pod-security.kubernetes.io/warn: restricted
```

`restricted` 要求 Pod 满足一组安全上下文。一个符合要求的训练 Pod 片段：

```yaml
spec:
  securityContext:
    runAsNonRoot: true
    runAsUser: 1000
    fsGroup: 1000
    seccompProfile:
      type: RuntimeDefault
  containers:
    - name: trainer
      image: registry.example.com/trainer@sha256:...
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

常见冲突：

| 需求 | restricted 下的做法 |
|---|---|
| 写临时文件 | 挂 `emptyDir` 到 `/tmp`，不要放开 root |
| GPU 访问 | 由 device plugin 注入设备，不需要 `privileged: true` |
| RDMA/IB | 用设备插件/CRD 暴露，避免 `hostNetwork` + privileged |
| 调试 | 用临时 debug 容器/`kubectl debug`，不要长期放宽 namespace |
| 固定 UID | 在镜像/securityContext 里设置，不要依赖 root |

如果某个团队确实需要 `baseline` 或特权，单独给它一个 namespace，并配合节点隔离；不要把整个集群的 PSA 降到 `privileged`。

### 17.11.2 默认拒绝的 NetworkPolicy

先拒绝所有入站/出站，再按需放行：

```yaml
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: default-deny
  namespace: team-a
spec:
  podSelector: {}
  policyTypes: ["Ingress", "Egress"]
---
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: allow-dns
  namespace: team-a
spec:
  podSelector: {}
  policyTypes: ["Egress"]
  egress:
    - to:
        - namespaceSelector:
            matchLabels:
              kubernetes.io/metadata.name: kube-system
      ports:
        - protocol: UDP
          port: 53
        - protocol: TCP
          port: 53
---
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: allow-same-job
  namespace: team-a
spec:
  podSelector:
    matchLabels:
      volcano.sh/job-name: pytorch-dist
  policyTypes: ["Ingress", "Egress"]
  ingress:
    - from:
        - podSelector:
            matchLabels:
              volcano.sh/job-name: pytorch-dist
  egress:
    - to:
        - podSelector:
            matchLabels:
              volcano.sh/job-name: pytorch-dist
```

注意：

- NetworkPolicy 需要 CNI 支持；不支持的 CNI 会静默忽略。
- 允许同 Job 通信时，标签必须由控制器写入且不可被用户伪造；否则用户可以打上别人的 job 标签绕过隔离。
- DNS 放行要同时覆盖 UDP/TCP 53；只放 UDP 会让某些解析失败。
- 训练作业常需要访问对象存储/镜像仓库；把这些出口显式列出来，不要图省事放开 `0.0.0.0/0`。

### 17.11.3 一组最小检查

- [ ] 每个租户 namespace 有 PSA 标签。
- [ ] 没有 `privileged: true` 的常规作业。
- [ ] 没有 `hostPath` 挂载宿主机敏感目录。
- [ ] 默认拒绝的 NetworkPolicy 存在，DNS 已放行。
- [ ] 作业 Pod 的 ServiceAccount 没有多余权限。
- [ ] 镜像用 digest，不用 `latest`。
- [ ] SSH Secret 只对 Job 的 ServiceAccount 可读。
- [ ] 审计日志覆盖 Queue/ConfigMap/Secret 的写操作。

## 17.12 威胁模型：把“多租户”拆成攻击面

第 17.1 节说“Queue 不是安全边界”。这一节把这句话展开成一张可操作的威胁模型：谁信任谁、攻击者能从哪进、每层用什么控制。

### 17.12.1 信任边界

```text
租户 A namespace ──┐
租户 B namespace ──┼──► API Server（认证/鉴权/准入）──► Volcano 控制面
租户 C namespace ──┘         │                              │
                             │                              ▼
                        etcd（集群状态）              节点 / device plugin / DRA
                             │                              │
                             └──────── 审计日志 ◄───────────┘
```

四条边界：

1. **租户 ↔ API Server**：RBAC 与 admission 是第一道闸；租户能创建什么对象，决定他能影响什么。
2. **API Server ↔ Volcano 控制面**：Volcano 的 ServiceAccount 权限很大；它被攻破等于集群被攻破。
3. **控制面 ↔ 节点**：调度器只写绑定，kubelet/device plugin/DRA driver 才真正接触设备和容器运行时。
4. **节点 ↔ 节点**：NetworkPolicy、CNI、RDMA 隔离决定横向移动的难易。

### 17.12.2 STRIDE 视角

| 威胁 | 在 Volcano 平台的例子 | 控制 |
|---|---|---|
| Spoofing（冒充） | 租户用别人的 ServiceAccount 提交作业；伪造 PodGroup 标签 | RBAC + 审计 + 准入校验 owner |
| Tampering（篡改） | 改 Queue capability、scheduler ConfigMap、PriorityClass | 集群级对象只读；GitOps + 审批；审计 |
| Repudiation（抵赖） | 用户否认提交过高优先级作业 | API 审计日志 + 提交人标签 |
| Information disclosure（泄露） | 读别人的 SSH Secret、日志、指标标签 | Secret RBAC；日志脱敏；指标基数控制 |
| Denial of service（拒绝服务） | 超大 body 打挂 webhook；海量 Pending 作业拖慢调度 | body 上限、限流、配额、unschedulable cache |
| Elevation of privilege（提权） | 用高优先级/特权 Pod/宿主路径逃逸 | 优先级白名单、PSA、禁止 privileged/hostPath |

### 17.12.3 六条真实攻击路径

**路径 1：优先级提权。** 租户给自己加 `high-priority`，抢占别人的训练。控制：PriorityClass 白名单（ValidatingAdmissionPolicy/webhook），只允许平台 SA 使用高优先级；Queue 侧再用 `priority` 限制队列内最高优先级。

**路径 2：队列绕过。** 用户直接创建 PodGroup，把 `queue` 写成别的租户，或创建一个同名 PodGroup 干扰调度。控制：限制 PodGroup 创建权限；准入校验 `queue` 在租户授权集合内；PodGroup 的 owner/labels 必须与提交人一致。

**路径 3：Secret 横向。** `ssh` 插件创建免密 Secret；如果同 namespace 的其他 Pod 能读，就能登录训练 Pod。控制：每个 Job 独立 ServiceAccount；Secret 只对该 SA 可读；Job 结束清理；不要把 SSH 端口暴露到集群外。

**路径 4：Webhook DoS。** webhook 对无界 body 的处理可导致 OOM（CVE-2026-44247），`failurePolicy: Fail` 时整个集群的写入都可能被阻断。控制：升级到修复版本；`MaxBytesReader`；副本/PDB/资源限制；评估 `Ignore` 的取舍。

**路径 5：Agent 出口。** Agent 沙箱访问外部工具时没有出口限制，攻击者用它外传数据或扫描内网。控制：默认拒绝的 NetworkPolicy；出口网关/白名单；沙箱 RuntimeClass；只读文件系统。

**路径 6：设备/驱动供应链。** 恶意或未签名的 device plugin/DRA driver 以高权限运行，能访问宿主机设备。控制：镜像签名/验证；driver 单独命名空间与 RBAC；节点隔离；审计 device plugin 的挂载。

### 17.12.4 控制清单

| 层次 | 预防 | 检测 | 纠正 |
|---|---|---|---|
| 身份 | 每租户 SA、最小 RBAC | 审计 API 调用 | 吊销 SA/Token |
| 准入 | 优先级/队列/镜像策略 | 拒绝率告警 | 回滚策略 |
| 调度 | Queue 配额、PDB、gang | 账本不守恒告警 | 暂停 preempt/reclaim |
| 运行时 | PSA、NetworkPolicy、RuntimeClass | 异常进程/网络告警 | 隔离节点、重建 Pod |
| 设备 | 签名驱动、DRA 策略 | 设备分配审计 | 下线驱动/节点 |
| 数据 | Secret 最小权限、加密 | 访问日志 | 轮换密钥 |

### 17.12.5 一个最小威胁建模流程

```text
1. 画出信任边界：租户/控制面/节点/设备/存储
2. 列出每层的资产：配额、优先级、Secret、模型、数据、GPU
3. 对每个资产问：谁能读/写/删？最坏后果是什么？
4. 选控制：预防（RBAC/准入）、检测（审计/告警）、纠正（隔离/回滚）
5. 每年或每次大版本升级后重跑一次
```

威胁模型不需要很长，但必须**具体到对象和字段**。写“加强安全”没有用；写“租户不能创建 PriorityClass，只有 platform SA 能，审计 event 里出现 priorityClassName=high-priority 且 user 不是 platform 就告警”才有用。

## 17.13 本章小结

- Queue 是资源边界，不是安全边界。
- 多租户需要 RBAC、NetworkPolicy、PSA、节点隔离、镜像安全共同配合。
- 高优先级和 Queue 管理权限必须收紧。
- 跟随 patch 线修复 CVE。
- 威胁模型要落到对象/字段：优先级提权、队列绕过、Secret 横向、webhook DoS、Agent 出口、设备供应链是六条高频路径。
- MiniVolcano 不覆盖安全。

下一章讲性能与规模化：当集群有几千节点、几万 Pod 时，调度器如何不成为瓶颈。
