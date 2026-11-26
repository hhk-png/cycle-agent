# 第 18 章 性能工程：从连接数到持久化

> 本章定位：性能不是“调大 prefetch”。它是发布、确认、路由、存储、消费和网络共同决定的系统属性。先定义指标，再找瓶颈，最后才调参数。

## 先定义“快”

“RabbitMQ 能跑多少 TPS”没有意义，除非说明：

- 消息大小和分布；
- 持久化/副本；
- publisher confirm 是否开启、同步还是异步；
- 消费者数量、prefetch、ack 模式；
- 路由复杂度（direct/topic/headers）；
- 队列类型（classic/quorum/stream）；
- 网络/TLS；
- 延迟目标（p50/p99）；
- 硬件规格。

吞吐和延迟常常冲突。批量 confirm 提高吞吐但增加单条延迟；大 prefetch 提高吞吐但增加未确认内存和重投范围；Quorum 提高可靠性但增加写延迟。

## 发布侧优化

### 1. 复用连接和 channel

每条消息建连接是最常见的性能杀手。连接建立涉及 TCP/TLS/认证/vhost，成本远高于发布本身。

### 2. 批量发布 + 批量确认

```python
# pika BlockingChannel：开启 confirm 后，basic_publish 会等待该条的 confirm
channel.confirm_delivery()
for body in batch:
    channel.basic_publish(...)
```

上面的写法在 pika 阻塞客户端里其实是逐条等待；要批量窗口，需要用异步客户端（aio-pika/pika SelectConnection）或 Java 客户端的 `waitForConfirms()`，按序号维护未确认集合。不要在同步客户端里假装“发一批再统一等”——API 不支持就会退化成逐条 RTT。

### 3. 控制消息大小

大消息会：

- 占用更多内存/磁盘；
- 增加网络传输；
- 放大复制成本；
- 增加消费者处理时间；
- 触发帧分片。

对图片/大文件，存对象存储，消息只传引用和校验和。

### 4. 减少不必要的属性/headers

headers 会增加消息体积和路由匹配成本。不要把整个 JSON 元数据塞进 headers。

### 5. 合理使用持久化

关键消息必须 persistent + confirm；非关键遥测可以用 transient 或批量写入外部存储。不要为了“看起来可靠”把所有消息都设为 persistent，导致磁盘/复制成为瓶颈。

### 6. 避免 topology churn

每次请求 declare queue/exchange 会产生管理开销和元数据竞争。启动时声明，或由平台统一管理。

## 消费侧优化

### 1. prefetch 匹配处理能力

prefetch 太小：消费者频繁等待，吞吐低；
prefetch 太大：消息囤积、内存高、故障重投范围大、公平性差。

经验起点：

- 快处理（<10ms）：20～100；
- 中等（10～500ms）：5～20；
- 慢处理（>1s）：1～5；
- 批处理：按批次大小设置。

最终用压测和下游容量校准。

### 2. 消费者并发

增加消费者可以提高吞吐，但受限于：

- 队列/分区数量；
- 下游数据库/API 容量；
- broker 连接/channel 资源；
- 顺序要求；
- prefetch 和 ack 模式。

如果单队列 + 多消费者仍不够，考虑分片队列（按 key）或 Stream 分区。

### 3. 批量 ack

批量 ack 减少协议开销，但只 ack 已完成的消息。不要为了吞吐提前 ack。

### 4. 避免阻塞事件循环

在 asyncio/Node 等模型中，CPU 密集或同步 IO 会阻塞心跳和消费。用线程池/worker 拆分。

### 5. 优雅关闭

关闭时等待在途任务，避免大量重复投递和冷启动抖动。

## Broker 侧优化

### 队列类型

- Classic：低延迟、单节点、支持优先级；
- Quorum：复制、多数派、写延迟更高；
- Stream：日志、retention、offset。

不要把低价值临时队列都做成 quorum；也不要把关键订单放在单节点 classic。

### 路由复杂度

- Direct 最快、最可预测；
- Topic 需要模式匹配，绑定多时成本上升；
- Headers 最重；
- E2E 链增加递归和潜在重复；
- 大量绑定/交换机增加内存和管理开销。

### 队列数量与长度

- 大量队列增加元数据/调度开销；
- 超长队列触发分页/磁盘 IO；
- 单队列是吞吐上限，分片可扩展；
- 但分片增加消费者/运维复杂度。

### 内存与磁盘

