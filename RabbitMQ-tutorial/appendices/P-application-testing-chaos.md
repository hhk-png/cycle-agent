# 附录 P RabbitMQ 应用测试与混沌工程手册

## 测试对象不是 broker，而是你的应用

第 25 章讨论的是 Mini-RabbitMQ 自身的测试；本附录讨论另一个更容易被忽略的问题：**使用 RabbitMQ 的业务应用怎么测**。broker 测试全绿，不代表你的生产者不会丢消息、消费者不会重复扣款、重试不会形成风暴。应用层测试要证明的是：

- 发布失败/结果未知时，业务状态是否可恢复；
- 消费重复/乱序/崩溃时，副作用是否只发生一次；
- DLQ、重试、对账是否真的能兜底；
- 配置/权限/证书变更后，应用是否还能启动；
- 故障注入时，监控和告警是否按预期工作。

## 测试金字塔

| 层级 | 依赖 | 速度 | 能证明什么 | 典型工具 |
|---|---|---|---|---|
| 单元 | 无 broker | 毫秒 | 业务处理逻辑、幂等键、状态机 | pytest/JUnit/xUnit |
| 组件 | 进程内 Mini 或 mock | 毫秒-秒 | 发布/消费/ack/DLX 的编排 | Mini、内存队列 |
| 集成 | 真实 RabbitMQ | 秒-分钟 | 协议、确认、权限、拓扑、真实客户端 | Docker/Testcontainers |
| 契约 | schema/样例 | 秒 | 事件结构/兼容性 | JSON Schema/AsyncAPI |
| 端到端 | 多服务 + broker | 分钟 | 业务链路、对账、SLO | Compose/K8s |
| 混沌 | 真实/近似环境 | 分钟-小时 | 故障下的行为与恢复 | Toxiproxy/Chaos Mesh |

不要把所有测试都写成端到端：慢、脆、定位难。也不要只写单元测试：它证明不了 confirm、ack、DLX 和重连。

## 必须覆盖的行为

| 行为 | 测试问题 | 失败时的典型 bug |
|---|---|---|
| 发布确认 | confirm 超时/失败时业务是否重试或告警 | 发布后直接认为成功 |
| mandatory return | 不可路由是否被记录/补偿 | 消息静默丢失 |
| 消费确认 | ack 前崩溃是否重投 | auto-ack 丢消息 |
| 幂等 | 同一 message_id 重复投递是否只执行一次 | 重复扣款/重复发货 |
| 顺序 | 同一实体的事件是否按版本/顺序处理 | 旧状态覆盖新状态 |
| DLQ | 失败消息是否进入 DLQ 且有上下文 | 毒消息无限循环 |
| 重试 | 退避/上限/抖动是否生效 | 重试风暴 |
| 流控 | 队列满/blocked 时生产者是否背压 | 内存暴涨/超时雪崩 |
| 安全 | 无权限连接/发布/消费是否被拒绝 | ACL 配置错误 |
| 可观测 | 失败是否产生指标/日志/追踪 | 出事无法定位 |

## 隔离与夹具

消息系统测试最常见的 flake 来源是共享状态：上一个测试的队列、消息、消费者还在。最低要求：

- 每个测试用独立 vhost 或唯一前缀（`test-{uuid}`）；
- 每个测试用随机队列名/交换机名；
- 测试结束删除 vhost/队列，或使用一次性容器；
- 集成测试用随机端口，避免和本机 5672/15672 冲突；
- 不要依赖消息到达顺序之外的时间假设；
- 消费者启动后等待“已注册”信号，而不是固定 sleep；
- 断言前轮询直到超时，超时后打印 broker 状态。

一个 pytest 夹具骨架：

```python
import uuid
import pytest

@pytest.fixture
def tenant():
    return f"test-{uuid.uuid4().hex[:12]}"

@pytest.fixture
def channel(connection, tenant):
    ch = connection.channel()
    ch.exchange_declare(
        exchange=f"{tenant}.orders", exchange_type="topic", durable=False
    )
    ch.queue_declare(queue=f"{tenant}.inventory", durable=False)
    ch.queue_bind(
        queue=f"{tenant}.inventory",
        exchange=f"{tenant}.orders",
        routing_key="order.created",
    )
    yield ch
    ch.queue_delete(queue=f"{tenant}.inventory")
    ch.exchange_delete(exchange=f"{tenant}.orders")
    ch.close()
```

