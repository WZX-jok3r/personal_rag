"""
应用工厂 + ASGI 入口

启动:
    cd backend
    uvicorn app.main:app --reload --port 8000
接口文档: http://localhost:8000/docs
健康检查: http://localhost:8000/api/v1/health

采用 create_app() 工厂模式：每个测试可获得全新应用实例，便于依赖覆盖。
lifespan 中集中管理外部资源的启动/关闭（P3 起接入 PostgreSQL / Redis / Qdrant）。
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.api.router import api_router
from app.cache.redis import redis_client
from app.core.config import settings
from app.core.exceptions import register_exception_handlers
from app.core.logging import setup_logging
from app.database import db

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(
        "Starting %s v%s (env=%s, auth=%s)",
        settings.app_name, __version__, settings.environment, settings.auth_enabled,
    )
    # 初始化外部资源连接池（DB / Redis 为强依赖，Qdrant 懒加载）
    db.init()
    redis_client.init()
    try:
        await db.healthcheck()
        logger.info("[DB] PostgreSQL 连接正常")
    except Exception as e:  # 探活失败不阻断启动，仅告警（便于无 DB 的环境先起服务）
        logger.warning("[DB] PostgreSQL 探活失败（表未迁移或服务未就绪？）: %s", e)
    try:
        await redis_client.healthcheck()
        logger.info("[Redis] 连接正常")
    except Exception as e:
        logger.warning("[Redis] 探活失败: %s", e)
    yield
    await db.dispose()
    await redis_client.dispose()
    logger.info("Shutdown complete: %s", settings.app_name)


def create_app() -> FastAPI:
    setup_logging(settings.log_level)

    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(api_router, prefix="/api")
    register_exception_handlers(app)
    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host=settings.host, port=settings.port, reload=settings.debug)
