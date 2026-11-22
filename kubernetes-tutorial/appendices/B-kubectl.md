# 附录 B kubectl 速查

## B.1 上下文与集群

```bash
kubectl config get-contexts
kubectl config current-context
kubectl config use-context prod
kubectl cluster-info
kubectl version --client
kubectl api-resources
kubectl api-versions
```

MiniK8s：

```bash
python -m mini_k8s.cli cluster-info
python -m mini_k8s.cli version
python -m mini_k8s.cli api-resources
```

## B.2 查看资源

```bash
kubectl get pods
kubectl get pods -A
kubectl get pods -o wide
kubectl get pods -o yaml
kubectl get pods -l app=web
kubectl get pods --field-selector status.phase=Running
kubectl get deploy,rs,pod
kubectl get all
kubectl get events --sort-by=.lastTimestamp
```

MiniK8s：

```bash
python -m mini_k8s.cli get pods -o wide
python -m mini_k8s.cli get all
python -m mini_k8s.cli get pods -l app=web
```

## B.3 详情与排障

```bash
kubectl describe pod <pod>
kubectl logs <pod>
kubectl logs <pod> -c <container>
kubectl logs <pod> --previous
kubectl logs -f <pod>
kubectl exec -it <pod> -- sh
kubectl port-forward <pod> 8080:8080
kubectl debug -it <pod> --image=busybox --target=<container>
```

MiniK8s：

```bash
python -m mini_k8s.cli describe pod <pod>
python -m mini_k8s.cli logs <pod>
python -m mini_k8s.cli exec <pod> -- echo hello
```

## B.4 创建/修改

```bash
kubectl apply -f app.yaml
kubectl apply -f dir/
kubectl apply -k overlays/prod
kubectl create -f app.yaml
kubectl replace -f app.yaml
kubectl delete -f app.yaml
kubectl delete pod <pod>
kubectl delete pods -l app=web
kubectl patch deployment web -p '{"spec":{"replicas":5}}'
kubectl edit deployment web
kubectl scale deployment web --replicas=5
kubectl set image deployment/web web=nginx:1.28
kubectl rollout restart deployment/web
```

MiniK8s：

```bash
python -m mini_k8s.cli apply -f examples/01-hello.yaml
python -m mini_k8s.cli scale deployment web --replicas 5
python -m mini_k8s.cli set image deployment/web web=nginx:1.28
python -m mini_k8s.cli delete pod <pod>
```

## B.5 发布与回滚

```bash
kubectl rollout status deployment/web
kubectl rollout history deployment/web
kubectl rollout undo deployment/web
kubectl rollout undo deployment/web --to-revision=2
kubectl rollout pause deployment/web
kubectl rollout resume deployment/web
```

MiniK8s：

```bash
python -m mini_k8s.cli rollout status deployment web
python -m mini_k8s.cli rollout history deployment web
python -m mini_k8s.cli rollout undo deployment web --to-revision=1
```

## B.6 节点运维

```bash
kubectl get nodes
kubectl describe node <node>
kubectl cordon <node>
kubectl uncordon <node>
kubectl drain <node> --ignore-daemonsets --delete-emptydir-data
kubectl taint node <node> key=value:NoSchedule
kubectl taint node <node> key-
```

MiniK8s：

```bash
python -m mini_k8s.cli cordon mini-node-1
python -m mini_k8s.cli drain mini-node-1 --ignore-daemonsets
python -m mini_k8s.cli taint mini-node-1 dedicated=db:NoSchedule
```

## B.7 调试网络

```bash
kubectl run nettest --image=nicolaka/netshoot -- sleep 3600
kubectl exec -it nettest -- nslookup web
kubectl exec -it nettest -- curl -v http://web
kubectl exec -it nettest -- tcpdump -i any -n port 80
```

## B.8 安全与权限

```bash
kubectl auth can-i get pods
kubectl auth can-i --list
kubectl auth can-i create pods --as=system:serviceaccount:default:deployer
kubectl create role pod-reader --verb=get,list,watch --resource=pods
kubectl create rolebinding read --role=pod-reader --user=alice
kubectl create serviceaccount deployer
kubectl create token deployer
```

MiniK8s：

```bash
python -m mini_k8s.cli auth can-i get pods
```

## B.9 配置与 Secret

```bash
kubectl create configmap app-config --from-literal=APP_MODE=prod
kubectl create secret generic app-secret --from-literal=password=...
kubectl get secret app-secret -o jsonpath='{.data.password}' | base64 -d
kubectl create secret docker-registry regcred --docker-server=... --docker-username=... --docker-password=...
kubectl create secret tls demo-tls --cert=tls.crt --key=tls.key
```

## B.10 存储

```bash
kubectl get pv,pvc
kubectl describe pvc data
kubectl get storageclass
kubectl patch pvc data -p '{"spec":{"resources":{"requests":{"storage":"20Gi"}}}}'
```

