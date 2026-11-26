# 第 11 章 服务发现与 relabel

## 11.1 为什么需要服务发现

静态目标清单在云原生环境里必然过期：

- Pod 重启换 IP；
- Deployment 扩缩容；
- 节点加入/离开；
- 蓝绿发布切换标签；
- 多集群/多租户动态变化。

服务发现（SD）把"目标从哪来"从配置文件里解耦出来。Prometheus 支持几十种 SD，核心接口一致：

```text
SD 提供目标列表（每个目标带一组 __meta_* 标签）
        ↓
relabel 把元数据转换成最终抓取参数/标签
        ↓
抓取
```

Mini 实现 static、file_sd、http_sd、dns_sd 四种；Kubernetes/EC2/Consul 在真实 Prometheus 中使用相同接口。

## 11.2 static_configs

```yaml
scrape_configs:
  - job_name: node
    static_configs:
      - targets: ["10.0.0.1:9100", "10.0.0.2:9100"]
        labels:
          env: prod
          region: cn-north
```

- `targets` 是 `host:port`；
- `labels` 附加到该组所有目标；
- `job` 来自 `job_name`；
- `instance` 默认是 `__address__`。

适合固定基础设施、测试、少量目标。

## 11.3 file_sd_configs

```yaml
- job_name: node
  file_sd_configs:
    - files: ["/etc/prometheus/sd/*.json", "/etc/prometheus/sd/*.yml"]
      refresh_interval: 30s
```

文件内容：

```json
[
  {
    "targets": ["10.0.0.1:9100", "10.0.0.2:9100"],
    "labels": {"env": "prod", "rack": "r1"}
  }
]
```

特点：

- 支持 JSON/YAML；
- 文件变更自动生效（按 refresh interval 轮询）；
- 适合由 CMDB/Ansible/自定义控制器生成；
- 文件删除/目标移除会触发 stale 标记。

陷阱：

- 文件必须整体可解析；部分写入会导致整次刷新失败。
- 路径相对于 Prometheus 进程工作目录，不是配置文件目录（Mini 为了易用改为相对配置目录，生产以官方行为为准）。
- 多个文件的目标会合并；重复目标会去重。

## 11.4 http_sd_configs

```yaml
- job_name: edge
  http_sd_configs:
    - url: https://cmdb.example/api/prometheus/targets
      refresh_interval: 1m
      headers:
        Authorization: Bearer <token>
```

响应格式与 file_sd 相同（JSON 数组）。适合动态控制器/服务目录。

安全：

- 必须使用 HTTPS/认证；
- 防止 SSRF：Prometheus 可以访问内网，配置权限要受限；
- 响应大小/超时要有上限；
- 失败时保留上一次成功的目标集（Mini 也这样实现）。

## 11.5 dns_sd_configs

```yaml
- job_name: dns
  dns_sd_configs:
    - names: ["api.internal.example"]
      type: A
      port: 9100
      refresh_interval: 30s
```

- `A`/`AAAA`：解析出 IP，端口用 `port`；
- `SRV`：从 SRV 记录获取端口和目标；
- 适合无 SD API 但有 DNS 的环境；
- DNS TTL/缓存会影响发现延迟。

Mini 只实现 A/AAAA，SRV 需要 DNS 客户端库。

## 11.6 Kubernetes SD

这是最常见的生产 SD。角色（role）：

```text
node, pod, service, endpoints, endpointslice, ingress
```

以 pod 为例：

```yaml
- job_name: kubernetes-pods
  kubernetes_sd_configs:
    - role: pod
  relabel_configs:
    - source_labels: [__meta_kubernetes_pod_annotation_prometheus_io_scrape]
      action: keep
      regex: "true"
    - source_labels: [__meta_kubernetes_pod_annotation_prometheus_io_path]
      action: replace
      target_label: __metrics_path__
      regex: (.+)
    - source_labels: [__address__, __meta_kubernetes_pod_annotation_prometheus_io_port]
      action: replace
      regex: ([^:]+)(?::\d+)?;(\d+)
      replacement: $1:$2
      target_label: __address__
    - source_labels: [__meta_kubernetes_namespace]
      target_label: namespace
    - source_labels: [__meta_kubernetes_pod_name]
      target_label: pod
    - source_labels: [__meta_kubernetes_pod_label_app]
      target_label: app
    - action: labelmap
      regex: __meta_kubernetes_pod_label_(.+)
```

常用 meta 标签：

```text
__meta_kubernetes_pod_name
__meta_kubernetes_pod_ip
__meta_kubernetes_pod_node_name
__meta_kubernetes_pod_phase
__meta_kubernetes_pod_label_<label>
__meta_kubernetes_pod_annotation_<annotation>
__meta_kubernetes_namespace
__meta_kubernetes_service_name
__meta_kubernetes_endpoint_address_target_name
```

生产建议：

- 用 Prometheus Operator 的 `PodMonitor`/`ServiceMonitor`，而不是手写 SD；
- 用 `namespace`/`label` 选择器限制发现范围；
- 只保留需要的 meta 标签（`labelmap` 会产生大量标签）；
- 注意 `__meta_kubernetes_pod_container_*` 等标签的基数。

## 11.7 其他主流 SD

