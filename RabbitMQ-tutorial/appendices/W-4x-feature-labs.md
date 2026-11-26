# 附录 W RabbitMQ 4.3/4.4 新特性实验手册

> 本附录定位：把[附录 I](I-version-migration.md)、第 06、09、11、12 章里“4.3/4.4 行为变了”的结论，变成可以在一次性集群上观察到的实验。所有实验都针对真实 RabbitMQ，Mini-RabbitMQ 不实现这些能力，不能用来验证本附录。实验结论只在记录的版本、配置和负载下成立；跨版本、跨云厂商前，用[附录 X](X-fact-check-and-sources.md)的核查流程重新确认。

## 为什么需要一份“新特性实验手册”

版本升级的风险很少来自“装不上”，更多来自**静默行为变化**：延迟消息突然立即投递、优先级不再按预期排序、nack 重投不再消耗 delivery-limit、元数据存储切换后少数派不能声明拓扑。只读文档很难形成肌肉记忆；只跑 happy path 又会漏掉这些边界。

本附录的原则：

1. **一次只验证一个行为**：固定版本、队列类型、消息大小、消费者数量和确认模式；
2. **同时记录“预期”和“实际”**：不要只记“成功了”；
3. **正反都测**：例如既测 `x-opt-delivery-time` 生效，也测字符串类型、classic 队列、`x-delay` 这些“静默失效”路径；
4. **不把单节点结论外推到集群**：复制、选举、Khepri 多数派都需要 3 节点；
5. **实验集群可丢弃**：不要在生产或共享预发上做迁移/降级实验。

## 实验环境

### 单节点：语义实验

```yaml
services:
  rabbitmq:
    image: rabbitmq:4.4-management
    container_name: rmq
    hostname: rmq
    ports:
      - "5672:5672"
      - "15672:15672"
    environment:
      RABBITMQ_DEFAULT_USER: app
      RABBITMQ_DEFAULT_PASS: change-me
    volumes:
      - rmq-data:/var/lib/rabbitmq

volumes:
  rmq-data:
```

```bash
docker compose up -d
docker exec rmq rabbitmq-diagnostics -q ping
docker exec rmq rabbitmqctl version
curl -u app:change-me http://localhost:15672/api/overview
```

单节点足以验证优先级、TTL、延迟、路由和队列参数；它不能验证 Quorum 的多数派、leader 故障转移、Khepri 的元数据多数派要求。

### 三节点：复制与元数据实验

```bash
docker network create rmq-net

for i in 1 2 3; do
  docker run -d --name "rmq$i" --hostname "rmq$i" --network rmq-net \
    -e RABBITMQ_DEFAULT_USER=app \
    -e RABBITMQ_DEFAULT_PASS=change-me \
    -e RABBITMQ_ERLANG_COOKIE=lab-cookie \
    rabbitmq:4.4-management
done

for i in 1 2 3; do
  until docker exec "rmq$i" rabbitmq-diagnostics -q ping; do
    sleep 2
  done
done

docker exec rmq2 rabbitmqctl stop_app
docker exec rmq2 rabbitmqctl join_cluster rabbit@rmq1
docker exec rmq2 rabbitmqctl start_app

docker exec rmq3 rabbitmqctl stop_app
docker exec rmq3 rabbitmqctl join_cluster rabbit@rmq1
docker exec rmq3 rabbitmqctl start_app

docker exec rmq1 rabbitmqctl cluster_status
```

只把 `rmq1` 的 5672/15672 映射到宿主机；其他节点通过 Docker 网络访问。生产环境还要考虑 Erlang cookie、节点名、DNS、时钟和防火墙，见第 11 章与[附录 N](N-erlang-runtime.md)。

### 版本指纹

每个实验开始前先记录：

```bash
docker exec rmq rabbitmqctl version
docker exec rmq rabbitmq-diagnostics status
docker exec rmq rabbitmqctl list_feature_flags
docker exec rmq rabbitmqctl list_queues name type arguments
```

