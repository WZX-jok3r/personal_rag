import re
import time
from datetime import datetime


# ========== 1. 定义工具 ==========

def tool_calculator(expr: str) -> str:
    """计算器工具：执行四则运算"""
    # 白名单过滤，防止代码注入
    if not re.fullmatch(r"[0-9+\-*/().\s]+", expr):
        return "错误：表达式不合法，只支持数字和+-*/运算符"
    try:
        result = eval(expr, {"__builtins__": {}}, {})
        return str(result)
    except Exception as e:
        return f"计算失败: {e}"


def tool_search(query: str) -> str:
    """搜索工具：模拟搜索引擎（实际项目替换为真实API）"""
    # 模拟知识库
    knowledge_base = {
        "天气": "合肥今天晴，气温25度",
        "python": "Python是一种广泛使用的高级编程语言",
        "react": "ReAct是Reasoning + Acting的缩写，是一种Agent设计模式",
        "agent": "Agent是能自主感知环境并采取行动完成任务的智能体",
        "rag": "RAG是检索增强生成，通过检索外部知识库来增强LLM的回答能力",
    }

    # 简单关键词匹配
    for keyword, answer in knowledge_base.items():
        if keyword.lower() in query.lower():
            return answer

    return f"未找到'{query}'的相关信息（这是模拟搜索，实际项目需接入真实搜索API）"


# 工具注册表
TOOLS = {
    "calculator": {
        "func": tool_calculator,
        "description": "计算器，用于数学计算。输入格式：calculator(表达式)，例如 calculator(2+3*4)"
    },
    "search": {
        "func": tool_search,
        "description": "搜索引擎，用于查询信息。输入格式：search(关键词)，例如 search(天气)"
    }
}


# ========== 2. 构造系统提示词 ==========

def build_system_prompt() -> str:
    """构建系统提示词，告诉LLM可用的工具和输出格式"""
    tools_desc = "\n".join([f"- {name}: {info['description']}" for name, info in TOOLS.items()])

    return f"""你是一个智能助手，通过思考、调用工具来回答问题。

可用工具：
{tools_desc}

你必须严格按照以下格式输出：

Thought: <你的推理过程，说明下一步要做什么>
Action: <工具名称>
Action Input: <工具参数>

当工具执行后，你会收到 Observation: <工具返回结果>

重复上述过程，直到你能给出最终答案。
当你有最终答案时，输出：
Thought: 我已经得到了最终答案
Final Answer: <你的最终回答>

重要规则：
1. 每次只能调用一个工具
2. 必须严格按照 Thought -> Action -> Action Input 的格式
3. 不要编造 Observation，Observation 由系统提供
4. 如果工具返回错误，请分析错误原因并调整参数重试
"""


# ========== 3. ReAct 主循环引擎 ==========

def parse_llm_output(llm_output: str) -> dict:
    """
    解析LLM输出，提取 Thought、Action、Action Input 或 Final Answer
    """
    result = {
        "thought": "",
        "action": None,
        "action_input": None,
        "final_answer": None
    }

    # 提取 Thought
    thought_match = re.search(r"Thought:\s*(.+)", llm_output, re.IGNORECASE)
    if thought_match:
        result["thought"] = thought_match.group(1).strip()

    # 检查是否有 Final Answer
    final_match = re.search(r"Final Answer:\s*(.+)", llm_output, re.IGNORECASE | re.DOTALL)
    if final_match:
        result["final_answer"] = final_match.group(1).strip()
        return result

    # 提取 Action
    action_match = re.search(r"Action:\s*(\w+)", llm_output, re.IGNORECASE)
    if action_match:
        result["action"] = action_match.group(1).strip()

    # 提取 Action Input
    input_match = re.search(r"Action Input:\s*(.+)", llm_output, re.IGNORECASE)
    if input_match:
        result["action_input"] = input_match.group(1).strip().strip('"').strip("'")

    return result


