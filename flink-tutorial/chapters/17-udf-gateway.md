# 第17章 UDF、SQL Gateway 与客户端

## 17.1 什么时候需要 UDF

内置函数覆盖大部分清洗和聚合，但以下场景需要自定义：

- 业务编码/解码（订单状态、地区映射）；
- 复杂字符串/JSON 处理；
- 调用模型/规则引擎；
- 自定义聚合（中位数、分位数、HyperLogLog）；
- 表函数（一行展开多行）；
- 表聚合（多行聚合成一行多列）；
- 有状态、带定时器的表处理（PTF）。

Flink 支持 Java UDF，也支持 Python UDF（PyFlink）。UDF 会被序列化进作业，依赖必须随作业分发。

### UDF 类型速查

| 类型 | 典型基类/接口 | 输入 → 输出 | 状态 | 典型用途 |
|---|---|---|---|---|
| 标量函数 | `ScalarFunction` | 0..n 列 → 1 值 | 无 | 脱敏、解析、编码转换 |
| 表函数 | `TableFunction` | 1 行 → 0..n 行 | 无 | 一行展开多行、数组打平 |
| 表聚合 | `TableAggregateFunction` | n 行 → 0..n 行多列 | 有 accumulator | TopN、分位数、多指标聚合 |
| 聚合函数 | `AggregateFunction` | n 行 → 1 值 | 有 accumulator | 自定义 SUM/AVG/去重 |
| PTF | `ProcessTableFunction` | 表 → 表 | 有状态/定时器/异步 | 复杂事件、跨行逻辑 |
| Python UDF | PyFlink scalar/table/aggregate | 同 Java | 视类型 | 复用 Python 生态 |
| 模型函数 | `ML_PREDICT`/Model DDL | 表 + 模型 → 表 | 外部服务 | 推理、向量检索 |

选择原则：能用内置函数就不写 UDF；能无状态就不带状态；能标量就不做表聚合；外部调用优先异步/限流，不要阻塞算子线程。

## 17.2 标量函数

```java
public class MaskPhone extends ScalarFunction {
    public String eval(String phone) {
        if (phone == null || phone.length() < 7) return phone;
        return phone.substring(0, 3) + "****" + phone.substring(phone.length() - 4);
    }
}
```

注册：

```java
tEnv.createTemporarySystemFunction("mask_phone", MaskPhone.class);
```

```sql
SELECT mask_phone(user_phone) FROM users;
```

注意：

- `eval` 可以重载，Flink 按参数类型匹配；
- 输入 null 要处理；
- 不要在 UDF 中持有不可序列化的连接；
- 外部调用要限流/超时，避免拖垮算子；
- UDF 的异常会导致 task 失败，要决定“失败还是返回 null”。

### 打开与关闭

`ScalarFunction` 有自己的 `open(FunctionContext)` / `close()` 生命周期，**不是** DataStream 的 `RichFunction`；`open` 适合初始化连接、模型、缓存：

```java
public class ModelScore extends ScalarFunction {
    private transient Model model;

    @Override
    public void open(FunctionContext ctx) {
        model = Model.load("/opt/models/risk-v3");
    }

    public Double eval(String features) {
        return model.score(features);
    }

    @Override
    public void close() {
        model.close();
    }
}
```

不要在 `eval` 里每次加载模型；那会让吞吐下降几个数量级。

## 17.3 表函数与表聚合

表函数（UDTF）一行输入、多行输出：

```java
public class SplitTags extends TableFunction<String> {
    public void eval(String tags) {
        if (tags == null) return;
        for (String tag : tags.split(",")) {
            collect(tag.trim());
        }
    }
}
```

```sql
SELECT user_id, tag
FROM users, LATERAL TABLE(split_tags(tags)) AS t(tag);
```

表聚合（UDTAF）多行输入、一行多列输出，适合 TopN、分位数等。它比普通 UDAF 更灵活，但状态管理更复杂。

```java
public class TopN extends TableAggregateFunction<String, List<String>> {
    public List<String> createAccumulator() { return new ArrayList<>(); }
    public void accumulate(List<String> acc, String value) { ... }
    public void emitValue(List<String> acc, Collector<String> out) { ... }
}
```

