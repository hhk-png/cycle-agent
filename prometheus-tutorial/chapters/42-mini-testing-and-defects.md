# 第 42 章 Mini 的测试策略与缺陷复盘

## 42.1 测试分层

Mini 的测试按"失败模式"而非"文件结构"组织：

```text
tests/
  test_timeutil.py        时间/持续时间/stale NaN
  test_labels.py          标签/匹配器
  test_exposition.py      文本/OpenMetrics
  test_chunk.py           XOR 编解码
  test_tsdb.py            head/WAL/block/compaction/retention
  test_wal.py             WAL 帧/恢复/损坏
  test_promql_parser.py   词法/语法/类型
  test_promql_eval.py     选择/计算/匹配/函数
  test_scrape.py          SD/relabel/抓取/失败/stale
  test_relabel.py         动作/锚定/展开
  test_api.py             HTTP 契约/错误/远程
  test_remote.py          Snappy/protobuf/队列
  test_config.py          配置解析/校验/拒绝
  test_rules.py           规则/告警/通知
  test_promtool.py        规则单元测试器
  test_audit_regressions.py / test_third_audit_regressions.py  审计回归
  test_capstone_examples.py  综合实战配置/规则
  test_utf8_names.py      UTF-8 名称/引号语法
  test_otlp.py            OTLP/JSON 翻译/delta/HTTP
  test_app_e2e.py         真实 HTTP + 线程
  test_cli.py             子命令/OTLP flags
```

单元测试覆盖语义，集成测试覆盖组件交互，e2e 覆盖真实进程。

## 42.2 属性测试

- Chunk roundtrip：随机时间间隔/值，解码必须等于输入；
- WAL replay：随机样本数/多段，重放后一致；
- Snappy roundtrip：随机字节；
- Protobuf encode/decode：随机序列/负时间戳；
- 聚合：结果标签 ⊆ 分组标签；
- 选择器：结果必须匹配 matcher。

属性测试比"固定期望值"更能发现边界 bug。

## 42.3 差分测试

可行时对比：

- 手算 `rate`/`increase` 与引擎；
- 不同窗口/重置/边界；
- Mini 与真实 Prometheus（同一组查询）；
- 不同 API 客户端（curl/Grafana）。

Mini 没有内置差分测试框架，但教程中的手算例子可作为基准。

## 42.4 故障注入

- HTTP 500/超时/畸形 body；
- 目标消失/relabel 丢弃；
- WAL 尾部截断/CRC 中间损坏；
- 磁盘/锁冲突（TSDB 锁）；
- Alertmanager 4xx/5xx；
- remote write 队列/重试；
- sample/label/body limit；
- 重复时间戳/乱序。

## 42.5 真实缺陷复盘

### 缺陷 1：查询封死开放 chunk

现象：抓取正常，但 Grafana 查询后下一次抓取报 `cannot append to a sealed chunk`。

根因：`XORChunk.samples()` 对开放 chunk 调用 `seal()`。

修复：用临时快照解码，不改变 `_sealed`。

测试：读后写回归。

教训：测试要覆盖"查询与写入交错"，而不是孤立测试。

### 缺陷 2：POST body 被消费

现象：remote write/read 请求超时/解析空 body。

根因：`_params()` 读取 body 后只用于 form，没有保存；`_remote_write()` 再次 `read()` 得到空。

修复：`_read_body()` 缓存实例级 body。

测试：refire remote write JSON/protobuf/read。

教训：HTTP body 是一次性流；任何中间层读取都必须缓存/传递。

### 缺陷 3：compaction 后 tombstone 未清

现象：`clean_tombstones()` 后空间未释放，`tombstones()` 仍非空。

根因：合并时把旧 tombstone 传给新 block，而样本已经过滤。

修复：合并写 block 时不传 tombstone。

测试：clean 后 `tombstones() == {}`。

教训：压缩的"输入已过滤"与"输出元数据"要一致。

### 缺陷 4：retention 失效

现象：配置 10s retention，旧 block 不删除。

根因：compaction 默认合并了不相邻 block，合并后的 maxTime 变新，retention 阈值不再满足。

修复：默认只合并时间重叠的 block，force 才全合并。

测试：两个 disjoint block 在 retention 后都删除。

