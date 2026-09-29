from fastapi import FastAPI

from app.api import knowledge
from app.api import chat


app = FastAPI(
    title="Enterprise RAG System",
    version="1.0.0"
)


@app.get("/")
def health():

    return {

        "status": "ok",

        "service":
        "rag"

    }



app.include_router(
    knowledge.router,
    prefix="/knowledge",
    tags=["knowledge"]
)


app.include_router(
    chat.router,
    prefix="/chat",
    tags=["chat"]
)