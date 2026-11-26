# 第 31 章 Mini-Prometheus 设计：目标、边界与模块

## 31.1 为什么写一个 Mini

目标不是"重写 Prometheus"，而是把关键机制压缩到可审计的代码里：

- 能跑：一条命令启动，抓取、查询、告警、API 全部可用。
- 能读：每个模块对应教程的一章，代码量可控。
- 能测：核心语义有测试，边界/失败有覆盖。
- 能对照：与真实 Prometheus 的差异明确记录，不假装兼容。

非目标：

- 百万序列性能；
- 字节级 TSDB 兼容；
- 完整 Kubernetes SD；
- 完整 Alertmanager；
- 多租户/HA；
- 原生直方图；
- OTLP/protobuf、完整 OTLP 翻译（Mini 只做 JSON 子集）。

## 31.2 依赖与运行环境

- Python 3.11+；
- PyYAML（配置/规则）；
- 标准库：`http.server`、`ssl`、`urllib`、`zlib`、`struct`、`threading`、`queue`、`json`；
- pytest（测试）。

没有 FastAPI/Flask/prometheus_client，避免"用库代替理解"。

## 31.3 模块图

```text
mini_prometheus/
  __main__.py        CLI
  app.py             assembly: config -> tsdb -> scrape -> rules -> api
  config.py          YAML -> dataclasses + validation
  model.py           Sample/TimeSeries/errors/metadata
  labels.py          Labels/Matcher
  exposition.py      text/OpenMetrics parser + writer
  remote.py          protobuf/snappy codecs
  remote_client.py   outgoing remote write queue
  otlp.py            OTLP/JSON -> Prometheus translation
  promtool.py        check rules / test rules runner
  util/
    timeutil.py      duration/timestamp/stale NaN
    ulid.py          block IDs
  promql/
    lexer.py parser.py ast.py engine.py functions.py value.py
  tsdb/
    chunkenc.py head.py wal.py block.py tsdb.py
  scrape/
    discovery.py relabel.py manager.py
  rules/
    manager.py notifier.py
  api/
    server.py
```

依赖方向：

```text
api → rules → promql → tsdb
api → scrape → tsdb
otlp → labels/model
promtool → rules → promql → tsdb
app → all（含 otlp）；api 通过 app.otlp 调用接收器
```

底层模块不反向依赖 API；`promql` 只依赖 `labels/model/util`。

## 31.4 数据流

```text
YAML config
  → ScrapeConfig
  → DiscoveryManager.discover()
  → relabel_target()
  → ScrapeManager.scrape_once()
      → fetch HTTP
      → parse_exposition()
      → merge target labels
      → metric_relabel
      → TSDB.append_batch()
          → Head.append()
          → WAL.log_series/log_samples()
          → watchers (remote writer)
  → Engine.query()
      → parser → AST
      → selector → TSDB.select()
      → functions/aggregation → Value
  → API JSON
```

规则：

```text
RuleManager.evaluate()
  → Engine.query(expr, now)
  → recording: TSDB.append_batch()
  → alerting: state machine → notifier.enqueue()
```

## 31.5 并发模型

- TSDB：`RLock` 保护 head/WAL/block 列表；
- Head：独立 `RLock`；
- ScrapeManager：每目标一个线程 + `_targets` 锁；
- RuleManager：一个调度线程（或手动 evaluate）；
- HTTP：`ThreadingHTTPServer`，每请求一个线程；
- Notifier/RemoteWriter：队列 + 后台 worker；
- 查询：读 TSDB 快照（block 不可变，head 加锁）。

没有 asyncio；线程 + 锁足以演示，且便于在 Windows 上运行。

## 31.6 确定性

测试/示例不依赖墙钟：

- `scrape_once(target, timestamp=...)` 显式传时间；
- `RuleManager.evaluate(now)` 显式传时间；
- `TSDB.compact(now=...)` 显式传时间；
- 端口用 0 让 OS 分配；
- 临时目录隔离。

