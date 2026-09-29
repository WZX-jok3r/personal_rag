"""
Application Configuration

统一管理:

- LLM
- Qdrant
- Retrieval
- Reranker
- Chat


"""

from functools import lru_cache

from pydantic import BaseModel, Field

from pydantic_settings import (
    BaseSettings,
    SettingsConfigDict
)


class LLMConfig(BaseModel):
    provider: str = "siliconflow"

    model: str = (
        "deepseek-ai/DeepSeek-V3"
    )

    api_key: str | None = None

    base_url: str | None = None

    temperature: float = 0.2


class QdrantConfig(BaseModel):
    host: str = "localhost"

    port: int = 6333

    collection: str = (
        "knowledge"
    )


class RetrievalConfig(BaseModel):
    top_k: int = 10

    score_threshold: float = 0.3


class RerankerConfig(BaseModel):
    """
    Reranker配置


    默认关闭


    """

    enabled: bool = False

    provider: str = "none"

    model: str | None = None

    api_key: str | None = None

    top_k: int = 5


class ChatConfig(BaseModel):
    max_history: int = 20

    session_ttl: int = 3600


class Settings(BaseSettings):
    """
    全局配置


    """

    app_name: str = (
        "Enterprise-RAG"
    )

    debug: bool = False

    llm: LLMConfig = (
        LLMConfig()
    )

    qdrant: QdrantConfig = (
        QdrantConfig()
    )

    retrieval: RetrievalConfig = (
        RetrievalConfig()
    )

    reranker: RerankerConfig = (
        RerankerConfig()
    )

    chat: ChatConfig = (
        ChatConfig()
    )

    model_config = SettingsConfigDict(

        env_file=".env",

        env_file_encoding="utf-8",

        extra="ignore",

        case_sensitive=False

    )


@lru_cache
def get_settings():
    return Settings()


settings = get_settings()
