# 第2章 运行时架构与部署模式

## 2.1 一张运行时地图

Flink 集群有四个核心角色：

```text
Client ──提交 JobGraph──> Dispatcher ──> JobManager (JobMaster)
                                              │
                    ┌─────────────────────────┼─────────────────────────┐
                    ▼                         ▼                         ▼
              ResourceManager          CheckpointCoordinator      Scheduler
                    │
                    ▼
             TaskManager A              TaskManager B              TaskManager C
             [slot][slot]               [slot][slot]               [slot][slot]
             [subtask][subtask]         [subtask][subtask]         [subtask][subtask]
```

- **Client**：把用户代码构建成 JobGraph 并提交。它不参与运行，提交后可以退出；`execute()` 之后客户端进程退出是正常行为。
- **Dispatcher**：提供 REST/Web UI 入口，管理作业提交和作业列表。会话模式下多个作业共享一个 Dispatcher。
- **JobManager / JobMaster**：单个作业的大脑。它持有 ExecutionGraph，向 ResourceManager 申请 slot，部署和监控 task，协调 checkpoint，触发故障恢复，对外提供指标和 REST。
- **ResourceManager**：管理 TaskManager 和 slot。Standalone 下它管理固定资源；YARN/Kubernetes 下它与外部资源系统交互，按需申请/释放容器。
- **TaskManager**：工作节点，提供若干 slot，执行一个或多个 subtask，管理内存、网络缓冲、状态后端和本地磁盘。

一个 JobManager 可以只服务一个作业（application 模式），也可以服务多个作业（session 模式）。生产上更推荐 application 模式：依赖、配置和生命周期绑定在一个作业上，升级和隔离都更清晰。Flink 2.3 进一步引入了“集群 → 应用 → 作业”的应用级生命周期管理，Web UI 和 REST 都能看到应用层级。

### JobManager 内部做什么

JobMaster 不是一个线程，而是一组服务：

- **Scheduler**：决定哪个 subtask 放到哪个 slot，处理失败重调度；
- **CheckpointCoordinator**：周期性向 source 注入 barrier，收集各 task 的快照确认；
- **JobGraph 编译器**：把 JobGraph 展开成 ExecutionGraph，计算中间结果分区；
- **BlobServer/BlobCache**：分发 JAR、依赖和任务资源；
- **MetricRegistry / REST endpoint**：暴露指标和运维接口。

理解“JobManager 是协调者而不是数据通道”很重要：数据只在 TaskManager 之间流动，JobManager 挂了不会丢数据，但会丢失调度和 checkpoint 协调能力。

## 2.2 从代码到执行图

Flink 的图有四层，越往下越接近物理执行：

1. **StreamGraph**：用户 API 直接生成的逻辑拓扑，节点是算子，边是数据流向；
2. **JobGraph**：经过算子链合并、分区器确定、slot sharing group 划分后的提交图；
3. **ExecutionGraph**：按并行度展开成 subtask（ExecutionVertex）、中间结果分区（IntermediateResultPartition）和调度单元；
4. **物理执行图**：每个 subtask 在 TaskManager 上实际运行，通过 Netty 通道交换数据。

例如：

```java
env.fromSource(kafkaSource, watermarkStrategy, "orders")
   .map(new ParseOrder())
   .filter(order -> order.isValid())
   .keyBy(Order::getUserId)
   .window(TumblingEventTimeWindows.of(Duration.ofMinutes(5)))
   .aggregate(new SumAmount())
   .sinkTo(kafkaSink);
```

在 JobGraph 里可能被链成两个 JobVertex：

```text
[source -> parse -> filter]  --hash by userId-->  [window -> aggregate -> sink]
```

相邻算子满足以下条件时会被**算子链（operator chaining）**合并到同一个 Task 线程：

- 并行度一致；
- 下游只有一个输入；
- 分区方式是 forward（一对一）；
- 没有显式禁用链化；
- 不在不同的 slot sharing group。

