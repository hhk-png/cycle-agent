# 附录 Q RabbitMQ 内部架构与源码阅读地图

本附录把真实 RabbitMQ 的 broker 内部结构摊开：连接如何被读取、信道如何持有状态、交换机如何路由、队列如何存储与复制、Quorum Queue 和 Stream 分别依赖什么共识/日志库、元数据为什么在 4.3 之后需要多数派。它和第 20～25 章的 Mini-RabbitMQ 是互补关系：Mini 用几百行 Python 把语义压扁，便于理解和修改；本附录给你一张真实实现的地图，便于读源码、看指标和定位“语义之外”的问题。

版本说明：以下模块名、监督树和存储实现以 RabbitMQ 4.x 的代码结构为参照。RabbitMQ 会在小版本中重命名、拆分或合并模块，阅读时以目标 tag 的源码为准。本附录描述职责和调用关系，不承诺每个文件名逐字不变。

## 为什么读源码

会用客户端库，能解释 exchange、queue、ack、confirm，已经能解决大部分业务问题。但下面这些问题，只看文档往往解释不清：

- 为什么一个空队列也占内存和进程？
- 为什么 publisher confirm 已经返回，消费者还是可能收不到？
- 为什么 Quorum Queue 的磁盘写放大远高于 classic？
- 为什么 4.3 之后少数派节点“还活着”，却不能声明队列？
- 为什么管理 API 的指标和实际队列深度有延迟？
- 为什么一个慢客户端会拖住整条连接，而不是只影响自己的 channel？

这些问题的答案分布在协议层、broker 模块层和 Erlang 运行时层。第 03 章讲协议，附录 N 讲运行时，本附录补上中间一层：**broker 内部的进程、模块和数据流**。

## 代码仓库与模块分层

RabbitMQ 的主仓库是一个 Erlang/OTP monorepo，核心代码在 `deps/` 下按应用拆分：

```text
rabbitmq-server/
├── deps/
│   ├── rabbit/                 # 核心 broker：连接、信道、交换机、队列、策略、CLI
│   ├── rabbit_common/          # AMQP 0-9-1 编解码、公共库、CLI 公共代码
│   ├── rabbitmq_management/    # 管理 HTTP API 与 Web UI
│   ├── rabbitmq_stream/        # Stream 协议接入
│   ├── rabbitmq_mqtt/          # MQTT 接入
│   ├── rabbitmq_web_stomp/     # Web STOMP
│   ├── rabbitmq_federation/    # Federation
│   ├── rabbitmq_shovel/        # Shovel
│   ├── ra/                     # Raft 库（Quorum Queue 的复制基础）
│   ├── osiris/                 # 追加日志库（Streams 的存储基础）
│   └── khepri/                 # 基于 Raft 的元数据存储（4.x）
└── ...
```

几个容易误解的点：

- **`rabbit` 不是“一个模块”**，而是一个 OTP application，内部有几十个 gen_server、supervisor 和 behaviour；
- **管理面是插件**，不是数据面的一部分；管理 API 挂了，AMQP 发布/消费通常仍在工作；
- **Quorum Queue 的复制不是 `rabbit` 自己写的**，而是复用 `ra`；Stream 的日志复用 `osiris`；4.x 元数据复用 `khepri`；
- **AMQP 1.0 在 4.0 起是核心协议**，不再依赖一个可启停的独立插件来决定是否可用；旧插件名更多是兼容壳。

这种“核心 + 库 + 插件”的分层，使 RabbitMQ 能在同一个 broker 里同时提供队列、流、多协议和管理面，但也意味着排障时要先判断问题属于哪一层。

## 启动：application、boot step 与监督树

RabbitMQ 启动时不是简单地把所有模块 `start_link` 一遍，而是通过 OTP application 和 boot step 管理依赖顺序。

一个简化后的启动阶段：

```text
读取配置/环境变量
  -> 启动基础应用（rabbit_common、khepri/ra/osiris 等）
  -> rabbit_sup 建立监督树
  -> boot steps 按 requires/enables 顺序执行
  -> 元数据存储就绪、vhost 恢复
  -> 连接监听器开始 accept
  -> 插件（管理、协议接入等）启动
```

boot step 是 RabbitMQ 用来表达“谁依赖谁”的机制。一个插件或核心模块可以声明：

