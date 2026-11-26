# 第 05 章 交换机、绑定与路由算法

> 本章定位：掌握 RabbitMQ 最核心的“路由层”。交换机不存消息，但决定了消息是否到达正确的队列；绑定设计错误，后面的确认和重试都救不回来。

## 路由的基本方程

一次发布的输入是：

- exchange name；
- routing key；
- headers（消息属性的一部分）；
- bindings（交换机上预先声明的规则）。

输出是“哪些队列应该收到这条消息”。用伪代码表示：

```text
destination_queues = []
for binding in exchange.bindings:
    if exchange.matches(binding, routing_key, headers):
        destination_queues.append(binding.destination)
```

不同交换机类型实现不同的 `matches`。Fanout 永远匹配；Direct 比较字符串；Topic 做通配匹配；Headers 比较键值。交换机到交换机绑定则会递归路由，形成路由图。

## 默认交换机

默认交换机名称是空字符串 `""`，类型为 direct，由 broker 隐式创建。每个队列会自动以队列名作为 routing key 绑定到默认交换机。因此：

```python
broker.publish("", "order.created", body)
```

会直接投递到名为 `order.created` 的队列。默认交换机不能被显式声明、删除，也不能在其上手动绑定；管理 API 中通常会显示它。

默认交换机适合简单任务和原型，但生产系统建议使用显式交换机：

- 显式命名便于审计和权限控制；
- 可以用 topic/fanout 表达多消费者关系；
- 默认交换机的隐式绑定容易让“队列名即路由键”耦合到生产者代码；
- 迁移或重命名队列时，默认交换机的生产者需要改代码。

Mini-RabbitMQ 在 `VHost.publish` 中对空交换机做了特殊处理：直接查找同名队列，而不是维护一份隐式绑定。这样语义一致，也避免用户误删默认绑定。

## Direct 交换机

Direct 只匹配 routing key 完全相等：

```text
binding: error  -> queue errors
binding: warning -> queue warnings

publish key=error   => errors
publish key=warning => warnings
publish key=info    => no route
```

Direct 的行为可预测、开销低，适合“任务类型”“日志级别”“租户 ID”等离散路由。它不会做前缀或通配；`order.*` 在 direct 里就是字面字符串。

一个队列可以绑定多个 key，一个 key 也可以绑定多个队列：

```text
queue audit -> error, warning
queue alerts -> error
```

此时 `error` 会进入两个队列。设计上要明确“广播”是业务需求还是重复消费；如果多个消费者属于同一逻辑订阅，应使用同一个队列的竞争消费者，而不是多个队列各自收到一份。

## Fanout 交换机

Fanout 忽略 routing key，把消息复制到所有绑定的队列：

```text
exchange logs (fanout)
  -> queue audit
  -> queue billing
  -> queue alerts
```

适合事件广播、缓存失效、审计复制。注意 fanout 是“每个绑定队列一份消息”，不是“每个消费者一份”。如果三个消费者都从同一个队列消费，它们会竞争消息，而不是每人收到一份；需要每人一份就绑定三个队列。

Fanout 也常用于死信交换机、Shovel 目标、Federation 下游。它不关心 routing key，因此绑定参数通常留空。

## Topic 交换机

Topic 是 RabbitMQ 最灵活的内置类型。routing key 由点分隔：

```text
order.created
order.eu.created
payment.eu.captured
```

绑定模式支持：

- `*`：匹配恰好一个词；
- `#`：匹配零个或多个词。

例子：

| 模式 | 匹配 | 不匹配 |
|---|---|---|
| `order.*` | `order.created` | `order.eu.created` |
| `order.#` | `order.created`、`order.eu.created` | `payment.created` |
| `*.created` | `order.created` | `order.created.eu` |
| `#` | 任意 key，包括空串 | 无 |

`#` 可以匹配零个词，因此 `order.#` 能匹配 `order` 本身。这个边界经常被忽略。

Topic 的匹配算法可以用递归或动态规划实现。Mini 的 `topic_matches` 使用带记忆化的递归，避免 `#` 的指数级回溯：

```python
memo = {}

def match(i, j):
    if (i, j) in memo:
        return memo[(i, j)]
    if i == len(pattern):
        result = j == len(key)
    elif pattern[i] == "#":
        result = match(i + 1, j) or (j < len(key) and match(i, j + 1))
    elif j == len(key):
        result = False
    elif pattern[i] == "*" or pattern[i] == key[j]:
        result = match(i + 1, j + 1)
    else:
        result = False
    memo[(i, j)] = result
    return result
```

测试覆盖了 `a.#.z`、`a.*`、空串、`#` 单独使用等边界。

## Headers 交换机