链化的收益是减少线程切换、减少序列化和网络传输。代价是：

- 一个 task 抛出异常会拖垮整条链；
- 火焰图里多个算子混在一起，定位变难；
- 长链的 checkpoint 同步点更粗，恢复粒度更大。

控制手段：

```java
stream.disableChaining();                 // 整条流不链化
stream.startNewChain();                   // 从当前算子开始新链
stream.slotSharingGroup("heavy");         // 隔离 slot 组
env.getConfig().setAutoWatermarkInterval(200);
```

### 并行度与数据分布

每个算子有自己的并行度。边的分区方式决定记录如何从上游 subtask 到下游 subtask：

| 分区 | 语义 | 典型场景 |
|---|---|---|
| forward | 一对一，要求并行度相同 | 链化、算子直连 |
| hash | 按 key 哈希 | keyBy、窗口聚合 |
| rebalance | 轮询 | 打散热点、sink 均衡 |
| rescale | 按比例分组轮询 | 上下游并行度成比例 |
| broadcast | 复制到所有下游 | 广播维表、规则 |
| custom | 用户自定义 | 特殊路由 |

`keyBy` 是流处理的分水岭：它把无界流切成按 key 独立的逻辑分区，之后的 keyed state、窗口、定时器都只在 key 内可见。第 3 章会展开。

## 2.3 Slot、并行度与资源

TaskManager 提供 **slot**。slot 是资源调度的最小单位，包含内存等资源，但不绑定固定 CPU 核。一个 slot 可以运行一个算子链（pipeline）中的多个 subtask。

关键关系：

- 作业的**并行度**是每个算子/subtask 的数量；
- 默认 **slot sharing** 允许同一作业不同算子的 subtask 共享 slot；
- 因此一个作业通常需要 `最大算子并行度` 个 slot，而不是所有算子并行度之和；
- `slotSharingGroup` 可以把不同算子隔离到不同 slot 组，常用于隔离重型或故障率高的算子。

例：source 并行度 4，窗口聚合并行度 4，sink 并行度 2，默认共享时通常需要 4 个 slot；如果把 sink 放到独立组，则需要额外的 2 个 slot。

```text
TaskManager: 4 slots
┌─────────┬─────────┬─────────┬─────────┐
│ slot 0  │ slot 1  │ slot 2  │ slot 3  │
│ source0 │ source1 │ source2 │ source3 │
│ window0 │ window1 │ window2 │ window3 │
│ sink0   │ sink1   │         │         │
└─────────┴─────────┴─────────┴─────────┘
```

资源配置要同时看 slot 数、每个 TaskManager 的内存、CPU 和网络缓冲。仅增加并行度而不同步增加 slot/内存，往往只会让作业更难调度。

### 细粒度资源管理

传统 slot 是“粗粒度”的：一个 slot 固定分配一定的内存和 CPU，即使作业用不满也不能给别人。Flink 1.14 之后支持**细粒度资源管理（Fine-Grained Resource Management）**，允许算子声明自己的 GPU、磁盘、内存需求，调度器按算子实际需求分配资源。适合：

- 异构算子（有的需要 GPU，有的只需要 CPU）；
- 多租户集群，希望提高资源利用率；
- 算子级隔离，避免一个重型算子拖垮整个 slot。

代价是配置复杂、调度开销更高，且并非所有部署模式都完整支持。生产上要先用粗粒度跑通，再针对瓶颈算子做细粒度。

### 并行度设置顺序

并行度可以在四个层级设置，优先级从高到低：

1. 算子级 `setParallelism()`；
2. 作业级 `env.setParallelism()`；
3. 提交时 `-p`；
4. 集群配置 `parallelism.default`。

建议：**核心算子显式设置并行度，sink 按下游承载能力设置，source 按分区数设置**。不要把并行度完全交给集群默认值，否则换环境时行为会变。

### 2.3.1 一个资源规划算例

假设一个作业：

