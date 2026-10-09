# RAG / AI Agent 面试题库（基于本项目，初/中/高各 20 题）

> 用法：每题解答都锚定本项目真实实现（文件、参数、决策），可直接作为面试口述底稿。
> 追问意识：每题末尾如与项目坑/亮点清单（见《项目全景深度解析》）联动，务必带出"亲历证据"。

---

## 初级（概念与项目认知，20 题）

**1. 什么是 RAG？为什么不直接微调大模型？**
RAG = 检索增强生成：先从知识库召回相关片段，拼进 Prompt 让 LLM 基于资料回答。相比微调：知识更新只需重新入库（本项目删除旧向量→重灌，分钟级）而非重训；天然可溯源（本项目 sources 带文件名+页码+预览）；不产生幻觉性"内化"。代价是每次查询多一跳检索延迟，且质量受召回上限约束——所以本项目在检索侧投入最重（混合召回+精排+低分过滤）。

**2. 用一分钟介绍你的项目架构。**
前后端分离全栈：Vue3+Pinia SPA 由 nginx(:8080) 托管并同源反代到 FastAPI(:8000)；存储三件套——PostgreSQL(文档/会话/任务登记，事实来源)、Redis(会话热缓存+ARQ 队列)、Qdrant(向量，单 collection `personal_rag_v2`)；大文件上传走异步：API 秒回 task_id，ARQ worker 后台解析/切分/向量化/索引；模型层走 SiliconFlow（bge-m3 Embedding 1024 维、DeepSeek-V3.2 生成、bge-reranker-v2-m3 精排）；全栈 Docker Compose 六服务一键起，含一次性 alembic migrate。

**3. Embedding 是什么？1024 维意味着什么？**
把文本映射为稠密向量，语义相近则 cosine 距离近。本项目用 BAAI/bge-m3，输出 1024 维（config `vector_dim=1024`，与 Qdrant dense 向量配置一致，改模型维度必须重建 collection）。维度本身决定表达容量与存储/计算成本；`EmbeddingClient.embed` 做了批量与维度校验，防止模型漂移悄悄污染索引。

**4. 分块（chunking）为什么必要？512/50 怎么理解？**
embedding 模型有输入长度上限，且"一段话一个向量"过粗会稀释语义；检索粒度应在"一个可独立回答的语义单元"。本项目 `chunk_size=512` 字符、`overlap=50` 滑窗重叠，防止句子被切断后两侧都检索不到。不是所有内容由同一策略处理：md 按标题层级、docx 段落/表格分路由、xlsx/PDF 表格走行级语义化（见中级题）。

**5. 向量数据库和普通数据库的区别？为什么两个都要？**
关系库精确匹配+事务+多条件查询；向量库做 ANN 近似最近邻（HNSW 索引），按语义相似度检索。本项目分工：Qdrant 存向量+payload（source/页码/tenant_id），负责"找内容"；PostgreSQL 存登记/状态/会话，负责"记账"——比如 `documents.status` 流转、消息历史的事实存储。

**6. top_k 是什么？你们设多少，为什么？**
返回相似度最高的 k 个片段。项目 `TOP_K=5`：评测（107 题）显示 k=5 时 Recall@5≈92%，再大边际收益低还稀释 Prompt、增加 rerank 成本。k 与"候选量"分开：实际先召回 `prefetch_k=20`/`rerank_candidates=20` 精排后再截 5——粗排扩候选、精排定输赢。

**7. 你们的 Prompt 是怎么设计的？**
`rag/prompts.py` 单一 SYSTEM_PROMPT 模板："仅根据提供的参考资料回答，资料不足要明说"，`context_builder` 把召回片段带编号和来源注入 `{context}`。约束式模板+低分过滤+拒答兜底三件套压幻觉（答案无据可查时宁可返回"根据现有资料，未能找到相关信息"）。

**8. 单轮问答和多轮对话后端实现有何不同？**
单轮 `POST /query` 直接用当前问题检索生成。多轮 `POST /chat`：先解析/懒建 session（租户 ACL 校验），从会话存储取最近 20 条历史，检索仍只用当前问题（历史做指代歧义时会参与生成但不做检索改写——明确的取舍点，可作改进项），生成后 user+assistant 两条消息写回。流式版 `POST /chat/stream` 走 SSE。

**9. SSE 和 WebSocket 的区别？为什么选 SSE？**
SSE 是 HTTP 单向服务器推送：实现简单、走普通反代、自动重连、天然穿透 nginx（只需 `proxy_buffering off`）；WebSocket 双向但要多处理升级/心跳/半开连接。LLM 输出是单向流，SSE 够用且运维成本低。本项目事件契约 `meta(来源)→delta(逐字)→done/error`，前端 ReadableStream 手工解帧。

