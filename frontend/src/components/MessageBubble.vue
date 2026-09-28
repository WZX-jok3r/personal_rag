<script setup lang="ts">
import { computed } from "vue";
import MarkdownIt from "markdown-it";
import type { SourceItem } from "../api/types";
import SourcesList from "./SourcesList.vue";

const props = defineProps<{
  role: "user" | "assistant";
  content: string;
  sources?: SourceItem[];
}>();

// html:false 禁止内联 HTML，规避 XSS；linkify 自动识别链接
const md = new MarkdownIt({ html: false, linkify: true, breaks: true });

// assistant 走 markdown 渲染，user 保持纯文本
const rendered = computed(() =>
  props.role === "assistant" ? md.render(props.content) : ""
);
</script>

<template>
  <div class="bubble-row" :class="role">
    <div class="avatar">{{ role === "user" ? "我" : "AI" }}</div>
    <div class="bubble">
      <div v-if="role === 'assistant'" class="md" v-html="rendered"></div>
      <div v-else class="plain">{{ content }}</div>
      <SourcesList v-if="role === 'assistant'" :sources="sources" />
    </div>
  </div>
</template>

<style scoped>
.bubble-row {
  display: flex;
  gap: 10px;
  align-items: flex-start;
}

.bubble-row.user {
  flex-direction: row-reverse;
}

.avatar {
  flex: 0 0 auto;
  width: 30px;
  height: 30px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 12px;
  color: #fff;
  background: var(--muted);
}

.bubble-row.user .avatar {
  background: var(--user-bubble);
}
.bubble-row.assistant .avatar {
  background: #16a34a;
}

.bubble {
  max-width: 76%;
  padding: 10px 14px;
  border-radius: var(--radius);
  font-size: 14px;
  line-height: 1.6;
  word-break: break-word;
}

.bubble-row.user .bubble {
  background: var(--user-bubble);
  color: #fff;
}

.bubble-row.assistant .bubble {
  background: var(--bot-bubble);
  border: 1px solid var(--border);
}

.plain {
  white-space: pre-wrap;
}

/* markdown 内容样式 */
.md :deep(p) {
  margin: 0 0 8px;
}
.md :deep(p:last-child) {
  margin-bottom: 0;
}
.md :deep(table) {
  border-collapse: collapse;
  margin: 8px 0;
  font-size: 13px;
}
.md :deep(th),
.md :deep(td) {
  border: 1px solid var(--border);
  padding: 4px 8px;
}
.md :deep(code) {
  background: #f0f2f5;
  padding: 1px 5px;
  border-radius: 4px;
}
.md :deep(pre) {
  background: #0f172a;
  color: #e2e8f0;
  padding: 10px 12px;
  border-radius: 8px;
  overflow-x: auto;
}
.md :deep(pre code) {
  background: transparent;
  color: inherit;
  padding: 0;
}
.md :deep(ul),
.md :deep(ol) {
  margin: 0 0 8px;
  padding-left: 20px;
}
</style>
