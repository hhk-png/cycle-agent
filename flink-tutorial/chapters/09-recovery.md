# 第9章 故障恢复与状态演进

## 9.1 会坏的东西

流作业长期运行，故障不是异常，而是常态：

- TaskManager 进程崩溃、磁盘满、OOM；
- JobManager 重启、leader 切换；
- 网络抖动、Kafka 超时、外部系统限流；
- 用户代码抛异常、序列化失败；
- checkpoint 存储暂时不可用；
- 机器缩容、Kubernetes Pod 被驱逐；
- 升级作业、改并行度、修 bug；
- 数据倾斜导致某个 subtask 先崩。

Flink 的恢复体系要回答五个问题：

1. 谁来发现失败？
2. 重启多少任务？
3. 从哪个状态恢复？
4. 恢复后如何避免重复/丢失？
5. 如何观察恢复过程？

## 9.2 故障检测与处理流程

```text
Task 失败
  → TaskManager 通知 JobManager（或心跳超时）
  → JobMaster 标记 ExecutionGraph 失败
  → 停止受影响 task
  → 按重启策略等待
  → 从最近完成 checkpoint 恢复状态
  → 重新调度 task
  → source 从 checkpoint offset 重放
  → sink 回滚/等待事务提交
```

如果 JobManager 失败，HA 集群会选举新 leader，从 ZooKeeper/K8s 元数据和最近 checkpoint 恢复整个作业。

### 检测机制

- **心跳超时**：JobManager 与 TaskManager 之间定期心跳，超时判定节点失联；
- **Task 异常**：用户代码抛出未捕获异常，task 直接失败；
- **Checkpoint 失败**：连续失败超过阈值，作业可能被判定为不稳定；
- **ResourceManager 事件**：容器退出、节点驱逐、资源不足；
- **外部健康检查**：K8s liveness/readiness probe。

检测的难点是**区分瞬时故障和持续性故障**。网络抖动可以重试；代码 bug 重试一万次也没用，只会拖长故障时间。

## 9.3 重启策略

```yaml
restart-strategy.type: fixed-delay
restart-strategy.fixed-delay.attempts: 3
restart-strategy.fixed-delay.delay: 10s
```

或：

```yaml
restart-strategy.type: failure-rate
restart-strategy.failure-rate.max-failures-per-interval: 3
restart-strategy.failure-rate.failure-rate-interval: 5min
restart-strategy.failure-rate.delay: 10s
```

或指数退避：

```yaml
restart-strategy.type: exponential-delay
restart-strategy.exponential-delay.initial-backoff: 1s
restart-strategy.exponential-delay.max-backoff: 5min
restart-strategy.exponential-delay.backoff-multiplier: 2.0
restart-strategy.exponential-delay.reset-backoff-threshold: 10min
```

选择原则：

- 瞬时故障（网络、外部限流）：固定延迟 + 有限次数；
- 持续性故障（代码 bug、数据格式）：快速失败，避免无限重启；
- 关键作业：配合告警和 runbook，不要只依赖自动重启；
- 重启次数用尽后，作业进入 FAILED，需要人工介入；
- 指数退避适合“重启本身有成本”的大状态作业。

### 重启策略与 checkpoint 的关系

重启后从最近**成功完成**的 checkpoint 恢复。如果 checkpoint 间隔是 10 分钟，故障发生在两次 checkpoint 之间，恢复后会重放最多 10 分钟的数据。这就是“恢复点目标（RPO）”。

```text
RPO ≈ checkpoint 间隔 + checkpoint 完成时间
RTO ≈ 检测时间 + 重启等待 + 状态加载 + 重放时间
```

缩短 RPO 要更频繁 checkpoint；缩短 RTO 要更小的状态、更快的存储、本地恢复。两者都会增加成本。

## 9.4 局部恢复与区域

全量恢复会重启所有 task，代价高。Flink 支持**区域化 failover**：把执行图划分为多个 region，只重启受影响的 region，其他 task 继续运行。

```yaml
jobmanager.execution.failover-strategy: region
```

区域划分与算子链、shuffle 边界有关。局部恢复要求：

- 未受影响的 task 状态仍然有效；
- checkpoint 能提供一致切面；
- sink 事务能处理部分提交。

Flink 的 failover strategy 只有 `full` 和 `region` 两种。`region` 会在作业图允许时只重启受影响的 region；不要把它和“pipeline 级恢复”混为一谈，也不存在 `pipeline` 这个取值。

它降低了恢复成本，但也增加了理解难度。状态复杂、外部副作用多的作业，先保证全量恢复正确，再考虑区域恢复。

