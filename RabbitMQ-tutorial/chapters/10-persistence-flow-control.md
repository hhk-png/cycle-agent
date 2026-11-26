# 第 10 章 持久化、恢复与流控

> 本章定位：理解 RabbitMQ 如何把消息放到磁盘、什么时候认为写入成功、内存/磁盘告警如何保护 broker，以及“持久化不等于备份”的边界。

## 持久化的四个层次

“消息会不会丢”要分层次回答：

1. **队列元数据**：交换机、队列、绑定、策略是否在重启后存在；
2. **消息内容**：消息是否写入磁盘/复制到多数派；
3. **确认语义**：发布者是否收到持久化/复制完成的 confirm；
4. **灾难恢复**：磁盘损坏、机房故障、误删队列时是否有备份。

RabbitMQ 的 durable queue/exchange 解决第 1 层；消息 `delivery_mode=2` 解决第 2 层的一部分；publisher confirms 解决第 3 层；备份/异地复制解决第 4 层。四层不能互相替代。

## Classic Queue 的存储

Classic queue 在单节点上存储消息。RabbitMQ 4.x 的 classic queue v2 改进了内存占用、分页和恢复行为。消息可能处于：

- 内存中（热数据）；
- 分页到磁盘（paging）；
- 持久化到队列段文件；
- 被消费者预取但未确认。

持久化消息在发布时写入磁盘，但写入策略受 fsync/批处理影响。即使消息已写盘，如果发布者没等 confirm，也不能假设“已可靠接受”。非持久消息在内存压力下可能被换出，但节点重启后不保证恢复。

Classic 队列的优点：

- 低延迟、资源开销相对小；
- 支持优先级；
- 适合大量短生命周期队列；
- 单节点吞吐好。

缺点：

- 不跨节点复制；
- 节点故障可能丢失未复制消息；
- 恢复大量队列/消息时可能较慢；
- 镜像队列在 RabbitMQ 4.x 已不再是推荐路径（经典镜像队列已移除）。

## Quorum Queue 的存储

Quorum Queue 基于 Raft：

- 一个 leader，多个 follower；
- 写入需要多数派确认；
- leader 故障后选举新 leader；
- 日志持久化在多个节点；
- 支持 delivery-limit、dead-letter、TTL、max-length 等；
- 优先级模型分版本：4.0–4.2 为 normal/high 两档，4.3+ 为 32 级严格优先级；两者都不使用 classic 的 `x-max-priority`；

写入路径大致是：

```text
publish -> leader append log -> replicate to followers
        -> majority durable -> commit -> publisher confirm
```

因此 quorum 的 confirm 延迟通常高于 classic，但故障恢复语义更强。3 节点集群可以容忍 1 个节点故障；5 节点容忍 2 个，但写入延迟和资源成本更高。

4.3 起 Quorum 队列会做**日志压缩**：Raft 段中已被覆盖/删除的数据会被回收，缓解“队列长度很短但磁盘占用很高”的问题，降低磁盘告警和重启恢复成本。压缩是后台行为，不等于备份，也不改变消息语义；容量规划仍要按峰值积压留余量。

Quorum 不适合：

- 大量临时/低价值队列；
- 需要 classic 式精细优先级参数、且不愿接受 Quorum 版本差异的场景；
- 极高队列/消息数量但低价值数据；
- 无法保证稳定网络的跨地域部署（除非接受高延迟）。

## 内存与磁盘告警

RabbitMQ 用高水位保护自己：

- **内存告警**：当 broker 使用内存超过配置阈值时阻塞发布连接。4.0 起默认 `vm_memory_high_watermark.relative = 0.6`（60%），3.x 常见默认是 40%；升级后容量模型要按新阈值重算。
- **磁盘告警**：当磁盘剩余空间低于 `disk_free_limit` 时阻塞发布。默认常见是 50 MB；生产应显式设置绝对值（如 5–10 GB）或相对值，不要依赖默认。

告警期间：

- 发布者收到 `connection.blocked`；
- 消费通常仍可继续，让队列排空；
- 管理 API 显示 alarm；
- 健康检查可能失败；
- 如果积压继续增长，可能触发分页甚至 OOM。

客户端应监听 blocked/unblocked，暂停发布并告警。不要在 blocked 时无限重试发布，否则会加重 broker 负担。

