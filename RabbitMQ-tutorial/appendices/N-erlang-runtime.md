# 附录 N Erlang/OTP 运行时、进程模型与调优

很多 RabbitMQ 的“奇怪行为”不是 AMQP 语义问题，而是 Erlang 运行时问题：为什么一个队列会占一个进程？为什么内存没满却开始 blocked？为什么容器里 CPU 没用满但延迟很高？为什么节点“看起来活着”却被判定下线？

本附录从运行时视角解释这些现象，并给出可执行的诊断顺序。它不试图把你变成 Erlang 专家，但会让你在排障时知道该看什么、不该盲目调什么。

## 为什么 RabbitMQ 的行为像“很多小进程”

RabbitMQ 用 Erlang/OTP 构建。Erlang 的并发单位不是线程，而是**轻量进程**：

- 创建成本低，可以有几十万甚至上百万个；
- 由 VM 抢占式调度，单个进程不会永久占住 CPU；
- 每个进程独立 GC，避免全局停顿；
- 进程间通过消息传递，默认复制消息（大二进制是引用计数共享）；
- 进程崩溃可以被监督树隔离/重启。

这带来两个直接后果：

1. **队列、连接、信道都是进程**，不是“数据结构里的一条记录”。队列数量、连接数量会直接变成进程数量；
2. **内存分布在大量进程堆和二进制堆中**，不能只看“一个进程用了多少内存”。

## 进程模型：从连接到队列

一个简化视图：

```text
TCP connection
  └── rabbit_reader / connection process
        ├── channel process 1
        ├── channel process 2
        └── ...

queue
  ├── classic queue process (+ message store / index)
  ├── quorum queue: Raft leader/follower processes
  └── stream: segment/log processes
```

不同版本内部命名不同，但职责稳定：

- **connection process**：读写 socket、握手、心跳、帧解析；
- **channel process**：方法分发、confirm、ack、拓扑操作；
- **queue process**：入队、出队、ack、TTL、死信、消费者调度；
- **Raft/stream 进程**：复制、日志、快照、offset。

这解释了为什么：

- 大量队列会消耗大量进程和内存，哪怕队列是空的；
- 一个“卡住”的队列进程可能拖慢该队列的所有消费者；
- 连接数暴涨会同时消耗 socket、进程、内存和调度器时间；
- Quorum 队列比 classic 队列重，因为它有 Raft group、日志和复制状态。

## 调度器与 CPU

Erlang VM 默认按 CPU 核数启动调度器。关键参数：

| 参数 | 作用 | 注意 |
|---|---|---|
| `+S N:M` | 总调度器数:在线调度器数 | 容器里不要超过 CPU limit |
| `+A N` | 异步线程池（文件 IO 等） | 盲目加大不会提升吞吐 |
| `+P N` | 最大进程数 | 默认很高，通常不需要改 |
| `+Q N` | 最大端口数 | 受文件描述符限制 |
| `+sbwt` / `+swt` | 调度器忙等策略 | 容器 CPU quota 下可减少空转 |
| `+hms` / `+hmqd` | 进程最小/最大堆 | 调优需测量，不要照抄 |

设置方式通常是通过环境变量追加：

```bash
export RABBITMQ_SERVER_ADDITIONAL_ERL_ARGS="+S 4:4 +A 16"
```

原则：

- `+S` 与容器的 CPU limit 对齐；超过 limit 会导致频繁被 cgroup  throttling，延迟反而变差；
- `+A` 只在确有大量文件 IO 阻塞时增加；
- 不要在没有基线的情况下改 `+hms`/`+hmqd`/allocator 参数；
- 先用 `rabbitmq-diagnostics runtime_thread_stats` 看调度器/脏调度器/异步线程的忙闲，再决定是否调。

一个常见误区是“CPU 没用满，所以不是 CPU 问题”。Erlang 调度器可能被 cgroup 限流、被锁竞争阻塞、或在等 IO；`top` 的 CPU 利用率不足以判断。

## 内存与 GC

RabbitMQ 的内存由多部分组成：

- 进程堆（消息、队列状态、连接状态）；
- 二进制堆（消息 body、大 header）；
- ETS 表（索引、元数据、路由表）；
- code/atom（模块、原子，原子不 GC）；
- 驱动/端口/文件缓存。

常用诊断：

```bash
rabbitmq-diagnostics memory_breakdown
rabbitmqctl status
rabbitmqctl eval 'erlang:memory().'
rabbitmqctl eval 'erlang:memory(binary).'
rabbitmqctl eval 'erlang:system_info(process_count).'
```

`memory_breakdown` 会把内存拆成 connection_readers、connection_writers、channel、queue_procs、binary、code、ets 等。先定位“哪一类在涨”，再谈调优。

