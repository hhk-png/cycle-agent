# 附录 C MiniFlink 边界、验证与练习

## C.1 MiniFlink 是什么

MiniFlink 是一个单进程、标准库依赖的 Flink 教学实现，用来验证以下概念：

- DataStream/KeyedStream/WindowedStream；
- 事件时间/处理时间、watermark；
- 窗口分配、触发、迟到、侧输出；
- 键控状态、TTL、计时器；
- 微批一致检查点、source offset 恢复；
- 内存 Kafka、文件、SQLite 连接器；
- Table API/SQL 子集、CEP、广播状态；
- 指标、CLI、LocalCluster。

它不是生产引擎，不提供分布式执行、故障自动恢复、事务 sink、RocksDB、Calcite 优化器、K8s 部署。

## C.2 与 Flink 的关键差异

| 维度 | Flink | MiniFlink |
|---|---|---|
| 进程模型 | JM/TM/JVM | 单进程 |
| 网络 | Netty、credit、反压 | 内存 deque，无界 |
| 序列化 | TypeInformation/Kryo | Python 对象 |
| 状态 | Key group、RocksDB | 堆内字典 |
| checkpoint | 屏障对齐/非对齐 | 微批同步快照 |
| 故障恢复 | 自动检测/重启/HA | 手动 restore |
| sink EOS | 2PC/幂等 | 仅 CollectSink 可恢复 |
| SQL | Calcite 流式 | SQLite 有界 |
| CEP | 完整 Pattern/NFA | 简单序列 |
| 安全 | 认证/授权/TLS | 无 |
| 扩缩容 | key group rescale | 不支持 |
| watermark | per-partition/idleness/alignment | 全局生成、无 idleness |

### 明确不实现的语义

- 分布式快照/barrier 对齐/非对齐；
- key group/rescale；
- 事务 sink / read_committed；
- 反压/网络缓冲；
- 算子链/slot sharing；
- HA/failover；
- 类型系统/schema 演进；
- SQL 优化器/changelog/窗口 TVF；
- 安全/多租户。

这些不是“缺陷”，而是教学边界。正文会在相应章节说明生产 Flink 的做法。

### 仍然存在的语义偏差

以下差异不会导致“跑不起来”，但会影响与 Flink 的逐字节对拍，读代码/写练习时要知道：

- **事件时间边界差 1ms**：MiniFlink 在 `watermark >= window.end` 时触发和清理；Flink 的窗口最大时间戳是 `end - 1`，因此 Flink 在 `watermark >= end - 1` 时就可能触发。对毫秒级对拍要特别小心。
- **结束信号会冲刷窗口**：`end_of_input()` 会给所有未触发窗口一次 trigger 机会，包括处理时间窗口；Flink 在作业结束时不会触发未来才到期的处理时间定时器，而是丢弃状态。
- **窗口 keyed state 没有 namespace**：`ProcessWindowFunction` 里的 `ctx.get_state(...)` 与同一 key 的其他窗口共享；只有 `ctx.window_state()` 是按窗口隔离的。Flink 的窗口状态天然带窗口 namespace。
- **普通 ProcessFunction 的定时器不会被服务**：`ProcessFunction` 也能拿到 `timer_service` 并注册定时器，但运行时只为 `KeyedProcessFunction` 弹出/回调定时器；非键控场景请改用 `KeyedProcessFunction` 或在业务上自行调度。
- **Async I/O 不是真并发**：`async_map` 有线程池，但调度器每轮只提交一条记录并等待完成，实际并发接近 1；`capacity` 只影响线程池大小，不代表 in-flight 并发。
- **CSV 混合列按 TEXT 处理**：一列里既有数字又有字符串时，`Schema` 会把整列推断为 `STRING`，SQL 比较按文本字典序，不会自动做数值转换。需要数值语义时请在 SQL 里 `CAST`。

## C.3 验证基线

在 `flink-tutorial` 目录执行：

```bash
bash verify.sh
```

或手动：

```bash
python -m pytest -q
for file in examples/*.py; do python "$file" >/dev/null || exit 1; done
python check_docs.py
```

期望：

- **156 个测试**全部通过；
- **22 个示例**退出码为 0；
- **41 章**、**16 个附录**，所有 README 链接可解析；
- `check_docs.py` 输出“文档检查通过”。

测试覆盖：

