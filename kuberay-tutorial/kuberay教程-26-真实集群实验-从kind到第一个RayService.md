# 第 26 章 真实集群实验：从 kind 到第一个 RayService

第 1–25 章用 MiniKubeRay 建立了控制平面的直觉。本章把同样的对象放到真实 Kubernetes 上：用 kind 建集群、用 Helm 安装 KubeRay、创建 RayCluster/RayJob/RayService/RayCronJob，并观察 Pod、Service、事件和日志。所有命令都可以直接复制执行。

> 实验环境：Linux/macOS，或 Windows + WSL2。需要 Docker、`kubectl`、`helm`、`kind`。资源建议至少 8 CPU / 16 GiB；GPU 实验另需 NVIDIA 节点。
>
> 版本：KubeRay v1.7.1，Ray 2.59.0，Kubernetes 1.30+。不同版本的 CRD/Chart 会有差异，先读 release notes。

## 26.1 为什么要做真实集群实验

MiniKubeRay 能验证状态机，但不能验证这些：

- CRD schema 是否接受你的 YAML；
- operator 的 RBAC 是否足够；
- 镜像、探针、init container 是否真的能启动；
- 网络策略、DNS、Service selector 是否连通；
- GPU device plugin、调度器、节点池是否匹配；
- Helm/CRD 升级是否会破坏现有对象。

真实集群实验的目标不是“跑通一个 demo”，而是建立一条**可重复的验证链路**：

```text
YAML -> API server schema -> operator reconcile -> Pod -> Ray runtime -> 用户请求
```

任何一环失败，都要能定位到具体对象和事件。

## 26.2 创建本地 Kubernetes 集群

### 26.2.1 安装工具

```bash
# 版本仅作参考，使用当前稳定版即可
kind version
kubectl version --client
helm version
docker info
```

### 26.2.2 kind 配置

`kind` 默认单节点，跑 Ray 多 Pod 场景容易资源不足。创建一个三节点集群，并把 Dashboard/Serve 端口映射到宿主机：

```yaml
# kind-kuberay.yaml
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
nodes:
  - role: control-plane
    # 只有把 Service 改成 NodePort/LoadBalancer 时才需要这些映射。
    # 默认样例是 ClusterIP/headless，用 kubectl port-forward 即可，不依赖 30080/30081。
    extraPortMappings:
      - containerPort: 30080
        hostPort: 30080
        protocol: TCP
      - containerPort: 30081
        hostPort: 30081
        protocol: TCP
  - role: worker
  - role: worker
```

```bash
kind create cluster --name kuberay-lab --config kind-kuberay.yaml
kubectl cluster-info --context kind-kuberay-lab
kubectl get nodes -o wide
```

如果只是想快速体验，`kind create cluster --name kuberay-lab` 也能用；后续把 `port-forward` 当作主要访问方式。

### 26.2.3 可选：metrics-server

Dashboard 的 CPU/内存图表和 `kubectl top` 需要 metrics-server：

```bash
kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml
# kind 的 kubelet 使用自签证书，通常需要加 --kubelet-insecure-tls
kubectl -n kube-system patch deployment metrics-server --type=json \
  -p='[{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-insecure-tls"}]'
kubectl -n kube-system rollout status deploy/metrics-server
kubectl top nodes
```

没有 metrics-server 不影响 Ray 本身，只影响资源图表。

## 26.3 安装 KubeRay Operator

### 26.3.1 Helm 安装

```bash
helm repo add kuberay https://ray-project.github.io/kuberay-helm/
helm repo update

helm install kuberay-operator kuberay/kuberay-operator \
  --version 1.7.1 \
  --namespace kuberay-system --create-namespace
```

检查：

```bash
kubectl -n kuberay-system get pods
kubectl -n kuberay-system logs deploy/kuberay-operator --tail=50
kubectl get crd | grep ray.io
```

期望看到四个 CRD：

