# 附录 H. 事件、错误与状态码目录

本附录把 KubeRay 常见的 condition、event、status、HTTP 错误和日志关键词集中起来，用于 on-call 时快速对照。事件/原因在不同版本可能变化；以你集群的实际输出为准。

## H.1 先看哪里

```text
CR 的 .status.conditions/.status.reason
  -> 事件（kubectl describe / kubectl get events）
  -> 子资源（Pod/Job/Service/EndpointSlice）
  -> operator 日志
  -> Ray 容器日志
```

不要一上来就 `kubectl logs`；先看 CR 的状态，能排除一半问题。

## H.2 Condition 目录

### H.2.1 RayCluster

| Condition | True 含义 | False 常见原因 | 处理 |
|---|---|---|---|
| `HeadPodReady` | head Pod Ready | 镜像/调度/启动/探针 | 看 head Pod |
| `RayClusterProvisioned` | 首次全部 Ready（粘性） | 仍在创建/资源不足 | 看 worker/配额 |
| `ReplicaFailure` | 创建/删除 Pod 失败 | RBAC/配额/镜像/API | 看事件/operator 日志 |
| `RayClusterSuspending` | 正在暂停 | Pod 未删完 | 等/查终止 |
| `RayClusterSuspended` | 已暂停 | 仍有 Pod | 查 finalizer/删除 |

### H.2.2 RayJob（MiniKubeRay 教学扩展）

上游 `RayJobStatus` 没有 `JobSubmission`/`JobDeployment`/`JobFinished` 这些 condition 类型；上游主要看 `status.jobDeploymentStatus` 和 `status.jobStatus`。下表只适用于 MiniKubeRay：

| Condition | 含义 | 处理 |
|---|---|---|
| `JobSubmission` | 作业已提交/未提交 | 看 reason |
| `JobDeployment` | 部署状态 | 看 `jobDeploymentStatus` |
| `JobFinished` | 作业结束 | 看 `jobStatus` |

### H.2.3 RayService

| Condition | 含义 | 处理 |
|---|---|---|
| `Ready` | 可接流量（首选） | 看 endpoints/Serve |
| `UpgradeInProgress` | 升级中 | 看 pending |
| `RollbackInProgress` | 回滚中 | 看路由/active |
| `Suspending`/`Suspended` | 正在暂停/已暂停（同时看底层 RayCluster） | 看底层集群 |

### H.2.4 RayCronJob

RayCronJob 通常没有复杂 conditions；看：

- `status.lastScheduleTime`
- `status.lastSuccessfulTime`（若版本提供）
- 子 RayJob 状态
- 事件

## H.3 Event 目录

### H.3.1 上游常见事件

| Reason | 类型 | 含义 | 处理 |
|---|---|---|---|
| `CreatedHeadPod` | Normal | 创建 head | — |
| `CreatedWorkerPod` | Normal | 创建 worker | — |
| `DeletedHeadPod` | Normal | 删除 head | — |
| `DeletedWorkerPod` | Normal | 删除 worker | — |
| `FailedToCreateHeadPod` | Warning | head 创建失败 | RBAC/配额/API |
| `FailedToCreateWorkerPod` | Warning | worker 创建失败 | 同上 |
| `InvalidRayClusterSpec` | Warning | spec 非法 | 看 message |
| `InvalidRayJobSpec` | Warning | RayJob 非法 | 看 message |
| `InvalidRayServiceSpec` | Warning | RayService 非法 | 看 message |
| `CreatedRayJobSubmitter` | Normal | 创建 submitter | — |
| `UpdatedServeApplications` | Normal | Serve 更新 | — |
| `UpdatedHTTPRoute` | Normal | 路由更新 | — |
| `FailedToUpdateHTTPRoute` | Warning | 路由失败 | RBAC/Gateway |
| `GCSFTRedisCleanup...` | Normal/Warning | Redis 清理 | 看 finalizer |

### H.3.2 MiniKubeRay 事件目录

`minikuberay/` 会发出这些事件（测试/演示可直接断言）：

