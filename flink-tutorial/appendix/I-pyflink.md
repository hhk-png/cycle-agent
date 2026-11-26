# 附录 I PyFlink 工程手册

> 版本基线：Flink 2.3，PyFlink 与 Flink 同版本发布。Python 版本、连接器 JAR、异步 API 的支持范围随发行版变化，落地时以所用版本的官方兼容矩阵为准。MiniFlink 是用 Python 写的教学实现，**不是 PyFlink**；两者只在“Python 写流处理”这一点上相似。

## I.1 什么时候用 PyFlink

PyFlink 不是“用 Python 重写 Flink”，而是让 Python 代码以 UDF、Table 作业或 DataStream 作业的形式运行在 Flink 的 JVM 运行时上。它的价值与代价都很明确。

| 场景 | 适合 PyFlink | 更适合 Java/SQL |
|---|---|---|
| 数据科学/算法团队维护特征、模型预处理 | 是 | 否 |
| pandas/numpy 向量化清洗、窗口统计 | 是 | 可用 SQL，但复杂逻辑仍 Python 顺手 |
| 快速原型、 Notebook 式探索 | 是 | 上线前通常要重写或固化 |
| 已有 Python 模型/规则/特征库 | 是 | 跨语言调用成本高 |
| 超高吞吐、极低延迟的核心链路 | 否 | 是 |
| 复杂 CEP、细粒度状态/定时器 | 有限（`pyflink.cep` 不完整/版本不稳定） | 是 |
| 依赖大量 Java 连接器/自定义 Source | 部分 | 是 |
| 需要 JVM 级火焰图、GC、堆外调优 | 否 | 是 |

一个实用的判断方法是：**Python 负责“算”，Java/SQL 负责“搬运和状态”**。如果瓶颈在模型/特征计算，PyFlink 很合适；如果瓶颈在网络、序列化、状态访问或 checkpoint，PyFlink 的跨进程开销会放大问题。

## I.2 版本、安装与本地运行

PyFlink 的版本必须与集群的 Flink 版本匹配。不要用 `pip install apache-flink` 装到最新版，再连一个旧集群。

```bash
# 示例：与 Flink 2.3 对齐；实际版本号以发行版为准
python -m pip install apache-flink==2.3.0

python - <<'PY'
import pyflink
print(pyflink.__version__)
PY
```

几个容易忽略的前提：

- **仍然需要 Java**：PyFlink 在本地和集群上都会启动 JVM；`JAVA_HOME` 要指向受支持的 JDK（2.x 默认 Java 17）。
- **Python 版本受支持矩阵限制**：不要把“本机 Python 3.14 能跑 MiniFlink”当成 PyFlink 也支持 3.14。生产镜像应固定到发行版明确支持的版本（常见是 3.10/3.11 一类的区间）。
- **本地模式不是集群验证**：`set_parallelism(1)` 能跑通，不代表分布式序列化、依赖分发、checkpoint 都没问题。
- **客户端与集群的 Python 环境要一致**：UDF 在客户端做类型推断/编译，在 TaskManager 的 Python worker 里执行；两端版本不一致会出现难以定位的序列化错误。

本地最小作业：

```python
from pyflink.common import Types
from pyflink.datastream import StreamExecutionEnvironment

env = StreamExecutionEnvironment.get_execution_environment()
env.set_parallelism(1)

(
    env.from_collection(["flink", "stream", "flink"], type_info=Types.STRING())
    .map(lambda word: (word, 1), output_type=Types.TUPLE([Types.STRING(), Types.INT()]))
    .key_by(lambda pair: pair[0])
    .reduce(lambda a, b: (a[0], a[1] + b[1]))
    .print()
)
env.execute("pyflink-word-count")
```

## I.3 运行时形状：JVM 与 Python worker

PyFlink 的典型结构不是“把 Python 解释器嵌进 JVM 算子”，而是：

```text
Client (Python)
  └── JVM Gateway / TableEnvironment
        └── JobManager
              └── TaskManager (JVM)
                    └── Python worker 进程（每个 subtask 一个）
                          ├── 用户 Python UDF / ProcessFunction
                          ├── pandas / numpy / 模型库
                          └── 第三方依赖
```

