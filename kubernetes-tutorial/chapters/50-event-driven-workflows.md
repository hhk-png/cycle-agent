# 第 50 章 事件驱动、工作流与数据管道

> 无状态服务是 Kubernetes 最舒服的场景：请求进来、处理、返回。但真实系统还需要异步解耦、削峰填谷、事件回放、长流程编排和批流一体的数据管道。这些能力**不在 Kubernetes 核心里**，而是由消息系统、事件总线、工作流引擎和 Operator 组合出来。本章讨论这些组件在 Kubernetes 上的运行方式、交付语义和运维陷阱。Spark/Flink 的运行细节见第 21.14 节，数据系统 Operator 见第 9.12 节。

## 50.1 事件驱动不是“加一个队列”

同步调用的问题是耦合：

- 调用方必须知道被调方地址、超时、重试和失败语义。
- 被调方抖动会直接放大为调用方延迟和错误。
- 流量峰值必须由所有下游同时承接。

事件驱动把“现在做”变成“记录一件事，稍后处理”：

```text
同步：订单服务 ──HTTP──▶ 库存服务（必须现在成功）
异步：订单服务 ──事件──▶ Broker ──▶ 库存/积分/通知/分析（各自按节奏消费）
```

它带来的能力：

| 能力 | 说明 |
|---|---|
| 解耦 | 生产者和消费者独立部署、扩缩、升级 |
| 缓冲 | 峰值写入 Broker，消费者按能力处理 |
| 扇出 | 一个事件多个消费者，各自维护 offset |
| 回放 | 保留期内可重放历史事件，重建读模型 |
| 削峰 | 突发流量不直接打垮下游 |
| 审计 | 事件流本身就是业务事实记录 |

代价也很直接：

- **最终一致**：用户下单后积分不会立刻到账，界面要处理中间态。
- **重复投递**：至少一次语义意味着消费者必须幂等。
- **顺序问题**：只有分区内有序，跨分区没有全局顺序。
- **运维复杂度**：Broker 是有状态系统，要管磁盘、复制、扩缩、升级、备份。
- **可观测性更难**：一次请求跨多个异步跳，trace 和排障链路更长。

所以先问：**这个场景真的需要异步吗？** 如果调用方必须立刻拿到结果、失败必须立刻回滚，同步 RPC 更简单。事件驱动适合“可以晚一点、可以重复、可以补偿”的业务。

## 50.2 消息系统选型

| 系统 | 模型 | 顺序 | 保留 | 典型场景 | 运维复杂度 |
|---|---|---|---|---|---|
| Kafka | 分区日志 | 分区内有序 | 按时间/大小，可回放 | 事件流、CDC、日志、流处理 | 高 |
| Pulsar | 分层存储 + 订阅 | 分区内有序 | 长期保留、多订阅 | 多租户、队列+流一体 | 很高 |
| NATS JetStream | 轻量消息 + 持久化 | 流/消费者语义 | 可配置 | 云原生、低延迟、边缘 | 中 |
| RabbitMQ | 队列/交换机 | 队列内近似有序 | 消费后删除 | 任务队列、路由、RPC | 中 |
| Redis Streams | 内存流 | 流内有序 | 内存/有限持久化 | 轻量场景、缓存附近 | 低 |
| 云队列（SQS/PubSub/Event Hubs） | 托管 | 有限 | 由服务定义 | 不想运维 Broker | 低（但锁定） |

选型时不要只看吞吐，先回答：

1. **需要回放吗？** 需要历史重放、读模型重建、CDC，优先日志型（Kafka/Pulsar）。
2. **需要多订阅者独立消费吗？** 日志型天然支持 consumer group；队列型要复制/路由。
3. **顺序要求是什么？** 全局有序几乎不可扩展；按 key 分区有序通常够用。
4. **保留多久？** 保留越久，存储和成本越高；分层存储可以缓解。
5. **团队能运维吗？** 自建 Kafka 的故障模式（磁盘满、ISR 收缩、rebalance）需要经验。
6. **是否被云锁定？** 托管队列省事，但迁移、协议、语义差异要提前评估。

