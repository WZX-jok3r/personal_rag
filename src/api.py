"""
api.py - FastAPI 后端服务（前后端分离的"后端"）

设计要点（结合本项目现状）:
1. 复用现有 RAGPipeline，不改其核心逻辑；仅把来源格式化、多轮历史等业务逻辑
   下沉到后端，前端只负责渲染。
2. pipeline 用单例（lifespan 启动时初始化一次），避免每请求重建 Qdrant 连接。
3. 接口用同步 def：RAGPipeline 内部是阻塞的 requests 调用，交给 FastAPI 线程池
   执行即可，无需把整条链路改成 async。
4. 多轮对话采用「后端 session 存储」：客户端持 session_id，历史保存在服务端
   （内存 + 锁 + TTL 清理）。如需持久化可把 SessionStore 换成 Redis 实现。

启动:
    python src/api.py                 # 默认 http://localhost:8000
    或
    uvicorn api:app --reload --port 8000   # 在 src/ 目录下

浏览器打开 http://localhost:8000/ 有一个测试多轮 session 的演示页；
接口文档见 http://localhost:8000/docs
"""

import time
import uuid
import logging
import threading
from contextlib import asynccontextmanager
from typing import List, Dict, Any, Optional

from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from config import (
    TOP_K,
    RETRIEVAL_MODE,
    RERANK_ENABLED,
    RERANK_MODEL,
    RERANK_CANDIDATES,
    LLM_MODEL,
    EMBEDDING_MODEL,
    QDRANT_COLLECTION_NAME,
    TENANT_FIELD,
    AUTH_ENABLED,
)
from rag_pipeline import get_pipeline, RAGPipeline
from auth import Principal, require_principal

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# ==================== session 存储策略可调参数 ====================
# 单会话最多保留的历史消息条数（user+assistant 各算 1 条），超出丢弃最早的，防止 prompt 无限膨胀
MAX_HISTORY_MESSAGES = 20
# session 空闲多久后回收（秒）。内存存储必须有 TTL，否则长期运行会泄漏
SESSION_TTL_SECONDS = 3600


# ==================== pipeline 单例 ====================
_pipeline: Optional[RAGPipeline] = None


def get_global_pipeline() -> RAGPipeline:
    """获取全局 pipeline 实例（懒加载单例，避免每请求重建 Qdrant 连接）"""
    global _pipeline
    if _pipeline is None:
        logger.info("[API] 初始化 RAG Pipeline...")
        _pipeline = get_pipeline()
    return _pipeline


# ==================== 多轮对话的 session 存储 ====================
class SessionStore:
    """
    线程安全的内存会话存储。

    - FastAPI 同步接口运行在线程池中，存在并发访问，故用锁保护。
    - 每个 session 保存干净的对话历史（仅 role/content，不含参考资料），
      与 pipeline.query_with_history 的入参格式一致。
    - TTL 惰性清理：每次写入时顺手回收过期条目，避免长期运行内存增长。
    - 若要换成 Redis：保持 create/append/get/delete 四个方法签名即可无缝替换。
    """

    def __init__(self):
        self._lock = threading.Lock()
        # session_id -> {"history": [...], "last_access": ts}
        self._sessions: Dict[str, Dict[str, Any]] = {}

    def _sweep_locked(self):
        """清理过期 session（需在持锁状态下调用）"""
        now = time.time()
        expired = [sid for sid, s in self._sessions.items()
                   if now - s["last_access"] > SESSION_TTL_SECONDS]
        for sid in expired:
            self._sessions.pop(sid, None)
        if expired:
            logger.info(f"[API] 回收过期 session {len(expired)} 个")

    def create(self) -> str:
        session_id = uuid.uuid4().hex
        with self._lock:
            self._sweep_locked()
            self._sessions[session_id] = {"history": [], "last_access": time.time()}
        logger.info(f"[API] 新建 session: {session_id}")
        return session_id

    def exists(self, session_id: str) -> bool:
        with self._lock:
            return session_id in self._sessions

    def get_history(self, session_id: str) -> List[Dict[str, str]]:
        """返回历史副本，避免调用方持有内部引用"""
        with self._lock:
            sess = self._sessions.get(session_id)
            if not sess:
                return []
            sess["last_access"] = time.time()
            return [dict(m) for m in sess["history"]]

    def append(self, session_id: str, message: Dict[str, str]):
        with self._lock:
            sess = self._sessions.get(session_id)
            if not sess:
                return
            sess["history"].append(dict(message))
            # 截断：只保留最近 MAX_HISTORY_MESSAGES 条
            if len(sess["history"]) > MAX_HISTORY_MESSAGES:
                sess["history"] = sess["history"][-MAX_HISTORY_MESSAGES:]
            sess["last_access"] = time.time()

    def delete(self, session_id: str) -> bool:
        with self._lock:
            return self._sessions.pop(session_id, None) is not None


_sessions = SessionStore()


