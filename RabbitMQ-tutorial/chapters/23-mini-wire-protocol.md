# 第 23 章 Mini-RabbitMQ 线协议与客户端实现

> 本章定位：从字节能跑到消息。拆解协议编解码、服务端握手/信道状态、客户端读线程/RPC/confirm，以及为什么原始帧测试不可省略。

## protocol.py：只做字节

`mini_rabbitmq/protocol.py` 不依赖 Broker/Queue，提供：

- `Writer` / `Reader`：octet、short、long、longlong、shortstr、longstr、table、bits；
- `encode_table` / `decode_table`：字段表；
- `encode_content_header` / `decode_content_header`：Basic 属性；
- `encode_method` / `decode_method`；
- `encode_frame` / `read_frame` / `write_frame`；
- 方法名表、协议头常量。

`Writer` 用 `bytearray` 累积，`Reader` 用偏移量切片。所有长度检查都抛 `SyntaxError_`/`FrameError`，避免越界读取。

一个容易写错的细节是 content header 的属性 flags：16 位标志从高位到低位表示 content-type、headers、delivery-mode 等，未设置的属性不占字节。Mini 用 `BASIC_PROPERTIES` 表统一编解码，测试覆盖全部属性。

## 帧读取

```python
def read_frame(sock, max_frame_size=0):
    header = read_exact(sock, 7)
    frame_type, channel, size = struct.unpack(">BHI", header)
    if max_frame_size and size + 8 > max_frame_size:
        raise FrameError(...)
    payload = read_exact(sock, size)
    end = read_exact(sock, 1)[0]   # bytes -> int；否则永远不等于 0xCE
    if end != 0xCE:
        raise FrameError(...)
    return frame_type, channel, payload
```

`read_exact` 必须循环 `recv`，因为 TCP 不保证一次读到完整帧。Mini 在客户端/服务端都使用它。

## 服务端握手

`ConnectionHandler._handshake`：

1. 读 8 字节协议头，不匹配则回送支持的协议头并关闭；
2. 发 `connection.start`（版本、server properties、PLAIN、locale）；
3. 读 `connection.start-ok`，解析 PLAIN 的 `\0user\0password`；
4. 检查 guest 是否来自 loopback；
5. 认证用户；
6. 发 `connection.tune`；
7. 读 `connection.tune-ok`，协商 frame/channel/heartbeat；
8. 读 `connection.open`，检查 vhost 权限；
9. 发 `connection.open-ok`，注册连接。

失败路径：

- 机制不支持 → 403 close；
- 认证失败 → 403 close；
- vhost 无权限 → 530 close；
- 协议头错误 → 回送协议头并关闭。

Mini 的 close 帧可能在 socket 关闭前发送；测试覆盖错误密码/无 vhost 权限的连接失败。

## ChannelState

每个 channel 保存：

- consumers：wire tag → ConsumerHandle；
- prefetch；
- confirm/tx 状态；
- `next_publish_seq`；
- 事务缓冲；
- 待完成 publish（content header/body 拼装）；
- cleanup 标志。

`_dispatch_channel_method` 按 class 分发：

- class 20：channel.open/flow/close；
- class 40：exchange；
- class 50：queue；
- class 60：basic；
- class 85：confirm；
- class 90：tx。

不支持的方法抛 `NotImplementedError_`，由外层关闭 channel（540），而不是静默成功。

## 内容拼装

`basic.publish` 方法到达时：

- 记录 exchange/routing key/mandatory；
- 拒绝 immediate；
- 分配 publish 序号；
- 创建 `_PendingPublish`；
- 等待 content header/body。

`_handle_content_header`：

- 解析 class/body-size/properties；
- 检查 user_id 与认证用户一致；
- body-size=0 立即完成；
- 否则等待 body 帧。

`_handle_content_body`：

- 追加字节；
- 超过声明大小 → connection error；
- 达到大小 → `_finish_publish_safe`。

`_finish_publish_safe` 捕获路由/队列错误：

