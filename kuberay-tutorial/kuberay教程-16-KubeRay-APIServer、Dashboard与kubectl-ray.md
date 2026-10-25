# 第 16 章 KubeRay APIServer、Dashboard 与 kubectl-ray

除了 operator，KubeRay 还提供三种“人机接口”：APIServer、Dashboard、kubectl-ray。它们不参与核心调和，但决定了平台好不好用。

## 16.1 KubeRay APIServer

### 16.1.1 v1

- 早期组件，提供简化的 Ray 资源 API；
- 用于 UI/平台后端；
- 维护模式，不再加新功能。

### 16.1.2 v2（v1.4 alpha）

- 提供与 Kubernetes API 兼容的 HTTP 代理；
- 支持标准 Kubernetes client/OpenAPI；
- 可作为 Go library 嵌入；
- 目标：让平台用同一套 SDK 管理 Ray 资源；
- 仍然是 alpha，生产慎用。

### 16.1.3 典型用途

- 平台 UI 不想直接对接 kube-apiserver；
- 多集群聚合；
- 统一鉴权/审计；
- 给非 K8s 用户暴露简化 API。

### 16.1.4 v2 的部署与调用

```bash
helm install kuberay-apiserver kuberay/kuberay-apiserver \
  --version 1.7.1 -n kuberay-system
kubectl -n kuberay-system get svc -l app.kubernetes.io/name=kuberay-apiserver
# 常见 Service 名：kuberay-apiserver-service
```

chart 默认带 security proxy，直接 curl 可能 401。测试时可显式关闭 security（仅本地）：

```bash
helm upgrade kuberay-apiserver kuberay/kuberay-apiserver \
  -n kuberay-system --reuse-values --set security=null
# 或保留 security，在请求头带原始 token：Authorization: <token>（不要加 Bearer）
```

如果平台后端用 Kubernetes SDK，可以：

```bash
# KubeRay APIServer v2 通常监听 8888；通过 Service 转发后访问
kubectl -n kuberay-system port-forward svc/kuberay-apiserver-service 8888:8888
curl -H "Authorization: <token>" \
  http://127.0.0.1:8888/apis/ray.io/v1/namespaces/team-a/rayclusters

# 对照：kubectl proxy 代理的是 kube-apiserver，不是 KubeRay APIServer
kubectl proxy --port=8001
curl http://127.0.0.1:8001/apis/ray.io/v1/namespaces/team-a/rayclusters
```

或使用兼容的 client：

```python
from kubernetes import client, config
config.load_kube_config()
api = client.CustomObjectsApi()
clusters = api.list_namespaced_custom_object(
    group="ray.io", version="v1", namespace="team-a", plural="rayclusters"
)
```

v2 的目标是让平台用同一套 Kubernetes SDK/OpenAPI 管理 Ray 资源；它仍然是 alpha，API 可能变化。

认证/RBAC：

- APIServer 自己的 ServiceAccount 需要 CRD/Pod/Service 权限；
- 前端/后端到 APIServer 的认证由网关/代理负责；
- 不要让 APIServer 用 cluster-admin；
- 多租户时限制 namespace。

### 16.1.5 注意

- APIServer 不是 operator 的替代；
- 它最终仍然写 CR；
- 需要自己的 RBAC/认证；
- 多租户场景要限制 namespace；
- alpha 版本 API 可能变化。

## 16.2 KubeRay Dashboard

KubeRay Dashboard（v1.4 起，Experimental）：

- 集中查看多个 RayCluster/RayJob/RayService；
- 展示状态、事件、资源；
- 不是 Ray Dashboard 的替代；
- 不保证生产可用；
- 适合平台团队做内部工具的基础。

Ray Dashboard（8265）仍然是调试 Ray 运行时的主入口：节点、任务、actor、Serve、日志、指标。

## 16.3 kubectl-ray

v1.3 beta，v1.4 增强：