# ==================== Pydantic 请求/响应模型 ====================
class QueryRequest(BaseModel):
    query: str = Field(..., min_length=1, description="用户问题")
    top_k: Optional[int] = Field(None, ge=1, le=20, description="召回数量，默认取配置 TOP_K")
    filter_dict: Optional[Dict[str, Any]] = Field(None, description="可选的元数据过滤条件")


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, description="本轮用户消息")
    session_id: Optional[str] = Field(None, description="会话 ID；留空则自动新建会话")
    top_k: Optional[int] = Field(None, ge=1, le=20, description="召回数量，默认取配置 TOP_K")


class SourceItem(BaseModel):
    source: str
    format: str
    score: float
    text_preview: Optional[str] = None


class QueryResponse(BaseModel):
    query: str
    answer: str
    sources: List[SourceItem]
    retrieved_count: int
    hidden_count: int = Field(0, description="本次被低相关过滤掉、未展示的条数")


class ChatResponse(BaseModel):
    session_id: str
    answer: str
    sources: List[SourceItem]
    retrieved_count: int
    history_length: int = Field(..., description="该会话当前保存的历史消息条数")
    hidden_count: int = Field(0, description="本次被低相关过滤掉、未展示的条数")


class SessionCreated(BaseModel):
    session_id: str


# ==================== 应用与路由 ====================
@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时预热 pipeline（建 Qdrant 连接、校验 API Key），首个请求不再承担初始化耗时
    try:
        get_global_pipeline()
    except Exception as e:
        logger.error(f"[API] pipeline 初始化失败: {e}")
        raise
    yield


app = FastAPI(
    title="Personal RAG API",
    description="知识库问答后端（FastAPI），供任意前端调用",
    version="1.0.0",
    lifespan=lifespan,
)

# 允许跨域，方便独立部署的 Vue/React/演示页调用
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],          # 生产环境应收敛为具体前端域名
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _resolve_top_k(top_k: Optional[int]) -> int:
    return int(top_k) if top_k else TOP_K


def _enforced_filter(client_filter: Optional[Dict[str, Any]], principal: Principal) -> Optional[Dict[str, Any]]:
    """
    在服务端强制叠加租户隔离条件：
    - 客户端传入的 filter 可含普通业务过滤（如 format），但绝不能覆盖 tenant 键；
    - tenant_id 由已认证的 Principal 决定，这里以服务端值为准（后写覆盖）；
    - 鉴权关闭时 principal.tenant_id 为 None，不注入租户条件（保持旧行为）。
    """
    merged = dict(client_filter or {})
    merged.pop(TENANT_FIELD, None)          # 丢弃客户端可能伪造的租户键
    if principal.tenant_id is not None:
        merged[TENANT_FIELD] = principal.tenant_id  # 服务端强制注入
    return merged or None


@app.get("/api/health", summary="健康检查与配置回显")
def health():
    return {
        "status": "ok",
        "embedding_model": EMBEDDING_MODEL,
        "llm_model": LLM_MODEL,
        "collection": QDRANT_COLLECTION_NAME,
        "retrieval_mode": RETRIEVAL_MODE,
        "rerank_enabled": RERANK_ENABLED,
        "rerank_model": RERANK_MODEL if RERANK_ENABLED else None,
        "rerank_candidates": RERANK_CANDIDATES if RERANK_ENABLED else None,
        "default_top_k": TOP_K,
        "auth_enabled": AUTH_ENABLED,
        "tenant_field": TENANT_FIELD,
    }


@app.post("/api/query", response_model=QueryResponse, summary="单轮问答")
def single_query(req: QueryRequest, principal: Principal = Depends(require_principal)):
    """无状态单轮问答：检索 -> 组装 context -> LLM 生成 -> 返回答案与来源。
    鉴权开启时，服务端按当前租户强制过滤检索范围。
    """
    try:
        result = get_global_pipeline().query(
            req.query,
            top_k=_resolve_top_k(req.top_k),
            filter_dict=_enforced_filter(req.filter_dict, principal),
        )
    except Exception as e:
        logger.error(f"[API] 单轮查询出错: {e}")
        raise HTTPException(status_code=500, detail=f"处理出错: {e}")

    return QueryResponse(**result)


