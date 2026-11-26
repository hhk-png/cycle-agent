# 附录 E 故障排查手册

排查原则：先确认“消息在哪”，再查“为什么不动”，最后才重启。重启会破坏现场，掩盖根因。

## 快速定位表

| 症状 | 优先检查 | 常见原因 | 处理 |
|---|---|---|---|
| 发布成功但队列无消息 | 绑定/路由键/交换机类型 | 路由不匹配、默认交换机误解、队列名错 | 管理 UI bindings、mandatory、trace |
| 发布报 404 | 交换机/队列是否存在 | 未声明、vhost 错、被删除 | 声明拓扑/检查 vhost |
| 发布报 403 | 用户/vhost/正则 | 权限不足、guest 远程限制 | `list_permissions`、修正 ACL |
| 发布变慢/阻塞 | 告警/队列深度/confirm | 内存/磁盘告警、队列满、confirm 未处理 | 看 alarms、扩容、处理积压 |
| 消费者收不到 | consumer 数/队列/vhost | 没启动、队列错、exclusive、prefetch | 管理 API/channel 状态 |
| 消费卡住 | unacked/prefetch/下游 | 处理阻塞、prefetch 满、死锁 | 看消费者线程栈、下游延迟 |
| 队列持续增长 | 生产/消费速率 | 消费者不足/慢、下游故障、毒消息 | 扩容/限流/DLQ |
| 消息重复 | redelivered/ack 时序 | 连接断开、ack 前崩溃、重试 | 幂等/去重/缩短处理 |
| 消息丢失 | durable/persistent/confirm | 非持久、无 confirm、队列被删 | 配置修复/审计 |
| 连接数暴涨 | 客户端生命周期 | 每请求建连接、泄漏、重连风暴 | 连接复用/退避 |
| channel 错误 | reply-code | 队列不存在/参数不一致/无权限 | 看错误码/修正声明 |
| 管理 API 401 | 认证/guest 限制 | 密码错、guest 非本机 | 本机/独立用户 |
| TLS 失败 | 证书/主机名/CA | 证书过期、CA 不匹配、verify | 预发验证/轮换 |
| quorum 不可写 | 多数派/leader | 节点宕机/分区 | 恢复节点/检查网络 |
| DLQ 暴涨 | 失败原因 | 下游故障、毒消息、maxlen | 告警/修复/重放 |

## 发布路径排查

1. 交换机存在？vhost 对？类型/参数一致？
2. 绑定是否存在？routing key 是否匹配？
3. 是否发到默认交换机但队列名写错？
4. mandatory 是否开启？basic.return 是否被处理？
5. publisher confirm 是否开启？是否收到 ack/nack？
6. 队列是否满（max-length/bytes）？overflow 策略？
7. 是否被 alternate exchange 接走？
8. 消息是否过期（TTL）？
9. 队列是否被 policy 删除/auto-delete？
10. 是否持久化但节点故障？

Mini 中可用：

```python
result = broker.publish("ex", "key", b"x", mandatory=True)
print(result.routed, result.queues, result.returned)
```

真实 RabbitMQ 用管理 UI 的 exchange bindings + publish mandatory + 客户端 return 回调。

## 消费路径排查

1. 队列有消费者吗？
2. 消费者连接/vhost 对吗？
3. channel 是否因错误关闭？
4. prefetch 是否已满（unacked 高）？
5. 消费者线程是否阻塞在下游？
6. 队列是否被 exclusive/单活消费者占用？
7. 消息是否被另一个消费者拿走？
8. ack 是否发送成功？
9. 是否 auto_ack 导致“看起来没有 unacked”？
10. 是否有 DLX 把消息转走？

真实 RabbitMQ：

```bash
rabbitmqctl list_queues -p /app name messages messages_ready messages_unacknowledged consumers
rabbitmqctl list_consumers -p /app
rabbitmqctl list_channels -p /app connection number consumer_count messages_unacknowledged
```

具体命令字段以版本为准。

## 队列积压

判断类型：

- 生产速率 > 消费速率：扩容消费者/优化处理；
- 消费者为 0：启动/重连；
- unacked 高：prefetch/处理慢/下游故障；
- 磁盘/内存告警：分页/阻塞，需扩容/清理；
- 毒消息：反复 requeue，需 DLQ/上限。

处理：

1. 先限流/降级，避免继续恶化；
2. 修复下游/消费者；
3. 扩容（消费者/分区/节点）；
4. 对积压做限速重放；
5. 不要直接清空队列，除非确认可丢；
6. 监控恢复时间。

