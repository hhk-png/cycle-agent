# 第 22 章 实现：RayJob、RayService 与 RayCronJob

本章拆三个“上层”控制器：它们复用 RayCluster，但各自有独立的状态机和清理语义。

## 22.1 RayJob 控制器

文件：`minikuberay/controllers/rayjob.py`。

### 22.1.1 Reconcile 分派

```python
if deleting: cleanup()
if no finalizer: add finalizer
default + validate
if suspend and not suspending/suspended: -> Suspending
if not suspend and suspended: -> New
if deployment New: initialize()
if Initializing/Waiting: initialize_or_wait()
if Running: running()
if Suspending/Retrying: cleanup_for_suspend_or_retry()
if Suspended: no-op
if Complete/Failed: finished()
```

### 22.1.2 初始化

- 设置 `startTime`；
- 生成/保留 `jobId`；
- 生成 `rayClusterName`（clusterSelector 模式除外）；
- `JobDeploymentStatus=Initializing`；
- condition `JobSubmission=False, NotSubmitted`。

### 22.1.3 Initializing/Waiting

- 找/建 RayCluster；
- 更新 `rayClusterStatus`、`dashboardURL`；
- `preRunningDeadlineSeconds` 或 `activeDeadlineSeconds` 超时 -> Failed（两个 deadline 在 Initializing/Waiting 阶段都生效）；
- 集群未 Ready -> 保持 Initializing；
- InteractiveMode -> Waiting；
- K8sJobMode -> 创建 submitter Job；
- 其他模式 -> 提交 Ray job（把 `entrypointNumCpus`/`entrypointNumGpus`/`entrypointResources` 写进模拟作业的 metadata）；
- -> Running。

### 22.1.4 Running

- suspend -> Suspending；
- `activeDeadlineSeconds` -> 停止作业 + Failed；
- 集群消失 -> Failed；
- 从 Runtime 读取 job 状态；
- 终态 -> Complete/Failed、endTime、rayJobInfo、conditions、submitter 完成注解；
- 失败 -> `failed++`；成功 -> `succeeded++`；
- 不重复计数（只在第一次进入终态时）。

### 22.1.5 终止与清理

`_finished`：

- 删除 submitter；
- 计算 deletion policy：
  - legacy `shutdownAfterJobFinishes` + TTL；
  - deletion rules；
  - `DeleteCluster`/`DeleteWorkers`/`DeleteSelf`/`DeleteNone`；
- 失败且可重试 -> Retrying；
- `Retrying`/`Suspending` -> 删除集群/Job，重置状态；
- `DeleteSelf` -> 删除 RayJob；
- `DeleteWorkers` -> 删除 worker Pod，保留 head；
- `DeleteCluster` -> 删除 RayCluster；
- legacy `shutdownAfterJobFinishes`：立即删除 RayCluster，`ttlSecondsAfterFinished` 到期后再删除 RayJob CR；
- `clusterSelector`：跳过所有删除策略，共享集群永不被删除。

### 22.1.6 四种提交模式

- K8sJobMode：`_ensure_submitter_job` 创建 Job；BatchJobController 创建 Pod；Runtime 提交作业；结束时给 Pod 打完成注解；
- HTTPMode：不创建 submitter，直接 Runtime 提交；
- SidecarMode：`_create_cluster` 在 head 模板注入 `ray-job-submitter` 容器；
- InteractiveMode：不提交；等待 `spec.jobId`，然后转为 Running。

模拟边界：K8sJobMode 的 submitter Job/Pod 会被创建、注入 `RAY_JOB_SUBMISSION_ID`/`RAY_DASHBOARD_ADDRESS`/`FQ_RAY_IP`，其失败会把 RayJob 置为 `SubmissionFailed`。SidecarMode 只把 `ray-job-submitter` 容器注入 head 模板，MiniKubeRay 不观察该容器的退出码，因此 `SubmissionFailed` 分支实际只对 K8sJobMode 生效；真正提交仍由 Runtime 状态机完成，不执行 `ray job submit`。真实 operator 的 submitter 是实际提交者。

### 22.1.7 clusterSelector

- 查找匹配的 RayCluster；
- 不创建新集群；
- 不支持 suspend/Sidecar/backoff；
- 终态不删除共享集群；
- 没有匹配时等待，preRunningDeadline 可兜底。

### 22.1.8 finalizer

删除时：

- 如果 JobStatus 非终态，调用 Runtime stop；
- 删除 submitter；
- 移除 finalizer；
- ownerReference GC 清理集群。

### 22.1.9 测试

`tests/test_rayjob.py`：

- 成功/失败；
- submitter 创建/清理；
- shutdown/TTL；
- backoff 重试；
- active/preRunning deadline；
- suspend/resume；
- deletion rules（DeleteCluster/DeleteSelf/DeleteWorkers）；
- clusterSelector；
- HTTP/Sidecar/Interactive；
- 显式 jobId；
- 校验失败；
- 删除运行中作业；
- runtime env/metadata；
- entrypoint 资源预留的校验与转发；
- 非法 entrypoint 资源导致 ValidationFailed。

## 22.2 RayService 控制器

文件：`minikuberay/controllers/rayservice.py`。

### 22.2.1 revision

`cluster_spec_hash` 只包含需要新建集群的字段：

- `rayClusterConfig`（排除 worker `replicas`/`minReplicas`/`maxReplicas`/`workersToDelete`）；
- **不包含** `serveConfigV2` 和 `excludeHeadPodFromServeSvc`——它们原地应用到 active 集群。

生成 10 位 hash；集群名 `<service>-<hash[:5]>`，标签 `minikuberay.io/service-revision`。只改 Serve 配置时 active 集群名不变，不会产生第二套集群。

