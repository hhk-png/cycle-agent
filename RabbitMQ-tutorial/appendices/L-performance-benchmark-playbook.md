# 附录 L 性能基准、容量规划与 PerfTest 实验手册

本附录不是“RabbitMQ 能跑多少 TPS”的排行榜，而是一套可复现的实验方法：如何定义负载、如何用 PerfTest 发压、如何读结果、如何把结果转成容量和成本决策。任何脱离消息大小、持久化、确认模式、队列类型、硬件和客户端配置的吞吐数字都没有意义。

## 先定义负载，再谈性能

一张最小的工作负载卡片：

```text
业务：订单事件 / 任务分发 / 遥测 / RPC
消息大小：平均、p50、p99、最大值
发布速率：平均、峰值、突发形态
消费速率：平均、峰值、处理耗时分布
确认：publisher confirm（同步/异步/批量）、consumer ack（手动/自动）
持久化：transient / persistent / quorum / stream
路由：direct / topic / headers / E2E / 多绑定
队列类型与数量：classic / quorum / stream
客户端：语言、连接数、channel 数、prefetch、并发
目标：吞吐、p50/p99 延迟、积压恢复时间、错误率
环境：CPU/内存/磁盘/网络/TLS/跨 AZ
```

如果这张卡片填不出来，先补业务观测，不要直接压测。压测一个错误的工作负载，只会得到错误的自信。

## PerfTest 基础

RabbitMQ 官方 PerfTest 是最常用的发压工具。安装方式以官方仓库为准；常见用法是下载 `perf-test.jar` 或使用发行包。以下命令展示参数结构，具体版本参数以 `--help` 为准。

### 发布压测

```bash
java -jar perf-test.jar \
  --uri amqp://app:password@broker.internal/%2f \
  --id order-publish \
  --size 1024 \
  --time 60 \
  --producer-count 4 \
  --qos 100 \
  --confirm 100 \
  --flag persistent \
  --queue-args x-queue-type=quorum
```

关键参数：

- `--size`：消息 body 字节数；必须测试真实分布，不只测 1 KB；
- `--time`：持续秒数；
- `--producer-count`：并发生产者；
- `--qos`：每个 producer 的未确认窗口；
- `--confirm`：每 N 条等待一次 confirm；`--confirm 0` 表示不等待；
- `--flag persistent`：`delivery_mode=2`；
- `--queue-args`：队列类型/参数。

### 消费压测

```bash
java -jar perf-test.jar \
  --uri amqp://app:password@broker.internal/%2f \
  --id order-consume \
  --queue order-publish \
  --time 60 \
  --consumer-count 8 \
  --qos 20 \
  --auto-ack false
```

消费端要明确：

- `--qos` 与业务处理时间匹配；
- `--auto-ack` 是否开启；
- 消费者数量与下游容量；
- 是否需要模拟处理延迟（PerfTest 有相关参数时使用，或自建消费者）。

### 固定速率与延迟

```bash
java -jar perf-test.jar \
  --uri amqp://app:password@broker.internal/%2f \
  --id latency \
  --size 512 \
  --rate 5000 \
  --time 120 \
  --producer-count 2 \
  --confirm 100
```

固定速率有助于观察**延迟随负载的变化**，避免“能发多快就发多快”掩盖拐点。记录 p50/p99/p999，而不是只看平均。

## 实验矩阵

至少覆盖以下维度，不要只测一个点：

| 维度 | 取值示例 | 观察 |
|---|---|---|
| 消息大小 | 128 B / 1 KB / 16 KB / 256 KB | 吞吐、CPU、网络、分帧 |
| 持久化 | transient / persistent | 磁盘 IO、confirm 延迟 |
| confirm | 关闭 / 每 1 条 / 每 100 条 / 异步 | 吞吐 vs 延迟 |
| 队列类型 | classic / quorum / stream | 写放大、延迟、恢复 |
| 副本/节点 | 1 / 3 / 5 节点 | 多数派成本、故障容忍 |
| 路由 | direct / topic / headers / 多绑定 | CPU、匹配成本 |
| 消费者 | 1 / 4 / 16 / 64 | 吞吐拐点、下游瓶颈 |
| prefetch | 1 / 10 / 100 / 1000 | 未确认内存、公平性 |
| TLS | 开 / 关 | CPU、握手、连接建立 |
| 故障 | 杀节点 / 杀 leader / 磁盘慢 | 恢复时间、错误率 |

每个点至少运行 3 次，记录中位数和离散度。不要用一次“跑得好看”的结果做容量承诺。

## 必须采集的指标

### 客户端

