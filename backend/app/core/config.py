"""
统一配置中心（pydantic-settings）

单一事实来源：合并原 src/config.py 的全部配置项，并新增企业级所需的
PostgreSQL / Redis / CORS 等。保持与现有根 .env 的扁平变量名 100% 兼容
（大小写不敏感），无需改写 .env 即可运行。

env_file 查找顺序（后者覆盖前者，均为绝对路径，避免受启动目录影响）：
    项目根 /.env  ->  backend/.env
容器内两者都不存在时，配置全部来自环境变量（12-factor）。
"""

from functools import lru_cache
from pathlib import Path
from typing import Dict, List

from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/app/core/config.py -> parents[2] = backend/，其 parent = 项目根
_BACKEND_DIR = Path(__file__).resolve().parents[2]
_PROJECT_ROOT = _BACKEND_DIR.parent


class Settings(BaseSettings):
    """全局配置。字段名与 .env 变量大小写不敏感匹配。"""

    model_config = SettingsConfigDict(
        env_file=(str(_PROJECT_ROOT / ".env"), str(_BACKEND_DIR / ".env")),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ==================== 应用 ====================
    app_name: str = "Enterprise RAG"
    environment: str = "development"          # development / staging / production
    debug: bool = False
    log_level: str = "INFO"
    host: str = "0.0.0.0"
    port: int = 8000

    # ==================== Qdrant 向量库 ====================
    qdrant_host: str = "localhost"
    qdrant_port: int = 6333
    qdrant_collection_name: str = "personal_rag_v2"

    # ==================== SiliconFlow（LLM + Embedding）====================
    siliconflow_api_key: str = ""
    siliconflow_base_url: str = "https://api.siliconflow.cn/v1"
    embedding_model: str = "BAAI/bge-m3"
    vector_dim: int = 1024
    llm_model: str = "deepseek-ai/DeepSeek-V3.2"
    llm_temperature: float = 0.3
    llm_max_tokens: int = 4096

    # ==================== Chunk ====================
    default_chunk_size: int = 512
    default_chunk_overlap: int = 50

    # ==================== 文档解析 ====================
    pdf_subdir: str = "pdf_examples"          # 知识库内 PDF 示例子目录名
    use_marker_for_pdf: bool = True           # 无线表格 PDF 三级提取失败时用 marker 转 md（未装则优雅降级）

    # ==================== 检索（dense + BM25 混合 + RRF）====================
    top_k: int = 5
    retrieval_mode: str = "hybrid"            # vector | hybrid
    prefetch_k: int = 20
    hybrid_tokenize: bool = True              # jieba 预分词，未装自动降级
    index_filename_prefix: bool = True        # 入库时块文本头部注入文档名（BM25/rerank 对称可见，问题点名文件名时可精确命中；改动需全量重灌生效）

    # ==================== Rerank 精排 ====================
    rerank_enabled: bool = True
    rerank_model: str = "BAAI/bge-reranker-v2-m3"
    rerank_candidates: int = 20

    # ==================== 低相关过滤（仅对 rerank 绝对分生效）====================
    score_rel_ratio: float = 0.15
    score_abs_min: float = 0.05
    score_confident: float = 0.5
    score_min_keep: int = 1
    score_drop_all_below: bool = False

    # ==================== 鉴权与多租户 ====================
    tenant_field: str = "tenant_id"
    rag_tenant_keys: str = ""                 # 格式: tenant_id=api_key;tenant2=key2

    # ==================== Langfuse 可观测（软依赖，默认关闭）====================
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "https://cloud.langfuse.com"

    # ==================== 会话 ====================
    session_ttl_seconds: int = 3600
    max_history_messages: int = 20

    # ==================== PostgreSQL（P3 起启用）====================
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_user: str = "rag"
    postgres_password: str = "rag"
    postgres_db: str = "rag"
    postgres_pool_size: int = 20
    postgres_max_overflow: int = 10

    # ==================== Analytics 只读库（Text2SQL，P1 起启用）====================
    # 设计：业务数据放独立库 kb_analytics，由独立只读角色 kb_ro 访问，
    # 与系统元数据库 rag 物理隔离 —— LLM 生成的 SQL 只能碰业务表，
    # 永远碰不到 messages（会话内容）/ tenants（租户表）。
    # 见 docs/text2sql-数据层设计.md 第一、二节。
    analytics_db: str = "kb_analytics"
    analytics_ro_user: str = "kb_ro"
    # 只读角色口令。留空 = 不设口令（本地 docker 映射到 127.0.0.1，pg_hba 为 trust，
    # 可直接连接）；生产环境务必通过环境变量注入强口令。
    analytics_ro_password: str = ""

    # ---- Text2SQL 执行护栏（安全网的第二层，见设计文档第五节）----
    # 单次查询返回行数硬上限：超限截断，并在结果里标注 truncated=true
    sql_max_rows: int = 200
    # 单条 SQL 的语句级超时（毫秒）：设在连接上，保证 PG 侧真的停下
    sql_timeout_ms: int = 5000
    # 结果集超过该行数时，不再把明细塞进 prompt，改为只给"统计概要"
    # 理由：LLM 无法预知结果集大小，上千行明细会直接炸掉上下文窗口
    sql_inline_max_rows: int = 50
    # LLM 生成 SQL 失败后，允许带错误信息重写的最多次数
    sql_max_retry: int = 2

    # ==================== Redis（缓存 / 会话 / ARQ 队列，P3+P4 起启用）====================
    redis_url: str = "redis://localhost:6379/0"
    arq_queue_name: str = "rag_ingest"

    # ==================== CORS ====================
    # 逗号分隔，便于 .env 直接书写；用 allowed_origins 属性取列表
    cors_origins: str = "http://localhost:5173,http://localhost:3000,http://localhost:8080"

    # ==================== 派生属性 ====================
    @property
    def base_dir(self) -> Path:
        return _PROJECT_ROOT

    @property
    def knowledge_base_dir(self) -> Path:
        return _PROJECT_ROOT / "knowledge_base"

    @property
    def processed_cache_file(self) -> Path:
        return _PROJECT_ROOT / "processed_cache" / "cache.json"

    @property
    def test_dataset_file(self) -> Path:
        return _PROJECT_ROOT / "test_dataset" / "qa_test_v2.jsonl"

    @property
    def allowed_origins(self) -> List[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def tenant_keys(self) -> Dict[str, str]:
        """解析 RAG_TENANT_KEYS -> {api_key: tenant_id}。留空则鉴权关闭。"""
        mapping: Dict[str, str] = {}
        for part in self.rag_tenant_keys.replace("\n", "").split(";"):
            part = part.strip()
            if not part or "=" not in part:
                continue
            tenant_id, api_key = part.split("=", 1)
            tenant_id, api_key = tenant_id.strip(), api_key.strip()
            if tenant_id and api_key:
                mapping[api_key] = tenant_id
        return mapping

    @property
    def auth_enabled(self) -> bool:
        return bool(self.tenant_keys)

    @property
    def langfuse_enabled(self) -> bool:
        return bool(self.langfuse_public_key and self.langfuse_secret_key)

    @property
    def postgres_url(self) -> str:
        """async SQLAlchemy 连接串（asyncpg 驱动）。"""
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def sync_postgres_url(self) -> str:
        """Alembic 迁移用的同步连接串（psycopg 驱动）。"""
        return (
            f"postgresql+psycopg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def analytics_url(self) -> str:
        """Analytics 只读库连接串（asyncpg 驱动，用只读角色）。

        Text2SQL 执行器只用这一条连接，配合：
        - 只读角色 kb_ro（GRANT SELECT only）
        - 会话级 SET LOCAL transaction_read_only = on
        - statement_timeout
        三道防线保证 LLM 生成的 SQL 无法写库。
        """
        auth = self.analytics_ro_user
        if self.analytics_ro_password:
            auth = f"{auth}:{self.analytics_ro_password}"
        return (
            f"postgresql+asyncpg://{auth}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.analytics_db}"
        )

    @property
    def sync_analytics_url(self) -> str:
        """建表 / 装载用的同步连接串（psycopg 驱动，用可写账号）。

        注意：这里刻意用 postgres_user（可写）而不是 kb_ro ——
        ETL 建表需要 DDL 权限，而 kb_ro 只应有 SELECT。
        """
        return (
            f"postgresql+psycopg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.analytics_db}"
        )

    @property
    def sync_maintenance_url(self) -> str:
        """维护连接串：连到默认库（rag），用于 CREATE DATABASE 等不能在本库内做的事。"""
        return (
            f"postgresql+psycopg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def supported_extensions(self) -> set[str]:
        return {".pdf", ".docx", ".xlsx", ".md", ".txt"}


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
