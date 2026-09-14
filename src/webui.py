"""
webui.py - Gradio 问答小助手前端

功能:
1. 单轮问答：输入问题，返回答案 + 引用来源
2. 多轮对话：支持连续对话，保留历史上下文
3. 显示检索到的参考资料（可展开查看）
4. 显示来源文件和相似度分数

用法:
    python src/webui.py
    然后在浏览器打开 http://localhost:7860
"""

import gradio as gr
import logging
from typing import List, Dict, Any, Tuple

from rag_pipeline import get_pipeline, RAGPipeline

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# 全局 pipeline 实例（避免每次请求都初始化）
_pipeline: RAGPipeline = None


def get_global_pipeline() -> RAGPipeline:
    """获取全局 pipeline 实例（懒加载）"""
    global _pipeline
    if _pipeline is None:
        logger.info("[WebUI] 初始化 RAG Pipeline...")
        _pipeline = get_pipeline()
    return _pipeline


def format_sources(sources: List[Dict[str, Any]]) -> str:
    """格式化来源信息为 Markdown"""
    if not sources:
        return "*未找到相关参考资料*"

    lines = []
    for i, s in enumerate(sources, 1):
        lines.append(f"**[{i}]** `{s['source']}` ({s['format']}) — 相似度: {s['score']}")
        preview = s.get("text_preview", "")
        if preview:
            lines.append(f"> {preview[:150]}{'...' if len(preview) > 150 else ''}")
        lines.append("")

    return "\n".join(lines)


def single_query(query: str, top_k: int) -> Tuple[str, str]:
    """
    单轮问答
    Returns: (answer, sources_markdown)
    """
    if not query or not query.strip():
        return "请输入问题", ""

    try:
        pipeline = get_global_pipeline()
        result = pipeline.query(query, top_k=int(top_k))

        answer = result["answer"]
        sources_md = format_sources(result["sources"])

        return answer, sources_md

    except Exception as e:
        logger.error(f"[WebUI] 单轮查询出错: {e}")
        return f"抱歉，处理出错: {str(e)}", ""


def chat_query(message: str, history: List[Dict], top_k: int) -> Tuple[str, List[Dict]]:
    """
    多轮对话问答，适配 Gradio6 messages格式
    history: [{"role":"user","content":"xxx"}, {"role":"assistant","content":"xxx"},...]
    Returns: ("", updated_history)
    """
    if not message or not message.strip():
        return "", history

    try:
        pipeline = get_global_pipeline()
        # history 本身就是 role/content 字典数组，直接传给pipeline，无需循环组装
        result = pipeline.query_with_history(message, history, top_k=int(top_k))

        # 追加用户消息 + 助手回复
        history.append({"role": "user", "content": message})
        history.append({"role": "assistant", "content": result["answer"]})
        return "", history

    except Exception as e:
        logger.error(f"[WebUI] 对话查询出错: {e}")
        error_msg = f"抱歉，处理出错: {str(e)}"
        history.append({"role": "user", "content": message})
        history.append({"role": "assistant", "content": error_msg})
        return "", history


