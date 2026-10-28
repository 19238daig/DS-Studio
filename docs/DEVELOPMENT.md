# 开发与验证

应用代码在 `src/`，Streamlit 入口是 `app.py`，后台入口是 `worker.py`。
Python 依赖与 Docker 分析依赖分别固定在两个 `requirements.lock` 文件。

## 测试

```powershell
python -m pytest -q
```

测试通常构造临时工作区及少量合成数据，不要求仓库携带用户数据。数据库集成测试使用隔离 PostgreSQL 集群；有些环境缺少本机工具时会跳过相应检查。Docker 执行测试需要 Docker 服务及 `ds-analysis:local` 镜像。

两项可选的 Wine Quality 全量数值回归仅在本机 `KB/` 已有相应 CSV 时运行；缺少外部数据时明确跳过，不自动下载或随代码分发。Docker 权限、禁网、只读挂载与结果校验测试使用内嵌合成数据，不依赖这些外部文件。

`scripts/diagnose.py` 默认进行本机环境诊断，不发起模型调用。只有显式添加 `--models`、`--tool-round`、`--thinking` 等参数时才进行相应真实模型诊断；真实调用可能产生费用。`smoke_agents.py` / `smoke_reports.py` 同样属于显式运行的集成验收入口，先查看其参数和配置。

## 发布前检查

1. 检查 `.gitignore`，不要强制添加密钥、数据集、聊天历史或运行产物。
2. `git add` 需要发布的代码与说明后运行 `python scripts/check_publication.py`。
3. 检查 `git diff --cached --stat` 与完整差异，再提交和推送。
4. 修改第三方适配时保留上游许可证及版本/哈希清单；不要将变更后的文件伪称为未修改的上游原件。

发布检查针对暂存区或指定提交中的实际 Git 对象，而不是仅检查磁盘文件。它会识别常见密钥模式、本机 `.env` 中可识别的长秘密值、禁止发布的路径和数据文件。不能据此保证不存在所有类型的私有信息，仍需人工审阅。

本地设计草稿、历史验收 HTML / PDF 和性能试验目录不作为发行物上传。验证结果应注明测试范围与环境；不要把单次报告速度对比写成所有复杂分析均能达到的响应保证。
