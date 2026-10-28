---
name: data-visualization
description: Choose clear charts for verified analysis results and export offline vector SVG with supported Chinese fonts; use only when a chart helps or the user requests one.
---

# 数据可视化：图形服务于结论

项目适配版，参考 Anthropic 官方 data-visualization 技能；上游固定版本、许可证与哈希见 skills/manifest.json。使用本项目已有 Matplotlib/Docker/SVG 流程，不照搬其他库的示例或安装新绘图依赖。

- 按问题选图：时间趋势用折线；类别、排名和分组比较用条形；分布用直方/箱线；两个连续量的关系用散点。多维报告选少量互补图，不为了展示能力给单指标问题加图。
- 先计算、登记结论指标与完整分组结果，再绘图。来源、筛选、日期口径与单位必须一致。比较是否成立由实际计算决定，图形不能替代统计证据或暗示因果。
- Matplotlib 绘图在现有 Docker 工具内执行，读取真实授权输入或完整 SQL 快照；不要从20行预览画出声称代表全部数据的图。结果行过多时显式计算聚合，不能静默采样或删掉不利类别。
- 图标题点明问题与维度，轴和图例使用可理解名称、单位及简洁标签。条形通常从零起；比例、负值与双轴尤其要避免误导。不同单位优先分面，不使用3D装饰。
- 保持中文及中英文混排。项目导出器会用 Noto CJK/系统真实字形检查标题、图例、刻度与注释；不要把中文改英文以规避缺字，不手动指定 Last Resort 字体。
- 导出真正矢量 SVG，配套 PNG 仅供 Excel。用 savefig('/output/chart.svg')；避免 imshow、rasterized=True、外部图片、SVG脚本和外链。交互由正式报告已有的悬浮、系列切换、缩放/平移、导出 SVG 提供，不另嵌 CDN 或任意 JavaScript。
- result.json 的 charts 列出真实存在且带扩展名的相对文件名。图表中的数字只在显示时舍入，已保存指标和结果表保留完整精度。
