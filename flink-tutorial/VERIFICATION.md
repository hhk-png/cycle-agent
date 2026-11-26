# 验证记录

验证日期：2026-11-26  
环境：Windows 11，Git Bash，Python 3.14.6，pytest 9.1.1  
内容基线：Apache Flink 2.3（2026-06-25 发布），Java 17

## 命令

```bash
cd flink-tutorial
bash verify.sh
```

`verify.sh` 依次执行：

```bash
python -m pytest -q
for file in examples/*.py; do python "$file" >/dev/null; done
python check_docs.py
```

## 结果

```text
........................................................................ [ 46%]
........................................................................ [ 92%]
............                                                             [100%]
156 passed in ~0.5s
examples passed: 22
文档检查通过：41 章 / 16 个附录 / 22 个示例 / 156 个测试
```

## 覆盖范围

- **测试**：core/API/watermark/window/state/timer/join/broadcast/CEP/Table/SQL（含 CTAS/VIEW AS SELECT）/connector/checkpoint/runtime/metrics/CLI，以及示例 22 对应的 SQL 模式测试。
- **示例**：word count、事件时间窗口、会话窗口、ProcessFunction、广播状态、窗口 join、区间 join、CEP、checkpoint 恢复、Kafka、Table API、SQL、Async I/O、侧输出、并行度、文件管道、指标/计划、LocalCluster，以及示例 19 的去重/对账、示例 20 的实时 AI 形状、示例 21 的离线状态迁移和示例 22 的 SQL 模式子集。
- **文档**：41 章 + 16 个附录；README 中全部相对链接可解析；每章含“本章小结”。

## 内容结构

- 第一部分（1～4）：全景、运行时、DataStream、时间/watermark；
- 第二部分（5～9）：窗口、ProcessFunction、状态、checkpoint、恢复；
- 第三部分（10～13）：连接器、Kafka EOS、维表/Async I/O、CDC/湖仓；
- 第四部分（14～18）：Table/SQL、窗口 TVF、优化器、UDF/Gateway、流批一体；
- 第五部分（19～23）：双流 join、CEP/广播、内存/网络、监控、安全；
- 第六部分（24～27）：工程化、K8s/Operator、容量/成本、排障；
- 第七部分（28～33）：MiniFlink 实现；
- 第八部分（34～41）：2.x 深潜、实时 Data + AI、正确性工程、生态选型、端到端实战、商业分析 I～III；
- 附录 A～N：API、配置、MiniFlink 边界、术语、面试、参考、SQL 速查、源码地图、PyFlink、性能工程、Table 连接器/Catalog、状态迁移/State Processor API、REST 运维自动化、实时数仓建模。

## 此前迭代的主要变化（第一、二轮）

### 新增章节

- **第 34 章 Flink 2.x 深潜**：DataStream API V2（FLIP-408/409/410/433）、ForSt、异步状态访问、物化表、PTF、`FROM_CHANGELOG`/`TO_CHANGELOG`、1.x→2.x 迁移矩阵。
- **第 35 章 实时 Data + AI**：实时特征、`ML_PREDICT`、`VECTOR_SEARCH`、Flink Agents 0.4、LLM 数据管道、成本/延迟/幂等边界。
- **第 36 章 正确性工程**：不变量、数据质量、对账、回放/影子链路、幂等、故障注入、SLO/复盘。

### 新增附录

- **附录 G**：Flink SQL 语法、窗口 TVF、Join、函数、物化表、changelog 函数速查。
- **附录 H**：源码阅读与二次开发地图（StreamGraph/JobGraph/ExecutionGraph、checkpoint、状态、网络、SQL planner）。

### 新增示例

- `examples/19_reconciliation_pipeline.py`：重复事件 → 键控去重 → checkpoint → 恢复 → 对账。
- `examples/20_realtime_ai_pipeline.py`：窗口特征 → 本地“模型”打分 → 内存向量检索（教学形状，不调用真实模型）。

### 章节重构

- 原第 34～38 章顺延为第 37～41 章；第 37 章重写为“生态、选型与未来”，避免与 34/35 重复。
- 第 1、3、4、5、6、7、8、9、10、12、14、15、16、17、18、19、20、21、22、23、24、25、32、33 章补充/修正了 2.x 语义、配置、SQL 示例和交叉引用。
- 新增“全书依赖图”和六条阅读主线（时间/状态/工程/实现/前沿/性能）。

## 独立对抗性审计

### 文档与事实审计

上一轮对 41 章、8 个附录和示例做了一轮独立事实/一致性审计，确认并修复了以下高价值问题：

- `execution.failover-strategy: pipeline` 不是合法取值；改为 `jobmanager.execution.failover-strategy: region`（只支持 full/region）。
- `state.backend.local-recovery` 在 2.0 已移除；改为 `execution.checkpointing.local-backup.enabled` + `execution.state-recovery.from-local`。
- 删除 `RichSourceFunction` 示例（随 `SourceFunction` 在 2.0 移除），改用 `RichFlatMapFunction` + `OperatorStateStore`。
- State Processor API 示例从 `DataSet` 改为 `DataStream`/`SavepointReader`。
- `toChangelogStream` 返回 `DataStream<Row>`，不是 `Tuple2<Boolean, Row>`。
- `AsyncDataStream.orderedWait` 参数改为 `(stream, fn, timeout, TimeUnit, capacity)`。
- 移除不存在的全局 `watermark-alignment.max-drift`；改为 source `WatermarkStrategy.withWatermarkAlignment(...)` 或 SQL `scan.watermark.alignment.*`。
- Kafka SQL `scan.startup.mode` 从 `committed-offsets` 改为 `group-offsets`。
- `QUALIFY` 版本归属从 2.1 改为 2.0。
- 删除“窗口 size/slide 可由事件字段动态决定”的错误说法。
- 修正 Model DDL/`ML_PREDICT` 的 OpenAI endpoint/凭据/参数，以及 `VECTOR_SEARCH` 参数顺序。
- `taskmanager.network.batch-shuffle.compression` 改为 2.x 的 `taskmanager.network.compression.codec`。
- `state.backend.rocksdb.max-write-buffer-number` 改为 `state.backend.rocksdb.writebuffer.count`。
- 删除不存在的 `flink run --dry-run`，改为 `flink info` / `env.getExecutionPlan()`。
- Flink Agents 的治理描述改为“Apache Flink 子项目”，不是独立孵化器项目。
- 修正 temporal join 的 `window_time`、capstone 的 `proc_time`、加盐 join 的随机盐、HybridSource 语法、PromQL 指标、CEP Python 示例变量遮蔽等问题。
- 修正 `union` rescale、侧输出序列化、K8s `flinkVersion: v2_3`、MiniFlink trigger 状态等内部不一致。

