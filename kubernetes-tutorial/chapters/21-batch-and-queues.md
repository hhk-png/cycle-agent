# 第 21 章 批处理与队列：从 Job 到 AI 训练

## 21.1 原生 Job 的边界

Job 能解决：

- 运行到完成。
- 固定并发/完成数。
- 失败重试。
- 定时任务。

它不解决：

- 队列与公平共享。
- 多租户配额。
- gang scheduling（一组 Pod 要么都调度，要么都不调度）。
- 拓扑感知（同机架/NVLink）。
- 队列级公平/份额/跨队列抢占（原生 PriorityClass + 抢占只解决 Pod 级资源竞争，没有队列账本）。
- 作业排队、暂停、恢复、配额借用。
- 大规模作业的调度吞吐。

这些需要批处理调度器/队列系统。

## 21.2 队列系统要解决什么

一个典型的 AI/大数据平台：

```text
团队 A：训练任务 16 张 GPU，必须同时启动
团队 B：推理服务，延迟敏感
团队 C：离线 ETL，可以等
```

需求：

- **配额**：每个团队有 GPU/CPU 配额。
- **公平**：空闲时借用，忙时归还。
- **优先级**：在线 > 离线，高优任务可抢占。
- **gang scheduling**：分布式训练的所有 worker 同时调度。
- **拓扑**：尽量同机架/同交换机。
- **排队**：提交后按策略排队，而不是全部 Pending。
- **可观测**：队列长度、等待时间、利用率。

## 21.3 Kueue

Kueue 是 Kubernetes 原生的作业排队系统：

- 定义 ClusterQueue（集群级资源池）和 LocalQueue（命名空间队列）。
- Workload 代表一个待调度作业。
- 资源不足时，作业排队，不创建 Pod（避免大量 Pending）。
- 支持配额借用/借还、优先级、抢占。
- 与 Job、JobSet、Kubeflow、Ray 等集成。

示例（Kueue 已从 `v1beta1` 迁到 `v1beta2`；字段以安装版本的 CRD 为准）：

```yaml
apiVersion: kueue.x-k8s.io/v1beta2
kind: ClusterQueue
metadata:
  name: gpu-queue
spec:
  namespaceSelector: {}
  resourceGroups:
    - coveredResources: ["cpu", "memory", "nvidia.com/gpu"]
      flavors:
        - name: default
          resources:
            - name: cpu
              nominalQuota: 100
            - name: memory
              nominalQuota: 500Gi
            - name: nvidia.com/gpu
              nominalQuota: 8
```

```yaml
apiVersion: kueue.x-k8s.io/v1beta2
kind: LocalQueue
metadata:
  name: team-a
  namespace: team-a
spec:
  clusterQueue: gpu-queue
```

Job 通过 label 选择队列：

```yaml
metadata:
  labels:
    kueue.x-k8s.io/queue-name: team-a
```

Kueue 决定何时准入（admit）作业，准入后才创建 Pod。这样：

- 不会产生大量 Pending Pod。
- 配额在队列层管理。
- 支持抢占和借还。

## 21.4 Volcano

Volcano 是 CNCF 批处理调度器：

- gang scheduling。
- 队列/配额。
- 多种调度插件（bin-pack、spread、topology）。
- 支持 MPI、Horovod、Kubeflow、Ray。
- 有自己的 PodGroup CRD。

适合：

- HPC/AI 训练。
- 需要复杂调度策略。
- 已有 Volcano 生态。

## 21.5 YuniKorn

YuniKorn 是另一个批处理调度器：

- 层级队列。
- 公平调度。
- gang scheduling。
- 多租户。
- 支持 Spark、Flink、TensorFlow。

选型要看生态集成和团队经验。

## 21.6 gang scheduling

分布式训练需要所有 worker 同时运行：

