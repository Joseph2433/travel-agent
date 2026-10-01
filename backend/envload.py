"""极简 .env 加载器（零依赖）：读取项目根目录 .env 注入 os.environ。

规则：
  - 已有真实环境变量优先（setdefault，.env 不覆盖外部环境）
  - 支持 KEY=VALUE、可选 export 前缀、单/双引号、行尾注释、空行与 # 注释行
  - 必须在 apis.py / llm.py 之前 import——它们在模块导入时读环境变量
"""
import os

_ENV_PATH = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), ".env")


def load(path: str = _ENV_PATH) -> int:
    """加载 .env，返回注入的变量数。文件不存在/解析失败静默跳过。"""
    n = 0
    try:
        with open(path, encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[7:].lstrip()
                if "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k, v = k.strip(), v.strip()
                if not k or k in os.environ:
                    continue
                if v and v[0] in "\"'":           # 引号值：取到收尾引号为止
                    end = v.find(v[0], 1)
                    v = v[1:end] if end > 0 else v.strip("\"'")
                elif " #" in v:                    # 无引号值 → 行尾注释
                    v = v[:v.index(" #")].rstrip()
                os.environ[k] = v
                n += 1
    except OSError:
        pass
    return n


load()
