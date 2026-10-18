# 第 37 章 生产故障案例与复盘

前面各章讲的是“系统应该怎样工作”。本章讲“它实际怎样坏掉”。下面 14 个案例都来自 KubeRay 生产环境的高频模式：每个案例给出症状、时间线、根因、诊断命令、修复和预防。你可以把它们当作 on-call 的剧本，也可以当作故障演练的题库。

> 说明：案例经过抽象，不指向任何具体公司或集群。命令中的名称、命名空间、镜像按你的环境替换。
>
> 其中 11 个控制面场景已经做成可运行实验：`python examples/incident_lab.py`。先跑实验建立直觉，再用本章的命令在真实集群复现数据面问题。

## 37.1 复盘的固定结构

一次合格的复盘至少回答：

```text
1. 影响：哪些用户/作业/服务受影响，持续多久？
2. 时间线：什么时候发生、发现、缓解、恢复？
3. 根因：直接原因 + 系统性原因（为什么没被拦住）？
4. 诊断：用了哪些信号？哪些信号缺失/误导？
5. 修复：临时缓解 vs 永久修复。
6. 预防：测试、监控、配额、流程、文档。
7. 行动项：负责人、截止时间、验收标准。
```

不要写“人为失误”就结束。人为失误通常意味着系统缺少防呆：

- 没有 dry-run；
- 没有配额；
- 没有告警；
- 没有升级检查表；
- 没有回滚演练。

## 37.2 案例 1：`/dev/shm` 太小导致对象存储崩溃

### 现象

- Ray 作业运行几分钟后大量 task 失败；
- worker 日志出现 `PlasmaStoreFull`、`ObjectStoreFullError`、`out of memory`；
- 节点内存看起来还有很多；
- Pod 没有 OOMKilled。

### 时间线

```text
t0    作业开始，读取大 Parquet，对象写入 Plasma
t+2m  对象存储达到 /dev/shm 上限
t+3m  task 开始 spill/失败，重试
t+5m  重试放大，更多对象，雪崩
t+8m  SRE 看到 task 失败率告警
```

### 根因

KubeRay 默认会给 Ray 容器挂载 memory-backed `emptyDir`（卷名 `shared-mem`）到 `/dev/shm`，`sizeLimit` 按 Ray 容器的 memory request/limit 推导，通常与 memory limit 1:1。事故不是“默认值太小”，而是下面几种情况：

- 用户模板覆盖/移除了这个卷，容器默认 `/dev/shm` 只有 64MB；
- `rayStartParams.object-store-memory` 或 Ray 配置超过了实际 `/dev/shm`；
- 多个容器共享同一个 Pod 内存预算，`/dev/shm` 计入 cgroup，导致 OOM；
- 大对象/高并发让对象存储峰值超过预算。

对象超过上限后 spill/失败，重试产生更多对象，形成正反馈。

### 诊断

```bash
kubectl exec <pod> -- df -h /dev/shm
kubectl exec <pod> -- ray status
kubectl logs <pod> -c ray | grep -iE 'plasma|object store|spill|out of memory'
kubectl get pod <pod> -o jsonpath='{.spec.volumes}'
```

### 修复

- 确保 KubeRay 注入了 `medium: Memory` 的 emptyDir；
- 增大 `sizeLimit`（真实 KubeRay 会按 memory request/limit 推导，必要时显式覆盖）；
- 调整 `object-store-memory`，不要超过 `/dev/shm`；
- 减少大对象/批大小，或改用流式处理。

### 预防

- 在 staging 用真实数据规模压测；
- 监控 `ray_object_store_memory`（按 `Location`）与 spill 速率；没有稳定的 `ray_object_store_memory_limit` 指标；
- 给 Pod 设置合理的 memory request/limit；
- 在应用层避免 `ray.get` 超大对象。

关联：[第 2 章](02-ray-architecture.md)、[第 6 章](06-pods-networking-storage.md)、[第 35 章](35-ray-runtime-internals.md)。

## 37.3 案例 2：head OOM 杀死整个集群

### 现象

- head Pod `OOMKilled`；
- 所有 worker 失联；
- Dashboard 无法访问；
- 重启 head 后作业全部丢失。