数据在 JVM 与 Python worker 之间通过网关和序列化交换；使用 pandas UDF 时通常走 Arrow 批量传输。这个结构带来五个工程后果：

1. **每批数据要跨进程**：逐行 UDF 的固定开销远高于 Java UDF；
2. **内存双份**：JVM 缓冲与 Python 对象各占一份，容器内存要同时覆盖；
3. **启动开销**：worker 启动、`import pandas/numpy/模型库` 会拉长作业启动和恢复时间；
4. **依赖隔离**：worker 环境必须与作业依赖完全一致，不能依赖“本机碰巧装过”；
5. **故障域**：Python worker 崩溃会表现为算子失败，日志分散在 TM 日志与 worker stderr。

### Bundle：跨进程的批大小

PyFlink 不会每来一条记录就跨一次进程，而是按 bundle 攒批：

```yaml
python.fn-execution.bundle.size: 1000
python.fn-execution.bundle.time: 100ms
python.fn-execution.arrow.batch.size: 1000
```

- `bundle.size` 太小：跨进程次数多，吞吐低；
- `bundle.size` 太大：单批延迟高，checkpoint 可能要等更久；
- `bundle.time` 控制“攒不够也发”的上限；
- `arrow.batch.size` 只影响 pandas UDF 的批大小。

调参顺序：先确认是逐行 UDF 还是向量化 UDF，再调 bundle；不要用增大 bundle 掩盖算法本身的逐行开销。

## I.4 DataStream API

PyFlink 的 DataStream API 与 Java 版本概念一致，但函数签名是 Python 风格：`ProcessFunction` 用生成器 `yield` 输出，而不是 `Collector`。

```python
from pyflink.common import Duration, Types, WatermarkStrategy
from pyflink.datastream import StreamExecutionEnvironment
from pyflink.datastream.functions import KeyedProcessFunction, RuntimeContext
from pyflink.datastream.state import ValueStateDescriptor


class Deduplicate(KeyedProcessFunction):
    def open(self, runtime_context: RuntimeContext):
        self.last_seen = runtime_context.get_state(
            ValueStateDescriptor("last-seen", Types.LONG())
        )

    def process_element(self, value, ctx):
        previous = self.last_seen.value()
        if previous is None or value["ts"] - previous > 60_000:
            self.last_seen.update(value["ts"])
            yield value
        ctx.timer_service().register_event_time_timer(value["ts"] + 60_000)

    def on_timer(self, timestamp, ctx):
        # 可以在这里清理状态或发出超时事件
        yield {"type": "timeout", "ts": timestamp, "key": ctx.get_current_key()}


env = StreamExecutionEnvironment.get_execution_environment()
strategy = (
    WatermarkStrategy.for_bounded_out_of_orderness(Duration.of_seconds(5))
    .with_timestamp_assigner(lambda event, record_ts: event["ts"])
    .with_idleness(Duration.of_seconds(30))
)

(
    env.from_collection(events, type_info=Types.MAP(Types.STRING(), Types.STRING()))
    .assign_timestamps_and_watermarks(strategy)
    .key_by(lambda event: event["user_id"])
    .process(Deduplicate())
    .print()
)
env.execute("dedup")
```

要点：

- `Types` 要显式声明复杂类型；Python 的动态类型不会自动变成 Flink 的 `TypeInformation`。
- `ctx.timer_service()` 是方法调用；Java 是 `ctx.timerService()`。
- 侧输出用 `ctx.output(tag, value)`；`OutputTag` 从 `pyflink.common` 导入。
- `open` 接收 `RuntimeContext`，用 `get_state`/`get_list_state`/`get_map_state` 获取状态。
- 有状态算子仍然要关注 keyBy、watermark、checkpoint 的语义；PyFlink 不会替你解决状态膨胀。

### Source/Sink 的现实边界

PyFlink 的 DataStream connector 覆盖不如 Java 全。常见做法：