常见反模式：

- 用 Kafka 当任务队列：分区并行度受限于分区数，单条任务重试会阻塞分区。
- 用 RabbitMQ 当事件日志：消费后删除，无法回放。
- 用 Redis 当唯一持久化：内存成本高，故障恢复和保留策略有限。
- 一个 topic 承载所有事件类型：schema 混乱、消费者过滤成本高。

## 50.3 Kafka on Kubernetes

### 50.3.1 为什么 Kafka 在 Kubernetes 上更难

Kafka 是有状态、强依赖磁盘和网络的系统：

- 每个 broker 有持久化日志，不能像无状态 Pod 一样随意漂移。
- 复制依赖稳定的网络和成员关系；频繁重启会触发 ISR 收缩和 rebalance。
- 磁盘 IOPS、吞吐、容量决定性能；本地盘通常优于网络盘。
- 分区数决定并行度，也决定 rebalance 的影响面。
- 升级、扩缩、分区迁移都需要受控流程。

在 Kubernetes 上跑 Kafka，核心不是“写一个 StatefulSet”，而是用 Operator 管理 broker 身份、存储、配置、滚动升级和分区再平衡。

### 50.3.2 Strimzi 示例

```yaml
apiVersion: kafka.strimzi.io/v1beta2
kind: Kafka
metadata:
  name: events
  namespace: messaging
spec:
  kafka:
    version: 4.0.0
    replicas: 3
    listeners:
      - name: internal
        port: 9092
        type: internal
        tls: true
    config:
      offsets.topic.replication.factor: 3
      transaction.state.log.replication.factor: 3
      transaction.state.log.min.isr: 2
      default.replication.factor: 3
      min.insync.replicas: 2
      unclean.leader.election.enable: false
    storage:
      type: jbod
      volumes:
        - id: 0
          type: persistent-claim
          size: 1Ti
          class: fast-ssd
          deleteClaim: false
    resources:
      requests:
        cpu: "4"
        memory: 16Gi
      limits:
        cpu: "8"
        memory: 32Gi
    template:
      pod:
        topologySpreadConstraints:
          - maxSkew: 1
            topologyKey: topology.kubernetes.io/zone
            whenUnsatisfiable: DoNotSchedule
            labelSelector:
              matchLabels:
                strimzi.io/cluster: events
  entityOperator:
    topicOperator: {}
    userOperator: {}
```

要点：

- **副本与 ISR**：`replication.factor: 3`、`min.insync.replicas: 2`、生产者 `acks=all` 才能在单 broker 故障时不丢已确认消息。代价是至少两个副本确认。
- **禁止 unclean leader election**：允许非同步副本当 leader 会丢数据；可用性换一致性要显式决策。
- **存储**：`deleteClaim: false` 防止误删 PVC；本地盘性能好但节点故障后数据不可迁移，要靠副本恢复。
- **反亲和/拓扑分布**：broker 不要全落在同一节点/可用区；否则一次故障带走多数副本。
- **资源**：Kafka 大量使用 page cache，内存不要只按 JVM heap 算；磁盘和网络往往是瓶颈。
- **版本**：Kafka 4.x 已移除 ZooKeeper，使用 KRaft；升级前核对 Strimzi 与 Kafka 的兼容矩阵。

### 50.3.3 Topic 与分区设计

```yaml
apiVersion: kafka.strimzi.io/v1beta2
kind: KafkaTopic
metadata:
  name: orders-created
  namespace: messaging
  labels:
    strimzi.io/cluster: events
spec:
  partitions: 48
  replicas: 3
  config:
    retention.ms: "604800000"
    cleanup.policy: delete
    min.insync.replicas: "2"
```

分区设计要考虑：