### 时间线

```text
t0    driver 在 head 上 ray.get 一个 40GB 数据集
t+1m  head 内存接近 limit
t+2m  kernel OOM killer 杀死 driver/raylet/gcs
t+3m  worker 断连，任务失败
t+4m  控制器重建 head（无 GCS FT）
t+10m 新集群启动，但作业需要重跑
```

### 根因

Driver 运行在 head（或 entrypoint 资源指定的 worker），做了大量数据处理。RayJob 的 submitter 只负责提交，不是 driver：

- `ray.get` 把远程对象拉到 driver；
- driver 持有大量 Python 对象；
- head 的 memory limit 不足以容纳 driver + GCS + Dashboard；
- OOM killer 可能杀死 GCS，导致集群不可恢复。

### 诊断

```bash
kubectl describe pod <head> | grep -A10 'Last State'
kubectl logs <head> -c ray --previous | tail -100
kubectl top pod <head>
kubectl exec <head> -- ps aux --sort=-rss | head
```

### 修复

- 把重活移到 task/actor，不在 driver 里 `ray.get` 大对象；
- 给 head 更多内存；
- 给 driver 预留资源（`entrypointNumCpus`/`entrypointResources`）；
- 使用 Ray Data 的流式执行；
- 关键作业 checkpoint，head 故障后可恢复。

### 预防

- head 的 memory limit 要覆盖 GCS + Dashboard + driver；
- 监控 head RSS 和重启次数；
- 代码 review 禁止 driver 持有大对象；
- 对 RayJob 设置 `activeDeadlineSeconds`，避免无限占用。

关联：[第 10 章](10-rayjob.md)、[第 15 章](15-performance-cost.md)、[第 35 章](35-ray-runtime-internals.md)。

## 37.4 案例 3：autoscaler 403，副本永远不涨

### 现象

- Ray 里有大量 pending task；
- `enableInTreeAutoscaling=true`；
- `replicas` 一直是 0；
- autoscaler 日志出现 `403 Forbidden` 或 `cannot patch rayclusters`。

### 时间线

```text
t0    提交需要 64 CPU 的作业
t+30s Ray pending demand 上升
t+1m  autoscaler 尝试 patch RayCluster
t+1m  403，重试
t+5m  任务仍 Pending，SRE 收到告警
```

### 根因

autoscaler 的 ServiceAccount/Role/RoleBinding 缺失或权限不足：

- operator 没有创建 `<cluster>-autoscaler` RBAC；
- 用户自定义了 `serviceAccountName`，覆盖了 operator 注入的 SA；
- Role 只允许 get，不允许 patch；
- namespace 不匹配。

### 诊断

```bash
kubectl logs <head> -c autoscaler --tail=200
kubectl get sa,role,rolebinding -n <ns> | grep autoscaler
kubectl auth can-i patch rayclusters -n <ns> \
  --as=system:serviceaccount:<ns>:<cluster>-autoscaler
kubectl get raycluster <name> -o jsonpath='{.spec.headGroupSpec.template.spec.serviceAccountName}'
```

### 修复

- 删除自定义 `serviceAccountName`，让 operator 管理；
- 补 Role/RoleBinding；
- 确认 operator 有创建 RBAC 的权限；
- 重启 head，让 sidecar 重新加载。

### 预防

- 部署后跑一次 `kubectl auth can-i --list`；
- 监控 autoscaler 日志中的 403；
- 不要把 head 的 SA 改成无关账户；
- 用准入策略禁止覆盖 autoscaler SA。

关联：[第 9 章](09-autoscaling.md)、[第 14 章](14-security.md)、[第 34 章](34-autoscaler-v2-internals.md)。

## 37.5 案例 4：RayJob 卡在 Initializing

### 现象

- `jobDeploymentStatus=Initializing` 持续十几分钟；
- RayCluster 可能已经 Ready；
- submitter Job 不存在或 Pending；
- `preRunningDeadlineSeconds` 未设置，永远不失败。

### 根因分类