```erlang
-rabbit_boot_step({my_step,
    [{description, "my step"},
     {mfa, {my_module, start, []}},
     {requires, rabbit_registry},
     {enables, recovery}]}).
```

这比在 `rabbit:start/2` 里硬编码顺序更容易扩展，也让插件可以插入启动流程。代价是：启动失败时，日志里的错误可能来自某个 boot step，而不是主进程。

`rabbit_sup` 之下是连接、信道、队列、vhost、节点监控、告警等 supervisor。一个简化的监督树视图：

```text
rabbit_sup
├── rabbit_node_monitor        # 节点上下线、分区检测
├── rabbit_epmd_monitor        # epmd 监控
├── rabbit_guid                # 集群唯一标识
├── rabbit_alarm               # 内存/磁盘告警
├── rabbit_memory_monitor
├── rabbit_disk_monitor
├── rabbit_connection_sup      # 连接进程
├── rabbit_channel_sup         # 信道进程
├── rabbit_amqqueue_sup        # 队列进程
├── rabbit_vhost_sup           # vhost 及其子进程
└── ...
```

具体层级会随版本变化，但职责稳定：**连接、信道、队列都是被监督的进程**，不是全局哈希表里的记录。理解这一点，才能理解为什么大量队列/连接会消耗大量进程、为什么单个队列进程卡住会影响该队列的所有消费者。

启动排障的常用入口：

```bash
rabbitmq-diagnostics status
rabbitmq-diagnostics check_local_alarms
rabbitmqctl cluster_status
rabbitmqctl list_feature_flags
rabbitmq-diagnostics environment
```

如果节点启动失败，先看日志中的 boot step 名称和 feature flag 状态，再判断是配置、元数据、插件还是磁盘/权限问题。4.3 之后还要检查 Khepri 是否有多数派；少数派节点可能“进程活着”，但元数据写入不可用。

## 连接与信道：reader、writer、channel

一条 AMQP 0-9-1 连接在 broker 内部通常不是“一个线程处理所有事”，而是由多个 Erlang 进程协作：

```text
TCP socket
  └── rabbit_reader        # 读 socket、解析帧、握手、心跳
        ├── rabbit_writer  # 序列化输出，避免慢客户端阻塞读路径
        └── rabbit_channel # 每个 channel 一个进程
              ├── rabbit_limiter   # prefetch/credit 限流
              ├── rabbit_confirms  # publisher confirm 跟踪
              └── 队列/交换机调用
```

关键职责：

| 模块/进程 | 职责 | 工程含义 |
|---|---|---|
| `rabbit_reader` | 从 socket 读字节、解析帧、处理握手和心跳 | 读路径不能被慢业务阻塞 |
| `rabbit_writer` | 把方法/内容帧写回客户端 | 写操作串行化，慢客户端会积压 |
| `rabbit_channel` | 方法分发、拓扑操作、confirm、ack、事务状态 | 一个 channel 是一个有状态进程 |
| `rabbit_limiter` | 未确认消息额度 | prefetch 不是“建议”，是 broker 侧限流 |
| `rabbit_confirms` | confirm 序号与未确认集合 | confirm 只代表 broker 接受，不代表消费 |

为什么要把 reader 和 writer 分开？因为 socket 写可能被慢客户端阻塞。如果读线程同步写 socket，一个不读数据的客户端就能拖住整条连接，甚至影响同一 reader 上的其他 channel。writer 进程把“解析/业务处理”和“网络写出”解耦，代价是内存里可能积压待写帧，需要通过连接级流控和客户端读取来消化。

`rabbit_channel` 是理解 AMQP 语义的关键。它持有：

- 当前 channel 的 confirm 模式与下一个 publish 序号；
- 未确认的 publish 集合；
- 当前 channel 上注册的 consumer；
- 未 ack 的 delivery tag 与对应队列；
- 事务状态（`tx.select` 后）；
- prefetch 设置与 limiter 状态。

这也解释了为什么 channel 不是线程安全的：多个线程并发使用同一个 channel，会在客户端库和 broker 两侧同时制造状态竞争。正确做法是“一个 channel 一个使用上下文”，或用连接池/信道池。

连接级错误和信道级错误的区别，在实现上对应“关闭 reader/connection”还是“只关闭 channel 进程”。协议错误、帧格式错误通常会升级为连接错误；队列不存在、权限不足、参数不匹配通常只关闭 channel。客户端必须能区分这两种错误，否则会把一个 channel 的 404 当成整条连接不可用。

