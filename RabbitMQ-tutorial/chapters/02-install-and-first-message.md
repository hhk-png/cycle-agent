# 第 02 章 安装、拓扑与第一条消息

> 本章定位：把概念变成可操作环境。先区分“真实 RabbitMQ 安装”和“教程内置 Mini-RabbitMQ”，再用两种方式跑通发布/消费闭环。

## 两种运行方式

本教程有两种运行方式，用途不同：

1. **真实 RabbitMQ**：生产语义、管理 UI、插件、集群和客户端生态的基准。需要安装 Erlang/RabbitMQ 或 Docker。
2. **Mini-RabbitMQ**：随教程分发的 Python 教学实现，用来观察协议和内部状态。无需额外依赖，但明确不是生产 broker。

学习建议：概念和命令以真实 RabbitMQ 为准；协议细节与内部状态用 Mini 对照。两者不要混为一谈，尤其不要把 Mini 的快照机制当成 RabbitMQ 的持久化实现。

## 安装真实 RabbitMQ

### 方式一：Docker

```yaml
services:
  rabbitmq:
    image: rabbitmq:4.4-management
    container_name: rabbitmq-lab
    hostname: rabbitmq-lab
    ports:
      - "5672:5672"     # AMQP
      - "15672:15672"   # Management UI/HTTP API
      - "15692:15692"   # Prometheus plugin (if enabled)
    environment:
      RABBITMQ_DEFAULT_USER: app
      RABBITMQ_DEFAULT_PASS: change-me
      RABBITMQ_DEFAULT_VHOST: /app
    volumes:
      - rabbitmq-data:/var/lib/rabbitmq
    healthcheck:
      test: ["CMD", "rabbitmq-diagnostics", "-q", "ping"]
      interval: 10s
      timeout: 5s
      retries: 10

volumes:
  rabbitmq-data:
```

启动后访问 `http://localhost:15672`。如果坚持使用默认的 `guest/guest`，请只从本机访问；RabbitMQ 默认限制 guest 只能从 localhost 登录。

management 镜像已经包含管理插件。若使用 `rabbitmq:4.4` 基础镜像，需要进入容器执行：

```bash
rabbitmq-plugins enable rabbitmq_management
```

### 方式二：Linux 包管理器

Debian/Ubuntu 系列通常可以这样安装（具体仓库配置以官方文档为准）：

```bash
sudo apt-get update
sudo apt-get install rabbitmq-server
sudo systemctl enable --now rabbitmq-server
sudo rabbitmq-plugins enable rabbitmq_management
```

注意：发行版仓库往往落后于上游，可能装到 3.x。要以 4.4 为基线，请按官方文档添加 RabbitMQ 仓库，或直接用前面的 Docker 镜像；装完用 `rabbitmqctl version` 核对。

检查状态：

```bash
sudo systemctl status rabbitmq-server
sudo rabbitmq-diagnostics -q ping
sudo rabbitmqctl status
sudo rabbitmqctl list_queues name messages consumers
```

### 创建一个最小用户

生产环境不要用 guest 跑业务。上面的 Docker Compose 已经通过环境变量创建了 `app/change-me` 和 vhost `/app`；如果使用包安装，或用的是别的密码，再执行下面的命令。Docker 下把 `sudo rabbitmqctl` 换成 `docker exec rabbitmq-lab rabbitmqctl`：

```bash
# 包安装（Docker 下省略 add_user，密码用 change-me 或自行同步修改）
sudo rabbitmqctl add_user app 'S3cure-Passw0rd!'
sudo rabbitmqctl add_vhost /app
sudo rabbitmqctl set_permissions -p /app app "^order" "^order" "^order"
sudo rabbitmqctl set_user_tags app management
```

三个正则依次对应 configure、write、read。上面只允许以 `order` 开头的资源（覆盖 `orders` 交换机与 `order.created` 队列），虽然仍偏宽，但能避免“所有队列都能删”的常见事故。生产可按交换机/队列前缀拆成更细的正则。

> 注意：默认交换机在权限检查中使用名字 `amq.default`，不是空字符串。只写 `^order` 会挡住默认交换机发布；需要默认交换机时，把 write 正则放宽到同时匹配 `amq.default`，例如 `^(order|amq\.default)`。

## 用 pika 跑通真实 RabbitMQ

