# 第28章 MiniFlink 总览与工程结构

## 28.1 为什么自己写一个

读 Flink 源码时，容易被三层图、序列化、网络栈、RocksDB、Calcite 淹没。MiniFlink 的目标不是复刻 Flink，而是用可读的 Python 把核心机制串起来：

- 一条记录如何从 source 流到 sink；
- keyBy 如何决定状态分区；
- watermark 如何在多输入之间取最小值；
- 窗口如何触发、合并、清理；
- checkpoint 如何保存 source offset、算子状态和计时器；
- 恢复后为什么不会重复处理。

当你能亲手实现这些，再回头看 Flink 的 Java 代码，抽象就有了落点。

```text
生产 Flink：分布式、JVM、网络栈、RocksDB、Calcite、K8s
MiniFlink：单进程、Python、deque、dict、SQLite、pytest
共同点：时间、状态、窗口、checkpoint 的语义
```

## 28.2 工程结构

```text
miniflink/
├── core.py             # Record、Watermark、Window、OutputTag
├── api.py              # StreamExecutionEnvironment、DataStream、KeyedStream、WindowedStream
├── graph.py            # Node、Edge、JobGraph、分区方式
├── runtime.py          # Task、通道、调度、watermark、side output、checkpoint 入口
├── operators.py        # source/map/filter/reduce/process/window/join/sink
├── windows.py          # assigner、trigger、evictor、WindowOperator
├── state.py            # KeyedStateBackend、Value/List/Map/Reducing/Aggregating State、TTL
├── functions.py        # 用户函数、上下文、TimerService
├── watermarks.py       # 时间戳分配、watermark 生成
├── checkpointing.py    # Checkpoint、存储、协调器、重启策略
├── connectors.py       # 内存 Kafka、文件、SQLite sink
├── table.py            # Table API/SQL 子集
├── cep.py              # Pattern、CepOperator
├── metrics.py          # Counter/Gauge/Meter/MetricGroup
├── deployment.py       # LocalCluster、slot 模拟
└── cli.py              # run/sql 命令
```

代码只依赖 Python 标准库；测试用 pytest。没有第三方运行时依赖，方便阅读和移植。

### 模块依赖

```text
api → graph, operators, runtime, watermarks, windows
runtime → graph, state, checkpointing, metrics
operators → functions, state, windows, watermarks
windows → functions, state
table → api, graph, connectors (可选)
cep → functions, graph
deployment → api, runtime, graph
```

依赖是单向的：`api` 依赖运行时，运行时不知道 `api`。这让运行时可以独立测试，也避免“用户 API 和调度逻辑耦合”。

## 28.3 安装与运行

在 `flink-tutorial` 目录：

```bash
python examples/01_word_count.py
python examples/09_checkpoint_restore.py
python -m pytest -q
python check_docs.py
```

MiniFlink 也提供 CLI：

```bash
python -m miniflink.cli run examples/01_word_count.py
python -m miniflink.cli sql --table orders=orders.csv \
  --query "SELECT user, SUM(amount) AS total FROM orders GROUP BY user"
```

## 28.4 第一个作业

```python
from miniflink import StreamExecutionEnvironment

env = StreamExecutionEnvironment(parallelism=2)
(
    env.from_collection(["a", "b", "a"], parallelism=2)
    .map(lambda word: (word, 1))
    .key_by(lambda pair: pair[0])
    .reduce(lambda a, b: (a[0], a[1] + b[1]))
    .print()
)
job = env.execute("word-count")
print(job.metrics()["job"])
```

执行过程：

1. `from_collection` 创建一个 source Node；
2. `map/key_by/reduce/print` 追加 Node 和 Edge；
3. `execute` 从 sink 反向构图；
4. 每个 Node 按并行度展开成 RuntimeTask；
5. source 产生记录，调度器沿通道推进；
6. keyBy 节点写入 key，reduce 节点按 key 使用状态；
7. watermark 和 EndOfStream 传播，所有 task 结束。

### 构图代码