- publish 速率、confirm 延迟 p50/p99；
- nack/return/超时数；
- 连接/channel 数、重连次数；
- 消费速率、ack 延迟、未确认数；
- 端到端延迟（消息时间戳）；
- GC/事件循环延迟/线程池队列。

### Broker

- `messages_ready`、`messages_unacknowledged`、`consumers`；
- publish/deliver/ack/redeliver 速率；
- confirm 延迟（若可从插件/日志获得）；
- 内存、磁盘剩余、磁盘 IOPS/fsync 延迟；
- CPU、运行队列、上下文切换；
- 网络带宽/丢包/重传；
- Quorum leader 变化、Raft 日志/快照大小；
- 分区、告警、blocked 连接。

### 系统

- CPU steal、内存回收、页缓存；
- 磁盘 await/queue depth；
- 网络 RTT/带宽；
- 虚拟化层限流（云盘 IOPS/网络突发）。

## 读结果的四个陷阱

### 1. 只看平均值

平均 5 ms 可能掩盖 p99 500 ms。消息系统的 SLO 通常由尾延迟决定：一次 confirm 超时就会触发重试和重复。

### 2. Coordinated omission

如果发压工具在系统变慢时自动降低发送速率，它会“漏记”本应发生的延迟。固定速率发压 + 记录实际完成时间，才能看到真实尾延迟。

### 3. 把 broker 吞吐当成端到端吞吐

发布压测只证明 broker 能接收；没有消费者时，消息堆在内存/磁盘，测的是“吸收能力”，不是“处理能力”。要分别测 publish-only、consume-only、端到端。

### 4. 忽略故障与恢复

稳态吞吐好看，不代表 leader 切换、节点重启、积压恢复时可用。必须测：

- 杀一个 follower/leader 后的错误率与恢复时间；
- 积压 100 万条后的消费追赶时间；
- 磁盘变慢/内存告警时的行为；
- 滚动升级期间的重连与重复。

## 容量估算公式

### 存储

```text
所需存储 ≈ 峰值到达率 × 可接受积压时长 × 平均消息大小 × 副本因子 × 安全系数
         + 索引/段文件/日志开销
         + DLQ/备份/操作系统余量
```

示例（示范假设，不是基准）：

```text
峰值 5,000 msg/s
积压容忍 10 分钟 = 600 s
平均 2 KB，p99 20 KB
Quorum 副本 3
安全系数 2

按平均：5000 × 600 × 2KB × 3 × 2 ≈ 34.4 GB
按 p99 校准：若 1% 消息为 20KB，则额外约 0.01 × 5000 × 600 × 18KB × 3 × 2 ≈ 3.2 GB
```

实际还要加段文件、索引、快照、日志、DLQ、备份和文件系统保留空间。用真实消息采样校准，不要只用平均值。

### 吞吐

```text
所需消费者数 ≈ 峰值消费速率 × 平均处理时间 / 目标利用率
```

示例：

```text
峰值 2,000 msg/s，平均处理 50 ms，目标利用率 70%
所需并发 ≈ 2000 × 0.05 / 0.7 ≈ 143
```

143 个并发消费者可能受下游数据库连接池、API 限流、分区数限制。先确认下游容量，再决定队列分片/消费者数量。

### 连接与 channel

```text
连接数 ≈ 应用实例数 × 每实例连接数
channel 数 ≈ 连接数 × 每连接 channel 数
```

常见基线：每进程 1 条连接，publish/consume/management 各一个 channel；每线程或每协程独立 channel。几万 channel 会显著增加 broker 内存和调度开销。

## Quorum Queue 的写放大

Quorum 的 confirm 代表“多数派已持久化”，代价是：

- leader 追加日志 + 复制到 follower；
- 多数派 fsync；
- commit index 推进；
- 消费者只能看到已提交消息。

影响因素：

- 副本数（3 vs 5）；
- 网络 RTT；
- 磁盘 fsync 延迟；
- 批量/流水线程度；
- 消息大小与批处理；
- leader 负载是否集中。

优化方向：

- 用低延迟网络和稳定 SSD；
- 避免跨地域放置 Quorum；
- 批量发布 + 批量 confirm；
- 控制单队列写入热点，必要时分片；
- 不要把所有低价值队列都做成 Quorum。

## Stream 的基准方法

Stream 的瓶颈通常不在“每条消息 ack”，而在：

- 追加吞吐与段文件滚动；
- retention/trim 的磁盘 IO；
- 消费者读取吞吐与 offset 提交；
- 分区数量与消费者组 rebalance；
- 网络带宽。

压测时要分别测：

