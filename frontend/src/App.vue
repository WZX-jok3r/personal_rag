<script setup lang="ts">
import { ref } from "vue";
import ChatView from "./components/ChatView.vue";
import UploadPanel from "./components/UploadPanel.vue";

const uploadOpen = ref(false);
</script>

<template>
  <div class="app-shell">

    <!-- 顶部产品栏 -->
    <header class="app-header">
      <div class="brand">
        <div class="logo">
          🧠
        </div>

        <div class="brand-text">
          <h1>RAG Studio</h1>
          <p>Personal Knowledge Assistant</p>
        </div>
      </div>

      <div class="header-actions">
        <button class="upload-btn" @click="uploadOpen = true">
          📄 上传文档
        </button>
        <div class="status">
          <span class="status-dot"></span>
          Online
        </div>
      </div>
    </header>


    <!-- 主区域 -->
    <main class="app-main">
      <ChatView />
    </main>

    <!-- 文档入库抽屉 -->
    <UploadPanel :open="uploadOpen" @close="uploadOpen = false" />


  </div>
</template>

<style scoped>
.header-actions {
  display: flex;
  align-items: center;
  gap: 16px;
}

.upload-btn {
  border: 1px solid var(--border);
  background: rgba(99, 102, 241, 0.15);
  color: var(--fg);
  padding: 8px 15px;
  border-radius: 12px;
  cursor: pointer;
  font-size: 13px;
}

.upload-btn:hover {
  background: rgba(99, 102, 241, 0.28);
}
</style>