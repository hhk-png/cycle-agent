# 第 34 章 MiniK8s 总体设计与工程结构

## 34.1 目标与非目标

MiniK8s 的目标：

- 用可读的 Python 复现 Kubernetes 的核心契约。
- 每个组件可单独测试。
- 能从真实 API/CLI 使用，而不是只跑单元测试。
- 代码量可控（约 9900 行），适合通读。
- 不依赖 Docker/root/云资源。

非目标：

- 生产级性能/高可用。
- 真实容器隔离。
- 完整 API 兼容。
- 替代 kind/minikube。

设计原则：

1. **API 优先**：所有协调通过对象和 watch，不直接 RPC。
2. **组件独立**：API Server、Scheduler、Controller、Kubelet 分开。
3. **可替换**：存储、运行时、网络、认证都可替换为真实实现。
4. **确定性**：模拟运行时不依赖真实时间/网络，测试可重复。
5. **显式简化**：每个简化都在文档中标注。

## 34.2 目录结构

```text
mini_k8s/
├── __init__.py
├── __main__.py              # python -m mini_k8s 启动开发集群
├── cluster.py               # 组件编排
├── client.py                # HTTP 客户端
├── errors.py                # ApiError
├── apimachinery/
│   ├── meta.py              # metadata、merge patch、uid、finalizer
│   ├── apply.py             # Server-Side Apply、字段所有权、managedFields
│   ├── quantity.py          # CPU/内存数量
│   ├── selectors.py         # label/field selector
│   └── cron.py              # cron 解析
├── api/
│   ├── registry.py          # GVK/GVR 注册表
│   └── server.py            # HTTP API、watch、子资源
├── store/
│   └── store.py             # 内存对象存储、resourceVersion、watch
├── admission/
│   └── admission.py         # 默认值、校验、Quota、PSA
├── auth/
│   ├── authn.py             # token 认证、SA token
│   └── authz.py             # RBAC
├── scheduler/
│   └── scheduler.py         # filter/score/preempt/extended resources
├── controllers/
│   ├── base.py              # 控制器运行时
│   ├── workloads.py         # Deploy/RS/STS/DS/Job/CronJob
│   ├── networking.py        # Service/Endpoints/Ingress
│   ├── gateway.py           # GatewayClass/Gateway/HTTPRoute 状态
│   ├── storage.py           # PV/PVC
│   ├── autoscaling.py       # Metrics/HPA
│   ├── policy.py            # PDB/Quota status
│   └── lifecycle.py         # Namespace/SA/Node/GC/CRD
├── kubelet/
│   └── kubelet.py           # 模拟运行时、探针、日志、exec
├── network/
│   ├── dns.py               # ClusterDNS
│   ├── ingress.py           # Ingress 路由
│   ├── gateway.py           # Gateway API L7 路由
│   ├── policy.py            # NetworkPolicy 判定
│   └── proxy.py             # ServiceProxy
├── coordination/
│   └── leader.py            # Lease 选举
└── cli/
    └── main.py              # mk8s
```

## 34.3 依赖方向

```text
cli/client → API Server → store
                │
                ├── admission
                ├── auth
                └── registry

controllers → store (直接，模拟 API 写入)
scheduler   → store
kubelet     → store

cluster.py 组装所有组件
```

为什么控制器直接操作 store 而不是 HTTP？

- 测试更快、更确定。
- 避免控制器也需要认证/序列化。
- 但这也意味着部分准入逻辑需要显式调用（`AdmissionChain.admit`），见第 35 章。

真实 Kubernetes 中控制器通过 API Server 写入，所有准入/审计/限流都生效。MiniK8s 只在**部分路径**调用 admission：`MiniCluster.apply`、ReplicaSet/StatefulSet/DaemonSet/Job 创建 Pod、CronJob 创建 Job 会走 `AdmissionChain.admit`；`DeploymentController` 创建 ReplicaSet、`StatefulSetController` 创建 PVC 是直接写 store，不经过准入。这是一个明确的简化，不能据此认为“控制器创建的所有对象都经过准入”。

## 34.4 线程模型

```text
主线程
├── API Server ThreadingHTTPServer（每请求一个线程）
├── Scheduler thread（每 50ms）
├── Controller threads（每个控制器一个，50ms–1s；多数 50–250ms，NodeLifecycle/Metrics 为 1s）
├── Kubelet threads（每节点一个，100ms tick）
└── Metrics controller（1s）
```

共享状态通过 `ResourceStore` 的 `RLock` 保护。每个组件读取 deep copy，避免外部修改内部对象。

停止：

```python
cluster.stop()
```

依次停止控制器、调度器、kubelet、API Server。没有复杂的优雅终止/租约释放；生产实现需要。

## 34.5 启动流程

`MiniCluster.__init__`：

1. 创建 `ResourceStore`、`ResourceRegistry`、`AdmissionChain`。
2. 创建认证器、RBAC authorizer、SA token issuer。
3. 创建 `APIServer`。
4. bootstrap：
   - namespace default/kube-system/kube-public。
   - ClusterRole/ClusterRoleBinding。
   - 默认 StorageClass。
5. 创建 Scheduler 和所有控制器。
6. 创建 ClusterDNS/NetworkSimulator/IngressRouter/ServiceProxy。
7. 为每个节点创建 Node 对象和 Kubelet。
8. 如果 `auto_start`，调用 `start()`。

`start()`：

1. 启动 API Server（可选）。
2. 如果 API 启动，立即创建 `ApiClient`（在 kubelet/scheduler/controllers 之前）。
3. 启动 kubelet。
4. 启动 scheduler。
5. 启动 controllers。

`stop()` 反向停止。

