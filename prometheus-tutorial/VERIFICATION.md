# 验证报告

## 1. 环境

- 日期：2026-11-26
- 平台：Windows 11（Git Bash / PowerShell 均可）
- Python：3.14.6
- PyYAML：6.0.3
- pytest：9.1.1
- 运行目录：`prometheus-tutorial/`

> 版本号会随环境变化；本文记录的是本次交付实际运行的版本。

本版结构调整：新增第 50～51 章《综合实战》两章，原第 50～54 章顺延为第 52～56 章；新增附录 AE（3.14–3.16 新特性与迁移）；第 20 章补充 3.14–3.16 增量；第 23、25、26、29 章合并重复小节并重新编号。章节/附录/引用/计数已同步。

上一轮增量（2026-11-26）：新增附录 AF（UTF-8 名称与转义）与 AG（OTLP 接收与 delta），Mini 实现 UTF-8 引号标签名和 OTLP/HTTP JSON 接收（含 delta→cumulative），新增 `examples/10_otlp_ingest.py` 与 `tests/test_utf8_names.py`、`tests/test_otlp.py`；第 20、31、32、37、41、42、45 章同步更新，测试从 462 增至 511，示例从 9 增至 10，附录从 31 增至 33。

本轮增量（2026-11-26，第二轮）：新增附录 AH（多行业监控案例库：电商/支付/流媒体/SaaS/IoT）与 AI（LLM 推理监控与成本治理：TTFT/TPOT、KV cache、DCGM、token 成本、OTel GenAI）；重写加深附录 A（PromQL 速查）、D（命名）、H（术语）、I（阅读）、L（安全基线）；Mini 实现组级 `query_offset` 与全局 `rule_query_offset`（查询时间后移、写入时间不变），新增 5 个回归用例；第 9、20、40 章与附录 B/AE 同步；测试从 511 增至 516，附录从 33 增至 35。

本轮增量（2026-11-26，第三轮）：新增附录 AJ（监控有效性评估与可观测性成熟度：四层模型、取证查询、100 分评分卡、两周审计、反模式与检查清单）；Mini 补齐 `first_over_time`、`ts_of_first_over_time`、`ts_of_last_over_time` 与 3.16 实验函数 `integral`（梯形积分、NaN 归零、单样本为 0），新增 3 个求值测试；第 22 章与附录 AE 扩写 3.16 `query-cost` 的端点、配置、上限、错误类型与指标；第 52～56 章补充 2025–2026 市场/并购/定价/认证数据点；README 的 promtool 计数由 2 修正为 3，`check_docs.py` 新增 promtool 计数自检；测试从 516 增至 519，函数从 71 增至 75，附录从 35 增至 36。

## 2. 三条验证命令

```bash
python -m pytest
python examples/run_all.py
python check_docs.py
```

### 2.1 pytest

结果：

```text
528 passed in ~32s
```

说明：

- 510 个测试函数，参数化后 528 个用例；
- 覆盖标签/解析/PromQL/TSDB/WAL/block/抓取/规则/API/远程/e2e/CLI/规则单元测试/UTF-8/OTLP；
- 包含真实 HTTP（localhost、端口 0）与线程的 e2e 测试；
- 没有跳过（skip）的用例。

### 2.2 examples

结果：

```text
10 / 10 example scenarios passed
```

场景：

1. `01_promql_tour.py`：PromQL 选择/rate/直方图/topk/比较，以及 `first_over_time`/`ts_of_last_over_time`/`integral`；
2. `02_scrape_and_rules.py`：真实 exporter + scrape + TSDB + 查询；
3. `03_storage_lifecycle.py`：head/flush/compaction/snapshot/restart；
4. `04_api_server.py`：启动 App + API 查询 + targets/health；
5. `05_alerting.py`：pending→firing、Alertmanager-lite、抑制；
6. `06_remote_write.py`：protobuf+Snappy remote write + remote read；
7. `07_relabel_and_sd.py`：file_sd + keep/replace/labeldrop；
8. `08_staleness_and_failures.py`：序列消失/HTTP 500/up/stale；
9. `09_rule_unit_tests.py`：promtool 风格规则单元测试（通过/失败 diff）；
10. `10_otlp_ingest.py`：OTLP/JSON 接收、资源属性、单位后缀、直方图、UTF-8 查询与接收端自身指标。

