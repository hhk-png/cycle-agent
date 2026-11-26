# 附录 J 多语言客户端工程手册

本附录把“客户端库怎么用”提升到“生产代码怎么写”。同一套 AMQP 0-9-1 语义，在不同语言/框架里的默认行为、线程模型、重连能力和确认 API 差异很大。下面的代码是可改写的骨架，不是可直接上线的完整实现；重点是连接生命周期、确认、预取、重连和优雅关闭这几条共同主线。

## 选库原则

| 语言 | 推荐库 | 并发模型 | 生产注意 |
|---|---|---|---|
| Java | `com.rabbitmq:amqp-client` | 连接/信道，回调线程 | 手动 confirm/consumer，需自己管理恢复 |
| Java/Spring | Spring AMQP | 监听容器 + 线程池 | `AcknowledgeMode`、重试拦截器、事务边界 |
| .NET | `RabbitMQ.Client` | 异步 `IChannel` | 连接/信道生命周期、`DispatchConsumersAsync` |
| Go | `github.com/rabbitmq/amqp091-go` | goroutine + channel | 连接复用、confirm 模式、重连监督 |
| Node.js | `amqplib` | 回调/Promise | ConfirmChannel、背压、事件循环阻塞 |
| Python | `pika` / `aio-pika` | 阻塞 / asyncio | BlockingConnection 非线程安全；Robust 连接 |
| Rust | `lapin` | async | 连接/信道恢复、确认 futures |

选库时优先确认：

1. 是否支持 publisher confirms（同步/异步）；
2. 是否支持 per-consumer prefetch；
3. 断线后能否恢复 channel/consumer/topology；
4. 是否暴露 `blocked`/`unblocked` 事件；
5. 是否有可观测性钩子（指标、日志、trace context）；
6. 维护活跃度与目标 RabbitMQ 版本兼容性。

## Java：amqp-client 的发布与消费骨架

发布端要同时处理 confirm 和 return。`basicPublish` 返回不代表 broker 接受，必须等 `waitForConfirmsOrDie` 或异步 confirm 回调。

```java
ConnectionFactory factory = new ConnectionFactory();
factory.setHost("broker.internal");
factory.setVirtualHost("/app");
factory.setUsername("order-svc");
factory.setPassword(System.getenv("RABBITMQ_PASSWORD"));
factory.setAutomaticRecoveryEnabled(true);
factory.setNetworkRecoveryInterval(5000);
factory.setTopologyRecoveryEnabled(true);

try (Connection connection = factory.newConnection();
     Channel channel = connection.createChannel()) {
    channel.confirmSelect();
    channel.addReturnListener(returned -> {
        // 不可路由：记录 message-id、exchange、routing key，进入审计/补偿
        log.warn("returned {} {} {}", returned.getReplyCode(),
                 returned.getExchange(), returned.getRoutingKey());
    });
    channel.exchangeDeclare("orders", BuiltinExchangeType.TOPIC, true);
    channel.queueDeclare("inventory.order-created", true, false, false,
                         Map.of("x-dead-letter-exchange", "orders.dlx"));

    AMQP.BasicProperties props = new AMQP.BasicProperties.Builder()
        .deliveryMode(2)
        .contentType("application/json")
        .messageId(orderId)
        .build();

    for (OrderEvent event : batch) {
        channel.basicPublish("orders", "order.created", true, props,
                             objectMapper.writeValueAsBytes(event));
    }
    channel.waitForConfirmsOrDie(5_000);
}
```

消费端的关键是“手动 ack + 有限预取 + 失败分类”：

```java
channel.basicQos(20);
channel.basicConsume("inventory.order-created", false, (tag, delivery) -> {
    try {
        handler.handle(delivery.getBody());
        channel.basicAck(delivery.getEnvelope().getDeliveryTag(), false);
    } catch (RetryableException e) {
        // 不立即 requeue；转入延迟/重试队列，避免毒消息循环
        channel.basicNack(delivery.getEnvelope().getDeliveryTag(), false, false);
    } catch (Exception e) {
        channel.basicNack(delivery.getEnvelope().getDeliveryTag(), false, false);
    }
}, tag -> { });
```

