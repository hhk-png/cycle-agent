# 第 5 章 RayCluster CRD 深度解析

`RayCluster` 是 KubeRay 的核心 CRD。RayJob 的 `rayClusterSpec`、RayService 的 `rayClusterConfig`、RayCronJob 的 `jobTemplate.rayClusterSpec` 都是它的子集或模板。读懂 RayCluster，其他三个 CRD 就只是生命周期的差异。

本章按 v1.7.1 的 `raycluster_types.go` 组织字段，并说明每个字段对 Pod/Service 的影响。

## 5.1 顶层结构

```text
apiVersion: ray.io/v1
kind: RayCluster
metadata:
  name: demo
  namespace: default
spec:
  rayVersion: "2.59.0"
  headGroupSpec: { ... }
  workerGroupSpecs: [ ... ]
  enableInTreeAutoscaling: false
  autoscalerOptions: { ... }
  suspend: false
status:
  state: ready
  ...
```

| 字段 | 类型 | 默认 | 作用 |
|---|---|---|---|
| `rayVersion` | string | 无（建议显式设置） | 镜像/命令/兼容性判断的基础 |
| `headGroupSpec` | object | 必填 | head Pod 的模板 |
| `workerGroupSpecs` | array | 空 | 零个或多个 worker group |
| `enableInTreeAutoscaling` | bool | false | 是否注入 autoscaler sidecar |
| `autoscalerOptions` | object | 无 | autoscaler 版本、模式、资源、环境 |
| `suspend` | bool | false | 删除所有 Pod，保留 CR |
| `managedBy` | string | 无 | 交给 Kueue MultiKueue 等外部控制器 |
| `gcsFaultToleranceOptions` | object | 无 | GCS 容错（Redis/RocksDB） |
| `authOptions` | object | 无 | token/K8s RBAC 认证 |
| `tlsOptions` | object | 无 | mTLS（cert-manager，alpha） |
| `networkPolicy` | object | 无 | 网络隔离（alpha） |
| `historyServerOptions` | object | 无 | History Server collector sidecar（发布说明为 beta，gate 默认关闭，阶段以 tag 为准） |
| `upgradeStrategy` | object | 无 | Pod 升级策略 `Recreate`/`None` |
| `headServiceAnnotations` | map | 空 | 注入 head Service 注解 |

## 5.2 headGroupSpec

### 5.2.1 字段

| 字段 | 说明 |
|---|---|
| `template` | `PodTemplateSpec`，Ray 容器必须是第一个应用容器（`containers[0]`）；名字通常叫 `ray`，上游按位置识别 |
| `headService` | 自定义 head Service（名称、注解、端口、类型） |
| `enableIngress` | 是否为 Dashboard 创建 Ingress |
| `ingressOptions` | host/path/pathType/TLS |
| `serviceType` | head Service 类型，默认 `ClusterIP`（实际为 headless） |
| `rayStartParams` | 传给 `ray start` 的参数 |
| `resources` | Ray 节点级自定义资源（不是 K8s requests） |
| `labels` | Ray 节点标签，同时镜像到 Pod labels |

### 5.2.2 最小 head 模板

```yaml
apiVersion: ray.io/v1
kind: RayCluster
metadata:
  name: minimal
spec:
  rayVersion: "2.59.0"
  headGroupSpec:
    rayStartParams: {}
    template:
      spec:
        restartPolicy: Never
        containers:
          - name: ray
            image: rayproject/ray:2.59.0
            resources:
              requests:
                cpu: "1"
                memory: 2Gi
```

KubeRay 会补全：

- `ray start --head --port=6379 --dashboard-host=0.0.0.0 --metrics-export-port=8080 --block`
- 环境变量 `RAY_CLUSTER_NAME`、`RAY_CLUSTER_NAMESPACE`、`RAY_ADDRESS`、`FQ_RAY_IP` 等；
- `/dev/shm` 内存 emptyDir；
- 容器端口 6379/8265/10001/8080/8000；
- 默认 metrics 端口（如果用户没写）；
- 可选 readiness/liveness probe。