**10. 你们怎么鉴权？为什么不用 JWT？**
`X-API-Key` 每租户一密钥：`RAG_TENANT_KEYS=a=abc_wzx11;b=...` 解析成 {key→tenant_id}，`resolve_principal` 校验后全链路带租户过滤；未配置 Key 时软关闭（匿名放行，行为与加鉴权前一致，本地调试零成本）。当前阶段不选 JWT：无用户体系、租户数小、静态映射足够；`Principal` 结构已预留 roles/scopes 位，升级 JWT/RBAC 只替换一个函数（这是面试里的"演进性"加分点）。

**11. 支持哪些文档格式？各自解析要点？**
PDF/DOCX/XLSX/MD/TXT（`supported_extensions` 白名单）。要点：PDF 有表格三级提取+页码清洗+marker 兜底；DOCX 段落与表格分别抽取保结构；XLSX 多 sheet、行级语义化；MD 按标题层级切并识别内嵌表格；TXT 最简滑窗。统一输出 `{text, metadata}` 文档对象进同一条 chunking→索引管道。

**12. 项目怎么做质量评估？**
`backend/eval`：107 题 QA 集（`test_dataset/qa_test_v2.jsonl`），retrieval-only 模式跳过 LLM（`generate=False`）只测检索，指标通过率/Hit@K/MRR；判分对比期望来源（含目录归属匹配）。基线 Recall@5≈92%、MRR≈0.90，重构全程逐项对齐——"迁移前后指标一致"是重构成败的硬证据。另有 pytest 45 例 hermetic 单测兜行为回归。

**13. 什么是"拒答"？为什么重要？**
召回为空或全部被低分过滤时，直接返回固定拒答文案而不是硬凑上下文生成。这是企业问答的底线设计：错答比"不知道"代价高得多。本项目两层拒答：`/query` 无召回直接拒；多轮无召回注入"无相关参考资料"兜底 context（保持对话语气但不给编造空间）。

**14. 文档状态 pending→ready 是怎么流转的？**
`documents.status`：上传登记即 `pending`（已落盘未索引）；worker 完成向量化并回写 chunk_count 后置 `ready`；处理失败保持 pending 可重试（`upsert` 幂等）。配套的 `ingest_tasks` 另有 `queued→running→done/failed` 状态机+progress 百分比，前端轮询 `/documents/{task_id}/status` 显示进度。两张表分工：文档是"物"，任务是"事"。

**15. Redis 在你项目里承担什么角色？挂了会怎样？**
两件事：①会话热缓存（`session:{id}:history` List，LTRIM 保 20 条+EXPIRE 3600s）；②ARQ 任务队列（`rag_ingest`）。Redis 挂掉：会话读取自动回源 PostgreSQL 并 reheat，数据零丢失（事实来源设计）；入队/消费失败则入库不可用但问答不受影响。缓存丢失只损失性能——这正是"PG 真相 + Redis 加速"分层的目的。

**16. FastAPI 相比 Flask 的优势？项目里怎么体现？**
原生 async + Pydantic 校验 + 自动 OpenAPI。体现：路由协程配合 `run_in_threadpool` 桥接同步 RAG 内核；依赖注入（`get_principal/get_pipeline`）让测试可整体替换依赖；response_model 契约化 API（前后端 types 对齐）；lifespan 集中管理 DB/Redis/ARQ 连接池的启停。

**17. session_id 是怎么生成和传递的？**
后端 `uuid4().hex` 生成 32 位 id，`POST /sessions` 返回后前端存 localStorage(`rag_session_id`)，每次请求带回。服务端以 PG `sessions` 表为权威（含 tenant_id），非本租户的 session 一律视为不存在（自动新建，防串会话）。前端另存一份 Key 的 djb2 指纹(`rag_session_tenant`)，切租户即弃旧会话。

**18. Docker Compose 起哪些服务？migrate 服务是干嘛的？**
qdrant/postgres/redis/migrate/backend/worker/frontend 七个容器（migrate 一次性）。migrate 跑 `alembic upgrade head` 建表后退出，backend/worker 用 `depends_on: {migrate: service_completed_successfully}` 等它退出码 0——保证应用启动时 schema 必然就绪，不用在应用代码里塞建表 hack。健康检查链：pg_isready/PING//api/v1/health。

**19. 前端怎么做到"逐字输出"？遇到中文乱码吗？**
fetch 拿 `ReadableStream`，TextDecoder(`stream:true`) 增量解码，按 `\n\n` 切 SSE 帧、剥 `data:` 前缀再 JSON.parse。乱码坑真实踩过：按字节 chunk 直接 toString 会把多字节汉字劈开，必须用流式解码器；服务端侧 nginx 对该 location 关缓冲，否则逐帧变整块。

**20. 怎么本地把整个项目跑起来？**
`cd docker_setting; docker compose up -d --build`，浏览器开 :8080（镜像构建期已烘演示 Key `abc_wzx11`）。分步验证清单在 README"运行验证清单"一节：compose ps 全 healthy → health 接口 → /query → SSE → 上传轮询 done → 列表/删除 → 切租户看 ACL。注意别 `down -v`（清空卷）；单独重建 backend 后 :8080 502 需 restart frontend。

