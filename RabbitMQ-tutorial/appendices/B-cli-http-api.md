# 附录 B CLI 与 HTTP 管理 API 参考

## Mini-RabbitMQ CLI

```bash
python -m mini_rabbitmq --help
python -m mini_rabbitmq version
python -m mini_rabbitmq diagnose
python -m mini_rabbitmq serve --no-persistence --port 5673 --management-port 15673
```

`serve` 选项：

| 选项 | 默认 | 说明 |
|---|---|---|
| `--host` | 127.0.0.1 | 监听地址 |
| `--port` | 5672 | AMQP 端口，0 表示临时端口 |
| `--management-port` | 15672 | 管理 HTTP 端口，0 临时，-1 关闭 |
| `--data-dir` | `var/data` | 快照目录 |
| `--no-persistence` | 关闭 | 不写快照 |
| `--fsync` | 关闭 | 快照写入后 fsync |
| `--node-name` | mini@localhost | 节点名 |
| `--heartbeat` | 60 | 建议心跳秒数 |
| `--frame-max` | 131072 | 最大帧 |
| `--channel-max` | 2047 | 最大信道 |
| `--tls-cert` / `--tls-key` | 无 | 启用 TLS |
| `--username` / `--password` | guest/guest | 创建/更新用户 |
| `--log-level` | INFO | 日志级别 |

示例：

```bash
# 临时端口 + 无持久化，适合 CI
python -m mini_rabbitmq serve --no-persistence --port 0 --management-port 0

# 持久化到 var/data，启用 fsync
python -m mini_rabbitmq serve --fsync

# TLS
python -m mini_rabbitmq serve --tls-cert server.crt --tls-key server.key
```

Mini 没有 `rabbitmqctl`/`rabbitmq-diagnostics` 等完整运维命令；管理通过 HTTP API 和 Python API。

## Mini HTTP API

默认地址 `http://127.0.0.1:15673`，Basic Auth（guest/guest）。guest 仅限 loopback。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/overview` | 节点/对象/队列总计、listeners、metrics |
| GET | `/api/whoami` | 当前用户与 tags |
| GET | `/api/connections` | AMQP 连接 |
| GET | `/api/channels` | 信道 |
| GET | `/api/vhosts` | 有权限的 vhost |
| GET | `/api/nodes` | 节点状态 |
| GET | `/api/queues` | 全部队列 |
| GET | `/api/queues/{vhost}` | 某 vhost 队列 |
| GET | `/api/queues/{vhost}/{name}` | 队列详情 |
| POST | `/api/queues/{vhost}/{name}/purge` | 清空 |
| DELETE | `/api/queues/{vhost}/{name}` | 删除 |
| GET | `/api/exchanges` | 全部交换机 |
| GET | `/api/exchanges/{vhost}` | 某 vhost 交换机 |
| GET | `/api/exchanges/{vhost}/{name}` | 交换机详情 |
| POST | `/api/exchanges/{vhost}/{name}/publish` | 管理面发布 |
| GET | `/api/bindings` | 全部绑定 |
| GET | `/api/bindings/{vhost}` | 某 vhost 绑定 |
| GET | `/api/policies` | 全部策略 |
| GET | `/api/policies/{vhost}` | 某 vhost 策略 |
| GET/PUT/DELETE | `/api/policies/{vhost}/{name}` | 策略 |
| GET | `/api/health/checks/alarms` | 告警健康检查 |
| GET | `/metrics` | Prometheus 文本 |

路径中的 `/` vhost 要编码为 `%2F`。例如：

```bash
curl -u guest:guest http://127.0.0.1:15673/api/overview
curl -u guest:guest http://127.0.0.1:15673/api/queues/%2F/orders
curl -u guest:guest -X POST http://127.0.0.1:15673/api/queues/%2F/orders/purge
curl -u guest:guest http://127.0.0.1:15673/metrics
```

管理面发布：

```bash
curl -u guest:guest -X POST \
  -H 'Content-Type: application/json' \
  -d '{"routing_key":"order.created","payload":"hello","payload_encoding":"string","properties":{"delivery_mode":2}}' \
  http://127.0.0.1:15673/api/exchanges/%2F/orders/publish
