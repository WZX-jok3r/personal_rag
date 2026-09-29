# Personal RAG · 企业级多租户检索增强问答系统

> 前后端分离的全栈 RAG 平台：FastAPI 异步后端 + Vue3 SPA + PostgreSQL/Redis/Qdrant 存储 + ARQ 异步入库 + Docker Compose 一键编排。支持**混合检索 + 精排**、**多租户数据隔离**、**流式多轮问答**、**大文件后台入库**与**离线评测门禁**。

---

## 一、技术栈

| 层次 | 选型 |
|---|---|
| **前端** | Vue 3 + TypeScript + Vite + Pinia；markdown-it 渲染；原生 `fetch` + `ReadableStream` 处理 SSE 流式 |
| **API 层** | FastAPI + Pydantic v2；应用工厂 + `lifespan` 生命周期；统一异常处理；OpenAPI 文档 |
| **RAG 引擎** | 自研检索链路：稠密向量 + BM25 稀疏 + RRF 融合 + 交叉编码器精排 + 低相关过滤 |
| **模型服务** | SiliconFlow：Embedding `BAAI/bge-m3`(1024d) · LLM `DeepSeek-V3.2` · Reranker `bge-reranker-v2-m3` |
| **向量库** | Qdrant（named vectors：`dense` + `bm25` 稀疏；payload 索引；多值 keyword ACL） |
| **关系/缓存** | PostgreSQL 16（事实来源）+ Redis 7（会话热缓存 / ARQ 队列） |
| **持久化 ORM** | SQLAlchemy 2.0（全异步 `asyncpg`）+ Alembic 迁移（`psycopg` 同步通道） |
| **异步任务** | ARQ（Redis 后端）worker，与 API 共享同一镜像、不同入口命令 |
| **文档解析** | PDF / DOCX / XLSX / Markdown / TXT；表格行级语义化；marker 兜底转换（可降级） |
| **可观测** | Langfuse（trace / span 软埋点，未配置零开销） |
| **部署** | Docker Compose 六服务编排；nginx 托管 SPA + 同源反代 + SSE 关缓冲 |
| **配置** | pydantic-settings 单一事实来源，`.env` 扁平变量 100% 兼容、12-factor 容器覆盖 |

---

## 二、整体架构

```
                         ┌──────────────────────────────────────────┐
   Browser  ── HTTP ───► │  frontend  (nginx :8080)                 │
                         │  • 托管 Vue3 SPA 静态产物                │
                         │  • 同源反代 /api/v1 → backend:8000       │
                         │  • SSE：proxy_buffering off（逐帧透传）  │
                         └───────────────────┬──────────────────────┘
                                             │
                         ┌───────────────────▼──────────────────────┐
                         │  backend  FastAPI (:8000)                │
                         │  api → services → repositories → models  │
                         │  RAG Pipeline · 鉴权 · 会话 · 入库编排   │
                         └───┬───────────────┬──────────────┬───────┘
                             │               │              │ enqueue
                   ┌─────────▼───┐   ┌───────▼──────┐   ┌───▼────────────┐
                   │ PostgreSQL  │   │   Qdrant     │   │  Redis (队列)  │
                   │ 文档/会话/  │   │ dense+bm25   │   │  会话热缓存    │
                   │ 任务状态    │   │ +tenant ACL  │   │  ARQ job queue │
                   └─────────────┘   └───────▲──────┘   └───┬────────────┘
                                             │              │ consume
                             ┌───────────────┴──────────────▼───────┐
                             │  worker  (ARQ, 同镜像不同命令)       │
                             │  解析 → 分块 → Embedding → 索引      │
                             └──────────────────────────────────────┘
```

**分层职责（后端）**：`api`（路由/依赖注入）· `core`（配置/鉴权/异常/日志）· `schemas`（DTO）· `services`（业务编排）· `repositories`（数据访问）· `models`（ORM）· `rag`（检索生成链路）· `vector`（Qdrant/Embedding）· `ingestion`（加载/分块/索引）· `llm`（模型客户端）· `worker`（异步任务）· `cache`（Redis）· `observability`（Langfuse）。

---

## 三、核心链路

### 1. 问答链路（读）
`query / chat / chat(stream)` → **鉴权解析租户** → `Retriever`（Qdrant 混合召回 → 精排 → 低相关过滤，全程叠加 `tenant_id` 强制过滤）→ `context_builder` 组装 → `LLM` 生成 → `source_formatter` 溯源。流式版本依次产出 `meta → delta* → done` 事件。

### 2. 入库链路（写，异步）
`POST /documents`（multipart）→ 落盘知识库 + 建 `Document(pending)/IngestTask(queued)` + 入队 → **立即 202 返回 `task_id`** → worker 后台 解析→分块→向量化→写 Qdrant→回写 PG 状态（`running→done/failed`）→ 客户端凭 `task_id` 轮询。

