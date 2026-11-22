# 附录 G Kubernetes 上游源码导读

> 本附录基于 Kubernetes v1.31 的仓库结构。上游路径会随版本移动；阅读时固定 release tag，并以 `git grep` 为准，不要依赖博客里的旧路径。

## G.1 仓库地图

```text
kubernetes/
├── cmd/                    # 各组件 main
│   ├── kube-apiserver/
│   ├── kube-controller-manager/
│   ├── kube-scheduler/
│   ├── kubelet/
│   ├── kube-proxy/
│   └── kubeadm/
├── pkg/                    # 核心实现（非 staging 模块）
│   ├── scheduler/
│   ├── controller/
│   ├── kubelet/
│   ├── kubeadm/
│   ├── registry/           # 每种资源的 REST strategy
│   └── proxy/
├── staging/src/k8s.io/     # 可独立引用的模块
│   ├── api/
│   ├── apimachinery/
│   ├── apiserver/
│   ├── client-go/
│   ├── apiextensions-apiserver/
│   ├── kube-aggregator/
│   ├── component-base/
│   ├── controller-manager/
│   ├── cloud-provider/
│   ├── cri-api/
│   └── kubectl/
├── plugin/pkg/admission/   # 内置准入插件
├── test/
│   ├── e2e/
│   ├── integration/
│   └── conformance/
├── api/                    # OpenAPI/proto 定义
└── hack/                   # 代码生成/构建脚本
```

`staging/` 不是“第三方代码”，而是主仓库的一部分，通过 `replace` 指向本地路径；发布时同步到独立仓库（client-go、apimachinery 等）。

## G.2 先读哪一层

推荐顺序：

1. **API 类型**：`staging/src/k8s.io/api/<group>/<version>/types.go`。
2. **注册与默认值**：同目录 `register.go`、`defaults.go`、`zz_generated.defaults.go`。
3. **REST strategy**：`pkg/registry/<group>/<resource>/strategy.go`。
4. **存储**：`staging/src/k8s.io/apiserver/pkg/storage/`。
5. **控制器**：`pkg/controller/<resource>/`。
6. **调度器**：`pkg/scheduler/`。
7. **kubelet**：`pkg/kubelet/`。

不要从 `types.go` 顺序通读；按一个问题追一条路径。

## G.3 一次 API 请求的源码路径

以 `GET /api/v1/namespaces/default/pods/web` 为例：

```text
cmd/kube-apiserver/app/server.go
  → staging/src/k8s.io/apiserver/pkg/server/config.go
  → server/handler.go（handler chain）
  → filters: authentication → authorization → admission → audit
  → endpoints/groupversion.go / installer.go（路由注册）
  → pkg/registry/core/pod/storage/storage.go（REST）
  → pkg/registry/core/pod/strategy.go（Prepare/Validate/Canonicalize）
  → staging/src/k8s.io/apiserver/pkg/registry/generic/registry/store.go
  → staging/src/k8s.io/apiserver/pkg/storage/etcd3/store.go
  → etcd
```

关键文件：

- `staging/src/k8s.io/apiserver/pkg/server/handler.go`：请求进入点。
- `staging/src/k8s.io/apiserver/pkg/endpoints/handlers/get.go`：GET 处理。
- `staging/src/k8s.io/apiserver/pkg/endpoints/handlers/create.go/update.go/patch.go`：写路径。
- `staging/src/k8s.io/apiserver/pkg/registry/generic/registry/store.go`：通用 CRUD、finalizer、resourceVersion。
- `staging/src/k8s.io/apiserver/pkg/storage/cacher/`：watch cache。
- `pkg/registry/core/pod/strategy.go`：Pod 特有校验/默认值。

## G.4 API 类型与代码生成

一个 API 类型通常包含：

```text
staging/src/k8s.io/api/apps/v1/
├── types.go
├── register.go
├── defaults.go
├── zz_generated.deepcopy.go
├── zz_generated.defaults.go
└── zz_generated.conversion.go   # 内部版本转换
```

对应关系：

- `types.go`：外部版本（用户看到的 JSON/YAML）。
- `pkg/apis/<group>/<version>/`：内部版本 + conversion/defaulting。
- `zz_generated.*`：由 `hack/update-codegen.sh` 生成，不要手改。
- `+k8s:deepcopy-gen`、`+k8s:conversion-gen`、`+k8s:defaulter-gen` 等标记决定生成内容。
- validation 在 `pkg/apis/<group>/validation/`，不是自动生成。

读 CRD/Operator 时，这套结构对应 Kubebuilder 的 `api/v1/*_types.go` 与 `zz_generated.deepcopy.go`。

## G.5 调度器源码

```text
pkg/scheduler/
├── scheduler.go              # 调度循环
├── schedule_one.go           # 单个 Pod 的调度周期
├── framework/                # 扩展点接口/运行时
│   ├── interface.go
│   ├── runtime/
│   └── plugins/
├── queue/                    # ActiveQ/BackoffQ/UnschedulableQ
├── backend/cache/            # SchedulerCache
├── plugins/                  # 内置插件
│   ├── noderesources/
│   ├── nodeaffinity/
│   ├── interpodaffinity/
│   ├── topology/
│   ├── tainttoleration/
│   ├── volumerestrictions/
│   └── defaultpreemption/
└── eventhandlers/
```