- **并行度上限**：一个 consumer group 内，活跃消费者数不超过分区数；多出来的消费者空闲。
- **顺序键**：用业务主键（如 `order_id`）做 partition key，保证同一订单的事件有序。
- **热点分区**：key 分布不均会导致单分区过热；要监控分区字节速率和 lag。
- **扩分区**：可以增加，但不能减少；增加会改变 key 到分区的映射，破坏“同一 key 永远同一分区”的假设（如果消费者依赖这一点）。
- **保留**：按业务回放需求设置 `retention.ms`/`retention.bytes`；日志型 topic 不要无限保留。

### 50.3.4 生产与消费配置

生产者：

- `acks=all` + `enable.idempotence=true`：避免重试导致重复写入。
- 合理设置 `linger.ms` 和 `batch.size`：吞吐与延迟的权衡。
- 设置 `delivery.timeout.ms`、`request.timeout.ms`、重试次数；不要无限重试。
- 需要跨分区原子写时使用事务，但事务有开销，且只保证 Kafka 内部。

消费者：

- `enable.auto.commit=false`，处理成功后再提交 offset。
- 控制 `max.poll.records` 和 `max.poll.interval.ms`：处理太慢会被踢出 group，触发 rebalance。
- 消费者要幂等；重复投递是常态。
- 优雅终止时停止拉取、处理完当前批次、提交 offset、关闭。

### 50.3.5 扩缩、升级与再平衡

- **扩 broker**：新 broker 不会自动分担分区；需要分区重分配（Cruise Control 或 `kafka-reassign-partitions`）。
- **缩 broker**：先迁移分区，再下线；不要直接删 Pod。
- **滚动升级**：一次一个 broker，等待 ISR 恢复；设置 `maxUnavailable` 和 PDB。
- **rebalance 风暴**：频繁重启、会话超时、处理过慢都会触发；用静态成员（static membership）和合理的 `session.timeout.ms` 降低影响。
- **配额**：用 `consumer_byte_rate`、`producer_byte_rate`、`request_percentage` 防止单租户打满集群。

### 50.3.6 多租户

- 用 namespace + topic 命名规范 + ACL 隔离租户。
- 每租户配额（生产/消费字节速率、请求比例）。
- 大租户独立集群，小租户共享；不要用“一个 topic 所有人用”省事。
- Schema Registry 也要多租户隔离和兼容性策略。
- 监控按租户维度的 lag、流量、磁盘。

## 50.4 KEDA：事件驱动伸缩

HPA 按 CPU/内存扩缩，但队列消费者最关心的指标是**积压**。KEDA 把外部事件源指标接入 HPA，并支持缩到零。

```yaml
apiVersion: keda.sh/v1alpha1
kind: ScaledObject
metadata:
  name: order-consumer
  namespace: orders
spec:
  scaleTargetRef:
    name: order-consumer
  minReplicaCount: 0
  maxReplicaCount: 60
  pollingInterval: 15
  cooldownPeriod: 300
  triggers:
    - type: kafka
      metadata:
        bootstrapServers: events-kafka-bootstrap.messaging.svc:9093
        consumerGroup: order-consumer
        topic: orders-created
        lagThreshold: "1000"
        activationLagThreshold: "10"
```

要点：

- `lagThreshold` 是“每个副本目标积压”，不是总积压；总积压 60,000、阈值 1,000 时目标副本约 60（受 max 限制）。
- `minReplicaCount: 0` 可以省钱，但冷启动会带来延迟；关键消费者保留最小副本。
- `pollingInterval` 太短会增加 Broker/监控压力，太长会扩缩不及时。
- `cooldownPeriod` 防止抖动；缩容太快会引发 rebalance。
- `activationLagThreshold` 决定从 0 到 1 的触发点，避免空队列频繁唤醒。
- 认证用 `TriggerAuthentication`/`ClusterTriggerAuthentication` 从 Secret 注入 TLS/SASL 参数，不要把密码写进 ScaledObject。
- 如果 Broker 启用了 TLS，ScaledObject 的 scaler 也需要显式配置 `tls: enable` 与 CA；否则 KEDA 连不上，表现为“指标不可用而不是不扩容”。

KEDA 的坑：

