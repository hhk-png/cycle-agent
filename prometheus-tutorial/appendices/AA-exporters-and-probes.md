# 附录 AA Exporter 生态、探针与中间件监控

第 5 章讲了"应用怎么暴露指标"，第 11、12 章讲了"Prometheus 怎么找到并抓取目标"。但在真实环境里，大量指标并不来自你自己的代码，而来自 **exporter**：主机、数据库、消息队列、缓存、网络设备、证书、HTTP 可用性。本附录把这些"非应用侧"的采集补齐，重点是三件事：

1. 每类 exporter 的**指标语义**与常见坑；
2. 如何用**多目标模式**和 relabel 把探针接进 Prometheus；
3. 如何为它们写**真正可用的告警**，而不是只把 dashboard 填满。

> 版本提示：exporter 由各自社区独立发布，指标名会随版本增删。本附录给出的是稳定、常见的一层；上线前请以目标实例的 `/metrics` 和对应 exporter 的 release notes 为准。Prometheus 侧的行为以 3.x 为基线。

```text
node_exporter ─┐
blackbox      ─┼─ scrape (pull) ─┐
mysqld        ─┤                 ├─ TSDB → PromQL → rules → Alertmanager
kafka (JMX)   ─┘                 │
Pushgateway   ───────────────────┘
```

## AA.1 Exporter 的分类与选型

Exporter 本质上是一个**协议适配器**：把某种系统的内部状态翻译成 Prometheus 文本格式，并暴露一个 `/metrics` 端点。按采集对象可以分四类：

| 类别 | 代表 | 部署位置 | 主要问题 |
|---|---|---|---|
| 主机/资源 | node_exporter、windows_exporter | 每台机器一个 | 容器内视角、挂载点、cgroup 版本 |
| 中间件/数据库 | mysqld_exporter、postgres_exporter、redis_exporter、kafka_exporter、JMX exporter | 与被监控系统同机/同网 | 权限、连接数、指标基数 |
| 探针/合成 | blackbox_exporter、SNMP exporter | 中心化部署 | 多目标 relabel、超时预算、网络路径 |
| 业务/第三方 API | 厂商 exporter、自定义 exporter | 视情况 | 限流、凭据、指标语义不稳定 |

选型时先问五个问题：

1. **它回答什么问题？** 如果只是"进程还活着"，`up` 可能就够了；如果要容量规划，必须有资源饱和度指标。
2. **指标基数多大？** 按 `topic`、`partition`、`sql_digest`、`url` 打标签的 exporter 很容易制造十万级序列。
3. **谁负责升级？** exporter 版本与目标系统版本有兼容矩阵，尤其是 MySQL、PostgreSQL、Kafka、JMX。
4. **采集路径是否可信？** 探针经过的负载均衡、DNS、代理，与真实用户路径是否一致？
5. **失败是否可见？** exporter 自身挂掉时，`up=0` 是否有人看到？exporter 内部采集失败时，是否有 `*_scrape_error` 或 `*_up=0`？

一条经验：**优先用官方/社区维护、单二进制、无状态、默认关闭高基数 collector 的 exporter**。不要为了"指标全"把 collector 全开；collector 的选择应该由告警和容量模型反推。

## AA.2 node_exporter：主机指标的事实标准

node_exporter 是 Prometheus 生态里最常用的 exporter。它默认监听 `:9100`，把 Linux/Unix 主机的 CPU、内存、磁盘、文件系统、网络、负载等暴露为指标。

### AA.2.1 部署方式

系统服务：

```bash
# 以 systemd 为例，二进制路径按实际调整
node_exporter \
  --web.listen-address=:9100 \
  --collector.textfile.directory=/var/lib/node_exporter/textfile
```

Kubernetes 里通常用 DaemonSet，并挂载宿主机的 `/proc`、`/sys`、`/` 到容器内，同时设置：

```yaml
# 片段：DaemonSet 的关键 volume/mount
volumeMounts:
  - name: proc
    mountPath: /host/proc
    readOnly: true
  - name: sys
    mountPath: /host/sys
    readOnly: true
  - name: root
    mountPath: /host/root
    readOnly: true
    mountPropagation: HostToContainer
args:
  - --path.procfs=/host/proc
  - --path.sysfs=/host/sys
  - --path.rootfs=/host/root
```

关键点：

- 容器里的 `/proc` 是容器命名空间的，不是宿主机的；必须显式指定 `--path.procfs`/`--path.sysfs`/`--path.rootfs`。
- `--path.rootfs` 影响文件系统指标；不设置会把容器镜像的文件系统当成主机。
- 只读挂载；node_exporter 不需要写权限。
- 如果使用 `hostNetwork: true`，注意端口冲突；不用 hostNetwork 时，Prometheus 需要通过 Pod IP 抓取。

### AA.2.2 核心指标族