```text
如果只调度 7/8 个 worker：
  - 已启动的 worker 等待第 8 个
  - 占用 GPU 但不工作
  - 其他作业也无法使用这些 GPU
```

gang scheduling：

- 作业的所有 Pod 要么一起调度，要么都不调度。
- 避免资源死锁。
- 需要调度器理解 PodGroup/Job 的整体资源需求。

Kubernetes 原生 `Workload`/`PodGroup` 与 gang 调度插件在 1.35 alpha、1.37 beta（feature-gated，默认关闭）。生产上仍常用 Volcano/Kueue+调度器集成：原生 gang 解决“全有或全无”，Kueue 解决队列/配额/借用，Volcano 提供更完整的批处理调度。不要用“原生已支持”替代容量与队列设计。

## 21.7 动态资源分配（DRA）

GPU、FPGA、NIC 等设备的管理：

- Device Plugin：传统方式，每个设备类型一个扩展资源。
- DRA（Dynamic Resource Allocation）：更灵活的设备声明/分配，支持属性、选择器、共享。

DRA 适合：

- 需要特定 GPU 型号/拓扑。
- 多设备组合。
- 动态配置。

DRA 核心在 1.34 GA（`resource.k8s.io/v1`），1.35 起默认启用；设备污点/容忍在 1.37 GA，可分区设备、可消费容量、组请求共享仍在 beta。生产使用要确认驱动、运行时、队列系统与目标版本的支持矩阵。对象模型与调度细节见第 22 章和[附录 R](../appendices/R-version-evolution-1.32-1.37.md) R.4.3。

## 21.8 AI 训练的特殊需求

- **GPU 拓扑**：NVLink、NVSwitch、同机架。
- **RDMA/InfiniBand**：多机训练网络。
- **本地 NVMe**：数据缓存/checkpoint。
- **高吞吐存储**：并行文件系统（Lustre、GPFS、JuiceFS）。
- **长时间运行**：checkpoint、故障恢复。
- **队列与配额**：GPU 昂贵，必须共享。
- **可观测**：GPU 利用率、显存、温度、ECC、网络。

Kubernetes 原生调度器不感知 GPU 拓扑；需要把不同层组合起来，而不是指望一个组件全包：

| 层 | 负责 | 典型组件 |
|---|---|---|
| 队列与准入 | 配额、借用、公平、作业排队 | Kueue、Volcano queue、YuniKorn |
| 调度与拓扑 | 选节点、gang、拓扑感知 | kube-scheduler + 插件、Volcano、Kueue TAS、DRA |
| 设备生命周期 | 驱动、device plugin、DCGM、MIG | NVIDIA GPU Operator、DRA driver |
| 可观测与成本 | 利用率、显存、XID、成本分摊 | DCGM Exporter、Prometheus、OpenCost |

GPU Operator **不是调度器**：它安装/管理驱动、device plugin、DCGM、MIG，让节点“暴露”GPU；真正决定 Pod 去哪台机器的是调度器/队列层。反过来，Kueue 也不负责发现设备，它假设设备已经通过 device plugin 或 DRA 暴露。选型时先画清这四层，再决定缺哪一层。

## 21.9 作业编排

大规模作业不只是一个 Job：

- 预处理 → 训练 → 评估 → 部署。
- 依赖关系（DAG）。
- 重试/回滚。
- 参数化。

工具：

- Argo Workflows：Kubernetes 原生 DAG。
- Kubeflow Pipelines：ML 流水线。
- Airflow：通用调度（可在 K8s 上跑）。
- Tekton：CI/CD 流水线。
- JobSet：管理一组相关 Job（Kueue 生态）。

## 21.10 成本与公平

GPU 集群的成本压力：

- 空闲 GPU 是最大浪费。
- 队列可以提高利用率，但增加等待时间。
- 抢占提高高优任务响应，但低优任务可能饿死。
- 配额借用需要防止“借了不还”。

策略：