## 17.4 聚合函数

```java
public class WeightedAvg extends AggregateFunction<Double, WeightedAvg.Acc> {
    public static class Acc {
        public double sum;
        public double weight;
    }

    @Override
    public Acc createAccumulator() { return new Acc(); }

    public void accumulate(Acc acc, double value, double weight) {
        acc.sum += value * weight;
        acc.weight += weight;
    }

    @Override
    public Double getValue(Acc acc) {
        return acc.weight == 0 ? null : acc.sum / acc.weight;
    }

    @Override
    public Acc merge(Acc a, Acc b) { ... }
}
```

会话窗口/批合并需要 `merge`。如果聚合不满足结合律，结果可能不稳定。

## 17.5 Process Table Functions（PTF）

Flink 2.1 引入 Process Table Functions，把 ProcessFunction 的能力带进 SQL：

- 有状态；
- 可以注册定时器；
- 可以访问事件时间/watermark；
- 可以产生 0 到多行输出；
- 可以与表参数（table argument）配合。

```java
// 伪代码示意：具体基类/方法签名以所用版本的 PTF API 为准
public class Sessionize extends ProcessTableFunction<Row> {
    public void eval(Context ctx, Row input) {
        // 维护状态、注册定时器、输出会话
    }
}
```

PTF 适合：

- SQL 难以表达的有状态逻辑；
- 自定义窗口/会话；
- 事件驱动的规则；
- 与外部系统交互（仍需异步/限流）。

它比普通 UDF 更强大，但也更难测试和运维。使用前先问：能否用窗口/join/聚合表达？

PTF 在 2.1～2.3 的设计（多表输入、set 语义、状态/定时器、changelog）见第 34 章；用 PTF/模型函数做实时 AI 的工程边界见第 35 章。

## 17.6 Python UDF

PyFlink 适合数据科学和快速开发。完整的运行时架构、依赖分发、UDF 类型、性能模型与生产清单见[附录 I](../appendix/I-pyflink.md)；这里只保留最常用的形状：

```python
from pyflink.table import DataTypes
from pyflink.table.udf import udf

@udf(result_type=DataTypes.STRING())
def mask_phone(phone):
    if phone is None or len(phone) < 7:
        return phone
    return phone[:3] + "****" + phone[-4:]

t_env.create_temporary_system_function("mask_phone", mask_phone)
```

注意：

- Python UDF 有进程间通信开销，吞吐低于 Java；
- 依赖需要安装在工作节点；
- 类型映射要显式声明；
- 大量 UDF 调用会放大延迟；
- 生产环境评估是否值得用 Java 重写。

### PyFlink 的运行时形状

PyFlink 不是“把 Python 解释器嵌进 JVM 算子”这么简单。典型结构是：

```text
JVM TaskManager
  └── Python worker 进程（一个或多个）
        ├── 用户 Python UDF
        ├── pandas/numpy
        └── 第三方依赖
```

JVM 与 Python worker 之间通过网关/序列化交换数据。这带来几个工程约束：

- **进程间通信**：每批数据要跨进程，吞吐低于纯 Java；
- **内存双份**：JVM 和 Python 各有一份对象/缓冲；
- **依赖隔离**：worker 环境必须和作业依赖一致；
- **启动开销**：worker 启动、import 大包会拉长启动时间；
- **故障域**：Python worker 崩溃会表现为算子失败。

PyFlink 也支持 DataStream API：

```python
from pyflink.datastream import StreamExecutionEnvironment

env = StreamExecutionEnvironment.get_execution_environment()
env.set_parallelism(2)
(
    env.from_collection([1, 2, 3])
    .map(lambda x: x * 2)
    .print()
)
env.execute("pyflink-demo")
```

以及 Pandas UDF/向量化 UDF：

```python
from pyflink.table.udf import udf
from pyflink.table import DataTypes
import pandas as pd

@udf(result_type=DataTypes.DOUBLE(), input_types=[DataTypes.DOUBLE()])
def normalize(series: pd.Series) -> pd.Series:
    return (series - series.mean()) / (series.std() or 1.0)
```