### MiniFlink 语义审计

上一轮开始时 MiniFlink 的 124 个测试全部通过；继续用“反例/边界”方式审计，重点覆盖事件时间、窗口、状态、checkpoint、join、CEP、Table 和连接器，补充了 9 个回归测试后达到 133 个。审计确认并修复了以下语义问题：

- 键控 `connect` 的左侧没有按 key 哈希，导致同一 key 的状态被拆到不同 subtask；已改为 HASH，并补回归测试。
- 窗口 `ProcessWindowFunction`/`WindowFunction` 的侧输出复用了“最后一条数据记录”的 key/timestamp，且晚于 watermark 下发；已改为携带窗口 key、`window.end - 1`，并在 watermark 之前下发。
- `on_timer` 中注册的链式事件时间 timer 只弹出一次；已改为在同一 watermark 上循环排空。
- 窗口清理会绕过 trigger 强行发射（CountTrigger 等会多输出）；已改为先给 trigger 一次机会，返回 CONTINUE 时清理但不发射。
- 窗口 join 清理后仍会发射迟到配对；已保留 emitted token，清理后拒绝迟到配对。
- `WindowContext.window_state()` 每次触发都是空 dict；已按窗口 bucket 持久化，并纳入快照/恢复。
- `TUMBLE_START`/`TUMBLE_END` 对 REAL 时间戳走浮点除法；已显式 `CAST AS INTEGER`。
- CLI CSV 全部按字符串比较；已对明显整数/小数做类型推断（保留前导零）。
- processing timer 的侧输出会污染当前记录；已在触发 timer 前排空记录侧输出。
- `LocalCluster.submit(max_rounds=N)` 在未完成时错误标记 FINISHED；已按 `is_finished()` 设置状态。
- `ValueState.get()` 返回活引用；已改为深拷贝，原地修改不再隐式写入。
- interval join 的清理 cut-off 公式错误（右侧应使用 `watermark + lower`）；已修正。
- 广播规则“先装载”只是单 task 内部尽力而为，不再声称跨 subtask 确定性；已在附录 C 记录。

回归测试位于 `tests/test_audit_regressions.py`（9 个用例）。已知限制仍以[附录 C](appendix/C-miniflink-limits.md)为准：

- 单进程、无真正反压/网络；
- 无 key group/rescale；
- 无 idle watermark/alignment；
- 无事务 sink；
- 无 RocksDB/增量快照；
- SQL 是 SQLite 子集；
- CEP 只覆盖简单序列；
- `MapState` TTL 按状态名整体计时。

## 2026-11-26 细化修订

本轮以“章节结构不变、内容更准更完整”为原则，未新增或删除章/附录，重点修正事实与 API，并补强商业分析。

### 事实与版本修正

- 第 2 章：Per-job 模式已在 Flink 2.0 移除；`run-application` 已合并进 `flink run --target ...`（FLIP-464）。同步修正第 25 章与附录 A。
- 第 2 章：Kubernetes HA 使用 `kubernetes.cluster-id`，不再把 ZooKeeper 时代的 `high-availability.cluster-id` 写进 K8s 配置。
- 第 4 章：`pipeline.watermark-alignment.buffer-size` 默认值为 3，设为 0 恢复 2.2 行为（FLINK-37399），并补充其解决的问题。
- 第 8 章：非对齐 checkpoint 的规范键改为 `execution.checkpointing.unaligned.enabled`；附录 A/B 同步。
- 第 34 章：补充 2.3 恢复期 checkpoint 的两个开关（FLIP-547）。
- 第 1 章：补充 StateFun 的 sunset 状态（FLIP-569），避免把已停更项目当作新系统基础。
- 第 21 章：网络内存键改为 `taskmanager.memory.network.*`，说明旧键只是兼容别名。
- 第 18 章：批调度器改为 `jobmanager.scheduler: AdaptiveBatch`，并给出自动并行度配置。
- 第 15 章、附录 B：upsert materializer 策略键当时改为 `table.exec.sink.upsert-materialize.strategy`；第五轮核对 2.3 源码后确认应为 `table.exec.sink.upsert-materialize-strategy.type`，已再次修正。

### API 与代码示例修正

- 第 7 章：operator state 示例改为实现 `CheckpointedFunction`，从 `FunctionInitializationContext.getOperatorStateStore()` 获取状态；`RuntimeContext` 没有该方法。TTL 枚举改为 `OnReadAndWrite`。
- 第 12 章：异步函数继承 `RichAsyncFunction`（`AsyncFunction` 接口没有 `open/close`）。
- 第 17 章：UDF 生命周期改为 `open(FunctionContext)`，指标从 `FunctionContext.getMetricGroup()` 获取；删除重复的“Python 依赖管理”小节。
- 第 6 章：处理时间定时器恢复后对已过期时间戳立即补发，而不是重新排到未来。

### MiniFlink 语义修正

本轮对 MiniFlink 做了一次独立对抗性审计，并用公开 API 复现/回归了以下问题：

