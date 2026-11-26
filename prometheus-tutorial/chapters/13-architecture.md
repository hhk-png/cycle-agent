# 第 13 章 架构总览：单机 Prometheus 的组件与边界

## 13.1 一个二进制，多个子系统

Prometheus 是单进程应用，但内部边界清晰：

```text
cmd/prometheus
  main.go
    ├── config       配置加载/校验/热更新
    ├── discovery    服务发现管理器
    ├── scrape       scrape manager + scrape pools
    ├── tsdb         head/WAL/blocks/compaction
    ├── rules        rule manager（recording/alerting）
    ├── notifier     Alertmanager 客户端
    ├── web          HTTP API、UI、federation、remote write receiver
    └── remote       remote write/read
```

各子系统通过明确的接口交互：

- discovery → scrape：目标列表（标签集合）；
- scrape → tsdb：`Appender`（批量样本）；
- tsdb → query：`Querier`（按 matcher/time range 选择序列）；
- rules → tsdb/query：规则求值读写；
- notifier → Alertmanager：HTTP。
- web → query/tsdb/rules：API。

注意"采集面"不在这个二进制里：node_exporter、blackbox_exporter、数据库/中间件 exporter 都是独立进程，Prometheus 只负责发现、抓取和解释它们的 `/metrics`。这条边界决定了故障域（exporter 挂了不等于 Prometheus 挂了）和权限模型（抓取账号只需只读）。采集面的分类、指标语义与告警见附录 AA。

Mini-Prometheus 的模块划分与之一一对应，只是把并发模型简化为线程 + 锁。

## 13.2 启动顺序

```text
解析 flags
  → 加载并校验配置
  → 打开 TSDB（加载 WAL、block、checkpoint）
  → 初始化 discovery
  → 初始化 scrape manager
  → 初始化 rule manager
  → 初始化 notifier
  → 启动 HTTP server
  → 启动 scrape/rules 调度
```

关键点：

- TSDB 打开失败（锁、损坏）时，Prometheus 拒绝启动；
- 配置错误在启动时暴露，而不是运行时；
- `/-/ready` 只有在 TSDB 完成 WAL replay 后才返回 200；
- `/-/healthy` 只表示进程存活，不代表抓取/规则正常。

## 13.3 进程模型

- 每个 scrape pool 有调度 goroutine；
- 每个目标有独立的抓取循环（受并发限制）；
- 查询请求在 HTTP handler 中执行，受 `--query.max-concurrency` 限制；
- 规则组按 interval 串行求值；
- compaction 在后台运行；
- WAL 写入在 appender 路径上同步完成（默认 fsync 策略可配置）。

单进程意味着：

- 查询和抓取共享 CPU/内存/磁盘 IO；
- 一次昂贵的查询可能拖慢抓取；
- 规则的慢查询会影响所有规则组；
- 进程崩溃会丢失未 fsync 的 WAL 尾部（通常可恢复）。

## 13.4 本地存储的定位

Prometheus 的本地 TSDB 是"近期数据 + 查询缓存"，不是长期归档：

- 默认 retention 15d；
- block 2h；
- 单机存储受磁盘和内存限制；
- 查询只覆盖本地数据（除非配置 remote read）；
- 长期化通过 remote write 到 Thanos/Mimir/VictoriaMetrics 等。

这个定位决定了部署模式：

- 边缘/集群内 Prometheus 负责抓取和短期查询；
- 中心存储负责长期、全局、跨集群查询；
- Grafana 指向中心查询层，而不是每个 Prometheus。

## 13.5 配置热更新

`SIGHUP` 或 `POST /-/reload` 会：

1. 读取并校验新配置；
2. 重建 discovery/scrape/rules/notifier；
3. 成功则原子替换；失败则保留旧配置并记录 `prometheus_config_last_reload_successful=0`。

注意：

- 抓取中的目标会被停止/重启，可能产生短暂缺口；
- 规则组重建会重置告警状态（pending 计时重新开始）；
- 配置里的 `external_labels` 变化会影响 remote write/告警身份；
- 生产建议用配置管理 + 灰度 + 回滚，而不是手工 curl。

