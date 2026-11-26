# 附录 A AMQP 0-9-1 方法、字段与错误码速查

本附录只覆盖教程实现和常见生产排障所需的方法子集。字段顺序必须与 AMQP 0-9-1 规范一致；位字段按 LSB first 打包。完整定义以官方规范为准。

## 帧

```text
frame = type(1) + channel(2, BE) + size(4, BE) + payload + 0xCE
```

| type | 名称 | 说明 |
|---:|---|---|
| 1 | METHOD | class-id + method-id + args |
| 2 | HEADER | content header |
| 3 | BODY | 消息正文分片 |
| 8 | HEARTBEAT | channel 必须为 0 |

## 连接类（class 10）

| method | id | 字段 |
|---|---:|---|
| connection.start | 10 | version-major octet, version-minor octet, server-properties table, mechanisms longstr, locales longstr |
| connection.start-ok | 11 | client-properties table, mechanism shortstr, response longstr, locale shortstr |
| connection.secure | 20 | challenge longstr |
| connection.secure-ok | 21 | response longstr |
| connection.tune | 30 | channel-max short, frame-max long, heartbeat short |
| connection.tune-ok | 31 | channel-max short, frame-max long, heartbeat short |
| connection.open | 40 | virtual-host shortstr, reserved-1 shortstr, reserved-2 bit |
| connection.open-ok | 41 | reserved-1 shortstr |
| connection.close | 50 | reply-code short, reply-text shortstr, class-id short, method-id short |
| connection.close-ok | 51 | 无 |
| connection.blocked | 60 | reason shortstr |
| connection.unblocked | 61 | 无 |

## 信道类（class 20）

| method | id | 字段 |
|---|---:|---|
| channel.open | 10 | reserved-1 shortstr |
| channel.open-ok | 11 | reserved-1 longstr |
| channel.flow | 20 | active bit |
| channel.flow-ok | 21 | active bit |
| channel.close | 40 | reply-code short, reply-text shortstr, class-id short, method-id short |
| channel.close-ok | 41 | 无 |

## 交换机类（class 40）

| method | id | 字段 |
|---|---:|---|
| exchange.declare | 10 | reserved-1 short, exchange shortstr, type shortstr, passive/durable/auto-delete/internal/no-wait bits, arguments table |
| exchange.declare-ok | 11 | 无 |
| exchange.delete | 20 | reserved-1 short, exchange shortstr, if-unused/nowait bits |
| exchange.delete-ok | 21 | 无 |
| exchange.bind | 30 | reserved-1 short, destination shortstr, source shortstr, routing-key shortstr, no-wait bit, arguments table |
| exchange.bind-ok | 31 | 无 |
| exchange.unbind | 40 | 同 bind 字段 |
| exchange.unbind-ok | 41 | 无 |

注意 exchange.bind 的字段顺序是 destination、source、routing-key；不要与 queue.bind 混淆。

## 队列类（class 50）

| method | id | 字段 |
|---|---:|---|
| queue.declare | 10 | reserved-1 short, queue shortstr, passive/durable/exclusive/auto-delete/no-wait bits, arguments table |
| queue.declare-ok | 11 | queue shortstr, message-count long, consumer-count long |
| queue.bind | 20 | reserved-1 short, queue shortstr, exchange shortstr, routing-key shortstr, no-wait bit, arguments table |
| queue.bind-ok | 21 | 无 |
| queue.purge | 30 | reserved-1 short, queue shortstr, no-wait bit |
| queue.purge-ok | 31 | message-count long |
| queue.delete | 40 | reserved-1 short, queue shortstr, if-unused/if-empty/nowait bits |
| queue.delete-ok | 41 | message-count long |
| queue.unbind | 50 | reserved-1 short, queue shortstr, exchange shortstr, routing-key shortstr, arguments table |
| queue.unbind-ok | 51 | 无 |

## Basic 类（class 60）

| method | id | 字段 |
|---|---:|---|
| basic.qos | 10 | prefetch-size long, prefetch-count short, global bit |
| basic.qos-ok | 11 | 无 |
| basic.consume | 20 | reserved-1 short, queue shortstr, consumer-tag shortstr, no-local/no-ack/exclusive/no-wait bits, arguments table |
| basic.consume-ok | 21 | consumer-tag shortstr |
| basic.cancel | 30 | consumer-tag shortstr, no-wait bit |
| basic.cancel-ok | 31 | consumer-tag shortstr |
| basic.publish | 40 | reserved-1 short, exchange shortstr, routing-key shortstr, mandatory/immediate bits |
| basic.return | 50 | reply-code short, reply-text shortstr, exchange shortstr, routing-key shortstr |
| basic.deliver | 60 | consumer-tag shortstr, delivery-tag longlong, redelivered bit, exchange shortstr, routing-key shortstr |
| basic.get | 70 | reserved-1 short, queue shortstr, no-ack bit |
| basic.get-ok | 71 | delivery-tag longlong, redelivered bit, exchange shortstr, routing-key shortstr, message-count long |
| basic.get-empty | 72 | reserved-1 shortstr |
| basic.ack | 80 | delivery-tag longlong, multiple bit |
| basic.reject | 90 | delivery-tag longlong, requeue bit |
| basic.recover-async | 100 | requeue bit |
| basic.recover | 110 | requeue bit |
| basic.recover-ok | 111 | 无 |
| basic.nack | 120 | delivery-tag longlong, multiple/requeue bits |