## 元数据：从 Mnesia 到 Khepri

RabbitMQ 的元数据包括 vhost、用户、权限、交换机、队列、绑定、policy、runtime parameter、topic permission 等。它们不是消息数据，但决定了消息能不能被路由、谁能发布/消费、队列用什么参数。

3.x 用 Mnesia 存储元数据；4.x 逐步迁移到 Khepri：

- 4.0：Khepri 可用，默认仍是 Mnesia；
- 4.1：通过 `khepri_db` feature flag 显式启用；
- 4.2：新集群默认 Khepri，老集群需显式迁移；
- 4.3：Khepri 成为唯一支持的元数据存储，Mnesia 移除。

代码层面，RabbitMQ 用 `rabbit_db_*` 一族模块把“元数据操作”与“底层存储”解耦，例如 vhost、user、permission、exchange、queue、binding、policy、runtime parameter 等各自有对应的 DB 模块。这样上层不需要到处判断“现在是 Mnesia 还是 Khepri”。

Khepri 基于 Raft，因此元数据写入需要多数派在线。这会带来一个非常现实的运维变化：

- 少数派节点可能仍能接受 TCP 连接、甚至还能读一些本地状态；
- 但声明交换机/队列、修改 policy、创建用户等写操作会失败或超时；
- “节点活着”不等于“集群能改拓扑”；
- 备份 definitions 仍然重要，但它不是消息数据的备份。

元数据的读路径通常有缓存/本地副本，写路径走共识。具体一致性取决于 API 和版本，不要把管理 API 的读结果当成线性一致。排查拓扑问题时，先确认多数派、Khepri 状态和 feature flag，再看单个节点的日志。

## 路由：exchange、binding 与 trie

AMQP 0-9-1 的路由在 broker 内部大致是：

```text
channel 收到 basic.publish(exchange, routing_key)
  -> 查 exchange
  -> 调用 exchange type 的 route/2（或等价接口）
  -> 得到目标队列/交换机集合
  -> 对每个目标执行 deliver/publish
```

`rabbit_exchange` 是交换机的通用进程/注册表，具体的匹配逻辑由 exchange type 实现：

- `direct`：精确匹配 routing key，通常用索引查绑定；
- `fanout`：忽略 routing key，命中所有绑定；
- `topic`：按 `.` 分段，`*` 匹配一段，`#` 匹配零或多段；实现上通常用 trie 做前缀/通配匹配；
- `headers`：按消息 header 与绑定参数匹配，支持 `all`/`any`；
- 插件式类型：一致性哈希、`x-modulus-hash` 等。

topic 路由的 trie 是“绑定数量会影响性能”的根源之一：匹配不是简单哈希，而是沿路径遍历并处理通配符回溯。绑定越多、`#` 越复杂，路由成本越高。direct 也不是“零成本”，只是索引更直接。

交换机到交换机（E2E）绑定会让路由递归：一个 exchange 的 route 结果里可能包含另一个 exchange，broker 继续递归路由，并做环路检测。E2E 能构建路由层级，但也会放大绑定数量、路由延迟和排障难度。

备用交换机（alternate exchange）是路由失败后的兜底：当某个 exchange 没有任何匹配目标时，消息被重新发布到 AE。它和 DLX 的区别是触发点不同：AE 处理“路由不到”，DLX 处理“被拒绝/过期/超限/超过投递次数”。

## 队列：rabbit_amqqueue、queue type 与 backing queue

队列在 RabbitMQ 里不是一张表，而是一个（或一组）进程加上底层存储。上层通过 `rabbit_amqqueue` 一族 API 操作队列；不同队列类型通过 `rabbit_queue_type` 分派到 classic/quorum/stream 实现。

一个简化视图：

```text
rabbit_amqqueue
  ├── classic queue process
  │     ├── backing queue（内存窗口 + 磁盘分页）
  │     ├── rabbit_queue_index（队列索引/段文件）
  │     └── rabbit_msg_store（共享消息存储）
  ├── quorum queue process
  │     └── ra group（leader/follower + Raft log + state machine）
  └── stream queue
        └── osiris log（段文件 + offset + retention）
```

### Classic Queue

classic queue 的核心是“队列进程 + backing queue + 消息存储/索引”：

