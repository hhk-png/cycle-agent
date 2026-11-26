# 第 10 章 Artifact Store：路径安全、原子写与生命周期

## 学习目标

- 理解 artifact URI、run 命名空间和本地目录布局。
- 实现路径穿越防护，并知道 Windows 上的额外陷阱。
- 理解原子写、下载语义和内容寻址/去重设计。
- 能规划 artifact 的生命周期、GC 和跨云迁移。

## 10.1 命名空间

Mini 的本地布局：

```text
<artifact_root>/
  <experiment_id>/
    <run_id>/
      artifacts/
        model/
          MLmodel
          model.json
        eval/
          report.json
        notes.md
```

run 的 `artifact_uri`：

```text
file:///.../mlartifacts/1/4f2c.../artifacts
```

真实 MLflow 类似，但可以换成：

```text
s3://bucket/mlflow/1/4f2c.../artifacts
gs://bucket/...
abfss://...
```

URI 是**根**，不是单个文件。`log_artifact` 的 `artifact_path` 是根下的相对前缀。

## 10.2 API 语义

| 操作 | 行为 | 注意 |
|---|---|---|
| `log_artifact(local_file, artifact_path)` | 复制单个文件到前缀下，保留文件名 | 源必须是文件 |
| `log_artifacts(local_dir, artifact_path)` | 递归复制目录内容 | 保持相对结构，不复制源目录名 |
| `log_text/log_dict` | 便捷写文本/JSON | 覆盖已有文件（Mini） |
| `list_artifacts(path)` | 列目录或返回单文件信息 | 路径相对 run 根 |
| `download_artifacts(path, dst)` | 文件复制或目录递归下载 | 目标目录与源隔离 |
| `read_bytes` | 直接读字节 | API 内部使用 |

真实 MLflow 的 `log_artifacts` 语义容易踩坑：

- `log_artifacts("/data/run1", artifact_path="data")` 会把 `run1` 的**内容**放到 `data/` 下，而不是 `data/run1/`；
- 如果 `artifact_path=None`，内容直接放到 run 根。

Mini 遵循这个语义。

## 10.3 路径穿越防护

不可信输入包括：

```text
../../etc/passwd
/absolute/path
C:\Windows\System32
\\server\share\file
a/../../b
file\x00.txt
```

`safe_join(root, relative)` 的检查顺序：

1. 拒绝 NUL；
2. 统一分隔符；
3. 拒绝绝对路径和盘符；
4. 拒绝任一 `..` 段；
5. `resolve(strict=False)` 后检查是否仍在 root 下（防 symlink 逃逸）。

为什么第 5 步不能省？因为 `root/link/../../outside` 在词法上可能通过，但解析 symlink 后逃逸。`resolve` 后做 `relative_to(root)` 是最终防线。

Mini 的实现：

```python
root_path = Path(root).resolve()
candidate = (root_path / Path(*pure.parts)).resolve(strict=False)
try:
    candidate.relative_to(root_path)
except ValueError:
    raise InvalidParameter("artifact path escapes the artifact root")
```

### Windows 细节

- `Path("C:/x").is_absolute()` 与 POSIX 行为不同；
- `file:///C:/x` 的 `urlparse().path` 是 `/C:/x`，需要去掉前导斜杠；
- `..\\` 可能被某些层当作普通字符，统一替换为 `/`；
- 大小写不敏感文件系统可能导致 `Root` 与 `root` 的比较问题，`Path.resolve()` 在 Windows 上会规范化。

Mini 的测试覆盖绝对路径、盘符、`..`、NUL 和正常路径。

### 下载目录也要防

```python
download_artifacts(run_id, "model", "/tmp/out")
```

如果直接把 `artifact_path` 拼到目标目录，`../` 可能写到目标外。Mini 先把源路径限制在 run 根，再只用**文件基名/相对结构**构造目标路径。

## 10.4 原子写

```python
fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=destination.parent)
# 写入、flush、fsync
os.replace(tmp, destination)
```

原因：

- 崩溃/断电不会留下半个最终文件；
- 并发读者不会看到部分内容；
- 上传失败可重试。

残留的 `.tmp-*` 文件应被 GC 清理。Mini 的目录列表会忽略它们。

注意：原子替换在跨文件系统时可能失败；`os.replace` 要求同一卷。Mini 的临时文件放在目标目录内，满足条件。

## 10.5 大文件与流式传输

Mini 的远端上传协议：

```json
{"run_id":"...", "path":"model/model.json", "content_b64":"..."}
```

问题：

