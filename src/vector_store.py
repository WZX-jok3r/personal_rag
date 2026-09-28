"""
vector_store.py - Qdrant 向量库连接、写入、检索、混合检索逻辑

职责:
1. 连接 Qdrant（本地 Docker）
2. 调用 SiliconFlow API 获取 BAAI/bge-m3 embedding
3. 创建/管理 collection
4. 写入向量（带 metadata）
5. 检索向量（混合检索 dense+BM25+RRF）
6. 支持增量更新（通过 processed_cache 判断）
7. 支持按 metadata 删除向量（用于文件更新时清理旧版本）
8. Rerank 精排（SiliconFlow /v1/rerank，开关控制，失败自动降级）
"""

import json
import hashlib
import logging
import re
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
    SparseVectorParams,
    Modifier,
    Document,
    Prefetch,
    FusionQuery,
    Fusion,
    PayloadSchemaType,
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
    RETRIEVAL_MODE,
    PREFETCH_K,
    HYBRID_TOKENIZE,
    RERANK_ENABLED,
    RERANK_MODEL,
    RERANK_CANDIDATES,
    TENANT_FIELD,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# ==================== BM25 稀疏通道：中文预分词 ====================
# 可选依赖：jieba（纯 Python，用于中文分词）；未安装时自动降级为原文投喂 BM25
try:
    import jieba
    _JIEBA_AVAILABLE = True
except ImportError:
    _JIEBA_AVAILABLE = False

if HYBRID_TOKENIZE and not _JIEBA_AVAILABLE:
    logger.warning("[Tokenize] HYBRID_TOKENIZE 已开启但 jieba 未安装，BM25 通道将使用原文（中文匹配能力受限）")

# 英数串保护正则：型号/SKU/金额/页码等 token 整体保留，避免被 jieba 拆碎
_ASCII_RUN_RE = re.compile(r"[A-Za-z0-9]+(?:[.\-_/][A-Za-z0-9]+)*")


def tokenize_for_bm25(text: str) -> str:
    """
    为 BM25 稀疏通道做预分词（入库与查询必须对称调用）：
    - 先保护英数串（PS-12V-1.5A、BHG-24667 等）整体成 token
    - 其余中文段用 jieba 切词
    - jieba 未安装或开关关闭时直接返回原文
    """
    if not _JIEBA_AVAILABLE or not HYBRID_TOKENIZE:
        return text

    tokens: List[str] = []
    pos = 0
    for m in _ASCII_RUN_RE.finditer(text):
        seg = text[pos:m.start()]
        if seg.strip():
            tokens.extend(t for t in jieba.cut(seg) if t.strip())
        tokens.append(m.group(0))
        pos = m.end()
    seg = text[pos:]
    if seg.strip():
        tokens.extend(t for t in jieba.cut(seg) if t.strip())
    return " ".join(tokens)


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


class RerankClient:
    """SiliconFlow Rerank API 客户端（交叉编码器精排，与 Embedding 共用 Key/Base URL）"""

    def __init__(self):
        self.api_key = SILICONFLOW_API_KEY
        self.base_url = SILICONFLOW_BASE_URL.rstrip("/")
        self.model = RERANK_MODEL

        if not self.api_key:
            raise ValueError("SILICONFLOW_API_KEY 未配置，请在 .env 中设置")

    def rerank(self, query: str, documents: List[str], top_n: int) -> List[Dict[str, Any]]:
        """
        对候选文档按 (query, document) 相关性精排
        返回: [{"index": 原文在 documents 中的下标, "relevance_score": float}]（按相关度降序）
        异常抛给调用方做降级处理
        """
        if not documents:
            return []

        url = f"{self.base_url}/rerank"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "query": query,
            "documents": documents,
            "top_n": top_n,
            "return_documents": False,
        }

        response = requests.post(url, headers=headers, json=payload, timeout=30)
        response.raise_for_status()
        data = response.json()

        ranked = [
            {"index": item["index"], "relevance_score": item["relevance_score"]}
            for item in data["results"]
        ]
        logger.info(f"[Rerank] 候选 {len(documents)} 条精排完成，返回前 {len(ranked)} 条")
        return ranked


