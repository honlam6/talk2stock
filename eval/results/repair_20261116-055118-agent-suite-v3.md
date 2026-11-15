# 导入修复 agent 评测 20261116-055118-agent-suite-v3

- 语料：repair-v1-20261116（4 条 · fixture 30 行/需处理 18 行）· 模型：DeepSeek V4.1 Flash · 链路耗时 map 3.1s + repair 340.8s
- **通过率：75.0%** · **gold 命中率：70.0%（7/10）**
- 自动修复 7 · 建议待采纳 0 · 未采纳 0（数字护栏拦截 0）· 取证 7 步 · token 46781
- 误改守门 no_touch_qty_price：✓ 守住 · actionable 精确率：1.0 · 改名带档案绑定：7/7

## 修复明细（applied + suggestions）

- [applied] 行11 item_name → 6号电池（conf 0.95，档案唯一候选 6号电池（单位节）与「6号电沲」形近同单位）
- [applied] 行12 item_name → 可乐（conf 0.95，繁体「可樂」对应档案 可乐（单位瓶））
- [applied] 行13 item_name → 方便面（conf 0.95，「方更面」为形近错写，档案 方便面 单位箱一致）
- [applied] 行14 item_name → 纸箱（conf 0.9，find_material 模糊命中档案 纸箱（单位个））
- [applied] 行15 item_name → 牛皮纸（conf 0.9，「牛批纸」形近，档案 牛皮纸 单位卷一致）
- [applied] 行16 item_name → 肥皂（conf 0.95，「肥皀」为异体字，档案 肥皂 单位块一致）
- [applied] 行17 item_name → 螺丝（conf 0.9，find_material 模糊命中档案 螺丝（型号 M8×20），与「螺丝M8」对应）

## 未通过

- c2：hit_min｜命中：无