注意：`automaticRecoveryEnabled` 会恢复连接、信道、消费者和拓扑，但不会替你恢复未确认消息的业务状态，也不会让重复消息消失。消费端仍然必须幂等。

## Spring AMQP：把可靠性做成配置

Spring AMQP 的价值在于把监听容器、确认模式、重试和死信组合起来。下面是一个偏生产化的配置骨架：

```java
@Configuration
public class RabbitConfig {

    @Bean
    public ConnectionFactory connectionFactory() {
        CachingConnectionFactory factory = new CachingConnectionFactory("broker.internal");
        factory.setUsername("order-svc");
        factory.setPassword(System.getenv("RABBITMQ_PASSWORD"));
        factory.setVirtualHost("/app");
        factory.setPublisherConfirmType(CachingConnectionFactory.ConfirmType.CORRELATED);
        factory.setPublisherReturns(true);
        return factory;
    }

    @Bean
    public RabbitTemplate rabbitTemplate(ConnectionFactory cf) {
        RabbitTemplate template = new RabbitTemplate(cf);
        template.setMandatory(true);
        template.setReturnsCallback(returned ->
            log.error("unroutable: {} {}", returned.getExchange(), returned.getRoutingKey()));
        return template;
    }

    @Bean
    public SimpleRabbitListenerContainerFactory listenerFactory(ConnectionFactory cf) {
        SimpleRabbitListenerContainerFactory factory = new SimpleRabbitListenerContainerFactory();
        factory.setConnectionFactory(cf);
        factory.setAcknowledgeMode(AcknowledgeMode.MANUAL);
        factory.setPrefetchCount(20);
        factory.setConcurrentConsumers(4);
        factory.setMaxConcurrentConsumers(16);
        factory.setDefaultRequeueRejected(false); // 失败进 DLX，而不是无限 requeue
        return factory;
    }
}
```

监听器里手动 ack，并区分异常：

```java
@RabbitListener(queues = "inventory.order-created",
                containerFactory = "listenerFactory")
public void onOrder(OrderEvent event, Channel channel,
                    @Header(AmqpHeaders.DELIVERY_TAG) long tag) throws IOException {
    try {
        inventoryService.reserve(event);
        channel.basicAck(tag, false);
    } catch (Exception e) {
        // requeue=false：交给 DLX/重试拓扑；业务必须幂等
        channel.basicNack(tag, false, false);
    }
}
```

Spring 的坑：

- `AcknowledgeMode.AUTO` 不是“自动 ack”，而是在方法成功返回后 ack；异常时行为取决于容器配置；
- `defaultRequeueRejected=true` 会让毒消息无限循环；
- `@Transactional` 只覆盖数据库事务，不会把 RabbitMQ 发布变成 XA；
- 重试拦截器可能在没有幂等的情况下重复调用业务。

## .NET：异步信道与确认

`RabbitMQ.Client` 6.x 与 7.x 的 API 有差异；下面用 7.x 风格的异步 `IChannel` 表达核心语义。

```csharp
var factory = new ConnectionFactory {
    HostName = "broker.internal",
    VirtualHost = "/app",
    UserName = "order-svc",
    Password = Environment.GetEnvironmentVariable("RABBITMQ_PASSWORD"),
    AutomaticRecoveryEnabled = true,
    TopologyRecoveryEnabled = true
};

await using var connection = await factory.CreateConnectionAsync();
await using var channel = await connection.CreateChannelAsync();

await channel.ExchangeDeclareAsync("orders", ExchangeType.Topic, durable: true);
await channel.QueueDeclareAsync("inventory.order-created", durable: true,
    exclusive: false, autoDelete: false,
    arguments: new Dictionary<string, object?> {
        ["x-dead-letter-exchange"] = "orders.dlx"
    });
await channel.QueueBindAsync("inventory.order-created", "orders", "order.created");

await channel.ConfirmSelectAsync();
var props = new BasicProperties {
    DeliveryMode = DeliveryModes.Persistent,
    ContentType = "application/json",
    MessageId = orderId
};
await channel.BasicPublishAsync("orders", "order.created", mandatory: true,
    props, body: payload);

// 批量发布后统一等待
await channel.WaitForConfirmsOrDieAsync(TimeSpan.FromSeconds(5));
```

消费端用 `AsyncEventingBasicConsumer`，不要在回调里做长时间同步阻塞：

