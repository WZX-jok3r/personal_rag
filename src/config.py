"""
config.py - 全局配置中心
所有模块通过 from config import * 获取统一配置

变更说明:
- Embedding: 使用 SiliconFlow API + BAAI/bge-m3 (dim=1024)
- LLM: 使用 SiliconFlow API + deepseek-ai/DeepSeek-V3.2
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# 加载 .env 文件
load_dotenv()

# ==================== 项目路径 ====================
BASE_DIR = Path(__file__).parent.parent  # src/ 的上一级

KNOWLEDGE_BASE_DIR = BASE_DIR / "knowledge_base"
PROCESSED_CACHE_FILE = BASE_DIR / "processed_cache" / "cache.json"
TEST_DATASET_FILE = BASE_DIR / "test_dataset" / "qa_test.jsonl"

# 确保缓存目录存在
PROCESSED_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)

# ==================== Qdrant 向量库配置 ====================
QDRANT_HOST = os.getenv("QDRANT_HOST", "localhost")
QDRANT_PORT = int(os.getenv("QDRANT_PORT", "6333"))
QDRANT_COLLECTION_NAME = os.getenv("QDRANT_COLLECTION_NAME", "personal_rag")

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