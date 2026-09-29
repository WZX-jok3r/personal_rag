<script setup lang="ts">
import { computed } from "vue";
import MarkdownIt from "markdown-it";
import SourcesList from "./SourcesList.vue";

const props = defineProps<{
  role: "user" | "assistant";
  content: string;
  sources?: any[];
  hiddenCount?: number;
  streaming?: boolean;
}>();


const md = new MarkdownIt({
  html: false,
  breaks: true,
  linkify: true,
});


const rendered = computed(() =>
  md.render(props.content || "")
);

</script>



<template>

<div
  class="message-row"
  :class="props.role"
>


  <!-- Avatar -->

  <div class="avatar">

    <span v-if="props.role === 'assistant'">
      🤖
    </span>

    <span v-else>
      👤
    </span>

  </div>



  <!-- 内容 -->

  <div class="message-card">


    <div class="message-header">

      <strong>
        {{
          props.role === "assistant"
          ? "RAG Assistant"
          : "You"
        }}
      </strong>


      <span v-if="props.streaming">
        正在生成...
      </span>


    </div>



    <div
      class="markdown"
      v-html="rendered"
    />



    <!-- 来源 -->

    <SourcesList
      v-if="
        props.role === 'assistant'
        &&
        props.sources
        &&
        props.sources.length
      "
      :sources="props.sources"
    />


  </div>


</div>


</template>



<style scoped>


.message-row {

display:flex;

gap:14px;

width:100%;

align-items:flex-start;

}



.message-row.user {

flex-direction:row-reverse;

}




.avatar {

width:38px;

height:38px;

border-radius:50%;

display:flex;

align-items:center;

justify-content:center;

background:var(--card);

border:1px solid var(--border);

font-size:20px;

flex-shrink:0;

}



.message-card {

max-width:75%;

padding:16px 18px;

border-radius:20px;

background:var(--card);

border:1px solid var(--border);

box-shadow:0 8px 25px rgba(0,0,0,.08);

}



.user .message-card {

background:linear-gradient(
135deg,
#2563eb,
#4f46e5
);

color:white;

border:none;

}



.message-header {

display:flex;

justify-content:space-between;

align-items:center;

margin-bottom:10px;

font-size:13px;

opacity:.8;

}



.message-header span {

font-size:12px;

}



.markdown {

font-size:15px;

line-height:1.7;

word-break:break-word;

}



/* Markdown */

.markdown :deep(p){

margin:8px 0;

}



.markdown :deep(h1),
.markdown :deep(h2),
.markdown :deep(h3){

margin-top:16px;

margin-bottom:8px;

}



.markdown :deep(code){

background:rgba(127,127,127,.18);

padding:3px 6px;

border-radius:6px;

font-family:
"JetBrains Mono",
monospace;

font-size:13px;

}



.markdown :deep(pre){

background:#111827;

color:#e5e7eb;

padding:15px;

border-radius:14px;

overflow-x:auto;

margin:14px 0;

}



.markdown :deep(pre code){

background:none;

padding:0;

color:inherit;

}




</style>