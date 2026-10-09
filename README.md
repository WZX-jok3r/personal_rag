# 内部知识库 Agent · RAG + Text2SQL 双引擎

> 前后端分离的多租户知识库 Agent：**非结构化问题走混合检索 RAG，统计类问题走 Text2SQL 精确计算**，
> 支持跨源联合推理、流式执行轨迹、四层 SQL 安全网关与 RBAC 敏感数据脱敏。
>
> 技术栈：FastAPI（异步）· Vue 3 + TypeScript · PostgreSQL · Qdrant · Redis · ARQ · Docker Compose · DeepSeek-V3.2

---

## 一、这个项目解决什么问题

### 起点：一个做得不错、但撞到天花板的 RAG 系统

最初的版本是一个标准的企业级 RAG：混合检索（dense + BM25 + RRF 融合）、交叉编码器精排、
量纲安全的低相关过滤、多租户隔离、异步大文件入库。检索指标做到 **Recall@5 99.01% / MRR 0.9703**（107 题评测集）。

### 转折点：一个"静默错误 31.8 倍"的真实案例

用真实系统问一个问题：

> **「员工表中 Sales 部门和 Engineering 部门各有多少人？两者相差多少人？」**

| | RAG 的回答 | 数据库真值 |
|---|---|---|
| Sales 部门人数 | 1042 | 1042 |
| Engineering 部门人数 | **25** | **1010** |
| 两者差值 | **1017** | **32** ← 误差 **31.8 倍** |

**最危险的不是它答错了，而是它错得毫无信号**：

- 5 条检索结果的 rerank 分数 **0.70 / 0.66 / 0.58 / 0.51** —— 全部正常偏高，低相关过滤一条都没拦；
- 系统还主动写了一句 **"两个数据来源不同，但逻辑上可调和"** 来自我辩护；
- 用户**没有任何信号**可以察觉这是错的。

**根因**：检索的**相关性度量不度量完备性**。万行表被切成 242 个块，
任何单块都不含全表聚合事实。曾经尝试的补丁（为大表预生成聚合摘要块）
只能覆盖"分组计数最多/最少 + 数值列 Top3/Bottom3"，
而"任意两部门对比"的组合空间是 O(n²)——**穷举预计算不可行**。

### 结论与方案

> **凡是需要跨全量数据精确计算的问题，必须走 SQL。**
> 于是把它升级为双引擎 Agent：需要计算的走 Text2SQL，需要依据的走 RAG，
> 两者都需要的做**跨源联合推理**。

---

## 二、架构

```
                         ┌──────────────────────────────────────────────┐
  Browser ──SSE─────────►│  nginx :8080 （SPA + 同源反代 + SSE 关缓冲）  │
                         └──────────────────┬───────────────────────────┘
                                            ▼
                         ┌──────────────────────────────────────────────┐
                         │  FastAPI :8000                               │
                         │  /query /chat /documents  （原有 RAG 链路）  │
                         │  /agent/stream /agent/route （Agent 链路）   │
                         │  /usage /usage/sql         （成本看板）      │
                         └───┬────────────┬─────────────┬───────────────┘
                             │            │             │
              ┌──────────────▼──┐  ┌──────▼──────┐  ┌───▼─────────────┐
              │ 关卡一 规则路由 │  │   Agent     │  │  ARQ worker     │
              │ （纯函数,0成本）│  │  有界状态机  │  │  异步入库        │
              └──────────────┬──┘  └──────┬──────┘  └───┬─────────────┘
                             │            │             │
                    ┌────────▼───────┐  ┌─▼──────────┐  │
                    │ 工具 kb_search │  │ 工具        │  │
                    │  （复用原有    │  │ sql_query   │  │
                    │   混合检索）   │  │             │  │
                    └────────┬───────┘  └─┬───────────┘  │
                             │            │              │
                    ┌────────▼─────┐  ┌───▼──────────────────────────┐
                    │ Qdrant       │  │ kb_analytics（独立只读库）    │
                    │ 1402 向量点  │  │ 四层防御 + RLS + RBAC 脱敏    │
                    └──────────────┘  └──────────────────────────────┘
```