## B.11 扩缩容

```bash
kubectl autoscale deployment web --min=2 --max=10 --cpu-percent=60
kubectl get hpa
kubectl top pods
kubectl top nodes
```

MiniK8s：

```bash
python -m mini_k8s.cli get hpa
python -m mini_k8s.cli top pods
python -m mini_k8s.cli top nodes
```

## B.12 格式化输出

```bash
kubectl get pods -o json
kubectl get pods -o yaml
kubectl get pods -o name
kubectl get pods -o wide
kubectl get pod web -o jsonpath='{.status.podIP}'
kubectl get pods --sort-by=.metadata.creationTimestamp
kubectl get pods -o custom-columns=NAME:.metadata.name,PHASE:.status.phase
```

MiniK8s 支持 table/wide/json/yaml/name。

## B.13 多文档与 dry-run

```bash
kubectl apply -f multi.yaml
kubectl apply --dry-run=client -f app.yaml
kubectl apply --dry-run=server -f app.yaml
kubectl diff -f app.yaml
```

## B.14 常见排障命令组合

```bash
# Pod 为什么 Pending
kubectl describe pod <pod> | sed -n '/Events/,$p'

# 为什么重启
kubectl logs <pod> --previous
kubectl get pod <pod> -o jsonpath='{.status.containerStatuses[*].lastState}'

# Service 为什么不通
kubectl get endpoints <svc> -o yaml
kubectl get endpointslice -l kubernetes.io/service-name=<svc>

# 节点为什么 NotReady
kubectl describe node <node>
ssh <node> 'systemctl status kubelet'

# 谁有权限
kubectl auth can-i --list
```

## B.15 kubectl 插件

```bash
kubectl krew install ctx ns neat
kubectl ctx
kubectl ns
kubectl neat get pod web
```

插件提高效率，但生产脚本不应依赖非标准插件。

## B.16 MiniK8s 命令覆盖

`mk8s` 是 `kubectl` 的教学子集，语法大体一致，但**资源/名称的分隔方式**不同：

| kubectl | mk8s | 差异 |
|---|---|---|
| `kubectl get pods` | `mk8s get pods` | 一致 |
| `kubectl describe pod web` | `mk8s describe pod web` | 一致 |
| `kubectl logs web` | `mk8s logs web` | 一致；支持 `--previous`、`--tail` |
| `kubectl exec web -- echo hi` | `mk8s exec web -- echo hi` | **模拟执行**：返回固定 stdout/exitCode，不运行真实命令 |
| `kubectl apply --server-side --field-manager=team-a -f app.yaml` | `mk8s apply --server-side --field-manager=team-a -f app.yaml` | 一致；支持 `--force-conflicts`、managedFields、409 冲突 |
| `kubectl set image deployment/web c=img` | `mk8s set image deployment/web c=img` | 一致 |
| `kubectl scale deployment web --replicas=3` | `mk8s scale deployment web --replicas=3` | 一致 |
| `kubectl rollout status deployment/web` | `mk8s rollout status deployment web` | **mk8s 用两个位置参数** |
| `kubectl rollout undo deployment/web` | `mk8s rollout undo deployment web` | 同上 |
| `kubectl run web --image=nginx` | `mk8s run web --image=nginx` | 一致 |
| `kubectl expose deployment web --port=80` | `mk8s expose web --port=80` | mk8s 只接受 Deployment 名 |
| `kubectl cordon/uncordon/drain/taint node` | 同名 | 一致；drain 支持 `--force`、`--ignore-daemonsets` |
| `kubectl auth can-i get pods` | `mk8s auth can-i get pods` | 一致 |
| `kubectl api-resources` | `mk8s api-resources` | 一致 |
| `kubectl explain deployment` | `mk8s explain deployment` | 简化字段说明 |
| `kubectl top pods/nodes` | `mk8s top pods/nodes` | 来自 MetricsController |
| `kubectl wait --for=condition=Ready pod/web` | `mk8s wait --for=condition=Ready pod web` | 两个位置参数 |

不支持：`port-forward`、`proxy`、`cp`、`edit`、`patch`、`label`、`annotate`、`config`、目录递归 apply（以实际代码为准）。这些差距在第 40 章有对照。

MiniK8s 的“模拟语义”要提前知道：容器不会真的启动，`logs` 是生成的文本，`exec echo/env/ls` 返回模拟结果（其中 `env` 会解析 ConfigMap/Secret/fieldRef），`top` 来自 MetricsController 的确定性用量。用它验证 API/YAML/控制器逻辑，不要用它验证应用行为。

## 延伸阅读

- kubectl Cheat Sheet：`https://kubernetes.io/docs/reference/kubectl/cheatsheet/`
- [第 5 章 声明式工作流](../chapters/05-declarative-workflow.md)
- [附录 C 故障排查清单](C-troubleshooting-checklist.md)
