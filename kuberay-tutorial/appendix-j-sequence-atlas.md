# 附录 J. 端到端时序图集

正文章节解释“每个组件做什么”，本附录把它们串成“一次操作到底经过哪些步骤”。排障时先定位自己停在哪一张图的哪一步，再回到对应章节看细节。

约定：

- 实线箭头是控制面请求/响应，虚线箭头是异步 watch/轮询；
- 图中省略了 RBAC、缓存同步、重试等横切逻辑；
- Ray 内部进程（GCS/Raylet/worker）用 `Ray` 统一表示，细节见第 35 章；
- 所有图都对应 `minikuberay/` 中可运行的状态机，实验命令见附录 K。

## J.1 RayCluster 首次创建

```mermaid
sequenceDiagram
    autonumber
    participant U as 用户
    participant A as kube-apiserver
    participant O as KubeRay operator
    participant K as kube-scheduler
    participant L as kubelet
    participant R as Ray head
    U->>A: create RayCluster
    A-->>O: watch ADDED
    O->>O: 默认值 + 校验 + spec hash
    O->>A: create ServiceAccount/Role/RoleBinding
    O->>A: create head Service
    O->>A: create head Pod
    A-->>K: bind Pod
    K->>A: Pod.spec.nodeName
    A-->>L: 启动容器
    L->>R: ray start --head
    R->>R: GCS/Dashboard/Raylet 就绪
    L->>A: Pod Ready
    O->>A: create worker Pods
    O->>O: 计算 status/conditions
    O->>A: update status (state=ready)
    Note over O,A: 之后按 resync/事件继续调和
```

排查锚点：

| 停在哪 | 先看 |
|---|---|
| CR 没有默认值/条件 | operator 日志、CRD 是否升级 |
| 没有 Service/Pod | RBAC、operator 错误事件 |
| Pod Pending | 资源/配额/污点/亲和性（附录 C） |
| Pod Running 但不 Ready | head 日志、端口、`ray start` 参数 |
| status 不更新 | status 子资源权限、冲突重试 |

## J.2 RayJob：K8sJobMode

```mermaid
sequenceDiagram
    autonumber
    participant U as 用户
    participant A as kube-apiserver
    participant O as RayJob controller
    participant C as RayCluster controller
    participant J as submitter Job
    participant D as Ray Dashboard
    participant R as Ray driver
    U->>A: create RayJob
    A-->>O: watch ADDED
    O->>A: create RayCluster
    A-->>C: watch ADDED
    C->>C: 调和到 state=ready
    O->>A: create submitter Job
    A-->>J: 启动 submitter Pod
    J->>D: POST /api/jobs/ (submission_id)
    D->>R: 启动 driver/JobSupervisor
    O->>D: GET /api/jobs/{id} (轮询)
    R-->>D: RUNNING -> SUCCEEDED
    O->>A: update status Complete
    O->>A: 按 deletion policy 删除集群/自身
```

关键点：

- submitter 只负责“提交”，不负责“执行”；
- `submission_id` 是幂等键，submitter 重试不会创建第二个作业；
- operator 轮询 Dashboard，不直接读 driver 状态。

## J.3 RayJob：HTTPMode

```mermaid
sequenceDiagram
    autonumber
    participant O as RayJob controller
    participant C as RayCluster controller
    participant D as Ray Dashboard
    O->>C: 创建/等待 RayCluster ready
    O->>D: POST /api/jobs/（operator 进程直连）
    D-->>O: job_id
    loop 轮询
        O->>D: GET /api/jobs/{job_id}
    end
    O->>O: 更新 status / 清理
```

与 K8sJobMode 的差别只有“谁发 HTTP 请求”。因此 operator 必须能解析 head Service；NetworkPolicy 忘记放行 operator 时，会卡在 Initializing/Running 而没有 submitter 日志。

## J.4 RayJob：SidecarMode

```mermaid
sequenceDiagram
    autonumber
    participant O as RayJob controller
    participant C as RayCluster controller
    participant H as head Pod
    participant S as ray-job-submitter sidecar
    participant D as Ray Dashboard (localhost)
    O->>C: 创建 RayCluster（head 注入 sidecar）
    C->>H: 启动 ray + submitter
    S->>D: POST http://127.0.0.1:8265/api/jobs/
    O->>D: 轮询作业状态
    O->>O: 终态/清理
```

关键点：