| 指标 | 类型 | 说明 |
|---|---|---|
| `node_cpu_seconds_total{cpu,mode}` | counter | 每个 CPU 每种模式的累计秒数 |
| `node_memory_MemTotal_bytes` | gauge | 物理内存总量 |
| `node_memory_MemAvailable_bytes` | gauge | 估计可用内存（比 `MemFree` 更接近真实） |
| `node_memory_MemFree_bytes` | gauge | 完全空闲内存 |
| `node_memory_Cached_bytes` / `Buffers_bytes` | gauge | 页缓存/缓冲 |
| `node_memory_SwapFree_bytes` | gauge | 交换分区剩余 |
| `node_filesystem_size_bytes{device,fstype,mountpoint}` | gauge | 文件系统总大小 |
| `node_filesystem_avail_bytes` | gauge | 非 root 用户可用空间 |
| `node_filesystem_free_bytes` | gauge | 空闲空间（含 root 保留） |
| `node_filesystem_files` / `_files_free` | gauge | inode 总数/剩余 |
| `node_disk_read_bytes_total` / `_written_bytes_total` | counter | 磁盘读写字节 |
| `node_disk_io_time_seconds_total` | counter | 磁盘忙碌时间 |
| `node_disk_io_time_weighted_seconds_total` | counter | 加权 IO 时间（可近似饱和度） |
| `node_network_receive_bytes_total` / `_transmit_bytes_total` | counter | 网卡流量 |
| `node_network_receive_errs_total` / `_drop_total` | counter | 收包错误/丢弃 |
| `node_load1` / `node_load5` / `node_load15` | gauge | 1/5/15 分钟负载 |
| `node_boot_time_seconds` | gauge | 开机时间（用于检测重启） |
| `node_time_seconds` | gauge | 当前时间（用于检测时钟漂移） |
| `node_uname_info` | gauge | 内核/主机信息，值恒为 1 |
| `node_exporter_build_info` | gauge | exporter 版本，值恒为 1 |
| `node_scrape_collector_success{collector}` | gauge | 各 collector 是否成功 |
| `node_textfile_mtime_seconds` | gauge | textfile collector 文件修改时间 |

### AA.2.3 常用查询

CPU 使用率（排除 idle）：

```promql
100 * sum by (instance) (rate(node_cpu_seconds_total{mode!="idle"}[5m]))
  / sum by (instance) (rate(node_cpu_seconds_total[5m]))
```

内存使用率（用 `MemAvailable`，不要用 `MemFree`）：

```promql
100 * (1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)
```

文件系统使用率（排除会波动的伪文件系统）：

```promql
100 * (1
  - node_filesystem_avail_bytes{fstype!~"tmpfs|overlay|squashfs"}
  / node_filesystem_size_bytes{fstype!~"tmpfs|overlay|squashfs"})
```

inode 使用率：

```promql
100 * (1 - node_filesystem_files_free / node_filesystem_files)
```

磁盘写满预测（6 小时窗口，预测 4 小时后是否小于 0）：

```promql
predict_linear(node_filesystem_avail_bytes{mountpoint="/"}[6h], 4 * 3600) < 0
```

每核负载：

```promql
node_load1 / count by (instance) (node_cpu_seconds_total{mode="idle"})
```

网络错误速率：

```promql
rate(node_network_receive_errs_total[5m]) > 0
```

### AA.2.4 常见坑

- **`MemFree` 不是可用内存**。Linux 会把空闲内存拿去做页缓存；看 `MemAvailable`。
- **`avail` vs `free`**：`avail` 扣除了 root 保留块，更接近普通进程可写空间；容量告警用 `avail`，但要知道 root 仍可能写入。
- **容器/overlay 文件系统**：容器内看到的 `overlay` 使用率往往不是宿主机的；用 `--path.rootfs` 并过滤 `fstype`。
- **CPU 模式**：`iowait` 高不一定是 CPU 忙，可能是磁盘慢；`steal` 高说明虚拟化层争抢。
- **counter 重置**：机器重启后 counter 归零，`rate()` 会自动处理重置，但跨重启的 `increase()` 可能低估。
- **collector 的代价**：`--collector.<name>` 全开会让 `/metrics` 变大、抓取变慢；默认集合通常够用，按需开启（如 `systemd`、`processes`、`textfile`）。
- **textfile collector**：适合 cron/批任务写指标。写入必须原子（先写临时文件再 `mv`），否则抓取到半截文件；文件里不要放时间戳标签。

### AA.2.5 textfile collector 示例

```bash
# /var/lib/node_exporter/textfile/backup.prom
# 由 cron 任务生成；使用 tmp + mv 原子替换
backup_last_success_timestamp_seconds 1764200000
backup_duration_seconds 42.7
backup_records_total 120034
```

对应的告警：

```promql
time() - backup_last_success_timestamp_seconds > 36 * 3600
```

### AA.2.6 Windows 与其它平台

- Windows 用 `windows_exporter`，指标前缀 `windows_*`（如 `windows_cpu_time_total`、`windows_memory_available_bytes`、`windows_logical_disk_free_bytes`）。
- macOS/BSD 的 node_exporter 指标子集不同，不要假设 Linux 的指标一定存在。
- 网络设备走 SNMP exporter（见 AA.4），不要试图在交换机上跑 node_exporter。

## AA.3 blackbox_exporter：从"进程活着"到"用户能用"

