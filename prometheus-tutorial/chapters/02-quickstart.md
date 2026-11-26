# 第 2 章 快速上手：从零到一个可查询的 Prometheus

## 2.1 两条并行的路径

这一章同时给出两条路径：

- **真实 Prometheus**：适合生产验证与后续平台建设。
- **Mini-Prometheus**：本仓库自带，无外部依赖，便于观察内部行为。

两者的配置、PromQL、HTTP API 和告警语义高度一致；Mini 的存储格式与性能不兼容生产，但足以跑通全部概念。

## 2.2 准备环境

Mini-Prometheus 只依赖 Python 3.11+、PyYAML 和 pytest：

```bash
python --version
python -m pip install -r requirements.txt
python -m pytest
python examples/run_all.py
python check_docs.py
```

三条验证命令分别覆盖单元/集成测试、10 个示例场景和文档结构。完整结果见 `VERIFICATION.md`。

## 2.3 启动 Mini-Prometheus

终端一，启动示例 exporter：

```bash
python examples/demo_exporter.py --port 9100
```

终端二，启动 Mini：

```bash
python -m mini_prometheus \
  --config.file=examples/config/prometheus.yml \
  --storage.tsdb.path=./data \
  --web.listen-address=127.0.0.1:9090
```

配置如下：

```yaml
global:
  scrape_interval: 15s
  scrape_timeout: 5s
  evaluation_interval: 15s

scrape_configs:
  - job_name: demo
    static_configs:
      - targets: ["127.0.0.1:9100"]
        labels:
          env: lab

rule_files:
  - "../rules/demo.yml"
```

打开 <http://127.0.0.1:9090/graph>，或直接查询：

```bash
curl -s 'http://127.0.0.1:9090/api/v1/query?query=up' | python -m json.tool
curl -s 'http://127.0.0.1:9090/api/v1/query?query=sum(rate(demo_http_requests_total[1m]))' | python -m json.tool
curl -s 'http://127.0.0.1:9090/api/v1/targets' | python -m json.tool
```

看到 `up` 为 1、目标 `health` 为 `up`，说明抓取链路已经闭环。

## 2.4 第一个 PromQL

```promql
# 当前所有目标是否在线
up

# 每秒请求速率（按 code 分组）
sum by (code) (rate(demo_http_requests_total[1m]))

# 90 分位延迟（先按 le 聚合，再对桶计数求速率；直接对累计桶求分位会得到"全历史"值）
histogram_quantile(
  0.9,
  sum by (le) (rate(demo_request_duration_seconds_bucket[1m]))
)

# 错误比例
sum(rate(demo_http_requests_total{code="500"}[5m]))
/
sum(rate(demo_http_requests_total[5m]))
```

注意最后一条查询的除号两边都没有指标名，但标签集合一致，因此可以一一匹配。若标签不一致，会得到空结果或"many-to-many"错误；第 8 章会系统解释向量匹配。

## 2.5 第一条告警

`examples/rules/demo.yml` 中定义：

```yaml
groups:
  - name: demo
    interval: 15s
    rules:
      - record: job:demo_requests:rate5m
        expr: sum by (job) (rate(demo_http_requests_total[5m]))

      - alert: DemoHighErrorRatio
        expr: |
          sum(rate(demo_http_requests_total{code="500"}[5m]))
          /
          sum(rate(demo_http_requests_total[5m])) > 0.2
        for: 30s
        labels:
          severity: warning
        annotations:
          summary: "{{ $labels.job }} 5xx ratio is {{ $value | humanizePercentage }}"
```

Mini 会渲染 `{{ $labels.* }}`、`{{ $value }}` 和 `printf` 形式；生产 Alertmanager 使用完整 Go template，支持 `humanizePercentage` 等函数。

查询规则与告警：

```bash
curl -s http://127.0.0.1:9090/api/v1/rules | python -m json.tool
curl -s http://127.0.0.1:9090/api/v1/alerts | python -m json.tool
```

## 2.6 真实 Prometheus

从官方 release 下载对应平台的压缩包，解压后目录里自带 `prometheus.yml`：

```bash
./prometheus \
  --config.file=prometheus.yml \
  --storage.tsdb.path=./data \
  --web.listen-address=127.0.0.1:9090
```

最小配置：

```yaml
global:
  scrape_interval: 15s
  evaluation_interval: 15s

scrape_configs:
  - job_name: prometheus
    static_configs:
      - targets: ["localhost:9090"]
```

Prometheus 会抓取自己的 `/metrics`。这是最可靠的"第一条线"：如果 `up{job="prometheus"}` 不是 1，问题在 Prometheus 自身或网络，而不是业务 exporter。

