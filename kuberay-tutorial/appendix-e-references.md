# 附录 E. 参考资料

> 版本基准：KubeRay v1.7.1（2026-09），Ray 2.59.0。文档 URL 可能随版本变化；以官方最新页面为准。

## E.1 KubeRay

- KubeRay 仓库：<https://github.com/ray-project/kuberay>
- v1.7.1 Release：<https://github.com/ray-project/kuberay/releases/tag/v1.7.1>
- v1.7.0 Release（History Server/增量升级发布说明为 beta；mTLS/NetworkPolicy alpha）：<https://github.com/ray-project/kuberay/releases/tag/v1.7.0>
- v1.6.0 Release（RayCronJob、deletion policy）：<https://github.com/ray-project/kuberay/releases/tag/v1.6.0>
- v1.5.0 Release（label selector、SidecarMode、增量升级 alpha）：<https://github.com/ray-project/kuberay/releases/tag/v1.5.0>
- v1.4.0 Release（Dashboard、APIServer v2、SLI 指标）：<https://github.com/ray-project/kuberay/releases/tag/v1.4.0>
- Helm Charts：<https://github.com/ray-project/kuberay-helm>
- CRD 源码：<https://github.com/ray-project/kuberay/tree/master/ray-operator/config/crd/bases>
- API 类型：<https://github.com/ray-project/kuberay/tree/master/ray-operator/apis/ray/v1>
- 控制器：<https://github.com/ray-project/kuberay/tree/master/ray-operator/controllers/ray>
- Feature gates：<https://github.com/ray-project/kuberay/blob/master/ray-operator/pkg/features/features.go>

## E.2 Ray 文档

- Ray 文档首页：<https://docs.ray.io/en/latest/>
- KubeRay 入门：<https://docs.ray.io/en/latest/cluster/kubernetes/getting-started.html>
- RayCluster 快速开始：<https://docs.ray.io/en/latest/cluster/kubernetes/getting-started/raycluster-quick-start.html>
- RayJob 快速开始：<https://docs.ray.io/en/latest/cluster/kubernetes/getting-started/rayjob-quick-start.html>
- RayService 快速开始：<https://docs.ray.io/en/latest/cluster/kubernetes/getting-started/rayservice-quick-start.html>
- RayCronJob 快速开始：<https://docs.ray.io/en/master/cluster/kubernetes/getting-started/raycronjob-quick-start.html>
- 配置自动扩缩容：<https://docs.ray.io/en/latest/cluster/kubernetes/user-guides/configuring-autoscaling.html>
- GCS 容错：<https://docs.ray.io/en/master/cluster/kubernetes/user-guides/kuberay-gcs-ft.html>
- kubectl 插件：<https://docs.ray.io/en/master/cluster/kubernetes/user-guides/kubectl-plugin.html>
- KubeRay Dashboard：<https://docs.ray.io/en/master/cluster/kubernetes/user-guides/kuberay-dashboard.html>
- History Server：<https://docs.ray.io/en/master/cluster/kubernetes/user-guides/kuberay-history-server.html>
- 指标参考：<https://docs.ray.io/en/master/cluster/kubernetes/k8s-ecosystem/metrics-references.html>
- Ray Job Submission：<https://docs.ray.io/en/latest/cluster/running-applications/job-submission/index.html>
- Ray Serve：<https://docs.ray.io/en/latest/serve/index.html>
- Ray autoscaler：<https://docs.ray.io/en/latest/cluster/vms/user-guides/configuring-autoscaling.html>
- Ray label selector：<https://docs.ray.io/en/latest/ray-core/scheduling/labels.html>
- Ray token auth：<https://docs.ray.io/en/latest/ray-security/token-auth.html>
- KubeRay + K8s RBAC auth：<https://docs.ray.io/en/master/cluster/kubernetes/user-guides/kuberay-auth-rbac.html>
- Ray Jobs API：<https://docs.ray.io/en/latest/cluster/running-applications/job-submission/index.html>
- Ray runtime_env：<https://docs.ray.io/en/latest/ray-core/handling-dependencies.html>
- Ray Client（已废弃）：<https://docs.ray.io/en/latest/cluster/running-applications/job-submission/ray-client.html>（迁移讨论见 <https://github.com/ray-project/ray/issues/47700>）
- Ray 优雅退出/drain：<https://github.com/ray-project/ray/pull/64454>（`RAY_GRACEFUL_SHUTDOWN_DRAIN_TIMEOUT_S`，以版本为准）