```text
rayclusters.ray.io
raycronjobs.ray.io
rayjobs.ray.io
rayservices.ray.io
```

### 26.3.2 常用 Helm values

```yaml
# kuberay-values.yaml
# 只监听指定 namespace（多租户时降低权限）
watchNamespace: [team-a]

# 高可用：多副本 + leader election
replicas: 2
leaderElectionEnabled: true

# operator 指标
metrics:
  enabled: true
  serviceMonitor:
    enabled: false   # 有 Prometheus Operator 时再开

# alpha 功能显式开启；一次只开一个
featureGates:
  - name: RayCronJob
    enabled: true
```

```bash
helm upgrade --install kuberay-operator kuberay/kuberay-operator \
  --version 1.7.1 -n kuberay-system -f kuberay-values.yaml
```

> 注意：`watchNamespace` 会让 operator 只处理该 namespace；跨 namespace 的 RayCluster 不会被调和。生产上通常按租户拆 operator 或使用 ClusterRole + namespace 过滤。

### 26.3.3 原始清单安装（无 Helm）

```bash
# 推荐：kustomize 一次性安装 CRD + namespace + ServiceAccount + RBAC + Deployment
kubectl apply -k "github.com/ray-project/kuberay/ray-operator/config/default?ref=v1.7.1"
# config/default/kustomization.yaml 把 namespace 覆盖为 default；
# manager.yaml 里的 system 只是原始文件的值，不要照着它查。
kubectl -n default get deploy,pods
```

如果只 apply `manager.yaml`，会缺少 namespace、ServiceAccount、RoleBinding、leader-election Role 和 webhook 配置，operator 起不来。手动安装至少要包含：

- 四个 CRD；
- `config/rbac/role.yaml` + RoleBinding；
- ServiceAccount；
- 带 leader election 权限的 Role/Lease；
- `config/manager/manager.yaml`（注意它默认 namespace 是 `system`）。

原始清单适合理解组件边界；生产优先 Helm。

## 26.4 第一个 RayCluster

### 26.4.1 应用清单

使用本仓库的样例（或官方 quickstart）：

```bash
kubectl apply -f manifests/samples/ray-cluster.yaml
kubectl get rayclusters -w
```

期望：

```text
NAME               DESIRED WORKERS   AVAILABLE WORKERS   CPUS   MEMORY   GPUS   STATUS   AGE
raycluster-sample  2                 2                   5      10Gi     0      ready    45s
```

如果 `STATUS` 为空，先看：

```bash
kubectl describe raycluster raycluster-sample
kubectl get pods -l ray.io/cluster=raycluster-sample -o wide
kubectl -n kuberay-system logs deploy/kuberay-operator --tail=100
```

### 26.4.2 观察子资源

```bash
kubectl get pods -l ray.io/cluster=raycluster-sample --show-labels
kubectl get svc -l ray.io/cluster=raycluster-sample
kubectl get endpointslices -l kubernetes.io/service-name=raycluster-sample-head-svc
```

你应该看到：

- 一个 head Pod：名称以 `<cluster>-head` 开头。启用确定性 head 名时就是 `raycluster-sample-head`，否则带随机后缀；
- 两个 worker Pod：名称包含 group 名，形如 `raycluster-sample-small-group-worker-xxxxx`（具体后缀由 operator 生成，以 `kubectl get pods` 为准）；
- 一个 headless Service：`raycluster-sample-head-svc`；
- EndpointSlice 中的 head Pod IP。

> 不要在生产脚本里硬编码 Pod 名或随机后缀。用 `-l ray.io/cluster=...`、`-l ray.io/node-type=head/worker`、`-l ray.io/group=...` 选择 Pod；只有启用了确定性命名才依赖名字。后面命令里的 `HEAD_POD`/`WORKER_POD` 都这样解析：

