# 第 3 章 · 安装与快速上手

## 3.1 前置条件

- 一个 Kubernetes 集群。Kueue v0.20 官方测试/推荐 Kubernetes **1.34+**；DRA/TAS 的不同子能力还有 1.34/1.35/1.36 的额外要求（见 9.2.4），具体以目标 release 为准。
- `kubectl` 能访问集群。
- 如果使用 kind/minikube，确保节点有足够资源运行示例。示例本身很轻，但 `kueue-controller-manager` 需要一些 CPU/内存。
- 可选：Helm 3、Prometheus Operator、kind。

快速创建一个本地集群：

```bash
kind create cluster --name kueue-lab
kubectl cluster-info --context kind-kueue-lab
```

## 3.2 安装 Kueue

### 方式一：kubectl

```bash
export KUEUE_VERSION=v0.20.5   # 2026-11 示例；替换成你要安装的 release
kubectl apply --server-side -f \
  "https://github.com/kubernetes-sigs/kueue/releases/download/${KUEUE_VERSION}/manifests.yaml"

kubectl wait deploy/kueue-controller-manager -n kueue-system \
  --for=condition=available --timeout=5m
```

`--server-side` 很重要：Kueue 的 CRD 较大，client-side apply 可能遇到 annotation 大小限制。

### 方式二：Helm

```bash
helm install kueue oci://registry.k8s.io/kueue/charts/kueue \
  --version=<chart-version> \
  --namespace kueue-system \
  --create-namespace \
  --wait --timeout 300s
```

### 验证安装

```bash
kubectl get pods -n kueue-system
kubectl get crd | grep kueue
```

你应该看到：

- `kueue-controller-manager` Running；
- `clusterqueues.kueue.x-k8s.io`、`localqueues.kueue.x-k8s.io`、`workloads.kueue.x-k8s.io`、`resourceflavors.kueue.x-k8s.io`、`cohorts.kueue.x-k8s.io`、`admissionchecks.kueue.x-k8s.io`、`topologies.kueue.x-k8s.io` 等 CRD。
- webhook Service、metrics Service 和 leader election 相关对象。

### 3.2.1 安装 alpha CRD（可选）

默认的 `manifests.yaml` 不包含 alpha API。第 7、9 章会用到：

- `PreemptionConfig`（`ConfigurablePreemptions` gate）；
- `CapacityProvider` / `DynamicQuotaOrchestrator`（`DynamicQuotaOrchestration` gate）。

如果按正文 apply 时报 `no matches for kind "PreemptionConfig"`，先补 CRD：

```bash
# kubectl 安装：release 资产里的 alpha CRD 清单（文件名以 release 为准，常见为 alpha-crds.yaml）
kubectl apply --server-side -f \
  "https://github.com/kubernetes-sigs/kueue/releases/download/${KUEUE_VERSION}/alpha-crds.yaml"
```

Helm 安装则用：

```bash
helm upgrade --install kueue oci://registry.k8s.io/kueue/charts/kueue \
  --namespace kueue-system \
  --set enableAlphaAPIs=true
```

装完 CRD 还不够：还要在 Configuration 或控制器参数里开启对应 gate。只装 CRD 不开 gate，对象可以创建但控制器不会处理。

### 3.2.2 安装 kueuectl

Kueue 提供了一个 kubectl 插件 `kueuectl`，比裸 `kubectl get` 更方便：

```bash
# 从 release 页面下载对应平台的二进制，或使用包管理器/源码安装
kubectl kueue version
kubectl kueue list workload --all-namespaces
kubectl kueue list localqueue -n default
kubectl kueue stop clusterqueue team-a-cq
kubectl kueue resume clusterqueue team-a-cq
```

`kueuectl stop/resume` 实际上是在改 `stopPolicy`，适合维护窗口。生产上仍建议通过 GitOps 管理这些字段，而不是手工敲命令。

### 3.2.3 安装后的验收

