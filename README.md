# Talk2Stock · 对话式进销存智能体

> **Talk-to-Order Agent for real ERPs**：架在真实企业 ERP（[jshERP · 管伊佳 ERP](https://gitee.com/jishenghua/JSH_ERP)）之上的对话式录单智能体——
> **一句话 → 解析成单据草稿 → 每个字段标明来历 → 人工确认 → 官方接口写入 → 审计留痕**。
> 不是另搭的玩具库，也不是简单包一层界面：理解、匹配、确认闸门、审计、回归沉淀是它自己的智能层。

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
| 一句话建单 | 对话式智能体解析出 6 类业务（采购入库 / 销售出库 / 采购退货 / 销售退货 / 其他入出库）的单据草稿 |
| 主数据自动补齐 | 报价里出现的陌生供应商 / 商品 / 仓库，预览时列清「将新建」，确认后一次补齐，无需先建档 |
| 确认卡（带字段来历） | 每个字段标明来历（原话抽的 / 匹配到已有档案 / 系统默认 / 建议新建），缺单价橙色高亮，AI 置信度与提示直接展示 |
| **服务端写入闸门** | `/api/parse·resolve` 签发一次性确认令牌（与草稿内容哈希绑定，30 分钟过期），`/api/commit` 无有效令牌一律拒绝——「无确认不写入」是服务端不变量，不是 UI 约定 |
| **审计与回归沉淀** | 每次写入落 `app/data/audit.jsonl`（原话 / 模型 / 提示词版本 / 改动 diff / 结果），被拒的写入尝试同样留痕；用户改过的字段自动沉淀为 `regression.jsonl` 回归测试集 |
| 只做加法，可回退 | **不改 jshERP 源码**，全部通过官方 REST API 写入，原系统随时可当后备 |
| 只读问答智能体 | 「可乐还有多少」「谁欠我钱」——LLM 选工具 → 只读查询 → 依数据作答，答案标注数据来源，绝不写库 |
| 主数据候选点选 | 名字没对上时给 top3 相似候选，确认卡上点一下即重新预览，不用重新解析 |
| 可复现评测 | `eval/` 60 条冻结语料 + 打分脚本（整单全对率 / 字段级准确率 / 幻觉字段数 / 延迟），一条命令跑分 |
| 多模型档案 | 任意 OpenAI 兼容接口都能配（OpenAI / DeepSeek / 通义 / Kimi / 本地 Ollama…），页面即可配置、切换、测试；支持自签证书网关（`insecure`）与丢弃 system 角色的网关（`merge_system`） |
| 密钥不进仓库 | 密钥只存 `app/.env`（已 `.gitignore`），`config.json` 永远不含密钥 |

---

## 四个入口

四个入口共用同一套后端（`app/core/draft.py` 的 `resolve / commit`），预览确认层行为完全一致。

| 页面 | 地址 | 适合谁 | 依赖 |
|---|---|---|---|
| 表单快速录入 | `http://localhost:8090/form` | 日常录单员 | 无（纯预设匹配，不需要 AI） |
| AI 智能录入 | `http://localhost:8090/ai` | 想省事的人 / 手机端 | 需配置一个 LLM 模型 |
| 问一问（只读问答） | `http://localhost:8090/ask` | 随口查库存/采销/欠款 | 需配置一个 LLM 模型 |
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
├── app/                     # ★ 自研：Talk2Stock 智能体服务（Python 3 纯标准库，零第三方依赖）
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
├── start-all.sh             # 一键拉起：容器 + ERP 四容器 + Talk2Stock 服务
├── verify-local.sh          # 端到端验证：运行时 → 容器 → HTTP → 登录 → AI 链路
└── LICENSE                  # MIT（仅覆盖自研部分）
```

---

## 快速开始

### 前置条件

- **Docker + Docker Compose**：macOS 可用 [colima](https://github.com/abiosoft/colima) 或 Docker Desktop；Linux / Windows(WSL) 用 Docker Engine 即可。
- **Python 3.8+**：Talk2Stock 只用标准库，不需要 `pip install` 任何东西。
- 首次启动会本地构建 ERP 后端（Maven）与前端（Node），**约 5–15 分钟**；之后启动是秒级。

### 方式一：一键启动（推荐）

```bash
bash start-all.sh
```

脚本依次完成：① 确保容器运行时在线（若无 colima 但已有 Docker，会自动跳过）→ ② 起 ERP 四个容器（mysql / redis / backend / web）→ ③ 起 Talk2Stock 智能体服务（`:8090`）。

### 方式二：手动分步

```bash
# 1) 起 ERP（首次会构建镜像）
cd jshERP-latest/deploy
docker compose up -d
docker compose ps          # 四个容器都应为 running，mysql/redis 为 healthy

# 2) 准备 Talk2Stock 配置并启动
cd ../..
cp app/.env.example app/.env      # 首次执行
python3 app/server.py             # 默认 0.0.0.0:8090，Ctrl-C 停止
```

### 访问入口

| 服务 | 地址 |
|---|---|
| Talk2Stock 智能体（本项目） | http://localhost:8090/ |
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
| `T2S_BASE_<档案名大写>` | 覆盖某档案的网关地址（公开仓库 config.json 只放占位，真实地址写这里） | 空 |
| `T2S_REQUIRE_AUTH` | 设为 `1` 后所有 `/api` 接口需带访问口令 | `0`（关闭） |
| `T2S_DEMO_PASS` | 访问口令 | `talk2stock` |
| `PORT` / `HOST` | Talk2Stock 监听端口 / 地址 | `8090` / `0.0.0.0` |

---

## 端到端验证

```bash
bash start-all.sh          # 确保整套在跑
bash verify-local.sh       # 部署层 + HTTP 端到端，共 12 项
cd app && python3 selfcheck.py   # 应用层自检（直连 core 模块）
```

### 重置演示数据

```bash
python3 eval/seed_demo.py --yes   # 备份 → 清场 → 重建固定演示状态（7 往来单位/7 商品/9 张单）
```

一键把业务数据恢复到受控状态（基线实验起点、演示前的状态还原），幂等可重复；完整说明见脚本头注释。

`verify-local.sh` 会依次检查：容器运行时 → 四个 ERP 容器健康 → 三个 HTTP 入口 → ERP 登录（MD5）→ Talk2Stock 读主数据 → 自然语言解析 → 只读预览（不写库）。最后一行给出「通过 / 告警 / 失败」汇总。

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
`start-all.sh` / `verify-local.sh` 是 bash 脚本，在 WSL 或 Git Bash 下运行即可；Talk2Stock 本身是跨平台 Python。

---

## 安全说明

- **密钥管理**：所有 API Key、ERP 密码只存 `app/.env`（已 `.gitignore`）。页面里配置的 Key 也会落到这里，`config.json` 永远不含密钥。**不要把填好密钥的 `.env` 提交或截图外发。**
- **写库闸门**：写入路径是「自然语言 → 草稿 → 确认卡 → 人工点确认 → commit」。`/api/resolve` 签发一次性确认令牌（与草稿内容 sha256 绑定，30 分钟过期），`/api/commit` 无有效令牌一律拒绝——**「无确认不写入」是服务端不变量，不是 UI 约定**；被拒的尝试同样进审计。
- **审计与归因**：每次写入（成功或被拒）落 `app/data/audit.jsonl`，含 `operator`、原话、模型、提示词版本+hash、diff、结果。审计写入失败会打 stderr，不静默丢弃。
- **接口鉴权**：生产环境务必设 `T2S_REQUIRE_AUTH=1` 并改掉默认 `T2S_DEMO_PASS`（当前部署已开启）。
- **ERP 凭据**：演示账号为 `admin / cuhksz`；`jsh` 是系统自带的租户所有者（`isystem=1`），保留但不对业务开放。生产环境请再轮换。

---

## 上游与许可

- **Talk2Stock 自研部分**（`app/`、`docs/`、`start-all.sh`、`verify-local.sh` 等）：**MIT**，见根目录 [`LICENSE`](./LICENSE)。
- **`jshERP-latest/`**：第三方开源项目 **jshERP（管伊佳 ERP，原名华夏 ERP）** 的源码副本，遵循其自身 **Apache License 2.0**（见 `jshERP-latest/LICENSE`）。上游快照来源：`https://gitcode.com/hualiruanjian/JSH_ERP`（快照 commit `de59390`）。
  > 注：网络上对 jshERP 的协议存在 Apache-2.0 / GPL-3.0 两种说法。**以实际收录副本的 `jshERP-latest/LICENSE`（Apache-2.0）为准。** 本项目未收录另一份旧部署包（其 LICENSE 为 GPL-3.0），以避免协议口径混淆。
- Talk2Stock 仅通过 jshERP 官方 REST API 交互，**未修改上游源码**。

---

## 评测（可复现跑分）

```bash
cd app
python3 ../eval/run.py                 # 全量 60 条冻结语料（只调解析，绝不写库）
python3 ../eval/run.py --type adversarial   # 只跑 12 条对抗样本
python3 ../eval/run.py --limit 8 --tag smoke # 冒烟
```

- 语料 `eval/cases.jsonl`（**v1-20261010 冻结**：48 条常规 + 12 条对抗，gold 人工标注）；
  打分口径：整单关键字段全对率（供应商/商品/数量×单位/单价/仓库/业务类型全对才算对）、
  字段级准确率、幻觉字段数（原话没提却被填进草稿）、p95 延迟。
- **首跑结果（2026-10-10）与提示词迭代证据**：

  | 模型 | 提示词 | 整单全对率 | 幻觉字段 | 解析报错 | 延迟 均值/p95 |
  |---|---|---|---|---|---|
  | DeepSeek V4.1 Flash（本地） | v1 | 78.3%（另 9 次网关 502） | **0** | 9 | 3.7s / 11.7s |
  | DeepSeek V4.1 Flash（本地） | **v2** | **98.3%**（59/60） | **0** | 0 | 2.6s / 4.4s |
  | Qwen3.8-Flash-Next（ModelScope） | v1 | 86.7%（52/60） | **0** | 0 | 7.5s / 19.8s |
  | Qwen3.8-Flash-Next（ModelScope） | **v2** | 96.7%（58/60） | **0** | 0 | 7.7s / 19.1s |

  v1→v2 的唯一改动：注入当前日期、要求相对日期换算成绝对日期——两模型的日期类失分全部消除。
  剩余唯一失败用例 `a12`（"客户1那里拿点衣服"，业务类型+数量双模糊）进 failure analysis。
- 基线对比实验（原生界面人工录单 vs Talk2Stock，两场景设计）见
  [`docs/基线对比实验手册.md`](docs/基线对比实验手册.md)，记录表模板 `eval/baseline_timing.csv`。
- 历次结果在 `eval/results/`（JSON + Markdown）。

---

## 路线图

已完成 ✅：
- ~~服务端确认闸门~~：一次性确认令牌 + 内容哈希绑定（`core/gate.py`），篡改/重放/无令牌全部拒绝并有审计。
- ~~审计日志~~：`app/data/audit.jsonl`（原话、模型、提示词版本+hash、与签发草稿的 diff、结果、被拒记录）。
- ~~回归沉淀~~：用户改过的字段自动写入 `app/data/regression.jsonl`。
- ~~评测资产~~：60 条冻结语料 + `eval/run.py`（v1→v2 提示词迭代证据见 `eval/results/`）。
- ~~只读问答智能体~~：`/api/ask` + `/ask` 页（选工具 → 只读查询 → 依数据作答）。
- ~~主数据候选匹配~~：匹配失败时给 top3 相似候选，确认卡上点选后自动重新预览。
- ~~单位换算与含税口径~~：商品建档带 `unitId`，`unit` 传用户单位，后端 `DepotItemService` 按单位组 ratio 自动折算 `basicNumber`；行级 `tax_rate` 落 `taxRate / taxMoney / taxLastMoney`，含税/未税口径跟随 ERP 的 `materialPriceTaxFlag`。

下一步（按对答辩的价值排序）：
1. **基线实验落地**：按 `docs/基线对比实验手册.md` 跑两场景计时（半天）。
2. **公网部署 + 备份录屏**：IP+端口或海外节点，暴露前开 `T2S_REQUIRE_AUTH=1`。
3. 语音输入（浏览器转写，计划书里是可选项）。

---

## 相关文档

- `docs/易用性改造方案.md` — 原系统痛点实测、已实现能力、分阶段改进清单
- `docs/本地部署验证报告.md` — 端到端验证记录与已修复缺陷（含时区、僵死进程）
- `docs/API端点全清单.md` — jshERP 接口实测清单与必填参数坑
- `docs/字段必填清单.md` — 单据字段哪些可自动生成
- `docs/项目合规评估与改进建议.md` — 对照课程要求的完成度评估与改进路线图