把版本、Erlang/OTP、feature flags、插件列表、队列类型写进实验记录。没有版本指纹的“实验结果”无法复核。

## 实验 1：Quorum 优先级——两档还是 32 级

### 背景

| 版本 | Quorum 优先级模型 | 关键点 |
|---|---|---|
| 4.0–4.2 | normal/high 两档 | priority 0–4 为 normal，>4 为 high；高:普通约 2:1 调度，避免 normal 饥饿 |
| 4.3+ | 32 级严格优先级（0–31） | 总是启用；无优先级按 4 处理；没有反饥饿插队；`x-max-priority` 只属于 classic，Quorum 忽略它 |

注意：官方文档明确说 Quorum 的优先级“总是启用”，`x-max-priority` 不是 Quorum 的开关。看到第三方文章说“Quorum 必须设置 `x-max-priority`”时，先用本实验验证。

### 步骤

```python
import pika

params = pika.ConnectionParameters(
    host="localhost",
    credentials=pika.PlainCredentials("app", "change-me"),
)
connection = pika.BlockingConnection(params)
channel = connection.channel()
channel.queue_declare(
    queue="prio-lab",
    durable=True,
    arguments={"x-queue-type": "quorum"},
)

for priority in (0, 4, 9, 31):
    channel.basic_publish(
        exchange="",
        routing_key="prio-lab",
        body=f"priority={priority}".encode(),
        properties=pika.BasicProperties(priority=priority, delivery_mode=2),
    )

seen = []
while True:
    method, properties, body = channel.basic_get("prio-lab", auto_ack=False)
    if method is None:
        break
    seen.append(properties.priority)
    channel.basic_ack(method.delivery_tag)

print(seen)
connection.close()
```

在 4.3+ 上预期看到从高到低的严格顺序（例如 `[31, 9, 4, 0]`）。在 4.0–4.2 上，high 组获得约 2/3 的调度机会，normal 仍持续有进展；不要假设严格的全局高低顺序，也不要用 4 条消息的偶然顺序证明“严格优先级”。

### 对照实验

1. 用 4.2 镜像重复上述脚本，记录顺序差异；
2. 在 4.3+ 上声明时加 `x-max-priority=10`，确认行为不因该参数改变；再在 classic 队列上对比；
3. 持续发布 priority=31，同时低频发布 priority=0，观察低优先级是否长期得不到处理；
4. 把消息 reject/requeue 后再消费，观察优先级是否保留。

### 验收

- 能解释“4.2 的两档”和“4.3 的 32 级”在调度目标上的区别；
- 能说明为什么 4.3 的严格优先级会把饥饿风险交还给业务；
- 能给出一个不依赖单一队列优先级的公平性方案（拆队列、限流、独立低优先级消费者）。

## 实验 2：delivery-limit、acquired-count 与 DLX

### 背景

Quorum Queue 在 4.0 起默认 `delivery-limit=20`。4.3 起还区分两个计数：

- **delivery-count**：消息被判定为“投递失败”时增加，例如 `basic.reject`、AMQP `rejected`、`modified` 且 `delivery-failed=true`；
- **acquired-count**：消息被重新获取/退回时增加，例如 `basic.nack`、AMQP `released`。

因此“nack requeue 无限循环一定会触发 delivery-limit”在 4.3+ 不一定成立：nack/released 主要增加 acquired-count，不消耗 delivery-limit；只有 failed 路径才消耗。这个差异会直接影响毒消息治理。

### 步骤

先建 DLX/DLQ 和一个 `x-delivery-limit=3` 的 Quorum 队列：

```python
import pika

connection = pika.BlockingConnection(pika.ConnectionParameters(
    host="localhost",
    credentials=pika.PlainCredentials("app", "change-me"),
))
channel = connection.channel()
channel.exchange_declare("lab.dlx", exchange_type="fanout", durable=True)
channel.queue_declare("lab.dlq", durable=True, arguments={"x-queue-type": "quorum"})
channel.queue_bind("lab.dlq", "lab.dlx")
channel.queue_declare(
    "lab.limit",
    durable=True,
    arguments={
        "x-queue-type": "quorum",
        "x-delivery-limit": 3,
        "x-dead-letter-exchange": "lab.dlx",
    },
)
```

