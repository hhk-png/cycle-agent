# Configuration 片段

这里的 YAML 是 `kueue-system/kueue-manager-config` ConfigMap 中
`controller_manager_config.yaml` 的**片段**，不是可以独立 `kubectl apply`
的 CR。

使用步骤（详见 `docs/03-install-and-quickstart.md` 3.6.1）：

1. 导出当前配置：
   ```bash
   kubectl -n kueue-system get cm kueue-manager-config \
     -o jsonpath='{.data.controller_manager_config\.yaml}' > kueue-config.yaml
   ```
2. 把需要的片段（`featureGates`、`resources.transformations`、
   `fairSharing` 等）合并进 `kueue-config.yaml`；不要创建第二个
   Configuration 对象。
3. 整体替换 ConfigMap 的键并重启：
   ```bash
   kubectl -n kueue-system create cm kueue-manager-config \
     --from-file=controller_manager_config.yaml=kueue-config.yaml \
     --dry-run=client -o yaml | kubectl apply -f -
   kubectl -n kueue-system rollout restart deploy/kueue-controller-manager
   ```
4. 用 `scripts/check_kueue_config.py` 做静态检查：
   ```bash
   python scripts/check_kueue_config.py kueue-config.yaml
   ```

| 文件 | 内容 |
|---|---|
| `kueue-config.yaml` | 一份完整、可检查的示例 Configuration |
| `resource-transformations.yaml` | 资源变换片段 |
| `fair-sharing-preemption.yaml` | Fair Sharing 抢占策略片段 |