### 2.3 check_docs

结果：

```text
Mini-Prometheus tutorial document check
  chapters:  56
  appendices:36
  examples:  10
  tests:     528
OK: all checks passed
```

检查项：

- 章节编号连续、标题格式、`## 本章小结`/`## 延伸阅读`；
- 章节内小节编号 `N.1`、`N.2`… 连续且与章号一致；
- 附录编号 A–AJ（支持多字母）、标题格式；
- 代码围栏配对；
- 所有 `yaml` 围栏可被 `yaml.safe_load_all` 解析；
- 内部链接存在且不越出项目根；
- `第 N 章`/`附录 X` 引用存在；
- README 目录完整、计数准确（528/10/56+36）；
- README 的 promtool 计数与实际 `promtool test rules` 输出一致（3）；
- 三条验证命令出现；
- 无 TODO/TBD/FIXME/待补充/待完善；
- `mini_prometheus/` 与 `examples/` 可编译。

### 2.4 综合实战配置的端到端冒烟

第 50～51 章新增的 `examples/config/capstone.yml` 与 `examples/rules/capstone.yml` 不只做语法检查，还做了真实进程冒烟：

```bash
python -m mini_prometheus check config examples/config/capstone.yml
python -m mini_prometheus check rules examples/rules/capstone.yml
python -m mini_prometheus.promtool test rules examples/rules/capstone_test.yml
```

结果：

```text
SUCCESS: examples/config/capstone.yml is valid; 2 scrape job(s), 1 rule group(s)
SUCCESS: 1 rule group(s), 3 rule(s)
SUCCESS: 1 test(s) passed
```

再用 `demo_exporter` + Mini 实际启动，等待 2 个抓取周期后查询：

```text
up                          -> success, 2 series
job:demo_requests:rate1m    -> success, 1 series
job:demo_request_errors:ratio1m -> success, 1 series
rules health                -> ['ok']
```

`tests/test_capstone_examples.py` 把配置加载与规则测试固化为 2 个 pytest 用例（当轮总数从 460 增加到 462；当前总数见第 4 节）。

## 3. 覆盖矩阵

