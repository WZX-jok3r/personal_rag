// 对话状态管理（Pinia）：维护 session_id、渲染用消息列表、加载/错误状态
// 后端是历史的权威存储，前端 messages 仅用于渲染

import { defineStore } from "pinia";
import * as api from "../api/client";
import type { SourceItem } from "../api/types";

export type ChatRole = "user" | "assistant";

export interface ChatMessage {
  role: ChatRole;
  content: string;
  sources?: SourceItem[];
  hiddenCount?: number;
  streaming?: boolean;
}

const SESSION_KEY = "rag_session_id";
// 会话归属租户的指纹（与 session 一同存储）：Key 变更后据此丢弃旧会话，避免跨租户串会话
const SESSION_TENANT_KEY = "rag_session_tenant";

/** 当前 API Key 的轻量非可逆指纹（djb2）；空 Key 返回空串 */
function keyFingerprint(): string {
  const k = api.getApiKey();
  if (!k) return "";
  let h = 5381;
  for (let i = 0; i < k.length; i++) h = ((h << 5) + h + k.charCodeAt(i)) >>> 0;
  return h.toString(16);
}

// 当前进行中的流式请求控制器（不入 state，避免被响应式代理包裹）
let currentAbort: AbortController | null = null;

export const useChatStore = defineStore("chat", {
  state: () => ({
    sessionId: null as string | null,
    messages: [] as ChatMessage[],
    status: "idle" as "idle" | "loading" | "error",
    error: "" as string,
    topK: 5,
    initialized: false,
  }),

  getters: {
    isLoading: (s) => s.status === "loading",
  },

  actions: {
    /** 进入页面：仅当本地 session 与当前租户指纹一致才复用，否则新建（防跨租户串会话） */
    async ensureSession() {
      if (this.initialized) return;
      const saved = localStorage.getItem(SESSION_KEY);
      const savedTenant = localStorage.getItem(SESSION_TENANT_KEY);
      if (saved && savedTenant === keyFingerprint()) {
        this.sessionId = saved;
      } else {
        // 无会话或已切换租户：清掉残留旧 id，避免沿用别的租户的会话
        localStorage.removeItem(SESSION_KEY);
        localStorage.removeItem(SESSION_TENANT_KEY);
        await this.createNewSession();
      }
      this.initialized = true;
    },

    /** 新建后端会话并写入 localStorage（连同当前租户指纹） */
    async createNewSession() {
      try {
        const res = await api.createSession();
        this.sessionId = res.session_id;
        localStorage.setItem(SESSION_KEY, res.session_id);
        localStorage.setItem(SESSION_TENANT_KEY, keyFingerprint());
        this.messages = [];
        this.error = "";
        this.status = "idle";
      } catch (e) {
        // 新建失败（如 Key 无效 401）：清掉可能残留的旧会话，修正 Key 后可干净重建
        localStorage.removeItem(SESSION_KEY);
        localStorage.removeItem(SESSION_TENANT_KEY);
        this.sessionId = null;
        this.status = "error";
        this.error = `新建会话失败: ${(e as Error).message}`;
      }
    },

    /** 发送一条消息，走多轮 /api/chat */
    async sendMessage(text: string) {
      const message = text.trim();
      if (!message || this.isLoading) return;

      await this.ensureSession();
      this.status = "loading";
      this.error = "";
      // 先本地追加用户消息，给出即时反馈
      this.messages.push({ role: "user", content: message });

      try {
        const res = await api.chat({
          message,
          session_id: this.sessionId,
          top_k: this.topK,
        });
        // 后端可能自动新建 session，回传最新的写回本地
        if (res.session_id && res.session_id !== this.sessionId) {
          this.sessionId = res.session_id;
          localStorage.setItem(SESSION_KEY, res.session_id);
        }
        this.messages.push({
          role: "assistant",
          content: res.answer,
          sources: res.sources,
          hiddenCount: res.hidden_count ?? 0,
        });
        this.status = "idle";
      } catch (e) {
        this.status = "error";
        this.error = `请求失败: ${(e as Error).message}`;
        // 撤回未成功的用户消息，保持与后端历史一致
        this.messages.pop();
      }
    },

    /** 发送一条消息（流式 SSE）：逐段增量渲染 assistant 消息 */
    async sendMessageStream(text: string) {
      const message = text.trim();
      if (!message || this.isLoading) return;

      await this.ensureSession();
      this.status = "loading";
      this.error = "";
      this.messages.push({ role: "user", content: message });
      // 先放一条空的 assistant 占位消息，后续就地累加
      this.messages.push({
        role: "assistant",
        content: "",
        sources: [],
        hiddenCount: 0,
        streaming: true,
      });
      const idx = this.messages.length - 1;

      currentAbort?.abort();
      const controller = new AbortController();
      currentAbort = controller;
      let hadError = false;

      try {
        await api.chatStream(
          { message, session_id: this.sessionId, top_k: this.topK },
          (ev) => {
            const msg = this.messages[idx];
            if (ev.type === "meta") {
              msg.sources = ev.sources;
              msg.hiddenCount = ev.hidden_count;
            } else if (ev.type === "delta") {
              msg.content += ev.text;
            } else if (ev.type === "done") {
              if (ev.answer) msg.content = ev.answer;
              msg.streaming = false;
              if (ev.session_id && ev.session_id !== this.sessionId) {
                this.sessionId = ev.session_id;
                localStorage.setItem(SESSION_KEY, ev.session_id);
              }
            } else if (ev.type === "error") {
              msg.streaming = false;
              hadError = true;
              this.status = "error";
              this.error = ev.message;
            }
          },
          controller.signal,
        );
        if (!hadError) this.status = "idle";
        const m = this.messages[idx];
        if (m) m.streaming = false;
      } catch (e) {
        const m = this.messages[idx];
        if (m) m.streaming = false;
        if ((e as Error).name === "AbortError") {
          // 用户主动中断：保留已流出内容，不算错误
          this.status = "idle";
        } else {
          this.status = "error";
          this.error = `请求失败: ${(e as Error).message}`;
        }
      } finally {
        if (currentAbort === controller) currentAbort = null;
      }
    },

    /** 中断当前进行中的流式输出 */
    stopStream() {
      currentAbort?.abort();
      currentAbort = null;
    },

    /** 新会话：删除旧 session -> 清空 -> 新建 */
    async resetSession() {
      // 若有进行中的流式请求，先中断
      currentAbort?.abort();
      currentAbort = null;
      if (this.sessionId) {
        try {
          await api.deleteSession(this.sessionId);
        } catch {
          // 删除失败（可能已过期）不阻断本地重置
        }
      }
      localStorage.removeItem(SESSION_KEY);
      localStorage.removeItem(SESSION_TENANT_KEY);
      this.sessionId = null;
      this.messages = [];
      this.error = "";
      this.status = "idle";
      await this.createNewSession();
    },
  },
});
