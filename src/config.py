"""
config.py - 全局配置中心
所有模块通过 from config import * 获取统一配置

变更说明:
- Embedding: 使用 SiliconFlow API + BAAI/bge-m3 (dim=1024)
- LLM: 使用 SiliconFlow API + deepseek-ai/DeepSeek-V3.2
"""

import os
from pathlib import Path
from typing import Dict
from dotenv import load_dotenv

# 加载 .env 文件
load_dotenv()

# ==================== 项目路径 ====================
BASE_DIR = Path(__file__).parent.parent  # src/ 的上一级

KNOWLEDGE_BASE_DIR = BASE_DIR / "knowledge_base"
PROCESSED_CACHE_FILE = BASE_DIR / "processed_cache" / "cache.json"
TEST_DATASET_FILE = BASE_DIR / "test_dataset" / "qa_test_v2.jsonl"

# 确保缓存目录存在
PROCESSED_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)

# ==================== Qdrant 向量库配置 ====================
QDRANT_HOST = os.getenv("QDRANT_HOST", "localhost")
QDRANT_PORT = int(os.getenv("QDRANT_PORT", "6333"))
# v2: 混合检索需要 named vectors（dense + bm25），与 v1 单向量结构不兼容，
# 故启用新 collection；旧 personal_rag 保留，如需回滚改回 .env 即可
QDRANT_COLLECTION_NAME = os.getenv("QDRANT_COLLECTION_NAME", "personal_rag_v2")

# ==================== SiliconFlow API 配置 ====================
SILICONFLOW_API_KEY = os.getenv("SILICONFLOW_API_KEY", "")
SILICONFLOW_BASE_URL = os.getenv("SILICONFLOW_BASE_URL", "https://api.siliconflow.cn/v1")

# ==================== Embedding 模型配置 (SiliconFlow) ====================
# BAAI/bge-m3 向量维度为 1024
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "BAAI/bge-m3")
VECTOR_DIM = int(os.getenv("VECTOR_DIM", "1024"))

# ==================== LLM 配置 (SiliconFlow) ====================
LLM_MODEL = os.getenv("LLM_MODEL", "deepseek-ai/DeepSeek-V3.2")
LLM_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE", "0.3"))
LLM_MAX_TOKENS = int(os.getenv("LLM_MAX_TOKENS", "4096"))

# ==================== Chunk 配置 ====================
DEFAULT_CHUNK_SIZE = int(os.getenv("DEFAULT_CHUNK_SIZE", "512"))
DEFAULT_CHUNK_OVERLAP = int(os.getenv("DEFAULT_CHUNK_OVERLAP", "50"))

# ==================== 检索配置 ====================
TOP_K = int(os.getenv("TOP_K", "5"))

# ==================== 混合检索配置（dense + BM25 稀疏 + RRF 融合）====================
# RETRIEVAL_MODE: vector=纯向量检索（旧行为）；hybrid=dense+BM25 混合检索（RRF 融合）
RETRIEVAL_MODE = os.getenv("RETRIEVAL_MODE", "hybrid")
# 混合检索时各通道（dense / bm25）单独召回的候选数，融合后再截取 TOP_K
PREFETCH_K = int(os.getenv("PREFETCH_K", "20"))
# BM25 通道是否对文本做 jieba 预分词（先保护英数串，再切中文词）。
# 关闭时直接用原文；jieba 未安装时自动降级为关闭
HYBRID_TOKENIZE = os.getenv("HYBRID_TOKENIZE", "true").lower() in ("1", "true", "yes")

# ==================== Rerank 精排配置（SiliconFlow /v1/rerank）====================
# 开启后：召回 RERANK_CANDIDATES 条候选 -> 交叉编码器精排 -> 截取 TOP_K。
# 复用 SILICONFLOW_API_KEY 与 SILICONFLOW_BASE_URL（无需额外 Key）；API 失败时自动降级为原始排序
RERANK_ENABLED = os.getenv("RERANK_ENABLED", "true").lower() in ("1", "true", "yes")
RERANK_MODEL = os.getenv("RERANK_MODEL", "BAAI/bge-reranker-v2-m3")
# 参与精排的候选数（需 >= TOP_K，否则精排无意义）
RERANK_CANDIDATES = int(os.getenv("RERANK_CANDIDATES", "20"))

