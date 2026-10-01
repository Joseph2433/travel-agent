"""账号体系：仅管理员可创建账号，无公开注册入口。

- 存储：backend/data/auth.json（已 gitignore），密码 PBKDF2-SHA256 加盐
- 会话：token 持久化在同文件，30 天有效
- 种子：环境变量 AUTH_SEED——JSON 数组 [{"user","pass","role"}]
  或简写 "admin:pass,user2:pass2"（缺省 role=admin）。
  云端 Render 文件系统是临时的：运行时添加的账号会在重启/重部署后丢失，
  长期账号请写进 AUTH_SEED；本地用 CLI：python backend/auth.py add <user> <pass> [role]
- 空用户表 = 未启用鉴权（本地开发免登录，/api/* 直接放行）
"""
import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time

STORE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "data", "auth.json")
_TOKEN_TTL = 30 * 86400                       # token 有效期 30 天
_NAME_RE = re.compile(r"^[\w.一-龥-]{2,20}$")

_lock = threading.Lock()
_mem = {"mtime": -1.0, "users": {}, "tokens": {}}


def _load():
    try:
        m = os.path.getmtime(STORE)
    except OSError:
        m = 0.0
    if m != _mem["mtime"]:
        try:
            d = json.load(open(STORE, encoding="utf-8"))
            _mem["users"] = d.get("users") or {}
            _mem["tokens"] = d.get("tokens") or {}
        except Exception:
            _mem["users"], _mem["tokens"] = {}, {}
        _mem["mtime"] = m


def _save():
    os.makedirs(os.path.dirname(STORE), exist_ok=True)
    tmp = STORE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"users": _mem["users"], "tokens": _mem["tokens"]},
                  f, ensure_ascii=False, indent=1)
    os.replace(tmp, STORE)
    _mem["mtime"] = os.path.getmtime(STORE)


def _hash(pw, salt):
    return hashlib.pbkdf2_hmac("sha256", (pw or "").encode(),
                               bytes.fromhex(salt), 120_000).hex()


def enabled():
    """空用户表 → 鉴权未启用（开发模式，全部放行）。"""
    with _lock:
        _load()
        return bool(_mem["users"])


def add_user(name, pw, role="user"):
    name = (name or "").strip()
    if not _NAME_RE.match(name):
        return None, "用户名需 2-20 位（中英文/数字/._-）"
    if not pw or len(pw) < 4:
        return None, "密码至少 4 位"
    with _lock:
        _load()
        if name in _mem["users"]:
            return None, "用户名已存在"
        salt = secrets.token_hex(16)
        _mem["users"][name] = {
            "salt": salt, "hash": _hash(pw, salt),
            "role": role if role in ("admin", "user") else "user",
            "created": int(time.time())}
        _save()
    return name, None


def remove_user(name):
    with _lock:
        _load()
        u = _mem["users"].get(name)
        if not u:
            return "用户不存在"
        admins = sum(1 for x in _mem["users"].values()
                     if x["role"] == "admin")
        if u["role"] == "admin" and admins <= 1:
            return "至少保留一个管理员账号"
        del _mem["users"][name]
        _mem["tokens"] = {t: s for t, s in _mem["tokens"].items()
                          if s.get("user") != name}
        _save()
    return None


def list_users():
    with _lock:
        _load()
        return [{"name": n, "role": u["role"], "created": u.get("created")}
                for n, u in _mem["users"].items()]


def login(name, pw):
    """校验账号密码 → 返回新 token；失败返回 None。"""
    with _lock:
        _load()
        u = _mem["users"].get((name or "").strip())
        if not u or not hmac.compare_digest(
                u["hash"], _hash(pw, u["salt"])):
            return None
        tok = secrets.token_urlsafe(32)
        _mem["tokens"] = {t: s for t, s in _mem["tokens"].items()
                          if s.get("exp", 0) > time.time()}
        _mem["tokens"][tok] = {"user": (name or "").strip(),
                               "exp": time.time() + _TOKEN_TTL}
        _save()
    return tok


def resolve(token):
    """token → {name, role}；无效/过期返回 None。"""
    if not token:
        return None
    with _lock:
        _load()
        s = _mem["tokens"].get(token)
        if not s or s.get("exp", 0) <= time.time():
            return None
        u = _mem["users"].get(s["user"])
        if not u:
            return None
        return {"name": s["user"], "role": u["role"]}


def logout(token):
    with _lock:
        _load()
        if _mem["tokens"].pop(token, None) is not None:
            _save()


def _seed():
    """启动时把 AUTH_SEED 里尚未存在的账号写入存储（幂等）。"""
    raw = os.environ.get("AUTH_SEED", "").strip()
    if not raw:
        return
    try:
        items = json.loads(raw)
        items = items if isinstance(items, list) else []
    except Exception:
        items = [{"user": p.split(":", 1)[0].strip(),
                  "pass": p.split(":", 1)[1].strip(), "role": "admin"}
                 for p in raw.split(",") if ":" in p]
    for it in items:
        name, pw = (it.get("user") or "").strip(), it.get("pass") or ""
        role = it.get("role") or "admin"
        if name and pw:
            with _lock:
                _load()
                exists = name in _mem["users"]
            if not exists:
                add_user(name, pw, role)


_load()
_seed()


if __name__ == "__main__":
    import sys
    args = sys.argv[1:]
    if args[:1] == ["add"] and len(args) >= 3:
        n, e = add_user(args[1], args[2],
                        args[3] if len(args) > 3 else "user")
        print(e or f"已创建账号 {n}")
    elif args[:1] in (["del"], ["rm"]) and len(args) >= 2:
        print(remove_user(args[1]) or "已删除")
    else:
        us = list_users()
        print("当前账号：" + (", ".join(
            f"{u['name']}({u['role']})" for u in us) or "空——未启用鉴权"))
