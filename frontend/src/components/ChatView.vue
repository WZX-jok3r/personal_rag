<script setup lang="ts">
import { ref, nextTick, onMounted, watch, computed } from "vue";
import { useChatStore } from "../stores/chat";
import MessageBubble from "./MessageBubble.vue";

const chat = useChatStore();

const input = ref("");
const listEl = ref<HTMLElement | null>(null);

const streamingActive = computed(() =>
  chat.messages.some((m) => m.streaming && m.content.length > 0)
);


onMounted(() => {
  chat.ensureSession();
});


watch(
  () => [chat.messages.length, chat.status],
  async () => {
    await nextTick();

    if (listEl.value) {
      listEl.value.scrollTop = listEl.value.scrollHeight;
    }
  }
);


async function onSend() {
  const text = input.value;

  if (!text.trim() || chat.isLoading) return;

  input.value = "";

  await chat.sendMessageStream(text);
}


function onStop() {
  chat.stopStream();
}


async function onNewSession() {
  await chat.resetSession();
}

</script>


<template>

<section class="chat-view">


  <!-- 消息区域 -->
  <div ref="listEl" class="message-list">


    <!-- 欢迎页 -->
    <div
      v-if="chat.messages.length === 0 && !chat.isLoading"
      class="welcome"
    >

      <div class="welcome-icon">
        🧠
      </div>

      <h2>
        Personal RAG Assistant
      </h2>

      <p>
        基于你的个人知识库进行智能问答
      </p>


      <div class="suggestions">

        <div class="suggestion">
          📚 查询知识库内容
        </div>

        <div class="suggestion">
          💡 总结上传文档
        </div>

        <div class="suggestion">
          🔍 查找相关资料
        </div>

      </div>

    </div>



    <!-- 消息 -->
    <MessageBubble
      v-for="(m,i) in chat.messages"
      :key="i"
      :role="m.role"
      :content="m.content"
      :sources="m.sources"
      :hidden-count="m.hiddenCount"
      :streaming="m.streaming"
    />



    <!-- 思考动画 -->
    <div
      v-if="chat.isLoading && !streamingActive"
      class="thinking"
    >

      <div class="thinking-avatar">
        🤖
      </div>


      <div class="thinking-box">

        <span></span>
        <span></span>
        <span></span>

        <em>
          AI 正在检索知识库...
        </em>

      </div>

    </div>


  </div>




  <!-- 错误 -->
  <div
    v-if="chat.error"
    class="error-bar"
  >
    {{ chat.error }}
  </div>




  <!-- 输入区域 -->
  <div class="composer">


    <div class="composer-tools">


      <div class="topk">

        <span>
          🔎 Recall Top-K
        </span>


        <input
          v-model.number="chat.topK"
          type="range"
          min="1"
          max="20"
          step="1"
        />


        <strong>
          {{ chat.topK }}
        </strong>

      </div>



      <button
        class="new-session"
        @click="onNewSession"
      >
        ＋ 新会话
      </button>


    </div>




    <div class="input-box">


      <textarea
        v-model="input"
        rows="2"
        placeholder="输入你的问题..."
        @keydown.enter.exact.prevent="onSend"
      />


      <button
        v-if="!chat.isLoading"
        class="send"
        :disabled="!input.trim()"
        @click="onSend"
      >
        🚀
      </button>


      <button
        v-else
        class="stop"
        @click="onStop"
      >
        ■
      </button>


    </div>


  </div>



</section>

</template>



<style scoped>

.chat-view {

height:100%;

display:flex;

flex-direction:column;

}



.message-list {

flex:1;

overflow-y:auto;

padding:32px;

display:flex;

flex-direction:column;

gap:18px;

}



/* 欢迎区域 */

.welcome {

margin:auto;

text-align:center;

color:var(--muted);

}



.welcome-icon {

font-size:52px;

margin-bottom:15px;

}



.welcome h2 {

font-size:26px;

color:var(--fg);

margin-bottom:10px;

}



.welcome p {

font-size:15px;

}



.suggestions {

display:flex;

gap:12px;

margin-top:30px;

justify-content:center;

}



.suggestion {

padding:12px 18px;

background:rgba(255,255,255,.08);

border:1px solid var(--border);

border-radius:18px;

font-size:13px;

}




/* AI思考 */

.thinking {

display:flex;

gap:12px;

align-items:center;

}



.thinking-avatar {

font-size:28px;

}



.thinking-box {

padding:12px 18px;

border-radius:18px;

background:var(--card);

display:flex;

align-items:center;

gap:6px;

}



.thinking-box span {

width:6px;

height:6px;

background:var(--primary);

border-radius:50%;

animation:pulse 1s infinite;

}



.thinking-box span:nth-child(2){

animation-delay:.2s;

}


.thinking-box span:nth-child(3){

animation-delay:.4s;

}


@keyframes pulse {

50%{

opacity:.2;

}

}



.thinking-box em {

font-style:normal;

font-size:13px;

color:var(--muted);

}




.error-bar {

padding:10px 20px;

background:#fee2e2;

color:#dc2626;

}




.composer {

padding:15px 25px 25px;

border-top:1px solid var(--border);

background:rgba(0,0,0,.15);

}



.composer-tools {

display:flex;

justify-content:space-between;

align-items:center;

margin-bottom:12px;

}



.topk {

display:flex;

align-items:center;

gap:10px;

font-size:13px;

}



.topk input {

width:120px;

}



.topk strong {

min-width:25px;

}



.new-session {

border:1px solid var(--border);

background:transparent;

padding:8px 15px;

border-radius:12px;

cursor:pointer;

}



.input-box {

display:flex;

gap:12px;

background:var(--card);

padding:10px;

border-radius:20px;

border:1px solid var(--border);

}



textarea {

flex:1;

resize:none;

background:transparent;

border:none;

outline:none;

font-size:15px;

}



.send,
.stop {

width:46px;

height:46px;

border-radius:50%;

border:none;

cursor:pointer;

font-size:18px;

}



.send {

background:var(--primary);

color:white;

}



.send:disabled {

opacity:.4;

}



.stop {

background:#ef4444;

color:white;

}


</style>