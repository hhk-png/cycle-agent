# 附录 M 插件体系、扩展开发与二次开发

RabbitMQ 的能力边界有很大一部分由插件决定：MQTT/STOMP 接入、Federation/Shovel、Prometheus、LDAP/OAuth、一致性哈希、延迟消息，历史上都以插件形式存在。理解插件，既能让你正确选型和运维，也能避免“为了一个业务需求，把 broker 改成一个不可维护的分支”。

本附录回答四个问题：

1. RabbitMQ 的插件体系是怎么组织的？
2. 常见插件分别解决什么问题，哪些已经进入内核、哪些已经退出历史？
3. 什么情况下应该写插件，什么情况下不该写？
4. 如果确实要写，工程上要准备什么？

## 插件不是“配置开关”

RabbitMQ 本身是一个 Erlang/OTP 应用集合。插件是一个独立的 OTP application，放在插件目录中，由 `rabbitmq-plugins` 启用后加载到 broker VM 里。这意味着：

- 插件和 broker 共享同一个 Erlang VM、调度器、内存和崩溃域；
- 插件可以注册交换机类型、认证后端、管理 API、boot step、指标；
- 插件代码拥有 broker 进程内的全部权限，**没有沙箱**；
- 插件版本通常只针对一个 RabbitMQ 系列，升级 broker 必须同步验证插件。

所以“装插件”不是改一个配置项，而是往生产 broker 里加载第三方代码。安全评审、版本锁定、回滚方案一个都不能少。

## 插件分类地图

| 类别 | 代表插件 | 作用 | 现状/注意 |
|---|---|---|---|
| 协议接入 | `rabbitmq_mqtt`、`rabbitmq_stomp`、`rabbitmq_web_mqtt`、`rabbitmq_web_stomp` | MQTT/STOMP/WebSocket | 仍为插件；4.4 STOMP 原生化 |
| 协议核心 | AMQP 0-9-1、AMQP 1.0 | 核心协议 | 4.0 起 AMQP 1.0 核心化，`rabbitmq_amqp1_0` 变 no-op |
| 管理/监控 | `rabbitmq_management`、`rabbitmq_prometheus`、`rabbitmq_top`、`rabbitmq_event_exchange` | UI、指标、进程观察、事件 | 生产常用；注意指标基数 |
| 复制/互联 | `rabbitmq_federation`、`rabbitmq_shovel`、`rabbitmq_stream` | 跨 broker 复制、转发、流 | Federation/Shovel 是插件；Stream 有核心组件 |
| 交换机类型 | `rabbitmq_consistent_hash_exchange` | 一致性哈希 | 插件；4.3+ 另有内置 `x-modulus-hash` |
| 延迟消息 | `rabbitmq_delayed_message_exchange` | `x-delayed-message` | **已归档**，依赖 Mnesia，4.3+ 不可用 |
| 认证/授权 | `rabbitmq_auth_backend_ldap`、`rabbitmq_auth_backend_oauth2`、`rabbitmq_auth_backend_http`、`rabbitmq_auth_backend_cache` | LDAP/OAuth/HTTP/缓存 | 企业环境常用；注意 token/证书轮换 |
| 节点发现 | `rabbitmq_peer_discovery_consul`、`_etcd`、`_k8s`、`_aws` | 集群自动发现 | K8s 多用 Operator，不必手写发现 |
| 证书/信任 | `rabbitmq_trust_store` | 动态信任库/证书轮换 | 与 mTLS 配合 |
| 其它 | `rabbitmq_sharding` 等 | 历史/实验能力 | 选型前确认维护状态与目标版本 |

这张表不是“全都该装”。生产原则是：**只装业务确实需要、且有人负责升级的插件**。每多一个插件，就多一个 CVE 面、升级阻塞点和故障域。

## 插件的生命周期

### 查看与启用

```bash
rabbitmq-plugins list -v
rabbitmq-plugins list -e -m
rabbitmq-plugins enable rabbitmq_management rabbitmq_prometheus
rabbitmq-plugins disable rabbitmq_top
rabbitmq-plugins directories -s
```