---

## 中级（实现细节与取舍，20 题）

**21. 详细讲你的混合检索实现。**
Qdrant 单 collection 双 named vectors：`dense`（bge-m3 1024d）+ `bm25` 稀疏向量（自管分词）。查询链路（`vector/qdrant.py search`）：①`tokenize_for_bm25` 对 query 做 jieba 分词+英数串保护（**与入库对称**，否则词表错位）；②prefetch_k=20 双路召回，RRF（reciprocal rank fusion，score=Σ1/(k+rank)）按排名融合——避开两路分数量纲不可比的问题；③融合候选送 bge-reranker-v2-m3 交叉编码精排；④rerank 绝对分做低相关过滤；⑤截 top_k=5 返回，stats 带 dropped 计数。

**22. RRF 为什么用排名而不是分数融合？**
dense 的 cosine∈[0,1] 与稀疏 BM25 分∈[0,+∞) 量纲/分布完全不同，线性加权需要逐 query 校准，很脆。RRF 只用名次：`1/(60+rank)` 求和，天然对分数分布免疫、对离群高分不敏感，一行参数都不用调。代价：丢失分差信息（第1名领先第2名很多和刚好领先同等看待），所以后面还要接 rerank 精排纠偏——这是"两级架构"的逻辑闭环。

**23. 低相关过滤的"量纲安全"具体指什么？**
普遍误区：对融合分/向量分套统一绝对阈值。本项目 `_filter_low_relevance` 按 score_kind 分支：**仅 rerank 绝对相关分**走组合策略——`SCORE_ABS_MIN=0.05` 下限、以最高分为锚的相对断层 `SCORE_REL_RATIO=0.15`、`SCORE_MIN_KEEP=1` 保底（防整页误杀）、`confident=0.5` 强锚点判定；RRF/原始向量分只按名次截断不设绝对限。效果：低质片段被滤且"已隐藏 N 条"可解释（dropped 计数沿 stats→retriever→pipeline.meta→前端徽标全链路透传）。

**24. rerank API 挂了怎么办？你们真踩过吗？**
`_rerank_points` 捕获异常降级为 RRF 原序返回，服务不中断——精排是增强项不是硬依赖。真踩的坑是第二层：**候选扩量到 20 后，降级分支忘了按 top_k 截断**，导致返回 20 条（评测发现"通过但超量"的怪数据）。修复：降级路径统一 `[:top_k]` 并加单测钉死。教训：任何 fallback 分支都要和主路径有相同的出口契约。

**25. 多租户隔离怎么实现的？为什么不用每租户一个 collection？**
单 collection + payload `tenant_id` keyword **多值**字段 + 服务端强制过滤：`enforced_filter` 注入当前租户且剥除客户端传入的租户键（防越权覆盖）；tenant_id 建 payload 索引（`_ensure_tenant_index`）。对比方案：①每租户一 collection——隔离最硬但 collection 数爆炸、HNSW 索引/内存碎片化、跨租户共享文档要复制；②每租户独立 Qdrant——运维不可承受。多值 keyword 还能原生表达"一份文档授权多租户"，未来文档级权限可直接复用该机制。跨租户数据在检索/列表/删除三条路径统一表现为 404/空。

**26. 上传大文件为什么会"秒回"？完整链路讲一遍。**
因为上传只做记账不做重活：`IngestionService.register_document` = 落盘（uuid8 前缀防同名覆盖）+ `Document(pending)` + `IngestTask(queued)` + `arq pool.enqueue_job` → 202+task_id。worker 消费：置 running(10) → `asyncio.to_thread(process_file)`（解析/切分/embed/upsert，同步重活进线程池避免卡事件循环）→ Document 回写(ready,chunk_count) + task done(100)。失败置 failed+error 并 re-raise 触发 ARQ 重试(max_tries=5)。前端轮询状态接口（带租户 ACL）。

**27. 为什么 RAG 内核是同步的？全改 async 不好吗？**
qdrant-client/requests 等同步生态成熟、心智负担低，pipeline 已是"纯函数式"读路径。同步/异步混合的工程答案不是重写而是**桥接三件套**：HTTP 入口 `run_in_threadpool`（query）、流式 `iterate_in_threadpool`（SSE 逐帧透传不阻塞循环）、worker `asyncio.to_thread`（重活）。全 async 化收益主要是极限并发，但要把整条链（含 marker/pdfplumber 等无 async 版的库）包 to_thread，改造收益低回归风险高——面试时这是"克制"的加分叙事。