- **集合/文件/自定义 Python Source**：适合原型和测试；
- **Kafka/Pulsar/JDBC/湖仓**：优先用 Table/SQL DDL + Java connector JAR；
- **复杂 Source（CDC、分区发现、事务 sink）**：用 Java 连接器，或让 Python 作业消费 SQL 表；
- **自定义 Python Source**：要实现 PyFlink 的 Source 接口，仍受 checkpoint/并行度/分区发现约束。

不要假设“Java 有的 connector，PyFlink DataStream 都有”。先查版本矩阵，再决定用 SQL 还是 DataStream。

## I.5 Table API、SQL 与 UDF

Table API/SQL 是 PyFlink 最常用的入口，因为连接器、Catalog、优化器都由 JVM 侧承担。

```python
from pyflink.table import DataTypes, EnvironmentSettings, TableEnvironment

settings = EnvironmentSettings.new_instance().in_streaming_mode().build()
t_env = TableEnvironment.create(settings)

# connector JAR 必须先加载；路径和版本以实际发行版为准
t_env.get_config().set(
    "pipeline.jars",
    "file:///opt/flink/lib/flink-sql-connector-kafka-4.0.0-2.0.jar",
)

t_env.execute_sql("""
CREATE TABLE orders (
  order_id STRING,
  user_id STRING,
  amount DECIMAL(18, 2),
  order_time TIMESTAMP(3),
  WATERMARK FOR order_time AS order_time - INTERVAL '5' SECOND
) WITH (
  'connector' = 'kafka',
  'topic' = 'orders',
  'properties.bootstrap.servers' = 'kafka:9092',
  'properties.group.id' = 'pyflink-orders',
  'scan.startup.mode' = 'group-offsets',
  'format' = 'json'
)
""")

t_env.execute_sql("""
INSERT INTO user_revenue
SELECT user_id, window_start, window_end, SUM(amount) AS total
FROM TABLE(TUMBLE(TABLE orders, DESCRIPTOR(order_time), INTERVAL '5' MINUTES))
GROUP BY user_id, window_start, window_end
""")
# 流式 INSERT 提交后持续运行；有界/批作业可对返回的 TableResult 调用 .wait()
```

Kafka/JDBC/湖仓 connector 的 JAR 必须能被集群加载：可以像上面那样通过 `pipeline.jars` 指定，也可以放进集群 `lib/`，或用 `pipeline.classpaths`。具体 JAR 名和版本以发行版为准；不要混用不同 Flink 小版本的 connector。

### 五类 Python UDF

| 类型 | 输入 → 输出 | 适合 | 备注 |
|---|---|---|---|
| 标量 UDF | 一行 → 一个值 | 字段清洗、简单规则 | 逐行开销最大 |
| 向量化 UDF（pandas） | Series → Series | 批量数值/字符串处理 | 通常比逐行快，仍受 Arrow/内存影响 |
| 表函数 UDTF | 一行 → 多行 | 拆分、展开 | 用 `yield` 输出 |
| 聚合 UDAF | 多行 → 一个值 | 自定义聚合 | 要实现 accumulator/merge |
| 表聚合 UDTAF | 多行 → 多行 | TopN、分位数 | 状态与输出更复杂 |

标量 UDF：

```python
from pyflink.table import DataTypes
from pyflink.table.udf import udf

@udf(result_type=DataTypes.STRING())
def mask_phone(phone):
    if phone is None or len(phone) < 7:
        return phone
    return phone[:3] + "****" + phone[-4:]

t_env.create_temporary_system_function("mask_phone", mask_phone)
t_env.execute_sql("SELECT mask_phone(user_phone) FROM users").print()
```

向量化 UDF：

```python
import pandas as pd
from pyflink.table import DataTypes
from pyflink.table.udf import udf

@udf(result_type=DataTypes.DOUBLE(), input_types=[DataTypes.DOUBLE()])
def normalize(series: pd.Series) -> pd.Series:
    std = series.std()
    return (series - series.mean()) / (std if std else 1.0)
```

不同 PyFlink 版本对“是否 pandas UDF”的声明方式不同（可能靠类型注解推断，也可能需要显式 `func_type`/`udf_type` 参数）；以所用版本文档为准。

