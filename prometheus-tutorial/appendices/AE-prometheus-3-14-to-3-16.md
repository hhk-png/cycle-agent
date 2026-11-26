# 附录 AE Prometheus 3.14–3.16 新特性与迁移手册

> 本附录以 2026-11 的官方 release notes 为基线，补充第 20 章之后的小版本变化。Prometheus 每 6 周左右发布一个小版本，特性开关、默认值和实验状态变化很快；上线前请以你实际使用的 tag 对应的 `CHANGELOG.md` 为准。

## AE.1 先固定版本基线

| 版本 | 发布时间 | 支持定位 | 对生产的意义 |
|---|---|---|---|
| 3.13 | 2026-07-01 | **LTS**，支持到 2027-07-31 | 保守生产的默认选择；只收 bug/安全/文档修复；`min()`/`max()` 时长函数已改名 `min_of()`/`max_of()` |
| 3.14 | 2026-08-17 | 常规版本，短期支持 | PromQL 时长表达式默认开启等破坏性变化 |
| 3.15 | 2026-09-24 | 常规版本，短期支持 | 日志级别可热更新、XOR2、Unix socket、OM2.0（实验） |
| 3.16 | 2026-11-04 | 最新常规版本，短期支持 | `smoothed`/`anchored` 稳定、查询成本控制实验 |

三个常见误区：

1. **"最新版一定最适合生产"**。常规版本只支持到下一个版本发布后约 6 周；LTS 才有 12 个月支持窗口。
2. **"LTS 包含所有新特性"**。LTS 只回填 bug/安全修复，3.14–3.16 的新功能不会进入 3.13。
3. **"升级只是换二进制"**。3.14 起 PromQL 默认行为、3.15 起存储编码、3.16 起 exemplar 校验都可能改变查询结果或写入行为。

版本选择建议：

```text
稳定优先、无新特性需求        → 3.13 LTS
需要 XOR2/OM2.0/Unix socket   → 3.15+，先验证 TSDB 读取端
需要 smoothed/anchored/查询成本 → 3.16+，先读实验开关
需要最长支持窗口              → 等下一个 LTS，或留在 3.13
```

## AE.2 3.14：PromQL 时长表达式成为默认

3.14 最重要的破坏性变化是 **PromQL duration expressions 默认开启**，原 `--enable-feature=promql-duration-expr` 变为 no-op。

这意味着下面这类表达式不再需要特性开关：

```promql
# 窗口长度由另一个表达式计算
rate(http_requests_total[5m * 2])

# 动态窗口：按抓取间隔推导
rate(http_requests_total[step() * 4])
```

相关变化：

- **注意**：`min()`/`max()` 在时长表达式语境中改名为 `min_of()`/`max_of()` 是 **3.13.0** 的变更，不是 3.14；3.14 只是把时长表达式从实验开关变为默认开启；
- `foo[5m+range()]` 的解析缺陷被修复；
- NaN、无穷大、越界的时长表达式会被拒绝，而不是静默截断；
- UI 对 `step()`、`range()`、`min_of()`、`max_of()` 提供高亮、补全和 lint。

迁移检查（3.13 已包含改名，只是时长表达式仍需开关）：

```bash
# 在 3.13 上显式开启开关，用 min_of/max_of 验证
prometheus --enable-feature=promql-duration-expr ...
```

```promql
# 升级后确认没有依赖旧的 min()/max() 名称
rate(http_requests_total[min_of(5m, 10m)])
rate(http_requests_total[max_of(5m, 10m)])
```

注意：时长表达式让查询的窗口长度依赖运行时参数，**记录规则的稳定性会下降**。生产规则仍建议使用字面量窗口；动态窗口留给交互式排障和探索式查询。

3.14 的 PromQL 还有两项容易记错的状态变化：

- `first_over_time()` 在 3.14 **转正**，不再需要 `promql-experimental-functions`；
- `ts_of_first_over_time()` 仍是实验函数，仍需 `promql-experimental-functions`。

注意不要把 `fill()` 系列算进 3.14：二元运算的 `fill()`/`fill_left()`/`fill_right()` 是 3.10 引入的实验特性，需要单独的 `--enable-feature=promql-binop-fill-modifiers`；3.14 只是让 `promtool check rules` 接受这些表达式。两者开关不同，升级时不要混为一谈。

