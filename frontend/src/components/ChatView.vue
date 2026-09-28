<script setup lang="ts">
import { ref, nextTick, onMounted, watch } from "vue";
import { useChatStore } from "../stores/chat";
import MessageBubble from "./MessageBubble.vue";

const chat = useChatStore();
const input = ref("");
const listEl = ref<HTMLElement | null>(null);

onMounted(() => {
  chat.ensureSession();
});

// 新消息或加载态变化时自动滚到底部
watch(
  () => [chat.messages.length, chat.status],
  async () => {
    await nextTick();
    if (listEl.value) listEl.value.scrollTop = listEl.value.scrollHeight;
  }
);

async function onSend() {
  const text = input.value;
  if (!text.trim() || chat.isLoading) return;
  input.value = "";
  await chat.sendMessage(text);
}

async function onNewSession() {
  await chat.resetSession();
}
</script>

<template>
  <section class="chat-view">
    <div ref="listEl" class="message-list">
      <p v-if="chat.messages.length === 0 && !chat.isLoading" class="empty-hint">
        输入问题开始对话，回答会引用知识库来源。
      </p>
      <MessageBubble
        v-for="(m, i) in chat.messages"
        :key="i"
        :role="m.role"
        :content="m.content"
        :sources="m.sources"
        :hidden-count="m.hiddenCount"
      />
      <div v-if="chat.isLoading" class="loading-row">
        <span class="dot"></span><span class="dot"></span><span class="dot"></span>
        <em>正在思考…</em>
      </div>
    </div>

    <div v-if="chat.error" class="error-bar">{{ chat.error }}</div>

    <div class="composer">
      <div class="composer-top">
        <label class="topk">
          召回 Top-K
          <input v-model.number="chat.topK" type="range" min="1" max="20" step="1" />
          <b>{{ chat.topK }}</b>
        </label>
        <button class="btn-ghost" @click="onNewSession">＋ 新会话</button>
      </div>
      <div class="composer-input">
        <textarea
          v-model="input"
          rows="2"
          placeholder="请输入您的问题，回车发送（Shift+Enter 换行）"
          @keydown.enter.exact.prevent="onSend"
        ></textarea>
        <button class="btn-primary" :disabled="chat.isLoading || !input.trim()" @click="onSend">
          发送
        </button>
      </div>
    </div>
  </section>
</template>

<style scoped>
.chat-view {
  display: flex;
  flex-direction: column;
  height: 100%;
  min-height: 0;
}

.message-list {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  padding: 20px;
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.empty-hint {
  color: var(--muted);
  text-align: center;
  margin-top: 40px;
}

.loading-row {
  display: flex;
  align-items: center;
  gap: 6px;
  color: var(--muted);
  font-size: 13px;
}

.dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--muted);
  animation: blink 1.2s infinite ease-in-out;
}
.dot:nth-child(2) { animation-delay: 0.2s; }
.dot:nth-child(3) { animation-delay: 0.4s; }

@keyframes blink {
  0%, 80%, 100% { opacity: 0.2; }
  40% { opacity: 1; }
}

.error-bar {
  background: #fef2f2;
  color: var(--danger);
  padding: 8px 20px;
  font-size: 13px;
  border-top: 1px solid #fecaca;
}

.composer {
  border-top: 1px solid var(--border);
  padding: 10px 20px 16px;
  background: #fff;
}

.composer-top {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 8px;
}

.topk {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  color: var(--muted);
}
.topk input[type="range"] {
  width: 120px;
}
.topk b {
  color: var(--fg);
  min-width: 18px;
  text-align: center;
}

.composer-input {
  display: flex;
  gap: 10px;
  align-items: flex-end;
}

.composer-input textarea {
  flex: 1;
  resize: vertical;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 10px 12px;
  font-size: 14px;
  font-family: inherit;
}
.composer-input textarea:focus {
  outline: none;
  border-color: var(--primary);
}
</style>
