# 第 13 章 认证：从 API Key 到 OAuth、OIDC 与工作负载身份

## 13.1 认证解决“你是谁”

认证（Authentication）回答：调用方是谁？

授权（Authorization）回答：它能做什么？

两者经常被混为一谈。一个请求通过认证，不代表有权限；一个请求被拒绝，也不一定是没登录。API 设计必须把两者分开建模。

混淆会带来具体问题：

- 用 401 表示权限不足，客户端会反复刷新令牌；
- 用 403 表示令牌过期，客户端不会重新登录；
- 把 scope 当权限，导致有 scope 就能访问所有对象；
- 认证通过后不再做对象级检查，造成 BOLA。

### 调用方类型

- 人类用户；
- 第三方应用（代表用户）；
- 合作伙伴服务；
- 内部微服务；
- 设备/IoT；
- 定时任务/脚本；
- AI Agent；
- 浏览器前端（公共客户端）；
- 命令行工具；
- 边缘函数/Serverless。

不同调用方需要不同的认证机制。选择认证方式时，先问：

1. 调用方能否安全保存密钥？
2. 是否有用户参与？
3. 令牌需要代表谁？
4. 令牌生命周期多长？
5. 是否需要跨组织信任？
6. 是否需要吊销？
7. 是否有合规要求（如 FAPI）？

## 13.2 API Key

最简单的形式：

```http
Authorization: Bearer sk_live_...
```

或：

```http
X-API-Key: sk_live_...
```

### 优点

- 简单；
- 适合服务器到服务器；
- 易于计量、限流、吊销；
- 对开发者门槛低；
- 不依赖浏览器流程；
- 适合脚本和 CLI。

### 缺点

- 通常代表长期权限；
- 泄露后风险大；
- 难以表达用户委托；
- 细粒度授权弱；
- 前端使用会暴露；
- 轮换需要协调；
- 无法证明“谁在用”，只能证明“持有 key”。

### 设计建议

- 使用高熵随机值（至少 128 位熵），服务端只存哈希；
- 带前缀区分环境与类型：`sk_live_`、`sk_test_`；
- 支持多个 key、按 key 限流、按 key 统计；
- 支持轮换与过期；
- 不在 URL、日志、错误信息中暴露；
- 提供 last-used 信息；
- 对敏感操作要求额外认证；
- 支持 scope 限制；
- 支持 IP 白名单（谨慎使用）；
- 支持一键吊销；
- 泄露检测（如公开仓库扫描）。

API Key 不是“低级的 OAuth”，它在服务器到服务器场景中仍然非常合理。

### API Key 的存储

服务端不应明文存储 key：

```text
key = "sk_live_" + random(32 bytes)
stored = sha256(key)  # 或 HMAC
```

验证时对传入 key 做同样哈希，再比对。这样即使数据库泄露，攻击者也无法直接使用 key。

### API Key 的轮换

```text
1. 创建新 key；
2. 客户端切换到新 key；
3. 观察旧 key 使用量；
4. 旧 key 标记过期；
5. 最终删除。
```

轮换期应足够长，覆盖客户的发布周期。

### API Key 的泄露响应

- 立即吊销；
- 检查使用记录；
- 通知客户；
- 评估影响；
- 如果是公开泄露，主动扫描 GitHub 等平台；
- 复盘存储和传输环节。

## 13.3 OAuth 2.0 / 2.1

OAuth 是授权委托框架，不是认证协议。它让用户授权第三方应用访问资源，而不共享密码。

### 核心角色

- Resource Owner：用户；
- Client：第三方应用；
- Authorization Server：授权服务器；
- Resource Server：API。

### 常见流程

#### 授权码 + PKCE

适用于有用户参与的 Web/移动/桌面应用：

```text
Client → /authorize?response_type=code&client_id=...&code_challenge=...
User → 登录并同意
Auth Server → redirect_uri?code=...
Client → /token (code + code_verifier)
Auth Server → access_token + refresh_token
Client → API with Bearer token
```

PKCE 防止授权码拦截攻击。OAuth 2.1 草案将 PKCE 作为所有授权码流程的必需项，并移除隐式流程与密码流程。截至 2026 年末，OAuth 2.1 仍是 IETF 草案（技术内容已高度稳定，但不要声称它已是 RFC）；FAPI 2.0 已发布并在金融场景生产采用。

PKCE 的关键：