| 算子 | 并行度 | 资源特点 |
|---|---|---|
| Kafka source | 8 | 网络/反序列化 |
| map/filter | 8 | 轻 CPU |
| keyBy + window + aggregate | 16 | 大状态、RocksDB、CPU |
| sink | 4 | 下游限流 |

**默认 slot sharing** 下，一个作业的 slot 数等于“所有算子并行度的最大值”：

```text
slots = max(8, 8, 16, 4) = 16
```

如果每个 TaskManager 提供 4 个 slot，则需要 4 个 TaskManager；每个 TM 16 GB，则每个 slot 约 4 GB（还要扣除堆外/网络/元空间，不能把 16 GB 全算给用户堆）。

**把 sink 放进独立 slot sharing group**：

```java
stream.slotSharingGroup("sink-group");
```

此时：

```text
主 group：max(8, 8, 16) = 16
sink group：4
总 slots = 20
```

多出的 4 个 slot 换来的是：sink 反压/故障不会直接拖垮 window 链，sink 也可以单独扩容。

**把 window 也隔离**：

```text
source/map group：max(8, 8) = 8
window group：16
sink group：4
总 slots = 28
```

隔离越细，资源利用率越低，但故障域和调优边界越清晰。生产上通常只隔离“重状态/重 IO/易故障”的算子，不要给每个算子都建 group。

**细粒度资源**适合更极端的场景：window 算子声明 16 GB managed memory，source 只声明网络内存，GPU 算子声明 GPU。它能把“一个 slot 固定一大块资源”变成“按算子需求分配”，但调度复杂度和版本支持要提前验证。

CPU 的估算不能只看 slot 数：slot 不绑定 CPU 核。一个实用起点是：

```text
总核数 ≈ 峰值事件率 × 单条记录平均 CPU 时间 / 目标利用率
```

再用压测修正。单条记录 CPU 时间包括反序列化、业务逻辑、状态读写、序列化；RocksDB 和 GC 往往是大头。不要用“并行度 = CPU 核数”这种拍脑袋规则。

## 2.4 部署模式

### Session 模式

先启动一个长期运行的 Flink 集群，多个作业共享。优点是提交快、资源复用；缺点是作业之间相互影响，依赖冲突需要靠 classloader 隔离，一个作业打满资源会影响其他作业。适合交互式开发和中小规模共享平台。

```bash
# 启动 session 集群
./bin/start-cluster.sh
# 提交作业
./bin/flink run -c com.example.Job app.jar
```

### Per-job 模式（已移除）

Per-job 模式在 Flink 1.15 被标记废弃，并在 **Flink 2.0 正式移除**（FLINK-26000、FLINK-36310）。它原本只在 YARN 上可用：每个作业单独启动一个集群，JobManager 只服务该作业。现在应迁移到 Application 模式——YARN 上的 `yarn-application` 同样提供整作业级别的资源隔离，还支持多作业应用。看到旧文档或脚本里的 `yarn-per-job`、`run-application`、`YarnJobClusterExecutor`，都应视为 1.x 遗留。

### Application 模式

`main()` 在集群内执行，客户端只负责提交。依赖、配置和作业生命周期绑定，适合 K8s 和生产环境。它与 Kubernetes Operator 的 `FlinkDeployment` 配合最自然。

Flink 2.0 起 `run-application` 子命令被合并进 `flink run`（FLIP-464），部署目标用 `--target`（或 `-t`）表达：

```bash
./bin/flink run \
  --target kubernetes-application \
  -Dkubernetes.cluster-id=risk-job \
  -Dkubernetes.container.image=registry/risk-flink:1.0 \
  local:///opt/flink/usrlib/app.jar
```

如果使用的是 1.x，命令仍是 `flink run-application -t kubernetes-application ...`；教程后文若出现该写法，均指 1.x 兼容语法。

Application 模式的关键收益：

