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
