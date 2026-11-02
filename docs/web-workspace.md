# EMNETS-RA 实验室 Web 工作空间

本项目包含可独立构建的 React 前端、Python 研究服务、论文评审服务与资源服务。Web 版为推荐入口；原桌面前端保留在 `frontend/`。

## 启动

需要 Python 3.11/3.12 和 Node 20.19+（或 22.12+）。从仓库根目录运行：

```bash
./scripts/setup-web.sh
./start-web.sh start
./start-web.sh status
```

开发环境默认访问 `http://localhost:5911/`。Web 版默认使用持久目录 `backend/runs/web-workspace/`，避免打开新版界面时自动恢复旧开发队列；原 `backend/runs/` 数据完整保留。需要使用历史账号与项目时，可显式设置 `EMNETS_RUNS_DIR=/absolute/path/to/backend/runs`。安装脚本创建项目内 `.venv-web`，不改动其他 Python 环境。已有 Python 环境可通过 `PYTHON_PATH=/path/to/python ./start-web.sh start` 使用；安装时可指定 `BOOTSTRAP_PYTHON=/path/to/python ./scripts/setup-web.sh`。

新项目在首次启动 worker 时固定 `engine_snapshots/<sha256>/researchclaw`，并把运行目录及版本写入项目元数据。后续阶段与重启后的调度读取同一快照，开发目录编辑不会改变项目中途的源码身份。已有旧检查点不会被静默重新绑定；旧运行需保留原版本，或用更新后的引擎新建项目。

模型凭据沿用 `scripts/load-provider-env.sh` 加载的私有环境。所有研究、代码与评审角色继续使用配置的 Qwen3.8-27B-FP8。浏览器不接收 API Key。登录与注册使用当前部署的本地工作空间账号，不是浙大统一身份认证。

| 服务 | 代码 | 默认端口 | 功能 |
| --- | --- | ---: | --- |
| Web（开发） | `frontend-web/` | 5911 | 登录、总览、研究、评审、技能与设置 |
| 研究与项目服务 | `backend/services/agent_bridge.py` | 8906 | 用户认证、项目、实时事件、成果与技能目录 |
| 评审服务 | `backend/services/review_service.py` | 8907 | 论文上传、评审、历史记录与报告导出 |
| 资源服务 | `backend/services/resource_monitor.py` | 8905 | CPU、内存与 GPU 状态 |

Vite 将 `/ws/agents`、`/ws/resources`、`/api/review`、`/api/models`、`/api/health` 和 `/download` 代理到对应后端。可用 `AGENT_BRIDGE_PORT`、`REVIEW_PORT`、`RESOURCE_MONITOR_PORT`、`FRONTEND_WEB_PORT` 覆盖端口。`EMNETS_RUNS_DIR` 可指定持久工作空间目录，`EMNETS_POOL_IDEA/EXP/CODE/EXEC/WRITE` 可指定各层 worker 数量，设为 0 可用于只读联调。`start-web.sh` 提供 `start/stop/restart/status`，不需要清空已有项目。

## 真实数据与交互

- 总览的项目数量、状态、阶段进度来自当前登录用户的实际项目；无项目或服务未连接时显示明确的空状态。
- `get_workspace_overview` 为只读 WebSocket 命令，服务端校验认证、按用户过滤，再只返回界面需要的字段；不会发起研究或调用模型。
- 工作空间连接统一处理认证、项目事件触发更新、30 秒兜底轮询、断线重连和页面卸载清理。
- 全局搜索支持 `Ctrl/⌘ + K`，查找页面、项目与已安装技能。项目跳转保留项目 ID，新建入口聚焦研究主题。
- 技能目录读取真实安装来源，支持分类、搜索、分页和按研究阶段预览匹配。预览不调用模型。
- 设置页读取模型与服务状态，支持主题持久化和当前账号的概览 JSON 导出。
- 研究默认采用「专注研究」单流程，可切换多方向探索；完整流程支持「研究报告」和「IEEE 论文」两种交付，普通报告不强制论文页数。Idea 确认策略仍可单独选择。
- 最终报告支持 Markdown 下载、浏览器 PDF 导出、实验代码与结果 ZIP、数据与图表 ZIP。服务器读取实际阶段文件；ZIP 在打包前检查 64 MiB / 5000 文件上限。
- 研究页面保留原有数据上传、文献追踪、实验、产物、干预及恢复功能；评审页面保留原有多维评审与报告流程。PDF 正文可直接由 Web 依赖中的 PyMuPDF 提取，不依赖额外安装的可选解析器；完整流式评审保留最终总评。

