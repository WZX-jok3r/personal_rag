"""
ingestion - 入库领域：解析(loader) → 分块(chunking) → 索引(indexer)

- loader.py  : 多格式解析（pdf/docx/xlsx/md/txt），迁自 src/document_loader.py
- chunking/  : 分块包（base/text/table），迁自 src/chunk_strategy.py
- indexer.py : 增量入库编排（ProcessedCache + process_file + run_ingest），迁自 src/ingest.py

对外主入口：load_file / scan_knowledge_base / load_all_documents / chunk_document / process_file / run_ingest。
"""

from app.ingestion.loader import (
    Document,
    compute_file_hash,
    load_all_documents,
    load_file,
    scan_knowledge_base,
)
from app.ingestion.chunking import Chunk, chunk_all, chunk_document, chunks_to_json
from app.ingestion.indexer import ProcessedCache, process_file, run_ingest

__all__ = [
    "Document", "compute_file_hash", "load_file", "scan_knowledge_base", "load_all_documents",
    "Chunk", "chunk_document", "chunk_all", "chunks_to_json",
    "ProcessedCache", "process_file", "run_ingest",
]
