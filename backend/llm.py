"""LLM 接入层：OpenAI 兼容协议（Kimi / DeepSeek / OpenAI 等通用）。

环境变量：
  LLM_API_KEY   必填；缺省时所有 LLM 能力静默降级为内置规则引擎
  LLM_BASE_URL  默认 https://api.moonshot.cn/v1
  LLM_MODEL     默认 kimi-k2-0905-preview

约定：任何调用失败一律返回 None，由调用方降级，绝不向上抛异常。
"""
import json
import os
import re

from openai import OpenAI

LLM_API_KEY = os.environ.get("LLM_API_KEY", "").strip()
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.moonshot.cn/v1").strip()
LLM_MODEL = os.environ.get("LLM_MODEL", "kimi-k2-0905-preview").strip()

_TIMEOUT = 45          # 秒，单次调用上限（推理模型慢，放宽）
_MAX_TOKENS = 4096     # 推理模型会把额度分给 reasoning_content，需留足正文空间

_client = None


def llm_available() -> bool:
    return bool(LLM_API_KEY)


def model_name() -> str:
    return LLM_MODEL


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL,
                         timeout=_TIMEOUT, max_retries=1)
    return _client


def chat_text(system: str, user: str, temperature: float = 0.4):
    """普通对话，返回文本或 None。"""
    if not llm_available():
        return None
    try:
        r = _get_client().chat.completions.create(
            model=LLM_MODEL, temperature=temperature, max_tokens=_MAX_TOKENS,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}])
        return (r.choices[0].message.content or "").strip() or None
    except Exception:
        return None


def chat_json(system: str, user: str, temperature: float = 0.2):
    """要求模型只输出 JSON 对象；解析失败返回 None。"""
    txt = chat_text(system + "\n【硬性要求】只输出一个 JSON 对象，不要 markdown 代码块，"
                           "不要任何解释文字。", user, temperature)
    if not txt:
        return None
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", txt, re.S)
    if m:
        txt = m.group(1)
    i, j = txt.find("{"), txt.rfind("}")
    if i < 0 or j <= i:
        return None
    try:
        d = json.loads(txt[i:j + 1])
        return d if isinstance(d, dict) else None
    except Exception:
        return None
