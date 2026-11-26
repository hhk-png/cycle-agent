# 第 18 章 文件、二进制与大对象传输

## 18.1 文件 API 的特殊性

文件接口与普通 JSON API 有本质差异：

- 体积大；
- 传输时间长；
- 可能中断；
- 需要校验完整性；
- 涉及存储、CDN、病毒扫描；
- 权限模型更复杂；
- 成本高（存储、出口流量）；
- 元数据与内容分离；
- 生命周期长。

把文件当作 base64 塞进 JSON 是最常见的错误之一。它会让体积膨胀约 33%，增加内存占用，破坏流式处理，并让重试代价极高。

### 文件 API 的核心问题

1. 文件如何进入系统？（上传）
2. 文件如何被访问？（下载/流式）
3. 文件如何被验证？（校验和/类型/病毒）
4. 文件如何被授权？（对象级/租户级）
5. 文件如何被处理？（转码/缩略图/OCR）
6. 文件如何被清理？（TTL/归档/合规）
7. 文件如何计费？（存储/流量/处理）

## 18.2 上传模式

### 1. 直接上传到服务端

```http
POST /files
Content-Type: multipart/form-data
```

```text
--boundary
Content-Disposition: form-data; name="file"; filename="report.pdf"
Content-Type: application/pdf

<binary>
--boundary
```

适合：

- 小文件；
- 需要服务端立即处理；
- 简单场景。

缺点：

- 占用应用服务器带宽；
- 大文件超时；
- 难以断点续传；
- 扩展成本高；
- 网关可能有请求体上限。

### 2. 预签名 URL 直传对象存储

流程：

1. 客户端请求上传凭证；
2. 服务端生成短期预签名 URL；
3. 客户端直接上传到 S3/OSS/GCS；
4. 客户端通知服务端完成；
5. 服务端校验并登记。

```http
POST /files/upload-sessions
```

```json
{
  "filename": "report.pdf",
  "contentType": "application/pdf",
  "size": 10485760,
  "checksumSha256": "..."
}
```

响应：

```json
{
  "uploadId": "upl_123",
  "uploadUrl": "https://bucket.s3.../...",
  "expiresAt": "2026-11-26T10:15:00Z",
  "headers": {
    "Content-Type": "application/pdf"
  }
}
```

优点：

- 不经过应用服务器；
- 可扩展；
- 支持大文件；
- 降低带宽成本；
- 可以利用对象存储的分片、加密、版本控制。

注意：

- URL 短期有效；
- 限制 content-type 和大小；
- 校验 checksum；
- 完成后必须回调确认；
- 防止客户端伪造元数据；
- 扫描病毒；
- 限制上传目标路径。

### 上传会话状态机

```text
CREATED → UPLOADING → UPLOADED → SCANNING → READY
              │            │          │
              │            │          └→ REJECTED
              │            └→ EXPIRED
              └→ ABORTED
```

每个状态都应可查询：

```http
GET /files/upload-sessions/upl_123
```

```json
{
  "id": "upl_123",
  "status": "SCANNING",
  "filename": "report.pdf",
  "size": 10485760,
  "createdAt": "2026-11-26T10:00:00Z",
  "expiresAt": "2026-11-26T10:15:00Z"
}
```

### 3. 分片上传

适合大文件与断点续传：

```text
POST /files/upload-sessions
POST /files/upload-sessions/{id}/parts
POST /files/upload-sessions/{id}/complete
DELETE /files/upload-sessions/{id}
```

每个分片：

```http
PUT /files/upload-sessions/upl_123/parts/1
Content-Type: application/octet-stream
Content-Length: 5242880
```

响应包含 `ETag`，完成时提交分片列表。

设计要点：

- 分片大小（通常 5–100 MB）；
- 分片顺序；
- 分片重试；
- 分片校验和；
- 完成时校验所有分片；
- 过期清理；
- 并发分片上传。

### 4. 可恢复上传协议

tus 等协议定义了标准的分片与断点续传语义：

```http
POST /files
Tus-Resumable: 1.0.0
Upload-Length: 104857600
```

