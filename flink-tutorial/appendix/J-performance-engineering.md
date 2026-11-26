# 附录 J 流处理性能工程与压测手册

> 本附录讲的不是“背参数”，而是**如何用可复现的实验证明瓶颈在哪、改动是否有效**。配置项默认值以所用 Flink 版本为准；所有数字都是方法与算例，不是通用 benchmark 结论。

## J.1 性能问题的五种形态

“作业慢”是一个太粗的描述。先把它拆成可测量的形态：

| 形态 | 表现 | 首要指标 | 常见根因 |
|---|---|---|---|
| 吞吐不足 | 输入积压、Kafka lag 增长 | records/s、busy、CPU | 并行度、序列化、状态访问、sink 限流 |
| 延迟高 | P99 超标、watermark lag 大 | 端到端延迟、watermark lag | 反压、buffer、外部调用、checkpoint 竞争 |
| 反压 | 上游 HIGH、下游慢 | backPressuredTime、credit | 热点、sink 慢、同步 IO、GC |
| 恢复慢 | 故障后长时间不可用 | RTO、restore 时间、下载量 | 状态大、全量快照、本地盘、并行恢复 |
| 成本高 | 资源利用率低、账单高 | CPU/内存利用率、状态 GB、CU | 过度并行、状态膨胀、跨 AZ、低效序列化 |

**先选形态，再选实验。** 用“延迟高”去做吞吐压测，往往会得到错误结论。

## J.2 可复现基准的六个要素

一个可信的 benchmark 必须写清楚：

1. **拓扑**：source → transform → sink 的算子、并行度、链化、slot 配置；
2. **数据**：事件率、记录大小、key 基数、倾斜分布、乱序程度；
3. **状态**：状态大小、TTL、窗口重叠、join 缓存；
4. **外部系统**：Kafka 分区/副本、数据库连接池、对象存储带宽；
5. **故障**：是否注入 checkpoint 失败、TM kill、sink 限流；
6. **测量**：指标口径、采样窗口、预热时间、重复次数。

缺任何一项，数字都不可比较。尤其是**数据分布**：均匀数据上的吞吐数字，通常比真实倾斜数据乐观数倍。

### 最小实验记录模板

```text
实验 ID：
Flink 版本 / Java 版本 / 连接器版本：
拓扑与并行度：
输入：events/s、bytes/record、key 基数、倾斜比例、乱序 P99
状态：初始大小、增长速率、TTL、checkpoint 间隔
输出：sink 类型、批量、限流
集群：TM 数、slot、CPU/内存、磁盘、网络
指标：吞吐、P50/P99 延迟、watermark lag、反压、checkpoint duration/size、GC、CPU、磁盘 IO
故障注入：无 / checkpoint 失败 / TM kill / sink 延迟
重复次数与方差：
结论与下一步：
```

## J.3 负载模型：不要只调事件率

流作业的负载不是单一维度。至少建模五个变量：

```text
负载 = 事件率 × 记录大小 × key 基数 × 状态保留 × 倾斜程度
```

### 事件率与到达模式

- **开环（open-loop）**：按固定速率发送，不等待下游确认。适合测“系统能否跟上”；
- **闭环（closed-loop）**：收到响应再发下一条。容易掩盖反压，不适合容量测试；
- **突发（burst）**：短时间高倍峰值，测缓冲、反压和恢复；
- **阶梯（step）**：逐级提高速率，找拐点（吞吐不再增长、延迟突变）。

### key 基数与倾斜

```text
均匀：100 万 key，每个 key 频率相近
Zipf：少量 key 占大部分流量
热点：单 key 占 30%+
```

热点会打满单个 subtask，增加并行度无效。压测必须包含倾斜场景，否则会高估扩容收益。

### 记录大小与序列化

- 1 KB 与 10 KB 的记录，网络/序列化成本差一个量级；
- JSON 比 Avro/Protobuf 更易读但更慢；
- 宽表/嵌套结构会放大序列化和状态；
- 大 value（MB 级）会拖慢 RocksDB 和 checkpoint。

### 状态增长

- 窗口重叠：`size/slide` 决定每条记录的窗口副本数；
- regular join：两侧历史无界；
- CEP：部分匹配数量；
- 去重：key 基数 × TTL；
- 广播：维表大小 × subtask 数。

压测要跑到“状态稳定”或“状态增长到目标规模”，而不是只跑几分钟。

## J.4 压测拓扑与工具

### 推荐的最小拓扑

```text
负载生成器 → Kafka(topic) → Flink 作业 → Kafka/DB/湖仓 → 校验器
                 │                │
                 └─ 监控 lag      └─ 监控吞吐/延迟/checkpoint
```

- 负载生成器与 Flink 分开部署，避免抢 CPU；
- Kafka 分区数 ≥ source 并行度；
- 下游 sink 用真实系统或等价限流模拟；
- 校验器统计唯一 id、重复、丢失、延迟分布。

### 工具选择