@app.post("/api/chat", response_model=ChatResponse, summary="多轮对话（后端 session）")
def chat_query(req: ChatRequest, principal: Principal = Depends(require_principal)):
    """
    多轮对话。历史保存在服务端：
    - 带有效 session_id -> 追加到该会话继续问答；
    - 不带或 session_id 无效 -> 自动新建会话并返回其 session_id。
    前端只需回传返回的 session_id 即可维持上下文。
    鉴权开启时，服务端按当前租户强制过滤检索范围。
    """
    if req.session_id and _sessions.exists(req.session_id):
        session_id = req.session_id
    else:
        session_id = _sessions.create()

    history = _sessions.get_history(session_id)

    try:
        result = get_global_pipeline().query_with_history(
            req.message, history, top_k=_resolve_top_k(req.top_k),
            filter_dict=_enforced_filter(None, principal),
        )
    except Exception as e:
        logger.error(f"[API] 对话查询出错: {e}")
        raise HTTPException(status_code=500, detail=f"处理出错: {e}")

    # 更新服务端历史（仅存干净的 user/assistant，不含参考资料）
    _sessions.append(session_id, {"role": "user", "content": req.message})
    _sessions.append(session_id, {"role": "assistant", "content": result["answer"]})

    return ChatResponse(
        session_id=session_id,
        answer=result["answer"],
        sources=result["sources"],
        retrieved_count=result["retrieved_count"],
        history_length=len(_sessions.get_history(session_id)),
        hidden_count=result.get("hidden_count", 0),
    )


@app.post("/api/sessions", response_model=SessionCreated, summary="新建空白会话")
def create_session():
    return SessionCreated(session_id=_sessions.create())


@app.delete("/api/sessions/{session_id}", summary="清空/删除会话")
def delete_session(session_id: str):
    removed = _sessions.delete(session_id)
    if not removed:
        raise HTTPException(status_code=404, detail="session 不存在")
    return {"deleted": session_id}


# ==================== 浏览器演示页（测试多轮 session）====================
_DEMO_HTML = """<!doctype html>
<html lang="zh">
<head><meta charset="utf-8"><title>Personal RAG API 演示</title>
<style>
 body{font-family:system-ui;max-width:760px;margin:32px auto;padding:0 16px}
 .row{margin:8px 0} #log{border:1px solid #ddd;padding:12px;height:360px;overflow:auto;border-radius:8px}
 button,input[type=text]{padding:8px 12px;border-radius:8px;border:1px solid #ccc}
 input[type=text]{width:70%} .who{font-weight:600} .src{color:#666;font-size:12px;margin-left:8px}
 .badge{display:inline-block;background:#fef3c7;color:#92400e;border:1px solid #fde68a;border-radius:9999px;padding:1px 8px;font-size:11px;margin-left:6px}
</style></head>
<body>
<h2>Personal RAG · 多轮对话 Session 演示</h2>
<p>后端 session 存储，前端只保存返回的 session_id。<code>session_id: <span id="sid">(新建)</span></code></p>
<div class="row"><input id="key" type="text" placeholder="API Key（开启鉴权后必填，存入 localStorage）" style="width:70%"></div>
<div id="log"></div>
<div class="row">
  <input id="msg" type="text" placeholder="输入问题，回车发送" onkeydown="if(event.key==='Enter')send()">
  <button onclick="send()">发送</button>
  <button onclick="newSession()">新会话</button>
</div>
<script>
let sid = null;
const keyEl = document.getElementById('key');
keyEl.value = localStorage.getItem('rag_api_key') || '';
keyEl.addEventListener('input', () => localStorage.setItem('rag_api_key', keyEl.value.trim()));
function authHeaders(extra){ const k=(keyEl.value||'').trim(); const h=Object.assign({}, extra); if(k) h['X-API-Key']=k; return h; }
function append(who, text, src){
  const d=document.createElement('div'); d.className='row';
  d.innerHTML='<span class="who">'+who+':</span> '+text+(src?'<div class="src">'+src+'</div>':'');
  const log=document.getElementById('log'); log.appendChild(d); log.scrollTop=log.scrollHeight;
}
async function send(){
  const el=document.getElementById('msg'); const m=el.value.trim(); if(!m) return;
  el.value=''; append('我', m);
  const r=await fetch('/api/chat',{method:'POST',headers:authHeaders({'Content-Type':'application/json'}),
    body:JSON.stringify({message:m, session_id:sid, top_k:5})});
  if(!r.ok){ const e=await r.json().catch(()=>({})); append('AI','⚠️ '+(e.detail||r.status)); return; }
  const j=await r.json(); sid=j.session_id; document.getElementById('sid').textContent=sid;
  const srcs=(j.sources||[]).map(s=>`[${s.source}] ${s.score}`).join(' · ');
  let srcHtml='参考资料: '+(srcs||'无');
  if(j.hidden_count>0){ srcHtml+=` <span class="badge">🙈 已隐藏 ${j.hidden_count} 条低相关</span>`; }
  append('AI', j.answer, srcHtml);
}
async function newSession(){
  if(sid) await fetch('/api/sessions/'+sid,{method:'DELETE', headers:authHeaders({})});
  sid=null; document.getElementById('sid').textContent='(新建)'; append('系统','已开启新会话');
}
</script>
</body></html>"""


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def demo():
    return _DEMO_HTML


def main():
    import uvicorn
    logger.info("[API] 启动 FastAPI 服务... http://localhost:8000")
    uvicorn.run(app, host="0.0.0.0", port=8000)


if __name__ == "__main__":
    main()
