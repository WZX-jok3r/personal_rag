"""
Document Loader


负责:

不同格式文件解析

支持:

PDF

TXT

Markdown

DOCX

输出统一Document格式

"""

import logging

from pathlib import Path

from typing import List, Dict, Any

logger = logging.getLogger(__name__)


class BaseLoader:

    def load(
            self,
            file_path: str
    ) -> List[Dict[str, Any]]:
        raise NotImplementedError


class TextLoader(
    BaseLoader
):
    """
    TXT / MD文件解析
    """

    def load(
            self,
            file_path: str
    ):
        path = Path(
            file_path
        )

        content = (
            path.read_text(
                encoding="utf-8"
            )
        )

        return [

            {

                "content":
                    content,

                "metadata":
                    {

                        "source":
                            path.name

                    }

            }

        ]


class PDFLoader(
    BaseLoader
):
    """
    PDF解析


    使用:

    pypdf


    """

    def load(
            self,
            file_path: str
    ):

        try:

            from pypdf import (
                PdfReader
            )


        except ImportError:

            raise ImportError(
                "please install pypdf"
            )

        reader = PdfReader(
            file_path
        )

        documents = []

        for index, page in enumerate(
                reader.pages
        ):

            text = (
                    page.extract_text()
                    or ""
            )

            if not text.strip():
                continue

            documents.append(

                {

                    "content":
                        text,

                    "metadata":
                        {

                            "page":
                                index + 1

                        }

                }

            )

        return documents


class DocxLoader(
    BaseLoader
):
    """
    Word文档解析
    """

    def load(
            self,
            file_path: str
    ):

        try:

            from docx import (
                Document
            )


        except ImportError:

            raise ImportError(
                "please install python-docx"
            )

        doc = Document(
            file_path
        )

        texts = []

        for para in doc.paragraphs:

            if para.text.strip():
                texts.append(
                    para.text
                )

        return [

            {

                "content":
                    "\n".join(
                        texts
                    ),

                "metadata":
                    {}

            }

        ]


class LoaderFactory:

    @staticmethod
    def create(
            file_path: str
    ) -> BaseLoader:

        suffix = (
            Path(file_path)
            .suffix
            .lower()
        )

        if suffix == ".pdf":
            return PDFLoader()

        if suffix in [
            ".txt",
            ".md"
        ]:
            return TextLoader()

        if suffix in [
            ".docx"
        ]:
            return DocxLoader()

        raise ValueError(

            f"unsupported file type:{suffix}"

        )