```http
PATCH /files/{id}
Upload-Offset: 5242880
Content-Type: application/offset+octet-stream
```

适合浏览器大文件上传、网络不稳定场景。

### 上传模式对比

| 模式 | 大文件 | 断点续传 | 服务端带宽 | 复杂度 |
|---|---|---|---|---|
| 直接上传 | 差 | 否 | 高 | 低 |
| 预签名 | 好 | 部分 | 低 | 中 |
| 分片 | 好 | 是 | 低 | 中高 |
| tus | 好 | 是 | 低 | 中高 |

## 18.3 下载模式

### 直接下载

```http
GET /files/{fileId}
```

返回：

```http
Content-Type: application/pdf
Content-Disposition: attachment; filename="report.pdf"
Content-Length: 10485760
ETag: "sha256:..."
```

### 预签名下载

```json
{
  "downloadUrl": "https://bucket.../...",
  "expiresAt": "2026-11-26T10:15:00Z"
}
```

优点：减轻应用服务器压力，支持 CDN。

### 范围请求

```http
GET /files/{fileId}
Range: bytes=0-1023
```

响应：

```http
HTTP/1.1 206 Partial Content
Content-Range: bytes 0-1023/10485760
```

适合视频拖动、断点续传、大文件分段处理。

### 流式下载

对于动态生成的大文件，使用流式响应，避免全部加载到内存：

```http
Content-Type: application/x-ndjson
Transfer-Encoding: chunked
```

### 下载的权限

- 在签发下载凭证时检查权限，不要只依赖“URL 很难猜”；
- 预签名 URL 一旦签发就是**持有者凭证（bearer capability）**：对象存储或 CDN 不会看到调用方当前的令牌，也无法在每次下载时重新做对象级授权；
- 因此预签名 URL 应尽量短 TTL、一次性、绑定租户前缀与签名的响应头/内容类型，并在服务端记录签发审计；
- 对撤销敏感的文件（如员工离职、权限回收、合同终止），使用**代理下载**（服务端每次校验权限并流式转发）或 CDN 签名 Cookie，而不是长期预签名 URL；
- 预签名 URL 在过期前通常无法单独撤销，除非轮换签名密钥、删除对象或使用短 TTL；把这一点写进安全设计；
- 不要把永久公开 URL 当权限；
- 日志记录下载签发与下载行为；
- 限速与防盗链。

### 防盗链

- Referer 白名单（可伪造）；
- 签名 URL（推荐）；
- 短期 token；
- IP 绑定（谨慎）；
- 用户代理绑定（不可靠）。

## 18.4 完整性校验

- 客户端上传前计算 SHA-256；
- 服务端校验；
- 对象存储 ETag 不一定是 MD5（分片上传时）；
- 下载时提供 checksum；
- 大文件可分片校验；
- 失败返回明确错误。

```json
{
  "checksum": {
    "algorithm": "sha256",
    "value": "9f86d081884c7d659a2feaa0c55ad015..."
  }
}
```

### 校验的时机

- 上传前：客户端计算，服务端记录；
- 上传后：服务端重新计算，比对；
- 分片上传：每片校验 + 整体校验；
- 下载时：提供 checksum，客户端校验；
- 存储层：对象存储可能提供校验。

### 校验失败

- 返回 422 + 明确错误；
- 删除不完整文件；
- 允许重试；
- 记录指标；
- 通知调用方。

## 18.5 内容类型与安全

### 不要信任客户端 Content-Type

客户端可以伪造。服务端应：

- 通过魔数检测真实类型；
- 限制允许的类型；
- 对图片重新编码；
- 对 PDF/Office 文档做安全检查；
- 对上传文件做病毒扫描；
- 隔离存储，禁止直接执行；
- 不要在上传目录提供 Web 访问；
- 使用 `Content-Disposition: attachment`。

### 文件名

- 不使用客户端文件名作为存储路径；
- 生成内部 ID；
- 清理路径穿越字符（`../`）；
- 限制长度与字符集；
- 下载时使用 `Content-Disposition` 提供原始文件名。

### 访问控制

