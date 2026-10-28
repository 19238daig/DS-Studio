---
name: analytics-dashboard
description: Build a conclusion-first analytical report or dashboard from registered results using the project's offline interactive SVG and validated HTML/Excel renderer, without default CSV detail tables.
---

# Analytics Dashboard：结论优先的分析报告

项目适配版，参考 Anthropic 官方 build-dashboard 与 data-visualization 技能。上游原件保留在 vendor/skills/anthropic-data，Apache-2.0 及版本哈希见 skills/manifest.json。上游 Chart.js/CDN、样例数据和直接生成任意 HTML 的做法不适用于本项目；本适配使用已有的受控渲染与离线交互 SVG。

1. 从APP_STATE核对原始需求、已有指标/图表与来源；已有完整账本就不重复read_analyses/read_report。普通“生成报告”仅整理已有分析，不为凑章节增加主题挖掘或无关统计；必要时省略narrative使用可信结论。修改先prepare_report_revision锁定base与目标，确需新统计/图型才request_analysis，不重做整份报告，不覆盖/删除原件。
2. 首屏直接回答用户的问题：比较谁高/低、趋势如何、客户与销售有什么已经验证的关系，并给最关键证据；不要只写“已完成分析”“来源可追溯”或把处理日志当摘要。没有关系指标时 request_analysis 请求针对性补充，不能从预览猜结论。
3. summary、章节文字中的数值必须用 {{metric:analysis_id:metric_key}} 绑定已有指标，不直接抄写数字。也可用 metric_refs 引用。不要添加未经计算的比例、排名、因果或显著性结论；质量问题不应取代业务答案。
4. 按问题组织简洁结论和已有互补图表，避免冗余图和重复指标。引用已有chart_ids；用户明确要求而已有结果无法满足时才request_analysis补图，不为普通排版请求重新计算。报告Agent不执行SQL/Python或自造图数据。
5. 新报告默认 include_tables=false、include_metrics_summary=false。原始 CSV、分组聚合 CSV 都保留为独立产物，不逐行铺进 HTML/Excel 正文。用户明确要求明细时才显式开启。修订继承旧版已保存布局，再执行用户指定的变更。
6. 使用当前工具菜单create_report/apply_report_edit（兼容模式render_report/revise_report），不确定参数用report_options只取所需节schema。现有模板校验指标、来源哈希与交互SVG；不直接写任意HTML、Chart.js、CDN、远程字体或未授权JavaScript。点击详情、缩放/平移和导出由模板提供。
7. 遵守用户指定语言和章节要求。翻译应替换原文，不重复追加；删除固定模块用layout开关，单个自定义章节增删改/移动用revision_patch.sections。保存新版后确认原版保留、未修改部分沿用上一版、结论可核对、图表中文正常；新报告默认无CSV明细与指标汇总，修订继承原版全部布局。
8. 通用修订用revision_patch按component_state稳定ID替换、增加、删除或移动指定图表/表格，并可单独改summary/findings/limitations。Analyst生成的新结果通过supplemental_analysis_ids接入，只能用于补丁明确引用的目标，不能将其额外图/表/发现自动追加。旧bar/Top工具只是快捷方式，不能限制所有报告修改；无法直接改的SVG类型交Analyst只重绘目标，再回到原base应用补丁。计划失败或歧义时说明/追问，不用默认整报告模板冒充完成。
9. 只改一条质量说明时用revision_patch.quality_edits，以上一版quality数组的index定位text或remove；不修改来源标识，也不删除底层分析事实。只删“结论”模块用layout_changes.include_conclusion=false，后续修订继续继承，不自动加回来。