- **EC2**：`ec2_sd_configs`，按 region/tag/security group 发现实例；需要 IAM 权限。
- **Azure**：`azure_sd_configs`，按 resource group/tag。
- **GCE**：`gce_sd_configs`。
- **Consul**：`consul_sd_configs`，服务/节点/健康检查。
- **Nomad**、**Docker**、**OpenStack**、**Vultr** 等。
- **Kuma**、**Eureka**、**Ionos** 等长尾。

选择原则：优先使用"平台原生 + 权限最小"的 SD；不要让 Prometheus 拥有过大的云账号权限。

## 11.8 relabel 的语义

relabel 是"对标签做转换/过滤"的规则引擎。三个阶段：

1. **relabel_configs**：抓取前，作用于目标标签（含 `__meta_*`、`__address__`）。
2. **metric_relabel_configs**：解析后，作用于样本标签。
3. **alert_relabel_configs**：告警发送前，作用于告警标签。

字段：

```yaml
- source_labels: [__address__, __meta_kubernetes_pod_annotation_prometheus_io_port]
  separator: ";"
  regex: "([^:]+)(?::\\d+)?;(\\d+)"
  target_label: __address__
  replacement: "$1:$2"
  action: replace
```

默认值：

```text
separator: ";"
regex: "(.*)"
replacement: "$1"
action: replace
```

**正则默认全匹配**（`^(?:...)$`），不是子串匹配。要子串匹配需写 `.*foo.*`。

## 11.9 动作清单

| action | 作用 |
|---|---|
| replace | 用 source 拼接后匹配 regex，展开 replacement，写入 target |
| keep | source 匹配 regex 则保留目标，否则丢弃 |
| drop | source 匹配 regex 则丢弃目标 |
| hashmod | 对 source 的 MD5 前 8 字节（大端）取模，写入 target（分片） |
| labelmap | 对标签名匹配 regex，展开为新标签名，复制值 |
| labeldrop | 删除名称匹配 regex 的标签 |
| labelkeep | 只保留名称匹配 regex 的标签 |
| lowercase | source 转小写写入 target |
| uppercase | source 转大写写入 target |
| keepequal | source 与 target 值相等才保留 |
| dropequal | source 与 target 值相等才丢弃 |

### 特殊标签

- `__address__`：抓取地址，可改写端口/主机；
- `__scheme__`：http/https；
- `__metrics_path__`：路径；
- `__param_<name>`：URL 查询参数；
- `__scrape_interval__`/`__scrape_timeout__`：按目标覆盖（部分版本支持）；
- `__meta_*`：SD 元数据，不会进入样本。

## 11.10 分片（hashmod）

大规模抓取时，用多个 Prometheus 实例分担目标：

```yaml
relabel_configs:
  - source_labels: [__address__]
    modulus: 4
    target_label: __tmp_shard
    action: hashmod
  - source_labels: [__tmp_shard]
    regex: "0"
    action: keep
```

- 每个实例配置不同的 `regex`（0/1/2/3）；
- 目标变化时 shard 可能移动，导致短暂重复/缺口；
- 必须配合外部标签和全局查询层（Thanos/Mimir）使用；
- 不要让每个实例都抓全量再丢弃（浪费）。

## 11.11 常见错误

- **regex 忘记锚定**：`node` 不会匹配 `node-exporter`；要写 `.*node.*`。
- **labelmap 放大基数**：把全部 `__meta_kubernetes_pod_label_*` 映射成业务标签。
- **`instance` 被覆盖**：relabel 后 `instance` 变成空值，序列冲突。
- **keep 条件写反**：把应该抓的目标丢掉。
- **文件 SD 部分写入**：整次刷新失败，旧目标继续。
- **K8s 权限过大**：Prometheus 能读所有 namespace/secret。
- **没有删除 `__meta_*`**：虽然不会进样本，但会占用 relabel 内存和日志。

## 11.12 Mini 的实现

- `scrape/discovery.py`：static/file/http/dns，统一返回 `DiscoveredTarget`；
- `scrape/relabel.py`：11 种 action，regex 全匹配，`$1`/`${name}` 展开；
- `ScrapeManager.sync()`：对比期望目标集，增删并写 stale；
- `target_key`：job + 标签哈希，用于稳定识别目标。

Mini 不支持 Kubernetes SD；第 24 章给出生产配置，附录 B 给出完整参考。

## 本章小结

- SD 提供 `__meta_*` 标签，relabel 把它们转换为抓取参数和业务标签。
- static/file/http/dns 覆盖大多数非 K8s 场景；K8s 用 Operator 更稳。
- relabel 正则全匹配，动作有 replace/keep/drop/labelmap/hashmod 等。
- `__address__`/`__scheme__`/`__metrics_path__`/`__param_*` 是抓取控制标签。
- 分片、命名空间过滤、元数据裁剪是规模化发现的关键。

## 延伸阅读

- Prometheus 官方 "Configuration" 的 `<scrape_config>` 与 `relabel_config`。
- Kubernetes SD 文档与 prometheus-operator 文档。
- 本教程第 12 章（抓取生命周期）、第 39 章（Mini 实现）与附录 AA（blackbox/SNMP 的多目标 relabel 实战）。
