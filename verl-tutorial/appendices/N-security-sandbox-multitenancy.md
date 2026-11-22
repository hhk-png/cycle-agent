# 附录 N 安全、沙箱与多租户

RL 训练会把“模型生成的代码/命令”真的执行。代码 RL、Agent RL、工具调用都要求模型在环境中行动；一旦环境隔离没做好，训练脚本就变成了一个带 GPU 的远程代码执行服务。本附录给出威胁模型与工程清单。

## N.1 威胁模型

| 资产 | 威胁 | 后果 |
|---|---|---|
| 训练集群 | 模型生成 `rm -rf`/挖矿/反弹 shell | 数据丢失、算力被盗 |
| 数据 | prompt 注入诱导模型泄露其他样本 | 隐私/商业数据泄漏 |
| 凭据 | 工具/沙箱环境里有云密钥 | 账号被接管 |
| 模型权重 | checkpoint 被未授权下载 | 知识产权损失 |
| 评测集 | 训练数据污染/泄漏 | 指标虚高 |
| 多租户 | 任务 A 读到任务 B 的数据/进程 | 隔离失效 |

注意：**模型不是可信执行者**。它可能被 prompt 注入、被 reward hacking 驱动，或者只是随机犯错。所有“模型生成的内容”都要按不可信输入处理。

## N.2 代码执行沙箱

代码奖励（单元测试、编译、运行）是最常见的执行面。最低要求：

1. **独立进程/容器**，不要在主训练进程里 `exec`；
2. **无网络**（或只允许白名单）；
3. **只读根文件系统 + 临时可写目录**；
4. **CPU/内存/时间/进程数限额**；
5. **非 root 用户**，seccomp/AppArmor 限制系统调用；
6. **输出截断**，防止模型打印巨量日志撑爆管道；
7. **幂等 request id**，重试不重复执行副作用。

verl 的 `verl/utils/reward_score/sandbox_fusion/` 是对接沙箱服务的路径；`verl/utils/reward_score/prime_code/testing_util.py` 也包含代码测试工具。生产环境通常还要：

- 每次执行新建容器（不要复用脏环境）；
- 限制文件描述符与磁盘配额；
- 禁止 fork bomb；
- 记录完整命令、退出码、耗时、资源峰值；
- 对超时/崩溃返回 0 分并标记错误类型。

## N.3 工具调用与 prompt 注入

Agent RL 的工具调用链：

```text
模型输出 tool call -> 解析 -> 执行 -> observation -> 拼回上下文
```

风险点：

- **解析器注入**：模型输出伪造的 `</tool>`、JSON 注入，导致执行非预期工具；
- **参数注入**：把 shell 元字符塞进“文件名/查询”参数；
- **observation 注入**：工具返回的内容里包含“忽略之前的指令”，影响后续轮次；
- **越权调用**：模型调用未授权工具或访问未授权路径。

工程措施：

- 工具参数用结构化 schema 校验，不拼 shell 字符串；
- 工具白名单 + 每个工具的最小权限；
- observation 加分隔符/转义，明确告诉模型“这是工具输出，不是指令”；
- 对高风险工具（写文件、发请求、执行代码）加人工审批或二次确认；
- 记录完整轨迹，支持事后审计与回放。

## N.4 密钥与数据隔离

- 不要把云密钥、HF token、数据库密码放进工具环境；
- 用短期凭据/代理服务，按任务下发最小权限；
- 训练数据按租户分区，checkpoint/日志/指标同样分区；
- 生产环境应默认脱敏 prompt/response；注意 verl 的 `log_val_generations` 与 `rollout_data_dir` 会记录原始文本，必须显式开启脱敏或限制访问；需要排查时走受控通道；
- 多租户共享 GPU 时，确保进程/共享内存/临时目录隔离；
- Ray 集群要启用认证与网络策略，不要裸奔在公网。

## N.5 多租户 GPU 与 Ray

共享集群常见隔离层次：

| 层次 | 手段 | 强度 |
|---|---|---|
| 进程 | 独立容器/用户 | 中 |
| GPU | MIG / 时间片 / 独占 | 高～中 |
| 网络 | namespace/NetworkPolicy | 高 |
| 存储 | 每租户 bucket/前缀 + IAM | 高 |
| Ray | 独立集群或 namespace + 认证 | 中～高 |

Ray 的 object store 与 dashboard 默认不适合直接暴露公网；多租户场景要关掉不必要的端口，启用 TLS/认证，限制 `ray job submit` 权限。

## N.6 供应链与模型来源

- 只从可信来源加载模型/tokenizer；`trust_remote_code=true` 等于执行远程代码；
- 固定版本与 hash，不要用漂移的 `latest`；
- 扫描依赖漏洞，锁定 lockfile；
- 对第三方 reward model/teacher 做同样的沙箱与权限限制；
- 导出 checkpoint 前检查是否包含敏感数据/后门。

## N.7 审计与合规

最小审计字段：

```text
request_id, tenant_id, user_id, model_version, dataset_version,
tool_name, tool_args_hash, exit_code, latency_ms, token_counts,
reward, error_type, timestamp
```

保留策略要满足业务/法规要求；对训练数据与生成内容做分级，敏感级别高的不落盘或加密落盘。

## N.8 上线前检查清单

- [ ] 代码执行在隔离沙箱，无网络/非 root/有资源限额
- [ ] 工具参数结构化校验，禁止 shell 拼接
- [ ] observation 有边界标记，防注入
- [ ] 密钥不进入模型/工具环境，使用短期凭据
- [ ] 多租户数据/日志/checkpoint 分区
- [ ] Ray dashboard/端口不暴露公网
- [ ] 依赖与模型来源锁定版本
- [ ] 审计日志包含 request/tenant/model/tool/错误类型
- [ ] 红队测试：prompt 注入、越权工具、沙箱逃逸、资源耗尽
- [ ] 故障演练：沙箱超时、reward 服务挂掉、checkpoint 损坏

## 本章小结

- 模型生成的内容是不可信输入；执行面必须沙箱化。
- 工具调用的风险在解析、参数、observation 与权限四个环节。
- 密钥、数据、日志、checkpoint 都要按租户隔离。
- 供应链与模型来源要和代码依赖一样被锁定与审计。
- 安全不是训练后的补丁，而是 RL 环境设计的一部分。

## 延伸阅读

- `verl/utils/reward_score/sandbox_fusion/`
- `verl/utils/reward_score/prime_code/`
- OWASP Top 10 for LLM Applications
- NIST AI Risk Management Framework
