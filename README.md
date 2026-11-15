# Talk2Stock · 对话式进销存智能体

> **Talk-to-Order Agent for real ERPs**：架在真实企业 ERP（[jshERP · 管伊佳 ERP](https://gitee.com/jishenghua/JSH_ERP)）之上的对话式录单智能体——
> **一句话 → 解析成单据草稿 → 每个字段标明来历 → 人工确认 → 官方接口写入 → 审计留痕**。
> 不是另搭的玩具库，也不是简单包一层界面：理解、匹配、确认闸门、审计、回归沉淀是它自己的智能层。

很多人抱怨「ERP 录单太慢」。我们把它拆成两段真正昂贵的时间，并只针对这两段做优化：

- **档案补齐成本**：新供应商 / 新商品 / 新规格，录单前必须先去「基础资料」建档，再回来重头填。小公司里这件事天天发生，且没有上限。
- **翻译成本**：「老张今天送了两箱水，12 块一瓶，放前面那个仓」是一句话，不是表格。谁把它翻译成供应商、商品、数量×单位、单价、仓库、日期——现在全靠人脑加鼠标。

Talk2Stock 让录单员**用一句人话把业务说清楚**，系统先给出草稿、标出「哪些档案要新建」，人确认后才真正落库。写错的成本被拦在写入之前，录入的点击次数从约 25 次降到约 4 次。

> **在线演示**：<http://111.91.30.23:8090/>（云服务器实测部署，已开启 ERP 账号登录鉴权；演示账号由管理员分配。部署与运维细节见 [生产部署与运维](#生产部署与运维)）。

---

## 目录

- [核心特性](#核心特性)
- [五个入口](#五个入口)
- [系统架构](#系统架构)
- [Agent 架构决策](#agent-架构决策为什么-tool-use-而不是-rag)
- [目录结构](#目录结构)
- [快速开始](#快速开始)
- [生产部署与运维](#生产部署与运维)
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
| **三层问答 agent** | 第 1 层规则直答（高频统计问句 0 token 秒回，含「多少个供应商」这类主数据计数）→ 第 2 层手写 function-calling loop（多工具链式取证，复合问题如「上月谁买最多？他欠我多少？」）→ 第 3 层数字接地兜底（答案每个数字必须来自工具观察，否则退模板句） |
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
| **列表全量拉取（自适应翻页）** | 两个列表端点认不同分页参数（`/material/list` 认 `currentPage`、`/depotHead/list` 认 `pageNo`），客户端按「空页 / 整页重复」自动换参续翻：主数据超过 500 条不再静默截断（规模基准 812 条商品计数 = SQL 真实值，三次复测一致）；单据侧超 200 张的边界见[已知边界](#已知边界与未解问题) |
| **推理强度与厂商参数可配** | 模型档案新增 `reasoning_effort`（minimal/low/medium/high）、`extra_body`（原样透传厂商私有参数，如 Qwen 的 `enable_thinking`）与 `extra_headers`（网关分流头）；agent 决策步建议 `low` + 2000 token 输出封顶，避免推理模型把预算烧在思考上（F10/F12 失败案例） |
| **问答评测驱动调优** | `eval/ask_cases.jsonl` 34 条冻结问句 + `eval/run_ask.py` 跑分器（分类统计 + 未通过逐条归因）：v1→v5 通过率 **76.5% → 94.1%**，每一版改动都有失败用例支撑（实体护栏、收紧步数预算、`query_bills` 工具） |
| 密钥不进仓库 | 密钥只存 `app/.env`（已 `.gitignore`）；`config.json` 也在忽略列表里，公开仓库只带 `config.example.json` 模板 |

---

## 五个入口

五个入口共用同一套后端（`app/core/draft.py` 的 `resolve / commit`），预览确认层行为完全一致。

| 页面 | 地址 | 适合谁 | 依赖 |
|---|---|---|---|
| 工作台（首页） | `http://localhost:8090/` | 一页看 KPI、发指令、录单、提问 | 无（AI 指令需配置模型） |
| 录入（/new） | `http://localhost:8090/new` | 日常录单员 / 想省事的人 / 手机端 | 对话模式需 LLM，表单模式纯预设匹配 |
| 问一问（只读问答） | `http://localhost:8090/ask` | 随口查库存/采销/欠款 | 需配置一个 LLM 模型 |
| 经营看板 | `http://localhost:8090/dashboard` | 老板（只读） | 无 |
| 设置 | `http://localhost:8090/settings` | 改 ERP 连接、配/切/测模型档案 | 无 |

录单页把原来的「AI 录入 `/ai`」和「表单录入 `/form`」合并成了一页双模式，旧地址自动跳转 `/new`。
外加原版 ERP 本身：`http://localhost:8080`（报表、权限、盘点、财务等高级功能仍回原系统）。

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
│  llm.py    解析：口语→严格 JSON 草稿 + 规则式澄清检测            │
│  agent.py  手写 function-calling loop：{thought,tool,args}     │
│            →执行→observation 回喂；步数+token 双预算；          │
│            数字接地校验(final 每个数字须来自观察)                │
│  ask.py    三层路由：规则直答(0 token) / agent loop / 模板兜底   │
│  insight.py 规则事实 → LLM 组织 → 逐条接地（洞察/预警/日报）      │
│  retrieve.py BM25 轻检索：操作文档 + 历史单据（RAG-lite）        │
│  dbread.py  只读 SQL：全库表结构动态发现 + SQL 白名单/只读事务     │
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

### Agent 架构决策：为什么 tool-use 而不是 RAG

库存、欠款、采销这类**结构化**经营数据，答案必须是精确数字且可追溯到 ERP——
检索文本块（RAG）会丢数字精度且不可审计。因此本项目的选择是：

- **结构化数据 → tool-use + 数字接地**：agent 只能通过只读工具取数，最终答案里
  每个数字都必须出现在某步工具观察 JSON 里，否则退回确定性模板句；
- **非结构化文本 → BM25 轻检索**：操作文档问答与历史单据取证（「上次可乐跟谁
  买的」）用纯标准库 BM25（中文字符 bigram），零外部依赖、命中相关度可解释；
- 混合架构的代价权衡：不引入 embedding API / 向量库，保持「纯标准库自建、
  可审计、可离线」的项目承诺。

详细决策记录与失败案例见 `docs/architecture.md` 与
`docs/失败案例与迭代证据-agent升级.md`。

---

## 目录结构

```
.
├── app/                     # ★ 自研：Talk2Stock 智能体服务（Python 3 纯标准库，零第三方依赖）
│   ├── server.py            #   HTTP 服务（默认 0.0.0.0:8090）
│   ├── selfcheck.py         #   应用层自检：直连 core 模块验逻辑
│   ├── core/
│   │   ├── config.py        #   配置与多模型档案（密钥只从 .env 读）
│   │   ├── erp.py           #   jshERP REST 客户端（登录/主数据/建单/自适应翻页）
│   │   ├── llm.py           #   LLM 调用层：多档案、推理强度、参数透传、用量计量
│   │   ├── draft.py         #   主数据匹配 + 建单（两段式 resolve/commit）
│   │   ├── gate.py          #   一次性确认令牌（HMAC 无状态 + nonce 黑名单）
│   │   ├── auth.py          #   ERP 账号联动登录 + 两级权限（管理员/成员）
│   │   ├── agent.py         #   手写 function-calling loop（步数 + token 双预算 + 数字接地）
│   │   ├── ask.py           #   三层问答路由：规则直答 / agent loop / 模板兜底
│   │   ├── insight.py       #   经营洞察、主动预警、每日日报
│   │   ├── retrieve.py      #   BM25 轻检索（操作文档 + 历史单据）
│   │   ├── dbread.py        #   只读 SQL（单条 SELECT 校验 + 只读事务 + 行数护栏）
│   │   ├── oplog.py         #   /api 耗时与结果埋点（oplog.jsonl）
│   │   └── report.py        #   经营看板数据
│   ├── web/                 #   前端页面（home / new / ask / dashboard / settings）
│   ├── config.example.json  #   配置模板；首启自动复制为 config.json（config.json 已被忽略）
│   └── .env.example         #   密钥模板；复制成 .env 后填真实值（.env 已被忽略）
├── jshERP-latest/           # 上游 jshERP 源码副本（Apache-2.0，含 deploy/ 编排）
│   └── deploy/              #   docker-compose.yml + Dockerfile + nginx.conf
├── docs/                    # 设计 / 部署运维 / 接口清单 / 失败案例文档
├── eval/                    # 冻结语料 + 跑分脚本（run.py 解析 / run_ask.py 问答 / stress_scale.py 规模）
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

默认账号：`jsh / 123456`（租户账号）、`admin / 123456`（超管，租户 0）——这是**初始化 SQL 的出厂值**，公网实例必须改（本实例已改，登录口令存 `app/.env`）。

> 演示数据：ERP 初始化 SQL 自带少量供应商/商品/仓库。AI 录入时如果库里没有对应的供应商或商品，确认后会**自动新建**，不用先去基础资料建档。

---

## 生产部署与运维

### 本实例（在线演示）

| 项 | 值 |
|---|---|
| Talk2Stock | <http://111.91.30.23:8090/> · 云服务器 Debian 12，2 vCPU / 2 GB，已开 `T2S_REQUIRE_AUTH=1` |
| 原版 ERP | <http://111.91.30.23:8080/>（容器 `erp-web`，报表/权限/财务等仍回原系统） |
| ERP 接口 | <http://111.91.30.23:9999/jshERP-boot> · Swagger `/doc.html` |
| 进程守护 | systemd `talk2stock.service`（`Restart=always`，`ExecStart=/usr/bin/python3 /opt/talk2stock/app/server.py`） |
| 日志 | `/opt/logs/talk2stock.log`（stdout + stderr 追加） |
| 留痕数据 | `app/data/audit.jsonl`（写入/被拒）· `oplog.jsonl`（接口耗时与结果）· `usage.jsonl`（token 分账） |
| 数据库 | 容器 `erp-mysql`（MySQL 8.0，库 `jsh_erp`，卷 `mysql-data`）· 容器 `erp-redis` |

### 从零部署到一台 Linux 服务器

```bash
git clone https://github.com/honlam6/talk2stock.git && cd talk2stock
cp app/.env.example app/.env && $EDITOR app/.env        # ERP 账号密码 + 模型 Key
docker compose -f jshERP-latest/deploy/docker-compose.yml up -d
python3 app/server.py                                    # 前台跑，或交给 systemd（见下）
```

systemd 单元（`/etc/systemd/system/talk2stock.service`，本实例即用此配置）：

```ini
[Unit]
Description=Talk2Stock Conversational ERP Agent
After=network.target docker.service
Wants=docker.service

[Service]
Type=simple
WorkingDirectory=/opt/talk2stock
ExecStart=/usr/bin/python3 /opt/talk2stock/app/server.py
Restart=always
RestartSec=3
StandardOutput=append:/opt/logs/talk2stock.log
StandardError=append:/opt/logs/talk2stock.log

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload && systemctl enable --now talk2stock
```

### 日常运维

```bash
systemctl status talk2stock            # 运行状态
systemctl restart talk2stock           # 改完 app/ 下代码后重启（web/ 静态文件改动免重启）
tail -f /opt/logs/talk2stock.log       # 访问日志 / 异常栈
docker ps --format '{{.Names}}\t{{.Status}}'   # erp-web / erp-backend / erp-mysql / erp-redis
curl -s localhost:8090/api/health      # 返回 need_auth=true 且有 401 = 进程活着且鉴权生效
python3 app/selfcheck.py               # 应用层自检（直连 core 模块）
```

### 公网暴露前置清单

- [x] `T2S_REQUIRE_AUTH=1`：所有 `/api` 需登录（本实例已开）
- [x] `T2S_DEMO_PASS` 已换成自定义口令（默认值等同于公开后门，本实例实测默认口令已失效）
- [x] ERP 初始账号已改密（初始化 SQL 自带的 `123456` 在本实例已登录失败）
- [ ] 前置 Caddy/Nginx 做 HTTPS + 限流；不裸奔 HTTP 端口（本实例当前为 IP:8090 直连，仅作演示）
- [ ] 生产库与演示数据分离：`eval/seed_demo.py` 会清场重建，**绝不能在真实业务库上跑**

### 备份与恢复

```bash
# 1) ERP 业务库（结构与数据，在线一致性快照）
docker exec erp-mysql mysqldump -uroot -p"$MYSQL_ROOT_PASSWORD" --single-transaction \
  jsh_erp > backups/jsh_erp-$(date +%F).sql
# 2) 不入库的留痕与配置（含 .env 密钥，注意保管）
tar czf backups/t2s-state-$(date +%F).tar.gz app/data app/config.json app/.env
# 3) 恢复业务库
docker exec -i erp-mysql mysql -uroot -p"$MYSQL_ROOT_PASSWORD" jsh_erp < backups/jsh_erp-YYYY-MM-DD.sql
```

本实例 `backups/` 已有历史快照（`seed-backup-*.sql`，演示数据重置前自动备份）；详细运维记录见 [`docs/生产部署与运维.md`](docs/生产部署与运维.md)。

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
   | 推理强度 | `reasoning_effort`：minimal / low / medium / high；留空用端点默认。推理模型跑抽取/决策建议 low | `low` |
   | 高级参数 | `extra_body`：厂商私有参数原样进请求体（如 `{"enable_thinking": false}`）；`extra_headers`：附加请求头（网关按头分流时用）；`merge_system`：网关丢弃 system 角色时开；`insecure`：自签证书网关 | 按需 |

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
python3 eval/run.py                    # 全量 60 条冻结语料（只调解析，绝不写库）
python3 eval/run.py --type adversarial # 只跑 12 条对抗样本
python3 eval/run.py --limit 8 --tag smoke  # 冒烟
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
- 基线对比实验（原生界面人工录单 vs Talk2Stock，两场景设计）见
  [`docs/基线对比实验手册.md`](docs/基线对比实验手册.md)，记录表模板 `eval/baseline_timing.csv`。
- 历次结果在 `eval/results/`（JSON + Markdown）。

### 问一问评测（34 条冻结问句）

```bash
python3 eval/run_ask.py                 # 全量（含 agent 多步取证，耗时随模型而定）
python3 eval/run_ask.py --tag smoke     # 冒烟
```

语料 `eval/ask_cases.jsonl`（`ask-v1-20261109` 冻结，34 条）分四类：`rule`（规则直答）、
`agent`（需多工具取证）、`compound`（复合问题）、`boundary`（越界/拒答）。
打分口径：答案是否正确 + 数字是否全部来自工具观察（接地）+ 数字集合是否齐全。
每次改动提示词/工具都留档在 `eval/results/ask_*.md`，这是 v1→v5 的完整证据链：

| 版本 | 提示词 | 总通过率 | rule / agent / compound / boundary | agent 均值延迟 | agent token/问 |
|---|---|---|---|---|---|
| v1 | ask-v1 | 76.5% | 100% / 62% / 67% / 83% | 130.0s | 10774 |
| v5（当前） | ask-v7-20261109 | **94.1%** | 100% / 94% / 67% / 100% | 63.2s | 7685 |

v1→v5 的有效改动（全部由失败用例驱动）：规则路由加**实体护栏**（问句含主数据名时不许
`today_stats` 截胡，交 agent 抽参）、步数预算 6→4（专用工具 1~2 步即答，多余步数只烧钱）、
新增 `query_bills` 工具（按时间窗/往来单位查单据明细）、收紧决策步数预算与决策输出封顶（800→2000，推理模型
思考占满预算会导致正文 JSON 截断 → 连环解析失败）、歧义候选禁止用 SQL 合并成一个总数。

### 规模基准（`eval/stress_scale.py`）

800 商品 / 120 供应商 / 1200 单据的一次实测（`eval/results/stress_*.md`）：
主数据计数 812 = SQL 真实值（自适应翻页修复后三次复测一致）；
`P5` 分仓聚合 1200 张单据 0.71s；`P6` 端到端问答 67.4s 且数字接地通过。

### 已知边界与未解问题

- **单据列表规模边界（open）**：`/depotHead/list` 带 `search` 时翻页不可靠（`currentPage`
  翻页为空、`pageNo` 重复第一页），单类单据超约 200 张时只覆盖最新一页 → 欠款合计会漏老账
  （260 张真实未付单实测漏 23%）。主数据侧已修；单据侧可行方向与触发条件记录在
  `docs/failure-cases.md`（P4）。
- 复合问句（一句话里问两件事）通过率 67%，是当前最弱的一类，失败样本见 `eval/results/ask_*-v5.md`。

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
- ~~CI~~：GitHub Actions（`py_compile` + unittest **112 项** + 前端内联 JS `node --check`）。
- ~~公网部署~~：云服务器实例 <http://111.91.30.23:8090/>，systemd 守护 + 鉴权开启，见[生产部署与运维](#生产部署与运维)。
- ~~登录与权限~~：ERP 账号联动登录（密码不落盘）+ 管理员/成员两级权限 + 私有模型档案 + 防爆破。
- ~~推理强度/厂商参数~~：档案级 `reasoning_effort` / `extra_body` / `extra_headers`，agent 决策步默认 low。
- ~~列表全量拉取~~：主数据自适应翻页（修 500 条截断）；单据侧仍有规模边界（见[已知边界](#已知边界与未解问题)）。
- ~~问答评测体系~~：34 条冻结问句 + `run_ask.py`，通过率 76.5% → 94.1%；规模基准 `stress_scale.py`。

下一步（按对答辩的价值排序）：
1. **基线实验落地**：`docs/基线对比实验手册.md` 的两场景计时（`eval/baseline_timing_slim.csv` 仍是空模板，需人工录两遍同一批单）。
2. **单据侧规模边界**：按 `docs/failure-cases.md` P4 的时间桶方案收口，让欠款合计在 1000+ 单据下也可信。
3. **复合问句**：67% 通过率是问答侧最后一块短板，先把「一句话两件事」拆解做完。
4. 语音输入（浏览器转写，计划书里是可选项）。

---

## 相关文档

- `docs/易用性改造方案.md` — 原系统痛点实测、已实现能力、分阶段改进清单
- `docs/本地部署验证报告.md` — 端到端验证记录与已修复缺陷（含时区、僵死进程）
- `docs/API端点全清单.md` — jshERP 接口实测清单与必填参数坑
- `docs/字段必填清单.md` — 单据字段哪些可自动生成
- `docs/项目合规评估与改进建议.md` — 对照课程要求的完成度评估与改进路线图
- `docs/architecture.md` — 分层架构与三大不变量的完整说明
- `docs/交互流程与边界情况.md` — 从输入到落库的每条分支与边界（A/B/C 编号案例）
- `docs/failure-cases.md` — 失败案例库（含 F10/F11/F12 与 P4 规模边界）
- `docs/证据索引.md` — 每条结论对应的代码位置与实验数据
- `docs/生产部署与运维.md` — 本实例的部署、端口、备份、运维排障记录
