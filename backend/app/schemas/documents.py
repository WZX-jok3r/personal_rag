"""文档入库相关响应模型。"""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field


class UploadResponse(BaseModel):
    task_id: str
    filename: str
    status: str = Field("queued", description="任务初始状态")
    message: str = "文件已接收，正在后台入库"


class TaskStatusResponse(BaseModel):
    task_id: str
    document_id: Optional[int] = None
    status: str
    progress: int
    error: Optional[str] = None


class DocumentItem(BaseModel):
    id: int
    source: str
    format: str
    status: str
    chunk_count: int


class DocumentListResponse(BaseModel):
    documents: List[DocumentItem]
    total: int