教训：compaction 策略会影响 retention 粒度；测试要覆盖时间范围。

### 缺陷 5：exposition 严格性

现象：`# TYPE` 在样本后出现未被拒绝；`{"utf8.name",...}` 解析失败。

修复：补状态机/引号指标名；测试覆盖。

教训：解析器的"宽松"会把 exporter bug 变成静默数据丢失。

### 缺陷 6：样本限制前已合并

现象：sample_limit 触发后仍写入了部分样本。

修复：解析阶段 `max_samples` 截断，失败时整次抓取不 append；测试验证 up=0、无部分写入。

教训：限制要在写入前生效，而不是写入后检查。

### 缺陷 7：snapshot 封死开放 chunk

现象：执行 `/api/v1/admin/tsdb/snapshot` 后，下一次抓取报 `cannot append to a sealed chunk`。

根因：`BlockWriter.add_chunk()` 调用 `chunk.bytes()`，后者会 seal 开放 chunk；快照把 head 中的 chunk 封死，但 head 仍持有它。

修复：新增 `XORChunk.snapshot_bytes()`，快照/写入 block 时不改变开放状态；回归测试 `test_snapshot_does_not_break_live_append`。

教训：任何"读取/复制"路径都不能改变写入对象的生命周期状态；快照是典型的读路径。

### 缺陷 8：histogram_quantile 的最高桶语义

现象：分位数落在 `+Inf` 桶时，Mini 返回 `+Inf`，而真实 Prometheus 返回次高有限桶的上界；测试也把这个错误行为写成了断言。

修复：只在有限桶中查找 rank，落在 `+Inf` 桶时返回 `cumulative[-2][0]`；补"只有 +Inf 桶返回 NaN"的用例。

教训：**测试会编码错误行为**。断言来自实现而不是规范时，全绿只证明自洽，不证明正确；分位数这类边界必须对照官方文档/源码。

### 缺陷 9：group_left / group_right 基数反向

现象：`group_left` 被实现成"左侧唯一、右侧可多条"，`group_right` 相反；测试也按反向语义编写，因此一直通过。

修复：`group_left` 左侧可多条、右侧必须唯一；`group_right` 右侧可多条、左侧必须唯一。补右侧/左侧重复的报错用例。

教训：多对一/一对一是 PromQL 最容易记反的语义；文档、实现、测试三处必须同时校正。

### 缺陷 10：区间/回看/子查询当成双闭区间

现象：`[5m]`、lookback 和子查询都包含左边界，且子查询从窗口起点相对对齐；与 Prometheus 3.0 的左开右闭和绝对 step 网格不一致。

修复：选择器查询 `mint + 1` 并过滤 `> mint`；子查询用 `(start // step + 1) * step` 对齐绝对网格。相关测试改成 3.x 语义（如 `count_over_time` 少一个点、`[1m:1m]` 只产生一个点）。

教训：升级 Prometheus 3 时，"少一个样本"不是数据丢失，而是区间定义变了；测试必须锁定版本语义。

### 缺陷 11：OpenMetrics 时间戳单位

现象：OpenMetrics 的时间戳按毫秒解析，实际规范是秒（可带小数）；1700000000 被解释成 1970 年。

修复：按方言解析——文本格式整数毫秒，OpenMetrics 浮点秒 ×1000；exemplar 同样处理。补样本/exemplar 的秒级时间戳用例。

教训：协议方言的差异不只在 `# EOF` 和 `# UNIT`，时间戳单位更隐蔽，且会静默产生错误时间轴。

### 缺陷 12：两个函数级偏差

现象：`changes()` 把 `NaN → NaN` 计为变化；`label_replace()` 只替换第一个匹配，而 Prometheus 的 `ReplaceAllString` 替换全部。

修复：`changes` 显式排除双 NaN；`label_replace` 改用 `regex.sub`。各补一个回归用例。

教训：小函数也值得逐条对照规范；"NaN 不等于自身"的 Python 语义会制造隐蔽的计数偏差。

### 缺陷 13：WAL 尾部重复计量

现象：尾部半条记录的 `truncated_bytes` 在错误分支和循环结束处各加一次，统计值翻倍。

修复：错误分支只 `break`，由统一的尾部计量累加一次；回归测试构造"帧头完整但声明长度越界"的 14 字节尾部，断言恰好 14。