- 分层队列（团队/项目）。
- 名义配额 + 借用上限。
- 优先级 + 抢占。
- 监控利用率/等待时间/抢占次数。
- 定期回顾配额。

## 21.11 MiniK8s 的边界

MiniK8s 实现：

- Job：completions/parallelism/backoff/TTL。
- CronJob：schedule/concurrency/history。
- PriorityClass + 抢占（调度器）。
- ResourceQuota（命名空间）。

不实现：

- 队列/公平共享。
- gang scheduling。
- GPU/DRA。
- 作业依赖/DAG。

第 22 章会展开 GPU、Device Plugin、DRA 与推理平台；第 42 章讨论 AI 工作负载与 Kueue 等未来方向。

## 21.12 Kueue 生产配置：从队列到 Workload

上一节给出了最小示例。生产配置通常还要回答：资源 flavor 怎么映射节点、配额如何借用/归还、抢占何时发生、作业为什么一直不 admitted。

### ResourceFlavor：把队列配额映射到真实节点

```yaml
apiVersion: kueue.x-k8s.io/v1beta2
kind: ResourceFlavor
metadata:
  name: gpu-a100
spec:
  nodeLabels:
    accelerator: nvidia-a100
  tolerations:
    - key: nvidia.com/gpu
      operator: Exists
      effect: NoSchedule
```

ResourceFlavor 用节点标签/污点描述“这类资源长什么样”。ClusterQueue 的 `resources[].flavors[].name` 引用它，Kueue 才能知道配额对应哪些节点。只写 `nominalQuota` 而不建 flavor，作业会停在队列里，事件通常提示找不到可用 flavor。

### ClusterQueue：配额、借用与抢占

```yaml
apiVersion: kueue.x-k8s.io/v1beta2
kind: ClusterQueue
metadata:
  name: team-pool
spec:
  cohort: shared-gpu
  resourceGroups:
    - coveredResources: ["cpu", "memory", "nvidia.com/gpu"]
      flavors:
        - name: gpu-a100
          resources:
            - name: cpu
              nominalQuota: 200
            - name: memory
              nominalQuota: 800Gi
            - name: nvidia.com/gpu
              nominalQuota: 16
              borrowingLimit: 8
  preemption:
    withinClusterQueue: LowerPriority
    reclaimWithinCohort: Any
```

几个容易混淆的字段：

- **nominalQuota**：该队列的名义配额，不是节点总量。
- **borrowingLimit**：最多能从 cohort 借多少。**语义与直觉相反**：`null`/不设置表示“没有上限”，可以借到 cohort 内其他队列的可用配额；`0` 才是禁止借用；`N` 是硬上限。没有 `cohort` 的队列不能借。
- **lendingLimit**：出借方最多允许别人借走多少；它限制的是别人能借的量，不是自己能借的量。
- **cohort**：共享借还的队列组；没有 cohort 的队列各自独立。
- **reclaimWithinCohort**：别人要收回借用配额时，是否允许抢占本队列的低优作业。
- **preemption.withinClusterQueue**：队列内部是否允许高优抢占低优。

因此“严格的配额隔离”要显式写 `borrowingLimit: 0`，而不是省略。省略看起来安全，实际会让队列在 cohort 内随意借用，配额保证和成本分摊都会失真。监控 `ClusterQueue.status.usage`、`flavorsReservation`、`admittedWorkloads` 和 `Inadmissible` 事件，才能看出借用是否发生。

配额是“账本”，不是 cgroup 限制。Kueue admitted 一个 Workload 后，Pod 仍要经过调度器；如果节点资源不足，Pod 仍可能 Pending。因此队列配额、节点容量、调度器策略必须一起规划。

### Workload 生命周期

```text
Job 创建
  → Kueue 生成 Workload（pending）
  → 队列检查配额/flavor/优先级
  → admitted（预留配额）
  → Job 控制器创建 Pod
  → Pod 调度/运行
  → Job 完成，Workload 释放配额
```

