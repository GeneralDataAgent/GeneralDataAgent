# Stage 6 运行手册

全部命令在仓库根目录执行。使用锁定 uv 环境；原始数据和输出默认不入 Git。

## 数据和评分器资格

```bash
UV_CACHE_DIR=/tmp/deepagents-uv-cache uv run --frozen python evals/prepare_stage6.py
UV_CACHE_DIR=/tmp/deepagents-uv-cache uv run --frozen python evals/prepare_stage6_bird.py
UV_CACHE_DIR=/tmp/deepagents-uv-cache uv run --frozen python evals/qualify_stage6_bird.py
UV_CACHE_DIR=/tmp/deepagents-uv-cache uv run --frozen pytest tests/test_stage6_evaluation.py tests/test_stage6_release_gates.py
```

首次下载约 87 MB 表格/任务源和 800 MB BIRD 包，SQLite 解压后约 1.5 GB。
准备器只下载/提取数据，不执行上游代码。BIRD 参考 SQL 资格记录在
`.artifacts/stage6/bird-environment.json`，它不是智能体成绩。

## 真实模型评估

先选已经配置的 CC Switch provider ID；不复制或写入密钥。可用 `--app-type claude` 或
`--app-type codex`，未指定 provider ID 时读取相应当前配置。不同模型的运行目录必须不同。
所有运行目录要求不存在，防止覆盖旧证据。

```bash
LANGSMITH_TRACING=false LANGCHAIN_TRACING_V2=false UV_CACHE_DIR=/tmp/deepagents-uv-cache uv run --frozen python evals/run_stage6.py --benchmark bird --limit 60 --workers 2 --app-type codex --provider-id PROVIDER_ID --bird-databases data/stage6/bird/databases --output .artifacts/stage6/bird-run
LANGSMITH_TRACING=false LANGCHAIN_TRACING_V2=false UV_CACHE_DIR=/tmp/deepagents-uv-cache uv run --frozen python evals/run_stage6.py --benchmark infi --limit 60 --workers 2 --app-type codex --provider-id PROVIDER_ID --output .artifacts/stage6/infi-run
LANGSMITH_TRACING=false LANGCHAIN_TRACING_V2=false UV_CACHE_DIR=/tmp/deepagents-uv-cache uv run --frozen python evals/run_stage6.py --benchmark dabstep --limit 10 --workers 1 --app-type codex --provider-id PROVIDER_ID --output .artifacts/stage6/dabstep-run
```

并发 worker 是独立题目进程，不是有共享轨迹的多智能体角色。并发数进入协议指纹。
避免超出服务限流。模型异常不自动重试；整题最多 125 秒，子进程超时会被终止。
SDK token 是事后遥测上限，单次调用可能越过阈值；不是供应商侧硬计费上限。

模型只有受控 SQL 工具。原始 DABstep 文档和 JSON 作为资料，不允许通过它们增加权限。
CSV 导入采用全列有限数→REAL、空单元格→NULL 的披露规则，这可能改变前导零、极大整数
或非标准缺失值语义，不能宣称与原始 pandas 环境等价。建模题不自动安装依赖或执行 Python。

每个目录保存 `report.partial.json` 和完整的 `report.json`，后者含逐题结果、协议指纹和摘要。
中断运行只有 partial，不能当完整成绩；修复协议后开新目录重新冻结并运行。保留旧结果，不
拼接不同协议/模型的成功题目制造高分。评分器和 runner 指纹定位工作树实际字节，Git commit
可能是运行启动时的基线提交；应结合哈希核对本轮代码，不单凭 commit 判断运行版本。

## 聚合与回归

```bash
UV_CACHE_DIR=/tmp/deepagents-uv-cache uv run --frozen python evals/run_stage6_acceptance.py --report .artifacts/stage6/bird-run/report.json --report .artifacts/stage6/infi-run/report.json --report .artifacts/stage6/dabstep-run/report.json --output .artifacts/stage6/acceptance.json
```

退出 1 表示发布诊断存在阻断，依旧会保存结果。首次运行没有配对基线、独立人工/重复/线上
证据时预期不通过。后续可用多个 `--baseline PATH` 提供对应轨道的同协议完整报告；
样本、协议不同就拒绝配对。报告摘要被改写、样本重复或遥测不合法同样拒绝。

完整回归使用现有 Stage 1–5 runner 与 `uv run --frozen pytest --cov=generaldataagent
--cov-fail-under=85`。公开基准网络运行不放入每次提交 CI；CI 测试评分器、隔离边界和门禁，
夜间/正式网络评估应由明确选定的模型与预算触发。

## 无模型复核

```bash
UV_CACHE_DIR=/tmp/deepagents-uv-cache uv run --frozen python evals/replay_stage6.py --report .artifacts/stage6/bird-run/report.json --output .artifacts/stage6/bird-replay.json
```