- 队列进程维护 ready/unacked、消费者、TTL、优先级、死信等状态；
- 消息 body 可能存放在共享的 `rabbit_msg_store`，队列索引保存消息引用；
- 内存压力下，消息会被分页到磁盘；
- 4.x 只保留 CQv2；历史文档里的 alpha/beta/gamma/delta 是 CQv1 的概念，不应再拿来解释 4.x 的内存行为。

classic queue 的一个重要语义是：**它不是复制队列**。4.0 移除了经典镜像队列，classic queue 的数据只属于一个节点。节点故障时，队列的可用性取决于该节点是否恢复，而不是“其他副本自动接管”。

### Quorum Queue

Quorum Queue 的复制由 `ra` 完成。每个 Quorum Queue 是一个 Raft group：

- 一个 leader 接收写请求；
- follower 复制日志；
- 多数派确认后提交；
- 状态机应用日志，维护队列状态；
- leader 故障时重新选举；
- 日志段和快照用于恢复。

这带来更强的故障语义，但也带来写放大、磁盘 IO、网络往返和选举成本。Quorum Queue 的 publisher confirm 通常在消息被多数派提交后返回；这也是它比 classic 更慢、但更可靠的原因。

Quorum Queue 的 `delivery-limit`、死信、优先级、延迟重试等语义都在队列状态机里实现，而不是简单复用 classic 的参数。4.3 的 32 级严格优先级、4.4 的 `x-opt-delivery-time` 都属于这一类“状态机能力”。

### Stream

Stream 不是“保留消息的队列”，而是追加日志：

- `osiris` 负责日志段、写入、读取、retention；
- `rabbit_stream_coordinator` 管理 stream 的成员/副本/leader；
- 消费者按 offset 读取，可以重放；
- Stream 协议（常见 5552）提供 consumer group、credit、过滤等能力；
- 通过 AMQP 0-9-1 访问 stream 时，语义会受限。

Stream 的“确认”不是删除消息，而是提交 offset；retention 由大小/时间决定。把 Stream 当普通队列用，会遇到“消息不删除”“offset 不推进”“消费者组语义不同”等问题。

## 一条消息的旅程：publish

把上面的模块串起来，一次 publish 大致经过：

```text
client
  │ basic.publish(exchange, routing_key) + content header + body
  ▼
rabbit_reader
  │ 解析 METHOD/HEADER/BODY 帧，拼装消息
  ▼
rabbit_channel
  │ 校验 exchange、权限、消息大小、confirm 状态
  ▼
rabbit_exchange:route(...)
  │ 按 exchange type 匹配绑定，得到目标队列
  ▼
rabbit_amqqueue / queue process
  │ classic: 入 backing queue + 消息存储/索引
  │ quorum: 写 ra log，等待多数派提交
  │ stream: 追加 osiris log
  ▼
channel
  │ 如果开启 confirm，发送 basic.ack/basic.nack
  │ 如果 mandatory 且不可路由，先发 basic.return
  ▼
client
```

几个容易混淆的点：

1. **confirm 的时间点取决于队列类型和持久化**。classic 非持久消息可能很快确认；持久消息要等存储路径；Quorum 要等多数派提交；Stream 的确认语义又不同。
2. **confirm 不代表消费者收到**。它只表示 broker 按当前队列语义接受了消息。消费者是否处理、何时 ack，是另一条链路。
3. **mandatory return 和 confirm 是两件事**。不可路由时，broker 先 `basic.return`，再对这条 publish 发 confirm（如果开启）。客户端要同时处理 return 和 ack，不能只看 confirm。
4. **路由是“对每个目标队列”执行的**。一个 exchange 绑定到多个队列时，每个队列都要入队/复制；confirm 的语义要覆盖这些目标。

## 一条消息的旅程：consume

消费路径同样是一条进程链：

```text
client
  │ basic.consume(queue, prefetch)
  ▼
rabbit_channel
  │ 注册 consumer，设置 limiter
  ▼
queue process
  │ 维护消费者、ready 消息、未 ack 消息
  │ 按 prefetch/credit 选择消费者
  ▼
rabbit_writer
  │ 发送 basic.deliver + header + body
  ▼
client
  │ 处理业务，发送 basic.ack/nack/reject
  ▼
rabbit_channel -> queue process
  │ 移除未 ack 记录；nack/requeue 重新入队或死信
```

关键机制：