| Reason | 类型 | 触发 |
|---|---|---|
| `CreatedHeadPod` | Normal | head Pod 创建 |
| `CreatedWorkerPod` | Normal | worker Pod 创建 |
| `DeletedWorkerPod` | Normal | 多余 worker 删除 |
| `FailedToCreateHeadPod` | Warning | head 创建异常 |
| `FailedToCreateWorkerPod` | Warning | worker 创建异常 |
| `InvalidRayClusterSpec` | Warning | RayCluster 校验失败 |
| `RecreateUpgrade` | Normal | Recreate 删除 Pod |
| `AutoscalerScaleUp` | Normal | 扩容 |
| `AutoscalerScaleDown` | Normal | 缩容 |
| `AutoscalerResizeUp` | Normal | IPPR 对运行中的 Pod 发起原地扩容 |
| `AutoscalerResizeRolledBack` | Warning | IPPR resize 失败/超时，恢复 previous resources |
| `DeletedRayCluster` | Normal | 删除完成 |
| `CreatedGCSFTRedisCleanupJob` | Normal | 创建 cleanup Job |
| `GCSFTRedisCleanupComplete` | Normal | 移除 FT finalizer |
| `GCSFTRedisCleanupTimeout` | Warning | 超时释放 finalizer |
| `FailedToCreateGCSFTRedisCleanupJob` | Warning | cleanup Job 创建失败 |
| `InvalidRayJobSpec` | Warning | RayJob 校验失败 |
| `CreatedRayJobSubmitter` | Normal | 创建 submitter |
| `FailedToCreateRayJobSubmitter` | Warning | submitter 创建失败 |
| `FailedToCreateRayJob` | Warning | 子 RayJob 创建失败 |
| `InvalidRayServiceSpec` | Warning | RayService 校验失败 |
| `FailedToCreateRayCluster` | Warning | RayService 创建集群失败 |
| `InvalidRayCronJobSpec` | Warning | RayCronJob 校验失败 |
| `CreatedRayJob` | Normal | RayCronJob 创建子作业 |
| `SuspendedRayCronJob` | Normal | RayCronJob 暂停 |
| `ReconcileError` | Warning | 控制器异常 |

## H.4 状态枚举

### H.4.1 RayCluster.state

| 值 | 含义 |
|---|---|
| 空 | 未就绪/创建中 |
| `ready` | head + 所有 desired worker Ready |
| `suspended` | 已暂停 |
| `failed` | 已弃用（仍可能在旧对象中） |

### H.4.2 RayJob.jobStatus（Ray 作业状态）

| 值 | 含义 |
|---|---|
| 空 | 未提交 |
| `PENDING` | 已提交，等待资源 |
| `RUNNING` | 运行中 |
| `SUCCEEDED` | 成功 |
| `FAILED` | 失败 |
| `STOPPED` | 被停止 |

### H.4.3 RayJob.jobDeploymentStatus（KubeRay 部署状态）

| 值 | 含义 | 下一步 |
|---|---|---|
| 空 | New | 等待初始化 |
| `Initializing` | 创建集群/等待就绪 | 看 RayCluster |
| `Waiting` | InteractiveMode 等 jobId | 用户提交 |
| `Running` | 作业已提交 | 看 Ray job |
| `Complete` | 完成 | 清理 |
| `Failed` | 失败 | 看 reason/message |
| `ValidationFailed` | spec 非法 | 修 spec |
| `Suspending` | 暂停中 | 等 Pod 删除 |
| `Suspended` | 已暂停 | 恢复 |
| `Retrying` | 准备重试 | 看 backoff |

### H.4.4 RayService

| 字段/值 | 含义 |
|---|---|
| `serviceStatus=Running` | 已弃用；看 `Ready` condition |
| `Ready=True` | 可接流量 |
| `UpgradeInProgress=True` | 升级中 |
| `RollbackInProgress=True` | 回滚中 |
| `numServeEndpoints` | 接流量 endpoint 数 |
| `trafficRoutedPercent` | 增量升级流量百分比 |

## H.5 常见错误字符串

### H.5.1 调度

