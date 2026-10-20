# 第六期效果迭代：实测结果

本报告由完整原始证据生成；公开开发集、本地评分协议，不是官方榜单或生产验收。

| 轨道 | 原策略 | 改进版 | 改善/回退题 | 原/改进服务失败 | 双侧配对 p 值 |
|---|---:|---:|---:|---:|---:|
| BIRD Mini-Dev | 20/60 | 21/60 | 4/3 | 0/0 | 1.0000 |
| InfiAgent-DABench | 18/60 | 27/60 | 12/3 | 1/1 | 0.0352 |
| DABstep dev | 1/10 | 2/10 | 1/0 | 0/1 | 服务故障，不作归因 |

p 值为逐轨道的未校正描述性配对检验；小样本、公开数据上的定向开发和多重比较限制其解释。回退题仍单独阻断，不能用净增抵消。

## 效率与证据

| 轨道 | 原/改进上报 token | 原/改进 P95 秒 | 完整计费 |
|---|---:|---:|---|
| BIRD Mini-Dev | 128934/219632 | 14.89/27.10 | 未取得 |
| InfiAgent-DABench | 285679/362810 | 48.09/44.21 | 未取得 |
| DABstep dev | 69762/75073 | 46.18/41.55 | 未取得 |

无模型回放：260 条判定全部一致。
历史工具反馈 14 条重算，字符数 114677 → 31289；不是 token 或费用实测节省。

## 实际产品 Supervisor

| 模式 | 成功 | 数据库未变化 |
|---|---:|---|
| 原工具/原结束条件 | 0/3 | True |
| 原工具/验证后完成 | 2/3 | True |
| 请求参数绑定工具/验证后完成 | 0/3 | True |

Supervisor 为三个 TLC 认证指标的小规模联调，成功要求角色、请求、结果及证据哈希同时通过；不替代新公开数据源的产品适配或独立上线验收。

## 发布门禁

`production_accepted=false`。当前阻断：

- bird-mini-dev-sqlite:quality_below_provisional_target
- bird-mini-dev-sqlite:cost_not_verified
- bird-mini-dev-sqlite:paired_regression
- infiagent-dabench:quality_below_provisional_target
- infiagent-dabench:cost_not_verified
- infiagent-dabench:paired_regression
- dabstep-dev:quality_below_provisional_target
- dabstep-dev:model_service_unreliable
- dabstep-dev:cost_not_verified
- repeat_stability_not_qualified
- independent_human_review_not_supplied
- production_supervisor_not_evaluated
- shadow_and_canary_not_qualified

完整机器证据：[JSON](stage6-iteration-v7-results.json)。原始报告和哈希保存在机器证据内；未修改默认模型、账户或外部系统。