| 主题 | 章节 | 主要代码 | 主要测试 |
|---|---|---|---|
| 数据模型/标签 | 3, 32 | `labels.py` | `test_labels.py` |
| exposition/OpenMetrics | 5, 32 | `exposition.py` | `test_exposition.py` |
| PromQL 语法 | 6–8, 37 | `promql/lexer.py`, `parser.py`, `ast.py` | `test_promql_parser.py` |
| PromQL 求值 | 7–8, 38 | `promql/engine.py`, `functions.py` | `test_promql_eval.py` |
| UTF-8 指标/标签名 | 3、20、32、37、41；附录 AF | `labels.py`, `exposition.py`, `promql/parser.py` | `test_utf8_names.py` |
| OTLP/JSON 接收 | 20、41；附录 AG | `otlp.py`, `api/server.py`, `app.py` | `test_otlp.py` |
| chunk/head | 14, 33 | `tsdb/chunkenc.py`, `head.py` | `test_chunk.py`, `test_tsdb.py` |
| WAL/恢复 | 14, 34 | `tsdb/wal.py` | `test_wal.py`, `test_tsdb.py` |
| block/compaction | 15, 35 | `tsdb/block.py`, `tsdb.py` | `test_tsdb.py` |
| 查询/合并 | 16, 36 | `tsdb/tsdb.py` | `test_tsdb.py` |
| SD/relabel/抓取 | 11–12, 39 | `scrape/*` | `test_scrape.py`, `test_relabel.py`, `test_config.py` |
| 规则/告警 | 9–10, 40 | `rules/*` | `test_rules.py` |
| 规则 query_offset | 9、20、40；附录 B/AE | `rules/manager.py`、`config.py` | `test_rules.py`、`test_config.py` |
| 多行业监控案例 | 附录 AH | 文档/PromQL | `check_docs.py` |
| LLM 推理监控/成本 | 30；附录 AI | vLLM/DCGM 外部组件 | 文档检查、PromQL 示例 |
| 速查/命名/术语/安全基线 | 附录 A/D/H/L | 文档 | `check_docs.py` |
| API/联邦/远程 | 18, 41 | `api/server.py`, `remote*.py` | `test_api.py`, `test_remote.py` |
| 配置/CLI | 2, 23 | `config.py`, `__main__.py` | `test_config.py`, `test_cli.py` |
| 端到端 | 2, 42 | `app.py` | `test_app_e2e.py` |
| 可视化 | 附录 M | Grafana 外部组件 | 文档检查 |
| Alertmanager 配置 | 附录 N | 外部组件/`examples/alertmanager_lite.py` | `test_rules.py` |
| PromQL 高级函数/模式 | 6–8, 17；附录 O | `promql/functions.py` | `test_promql_eval.py` |
| Operator/K8s 生产 | 11、24；附录 P | 外部组件/relabel 语义 | `test_relabel.py` |
| 长期存储架构 | 18、19；附录 Q/G | `remote*.py`（发送端语义） | `test_remote.py` |
| 原生直方图/RW2 | 4、20；附录 R | 外部组件/Mini 边界 | 文档检查 |
| 生产事故复盘 | 12、23、27；附录 S | `scrape/*`、`api/*` | 对应测试 |
| Pushgateway/短任务 | 1、5；附录 T | `remote_client.py`（相邻能力） | `test_remote.py` |
| 自监控/元监控 | 13、22、23；附录 U | `api/server.py` 的 `/metrics` | `test_api.py` |
| 审计回归 | 12、42；附录 S | `promql/`、`tsdb/`、`scrape/` | `test_audit_regressions.py` |
| Exemplar/追踪/Profiling | 4、44；附录 R | `exposition.py`、`scrape/manager.py`、`api/server.py` | `test_exposition.py`、`test_api.py` |
| HTTP API/UI/自动化 | 41、45；附录 J/V | `api/server.py` | `test_api.py` |
| 部署/GitOps | 23、24、46；附录 P/X | YAML 模板/外部组件 | `check_docs.py`（YAML/链接） |
| 指标治理 | 27、47；附录 D/Z | 文档/流程 | `check_docs.py` |
| 监控有效性/成熟度 | 25～28、47、49～51；附录 AJ/U/S | 文档/PromQL 取证查询 | `check_docs.py`、PromQL 示例 |
| 性能基准/容量 | 22、48；附录 F/Y | `promql/engine.py`、`tsdb/*` | 示例/文档 |
| 规则单元测试 | 9、28、42；附录 W | `promtool.py` | `test_promtool.py` |
| 客户端埋点 | 5；附录 W | 外部客户端库 | 文档检查 |
| Exporter/探针/中间件 | 5、11、12、24；附录 AA、T | 外部 exporter/blackbox | 文档检查、PromQL 示例 |
| 上游源码导读 | 13～17、31～43；附录 AB | 上游 Go 仓库 | 文档检查 |
| 合规/数据生命周期/审计 | 21、23、27、47、49；附录 L | `api/server.py` 的 admin 端点、tombstone/快照 | `test_api.py`、`test_tsdb.py` |
| 工具链/CI | 9、23、28、46；附录 AC | `promtool.py`、外部工具 | `test_promtool.py` |
| 自建 Exporter | 5、12；附录 AA、AD、W | `exposition.py`、外部客户端库 | `test_exposition.py` |
| 综合实战 | 50、51；附录 C、F、S、Z | `examples/config/capstone.yml`、`examples/rules/capstone*.yml` | `test_promtool.py`、capstone 规则测试 |
| 3.14–3.16 迁移 | 20、23；附录 AE | 外部版本/配置 | 文档检查、配置校验 |