### 内存水位与 blocked

`vm_memory_high_watermark` 控制发布端阻塞：

```ini
vm_memory_high_watermark.relative = 0.6
# 或
vm_memory_high_watermark.absolute = 2GB
```

达到水位后，RabbitMQ 会阻塞**发布连接**（不是消费连接），让消费者有机会追上。常见误判：

- “内存没到 100%，为什么 blocked？”——水位是相对可用内存的比例，不是物理内存上限；
- “容器 limit 是 4 GB，为什么 2.4 GB 就 blocked？”——Erlang 看到的总内存、cgroup limit、`total_memory_available_override_value` 可能不一致；
- “blocked 了是不是要调高水位？”——调高只是延后问题，先看积压、消费者和分页。

容器里要显式设置 `total_memory_available_override_value`，否则 VM 可能按宿主机内存计算水位。

### 分页与 GC

classic queue 在内存压力下会把消息分页到磁盘；quorum 用日志/段；stream 用段文件。分页不是“免费扩容”，它会显著增加延迟和磁盘 IO。

Erlang 的 per-process GC 通常不是停顿来源，但以下情况会放大内存：

- 大量小进程持有大 binary 引用；
- 消息 header 过大；
- 队列索引/ETS 膨胀；
- atom 泄漏（动态生成 atom，永不回收）；
- 插件持有全局状态。

不要用 `erlang:garbage_collect()` 手工“清理”生产节点；GC 是症状，不是根因。

## Erlang distribution 与集群

集群节点通过 Erlang distribution 通信：

- **epmd**：4369/TCP，节点名注册；
- **distribution**：常见 25672/TCP；
- **CLI**：35672–35682/TCP（工具与节点通信）；
- **cookie**：`~/.erlang.cookie` 或 `RABBITMQ_ERLANG_COOKIE`，集群共享；
- **node name**：`rabbit@host`，主机名/DNS 必须稳定。

`net_ticktime` 控制节点存活检测：

```ini
net_ticktime = 60
```

它影响“多久判定对端失联”。太小会因网络抖动误判，太大则故障发现慢。跨 AZ/跨地域部署时，要结合 RTT 和丢包设置。

常见集群问题：

- 容器重启后 hostname 变化，节点身份变了，无法重新加入；
- DNS 解析不稳定，节点互相看不到；
- cookie 不一致，加入集群被拒；
- 防火墙只开了 5672，没开 4369/25672；
- 时钟漂移影响证书、token、日志排序（Erlang 分布本身不要求严格时钟同步，但业务语义需要）。

4.3 起 Khepri 是唯一元数据存储，元数据写入要求多数派在线；这改变了“少数派节点还能做什么”的预期。`cluster_partition_handling` 等 Mnesia 时代配置不再生效，故障语义由 Raft 统一处理。

## 端口、文件描述符与限制

RabbitMQ 会消耗大量文件描述符：

- 每个客户端连接一个 socket；
- 每个监听器一个 socket；
- 文件/日志/管道/驱动端口；
- Erlang 的 port 数量受 `+Q` 和 OS `ulimit -n` 限制。

检查：

```bash
ulimit -n
rabbitmqctl eval 'erlang:system_info(port_count).'
rabbitmqctl eval 'erlang:system_info(process_count).'
rabbitmq-diagnostics check_port_connectivity
```

生产建议：

- `ulimit -n` 至少数万（按连接数规划）；
- systemd 单元里显式设置 `LimitNOFILE`；
- 容器里设置 `nofile` ulimit，不要依赖默认值；
- 监控 FD 使用率，而不是等“too many open files”。

## 诊断工具箱

| 命令 | 用途 |
|---|---|
| `rabbitmq-diagnostics status` | 节点、内存、磁盘、告警、监听器 |
| `rabbitmq-diagnostics memory_breakdown` | 内存分类 |
| `rabbitmq-diagnostics runtime_thread_stats` | 调度器/脏调度器/异步线程 |
| `rabbitmq-diagnostics processes` | 进程数与内存占用 |
| `rabbitmq-diagnostics environment` | 运行时配置 |
| `rabbitmq-diagnostics check_port_connectivity` | 节点间端口 |
| `rabbitmq-diagnostics check_local_alarms` | 内存/磁盘告警 |
| `rabbitmqctl list_connections` | 连接状态、通道数、收发 |
| `rabbitmqctl list_channels` | 信道、prefetch、未确认 |
| `rabbitmqctl list_queues` | 队列深度、消费者、内存 |
| `rabbitmq-plugins enable rabbitmq_top` | 进程级观察（管理 UI） |
| `rabbitmq-diagnostics observer` | Observer GUI（需要图形环境） |

