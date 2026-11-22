# 第 11 章 配置与密钥：ConfigMap 与 Secret

## 11.1 配置外置的原则

12-Factor App 的核心原则之一：配置与代码分离。Kubernetes 提供两类对象：

- **ConfigMap**：非敏感配置。
- **Secret**：敏感数据（密码、token、证书）。

两者结构相似，但安全语义不同：Secret 默认只是 base64 编码，不是加密。真正的保护来自加密存储、RBAC、准入和外部密钥管理。

## 11.2 ConfigMap

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: app-config
  namespace: default
data:
  APP_MODE: production
  LOG_LEVEL: info
  config.yaml: |
    server:
      port: 8080
    features:
      new_ui: true
binaryData:
  logo.png: iVBORw0KGgo...
```

使用方式：

### 环境变量

```yaml
env:
  - name: APP_MODE
    valueFrom:
      configMapKeyRef:
        name: app-config
        key: APP_MODE
envFrom:
  - configMapRef:
      name: app-config
```

### 卷

```yaml
volumes:
  - name: config
    configMap:
      name: app-config
      items:
        - key: config.yaml
          path: config.yaml
containers:
  - name: app
    volumeMounts:
      - name: config
        mountPath: /etc/app
        readOnly: true
```

卷方式会把每个 key 写成文件；更新 ConfigMap 后，挂载文件会在一段时间后更新（kubelet 同步周期，通常 1 分钟左右）。

环境变量不会自动更新：容器启动时读取一次，之后修改 ConfigMap 不会影响已运行的进程。要生效必须重启 Pod（或应用自己监听文件）。

## 11.3 Secret

```yaml
apiVersion: v1
kind: Secret
metadata:
  name: app-secret
type: Opaque
stringData:
  username: admin
  password: "s3cr3t"
```

`stringData` 让用户写明文，API Server 会合并到 `data` 并做 base64 编码。读取时：

```bash
kubectl get secret app-secret -o jsonpath='{.data.password}' | base64 -d
```

### 常见类型

- `Opaque`：通用。
- `kubernetes.io/service-account-token`：旧版 SA token Secret（新版本用 TokenRequest）。
- `kubernetes.io/dockerconfigjson`：镜像仓库凭据。
- `kubernetes.io/tls`：`tls.crt`/`tls.key`。
- `kubernetes.io/basic-auth`、`kubernetes.io/ssh-auth`。
- `bootstrap.kubernetes.io/token`：节点引导。

类型会影响校验和 kubelet 的使用方式，例如 TLS Secret 的 key 名必须正确。

### 镜像拉取凭据

```bash
kubectl create secret docker-registry regcred \
  --docker-server=registry.example.com \
  --docker-username=robot \
  --docker-password='...' \
  --docker-email=ops@example.com
```

```yaml
spec:
  imagePullSecrets:
    - name: regcred
```

## 11.4 Secret 并不天然安全

默认情况下，Secret 以 base64 存储在 etcd 中。任何能读 etcd 备份的人都能还原。风险包括：

- etcd 未加密、备份泄露。
- RBAC 过宽：`get secrets` 权限。
- Secret 被挂进 Pod，应用日志打印。
- Git 仓库误提交。
- 多租户共享命名空间。

防护措施：

1. **启用 etcd 加密**（EncryptionConfiguration），至少加密 Secret。
2. **限制 RBAC**：不要让普通开发者读所有 Secret。
3. **外部密钥管理**：Vault、AWS Secrets Manager、GCP Secret Manager、Azure Key Vault，通过 External Secrets Operator/CSI 驱动注入。
4. **短期凭据**：ServiceAccount TokenRequest、IRSA/Workload Identity，而不是长期静态密钥。
5. **准入控制**：禁止明文 Secret、限制镜像仓库、扫描镜像。
6. **审计**：记录谁读取了 Secret。

## 11.5 Secret 的挂载与更新

卷挂载的 Secret 更新后，文件最终会更新；环境变量不会。对证书轮换，应用需要监听文件变化或定期重载。

`subPath` 挂载有一个著名坑：

```yaml
volumeMounts:
  - name: secret
    mountPath: /etc/secret/password
    subPath: password
