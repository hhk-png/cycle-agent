# 第24章 工程化开发、测试与 CI/CD

## 24.1 从“能跑”到“可维护”

一个 Flink 作业上线后，会经历：

- 需求变更；
- 状态 schema 演进；
- Flink/连接器升级；
- 流量增长；
- 故障恢复；
- 多环境部署。

如果没有工程化，代码会变成“只有作者敢动”的黑盒。工程化至少包括：项目结构、依赖管理、测试、CI、发布、回滚、监控。

```text
开发 → 单测 → 集成测试 → 构建镜像 → 预发 → savepoint 升级 → 灰度 → 生产
  ↑                                                        │
  └──────────────────── 回滚/复盘 ←────────────────────────┘
```

## 24.2 项目结构

推荐按职责拆分：

```text
flink-job/
├── pom.xml
├── src/main/java/com/acme/realtime/
│   ├── Job.java
│   ├── source/
│   ├── transform/
│   ├── sink/
│   ├── state/
│   └── udf/
├── src/main/resources/
│   ├── log4j2.properties
│   └── job.properties
└── src/test/java/
```

原则：

- `main()` 只负责组装，业务逻辑放可测试的类；
- source/sink/transform 分离；
- 配置外置，不写死地址；
- 版本号、作业名、uid 统一管理；
- 避免一个类几千行。

### 可测试的算子

```java
// 好：纯逻辑 + 可单测
public class AmountFilter implements FilterFunction<Order> {
    private final BigDecimal min;
    public AmountFilter(BigDecimal min) { this.min = min; }
    public boolean filter(Order o) { return o.amount().compareTo(min) >= 0; }
}

// 差：所有逻辑塞进匿名类，无法复用/测试
stream.filter(o -> {
    // 200 行
});
```

## 24.3 依赖与打包

- Flink 依赖用 `provided`，不要打进 fat jar；
- 连接器版本与 Flink 版本严格匹配；
- 使用 shade 重定位冲突依赖；
- 控制 jar 大小；
- 排除日志绑定冲突（log4j/slf4j）；
- 锁定版本，生成依赖清单。

```xml
<dependency>
  <groupId>org.apache.flink</groupId>
  <artifactId>flink-streaming-java</artifactId>
  <version>${flink.version}</version>
  <scope>provided</scope>
</dependency>
```

### 依赖冲突的典型症状

- `NoSuchMethodError`；
- `ClassNotFoundException`；
- `IllegalAccessError`；
- 序列化器行为不一致；
- 连接器版本不匹配导致 checkpoint 失败。

排查：

```bash
mvn dependency:tree
mvn dependency:analyze
```

## 24.4 单元测试

纯函数最容易测。把业务计算抽成不依赖 Flink 运行时的类/静态方法，测试就不需要 MiniCluster：

```java
final class AmountMath {
    static int add(int a, int b) { return a + b; }
}

assertEquals(30, AmountMath.add(10, 20));
```

有状态/时间逻辑用 Flink 的测试工具：

- `MiniClusterWithClientResource`：启动本地迷你集群（JUnit 4 风格）；JUnit 5 用 `MiniClusterExtension` + `@RegisterExtension`；
- `OneInputStreamOperatorTestHarness`：测试算子状态、watermark、timer；
- `KeyedOneInputStreamOperatorTestHarness`：测试键控算子；
- `TestHarness` 可以注入 watermark、处理时间、快照。

```java
OneInputStreamOperatorTestHarness<Event, Result> harness =
    new OneInputStreamOperatorTestHarness<>(new MyOperator(), ...);
harness.open();
harness.processElement(new StreamRecord<>(event, timestamp));
harness.processWatermark(new Watermark(1000));
assertThat(harness.extractOutputValues()).containsExactly(...);
```

测试要覆盖：

- 正常路径；
- 空值/异常；
- 迟到数据；
- watermark 推进；
- 状态恢复；
- checkpoint 前后结果一致。

### 测试时间

Flink 的 TestHarness 可以控制处理时间：

```java
harness.setProcessingTime(1000);
harness.processElement(...);
```

这避免测试依赖真实时钟，减少 flaky。

### 测试快照

```java
harness.snapshot(1, 1000);
// 恢复
harness.restore(harness.snapshot(1, 1000));
```

测试状态恢复是发现 schema/序列化问题的有效方式。

### 一个完整的 TestHarness 例子

