# 上游技能来源

本目录由官方 `skill-installer/scripts/install-skill-from-github.py` 使用 `--dest` 下载至当前项目，保留四份 Anthropic 官方数据技能原件及 Apache-2.0 许可证。固定提交为 `33d3dea41a341b341d4f5da2e8801ae0c89be2b8`。

来源：[Anthropic knowledge-work-plugins/data](https://github.com/anthropics/knowledge-work-plugins/tree/33d3dea41a341b341d4f5da2e8801ae0c89be2b8/data/skills)。目录包含 `analyze`、`sql-queries`、`data-visualization`、`build-dashboard`，具体路径、原文哈希、许可证哈希和安装参数见 [项目技能清单](../../skills/manifest.json)。未执行上游脚本，也未安装 Claude MCP 连接器。

上游原件用于保留来源，不在应用的技能发现目录中。`skills/<role>/` 中的项目适配版对照本项目工具、离线 SVG、数据授权和报告校验契约编写，不能将上游任意 HTML、CDN、假样例数据或其他工具示例直接用于正式报告。适配版均明确标注来源；主 Agent 的 `report-routing` 为本项目自有指导。