实验函数不要在没有回归测试的情况下进入 page 级告警。

## AE.3 3.15：运行时可调、存储与抓取协议扩展

3.15 的变化集中在"运维可调性"和"存储/抓取效率"。

### AE.3.1 日志级别可热更新

`--log.level` 被弃用，日志级别移入配置：

```yaml
runtime:
  log_level: info
```

行为：

- 配置文件里的 `runtime.log_level` 在 reload 时生效；
- `--log.level` 仍可用，作为字段缺失时的默认值；
- 字段值拼写错误会导致 reload 失败，而不是静默忽略。

这解决了一个长期痛点：排障时想把日志临时调到 `debug`，不必重启进程或改启动参数。

### AE.3.2 XOR2 编码稳定

XOR2 浮点 chunk 编码在 3.15 稳定：

```yaml
storage:
  tsdb:
    chunk_encoding:
      floats: xor2
```

要点：

- 原 `--enable-feature=xor2-encoding` 被弃用，改用配置项；
- **XOR2 一旦写入，旧读取端可能读不了**；Thanos sidecar、远程读取端、备份工具必须先确认支持；
- `--enable-feature=st-storage` 会自动启用 XOR2 与支持 ST 的直方图编码；
- 这是"性能收益明确、回滚成本也明确"的变更，先在 staging 验证读取链路。

### AE.3.3 抓取协议扩展

3.15 增加：

- **Unix Domain Socket 抓取**：目标可以是本地 socket，适合 sidecar/本机 agent；
- **OpenMetrics 2.0 抓取格式**：更完整的元数据、单位、exemplar 表达；3.15 仍是实验特性，通过 `--enable-feature=openmetrics2` 启用，**不是** `scrape_protocols` 的取值；
- **zstd 压缩抓取响应**：`--enable-feature=zstd-scrape`，降低大 exposition 的带宽与解析开销。

Unix socket 用目标标签 `__unix_socket__` 指定路径，不是 `unix://` 地址：

```yaml
scrape_configs:
  - job_name: local-agent
    static_configs:
      - targets: ["local-agent"]
        labels:
          __unix_socket__: /run/agent/metrics.sock
```

注意：

- `__address__` 仍作为 Host 名字（不需要能解析），拨号走 `__unix_socket__`；
- `__unix_socket__` 在请求发出前会被剥离；3.15 之前会忽略该标签并以 `lookup` 失败；
- socket 权限要匹配 Prometheus 进程 uid（官方镜像默认 65534），容器场景需要共享卷；
- zstd 需要目标端支持 `Accept-Encoding: zstd`，否则会回退；
- OM2.0 的解析更严格，历史上"能解析但不规范"的 exporter 可能暴露问题；未开开关时遇到 OM2 Content-Type 会抓取失败，除非配置 `fallback_scrape_protocol`；
- `scrape_protocols` 的合法值仍以 `PrometheusProto`、`OpenMetricsText0.0.1`、`OpenMetricsText1.0.0`、`PrometheusText0.0.4`（以及较新版本的 `PrometheusText1.0.0`）为准，不要写 `OpenMetricsText2.0`。

### AE.3.3.1 OpenMetrics 2.0 迁移清单

OM2.0 把更多语义放进 exposition（元数据、单位、exemplar、时间戳），解析器也更严格。迁移时不要把它当成"换个 Content-Type"：

1. **灰度开关**：先在 staging 开 `--enable-feature=openmetrics2`，只对一两个 job 启用；生产保留 `scrape_protocols` 的 1.0/文本回退和 `fallback_scrape_protocol`。
2. **验证解析**：用 `promtool check metrics` 或抓取日志确认没有解析错误；OM2 会暴露旧 exporter 里"能解析但不规范"的行。
3. **验证元数据**：抓取后对比 `/api/v1/metadata`，确认 HELP/TYPE/UNIT 没有被丢弃或改写；单位/后缀变化会影响查询和 recording rule。
4. **验证 exemplar**：确认 trace/span ID 能通过 `/api/v1/query_exemplars` 查到，且时间戳单位正确（OM 的时间戳是秒，不是毫秒）。
5. **验证回退**：让目标同时支持 OM2 与 OM1/text，模拟 OM2 失败，确认 Prometheus 会回退而不是把目标打成 down。
6. **检查后端**：remote write/长期存储是否保留元数据/exemplar；不支持的接收端会静默丢字段。
7. **固定版本**：OM2 在 3.15/3.16 仍是实验特性，解析细节可能随小版本变化；升级前读 changelog，升级后重跑同一组 exporter。

