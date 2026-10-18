# 附录 A. API 字段速查

本附录按 CRD 列出常用字段。完整 schema 以 KubeRay v1.7.1 的 `apis/ray/v1/*_types.go` 和 CRD YAML 为准。

## A.1 RayCluster

### A.1.1 spec

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `rayVersion` | string | 无 | Ray 版本 |
| `headGroupSpec` | object | 必填 | head 模板 |
| `workerGroupSpecs` | array | 空 | worker group |
| `enableInTreeAutoscaling` | bool | false | 注入 autoscaler |
| `autoscalerOptions` | object | 无 | 扩缩容配置 |
| `suspend` | bool | false | 删除 Pod |
| `managedBy` | string | 无 | 外部控制器 |
| `gcsFaultToleranceOptions` | object | 无 | GCS FT |
| `authOptions` | object | 无 | 认证 |
| `tlsOptions` | object | 无 | mTLS |
| `networkPolicy` | object | 无 | 网络隔离 |
| `historyServerOptions` | object | 无 | History Server |
| `upgradeStrategy` | object | 无 | Recreate/None |
| `headServiceAnnotations` | map | 空 | head Service 注解 |

### A.1.2 headGroupSpec

| 字段 | 说明 |
|---|---|
| `template` | PodTemplateSpec，ray 容器第一个 |
| `headService` | 自定义 Service |
| `enableIngress` / `ingressOptions` | Ingress |
| `serviceType` | 默认 ClusterIP |
| `rayStartParams` | ray start 参数 |
| `resources` | Ray 资源 |
| `labels` | Ray 标签 |

### A.1.3 workerGroupSpecs[]

| 字段 | 默认 | 说明 |
|---|---|---|
| `groupName` | 必填 | 唯一名 |
| `replicas` | 0 | 期望副本 |
| `minReplicas` | 0 | 下限 |
| `maxReplicas` | maxInt32 | 上限 |
| `idleTimeoutSeconds` | 继承 | 空闲缩容 |
| `priority` | 0 | 扩容优先级 |
| `numOfHosts` | 1 | 每 replica Pod 数 |
| `template` | 必填 | Pod 模板 |
| `rayStartParams` | 空 | ray start |
| `resources` / `labels` | 空 | Ray 资源/标签 |
| `scaleStrategy.workersToDelete` | 空 | 指定删除 |
| `suspend` | false | 删除 group Pod |

### A.1.4 autoscalerOptions

| 字段 | 说明 |
|---|---|
| `version` | v1/v2 |
| `upscalingMode` | Default/Aggressive/Conservative |
| `idleTimeoutSeconds` | 默认 60 |
| `image` / `imagePullPolicy` | 镜像 |
| `resources` | 资源 |
| `env` / `envFrom` | 环境 |
| `volumeMounts` | 挂载 |
| `command` / `args` | 覆盖命令 |

### A.1.5 gcsFaultToleranceOptions

| 字段 | 说明 |
|---|---|
| `backend` | redis/rocksdb |
| `redisAddress` | redis 必填 |
| `redisUsername` / `redisPassword` | 凭据 |
| `externalStorageNamespace` | 命名空间 |
| `storage` | rocksdb PVC |
| `activePassiveHeadOptions` | master/未来字段，v1.7.1 未发布 |

### A.1.6 authOptions

| 字段 | 默认 | 说明 |
|---|---|---|
| `mode` | `token`（块存在时） | `disabled`/`token`；整个块不设置时认证关闭 |
| `secretName` | operator 生成 | 包含 `auth_token` 的 Secret |
| `enableK8sTokenAuth` | false | 使用 K8s RBAC 委托认证（Ray 2.55+；不支持 RayJob/RayService） |

### A.1.7 tlsOptions

| 字段 | 默认 | 说明 |
|---|---|---|
| `enabled` | false | 需要 `RayClusterMTLS` gate + cert-manager |

### A.1.8 networkPolicy

| 字段 | 说明 |
|---|---|
| `mode` | `DenyAll`/`DenyAllIngress`/`DenyAllEgress` |
| `head.ingressRules` / `worker.ingressRules` | 额外放行规则 |

注意：`DenyAll`/`DenyAllEgress` 下必须显式放行 DNS egress。

### A.1.9 historyServerOptions

