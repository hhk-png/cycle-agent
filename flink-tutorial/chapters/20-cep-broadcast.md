# 第20章 CEP 与广播状态

## 20.1 CEP 适合什么

复杂事件处理（CEP）在事件流中匹配模式：

- 5 分钟内连续 3 次登录失败；
- 下单后 30 分钟未支付；
- 先大额转账，再修改密码，再异地登录；
- 设备温度连续 10 分钟超阈值。

这些问题的共同点：**不只看单条事件，而是看事件之间的顺序、时间间隔和组合**。用 ProcessFunction 也能写，但 CEP 把模式表达、状态管理和超时处理封装了起来。

```text
单条事件规则   → filter / map
窗口聚合规则   → window
事件序列规则   → CEP
跨流关联规则   → join
动态规则       → broadcast state + 规则引擎
```

## 20.2 Pattern API

```java
Pattern<Event, ?> pattern = Pattern.<Event>begin("first")
    .where(new SimpleCondition<Event>() {
        public boolean filter(Event event) { return event.isLoginFailed(); }
    })
    .next("second")
    .where(event -> event.isLoginFailed())
    .next("third")
    .where(event -> event.isLoginFailed())
    .within(Duration.ofMinutes(5));

PatternStream<Event> patternStream = CEP.pattern(stream.keyBy(Event::userId), pattern);
```

关键语义：

- `next`：严格连续，中间不能有非匹配事件；
- `followedBy`：非严格，允许中间有其他事件；
- `followedByAny`：非严格且允许重叠匹配；
- `within`：整个模式必须在时间窗口内完成；
- `oneOrMore`/`times`：重复；
- `optional`：可选步骤；
- `greedy`：贪婪匹配。

```java
// 严格连续：a 后必须紧跟 b
.begin("a").where(...).next("b").where(...)

// 非严格：a 和 b 之间可以有其他事件
.begin("a").where(...).followedBy("b").where(...)

// 可选：a 可以有，也可以没有
.begin("a").where(...).optional()
.next("b").where(...)
```

生产上要明确“连续”的定义。风控里的“连续失败”通常是非严格（中间可以有成功？取决于规则），必须和业务确认。

## 20.3 CEP 的内部

CEP 会把 Pattern 编译成 NFA（非确定性有限自动机）：

- 每个状态保存部分匹配；
- 每条事件推进状态；
- watermark 推进时触发超时；
- 状态按 key 存储，随 checkpoint 持久化；
- 输出匹配结果或超时事件。

```text
状态 0 --a--> 状态 1 --b--> 状态 2 --c--> 匹配
              │
              └─ 非匹配事件：根据 next/followedBy 决定丢弃或保留
```

状态大小取决于：

- 模式复杂度；
- 并发部分匹配数量；
- 时间窗口；
- key 基数。

一个宽松的 `followedBy` + 长 `within` 可能产生大量部分匹配。必须压测。

### 部分匹配的爆炸

```text
模式：A followedBy B within 1 小时
每个 key 每小时 1000 个 A，1000 个 B
部分匹配可能达到 100 万
```

优化：

- 用 `next` 代替 `followedBy`，减少并存匹配；
- 缩短 `within`；
- 在 `where` 中尽早过滤；
- 限制每个 key 的并发匹配数（业务上分批）；
- 用窗口/聚合替代部分场景；
- 用 `AfterMatchSkipStrategy` 控制重叠匹配（见 20.9）。

## 20.4 超时与侧输出

```java
OutputTag<TimeoutEvent> timeoutTag = new OutputTag<>("timeout") {};

PatternStream<Event> patternStream = CEP.pattern(stream, pattern)
    .inEventTime()
    .flatSelect(timeoutTag, new PatternFlatTimeoutFunction<...>() {
        ...
    }, new PatternFlatSelectFunction<...>() {
        ...
    });
```

超时事件可以：

- 告警“支付超时”；
- 清理未完成流程；
- 写入死信队列；
- 统计规则漏判。

```java
DataStream<TimeoutEvent> timeouts = patternStream.getSideOutput(timeoutTag);
```

超时语义：

- `within` 是事件时间窗口；
- watermark 越过窗口结束才触发超时；
- watermark 不推进，超时不会触发；
- 处理时间 CEP 可以按系统时间触发，但不可重放。

## 20.5 广播状态与动态规则

规则经常变化，不能每次改规则都重启作业。广播状态模式：