**28. SSE 的事件契约具体定义？为什么先 meta 后 delta？**
`{type:"meta", sources, retrieved_count, hidden_count}` → `{type:"delta", text}`* → `{type:"done", answer}` / `{type:"error", message}`。meta 先行让用户第一时间看到来源和过滤徽标（感知延迟<1s），done 定稿供落库/复制。前端对契约的鲁棒性：忽略未知事件类型；AbortController 支持中断。评测与 API 结果键（query/answer/sources/retrieved_count/hidden_count）是同一契约的两种载体，迁移时逐项对齐保前端无感。

**29. 表格类文档怎么检索？讲讲行级语义化。**
痛点："型号 X7 的质保几年"这种 query 对着几百行 markdown 表格，embedding 与 BM25 都失灵。方案（`chunking/table.py`）：`table_rows_to_sentences` 把每行改写成自然语言句——`_humanize_column` 美化列名、`_guess_entity_word` 从表头猜实体词（"产品/条款/项目"）、`_find_key_col` 选关键列做主语，产出"X7 路由器 的 质保期 为 3年"式句子块；大表按 `split_markdown_table` 切块且**每块补表头**（`prepend_header`），保住列名语义。xlsx 多 sheet 逐 sheet 走同一路径。

**30. PDF 解析你们做了什么深活？**
`loader.py`(1419 行) 三件事：①表格三级提取链：pdfplumber（线框准）→ PyMuPDF `get_tables` → 自研 `extract_tables_with_dict`+`detect_columns`（无线框/排版怪的最后兜底），提取结果转 markdown 再进表格切块；②噪声清洗：`preprocess_page` 去页眉页脚页码标记（切块后还会 `clean_chunks_page_markers` 二次清）；③极端版面用 marker 模型转 md（`timeout=900s`），**未安装/超时则优雅降级**纯文本继续——解析失败不允许整批入库崩掉。

**31. 小 chunk 合并为什么需要"受保护块"？踩过什么坑？**
`merge_small_chunks` 把 <100 字符碎片并入邻块提升语义密度，但曾把**表格块与周围普通文本粘合**——表格行级语义块被稀释，检索质量回归（评测分数掉下来的实锤）。修复：`_is_protected_chunk` 识别表格/代码块，受保护块**禁止跨类型合并**（同类也不硬并，宁留小体积）。原则：分块策略的每个"优化"都必须过评测回归验证，防止直觉性反作用。

**32. 重灌/删除时向量怎么不残留？ID 幂等怎么回事？**
约定"文件名 = Document.source = Qdrant payload source = 删除键"，重灌前 `delete_by_metadata({"source":...})` 精确清旧点（防新旧两份 chunk 混检）。点 ID = `uuid5(text+metadata)`——同内容同 ID，重复 upsert 覆盖不产生副本。曾踩坑：迁移早期删除键与写入 payload 字段不一致导致向量残留，后来用"单一约定+删除三段验证（vectors_cleared/file_removed/行消失）"钉死，并写进《全景解析》坑表。

**33. "PG 事实来源 + Redis 热缓存"的会话读写路径讲一下。**
读 `get_history`：exists(ACL)→Redis 命中则 touch 续期返回；miss（用 EXISTS 区分"空列表"与"未缓存"）→PG `get_recent_messages` 回源 + `reheat`（pipeline 覆盖重建 LTRIM+EXPIRE）。写 `append`：**先 PG（seq=next_seq、CASCADE 归属、touch 活跃时间）后 Redis**，顺序不可反——先写缓存会出现"缓存有库里无"的幻象数据。租户不符静默跳过（对齐原内存实现语义）。效果：重启/清缓存会话不丢，热路径又免去每次全表读。

**34. ingest_tasks 的 document_id 为什么用 ON DELETE SET NULL？**
删除文档时业务上要"文档没了但历史任务记录保留"（审计/排障）。两种坏选择：CASCADE 会把任务证据一起删；RESTRICT 会报错阻断删除。SET NULL 让任务行留存且 document_id 置空。同理 messages 用 CASCADE——会话删除时消息没有独立留存价值。每个外键动作都是一次数据生命周期决策，面试值得展开。

**35. 删除文档为什么"先清向量、最后删 DB 行"？**
一致性次序设计（`delete_document`）：外部资源清理（Qdrant→磁盘文件）先做且**任何一步失败就中止保留登记行**——行在，用户可重试，重试幂等（向量已清则 delete_by_metadata 返回 0 不报错）；若反过来先删行，半途失败就产生"向量/文件孤儿且无账可查"。跨租户在入口 `get_by_id(id, tenant)` 统一 404。两个私有函数 `_clear_source_vectors/_remove_physical_file` 就是为单测 monkeypatch 设计的——编排逻辑纯本地可测。

**36. Alembic 在项目里怎么用的？为什么 compose 里做一次性 migrate 服务？**
schema 版本控制：`migrations/versions/35fd0e1e9233` 初始建 5 表+索引+外键；改 models 后 `alembic revision --autogenerate`。迁移用同步 psycopg URL（`sync_postgres_url`），运行时用 asyncpg——一次性脚本任务不需要异步。compose 把 `alembic upgrade head` 做成一次性 migrate 容器，backend/worker `service_completed_successfully` 等待：应用代码永远假设表存在，避免"启动时建表"的竞态与权限混乱。另有个环境坑：alembic.ini 在 Windows 按 GBK 读，中文注释直接崩，ini 注释保持 ASCII。

