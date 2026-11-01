# 开发与验证

React / TypeScript 前端在 `frontend/`，FastAPI 和 Agent 在 `src/`，后台入口是 `worker.py`。FastAPI 直接提供 `frontend/dist` 的静态构建。
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

## React 前端

```powershell
cd frontend
npm ci
npm run typecheck
npm test
npm run build
npm run test:e2e
```

`test:e2e` 在 18180 端口启动真实 FastAPI 和 Worker，使用新建隔离工作区、合成 CSV 和确定性执行器，不读取用户数据、调用模型或覆盖既有会话。默认使用本机 Edge 无头浏览器；没有 Edge 时先执行 `npx playwright install chromium`，并去掉 `playwright.config.ts` 的 `channel`。截图、失败 trace、HTML 测试报告保存在 `frontend/test-results/` 和 `frontend/playwright-report/`，均不提交。

开发入口为 `scripts/dev.ps1`，生产入口为 `scripts/start.ps1`，停止均使用 `scripts/stop.ps1`。生产入口只启动 API 和 Worker；Vite 的代理目标从启动脚本传入的 `DS_API_URL` 读取，不向浏览器暴露密钥。开发和生产模式不能同时使用同一工作区的 Worker。

API 类型生成：

```powershell
.\.venv\Scripts\python.exe scripts/export_openapi.py
cd frontend
npm run generate:api
npm run typecheck
```

`openapi.json` 是本地生成文件，不提交；`src/shared/api/generated.ts` 是已锁定的公开契约，修改后需要审阅差异。请求中的来源未提供与明确空数组保留不同语义；流重连只订阅已有任务。报告预览使用独立接口、来源校验与沙箱 iframe，不注入应用 DOM。

React Router 使用实际可安装的 7.18.5，TypeScript 使用生成器兼容的 5.9.3，其余版本以 `frontend/package-lock.json` 为准。UI 由 React 单元测试及 Playwright 浏览器验收覆盖。
