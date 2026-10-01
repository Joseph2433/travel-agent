"""LLM 接入配置：OpenAI 兼容协议，支持多渠道切换。

环境变量（也支持 .env，见 envload.py）：
  LLM_CHANNEL   当前生效渠道名，默认 default；渠道没配 key 时回退 default

  内置渠道 default（Chat Completions 格式，Kimi/DeepSeek/OpenAI 通用）：
    LLM_API_KEY   缺省时走规则引擎流水线（agent_graph.py 中的降级分支）
    LLM_BASE_URL  默认 https://api.moonshot.cn/v1
    LLM_MODEL     默认 kimi-k2-0905-preview

  追加渠道（JSON 数组，与 AUTH_SEED 同风格）：
    LLM_CHANNELS=[{"name":"xpeach","key":"sk-...",
                   "base_url":"https://xpeach.codes/v1",
                   "model":"gpt-5.2","format":"responses"}]
    字段：name 必填；key/base_url/model 缺省继承 default；
    format = chat（默认，/chat/completions）| responses（/responses 新协议）；
    temperature 可选——responses 渠道默认不传（推理模型拒收非默认 temperature）

实际模型调用由 agent_graph.py 中的 ChatOpenAI（LangChain）发起，
ReAct 循环与工具编排由 LangGraph 驱动。渠道在进程启动时确定，换渠道需重启。
"""
import json
import os

_TIMEOUT = 45          # 秒，单次调用上限（推理模型慢，放宽）
_MAX_TOKENS = 4096     # 推理模型会把额度分给 reasoning_content，需留足正文空间

LLM_API_KEY = os.environ.get("LLM_API_KEY", "").strip()
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.moonshot.cn/v1").strip()
LLM_MODEL = os.environ.get("LLM_MODEL", "kimi-k2-0905-preview").strip()


def _load_channels():
    chans = {"default": {"api_key": LLM_API_KEY, "base_url": LLM_BASE_URL,
                         "model": LLM_MODEL, "api_format": "chat",
                         "temperature": 0.3}}
    raw = os.environ.get("LLM_CHANNELS", "").strip()
    if raw:
        try:
            items = json.loads(raw)
            if not isinstance(items, list):
                raise ValueError
        except Exception:
            items = []
        for c in items:
            if not isinstance(c, dict) or not c.get("name"):
                continue
            base = chans["default"]
            chans[str(c["name"]).strip()] = {
                "api_key": str(c.get("key") or c.get("api_key") or "").strip(),
                "base_url": str(c.get("base_url") or base["base_url"]).strip(),
                "model": str(c.get("model") or base["model"]).strip(),
                "api_format": str(c.get("format") or "chat").strip().lower(),
                "temperature": c.get("temperature"),          # None → 不传给厂商
            }
    return chans


CHANNELS = _load_channels()
LLM_CHANNEL = os.environ.get("LLM_CHANNEL", "default").strip()


def active() -> dict:
    """当前生效渠道配置；渠道名不存在或没配 key 时回退 default。"""
    ch = CHANNELS.get(LLM_CHANNEL)
    if not ch or not ch["api_key"]:
        ch = CHANNELS["default"]
    return ch


def llm_available() -> bool:
    return bool(active()["api_key"])


def model_name() -> str:
    return active()["model"]