- `KafkaSource` 快照未保存微批边界上待下发的 watermark，恢复后会丢失一次 watermark；已把 `_pending` 纳入快照/恢复。
- 窗口 join 输出时间戳从 `window.end` 改为 `window.end - 1`，与 Flink 的窗口最大时间戳一致，避免下游窗口归属错位。
- interval join 现在丢弃低于当前 watermark 的迟到记录；输出时间戳取两侧记录中较大的事件时间。
- 窗口算子不再把每条迟到记录堆进内部列表；只有配置了侧输出才发送，checkpoint 不再随迟到量增长。
- 单步 `one_or_more` 现在会累积前缀（`[1]`、`[1,2]`、`[1,2,3]`），不再退化为逐条独立匹配。
- `GlobalWindows` 改为事件时间语义，默认 trigger 为 `NeverTrigger`；未显式设置 trigger 时不再在作业结束时强行输出。
- CSV/CLI：空表保留表头，不再生成空 DDL；类型推断改为按整列，混合列退化为 `STRING`，前导零不再被 SQLite 数值亲和性吞掉。
- 上一轮新增 9 个回归测试，测试总数从 124 变为 133；本轮又新增 7 个，当时 140。

仍存在的教学偏差（事件时间 `end` 与 `end-1` 的 1ms 边界、结束信号冲刷处理时间窗口、`ProcessWindowFunction` 状态无窗口 namespace、非键控 `ProcessFunction` 定时器不回调、Async I/O 实际并发接近 1）已写入[附录 C](appendix/C-miniflink-limits.md)。

### 商业分析补强

- 第 39 章新增 39.11：TAM/SAM/SOM 自下而上测算、敏感性分析与常见错误。
- 第 40 章新增 40.10/40.11：ACV、毛利、CAC、LTV、NRR 等单位经济模型，定价单位，以及建 vs 买的 NPV/敏感性决策。
- 第 41 章新增 41.10：一页纸商业决策备忘录模板与示例。

### 验证结果

```text
140 passed
examples passed: 20
文档检查通过：41 章 / 10 个附录 / 20 个示例 / 140 个测试
```

## 2026-11-26 第三轮：内容扩充与第二轮 MiniFlink 审计

### 章节与附录

- 新增**附录 I（PyFlink 工程手册）**：运行时形状、DataStream/Table API、五类 UDF、依赖分发、状态/checkpoint、性能模型、测试、限制与混合架构。
- 新增**附录 J（流处理性能工程与压测手册）**：问题分类、可复现基准、负载模型、指标口径、瓶颈决策树、Profiling、checkpoint/恢复压测、实验报告模板。
- 章节增补：
  - 第 1 章 1.10：动手环境与实验约定；
  - 第 2 章 2.9：Task 线程、Mailbox 与检查点协作；原检查清单顺延为 2.10；
  - 第 8 章：重排为 8.10 Checkpoint 生命周期与诊断、8.11 生产检查清单；
  - 第 11 章 11.10：Kafka 事务 fencing、幂等与多 sink 边界；
  - 第 21 章 21.10：反压的定量模型（Little 定律、buffer/credit 估算）；
  - 第 24 章 24.11：测试金字塔与 Flaky 治理；
  - 第 34 章 34.10：2.x 连接器与生态迁移清单；
  - 第 41 章 41.11：从试点到规模化的 90 天路线图；
  - 第 17 章增加 PyFlink 附录入口；第 30/31 章同步更新窗口记录/Evictor 的实现说明。
- README、附录 C/F、`check_docs.py` 的数量与标题校验同步：41 章 / 10 附录 / 20 示例 / 140 测试。

### 第二轮 MiniFlink 独立审计（反例驱动）

本轮不依赖“测试全绿”，而是用公开 API 写反例、先复现失败再修复。确认并修复了 7 个语义缺陷：

1. **CollectSink 恢复后丢输出**：`restore` 整体替换 `values`，而 `sink_function` 绑定的是旧列表的 `append`，恢复后的记录写进旧列表、对 `values` 不可见。改为原地切片赋值。
2. **链式 processing timer 不排空**：`on_timer` 中注册的、同一时刻已到期的 timer 只触发一次，后续要等下一条记录或作业结束。改为像事件时间 timer 一样循环排空（带 10,000 次保护）。
3. **allowedLateness 绕过 trigger**：窗口未触发但处于允许迟到区间时，任一迟到记录都会被无条件强制发射，`CountTrigger(5)` 在只有 3 条记录时也会输出。改为调用 `trigger.on_event_time`，由 trigger 决定是否 FIRE。
4. **TimeEvictor 对裸 value 失效**：窗口只存 value，`TimeEvictor` 读不到时间戳，等于不驱逐。改为 bucket 存 `Record`，触发时再取 value 交给用户函数。
5. **MapState.get 返回活引用**：原地修改会隐式写入状态，与 `ValueState`/`ListState` 的快照语义不一致。改为 `get/items/keys/values` 返回深拷贝，写入必须显式 `put`。
6. **RuntimeContext.processing_time 忽略注入 clock**：构造时传入的 clock 只影响 TTL，不影响 processing timer 的“当前时间”，测试不可确定复现。改为优先使用注入 clock。
7. **Async 函数实例跨 subtask 共享**：`async_map` 的 factory 未 clone 用户函数，多个 subtask 可能共享可变状态。改为与其他算子一致，每个 subtask 一份实例。

另外做了一项状态卫生修复：窗口 join 在 watermark 清理后移除空 key 字典，避免长期运行时堆满空节点。

**一个被否定的“修复”**：审计初期怀疑 `CountTrigger` 用 `count % N` 而不是 `count >= N` 是 bug；对照 Flink 实现后确认 `CountTrigger` 在触发后会清零计数，因此按 N 的倍数触发是正确语义，未改动，并由 `test_count_evictor_after_fire_keeps_current_records` 固定。

新增 7 个回归测试（`tests/test_audit_regressions.py`），原有 133 个测试在修复后全部保持通过，当时为 140 个。

### 验证结果

```text
140 passed in ~0.3s
examples passed: 20
文档检查通过：41 章 / 10 个附录 / 20 个示例 / 140 个测试
```

## 2026-11-26 第四轮：连接器/Table API/时区/湖仓/状态预算与长期状态卫生

本轮以独立结构审计的结果为依据，保留 41 章 / 10 附录的总体结构，补强“资深工程师会问、但原文只一笔带过”的主题，并修复 MiniFlink 的两个长期状态增长点。