**37. ARQ 为什么不用 Celery？worker 为什么单并发？**
选型：任务形态是"IO+CPU 混合型单步任务"，ARQ 原生 asyncio、只依赖 Redis（本来就有）、配置即 WorkerSettings 类，比 Celery（要 broker+routing+result backend 全家桶）轻一个量级；P0 决策记在项目里。坑：`RedisSettings.from_dsn`（不是 from_url）。`max_jobs=1` 是刻意的：保证同一文件不会并行重灌（delete+upsert 竞态），job_timeout=600 给大 PDF 留量。吞吐不足时升级路径是"按文件粒度分队列+多副本+幂等已具备"，不是盲目调大并发。

**38. 前端"切租户不串会话"是怎么做的？**
会话绑定 Key 指纹：`createNewSession` 成功时除存 `rag_session_id` 再存 `rag_session_tenant`（API Key 的 djb2 hex 指纹）；`ensureSession` 只在指纹与当前 Key 一致时复用，否则清掉两个键重建；新建失败（401）同样清残留。为什么存指纹不存 Key 本身：不落敏感原文、比对 O(1)。配合后端"非本租户 session 视为不存在自动新建"的 ACL，双端闭环。Key 本身 localStorage 优先于构建期 VITE 变量，随时可切。

**39. Langfuse 埋点怎么做到"不配置就零开销"？**
`observability/langfuse.py` 封装 `trace()/span()` 上下文管理器：`settings.langfuse_enabled`（public+secret 都非空才真）为假时返回 no-op 上下文，pipeline 代码里埋点语句照常写但不产生任何 I/O。真开启时记录 rag_query/rag_chat/rag_chat_stream trace、retrieve 子 span（output 带 retrieved/hidden 计数）、LLM generation 用量。埋点策略是"选择性打点"：只打编排节点，不在 chunk 循环里打，防 trace 噪音与费用。

**40. hidden_count（已隐藏 N 条）这个数据怎么从引擎走到 UI 徽标？**
完整透传链路五环：Qdrant `search` 内 `_filter_low_relevance` 返回 dropped → stats 字典 → `Retriever.search` 解包成 `(chunks, dropped)` → pipeline 三种出口（query 结果键 / stream meta 事件 / chat response）→ 前端 stores 存 currentMeta → SourcesList 组件渲染"已隐藏 N 条"徽标。设计意图：低分过滤是把双刃剑（可能用户觉得漏答），把"被藏了多少"暴露出来让行为可解释、可反馈。这条链曾断过——迁移时 pipeline 返回键少了 dropped，UI 永远显示 0，靠端到端冒烟抓回。

---

## 高级（架构决策、规模化与演进，20 题）

**41. 多租户方案的完整权衡：什么时候你会换掉单 collection 字段隔离？**
三层方案谱系：字段 ACL（当前）→ collection 分区 → 独立实例。换 collection 的触发条件：①租户数使单 collection payload 过滤选择率恶化（大量租户×稀疏数据）；②租户间数据量悬殊引发 HNSW 局部退化与大租户抢占；③合规要求物理隔离（数据驻留/加密边界）。迁移成本低的原因：tenant_id 已贯穿 API/入库/删除全链，抽象点只在 `enforced_filter` 和 collection 名解析两处。面试答法：先讲当前方案为何占优（共享文档多值授权、运维面小），再讲边界与可退路径——体现"决策有条件"而非"选型有信仰"。

**42. 从你的系统角度，幻觉抑制的完整链路怎么设计？**
四层：①检索层——混合召回保"有据可查"，低分过滤保"无据不发"（宁缺毋滥）；②Prompt 层——系统模板约束"仅根据资料回答"，context 带来源编号供引用；③兜底层——无召回固定拒答/多轮"无相关参考资料"降级，`done` 事件保持契约一致；④验证层——sources 全量透传到 UI（文件名+页码+预览），人肉核对答案-依据对应关系，评测侧 expected-source 命中判分。再往企业级走：答案-引用一致性检查（每句话是否挂得上 source，LLM-as-judge 自动核）、事实冲突时的版本优先级策略。

**43. 检索指标不错但答案质量差，你怎么定位和改进？**
先归因分层：检索层与生成层解耦——本项目 retrieval-only 评测（`generate=False`）就是为此：MRR 正常则问题不在召回，转向生成侧。生成侧检查项：context 组装是否把最相关块放对位置（中间迷失→按 rerank 分排序注入）、Prompt 是否诱发外推（约束语+few-shot 拒答样例）、模型温度与 max_tokens 截断、历史消息污染（多轮时旧答案被当依据）。改进验证全部走同一评测集回归。更系统化的下一步：LLM-as-judge 三维度打分（faithfulness/relevance/refusal correctness），已列入改进路线第三梯队。