```

使用 `subPath` 时，Secret 更新**不会**传播到容器内文件。因为 subPath 挂载的是单个文件 inode，不是目录。解决方案：

- 挂载整个目录，而不是 subPath。
- 接受不更新，重启 Pod。
- 应用自己 watch 文件。

## 11.6 immutable

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: app-config
immutable: true
```

不可变对象：

- 不能被修改，只能删除重建。
- 减少 API Server watch 开销（大规模集群有性能收益）。
- 防止误改导致配置漂移。

适合版本化配置：每次发布创建新名字（`app-config-v2`），而不是原地修改。

## 11.7 projected volumes

Projected volume 可以把多个来源投影到同一目录：

```yaml
volumes:
  - name: combined
    projected:
      sources:
        - configMap:
            name: app-config
        - secret:
            name: app-secret
        - serviceAccountToken:
            path: token
            audience: api
            expirationSeconds: 3600
        - downwardAPI:
            items:
              - path: pod-name
                fieldRef:
                  fieldPath: metadata.name
```

用途：

- 同时挂载 ConfigMap、Secret、SA token、Downward API。
- 控制 token 的 audience 和有效期。
- 避免多个卷。

## 11.8 Downward API

把 Pod/容器元数据注入容器：

```yaml
env:
  - name: POD_NAME
    valueFrom:
      fieldRef:
        fieldPath: metadata.name
  - name: CPU_LIMIT
    valueFrom:
      resourceFieldRef:
        containerName: app
        resource: limits.cpu
```

常见字段：

- `metadata.name`、`namespace`、`uid`、`labels`、`annotations`。
- `spec.nodeName`、`status.podIP`。
- 容器资源 limits/requests。

注意：环境变量方式不会更新；卷方式会更新。

## 11.9 配置变更的发布策略

修改 ConfigMap/Secret 后，应用是否生效取决于注入方式：

- 环境变量：不更新，需要重启/滚动发布。
- 卷：文件最终更新，应用需支持热加载。
- `subPath`：不更新。

推荐：

- 配置变更走版本化对象 + 滚动发布，确保可回滚。
- 或者在应用内实现配置热加载 + 校验。
- 使用 checksum annotation 触发滚动：

```yaml
metadata:
  annotations:
    checksum/config: "sha256:..."
```

Helm 常用这种模式。

## 11.10 大小限制

两个限制不要混淆：

- **API Server 限制**：单个 ConfigMap/Secret 的 `data` + `binaryData` 总量不得超过 **1 MiB**，超过直接拒绝创建。
- **etcd 请求限制**：etcd 默认 `--max-request-bytes` 约 1.5 MiB；整个对象（含 metadata、annotations）必须能塞进一次请求。因此实际可用空间略小于 1 MiB。

不要放：

- 大证书链。
- 模型文件。
- 大型 JSON 配置。
- 二进制包。

大文件用对象存储/CSI 卷/Init Container 下载。

## 11.11 MiniK8s 的实现

MiniK8s 的 API Server 存储 ConfigMap/Secret；kubelet 检查卷引用的 ConfigMap/Secret 是否存在：

```python
def _missing_volumes(pod):
    if volume.configMap.name not in store: missing.append(...)
    if volume.secret.secretName not in store: missing.append(...)
```

缺失时 Pod 保持 Pending，并写入 message。测试 `test_missing_configmap_volume_blocks_pod` 验证。

MiniK8s 不实现：

- 加密存储。
- 环境变量注入的实际值解析（只存对象）。
- Secret 轮换。
- External Secrets。

示例：

```bash
python -m mini_k8s.cli apply -f examples/06-config-secret.yaml
python -m mini_k8s.cli get configmaps
python -m mini_k8s.cli get secrets
```

## 11.12 生产清单