### 3. 会话链路
`session_id` 驱动多轮：PostgreSQL 存历史为**事实来源**，Redis 作热缓存；前端仅回传 `session_id` 即维持上下文，并按租户指纹隔离，切租户自动换会话。

---

## 四、项目亮点与难点

### 🔍 检索质量：两级混合检索 + 精排
- **稠密 + BM25 稀疏双通道**（Qdrant named vectors），**RRF 排名融合**兼顾语义与关键词精确匹配；中文经 jieba 预分词与英数串保护（型号/SKU 整体成 token），入库与查询**对称分词**。
- **交叉编码器精排**：先召回候选（`prefetch_k`/`rerank_candidates`）再按 (query, doc) 相关性重排取 top_k；精排 API 异常**自动降级**为原始排序，保证可用性。
- **离线评测门禁**：100+ 条 QA 评测集，retrieval-only 口径 **Recall@5 ≈ 92%、MRR ≈ 0.90**，重构前后逐项对齐。

### 🛡️ 难点一：量纲安全的低相关过滤
RRF 融合分、原始向量分、rerank 绝对分**量纲不可跨 query 比较**——套用统一绝对阈值会误杀或漏杀。方案：**仅对 rerank 绝对相关分**启用「绝对下限 + 强锤点相对断层 + 保底保留 N 条」的组合阈值策略，非 rerank 量纲只截断不设限；被过滤条数沿链路**透传前端**展示"已隐藏 N 条"，兼顾"宁缺毋滥"与"可解释"。

### 🏢 难点二：多租户数据隔离（单集合 + 字段级 ACL）
- 采用**单 collection + `tenant_id` keyword 多值字段**而非每租户建库，兼顾隔离与运维成本；Qdrant 多值 keyword 支持"一份数据多方可见"的包含式匹配。
- 服务端**强制叠加**租户过滤且**禁止客户端覆盖**租户键（`enforced_filter`）；`API Key → Principal → tenant_id` 全链路贯穿检索、入库、会话、文档增删。
- **软关闭设计**：未配置租户 Key 时鉴权不启用、行为与加鉴权前完全一致，不打断既有数据与本地调试；预留 JWT/RBAC 升级位（仅需替换 `resolve_principal`）。

### ⚡ 难点三：同步 RAG 引擎桥接异步 API 与 SSE 流式
底层 pipeline（Qdrant / LLM 调用）为**同步阻塞**。通过 `run_in_threadpool` / `iterate_in_threadpool` 将其逐步驱动接入 FastAPI 异步路由，**事件循环不被占住**，实现 `meta → delta → done` 的 SSE 流式；配合 nginx `proxy_buffering off` 保证逐帧实时透传（首帧 <1s）。

### 📄 难点四：结构化文档解析与分块
- **表格行级语义化**：xlsx 大表逐行转语义文本、PDF 表格多级提取，失败时以 **marker 转 Markdown** 兜底并**优雅降级**（未装不阻断）。
- **分类型分块策略**（pdf/docx/xlsx/md/txt）+ 受保护块（表格/代码）**禁止跨类型合并**，避免语义割裂。
- 增量入库：按 `(source, file_hash)` 判重，重灌前**按 source 精确清理旧向量**，杜绝新旧块并存。

### 🧩 工程化与运维
- **全栈容器化**：六服务 Compose 编排，含一次性 `migrate` 服务（`depends_on: service_completed_successfully`）保证建表先于应用启动；**单镜像多角色**（API / worker）。
- **分层 + 依赖注入**：`api → service → repository → model` 清晰边界，仓储只 `flush`、事务由 service 掌控；便于用假依赖做无外部服务的单测。
- **可观测**：Langfuse `trace/span` 软埋点覆盖检索与生成，未配置时零开销。
- **PG 真相 + Redis 热缓存**的会话模型；文档删除以 `ON DELETE SET NULL` 保历史任务、清向量、删副本一体化。
- **统一配置中心**：pydantic-settings 合并全部环境变量，`.env` 与容器环境变量双通道，绝对路径查找回避启动目录差异。

---

## 五、目录结构