**44. 要支撑 1000 租户，现在架构哪些点会先崩？怎么改？**
按爆炸半径排序：①Qdrant 单 collection 过滤选择率与内存（1000 租户稀疏数据）——租户分片 collection 或 Qdrant 集群分片+tenant hash；②`RAG_TENANT_KEYS` 静态配置与重启轮换不可行——Key 入 PG 哈希存储+管理面 API；③worker max_jobs=1 吞吐——队列分级（小文件快车道）+多副本，幂等 ID 已保证安全；④rerank 每 query 外呼——结果缓存+批量归并，否则 P95 与费用双爆；⑤PG 连接池 20+10 单实例——读写分离/pgbouncer；⑥评测集按租户维度扩容否则质量不可知。答法要点：给出"先崩顺序"比罗列方案更能体现系统直觉。

**45. 异步入库的可靠性设计：重试、幂等、死信、对账你们各做到什么程度？**
已有：失败 re-raise → ARQ 按 max_tries=5 自动重试；幂等双保险（uuid5 点 ID 覆盖式 upsert + delete_by_source 先清后灌）；状态机+error 字段可观测。缺口与改进（诚实答，面试官追问必考）：①无死信队列——5 次失败的 task 停在 failed，需 DLQ+告警看板；②无对账 reconcile——删除三段清理（向量→文件→DB 行）非事务，中途失败靠"保留登记行可重试"兜底，但没有定时任务扫描 PG documents vs Qdrant payload 的漂移；③无任务优先级。设计原则可总结为："任何非事务的多步操作，要么可重试幂等，要么有对账"，我们做了前者、后者在路上。

**46. Agent 方向：你的系统是 pipeline 不是 Agent，如果升级为 Agentic RAG 怎么做？**
现状：固定编排（检索→生成），无分支决策。升级路径分三级：①轻——查询理解 Agent：改写/拆解（多跳问题拆子查询、指代消解补全），带 fallback 不阻塞主链；②中——ReAct 循环：工具集 = {retrieve, list_documents, rerank_query, clarify_ask_user}，LLM 决定"再检索/追问/作答"，本项目早期做过 ReAct 原型验证（test_react 实验），深知主要风险是轨迹发散与费用——需 max_steps+预算护栏+轨迹 trace（Langfuse span 树现成）；③重——多 Agent（检索员/审核员/汇总员）。面试杀手锏：说清"为什么当前没做"——单跳 QA 场景固定管线 Recall@5 92%，Agent 化的边际收益要等"多跳/跨文档对比"类失败案例占比起来再兑现，是评测驱动的演进不是技术时髦。

**47. 成本工程：LLM 系统的钱都花在哪，你怎么控？**
大头排序：①生成 token——max_tokens=4096 封顶、temperature 0.3 防啰嗦、多轮历史 LTRIM 20 条封顶、Langfuse usage 字段已按 trace 记账（改进：按租户配额）；②rerank 外呼——每 query 20 候选×交叉编码，缓存与批量归并在路线图上；③embedding——入库一次性成本，ProcessedCache hash 判重让重复灌入零成本；④被低估的隐性成本——低分过滤省掉"无关块进 context"的生成 token。企业控费三件套：计量（租户维度 usage）、预算（超限降级为检索-only）、透明（sources 面板让用户自己判断是否值得追问）。

**48. 灰度发布与回滚：schema、索引、模型三个层面各怎么做？**
schema：expand-contract 迁移（先加列/新表→双写过渡→切读→后删旧），alembic down 脚本必须实测；索引：collection 版本化——项目真实案例 `personal_rag_v2` 就是换维度/字段时的新 collection 并行重灌+别名切换（旧 collection 保留随时回切），配置 `QDRANT_COLLECTION_NAME` 一行完成切换；模型：embedding 模型与维度绑定（1024 写进 collection 配置），换模型=新版本 collection，不存在"热换"——这是 RAG 特有的发布约束，面试讲出来很识别内行。服务层：compose 镜像 tag+`up -d` 滚动，回滚即换 tag，healthcheck 兜底自动摘流。

**49. "忠实迁移"重构策略你是怎么执行的？为什么不全重写？**
背景：单体 src/ → 分层 backend/ 是行为保持重构，不是重写。方法：①每次迁移一个域（vector→ingestion→rag→持久化→API→前端），改完立即用同一评测集跑基线对齐（Recall@5/MRR 逐项相等才算过）；②行为契约写进代码注释（pipeline 返回键/事件类型"与 src 完全一致，确保前端与评测无感"）；③45 例 hermetic 单测锁边界行为（量纲过滤策略、降级截断这类曾经回归过的点全部钉死）；④旧代码存活到最后一域迁移完才退役删除。为什么不重写：重写丢失的是"评测对齐"这条生命线——没有差分基准的重写，质量回归无法归因。这是能讲 10 分钟的工程叙事，准备好"迁移中发现 hidden_count 断链、靠端到端冒烟抓回"这种具体案例。

