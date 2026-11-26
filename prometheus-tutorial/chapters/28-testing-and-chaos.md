# 第 28 章 测试、验证与混沌工程

## 28.1 测试金字塔

```text
        e2e / 演练
      integration
    contract / API
  unit / property / rule tests
```

Prometheus 相关测试分几层：

1. **PromQL 单元测试**：给定合成序列，验证查询结果；
2. **规则测试**：`promtool test rules`；
3. **API 契约测试**：JSON 格式/状态码/错误类型；
4. **抓取集成测试**：exporter + scrape + TSDB；
5. **端到端**：启动完整进程，查询/告警；
6. **混沌/演练**：目标失败、磁盘满、Alertmanager 不可达；
7. **性能/容量测试**：序列数/查询延迟/摄入。

## 28.2 PromQL 单元测试

构造合成序列：

```python
q = InMemoryQuerier()
q.add_series(
    Labels({"__name__": "http_requests_total", "code": "200"}),
    [(i * 15_000, i * 2.0) for i in range(20)],
)
engine = Engine(q)
assert engine.query("rate(http_requests_total[1m])", 300_000).value.items[0].value == pytest.approx(2 / 15)
```

要覆盖：

- 空结果；
- 单样本/多样本；
- 计数器重置；
- 窗口边界；
- `offset`/`@`；
- 标签匹配/多对多；
- 直方图缺桶/非单调；
- NaN/Inf/stale。

## 28.3 规则测试

`promtool test rules` 的要点：

- `input_series` 用 `values` 语法构造；
- `promql_expr_test` 验证记录规则/查询；
- `alert_rule_test` 验证告警标签/注解/状态；
- `eval_time` 覆盖 pending/firing/resolve；
- 浮点用容差/近似。

没有 `promtool` 时，用 pytest + RuleManager 做等价测试：

```python
manager.evaluate(now)
assert [a.state for a in manager.alerts()] == ["pending"]
manager.evaluate(now + 30_000)
assert [a.state for a in manager.alerts()] == ["firing"]
```

## 28.4 API 契约测试

检查：

- 成功 envelope：`{"status":"success","data":...}`；
- 错误 envelope：`{"status":"error","errorType":...,"error":...}`；
- 时间戳格式（秒，浮点）；
- 值格式（字符串）；
- `resultType`：vector/matrix/scalar/string；
- 状态码：400/422/500/503；
- 空结果不是错误；
- 参数缺失/非法。

Mini 的 `tests/test_api.py` 覆盖 30+ 端点/错误场景。

## 28.5 抓取集成测试

用本地 exporter（`examples/demo_exporter.py`）+ 真实 HTTP：

- 成功抓取；
- HTTP 500；
- 解析错误；
- 超时；
- honor_labels/timestamps；
- 序列消失写 stale；
- 目标移除写 stale；
- OpenMetrics/exemplar；
- sample_limit/label_limit/body_size_limit。

不要 mock HTTP 解析器本身；mock 的是网络边界。

## 28.6 属性测试与差分测试

属性：

- `rate(x[5m])` 对常数序列为 0；
- `increase` 对无重置线性序列等于首尾差（外推后）；
- `sum by (job)` 的组数 ≤ 输入序列数；
- 聚合结果标签 ⊆ 分组标签；
- 序列化/反序列化 roundtrip（chunk/WAL/protobuf）。

差分：

- Mini vs 真实 Prometheus 对同一组查询的结果；
- 同一查询不同实现（如手算 vs 引擎）；
- 不同版本行为对比。

Mini 的 chunk/WAL 测试使用随机数据 roundtrip，属于属性测试。

## 28.7 故障注入

场景：

- exporter 返回 500/超时/畸形数据；
- 目标从 SD 消失；
- TSDB 磁盘满；
- WAL 尾部截断/CRC 错；
- Alertmanager 返回 4xx/5xx；
- remote write 队列满；
- 规则表达式除零/空数据；
- 查询超时/OOM。

验证：

- 进程是否存活；
- 指标是否反映故障（up/health/errors）；
- 是否写 stale；
- 是否丢数据/重复；
- 恢复后是否自动补发。

## 28.8 混沌工程

在 staging/生产小范围：

- 杀掉 Prometheus 进程（验证 WAL 恢复）；
- 断开 Alertmanager（验证队列/告警丢失）；
- 网络分区（验证 remote write 积压）；
- 磁盘 IO 延迟（验证 compaction）；
- 目标随机失败（验证告警/抑制）。

原则：

- 有假设、有监控、有回滚；
- 从小范围开始；
- 记录结果/改进。

## 28.9 Mini 的测试策略

测试目录：

```text
tests/
  test_labels.py
  test_exposition.py
  test_promql_parser.py
  test_promql_eval.py
  test_chunk.py
  test_tsdb.py
  test_wal.py
  test_scrape.py
  test_relabel.py
  test_rules.py
  test_api.py
  test_remote.py
  test_app_e2e.py
  test_cli.py
```

覆盖：

