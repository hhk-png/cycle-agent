# 第 25 章 测试、验证与实现边界

> 本章定位：用测试证明语义，而不是用“跑通了”安慰自己。本章给出 219 项测试的分布、验证命令、假绿风险、已知盲区和生产化路线。

## 验证三件套

在教程根目录执行：

```bash
python -m pytest -q
python examples/run_all.py
python check_docs.py
```

或一条命令：

```bash
python verify_all.py
```

期望：

```text
219 passed
EXAMPLES OK 22/22
文档校验通过：32 章 / 26 附录 / 22 个示例 / ... / 219 项测试
VERIFY OK: 219 tests, 22 examples, 32 chapters, 26 appendices.
```

`verify_all.py` 不只看退出码：它解析 pytest 的 passed 数、示例的 `EXAMPLES OK 22/22`，避免“测试被跳过/没收集”的假绿。`check_docs.py` 会实际执行 `pytest --collect-only` 核对测试数，并检查正文里引用的验证输出是否仍是当前数字。

## 测试分布

| 测试文件 | 收集数 | 覆盖 |
|---|---:|---|
| `tests/test_auth.py` | 13 | 密码哈希、认证、vhost ACL、序列化 |
| `tests/test_broker.py` | 33 | 默认交换机、AE、E2E、vhost、策略、告警、所有权 |
| `tests/test_cluster.py` | 13 | 选举、复制、提交、故障转移、日志冲突 |
| `tests/test_exchange.py` | 28 | 四类交换机、topic 边界、一致性哈希 |
| `tests/test_links.py` | 9 | Shovel、Federation、防环、失败重排 |
| `tests/test_management.py` | 12 | HTTP 认证、API、策略、指标、权限 |
| `tests/test_persistence.py` | 9 | 快照恢复、ack、用户/策略、损坏文件 |
| `tests/test_protocol.py` | 19 | 帧、字段表、content header、截断/错误 |
| `tests/test_queue.py` | 32 | 优先级、TTL、溢出、ack、prefetch、DLX |
| `tests/test_streams.py` | 15 | offset、retention、订阅、Super Stream |
| `tests/test_ordering.py` | 8 | FIFO 边界、requeue、优先级、分区、单活消费者 |
| `tests/test_wire_server.py` | 28 | 握手、发布/消费、confirm、tx、ACL、大消息 |

合计 219。这个分布刻意偏重队列/路由/协议，因为它们是“看起来简单、边界最多”的部分。

## 测试分层

### 纯单元

- topic 匹配（`#` 零词、空串、多段）；
- 字段表类型往返；
- 密码哈希/权限正则；
- 策略参数映射；
- priority heap、TTL 计算；
- Stream retention。

这些测试不碰 socket，失败定位快。

### 组件

- Queue + Broker：入队、ack、nack、DLX、prefetch、auto-delete；
- VHost：绑定、AE、E2E、vhost 隔离；
- Persistence：快照恢复。

### 端到端

- 原始 TCP 握手（不经客户端库）；
- 客户端/服务端发布/消费/confirm/tx；
- 管理 HTTP 请求；
- 大消息分帧；
- ACL 拒绝。

端到端测试覆盖“协议 + 状态 + 业务”组合，但比单元测试慢、定位难。

## 假绿的六种常见来源

1. **共享编解码**：客户端和服务端共用 `protocol.py`，对称 bug 可能两边都错但测试通过。对策：原始帧测试 + 固定字节断言。
2. **测试恰好编码 bug 行为**：例如断言了错误的顺序/计数。对策：对照规范/真实 RabbitMQ 行为，写边界用例。
3. **静默跳过**：pytest 配置/导入错误导致测试没收集。对策：`verify_all.py` 解析 passed 数，`check_docs.py` 收集测试数。
4. **只测 happy path**：不测拒绝、超时、关闭、恢复。对策：每个特性至少一个失败路径测试。
5. **过度 mock**：mock 掉真正要验证的存储/网络。对策：保留原始 socket/文件级测试。
6. **时序偶然**：依赖 sleep 的测试不稳定。对策：用事件/轮询/确定时钟，减少固定 sleep。

## 开发中真实抓到的 bug

这些不是教程编造的“教学案例”，而是实现/测试过程中实际暴露的问题：

- **公平分发游标在空队列上被推进**：`dispatch` 先选消费者再看堆是否为空，导致每次 add_consumer 都移动轮询游标，双消费者时总是同一个收到消息。修复：先检查堆非空再选消费者。
- **策略重配置调用了不存在的方法**：`reconfigure` 引用 `_enforce_limits_locked`，但方法未实现，policy 一应用就报错。修复：实现按新上限裁剪。
- **死信回调缺失**：Queue 调 `broker.dead_letter`，但 Broker 没有该方法，异常被 `_dead_letter` 吞掉，消息静默丢失。修复：Broker 转发到 VHost，并让测试断言 DLQ 内容。
- **投递计数在 requeue 时被清零**：`x-delivery-limit` 永远不触发。修复：保留计数直到 ack/最终丢弃。
- **content header 多写了一个 continuation word**：解码器会把它当成属性起始，导致属性错位。修复：只在 flags 的 continuation 位存在时读取下一字。
- **客户端漏读 connection.start 的版本字段**：把版本字节当成 field table 长度，握手解析失败。修复：先读 two octets。