### 5.2.3 rayStartParams 的边界

常见参数：

| 参数 | 作用 |
|---|---|
| `dashboard-host` | Dashboard 监听地址 |
| `num-cpus` | Ray 可见 CPU 数 |
| `num-gpus` | Ray 可见 GPU 数 |
| `object-store-memory` | 对象存储上限 |
| `memory` | Ray 可见内存 |
| `resources` | 自定义资源 JSON |
| `labels` | **v1.7.1 会直接拒绝**：`rayStartParams['labels'] is not supported`，请使用顶层 `labels` |
| `autoscaling-config` | autoscaler v1 配置 |
| `plasma-directory` | 自定义对象存储目录（设置后不再自动挂 `/dev/shm`） |

KubeRay 1.5 起推荐：

```yaml
headGroupSpec:
  rayStartParams: {}
  resources:
    Custom1: "1"
  labels:
    ray.io/zone: us-west-2a
```

顶层 `resources`/`labels` 是推荐写法，并会镜像到 Pod 标签；不要同时在这些顶层字段和 `rayStartParams` 里设置资源（KubeRay 校验会拒绝）。

### 5.2.4 head 是否应该承载业务 task

默认情况下，head 的 CPU 也会被 Ray 视为可调度资源。对共享集群，这通常不是好事：

- head 同时运行 GCS、Dashboard、autoscaler sidecar 和用户 task；
- task 可能把 head 的 CPU 吃满，导致 GCS/健康检查抖动；
- head 故障的代价远高于 worker。

常见做法是让 head 只做控制面：

```yaml
headGroupSpec:
  rayStartParams:
    num-cpus: "0"
  template:
    spec:
      containers:
        - name: ray
          resources:
            requests: {cpu: "2", memory: 8Gi}
            limits: {cpu: "2", memory: 8Gi}
```

这样 Ray 不会把业务 task 调度到 head；head 仍然需要 2 核来跑 GCS/Dashboard。例外：

- 单机/开发集群，head 也要跑 task，不要设 0；
- Ray Serve 的 proxy 默认可能跑在 head 上，需要确认副本放置；
- 某些算子/驱动需要 head 资源，先测再改。

## 5.3 workerGroupSpecs

### 5.3.1 字段

| 字段 | 默认 | 说明 |
|---|---|---|
| `groupName` | 必填 | group 唯一名，DNS-1123 label |
| `replicas` | 0 | 期望副本数（autoscaler 修改它） |
| `minReplicas` | 0 | 缩容下限 |
| `maxReplicas` | 2147483647 | 扩容上限 |
| `idleTimeoutSeconds` | 继承 autoscaler | 空闲多久后缩容（v2） |
| `priority` | 0 | 多 group 扩容优先级（Ray >= 2.56） |
| `numOfHosts` | 1 | 每个 replica 包含几个 Pod（多主机） |
| `template` | 必填 | 同构 Pod 模板 |
| `rayStartParams` | 空 | worker 的 `ray start` 参数 |
| `resources` / `labels` | 空 | Ray 资源/标签 |
| `scaleStrategy.workersToDelete` | 空 | 指定要删除的 Pod（autoscaler 用） |
| `suspend` | false | 删除该 group 所有 Pod（仅供 RayJob deletion policy 使用） |

### 5.3.2 副本数语义

KubeRay 的 `GetWorkerGroupDesiredReplicas`：

```text
desired = clamp(replicas, minReplicas, maxReplicas) * numOfHosts
```

如果 `replicas` 未设置或小于 `minReplicas`，按 `minReplicas`；大于 `maxReplicas`，按 `maxReplicas`。`status.desiredWorkerReplicas` 是所有 group 的 `desired` 之和。

### 5.3.3 多主机（multi-host）

`numOfHosts > 1` 时，一个 replica 由多个 Pod 组成（例如 TPU v4/v6e 的多切片训练）。KubeRay 会：

- 创建 `${cluster}-headless` Service；
- 给 Pod 打三个索引标签：
  - `ray.io/worker-group-replica-index`：replica 序号（0..replicas-1）；
  - `ray.io/worker-group-replica-name`：replica 的唯一名，同一 replica 的所有 host 共享；
  - `ray.io/replica-host-index`：replica 内的 host 序号（0..numOfHosts-1）；