### 章节增补

- **第 10 章 10.13 自定义 Source/Sink V2 连接器开发**：FLIP-27 四对象、分片发现/分配、枚举器与 reader 状态、rescale、Sink V2 的 write/pre-commit/commit/abort、幂等与测试/打包检查表。
- **第 10 章 10.14 Schema 契约、序列化与兼容性工程**：事件/序列化/状态三层契约、Avro/Protobuf/JSON Schema 兼容性、Schema Registry 策略、CDC DDL、坏数据与兼容性测试矩阵。
- **第 13 章 13.9 Paimon 合并引擎与湖仓写入模式**：primary-key/append 表、`deduplicate`/`partial-update`/`aggregation`/`first-row`、sequence field/sequence group、changelog-producer、compaction 与适用边界。
- **第 14 章 14.7 Table API 流式 DSL**：`Table` 惰性语义、`select/where/groupBy/window/join`、与 DataStream/changelog 互转、Table API vs SQL 的取舍，补齐章节标题承诺的 Table API 部分。
- **第 14 章 14.12 时间类型、时区与夏令时**：`TIMESTAMP` vs `TIMESTAMP_LTZ`、`table.local-time-zone`、epoch 转换、DST gap/overlap、窗口/分区/对账的统一时区规则。
- **第 16 章 16.10 SQL 算子状态布局与状态预算**：GroupAggregate/窗口/去重/TopN/Regular Join/Interval Join/Temporal Join/Lookup/SinkUpsertMaterializer 的状态来源、TTL 边界、预算表与调优顺序。
- **第 18 章 18.6 批模式的恢复、输出提交与幂等边界**：三种恢复路径、shuffle 复用、覆盖写/upsert/append、外部副作用；并删除原 18.2 中重复的容错段落。
- **第 18 章 18.9 批执行的内核**：stage/blocking exchange、批算子差异、AdaptiveBatch、资源与恢复、调优顺序。
- **第 19 章 19.10 Temporal Join 的两种形态**：事件时间版本表 vs 处理时间 lookup join、DDL/状态/可重放性/缓存/hint 与常见错误。
- **第 20 章 20.9 CEP 匹配后跳过策略**：`AfterMatchSkipStrategy` 五种策略、重叠匹配对状态/告警的影响、输出治理与测试。
- **第 27 章 27.13 诊断决策树与故障演练**：作业起不来/checkpoint/反压/OOM/数据正确性五棵决策树、排障禁忌与故障演练矩阵。
- 连续性修订：为第 7/12/14/15/17/21/22/23 章的 MiniFlink 小节补上实现章节/附录 C 指针；成本算例标注“各章独立基线，不可跨章相加”；VERIFICATION 的阅读主线改为六条（含性能线）；`check_docs.py` 增加 Markdown 锚点校验。

### 事实与 API 修正（独立事实审计）

- 第 9 章：operator state 的重新分配模式改为 `SPLIT_DISTRIBUTE`/`UNION`/`BROADCAST`，明确 `FORWARD` 是流分区而不是状态模式；补充 V2 的 `NONE`/`REDISTRIBUTABLE`/`IDENTICAL`。
- 第 25 章、附录 B：删除不存在的 `kubernetes.taskmanager.memory.process.size`，改用 `taskmanager.memory.process.size`。
- 第 25 章：`FlinkStateSnapshot` 改为 `jobReference.kind` + `savepoint: {}`，去掉不存在的 `spec.type`。
- 第 25 章：Operator autoscaler 改为 `flinkConfiguration` 下的 `job.autoscaler.*` 扁平键，去掉不存在的嵌套 `job.autoscaler` 对象和 camelCase 字段。
- 第 34 章：DataStream API V2 状态示例改用 `TypeDescriptors.LONG`、`getState(...).orElseThrow()` 和 `PartitionedContext`。
- 第 34 章、附录 G：物化表 `START_MODE = FROM_NOW` 改为未加引号的关键字形式。
- 第 35 章：`ML_PREDICT` 命名参数改为 `MODEL => MODEL risk_scorer`。
- 第 1/3/7 章：Kryo 升级归属改为 Flink 2.0（FLIP-317，2.24→5.x），2.2 沿用，不再写成 2.2 引入。
- 第 22 章：`numRecordsInPerSecond` 不再套 `rate()`；`numRestarts` 由 `increase(_total)` 改为 gauge 查询。

### MiniFlink 语义修复（第三轮独立审计）

用直接构造算子的反例复现，而不是只依赖“测试全绿”：

1. **窗口 join 迟到记录仍入状态**：`process_record` 在 watermark 已越过窗口结束时仍把记录写入 left/right 缓冲，直到下一个 watermark 才清理；同时 `emitted` token 永久保留。改为入状态前丢弃已过期窗口，并在 watermark 推进后清理已越过窗口的 token；`end_of_input` 清空 token。
2. **CEP 空 key 状态**：`partials.setdefault(key, [])` 与 `positions[key]` 对每个见过的 key 都留下条目，永不匹配的 key 会让状态随基数增长。改为在没有未完成匹配时同时清理 `partials` 与 `positions`。

新增 2 个回归测试（`tests/test_audit_regressions.py`），总数从 140 变为 142。附录 C 已移除这两条“已知限制”，并更新测试说明。

### 验证结果

```text
142 passed in ~0.4s
examples passed: 20
文档检查通过：41 章 / 10 个附录 / 20 个示例 / 142 个测试
```

## 2026-11-26 第五轮：SQL 数据管道、批执行尾部延迟与延迟预算

本轮保持 41 章 / 10 附录的结构，补强三个此前只有一句话、但生产上经常被追问的主题，并把新能力落到可运行的 MiniFlink 子集。

### 章节增补

