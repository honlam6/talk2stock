# 导入修复 agent 评测 20261116-053829-agent-suite-final

- 语料：repair-v1-20261116（4 条 · fixture 30 行/需处理 18 行）· 模型：DeepSeek V4.1 Flash · 链路耗时 map 3.0s + repair 65.0s
- **通过率：75.0%** · **gold 命中率：10.0%（1/10）**
- 自动修复 1 · 建议待采纳 0 · 未采纳 0（数字护栏拦截 0）· 取证 2 步 · token 15548
- 误改守门 no_touch_qty_price：✓ 守住 · actionable 精确率：1.0 · 改名带档案绑定：1/1

## 修复明细（applied + suggestions）

- [applied] 行13 item_name → 方便面（conf 0.85，find_material 查询“方便面”返回 exact 档案 id 664；表中“方更面”与“方便面”同价 44.0、同单位“箱”，判断为错别字。）

## 未通过

- c2：hit_min｜命中：无