```bash
# 安装方式以官方文档为准（krew/二进制/包管理器）
kubectl krew install ray

# 查看（v1.7 插件用单数；以 kubectl ray --help 为准）
kubectl ray get cluster
kubectl ray get workergroup
kubectl ray get node
kubectl get rayjobs
kubectl get rayservices

# 创建
kubectl ray create cluster my-cluster --worker-replicas 2

# 扩缩容（不同插件版本参数不同，先看 help）
kubectl ray scale cluster my-cluster --worker-group workers --replicas 4

# 提交作业：从 RayJob YAML 创建（v1.7.1 没有 --cluster 参数）
kubectl ray job submit -f rayjob.yaml --working-dir ./code -- python train.py

# 或直接提交到已有 Dashboard（不创建 RayJob CR）
kubectl ray job submit --address http://<head-svc>:8265 \
  --working-dir ./code -- python train.py

# 日志：log 而不是 logs
kubectl ray log my-cluster --node-type head
```

v1.4 的改进：

- `scale` 命令；
- `get` 支持 nodes/workergroups；
- `create` 支持覆盖默认值、标签、注解、nodeSelector、ephemeral storage、ray start 参数、TPU、autoscaler 版本。

v1.7.1 的修复：

- 修复 port-forward 断线后状态不更新的问题；
- 具体命令/参数以 `kubectl ray --help` 为准。

### 16.3.1 原理

kubectl-ray 本质是：

1. 读取/创建 CR；
2. port-forward 到 head Dashboard；
3. 调用 Ray Job API；
4. 把结果格式化。

它不绕过 Kubernetes API，也不替代 operator。

## 16.4 自动化与 GitOps

### 16.4.1 GitOps

- 把 CR 放在 Git；
- Argo CD/Flux 同步；
- CI 做 schema 校验/策略检查；
- 环境用 Kustomize/Helm overlay；
- 不要把 Secret 明文放 Git。

### 16.4.2 CI 校验

- `kubectl apply --dry-run=server`；
- `kubeconform`/`kubeval`；
- OPA/Gatekeeper/Kyverno 策略；
- 本仓库的 `python check_docs.py` 是轻量替代。

### 16.4.3 平台 API

如果平台要提供“提交 Ray 作业”的 API：

- 调用 Kubernetes API 创建 RayJob；
- 或用 KubeRay APIServer v2；
- 自己做鉴权、配额、审计；
- 返回 jobId/namespace/console URL。

## 16.5 MiniKubeRay 的 APIServer

`minikuberay/apiserver/server.py` 提供 Kubernetes 风格 REST：

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/api/v1/namespaces/{ns}/pods` | 列 Pod |
| GET | `/api/v1/namespaces/{ns}/services` | 列 Service |
| GET | `/apis/ray.io/v1/namespaces/{ns}/rayclusters` | 列 RayCluster |
| POST | 同上 | 创建 |
| GET/PUT/DELETE | `.../{name}` | 读/更新/删除 |
| PUT | `.../{name}/status` | 更新 status |
| PATCH | `.../{name}` | merge patch |
| GET | `...?watch=true` | watch |
| GET | `.../rayjobs/{name}/logs` | 作业日志 |
| POST | `.../rayjobs/{name}/stop` | 停止作业 |
| GET | `/metrics` | Prometheus |
| GET | `/dashboard` | Dashboard |
| GET | `/healthz` `/readyz` `/version` | 健康/版本 |

认证：

```bash
python -m minikuberay server --token secret
curl -H "Authorization: Bearer secret" http://127.0.0.1:8080/apis/ray.io/v1/namespaces/default/rayclusters
```

只读：

```bash
python -m minikuberay server --read-only
```

## 16.6 MiniKubeRay 的 CLI

```bash
python -m minikuberay version
python -m minikuberay validate -f manifests/samples/ray-cluster.yaml
python -m minikuberay server
python -m minikuberay apply -f manifests/samples/ray-cluster.yaml --server http://127.0.0.1:8080
python -m minikuberay get rayclusters --server http://127.0.0.1:8080
python -m minikuberay get raycluster raycluster-sample -o yaml --server http://127.0.0.1:8080
python -m minikuberay describe raycluster/raycluster-sample --server http://127.0.0.1:8080
python -m minikuberay logs rayjob/hello-rayjob --server http://127.0.0.1:8080
python -m minikuberay scale raycluster/raycluster-sample workers=3 --server http://127.0.0.1:8080
python -m minikuberay job stop hello-rayjob --server http://127.0.0.1:8080
python -m minikuberay delete raycluster/raycluster-sample --force --server http://127.0.0.1:8080
python -m minikuberay demo --fast
```

它模拟了 kubectl/kubectl-ray 的核心操作，便于在没有 Kubernetes 的环境里练习。

## 16.7 Dashboard（MiniKubeRay）

启动 server 后打开 `http://127.0.0.1:8080/dashboard`：