以下代码需要 `pip install pika`，连接前面的 Docker 实例。先声明持久化交换机、队列和绑定，再发布消息并消费。

```python
import os

import pika

params = pika.ConnectionParameters(
    host="localhost",
    port=5672,
    virtual_host="/app",
    credentials=pika.PlainCredentials("app", os.environ.get("RABBITMQ_PASSWORD", "change-me")),
    heartbeat=30,
    blocked_connection_timeout=30,
)
connection = pika.BlockingConnection(params)
channel = connection.channel()

channel.exchange_declare(
    exchange="orders",
    exchange_type="topic",
    durable=True,
)
channel.exchange_declare(
    exchange="orders.dlx",
    exchange_type="fanout",
    durable=True,
)
channel.queue_declare(
    queue="order.created",
    durable=True,
    arguments={
        "x-dead-letter-exchange": "orders.dlx",
        "x-message-ttl": 86_400_000,
    },
)
channel.queue_bind(
    queue="order.created",
    exchange="orders",
    routing_key="order.created",
)

channel.confirm_delivery()
channel.basic_publish(
    exchange="orders",
    routing_key="order.created",
    body=b'{"order_id": 42}',
    properties=pika.BasicProperties(
        delivery_mode=2,
        content_type="application/json",
        message_id="42",
    ),
    mandatory=True,
)
print("published")
connection.close()
```

消费者需要手动确认，并设置预取，避免一个消费者把队列全部吸走却处理不过来：

```python
import os

import pika

connection = pika.BlockingConnection(
    pika.ConnectionParameters(
        host="localhost",
        virtual_host="/app",
        credentials=pika.PlainCredentials("app", os.environ.get("RABBITMQ_PASSWORD", "change-me")),
    )
)
channel = connection.channel()
channel.basic_qos(prefetch_count=10)

def on_message(ch, method, properties, body):
    try:
        print("received", body)
        # 业务处理必须幂等；这里用 message_id 做去重是示例。
        ch.basic_ack(delivery_tag=method.delivery_tag)
    except Exception:
        # requeue=False 让消息进入 DLX；不要无脑 requeue 造成毒消息循环。
        ch.basic_nack(delivery_tag=method.delivery_tag, requeue=False)

channel.basic_consume(
    queue="order.created",
    on_message_callback=on_message,
    auto_ack=False,
)
channel.start_consuming()
```

这里有两个容易忽略的点：

- `queue_declare` 与 `exchange_declare` 必须是幂等的；生产代码应把它们放在启动时声明，而不是每次请求。
- `mandatory=True` 只保证“不可路由时退回”，不等于“写入磁盘成功”。发布可靠性还需要 publisher confirm。

## 用 Mini-RabbitMQ 跑通第一条消息

在教程根目录执行：

```bash
python examples/01_hello_world.py
python examples/02_work_queue.py
python examples/09_publisher_confirms.py
```

第一个示例的核心代码如下：

```python
from mini_rabbitmq import Broker

broker = Broker()
broker.declare_queue("hello")
broker.publish("", "hello", b"Hello, RabbitMQ!")
delivery = broker.get("hello")
print(delivery.message.body)
broker.ack(delivery.delivery_tag)
broker.close()
```

真实 AMQP 路径可以直接运行教程自带客户端示例：

```bash
python examples/12_wire_amqp.py
```

`examples/12_wire_amqp.py` 会在进程内启动一个临时服务端（`AMQPServer(broker, port=0)`）、完成 AMQP 握手、声明拓扑、消费并 ack，最后清理。它不依赖外部 RabbitMQ，也不会占用 5672/5673 端口；不需要事先另起 `mini_rabbitmq serve`。

## Docker Compose 之外的部署选择

- **单机 systemd**：适合开发和低风险内部系统；队列与元数据要备份。
- **多节点集群**：至少 3 个节点，Quorum Queue 才能容忍 1 个节点故障；注意网络延迟和 Erlang cookie。
- **Kubernetes**：使用 RabbitMQ Cluster Operator 管理 StatefulSet、服务发现、TLS 和升级；不要自己拼裸 Deployment。生产化的 CRD、存储、调度、Topology Operator 与 GitOps 流程见[附录 T](../appendices/T-kubernetes-operator-production.md)。
- **托管服务**：Amazon MQ、CloudAMQP 等把控制面交给厂商，但限制插件、参数和网络拓扑。