`up=1` 只说明 Prometheus 能连上 exporter，不说明你的网站能打开、证书没过期、DNS 能解析。blackbox_exporter 从**外部视角**发起探测，把结果暴露成指标。

### AA.3.1 多目标模式

blackbox_exporter 通常只部署一份（或每组网络路径一份），Prometheus 每次抓取时通过 `target` 参数告诉它"探测谁"：

```yaml
# prometheus.yml 片段
scrape_configs:
  - job_name: blackbox-http
    metrics_path: /probe
    params:
      module: [http_2xx]
    static_configs:
      - targets:
          - https://www.example.com
          - https://api.example.com/healthz
    relabel_configs:
      # 把原始目标放进 __param_target
      - source_labels: [__address__]
        target_label: __param_target
      # 让面板/告警看到真实目标，而不是 blackbox 的地址
      - source_labels: [__param_target]
        target_label: instance
      # 真正抓取的是 blackbox exporter
      - target_label: __address__
        replacement: blackbox-exporter:9115
```

三个 relabel 的顺序不能错：

1. `__address__` 原本是探测目标，先复制到 `__param_target`；
2. 用 `__param_target` 覆盖 `instance`，否则所有探测的 `instance` 都是 blackbox 地址；
3. 最后把 `__address__` 改成 blackbox exporter 的地址。

如果第 3 步放在第 1 步之前，`__param_target` 就会变成 blackbox 的地址，探测对象全错。

### AA.3.2 常用 module

```yaml
# blackbox.yml
modules:
  http_2xx:
    prober: http
    timeout: 5s
    http:
      preferred_ip_protocol: ip4
      follow_redirects: true
      valid_status_codes: []   # 空表示 2xx
  http_post_2xx:
    prober: http
    timeout: 5s
    http:
      method: POST
      headers:
        Content-Type: application/json
      body: '{"ping":true}'
  tcp_connect:
    prober: tcp
    timeout: 5s
  icmp:
    prober: icmp
    timeout: 5s
    icmp:
      preferred_ip_protocol: ip4
  dns_example:
    prober: dns
    timeout: 5s
    dns:
      query_name: example.com
      query_type: A
      valid_rcodes: [NOERROR]
  grpc_health:
    prober: grpc
    timeout: 5s
    grpc:
      preferred_ip_protocol: ip4
      tls: true
```

探测类型与典型用途：

| prober | 用途 | 关键配置 |
|---|---|---|
| `http` | 网站/API 可用性、状态码、TLS、重定向 | `valid_status_codes`、`method`、`headers`、`body`、`tls_config` |
| `tcp` | 端口连通性、数据库/消息队列端口 | `tls`、`preferred_ip_protocol` |
| `icmp` | 主机可达性/丢包/延迟 | 需要 `CAP_NET_RAW` 或 root |
| `dns` | 域名解析、权威服务器 | `query_name`、`query_type`、`valid_rcodes` |
| `grpc` | gRPC health check | `tls`、`service` |
| `ssh_banner` | SSH 端口与 banner | 仅做协议握手，不做登录 |

### AA.3.3 核心指标

| 指标 | 含义 |
|---|---|
| `probe_success` | 探测是否成功（1/0） |
| `probe_duration_seconds` | 整个探测耗时 |
| `probe_http_status_code` | HTTP 状态码 |
| `probe_http_duration_seconds{phase}` | 各阶段耗时：resolve/connect/tls/processing/transfer |
| `probe_http_redirects` | 重定向次数 |
| `probe_http_version` | HTTP 版本 |
| `probe_ssl_earliest_cert_expiry` | 证书最早过期时间（Unix 秒） |
| `probe_tls_version_info{version}` | TLS 版本，值恒为 1 |
| `probe_dns_lookup_time_seconds` | DNS 解析耗时 |
| `probe_ip_protocol` | 实际使用的 IP 协议（4/6） |
| `probe_icmp_duration_seconds{phase}` | ICMP 各阶段耗时 |
| `probe_ssh_banner` | SSH banner 信息 |

### AA.3.4 告警规则

```yaml
groups:
  - name: blackbox
    rules:
      - alert: ProbeFailed
        expr: probe_success == 0
        for: 2m
        labels:
          severity: critical
        annotations:
          summary: "探测失败：{{ $labels.instance }}"
          description: "{{ $labels.job }} 对 {{ $labels.instance }} 的探测已连续失败 2 分钟。"

      - alert: ProbeSlow
        expr: probe_duration_seconds > 3
        for: 10m
        labels:
          severity: warning
        annotations:
          summary: "探测变慢：{{ $labels.instance }}"
          description: "探测耗时 {{ $value | humanize }}s，超过 3s。"

      - alert: SSLCertExpiringSoon
        expr: probe_ssl_earliest_cert_expiry - time() < 14 * 24 * 3600
        for: 1h
        labels:
          severity: warning
        annotations:
          summary: "证书即将过期：{{ $labels.instance }}"
          description: "证书将在 {{ $value | humanizeDuration }} 后过期。"
```

要点：

