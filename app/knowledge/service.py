"""
Knowledge Service


职责:

管理知识库文档生命周期。


包括:

- 上传文档
- 文档解析
- 文档切分
- 向量化
- 写入向量库


不负责:

- HTTP接口
- 前端交互


"""

import os
import uuid
import logging

from pathlib import Path

from typing import Dict, Any

from app.knowledge.loader import (
    LoaderFactory
)

logger = logging.getLogger(__name__)


class KnowledgeService:

    def __init__(
            self,
            splitter=None,
            vector_store=None,
            embedding=None
    ):

        """
        依赖注入


        后续替换:

        loader:
            PDFLoader


        splitter:
            RecursiveSplitter


        vector:
            Qdrant


        embedding:
            BGE/OpenAI


        """
        self.splitter = splitter

        self.vector_store = vector_store

        self.embedding = embedding

    def ingest_file(
            self,
            file_path: str,
            metadata: Dict[str, Any] | None = None
    ):

        """
        导入单个文件


        流程:

        file

        ↓

        loader

        ↓

        chunks

        ↓

        embedding

        ↓

        vector db


        """

        logger.info(
            f"[KNOWLEDGE] ingest {file_path}"
        )

        if not os.path.exists(
                file_path
        ):
            raise FileNotFoundError(
                file_path
            )

        #
        # 1.
        # 文档解析
        #

        loader = (
            LoaderFactory.create(
                file_path
            )
        )

        documents = (
            loader.load(
                file_path
            )
        )

        #
        # 2.
        # 文档切分
        #

        chunks = (
            self.splitter.split(
                documents
            )
        )

        #
        # 3.
        # 增加metadata
        #

        doc_id = str(
            uuid.uuid4()
        )

        for index, chunk in enumerate(chunks):
            chunk["metadata"] = {

                **(
                        metadata
                        or {}
                ),

                "doc_id":
                    doc_id,

                "chunk_id":
                    index,

                "source":
                    Path(file_path).name

            }

        #
        # 4.
        # 写入向量库
        #

        self.vector_store.add(
            chunks
        )

        return {

            "doc_id":
                doc_id,

            "filename":
                Path(file_path).name,

            "chunks":
                len(chunks),

            "status":
                "success"

        }

    def delete_document(
            self,
            doc_id: str
    ):

        """
        删除知识库文档


        后续:

        Qdrant filter:

        metadata.doc_id


        """

        if not self.vector_store:
            raise RuntimeError(
                "vector store not configured"
            )

        return (
            self.vector_store.delete(
                {
                    "doc_id":
                        doc_id
                }
            )
        )

    def list_documents(self):

        """
        获取文档列表


        后续接:

        PostgreSQL documents表


        """

        if hasattr(
                self.vector_store,
                "list_documents"
        ):
            return (
                self.vector_store
                .list_documents()
            )

        return []