关键观测点：

- Workload 的 `status.conditions`：`QuotaReserved`、`Admitted`、`Finished`。
- 队列的 `status.pendingWorkloads`、`admittedWorkloads`、`usage`。
- 事件：`FailedToSchedule`、`Preempted`、`Inadmissible`。
- 指标：等待时间、队列长度、配额利用率、抢占次数。

### 常见“队列不工作”原因

1. **没有 ResourceFlavor**：ClusterQueue 引用的 flavor 不存在。
2. **配额被占满且不能借**：`borrowingLimit` 为 0 或 cohort 配置错误。
3. **PriorityClass 缺失**：作业引用了不存在的优先级。
4. **LocalQueue 与命名空间不匹配**：Job 的 label 指向了别的命名空间队列。
5. **Pod 模板请求超过 flavor 可提供量**：即使队列有空闲，单个 Pod 也放不下。
6. **调度器不认节点**：队列 admitted，但节点标签/污点与 flavor 不一致。

排障顺序是“Workload → ClusterQueue → ResourceFlavor → 节点 → 调度器”，不要一上来就改调度器参数。

## 21.13 调度演练：从 Pending 到 Admitted

在没有 Kueue 的环境中，可以用 MiniK8s 观察“资源账本”的简化版；在真实集群中，按下面的顺序做一次演练：

1. 创建两个队列 A/B，A 的名义配额为 4 GPU，B 为 4 GPU，加入同一 cohort。
2. 向 A 提交一个需要 4 GPU 的 Job，确认 admitted 且 Pod 运行。
3. 再向 A 提交第二个 4 GPU Job，确认它排队而不是创建 Pending Pod。
4. 向 B 提交 4 GPU Job，观察 B 是否借用、A 的低优作业是否被抢占（取决于配置）。
5. 删除 A 的第一个 Job，观察配额释放、排队作业 admitted。
6. 记录：等待时间、抢占次数、节点 GPU 利用率、业务完成时间。

演练的结论通常不是“队列生效了”，而是“在当前配额/优先级/抢占策略下，哪类作业会等待、等待多久、牺牲了谁”。这才是容量治理要回答的问题。

## 21.14 Spark 与 Flink on Kubernetes：有状态批流的运行细节

Job 是“一个 Pod 模板跑 N 次”，而 Spark 和 Flink 是“一个作业包含一组有角色的进程”。把它们跑在 Kubernetes 上，难点不在提交 YAML，而在 driver/executor 生命周期、shuffle/checkpoint 存储、故障恢复和资源回收。

### 21.14.1 两种运行模型

**Spark on Kubernetes**

- Spark 3.x 原生支持 Kubernetes 作为 cluster manager。
- Driver 是一个 Pod；Executor 是按需创建的 Pod。
- `spark-submit` 可以直接向 API Server 提交，也可以通过 Spark Operator 用 CRD 管理。
- 动态分配（dynamic allocation）让 executor 随负载增减。
- Shuffle 数据默认写本地盘；executor 被删后 shuffle 丢失，需要重算。
- 常用对象：`SparkApplication`（Operator）、driver/executor Pod、Service（driver UI/通信）、PVC（可选）。

**Flink on Kubernetes**

- Flink 有两类进程：JobManager（协调/checkpoint）和 TaskManager（执行）。
- 原生支持 Kubernetes HA（基于 ConfigMap/Lease）与嵌入式模式。
- Flink Kubernetes Operator 用 `FlinkDeployment`/`FlinkSessionJob` 管理生命周期、savepoint、升级。
- Checkpoint/Savepoint 必须放持久化存储（S3/OSS/HDFS/PVC），不能只放本地。
- 作业恢复依赖 checkpoint 与算子状态后端（RocksDB 等）。

### 21.14.2 一个 SparkApplication 的关键字段