```csharp
var consumer = new AsyncEventingBasicConsumer(channel);
consumer.ReceivedAsync += async (_, ea) => {
    try {
        await handler.HandleAsync(ea.Body.ToArray());
        await channel.BasicAckAsync(ea.DeliveryTag, false);
    } catch (Exception) {
        await channel.BasicNackAsync(ea.DeliveryTag, false, requeue: false);
    }
};
await channel.BasicQosAsync(prefetchSize: 0, prefetchCount: 20, global: false);
await channel.BasicConsumeAsync("inventory.order-created", autoAck: false, consumer);
```

注意 `global: false`。RabbitMQ 4.3 起 global QoS 默认被拒绝，跨语言代码都要避免依赖它。

## Go：amqp091-go 与显式重连监督

Go 客户端本身不提供自动重连，通常需要自己写 supervisor：连接断开后重建连接、channel、拓扑和消费者。

```go
func consume(ctx context.Context, url string) error {
    conn, err := amqp.Dial(url)
    if err != nil { return err }
    defer conn.Close()

    ch, err := conn.Channel()
    if err != nil { return err }
    defer ch.Close()

    if err := ch.Qos(20, 0, false); err != nil { return err }
    deliveries, err := ch.Consume("inventory.order-created", "",
        false, false, false, false, nil)
    if err != nil { return err }

    for {
        select {
        case <-ctx.Done():
            return nil
        case d, ok := <-deliveries:
            if !ok { return errors.New("delivery channel closed") }
            if err := handle(d.Body); err != nil {
                _ = d.Nack(false, false) // 进入 DLX，不 requeue
                continue
            }
            _ = d.Ack(false)
        }
    }
}
```

发布端：

```go
ch, _ := conn.Channel()
_ = ch.Confirm(false)
confirms := ch.NotifyPublish(make(chan amqp.Confirmation, 256))
for _, msg := range batch {
    _ = ch.PublishWithContext(ctx, "orders", "order.created", true, false,
        amqp.Publishing{
            DeliveryMode: amqp.Persistent,
            ContentType:  "application/json",
            MessageId:    msg.ID,
            Body:         msg.Body,
        })
}
for i := 0; i < len(batch); i++ {
    if c := <-confirms; !c.Ack {
        return errors.New("publish nacked")
    }
}
```

重连监督的要点：

- 用指数退避 + 抖动，避免所有实例同时重连；
- 重连后重新声明拓扑（或由平台保证拓扑已存在）；
- 旧 channel 上的未确认消息会被 broker 重投，业务必须幂等；
- 用 context 控制关闭，不要在信号处理里无限等待。

## Node.js：amqplib 与背压

Node 是单线程事件循环，任何同步 CPU 工作或阻塞 IO 都会拖住心跳和消费。用 `ConfirmChannel` 发布，用 `prefetch` 控制未确认数。

```javascript
const amqp = require('amqplib');

async function main() {
  const conn = await amqp.connect(process.env.AMQP_URL);
  const ch = await conn.createConfirmChannel();

  await ch.assertExchange('orders', 'topic', { durable: true });
  await ch.assertQueue('inventory.order-created', {
    durable: true,
    arguments: { 'x-dead-letter-exchange': 'orders.dlx' }
  });
  await ch.bindQueue('inventory.order-created', 'orders', 'order.created');

  ch.publish('orders', 'order.created', Buffer.from(payload), {
    persistent: true,
    contentType: 'application/json',
    messageId: orderId,
    mandatory: true
  }, (err) => {
    if (err) console.error('nacked/returned', err);
  });
  await ch.waitForConfirms();

  await ch.prefetch(20);
  await ch.consume('inventory.order-created', async (msg) => {
    if (!msg) return;
    try {
      await handle(msg.content);
      ch.ack(msg);
    } catch (e) {
      ch.nack(msg, false, false);
    }
  }, { noAck: false });
}
```

不要在 `consume` 回调里做同步 `fs.readFileSync` 或大计算；把 CPU 工作丢到 worker thread，把阻塞 IO 换成异步版本。

## Python：pika 与 aio-pika

阻塞版 pika 的 `BlockingConnection` 不是线程安全的；多线程要用每线程独立连接/channel，或改用异步客户端。