```java
import org.apache.flink.api.common.ExecutionConfig;
import org.apache.flink.api.common.typeinfo.TypeInformation;
import org.apache.flink.api.common.typeinfo.Types;
import org.apache.flink.runtime.state.OperatorSubtaskState;
import org.apache.flink.streaming.api.operators.KeyedProcessOperator;
import org.apache.flink.streaming.util.KeyedOneInputStreamOperatorTestHarness;
import static org.assertj.core.api.Assertions.assertThat;

class DedupTest {
    @Test
    void deduplicatesWithinState() throws Exception {
        // 构造器要 TypeSerializer，不是 TypeInformation；
        // KeyedProcessFunction 要用 Keyed 版 harness + KeyedProcessOperator。
        TypeInformation<Event> eventType = Types.POJO(Event.class);
        ExecutionConfig config = new ExecutionConfig();

        KeyedOneInputStreamOperatorTestHarness<String, Event, Event> harness =
            new KeyedOneInputStreamOperatorTestHarness<>(
                new KeyedProcessOperator<>(new DeduplicateFunction()),
                event -> "k",
                Types.STRING);
        harness.setup(eventType.createSerializer(config));
        harness.open();

        harness.processElement(new Event("e1", 1000), 1000);
        harness.processElement(new Event("e1", 1001), 1001);

        assertThat(harness.extractOutputValues()).hasSize(1);

        // 快照 → 恢复 → 再处理
        OperatorSubtaskState snapshot = harness.snapshot(1L, 1000L);
        harness.close();

        KeyedOneInputStreamOperatorTestHarness<String, Event, Event> restored =
            new KeyedOneInputStreamOperatorTestHarness<>(
                new KeyedProcessOperator<>(new DeduplicateFunction()),
                event -> "k",
                Types.STRING);
        restored.setup(eventType.createSerializer(config));
        restored.initializeState(snapshot);
        restored.open();
        restored.processElement(new Event("e1", 1002), 1002);
        assertThat(restored.extractOutputValues()).isEmpty();
    }
}
```

### MiniCluster 集成测试

```java
import org.apache.flink.runtime.testutils.MiniClusterResourceConfiguration;
import org.apache.flink.test.util.MiniClusterWithClientResource;

class PipelineITCase {
    @ClassRule
    public static MiniClusterWithClientResource cluster =
        new MiniClusterWithClientResource(
            new MiniClusterResourceConfiguration.Builder()
                .setNumberTaskManagers(1)
                .setNumberSlotsPerTaskManager(2)
                .build());

    @Test
    void endToEnd() throws Exception {
        StreamExecutionEnvironment env = StreamExecutionEnvironment.getExecutionEnvironment();
        env.setParallelism(2);
        // 构建 source → process → sink，收集结果并断言
        env.execute("it-case");
    }
}
```

MiniCluster 测试比单元测试慢，但能覆盖序列化、网络、checkpoint、并行度等真实行为。关键作业至少要有：

- 一个端到端 MiniCluster 测试；
- 一个 savepoint 恢复测试；
- 一个故障注入测试（kill TM / 抛异常）。

## 24.5 集成测试

- Kafka/MySQL 用 Testcontainers；
- 端到端跑 source→transform→sink；
- 校验 exactly-once（重复执行、故障注入）；
- 用真实 schema 和倾斜数据；
- 记录基线指标（吞吐、延迟、状态大小）。

```java
@Container
static KafkaContainer kafka = new KafkaContainer(DockerImageName.parse("confluentinc/cp-kafka:7.6.0"));
```

集成测试要覆盖：

- 正常链路；
- 重启/恢复；
- checkpoint 失败；
- 脏数据；
- schema 变化；
- 下游限流。

## 24.6 CI/CD

流水线：

```text
提交 → 编译 → 单测 → 集成测试 → 镜像/制品 → 预发部署 → 冒烟 → 生产灰度
```

关键检查：

- 代码格式/静态分析；
- 依赖漏洞扫描；
- 作业图 diff（uid、并行度、状态变化）；
- SQL 计划 diff；
- savepoint 兼容性测试；
- 配置校验；
- 回滚脚本。

### 作业图 diff

```bash
# 只解析、不提交（不会连接集群执行作业）
flink info target/app.jar
```

或者在代码里导出计划：

```java
String plan = env.getExecutionPlan();   // JSON
```

对比：

