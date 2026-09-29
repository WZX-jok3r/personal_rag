# Enterprise RAG backend image
FROM python:3.12-slim AS base

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# 先装依赖（利用层缓存），再拷贝源码
COPY pyproject.toml ./
COPY app ./app
RUN pip install --no-cache-dir .

# 非 root 运行
RUN adduser --disabled-password --no-create-home appuser
USER appuser

EXPOSE 8000

# 容器内健康检查命中存活探针
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/api/v1/health').status==200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