### Region 划分示例

```text
source → map → keyBy → window → sink
         └──── region A ────┘
                         └─ region B ─┘

map 失败：只重启 region A，window/sink 继续
sink 失败：重启 region B，source/window 可能需要回滚
```

## 9.5 本地恢复

RocksDB 可以把状态快照的本地副本留在 TaskManager 磁盘上，恢复时优先从本地加载，减少远程存储下载。配置：

```yaml
# 写 checkpoint 时同时保留本地备份
execution.checkpointing.local-backup.enabled: true
state.backend.rocksdb.localdir: /data/flink/rocksdb

# 恢复时优先使用本地备份（2.x）
execution.state-recovery.from-local: true
```

注意：`state.backend.local-recovery` 在 2.0 已移除，写 checkpoint 和从本地恢复拆成了两个配置。本地恢复依赖 task 被调度回同一节点；Kubernetes 上节点漂移会失效。它不是持久化方案，远程 checkpoint 仍是最终真相。

本地恢复的收益：

- 大状态恢复从“下载 TB 级数据”变成“本地加载”；
- 减少对象存储带宽和恢复时间；
- 适合节点稳定的裸金属/自建集群。

代价：

- 本地磁盘需要额外容量；
- 节点漂移时退化为远程恢复；
- 本地副本与远程 checkpoint 可能不一致，要以远程为准。

## 9.6 扩缩容与状态重分配

改变并行度时，key group 需要重新分配：

```text
key → hash → key group（默认 128 个）→ subtask
```

恢复时按新并行度重新映射：

```text
并行度 4：key group 0-31 → subtask 0, 32-63 → 1, ...
并行度 8：key group 0-15 → subtask 0, 16-31 → 1, ...
```

大部分 keyed state 支持 rescale；operator state 需要看重新分配模式：

- `SPLIT_DISTRIBUTE`（常说的 even-split）：各 subtask 均分；
- `UNION`：每个 subtask 拿到全量；
- `BROADCAST`：每个 subtask 持有相同副本（广播状态）。

`FORWARD` 是流分区方式，不是 operator state 的重新分配模式，不要混用。DataStream API V2 的 `StateDeclaration` 则用 `NONE`/`REDISTRIBUTABLE`/`IDENTICAL` 描述状态能否重分配。

source/sink 的 offset 状态在扩缩容时尤其要小心。Kafka source 通常支持按分区重新分配；自定义 source 需要正确实现。

### 扩缩容的操作顺序

1. 停止写入或准备双跑；
2. 触发 savepoint；
3. 用新并行度从 savepoint 恢复；
4. 验证状态大小、key 分布、输出；
5. 切换上游/下游；
6. 保留旧作业一段时间以便回滚。

不要直接改并行度重启，除非你确认状态可以 rescale 且没有外部副作用。

## 9.7 状态 schema 演进

升级作业时，状态结构可能变化：

- 加字段：通常兼容；
- 删字段：可能兼容（保留旧数据忽略）；
- 改类型：通常不兼容；
- 改 key：分区变化，不能直接恢复；
- 改序列化器：必须保证新旧格式可读。

实践流程：

1. 在预发环境用生产 savepoint 恢复；
2. 验证状态读取、关键指标、输出；
3. 保留旧版本作业和 savepoint；
4. 灰度切换，观察 checkpoint 和延迟；
5. 准备回滚方案。

不要在生产直接“改完代码重启试试”。状态不兼容往往不是启动时报错，而是运行一段时间后结果错误。

### 兼容性矩阵

| 变更 | 兼容性 | 建议 |
|---|---|---|
| 新增可选字段 | 通常兼容 | 用 Avro/Protobuf 默认值 |
| 删除字段 | 视序列化器 | 保留字段并标记废弃 |
| 字段类型变更 | 通常不兼容 | 新字段 + 迁移逻辑 |
| 类名/包名变更 | Kryo 风险高 | 避免或写自定义序列化器 |
| key 类型变更 | 不兼容 | 新作业 + 重放 |
| 状态结构大改 | 不兼容 | State Processor API / 重放 |

State Processor API 的读写接口、savepoint 兼容性规则、1.x → 2.x 迁移路径和演练手册见[附录 L](../appendix/L-state-migration-spi.md)。

## 9.8 端到端恢复

作业内部恢复正确，不代表外部系统正确：

- source 重放：Kafka offset 存在 checkpoint 中，恢复后从该 offset 继续；
- sink 事务：未提交事务要回滚，已提交的不能重复；
- 幂等 sink：用业务主键 upsert；
- 下游缓存：可能需要失效；
- 告警/邮件：可能重复发送，需要去重；
- 特征存储：恢复后特征可能落后，需要回填。