教训：错误路径也需要精确的账本；"只用于判断真值"的统计量一旦被展示或告警，双计数就会变成误导。

### 缺陷 14：hashmod 用了完整 128 位摘要

现象：`hashmod` 对完整 MD5 十六进制取模，真实 Prometheus 取前 8 字节的大端 uint64；同样的目标会被分到不同 shard。

修复：改用 `int.from_bytes(md5(...).digest()[:8], "big") % modulus`，并用 `MD5("bar")` 的已知值锁定结果。

教训：分片算法必须与上游逐位一致，否则"看起来能分片"的配置在混合部署时会漏抓目标。

### 缺陷 15：IEEE 边界让 Python 异常泄漏

现象：`0 ^ -1`、`10 ^ 400`、`Inf % 2` 在 Go 里分别是 `+Inf`、`+Inf`、`NaN`，但 Python 的 `math.pow`/`math.fmod` 会抛 `ValueError`/`OverflowError`，直接变成查询错误。

修复：捕获异常并按 IEEE 语义映射（0 的负次幂为 +Inf；溢出为 ±Inf；Inf 取模为 NaN）。修复过程中还发现 `ln/log2/log10` 被 lambda 包装，导致 `_go_math` 的 `fn is math.log` 特判失效；改为直接注册 `math.log` 等函数。

教训：跨语言实现数学函数时，边界值比正常值更容易暴露差异；要把异常路径也纳入差分测试，并注意"包装函数"会破坏基于函数身份的特判。

### 缺陷 16：NaN 比较被一刀切

现象：比较函数只要遇到 NaN 就返回 false，导致 `NaN != 1` 被错误过滤；IEEE/Go 语义是 `NaN != x` 为 true、`NaN == x` 为 false。

修复：`==`/`!=` 直接使用 Python 的 IEEE 语义，只有排序比较（`<`/`>`/`<=`/`>=`）在 NaN 时返回 false。

教训：NaN 不是"空值"，不能用一个 truthiness 判断统一处理；比较运算要逐运算符对照。

### 缺陷 17：sort_desc 把 NaN 排到最前

现象：`sort()` 用 `(isnan, value)` 排序后整体反转，导致 `sort_desc()` 把 NaN 放到最前面；Prometheus 对 sort/sort_desc 都把 NaN 放在末尾。

修复：把有限值与 NaN 分开，有限值按方向排序，NaN 始终追加在末尾。回归：升序/降序都断言 NaN 最后。

教训："逆序"不是简单反转：NaN 的排序位置是独立规则，和方向无关。

### 缺陷 18：sum by (__name__) 丢指标名

现象：聚合键生成时无条件丢弃 `__name__`，导致官方推荐的 `sum by (__name__)` 保留指标名的写法失效。

修复：`by` 分组只保留列出的标签，包括显式列出的 `__name__`；`without` 仍始终丢弃指标名。回归：按指标名聚合两个指标。

教训：`__name__` 是标签也是身份；"默认丢弃"与"显式保留"必须区分，不能用一个 `discard` 覆盖两种意图。

### 缺陷 19–34：第二轮独立审计发现的 16 个语义偏差

第二轮审计不再看"测试是否通过"，而是逐条对照 Prometheus 上游语义/源码。16 个问题全部有最小复现、修复和回归测试，集中记录在 `tests/test_audit_regressions.py`。