不要只看到 Pod Running 就认为装好了。至少做五项检查：

```bash
# 1. 控制器可用
kubectl -n kueue-system get deploy kueue-controller-manager

# 2. webhook 可用（创建非法对象应被拒绝）
kubectl apply -f - <<'EOF'
apiVersion: kueue.x-k8s.io/v1beta2
kind: ClusterQueue
metadata:
  name: invalid
spec: {}
EOF
# 期望：Error from server (Invalid) ... resourceGroups

# 3. CRD 已建立
kubectl get crd | grep kueue

# 4. 指标端点
kubectl -n kueue-system port-forward svc/kueue-controller-manager-metrics-service 8443:8443 &
curl -k https://localhost:8443/metrics | grep '^kueue_'

# 5. 版本
kubectl -n kueue-system get deploy kueue-controller-manager \
  -o jsonpath='{.spec.template.spec.containers[0].image}'
```

### 3.2.4 离线/气隙环境安装

企业集群经常不能直连 GitHub 和 `registry.k8s.io`。离线安装要准备四类东西：镜像、清单/CRD、Helm chart（如果使用）、`kueuectl` 二进制。

**1. 在联网机器上收集资产**

```bash
export KUEUE_VERSION=v0.20.5
mkdir -p kueue-airgap && cd kueue-airgap

# 控制器清单 + alpha CRD
curl -fLO "https://github.com/kubernetes-sigs/kueue/releases/download/${KUEUE_VERSION}/manifests.yaml"
curl -fLO "https://github.com/kubernetes-sigs/kueue/releases/download/${KUEUE_VERSION}/alpha-crds.yaml" || true

# kueuectl（文件名以 release 资产为准；下面只是常见命名）
curl -fLO "https://github.com/kubernetes-sigs/kueue/releases/download/${KUEUE_VERSION}/kubectl-kueue-linux-amd64"

# 记录镜像与 digest（镜像名以 manifests.yaml 里的实际值为准）
grep -o 'registry.k8s.io/kueue/[^"]*' manifests.yaml | sort -u
```

**2. 把镜像同步到内部 registry**

```bash
# 任选一种工具：skopeo / crane / regctl / docker pull+tag+push
skopeo copy \
  docker://registry.k8s.io/kueue/kueue:${KUEUE_VERSION} \
  docker://registry.internal.example.com/kueue/kueue:${KUEUE_VERSION}

# 用 digest 固定，避免 tag 被重写
skopeo inspect docker://registry.k8s.io/kueue/kueue:${KUEUE_VERSION} | jq -r .Digest
```

如果使用 KueueViz，还要同步 frontend/backend 镜像；以 Helm chart 渲染出的 Deployment 为准，不要只同步控制器镜像。

**3. 校验供应链（能拿到签名/ SBOM 时）**

```bash
cosign verify registry.k8s.io/kueue/kueue:${KUEUE_VERSION} \
  --certificate-identity-regexp 'https://github.com/kubernetes-sigs/kueue/.*' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
cosign download sbom registry.k8s.io/kueue/kueue:${KUEUE_VERSION} > kueue.sbom.json
```

没有签名时至少记录镜像 digest、来源 URL 和采集时间，并在内部 registry 开启不可变 tag/扫描。

**4. 在离线集群安装**

```bash
# 方案 A：直接用清单，把 image 改写成内部 registry
sed -i 's#registry.k8s.io/kueue/kueue#registry.internal.example.com/kueue/kueue#g' manifests.yaml
kubectl apply --server-side -f manifests.yaml
kubectl apply --server-side -f alpha-crds.yaml   # 可选

# 方案 B：Helm，从内部 OCI registry 拉 chart 并覆盖镜像
helm install kueue oci://registry.internal.example.com/charts/kueue \
  --version=<chart-version> -n kueue-system --create-namespace \
  --set controllerManager.image.repository=registry.internal.example.com/kueue/kueue \
  --set controllerManager.image.tag=${KUEUE_VERSION} \
  --set controllerManager.image.pullPolicy=IfNotPresent
```