| 字段 | 说明 |
|---|---|
| `collectorOptions.image` | collector sidecar 镜像 |
| `collectorOptions.resources` | sidecar 资源 |
| `collectorOptions.env` | collector 环境变量 |

需要 `RayClusterHistoryServer` gate；History Server 本身是独立 Deployment。

### A.1.10 upgradeStrategy

| 字段 | 值 | 说明 |
|---|---|---|
| `type` | `Recreate`/`None` | 未设置时按 `None` 行为（不因 spec 变化自动重建）；只能用于用户直接创建的 RayCluster |

### A.1.11 status

| 字段 | 说明 |
|---|---|
| `state` | ready/suspended/空 |
| `desiredWorkerReplicas` | 期望 worker |
| `availableWorkerReplicas` | Running |
| `readyWorkerReplicas` | Ready |
| `minWorkerReplicas` / `maxWorkerReplicas` | 汇总 |
| `desiredCPU/Memory/GPU/TPU` | 资源 |
| `endpoints` | 端口地址 |
| `head` | podIP/serviceIP/name |
| `conditions` | 条件 |
| `observedGeneration` | 已处理 generation |
| `lastUpdateTime` | 更新时间 |
| `reason` / `message` | 原因 |

## A.2 RayJob

### A.2.1 spec

| 字段 | 默认 | 说明 |
|---|---|---|
| `entrypoint` | 无 | 命令 |
| `runtimeEnvYAML` | 无 | runtime env |
| `jobId` | 自动 | 作业 ID |
| `metadata` | 空 | 作业元数据 |
| `rayClusterSpec` | 无 | 集群模板 |
| `clusterSelector` | 无 | 复用集群 |
| `submissionMode` | K8sJobMode | 提交模式 |
| `submitterPodTemplate` | 无 | submitter Pod |
| `submitterConfig.backoffLimit` | 2 | submitter Job 重试（与 RayJob 级 `backoffLimit` 不同） |
| `shutdownAfterJobFinishes` | false | 完成删集群 |
| `ttlSecondsAfterFinished` | 0 | `shutdownAfterJobFinishes` 的延迟（`endTime + TTL`）；不是 RayJob CR 的 TTL |
| `activeDeadlineSeconds` | 无 | 总时限 |
| `preRunningDeadlineSeconds` | 无 | 到 Running 时限 |
| `backoffLimit` | 0 | 失败重试 |
| `suspend` | false | 暂停 |
| `managedBy` | 无 | 外部控制器 |
| `deletionStrategy` | 无 | 清理规则 |
| `entrypointResources` | 空 | 预留资源 |
| `entrypointNumCpus` / `entrypointNumGpus` | 0 | 预留 CPU/GPU |

### A.2.2 status

| 字段 | 说明 |
|---|---|
| `jobId` | Ray job ID |
| `rayClusterName` | 集群名 |
| `dashboardURL` | Dashboard |
| `jobStatus` | PENDING/RUNNING/SUCCEEDED/FAILED/STOPPED |
| `jobDeploymentStatus` | Initializing/Running/Complete/Failed/... |
| `reason` / `message` | 原因 |
| `startTime` / `endTime` | 时间 |
| `succeeded` / `failed` | 计数 |
| `rayClusterStatus` | 集群状态快照 |
| `rayJobInfo` | Ray 起止时间 |
| `observedGeneration` | generation |

### A.2.3 deletionStrategy

| 字段 | 说明 |
|---|---|
| `deletionRules[]` | 规则列表，与 legacy `onSuccess/onFailure` 互斥 |
| `deletionRules[].policy` | `DeleteCluster`/`DeleteWorkers`/`DeleteSelf`/`DeleteNone` |
| `deletionRules[].condition.jobStatus` | 触发终态：`SUCCEEDED`/`FAILED`（v1.7.1 CRD enum） |
| `deletionRules[].condition.jobDeploymentStatus` | 触发部署终态：`Failed`（v1.7.1 当前实现只支持这个值） |
| `deletionRules[].condition.ttlSeconds` | 进入终态后延迟多少秒执行 |
| `onSuccess` / `onFailure` | 旧式策略（`DeleteCluster`/`DeleteWorkers`/`DeleteSelf`/`DeleteNone`） |