- `code_verifier` 是随机字符串；
- `code_challenge = BASE64URL(SHA256(code_verifier))`；
- 授权时发送 challenge；
- 换令牌时发送 verifier；
- 授权服务器验证二者匹配。

#### 客户端凭证

适用于服务间：

```http
POST /token
Content-Type: application/x-www-form-urlencoded

grant_type=client_credentials
&client_id=...
&client_secret=...
&scope=orders:read
```

返回：

```json
{
  "access_token": "...",
  "token_type": "Bearer",
  "expires_in": 3600,
  "scope": "orders:read"
}
```

客户端凭证没有用户身份，只有应用身份。不要用它代表用户。

#### 设备码

适用于电视、CLI、IoT 等无法输入密码的设备：

```text
Device → /device_authorization
User → 在另一设备输入 code
Device → 轮询 /token
```

设备码的关键：

- 用户需要在有浏览器的设备上确认；
- 设备轮询有间隔；
- code 有短有效期；
- 适合输入能力有限的设备。

#### 令牌交换

用于服务间委托、租户切换、权限降级：

```http
POST /token
grant_type=urn:ietf:params:oauth:grant-type:token-exchange
subject_token=...
subject_token_type=urn:ietf:params:oauth:token-type:access_token
```

令牌交换的常见用途：

- 服务 A 用用户令牌换取服务 B 的令牌；
- 多租户应用切换租户；
- 权限降级（只保留必要 scope）；
- 跨信任域联邦。

令牌交换有两种语义，必须在契约中写清楚：

- **委托（delegation）**：服务 A 代表用户调用服务 B，令牌的 `sub` 仍是用户，`act` 记录 A；
- **模拟（impersonation）**：服务 A 以用户身份行事，下游无法区分 A 与用户。除非有强审计与明确授权，否则不应默认使用。

```json
{
  "sub": "user_123",
  "act": { "sub": "service_checkout" },
  "aud": "https://api.warehouse.example.com",
  "scope": "inventory:reserve",
  "exp": 1798761599
}
```

资源服务器要同时校验 `sub`、`act`、`aud` 与 `scope`。只校验签名，等于把“谁代表谁”交给了调用方自证。

#### 元数据与发现

OAuth 生态有一套常被忽略、但对多租户与 Agent 接入非常重要的元数据标准：

| 标准 | 端点/机制 | 解决的问题 |
|---|---|---|
| RFC 8414 | `/.well-known/oauth-authorization-server` | 授权服务器元数据发现 |
| OIDC Discovery | `/.well-known/openid-configuration` | OIDC 端点与能力发现 |
| RFC 9728 | `/.well-known/oauth-protected-resource` | 资源服务器声明所需令牌与授权服务器 |
| RFC 7662 | 令牌内省端点 | 不透明令牌的实时校验 |
| RFC 7009 | 令牌撤销端点 | 主动吊销 access/refresh token |
| RFC 7591 | 动态客户端注册 | 客户端自助注册（高风险） |

授权服务器元数据至少应声明：

```json
{
  "issuer": "https://auth.example.com",
  "authorization_endpoint": "https://auth.example.com/authorize",
  "token_endpoint": "https://auth.example.com/token",
  "jwks_uri": "https://auth.example.com/.well-known/jwks.json",
  "scopes_supported": ["orders:read", "orders:write"],
  "response_types_supported": ["code"],
  "grant_types_supported": ["authorization_code", "client_credentials", "refresh_token"],
  "code_challenge_methods_supported": ["S256"],
  "token_endpoint_auth_methods_supported": ["private_key_jwt", "client_secret_basic"],
  "revocation_endpoint": "https://auth.example.com/revoke",
  "introspection_endpoint": "https://auth.example.com/introspect",
  "pushed_authorization_request_endpoint": "https://auth.example.com/par",
  "require_pushed_authorization_requests": true
}
```

资源服务器（你的 API）可以用 RFC 9728 声明“我接受哪个授权服务器签发的令牌、需要什么 scope”：

```http
HTTP/1.1 401 Unauthorized
WWW-Authenticate: Bearer resource_metadata="https://api.example.com/.well-known/oauth-protected-resource"
```

```json
{
  "resource": "https://api.example.com",
  "authorization_servers": ["https://auth.example.com"],
  "scopes_supported": ["orders:read", "orders:write"],
  "bearer_methods_supported": ["header"]
}
```

