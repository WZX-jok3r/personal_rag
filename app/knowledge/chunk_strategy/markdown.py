from typing import List, Dict, Any

from .base import BaseChunkStrategy


class MarkdownChunkStrategy(
    BaseChunkStrategy
):
    """
    Markdown切分


    保留:

    - heading
    - table
    - code block

    """

    def split(
            self,
            documents
    ):
        chunks = []

        for doc in documents:
            content = doc.get(
                "content",
                ""
            )

            metadata = doc.get(
                "metadata",
                {}
            )

            chunks.append(

                {

                    "content":
                        content,

                    "metadata":
                        metadata

                }

            )

        return chunks
