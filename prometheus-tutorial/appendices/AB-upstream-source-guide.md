# 附录 AB 上游源码导读：从 Mini 到 Prometheus 代码库

第 31～43 章用一个可运行的 Mini-Prometheus 讲清了"每个子系统在做什么"。本附录解决下一个问题：**如果你要读真实 Prometheus 的 Go 源码，应该从哪里进、按什么顺序读、哪些文件对应 Mini 的哪个模块？**

> 源码路径以 Prometheus 3.x 为基线。上游重构频繁，文件可能移动或拆分；用 `git grep`/`go doc` 定位，不要死记行号。阅读前先 `git checkout` 一个固定 tag（例如你生产使用的版本），避免 main 分支漂移。

## AB.1 代码库地图

Prometheus 仓库的核心目录：

```text
prometheus/
  cmd/
    prometheus/          # 主程序：flag、启动、热加载
    promtool/            # 规则/配置/TSDB 检查工具
  config/               # YAML 配置解析与校验
  discovery/            # 服务发现：file、http、dns、kubernetes、consul、ec2...
  scrape/               # 抓取池、目标、relabel、scrape loop
  storage/              # 存储接口 + remote read/write
  tsdb/                 # Head、WAL、Block、compaction、index
  promql/               # 词法/语法/AST/求值器/函数
  rules/                # recording/alerting 规则与分组求值
  notifier/             # 向 Alertmanager 发送告警
  web/                  # HTTP server、API v1、UI、federation
  model/                # labels、value、timestamp 等基础类型
  util/                 # 通用工具
  documentation/        # 官方文档（部分）
```

和 Mini 的对应关系：

| 能力 | Mini 文件 | 上游目录/文件 |
|---|---|---|
| 标签/匹配器 | `labels.py` | `model/labels/` |
| exposition 解析 | `exposition.py` | `model/textparse/`、`scrape/` |
| 配置 | `config.py` | `config/config.go` |
| 服务发现 | `scrape/discovery.py` | `discovery/` |
| relabel | `scrape/relabel.py` | `model/relabel/`、`scrape/target.go` |
| 抓取循环 | `scrape/manager.py` | `scrape/scrape.go`、`scrape/manager.go` |
| Head/chunk | `tsdb/head.py`、`tsdb/chunkenc.py` | `tsdb/head*.go`、`tsdb/chunkenc/` |
| WAL | `tsdb/wal.py` | `tsdb/wal/`、`tsdb/record/` |
| Block/compaction | `tsdb/block.py`、`tsdb/tsdb.py` | `tsdb/block.go`、`tsdb/compact.go`、`tsdb/db.go` |
| 索引/查询 | `tsdb/tsdb.py` | `tsdb/index/`、`tsdb/querier.go` |
| PromQL 前端 | `promql/lexer.py`、`parser.py`、`ast.py` | `promql/parser/` |
| PromQL 求值 | `promql/engine.py`、`functions.py` | `promql/engine.go`、`promql/functions.go`、`quantile.go` |
| 规则/告警 | `rules/manager.py` | `rules/manager.go`、`rules/group.go`、`rules/alerting.go` |
| 通知 | `rules/notifier.py` | `notifier/notifier.go` |
| HTTP API | `api/server.py` | `web/api/v1/api.go`、`web/web.go` |
| 联邦 | `api/server.py` | `web/federate.go` |
| remote write/read | `remote.py`、`remote_client.py` | `storage/remote/` |

## AB.2 启动链路：先看 `cmd/prometheus/main.go`

主程序是理解全局依赖的最好入口。阅读时抓住四条线：

1. **flag 与配置**：`--config.file`、`--storage.tsdb.path`、`--web.listen-address`、`--enable-feature` 如何解析；配置如何 `LoadFile` 并校验。
2. **组件装配**：`tsdb.Open`、`scrape.NewManager`、`rules.NewManager`、`notifier.NewManager`、`web.New` 的创建顺序与依赖注入。
3. **生命周期**：`context.Context` 取消、`run.Group` 的 actor 模型、优雅退出。
4. **热加载**：SIGHUP/`/-/reload` 如何触发 `reloadConfig`，哪些组件支持热更新、哪些必须重启。