```text
规则流（小） ──broadcast──> 每个 subtask 一份规则副本
主数据流（大） ──keyBy──> 用规则匹配
```

```java
MapStateDescriptor<String, Rule> ruleState =
    new MapStateDescriptor<>("rules", Types.STRING, Types.POJO(Rule.class));

BroadcastStream<Rule> rules = ruleStream.broadcast(ruleState);

events.keyBy(Event::userId)
      .connect(rules)
      .process(new BroadcastMatch())
      .sinkTo(alertSink);
```

```java
public class BroadcastMatch
        extends BroadcastProcessFunction<Event, Rule, Alert> {

    @Override
    public void processElement(Event e, ReadOnlyContext ctx, Collector<Alert> out) {
        for (Map.Entry<String, Rule> entry :
                ctx.getBroadcastState(RULE_STATE).immutableEntries()) {
            if (entry.getValue().matches(e)) {
                out.collect(new Alert(e, entry.getValue()));
            }
        }
    }

    @Override
    public void processBroadcastElement(Rule r, Context ctx, Collector<Alert> out) {
        ctx.getBroadcastState(RULE_STATE).put(r.id(), r);
    }
}
```

优点：

- 规则热更新；
- 无外部查询；
- 低延迟。

限制：

- 规则表必须小；
- 规则更新是最终一致，主数据可能先到；
- 广播状态会进入 checkpoint；
- 规则冲突/版本要管理；
- 规则匹配是逐条遍历，规则多时 CPU 高。

### 规则匹配的优化

- 按规则类型/字段建索引，避免全量遍历；
- 预编译正则/表达式；
- 规则分组，按 key 只加载相关规则；
- 限制单条事件的规则匹配时间；
- 规则数量大时，考虑外部规则引擎 + Async I/O。

## 20.6 规则引擎的工程化

- 规则 DSL/JSON schema 版本化；
- 规则校验（语法、字段、阈值）；
- 灰度发布和回滚；
- 规则命中率、误报率监控；
- 规则执行超时/异常隔离；
- 规则与代码解耦，但不要做成“万能脚本引擎”；
- 审计谁在什么时候改了规则。

```json
{
  "id": "rule-001",
  "version": 3,
  "pattern": "login_failed >= 3 within 5m",
  "action": "alert",
  "severity": "high",
  "enabled": true
}
```

规则引擎的边界：

- 规则太复杂 → 难测试、难优化；
- 规则太多 → 匹配成本高；
- 规则热更新 → 状态一致性问题；
- 规则误报 → 业务信任下降。

## 20.7 CEP vs 规则引擎 vs Agents

| 方案 | 适合 | 状态 | 灵活性 |
|---|---|---|---|
| CEP | 固定序列模式 | 中 | 低 |
| 广播规则 | 动态规则、字段级匹配 | 小 | 中 |
| ProcessFunction | 任意有状态逻辑 | 大 | 高 |
| Flink Agents | 事件驱动 AI Agent、工具调用 | 大 | 很高 |

Flink Agents（Apache Flink 的子项目，由 Flink PMC 管理）把 LLM、工具、记忆和 Flink 的流式运行时结合：

- 事件驱动触发 Agent；
- 多轮记忆/状态；
- 工具调用（MCP）；
- Flink checkpoint + 外部 Action State Store + reconciler，提高外部动作的可靠性（不是无条件的 exactly-once）；
- Python/Java API。

它适合“需要推理/工具调用”的实时场景，但成本和确定性是挑战。不要把它当成 CEP 的替代品：规则明确的风控仍应使用 CEP/规则引擎。Flink Agents 0.4 的能力、可靠性模型和成本边界见第 35 章。

## 20.8 MiniFlink 的 CEP 与广播

MiniFlink 支持基本序列模式：

```python
from miniflink import Pattern, pattern

login_pattern = (
    Pattern.begin("first").where(lambda e: e["type"] == "fail")
    .next("second").where(lambda e: e["type"] == "fail")
    .within(Duration.of_seconds(5))
)

pattern(stream.key_by(lambda e: e["user"]), login_pattern) \
    .select(lambda match: (match.key, match["first"], match["second"]))
```

它维护每个 key 的部分匹配，严格 `next` 会在中间出现非匹配事件时丢弃，`followed_by` 允许跳过，`within` 按事件时间过滤。广播状态通过 `BroadcastProcessFunction` + `MapStateDescriptor` 使用。