- `up` 和 `probe_success` 是两件事：`up=0` 表示 Prometheus 连不上 blackbox；`up=1` 且 `probe_success=0` 表示 blackbox 连不上目标。排障时先看 `up`，再看 `probe_success`。
- `probe_ssl_earliest_cert_expiry` 只在 HTTPS/TLS 探测时存在；对 HTTP 目标做证书告警会得到空结果。
- 证书告警要加 `probe_success == 1` 或 `> 0` 过滤，避免探测失败时 `probe_ssl_earliest_cert_expiry` 缺失导致误判。
- 探测频率不要过高：每个目标每分钟一次已经能覆盖大多数可用性场景；高频探测会把 blackbox 变成被监控系统的一部分。
- **超时预算**：Prometheus 的 `scrape_timeout` 必须大于 module 的 `timeout`，否则 Prometheus 先超时，`probe_success` 永远来不及写。通常 `scrape_timeout: 10s`、module `timeout: 5s`。
- ICMP 需要额外 capability：

```yaml
securityContext:
  capabilities:
    add: ["NET_RAW"]
```

### AA.3.5 探测设计的反模式

- 只探测首页 200：首页有缓存/CDN，不能代表核心 API。
- 探测经过内网直连，但用户走公网 LB：路径不一致，故障发现不了。
- 对所有目标用同一个 module：API 需要 POST、gRPC 需要 TLS、内部服务不需要证书，应该分 job。
- 用 blackbox 探测量大范围端口扫描：这不是监控，是滥用；只探测你负责的目标。
- 把 `probe_success` 当业务成功率：它只代表"这次探测成功"，不代表用户请求成功率。

## AA.4 SNMP exporter：网络设备

交换机、路由器、防火墙通常只支持 SNMP。SNMP exporter 把 OID 翻译成指标，支持 v1/v2c/v3。

### AA.4.1 生成配置

SNMP exporter 使用 `generator.yml` 生成 `snmp.yml`：

```yaml
# generator.yml
auths:
  public_v2:
    community: public
    version: 2
modules:
  if_mib:
    walk:
      - 1.3.6.1.2.1.2
      - 1.3.6.1.2.1.31.1.1
    get:
      - 1.3.6.1.2.1.1.3.0
    metrics:
      - name: ifHCInOctets
        oid: 1.3.6.1.2.1.31.1.1.1.6
        type: counter
        help: Incoming bytes
        indexes:
          - labelname: ifIndex
            type: gauge
      - name: ifHCOutOctets
        oid: 1.3.6.1.2.1.31.1.1.1.10
        type: counter
        help: Outgoing bytes
        indexes:
          - labelname: ifIndex
            type: gauge
```

```bash
generator generate -g generator.yml -o snmp.yml
snmp_exporter --config.file=snmp.yml
```

生产环境建议：

- 用 SNMPv3（`auth` + `priv`），不要用 `public` 团体名；
- 只 walk 需要的子树，全表 walk 会让设备 CPU 飙升；
- 对每台设备设置合理的 `timeout` 和重试次数；
- 设备重启后 counter 归零，`rate()` 会处理，但要注意 `ifIndex` 可能变化。

### AA.4.2 多目标抓取

```yaml
scrape_configs:
  - job_name: snmp
    metrics_path: /snmp
    params:
      module: [if_mib]
    static_configs:
      - targets:
          - 192.0.2.10
          - 192.0.2.11
    relabel_configs:
      - source_labels: [__address__]
        target_label: __param_target
      - source_labels: [__param_target]
        target_label: instance
      - target_label: __address__
        replacement: snmp-exporter:9116
```

设备侧指标通常带 `if_mib_` 前缀（取决于模块名），例如：

```text
if_mib_ifHCInOctets{ifIndex="1",instance="192.0.2.10"}
if_mib_ifHCOutOctets{ifIndex="1",instance="192.0.2.10"}
if_mib_ifOperStatus{ifIndex="1",instance="192.0.2.10"}
```

常用查询：

```promql
# 端口流量（bit/s）
rate(if_mib_ifHCInOctets[5m]) * 8
```

```promql
# 端口 down
if_mib_ifOperStatus != 1
```

告警要按 `instance`、`ifIndex` 分组，并把 `ifIndex` 映射成可读的接口名（用 recording rule 或面板映射）。

## AA.5 JMX exporter：JVM 生态

Kafka、ZooKeeper、Cassandra、Hadoop、Tomcat 等 JVM 系统通过 JMX 暴露指标。JMX exporter 有两种运行方式：

- **javaagent**：挂到目标 JVM 上，随应用启动；
- **standalone HTTP server**：独立进程通过 RMI 连接目标 JMX。

javaagent 示例：

```bash
java \
  -javaagent:/opt/jmx_exporter/jmx_prometheus_javaagent.jar=9404:/opt/jmx_exporter/kafka.yml \
  -jar kafka.jar
```

### AA.5.1 规则文件

JMX 属性名长且不稳定，需要规则把 `kafka.server<type=BrokerTopicMetrics, name=MessagesInPerSec><>Count` 映射成稳定指标：

