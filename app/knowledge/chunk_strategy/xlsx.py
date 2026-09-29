from .base import BaseChunkStrategy


class XLSXChunkStrategy(
    BaseChunkStrategy
):
    """
    Excel语义切分


    后续保留:

    row -> sentence

    table metadata

    """

    def split(
            self,
            documents
    ):
        chunks = []

        for doc in documents:
            chunks.append(

                {

                    "content":
                        doc["content"],

                    "metadata":
                        doc.get(
                            "metadata",
                            {}
                        )

                }

            )

        return chunks
