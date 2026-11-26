# 第31章 MiniFlink：检查点、恢复与连接器

## 31.1 微批一致快照

MiniFlink 不在数据流动中插入 barrier，而是在每个微批结束时暂停。需要先说明配置边界：`CheckpointConfig` 里只有 `micro_batch_records` 真正影响执行；`interval_ms`、`timeout_ms`、`min_pause_ms`、`max_concurrent`、`mode` 是为了对齐 Flink 的配置形状而保留的**声明性字段**，当前运行时每个微批都会 checkpoint，不会按 interval 调度，也不会因为 `timeout_ms` 中止快照。

```python
def run_round(self):
    budget = self.checkpoint_config.micro_batch_records
    for task in self.source_tasks:
        produced = 0
        while produced < budget and not task.finished:
            element = task.operator.next_element()
            if isinstance(element, Record):
                produced += 1
            self._emit_from_source(task, element)
    self._drain()
    if self.checkpointing_enabled:
        self.checkpoint()
```

```text
source 读 50 条 → 排空所有通道 → 所有 task 静止
→ snapshot 全部 task → 写存储 → 继续下一批
```

这保证快照对应“同一批输入处理完”的时刻。代价是：

- 处理暂停，吞吐下降；
- 不适合生产；
- 没有 barrier 对齐/非对齐的概念。

但它把“一致切面”的核心思想暴露得很清楚：**快照时不能有 in-flight 数据**。

## 31.2 Task 快照内容

`RuntimeTask.snapshot()`：

```python
def snapshot(self):
    return {
        "operator": self.operator.snapshot(),
        "state": self.context.state_backend.snapshot(),
        "timers": self.context.timer_service.snapshot(),
        "records_in": self.records_in,
        "records_out": self.records_out,
    }
```

四部分：

1. **operator**：窗口 bucket、join 缓冲、CEP 部分匹配、广播状态、source offset；
2. **state**：KeyedStateBackend 的全部 key/value；
3. **timers**：事件/处理时间定时器 + 当前 watermark；
4. **metrics**：records in/out（用于恢复后统计）。

不同算子的 `snapshot()`：

| 算子 | 快照内容 |
|---|---|
| CollectionSource | offset、pending watermark、generator 状态 |
| KafkaSource | 各分区 offset、generator 状态 |
| WindowOperator | 所有 key/window bucket（含带时间戳的记录）、watermark |
| ProcessOperator | 定时器在 context 里，状态在 backend 里 |
| JoinOperator | 左右缓冲、watermark、emitted 集合 |
| IntervalJoinOperator | 左右缓冲、watermark |
| BroadcastCoProcessOperator | broadcast_state |
| CepOperator | partials、positions、watermark |
| CollectSink | 已收集的值 |
| TextFileSink | 空（文件在结束时原子出现） |

## 31.3 存储与恢复

`Checkpoint`：

```python
@dataclass
class Checkpoint:
    checkpoint_id: int
    timestamp_ms: int
    task_states: dict[str, dict[str, Any]]
    completed: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)
```

存储接口：

```python
class CheckpointStorage(Protocol):
    def save(self, checkpoint): ...
    def load(self, checkpoint_id): ...
    def latest(self): ...
    def list_checkpoints(self): ...
```

两种实现：

- `MemoryCheckpointStorage`：dict + deepcopy；
- `FileCheckpointStorage`：每个 checkpoint 一个 `chk-<id>.bin`，先写 `.tmp` 再 `os.replace`，保证原子可见。

```python
def save(self, checkpoint):
    tmp = self._path(checkpoint.checkpoint_id) + ".tmp"
    with open(tmp, "wb") as handle:
        pickle.dump(checkpoint, handle)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, self._path(checkpoint.checkpoint_id))
```

恢复：

```python
def restore(self, checkpoint):
    for task in self.tasks:
        task.restore(checkpoint.task_states[task.id])
    self.records_processed = checkpoint.metadata.get("records_processed", 0)
```

恢复要求 task id 完全匹配：

```python
missing = [task.id for task in self.tasks if task.id not in checkpoint.task_states]
if missing:
    raise JobExecutionError("检查点与当前作业图不兼容...")
```

恢复后还会把协调器的 `last_checkpoint_id` 对齐到已恢复快照，保证新 checkpoint 编号继续递增，不会覆盖存储中的旧快照。

这意味着 MiniFlink **不支持 rescale**：并行度变了，task id 变了，状态对不上。生产 Flink 用 key group 解决这个问题。

## 31.4 Exactly-once 的边界

MiniFlink 的“exactly-once”只覆盖：

- source offset；
- 算子状态；
- 定时器；
- CollectSink 的已收集值。

它不覆盖：

- 通用 sink 的外部副作用；
- KafkaSink 的事务；
- 文件 sink 的两阶段提交；
- 下游消费者的 read_committed。

```text
MiniFlink 能保证：恢复后内部状态一致
不能保证：外部系统不重复/不丢失
```