- sidecar 与 head 共享网络命名空间，不需要 operator→head 连通；
- 不支持 `clusterSelector`、`submitterPodTemplate`、`submitterConfig`；
- head 的 `restartPolicy` 必须是 `Never`；
- head 重启会丢提交上下文，所以 SidecarMode 集群禁止自动重建 head。

## J.5 RayJob：InteractiveMode

```mermaid
sequenceDiagram
    autonumber
    participant U as 用户/Notebook
    participant O as RayJob controller
    participant C as RayCluster
    participant D as Ray Dashboard
    O->>C: 创建集群并等待 ready
    O->>O: jobDeploymentStatus=Waiting
    U->>D: ray job submit / POST /api/jobs/
    D-->>U: job_id
    U->>O: patch spec.jobId=job_id
    O->>D: 轮询该 job_id
    O->>O: Running -> Complete
```

关键点：

- operator 不生成 job ID，也不会替你提交；
- 忘记写 `spec.jobId` 会永远停在 Waiting；
- `backoffLimit` 必须为 0（重试语义由用户负责）。

## J.6 RayService：NewCluster 升级

```mermaid
sequenceDiagram
    autonumber
    participant U as 用户
    participant O as RayService controller
    participant A as active RayCluster
    participant P as pending RayCluster
    participant S as head/serve Service
    U->>O: 修改镜像/模板/serveConfig
    alt 只改 serveConfig/副本
        O->>A: 原地部署/扩缩
    else revision hash 变化
        O->>P: 创建 pending 集群
        P->>P: 部署 Serve，等待 RUNNING
        O->>S: selector/流量切到 pending
        O->>A: 延迟 rayClusterDeletionDelaySeconds 后删除
    end
```

关键点：

- NewCluster 需要 2 倍资源；
- 切换是 Service selector 的改变，长连接可能断开；
- `rayClusterDeletionDelaySeconds` 给 in-flight 请求留时间。

## J.7 RayService：增量升级

```mermaid
sequenceDiagram
    autonumber
    participant O as RayService controller
    participant P as pending RayCluster
    participant G as Gateway/HTTPRoute
    participant A as active RayCluster
    O->>P: 创建集群，target_capacity=0
    P->>P: Serve 应用 RUNNING
    loop 每 intervalSeconds
        O->>G: 调整 backendRefs 权重（stepSizePercent）
        O->>P: 提高 target_capacity（<= maxSurgePercent）
    end
    O->>G: 权重 100% -> pending
    O->>A: 延迟删除旧集群
    Note over O: 稳定 Serve Service 的 selector 最终切到新集群
```

关键点：

- 权重控制“多少流量”，`target_capacity` 控制“副本准备好多少”；
- Gateway 的 idle timeout 要覆盖流式响应；
- 接管已有 HTTPRoute 时只需要 patch `backendRefs`，但 RBAC 必须允许。

## J.8 Autoscaler v2 扩容/缩容

```mermaid
sequenceDiagram
    autonumber
    participant R as Ray (demand)
    participant S as autoscaler sidecar
    participant A as kube-apiserver
    participant C as RayCluster controller
    participant K as kube-scheduler
    R->>S: pending resource demand
    S->>S: 聚合 demand -> 节点类型/数量
    S->>A: patch workerGroup replicas
    A-->>C: watch MODIFIED
    C->>A: create worker Pods
    A-->>K: bind
    K->>R: 新节点加入 Ray
    Note over S: idle 超时后选择 victim
    S->>A: patch replicas + workersToDelete
    C->>A: 删除指定 Pod
```

关键点：

- autoscaler 只改 `replicas`/`workersToDelete`，不直接删 Pod；
- 单一写入者：不要同时手工改 replicas；
- 扩容看起来“没生效”时，依次看 demand、replicas、Pod Pending、节点池上限。

### J.8.1 IPPR：原地扩容与回滚

```mermaid
sequenceDiagram
    autonumber
    participant R as Ray (demand)
    participant S as autoscaler v2
    participant A as kube-apiserver
    participant K as kubelet
    participant RT as Raylet/GCS
    R->>S: pending demand 3 CPU
    S->>S: 当前容量 1 -> 剩余 2<br/>headroom = max-cpu 4 - 1 = 3
    S->>A: resize Pod requests 1 -> 3
    A-->>K: Pod spec 更新
    K->>K: cgroup 调整（异步）
    alt 成功
        K->>A: PodResizeInProgress -> 完成
        S->>RT: 更新逻辑 CPU 为 3
        S->>A: ray.io/ippr-status = succeeded
    else 超时/失败
        K->>A: resize 失败
        S->>A: 恢复 previous requests
        S->>S: retry window 内不再垂直重试
        S->>A: 剩余 demand 走 replicas +1
    end
```