| 根因 | 信号 |
|---|---|
| RayCluster 不 Ready | `status.conditions` |
| head DNS/6379 不通 | init container 日志 |
| 镜像拉取失败 | Pod `ImagePullBackOff` |
| submitter Pending | 资源/配额/亲和性 |
| HTTPMode operator 访问不到 Dashboard | operator 日志 |
| SidecarMode 容器未注入 | head Pod containers |
| InteractiveMode 没有写 `jobId` | `Waiting`，不是 Initializing |

### 诊断

```bash
kubectl get rayjob <name> -o yaml
kubectl describe rayjob <name>
kubectl get pods,jobs -l ray.io/originated-from-cr-name=<name>
kubectl logs job/<name>-submitter
kubectl logs <head> -c wait-gcs-ready
kubectl -n kuberay-system logs deploy/kuberay-operator --tail=200
```

### 修复

- 先让 RayCluster Ready；
- 修镜像/Secret/网络；
- 给 submitter 足够资源；
- 设置 `preRunningDeadlineSeconds`，让卡住的作业显式失败；
- HTTPMode 检查 operator 到 head 的网络策略。

### 预防

- 所有 RayJob 设置 `preRunningDeadlineSeconds` 和 `activeDeadlineSeconds`；
- 监控 `jobDeploymentStatus` 停留时间；
- submitter 镜像与 Ray 镜像版本一致；
- 在 CI 里跑一次“最小 RayJob”。

关联：[第 10 章](10-rayjob.md)、[第 17 章](17-production-runbook.md)。

## 37.6 案例 5：RayService 升级后全部 503

### 现象

- 升级 `serveConfigV2` 后，Serve Service 返回 503；
- `kubectl get pods` 显示 Pod Running；
- `ray serve status` 显示应用 RUNNING；
- Service 的 EndpointSlice 为空。

### 根因

Serve Service 的 selector 是：

```text
ray.io/cluster=<active>
ray.io/serve=true
```

常见原因：

- `ray.io/serve` 标签没有变成 `true`；
- Service selector 还指向旧集群；
- 新集群的 proxy 不健康；
- `excludeHeadPodFromServeSvc=true`，但 worker 没有 proxy；
- NetworkPolicy 阻断 8000；
- 增量升级的 HTTPRoute 权重指向了空 Service。

### 诊断

```bash
kubectl get svc <service>-serve-svc -o yaml
kubectl get endpointslices -l kubernetes.io/service-name=<service>-serve-svc
kubectl get pods -l ray.io/cluster=<active> --show-labels | grep serve
kubectl exec <head> -- ray serve status
kubectl get rayservice <name> -o jsonpath='{.status}'
kubectl get httproute,gateway -A
```

### 修复

- 等 Serve 应用真正 RUNNING；
- 确认 `ray.io/serve=true`；
- 修正 selector（不要手改，让控制器管）；
- 检查 proxy 日志/端口；
- 增量升级时确认 `backendRefs` 指向存在的 Service。

### 预防

- 升级前预热镜像/模型；
- 监控 `numServeEndpoints` 和 5xx；
- 蓝绿/灰度时保留旧集群；
- 客户端配置重试；
- 用 `kubectl get endpointslices` 做上线检查。

关联：[第 11 章](11-rayservice.md)、[第 28 章](28-llm-serving.md)、[第 35 章](35-ray-runtime-internals.md)。

## 37.7 案例 6：RayService 升级卡住，pending 集群永远不 Ready

### 现象

- `UpgradeInProgress=True` 持续很久；
- `pendingServiceStatus` 存在；
- pending cluster 的 worker Pod Pending；
- 旧集群仍在接流量，资源被双倍占用。

### 根因

NewCluster 升级需要 **2 倍资源**：

- 旧集群占满 GPU；
- pending 集群没有 GPU 可调度；
- 资源配额/节点池上限不足；
- 没有配置 `maxSurgePercent` 或增量升级。

### 诊断

```bash
kubectl get rayservice <name> -o yaml | sed -n '/pendingServiceStatus/,+30p'
kubectl get pods -l ray.io/cluster=<pending-cluster> -o wide
kubectl describe pod <pending-pod>
kubectl get resourcequota -n <ns>
```

### 修复

- 临时扩容节点池/配额；
- 暂停旧集群的部分副本；
- 改用 `NewClusterWithIncrementalUpgrade`（需要 Gateway API）；
- 回滚 spec，删除 pending；
- 给升级预留 surge 容量。

