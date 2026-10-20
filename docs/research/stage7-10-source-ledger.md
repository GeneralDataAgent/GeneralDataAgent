# 第七至十期优化规划：参考源码台账

研究日期：2026-10-20。两个用户指定 GitHub 项目的远端 main 均已实时核对；源码只读检查，未运行上游代码。完整文件 SHA256 见[错题审计](../validation/stage6-magpie-error-audit.json)的 references 字段。

## OpenAI-Data-Analytics

固定版本：`52ae75ef7387bef9de52b8cbceea91cde7cf4e51`。

本机该仓库 README 有未提交修改，本轮排除 README 的工作树修改；以下 Skill 文件逐字节核对为固定提交内容。项目是用户指定的工作流参考，不把它表述成已复现的官方运行服务。

| 源码 | 已核对的方法 | 规划用途 |
|---|---|---|
| [Create Data Context](https://github.com/ibillxia/OpenAI-Data-Analytics/blob/52ae75ef7387bef9de52b8cbceea91cde7cf4e51/plugins/data-analytics/skills/create-data-context/SKILL.md) | 来源驱动的语义上下文、定义及证据 | 第七期语义契约、字段/口径映射 |
| [Analyze Data Quality](https://github.com/ibillxia/OpenAI-Data-Analytics/blob/52ae75ef7387bef9de52b8cbceea91cde7cf4e51/plugins/data-analytics/skills/analyze-data-quality/SKILL.md) | 粒度、缺失、重复、关联覆盖、分布检查 | 第七至八期可执行质量检查 |
| [Validate Data](https://github.com/ibillxia/OpenAI-Data-Analytics/blob/52ae75ef7387bef9de52b8cbceea91cde7cf4e51/plugins/data-analytics/skills/validate-data/SKILL.md) | 方法、分母、聚合、独立复算和结论审查 | 第七期语义验证、第十期验收 |
| [Product Business Analysis](https://github.com/ibillxia/OpenAI-Data-Analytics/blob/52ae75ef7387bef9de52b8cbceea91cde7cf4e51/plugins/data-analytics/skills/product-business-analysis/SKILL.md) | 决策问题、分析框架、定量证据、验证交接 | 问题规划与结果契约 |
| [Jupyter Notebook](https://github.com/ibillxia/OpenAI-Data-Analytics/blob/52ae75ef7387bef9de52b8cbceea91cde7cf4e51/plugins/data-analytics/skills/jupyter-notebook/SKILL.md) | 来源、方法、执行与重跑要求 | 第八期可复现实验产物 |
| [Gather Business Context](https://github.com/ibillxia/OpenAI-Data-Analytics/blob/52ae75ef7387bef9de52b8cbceea91cde7cf4e51/plugins/data-analytics/skills/gather-business-context/SKILL.md) | 来源权威性、背景与冲突 | 第九期规则来源 |
| [Metric Diagnostics](https://github.com/ibillxia/OpenAI-Data-Analytics/blob/52ae75ef7387bef9de52b8cbceea91cde7cf4e51/plugins/data-analytics/skills/metric-diagnostics/SKILL.md) | 指标变化、口径与机制诊断 | 第九期业务解释 |

执行边界：该版本 `mcp/server.cjs`、`create-data-context/scripts/validate_data_context_contract.py` 均为零字节；不能据工作流描述推断已经提供对应工具。规划参考其分析方法，不执行其面向其他产品的安装、发布或资料访问指令。

## AutoKaggle

用户指定 fork 的固定版本：`cf151dd8a2d44ec6692b956bad8bca59f9e0d7c1`。

本机 `/Users/billxia/Program/Github/AutoKaggle` 指向另一上游且存在未跟踪扩展；它的 HEAD 为 `2640d756…`，不能当作用户指定 fork 的当前版本。本轮另取指定 fork 的只读裸仓库及所需源文件，未读取 api_key.txt、未执行生成代码，也未把本机未提交扩展纳入证据。

| 源码 | 已核对的实现 | 规划用途与限制 |
|---|---|---|
| [config.json](https://github.com/ibillxia/AutoKaggle/blob/cf151dd8a2d44ec6692b956bad8bca59f9e0d7c1/multi_agents/config.json) | 背景、EDA、清洗、特征、建模等六阶段，以及角色、阶段测试和工具映射 | 第八至十期任务分解；不将所有任务强制成竞赛流水线 |
| [sop.py](https://github.com/ibillxia/AutoKaggle/blob/cf151dd8a2d44ec6692b956bad8bca59f9e0d7c1/multi_agents/sop.py) | 阶段流转、审查分数驱动重做，默认 max_iterations=3 | 有上限的阶段修复；LLM 分数不能代替事实验收 |
| [planner.py](https://github.com/ibillxia/AutoKaggle/blob/cf151dd8a2d44ec6692b956bad8bca59f9e0d7c1/multi_agents/agents/planner.py) | 结合背景、数据预览和历史反馈形成计划 | 生成结构化计划，仍需工具/预算预检 |
| [developer.py](https://github.com/ibillxia/AutoKaggle/blob/cf151dd8a2d44ec6692b956bad8bca59f9e0d7c1/multi_agents/agents/developer.py) | 生成代码、子进程执行、错误调试、阶段测试后修复 | 借鉴循环；不搬用宿主子进程作为生产沙箱 |
| [reviewer.py](https://github.com/ibillxia/AutoKaggle/blob/cf151dd8a2d44ec6692b956bad8bca59f9e0d7c1/multi_agents/agents/reviewer.py) | 对计划、开发结果作模型评审 | 评审建议与独立断言分离 |
| [unit_test.py](https://github.com/ibillxia/AutoKaggle/blob/cf151dd8a2d44ec6692b956bad8bca59f9e0d7c1/multi_agents/tools/unit_test.py) | 文件、列、行、缺失及提交等检查 | 数据/产物完整性检查不等同答案正确；还需方法和数值断言 |
| [ml_tools.py](https://github.com/ibillxia/AutoKaggle/blob/cf151dd8a2d44ec6692b956bad8bca59f9e0d7c1/multi_agents/tools/ml_tools.py) | 缺失处理、异常值、编码、特征与模型选择函数；模型工具有默认 80/20、seed 42 切分 | 参考工具分层；题目要求应优先，不能盲用默认切分或将训练集外数据用于拟合 |

本轮没有复现 AutoKaggle 论文或 README 的性能结果，不将其作者报告的成绩外推为 GeneralDataAgent 的潜在收益。未来若导入代码须按原文件许可处理；其 LICENSE.md 另有 evaluation 模块许可声明，本规划不移入该评估模块。

## 本项目真实边界与独立方法参考

- 当前 `evaluation/stage_six_analysis.py` 的固定统计入口只支持 describe、Pearson、单变量描述回归。describe 已包含 sample_std；没有多变量预测留出评估或任意派生列计算。
- 当前 `data_science/_worker.py` 的回归是均值基线；`workbench.py`、`sandbox.py` 已有实验快照、配额、OCI 和验证治理，应复用而不是重新引入任意代码执行。
- DABstep 本地固定 manual 规定了金额公式、自然月及 null 通配含义；业务规则工具应绑定该版本资料，再用独立样例验证。
- [scikit-learn：常见错误与数据泄漏](https://scikit-learn.org/stable/common_pitfalls.html)：用于训练/测试隔离及 Pipeline 设计依据。正式依赖版本须在实现时锁定。
- [SciPy：Pearson 相关与 p 值](https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats.pearsonr.html)：用于统计方法契约；不能把系数、显著性和因果解释混为一谈。

复盘使用参考 SQL/答案仅为离线审计；这些内容必须与运行时 Skill、RAG 和记忆隔离。所有优化收益仍需后续同协议、同模型、同预算的真实候选实验确认。