```python
def from_collection(self, data, *, parallelism=None, ...):
    par = parallelism or self.parallelism

    def factory(index: int, subtasks: int) -> SourceOperator:
        return CollectionSource(values, index, subtasks, strategy=...)

    node = make_node(name, factory, parallelism=par, is_source=True)
    return DataStream(self, node)
```

`Node` 只保存“如何创建算子”的工厂，不保存算子实例。真正的算子在 `JobExecution` 展开时按 subtask 创建，这对应 Flink 的“逻辑图 vs 物理执行图”。

## 28.5 设计取舍

MiniFlink 有意做了这些简化：

- **单进程**：没有 JVM、网络、序列化、反压；
- **调度是同步的**：Async I/O 虽有线程池，但调度器每轮只提交一条记录并等待完成，实际并发接近 1；`capacity` 不代表真实并发；
- **微批快照**：checkpoint 时暂停并排空，不是屏障协议；
- **类型系统缺失**：Python 对象直接传递，没有 TypeInformation/Kryo；
- **SQL 有界表**：SQLite 执行，不是流式 Calcite；CTAS/VIEW 只做一次性物化；
- **单机存储**：内存/本地文件，不是 HDFS/S3；
- **无 HA**：没有 JobManager 选举和自动 failover；
- **无 key group**：恢复时按 task id 对应，不支持 rescale。

这些简化换来的是可读性和可测试性。它不是生产引擎，而是“可执行的架构图”。

### 为什么用 Python

- 标准库足够，无依赖；
- 代码短，适合逐行阅读；
- `dataclass`/`dict`/`deque` 能直观表达状态和通道；
- pytest 让行为可测试；
- 读者可以把概念快速映射到 Java/Scala。

代价是性能、类型安全和分布式语义。MiniFlink 不打算解决这些问题。

## 28.6 与 Flink 的映射

| Flink | MiniFlink | 说明 |
|---|---|---|
| StreamGraph/JobGraph | Node/Edge | 转换 DAG |
| ExecutionGraph | RuntimeTask | 按并行度展开 |
| Network buffer | deque | 无容量限制 |
| Watermark | Watermark | 多输入取最小 |
| KeyedStateBackend | KeyedStateBackend | 堆内字典 |
| CheckpointBarrier | 微批边界 | 同步快照 |
| RocksDB | 无 | 状态全在内存 |
| TableEnvironment | TableEnvironment | SQLite 子集 |
| CEP | CepOperator | 简单序列 |
| MiniCluster | LocalCluster | 模拟 slot |

## 28.7 验证基线

仓库包含测试和示例，覆盖：

- DataStream 转换、union、side output、async；
- 事件时间/处理时间窗口、迟到、触发器；
- 键控状态、TTL、事件/处理时间定时器；
- checkpoint/恢复、source offset；
- Kafka/文件/SQLite 连接器；
- Table/SQL（含 CTAS/VIEW AS SELECT 物化子集）、CEP、广播、join；
- 指标、CLI、LocalCluster；
- 去重/对账（示例 19）、实时 AI 形状（示例 20）与 SQL 生产模式子集（示例 22）。

`check_docs.py` 会核对章节数、示例数、测试数和链接。

```bash
bash verify.sh
# pytest → examples → check_docs
```

## 28.8 阅读路线

- 想理解运行时：第 29 章；
- 想理解时间/窗口/状态：第 30 章；
- 想理解 checkpoint/连接器：第 31 章；
- 想理解 Table/CEP/指标：第 32 章；
- 想对照生产 Flink：第 33 章。

建议边读边跑对应测试：

```bash
python -m pytest tests/test_runtime.py -q
python -m pytest tests/test_windows.py -q
python -m pytest tests/test_checkpointing.py -q
```

## 本章小结

MiniFlink 是一张“可运行的架构图”。它把 Flink 的核心概念压缩到几千行 Python，用测试保证行为可观察。

接下来的四章分别拆解运行时、时间与状态、检查点与连接器、Table/SQL/CEP。
