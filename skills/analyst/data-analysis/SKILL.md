---
name: data-analysis
description: Answer the user's actual data question with verified file or PostgreSQL results; use a minimal scalar fast path or a scoped multi-step analysis for reports.
---

# 数据分析：从问题到可验证结论

项目适配版，参考 Anthropic 官方 analyze 与 sql-queries 技能。上游原件、Apache-2.0 许可证和固定版本见项目 vendor/skills/anthropic-data 与 skills/manifest.json。本文件仅提供方法，不增加工具或数据权限。

1. 先识别用户真正需要的指标、实体、时间范围和交付格式。**单指标快答**只计算该指标：例如平均 pH 只算 mean，不额外算 count、median、std、图表或全量探索；普通文件问答使用 answer_metric，数据库使用单个精确聚合查询。若有report_revision_plan则只补supplement_request目标，用aggregate/run_sql/run_python登记交回报告修订，不用会结束问答的answer_metric。
2. 复杂问题按需核对已授权文件字段或 database_catalog，先确认真实列名再使用。Pandas merge 后检查 columns 与关联键；不要猜 customer_id_sale 这类不存在的后缀。必要检查用于保证正确性，不作为额外指标铺给用户。
3. SQL 仅通过分析 Agent 的 run_sql/run_sql_batch；真实表显式 olist.table，参数采用 $1、$2 与独立 params 数组。只读权限、所选来源和表范围是硬边界。相关报告指标与分组结果尽量同一 run_sql_batch，保留一致性快照。先分别聚合一对多子表再关联，避免金额重复累计。
4. 仅计算当前问题必需的指标，不把比较、排名、集中度、关系检验或全文主题挖掘当成每题必做清单。普通概览优先最小充分指标；明确新增维度再扩展。已有结果整理成报告不新增统计。保存metrics与逐项metric_definitions，预览不能代替完整统计，也不能从预览心算比例/排名；保留完整精度，仅显示时格式化。
5. 只有需要复杂计算或图表时才通过 python_inputs/run_python 在 Docker 内执行，实际写 result.json。先持久保存指标，再生成必要图表；没有成功登记的结果不能声称完成。SQL 快照从 /previous_output 读取，不伪造 file_id。
6. findings 必须直接回答问题，先说明比较结果，再说明边界。区分相关、描述性差异与因果；未执行检验时不写“统计显著”。计数/覆盖率等质量记录不要代替业务结论。每个定量结论须有已实际计算的对应指标，未知内容明确不足而不编造。
7. 报告请求交回主 Agent，由报告 Agent 使用已验证分析生成正式 HTML/Excel。不要把 CSV 明细直接贴入报告，不为无关维度扩大分析范围。
8. 修改已有报告不是重新做全报告。保留report_revision_plan的base和targets；只生成需替换/新增的图表、表格及其直接依赖指标，返回准确artifact_id和analysis_id。已有完整快照可满足就复用，不无故刷新SQL；改变统计口径只查询必要范围。报告专家用revision_patch接入目标，其余图表、段落、布局与历史版本保留。
