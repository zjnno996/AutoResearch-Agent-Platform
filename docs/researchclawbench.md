# ResearchClawBench 接入与评测

EMNETS-RA 提供官方 [ResearchClawBench](https://github.com/InternScience/ResearchClawBench) 的适配器。评测代码与数据需要独立安装，仓库不附带任务数据、评审缓存、评分反馈或本机运行账本。网页项目完成和内部模型复核分数不等于官方 benchmark 成绩。

## 准备

先按 [Web 工作空间文档](web-workspace.md) 安装项目、配置私有模型环境。研究、代码与内部复核默认使用 Qwen3.8-27B-FP8；裁判的环境和参数应按官方评测要求单独配置。模型服务地址和凭据通过仓库外的 `provider.env` 设置。

```bash
# 从项目根目录执行；评测目录可放在任意位置。
export RCBENCH_DIR="$PWD/.local/ResearchClawBench"
git clone https://github.com/InternScience/ResearchClawBench "$RCBENCH_DIR"
# 按上游 README 准备依赖、数据和裁判环境。
. scripts/load-provider-env.sh
.venv-web/bin/python -m researchclaw rcbench --help
.venv-web/bin/python -m researchclaw rcbench doctor \
  --repo "$RCBENCH_DIR" --config "$PWD/config.arc.yaml" --task Math_001
```

适配器记录已检查的上游版本；实际使用前通过 `doctor` 确认兼容性。上游数据可用性、模型服务可达性和裁判凭据决定能否进行完整评测，不能仅凭服务端口在线判定。

## 执行与汇总

```bash
.venv-web/bin/python -m researchclaw rcbench register \
  --repo "$RCBENCH_DIR" --config "$PWD/config.arc.yaml"

# 一次任务、一次预选尝试；本命令会实际调用研究模型与官方裁判。
.venv-web/bin/python -m researchclaw rcbench batch \
  --repo "$RCBENCH_DIR" --config "$PWD/config.arc.yaml" \
  --task Math_001 --repeats 1

.venv-web/bin/python -m researchclaw rcbench summary --repo "$RCBENCH_DIR"
```

其他命令包括 `prepare`、`agent` 和 `score`；运行相应子命令的 `--help` 查看参数。结果保留在指定的本地评测目录，默认不会随项目提交。

## 可比性与验收

- 固定代码、设置、模型、数据和上游评测版本，提前指定任务与尝试数量。不同版本的单题分数不得拼成同一批次均分。
- 分别记录任务执行、内部科学验收、成果导出和官方评分。内部复核高分不能替代官方裁判结果；失败、缺失分数和科学缺口保留在账本中。
- 研究阶段仅使用原始任务、可见输入及合法公开资料，不读取 evaluation-only 目标研究、检查清单、评分反馈或其他尝试的结果。
- 记录模型调用量、追踪 token、墙钟时间与并发条件。服务排队、KV 压力和模型推理耗时应分开观察，不通过放宽科学验收伪造提速。
- 活动研究绑定固定源码；修改开发目录不会静默改变正在运行的候选。需要升级时新建版本和尝试，保留旧记录。

`scripts/check-qwen-bench-suite.py` 用于只读核对指定批次的身份、执行哈希、导出与评分终态；`scripts/bench_admission.py` 和 `scripts/watch-bench-capacity.py` 可用于共享模型服务的聚合容量观察。它们只接受显式指定的本地清单，不携带本机服务地址或访问密钥。

本仓库公开的是运行能力与方法，不声明未附带可复核官方记录的榜单成绩。端到端功能联调记录见 [Web 工作空间文档](web-workspace.md)。
