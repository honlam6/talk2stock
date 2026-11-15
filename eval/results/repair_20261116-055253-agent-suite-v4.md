# 导入修复 agent 评测 20261116-055253-agent-suite-v4

- 语料：repair-v1-20261116（4 条 · fixture 30 行/需处理 18 行）· 模型：DeepSeek V4.1 Flash · 链路耗时 map 2.9s + repair 57.7s
- **通过率：100.0%** · **gold 命中率：100.0%（10/10）**
- 自动修复 10 · 建议待采纳 0 · 未采纳 0（数字护栏拦截 0）· 取证 8 步 · token 54541
- 误改守门 no_touch_qty_price：✓ 守住 · actionable 精确率：1.0 · 改名带档案绑定：7/7

## 修复明细（applied + suggestions）

- [applied] 行11 item_name → 6号电池（conf 0.95，档案唯一候选：6号电池（id 685），「6号电沲」为形近错字）
- [applied] 行12 item_name → 可乐（conf 0.95，档案唯一候选：可乐（id 674），「可樂」为繁体写法）
- [applied] 行13 item_name → 方便面（conf 0.95，档案唯一候选：方便面（id 676），「方更面」为形近错字）
- [applied] 行14 item_name → 纸箱（conf 0.9，模糊匹配命中：纸箱（id 678），「纸箱儿」为口语化后缀）
- [applied] 行15 item_name → 牛皮纸（conf 0.95，档案唯一候选：牛皮纸（id 679），「牛批纸」为同音错字）
- [applied] 行16 item_name → 肥皂（conf 0.9，档案唯一候选：肥皂（id 680），「肥皀」为形近错字）
- [applied] 行17 item_name → 螺丝（conf 0.85，模糊匹配命中：螺丝（型号M8×20，id 681），「螺丝M8」把规格写进名称）
- [applied] 行18 party → 张记便利店（conf 0.85，历史单据「方便面 44.0」命中张记便利店，单价一致）
- [applied] 行19 party → 永辉超市（conf 0.85，历史单据「可乐 4.0」命中永辉超市，单价一致）
- [applied] 行20 party → 校园超市（conf 0.85，历史单据「电池 3.5」命中校园超市，单价一致）

## 未通过