| 目的 | 工具 |
|---|---|
| Kafka 生产/消费 | `kafka-producer-perf-test.sh`、自定义 Python/Java producer |
| HTTP/gRPC 压测 | wrk、vegeta、k6、JMeter |
| Flink 指标 | Web UI、REST、Prometheus/Grafana |
| JVM profiling | async-profiler、JFR、jstack、jmap |
| Python profiling | py-spy、cProfile、worker 日志 |
| 系统指标 | node_exporter、容器 metrics、iostat、sar |

不要用“单机 while 循环发消息”当负载生成器：它很容易成为瓶颈，且不记录发送延迟。

## J.5 指标：平均值会骗人

至少采集：

```text
吞吐：records/s、bytes/s、numRecordsInPerSecond / numRecordsOutPerSecond
延迟：P50/P95/P99、端到端延迟、watermark lag
反压：backPressuredTimeMsPerSecond、busyTimeMsPerSecond、idleTime
checkpoint：duration、size、alignment、failed count、restore time
状态：state size、RocksDB block cache hit、compaction pending、stall
系统：CPU、内存、GC pause、磁盘 IOPS/吞吐、网络带宽、容器 OOMKill
外部：Kafka lag、DB P99、错误率、限流/429
```

### 延迟口径

- **处理延迟**：记录进入 Flink 到写出 sink；
- **端到端延迟**：事件时间/产生时间到结果可见；
- **watermark lag**：当前时间 − watermark，反映“数据新鲜度”；
- **checkpoint 延迟**：不影响单条延迟，但会影响恢复和吞吐。

不同口径不能混用。对外承诺 SLO 时，要写清楚是哪一个。

### 分位数与长尾

P99 比平均值更能反映用户体验，但也会被少量慢请求主导。建议同时看：

- P50/P95/P99；
- 最大值与慢请求样例；
- 分窗口趋势（是否周期性恶化）；
- 按 subtask 拆分（是否单点）。

## J.6 瓶颈定位决策树

```text
吞吐上不去 / lag 增长
├─ source 端 lag 高，source busy 高？
│  ├─ 是 → 分区数、反序列化、watermark 生成、source 限流
│  └─ 否 → 继续
├─ 某算子 busy 接近 100%，其他低？
│  ├─ 是 → 热点 key、状态访问、同步 IO、算子算法
│  └─ 否 → 继续
├─ 全链路 busy 高，sink 也忙？
│  ├─ 是 → 扩容/优化 sink/批量/连接池
│  └─ 否 → 继续
├─ checkpoint 期间周期性恶化？
│  ├─ 是 → 状态大、上传带宽、RocksDB flush、对齐
│  └─ 否 → 继续
└─ GC/CPU/磁盘异常？
   ├─ 是 → 内存模型、状态后端、序列化、磁盘
   └─ 否 → 检查数据倾斜与外部系统
```

反压的读法：

- 反压 HIGH 的算子不一定是根因；看它自己的 busy：
  - busy 高 → 自身慢；
  - idle 高 → 下游慢，反压传导；
- 从 sink 往上找第一个“自身忙”的算子；
- 按 subtask 看，区分全局慢与单点热点。

## J.7 Profiling：从现象到代码

### JVM

```bash
# CPU/alloc/lock 火焰图
./profiler.sh -d 30 -f /tmp/flink.html <pid>

# 线程栈
jstack <pid> > /tmp/threads.txt

# 堆直方图/堆 dump
jmap -histo:live <pid> | head -50
jmap -dump:format=b,file=/tmp/heap.hprof <pid>

# JFR
jcmd <pid> JFR.start duration=60s filename=/tmp/rec.jfr
```

看什么：

- CPU 火焰图：序列化、正则、JSON、压缩、状态访问；
- alloc：短生命周期对象、装箱、字符串拼接；
- lock：同步块、连接池、日志；
- 线程栈：卡在 socket/DB/checkpoint/锁；
- 堆：状态对象、缓存、泄漏。

### RocksDB

```text
block cache hit ratio
compaction pending bytes / stall count
write amplification / read amplification
SST 文件数 / 每层大小
```

状态访问慢通常不是“RocksDB 不行”，而是：

- value 太大；
- key 设计导致随机读；
- block cache 太小；
- compaction 跟不上写入；
- 本地盘 IOPS 不足。

### PyFlink

```bash
py-spy top --pid <python-worker-pid>
py-spy dump --pid <python-worker-pid>
```

看什么：UDF 内部循环、pandas 拷贝、模型加载、序列化、GIL 等待。

## J.8 状态与 checkpoint 压测

### 状态规模实验

1. 构造目标 key 基数与每 key 状态大小；
2. 跑到状态稳定（或达到目标 GB）；
3. 记录 checkpoint size、duration、上传带宽；
4. 触发 savepoint，测恢复时间；
5. 改变并行度，测 rescale 时间；
6. 对比 HashMap / RocksDB / 增量 / 本地恢复。

### checkpoint 压测矩阵