**50. 你的技术债清单排过优先级吗？第一个动的是哪个，为什么？**
按"爆炸半径×修复成本"排：先动安全与成本的三件——①Key 体系（静态明文→入库哈希+轮换+吊销）：它是所有企业客户接入的硬门槛，其他一切改进都会被它挡在门外；②限流/配额：LLM API 直通意味着一个脚本小子的成本事故，止损面最大；③rerank 缓存：P95 延迟与费用的单点，收益天天可见。而后端吞吐类（worker 扩容）反而靠后——当前单并发+重试已满足上传规模，"没有疼的债不急着还"。明确不做的：全链路 async 化（收益/风险比不划算，见 27 题）。这题答好的关键：让面试官看到你区分"技术优雅"与"业务疼点"的判断力。

**51. 文档级权限（同租户内不同人可见范围不同）怎么在现有架构落地？**
现有 tenant_id 多值 keyword ACL 直接复用：payload 加第二个多值字段（如 `dept_ids`/`acl_tags`），`enforced_filter` 从 Principal 扩展读取用户标签集合，Qdrant 对多值 keyword 的 match_any 天然表达交集授权；写入侧入库接口增加标签参数（当前 `_normalize_tenants` 已是同类处理）。UI/登记侧：Document.meta_json 存标签、列表接口按标签过滤。为什么不建议换专用权限系统：当前 ACL 是"检索前置过滤"（在向量层就挡住，零泄漏面），换成应用层后置过滤会出现"召回了但不能看"的计数与泄露歧义。关键细节：权限变更后需重打 payload（`delete_by_metadata+upsert` 或直接 set_payload），这点做成管理接口。

**52. 系统怎么可观测：metrics/logs/traces 三条腿现状与目标？**
现状诚实版：traces 有（Langfuse 选择性埋点：trace/span/generation usage）；logs 有统一格式（`时间|级别|模块`）但无结构化 trace_id 贯穿；metrics 缺（无 QPS/P95/错误率聚合，无告警）。目标架构：①Prometheus 指标层——FastAPI instrumentator + Qdrant/rerank 外呼时延直方图 + worker 队列深度；②trace_id 从 HTTP 中间件注入贯穿日志与 ARQ job（ctx 传递，链路才完整——当前 API→enqueue→worker 这段是断的）；③业务指标看板：hidden_count 分布（低分过滤是否在误杀的量感）、每租户检索零召回率（知识库缺口雷达）。面试加分句：可观测性的优先级是"先回答业务问题（答不好是检索差还是生成差），再回答性能问题"。

**53. nginx/前端这一层有什么值得讲的工程决策？**
三个点：①同源反代替代 CORS：SPA 与 API 同域名(:8080→/api/v1)，CORS 白名单仅留给本地开发（5173），生产凭证不进浏览器跨域脚本面；②SSE 专用 location `proxy_buffering off`——不开缓冲则逐帧变整块，流式白做，这是"前端体验由最窄管道决定"的典型；③前端构建期烘 Key 的边界意识：`ARG VITE_RAG_API_KEY` 让演示零配置，但明确它是演示妥协（产物明文），登录换发短时 token 才是终态，Key 优先级设计（localStorage>VITE 变量）已为切换留好缝。另备一个真实 UI 坑：backdrop-filter 创建层叠上下文把下拉裁掉，Teleport 到 body 根治——证明前端深度。

**54. 评测体系怎么从"能跑"升级到"可信的门禁"？**
现状：107 题检索指标+人工冒烟，已是"迁移基准"。升级到门禁四步：①集子扩容分层：按文档类型/难度（直接检索/跨块聚合/表格行级/拒答负样本）分桶统计，总分掩盖结构缺陷——项目已验证过分桶的价值（表格类失败单独归因出行级语义化改造）；②答案质量 LLM-as-judge（faithfulness/relevance/refusal 三维，双模型交叉防自评偏置）；③CI 接门禁：检索指标低于基线-2pp 阻断合并，judge 分数周级看板不阻断（防噪声绑架）；④线上回流：用户点踩+hidden_count=全部的 query 自动进候选评测集，形成数据飞轮。这套顺序强调"先自动化回归网，再谈覆盖率"。

**55. 如果重做一次这个项目，你会改哪三个决策？**
准备这题是资深面试的信号题——考反思而非悔棋。候选：①API 契约先行：前端是后完成后接的，字段靠注释约定，中间吃过"结果键漂移"（hidden_count 断链）的亏——应一开始 OpenAPI schema + 前端 codegen（types.ts 自动生成）；②payload schema 与删除键约定应更早写成 ADR：`source=文件名=删除键` 这个隐性契约散落三个模块，差点因不一致造成向量残留；③测试基建立得更早：hermetic 单测是在 P4 worker 阶段才补的，如果 P1（vector 迁移）就建好，量纲过滤这类坑不会等到评测阶段才暴露。每一条都带"付出了什么代价"的具体事实，比空谈"会更模块化"有力得多。

