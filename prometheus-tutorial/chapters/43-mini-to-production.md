# 第 43 章 从 Mini 到生产：Go 重写、并发与兼容性

## 43.1 哪些设计可以保留

Mini 的分层与语义可以直接迁移：

- discovery → scrape → TSDB → query → rules → notifier；
- 标签/匹配器/选择器语义；
- rate 外推/直方图分位数的算法；
- relabel 动作/锚定；
- 告警状态机；
- API envelope/路径。

需要替换的是**实现**，不是**架构**。

## 43.2 语言与性能

Python → Go 的收益：

- 原生并发（goroutine）替代线程；
- 更低的内存/对象开销；
- 更快的解析/编码；
- 成熟的 HTTP/TSDB 库；
- 静态二进制部署。

但不要"为了 Go 而 Go"：如果只是小规模内部监控，Python 版本 + 真实 Prometheus 更省事。

## 43.3 存储引擎重写

生产级 TSDB 需要：

- postings 索引（不是线性扫描）；
- 流式 chunk 读写（不是一次性物化）；
- mmap/文件缓存；
- 压缩/编码优化；
- 并发安全的 head shard；
- WAL 分页/压缩/修复；
- block 的二进制 index；
- compaction 策略/限流；
- retention 按时间/大小；
- 快照/备份。

Mini 的 JSON/内存实现只适合教学。

## 43.4 查询引擎重写

- 流式 `SeriesSet`；
- 查询取消/超时；
- 内存配额；
- 并行求值（部分函数/聚合）；
- 向量池/对象复用；
- 更完整的 PromQL 函数/类型；
- native histogram；
- 查询统计/慢查询日志。

Mini 的递归 Python 实现便于理解，但每次查询都创建大量对象。

## 43.5 抓取与发现

生产需要：

- scrape pool + 调度器（不是每目标一线程）；
- 并发上限/背压；
- Kubernetes/EC2/Consul 等 SD；
- 完整 TLS/代理/重定向；
- 压缩/内容协商；
- target limit/label limit；
- 服务发现缓存/失败保留；
- 目标健康/指标。

Mini 的 `ScrapeManager` 是单机教学版。

## 43.6 API 与兼容性

要保持：

- `/api/v1/*` 路径/envelope/错误类型；
- PromQL 语义；
- exposition/remote write 协议；
- federation；
- 配置字段。

要补：

- protobuf remote read；
- native histogram/RW2；
- TLS server/认证；
- admin API 的完整语义；
- 多租户（如果需要）。

兼容性测试：

- Grafana 数据源；
- `promtool`/curl 客户端；
- 现有 recording/alerting 规则；
- 现有 dashboard 查询。

## 43.7 规则与告警

- 规则组并发/限流；
- 规则状态持久化（可选）；
- 完整 Go template；
- 完整 promtool 子命令/JUnit 输出（Mini 只实现 `check rules`/`test rules` 子集）；
- Alertmanager 完整协议/批量/重试；
- 告警状态恢复。

Mini 的 notifier/状态机可作为参考实现。

## 43.8 生产化清单

- [ ] 压测：序列/摄入/查询/规则
- [ ] 高可用：双副本/去重/远端
- [ ] 安全：TLS/认证/admin 关闭/网络策略
- [ ] 监控：自监控指标/告警
- [ ] 备份：快照/恢复演练
- [ ] 升级：版本兼容/回滚
- [ ] 容量：磁盘/内存/retention
- [ ] 合规：数据保留/审计/多租户
- [ ] 文档：runbook/架构图/变更流程
- [ ] 测试：单元/集成/e2e/混沌

## 43.9 迁移路径

如果要把 Mini 的某个组件替换成生产实现：

1. 保持接口（Querier/Appender/Notifier）；
2. 先并行运行（shadow）；
3. 对比结果/指标；
4. 切读/切写；
5. 保留回滚。

例如：

- 用真实 Prometheus 替换 Mini 的抓取/TSDB；
- 用 Mimir 替换本地存储；
- 用 Alertmanager 替换 notifier；
- 保留 Mini 作为教学/回归工具。

## 43.10 不要做的事

- 直接把 Mini 部署到生产；
- 用 JSON index 存百万序列；
- 用每目标一线程抓数千目标；
- 忽略 WAL/快照/升级；
- 把"测试全绿"当成"生产就绪"；
- 在生产开启 admin API 且无认证。

