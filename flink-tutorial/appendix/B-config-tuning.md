# 附录 B 配置、调优参数与默认值

> Flink 配置项随版本变化；本表给出常见项和调优方向，具体默认值与可用性以所用发行版文档为准。1.x 常见入口是 `flink-conf.yaml`；2.x 使用 `config.yaml`，可用 `bin/migrate-config-file.sh` 迁移。

## B.1 集群与资源

| 配置 | 说明 | 调优提示 |
|---|---|---|
| `jobmanager.rpc.address` | JM 地址 | K8s 用 Service |
| `jobmanager.memory.process.size` | JM 总内存 | 大作业/多作业时增加 |
| `taskmanager.memory.process.size` | TM 总内存 | 按容器限制配置 |
| `taskmanager.numberOfTaskSlots` | 每 TM slot 数 | 与 CPU/内存匹配 |
| `parallelism.default` | 默认并行度 | 受 source 分区限制 |
| `taskmanager.cpu.cores` | CPU 核数 | 容器环境显式设置 |
| `slot.request.timeout` | 申请 slot 超时 | 资源紧张时观察 |
| `jobmanager.scheduler` | 调度器 | `AdaptiveBatch` 批作业默认；流式弹性用 adaptive |

### 内存模型

```yaml
taskmanager.memory.process.size: 16g
taskmanager.memory.task.heap.size: 4g
taskmanager.memory.managed.size: 4g
taskmanager.memory.network.fraction: 0.15
taskmanager.memory.jvm-metaspace.size: 256m
```

顺序：先定总进程内存，再分 Flink 内存，最后才调堆/堆外。不要只调 `-Xmx`。

## B.2 Checkpoint / Savepoint

| 配置 | 说明 | 建议 |
|---|---|---|
| `execution.checkpointing.interval` | 间隔 | 30s～5min，按状态大小 |
| `execution.checkpointing.timeout` | 超时 | 覆盖上传时间 |
| `execution.checkpointing.min-pause` | 两次最小间隔 | 避免连续快照 |
| `execution.checkpointing.max-concurrent-checkpoints` | 并发数 | 通常 1 |
| `execution.checkpointing.mode` | 语义 | 默认 exactly-once |
| `execution.checkpointing.unaligned.enabled` | 非对齐 | 反压严重时评估 |
| `execution.checkpointing.aligned-checkpoint-timeout` | 对齐超时 | 超时转非对齐 |
| `execution.checkpointing.tolerable-failed-checkpoints` | 容忍失败数 | 不要用来掩盖问题 |
| `state.checkpoints.dir` | 存储 | 高可靠对象存储 |
| `state.savepoints.dir` | savepoint | 长期保留 |
| `state.backend.incremental` | 增量 | RocksDB 建议开启 |
| `execution.checkpointing.local-backup.enabled` | 本地备份 | 加速恢复 |

## B.3 状态后端

| 配置 | 说明 |
|---|---|
| `state.backend.type` | `hashmap` / `rocksdb` / `forst` |
| `state.backend.rocksdb.localdir` | 本地目录，SSD |
| `state.backend.rocksdb.memory.managed` | 托管内存（默认 `true`；开启时下面两个绝对值被忽略） |
| `state.backend.rocksdb.block.cache-size` | block cache（仅 `memory.managed=false` 时按绝对值生效） |
| `state.backend.rocksdb.writebuffer.size` | 写缓冲（仅 `memory.managed=false` 时按绝对值生效） |
| `state.backend.rocksdb.writebuffer.count` | 写缓冲数 |
| `state.backend.rocksdb.compaction.style` | compaction 策略 |
| `execution.checkpointing.local-backup.enabled` | 写 checkpoint 的本地备份 |
| `execution.state-recovery.from-local` | 恢复时优先本地备份（2.x） |
| `table.exec.state.ttl` | SQL 状态 TTL |

`state.backend.local-recovery` 在 2.0 已移除，不要继续使用。

ForSt 相关（2.x，实验）：

```yaml
state.backend.type: forst
# 其余 ForSt 配置以版本 FLIP/官方文档为准
```

注意：ForSt 在 2.3 仍不支持 canonical savepoint、全量快照、changelog 和文件合并 checkpoint；生产大状态优先 RocksDB。

## B.4 网络与反压