```yaml
# kafka.yml（片段）
rules:
  - pattern: "kafka.server<type=(.+), name=(.+)><>Count"
    name: "kafka_server_$1_$2_total"
    type: COUNTER
  - pattern: "kafka.server<type=(.+), name=(.+)><>Value"
    name: "kafka_server_$1_$2"
    type: GAUGE
  - pattern: "java.lang<type=Memory><HeapMemoryUsage>used"
    name: "jvm_memory_heap_used_bytes"
    type: GAUGE
```

要点：

- `pattern` 是正则，捕获组用 `$1`、`$2`；写错会静默丢指标；
- `type` 必须与语义一致：累计值用 `COUNTER`，瞬时值用 `GAUGE`；
- 用 `whitelistObjectNames`/`blacklistObjectNames` 限制采集范围，否则 `/metrics` 可能几十 MB；
- JMX exporter 自身指标：`jmx_exporter_build_info`、`jmx_scrape_duration_seconds`、`jmx_config_reload_success_total`。

### AA.5.2 基数陷阱

Kafka 的 `kafka_consumergroup_lag{consumergroup,topic,partition}` 是典型高基数指标：

```text
序列数 ≈ 消费组数 × topic 数 × 分区数
```

100 个消费组 × 50 个 topic × 12 分区 = 6 万条序列。治理方法：

- 只采集关键消费组（用规则过滤）；
- 聚合到 `consumergroup` + `topic` 级别，丢掉 `partition`；
- 用 recording rule 预聚合 `sum by (consumergroup, topic)`；
- 对 lag 做阈值告警，而不是把每个分区都画到面板上。

## AA.6 中间件与数据库 exporter

### AA.6.1 mysqld_exporter

部署：

```bash
mysqld_exporter \
  --config.my-cnf=/etc/mysqld_exporter/.my.cnf \
  --web.listen-address=:9104
```

`.my.cnf`：

```ini
[client]
user=exporter
password=CHANGE_ME
host=127.0.0.1
port=3306
```

最小权限：

```sql
CREATE USER 'exporter'@'127.0.0.1' IDENTIFIED BY 'CHANGE_ME';
GRANT PROCESS, REPLICATION CLIENT, SELECT ON *.* TO 'exporter'@'127.0.0.1';
```

核心指标：

| 指标 | 说明 |
|---|---|
| `mysql_up` | exporter 能否连上 MySQL |
| `mysql_global_status_uptime` | 运行时长 |
| `mysql_global_status_threads_connected` | 当前连接数 |
| `mysql_global_status_threads_running` | 正在执行的连接数 |
| `mysql_global_status_questions` | 语句计数（QPS 来源） |
| `mysql_global_status_slow_queries` | 慢查询计数 |
| `mysql_global_status_aborted_connects` | 连接失败 |
| `mysql_global_status_innodb_row_lock_waits` | 行锁等待 |
| `mysql_global_status_innodb_buffer_pool_read_requests` / `_reads` | 缓冲池命中率 |
| `mysql_slave_status_seconds_behind_master` | 主从延迟（旧命名） |
| `mysql_slave_status_slave_io_running` / `_sql_running` | 复制线程状态 |

常用查询：

```promql
# QPS
rate(mysql_global_status_questions[5m])
```

```promql
# 连接使用率
100 * mysql_global_status_threads_connected / mysql_global_variables_max_connections
```

```promql
# 缓冲池命中率
100 * (1 - rate(mysql_global_status_innodb_buffer_pool_reads[5m])
  / rate(mysql_global_status_innodb_buffer_pool_read_requests[5m]))
```

```promql
# 主从延迟
mysql_slave_status_seconds_behind_master > 60
```

注意事项：

- `mysql_up=0` 可能是 exporter 连不上，也可能是 MySQL 挂了；看 exporter 日志和 `mysql_global_status_uptime`。
- 复制相关指标名在 MySQL 8.0+ / exporter 新版本中可能变化（`slave` → `replica`），升级前检查 dashboard 和告警。
- 不要给 exporter 用户 `SUPER`；`PROCESS` + `REPLICATION CLIENT` 足够。
- 大实例上 `SHOW GLOBAL STATUS` 本身有成本，采集间隔不要低于 15s。

### AA.6.2 postgres_exporter

部署：

```bash
DATA_SOURCE_NAME="postgresql://exporter:CHANGE_ME@127.0.0.1:5432/postgres?sslmode=disable" \
  postgres_exporter --web.listen-address=:9187
```

权限：

```sql
CREATE ROLE exporter LOGIN PASSWORD 'CHANGE_ME';
GRANT pg_monitor TO exporter;
```

核心指标：

| 指标 | 说明 |
|---|---|
| `pg_up` | 能否连接 |
| `pg_stat_database_numbackends{datname}` | 连接数 |
| `pg_stat_database_xact_commit` / `_rollback` | 事务提交/回滚 |
| `pg_stat_database_blks_hit` / `_blks_read` | 缓存命中 |
| `pg_stat_activity_count{state}` | 按状态的活动会话 |
| `pg_locks_count{mode,datname}` | 锁 |
| `pg_replication_lag` | 复制延迟（视版本/查询集） |
| `pg_replication_is_replica` | 是否从库 |

常用查询：