## 43.11 Go 并发模型：从线程到 goroutine

Mini 用"每目标一线程 + 队列 + 锁"演示并发。生产实现要换成更细的模型：

```text
discovery manager
  → 每个 SD 一个 goroutine，更新 target 集合
scrape manager
  → 每个 job 一个 scrapePool
  → 每个 target 一个 scrapeLoop goroutine
  → 共享 HTTP client/连接池，限制并发
rule manager
  → 每个 rule group 一个 goroutine
  → 组内顺序求值，组间并发
query engine
  → 每个查询一个 goroutine + context
  → 受 max-concurrency 限制
WAL
  → 写入串行化（或分片锁）
compaction
  → 后台 goroutine，受 IO/并发限制
```

关键模式：

- **context**：查询/抓取/规则求值都可取消；
- **errgroup**：一组 goroutine 的错误聚合；
- **channel**：target 更新、通知队列、WAL 队列；
- **sync.Pool**：复用 label slice、chunk buffer、query 对象；
- **原子/无锁**：计数器、状态位；
- **背压**：队列满时阻塞/丢弃，而不是无限增长。

不要直接照搬"每目标一线程"到 Go：goroutine 虽轻，但每目标一个 HTTP 连接、一个 ticker、一个缓冲区，规模大时仍会耗尽资源。生产实现会复用连接、限制并发、按 job 分片。

## 43.12 内存与对象复用

Go 的内存优化重点：

```text
labels.Labels        不可变、排序、共享底层数组
chunkenc             []byte 缓冲复用，避免每样本分配
query                复用 vector/matrix 切片，控制峰值
scrape               复用 buffer，限制 body size
WAL                  page/buffer 复用，批量写
```

工具：

- `pprof`（heap/cpu/block/mutex）；
- `GOMEMLIMIT`（软内存上限）；
- `GOGC`；
- `runtime/metrics`；
- `expvar`/Prometheus 自监控。

验收指标：

- 每样本分配字节数（allocs/op）；
- GC 频率/暂停；
- RSS 与 `prometheus_tsdb_head_series` 的比值；
- 查询峰值内存。

Python 版本的瓶颈很大一部分来自对象开销：每个 `Sample`、每个 `Labels`、每个 dict 都是独立对象。Go 重写不是"语言更快"，而是**数据布局 + 复用 + 并发**共同作用。

## 43.13 兼容性测试矩阵

生产替代品必须通过：

| 维度 | 测试 | 工具 |
|---|---|---|
| 配置 | 现有 `prometheus.yml` 能加载 | `promtool check config` |
| 规则 | 现有 recording/alerting 规则语义一致 | `promtool test rules` |
| PromQL | 关键查询结果一致 | 差分测试/快照 |
| Exposition | text/OpenMetrics 解析一致 | `promtool check metrics` |
| Remote Write | 1.0/2.0 协议、重试、去重 | 接收端/抓包 |
| Remote Read | protobuf 响应 | 客户端 |
| HTTP API | `/api/v1/*` envelope/字段 | 契约测试 |
| Federation | `/federate` 输出 | 抓取测试 |
| UI | 查询/图表/目标页 | 手工/Playwright |
| Alertmanager | 通知格式/重试 | `amtool`/测试接收器 |

差分测试示例：

```text
同一份 TSDB 数据
  → 旧实现执行查询 Q
  → 新实现执行查询 Q
  → 比较序列、标签、样本、NaN/Inf
```

对浮点结果要定义容差；对顺序要定义排序规则；对错误要定义错误码。

## 43.14 性能预算与基准

在重写前先定预算：

```text
摄入：每秒样本数、每样本 CPU/内存
查询：P99 延迟、并发数、峰值内存
规则：每组求值时间、失败率
启动：WAL replay 时间、ready 时间
存储：压缩比、block 大小、compaction 时长
```

基准方法：

- 固定硬件/内核/文件系统；
- 固定数据分布（序列数、标签基数、样本间隔）；
- 固定查询语料（从生产采样，脱敏）；
- 预热 + 多轮 + 报告分位数；
- 同时记录资源使用，而不只是 QPS；
- 与真实 Prometheus 对比，而不是与 Mini 对比。

