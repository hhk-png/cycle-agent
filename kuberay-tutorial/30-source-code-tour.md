# 第 30 章 KubeRay 源码导读与二次开发

本章给出一条读 KubeRay 源码的路径：先理解 controller-runtime 的骨架，再按“类型 → 默认值/校验 → 调和 → Pod/Service 构造 → 状态”的顺序读四个控制器，最后看如何加字段、跑测试、提 PR。目标不是逐行读完，而是能在需要时快速定位。

> 源码基线：KubeRay v1.7.1。路径以该版本仓库为准；master 可能已经重构。

## 30.1 仓库地图

```text
kuberay/
├── ray-operator/
│   ├── apis/ray/v1/                 # CRD Go 类型（API 契约）
│   │   ├── raycluster_types.go
│   │   ├── rayjob_types.go
│   │   ├── rayservice_types.go
│   │   ├── raycronjob_types.go
│   │   └── zz_generated.deepcopy.go
│   ├── controllers/ray/
│   │   ├── raycluster_controller.go
│   │   ├── rayjob_controller.go
│   │   ├── rayservice_controller.go
│   │   ├── raycronjob_controller.go
│   │   ├── networkpolicy_controller.go   # NetworkPolicy（不是 common/）
│   │   ├── raycluster_mtls_controller.go # mTLS 证书（不是 common/cert.go）
│   │   ├── common/
│   │   │   ├── pod.go               # Pod 构造
│   │   │   ├── service.go           # Service/DNS
│   │   │   ├── rbac.go              # autoscaler RBAC
│   │   │   ├── gcs_ft.go            # GCS FT PVC
│   │   │   ├── ingress.go
│   │   │   ├── association.go
│   │   │   ├── job.go
│   │   │   ├── openshift.go
│   │   │   └── ...
│   │   └── utils/
│   ├── pkg/features/features.go     # feature gates
│   └── config/
│       ├── crd/bases/               # 生成的 CRD YAML
│       ├── rbac/
│       ├── manager/
│       └── samples/
├── apiserver/                       # v1 / v2
├── dashboard/
├── historyserver/
├── kubectl-plugin/
├── helm-chart/
└── docs/
```

阅读顺序建议：

1. `apis/ray/v1/*_types.go`：字段、默认值、状态枚举；
2. `pkg/features/features.go`：哪些功能默认开启；
3. `controllers/ray/raycluster_controller.go`：核心调和；
4. `controllers/ray/common/pod.go`：CR → Pod；
5. `rayjob_controller.go` / `rayservice_controller.go`；
6. `historyserver/` / `apiserver/` / `kubectl-plugin/`。

## 30.2 controller-runtime 骨架

