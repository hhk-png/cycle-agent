# 第 3 章 实验环境与 MiniK8s 快速开始

## 3.1 两种实验环境

学习 Kubernetes 最好同时准备两种环境：

1. **真实集群**：kind、minikube、k3d 或云托管集群。用于验证 kubectl、网络、存储和真实运行时。
2. **MiniK8s**：本仓库自带的纯 Python 实现。用于观察 API、watch、调度和控制器内部逻辑，不依赖 Docker、root 或云资源。

真实集群适合“用”，MiniK8s 适合“拆”。本书的代码级分析全部基于 MiniK8s，概念与稳定 API 语义与真实集群一致。

## 3.2 MiniK8s 环境要求

- Python 3.10+（本项目在 Python 3.14 上验证）。
- PyYAML、pytest。
- 不需要 Docker、Kubernetes、etcd、root 权限。

安装：

```bash
cd kubernetes-tutorial
python -m pip install -r requirements.txt
```

上面的 `cd` 假设你在仓库根目录；如果你已经在 `kubernetes-tutorial/` 内，可以跳过。所有命令都在该目录下执行。

## 3.3 启动开发集群

第一个终端：

```bash
python -m mini_k8s --port 8080 --nodes 2
```

你会看到：

```text
MiniK8s API server: http://127.0.0.1:8080
Admin token:        mini-admin-token
Nodes:              mini-node-1, mini-node-2
Press Ctrl+C to stop.
```

第二个终端：

```bash
export MINIK8S_SERVER=http://127.0.0.1:8080
export MINIK8S_TOKEN=mini-admin-token

python -m mini_k8s.cli get nodes
python -m mini_k8s.cli get namespaces
python -m mini_k8s.cli api-resources | head
```

Windows PowerShell：

```powershell
$env:MINIK8S_SERVER="http://127.0.0.1:8080"
$env:MINIK8S_TOKEN="mini-admin-token"
python -m mini_k8s.cli get nodes
```

如果使用 bash，也可以先安装命令行入口：

```bash
python -m pip install -e .
mk8s get nodes
```

`mk8s` 默认连接 `http://127.0.0.1:8080`，默认令牌是开发用 `mini-admin-token`。生产环境当然不能这样默认。

## 3.4 第一个工作负载

```bash
python -m mini_k8s.cli apply -f examples/01-hello.yaml
python -m mini_k8s.cli get deployments
python -m mini_k8s.cli get pods -o wide
python -m mini_k8s.cli get services
python -m mini_k8s.cli logs <pod-name>
```

预期：

- Deployment `web` 有 2 个副本。
- Pod 分配到 `mini-node-1` 或 `mini-node-2`。
- Service `web` 获得 `10.96.x.x` 的 ClusterIP。
- Endpoints 中有两个 Ready Pod IP（`10.244.*`）。
- Pod 日志里能看到模拟运行时的启动记录。

访问 Service 的模拟代理：

```python
from mini_k8s.client import ApiClient
client = ApiClient("http://127.0.0.1:8080", token="mini-admin-token")
print(client.list("v1", "Endpoints", "default"))
```

MiniK8s 不在宿主机上真的监听 80 端口。Service 的可达性通过 `ServiceProxy` 和 `ClusterDNS` 模拟，测试中会验证端点选择与 NetworkPolicy。

## 3.5 观察控制循环

开三个终端分别执行：

```bash
# 终端 A：持续观察 Pod（Linux/macOS）
watch -n 0.5 'python -m mini_k8s.cli get pods -o wide'
# Windows PowerShell：
# while ($true) { python -m mini_k8s.cli get pods -o wide; Start-Sleep -Milliseconds 500 }

# 终端 B：扩容
python -m mini_k8s.cli scale deployment web --replicas 5

# 终端 C：改镜像触发滚动更新
python -m mini_k8s.cli set image deployment/web web=nginx:1.28
```

终端 C 的 `set image` 会修改 Deployment 的 Pod 模板并触发新的 ReplicaSet。

观察：