## 4. 数字声明

| 项 | 数量 | 来源 |
|---|---|---|
| 章节 | 56 | `chapters/01..56` |
| 附录 | 36 | `appendices/A..AJ` |
| 测试函数 | 510 | `grep '^def test_'` |
| pytest 用例 | 528 | `pytest --collect-only` |
| 示例场景 | 10 | `examples/run_all.py` |
| promtool 规则测试 | 3 | demo 2 + capstone 1 |
| PromQL 函数 | 75 | `FUNCTIONS` 注册表 |
| PromQL 聚合 | 14 | `ast.AGGREGATORS` |
| relabel 动作 | 11 | `RELABLE_ACTIONS` |

> README 声明 528 passed / 10 / 56+36，check_docs 会重新计算并校验。若新增测试/示例/章节，必须同步 README 与本文。

## 5. 已知缺口

### 5.1 功能缺口

- 无 native histogram / remote write 2.0 / 3.16 `query-cost` 成本估算与上限；
- 无 OpenMetrics 2.0 抓取、XOR2 编码、Unix socket 抓取；
- PromQL 未实现原生直方图函数（`histogram_count/sum/avg/fraction/stddev/stdvar`）、`info()`、`smoothed`/`anchored`、时长表达式与 `fill` 修饰符；已实现的 3.x 新函数见附录 A/O；
- OTLP 只实现 HTTP JSON 子集：无 protobuf、无 exponential histogram/NHCB、无 exemplar 持久化、scope attributes 不摊平、delta 状态不持久化；
- 无 Kubernetes/EC2/Consul SD；
- 无 TLS server/basic auth/多租户；
- 无完整 Alertmanager（路由/silence/gossip）；
- `mini_prometheus/promtool.py` 只实现 `check rules` 与 `test rules` 的子集，无 `--junit`、无全部 promtool 子命令；
- exemplar 只挂在 target 状态上，没有 TSDB 环形缓冲/WAL 持久化；
- 无完整 Go template；
- 无 out-of-order head；
- 无压缩/缓存优化；
- `query_offset` 只实现"查询时间后移、写入时间不变"的主语义；没有实现 Prometheus 对 stale marker 按 offset 回填的细节。

### 5.2 性能缺口

- 线性索引（无 postings）；
- 每目标一线程；
- 查询物化结果；
- Python 对象开销；
- 未做百万序列压测。

### 5.3 合规/安全说明

- 第 49 章是**工程控制映射**，不构成法律意见；具体义务须由法务/合规确认。
- Mini 只有 delete/tombstone/snapshot 的教学实现，没有认证、RBAC、查询审计、多租户、加密和远端删除传播，不能作为合规系统使用。

这些缺口在附录 G 与第 43 章明确记录；Mini 不作为生产系统。

## 6. 缺陷复盘

开发/验证过程中发现并修复的真实缺陷：

