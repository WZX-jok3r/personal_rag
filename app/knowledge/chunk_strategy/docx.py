from .base import BaseChunkStrategy


class DOCXChunkStrategy(
    BaseChunkStrategy
):

    def split(
            self,
            documents
    ):
        return [

            {

                "content":
                    doc["content"],

                "metadata":
                    doc.get(
                        "metadata",
                        {}
                    )

            }

            for doc in documents

        ]