def build_ui() -> gr.Blocks:
    """构建 Gradio 界面"""
    with gr.Blocks(title="Personal RAG 知识库问答") as demo:
        gr.Markdown("""
        # 🤖 Personal RAG 知识库问答助手
        基于本地知识库的智能问答系统，支持 PDF、Word、Excel、Markdown、TXT 等多种格式。
        """)

        with gr.Tab("💬 单轮问答"):
            with gr.Row():
                with gr.Column(scale=2):
                    query_input = gr.Textbox(
                        label="问题",
                        placeholder="请输入您的问题，例如：路由器质保多久？",
                        lines=2,
                    )
                    top_k_single = gr.Slider(
                        minimum=1, maximum=20, value=5, step=1,
                        label="召回数量 (Top-K)",
                    )
                    submit_btn = gr.Button("🚀 查询", variant="primary")

                with gr.Column(scale=3):
                    answer_output = gr.Markdown(label="回答")
                    sources_output = gr.Markdown(label="📚 参考资料")
            submit_btn.click(
                fn=single_query,
                inputs=[query_input, top_k_single],
                outputs=[answer_output, sources_output],
            )
            gr.Examples(
                examples=[
                    ["路由器质保多久？", 5],
                    ["铰链的单价是多少？", 5],
                    ["新旧版本文档有什么冲突？", 5],
                    ["PET 肤感门板是什么基材？", 5],
                ],
                inputs=[query_input, top_k_single],
                label="示例问题",
            )

        with gr.Tab("🗨️ 多轮对话"):
            with gr.Row():
                top_k_chat = gr.Slider(
                    minimum=1, maximum=20, value=5, step=1,
                    label="召回数量 (Top-K)",
                )
            # 新增state：保存干净对话历史，仅用于传给LLM（不带参考资料）
            clean_history_state = gr.State([])
            chatbot = gr.Chatbot(
                label="对话历史",
                height=500,
            )
            with gr.Row():
                msg_input = gr.Textbox(
                    label="输入消息",
                    placeholder="请输入您的问题...",
                    scale=8,
                )
                send_btn = gr.Button("发送", variant="primary", scale=1)

            def chat_query(message: str, clean_history: List[Dict], top_k: int):
                if not message or not message.strip():
                    return "", clean_history, clean_history

                try:
                    pipeline = get_global_pipeline()
                    # ✅ 传给LLM的是干净历史，里面没有任何参考资料！
                    result = pipeline.query_with_history(message, clean_history, top_k=int(top_k))
                    answer = result["answer"]
                    sources_md = format_sources(result["sources"])
                    full_reply = f"{answer}\n\n---\n📚 参考资料\n{sources_md}"

                    # 更新干净历史：只追加纯问答，不拼接参考
                    new_clean_history = clean_history.copy()
                    new_clean_history.append({"role": "user", "content": message})
                    new_clean_history.append({"role": "assistant", "content": answer})

                    # 前端展示用的对话（带参考资料）
                    display_history = new_clean_history.copy()
                    display_history[-1]["content"] = full_reply

                    return "", new_clean_history, display_history
                except Exception as e:
                    logger.error(f"[WebUI] 对话查询出错: {e}")
                    error_msg = f"抱歉，处理出错: {str(e)}"
                    new_clean_history = clean_history.copy()
                    new_clean_history.append({"role": "user", "content": message})
                    new_clean_history.append({"role": "assistant", "content": error_msg})
                    display_history = new_clean_history.copy()
                    return "", new_clean_history, display_history

            send_btn.click(
                fn=chat_query,
                inputs=[msg_input, clean_history_state, top_k_chat],
                outputs=[msg_input, clean_history_state, chatbot],
            )
            msg_input.submit(
                fn=chat_query,
                inputs=[msg_input, clean_history_state, top_k_chat],
                outputs=[msg_input, clean_history_state, chatbot],
            )

            def clear_conversation():
                return "", [], []

            gr.Button("🗑️ 清空对话").click(
                fn=clear_conversation,
                outputs=[msg_input, clean_history_state, chatbot],
            )

        with gr.Tab("ℹ️ 系统信息"):
            gr.Markdown("""
            ### 系统配置
            - **Embedding 模型**: BAAI/bge-m3 (SiliconFlow)
            - **LLM 模型**: DeepSeek-V3.2 (SiliconFlow)
            - **向量库**: Qdrant (本地 Docker)
            - **支持格式**: PDF, DOCX, XLSX, MD, TXT

            ### 使用说明
            1. 将文档放入 `knowledge_base/` 目录
            2. 运行 `python src/ingest.py` 入库
            3. 在上方输入问题即可查询

            ### 增量更新
            - 新增/修改文件后，再次运行 `python src/ingest.py`
            - 已处理的未变更文件会自动跳过
            """)
    return demo


def main():
    """启动 Gradio 服务"""
    logger.info("[WebUI] 启动 Gradio 服务...")
    demo = build_ui()
    demo.launch(
        server_name="0.0.0.0",
        server_port=7860,
        share=False,
        show_error=True,
        theme=gr.themes.Soft()
    )


if __name__ == "__main__":
    main()