- **分区数上限**：Kafka 消费者副本超过分区数没有意义；扩到 100 个副本但只有 12 个分区，88 个空闲。
- **rebalance 成本**：频繁扩缩会让 consumer group 不断 rebalance，处理延迟上升。
- **指标延迟**：lag 指标有采集延迟，突发流量下扩缩永远慢半拍；要有缓冲和降级。
- **缩到零的状态**：有状态消费者缩到零可能丢失本地状态；确认 offset/checkpoint 已持久化。
- **ScaledJob**：一次性任务用 `ScaledJob`，不要用 Deployment 模拟 Job。

## 50.5 Knative Eventing

Knative Eventing 提供事件路由层：Broker 接收事件，Trigger 按属性过滤并投递到订阅者。它不存储长期日志，定位是**事件分发**而不是 Kafka 替代。

```yaml
apiVersion: eventing.knative.dev/v1
kind: Broker
metadata:
  name: orders
  namespace: orders
spec:
  delivery:
    retry: 5
    backoffPolicy: exponential
    backoffDelay: PT2S
    deadLetterSink:
      ref:
        apiVersion: v1
        kind: Service
        name: orders-dlq
---
apiVersion: eventing.knative.dev/v1
kind: Trigger
metadata:
  name: order-created
  namespace: orders
spec:
  broker: orders
  filter:
    attributes:
      type: com.example.order.created
      source: com.example.checkout
  subscriber:
    ref:
      apiVersion: v1
      kind: Service
      name: order-consumer
  delivery:
    retry: 5
    deadLetterSink:
      ref:
        apiVersion: v1
        kind: Service
        name: orders-dlq
```

关键概念：

- **CloudEvents**：事件信封标准，包含 `id`、`source`、`type`、`time`、`datacontenttype` 等属性；过滤基于属性。
- **Broker/Trigger**：Broker 是事件入口，Trigger 是“过滤 + 订阅”。
- **Delivery spec**：重试、退避、死信；没有 DLQ 的异步系统迟早会丢事件。
- **Sources**：Cron、Kafka、GitHub、S3 等事件源适配器。
- **Channel/Subscription**：更底层的消息通道抽象；新版本推荐 Broker/Trigger。

与 Kafka 的分工：

| 需求 | Knative Eventing | Kafka |
|---|---|---|
| 事件路由/过滤 | 强 | 需消费者自己实现 |
| 长期保留/回放 | 弱 | 强 |
| 多租户事件总线 | 中 | 强（配配额/ACL） |
| 低延迟扇出 | 强 | 强 |
| 运维复杂度 | 中（依赖 Broker 实现） | 高 |

常见组合：Kafka 作为持久事件日志，Knative Eventing 作为集群内事件路由；或者直接用 Kafka + 消费者，减少一层。

## 50.6 工作流引擎：Argo、Tekton、Airflow、Temporal

### 50.6.1 Argo Workflows

Argo 把每个步骤跑成 Pod，适合批处理、ETL、ML 流水线、CI 任务。

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Workflow
metadata:
  generateName: etl-
  namespace: data
spec:
  entrypoint: pipeline
  serviceAccountName: argo-workflow
  ttlStrategy:
    secondsAfterCompletion: 86400
  podGC:
    strategy: OnPodCompletion
  templates:
    - name: pipeline
      dag:
        tasks:
          - name: extract
            template: run
            arguments:
              parameters:
                - name: step
                  value: extract
          - name: transform
            template: run
            depends: extract
            arguments:
              parameters:
                - name: step
                  value: transform
          - name: load
            template: run
            depends: transform
            arguments:
              parameters:
                - name: step
                  value: load
    - name: run
      inputs:
        parameters:
          - name: step
      container:
        image: example/etl:1.0
        command: ["/etl"]
        args: ["--step", "{{inputs.parameters.step}}"]
        resources:
          requests:
            cpu: "1"
            memory: 2Gi
          limits:
            cpu: "2"
            memory: 4Gi