要点：

- `enable` 默认要求 broker 运行；离线镜像构建可用 `--offline`；
- 启用/禁用会修改节点的 enabled_plugins 文件，滚动升级时要保证所有节点一致；
- 插件启用顺序由依赖关系决定，不要手工改启动脚本绕过；
- 禁用插件可能让已有拓扑/客户端失效（例如禁用 MQTT 后 1883 不再监听）；
- 云厂商托管通常只允许白名单插件，选型前先确认。

### 插件目录与打包

插件以 `.ez` 归档或展开目录存在。`rabbitmq-plugins directories -s` 会显示插件目录。自建镜像时常见做法：

```dockerfile
FROM rabbitmq:4.4-management
COPY plugins/rabbitmq_consistent_hash_exchange-*.ez /opt/rabbitmq/plugins/
RUN rabbitmq-plugins enable --offline rabbitmq_consistent_hash_exchange
```

这段示例只说明打包方式。生产镜像要固定 RabbitMQ 版本、插件版本和校验和，不要用 `latest`；像延迟交换机这种依赖 Mnesia 的插件在 4.3+ 已不可用，不能照搬旧镜像。

### 版本兼容

插件与 broker 的兼容性不是“Erlang 能加载就行”：

- 插件通常针对一个 RabbitMQ 系列编译；
- 依赖的 internal API、Mnesia 表、feature flag 可能在新版本变化；
- 4.3 移除 Mnesia 后，依赖 Mnesia 的插件直接失效；
- 插件升级可能要求先升级 broker，或反之。

升级检查清单里要有“插件兼容矩阵”这一项，而不是升级后才看日志。

## 4.3/4.4 给插件生态带来的两次冲击

### 1. Mnesia 移除与延迟插件归档

`rabbitmq_delayed_message_exchange` 把延迟消息存在 Mnesia 表中。4.3 移除 Mnesia 后，插件无法工作，官方仓库已归档。替代方案：

- 4.3+：Quorum 原生延迟重试（`x-delayed-retry-*`）；
- 4.4+：Quorum 原生定时投递（`x-opt-delivery-time`）；
- 跨版本：TTL + DLX 或外部调度；
- 商业版：Tanzu RabbitMQ 的 delayed queue。

这不是“某个插件运气不好”，而是平台演进的必然：**依赖内部存储实现的插件，会随着存储引擎替换而失效**。写插件时要尽量依赖公开 behaviour，而不是私有表。

### 2. Khepri 成为唯一元数据存储

4.3 起 Khepri 是唯一元数据存储，Mnesia 被完全移除。影响：

- 使用 Mnesia 存插件状态的插件需要迁移到 Khepri 或外部存储；
- 元数据写入要求集群多数派在线，插件的管理面操作也要接受这个约束；
- `cluster_partition_handling` 等旧配置不再生效，插件的故障处理逻辑要按 Raft 语义重写。

## 什么时候该写插件

先做一个判断：**这个需求是“broker 语义”还是“业务逻辑”？**

| 需求 | 更合适的做法 | 原因 |
|---|---|---|
| 新的路由算法/交换机类型 | 插件 | 必须参与 broker 的路由决策 |
| 新的认证/授权后端 | 插件 | 必须接入连接握手和权限检查 |
| 新的管理 API/指标 | 插件 | 必须挂到管理面 |
| 新的协议接入 | 插件 | 必须实现协议监听和帧解析 |
| 订单状态机、重试策略、对账 | 应用代码 | 业务逻辑不该进 broker |
| 消息格式转换 | 客户端/桥接服务 | 插件转换难以测试和升级 |
| 限流/配额 | 客户端 + 网关/代理 | broker 插件做业务限流容易变成单点 |
| 审计日志 | 插件或消费端 | 取决于是否需要 broker 级全量捕获 |

写插件的合理理由通常只有一个：**这个能力必须在 broker 进程内、对所有客户端透明地生效**。如果应用层能解决，优先应用层；如果边车/代理能解决，优先边车；插件是最后手段。

## 插件开发的基本结构