如果 registry 需要认证，先创建 imagePullSecret 并在 chart values/Deployment 里引用；不要用节点级 docker config 作为唯一方案。

**5. 升级也是离线流程**

离线升级要同时准备新版本 `manifests.yaml`、alpha CRD、镜像和（如有）conversion webhook 证书。顺序是：镜像先入仓 → CRD/清单先 apply → 再滚动控制器。不要把"镜像还没同步完"和"CRD 已升级"混在一起，否则新控制器会拉不到镜像而 CrashLoop。

`kueuectl` 二进制放到 PATH 或 kubectl 插件目录（Linux 通常 `~/.local/bin` 或 `/usr/local/bin`），用 `kubectl kueue version --client` 验证。

## 3.3 最小可用配置

创建三个对象：

1. `ResourceFlavor`：描述资源类型；
2. `ClusterQueue`：定义配额；
3. `LocalQueue`：给 namespace 一个入口。

文件在 `examples/kueue/quick-start-setup.yaml`：

```yaml
apiVersion: kueue.x-k8s.io/v1beta2
kind: ResourceFlavor
metadata:
  name: default-flavor
---
apiVersion: kueue.x-k8s.io/v1beta2
kind: ClusterQueue
metadata:
  name: cluster-queue
spec:
  namespaceSelector: {}          # 允许所有 namespace
  resourceGroups:
  - coveredResources: ["cpu", "memory"]
    flavors:
    - name: default-flavor
      resources:
      - name: cpu
        nominalQuota: 9
      - name: memory
        nominalQuota: 9Gi
---
apiVersion: kueue.x-k8s.io/v1beta2
kind: LocalQueue
metadata:
  namespace: default
  name: user-queue
spec:
  clusterQueue: cluster-queue
```

应用：

```bash
kubectl apply -f examples/kueue/quick-start-setup.yaml
```

检查：

```bash
kubectl get clusterqueue cluster-queue -o wide
# NAME            COHORT   STRATEGY         PENDING WORKLOADS   ADMITTED WORKLOADS   ACTIVE   REASON
# cluster-queue            BestEffortFIFO   0                   0                    True     Ready

kubectl get localqueue user-queue -n default -o wide
# NAME         CLUSTERQUEUE    PENDING WORKLOADS   ADMITTED WORKLOADS   ACTIVE   REASON
# user-queue   cluster-queue   0                   0                    True     Ready
```

如果 `ACTIVE=False`，先用 `-o wide` 看 `REASON`，再 `kubectl describe clusterqueue`。常见原因：

- 引用的 ResourceFlavor 不存在；
- 引用的 AdmissionCheck 不存在或未 Active；
- Cohort 引用错误；
- 配额/资源组 schema 不合法。

## 3.4 提交第一个 Job

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: sample-job
  namespace: default
  labels:
    kueue.x-k8s.io/queue-name: user-queue
spec:
  parallelism: 3
  completions: 3
  template:
    spec:
      containers:
      - name: dummy-job
        image: registry.k8s.io/e2e-test-images/agnhost:2.53
        command: ["/bin/sh"]
        args: ["-c", "sleep 30"]
        resources:
          requests:
            cpu: "1"
            memory: "200Mi"
      restartPolicy: Never
```

```bash
kubectl apply -f examples/kueue/quick-start-job.yaml
```

关键点是标签：

```yaml
metadata:
  labels:
    kueue.x-k8s.io/queue-name: user-queue