```

要点：

- **DAG vs Steps**：DAG 显式依赖，Steps 顺序执行；复杂流水线用 DAG。
- **Artifacts**：步骤间传文件用 artifact repository（S3/OSS/MinIO），不要依赖 Pod 本地盘。
- **Pod GC/TTL**：工作流完成后清理 Pod 和对象，否则 etcd 和节点会被历史 Pod 撑爆。
- **重试**：`retryStrategy` 支持次数、退避、重试条件；重试的步骤必须幂等。
- **退出处理**：`exitHandlers` 做清理/通知；失败也要执行。
- **安全**：每个 Workflow 用独立 ServiceAccount；不要让所有工作流共用 cluster-admin。
- **资源**：并行任务多时用 Kueue/队列准入，避免一次创建几百个 Pod。

### 50.6.2 Tekton

Tekton 面向 CI/CD：`Task` 是可复用步骤集合，`Pipeline` 编排 Task，`PipelineRun` 是一次执行。

- **Workspace**：Task 间共享存储（PVC/ConfigMap/Secret/emptyDir）。
- **Results**：Task 输出小结果给后续 Task。
- **Triggers**：从 Git webhook 创建 PipelineRun。
- **Sidecar**：日志、docker-in-docker、缓存等常以 sidecar 形式存在。
- **安全**：构建镜像需要特权或 rootless 构建工具；不要把 Docker socket 挂进不可信流水线。

Tekton vs Argo：

- Tekton 更偏 CI/CD 原语，和 Git webhook/事件集成好。
- Argo 更偏通用 DAG/批处理，UI 和 artifact 生态成熟。
- 两者都能跑在 Kubernetes 上，但都不解决“构建缓存、制品签名、环境晋级”的完整问题。

### 50.6.3 Airflow

Airflow 是通用工作流调度器，不是 Kubernetes 原生控制器：

- `KubernetesExecutor`/`KubernetesPodOperator` 把任务跑成 Pod。
- Scheduler、 webserver、metadata DB、executor 都要运维；metadata DB 是新的有状态依赖。
- DAG 是 Python 代码，灵活但容易把业务逻辑塞进调度层。
- 适合数据团队已有的 Airflow 生态；不建议为了“跑在 K8s 上”而引入整套 Airflow。

### 50.6.4 Temporal

Temporal 提供持久化执行（durable execution）：

- Workflow 代码看起来是同步的，但状态由 Temporal 持久化，进程崩溃后可恢复。
- Activity 负责外部调用，支持重试、超时、心跳。
- 适合长流程、订单履约、审批、补偿；不是 DAG 调度器。
- 可以跑在 Kubernetes 上，但 Temporal 集群本身（Cassandra/PostgreSQL、history、matching）需要运维。

### 50.6.5 选型矩阵

| 需求 | 推荐 |
|---|---|
| 批处理 DAG、ML 流水线 | Argo Workflows |
| CI/CD 流水线、Git 触发 | Tekton |
| 数据团队已有 Airflow | Airflow + KubernetesExecutor |
| 长流程、强状态、补偿 | Temporal |
| 简单定时任务 | CronJob |
| 大规模队列/批处理 | Kueue + Job/JobSet |

## 50.7 数据管道与流处理

Kubernetes 上的数据管道通常由四层组成：

```text
采集（CDC/日志/埋点）
  → 传输（Kafka/Pulsar）
  → 处理（Flink/Spark/消费者）
  → 服务（数据库/湖仓/OLAP/特征库）
