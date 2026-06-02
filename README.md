# AnyKB — 私有 RAG 知识库 + 透明 Agent

> 上传文档 / 抓取网页 → 选中 KB → 用一句话问出来。
> 30 秒内吐一份带原文引用的 markdown 报告，全过程思考链可视。

**版本**：v3.2-admin（2026-06-02 / 后台管理系统）· v3.1.0（2026-05-25 / Docker + HTTPS）· **线上**：https://anykb.cc.cd · **协议**：MIT

---

## 目录

- [一、它能做什么](#一它能做什么)
- [二、架构总览](#二架构总览)
- [三、项目目录](#三项目目录)
- [四、模块说明](#四模块说明)
- [五、数据库现状与迁移路径](#五数据库现状与迁移路径)
- [六、Docker 化方案](#六docker-化方案)
- [七、本地开发](#七本地开发)
- [八、服务器部署（已实战）](#八服务器部署已实战)
- [九、配置项速查](#九配置项速查)
- [十、常见运维](#十常见运维)
- [十一、文档导航](#十一文档导航)

---

## 一、它能做什么

| 能力 | 一句话 |
|---|---|
| **私有 RAG 知识库** | 上传 md / txt / pdf / docx 或抓 URL → 后台异步 ingest → 对话顶部选这个 KB 提问 |
| **多账户隔离** | 本地 JWT，每用户自建 KB；可邀请协作者 owner / editor / viewer 三档权限 |
| **KB 协作 / 分享** | 邮箱邀请已注册用户 + 匿名 token 分享链接（可设过期 + 次数 + 撤销） |
| **真·通用聊天** | 不选 KB 时纯模型直答 + DuckDuckGo `web_search` 实时搜索兜底，引用必须带 URL |
| **KB+Web 混合兜底** | KB 模式可选启用 web_search：KB 强相关（score ≥ 0.7）走 KB；KB 没找到（< 0.4）兜底搜网，答案分【📚 KB】/【🌐 Web】两段 |
| **会话级模型切换** | 每个对话独立保存 LLM model，刷新 / 切对话 / 跨设备都记得 |
| **Per-KB 检索配置** | 每个 KB 自带 embedding（必填）+ 可选 cross-encoder reranker —— 不同 KB 用不同向量模型互不干扰 |
| **混合检索** | Milvus 服务端 BM25 全文检索 + 稠密向量 RRF 融合，关键词查询召回明显改善 |
| **结构化报告** | 旅行 / 通用两种 skill 模板，markdown 报告含 TL;DR + sections + citations |
| **品牌图文分享卡** | 报告一键导出品牌框 PNG（html2canvas + 顶部品牌条 + 问题 chip + 水印），支持复制到剪贴板 |
| **透明 Agent** | 前端实时展示思考链（每步工具调用 / 耗时 / 命中数），不再是黑盒 |
| **流式输出** | SSE 推 token，"正在思考 → 工具跑 → 正在撰写报告" 三段过渡 |
| **BYOK 强制 gate** | `BYOK_REQUIRED=true` 公网部署模式，禁止白嫖 owner 的 API key |
| **后台管理** | `/admin` 管理员后台：统计看板 + 用户管理（封禁 / 设管理员 / 重置密码 / 删除）+ 跨用户 KB 管理；`ADMIN_EMAILS` 指定管理员，仅元数据不看正文 |
| **Docker 化部署** | 4 容器 compose（postgres + backend + frontend + nginx），`./scripts/deploy.sh` 一行起 |
| **完全解耦** | LLM / embedding / 向量库 / App DB 全部 env 驱动或在 `/settings` 改，代码零改动 |

历史完整 changelog 见 [PROGRESS.md](PROGRESS.md)（M0 → v3-M8.2 共 30 个 milestone）。

---

## 二、架构总览

```
浏览器
  │ HTTPS / SSE
  ▼
┌────────────────────────────────┐
│  Next.js 14 (App Router)       │  :3000
│  - 聊天界面（流式 + 思考链）   │
│  - KB 管理 / 协作邀请          │
│  - 系统设置 modal              │
│  - 内置 API proxy 转发到后端   │
└──────────────┬─────────────────┘
               │ proxy
               ▼
┌────────────────────────────────┐
│  FastAPI + LangGraph           │  :8000
│  ├─ Auth (JWT, bcrypt)         │
│  ├─ Conversations REST         │
│  ├─ KB REST (含协作 / 邀请)    │
│  ├─ Settings REST              │
│  └─ /api/chat (SSE)            │
│       └─ Agent 主循环          │
│            plan → call_tools → skill_report
└──┬─────────────────────┬───────┘
   │                     │
   ▼                     ▼
┌──────────────┐    ┌─────────────────────────────┐
│ App DB       │    │ Vector DB                   │
│ SQLite       │    │ Milvus Lite (默认)          │
│ (users / kbs │    │   或 Milvus Standalone      │
│  /docs /msgs)│    │   或 Qdrant                 │
└──────────────┘    └─────────────────────────────┘

外部服务（按 KB / 用户配置）：
  - LLM        : DeepSeek / Claude / OpenAI / SiliconFlow / Ollama
  - Embedding  : SiliconFlow BGE-M3 / OpenAI / Ollama
  - Reranker   : SiliconFlow / Cohere / 自托管 TEI (opt-in)
  - Web Search : DuckDuckGo (ddgs, 免 key)
```

Agent 按所选 KB 切换工具集 — 用户 KB 只挂 `search_kb`；通用聊天挂 `web_search`；系统旅行示例 KB 挂旅行四件套（weather / restaurant_kb / amap / travel_report）。

详见 [docs/architecture.md](docs/architecture.md)。

---

## 三、项目目录

```
ai-agent/
├── backend/                       Python 3.11 / FastAPI
│   ├── src/
│   │   ├── app.py                 FastAPI 入口 + SSE chat 端点 + lifespan
│   │   ├── settings.py            pydantic-settings（.env 驱动）
│   │   ├── auth/                  M1: JWT 认证（注册 / 登录 / me / 改密 / 删号）
│   │   │   ├── models.py          User SQLAlchemy 模型（17 列：4 主 + 5 LLM + 5 embed + 1 reranker_enabled + 2 平台标志 is_admin/is_active）
│   │   │   ├── password.py        bcrypt cost=12
│   │   │   ├── tokens.py          JWT HS256 编解码
│   │   │   ├── middleware.py      CurrentUser + require_admin/AdminUser 依赖（封禁拦截下沉于此）
│   │   │   └── routes.py          /api/auth/{register,login,me,change-password}
│   │   ├── kb/                    M2/M3: 知识库 + 文档 + ingest
│   │   │   ├── models.py          KB / Document / KBMember / KBInvitation
│   │   │   ├── routes.py          /api/kbs/* + /api/invitations/*
│   │   │   ├── ingest.py          后台异步 chunk + embed + upsert
│   │   │   ├── chunker.py         段落→句子→字符递归切分
│   │   │   ├── system_seed.py     启动注册 TravelGPT 演示库
│   │   │   └── parsers/           markdown / pdf (pymupdf) / docx / webpage (trafilatura)
│   │   ├── conversations/         v2-M3: 跨设备会话历史
│   │   │   ├── models.py          Conversation / Message (FK CASCADE)
│   │   │   └── routes.py          /api/conversations/* + bulk import + export
│   │   ├── settings_user/         v2-M1: 每用户自助配置
│   │   │   ├── models.py          UserLLMConfig / UserEmbeddingConfig / UserRerankerConfig
│   │   │   ├── kb_resolvers.py    v3-M7: KB 级 cfg 优先 → user 兜底
│   │   │   ├── probe.py           列模型 / 验 base_url+api_key
│   │   │   ├── gate.py            v2-M2: BYOK 强制 gate
│   │   │   └── routes.py          /api/settings/*
│   │   ├── agent/                 LangGraph 主循环
│   │   │   ├── graph.py           build_graph(kb, llm_cfg, embedding_cfg, reranker_cfg, ...)
│   │   │   ├── nodes.py           plan / call_tools / skill_report
│   │   │   ├── state.py           AgentState TypedDict
│   │   │   └── prompts.py         3 套 system prompt (general / travel / kb)
│   │   ├── tools/                 Agent 工具实现
│   │   │   ├── base.py            ToolRegistry 工厂（按 KB 模式三态切换工具集）
│   │   │   ├── kb_search.py       通用 KB 检索（含 reranker over-fetch + rerank reorder）
│   │   │   ├── web_search.py      DuckDuckGo / ddgs
│   │   │   ├── weather.py         高德天气（旅行示例）
│   │   │   ├── restaurant_rag.py  旅行 KB 检索（MMR）
│   │   │   └── amap_fallback.py   高德 POI 兜底
│   │   ├── skills/                结构化输出模板
│   │   │   ├── loader.py          invoke_skill (LLM JSON 输出)
│   │   │   ├── general_report/SKILL.md   v2-M8: 通用 KB 报告模板
│   │   │   └── travel_report/SKILL.md    旅行报告模板
│   │   ├── safety/                输入清洗 / 输出脱敏 / 工具守卫
│   │   └── infra/                 基础设施抽象层（解耦核心）
│   │       ├── database.py        SQLAlchemy async + init_db + 幂等 ALTER
│   │       ├── vector_store.py    VectorStore Protocol + Qdrant/Milvus/Local 三 adapter
│   │       ├── local_vector.py    SQLite 兜底向量实现
│   │       ├── embedding.py       OpenAI 兼容统一 embed 客户端 + provider 预设
│   │       ├── reranker.py        v3-M4: cross-encoder rerank 客户端
│   │       ├── llm.py             LLM 客户端（Anthropic / OpenAI 兼容）+ pick_model
│   │       ├── crypto.py          Fernet at-rest 加密（key 派自 JWT_SECRET）
│   │       └── rate_limit.py      内存 sliding window
│   ├── data/                      运行时数据（gitignored）
│   │   ├── app.db                 SQLite 应用库
│   │   ├── milvus_local.db        Milvus Lite 向量库
│   │   └── uploads/{kb_id}/       原始上传文件
│   ├── tests/                     smoke (reranker + milvus + graph) + test_admin (28) + conftest（临时 DB/HTTP 夹具）
│   ├── env.example                env 模板（每个字段含注释）
│   └── pyproject.toml             依赖（核心 + dev/ollama/openai/monitoring/milvus extras）
│
├── frontend/                      Next.js 14 App Router + Tailwind
│   ├── app/
│   │   ├── page.tsx               主聊天页（KB+Model selector + Sidebar + Chat）
│   │   ├── welcome/               未登录落地页
│   │   ├── login/ register/       双栏登录注册 + ?next= 回跳
│   │   ├── kbs/                   KB 列表 + 详情（含成员管理 / 邀请 / 高级设置）
│   │   ├── settings/              v3-M8 极简后只剩 LLM 凭据 + KB 选项
│   │   ├── invite/[token]/        v2-M9: 匿名邀请落地页
│   │   ├── api/                   catch-all proxy → 后端 :8000
│   │   └── layout.tsx             ThemeProvider + Toaster + Plausible 钩子
│   ├── components/
│   │   ├── Sidebar.tsx            对话列表 + 重命名 + 底部 UserMenu
│   │   ├── ChatBox.tsx            自适应高度 + Stop 按钮
│   │   ├── MessageBubble.tsx      用户/助手气泡 + 流式占位
│   │   ├── ThinkingChain.tsx      工具调用思考链可视
│   │   ├── ReportView.tsx         markdown 报告渲染（prose-tg）
│   │   ├── ExportActions.tsx      复制 MD / 导 PDF / 图文分享
│   │   ├── ShareCardDialog.tsx    品牌图文卡 portal 模态
│   │   ├── SystemSettingsDialog.tsx  v3-M5: DeepSeek 风 4-tab 设置 modal
│   │   ├── Select.tsx Dialog.tsx Brand.tsx ThemeToggle.tsx ...
│   │   └── CreateKbDialog.tsx     v3-M7/8.2: 含 Embedding+Reranker 配置 + 测试连接
│   ├── lib/
│   │   ├── auth.ts                token + authFetch + per-user storage key
│   │   ├── sseClient.ts           fetch+ReadableStream 手动 SSE 解析
│   │   ├── conversations-api.ts   会话 CRUD wrapper
│   │   ├── kb-api.ts              KB / 文档 / 成员 / 邀请 wrapper
│   │   ├── settings-api.ts        LLM / Embedding / Reranker probe + save
│   │   ├── conversationStore.ts   Message 类型 + deriveTitle
│   │   ├── byok-toast.ts          v2-M2: BYOK 422 → toast「去配置」
│   │   ├── storage-migrate.ts     一次性 anykb:* 命名空间迁移
│   │   ├── cn.ts                  clsx + tailwind-merge
│   │   └── theme.ts               class 策略 dark mode
│   ├── tailwind.config.ts         语义化 token + dark class 策略
│   └── package.json               (next 14 / react 18 / tailwind 3.4)
│
├── data/                          策展示例数据
│   ├── seed/{shanghai,beijing,chengdu,hangzhou}.json   20 家餐厅
│   ├── schema.json                seed 数据 schema
│   └── ingest.py                  独立 seed → vector store 入库脚本
│
├── docs/                          完整文档
│   ├── architecture.md            内部架构 / 状态图 / 解耦设计
│   ├── deploy.md                  本地启动 + 服务器部署 + 换部件
│   ├── rag-primer.md              小白入门：RAG / Embedding / BM25 / Hybrid / Rerank
│   ├── milvus-guide.md            Milvus 向量库详解
│   ├── curation-sop.md            策展 SOP
│   └── tutorial.md                端到端流程教学
│
├── docker-compose.yml             4 服务编排（postgres + backend + frontend + nginx）
├── env.docker.example             Docker 部署 env 模板
├── nginx/anykb.conf               nginx 反代配置（SSE-safe）
├── scripts/                       运维脚本
│   ├── deploy.sh                  build + up + 健康检查
│   ├── backup.sh                  备份 PG + backend-data 卷
│   └── logs.sh                    tail -f 服务日志
├── start_local.bat / start_local.sh   一键本地启动（非 Docker）
├── start.py                       Python 启动器
├── PROGRESS.md                    完整 changelog（M0 → v3-M8.2 + Docker 化）
└── README.md                      本文档
```

---

## 四、模块说明

### 后端核心模块

| 模块 | 职责 | 关键文件 |
|---|---|---|
| `auth/` | 本地账户、JWT 签发、密码哈希、CurrentUser/require_admin 依赖、ADMIN_EMAILS 启动 seed | `routes.py`, `middleware.py`, `admin_seed.py` |
| `admin/` | **v3.2** 后台管理 API：stats / 用户管理 / 跨用户 KB 管理 + 自我保护不变量（全 `AdminUser` 守卫，仅元数据） | `routes.py` |
| `kb/` | 知识库 / 文档 CRUD、4 种格式解析、后台 ingest、协作邀请 | `routes.py`, `ingest.py`, `parsers/` |
| `conversations/` | 跨设备会话历史，含 bulk import + JSON export | `routes.py`, `models.py` |
| `settings_user/` | 每用户自助配 LLM / Embedding / Reranker，含 probe + Fernet 加密 | `routes.py`, `probe.py` |
| `agent/` | LangGraph 主循环 + 3 套 system prompt + 三态路由 | `graph.py`, `nodes.py` |
| `tools/` | KBSearchTool / WebSearchTool / 旅行四件套 + ToolRegistry 工厂 | `base.py`, `kb_search.py` |
| `skills/` | invoke_skill 二级 LLM 调用生成结构化报告 (JSON → markdown) | `loader.py`, `*/SKILL.md` |
| `safety/` | 输入 prompt-injection 过滤 / 输出 PII 脱敏 / 工具白名单守卫 | `input_filter.py`, `output_filter.py` |
| `infra/` | DB / 向量库 / Embedding / Reranker / LLM / 加密 / 速率 — **解耦核心层** | `vector_store.py`, `embedding.py` |

### 前端核心模块

| 模块 | 职责 |
|---|---|
| `app/page.tsx` | 主聊天页（含 KB selector + Model selector + Sidebar） |
| `app/kbs/` | KB 列表 / 详情 / 成员管理 / 邀请 Dialog |
| `app/settings/` | LLM 凭据卡 + KB 选项（v3-M8 精简后） |
| `app/admin/` | **v3.2** 后台管理三页（看板 / 用户 / KB）+ `AdminShell` 客户端守卫；普通用户 Sidebar 无入口 |
| `components/SystemSettingsDialog.tsx` | DeepSeek 风 4-tab 系统设置 modal（通用 / 账号 / 数据 / 关于） |
| `components/CreateKbDialog.tsx` | 含 embedding + reranker 配置 + 强制"测试连接"才能保存 |
| `components/ThinkingChain.tsx` | 工具调用链实时可视化（运行中跳秒） |
| `components/ShareCardDialog.tsx` | 品牌框图文分享卡（html2canvas → PNG） |
| `lib/sseClient.ts` | 手动解析 SSE（替代浏览器 EventSource，支持 POST + Bearer auth） |

---

## 五、数据库现状与迁移路径

### 当前部署形态

项目支持两种部署模式，**自动按 env 切换，业务代码 0 差异**：

| 模式 | App DB | Vector DB | 适用 |
|---|---|---|---|
| **本地开发** | SQLite + aiosqlite（单文件 `backend/data/app.db`） | Milvus Lite（嵌入式 `backend/data/milvus_local.db`） | 单人调试、零依赖、Windows 原生跑 |
| **生产 Docker**（线上实例采用） | **PostgreSQL 16** 容器（独立服务） | **Milvus Lite**（仍嵌在 backend 容器内，volume 持久化） | 多用户、商业级、可滚动升级 |

> **线上为什么不直接上 Milvus Standalone？** 服务器内存只有 1.9GB，Standalone 需要 etcd + minio + milvus 三容器 ≥ 2GB。当前 Milvus Lite 完全够用（万级 chunk 性能稳定），未来上更大服务器再切（见下方"切到 Milvus Standalone"）。

### 为什么 SQLite 不适合生产

| 限制 | 影响 |
|---|---|
| SQLite 写锁是单线程 | 并发上传 / 写聊天历史时容易触发 `database is locked` |
| SQLite 文件不支持网络访问 | 多 worker / 多容器无法共享同一份 DB |
| 无副本 / failover / point-in-time recovery | 数据丢了就丢了 |

所以**线上必须用 PostgreSQL**，本地开发 SQLite 够用。

### 切换 / 升级路径

**强解耦设计早就为换 DB 留好了入口**：SQLAlchemy 抽象层 + VectorStore Protocol，**改 1~2 个 env 变量即可，业务代码 0 改动**。

#### PostgreSQL（已在线上生效）

```bash
# Docker backend 已预装 asyncpg；本地需要：pip install asyncpg
DATABASE_URL=postgresql+asyncpg://anykb:strong_password@postgres:5432/anykb
```

所有表定义（User / KB / Document / Conversation / Message / KBMember / KBInvitation）都是 SQLAlchemy 2.x Mapped 风格，跨 dialect 通用。**加列迁移**：`_migrate_additive_columns` 在每次启动 `create_all` 之后跑，缺列才 `ALTER TABLE ADD COLUMN`，幂等。

> ⚠️ **新布尔列必须用 portable 字面量 `DEFAULT FALSE/TRUE`**（不是 `0/1`）。往**已存在的生产表**追加布尔列时（如 v3.2 的 `users.is_admin/is_active`），ALTER 会**真的在 Postgres 执行**，整数 `DEFAULT 0/1` 会触发 `boolean ≠ integer` 报错；`FALSE/TRUE` 在 Postgres 原生、SQLite ≥3.23 也支持，两方言通吃。早期那些 `DEFAULT 0/1` 的布尔列只在 SQLite 验证过——生产 Postgres 当年是 `create_all` 一次性建表，那些 ALTER 分支从没在 PG 上真正跑过；v3.2 是第一次在生产 Postgres 上真正执行加列 ALTER，已验证通过。

#### Milvus Standalone（向量 DB 升级，未做）

服务器内存够时（≥ 4GB）值得切，可拿到更强的 index 调优 + 副本：

```bash
# 1. 在 docker-compose.yml 加 milvus / etcd / minio 三服务（参考官方 milvus-standalone-docker-compose.yml）
# 2. backend service env：
VECTOR_STORE=milvus
MILVUS_URI=http://milvus:19530
MILVUS_TOKEN=
```

`MilvusStore` adapter 对 Lite / Standalone / Zilliz Cloud 行为完全一致 — 这是 v3-M2 设计时刻意保留的兼容性。**数据迁移**：向量数据无法跨实例直接复制；最干净的做法是 KB 详情页点「重建索引」按钮（v3-M3 加的功能）→ 走 ingest pipeline 重新生成。

#### Zilliz Cloud（托管 Milvus，0 运维）

```bash
VECTOR_STORE=milvus
MILVUS_URI=https://your-cluster.api.gcp-us-west1.zillizcloud.com
MILVUS_TOKEN=your_cluster_api_key
```

免费层 5 GB 够用很久。

#### Qdrant（如果偏好）

```bash
VECTOR_STORE=qdrant
QDRANT_URL=http://qdrant:6333
QDRANT_API_KEY=
```

**注意**：Qdrant adapter 没有 hybrid 检索（v3-M3 是 Milvus 专属服务端 BM25），切 Qdrant 后自动降级 dense-only。

### 推荐生产组合

```
Backend (1~N worker) ──► PostgreSQL 15+   (App DB, 关系数据)
                    ──► Milvus Standalone (Vector DB, chunk 向量)
                    ──► SiliconFlow API   (Embedding + Reranker)
                    ──► Anthropic / DeepSeek API (LLM)
```

PG + Milvus Standalone 是被 1000+ 公司验证过的组合，资源占用合理（PG 内存 256MB 起、Milvus Standalone 2GB 起就能跑几十万向量）。

---

## 六、Docker 化方案

### 当前实现：4 服务 compose

仓库根目录已包含完整的 docker 化文件，**在你服务器上一行 `./scripts/deploy.sh` 就能起整套栈**。

| 服务 | 镜像 | 内容 |
|---|---|---|
| `postgres` | `postgres:16-alpine` | App DB（用户 / KB / 文档 / 会话）→ volume `anykb_postgres-data` |
| `backend` | 自建（`./backend/Dockerfile`） | FastAPI + LangGraph + **Milvus Lite 嵌入式** → volume `anykb_backend-data`（`/app/data/milvus_local.db` + uploads） |
| `frontend` | 自建（`./frontend/Dockerfile`） | Next.js 14 standalone build（multi-stage，~150MB） |
| `nginx` | `nginx:1.27-alpine` | 反代 :80，`/api/chat` 关 buffering 透传 SSE |

```
┌────────────────────────────────────────────┐
│           nginx:80 (host:80)               │
│   /api/chat → backend (SSE buffering off)  │
│   /api/...  → backend                      │
│   /        → frontend                      │
└──────────────────┬─────────────────────────┘
                   │ docker network: anykb_default
   ┌───────────────┼───────────────┐
   ▼               ▼               ▼
postgres        backend         frontend
(:5432)         (:8000)         (:3000)
   ▲                │
   │                │ asyncpg
   └────────────────┘
```

### 关键文件

| 文件 | 作用 |
|---|---|
| `docker-compose.yml` | 4 服务编排 + volume + 健康检查 + env 注入 |
| `backend/Dockerfile` | Python 3.11 slim + `pip install .[milvus] asyncpg` + healthcheck `/health` |
| `frontend/Dockerfile` | 两阶段 build：builder npm ci + build / runner 只装 `.next/standalone`（~150MB） |
| `nginx/anykb.conf` | upstream `backend:8000` + `frontend:3000` + SSE-safe 配置 |
| `env.docker.example` | 模板：POSTGRES_PASSWORD / JWT_SECRET / PUBLIC_URL / BYOK_REQUIRED / ADMIN_EMAILS |
| `scripts/deploy.sh` | build + up + healthcheck + 日志（**主入口**） |
| `scripts/backup.sh` | 备份 PG + backend-data volume 到 tarball |
| `scripts/logs.sh` | tail -f 指定服务日志 |

### 启动步骤

```bash
# 1. 准备 .env 文件
cp env.docker.example .env
# 编辑 .env：
#   POSTGRES_PASSWORD=$(openssl rand -hex 16)
#   JWT_SECRET=$(openssl rand -hex 32)
#   PUBLIC_URL=http://你的IP或域名

# 2. 一行起栈
./scripts/deploy.sh

# 3. 看健康检查 + 日志
docker compose ps
./scripts/logs.sh backend

# 4. 公网验证
curl http://localhost/health
```

**`./scripts/deploy.sh` 做了什么**：
1. 检查 `.env` 存在
2. `docker compose build`（自动用 layer cache，未变的层秒过）
3. `docker compose up -d --remove-orphans`
4. 等 10s healthcheck
5. 打印容器状态 + backend 日志

### Docker 镜像生命周期（FAQ）

> **"镜像掉了或服务器重启了，要重新构建吗？"**

**不要**。镜像由 docker daemon 持久化在 `/var/lib/docker/`，独立于容器生命周期：

| 操作 | 影响镜像？ | 影响容器？ |
|---|---|---|
| `docker compose up -d` | 不存在才 build，存在直接复用 | 创建并启动 |
| `docker compose down` | ❌ 不删 | ✅ 删（数据 volume 保留） |
| `docker compose restart` | ❌ 不删 | 重启 |
| 服务器重启 | ❌ 不删 | docker daemon 按 restart policy 自动起 |
| `docker rmi` / `docker system prune` | ✅ 删 | — |

所以正常运维不需要"重建"。只有以下场景需要：
- **代码变了** → `./scripts/deploy.sh`（增量 build，秒级 cache）
- **磁盘清空过** → `./scripts/deploy.sh`（全量 build，几分钟）
- **想换 Python 依赖版本** → 同上

### 数据持久化

| Volume | 内容 | 备份命令 |
|---|---|---|
| `anykb_postgres-data` | PG 全部数据 | `./scripts/backup.sh` |
| `anykb_backend-data` | Milvus Lite `.db` + 用户上传文件 | 同上 |

`./scripts/backup.sh` 会同时打 PG + backend-data 两个 tarball 到 `./backups/`。

### 升级 / 回滚 / 滚动重启

```bash
# 改代码后部署
./scripts/deploy.sh

# 只重建 backend（前端没变）
./scripts/deploy.sh backend

# 滚动重启（不重建，只重启容器）
docker compose restart backend

# 完全停止（数据 volume 保留）
docker compose down

# 完全清掉（包括数据 volume，慎用）
docker compose down -v
```

---

## 七、本地开发

### 前置依赖

- **Python 3.11+**
- **Node.js 20+** + npm（或 pnpm）
- 可选：Docker（如果走 Qdrant Docker）

### 3 个进程启动

```bash
# 1. 后端
cd backend
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e '.[milvus]'         # 默认带 Milvus Lite
cp env.example .env                # 编辑填关键 key
python -m uvicorn src.app:app --host 0.0.0.0 --port 8000

# 2. 前端（新窗口）
cd frontend
npm install
npm run dev                        # :3000

# 3. 浏览器进 http://localhost:3000
#    注册 → 登录 → 选「TravelGPT 演示库」试问 "5月13号上海，想找做酸菜鱼的店"
```

### 一键启动（推荐：Docker）

```bash
cp env.docker.example .env
# 编辑 .env 填 POSTGRES_PASSWORD / JWT_SECRET / PUBLIC_URL
./scripts/deploy.sh
```

详见 [docs/deploy.md](docs/deploy.md)。

---

## 八、服务器部署（已实战）

**线上实例**：https://anykb.cc.cd （43.163.245.206 / Ubuntu 24.04 / 2 核 / 1.9 GB / 50 GB）

### 当前部署架构（Docker compose）

2026-05-25 已从 systemd 切换到 Docker。架构 = §6 的 4 服务全栈：

```
公网 :80
  │
  ▼
┌──────────────────────────────────┐
│ nginx (nginx:1.27-alpine)        │ host 端口 80
│   /api/chat → backend (SSE off)  │
│   /api      → backend            │
│   /         → frontend           │
└──┬─────────────┬─────────────────┘
   ▼             ▼
backend       frontend
(FastAPI +    (Next.js
 Milvus       standalone)
 Lite 嵌入式)
   │
   ▼
postgres (postgres:16-alpine)
  → volume anykb_postgres-data

data volumes:
  anykb_postgres-data   (PG 数据)
  anykb_backend-data    (Milvus Lite + uploads)
```

### 部署清单

| 项 | 状态 |
|---|---|
| ✅ docker compose 4 服务全栈 healthy | `restart: unless-stopped` 自启 |
| ✅ PostgreSQL 16 替代 SQLite | volume 持久化 |
| ✅ Milvus Lite 嵌入 backend 容器 | volume 持久化 |
| ✅ nginx 反代 + SSE buffering off + 60M body limit | `nginx/anykb.conf` |
| ✅ **HTTPS / Let's Encrypt 证书**（2026-05-25 上线） | `/etc/letsencrypt/live/anykb.cc.cd/`，TLSv1.2+1.3 + HSTS 1 年 |
| ✅ **HTTP → HTTPS 301 自动跳** | nginx :80 block 全部 redirect |
| ✅ **certbot 自动续期**（每 60 天自动） | systemd timer + renewal-hooks 自动停启 nginx 容器，downtime ~30s |
| ✅ UFW 防火墙仅开 22 + 80 + 443 | 已启用 |
| ✅ SSH key | `~/.ssh/anykb_deploy` |
| ✅ ubuntu 加入 docker 组 | 下次 SSH 登录免 sudo |
| ✅ scripts/ 一键运维 | `deploy.sh` / `backup.sh` / `logs.sh` |
| ⏳ 改 root / ubuntu 密码 + 禁用密码登录 | TODO |
| ⏳ 定时备份 cron（每日调 `backup.sh`） | TODO |
| ⏳ Milvus Lite → Standalone | 等更大服务器或数据规模到瓶颈 |

### 服务器初次部署步骤（如果是全新服务器）

```bash
# 1. 装 docker（Ubuntu）
sudo apt update && sudo apt install -y docker.io docker-compose-plugin
sudo systemctl enable --now docker
sudo usermod -aG docker $USER  # 重新登录生效

# 2. 拉代码（或 tar pipe 同步）
mkdir -p ~/anykb && cd ~/anykb
# scp / rsync / git clone 你的源码到这里

# 3. 准备 .env
cp env.docker.example .env
vim .env  # 填 POSTGRES_PASSWORD / JWT_SECRET / PUBLIC_URL

# 4. 启动
./scripts/deploy.sh
```

### 升级后端代码

本地改完代码 → tar pipe 同步 → 服务器跑 deploy.sh：

```bash
# 本地（在项目根目录）
tar -cz backend/src | ssh user@server 'cd ~/anykb && tar -xz'

# 服务器
ssh user@server
cd ~/anykb && ./scripts/deploy.sh backend
```

### 推荐后续动作

1. **改密码 + 关 SSH 密码登录**（5 分钟） — `passwd ubuntu` + `sudo vi /etc/ssh/sshd_config` 设 `PasswordAuthentication no`
2. **定时备份** — `crontab -e` 加 `0 3 * * * cd /home/ubuntu/anykb && ./scripts/backup.sh /home/ubuntu/anykb-backups`
3. **多 worker** — `backend` Dockerfile CMD 加 `--workers 4`（PG 已经能支持，不再有 SQLite 写冲突）
4. **Milvus Standalone** — 数据上百万级再考虑（见 §5 切换步骤）

### 启用后台管理（Admin Dashboard）

`/admin` 后台只对管理员开放。**第一个管理员通过 env 引导，之后可在后台互相增减**（env 仅作启动兜底，不是唯一来源）。

**首次开通（bootstrap）：**

```bash
# 1) 先在网站正常注册一个账号（管理员本身也是普通用户，env 只提升“已注册”的账号）

# 2) 在根 .env（Docker）或 backend/.env（本地）加白名单，多个逗号分隔：
ADMIN_EMAILS=you@example.com,ops@example.com

# 3) 重新部署 / 重启 backend，启动时自动 seed：
./scripts/deploy.sh
#    backend 日志出现  admins_seeded ... promoted=N  即生效
```

完成后该账号登录 → 左下角用户菜单出现 **「后台管理」** 入口 → 进 `/admin`（普通用户看不到入口，直接访问 `/admin` 会被弹回首页，服务端 403 为最终防线）。

**三个页面能做什么（只读元数据 / 计数，绝不暴露会话正文或 KB 文本）：**

| 页 | 能力 |
|---|---|
| 看板 | 用户总数 / 活跃 / 封禁 / 管理员 / 近 7 天新增 + KB（含 system）/ 文档 / 会话 / 消息 计数 |
| 用户 | 列表（分页，每行 KB/会话计数 + 是否配过 LLM）+ 封禁·解封 / 设·取消管理员 / 重置密码 / 删除（连带清其 KB/会话） |
| 知识库 | 跨用户列出所有 KB（含 owner 邮箱）+ 删除（system KB 禁删，含向量 collection 清理） |

**安全不变量（服务端强制，违反返 400/409）：** 不能封禁/降级/删除**自己**；不能动掉**最后一个活跃管理员**；**system KB 不可删**。被封账号（`is_active=false`）无法登录、所有受保护接口返回 403。每个写操作落一行 structlog `admin_action`（actor / action / target）审计。

> **平台标志列** `users.is_admin`（默认 false）/ `is_active`（默认 true）由启动迁移自动补到现有库，旧用户默认“普通且启用”，无人受影响（布尔列用 portable `DEFAULT FALSE/TRUE`，见 §5）。

### HTTPS 部署细节（已生效）

域名 `anykb.cc.cd` → Let's Encrypt 免费证书：

```bash
# 证书路径（容器内只读挂载）
/etc/letsencrypt/live/anykb.cc.cd/fullchain.pem
/etc/letsencrypt/live/anykb.cc.cd/privkey.pem

# 自动续期机制
systemd timer:  certbot.timer (每天 20:47 检查)
pre-hook:       /etc/letsencrypt/renewal-hooks/pre/anykb-stop-nginx.sh
post-hook:      /etc/letsencrypt/renewal-hooks/post/anykb-start-nginx.sh

# 实际续期时间：到期 < 30 天才会真续；约 60 天一次，每次 nginx ~30s downtime（凌晨）

# 手动验证续期流程
sudo certbot renew --dry-run

# 手动续期（紧急用）
sudo certbot renew
```

完整部署细节见服务器上 `/home/ubuntu/anykb-deploy-notes.md`。

---

## 九、配置项速查

### 后端 env（`backend/.env`）

| 类别 | 字段 | 默认 / 示例 | 说明 |
|---|---|---|---|
| **LLM** | `ANTHROPIC_API_KEY` | `sk-ant-...` | env fallback（用户可在 /settings 覆盖） |
| | `DEEPSEEK_API_KEY` | `sk-...` | 同上 |
| | `LLM_DEFAULT_MODEL` | `claude-haiku-4-5-20251001` | 默认轻量模型 |
| | `LLM_COMPLEX_MODEL` | `claude-sonnet-4-6` | 复杂任务用（plan_node 自动切） |
| **向量库** | `VECTOR_STORE` | `milvus` | `milvus` / `qdrant` / `local` |
| | `MILVUS_URI` | `./data/milvus_local.db` | Lite 文件路径 / Standalone HTTP |
| | `MILVUS_TOKEN` | (空) | Zilliz Cloud / 鉴权 |
| | `QDRANT_URL` | `http://localhost:6333` | Qdrant 时用 |
| **Embedding** | `EMBEDDING_PROVIDER` | `openai` | 默认 fallback |
| | `EMBEDDING_MODEL` | `text-embedding-3-small` | 同上 |
| **App DB** | `DATABASE_URL` | `sqlite+aiosqlite:///./data/app.db`（本地）/ `postgresql+asyncpg://...`（Docker） | Docker compose 自动注入 |
| **Auth** | `JWT_SECRET` | (必填) | 32 字节随机十六进制 |
| | `JWT_EXPIRE_MINUTES` | `10080` | 7 天 |
| **后台管理** | `ADMIN_EMAILS` | (空) | 逗号分隔邮箱，启动时 seed 为管理员（账号须先注册）；运行时也可由现有管理员在 `/admin` 增减 |
| **BYOK gate** | `BYOK_REQUIRED` | `false` | **公网部署必设 true** |
| **加密** | (派生自 `JWT_SECRET`) | — | Fernet at-rest 加密所有 api_key |
| **CORS** | `CORS_ORIGINS` | `http://localhost:3000` | 多个用逗号分隔 |
| **限流** | `RATE_LIMIT_PER_HOUR` | `20` | 每用户每小时 chat 上限 |
| **监控** | `LOGFIRE_TOKEN` | (空) | 可选 Logfire backend tracing |

### 前端 env（`frontend/.env.local`）

```ini
BACKEND_URL=http://127.0.0.1:8000           # 后端地址（前端 proxy 转发）
NEXT_PUBLIC_APP_NAME=AnyKB                  # 显示名
NEXT_PUBLIC_PLAUSIBLE_DOMAIN=               # 可选 Plausible analytics
NEXT_TELEMETRY_DISABLED=1                   # 关掉 next telemetry
```

---

## 十、常见运维

### 起 / 停 / 看日志（Docker 栈）

```bash
# 一键起 / 重建（核心入口）
./scripts/deploy.sh                # 全栈
./scripts/deploy.sh backend        # 只 backend

# 看日志
./scripts/logs.sh backend          # tail -f backend
./scripts/logs.sh all              # 全部服务

# 重启容器（不重建镜像）
docker compose restart backend

# 看状态
docker compose ps

# 完全停（数据 volume 保留）
docker compose down

# 完全清（包括数据，慎用）
docker compose down -v
```

### 起 / 停 / 看日志（本地非 Docker）

```bash
# Ctrl+C 停；删 backend/data/app.db 重启 → 全新数据库
cd backend && python -m uvicorn src.app:app --port 8000
```

### 备份 / 恢复

```bash
# Docker 模式：备份两个 volume 到 ./backups/
./scripts/backup.sh
# 输出：
#   ./backups/anykb-pg-2026-05-25-1430.tgz       (PG 全量)
#   ./backups/anykb-data-2026-05-25-1430.tgz     (Milvus Lite + uploads)

# 恢复（停服 → 替换 volume → 起服）
docker compose down
docker run --rm -v anykb_postgres-data:/dst -v $(pwd)/backups:/src alpine \
  tar xzf /src/anykb-pg-2026-05-25-1430.tgz -C /dst
docker run --rm -v anykb_backend-data:/dst -v $(pwd)/backups:/src alpine \
  tar xzf /src/anykb-data-2026-05-25-1430.tgz -C /dst
./scripts/deploy.sh
```

### 重置 DB（删数据 + 重建）

```bash
docker compose down -v               # -v 关键：删 volume
./scripts/deploy.sh                  # init_db() 自动建表 + seed TravelGPT 演示库
```

### 跑测试

```bash
cd backend
pytest                          # 11 测（test_reranker_smoke + test_milvus_smoke）
pytest -v -k milvus             # 跑 Milvus smoke
```

### 修密 / 删账号 / 导出对话

全部在前端 Sidebar 底部 user card → 设置 modal → 账号 / 数据 tab 完成（v3-M5 加的）。

---

## 十一、文档导航

| 文档 | 用途 |
|---|---|
| [PROGRESS.md](PROGRESS.md) | 完整开发日志 + 30 个 milestone changelog |
| [docs/architecture.md](docs/architecture.md) | 内部架构 / Agent 状态图 / 解耦设计 |
| [docs/deploy.md](docs/deploy.md) | 详细部署（含 systemd / nginx / Linux 迁移） |
| [docs/rag-primer.md](docs/rag-primer.md) | **入门**：RAG / Embedding / BM25 / Hybrid / Rerank 从零讲 |
| [docs/milvus-guide.md](docs/milvus-guide.md) | Milvus 向量库定义 / 部署形态 / 集成 AnyKB |
| [docs/curation-sop.md](docs/curation-sop.md) | 添加 / 维护策展数据 |
| [docs/tutorial.md](docs/tutorial.md) | 端到端流程教学（适合给团队培训用） |

---

## 致谢 / 协议

- Vibe project · MIT 协议
- Powered by Claude / DeepSeek / FastAPI / LangGraph / Next.js / Milvus / SiliconFlow
- 不隶属任何组织 / 公司，独立维护

如果这个项目对你有帮助，star 一下，欢迎 PR。