```

Kueue 通过这个标签识别要托管的 Job，并把它路由到对应 LocalQueue。

## 3.5 观察生命周期

### 看 Workload

```bash
kubectl get workloads -n default
# NAME          QUEUE        RESERVED IN     ADMITTED   FINISHED   AGE
# sample-job    user-queue   cluster-queue   True                  10s
```

`RESERVED IN` 表示配额预留到了哪个 ClusterQueue；`ADMITTED=True` 表示 Job 已被放行。

### 看 Job 和 Pod

```bash
kubectl get jobs -n default
kubectl get pods -n default
```

Pod 应该开始运行。因为示例 sleep 30 秒，30 秒后 Job 完成，Workload 会变成 `FINISHED=True`。

### 看条件

```bash
kubectl get workload sample-job -n default -o yaml
```

重点看：

- `status.conditions`：
  - `QuotaReserved=True`
  - `Admitted=True`
  - `Finished=True`
- `status.admission`：
  - `clusterQueue`
  - `podSetAssignments`（Flavor 分配）
- `status.requeueState`（如果被驱逐过）

### 看事件

```bash
kubectl describe workload sample-job -n default
```

事件和条件能回答"为什么还没开始"。

## 3.6 Kueue Configuration 与 feature gate

Kueue 控制器通过一个 `Configuration` 对象（通常放在 `kueue-system/kueue-manager-config` ConfigMap 里）配置：

```yaml
apiVersion: config.kueue.x-k8s.io/v1beta2
kind: Configuration
namespace: kueue-system
manageJobsWithoutQueueName: false
integrations:
  frameworks:
  - "batch/job"
  - "jobset.x-k8s.io/jobset"
  - "ray.io/rayjob"
  labelKeysToCopy:
  - "team.example.com/cost-center"
waitForPodsReady:
  timeout: 30m
  recoveryTimeout: 5m
  blockAdmission: false
fairSharing:
  preemptionStrategies:
  - LessThanOrEqualToFinalShare
  - LessThanInitialShare
admissionFairSharing:
  usageHalfLifeTime: 168h
  usageSamplingInterval: 5m
  resourceWeights:
    nvidia.com/gpu: 10
```

常见配置项：

| 配置 | 作用 |
|---|---|
| `integrations.frameworks` | 启用哪些作业框架的集成 |
| `integrations.labelKeysToCopy` | 把 Job/Pod 的哪些标签复制到 Workload，便于排障/成本分摊 |
| `manageJobsWithoutQueueName` | 是否管理没有 queue-name 标签的 Job（谨慎；不会自动选队列） |
| `managedJobsNamespaceSelector` | 只 reconcile 匹配的 namespace；v0.19+ 总是生效，不只是 `manageJobsWithoutQueueName` 时 |
| `waitForPodsReady` | Pod 未就绪超时/恢复；`requeuingStrategy` 可配退避次数/基数/上限（默认 3/60/1800） |
| `objectRetentionPolicies` | `workloads.afterFinished` / `afterDeactivatedByKueue`；控制 Workload 保留/清理，**删除可能级联影响 owner 对象** |
| `queueVisibility` | `updateIntervalSeconds`、`clusterQueues.maxCount`；Visibility 快照频率/保留数 |
| `clientConnection.qps` / `burst` | 控制器到 API server 的限速（默认 20/30）；大规模调优的直接旋钮 |
| `fairSharing.preemptionStrategies` | Preemption-based Fair Sharing 的策略组合 |
| `admissionFairSharing` | Admission Fair Sharing 的半衰期、采样间隔、资源权重 |
| `multiKueue` | MultiKueue 的 GC、worker 超时、dispatcher、stepSize |
| `metrics.bindAddress` / `customLabels` | 指标监听地址与自定义标签 |
| `webhook.port` | webhook 端口；证书管理是顶层 `internalCertManagement` |
| `featureGates` | alpha/beta gate 开关；生产默认全关 alpha |

两个容易踩的 Day-2 配置：

- `objectRetentionPolicies` 不是"垃圾回收越激进越好"。`afterFinished` 太短会让排障/审计丢失 Workload；`afterDeactivatedByKueue` 删除 Workload 时可能连带删除 owner 对象，先在 staging 验证。
- `clientConnection.qps/burst` 太小会让大规模集群的 reconcile 排队；太大可能打爆 API server。结合第 12 章的 `apiserver_request_total`/workqueue 指标调。

feature gate 通常通过 Configuration 顶层的 `featureGates`（v1beta2 是 `map[string]bool`）或安装清单的 `--feature-gates` 配置；两者不能同时使用。几个实践要点：

- **不要在生产随意打开 alpha gate**；先看该 gate 的依赖关系。例如关闭 `AdmissionFairSharing` 时，v0.20 要求同时关闭依赖它的 `AdmissionFairSharingAnchorAtQuotaReservation`，否则控制器启动失败。
- **TAS 相关 gate 有依赖链**；只关 `TopologyAwareScheduling` 而不关依赖它的 `TAS*` gate，控制器会拒绝启动并列出冲突。
- **升级前比对 gate 默认值**；beta gate 可能在某个版本默认开启，导致行为变化。
- **配置错误时控制器可能直接不启动**；这比"部分功能异常"更难发现，部署后务必看启动日志。

### 3.6.1 修改 Configuration 的安全流程

`Configuration` 不是一个可以用 `kubectl apply` 直接创建/更新的 CR；Helm 安装时它被渲染到 `kueue-system/kueue-manager-config` ConfigMap 的 `controller_manager_config.yaml` 键里，控制器在**启动时**读取。

```bash
# 1. 备份当前配置
kubectl -n kueue-system get cm kueue-manager-config \
  -o jsonpath='{.data.controller_manager_config\.yaml}' > kueue-config.backup.yaml