选择原则：**能向量化就向量化；不能向量化就减少调用次数（预聚合、缓存、批处理），而不是无脑加并行度**。

### 类型映射

| Python | Flink SQL |
|---|---|
| `bool` | `BOOLEAN` |
| `int` | `BIGINT`（注意溢出） |
| `float` | `DOUBLE` |
| `decimal.Decimal` | `DECIMAL(p, s)` |
| `str` | `STRING` |
| `bytes` | `BYTES` |
| `datetime.datetime` | `TIMESTAMP(3)` / `TIMESTAMP_LTZ(3)` |
| `list` | `ARRAY<T>` |
| `dict` | `MAP<K, V>` / `ROW<...>` |

金额用 `Decimal`，不要用 `float`；时间统一 UTC，并明确 `TIMESTAMP` 与 `TIMESTAMP_LTZ` 的区别。

## I.6 依赖管理：把 Python 环境当成作业制品

PyFlink 最常见的生产事故不是 SQL 写错，而是“TaskManager 上没有这个包”。依赖管理有三种落地方式。

### 方式一：集群预装（最简单，最不隔离）

```bash
# 所有 TaskManager 镜像/机器都装同一套依赖
python -m pip install -r requirements.txt
```

适合：平台统一管理、依赖少、版本稳定的环境。缺点是升级依赖会影响所有作业。

### 方式二：作业级 archive

```yaml
python.client.executable: /opt/venv/bin/python
python.executable: python_env/bin/python
python.archives: s3://libs/pyflink-env.zip#python_env
```

- `python.client.executable` 用于客户端编译/类型推断；
- `python.executable` 是 worker 启动的解释器；
- `python.archives` 把 zip/tar.gz 分发到每个 TaskManager 并解压；
- `#python_env` 是解压目录名，`python.executable` 的相对路径要与之对应。

不同版本对相对路径/解压位置的解析可能不同，落地前在副本集群验证。

### 方式三：自定义镜像（推荐生产）

```dockerfile
FROM flink:2.3-scala_2.12-java17
RUN python3 -m venv /opt/venv \
 && /opt/venv/bin/pip install --no-cache-dir -r /opt/requirements.txt
ENV PATH="/opt/venv/bin:${PATH}"
```

```yaml
python.client.executable: /opt/venv/bin/python
python.executable: /opt/venv/bin/python
```

优点：依赖可复现、启动快、与 Flink 版本一起版本化。缺点是镜像变大，构建/扫描成本上升。

### 依赖治理清单

- 锁定 `requirements.txt` 或 `poetry.lock`，记录 hash；
- 区分客户端依赖与 worker 依赖；
- 避免 native 扩展与集群 glibc/CPU 架构不匹配；
- 不要在 UDF 里 `pip install`；
- 定期扫描 CVE；
- 记录 Python、pandas、numpy、模型库版本；
- 升级 Python 版本前做序列化/状态恢复演练。

## I.7 状态、Watermark 与 Checkpoint

PyFlink 的状态语义与 Java 一致：keyed state 必须 `key_by` 后使用，事件时间由 watermark 驱动，checkpoint 保存 source offset、算子状态和定时器。

```python
from pyflink.common import Duration
from pyflink.datastream import StreamExecutionEnvironment

env = StreamExecutionEnvironment.get_execution_environment()
env.enable_checkpointing(30_000)
env.get_checkpoint_config().set_min_pause_between_checkpoints(10_000)
env.get_checkpoint_config().set_checkpoint_timeout(10 * 60_000)
```

注意：

- **状态 TTL**：PyFlink 的 `StateTtlConfig` 用法与 Java 类似，但部分枚举/方法名有差异；以版本文档为准。
- **watermark**：`WatermarkStrategy.for_bounded_out_of_orderness(...)`、`with_idleness(...)`、`with_timestamp_assigner(...)`。
- **checkpoint 存储**：在 `config.yaml` 或代码里配置 `state.checkpoints.dir`；PyFlink 作业同样需要共享存储。
- **端到端 exactly-once**：取决于 source offset + sink 事务/幂等 + checkpoint 完成通知；Python UDF 里直接写外部系统仍然无法回滚。
- **恢复时间**：Python worker 启动和 import 大包会拉长恢复时间，要把这部分算进 RTO。