```bash
HEAD_POD=$(kubectl get pod -l ray.io/cluster=raycluster-sample,ray.io/node-type=head \
  -o jsonpath='{.items[0].metadata.name}')
WORKER_POD=$(kubectl get pod -l ray.io/cluster=raycluster-sample,ray.io/node-type=worker \
  -o jsonpath='{.items[0].metadata.name}')
echo "$HEAD_POD $WORKER_POD"
```

### 26.4.3 访问 Ray Dashboard

```bash
kubectl port-forward svc/raycluster-sample-head-svc 8265:8265 10001:10001
# 浏览器打开 http://127.0.0.1:8265
```

Dashboard 里检查：

- Nodes：head + 2 workers；
- CPU/GPU/对象存储；
- Logs：每个节点的 `raylet.out`；
- Jobs：后续提交的作业。

也可以直接 `kubectl exec` 进 head：

```bash
HEAD_POD=$(kubectl get pod -l ray.io/cluster=raycluster-sample,ray.io/node-type=head \
  -o jsonpath='{.items[0].metadata.name}')
kubectl exec -it "$HEAD_POD" -- bash
ray status
ray list nodes
```

### 26.4.4 验证 Ray 资源与 Pod 资源一致

```bash
HEAD_POD=$(kubectl get pod -l ray.io/cluster=raycluster-sample,ray.io/node-type=head \
  -o jsonpath='{.items[0].metadata.name}')
kubectl exec -it "$HEAD_POD" -- ray status
kubectl get pod "$HEAD_POD" -o jsonpath='{.spec.containers[0].resources}'
```

如果 Pod 请求 4 核而 `ray status` 只看到 2 核，检查 `rayStartParams.num-cpus` 或顶层 `resources` 是否写错。第 6、9 章解释了对齐规则。

## 26.5 提交 RayJob

### 26.5.1 简单作业

```bash
kubectl apply -f manifests/samples/ray-job.yaml
kubectl get rayjobs -w
```

状态列会经历：

```text
NAME            JOB STATUS   DEPLOYMENT STATUS   ...
rayjob-sample                Initializing
rayjob-sample   PENDING      Running
rayjob-sample   RUNNING      Running
rayjob-sample   SUCCEEDED    Complete
```

查看 submitter 与日志：

```bash
kubectl get jobs,pods -l ray.io/originated-from-cr-name=rayjob-sample
kubectl logs job/rayjob-sample-submitter
kubectl exec <head-pod> -- ray job logs <jobId>
```

`status.jobId`、`status.dashboardURL`、`status.rayClusterName` 都可以在 `kubectl get rayjob -o yaml` 中看到。

### 26.5.2 InteractiveMode 实验

```bash
kubectl apply -f manifests/samples/ray-job-interactive.yaml
kubectl get rayjob rayjob-interactive -o yaml | grep -A3 status
```

在另一个终端提交作业：

```bash
kubectl exec -it <head-pod> -- \
  ray job submit --address http://127.0.0.1:8265 \
  --submission-id my-interactive-job -- python -c "print('hi')"
```

然后把 job ID 写入 RayJob 的 `spec.jobId`，观察状态转为 Running。

### 26.5.3 清理策略实验

修改 `shutdownAfterJobFinishes`/`ttlSecondsAfterFinished`，观察集群保留时间：

```bash
kubectl get rayjob rayjob-sample -o yaml | grep -A5 deletionStrategy
kubectl get rayclusters
```

第 10 章给出四种 submission mode 与 deletion rules 的完整对照。

## 26.6 部署 RayService

### 26.6.1 应用清单

```bash
kubectl apply -f manifests/samples/ray-service.yaml
kubectl get rayservices -w
```

期望：

```text
NAME               STATUS    ACTIVE CLUSTER          ENDPOINTS
rayservice-sample  Running   rayservice-sample-xxxx  2
```

### 26.6.2 访问 Serve

```bash
kubectl port-forward svc/rayservice-sample-serve-svc 8000:8000
curl http://127.0.0.1:8000/text
```

如果返回 503，检查：