- 文件默认私有；
- 使用短期签名 URL；
- 校验用户/租户权限；
- 防止 IDOR（直接对象引用）；
- CDN 签名；
- 日志记录访问。

### 病毒扫描

- 上传后异步扫描；
- 扫描前不对外提供；
- 扫描失败隔离；
- 扫描引擎更新；
- 大文件分片扫描；
- 扫描超时处理；
- 记录扫描结果。

### 内容审核

- 图片/视频的违规内容；
- 文档的敏感信息；
- 音频的转写与审核；
- 审核失败的处理；
- 人工复核。

## 18.6 图片与媒体

图片上传常见需求：

- 缩略图；
- 格式转换；
- 元数据剥离（EXIF 可能含位置）；
- 压缩；
- 水印；
- 内容审核。

设计：

```http
POST /images
```

```json
{
  "sourceFileId": "file_123",
  "variants": ["thumb", "medium", "large"],
  "format": "webp"
}
```

返回异步任务或变体列表。

视频：

- 转码；
- 自适应码率；
- 字幕；
- 截图；
- 内容审核；
- 存储成本；
- 播放授权。

通常使用异步任务 + 回调。

### 图片处理的安全

- 重新编码，去除恶意负载；
- 限制像素数（防止解压炸弹）；
- 限制尺寸；
- 限制格式；
- 剥离 EXIF；
- 使用沙箱处理；
- 限制并发。

### 视频处理

- 转码是 CPU/GPU 密集；
- 需要异步任务；
- 多码率；
- 分片；
- 加密；
- 播放授权；
- CDN；
- 成本高。

## 18.7 成本与生命周期

- 存储类型（标准、低频、归档）；
- 出口流量；
- CDN 缓存；
- 过期删除；
- 版本保留；
- 合规保留；
- 孤儿文件清理；
- 配额限制；
- 跨区域复制。

文件 API 的设计必须考虑“上传后谁来付存储费”。

### 孤儿文件

常见来源：

- 上传成功但业务记录创建失败；
- 用户取消；
- 临时文件；
- 多版本未清理；
- 软删除未清理。

解决：

- 上传会话 TTL；
- 业务记录与文件关联；
- 定期扫描；
- 标记删除；
- 延迟清理。

### 生命周期策略

```text
临时文件：24 小时
用户文件：账户存续期间
导出文件：7 天
备份：90 天
合规归档：7 年
```

## 18.8 反模式

- base64 塞 JSON；
- 应用服务器中转所有大文件；
- 预签名 URL 永不过期；
- 信任客户端文件名和类型；
- 上传后不同步元数据；
- 没有 checksum；
- 不支持断点续传；
- 下载不带权限校验；
- 公开 bucket；
- 没有生命周期管理；
- 扫描前就对外提供文件；
- 用永久 URL 做访问控制。

## 18.9 本章小结

- 文件应使用流式/直传，而不是 base64。
- 小文件可直接上传，大文件用预签名/分片/可恢复协议。
- 上传会话需要状态机、TTL 和清理。
- 下载支持范围请求与预签名 URL。
- 完整性校验、类型检测、病毒扫描、访问控制缺一不可。
- 图片/视频通常需要异步处理。
- 存储、流量、生命周期是成本核心。
- 文件权限必须逐次检查，不能依赖 URL 保密。
- 大批量导出、签名 URL 的保留期与数据可携带性，见[附录 S](../appendices/s-bulk-import-export.md)。

## 设计练习

为“在线教育视频平台”设计文件 API：

- 教师上传大视频；
- 断点续传；
- 转码为多码率；
- 学生流式播放；
- 防盗链；
- 课程权限；
- 存储成本控制。

写出上传、处理、播放、权限和清理流程。然后回答：

1. 上传中断后如何恢复？
2. 转码失败如何重试？
3. 学生分享播放链接，如何防止未授权访问？
4. 课程下架后，视频文件如何处理？

---

上一章：[实时 API：SSE、WebSocket、WebTransport 与 WebRTC](17-realtime.md)  
下一章：[多租户与数据隔离](19-multi-tenancy.md)