- 算子 uid 是否变化；
- 并行度是否变化；
- 状态是否新增/删除；
- 连接器参数是否变化。

任何 uid 变化都可能导致 savepoint 无法恢复。

### SQL 计划 diff

```sql
EXPLAIN SELECT ...;
```

在 CI 中保存基线计划，对比：

- 是否多了 shuffle；
- 是否退化了 join；
- 是否状态变大。

## 24.7 发布与回滚

- 使用 savepoint 升级；
- 灰度/双跑对比；
- 监控关键指标；
- 保留旧版本镜像和 savepoint；
- 明确回滚条件；
- 不要在高峰期做大规模状态变更。

```text
1. 触发 savepoint
2. 停止旧作业
3. 用新镜像从 savepoint 恢复
4. 观察 5～30 分钟
5. 异常则回滚到旧镜像 + 旧 savepoint
```

回滚不是“再跑一次旧代码”这么简单：如果新作业已经写入了下游，旧作业恢复后可能重复/冲突。要设计下游幂等/去重。

## 24.8 配置与环境

- 开发/测试/生产分离；
- 敏感配置用 Secret；
- 环境差异用配置覆盖，不复制代码；
- 记录每次发布的配置快照；
- 配置变更也要走 review。

```yaml
# application-prod.yaml
kafka.bootstrap: kafka-prod:9092
checkpoint.dir: s3://prod/checkpoints
```

配置优先级要明确：代码默认值 < 配置文件 < 环境变量 < 命令行。不要让生产值藏在镜像里。

## 24.9 代码评审清单

- 是否有稳定 uid？
- 状态是否有 TTL/清理？
- watermark 策略是否合理？
- 是否有同步外部调用？
- sink 语义是否明确？
- 异常/脏数据是否有侧输出？
- 关键指标/日志是否齐全？
- 测试是否覆盖恢复和迟到？
- 资源/并行度是否评估？
- 回滚方案是否可行？

## 24.10 MiniFlink 的测试

MiniFlink 用 pytest 覆盖：

- API 转换；
- 窗口/水位线；
- 状态/定时器；
- checkpoint/恢复；
- 连接器；
- Table/SQL；
- CEP；
- 指标/CLI。

```bash
python -m pytest -q
```

它没有 JVM 生态的 TestHarness，但测试思想相同：构造输入、推进时间、断言输出、验证快照。

## 24.11 测试金字塔与 Flaky 治理

Flink 作业的测试很容易走向两个极端：只测纯函数（覆盖不到状态/时间/序列化），或所有用例都起 MiniCluster（慢、脆、难定位）。一个可维护的测试组合应该分层。

### 五层测试

| 层 | 测什么 | 速度 | 能抓到的 bug |
|---|---|---|---|
| 纯逻辑单测 | 计算、规则、解析 | 毫秒 | 业务逻辑、边界、空值 |
| Operator Harness | 单个算子的状态/timer/watermark | 百毫秒 | 状态泄漏、timer、侧输出 |
| MiniCluster | 拓扑、序列化、并行度、checkpoint | 秒 | 链化、序列化、恢复 |
| Testcontainers | Kafka/DB/对象存储集成 | 十秒 | 连接器、事务、schema |
| 预发/影子 | 真实流量、故障注入 | 分钟 | 容量、运维、端到端语义 |

原则：**越往下越多、越快；越往上越少、越真**。不要把“所有测试都起集群”当作严谨。

### 每层应该覆盖什么

**纯逻辑单测**：

- 金额/精度、时区、空值、边界；
- 规则匹配、解析失败、脏数据；
- 不依赖 Flink 运行时，最快、最稳定。

**Operator Harness**：

- `KeyedProcessFunction` 的 timer、状态、侧输出；
- 窗口的触发/清理/迟到；
- checkpoint 前后状态一致；
- 用 `setProcessingTime` 控制时钟，避免真实等待。

**MiniCluster**：

- source → transform → sink 的端到端；
- 并行度/分区/序列化；
- checkpoint/restore；
- uid/拓扑变化；
- 不需要外部系统时，用内存 source/sink。

**Testcontainers**：

- Kafka 事务/read_committed；
- JDBC upsert/批量；
- 湖仓提交/小文件；
- schema 演进。

**预发/影子**：

- 峰值压测、故障注入、扩缩容；
- 真实数据倾斜；
- 对账/回滚。

### Savepoint 兼容性必须进 CI