这让 Agent、SDK 和网关不必硬编码端点，也降低了令牌被错误 audience 复用的风险。实现时要注意：

- 元数据文档要缓存，但不能永久缓存；轮换 `jwks_uri` 时要能平滑过渡；
- 只信任 HTTPS 且域名与 `issuer` 一致的元数据；
- 不要把客户端提供的任意 URL 当作元数据地址去抓取（SSRF）；
- 元数据是发现机制，不是信任机制；最终仍要校验签名、`iss`、`aud` 与 `exp`。

#### 内省、JWT 与吊销

资源服务器验证令牌有三条路线：

1. **本地校验 JWT**：用 JWKS 验签，校验 `iss`、`aud`、`exp`、`nbf`、算法白名单。延迟低，但吊销不及时。
2. **远程内省**：把令牌发给 RFC 7662 内省端点，得到 `active`、`scope`、`client_id`、`sub`、`exp`。适合不透明令牌，但每次调用都有网络成本。
3. **混合**：普通读请求本地校验；高风险写操作、权限变更后的一段时间、疑似泄露令牌走内省。

内省响应示例：

```json
{
  "active": true,
  "scope": "orders:read",
  "client_id": "cli_123",
  "sub": "user_123",
  "aud": "https://api.example.com",
  "exp": 1798761599,
  "tenant_id": "t_123"
}
```

内省的工程要点：

- 内省端点只接受受信客户端认证，不能公开；
- 内省结果按 `exp` 与短 TTL 缓存，避免每请求一次；
- 不要把令牌放进 URL、日志或追踪属性；
- 内省失败时默认拒绝（fail closed），而不是放行；
- 令牌撤销后，内省缓存要能失效；否则“已吊销”只是文档里的承诺。

#### PAR、JARM 与 FAPI

高安全场景（开放银行、支付、政务）通常要求比“授权码 + PKCE”更严格的流程：

- **PAR（RFC 9126，Pushed Authorization Requests）**：客户端先把授权参数 POST 到 `/par`，拿回短期 `request_uri`，再跳转 `/authorize?client_id=...&request_uri=...`。参数不再经过浏览器地址栏，降低篡改与泄露风险。
- **JARM（JWT Secured Authorization Response Mode）**：授权响应本身用 JWT 签名，防止授权码、state 在返回途中被篡改。它属于 OpenID Foundation 的扩展，采用前要确认授权服务器与客户端库支持。
- **FAPI 2.0**：面向金融级 API 的安全配置，通常组合 PAR、PKCE、sender-constrained token（mTLS 或 DPoP）、严格的 `redirect_uri` 与算法限制，并禁用隐式流程。
- **CIBA（Client Initiated Backchannel Authentication）**：客户端在后端发起认证请求，用户在另一设备上确认，适合呼叫中心、柜面、IoT。它不是“跳过 SCA”，而是把确认放到更合适的通道。

```text
普通 Web 登录：  /authorize → 用户代理 → 回调
PAR：            /par → request_uri → /authorize → 回调
CIBA：           /backchannel-authentication → auth_req_id → 轮询/ping → 用户另一设备确认
```

#### RAR：把授权范围写成结构化对象

Scope 是字符串，表达力有限。RAR（RFC 9396，Rich Authorization Requests）允许客户端在授权请求中携带结构化的 `authorization_details`：

```json
{
  "type": "payment",
  "amount": 12900,
  "currency": "CNY",
  "creditor": "merchant_123",
  "purpose": "order_ord_123",
  "max_uses": 1
}
```

授权服务器可以把这些细节写进令牌，资源服务器逐项校验。它特别适合：

- Agent 代表用户支付，需要限制金额、商户与次数；
- 开放银行的数据访问，需要限定账户、用途与期限；
- 企业采购，需要把预算与审批单绑定到令牌。

RAR 不是“更长的 scope”。它要求授权服务器、令牌格式与资源服务器都理解这些结构化字段，否则只是把 JSON 放进了无人校验的地方。

### OAuth 设计要点