- **prefetch 是 broker 侧额度**。`basic.qos(prefetch_count=N)` 限制每个 consumer（或 channel，取决于 global 标志）最多 N 条未 ack 消息；额度不足时队列不再投递。
- **consumer 是队列进程里的状态**。消费者断开时，队列会把它未 ack 的消息重新入队（或按策略处理），所以消费者必须幂等。
- **ack 是 channel 到队列的操作**。`basic.ack` 的 delivery tag 属于 channel；multiple 表示批量确认到某个 tag。
- **`basic.get` 是同步拉取**，与 `basic.consume` 的推送模型不同；高吞吐场景通常用 consume。
- **consumer_timeout** 会在消费者长时间不 ack 时关闭消费者/channel，避免“在线但不动”拖住队列。

## 持久化：msg_store、queue_index 与段文件

classic queue 的持久化不是“把每条消息写一个文件”，而是分层：

- `rabbit_msg_store`：按 vhost 共享的消息存储，保存消息 body/属性，按引用计数回收；
- `rabbit_queue_index`：每个队列的索引，记录消息在队列中的位置、状态和存储引用；
- 段文件（segment）：消息存储和索引都按段组织，便于追加、删除和回收；
- 分页（paging）：内存压力下把消息从内存移到磁盘，消费时再读回。

这解释了为什么：

- 删除队列不等于立即释放磁盘，消息存储可能仍被其他队列引用；
- 磁盘占用和“队列深度 × 消息大小”不是线性关系，索引、段、碎片、副本都会放大；
- `delivery_mode=2` 只是标记持久化，真正落盘还取决于队列类型、存储路径和 confirm 时机；
- 持久化不等于备份，节点磁盘损坏仍可能丢失数据。

Quorum Queue 用 Ra 日志和状态机快照，Stream 用 Osiris 段文件。它们的磁盘布局、回收策略和恢复流程都不同，不能拿 classic 的“消息存储 + 索引”模型去套。

## 复制：Quorum Queue 的 Ra 内部

Quorum Queue 的可靠性来自 Raft，而不是“多存几份”。一个简化的写入路径：

```text
leader 收到 publish
  -> 追加到本地 ra log
  -> 复制到 follower
  -> 多数派持久化
  -> commit index 前进
  -> 状态机 apply
  -> 返回 confirm
```

关键概念：

- **term/leader**：每个 Raft group 有任期和 leader；leader 故障后重新选举；
- **log**：按索引追加的日志，包含消息和队列状态变更；
- **commit index**：多数派确认的位置；只有已提交的日志才能被应用；
- **state machine**：按日志顺序应用，维护队列的 ready/unacked/消费者等状态；
- **snapshot**：压缩日志、加速恢复；
- **membership**：增加/删除副本需要 Raft 配置变更。

Quorum Queue 的运维要点：

- 3 副本能容忍 1 个节点故障，5 副本能容忍 2 个；多数派丢失时队列不可写；
- leader 分布不均会导致热点，需要 `leader-locator`/rebalance；
- 磁盘延迟会直接放大 confirm 延迟；
- 网络分区时，少数派不能继续提交，避免脑裂；
- 4.3 的日志压缩减少“队列很短但磁盘很大”的问题，但不是万能药。

Mini 的 `cluster.py` 用投票和日志复制模拟了 Raft 风格行为，但没有真实网络 RPC、成员变更、快照安装和磁盘日志。它能帮你理解“多数派提交”的直觉，不能用来验证真实 Quorum Queue 的故障语义。

## Streams：Osiris 与 coordinator

Stream 的实现围绕追加日志：

- `osiris` 管理日志的写入、读取、段文件、retention 和副本；
- `rabbit_stream_coordinator` 管理 stream 的成员、leader、副本和配置；
- Stream 协议客户端按 offset 读取，broker 用 credit 控制推送；
- consumer group 把分区（Super Stream）分配给消费者；
- 单活消费者（SAC）保证同一时刻只有一个消费者处理某个分区；
- 过滤/去重在发布端或消费端实现，具体能力随版本变化。

Stream 的“删除”是 retention 到期或超过大小上限，不是 ack 后立即删除。消费者 offset 提交是消费进度，不是消息删除。把 Stream 当队列用，最容易犯的错误是：

- 以为 ack 会删除消息；
- 以为没有消费者时消息会丢；
- 以为 offset 会自动提交；
- 以为 AMQP 0-9-1 消费和 Stream 协议消费的语义完全一样。