`observer` 在无 GUI 的服务器上通常不可用；用 `rabbitmq-top` 插件或 `runtime_thread_stats` 替代。

## 崩溃转储与日志

Erlang VM 崩溃时会生成 `erl_crash.dump`。它包含：

- slogan（崩溃原因）；
- 内存/进程/端口/atom/ETS 统计；
- 进程栈和消息队列；
- 调度器/运行队列信息。

排查顺序：

1. 看文件头部的 slogan；
2. 看内存分类，判断是 binary、进程堆还是 ETS；
3. 看进程数量最多的模块；
4. 看是否有异常大的消息队列/二进制；
5. 结合崩溃前日志、监控、变更记录。

不要把 crash dump 直接贴到公开 issue；它可能包含消息内容、主机名、用户名、拓扑等敏感信息。

日志方面，4.x 使用 Erlang logger；至少配置：

- 日志级别（生产不要长期 debug）；
- 文件轮转/大小；
- 崩溃日志位置；
- 审计日志单独保留；
- 日志中不要打印密码/token/消息 body。

## 调优方法论

正确的顺序是：

1. **定义 SLO**：延迟、吞吐、错误率、积压恢复时间；
2. **建立基线**：固定负载、固定版本、固定硬件；
3. **采集**：客户端、broker、OS 三层指标；
4. **定位瓶颈**：CPU、内存、磁盘 IO、网络、锁、GC、下游；
5. **一次只改一个变量**；
6. **回归**：同负载复测，观察尾延迟和故障恢复；
7. **记录**：版本、参数、拓扑、消息大小、确认模式。

反模式：

- 照抄网上的 `+S`/`+A`/allocator 参数；
- 只看平均延迟，不看 p99/p999；
- 用“消息越小越快”掩盖业务语义问题；
- 把 `vm_memory_high_watermark` 调到 0.9 来“解决” blocked；
- 在容器里给 Erlang 分配超过 CPU limit 的调度器；
- 没有基线就升级/调参，无法判断回归。

## 容器与 Kubernetes 特别注意

- **CPU limit**：`+S` 不要超过 limit；否则 cgroup throttling 会让尾延迟变差；
- **内存 limit**：给 Erlang 堆、binary、页缓存、OS 留余量；`vm_memory_high_watermark` 要按容器可用内存计算；
- **`total_memory_available_override_value`**：容器里显式设置，避免 VM 误判宿主机内存；
- **hostname**：StatefulSet 的稳定网络标识是节点身份的一部分，不要随意改；
- **PVC**：每副本独立 PVC；PVC 快照不等于消息备份；
- **反亲和**：避免所有副本落同一节点；
- **网络策略**：开放 4369、25672、5672/5671、15672/15692、以及协议插件端口；
- **优雅终止**：给足 `terminationGracePeriodSeconds`，让连接迁移/队列 leader 切换完成；
- **升级**：按官方路径矩阵升级，4.3 前完成 Khepri 迁移，4.4 注意 `.sqs` 单向门。

## Mini-RabbitMQ 的运行时对照

Mini 是单进程 Python 实现，和 Erlang 运行时差异很大：

| 维度 | Mini | RabbitMQ |
|---|---|---|
| 并发单位 | 线程 | 轻量进程 |
| 调度 | OS 线程 + GIL | VM 抢占式调度 |
| 隔离 | 无（一个异常可能影响全局） | 进程/监督树 |
| GC | Python 引用计数 + 分代 GC | 每进程 GC + binary 引用计数 |
| 分布式 | 单进程模拟 | Erlang distribution |
| 崩溃恢复 | 快照 | 日志/段/Raft |
| 背压 | 队列长度/锁 | 内存水位/credit/分页 |

Mini 的 `Broker`、`VHost`、`Queue` 用锁保护共享状态；这在教学规模下足够，但不能模拟 Erlang 的抢占式调度、进程隔离和分布式故障。把 Mini 的线程模型当成 RabbitMQ 的运行时模型，是典型的错误类比。

## 小结

RabbitMQ 的运行时是 Erlang/OTP：连接、信道、队列、Raft 都是进程；调度器、内存水位、GC、distribution、文件描述符共同决定生产行为。排障时先定位“哪类资源在涨”，再决定是否调参；调优前先有基线和 SLO。容器环境要特别注意 CPU/内存 limit、hostname、端口和优雅终止。Mini 用线程和锁模拟了语义，但没有模拟 Erlang 的运行时，两者不能混为一谈。

---

[返回目录](../README.md) | [上一章：附录 M 插件体系、扩展开发与二次开发](M-plugin-development.md) | [下一章：附录 O 事件契约、Schema 治理与事件目录](O-event-contracts-governance.md)