## E.3 Kubernetes

- Operator 模式：<https://kubernetes.io/docs/concepts/extend-kubernetes/operator/>
- CRD：<https://kubernetes.io/docs/concepts/extend-kubernetes/api-extension/custom-resources/>
- Finalizers：<https://kubernetes.io/docs/concepts/overview/working-with-objects/finalizers/>
- OwnerReferences：<https://kubernetes.io/docs/concepts/overview/working-with-objects/owners-dependents/>
- Pod Security Standards：<https://kubernetes.io/docs/concepts/security/pod-security-standards/>
- NetworkPolicy：<https://kubernetes.io/docs/concepts/services-networking/network-policies/>
- ResourceQuota：<https://kubernetes.io/docs/concepts/policy/resource-quotas/>
- Scheduler：<https://kubernetes.io/docs/concepts/scheduling-eviction/kube-scheduler/>
- Pod 生命周期与终止：<https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/>
- 容器生命周期钩子（preStop）：<https://kubernetes.io/docs/concepts/containers/container-lifecycle-hooks/>
- Disruptions / PDB：<https://kubernetes.io/docs/concepts/workloads/pods/disruptions/>
- Eviction API：<https://kubernetes.io/docs/concepts/scheduling-eviction/api-eviction/>
- 节点压力驱逐：<https://kubernetes.io/docs/concepts/scheduling-eviction/node-pressure-eviction/>
- Topology Spread：<https://kubernetes.io/docs/concepts/scheduling-eviction/topology-spread-constraints/>
- Cluster Autoscaler FAQ：<https://github.com/kubernetes/autoscaler/blob/master/cluster-autoscaler/FAQ.md>
- Karpenter：<https://karpenter.sh/docs/>
- controller-runtime：<https://pkg.go.dev/sigs.k8s.io/controller-runtime>
- client-go workqueue：<https://pkg.go.dev/k8s.io/client-go/util/workqueue>
- SLSA：<https://slsa.dev/spec/v1.0/>
- Gateway API：<https://gateway-api.sigs.k8s.io/>
- Kueue：<https://kueue.sigs.k8s.io/>
- Volcano：<https://volcano.sh/>
- Apache YuniKorn：<https://yunikorn.apache.org/>
- scheduler-plugins：<https://github.com/kubernetes-sigs/scheduler-plugins>
- cert-manager：<https://cert-manager.io/>
- Prometheus Operator：<https://prometheus-operator.dev/>

## E.4 进阶主题

- Ray Train：<https://docs.ray.io/en/latest/train/train.html>
- Ray Data：<https://docs.ray.io/en/latest/data/data.html>
- Ray Data 执行器/性能：<https://docs.ray.io/en/latest/data/performance-tips.html>
- Ray Data shuffle：<https://docs.ray.io/en/latest/data/performance-tips.html#shuffle>
- Ray Train 弹性/容错：<https://docs.ray.io/en/latest/train/user-guides/fault-tolerance.html>
- Ray Train checkpoint：<https://docs.ray.io/en/latest/train/user-guides/checkpoints.html>
- PyTorch TorchElastic：<https://docs.pytorch.org/docs/stable/elastic/run.html>
- vLLM PagedAttention：<https://docs.vllm.ai/en/latest/design/kernel/paged_attention.html>
- vLLM 前缀缓存：<https://docs.vllm.ai/en/latest/features/automatic_prefix_caching.html>
- SGLang RadixAttention：<https://docs.sglang.ai/advanced_features/router.html>
- Ray Tune：<https://docs.ray.io/en/latest/tune/index.html>
- RLlib：<https://docs.ray.io/en/latest/rllib/index.html>
- Ray Serve LLM：<https://docs.ray.io/en/latest/serve/llm/index.html>
- vLLM：<https://docs.vllm.ai/>
- SGLang：<https://docs.sglang.ai/>
- Argo Workflows：<https://argo-workflows.readthedocs.io/>
- Airflow：<https://airflow.apache.org/docs/>
- OpenCost：<https://opencost.io/docs/>
- KubeRay 源码：<https://github.com/ray-project/kuberay>
- KubeRay API 参考：<https://docs.ray.io/en/master/cluster/kubernetes/references/api.html>