关键点：

- resize 只改 Pod spec，不重建 Pod；UID 不变；
- kubelet 完成 cgroup 后，autoscaler 还要更新 Raylet 的逻辑资源；
- 失败时先回滚，再把 demand 交给水平路径；`ray.io/ippr-status` 与事件是主要证据。

## J.9 GCS FT：head 故障恢复

```mermaid
sequenceDiagram
    autonumber
    participant L as kubelet
    participant O as RayCluster controller
    participant H as 新 head Pod
    participant Redis as Redis
    participant W as workers
    L->>L: 原 head 容器退出 / Pod 删除
    O->>O: 检测 head 不 Ready
    O->>H: 创建新 head Pod
    H->>Redis: 连接 GCS 后端
    H->>H: ray start --head（复用外部存储）
    W->>H: 重新注册/重连
    O->>O: 更新 status/conditions
    Note over W: 作业是否恢复取决于 driver/actor 的容错语义
```

关键点：

- GCS FT 恢复的是**集群元数据**，不保证正在运行的 driver 自动恢复；
- Redis 地址/密码/namespace 必须稳定；
- RocksDB 后端是单写者，不能同时挂两个 head。

## J.10 Operator 启动与缓存同步

```mermaid
sequenceDiagram
    autonumber
    participant M as manager
    participant I as informer cache
    participant A as kube-apiserver
    participant W as workqueue
    participant R as reconciler
    M->>I: 注册 CR/Pod/Service 等 informer
    I->>A: LIST + WATCH
    A-->>I: 全量对象 + 增量事件
    I->>W: ADDED/MODIFIED/DELETED
    W->>R: 取出 key（去重/限速）
    R->>I: 从缓存读对象
    R->>A: 写 status/子资源
    Note over R,A: 写成功后等缓存追上，再处理下一个 key
```

排查锚点：

| 现象 | 可能原因 |
|---|---|
| 启动后长时间不调和 | 缓存未同步、RBAC 无 list/watch 权限 |
| 反复处理同一个 key | 写入了不稳定字段（时间戳/随机数） |
| 删除后还在创建 | 没有先处理 deletionTimestamp |
| 多副本同时写 | leader election 未生效 |

## J.11 Ray 作业内部路径（补充）

```mermaid
sequenceDiagram
    autonumber
    participant C as 客户端/operator
    participant D as Dashboard/Job API
    participant S as JobSupervisor
    participant DR as driver
    participant G as GCS
    participant R as Raylet/worker
    C->>D: 提交 entrypoint + runtime_env
    D->>S: 启动作业
    S->>DR: 创建 driver 进程
    DR->>G: 注册作业/获取集群信息
    DR->>R: task/actor 调度
    R-->>G: 状态/资源更新
    S-->>D: 日志/状态
    D-->>C: 轮询结果
```

这张图解释了为什么“Pod Ready”不等于“作业能跑”：runtime_env 准备、driver 启动、GCS 注册、worker 资源都可能失败。

## J.12 RayCronJob 调度

```mermaid
sequenceDiagram
    autonumber
    participant U as 用户
    participant A as kube-apiserver
    participant O as RayCronJob controller
    participant R as RayJob controller
    U->>A: create RayCronJob(schedule/timeZone)
    A-->>O: watch ADDED
    loop 每次调和
        O->>O: last = status.lastScheduleTime or creationTimestamp
        O->>O: next = cron.next(after=last, tz=timeZone)
        alt next > now
            O->>O: requeue（未到点）
        else next <= now
            O->>A: create RayJob（名字 = <cronjob>-<scheduledTime.Unix()/60>）
            O->>A: status.lastScheduleTime = next
            A-->>R: watch RayJob ADDED
            R->>R: 建集群/提交/清理
        end
    end
```

关键点：create 与 status 写回不是原子的；名字由计划时刻决定（上游和 MiniKubeRay 都使用 unix 分钟），重复 create 依赖 AlreadyExists 去重，不能假设“绝不会重复创建”。

## J.13 suspend / resume