这些 bug 说明：测试不是为了“让 CI 变绿”，而是为了把语义钉死。每次修复都应补一个会失败的回归测试。

## 已知盲区

### 协议互操作

Mini 只保证自身客户端/服务端互通，并用原始帧测试关键布局；没有用 pika、Spring AMQP、.NET 等官方生态客户端做互操作矩阵。生产前若声称兼容，必须补真实客户端测试。

### TLS/安全

服务端支持传入 SSLContext，但默认不启用，也没有 TLS 握手/证书校验/OAuth/mTLS 的自动化测试。Management 没有 HTTPS/审计/速率限制。

### 多进程/并发

测试主要是单进程多线程；没有跨进程 Raft、网络分区、真实集群、长时间压测/内存泄漏测试。

### 性能

没有 benchmark 断言；Python GIL、JSON 快照、线性路由都不适合生产。教程只讲方法，不给 Mini 的 TPS 背书。

### 崩溃一致性

快照是原子替换，但没有 WAL/fsync 故障注入；无法证明“任意时刻断电”后的恢复。

### Streams/Quorum/互联

Streams 未接入 AMQP 线协议；Raft 是单进程模拟；Shovel/Federation 没有真实网络/TLS/动态配置。

### 管理面

没有分页、RBAC 细粒度、CSRF、CSP、审计、限流、HTTPS；不要暴露公网。

## 如何扩展测试

- 用真实 RabbitMQ 容器做客户端互操作测试；
- 用 `pytest-xdist` 并行化（注意端口/临时目录隔离）；
- 加属性测试（Hypothesis）覆盖字段表/路由；
- 加故障注入：kill 进程、断网、磁盘满、快照损坏；
- 加性能基准（PerfTest/自定义）；
- 加 TLS/OAuth/管理安全测试；
- 加跨进程 Raft/分区模拟；
- 加 Stream 协议集成。

每个扩展都要更新 README 计数、`check_docs.py` 的 `EXPECTED_TESTS` 和 `verify_all.py`，否则计数校验会失败。这是刻意的摩擦：文档数字必须随测试变化。

本章讨论的是 Mini 自身的测试。业务应用如何使用真实 RabbitMQ 做单元/组件/集成/契约/混沌测试，见[附录 P](../appendices/P-application-testing-chaos.md)。

## 测试覆盖矩阵

| 能力 | 单元 | 组件 | 端到端 | 已知盲区 |
|---|---|---|---|---|
| 协议帧/字段表 | ✓ |  | ✓ | 官方客户端互操作 |
| 握手/认证 |  | ✓ | ✓ | TLS/OAuth/mTLS |
| direct/fanout/topic | ✓ | ✓ | ✓ | 超大绑定表性能 |
| headers | ✓ |  |  | 高基数 headers |
| 一致性哈希 | ✓ |  |  | 增删绑定迁移 |
| E2E/AE |  | ✓ | ✓ | 复杂环/多级 AE |
| 优先级/TTL | ✓ | ✓ |  | 队头阻塞精确语义 |
| 溢出/DLX | ✓ | ✓ |  | DLX 不可路由审计 |
| ack/nack/prefetch | ✓ | ✓ | ✓ | 跨进程并发 |
| 顺序/分区/单活 | ✓ | ✓ |  | 真实 Quorum 重投/网络分区 |
| confirm/tx |  | ✓ | ✓ | 持久化后 confirm |
| 持久化/恢复 |  | ✓ |  | 崩溃一致性/WAL |
| Quorum/Raft | ✓ | ✓ |  | 真实网络/成员变更 |
| Stream | ✓ |  |  | Stream 协议/复制 |
| Shovel/Federation | ✓ |  |  | 网络/TLS/动态配置 |
| 管理 API |  | ✓ | ✓ | 分页/审计/HTTPS |

这张表的作用不是“打勾越多越好”，而是明确“哪些声明有证据，哪些只是设计意图”。

## 假绿的其他来源

除了前面六种，还有五类更偏工程治理的假绿：

7. **测试环境与生产不同构**：Mini 单进程、无 TLS、无网络分区，测试全绿不代表生产可靠。
8. **断言太弱**：只断言“没有异常”，没有断言消息内容/顺序/计数。
9. **清理不彻底**：测试之间共享状态，顺序变化导致偶发通过/失败。
10. **计数漂移**：新增测试但没更新 `EXPECTED_TESTS`，校验被绕过/失败。
11. **文档数字与代码脱节**：README/章节声称的测试数、示例数、行为与实际不符。

