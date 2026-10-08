<script setup lang="ts">
import { computed } from "vue";
import MarkdownIt from "markdown-it";
import AgentTrace from "./AgentTrace.vue";
import SourcesList from "./SourcesList.vue";
import type { AgentStats, TraceStep } from "../api/types";

const props = defineProps<{
  role: "user" | "assistant";
  content: string;
  sources?: any[];
  hiddenCount?: number;
  streaming?: boolean;
  /** Agent 执行轨迹（agent 模式下有内容时渲染折叠面板） */
  trace?: TraceStep[];
  /** 回答依据的数据通道，用于在气泡上打标 */
  channel?: "rag" | "sql" | "multi";
  /** 口径澄清问句 */
  clarify?: string;
  /** Agent 运行统计 */
  stats?: AgentStats;
}>();


const md = new MarkdownIt({
  html: false,
  breaks: true,
  linkify: true,
});


const rendered = computed(() =>
  md.render(props.content || "")
);

// 通道徽标：让用户一眼看出这条答案是"查文档"还是"算数据"得来的
const channelBadge = computed(() => {
  if (props.role !== "assistant") return "";
  if (props.channel === "sql") return "📊 数据查询";
  if (props.channel === "multi") return "🔀 文档+数据";
  if (props.channel === "rag") return "📚 文档检索";
  return "";
});

const statsText = computed(() => {
  const s = props.stats;
  if (!s) return "";
  const parts = [`${s.steps} 步`, `${s.llm_calls} 次模型调用`, `${(s.elapsed_ms / 1000).toFixed(1)}s`];
  if (s.degraded) parts.push(`已降级(${s.degraded_reason})`);
  return parts.join(" · ");
});

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
          ? "知识库 Agent"
          : "You"
        }}
      </strong>

      <span v-if="channelBadge" class="channel-badge">{{ channelBadge }}</span>

      <span v-if="props.streaming">
        正在处理...
      </span>


    </div>


    <!-- Agent 执行轨迹（折叠） -->
    <AgentTrace
      v-if="props.role === 'assistant' && props.trace && props.trace.length"
      :steps="props.trace"
      :collapsed="true"
    />

    <div
      class="markdown"
      v-html="rendered"
    />

    <!-- 口径澄清提示：这类回答不是答案，需要用户补充信息 -->
    <div v-if="props.clarify" class="clarify-hint">
      💬 请补充统计口径后我再查询
    </div>

    <!-- 运行统计（成本/步数可观测） -->
    <div v-if="statsText" class="agent-stats">{{ statsText }}</div>



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



/* 通道徽标：一眼看出答案来自"查文档"还是"算数据" */

.channel-badge {

padding:2px 8px;

border-radius:999px;

background:rgba(37,99,235,.12);

color:#2563eb;

font-weight:600;

font-size:11px !important;

margin-left:8px;

margin-right:auto;

}



.agent-stats {

margin-top:8px;

font-size:11px;

color:var(--text-muted,#9ca3af);

}



.clarify-hint {

margin-top:10px;

padding:8px 10px;

border-radius:8px;

background:rgba(245,158,11,.12);

color:#b45309;

font-size:12.5px;

font-weight:500;

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