"""列出某 LLM 渠道的可用模型：GET {base_url}/models。

用法（在 backend/ 目录下）：
    python list_models.py            # 用当前生效渠道（LLM_CHANNEL 指向的）
    python list_models.py xpeach     # 指定渠道名（对应 LLM_CHANNELS 里的 name）
    python list_models.py xpeach --raw   # 打印原始 JSON

key 从 .env / 环境变量读，渠道定义见 llm.py。
"""
import json
import sys

import envload                      # noqa: F401  先于 llm 加载 .env
import requests

import llm


def main():
    name = sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("-") \
        else llm.LLM_CHANNEL
    ch = llm.CHANNELS.get(name)
    if not ch:
        sys.exit(f"未知渠道「{name}」，已注册：{'、'.join(llm.CHANNELS)}")
    if not ch["api_key"]:
        sys.exit(f"渠道「{name}」没配 key（LLM_CHANNELS 里的 key 字段）")

    url = ch["base_url"].rstrip("/") + "/models"
    try:
        r = requests.get(url, headers={"Authorization": f"Bearer {ch['api_key']}"},
                         timeout=20)
    except requests.RequestException as e:
        sys.exit(f"请求失败：{e}")
    if r.status_code != 200:
        sys.exit(f"HTTP {r.status_code}：{r.text[:300]}")

    data = r.json()
    if "--raw" in sys.argv:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return
    ids = sorted(str(m.get("id", "")) for m in data.get("data") or [])
    print(f"渠道「{name}」 {url} 共 {len(ids)} 个模型：")
    for i in ids:
        print(" ", i)


if __name__ == "__main__":
    main()