- 使用 `state` 防 CSRF；
- 使用 PKCE；
- 严格校验 `redirect_uri`（精确匹配，不做通配）；
- 最小化 scope；
- access token 短期有效；
- refresh token 轮换与吊销；
- 使用 `audience` 限制令牌用途；
- 用 RFC 8707 `resource` 参数把令牌绑定到具体 API；
- 对外提供 RFC 8414 / RFC 9728 元数据，不要求客户端硬编码端点；
- 高风险场景使用 PAR，必要时启用 JARM；
- 需要金额、账户、用途等细粒度授权时使用 RAR，而不是继续堆 scope；
- 不透明令牌使用内省，JWT 使用本地校验与短 TTL，并在两者之间做缓存与降级设计；
- 明确委托与模拟的语义，记录 `act` 链；
- 不要在前端保存 client_secret；
- 不要用 OAuth 代替认证（需要 OIDC）；
- 令牌不要放 URL；
- 记录令牌签发与使用审计。

### 常见 OAuth 攻击

| 攻击 | 防护 |
|---|---|
| 授权码拦截 | PKCE |
| CSRF | `state` |
| 开放重定向 | 严格 `redirect_uri` |
| 令牌泄露 | 短期令牌、DPoP、绑定 |
| 混淆代理 | `audience`、最小 scope、逐请求授权 |
| 客户端冒充 | 客户端认证、mTLS |
| 刷新令牌被盗 | 轮换、绑定、吊销 |

## 13.4 OIDC：在 OAuth 上做认证

OpenID Connect 在 OAuth 2.0 上增加身份层：

- ID Token（JWT）；
- UserInfo 端点；
- Discovery（`.well-known/openid-configuration`）；
- 标准 claims（`sub`、`iss`、`aud`、`exp`、`email` 等）。

ID Token 示例：

```json
{
  "iss": "https://auth.example.com",
  "sub": "user_123",
  "aud": "client_abc",
  "exp": 1798761599,
  "iat": 1798757999,
  "email": "alice@example.com",
  "email_verified": true
}
```

校验要点：

- 签名；
- `iss` 与 `aud`；
- `exp`/`nbf`；
- 算法白名单；
- 不接受 `alg: none`；
- 不信任未验证的邮箱；
- 使用 JWKS 缓存与轮换；
- 校验 `nonce`（如果使用）；
- 校验 `azp`（多受众时）。

### ID Token 不是 Access Token

- ID Token 用于证明“用户是谁”，给客户端看；
- Access Token 用于访问 API，给资源服务器看；
- 不要把 ID Token 当 Access Token 发给 API；
- 不要把 Access Token 当 ID Token 解析用户信息。

### UserInfo 端点

- 需要 Access Token；
- 返回用户 claims；
- 可能比 ID Token 更完整；
- 需要 scope（如 `profile`、`email`）；
- 有速率限制。

## 13.5 JWT 的边界

JWT 适合：

- 无状态令牌；
- 跨服务传递身份与声明；
- 短期访问令牌；
- 需要防篡改的声明。

JWT 不适合：

- 存储敏感数据（可被解码）；
- 长期会话；
- 需要立即吊销的场景（无状态校验无法即时失效）；
- 作为加密容器（JWE 另说）；
- 存储大量数据（令牌会变大）；
- 作为数据库替代。

### 常见错误

- 把用户密码、身份证号放 JWT；
- 不校验 `aud`，导致令牌可用于其他服务；
- 不校验算法，接受 `none`；
- 使用对称密钥但分发到所有服务；
- 过期时间过长；
- 把 JWT 放在 URL；
- 在 localStorage 存储长期令牌（XSS 风险）；
- 不校验 `iss`；
- 信任 `email` 但未校验 `email_verified`；
- 把 JWT 当会话存储，塞入大量权限。

### JWT 的吊销

无状态 JWT 的吊销很难：

- 短期令牌 + 刷新令牌；
- 令牌版本号（用户表记录 `token_version`）；
- 黑名单（需要共享存储）；
- 短 TTL + 事件驱动失效；
- 关键操作重新认证。

不要承诺“JWT 可以即时吊销”，除非你有黑名单或版本机制。

### JWE

JWE 提供加密，不只是签名：

```text
JWS: 签名，内容可读
JWE: 加密，内容不可读
```

如果令牌包含敏感信息，使用 JWE 或不要把敏感信息放令牌。

## 13.6 mTLS 与工作负载身份

服务间认证常用：

- **mTLS**：双向证书认证；
- **SPIFFE/SPIRE**：工作负载身份；
- **云 IAM**：AWS IAM Roles、GCP Service Account、Azure Managed Identity；
- **Kubernetes ServiceAccount**；
- **OIDC 联邦**：CI/CD 获取短期云凭证。

