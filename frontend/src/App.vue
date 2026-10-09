<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import ChatView from "./components/ChatView.vue";
import UploadPanel from "./components/UploadPanel.vue";
import { getApiKey, setApiKey, clearApiKey, verifyApiKey } from "./api/client";

const uploadOpen = ref(false);

// 租户 Key（API Key）设置：写入 localStorage，决定上传/问答/删除的文档归属租户。
type KeyState = "unknown" | "checking" | "valid" | "invalid" | "unset";
const keyOpen = ref(false);
const keyInput = ref("");
const currentKey = ref("");
const keyState = ref<KeyState>("unknown");
const saving = ref(false);
const saveError = ref("");

const label = computed(() => {
  switch (keyState.value) {
    case "valid":
      return "租户：已连接";
    case "invalid":
      return "租户：Key 无效";
    case "checking":
      return "租户：校验中…";
    case "unset":
      return "设置租户 Key";
    default:
      return "租户：检测中";
  }
});

const btnClass = computed(() => `state-${keyState.value}`);

function maskedKey(k: string): string {
  if (!k) return "";
  if (k.length <= 4) return "*".repeat(k.length);
  return k.slice(0, 2) + "•".repeat(Math.min(10, k.length - 4)) + k.slice(-2);
}

async function refreshState() {
  currentKey.value = getApiKey();
  if (!currentKey.value) {
    keyState.value = "unset";
    return;
  }
  keyState.value = "checking";
  keyState.value = (await verifyApiKey()) ? "valid" : "invalid";
}

onMounted(async () => {
  await refreshState();
  // 无有效 Key 时主动弹出设置框，避免用户对着 401 发懵
  if (keyState.value !== "valid") {
    keyInput.value = currentKey.value;
    keyOpen.value = true;
  }
});

function openPanel() {
  keyInput.value = currentKey.value;
  saveError.value = "";
  keyOpen.value = true;
}

function closePanel() {
  keyOpen.value = false;
}

async function saveKey() {
  const k = keyInput.value.trim();
  saveError.value = "";
  setApiKey(k);
  if (!k) {
    currentKey.value = "";
    keyState.value = "unset";
    return;
  }
  saving.value = true;
  const ok = await verifyApiKey();
  saving.value = false;
  if (!ok) {
    keyState.value = "invalid";
    saveError.value =
      "该 Key 无效或无权限（后端返回 401）。请检查是否误粘贴了 `a=` 前缀——应只填值本身，如 abc_wzx11。";
    return;
  }
  currentKey.value = k;
  keyState.value = "valid";
  keyOpen.value = false;
  // Key 变更后重载，让会话/对话以新身份重新初始化
  location.reload();
}

function clearKey() {
  clearApiKey();
  currentKey.value = "";
  keyState.value = "unset";
  keyInput.value = "";
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
        <button class="key-btn" :class="btnClass" @click="openPanel">
          🔑 {{ label }}
        </button>

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

    <!-- 租户 Key 设置模态（Teleport 到 body，避免被 header 的 backdrop-filter 层叠裁切） -->
    <Teleport to="body">
      <div v-if="keyOpen" class="modal-overlay" @click.self="closePanel">
        <div class="key-modal">
          <div class="km-head">
            <h3>🔑 租户 API Key</h3>
            <button class="km-close" @click="closePanel">✕</button>
          </div>
          <p class="km-desc">Key 决定上传 / 问答 / 删除的文档归属租户；后端开启鉴权后必须携带，否则 401。</p>
          <p v-if="currentKey" class="km-cur">当前：<code>{{ maskedKey(currentKey) }}</code></p>
          <input
            v-model="keyInput"
            class="km-input"
            type="text"
            placeholder="只填 Key 值本身，例如 abc_wzx11"
            @keyup.enter="saveKey"
          />
          <p v-if="saveError" class="km-err">{{ saveError }}</p>
          <div class="km-actions">
            <button class="km-clear" :disabled="!currentKey" @click="clearKey">清除</button>
            <button class="km-save" :disabled="saving" @click="saveKey">
              {{ saving ? "校验中…" : "保存并校验" }}
            </button>
          </div>
        </div>
      </div>
    </Teleport>

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

.key-btn {
  border: 1px solid var(--border);
  color: var(--fg);
  padding: 8px 14px;
  border-radius: 12px;
  cursor: pointer;
  font-size: 13px;
  white-space: nowrap;
  background: rgba(6, 182, 212, 0.15);
}

.key-btn:hover {
  filter: brightness(1.15);
}

.key-btn.state-valid {
  background: rgba(34, 197, 94, 0.18);
}

.key-btn.state-invalid {
  background: rgba(239, 68, 68, 0.22);
}

.key-btn.state-unset {
  background: rgba(245, 158, 11, 0.18);
}

.key-btn.state-checking,
.key-btn.state-unknown {
  background: rgba(148, 163, 184, 0.16);
}

.modal-overlay {
  position: fixed;
  inset: 0;
  background: rgba(2, 6, 23, 0.6);
  backdrop-filter: blur(3px);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 100;
  padding: 20px;
}

.key-modal {
  width: 420px;
  max-width: 92vw;
  background: rgba(15, 23, 42, 0.97);
  border: 1px solid var(--border);
  border-radius: 18px;
  padding: 20px;
  display: flex;
  flex-direction: column;
  gap: 12px;
  box-shadow: 0 24px 64px rgba(2, 6, 23, 0.6);
}

.km-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.km-head h3 {
  margin: 0;
  font-size: 17px;
}

.km-close {
  background: transparent;
  border: 1px solid var(--border);
  color: var(--muted);
  border-radius: 10px;
  width: 30px;
  height: 30px;
  cursor: pointer;
}

.km-desc {
  margin: 0;
  font-size: 12px;
  color: var(--muted);
  line-height: 1.5;
}

.km-cur {
  margin: 0;
  font-size: 12px;
  color: var(--muted);
}

.km-cur code {
  background: rgba(148, 163, 184, 0.15);
  padding: 1px 6px;
  border-radius: 6px;
}

.km-input {
  width: 100%;
  background: rgba(30, 41, 59, 0.7);
  border: 1px solid var(--border);
  border-radius: 10px;
  color: var(--fg);
  padding: 10px 12px;
  font-size: 14px;
}

.km-err {
  margin: 0;
  font-size: 12px;
  color: var(--danger);
  line-height: 1.5;
}

.km-actions {
  display: flex;
  gap: 10px;
}

.km-save {
  flex: 1;
  background: linear-gradient(135deg, #6366f1, #06b6d4);
  color: #fff;
  border: none;
  border-radius: 10px;
  padding: 10px 12px;
  cursor: pointer;
  font-size: 14px;
}

.km-save:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}

.km-clear {
  border: 1px solid var(--border);
  background: transparent;
  color: var(--muted);
  border-radius: 10px;
  padding: 10px 14px;
  cursor: pointer;
  font-size: 14px;
}

.km-clear:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}
</style>