- 客户端不再承担依赖解析和 `main()` 执行，网络抖动不影响提交；
- 每个作业一个集群，资源隔离清晰；
- 与 Operator 的 savepoint、升级、恢复流程集成。

### 资源平台

- **Standalone**：手工启动 JM/TM，适合实验和小集群；
- **YARN**：资源交给 YARN，2.x 只剩 session 与 application 两种模式；
- **Kubernetes**：原生部署或 Flink Kubernetes Operator，支持弹性、HA、savepoint 管理；
- **云托管**：AWS Managed Flink、阿里云实时计算、Confluent Cloud、华为云等，把资源与运维封装为服务。云托管通常滞后于社区版本，选型时要核对版本与特性矩阵。

### YARN 与 Standalone 的落地细节

YARN 在 2.x 只剩两种模式，命令形态也随之变化：

```bash
# Session：先起一个长期集群，再提交多个作业
./bin/yarn-session.sh -d -s 4 -jm 2048 -tm 8192
./bin/flink run --target yarn-session -c com.acme.Job app.jar

# Application：每个应用一个集群，main() 在集群内执行
./bin/flink run --target yarn-application \
  -D yarn.application-attempts=3 \
  -c com.acme.Job app.jar
```

YARN 落地要额外关注：

- **队列与容量**：要同时配置 YARN 队列配额和 Flink 的 slot/容器规格（ApplicationMaster 与 TaskManager 的 vcore/内存）；队列配额不足时作业会长期停在 ACCEPTED；
- **Kerberos**：提交前 `kinit`，并确保 keytab、 delegation token、HDFS/S3 凭证能传到 TaskManager；长时间作业要开 token 续期；
- **本地目录**：NodeManager 的 `yarn.nodemanager.local-dirs` 要足够大，RocksDB 和 shuffle 都落在这里；
- **日志聚合**：开启 `yarn.log-aggregation-enable`，否则容器删除后日志丢失；
- **镜像与依赖**：用 `yarn.provided.lib.dirs` 把公共依赖放到 HDFS，避免每次上传大 jar；
- **节点亲和/标签**：用 YARN node label 把状态重的作业放到本地 SSD 节点。

Standalone 则适合实验和“自己管机器”的场景：手工起 JM/TM、配置 `flink-conf.yaml`/`config.yaml`、自己保证 HA 和资源隔离。它没有资源申请和弹性，生产上通常只在专有集群或边缘环境使用。

### 2.4.1 容器化与本地运行

本地验证语义时，Docker 比“装一个 Flink 发行版”更省事。官方镜像同时包含 JobManager 和 TaskManager 的入口：

```bash
# JobManager
docker run --name flink-jm -p 8081:8081 \
  -e FLINK_PROPERTIES="jobmanager.rpc.address: flink-jm
taskmanager.numberOfTaskSlots: 4" \
  flink:2.3 jobmanager

# TaskManager（与 JM 同一网络）
docker run --name flink-tm --link flink-jm \
  -e FLINK_PROPERTIES="jobmanager.rpc.address: flink-jm
taskmanager.numberOfTaskSlots: 4" \
  flink:2.3 taskmanager
```

镜像 tag 的具体后缀（Scala 版本、Java 版本）随发行版变化，落地时以官方镜像页为准。`FLINK_PROPERTIES` 用换行分隔多个配置项；也可以挂载 `config.yaml`。

Docker Compose 的最小形状：

```yaml
services:
  jobmanager:
    image: flink:2.3
    command: jobmanager
    ports: ["8081:8081"]
    environment:
      - |
        FLINK_PROPERTIES=
        jobmanager.rpc.address: jobmanager
        taskmanager.numberOfTaskSlots: 4
    volumes:
      - ./checkpoints:/opt/flink/checkpoints
      - ./usrlib:/opt/flink/usrlib
  taskmanager:
    image: flink:2.3
    command: taskmanager
    depends_on: [jobmanager]
    environment:
      - |
        FLINK_PROPERTIES=
        jobmanager.rpc.address: jobmanager
        taskmanager.numberOfTaskSlots: 4
    volumes:
      - ./checkpoints:/opt/flink/checkpoints
      - ./usrlib:/opt/flink/usrlib
```