## 34.6 一个请求的生命周期（代码级）

```text
ApiClient.apply(deployment)
  → POST /apis/apps/v1/namespaces/default/deployments
  → APIHandler._create
      → AdmissionChain.admit (defaults/validation/quota)
      → ResourceStore.create (uid/rv/generation)
      → emit ADDED
  → DeploymentController (轮询 store)
      → 创建 ReplicaSet
  → ReplicaSetController
      → 创建 Pod
  → Scheduler
      → 写 spec.nodeName
  → Kubelet
      → 模拟容器、探针
      → 写 status
  → EndpointsController
      → 创建 Endpoints/EndpointSlice
```

这和真实 Kubernetes 的路径一致，只是组件间用 store 轮询而不是 informer。

## 34.7 工程化取舍

### 为什么用 Python

- 可读性高，适合教学。
- 标准库足够（HTTP、JSON、线程）。
- 测试快。
- 不需要编译/容器。

代价：

- GIL 限制并发。
- 性能远低于 Go。
- 类型系统弱（用 dataclass/typing 补足）。

### 为什么内存存储

- 无外部依赖。
- 测试可重复。
- 易于观察 resourceVersion/watch。

代价：

- 不持久化。
- 重启丢失。
- 无 Raft/多副本。

### 为什么模拟运行时

- 不需要 Docker/root。
- 行为确定。
- 可以在 CI 中跑。

代价：

- 不验证真实镜像/命令/网络。
- 不能替代真实集群测试。

### 为什么直接写 store

- 简化控制器。
- 测试快。

代价：

- 绕过部分 API 契约。
- 准入/审计需要显式处理。

## 34.8 扩展点

- **存储**：用 etcd 替换 `ResourceStore`，保留接口。
- **运行时**：用 CRI 客户端替换 Kubelet 的模拟状态机。
- **网络**：用 CNI/真实 Service 替换 NetworkSimulator/ServiceProxy。
- **认证**：接入 OIDC/webhook。
- **调度**：增加插件/自定义打分。
- **控制器**：用真实 informer/workqueue 替换轮询。

第 40 章详细讨论替换路线。

## 34.9 代码规模与质量

当前：

- 217 个测试。
- 18 个端到端示例。
- 文档检查脚本。
- 无第三方运行时依赖（PyYAML 除外）。

质量措施：

- 类型注解。
- 领域异常（ApiError）。
- 单元 + 集成 + E2E。
- 核心场景有 E2E 示例；SSA/managedFields、watch 书签、Lease 选举、访问审查等由测试覆盖，不都有独立示例脚本。
- 文档与代码同步检查。

## 34.10 从 MiniK8s 到上游源码的阅读路线

MiniK8s 是“把机制压缩到可读规模”，上游是“把机制做到生产规模”。两者对照阅读，效率最高。

建议的映射顺序：

| MiniK8s 模块 | 上游对应 | 先读什么 |
|---|---|---|
| `api/server.py` | `staging/src/k8s.io/apiserver`、`cmd/kube-apiserver` | 请求 handler 链、storage、watch cache |
| `store/store.py` | etcd + `staging/src/k8s.io/apiserver/pkg/storage` | `etcd3` 实现、resourceVersion |
| `apimachinery/meta.py` | `staging/src/k8s.io/apimachinery` | types、meta、conversion |
| `scheduler/scheduler.py` | `pkg/scheduler` | framework、queue、plugins |
| `controllers/workloads.py` | `pkg/controller/deployment` 等 | informer + workqueue + reconcile |
| `kubelet/kubelet.py` | `pkg/kubelet` | syncLoop、kuberuntime、prober |
| `network/proxy.py` | kube-proxy/eBPF 数据面 | 先看 Service/EndpointSlice 语义 |
| `admission/admission.py` | `staging/src/k8s.io/apiserver/pkg/admission` | plugin 链、webhook |
| `auth/authz.py` | `staging/src/k8s.io/apiserver/pkg/authorization` | RBAC evaluator |
| `coordination/leader.py` | `client-go/tools/leaderelection` | Lease 续约/接管 |

阅读方法：

1. **先读测试**：上游每个包的 `*_test.go` 往往比实现更能说明契约。
2. **从入口追一条路径**：例如从 `cmd/kube-apiserver/app/server.go` 追到一个 GET Pod 请求。
3. **不要从 `types.go` 开始通读**：字段太多，按需求查。
4. **用 `git grep` 找资源/动词**：例如 `git grep "pods/log"`。
5. **对照 KEP**：理解为什么这样实现，而不是只看代码。
6. **固定版本**：用 release tag（例如 `v1.31.0`）而不是 `master`，否则路径会漂移。

附录 G 给出更细的目录与关键文件清单。

## 本章小结

- MiniK8s 用清晰的分层和独立组件复现 Kubernetes 核心契约。
- 存储、调度、控制器、kubelet、网络、安全各自成模块，便于阅读和替换。
- 教学简化（内存存储、模拟运行时、轮询控制器）在文档中明确标注。
- 工程化不是“代码多”，而是边界清晰、可测试、可解释。
- 从 MiniK8s 到上游源码的映射见 34.10 与附录 G；先读最小闭环，再按路径追实现。

## 延伸阅读

- [第 35 章 API Server 与存储](35-apiserver-store.md)
- [第 36 章 调度器与控制器实现](36-scheduler-controllers.md)
- [第 37 章 Kubelet 与运行时模拟](37-kubelet-runtime.md)
- [第 40 章 与真实 Kubernetes 的差距](40-gap-and-replacement.md)
- [附录 G Kubernetes 上游源码导读](../appendices/G-source-reading.md)
