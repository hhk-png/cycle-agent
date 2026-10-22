# CI 示例

`github-actions.yml` 是一份可以直接改用的 GitHub Actions 工作流。它做四件事：

1. 在 Python 3.10、3.11、3.12 上安装包并跑 `pytest -q`；
2. 逐个运行 `examples/*.py`，任何一个非零退出码都会失败；
3. 运行 `check_docs.py`，检查章节、链接、代码路径和计数；
4. 构建 wheel 并作为 artifact 上传。

## 为什么放在 `ci/` 而不是 `.github/workflows/`

这个教程目录可能被复制或 vendored 到另一个仓库。如果它自带 `.github/workflows/ci.yml`，复制到新仓库后可能和对方的 CI 冲突，甚至在没有预期的情况下开始运行。把示例放在 `ci/`，由使用方显式复制到 `.github/workflows/`，更安全。

## 怎么用

```bash
mkdir -p .github/workflows
cp ci/github-actions.yml .github/workflows/ci.yml
```

如果教程不在仓库根目录，把 `working-directory` 和 `paths` 改成实际路径。

## 还应该补什么

这个工作流只覆盖“代码和文档一致”。生产项目还应该加：

- 类型检查（mypy/pyright）和 lint（ruff）；
- 依赖漏洞扫描（pip-audit、Trivy）；
- 镜像构建、签名和 SBOM 生成；
- GPU 测试（需要 self-hosted runner 或云 GPU）；
- 性能回归（固定硬件、固定负载、和 baseline 比较）；
- 文档构建（如果教程发布为静态站点）。

这些步骤不一定都放在同一个 job 里；静态检查和单元测试应该快，GPU 和性能测试可以按需触发。