建议对照 Mini 的 `__main__.py`/`app.py` 看：

- Mini 把所有组件挂在 `App` 上，启动顺序是"TSDB → scrape → rules → API"；
- 上游用 `run.Group` 管理 actor，每个子系统实现 `run()`/`stop()`；
- Mini 的配置热加载是"重新解析 + 替换"，上游要处理 discovery/scrape/rules 的增量更新与失败回滚。

关键问题：

- 配置校验失败时，旧配置是否继续生效？
- 规则文件解析失败时，是整批拒绝还是部分加载？
- 关闭时先停 scrape 还是先停 TSDB？为什么？

## AB.3 配置与校验：`config/config.go`

上游配置解析的核心不是"读 YAML"，而是**默认值、校验、跨字段约束**：

- `DefaultConfig`、`GlobalConfig`、`ScrapeConfig`、`RemoteWriteConfig` 的默认值；
- `Validate()` 里的约束：job 名唯一、relabel 规则合法、`scrape_timeout <= scrape_interval`、remote write URL 必填；
- `config.LoadFile` 的严格 YAML 解析（未知字段报错）；
- `config.ReloadConfig` 的原子替换。

读法：

1. 先看 `config/config.go` 顶部的结构体，建立"配置长什么样"；
2. 再看 `Validate()`，这是最容易被文档漏掉的约束；
3. 最后看 `config/testdata/` 里的正反例，比读文档快。

Mini 的 `config.py` 只实现了子集；对照时可以问：Mini 漏掉了哪些校验？这些校验在真实环境里防止了什么故障？

## AB.4 服务发现：`discovery/`

`discovery/` 的抽象很小：

```go
type Discoverer interface {
    Run(ctx context.Context, up chan<- []*targetgroup.Group)
}
```

每个子目录实现一个 Discoverer：

- `discovery/file/`：file_sd，监听文件变化；
- `discovery/http/`：HTTP SD，定时拉取 JSON；
- `discovery/dns/`：DNS SRV/A；
- `discovery/kubernetes/`：watch Pod/Service/Endpoints/Ingress；
- `discovery/consul/`、`ec2/`、`azure/`、`gce/` 等云厂商；
- `discovery/manager.go`：合并所有 Discoverer 的 target group，维护 provider 状态。

阅读重点：

- target group 的 `Source`、`Labels`、`Targets` 三元组；
- 发现失败时是"保留旧目标"还是"清空"；
- `discovery/manager.go` 如何做去重与 provider 生命周期；
- Kubernetes SD 的 role（pod/service/endpoints/endpointslice/ingress/node）如何映射成标签。

Mini 只实现 static/file/http/dns；读上游时重点补 Kubernetes 的 label 映射与 RBAC 要求。

## AB.5 抓取：`scrape/`

抓取是 Prometheus 最值得精读的子系统之一。入口：

- `scrape/manager.go`：`Manager` 管理所有 scrape pool；`reload` 根据配置增删 pool；`targetSet` 维护目标；
- `scrape/scrape.go`：`scrapePool`、`scrapeLoop`、`scrapeCache`；真正的抓取、解析、append、stale 标记都在这里；
- `scrape/target.go`：`Target`、`PopulateLabels`、relabel 应用、`Labels()`；
- `model/relabel/`：relabel 动作与 Go regex 展开。

建议按一次抓取的生命周期读：

1. `target.go`：从 discovered labels 到 final labels（job/instance、`__address__`、relabel）；
2. `scrape.go` 的 `scrapeLoop.run`：定时器、`scrape()`、`append()`、`report()`；
3. `scrapeCache`：如何避免重复解析、如何处理 metric relabel；
4. stale 标记：抓取失败/目标消失时写什么；
5. `scrape_samples_scraped`、`scrape_duration_seconds`、`up` 在哪里设置。

