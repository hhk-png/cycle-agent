# 第 33 章 MiniKubeRay 进阶实现：History Server、Gateway API 与批调度

第 19–23 章实现了 MinKubeRay 的核心控制平面。本章拆解后来补齐的四块“生产感”能力：History Server、Gateway API 增量升级、批调度/Kueue 集成、多主机与 GCS FT 清理。它们对应真实 KubeRay v1.6/1.7 的功能，也解释了第 26–31 章的运维现象。

## 33.1 History Server

### 33.1.1 问题

Ray Dashboard 的数据来自运行中的集群。RayJob 的集群被 `shutdownAfterJobFinishes` 删除后，调试信息就没了。真实 History Server 用 collector sidecar 采集事件、写对象存储，再提供回放。

MiniKubeRay 的模型：

```text
EventRecorder ──> HistoryStore ──> sessions/events/snapshot
                      │
                      ├── Operator 每轮 snapshot RayCluster + Runtime
                      ├── API Server /api/history/...
                      └── CLI `history list|show|events|replay`
```

### 33.1.2 数据结构

文件：`minikuberay/history/server.py`。

```python
@dataclass(frozen=True)
class HistoryEvent:
    session: str          # namespace/cluster
    namespace: str
    cluster: str
    event_type: str       # Normal/Warning
    reason: str
    message: str
    timestamp: str
    object_kind: str
    object_name: str
```

`HistoryStore` 内部：

```python
_sessions: dict[str, _Session]
_Session:
    namespace, cluster, created_at, deleted_at
    snapshot: dict          # 最后一次 RayCluster spec/status + runtime
    events: list[HistoryEvent]
    last_snapshot_at
```

### 33.1.3 事件归属

`_session_for_object` 决定一个事件属于哪个 session：

| 对象 | session |
|---|---|
| RayCluster | `namespace/cluster-name` |
| RayJob | `status.rayClusterName`，没有则用对象名 |
| RayService | `activeServiceStatus.rayClusterName`，否则 `pendingServiceStatus.rayClusterName`，否则对象名 |
| RayCronJob | 对象名（子 RayJob 的 session 由 RayJob 自己归属） |
| Pod/Service/Job/Secret/PVC | `ray.io/cluster` 标签 |
| Event | `involvedObject` 的 RayCluster 名 |

这样即使事件来自 worker Pod 或 submitter Job，也能归到正确的集群。

### 33.1.4 快照

`Operator.run_once` 在调和前后各做一次：

```python
clusters_before = self._cluster_map()
self._snapshot_clusters(clusters_before)
# ... kubelet + controllers + gc ...
clusters_after = self._cluster_map()
self._snapshot_clusters(clusters_after)
for key in clusters_before:
    if key not in clusters_after:
        self.history_store.mark_deleted(*key)
```

快照包括：

- CR 的 metadata/spec/status；
- 模拟 Runtime 的 nodes/jobs/serve/demand。

删除后 session 保留，`deletedAt` 被设置。这正是 History Server 的价值。

### 33.1.5 持久化

`HistoryStore(path=...)` 支持 JSON：

- `save()` 原子写（临时文件 + replace）；
- `load()` 启动时恢复；
- `Operator.stop()` 会保存；
- CLI `--history-path`、`--no-history`。

生产 History Server 会写对象存储、做索引/保留策略；教学实现保留了“会话/事件/快照/回放”的语义。

### 33.1.6 API 与 CLI

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/api/history/sessions` | 列表；`?q=` 搜索 |
| GET | `/api/history/sessions/{ns}/{cluster}` | 会话 + 事件 + 快照 |
| GET | `.../events?limit=&kind=` | 事件 |
| GET | `.../snapshot` | 最后一次快照 |
| GET | `.../replay` | 快照 + 时间线 |

```bash
python -m minikuberay history list
python -m minikuberay history show default/demo
python -m minikuberay history events default/demo --limit 20
python -m minikuberay history replay default/demo
```

Dashboard 也增加了 History Server 表格。

### 33.1.7 测试

`tests/test_history.py`：

- 事件/快照记录；
- 删除后 session 保留；
- JSON 持久化/重载；
- API 列表/详情/事件/回放/404。

## 33.2 Gateway API 增量升级

### 33.2.1 问题

`NewCluster` 升级需要 2 倍资源，切换是瞬时的。增量升级用 Gateway API 把流量按权重从 active 迁到 pending。

真实 KubeRay：

- 创建 `{rayservice}-gateway`（如果配置 `gatewayClassName`）；
- 创建 `{rayservice}-httproute`；
- 每个 RayCluster 有自己的 Serve Service；
- HTTPRoute `backendRefs` 权重从 100/0 逐步变成 0/100；
- pending 的 `target_capacity` 从 0 升到 `maxSurgePercent`；
- 上游 v1.7.1 只通过 `gatewayClassName` 创建/管理路由；MiniKubeRay 额外支持 adopt 已有 HTTPRoute（`httpRouteName`）。

### 33.2.1.1 先装一个 Gateway 实现

KubeRay 只创建 Gateway/HTTPRoute 对象，不提供数据面。没有 GatewayClass 时，`gatewayClassName` 会一直找不到。实验环境任选一个：

```bash
# 方案 A：Envoy Gateway（示例，版本以官方为准）
kubectl apply -f https://github.com/envoyproxy/gateway/releases/latest/download/install.yaml
kubectl get gatewayclass

