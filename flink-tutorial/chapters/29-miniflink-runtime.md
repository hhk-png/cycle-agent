# 第29章 MiniFlink：作业图、调度与并行执行

## 29.1 从转换到图

每次调用 `map/filter/key_by/window`，MiniFlink 都会创建一个新的 `Node`，并用 `Edge` 指向父节点：

```python
node = make_node(
    name,
    factory,
    parallelism=self.parallelism,
    inputs=[Edge(self.node, partitioning, input_index=0)],
)
```

`Node` 不持有算子实例，只持有 `operator_factory(index, subtasks)`。这让“逻辑图”和“物理执行”分离：

```text
DataStream 链式调用 → Node/Edge 图
env.execute()        → build_graph → JobExecution
JobExecution         → 按并行度展开 RuntimeTask → 实例化算子
```

### 反向构图

`env.execute()` 从 sink 节点反向收集所有依赖：

```python
def build_graph(terminals):
    seen = {}
    def collect(node):
        if node.id in seen:
            return
        seen[node.id] = node
        for edge in node.inputs:
            collect(edge.source)
    for terminal in terminals:
        collect(terminal)
    return JobGraph(list(seen.values()), list(terminals))
```

好处是：

- 没有被 sink 使用的“悬空分支”不会进入作业；
- 同一个 Node 被多个下游引用时只保留一份；
- 拓扑顺序由 `topological_order()` 计算，检测环。

```python
def topological_order(self):
    visited, visiting, order = set(), set(), []
    def visit(node):
        if node.id in visited: return
        if node.id in visiting: raise ValueError("作业图存在环")
        visiting.add(node.id)
        for edge in node.inputs: visit(edge.source)
        visiting.remove(node.id)
        visited.add(node.id)
        order.append(node)
    for sink in self.sinks: visit(sink)
    return order
```

## 29.2 展开成 Task

`JobExecution.__init__` 对拓扑序中的每个 Node，按 `parallelism` 创建 `RuntimeTask`：

```python
for index, node in enumerate(topo):
    base = node.uid or f"{index:03d}-{node.name}"
    for subtask in range(node.parallelism):
        task_id = f"{base}#{subtask}"
        task = RuntimeTask(task_id, node, subtask, node.parallelism)
        self.tasks.append(task)
        self.tasks_by_node[node.id].append(task)
```

每个 `RuntimeTask` 拥有：

- 独立的 `RuntimeContext`；
- 独立的 `KeyedStateBackend`；
- 独立的 `TimerService`；
- 自己的算子实例（通过 factory 创建）。

```python
self.context = RuntimeContext(
    task_name=f"{node.name}[{subtask_index}]",
    subtask_index=subtask_index,
    parallelism=parallelism,
    state_backend=KeyedStateBackend(),
)
self.operator = node.operator_factory(subtask_index, parallelism)
self.operator.open(self.context)
```

**状态隔离**：两个 subtask 即使同名状态，也不会互相污染，因为后端是 per-task 的。这对应 Flink 的“每个 subtask 独立状态”。

## 29.3 通道与分区

`_wire_edges` 为每条边建立“上游 subtask → 下游 subtask”的通道：

```python
for source_task in source_tasks:
    queues = {i: deque() for i in range(len(child_tasks))}
    for target_index, child in enumerate(child_tasks):
        channel = Channel(edge_index, source_task.id, queue=queues[target_index])
        child.input_channels[edge_index].append(channel)
    source_task.downstream.append(
        Downstream(edge, edge_index, child_tasks, queues,
                   source_subtask_index=source_task.subtask_index)
    )
```

```text
source#0 ──┬─> window#0 queue
           ├─> window#1 queue
source#1 ──┼─> window#0 queue
           └─> window#1 queue
```

分区方式在 `_select_targets` 中实现：