| # | 问题 | 修复 | 回归 |
|---|---|---|---|
| 19 | `label_replace` 用未锚定 `sub`，无匹配也写 dst，空替换不删除 | 全匹配 + Go `$` 展开 + 空值删除 dst | `test_label_replace_*` |
| 20 | 标量-标量比较带 `bool` 走算术分支，返回 500 | 比较走 `_compare`，返回 0/1 scalar | `test_scalar_scalar_comparison_uses_bool` |
| 21 | `double_exponential_smoothing` 初始化/趋势公式与上游不同 | 按 `s0=0,s1=v0,b=v1-v0` 重写 | `test_double_exponential_smoothing_matches_prometheus` |
| 22 | `series/labels/values` 忽略 block tombstone 与时间范围 | 改走 `block.select`，按样本过滤 | `test_series_and_labels_respect_tombstones_and_time_range` |
| 23 | snapshot 丢失 head tombstone，恢复后删除的数据复活 | 把 head tombstone 写进 head block | `test_snapshot_preserves_head_tombstones` |
| 24 | `group_left/right` 的 include 标签在 one 侧缺失时不删除 | `Labels.set("",)` 改为删除语义 | `test_group_left_deletes_include_label_missing_on_one_side` |
| 25 | relabel 缺失捕获组展开成字面 `$2`，命名缺失抛 `IndexError` | 自定义 Go `Expand`，缺失组为空 | `test_relabel_missing_capture_group_expands_empty` |
| 26 | `round(-1.5)` 远离零取整为 -2 | 统一 `floor(v/n+0.5)*n` | `test_round_ties_toward_positive_infinity` |
| 27 | 抓取成功但 append 错误把 `up` 置 0/health down | `up` 只看抓取成功，append 错误单独计数 | `test_duplicate_timestamp_across_scrapes` |
| 28 | 抓取失败不写 stale，旧值保留到 lookback | 失败抓取视为空抓取，写 stale | `test_failed_scrape_writes_stale_for_previous_series` |
| 29 | `keep_firing_for` 从上次 seen 起算，早一个 interval 失效 | 从首次消失的求值点起算 | `test_keep_firing_for_does_not_expire_one_interval_early` |
| 30 | `min/max` 对 NaN 依赖输入顺序 | 有有限值时忽略 NaN，全 NaN 才 NaN | `test_min_max_ignore_nan_when_finite_exists` |
| 31 | 重复序列错误只在另一侧有同组时才报 | 先对两侧 signature map 做全局重复检查 | `test_duplicate_series_error_even_without_matching_group` |
| 32 | `histogram_quantile(φ>1)` 先查 `+Inf` 桶，返回 NaN | 先检查 φ 边界，再查桶 | `test_histogram_quantile_phi_above_one_returns_inf_without_inf_bucket` |
| 33 | `absent` 的标签合成会因重复 matcher 抛错 | 按上游顺序 set/delete 推导标签 | `test_absent_drops_labels_with_non_equality_matcher` |
| 34 | `sort_by_label` 字典序（`a10 < a2`）；NaN 聚合参数崩溃 | 自然排序 + NaN 参数返回空/NaN | `test_sort_by_label_uses_natural_order`、`test_topk_nan_parameter_is_empty` |

这轮审计的教训：

- **测试会编码错误行为**：`label_replace` 的旧测试把"未锚定、全部替换"写成了注释和断言；对照上游源码才发现整个测试方向错了。
- **"没报错"不等于"对"**：`Labels.set(name, "")` 是 no-op，悄悄让 include/空替换/删除语义全部失效；一个底层语义错误会向上污染多个函数。
- **边界值要逐项对照**：`round` 的负数半值、`histogram_quantile` 的 φ 越界、NaN 的 min/max、`keep_firing_for` 的计时起点，都是"正常路径全绿、边界才暴露"。
- **读路径也要测**：snapshot、series/labels、group join 都是读路径，却会改变"恢复后/查询后看到什么"；不能只测写入路径。

### 缺陷 35–51：第三轮独立审计（17 项）

第三轮专门挑"测试全绿但边角语义不对"的问题，新增回归在 `tests/test_third_audit_regressions.py` 与 `tests/test_api.py`。代表性案例：

- **修饰符状态泄漏**：`x > bool y + 1` 的 `bool` 在解析 RHS 时被内层二元表达式重置，结果从 0/1 变成空向量；修复为把 `bool_modifier` 显式传给 `build_binary`。
- **函数丢名不彻底**：`abs`、`-x`、`clamp`、`round` 保留了 `__name__`，而 `last_over_time` 反而丢了名；按上游规则逐个修正。
- **Go 展开**：`$1x` 被错误展开为 group 1 + `x`；Go 实际按最长字母数字名解析，未定义组展开为空。
- **NaN 传播**：`clamp*` 把 NaN 变成边界值，`min/max_over_time` 对 NaN 顺序敏感；统一按 Go 的 NaN 语义处理。
- **时序**：`sample_limit` 在 metric relabel 之前计数，会误杀"先抓后删"的配置；改为 relabel 之后计数。
- **API 默认范围**：`/series` 等端点默认 `0..now`，未来/负时间戳样本不可见；改为 MinTime/MaxTime。
- **编码**：XOR chunk 的首时间戳按无符号解码，负时间戳回绕；补符号扩展。