验收：

```text
[ ] 摄入达到目标 samples/s，CPU/内存在预算内
[ ] P99 查询延迟达标
[ ] 规则求值不拖垮查询
[ ] WAL replay/启动时间可接受
[ ] 连续 24h 稳定性/无泄漏
```

## 43.15 渐进式迁移

不要"重写完成再切换"。用 strangler 模式：

```text
1. 新实现先做只读 shadow：接收同样的数据，跑同样的查询
2. 对比结果/延迟/资源
3. 切一部分读流量（Grafana 数据源/代理）
4. 切一部分写流量（remote write/双写）
5. 全量切换
6. 保留旧实现作为回滚
```

回滚触发：

- 查询错误率/延迟超阈值；
- 数据丢失/重复；
- 规则漏报/误报；
- 资源耗尽；
- 关键客户投诉。

回滚要能在分钟级完成：数据源、路由、feature flag、DNS 都要预先演练。

## 43.16 生产事故与防御

重写/迁移中常见事故：

| 事故 | 根因 | 防御 |
|---|---|---|
| OOM | 高基数/查询峰值 | 限额、基数治理、压测 |
| WAL 损坏 | 磁盘/断电/并发写 | fsync、单写者、恢复演练 |
| 抓取风暴 | 目标激增/重试 | 并发限制、退避、target limit |
| 规则拖垮查询 | 大范围/高基数规则 | 规则超时、并发限制、预聚合 |
| 数据重复 | 双写/重试 | 去重、幂等、external labels |
| 升级失败 | 格式/配置不兼容 | 快照、灰度、回滚 |
| 删除不彻底 | 副本/备份/远端 | 删除清单、审计 |

每个事故都要有：

- 检测指标/告警；
- runbook；
- 回滚/恢复步骤；
- 复盘与回归测试。

## 43.17 上游代码走读路线

用 Mini 建立语义后，按子系统读上游：

```text
cmd/prometheus/main.go        启动/flag/组件装配
config/                       配置解析/校验
discovery/                    各种 SD
scrape/                       抓取循环/relabel/限制
tsdb/                         head/WAL/block/compaction/querier
tsdb/index/                   索引格式/postings
tsdb/chunkenc/                chunk 编码
promql/                       parser/engine/functions
rules/                        规则管理/状态机
notifier/                     Alertmanager 协议
storage/remote/               remote write/read
web/api/v1/                   HTTP API
web/ui/                       UI
```

阅读顺序建议：

1. `main.go` 看组件如何组装；
2. `scrape/scrape.go` 看一次抓取；
3. `tsdb/head_append.go` 看写入；
4. `tsdb/compact.go` 看压缩；
5. `promql/engine.go` 看求值；
6. `rules/manager.go` 看规则；
7. `storage/remote/write.go` 看 remote write。

每读一块，对照 Mini 的对应实现和测试，问三个问题：

- 它解决了什么失败模式？
- 它的并发/内存边界在哪？
- 如果我要重写，最小正确实现是什么？

## 43.18 教学实现与生产系统的距离

Mini 证明的是：

- 语义可以被压缩成可读代码；
- 关键失败模式可以被测试覆盖；
- 分层/接口可以迁移。

Mini 不证明的是：

- 性能/规模；
- 字节级兼容；
- 生产级安全/多租户/HA；
- 长期维护/升级。

把 Mini 当"可执行的教科书"，而不是"待上线的产品"。真正的生产系统需要：

- 严格的兼容性测试；
- 压测/容量规划；
- 安全/合规；
- 监控/告警/值班；
- 升级/回滚/备份；
- 团队与流程。

## 本章小结

- Mini 的架构/语义可迁移，实现需要 Go/生产级重写。
- 存储/查询/抓取/API 都要替换为流式/索引/并发实现。
- 兼容性是迁移的核心：API/PromQL/协议/配置。
- 生产化清单覆盖压测/HA/安全/监控/备份/升级。
- 不要直接部署 Mini；把它当教学/回归工具。

## 延伸阅读

- Prometheus 源码结构。
- Thanos/Mimir/VictoriaMetrics 的架构文档。
- 本教程第 13～24 章（生产主题）与附录 AB（上游源码导读，按子系统给出阅读路线）。