对照 Mini 的 `scrape/manager.py`：

- Mini 每目标一线程；上游用固定大小的 scrape pool + 调度；
- Mini 的 stale 逻辑是简化版；上游要处理 honor_timestamps、out-of-order、sample_limit；
- Mini 的 relabel 是自实现；上游用 `model/relabel` 的 `Expand` 语义。

## AB.6 TSDB：`tsdb/`

TSDB 是代码量最大、最值得按"写入 → 查询 → 压缩"三条路径读的部分。

### AB.6.1 写入路径

- `tsdb/db.go`：`DB`、`Open`、`Appender`、`Querier`；
- `tsdb/head.go`：`Head`、`memSeries`、`stripeSeries`（按 label hash 分片）；
- `tsdb/head_append.go`：`appendable`、`Append`、乱序/重复处理、WAL 记录；
- `tsdb/head_read.go`：`headChunkReader`、`chunkReader`；
- `tsdb/chunkenc/`：`XORChunk`、`HistogramChunk`、`FloatHistogramChunk`；
- `tsdb/wal/`：WAL 分段、record、checkpoint；
- `tsdb/record/`：WAL record 的编码格式。

阅读顺序：

1. `Appender` 接口（`storage/interface.go`）→ `headAppender`；
2. `memSeries.append` 如何选 chunk、切 chunk；
3. WAL record 何时写、何时 fsync；
4. `Head.Truncate`/checkpoint 如何截断；
5. 乱序样本的 `outOfOrder` 支持与 `minValidTime`。

### AB.6.2 查询路径

- `tsdb/querier.go`：`blockQuerier`、`mergedQuerier`、`NewBlockQuerier`；
- `tsdb/index/index.go`：index 文件格式、postings、symbols、label values；
- `tsdb/index/postings.go`：postings 交集/并集；
- `tsdb/chunks/`：chunk 读取与迭代；
- `storage/interface.go`：`Querier`、`SeriesSet`、`ChunkQuerier`。

阅读顺序：

1. `Querier.Select` 如何把 matcher 变成 postings；
2. `mergedQuerier` 如何合并 Head 与多个 Block；
3. `SeriesSet` 的惰性迭代；
4. tombstone 在查询时如何过滤；
5. `@`/offset/子查询如何影响 `Select` 的时间范围。

### AB.6.3 压缩与保留

- `tsdb/compact.go`：`LeveledCompactor`、`Plan`、`Compact`、`Write`;
- `tsdb/block.go`：`Block`、`meta.json`、`Delete`、`CleanTombstones`；
- `tsdb/db.go`：`compactLoop`、`blocksToDelete`、retention by time/size；
- `tsdb/index/index.go`：写 index；
- `tsdb/chunks/chunks.go`：写 chunks。

阅读顺序：

1. `Plan` 如何选择要合并的 block（时间重叠、层级）；
2. `Compact` 如何流式重写 chunk/index；
3. tombstone 何时真正删除；
4. retention 如何决定删除哪些 block；
5. 压缩失败/中断后如何恢复（临时目录、`meta.json` 的 `ulid`）。

## AB.7 PromQL：`promql/`

PromQL 的实现分三层：

### AB.7.1 词法/语法/AST

- `promql/parser/lex.go`：token、字符串/数字/持续时间、UTF-8 指标名；
- `promql/parser/generated_parser.y` + `generated_parser.go`：yacc 语法；
- `promql/parser/parse.go`：ParseExpr、类型检查、`@`/offset 修饰；
- `promql/parser/ast.go`：AST 节点、`String()`；
- `promql/parser/printer.go`：格式化输出。

Mini 对照：`promql/lexer.py`、`parser.py`、`ast.py`。上游用生成式 parser，Mini 用递归下降；语义要对齐的是**优先级、结合性、类型检查**，不是实现方式。