### 预防

- 容量规划包含升级 surge；
- 监控 `UpgradeInProgress` 持续时间；
- 在 staging 演练升级；
- 对 GPU 服务设置升级窗口。

关联：[第 11 章](11-rayservice.md)、[第 32 章](32-platform-capstone.md)。

## 37.8 案例 7：CRD 没升级，新字段被静默裁剪

### 现象

- operator 升级到 v1.7.1；
- 新字段写进 YAML 后不生效；
- `kubectl get raycluster -o yaml` 里字段消失；
- operator 日志没有报错。

### 根因

Helm 不会自动升级 `crds/` 目录中的 CRD：

- `helm upgrade` 只升级 Deployment/Service 等模板资源；
- CRD 仍是旧 schema；
- API Server 按旧 schema **pruning** 未知字段；
- 控制器看不到字段，自然不会生效。

### 诊断

```bash
kubectl get crd rayclusters.ray.io -o jsonpath='{.metadata.annotations}'
kubectl get crd rayclusters.ray.io -o yaml | grep -A2 '<new-field>'
helm history kuberay-operator -n kuberay-system
```

### 修复

```bash
kubectl apply --server-side -f \
  https://raw.githubusercontent.com/ray-project/kuberay/v1.7.1/ray-operator/config/crd/bases/ray.io_rayclusters.yaml
# 其余三个 CRD 同理
```

然后重新 apply CR。

### 预防

- 把 CRD 升级纳入独立变更流程；
- 用 `kubectl diff`/CI 检查 schema；
- 升级后跑一个包含新字段的 smoke test；
- 备份 CRD/CR。

关联：[第 4 章](04-kuberay-overview.md)、[第 17 章](17-production-runbook.md)、[第 31 章](31-upgrade-migration-dr.md)。

## 37.9 案例 8：删除卡在 GCS FT finalizer

### 现象

- `kubectl delete raycluster` 后对象一直 `Terminating`；
- `metadata.finalizers` 包含 `ray.io/gcs-ft-redis-cleanup-finalizer`；
- cleanup Job 不存在/Pending/失败；
- Redis 里残留命名空间。

### 根因

GCS FT 删除流程：

```text
删除 Pod -> 创建 Redis cleanup Job -> 等 Job Complete -> 移除 finalizer
```

卡住原因：

- cleanup Job 的镜像拉不到；
- NetworkPolicy 阻断 Redis；
- Redis 密码 Secret 不存在；
- Job 被 ResourceQuota 拒绝；
- cleanup Job 的 submitter Pod 被误删；
- 超时注解未设置/设置太大。

### 诊断

```bash
kubectl get raycluster <name> -o jsonpath='{.metadata.finalizers}'
kubectl get jobs,pods -l ray.io/cluster=<name>
kubectl describe job <name>-redis-cleanup
kubectl logs job/<name>-redis-cleanup
kubectl -n kuberay-system logs deploy/kuberay-operator --tail=200
```

### 修复

- 修镜像/网络/Secret/配额；
- 手动创建/修复 cleanup Job；
- 确认 Redis 可达；
- 检查 Redis cleanup Job 的状态；必要时调整 `ray.io/gcs-ft-deletion-timeout`（默认 300s，超时强制移除 finalizer，可能留下垃圾数据）；
- 最后才考虑手动移除 finalizer，并记录风险。

### 预防

- 删除前检查 cleanup Job 的前置条件；
- 给 cleanup Job 独立资源配额；
- 监控 finalizer 停留时间；
- 定期清理 Redis 命名空间。

关联：[第 13 章](13-ha-fault-tolerance.md)、[第 33 章](33-mini-advanced-implementation.md)。

## 37.10 案例 9：多主机 worker 反复重建

### 现象

- 多主机 group（`numOfHosts > 1`）的 Pod 不断创建/删除；
- 一个 replica 的某个 host 失败后，整个 replica 被重建；
- 训练任务反复从头开始。

### 根因

多主机 replica 是**原子单位**：

