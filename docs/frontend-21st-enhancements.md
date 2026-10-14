# 前端增强批次 · 21st.dev 交互模式吸收

日期：2026-10-15
范围：`app/web/`（style.css / common.js / 五个页面 html）
回滚：见 [backups/web-pre-21st-20261014/ROLLBACK.md](../../backups/web-pre-21st-20261014/ROLLBACK.md)

## 背景与动机

对照 21st.dev/community/components 的组件分类清单（Heroes / Backgrounds / Buttons / Dialogs /
Toasts / Tables / Sidebars / Menus / Empty State / Progress），逐项比对本项目前端缺哪些**交互模式**。
结论：视觉语言「墨金账房」（墨绿×鎏金、衬线、tabular-nums）是强资产，**不换皮，只补模式**。
Shader/3D/ASCII 等炫技类明确不做（ERP 是生产力工具，且会破坏零 CDN 承诺）。

## 改动清单

### 1. 骨架屏（最高优先）
- 新增 `.skel` / `.skel-*` 系列 + `@keyframes skel-shimmer`，与真实内容同构。
- 首页 KPI 顶条（5 槽）、最近录单列表、看板全部 9 个区块、问一问等待态、设置页徽标。
- 替代原「加载中…」纯文字（原 7 处）。看板刷新时保留旧数据不闪空，仅首次铺骨架。

### 2. 通知组件替换 Toast
- `common.js` 的 `toast()` 重写：右下角堆叠、三态（ok/warn/bad）、状态图标、手动关闭、
  4 条上限、`role=status` + `aria-live=polite`。
- **顺带修掉隐性 bug**：旧实现颜色硬编码 hex（#b5532f 等），绕过设计系统令牌。
- 旧调用签名 `toast(msg, kind)` 完全兼容，五个页面零改动。

### 3. Command Palette（⌘K / Ctrl+K）
- 全站 `Ctrl/⌘+K` 唤起；侧栏新增「命令面板」入口；首次访问右下角轻提示（sessionStorage 去重）。
- 模糊搜索、分组分区、↑↓ 导航、Enter 执行、Esc 关闭、焦点陷阱。
- **只做导航/填入，绝不触发解析或提交**（遵守用户「少一步」偏好）。

### 4. Dialog 焦点管理
- 新增 `trapFocus(container, onEscape)`：Tab 在容器内循环、Esc 统一走 close、打开即聚焦关闭按钮。
- 接入 `showPreview`（预览确认弹层）。此前 Tab 会跑到背后页面，读屏体验断裂。

### 5. Feature 卡渐进揭示
- `.quick-card` hover/focus 时 `.qc-peek` 展开一行示例（grid-template-rows 0fr→1fr，不跳布局）+ 光标闪烁。

### 6. 跑马灯
- `.marquee` / `@keyframes marquee-scroll`：两份内容首尾相接 + `translateX(-50%)` 无缝滚动。
- 首页展示真实今日采销/差额/欠款/预警 + 系统信任点。hover 暂停。

### 7. Empty State 统一组件
- `.empty`（图标 + 标题 + 描述 + 主操作 + 示例 chips）替换各页零散空文案。
- 覆盖：首页最近录单、看板趋势/进销榜/库存/最近单据、问一问引导、录单会话流。

### 8. 表格排序 + 吸顶表头
- `dtable th.sortable` 样式（悬停/排序中/升降序箭头）。
- 看板「最近单据」点表头排序（7 列，数字列按数值、文本列按 localeCompare zh-CN）。
- `.tbl-sticky` 预留吸顶表头样式。

### 9. 无障碍与响应式加固
- 全局 `:focus-visible` 焦点环（此前仅输入框有）。
- `prefers-reduced-motion` 增加：骨架 shimmer、marquee 滚动、mic-pulse。
- **修移动端 390px 横向溢出 120px**：`.two-col`/`.db-grid`/`.bill-grid` 子项 `min-width:0`
  （Grid item 默认 min-width:auto，长单号/长原话顶破轨道）。
- `.marquee` 加 `max-width:100%` + `contain: layout paint style`。
- `html`/`body` 加 `overflow-x: hidden` 兜底。
- 收拢 130+ 处散落 `style="margin-top:Npx"` 为 `.mt-1`~`.mt-5` 工具类。
- 命令面板键盘导航 bug：`render()` 无条件 `sel=0` 导致 ↑↓ 失效，改为 `render(resetSel)`。

## 实测记录

| 项 | 结果 |
| --- | --- |
| `verify-local.sh` | 12/12 通过（含 Talk2Stock→ERP→LLM 端到端） |
| `app/selfcheck.py` | 8/8 全绿 |
| 五页面 HTTP | 全部 200 |
| 内联脚本语法 | 5/5 通过 `node --check` |
| ⌘K 打开/搜索/导航/Enter 跳转/Esc | 通过（搜索「看板」精确命中） |
| 焦点陷阱 | 尾部 Tab 绕回首部、首部 Shift+Tab 跳尾部、Esc 关闭，通过 |
| Toast 三态/图标/令牌色/关闭/aria-live | 通过 |
| 骨架→真实数据 | 首页/问一问均通过；问一问返回「可乐还有 102 瓶…」 |
| 表格排序 | 点「金额」正确降序排列，表头 `sorted desc` |
| 390px 六页横向溢出 | 全部 `scrollWidth == 视口宽`，无页面级溢出 |
| 回滚演练 | 备份覆盖→旧版可用→新版恢复，往返通过 |

## 复现方式

    cd ~/Desktop/jshERP-suite
    colima start && cd jshERP-latest/deploy && docker compose up -d --no-build
    nohup python3 app/server.py &      # 8090
    bash verify-local.sh

浏览：http://localhost:8090/ （⌘K 试命令面板）

## 未做 / 后续候选

- 桌面端 `.marquee` 内容目前是首页独有，看板页可复用。
- `.tbl-sticky` 样式已备，看板大表可开启。
- SSE 流式问答仍按既定结论暂不做。