1. 新 ReplicaSet 出现，旧 ReplicaSet 逐步缩容。
2. Pod 数量在 `maxSurge` 内短暂超过期望值。
3. 新 Pod Ready 后，旧 Pod 才被删除。
4. Deployment 的 `status.updatedReplicas` 最终等于期望副本数。

这比背“滚动更新原理”更能建立直觉。

## 3.6 运行测试与示例

完整验证：

```bash
python -m pytest
python examples/run_all.py
python check_docs.py
```

截至当前版本：

- `217` 个测试通过。
- `17` 个端到端示例全部通过。
- 文档检查通过。

只跑某个示例：

```bash
python examples/run_all.py --only 03,14
```

只跑某类测试：

```bash
python -m pytest tests/test_scheduler.py -q
python -m pytest tests/test_api.py -q
python -m pytest tests/test_kubelet.py -q
```

测试会启动多个临时集群，每个使用随机端口，不会互相干扰。

## 3.7 目录导览

```text
mini_k8s/
  api/server.py          HTTP 路由、discovery、watch、子资源
  api/registry.py        GVK/GVR 注册表
  store/store.py         带 resourceVersion 的对象存储
  admission/admission.py 默认值、校验、Quota、PSA
  auth/authn.py          静态 token 与 ServiceAccount JWT
  auth/authz.py          RBAC
  scheduler/scheduler.py 过滤、打分、抢占
  controllers/           各类控制器
  kubelet/kubelet.py     模拟 kubelet
  network/               DNS、Ingress、NetworkPolicy、Proxy
  coordination/leader.py Lease 选主
  client.py              HTTP 客户端
  cli/main.py            mk8s 命令
examples/                18 个 YAML 场景 + Website Operator
tests/                   217 个测试
```

从第 34 章开始，会按模块拆解这些文件。

## 3.8 模拟运行时的约定

MiniK8s 不执行真实命令。为了让 Job、探针、崩溃重启等行为可测试，它定义了一组注解：

| 注解 | 作用 | 默认 |
|---|---|---|
| `mini-k8s.io/simulate.run-ticks` | 容器运行多少个调度 tick 后退出 | Job/一次性 Pod 为 2，长驻容器不退出 |
| `mini-k8s.io/simulate.exit-code` | 退出码 | 0 |
| `mini-k8s.io/simulate.ready-after-ticks` | 启动后多少个 tick 变为 Ready | 0 |
| `mini-k8s.io/simulate.probe-fail` | 让探针始终失败 | false |
| `mini-k8s.io/simulate.cpu-usage` | HPA/metrics 使用的 CPU 使用量 | 请求值的 60% |
| `mini-k8s.io/simulate.memory-usage` | metrics 使用的内存 | 请求值的 70% |

例如让一个 Pod 启动 5 个 tick 后才 Ready：

```yaml
metadata:
  annotations:
    mini-k8s.io/simulate.ready-after-ticks: "5"
```

这些注解只对 MiniK8s 有意义。真实集群会忽略未知注解。

## 3.9 使用真实集群（可选）

MiniK8s 能验证控制面语义，但不能验证真实镜像、网络和存储。准备一个真实集群对照实验，效果最好。

### kind：最快的有 Docker 环境

```bash
kind create cluster --name k8s-lab
kubectl get nodes
kubectl apply -f examples/01-hello.yaml
kubectl get pods -o wide
```

需要 Ingress/NodePort 实验时，创建带端口映射的集群：

```yaml
# kind-config.yaml
kind: Cluster
apiVersion: kind.x-k8s.io/v1alpha4
nodes:
  - role: control-plane
    extraPortMappings:
      - containerPort: 30080
        hostPort: 30080
        protocol: TCP
```

```bash
kind create cluster --name k8s-lab --config kind-config.yaml
```

kind 的节点是 Docker 容器，适合 CI 和本地实验；它不模拟云 LB、多 AZ 或真实云盘。

> **Ingress NGINX 已退役**：社区版在 2026-03 后停止维护，不再发布安全补丁。新实验请用受维护的 Gateway API 实现（Envoy Gateway、Cilium、Traefik 等）或受厂商支持的 Ingress Controller；不要照搬旧的 `ingress-nginx` 安装命令到生产。迁移思路见第 14 章。