Flink 作业的升级风险主要不在编译，而在状态/拓扑兼容。CI 至少要做：

1. 用旧版本作业跑出 savepoint（或固定一份测试 savepoint）；
2. 用新代码从 savepoint 恢复；
3. 校验算子 uid、状态大小、关键输出；
4. 对比作业图（uid/并行度/状态）。

```text
旧代码 + 输入 → savepoint → 新代码 restore → 输出
                              │
                              └─ 与“旧代码直接跑”的输出对拍
```

没有这一步，“编译通过”不代表能上线。

### 确定性与 Flaky

流测试的 flaky 大多来自“时间”和“共享状态”：

| 来源 | 表现 | 处置 |
|---|---|---|
| 真实时钟 | 处理时间 timer 偶尔不触发 | 注入 clock/TestHarness 时间 |
| 线程/异步 | 输出顺序不稳定 | 按 key 排序后断言，或测集合 |
| 端口/容器 | 启动失败、连接拒绝 | 动态端口、健康检查、重试 |
| 外部服务 | 超时/限流 | 契约测试/ mock，集成测试单独跑 |
| 并行测试 | 共享 topic/表 | 唯一前缀、隔离 namespace |
| checkpoint 时序 | 有时恢复点不同 | 固定微批/手动触发 checkpoint |
| 随机/UUID | 输出不可复现 | 固定种子/注入 id 生成器 |

Flaky 测试不能长期“重跑三次”。要么修确定性，要么隔离到独立流水线，并记录 quarantine 原因和 owner。

### 测试数据设计

至少覆盖：

- 空输入、单条、重复 key；
- 乱序、迟到、超过 allowedLateness；
- null/空字符串/非法格式；
- 金额边界、时区边界、闰秒/夏令时（如适用）；
- 倾斜 key（单 key 占 30%+）；
- schema 新增/删除/改类型；
- 恢复点前后各一条记录。

不要只用“均匀、干净、有序”的样例数据。生产事故几乎都来自边界。

### CI 流水线形状

```text
提交
  → 编译/静态检查
  → 纯逻辑单测（并行）
  → Operator Harness（并行）
  → MiniCluster 端到端（少量）
  → 容器集成（ nightly / 合并前）
  → 作业图 diff + savepoint 恢复
  → 镜像/制品
  → 预发/影子
```

时间预算：

- 单测/Harness：秒级，每次提交跑；
- MiniCluster：分钟级，合并前跑；
- 容器/恢复：十分钟级，nightly 或发布前跑；
- 压测/故障注入：小时级，版本发布前跑。

### MiniFlink 的测试启示

MiniFlink 的 156 个测试是“语义级测试”的样板：

- 用公开 API 构造输入，不依赖内部字段；
- 显式推进 watermark/时间；
- checkpoint → restore → 继续处理 → 断言；
- 对每个审计发现的 bug 留回归用例；
- 明确记录“测试没覆盖的边界”（附录 C）。

生产 Flink 作业可以借鉴同样的结构，只是把执行器换成 TestHarness/MiniCluster/Testcontainers。

### 检查清单

- [ ] 是否有纯逻辑单测，而不是所有逻辑都塞进算子？
- [ ] 关键算子是否有 Harness 测试（状态/timer/watermark）？
- [ ] 是否有一个端到端 MiniCluster 测试？
- [ ] savepoint 恢复是否在 CI 中自动验证？
- [ ] 测试是否注入时钟/随机种子，避免 flaky？
- [ ] 容器/外部依赖是否隔离，失败可定位？
- [ ] 是否覆盖迟到/重复/空值/倾斜/schema 变化？
- [ ] Flaky 用例是否有 owner 和退出机制？

## 24.12 依赖冲突、类加载与作业制品

Flink 作业的很多“玄学故障”最终都落在类加载上：本地 IDE 跑得好好的，提交到集群就 `NoSuchMethodError`；两个连接器单独都能用，放一起就 `ClassNotFoundException`；升级一个库，另一个 UDF 开始报 `LinkageError`。这不是 Flink 特有，但 Flink 的多层 classloader 让问题更隐蔽。

### 24.12.1 类加载器层次

```text
Bootstrap / Platform ClassLoader
        │
Flink Core ClassLoader（flink-dist、集群 lib/）
        │
User Code ClassLoader（每个作业/每个 slot 的用户 jar）
        │
Dynamic ClassLoader（SQL Client、UDF、连接器按需加载）
```