提交作业：

```bash
docker exec -it flink-jm flink run -c com.acme.Job /opt/flink/usrlib/app.jar
```

本地实验的边界：

- 单机 Docker 没有真正的资源隔离和 HA，不能验证故障恢复；
- checkpoint 目录要挂载到宿主机，否则容器删除就丢；
- 不要把 8081 暴露到公网，REST/Web UI 默认没有强认证；
- 生产用 Kubernetes Operator 或托管服务，不要用 `docker run` 管长期作业；
- 需要多版本对比时，用不同 tag 的镜像，不要把多个 Flink 版本混在一个容器里。

Standalone 的本地启动则是：

```bash
./bin/start-cluster.sh
./bin/flink run -c com.acme.Job examples/app.jar
./bin/stop-cluster.sh
```

它适合“一台机器、快速验证、不需要弹性”的场景；日志、HA、升级、资源隔离都要自己负责。

## 2.5 高可用与状态存放

JobManager 本身也会失败，因此需要 HA：

- 用 ZooKeeper 或 Kubernetes HA 保存 JobManager 的元数据（作业图、checkpoint 指针、leader 选举）；
- checkpoint/savepoint 必须放在所有节点都能访问的持久存储（HDFS、S3、OSS、GCS、PVC）；
- TaskManager 失败由 JobManager 重新调度；JobManager 失败则由新 leader 从最近 checkpoint 恢复。

```yaml
# config.yaml 中的 Kubernetes HA 片段
high-availability.type: kubernetes
high-availability.storageDir: s3://bucket/flink/ha
kubernetes.cluster-id: risk-job      # K8s HA 的集群标识，HA ConfigMap 以它命名
```

注意：`high-availability.cluster-id` 是 ZooKeeper 时代的标识，在 YARN/原生 Kubernetes 上不要手工设置（Flink 会从应用 ID 推导），Kubernetes Operator 甚至会拒绝该键；K8s 场景用 `kubernetes.cluster-id`。**HA 不等于 exactly-once**。HA 只保证“有人接管”，端到端一致性还取决于 source offset、sink 事务和检查点是否原子完成。一个常见的错误是只配了 HA，却没有验证 sink 的 2PC 和 source 的 offset 提交，故障后仍然重复写。

### 故障域

- **Task 失败**：单个 subtask 异常，JobManager 重启该 task 或整个 pipeline；
- **TaskManager 失败**：该节点上所有 task 失败，资源重新申请；
- **JobManager 失败**：新 leader 从 HA 存储恢复，重新调度；
- **Region failover**：把作业图切成多个 region，只重启失败的 region，减少恢复范围（通过 `jobmanager.execution.failover-strategy: region` 开启，且对作业图有要求）。

故障域越小，恢复越快，但配置和状态管理越复杂。生产上先保证“能恢复”，再优化“恢复多快”。

### 2.5.1 HA 的内部组件与故障语义

配置项只是表象。理解 HA 的关键，是知道 JobManager 到底把什么写进了外部存储、新 leader 又读回了什么。

```text
HighAvailabilityServices
├── LeaderElectionService      竞选 leader（创建/续约竞选节点）
├── LeaderRetrievalService     监听 leader 变化（通知 TM/Client/其他 JM）
├── JobGraphStore              保存已提交但未完成的 JobGraph
├── CheckpointRecoveryFactory  读取最近完成的 checkpoint
└── RunningJobsRegistry        记录作业运行状态（RUNNING/FINISHED）
```