约束：规则模式与 `shutdownAfterJobFinishes`、legacy `onSuccess/onFailure` 互斥；`clusterSelector` 模式下不能使用任何删除策略。

### A.2.4 entrypoint 资源预留

| 字段 | 类型 | 说明 |
|---|---|---|
| `entrypointNumCpus` | number/string | entrypoint 预留 CPU |
| `entrypointNumGpus` | number/string | entrypoint 预留 GPU |
| `entrypointResources` | JSON string | 自定义资源，如 `'{"Custom1": 2}'`（注意是字符串，不是 map） |

这些字段进入 Ray Job Submission API 的 `entrypoint_num_cpus`/`entrypoint_num_gpus`/`entrypoint_resources`，不是 RayCluster 模板的一部分。

## A.3 RayService

### A.3.1 spec

| 字段 | 说明 |
|---|---|
| `serveConfigV2` | Serve 多应用 YAML |
| `rayClusterConfig` | RayCluster spec |
| `serveService` | 自定义 Serve Service |
| `excludeHeadPodFromServeSvc` | head 不接流量 |
| `suspend` | 暂停 |
| `rayClusterDeletionDelaySeconds` | 旧集群延迟删除，默认 60 |
| `serviceUnhealthySecondThreshold` | 已废弃 |
| `deploymentUnhealthySecondThreshold` | 已废弃 |
| `upgradeStrategy` | NewCluster/增量/None |
| `upgradeStrategy.clusterUpgradeOptions.maxSurgePercent` | 增量升级额外容量上限 |
| `upgradeStrategy.clusterUpgradeOptions.stepSizePercent` | 每次迁移比例 |
| `upgradeStrategy.clusterUpgradeOptions.intervalSeconds` | 迁移间隔 |
| `upgradeStrategy.clusterUpgradeOptions.gatewayClassName` | 创建 Gateway |
| `upgradeStrategy.clusterUpgradeOptions.httpRouteName` | MiniKubeRay 扩展；上游 v1.7.1 未发布 |
| `upgradeStrategy.clusterUpgradeOptions.httpRouteInProgressLabels` | MiniKubeRay 扩展；上游 v1.7.1 未发布 |
| `managedBy` | 外部控制器 |

### A.3.2 status

| 字段 | 说明 |
|---|---|
| `serviceStatus` | Running/空（自 v1.3 弃用；首选 `Ready` condition） |
| `numServeEndpoints` | 接流量 endpoint 数 |
| `activeServiceStatus` | 当前集群状态 |
| `pendingServiceStatus` | 新集群状态 |
| `conditions` | `Ready`、`UpgradeInProgress`、`RollbackInProgress`、`Suspending`、`Suspended`（底层 RayCluster 另有 `RayClusterSuspending`/`RayClusterSuspended`） |
| `observedGeneration` | generation |
| `lastUpdateTime` | 更新时间 |

### A.3.3 active/pending 子状态

| 字段 | 说明 |
|---|---|
| `rayClusterName` | 集群名 |
| `rayClusterStatus` | 集群状态快照 |
| `applicationStatuses` | Serve app 状态 |
| `targetCapacity` | Serve 目标容量 |
| `trafficRoutedPercent` | 流量百分比 |
| `lastTrafficMigratedTime` | 最近迁移时间 |

## A.4 RayCronJob

### A.4.1 spec

| 字段 | 说明 |
|---|---|
| `schedule` | 五段 cron |
| `timeZone` | IANA 时区 |
| `suspend` | 暂停调度 |
| `jobTemplate` | RayJobSpec |

### A.4.2 status

| 字段 | 说明 |
|---|---|
| `lastScheduleTime` | 上次调度时间 |
| `suspended` | MiniKubeRay 扩展 |

## A.5 常用标签/注解