### AB.7.2 求值器

- `promql/engine.go`：`Engine`、`Query`、`evalStmt`、`eval`、`rangeEval`；
- 选择器：`vectorSelector`、`matrixSelector`；
- 聚合：`aggregation`、`sum`/`avg`/`topk` 等；
- 二元运算：`binary`、`VectorMatching`；
- 子查询：`SubqueryExpr`；
- 函数：`functions.go`、`quantile.go`；
- 原生直方图：`histogram_quantile`、`histogram_count`、`histogram_sum`、`histogram_fraction`。

阅读建议：

1. 先读 `evalStmt`，理解"每个 step 一个 evaluation"；
2. 再读 `rangeEval`，理解区间向量函数如何按 step 调用；
3. 然后读 `vectorSelector`/`matrixSelector`，理解 lookback 与 `[range]`；
4. 最后读 `binary` 的 VectorMatching，理解 `on`/`ignoring`/`group_left`/`group_right`；
5. 函数按需读，先看 `rate`、`increase`、`histogram_quantile`。

### AB.7.3 测试

- `promql/promqltest/testdata/*.test`：官方 PromQL 语义测试，最好的"行为字典"；
- `promql/engine_test.go`：引擎测试；
- `promql/bench_test.go`：基准测试；
- `promtool test rules`：规则单元测试（`cmd/promtool/unittest.go`）。

读某个函数的边界行为时，先 `grep` 测试文件里的表达式，比读文档更快。

## AB.8 规则与告警：`rules/` + `notifier/`

- `rules/manager.go`：加载规则文件、构建 group、周期执行、更新；
- `rules/group.go`：`Group.Eval`、并发限制、`evaluation_duration_seconds`；
- `rules/alerting.go`：`AlertingRule`、`AlertState`（inactive/pending/firing）、`for`/`keep_firing_for`；
- `rules/recording.go`：`RecordingRule`；
- `rules/templates.go`：Go template、`$labels`/`$value`/`humanize`；
- `notifier/notifier.go`：告警队列、重试、发送到 Alertmanager；
- `notifier/notify.go`：通知接口。

阅读顺序：

1. `Group.Eval` 的"按 interval 对齐"；
2. `AlertingRule.Eval` 的状态迁移；
3. 规则求值失败时 `lastError`/`health` 如何暴露；
4. notifier 的队列满/重试/丢弃策略；
5. 规则热加载时 pending 状态是否保留（这决定告警是否会重新计时）。

## AB.9 HTTP API 与 federation：`web/`

- `web/web.go`：HTTP server、路由、TLS、生命周期；
- `web/api/v1/api.go`：`/api/v1/*` 的 handler、参数解析、错误码；
- `web/api/v1/json_codec.go`：JSON 编码；
- `web/federate.go`：`/federate` 的 matcher 解析与流式输出；
- `web/ui/`：内置 UI（React）；
- `web/api/v1/api_test.go`：API 行为测试。

阅读重点：

- `query`/`query_range` 的 `time`/`timeout`/`step` 默认值与边界；
- `series`/`labels`/`label/*/values` 如何走 TSDB；
- federation 的 `match[]` 参数与 `Content-Type`；
- 错误 envelope：`status`/`errorType`/`error`；
- remote read 的 protobuf/JSON 编码（`storage/remote/read.go`）。

## AB.10 remote write/read：`storage/remote/`

- `storage/remote/write.go`：`WriteStorage`、`Append`、shard；
- `storage/remote/queue_manager.go`：队列、批处理、重试、背压、shard；
- `storage/remote/codec.go`：protobuf 编解码、Snappy；
- `storage/remote/read.go`：remote read；
- `storage/remote/client.go`：HTTP 客户端、认证、TLS；
- `storage/remote/queue_manager_test.go`：队列行为。

阅读顺序：

