from .pdf import PDFChunkStrategy
from .markdown import MarkdownChunkStrategy
from .txt import TXTChunkStrategy
from .docx import DOCXChunkStrategy
from .xlsx import XLSXChunkStrategy


class ChunkStrategyFactory:
    strategies = {

        "pdf":
            PDFChunkStrategy(),

        "md":
            MarkdownChunkStrategy(),

        "markdown":
            MarkdownChunkStrategy(),

        "txt":
            TXTChunkStrategy(),

        "docx":
            DOCXChunkStrategy(),

        "xlsx":
            XLSXChunkStrategy(),

    }

    @classmethod
    def create(
            cls,
            file_type: str
    ):
        strategy = (
            cls.strategies.get(
                file_type.lower()
            )
        )

        if not strategy:
            raise ValueError(
                f"unsupported file type:{file_type}"
            )

        return strategy