### 两条链路的关键区别

| | RAG 链路 | Text2SQL 链路 |
|---|---|---|
| 回答什么 | 政策、条款、规格、手册等**非结构化**内容 | 计数、求和、平均、排名、对比等**精确计算** |
| 数据来源 | Qdrant 向量检索（dense + BM25 + RRF + 精排） | PostgreSQL 只读 SQL |
| 正确性来源 | 检索到的片段 | **数据库计算** |
| 失败模式 | 可能"局部相关"而给出错误答案 | 生成错 SQL 会被 guard 拦或执行报错 |

---

## 三、核心工程亮点

### 1. 四层纵深防御的只读 SQL 网关

LLM 生成的 SQL 必须无法伤害数据库。**任何一层单独都不够**：

| 层 | 实现 | 挡住什么 | 为什么单独不够 |
|---|---|---|---|
| ① AST 白名单 | `sqlglot` 解析后遍历语法树 | 非 SELECT、多语句、系统表、`pg_read_file`、`COPY TO PROGRAM`、`pg_sleep` | 📄 sqlglot 官方 FAQ 自称 *"a transpiler, not a validator"* |
| ② 只读事务 | `set_config('transaction_read_only','on')` + `statement_timeout` | 漏网写操作、长查询 DoS | 📄 PG 官方承认只读是 *"high-level notion…does not prevent all writes to disk"* |
| ③ 角色权限 | 独立库 + `kb_ro` 只授 `SELECT` | 任何写企图 | 挡不住"用合法 SELECT 读敏感数据" |
| ④ 数据隔离 | RLS（`app.tenant_id`）+ RBAC 列脱敏 | 跨租户越权、越权看敏感列 | 需应用正确设置会话变量 |

**44 条攻击载荷全部拦截**（含 `DROP`、多语句注入、`pg_shadow`、文件读写、命令执行、
注释混淆、大小写/空白变体），22 条合法查询全部放行。

LIMIT 注入用**外层包裹**而非尾部追加：`SELECT * FROM (<orig>) AS _q LIMIT n`——
这样即使模型自己写了 `LIMIT 999999` 也被压住。

### 2. 不做"看起来对但口径错"的回答

- **口径歧义澄清**：问「平均客单价是多少？」，系统**反问**并给出候选口径，而不是猜一个。
- **截断如实标注**：结果被行数上限截断时，`truncated` 标志**同时**推给前端（用户可见）
  和工具观察（LLM 可见），并明确要求"不要声称这是完整结果"。
- **大结果集保护**：>50 行只给"列名 + 行数 + 前 5 行样例"，
  因为 LLM 无法预知结果集大小，上千行明细会炸掉上下文窗口。

### 3. 有界状态机 Agent（手写，不引入框架）

评估过 8 个框架（LangGraph / Pydantic AI / LlamaIndex / AutoGen / CrewAI / OpenAI Agents SDK /
Google ADK / DB-GPT）后决定手写约 300 行有界状态机。理由：只有 3 个工具、
循环上界明确、必须复用既有 SSE 契约与 hermetic 测试体系。

**五重防失控**（框架默认往往只有前两条）：

| # | 机制 | 作用 |
|---|---|---|
| 1 | `MAX_STEPS = 5` | 步数硬上界（状态机而非 `while True`） |
| 2 | `WALL_CLOCK = 60s` | 墙钟超时 |
| 3 | `MAX_LLM_CALLS = 8` | **成本上界** |
| 4 | 相同工具+参数去重 | 检测重复调用即跳出 |
| 5 | 失败降级纯 RAG | 保证可用性不倒退 |

### 4. 三层混合路由：让"意图识别"可测试

```
关卡一  规则路由（纯函数，0 成本）
         必须**同时**命中「聚合词」与「已注册列名词」才强路由 SQL
关卡二  LLM function calling 自主选工具（可多选做跨源推理）
关卡三  异常/超步数/超时 → 强制退回纯 RAG
```