**56. 知识图谱/GraphRAG 要不要上？你的判断框架是什么？**
判断框架：失败案例归因决定引入成本。当前评测失败集中在"表格行级/跨块聚合"类——GraphRAG 对"实体-关系多跳"类问题（A 公司的 B 项目的负责人参与过哪些合同）才有显著收益，对表格逐行事实无优势。引入成本：抽取质量（LLM 批量抽取实体的噪声）、增量更新（文档删除时图谱一致性比向量删除难得多）、运维面。结论：不上，但把"多跳类失败占比"设进评测监控，超阈值再启动——届时方案是混合路由（检索型走现有管线，关系型 query 路由到图）。核心答法：新技术的引入由评测分布驱动，不由 demo 驱动。

**57. 讲讲你在这个项目里做过的最有技术含量的一次 debug。**
推荐讲 uv 互搏事件（真实、完整、跨工具链）：现象——PyCharm 每次启动 backend 卸 11 装 11 还重新下载；假设链——先怀疑"关程序释放"（否，venv 是磁盘目录）→ `uv run` 隐式 `sync --exact`（方向对）→ 真凶定位用探针：跑两次 `uv sync`，第一次卸了 6 个 pytest 系包、第二次零动作——证明"环境里存在 lock 外的包被 uv 清除，而 PyCharm 测试集成又会装回"的循环；根因是声明位置错误：pytest 在 `[project.optional-dependencies]`（extras，uv 默认不同步）而 PyCharm 需要它。修复：迁移到 PEP 735 `[dependency-groups].dev`，lock+sync 二连静默，45 测试在新 venv 全绿。展示点：可复现探针设计（"pass2 必须零动作"作为判据）、对工具语义的精确理解（extras vs groups）、修复不伤 Docker 构建（pip 忽略 dependency-groups）。

**58. 这个系统离"生产可用"还差什么？给一个上线 checklist。**
按门禁项列：安全（JWT+Key 轮换吊销、限流配额、HTTPS、CORS 收紧、前端去烘 Key）、数据（PG/Qdrant 备份与恢复演练、死信队列、对账任务）、观测（指标聚合+告警、trace 贯穿 worker）、容量（rerank 缓存、worker 多副本压测、连接池评估）、流程（CI/CD+评测门禁、灰度回滚 runbook、on-call 手册）、合规（数据留存策略：messages 表 TTL/删除请求响应——session 删除已支持，文档级"被遗忘"流程需补）。一句话收束："架构与功能面已达 MVP，差的是运维皮肤与安全硬化层，全部在既定路线图上而非未知领域"——展示对"能演示"和"能托付生产"的区别有清醒认知。

**59. 如何向非技术决策者介绍这个项目的价值与投入？**
练的是表达降维能力。价值三句话：让企业文档"开口回答问题"，每个答案标注出处可核查（信任基础）；新资料放进知识库分钟级生效，无需重训模型（运营成本）；租户间数据严格隔离，可满足部门级权限要求（合规起点）。投入叙事：已交付全栈+一键部署+质量基线（92% 检索命中），下一步投入集中在安全加固与吞吐（约一个迭代的量）。风险坦白：答案质量上限受文档质量约束，需要业务侧配合治理知识库——把"这不是魔法"讲清楚反而赢得信任。面试用得到：考察能否跳出技术自嗨，以及向 PM/老板争取资源的能力。

**60. 项目里你最满意的一个设计决策？为什么？**
开放收尾题，推荐答"量纲安全的低分过滤"：①问题识别力——它不是教科书问题，是"把 RRF 分当相关性分套阈值"这一流行错误的自发纠偏（能讲清为什么多数人会做错）；②方案分寸——没有引入校准模型等重武器，而是"仅对 rerank 绝对分建阈值体系+相对断层+保底 N 条"的组合拳，简单但边界严密；③工程闭环——把"被过滤多少"变成产品可见性（前端徽标透传链路），可解释性做进 UI；④防回归——策略被单测钉死、评测验证净收益。一道小题同时体现"洞察、克制、闭环"三种素质，这正是要传递给面试官的信号。

---

## 附：答题节奏建议
- 初级题：概念一句话 + 项目实例一句 + 一个参数/文件位置，证明"亲手做过"。
- 中级题：先讲机制再讲"为什么这么选"，主动带一个相关踩坑（坑从《全景解析》18 条里取）。
- 高级题：结构 = 当前决策与理由 → 边界/失效条件 → 演进路径 → 成本意识。宁可承认缺口（死信/对账/metrics 缺失）也别装——资深面试官对"知道没做什么"的敏感度远高于"做了什么"。