生产环境通常不手工下载，而是：

- Kubernetes：Prometheus Operator + `ServiceMonitor`；
- 虚拟机：包管理器/Ansible + systemd；
- 托管：AWS Managed Prometheus、GCP Managed Service for Prometheus、Azure Monitor managed Prometheus、Grafana Cloud 等。

## 2.7 目录与数据

Mini 的数据目录结构：

```text
data/
  lock
  checkpoint.json
  wal/00000000
  blocks/01J.../meta.json
  blocks/01J.../index.json
  blocks/01J.../chunks.bin
  snapshots/...
```

真实 Prometheus 的结构类似，但 WAL 是分页的二进制格式，block 的 index 是自定义二进制格式。不要手工修改生产 TSDB 目录；备份与迁移应使用快照 API 或 `tsdb` 工具。

## 2.8 常见第一次失败

- **`up == 0`**：目标不可达、端口错、路径不是 `/metrics`、TLS/认证失败。看 `/api/v1/targets` 的 `lastError`。
- **查询为空但目标 up**：指标名/标签写错，或时间参数不在样本范围内。先用 `/api/v1/series?match[]={__name__=~".+"}` 看实际序列。
- **`rate()` 为空**：窗口内少于两个样本。抓取间隔 1m 时，`rate(x[1m])` 经常只有一两个点；至少用 `[2m]` 或缩短抓取间隔。
- **端口占用**：换 `--web.listen-address`，或先停掉旧进程。
- **权限**：数据目录不可写、配置不可读。Mini 会输出明确的启动错误。

## 2.9 用容器启动真实 Prometheus

如果不想手工下载二进制，容器是最快的真实环境：

```bash
docker run --rm -p 9090:9090 \
  -v "$PWD/prometheus.yml:/etc/prometheus/prometheus.yml:ro" \
  prom/prometheus:v3.13.5 \
  --config.file=/etc/prometheus/prometheus.yml \
  --storage.tsdb.path=/prometheus \
  --web.enable-lifecycle
```

> 这里固定 `v3.13.5`（3.13 LTS，支持到 2027-07）。要用 3.16 的 `smoothed`/`anchored` 稳定版或最新特性，把 tag 换成对应版本；生产不要用 `latest`。3.14–3.16 的增量见第 20 章与附录 AE。

容器里要注意：

- 配置必须挂载进去，容器内没有你本地的相对路径；
- 数据目录要挂卷，否则容器删除即丢数据；
- `--web.enable-lifecycle` 才会开放 `/-/reload`；
- 容器内 `localhost` 是容器自己，抓取宿主机要用 `host.docker.internal`（Docker Desktop）或宿主机 IP。

验证：

```bash
curl -s http://localhost:9090/-/ready
curl -s http://localhost:9090/api/v1/targets | python -m json.tool
```

如果 `up{job="prometheus"}` 为 1，说明 Prometheus 能抓取自己；这是最小闭环。

## 2.10 第一次排障路径

遇到"没有数据"时，按固定顺序排查，避免在 PromQL 里瞎猜：

1. **进程**：`/-/healthy`、`/-/ready` 是否 200？
2. **配置**：`/api/v1/status/config` 是否是期望配置？`reloadConfigSuccess` 是否为 true？
3. **目标**：`/api/v1/targets` 的 `health`、`lastError`、`lastScrape`；`discoveredLabels` 是否符合 relabel 预期？
4. **抓取**：`up`、`scrape_duration_seconds`、`scrape_samples_scraped`；是失败、超时、还是 0 样本？
5. **序列**：`/api/v1/series?match[]=...` 是否真的有这条序列？标签是否被 relabel 改掉？
6. **时间**：查询时间是否落在样本范围内？`rate()` 窗口是否至少有两个样本？
7. **规则**：`/api/v1/rules` 的 `health`/`lastError`；规则是否加载、是否在求值？
8. **告警**：`/api/v1/alerts` 是否有 pending/firing；Alertmanager 是否收到？

这条路径同样适用于 Mini；第 12 章会解释每一步的失败语义，第 45 章给出 API 细节。

## 本章小结

- Mini 与真实 Prometheus 共享配置形态、PromQL 和 API，可以交替使用。
- 第一条链路要验证 `up`、`/targets`、`/query` 三件事。
- 第一条告警要先确认规则加载（`/rules`），再确认状态（`/alerts`）。
- 大部分"第一次失败"是目标可达性、时间窗口或标签不匹配，而不是 PromQL 语法。

## 延伸阅读

- Prometheus 官方 "Getting started"。
- 本教程第 9、10 章（规则与告警生命周期）。
- 附录 B（配置参考）与附录 J（API 参考）。