RabbitMQ 插件是 Erlang/OTP application。一个最小插件包含：

```text
my_plugin/
├── src/
│   ├── my_plugin.app.src
│   ├── my_plugin_app.erl
│   └── my_plugin_sup.erl
├── Makefile
└── README.md
```

`.app.src` 至少声明应用名、版本、模块和依赖：

```erlang
{application, my_plugin,
 [{description, "Example RabbitMQ plugin"},
  {vsn, "0.1.0"},
  {modules, [my_plugin_app, my_plugin_sup]},
  {registered, [my_plugin_sup]},
  {applications, [kernel, stdlib, rabbit]},
  {mod, {my_plugin_app, []}},
  {env, []}]}.
```

常见扩展点：

| 扩展点 | behaviour/机制 | 用途 |
|---|---|---|
| 交换机类型 | `rabbit_exchange_type` | 自定义路由算法 |
| 认证 | `rabbit_authn_backend` | 校验用户名/密码/token |
| 授权 | `rabbit_authz_backend` | vhost/资源/topic 权限 |
| 管理 API | `rabbit_mgmt_extension` | 增加 REST 端点/页面 |
| 启动步骤 | `rabbit_boot_step` | 在 broker 启动序列中插入初始化 |
| 指标 | Prometheus 插件扩展/内部指标 | 暴露自定义指标 |

具体回调的函数签名随版本变化，写代码时以目标版本的 behaviour 定义和官方插件样例为准，不要照抄旧博客。

### 交换机类型插件的最小骨架

```erlang
-module(my_exchange_type).
-behaviour(rabbit_exchange_type).

%% 常见回调（不同大版本会增删，签名以目标版本的 behaviour 定义为准）：
%%   description/0            类型元数据
%%   serialise_events/0       绑定事件是否需要序列化
%%   route/3                  路由（旧 route/2 已弃用，优先 route/3）
%%   validate/1               交换机声明参数校验
%%   validate_binding/2       绑定参数校验
%%   create/2 delete/3        生命周期
%%   add_binding/3 remove_bindings/3  绑定维护
%%   policy_changed/2         策略变更
%%   assert_args_equivalence/2 声明等价性
%%
%% 路由函数位于每条消息的关键路径上：
%% 不要做阻塞 IO、不要访问外部数据库、不要做长计算。
```

这只是一个示意：真实插件要处理绑定参数、策略等价性、错误返回和并发安全。`x-` 前缀是社区插件交换机类型的惯例，但类型名最终由注册表和目标版本约定决定。不要照抄旧博客里的回调列表——RabbitMQ 在 3.x→4.x 之间调整过 behaviour 的签名。

### 注册扩展点

插件通常在 application start 或 boot step 中注册：

```erlang
rabbit_registry:register(exchange, <<"x-my-exchange">>, my_exchange_type).
```

注册时机很重要：太早 broker 还没准备好，太晚拓扑已经可以声明。用 boot step 声明依赖关系，比在 `start/2` 里硬编码顺序可靠。

## 管理面扩展

管理插件提供 `rabbit_mgmt_extension` behaviour，可以挂载自定义 REST 端点。典型用途：

- 暴露插件内部状态；
- 提供运维操作（重放、清理、重建）；
- 增加自定义页面。

注意：

- 管理 API 是高权限面，新增端点必须做认证/授权；
- 不要让 GET 产生副作用；
- 返回大数据要分页；
- 指标标签基数要控制，否则 Prometheus 会被拖垮；
- 端点路径要避免与官方 API 冲突。

## 插件工程化清单

写插件不是“编译通过就完了”。生产插件至少要有：

1. **兼容矩阵**：支持哪些 RabbitMQ/Erlang 版本；
2. **单元/集成测试**：用 `common_test` 或官方测试工具，覆盖正常和失败路径；
3. **升级/回滚测试**：插件自身、broker、拓扑三层；
4. **性能基线**：路由/认证插件在消息关键路径上，必须测吞吐和尾延迟；
5. **安全评审**：插件是 broker 进程内代码，等同 root 权限；
6. **可观测性**：日志、指标、错误分类；
7. **打包与校验**：固定版本、校验和、镜像可复现；
8. **文档**：安装、配置、升级、卸载、故障处理；
9. **退出策略**：插件停用后拓扑/数据如何迁移。