1. **查询封死开放 chunk**：`samples()` 调用 `seal()`，后续 append 报错；改为临时快照解码。回归：读后写。
2. **HTTP body 被消费**：`_params()` 读取 body 后未保存，remote write/read 读到空；改为 `_read_body()` 缓存。回归：remote JSON/protobuf/read。
3. **compaction 保留 tombstone**：合并后空间不释放；合并时不再传 tombstone。回归：clean tombstones。
4. **retention 失效**：默认合并不相邻 block，maxTime 变新；默认只合并重叠 block。回归：retention 删除。
5. **OpenMetrics/引号指标名**：解析器缺状态；补 `# EOF`/`{"utf8",...}`。回归：exposition。
6. **限制时机**：sample_limit 在部分写入后才触发；改为解析阶段截断、失败不 append。回归：limit + up=0。
7. **snapshot 封死开放 chunk**：`BlockWriter.add_chunk()` 调用 `chunk.bytes()` 触发 seal，快照后 append 失败；新增 `snapshot_bytes()`，回归：snapshot 后继续 append。
8. **histogram_quantile 最高桶**：分位数落在 `+Inf` 桶时返回 `+Inf`，真实 Prometheus 返回次高有限桶的上界；测试曾把错误行为写成断言。回归：0.99 分位 + 只有 +Inf 桶。
9. **group_left/group_right 基数反向**：实现和测试把"唯一侧"弄反；改为 group_left 右侧唯一、group_right 左侧唯一，补重复侧报错回归。
10. **区间/回看/子查询双闭**：`[5m]`、lookback、子查询包含左边界，子查询还按相对起点对齐；改为 3.x 的左开右闭 + 绝对 step 网格。回归：`count_over_time` 边界、`[1m:15s]` 网格。
11. **OpenMetrics 时间戳单位**：按毫秒解析 OpenMetrics 时间戳，实际是浮点秒；按方言解析，exemplar 同步。回归：秒级样本/exemplar。
12. **函数级偏差**：`changes()` 把 `NaN→NaN` 计为变化；`label_replace()` 只替换第一个匹配。回归：双 NaN、重复匹配。
13. **WAL 尾部重复计量**：torn tail 的 `truncated_bytes` 在错误分支和循环末尾各加一次；改为只由统一出口累加。回归：14 字节帧头越界尾部恰好计 14。
14. **hashmod 摘要**：用完整 128 位 MD5 取模，与 Prometheus 的前 8 字节大端 uint64 不一致；改为上游算法。回归：`MD5("bar")` 的固定分片值。
15. **IEEE 边界异常**：`0^-1`、幂溢出、`Inf%2` 触发 Python `ValueError/OverflowError`；按 Go/IEEE 语义映射为 ±Inf/NaN。修复中还发现 `ln` 等被 lambda 包装，导致 `fn is math.log` 特判失效。回归：标量/向量边界表达式。
16. **NaN 比较**：比较函数对 NaN 一刀切返回 false，导致 `NaN != 1` 被过滤；改为 `==`/`!=` 使用 IEEE 语义、排序比较遇 NaN 为 false。回归：NaN 标量/向量比较。
17. **sort_desc 的 NaN 位置**：整体反转把 NaN 排到最前；改为有限值按方向排序、NaN 始终末尾。回归：sort/sort_desc 升序/降序。
18. **`sum by (__name__)` 丢名**：聚合键无条件丢弃 `__name__`；改为 `by` 保留显式列出的标签，`without` 仍丢弃。回归：按指标名聚合。

### 6.1 第二轮独立审计（16 项，2026-11-26）

独立审计逐条对照上游语义，发现并修复 16 个"测试全绿但行为错误"的问题；回归集中在 `tests/test_audit_regressions.py`：

1. `label_replace` 未锚定/无匹配写 dst/空替换不删除 → 全匹配 + Go 展开 + 空值删除；
2. 标量-标量 `bool` 比较返回 500 → 返回 0/1 scalar；
3. `double_exponential_smoothing` 初始化/趋势公式错误 → 对齐 `s0=0,s1=v0,b=v1-v0`；
4. `series/labels/values` 忽略 block tombstone/时间范围 → 走 `block.select`；
5. snapshot 丢失 head tombstone → 写入 head block；
6. `group_left/right` include 标签缺失不删除 → `Labels.set("",)` 删除；
7. relabel 缺失捕获组字面化/抛 `IndexError` → 自定义 Go `Expand`；
8. `round` 负数半值远离零 → `floor(v/n+0.5)*n`；
9. append 错误把 `up` 置 0 → `up` 只看抓取成功；
10. 抓取失败不写 stale → 失败抓取视为空抓取写 stale；
11. `keep_firing_for` 早一个 interval 失效 → 从首次消失起算；
12. `min/max` 对 NaN 输入顺序敏感 → 有有限值忽略 NaN；
13. 重复序列错误在无匹配组时漏报 → 两侧全局 signature 检查；
14. `histogram_quantile(φ>1)` 先查 `+Inf` 返回 NaN → 先查 φ 边界；
15. `absent` 重复 matcher 抛错 → 按上游顺序 set/delete；
16. `sort_by_label` 字典序、NaN 聚合参数崩溃 → 自然排序 + NaN 返回空/NaN。

