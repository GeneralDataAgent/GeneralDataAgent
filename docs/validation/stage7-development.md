# 第七期开发与验证记录

更新：2026-10-22（Asia/Shanghai）。分支：`feat/stage7-semantic-sql`。

**状态：继续迭代，第七期质量验收未通过。** 已合并main `94e9d19`，CI三版本通过。最新完整v57冻结 `1cdcd84`，260次尝试完成：BIRD 26→30/60、Infi 34→39/60、DABstep 1→1/10，合计61→70/130；历史保护56/61，BIRD回退1195/83/857/931，Infi回退271。冻结源码资格检查的收据错误为空，仍因BIRD未达36/60和历史/新鲜基线回退而不具备开发资格。后续7b03359的否定精度、SQL绑定候选和Pearson公开策略没有被回填到v57分数。v53及所有单题正负结果保留。未见、重复、独立审阅及生产门禁未完成。详见[规划核对](stage7-plan-acceptance-matrix.md)、[续开发记录](stage7-continuation-2026-10-21.md)与[完整证据](stage7-20261021-results.json)；下文为历史记录。

## 已实现

- `AnalysisTaskSpec` / `AnswerSpec`：对象、粒度、过滤、时间、分母、计数口径、方向、并列、输出类型、范围和精度。公开问题及补充要求由运行时绑定；冲突拒绝；明确的英文舍入要求可确定性补齐，未找到公开舍入要求时禁止擅自声明精度，原始计算不舍入。
- `sql-semantic` 独立研究配置：简单 SQL 可以一次提交契约与查询，复杂任务保留分阶段路径；严格工具 schema；错误路径不记录输入值；统计能力仍复用 describe / Pearson / 描述回归，不扩展第八期模型。
- 只读语义上下文：字段、声明键、枚举样本、日期编码未验证标记、复合关联键重复度；主外键与观测基数分开。表/字段/样本均有预览上限和未包含数量，不把预览当全量。
- 最终 SQL 的结构矛盾检查与结果核对；显式高风险交叉验证最多一次，仍消耗原六步中的一步；不一致拒绝。
- 答案逐字段绑定成功计算的完整产物。超过 200 行的 ID 集合可通过 `collect` 完整读取，最多 10,000 项，核对总数，超限失败，禁止 LIMIT/OFFSET 截断。
- `stage7-v1` 评估入口：保留六步 / 24,000 token / 请求输出 3,000 token / 120 秒，使用 identifier-v2；严格分数、原正确题保护、争议标签侧账分别输出。旧历史成绩不可替代新协议配对基线。

认证指标 Supervisor 和生产接入未改变。

## 可复现命令

```sh
UV_CACHE_DIR=/tmp/deepagents-uv-cache LANGSMITH_TRACING=false LANGCHAIN_TRACING_V2=false uv run --frozen pytest
UV_CACHE_DIR=/tmp/deepagents-uv-cache uv run --frozen ruff check .
UV_CACHE_DIR=/tmp/deepagents-uv-cache uv run --frozen ruff format --check .
```

真实接口对照使用已有 Magpie 网关，明确型号 `workbuddy/deepseek-v4.1-flash`。分别执行以下命令，第二条只改变 profile 和输出目录，保持其余参数相同。

```sh
UV_CACHE_DIR=/tmp/deepagents-uv-cache uv run --frozen python evals/run_stage6.py --benchmark infi --limit 2 --seed 20261020 --workers 1 --app-type magpie --model workbuddy/deepseek-v4.1-flash --transport openai-chat --csv-importer identifier-v2 --evaluation-protocol stage7-v1 --tool-profile sql-repair --request-interval-seconds 3.5 --output .artifacts/stage7/infi-baseline-v2
UV_CACHE_DIR=/tmp/deepagents-uv-cache uv run --frozen python evals/run_stage6.py --benchmark infi --limit 2 --seed 20261020 --workers 1 --app-type magpie --model workbuddy/deepseek-v4.1-flash --transport openai-chat --csv-importer identifier-v2 --evaluation-protocol stage7-v1 --tool-profile sql-semantic --request-interval-seconds 3.5 --output .artifacts/stage7/infi-candidate-v2
UV_CACHE_DIR=/tmp/deepagents-uv-cache uv run --frozen python evals/compare_stage7.py --baseline .artifacts/stage7/infi-baseline-v2/report.json --candidate .artifacts/stage7/infi-candidate-v2/report.json --output .artifacts/stage7/infi-comparison-v2.json
```

BIRD 使用相同固定 seed、真实数据库快照、`--benchmark bird --bird-databases <已验证快照目录>`，基线选择 `sql-precision`。全量队列：BIRD 60、InfiAgent 60、DABstep 10；每个轨道均需要重新执行两臂，不能将第六期旧分数作为新协议基线。

## 失败记录与边界