Mini 的 `/-/reload` 实现类似，但会重建 scrape 目标并写 stale。

## 13.6 端口与端点

默认 `:9090`：

```text
/            UI（重定向到 /graph）
/graph       PromQL 查询界面
/api/v1/*    HTTP API
/metrics     自身指标
/federate    联邦
/-/healthy   健康
/-/ready     就绪
/-/reload    重载（需 --web.enable-lifecycle）
```

远程写接收（如启用）：

```text
/api/v1/write
```

管理 API（需 `--web.enable-admin-api`）：

```text
/api/v1/admin/tsdb/snapshot
/api/v1/admin/tsdb/delete_series
/api/v1/admin/tsdb/clean_tombstones
```

## 13.7 失败域

| 组件 | 故障表现 | 影响 |
|---|---|---|
| discovery | 目标列表为空/过期 | 抓取停止，up 消失 |
| scrape | 目标失败 | 单目标 up=0，旧序列写 stale |
| TSDB | 磁盘满/损坏/锁冲突 | 写入失败，查询可能降级 |
| query | 慢查询/OOM | API 超时，规则延迟 |
| rules | 求值错误 | 记录规则缺失，告警不更新 |
| notifier | Alertmanager 不可达 | 告警丢失/延迟 |
| web | 端口/证书 | API/UI 不可用 |

设计监控系统时，要监控 Prometheus 自己（完整指标/告警清单见附录 U）：

```promql
up{job="prometheus"}
prometheus_tsdb_head_series
prometheus_tsdb_storage_blocks_bytes
prometheus_rule_evaluation_failures_total
prometheus_notifications_errors_total
prometheus_target_scrape_pool_sync_total
process_resident_memory_bytes
```

## 13.8 单机容量边界

粗略经验（因指标基数/查询模式差异很大）：

- 活跃序列：几十万到几百万；
- 样本摄入：10 万～100 万 samples/s（取决于硬件与配置）；
- 内存：每百万活跃序列约数 GB（head index + chunks + postings）；
- 磁盘：每样本 1～2 字节（压缩后），加索引开销；
- 查询：受 `--query.max-samples`（默认 5000 万）和并发限制。

超过边界时：

- 垂直扩展（更多 CPU/内存/SSD）；
- 功能分片（按集群/业务拆 Prometheus）；
- 抓取分片（hashmod）；
- agent 模式 + 中心存储；
- 使用 Mimir/Thanos/VictoriaMetrics 等水平扩展方案。

## 13.9 Prometheus 不是什么

- **不是集群数据库**：没有分片/副本/一致性协议。
- **不是多租户系统**：单机没有租户隔离。
- **不是日志/追踪存储**：只处理指标。
- **不是长期归档**：retention 默认 15 天。
- **不是告警路由系统**：路由在 Alertmanager。

理解这些边界，才能正确设计架构，而不是把所有需求压到一个 Prometheus 上。

## 13.10 Mini 的架构映射

| 真实组件 | Mini 模块 |
|---|---|
| discovery | `scrape/discovery.py` |
| scrape manager | `scrape/manager.py` |
| TSDB | `tsdb/tsdb.py` + `head.py` + `block.py` + `wal.py` |
| query engine | `promql/engine.py` |
| rules | `rules/manager.py` |
| notifier | `rules/notifier.py` |
| web/API | `api/server.py` |
| remote write/read | `remote.py` + `remote_client.py` |
| config | `config.py` |

Mini 用一个 `PrometheusApp` 组装这些组件，对应真实 Prometheus 的 `main.go`。

## 本章小结

- Prometheus 是单进程、多子系统，组件边界清晰但共享资源。
- 本地 TSDB 是近期数据层，不是长期归档；长期化靠 remote write/中心存储。
- 热更新会重建目标与规则，可能造成短暂缺口和告警状态重置。
- 单机有明确容量边界；扩展靠垂直、分片、agent、中心存储。
- 监控 Prometheus 自身是生产部署的必选项。

## 延伸阅读

- Prometheus 官方 "Architecture" 与 "Storage"。
- Prometheus 源码 `cmd/prometheus/main.go`。
- 本教程第 14～19 章（存储、查询、远程、HA）。