Mini 用 `Broker.set_alarm` 模拟：设置 memory/disk 告警后，publish 抛 `ResourceError`，服务端向连接发送 `connection.blocked`。这不是完整的 credit-based 流控，但足以演示“告警 → 阻塞发布 → 恢复”的状态转换。

## 分页（Paging）与 4.x 的内存窗口

“分页”是 CQv1 时代的概念：内存不足时把消息换出到磁盘。4.x 的 CQv2、Quorum 和 Stream 默认就是磁盘驻留 + 内存窗口，旧的分页计数/`vm_memory_high_watermark_paging_ratio` 语义已经变化甚至废弃；`messages_paged_out` 不应再作为 4.x 的核心告警指标。

4.x 更实用的内存控制手段：

- `vm_memory_high_watermark`：超过阈值阻塞发布；
- `x-max-in-memory-length` / `x-max-in-memory-bytes`：限制队列在内存中的消息量；
- 队列积压时更多消息留在磁盘，消费速率决定内存窗口；
- 监控 blocked 时长、队列深度、磁盘 IO 和消费者追赶速度。

分页/磁盘驻留不是“无限内存”的替代品。磁盘 IO 仍会成为瓶颈，恢复时也可能产生大量读取。容量规划要按“积压期间仍能满足 SLO”来算，而不是假设 broker 能无限吸收。

## 流控与 credit

RabbitMQ 的流控是分层的：

- 连接级 blocked（内存/磁盘告警）；
- AMQP 0-9-1 的 prefetch（`basic.qos`）和 consumer 处理速度；
- 队列索引/存储的背压；
- Erlang 进程邮箱/内存限制。

注意：**AMQP 0-9-1 没有链路级 credit**；credit 是 AMQP 1.0 和 Stream 协议的概念。0-9-1 客户端应通过 per-consumer prefetch 和 `connection.blocked` 做背压，不要把 1.0 的 credit 模型套过来。

客户端库通常把 blocked 事件暴露为回调。应用应：

1. 暂停新发布；
2. 保留本地缓冲/降级；
3. 监控 blocked 持续时间；
4. 恢复后缓慢放量，避免瞬间打满；
5. 对关键消息使用 confirm + 重试 + 幂等。

## 持久化的代价

持久化不是免费的：

- 每次写入可能触发 fsync/复制；
- 大量小消息放大元数据和索引开销；
- 队列恢复需要扫描段文件；
- Quorum 写入需要网络往返；
- 磁盘 IOPS/带宽成为瓶颈；
- 备份和恢复时间随数据量增长。

优化方向：

- 批量发布/批量 confirm；
- 合并小消息（但注意延迟和语义）；
- 对非关键消息使用 transient；
- 控制队列数量和单队列长度；
- 使用 SSD 和独立磁盘；
- 监控 fsync/复制延迟；
- 对 quorum 使用合适的节点数和网络。

## 持久化不等于备份

durable/persistent 只保证“正常重启后尽量恢复”。它不能防止：

- 误删队列/交换机；
- 错误的 policy 导致消息被丢弃；
- 磁盘损坏；
- 集群整体故障；
- 勒索软件/权限滥用；
- 逻辑 bug 把消息消费掉。

备份需要：

- 定期导出 definitions（拓扑、用户、权限、策略）；
- 对 quorum 队列做消息级备份/异地复制；
- 使用 Shovel/Federation 或外部归档；
- 保留可重放的事件日志；
- 定期演练恢复；
- 记录 RPO/RTO。

RabbitMQ 没有“一键备份所有队列消息”的通用命令。Classic 队列依赖底层存储/快照，Quorum 依赖 Raft 日志和节点；生产方案通常是“上游事件日志/对象存储归档 + 拓扑 definitions 备份”。

## 恢复流程

发生节点/集群故障后：

1. 确认磁盘、网络、Erlang cookie、节点名；
2. 启动 broker，观察元数据恢复；
3. 检查队列类型、leader、副本状态；
4. 恢复 definitions（如果元数据丢失）；
5. 验证交换机/队列/绑定/策略；
6. 检查未确认消息、DLQ、积压；
7. 逐步恢复消费者，控制速率；
8. 对重复/丢失消息做业务对账。