生产代码不应在每次请求里声明拓扑；测试夹具可以，因为测试要自包含。

## 单元测试：把业务逻辑与 broker 解耦

消费者回调里最容易出错的是业务逻辑，不是 AMQP。把“消息 -> 命令/领域对象”的转换和“领域对象 -> 副作用”的处理拆开，就可以不启动 broker 直接测：

```python
def handle_order_created(event: dict, repo, idempotency) -> None:
    if not idempotency.claim(event["id"]):
        return
    repo.reserve(event["data"]["order_id"], event["data"]["amount"])

def test_duplicate_event_is_ignored():
    repo = FakeRepo()
    idem = FakeIdempotency()
    event = {"id": "evt-1", "data": {"order_id": "o-1", "amount": 100}}
    handle_order_created(event, repo, idem)
    handle_order_created(event, repo, idem)
    assert repo.reserved == [("o-1", 100)]
```

单元测试要覆盖：

- 重复 message_id；
- 未知字段/未知枚举；
- 缺失必填字段；
- 业务状态机非法迁移；
- 下游超时/异常分类；
- 幂等键冲突与并发。

## 组件测试：用 Mini-RabbitMQ 验证编排

Mini 适合验证“发布 -> 路由 -> 消费 -> ack/nack -> DLX”的编排，不需要 Docker。`examples/19_outbox_idempotency.py` 就是一个可运行的组件级测试：

```python
first = broker.get("inventory.order-created")
assert handle(conn, first.message) is True
broker.nack(first.delivery_tag, requeue=True)  # 模拟 ack 前崩溃

while (delivery := broker.get("inventory.order-created")) is not None:
    assert handle(conn, delivery.message) is False  # 重复被幂等挡住
    broker.ack(delivery.delivery_tag)

assert inventory_rows == 1
```

它证明了：

- outbox 行与业务状态在同一事务；
- 发布成功后 outbox 标记 sent；
- 重复发布/重投不会重复写 inventory；
- ack 前崩溃会重投，但幂等键吸收重复。

它没有证明：

- 真实 RabbitMQ 的 confirm/持久化/复制；
- 网络分区/节点故障；
- 多进程并发；
- 真实数据库事务隔离。

所以 Mini 组件测试之后，还需要真实 broker 的集成测试。

## 集成测试：真实 RabbitMQ

### Docker Compose

```yaml
services:
  rabbitmq:
    image: rabbitmq:4.4-management
    ports:
      - "5672"
      - "15672"
    environment:
      RABBITMQ_DEFAULT_USER: test
      RABBITMQ_DEFAULT_PASS: test
    healthcheck:
      test: ["CMD", "rabbitmq-diagnostics", "-q", "ping"]
      interval: 2s
      timeout: 5s
      retries: 30
```

测试进程读取 `AMQP_URL`，CI 用 service container 或 `docker compose up -d --wait`。不要用固定 sleep 等启动，等健康检查/端口可连接。

### Java Testcontainers

```java
@Testcontainers
class OrderPublisherIT {
    @Container
    static RabbitMQContainer broker =
        new RabbitMQContainer("rabbitmq:4.4-management");

    @Test
    void publishIsConfirmed() throws Exception {
        ConnectionFactory factory = new ConnectionFactory();
        factory.setHost(broker.getHost());
        factory.setPort(broker.getAmqpPort());
        factory.setUsername(broker.getAdminUsername());
        factory.setPassword(broker.getAdminPassword());

        try (Connection connection = factory.newConnection();
             Channel channel = connection.createChannel()) {
            channel.confirmSelect();
            channel.exchangeDeclare("orders", "topic", true);
            channel.queueDeclare("inventory", true, false, false, null);
            channel.queueBind("inventory", "orders", "order.created");
            channel.basicPublish("orders", "order.created", true, null,
                                 "{\"id\":\"o-1\"}".getBytes());
            assertTrue(channel.waitForConfirms(5_000));
        }
    }
}
```