### 22.2.2 初始创建

- 没有 active -> 创建 active；
- 确保稳定的 `<service>-head-svc` 和 `<service>-serve-svc`（前者 selector 指向 active 集群；底层 RayCluster 自己的 `<cluster>-head-svc` 仍会由 RayCluster 控制器创建）；
- 部署 Serve；
- 更新 `ray.io/serve` 标签；
- status active。

### 22.2.3 Serve 部署

- `runtime.deploy_serve` 保存配置/部署时间；
- `runtime.serve_status` 在延迟后返回 RUNNING；
- `_update_serve_labels`：
  - active 且 ready -> head/worker `ray.io/serve=true`；
  - `excludeHeadPodFromServeSvc` -> head 永远 false；
  - NewCluster 的 pending -> false；增量升级的 pending 在 Serve RUNNING 后 -> true（否则 HTTPRoute 会打到空 Service）；
- `numServeEndpoints` 数 ready + serve=true 的 Pod。

### 22.2.4 NewCluster 升级

- 只有 revision hash（镜像/模板等）变化才创建 pending；只改 `serveConfigV2`/`excludeHeadPodFromServeSvc` 时原地部署，active 集群不变；
- active revision != desired -> 创建 pending；
- 部署 Serve；
- pending ready + apps RUNNING -> `_promote`：
  - active = pending；
  - pending 清空；
  - Serve/head Service selector 指向新集群；
  - 旧集群标 `minikuberay.io/delete-after`；
  - `_reap_old_clusters` 到期删除。

### 22.2.5 增量升级

- `NewClusterWithIncrementalUpgrade`；
- 为 active/pending 各创建 `<cluster>-serve-svc`；
- `_advance_traffic` 每 `intervalSeconds` 增加 `stepSizePercent`；
- `targetCapacity = min(maxSurgePercent, max(step, current))`，随流量预热；
- `_ensure_upgrade_routing` 创建 Gateway/HTTPRoute，`backendRefs` 权重 = 100-current/current；
- 支持 `gatewayClassName`（创建 Gateway）或 `httpRouteName`（只 patch backendRefs）；
- 到 100 -> promote，路由只指向新 active；
- 回滚：desired == active revision 且 pending 存在 -> 删除 pending，路由回 active；
- 切换策略时 `_delete_routing` 清理遗留 Gateway/HTTPRoute。

### 22.2.6 回滚

- 检测 spec 回退；
- 删除 pending；
- 清空 pending status；
- 保持 active；
- 设置/清除 RollbackInProgress。

### 22.2.7 suspend/delete

- suspend：删除 owned RayCluster/Service/EndpointSlice，状态 suspended；恢复时重建；
- delete：等待 RayCluster 清理，移除 finalizer。

### 22.2.8 测试

`tests/test_rayservice.py`：

- 初始 Ready/endpoints；
- Service selector；
- exclude head；
- NewCluster 升级；
- 增量升级；
- 回滚；
- suspend/resume；
- 删除；
- 非法 serve config；
- 自定义 Serve Service；
- upgradeStrategy None。

## 22.3 RayCronJob 控制器

文件：`minikuberay/controllers/raycronjob.py`。

### 22.3.1 流程

1. 删除中 -> 返回（子资源由 ownerRef GC）；
2. 默认/校验；
3. suspend -> 记录状态/事件；
4. 计算下一次调度（lastScheduleTime 或 creationTimestamp）；
5. 未到 -> 返回；
6. 创建确定性子 RayJob；
7. 更新 lastScheduleTime。

### 22.3.2 名字

```python
f"{cronjob_name}-{int(scheduled.timestamp()) // 60}"
```

v1.7.1 的上游 `getRayJobName` 同样使用 `scheduledTime.Unix()/60` 的确定性格式，MiniKubeRay 与之一致。v1.6.x 使用随机后缀（issue #4849），升级时要区分版本。

### 22.3.3 时区

- `next_schedule` 使用 `load_timezone`；
- UTC/常见时区有固定偏移回退（Windows 无 tzdata 时）；
- 有 tzdata 时优先 ZoneInfo。

### 22.3.4 测试

`tests/test_raycronjob.py`：

- 到点创建；
- ownerReference；
- suspend；
- 时区；
- 非法 schedule；
- feature gate；
- 不重复；
- 多个 tick。

## 22.4 状态机对照表

| 对象 | 状态字段 | 终态 |
|---|---|---|
| RayJob | `jobStatus`：Ray 作业状态；`jobDeploymentStatus`：KubeRay 部署状态 | `jobStatus` = SUCCEEDED/FAILED/STOPPED；`jobDeploymentStatus` = Complete/Failed |
| RayService | `serviceStatus` + active/pending | Running/空 |
| RayCronJob | `lastScheduleTime` | 无终态（持续调度） |

## 22.5 三个控制器的共同点

- 都复用 RayCluster；
- 都用 finalizer/ownerReference；
- 都在 status 变化时才写；
- 都通过 Runtime 读取“外部状态”；
- 都有 suspend 语义（RayCronJob 是暂停调度）；
- 都有 feature gate/validation。

## 22.6 小结

- RayJob 是“集群 + 提交 + 清理”的状态机，四种模式对应不同网络/隔离需求。
- RayService 是“active/pending + Serve + 流量”的状态机。
- RayCronJob 是“确定性创建 RayJob”的薄控制器。
- 三者都依赖 RayCluster 的收敛，测试要按“先集群、后作业/服务”的顺序驱动。

下一章 APIServer、Dashboard、CLI 与指标。

→ [第 23 章 实现：APIServer、Dashboard、CLI 与指标](kuberay教程-23-实现-APIServer、Dashboard、CLI与指标.md)