`basic.publish` 后必须跟 content header 和 body 帧；没有 body 也要发 body-size=0 的 header。`basic.return`、`basic.deliver`、`basic.get-ok` 后也跟 content header/body。

## Confirm 与 Tx

| method | id | 字段 |
|---|---:|---|
| confirm.select | 10 | nowait bit |
| confirm.select-ok | 11 | 无 |
| tx.select | 10 | 无（class 90） |
| tx.select-ok | 11 | 无 |
| tx.commit | 20 | 无 |
| tx.commit-ok | 21 | 无 |
| tx.rollback | 30 | 无 |
| tx.rollback-ok | 31 | 无 |

## Content header

```text
class-id(2) | weight(2) | body-size(8) | property-flags(2) | properties
```

Basic 属性标志位：

| bit | 属性 | 类型 |
|---:|---|---|
| 15 | content-type | shortstr |
| 14 | content-encoding | shortstr |
| 13 | headers | table |
| 12 | delivery-mode | octet |
| 11 | priority | octet |
| 10 | correlation-id | shortstr |
| 9 | reply-to | shortstr |
| 8 | expiration | shortstr |
| 7 | message-id | shortstr |
| 6 | timestamp | longlong |
| 5 | type | shortstr |
| 4 | user-id | shortstr |
| 3 | app-id | shortstr |
| 2 | cluster-id | shortstr |

## 字段表类型

| 类型码 | 名称 | 编码 |
|---|---|---|
| t | boolean | 1 字节 |
| b | signed 8-bit | 1 字节 |
| B | unsigned 8-bit | 1 字节 |
| U | signed 16-bit | 2 字节 |
| u | unsigned 16-bit | 2 字节 |
| I | signed 32-bit | 4 字节 |
| i | unsigned 32-bit | 4 字节 |
| L | signed 64-bit | 8 字节 |
| l | unsigned 64-bit | 8 字节 |
| f | float | 4 字节 |
| d | double | 8 字节 |
| D | decimal | scale octet + int32 |
| S | long string | 4 字节长度 + UTF-8 |
| x | byte array | 4 字节长度 + bytes |
| A | array | 4 字节长度 + typed values |
| T | timestamp | 8 字节 |
| F | nested table | table |
| V | void | 无 |

## 错误码

| code | 名称 | 典型场景 |
|---:|---|---|
| 311 | CONTENT_TOO_LARGE | 内容过大 |
| 312 | NO_ROUTE | mandatory 不可路由 |
| 313 | NO_CONSUMERS | immediate（历史） |
| 320 | CONNECTION_FORCED | 管理/节点强制关闭 |
| 402 | INVALID_PATH | vhost 路径无效 |
| 403 | ACCESS_REFUSED | 权限/认证失败 |
| 404 | NOT_FOUND | 队列/交换机不存在 |
| 405 | RESOURCE_LOCKED | exclusive 资源被占用 |
| 406 | PRECONDITION_FAILED | 参数不一致/交付标签未知 |
| 501 | FRAME_ERROR | 帧格式错误 |
| 502 | SYNTAX_ERROR | 字段语法错误 |
| 503 | COMMAND_INVALID | 命令无效 |
| 504 | CHANNEL_ERROR | 信道错误 |
| 505 | UNEXPECTED_FRAME | 非预期帧 |
| 506 | RESOURCE_ERROR | 资源告警 |
| 530 | NOT_ALLOWED | 不允许的操作 |
| 540 | NOT_IMPLEMENTED | 未实现 |
| 541 | INTERNAL_ERROR | 内部错误 |

Mini 的错误码映射在 `mini_rabbitmq/errors.py`，客户端测试覆盖 403/404/406/540 等路径。

## 快速判断

- 队列不存在 → 404，关闭 channel；
- 交换机不存在（发布时）→ 404，关闭 channel；
- 参数不一致 → 406，关闭 channel；
- 无权限 → 403，关闭 channel；
- 帧非法 → 501，关闭 connection；
- 不可路由 → 312 return（不关闭）；
- 队列满 reject-publish → basic.nack（不关闭）；
- 未实现 → 540，关闭 channel。

本附录只覆盖 AMQP 0-9-1。AMQP 1.0 的帧、类型系统、performative、credit 与 settlement 见[附录 R](R-amqp-1-0-deep-dive.md)。

---

[返回目录](../README.md) | [下一章：附录 B CLI 与 HTTP 管理 API 参考](B-cli-http-api.md)