分别用 `basic_reject(requeue=True)` 和 `basic_nack(requeue=True)` 循环消费同一条消息：

```python
for attempt in range(6):
    method, properties, body = channel.basic_get("lab.limit", auto_ack=False)
    if method is None:
        print("no message; check DLQ")
        break
    print("attempt", attempt, "redelivered", method.redelivered)
    channel.basic_reject(method.delivery_tag, requeue=True)
```

把 `basic_reject` 换成 `basic_nack` 再跑一遍。最后从 DLQ 取消息，打印 `x-death`：

```python
method, properties, body = channel.basic_get("lab.dlq", auto_ack=True)
if method is not None:
    print(properties.headers.get("x-death"))
```

### 观察与解释

- `basic_reject` 路径通常在达到 limit 后进入 DLQ，`x-death.reason` 为 `delivery_limit`；
- `basic_nack` 路径在 4.3+ 可能一直不进入 DLQ，因为 acquired-count 不消耗 delivery-limit；
- 用管理 API 的 `messages`、`messages_unacknowledged`、队列参数和消息 header 交叉验证；
- 4.0–4.2 的行为以目标版本文档为准，不要用 4.3 的计数器语义回推。

### 验收

- 能解释“投递失败”和“退回”不是同一个计数；
- 能设计一个不会无限循环的毒消息策略：失败分类、最大次数、parking lot、告警；
- 能说明为什么“把 delivery-limit 调小”不是唯一的毒消息治理手段。

## 实验 3：4.3 原生延迟重试

### 背景

4.3 的 Quorum Queue 可以在队列内部把失败消息放置一段时间再重投，不需要 TTL 桶、不需要重新发布、不需要改写消息：

```text
x-delayed-retry-type: disabled | all | failed | returned
x-delayed-retry-min:  2000
x-delayed-retry-max:  8000
```

退避按线性计算：`min(min_delay × delivery_count, max_delay)`。`failed` 对应 delivery-count 增加（reject/failed），`returned` 对应 acquired-count 增加（nack/released），`all` 覆盖所有退回消息。精确语义以目标版本文档为准。

### 步骤

```python
channel.queue_declare(
    "lab.retry",
    durable=True,
    arguments={
        "x-queue-type": "quorum",
        "x-delayed-retry-type": "failed",
        "x-delayed-retry-min": 2000,
        "x-delayed-retry-max": 8000,
        "x-delivery-limit": 5,
        "x-dead-letter-exchange": "lab.dlx",
    },
)
channel.basic_publish(
    exchange="",
    routing_key="lab.retry",
    body=b"retry-me",
    properties=pika.BasicProperties(delivery_mode=2),
)
```

消费者记录每次投递的相对时间，并 `basic_reject(requeue=True)`：

```python
import time

start = time.time()

def on_message(ch, method, properties, body):
    print(round(time.time() - start, 1), "seconds", method.delivery_tag)
    ch.basic_reject(method.delivery_tag, requeue=True)

channel.basic_qos(prefetch_count=1)
channel.basic_consume("lab.retry", on_message_callback=on_message, auto_ack=False)
channel.start_consuming()
```

### 观察

- 首次失败后不会立即重投，约 2 秒后第二次投递，之后约 4、6、8 秒，封顶 8 秒；
- 达到 `x-delivery-limit` 后进入 DLQ，`x-death` 记录 `delivery_limit`；
- 把 type 改成 `returned` 并用 `basic_nack` 重复，观察计数与延迟的差异；
- 对比 TTL+DLX：后者需要重新发布、可能改写消息、需要为每个延迟建桶；原生延迟重试没有这些拓扑成本。

### 注意

- 原生延迟重试不是通用定时任务；预约/定时投递用 4.4 的 `x-opt-delivery-time`；
- 它不替代业务重试状态机：最大次数、毒消息隔离、parking lot、审计仍要在应用层设计；
- 延迟精度受队列调度、消费者数量和 broker 负载影响，不要在实验里用“毫秒级精确”做断言。

