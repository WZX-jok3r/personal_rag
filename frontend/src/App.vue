<script setup lang="ts">
import ChatView from "./components/ChatView.vue";
</script>

<template>
  <div class="app-shell">
    <header class="app-header">
      <h1>Personal RAG 知识库问答</h1>
      <span class="app-sub">Vue3 SPA · 通过 /api 调用 FastAPI 后端</span>
    </header>
    <main class="app-main">
      <ChatView />
    </main>
  </div>
</template>