- **LeaderElectionService**：每个 JobManager 启动时参与竞选。ZooKeeper 用临时 znode，Kubernetes 用 ConfigMap/Lease；谁先创建成功谁是 leader。
- **LeaderRetrievalService**：TaskManager、Client、其他 JobManager 通过它获知当前 leader 的地址；leader 变化时重新连接。
- **JobGraphStore**：保存作业图，保证新 leader 不需要原客户端仍在就能恢复作业。
- **CheckpointRecoveryFactory**：从 checkpoint 存储读取最近完成的快照；它不复制状态，只读指针。
- **RunningJobsRegistry**：记录作业是 RUNNING 还是 FINISHED，避免重复恢复已完成的作业。

新 leader 的恢复顺序：

```text
1. 竞选成功，获得 leader 身份
2. 从 JobGraphStore 读取待恢复的 JobGraph
3. 从 CheckpointRecoveryFactory 找到最近完成的 checkpoint
4. 重建 ExecutionGraph，向 ResourceManager 申请 slot
5. 部署 task，从 checkpoint 恢复状态
6. source 按 checkpoint 中的 offset 重新读取
```

**HA 存储里没有算子状态**。算子状态在 checkpoint/savepoint 存储里；HA 存储只放“作业图 + 指针 + 运行状态”。所以：

- checkpoint 存储不可用，HA 也救不了作业；
- HA 存储不可用，新 leader 不知道有哪些作业、该从哪个 checkpoint 恢复；
- 两者要用不同的故障域/权限/备份策略，但都要可靠。

### 脑裂与 fencing

分布式 leader 选举最怕“旧 leader 还活着，新 leader 已经上任”。Flink 用 leader session ID / fencing token 让旧 leader 的请求失效：

- 旧 leader 的 checkpoint 协调、作业操作会被拒绝；
- TaskManager 只接受当前 leader 的连接；
- 新 leader 会清理旧 leader 留下的未完成 checkpoint。

即便如此，外部副作用仍可能重复：旧 leader 已经通知 sink 提交、但新 leader 不知道。端到端 exactly-once 依赖 sink 的幂等/事务 fencing，而不是只靠 HA。

### ZooKeeper HA 与 Kubernetes HA

| 维度 | ZooKeeper HA | Kubernetes HA |
|---|---|---|
| 选举存储 | znode | ConfigMap / Lease |
| 集群标识 | `high-availability.cluster-id` | `kubernetes.cluster-id` |
| 依赖 | ZooKeeper 集群 | K8s API/RBAC |
| 常见故障 | session 超时、znode 冲突 | RBAC 不足、ConfigMap 配额/冲突 |
| 适用 | YARN/Standalone/自建 | 原生 K8s/Operator |

Kubernetes HA 下要确认：

- ServiceAccount 能创建/更新/删除 ConfigMap 和 Lease；
- `kubernetes.cluster-id` 全局唯一，多个集群同名会互相干扰；
- HA 存储目录可写，且与 checkpoint 存储区分；
- 节点时间同步，否则 Lease 过期判断异常；
- Operator 自身也有 leader 选举，避免多个 Operator 实例同时调谐同一个 CR。

### HA 不等于零停机

HA 能缩短不可用时间，但不能消除：

- 检测时间（心跳/探针）；
- 选举时间；
- 状态加载时间；
- source 重放时间；
- sink 事务恢复时间。

大状态作业的 JM 切换可能从分钟级到小时级。要缩短，靠的是更小的状态、更快的存储、本地恢复、区域 failover，而不是“多开一个 JobManager”。

## 2.6 线程、内存与反压

每个 TaskManager 是一个 JVM 进程，内部有：

- Task 线程（执行用户算子）；
- 网络线程（Netty，收发缓冲）；
- checkpoint 线程、定时器线程；
- JVM 元空间、堆、直接内存、RocksDB 本地内存等。

Flink 1.10 之后采用统一内存模型：

```text
Total Process Memory
├── JVM Metaspace
├── JVM Overhead
├── Flink Memory
│   ├── Task Heap（用户代码堆）
│   ├── Managed Memory（RocksDB、批算子）
│   ├── Network Memory（网络缓冲）
│   └── Framework Heap/Off-heap
└── 其他 JVM 内存
```