只有 live e2e 测试使用线程和真实时间，并配有超时轮询。

## 31.7 工程约定

- 所有时间戳毫秒整数；
- 所有标签用 `Labels`（不可变、排序、空值丢弃）；
- 错误类型：`PrometheusError`/`ParseError`/`ConfigError`/`StorageError`；
- 配置校验在加载期，不在运行期；
- 公开 API 用 dataclass；
- 模块内不 print，CLI 负责输出；
- 测试用 pytest fixture 隔离数据目录。

## 31.8 与真实 Prometheus 的差异

| 方面 | Mini | 真实 Prometheus |
|---|---|---|
| 语言 | Python | Go |
| 性能 | 小规模 | 生产级 |
| 存储格式 | JSON index + 自定义 chunk | 二进制 index/chunk |
| WAL | JSON 记录 + CRC | 分页二进制 |
| PromQL | 子集（75 个函数 + 14 个聚合） | 完整 |
| Native histogram | 不支持 | 稳定（3.8 起），客户端/后端支持不一 |
| OTLP | JSON 接收（名称/单位/delta 子集） | protobuf/JSON、完整翻译与 NHCB |
| K8s SD | 不支持 | 支持 |
| Alertmanager | lite | 完整 |
| HA/多租户 | 不支持 | 需外部系统 |
| TLS server | 不支持（客户端 TLS 支持） | 支持 |
| Remote write | 1.0 子集 | 1.0/2.0 |

差异不是缺陷清单，而是教学边界；每章会明确"Mini 做到了什么/没做什么"。

## 31.9 代码规模与可读性

- 核心包约数千行 Python；
- 单文件最大约 950 行（`promql/functions.py`）；
- 测试文件按主题拆分；
- 关键算法有注释说明与真实 Prometheus 的对应关系；
- 不追求抽象层级，优先可读。

## 31.10 如何阅读代码

推荐顺序：

1. `labels.py` / `model.py`：数据模型；
2. `exposition.py`：输入；
3. `chunkenc.py` / `head.py` / `wal.py`：写入；
4. `block.py` / `tsdb.py`：持久化/查询；
5. `parser.py` / `engine.py`：查询；
6. `scrape/` / `rules/` / `api/`：外围；
7. `app.py` / `__main__.py`：组装。

每章末尾的"延伸阅读"会指向具体文件。

## 31.11 需求与非功能目标

功能目标：

- 抓取 text/OpenMetrics；
- 写入 TSDB、WAL、block；
- PromQL 子集查询；
- 规则/告警/API/remote write/read；
- 可运行示例与测试。

非功能目标：

| 目标 | 含义 | 取舍 |
|---|---|---|
| 正确性 | 语义对齐真实 Prometheus | 优先于性能 |
| 可读性 | 单文件可读、命名清晰 | 少抽象、少依赖 |
| 可测试性 | 显式时间/端口/临时目录 | 不依赖墙钟 |
| 可移植性 | Windows/Linux、标准库 | 不用 Linux 专属 API |
| 可观测性 | `/metrics`、stats、日志 | 够用即可 |
| 确定性 | 同样输入同样输出 | 排序/去重稳定 |
| 边界清晰 | 不假装生产级 | 文档逐项列出差异 |

非目标：

- 百万序列；
- 字节级兼容；
- 完整 SD/Alertmanager/多租户；
- 原生直方图、OTLP/protobuf、完整 OTLP 翻译、RW2；
- 高可用。

这些取舍贯穿代码：例如 WAL 用 JSON 而不是二进制，查询用线性扫描而不是 postings，抓取每目标一线程而不是线程池。

## 31.12 错误处理与不变量

错误分类：

```text
PrometheusError
├─ ParseError          语法/类型/参数
├─ ConfigError         配置/规则加载
├─ StorageError        磁盘/WAL/block
├─ DuplicateSampleError
└─ OutOfOrderError
```

不变量：