常见错误：

- 把 `OpenMetricsText2.0` 写进 `scrape_protocols`（它不是合法取值，OM2 走独立 feature flag）；
- 只开 OM2 不留回退，旧 exporter 直接抓取失败；
- 用毫秒时间戳构造 OM2 样本/exemplar；
- 以为开了 OM2 就有 exemplar：exporter 仍必须实际输出 exemplar，且存储层要保留。

### AE.3.4 内存与发现

- `--auto-gomemlimit.refresh-interval` 周期性重新探测容器/系统内存上限并更新 `GOMEMLIMIT`，适合 VPA 场景；`0s` 表示只在启动时探测一次。
- Docker SD 增加 `__meta_docker_container_image` 与 `__meta_docker_container_image_id`，便于按镜像版本做 relabel/聚合。
- UI 的 Targets/SD 页面展示每个 scrape pool 的**生效配置**，排障时不用再猜全局默认值。

## AE.4 3.16：查询能力与正确性收紧

### AE.4.1 `smoothed` / `anchored` 稳定

3.7 引入、3.16 稳定的区间选择器修饰符：

```promql
rate(http_requests_total[5m] smoothed)
increase(http_requests_total[1h] anchored)
```

语义差异：

- **smoothed**：在窗口边界做插值，减少"窗口末端样本不足"造成的锯齿；
- **anchored**：把窗口锚定到固定边界，适合与日历/计费周期对齐。

代价与约束：

- 会改变 `rate`/`increase`/`delta` 在边界的行为，历史面板可能出现数值跳变；
- 在记录规则中使用 `smoothed` 时，规则组要设置 `query_offset`（至少一个抓取间隔），否则窗口末端没有"未来样本"可插值；
- 与 `@`、子查询组合时要逐个用例验证。

### AE.4.2 查询成本控制（实验）

3.16 引入 `--enable-feature=query-cost`，把查询成本拆成三套独立机制：

| 机制 | 端点/配置 | 行为 |
|---|---|---|
| 只估算 | `GET/POST /api/v1/query_cost`、`/api/v1/query_range_cost` | 返回预计序列数/读取样本数，不执行查询 |
| 估算 + 实际 | `/api/v1/query`、`/api/v1/query_range` 加 `cost=true` | 响应同时给出估算与实际成本 |
| 执行上限 | `global.query_max_series`、`query_max_samples_read`、`query_max_duration` | 执行中超过上限返回 HTTP 422，`errorType=cost_limit` |

一个最小配置：

```yaml
global:
  query_max_series: 200000
  query_max_samples_read: 50000000
  query_max_duration: 30s
```

验证：

```bash
curl -s http://localhost:9090/api/v1/features
# data.query_cost == true

curl -sG 'http://localhost:9090/api/v1/query_cost' \
  --data-urlencode 'query=sum(rate(http_requests_total[1h]))'
```

拒绝指标：

```promql
sum by (reason) (increase(prometheus_engine_query_rejected_total[1h]))
```

`reason` 可能是 `max_series` 或 `max_samples_read`。

关键行为与坑：

- 估算不是精确计费：同一查询的估算可能高估（range 查询更明显）或低估；
- 上限对所有查询生效，包括 recording/alerting 规则；过紧的 `query_max_samples_read` 会让昂贵的规则**静默失败**；
- 开启后，`query_max_duration` 会替代规则查询的 `--query.timeout`；升级时检查规则执行时间；
- 单请求的 `max_series`/`max_samples_read` 不能超过服务端上限，否则是 HTTP 400 `bad_data`，不是 422；
- 3.16 对子查询样本的统计口径做了调整：既把子查询从存储读取的样本计入 `TotalSamples`/`TotalSamplesPerStep`，也修复了物化样本被重复计入总统计的问题。升级后要重新基线化成本面板/告警，不要假设旧阈值仍准确；
- 先在 staging 用真实面板/规则跑一周，记录估算 vs 实际，再设上限；多租户平台把上限做成租户配额。

适合场景：多租户/自助查询平台、大范围 `query_range` 的 Grafana 面板、需要按查询收费或配额的内部平台。不要把它当成精确账单系统。