- channel 级错误 → 关闭 channel；
- connection 级错误 → 关闭连接；
- 避免异常穿透到读线程导致无响应。

## Confirm 与 Return

`_publish_now`：

- 调 Broker 发布；
- 若 `PublishRejected` 且 confirm → nack，不关闭 channel；
- 若 `ResourceError`/其他异常 → 可能 nack 并关闭 channel；
- mandatory 返回 → 先发 basic.return + 内容；
- confirm → 发 basic.ack(seq)。

Return 的内容帧和普通投递一样：METHOD + HEADER + BODY。客户端必须拼装后再回调。

## 客户端：读线程 + RPC

`AMQPClient` 不是简单的“发一帧收一帧”，因为消费投递、心跳、confirm 是异步的。它使用：

- 一个后台读线程解析所有帧；
- 每个 channel 一个 `queue.Queue` 等待 RPC 响应；
- `pending_content` 拼装 deliver/get-ok/return 的内容；
- consumer 回调表；
- confirm 集合和条件变量。

`_rpc` 的流程：

1. 清空该 channel 的旧响应（防御性）；
2. 发送方法；
3. 在 waiter 上等待预期方法；
4. 收到 channel.close → 抛 ChannelClosedError；
5. 超时 → TimeoutError。

一个限制：每个 channel 同时只支持一个未完成 RPC。这适合同步客户端/测试；生产客户端需要关联 ID/多路并发。

## 客户端发布与 confirms

`publish`：

- 递增本地序号；
- 如果 confirm 开启，把序号加入 pending；
- 发送 METHOD + HEADER + BODY（持发送锁，避免帧交错）。

读线程收到 `basic.ack/nack` 且 channel 在 confirm 集合：

- 更新 pending/nacked；
- 通知条件变量。

`wait_for_confirms` 等待 pending 清空，返回是否没有 nack。这比“每发一条 wait 一次”更接近生产用法。

## 客户端消费

`consume`：

- 生成/使用 consumer tag；
- 先注册 callback（避免 deliver 先到）；
- 发送 basic.consume；
- 等待 consume-ok；
- 服务端随后投递 deliver。

读线程收到 deliver 方法后：

- 创建 pending content；
- 拼装 header/body；
- 构造 Message；
- 调 callback(message, WireDelivery)。

`WireDelivery` 包含 delivery_tag、consumer_tag、redelivered、exchange、routing_key、channel。示例在 `examples/12_wire_amqp.py`。

回调在读者线程执行。若回调阻塞，会阻塞同一连接的其他帧（包括心跳），因此生产客户端通常把业务处理丢到线程池/队列。Mini 的示例只是教学。

## 原始帧测试

为什么不能只测客户端+服务端互通？因为两边共享同一个 `protocol.py`，一个对称的编解码 bug 可能让测试全绿。Mini 在 `tests/test_wire_server.py::test_raw_handshake_connection_start` 中：

- 直接建立 socket；
- 发送原始协议头；
- 读帧；
- 用 `decode_method` 校验 class/method/version/table/mechanisms。

`tests/test_protocol.py` 独立验证帧布局、字段表类型、content header 属性、bits 顺序、帧结束符和最大帧。这样即使客户端/服务端有同源 bug，原始字节测试也能发现。

## 互操作边界

Mini 的目标不是通过官方 AMQP 一致性测试。已知差异：

- 不支持完整方法集；
- `basic.qos global=true` 返回 NOT_IMPLEMENTED；
- `basic.recover(requeue=false)` 不支持；
- `no-local` 不支持；
- `immediate` 不支持；
- delivery tag 全局而非 channel 级；
- 内容属性只支持 Basic 常用属性；
- 没有 access class、credit、flow 的完整语义；
- 没有官方客户端互操作测试。

因此不要在文档/演示中声称“Mini 与 RabbitMQ 完全兼容”。它是教学实现，正确说法是“实现了一个可测试的 AMQP 0-9-1 子集”。

## 一次 publish 的完整帧序列

