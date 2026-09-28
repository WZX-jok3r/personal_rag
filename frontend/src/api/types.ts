// 与后端 FastAPI (src/api.py) 的 Pydantic 模型一一对应的 TS 类型定义

/** 单条参考资料 */
export interface SourceItem {
  source: string;
  format: string;
  score: number;
  text_preview?: string | null;
}

/** POST /api/query 请求体 */
export interface QueryRequest {
  query: string;
  top_k?: number;
  filter_dict?: Record<string, unknown> | null;
}

/** POST /api/query 响应 */
export interface QueryResponse {
  query: string;
  answer: string;
  sources: SourceItem[];
  retrieved_count: number;
}

/** POST /api/chat 请求体 */
export interface ChatRequest {
  message: string;
  session_id?: string | null;
  top_k?: number;
}

/** POST /api/chat 响应 */
export interface ChatResponse {
  session_id: string;
  answer: string;
  sources: SourceItem[];
  retrieved_count: number;
  history_length: number;
}

/** POST /api/sessions 响应 */
export interface SessionCreated {
  session_id: string;
}

/** GET /api/health 响应 */
export interface HealthInfo {
  status: string;
  embedding_model: string;
  llm_model: string;
  collection: string;
  retrieval_mode: string;
  rerank_enabled: boolean;
  rerank_model: string | null;
  rerank_candidates: number | null;
  default_top_k: number;
}
