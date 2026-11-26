# 附录 C 配置、参数与功能矩阵

## 队列参数（x-arguments）

| 参数 | 类型 | 作用 | Mini |
|---|---|---|---|
| `x-queue-type` | string | classic/quorum/stream | classic/quorum（stream 未接入） |
| `x-message-ttl` | int ms | 队列级消息 TTL | 支持 |
| `x-expires` | int ms | 队列不活跃过期 | 支持 |
| `x-max-length` | int | 最大消息数 | 支持 |
| `x-max-length-bytes` | int | 最大字节数 | 支持（估算口径不同） |
| `x-overflow` | string | drop-head/reject-publish/reject-publish-dlx | 支持 |
| `x-dead-letter-exchange` | string | 死信交换机 | 支持 |
| `x-dead-letter-routing-key` | string | 死信 routing key | 支持 |
| `x-max-priority` | int 0-255 | 优先级 | 支持（classic） |
| `x-single-active-consumer` | bool | 单活消费者 | 支持 |
| `x-delivery-limit` | int | 最大投递次数 | 支持（简化） |
| `x-queue-mode` | string | lazy 等历史模式 | 接受但忽略 |
| `x-queue-version` | int | classic 版本 | 未实现 |
| `x-max-in-memory-length` | int | 内存上限 | 未实现 |
| `x-max-in-memory-bytes` | int | 内存字节上限 | 未实现 |
| `x-stream-max-segment-size-bytes` | int | Stream 段大小 | 模型支持 |
| `x-max-age` | string | Stream 保留时间 | 模型支持 |
| `x-stream-filter-size-bytes` | int | Stream 过滤 | 未实现 |

真实 RabbitMQ 的参数可用性取决于队列类型/版本。例如 Quorum Queue 不使用 classic 的 `x-max-priority`：4.0–4.2 是 normal/high 两档调度，4.3+ 是 32 级严格优先级（0–31，总是启用，无优先级按 4 处理）。使用前查目标版本矩阵，不要把一个版本的优先级结论套到另一个版本。

## Policy key 映射

| Policy key | 队列参数 |
|---|---|
| `max-length` | `x-max-length` |
| `max-length-bytes` | `x-max-length-bytes` |
| `message-ttl` | `x-message-ttl` |
| `expires` | `x-expires` |
| `dead-letter-exchange` | `x-dead-letter-exchange` |
| `dead-letter-routing-key` | `x-dead-letter-routing-key` |
| `max-priority` | `x-max-priority` |
| `overflow` | `x-overflow` |
| `queue-mode` | `x-queue-mode` |
| `single-active-consumer` | `x-single-active-consumer` |
| `delivery-limit` | `x-delivery-limit` |
| `max-in-memory-length` | `x-max-in-memory-length` |
| `max-in-memory-bytes` | `x-max-in-memory-bytes` |

Mini 的 `mini_rabbitmq/policy.py` 实现这张映射，并在声明/策略变更时合并。policy 按 priority 从低到高覆盖。

## rabbitmq.conf 常用项（真实 RabbitMQ）

```ini
listeners.tcp.default = 5672
management.tcp.port = 15672
heartbeat = 60
frame_max = 131072
channel_max = 2047
default_vhost = /
vm_memory_high_watermark.relative = 0.6
disk_free_limit.absolute = 5GB
cluster_partition_handling = pause_minority
consumer_timeout = 1800000
log.file.level = info
```

TLS 示例：

```ini
listeners.ssl.default = 5671
ssl_options.cacertfile = /etc/rabbitmq/ca.pem
ssl_options.certfile = /etc/rabbitmq/server.pem
ssl_options.keyfile = /etc/rabbitmq/server-key.pem
ssl_options.verify = verify_peer
ssl_options.fail_if_no_peer_cert = true
```

注意：

- `consumer_timeout` 是消费者 ack 超时，不是消息 TTL；超时会关闭 channel；
- 内存/磁盘阈值要留安全余量；
- `cluster_partition_handling` 必须按网络/业务选择并演练；
- TLS 证书路径/权限/轮换要纳入配置管理；
- 真实配置项随版本变化，以官方文档为准。