## I.8 性能模型与调优

PyFlink 的性能问题通常不是“Python 慢”，而是“跨进程次数太多”或“逐行处理”。

```text
总开销 ≈ 记录数 × (序列化 + 跨进程 + Python 调用)
        + 批次数 × (网关往返 + 反序列化)
        + worker 启动/import
```

优化顺序：

1. **向量化**：pandas UDF 替代逐行 UDF；
2. **减少调用**：预聚合、缓存、规则短路；
3. **调 bundle**：增大 `bundle.size`/`bundle.time`，摊薄跨进程成本；
4. **减少数据宽度**：只传模型需要的字段；
5. **控制 worker 数**：并行度高会增加 worker 数和内存；
6. **异步外部调用**：只在支持且有必要时使用，仍需限流/超时；
7. **JVM 侧承担重活**：连接器、状态、窗口尽量用 SQL/Java。

经验判断（定性，不是 benchmark）：

- 逐行 Python UDF 的吞吐通常显著低于等价 Java UDF；
- pandas 向量化 UDF 可以接近 Java 的量级，但受批大小、内存拷贝和 GIL 影响；
- 复杂模型推理往往是外部服务瓶颈，不是 Python 本身；
- 状态大、checkpoint 重的作业，PyFlink 的跨进程开销不是主因，先解决状态设计。

### 内存

- 容器内存要同时覆盖 JVM heap/off-heap、Python worker 堆、Arrow 缓冲、模型文件；
- `taskmanager.memory.process.size` 是总预算，不是 Python 进程的预算；
- Python worker OOM 可能表现为容器 OOMKill 或 worker 重启；
- 大模型不要在每个 worker 里加载多份；考虑外部模型服务或共享内存/映射文件。

### 观测

- JVM 侧：`numRecordsIn/Out`、busy、反压、checkpoint、GC；
- Python 侧：worker CPU/内存、启动时间、import 时间、UDF 调用耗时；
- 跨进程：bundle 大小、网关往返、序列化错误；
- 工具：`py-spy`、`cProfile`、`async-profiler`（JVM）、容器指标。

## I.9 测试与调试

### 本地功能测试

```python
from pyflink.datastream import StreamExecutionEnvironment

def test_word_count():
    env = StreamExecutionEnvironment.get_execution_environment()
    env.set_parallelism(1)
    result = (
        env.from_collection(["a", "b", "a"])
        .map(lambda word: (word, 1))
        .key_by(lambda pair: pair[0])
        .reduce(lambda a, b: (a[0], a[1] + b[1]))
        .execute_and_collect()
    )
    assert sorted(result) == [("a", 2), ("b", 1)]
```

`execute_and_collect()` 在不同版本/连接器上的可用性不同；不可用时用 `print`/测试 sink 收集。

### Table 测试

```python
rows = t_env.sql_query("SELECT ...").execute().collect()
```

### 常见错误

| 现象 | 可能原因 |
|---|---|
| `ModuleNotFoundError` | worker 没装依赖/`python.executable` 指错 |
| `TypeError: ... not serializable` | UDF 捕获了不可序列化对象/闭包 |
| `JavaException`/`Py4JJavaError` | JVM 侧异常，向上看完整栈 |
| 类型不匹配 | Python 返回值与 `result_type` 不一致 |
| worker 启动超时 | import 大包/镜像拉取/资源不足 |
| checkpoint 超时 | bundle 太大/状态大/外部调用阻塞 |
| 结果与本地不一致 | 并行度/序列化/时区/watermark 差异 |

调试顺序：先 `set_parallelism(1)` 本地复现，再看 worker 日志，最后看 JVM 栈和 checkpoint 指标。不要一上来就怀疑 Flink 集群。

## I.10 限制与常见坑