默认策略是 **child-first**：用户 jar 里的类优先于集群 `lib/` 里的同名类。这样作业可以带自己的依赖，但也会带来“同一个类被加载两次”的问题——两个 `Class` 对象不相等，`instanceof`、静态变量、单例全部失效。

相关配置：

```yaml
# 默认 child-first；改成 parent-first 会让集群 lib/ 优先
classloader.resolve-order: child-first

# 额外指定必须 parent-first 的前缀（分号分隔）
classloader.parent-first-patterns.additional: "com.acme.spi.;org.apache.kafka."

# 检查 classloader 泄漏（默认 true）
classloader.check-leaked-classloader: true
```

Flink 内置一组默认 parent-first 前缀（Java、Flink 核心、日志等），不要随意把业务包加进去。把 `com.acme.spi.` 设成 parent-first，意味着用户 jar 里的同名 SPI 不再生效，常见于“接口在集群、实现在作业”的插件模型。

### 24.12.2 典型症状与根因

| 症状 | 常见根因 | 排查 |
|---|---|---|
| `NoSuchMethodError` | 两个版本的同一库，运行时选了旧的 | `mvn dependency:tree`、`-verbose:class` |
| `ClassNotFoundException` | 依赖没进作业 jar/classpath | 检查 `provided`、`-C`、`pipeline.classpaths` |
| `LinkageError` | 类被两个 classloader 加载 | 看异常里的 classloader 名 |
| `IllegalAccessError` | 模块/包可见性/版本不匹配 | 对齐依赖版本 |
| 静态状态不共享 | child-first 加载了两份 | 改为 parent-first 或去掉重复 jar |
| `ServiceConfigurationError` | `META-INF/services` 被覆盖 | 用 `ServicesResourceTransformer` |
| Metaspace OOM | 反复提交作业、classloader 泄漏 | `classloader.check-leaked-classloader`、重启 TM |

`NoSuchMethodError` 的经典场景是 Guava、Jackson、Netty、Kafka client、Protobuf、Log4j/SLF4J。它们既是 Flink 内部依赖，也是连接器/UDF 的依赖；版本不一致时，编译期没问题，运行期按 classloader 顺序选中一个。

### 24.12.3 诊断方法

1. **先看完整异常链**：`NoSuchMethodError` 后面通常有“which classloader loaded which class”的提示；
2. **打印依赖树**：

   ```bash
   mvn dependency:tree -Dincludes=com.google.guava:guava
   mvn dependency:tree -Dverbose > deps.txt
   ```

3. **打印类加载来源**：JVM 加 `-verbose:class`，或在代码里打印 `X.class.getClassLoader()`；
4. **对比集群与作业 jar**：`jar tf` 看同名类是否两边都有；
5. **最小复现**：只保留一个连接器/UDF，逐个加回，定位冲突源；
6. **看 TaskManager 日志**：classloader 泄漏检查会打印“leaked classloader”及创建堆栈。

不要靠“把冲突的 jar 删掉试试”。先确定是哪个类、从哪个 classloader 加载、期望哪个版本，再决定 shade、排除还是 parent-first。

### 24.12.4 打包策略

| 依赖 | 打包方式 | 原因 |
|---|---|---|
| `flink-core`、`flink-streaming-java`、`flink-table-*` | `provided` | 集群已提供，打进去会冲突 |
| 连接器官方 jar | 放 `lib/` 或 `--jar` | 与集群版本匹配 |
| 第三方 SDK | shade + relocate | 避免与集群/其他作业冲突 |
| 日志实现 | `provided` 或统一版本 | 多份 SLF4J binding 会告警/失效 |
| Service 文件 | `ServicesResourceTransformer` | 合并而不是覆盖 |
| `reference.conf` | `AppendingTransformer` | Typesafe Config 需要合并 |

Maven shade 的关键配置：

```xml
<transformers>
  <transformer implementation="org.apache.maven.plugins.shade.resource.ServicesResourceTransformer"/>
  <transformer implementation="org.apache.maven.plugins.shade.resource.AppendingTransformer">
    <resource>reference.conf</resource>
  </transformer>
</transformers>
```

relocate 的例子：

```xml
<relocation>
  <pattern>com.google.common</pattern>
  <shadedPattern>com.acme.shaded.guava</shadedPattern>
</relocation>
```