不要一上来就全量重放 DLQ 或清空队列。先确认根因和幂等性。

## Mini 的快照实现

`mini_rabbitmq/storage.py` 用原子替换的 JSON 快照：

```text
serialize broker state -> write temp file -> fsync (optional) -> os.replace
```

优点：

- 易读、易调试；
- 原子替换避免半写文件；
- 能恢复 durable 拓扑、persistent 消息、用户、策略；
- 测试简单。

缺点：

- 全量快照，写入成本随状态增长；
- 没有 WAL/段文件；
- 不能承受高频写入；
- 没有 Quorum 复制；
- 崩溃窗口内可能丢最近消息；
- 不适合生产。

Mini 在 `tests/test_persistence.py` 中验证了：

- durable 拓扑和 persistent 消息恢复；
- transient 消息不恢复；
- ack 后不恢复；
- 未确认 persistent 消息恢复为 ready；
- policy/用户/vhost 恢复；
- 损坏快照报错；
- 关闭自动 checkpoint 后显式 flush。

这些测试证明“语义边界”，不证明性能或灾难恢复能力。

## 容量与磁盘规划

一个粗略公式：

```text
磁盘需求 ≈ 消息数 × (消息大小 + 元数据开销) × 副本因子
         + 索引/段文件开销
         + 安全余量
```

还要考虑：

- 峰值积压；
- DLQ 保留；
- 备份副本；
- 日志；
- 操作系统和 Erlang 运行时；
- 磁盘水位阈值（低于阈值会阻塞发布）。

建议：

- 磁盘使用率长期低于 60%；
- 单独监控数据盘；
- 预留至少一次全量恢复的空间；
- 对 quorum 节点使用相同规格；
- 定期清理无用队列和 DLQ；
- 对超大消息使用对象存储 + 引用，而不是直接塞进队列。

## Classic Queue v2 与 Quorum 的存储布局

理解存储布局，才能判断“confirm 之后到底安全到什么程度”。

### Classic Queue v2

- 消息写入队列的段文件/索引结构，内存中只保留一部分；
- 消费速率决定内存窗口，积压时更多消息在磁盘；
- 持久化消息在发布时写入存储路径，但 classic 队列**不会在 publisher confirm 前强制 fsync**；
- 节点故障时，单副本队列的数据可能不可用或丢失；
- `x-queue-mode` 已不再需要，classic 默认就接近 lazy 行为。

因此 classic 的 confirm 只表示“broker 已接受并进入存储路径”，不是“已 fsync 到磁盘”，更不是“多数派持久化”。服务器崩溃时，已 confirm 的 durable 消息仍可能丢；需要更强保证时用 Quorum Queue。不要把 classic confirm 当成数据库事务提交。

### Quorum Queue

- 每个队列一个 Raft group；
- leader 追加 WAL/日志，复制到 follower；
- 多数派持久化后 commit，再对发布者 confirm；
- 日志会周期性快照（snapshot），避免无限增长；
- 消费者只能看到已提交消息；
- 节点恢复时通过日志/快照追赶。

Quorum 的 confirm 比 classic 强（多数派持久化），但代价是网络往返、fsync、leader 负载。3 节点容忍 1 节点故障；5 节点容忍 2 个，但写延迟更高。

## fsync、confirm 与“到底丢不丢”

把问题拆成四个时间点：

```text
t1 客户端 write() 成功        -> 只证明进了内核缓冲区
t2 broker 收到并路由          -> 可能还在内存
t3 broker 写入磁盘/多数派      -> 持久化
t4 消费者处理并 ack           -> 业务完成
```

- 客户端 `basic_publish` 返回：最多到 t1/t2，不代表 t3；
- publisher confirm：quorum 到 t3（多数派持久化）；classic 通常只到 t2/t3 之间，不保证 confirm 前 fsync；
- consumer ack：t4，且只代表“这个消费者说处理完了”；
- 业务成功：还需要数据库事务/幂等/对账。

所以：

- 关键消息：durable exchange/queue + persistent + confirm + quorum（或可靠的 classic + 备份）；
- 不能接受重复：必须幂等；
- 不能接受丢失：还要备份/异地复制/上游事件日志；
- “confirm 成功”不等于“消费成功”，更不等于“业务成功”。