| 字符串 | 含义 | 处理 |
|---|---|---|
| `0/N nodes are available: N Insufficient cpu` | CPU 不足 | 扩容/降 request |
| `Insufficient memory` | 内存不足 | 扩容/降 request |
| `Insufficient nvidia.com/gpu` | GPU 不足/碎片 | 节点池/队列 |
| `node(s) had untolerated taint` | 缺少 toleration | 加 toleration |
| `node affinity/selector mismatch` | 标签/亲和性 | 修 selector |
| `volume node affinity conflict` | 存储拓扑 | 改 StorageClass/节点 |
| `exceeded quota` | ResourceQuota | 调配额 |
| `pod has unbound immediate PersistentVolumeClaims` | PVC 未绑定 | 修 StorageClass |
| `failed to fit in any node` | 资源形状不匹配 | 看 PodGroup/资源 |

### H.5.2 镜像/启动

| 字符串 | 含义 | 处理 |
|---|---|---|
| `ImagePullBackOff` | 拉取失败 | 镜像/Secret/网络 |
| `ErrImagePull` | 拉取错误 | 同上 |
| `CrashLoopBackOff` | 反复崩溃 | 看 logs/previous |
| `OOMKilled` | 内存超限 | 提 limit/查泄漏 |
| `CreateContainerConfigError` | Secret/ConfigMap 缺失 | 补资源 |
| `RunContainerError` | 启动失败 | 命令/权限 |
| `permission denied` | 权限/只读文件系统 | securityContext |

### H.5.3 Ray/网络

| 字符串 | 含义 | 处理 |
|---|---|---|
| `GCS is not available` | head/GCS 未就绪 | 查 head |
| `Raylet is not available` | 节点未注册 | 查 raylet/资源 |
| `PlasmaStoreFull` | 对象存储满 | 增 `/dev/shm`/减对象 |
| `ObjectStoreFullError` | 同上 | 同上 |
| `worker died unexpectedly` | worker 崩溃 | 查 OOM/日志 |
| `The actor is dead` | actor 死亡 | 查 `max_restarts` |
| `Task failed with retry` | 重试 | 查幂等/资源 |
| `Ray version mismatch` | 版本不一致 | 统一镜像 |
| `Authentication failed` | token/mTLS | 查 Secret/证书 |

### H.5.4 删除/finalizer

| 字符串 | 含义 | 处理 |
|---|---|---|
| `object is being deleted` | 删除中 | 等/查 finalizer |
| `finalizer ... not removed` | 清理未完成 | 查 cleanup Job |
| `cleanup Job failed` | Redis 清理失败 | 修 Job/网络 |
| `deletionTimestamp` | 删除标记 | 不要继续创建子资源 |

### H.5.5 网络/DNS

| 字符串 | 含义 | 处理 |
|---|---|---|
| `Could not resolve host` | DNS 失败 | CoreDNS/NetworkPolicy/search 域 |
| `connection refused` | 端口未监听/无 endpoint | head/Service/EndpointSlice |
| `i/o timeout` | 丢包/MTU/策略 | 抓包、MTU、NetworkPolicy |
| `no route to host` | 路由/underlay | CNI/节点路由 |
| `TLS handshake timeout` | MTU/证书/代理 | MTU、证书、timeout |
| `context deadline exceeded` | 客户端超时 | 后端慢/网络 |
| `too many open files` | fd 耗尽 | ulimit/连接池 |

### H.5.6 规模化

| 字符串/指标 | 含义 | 处理 |
|---|---|---|
| `429 Too Many Requests` | API 限流 | QPS/APF/分片 |
| `etcdserver: request timed out` | etcd 压力 | 降低写入/扩容 etcd |
| `watch ... 410 Gone` | resourceVersion 过期 | 重新 LIST（客户端自动） |
| `workqueue depth` 高 | 调和积压 | 并发/热点/分片 |
| `cache` 内存高 | 对象太多 | 分片/watch 范围 |
| `reconcile` 延迟高 | 单轮成本/热点 | 优化 list/status 写入 |

## H.6 HTTP 状态码（MiniKubeRay API）

`minikuberay/errors.py` 的映射：