向量化 UDF 用 pandas Series 批量处理，通常比逐行 UDF 快，但仍受跨进程和内存影响。

### Python 依赖管理

```bash
# 方式一：集群预装
pip install -r requirements.txt

# 方式二：作业级依赖（部分部署模式支持）
# 把 venv/zip 随作业分发
```

常见配置：

```yaml
python.executable: /opt/venv/bin/python
python.client.executable: /opt/venv/bin/python
python.archives: s3://libs/pyflink-env.zip#env
```

生产建议：

- K8s 上构建自定义镜像，把 Python 依赖固化进去；
- 固定 Python/依赖版本，记录 hash；
- 避免在 UDF 里做重 IO/大模型加载；
- 用向量化 UDF 替代逐行 UDF；
- 对 Python 算子单独做压测和内存评估。

PyFlink 在 2.x 提供异步 UDF/异步 lookup 能力（具体引入版本以文档为准），但异步不等于无限并发；仍要限流、超时和死信。

## 17.7 模型与 AI 函数

Flink 2.1/2.2 增加了模型相关能力：

```sql
CREATE MODEL sentiment_model
INPUT (comment STRING)
OUTPUT (label STRING)
WITH (
  'provider' = 'openai',
  'endpoint' = 'https://api.openai.com/v1/chat/completions',
  'api-key' = '${secret:openai_api_key}',
  'model' = 'gpt-4o-mini',
  'system-prompt' = 'Classify the comment as positive, negative, or neutral. Output only the label.'
);

SELECT id, label
FROM ML_PREDICT(
  TABLE comments,
  MODEL sentiment_model,
  DESCRIPTOR(comment),
  MAP['async', 'true', 'timeout', '30s']
);
```

注意：

- `openai` provider 需要完整的 chat completions 端点（或 embeddings 端点），不是 `/v1/llm/v1/chat`；
- `api-key` 是必填项，生产上应通过密钥管理/环境变量注入，不要写死在 SQL；
- 没有通用的 `'task' = 'text_generation'` 选项，任务类型由端点和 provider 选项决定；
- 不同 provider 的 endpoint/模型/参数不同，以版本文档为准。

`ML_PREDICT` 是一个表值函数：输入表 + 模型 + 特征列，输出“原列 + 模型输出列”。它有异步、并发、超时、输出顺序等配置；只支持 append-only 输入，模型需先在 Catalog 注册。

`VECTOR_SEARCH`（2.2 起，语法以版本 FLIP/文档为准）用于流式向量检索，典型形态是“查询向量 + 检索表 + top-k”：

```sql
-- 语法示意：具体参数以所用版本文档为准
SELECT o.order_id, v.item_id, v.score
FROM orders AS o,
LATERAL TABLE(VECTOR_SEARCH(
  TABLE vector_index,
  o.query_vector,
  DESCRIPTOR(embedding),
  10,
  MAP['async', 'true', 'timeout', '1s']
)) AS v;
```

这些能力的工程注意点：

- 模型调用有延迟和配额，必须异步/限流；
- 结果可能不确定，不能假设幂等；
- 需要缓存/降级；
- 数据隐私和合规；
- 版本管理：模型版本要可追溯。

## 17.8 SQL Gateway

Flink SQL Gateway 提供 REST/JDBC 接口，让外部工具提交 SQL：

```text
BI 工具 / SQL 客户端
        ↓ JDBC/REST
   SQL Gateway
        ↓
   Flink 集群
```

典型用途：

- 交互式 SQL 查询；
- 多租户 SQL 服务；
- 与 BI/调度系统集成；
- 会话管理、语句超时、结果分页。

生产要点：

- 认证/授权（LDAP、Kerberos、mTLS）；
- 会话隔离与资源队列；
- 查询超时和结果大小限制；
- 防止大查询打爆集群；
- 审计 SQL 和用户；
- 限制 DDL/写入权限。

### 会话与语句

SQL Gateway 的模型：