对策：

- 每个声明对应一个可执行测试或明确的“未实现”；
- 关键不变量用固定字节/独立实现校验；
- 测试隔离（临时目录/随机端口/独立 vhost）；
- CI 解析 passed/collected 数，防止静默跳过；
- 文档数字由脚本校验；
- 定期做“删掉实现看测试是否失败”的变异检查。

## 如何设计边界测试

以 TTL 为例：

| 场景 | 断言 |
|---|---|
| TTL=0 | 入队即过期/死信 |
| TTL 未设置 | 不过期 |
| 队列 TTL + 消息 TTL | 取较小值 |
| 队头未过期、后面过期 | 不提前死信（真实语义） |
| 过期 + DLX | 进入 DLX，x-death.reason=expired |
| 过期 + 无 DLX | 丢弃，计数正确 |
| requeue 后过期 | 不投递，进入死信 |

以 confirm 为例：

| 场景 | 断言 |
|---|---|
| 正常发布 | ack 序号正确 |
| 队列满 reject-publish | nack，不关闭 channel |
| mandatory 不可路由 | return + ack |
| 连接断开 | 未确认集合清理/重发 |
| 事务 + confirm | 拒绝混用 |
| 批量 confirm | multiple 语义正确 |

边界测试的价值在于“把语义钉死”，而不是“让覆盖率好看”。

## 生产化路线

如果要把 Mini 变成生产级 broker，按依赖顺序：

1. **协议完整度**：方法/属性/错误/互操作矩阵；
2. **存储**：WAL/段文件/fsync/恢复/快照；
3. **复制**：真实 Raft/网络 RPC/成员变更/分区；
4. **流控**：credit/blocked/内存磁盘告警；
5. **安全**：TLS/OAuth/mTLS/审计；
6. **管理**：HTTPS/分页/RBAC/审计/指标；
7. **性能**：异步 IO/零拷贝/批量/压测；
8. **运维**：升级/备份/DR/K8s Operator；
9. **Streams**：协议/分区/offset 提交/过滤；
10. **生态**：客户端库/插件/文档/兼容性认证。

每一步都需要大量测试/故障注入/长期运维，不是“再写几千行 Python”。Mini 的价值是让你理解这些工程为什么存在。

## 校验能证明什么，不能证明什么

三条命令全绿，只证明：

- 章节/附录编号连续、H1 匹配；
- 本地链接/路径存在；
- 代码围栏闭合、语言标记可识别；
- README 计数与实际一致；
- 219 项测试实际收集并通过；
- 22 个示例实际运行成功。

它**不证明**：

- 文档中的版本行为描述与 RabbitMQ 官方最新行为一致；
- 性能数字/容量公式适用于你的硬件和工作负载；
- Mini 与官方客户端互操作；
- 生产配置安全、合规、可恢复；
- 故障时系统真的按文档行为工作。

因此，版本相关结论要对照官方文档/目标集群验证；可靠性声明要靠故障注入/演练；性能结论要靠真实环境压测。文档校验是“防文档腐烂”，不是“证明系统正确”。

版本事实的核查流程、证据分级和官方资料地图见[附录 X](../appendices/X-fact-check-and-sources.md)；4.3/4.4 新特性的正反实验见[附录 W](../appendices/W-4x-feature-labs.md)。这两个附录只增加文档，不改变测试语义；当前验证口径是 219 项测试、22 个示例。跨系统桥接的可运行闭环见 `examples/20_bridge_interop.py` 和[附录 Z](../appendices/Z-event-bridging-interop.md)，零停机迁移的可运行闭环见 `examples/21_zero_downtime_migration.py` 和[第 30 章](30-change-engineering.md)，顺序/分区/单活的可运行闭环见 `examples/22_ordering_partitions.py` 和[第 31 章](31-ordering-partitions.md)；`check_docs.py` 仍然会核对附录总数、编号、链接和正文引用的计数。

## 本章小结

验证由 219 项测试、22 个示例和文档校验组成。测试分单元/组件/端到端三层，覆盖核心语义和失败路径。已知盲区包括官方互操作、TLS、多进程、性能、崩溃一致性、Streams/Quorum 线协议和管理安全。生产化需要协议、存储、复制、流控、安全、运维的长期投入。

下一章讨论 RabbitMQ 与事件驱动架构的未来方向，包括 AMQP 1.0、Kubernetes、Streams、AI 数据管道和标准演进。

---

[上一章：第 24 章 Mini-RabbitMQ 管理面、集群模拟与互联](24-mini-management-cluster-links.md) | [返回目录](../README.md) | [下一章：第 26 章 未来方向：RabbitMQ、Kubernetes 与事件驱动架构](26-future-directions.md)