> 下面的 Spark/Flink 镜像是**兼容性示例 pin**，不是 2026 年的最新版本（Spark 已进入 4.x、Flink 进入 2.x）。真实升级要同时核对 operator、Kubernetes 版本、Shuffle Service、状态后端和 SQL 兼容性，不要只改镜像 tag。

```yaml
apiVersion: sparkoperator.k8s.io/v1beta2
kind: SparkApplication
metadata:
  name: wordcount
  namespace: batch
spec:
  type: Scala
  mode: cluster
  image: example/spark:3.5.1
  mainClass: org.apache.spark.examples.SparkPi
  mainApplicationFile: local:///opt/spark/examples/jars/spark-examples.jar
  sparkVersion: "3.5.1"
  restartPolicy:
    type: OnFailure
    onFailureRetries: 3
    onFailureRetryInterval: 10
  driver:
    cores: 1
    coreLimit: "1200m"
    memory: "1g"
    serviceAccount: spark
  executor:
    cores: 2
    instances: 4
    memory: "4g"
    deleteOnTermination: true
    volumeMounts:
      - name: shuffle
        mountPath: /tmp/spark
  volumes:
    - name: shuffle
      emptyDir: {sizeLimit: 20Gi}
```

要点：

- **driver 是单点**：driver 失败通常意味着作业失败；重试策略要明确。
- **executor 是 cattle**：可以随时增减/重建，但 shuffle 数据在本地盘，重建后要重算。
- **资源声明**：Spark 的 `cores`/`memory` 要映射到容器 requests/limits，否则调度器按错误账本放置。
- **ServiceAccount**：driver 需要创建/删除 executor Pod 的 RBAC。
- **动态分配**：需要 shuffle service 或外部 shuffle，否则删 executor 会丢数据；很多团队关闭动态分配换稳定性。
- **镜像**：Spark 版本、Hadoop 客户端、云存储连接器、Python 依赖都要进镜像，避免运行时下载。

### 21.14.3 Shuffle 与 Checkpoint 的存储选择

| 数据 | 位置 | 失败后的影响 |
|---|---|---|
| Spark shuffle | 本地盘/emptyDir | executor 丢失后重算上游 stage |
| Spark checkpoint（流） | 持久存储（S3/HDFS/PVC） | 从最近 checkpoint 恢复 |
| Flink checkpoint | 持久存储 | 从 checkpoint 恢复，可能重复处理 |
| Flink savepoint | 持久存储 | 计划内升级/迁移 |
| 日志 | 对象存储/日志系统 | 排障 |

不要用 `emptyDir` 存 checkpoint/savepoint。节点重建、Pod 驱逐都会丢。Spark 的 `emptyDir` 只适合可重算的 shuffle。

### 21.14.4 Flink 的关键配置

```yaml
apiVersion: flink.apache.org/v1beta1
kind: FlinkDeployment
metadata:
  name: orders
spec:
  image: example/flink:1.19
  flinkVersion: v1_19
  mode: kubernetes
  serviceAccount: flink
  jobManager:
    resource: {cpu: 1, memory: "2g"}
  taskManager:
    resource: {cpu: 2, memory: "4g"}
    replicas: 4
  job:
    jarURI: local:///opt/flink/usrlib/orders.jar
    parallelism: 8
    upgradeMode: savepoint
    state: running
  flinkConfiguration:
    state.backend: rocksdb
    state.checkpoints.dir: s3://flink-state/checkpoints
    state.savepoints.dir: s3://flink-state/savepoints
    high-availability.type: kubernetes
    high-availability.storageDir: s3://flink-state/ha
    kubernetes.jobmanager.replicas: "2"
```

要点：