- **连接器覆盖不全**：复杂 Source/Sink 优先用 SQL + Java connector。
- **CEP/复杂事件**：PyFlink 的 `pyflink.cep` 不是 Java CEP 的完整绑定，模块可用性随版本变化；复杂模式优先 Java CEP、SQL `MATCH_RECOGNIZE`，或用 `KeyedProcessFunction` + 定时器自己实现。
- **异步 API**：2.2 起有增强，但仍受 worker 线程/GIL/限流约束；“异步”不等于无限并发。
- **状态 schema 演进**：Python 对象没有 Java POJO 的稳定 schema，跨版本恢复更脆弱；关键状态用 Avro/Protobuf 或显式序列化。
- **类型推断**：复杂嵌套类型要显式 `Types`/`DataTypes`，不要依赖推断。
- **时间与时区**：Python `datetime` 的 tzinfo 与 Flink `TIMESTAMP_LTZ` 的语义要对齐。
- **UDF 副作用**：在 UDF 里写数据库/发消息不会随 checkpoint 回滚。
- **日志**：`print` 在生产会污染 stdout；用 `logging` 并控制级别。
- **依赖体积**：镜像越大，启动/恢复越慢。

## I.11 Java、SQL、PyFlink 的混合架构

最常见的生产形态不是“全 Python”，而是混合：

```text
Kafka/CDC
  → Flink SQL（连接器、窗口、join、状态）
  → Python UDF（模型/特征/规则）
  → Flink SQL/Java Sink（事务/湖仓）
```

或：

```text
Java DataStream（核心状态/CEP）
  → 外部模型服务（HTTP/gRPC）
  → Python 离线特征（同一份定义，批流对拍）
```

混合架构的关键是**契约**：

- 输入/输出 schema 固定；
- UDF 的版本、依赖、超时、降级明确；
- 训练与线上特征同一份定义；
- 端到端对账覆盖 Python 环节；
- Python worker 的资源/故障域隔离。

## I.12 生产检查清单

- [ ] PyFlink 与 Flink 版本严格匹配；
- [ ] Python 版本在支持矩阵内，客户端与 worker 一致；
- [ ] 依赖固化在镜像/archive，锁定版本与 hash；
- [ ] 复杂类型显式声明，金额用 Decimal，时间统一 UTC；
- [ ] UDF 无不可序列化闭包，无重 IO，异常有降级/死信；
- [ ] 向量化优先，bundle 参数经过压测；
- [ ] 容器内存覆盖 JVM + Python + Arrow + 模型；
- [ ] checkpoint/恢复演练包含 Python worker 启动时间；
- [ ] 连接器 JAR 与 Flink 版本匹配；
- [ ] 指标/日志覆盖 worker 与 UDF；
- [ ] 端到端 exactly-once 边界已逐项验证；
- [ ] 有 Java/SQL 回退方案。

## I.13 与 MiniFlink 的对照

| 概念 | PyFlink | MiniFlink |
|---|---|---|
| 运行时 | JVM + Python worker | 单进程 Python |
| API | DataStream/Table（Python 绑定） | 自定义 Python API |
| 类型 | `Types`/`DataTypes` + JVM TypeInformation | Python 对象直传 |
| 状态 | Flink keyed/operator state | 堆内字典 |
| checkpoint | 屏障/分布式快照 | 微批同步快照 |
| 连接器 | Java connector + SQL DDL | 内存 Kafka/文件/SQLite |
| SQL | Calcite + 流式优化器 | SQLite 有界子集 |
| 生产 | 是 | 否，仅教学 |

MiniFlink 可以帮助理解“Python 代码如何表达 keyBy/窗口/状态”，但不能用来验证 PyFlink 的跨进程、依赖分发和 JVM 集成问题。读 PyFlink 文档时，把 MiniFlink 当作语义草图，而不是运行时替身。

## I.14 小结

PyFlink 的工程化可以概括为四句话：

1. **版本对齐**：PyFlink、Flink、Python、connector 四者版本必须匹配；
2. **依赖制品化**：镜像/archive 固化 worker 环境，不靠“机器上装过”；
3. **减少跨进程**：向量化、批处理、把重活放 JVM/SQL；
4. **边界清晰**：Python 负责算，Flink 负责状态、时间和一致性。

用对了，PyFlink 能让算法团队直接参与实时链路；用错了，它会把 JVM 的复杂度和 Python 的依赖问题叠加在一起。
