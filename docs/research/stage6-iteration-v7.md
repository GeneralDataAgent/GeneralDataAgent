# 第六期第七轮：减少执行错误与上下文浪费

本轮针对既有失败轨迹开发，不读取黄金 SQL/答案来生成任务解法，也不放宽评分或权限。
沿用[数据集与评估方案](stage6-benchmark-plan.md)。v7 表示本次系统迭代；评分协议仍为
`stage6-v6`，每份报告保存真实协议和实现指纹，不能只根据目录后缀判断可比性。

## 问题和实现

旧轨迹中可见三类可修复损失：同一 SQL 重复到预算用尽；同时返回 SQL/答案导致动作被拒绝；
大量费用 JSON/明细反馈进入上下文后耗尽 token。此外，查询错误只反馈类别，无法定位拼错的列。

新增 `sql-repair` 配置，继续复用原有只读 SQL、固定统计与检索工具：

- 只去掉空动作字段。SQL 与答案同时出现时，仅执行 SQL，并明确记录“丢弃提前答案”；
  不把未执行的预测当作工具结果。两个非空工具动作仍拒绝，不猜测执行顺序。
- 查询成功后提示及时完成；空结果提示检查真实日期/类别；每轮显示剩余动作预算。
- 用成功工具步骤核对引用。缺引用、公开答案键缺失/错误时允许在原有总步数内修正；
  不自动编造引用、不把评分器的隐藏键透露给模型。
- 仅把已知本地 SQL 诊断中的列名/表名/函数名作为反馈。任意 SDK 异常文本不会被回显。
- 工具反馈序列化后至多 6,000 字符；长单元格、过宽列元数据和非表结果均受限。
  返回值明确标为不完整预览；完整执行结果继续用于哈希和评分，模型需在全量数据上做聚合。
- 错误轨迹新增失败 SQL，便于下一轮定位。没有增加重试次数、执行时间、任意 Python 或外部权限。

一次工具回放资格检查重现了历史 DABstep 14 条不同 SQL 结果，全部完整结果哈希一致。
反馈字符总数从 114,677 降到 31,289，3 条被截断，均在预算内。这是字符量测量，
不能等同于真实 token、账单或所有任务均能获得的节省。

## 公平对照

原 30B 型号的三题先导仍有两题 429。读取现有连接的型号目录后，DeepSeek 替代型号最小
调用超时；`nvidia/nemotron-3-super-120b-a12b` 最小调用成功。所有连接复用已配置凭据，
不修改默认型号、账户、密钥或付费配置。

随后使用同一 120B 型号、同一题目/数据库、`identifier-v2` 导入、六步/24,000 token 预算、
共享 3.5 秒请求间隔，对比 `sql-analysis` 和 `sql-repair`。不能把换型号后的结果直接归因于动作修复。

先导队列：BIRD 原策略 5/12、改进版 6/12；InfiAgent 原策略 4/12、改进版 7/12。
这些结果用于决定是否扩大评估，不足以建立总体提升结论。完整队列分别重新运行 60 题，
不把先导的成功案例拼入新报告；DABstep dev 独立使用完整十题对照。
最终实测与逐题证据见[结果报告](../validation/stage6-iteration-v7-results.md)。

## Supervisor 的请求绑定工具

增加可选的 `MetricSupervisorRuntime(bound_tool_arguments=True)`：认证指标工具直接闭包绑定
已审核的指标、版本、维度和时间，模型只调用空参数工具。底层领域权限、角色顺序、只读执行、
结果验证、缓存一致性和证据检查全部保留。默认模式暂不切换，由实际联调结果决定是否晋升。

本地 SDK 的空参数工具存在跳过输入验证的快速路径，因此实现显式验证的工具适配器，
拒绝额外参数，避免“schema 声称禁止、运行时却忽略”的差异。测试同时核验正确调用、
拒绝参数覆盖、领域越权、错误角色顺序及缺失绑定请求。

真实联调在同一 120B 型号上分别跑原模式和绑定模式各三个 TLC 指标。每题仍为 60 秒，
必须同时满足完整角色链、正确请求、数值与确定性参考一致、真实证据哈希匹配，才计为成功。
这不是开放基准在生产 Supervisor 上的资格认证。

## 重现

在仓库根目录运行，输出目录使用新路径：

```bash
LANGSMITH_TRACING=false LANGCHAIN_TRACING_V2=false UV_CACHE_DIR=/tmp/deepagents-uv-cache uv run --frozen python evals/run_stage6.py --benchmark infi --limit 60 --workers 1 --app-type claude --provider-id PROVIDER_ID --model nvidia/nemotron-3-super-120b-a12b --transport openai-chat --thinking off --json-mode --request-interval-seconds 3.5 --csv-importer identifier-v2 --tool-profile sql-repair --output .artifacts/stage6/infi-repair-fresh
LANGSMITH_TRACING=false LANGCHAIN_TRACING_V2=false UV_CACHE_DIR=/tmp/deepagents-uv-cache uv run --frozen python evals/qualify_stage6_supervisor.py --provider-id PROVIDER_ID --model nvidia/nemotron-3-super-120b-a12b --bound-tools --output .artifacts/stage6/supervisor-bound-fresh/report.json
UV_CACHE_DIR=/tmp/deepagents-uv-cache uv run --frozen python evals/qualify_stage6_repair.py --report .artifacts/stage6/dabstep-nvidia-enhanced-v5/report.json --output .artifacts/stage6/feedback-replay-fresh.json
UV_CACHE_DIR=/tmp/deepagents-uv-cache uv run --frozen python evals/build_stage6_iteration_report.py --manifest evals/stage6_iteration_v7.json --output docs/validation/stage6-iteration-v7-results
```

证据构建器重新验证摘要、配对协议和全部预测评分，生成 Markdown 与机器证据，不调用模型。
配置 manifest 只指定报告位置，不包含凭据或标签。生产验收仍由独立门禁决定。

## 实际产品路径暴露的附加问题

原参数接口三题全部在 60 秒超时，但发现、分析、验证、报告四个工具角色已经完成，
数值与证据也可核验。原实现仍等待模型的后续收尾回复，使已完成的交付被标为失败。
新增已验证交付信号：报告工具确认候选通过验证后发出信号，运行时取消多余模型工作，
直接返回已有认证交付。错误/越权/缺证据不能触发信号，外层总截止仍有效。
测试覆盖及时返回、取消清理、原错误传播、总截止、两种工具接口和真实工厂准入路径。

空参数工具的首次联调因改写内置角色提示被准入机制拒绝；已恢复标准角色定义，
没有放宽准入机制。随后三题重新联调仍在调用执行器前超时，故此接口保留为实验模式，
不默认启用。原参数接口与及时完成机制的独立实测另列在最终报告，不能把两项变化混作单一归因。

## 后续 SQL 与服务校准

针对通用修复版的 BIRD 收益不足，另实现 `sql-precision` 和声明主外键上下文，
已对 11 库、105 条列级外键映射完成只读资格检查。两批完整队列均触发限流，未取得有效能力对照。

后续预算协议升为 `stage6-v7`，显式记录单次输出上限，支持更保守的请求节流和校准闸门。
1,024 token / 16 秒间隔的六题校准仍有两次服务失败，实际闸门已拒绝扩大请求。
这些结果独立于本文件前述固定 `stage6-v6` 的三轨道配对，详见
[最终迭代总结](../validation/stage6-iteration-summary-2026-10-20.md)。