- **第 7 章 7.9 Queryable State**（新增）：说明 1.x 的 `asQueryableState`/`QueryableStateClient` 为何在 1.18 被标记 deprecated、与内部状态布局/权限/扩展性的冲突，以及外部 KV、物化表、lookup join、State Processor API 等替代方案；原 7.9 检查清单顺延为 7.10。
- **第 14 章 14.4 多路输出**：补全 `StatementSet` 的 Table API 写法、同一 JobGraph/checkpoint/失败域、跨 sink 事务边界，以及“何时应该拆作业”。
- **第 14 章 14.9 CTAS、RTAS 与数据管道**（新增）：CTAS 的 schema 推导、Catalog 建表、流批语义、非原子默认行为与 `SupportsStaging` 原子建表、RTAS 风险、CTAS 与 Flink CDC Pipeline/CDAS 的边界、生产检查清单。原 14.9～14.12 顺延为 14.10～14.13，并同步第 4 章的节引用。
- **第 18 章 18.12**（新增）：批作业尾部延迟的两种手段——推测执行（`execution.batch.speculative.*`、慢任务检测、重复计算与 sink 幂等）和混合 shuffle（`execution.batch-shuffle-mode`、Celeborn 远程 tier、2.x legacy mode 移除）。
- **第 22 章 22.2**（新增小节）：Flink 内置 `latency` 追踪的 marker 机制、`metrics.latency.*` 配置、三种粒度，以及“marker 绕过用户函数/不是端到端延迟”的四个边界。
- **第 26 章 26.12**（新增）：端到端 P99 延迟预算，区分事件/摄取/处理/可见延迟，给出 5 秒预算拆分、排队论与 Little 定律、不该计入处理延迟的时间、度量方法和优化顺序；原 26.12 商业视角顺延为 26.13。

### MiniFlink 增强

- `TableEnvironment.execute_sql` 支持 `CREATE [OR REPLACE] {TABLE|VIEW} [IF NOT EXISTS] name AS SELECT ...`：查询结果物化为内存表并注册；重复建表在缺少 `IF NOT EXISTS`/`OR REPLACE` 时报错；无 `AS` 的 DDL 给出明确的教学边界错误。
- 新增 2 个测试：CTAS 注册/查询/重复建表冲突，VIEW 创建/替换与 DDL 边界；测试总数 142 → 144。
- `examples/12_sql_analytics.py` 增加 CTAS 演示；附录 C 更新能力矩阵、已知限制和练习。

### 事实与配置修正

- **附录 B**：按 Flink 2.3 源码修正 upsert materializer 配置：`table.exec.sink.upsert-materialize` 控制是否物化（`AUTO`/`NONE`/`FORCE`），`table.exec.sink.upsert-materialize-strategy.type` 控制实现策略（`LEGACY`/`MAP`/`VALUE`/`ADAPTIVE`）；同步修正第 13、15 章。新增批执行（shuffle/推测）配置表，以及 B.12 的延迟追踪/推测执行/混合 shuffle/CTAS 原子性条目。
- **CTAS/RTAS 版本与键**：CTAS 为 FLIP-218（1.16+），RTAS 为 FLIP-303（1.18+）；核对 1.18 与 2.3 源码，原子性键均为 `table.rtas-ctas.atomicity-enabled`，正文与附录 A/B 已统一，不再引用未经证实的旧键。
- **附录 F**：补充 FLIP-218/305/463（CTAS）、FLIP-168（推测执行）、FLIP-459（Celeborn）参考。
- 明确 MiniFlink 的 `set_runtime_mode(BATCH)` 只记录配置、不改变执行语义（第 18 章与附录 C）。

### 验证结果

```text
144 passed in ~0.4s
examples passed: 20
文档检查通过：41 章 / 10 个附录 / 20 个示例 / 144 个测试
```

## 2026-11-26 第六轮：Table 连接器/Catalog、状态迁移与工程深化

本轮以“补齐资深工程师会追问、但原结构没有承载位置”的主题为目标，保留 41 章不动，新增 2 个附录，并在现有章节末尾追加 15 个小节；同时用独立审计核对 2.x 新特性的 API/配置。

### 新增附录

- **附录 K（Table 连接器与 Catalog 开发手册）**：`DynamicTableFactory` 工厂发现、`ScanTableSource`/`LookupTableSource`、`DynamicTableSink`/Sink V2、filter/projection/limit/partition pushdown、元数据列、Format/类型映射、Catalog SPI/CatalogStore、测试与打包。填补了原书“DataStream 连接器有、Table 连接器没有”的空档。
- **附录 L（状态迁移、Savepoint 兼容与 State Processor API）**：状态的四层身份（逻辑结构/序列化/分区布局/物理格式）、UID、savepoint 规则、maxParallelism/key group、序列化器快照、State Processor API 读写/bootstrap、1.x → 2.x 迁移、演练手册与故障表。

### 章节增补

- 第 2 章：2.3.1 资源规划算例、2.4.1 Docker/Standalone、2.5.1 HA 内部组件与 fencing；
- 第 8 章：8.12 Checkpoint 存储的内部结构与保留策略；
- 第 9 章：9.12 灾难恢复与备份；
- 第 10 章：10.15 Table 连接器边界、10.16 连接器生态矩阵与升级路径；
- 第 14 章：14.14 Catalog SPI 与元数据治理；
- 第 17 章：UDF 类型速查表、17.13 SQL Client/JDBC Driver/Gateway 落地；
- 第 22 章：22.12 OpenTelemetry trace 的传播、采样与日志关联；
- 第 23 章：23.14 Kerberos/SASL/TLS 的深度排障；
- 第 24 章：24.12 类加载与依赖隔离、24.13 SQL 与连接器测试；
- 第 25 章：25.11 生产检查清单；
- 第 34 章：34.11 `VARIANT`/`PARSE_JSON`、Delta Join（FLIP-486）、StreamingMultiJoin（FLIP-516）的工程细节。

### MiniFlink 增强

- 新增 `examples/21_state_migration.py`：跑一条记录 → 取 checkpoint → 定位 keyed state → 把计数从 1 改成 100 → 写回新快照 → 恢复并验证后续输出为 101/102。它是附录 L 的可执行注脚。
- 新增 `test_offline_state_edit_and_restore` 回归测试，覆盖“离线改状态后恢复”的语义。