1. 单分区追加吞吐；
2. 多分区并行追加；
3. 单消费者从 earliest 读取；
4. 多消费者组独立读取；
5. retention 触发时的写放大；
6. 消费者落后很多时的追赶速度。

不要用“队列消费”的方式压 Stream：Stream 读不删除，语义不同。

## 调优顺序

```text
1. 明确 SLO 与工作负载
2. 建立基线（当前配置、当前版本）
3. 找瓶颈：客户端 / 网络 / broker CPU / 磁盘 / 下游
4. 先修架构：连接复用、批量 confirm、队列类型、分片、prefetch
5. 再调参数：prefetch、并发、frame_max、内存水位、磁盘
6. 验证故障与恢复
7. 建立回归基准，变更后重跑
```

不要一上来改 `prefetch` 或内存水位。先确认瓶颈在哪；否则只是把瓶颈从一个地方挪到另一个地方。

## 常见性能反模式

| 反模式 | 表现 | 修正 |
|---|---|---|
| 每消息建连接 | 连接风暴、TLS/认证开销 | 连接复用 |
| 每消息同步 confirm | 吞吐被 RTT 限制 | 批量/异步 confirm |
| prefetch 过大 | 未确认堆积、重投范围大 | 按处理能力调小 |
| prefetch=1 一刀切 | 吞吐低 | 按场景调整 |
| 所有消息 persistent | 磁盘/复制瓶颈 | 分层可靠性 |
| 所有队列 Quorum | 资源浪费、写延迟高 | 按重要性选择 |
| 大消息直塞队列 | 内存/网络/磁盘放大 | 对象存储 + 引用 |
| headers 塞大 JSON | 体积/匹配成本 | 精简 headers |
| 消费者阻塞事件循环 | 心跳超时/假死 | 异步/线程池 |
| 无限重试 | 重试风暴 | 退避/上限/DLQ |
| 只看 broker 指标 | 忽略客户端/下游瓶颈 | 端到端观测 |

## 一份可执行的实验计划

### 第 1 天：基线

- 单节点/三节点各跑一次 publish-only、consume-only、端到端；
- 消息 1 KB、persistent、confirm 每 100 条；
- 记录吞吐、p50/p99、CPU、磁盘、内存。

### 第 2 天：消息大小与确认

- 128 B / 1 KB / 16 KB / 256 KB；
- confirm 关闭 / 每 1 条 / 每 100 条；
- 找出吞吐拐点和尾延迟恶化点。

### 第 3 天：队列类型

- classic vs quorum vs stream；
- 3 节点 quorum 的写放大；
- stream 的单分区/多分区读取。

### 第 4 天：故障

- 杀 follower、杀 leader、杀两个节点；
- 观察错误率、confirm 超时、恢复时间；
- 验证客户端重连/幂等。

### 第 5 天：积压恢复

- 灌入 100 万条积压；
- 启动消费者，测追赶时间；
- 观察分页/磁盘 IO/内存。

### 第 6 天：回归与报告

- 固定配置重跑关键点；
- 记录版本、参数、硬件、客户端；
- 输出容量建议、风险与待办。

## 与 Mini-RabbitMQ 的关系

Mini 可以演示“批量 confirm 减少 RPC”“prefetch 限制未确认数”“快照写入随状态增长变慢”，但不能作为 RabbitMQ 的性能基准：Python GIL、JSON 全量快照、线性路由、无真实复制都会让数字失真。性能结论必须在真实 RabbitMQ + PerfTest + 生产同构环境上得出。

## 进一步阅读

- RabbitMQ PerfTest：<https://github.com/rabbitmq/rabbitmq-perf-test>
- Quorum Queues：<https://www.rabbitmq.com/docs/quorum-queues>
- Streams：<https://www.rabbitmq.com/docs/streams>
- Consumer Prefetch：<https://www.rabbitmq.com/docs/consumer-prefetch>
- Prometheus 指标：<https://www.rabbitmq.com/docs/prometheus>

## 小结

性能工程的核心不是“调大某个参数”，而是定义负载、建立基线、分离 publish/consume/端到端、观察尾延迟、测试故障与恢复，再把结果转成容量和成本。PerfTest 是工具，实验矩阵和指标采集才是方法。Quorum 的写放大、Stream 的保留/读取、消息大小和 confirm 模式是最常见的四个变量。任何性能数字都要带上版本、配置、硬件和工作负载，否则不可复现。

---

[返回目录](../README.md) | [上一章：附录 K 生产故障案例库与复盘模板](K-production-incident-library.md) | [下一章：附录 M 插件体系、扩展开发与二次开发](M-plugin-development.md)