InfiAgent、DABstep 采用相同命令，替换报告路径。复核会重建队列并校验来源和数据库摘要，
再对原预测评分；不发起模型请求。评分一致只能证明结果可重复，不能证明答案正确或生产通过。

## 显式选择本次运行型号和工具配置

可用型号目录必须实时核查。`--model EXACT_MODEL_ID --transport openai-chat` 只影响当前评估；
不修改 CC Switch 当前型号、端点或密钥。父进程记录有效路由摘要，子进程重建并验证摘要，
配置在运行中漂移会拒绝执行。浮动 `~...latest` 选择器和带 URL 凭据/参数的目标被拒绝。

`--tool-profile sql` 是原 SQL 基线；`--tool-profile sql-analysis` 增加固定的 describe、Pearson、
单变量描述性线性拟合与文档检索。只允许指定本题表和已存在列，拒绝任意代码或其它数据源。
线性拟合输出不作为训练/测试泛化性能。文档检索每次返回至多 6,000 字符，并保留段落摘要。

新版协议将评分器、数据导入、SQL 执行器和预算单独锁定；模型、提示和工具实现放在被测系统
指纹中。因此同一评估协议可以比较不同实现，同时数据、评分或预算变化仍拒绝配对。
服务端若提供 `usage.cost`，逐题累计为 `provider_reported`；失败调用和未知计费保留 null，
不会用公开报价代替实测费用。每题观察费用超过 0.02 美元即停止；这是调用后预算护栏，
不是供应商侧硬计费上限。

## 服务节流、熔断与循环中止