- 任一 host 失败/缺失 → 整个 replica 删除重建；
- 节点池碎片/亲和性导致某个 host 一直调度不上；
- `workersToDelete` 命中一个 host → 整个 replica 被删；
- 节点不在同一拓扑域，无法满足 gang。

### 诊断

```bash
kubectl get pods -l ray.io/cluster=<name>,ray.io/group=<group> \
  -o custom-columns=NAME:.metadata.name,REPLICA:.metadata.labels.ray\.io/worker-group-replica-name,HOST:.metadata.labels.ray\.io/replica-host-index,NODE:.spec.nodeName,PHASE:.status.phase
kubectl describe pod <failing-host>
kubectl get podgroup <name>-podgroup -o yaml
```

### 修复

- 确保节点池能同时放下整个 replica；
- 用 gang scheduling/PodGroup；
- 给同一 replica 的 host 加 affinity/拓扑约束；
- 检查 `minMember` 是否覆盖所有 host；
- 避免手动删除单个 host。

### 预防

- 多主机池按整组预留；
- 监控 replica 重建次数；
- 训练 checkpoint；
- 在 staging 演练节点故障。

关联：[第 5 章](05-raycluster-crd.md)、[第 6 章](06-pods-networking-storage.md)、[第 33 章](33-mini-advanced-implementation.md)。

## 37.11 案例 10：Kueue 配额足够，Pod 却一直 Pending

### 现象

- Kueue Workload 已准入；
- ClusterQueue 显示有配额；
- RayCluster Pod 仍 Pending；
- 事件显示 `insufficient nvidia.com/gpu` 或 `untolerated taint`。

### 根因

Kueue 管的是**作业准入/配额**，不是 Pod 调度：

- 配额是逻辑资源，不代表节点有空闲；
- 节点池容量不足；
- GPU 型号/taint/toleration 不匹配；
- PodGroup `minMember` 大于可调度资源；
- Cluster Autoscaler 没配/达到上限。

### 诊断

```bash
kubectl get workloads -A
kubectl describe workload <name>
kubectl get clusterqueue,localqueue -A
kubectl describe pod <pending>
kubectl get nodes -o custom-columns=NAME:.metadata.name,GPU:.status.allocatable.nvidia\.com/gpu
```

### 修复

- 扩节点池；
- 修 toleration/nodeSelector；
- 调整 `minMember`；
- 检查 Cluster Autoscaler/Karpenter；
- 调整 Kueue 配额（如果配额本身是瓶颈）。

### 预防

- 队列配额与节点池容量对齐；
- 监控 `kueue_pending_workloads` 和 Pending Pod；
- 在准入后仍要检查调度失败；
- 区分“配额等待”和“调度等待”。

关联：[第 29 章](29-ecosystem-integrations.md)、[第 32 章](32-platform-capstone.md)。

## 37.12 案例 11：Ray 版本不一致导致序列化错误

### 现象

- 作业提交后 driver 报 `TypeError`/`AttributeError`/反序列化失败；
- head/worker 日志显示不同 Ray 版本；
- 部分节点正常，部分节点失败。

### 根因

- `spec.rayVersion` 与实际镜像 tag 不一致；
- head 和 worker 使用不同镜像；
- 客户端 `ray` 版本与集群不一致；
- runtime_env 安装了不同版本的 Ray；
- 滚动升级只更新了部分节点。

### 诊断

```bash
kubectl exec <head> -- ray --version
kubectl exec <worker> -- ray --version
kubectl get raycluster <name> -o jsonpath='{.spec.rayVersion}'
kubectl get pods -l ray.io/cluster=<name> -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.spec.containers[0].image}{"\n"}{end}'
python -c "import ray; print(ray.__version__)"
```

### 修复

- 统一 head/worker 镜像；
- 更新 `spec.rayVersion`；
- 客户端升级到匹配版本；
- 清理 runtime_env 中的 Ray 依赖；
- 用 `upgradeStrategy: Recreate` 或重建集群。

### 预防

- 镜像 tag 固定 digest；
- CI 检查 `rayVersion` 与镜像；
- 升级按集群分批；
- 不在 runtime_env 里安装 Ray。

关联：[第 2 章](02-ray-architecture.md)、[第 31 章](31-upgrade-migration-dr.md)。