### 验收

- 能画出 2/4/6/8 秒的退避时间线；
- 能解释 `failed`、`returned`、`all` 的差异；
- 能说明原生延迟重试与 TTL+DLX、延迟插件、外部调度的取舍。

## 实验 4：4.4 `x-opt-delivery-time` 定时投递

### 背景

4.4 起 Quorum Queue 支持生产者指定绝对投递时间：

- AMQP 0-9-1：header `x-opt-delivery-time`，Unix epoch **毫秒**，类型必须是 long；
- AMQP 1.0：message annotation；
- 消息立即路由/入队，但在指定时间前不可消费；
- **classic queue 不支持**，会立即投递；AMQP 1.0 消费者还可以在 `modified` outcome 里用它覆盖下次投递时间。

### 步骤

```python
import time

channel.queue_declare(
    "sched-lab",
    durable=True,
    arguments={"x-queue-type": "quorum"},
)

due = int(time.time() * 1000) + 10_000
channel.basic_publish(
    exchange="",
    routing_key="sched-lab",
    body=b"scheduled",
    properties=pika.BasicProperties(
        delivery_mode=2,
        headers={"x-opt-delivery-time": due},
    ),
)

time.sleep(0.2)  # 给 publish 一点落队时间，仍远早于 due

method, properties, body = channel.basic_get("sched-lab", auto_ack=False)
print("before due:", method is None)

time.sleep(11)
method, properties, body = channel.basic_get("sched-lab", auto_ack=False)
print("after due:", method is not None)
```

### 静默失效对照

1. 把 header 值改成字符串 `str(due)`：4.4 Quorum 会立即投递，不报错；
2. 发送 `x-delay: 10000`：4.4 Quorum 不识别，立即投递，不报错；
3. 把消息发到 classic 队列：即使 header 是 long，也立即投递；
4. 在 4.3 集群上发同样的 header：不识别，立即投递。

这四种路径是升级/迁移中最危险的“看起来成功”。客户端库可能封装了 `setDelayLong`/`x-delay`，必须确认它写到的是 `x-opt-delivery-time` 且类型正确。

### 验收

- 能区分“生产者定时投递”（4.4 `x-opt-delivery-time`）和“失败后延迟重试”（4.3 `x-delayed-retry-*`）；
- 能解释为什么 `x-delay` 在 4.4 Quorum 上静默失效；
- 能写出迁移检查项：header 名、类型、队列类型、broker 版本。

## 实验 5：`x-modulus-hash` 与一致性哈希

### 背景

4.3 起内核自带 `x-modulus-hash` 交换机，按 routing key 哈希后对绑定目标数取模。绑定关系不变时，同一 key 稳定到同一队列；增删绑定会改变 N，几乎重排所有 key。它与一致性哈希插件（`x-consistent-hash`）的取舍不同。

### 步骤

```python
channel.exchange_declare("mod-lab", exchange_type="x-modulus-hash", durable=True)

for name in ("mod-0", "mod-1", "mod-2"):
    channel.queue_declare(name, durable=True, arguments={"x-queue-type": "quorum"})
    channel.queue_bind(name, "mod-lab", routing_key="")

for index in range(300):
    channel.basic_publish(
        exchange="mod-lab",
        routing_key=f"order-{index}",
        body=b"x",
        properties=pika.BasicProperties(delivery_mode=2),
    )
```

用管理 API 或 `rabbitmqctl list_queues name messages` 查看分布；重复发布同一个 `order-42`，确认它总是进入同一个队列。再加一个 `mod-3` 绑定，重新发布同一批 key，观察迁移比例。

### 注意

- 绑定键不参与取模；权重靠“同一队列绑定多次”；
- 增删队列会重排几乎所有 key，不适合频繁扩缩容；
- 一致性哈希插件需要安装，且增删绑定也会迁移部分 key；
- 两者都不提供“免费的顺序”：消费者并发、重投、prefetch 仍会破坏顺序，要配合单活消费者/分区内串行。