```promql
# 缓存命中率
100 * rate(pg_stat_database_blks_hit[5m])
  / (rate(pg_stat_database_blks_hit[5m]) + rate(pg_stat_database_blks_read[5m]))
```

```promql
# 长事务/活动会话
pg_stat_activity_count{state="active"} > 50
```

注意事项：

- 自定义查询用 `--extend.query-path`/`queries.yaml`，不同大版本参数名有变化；升级前看 release notes。
- `pg_stat_activity` 的 `state` 标签值有限，适合告警；不要按 `query` 文本打标签。
- 连接数告警要对照 `max_connections`，而不是拍脑袋定绝对值。
- 从库延迟用 `pg_last_wal_replay_lsn` 与 `pg_last_wal_receive_lsn` 的差值更可靠，具体指标取决于 exporter 版本。

### AA.6.3 redis_exporter

```bash
REDIS_ADDR=redis://127.0.0.1:6379 \
REDIS_PASSWORD=CHANGE_ME \
  redis_exporter --web.listen-address=:9121
```

核心指标：

| 指标 | 说明 |
|---|---|
| `redis_up` | 能否连接 |
| `redis_memory_used_bytes` | 已用内存 |
| `redis_memory_max_bytes` | 最大内存（0 表示未限制） |
| `redis_connected_clients` | 客户端连接数 |
| `redis_blocked_clients` | 阻塞客户端 |
| `redis_db_keys{db}` | 各 DB key 数 |
| `redis_keyspace_hits_total` / `_misses_total` | 命中/未命中 |
| `redis_evicted_keys_total` | 驱逐 key 数 |
| `redis_rejected_connections_total` | 拒绝连接 |
| `redis_rdb_last_save_timestamp_seconds` | 最近 RDB 保存时间 |
| `redis_aof_last_write_status` | AOF 最近写入状态 |

常用查询：

```promql
# 缓存命中率
100 * rate(redis_keyspace_hits_total[5m])
  / (rate(redis_keyspace_hits_total[5m]) + rate(redis_keyspace_misses_total[5m]))
```

```promql
# 内存使用率（有 maxmemory 时）
100 * redis_memory_used_bytes / redis_memory_max_bytes
```

```promql
# 驱逐速率
rate(redis_evicted_keys_total[5m]) > 0
```

注意事项：

- `maxmemory=0` 时内存使用率查询会除零/无穷，告警要加 `redis_memory_max_bytes > 0`。
- 集群模式要抓每个节点；`redis_up` 只能说明单节点。
- `redis_commands_total{cmd}` 的 `cmd` 基数可控，但不要按 key 打标签。
- 持久化失败（`redis_rdb_last_save_timestamp_seconds` 过旧）比内存高更危险，应该有独立告警。

### AA.6.4 kafka_exporter 与 JMX

kafka_exporter 通过 Kafka 协议拉取 topic/partition/consumer group 信息，部署简单：

```bash
kafka_exporter \
  --kafka.server=kafka-1:9092 \
  --kafka.server=kafka-2:9092 \
  --web.listen-address=:9308
```

核心指标：

| 指标 | 说明 |
|---|---|
| `kafka_brokers` | broker 数量 |
| `kafka_topic_partitions{topic}` | 分区数 |
| `kafka_topic_partition_current_offset{topic,partition}` | 当前 offset |
| `kafka_topic_partition_oldest_offset{topic,partition}` | 最旧 offset |
| `kafka_consumergroup_current_offset{consumergroup,topic,partition}` | 消费 offset |
| `kafka_consumergroup_lag{consumergroup,topic,partition}` | 消费 lag |
| `kafka_topic_partition_under_replicated_partition` | 副本不足 |
| `kafka_topic_partition_in_sync_replica` | ISR 数量（视版本） |

常用查询：

```promql
# 消费组总 lag
sum by (consumergroup, topic) (kafka_consumergroup_lag)
```

```promql
# 副本不足
sum by (topic) (kafka_topic_partition_under_replicated_partition) > 0
```

注意事项：

- `kafka_consumergroup_lag` 基数 = 消费组 × topic × 分区，务必预聚合。
- kafka_exporter 依赖 Kafka 协议版本；新版本 Kafka 可能需要更新 exporter。
- 需要 broker 的 JMX 指标（如请求延迟、ISR 收缩）时，仍要上 JMX exporter；两者互补。
- 不要用 `kafka_topic_partition_current_offset` 的绝对值做告警，应该看 lag 和增长速率。

### AA.6.5 其它常见 exporter