完整清单与复现见 `VERIFICATION.md` 6.4。这轮再次说明：**"测试通过"只是起点，边界语义要逐条对照上游**。

## 42.6 测试数量与验证

当前测试套件包含 **510 个测试函数、参数化后 528 个用例**，全部通过；其中 `tests/test_promtool.py` 覆盖 20 个规则单元测试器用例，`tests/test_capstone_examples.py` 额外固化第 50～51 章的 capstone 配置与规则测试。准确数字以 `README.md` 与 `VERIFICATION.md` 为准（`check_docs.py` 会校验声明）。

三条验证命令：

```bash
python -m pytest
python examples/run_all.py
python check_docs.py
```

`check_docs.py` 检查：

- 章节编号/标题/小结/延伸阅读；
- 代码围栏/YAML 可解析；
- 内部链接；
- README 目录/计数；
- 测试/示例数量声明；
- 代码编译。

## 42.7 测试的局限

- 没有真实百万序列压测；
- 没有多线程竞争压测（TSDB 锁）；
- 没有真实 Kubernetes/云 SD；
- 没有 native histogram；OTLP 只测了 JSON 子集，没有 protobuf/NHCB/真实 Collector；
- exemplar 没有 TSDB 持久化；
- 没有跨版本兼容测试；
- e2e 依赖本地端口/时间，CI 需留超时。

这些局限在附录 G 中列出，避免把 Mini 的"全绿"当成生产就绪。

## 42.8 规则单元测试器

`mini_prometheus/promtool.py` 是本轮新增的工程化组件，解决"规则/告警改动没有快速反馈"的问题：

```bash
python -m mini_prometheus.promtool check rules rules/demo.yml
python -m mini_prometheus.promtool test rules examples/rules/demo_test.yml
```

实现要点：

- 解析 promtool 风格 YAML（`rule_files`/`evaluation_interval`/`tests`）；
- `input_series` 支持 `_`（缺失）、`stale`、`a+bxN`/`a-bxN`/`a*bxN`/`a/bxN`/`axN`；
- 为每个测试用例创建临时真实 TSDB，按 interval 求值规则；
- `promql_expr_test` 比较标签集/值（NaN/Inf 语义）；
- `alert_rule_test` 比较 firing 告警的标签/注解，覆盖 `for`/`keep_firing_for`；
- 失败时输出 missing/unexpected 差异，退出码 1；
- 复用 `RuleManager`/PromQL/TSDB，避免"测试用另一套语义"。

测试：

```bash
python -m pytest tests/test_promtool.py -q
python examples/09_rule_unit_tests.py
```

这是"Mini 也要工程化"的示例：功能不必多，但要有 CLI、错误信息、测试和示例。

## 本章小结

- 测试按失败模式分层：单元/属性/集成/e2e/故障注入。
- 属性测试覆盖编码/重放/协议；差分测试覆盖语义。
- 51 个真实缺陷说明：读写交错、HTTP body、压缩元数据、retention 粒度、解析严格性、限制时机、快照生命周期、分位数最高桶、group 基数、区间左开、OpenMetrics 时间戳、函数级偏差、WAL 尾部计量、hashmod 摘要、IEEE 边界异常、NaN 比较、sort_desc 的 NaN 位置、`by (__name__)`，第二轮审计的 `label_replace`/relabel 展开、scalar bool、平滑算法、block tombstone、snapshot tombstone、group include、round、append 错误 `up`、失败 stale、`keep_firing_for`、min/max NaN、重复序列、φ 越界、absent、自然排序、NaN 聚合参数，以及第三轮审计的 bool RHS、转换函数丢名、Go `$1x` 展开、clamp NaN、`last_over_time` 保名、sample_limit 时机、honor_labels、默认时间范围、负时间戳与查询点数边界。
- 测试全绿不等于正确；要覆盖生命周期/边界/失败。
- Mini 的局限要明确记录。

## 延伸阅读

- 本教程第 28 章（测试方法）。
- `tests/`、`VERIFICATION.md`。
- 真实 Prometheus 的 `promtool test rules` 与 e2e 测试。