以 `examples/12_wire_amqp.py` 的路径为例：

```text
client -> server : protocol header (AMQP\0\0\9\1)
client <- server : connection.start
client -> server : connection.start-ok (PLAIN)
client <- server : connection.tune
client -> server : connection.tune-ok
client -> server : connection.open (vhost)
client <- server : connection.open-ok
client -> server : channel.open
client <- server : channel.open-ok
client -> server : exchange.declare
client <- server : exchange.declare-ok
client -> server : queue.declare
client <- server : queue.declare-ok
client -> server : queue.bind
client <- server : queue.bind-ok
client -> server : basic.publish
client -> server : content-header
client -> server : content-body
client <- server : basic.ack (confirm, 若开启)
client -> server : basic.consume
client <- server : basic.consume-ok
client <- server : basic.deliver + header + body
client -> server : basic.ack
```

注意：

- 同一信道的 METHOD/HEADER/BODY 不能交错；
- 不同信道可以交错；
- `basic.consume-ok` 必须先于 `basic.deliver`（Mini 用 suspended 保证）；
- `basic.return` 也带 header/body；
- heartbeat 可以在任意空闲时刻插入，channel=0。

## 错误处理与关闭

Mini 的关闭路径：

```text
协议/帧错误 -> connection.close(501/502/505)
信道操作错误 -> channel.close(403/404/406/540)
发布被拒绝 -> basic.nack（不关闭）
资源告警 -> connection.blocked（不关闭）
客户端主动关闭 -> connection.close-ok / channel.close-ok
```

实现要点：

- 发送 close 后要等 close-ok（或超时）；
- 关闭 channel 时清理消费者、未确认消息重投；
- 关闭 connection 时清理所有 channel/owner 资源；
- 不要在读线程里让异常穿透，否则连接会“半死”；
- 关闭过程中仍可能收到帧，要能处理或丢弃。

## 客户端并发限制

Mini 的 `AMQPClient`：

- 每 channel 同时只支持一个未完成 RPC；
- 读线程负责所有帧分发；
- confirm 用条件变量等待；
- consumer callback 在读线程执行。

这意味着：

- 回调阻塞会阻塞同连接所有帧（包括心跳）；
- 不能在回调里做长时间同步 IO；
- 不能并发发两个 RPC 到同一 channel；
- 生产客户端需要请求关联 ID/多路并发/线程池。

这些限制是教学简化，不是 AMQP 协议限制。真实客户端库通常支持多路并发和异步回调。

## 原始帧测试的价值（补充）

前面的“原始帧测试”已经给出三层策略；这里只强调它的边界：

1. `test_protocol.py` 证明“字节布局符合我们对规范的理解”，不证明服务端状态机正确；
2. 原始握手测试证明“不经过共享客户端库也能互通”，但仍可能漏掉可选字段；
3. 客户端/服务端端到端证明“链路可用”，不能单独证明协议正确。

三层要同时存在，缺一层就会留下“同源 bug 全绿”或“协议对但业务状态错”的盲区。

如果只做第 3 层，客户端和服务端共享 `protocol.py`，一个对称 bug 会让两边都错但测试全绿。第 1/2 层用固定字节/独立解析打破这种同源假绿。

## 本章小结

线协议层把字节、方法、内容、握手和状态机分开：`protocol.py` 负责编码，`server.py` 负责协议状态，`client.py` 负责同步/异步桥接。原始帧测试防止“同源 bug 假绿”。Mini 覆盖了发布/消费/confirm/tx 的核心路径，但明确不支持完整 AMQP/官方互操作。

下一章拆管理面、Streams 模型、Raft 模拟和 Shovel/Federation，把“运维/分布式/互联”也落到代码。

---

[上一章：第 22 章 Mini-RabbitMQ 可靠性特性实现](22-mini-reliability-features.md) | [返回目录](../README.md) | [下一章：第 24 章 Mini-RabbitMQ 管理面、集群模拟与互联](24-mini-management-cluster-links.md)