### 6.2 文档语义修正（已同步代码）

- **抓取失败与 stale**：早期文档写成"抓取失败保留旧值到 lookback"。真实 Prometheus 把失败抓取视为空抓取，会为上次成功抓取的序列写 stale；lookback 主要兜底远端写/显式时间戳/未写标记的场景。第 3、12 章已改写，Mini 也已改为失败写 stale（审计修复 #10）。
- **append 错误与 up**：真实 Prometheus 中乱序/重复样本被丢弃时 `up` 仍为 1；Mini 已改为 `up=1`、把 append 错误单独计数（审计修复 #9）。第 12 章新增 12.13 说明。

### 6.3 本轮新增：promtool 风格规则单元测试

新增 `mini_prometheus/promtool.py`，复用 `RuleManager`、PromQL 引擎和真实 TSDB：

- `check rules`：校验规则文件；
- `test rules`：支持 `input_series` 的 `_`/`stale`/`a+bxN` 展开、`promql_expr_test`、`alert_rule_test`、`for`/`keep_firing_for` 状态机；
- CLI：`python -m mini_prometheus.promtool test rules <file>`；
- 回归：`tests/test_promtool.py`（20 个用例），示例 `examples/09_rule_unit_tests.py`。

与官方 promtool 的差异：只实现教学所需的子集，无 JUnit 输出/全部子命令/完整 YAML 校验；以本仓库测试为准。

### 6.4 第三轮独立审计（17 项，2026-11-26）

第三轮审计专挑"测试全绿但语义不对"的边角，新增回归集中在 `tests/test_third_audit_regressions.py` 与 `tests/test_api.py`：

1. `x > bool y + 1` 丢失 `bool`（RHS 含二元运算时）→ 修饰符改为显式返回值，不再用实例状态；
2. `abs`/`ceil`/`-x`/`clamp*`/`round` 保留 `__name__` → 转换类函数与一元负号统一丢名；
3. Go `$1x` 展开：把 `$1x` 当 group 1 + `x` → 按最长字母数字名解析（未定义组展开为空）；
4. `clamp*` 把 NaN 变成边界 → Go `math.Max/Min` 语义，NaN 传播；
5. `min/max_over_time` 对 NaN 顺序敏感 → 忽略 NaN，全 NaN 才返回 NaN；
6. `round(v, 0)` 返回 v、非有限 `to_nearest` 丢序列 → 返回 NaN 样本但保留序列；
7. `last_over_time` 丢 `__name__` → 按上游特例保留；
8. `sample_limit` 在 metric relabel 之前生效 → 改为 relabel 之后计数；
9. `honor_labels: false` 仅在值冲突时改 `exported_` → 只要标签存在就改，并处理 `exported_exported_`；
10. `/series`、`/labels`、`/label/*/values` 默认 `0..now` → 改为 MinTime/MaxTime，未来/负时间戳可见；
11. `query_range` 11,000 点上限差二 → 改为 `(end-start)/step + 1`；
12. Head 用 `-1` 当"无样本"哨兵 → 改用 `None`，负时间戳可正常追加/去重；
13. `a > on(x) bool b` 被接受 → 强制 `bool` 在 `on/ignoring/group` 之前；
14. `quantile` 对 NaN 排序依赖输入顺序 → NaN 排在最前；
15. `double_exponential_smoothing` 单样本返回原值、非法参数仅 warning → 单样本跳过、非法参数抛执行错误；
16. `topk/bottomk` 的 NaN 排序：对照上游确认两者都优先有限值，新增 `bottomk` 回归防止误改成"NaN 最前"；
17. 负时间戳 XOR chunk 解码未符号扩展 → 64 位时间戳按有符号解释。

