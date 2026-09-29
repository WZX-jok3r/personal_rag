// 统一的 API 客户端：封装对后端 FastAPI 的 fetch 调用与错误处理
// 开发期通过 Vite 代理，使用相对路径 /api/*（同源，免 CORS）

import type {
  ChatRequest,
  ChatResponse,
  HealthInfo,
  QueryRequest,
  QueryResponse,
  SessionCreated,
  StreamEvent,
} from "./types";

const BASE = "/api";

// API Key：优先取构建时环境变量 VITE_RAG_API_KEY，其次 localStorage（便于运行时手动填入）。
// 后端开启鉴权（配置了 RAG_TENANT_KEYS）时必须携带，否则 401。
function getApiKey(): string {
  const envKey = (import.meta as any).env?.VITE_RAG_API_KEY as string | undefined;
  if (envKey) return envKey;
  try {
    return localStorage.getItem("rag_api_key") || "";
  } catch {
    return "";
  }
}

/** 通用请求：非 2xx 统一抛出带后端 detail 的错误 */
async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...((options?.headers as Record<string, string>) || {}),
  };
  const key = getApiKey();
  if (key) headers["X-API-Key"] = key;

  const resp = await fetch(`${BASE}${path}`, {
    ...options,
    headers,
  });

  if (!resp.ok) {
    let detail = `${resp.status} ${resp.statusText}`;
    try {
      const err = await resp.json();
      if (err && err.detail) detail = String(err.detail);
    } catch {
      // 忽略解析失败，回退到状态行
    }
    throw new Error(detail);
  }

  return (await resp.json()) as T;
}

/** 健康检查与配置回显 */
export function getHealth(): Promise<HealthInfo> {
  return request<HealthInfo>("/health");
}

/** 单轮问答 */
export function query(payload: QueryRequest): Promise<QueryResponse> {
  return request<QueryResponse>("/query", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

/** 多轮对话（后端 session 存储） */
export function chat(payload: ChatRequest): Promise<ChatResponse> {
  return request<ChatResponse>("/chat", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

/**
 * 多轮对话的流式版本（SSE）：逐条回调 StreamEvent，支持 AbortController 外部中断。
 * 用 fetch + ReadableStream 而非 EventSource，因为需要 POST 且能带 X-API-Key 请求头。
 */
export async function chatStream(
  payload: ChatRequest,
  onEvent: (ev: StreamEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    Accept: "text/event-stream",
  };
  const key = getApiKey();
  if (key) headers["X-API-Key"] = key;

  const resp = await fetch(`${BASE}/chat/stream`, {
    method: "POST",
    headers,
    body: JSON.stringify(payload),
    signal,
  });

  if (!resp.ok || !resp.body) {
    let detail = `${resp.status} ${resp.statusText}`;
    try {
      const err = await resp.json();
      if (err && err.detail) detail = String(err.detail);
    } catch {
      // 忽略解析失败，回退到状态行
    }
    throw new Error(detail);
  }

  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    // 按 SSE 帧分隔符 \n\n 切分，残留不足一帧的留在 buf 中
    let sep: number;
    while ((sep = buf.indexOf("\n\n")) !== -1) {
      const frame = buf.slice(0, sep);
      buf = buf.slice(sep + 2);
      const line = frame.split("\n").find((l) => l.startsWith("data:"));
      if (!line) continue;
      const jsonStr = line.slice("data:".length).trim();
      if (!jsonStr) continue;
      try {
        onEvent(JSON.parse(jsonStr) as StreamEvent);
      } catch {
        // 忽略单帧解析异常，不中断整条流
      }
    }
  }
}

/** 新建空白会话 */
export function createSession(): Promise<SessionCreated> {
  return request<SessionCreated>("/sessions", { method: "POST" });
}

/** 删除/清空会话 */
export function deleteSession(sessionId: string): Promise<void> {
  return request<void>(`/sessions/${encodeURIComponent(sessionId)}`, {
    method: "DELETE",
  });
}