`KafkaSink` 只是把消息 append 到内存列表，`snapshot()` 记录 latest offset，但 `restore()` 不截断总线：

```python
def restore(self, snapshot):
    # 真实 KafkaSink 需要事务或幂等写入；这里只记录位置，不截断总线。
    pass
```

`TextFileSink` 在 `end_of_input` 时 flush + fsync + rename，保证文件原子出现；但作业中途失败会留下 `.tmp`，恢复后重新写，不是 2PC。

`SqliteSink` 按 batch commit，失败时已提交的批次不会回滚，最多做到 at-least-once。

## 31.5 文件连接器

`FileSource` 继承 `CollectionSource`，启动时把文件按行读入内存：

```python
with open(path, "r", encoding=encoding) as handle:
    lines = [line.rstrip("\n") for line in handle]
```

然后按 subtask 轮转切片。它适合批式回填，不适合大文件/持续追加。

`TextFileSink`：

```python
def open(self, context):
    self._handle = open(self._tmp_path, "w", encoding="utf-8")

def _write(self, value):
    self._handle.write(self.formatter(value) + "\n")

def end_of_input(self):
    self._handle.flush()
    os.fsync(self._handle.fileno())
    self._handle.close()
    os.replace(self._tmp_path, self.path)
```

语义：

- 写入期间下游看不到文件（`.tmp`）；
- 作业结束时原子 rename；
- 失败时 `.tmp` 残留，需要清理；
- 不支持 checkpoint 中途提交。

## 31.6 内存 Kafka

`InMemoryKafka`：

```python
class InMemoryKafka:
    def __init__(self):
        self._topics: dict[str, list[list[KafkaMessage]]] = {}
        self._committed: dict[tuple[str, str, int], int] = {}
```

- topic → partition → messages；
- `publish` 追加消息，offset = 当前长度；
- `read(topic, partition, offset)` 返回消息或 None；
- `commit/committed` 记录消费组 offset。

`KafkaSource`：

```python
self.partitions = [p for p in range(bus.partitions(topic))
                   if p % parallelism == subtask_index]
self.offsets = {p: bus.committed(group_id, topic, p) for p in self.partitions}
```

- 按 `p % parallelism` 分配分区；
- 每个分区维护 offset；
- `next_element` 轮转读取；
- 快照保存 offsets + generator 状态；
- 恢复后从 offset 继续。

```python
def snapshot(self):
    return {
        "offsets": dict(self.offsets),
        "final": self._final_emitted,
        "ended": self._ended,
        "generator": self.generator.snapshot() if self.generator else None,
    }
```

这演示了“offset 与状态同快照”的核心思想。它没有：

- 分区副本；
- 消费组协调；
- 事务；
- read_committed；
- 动态分区发现。

## 31.7 SQLite sink

```python
class SqliteSink(SinkOperator):
    def __init__(self, path, insert_sql, *, batch_size=100):
        ...
    def _write(self, value):
        self._connection.execute(self.insert_sql, params)
        self._pending += 1
        if self._pending >= self.batch_size:
            self._connection.commit()
            self._pending = 0

    def end_of_input(self):
        self._connection.commit()
        self._connection.close()
```

语义：

- 按 batch 提交；
- 失败时未提交的批次丢失；
- 已提交的批次无法回滚；
- 没有主键 upsert/事务表。

它适合演示 JDBC sink 的“批提交边界”，不是 exactly-once。

## 31.8 测试恢复

`tests/test_checkpointing.py` 覆盖：

- 内存/文件存储；
- checkpoint id 递增；
- 恢复后 source offset；
- 作业图不兼容检查（恢复缺任务时明确报错）；
- 手动 savepoint 与恢复后 id 连续；
- 离线编辑状态后恢复。

```python
execution = env.create_execution()
execution.run(max_rounds=1)
checkpoint = execution.checkpoint()
restored = env.create_execution()
restored.restore(checkpoint)
restored.run()
```

测试思路：

1. 跑一部分数据；
2. 做 checkpoint；
3. 新建执行图并恢复；
4. 跑剩余数据；
5. 断言结果与“不中断跑完”一致。

## 31.9 练习

1. 给 `KafkaSink` 加“恢复时截断到快照 offset”的逻辑，观察重复/丢失；
2. 给 `TextFileSink` 加两阶段提交（临时文件 + checkpoint 完成 rename）；
3. 实现 `FileCheckpointStorage` 的清理策略（保留最近 N 个）；
4. 给恢复加 task id 映射，支持简单的 rescale；
5. 写故障注入测试：在 checkpoint 前后抛异常，验证恢复结果。

## 本章小结

MiniFlink 的 checkpoint = **微批静止点 + 全 task 快照 + 原子存储 + task id 恢复**。它把“一致切面”讲清楚，但不提供生产级的分布式快照、事务 sink 和 rescale。

下一章进入 Table/SQL、CEP 与可观测性：看 SQLite 如何承载关系语义，CEP 如何维护部分匹配，指标如何暴露运行时状态。