| 系统 | exporter | 关键指标 | 典型告警 |
|---|---|---|---|
| Elasticsearch | elasticsearch_exporter | `elasticsearch_cluster_health_status`、`elasticsearch_cluster_health_unassigned_shards`、`elasticsearch_jvm_memory_used_bytes` | 集群 red/yellow、未分配分片、JVM 堆高 |
| Nginx | nginx-prometheus-exporter / nginx-vts | `nginx_connections_active`、`nginx_http_requests_total`、`nginx_up` | 连接耗尽、5xx 比例、exporter 挂掉 |
| HAProxy | haproxy_exporter | `haproxy_backend_up`、`haproxy_backend_response_errors_total`、`haproxy_backend_current_queue` | backend down、队列堆积、5xx |
| Envoy/Istio | Envoy stats → Prometheus | `envoy_cluster_upstream_rq_total`、`envoy_cluster_upstream_rq_time`、`envoy_cluster_upstream_cx_active` | 上游 5xx、连接池耗尽、延迟 |
| MongoDB | mongodb_exporter | `mongodb_up`、`mongodb_ss_connections`、`mongodb_ss_opcounters`、`mongodb_replset_member_state` | 连接数、复制状态、慢查询 |
| RabbitMQ | rabbitmq_exporter | `rabbitmq_queue_messages`、`rabbitmq_queue_consumers`、`rabbitmq_connections` | 队列积压、消费者为 0、内存/磁盘告警 |
| JVM 通用 | JMX exporter | `jvm_memory_*`、`jvm_gc_*`、`jvm_threads_*` | GC 停顿、堆使用率、线程死锁 |

这些 exporter 的指标名同样会随版本变化；把它们当成"起点"，上线前用 `curl` 核对实际输出。

## AA.7 采集模式：pull、push、probe 与 remote write

| 模式 | 适用 | Prometheus 侧配置 | 失败语义 |
|---|---|---|---|
| pull exporter | 长期运行服务/主机 | `scrape_configs` | `up=0`，可写 stale |
| blackbox probe | 外部可用性/证书/DNS | `metrics_path: /probe` + relabel | `up=0` 或 `probe_success=0` |
| Pushgateway | 短生命周期批任务 | 抓取 Pushgateway | 组不删除则永不消失 |
| OTLP push | OTel SDK/Collector | `--web.enable-otlp-receiver` | 依赖客户端重试 |
| remote write | 边缘/多集群汇聚 | `remote_write` | 队列/重试/丢弃指标 |

选择原则：

- **能 pull 就 pull**：目标清单可审计，失败可见；
- **短任务用 Pushgateway 或 textfile**：Pushgateway 必须配 `push_time_seconds` 告警和清理策略；
- **外部可用性用 blackbox**：不要用 `up` 冒充业务可用性；
- **跨集群/跨云用 remote write**：边缘 Prometheus 负责采集，中心负责长期存储与全局查询。

Pushgateway 的细节见附录 T；remote write 的队列、重试、分片见第 18、19 章与附录 Q。

## AA.8 安全、权限与可运维性

### AA.8.1 网络与认证

- exporter 默认没有认证。不要把 `:9100`、`:9104`、`:9187` 直接暴露到公网。
- 绑定内网/回环地址，或放在反向代理/mTLS 后面。
- 支持 `exporter-toolkit` 的 exporter 可以用 `--web.config.file` 开启 TLS/basic auth：

```yaml
# web-config.yml（exporter-toolkit 格式）
tls_server_config:
  cert_file: server.crt
  key_file: server.key
basic_auth_users:
  prometheus: "$2y$12$CHANGE_ME_BCRYPT_HASH"
```

Prometheus 侧：

```yaml
scrape_configs:
  - job_name: node
    scheme: https
    basic_auth:
      username: prometheus
      password_file: /etc/prometheus/node-password
    tls_config:
      ca_file: /etc/prometheus/ca.crt
    static_configs:
      - targets: ["node-1:9100"]
```

生产建议：

- 用 `password_file` 而不是明文 `password`；
- 证书轮换要纳入流程，监控证书过期；
- 不同环境/租户用不同凭据；
- 抓取账号只读，最小权限。

### AA.8.2 最小权限清单

| 目标 | 账号权限 |
|---|---|
| Linux 主机 | 只读文件系统/进程；node_exporter 不需要 root（ICMP 探测除外） |
| MySQL | `PROCESS`、`REPLICATION CLIENT`、`SELECT` |
| PostgreSQL | `pg_monitor`（或等价的只读角色） |
| Redis | 只读 ACL 用户（`+@read`） |
| SNMP | 只读团体名/SNMPv3 authPriv |
| JMX | 只读 JMX 用户；限制 ObjectName |
| Kubernetes | 只读 `list/watch` 目标资源；不要 cluster-admin |

### AA.8.3 基数与抓取预算

exporter 是基数失控的重灾区。上线前做三件事：

1. 看 `scrape_samples_scraped`：单目标多少样本？
2. 看 `scrape_duration_seconds`：是否接近 `scrape_timeout`？
3. 看 `scrape_series_added`：是否有持续新增序列？

用 `metric_relabel_configs` 丢弃不需要的指标：

```yaml
scrape_configs:
  - job_name: node
    static_configs:
      - targets: ["node-1:9100"]
    metric_relabel_configs:
      # 丢掉不需要的 collector 指标
      - source_labels: [__name__]
        regex: "node_(arp|buddyinfo|netstat|vmstat)_.*"
        action: drop
      # 丢掉高基数标签
      - regex: "container_label_.*"
        action: labeldrop
```

限制：

```yaml
scrape_configs:
  - job_name: node
    sample_limit: 20000
    label_limit: 40
    label_name_length_limit: 200
    label_value_length_limit: 500
    static_configs:
      - targets: ["node-1:9100"]
```