Task 线程把结果写入网络缓冲，下游消费。如果下游变慢，缓冲耗尽，上游会被反压（backpressure），最终追溯到 source。Flink 的 credit-based 流控把“下游还有多少缓冲”反馈给上游，避免盲目发送。

这一机制是 Flink 低延迟和高吞吐的关键，也是排障时最常看的指标之一。第 21 章会深入内存模型和反压；Task 线程内部如何用 mailbox 串行化记录、watermark、barrier 和定时器，见 2.9。

## 2.7 配置入口与版本差异

Flink 1.x 主要使用 `flink-conf.yaml`；Flink 2.x 使用 `config.yaml`，并提供迁移工具：

```bash
./bin/migrate-config-file.sh flink-conf.yaml config.yaml
```

核心配置项类似：

```yaml
jobmanager.rpc.address: flink-jobmanager
taskmanager.numberOfTaskSlots: 4
parallelism.default: 4
state.backend.type: hashmap        # 或 rocksdb、forst
state.checkpoints.dir: s3://bucket/checkpoints
execution.checkpointing.interval: 30s
execution.checkpointing.mode: EXACTLY_ONCE
```

配置的优先级通常是：命令行/代码 > 作业级配置 > 集群配置 > 默认值。不要把关键语义（例如 checkpoint 间隔、状态后端）只放在集群默认配置里，否则迁移环境时容易“悄悄变了”。

2.x 的配置迁移还有两个坑：

- 大量 1.x 的 `StateBackend` getter/setter 在 2.0 被移除，改用 `Configuration`/`ConfigOption`；
- `Time` 类型逐步被 `java.time.Duration` 取代，旧代码编译会报错。

## 2.8 MiniFlink 的映射

MiniFlink 不实现真正的 JM/TM，而是用单进程模拟：

- `JobGraph` 对应提交图；
- `RuntimeTask` 对应 subtask；
- `RuntimeTask.input_channels` 和 `downstream` 对应网络通道；
- `JobExecution` 负责调度、watermark 传播和检查点；
- `LocalCluster` 模拟 slot 分配和作业状态。

这足以观察“图如何变成任务、任务如何交换数据”，但无法模拟 JVM 内存、网络缓冲和真正分布式故障。第 29 章会逐行拆解。

## 2.9 Task 线程、Mailbox 与检查点协作

前面把 TaskManager 描述成“slot 里跑 subtask”。再往下看一层，一个 subtask 的执行是一个**单线程事件循环**：`StreamTask` 从输入读取记录、调用算子、处理 watermark/barrier、触发定时器，全部在同一个 task 线程里串行完成。Flink 用 **mailbox** 把“外部事件”安全地投递到这个线程。

```text
StreamTask 线程
  └── Mailbox 循环
        ├── 处理输入记录（operator.processElement）
        ├── 处理 watermark / barrier
        ├── 处理定时器回调
        ├── 处理 checkpoint 快照请求
        ├── 处理取消/关闭等控制消息
        └── 处理异步回调（Async I/O 完成、yield）
```

为什么要这么设计？

- **状态安全**：keyed state、timer、窗口状态都只在 task 线程里访问，不需要给每个状态加锁；
- **顺序可控**：记录、watermark、barrier、timer 有明确的处理顺序；
- **可协作**：checkpoint、取消、异步回调可以插到记录之间，而不是抢占线程。

### 三类线程不要混淆

| 线程/组件 | 职责 | 与 mailbox 的关系 |
|---|---|---|
| Task 线程 | 执行用户算子、状态、定时器 | mailbox 的所有者 |
| 网络线程（Netty） | 收发包、credit、缓冲 | 把数据和事件交给 task 的输入门 |
| 定时器服务 | 处理时间到期 | 把回调投递到 mailbox，由 task 线程执行 |
| checkpoint 协调 | JM 侧触发、收集确认 | barrier 随数据流进入 task，快照在 task 线程做 |
| Async I/O 线程池 | 外部请求 | 完成后把结果投递回 mailbox |