```

策略：

```bash
curl -u guest:guest -X PUT \
  -H 'Content-Type: application/json' \
  -d '{"pattern":"^orders","definition":{"max-length":10000},"apply-to":"queues","priority":1}' \
  http://127.0.0.1:15673/api/policies/%2F/limits
```

## 真实 RabbitMQ CLI

常用命令（具体输出/选项随版本变化）：

```bash
rabbitmqctl status
rabbitmqctl cluster_status
rabbitmqctl list_users
rabbitmqctl list_vhosts
rabbitmqctl list_queues -p /app name messages consumers
rabbitmqctl list_connections user vhost channels state
rabbitmqctl list_channels connection number consumer_count
rabbitmqctl list_permissions -p /app
rabbitmqctl list_policies -p /app
rabbitmqctl export_definitions /backup/definitions.json
rabbitmqctl import_definitions /backup/definitions.json
rabbitmq-diagnostics -q ping
rabbitmq-diagnostics status
rabbitmq-diagnostics memory_breakdown
rabbitmq-diagnostics check_port_connectivity
rabbitmq-plugins list
rabbitmq-plugins enable rabbitmq_management
rabbitmq-queues quorum_status <queue>
```

创建用户/vhost/权限：

```bash
rabbitmqctl add_user app 'change-me'
rabbitmqctl add_vhost /app
rabbitmqctl set_permissions -p /app app '^app\.' '^app\.' '^app\.'
rabbitmqctl set_user_tags app management
```

## 真实 RabbitMQ HTTP API

默认 15672，常见端点与 Mini 类似但更丰富：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/overview` | 概览 |
| GET | `/api/connections` | 连接 |
| GET | `/api/channels` | 信道 |
| GET | `/api/queues` | 队列 |
| GET | `/api/queues/{vhost}/{name}` | 队列详情 |
| GET | `/api/exchanges` | 交换机 |
| GET | `/api/bindings` | 绑定 |
| GET | `/api/nodes` | 节点 |
| GET | `/api/policies` | 策略 |
| GET | `/api/health/checks/alarms` | 健康 |
| GET | `/metrics` | Prometheus（插件） |
| POST | `/api/queues/{vhost}/{name}/purge` | 清空 |
| DELETE | `/api/queues/{vhost}/{name}` | 删除 |
| POST | `/api/exchanges/{vhost}/{name}/publish` | 发布 |

真实 API 支持分页、过滤、列选择、审计等；生产监控优先 Prometheus 指标，避免高频轮询管理 API。

## 常见状态码

| 状态 | 含义 |
|---|---|
| 200 | 成功 |
| 201 | 创建成功（policy） |
| 204 | 删除成功（可能无 body） |
| 400 | 请求体/参数错误 |
| 401 | 未认证 |
| 403 | 无权限 |
| 404 | 对象不存在 |
| 405 | 方法不允许 |
| 500 | 内部错误 |
| 503 | 健康检查失败 |

## 与官方工具的差异

Mini 的 CLI/API 是教学子集：

- 没有完整的 `rabbitmqctl`；
- 没有分页/过滤/列选择；
- 没有审计/HTTPS/OAuth；
- 没有多节点聚合；
- 没有 definitions 导入/导出；
- 策略/队列参数支持有限。

生产请使用官方 RabbitMQ 工具，并以目标版本文档为准。

---

[返回目录](../README.md) | [上一章：附录 A AMQP 0-9-1 方法、字段与错误码速查](A-amqp-method-reference.md) | [下一章：附录 C 配置、参数与功能矩阵](C-configuration-feature-matrix.md)