def call_llm(messages: list) -> str:
    """
    调用LLM API（这里用模拟输出，实际项目替换为真实API调用）

    实际使用时替换为：
    from openai import OpenAI
    client = OpenAI(api_key="your-api-key")
    response = client.chat.completions.create(
        model="gpt-3.5-turbo",
        messages=messages,
        stop=["Observation:"]  # 关键：防止LLM自己编造Observation
    )
    return response.choices[0].message.content
    """

    # 模拟LLM输出（用于演示流程，实际项目删除这段）
    # 关键：只看最后一条消息判断"当前阶段"，避免被 system prompt 里的工具名污染
    last_msg = messages[-1]["content"] if messages else ""

    # 若最后一条是工具返回的 Observation，则基于工具结果决定下一步
    if last_msg.startswith("Observation:"):
        if "200" in last_msg:
            return """Thought: 括号内计算结果是200，现在需要计算 200*3。
Action: calculator
Action Input: 200*3"""

        if "600" in last_msg:
            return """Thought: 我已经得到了计算结果，(128+72)*3 = 600。
Final Answer: (128+72)*3 的结果是 600"""

        if "合肥" in last_msg or "25度" in last_msg:
            return """Thought: 我已经查询到天气信息，可以给出最终答案。
Final Answer: 合肥今天晴天，气温25度。"""

    # 否则是初始用户问题，根据问题内容决定首个工具调用
    if "128+72" in last_msg:
        return """Thought: 用户要求计算 (128+72)*3，我需要先计算括号内的加法，再乘以3。先调用计算器计算 128+72。
Action: calculator
Action Input: 128+72"""

    if "天气" in last_msg:
        return """Thought: 用户询问天气信息，我需要调用搜索工具查询。
Action: search
Action Input: 天气"""

    return """Thought: 我需要理解用户的问题并调用合适的工具。
Final Answer: 这是一个模拟响应，实际项目需要接入真实LLM API"""


