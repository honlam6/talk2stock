# 评测结果 20261012-135109-workbuddy-promptv3-rerun

- 语料：v1-20261010（60 条）· 模型：global:deepseek-v4.1-flash · prompt：v3-20261011/258de44c
- **整单关键字段全对率：96.7%**（目标 ≥85%）
- 幻觉字段：0 处 / 影响 0 条（目标 0）
- 解析报错：0 条
- 延迟：均值 3.58s · p95 6.54s（目标 p95≤3s）
- 对抗样本上标 low 置信度：8/12

| 字段 | 对/总 | 准确率 |
|---|---|---|
| biz | 59/60 | 98.3% |
| party | 59/60 | 98.3% |
| item_name | 60/60 | 100.0% |
| item_qty | 60/60 | 100.0% |
| item_unit | 60/60 | 100.0% |
| item_price | 60/60 | 100.0% |
| depot | 60/60 | 100.0% |
| date | 60/60 | 100.0% |
| pay_amount | 60/60 | 100.0% |

## 未通过用例

- a03（adversarial）：party
- a12（adversarial）：biz