# 方案 B：NGINX Gateway Fabric（Helm）
helm install ngf oci://ghcr.io/nginx/charts/nginx-gateway-fabric \
  --namespace nginx-gateway --create-namespace
kubectl get gatewayclass
```

确认 `GatewayClass` 是 `Accepted=True`，并把它的名字填到 `clusterUpgradeOptions.gatewayClassName`。然后验证：

```bash
kubectl get gateway,httproute -A
kubectl get httproute <name> -o jsonpath='{.status.parents[*].conditions}'
```

流式/长连接场景要显式配置 Gateway/HTTPRoute 的 idle timeout；否则默认超时会在流式响应中途断开。

### 33.2.2 数据模型

`builders.build_gateway`：

```python
Gateway:
  gatewayClassName: <class>
  listeners: [{name: serve, port: 8000, protocol: HTTP}]
```

`builders.build_httproute`：

```python
HTTPRoute:
  parentRefs: [{name: gateway}]
  rules:
    - matches: [{path: {type: PathPrefix, value: /}}]
      backendRefs:
        - {name: <active>-serve-svc, port: 8000, weight: 100-percent}
        - {name: <pending>-serve-svc, port: 8000, weight: percent}
```

### 33.2.3 控制器流程

`RayServiceController`：

1. 计算 revision hash（只包含 `rayClusterConfig`，并排除 worker `replicas`/`minReplicas`/`maxReplicas`/`workersToDelete`；`serveConfigV2` 和 `excludeHeadPodFromServeSvc` 原地应用到 active，不参与 hash）；
2. 创建 active/pending RayCluster；
3. 为每个集群创建 `<cluster>-serve-svc`（`stable=False` 时不改稳定 Service）；
4. 部署 Serve；pending `target_capacity=0`；
5. `_advance_traffic` 按 `intervalSeconds`/`stepSizePercent` 增加百分比，并更新 `targetCapacity`；
6. `_ensure_upgrade_routing` 创建/更新 Gateway/HTTPRoute；
7. 100% 后 `_promote`：active=pending，稳定 Service 切到新集群，路由只指向新 active；
8. 旧集群延迟删除。

### 33.2.4 回滚与关闭

- spec 回退到 active revision -> 删除 pending，路由回到 active；
- `upgradeStrategy.type != NewClusterWithIncrementalUpgrade` -> `_delete_routing` 清理遗留 Gateway/HTTPRoute；
- 删除 RayService -> 删除路由/网关。

### 33.2.5 adopt 已有 HTTPRoute（MiniKubeRay 扩展）

```yaml
spec:
  upgradeStrategy:
    type: NewClusterWithIncrementalUpgrade
    clusterUpgradeOptions:
      httpRouteName: platform-ray-route
```

`_ensure_upgrade_routing` 只改 `spec.rules[0].backendRefs`，保留 `parentRefs`/labels/其他规则。这样 GitOps 管理的路由不会被 operator 整体覆盖。注意：`httpRouteName`/`httpRouteInProgressLabels` 是 MiniKubeRay 的教学字段，**不在上游 v1.7.1 RayService API 中**；上游只有 `gatewayClassName`，由 operator 创建/管理 Gateway 与 HTTPRoute。

### 33.2.6 测试

`tests/test_rayservice.py`：

- Gateway/HTTPRoute 创建；
- 初始只有 active backend（weight 100）；pending 就绪后再加入第二个 backend；
- 升级后两个 backend、权重和为 100；
- adopt 路由只改 backendRefs；
- 回滚/删除清理。

## 33.3 批调度与 Kueue

### 33.3.1 配置

`OperatorConfig.batch_scheduler`：

```text
""                 不启用
"volcano"          PodGroup + schedulerName=volcano
"yunikorn"         schedulerName=yunikorn（无 PodGroup）
"scheduler-plugins" PodGroup(scheduling.x-k8s.io) + schedulerName
"was"              Kubernetes WAS（MiniKubeRay 教学别名；需要 feature gate）
```

> 注意：上游 v1.7.1 的 WAS 由 `KubernetesWAS` gate 控制，且与 `--batch-scheduler` **互斥**。MiniKubeRay 为了把五种模式放在同一张表里，把 `was` 简化成“设置 schedulerName + 要求 gate”的别名，不创建 in-tree `scheduling.k8s.io` Workload/PodGroup。不要把 Mini 的行为当成上游语义。

CLI：

```bash
python -m minikuberay server --batch-scheduler volcano
python -m minikuberay operator --batch-scheduler scheduler-plugins
```

### 33.3.2 PodGroup 构造

`build_podgroup`：

- `minMember` = head + 所有 worker 的 desired Pod 数；
- `queue` 从 `ray.io/queue`/`volcano.sh/queue`/`yunikorn.apache.org/queue` 读取（这些是 MiniKubeRay 的兼容别名；上游 Volcano 只认 `volcano.sh/queue-name`）；
- Volcano apiVersion `scheduling.volcano.sh/v1beta1`；
- scheduler-plugins apiVersion `scheduling.x-k8s.io/v1alpha1`；
- ownerReference 指向 RayCluster。

### 33.3.3 schedulerName

`build_head_pod`/`build_worker_pod` 接收 `scheduler_name`：

```python
if scheduler_name:
    pod_spec.setdefault("schedulerName", scheduler_name)
