"""
ingest.py - 入口脚本：扫描知识库，增量入库

核心逻辑:
1. 扫描 knowledge_base 目录，收集所有文件
2. 对比 processed_cache，判断文件是否已处理/是否变更
3. 新增/变更的文件：清理旧向量 → 解析 → 分块 → Embedding → 写入 Qdrant
4. 更新 processed_cache 记录（增量保存）

用法:
    python src/ingest.py              # 全量扫描并入库
    python src/ingest.py --force      # 强制重新处理所有文件（忽略缓存）
    python src/ingest.py --reset      # 清空 Qdrant collection 和缓存，重新全量入库
"""

import json
import time
import argparse
import logging
from pathlib import Path
from typing import Dict, List, Any

from config import (
    KNOWLEDGE_BASE_DIR,
    PROCESSED_CACHE_FILE,
    SUPPORTED_EXTENSIONS,
    PDF_SUBDIR,
)
from document_loader import (
    load_file,
    compute_file_hash,
    scan_knowledge_base,
    Document,
)
from chunk_strategybak import chunk_document, Chunk, chunks_to_json
from vector_store import get_vector_store, VectorStore

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


class ProcessedCache:
    """processed_cache 管理器"""

    def __init__(self, cache_file: Path):
        self.cache_file = cache_file
        self.cache_file.parent.mkdir(parents=True, exist_ok=True)
        self.data = self._load()
        self._dirty = False  # 标记是否有未保存的变更

    def _load(self) -> Dict[str, Any]:
        """加载缓存文件"""
        if self.cache_file.exists():
            try:
                with open(self.cache_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except json.JSONDecodeError:
                logger.warning("[Cache] 缓存文件损坏，将重建")
                return {}
        return {}

    def save(self, force: bool = False):
        """保存缓存到文件（仅在数据变更时写入）"""
        if not self._dirty and not force:
            return
        with open(self.cache_file, "w", encoding="utf-8") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)
        self._dirty = False
        logger.info(f"[Cache] 缓存已保存: {self.cache_file}")

    def get_file_record(self, rel_path: str) -> Dict[str, Any]:
        """获取单个文件的缓存记录"""
        return self.data.get(rel_path, {})

    def set_file_record(self, rel_path: str, record: Dict[str, Any]):
        """设置单个文件的缓存记录"""
        self.data[rel_path] = record
        self._dirty = True

    def is_file_unchanged(self, rel_path: str, file_hash: str) -> bool:
        """判断文件是否未变更"""
        record = self.get_file_record(rel_path)
        return record.get("file_hash") == file_hash and record.get("status") == "success"

    def clear(self):
        """清空缓存"""
        self.data = {}
        self._dirty = True
        logger.info("[Cache] 缓存已清空")


def _get_rel_path(file_path: Path) -> str:
    """获取相对于知识库根目录的路径，作为跨环境稳定的 cache key 和 metadata source"""
    try:
        return str(file_path.relative_to(KNOWLEDGE_BASE_DIR))
    except ValueError:
        # 兜底：如果文件不在 KNOWLEDGE_BASE_DIR 下，使用文件名
        return file_path.name


def process_file(file_path: Path, vector_store: VectorStore) -> Dict[str, Any]:
    """
    处理单个文件：清理旧向量 → 解析 → 分块 → 向量化入库
    返回处理记录，用于更新 cache
    """
    rel_path = _get_rel_path(file_path)
    start_time = time.time()
    logger.info(f"[Process] 开始处理: {file_path.name}")

    # ✅ P0: 无论新增/变更/空文件，先清理该文件的所有旧向量，保证数据一致性
    deleted = vector_store.delete_by_metadata({"source": rel_path})
    if deleted > 0:
        logger.info(f"[Ingest] 已清理 {rel_path} 的旧向量")

    # 1. 解析
    documents = load_file(file_path)
    if not documents:
        logger.warning(f"[Process] 文件解析结果为空: {file_path.name}")
        return {
            "file_hash": compute_file_hash(file_path),
            "status": "empty",
            "chunk_count": 0,
            "processing_time": round(time.time() - start_time, 2),
        }

    logger.info(f"[Process] {file_path.name}: 解析得到 {len(documents)} 个文档块")

    # 2. 按文件分组（转成 dict 格式供 chunk_strategy 使用）
    doc_dicts = [doc.to_dict() for doc in documents]

    # 3. 分块
    chunks = chunk_document(doc_dicts)
    logger.info(f"[Process] {file_path.name}: 切分为 {len(chunks)} 个 chunk")

    # 4. 向量化入库（内部应支持批量 embedding + 批量 upsert）
    chunk_dicts = [chunk.to_dict() for chunk in chunks]
    inserted = vector_store.upsert_chunks(chunk_dicts)
    logger.info(f"[Process] {file_path.name}: 已写入 {inserted} 条向量")

    # 5. 返回处理记录
    return {
        "file_hash": compute_file_hash(file_path),
        "status": "success",
        "chunk_count": len(chunks),
        "inserted_count": inserted,
        "format": file_path.suffix.lower().lstrip("."),
        "processing_time": round(time.time() - start_time, 2),
    }