### MiniFlink 语义审计（第四轮反例驱动）

独立审计用公开 API 复现并修复了 7 个语义缺陷，并补了 7 个回归测试（`test_audit_regressions.py`）：

1. **窗口聚合结果别名 accumulator**：`get_result` 合法地可以直接返回 accumulator；发射后迟到记录会改写已发出的结果。已在发射时深拷贝。
2. **CEP `within` 清理漏删 positions**：watermark 清理只删 `partials`，每个见过的 key 永久留下位置计数。已同步删除 `positions`。
3. **GlobalWindows + EventTimeTrigger 永不触发**：`process_watermark` 按 `assigner.is_event_time` 直接返回，导致显式事件时间 trigger 收不到 MAX_WATERMARK。已让 GlobalWindows 走事件时间路径，并在 `end_of_input` 做 processing-time 兜底。
4. **状态写入路径别名**：`ValueState.update`/`ListState.add`/`MapState.put`/`OperatorListState.add` 存调用方对象引用，写后修改原对象会改状态；`AggregatingState.get` 返回活 accumulator。已在写入/读取两侧深拷贝。
5. **open/close 生命周期不完整**：Reduce/Aggregate/CoProcess/Broadcast/Async 算子只调用 `open` 不调用 `close`，Async 还漏了 `open`。已补齐。
6. **异构 dict 行静默丢列**：`Schema.from_rows` 只看首行，后续行多出的字段被丢弃。已改为建表时报错。
7. **零列输入延迟到 SQL 报语法错**：空输入/无 schema 会生成 `CREATE TABLE t ()`。已在 `Table` 构造时给出明确错误。

仍保留一个已记录的限制：SQLite 没有原生 BOOLEAN，查询结果里的布尔列会以 0/1 返回（见附录 C）。测试总数 144 → 152（含此前新增的离线状态迁移用例）。

### 事实校准

本轮独立事实审计确认并修复了以下问题：

- **物化表版本归属**：`CREATE MATERIALIZED TABLE` 由 FLIP-435 在 **Flink 1.20** 引入，不是 2.0；2.2/2.3 只是扩展（可选 `FRESHNESS`、`DISTRIBUTED BY`、`START_MODE`）。已修正第 1、14、16、34 章与附录 A/D。
- **FROM_CHANGELOG/TO_CHANGELOG**：2.3 只覆盖基础用法；`PARTITION BY`、`invalid_op_handling`、`produces_full_deletes` 在 2.4 才逐步就绪。参数名是 `invalid_op_handling`，不是 `error_handling`。已从 2.3 示例移除这些子句。
- **CTAS/RTAS**：FLIP-463 是 **2.0** 特性，支持 `PARTITIONED BY`、`DISTRIBUTED BY` 和在 CREATE 部分声明 `NOT NULL`；原“不能创建分区表/CREATE 不能声明 NOT NULL”的说法已删除。
- **2.x Schema API**：`TableSchema` / `CatalogTable.of` 已移除，Catalog 示例改为 `Schema.newBuilder()` + `CatalogTable.newBuilder()`（第 14 章、附录 K）。
- **虚构的 RocksDB 旧键**：删除 `state.backend.rocksdb.max-write-buffer-number` 这一从未存在的“旧键”；RocksDB 原生 `max_write_buffer_number` 一直对应 `state.backend.rocksdb.writebuffer.count`。
- **Kafka 有界读**：`scan.stop.timestamp` 不存在；改用 `scan.bounded.mode` + `scan.bounded.timestamp-millis` / `scan.bounded.specific-offsets`。
- **Table 分区下推**：接口是 `SupportsPartitionPushDown`，方法是 `listPartitions()` / `applyPartitions(List<Map<String,String>>)`；`Optional.empty()` 属于 `listPartitions`，不是 `applyPartitions` 参数。
- **算子状态重分配**：没有 `ListStateDescriptor.setAssignmentMode`；模式由 `getListState`/`getUnionListState`/`getBroadcastState` 决定。附录 L 已改。
- **Elasticsearch 方向**：官方 connector 只有 sink，没有 source；矩阵已改。
- **Delta Join 描述**：不是“复用 source 状态”，而是用外部索引点查替代两侧完整 join 状态；第 16、19 章与附录 E 已对齐 34.11.2。
- **FlinkStateSnapshot**：`jobReference.kind` 必须与被引用资源一致；示例中的 `orders-job` 是 `FlinkSessionJob`，已改。
- **RocksDB 托管内存**：`memory.managed: true`（默认）时，`block.cache-size` / `writebuffer.size` 绝对值被忽略；第 7、21 章与附录 B 已注明，要么关闭 managed memory，要么调 ratio。
- `VARIANT` 取值按 FLIP-521：`PARSE_JSON`/`TRY_PARSE_JSON`、`VARIANT_VALUE`/`VARIANT_QUERY`、`TO_JSON`；
- Delta Join 配置按 FLIP-486 的发布形态：`table.optimizer.delta-join.strategy`（AUTO/FORCE/NONE）、`table.exec.delta-join.cache-enabled`、`table.exec.delta-join.left.cache-size` / `right.cache-size`（FLIP 草案里的全连字符写法不是发布配置名）；
- StreamingMultiJoin 配置按 FLIP-516：`table.optimizer.multi-join.enabled`、`table.optimizer.multi-join.max-tables`、`MULTI_JOIN` hint；
- 连接器矩阵明确标注“以连接器版本文档为准”，不把典型能力写成绝对承诺。

### 验证结果

```text
152 passed in ~0.5s
examples passed: 21
文档检查通过：41 章 / 12 个附录 / 21 个示例 / 152 个测试
```

## 2026-11-26 第七轮：REST 运维自动化与实时数仓建模

本轮保持 41 章、21 个示例、152 个测试不变，新增 2 个附录，并在第 13、14、22、25、27 章补上建模与运维的衔接段落。目标不是再增加 API 清单，而是补齐两个此前分散在章节里、没有独立承载位置的主题：

