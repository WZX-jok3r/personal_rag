from .base import BaseChunkStrategy


class TXTChunkStrategy(
    BaseChunkStrategy
):

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
