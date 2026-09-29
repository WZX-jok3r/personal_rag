<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import ChatView from "./components/ChatView.vue";
import UploadPanel from "./components/UploadPanel.vue";
import { getApiKey, setApiKey, clearApiKey } from "./api/client";

const uploadOpen = ref(false);

// 租户 Key（API Key）轻量设置：写入 localStorage，决定上传/列表/删除的租户归属。
const keyOpen = ref(false);
const keyInput = ref("");
const currentKey = ref("");
const hasKey = computed(() => currentKey.value.length > 0);

onMounted(() => {
  currentKey.value = getApiKey();
});

function openKeyPanel() {
  keyInput.value = currentKey.value;
  keyOpen.value = true;
}

function saveKey() {
  setApiKey(keyInput.value.trim());
  currentKey.value = getApiKey();
  keyOpen.value = false;
}

function clearKey() {
  clearApiKey();
  currentKey.value = "";
  keyInput.value = "";
}

function maskedKey(k: string): string {
  if (k.length <= 4) return "*".repeat(k.length);
  return k.slice(0, 2) + "•".repeat(Math.min(8, k.length - 4)) + k.slice(-2);
}
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
        <div class="key-wrap">
          <button
            class="key-btn"
            :class="{ unset: !hasKey }"
            @click="keyOpen ? (keyOpen = false) : openKeyPanel()"
          >
            🔑 {{ hasKey ? "租户：已设置" : "设置租户 Key" }}
          </button>

          <div v-if="keyOpen" class="key-pop">
            <p class="key-title">API Key（决定文档归属租户）</p>
            <p v-if="hasKey" class="key-cur">当前：<code>{{ maskedKey(currentKey) }}</code></p>
            <p v-else class="key-warn">未设置 Key，请求将回 401。</p>
            <input
              v-model="keyInput"
              class="key-input"
              type="text"
              placeholder="粘贴租户 API Key，如 a=abc_wzx11 对应的值"
              @keyup.enter="saveKey"
            />
            <div class="key-actions">
              <button class="key-save" @click="saveKey">保存</button>
              <button class="key-clear" :disabled="!hasKey" @click="clearKey">清除</button>
            </div>
            <p class="key-hint">保存后即时生效（无需重启），刷新对话/上传均使用该 Key。</p>
          </div>
        </div>

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

.key-wrap {
  position: relative;
}

.key-btn {
  border: 1px solid var(--border);
  background: rgba(6, 182, 212, 0.15);
  color: var(--fg);
  padding: 8px 14px;
  border-radius: 12px;
  cursor: pointer;
  font-size: 13px;
  white-space: nowrap;
}

.key-btn:hover {
  background: rgba(6, 182, 212, 0.28);
}

.key-btn.unset {
  background: rgba(245, 158, 11, 0.16);
}

.key-btn.unset:hover {
  background: rgba(245, 158, 11, 0.3);
}

.key-pop {
  position: absolute;
  top: calc(100% + 10px);
  right: 0;
  width: 300px;
  background: rgba(15, 23, 42, 0.97);
  border: 1px solid var(--border);
  border-radius: 14px;
  padding: 14px;
  display: flex;
  flex-direction: column;
  gap: 8px;
  z-index: 60;
  box-shadow: 0 12px 32px rgba(2, 6, 23, 0.5);
}

.key-title {
  margin: 0;
  font-size: 13px;
  font-weight: 600;
  color: var(--fg);
}

.key-cur {
  margin: 0;
  font-size: 12px;
  color: var(--muted);
}

.key-cur code {
  background: rgba(148, 163, 184, 0.15);
  padding: 1px 6px;
  border-radius: 6px;
}

.key-warn {
  margin: 0;
  font-size: 12px;
  color: var(--danger);
}

.key-input {
  width: 100%;
  box-sizing: border-box;
  background: rgba(30, 41, 59, 0.7);
  border: 1px solid var(--border);
  border-radius: 10px;
  color: var(--fg);
  padding: 8px 10px;
  font-size: 13px;
}

.key-actions {
  display: flex;
  gap: 8px;
}

.key-save {
  flex: 1;
  background: linear-gradient(135deg, #6366f1, #06b6d4);
  color: #fff;
  border: none;
  border-radius: 10px;
  padding: 8px 10px;
  cursor: pointer;
  font-size: 13px;
}

.key-clear {
  border: 1px solid var(--border);
  background: transparent;
  color: var(--muted);
  border-radius: 10px;
  padding: 8px 12px;
  cursor: pointer;
  font-size: 13px;
}

.key-clear:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

.key-hint {
  margin: 0;
  font-size: 11px;
  color: var(--muted);
}
</style>