```bash
kubectl get pods -l ray.io/cluster=<active-cluster> -o wide
kubectl get pods -l ray.io/serve=true
kubectl exec <head-pod> -- ray serve status
kubectl get svc rayservice-sample-serve-svc -o yaml
```

### 26.6.3 零停机升级实验

先区分两类变更：

- **只改 `serveConfigV2`**：KubeRay 会在 active 集群上原地应用配置，**不会**创建 pending 集群；观察 `serveConfig`/Deployment 滚动即可。
- **改 `rayClusterConfig`**（镜像、资源、副本、`rayStartParams` 等）：才会触发新集群/pending 流程（`NewCluster` 或 `NewClusterWithIncrementalUpgrade`）。

零停机实验用第二类：

1. 记录当前 active 集群名；
2. 修改 `rayClusterConfig`（例如镜像 tag 或 worker 资源）；
3. `kubectl apply`；
4. 观察 `status.pendingServiceStatus` 出现；
5. 新集群 Ready 后 `activeServiceStatus` 切换；
6. 旧集群在 `rayClusterDeletionDelaySeconds` 后被删除。

如果想验证“只改 serveConfigV2 不新建集群”，在第 4 步应看到 `pendingServiceStatus` 仍为空——这是预期行为，不是失败。

```bash
kubectl get rayservice rayservice-sample -o jsonpath='{.status.activeServiceStatus.rayClusterName}'
kubectl get rayservice rayservice-sample -o jsonpath='{.status.pendingServiceStatus}'
kubectl get rayclusters -l ray.io/originated-from-crd=RayService
```

同时持续请求 `curl`，观察是否出现 5xx。注意：NewCluster 升级在切换瞬间仍可能断开长连接；需要更平滑的流量迁移时使用增量升级（第 11 章）。

## 26.7 启用 RayCronJob

RayCronJob 默认被 feature gate 关闭。安装时开启：

```bash
helm upgrade kuberay-operator kuberay/kuberay-operator \
  --version 1.7.1 -n kuberay-system --reuse-values \
  --set 'featureGates[0].name=RayCronJob' \
  --set 'featureGates[0].enabled=true'
```

应用样例：

```bash
kubectl apply -f manifests/samples/ray-cronjob.yaml
kubectl get raycronjobs
kubectl get rayjobs -l ray.io/cronjob-name=raycronjob-sample
```

v1.7.1 的子 RayJob 名字由计划时刻决定：`<cronjob>-<scheduledTime.Unix()/60>`。同一分钟重复调和会得到同一个名字，第二次 create 因 AlreadyExists 被忽略。（v1.6.x 使用随机后缀，issue #4849 记录了重复创建窗口；升级到 v1.7.1 后该风险被确定性命名消除。）用 `kubectl get rayjobs -l ray.io/cronjob-name=raycronjob-sample` 观察子作业。

## 26.8 自动扩缩容实验

### 26.8.1 部署可扩缩容集群

```bash
kubectl apply -f manifests/samples/ray-cluster-autoscaling.yaml
kubectl get raycluster raycluster-autoscaled -w
```

初始 `replicas: 0`。提交一个需要更多 CPU 的作业：

```bash
kubectl exec -it <head-pod> -- python - <<'PY'
import ray, time
ray.init(address="auto")

@ray.remote(num_cpus=4)
def work(i):
    time.sleep(30)
    return i

futures = [work.remote(i) for i in range(12)]
print(ray.get(futures))
PY
```

观察：

```bash
kubectl get raycluster raycluster-autoscaled -o jsonpath='{.spec.workerGroupSpecs[0].replicas}'
kubectl get pods -l ray.io/cluster=raycluster-autoscaled
kubectl exec <head-pod> -c autoscaler -- tail -n 100 /tmp/ray/session_latest/logs/autoscaler.out
```

作业结束后等待 `idleTimeoutSeconds`，副本数回到 0。若没有缩容：

- 检查 `ray status` 是否仍有资源占用；
- 检查 autoscaler sidecar 日志；
- 检查 `minReplicas`；
- 检查是否有 actor 持有资源。