```

注意：切换 operator 的 `--batch-scheduler` 不会重建已有 Pod（schedulerName 是 Pod spec 的一部分，但模板 hash 不变）。需要滚动/删除 Pod 才生效。这与真实 operator 的行为一致，测试里也明确断言。

### 33.3.4 清理

`_prune_auxiliary` 在关闭调度器时删除 PodGroup；`_cleanup` 删除所有子资源；GC 也会兜底。

### 33.3.5 Kueue

MiniKubeRay 只实现：

- `managedBy: kueue.x-k8s.io/multikueue` 时跳过调和；
- 文档/样例展示 Kueue 的 Workload/ClusterQueue 关系；
- 不实现 Workload 准入/配额/抢占。

这是有意的边界：Kueue 是独立 controller，完整模拟会引入另一套对象模型；教学重点是“KubeRay 如何把控制权交出去”。

## 33.4 多主机 worker

### 33.4.1 语义

`numOfHosts > 1` 时：

- 一个 replica = `numOfHosts` 个 Pod；
- `replicas` 控制 replica 数，不是 Pod 数；
- 总 Pod 数 = `replicas * numOfHosts`；
- 同一 replica 的 Pod 共享 `ray.io/worker-group-replica-name`；
- `ray.io/worker-group-replica-index` 是 replica 序号；
- `ray.io/replica-host-index` 是 replica 内的 host 序号。

### 33.4.2 调和

`_ensure_multi_host_pods`：

1. 按 `worker-group-replica-name` 分组；
2. 任何 host 失败/缺失/多余 -> 删除整个 replica；
3. 缩容删除整个 replica（从高索引开始）；
4. 扩容按 replica 创建所有 host，使用确定性名字 `<cluster>-<group>-<replica>-<host>`；
5. `workersToDelete` 命中任一 host -> 删除整个 replica。

这修正了早期实现把每个 Pod 当成独立 replica 的错误。

### 33.4.3 测试

`tests/test_raycluster.py`：

- 2 replicas × 2 hosts = 4 Pod；
- replica/host 标签正确；
- 一个 host 失败 -> 整个 replica 重建；
- 3->1 replica -> 只剩 2 Pod。

## 33.5 Ingress 与自定义 head Service

### 33.5.1 Ingress

`build_ingress` 在 `headGroupSpec.enableIngress=true` 时创建：

```python
Ingress:
  rules[0].host = ingressOptions.host
  path = ingressOptions.path
  backend = <head-svc>:8265
  ingressClassName / tls / annotations
```

关闭 `enableIngress` 后 `_prune_auxiliary` 删除。

### 33.5.2 自定义 head Service 端口

`build_head_service` 按端口名合并默认端口和用户端口：

```python
merged = {port.name: port for port in default_ports}
for port in custom_ports:
    merged[port.name] = port