### 6.5 本轮新增：UTF-8 名称与 OTLP 接收（2026-11-26）

**UTF-8 名称**：

- 数据模型新增 legacy/UTF-8 分类与统一格式化（`is_legacy_*`、`quote_*`、`format_labels`）；
- exposition 支持 `{"metric.name", "label.name"="value"}`、`# HELP "name"`、`# TYPE "name"`；
- PromQL 支持引号指标名、引号标签名、引号分组标签，并保留"非空 matcher"约束；
- 修复了一个实现陷阱：花括号内首个引号 token 要通过"后面是否跟 `=`"区分指标名与标签名，否则 `{"service.name"="api"}` 会被误判为指标名；
- 回归 18 个用例（`tests/test_utf8_names.py`），包括 `sum by ("service.name")` 与 `label_replace` 的端到端求值。

**OTLP/HTTP JSON 接收**：

- 新增 `mini_prometheus/otlp.py`：resource/scope 标签、名称/单位后缀、`_total`、gauge/sum/histogram/summary、delta 状态机；
- HTTP 层：`POST /api/v1/otlp/v1/metrics`，JSON/gzip，protobuf 明确 400，未启用 404，成功裸 200；
- 安全/健壮性：丢弃 `__name__`/`__*` 属性与非法 UTF-8（含孤立代理项）、属性名冲突 warning、流式 gzip 解压上限（不解压完整炸弹）、负/非法 `Content-Length` 拒绝、连接超时、delta 状态上限；
- 并发/原子性：`translate()` 串行化，delta 状态按请求事务回滚；失败请求不会污染下一次重试；
- delta 语义：同一时间戳的重试丢弃、乱序点丢弃、start time 变化重置、直方图状态按指标名隔离、桶布局变化在修改状态前报错；
- 标签策略：`NoUTF8EscapingWithSuffixes` 保留 UTF-8 标签名，`UnderscoreEscapingWithSuffixes` 转下划线；
- 自身指标：`prometheus_otlp_received_samples_total`、`..._metric_points_total`、`..._delta_resets_total`、`..._append_errors_total`；
- 回归 29 个用例（`tests/test_otlp.py`）：累积/delta/reset/重试/回滚/多指标状态隔离、单位后缀、资源映射、直方图/summary、exponential 拒绝、HTTP e2e/gzip/404/400；
- 示例 `examples/10_otlp_ingest.py` 实际启动 Mini、POST OTLP、查询 UTF-8 指标名与直方图分位数。

**明确未做**（写入附录 AG 与 README）：OTLP/protobuf、exponential histogram/NHCB、exemplar 持久化、真实 OTel Collector 端到端。Mini 的 OTLP 是教学实现，不作为生产接收端。

### 6.6 第四轮独立审计（10 项代码 + 文档事实修正，2026-11-26）

第四轮把"测试全绿"与上游语义逐条对拍，新增回归 9 个用例（`tests/test_promql_eval.py`、`tests/test_tsdb.py`、`tests/test_api.py`、`tests/test_otlp.py`）：