# ==================== 低相关结果过滤（仅对 rerank 绝对分生效）====================
# 开精排时 score 是交叉编码器输出的相关度（约 [0,1]），可用绝对阈值；
# 未开精排 / 精排降级时 score 是 RRF 融合分（量级仅 ~0.016），此时只做数量截断，
# 绝不套用绝对阈值，否则会把全部结果误杀。
SCORE_REL_RATIO = float(os.getenv("SCORE_REL_RATIO", "0.15"))   # 相对断层：低于最高分该比例的视为噪声
SCORE_ABS_MIN = float(os.getenv("SCORE_ABS_MIN", "0.05"))       # 绝对下限：兼作“整体无相关”的拒答判定线
SCORE_CONFIDENT = float(os.getenv("SCORE_CONFIDENT", "0.5"))    # 高水位：达到此分的结果无条件保留（不限量）
SCORE_MIN_KEEP = int(os.getenv("SCORE_MIN_KEEP", "1"))          # 保底条数：即使全部低于水位也至少保留最高分 N 条
SCORE_DROP_ALL_BELOW = os.getenv("SCORE_DROP_ALL_BELOW", "false").lower() in ("1", "true", "yes")
# 行为（仅对 rerank 分生效，详见 vector_store._filter_low_relevance）:
#   最高分 < SCORE_ABS_MIN         -> 整体无相关：根据 SCORE_DROP_ALL_BELOW 全丢（拒答）或仅保底 N 条
#   最高分 >= SCORE_CONFIDENT       -> 有强锤点，启用相对断层：阈值 = max(SCORE_ABS_MIN, 最高分*SCORE_REL_RATIO)
#   SCORE_ABS_MIN <= 最高分 < 高水位 -> 只有中等分，保守仅用绝对下限截断，不对中等分簇施断层（避免误杀）
#   过滤后为空                       -> 保底保留最高分 SCORE_MIN_KEEP 条（SCORE_MIN_KEEP=0 等价取消保底）

# ==================== 鉴权与多租户配置 ====================
# 多租户隔离字段名：入库写入该 metadata 字段，检索时服务端按此字段强制过滤
TENANT_FIELD = os.getenv("TENANT_FIELD", "tenant_id")


def _parse_tenant_keys(raw: str) -> Dict[str, str]:
    """
    解析租户 API Key 映射。
    格式: tenant_id=api_key;tenant_id2=api_key2  （分号分隔，等号左右去空白）
    返回: {api_key: tenant_id}
    留空 -> 返回空字典 -> 鉴权关闭（开发模式，不做租户过滤，行为与加鉴权前一致）
    """
    mapping: Dict[str, str] = {}
    for part in raw.replace("\n", "").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        tenant_id, api_key = part.split("=", 1)
        tenant_id, api_key = tenant_id.strip(), api_key.strip()
        if tenant_id and api_key:
            mapping[api_key] = tenant_id
    return mapping


# 租户密钥表：由 .env 的 RAG_TENANT_KEYS 提供；为空则整套鉴权/租户隔离自动关闭
TENANT_KEYS = _parse_tenant_keys(os.getenv("RAG_TENANT_KEYS", ""))
# 鉴权是否启用（配置了任意租户 Key 即启用）
AUTH_ENABLED = bool(TENANT_KEYS)

# ==================== 可观测配置（Langfuse，软依赖，默认关闭）====================
# 仅在同时配置 public+secret 两把密钥时才启用；否则埋点全部 no-op，
# 对主链路零影响、零网络调用。需安装 langfuse 包: pip install "langfuse>=3.0.0"
# 云端与自托管均可：自托管把 LANGFUSE_HOST 改成自己的服务地址即可。
LANGFUSE_PUBLIC_KEY = os.getenv("LANGFUSE_PUBLIC_KEY", "")
LANGFUSE_SECRET_KEY = os.getenv("LANGFUSE_SECRET_KEY", "")
# 兼容两种变量名： LANGFUSE_HOST 或 LANGFUSE_BASE_URL（都映射到 SDK 的 host）
LANGFUSE_HOST = os.getenv("LANGFUSE_HOST") or os.getenv("LANGFUSE_BASE_URL") or "https://cloud.langfuse.com"
LANGFUSE_ENABLED = bool(LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY)

# ==================== 支持的文件格式 ====================
SUPPORTED_EXTENSIONS = {
    ".pdf",
    ".docx",
    ".xlsx",
    ".md",
    ".txt",
}

# PDF 子目录
PDF_SUBDIR = "pdf_examples"

# ==================== Marker 深度表格识别 ====================
# 开启后，遇到"无线/纯文本对齐表格"的 PDF（三级传统提取失败时），
# 会用 marker 转 Markdown，由 chunk 层自动走 md 表格感知切分。
# 需已安装 marker-pdf；未安装时会优雅降级，不影响现有功能。
USE_MARKER_FOR_PDF = True
