# 附录 AC Prometheus 工具链速查

Prometheus 生态的工具很多，名字相似、职责不同。本附录按"你会用它做什么"组织，给出常用命令、输入输出和踩坑点。命令参数以 Prometheus 3.x 为基线；不同小版本可能有增减，执行前用 `--help` 确认。

## AC.1 官方二进制

| 二进制 | 职责 | 典型用户 |
|---|---|---|
| `prometheus` | 抓取、TSDB、PromQL、规则、API、UI | 平台/SRE |
| `promtool` | 配置/规则校验、规则单测、TSDB 检查、查询、调试 | 平台/SRE/CI |
| `alertmanager` | 告警去重、分组、路由、抑制、静默、通知 | SRE |
| `amtool` | Alertmanager 的命令行客户端 | SRE |

四个二进制都是静态编译的 Go 程序，适合放进容器和离线环境。

## AC.2 promtool 子命令

### AC.2.1 配置与规则校验

```bash
# 语法 + 语义校验（会检查 rule_files 是否存在、scrape 配置是否合法）
promtool check config prometheus.yml

# 只做语法校验，不访问文件系统
promtool check config --syntax-only prometheus.yml

# 校验规则文件
promtool check rules rules/*.yml

# 校验抓取目标发现结果（会真正执行 SD）
promtool check service-discovery prometheus.yml <job_name>

# 校验 Web 配置（TLS/认证）
promtool check web-config web.yml
```

`check config` 常被误认为"只查 YAML"。它还会展开 `rule_files` glob、检查 duration、relabel 正则、remote write URL 等。CI 中应同时跑 `check config` 和 `check rules`。

### AC.2.2 指标格式校验

```bash
# 从 stdin 读取 exposition，报告命名/类型/重复等问题
curl -s http://target:9100/metrics | promtool check metrics
```

它检查的是**文本格式和命名约定**（如 counter 的 `_total` 后缀），不是业务语义。适合在 exporter 的 CI 中跑。

### AC.2.3 规则单元测试

```bash
promtool test rules tests/rules/*.yml

# JUnit 输出，便于 CI 展示
promtool test rules --junit tests/rules/*.yml > report.xml
```

测试文件结构：

```yaml
rule_files:
  - ../rules/api.yml
evaluation_interval: 1m
tests:
  - interval: 1m
    input_series:
      - series: 'http_requests_total{job="api",code="500"}'
        values: '0+10x10'
    alert_rule_test:
      - eval_time: 5m
        alertname: APIHighErrorRatio
        exp_alerts:
          - exp_labels:
              severity: page
              job: api
            exp_annotations:
              summary: "api error ratio high"
```

`input_series` 支持 `_`（缺样本）、`stale`、`a+bxN` 展开。Mini 的 `promtool test rules` 实现了这个子集，见第 9、28 章与附录 W。

### AC.2.4 TSDB 检查

```bash
# 列出 block
promtool tsdb list /var/lib/prometheus

# 分析 block 的 series/label/压缩情况
promtool tsdb analyze /var/lib/prometheus

# 导出样本（调试/迁移）
promtool tsdb dump /var/lib/prometheus > dump.txt

# 清理 tombstone（停机后执行）
promtool tsdb clean-tombstones /var/lib/prometheus

# 从 OpenMetrics 文件创建 block（历史数据回填）
promtool tsdb create-blocks-from openmetrics input.txt /var/lib/prometheus
```

`tsdb analyze` 是排查"哪个指标基数最高""标签值分布"的首选工具。它会读取索引，可能很慢；生产上建议对快照副本跑，避免和查询抢 IO。

### AC.2.5 查询与调试

```bash
# 即时查询
promtool query instant http://localhost:9090 'up'

# 区间查询
promtool query range --start=2026-11-01T00:00:00Z --end=2026-11-02T00:00:00Z \
  --step=1m http://localhost:9090 'sum(rate(http_requests_total[5m]))'

# 查序列/标签
promtool query series --match='{job="api"}' http://localhost:9090
promtool query labels http://localhost:9090 job

# 格式化 PromQL
promtool promql format 'sum(rate(http_requests_total{code=~"5.."}[5m]))by(job)'

# 抓取 pprof/trace（需要目标开启）
promtool debug pprof http://localhost:9090
promtool debug trace --seconds=5 http://localhost:9090
```

`promtool query` 适合脚本和 CI 冒烟测试；复杂分析仍用 API + 客户端库。

### AC.2.6 推送指标

```bash
# 把本地 exposition 推到远端（需要远端支持 remote write receiver）
promtool push metrics http://remote:9090/api/v1/write ./metrics.txt
```

用于测试 remote write receiver 或灌入合成数据。不是生产采集方式。

## AC.3 amtool

`amtool` 是 Alertmanager 的客户端，常用于演练和自动化：

```bash
# 查看当前告警
amtool --alertmanager.url=http://am:9093 alert query

# 添加/更新告警（测试路由）
amtool alert add alertname=Test severity=page --annotation=summary="test"

# 静默
amtool silence add --alertmanager.url=http://am:9093 \
  --author=alice --comment="maintenance" \
  --start=2026-11-26T00:00:00Z --end=2026-11-26T02:00:00Z \
  'job="api"'

# 查看/过期静默
amtool silence query
amtool silence expire <silence-id>

# 校验配置
amtool check-config alertmanager.yml

# 测试路由（不真的发通知）
amtool config routes test --config.file=alertmanager.yml severity=page job=api
```

`config routes test` 是投产前验证路由树的最快方式；`check-config` 能发现模板/接收器错误。

## AC.4 配置与规则 lint

`promtool` 校验"能不能加载"，不校验"规则是否合理"。规则 lint 可用：

