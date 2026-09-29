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
  hidden_count?: number;
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
  hidden_count?: number;
}

/** POST /api/sessions 响应 */
export interface SessionCreated {
  session_id: string;
}

/** POST /api/chat/stream 的 SSE 事件（逐条 data: <json>） */
export type StreamEvent =
  | { type: "meta"; sources: SourceItem[]; retrieved_count: number; hidden_count: number }
  | { type: "delta"; text: string }
  | { type: "done"; answer: string; session_id?: string; history_length?: number }
  | { type: "error"; message: string };

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

/** POST /api/documents 上传响应（202 已接收、后台入库中） */
export interface UploadResponse {
  task_id: string;
  filename: string;
  status: string;
  message?: string;
}

/** GET /api/documents/{task_id}/status 入库任务状态 */
export interface IngestTaskStatus {
  task_id: string;
  document_id: number | null;
  status: string; // queued | running | done | failed
  progress: number; // 0~100
  error?: string | null;
}

/** 单条已登记文档 */
export interface DocumentItem {
  id: number;
  source: string;
  format: string;
  status: string;
  chunk_count: number;
}

/** GET /api/documents 列表响应 */
export interface DocumentListResponse {
  documents: DocumentItem[];
  total: number;
}

/** DELETE /api/documents/{id} 删除响应 */
export interface DeleteDocumentResponse {
  deleted_id: number;
  source: string;
  vectors_cleared: boolean;
  file_removed: boolean;
  message?: string;
}