## 26.9 故障实验

### 26.9.1 删除 worker Pod

```bash
WORKER_POD=$(kubectl get pod -l ray.io/cluster=raycluster-sample,ray.io/node-type=worker \
  -o jsonpath='{.items[0].metadata.name}')
kubectl delete pod "$WORKER_POD"
kubectl get pods -l ray.io/cluster=raycluster-sample -w
```

期望控制器补一个同名或新索引的 Pod；Ray 任务可能重试。

### 26.9.2 删除 head Pod（无 GCS FT）

```bash
HEAD_POD=$(kubectl get pod -l ray.io/cluster=raycluster-sample,ray.io/node-type=head \
  -o jsonpath='{.items[0].metadata.name}')
kubectl delete pod "$HEAD_POD"
```

观察 worker 是否退出、控制器是否重建 head、作业是否失败。没有 GCS FT 时，集群元数据丢失，正在运行的作业通常需要重跑。

### 26.9.3 制造 Pending

把 worker 请求改成集群无法满足的 GPU 数：

```bash
kubectl patch raycluster raycluster-sample --type=json \
  -p='[{"op":"add","path":"/spec/workerGroupSpecs/0/template/spec/containers/0/resources/requests/nvidia.com~1gpu","value":"8"}]'
kubectl get pods -l ray.io/cluster=raycluster-sample
kubectl describe pod <pending-pod>
```

事件会给出 `Insufficient nvidia.com/gpu` 或 taint/affinity 原因。默认 upgradeStrategy 不会重建已有 Pod；如果旧 worker 还在，先删掉一个 worker 或把 upgradeStrategy 设为 `Recreate`，否则看不到新的 Pending Pod。记得改回。

### 26.9.4 镜像拉取失败

```bash
kubectl patch raycluster raycluster-sample --type=json \
  -p='[{"op":"replace","path":"/spec/headGroupSpec/template/spec/containers/0/image","value":"rayproject/ray:does-not-exist"}]'
# 默认 upgradeStrategy 不会自动重建已有 Pod；删掉 head 让它按新模板重建
HEAD_POD=$(kubectl get pod -l ray.io/cluster=raycluster-sample,ray.io/node-type=head \
  -o jsonpath='{.items[0].metadata.name}')
kubectl delete pod "$HEAD_POD"
kubectl describe pod -l ray.io/cluster=raycluster-sample,ray.io/node-type=head
```

观察 `ImagePullBackOff`，再改回正确镜像。注意 `upgradeStrategy` 的默认行为：模板变化不一定会自动重建已有 Pod，可能需要删 Pod 或设置 `Recreate`。

## 26.10 升级与清理

### 26.10.1 升级 operator

```bash
helm repo update
helm upgrade kuberay-operator kuberay/kuberay-operator --version 1.7.1 -n kuberay-system
kubectl -n kuberay-system rollout status deploy/kuberay-operator
```

### 26.10.2 升级 CRD

Helm 不会自动升级 `crds/`。单独执行：

```bash
kubectl apply --server-side -f \
  https://raw.githubusercontent.com/ray-project/kuberay/v1.7.1/ray-operator/config/crd/bases/ray.io_rayclusters.yaml
# 其余三个 CRD 同理
```

升级前先备份 CR：

```bash
kubectl get rayclusters,rayjobs,rayservices,raycronjobs -A -o yaml > kuberay-backup.yaml
```

### 26.10.3 清理

```bash
kubectl delete -f manifests/samples/ --ignore-not-found
helm uninstall kuberay-operator -n kuberay-system
kind delete cluster --name kuberay-lab
```

## 26.11 同一个实验在 MiniKubeRay 中复现

没有 Kubernetes 时，用简版实现跑同样的流程：

