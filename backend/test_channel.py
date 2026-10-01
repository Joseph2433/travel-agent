"""冒烟测试某渠道的模型：连通性 + function calling。

用法（在 backend/ 目录下）：
    python test_channel.py xpeach minimaxai/minimax-m3 z-ai/glm-5.2
    python test_channel.py            # 测当前生效渠道的 model
"""
import sys
import time

import envload                      # noqa: F401  先于 llm 加载 .env

import llm


def get_weather(city: str) -> str:
    """查询城市实时天气"""
    return f"{city} 晴 25°C"


def test(name, model):
    from langchain_openai import ChatOpenAI
    ch = dict(llm.CHANNELS[name])
    kw = {"use_responses_api": True} if ch["api_format"] == "responses" else {}
    if ch["temperature"] is not None:
        kw["temperature"] = ch["temperature"]
    m = ChatOpenAI(model=model, api_key=ch["api_key"],
                   base_url=ch["base_url"].rstrip("/"),
                   timeout=llm._TIMEOUT, max_tokens=llm._MAX_TOKENS, **kw)

    t0 = time.time()
    r = m.invoke("用一句话介绍北京")
    dt = time.time() - t0
    txt = r.content if isinstance(r.content, str) else str(r.content)
    print(f"[{model}] 对话 OK（{dt:.1f}s）：{' '.join(txt.split())[:60]}")

    t0 = time.time()
    r2 = m.bind_tools([get_weather]).invoke("查一下上海天气")
    dt = time.time() - t0
    calls = getattr(r2, "tool_calls", None) or []
    if calls:
        print(f"[{model}] 工具调用 OK（{dt:.1f}s）："
              f"{calls[0]['name']}({calls[0].get('args')})")
    else:
        print(f"[{model}] 工具调用失败：模型没发起 tool_call，"
              f"回了 {' '.join(str(r2.content).split())[:60]!r}")


def main():
    argv = sys.argv[1:]
    name = argv[0] if argv and argv[0] in llm.CHANNELS else llm.LLM_CHANNEL
    models = argv[1:] if argv and argv[0] in llm.CHANNELS else argv
    if not models:
        models = [llm.CHANNELS[name]["model"]]
    ch = llm.CHANNELS.get(name)
    if not ch or not ch["api_key"]:
        sys.exit(f"渠道「{name}」不存在或没配 key")
    print(f"渠道「{name}」 {ch['base_url']} format={ch['api_format']}")
    for model in models:
        try:
            test(name, model)
        except Exception as e:
            print(f"[{model}] 调用失败：{type(e).__name__} {str(e)[:200]}")
        print()


if __name__ == "__main__":
    main()