```

### 50.7.1 CDC

- Debezium 捕获数据库 binlog/WAL，把变更写入 Kafka。
- 优点：业务代码不双写；缺点：schema 演进、快照一致性、重复事件、数据库权限。
- 初始快照要限速，避免打满数据库；快照与增量切换要保证不丢不重。
- 删除事件、DDL 变更、大事务都要单独处理。

### 50.7.2 流处理

- Flink/Spark Structured Streaming 的 checkpoint 必须放持久存储（S3/HDFS/PVC），不能放 `emptyDir`。
- 状态大小决定本地盘和内存；RocksDB 状态可能远大于堆内存。
- 背压是常态：下游慢时，上游要降速而不是无限堆积。
- exactly-once 需要 source 可重放、sink 支持事务或幂等；端到端仍可能有重复。
-  Kubernetes 上的 Pod 重启、节点回收会触发 checkpoint 恢复；恢复时间要计入 SLO。

### 50.7.3 湖仓与 OLAP

- Iceberg/Hudi/Delta 提供表格式和快照；需要 catalog（Hive/Glue/Nessie）和清理策略。
- ClickHouse/Doris/StarRocks 等 OLAP 通常用 Operator 管理分片/副本；写入批次和 part 合并要调优。
- 对象存储是数据湖的底座；注意小文件、请求成本、跨区流量。
- 数据管道的 SLO 不是“Pod Running”，而是“数据按时、完整、可查询”。

## 50.8 交付语义与一致性

### 50.8.1 三种交付语义

| 语义 | 含义 | 实现代价 |
|---|---|---|
| At-most-once | 最多一次，可能丢 | 不重试，简单 |
| At-least-once | 至少一次，可能重复 | 重试 + 幂等消费 |
| Exactly-once | 恰好一次 | 事务/幂等 sink/去重，端到端很难 |

大多数系统实际是 **at-least-once + 幂等**。不要轻信“我们 exactly-once”，要问：从生产到消费、从消费到数据库，整条链路是否都满足？

### 50.8.2 幂等与去重

- 事件带唯一 `event_id`，消费者用去重表/Redis/数据库唯一约束。
- 去重窗口要覆盖最大重试/回放时间；无限窗口成本高。
- 业务操作用幂等键（订单号、支付单号），不要用“消息 offset”做业务幂等。
- 数据库写入用 `INSERT ... ON CONFLICT`/唯一索引，避免“先查后写”的竞态。

### 50.8.3 Outbox 与 Saga

- **Outbox**：业务表和 outbox 表同事务写入；发布器扫描 outbox 投递，成功后标记。解决“写库成功、发消息失败”。
- **CDC**：直接捕获 outbox/业务表变更，减少应用双写。
- **Saga**：每个步骤有补偿动作；补偿必须幂等，且要考虑“补偿失败后怎么办”。
- **TCC**：Try-Confirm-Cancel，适合金融场景，但业务改造大。

### 50.8.4 顺序与重试

- 只保证分区内有序；跨分区顺序需要业务自己处理（版本号、状态机）。
- 重试会阻塞分区；对毒消息要有限次重试后进 DLQ，不要无限阻塞。
- DLQ 要有消费者、告警、重放工具；只创建 DLQ 不处理等于丢数据。
- 重放历史事件时，消费者要能处理旧 schema 和重复副作用。

## 50.9 多租户、配额与成本

事件平台是多租户共享资源，必须治理：

- **Topic/Namespace 隔离**：命名规范、ACL、配额；生产环境禁止租户创建无限 topic。
- **配额**：生产/消费字节速率、请求比例、分区数、保留容量。
- **保留策略**：按租户设置 retention；长期保留用分层存储，不要全放本地盘。
- **网络成本**：跨可用区复制、跨区消费、出网流量都要计入。
- **计算成本**：消费者空闲副本、过度分区、过度副本都是浪费。
- **批处理**：用 Kueue 队列和优先级，避免离线作业挤占在线消费者。

一个粗略的 Kafka 成本模型：

```text
存储成本 ≈ 日写入字节 × 保留天数 × 副本数 × 单价
网络成本 ≈ 跨 AZ 复制流量 × 单价 + 跨区消费流量 × 单价
计算成本 ≈ broker 数量 × 单 broker 成本 + 消费者副本成本
```

优化顺序通常是：先删无用 topic/缩短保留，再降副本/分区，最后才考虑换存储介质。

## 50.10 可观测性与 SLO

### 50.10.1 关键指标

**Broker**

- Under-replicated partitions、offline partitions、ISR 收缩。
- 磁盘使用率、磁盘 IO 延迟、网络吞吐。
- 请求队列、leader 选举、controller 状态。

**消费者**

- Consumer lag（按 group/topic/partition）。
- 处理延迟、错误率、重试次数、DLQ 深度。
- rebalance 次数/时长、`max.poll.interval` 超时。
- 消费者副本数与分区数的比值。

**端到端**

- 事件从产生到可查询的延迟。
- 数据完整性（条数、校验和、对账）。
- 失败事件的补偿完成率。

```promql
# 每个消费组的 lag（示例指标名，按 exporter 调整）
sum by (consumergroup) (kafka_consumergroup_lag{namespace="orders"})