- RayClusters：state、workers、CPU/GPU；
- RayJobs：job status、deployment status、cluster；
- RayServices：Ready、active cluster、endpoints；
- RayCronJobs：schedule、last schedule。

前端每 3 秒调用一次 REST API；没有构建步骤，方便阅读/修改。

## 16.8 设计建议

- 平台 UI 不要直接读 etcd；
- 用 namespace 做租户边界；
- 所有写操作走 CR；
- APIServer/Dashboard 只读 + 受控写；
- 审计每个提交者；
- 给用户展示“为什么没 Ready”（conditions/events），而不只是红色状态。

## 16.9 APIServer v2 的 API 兼容性

### 16.9.1 v1 与 v2 的定位

| 维度 | v1 | v2 |
|---|---|---|
| API 风格 | 自定义简化 REST | 兼容 Kubernetes API 的 HTTP 代理/SDK |
| 客户端 | 平台后端按文档对接 | 标准 Kubernetes client/OpenAPI |
| 资源覆盖 | 子集 | CRD/Pod/Service 等代理 |
| 成熟度 | 维护模式 | Alpha |
| 适用 | 已有 v1 集成 | 新平台/多集群 |

### 16.9.2 兼容性测试

如果你用 v2 作为平台 API：

```bash
# 1. 资源发现
curl -s http://<apiserver>:8888/apis/ray.io/v1 | jq

# 2. list/get/create 与直接访问 kube-apiserver 对比
curl -s http://<apiserver>:8888/apis/ray.io/v1/namespaces/team-a/rayclusters | jq '.items | length'
kubectl get rayclusters -n team-a -o json | jq '.items | length'

# 3. status subresource
curl -s http://<apiserver>:8888/apis/ray.io/v1/namespaces/team-a/rayclusters/<name>/status | jq

# 4. watch
curl -N 'http://<apiserver>:8888/apis/ray.io/v1/namespaces/team-a/rayclusters?watch=true'
```

测试矩阵：

- list/get/create/update/patch/delete；
- status/finalizers；
- label/field selector；
- watch（含 410/断线重连）；
- 认证/授权/多租户；
- 错误码（400/401/403/404/409/422/500）。

### 16.9.3 不要把 v2 当稳定 API

- v2 仍是 alpha，路径/字段可能变化；
- 平台要对 v2 做版本适配层；
- 生产关键路径优先直接使用 Kubernetes API；
- 如果必须用 v2，锁定版本并做契约测试。

## 16.10 小结

- APIServer v1 维护、v2 alpha；Dashboard experimental；kubectl-ray beta。
- 它们都是 CR 的客户端，不是 operator 的替代。
- GitOps/CI 校验是生产管理 CR 的主流方式。
- MiniKubeRay 提供 REST + CLI + Dashboard，用于练习同样的操作。

下一章生产运行手册。

→ [第 17 章 生产运行手册](kuberay教程-17-生产运行手册.md)
