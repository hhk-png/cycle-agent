# 附录 H 实验课与生产上线检查清单

## 实验环境

```bash
cd RabbitMQ-tutorial
python -m pytest -q
python examples/run_all.py
```

每个实验都可以先用 Mini 完成，再用真实 RabbitMQ 复现。Mini 实验不需要 Docker；真实实验需要 2～3 节点集群。

## 实验 1：四类交换机

目标：理解 direct/fanout/topic/headers 的差异。

步骤：

1. 运行 `python examples/03_pubsub_fanout.py`；
2. 运行 `python examples/04_routing_direct.py`；
3. 运行 `python examples/05_topics.py`；
4. 运行 `python examples/06_headers.py`；
5. 修改 topic 绑定，观察 `#` 零词匹配；
6. 用管理 API 查看 bindings。

验收：能解释为什么同一条消息进入某队列而不进入另一个。

## 实验 2：死信与重试

目标：设计一个可运维的失败路径。

步骤：

1. 运行 `python examples/08_dead_letter.py`；
2. 给队列加 `x-message-ttl`，观察过期死信；
3. 构造 nack requeue 循环，观察毒消息；
4. 加 `x-delivery-limit`，验证达到上限后死信；
5. 设计 parking lot 队列和重放流程。

验收：能画出 work → retry → parking 的拓扑，并说明重复/顺序。

## 实验 3：优先级与 TTL

目标：理解堆序、clamp、TTL 边界。

步骤：

1. 声明 `x-max-priority=10`；
2. 发布不同优先级，观察消费顺序；
3. 设置消息 `expiration`，验证过期；
4. 设置队列 `x-message-ttl`，对比；
5. 讨论 TTL 队头阻塞。

验收：能解释为什么低优先级消息可能饥饿。

## 实验 4：Publisher Confirms

目标：区分路由成功/broker 接受/持久化。

步骤：

1. 运行 `python examples/09_publisher_confirms.py`；
2. 发布到不存在的 routing key，开启 mandatory，观察 return；
3. 队列设 `x-max-length=1`、`x-overflow=reject-publish`，观察 nack；
4. 批量发布 1000 条，比较逐条 wait 与批量 wait 的耗时；
5. 关闭 confirm，模拟不可路由，观察无反馈。

验收：能说出 confirm 与 return 的区别。

## 实验 5：持久化与恢复

目标：验证 durable/persistent/ack 的边界。

步骤：

1. 运行 `python examples/11_persistence.py`；
2. 把消息改成 transient，重复；
3. 消费后不 ack，重启（Mini 快照/真实 broker）；
4. ack 后重启，验证不恢复；
5. 删除快照/数据目录，观察恢复失败。

验收：能解释“持久化不等于备份”。

## 实验 6：AMQP 抓包与原始帧

目标：看懂握手和内容帧。

步骤：

1. 启动 `python -m mini_rabbitmq serve --no-persistence --port 5673`；
2. 用 `tcpdump`/Wireshark 抓 5673（或打印帧调试）；
3. 运行 `python examples/12_wire_amqp.py`；
4. 观察 protocol header、start/tune/open、publish/header/body、deliver/ack；
5. 用 `tests/test_wire_server.py::test_raw_handshake_connection_start` 对照字段。

验收：能画出一次 publish 的帧序列。

## 实验 7：管理 API 与指标

目标：从外部观察 broker。

步骤：

1. 运行 `python examples/13_management_api.py`；
2. 用 curl 查 `/api/overview`、`/api/queues`；
3. 查 `/metrics`；
4. 触发告警（Mini `set_alarm`，真实磁盘/内存阈值）；
5. 观察健康检查/指标变化。

验收：能写一条“队列积压 + 消费者为 0”的告警规则。

## 实验 8：Streams

目标：理解 offset/retention/分区。

步骤：

1. 运行 `python examples/14_streams.py`；
2. 创建两个消费者，从不同 offset 读；
3. 设置小 retention，观察旧数据被 trim；
4. 用 SuperStream 分区，验证同一 key 同分区；
5. 讨论与 Kafka 的差异。

验收：能解释为什么 Stream 不能替代任务队列。

## 实验 9：Raft 与 Quorum

目标：理解多数派/选举/日志。

步骤：

1. 运行 `python examples/15_quorum_failover.py`；
2. 停一个 follower，继续写；
3. 停 leader，观察选举；
4. 停两个节点，观察写入失败；
5. 恢复节点，观察追赶；
6. 注入日志冲突（测试里有例子）。

验收：能解释为什么 3 节点容忍 1 个故障。

## 实验 10：Shovel/Federation

目标：跨 broker 复制/转发。

步骤：

1. 运行 `python examples/16_shovel_federation.py`；
2. 让目标不可路由，观察 requeue；
3. 注入 origin header，观察防环；
4. 讨论 active-active 的重复/顺序；
5. 设计多区域拓扑。

验收：能说出 Shovel 与 Federation 的区别。

## 实验 11：安全与 ACL

目标：最小权限和错误隔离。

步骤：