安装不是“官方镜像一跑就完”。生产前至少要确认：vhost 和权限、持久化卷、内存/磁盘告警阈值、管理面是否暴露公网、TLS、备份、升级窗口以及客户端重连策略。

## 版本基线：4.x、Erlang 与镜像标签

本教程以 RabbitMQ 4.x 的概念模型为主。选版本时至少确认：

| 项 | 建议 | 原因 |
|---|---|---|
| RabbitMQ | 4.4.x 的最新补丁（存量系统按官方升级路径矩阵升级） | 4.0 起镜像队列/CQv1 移除；4.3 起 Khepri only；4.4 有 `.sqs` 单向升级门 |
| Erlang/OTP | 与目标 RabbitMQ 版本匹配 | 版本不匹配可能无法启动或行为异常 |
| 镜像标签 | 固定到具体版本，不用 `latest` | 可复现、可回滚 |
| 管理插件 | `rabbitmq:4.4-management` 或显式 enable | 管理 UI/API 不是默认基础镜像能力 |
| 部署方式 | 自建用 systemd/K8s Operator，托管看限制 | 有状态服务的运维不能靠裸容器 |

生产升级到 4.x 的破坏性变更（镜像队列、lazy 模式、global QoS、16 MiB 默认消息上限、Quorum 优先级与 delivery-limit、Khepri/Mnesia 移除、4.3/4.4 的延迟能力）集中在[附录 I](../appendices/I-version-migration.md)。不要在未读升级清单的情况下直接跨大版本升级；4.3 只支持从 4.2.x 升级，4.4 可从 4.2.x/4.3.x 升级（从 3.13.x/4.0.x 要先到 4.2.x），4.3.7+ 是 4.3↔4.4 元数据互操作条件。

## 拓扑即代码：definitions 与启动声明

生产拓扑不应该靠“某个人在管理 UI 上点出来”。两种常见做法：

1. **definitions 导出/导入**：把交换机、队列、绑定、策略、用户、权限导出成 JSON，纳入版本控制；
2. **应用启动声明**：客户端在启动时幂等声明自己需要的拓扑（适合小团队/简单拓扑）。

```bash
rabbitmqctl export_definitions /backup/definitions.json
rabbitmqctl import_definitions /backup/definitions.json
```

definitions 只包含元数据，不包含队列里的消息。导入前要确认：

- 目标版本兼容（4.x 会拒绝/忽略旧参数）；
- 是否覆盖现有对象；
- 密码哈希/凭据如何处理；
- 导入后策略、权限、插件参数是否符合预期。

更严格的团队会把拓扑写成声明式配置，由平台服务在部署时 apply，并在 CI 中做 drift 检测。关键是：**拓扑变更要像代码变更一样评审、测试、可回滚**。

## 一个带重连与 confirm 的 pika 骨架

前面的示例是最小路径。生产代码至少要处理重连、拓扑恢复、confirm、return 和优雅关闭。下面是一个可改写的骨架：

```python
import os
import random
import time

import pika

def connect(params):
    while True:
        try:
            return pika.BlockingConnection(params)
        except pika.exceptions.AMQPConnectionError:
            time.sleep(min(30, 1 + random.uniform(0, 5)))

def declare(channel):
    channel.exchange_declare("orders", exchange_type="topic", durable=True)
    channel.exchange_declare("orders.dlx", exchange_type="fanout", durable=True)
    channel.queue_declare(
        "order.created",
        durable=True,
        arguments={"x-dead-letter-exchange": "orders.dlx"},
    )
    channel.queue_bind("order.created", "orders", "order.created")

params = pika.ConnectionParameters(
    host="localhost",
    virtual_host="/app",
    credentials=pika.PlainCredentials("app", os.environ.get("RABBITMQ_PASSWORD", "change-me")),
    heartbeat=30,
    blocked_connection_timeout=30,
)

while True:
    try:
        connection = connect(params)
        channel = connection.channel()
        declare(channel)
        channel.confirm_delivery()
        channel.basic_qos(prefetch_count=20)
        channel.basic_publish(
            exchange="orders",
            routing_key="order.created",
            body=b'{"order_id": 42}',
            mandatory=True,
            properties=pika.BasicProperties(delivery_mode=2, message_id="42"),
        )
        # 批量场景：继续发布后统一 wait_for_confirms，而不是每条都等
        connection.close()
        break
    except (pika.exceptions.AMQPError, OSError):
        time.sleep(2)
```