| 文件 | 覆盖 |
|---|---|
| `test_core.py` | 数据类型、窗口、Collector |
| `test_api.py` | API、union、side output、async |
| `test_watermarks.py` | 时间戳、watermark、迟到 |
| `test_windows.py` | 滚动/滑动/会话/全局、触发、驱逐 |
| `test_process_state.py` | 状态、TTL、定时器 |
| `test_join_broadcast.py` | window/interval join、广播 |
| `test_cep.py` | 序列、严格/非严格、within |
| `test_table.py` | SQL、Table API、窗口列、CTAS/VIEW AS SELECT |
| `test_sql_patterns.py` | 附录 O 子集：去重、窗口聚合、TopN、维表 join、CTAS 形状 |
| `test_connectors.py` | Kafka、文件、SQLite |
| `test_checkpointing.py` | 存储、协调器、恢复 |
| `test_runtime.py` | 图、指标、LocalCluster |
| `test_metrics.py` | 指标 |
| `test_cli.py` | SQL CLI |
| `test_audit_regressions.py` | 审计回归：keyed connect、窗口侧输出、链式 timer、trigger 清理、CLI 类型、TUMBLE 浮点、ValueState 快照、Kafka pending watermark、join 时间戳/迟到、CEP 循环、GlobalWindows；第二轮补充 CollectSink 恢复、processing timer 链、allowedLateness 不绕过 trigger、TimeEvictor 时间戳、MapState 快照、clock 注入、Async 函数按 subtask 隔离；第三轮补充窗口 join 迟到记录丢弃/`emitted` 清理、CEP 空 key 状态清理；第四轮补充窗口聚合快照、CEP `within` 清理 positions、GlobalWindows 事件时间 trigger、状态写入别名、open/close 生命周期、异构行与零列表校验 |

示例覆盖（可执行，不替代测试）：

| 示例 | 覆盖 |
|---|---|
| `examples/19_reconciliation_pipeline.py` | 去重、checkpoint 恢复与对账 |
| `examples/20_realtime_ai_pipeline.py` | 实时特征 + 模型打分 + 向量检索形状 |
| `examples/21_state_migration.py` | 离线读状态 → 修改 → 写回新快照 → 恢复（State Processor API 形状） |
| `examples/22_sql_patterns.py` | 去重、窗口聚合、TopN、维表 join、CTAS 形状（附录 O） |

## C.4 功能覆盖矩阵

| Flink 能力 | MiniFlink | 说明 |
|---|---|---|
| DataStream 转换 | 部分 | map/filter/flatMap/keyBy/reduce/aggregate/union/connect |
| 事件时间/watermark | 部分 | 有界乱序、多输入最小值；无 idleness/alignment |
| 窗口 | 部分 | 滚动/滑动/会话/全局、trigger/evictor 子集 |
| 键控状态 | 部分 | Value/List/Map/Reducing/Aggregating、TTL |
| 算子状态 | 部分 | 有存储类，未完整接入运行时 |
| 广播状态 | 部分 | 规则热更新形状 |
| Queryable State | 无 | 1.x 已 deprecated；见第 7 章 7.9 |
| Checkpoint | 部分 | 微批一致快照、内存/文件存储 |
| Savepoint | 部分 | 手动 checkpoint，无 canonical 格式 |
| 扩缩容/rescale | 无 | 恢复要求 task id 完全匹配 |
| 端到端 EOS | 无 | CollectSink 可恢复；Kafka/文件/SQLite 非事务 |
| SQL | 部分 | SQLite 有界表、SQL 子集、窗口列、CTAS/VIEW AS SELECT 物化子集 |
| SQL 生产模式 | 形状子集 | 去重/TopN/窗口列/维表 join 可在有界表运行；窗口 TVF/changelog/lookup/temporal join/Paimon 等流式语义不支持（附录 O） |
| 批执行模式 | 无 | `set_runtime_mode(BATCH)` 只记录配置，不改变执行语义 |
| CEP | 部分 | 序列/严格/非严格/within；无否定/optional/模式组 |
| Async I/O | 部分 | 线程池 + 顺序回收；无 in-flight checkpoint |
| 指标 | 部分 | Counter/Gauge/Meter、进程内 |
| 部署 | 模拟 | LocalCluster/slot；无 JVM/网络/HA |
| 安全/多租户 | 无 | 仅本地教学 |
| DataStream API V2 | 无 | 第 34 章讲生产设计 |
| ForSt/异步状态 | 无 | 进程内状态 + 全量快照 |
| 物化表/PTF | 无 | 第 34 章 |
| ML_PREDICT/VECTOR_SEARCH/Agents | 模拟形状 | 第 35 章与 examples/20 |
| changelog/FROM_CHANGELOG | 无 | SQLite 有界表 |
| Table 连接器 | 无 | `DynamicTableFactory`/pushdown/元数据列见附录 K |
| Catalog SPI | 无 | MiniFlink 只有内存表注册；生产 Catalog 见附录 K |
| State Processor API | 无 | 状态迁移/重写见附录 L |
| 连接器生态/升级矩阵 | 无 | 第 10 章 10.16 是生产速查 |