- 内存告警会阻塞发布；
- 分页会显著降低消费速度；
- 磁盘 IOPS/fsync 是持久化瓶颈；
- Quorum 需要低延迟网络和稳定磁盘；
- TLS 增加 CPU 开销。

## 基准测试方法

### 工具

- RabbitMQ PerfTest（官方压测工具）；
- 自定义生产回放；
- 客户端库自带 benchmark；
- 系统指标（CPU、内存、磁盘、网络）。

### 原则

1. **固定变量**：消息大小、路由、持久化、confirm、消费者数；
2. **预热**：JIT、连接、缓存、页缓存；
3. **测分布**：p50/p95/p99，不只看平均；
4. **避免 coordinated omission**：按固定速率发压，记录真实延迟；
5. **观察资源**：CPU、内存、磁盘 IOPS、网络、GC；
6. **逐步加压**：找到拐点，而不是只测一个点；
7. **测故障**：节点重启、leader 切换、网络抖动；
8. **测恢复**：积压后消费者追赶时间；
9. **记录配置/版本**：可复现；
10. **和生产同构**：TLS、插件、队列类型、消息大小。

### 指标

- publish rate、confirm latency；
- deliver rate、ack rate；
- ready/unacked；
- p50/p99 端到端延迟；
- CPU/内存/磁盘/网络；
- 错误/nack/return/redelivery；
- 队列恢复时间。

## 常见性能反模式

| 反模式 | 后果 | 修正 |
|---|---|---|
| 每消息建连接 | 连接风暴、低吞吐 | 连接复用 |
| 每消息同步 confirm | RTT 限制吞吐 | 批量/异步 confirm |
| prefetch 极大 | 消息囤积、重投范围大 | 按处理能力调小 |
| 依赖 global QoS / 全局 prefetch=1 | 4.3+ 对 Quorum/Stream 拒绝；吞吐低 | 用 per-consumer prefetch，按场景调整 |
| 所有消息 persistent | 磁盘/复制瓶颈 | 分层可靠性 |
| 所有队列 quorum | 资源浪费/延迟高 | 按重要性选择 |
| 单队列无限增长 | 分页/磁盘告警 | max-length/DLX/扩容 |
| headers 塞大 JSON | 体积/匹配成本 | 精简 headers |
| 消费者阻塞事件循环 | 心跳超时/假死 | 异步/线程池 |
| 无限重试 | 重试风暴 | 退避/上限/DLQ |

## 一个调优顺序

1. 明确 SLO（吞吐、p99、错误率）；
2. 压测基线；
3. 找瓶颈（发布/路由/存储/消费/网络）；
4. 先修架构问题（连接复用、批量、队列类型、分片）；
5. 再调参数（prefetch、并发、确认批次）；
6. 验证故障/恢复；
7. 监控回归。

不要一上来改 `prefetch` 或内存水位；先确认瓶颈在哪。

## Mini 的性能边界

Mini-RabbitMQ 是教学实现：

- Python GIL/锁限制并发；
- JSON 快照全量写入；
- 无磁盘段/WAL；
- 无真实 Quorum 网络复制；
- 无零拷贝/credit 流控；
- 无性能基准意义。

可以用它观察“批量 confirm 减少 RPC”“prefetch 限制未确认数”“持久化快照变慢”，但不能把它的 TPS 当成 RabbitMQ 的参考。真实性能请用官方 RabbitMQ + PerfTest + 生产同构环境。

## 性能检查清单

- [ ] 连接/channel 复用；
- [ ] 发布批量 + confirm 批处理；
- [ ] 消费 prefetch 与处理能力匹配；
- [ ] 消费者并发与下游容量匹配；
- [ ] 消息大小受控；
- [ ] 持久化分层；
- [ ] 队列类型匹配业务；
- [ ] 队列长度/分片规划；
- [ ] 监控 p99/资源/积压；
- [ ] 压测包含故障与恢复；
- [ ] 调参有基线、有回归。

## PerfTest 实战命令

官方 PerfTest 是最常用的压测工具。下面展示参数结构，具体以目标版本 `--help` 为准：

```bash
# 发布：1 KB、persistent、每 100 条 confirm、4 生产者、Quorum
java -jar perf-test.jar \
  --uri amqp://app:password@broker.internal/%2f \
  --id publish-lab --size 1024 --time 60 \
  --producers 4 --qos 100 --confirm 100 \
  --flag persistent --queue-args x-queue-type=quorum

# 消费：8 消费者、prefetch 20、手动 ack
java -jar perf-test.jar \
  --uri amqp://app:password@broker.internal/%2f \
  --id consume-lab --queue publish-lab --time 60 \
  --consumers 8 --qos 20

# 固定速率观察尾延迟
java -jar perf-test.jar \
  --uri amqp://app:password@broker.internal/%2f \
  --id latency-lab --size 512 --rate 5000 --time 120 \
  --producers 2 --confirm 100
```