Headers 交换机忽略 routing key，比较消息 headers 与绑定参数。`x-match` 决定匹配模式：

- `all`：所有普通键值都匹配；
- `any`：至少一个匹配；
- 以 `x-` 开头的键是控制参数，不参与比较；
- 需要把 `x-` 头也纳入比较时，用 `all-with-x` / `any-with-x`（4.x 支持），否则 `x-*` 永远被忽略。

```text
binding arguments:
  x-match: all
  format: pdf
  department: sales

message headers:
  format: pdf
  department: sales
  trace: abc
=> match
```

Headers 适合“多维属性路由”，例如按文档类型 + 部门 + 优先级路由。它的代价是：

- 绑定匹配比 direct/topic 更重；
- headers 本身占消息体积；
- 参数难以在 UI 中快速审计；
- 不适合极高频、超大绑定表。

生产上常见做法是用 topic 表达主路由，headers 只用于少量辅助维度。

## 备用交换机（Alternate Exchange）

在交换机声明参数中设置：

```text
alternate-exchange = ae
```

当消息在该交换机上“无法路由”时，broker 会把消息转发到备用交换机。关键点：

- 触发条件是**没有匹配绑定**，不是“队列不存在”；
- 备用交换机可以是任何类型；
- 备用交换机本身也可能无法路由，此时消息可能被丢弃或返回；
- mandatory 和 alternate exchange 可以同时使用，具体返回行为取决于路由结果；
- 备用交换机链要有终止条件，避免环。

备用交换机适合“未路由消息审计”：

```text
orders (topic) --unrouted--> orders.unrouted (fanout) -> queue audit.unrouted
```

Mini 在 `VHost._route_exchange` 和 `VHost.publish` 中实现备用交换机，并用 `tests/test_broker.py::test_alternate_exchange` 验证。Mini 也会沿备用交换机递归路由，因此两跳链可以工作；无论 Mini 还是真实 RabbitMQ，生产设计都应保持链短，并设置明确的终止点。

## 交换机到交换机绑定

AMQP 0-9-1 支持 exchange.bind / exchange.unbind。消息可以从一个交换机路由到另一个交换机，再路由到队列。这适合构建路由层级：

```text
global (topic)
  -> region.eu (topic)
  -> region.us (topic)
       -> queue eu.orders
       -> queue us.orders
```

管理上要特别小心：

- 环会导致路由递归，必须有 visited/深度限制；
- 备用交换机和 E2E 可以组合出复杂路径；
- `if-unused` 删除交换机时要考虑入站绑定；
- 交换机删除会移除相关绑定，可能改变其他交换机行为。

Mini 的 `_route_exchange` 用 visited 集合和 32 层深度限制防环，并在绑定目标队列去重。真实 RabbitMQ 也有自己的环检测与路由限制；不要把“没崩溃”当成“拓扑正确”。

## 一致性哈希交换机

`x-consistent-hash` 是常见插件（rabbitmq_consistent_hash_exchange）。它把 routing key（或指定 header）哈希到环上的一个节点，从而把同一键稳定映射到同一个队列。典型用途：

- 把同一订单/用户的消息路由到同一个队列，保持局部顺序；
- 在多个消费者/队列之间分片；
- 避免所有消息堆积到一个队列。

绑定键表示权重，例如 `"50"`、`"10"`。权重越高，虚拟节点越多，分到的流量越大。Mini 的 `ConsistentHashExchange` 用排序虚拟节点和 SHA-1 实现确定性选择，并支持 `hash-header` 参数。

注意：

- 一致性哈希只保证“同一个键尽量到同一个队列”，增删绑定时仍会迁移部分键；
- 它不能替代业务分区键设计；
- 插件不是核心协议，跨版本/跨云厂商支持要确认。

### 内置模数哈希：`x-modulus-hash`（4.3+）

4.3 起 RabbitMQ 内核自带 `x-modulus-hash` 交换机。它不维护哈希环，而是对 routing key 做哈希后取模：

```text
destination = hash(routing_key) mod N
N = 绑定条目总数（不是去重后的队列数）
```

它忽略绑定键的具体值，只看 routing key。特点：

- 绑定关系不变时，同一 routing key 总是到同一队列，跨重启也稳定；
- 适合“按订单 ID/租户 ID 分区 + 每个分区单活消费者”的顺序方案；
- 绑定同一队列多次可以给它加权（权重 = 该队列的绑定条目数），但 N 也随条目总数变化；
- 增删队列或改变权重都会改变 N，几乎重排所有键；这一点不如一致性哈希插件平滑。