```python
def _select_targets(self, down, record):
    count = len(down.child_tasks)
    partitioning = down.edge.partitioning
    if partitioning is Partitioning.BROADCAST:
        return list(range(count))
    if partitioning is Partitioning.HASH:
        return [_stable_hash(record.key) % count]
    if partitioning is Partitioning.REBALANCE:
        target = down.round_robin % count
        down.round_robin += 1
        return [target]
    # FORWARD：并行度一致时按子任务编号直连
    if count == down.edge.source.parallelism:
        return [min(down.source_subtask_index, count - 1)]
    target = down.round_robin % count
    down.round_robin += 1
    return [target]
```

### 稳定哈希

Python 的 `hash()` 对字符串有随机种子，不能跨进程稳定。MiniFlink 用 SHA-256：

```python
def _stable_hash(value):
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        digest = hashlib.sha256(value.encode("utf-8")).digest()
        return int.from_bytes(digest[:8], "big", signed=False)
    return _stable_hash(repr(value))
```

这对应 Flink 的 `keyGroup` 哈希：同一个 key 必须稳定落到同一个 subtask。

## 29.4 调度循环

MiniFlink 的调度器是单线程轮转：

```python
def run_round(self):
    budget = self.checkpoint_config.micro_batch_records
    for task in self.source_tasks:
        produced = 0
        while produced < budget and not task.finished:
            element = task.operator.next_element()
            if element is None: break
            if isinstance(element, Record): produced += 1
            self._emit_from_source(task, element)
    self._drain()
    if self.checkpointing_enabled:
        self.checkpoint()
```

`_drain()` 反复做两件事，直到没有进展：

1. 每个非 source task 从输入通道取一个元素处理；
2. 每个 task 调用 `operator.poll()` 收集异步输出（例如 Async I/O）。

```python
def _drain(self):
    while True:
        progress = False
        for task in self.non_source_tasks:
            if self._process_one(task): progress = True
        for task in self.tasks:
            outputs = task.operator.poll()
            if outputs:
                self._handle_outputs(task, outputs)
                progress = True
        if not progress: break
```

这是一个“确定性、可调试”的调度器：没有真实线程竞争，测试可以稳定复现。代价是没有并发、没有反压、没有真正的异步。

### 微批边界

每个 round 从每个 source 读取最多 `micro_batch_records` 条，然后排空所有通道。这形成一个“静止点”，checkpoint 可以在这里做全局快照。

```text
round 1: source 读 50 条 → 排空 → checkpoint 1
round 2: source 读 50 条 → 排空 → checkpoint 2
...
```

生产 Flink 不需要停机做 checkpoint，而是用 barrier 在流动中切面。MiniFlink 用“暂停”换“简单”。

## 29.5 Watermark 传播

source 产生 watermark 后，`_route_control` 把它广播到所有下游通道：

```python
def _route_control(self, task, element):
    for down in task.downstream:
        for target_index in range(len(down.child_tasks)):
            down.queues[target_index].append(element)
```

下游收到 watermark 后，`_recompute_watermark` 取所有**未结束**通道的最小值：

```python
channels = [c for group in task.input_channels for c in group]
active = [c for c in channels if not c.ended]
effective = min((c.watermark for c in active), default=MAX_WATERMARK)
if effective <= task.current_watermark:
    return
task.current_watermark = effective
outputs = task.operator.process_watermark(effective)
```

```text
input A: wm=100
input B: wm=80
effective = 80
```

这与 Flink 的语义一致：最慢的输入决定进度。区别是：

- MiniFlink 没有 per-channel 的 watermark 对齐/缓冲；
- 没有 idleness；
- 没有 watermark alignment；
- 没有真正的网络反压。

### 结束信号

source 读完数据后：

1. 发 `Watermark(MAX)`；
2. 发 `EndOfStream`；
3. 下游把通道标记为 ended，watermark 设为 MAX；
4. 所有通道结束时，task finalize：触发剩余窗口/定时器，发 `EndOfStream` 给下游。

```python
def _maybe_finalize(self, task):
    if not all(c.ended for group in task.input_channels for c in group):
        return
    task.finished = True
    if task.current_watermark < MAX_WATERMARK:
        self._handle_outputs(task, task.operator.process_watermark(MAX_WATERMARK))
    self._handle_outputs(task, task.operator.end_of_input())
    self._route_control(task, EndOfStream())
    task.close()
```