## 备份与恢复的具体命令

### 拓扑/配置

```bash
rabbitmqctl export_definitions /backup/definitions-$(date +%F).json
rabbitmqctl import_definitions /backup/definitions-2026-01-01.json
rabbitmqctl list_policies -p /app
rabbitmqctl list_permissions -p /app
```

### 节点数据

- Quorum/Classic 的消息数据在节点数据目录（常见 `/var/lib/rabbitmq/mnesia`，由 `RABBITMQ_MNESIA_DIR` 决定；4.3+ 的 Khepri 元数据也复用该目录，但内部布局不同）；
- 文件系统/云盘快照可以备份“整节点”，但必须保证一致性（停机或按存储/集群语义）；
- 不要把“PVC 快照”当成“消息备份”而不做恢复演练；
- 跨集群复制用 Federation/Shovel 或上游事件归档。

备份对象、Quorum/Stream 快照的一致性边界、跨区域复制方案、切换/回切 runbook 和演练验收，见[附录 U](../appendices/U-disaster-recovery-continuity.md)。

### 恢复顺序

1. 恢复节点身份（hostname/节点名/cookie）；
2. 恢复元数据（definitions；4.3+ 为 Khepri，旧版本可能是 Mnesia）；
3. 恢复消息数据（快照/日志）；
4. 启动 broker，检查队列/leader/副本；
5. 逐步恢复消费者，限速；
6. 对账重复/丢失。

### 备份检查

- [ ] definitions 定期导出并版本化；
- [ ] 消息数据有快照/复制/归档；
- [ ] 证书/密钥/配置独立备份；
- [ ] 恢复演练有记录；
- [ ] RPO/RTO 明确；
- [ ] 备份加密、访问受控；
- [ ] 恢复后能验证业务对账。

## 4.3/4.4 的存储与恢复变化

- **4.3：Khepri 成为唯一元数据存储**。拓扑、用户、policy 等元数据也走 Raft；元数据写入要求集群多数派在线。恢复语义更统一，但“少数派节点还能启动并读消息”这种旧习惯不再成立；
- **4.3：Quorum 日志压缩**。减少无效段占用，缩短重启恢复时间；监控磁盘时要把压缩进度和段数量一起看；
- **4.4：classic queue 共享存储 v2**。新文件使用 `.sqs` 扩展名，节点写入后不能降级到 4.3.x 或更早版本；升级前要确认回滚策略；
- **4.4：Stream 初始 offset**。`x-stream-initial-offset` 是创建 Stream 时的队列参数，决定第一条消息的 offset（创建后固定，非零值可能需要 4.4 feature flag）；它不是消费者起点，消费者仍用 `x-stream-offset`（first/last/next/数值）选择读取位置，两者不要混淆；
- **持久化仍然不等于备份**。Khepri/Quorum 复制的是当前状态，误删、逻辑损坏、勒索软件仍需要独立备份和恢复演练。

## 4.x 的 16 MiB 默认上限

4.0 把 `max_message_size` 默认值从 128 MiB 降到 16 MiB。影响：

- 大消息发布会失败；
- 失败可能表现为连接/信道关闭，而不是清晰的业务错误；
- 旧系统升级后可能突然丢发布；
- 需要改配置或改架构（对象存储 + 引用）。

```ini
max_message_size = 16777216
```

不要为了“兼容旧系统”直接设成无限大；大消息会放大内存、磁盘、复制和网络成本。先问“这条消息为什么这么大”，再决定是调上限还是改设计。

## 本章小结

持久化是分层契约：durable 元数据、persistent 消息、confirm、quorum 复制和备份各自解决不同问题。Classic 与 Quorum 的存储/复制模型不同，内存/磁盘告警会阻塞发布以保护 broker，分页能救急但会拖慢消费。Mini 的 JSON 快照是教学模型，不是生产 WAL。

下一章进入集群与 Quorum Queue：Erlang 分布式、Raft 角色、多数派、故障转移、网络分区和 Kubernetes 部署。

---

[上一章：第 09 章 死信、延迟与重试架构](09-dead-letter-delay-retry.md) | [返回目录](../README.md) | [下一章：第 11 章 集群、Quorum Queue 与故障转移](11-cluster-quorum.md)