- 标签/解析/时间；
- PromQL 解析/求值；
- chunk roundtrip/压缩；
- head/WAL/block/compaction/retention/tombstone；
- 抓取/relabel/失败/stale；
- 规则/告警/notifier；
- API/错误/remote；
- 端到端/CLI。

真实缺陷（在开发中由测试暴露）：

- 查询开放 chunk 会 `seal()`，导致后续 append 报错；
- HTTP POST 的 body 被 `_params()` 读掉，remote write/read 读到空；
- compaction 合并后 tombstone 未清空，空间不释放；
- retention 合并了不相邻 block，导致过期数据删不掉；
- MSG 的 `TYPE` 在样本后出现未被拒绝。

这些案例说明：**测试要覆盖"读后写""HTTP body 生命周期""压缩后元数据""retention 粒度"**，而不只是 happy path。

## 28.10 文档验证

`check_docs.py` 检查：

- 章节编号/标题；
- 每章小结/延伸阅读；
- 代码围栏配对；
- YAML 围栏可解析；
- 内部链接存在；
- README 目录/计数；
- 测试/示例数量声明；
- 代码可编译。

`scripts/verify_all.py`（或三条命令）把测试/示例/文档串起来。

## 28.11 规则单元测试：promtool 与 Mini

规则单元测试的核心是"给定输入序列，断言求值结果/告警状态"，不依赖真实抓取/时钟。

最小测试：

```yaml
rule_files:
  - rules.yml
evaluation_interval: 1m
tests:
  - name: threshold boundary
    interval: 1m
    input_series:
      - series: 'up{job="api"}'
        values: '1 1 0 0'
    promql_expr_test:
      - expr: up
        eval_time: 2m
        exp_samples:
          - labels: 'up{job="api"}'
            value: 0
    alert_rule_test:
      - eval_time: 3m
        alertname: InstanceDown
        exp_alerts:
          - exp_labels:
              severity: warning
              job: api
```

运行：

```bash
promtool test rules examples/rules/demo_test.yml
python -m mini_prometheus.promtool test rules examples/rules/demo_test.yml
```

测试设计要点：

- 覆盖**边界**：刚好等于阈值、刚好满足 `for`、刚好恢复；
- 覆盖**标签**：聚合后标签是否符合告警路由预期；
- 覆盖**模板**：`$labels`/`$value` 渲染是否为空/错误；
- 覆盖**依赖**：recording rule 是否在 alert 前生效；
- 覆盖**失败**：缺失序列、stale、NaN、除零；
- 每个告警至少一个"触发"和一个"不触发"用例。

CI 集成：

```bash
promtool check rules rules/*.yml
promtool test rules examples/rules/demo_test.yml
python -m mini_prometheus.promtool test rules examples/rules/demo_test.yml
```

规则测试不能替代：

- 真实流量下的阈值校准；
- Alertmanager 路由/抑制/静默；
- 值班流程/runbook；
- 生产故障演练。

## 28.12 混沌实验设计

混沌工程不是"随机杀 Pod"，而是"用受控实验验证系统假设"。

### 28.12.1 实验结构

```text
假设：当 X 故障时，系统仍能维持 Y（SLO/错误率/延迟）
稳态指标：如何判断系统正常
故障注入：X（网络/磁盘/进程/依赖）
爆炸半径：影响哪些实例/租户/区域
中止条件：什么时候停止实验
观察：指标/日志/追踪/告警
结论：假设成立/不成立，改进项
```

### 28.12.2 故障类型

- 目标抓取失败/超时；
- remote write 接收端 500/慢；
- 磁盘满/IO 慢；
- Prometheus 重启/WAL replay；
- Alertmanager 不可达；
- 规则求值超时；
- 高基数指标突增；
- 时钟偏差/乱序样本。

### 28.12.3 爆炸半径

从最小开始：

1. 单实例/单 job；
2. 单集群 staging；
3. 生产小比例；
4. 生产全量（只在有充分把握/回滚时）。

### 28.12.4 游戏日

- 提前通知相关团队；
- 指定 IC/观察者/记录者；
- 设定中止条件；
- 记录时间线；
- 复盘改进项。

### 28.12.5 常见错误

- 没有稳态指标，无法判断"是否正常"；
- 爆炸半径过大；
- 没有中止条件；
- 在生产上做未演练的实验；
- 实验后不修复发现的问题；
- 把混沌当表演，没有假设/结论。

## 本章小结

- 测试分层：单元/规则/API/集成/e2e/混沌/性能。
- PromQL/规则测试要覆盖边界、重置、空数据、标签变化。
- 抓取测试 mock 网络边界，不 mock 解析器。
- 属性/差分测试适合验证编码/聚合/匹配。
- Mini 的真实缺陷说明测试要覆盖生命周期与元数据，而不只是结果。

## 延伸阅读

- Prometheus `promtool test rules` 文档。
- Google SRE Workbook 的混沌/演练章节。
- 本教程第 42 章（Mini 测试与缺陷复盘）。