| 配置 | 说明 | 影响 |
|---|---|---|
| `taskmanager.memory.network.fraction` | 网络内存占比 | 太小限制吞吐 |
| `taskmanager.memory.network.min/max` | 网络内存上下限 | 按规模调整 |
| `execution.buffer-timeout` | 缓冲发送间隔 | 增大吞吐↑延迟↑ |
| `taskmanager.network.memory.buffer-debloat.enabled` | 动态缓冲 | 降低延迟/内存 |
| `taskmanager.network.memory.buffer-debloat.target` | 目标缓冲时长 | 1s 左右 |
| `taskmanager.network.request-backoff.max` | 请求退避 | 网络异常时 |
| `taskmanager.network.retries` | 重试 | 不稳定网络 |

### 批执行：shuffle 与推测

| 配置 | 说明 | 注意 |
|---|---|---|
| `execution.batch-shuffle-mode` | `ALL_EXCHANGES_BLOCKING` / `ALL_EXCHANGES_PIPELINED` / `ALL_EXCHANGES_HYBRID_FULL` / `ALL_EXCHANGES_HYBRID_SELECTIVE` | 混合模式需要 2.x 新实现；旧 legacy mode 已移除；`POINTWISE_EDGES_PIPELINED` 属于 `table.exec.shuffle-mode`，不要写在这里 |
| `taskmanager.network.hybrid-shuffle.external-remote-tier-factory.class` | 远程 shuffle tier 工厂 | 常配 Celeborn；需额外部署与容量规划 |
| `execution.batch.speculative.enabled` | 批作业推测执行 | 默认 false；仅批 + AdaptiveBatch；sink 需支持并发 attempt |
| `execution.batch.speculative.max-concurrent-executions` | 单 vertex 最大并发尝试 | 默认 2；太大会争抢资源 |
| `execution.batch.speculative.block-slow-node-duration` | 慢节点 blocklist 时长 | 默认 1min；只影响后续调度 |

推测执行会重复计算/重复写 shuffle，sink 必须幂等或事务化；sink 默认不参与，需实现 `SupportsConcurrentExecutionAttempts`；带自定义 `SourceEvent` 的 source 需实现 `SupportsHandleExecutionAttemptSourceEvent`。

## B.5 重启策略

```yaml
restart-strategy.type: fixed-delay
restart-strategy.fixed-delay.attempts: 3
restart-strategy.fixed-delay.delay: 10s
```

或：

```yaml
restart-strategy.type: failure-rate
restart-strategy.failure-rate.max-failures-per-interval: 3
restart-strategy.failure-rate.failure-rate-interval: 10min
restart-strategy.failure-rate.delay: 10s
```

或 `exponential-delay`、`none`。关键作业建议有限重试 + 告警，不要无限重启。

## B.6 SQL / Table

| 配置 | 说明 |
|---|---|
| `table.exec.mini-batch.enabled` | 开启 mini-batch |
| `table.exec.mini-batch.allow-latency` | 攒批延迟 |
| `table.exec.mini-batch.size` | 批大小 |
| `table.exec.state.ttl` | 状态保留 |
| `table.optimizer.distinct-agg.split.enabled` | 拆分 distinct |
| `table.optimizer.join-reorder-enabled` | join 重排（视版本） |
| `table.dynamic-table-options.enabled` | 动态表参数 |
| `table.exec.sink.upsert-materialize` | 是否物化：`AUTO`（默认）/`NONE`/`FORCE` |
| `table.exec.sink.upsert-materialize-strategy.type` | 实现策略：`LEGACY`/`MAP`/`VALUE`/`ADAPTIVE`（2.2+） |
| `table.rtas-ctas.atomicity-enabled` | CTAS/RTAS 原子建表（1.18+；需 sink 支持 staging） |
| `execution.runtime-mode` | STREAMING/BATCH/AUTOMATIC |

## B.7 Kafka

| 配置 | 说明 |
|---|---|
| `properties.bootstrap.servers` | broker |
| `properties.group.id` | 消费组 |
| `scan.startup.mode` | `earliest-offset` / `latest-offset` / `group-offsets` / `timestamp` / `specific-offsets` |
| `scan.topic-partition-discovery.interval` | 分区发现 |
| `sink.delivery-guarantee` | at-least-once/exactly-once |
| `sink.transactional-id-prefix` | 事务前缀，必须唯一 |
| `properties.transaction.timeout.ms` | 事务超时 |
| `properties.isolation.level` | **consumer 侧** read_committed；写在 sink 表上无效 |

事务超时经验：

```text
transaction.timeout.ms > checkpoint.interval + checkpoint.timeout + 缓冲
```

## B.8 Kubernetes