注意：`sample_limit` 触发时整次抓取失败（`up=0`），不是静默截断；这会让问题可见，但也可能让目标"消失"。阈值要留余量。

## AA.9 端到端示例：node + blackbox + MySQL

下面是一份可直接改造的 `prometheus.yml` 片段，包含三类目标与各自的抓取参数：

```yaml
global:
  scrape_interval: 15s
  scrape_timeout: 10s
  evaluation_interval: 15s

scrape_configs:
  - job_name: node
    static_configs:
      - targets:
          - node-1:9100
          - node-2:9100

  - job_name: blackbox-http
    metrics_path: /probe
    params:
      module: [http_2xx]
    static_configs:
      - targets:
          - https://www.example.com
          - https://api.example.com/healthz
    relabel_configs:
      - source_labels: [__address__]
        target_label: __param_target
      - source_labels: [__param_target]
        target_label: instance
      - target_label: __address__
        replacement: blackbox-exporter:9115

  - job_name: mysql
    static_configs:
      - targets:
          - mysql-1:9104
```

配套告警规则：

```yaml
groups:
  - name: host
    rules:
      - alert: HostDown
        expr: up{job="node"} == 0
        for: 5m
        labels:
          severity: critical
        annotations:
          summary: "主机 exporter 不可达：{{ $labels.instance }}"

      - alert: DiskWillFillIn4Hours
        expr: predict_linear(node_filesystem_avail_bytes{job="node",mountpoint="/"}[6h], 4 * 3600) < 0
        for: 10m
        labels:
          severity: warning
        annotations:
          summary: "磁盘预计 4 小时内写满：{{ $labels.instance }}"

      - alert: HighMemoryUsage
        expr: 100 * (1 - node_memory_MemAvailable_bytes{job="node"} / node_memory_MemTotal_bytes{job="node"}) > 90
        for: 15m
        labels:
          severity: warning
        annotations:
          summary: "内存使用率超过 90%：{{ $labels.instance }}"

  - name: probes
    rules:
      - alert: ProbeFailed
        expr: probe_success == 0
        for: 2m
        labels:
          severity: critical
        annotations:
          summary: "探测失败：{{ $labels.instance }}"

      - alert: SSLCertExpiringSoon
        expr: probe_ssl_earliest_cert_expiry - time() < 14 * 24 * 3600
        for: 1h
        labels:
          severity: warning
        annotations:
          summary: "证书即将过期：{{ $labels.instance }}"

  - name: mysql
    rules:
      - alert: MySQLDown
        expr: mysql_up == 0
        for: 2m
        labels:
          severity: critical
        annotations:
          summary: "MySQL exporter 不可达：{{ $labels.instance }}"

      - alert: MySQLReplicaLag
        expr: mysql_slave_status_seconds_behind_master > 60
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "MySQL 主从延迟超过 60s：{{ $labels.instance }}"
```

验证步骤：

```bash
# 1. exporter 自身能出指标
curl -s http://node-1:9100/metrics | head

# 2. Prometheus 能看到目标
curl -s 'http://prometheus:9090/api/v1/targets?state=active'

# 3. 抓取是否健康
curl -s 'http://prometheus:9090/api/v1/query?query=up'

# 4. 规则语法
promtool check rules rules/host.yml

# 5. 规则单元测试
promtool test rules rules/host_test.yml
```

## AA.10 上线检查表

- [ ] 每个 exporter 有明确 owner、版本、升级窗口？
- [ ] 只开启需要的 collector，单目标样本数在预算内？
- [ ] `/metrics` 不暴露公网，凭据用文件/密钥管理？
- [ ] 抓取账号最小权限，且只读？
- [ ] `up`、`scrape_duration_seconds`、`scrape_samples_scraped` 有面板/告警？
- [ ] 探针的 `up` 与 `probe_success` 分开告警？
- [ ] 证书过期、DNS、端口连通性有独立探针？
- [ ] 中间件 exporter 的复制/队列/连接数有告警？
- [ ] 高基数指标有 `metric_relabel_configs` 或预聚合？
- [ ] `sample_limit`/`label_limit` 有阈值且经过压测？
- [ ] exporter 升级后 dashboard/告警指标名已核对？
- [ ] 短任务用 Pushgateway/textfile，且有清理策略？
- [ ] 有"监控的监控"：Prometheus 自身、Alertmanager、exporter 的可用性？

## 延伸阅读

- Prometheus 官方文档：Exporters、Writing exporters、configuration。
- node_exporter、blackbox_exporter、snmp_exporter、jmx_exporter 的 README 与 release notes。
- Percona/PostgreSQL/Redis/Kafka 社区的 exporter 文档。
- vLLM/TGI/Triton/DCGM 的 LLM/GPU 指标语义（TTFT/TPOT、KV cache、XID/ECC）见附录 AI。
- 本教程第 5 章（exposition）、第 11 章（服务发现与 relabel）、第 12 章（抓取生命周期）、第 24 章（Kubernetes 监控）、第 27 章（基数）与附录 T（Pushgateway）、附录 W（埋点）。