这段代码的重点是**退避 + 抖动**，避免所有实例同时重连；生产版本还应加入最大重试次数、结构化日志和指标。重连后必须重新声明拓扑、恢复消费者、重新设置 QoS；自动重连库也不会替你恢复业务状态。

## 本地开发：Testcontainers 与 devcontainer

如果团队不想在每台开发机装 Erlang/RabbitMQ，可以用容器化测试：

- **Testcontainers**：测试启动临时 RabbitMQ 容器，跑完销毁；适合集成测试；
- **devcontainer / docker compose**：开发环境一条命令起 broker + 管理 UI；
- **CI**：用 service container 或 Testcontainers，固定镜像版本；
- **隔离**：每个测试用独立 vhost 或随机队列名，避免互相污染。

容器化测试的坑：

- 端口冲突（5672/15672 常被本机 broker 占用）；
- 容器启动慢，测试要等待健康检查而不是固定 sleep；
- 数据卷残留导致“上次的队列还在”；
- 镜像版本漂移导致 CI 与生产行为不一致。

Mini-RabbitMQ 的价值就在这里：它零依赖、启动快，适合单元/协议测试；真实 RabbitMQ 容器适合互操作和运维语义测试。两者不能互相替代。

## 第一个拓扑的常见错误

| 现象 | 常见原因 | 排查 |
|---|---|---|
| 发布成功但队列无消息 | 路由键不匹配、绑定方向错、交换机类型选错 | 管理 UI 的 bindings、`mandatory` 返回、打开 trace |
| 队列重启后消息没了 | 队列非 durable、消息非 persistent、节点非 quorum | 检查 durable、delivery_mode、队列类型 |
| 消费者收不到消息 | 消费者在另一个 vhost、队列被排他占用、prefetch 问题 | 对照 vhost/connection/channel 管理页 |
| `ACCESS_REFUSED` | 用户无 vhost 权限或 configure 正则不匹配 | `rabbitmqctl list_permissions -p /app` |
| 管理 UI 登录不了 | guest 远程限制、管理插件未启、端口未映射 | 本机登录、`rabbitmq-plugins list`、端口检查 |
| 发布变慢甚至阻塞 | 内存/磁盘告警、publisher confirm 等待、队列过长 | 看 alarms、blocked 连接、队列深度 |
| 升级后声明 406 | 4.x 弃用参数/默认值变化、transient 非排他队列 | 对照附录 I，移除 `ha-mode`/`x-queue-mode`/transient |
| 大消息发布失败 | 4.0 默认 `max_message_size` 为 16 MiB | 提高上限或改对象存储 + 引用 |

## 一个最小但正确的启动顺序

生产应用启动时，建议按以下顺序：

1. 连接 broker，设置心跳与 blocked 超时；
2. 打开 channel，声明 durable 拓扑（若由平台统一管理，则只做 passive 检查）；
3. 设置 QoS；
4. 启动消费者；
5. 启动生产者或接受流量；
6. 注册优雅关闭：停止接收新任务、等待在途消息、关闭 channel/connection。

不要在每次请求里创建连接或 channel。连接是昂贵的 TCP + 认证 + 心跳资源，channel 也有内存和状态开销。常见模式是“每进程一个连接，每线程/协程一个 channel”，或使用客户端库的连接池/自动恢复机制。

## 本章小结

真实 RabbitMQ 适合用 Docker、系统包或 Kubernetes Operator 安装。业务应用应使用独立用户、最小权限 vhost、durable 拓扑、手动确认、合理预取和 publisher confirm。Mini-RabbitMQ 提供零依赖的对照环境，让你能观察协议和内部状态，但它不承担生产责任。

下一章进入 AMQP 0-9-1 的字节层：连接如何握手、方法如何编号、消息如何分成 method/header/body 帧，以及 Mini 如何编解码这些帧。

---

[上一章：第 01 章 消息中间件与 RabbitMQ 全景](01-rabbitmq-landscape.md) | [返回目录](../README.md) | [下一章：第 03 章 AMQP 0-9-1 协议深潜](03-amqp-0-9-1-protocol.md)