MiniFlink 不支持模式组、否定模式、optional、复杂循环和超时侧输出；它是理解 NFA 形状的最小实现，不是生产 CEP 引擎。

## 20.9 匹配后跳过策略与输出治理

默认的 CEP 会把**所有可能的重叠匹配**都输出。对 `a b` 和事件 `a1 a2 b1`，如果两个 `a` 都满足条件，就可能输出 `(a1,b1)` 和 `(a2,b1)`。风控里这通常是重复告警，状态和下游都会被放大。

Flink 用 `AfterMatchSkipStrategy` 控制“匹配完成后从哪里继续搜索”：

```java
import org.apache.flink.cep.nfa.aftermatch.AfterMatchSkipStrategy;

Pattern<Event, ?> pattern = Pattern
    .<Event>begin("first", AfterMatchSkipStrategy.skipPastLastEvent())
    .where(e -> e.isLoginFailed())
    .next("second")
    .where(e -> e.isLoginFailed())
    .within(Duration.ofMinutes(5));
```

常用策略：

| 策略 | 行为 | 适合 |
|---|---|---|
| `noSkip()` | 输出所有可能的重叠匹配（默认） | 审计/穷举，通常需要下游去重 |
| `skipToNext()` | 丢弃与已输出匹配**同一起始事件**的部分匹配，从下一个事件继续 | 滑动窗口式检测，减少重叠 |
| `skipPastLastEvent()` | 丢弃在已输出匹配**结束之前**开始的所有部分匹配，跳过整个序列 | 告警去重、非重叠事件链 |
| `skipToFirst("name")` | 丢弃在指定步骤**第一个事件**之前开始的部分匹配 | 以某个关键步骤为锚点 |
| `skipToLast("name")` | 丢弃在指定步骤**最后一个事件**之前开始的部分匹配 | 以某个结束步骤为锚点 |

选择策略前先回答业务问题：

- 同一笔风险行为应该告警一次还是多次？
- 重叠的序列是“同一件事”还是“不同的事”？
- 下游能否按业务键去重？
- 规则审计是否需要保留所有匹配？

### 20.9.1 策略对状态的影响

重叠匹配意味着 CEP 要同时维护多个部分匹配。`noSkip` + 宽松 `followedBy` + 长 `within` 是最坏组合：

```text
每个 key 每小时 1000 个 A、1000 个 B
noSkip + followedBy + within 1h
→ 大量部分匹配和输出
```

`skipPastLastEvent` 通常会显著减少并发的部分匹配，但它也可能漏掉业务上需要的重叠模式。不要把“减少状态”当成唯一目标；先确认语义。

### 20.9.2 输出治理

即使选了跳过策略，仍要做输出治理：

- 给每个匹配生成稳定的 `match_id`（规则 id + 业务 key + 起止时间 + 版本）；
- 下游按 `match_id` 幂等写入/告警；
- 对同一 key 的短时间重复告警做抑制；
- 超时事件走侧输出，不要和正常匹配混在一起；
- 规则版本变化时，旧匹配的语义要能解释；
- 监控匹配率、误报率、部分匹配数、状态大小。

### 20.9.3 CEP 测试

CEP 最容易“测试全绿但线上多报/漏报”。测试至少覆盖：

- 严格 `next` 与非严格 `followedBy` 的差异；
- 重叠事件、同一时间戳、乱序到达；
- `within` 边界（刚好等于、刚超过）；
- 超时侧输出；
- 规则热更新后的状态；
- checkpoint/恢复后部分匹配是否还在；
- 跳过策略的预期输出数量。

```java
// 断言输出数量，而不仅是“有输出”
assertThat(collect(matches)).hasSize(1);
```

## 20.10 生产检查清单

- [ ] 模式是严格连续还是非严格？业务确认过吗？
- [ ] `within` 多长？部分匹配状态多大？
- [ ] 每个 key 的并发匹配数是否可控？
- [ ] 超时事件是否有下游处理？
- [ ] 规则表多大？广播状态能否放下？
- [ ] 规则更新是否需要版本/灰度/回滚？
- [ ] 规则匹配的 CPU 成本是否压测？
- [ ] 误报/漏报如何监控？

## 本章小结

CEP 把“事件之间的关系”变成可管理的状态机，广播状态把“规则”变成可热更新的数据。两者都容易造成状态膨胀和规则失控，必须配合监控、版本和压测。

下一章讨论内存、网络与反压：当状态和流量增长时，作业为什么变慢，以及如何定位真正的瓶颈。