```text
Session（用户/租户）
  ├─ SessionConfig（catalog、并行度、超时）
  ├─ Operation 1（execute statement）
  ├─ Operation 2（fetch results）
  └─ Operation 3（cancel）
```

生产上要为每个租户设置：

- 最大并发 operation；
- 最大结果行数；
- 最大执行时间；
- 可用 catalog/表；
- 资源队列。

## 17.9 客户端生态

- **SQL Client**：命令行交互，适合开发和运维；
- **Flink JDBC Driver**：用 JDBC 协议连接 SQL Gateway；
- **REST API**：提交作业、查询状态、触发 savepoint；
- **Kubernetes Operator**：声明式管理作业；
- **Catalog**：Hive、JDBC、Paimon、Iceberg 等共享元数据。

客户端不是“附属品”。一个成熟的实时平台，提交、权限、元数据、版本、回滚都要通过客户端层治理。

## 17.10 UDF 的工程化

- UDF 代码独立仓库/模块，版本化；
- 单元测试覆盖 null、边界、异常；
- 依赖瘦身，避免冲突；
- 给 UDF 加指标（调用次数、失败数、耗时）；
- 外部调用设置超时、重试、熔断；
- 避免在 UDF 中做重初始化（连接、模型加载放 `open`）；
- 评估 UDF 对 checkpoint 和序列化的影响；
- 给 UDF 设置稳定的类名/版本，避免 savepoint 不兼容。

```java
private transient Counter calls;
private transient Counter failures;

@Override
public void open(FunctionContext ctx) {
    calls = ctx.getMetricGroup().counter("udf_calls");
    failures = ctx.getMetricGroup().counter("udf_failures");
}
```

一个容易踩的坑：planner 做常量折叠时可能用 `ConstantFunctionContext` 调用 `open`，此时 `getMetricGroup()` 会抛 `UnsupportedOperationException`。如果 UDF 在 `open` 里注册指标，要么捕获异常/延迟注册，要么让函数非确定性（并接受无法常量折叠的代价）。

## 17.11 安全与治理

UDF 是 SQL 平台最大的攻击面之一：

- 任意代码执行；
- 读取本地文件/网络；
- 消耗 CPU/内存；
- 泄漏数据；
- 依赖漏洞。

治理手段：

- UDF 白名单/审核；
- 独立 classloader/沙箱；
- 资源限制（CPU、内存、超时）；
- 禁止危险 API（`Runtime.exec`、文件系统）；
- 依赖扫描；
- 审计调用。

## 17.12 MiniFlink 的边界

MiniFlink 的 Table 层依赖 SQLite 内置函数，没有 UDF 注册、Python UDF、SQL Gateway。DataStream 层可以直接用 Python 函数，但那是算子函数，不是 SQL UDF。生产 UDF 的语义以 Flink 文档为准。MiniFlink 的 Table 实现见[第 32 章](32-miniflink-table-cep.md)。

## 17.13 SQL Client、JDBC Driver 与 Gateway 落地

17.8 讲的是 Gateway 的模型，这一节补“实际怎么连”。

### 17.13.1 SQL Client

SQL Client 是最轻的入口：

```bash
./bin/sql-client.sh

# 执行一个 SQL 文件
./bin/sql-client.sh -f job.sql

# 先执行初始化文件，再进入交互模式
./bin/sql-client.sh -i init.sql

# 加载连接器/UDF jar
./bin/sql-client.sh -j lib/flink-connector-kafka.jar
```

常用会话命令：

```sql
SET 'pipeline.name' = 'orders-sql';
SET 'parallelism.default' = '8';
SET 'execution.runtime-mode' = 'streaming';
RESET 'parallelism.default';

EXPLAIN SELECT ...;
DESCRIBE orders;
SHOW TABLES;
USE CATALOG paimon;
```

结果模式（`table`/`changelog`/`tableau`）影响展示，不改变语义。排查计划时用 `EXPLAIN`，不要只看结果。

SQL Client 的局限：

- 会话是本地进程，关闭即结束；
- 不适合多租户/远程接入；
- 大结果集会占客户端内存；
- 依赖/classpath 由启动参数决定，容易与集群不一致。