- Secret 启用 etcd 加密；备份也要加密。
- RBAC 最小化；区分“读配置”和“读密钥”。
- 使用外部密钥管理 + 短期凭据。
- 不要把 Secret 提交到 Git；使用 SOPS/Sealed Secrets/External Secrets。
- 镜像仓库使用 robot account + 定期轮换。
- 配置版本化，变更可回滚。
- 监控证书过期时间。
- 避免 subPath 挂载需要热更新的 Secret。

## 11.13 etcd 静态加密与 KMS

Secret 的 “base64 不是加密” 已经是常识，但生产落地常停在“开了 EncryptionConfiguration”这一步。完整方案要考虑：

### EncryptionConfiguration

```yaml
apiVersion: apiserver.config.k8s.io/v1
kind: EncryptionConfiguration
resources:
  - resources: ["secrets"]
    providers:
      - kms:
          apiVersion: v2
          name: prod-kms
          endpoint: unix:///var/run/kmsplugin/socket.sock
          timeout: 3s
      - identity: {}   # 读取旧明文数据的兜底
```

API Server 用 `--encryption-provider-config` 加载。写入时用第一个 provider 加密；读取时按顺序尝试所有 provider。`identity` 放在最后可以读取历史明文，但也意味着“未重写的老对象仍未加密”。

### 轮换密钥

1. 新增新密钥 provider 放在第一位，保留旧 provider。
2. 滚动重启 API Server。
3. 用 `kubectl get secrets -A -o json | kubectl replace -f -` 重写所有 Secret（或用脚本/控制器）。
4. 确认没有对象再用旧密钥后，移除旧 provider。

没有第 3 步，旧数据仍然用旧密钥加密；删除旧 provider 会导致这些对象无法读取。

### KMS v2 与 envelope encryption

- KMS provider 不直接加密每个 Secret，而是用 **DEK（数据加密密钥）** 加密对象，再用 KMS 的 KEK 加密 DEK。
- KMS v2 支持密钥轮换、健康检查、请求限流，比 v1 更适合生产。
- KMS 不可用时，API Server 的 Secret 读写会失败；必须把 KMS 当成控制面关键依赖做高可用。
- 云厂商 KMS 通常有请求配额/延迟；大量 Secret 轮换要分批。

### 备份与恢复

- etcd 备份默认包含加密后的 Secret；如果密钥丢失，备份也无法恢复。
- 把 KMS 密钥/配置纳入灾备演练，而不是只备份 etcd。
- 恢复集群时，API Server 必须先能访问同一 KMS/密钥版本，否则 Secret 不可读。

### 外部密钥管理

External Secrets Operator、Secrets Store CSI Driver 可以把 Vault/云 Secrets Manager 的值同步为 Secret 或挂载为卷。它们减少“密钥进 Git”，但：

- 同步控制器本身是高权限组件，要限制其可读的路径/命名空间。
- 同步后的 Secret 仍存在 etcd，静态加密仍必要。
- 轮换/缓存/故障降级要设计，否则外部系统故障会导致 Pod 无法启动。

MiniK8s 不做加密存储；它只保存对象，因此教程中的 Secret 行为对应“未加密 etcd”的基线。生产必须把静态加密、KMS 高可用和轮换流程作为独立工程。

## 本章小结

- ConfigMap/Secret 是配置外置的基本对象，但 Secret 默认不等于加密。
- 环境变量不更新，卷会最终更新，subPath 不更新。
- 配置变更应走可回滚的发布流程，而不是手工编辑生产对象。
- 密钥管理需要加密、RBAC、外部 KMS 和短期凭据共同防护。

## 延伸阅读

- [第 12 章 存储](12-storage.md)
- [第 23 章 认证、授权与 RBAC](23-authn-authz-rbac.md)
- [第 24 章 准入控制与供应链安全](24-admission-supply-chain.md)
- [第 49 章 应用工程](49-application-engineering.md)：配置热更新、Secret 轮换与工作负载身份。
- [附录 H 安全加固基线](../appendices/H-security-baseline.md)
- [附录 P 字段语义、默认值与 YAML 陷阱](../appendices/P-defaults-and-yaml-traps.md)：Secret/ConfigMap 的默认与更新语义。