- 让同一 replica 的 Pod 通过 headless Service 互相发现（DNS：`<pod>.<headless>.<ns>.svc.<domain>`）。

调度语义：

- `replicas` 控制 replica 数，总 Pod 数 = `replicas * numOfHosts`；
- 一个 replica 是**原子单位**：任一 host 失败/缺失，整个 replica 会被删除重建；
- 缩容删除整个 replica，不会留下半个切片；
- autoscaler 的 `workersToDelete` 命中任一 host，也会删除整个 replica；
- `minReplicas/maxReplicas` 以 replica 为单位，不是 Pod。

`RayMultiHostIndexing` 在 v1.7 默认开启。没有该 gate 时，索引标签/多主机调和行为会不同。

TPU 场景通常还需要：

```yaml
workerGroupSpecs:
  - groupName: tpu
    numOfHosts: 2
    template:
      spec:
        nodeSelector:
          cloud.google.com/gke-tpu-topology: 2x2x2
        containers:
          - name: ray
            resources:
              requests:
                google.com/tpu: "4"
```

具体资源名/拓扑标签随云厂商与 TPU 代际变化，以 device plugin 文档为准。

## 5.4 自动扩缩容字段

```yaml
spec:
  enableInTreeAutoscaling: true
  autoscalerOptions:
    version: v2
    upscalingMode: Default
    idleTimeoutSeconds: 60
    resources:
      requests: {cpu: 500m, memory: 512Mi}
    env:
      - name: RAY_AUTOSCALER_LOG_LEVEL
        value: INFO
```

| 字段 | 说明 |
|---|---|
| `version` | `v1`/`v2`；不设置时按 Ray 版本推断（2.48+ 默认 v2） |
| `upscalingMode` | `Default`/`Aggressive`/`Conservative` |
| `idleTimeoutSeconds` | 空闲缩容时间；worker group 可覆盖 |
| `resources` | autoscaler sidecar 的资源 |
| `image` | 覆盖 autoscaler 镜像（测试用） |
| `env` / `envFrom` | 注入环境变量 |
| `volumeMounts` | 挂载证书等 |
| `command` / `args` | 覆盖默认命令 |

autoscaler v2 使用 `RAY_CLOUD_INSTANCE_ID`（Pod 名）和 `RAY_NODE_TYPE_NAME`（group 名）与 KubeRay API 交互。v1 使用 `autoscaling-config` 和 Ray 的旧接口。字段语义在[第 9 章](kuberay教程-09-自动扩缩容-Ray-Autoscaler与KubeRay.md)展开，决策算法与容量模型在[第 34 章](kuberay教程-34-Autoscaler-v2内部机制与容量模型.md)。

### 5.4.1 IPPR：用注解声明垂直上限

In-Place Pod Resizing（IPPR）不是 `spec` 字段，而是 RayCluster 的注解：

```yaml
metadata:
  annotations:
    ray.io/ippr: '{"groups":{"small-group":{"max-cpu":"4","max-memory":"4Gi","resize-timeout":60}}}'
```

它约束的是 worker group 每个 Pod 的 CPU/memory 上限，要求 autoscaler v2、`enableInTreeAutoscaling: true`、`rayStartParams` 不含 `num-cpus`/`memory`、Ray 容器有显式 requests、`resizePolicy: NotRequired`。完整契约、决策顺序与限制见[第 57 章](kuberay教程-57-垂直弹性-KubeRay-IPPR与原地Pod扩缩.md)。

## 5.5 GCS 容错

### 5.5.1 Redis 后端

```yaml
spec:
  gcsFaultToleranceOptions:
    backend: redis
    redisAddress: redis.default.svc.cluster.local:6379
    redisPassword:
      valueFrom:
        secretKeyRef:
          name: redis-password
          key: password
    externalStorageNamespace: demo-ray
```