不要只测一个点。完整矩阵、指标采集、结果解读和容量公式见[附录 L](../appendices/L-performance-benchmark-playbook.md)。

## Benchmark 矩阵与结果解读

至少覆盖：

| 维度 | 取值 | 观察 |
|---|---|---|
| 消息大小 | 128 B / 1 KB / 16 KB / 256 KB | 吞吐、CPU、网络、分帧 |
| 持久化 | transient / persistent | 磁盘 IO、confirm 延迟 |
| confirm | 关 / 每条 / 每 100 条 | 吞吐 vs 尾延迟 |
| 队列类型 | classic / quorum / stream | 写放大、恢复 |
| 节点数 | 1 / 3 / 5 | 多数派成本、容错 |
| 消费者 | 1 / 4 / 16 / 64 | 拐点、下游瓶颈 |
| prefetch | 1 / 10 / 100 / 1000 | 未确认内存、公平性 |
| TLS | 开 / 关 | CPU、握手 |

读结果时：

- 看 p50/p99/p999，不只看平均；
- 区分 publish-only、consume-only、端到端；
- 固定速率发压，避免 coordinated omission；
- 记录硬件/版本/配置，保证可复现；
- 测故障（leader 切换/节点重启/磁盘慢）和积压恢复；
- 不用一次结果做容量承诺。

## 4.x 的消息大小与帧

- `frame_max` 默认常见 131072 字节，消息会自动拆成多个 BODY 帧；
- `max_message_size` 4.0 起默认 16 MiB（3.13 为 128 MiB）；
- 超过 `max_message_size` 的发布失败，不是“自动拆成多条消息”；
- 大消息放大内存/磁盘/复制/网络成本；
- 压测要覆盖真实消息大小分布（p99 往往比平均重要）。

## 4.3/4.4 的性能变化

版本升级会改变性能基线，不能直接套用旧压测结果：

- **4.3 Quorum 压缩**：回收失效段，降低“短队列高磁盘”的占用和重启恢复时间；压测要同时看吞吐、p99 和磁盘；
- **4.3 消息内存开销**：官方给出 ≤32 KiB 消息约 50% 的单消息内存下降；小消息密集场景的容量模型要重算；
- **4.3 原生延迟重试**：失败消息在队列内放置，不重写消息，减少重试路径的发布/确认开销；
- **4.3 32 级优先级**：每个优先级独立计数/调度，高基数优先级会增加管理面和内存开销；
- **4.3 Khepri**：大量 vhost/队列的启动、滚动重启可能更快，但元数据写入要求多数派；
- **4.4 CQv2 共享存储 v2**：`.sqs` 文件格式改变，磁盘 IO/恢复行为要重新基线；
- **4.4 原生 STOMP**：STOMP 连接的进程/内存模型变化，旧容量阈值不再准确。

升级后的性能验证要和旧版本同负载并排跑，而不是只跑一次“看起来没问题”。

## 性能回归测试

性能不是一次性项目，而是持续回归：

1. 固定基准环境（硬件/镜像/配置/数据）；
2. 固定工作负载（大小/速率/确认/队列类型）；
3. 每次发布/升级/配置变更后重跑；
4. 记录 p50/p99、吞吐、错误率、资源；
5. 设回归阈值（例如 p99 退化 > 20% 告警）；
6. 保存原始结果/图表/版本信息；
7. 区分“噪声”和“真实回归”（多次运行/置信区间）。

没有回归基准的团队，通常在上线后才发现“某个版本变慢了”。

## 本章小结

性能是端到端系统工程：发布确认、路由、存储、消费、网络和故障恢复共同决定。优化顺序是先架构后参数，先基线后调优。批量 confirm、合理 prefetch、连接复用、队列类型和分片是最有效的杠杆；Mini 只能用于理解机制，不能作为生产性能基准。

下一章把所有可靠性拼接起来：至少一次、幂等、outbox、顺序、Saga、对账和 DLQ 运营，形成端到端一致性方案。

---

[上一章：第 17 章 运维、部署与容量规划](17-operations-capacity.md) | [返回目录](../README.md) | [下一章：第 19 章 端到端可靠性与数据一致性](19-end-to-end-reliability.md)
