# 附录 D 术语表

## D.1 时间与窗口

| 术语 | 含义 |
|---|---|
| Event Time | 事件实际发生的时间，通常来自业务字段 |
| Processing Time | 算子所在机器的系统时间 |
| Ingestion Time | 数据进入 Flink source 的时间 |
| Watermark | 表示“事件时间小于等于该值的数据已到齐”的单调时间戳 |
| Idleness | 空闲输入不阻塞 watermark 的机制 |
| Watermark Alignment | 让快分区等待慢分区，限制 watermark 漂移/状态膨胀 |
| Window | 把无界流切成的有界区间 |
| Tumbling | 固定大小、不重叠的窗口 |
| Sliding | 固定大小、可重叠的窗口 |
| Session | 按活动间隔动态合并的窗口 |
| Cumulate | 累积窗口，按 step 增量合并到最大窗口 |
| Trigger | 决定窗口何时触发的逻辑 |
| Evictor | 触发前/后移除窗口元素的逻辑 |
| Allowed Lateness | 窗口结束后仍接受迟到数据的时间 |
| Late Data | 超过 watermark/允许迟到范围的数据 |
| Side Output | 从主流分叉出的带标签数据流 |

## D.2 状态与容错

| 术语 | 含义 |
|---|---|
| Keyed State | 按 key 分区的状态 |
| Operator State | 与算子实例绑定、不按 key 分区的状态 |
| Broadcast State | 所有 subtask 持有相同副本的状态 |
| State Backend | 状态存储与快照的实现（HashMap/RocksDB/ForSt） |
| ForSt | Flink 2.x 的分离式状态存储，远程 DFS 为主、本地盘缓存；2.3 仍实验 |
| Key Group | key 的哈希分组，扩缩容时状态迁移单位 |
| TTL | 状态过期时间 |
| State Processor API | 离线读写 savepoint 的 API |
| Queryable State | 1.x 按 key 远程读算子状态的机制；1.18 起 deprecated，不建议新系统使用 |
| Checkpoint | 自动的全局一致状态快照，用于故障恢复 |
| Savepoint | 手动触发、格式稳定的检查点，用于升级/迁移 |
| Barrier | 划分 checkpoint 的流内标记 |
| Alignment | 多输入等待所有屏障到齐的过程 |
| Unaligned Checkpoint | 不对齐、把 in-flight 数据写入快照的检查点 |
| Changelog State Backend | 用 changelog 减少 checkpoint 上传的状态后端 |
| Exactly-once | 作业内部状态不重不丢；端到端还需 source offset 与 sink 事务/幂等配合 |
| At-least-once | 不丢但可能重复 |
| Two-phase Commit | 预提交 + 提交/回滚的事务协议 |
| RPO / RTO | 恢复点目标 / 恢复时间目标 |

## D.3 运行时

| 术语 | 含义 |
|---|---|
| JobManager / JobMaster | 单个作业的协调者 |
| TaskManager | 工作节点，提供 slot |
| Dispatcher | 作业提交/Web UI 入口 |
| ResourceManager | 管理 TM/slot 与外部资源 |
| Slot | TaskManager 的资源调度单位 |
| Slot Sharing | 同一作业不同算子共享 slot |
| StreamGraph | 用户 API 生成的逻辑图 |
| JobGraph | 提交给 JobManager 的图 |
| ExecutionGraph | 按并行度展开的调度图 |
| Operator Chain | 相邻算子合并到同一 task 线程 |
| Backpressure | 下游处理不过来导致上游被阻塞 |
| Credit-based Flow Control | 基于下游可用缓冲的流控 |
| Buffer Debloating | 根据吞吐动态调整网络缓冲 |
| Region Failover | 只重启失败 region 的恢复策略 |
| Adaptive Scheduler | 根据可用资源动态决定并行度 |
| Application Mode | main() 在集群内执行、每作业独立集群 |
| DataStream API V2 | Flink 2.x 的实验 API，ProcessFunction/StateDeclaration/广义 watermark |
| StateDeclaration | V2 的状态声明，带重分配模式（NONE/REDISTRIBUTABLE/IDENTICAL） |
| Generalized Watermark | V2 对 watermark 的统一抽象，支持声明/窥视/消费 |
| Adaptive Partition Selection | 2.3 按数据分布选择分区策略（实验/可配置） |
| Speculative Execution | 批作业为慢 task 启动并行 attempt，先完成者胜出；需 AdaptiveBatch |
| Hybrid Shuffle | shuffle 数据在内存/本地磁盘/远程 tier 间分层，降低落盘瓶颈 |
| Latency Marker | source 周期注入的延迟采样标记，绕过用户函数，测 source→算子的排队延迟 |