### Python 集成夹具

```python
import os
import socket
import time
import pytest
import pika

def wait_port(host: str, port: int, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket() as sock:
            sock.settimeout(0.5)
            if sock.connect_ex((host, port)) == 0:
                return
        time.sleep(0.2)
    raise TimeoutError(f"{host}:{port} not ready")

@pytest.fixture(scope="session")
def amqp_url():
    url = os.environ.get("AMQP_URL", "amqp://test:test@localhost:5672/%2F")
    wait_port("localhost", 5672)
    return url

@pytest.fixture
def connection(amqp_url):
    params = pika.URLParameters(amqp_url)
    conn = pika.BlockingConnection(params)
    yield conn
    conn.close()
```

集成测试要覆盖真实客户端库的差异：pika 的 `confirm_delivery`、Spring 的 `CachingConnectionFactory`、.NET 的 `IChannel`、Go 的 `NotifyPublish`。

## 异步断言：不要用 sleep

消息系统是异步的。`sleep(1)` 既慢又不稳定。用轮询 + 超时：

```python
def eventually(predicate, timeout=10.0, interval=0.1, message="condition"):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(interval)
    raise AssertionError(f"timeout waiting for {message}; last={last!r}")
```

Java 用 Awaitility：

```java
await().atMost(Duration.ofSeconds(10)).untilAsserted(() ->
    assertEquals(1, received.size()));
```

轮询失败时输出 broker 状态（队列深度、消费者数、连接状态），否则只看到“超时”无法定位。

## 测试 confirm 与 return

### confirm 成功

- 发布到已声明交换机/已绑定队列；
- 等待 `waitForConfirms` 或异步 confirm；
- 断言业务记录标记 sent；
- 断言队列 ready/unacked 符合预期。

### confirm 失败/结果未知

- 关闭连接/杀节点，让 confirm 超时；
- 断言应用进入“结果未知”状态，而不是直接成功；
- 断言重试/对账能补偿；
- 对不可路由消息，断言 `basic.return` 被记录并进入补偿。

### 强制 return

- `mandatory=True` 发布到一个没有匹配绑定的 routing key；
- 或发布到不存在的交换机（会关闭 channel，需区分错误类型）；
- 断言 return 回调收到 exchange/routing key/message_id。

```python
channel.confirm_delivery()
returned = []
channel.add_on_return_callback(lambda ch, method, props, body: returned.append(props.message_id))
channel.basic_publish(
    exchange="orders",
    routing_key="no.such.binding",
    body=b"{}",
    mandatory=True,
)
eventually(lambda: returned == ["msg-1"], message="basic.return")
```

## 测试重投与 ack 丢失

三种模拟方式：

1. **不 ack 直接关闭 channel/connection**：broker 会重投未确认消息；
2. **`basic.nack(requeue=True)`**：显式重投；
3. **杀消费者进程/容器**：最接近真实崩溃。

断言：

- 消息 `redelivered=True`；
- 业务副作用只发生一次（幂等）；
- 重投次数/delivery-limit 符合预期；
- 超过上限进入 DLQ/parking。

## 测试幂等

幂等测试要覆盖“单线程重复”和“并发重复”：

- 同一 message_id 顺序投递两次；
- 同一 message_id 两个消费者并发处理；
- 业务唯一键冲突；
- 幂等表写入成功但业务事务回滚；
- 幂等表清理后重放。

并发测试可以用两个线程/协程同时调用 handler，断言数据库唯一约束或行锁生效。不要只测“第二次被忽略”，还要测“第一次失败后可以重试”。

## 测试顺序

顺序测试要区分：

- 同一分区/队列内顺序；
- 同一实体跨队列顺序；
- 重试/requeue 后的顺序；
- 多消费者竞争时的顺序。

测试方法：

- 单消费者 + 单队列，断言到达顺序；
- 按实体 ID 分区，断言同一实体顺序；
- 注入乱序，断言版本号/状态机拒绝旧事件；
- 失败重试后，断言不会用旧状态覆盖新状态。

不要断言“全局顺序”，除非拓扑真的只有一个队列一个消费者。

## 测试 DLX、重试与 delivery-limit