```mermaid
sequenceDiagram
    autonumber
    participant U as 用户
    participant A as kube-apiserver
    participant O as RayCluster controller
    participant K as kubelet
    U->>A: patch spec.suspend=true
    A-->>O: watch MODIFIED
    O->>A: 设置 RayClusterSuspending=True
    O->>A: delete Pods
    A-->>K: 终止容器/清理
    O->>A: RayClusterSuspended=True, Suspending=False
    O->>A: status.state=suspended
    U->>A: patch spec.suspend=false
    A-->>O: watch MODIFIED
    O->>A: 清除 suspend conditions
    O->>A: 重新创建 head/worker Pods
    Note over O: 恢复后 Pod UID 必然变化；Ray 内部状态不会自动恢复
```

## J.14 删除与 finalizer

```mermaid
sequenceDiagram
    autonumber
    participant U as 用户
    participant A as kube-apiserver
    participant O as RayCluster controller
    participant J as Redis cleanup Job
    participant G as Kubernetes GC
    U->>A: delete RayCluster
    A->>A: 设置 deletionTimestamp（不立即删除）
    A-->>O: watch MODIFIED
    O->>O: 处理 finalizer（GCS FT/自定义清理）
    O->>A: 删除/停止 Pod、Job
    opt GCS FT Redis
        O->>A: create cleanup Job
        A-->>J: 运行 Redis namespace 清理
        O->>J: 等待完成或超时
    end
    O->>A: remove finalizer
    A->>G: 对象真正删除
    G->>G: ownerReference 级联删除子资源
```

关键点：finalizer 清理必须幂等且有超时；超时放行可能留下外部垃圾（Redis namespace、PVC、对象存储）。

## J.15 一次 reconcile 的 trace 树

时序图回答“谁调用了谁”；trace 树回答“这一轮实际花了多久、在哪一层出错”。MiniKubeRay 的 `Operator.run_once()` 自动产生：

```text
operator.round (root)
├── reconcile (raycluster, result=success)
├── reconcile (raycluster, result=success)
├── reconcile (rayjob, result=success)
└── reconcile (batchjob, result=success)
```

生产接入 OpenTelemetry 后，子 span 可以继续展开：

```text
rayjob.reconcile
├── ensure_raycluster
├── submit_job
│   ├── http.request (8265)
│   └── retry (backoff=2s)
└── update_status
```

查看方式：

```bash
python -m minikuberay traces --tree --limit 5
curl -s http://127.0.0.1:8080/api/traces/<trace_id>
```

诊断顺序：先找 `status=ERROR` 的 span，再沿 `parent_span_id` 向上找调用者；如果错误 span 的父节点是 `submit_job`，问题在 Ray Dashboard/网络；如果是 `ensure_pod`，问题在调度/镜像/RBAC。

## J.16 用图定位问题

| 症状 | 先看哪张图 | 关键证据 |
|---|---|---|
| RayCluster 一直不 Ready | J.1 | Pod 状态、head 日志、conditions |
| RayJob 卡 Initializing | J.2/J.3 | 集群是否 Ready、submitter 是否存在 |
| RayJob 卡 Waiting | J.5 | `spec.jobId` 是否写回 |
| RayService 升级不切流 | J.6/J.7 | active/pending、Service selector、HTTPRoute 权重 |
| 扩容不生效 | J.8 | demand、replicas、Pending Pod、节点池 |
| 扩容后 Pod 一直 Pending | J.17 | Pending reason、配额、NodePool maxSize、云容量 |
| Pod 卡 Terminating / drain 卡住 | J.18 | finalizer、preStop、PDB、grace |
| head 挂了没恢复 | J.9 | GCS FT 配置、Redis 连通性、新 head 日志 |
| operator 重启后风暴 | J.10 | 缓存同步、status 抖动、事件去重 |
| operator 多副本不切换/脑裂 | J.19 | Lease 续约、租约到期、leader 切换 |
| 定时任务没触发/重复触发 | J.12 | `lastScheduleTime`、子 RayJob 名/标签 |
| suspend 卡住/恢复后不 Ready | J.13 | conditions、Pod UID、最终状态 |
| 删除卡在 Terminating | J.14 | finalizers、cleanup Job、超时事件 |

## J.17 节点供给：从 Pending Pod 到 Raylet 注册

