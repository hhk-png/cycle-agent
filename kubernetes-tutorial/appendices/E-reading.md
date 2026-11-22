# 附录 E 延伸阅读

> 链接可能随项目演进变化；以官方站点为准。Kubernetes 源码链接固定到 **v1.31.0**（与教程基线一致），避免 `master` 路径漂移。

## E.1 官方文档与规范

- Kubernetes Documentation：`https://kubernetes.io/docs/`
- Kubernetes API Reference：`https://kubernetes.io/docs/reference/kubernetes-api/`
- Kubernetes GitHub：`https://github.com/kubernetes/kubernetes`
- Kubernetes Enhancement Proposals (KEP)：`https://github.com/kubernetes/enhancements`
- Gateway API：`https://gateway-api.sigs.k8s.io/`
- CRD/API conventions：`https://github.com/kubernetes/community/blob/master/contributors/devel/sig-architecture/api-conventions.md`
- Scheduler Framework：`https://kubernetes.io/docs/concepts/scheduling-eviction/scheduling-framework/`
- Pod Security Standards：`https://kubernetes.io/docs/concepts/security/pod-security-standards/`
- Dynamic Resource Allocation：`https://kubernetes.io/docs/concepts/scheduling-eviction/dynamic-resource-allocation/`

## E.2 源码与实现

- `kube-apiserver`：`https://github.com/kubernetes/kubernetes/tree/v1.31.0/cmd/kube-apiserver`
- `kube-scheduler`：`https://github.com/kubernetes/kubernetes/tree/v1.31.0/pkg/scheduler`
- `kube-controller-manager`：`https://github.com/kubernetes/kubernetes/tree/v1.31.0/cmd/kube-controller-manager`
- `kubelet`：`https://github.com/kubernetes/kubernetes/tree/v1.31.0/pkg/kubelet`
- client-go：`https://github.com/kubernetes/client-go`
- controller-runtime：`https://github.com/kubernetes-sigs/controller-runtime`
- Kubebuilder：`https://book.kubebuilder.io/`
- Operator SDK：`https://sdk.operatorframework.io/`
- etcd：`https://github.com/etcd-io/etcd`

## E.3 网络

- CNI spec：`https://github.com/containernetworking/cni`
- Cilium docs：`https://docs.cilium.io/`
- Calico docs：`https://docs.tigera.io/calico/latest/about/`
- Istio docs：`https://istio.io/latest/docs/`
- Linkerd docs：`https://linkerd.io/2/overview/`
- Envoy docs：`https://www.envoyproxy.io/docs`
- eBPF：`https://ebpf.io/`

## E.4 存储

- CSI spec：`https://github.com/container-storage-interface/spec`
- Kubernetes storage docs：`https://kubernetes.io/docs/concepts/storage/`
- Velero（备份）：`https://velero.io/`
- OpenEBS：`https://openebs.io/`
- Rook：`https://rook.io/`

## E.5 安全

- SIG Security：`https://github.com/kubernetes/community/tree/master/sig-security`
- Sigstore：`https://www.sigstore.dev/`
- SLSA：`https://slsa.dev/`
- OPA Gatekeeper：`https://open-policy-agent.github.io/gatekeeper/website/`
- Kyverno：`https://kyverno.io/`
- Falco：`https://falco.org/`
- Tetragon：`https://tetragon.io/`
- Kubernetes security checklist：`https://kubernetes.io/docs/concepts/security/`

## E.6 可观测性

- Prometheus：`https://prometheus.io/docs/`
- OpenTelemetry：`https://opentelemetry.io/docs/`
- Grafana：`https://grafana.com/docs/`
- Loki：`https://grafana.com/docs/loki/latest/`
- Jaeger：`https://www.jaegertracing.io/docs/`
- OpenCost：`https://opencost.io/`

## E.7 批处理/AI

