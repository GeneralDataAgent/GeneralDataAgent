# 第七期待独立裁定材料（2026-10-22）

状态：待独立裁定。此文件用于离线审阅，不能作为模型工具输入，也不是已有外部审阅或验收结论。严格分数、历史保护成员、标签和分母保持不变。

当前最新完整v62：63→72/130，历史保护56/61，开发资格未通过。后续局部诊断分别保留，不拼接成绩。单题结果与争议解释不加回任何分数。

## 审阅方式

请审阅者先只依据公开问题、说明和固定数据独立写出方法/口径，再查看参考答案与旧候选差异，记录结论、计算步骤、快照哈希、审阅者和时间。可裁定“参考成立”“说明成立但参考需修订”“仍有歧义”；未裁定时保留pending状态。任何经认可的修订须建立新版本评估，不能重写历史成绩。

原始侧账：[stage7-label-reviews.json](../../evals/stage7-label-reviews.json)。旧离线核验：[stage6-magpie-error-audit.json](stage6-magpie-error-audit.json)。新离线原始证据：[stage7-20261021-results.json](stage7-20261021-results.json)，键rounding_discrepancy_412、full_v53、label_review_ledger_20261022。

## bird-mini-dev-sqlite / 219：条件总体与计数口径

公开问题：

What is the percentage of carcinogenic molecules in triple type bonds?

公开补充说明：

label = '+' mean molecules are carcinogenic; triple bond refers to bond_type = '#'; percentage = DIVIDE(SUM(bond_type = '#') * 100, COUNT(bond_id)) as percent where label = '+'

固定资产：`data/stage6/bird/databases/toxicology/toxicology.sqlite`；SHA-256：`35ef27ae6bdfda530e125ed369666ac0abb8bc8c0bcc0ad09407f547ecb61a93`。

已有观察：旧离线SQL重放得到候选0.08075370121130551、参考66.66666666666667。公开提示与参考SQL使用不同条件总体，数值差异本身不能裁定哪种解释符合题意。

待裁定：请先独立写出分子、分母及实体/记录口径，再核对公开提示是否与题意一致。

裁定记录：审阅者____；时间____；结论____；独立步骤/产物____；状态pending。

## bird-mini-dev-sqlite / 672：实体与关联记录

公开问题：

Among the users located in United Kingdom, how many users whose post have a total favorite amount of 4 or more?

公开补充说明：

favorite amount of 4 or more refers to FavoriteCount > = 4; Location = 'United Kingdom';

固定资产：`data/stage6/bird/databases/codebase_community/codebase_community.sqlite`；SHA-256：`92101be6d2a9f6adceea59d38f6d1c556087f9eea1432432a21268cb1348036d`。

已有观察：旧离线重放为DISTINCT实体14、参考关联记录19。关联膨胀不自动意味着参考错误。

待裁定：请裁定请求对象是唯一实体还是每条关联记录，并说明主键与重复记录的含义。

裁定记录：审阅者____；时间____；结论____；独立步骤/产物____；状态pending。

## infiagent-dabench / 300：Pearson系数

公开问题：

1. Is there a correlation between the "nsnps" and "nsamplecov" columns? Calculate the Pearson correlation coefficient (r) to assess the strength of the correlation. Assess the significance of the correlation using a two-tailed test with a significance level (alpha) of 0.05. Report the p-value associated with the correlation test. If the p-value is greater than or equal to 0.05, report that there is no significant correlation.

公开补充说明：

Calculate the Pearson correlation coefficient (r) to assess the strength and direction of the linear relationship between "nsnps" and "nsamplecov". Assess the significance of the correlation using a two-tailed test with a significance level (alpha) of 0.05. Report the p-value associated with the correlation test. Consider the relationship to be correlated if the p-value is less than 0.05. If the p-value is greater than or equal to 0.05, report that there is no significant correlation.
@correlation_coefficient[r_value]
@p_value[p_value]
@correlation[colleration]
where "r_value" is a number between -1 and 1, rounded to two decimal places.
where "p_value" is a number between 0 and 1, rounded to four decimal places.
where "colleration" is a string that can either be "correlated" or "not correlated" based on the conditions specified in the constraints.

固定资产：`data/stage6/infi/da-dev-tables/ts-sc4-wi100000-sl25000-Qrob_Chr05.tree_table.csv`；SHA-256：`3e0540386cf89b4f5e1d7f2760f9aa3885ea21119d3b12e9e5c07056caae7367`。

