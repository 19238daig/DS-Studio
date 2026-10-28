# DS Project 技能库

活动技能按 Agent 角色隔离，路径固定在代码项目下，与上传数据、KB、记忆和独立任务工作区分离。

| Agent | 活动技能 | 用法 |
|---|---|---|
| 主 Agent | `report-routing` | 报告任务自动载入协调工作流 |
| 数据分析 Agent | `data-analysis` | 自动载入精确分析与单指标快答指导 |
| 数据分析 Agent | `data-visualization` | 图表或复杂报告任务自动载入；其他任务按需读取 |
| 报告 Agent | `analytics-dashboard` | 每次生成/修改报告自动载入结论优先的报告设计指导 |

通过 `create_deep_agent(skills=['/skills/'])` 和原生 `pinned_skills` 状态加载，模型收到真实 SKILL.md 正文；任务事件 `skill_loaded` 记录名称、角色、虚拟路径、SHA256 和激活方式。仅列出技能目录不算已经使用，事件只在原生技能消息已进入模型请求时记录。

上游四份官方技能已固定版本下载到 `vendor/skills/anthropic-data/`，不直接加入活动发现目录。原件、许可证与活动适配版的哈希记录见 [manifest.json](manifest.json)。活动适配保留现有权限、真实数据计算、受控报告渲染、SVG 离线交互与默认隐藏明细，未新增绘图依赖、执行权限或外部连接器。

后续维护时先审核上游来源和许可证，再修改相应角色的活动适配文件、`src/skills.py` 注册项及清单哈希；运行 `python -m pytest -q tests/test_project_skills.py`。不要将用户上传材料或未经审核的上游完整插件放进活动技能目录。技能更新对后续新任务进程生效，已有报告保持原版本。

API 参考：[Deep Agents 官方 Skills 文档](https://docs.langchain.com/oss/python/deepagents/skills)。本次基于项目已安装 `deepagents==0.7.30` 的实际接口验证。