```
.
├── backend/                 # FastAPI 后端（应用工厂 + 分层）
│   ├── app/
│   │   ├── api/             # 路由、依赖注入（v1: query/chat/sessions/documents/health）
│   │   ├── core/            # config / security / exceptions / logging
│   │   ├── models/          # SQLAlchemy ORM（document/chat/ingest_task/tenant）
│   │   ├── repositories/    # 数据访问层
│   │   ├── services/        # 业务编排（ingestion / session）
│   │   ├── rag/             # pipeline / retriever / context_builder / prompts / source_formatter
│   │   ├── vector/          # qdrant（混合检索/精排/过滤）· embedding（向量化/重排客户端）
│   │   ├── ingestion/       # loader / chunking（text·table·base）/ indexer
│   │   ├── llm/             # LLM 客户端（OpenAI 兼容）
│   │   ├── worker/          # ARQ settings / pool / tasks（异步入库）
│   │   ├── cache/           # Redis 客户端与会话缓存
│   │   └── observability/   # Langfuse 埋点
│   ├── migrations/          # Alembic
│   ├── eval/                # 离线评测（python -m eval）
│   └── tests/               # pytest 门禁
├── frontend/                # Vue3 + Vite + TS SPA（Pinia / SSE / 上传与知识库管理）
├── docker_setting/          # docker-compose.yml（六服务）+ Qdrant 存储
├── knowledge_base/          # 知识库源文件（入库对象）
└── test_dataset/            # QA 评测集（jsonl）
```

---

## 六、快速开始

### 一键全栈（推荐）
```bash
cd docker_setting
docker compose up -d --build
```
- 前端：http://localhost:8080
- 后端 API / 文档：http://localhost:8000/api/v1 · http://localhost:8000/docs
- Qdrant：http://localhost:6333

> 前端默认在构建期烘入演示租户 Key（`VITE_RAG_API_KEY`，默认 `abc_wzx11`）；也可在页面右上「🔑 租户」随时切换/清除，切换后自动更换会话。

### 本地开发
```bash
# 后端（依赖 uv/venv，需先起 postgres/redis/qdrant）
cd backend && alembic upgrade head
uvicorn app.main:app --reload --port 8000

# 入库 worker
arq app.worker.settings.WorkerSettings

# 离线评测（retrieval-only 基线）
python -m eval --retrieval-only

# 前端
cd frontend && npm install && npm run dev
```

---

## 七、API 一览（`/api/v1`）

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/health` | 健康检查与检索/模型配置回显 |
| `POST` | `/query` | 单轮问答（检索 + 生成，返回答案与溯源） |
| `POST` | `/chat` | 多轮对话（后端会话存储） |
| `POST` | `/chat/stream` | 多轮对话 SSE 流式（`meta → delta* → done`） |
| `POST` | `/sessions` · `DELETE /sessions/{id}` | 新建 / 删除会话 |
| `POST` | `/documents` | 上传文档，异步入库（`202` 返回 `task_id`） |
| `GET` | `/documents/{task_id}/status` | 轮询入库任务状态与进度 |
| `GET` | `/documents` | 列出当前租户已登记文档 |
| `DELETE` | `/documents/{id}` | 删除文档（向量 + 物理副本 + 登记记录一体化） |

**鉴权**：请求头 `X-API-Key: <key>`（或 `Authorization: Bearer <key>`）；`RAG_TENANT_KEYS=tenantA=keyA;tenantB=keyB` 配置租户映射，留空则关闭鉴权。

---

## 八、关键配置（`.env`）

| 分组 | 变量 |
|---|---|
| 模型 | `SILICONFLOW_API_KEY` · `EMBEDDING_MODEL` · `LLM_MODEL` · `RERANK_MODEL` · `VECTOR_DIM` |
| 检索 | `RETRIEVAL_MODE`(hybrid/vector) · `TOP_K` · `PREFETCH_K` · `RERANK_ENABLED` · `RERANK_CANDIDATES` · `HYBRID_TOKENIZE` |
| 低相关过滤 | `SCORE_ABS_MIN` · `SCORE_CONFIDENT` · `SCORE_REL_RATIO` · `SCORE_MIN_KEEP` · `SCORE_DROP_ALL_BELOW` |
| 分块 | `DEFAULT_CHUNK_SIZE` · `DEFAULT_CHUNK_OVERLAP` · `USE_MARKER_FOR_PDF` |
| 多租户 | `RAG_TENANT_KEYS` · `TENANT_FIELD` |
| 存储 | `QDRANT_HOST/PORT/COLLECTION_NAME` · `POSTGRES_*` · `REDIS_URL` · `ARQ_QUEUE_NAME` |
| 会话 | `SESSION_TTL_SECONDS` · `MAX_HISTORY_MESSAGES` |
| 可观测 | `LANGFUSE_PUBLIC_KEY` · `LANGFUSE_SECRET_KEY` · `LANGFUSE_HOST` |

---

## 九、质量保障

- **单元/门禁测试**：`backend/tests`（鉴权、检索、分块、异步入库状态机、会话持久化、API v1、向量过滤）。
- **评测对齐**：`backend/eval` 复刻迁移前检索口径，重构前后逐项一致（Recall@5 ≈ 92%、MRR ≈ 0.90）。
- **端到端冒烟**：经 nginx 验证 health / query 真实生成 / SSE 流式不缓冲 / 上传入库轮询 / 文档删除全链路。