KubeRay 使用 [controller-runtime](https://github.com/kubernetes-sigs/controller-runtime)。一个控制器的生命周期：

```text
Manager
  ├── Scheme（类型注册）
  ├── Cache（informer 本地缓存）
  ├── Client（读写 API server）
  └── Controller
        ├── For(&RayCluster{})
        ├── Owns(&Pod{})
        ├── Owns(&Service{})
        ├── Watches(...)
        └── Reconcile(ctx, req)
```

关键概念：

| 概念 | 作用 |
|---|---|
| `Manager` | 启动 cache/controller/webhook/metrics |
| `Cache` | watch API server，本地读；最终一致 |
| `Client` | 读 cache 或直接读 API server；写 API |
| `Reconcile` | 处理一个 `namespace/name` |
| `Owns` | 子资源变化触发父对象调和 |
| `Predicate` | 过滤事件（generation/label） |
| `WorkQueue` | 去重、限速、重试 |
| `RequeueAfter` | 定时重新调和 |

### 30.2.1 level-triggered

Reconcile 不依赖事件内容，只读当前对象和子资源，计算期望状态。因此：

- 重复调和必须幂等；
- 缓存可能落后，不能假设“刚创建的 Pod 已经在 cache 里”；
- 冲突（resourceVersion）要重试；
- 外部状态（Ray job/Serve）要轮询。

### 30.2.2 为什么需要 scale expectations

刚创建 Pod 后，informer cache 可能还没收到 ADD 事件。下一轮 Reconcile 看到“还缺 Pod”，会重复创建。KubeRay 用 `scale_expectations` 记录“我已经期望创建/删除了哪些 Pod”，在 cache 追上之前跳过该 group。这是读源码时最容易被忽略、但生产必须有的细节。

## 30.3 RayCluster 控制器

文件：`ray-operator/controllers/ray/raycluster_controller.go`。

### 30.3.1 Reconcile 入口

```go
func (r *RayClusterReconciler) Reconcile(ctx context.Context, request ctrl.Request) (ctrl.Result, error) {
    instance := &rayv1.RayCluster{}
    if err := r.Get(ctx, request.NamespacedName, instance); err != nil {
        return ctrl.Result{}, client.IgnoreNotFound(err)
    }
    return r.rayClusterReconcile(ctx, request, instance)
}
```

### 30.3.2 rayClusterReconcile 的顺序

```text
1. managedBy 检查（外部控制器）
2. setDefaults
3. ValidateRayClusterMetadata / ValidateRayClusterSpec
4. mTLS/cert-manager 检查
5. ValidateRayClusterUpgradeOptions / Status
6. GCS FT finalizer
7. deletionTimestamp -> 删除分支
8. reconcileAutoscalerServiceAccount/Role/RoleBinding
9. reconcileIngress
10. reconcileAuthSecret
11. reconcileHeadService / HeadlessService / ServeService
12. reconcileGCSStoragePVC
13. reconcilePods
14. calculateStatus
15. updateStatus
16. RequeueAfter
```

顺序原则：**依赖先行**。Service/RBAC/Secret 必须早于 Pod。

### 30.3.3 setDefaults

`setDefaults` 补齐：

- `rayVersion`；
- worker group 的 `replicas/minReplicas/maxReplicas/numOfHosts`；
- autoscaler options；
- GCS FT 默认后端；
- head service 类型。

CRD schema 里也有一部分默认值；两层默认值要一起看。

### 30.3.4 ValidateRayClusterSpec

校验包括：

- 名称/长度/DNS；
- head 容器与 `ray` 容器位置；
- min/max 关系；
- autoscaling 与 worker suspend 的互斥；
- GCS FT 字段互斥；
- auth/TLS/NetworkPolicy/History Server 的前置条件；
- `managedBy`/`upgradeStrategy` 枚举；
- 顶层 `resources`/`labels` 与 `rayStartParams` 冲突。

校验失败通常发 Event + condition，不创建 Pod。

### 30.3.5 reconcilePods

核心逻辑：

```text
head:
  list(head pods)
  if len == 1: maybe delete
  if len == 0: create
  if len > 1: error

worker group (single-host):
  list pods
  delete unhealthy
  handle workersToDelete
  desired = clamp(replicas, min, max) * numOfHosts
  diff > 0 -> create
  diff < 0 -> delete (unless autoscaler owns victims)

worker group (multi-host):
  group pods by ray.io/worker-group-replica-name
  unhealthy/incomplete replica -> delete whole replica
  scale whole replicas
  create each host with replica/host labels
```

### 30.3.6 calculateStatus

统计：

- head/worker Pod 的 desired/ready/available；
- CPU/Memory/GPU；
- endpoints/head；
- conditions；
- observedGeneration/lastUpdateTime。

只在变化时写 status，避免 watch 风暴。

## 30.4 Pod 构造

文件：`controllers/ray/common/pod.go`。

关键函数：

| 函数 | 作用 |
|---|---|
| `BuildPod` | 从 PodTemplate 生成 Pod |
| `setMissingRayStartParams` | 合并 `rayStartParams` 与顶层 resources/labels |
| `generateRayStartCommand` | 生成 `ray start` 命令 |
| `setContainerEnvVars` | 注入 KubeRay 环境变量 |
| `addEmptyDir` | `/dev/shm` |
| `initLivenessAndReadinessProbe` | readiness/liveness |
| `setInitContainerEnvVars` | `wait-gcs-ready` 的环境 |
| `configureTLS` | mTLS 证书 |
| `BuildGCSStoragePVC` | RocksDB PVC |

阅读时关注：

- 用户 command 是否被覆盖（`ray.io/overwrite-container-cmd`）；
- head/worker 的 address；
- 端口列表；
- 资源/env 的优先级；
- 多主机标签。

## 30.5 RayJob 控制器

文件：`controllers/ray/rayjob_controller.go`。

### 30.5.1 状态机

```text
New -> Initializing -> Running -> Complete/Failed
                         |            |
                         +-> Retrying +-> Suspended
```

两套状态：

- `JobStatus`：Ray 作业状态（PENDING/RUNNING/SUCCEEDED/FAILED/STOPPED）；
- `JobDeploymentStatus`：KubeRay 部署状态（Initializing/Running/Complete/Failed/...）。

### 30.5.2 提交模式

| 模式 | 关键函数/资源 |
|---|---|
| K8sJobMode | 创建 submitter Kubernetes Job |
| HTTPMode | 调 Dashboard `/api/jobs/` |
| SidecarMode | head Pod 注入 submitter sidecar |
| InteractiveMode | 等用户写 `spec.jobId` |

### 30.5.3 清理

- `shutdownAfterJobFinishes` + TTL；
- `deletionStrategy.deletionRules`；
- `DeleteCluster/DeleteWorkers/DeleteSelf/DeleteNone`；
- finalizer 停止 Ray job。

阅读时画一张状态转移图，标出“哪些状态会重试、哪些不会”。

## 30.6 RayService 控制器

文件：`controllers/ray/rayservice_controller.go`。

### 30.6.1 关键函数

| 函数 | 作用 |
|---|---|
| `calculateStatus` | active/pending 状态 |
| `reconcileServe` | 通过 Dashboard 部署 Serve |
| `updateServeDeployment` | 更新 deployment |
| `rayClusterSpecForHashing` / `isClusterSpecHashEqual` | revision hash |
| `createHTTPRoute` / `reconcileHTTPRoute` | 增量升级的 HTTPRoute |
| `reconcileServices` | 维护/切换 Serve Service selector |
| `cleanUpRayClusterInstance` | 删除旧集群 |

### 30.6.2 active/pending

- active 接流量；
- pending 预热；
- 新集群 Serve 全部 RUNNING 才切换；
- 旧集群延迟删除；
- 回滚时删除 pending。

### 30.6.3 增量升级

- 创建 Gateway/HTTPRoute；
- pending `target_capacity` 从 0 升到 maxSurge；
- 按 step/interval 改 `backendRefs` 权重；
- 100% 后 promote。

阅读时对照第 11 章的状态图。

## 30.7 RayCronJob 控制器

文件：`controllers/ray/raycronjob_controller.go`。

- 解析 cron/timeZone；
- 计算下一次调度（lastScheduleTime）；
- 确定性子作业名 `<cronjob>-<unix minute>`；
- ownerReference；
- suspend。

没有 concurrencyPolicy/history limits；这些要上层实现。

## 30.8 Feature gates

文件：`pkg/features/features.go`。

```go
var (
    RayClusterStatusConditions = featuregate.Feature("RayClusterStatusConditions")
    RayJobDeletionPolicy       = featuregate.Feature("RayJobDeletionPolicy")
    RayMultiHostIndexing       = featuregate.Feature("RayMultiHostIndexing")
    RayServiceIncrementalUpgrade = featuregate.Feature("RayServiceIncrementalUpgrade")
    RayCronJob                 = featuregate.Feature("RayCronJob")
    ...
)
```

每个 gate 有默认值和 stage。读源码时先看 gate，再看实现，能解释“为什么这个字段在我的版本不生效”。

## 30.9 如何加一个字段

以给 RayCluster 加 `spec.foo` 为例：

1. **类型**：`apis/ray/v1/raycluster_types.go` 加字段 + json tag + kubebuilder 注解；
2. **deepcopy**：运行 `make generate`（生成 `zz_generated.deepcopy.go`）；
3. **CRD**：运行 `make manifests`（生成 `config/crd/bases/*.yaml`）；
4. **默认值**：CRD 的 `default` 或 controller 的 `setDefaults`；
5. **校验**：`ValidateRayClusterSpec`；
6. **调和**：在 `rayClusterReconcile`/`reconcilePods`/`common/pod.go` 使用；
7. **status**：如果需要暴露，加字段并更新 `calculateStatus`；
8. **测试**：单元测试 + envtest/e2e；
9. **文档**：API reference、sample、release notes；
10. **兼容性**：旧对象/旧 operator 的行为。

### 30.9.1 kubebuilder 注解

```go
// +kubebuilder:validation:Minimum=0
// +kubebuilder:default=1
// +optional
Foo *int32 `json:"foo,omitempty"`
```

- `+optional` 与指针；
- `+kubebuilder:validation:*` 生成 OpenAPI 校验；
- `+kubebuilder:default` 生成 CRD 默认值；
- `+kubebuilder:pruning:PreserveUnknownFields` 谨慎使用。

### 30.9.2 兼容性

- 加字段通常向后兼容；
- 删字段/改语义是 breaking change；
- CRD 只增不减，但字段弃用要有迁移路径；
- 新 feature gate 默认关闭，观察一个版本再默认开启。

## 30.10 构建与测试

```bash
# 代码生成
make generate
make manifests

# 单元测试
go test ./ray-operator/...

# 全量
make test

# 构建镜像
make docker-build IMG=my-registry/kuberay-operator:dev

# 部署到 kind
make deploy IMG=my-registry/kuberay-operator:dev

# e2e（需要集群）
make test-e2e
```

测试分层：

| 层 | 工具 | 覆盖 |
|---|---|---|
| 单元 | `go test` | 默认值/校验/构造函数 |
| envtest | controller-runtime envtest | Reconcile + fake API server |
| e2e | kind/真实集群 | Pod/Ray 运行时 |
| 集成 | Helm/CRD | 安装/升级 |

## 30.11 贡献流程

1. 搜 issue/REP，确认没有被讨论过；
2. 开 issue 描述问题/提案；
3. 小步 PR：一个功能一个 PR；
4. 加测试（单元 + e2e，视改动）；
5. 更新文档/sample/release note；
6. 处理 review；
7. 合并后观察 CI/发布。

约定：

- commit message 清晰；
- 不混入无关格式化；
- breaking change 在 PR 描述中标注；
- feature gate 默认关闭；
- 保持 API 向后兼容。

## 30.12 调试源码中的常见问题

| 现象 | 可能原因 |
|---|---|
| 改了 CRD 但字段不生效 | 没重新 apply CRD/没重启 operator |
| 默认值不生效 | CRD default 与 controller default 冲突 |
| Reconcile 不触发 | Owns/Watches 没注册；predicate 过滤 |
| 重复创建 Pod | 没处理 scale expectations/cache 延迟 |
| status 抖动 | lastTransitionTime/时间戳每次更新 |
| finalizer 卡住 | 清理逻辑没有幂等/超时 |
| webhook 阻塞 | 证书/Service 未就绪 |

## 30.13 与 MiniKubeRay 的对照

| KubeRay | MiniKubeRay |
|---|---|
| `raycluster_controller.go` | `minikuberay/controllers/raycluster.py` |
| `common/pod.go` | `minikuberay/controllers/builders.py` |
| `rayjob_controller.go` | `minikuberay/controllers/rayjob.py` |
| `rayservice_controller.go` | `minikuberay/controllers/rayservice.py` |
| `raycronjob_controller.go` | `minikuberay/controllers/raycronjob.py` |
| `features.go` | `DEFAULT_FEATURE_GATES` |
| CRD YAML | `manifests/crds/` |
| envtest/e2e | `tests/` + `check_docs.py` |

读 MiniKubeRay 的好处是可以在 Python 里打断点、改状态、快速验证；读 Go 源码的好处是看到真实的 client/cache/并发/错误处理。

## 30.14 十个必读函数

如果只有半天读源码，按这张表定位：

| # | 文件/函数 | 为什么读 |
|---|---|---|
| 1 | `apis/ray/v1/raycluster_types.go` | API 契约、字段、默认值 |
| 2 | `controllers/ray/raycluster_controller.go::rayClusterReconcile` | 调和顺序 |
| 3 | `controllers/ray/common/pod.go::BuildPod` | CR → Pod |
| 4 | `controllers/ray/common/pod.go::generateRayStartCommand` | 命令/参数 |
| 5 | `controllers/ray/common/service.go::BuildServiceForHeadPod` | Service/DNS |
| 6 | `controllers/ray/rayjob_controller.go::Reconcile` | 作业状态机 |
| 7 | `controllers/ray/rayservice_controller.go::reconcileServe` | Serve 部署 |
| 8 | `controllers/ray/rayservice_controller.go::createHTTPRoute` | 增量升级 |
| 9 | `controllers/ray/raycronjob_controller.go::Reconcile` | 定时调度 |
| 10 | `pkg/features/features.go` | 默认开启的功能 |

读每个函数时问四个问题：

```text
1. 输入是什么？读了哪些子资源？
2. 输出是什么？写了哪些对象/status？
3. 失败会怎样？重试/事件/condition？
4. 幂等吗？重复执行会多创建吗？
```

### 30.14.1 读源码的“三遍法”

1. **第一遍看形状**：只读函数签名/注释/调用关系，不抠细节；
2. **第二遍看状态**：标出所有写 status/子资源的地方；
3. **第三遍看失败**：标出所有 error 分支/重试/清理。

不要从 `main.go` 开始逐行读；先找 `Reconcile`，再追它调用的函数。

## 30.15 练习

1. 在 KubeRay 源码中找到 `reconcileMultiHostWorkerGroup`，画出它的决策流程。
2. 给 MiniKubeRay 的 RayCluster 加一个 `spec.foo` 字段，走完类型/默认/校验/调和/测试。
3. 用 envtest 写一个 RayCluster Reconcile 测试。
4. 找到 `RayCronJob` 的 feature gate，解释默认关闭的影响。
5. 给一个 feature 写 REP/issue，说明动机、API、兼容性、测试计划。

## 30.16 “master 能用、release 不能用”怎么定位

这是读源码/二次开发最常见的问题。按下面顺序排除：

### 30.16.1 先确认版本

```bash
git describe --tags --always
git log --oneline -5
go version
```

确认你读的代码、构建的镜像、集群里的镜像三者一致。

### 30.16.2 对比 tag

```bash
git log --oneline v1.7.1..master -- ray-operator/controllers/ray/...
git diff v1.7.1..master -- ray-operator/apis/ray/v1/...
```

重点看：

- 新字段是否只在 master 的 API 类型里；
- feature gate 默认值是否变化；
- CRD 是否更新；
- 默认值/校验是否变化。

### 30.16.3 检查 feature gate

```bash
kubectl -n kuberay-system get deploy kuberay-operator -o yaml | grep -A2 args
kubectl -n kuberay-system logs deploy/kuberay-operator | grep -i feature
```

master 上的功能可能默认关闭，或 gate 名称不同。

### 30.16.4 检查 CRD

```bash
kubectl get crd rayclusters.ray.io -o jsonpath='{.metadata.annotations}'
kubectl explain raycluster.spec --recursive | grep -i <field>
```

字段不存在 = CRD 没升级/被裁剪，不是控制器 bug。

### 30.16.5 检查构建

- 镜像是否用了旧代码；
- `make generate/manifests` 是否跑过；
- 是否用了 `-tags`/不同 Go 版本；
- 是否把本地改动带进了构建。

### 30.16.6 最小复现

1. 用同一个 tag 构建；
2. 用最小 CR；
3. 开 `--zap-log-level=debug`；
4. 对比 master 与 release 的 Reconcile 日志；
5. 用 `git bisect` 定位引入点（如果是回归）。

## 30.17 小结

- KubeRay = controller-runtime + 四个控制器 + common 构造器 + feature gates。
- 读源码顺序：类型 → 默认/校验 → 调和 → Pod/Service → status。
- 加字段要同步类型、生成代码、CRD、默认、校验、调和、测试、文档。
- scale expectations、幂等、status 抖动是生产级控制器的关键细节。
- MiniKubeRay 是源码的“可调试镜像”，适合先建立直觉。

下一章：升级、迁移、离线安装与灾难恢复。

→ [第 31 章 升级、迁移、离线安装与灾难恢复](31-upgrade-migration-dr.md)
