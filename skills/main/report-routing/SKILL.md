---
name: report-routing
description: Coordinate an analytical report by routing verified calculations to the analysis specialist and validated conclusion-first presentation to the report specialist.
---

# 分析报告协调

本项目自有适配指导，配合已安装的 data-analysis、data-visualization 与 analytics-dashboard 技能。

- 理解用户问题后保留原范围与明确来源授权。单指标直接交分析 Agent 精确快答，不扩大统计清单。
- 报告先委派分析 Agent，要求只计算结论必需的指标、分组和图表；使用 data-analysis，必要时使用 data-visualization。SQL 只能由分析 Agent 在既有只读来源/表边界内执行。
- 已有匹配且授权的结果直接复用。数据已充分时委派报告 Agent，要求使用 analytics-dashboard 生成结论优先、默认不附 CSV 明细的正式 HTML/Excel。
- 报告 Agent 只能引用已验证指标/表/图；不足则精确补充分析。不得把别的数据源、单表统计或预览猜测当成用户要求的关系分析。
- 已有报告的任何局部修改都先读取并prepare_report_revision锁定原版与稳定目标；Top、翻译、段落简写、增删章节和更换图表都是同一修订流程。纯文字不重新分析；需要新指标/图表只补目标，并携report_revision_plan回到同一base_report_id用revision_patch接入，不生成默认新报告或改其它部分。全部布局继承，原件与历次版本保留。
- 技能是方法指导，不增加权限。所有来源、数值绑定、SVG安全、任务预算和沙箱约束仍有效；不因技能建立额外通用子 Agent。