| 场景 | 操作 | 断言 |
|---|---|---|
| reject 进 DLX | `basic_nack(requeue=False)` | DLQ 收到，`x-death.reason=rejected` |
| TTL 过期 | 设置短 TTL，等待 | DLQ 收到，reason=expired |
| 长度溢出 | max-length=1，发两条 | 溢出消息进 DLQ/parking |
| delivery-limit | Quorum 设置上限，反复 nack | 达到上限后不再投递 |
| 重试退避 | 记录 `x-death`/自定义 attempt | 延迟递增、抖动、上限 |
| 毒消息 | 固定失败消息 | 不阻塞后续消息，进入 parking |

TTL 测试不要依赖精确毫秒；用“最终进入 DLQ”的轮询断言。Quorum 的 leader 切换/复制延迟会让时间更长，超时要留余量。

## 测试流控与大消息

- 发布接近 `max_message_size` 的消息，断言拒绝/错误可诊断；
- 发布超过 `frame_max` 的消息，断言分帧正常（不是失败）；
- 队列满 + `reject-publish`，断言 confirm nack/异常；
- 内存/磁盘告警时，断言生产者收到 blocked/背压；
- 慢消费者 + prefetch，断言未确认数受限。

不要在单元测试里模拟 16 MiB 大消息；用集成测试，且注意 CI 内存。

## 测试安全

- 无权限用户连接被拒绝；
- 有 connect 权限但无 configure/write/read 的用户，操作被拒；
- vhost 隔离：A vhost 的用户看不到 B vhost 的队列；
- TLS 证书错误/过期被拒绝；
- OAuth token 过期/权限不足；
- 管理 API 未认证/越权被拒绝；
- 日志/追踪不包含密码/token/PII。

安全测试要有“负向用例”，不要只测“正确用户能连”。

## 故障注入矩阵

| 故障 | 注入方式 | 观察点 | 期望 |
|---|---|---|---|
| broker 不可达 | 停容器/关端口 | 连接错误、重连次数 | 退避重连，不雪崩 |
| 网络延迟 | Toxiproxy latency | confirm/消费延迟 | 超时/重试可控 |
| 网络分区 | Toxiproxy/iptables | 节点状态、quorum | 少数派停止写入/业务对账 |
| 节点崩溃 | kill 容器 | leader 切换、未确认消息 | 自动恢复，消息不丢/可重投 |
| 磁盘满 | 限制磁盘/填充 | 告警、blocked | 停止接受持久化消息，告警 |
| 慢消费者 | 消费者 sleep | ready/unacked、内存 | prefetch 限制，背压 |
| 毒消息 | 固定异常 | DLQ/parking | 不阻塞队列 |
| 重复消息 | 重发/重投 | 幂等表、业务表 | 副作用一次 |
| 乱序消息 | 多队列/延迟 | 版本号/状态机 | 旧事件被忽略 |
| 时钟漂移 | 修改容器时间 | TTL/延迟/顺序 | 不依赖绝对时钟做正确性 |

## Toxiproxy 示例

Toxiproxy 在应用与 broker 之间插入可控代理：

```bash
docker run -d --name toxiproxy -p 8474:8474 -p 5673:5673 \
  ghcr.io/shopify/toxiproxy:2.9.0

toxiproxy-cli create rabbitmq -l 0.0.0.0:5673 -u rabbitmq:5672
toxiproxy-cli toxic add rabbitmq -t latency -a latency=500 -a jitter=100
toxiproxy-cli toxic remove rabbitmq -n latency_downstream
```

测试应用连接 `localhost:5673`，观察：

- confirm 延迟是否触发超时；
- 消费者心跳是否断开；
- 重连后是否恢复消费者；
- 延迟恢复后积压是否消化；
- 监控是否记录延迟/错误。

## 混沌实验模板

```text
实验名称：单节点故障下的订单消费
假设：kill 一个 quorum 节点后，消费者在 30s 内恢复，消息不丢，重复可控
稳态指标：发布成功率 > 99.9%，p99 消费延迟 < 2s，DLQ 增长 = 0
注入：随机 kill 一个 broker 容器，持续 60s
爆炸半径：预发环境单集群；生产先在低峰单节点
中止条件：发布成功率 < 95% 持续 2 分钟，或 DLQ 激增
验证：消息计数、业务表、幂等表、对账、告警、runbook
复盘：时间线、根因、改进项、负责人、截止日期
```

