"""
vector_store.py - Qdrant 向量库连接、写入、检索、混合检索逻辑

职责:
1. 连接 Qdrant（本地 Docker）
2. 调用 SiliconFlow API 获取 BAAI/bge-m3 embedding
3. 创建/管理 collection
4. 写入向量（带 metadata）
5. 检索向量（相似度搜索）
6. 支持增量更新（通过 processed_cache 判断）
"""

import json
import hashlib
import logging
from typing import List, Dict, Any, Optional
from pathlib import Path

import requests
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    VectorParams,
    PointStruct,
    Filter,
    FieldCondition,
    MatchValue,
)

from config import (
    QDRANT_HOST,
    QDRANT_PORT,
    QDRANT_COLLECTION_NAME,
    VECTOR_DIM,
    SILICONFLOW_API_KEY,
    SILICONFLOW_BASE_URL,
    EMBEDDING_MODEL,
    TOP_K,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


class EmbeddingClient:
    """SiliconFlow Embedding API 客户端"""

    def __init__(self):
        self.api_key = SILICONFLOW_API_KEY
        self.base_url = SILICONFLOW_BASE_URL.rstrip("/")
        self.model = EMBEDDING_MODEL

        if not self.api_key:
            raise ValueError("SILICONFLOW_API_KEY 未配置，请在 .env 中设置")

    def embed(self, texts: List[str]) -> List[List[float]]:
        """
        批量获取文本的 embedding 向量
        SiliconFlow embedding API 支持批量，但建议单次不超过 100 条
        """
        if not texts:
            return []

        url = f"{self.base_url}/embeddings"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        # SiliconFlow embedding API 格式
        payload = {
            "model": self.model,
            "input": texts,
            "encoding_format": "float",
        }

        try:
            response = requests.post(url, headers=headers, json=payload, timeout=60)
            response.raise_for_status()
            data = response.json()

            # 解析返回结果
            embeddings = []
            for item in data["data"]:
                embeddings.append(item["embedding"])

            logger.info(f"[Embedding] 成功获取 {len(embeddings)} 个向量，维度={len(embeddings[0]) if embeddings else 0}")
            return embeddings

        except requests.exceptions.RequestException as e:
            logger.error(f"[Embedding] API 请求失败: {e}")
            raise
        except (KeyError, IndexError) as e:
            logger.error(f"[Embedding] 响应解析失败: {e}, 响应内容: {response.text[:500]}")
            raise

    def embed_single(self, text: str) -> List[float]:
        """获取单条文本的 embedding"""
        results = self.embed([text])
        return results[0] if results else []


class VectorStore:
    """Qdrant 向量库封装"""

    def __init__(self):
        self.client = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)
        self.collection_name = QDRANT_COLLECTION_NAME
        self.embedding_client = EmbeddingClient()
        self.vector_dim = VECTOR_DIM

        # 确保 collection 存在
        self._ensure_collection()

    def _ensure_collection(self):
        """检查并创建 collection"""
        collections = self.client.get_collections().collections
        collection_names = [c.name for c in collections]

        if self.collection_name not in collection_names:
            logger.info(f"[Qdrant] 创建 collection: {self.collection_name}, dim={self.vector_dim}")
            self.client.create_collection(
                collection_name=self.collection_name,
                vectors_config=VectorParams(size=self.vector_dim, distance=Distance.COSINE),
            )
        else:
            logger.info(f"[Qdrant] collection 已存在: {self.collection_name}")

    def delete_collection(self):
        """删除 collection（用于重置）"""
        logger.warning(f"[Qdrant] 删除 collection: {self.collection_name}")
        self.client.delete_collection(collection_name=self.collection_name)

    def upsert_chunks(self, chunks: List[Dict[str, Any]], batch_size: int = 50) -> int:
        """
        将 chunks 写入 Qdrant
        - 先获取 embedding
        - 再批量写入
        返回写入的 chunk 数量
        """
        if not chunks:
            logger.warning("[Upsert] 无 chunk 需要写入")
            return 0

        total_inserted = 0

        # 分批处理（避免 API 超时或内存溢出）
        for i in range(0, len(chunks), batch_size):
            batch = chunks[i:i + batch_size]
            texts = [c["text"] for c in batch]

            # 获取 embedding
            embeddings = self.embedding_client.embed(texts)

            # 构建 PointStruct
            points = []
            for j, (chunk, vector) in enumerate(zip(batch, embeddings)):
                point_id = self._generate_id(chunk["text"], chunk["metadata"])
                points.append(
                    PointStruct(
                        id=point_id,
                        vector=vector,
                        payload={
                            "text": chunk["text"],
                            **chunk["metadata"],  # 展开所有 metadata
                        },
                    )
                )

            # 写入 Qdrant
            self.client.upsert(
                collection_name=self.collection_name,
                points=points,
            )

            total_inserted += len(points)
            logger.info(f"[Upsert] 批次 {i // batch_size + 1}: 写入 {len(points)} 个 chunk")

        logger.info(f"[Upsert] 总计写入 {total_inserted} 个 chunk")
        return total_inserted

    def search(self, query: str, top_k: int = TOP_K, filter_dict: Optional[Dict] = None) -> List[Dict[str, Any]]:
        """
        向量检索
        - query: 查询文本
        - top_k: 返回数量
        - filter_dict: 可选的过滤条件，如 {"format": "pdf"}
        返回: List[{"text": str, "score": float, "metadata": dict}]
        """
        # 获取查询向量
        query_vector = self.embedding_client.embed_single(query)
        # 构建过滤条件
        query_filter = None
        if filter_dict:
            conditions = []
            for key, value in filter_dict.items():
                conditions.append(
                    FieldCondition(key=key, match=MatchValue(value=value))
                )
            if conditions:
                query_filter = Filter(must=conditions)
        # v2 接口 query_points
        results = self.client.query_points(
            collection_name=self.collection_name,
            query=query_vector,
            limit=top_k,
            query_filter=query_filter,
            with_payload=True,
        )
        # 格式化返回
        formatted = []
        for r in results.points:
            formatted.append({
                "text": r.payload.get("text", ""),
                "score": r.score,
                "metadata": {k: v for k, v in r.payload.items() if k != "text"},
            })
        logger.info(f"[Search] 查询 \"{query[:30]}...\" 返回 {len(formatted)} 条结果")
        return formatted

    def count(self) -> int:
        """获取 collection 中的向量数量"""
        return self.client.count(collection_name=self.collection_name).count

    def _generate_id(self, text: str, metadata: Dict[str, Any]) -> str:
        """
        生成唯一 point ID
        基于 text + source 的 hash，确保同一内容重复写入时覆盖旧数据
        """
        source = metadata.get("source", "")
        unique_str = f"{source}:{text[:200]}"  # 取前200字符避免过长
        return hashlib.md5(unique_str.encode("utf-8")).hexdigest()


# ==================== 便捷函数 ====================

def get_vector_store() -> VectorStore:
    """获取 VectorStore 实例（单例模式）"""
    return VectorStore()


if __name__ == "__main__":
    # 本地测试
    vs = get_vector_store()
    print(f"Collection 向量数: {vs.count()}")

    # 测试写入
    test_chunks = [
        {"text": "这是一个测试文档，关于路由器的质保政策", "metadata": {"format": "txt", "source": "test.txt"}},
        {"text": "路由器整机质保24个月，配件质保12个月", "metadata": {"format": "md", "source": "test.md"}},
    ]
    vs.upsert_chunks(test_chunks)
    print(f"写入后向量数: {vs.count()}")

    # 测试检索
    results = vs.search("路由器质保多久")
    for r in results:
        print(f"\n[score={r['score']:.4f}] {r['text'][:100]}")
        print(f"  metadata: {r['metadata']}")