### minikube：单机功能完整

```bash
minikube start --cpus=4 --memory=8g --driver=docker
minikube addons enable metrics-server
minikube service web --url
```

minikube 的 addon 生态适合学习 metrics、dashboard；但它的网络/存储与生产差异较大。旧版 `minikube addons enable ingress` 仍可能安装已退役的 ingress-nginx，只建议离线实验使用。

### k3s：轻量、接近生产

```bash
curl -sfL https://get.k3s.io | sh -
sudo k3s kubectl get nodes
```

k3s 把控制面打包成单二进制，默认使用 SQLite（可换 etcd），适合边缘、开发和资源受限环境。它仍然是真实 kubelet/CNI/CRI，不是模拟。

### kubeadm：理解生产集群的起点

```bash
sudo kubeadm init --control-plane-endpoint "k8s-api.example.com:6443" \
  --pod-network-cidr 10.244.0.0/16 --upload-certs
```

kubeadm 会生成证书、静态 Pod、kubeconfig 和 join 命令。生产高可用、CNI/CSI、证书轮换和升级步骤见附录 F。

### 对照实验的注意事项

- `examples/` 中的镜像名是演示值，真实集群会尝试拉取并失败。把 `image` 改成 `nginx:1.29` 等真实镜像。
- MiniK8s 专属注解在真实集群无效，Job 不会自动完成，除非容器真的退出。
- 示例中的 `demo.local` Ingress 需要真实 Ingress Controller 和 DNS/hosts 配置。
- NetworkPolicy 需要支持策略的 CNI（Calico、Cilium 等），否则策略对象存在但不生效。
- 真实集群的 Pod 会有真实 IP、真实卷、真实重启；MiniK8s 的状态机是确定性的。

建议的对照方法：

| 观察点 | MiniK8s | 真实集群 |
|---|---|---|
| Pod 调度 | `spec.nodeName` 被 scheduler 写入 | 看 `kubectl get pod -o wide` 与事件 |
| 探针 | tick 模拟 | 真实 HTTP/TCP/exec |
| 存储 | hostPath PV | CSI 云盘/NFS |
| Service | ServiceProxy 判定 | kube-proxy/eBPF 真实转发 |
| 排障 | `diagnostics()`/事件 | `describe`/logs/节点工具 |

真实集群用于验证“行为”，MiniK8s 用于验证“机制”。两者结合，学习效果最好。

## 3.10 常见启动问题

**端口被占用**：换 `--port 8081`。

**`ModuleNotFoundError: mini_k8s`**：确认在 `kubernetes-tutorial` 目录下执行，或 `pip install -e .`。

**CLI 返回 401**：检查 `MINIK8S_TOKEN`；MiniK8s 默认管理员令牌是 `mini-admin-token`。

**CLI 返回 403**：你可能用了 `mini-dev-token`，它没有默认权限；第 23 章会创建 RoleBinding。

**Windows 上 Ctrl+C 后端口未释放**：等待进程退出；必要时用任务管理器结束 Python 进程。

**测试很慢**：测试会启动多个集群，通常 20～40 秒；可以按文件运行。

## 本章小结

- MiniK8s 无需 Docker 即可运行，适合观察控制面机制。
- 启动集群、应用 YAML、扩容、看事件，是后续每章的基本实验循环。
- 模拟注解让 Job、探针、HPA 等行为确定可测，但不代表真实运行时行为。
- 真实集群与 MiniK8s 各有所长，建议对照使用。

## 延伸阅读

- [第 5 章 声明式工作流](05-declarative-workflow.md)
- [第 34 章 MiniK8s 总体设计](34-minik8s-design.md)
- [第 39 章 测试策略](39-testing.md)
- [附录 F 集群搭建与生产化手册](../appendices/F-cluster-build.md)
- [附录 J 自测题库与实验手册](../appendices/J-self-assessment-labs.md)：从启动集群开始的完整实验序列。
