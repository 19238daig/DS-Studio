# React + FastAPI 实施与验证

本轮将正式前端迁移到 React / TypeScript，保留 FastAPI、Deep Agents、Worker、PostgreSQL、工作区数据和报告版本。设计使用 frontend-design 与 ui-ux-pro-max 的规范：中性浅灰背景、白色内容表面、克制的品牌蓝和清晰的信息层级。

## 运行入口

- 正式工作台：`http://127.0.0.1:8000/`，API 文档为 `/docs`。
- `scripts/start.ps1` 只启动 API 和 Worker，由 FastAPI 提供 `frontend/dist`。生产入口不再运行 Streamlit。
- `scripts/dev.ps1` 启动 Vite 热更新界面（5173），代理到 FastAPI。
- `scripts/stop.ps1` 按进程身份停止启动脚本登记的服务，包括 Windows 虚拟环境启动器的子进程。`-IfIdle` 在有任务执行时保留服务，不取消任务。
- 旧 `app.py` 和迁移前已有修改保留，Streamlit 改为 `legacy-ui` 可选依赖。不会同时启动两套界面写入流程。

## 主要文件

| 文件 | 实现 |
| --- | --- |
| `frontend/src/app/App.tsx`、`App.module.css` | 会话、路由、三栏布局、移动端抽屉、错误恢复和页面导航 |
| `frontend/src/main.tsx`、`styles/index.css` | 主题变量、组件密度、字体层级、焦点和减少动画偏好 |
| `features/conversation/Conversation.tsx` | 独立滚动消息列表、底部输入区、长历史虚拟化、输入法、澄清和幂等提交 |
| `features/datasets/Sources.tsx` | 1 GiB 上传进度与取消、知识库目录树、文件画像、数据库范围与预览 |
| `features/reports/Reports.tsx` | 按回复绑定报告、按需预览、下载、版本选择与局部修订入口 |
| `features/tasks/`、`features/memory/` | 任务状态、来源快照、次级 Token 指标、增量日志与专家记忆 |
| `shared/api/`、`shared/state.ts` | 生成的公开 API 类型、请求错误、按会话隔离的草稿和选择 |
| `src/api.py`、`src/api_contracts.py`、`src/web.py` | 兼容响应 DTO、安全预览、静态资源及受限 SPA 深链接 |
| `src/schemas.py`、`src/services.py` | 可选 `base_report_id` 的交付、版本、文件和数据库授权校验 |
| `scripts/export_openapi.py`、`scripts/web_acceptance.py` | 契约生成与不调用模型的隔离验收环境 |

## 已解决的交互问题

消息区保留一层主要边界，用户问题使用轻背景，助手结论使用正文段落；报告以附件形式跟随自己的任务回复。历史报告仍可查看和下载，选择基础版本后再提出任意修改要求，原版不会被新版本替代。

左栏宽 268px、右栏宽 288px，中栏自适应；在窄屏上将侧栏与检查面板改为抽屉。右侧可以独立滚动，展开运行记录或下次数据后仍能看到内容；消息列表与底部输入区分开，不依靠整个页面滚动。

来源快照与下次选择分别显示；未知 Token 不显示为零，技术指标默认折叠。上传、提问、取消和重试有明确反馈。中文输入法回车不会误提交；网络结果不确定时，相同内容继续复用幂等键。页面切换、刷新和流重连不会再次发起 Agent 请求。

报告用受限 iframe 预览，保留可信 SVG 脚本和下载能力；不将报告 HTML 注入应用 DOM。数据库列预览按后端字段描述对象渲染，空表选择和空来源继续保留明确的清空语义。

## 验证方式与边界

Python 相关回归通过 72 项；最终另补测静态路由、版本指定、安全预览和真实隔离 PostgreSQL 预览共 6 项，其中包含前述范围内的重复检查。前端单元测试通过 8 项，构建和 TypeScript 检查通过。E2E 通过真实 FastAPI、Worker、数据分析和报告校验器运行，使用合成数据与确定性执行器，不调用付费模型。

浏览器使用本机 Edge 无头模式，7 个 E2E 场景全部通过：检查桌面、1024px、768px、390px、深浅主题、自动无障碍规则、独立滚动、会话操作、上传、报告预览/下载/修订、流连接失败和取消；也验证了 1000 条消息的有限 DOM 渲染、输入响应、澄清上下文与清空格式，以及普通/减少动画偏好下的格式菜单定位。沙箱 iframe 内的 SVG 缩放、重置、系列切换和导出均通过真实交互验收。

另以正式服务进行 1 项只读检查，确认已有会话、数据、报告和专家记忆页面可以载入，不创建额外分析任务；本机 PostgreSQL 的普通和受限来源目录也能读取。切换前后的会话、报告和数据索引已核对一致。

构建的初始 JavaScript gzip 约 414KB，低于方案约 500KB 的首屏预算；Ant Design 公共块原始体积约 985KB，构建仍有大块提示。记忆页面单独加载，报告 HTML 只有展开时下载。大块仍可继续拆分，不能据此次本地测试推断所有设备或网络的首屏速度。

本轮没有重新进行付费模型全链路生成，也没有实传 1 GiB 文件或完成多浏览器全矩阵压测。1 GiB 上传配置、流式传输实现和小文件的真实上传路径保留并验证；复杂 SQL / Python / LLM 的耗时仍由后端任务本身决定。自动无障碍检查不能替代完整人工辅助技术评估。

验收命令见 `docs/DEVELOPMENT.md`。本机详细记录在 `.runtime/react-migration-20261030/`；浏览器截图和测试报告在 `frontend/test-results/`、`frontend/playwright-report/`，这些产物不发布到仓库。
