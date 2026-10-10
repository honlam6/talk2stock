# 组员协作指南（Talk2Stock）

> main 分支已开启保护：**任何人（包括管理员）都不能直接 push main，一律走分支 + PR，由组长整合。**

## 日常流程

```bash
# 1) 同步最新 main
git checkout main && git pull origin main

# 2) 从 main 切自己的功能分支（命名：feat/ 或 fix/ 或 docs/ + 短横线描述）
git checkout -b feat/voice-input

# 3) 开发、小步提交
git add <文件>
git commit -m "feat(模块): 做了什么（为什么、怎么验证的）"

# 4) 推送自己的分支
git push -u origin feat/voice-input

# 5) 在 GitHub 上开 Pull Request（base: main ← compare: 你的分支）
#    描述里写清：改了什么 / 为什么 / 怎么验证的（附截图或测试输出）
# 6) 组长 review 后合并。合并后删远程分支：
git push origin --delete feat/voice-input
```

## 规矩

1. **密钥永远不进仓库**：API Key、ERP 密码、真实网关地址只写 `app/.env`（已 gitignore）。
   `config.json` 里的模型地址是占位符；自己配置时用环境变量 `T2S_KEY_<档案>` / `T2S_BASE_<档案>` 覆盖。
2. **不动 `jshERP-latest/` 上游源码**：我们只做加法，全部通过官方 REST API。
3. **改提示词必须**：更新 `app/core/llm.py` 的 `PROMPT_VERSION` + 变更记录注释，并跑一遍
   `python3 ../eval/run.py --tag <你的名字>-<改动>`，把结果文件一起提交——这就是课程的"迭代留痕"。
4. **改写入逻辑必须**：保持「无确认不写入」不变量（`core/gate.py`），并在 PR 里说明怎么验证的。
5. 提交信息用中文，格式 `<类型>(<范围>): <做了什么>`，类型：feat / fix / docs / eval / refactor。

## 本地跑起来

见 README「快速开始」：`bash start-all.sh` 一键拉起；`bash verify-local.sh` 12 项自检；
`cd app && python3 selfcheck.py` 应用层自检。有问题先看 `docs/` 里的部署验证报告和 API 清单。

## 分工与领任务

看 Project Plan 的角色表；认领新任务先在群里说，避免撞车。