## C.5 已知限制

- 无真正反压和网络缓冲；
- 无算子链；
- 无 key group/rescale；
- 无空闲 watermark；
- 无自动故障注入/重启；
- 广播规则不保证在主线数据之前装载（并行度 > 1 时尤其明显）；
- 无事务 Kafka sink；
- 无 RocksDB/增量快照；
- Table SQL 不支持 changelog/窗口 TVF 完整语义；
- CTAS/`CREATE VIEW ... AS SELECT` 会把结果一次性物化到内存，没有 Catalog、外部存储、持续写入、schema 演进和原子建表；
- CEP 不支持否定/optional/模式组；
- 无安全/多租户；
- 没有 Table 连接器、Catalog SPI、State Processor API；附录 K/L 给的是生产实现方法，不是 MiniFlink 能力；
- `MapState` 的 TTL 按“状态名”整体计时，不按 entry 计时；
- 处理时间窗口用记录时间戳近似“当前时间”，没有独立系统时钟线程；
- 窗口 join 在窗口清理后到达的迟到配对会被丢弃（未实现 allowedLateness）；
- CEP 未配置 `within` 时，已存在的未完成部分匹配不会按时间自动清理（这是无界模式语义的一部分，不是泄漏）；
- 窗口快照保存带时间戳的 `Record`，因此 `ProcessWindowFunction` 仍只看到 value，但快照体积比只存 value 略大；
- 会话窗口合并时不会合并 `ProcessWindowFunction` 的 `window_state`；需要跨合并保留的状态要显式设计；
- SQLite 没有原生 `BOOLEAN`，表里以 `INTEGER` 存储；查询结果中的布尔列会以 `0/1` 返回，需要布尔语义时在 SQL 里显式比较/`CASE`，或在应用层按 schema 还原；
- 空输入且没有显式 schema 的数据流无法建表；`Table` 构造会直接报错，要求显式 `Schema`。

这些限制在第 28～33 章和正文相应章节均有说明。不要把 MiniFlink 的“绿灯”当成生产正确性证明。

## C.6 建议练习

1. 给 WindowOperator 加窗口状态 TTL；
2. 给 CollectionSource 加 idle watermark；
3. 给 TextFileSink 加 checkpoint 级提交；
4. 实现 key group 和 rescale；
5. 给 CEP 加超时侧输出；
6. 增加 Prometheus 文本 reporter；
7. 用多进程模拟 TM 和网络；
8. 对同一作业做 Flink/MiniFlink 结果对拍；
9. 实现 barrier 式 checkpoint（哪怕单线程）；
10. 实现 watermark alignment 的简化版；
11. 把 MiniFlink 的 VIEW 改成惰性求值（查询时重新执行，而不是建视图时物化）；
12. 给 MiniFlink 加一个最小的 Catalog，支持 CTAS 的 schema 推导和表生命周期。

## C.7 排障

- 先跑 `env.explain()` 看拓扑；
- 看 `execution.metrics()["tasks"]` 的 records/watermark；
- 看 `side_outputs`；
- 看 checkpoint 的 `task_states`；
- 用 pytest 最小复现；
- 异常会被包装为 `JobExecutionError`，注意 `__cause__`。

```python
from miniflink.core import JobExecutionError

try:
    env.execute()
except JobExecutionError as exc:
    print("cause:", exc.__cause__)
    print(env.last_execution.metrics())
```

## C.8 如何验证“测试没骗你”

MiniFlink 测试全绿不等于语义正确。建议：

1. 对关键语义写“反例测试”（迟到、乱序、空输入、重复 key）；
2. 与 Flink 的官方行为对照（文档/源码/实际跑）；
3. 对同一输入，比较 MiniFlink 与 Flink 的输出；
4. 检查测试是否把 bug 行为写成了断言；
5. 用随机化/属性测试覆盖边界。

## C.9 结论

MiniFlink 的价值是“可执行的架构图”和“可验证的语义”。它让读者在没有 JVM/分布式环境的情况下理解 Flink 的核心机制，但不能替代生产 Flink。把两者对照阅读，收益最大。