| 键 | 类型 | 说明 |
|---|---|---|
| `ray.io/cluster` | label | 集群名 |
| `ray.io/node-type` | label | head/worker |
| `ray.io/group` | label | worker group |
| `ray.io/is-ray-node` | label | yes |
| `ray.io/serve` | label | Serve 接流量 |
| `ray.io/originated-from-cr-name` | label | 上游 CR 名 |
| `ray.io/originated-from-crd` | label | 上游 CRD |
| `ray.io/cronjob-name` | label | 子 RayJob |
| `ray.io/ft-enabled` | annotation | 旧式 GCS FT |
| `ray.io/external-storage-namespace` | annotation | Redis 命名空间 |
| `ray.io/overwrite-container-cmd` | annotation | 不覆盖命令 |
| `ray.io/enable-serve-service` | annotation | 创建 Serve Service |
| `ray.io/disable-provisioned-head-restart` | annotation | 集群 provisioned 后不自动重建 head；Sidecar 模式使用，也可用于维护窗口 |
| `ray.io/upgrade-strategy-recreate-hash` | annotation（head Pod） | `upgradeStrategy: Recreate` 的 spec hash 基线 |
| `ray.io/initializing-timeout` | annotation | RayService 初始化超时 |
| `ray.io/cronjob-scheduled-timestamp` | annotation | 子 RayJob 的计划时刻（RFC3339，上游 v1.7 写入） |
| `ray.io/ippr` | annotation（RayCluster） | IPPR 上限：`{"groups":{"<group>":{"max-cpu":"4","max-memory":"4Gi","resize-timeout":60}}}` |
| `ray.io/ippr-status` | annotation（Pod） | autoscaler 写入的 resize 状态；键名以目标版本为准，MiniKubeRay 用 `state/previous_*/target_*` |

## A.6 Condition 类型

| 对象 | 类型 |
|---|---|
| RayCluster | `HeadPodReady`、`RayClusterProvisioned`（粘性，不随失败/suspend 重置）、`ReplicaFailure`（Go 常量 `RayClusterReplicaFailure`）、`RayClusterSuspending`、`RayClusterSuspended` |
| RayJob | `JobSubmission`、`JobDeployment`、`JobFinished`（以便 MiniKubeRay 教学与断言；上游主要看 `status.jobDeploymentStatus`/`jobStatus`） |
| RayService | `Ready`、`UpgradeInProgress`、`RollbackInProgress`、`Suspending`、`Suspended` |
| Pod（Kubernetes 1.35） | `PodResizePending`、`PodResizeInProgress`（IPPR 的 kubelet 进度） |

## A.7 状态枚举

| 字段 | 值 |
|---|---|
| RayCluster.state | `ready`、`suspended`、空（`failed` 已弃用但仍在 schema 中） |
| RayJob.jobStatus | 空、`PENDING`、`RUNNING`、`STOPPED`、`SUCCEEDED`、`FAILED` |
| RayJob.jobDeploymentStatus | 空、`Initializing`、`Running`、`Complete`、`Failed`、`ValidationFailed`、`Suspending`、`Suspended`、`Retrying`、`Waiting` |
| RayService.serviceStatus | `Running`、空 |
| GCS FT backend | `redis`、`rocksdb` |
| submissionMode | `K8sJobMode`、`HTTPMode`、`InteractiveMode`、`SidecarMode` |
| upgradeStrategy (RayService) | `NewCluster`、`NewClusterWithIncrementalUpgrade`、`None` |
| upgradeStrategy (RayCluster) | `Recreate`、`None` |

## A.8 命名、长度与不可变约束

| 对象 | 名称上限 | 规则 |
|---|---:|---|
| RayCluster | 53 | DNS-1035（小写字母开头，含 `-`/数字） |
| RayJob | 47 | DNS-1035 |
| RayService | 47 | DNS-1035 |
| RayCronJob | 36 | DNS-1035 |
| worker group `groupName` | 63 | DNS-1123 label |

其他约束：

- Ray 容器必须是**第一个应用容器**（`spec.containers[0]`）；上游按位置识别，不强制名字。示例通常命名为 `ray`，MiniKubeRay 为了教学清晰额外要求名字为 `ray`；
- `managedBy` 允许 `kueue.x-k8s.io/multikueue`、`ray.io/kuberay-operator`（或空），且不可变；
- `upgradeStrategy` 只能由用户直接创建的 RayCluster 设置，RayJob/RayService 的嵌套配置里不允许；
- `rayStartParams` 与顶层 `resources`/`labels` 不能重复设置同一资源；
- GCS FT 的 `redisAddress` 与 `storage` 不能同时出现；
- `authOptions.enableK8sTokenAuth` 不支持 RayJob/RayService。

完整校验以 CRD OpenAPI schema + 控制器为准；MiniKubeRay 的实现见 `minikuberay/models/validation.py`。

← [返回目录](README.md)