- **控制面**：作业提交、savepoint、停止、升级、回滚、审计如何从手工操作变成可重试的平台 API；
- **数据模型**：事实粒度、维度版本、SCD、可加指标、迟到/更正和层间对账如何决定实时链路的正确性。

### 新增附录

- **附录 M（Flink REST API 与作业运维自动化）**：控制面/数据面边界、2.x REST 变化、端点地图、异步 savepoint 轮询、stop-with-savepoint 的 `drain` 语义、Python 标准库客户端、deployment/operation 平台模型、安全与 Runbook。明确记录 Flink 2.0 已移除在线 rescale REST 端点，不能用 savepoint 轮询脚本替代发布控制器。
- **附录 N（实时数仓建模手册）**：ODS/DWD/DWS/ADS/DIM 契约、事务/周期快照/累积快照事实表、SCD1/2/3、事件时间与版本化维表、Flink SQL 模板（ODS、去重、as-of 打宽、窗口 DWS、ADS、当前/历史维表）、宽表与星型取舍、状态成本、数据质量与订单案例。

### 章节衔接

- 第 13 章新增 13.11“从管道到模型”，原检查清单顺延为 13.12；
- 第 14 章 Catalog 小节增加实时建模入口；
- 第 22、25、27 章分别从可观测性、Operator、排障角度链接附录 M；
- 第 1 章、附录 C、附录 F、README 的附录计数与阅读路线同步为 14 个附录。

### 事实与版本校准

- 核对 Flink 2.0 REST 变更：在线 rescale 端点已移除；`terminationMode` 被移除；Jar 请求使用 `programArgsList`；stop 使用独立端点；
- 明确 2.x REST 字段风格并不完全统一：savepoint 触发仍用 `target-directory`/`cancel-job`，stop 用 `targetDirectory`/`drain`，避免脚本按“全部 camelCase”猜测；
- savepoint 触发返回 `request-id`，必须轮询到 `COMPLETED` 并取得 `location`，不能把 202 当作完成；
- `drain=true` 不是“更安全的暂停”：它会推进 watermark 并可能让 source 消费位置前移，发布暂停应使用 `drain=false`；
- 建模部分只使用稳定的维度建模概念与 Flink SQL 语法；连接器选项、版本化表主键限制等均标注“以发行版为准”，不把 Paimon/Kafka 的典型能力写成绝对承诺。

### MiniFlink 与测试

本轮不修改 MiniFlink 代码和测试；新增内容属于生产接口与建模方法，MiniFlink 的边界仍以附录 C 为准。`check_docs.py` 的附录常量从 12 改为 14，并继续校验 README 链接、锚点、占位符和章节小结。

### 验证结果

```text
152 passed
examples passed: 21
文档检查通过：41 章 / 14 个附录 / 21 个示例 / 152 个测试
```

## 2026-11-26 第八轮：SQL 生产模式库与作业交付治理

本轮保留 41 章不动，新增 2 个附录、1 个可运行示例和 4 个测试，并修正 Delta Join 在两处文档中的陈旧描述。目标不是再罗列 API，而是补齐“知道语法之后，生产上到底怎么写、怎么发”的两层空白：

- **写法层**：去重、TopN、窗口聚合、lookup/temporal join、CDC 入湖、物化表、changelog、VARIANT、MATCH_RECOGNIZE、STATEMENT SET、死信分流、性能 hints 的模式、状态与反模式；
- **交付层**：版本坐标、变更分类、兼容性门禁、发布策略、标准升级 Runbook、回滚决策、灰度切流、变更冻结、RACI 与模板。

### 新增附录

- **附录 O（Flink SQL 生产模式库）**：13 类生产模式，每类按“问题 → SQL → 语义与状态 → 生产坑 → 何时不要用”展开；包含状态预算公式、模式选择决策树和 14 条反模式。明确区分“SQL 形状”与“流式语义”，并标注窗口 TVF、物化表、FROM/TO_CHANGELOG 的版本边界。
- **附录 P（作业交付与变更管理手册）**：六个交付坐标、变更风险矩阵、UID/状态/key group/连接器/配置/SQL 计划六类门禁、滚动/savepoint/蓝绿/金丝雀/影子/双跑六种发布策略、七阶段升级 Runbook、代码/状态/数据三种回滚、灰度切流、变更冻结与审批、发布后四类验证、多作业发布顺序、事故分级与复盘模板、RACI 与六类失败案例。

### 可执行新增

- **`examples/22_sql_patterns.py`**：用 MiniFlink 的有界 Table 层演示按主键去重、`TUMBLE_START` 窗口聚合、TopN、维表 join 与 `CREATE VIEW ... AS SELECT` 物化形状，并对每一步结果做断言。
- **`tests/test_sql_patterns.py`**：4 个测试分别覆盖去重保留最新、窗口聚合、TopN 排序、维表 join + 物化视图；测试总数 152 → 156。
- 示例总数 21 → 22；附录总数 14 → 16；README、`check_docs.py`、第 1 章、附录 C/F 的计数同步更新。

### 事实与一致性修正

- 统一 Delta Join 的描述：它是“用外部索引表双向点查替代两侧完整 join 状态”，不是“复用 source 状态”；第 34 章 34.6 与附录 D 术语表已对齐第 16、19、34.11.2 章。
- 附录 O 对 2.3 的 `FROM_CHANGELOG`/`TO_CHANGELOG` 只写基础用法，不把 2.4 的 `PARTITION BY`/`invalid_op_handling`/`produces_full_deletes` 写成 2.3 能力；物化表仍标注为 1.20 引入、2.2/2.3 扩展。

### 章节衔接

- 第 9 章：恢复链路后链接附录 P 的发布/回滚边界；
- 第 14、15、16、19 章：SQL 语义与优化之后链接附录 O 的生产模式；
- 第 24、25、27 章：工程化、Operator、排障之后链接附录 P 的交付治理。

### 事实与一致性校准（本轮三路独立审计）

三路只读审计分别覆盖第 1～13 章、第 14～27 章、第 28～41 章与 MiniFlink 代码，共确认并修复以下高价值问题：

