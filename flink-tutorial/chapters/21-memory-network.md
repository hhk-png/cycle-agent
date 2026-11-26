# 第21章 内存、网络与反压

## 21.1 为什么流作业会“越跑越慢”

常见原因不是 CPU 不够，而是：

- 状态太大，频繁 GC 或 RocksDB 读写；
- 网络缓冲耗尽，上游被反压；
- checkpoint 与数据处理争抢磁盘/网络；
- 数据倾斜，单个 subtask 过载；
- 外部系统变慢，反压传导到 source；
- 缓冲区超时/批量参数不合理；
- 序列化开销大，CPU 花在编解码上。

Flink 的运行时是一套流水线，任何一段变慢都会向上游传导。反压是症状，不是病因。

```text
source → map → keyBy → window → sink
                              ↑ 变慢
         ←──── 反压逐级向上传导 ────
```

## 21.2 TaskManager 内存模型

大致分为：

- **JVM 元空间/开销**；
- **Task 堆内存**：用户对象、状态（HashMap backend）；
- **Managed Memory**：RocksDB、批算子、排序；
- **Network Memory**：网络缓冲；
- **Direct/Off-heap**：Netty、RocksDB cache；
- **本地磁盘**：RocksDB 数据、临时文件。

```text
Total Process Memory
├── JVM Metaspace
├── JVM Overhead
├── Flink Memory
│   ├── Task Heap
│   ├── Managed Memory
│   ├── Network Memory
│   └── Framework Heap/Off-heap
└── 其他 JVM 内存
```

Flink 通过 `taskmanager.memory.process.size` 统一规划，再细分。配置不当会出现：

- 堆外内存不足，容器被 OOMKill；
- 网络缓冲太小，吞吐上不去；
- RocksDB block cache 太大，挤占其他内存；
- 堆内状态太大，Full GC。

```yaml
taskmanager.memory.process.size: 16g
taskmanager.memory.task.heap.size: 4g
taskmanager.memory.managed.size: 4g
taskmanager.memory.network.fraction: 0.15
```

不要只调 `-Xmx`。要按 Flink 的内存模型配置，并留出容器开销。

### 常见内存故障

| 现象 | 可能原因 |
|---|---|
| 容器 OOMKilled，堆没满 | 堆外/网络/RocksDB 超限 |
| Full GC 频繁 | 堆内状态太大 / 对象太多 |
| RocksDB 读慢 | block cache 太小 / 磁盘 IO 差 |
| 网络吞吐上不去 | network memory 太小 / buffer 不足 |
| checkpoint 慢 | 状态大 / 上传带宽不足 |

## 21.3 网络栈与缓冲

Flink 的网络栈基于 Netty：

```text
Task A 输出 → 序列化 → 网络缓冲 → Netty → 网络 → Netty → 反序列化 → Task B
```

关键参数：

- `taskmanager.memory.network.fraction`：网络缓冲占 Flink 总内存的比例（1.10+ 的正式名字；旧的 `taskmanager.network.memory.fraction` 只是兼容别名）；
- `taskmanager.memory.network.min/max`：网络内存的下限/上限；
- `execution.buffer-timeout`：缓冲多久发一次，吞吐 vs 延迟；
- `taskmanager.network.compression.codec`：网络压缩（2.x；旧版的 `taskmanager.network.batch-shuffle.compression` 已废弃/移除）；
- 每个通道的 buffer 数。

`buffer-timeout` 增大：攒批更多，吞吐高，延迟高；减小：延迟低，缓冲压力大。

```yaml
execution.buffer-timeout: 100ms
taskmanager.memory.network.fraction: 0.15
taskmanager.memory.network.max: 2g
```

### Buffer 与吞吐/延迟

```text
buffer-timeout=0ms   → 每条记录立即发，延迟低，网络包多
buffer-timeout=100ms → 攒批发送，吞吐高，延迟增加
```

对于低延迟风控，可能用 10～50ms；对于大吞吐 ETL，可以用 100～200ms。没有统一最优值。

### Buffer debloating

Flink 1.14+ 支持 buffer debloating，根据吞吐自动调整缓冲大小：

```yaml
taskmanager.network.memory.buffer-debloat.enabled: true
taskmanager.network.memory.buffer-debloat.target: 1s
```

它试图在“缓冲足够平滑”和“延迟不过高”之间平衡。适合吞吐波动大的作业，但需要观察效果。

## 21.4 Credit-based 流控

Flink 的 credit-based flow control：

- 下游告诉上游“我还有多少可用缓冲”（credit）；
- 上游只发不超过 credit 的数据；
- 下游消费后归还 credit。

```text
下游 → 上游：credit = 5
上游 → 下游：最多 5 个 buffer
下游消费后 → 上游：credit += 1
```

好处：

- 避免上游盲目发送导致下游 OOM；
- 反压更平滑；
- 可以定位哪个通道慢。

反压不是错误，而是系统在告诉你“下游处理不过来”。要找到根因，而不是简单加大内存。

## 21.5 反压排查