- `Labels` 不可变、排序、空值丢弃；
- 序列标签集合不可变；
- 同一序列时间戳严格递增（默认）；
- block 不可变；
- tombstone 只增不减（直到 clean）；
- 查询结果按标签/时间排序；
- 所有时间戳是毫秒整数（API 输出秒）。

失败策略：

- 配置错误：启动/重载失败，不进入半可用状态；
- WAL 中间损坏：拒绝启动，避免静默丢数据；
- WAL 尾部截断：容忍，记录 `truncated_bytes`；
- 抓取失败：写 `up=0` + stale，不伪装成功；
- append 错误：单独计数，不改变 `up`；
- 查询超限：返回错误，不 OOM。

## 31.13 测试策略

```text
单元测试：labels/parser/promql/tsdb/wal/block/scrape/rules/api
属性测试：chunk/WAL/snappy/protobuf roundtrip
集成测试：真实 HTTP exporter + scrape + TSDB
e2e：启动 App/HTTP server，轮询查询/targets
规则测试：promtool test rules 风格
文档测试：check_docs.py
```

测试原则：

- 语义优先，不只 happy path；
- 每个 bug 有回归测试；
- 显式时间/端口/临时目录；
- 不依赖外部服务；
- 测试计数纳入文档校验。

Mini 的测试不是"覆盖率数字"，而是"关键语义/失败模式是否被钉住"。第 42 章复盘了 51 个真实缺陷，大多数是"测试全绿但语义错误"。

## 31.14 扩展点与已知限制

想扩展 Mini 时，优先改这些接口：

| 扩展 | 位置 | 难度 |
|---|---|---|
| postings 索引 | `tsdb/block.py`/`tsdb.py` | 中 |
| 流式 SeriesSet | `tsdb/tsdb.py`/`promql/engine.py` | 中 |
| 新 PromQL 函数 | `promql/functions.py` | 低 |
| 新聚合 | `promql/engine.py` | 中 |
| 原生直方图 | `model.py`/`chunkenc.py`/`promql` | 高 |
| OTLP protobuf/NHCB/exemplar | `otlp.py` + `api/server.py` | 高（JSON 子集已在 `otlp.py` 实现） |
| K8s SD | `scrape/discovery.py` | 中 |
| 完整 Alertmanager | `rules/notifier.py` + 新服务 | 高 |
| TLS server | `api/server.py` | 中 |
| 多租户 | 全局 | 高 |

扩展时保持：

- 语义对齐（对照文档/源码）；
- 不破坏现有测试；
- 新行为有测试；
- 文档/计数同步。

## 31.15 从问题到代码的映射

| 问题 | 章节 | 代码 |
|---|---|---|
| 为什么 `rate` 要外推？ | 7、38 | `promql/functions.py` |
| 为什么删除不立刻释放空间？ | 15、35 | `tsdb/block.py` |
| 为什么抓取失败写 stale？ | 12、39 | `scrape/manager.py` |
| 为什么高基数先爆内存？ | 14、27 | `tsdb/head.py` |
| 为什么 remote write 要队列？ | 18、41 | `remote_client.py` |
| 为什么告警要状态机？ | 9、40 | `rules/manager.py` |
| 为什么查询要限样本？ | 16、38 | `promql/engine.py` |

这张表可以作为阅读/教学的索引：先看问题，再定位章节，最后读代码与测试。

## 本章小结

- Mini 的目标是可运行、可读、可测、可对照，不是生产替代。
- 模块划分镜像真实 Prometheus，依赖方向清晰。
- 线程 + 锁 + 队列的并发模型足够演示，便于跨平台。
- 确定性测试通过显式时间/端口/临时目录实现。
- 与真实 Prometheus 的差异在每章和附录 G 中明确记录。

## 延伸阅读

- 本教程第 32～43 章，逐模块走读。
- 仓库 `README.md` 的架构图与验证命令。
- 附录 J（Mini API 参考）。