混沌实验不是“随便杀个节点看看”。没有稳态指标、爆炸半径和中止条件，就是制造事故。

## CI 分层

```text
每次提交：
  - 单元测试
  - schema/契约测试
  - lint/类型检查

每次合并：
  - Mini 组件测试
  - 真实 RabbitMQ 集成测试（Docker service）
  - 安全负向用例

每日/ nightly：
  - 多版本矩阵（4.2/4.3/4.4）
  - 故障注入（Toxiproxy）
  - 长稳/压测
  - 升级/回滚演练
```

CI 中的 RabbitMQ 版本要固定，不要 `latest`。多版本矩阵能提前发现 4.x 行为差异（global QoS、优先级、延迟插件、16 MiB 上限）。

## 可观测性断言

测试不只断言业务结果，还要断言“出事时能被发现”：

- confirm 失败产生指标/日志；
- DLQ 增长触发告警；
- 重连次数/耗时被记录；
- 消费延迟有直方图；
- trace 跨生产/消费传递；
- 结构化日志包含 message_id/correlation_id（不含 PII）。

可以用测试用的 metrics registry 断言计数增量，或在集成环境查询 Prometheus。

## 反模式

1. **sleep 等消息**：慢、脆、掩盖竞态；用轮询/事件。
2. **共享 broker 状态**：测试顺序一变就失败；用独立 vhost/唯一前缀。
3. **只测 happy path**：不测 nack/return/超时/崩溃。
4. **mock 掉 broker**：证明不了 confirm/ack/DLX；至少保留集成测试。
5. **断言实现细节**：断言内部方法调用而不是业务结果，重构就红。
6. **用生产镜像 latest**：CI 与生产版本漂移。
7. **不做清理**：队列/消费者泄漏，后续测试被污染。
8. **忽略负向安全用例**：ACL/TLS 配错上线才发现。
9. **没有故障注入**：文档说能恢复，实际没验证。
10. **测试数字造假**：只统计“跑了多少”，不检查“证明了什么”。

## 应用测试检查清单

- [ ] 单元测试覆盖幂等/状态机/异常分类；
- [ ] 组件测试覆盖发布-路由-消费-ack/DLX；
- [ ] 集成测试使用真实 RabbitMQ 和真实客户端；
- [ ] 测试隔离（vhost/唯一前缀/随机端口/清理）；
- [ ] 异步断言用轮询+超时，不用固定 sleep；
- [ ] confirm 成功/失败/结果未知都有用例；
- [ ] mandatory return 有断言；
- [ ] ack 前崩溃/重投有断言；
- [ ] 重复/并发/乱序有断言；
- [ ] DLX/重试/delivery-limit 有断言；
- [ ] 大消息/流控/blocked 有集成用例；
- [ ] 安全负向用例（ACL/TLS/OAuth）；
- [ ] 故障注入矩阵至少覆盖 broker 不可达/节点崩溃/慢消费者；
- [ ] 混沌实验有稳态指标/爆炸半径/中止条件；
- [ ] CI 分层且固定版本；
- [ ] 可观测性断言（指标/日志/追踪）；
- [ ] 测试失败时输出 broker 状态。

## 小结

应用层测试的目标不是“让 CI 变绿”，而是证明在 confirm 超时、ack 丢失、重复投递、节点故障和重试风暴下，业务仍然正确。用单元测试钉住业务逻辑，用 Mini 组件测试验证编排，用真实 RabbitMQ 集成测试验证协议与客户端，用契约测试保护 schema，用故障注入/混沌实验验证恢复与告警。测试隔离、异步轮询、负向用例和可观测性断言，是把“消息系统测试”从脆弱 demo 变成工程能力的关键。

---

[返回目录](../README.md) | [上一章：附录 O 事件契约、Schema 治理与事件目录](O-event-contracts-governance.md) | [下一章：附录 Q RabbitMQ 内部架构与源码阅读地图](Q-broker-internals-source-map.md)