def run_ingest(force: bool = False, reset: bool = False):
    """
    执行入库流程

    Args:
        force: 强制重新处理所有文件（忽略缓存）
        reset: 清空 Qdrant 和缓存，重新全量入库
    """
    # 初始化
    cache = ProcessedCache(PROCESSED_CACHE_FILE)
    vector_store = get_vector_store()

    # 处理 reset（带交互确认）
    if reset:
        confirm = input("⚠️  确认清空 Qdrant 和缓存？此操作不可逆 (y/N): ")
        if confirm.lower() != "y":
            logger.info("[Ingest] 已取消重置操作")
            return
        logger.warning("[Ingest] 执行重置操作...")
        vector_store.delete_collection()
        vector_store._ensure_collection()  # 重新创建
        cache.clear()
        cache.save(force=True)
        force = True  # reset 后强制全量处理

    # 扫描文件
    file_paths = scan_knowledge_base()
    if not file_paths:
        logger.error("[Ingest] 知识库目录为空或未找到支持的文件")
        return

    logger.info(f"[Ingest] 扫描到 {len(file_paths)} 个文件")

    # 统计
    stats = {
        "total_files": len(file_paths),
        "processed": 0,
        "skipped": 0,
        "failed": 0,
        "empty": 0,
        "total_chunks": 0,
        "total_processing_time": 0.0,
    }

    # 逐个处理
    for fp in file_paths:
        rel_path = _get_rel_path(fp)
        file_hash = compute_file_hash(fp)

        # 判断是否跳过（仅当缓存状态为 success 且 hash 一致时跳过）
        if not force and cache.is_file_unchanged(rel_path, file_hash):
            logger.info(f"[Ingest] 跳过未变更文件: {fp.name}")
            stats["skipped"] += 1
            continue

        # 处理文件
        try:
            record = process_file(fp, vector_store)
            cache.set_file_record(rel_path, record)
            # ✅ P1: 每处理完一个文件立即保存缓存，防止中途崩溃导致重跑
            cache.save()

            stats["total_processing_time"] += record.get("processing_time", 0)

            if record["status"] == "empty":
                stats["empty"] += 1
            elif record["status"] == "success":
                stats["processed"] += 1
                stats["total_chunks"] += record.get("chunk_count", 0)
            else:
                stats["failed"] += 1

        except Exception as e:
            logger.error(f"[Ingest] 处理失败 {fp.name}: {e}", exc_info=True)
            cache.set_file_record(rel_path, {
                "file_hash": file_hash,
                "status": "failed",
                "error": str(e),
            })
            cache.save()
            stats["failed"] += 1

    # 最终统计
    total_vectors = vector_store.count()
    logger.info("=" * 50)
    logger.info("[Ingest] 入库完成!")
    logger.info(f"  总文件: {stats['total_files']}")
    logger.info(f"  成功处理: {stats['processed']}")
    logger.info(f"  空文件: {stats['empty']}")
    logger.info(f"  已跳过: {stats['skipped']}")
    logger.info(f"  失败: {stats['failed']}")
    logger.info(f"  总 chunk 数: {stats['total_chunks']}")
    logger.info(f"  总耗时: {round(stats['total_processing_time'], 2)}s")
    logger.info(f"  Qdrant 向量总数: {total_vectors}")
    logger.info("=" * 50)


def main():
    parser = argparse.ArgumentParser(description="RAG 知识库增量入库脚本")
    parser.add_argument("--force", action="store_true", help="强制重新处理所有文件")
    parser.add_argument("--reset", action="store_true", help="清空 Qdrant 和缓存，重新全量入库")
    args = parser.parse_args()

    run_ingest(force=args.force, reset=args.reset)


if __name__ == "__main__":
    main()