relocate 不是万能的：反射、字符串类名、JNI、`META-INF/services` 里的全限定名都可能绕过重定位。对复杂 SDK，优先使用官方 shaded 版本，或在独立 classloader/独立作业中隔离。

### 24.12.5 制品分发路径

Flink 的“作业制品”不止一个 jar：

```text
作业 jar（业务代码 + UDF）
连接器 jar（Kafka/JDBC/Paimon/...）
格式 jar（Avro/Protobuf/...）
配置文件（flink-conf.yaml、log4j2.properties）
Python 依赖（PyFlink，见附录 I.6）
```

分发方式：

- **Session 集群**：连接器放 `lib/`，作业 jar 用 `flink run` 提交；
- **Application 模式**：`pipeline.jars`/`pipeline.classpaths` 指定，或打进镜像；
- **Kubernetes Operator**：`kubernetes.artifacts.*` 拉取远程制品，或镜像内置；
- **SQL Client**：`ADD JAR`、`-j`、`CREATE FUNCTION ... USING JAR`；
- **PyFlink**：`--pyFiles`、`python.files`、镜像预装（附录 I.6）。

生产建议：

- 作业镜像不可变，jar 版本与镜像 tag 绑定；
- 连接器版本写进制品清单，和 Flink 版本一起做兼容矩阵；
- 不要把凭证、测试数据、IDE 配置打进 jar；
- 制品的 SHA/签名进发布记录，回滚时能取到同一份。

### 24.12.6 类加载泄漏

每次提交作业都会创建 User Code ClassLoader。如果作业取消后 classloader 仍被引用（线程、Timer、JDBC driver、Metrics reporter、静态缓存），Metaspace 会持续增长，最终 OOM。

```yaml
classloader.check-leaked-classloader: true
taskmanager.memory.jvm-metaspace.size: 512m
```

常见泄漏源：

- 用户代码启动的线程没有关闭；
- 连接池/客户端没有 `close()`；
- 注册到全局 Metrics/静态注册表的对象；
- JDBC driver 被 `DriverManager` 持有；
- 日志 appender 持有 classloader；
- 自定义 `Cleaner`/`Finalizer` 引用。

排查：观察 TaskManager 的 Metaspace 使用、频繁 Full GC、`leaked classloader` 日志；用 `jcmd <pid> VM.classloader_stats`（以 JDK 版本为准）看 classloader 数量。长期运行的多租户集群要限制作业反复提交，并在升级/取消后验证资源回收。

### 24.12.7 检查清单

- [ ] Flink 核心依赖是 `provided`，没有打进 fat jar；
- [ ] 连接器/格式 jar 的版本与 Flink 版本在兼容矩阵内；
- [ ] 冲突的第三方库已 shade + relocate，或使用官方 shaded 版本；
- [ ] `META-INF/services` 与 `reference.conf` 正确合并；
- [ ] 没有多份 SLF4J binding / 日志实现；
- [ ] 类加载顺序（child-first/parent-first）有明确理由；
- [ ] 制品清单包含 jar、连接器、格式、配置、Python 依赖；
- [ ] 作业取消后 Metaspace/classloader 能回收；
- [ ] CI 有“最小集群提交”测试，而不是只在本机跑；
- [ ] 回滚能取到与发布时完全相同的制品。

类加载问题的成本很高，因为它通常不在编译期暴露，也不在单测里出现。把依赖边界、打包规则和制品清单当成接口来设计，才能让“本地能跑”变成“集群也能跑”。

## 24.13 SQL 与连接器测试

24.4/24.5 讲的是 DataStream 作业的测试。SQL 作业和自定义连接器有额外的测试面：**计划**和**契约**。

### 24.13.1 SQL 单测

```java
TableEnvironment tEnv = TableEnvironment.create(
    EnvironmentSettings.inStreamingMode());

tEnv.executeSql("CREATE TABLE orders (...) WITH ('connector'='datagen', ...)");
tEnv.executeSql("CREATE TABLE sink (...) WITH ('connector'='collect', ...)");

TableResult result = tEnv.executeSql(
    "INSERT INTO sink SELECT user_id, SUM(amount) FROM orders GROUP BY user_id");
// 有界/批模式下可以 wait；流式作业要配合测试 sink 或 MiniCluster
```

SQL 单测的重点不是“能不能跑”，而是：

- schema/类型是否正确；
- 聚合/join/去重的结果是否符合预期；
- changelog 模式是否与 sink 匹配；
- 时间属性/watermark 是否生效；
- 边界：null、空表、重复 key、迟到。