**为什么必须两道关卡**：全交 LLM 则每轮多 1~2s 且可能误选；
纯规则则处理不了"研发薪资 vs 文档带宽"这类跨源问题。
关键是**关卡一是纯函数，107 题评测集就是它的表驱动测试用例**——
意图识别在本项目里是可测试的，不是靠 prompt 祈祷。

> 反例（评测集里真实存在）：「布洛芬最多多久吃一次？」命中聚合词"最多"，
> 但本质是文档问题。**这就是规则层必须可单测的原因。**

### 5. 向后兼容的事件契约（扩帧不改帧）

前端按事件 `type` 分支渲染，其中 `meta` 承担来源列表与「已隐藏 N 条」徽标。
改造策略：**原有 4 类事件字段一个不改**，新增 6 类轨迹事件
（`route` / `tool_call` / `tool_result` / `sql` / `clarify` / `degraded`），
前端对未知 type 走 `default` 忽略分支 ⇒ **旧前端 + 新后端不会崩**。

⚠️ 一个容易漏的点：**降级路径必须补发标准 `meta` 帧**，
否则 Agent 降级到 RAG 时前端拿不到它，来源列表与徽标会永久空着。

---

## 四、量化结果（全部可复现）

| 指标 | 数值 | 复现方式 |
|---|---|---|
| **Text2SQL Execution Accuracy** | **100%**（42/42） | `python -m eval.text2sql_runner` |
| **Valid SQL Rate** | 100%（42/42） | 同上 |
| **平均生成次数** | 1.00（一次生成即正确） | 同上 |
| **检索 Recall@5 / MRR** | 99.01% / 0.9703（107 题） | `python -m eval --retrieval-only` |
| **单测** | **541 passed** | `pytest -q` |
| **SQL 攻击载荷拦截** | 44/44 | `pytest tests/test_sql_guard.py` |
| **端到端延迟** | SQL 查询 2~5ms；检索路径首帧 <1s | `scripts/check.ps1 -Full` |

### 关键回归验证

改造前 RAG 对「Sales 与 Engineering 各多少人、差多少」答 **1017**；
改造后 Agent 答 **32**，并展示执行的 SQL：

```
[route]       判定为统计问题 → 走数据查询（同时命中聚合词与列名词）
[tool_call]   读取数据表清单
[tool_result] 共 4 张可查数据表：cost_data、employees、expenses、sales
[tool_call]   查询数据表
[sql]         SELECT * FROM (SELECT department, COUNT(*) AS 人数 FROM employees
              WHERE department IN ('Sales','Engineering') GROUP BY department) AS _q
              → 2 行 / 3ms
[done]        Sales 部门有 1042 人，Engineering 部门有 1010 人，两者相差 32 人。
```

> **诚实说明**：100% 不等于"比公开榜单强"。本项目只有 4 张表、列有中文注释、
> 枚举值已内联，难度远低于 BIRD（95 库 / 33.4GB / 37 领域，**人类专家 92.96%**、
> 榜首方案约 83%）。这个数字证明的是**"schema 语义层做对了，模型就能稳定生成正确 SQL"**。

---

## 五、RBAC 与合规

接入 SQL 之后，"能查到什么"从"文档里写了什么"变成了**可枚举的数据权限问题**
（例如"全公司薪资最高的员工是谁"）。这把租户隔离升级成了**合规问题**。

| 角色 | 同一查询的结果 |
|---|---|
| `analyst`（默认） | `Derek Cummings，薪资 179,997` —— 全部可见 |
| `employee` | `Derek Cummings` + "具体薪资因数据脱敏未显示" |

- **脱敏在结果集上按列名做**，而不是改写 SQL —— 因为 LLM 常写 `SELECT *`，
  按列名匹配**不依赖模型生成什么**，是"默认拒绝"式的兜底。