- **pint**（Cloudflare）：检查告警表达式、recording rule 命名、聚合成本、过期规则、重复规则。
- **prometheus-config-validator**：配置结构校验的补充。
- **conftest/OPA**：把组织规范写成策略（如禁止 `job=~".+"`、要求 `severity` 标签）。
- **自定义脚本**：用 `promtool promql format` + 正则做命名检查。

pint 示例：

```bash
pint lint rules/*.yml
pint ci --config-file=.pint.hcl
```

`.pint.hcl` 可以配置禁用检查、成本阈值、告警命名模板。适合在 PR 流水线里阻断明显有问题的规则。

## AC.5 Kubernetes 与 GitOps 工具

| 工具 | 用途 |
|---|---|
| `kube-prometheus` | jsonnet 库，生成 Prometheus/Alertmanager/Grafana/Exporter 清单 |
| `jsonnet-bundler`（`jb`） | 管理 jsonnet 依赖 |
| `kubectl` | 应用 CRD/清单、端口转发、调试 |
| `kustomize` | 覆盖镜像/副本/资源 |
| `helm` | 安装 kube-prometheus-stack 等 chart |
| `prometheus-operator` | 通过 ServiceMonitor/PodMonitor/PrometheusRule 管理 |
| `conftest` | 校验生成的 YAML |

Operator 的 CRD 与原生配置的映射见第 24 章与附录 P。GitOps 流水线见第 46 章。

## AC.6 长期存储与迁移工具

| 工具 | 用途 |
|---|---|
| `thanos` | sidecar/query/store/compact/receive/rule；`thanos tools bucket inspect/rewrite/retention` 管理对象存储 |
| `mimirtool` | Mimir 配置转换、规则/告警加载、Grafana 分析、历史回填 |
| `cortextool` | Cortex 规则/告警管理 |
| `vmctl` | VictoriaMetrics 数据导入/迁移 |
| `prom-migrate` | 通过 remote write 把历史数据迁移到远端 |
| `promtool tsdb create-blocks-from` | 从 OpenMetrics/规则结果生成 block |

迁移前先确认目标端的 remote write 版本、租户头、去重策略和保留策略。历史回填会产生大量乱序/重复样本，必须评估目标端是否接受。

## AC.7 客户端与测试工具

| 语言 | 库 | 备注 |
|---|---|---|
| Go | `client_golang` | 官方，支持 native histogram/exemplar |
| Python | `prometheus_client` | 官方，支持 multiprocess |
| Java | `client_java` | 官方 |
| Rust | `prometheus` / `metrics` | 社区 |
| .NET | `prometheus-net` | 社区 |
| Node.js | `prom-client` | 社区 |

测试工具：

- `promlint`（Go）：检查指标命名；
- `prometheus_client` 的 `generate_latest()`：单测 exposition；
- `pytest` + `testcontainers`：起真实 Prometheus 做集成测试；
- `promtool test rules`：规则语义测试；
- `prom-migrate`/`promtool push metrics`：灌数据。

## AC.8 CI 流水线示例

```yaml
name: prometheus-config
on:
  pull_request:
    paths:
      - "prometheus/**"
      - "rules/**"
jobs:
  validate:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Install promtool
        run: |
          curl -sL https://github.com/prometheus/prometheus/releases/download/v3.13.0/prometheus-3.13.0.linux-amd64.tar.gz \
            | tar xz
          sudo mv prometheus-3.13.0.linux-amd64/promtool /usr/local/bin/
      - run: promtool check config prometheus/prometheus.yml
      - run: promtool check rules rules/*.yml
      - run: promtool test rules tests/rules/*.yml
      - run: promtool check metrics < testdata/metrics.txt
```

流水线应缓存 promtool 二进制；版本与生产一致，避免"CI 能过、生产加载失败"。

## AC.9 工具选择决策表

| 任务 | 首选 | 备选 |
|---|---|---|
| 配置语法/引用检查 | `promtool check config` | pint |
| 规则语义测试 | `promtool test rules` | Mini promtool |
| 规则质量/成本 lint | pint | conftest/OPA |
| 基数分析 | `promtool tsdb analyze` | `/api/v1/status/tsdb` |
| 历史数据回填 | `promtool tsdb create-blocks-from` | prom-migrate |
| 告警路由测试 | `amtool config routes test` | 手工触发 |
| 静默管理 | `amtool silence` | Alertmanager UI |
| 长期存储巡检 | `thanos tools bucket inspect` | 对象存储清单 |
| 多租户规则管理 | `mimirtool rules` | API |
| 压测 | prombench/自建 | 合成指标 |

## AC.10 常见陷阱

- **版本不匹配**：CI 用 2.x 的 promtool 校验 3.x 配置，可能误报/漏报。
- **`check config` 通过 ≠ 能启动**：文件权限、Secret 挂载、网络、磁盘、端口冲突都不在检查范围。
- **`tsdb analyze` 在线上跑**：会读大量索引，可能拖慢查询；对快照跑。
- **`clean-tombstones` 在运行时跑**：需要停机或确保没有并发写入，否则可能损坏/丢数据。
- **`promtool query` 忽略 external labels**：它查的是单机数据，不是全局视图。
- **`amtool` 默认 URL**：不传 `--alertmanager.url` 时可能连 localhost，误操作。
- **JUnit 输出被日志污染**：`promtool test rules --junit` 的输出要单独重定向，否则 CI 解析失败。
- **pint 规则过严**：先以 warning 模式跑，再逐步阻断。

## 延伸阅读

- Prometheus 官方 `promtool` 文档与 `--help`。
- Alertmanager 官方 `amtool` 文档。
- pint、Thanos、Mimir、VictoriaMetrics 的官方文档。
- 本教程第 9 章（规则测试）、第 23 章（运维）、第 28 章（测试）、第 46 章（GitOps）。