阅读路径：

1. `schedule_one.go` 的 `SchedulePod`：看 PreFilter/Filter/Score/Reserve/Permit/Bind 顺序。
2. `framework/runtime/framework.go`：插件如何注册/调用。
3. `queue/scheduling_queue.go`：Pod 如何入队/退避/不可调度。
4. `backend/cache/cache.go`：节点/Pod 快照如何维护。
5. `plugins/defaultpreemption`：抢占受害者选择。

## G.6 控制器与 informer

一个典型控制器：

```text
pkg/controller/deployment/
├── deployment_controller.go   # NewController/Informer/Workqueue
├── sync.go                    # Reconcile 逻辑
├── rollout.go                 # 滚动更新
└── util/
```

通用模式：

```text
NewController:
  informer.Informer().AddEventHandler(...)  → enqueue key
  go wait.Until(worker, time.Second, stopCh)
worker:
  processNextWorkItem → syncHandler(key)
  success → Forget
  error   → AddRateLimited
```

相关库：

- `staging/src/k8s.io/client-go/tools/cache`：Reflector/DeltaFIFO/Indexer/SharedInformer。
- `staging/src/k8s.io/client-go/util/workqueue`：限速队列。
- `staging/src/k8s.io/client-go/tools/leaderelection`：Lease 选主。
- `staging/src/k8s.io/controller-manager`：控制器管理器启动/选主。

## G.7 kubelet 与 CRI

```text
pkg/kubelet/
├── kubelet.go                # syncLoop
├── kubelet_pods.go           # Pod 状态/卷/配置
├── kuberuntime/              # CRI 调用
├── prober/                   # 探针
├── volumemanager/            # 卷挂载
├── cm/                       # cgroup/CPU/内存管理
├── eviction/                 # 驱逐
├── status/                   # status 生成
└── server/                   # 10250 API
```

阅读路径：

1. `kubelet.go` 的 `syncLoopIteration`：Pod 增删改如何进入 `syncPod`。
2. `kuberuntime/kuberuntime_manager.go`：`SyncPod` 创建 sandbox/容器。
3. `prober/worker.go`：探针执行与结果处理。
4. `staging/src/k8s.io/cri-api/pkg/apis/runtime/v1/api.proto`：CRI 接口定义。
5. `pkg/kubelet/cm/cpumanager/`：CPU 绑核/拓扑。

## G.8 网络与存储

- Service/EndpointSlice：`pkg/controller/endpointslice/`、`pkg/proxy/`。
- kube-proxy iptables/IPVS：`pkg/proxy/iptables/`、`pkg/proxy/ipvs/`。
- NetworkPolicy：由 CNI 实现，Kubernetes 只提供 API；Calico/Cilium 源码各自仓库。
- CSI：`staging/src/k8s.io/csi-translation-lib/`、external-provisioner/attacher 等 sidecar 仓库。
- PV/PVC 控制器：`pkg/controller/volume/`。

## G.9 阅读工具与方法

```bash
git clone --depth 1 --branch v1.31.0 https://github.com/kubernetes/kubernetes
cd kubernetes

# 找资源注册
git grep -n "pods/log" -- pkg staging
# 找某个字段的处理
git grep -n "observedGeneration" -- pkg staging
# 找测试（契约）
git grep -n "func Test.*Deployment" -- pkg/controller/deployment
```

技巧：

- 用 `go doc`/IDE 跳转，但不要被接口层次绕晕；先看调用方。
- 测试文件往往比实现更清楚：`strategy_test.go`、`schedule_one_test.go`。
- 关注 `// +k8s:` 标记，它们决定生成代码和行为。
- 对照 KEP 的 “Design Details” 与 “Alternatives”。
- 一次只追一条路径（例如“创建一个 Pod 到 status 写回”），不要试图通读整个 `pkg/`。

## G.10 MiniK8s 与上游对照

| MiniK8s | 上游 | 差异 |
|---|---|---|
| `store/store.py` | etcd3 store + cacher | 无 Raft/持久化/watch cache |
| `api/server.py` | apiserver handler chain | 无 TLS/审计/APF/protobuf |
| `admission/admission.py` | admission plugins + webhook | 无外部 webhook/CEL |
| `scheduler/scheduler.py` | scheduler framework | 无插件体系/队列/缓存 |
| `controllers/*.py` | pkg/controller/* | 轮询 vs informer/workqueue |
| `kubelet/kubelet.py` | pkg/kubelet + CRI | 模拟 vs 真实运行时 |
| `network/*.py` | CNI/kube-proxy | 判定 vs 真实数据面 |

先读 MiniK8s 建立“最小闭环”，再按上表追上游同名概念，最后用真实集群验证行为。

## 延伸阅读

- [第 34 章 MiniK8s 总体设计](../chapters/34-minik8s-design.md)
- [第 35 章 API Server 与存储](../chapters/35-apiserver-store.md)
- [第 36 章 调度器与控制器实现](../chapters/36-scheduler-controllers.md)
- [第 37 章 Kubelet 与运行时模拟](../chapters/37-kubelet-runtime.md)
- [附录 E 延伸阅读](E-reading.md)