- **SQL 审计**：每次执行落库（谁/何时/查了什么/是否脱敏/耗时），
  但**只记 SQL 与元数据，不记结果集**（结果集会含敏感数据，存下来等于脱敏白做）。
- **向后兼容**：不配置 `RAG_TENANT_ROLES` 时所有人走 `analyst`，
  脱敏只对非特权角色生效 ⇒ 不配置就等于没有这个功能。

---

## 六、快速开始

```bash
# 1) 配置（唯一必填项是模型 API Key）
cp .env.example .env      # 填 SILICONFLOW_API_KEY

# 2) 起全栈（六服务 + 一次性 migrate）
cd docker_setting && docker compose up -d --build

# 3) 建 Analytics 库与只读角色，并装载业务表
cd ../backend
python -m app.analytics.setup_db          # 建库 + kb_ro 只读角色 + 权限自检
python -m app.analytics.seed --all --verify   # ETL 装载 4 张表并跑真值断言
```

访问：

| 地址 | 说明 |
|---|---|
| http://localhost:8080 | 前端（默认 Agent 模式，可切纯 RAG 模式） |
| http://localhost:8000/docs | OpenAPI 文档 |
| http://localhost:6333/dashboard | Qdrant 控制台 |

### 验证改动没破坏东西（三级闸口）

```bash
# L1 语法 + L2 全量单测（秒级）
.\scripts\check.ps1

# 再加 L3：检索评测 + 与冻结基线**逐题**比对（分钟级）
.\scripts\check.ps1 -Full
```

> 为什么必须逐题比对：聚合指标会掩盖"等量置换"——
> A 题修好、B 题坏了，召回率仍是 99.01%，但行为已经变了。

---

## 七、目录结构

```
backend/
├── app/
│   ├── agent/            # Agent 编排（P4/P5）
│   │   ├── router.py     #   三层混合路由第一层（纯函数，可穷举单测）
│   │   ├── tools.py      #   3 个工具 + 异常链解包
│   │   ├── loop.py       #   有界状态机（五重防失控）
│   │   └── events.py     #   SSE 事件契约（扩帧不改帧）
│   ├── analytics/        # Text2SQL（P1/P2/P3）
│   │   ├── guard.py      #   四层防御①：AST 白名单 + LIMIT 注入
│   │   ├── executor.py   #   四层防御②：只读事务 + 超时 + 截断标注
│   │   ├── redact.py     #   RBAC 敏感列脱敏
│   │   ├── text2sql.py   #   NL→SQL 全链路
│   │   ├── schema_infer.py / ddl.py / seed.py   # ETL
│   │   ├── schema.py / fewshot.py               # Schema Linking + 示例检索
│   │   ├── audit.py / usage.py                  # 审计 + 成本记账
│   │   └── sql/001_init.sql                     # 只读角色与最小权限
│   ├── rag/ vector/ ingestion/ llm/ worker/     # 原有 RAG 链路（基本未改）
│   └── core/             # config / security(RBAC) / retry / exceptions
├── eval/                 # 两个评测 runner + 冻结基线
├── tests/                # 541 项测试
└── migrations/           # Alembic（元数据表 / 审计 / 用量）
frontend/src/             # Vue3 SPA（AgentTrace / MessageBubble / ChatView）
scripts/check.ps1         # 三级验证闸口
docs/                     # 改造方案 + 数据层设计 + 经验教训（19 条）
```

---