| 配置 | 说明 |
|---|---|
| `kubernetes.cluster-id` | 集群 id |
| `kubernetes.namespace` | 命名空间 |
| `kubernetes.jobmanager.service-account` | ServiceAccount |
| `kubernetes.taskmanager.cpu` | TM CPU |
| `taskmanager.memory.process.size` | TM 内存（K8s 不改变这个键） |
| `high-availability.type` | kubernetes |
| `high-availability.storageDir` | HA 存储 |
| `kubernetes.rest-service.exposed.type` | Service 类型 |

## B.9 Watermark / 时间

| 配置 | 说明 |
|---|---|
| `pipeline.auto-watermark-interval` | 周期 watermark 间隔 |
| `pipeline.watermark-alignment.allow-unaligned-source-splits` | 允许不对齐 source split |
| `pipeline.watermark-alignment.buffer-size` | 2.3 对齐环形缓冲，默认 3；0 恢复旧行为 |
| `table.exec.source.idle-timeout` | SQL source 空闲检测 |
| `scan.watermark.alignment.group` / `scan.watermark.alignment.max-drift` | SQL source 的对齐组/最大漂移 |
| `WatermarkStrategy.withWatermarkAlignment(...)` | DataStream source 的对齐组/最大漂移 |

注意：没有一个全局的 `watermark-alignment.max-drift` 配置；最大漂移要挂在 source 的 `WatermarkStrategy` 或 SQL 表选项上。

## B.10 调优顺序

1. 先看 SLO 和瓶颈指标；
2. 解决数据倾斜/同步调用；
3. 调整状态 TTL 和 checkpoint；
4. 调整并行度/slot；
5. 调整内存/网络；
6. 最后才调 JVM/OS 参数。

每次只改一个变量，保留基线。

## B.11 版本迁移提示

- 1.x → 2.x：Java 17、`config.yaml`、`open(OpenContext)`、`Duration`；
- 移除 `SourceFunction`/`SinkFunction` V1/DataSet/Scala API；
- 移除 per-job 部署模式与 `run-application` 子命令，统一为 `flink run --target ...`；
- Kryo/RocksDB 版本变化可能影响状态二进制；
- 升级前在副本上做 savepoint 恢复；
- 升级后检查指标名称/看板。

## B.12 2.x 新配置速查

| 配置 | 版本 | 作用 | 注意 |
|---|---|---|---|
| `state.backend.type: forst` | 2.0 实验 | 分离式状态后端 | 不支持 canonical savepoint/全量快照等 |
| `table.exec.async-state.enabled` | 2.0 | SQL 算子异步状态访问 | 算子覆盖有限 |
| `taskmanager.network.adaptive-partitioner.enabled` | 2.3 | 自适应分区选择 | 需压测 |
| `pipeline.watermark-alignment.allow-unaligned-source-splits` | 1.x+ | watermark 对齐 | 最大漂移在 source/SQL 选项上 |
| `pipeline.watermark-alignment.buffer-size` | 2.3 | 对齐环形缓冲 | 默认 3，0 关闭缓冲 |
| `execution.checkpointing.unaligned.during-recovery.enabled` | 2.3 | 恢复期也允许 checkpoint | 默认 false；需同时开 `recover-output-on-downstream` |
| `table.exec.sink.upsert-materialize-strategy.type` | 2.2+ | `LEGACY`/`MAP`/`VALUE`/`ADAPTIVE` | 与 `ON CONFLICT` 一起理解；是否物化由 `table.exec.sink.upsert-materialize` 控制 |
| `table.optimizer.multi-join.enabled` | 2.1 | StreamingMultiJoin | 实验/需验证 |
| `traces.checkpoint.span-detail-level` | 2.2 | checkpoint trace 详细度 | 影响 trace 量 |
| `metrics.reporter.otel.batch.size` | 2.3 | OTel gRPC 批量导出 | 大作业避免 payload 超限 |
| `s3.*` | 2.3 | 原生 S3 文件系统 | 实验，新命名空间 |
| `metrics.latency.interval` | 1.x+ | 内置延迟追踪间隔 | 默认 0 关闭；开启有开销 |
| `metrics.latency.granularity` | 1.x+ | `single`/`operator`/`subtask` | `subtask` 直方图数量随并行度平方增长 |
| `execution.batch.speculative.enabled` | 1.17+ | 批作业推测执行 | 仅批 + AdaptiveBatch；sink 需支持并发 attempt |
| `execution.batch-shuffle-mode: ALL_EXCHANGES_HYBRID_FULL` | 1.16+/2.x | 混合 shuffle | 2.x 仅保留新实现，legacy 已移除 |

配置项会随版本改名/改默认值。升级时先做 `config.yaml` diff，再在副本上验证语义。
