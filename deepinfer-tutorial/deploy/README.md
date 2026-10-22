# 部署示例：容器与 Kubernetes

这个目录把教程第 35 章的部署讨论落到可执行的文件上。它面向 `mini_deepinfer` 的教学服务，不是 DeepInfer 的生产部署模板；生产环境的镜像、设备插件、拓扑调度和存储客户端都要复杂得多。

## 文件

- `Dockerfile`：Python 3.12 slim 镜像，非 root 用户、只读根文件系统友好的布局、健康检查、`SIGTERM` 停止信号。
- `k8s.yaml`：Namespace、ConfigMap、Deployment、Service、PVC、PDB、HPA 和 NetworkPolicy 的最小组合。
- `README.md`：本文件，解释每个字段的意图和改造点。

## 本地构建与运行

```bash
docker build -f deploy/Dockerfile -t mini-deepinfer:0.1.0 .
docker run --rm -p 8000:8000 mini-deepinfer:0.1.0
curl -s http://127.0.0.1:8000/health
curl -s http://127.0.0.1:8000/v1/models
```

镜像默认从源码安装包；实际项目应该在 CI 中构建 wheel，再在镜像里 `pip install` 该 wheel，以保证镜像内容和测试过的产物一致。

## Kubernetes

```bash
kubectl apply -f deploy/k8s.yaml
kubectl -n mini-deepinfer rollout status deployment/mini-deepinfer
kubectl -n mini-deepinfer port-forward svc/mini-deepinfer 8080:80
curl -s http://127.0.0.1:8080/health
```

## 每个字段为什么在那里

**非 root + 只读根文件系统。** 推理进程不需要写代码目录；只挂载 `/tmp` 和模型缓存。这样可以缩小容器逃逸后的影响面。真正需要写的目录（KV 换出、trace、日志）应该显式挂载并设置配额。

**readiness 与 liveness 分开。** readiness 失败会把 Pod 从 Service 端点摘除，但不会重启；liveness 失败会重启。`k8s.yaml` 的 readinessProbe 指向 `/ready`（draining 时返回 503），liveness 指向 `/health`（draining 时仍返回 200，避免 drain 触发重启）。模型加载慢时，`initialDelaySeconds` 要覆盖权重加载和 warmup，否则会陷入“启动未完成就被杀”的循环。

**terminationGracePeriodSeconds + preStop。** 缩容时先摘流量，再给运行中的请求时间完成或转发。`preStop` 现在调用 `POST /admin/drain`，然后在 grace period 内等待；`EngineServer.stop(grace_s)` 会等到 `active_requests == 0` 再关闭 listener。生产服务还要在网关侧先停止新请求，并在超时后把未完成的请求转发或强制 abort。

**PDB。** 节点维护时至少保留一个副本，避免滚动更新或驱逐把服务打到零。

**HPA 用自定义指标。** CPU 利用率不能代表推理负载：decode 可能 GPU 打满但 CPU 很闲，prefill 可能 CPU 在等 tokenizer。更有意义的指标是等待队列长度、KV 占用、P95 TTFT 或 goodput。`mini_deepinfer_queue_depth` 是示例指标名，真实集群需要由 metrics adapter 暴露。

**NetworkPolicy。** 只允许 ingress 命名空间访问 8000 端口；KV 存储、模型仓库、监控的访问要单独放行。多租户场景还要限制 Pod 之间的横向流量。

## 改成 GPU/NPU 部署

1. 基础镜像换成厂商提供的 CUDA/CANN 运行时镜像，并在构建阶段编译 C++/kernel 扩展。
2. 资源请求改成 `nvidia.com/gpu: 1` 或 Ascend 设备插件对应的资源名；`limits` 与 `requests` 必须相等，GPU 不支持超卖。
3. 加 node affinity、taint/toleration 和拓扑约束，尽量让 EP 组落在同一个 NVLink/supernode 域。
4. 模型权重放到共享 PVC 或对象存储缓存，避免每个 Pod 重复下载数百 GB。
5. 健康检查要覆盖“权重已加载、CUDA Graph 已捕获、KV 已分配”，而不只是 HTTP 端口可连。
6. 滚动更新要按 PD 角色分开：先扩新 P，再切流量，最后缩旧 P；D 实例还要处理 KV 交接。

## 不能从这个示例学到的东西

这个目录不包含真实的 GPU 调度、RDMA/3FS 客户端、跨实例 KV 传输、分布式限流、TLS 终止、鉴权网关和成本分摊。它只展示“一个推理进程如何被容器编排”，以及哪些字段在真实部署里会变成故障点。第 35 章会逐项展开。