## 八、API 一览（`/api/v1`）

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET` | `/health` | 健康检查与配置回显 |
| `POST` | `/query` · `/chat` · `/chat/stream` | **原有 RAG 链路**（契约已冻结，未改动） |
| `POST` | `/sessions` · `DELETE /sessions/{id}` | 会话管理 |
| `POST` | `/documents` · `GET /documents` · `DELETE /documents/{id}` | 文档入库/列表/删除 |
| `GET` | `/documents/{task_id}/status` | 入库任务状态轮询 |
| **`POST`** | **`/agent/stream`** | **Agent 主入口（SSE，含完整执行轨迹）** |
| **`POST`** | **`/agent/route`** | **规则路由调试（纯函数，不触发 LLM/DB）** |
| **`GET`** | **`/usage`** · **`/usage/sql`** | **LLM 成本看板 / SQL 执行健康度** |

**鉴权**：请求头 `X-API-Key`；`RAG_TENANT_KEYS=tenantA=keyA;tenantB=keyB` 配置租户映射，
`RAG_TENANT_ROLES=tenantB=employee` 配置角色。留空则鉴权软关闭（本地调试零成本）。

---

## 九、工程实践与踩坑记录

本项目把**真实踩过的坑**沉淀成了 [docs/经验教训.md](docs/经验教训.md)（19 条），
每条都是"现象 → 根因 → 处置 → 可执行的规避规则"。几条最有代表性的：

| # | 坑 | 教训 |
|---|---|---|
| L-007 | 裸 `SELECT` 被 sqlglot 解析成合法语句，包裹后生成语法非法的 SQL | **"解析成功" ≠ "语句合法"**；实测确证了官方所说的"宽松解析" |
| L-009 | guard 注入 `LIMIT 200` 导致执行器的"多读一行"截断判定**失效**；且截断警告写在到不了的分支里 | 靠"多读一行"判断边界时，必须回头检查上游有没有把上界卡死 |
| L-010 | 评测指标用严格相等，把 15 个语义正确（但多返回了识别性列）的答案判成错误 | **指标错比模型错更难发现**，因为它看起来像"模型不行" |
| L-011 | 歧义检测把「Sales 部门的平均薪资」也拦下反问 | **过度澄清比不澄清更糟**——把能答的问题变成答不了 |
| L-012 | 把概率性行为写成确定性断言 → 必然 flaky；追查时发现真实缺陷 | flaky 测试是**探测器**，不要靠重跑掩盖 |
| L-013 | 上游偶发 504 + 零重试；错误被包装成完全无关的信息 | 长链路必须重试；**不能只报最外层异常** |
| L-014 | 给工具加参数后测试替身没同步签名 → 25 例失败 | 替身**不要用 `**kwargs` 吞参数**：响亮失败 >> 安静失效 |
| L-015 | 修 `next_seq` 竞态 | **必须先写"能复现竞态"的反证测试**，否则无法区分"修好了"与"没测到" |

---

## 十、已知不足（诚实清单）

1. **评测面窄**：Text2SQL 只有 42 题、4 张表；检索评测**不含答案质量**（faithfulness / LLM-as-judge）。
2. **脱敏靠列名匹配**：模型给敏感列起别名（`SELECT salary AS s`）则匹配不到。
   彻底方案是数据库列级权限 `GRANT SELECT(col)`。
3. **worker 单并发**（`max_jobs=1`）：大文件会堵队尾。
4. **rerank 外呼无缓存**：每次查询都打 API。
5. **无 Prometheus 指标聚合 / 无结构化日志 / trace_id 未贯穿** HTTP→Agent→worker。
6. **无备份与对账**：PG 与 Qdrant payload 可能漂移。
7. **静态 API Key**：无过期/轮换/吊销；无 JWT/OIDC。

---

## 十一、文档索引

| 文档 | 内容 |
|---|---|
| [docs/升级为内部知识库Agent-完整改造方案.md](docs/升级为内部知识库Agent-完整改造方案.md) | 完整改造方案：结构梳理、优缺点诊断、可行性调研、8 阶段计划、简历包装 |
| [docs/text2sql-数据层设计.md](docs/text2sql-数据层设计.md) | 可直接执行的 DDL、只读角色、RLS、ETL 与数据源边界 |
| [docs/经验教训.md](docs/经验教训.md) | 19 条真实踩坑记录与方法学规则 |
| [docs/项目全景深度解析.md](docs/项目全景深度解析.md) | 原有 RAG 模块逐一梳理与端到端调用链 |
| [docs/RAG面试题库_初中高60题.md](docs/RAG面试题库_初中高60题.md) | RAG 面试题库 |