# 消费速率低于生产速率时，lag 会持续增长
sum(rate(kafka_topic_partition_current_offset[5m]))
  - sum(rate(kafka_consumergroup_current_offset[5m]))
```

### 50.10.2 告警

- lag 持续超过阈值（结合消费速率，不只看绝对值）。
- under-replicated partitions > 0 持续数分钟。
- 磁盘使用率 > 80%，或预计 24 小时内写满。
- DLQ 深度增长。
- 消费者错误率/重试率突增。
- 端到端延迟超过 SLO。

### 50.10.3 追踪

异步链路的 trace 容易断：生产者在发送时注入 `traceparent` 到消息头，消费者提取并创建 span link。没有 trace 时，至少用 `event_id` 把生产日志、消费日志和数据库记录串起来。

## 50.11 测试、发布与灾难恢复

### 50.11.1 测试分层

| 层 | 工具 | 验证 |
|---|---|---|
| 单元 | mock/内存队列 | 处理逻辑、幂等、重试 |
| 契约 | Schema Registry/JSON Schema | 事件格式、兼容性 |
| 集成 | Testcontainers（Kafka/RabbitMQ） | 生产/消费/offset/DLQ |
| 集群 | kind + Strimzi/KEDA/Knative | Operator、扩缩、网络 |
| 故障 | 杀 broker、断网、磁盘满、重复投递 | 恢复、告警、数据完整性 |
| 回放 | 历史事件重放 | 读模型重建、幂等 |

### 50.11.2 发布与迁移

- Broker 升级：先升级客户端兼容性，再滚动升级 broker；一次一个，观察 ISR。
- Schema 演进：只用向后兼容的变更（新增可选字段、不删不改类型）；用 Schema Registry 强制。
- Topic 迁移：双写/镜像 + 消费者切换 + 校验 + 下线旧 topic。
- 客户端升级：协议版本、认证方式、序列化格式都可能不兼容。
- 回滚：保留旧 topic/旧 consumer group；不要删除历史数据直到确认。

### 50.11.3 灾难恢复

- 明确 RPO/RTO：Kafka 副本能扛单节点/单 AZ，但扛不了误删 topic 或区域故障。
- 跨集群复制（MirrorMaker 2/Cluster Linking）用于灾备和多活；注意 offset 不共享，消费者要重置。
- 备份 topic 配置、ACL、Schema；消息本身是否备份取决于保留和成本。
- 定期演练：删 topic 恢复、broker 全挂、消费者从零重建读模型。
- 数据库/湖仓的备份与消息保留要一起设计；只备份 Kafka 不备份下游，恢复不了业务。

## 50.12 参考架构与反模式

### 50.12.1 订单事件流

```text
checkout
  → 本地事务写 orders + outbox
  → outbox publisher → Kafka topic orders-created（key=order_id）
  → 库存/积分/通知消费者（各自 group，幂等）
  → DLQ + 重放工具
  → 指标：lag、错误率、端到端延迟、对账差异
```

### 50.12.2 CDC 到湖仓

```text
PostgreSQL
  → Debezium（Kafka Connect）
  → Kafka topic（按表/主键分区）
  → Flink/Spark 清洗
  → Iceberg/Hudi 表
  → Trino/ClickHouse 查询