首轮受限网络运行两题均为 `model_error`，观测 token 为零，不能诊断为算法正确性失败。授权网络后初版两题为 0/2：526 产生正确全精度 Pearson，却漏掉公开的两位小数；214 在重抄公开问题和修复契约时耗尽 token。已加入公开精度提取、运行时绑定原文与完整枚举说明，保留失败，不修改标签。

契约仍含模型提出的语义解释，结构检查不能证明自然语言理解正确，也未验证所有过滤条件、时间区间、单位、分母总体或日期编码。字段别名默认空，不伪造人工映射；声明关系缺失不代表无关联。范围与空结果检查依赖明确契约；交叉查询的一致性不是独立正确性证明。复杂表达式、窗口函数或等价 SQL 可能被保守检查拒绝，需在开发集和反例中继续诊断。

本期不将小样本成绩作总体收益，不宣称达到 BIRD 36/60 或完成生产验收。完整 130 题、历史 61 道正确题保护、未见题、三次配对重复、独立复核仍是未完成门禁。

## 后续修复与评估进度

- 新协议两题真实对照：基线 1/2、候选 1/2，改善 0、回退 0；原正确题 526 保持正确。候选观测 token 36,055，高于基线 12,175，不宣称效率改善。费用继续为 null。
- BIRD 初版六题 0/6，全部为 token_budget。诊断确认重复语义上下文达约 12–24 KB，挤占原预算。修复为全局 4,000 字符上限，并裁剪 SQL-only 原生函数 schema；保持原六步和 token 上限。
- 真实模型改用原生 `analysis_action` 函数调用，保留统一的本地严格校验、原文绑定与单动作限制。测试覆盖原生调用与 JSON 兼容路径，模型预算不随工具绑定扩大。
- 侧账模板保存在 `evals/stage7-label-reviews.json`，仅离线评估读取；比较时按实际轨道及样本过滤，不把另一轨道或队列外案例附到报告。原始 strict 分数始终保持。

- 第二、三轮 BIRD 各 0/6：实际限速包装器未透传 `bind`，因此当时仍是 JSON 路径，不作为原生函数调用收益证据。已增加包装器透传回归，确保原截止时间、共享 pacer 和输出预算不变。原始失败报告继续保留。

- 第四轮原生路径已接通，六题 1/6；仍低于同协议基线 4/6。后续加入响应模式遥测、无效 null 字段删除与函数信封规范化，继续保留全部失败，不选择性替换正式评分。


## 2026-10-20 轮次结果与当时剩余工作

| 验证 | 基线 | 最新对应候选 | 结论 |
|---|---:|---:|---|
| InfiAgent 两题（短路径调整前） | 1/2 | 1/2 | 0 改善、0 回退；候选消耗更高，无收益结论 |
| BIRD 六题（原子 SQL/契约短路径） | 4/6 | 2/6 | 0 改善、2 回退；拒绝资格推广 |

BIRD 最新候选 422、800 正确，1168、41 耗尽 token，116、72 答案不匹配。保护集回退为 116、41。单次小批调试没有统计泛化意义，六题被重复用于开发，不能充当未见题。全 130 题与历史 61 道正确题保护尚未执行，不以初版、修复后不同候选的逐题最好结果拼成分数。

原生调用已确认在实际 paced worker 中生效；函数参数与 JSON 兼容响应仍可能出错，保留 response_mode、错误类型、schema 路径和修复类型供继续定位。复杂题在同预算下仍有规划消耗、重复探索和业务语义错误；不能仅靠继续堆重试解决。

下一增量应优先收敛复杂题的动作结构、减少无目的探索，补充过滤总体/时间/分母的可执行检查，然后重新完成小批配对门禁。通过后再执行全量 130 题及 61 道保护集、独立复核与重复实验。研究配置继续 opt-in，不替代认证指标 Supervisor 或默认第六期基线。

本轮最后全库回归：`773 passed, 1 skipped in 19.26s`；Ruff 检查及 352 个文件格式检查、差异空白检查均通过。


## 最终列名边界修复

`10f156a` 增加数据库作用域列名解析，覆盖所有研究 SQL、交叉检查和集合读取。拒绝 SQLite 双引号字符串兼容性掩盖的未知字段；保留 CTE、输出别名、特殊字符字段及 COUNT(*)。既有候选六条不同成功 SQL 离线重检均通过；该重检不调用模型、不重算或提升原始分数。新增预检之后尚未重跑模型候选，前述 2/6 属于冻结的原子提交候选。

此前提交 `9d5457e` 的 Python 3.11/3.12/3.13 远端 CI 全部通过，运行 [39438503184](https://github.com/GeneralDataAgent/GeneralDataAgent/actions/runs/39438503184)。这项记录明确属于该提交；不冒充后续列名修复的远端验证。

最终列名修复后的全库回归：`774 passed, 1 skipped in 19.65s`。