### 验收

- 能根据“拓扑是否频繁变化”选择 modulus hash 或 consistent hash；
- 能解释为什么分区键必须是高基数、分布均匀的业务键；
- 能说明顺序保证的边界。

## 实验 6：Khepri 迁移与元数据多数派

### 背景

- 4.1：`khepri_db` feature flag 可显式启用；
- 4.2：新集群默认 Khepri，旧集群需显式启用并迁移；
- 4.3+：Khepri 是唯一元数据存储，Mnesia 被移除，迁移不可逆；
- 4.3+ 元数据写入要求集群多数派在线，少数派节点仍能连接，但无法声明新拓扑。

### 步骤

在一次性 4.2 三节点集群上：

```bash
docker exec rmq1 rabbitmqctl list_feature_flags
docker exec rmq1 rabbitmqctl enable_feature_flag khepri_db
docker exec rmq1 rabbitmqctl status
```

等待元数据迁移完成，滚动重启节点，检查集群健康。然后在 4.3/4.4 集群上：

1. 停掉两个节点，只留一个；
2. 尝试 `queue_declare` 一个新队列；
3. 观察声明失败/超时，但已有队列仍可连接/消费（以目标版本行为为准）；
4. 恢复多数派后重试声明。

### 注意

- 不要在共享预发或生产上做迁移实验；
- 迁移前备份 definitions 和节点数据；
- 迁移不可逆，回滚边界要提前写清楚；
- `cluster_partition_handling` 等 Mnesia 时代配置在 4.3+ 会被接受但不生效，应从配置中清理。

### 验收

- 能解释为什么“少数派还能连”不等于“还能改拓扑”；
- 能写出 Khepri 迁移的前置条件、验证点和回滚边界；
- 能说明元数据备份与消息备份不是一回事。

## 实验 7：4.4 CQv2 `.sqs` 单向门

### 背景

4.4 的 classic queue 共享存储 v2 使用 `.sqs` 文件。节点一旦写入 v2 文件，就不能降级到 4.3.x 或更早版本。应用回滚容易，数据格式回滚很难。

### 步骤

在一次性 4.4 集群上：

1. 声明 durable classic queue；
2. 发布 persistent 消息并确认；
3. 停止节点，检查数据目录：

```bash
docker exec rmq find /var/lib/rabbitmq -name '*.sqs' -ls
```

4. 复制数据目录到临时目录，用 4.3 镜像尝试启动（只做一次，确认失败/不支持）；
5. 记录：哪些文件是 `.sqs`、启动日志报什么、团队的回滚方案是什么。

### 注意

- 不要在承载数据的环境做降级实验；
- 数据目录随版本/发行版变化，以实际 `RABBITMQ_MNESIA_BASE` 为准；
- “PVC 快照”不等于“可降级备份”，恢复演练要覆盖版本边界。

### 验收

- 能写出 4.4 的回滚边界：应用、配置、数据格式分别能回滚到什么程度；
- 能说明为什么关键业务更适合蓝绿迁移而不是原地降级。

## 实验 8：Stream 过滤与初始 offset（选做）

### 背景

Stream 的过滤值（`x-stream-filter-value`）让消费者只读关心的事件；4.2 起还支持 SQL 过滤表达式（以目标版本文档为准）。4.4 的 `x-stream-initial-offset` 是**创建 Stream 时**的队列参数，用于设置第一条消息的 offset，不是消费者的起始位置；消费者起点仍用 `x-stream-offset`（`first`/`last`/`next`/数值）选择。两者不要混淆。

### 步骤

使用 Stream 协议客户端（Java/Go/.NET/Python 的官方或社区客户端）：

```text
producer:
  publish(body, filter_value="tenant=eu")

consumer A:
  subscribe(stream, filter="tenant=eu")

consumer B:
  subscribe(stream, filter="tenant=us")
```

然后：

