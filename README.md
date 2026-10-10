# Talk2Stock · 自然语言进销存助手

> 给企业 ERP 套一层「说人话就开单」的壳：**一句话 → 系统生成单据草稿 → 人工确认 → 真实写入 ERP**。
> 底座是真实开源 ERP（[jshERP · 管伊佳 ERP](https://gitee.com/jishenghua/JSH_ERP)），不是另搭的玩具库。

很多人抱怨「ERP 录单太慢」。我们把它拆成两段真正昂贵的时间，并只针对这两段做优化：

- **档案补齐成本**：新供应商 / 新商品 / 新规格，录单前必须先去「基础资料」建档，再回来重头填。小公司里这件事天天发生，且没有上限。
- **翻译成本**：「老张今天送了两箱水，12 块一瓶，放前面那个仓」是一句话，不是表格。谁把它翻译成供应商、商品、数量×单位、单价、仓库、日期——现在全靠人脑加鼠标。

Talk2Stock 让录单员**用一句人话把业务说清楚**，系统先给出草稿、标出「哪些档案要新建」，人确认后才真正落库。写错的成本被拦在写入之前，录入的点击次数从约 25 次降到约 4 次。

---

## 目录

- [核心特性](#核心特性)
- [三个入口](#三个入口)
- [系统架构](#系统架构)
- [目录结构](#目录结构)
- [快速开始](#快速开始)
- [配置你的 AI 解析模型](#配置你的-ai-解析模型)
- [环境变量](#环境变量)
- [端到端验证](#端到端验证)
- [常见问题](#常见问题)
- [安全说明](#安全说明)
- [上游与许可](#上游与许可)
- [路线图](#路线图)

---

## 核心特性

| 能力 | 说明 |
|---|---|
| 一句话建单 | 自然语言解析出 6 类业务（采购入库 / 销售出库 / 采购退货 / 销售退货 / 其他入出库）的单据草稿 |
| 主数据自动补齐 | 报价里出现的陌生供应商 / 商品 / 仓库，预览时列清「将新建」，确认后一次补齐，无需先建档 |
| 确认卡闸门 | 每个字段标明来历（原话抽的 / 匹配到已有档案 / 系统默认 / 建议新建），确认后才写入 ERP |
| 只做加法，可回退 | **不改 jshERP 源码**，全部通过官方 REST API 写入，原系统随时可当后备 |
| 多模型档案 | 任意 OpenAI 兼容接口都能配（OpenAI / DeepSeek / 通义 / Kimi / 本地 Ollama…），页面即可配置、切换、测试 |
| 密钥不进仓库 | 密钥只存 `app/.env`（已 `.gitignore`），`config.json` 永远不含密钥 |

---

## 三个入口

三个入口共用同一套后端（`app/core/draft.py` 的 `resolve / commit`），预览确认层行为完全一致。

| 页面 | 地址 | 适合谁 | 依赖 |
|---|---|---|---|
| 表单快速录入 | `http://localhost:8090/form` | 日常录单员 | 无（纯预设匹配，不需要 AI） |
| AI 智能录入 | `http://localhost:8090/ai` | 想省事的人 / 手机端 | 需配置一个 LLM 模型 |
| 经营看板 | `http://localhost:8090/dashboard` | 老板（只读） | 无 |

外加原版 ERP 本身：`http://localhost:8080`（报表、权限、盘点、财务等高级功能仍回原系统）。

---

## 系统架构

三层职责切开，写入闸门由我们自己的代码把关：

```
┌─────────────────────────────────────────────────────────┐
│  入口层  app/web/*.html + app/server.py（:8090）          │
│  一句话输入 / 表单 / 看板，输出「确认卡」                   │
└───────────────┬─────────────────────────────────────────┘
                │  /api/parse  /api/resolve  /api/commit
┌───────────────▼─────────────────────────────────────────┐
│  模型层  app/core/llm.py                                  │
│  意图分类 → 口语解析（数量/单位/价格/日期）→ 强制 JSON 草稿   │
└───────────────┬─────────────────────────────────────────┘
                │  draft：{biz, party, items[], depot, ...}
┌───────────────▼─────────────────────────────────────────┐
│  落库层  app/core/draft.py + app/core/erp.py              │
│  resolve()：只读匹配主数据，返回「将新建」清单（不写库）      │
│  commit()：人工确认后，补主数据 + 建单，全部走官方接口        │
└───────────────┬─────────────────────────────────────────┘
                │  jshERP REST API（仅 HTTP，绝不直连数据库）
┌───────────────▼─────────────────────────────────────────┐
│  底座 ERP  jshERP（MySQL + Redis + Spring Boot + Vue）     │
└─────────────────────────────────────────────────────────┘
```

---

## 目录结构

```
.
├── app/                     # ★ 自研：AI 录入壳（Python 3 纯标准库，零第三方依赖）
│   ├── server.py            #   HTTP 服务（默认 0.0.0.0:8090）
│   ├── selfcheck.py         #   应用层自检：直连 core 模块验逻辑
│   ├── core/
│   │   ├── config.py        #   配置与多模型档案（密钥只从 .env 读）
│   │   ├── erp.py           #   jshERP REST 客户端（登录/主数据/建单/附件）
│   │   ├── llm.py           #   LLM 解析层：口语 → 严格 JSON 草稿
│   │   ├── draft.py         #   主数据匹配 + 建单（两段式 resolve/commit）
│   │   └── report.py        #   经营看板数据
│   ├── web/                 #   前端页面（home / ai / form / dashboard）
│   ├── config.json          #   非敏感配置（不含任何密钥）
│   └── .env.example         #   密钥模板；复制成 .env 后填真实值（.env 已被忽略）
├── jshERP-latest/           # 上游 jshERP 源码副本（Apache-2.0，含 deploy/ 编排）
│   └── deploy/              #   docker-compose.yml + Dockerfile + nginx.conf
├── docs/                    # 设计 / 部署验证 / 接口清单文档
├── start-all.sh             # 一键拉起：容器 + ERP 四容器 + AI 壳
├── verify-local.sh          # 端到端验证：运行时 → 容器 → HTTP → 登录 → AI 链路
└── LICENSE                  # MIT（仅覆盖自研部分）
```

---

## 快速开始

### 前置条件

- **Docker + Docker Compose**：macOS 可用 [colima](https://github.com/abiosoft/colima) 或 Docker Desktop；Linux / Windows(WSL) 用 Docker Engine 即可。
- **Python 3.8+**：AI 壳只用标准库，不需要 `pip install` 任何东西。
- 首次启动会本地构建 ERP 后端（Maven）与前端（Node），**约 5–15 分钟**；之后启动是秒级。

### 方式一：一键启动（推荐）

```bash
bash start-all.sh
```

脚本依次完成：① 确保容器运行时在线（若无 colima 但已有 Docker，会自动跳过）→ ② 起 ERP 四个容器（mysql / redis / backend / web）→ ③ 起 AI 壳（`:8090`）。

### 方式二：手动分步

```bash
# 1) 起 ERP（首次会构建镜像）
cd jshERP-latest/deploy
docker compose up -d
docker compose ps          # 四个容器都应为 running，mysql/redis 为 healthy

# 2) 准备 AI 壳配置并启动
cd ../..
cp app/.env.example app/.env      # 首次执行
python3 app/server.py             # 默认 0.0.0.0:8090，Ctrl-C 停止
```

### 访问入口

| 服务 | 地址 |
|---|---|
| AI 录入壳（本项目） | http://localhost:8090/ |
| ERP 前端 | http://localhost:8080/ |
| ERP 后端接口文档 (Swagger) | http://localhost:9999/jshERP-boot/doc.html |

默认账号：`jsh / 123456`（租户账号）、`admin / 123456`（超管，租户 0）。

> 演示数据：ERP 初始化 SQL 自带少量供应商/商品/仓库。AI 录入时如果库里没有对应的供应商或商品，确认后会**自动新建**，不用先去基础资料建档。

---

## 配置你的 AI 解析模型

AI 录入需要**任意一个 OpenAI 兼容接口**。两种配置方式，任选其一：

### 方式 A：在页面里配置（推荐，无需改文件）

1. 打开 http://localhost:8090/ai
2. 点右上角 **「⚙ 模型设置」**
3. 展开 **「＋ 添加 / 编辑模型」**，填：

   | 字段 | 说明 | 示例 |
   |---|---|---|
   | 显示名 | 自己认的名字 | `DeepSeek` |
   | Base URL | 接口地址，**带 `/v1`** | `https://api.deepseek.com/v1` |
   | API Key | 密钥 | `sk-…` |
   | 模型名 | 该接口的 model id | `deepseek-chat` |
   | temperature | 抽取任务建议 0～0.3 | `0.2` |

4. 点 **「测试连接」** 确认连通 → **「保存」** → 在列表里点 **「启用」**

保存后，这个 Key 会写入 `app/.env` 的 `T2S_KEY_<档案名大写>`，**重启不丢**，且 `config.json` 始终不含密钥。

常见服务对照：

| 服务 | Base URL | 模型名示例 |
|---|---|---|
| DeepSeek | `https://api.deepseek.com/v1` | `deepseek-chat` |
| OpenAI | `https://api.openai.com/v1` | `gpt-4o-mini` |
| 通义千问（阿里云百炼） | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-plus` |
| Moonshot (Kimi) | `https://api.moonshot.cn/v1` | `moonshot-v1-8k` |
| 本地 Ollama | `http://localhost:11434/v1` | `qwen2.5:7b`（Key 随便填） |
| 任意自建网关 | 你的地址 + `/v1` | 你的模型名 |

### 方式 B：用环境变量 / `.env`

```bash
cp app/.env.example app/.env
# 编辑 app/.env，填 T2S_KEY_DEEPSEEK=sk-xxxx 等
```

`app/config.json` 里已经预置了 `deepseek / openai / qwen` 三个档案的 `base_url` 和 `model`（**不含密钥**）；只要在 `.env` 里补上对应密钥，重启即可生效。也可以完全不管 `.env`，直接用**方式 A** 在页面里配。

> 解析层对推理类端点的兼容：优先用流式 + `response_format=json_object`，失败自动降级（去掉 `response_format`、再退非流式）；若网关会丢弃 `system` 角色，可在档案里打开 `merge_system`（见 `app/core/llm.py`）。

---

## 环境变量

配置来源优先级：`DEFAULTS`（代码内）→ `app/config.json` → 环境变量 / `app/.env`（最高）。

| 变量 | 作用 | 默认 |
|---|---|---|
| `T2S_ERP_BASE` | ERP 后端地址 | `http://localhost:9999/jshERP-boot` |
| `T2S_ERP_USER` | ERP 登录账号 | `jsh` |
| `T2S_ERP_PASS` | ERP 登录密码（明文，服务端会做 MD5） | 空（需填） |
| `T2S_KEY_<档案名大写>` | 某个模型档案的 API Key，如 `T2S_KEY_DEEPSEEK` | 空 |
| `T2S_REQUIRE_AUTH` | 设为 `1` 后所有 `/api` 接口需带访问口令 | `0`（关闭） |
| `T2S_DEMO_PASS` | 访问口令 | `talk2stock` |
| `PORT` / `HOST` | AI 壳监听端口 / 地址 | `8090` / `0.0.0.0` |

---

## 端到端验证

```bash
bash start-all.sh          # 确保整套在跑
bash verify-local.sh       # 部署层 + HTTP 端到端，共 12 项
cd app && python3 selfcheck.py   # 应用层自检（直连 core 模块）
```

`verify-local.sh` 会依次检查：容器运行时 → 四个 ERP 容器健康 → 三个 HTTP 入口 → ERP 登录（MD5）→ AI 壳读主数据 → 自然语言解析 → 只读预览（不写库）。最后一行给出「通过 / 告警 / 失败」汇总。

> 第 6 项依赖外部模型，失败记作**告警**而非失败——没配模型时属正常。

---

## 常见问题

**Q：`:8090` 页面打不开 / 连接后无响应（浏览器转圈、curl 返回 `000`）？**
多为「进程存活但僵死」——端口在 `LISTEN` 却不返回数据。`bash start-all.sh` 会检测并清理占用 8090 的旧进程后重启；也可手动：
```bash
lsof -nP -iTCP:8090 -sTCP:LISTEN -t | xargs kill -9
python3 app/server.py
```

**Q：AI 明明写库成功了，但 ERP 里按「今天」查不到单据？**
时区问题。`docker-compose.yml` 已给 `erp-backend` 设了 `TZ: Asia/Shanghai`；若你改过编排或用了旧容器，确认 backend 容器时区为 CST：
```bash
docker exec erp-backend date        # 应为 CST 而非 UTC
```

**Q：ERP 容器起不来 / 一直 build？**
首次构建后端（Maven 拉依赖）和前端（npm）确实慢，属正常。若失败，先看日志：
```bash
cd jshERP-latest/deploy && docker compose logs --tail=80 erp-backend
```

**Q：模型报 `response_format` 不支持 / 400？**
`app/core/llm.py` 已内置降级：会自动去掉 `response_format` 重试、再退非流式。若仍失败，多为 Base URL 少了 `/v1` 或模型名不对。

**Q：端口 8080 / 9999 / 8090 被占用？**
`8080`、`9999` 在 `jshERP-latest/deploy/docker-compose.yml` 里改；`8090` 用 `PORT=8099 python3 app/server.py` 换端口。

**Q：Windows 能用吗？**
`start-all.sh` / `verify-local.sh` 是 bash 脚本，在 WSL 或 Git Bash 下运行即可；AI 壳本身是跨平台 Python。

---

## 安全说明

- **密钥管理**：所有 API Key、ERP 密码只存 `app/.env`（已 `.gitignore`）。页面里配置的 Key 也会落到这里，`config.json` 永远不含密钥。**不要把填好密钥的 `.env` 提交或截图外发。**
- **写库闸门**：写入路径是「自然语言 → 草稿 → 确认卡 → 人工点确认 → commit」，UI 上不存在静默写入；但请注意，当前 `/api/commit` 只在 UI 层要求先确认，**服务端未强制校验一次性确认令牌**（见路线图）。
- **接口鉴权**：`/api/*` **默认不鉴权**（方便本地开发）。若把 8090 暴露到公网，请务必：
  - 设 `T2S_REQUIRE_AUTH=1` 并改掉默认 `T2S_DEMO_PASS`；或
  - 放到反向代理后用 Basic Auth / IP 白名单保护；或
  - 直接只在内网/本机使用。
- **ERP 凭据**：默认 `jsh / 123456` 仅供本地演示，生产环境请改强密码并轮换。

---

## 上游与许可

- **Talk2Stock 自研部分**（`app/`、`docs/`、`start-all.sh`、`verify-local.sh` 等）：**MIT**，见根目录 [`LICENSE`](./LICENSE)。
- **`jshERP-latest/`**：第三方开源项目 **jshERP（管伊佳 ERP，原名华夏 ERP）** 的源码副本，遵循其自身 **Apache License 2.0**（见 `jshERP-latest/LICENSE`）。上游快照来源：`https://gitcode.com/hualiruanjian/JSH_ERP`（快照 commit `de59390`）。
  > 注：网络上对 jshERP 的协议存在 Apache-2.0 / GPL-3.0 两种说法。**以实际收录副本的 `jshERP-latest/LICENSE`（Apache-2.0）为准。** 本项目未收录另一份旧部署包（其 LICENSE 为 GPL-3.0），以避免协议口径混淆。
- Talk2Stock 仅通过 jshERP 官方 REST API 交互，**未修改上游源码**。

---

## 路线图

按「对课程/答辩最有说服力」的性价比排序（详见 `docs/项目合规评估与改进建议.md`）：

1. **评测资产**：60 条冻结语料 + `eval/run.py` 打分脚本 + 三组基线（原生手工录入 / 去掉确认闸门 / 去掉主数据检索）。
2. **服务端确认闸门**：`/api/resolve` 签发一次性 `confirm_token`，`/api/commit` 校验——把「无确认不写入」从 UI 约定升级为服务端不变量。
3. **审计日志**：每次 `commit()` 追加一条 `app/data/audit.jsonl`（原话、模型名、提示词版本 hash、draft 与最终写入的 diff、时间戳）。
4. **只读 agent 问答**：新增 `/api/ask`，用工具调用回答库存/补货/动销/欠款。
5. **主数据候选匹配**：多候选返回 top3 让用户点选 + 别名表。
6. **单位换算（箱↔瓶）与含税/未税口径**：解析层与评测口径同步归一。

---

## 相关文档

- `docs/易用性改造方案.md` — 原系统痛点实测、已实现能力、分阶段改进清单
- `docs/本地部署验证报告.md` — 端到端验证记录与已修复缺陷（含时区、僵死进程）
- `docs/API端点全清单.md` — jshERP 接口实测清单与必填参数坑
- `docs/字段必填清单.md` — 单据字段哪些可自动生成
- `docs/项目合规评估与改进建议.md` — 对照课程要求的完成度评估与改进路线图