## 管理面、指标与安全模块

管理面由 `rabbitmq_management` 插件提供：

- HTTP 处理器（`rabbit_mgmt_wm_*` 一族）暴露 overview、queues、connections、channels、exchanges、bindings、nodes 等 API；
- 统计收集器周期性采样，管理 API 读到的是聚合后的数据，不是实时线性一致状态；
- Prometheus 插件把指标暴露为文本格式；
- 管理 UI 是这些 API 的前端。

这解释了为什么管理 API 的队列深度可能比实际晚几秒，为什么高频轮询管理 API 会给 broker 增加负担，为什么“管理 API 挂了但消息还能发”。管理面是控制面，不是数据面。

安全模块的分层：

- `rabbit_auth_backend`：认证后端行为（internal、LDAP、OAuth 2.0 等）；
- `rabbit_auth_mechanism`：SASL 机制（PLAIN、EXTERNAL 等）；
- `rabbit_authz_backend` / `rabbit_access_control`：vhost 级 configure/write/read 权限；
- topic authorization：按 routing key 模式限制发布/订阅；
- 审计日志、TLS、证书轮换在连接/监听器层配置。

认证和授权是两条路径：认证回答“你是谁”，授权回答“你能对这个 vhost 的哪些资源做什么”。只配认证不配最小权限，等于把整个 vhost 交给所有通过认证的客户端。

## 插件、boot step 与 feature flag

RabbitMQ 的扩展点大多通过 behaviour 和 boot step 暴露：

- 交换机类型：实现 exchange type behaviour，注册到 `rabbit_registry`；
- 认证后端/机制：实现 auth behaviour；
- 管理面扩展：注册 HTTP handler、统计收集器；
- CLI 命令：注册 `rabbitmqctl` 子命令；
- 插件本身：打包为 `.ez`，通过 `rabbitmq-plugins` 启用。

feature flag 是跨版本的“能力闸门”：

- 升级前在旧版本启用所有可用 flag；
- flag 一旦启用通常不能回退；
- 新版本可能要求某些 flag 已启用才能启动/加入集群；
- 4.2 的 `khepri_db`、后续版本的元数据/队列能力都通过 flag 管理。

插件开发不是“改个配置”，而是进入 broker 的生命周期。写插件前先确认：能不能用现成的 exchange type、policy、Shovel/Federation、外部服务解决？插件的长期维护、升级兼容和安全责任，往往比首版开发更贵。

## 源码阅读路线

不建议从 `rabbit.erl` 第一行开始读。按“协议 -> 连接/信道 -> 路由/队列 -> 存储/复制 -> 管理/安全”的顺序，逐层建立地图。

### 阶段 1：协议编解码

- `rabbit_common` 中的 AMQP 0-9-1 编解码；
- 生成的 framing 模块（方法/字段表）；
- `rabbit_binary_parser` / `rabbit_binary_generator` 一类模块；
- 目标：能解释一个 `basic.publish` 的字节布局，能对照第 03 章和附录 A。

### 阶段 2：连接与信道

- `rabbit_reader`：握手、帧读取、心跳；
- `rabbit_writer`：输出串行化；
- `rabbit_channel`：方法分发、confirm、ack、事务；
- 目标：能画出“客户端 publish -> channel -> exchange”的调用链。

### 阶段 3：路由与队列

- `rabbit_exchange` 与各 exchange type；
- `rabbit_amqqueue`、`rabbit_queue_type`；
- classic/quorum/stream 的队列实现；
- 目标：能解释 direct/topic/headers 的匹配差异，能说清队列类型的分派。

### 阶段 4：存储与复制

- `rabbit_msg_store`、`rabbit_queue_index`；
- `ra` 的 log/state machine/snapshot；
- `osiris` 的段文件/offset/retention；
- 目标：能解释 confirm 的持久化时间点，能判断磁盘/复制瓶颈。

### 阶段 5：管理、安全与插件

- `rabbitmq_management` 的 HTTP handler/统计收集；
- auth backend/mechanism/access control；
- plugin/boot step/feature flag；
- 目标：能定位“管理 API 慢”“认证失败”“插件升级不兼容”。

阅读时配合工具：

```bash
rabbitmqctl eval 'erlang:memory().'
rabbitmq-diagnostics processes
rabbitmq-diagnostics runtime_thread_stats
rabbitmq-plugins enable rabbitmq_top
rabbitmq-plugins enable rabbitmq_tracing
```