## 连接/channel 问题

- 连接数持续上涨：检查客户端是否每请求建连接、是否正确 close、是否有重连风暴；
- channel 数上涨：每请求建 channel、消费者泄漏；
- blocked 持续：内存/磁盘告警，扩容/清理；
- 连接频繁断开：心跳、网络、TLS、服务端超时、consumer_timeout；
- 认证失败：密码/证书/OAuth token、guest 限制。

## Quorum/集群

```bash
rabbitmqctl cluster_status
rabbitmq-queues quorum_status <queue>
rabbitmq-diagnostics check_port_connectivity
rabbitmq-diagnostics status
```

检查：

- 节点是否全部存活；
- 是否有 partitions；
- quorum queue 是否有 leader/足够副本；
- 磁盘/内存是否告警；
- Erlang cookie/节点名/DNS；
- 版本/feature flags；
- 网络延迟/丢包。

多数派丢失时不要强行写入；先恢复网络/节点。

## 重复消息

来源：

- 消费者 ack 前崩溃/断开；
- 网络分区/重连；
- publisher confirm 超时后重发；
- nack requeue；
- Shovel/Federation 重试；
- 事务/批量 ack 边界。

处理：

- 幂等键/去重表；
- 缩短处理时间；
- 调整 prefetch；
- 监控 redelivered；
- 对账。

不要试图通过“关闭重投”解决；那会丢消息。

## 消息丢失

排查：

- 队列/交换机 durable？
- 消息 persistent？
- publisher confirm 是否等到了 ack？
- 是否被 max-length/drop-head 丢弃？
- 是否 TTL 过期？
- 是否 DLX 不可路由？
- 是否队列被删除/auto-delete？
- 是否节点故障且未复制？
- 是否消费者 auto_ack 后崩溃？

修复：

- 关键消息 durable/persistent/quorum；
- confirm + return；
- DLX + 审计；
- 备份/对账；
- 消费手动 ack + 幂等。

## TLS/认证

- 证书 CN/SAN 是否匹配主机名；
- CA 链是否完整；
- 客户端是否信任服务端 CA；
- 服务端是否要求客户端证书；
- 证书是否过期；
- TLS 版本/套件；
- 代理是否终止 TLS；
- OAuth token 过期/scope；
- guest 是否远程登录。

不要通过关闭证书校验“临时解决”；应修复信任链。

## 管理面

- 401：认证失败/未带 Authorization；
- 403：用户无 vhost/权限；
- 404：对象不存在/URL 编码错（vhost `/` 要 `%2F`）；
- 503：健康检查失败（告警）；
- 管理 UI 慢：队列/连接太多，避免高频轮询。

## Mini 特定问题

| 现象 | 原因 | 处理 |
|---|---|---|
| 端口占用 | 5672/15672 已被真实 RabbitMQ 占用 | 用 `--port 5673 --management-port 15673` |
| guest 连接失败 | 非 loopback | 用 127.0.0.1 或创建用户 |
| 测试挂起 | 服务端线程未停止/端口冲突 | 用 port=0、确保 stop/close |
| 快照报错 | JSON 损坏/格式版本 | 删除 `var/data/broker.json` 或修复 |
| snapshot 很慢 | 全量 JSON | 只用于教学，减小数据量 |
| confirm 立即 ack 但重启丢 | Mini 内存接受 | 不要用于生产 |
| Stream 队列声明失败 | core 不支持 x-queue-type=stream | 用 `mini_rabbitmq/streams.py` 模型 |
| Raft 不可跨进程 | 单进程模拟 | 用真实 RabbitMQ 学习生产 |
| TLS 无测试 | 只支持传入 SSLContext | 生产用真实 RabbitMQ |

## 禁止操作

- 生产直接 `rabbitmqctl reset`；
- 未备份删队列/交换机；
- 全量清空 DLQ；
- 关闭证书校验；
- 无限 requeue 毒消息；
- 无 confirm 发关键消息；
- 生产用 guest；
- 把管理端口暴露公网；
- 不看指标盲目扩容。

## 复盘模板

```text
事件时间线：
影响范围：
根因：
发现方式：
处理过程：
恢复时间/RPO/RTO：
做得好的：
需要改进：
行动项（owner/截止）：
监控/测试补充：
```

复盘的目标是改系统，不是找替罪羊。

---

[返回目录](../README.md) | [上一章：附录 D RabbitMQ 与 Kafka、Pulsar、NATS、SQS 对比](D-comparison.md) | [下一章：附录 F 术语表](F-glossary.md)