# 2. 查看/编辑（推荐先改到本地文件，再整体替换）
kubectl -n kueue-system get cm kueue-manager-config \
  -o jsonpath='{.data.controller_manager_config\.yaml}' > kueue-config.yaml
$EDITOR kueue-config.yaml

# 3. 整体替换该键（不要只 apply 一个半截 YAML）
kubectl -n kueue-system create cm kueue-manager-config \
  --from-file=controller_manager_config.yaml=kueue-config.yaml \
  --dry-run=client -o yaml | kubectl apply -f -

# 4. 重启控制器让配置生效
kubectl -n kueue-system rollout restart deploy/kueue-controller-manager
kubectl -n kueue-system rollout status deploy/kueue-controller-manager
kubectl -n kueue-system logs deploy/kueue-controller-manager --tail=100
```

要点：

- **配置在启动时读取**，改完必须重启；不要以为 ConfigMap 更新会自动热加载。
- Helm chart 的 `managerConfig.controllerManagerConfigYaml` 是**整块字符串**，Helm 不会和已有值做字段级合并。用 Helm 管理时，改一个字段也要提供完整块，否则会把其他配置覆盖掉。
- 改完先看启动日志。feature gate 依赖冲突、非法字段、保留资源名等问题通常会让控制器 crash-loop，而不是"部分功能失效"。
- 保留备份和变更记录；配置错误导致无法启动时，先回滚 ConfigMap 再排查。
- 用 `create cm ... --dry-run=client | kubectl apply` 时，生成的对象只带 name/namespace；如果原 ConfigMap 有 Helm/GitOps 标签、注解或 ownerReference，先确认 apply 不会把它们丢掉（必要时在本地文件里保留 `metadata.labels/annotations`）。
- 不要同时用 `kubectl edit` 和 Helm/GitOps 管理同一个 ConfigMap，否则下一次同步会把手工修改覆盖掉。

### 3.6.2 用静态检查提前发现配置错误

教程提供一个轻量检查器 `scripts/check_kueue_config.py`，它接受裸 `Configuration` 或 Helm 渲染出的 ConfigMap，重点检查最容易导致 crash-loop 或行为突变的问题：

- 顶层字段拼写错误、v1beta1/v1beta2 混用；
- 未知 feature gate，以及已知的依赖反转（例如 `TopologyAwareScheduling=false` 但某个 `TAS*` gate 为 true）；
- `featureGates` 与 `--feature-gates` 同时使用的冲突；
- `manageJobsWithoutQueueName` 与 `managedJobsNamespaceSelector` 的危险组合；
- 非法 duration、非正数的 Visibility 采样间隔；
- 资源变换里使用保留资源名 `pods`、重复 input、非法 strategy；
- 未知的 integration framework 与 fair-sharing strategy。

```bash
# 正常配置：0 errors
python scripts/check_kueue_config.py examples/config/kueue-config.yaml