1. 运行 `python examples/17_security_acl.py`；
2. 创建只读/只写用户；
3. 尝试越权声明/消费，观察 403；
4. 用错误密码连接；
5. 观察 channel 错误不杀死连接；
6. 讨论 TLS/OAuth 的生产配置。

验收：能写出一个服务的 configure/write/read 正则。

## 实验 12：顺序、分区与重排序

目标：把“队列是 FIFO”拆成可观察的边界。

步骤：

1. 运行 `python examples/22_ordering_partitions.py` 和 `python -m pytest -q tests/test_ordering.py`；
2. 单队列单消费者发布 5 条消息，确认 FIFO；
3. 给同一队列加两个消费者，确认全局交错、每个消费者的子序列有序；
4. `nack(requeue=True)` 一条消息，确认 `redelivered` 标记和处理序列变化；
5. 用 `x-max-priority` 队列观察优先级如何覆盖 FIFO；
6. 用 SuperStream 同一 key 发多条消息，确认同一分区、分区内有序；
7. 把一批 `(key, seq)` 乱序输入 Resequencer，确认按 key 连续输出；
8. 讨论：如果业务要求“同一订单严格有序”，哪些消费者/重试配置必须改。

验收：能画出生产、入队、投递、处理、副作用五层顺序，并说明每一层由谁保证、失败时如何恢复。

## 实验 13：端到端流水线（Capstone）

目标：把发布/消费/失败/监控/运维串起来。

起点：先运行 `examples/19_outbox_idempotency.py`，理解 outbox 事务、重复发布、幂等消费和 ack 前崩溃的闭环；再把 SQLite/Mini 替换为真实数据库/RabbitMQ，并补上[附录 P](P-application-testing-chaos.md)的集成与故障注入测试。

需求：

- 订单主题交换机；
- 库存/审计两个队列；
- 消费失败进入重试/停车队列；
- 发布 confirm + mandatory；
- 消费手动 ack + 幂等键；
- 管理 API/指标告警；
- 故障演练（消费者崩溃/队列满/节点重启）。

交付：

1. 拓扑图；
2. 代码/配置；
3. 测试（发布/消费/重复/失败）；
4. 监控面板/告警；
5. 故障演练报告；
6. 迁移/回滚方案。

验收标准：

- 消息不因单点故障丢失（在声明语义内）；
- 重复消息不产生重复副作用；
- DLQ 可观测/可重放；
- 文档能让他人复现。

## 生产上线检查清单

### 需求与语义

- [ ] 明确 at-least-once/at-most-once；
- [ ] 明确顺序范围（全局/实体）；
- [ ] 明确重放/保留需求；
- [ ] 明确 RPO/RTO；
- [ ] 明确消息大小/峰值速率。

### 拓扑

- [ ] vhost 隔离；
- [ ] 交换机/队列/绑定版本化；
- [ ] durable/persistent/队列类型；
- [ ] 队列长度/TTL/溢出；
- [ ] DLX/重试/parking；
- [ ] 命名/路由键规范；
- [ ] auto-delete/exclusive 清理。

### 发布

- [ ] confirms；
- [ ] mandatory/return；
- [ ] outbox/幂等键；
- [ ] 超时/重试/退避；
- [ ] 批量/异步 confirm；
- [ ] 不可路由告警。

### 消费

- [ ] 手动 ack；
- [ ] prefetch 匹配；
- [ ] 幂等/去重；
- [ ] 失败分类/退避/上限；
- [ ] 优雅关闭；
- [ ] 消费者并发/下游容量；
- [ ] 顺序分区。

### 集群/存储

- [ ] 3 节点 Quorum（关键队列）；
- [ ] 分区策略；
- [ ] 磁盘/内存水位；
- [ ] 备份/恢复演练；
- [ ] 升级/回滚；
- [ ] 故障演练。

### 安全

- [ ] 禁用 guest/最小权限；
- [ ] TLS/mTLS/OAuth；
- [ ] 管理面隔离/HTTPS；
- [ ] 证书/密钥轮换；
- [ ] 审计/日志脱敏；
- [ ] 插件供应链。

### 可观测

- [ ] 队列/连接/资源/Quorum 指标；
- [ ] confirm/return/DLQ/redelivery；
- [ ] 端到端延迟；
- [ ] 告警/runbook；
- [ ] 追踪/correlation_id；
- [ ] 容量趋势。

### 组织

- [ ] owner/on-call；
- [ ] 变更流程；
- [ ] 培训；
- [ ] 成本/TCO；
- [ ] 供应商/许可风险；
- [ ] 迁移/退出策略。

## 评分建议

Capstone 可按：

- 语义正确性 30%；
- 失败处理 25%；
- 测试/验证 20%；
- 可观测/运维 15%；
- 文档/复盘 10%。

不要只按“功能跑通”评分；消息系统的价值在失败时体现。

---

[返回目录](../README.md) | [上一章：附录 G 代码地图与测试矩阵](G-code-map-test-matrix.md) | [下一章：附录 I RabbitMQ 4.x 版本演进、破坏性变更与升级迁移](I-version-migration.md)