## Mini CLI 配置

见附录 B。Mini 没有完整配置文件，只有命令行参数；数据快照路径默认 `var/data/broker.json`。

## 功能矩阵

| 能力 | Mini | RabbitMQ 4.x 概念 |
|---|---|---|
| AMQP 0-9-1 TCP | 子集 | 完整 |
| AMQP 1.0 | 无 | 核心（4.0+ 默认启用） |
| MQTT/STOMP/WebSocket | 无 | 插件 |
| 交换机 direct/fanout/topic/headers | 有 | 有 |
| 一致性哈希 | 有 | 插件；4.3+ 另有内置 `x-modulus-hash` |
| E2E 绑定 | 有 | 有 |
| Alternate exchange | 有 | 有 |
| 默认交换机 | 有 | 有 |
| vhost | 有 | 有 |
| 用户/ACL | PLAIN + 正则 | 多种认证后端 |
| 队列 TTL/长度/溢出 | 有 | 有 |
| 优先级 | 有（classic 多级；quorum 拒绝 x-max-priority） | classic 多级；quorum 4.0–4.2 两档、4.3+ 32 级 |
| 死信/x-death | 有 | 有 |
| delivery-limit | 简化 | quorum 等 |
| publisher confirms | 有（内存） | 持久化/复制 |
| 事务 | 有（简化） | 有（非 XA） |
| 持久化 | JSON 快照 | 段文件/WAL/Quorum |
| Quorum/Raft | 单进程模拟 | 真实复制 |
| Quorum 延迟重试 | 无 | 4.3+ 原生 `x-delayed-retry-*` |
| 定时投递 | TTL 模型 | 4.4+ Quorum `x-opt-delivery-time` |
| Quorum 压缩 | 无 | 4.3+ |
| 元数据存储 | 内存 JSON | Khepri（4.3+ 唯一） |
| Streams | 语义模型 | 完整 + 协议 |
| Federation/Shovel | 教学模型 | 插件 |
| 管理 API | 子集 | 完整 |
| Prometheus | 文本 | 插件 |
| TLS | 可传入 SSLContext | 完整配置 |
| OAuth/LDAP | 无 | 插件 |
| 集群 | 无 | 有 |
| K8s Operator | 无 | 有 |
| 备份/恢复 | 快照 | 工具/方案 |

## 版本相关注意

- RabbitMQ 4.x 的 classic queue v2、Quorum、Khepri、AMQP 1.0 能力随小版本演进；
- 经典镜像队列在 4.x 已不再是可用/推荐方案；
- Quorum Queue 的参数/限制（优先级、exclusive、TTL 细节）需查版本文档：4.0–4.2 两档优先级，4.3+ 32 级；
- 4.3 起 Khepri 是唯一元数据存储，Mnesia 移除；`cluster_partition_handling` 不再生效；
- 4.3 起延迟交换机插件不可用，改用原生延迟重试；4.4 起可用 `x-opt-delivery-time`；
- 4.4 的 CQv2 `.sqs` 文件是单向升级门；
- Stream 的 retention/segment/协议支持随版本变化；
- 管理/Prometheus 指标名/标签可能变化；
- 插件兼容性必须逐版本验证。

## Mini 环境变量

Mini 只读取 `MINI_RABBITMQ_PASSWORD` 作为 CLI `--password` 默认值；其他配置通过命令行参数。认证/权限通过 Python API 或管理面修改。

## 配置管理建议

1. 把拓扑/策略/用户定义为代码；
2. 用 definitions 导出做漂移检测；
3. 配置变更走评审/审计；
4. 生产/预发参数分离；
5. 对内存/磁盘/consumer_timeout 设置显式值；
6. 证书/密钥独立管理；
7. 升级前核对参数弃用/重命名；
8. 变更后跑故障/回归测试。

---

[返回目录](../README.md) | [上一章：附录 B CLI 与 HTTP 管理 API 参考](B-cli-http-api.md) | [下一章：附录 D RabbitMQ 与 Kafka、Pulsar、NATS、SQS 对比](D-comparison.md)
