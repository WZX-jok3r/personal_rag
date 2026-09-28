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
}

const SESSION_KEY = "rag_session_id";

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
    /** 进入页面：优先复用 localStorage 里的 session，否则惰性新建 */
    async ensureSession() {
      if (this.initialized) return;
      const saved = localStorage.getItem(SESSION_KEY);
      if (saved) {
        this.sessionId = saved;
      } else {
        await this.createNewSession();
      }
      this.initialized = true;
    },

    /** 新建后端会话并写入 localStorage */
    async createNewSession() {
      try {
        const res = await api.createSession();
        this.sessionId = res.session_id;
        localStorage.setItem(SESSION_KEY, res.session_id);
        this.messages = [];
        this.error = "";
        this.status = "idle";
      } catch (e) {
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

    /** 新会话：删除旧 session -> 清空 -> 新建 */
    async resetSession() {
      if (this.sessionId) {
        try {
          await api.deleteSession(this.sessionId);
        } catch {
          // 删除失败（可能已过期）不阻断本地重置
        }
      }
      localStorage.removeItem(SESSION_KEY);
      this.sessionId = null;
      this.messages = [];
      this.error = "";
      this.status = "idle";
      await this.createNewSession();
    },
  },
});