| 字段 | 说明 |
|---|---|
| `backend` | `redis`（默认）/`rocksdb` |
| `redisAddress` | 外部 Redis 地址（redis 必填） |
| `redisUsername`/`redisPassword` | 凭据，支持 `valueFrom` |
| `externalStorageNamespace` | Redis 中的命名空间，隔离多个集群 |
| `activePassiveHeadOptions` | active-passive head HA（master/REP-65 方向，v1.7.1 未发布） |

### 5.5.2 RocksDB 后端（v1.7 alpha）

```yaml
spec:
  gcsFaultToleranceOptions:
    backend: rocksdb
    storage:
      size: 10Gi
      storageClassName: fast-ssd
      accessModes: [ReadWriteOnce]
      deletionPolicy: Retain
```

RocksDB 把 GCS 状态持久化到 head 的 PVC 上，不依赖 Redis。限制：

- 单写者；同一时间只能一个 head 挂载；
- 需要 `GCSFaultToleranceEmbeddedStorage` feature gate；
- 升级/故障切换需要外部协调；
- `Retain` 可以让 PVC 在集群删除后保留，供恢复使用。

### 5.5.3 验证规则

- `redis` 后端必须有 `redisAddress`；
- `rocksdb` 后端不允许 `redisAddress`；
- 不能同时设置 `ray.io/ft-enabled` 注解和 `gcsFaultToleranceOptions`；
- 不能在 `rayStartParams`/env 里重复设置 Redis 密码/地址；
- active-passive 是 master/REP-65 的未来字段；若启用，必须满足 `leaseDurationSeconds > renewDeadlineSeconds > retryPeriodSeconds`。

## 5.6 认证与 TLS

### 5.6.1 AuthOptions（v1.6+）

```yaml
spec:
  authOptions:
    mode: token
    secretName: ray-cluster-token
    enableK8sTokenAuth: false
```

| 字段 | 说明 |
|---|---|
| `mode` | `disabled`/`token`，默认 `token` |
| `secretName` | 引用包含 `auth_token` 的 Secret；不填则 operator 生成 |
| `enableK8sTokenAuth` | 使用 Kubernetes RBAC 委托认证（Ray 2.55+） |

注意：**整个 `authOptions` 块不设置时认证是关闭的**；`mode: token` 只是该块存在且未显式写 mode 时的默认值。`enableK8sTokenAuth` 会设置 `RAY_ENABLE_K8S_TOKEN_AUTH=true`，并挂载 ServiceAccount token。需要给 Raylet 的 ServiceAccount 授予 `ray:write` 自定义 verb。该功能目前不支持 RayJob/RayService。

### 5.6.2 TLSOptions（v1.7 alpha）

```yaml
spec:
  tlsOptions:
    enabled: true
```

需要：

- `RayClusterMTLS` feature gate；
- cert-manager；
- 证书 SAN 随 Pod IP 变化而更新；
- head/worker/autoscaler 容器注入 `RAY_USE_TLS`、证书路径。

mTLS 保护 Pod 之间的 gRPC/对象传输，不替代外部入口的 TLS（Ingress/Gateway 仍需配置）。

## 5.7 NetworkPolicy（v1.7 alpha）

```yaml
spec:
  networkPolicy:
    mode: DenyAll
    head:
      ingressRules:
        - from:
            - namespaceSelector:
                matchLabels:
                  kubernetes.io/metadata.name: monitoring
          ports:
            - port: 8080
```

模式：

- `DenyAll`：拒绝所有 ingress/egress，但保留集群内 Pod 互通；
- `DenyAllIngress`：只拒绝入站；
- `DenyAllEgress`：只拒绝出站。

注意：**DNS 和 kube-apiserver egress 都不会自动放行**。在 `DenyAll`/`DenyAllEgress` 下必须显式允许：

- kube-dns（通常 namespace `kube-system`，端口 53/UDP/TCP）；
- kube-apiserver（autoscaler、token auth/RBAC 委托、控制器访问 API 需要；端口通常是 443，地址按集群 Service/Endpoint 确认）；
- RayJob submitter → head Dashboard（8265）；
- 监控/Prometheus 抓取端口（如需要）。