- 全量读入内存；
- base64 膨胀约 33%；
- 请求体上限 64 MiB；
- 无法断点续传。

生产方案：

1. **预签名 URL**：客户端直传 S3/GCS，服务端只接收元数据；
2. **分片/多段上传**：大模型按块上传，失败重传块；
3. **流式 multipart**：服务端不缓冲整个 body；
4. **校验和**：上传前/后计算 SHA-256，服务端校验；
5. **并发限流**：避免一个客户端占满带宽。

真实 MLflow 的 artifact 上传依客户端/存储后端不同，可能走服务器代理或直传。

## 10.6 内容寻址与去重

当前 Mini 的布局是“命名空间树 + 直接复制”。优点是：

- 人类可直接浏览；
- 路径与 URI 直观；
- 不需要额外索引。

缺点是：

- 同一个模型文件在多个 run 中重复存储；
- 删除/GC 需要遍历目录；
- 没有内容完整性校验。

内容寻址存储（CAS）设计：

```text
<root>/.blobs/ab/cd/<sha256>
<root>/<exp>/<run>/artifacts/model/MLmodel  -> 引用 manifest
```

或对象存储中的：

```text
s3://bucket/blobs/ab/cd/<sha256>
s3://bucket/trees/<run_id>/manifest.json
```

优点：去重、校验、GC 可达性分析简单。
缺点：失去人类可读路径，需要 manifest 层；跨租户去重可能泄漏“某个 blob 存在”这一事实。

实现时还要考虑：

- 小文件去重收益低，元数据开销高；
- 大文件分块去重（CDC）复杂度高；
- 加密/合规可能禁止跨租户去重。

## 10.7 生命周期与 GC

一个安全的删除流程：

```text
1. 标记 run deleted（软删除）
2. 等待保留期（例如 30 天）
3. 检查是否被模型版本/外部系统引用
4. 删除 artifact 树或 blob 引用
5. 物理删除元数据
6. 记录审计
```

难点：

- 模型版本只存 source URI，没有外键到 artifact；删除 run 前必须扫描 Registry；
- 外部系统可能直接引用 `runs:/...`；
- 对象存储版本控制/对象锁会影响删除；
- 多租户共享 blob 时要做引用计数。

Mini 的 `delete_run` 只改元数据，不删 artifact；这是安全默认。

## 10.8 后端抽象

一个可扩展的接口应至少包含：

```python
class ArtifactRepository:
    def log_artifact(self, run_uri, local_path, artifact_path): ...
    def log_artifacts(self, run_uri, local_dir, artifact_path): ...
    def list_artifacts(self, run_uri, path): ...
    def read(self, run_uri, path): ...
    def download(self, run_uri, path, dst): ...
    def delete(self, run_uri, path): ...
```

真实实现还需要：

- URI scheme 解析与凭证；
- 分片/多段上传；
- 重试与超时；
- 服务端加密/KMS；
- 访问日志与审计；
- 断点续传；
- 并发与限流。

Mini 只实现 `file://`，并对其他 scheme 明确报错，而不是静默失败。

## 10.9 缓存与 CDN

模型下载可能占部署时间的大部分。优化：

- 在推理节点本地缓存（按 digest/版本）；
- 使用对象存储 CDN；
- 预热常用模型；
- 避免每次启动都拉取完整目录；
- 对模型文件做签名 URL，短期有效。

缓存失效要以“内容/版本”为 key，而不是路径（路径可能被覆盖）。

## 10.10 与真实 MLflow 的差距

- 真实 MLflow 支持多种 artifact repository 与客户端直传；
- 真实上传协议可能是 multipart、预签名或 SDK 直写；
- 真实系统有更完善的 GC 命令和云生命周期集成；
- Mini 的 base64 JSON 只适合小文件和教学。

## 小结

- artifact URI 是命名空间根；所有相对路径必须经过 containment 检查。
- 原子写避免半文件；跨卷替换要特别处理。
- 大文件应使用流式/分片/预签名，而不是 base64 全量缓冲。
- 内容寻址能去重和校验，但会牺牲可读性并引入引用计数问题。
- 删除元数据不等于删除字节；GC 必须考虑 Registry 和外部引用。

## 练习

1. 为 Mini 增加 `sha256` sidecar/manifest，设计读取时的校验流程。
2. 设计一个 S3 ArtifactRepository 的接口与错误映射，不写实现。
3. 如果一个模型版本引用了已删除 run 的 artifact，发布流水线应如何检测？

---

下一章：[第 11 章 Model 封装](11-models.md)