mTLS 优点：

- 强身份；
- 传输加密；
- 适合零信任网络；
- 不依赖共享密钥；
- 证书可短命、自动轮换。

缺点：

- 证书管理复杂；
- 轮换与吊销；
- 调试困难；
- 对开发者不友好；
- 需要 PKI；
- 证书过期会导致大面积故障。

### SPIFFE

SPIFFE 定义工作负载身份：

```text
spiffe://example.com/ns/payments/sa/checkout
```

SPIRE 负责签发和轮换证书。适合：

- 多集群；
- 多云；
- 服务网格；
- 零信任。

### 工作负载身份的最佳实践

- 证书短命（小时级）；
- 自动轮换；
- 身份与 workload 绑定，不与环境变量共享；
- 最小权限；
- 审计签发；
- 监控证书过期；
- 避免把长期密钥写入镜像。

## 13.7 DPoP 与令牌绑定

Bearer token 的风险是“谁拿到谁能用”。DPoP（Demonstrating Proof of Possession）把令牌绑定到客户端密钥：

```http
Authorization: DPoP <token>
DPoP: <proof-jwt>
```

即使令牌泄露，没有私钥也无法使用。适合高安全场景、公共客户端和金融 API。

DPoP 的关键：

- 客户端生成密钥对；
- 令牌绑定公钥指纹（`cnf.jkt`）；
- 每次请求签名；
- 服务端验证 proof；
- 防止重放（`jti`、`htu`、`htm`、`iat`）；
- 把 proof 绑定到具体 access token：`ath = base64url(SHA256(access_token))`。缺少 `ath` 校验时，攻击者可以把同一 proof 挪用到另一个令牌上。

### DPoP 校验清单与失败模式

资源服务器至少应校验：

1. `Authorization` 方案是 `DPoP`，不是 `Bearer`；
2. proof 的签名算法在允许列表内，且与 `jwk` 匹配；
3. `jwk` 指纹等于令牌中的 `cnf.jkt`；
4. `ath` 等于 `base64url(SHA256(access_token))`；
5. `htu` 与请求 URL 匹配（忽略查询参数的规范化要一致）；
6. `htm` 与请求方法匹配；
7. `iat` 在允许的时钟偏差内（如 ±60 秒）；
8. `jti` 未在重放缓存中出现，且缓存 TTL 覆盖 proof 有效期；
9. `aud`/`iss` 等令牌声明符合预期。

常见失败：

```http
HTTP/1.1 401 Unauthorized
WWW-Authenticate: DPoP error="invalid_dpop_proof", error_description="ath mismatch"
```

不要只校验签名就放行；DPoP 的安全来自“令牌 + 请求 + 密钥”三者绑定。

### 其他令牌绑定

- **mTLS 绑定**：证书绑定令牌；
- **Token Binding**（较少使用）；
- **Sender-constrained tokens**：OAuth 的统称。

## 13.8 Passkeys 与无密码认证

Passkeys 基于 WebAuthn/FIDO2：

- 公钥认证；
- 抗钓鱼；
- 无需共享密码；
- 支持生物识别；
- 可同步到设备生态。

API 设计中，Passkeys 通常用于用户登录，签发短期会话或令牌，而不是直接替代服务间认证。

### Passkeys 的服务端设计

- 注册：存储公钥、credential ID、签名计数器；
- 登录：生成 challenge，验证签名；
- 恢复：备用设备/恢复码；
- 多设备：同步或逐个注册；
- 账户恢复：防止被锁死；
- 审计：记录认证事件。

## 13.9 前端与公共客户端

浏览器和移动端是公共客户端，不能安全保存密钥。

推荐：

- 授权码 + PKCE；
- BFF 模式：前端只与自己的后端会话交互，后端持有令牌；
- 短期 access token + 安全存储的 refresh token；
- HttpOnly、Secure、SameSite Cookie；
- CSRF 防护；
- 不在 URL 放令牌；
- 不在 localStorage 存长期令牌；
- 使用 CSP 减少 XSS；
- 令牌泄露检测。

### BFF 模式

```text
Browser → BFF (session cookie) → API (access token)
```

优点：

- 令牌不暴露给浏览器；
- 可以用 HttpOnly cookie；
- 容易做 CSRF 防护；
- 容易做令牌刷新；
- 适合多 API 聚合。

缺点：