少了 DNS，worker 解析不了 head Service；少了 API server egress，autoscaler 会 403/超时，表面看像 RBAC 问题。

## 5.8 History Server（v1.7 发布说明为 beta，gate 默认关闭）

```yaml
spec:
  historyServerOptions:
    collectorOptions:
      image: quay.io/kuberay/collector:v1.7.1
      resources:
        requests: {cpu: 100m, memory: 128Mi}
```

开启 `RayClusterHistoryServer` 后，operator 会把 collector sidecar 注入 head/worker Pod，采集 Ray 事件并写到对象存储。History Server 本身是独立 Deployment，用于在集群删除后回放 Dashboard。第 8 章展开。

> 字段形状以目标版本的 CRD 为准：`collectorOptions` 是本教程教学 CRD 的子集表达；真实 CRD 若未声明某个子字段，Kubernetes 会静默裁剪。collector 的存储后端/桶/前缀通常还要通过 collector 的 args/env（如 `STORAGE_BACKEND`、`S3_BUCKET`、`STORAGE_ROOT_DIR`）配置，且必须与 History Server Deployment 一致，否则会话不可见。

## 5.9 升级策略

```yaml
spec:
  upgradeStrategy:
    type: Recreate
```

| 值 | 行为 |
|---|---|
| `Recreate` | spec 哈希变化时删除所有 Pod，再创建新 Pod |
| `None` | 不创建新 Pod，保留现有 Pod（用于手动控制） |
| 未设置 | 默认/`None` 行为：不会因 spec 变化自动删除并重建现有 Pod |

注意：`upgradeStrategy` 只能由用户直接创建的 RayCluster 设置；RayJob/RayService 创建的 RayCluster 会拒绝该字段。字段变更的完整影响矩阵、混合版本风险与 head 维护流程见[第 55 章](kuberay教程-55-变更管理-升级、重启与不丢状态.md)。

## 5.10 状态与条件

### 5.10.1 Status 字段

| 字段 | 说明 |
|---|---|
| `state` | `ready`/`suspended`/空；**v1.7.1 已标记 deprecated**，新代码应优先读 conditions。`failed` 仍在 CRD 中但已弃用 |
| `desiredWorkerReplicas` | 期望 worker Pod 总数 |
| `availableWorkerReplicas` | Running 的 worker 数 |
| `readyWorkerReplicas` | Ready 的 worker 数 |
| `minWorkerReplicas`/`maxWorkerReplicas` | 汇总的上下限 |
| `desiredCPU/Memory/GPU/TPU` | 期望资源总量 |
| `endpoints` | client/dashboard/metrics/serve/gcs-server（上游通常只存端口字符串，如 `8265`；MiniKubeRay 为便于打印存 `host:port`，属于教学差异） |
| `head` | podIP/serviceIP/podName/serviceName |
| `conditions` | 结构化状态 |
| `observedGeneration` | 最近处理的 generation |
| `lastUpdateTime` | 最近状态更新时间 |
| `reason`/`message` | 失败原因 |

### 5.10.2 Conditions

| 类型 | 含义 |
|---|---|
| `HeadPodReady` | head Pod 是否 Ready |
| `RayClusterProvisioned` | 首次全部 Pod Ready 后**一直保持 True**；后续 Pod 失败或 suspend 都不会自动重置。不能用它判断“当前是否可用”；当前可用性看 `HeadPodReady`、`readyWorkerReplicas` 或 Pod 状态 |
| `ReplicaFailure` | Pod 创建/删除失败（Go 常量名是 `RayClusterReplicaFailure`，但序列化到 status 的 `type` 是 `ReplicaFailure`） |
| `RayClusterSuspending` | 正在暂停 |
| `RayClusterSuspended` | 已完成暂停 |

`RayClusterSuspending` 与 `RayClusterSuspended` 不能同时为 True。

## 5.11 一个完整的生产型样例