Web UI 的反压状态：

- OK；
- LOW；
- HIGH。

排查顺序：

1. 找到反压最高的算子；
2. 看它的 busy、records in/out、state size；
3. 看下游是否更慢（sink/外部系统）；
4. 看数据倾斜（各 subtask 指标）；
5. 看 GC、CPU、磁盘、网络；
6. 看 checkpoint 是否与处理争抢资源。

常见根因：

- sink 写入慢（数据库限流、Kafka 分区少）；
- key 热点；
- 状态访问慢（RocksDB 配置差）；
- 同步外部调用；
- 序列化开销大；
- checkpoint 太大/太频繁；
- 网络缓冲区不足。

### 反压的四种典型形态

| 形态 | 表现 | 根因 |
|---|---|---|
| 单点反压 | 某个 subtask HIGH，其他 OK | 热点 key/倾斜 |
| 全链路反压 | 所有算子 HIGH | sink/外部系统慢 |
| 周期性反压 | 与 checkpoint 同步 | checkpoint 争抢资源 |
| 突发反压 | 流量尖峰 | 容量不足/缓冲太小 |

## 21.6 数据倾斜

表现：

- 某些 subtask 的 records in 远高于其他；
- 反压集中；
- checkpoint 卡在个别 subtask；
- 状态大小差异大。

处理：

- 采样 key 分布；
- 热点 key 加盐/拆分；
- 两阶段聚合；
- 广播小表；
- 单独作业处理大 key；
- 调整分区器（自定义分区）。

加并行度不能解决单 key 热点，因为同 key 必须同 subtask。

```text
subtask 0: 1000 rec/s
subtask 1: 1100 rec/s
subtask 2: 95000 rec/s  ← 热点
subtask 3: 1050 rec/s
```

## 21.7 RocksDB 调优

- 开启 `state.backend.rocksdb.memory.managed: true`，让 Flink 管理内存；
- 调整 block cache、write buffer；
- 使用本地 SSD；
- 增量 checkpoint；
- 监控 compaction、读放大、写放大；
- 避免超大 value（几 MB 的对象）；
- 状态序列化用高效格式。

```yaml
state.backend.rocksdb.memory.managed: true
state.backend.rocksdb.block.cache-size: 256mb
state.backend.rocksdb.writebuffer.size: 64mb
state.backend.rocksdb.writebuffer.count: 4
state.backend.rocksdb.compaction.style: LEVEL
```

**注意**：`state.backend.rocksdb.memory.managed: true`（默认）时，Flink 从 TaskManager 的 managed memory 统一分配 block cache 和 write buffer，上面的 `block.cache-size` / `writebuffer.size` **绝对值会被忽略**。要使用绝对值，先设 `state.backend.rocksdb.memory.managed: false` 并手工预留 off-heap；否则应调 `state.backend.rocksdb.memory.write-buffer-ratio` / `state.backend.rocksdb.memory.high-prio-pool-ratio`。

RocksDB 不是“无限状态”的魔法。状态越大，恢复越慢，compaction 越重。

### 写放大与读放大

```text
写放大：一条记录写入 → memtable → L0 → L1 → ... → 磁盘
读放大：查询 → 多层 SST → 合并结果
```

状态更新频繁时，写放大可能达到 10 倍以上。要监控：

- compaction pending bytes；
- stall 次数；
- 每层文件数；
- block cache 命中率。

## 21.8 Checkpoint 与处理的竞争

大 checkpoint 会：

- 占用网络上传带宽；
- 触发 RocksDB flush/compaction；
- 增加反压；
- 拖长 checkpoint 时间。

优化：

- 增量 checkpoint；
- 非对齐 checkpoint（谨慎）；
- 调整间隔；
- 分离 checkpoint 存储网络；
- 避免在高峰做 savepoint；
- 控制状态大小。

```text
checkpoint 期间：状态快照 + 上传 + 正常处理 同时进行
→ 磁盘/网络/CPU 竞争
```

## 21.9 MiniFlink 的简化

MiniFlink 用内存队列连接 task，没有网络缓冲、序列化、credit 和反压。调度器会一直排空队列，因此不会出现生产意义上的反压。它可以帮助理解“数据如何流动”，但不能用于性能调优。运行时实现见[第 29 章](29-miniflink-runtime.md)。

## 21.10 反压的定量模型：从 credit 到 buffer

“加内存”“调大 buffer”之所以经常无效，是因为没有算清**在途数据量**。这一节用三个公式把反压从“感觉”变成“可估算”。

### 公式一：Little 定律

```text
在途数据量 = 吞吐 × 延迟
```

例如单通道 100 MB/s、下游处理延迟 20 ms：

```text
在途字节 ≈ 100 MB/s × 0.02 s = 2 MB
```

这 2 MB 必须由网络缓冲、下游队列和算子内部缓冲共同容纳。容不下，上游就会被反压。

### 公式二：缓冲能支撑的吞吐

Flink 的网络缓冲以 segment 为单位（默认常见 32 KiB，具体以版本配置为准）：