## 37.13 案例 12：GPU 碎片导致大作业永远 Pending

### 现象

- 集群总空闲 GPU 足够；
- 8 GPU 作业一直 Pending；
- 小作业正常运行；
- 事件显示 `Insufficient nvidia.com/gpu`。

### 根因

- 每节点 8 GPU，已被多个 1 GPU 作业占满；
- 每个节点只剩 1–2 GPU，无法满足 8 连续 GPU；
- 调度器不做跨节点拼接；
- 没有队列/预留机制。

### 诊断

```bash
kubectl describe node <gpu-node> | sed -n '/Allocated resources/,/Events/p'
kubectl get pods -A -o wide | grep <gpu-node>
kubectl describe pod <pending-8gpu>
```

### 修复

- 排空/迁移小作业；
- 用队列限制小作业并发；
- 预留整机节点池给大作业；
- 大作业优先/抢占；
- 用 gang scheduling 防止部分调度。

### 预防

- 按作业形状划分节点池；
- 监控“最大连续 GPU”而不是总空闲；
- Kueue/Volcano 队列；
- 定期整理碎片。

关联：[第 9 章](09-autoscaling.md)、[第 29 章](29-ecosystem-integrations.md)、[第 36 章](36-gpu-heterogeneous-scheduling.md)。

## 37.14 案例 13：Prometheus 抓取把 operator 打挂

### 现象

- operator 周期性地变慢/重启；
- Prometheus 抓取超时；
- 指标基数爆炸（大量 task/actor/namespace 标签）；
- API Server 限流。

### 根因

- 抓取间隔太短；
- 指标 cardinality 太高；
- operator 和 Ray 共用抓取路径；
- 没有 relabel/drop；
- 大量短命 Pod/Job 产生新时间序列。

### 诊断

```bash
curl -s http://<operator>:8080/metrics | wc -l
curl -s http://<operator>:8080/metrics | grep '^kuberay_' | head
kubectl top pod -n kuberay-system
kubectl get servicemonitor,podmonitor -A -o yaml
```

### 修复

- 增大抓取间隔；
- 用 relabel 删除高基数标签；
- 分开抓取 operator/Ray；
- 限制保留时间；
- 给 operator 足够资源。

### 预防

- 上线前估算 series 数；
- 监控 Prometheus 自身；
- 对高基数指标做聚合；
- 不要把 task ID 放进标签。

关联：[第 8 章](08-observability.md)、[第 29 章](29-ecosystem-integrations.md)。

## 37.15 案例 14：NetworkPolicy 阻断 DNS，worker 无法加入

### 现象

- worker Pod Running，但 `ray start` 一直失败；
- 日志显示无法解析 head Service；
- head 正常；
- `kubectl exec` 手动 `nc` 也不通。

### 根因

启用 `DenyAll` 后：

- DNS egress 没有显式放行；
- head 6379 没有放行；
- 监控/对象存储端点没有放行；
- NetworkPolicy 的 namespaceSelector 写错。

### 诊断

```bash
kubectl exec <worker> -- getent hosts <head-svc>
kubectl exec <worker> -- nc -zv <head-svc> 6379
kubectl get networkpolicy -n <ns> -o yaml
kubectl describe networkpolicy <name>
```

### 修复

- 放行 kube-system DNS（UDP/TCP 53）；
- 放行 head 6379；
- 放行对象存储/镜像仓库；
- 用 `kubectl exec` 验证。

### 预防

- 默认拒绝 + 显式白名单；
- 在 staging 验证 DNS；
- 监控 worker 加入失败；
- 文档化每个 NetworkPolicy 的用途。

关联：[第 6 章](06-pods-networking-storage.md)、[第 14 章](14-security.md)。

## 37.16 故障排查总顺序

```text
1. 影响面：哪些 CR/Pod/用户/服务？
2. 最近变更：operator/CRD/Ray/镜像/配置/配额？
3. CR 状态：conditions/reason/message/observedGeneration
4. 子资源：Pod/Service/EndpointSlice/Job/PodGroup
5. 调度：Pending 原因/配额/taint/affinity/GPU
6. 运行时：镜像/命令/探针/日志/Ray 注册
7. 网络：DNS/端口/NetworkPolicy/token/mTLS
8. 外部：Redis/对象存储/Gateway/Kueue
9. 缓解：回滚/扩容/摘流量/删 pending
10. 复盘：根因/预防/行动项
```