| 变量 | 取值 | 观察 |
|---|---|---|
| 间隔 | 10s / 30s / 1min / 5min | 吞吐、duration、恢复点 |
| 状态大小 | 1GB / 10GB / 100GB | duration、上传、恢复 |
| 并发数 | 1 / 2 | 资源竞争 |
| 对齐/非对齐 | aligned / unaligned | 反压下的完成时间 |
| 存储 | 本地 / S3 / OSS | 带宽、错误率 |
| 本地恢复 | on / off | RTO、节点漂移 |

结论要写成“在 X 状态下，间隔 Y 的 checkpoint 需要 Z 秒，恢复需要 W 秒”，而不是“调大 timeout 就好了”。

## J.9 SQL 性能实验

### 计划对比

```sql
EXPLAIN SELECT ...;
EXPLAIN CHANGELOG SELECT ...;
EXPLAIN ESTIMATED_COST SELECT ...;
```

对比改动前后：

- 是否多了 shuffle；
- 聚合是否 local/global 拆分；
- join 是否从 interval 退化为 regular；
- 扫描是否下推过滤/投影；
- 状态是否变大。

### 参数实验

一次只改一个：

```yaml
table.exec.mini-batch.enabled: true
table.exec.mini-batch.allow-latency: 5s
table.exec.mini-batch.size: 5000
table.exec.state.ttl: 24h
```

记录：吞吐、P99、checkpoint size、状态大小、CPU。MiniBatch 提高吞吐但增加延迟；TTL 降低状态但可能改变结果。

### join 策略实验

- regular join vs interval join vs temporal join；
- broadcast vs shuffle；
- 加盐 vs 两阶段聚合；
- Delta Join（多流同源）。

用同一份数据和同一组 key，比较状态大小、shuffle 字节、延迟。

## J.10 容量与成本回归

每次大变更后做一次回归：

```text
吞吐/CU = 峰值 records/s ÷ 实际 CU
成本/百万事件 = 小时成本 ÷ (每小时事件数 / 1e6)
状态成本/GB = 存储账单 ÷ 状态 GB
```

关注：

- 扩容是否线性？并行度翻倍，吞吐是否接近翻倍？
- 状态是否随流量线性增长？
- checkpoint 成本是否随状态增长？
- 闲置资源比例；
- 跨 AZ/跨区流量；
- 恢复时间是否随状态增长不可接受。

## J.11 常见误判

1. **只测吞吐，不测延迟**：高吞吐可能靠攒批换来高延迟；
2. **均匀数据**：忽略倾斜，扩容收益被高估；
3. **短时间压测**：没跑到状态稳定/compaction 稳定；
4. **闭环压测**：下游慢时自动降速，掩盖容量不足；
5. **忽略 checkpoint**：真实作业的周期性抖动没被覆盖；
6. **忽略恢复**：正常路径吞吐好，故障后 RTO 不可接受；
7. **只看平均值**：P99 长尾被掩盖；
8. **单次实验**：没有重复与方差，无法判断改动是否显著；
9. **混用版本**：Flink/connector/JDK 版本不同，数字不可比；
10. **把 benchmark 当生产承诺**：数据、拓扑、SLA 都不同。

## J.12 实验报告模板

```text
# 实验：<一句话假设>

## 背景与目标
- 业务场景：
- 当前问题（延迟/吞吐/成本/恢复）：
- 成功标准：

## 环境
- Flink/Java/connector 版本：
- 集群规格与并行度：
- Kafka/DB/存储：
- 数据规模与分布：

## 方法
- 负载模型（开环/闭环、速率、倾斜）：
- 预热与采样窗口：
- 重复次数：
- 故障注入：

## 结果
- 吞吐：
- P50/P95/P99：
- watermark lag：
- checkpoint duration/size：
- 状态大小/增长：
- CPU/GC/磁盘/网络：
- 成本：

## 分析
- 瓶颈位置与证据：
- 与基线的差异：
- 是否达到成功标准：

## 结论与下一步
- 采纳/回滚：
- 需要补充的实验：
- 风险与回滚方案：
```

## J.13 MiniFlink 能测什么、不能测什么

MiniFlink 是单进程、同步调度、无网络/无序列化的教学实现，**不能**用于：

- 吞吐/延迟 benchmark；
- 反压/credit/网络缓冲；
- RocksDB/状态后端；
- checkpoint 对齐/恢复时间；
- 分布式故障与扩容。

它能做的是**语义级实验**：

- 同一输入下窗口/watermark/join 的输出；
- checkpoint 前后状态是否一致；
- 去重/对账逻辑；
- CEP 部分匹配的增长趋势（用计数观察，不是性能）。

把 MiniFlink 当“正确性实验室”，把真实 Flink 当“性能实验室”，两者不要混用。

## J.14 小结

性能工程的核心不是“调参”，而是：

1. **定义问题形态**（吞吐/延迟/反压/恢复/成本）；
2. **建立可复现负载**（数据、状态、倾斜、故障）；
3. **用分位数和分 subtask 指标定位瓶颈**；
4. **一次只改一个变量**，保留基线；
5. **把恢复和成本纳入实验**，而不是只看正常路径吞吐。

一个可信的性能结论，应该能让别人用同样的拓扑、数据和配置复现。做不到复现的数字，只能算故事。
