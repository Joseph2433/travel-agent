"""LLM 接入配置：OpenAI 兼容协议（Kimi / DeepSeek / OpenAI 等通用）。

环境变量（也支持 .env，见 envload.py）：
  LLM_API_KEY   必填；缺省时走规则引擎流水线（agent_graph.py 中的降级分支）
  LLM_BASE_URL  默认 https://api.moonshot.cn/v1
  LLM_MODEL     默认 kimi-k2-0905-preview

实际模型调用由 agent_graph.py 中的 ChatOpenAI（LangChain）发起，
ReAct 循环与工具编排由 LangGraph 驱动。
"""
import os

LLM_API_KEY = os.environ.get("LLM_API_KEY", "").strip()
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.moonshot.cn/v1").strip()
LLM_MODEL = os.environ.get("LLM_MODEL", "kimi-k2-0905-preview").strip()

_TIMEOUT = 45          # 秒，单次调用上限（推理模型慢，放宽）
_MAX_TOKENS = 4096     # 推理模型会把额度分给 reasoning_content，需留足正文空间


def llm_available() -> bool:
    return bool(LLM_API_KEY)


def model_name() -> str:
    return LLM_MODEL
