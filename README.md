# DS Studio

基于 **React + FastAPI + Deep Agents** 的本地数据分析工作台。通过对话分析 CSV、Excel、静态 HTML 或已授权的 PostgreSQL 数据，生成带交互 SVG 图表的 HTML / Excel 报告，并在后续对话中增量修改报告。

> 仓库只包含代码、配置模板和技能参考。不提供 API 密钥、分析数据集、知识库内容、聊天记录或生成报告。首次运行请自行配置模型服务并上传数据。

## 功能

- **三个 Agent**：主 Agent 负责理解和路由；数据分析 Agent 负责查询与计算；报告 Agent 负责报告和局部修订。两个专家使用独立记忆。
- **文件与知识库**：上传 CSV / Excel / HTML，单文件上限 1 GiB；所有角色可读取明确授权的知识库内容。
- **PostgreSQL**：可选的只读 SQL 分析，支持来源/表范围选择、结果快照、哈希校验及受限身份示例。
- **报告**：HTML 内嵌交互 SVG；Excel 保留图表预览。报告结论引用已登记指标，默认不复制原始数据整表。
- **连续修订**：修改文字、章节、图表、Top N 或口径时保留原版，另存修订版；报告附件跟随对应的助手回复。
- **执行与观测**：生成的 Python 在受限 Docker 中执行；界面显示分派记录、任务状态、响应时间和服务商实际返回的 Token 用量，支持流式进度。
- **上下文优化**：按角色裁剪工具、复用有效分析、按需读取配置、保护完整需求并停止无进展循环。复杂分析仍可能需要较长时间。

架构与边界见 [架构说明](docs/ARCHITECTURE.md)。

## 环境

- Node.js **22.12+**，用于构建 React / TypeScript 前端。
- Python **3.11–3.13**；项目已在 Windows + Python 3.13 环境验证。
- Docker Desktop / Linux Engine，用于生成代码的隔离执行和图表导出。
- 兼容 OpenAI Chat Completions 的模型服务；默认适配阿里云百炼 DashScope。
- 可选 PostgreSQL **18**，仅在启用数据库功能时需要。

依赖版本锁定在 `requirements.lock` 和 `docker/requirements.lock`。不同模型可能对思考模式、工具调用或流式参数有不同要求，替换提供商时请检查 `src/llm.py`。

## 快速开始（Windows PowerShell）

```powershell
git clone https://github.com/19238daig/DS-Studio.git
cd DS-Studio
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.lock
.\.venv\Scripts\python.exe -m pip check
Copy-Item .env.example .env
```

编辑本机 `.env`，填写 `API_KEY`、`BASE_URL` 与模型名称。**不要将 `.env` 提交到 GitHub。** 数据库默认关闭；文件分析不要求数据库连接。

启动 Docker，然后构建沙箱镜像：

```powershell
docker build --pull=false -t ds-analysis:local -f docker/Dockerfile docker
```

构建 React 前端（首次运行或前端代码更新后）：

```powershell
cd frontend
npm ci
npm run build
cd ..
```

启动工作台：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/start.ps1
```

- React 问答界面：<http://127.0.0.1:8000/>（与 API 同源）
- FastAPI 文档：<http://127.0.0.1:8000/docs>
- 健康检查：<http://127.0.0.1:8000/api/v1/health>

停止脚本管理的进程：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/stop.ps1
```

需要查看前台输出时，在两个终端分别启动：

```powershell
.\.venv\Scripts\python.exe -m uvicorn src.api:app --host 127.0.0.1 --port 8000
.\.venv\Scripts\python.exe worker.py
```

macOS / Linux 可使用 `.venv/bin/python` 分别执行上述两个 Python 入口；PowerShell 进程管理脚本主要面向 Windows。完整平台组合尚未全部验证。

前端热更新开发：运行 `powershell -ExecutionPolicy Bypass -File scripts/dev.ps1`，打开 <http://127.0.0.1:5173/>，Vite 将 `/api` 代理到 FastAPI。生产模式不启动 Node 服务。


## 使用

1. 在侧栏新建对话，上传自己的文件，或将可公开使用的材料放到本机 `KB/`。
2. 明确选择本轮的数据来源，然后提出问题，例如“计算销售额平均值，只返回结论”。
3. 需要报告时选择 HTML / Excel 格式并说明主题。报告完成后可在对应回复下预览和下载。
4. 继续说明修改目标，例如“删除建议章节”“将地区排名改为 Top 10”。原文件保留，新版附在新的回复下。

`KB/` 和其他运行目录会在启动/使用时创建。不要将从第三方取得的数据或含个人信息的数据随代码提交。

## 可选 PostgreSQL

在 `.env` 中配置 `DB_ENABLED`、连接地址、独立管理身份和只读查询身份。自行准备 Olist 数据时，将 `DB_DATASET_DIR` 指向本机 CSV 目录；数据集不随仓库提供。

```powershell
.\.venv\Scripts\python.exe scripts/db_manage.py status
# 确认目标库正确后，初始化项目对象和独立只读身份：
.\.venv\Scripts\python.exe scripts/db_manage.py bootstrap
.\.venv\Scripts\python.exe scripts/db_manage.py import
```

导入和迁移会改变所配置数据库，请先阅读 [数据库说明](db/README.md)。Agent 不接收数据库管理密码，也不执行写入 SQL。

## 目录与私有文件

| 路径 | 用途 | 提交到 GitHub |
| --- | --- | --- |
| `frontend/`、`src/`、`templates/` | React 界面、API、Agent、数据与报告实现 | 是 |
| `config/`、`skills/`、`vendor/` | 提示词、项目适配和带许可证的技能参考 | 是 |
| `db/migrations/`、`docker/`、`scripts/`、`tests/` | 迁移、沙箱、工具和自动化测试 | 是 |
| `.env.example` | 无凭据的配置模板 | 是 |
| `.env`、`KB/`、`input/`、`datasets/`、`data/` | 本机密钥和数据集 | **否** |
| `state/`、`memory/`、`report/`、`script_output/`、`.runtime/` | 会话、记忆、报告和中间产物 | **否** |

UI/UX Pro Max 技能目录里的 CSV 是上游公开的设计参考目录，不是用户分析数据。其来源、版本和许可证见 [第三方声明](THIRD_PARTY_NOTICES.md)。

## 开发与安全

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe scripts/diagnose.py
.\.venv\Scripts\python.exe scripts/check_publication.py
```

测试使用临时工作区和合成数据；部分测试需要本机 PostgreSQL 工具或 Docker。真实模型诊断需要显式参数，可能产生服务商费用。前端验证在 `frontend/` 运行 `npm test`、`npm run typecheck` 和 `npm run test:e2e`；E2E 使用隔离 API / Worker，不调用付费模型。详情见 [开发说明](docs/DEVELOPMENT.md)。

当前产品面向单用户本地运行，没有面向公网的完整多租户认证体系。不要直接暴露服务端口；请阅读 [安全说明](SECURITY.md)。模型生成的定性解释仍需人工判断，指标绑定与来源检查不等于证明所有结论正确。

## 许可证

项目原创代码采用 [MIT](LICENSE)。第三方技能保留其 Apache-2.0 / MIT 等原许可证，详见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。外部模型服务和数据集的使用条款需分别遵守。