```yaml
apiVersion: ray.io/v1
kind: RayCluster
metadata:
  name: prod-cluster
  namespace: team-a
  labels:
    team: ml
spec:
  rayVersion: "2.59.0"
  enableInTreeAutoscaling: true
  autoscalerOptions:
    version: v2
    upscalingMode: Default
    idleTimeoutSeconds: 120
  headGroupSpec:
    serviceType: ClusterIP
    rayStartParams:
      dashboard-host: "0.0.0.0"
    template:
      metadata:
        labels:
          team: ml
      spec:
        serviceAccountName: ray-head
        restartPolicy: Never
        containers:
          - name: ray
            image: rayproject/ray:2.59.0
            imagePullPolicy: IfNotPresent
            resources:
              requests: {cpu: "2", memory: 8Gi}
              limits: {cpu: "4", memory: 16Gi}
            volumeMounts:
              - name: data
                mountPath: /data
        volumes:
          - name: data
            persistentVolumeClaim:
              claimName: team-a-data
  workerGroupSpecs:
    - groupName: cpu
      replicas: 2
      minReplicas: 0
      maxReplicas: 50
      idleTimeoutSeconds: 120
      priority: 10
      rayStartParams: {}
      template:
        spec:
          restartPolicy: Never
          containers:
            - name: ray
              image: rayproject/ray:2.59.0
              resources:
                requests: {cpu: "4", memory: 8Gi}
    - groupName: gpu
      replicas: 0
      minReplicas: 0
      maxReplicas: 8
      idleTimeoutSeconds: 300
      priority: 0
      rayStartParams: {}
      template:
        spec:
          restartPolicy: Never
          nodeSelector:
            nvidia.com/gpu.product: NVIDIA-A100-SXM4-80GB
          tolerations:
            - key: nvidia.com/gpu
              operator: Exists
              effect: NoSchedule
          containers:
            - name: ray
              image: rayproject/ray:2.59.0-gpu
              resources:
                requests: {cpu: "8", memory: 64Gi, nvidia.com/gpu: "1"}
                limits: {nvidia.com/gpu: "1"}
```

## 5.12 生产模板库：五种常见组合

字段记住了不等于会组合。下面五个模板覆盖大多数生产场景；把它们当作起点，而不是可以直接复制的最终配置。

### 5.12.1 CPU 批处理池

```yaml
spec:
  rayVersion: "2.59.0"
  enableInTreeAutoscaling: true
  autoscalerOptions:
    version: v2
    upscalingMode: Default
    idleTimeoutSeconds: 120
  headGroupSpec:
    template:
      spec:
        containers:
          - name: ray
            image: rayproject/ray:2.59.0
            resources:
              requests: {cpu: "1", memory: 2Gi}
  workerGroupSpecs:
    - groupName: cpu
      replicas: 0
      minReplicas: 0
      maxReplicas: 200
      priority: 100
      template:
        spec:
          containers:
            - name: ray
              image: rayproject/ray:2.59.0
              resources:
                requests: {cpu: "4", memory: 8Gi}
```

适用：Ray Data ETL、Tune、CPU 训练。注意 `maxReplicas` 与节点池/配额对齐；`minReplicas: 0` 时冷启动要算进 SLO。

### 5.12.2 GPU 训练池

```yaml
workerGroupSpecs:
  - groupName: gpu
    replicas: 0
    minReplicas: 0
    maxReplicas: 32
    template:
      spec:
        nodeSelector:
          nvidia.com/gpu.product: NVIDIA-A100-SXM4-80GB
        tolerations:
          - {key: nvidia.com/gpu, operator: Exists, effect: NoSchedule}
        containers:
          - name: ray
            image: rayproject/ray:2.59.0-gpu
            resources:
              requests: {cpu: "8", memory: 64Gi, nvidia.com/gpu: "1"}
              limits: {nvidia.com/gpu: "1"}
```

注意：

- GPU `requests == limits`；
- 顶层 `resources`/`rayStartParams.num-gpus` 与 Pod GPU 数一致；
- 大作业用 gang scheduling/PodGroup，避免半个作业占住资源；
- 节点池要能放下最坏形状（第 36 章）。

### 5.12.3 共享交互集群