缺少任何一项，插件都会变成“只有作者敢升级”的技术债。

## Mini-RabbitMQ 的扩展点

Mini 没有 Erlang 插件系统，但保留了同构的扩展点，便于理解“插件注册”这件事：

- `mini_rabbitmq/exchange.py` 的 `ExchangeTypeRegistry` 负责注册交换机类型；
- `Exchange` 子类通过 `type_name` 声明类型名；
- `default_registry()` 注册 direct/fanout/topic/headers 和一致性哈希；
- `VHost` 通过 registry 创建交换机，未知类型返回 `InvalidArgument`。

```python
from mini_rabbitmq.exchange import Exchange, ExchangeTypeRegistry

class AuditExchange(Exchange):
    type_name = "x-audit"
    # 覆写 route/绑定逻辑

registry = ExchangeTypeRegistry()
registry.register(AuditExchange)
```

这个设计与 RabbitMQ 的 `rabbit_registry` 思路一致：**类型名 → 实现类**。区别在于 Mini 是单进程 Python、没有热加载、没有插件隔离、没有 boot step；它适合演示扩展点，不适合承载生产插件。

Mini 没有实现的插件相关能力：

- 插件发现/打包/启停；
- 动态加载与版本兼容；
- 认证后端插件；
- 管理面扩展；
- 插件级指标/日志；
- 插件故障隔离（一个插件崩溃会拖垮整个 broker，Mini 也是如此）。

## 二次开发的替代路径

在决定写插件前，先比较四条路径：

| 路径 | 适用 | 代价 |
|---|---|---|
| 应用层库/框架 | 业务重试、幂等、Outbox、Saga | 每个服务都要接入，但可控、可测试 |
| 边车/代理 | 协议转换、限流、审计、灰度 | 多一跳网络，运维面增加 |
| 外部调度/编排 | 延迟、定时、长事务 | 引入组件和一致性边界 |
| RabbitMQ 插件 | 路由/认证/协议/管理面语义 | 开发成本高、升级耦合、安全风险大 |

一个常见误区是“用插件解决业务问题，避免改应用”。短期看省事，长期看把业务逻辑固化在 broker 里：无法独立发布、无法单元测试、升级 broker 时被插件卡住。**插件应该扩展 broker 的通用语义，而不是承载某个业务域的规则。**

## 与真实 RabbitMQ 的对照

| 能力 | Mini | 真实 RabbitMQ |
|---|---|---|
| 交换机类型注册 | 有（进程内 registry） | 插件 + `rabbit_registry` |
| 插件打包/启停 | 无 | `.ez` + `rabbitmq-plugins` |
| 认证后端扩展 | 无（内置 PLAIN/ACL） | `rabbit_authn_backend`/`rabbit_authz_backend` |
| 管理 API 扩展 | 无 | `rabbit_mgmt_extension` |
| 热加载/隔离 | 无 | 有限（同一 VM，无沙箱） |
| 版本兼容 | 随教程代码 | 插件/broker/Erlang 三方矩阵 |

## 小结

RabbitMQ 的插件体系是它生态广度的来源，也是运维复杂度的来源。插件运行在 broker VM 内、拥有完整权限、与版本强耦合；4.3 的 Mnesia 移除让一批依赖内部存储的插件退出历史。写插件前先判断需求是否真的属于 broker 语义；如果属于，按 Erlang/OTP 的 behaviour 和 boot step 规范实现，并把兼容矩阵、测试、安全评审和退出策略当成一等公民。Mini 用 `ExchangeTypeRegistry` 演示了同一思想，但不提供生产级插件隔离。

---

[返回目录](../README.md) | [上一章：附录 L 性能基准、容量规划与 PerfTest 实验手册](L-performance-benchmark-playbook.md) | [下一章：附录 N Erlang/OTP 运行时、进程模型与调优](N-erlang-runtime.md)