`rabbitmq_tracing` 可以抓取进入/离开 broker 的消息（注意隐私和性能成本）；Wireshark 可以看线协议；`observer` 可以看进程/监督树，但需要图形环境。

## 常见误解

| 误解 | 更准确的说法 |
|---|---|
| 队列就是数据库里的一张表 | 队列是进程 + 状态 + 底层存储；空队列也占进程/内存 |
| channel 是一个线程 | channel 是连接内的有状态进程；连接还有 reader/writer |
| publisher confirm 表示消费者收到了 | confirm 只表示 broker 按队列语义接受了消息 |
| Quorum Queue 就是 classic 加副本 | 它用 Raft 日志/状态机，存储、确认、恢复语义都不同 |
| Khepri 只是换了个数据库 | 它基于 Raft，元数据写入需要多数派，运维语义变了 |
| Stream 就是保留消息的队列 | 它是追加日志，ack 不删除消息，消费靠 offset |
| 持久化就是每条消息 fsync | 取决于队列类型、存储路径和 confirm 时机 |
| 管理 API 是实时的 | 统计是采样的，读结果可能滞后 |
| 插件只是配置开关 | 插件进入 broker 生命周期，有升级/兼容/安全责任 |

## Mini 与真实实现的映射

| Mini 模块 | 真实 RabbitMQ 对应 | Mini 简化了什么 |
|---|---|---|
| `mini_rabbitmq/protocol.py` | `rabbit_common` 编解码、framing | 只覆盖 0-9-1 子集；无完整方法集 |
| `mini_rabbitmq/server.py` | `rabbit_reader` + `rabbit_channel` | 单进程/线程模型；无 writer 进程 |
| `mini_rabbitmq/broker.py` | channel/queue 编排、confirm 路径 | 无真实监督树、无 credit 流控 |
| `mini_rabbitmq/vhost.py` | vhost + `rabbit_db_*` + 路由 | 元数据在内存/快照；无共识 |
| `mini_rabbitmq/exchange.py` | exchange type behaviour | 无 trie 优化；路由规模小 |
| `mini_rabbitmq/queue.py` | queue process + backing queue | 内存队列 + 简单 TTL/优先级 |
| `mini_rabbitmq/storage.py` | msg_store + queue_index | 原子 JSON 快照，非段文件 |
| `mini_rabbitmq/cluster.py` | `ra` / Quorum Queue | 单进程模拟投票/复制，无网络 RPC |
| `mini_rabbitmq/streams.py` | `osiris` + stream coordinator | 语义模型，非真实日志/副本 |
| `mini_rabbitmq/management.py` | `rabbitmq_management` | 子集 API，无采样统计管线 |
| `mini_rabbitmq/auth.py` | auth backend + access control | PLAIN + 正则 ACL，无 LDAP/OAuth |
| `mini_rabbitmq/links.py` | federation/shovel 插件 | 单进程教学模型，无跨节点网络 |
| `mini_rabbitmq/policy.py` | policy/parameter | 合并规则子集 |

这张表的目的不是“给 Mini 挑错”，而是明确学习迁移：读懂 Mini 的语义后，知道真实实现把哪些部分换成了进程、日志、共识和插件。不要把 Mini 的线程/锁模型当成 RabbitMQ 的运行时模型，也不要把 Mini 的快照当成生产备份。

## 小结

真实 RabbitMQ 的内部可以概括为五层：协议编解码、连接/信道进程、交换机/队列路由、存储/复制、管理/安全/插件。连接有 reader/writer，信道是有状态进程，队列是进程加底层存储；classic 用消息存储/索引，Quorum 用 Ra，Stream 用 Osiris，元数据在 4.x 用 Khepri。4.3 之后元数据写入需要多数派，这让“节点活着”和“集群可写”成为两个不同的问题。

读源码的价值不是记住模块名，而是建立“一个语义由哪些进程、日志和共识步骤实现”的直觉。有了这张地图，再看第 20～25 章的 Mini，就能清楚哪些是教学简化，哪些是生产系统必须付出的工程成本。

---

[返回目录](../README.md) | [上一章：附录 P RabbitMQ 应用测试与混沌工程手册](P-application-testing-chaos.md) | [下一章：附录 R AMQP 1.0 协议深度手册](R-amqp-1-0-deep-dive.md)