选择建议：拓扑长期固定、要严格分区顺序，用 `x-modulus-hash`；需要动态扩缩容且尽量少迁移键，用一致性哈希插件。两者都不是“免费的顺序保证”——消费者并发、重投和 prefetch 仍会破坏顺序，必须配合单活消费者或分区内串行处理。

## 交换机声明与等价性

声明交换机时要明确：

```text
name, type, durable, auto-delete, internal, arguments
```

再次声明同名交换机时，属性必须一致，否则返回 406 PRECONDITION_FAILED。常见事故：

- 开发环境用了 fanout，生产代码声明 direct；
- 一边 durable=true，一边 durable=false；
- 参数里多了 `alternate-exchange`，另一边没有。

Mini 的 `VHost.declare_exchange` 检查 type/durable/auto_delete/internal；队列声明则检查 durable/exclusive/auto_delete/arguments。真实 RabbitMQ 对参数的比较更严格。上线前应把拓扑作为代码/配置管理，避免不同服务各自 declare 出冲突。

## 路由键设计

好的 routing key 应该：

1. 语义稳定：`order.created` 比 `order_service_v2_new` 更持久；
2. 层级有限：2～5 段通常够用，过长难以维护；
3. 不携带敏感信息：routing key 会出现在日志和管理 UI；
4. 与分区键区分：topic 路由键和一致性哈希键可以是不同字段；
5. 有命名规范：团队约定 domain.entity.action，避免大小写和分隔符混用。

示例：

```text
order.created
order.paid
order.cancelled
payment.captured
payment.refunded
```

不要用随机 UUID 作为 topic key 的唯一层级，否则绑定模式会变得不可读。若需要按租户分区，用一致性哈希或独立队列，而不是把租户塞进 topic 的固定段。

## Mini 实现的代码路径

| 功能 | 位置 | 测试 |
|---|---|---|
| direct/fanout/topic/headers | `mini_rabbitmq/exchange.py` | `tests/test_exchange.py` |
| 默认交换机、E2E、备用交换机 | `mini_rabbitmq/vhost.py` | `tests/test_broker.py`、`tests/test_wire_server.py` |
| 一致性哈希 | `mini_rabbitmq/exchange.py` | `tests/test_exchange.py` |
| 绑定管理 API | `mini_rabbitmq/management.py` | `tests/test_management.py` |

`examples/03_pubsub_fanout.py`、`examples/04_routing_direct.py`、`examples/05_topics.py`、`examples/06_headers.py` 分别演示四类交换机。

## 拓扑设计模式

交换机/绑定不是“配置细节”，而是架构的表达。下面是几种常见模式，以及它们的失败方式。

### 模式 1：命令队列（work queue）

```text
exchange: jobs (direct)
  binding: resize -> queue jobs.resize
  binding: email  -> queue jobs.email

queue jobs.resize: 多个 worker 竞争消费
```

适用：任务分发、后台作业、需要每任务只执行一次。

陷阱：多个队列绑定同一个 key 会变成广播；消费者失败 requeue 会破坏顺序；没有 DLX 时毒消息会卡住队列。

### 模式 2：事件广播（pub/sub）

```text
exchange: orders (topic)
  binding: order.created -> queue billing.order-created
  binding: order.created -> queue inventory.order-created
  binding: order.#       -> queue audit.all-orders
```

适用：一个事件多个下游、审计、通知。

陷阱：每个订阅者一个队列，队列会各自收到一份消息；如果多个消费者共享一个队列，它们是竞争关系，不是“每人一份”。

### 模式 3：CQRS/事件溯源式路由

```text
exchange: domain (topic)
  binding: order.*   -> queue write-model.orders
  binding: order.*   -> queue read-model.orders
  binding: payment.* -> queue read-model.payments
```

适用：写模型与读模型分离、投影/物化视图。

陷阱：读模型重建需要重放历史；普通队列 ack 后删除，无法重放。需要 Stream 或上游事件日志。

### 模式 4：Saga/编排

```text
exchange: saga.commands (direct)
  queue saga.order.step1
  queue saga.order.step2
  queue saga.order.compensate

exchange: saga.events (topic)
  binding: saga.* -> queue saga.coordinator
```

适用：分布式事务补偿、长流程。

陷阱：Saga 不是 ACID；补偿必须幂等；消息重复/乱序要用状态机处理；超时和人工介入要有入口。

### 模式 5：多租户

```text
exchange: tenant.events (topic)
  binding: tenant.<id>.order.# -> queue tenant.<id>.orders
```

或每个租户独立 vhost。选择取决于隔离强度、租户数量、运维成本。

陷阱：租户数量增长导致队列/vhost 爆炸；routing key 泄露租户信息；权限正则过宽。

### 模式 6：蓝绿/灰度

