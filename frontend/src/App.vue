<script setup lang="ts">
import ChatView from "./components/ChatView.vue";
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

      <div class="status">
        <span class="status-dot"></span>
        Online
      </div>
    </header>


    <!-- 主区域 -->
    <main class="app-main">
      <ChatView />
    </main>


  </div>
</template>