### 17.13.2 SQL Gateway

Gateway 把 SQL 能力变成服务：

```text
BI / JDBC / REST 客户端
        │
        ▼
   SQL Gateway（REST/JDBC）
        │
        ▼
   Flink 集群
```

REST 的资源模型：

```text
POST /v1/sessions                         创建会话
POST /v1/sessions/{session}/statements    提交语句
GET  /v1/sessions/{session}/operations/{op}/result/{token}  拉取结果
POST /v1/sessions/{session}/operations/{op}/cancel          取消
DELETE /v1/sessions/{session}             关闭会话
```

典型配置：

```yaml
sql-gateway.endpoint.rest.address: 0.0.0.0
sql-gateway.endpoint.rest.port: 8083
sql-gateway.session.max-num: 100
sql-gateway.session.idle-timeout: 10min
sql-gateway.session.check-interval: 1min
```

`sql-gateway.session.max-num` 是活跃会话上限，`idle-timeout` 控制空闲回收；单条语句的超时通过 REST 请求体的 `executionTimeout`（毫秒，驼峰）传入，而不是一个全局的 `operation.timeout` 键。Gateway 的请求体字段风格并不完全统一，写客户端时要以所用版本的 OpenAPI/源码为准。生产要点：

- Gateway 无状态/可水平扩展，会话状态要有持久化或粘性路由；
- 每个租户限制会话数、并发 operation、结果行数、执行时间；
- 认证放在反向代理/OIDC/网关注入，不要裸露 REST；
- 审计 SQL、用户、耗时、扫描量；
- 限制 DDL/写入权限，防止误建表/误写；
- 大查询要限流/排队，避免打爆集群。

### 17.13.3 JDBC Driver

Flink 提供 JDBC Driver，让 BI 工具/SQL 客户端通过标准 JDBC 连接 Gateway：

```text
jdbc:flink://gateway-host:8083
driver: org.apache.flink.table.jdbc.FlinkDriver
```

使用前要确认：

- 驱动版本与 Flink/Gateway 版本匹配；
- Gateway 已启用 REST endpoint；
- 认证/ TLS 由 Gateway 或反向代理处理；
- 驱动只支持 Gateway 暴露的 SQL 子集，不是完整数据库协议；
- 元数据查询、事务、游标行为与关系库不同，BI 工具要做兼容测试。

常见问题：

| 现象 | 原因 | 处理 |
|---|---|---|
| `No suitable driver` | 驱动未加载/URL 前缀错 | 检查 classpath 与 `jdbc:flink://` |
| 连接超时 | Gateway/REST 地址/端口 | 检查 endpoint、网络策略 |
| 查询很慢 | 结果全量拉取/无 limit | 加 limit、分页、超时 |
| 元数据为空 | 驱动/Catalog 不支持 | 确认 Catalog 与驱动版本 |
| 认证失败 | Gateway 未配认证/令牌过期 | 检查 OIDC/TLS/令牌 |

### 17.13.4 选型：Client vs Gateway vs JDBC

| 场景 | 推荐 | 原因 |
|---|---|---|
| 本地开发/调试 | SQL Client | 零部署、直接看计划 |
| 运维/一次性任务 | SQL Client + `-f` | 简单、可脚本化 |
| 多租户 SQL 服务 | Gateway REST | 会话隔离、限流、审计 |
| BI 工具接入 | Gateway + JDBC | 标准协议、生态兼容 |
| 程序化提交 | REST/Java API | 可编排、可观测 |

不要用 SQL Client 做生产调度，也不要把 Gateway 当成“无限并发的查询引擎”。它们都是入口，资源治理和权限仍在平台层。

## 本章小结

UDF 把业务逻辑带进 SQL，SQL Gateway 把 SQL 能力开放给外部系统。两者都扩大了攻击面和运维复杂度，需要认证、限流、测试和版本管理。

下一章讨论流批一体：同一套 SQL/API 如何根据输入边界选择不同的执行策略，以及批模式在容错、调度和性能上的差异。
