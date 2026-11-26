# 附录 Y 压测与基准手册

本附录是第 48 章的配套操作手册。它不提供"标准性能数字"（硬件/版本/数据分布不同，数字不可移植），而是提供可复现的实验步骤、脚本骨架和判读方法。

## Y.1 实验目标

先写目标，再选工具：

- 容量：N 个活跃序列、M samples/s、保留 D 天，需要多少 CPU/内存/磁盘？
- 回归：升级 Prometheus/改配置后，吞吐/延迟是否退化？
- 瓶颈：写入、查询、规则、compaction 哪个先饱和？
- 故障：remote write 中断/磁盘变慢/目标大量失败时，系统是否可控？
- 选型：自建 vs 托管 vs 其他 TSDB 的成本/性能对比。

目标要可量化：

```text
在 4 核/16 GiB/SSD 上，维持 200k 活跃序列、15s 抓取、
查询 P99 < 2s、错误率 < 0.1%，持续 24h。
```

## Y.2 数据集设计

### Y.2.1 序列规模

```text
active_series = metrics × label_combinations
samples/s     = active_series / scrape_interval
```

例子：

```text
10,000 pods × 20 metrics = 200,000 series
200,000 / 15s ≈ 13,333 samples/s
```

### Y.2.2 标签分布

真实数据不是均匀笛卡尔积：

- 少数指标贡献大部分序列（`container_*`、`kube_*`）；
- 标签值有热点（少数 namespace/pod 占多数样本）；
- 有 churn（Pod 重建、版本发布）；
- 有大量低流量/僵尸序列。

合成数据至少要有：

- 3～5 种指标族；
- 每族 10～100 个标签组合；
- 10%～30% 的序列在实验中新增/消失；
- 值分布有长尾（不是常数）。

### Y.2.3 时间跨度

- 查询基准至少覆盖 1h/6h/24h/7d；
- 写入基准至少跨过一次 block flush（默认 2h）和 compaction；
- 长跑 24h～7d 观察 retention/磁盘。

## Y.3 写入负载生成

### Y.3.1 生成 exposition 端点

一个最小的 Python 生成器（仅用于实验）：

```python
from __future__ import annotations

import random
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SERIES = 20_000
START = time.time()


def render() -> bytes:
    lines = []
    elapsed = time.time() - START
    for index in range(SERIES):
        # 20 metrics × 1000 pods = 20,000 unique series; instance is derived
        # from the pod so it does not create duplicate label sets.
        metric = index // 1000
        pod = index % 1000
        value = (index * 17 + int(elapsed)) % 10_000
        lines.append(
            f'bench_metric_{metric}{{pod="pod-{pod:04d}",instance="host-{pod % 100:02d}"}} {value}'
        )
    lines.append("# EOF")
    return ("\n".join(lines) + "\n").encode()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/metrics":
            self.send_response(404)
            self.end_headers()
            return
        body = render()
        self.send_response(200)
        self.send_header("Content-Type", "application/openmetrics-text; version=1.0.0")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", 9200), Handler).serve_forever()
```

用 Prometheus 抓取它：

```yaml
scrape_configs:
  - job_name: bench
    scrape_interval: 15s
    static_configs:
      - targets: ["127.0.0.1:9200"]
```

注意：

- 生成器本身可能成为瓶颈；用多进程/Go 重写；
- 值不要全相同，否则压缩率失真；
- 每次抓取序列要稳定（除非故意测 churn）；
- 生成器的 CPU/内存要监控，别把客户端瓶颈当成服务端瓶颈。

### Y.3.2 Remote Write 压测

用真实 Prometheus + `remote_write` 指向被测接收端，或写一个发送端：

```text
发送端 → protobuf + snappy → /api/v1/write
```

关注：

- 发送端队列长度/重试；
- 接收端 samples/s、CPU、内存、磁盘；
- 4xx/5xx/超时；
- 乱序/重复。

### Y.3.3 官方工具

```bash
# TSDB 写入基准（参数以 promtool help 为准）
promtool tsdb bench write --samples=1000000 /tmp/tsdb-bench

# 分析 block
promtool tsdb analyze /tmp/tsdb-bench

# prombench（Prometheus 官方基准框架）按仓库文档运行
```

`promtool` 的基准主要测 TSDB/磁盘，不覆盖抓取/查询/规则；端到端仍要自建。

## Y.4 查询语料

从生产查询日志/面板/告警抽样，固定成文件：

```text
# queries.txt
up
sum by (job) (rate(http_requests_total[5m]))
histogram_quantile(0.99, sum by (le) (rate(http_request_duration_seconds_bucket[5m])))
topk(10, sum by (pod) (rate(container_cpu_usage_seconds_total[5m])))
sum without (instance) (node_memory_MemAvailable_bytes)
```

每条查询标注：

```text
expr | start | end | step | expected_series | notes
```

压测客户端要：

- 固定并发（1/10/50/100）；
- 固定时长/请求数；
- 记录 P50/P90/P99、错误、扫描样本；
- 区分冷/热缓存；
- 支持取消/超时。

一个最小客户端：

