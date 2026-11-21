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
- [当前入口](#当前入口)
- [系统架构](#系统架构)
- [Agent 架构决策](#agent-架构决策模型负责判断程序负责正确性)
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
| **主动澄清（slot-filling）** | 缺往来单位 / 数量 / 单价时 agent 主动追问（规则式检测，零额外 LLM 成本），补充回答自动并入草稿；最多 2 轮，可随时「跳过直接预览」 |
| 主数据自动补齐 | 报价里出现的陌生供应商 / 商品 / 仓库，预览时列清「将新建」，确认后一次补齐，无需先建档 |
| 确认卡（带字段来历） | 每个字段标明来历（原话抽的 / 匹配到已有档案 / 系统默认 / 建议新建），缺单价橙色高亮，AI 置信度与提示直接展示 |
| **服务端写入闸门** | `/api/parse·resolve` 签发一次性确认令牌（HMAC 无状态签名 + 草稿内容哈希绑定 + nonce 黑名单防重放，30 分钟过期），`/api/commit` 无有效令牌一律拒绝——「无确认不写入」是服务端不变量，不是 UI 约定 |
| **审计与回归沉淀** | 每次写入落 `app/data/audit.jsonl`（原话 / 模型 / 提示词版本 / 改动 diff / 结果），被拒的写入尝试同样留痕；用户改过的字段自动沉淀为 `regression.jsonl` 回归测试集 |
| 只做加法，可回退 | **不改 jshERP 源码**，全部通过官方 REST API 写入，原系统随时可当后备 |
| **统一业务 Agent** | 一个主入口理解查账、录单、澄清与修正；模型逐步规划并调用只读工具，错误结果会回喂修正。回答数字必须来自工具观察，不合规回答先被要求自我修正，仍失败才确定性兜底 |
| **会话内草稿修正** | 当前未写入草稿保留在会话里；说「改成60瓶」会沿用供应商、商品、价格与仓库，只改指定字段。会话不会保存确认令牌，刷新后只能继续修改，不能重放写入 |
| **图片与表格入口** | 同一输入框支持拍照/粘贴图片与 Excel/CSV 弹窗导入；导入保留列映射与修复 Agent，最后统一回到草稿确认卡 |
| **ERP 全库只读视野** | `sql_query` 直连 jshERP 数据库（只读事务 + 单条 SELECT 校验 + 行数/超时护栏），表结构从 `information_schema` 动态读取并注入上下文，不硬编码表清单：供应商/客户/商品/账户/用户/单据明细/任意聚合与排名都能问，不受预置工具清单限制；写入仍只走官方 REST API |
| **流式过程可视化** | SSE 逐步推送：正在查询库存…（金色脉冲动效）→ ✓ 工具调用行（耗时/参数/思考/观察 JSON 可逐行展开）→ 徽章行（路由/token/数字已核对）——agent 每步可审计可回放 |
| **轻检索（RAG-lite）** | 纯标准库 BM25 双索引：操作文档检索（怎么录单/术语）+ 历史单据取证（「上次可乐跟谁买的」），零外部依赖、命中可解释 |
| **主动洞察与预警** | 看板「AI 经营洞察」卡（规则算事实→LLM 组织语言→逐条数字接地）+ 工作台主动预警横幅（负库存/低库存/应收集中/大额单，纯规则零 LLM）+ 一键生成经营日报（打印友好） |
| **token 用量台账** | 每次调用精确计量（流式 `stream_options.include_usage`，网关不支持时字符估算并标注）落 `data/usage.jsonl`；设置页成本条分用途展示；eval 输出每单 token 均值 |
| 主数据候选点选 | 名字没对上时给 top3 相似候选，确认卡上点一下即重新预览，不用重新解析 |
| 可复现评测 | `eval/` 60 条冻结语料 + 打分脚本（整单全对率 / 字段级准确率 / 幻觉字段数 / 延迟 / token 均值），一条命令跑分 |
| 多模型档案 | 任意 OpenAI 兼容接口都能配（OpenAI / DeepSeek / 通义 / Kimi / 本地 Ollama…），页面即可配置、切换、测试；支持自签证书网关（`insecure`）与丢弃 system 角色的网关（`merge_system`） |
| **ERP 账号联动登录 + 两级权限** | 成员用 **ERP 账号密码**登录（服务端转发 ERP 校验，密码不落盘），会话为 HMAC 无状态签名（默认 30 天）；管理员 = ERP `ismanager=1` 或 `T2S_ADMINS` 名单。共享模型档案 / ERP 连接仅管理员可见可改（接口层 403，非前端隐藏）；每人可另配**私有模型档案**（密钥存服务端本人档案，录单/问答优先用它），设置页成员只见共享模型只读状态行 |
| **双端留痕** | Agent 侧：每次写入落 `audit.jsonl`（operator=登录名）；ERP 侧：录单成功自动往 ERP 自带日志模块写一条「操作人 X 通过 Talk2Stock 录单」（按 userId 归因，原 ERP「日志」页按人可查）；token 用量按登录者分账（usage.jsonl `user` 字段） |
| 密钥不进仓库 | 密钥只存 `app/.env`（已 `.gitignore`）；`config.json` 也在忽略列表里，公开仓库只带 `config.example.json` 模板 |
| **Supervisor 统一智能助手（/ask）** | 问一问 agent loop 升级 supervisor：对话里直接说「给永辉卖50瓶可乐」即自主起草单据草稿（`draft_order` 工具 → 与对话录单同一确认卡/闸门/审计）；改库存删单据类请求在工具层拒绝——写权限边界：agent 自主性到草稿为止，写入必须人工确认（ADR-10） |
| **录单自主校验 agent** | 草稿出预览前先过一个只读取证 loop：查库存够不够、查该客户历史真实成交价（只读 SQL 直查行价）、按操作员习惯补默认仓——补丁是确定性写回（白名单 depot/price + 置信度≥0.85 + 只补缺失），确认卡上可改；结论数字逐个过接地校验（ADR-11） |
| **操作员习惯记忆** | 确认写入的单据沉淀为本人偏好（jsonl 只增不改）：常用仓库、历史成交价（该客户口径优先），校验 agent 用它补默认值，新操作员退全局口径 |
| **导入修复 agent** | Excel/图片导入的「需处理」行：agent 只读取证（商品档案/往来单位/历史原话）给修复建议——高置信自动改、低置信点采纳；白名单只有商品名与往来单位，数量单价永远是 agent 不可触碰字段（ADR-12） |
| **老板日报 agent** | 全项目唯一「无人触发」的 agent：当日懒触发自主分析（自主挑最值得说的 3~5 个点，逐点数字接地），附一键生成采购草稿（走确认卡）；LLM 失败降级规则模板，数字全真（ADR-12） |
| **欠款聚合 SQL 直查** | 欠款合计与 top 归因直查 DB（只读事务 + SELECT 白名单，与表头真值同公式），规模下分毫不差——修掉「单类 >200 张漏老账 23%」的规模缺陷；两轮失败尝试（时间窗二分/递归塌缩）的完整证据链在 ADR-13，聚合走 SQL、明细走 REST 的架构分界由此确立 |
| 语音输入 | 对话输入框语音转写（浏览器 Web Speech API，zh-CN，中间结果实时上屏）；非安全上下文（HTTP 部署）自动置灰并指路部署文档，HTTPS 部署见 `docs/语音输入HTTPS部署.md` |

---

## 当前入口

`/ask` 是唯一主入口；桌面与手机都使用同一对话。经营看板保持原页面，
设置保留模型与账号配置，`/new` 仅作为手动表单和旧链接兼容页。

| 页面 | 地址 | 用途 |
|---|---|---|
| 智能助手 | `http://localhost:8090/ask` | 查经营、起草单据、修正草稿、图片/表格导入 |
| 经营看板 | `http://localhost:8090/dashboard` | 老板只读看板；本次不改动 |
| 设置 | `http://localhost:8090/settings` | ERP 连接、模型档案、账号权限 |
| 手动录入（兼容） | `http://localhost:8090/new` | 逐字段表单；旧 `/ai`、`/form` 链接仍可访问 |

真实写入仍然只发生在确认卡之后；图片、文字、表格三种输入最终都是同一份草稿契约。

---

## 系统架构

分层职责切开，写入闸门由我们自己的代码把关；问答侧是手写 agent loop（不依赖
OpenAI function-calling 协议，任意兼容端点行为一致）：

```
┌──────────────────────────────────────────────────────────────┐
│  入口层  app/web/*.html + app/server.py（:8090，纯标准库）      │
│  一句话输入 / 表单 / 流式问答(SSE) / 看板洞察，输出「确认卡」      │
└───────────────┬──────────────────────────────────────────────┘
                │ /api/parse ──► 澄清追问(可跳过) ──► 确认卡      │
                │ /api/ask/stream ► SSE: action/step/done        │
┌───────────────▼──────────────────────────────────────────────┐
│  智能体层                                                     │
│  llm.py    模型调用：OpenAI 兼容接口、JSON 抽取、图片识别          │
│  agent.py  Agent 运行时：规划→工具→观察→检查→修正/收尾            │
│  ask.py    统一入口：意图理解、工具箱、会话草稿、draft_order       │
│  billcheck.py 录单校验：库存/历史价/习惯，确定性补丁              │
│  dbread.py 只读 SQL：全库动态结构 + SELECT 白名单/只读事务        │
│  repair.py / daily.py 表格修复与老板日报的专项循环                 │
└───────────────┬──────────────────────────────────────────────┘
                │  draft：{biz, party, items[], depot, ...}
┌───────────────▼──────────────────────────────────────────────┐
│  落库层  draft.py + erp.py + gate.py                           │
│  gate：HMAC 无状态一次性令牌（nonce 黑名单防重放）               │
│  resolve()：只读匹配主数据，返回「将新建」清单（不写库）           │
│  commit()：人工确认后，补主数据 + 建单，全部走官方接口             │
└───────────────┬──────────────────────────────────────────────┘
                │  jshERP REST API（仅 HTTP，绝不直连数据库）
┌───────────────▼──────────────────────────────────────────────┐
│  底座 ERP  jshERP（MySQL + Redis + Spring Boot + Vue）          │
└──────────────────────────────────────────────────────────────┘
```

### Agent 架构决策：模型负责判断，程序负责正确性

库存、欠款、采销这类结构化数据不能用一段模型回答代替业务逻辑。当前版本的边界是：

- **模型负责**：识别意图、规划步骤、选择工具、根据错误与候选修正计划、组织自然语言；
- **工具负责**：库存/采销/欠款/单据/SQL 的结构化读取，草稿校验与主数据匹配；
- **程序负责**：数字接地、补丁白名单、确认令牌、审计与真实写入；
- **修正机制**：工具异常、无候选、答案未通过接地校验都会作为观察回到循环；仍失败才用确定性模板兜底；
- **权限边界**：Agent 自主到草稿为止，真实写入必须经过人工确认和一次性令牌。

旧规则直答函数仍作为高确定性工具保留，但不再抢占主入口；这避免“看起来像 Agent，
实际是正则分派”，也保留数字可靠性。

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
│   ├── web/                 #   前端页面（home / new / ask / dashboard / settings）
│   ├── config.example.json  #   配置模板；首启自动复制为 config.json（config.json 已被忽略）
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

1. 打开 http://localhost:8090/settings
2. 进 **「模型档案」** 区
3. 点 **「＋ 添加 / 编辑模型」**，填：

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

`app/config.example.json` 是配置模板，首次启动会自动复制成 `app/config.json`（**config.json 已被 `.gitignore` 排除**——它可能带本机网关地址，不进仓库），里面预置了模型档案的 `base_url` 和 `model`（不含密钥）；只要在 `.env` 里补上对应密钥，重启即可生效。也可以完全不管 `.env`，直接用**方式 A** 在页面里配。

> 解析层对推理类端点的兼容：优先用流式 + `response_format=json_object`，失败自动降级（去掉 `response_format`、再退非流式）；若网关会丢弃 `system` 角色，可在档案里打开 `merge_system`（见 `app/core/llm.py`）。

---

## 环境变量

配置来源优先级：`DEFAULTS`（代码内）→ `app/config.json`（本机文件，不入库）→ 环境变量 / `app/.env`（最高）。

| 变量 | 作用 | 默认 |
|---|---|---|
| `T2S_ERP_BASE` | ERP 后端地址 | `http://localhost:9999/jshERP-boot` |
| `T2S_ERP_USER` | ERP 登录账号 | `jsh` |
| `T2S_ERP_PASS` | ERP 登录密码（明文，服务端会做 MD5） | 空（需填） |
| `T2S_KEY_<档案名大写>` | 某个模型档案的 API Key，如 `T2S_KEY_DEEPSEEK` | 空 |
| `T2S_BASE_<档案名大写>` | 覆盖某档案的网关地址（config.json 不入库，真实地址写这里） | 空 |
| `T2S_REQUIRE_AUTH` | 设为 `1` 后所有 `/api` 接口需登录（ERP 账号，或管理员口令） | `0`（关闭） |
| `T2S_DEMO_PASS` | 访问口令（开启鉴权后作为**管理员后备通道**，ERP 挂了也能进） | `talk2stock` |
| `T2S_ADMINS` | 额外管理员名单（逗号分隔 ERP 登录名）；ERP `ismanager=1` 本来就算管理员 | 空 |
| `T2S_SESSION_TTL_DAYS` | 登录会话有效期（天） | `30` |
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

- **密钥管理**：所有 API Key、ERP 密码只存 `app/.env`（已 `.gitignore`）。页面里配置的 Key 也会落到这里，`config.json` 永远不含密钥。**不要把填好密钥的 `.env` 提交或截图外发**；本机网关地址请配 `T2S_BASE_<档案名>` 环境变量，不要写回 `config.json`。
- **写库闸门**：写入路径是「自然语言 → 草稿 → 确认卡 → 人工点确认 → commit」。`/api/resolve` 签发一次性确认令牌（与草稿内容 sha256 绑定，30 分钟过期），`/api/commit` 无有效令牌一律拒绝——**「无确认不写入」是服务端不变量，不是 UI 约定**；被拒的尝试同样进审计。
- **审计与归因**：每次写入（成功或被拒）落 `app/data/audit.jsonl`，含 `operator`（登录后自动取 ERP 登录名）、原话、模型、提示词版本+hash、diff、结果。同时向 ERP 自带日志模块写一条「操作人 X 通过 Talk2Stock 录单」（按 userId 归因，原 ERP「日志」页可按人查）。审计写入失败会打 stderr，不静默丢弃，按 10MB 轮转保留 5 份。token 用量台账（`usage.jsonl`）带 `user` 字段，可按登录者分账。
- **接口鉴权与登录**：`/api/*` 默认不鉴权（方便本地开发）。把 8090 暴露到公网前，**必须**设 `T2S_REQUIRE_AUTH=1` 并改掉默认 `T2S_DEMO_PASS`。开启后：
  - **成员用 ERP 账号密码登录**（前端弹登录框；服务端转发 ERP `/user/login` 校验，MD5 在服务端做，密码不落盘、不进日志）；登录失败连续 5 次锁 60 秒，错误文案不区分「账号不存在/密码错误」；
  - 会话令牌是 HMAC 无状态签名（复用写入闸门的密钥机制），默认 30 天，重启不失效；
  - **两级权限**：共享模型档案与 ERP 连接（`/api/config/*`）仅管理员可用，成员在接口层收到 403；成员可配自己的私有模型档案（密钥只存服务端 `data/user_profiles.json`，0600）；
  - **静态口令 `T2S_DEMO_PASS` = 管理员后备通道**（ERP 不可用时仍可进）；
  - 或者退一步：放到反向代理后用 Basic Auth / IP 白名单保护，或只在内网使用。
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
python3 eval/run.py                    # 历史录单解析语料（v1 60 条；v2 追加 8 条规格）
python3 eval/run_unified.py            # 当前统一 Agent 冒烟：查询/起草/澄清/边界（不写库）
```

- 语料 `eval/cases.jsonl`（**v1-20261010 冻结**：48 条常规 + 12 条对抗，gold 人工标注）；
  打分口径：整单关键字段全对率（供应商/商品/数量×单位/单价/仓库/业务类型全对才算对）、
  字段级准确率、幻觉字段数（原话没提却被填进草稿）、p95 延迟、**每单 token 均值**
  （2026-10 起精确计量：流式 `include_usage`，网关不支持时按字符估算并标注）。
- **跑分结果与提示词迭代证据**（历次原始数据在 `eval/results/`）：

  | 模型 | 提示词 | 整单全对率 | 幻觉字段 | 解析报错 | 延迟 均值/p95 | token/单 | 结果文件 |
  |---|---|---|---|---|---|---|---|
  | DeepSeek V4.1 Flash（本地） | v1 | 78.3%（另 9 次网关 502） | **0** | 9 | 3.7s / 11.7s | — | `run_20261010-*` |
  | DeepSeek V4.1 Flash（本地） | **v2** | **98.3%**（59/60） | **0** | 0 | 2.6s / 4.4s | — | `run_20261010-235627-workbuddy-promptv2.md` |
  | Qwen3.8-Flash-Next（ModelScope） | **v2** | **96.7%**（58/60） | **0** | 0 | 7.7s / 19.1s | — | `run_20261010-235336-moda-promptv2.md` |
  | DeepSeek V4.1 Flash（本地） | v3 复跑 | 96.7%（58/60） | **0** | 0 | 3.6s / 6.5s | — | `run_20261012-135109-workbuddy-promptv3-rerun.md` |
  | DeepSeek V4.1 Flash（本地） | v3+澄清+用量（15 条冒烟） | 100%（15/15） | **0** | 0 | 2.1s / 3.3s | **1359**（prompt 1233 + completion 126） | `run_20261025-001549-agentloop-smoke.md` |

  v1→v2 的唯一改动：注入当前日期、要求相对日期换算成绝对日期——两模型的日期类失分全部消除。
  v3 复跑（本地网关在线波动）为 96.7%，比 v2 少 1 条对抗用例 a03：同一模型同一提示词存在 ±1 条的网络侧波动，
  这本身就是"为什么最终交付按 95%+ 口径、并按失败用例做分析"的依据。固定失败用例 `a12`
  （"客户1那里拿点衣服"，业务类型+数量双模糊）进 failure analysis。
  token 效率的另一个维度：高频统计问句（"今天卖了多少"等）被规则直答层拦截，**0 token** 秒回；
  详见 `docs/失败案例与迭代证据-agent升级.md`（含"网关固定注入 8.5K/请求触发预算熔断"的 C3 案例）。
- 人工基线实验（2 名无 ERP 经验组员 × 4 任务 × 2 工具，共 16 条正式记录）
  已入库：`eval/baseline_timing_slim.csv`。统计口径：空白错误/中断按 0 计；
  S1 的语音失败 71.58s 保留，不剔除。结果：
  - 场景 A（档案齐全）：ERP 均值 88.00s / Talk2Stock 48.45s，快 45.0%；
  - 场景 B（缺档案）：ERP 均值 86.48s / Talk2Stock 50.29s，快 41.8%；
  - ERP 侧共 5 次中断（建档/来回），Talk2Stock 0 次；原始备注与操作错误均在 CSV。
- 历次结果在 `eval/results/`（JSON + Markdown）。

---

## 路线图

已完成 ✅：
- ~~服务端确认闸门~~：一次性确认令牌 + 内容哈希绑定（`core/gate.py`），篡改/重放/无令牌全部拒绝并有审计；2026-10 升级为 HMAC 无状态令牌（重启不失效）+ nonce 黑名单防重放。
- ~~审计日志~~：`app/data/audit.jsonl`（原话、模型、提示词版本+hash、与签发草稿的 diff、结果、被拒记录）。
- ~~回归沉淀~~：用户改过的字段自动写入 `app/data/regression.jsonl`。
- ~~评测资产~~：60 条冻结语料 + `eval/run.py`（v1→v2 提示词迭代证据见 `eval/results/`）；token 维度（每单均值）。
- ~~只读问答智能体~~：三层架构（规则直答 0 token / 手写 agent loop 多步取证 / 数字接地兜底）+ SSE 流式过程可视化；失败案例与迭代证据见 `docs/失败案例与迭代证据-agent升级.md`。
- ~~主动澄清~~：缺关键信息 agent 追问（≤2 轮，可跳过），复用多轮修正链路。
- ~~轻检索（RAG-lite）~~：BM25 双索引（操作文档 + 历史单据）注册为 agent 工具。
- ~~经营洞察 / 主动预警 / 每日日报~~：规则事实 + LLM 组织 + 逐条数字接地。
- ~~token 用量台账~~：usage.jsonl 精确计量 + 设置页成本条 + /api/usage。
- ~~主数据候选匹配~~：匹配失败时给 top3 相似候选，确认卡上点选后自动重新预览。
- ~~单位换算与含税口径~~：商品建档带 `unitId`，`unit` 传用户单位，后端 `DepotItemService` 按单位组 ratio 自动折算 `basicNumber`；行级 `tax_rate` 落 `taxRate / taxMoney / taxLastMoney`，含税/未税口径跟随 ERP 的 `materialPriceTaxFlag`。
- ~~CI / 离线回归~~：Python 编译检查、`unittest` 当前 183 项、前端内联 JS `node --check`；统一 Agent 冒烟 `eval/run_unified.py` 5/5。

- ~~Supervisor 统一智能助手~~：问一问起草单据（写意图路由到草稿，闸门不变）+ 录单自主校验 agent + 操作员习惯记忆 + 导入修复 agent + 老板日报 agent（ADR-10/11/12）；评测资产 ask_cases_v2 / billcheck_cases / repair_cases / daily_cases + 各自跑分器。
- ~~P4 欠款规模漏报闭合~~：欠款聚合 SQL 直查（ADR-13，stress_scale P4 探针漏报 0；三路尝试完整证据链）。
- ~~语音输入~~：Web Speech API + 安全上下文三级降级 + HTTPS 部署文档。

下一步（按对答辩的价值排序）：
1. **最终材料预演**：按 `docs/final/Presentation_Outline_*.md` 的 10 分钟时间轴走两遍，确认 live demo 与回退截图都可用。
2. **公网部署 + 备份录屏**：IP+端口或海外节点，暴露前开 `T2S_REQUIRE_AUTH=1`。
3. 将统一 Agent 的冻结评测扩到完整语料，并补扫描件 OCR 与旧版 Office 格式。

---

## 最终交付物

完整索引见 [`docs/final/README.md`](docs/final/README.md)。

- 最终报告：[English DOCX](docs/final/Talk2Stock_Final_Report_EN.docx) / [PDF](docs/final/Talk2Stock_Final_Report_EN.pdf) · [中文 DOCX](docs/final/Talk2Stock_Final_Report_ZH.docx) / [PDF](docs/final/Talk2Stock_Final_Report_ZH.pdf)
- 10 分钟演示：[English PPTX](docs/final/Talk2Stock_Presentation_EN.pptx) / [PDF](docs/final/Talk2Stock_Presentation_EN.pdf) · [中文 PPTX](docs/final/Talk2Stock_Presentation_ZH.pptx) / [PDF](docs/final/Talk2Stock_Presentation_ZH.pdf)
- 逐页大纲：[EN](docs/final/Presentation_Outline_EN.md) · [ZH](docs/final/Presentation_Outline_ZH.md)
- 共用图表与界面素材：`docs/final/assets/`

---

## 相关文档

- `docs/易用性改造方案.md` — 原系统痛点实测、已实现能力、分阶段改进清单
- `docs/本地部署验证报告.md` — 端到端验证记录与已修复缺陷（含时区、僵死进程）
- `docs/API端点全清单.md` — jshERP 接口实测清单与必填参数坑
- `docs/字段必填清单.md` — 单据字段哪些可自动生成
- `docs/项目合规评估与改进建议.md` — 对照课程要求的完成度评估与改进路线图