## 视觉设计与开源参考

主色采用浙大官方[求是蓝](https://www.zju.edu.cn/english/2023/0323/c74903a2732011/page.htm) `#003F88`，配合深蓝导航、浅色内容面、中文衬线标题与 EMNETS 网络节点图形。项目标记为自行绘制的网络几何标记。实验室信息参考 [EMNETS 官网](https://www.emnets.cn/en/authors/admin/)。

以下为 2026-10-29 查看官方仓库时显示的近似星数，随时间变化：

| 开源参考 | GitHub 显示星数 | 使用的设计思路 |
| --- | ---: | --- |
| [Ant Design Pro](https://github.com/ant-design/ant-design-pro) | 38.9k | 企业工作空间的信息层级、页头与状态展示 |
| [Shadcn Admin](https://github.com/satnaing/shadcn-admin) | 16.3k | 侧栏、全局搜索、轻量卡片及可访问交互 |
| [TailAdmin React](https://github.com/TailAdmin/free-react-tailwind-admin-dashboard) | 1.3k | 响应式控制台和统计面板布局 |

以上 MIT 项目用于设计与组织方式参考，未整包替换现有科研界面。图标使用 `lucide-react`，许可证随 npm 依赖保留。

## 构建与验证

```bash
cd frontend-web
npm ci
npm run build
npx playwright install --with-deps chromium
npm run test:e2e
```

浏览器测试使用独立的 25910/18906/18907 端口与临时工作空间，研究 worker 数为 0，不发起模型研究或评审请求。生产网关使用容器端口 5910，由宿主机映射到 15910，测试结束不影响生产入口。检查真实注册认证、用户项目概览、阶段进度、搜索、技能匹配、主题、服务健康、导出和移动端布局。截图位于 `frontend-web/artifacts/`，HTML 报告位于 `frontend-web/playwright-report/`。联调项目为测试夹具，结束后清理，不作为科研产物或 benchmark 结果。

后端验证：

```bash
.venv-web/bin/python -m pip install -e 'backend/agent[dev]'
PYTHONPATH=backend:backend/agent .venv-web/bin/python -m pytest backend/tests/test_web_research_submission.py backend/tests/test_workspace_overview.py backend/tests/test_research_capabilities.py backend/tests/test_agent_bridge_recovery.py backend/tests/test_ieee_full_chain.py backend/tests/test_project_downloads.py backend/tests/test_review_pdf_extraction.py backend/tests/test_review_orchestration.py backend/tests/test_review_scoring.py backend/tests/test_engine_snapshot.py backend/tests/test_project_pause_retries.py backend/agent/tests/test_json_contracts.py backend/agent/tests/test_compact_contract_protocol.py backend/agent/tests/test_compact_workflow.py -q
```

## 本轮验证记录

2026-10-29：生产构建通过；160 项后端与研究引擎检查通过；7 条浏览器回归通过，覆盖自动启动的隔离后端、真实注册/项目提交/报告下载，以及完整评审事件保留的流协议回归（该条使用明确的测试响应）。npm 依赖扫描为 0 项漏洞。一键安装与四服务启动已在项目本地环境验证。这组回归不发起模型科研或评审生成请求。实际界面截图见 `assets/screenshots/`。

## 真实模型联调（2026-10-29）

本轮另行通过 5910 页面提交了真实 Qwen 请求，区别于上述不调用模型的回归测试：

- 论文评审：上传一页 1555 字符的自有合成技术说明，选择英文论文的 methodology 维度，沿用完整证据核验与讨论配置。约 34 分钟后返回正式评审、附加诊断、证据共识和总评，历史记录为 `20261029_131518_c2c2996b`；模型为 Qwen3.8-27B-FP8。前端显示、历史加载、刷新恢复、Markdown 和 PDF 导出均已验证，浏览器无脚本异常。该技术说明不是科研论文或 benchmark，所得 42 分仅为这份说明的模型评审结果。
- 暂停操作写入项目元数据并持久保存队列，服务重启不会把已暂停的项目当作孤儿任务恢复；只有实际阶段成功或用户手动恢复才清理失败计数，连续失败达到三次后持久停止自动重试。浏览器回归已覆盖排队项目的暂停、恢复和删除。
- 正式综合分和维度数统一排除 `deep_dive`、`patch` 等辅助诊断，历史读取、搜索和流式响应采用同一口径，模型原始意见继续保留。
- 四页评审 PDF 在保持原有分辨率的条件下启用无损压缩，从 32709732 字节降到约 1661383 字节，已验证文件可打开、中文与版式正常。导出页眉统一为 EMNETS-RA。
- 科研验收使用固定的五点无噪声线性数据，要求真实最小二乘拟合、均值基线、逐点 CSV、metrics.json、Pillow PNG 和研究报告。第一轮发现无外部数据时的生成约束与校验器不一致；已将顶层 `dataset_policy` 明确列为必需字段。第二轮采用固定源码快照，避免开发期间源码修改破坏后续阶段的检查点身份。

质量优先配置下，短任务仍可能耗时较长；本次联调不降低推理配置或人为缩短模型输出。功能联调不等同于 ResearchClawBench 成绩；暂停的 benchmark 队列保持暂停。

科研验收最终结果：第二轮项目在约 **52 分 56 秒** 后完成 `ground → plan → implement → execute → report → verify` 六个阶段。12 项任务条件全部通过独立复核；没有失败或待确认项。实际控制器执行返回码为 0，4 个源码哈希及 6 个输出哈希一致，得到 a=2、b=1、MSE=0、R²=1，均值预测基线 MSE=8；逐点 CSV 为 5 行，760×560 PNG 已目视检查。首次方案复核发现零残差探针对残差符号错误不敏感，模型修正探针后继续推进。最终模型自评为 10/10，仅用于本任务内部验收，**不是官方 benchmark 分数**。

通过真实页面下载了研究报告、实验代码与结果 ZIP、数据与图表 ZIP；实验 ZIP 在全新临时目录使用 `.venv-web` 独立重跑，指标与原执行逐项一致。研究服务重启后仍显示项目完成、6/6、100%，没有重新派发任务。浏览器未捕获脚本异常。最终生产构建、160 项 Python 检查与 7 条浏览器回归通过。

原始追踪记录了 9 次研究模型调用、312313 个输入加输出 token（不等同于供应商完整计费统计），其中方案阶段约 23 分钟、源码生成约 18.5 分钟；实际数值实验本体仅约 1.2 秒。当前已证明功能链路跑通，低延迟与低 token 消耗仍是后续优化项，未用缩减科学验收或替换模型来掩盖。

本机验证产物：

- `frontend-web/artifacts/live-integration-audit.json`：阶段、耗时、模型、数值、哈希核对与浏览器下载结果。
- `frontend-web/artifacts/live-experiment.zip`、`live-outputs.zip`、`live-research-report.md`：经页面下载的实际产物。
- `frontend-web/artifacts/live-review-export.md`、`live-review-export.pdf`：实际评审导出。
- `frontend-web/artifacts/live-research-complete.png`、`live-overview-complete.png`、`live-review-history.png`：真实浏览器截图。

第一轮失败测试已归档到 本机私有归档目录 并从活动工作空间清理，防止开发版本不匹配的旧测试反复启动；第二轮验收项目与结果保留在持久 Web 工作空间。
