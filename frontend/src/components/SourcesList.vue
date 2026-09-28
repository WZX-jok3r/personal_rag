<script setup lang="ts">
import { ref } from "vue";
import type { SourceItem } from "../api/types";

defineProps<{ sources?: SourceItem[] }>();

// 默认折叠，点击展开查看引用片段
const open = ref(false);
</script>

<template>
  <div v-if="sources && sources.length" class="sources">
    <button class="sources-toggle" @click="open = !open">
      📚 参考资料 {{ sources.length }} 条 {{ open ? "▲" : "▼" }}
    </button>
    <ol v-show="open" class="sources-list">
      <li v-for="(s, i) in sources" :key="i" class="source-item">
        <div class="source-head">
          <b>[{{ i + 1 }}]</b>
          <code>{{ s.source }}</code>
          <span class="fmt">({{ s.format }})</span>
          <span class="score">score: {{ s.score }}</span>
        </div>
        <p v-if="s.text_preview" class="preview">{{ s.text_preview }}</p>
      </li>
    </ol>
  </div>
</template>

<style scoped>
.sources {
  margin-top: 8px;
  border-top: 1px dashed var(--border);
  padding-top: 6px;
}

.sources-toggle {
  background: transparent;
  color: var(--muted);
  font-size: 12px;
  padding: 2px 0;
}

.sources-list {
  margin: 8px 0 0;
  padding-left: 18px;
  font-size: 12px;
  color: var(--fg);
}

.source-item {
  margin-bottom: 8px;
}

.source-head {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}

.source-head code {
  background: #f0f2f5;
  padding: 1px 6px;
  border-radius: 4px;
}

.fmt,
.score {
  color: var(--muted);
}

.preview {
  margin: 4px 0 0;
  color: var(--muted);
  border-left: 2px solid var(--border);
  padding-left: 8px;
  white-space: pre-wrap;
}
</style>