1. **一对一 `on()`/`ignoring()` 不限制结果标签**：`a / on(job) b` 仍保留 `instance`，`ignoring(instance)` 也不删除；改为上游 `resultMetric` 语义（`on` 只保留列出标签，`ignoring` 删除列出标签，统一丢弃 `__name__`）。
2. **比较运算的 `group_left/right` 丢失 `include` 标签**：`a > on(job) group_left(version) b` 不返回 `version`；改为所有向量运算统一套用 resultMetric，bool/过滤分支也带 include。
3. **`@ 0`/负 offset 时区间函数用错窗口**：`matrix.end_time == 0` 被当成假值，回退到求值时间，`rate(c[5m] @ 0)` 得到负数；改为只判断 `range_ms`。
4. **OTLP delta histogram 同桶数不同边界仍累加**：只比较桶数量，`[1,2]` 改成 `[10,20]` 时旧观测混入新桶；改为比较完整 `bounds`。
5. **`timestamp()`/日期函数把样本时间 0 当缺失**：`source_timestamp or ...` 在 0 时回退到求值时间；改为显式 `is not None`。
6. **Head min/max 用 0 当"未设置"**：epoch 样本使 `min_time` 变成 60000，全负样本使 `max_time` 变成 0；改为 `None` 哨兵。
7. **`query_range` 11k 点上限差一**：上游只在 `(end-start)/step > 11000` 时拒绝，Mini 拒绝了 11,001 点的合法请求；改为同样的比较，并补 11,001/11,002 边界测试。
8. **`1 / -0` 丢失符号**：只按分子符号返回 `+Inf`；改为 IEEE 符号（`sign(left) XOR sign(right)`）。
9. **`absent` 重复等值 matcher / 括号选择器标签错误**：`absent(nope{a="1",a="2"})` 返回 `{a="2"}`、`absent((nope{job="a"}))` 返回 `{job="a"}`；改为上游"首个 matcher 设置、后续同名删除"且不递归 `ParenExpr`。
10. **相同重复样本被重复计数/WAL/通知**：chunk 正确忽略，但 `Head.num_samples`、`stats.samplesAppended`、WAL 记录、watcher 都各多一次；改为 `HeadSeries.append` 返回是否真正写入，TSDB 只在写入时记账。

同轮文档事实修正：

- `min()`/`max()` 时长函数改名发生在 **3.13.0**，不是 3.14；3.14 只是默认开启；
- `holt_winters` 在 3.0 被移除，**上游没有别名**（Mini 为教学保留别名）；
- quickstart/examples 的 `histogram_quantile` 改为 `sum by (le) (rate(...))`，避免教"全历史累计分位"；
- 3.16 子查询样本统计口径改为更准确的描述（计入存储读取、排除物化重复计数）；
- Gartner MQ 名单软化为"多家 Leader，以原文为准"；
- 补充 `__unix_socket__` 的 SSRF/本地 socket 安全提示（第 12 章、附录 L）；
- exemplar 默认开启改为"较新 3.x flag deprecated；早期版本需确认"；
- 统一示例镜像为 `v3.13.5`，修正附录 V/M 的 Grafana 指向；
- 第 35/39 章补充 Mini 的 index/compaction 计划、目标状态机与 relabel 顺序。

## 7. 验证方法

- 单元测试优先验证语义（不是只测 happy path）；
- 属性测试：chunk/WAL/snappy/protobuf roundtrip；
- 集成测试：真实 localhost exporter + scrape + TSDB；
- e2e：启动 App/HTTP server，轮询 `/api/v1/query` 与 `/targets`；
- 规则测试：用 `mini_prometheus.promtool` 跑 input series + alert 状态机；
- 静态检查：章节/小节编号/链接/YAML/编译；
- 关键语义对照官方文档/源码，避免"测试编码错误行为"；
- 每个缺陷都有回归测试，避免"修了又坏"。

## 8. 复现

```bash
cd prometheus-tutorial
python -m pip install -r requirements.txt
python -m pytest
python examples/run_all.py
python check_docs.py
```

如果 pytest 计数与 README 不一致：

1. 运行 `python -m pytest --collect-only` 看实际收集数；
2. 更新 README/本文件的数字；
3. 重新运行 `python check_docs.py`。

## 9. 结论

- 教程结构完整（56 章 + 36 附录），内部引用/链接/YAML/小节编号通过检查。
- Mini-Prometheus 可运行、可测试，覆盖 Prometheus 的核心语义与工程路径。
- 测试全部通过，示例全部可运行。
- 生产使用请以官方 Prometheus/Alertmanager/Thanos/Mimir/VictoriaMetrics 为准。
