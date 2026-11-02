# EMNETS-RA 服务部署

部署使用前端生产构建、Caddy 网关和 Supervisor 进程守护。科研、评审、资源服务分别运行，科研工作空间与开发目录中的历史运行数据分开保存。所有模型调用继续读取仓库外的私有 `provider.env`。

## 安装与启动

先按项目首页配置模型并完成 Web 环境安装。以下工具安装器支持 Linux x86_64，固定下载版本并核验官方发布的 SHA-256：

```bash
.venv-web/bin/python -m pip install -r backend/requirements-production.txt
python3 scripts/install-production-tools.py
npm --prefix frontend-web run build
.venv-web/bin/python scripts/manage-production.py start --public
.venv-web/bin/python scripts/manage-production.py status
.venv-web/bin/python scripts/manage-production.py url
```

首次启动会创建 `emnets` 账号和随机密码，保存在 `~/.config/emnets-ra/production-login.txt`，文件权限为 0600。HTTPS 入口的访问口令与应用内登录使用这一账号。再次启动保留账号和数据。

默认网关仅监听 `127.0.0.1:8080`；三个后台服务分别监听本机 9905、9906、9907。网关负责页面、WebSocket、评审流和成果下载，前端无需填写后台地址。没有口令的页面、API、WebSocket 和下载请求均由网关拒绝。

评审使用 NDJSON 流，在模型长时间生成期间每 15 秒发送空闲心跳，避免将长请求误认为连接中断；心跳不计入评审结果。浏览器模型目录只显示模型名称，不返回私有服务地址。

结构化模型回复会检查完整 JSON 后的异常闭合符重复。仅在根对象完整且能够严格解析时保留其原始内容，后续科学与执行校验继续生效；不完整或无效的对象仍被拒绝。该机制不限制正常输出长度，也不降低模型推理强度。恢复事件记录在运行目录的 `traces/transport_recoveries.jsonl`；提前终止异常后缀时，供应商尚未返回完整用量的情况会明确标记。

`--public` 使用 Cloudflare Quick Tunnel 生成临时 HTTPS 地址。地址在隧道进程退出后失效，重建时会变化；它适合外网联调。固定域名和长期公网服务需要配置正式隧道或现有反向代理。参见 [Cloudflare 官方说明](https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/)。正式反向代理应连接本机网关，保留访问认证，并支持 WebSocket 与流式响应。

## 数据与维护

- `backend/runs/production/`：生产账号、科研项目、队列和源码快照。
- 该目录下的 `review_history/`、`review_cache/`：生产评审记录与解析缓存。
- `.local/production/`：进程配置、口令哈希、日志和管理 socket。
- `~/.config/emnets-ra/production-access.json`：私有初始账号配置。

上述运行目录与凭据均不提交 Git。Caddy 只提供 `frontend-web/dist/` 中的构建文件，源码、环境文件和内部日志路径不对外提供。

```bash
# 查看服务状态
.venv-web/bin/python scripts/manage-production.py status
# 停止本部署的全部服务，保留数据
.venv-web/bin/python scripts/manage-production.py stop
```

Supervisor 会重启意外退出的服务。在使用 systemd、Docker 或其他宿主管理器时，应将生产启动命令加入其启动配置；仅启动当前容器中的 Supervisor 不代表宿主重启后会自动拉起容器。

可使用 `EMNETS_PRODUCTION_RUNS_DIR`、`EMNETS_GATEWAY_PORT`、`EMNETS_PRODUCTION_AGENT_PORT`、`EMNETS_PRODUCTION_RESOURCE_PORT`、`EMNETS_PRODUCTION_REVIEW_PORT` 设置隔离的目录和端口。手动安装工具时可设置 `EMNETS_CADDY_BIN`、`EMNETS_CLOUDFLARED_BIN`。

## 公网科研验收

默认生产目录下可以显式提交一个固定训练集与测试集的线性回归验收任务：

```bash
EMNETS_PUBLIC_URL=https://your-deployed-address node frontend-web/scripts/verify-production.mjs --submit
python3 scripts/inspect-production-run.py --watch
# 六阶段完成后，经公网页面导出报告、PDF 和 ZIP
node frontend-web/scripts/export-production-run.mjs
PYTHONPATH=backend/agent .venv-web/bin/python scripts/verify-production-delivery.py
```

浏览器脚本通过真实登录和页面提交，不替换模型返回；已有验收运行记录时拒绝重复提交。最后一个检查只适用于该固定数值题：核对阶段、执行哈希、8 行数据及指标，并删除解压后的旧输出，在干净目录独立重跑下载的实验代码。验收记录和下载文件保存在 `.local/production/verification/`，不构成官方 benchmark 成绩。

PDF 按钮打开浏览器打印窗口，可选择“另存为 PDF”。项目图表通过成果下载接口读取并嵌入打印文档，加载完成后才开始打印；加载失败会显示错误并停止自动打印。浏览器验收会检查图片已解码，再保存实际打印文档。交付核验同时审计冻结方法探针、独立探针试运行与主实验调用记录。

锁定的 Qwen3.8 服务使用普通 JSON 模式生成实验协议，由控制器用完整 JSON Schema 和科学约束共同校验。该兼容路径用于规避生产验收中原生 `json_schema` 持续生成协议而不结束的问题；缺字段、错误类型和不合格方法仍会被拒绝。局部修复先合并模型原始字段，再展开控制器拥有的来源锚点，避免已验证的原文被重复生成。