### AE.4.2.1 其他 3.16 实验能力

- `integral(v[5m])`：对区间向量按时间积分（配合 `promql-experimental-functions`）；
- extended range selectors（`smoothed`/`anchored`）转正，规则里仍需 `query_offset`；
- OpenMetrics 2.0 的抓取/解析继续修复，但仍是实验特性；生产保留 1.0/文本回退，不要把 OM2.0 作为唯一协议；
- 搜索端点 `batch_size` 上限 1000；
- 指标元数据/自监控字段有调整，升级时对比 `/metrics`。

### AE.4.3 计数器重置检测

新增基于 start timestamp 的 counter reset 检测，以及按查询覆盖的请求头：

```text
X-Prometheus-Use-Start-Timestamps: true
```

用途：

- 远端写入/OTLP 场景下，counter 的 start timestamp 比"值变小"更可靠；
- 可以在单条查询上验证新检测逻辑，而不必全局切换。

### AE.4.4 正确性收紧

3.16 的几个行为变化值得注意：

- **exemplar 重复标签名被拒绝**：过去会存储并在 `/api/v1/query_exemplars` 返回重复 JSON key，现在直接拒绝；
- 子查询样本计入 `TotalSamples`/`TotalSamplesPerStep`，查询统计会更接近真实成本；
- 实验搜索端点把 `batch_size` 限制在 1000；
- 启用 TLS 的 `--web.config.file` 时，默认 `--web.external-url` 变为 `https`；
- Remote Write 2.0 队列移除了一批废弃的 `prometheus_remote_storage_metadata_*` 指标；
- 新增 `convert_nhcb_to_classic` 远程写选项，用于把 NHCB（自定义桶原生直方图）转成经典直方图发给旧后端。

### AE.4.5 promtool 与 agent

- `promtool test rules` 输出测试文件名，失败定位更快；
- `promtool` 新增 `--allow-incompatible-block-duration`，处理块时长不一致的检查场景；
- Agent 模式支持 `metadata-wal-records`，减少元数据丢失；
- 实验性的 PromQL 解析选项允许未加引号的 Unicode 字母/点号（仍建议用引号）。

## AE.5 破坏性变化速查

| 版本 | 变化 | 影响 | 动作 |
|---|---|---|---|
| 3.14 | PromQL 时长表达式默认开启 | 查询语义/窗口可变 | 回归所有规则与面板；改 `min_of`/`max_of` |
| 3.14 | `stats` 参数只建议 `true`/`all`，其他值弃用 | 旧客户端可能收到警告 | 改客户端参数 |
| 3.14 | `__meta_hetzner_datacenter` 移除 | Hetzner SD relabel 失效 | 改用其他标签 |
| 3.15 | `--log.level` 弃用 | 启动参数行为变化 | 迁移到 `runtime.log_level` |
| 3.15 | XOR2 稳定但需显式配置 | 旧读取端可能不兼容 | 先验证 Thanos/备份/远程读 |
| 3.16 | exemplar 重复标签被拒绝 | 脏 exemplar 写入失败 | 修客户端/exporter |
| 3.16 | 子查询样本计入统计 | 查询成本指标上升 | 调整告警阈值/成本模型 |
| 3.16 | TLS 下 external-url 默认 https | 反代/链接生成变化 | 显式设置 `--web.external-url` |

## AE.6 特性开关与配置迁移表

| 旧写法 | 新写法 | 版本 | 说明 |
|---|---|---|---|
| `--enable-feature=native-histograms` | `scrape_native_histograms: true` | 3.8+ / 3.9 no-op | 抓取侧显式开启 |
| `scrape_classic_histograms` | `always_scrape_classic_histograms` | 3.8+ | 同时保留经典桶 |
| `--enable-feature=promql-duration-expr` | 默认开启 | 3.14 | flag 变 no-op |
| `--log.level` | `runtime.log_level` | 3.15 | reload 生效 |
| `--enable-feature=xor2-encoding` | `storage.tsdb.chunk_encoding.floats: xor2` | 3.15 | 写入格式变化，慎回滚 |
| `--enable-feature=zstd-scrape` | 仍为实验 flag | 3.15 | 需目标端支持 |
| `--enable-feature=openmetrics2` | 仍为实验 flag | 3.15 | 不是 `scrape_protocols` 取值 |
| `--enable-feature=query-cost` | 仍为实验 flag | 3.16 | 成本控制 |
| `--enable-feature=promql-experimental-functions` | 仍为实验 flag | 3.14+ | `ts_of_first_over_time`/`integral` 等（`first_over_time` 已在 3.14 转正） |
| `--enable-feature=promql-binop-fill-modifiers` | 仍为实验 flag | 3.10+ | `fill()`/`fill_left()`/`fill_right()` |