这保证“作业结束时，所有窗口/定时器都被冲刷”。

## 29.6 侧输出

侧输出通过 `RuntimeContext.emit_side_output(tag, value)` 暂存；发射时会捕获当时的 `current_key`/`current_timestamp`，所以窗口和定时器的侧输出不会被错归到“最后一条数据记录”。`_handle_outputs` 在每条记录处理后调用 `_route_side_outputs`：

```python
for tag, records in task.context.drain_side_output_records().items():
    for side in records:
        record = Record(side.value, timestamp=side.timestamp, key=side.key)
        routed = self._route_record(task, record, tag)
        if not routed:
            self.side_outputs.setdefault(tag, []).append(side.value)
```

窗口算子在 `process_watermark` 中会把侧输出排在 `Watermark` 之前，避免下游窗口把侧输出当迟到数据。

`_route_record` 只把记录发给 `edge.output_tag == tag` 的下游：

```python
for down in task.downstream:
    if down.edge.output_tag != tag:
        continue
    ...
```

如果侧输出没有下游，值会汇总到 `JobExecution.side_outputs`，方便测试/调试。

## 29.7 并行度与 key

MiniFlink 的并行度来自 `Node.parallelism`：

```python
env = StreamExecutionEnvironment(parallelism=4)
stream.key_by(...).window(...)  # 继承 keyBy 的并行度
```

`KeyByOperator` 把 key 写入 `Record.key`：

```python
class KeyByOperator(Operator):
    def process_record(self, record, input_index=0):
        return [record.with_key(self.selector(record.value))]
```

之后的 HASH 边用 `_stable_hash(key) % count` 路由。因此：

- 同 key 一定同 subtask；
- 不同 key 可能同 subtask（哈希冲突/取模）；
- 热点 key 会打满一个 subtask（没有自动拆分）。

### source 的分片

`CollectionSource` 按 subtask 轮转切片：

```python
self.data = [values[i] for i in range(subtask_index, len(values), parallelism)]
```

这类似“按分区读”，但不是真正的分区语义：记录的顺序和归属由列表下标决定。

## 29.8 指标与调试

`JobExecution.metrics()` 返回：

```python
{
  "job": {"rounds": ..., "records_processed": ..., "finished": ...},
  "checkpoints": {"completed": ..., "failed": ..., "last_checkpoint_id": ...},
  "tasks": {
     "<task-id>": {"operator": ..., "subtask": ..., "records_in": ...,
                   "records_out": ..., "watermark": ..., "finished": ...}
  },
  "side_outputs": {"late": 3}
}
```

`env.explain()` 打印拓扑：

```text
JobGraph:
  collection-source[2] <- (source)
  map[2] <- (collection-source:forward)
  key-by[2] <- (map:rebalance)
  window[2] <- (key-by:hash)
  print[1] <- (window:rebalance)
```

`LocalCluster` 模拟 slot 分配：

```python
cluster = LocalCluster(task_managers=2, slots_per_task_manager=4)
job = cluster.submit(env)
print(job.status, cluster.metrics())
```

它按“最大算子并行度”计算 slot 需求，并在作业结束后释放。

## 29.9 已知限制

- 没有真正的网络缓冲/反压；
- 没有算子链（每个 Node 一个 task）；
- 没有 key group/rescale；
- 没有 checkpoint barrier/对齐；
- 没有自动故障检测/重启；`set_restart_strategy()` 只保存配置，运行时不读取，不驱动自动重启；
- 没有 per-channel watermark 对齐/idleness；
- 没有异步快照。

这些限制不影响理解“图如何变成 task、记录如何路由、watermark 如何传播”，但决定了它不能用于生产。

## 本章小结

MiniFlink 的运行时可以概括为：**Node 图 → 按并行度展开 RuntimeTask → 通道连接 → 单线程轮转调度 → watermark 取最小 → 微批边界快照**。

下一章进入时间、窗口与状态：看 `WindowOperator` 如何维护 bucket、触发、合并会话，以及 `KeyedStateBackend` 如何保存状态和定时器。