```python
import pika

params = pika.ConnectionParameters(
    host="broker.internal",
    virtual_host="/app",
    credentials=pika.PlainCredentials("order-svc", password),
    heartbeat=30,
    blocked_connection_timeout=30,
)

connection = pika.BlockingConnection(params)
channel = connection.channel()
channel.confirm_delivery()
channel.basic_qos(prefetch_count=20)

channel.basic_publish(
    exchange="orders",
    routing_key="order.created",
    body=payload,
    mandatory=True,
    properties=pika.BasicProperties(
        delivery_mode=2,
        content_type="application/json",
        message_id=order_id,
    ),
)
channel.wait_for_confirms()
```

aio-pika 的 `RobustConnection` 会自动重连，但不会自动恢复业务状态：

```python
import aio_pika

connection = await aio_pika.connect_robust(url)
async with connection:
    channel = await connection.channel()
    await channel.set_qos(prefetch_count=20)
    queue = await channel.declare_queue(
        "inventory.order-created",
        durable=True,
        arguments={"x-dead-letter-exchange": "orders.dlx"},
    )
    exchange = await channel.declare_exchange(
        "orders", aio_pika.ExchangeType.TOPIC, durable=True
    )
    await queue.bind(exchange, "order.created")

    async with queue.iterator() as iterator:
        async for message in iterator:
            async with message.process(requeue=False):
                await handle(message.body)
```

`message.process(requeue=False)` 在异常时会 nack 且不重投；要确保队列配置了 DLX，否则消息会被丢弃。

## 跨语言共同的反模式

| 反模式 | 后果 | 正确做法 |
|---|---|---|
| 每请求建连接 | 连接风暴、TLS/认证开销 | 进程级连接 + 重连监督 |
| 多线程共享一个 channel 做 RPC | 响应串线、状态损坏 | 每线程/每用途独立 channel |
| 只开 mandatory 不开 confirm | 不知道 broker 是否接受 | confirm + return 一起处理 |
| 每条消息同步等 confirm | 吞吐被 RTT 限制 | 批量发布 + 批量/异步 confirm |
| 失败就 requeue | 毒消息无限循环 | 退避/上限/DLX |
| auto-ack 关键业务 | 崩溃即丢消息 | 手动 ack + 幂等 |
| 回调里阻塞事件循环 | 心跳超时、假死 | 线程池/异步 IO |
| 依赖 global QoS | 4.3 起默认拒绝 | per-consumer prefetch |
| 重连后不恢复消费者/拓扑 | “连接正常但没消费” | 拓扑恢复 + 健康检查 |
| 不记录 message_id | 无法对账/重放 | 业务唯一键 + 结构化日志 |

## 客户端可观测性

每个客户端至少应暴露：

- 连接状态、重连次数、最后一次错误；
- channel 数、consumer 数；
- publish 速率、confirm 延迟、nack/return 数；
- 消费速率、失败率、当前未确认数；
- 端到端延迟（消息内时间戳）；
- 与 broker 的 `blocked`/`unblocked` 事件。

不要只在 broker 侧看指标。客户端的连接泄漏、重连风暴、回调阻塞，往往只能从客户端侧发现。

## 与 Mini-RabbitMQ 的关系

Mini 的 `client.py` 是一个同步教学客户端，只支持单 channel 同时一个 RPC、没有自动重连、没有连接池。它适合验证协议语义，不适合作为多语言客户端行为的参考。真实项目请用上表的成熟客户端，并用真实 RabbitMQ 做互操作测试。

## 小结

多语言客户端的差异不在“能不能发消息”，而在确认模型、线程/协程安全、重连恢复和背压。Java/Spring 适合企业集成但要防重试拦截器重复调用；.NET 要区分信道生命周期与异步回调；Go 需要自己写重连监督；Node 要避免阻塞事件循环；Python 要区分阻塞与 asyncio。跨语言的共同底线是：连接复用、手动 ack、per-consumer prefetch、confirm + return、幂等、优雅关闭、可观测。

---

[返回目录](../README.md) | [上一章：附录 I RabbitMQ 4.x 版本演进、破坏性变更与升级迁移](I-version-migration.md) | [下一章：附录 K 生产故障案例库与复盘模板](K-production-incident-library.md)