这是“端到端 exactly-once”的真正难点，也是第 11 章的重点。

```text
作业内部：checkpoint 保证状态一致
source：offset 回拨，重放
sink：事务回滚/提交，幂等键
下游：read_committed / 去重
```

任何一环缺失，端到端语义都会退化为 at-least-once 或 at-most-once。

## 9.9 2.x 的弹性与动态扩缩容

Flink 2.x 在弹性方面继续演进：

- **自适应调度器（Adaptive Scheduler）**：根据可用 slot 动态决定并行度，适合 K8s 弹性资源；
- **自适应批调度**：根据数据量决定 shuffle 和并行度；
- **应用级生命周期管理**（2.3）：集群 → 应用 → 作业的层级，便于平台化管理；
- **ForSt**：理论上支持更快的状态重分配，但仍是实验特性。

注意：动态扩缩容不是“无感”的。状态重分配、checkpoint 恢复、下游连接重建都会带来暂停。生产上要把它当作“受控运维操作”，而不是自动魔法。

## 9.10 恢复演练

恢复能力不能靠“应该可以”，要靠演练：

1. **单 task 故障**：kill 一个 TaskManager，观察 region failover；
2. **JobManager 故障**：kill leader，观察 HA 切换；
3. **checkpoint 存储故障**：模拟 S3 限流/不可用；
4. **状态不兼容**：用旧 savepoint 恢复新代码；
5. **扩缩容**：从并行度 N 恢复到 N±k；
6. **端到端重复**：在 sink 提交前后注入故障，验证下游去重。

每次演练记录：

- 检测时间；
- 恢复开始/完成时间；
- 重放数据量；
- 是否有重复/丢失；
- 人工介入步骤。

## 9.11 MiniFlink 的恢复

MiniFlink 通过 `JobExecution.restore(checkpoint)` 恢复：

```python
first = env.create_execution()
first.run(max_rounds=1)
checkpoint = first.checkpoint()
second = env.create_execution()
second.restore(checkpoint)
second.run()
```

恢复内容：

- source 的 offset / 待发射 watermark；
- 算子内部状态（窗口 bucket、CEP 部分匹配、广播状态）；
- KeyedStateBackend 的键值；
- TimerService 的计时器；
- sink 的已写数据（CollectSink 支持，通用 sink 不支持）。

MiniFlink 没有自动故障检测和重启策略执行器，`restart_strategy` 只是配置对象；它演示“从一致快照恢复”的语义，不模拟真实 failover。

## 9.12 灾难恢复与备份

前面讲的是“作业恢复”：task/JM 失败后从 checkpoint 回来。灾难恢复（DR）的范围更大：整个区域、存储桶、元数据、镜像仓库或消息队列没了，作业恢复机制本身也可能不可用。

### 9.12.1 作业恢复 vs 平台恢复

| 层次 | 故障 | 依赖 | 恢复手段 |
|---|---|---|---|
| 作业恢复 | task/JM 崩溃 | checkpoint、HA | 自动重启/区域 failover |
| 平台恢复 | 集群/区域不可用 | 镜像、配置、Catalog、密钥 | 在另一区域重建 |
| 数据恢复 | checkpoint 桶/湖表误删 | 备份、版本化、跨区复制 | 从备份恢复 |
| 上游恢复 | Kafka topic 被删/保留期过短 | 上游备份/重放 | 从数据源重建 |
| 下游恢复 | 目标表/索引损坏 | 幂等重放、对账 | 重放/重建 |

很多团队只做了第一层，就以为“有 checkpoint 就有 DR”。如果 checkpoint 桶和集群在同一个区域、同一个账号，区域故障时两者一起没了。

### 9.12.2 RPO/RTO 分层

```text
RPO（能丢多少数据）  ≈ checkpoint 间隔 + 上游保留窗口
RTO（多久恢复）      ≈ 检测 + 重建集群 + 加载状态 + 重放 + 校验
```

按业务分级：

| 级别 | 典型 RPO | 典型 RTO | 手段 |
|---|---|---|---|
| 核心交易/风控 | 秒级 | 分钟级 | 高频 checkpoint、跨区热备、双跑 |
| 重要实时数仓 | 分钟级 | 十分钟级 | 跨区 checkpoint、镜像/配置备份 |
| 分析/报表 | 小时级 | 小时级 | 每日备份、可重放 |
| 非关键 | 天级 | 天级 | 重建 + 回放 |

