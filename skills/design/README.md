# 问答工作台的设计技能

这两份技能安装在当前项目，供后续界面开发时直接读取和使用。它们不属于业务数据，也不会混入主助手、分析助手和报告助手的运行时技能目录。

- [frontend-design](frontend-design/SKILL.md)：建立视觉方向、排版、布局与内容层次。
- [UI/UX Pro Max](ui-ux-pro-max/SKILL.md)：检索设计规则、检查可达性、溢出、响应式与交互状态。

来源、固定版本、许可证和文件哈希记录在 [design-manifest.json](../design-manifest.json)。本次实际使用的设计决策见 [workbench-design.md](workbench-design.md)。

后续修改界面时先读取两份 SKILL.md。Pro Max 的本机检索入口为 `skills/design/ui-ux-pro-max/scripts/search.py`，使用项目 `.venv` Python 即可，不需要额外安装依赖。