```text
单通道可用缓冲字节 = 该通道 buffer 数 × segment 大小
单通道最大吞吐 ≈ 单通道可用缓冲字节 / 下游延迟
```

例如单通道 64 个 buffer × 32 KiB = 2 MB，下游延迟 20 ms：

```text
最大吞吐 ≈ 2 MB / 0.02 s = 100 MB/s
```

如果实际吞吐超过这个值，缓冲会持续打满，credit 耗尽，反压向上游传导。此时**加并行度**可能有效（把流量分到更多通道），**单纯加内存**只在通道数不变、缓冲确实不足时有效。

### 公式三：网络内存需求

```text
网络内存 ≈ Σ(每条通道的在途字节) × 安全系数
         ≈ 通道数 × 吞吐/通道 × 延迟 × 2
```

安全系数 2 用于吸收抖动、批量和对齐。例：

```text
32 条通道 × 10 MB/s × 20 ms × 2 ≈ 12.8 MB
```

这看起来很小，但真实作业的通道数、延迟和批量会大得多；而且网络内存还要与 checkpoint 上传、shuffle、反序列化争抢。

### 为什么利用率高时延迟会突变

把下游看作一个排队系统：利用率 ρ = 到达率 / 服务率。

```text
ρ < 0.7   → 延迟大致稳定
ρ → 0.9   → 排队延迟快速上升
ρ → 1     → 队列发散，反压成为常态
```

这解释了“流量涨 10%，延迟涨一倍”的现象：不是代码突然变慢，而是系统进入了排队区。容量规划要留余量，不要按 100% 利用率设计。

### buffer timeout 的取舍

```text
buffer-timeout 小 → 每条/每小批立即发，延迟低，包多，CPU/网络开销高
buffer-timeout 大 → 攒批，吞吐高，延迟增加，缓冲占用时间变长
```

它影响的是“发送时机”，不是“处理能力”。如果下游处理不过来，调大 timeout 只会让数据在缓冲里待更久，不会提高吞吐；如果下游很快、流量小，调小 timeout 可以显著降延迟。

### buffer debloating 在做什么

Debloating 根据当前吞吐动态调整缓冲量，目标是在“足够平滑”和“不过度缓冲”之间平衡：

```yaml
taskmanager.network.memory.buffer-debloat.enabled: true
taskmanager.network.memory.buffer-debloat.target: 1s
```

- 吞吐高时保留更多缓冲，避免频繁反压；
- 吞吐低时收缩缓冲，降低延迟和内存；
- 目标时间不是越小越好；太小会导致频繁暂停/恢复。

它适合吞吐波动大的作业，不适合用固定 buffer 已经稳定的场景。

### 反压指标怎么读

`backPressuredTimeMsPerSecond` 是“每秒有多少毫秒处于反压”。它高不代表这个算子慢：

```text
算子 busy 高 + 反压高 → 自身处理不过来（CPU/状态/算法）
算子 busy 低 + 反压高 → 下游慢，反压传导
算子 idle 高 + 反压低 → 上游没数据，不是瓶颈
```

定位顺序：

1. 从 sink 往上找第一个“busy 高”的算子；
2. 看它的每个 subtask 是否倾斜；
3. 看它的状态访问、GC、外部调用；
4. 再看网络缓冲/credit 是否成为限制；
5. 最后才调 buffer/内存。

### 一个算例

```text
输入 200 MB/s
下游聚合算子单 subtask 处理能力 15 MB/s
并行度 8 → 总能力 120 MB/s < 200 MB/s
```

无论怎么加 buffer，积压都会增长。正确处置是：

- 提高聚合并行度（如果 key 分布允许）；
- 预聚合/减少 shuffle 数据量；
- 优化状态访问/序列化；
- 或者把上游流量削峰。

如果单 subtask 能力是 30 MB/s、并行度 8（240 MB/s），但网络内存只够每通道 1 MB、延迟 50 ms，则单通道上限约 20 MB/s，总上限 160 MB/s，仍会反压。这时加网络内存或减少通道延迟才有效。

### 检查清单

- [ ] 先算在途数据量（吞吐 × 延迟），再看缓冲是否匹配；
- [ ] 区分“处理能力不足”和“缓冲不足”；
- [ ] 看每个 subtask，而不是全局平均；
- [ ] 利用率是否长期超过 80%？
- [ ] buffer timeout 是否与延迟目标匹配？
- [ ] debloating 是否经过压测，而不是照抄配置？
- [ ] checkpoint 是否与数据处理争抢网络/磁盘？

把估算变成可复现实验的方法（负载模型、指标口径、故障注入、实验报告模板）见[附录 J](../appendix/J-performance-engineering.md)。

## 本章小结

流作业的性能是内存、网络、状态、外部系统共同作用的结果。反压是症状，不是病因。先定位瓶颈，再调参数；先解决倾斜和同步调用，再谈扩容。

下一章讨论监控与可观测性：如何把“作业看起来正常”变成“能量化地知道它是否健康”。