- **parallelism** 与 TaskManager slot 数决定实际并发；slot 不足会导致作业无法启动。
- **upgradeMode: savepoint** 在升级前触发 savepoint；`last-state` 更快但依赖 HA 元数据。
- **HA**：JobManager 多副本 + Kubernetes HA，避免单点。
- **checkpoint 间隔与超时**：太频繁增加开销，太稀疏增加恢复重算。
- **watermark/idleness**：数据源空闲会影响窗口触发，需配置 idleness。
- **状态大小**：RocksDB 状态可能很大，TaskManager 本地盘与内存要规划。

### 21.14.5 与队列/调度的配合

Spark/Flink 的 driver/JobManager 通常是长期或半长期进程，executor/TaskManager 是弹性资源。常见组合：

- **Kueue**：对 SparkApplication/JobSet 做队列准入，避免一次性创建大量 executor 打爆集群。
- **Volcano/YuniKorn**：gang scheduling，让 driver + 一批 executor 同时获得资源。
- **PriorityClass**：生产流处理高于离线批；离线可被抢占。
- **节点池**：shuffle/checkpoint 密集的作业放在本地 NVMe 节点池。

只配 HPA 不够：Spark executor 的扩缩由 Spark 自己决定，HPA 无法理解 stage 边界。

### 21.14.6 可观测性与成本

- Spark：driver UI、Spark metrics（Prometheus sink）、executor 数量/失败率、shuffle spill、GC。
- Flink：JobManager/TaskManager 指标、checkpoint 时长/大小/失败率、背压（backpressure）、lag。
- 成本：按 executor/TaskManager 的 CPU/内存/GPU 时长计费；空闲副本是主要浪费。
- 日志：driver/JobManager 日志是排障入口，必须持久化；executor 日志可采样。
- 事件：Kubernetes Event 只保留短期，关键状态要写入作业状态/外部系统。

### 21.14.7 常见故障

1. **driver OOM**：collect 大结果、广播变量过大、并行度太低。
2. **executor 频繁被删**：节点压力驱逐、Spot 回收、资源 requests 过低。
3. **shuffle fetch 失败**：executor 丢失或网络不通；检查 shuffle 存储与服务。
4. **Flink checkpoint 超时**：状态太大、对象存储慢、背压严重。
5. **JobManager 重启后作业不恢复**：HA 配置/存储路径错误。
6. **提交后一直 Pending**：队列配额、gang 调度、镜像拉取、RBAC 不足。
7. **版本不兼容**：Spark/Flink 与 Kubernetes 客户端、Operator、云存储连接器版本错配。

### 21.14.8 MiniK8s 的边界

MiniK8s 的 Job/CronJob 能模拟完成数、并行度、失败重试和 TTL，但不模拟 driver/executor 角色、shuffle、checkpoint 或 Operator。可以用 Job 观察“控制器如何补齐副本”，但不要把它当作 Spark/Flink 的替代验证环境。真实作业应在 kind/k3s 或托管集群上跑小规模端到端。

## 本章小结

- 原生 Job 适合简单批处理，不解决队列、公平和 gang scheduling。
- Kueue/Volcano/YuniKorn 填补大规模批处理调度空白。
- GPU/AI 训练需要拓扑、RDMA、存储和故障恢复的整体设计。
- 队列系统提高利用率，但引入等待、抢占和公平性治理。
- Spark/Flink 的难点在 driver/executor 生命周期、shuffle/checkpoint 存储和作业恢复，不是提交 YAML。
- 有状态批流的 checkpoint/savepoint 必须放持久存储；shuffle 可以重算，状态不能丢。

## 延伸阅读

- [第 10 章 DaemonSet、Job 与 CronJob](10-daemonset-job-cronjob.md)
- [第 17 章 调度器](17-scheduler.md)
- [第 22 章 GPU、加速器与 AI 推理平台](22-gpu-ai-inference.md)
- [第 42 章 云原生未来方向](42-future.md)
- [第 50 章 事件驱动、工作流与数据管道](50-event-driven-workflows.md)：Kafka、KEDA、Knative Eventing、Argo Workflows 与 CDC。