## E.5 GPU、设备与拓扑

- Kubernetes device plugin：<https://kubernetes.io/docs/concepts/extend-kubernetes/compute-storage-net/device-plugins/>
- Kubernetes DRA：<https://kubernetes.io/docs/concepts/scheduling-eviction/dynamic-resource-allocation/>
- NVIDIA GPU Operator：<https://docs.nvidia.com/datacenter/cloud-native/gpu-operator/latest/>
- GPU Feature Discovery：<https://github.com/NVIDIA/gpu-feature-discovery>
- MIG：<https://docs.nvidia.com/datacenter/tesla/mig-user-guide/>
- Topology Manager：<https://kubernetes.io/docs/tasks/administer-cluster/topology-manager/>
- SR-IOV Network Operator：<https://github.com/k8snetworkplumbingwg/sriov-network-operator>
- RDMA shared device plugin：<https://github.com/Mellanox/rdma-shared-device-plugin>

## E.6 网络、规模化与发布工程

- Kubernetes Service/EndpointSlice：<https://kubernetes.io/docs/concepts/services-networking/service/>
- Kubernetes DNS：<https://kubernetes.io/docs/concepts/services-networking/dns-pod-service/>
- NetworkPolicy：<https://kubernetes.io/docs/concepts/services-networking/network-policies/>
- Gateway API：<https://gateway-api.sigs.k8s.io/>
- Cilium：<https://docs.cilium.io/>
- Calico：<https://docs.tigera.io/calico/latest/about/>
- CoreDNS：<https://coredns.io/manual/toc/>
- controller-runtime：<https://pkg.go.dev/sigs.k8s.io/controller-runtime>
- Kubernetes API Priority and Fairness：<https://kubernetes.io/docs/concepts/cluster-administration/flow-control/>
- KubeRay REP/设计文档：<https://github.com/ray-project/enhancements>
- SLSA：<https://slsa.dev/>
- Sigstore/cosign：<https://docs.sigstore.dev/>
- Syft（SBOM）：<https://github.com/anchore/syft>
- Kyverno：<https://kyverno.io/docs/>
- Gatekeeper：<https://open-policy-agent.github.io/gatekeeper/website/docs/>
- Kubernetes CEL validation：<https://kubernetes.io/docs/reference/using-api/cel/>
- OpenTelemetry semantic conventions：<https://opentelemetry.io/docs/specs/semconv/>
- Google SRE Workbook（SLO/错误预算）：<https://sre.google/workbook/implementing-slos/>
- MLflow Model Registry：<https://mlflow.org/docs/latest/model-registry.html>
- Feast（feature store）：<https://docs.feast.dev/>
- OpenLineage：<https://openlineage.io/docs/>
- Apache Iceberg：<https://iceberg.apache.org/docs/latest/>

## E.7 工具与生态

- Helm：<https://helm.sh/>
- kustomize：<https://kustomize.io/>
- Argo CD：<https://argo-cd.readthedocs.io/>
- Flux：<https://fluxcd.io/>
- OpenTelemetry：<https://opentelemetry.io/>
- DCGM Exporter：<https://github.com/NVIDIA/dcgm-exporter>
- py-spy：<https://github.com/benfred/py-spy>
- Trivy：<https://trivy.dev/>
- cosign：<https://docs.sigstore.dev/cosign/overview/>

## E.8 本仓库文件

- 教程目录：[README.md](README.md)
- 简版实现：[minikuberay](minikuberay)
- CRD 清单：[manifests/crds](manifests/crds)
- 样例清单：[manifests/samples](manifests/samples)
- 测试：[tests](tests)
- 文档检查：[check_docs.py](check_docs.py)

## E.9 版本说明

- 本教程的字段/默认值/特性门控以 KubeRay v1.7.1 源码为基准；
- Ray 行为以 Ray 2.59 文档为基准；
- Kubernetes API 以 1.30+ 为基准；
- 若你使用更旧/更新的版本，请先核对 release notes 和 CRD schema；
- MiniKubeRay 是教学实现，不与任何生产版本承诺兼容。

← [返回目录](README.md)