1. 确认 A 只收到 `tenant=eu`，B 只收到 `tenant=us`；
2. 用 `x-stream-offset=first` 和 `last` 分别创建消费者，观察读取范围；
3. 创建新 Stream 时设置 `x-stream-initial-offset`，观察第一条消息 offset 与 `first` 的关系；
4. 讨论过滤值基数、索引内存和“过滤不是安全边界”。

### 注意

- 过滤不是权限控制；能连到 Stream 的消费者仍可能读到其他值；
- `x-stream-initial-offset` 在创建时固定，非零值可能需要 4.4 feature flag；
- AMQP 0-9-1 访问 Stream 的能力受客户端/版本限制，consumer group、offset 提交、过滤通常需要 Stream 协议。

### 验收

- 能区分“Stream 初始 offset”和“消费者起始 offset”；
- 能解释过滤的收益与边界；
- 能说明为什么 Stream 不能替代任务队列。

## 实验 9：AMQP 1.0 互操作（选做）

AMQP 1.0 在 4.0 起是核心协议。选做目标不是背帧格式，而是验证映射：

1. 用官方 AMQP 1.0 客户端连接 4.4，向 exchange 发送、从 queue 接收；
2. 观察 source/target、link credit、settlement 与 0-9-1 的 exchange/queue/ack 的差异；
3. 4.2+ 验证 Direct Reply-To 的请求/响应；
4. 4.4 验证连接唯一性（container ID）在集群范围的行为（以客户端支持为准）；
5. 记录：属性映射、确认语义、错误处理、重连行为。

线级细节见[附录 R](R-amqp-1-0-deep-dive.md)；不要假设 0-9-1 的 confirm/prefetch 与 1.0 的 settlement/credit 一一对应。

## 实验记录模板

```yaml
experiment: 4.3-quorum-priority
rabbitmq_version: 4.3.x
erlang_version: "27"
topology: single-node
queue_type: quorum
client: pika 1.3.x
config:
  x-queue-type: quorum
  x-max-priority: null
workload:
  messages: 4
  priorities: [0, 4, 9, 31]
expected: "4.3+ 严格从高到低"
observed: "[31, 9, 4, 0]"
negative_cases:
  - "4.2 镜像：high 组先于 normal 组，组内顺序不保证"
conclusion: "4.3 32 级严格优先级；x-max-priority 不是 Quorum 开关"
source: "官方 4.3 文档 + 本实验"
date: "2026-11-26"
owner: "platform-team"
```

记录要包含**反例**和**未验证项**。只写“符合预期”的记录，无法在升级复盘时复用。

## 不要把实验结论外推

- 单节点实验不能证明 Quorum 故障转移、Khepri 多数派、网络分区行为；
- 没有 TLS/插件/真实消息大小的实验，不能代表生产性能；
- 客户端库的封装可能改变 header 名/类型（例如 `setDelayLong` 写 `x-delay`），要以实际抓包/管理 API 为准；
- 云托管版本可能滞后，先确认厂商支持的版本与参数；
- 实验日期和版本必须记录，否则半年后无法判断结论是否仍成立。

## 小结

本附录把 4.3/4.4 的关键行为拆成可观察实验：Quorum 两档与 32 级优先级、delivery-limit 的 acquired/delivery 计数、4.3 原生延迟重试、4.4 `x-opt-delivery-time`、`x-modulus-hash`、Khepri 多数派、CQv2 `.sqs` 单向门、Stream 过滤与初始 offset、AMQP 1.0 互操作。每个实验都要求记录版本指纹、正反路径和未验证项。实验的目的不是“证明文档对”，而是建立“版本行为 → 可观察证据 → 升级/回滚决策”的闭环；事实核查方法见[附录 X](X-fact-check-and-sources.md)，版本迁移清单见[附录 I](I-version-migration.md)。

---

[返回目录](../README.md) | [上一章：附录 V 生产参考架构案例集](V-reference-architectures.md) | [下一章：附录 X 事实核查、版本验证与官方资料地图](X-fact-check-and-sources.md)