关键结论：**用户代码阻塞 task 线程，就等于阻塞这个 subtask 的 mailbox**。`Thread.sleep`、同步 JDBC/HTTP、长时间 GC、无界循环、锁等待，都会让记录处理、watermark、定时器和 checkpoint 一起停摆。

### Barrier 为什么能“切”出快照

Checkpoint barrier 不是带外信号，而是像记录一样在通道里流动。task 在 mailbox 循环里读到 barrier 时：

1. 对齐模式：等待所有输入通道的 barrier 到齐；先到的通道后续数据被缓冲；
2. 非对齐模式：把当前 in-flight 数据一起写入快照，立即转发 barrier；
3. 在 task 线程内对算子状态做快照；
4. 把快照句柄/确认发回 CheckpointCoordinator。

因为快照发生在 task 线程、barrier 之前的数据都已处理，所以“状态”和“输入位置”对应同一个逻辑时刻。反过来，如果用户在 `processElement` 里阻塞很久，barrier 迟迟到不了算子，checkpoint 就会变慢甚至超时。

### 算子链把多个算子放进同一个 mailbox

相邻算子链化后共享一个 task 线程。好处是减少线程切换和序列化；代价是：

- 链中任何一个算子阻塞，整条链都停；
- 链中任何一个算子抛异常，整条链重启；
- 火焰图里多个算子混在一起，定位更难。

所以“把重 IO 放进 map”不仅慢，还会拖垮同一链上的所有算子。正确做法是异步化、拆链，或把外部调用放到独立算子/服务。

### 处理时间定时器与 mailbox

处理时间定时器由定时器服务在到期时投递到 mailbox，再由 task 线程调用 `onTimer`。这意味着：

- `onTimer` 不会与 `processElement` 并发，状态访问是安全的；
- 如果 task 线程被阻塞，定时器会延迟；
- 恢复后，已过期的处理时间定时器可能集中投递，造成瞬时压力。

事件时间定时器则由 watermark 推进触发，同样在 task 线程内执行。

### 工程上的四条规则

1. **不要在算子线程做同步外部调用**：用 Async I/O、外部服务或预计算；
2. **不要在 `processElement` 里做重初始化**：连接、模型、缓存放到 `open`；
3. **控制单条记录的处理时间**：长循环要拆分/让出，避免饿死 mailbox；
4. **给重算子拆链/独立 slot 组**：限制故障和阻塞的爆炸半径。

Mailbox 本身没有多少面向用户的“调参项”。性能问题通常不是“mailbox 太小”，而是“task 线程被阻塞”或“单条记录太重”。理解这一点，比记住任何参数都重要。

## 2.10 生产部署检查清单

- [ ] 使用 application 模式，每个作业独立集群或独立 namespace；
- [ ] JobManager HA 已开启，HA 存储与 checkpoint 存储在可靠对象存储；
- [ ] TaskManager 数量 × slots ≥ 最大算子并行度；
- [ ] 关键算子显式设置并行度和 UID；
- [ ] 内存模型按总进程内存反推，不孤立调 `taskmanager.memory.task.heap.size`；
- [ ] checkpoint 间隔、超时、最小间隔与状态大小匹配；
- [ ] 网络缓冲与反压指标有告警；
- [ ] 版本升级前做 savepoint 恢复演练，确认状态兼容。

## 本章小结

Flink 运行时可以概括为：**Client 提图，JobManager 调度，TaskManager 用 slot 执行，网络通道传数据，checkpoint 协调器保一致性**。部署模式决定隔离与启动方式，slot 和并行度决定资源形状，算子链决定线程和序列化边界。

下一章进入 DataStream API，看看用户代码如何变成这张图，以及类型系统为什么会影响性能、状态兼容性和故障恢复。