```yaml
spec:
  authOptions:
    mode: token
    secretName: team-a-ray-token
  workerGroupSpecs:
    - groupName: shared
      replicas: 2
      minReplicas: 2
      maxReplicas: 10
      idleTimeoutSeconds: 300
```

适用：Notebook、探索式分析、多个 RayJob 通过 `clusterSelector` 复用。

注意：

- `minReplicas > 0` 保留热池，但会增加闲置成本；
- 需要配额/队列控制共享集群的争抢；
- 作业结束不能删除共享集群（`clusterSelector` 的清理策略必须为空）。

### 5.12.4 LLM 推理集群

```yaml
spec:
  rayVersion: "2.59.0"
  enableInTreeAutoscaling: true
  autoscalerOptions:
    version: v2
    idleTimeoutSeconds: 300
  headGroupSpec:
    template:
      spec:
        containers:
          - name: ray
            image: rayproject/ray-llm:2.59.0
            resources:
              requests: {cpu: "4", memory: 16Gi}
  workerGroupSpecs:
    - groupName: gpu
      replicas: 1
      minReplicas: 1
      maxReplicas: 16
      template:
        spec:
          containers:
            - name: ray
              image: rayproject/ray-llm:2.59.0
              resources:
                requests: {cpu: "8", memory: 64Gi, nvidia.com/gpu: "1"}
                limits: {nvidia.com/gpu: "1"}
```

配套 RayService 的 Serve 配置、Serve Service、`excludeHeadPodFromServeSvc` 见第 11、28 章。注意模型缓存、冷启动和 GPU 显存。

### 5.12.5 多主机切片（TPU/大模型）

```yaml
workerGroupSpecs:
  - groupName: slice
    replicas: 2
    minReplicas: 0
    maxReplicas: 8
    numOfHosts: 2
    template:
      spec:
        nodeSelector:
          cloud.google.com/gke-tpu-topology: 2x2x2
        containers:
          - name: ray
            image: rayproject/ray:2.59.0
            resources:
              requests: {google.com/tpu: "4"}
```

注意：

- `replicas` 是 replica 数，总 Pod 数 = `replicas × numOfHosts`；
- 任一 host 失败，整个 replica 重建；
- 必须 gang scheduling，且 `minMember` 覆盖 head + 所有 host；
- 多主机 DNS 依赖 headless Service（第 6 章）。

## 5.13 默认值与校验清单

KubeRay 在调和时做的默认/校验（节选）：

- head 至少一个容器；Ray 容器必须是 `containers[0]`（示例命名为 `ray`；MiniKubeRay 校验更严格，要求名字为 `ray`）；
- `minReplicas <= maxReplicas`；
- 开启 autoscaling 时 `minReplicas`/`maxReplicas` 必须有值（CRD 有默认）；关闭时只使用 `replicas`，min/max 不参与调和；
- autoscaling 开启时 worker group 不能 suspend；
- GCS FT 字段互斥；
- `managedBy` 只能取两个值且不可变；
- `upgradeStrategy` 不能由 RayJob/RayService 设置；
- Ray 名称长度限制（RayCluster 53，RayJob/RayService 47，RayCronJob 36），且必须是 DNS-1035（小写字母开头）。

MiniKubeRay 的对应实现：

- 默认值：`minikuberay/models/defaults.py`
- 校验：`minikuberay/models/validation.py`
- 命名/状态：`minikuberay/models/raycluster.py`

## 5.14 小结

- RayCluster 的 spec 是 head + worker groups + 可选能力（autoscaler、GCS FT、auth、TLS、NetworkPolicy、History Server）。
- worker 副本数会被 clamp 到 min/max，再乘以 `numOfHosts`。
- GCS FT、auth、TLS、NetworkPolicy 都有明确的字段互斥和前置条件。
- status/conditions 是控制器对用户的契约；不要把 status 当配置。
- 生产 YAML 应显式设置 `rayVersion`、资源、重启策略、节点选择与容忍。

下一章把这些字段落到 Pod、Service、网络、存储和调度细节。

→ [第 6 章 Pod、网络、存储与调度](kuberay教程-06-Pod、网络、存储与调度.md)