```text
exchange: orders (topic)
  binding: order.created -> queue orders.v1
  binding: order.created -> queue orders.v2   # 影子消费

消费者 v1 处理生产；v2 只读比对，不产生副作用。
```

适用：迁移、重写消费者、验证新逻辑。

陷阱：影子消费者不能和生产消费者共用同一队列，否则会竞争并 ack 掉同一条消息；应给影子消费者建独立队列，并从同一交换机用相同 routing key 绑定——RabbitMQ 会向每个匹配队列各投递一份，不需要 Federation/Shovel。Federation/Shovel 用于跨集群/vhost 隔离。比对要考虑重复/乱序。

## 绑定数量与匹配成本

路由的逻辑是“对所有绑定做匹配”，但真实实现会做索引/优化。你能控制的是：

- 绑定数量：一个交换机上几千个绑定，匹配和元数据成本都会上升；
- 通配符复杂度：`#` 的回溯比 `*` 更重，极端模式会放大匹配成本；
- headers 绑定：比 direct/topic 更重，且 headers 占消息体积；
- E2E 链：每层递归都会增加路由路径和环检测成本；
- 重复绑定：同一队列被多个绑定命中，是否重复投递取决于实现/语义，不要依赖“去重”做业务假设。

设计建议：

- 用 direct 表达精确路由，topic 表达层级，headers 只做少量辅助维度；
- 避免“每个实体一个绑定”的无界增长；
- 大租户/大客户用独立交换机或 vhost，避免共享绑定表；
- 定期审计无用绑定和交换机；
- 对路由失败配置 alternate exchange 或 mandatory + 审计。

## routing key 与事件契约治理

routing key 是生产者和消费者之间的隐式契约。没有治理，半年后就会出现：

```text
order.created
order.created.v2
OrderCreated
order_created
order.created.new
```

治理建议：

1. **命名规范**：`domain.entity.action`，全小写，点分隔；
2. **版本策略**：版本放 header/body，不要轻易改 routing key；必须改时并行发布旧/新 key；
3. **不要放敏感信息**：routing key 会出现在日志、管理 UI、追踪里；
4. **不要放随机 ID**：会让绑定不可读、匹配爆炸；
5. **契约文档**：用 AsyncAPI/JSON Schema/CloudEvents 描述事件；
6. **所有权**：每个交换机/事件有明确 owner 和废弃流程；
7. **兼容性测试**：生产者/消费者跨版本契约测试。

一个可操作的检查：把生产环境所有 routing key 导出，按前缀聚类，看看是否存在同义不同名、版本混乱、租户信息泄露。拓扑治理和技术治理一样重要。

## 备用交换机与 DLX 的组合

备用交换机（AE）和死信交换机（DLX）经常被混用，但它们解决不同问题：

| 机制 | 触发 | 处理对象 |
|---|---|---|
| Alternate exchange | 消息在该交换机上没有任何匹配绑定 | “路由失败”的消息 |
| Dead letter exchange | 消息被拒绝/过期/超长/超过投递次数 | “处理失败/过期”的消息 |

组合方式：

```text
orders (topic)
  alternate-exchange: orders.unrouted
  queue orders.work
    x-dead-letter-exchange: orders.dlx

orders.unrouted (fanout) -> queue audit.unrouted
orders.dlx (fanout)      -> queue orders.failed
```

注意：

- AE 只处理“当前交换机无匹配”，不处理“队列满/消费失败”；
- DLX 是重新发布，可能再次不可路由；DLX 自身也要有 AE/审计；
- 两者都可能形成环，必须有终止条件；
- mandatory + AE 同时存在时，最终行为取决于路由结果，客户端要同时处理 return 和 confirm。

本章的六种模式是起点，不是全集。内容路由、拆分/聚合、索赔检查、路由单、Saga、多活等模式的拓扑、失败路径、反模式和测试映射，见[附录 S](../appendices/S-messaging-patterns-antipatterns.md)。

## 本章小结

交换机负责匹配，队列负责存储。Direct 看精确 key，fanout 广播，topic 用 `*`/`#` 通配，headers 看属性；备用交换机处理不可路由消息，E2E 绑定构建路由层级，一致性哈希用于稳定分片。拓扑设计要避免环、重复绑定、参数漂移和敏感信息进入 routing key。

下一章从路由转到队列内部：FIFO、优先级、TTL、长度上限、溢出策略和队列类型，解释“消息进了队列之后会发生什么”。

---

[上一章：第 04 章 连接、信道与虚拟主机](04-connections-channels-vhosts.md) | [返回目录](../README.md) | [下一章：第 06 章 队列语义：FIFO、优先级、TTL 与长度限制](06-queues-ttl-priority.md)