| 状态码 | Reason | 场景 |
|---|---|---|
| 400 | `BadRequest` | 路径/body 非法、JSON patch 未实现 |
| 401 | `Unauthorized` | 缺少/错误 Bearer token |
| 403 | `Forbidden` | `--read-only` 或权限不足 |
| 404 | `NotFound` | 对象不存在 |
| 409 | `AlreadyExists`/`Conflict` | 创建冲突/resourceVersion 冲突 |
| 422 | `Invalid` | 校验失败 |
| 500 | `InternalError` | 未捕获异常 |
| 504 | `Timeout` | 外部超时 |

真实 Kubernetes 还会返回：

| 状态码 | 场景 |
|---|---|
| 410 Gone | resourceVersion 过期 |
| 429 Too Many Requests | API 限流 |
| 503 Service Unavailable | API Server 不可用 |

## H.7 Operator 日志关键词

```bash
kubectl -n kuberay-system logs deploy/kuberay-operator --tail=500 \
  | grep -iE 'error|failed|invalid|conflict|forbidden|timeout|reconcile'
```

| 关键词 | 含义 |
|---|---|
| `Reconcile error` | 调和异常 |
| `Invalid...Spec` | 校验失败 |
| `failed to create` | 子资源创建失败 |
| `failed to update status` | status 冲突/权限 |
| `forbidden` | RBAC |
| `conflict` | resourceVersion |
| `timeout` | 外部依赖 |
| `not found` | 对象删除/缓存延迟 |

## H.8 Ray 日志关键词

```bash
kubectl exec <head> -- ls /tmp/ray/session_latest/logs
kubectl exec <head> -- tail -n 200 /tmp/ray/session_latest/logs/raylet.out
kubectl exec <head> -- tail -n 200 /tmp/ray/session_latest/logs/gcs_server.out
```

| 文件/关键词 | 含义 |
|---|---|
| `raylet.out` | 本地调度/心跳/资源 |
| `gcs_server.out` | 注册/元数据/恢复 |
| `dashboard.log` | Job/Serve API |
| `serve/` | controller/proxy |
| `python-core-driver-*.log` | driver 输出 |
| `raylet_monitor.log` | 节点监控 |
| `autoscaler.out` | 扩缩容决策 |

## H.9 一页对照表

> 这是附录 C.0 的压缩版：C.0 负责“按症状分流到章节/命令”，H.9 只给“一眼定位”。两者冲突时以 C.0 的排查顺序为准。

| 症状 | 先看 | 最常见根因 |
|---|---|---|
| RayCluster 不 Ready | conditions/Pod | 镜像/资源/探针 |
| Pod Pending | describe pod | 配额/GPU/taint/affinity/节点池上限（第 51 章） |
| ImagePullBackOff | describe pod | 镜像/Secret/网络 |
| CrashLoopBackOff | logs --previous | 命令/权限/OOM |
| worker 不加入 | init 日志/DNS/6379 | 网络/token/版本 |
| RayJob 卡 Initializing | RayCluster/submitter | 集群/镜像/网络 |
| RayJob 卡 Running | Ray job 状态 | 作业本身/队列 |
| RayJob 不结束 | deadline/entrypoint | 无超时/死循环 |
| RayService 503 | selector/label/Serve | 标签/endpoint/路由 |
| 升级卡住 | pending/配额 | 2× 资源/Gateway |
| 删除卡住 | finalizers | Redis cleanup/子资源 |
| 不扩缩容 | autoscaler 日志 | RBAC/上限/资源 |
| 指标异常 | /metrics | 标签/抓取/基数 |

## H.10 采集证据的命令

```bash
# CR
kubectl get raycluster,rayjob,rayservice,raycronjob -A -o yaml > crs.yaml

# 子资源
kubectl get pods,svc,endpointslices,jobs,podgroups,httproute,gateway -A -o yaml > children.yaml

# 事件
kubectl get events -A --sort-by=.lastTimestamp > events.txt

# operator
kubectl -n kuberay-system logs deploy/kuberay-operator --tail=2000 > operator.log

# Ray
kubectl exec <head> -- tar czf - /tmp/ray/session_latest/logs > ray-logs.tgz

# 指标
curl -s http://<operator>:8080/metrics > operator-metrics.txt
```

← [返回目录](README.md)