1. `WriteStorage.Append` 如何把样本分片到 queue；
2. `queue_manager` 的 batch/shard/retry 参数；
3. 队列满时是阻塞、丢弃还是降级；
4. `remote_write` 的 `write_relabel_configs` 在哪里应用；
5. RW 2.0 的 codec 与 metadata/exemplar/native histogram 支持。

## AB.11 用 Mini 做对照阅读

推荐一个"双栏阅读"方法：

1. 选一个主题（例如 rate）；
2. 先在 Mini 里找到实现和测试，跑一遍；
3. 再到上游 `promql/functions.go` 找到同名函数；
4. 对照输入/输出/边界，列出差异；
5. 把差异写成一张表：Mini 简化了什么、上游为什么这样做、生产上会有什么后果。

示例：`rate()` 的对照问题：

- Mini 的窗口边界是左开右闭吗？
- 外推（extrapolation）在窗口不足两个样本时怎么处理？
- counter 重置检测的阈值是什么？
- 原生直方图的 rate 与 float 样本有何不同？

这类问题比"读完整个仓库"更能建立真正的理解。

## AB.12 构建、测试与调试

```bash
# 构建
make build          # 产出 ./prometheus 和 ./promtool
go build ./cmd/prometheus

# 单元测试（按包）
go test ./tsdb/...
go test ./promql/...
go test ./scrape/...

# 规则/配置检查
./promtool check config prometheus.yml
./promtool check rules rules/*.yml
./promtool test rules rules/*_test.yml

# 本地跑一个最小实例
./prometheus --config.file=prometheus.yml --storage.tsdb.path=./data
```

调试建议：

- 用 `--log.level=debug` 看抓取/规则细节；
- `/-/healthy`、`/-/ready`、`/metrics` 是第一批要看的端点；
- `go tool pprof` 看 CPU/heap（`/debug/pprof/`）；
- `promtool tsdb analyze` 看 block 基数/标签分布；
- 修改代码后先跑对应包的测试，再跑 `promql/promqltest`。

## AB.13 阅读路线（按目标）

| 目标 | 路线 |
|---|---|
| 写 exporter/埋点 | `model/textparse` → `scrape/scrape.go` → `promql/functions.go` |
| 排查抓取问题 | `discovery/` → `scrape/target.go` → `scrape/scrape.go` |
| 排查存储/压缩 | `tsdb/head_append.go` → `tsdb/wal/` → `tsdb/compact.go` |
| 排查查询慢 | `promql/engine.go` → `tsdb/querier.go` → `tsdb/index/` |
| 写规则/告警 | `rules/group.go` → `rules/alerting.go` → `notifier/` |
| 做长期存储 | `storage/remote/` → `tsdb/` → 对接 Thanos/Mimir 的协议 |
| 读 PromQL 语义 | `promql/promqltest/testdata/*.test` → `promql/engine.go` → `functions.go` |

## AB.14 常见误区

- **把 main 分支当稳定版**：上游重构快，读 tag 对应的代码；
- **只看文档不看测试**：边界行为以测试为准；
- **把 Mini 当上游**：Mini 是教学简化，差异在 `VERIFICATION.md` 与第 43 章列明；
- **忽略并发**：上游大量使用 goroutine/锁/原子操作，单线程阅读会漏掉竞态；
- **忽略错误路径**：WAL 损坏、抓取超时、队列满、规则失败，才是生产代码的主体；
- **不看 metrics**：每个子系统都暴露自监控指标，读代码时对照 `/metrics` 更容易理解。

## 延伸阅读

- Prometheus 官方仓库与 `CONTRIBUTING.md`、`RELEASE.md`。
- `promql/promqltest/testdata/` 的语义测试。
- `tsdb/` 的设计文档与历史 PR（尤其是 compaction/WAL 变更）。
- 本教程第 13～17 章（架构/TSDB/PromQL）、第 31～43 章（Mini 实现与重写）、附录 J（Mini API）。