- 多一层部署；
- 需要维护会话；
- 可能成为瓶颈；
- 需要处理 cookie 跨域。

### SPA 的令牌存储

- 内存：刷新页面丢失，但 XSS 难偷；
- sessionStorage：刷新保留，XSS 可偷；
- localStorage：持久，XSS 可偷；
- HttpOnly cookie：XSS 偷不到，但需要 CSRF 防护。

没有完美方案。BFF + HttpOnly cookie 通常是较安全的选择。

## 13.10 AI Agent 的认证

Agent 可能：

- 代表用户调用 API；
- 以自身身份调用；
- 在多个服务间传递权限；
- 动态获取工具；
- 长时间运行；
- 调用多个下游。

设计要点：

- 使用 OAuth 2.1 + PKCE 或客户端凭证；
- 令牌绑定用户与租户；
- 最小 scope；
- 短期令牌；
- 高风险操作需要用户确认；
- 记录 Agent ID 与委托链；
- 防止 confused deputy（混淆代理）；
- 工具调用逐次授权；
- 支持撤销委托；
- 审计完整轨迹。

### 委托链

```text
user:alice
  → agent:sales-assistant
    → tool:crm
      → resource:customer/cus_123
```

每个环节都应记录：

- 谁委托谁；
- 委托范围；
- 有效期；
- 是否可转委托；
- 撤销状态。

### Agent 令牌的设计

```json
{
  "sub": "user_123",
  "act": { "sub": "agent_sales_assistant" },
  "tenant_id": "t_123",
  "scope": "customers:read orders:read",
  "exp": 1798761599
}
```

`sub` 是被代表的用户，`act` 是代表 `sub` 行动的 Agent（actor）。资源服务器应同时校验 `sub`、`act` 与委托链，不能只看其中一项。

## 13.11 认证设计检查清单

1. 调用方类型是否明确？
2. 是否区分认证与授权？
3. 令牌是否短期、可轮换、可吊销？
4. 是否校验 `iss`、`aud`、`exp`、签名算法？
5. 是否使用 PKCE？
6. 是否避免在 URL/日志中泄露凭证？
7. 是否为不同环境使用不同密钥？
8. 是否有密钥轮换与泄露响应流程？
9. 是否支持多 key、按 key 限流与审计？
10. 是否对高风险操作要求二次认证？
11. 公共客户端是否使用 BFF 或安全存储？
12. 服务间是否使用工作负载身份而非共享密钥？
13. Agent 是否有委托链和最小权限？
14. 是否有令牌泄露检测？
15. 是否有认证事件审计？
16. 是否提供授权服务器与资源服务器元数据，支持端点轮换？
17. 高价值交易是否使用 PAR、DPoP/mTLS 与 RAR 等更强的约束？
18. 委托链中的 `sub`、`act`、`aud`、`scope` 是否逐跳校验？

## 13.12 本章小结

- 认证回答“你是谁”，授权回答“你能做什么”。
- API Key 适合服务间简单场景，但要高熵、可轮换、可审计。
- OAuth 是授权委托框架，OIDC 在其上提供身份。
- PKCE 是公共客户端的默认要求。
- JWT 不是加密容器，必须严格校验声明。
- JWT 的即时吊销需要额外机制。
- mTLS 与工作负载身份适合零信任内部网络。
- DPoP 把令牌绑定到客户端密钥。
- 公共客户端优先 BFF + HttpOnly cookie。
- Agent 认证需要委托链、最小权限与用户确认。
- JWT/JOSE 的严格校验、DPoP/mTLS 绑定、密钥轮换与后量子迁移，见[附录 AD](../appendices/ad-cryptography-pqc.md)。

## 设计练习

为一个“开放银行 API”设计认证：

- 第三方应用代表用户访问账户；
- 银行内部服务互调；
- 移动端；
- 高风险转账；
- 监管审计。

选择每种调用方的认证机制，并说明令牌生命周期、吊销和审计方案。然后回答：

1. 第三方应用如何证明它代表用户，而不是自己？
2. 如果用户的手机丢失，如何吊销访问？
3. 高风险转账如何要求二次认证？
4. 监管要求“可追溯”，需要记录哪些字段？

---

上一章：[异步任务与长时操作](../part-1-foundations/12-async-and-long-running.md)  
下一章：[授权：RBAC、ABAC、ReBAC 与策略即代码](14-authorization.md)