```mermaid
sequenceDiagram
    participant OP as KubeRay operator
    participant API as kube-apiserver
    participant SCH as kube-scheduler
    participant CA as Cluster Autoscaler/Karpenter
    participant CLOUD as Cloud provider
    participant NODE as New node
    participant RAY as Ray head
    OP->>API: create worker Pods (replicas increased)
    API->>SCH: schedule Pod
    SCH-->>API: no node fits (Pending)
    CA->>API: watch unschedulable Pods
    CA->>CLOUD: create instance / increase node group
    CLOUD->>NODE: boot + join
    NODE->>API: Node Ready
    API->>SCH: reschedule
    SCH->>NODE: bind Pod
    NODE->>RAY: raylet registers with GCS
    RAY-->>OP: worker visible in status
```

关键证据链：`Pod.status.conditions[PodScheduled]=False` → `cluster-autoscaler`/`karpenter` 日志 → Node Ready → Pod Bound → `ray status` 里出现节点。任何一步缺失，都不要再等“Ray autoscaler 再扩一次”。

## J.18 Pod 终止与 drain

```mermaid
sequenceDiagram
    participant C as Client/CA
    participant API as kube-apiserver
    participant EP as EndpointSlice controller
    participant K as kubelet
    participant P as Ray container
    participant R as Ray head
    C->>API: DELETE Pod / Eviction
    API->>API: set deletionTimestamp
    API->>EP: Pod terminating
    EP->>EP: remove Ready endpoint
    API->>K: watch deletion
    K->>P: preStop (ray stop / custom)
    P-->>K: preStop returns
    K->>P: SIGTERM
    P->>R: drain/unregister/stop workers
    alt exits before grace
        P-->>K: exit 0
    else grace expired
        K->>P: SIGKILL (137)
    end
    K->>API: Pod terminated
    Note over API: finalizer may still block object deletion
```

关键证据链：`deletionTimestamp` → EndpointSlice 移除 → preStop 事件 → SIGTERM/exit code → finalizer。`kubectl drain` 被 429 拒绝时，先看 PDB 的 `ALLOWED DISRUPTIONS`，不要反复重试。

## J.19 Operator leader election

```mermaid
sequenceDiagram
    participant A as operator-a
    participant B as operator-b
    participant API as kube-apiserver
    participant L as Lease object
    A->>API: acquire Lease (holderIdentity=a)
    API->>L: create/update
    loop every retryPeriod
        A->>API: renew Lease
        B->>API: try acquire (fails, lease valid)
    end
    Note over A: A crashes at t=12
    B->>API: try acquire at t=13..24 (fails)
    Note over L: lease expires at t=25
    B->>API: acquire at t=26
    API->>L: holderIdentity=b
    B->>B: start reconcilers
    Note over A: if A returns, it must not write until it re-acquires
```

关键证据：Lease 的 `holderIdentity`/`renewTime`/`leaseDurationSeconds`、operator 日志中的 “became leader/lost leader”、副本重启次数。`leaseDuration > renewDeadline > retryPeriod` 不满足时，先修参数，不要先怀疑控制器逻辑。

## J.20 变更：Recreate 与 head 维护

```mermaid
sequenceDiagram
    participant U as 用户/GitOps
    participant API as kube-apiserver
    participant OP as kuberay-operator
    participant H as head Pod
    participant W as worker Pods
    participant R as Ray/GCS

    U->>API: patch image + upgradeStrategy=Recreate
    API->>OP: RayCluster MODIFIED
    OP->>OP: default + validate
    OP->>OP: spec hash != stored hash
    OP->>API: delete head/worker Pods
    API->>H: deletionTimestamp
    H->>R: SIGTERM / ray stop
    API->>W: deletionTimestamp
    W->>R: SIGTERM / ray stop
    OP->>OP: next reconcile: hash recorded
    OP->>API: create Pods with new template
    API->>H: start ray start
    H->>R: GCS available
    API->>W: start ray start --address=head
    W->>R: raylet register
    OP->>API: status ready / provisioned
```

head 维护则是另一条路径：先给 CR 加 `ray.io/disable-provisioned-head-restart: "true"`，删除 head Pod 后控制器**不会**创建替代者；维护完成移除注解，下一轮调和才补回 head。证据链：CR 注解 → head Pod 不存在 → 事件/status → 移除注解后新 head UID。

变更后的完整检查表见[第 55 章](55-change-management-upgrades.md)，现场取证见[第 56 章](56-live-diagnostics-state-api.md)。

更细的排查命令见[附录 C](appendix-c-troubleshooting.md)与[附录 F](appendix-f-lab-cheatsheet.md)。

← [附录 I. 自测题与答案](appendix-i-self-assessment.md) ｜ [返回目录](README.md) ｜ [附录 K. 可运行实验索引](appendix-k-lab-index.md)