```

关键：schema 演进、快照一致性、重复事件、湖仓小文件、数据质量校验。

### 50.12.3 反模式

- **没有 DLQ**：毒消息阻塞分区，整个消费组停滞。
- **无限重试**：一条坏消息拖垮所有后续事件。
- **消费者无幂等**：重复投递导致重复扣款/重复发货。
- **用 Pod 本地盘存 checkpoint**：Pod 重建后状态丢失。
- **消费者副本数超过分区数**：大量空闲副本，扩缩容无效。
- **Kafka 当数据库**：没有查询能力、保留有限、运维复杂。
- **忽略 schema 兼容**：上游加字段导致下游反序列化失败。
- **只在测试环境验证**：真实 broker 的磁盘、网络、rebalance 行为差异很大。

## 50.13 MiniK8s 对照

MiniK8s 不实现消息 Broker、KEDA、Knative Eventing 或工作流引擎，但可以用它验证周边契约：

- **Job/CronJob**：批处理任务的重试、完成数、TTL、并发策略（第 10 章）。
- **HPA**：基于 CPU/内存的副本调整；KEDA 的外部指标不在 MiniK8s 中（第 20 章）。
- **CRD/Operator**：可以定义一个 `Queue`/`Pipeline` CRD，用控制器创建 Job/Deployment；参考 `examples/website_operator.py` 的模式（第 19 章）。
- **Service/DNS**：模拟消费者访问 Broker 的 Service 名称解析。
- **NetworkPolicy**：限制哪些 namespace 可以访问消息端口。
- **PDB/优先级/抢占**：保护在线消费者，驱逐离线批处理。

一个练习：在 MiniK8s 上写一个极简 `Topic` CRD 和控制器，为每个 Topic 创建一个 ConfigMap 记录分区数，并创建一个消费者 Deployment；用 HPA 模拟按 CPU 扩缩。它能验证“CRD → 子对象 → 状态”的控制器闭环，但不能验证真实 Kafka 的复制、offset 和 rebalance。

真实事件平台必须在 kind/k3d 或托管集群上验证：Strimzi/KEDA/Knative 的 CRD、Broker 滚动升级、消费者 rebalance、DLQ 重放和灾难恢复。

## 本章小结

- 事件驱动解决解耦、缓冲、扇出和回放，代价是最终一致、重复投递和更高的运维复杂度。
- 选型先看是否需要回放、多订阅、顺序和保留，再看吞吐；不要用错消息模型。
- Kafka 在 Kubernetes 上的关键是 Operator、副本/ISR、磁盘、分区设计和受控扩缩升级。
- KEDA 用外部指标扩缩并支持缩到零，但受分区数、指标延迟和 rebalance 成本约束。
- Knative Eventing 提供事件路由/过滤，不替代长期事件日志；与 Kafka 可以组合。
- Argo/Tekton/Airflow/Temporal 解决不同层次的问题：DAG、CI/CD、调度、持久化执行。
- 端到端 exactly-once 很难；工程上应默认 at-least-once + 幂等 + 去重 + DLQ。
- 事件平台的 SLO 要围绕 lag、端到端延迟、数据完整性和 DLQ，而不是 Pod 数量。
- 测试要覆盖重复、乱序、毒消息、broker 故障、回放和灾难恢复；MiniK8s 只能验证控制器契约。

## 延伸阅读

- [第 9 章 StatefulSet](09-statefulset.md)：有状态工作负载、数据系统 Operator 与存储。
- [第 10 章 DaemonSet、Job 与 CronJob](10-daemonset-job-cronjob.md)：批处理原语、重试、并发策略。
- [第 20 章 自动伸缩](20-autoscaling.md)：HPA/VPA/KEDA 与指标源。
- [第 21 章 批处理与队列](21-batch-and-queues.md)：Kueue、gang scheduling、Spark/Flink。
- [第 41 章 通用计算平台](41-general-compute-platform.md)：Knative Serving/Eventing、Serverless 与边缘。
- [第 49 章 应用工程](49-application-engineering.md)：幂等、超时、重试、优雅终止与可观测性契约。
- [附录 M 生产事故案例库](../appendices/M-incident-casebook.md)：队列积压、Broker 故障与发布事故。
- [附录 N 成本工程](../appendices/N-cost-engineering.md)：存储/网络/计算成本分摊与优化。