- **watermark 与 checkpoint**：删除“watermark 从 checkpoint 继续/恢复”的错误说法；watermark 本身不持久化，恢复后由 source 重放重新生成（第 4、5 章）。
- **State Processor API**：补上 `RuntimeExecutionMode.BATCH` 与 `SavepointReader.read(env, path, backend)` 三参形式；示例不再是无法运行的 DataSet 时代写法（第 7 章）。
- **Table 连接器 API**：`FactoryUtil.TableFactoryHelper` 没有 `discoverOptions()`，改为 `getOptions()`；自定义 `Source` 明确说明必须补 `getSplitSerializer()`/`getEnumeratorCheckpointSerializer()`（第 10 章、附录 K）。
- **批 shuffle 配置**：从 `execution.batch-shuffle-mode` 删除非法值 `POINTWISE_EDGES_PIPELINED`，它属于 `table.exec.shuffle-mode` 的旧词汇（第 18 章、附录 B）。
- **CDAS 归属**：改为“阿里云 VVR 扩展、退役中；开源 Flink CDC 3.x 走 YAML pipeline”，并补充 Iceberg/Hudi/Delta 的提交/compaction 差异（第 13 章）。
- **窗口 API**：`SessionWindows` 改为 `EventTimeSessionWindows`；补充动态 gap 与 `canMerge()`/`onMerge()`；把名不副实的 `EarlyFireTrigger` 改为 `EndOfWindowTrigger`（第 5 章、附录 A）。
- **窗口 TVF 早触发**：明确 TVF 不支持 legacy `table.exec.emit.early-fire.*`，并删除第 26 章与附录 O 中“用 early fire 优化 TVF”的错误建议（第 15、26 章、附录 O）。
- **Table API 表别名**：删除不存在的 `Table.as("o")` 用法，改为 join 前重命名唯一列；`Catalog.getTable` 参数改为 `ObjectPath`；`CatalogStore` 明确只存 Catalog 配置（第 14 章、附录 K）。
- **SQL 语法**：`CREATE FUNCTION ... USING ARTIFACT`、物化表列/主键/`FRESHNESS` 顺序、`START_MODE` 的 2.4/厂商边界、SQL Gateway `executionTimeout`、`VECTOR_SEARCH` 官方参数顺序（查询向量在前）、窗口 TVF `GROUP BY window_start, window_end`、`consume_full_deletes`/`produces_full_deletes` 的区分、`AFTER MATCH SKIP` 的空格形式（第 14、15、17、34、35、36 章、附录 G/O）。
- **MiniFlink 文档与代码**：`StateTtlConfig` 是 frozen dataclass、checkpoint 配置只有 `micro_batch_records` 生效、`LoggingReporter` 只写内存 history、`BYTES` 映射补 `BLOB`、广播状态用 `MapStateDescriptor` + `items()`、算子状态未接入运行时、restart strategy 不驱动自动重启（第 29～33 章）。
- **商业算例**：修正 SOM 量纲、补第 38 章单价与合计、标注第 40/41 章币种/基线不可直接比较（第 38～40 章）。

另有一路独立复核专门检查新增附录 O/P、示例 22 与测试，修正了 `MATCH_RECOGNIZE` 的 `AFTER MATCH SKIP` 空格形式、物化表 `START_MODE` 的 2.4/厂商边界、`VECTOR_SEARCH` 的官方参数顺序、TopN 查询/测试依赖未定义顺序，以及回滚流程中“savepoint 已含 offset 又手工重放”的重复消费风险。

### 验证结果

```text
156 passed
examples passed: 22
文档检查通过：41 章 / 16 个附录 / 22 个示例 / 156 个测试
```

## 已知验证盲区

- 测试是单进程实现的行为测试，不等价于 Flink 分布式语义验证；
- 没有对真实 Kafka、RocksDB、Kubernetes 做集成测试；
- Table SQL 是 SQLite 子集，测试不覆盖 Flink 流式 SQL/changelog；
- CEP 只覆盖简单序列；
- 没有性能/容量压测；附录 J 提供的是可复现的方法与模板，本轮未在真实集群执行；
- 附录 I 的 PyFlink 代码是文档示例，**未在 PyFlink 运行时执行**（本机未安装 PyFlink；PyFlink 支持的 Python 版本与 MiniFlink 的 3.14 验证环境不同），具体 API/配置以所用版本文档为准；
- `check_docs.py` 校验数量、链接/锚点、占位符和“本章小结”，**不编译 Java 片段、不校验 SQL 语法、不校验配置键是否存在**；
- 附录 M 的 Python REST 客户端只做了语法编译检查，**未对真实 Flink 集群执行**；不同版本的请求体字段、错误码和异步响应结构以发行版文档为准；
- 附录 N 的 Flink SQL 是建模模板，**未在真实 Flink/Paimon 集群编译执行**；连接器能力、主键/水位线限制和对象存储配置需按发行版验证；
- 附录 O 的 SQL 是生产写法参考，**未在真实 Flink 集群逐条编译执行**；示例 22 只验证 MiniFlink 的 SQLite 有界表子集（去重/TopN/窗口列/join/物化形状），不验证窗口 TVF、changelog、lookup/temporal join、Paimon、VARIANT 与 `MATCH_RECOGNIZE` 的流式语义；
- 附录 P 是发布与变更流程手册，**不是可执行代码**；其中的命令、REST/Operator 字段和保留策略需按所用 Flink/Operator 版本核对；
- `START_MODE` 属于 FLIP-557 的 2.4 路线/厂商方言，**2.3 开源语法没有**；Confluent/VVR 等方言的取值与行为未在本仓库逐一验证；
- `VECTOR_SEARCH` 的命名参数形式在 Flink 2.2 文档可用，但厂商方言（如 VVR）的参数顺序/额外参数不同；示例未在真实向量表上执行；
- 商业/成本数字为教学示意，不构成采购或投资建议。

这些边界在正文和[附录 C](appendix/C-miniflink-limits.md)中明确说明。生产环境请以 Apache Flink 的官方测试、故障注入和压测为准。