- Kueue：`https://kueue.sigs.k8s.io/`
- Volcano：`https://volcano.sh/`
- YuniKorn：`https://yunikorn.apache.org/`
- Kubeflow：`https://www.kubeflow.org/docs/`
- Ray on Kubernetes：`https://docs.ray.io/en/latest/cluster/kubernetes/index.html`
- KServe：`https://kserve.github.io/website/`
- NVIDIA GPU Operator：`https://docs.nvidia.com/datacenter/cloud-native/gpu-operator/`
- Kubeflow Training Operator：`https://www.kubeflow.org/docs/components/training/`
- JobSet：`https://jobset.sigs.k8s.io/`
- vLLM：`https://docs.vllm.ai/`
- Triton Inference Server：`https://docs.nvidia.com/deeplearning/triton-inference-server/`
- DCGM Exporter：`https://github.com/NVIDIA/dcgm-exporter`

## E.8 平台工程/运营

- CNCF Landscape：`https://landscape.cncf.io/`
- Team Topologies：`https://teamtopologies.com/`
- DORA：`https://dora.dev/`
- Argo CD：`https://argo-cd.readthedocs.io/`
- Flux：`https://fluxcd.io/docs/`
- Backstage：`https://backstage.io/docs/`
- Crossplane：`https://docs.crossplane.io/`
- Cluster API：`https://cluster-api.sigs.k8s.io/`
- Karmada：`https://karmada.io/`
- Argo Rollouts：`https://argoproj.github.io/argo-rollouts/`
- Flagger：`https://flagger.app/`
- vCluster：`https://www.vcluster.com/docs`
- KubeVirt：`https://kubevirt.io/user-guide/`
- Knative：`https://knative.dev/docs/`
- K3s：`https://docs.k3s.io/`
- KubeEdge：`https://kubeedge.io/`
- SpinKube：`https://www.spinkube.dev/`

## E.9 书籍

- *Kubernetes: Up and Running*（Burns/Hightower/Beda）
- *Kubernetes Patterns*（Ibryam/Huß）
- *Programming Kubernetes*（Hausenblas/Schimmel）
- *Designing Data-Intensive Applications*（Kleppmann）
- *Site Reliability Engineering*（Google SRE Book）
- *The Phoenix Project* / *Accelerate*（DevOps/组织）
- *Team Topologies*（Skelton/Pais）

## E.10 论文/设计文档

- Borg, Omega, and Kubernetes（Google，ACM Queue）
- Large-scale cluster management at Google with Borg（EuroSys）
- Raft: In Search of an Understandable Consensus Algorithm
- The Tail at Scale（Dean & Barroso）
- SEDA / workqueue 相关设计
- Kubernetes KEPs（按兴趣读）

## E.11 本书配套

- [附录 J 自测题库与实验手册](J-self-assessment-labs.md)：按章节自测、动手实验、结业项目。
- [附录 K etcd 深入](K-etcd-operations.md)：一致性、备份、恢复与调优。
- [附录 L 云原生生态地图](L-ecosystem-map.md)：分层地图与选型方法。
- [附录 M 生产事故案例库](M-incident-casebook.md)：按症状与证据训练排障顺序。
- [附录 N 成本工程手册](N-cost-engineering.md)：从账单到 Pod 的成本模型与优化。
- [附录 O 网络取证手册](O-network-forensics.md)：DNS、Service、CNI、MTU 的分层取证。
- [附录 P 字段语义与 YAML 陷阱](P-defaults-and-yaml-traps.md)：默认值、可变性与高频配置错误。
- [附录 Q 扩展点与交付物开发实战](Q-extension-development.md)：client-go/controller-runtime/webhook/Helm 的工程骨架。

## E.12 学习方法

1. 先读本教程对应章节，建立模型。
2. 读官方文档的概念页，不看“任务页”先。
3. 读 KEP/设计文档，理解取舍。
4. 读源码的关键路径（API/storage/scheduler/controller）。
5. 在真实集群做实验，观察事件/指标。
6. 写复盘/笔记，输出倒逼理解。

## 延伸阅读

- [第 1 章 为什么需要 Kubernetes](../chapters/01-why-kubernetes.md)
- [第 34 章 MiniK8s 总体设计](../chapters/34-minik8s-design.md)
- [第 42 章 云原生未来方向](../chapters/42-future.md)