### 37.16.1 先缓解还是先定位

生产故障优先**止血**：

- 回滚最近变更；
- 扩容/摘流量；
- 删除卡住的 pending/Job；
- 切到备用集群。

但要在止血的同时保留证据：

```bash
kubectl get all -n <ns> -o yaml > incident-$(date +%s).yaml
kubectl -n kuberay-system logs deploy/kuberay-operator --tail=1000 > operator.log
kubectl get events --sort-by=.lastTimestamp > events.log
```

否则复盘时只能靠猜。

## 37.17 演练题库

把下面场景做成混沌实验：

1. 删除 head（有/无 GCS FT）；
2. 删除 worker；
3. 填满 `/dev/shm`；
4. 制造 GPU 碎片；
5. 断开 Redis；
6. 阻断 DNS；
7. 让镜像拉取失败；
8. 把 autoscaler RBAC 删掉；
9. 把 CRD 回退一个版本；
10. 让 Kueue 配额为 0；
11. 让 Gateway 权重指向空 Service；
12. 让多主机的一个 host 不可调度；
13. 让 operator 失去 leader election；
14. 让 Prometheus 抓取超时。

每个实验都要有：

- 预期信号；
- 实际信号；
- 恢复步骤；
- 自动化验证（脚本/测试）；
- 复盘记录。

## 37.18 练习

1. 选一个案例，在 staging 复现并记录：预期信号、实际信号、恢复时间、缺失的告警。
2. 给“RayService 升级后 503”写一份完整复盘（时间线/根因/行动项）。
3. 为“autoscaler 403”设计一个告警：指标/日志/事件，触发条件和 runbook。
4. 把 37.17 的题库做成脚本/Chaos Mesh 实验，至少自动化 3 个。
5. 对比“先止血”和“先定位”的取舍：什么情况下必须先保留证据？

## 37.19 一页纸复盘模板

```markdown
# 事故复盘：<标题>

## 1. 摘要
- 影响：
- 持续：
- 级别：
- 当前状态：

## 2. 时间线（UTC）
| 时间 | 事件 | 证据 |
|---|---|---|
| | | |

## 3. 根因
- 直接原因：
- 系统性原因（为什么没被拦住）：

## 4. 诊断
- 有效信号：
- 误导信号：
- 缺失信号：

## 5. 修复
- 临时缓解：
- 永久修复：
- 回滚/验证：

## 6. 影响面
- 租户/作业/服务：
- 数据/成本：

## 7. 行动项
| 动作 | 负责人 | 截止 | 验收 |
|---|---|---|---|
| | | | |

## 8. 经验
- 做对了什么：
- 下次怎么更快：
```

### 37.19.1 行动项的质量标准

好的行动项：

- 可验证（“增加告警 X，规则 Y，演练 Z”）；
- 有负责人/截止；
- 能防止同类问题，而不是“加强意识”；
- 有测试/演练覆盖。

差的行动项：

- “注意配置”；
- “加强监控”（没有指标/阈值）；
- “培训团队”（没有验收）；
- “以后小心”。

### 37.19.2 复盘后的闭环

```text
复盘 -> 行动项 -> 代码/配置 -> 测试/告警 -> 演练 -> 下一次复盘
```

没有闭环的复盘只是文档。

## 37.20 小结

- 大多数故障不是“KubeRay 的 bug”，而是资源、网络、镜像、RBAC、配额、版本的组合。
- 先按回路定位：Ray demand → autoscaler → CR → operator → Pod → 调度 → 运行时。
- 保留证据再止血；复盘要覆盖系统性原因。
- 每个案例都应该变成监控/测试/演练，而不是只修一次。
- 第 24 章的测试、第 26 章的真实集群实验、本章的案例可以互相验证。

下一章把 MiniKubeRay 的代码迁移成真实 Operator，讲清楚每一步的工程取舍。

→ [第 38 章 从 MiniKubeRay 到生产 Operator](38-porting-to-production-operator.md)