原则：

- **能写成配置的，不要依赖 flag**；flag 的生命周期通常比配置短；
- **实验特性不要进入 page 级告警**，除非有独立回归；
- **存储格式变化（XOR2/ST）不可轻易回滚**，升级前先备份并验证所有读取端。

## AE.7 升级步骤

```text
1. 盘点：当前版本、LTS 状态、feature flags、remote write 接收端
2. 读 changelog：标出所有 [CHANGE] 与 [FEATURE]
3. 备份：TSDB 快照 + 配置/规则仓库 tag
4. staging：同数据量、同查询集、同规则集
5. 回归：PromQL 结果、告警触发、Grafana 面板、API 客户端
6. 兼容：Thanos/Mimir/远端读/备份工具是否支持新编码
7. 灰度：HA 对中的从库先升，观察 24h
8. 生产：滚动升级，保留旧二进制与回滚路径
9. 复盘：记录行为差异与新增告警
```

回滚边界：

- 只改配置/规则：直接回滚配置；
- 已写入 XOR2/ST 数据：回滚到旧版本可能读不了新 block，必须保留旧数据快照或确认读取端兼容；
- 已切换 OTLP delta 转换/remote write 协议：确认接收端仍能处理新格式。

## AE.8 兼容性验证清单

```text
[ ] 所有 recording/alerting 规则的 PromQL 已在目标版本跑通
[ ] 所有 Grafana 面板的查询在目标版本返回相同量级结果
[ ] `promtool check rules` 与 `promtool test rules` 通过
[ ] `/api/v1/query`、`/query_range`、`/series`、`/labels` 的客户端兼容
[ ] remote write 接收端支持目标版本的协议/编码
[ ] remote read 客户端能读取新 block
[ ] Thanos sidecar/compactor 支持 XOR2/ST（如启用）
[ ] 备份/恢复流程在新版本上演练过
[ ] feature flags 与配置文件已收敛，不依赖弃用项
[ ] 回滚所需的旧二进制、旧配置、旧快照可用
```

## AE.9 常见错误

- **把 3.16 的实验特性写进 SLO 规则**：实验特性可能在下个小版本改语义；SLO 规则应只用稳定功能。
- **在 3.13 LTS 上找 3.15 的配置项**：LTS 不会回填新功能；配置校验会直接报未知字段。
- **启用 XOR2 后只验证 Prometheus 自身**：Grafana 通过 Prometheus 查询没问题，但 Thanos/Mimir 读 block 时才暴露兼容性问题。
- **忽略 exemplar 重复标签**：客户端库版本混杂时会生成重复 label 的 exemplar，3.16 起写入失败；升级前先清理。
- **把 `runtime.log_level` 写成 `runtime.log-level`**：reload 失败；字段名以下发配置的 schema 为准。
- **在 3.13+ 上继续用 `min()`/`max()` 写时长表达式**：可能被解析成聚合运算符，结果错误；改名发生在 3.13.0，改用 `min_of()`/`max_of()`。

## AE.10 版本选择决策树

```text
需要 12 个月支持窗口？
├─ 是 → 3.13 LTS（或等下一个 LTS）
└─ 否
   ├─ 需要 XOR2/OM2.0/Unix socket？
   │  ├─ 是 → 3.15+（先验证读取端）
   │  └─ 否
   │     ├─ 需要 smoothed/anchored/查询成本？
   │     │  ├─ 是 → 3.16+
   │     │  └─ 否 → 3.13 LTS 足够
   └─ 有严格合规/审计要求？ → LTS + 固定补丁版本
```

## 延伸阅读

- Prometheus 官方 `CHANGELOG.md` 与 release notes（3.14、3.15、3.16）。
- Prometheus 官方 release cycle / LTS 说明。
- 本教程第 20 章（3.x/OTel）、附录 R（原生直方图/RW2）与附录 B（配置参考）。