## D.4 SQL 与表

| 术语 | 含义 |
|---|---|
| Dynamic Table | 随时间变化的表 |
| Changelog | 动态表的插入/更新/删除消息流（+I/-U/+U/-D） |
| Append-only | 只追加的结果模式 |
| Retract | 先撤回旧值再插入新值的模式 |
| Upsert | 按主键更新的模式 |
| TVF | 表值函数，如 TUMBLE/HOP/SESSION/CUMULATE |
| Temporal Join | 按事件时间取维表版本的 join |
| Interval Join | 按时间区间关联两条流 |
| Regular Join | 无时间约束、两侧保留历史的 join |
| Delta Join | 多流同源 join 用外部索引点查替代两侧完整状态的执行策略（2.1+） |
| Lookup Join | 查询外部维表的 join |
| Materialized Table | 按新鲜度刷新、统一流批的物化表（1.20+） |
| CTAS / RTAS | `CREATE TABLE AS SELECT` / `[CREATE OR] REPLACE TABLE AS SELECT` |
| CDAS | `CREATE DATABASE AS SELECT`，Flink CDC/云厂商扩展，**不是**开源 Flink 语法 |
| Statement Set | 多条 INSERT 共享一个 JobGraph/checkpoint 的执行单元 |
| PTF | Process Table Function，有状态/定时器的表函数（2.1+） |
| SinkUpsertMaterializer | 保证 upsert 顺序的 sink 物化算子 |
| ML_PREDICT | SQL 中调用模型的函数（2.1+） |
| VECTOR_SEARCH | 流式向量检索（2.2+） |
| FROM_CHANGELOG / TO_CHANGELOG | changelog 与 append 流互转（2.3+） |
| ON CONFLICT | upsert sink 的冲突策略（DO NOTHING/DO ERROR/DO DEDUPLICATE） |
| VARIANT | 保留类型信息的半结构化 JSON 类型（2.1+） |
| QUALIFY | 窗口函数之后的过滤子句 |

## D.5 数据集成

| 术语 | 含义 |
|---|---|
| CDC | Change Data Capture，变更数据捕获 |
| Incremental Snapshot | 全量 + 增量一体化读取 |
| Schema Evolution | schema 演进/兼容 |
| Lakehouse | 数据湖 + 表格式 + ACID/元数据 |
| Paimon / Iceberg / Hudi | 常见湖仓表格式 |
| Fluss | 2026 年毕业的 Apache 顶级流存储项目，面向分析型流负载与湖仓 |
| Hybrid Source | 历史有界 + 实时无界的混合 source |
| Compaction | 合并小文件/ LSM 层 |
| Exactly-once Sink | 事务/幂等写入的 sink |

## D.6 业务与平台

| 术语 | 含义 |
|---|---|
| SLO | 服务等级目标 |
| SLA | 服务等级协议 |
| TCO | 总拥有成本 |
| ROI | 投资回报率 |
| RPO / RTO | 恢复点目标 / 恢复时间目标 |
| Multi-tenancy | 多租户 |
| RBAC | 基于角色的访问控制 |
| Audit | 审计 |
| Serverless | 按需/按量、无需管理集群 |
| Data Freshness | 数据新鲜度（结果与源的时间差） |
| Feature Store | 特征存储，统一离线/在线特征 |
| Point-in-time Correctness | 训练样本只使用当时可见的特征，避免未来信息泄漏 |
| Training-serving Skew | 训练与线上特征/逻辑不一致 |
| RAG | 检索增强生成，检索上下文后调用 LLM |
| Action State Store | Flink Agents 的外部动作状态存储，用于 durable execution |
| Flink Agents | 事件驱动 AI Agent 的 Apache Flink 子项目 |
| MiniFlink | 本教程的单进程 Flink 教学实现 |