已有观察：旧固定快照全量2657对有效观测复算r=0.5261625171827068，两位小数0.53；参考系数0.54。现有复算未获外部独立裁定。

待裁定：请核对原始文件版本、缺失/非数值处理和精度规则，并独立复算；不能为贴近参考修改算法。

裁定记录：审阅者____；时间____；结论____；独立步骤/产物____；状态pending。

## bird-mini-dev-sqlite / 931：数值与来源字段类型

公开问题：

What was the fastest lap speed among all drivers in the 2009 Spanish Grand Prix?

公开补充说明：

the fastest lap speed among all refers to max(fastestLapSpeed); Spanish Grand Prix refers to races.name = 'Spanish Grand Prix';

固定资产：`data/stage6/bird/databases/formula_1/formula_1.sqlite`；SHA-256：`79770caf966707e35516fa566e24b40ae515c74ec1ec4631235245645b87b24d`。

已有观察：v53数值结果202.484与历史通过SQL的TEXT结果202.484严格比较不等。数值排序与输出原始类型是不同决策。

待裁定：请裁定该数据集是否要求保留TEXT类型以及严格比较规则是否符合任务语义；不把数值计算改成字典序来模仿标签。

裁定记录：审阅者____；时间____；结论____；独立步骤/产物____；状态pending。

## infiagent-dabench / 412：向上舍入与普通舍入

公开问题：

Create a new feature called "FamilySize" by adding the "SibSp" and "Parch" columns together. What is the mean "FamilySize" for passengers who survived versus passengers who did not survive?

公开补充说明：

Calculate the mean of "FamilySize" separately for the passengers who survived and the passengers who did not survive. "FamilySize" should be an integer value. The mean should be calculated rounding up to two decimal places.
@mean_familysize_survived[answer1] @mean_familysize_did_not_survive[answer2] where "answer1" is the mean "FamilySize" for passengers who survived and "answer2" is the mean "FamilySize" for passengers who did not survive. Both results should be rounded to 2 decimal places.

固定资产：`data/stage6/infi/da-dev-tables/titanic_train.csv`；SHA-256：`4a437fde05fe5264e1701a7387ac6fb75393772ba38bb2c9c566405af5af4bd7`。

已有观察：同一公开说明先说rounding up，后说rounded。非生还549人FamilySize总和485；485/549向上到两位为0.89，half-even为0.88，参考0.88。生还342人总和321，两者均0.94。

待裁定：请裁定rounding up是否指向正无穷舍入，或只是普通舍入措辞，并记录采用的规则和独立复算。

裁定记录：审阅者____；时间____；结论____；独立步骤/产物____；状态pending。


## bird-mini-dev-sqlite / 83：共同总体、年级字段与逐市输出

公开问题：

Of the schools that offers a magnet program serving a grade span of Kindergarten to 8th grade, how many offers Multiple Provision Types? List the number of cities that offers a Kindergarten to 8th grade span and indicate how many schools are there serving such grade span for each city.

公开说明：

Kindergarten to 8th grade refers to K-8; 'Offers a magnet program' means Magnet = 1; Multiple Provision Types refers to `NSLP Provision Status` = 'Multiple Provision Types'

固定数据库SHA-256：`c0903eec662e63068fd1d14403d3d6c1d473287fc10c4356333ea58f878db983`。以下均为离线口径核对，不是模型输入；不裁定参考错误，不改严格成绩。

- `GSoffered_all_magnet`：19个城市结果，35条关联学校记录，逐市学校去重计数之和35。
- `GSoffered_mpt`：1个城市结果，1条关联学校记录，逐市学校去重计数之和1。
- `GSserved_all_magnet`：18个城市结果，34条关联学校记录，逐市学校去重计数之和34。
- `GSserved_mpt`：1个城市结果，1条关联学校记录，逐市学校去重计数之和1。

待裁定：先独立说明年级字段依据、学校总体与后半句是否继承Multiple Provision Types限制，再核对所需输出行和列；现有公开措辞不能由一份数值结果替代裁定。即使裁定支持修订，也必须建立新版本评估，不能重写既有分数和61题保护成员。

裁定记录：审阅者____；时间____；结论____；独立步骤/产物____；状态pending。

83补充范围说明：以上四种计数均使用schools与frpm按CDSCode内连接；不是全部主表学校均已匹配的证明。审阅还须核实未匹配学校是否属于后半句所问总体。