### 24.13.2 计划测试

`EXPLAIN` 可以发现“语义对了但计划错了”：

```java
String plan = tEnv.explainSql(
    "SELECT id FROM orders WHERE amount > 100 LIMIT 10");

assertThat(plan).contains("Calc");
// 断言下推标记、算子名、状态算子，而不是整段字符串
```

计划文本随版本变化，断言要针对稳定片段：

- 连接器是否出现在 source/sink；
- `Filter`/`Calc` 是否被下推；
- join 是否选择了预期的策略（lookup/interval/temporal）；
- 是否出现了不该有的 `SinkUpsertMaterializer`；
- 是否发生了全表扫描。

计划回归测试适合放进 CI，但要接受“升级 Flink 时计划会变”。做法是：把计划快照作为“需要人工确认的 diff”，而不是自动失败后直接改快照。

### 24.13.3 连接器测试

| 层 | 工具 | 重点 |
|---|---|---|
| 工厂 | JUnit + `FactoryUtil` | 选项、必填、默认值、service 发现 |
| Source | `SourceOperatorTestHarness` | split 分配、poll、watermark、snapshot |
| Sink | `SinkWriter` 单测 | write/flush/prepareCommit/abort |
| 端到端 | `MiniCluster`/`MiniClusterExtension` | 拓扑、序列化、checkpoint/restore |
| 外部系统 | Testcontainers | 事务、schema、限流、故障 |

Source 的测试要覆盖：

- 分片发现/分配/回收（`addSplitsBack`）；
- 空闲分片的 watermark；
- 有界 source 的 `END_OF_INPUT`；
- checkpoint 时 offset/分片状态；
- 并行度变化后的重分配。

Sink 的测试要覆盖：

- 正常写入与 flush；
- `prepareCommit` 返回的句柄可序列化；
- checkpoint 完成后的 commit 幂等；
- 失败/取消时的 abort；
- 重复 commit 不产生重复数据；
- 事务超时/恢复。

### 24.13.4 用 Testcontainers 做集成测试

```java
@Testcontainers
class KafkaConnectorITCase {

    @Container
    static KafkaContainer kafka = new KafkaContainer(DockerImageName.parse("confluentinc/cp-kafka:7.6.0"));

    @Test
    void should_read_and_write_with_eos() {
        // 建 topic、写测试数据、跑 MiniCluster、校验 read_committed
    }
}
```

注意：

- 容器镜像版本要固定，避免“今天能跑明天不能跑”；
- 每个测试用唯一 topic/表前缀，避免并行冲突；
- 容器启动慢，放在 nightly/合并前，不要每次提交都跑；
- 失败要能区分“连接器 bug”和“容器/网络问题”。

### 24.13.5 契约测试

连接器的契约包括：

- `ChangelogMode` 与真实输出一致；
- 下推的 filter/projection 不改变结果；
- 元数据列顺序/类型正确；
- watermark 不被吞掉；
- savepoint/restore 后 offset/事务状态正确；
- rescale 后分片/状态不丢不重。

把这些写成测试，而不是写在文档里。连接器升级时，契约测试是最早的预警。

### 24.13.6 检查清单

- [ ] SQL 逻辑有单测，不依赖真实集群；
- [ ] 关键查询有计划断言（下推/join/状态算子）；
- [ ] 连接器工厂有选项/服务发现测试；
- [ ] Source/Sink 有 harness 级测试；
- [ ] 有 MiniCluster 端到端测试；
- [ ] 外部系统用 Testcontainers，版本固定；
- [ ] checkpoint/restore、rescale、故障注入覆盖；
- [ ] 计划快照变更需要人工确认；
- [ ] 测试数据覆盖 null/空/重复/迟到/倾斜；
- [ ] 失败能区分连接器、Flink、外部系统。

## 本章小结

工程化不是“多写文档”，而是让作业可测试、可发布、可回滚、可观测。Flink 作业的状态和拓扑让升级比普通服务更危险，必须把 savepoint 兼容性纳入 CI。从兼容性门禁到标准升级 Runbook、蓝绿/金丝雀和回滚决策，见[附录 P](../appendix/P-release-change-management.md)。

下一章讨论 Kubernetes 与 Operator：如何把作业生命周期声明式化，以及升级、弹性、存储和权限的工程细节。