```

早期实现直接 `custom + default`，会产生重复端口名，Kubernetes 会拒绝。测试 `test_custom_head_service_ports_are_merged_by_name` 覆盖。

## 33.6 GCS FT Redis 清理

### 33.6.1 finalizer

Redis 后端时，RayCluster 有两个 finalizer：

- `ray.io/raycluster-finalizer`：通用子资源清理；
- `ray.io/gcs-ft-redis-cleanup-finalizer`：清理 Redis 命名空间。

### 33.6.2 删除流程

`_cleanup_gcs_ft`：

1. 只删除 Ray 节点 Pod（`ray.io/is-ray-node=yes`），不碰 cleanup Job 的 submitter；
2. 创建 `<cluster>-redis-cleanup` Job；
3. 等待 Job `Complete`；
4. 移除 GCS FT finalizer；
5. 下一轮通用 `_cleanup` 删除剩余资源/CR。

### 33.6.3 超时

`ray.io/gcs-ft-deletion-timeout`（整数秒，默认 300s；与上游 v1.7.1 对齐）：

- 超时后强制移除 finalizer；
- 发 Warning 事件；
- 可能留下 Redis 垃圾数据，需要人工清理。

### 33.6.4 测试

- finalizer 阻止删除直到 Job 完成；
- 超时释放 finalizer；
- cleanup Job 最终被删除。

## 33.7 ReplicaFailure condition

`_ensure_pods` 捕获 head/worker 创建异常：

- 记录 `(namespace, name) -> message`；
- `_write_status` 设置 `ReplicaFailure=True`（Go 常量 `RayClusterReplicaFailure`，序列化 type 为 `ReplicaFailure`）；
- 恢复后设置 `False/ReplicaFailureResolved`。

这让“Pod 创建失败”在 `kubectl describe`/API 里可见，而不是只藏在 operator 日志。

## 33.8 新增的 API/CLI

### 33.8.1 API

| 路径 | 作用 |
|---|---|
| `GET .../rayclusters/{name}/nodes` | 模拟 Ray 节点 |
| `GET .../rayclusters/{name}/workergroups` | worker group 汇总 |
| `GET /api/history/...` | History Server |
| `GET/POST .../httproutes` | Gateway API 对象 |
| `GET/POST .../gateways` | Gateway |
| `GET/POST .../podgroups` | PodGroup |
| `GET/POST .../ingresses` | Ingress |

### 33.8.2 CLI

```bash
python -m minikuberay get nodes -c demo
python -m minikuberay get workergroups -c demo
python -m minikuberay create cluster my-cluster --worker-replicas 2
python -m minikuberay job submit my-job --cluster my-cluster --entrypoint "python train.py"
python -m minikuberay history list
python -m minikuberay history replay default/my-cluster
```

## 33.9 测试矩阵

| 能力 | 测试文件 |
|---|---|
| History Server | `tests/test_history.py` |
| Gateway/增量升级 | `tests/test_rayservice.py` |
| 批调度 | `tests/test_batch_scheduler.py` |
| 多主机 | `tests/test_raycluster.py` |
| Ingress/自定义 Service | `tests/test_raycluster.py` |
| GCS FT 清理 | `tests/test_raycluster.py` |
| ReplicaFailure | `tests/test_raycluster.py` |
| History API/CLI 命令 | `tests/test_apiserver_cli.py`、`tests/test_history.py` |
| 状态机/升级回归 | `tests/test_audit_regressions.py` |

验证命令：

```bash
python -m pytest -q
python check_docs.py
python -m minikuberay demo --fast
```

## 33.10 与真实 KubeRay 的差距

| 能力 | MiniKubeRay | 真实 KubeRay |
|---|---|---|
| History Server | 内存/JSON、事件回放 | collector sidecar + 对象存储 + UI |
| Gateway | 模拟对象/权重 | 真实 Gateway/HTTPRoute 数据面 |
| 批调度 | PodGroup/schedulerName | 真实 scheduler 执行 gang |
| Kueue | managedBy 跳过 | Workload/配额/抢占 |
| 多主机 | 确定性命名 | 随机 replica 名 + 完整 DNS |
| GCS FT | 模拟 cleanup Job | 真实 Redis cleanup |
| RayJob 提交 | submitter 对象/env/失败可见，Runtime 代提交 | submitter 真正执行 `ray job submit` |
| RayService 升级 | serveConfig 原地；集群变更走 pending | 更细的字段级原地更新/回滚 |
| Serve 运行时 | 时间驱动的状态机；`targetCapacity` 只记录，不改变副本/就绪 | 按 capacity/replicas 真正预热，流量按权重生效 |
| mTLS/NetworkPolicy | 对象/字段 | cert-manager/CNI 执行 |

这些差距是教学边界，不是“遗漏”；第 25 章给出了扩展路线。

## 33.11 小结

- History Server 的关键是事件归属、快照、删除后保留。
- Gateway 增量升级的核心是每集群 Serve Service + HTTPRoute 权重 + targetCapacity。
- 批调度通过 PodGroup/schedulerName 接入；Kueue 通过 managedBy 接管。
- 多主机以 replica 为原子单位，不能按 Pod 扩缩。
- GCS FT 清理是 finalizer + cleanup Job + 超时的组合。
- 新增能力都有对应测试和 API/CLI，保持“可运行、可验证”。

← [第 32 章 平台实战](kuberay教程-32-平台实战-多租户Ray平台设计.md)

→ [第 34 章 Autoscaler v2 内部机制与容量模型](kuberay教程-34-Autoscaler-v2内部机制与容量模型.md)