```python
from __future__ import annotations

import concurrent.futures
import time
import urllib.parse
import urllib.request


def one(base: str, expression: str, start: str, end: str, step: str) -> float:
    params = urllib.parse.urlencode(
        {"query": expression, "start": start, "end": end, "step": step}
    )
    url = f"{base}/api/v1/query_range?{params}"
    started = time.perf_counter()
    with urllib.request.urlopen(url, timeout=60) as response:
        response.read()
    return time.perf_counter() - started


def run(base: str, expression: str, concurrency: int = 10, requests: int = 100):
    start, end, step = "2026-11-26T00:00:00Z", "2026-11-26T06:00:00Z", "60s"
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [pool.submit(one, base, expression, start, end, step) for _ in range(requests)]
        latencies = sorted(future.result() for future in futures)
    print(
        f"n={len(latencies)} p50={latencies[len(latencies)//2]:.3f}s "
        f"p99={latencies[int(len(latencies)*0.99)]:.3f}s"
    )
```

客户端本身要预热，避免 DNS/TLS/连接池影响。

## Y.5 观测指标

Prometheus 自身：

```promql
rate(prometheus_tsdb_head_samples_appended_total[5m])
prometheus_tsdb_head_series
rate(prometheus_tsdb_wal_fsync_duration_seconds_sum[5m])
  / rate(prometheus_tsdb_wal_fsync_duration_seconds_count[5m])
rate(prometheus_tsdb_compaction_duration_seconds_sum[5m])
  / rate(prometheus_tsdb_compaction_duration_seconds_count[5m])
histogram_quantile(0.99, sum by (le) (rate(prometheus_engine_query_duration_seconds_bucket[5m])))
rate(prometheus_rule_evaluation_duration_seconds_sum[5m])
  / rate(prometheus_rule_evaluation_duration_seconds_count[5m])
rate(process_cpu_seconds_total[5m])
go_memstats_alloc_bytes
```

OS：

```bash
iostat -xz 1
pidstat -p <pid> 1
vmstat 1
ss -s
```

容器：

```bash
kubectl top pod -n monitoring
kubectl describe pod <prometheus-pod>
```

注意指标名随版本变化；以 `/metrics` 实际输出为准。

## Y.6 结果判读

### Y.6.1 吞吐 vs 延迟

- 低并发：延迟低、吞吐随并发上升；
- 拐点：延迟快速上升、吞吐不再增长；
- 饱和：错误/超时增加，CPU/磁盘/内存打满。

记录拐点，不要只记录"最大 QPS"。

### Y.6.2 瓶颈定位

| 观察 | 判断 |
|---|---|
| CPU 100%，磁盘空闲 | 解析/查询/规则 CPU 瓶颈 |
| CPU 不高，iowait 高 | 磁盘/compaction/WAL |
| 内存持续涨，GC 高 | head/查询结果/规则 |
| WAL fsync 高 | 磁盘 IOPS/同步写 |
| 查询 P99 高但平均低 | 大查询/高基数/噪声邻居 |
| 规则 duration ≈ interval | 规则过载 |

### Y.6.3 排队

```text
L = λ × W
```

如果 QPS 20、平均延迟 2s，平均并发约 40；P99 延迟 10s 时，峰值并发可能远超 40。按峰值设计，不要按平均。

## Y.7 容量公式

```text
samples/s = active_series / scrape_interval
samples/day = samples/s × 86400
raw_bytes/day ≈ samples/day × bytes_per_sample
disk ≈ raw_bytes/day × retention_days × replicas × overhead
head_memory ≈ active_series × bytes_per_series
query_points = matched_series × (range / step)
```

`bytes_per_sample`/`bytes_per_series` 必须实测：

- 不同标签重复度/值分布差异很大；
- 索引/WAL/临时空间另算；
- compaction 峰值需要额外磁盘；
- 副本/remote write 会放大。

## Y.8 报告模板

```text
实验名称：
日期/负责人：
目标：

环境：
  Prometheus/Go 版本
  CPU/内存/磁盘/文件系统/网络
  数据目录/缓存状态

数据集：
  活跃序列/指标数/标签基数/churn
  抓取间隔/目标数

工作负载：
  写入 samples/s
  查询语料/并发/时间范围/step
  规则组数/间隔
  持续时间

结果：
  写入吞吐/CPU/内存/WAL/磁盘增长
  查询 P50/P90/P99/QPS/错误率
  规则 duration/interval
  恢复时间（WAL replay）

瓶颈与证据：
  pprof/iostat/日志/指标

容量模型：
  每 10 万序列的 CPU/内存/磁盘

风险/余量/建议：

复现步骤：
```

## Y.9 常见错误

- 生成器与服务端同机，互相抢 CPU；
- 所有值相同，压缩率虚高；
- 没有 churn，head 内存低估；
- 只跑 5 分钟，没看到 compaction；
- 用平均值代替 P99；
- 冷/热缓存混在一起；
- 升级前后硬件/数据/查询语料不一致；
- 把 `query.max-samples` 调大来"提升性能"；
- 用 Mini/Python 的绝对数字推断生产。

## Y.10 实验记录表

| 项 | 值 |
|---|---|
| Prometheus 版本 | |
| Go 版本 | |
| CPU/内存 | |
| 磁盘/文件系统/IOPS | |
| 活跃序列 | |
| 抓取间隔 | |
| samples/s | |
| 查询语料条数/并发 | |
| 规则组数/间隔 | |
| 持续时间 | |
| 写入 CPU/内存 | |
| WAL fsync P99 | |
| 查询 P99/错误率 | |
| 磁盘增长/天 | |
| compaction 峰值 | |
| WAL replay 时间 | |
| 瓶颈 | |
| 结论/建议 | |

## 延伸阅读

- Prometheus `promtool tsdb`、prombench 文档。
- Thanos `bench`、Mimir/VictoriaMetrics 基准博客。
- Brendan Gregg：Systems Performance、USE Method。
- 本教程第 22 章（容量）、第 48 章（基准方法）。