`--request-interval-seconds 1.6` 使用 `.state/stage6/request-pacing.sqlite` 在多个进程间共享请求
时隙；其中只存目标摘要和时间，不存密钥、问题或答案。型号/请求参数仍由单次运行显式选择。
NVIDIA Nemotron 原生 Chat 可用 `--thinking off --json-mode`；参数来源是 NVIDIA 官方 NIM
[接口说明](https://build.nvidia.com/playbooks/nim-llm/instructions)。这只关闭本次请求的长推理。

HTTP 401/402/403/404 会立即打开本轮服务熔断；其它连续三次模型服务失败也会打开。
已在执行的工作允许按原有截止完成；后续题目标记 `service_circuit_open` 和 `attempted=false`，
依旧保留在原分母。任务报告另列实际尝试数量和尝试任务 P95，避免把跳过的零耗时当作低延迟。
退出码 2 表示服务阻断，退出码 1 仍用于发布诊断不通过。程序不会充值、更改账户或修改默认模型。

动作字段错误会返回结构化修复提示；完全相同的动作累计三次会以 `repeated_action` 中止。
显式 `final_sql` 可以完成此前探索过的 SQL，不会误当成循环。数据库上下文仅提供一行样例，
长文本有截断标识；真正的完整值必须通过查询取得。

## 工具交叉核验与重复实验

```bash
UV_CACHE_DIR=/tmp/deepagents-uv-cache uv run --frozen python evals/qualify_stage6_analysis.py
UV_CACHE_DIR=/tmp/deepagents-uv-cache uv run --frozen python evals/compare_stage6.py --baseline BASELINE_REPORT --candidate CANDIDATE_REPORT --output COMPARISON_JSON
UV_CACHE_DIR=/tmp/deepagents-uv-cache uv run --frozen python evals/qualify_stage6_stability.py --report RUN1 --report RUN2 --report RUN3 --output STABILITY_JSON
```

工具资格检查覆盖 52 个被任务引用的 CSV，每文件最多三个数值列，与 DuckDB 独立比较。
重复实验要求独立记录、相同题目顺序、数据摘要、协议和被测系统，拒绝复制报告充当多次运行。
模型服务失败过多时，配对比较保留分数变化，但明确阻止将变化归因于算法，也不输出显著性结论。

## 编码保留导入与反事实评分资格

`--csv-importer identifier-v2` 显式启用新版策略；默认 `legacy-v1` 仅用于复现历史运行。
新版整列遇到前导零整数、过大整数或非数值文本时保留 TEXT；其它有限数值为 REAL。
导入策略变化会改变评估协议，不能与旧版直接配对。相同原始文件也不代表执行语义相同。

```bash
UV_CACHE_DIR=/tmp/deepagents-uv-cache uv run --frozen python evals/qualify_stage6_analysis.py --csv-importer identifier-v2 --include-dabstep
UV_CACHE_DIR=/tmp/deepagents-uv-cache uv run --frozen python evals/qualify_stage6_scorer.py
LANGSMITH_TRACING=false LANGCHAIN_TRACING_V2=false UV_CACHE_DIR=/tmp/deepagents-uv-cache uv run --frozen python evals/run_stage6.py --benchmark dabstep --limit 10 --workers 1 --app-type claude --provider-id PROVIDER_ID --model nvidia/nemotron-3.5-lightning-30b-a3b --transport openai-chat --thinking off --json-mode --tool-profile sql-analysis --csv-importer identifier-v2 --request-interval-seconds 1.6 --output .artifacts/stage6/dabstep-fresh-run
```

三轮重复要分别使用新的目录和真实模型调用；聚合可传入多个 `--repeat-report PATH`。
重复证据须与候选的完整协议、系统和数据一致；不同协议会抛出错误，不能关闭稳定性门禁。
反事实评分资格使用合成 SQLite 夹具，覆盖偶然等价、NULL、重复值和日期边界，不算模型成绩。

## 实际产品 Supervisor 联调

```bash
LANGSMITH_TRACING=false LANGCHAIN_TRACING_V2=false UV_CACHE_DIR=/tmp/deepagents-uv-cache uv run --frozen python evals/qualify_stage6_supervisor.py --provider-id PROVIDER_ID --model nvidia/nemotron-3.5-lightning-30b-a3b --limit 3 --output .artifacts/stage6/supervisor-fresh-run/report.json
```

需要已经准备好的 `data/tlc/tlc-2025-01.sqlite`。驱动在一个异步会话中调用实际产品 Supervisor，
每题 60 秒，局部证据落本地，并与确定性认证指标运行比较。输出文件必须不存在。
工具调用模式不使用 JSON 动作模式；没有任意代码或外部发送能力。未触发执行器不能被算作
请求绑定通过；完整成功同时要求正确请求、角色顺序、相同结果和哈希校验后的真实证据。
三题联调仅是既有 TLC 产品路径的探测，不代表公开数据源已接入产品或完成生产资格。

双环境完整回归时显式指定解释器和隔离环境，避免工具按项目默认解释器重建环境：

```bash
LANGSMITH_TRACING=false LANGCHAIN_TRACING_V2=false UV_CACHE_DIR=/tmp/deepagents-uv-cache COVERAGE_FILE=.artifacts/stage6/py312.coverage uv run --frozen --python 3.12 pytest --cov=generaldataagent --cov-fail-under=85
LANGSMITH_TRACING=false LANGCHAIN_TRACING_V2=false UV_CACHE_DIR=/tmp/deepagents-uv-cache UV_PROJECT_ENVIRONMENT=/tmp/gda-stage6-py311 COVERAGE_FILE=.artifacts/stage6/py311.coverage uv run --frozen --python 3.11 pytest --cov=generaldataagent --cov-fail-under=85
```

## 单次输出预算与服务预检（当前协议）

`--max-output-tokens` 默认 3,000，可请求 32–3,000；具体提供商兼容性需要实测。
它同时进入有效路由和 `stage6-v7` 协议。共享请求间隔允许 0–60 秒，仍受整题截止约束，
配置更慢的间隔可能使任务在原时间预算内无法完成所有动作。

历史实验使用旧协议；复现精确历史运行需检出相应提交/源码快照。当前命令产生新协议记录，
不能将不同输出预算、节流参数或版本的成绩直接配对。

先运行含真实上下文的六题校准，再为完整批次显式提供校准报告：

```bash
LANGSMITH_TRACING=false LANGCHAIN_TRACING_V2=false UV_CACHE_DIR=/tmp/deepagents-uv-cache uv run --frozen python evals/run_stage6.py --benchmark bird --limit 6 --workers 1 --app-type claude --provider-id PROVIDER_ID --model nvidia/nemotron-3-super-120b-a12b --transport openai-chat --thinking off --json-mode --max-output-tokens 1024 --request-interval-seconds 16 --csv-importer identifier-v2 --tool-profile sql-precision --bird-databases data/stage6/bird/databases --output .artifacts/stage6/calibration-fresh
LANGSMITH_TRACING=false LANGCHAIN_TRACING_V2=false UV_CACHE_DIR=/tmp/deepagents-uv-cache uv run --frozen python evals/run_stage6.py --benchmark bird --limit 60 --workers 1 --app-type claude --provider-id PROVIDER_ID --model nvidia/nemotron-3-super-120b-a12b --transport openai-chat --thinking off --json-mode --max-output-tokens 1024 --request-interval-seconds 16 --csv-importer identifier-v2 --tool-profile sql-precision --bird-databases data/stage6/bird/databases --preflight-report .artifacts/stage6/calibration-fresh/report.json --output .artifacts/stage6/full-fresh
```

校准报告必须完整，最少六题、全部有已观测模型回复、服务失败不超过 5%、token 遥测完整。
该闸门不检查答案成功率，避免通过剔除难题来伪装服务健康。它还核对基准、协议、有效模型路由
和来源快照；错误校准会以退出码 2 保存拒绝记录，并在启动 worker 前停止，模型调用为零。
校准通过只是样本服务检查，不能保证后续容量，也不能替代质量或生产验收。

`sql-precision` 提供声明的主外键上下文，仅适用于 SQL 输出轨道。
只读元数据资格可用 `evals/qualify_stage6_sql_schema.py --output PATH` 重现。
本轮因服务校准失败尚未获得其有效完整能力结果，参见
[迭代总结](../validation/stage6-iteration-summary-2026-10-20.md)。

## Magpie 网关

第六期 runner 和实际指标 Supervisor 资格脚本现在支持 `--app-type magpie`。
连接只读解析 `~/.codex/config.toml` 中明确指定的 `model_providers.magpie`，使用其中的
`base_url`、`wire_api`、`experimental_bearer_token` 和 `http_headers`；不修改 CC Switch、
Codex 或 Magpie 设置，也不读取/复制 Magpie 上游供应商密钥。

必须明确给出 `--model`。全局 Codex model 可能来自其他供应商，因此不能用作 Magpie 默认值。
可先查询已配置网关的 `/v1/models`，选择固定路径，例如本机已发现的
`workbuddy/deepseek-v4.1-flash`。避免用 `auto/` 路由作固定模型对照。网关返回的型号名称
不构成服务端权重版本的独立证明；跨模型结果不能归因于单一算法改进。

```bash
# 沿用原有结构化叙述验证入口，也可选择 Magpie。
LANGSMITH_TRACING=false LANGCHAIN_TRACING_V2=false UV_CACHE_DIR=/tmp/deepagents-uv-cache uv run --frozen python examples/cc_switch_smoke.py --app-type magpie --provider-id magpie --model workbuddy/deepseek-v4.1-flash --timeout-seconds 20

# 使用真实任务上下文校准；先确认完整 report 的 service_readiness 通过。
LANGSMITH_TRACING=false LANGCHAIN_TRACING_V2=false UV_CACHE_DIR=/tmp/deepagents-uv-cache uv run --frozen python evals/run_stage6.py --benchmark infi --limit 6 --workers 1 --app-type magpie --provider-id magpie --model workbuddy/deepseek-v4.1-flash --transport openai-chat --json-mode --max-output-tokens 3000 --tool-profile sql-repair --csv-importer identifier-v2 --request-interval-seconds 3.5 --output .artifacts/stage6/magpie-infi-calibration

# 同轨道、型号、路由与协议的服务预检通过后，运行固定六十题。
LANGSMITH_TRACING=false LANGCHAIN_TRACING_V2=false UV_CACHE_DIR=/tmp/deepagents-uv-cache uv run --frozen python evals/run_stage6.py --benchmark infi --limit 60 --workers 1 --app-type magpie --provider-id magpie --model workbuddy/deepseek-v4.1-flash --transport openai-chat --json-mode --max-output-tokens 3000 --tool-profile sql-repair --csv-importer identifier-v2 --request-interval-seconds 3.5 --preflight-report .artifacts/stage6/magpie-infi-calibration/report.json --output .artifacts/stage6/magpie-infi-full

# 实际指标产品编排验证，不是公开数据集的代理成绩。
LANGSMITH_TRACING=false LANGCHAIN_TRACING_V2=false UV_CACHE_DIR=/tmp/deepagents-uv-cache uv run --frozen python evals/qualify_stage6_supervisor.py --app-type magpie --provider-id magpie --model workbuddy/deepseek-v4.1-flash --limit 3 --output .artifacts/stage6/magpie-supervisor/report.json
```

不指定 `--transport openai-chat` 时沿用配置的 Responses/Chat 协议；JSON mode 仍只允许
Chat 路径。附加鉴权头在请求时解封，日志/报告只有头摘要与适配器源码摘要；父进程和隔离
worker 重新核对路由，配置变化会拒绝运行。部署者可显式调用 `MagpieProvider.load(config_path=...)`
读取其他只读配置，但当前评估 CLI 统一使用用户的现有 Codex 配置位置。

本机实测普通 Responses 文本可用，但当前所选上游没有遵循原生 JSON schema 输出约束。
叙述入口对 Magpie 显式使用函数调用结构化返回，仍执行同一 Narrative schema 校验；其他
供应商默认行为不变。模型在正文/工具参数里自报的型号不可信，叙述元数据由已选择路由覆盖。

单次小请求和 JSON mode 参数不保证供应商输出始终合法；必须保留真实任务中的无效动作、
服务失败、超时和全部样本分母。服务校准通过也不代表答案质量或生产验收通过。