def run_react_agent(
        question: str,
        max_steps: int = 10,
        timeout_seconds: int = 30
) -> dict:
    """
    运行 ReAct Agent 主循环

    参数：
        question: 用户问题
        max_steps: 最大步数限制，防止死循环
        timeout_seconds: 超时时间（秒），防止长时间运行

    返回：
        包含答案、步数、耗时等信息的字典
    """

    # 初始化
    start_time = time.time()
    system_prompt = build_system_prompt()

    # 构建消息历史
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"Question: {question}"}
    ]

    # 记录调用历史，用于检测重复调用
    call_history = []

    # 记录每一步的思考与工具调用过程，用于最终展示
    traces = []

    print("=" * 60)
    print(f"开始处理问题: {question}")
    print(f"最大步数: {max_steps}, 超时限制: {timeout_seconds}秒")
    print("=" * 60)

    # ========== ReAct 主循环 ==========
    for step in range(1, max_steps + 1):
        print(f"\n{'─' * 60}")
        print(f"📍 第 {step} 步")

        # 检查超时
        elapsed = time.time() - start_time
        if elapsed > timeout_seconds:
            print(f"⏰ 超时！已运行 {elapsed:.1f} 秒，超过限制 {timeout_seconds} 秒")
            return {
                "success": False,
                "answer": f"任务超时，已运行 {elapsed:.1f} 秒",
                "steps": step,
                "elapsed_time": elapsed,
                "traces": traces
            }

        # 1. 调用 LLM
        print("🤖 调用 LLM...")
        llm_output = call_llm(messages)
        print(f"LLM 输出:\n{llm_output}")

        # 2. 解析 LLM 输出
        parsed = parse_llm_output(llm_output)

        # 3. 检查是否有最终答案
        if parsed["final_answer"]:
            elapsed = time.time() - start_time
            traces.append({
                "step": step,
                "thought": parsed["thought"],
                "final_answer": parsed["final_answer"],
            })
            print(f"\n✅ 任务完成！")
            print(f"最终答案: {parsed['final_answer']}")
            print(f"总步数: {step}, 耗时: {elapsed:.1f}秒")
            print("=" * 60)
            return {
                "success": True,
                "answer": parsed["final_answer"],
                "steps": step,
                "elapsed_time": elapsed,
                "traces": traces
            }

        # 4. 检查是否有 Action
        if not parsed["action"]:
            print("⚠️ 未解析到 Action，要求重试...")
            messages.append({"role": "assistant", "content": llm_output})
            messages.append({"role": "user", "content": "请严格按照 Thought -> Action -> Action Input 格式输出"})
            continue

        # 5. 检查工具是否存在
        if parsed["action"] not in TOOLS:
            print(f"❌ 未知工具: {parsed['action']}")
            observation = f"错误：未知工具 '{parsed['action']}'，可用工具: {list(TOOLS.keys())}"
        else:
            # 6. 检测重复调用（防死循环）
            call_signature = f"{parsed['action']}({parsed['action_input']})"
            if call_signature in call_history:
                print(f"🔄 检测到重复调用: {call_signature}，强制终止")
                return {
                    "success": False,
                    "answer": f"检测到重复调用 {call_signature}，可能陷入死循环",
                    "steps": step,
                    "elapsed_time": time.time() - start_time,
                    "traces": traces
                }

            # 7. 执行工具
            print(f"🔧 执行工具: {parsed['action']}({parsed['action_input']})")
            try:
                tool_func = TOOLS[parsed["action"]]["func"]
                observation = tool_func(parsed["action_input"])
                print(f"📥 工具返回: {observation}")
            except Exception as e:
                observation = f"工具执行异常: {e}"
                print(f"❌ 工具执行异常: {e}")

            # 记录调用历史
            call_history.append(call_signature)

        # 8. 将 LLM 输出和 Observation 加入消息历史
        messages.append({"role": "assistant", "content": llm_output})
        messages.append({"role": "user", "content": f"Observation: {observation}"})

        # 9. 记录本步的思考、工具调用与观察结果
        traces.append({
            "step": step,
            "thought": parsed["thought"],
            "action": parsed["action"],
            "action_input": parsed["action_input"],
            "observation": observation,
        })

    # 达到最大步数
    elapsed = time.time() - start_time
    print(f"\n⚠️ 达到最大步数 {max_steps}，任务未完成")
    print("=" * 60)
    return {
        "success": False,
        "answer": f"达到最大步数 {max_steps}，未能完成任务",
        "steps": max_steps,
        "elapsed_time": elapsed,
        "traces": traces
    }


# ========== 4. 测试运行 ==========

def print_trace_summary(result: dict):
    """打印 ReAct 每一步的思考与工具调用过程"""
    traces = result.get("traces", [])
    if not traces:
        print("  （无过程记录）")
        return
    print("  🧠 思考与执行过程：")
    for t in traces:
        print(f"    [第{t['step']}步] Thought: {t.get('thought', '')}")
        if t.get("action"):
            print(f"              Action: {t['action']}({t.get('action_input', '')})")
            print(f"              Observation: {t.get('observation', '')}")
        if t.get("final_answer"):
            print(f"              Final Answer: {t['final_answer']}")


if __name__ == "__main__":
    print("\n" + "🚀" * 30)
    print("测试1: 数学计算")
    print("🚀" * 30 + "\n")
    result1 = run_react_agent(
        question="帮我计算 (128+72)*3 的结果",
        max_steps=10,
        timeout_seconds=30
    )

    print("\n\n")
    print("🚀" * 30)
    print("测试2: 信息查询")
    print("🚀" * 30 + "\n")
    result2 = run_react_agent(
        question="合肥今天天气怎么样？",
        max_steps=10,
        timeout_seconds=30
    )

    print("\n\n")
    print("=" * 60)
    print("📊 测试结果汇总")
    print("=" * 60)
    print(f"测试1 - 成功: {result1['success']}, 答案: {result1['answer']}")
    print_trace_summary(result1)
    print("-" * 60)
    print(f"测试2 - 成功: {result2['success']}, 答案: {result2['answer']}")
    print_trace_summary(result2)