# CI 里把 warning 也当失败
python scripts/check_kueue_config.py --strict examples/config/kueue-config.yaml

# JSON 输出，接入流水线
python scripts/check_kueue_config.py --json examples/config/kueue-config.yaml

# 如果 Deployment 还通过 --feature-gates 传了 gate，检查二者冲突
python scripts/check_kueue_config.py --feature-gates-flag examples/config/kueue-config.yaml
```

它不是 CRD schema 校验器：字段的完整类型、默认值和版本差异仍以目标版本的 API reference 为准。它的价值在于把"配置错误导致控制器起不来"提前到提交/CI 阶段，而不是等 rollout 之后看 crash-loop。

## 3.7 配额不够时会发生什么

把 ClusterQueue 的 CPU 改成 1，或提交一个需要 10 CPU 的 Job：

```yaml
spec:
  template:
    spec:
      containers:
      - name: big
        image: busybox
        command: ["sleep", "3600"]
        resources:
          requests:
            cpu: "10"
```

结果：

- Workload 保持 `Pending`；
- `QuotaReserved=False`，原因通常是 `Pending`/`InsufficientQuota`；
- Job 保持 suspended，不会创建 Pod；
- ClusterQueue 的 `PENDING WORKLOADS` 增加。

这就是 Kueue 的核心价值：**在 Pod 创建之前就把"不可能"挡住**，而不是让 10 个 Pod 去集群里碰运气。

### 3.7.1 Pending 的五种类型

"Pending" 不是一种原因，至少分五类：

| 类型 | 典型 Condition | 处理方向 |
|---|---|---|
| 配额不足 | `QuotaReserved=False, Reason=InsufficientQuota` | 等释放、调配额、抢占 |
| 结构性不可准入 | `QuotaReserved=False`、`Reason=Inadmissible`（selector/LimitRange/Flavor 缺失） | 改配置，等待无用 |
| 等外部检查 | `QuotaReserved=True, Admitted=False` | 看 AdmissionCheck/ProvisioningRequest |
| 等外部 gate | `AdmissionGated=True` | 找 gate owner，清除注解；不要加配额 |
| 退避中 | `requeueState.requeueAt` 在未来 | 等到时间点，不要反复重启控制器 |

先分类，再排障。把"结构性不可准入"当成"配额不足"去加配额，是最常见的无效操作。

## 3.8 安装并运行 MiniKueue

真实 Kueue 需要集群。为了在没有集群时也能理解算法，本教程提供了一个纯 Go 的模拟器。

```bash
cd mini-kueue
go test ./...
go run ./cmd/minikueue -list
```

输出示例（节选；当前共 28 个 CLI 场景 = 26 个引擎场景 + 2 个 MultiKueue 演示）：

```text
basic                FIFO admission, quota exhaustion, and release after completion
cohort-borrow        A ClusterQueue borrows unused cohort quota
preemption           Higher-priority workload preempts a lower-priority peer
reclaim              A nominal-quota owner reclaims capacity from a borrower
admission-check      Quota reservation followed by an external admission check
strict-fifo          StrictFIFO blocks a smaller job behind a larger head
best-effort-fifo     BestEffortFIFO skips an unadmittable head
tas                  4-GPU workload spans hosts but fills one rack
multikueue           Manager/worker dispatch with nomination, selection and cleanup
```

运行：

```bash
go run ./cmd/minikueue -scenario basic -ticks 4
```

你会看到每个 tick 的事件、Workload 状态、CQ 使用量和 Prometheus 风格的指标。第 14 章会逐段解释。

## 3.9 卸载

Kueue 会给 Workload、ClusterQueue、ResourceFlavor、AdmissionCheck、Topology、被管理的 Pod 加 finalizer。**不要先删控制器再删对象**，否则对象可能卡在 `Terminating`。

推荐顺序：

1. 删除提交的 Job/JobSet 等作业；
2. 删除残留的 Workload；
3. 删除 LocalQueue、ClusterQueue；
4. 删除 Cohort、ResourceFlavor、AdmissionCheck；
5. 删除 Topology；
6. 卸载 Kueue。

```bash
kubectl delete jobs --all -n default
kubectl delete workloads --all -n default
kubectl delete localqueues --all -n default
kubectl delete clusterqueue cluster-queue
kubectl delete resourceflavor default-flavor
kubectl delete -f "https://github.com/kubernetes-sigs/kueue/releases/download/${KUEUE_VERSION}/manifests.yaml"
```

如果已经卡住，参考官方 troubleshooting 文档，不要直接 `kubectl delete crd --force`。

## 3.10 常见问题

### Job 一直 suspended

检查：

```bash
kubectl get workload -n default -o wide
kubectl describe workload <name> -n default
kubectl get clusterqueue -o wide
kubectl get localqueue -n default -o wide
```

常见原因：

- Job 没有 `kueue.x-k8s.io/queue-name` 标签；
- LocalQueue 不存在或指向错误；
- ClusterQueue 不 Active；
- 配额不足；
- AdmissionCheck Pending；
- `namespaceSelector` 不匹配；
- 作业请求了 CQ 未覆盖的资源，但物理上仍然不够（Kueue 只管理被覆盖的资源）。

### Workload 有 QuotaReserved 但 Admitted=False

大概率在等 AdmissionCheck。看：

```bash
kubectl get workload <name> -n default -o jsonpath='{.status.admissionChecks}'
```

### ClusterQueue Active=False

```bash
kubectl describe clusterqueue <name>
```

看 Conditions 的 `Reason` 和 `Message`，通常是引用的 Flavor/AdmissionCheck/Cohort 不存在。如果引用的是 AdmissionCheck，还要看它自己的 `status.conditions[Active]`：检查控制器没有把 AdmissionCheck 标为 Active，也会让 CQ Inactive。

### 卸载卡住

先恢复控制器，再按顺序删除对象。Kueue 的 finalizer 需要控制器来清理。

## 3.11 本章小结

- 安装 Kueue 用 `kubectl apply --server-side` 或 Helm。
- 最小配置是 ResourceFlavor + ClusterQueue + LocalQueue。
- Job 通过 `kueue.x-k8s.io/queue-name` 标签接入。
- Workload 的 `RESERVED IN`、`ADMITTED`、`FINISHED` 是观察生命周期的三个关键列。
- 配额不足时，Kueue 在 Pod 创建前就挡住作业，这是它与"让调度器碰运气"的根本区别。
- Configuration 存在 `kueue-manager-config` ConfigMap 的 `controller_manager_config.yaml` 键里，启动时读取；改完要重启并检查启动日志。
- 用 `scripts/check_kueue_config.py` 在 CI 里检查 gate 依赖、危险组合和保留资源名，避免配置错误变成 crash-loop。
- 离线安装要同时准备镜像、清单/CRD、Helm chart 和 `kueuectl`，并用 digest 固定；升级时先同步镜像，再 apply CRD/清单。
- 没有集群时，可以用 `mini-kueue` 观察同样的语义。

---

[← 上一章：核心对象与资源模型](02-core-objects.md) | [返回目录](../README.md) | [下一章：准入生命周期 →](04-admission-lifecycle.md)