```bash
python -m minikuberay demo --fast
python -m minikuberay server
# 另一个终端
python -m minikuberay apply -f manifests/samples/ray-cluster.yaml
python -m minikuberay get rayclusters
python -m minikuberay get nodes -c raycluster-sample
python -m minikuberay describe raycluster/raycluster-sample
python -m minikuberay history list
```

MiniKubeRay 会创建同名的 Pod/Service/EndpointSlice/Event，并把事件写入 History Server；对比两边的 `describe` 输出，可以理解哪些字段是控制器的契约，哪些是真实运行时的细节。

## 26.12 Windows/WSL2 与 GPU 实验

### 26.12.1 Windows + WSL2

推荐在 WSL2 里跑 kind/helm/kubectl：

```bash
# WSL2 内
docker info
kind version
kubectl version --client
helm version
```

注意：

- Docker Desktop 要开启 WSL2 integration；
- 项目文件放在 WSL 文件系统（`~/...`）比 `/mnt/c/...` 快很多；
- 换行符用 LF，避免 shell 脚本 `\r` 错误；
- WSL2 内存限制在 `%UserProfile%\.wslconfig`：

```ini
[wsl2]
memory=16GB
processors=8
swap=4GB
```

- `kind` 的端口映射要写 `extraPortMappings`，否则宿主机访问不到；
- `kubectl port-forward` 在 WSL2 内启动，Windows 浏览器访问 `127.0.0.1` 通常可用；不行就用 `wsl hostname -I` 的 IP。

### 26.12.2 kind 上的 GPU

kind 节点是 Docker 容器，GPU 直通需要额外的 runtime/device 配置，通常不值得。建议：

- CPU 实验：kind；
- GPU 实验：真实云集群/本地裸金属，或支持 GPU 的托管 K8s；
- 只想验证 YAML：用 `kubectl apply --dry-run=server` 或 MiniKubeRay；
- 需要 device plugin：在真实节点上装 GPU Operator。

不要假设 `kind` 里 `nvidia.com/gpu` 一定可用；先：

```bash
kubectl get nodes -o custom-columns=NAME:.metadata.name,GPU:.status.allocatable.nvidia\.com/gpu
```

### 26.12.3 资源与 /dev/shm

WSL2/kind 的内存和 `/dev/shm` 都可能偏小：

```bash
kubectl exec <pod> -- df -h /dev/shm
kubectl exec <pod> -- free -h
```

如果 Ray 作业频繁 spill/OOM，先调大 WSL2 内存和 Pod memory，再调 `/dev/shm`。

## 26.13 实验检查表

- [ ] kind 集群至少 3 个节点，资源足够
- [ ] Helm 安装 operator，四个 CRD 存在
- [ ] RayCluster Ready，head/worker Service/EndpointSlice 正常
- [ ] port-forward 能打开 Dashboard
- [ ] RayJob 能完成，submitter 被清理
- [ ] RayService Ready，Serve 请求返回 200
- [ ] 升级 RayService，观察 active/pending 切换
- [ ] RayCronJob 创建子 RayJob
- [ ] autoscaler 扩容/缩容
- [ ] 删除 Pod/制造 Pending/镜像失败，能定位到事件
- [ ] operator 与 CRD 升级流程走通
- [ ] 清理所有资源

## 26.14 小结

- 真实集群实验验证的是 YAML→schema→controller→Pod→Ray 的完整链路。
- kind + Helm 是最低成本的复现方式；端口映射/port-forward 都能访问 Dashboard 和 Serve。
- 观察顺序：CR status → Pod → Event → operator/Ray 日志。
- CRD 升级与 operator 升级是两条独立流程；Helm 不升级 CRD。
- MiniKubeRay 可以复现同一套对象和事件，适合在真实集群不可用时练习排查。

下一章进入真实工作负载：Ray Train、Ray Data、Ray Tune 与 RLlib 在 KubeRay 上怎么跑。

→ [第 27 章 在 KubeRay 上运行 Ray Train / Data / Tune / RLlib](kuberay教程-27-在KubeRay上运行Ray-Train-Data-Tune-RLlib.md)