RPO/RTO 不是技术指标，而是业务承诺。先和业务确认“能丢多少、能停多久”，再设计备份频率和成本。

### 9.12.3 要备份什么

```text
计算制品：作业 jar、连接器/格式 jar、镜像、Dockerfile、依赖锁
配置：flink-conf/config.yaml、Operator CR、K8s manifests、环境变量
元数据：Catalog（Hive Metastore/JDBC/Paimon/Iceberg）、表 schema、视图、UDF
状态：checkpoint/savepoint 桶、HA 元数据、savepoint 清单
凭证：Secret、证书、keytab、Kafka SASL、云 IAM 角色
上游：Kafka topic 配置、保留期、Schema Registry、CDC 位点
下游：目标表、索引、缓存、告警路由
```

备份要满足：

- **异地/异账号**：至少一份不在主区域/主账号；
- **版本化**：对象存储开启 versioning，误删可恢复；
- **不可变**：关键备份开 object lock/WORM，防勒索/误删；
- **可验证**：定期从备份恢复，而不是只看“备份任务成功”；
- **有主**：每类资产有 owner、保留期、恢复步骤。

### 9.12.4 恢复顺序

```text
1. 确认故障范围与业务影响
2. 恢复身份/密钥/网络（否则后面都做不了）
3. 恢复镜像、配置、Catalog 元数据
4. 恢复/挂载 checkpoint 存储（或从备份复制）
5. 重建 Flink 集群/Operator
6. 从 savepoint/checkpoint 恢复作业
7. 恢复上游消费位点，必要时重放
8. 校验下游数据（计数、对账、抽查）
9. 切回流量/DNS，保留旧环境观察
```

顺序不能反：没有密钥就无法访问存储；没有 Catalog 就无法解析表；没有镜像就无法启动作业。

### 9.12.5 典型灾难场景

| 场景 | 影响 | 关键动作 |
|---|---|---|
| 区域故障 | 集群 + 存储同时不可用 | 切到备用区域，使用跨区复制的 checkpoint |
| checkpoint 桶误删 | 无法恢复 | 对象版本化/跨区副本，重建后重放 |
| Catalog 丢失 | 表定义/权限丢失 | 从 Git/备份恢复 DDL，校验 schema |
| Kafka topic 删除 | 无法重放 | 从上游数据库/湖表重建，检查保留期 |
| 镜像仓库故障 | 无法启动新 Pod | 多仓库镜像、离线镜像包 |
| 凭证泄漏/轮换 | 作业认证失败 | Secret 版本化、双凭证过渡、审计 |
| 下游表损坏 | 结果错误 | 幂等重放 + 对账，避免重复写 |

### 9.12.6 演练与 runbook

DR 不能靠“设计文档”。至少每季度做一次：

1. **桌面演练**：按 runbook 走一遍，找出缺失的权限/联系人；
2. **部分演练**：在备用区域恢复一个非核心作业；
3. **全链路演练**：核心作业 + 上游 + 下游 + 对账；
4. **破坏性演练**：在隔离环境模拟桶删除、Catalog 丢失、区域切换。

Runbook 要写清楚：

- 谁有权执行恢复；
- 每个系统的恢复命令/控制台路径；
- 预期 RTO 和检查点；
- 回滚/降级方案；
- 对外沟通模板。

### 9.12.7 检查清单

- [ ] 核心作业有明确的 RPO/RTO，并与业务确认；
- [ ] checkpoint/savepoint 至少一份跨区域/跨账号；
- [ ] 对象存储开启版本化，关键备份不可变；
- [ ] 镜像、配置、CR、依赖锁可重建；
- [ ] Catalog 元数据有备份和恢复演练；
- [ ] 密钥/证书有备份和轮换方案；
- [ ] 上游保留期 ≥ 最大 RTO + 重放窗口；
- [ ] 下游支持幂等重放/对账；
- [ ] 每季度做 DR 演练并更新 runbook；
- [ ] “作业恢复成功”之外，还有“数据校验通过”的门槛。

## 本章小结

恢复的核心是“从一致状态继续”，而不是“重启进程”。重启策略决定多快重试，区域恢复决定重启范围，状态兼容决定能否升级，sink 事务决定端到端是否正确。

生产上要把恢复当作一条完整链路演练，而不是只看作业能否启动。恢复之后的发布与回滚流程、状态回滚与数据重放的边界，见[附录 P](../appendix/P-release-change-management.md)。下一章进入连接器与数据集成：数据从哪里来、到哪里去，以及连接的边界如何决定作业的一致性。