class VectorStore:
    """Qdrant 向量库封装"""

    def __init__(self):
        self.client = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)
        self.collection_name = QDRANT_COLLECTION_NAME
        self.embedding_client = EmbeddingClient()
        self.vector_dim = VECTOR_DIM
        # Rerank 精排客户端（RERANK_ENABLED 控制；精排 API 异常时检索路径自动降级）
        self.rerank_client = RerankClient() if RERANK_ENABLED else None
        if self.rerank_client:
            logger.info(f"[Rerank] 精排已启用: {RERANK_MODEL}（候选 {RERANK_CANDIDATES} 条）")

        # 确保 collection 存在
        self._ensure_collection()

    def _ensure_collection(self):
        """检查并（按需）创建 collection：named vectors = dense + bm25(sparse)"""
        collections = self.client.get_collections().collections
        collection_names = [c.name for c in collections]

        if self.collection_name not in collection_names:
            logger.info(f"[Qdrant] 创建 collection: {self.collection_name} (named vectors: dense + bm25)")
            self.client.create_collection(
                collection_name=self.collection_name,
                vectors_config={
                    "dense": VectorParams(size=self.vector_dim, distance=Distance.COSINE),
                },
                sparse_vectors_config={
                    "bm25": SparseVectorParams(modifier=Modifier.IDF),
                },
            )
        else:
            # 结构校验：v2 要求名为 "dense" 的稠密向量；旧版单无名向量结构不兼容
            info = self.client.get_collection(self.collection_name)
            vectors = info.config.params.vectors
            if not isinstance(vectors, dict) or "dense" not in vectors:
                raise RuntimeError(
                    f"collection '{self.collection_name}' 为旧版单向量结构，与混合检索（named vectors）不兼容。"
                    "请使用新的 collection 名，或执行 python src/ingest.py --reset 重建。"
                )
            logger.info(f"[Qdrant] collection 已存在: {self.collection_name}")

        # 为多租户隔离字段建 keyword 索引，避免按 tenant_id 过滤时逐点扫描（幂等，失败仅告警）
        self._ensure_tenant_index()

    def _ensure_tenant_index(self):
        """为租户字段创建 payload 索引，保障 tenant_id 过滤性能"""
        try:
            self.client.create_payload_index(
                collection_name=self.collection_name,
                field_name=TENANT_FIELD,
                field_schema=PayloadSchemaType.KEYWORD,
            )
            logger.info(f"[Qdrant] 已确保租户字段 payload 索引: {TENANT_FIELD}")
        except Exception as e:
            # 旧版 client 方法名差异或已存在时，不阻断启动
            logger.warning(f"[Qdrant] 创建租户字段索引跳过/失败（不影响主流程）: {e}")

    def delete_collection(self):
        """删除 collection（用于重置）"""
        logger.warning(f"[Qdrant] 删除 collection: {self.collection_name}")
        self.client.delete_collection(collection_name=self.collection_name)

    def delete_by_metadata(self, filter_dict: Dict[str, Any]) -> int:
        """
        根据 metadata 字段精确匹配删除向量
        用于增量更新时清理同一文件的旧版本向量

        Args:
            filter_dict: 要匹配的 metadata 键值对，如 {"source": "contract_snippet.md"}

        Returns:
            1 表示删除请求已成功提交，0 表示无匹配条件未执行删除
            注意: Qdrant delete API 不返回实际删除数量，仅返回操作确认
        """
        if not filter_dict:
            return 0

        # 构建 must 条件列表（所有条件都要满足）
        must_conditions = [
            FieldCondition(
                key=key,
                match=MatchValue(value=value)
            )
            for key, value in filter_dict.items()
        ]

        try:
            result = self.client.delete(
                collection_name=self.collection_name,
                points_selector=Filter(must=must_conditions),
                wait=True,  # 同步等待删除完成，保证后续 upsert 不会与删除并发冲突
            )
            # UpdateResult 只有 status 和 operation_id，没有 points_count
            # status == "completed" 即表示删除操作已成功执行
            if result.status == "completed":
                logger.info(f"[VectorStore] 按 metadata 删除请求已执行: {filter_dict}")
                return 1
            else:
                logger.warning(f"[VectorStore] 删除操作未完成，status={result.status}")
                return 0
        except Exception as e:
            logger.error(f"[VectorStore] delete_by_metadata 失败: {e}")
            raise

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

            # 构建 PointStruct（双通道：dense 向量 + bm25 稀疏向量）
            points = []
            for j, (chunk, vector) in enumerate(zip(batch, embeddings)):
                point_id = self._generate_id(chunk["text"], chunk["metadata"])
                points.append(
                    PointStruct(
                        id=point_id,
                        vector={
                            "dense": vector,
                            "bm25": Document(
                                text=tokenize_for_bm25(chunk["text"]),
                                model="qdrant/bm25",
                            ),
                        },
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
        检索（默认混合检索 + 可选精排）
        - RETRIEVAL_MODE=hybrid: dense + BM25 双通道召回，RRF 融合（推荐）
        - RETRIEVAL_MODE=vector: 仅 dense 向量检索（旧行为，做对照用）
        - RERANK_ENABLED=true: 先召回 RERANK_CANDIDATES 条候选，再交叉编码器精排取 top_k
        返回: List[{"text": str, "score": float, "metadata": dict}]
        """
        # 获取查询向量（dense 通道）
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

        # 候选召回条数：开启精排时多召回候选供重排，否则直接取 top_k
        limit = max(top_k, RERANK_CANDIDATES) if self.rerank_client else top_k

        if RETRIEVAL_MODE == "hybrid":
            results = self._hybrid_query(query, query_vector, limit, query_filter)
        else:
            results = self.client.query_points(
                collection_name=self.collection_name,
                query=query_vector,
                using="dense",
                limit=limit,
                query_filter=query_filter,
                with_payload=True,
            )

        points = results.points

        # Rerank 精排：候选按 (query, doc) 相关性重排后取 top_k；API 异常自动降级为原始排序
        if self.rerank_client and len(points) > 1:
            reranked = self._rerank_points(query, points, top_k)
            if reranked is not None:
                formatted = [
                    {
                        "text": p.payload.get("text", ""),
                        "score": score,
                        "metadata": {k: v for k, v in p.payload.items() if k != "text"},
                    }
                    for p, score in reranked
                ]
                logger.info(f"[Search] ({RETRIEVAL_MODE}+rerank) 查询 \"{query[:30]}...\" 返回 {len(formatted)} 条结果")
                return formatted

        # 未开启精排 / 精排降级：按召回原始顺序取前 top_k 条格式化
        formatted = []
        for r in points[:top_k]:
            formatted.append({
                "text": r.payload.get("text", ""),
                "score": r.score,
                "metadata": {k: v for k, v in r.payload.items() if k != "text"},
            })
        logger.info(f"[Search] ({RETRIEVAL_MODE}) 查询 \"{query[:30]}...\" 返回 {len(formatted)} 条结果")
        return formatted

    def _hybrid_query(self, query: str, query_vector: List[float], top_k: int,
                      query_filter: Optional[Filter]):
        """
        混合检索核心：dense 与 BM25 各取 PREFETCH_K 候选，RRF 排名融合后取 top_k。
        - filter 同时挂在各通道与顶层，保证子查询各自受限且结果不含被过滤项
        - BM25 通道查询文本经 tokenize_for_bm25 切分（与入库对称）
        """
        return self.client.query_points(
            collection_name=self.collection_name,
            prefetch=[
                Prefetch(
                    query=query_vector,
                    using="dense",
                    limit=PREFETCH_K,
                    filter=query_filter,
                ),
                Prefetch(
                    query=Document(text=tokenize_for_bm25(query), model="qdrant/bm25"),
                    using="bm25",
                    limit=PREFETCH_K,
                    filter=query_filter,
                ),
            ],
            query=FusionQuery(fusion=Fusion.RRF),
            limit=top_k,
            query_filter=query_filter,
            with_payload=True,
        )

    def _rerank_points(self, query: str, points: List[Any], top_k: int):
        """
        精排候选：调用 Rerank API 按相关性重排，返回 [(point, relevance_score), ...]。
        API 异常时返回 None，由 search 降级为原始排序（保证检索可用性）
        """
        documents = [p.payload.get("text", "") for p in points]
        try:
            ranked = self.rerank_client.rerank(query, documents, top_n=top_k)
        except Exception as e:
            logger.warning(f"[Rerank] 精排失败，降级为原始排序: {e}")
            return None
        return [(points[item["index"]], item["relevance_score"]) for item in ranked]

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

    # 测试按 metadata 删除
    deleted = vs.delete_by_metadata({"source": "test.txt"})
    print(f"删除 test.txt 相关向量: {deleted} 个")
    print(f"删除后向量数: {vs.count()}")

    # 测试检索
    results = vs.search("路由器质保多久")
    for r in results:
        print(f"\n[score={r['score']:.4f}] {r['text'][:100]}")
        print(f"  metadata: {r['metadata']}")
