"""访问监控：JSONL 日志落盘 + 管理员查询。

- 存储：backend/data/monitor/{access,gen}-YYYYMMDD.jsonl（已 gitignore）
  · access  每个 HTTP 请求一行：时间/IP/方法/路径/状态码/耗时/UA/登录用户
  · gen     每次 Agent 生成一行：类型/参数摘要/结果摘要/耗时/成败/IP/用户
- 真实 IP：优先 X-Forwarded-For / X-Real-IP——部署在 Render 等反代后面时
  request.client.host 拿到的是代理 IP，必须读转发头
- 所有写盘均 try 容错：日志出问题绝不能拖垮业务请求
"""
import glob
import json
import os
import threading
import time
from datetime import datetime

DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   "data", "monitor")
_lock = threading.Lock()

# Render 免费档无持久磁盘，实例重启/重新部署后 data/ 会清空；
# 同时把每行日志镜像到 stdout，Render 控制台 → Logs 里仍可查到。
# 本地想关：MONITOR_STDOUT=0；非 Render 环境想开：MONITOR_STDOUT=1
_STDOUT = (os.environ.get("MONITOR_STDOUT", "").strip().lower()
           in ("1", "true", "yes", "on") or bool(os.environ.get("RENDER")))


def client_ip(request):
    """真实客户端 IP：X-Forwarded-For 首个 → X-Real-IP → 直连对端。"""
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    rip = request.headers.get("x-real-ip", "")
    if rip:
        return rip.strip()
    return request.client.host if request.client else "-"


def _append(kind, rec):
    """往 backend/data/monitor/{kind}-YYYYMMDD.jsonl 追加一行。"""
    try:
        os.makedirs(DIR, exist_ok=True)
        day = datetime.now().strftime("%Y%m%d")
        path = os.path.join(DIR, f"{kind}-{day}.jsonl")
        line = json.dumps(rec, ensure_ascii=False)
        with _lock:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        if _STDOUT:
            print(f"[monitor:{kind}] {line}", flush=True)
    except Exception:
        pass


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ------------------------------------------------------------ 访问日志 ----

def log_access(request, status, ms):
    rec = {"t": _now(), "ip": client_ip(request),
           "m": request.method, "p": request.url.path,
           "st": status, "ms": round(ms, 1),
           "ua": (request.headers.get("user-agent") or "")[:160],
           "ref": (request.headers.get("referer") or "")[:200]}
    u = getattr(request.state, "user", None)
    if u:
        rec["user"] = u["name"]
    _append("access", rec)


# ------------------------------------------------------------ 生成日志 ----

def req_summary(kind, req):
    """从请求体里挑出监控关心的字段。"""
    s = {"kind": kind}
    if not isinstance(req, dict):
        return s
    for k in ("city", "lat", "lng", "budget", "days", "date", "transport",
              "prefs", "provinces", "dest"):
        v = req.get(k)
        if v not in (None, "", []):
            s[k] = v
    return s


def result_summary(kind, data):
    """从最终结果里挑出结果摘要（推荐了几个目的地/生成了几套方案）。"""
    s = {}
    if not isinstance(data, dict):
        return s
    if kind == "recommend":
        ds = data.get("destinations") or []
        s["n_dest"] = len(ds)
        s["top"] = [d.get("city") or d.get("name") for d in ds[:5]
                    if isinstance(d, dict)]
        if data.get("origin"):
            s["origin"] = data["origin"].get("name")
    elif kind == "plan":
        s["dest"] = data.get("dest")
        s["n_plan"] = len(data.get("plans") or [])
        s["mode"] = data.get("resolved_mode")
        if data.get("origin"):
            s["origin"] = data["origin"].get("name")
    elif kind == "scope":
        s["total"] = data.get("total")
        s["n_prov"] = len(data.get("provinces") or [])
        if data.get("origin"):
            s["origin"] = data["origin"].get("name")
    return s


def log_gen(request, kind, req, ms, ok=True, data=None, error=None):
    rec = {"t": _now(), "ip": client_ip(request), "ms": round(ms, 1),
           "ok": bool(ok), **req_summary(kind, req)}
    u = getattr(request.state, "user", None)
    if u:
        rec["user"] = u["name"]
    if ok:
        rec["result"] = result_summary(kind, data)
    else:
        rec["error"] = str(error)[:300]
    _append("gen", rec)


def wrap_stream(request, kind, req, gen):
    """包住 SSE 生成器：透传所有事件，解析 done/error 后写生成日志。
    客户端中途断开（GeneratorExit）记为 aborted。"""
    t0 = time.time()
    done = False
    try:
        for chunk in gen:
            # 每个 chunk 是一条完整 "data: {...}\n\n"
            try:
                ev = json.loads(chunk[chunk.index(":") + 1:].strip())
            except Exception:
                ev = None
            if isinstance(ev, dict) and ev.get("type") in ("done", "error"):
                done = True
                if ev["type"] == "done":
                    log_gen(request, kind, req, (time.time() - t0) * 1000,
                            ok=True, data=ev.get("data"))
                else:
                    log_gen(request, kind, req, (time.time() - t0) * 1000,
                            ok=False, error=ev.get("message") or "error")
            yield chunk
    except GeneratorExit:
        if not done:
            log_gen(request, kind, req, (time.time() - t0) * 1000,
                    ok=False, error="aborted: 客户端断开")
        raise
    except Exception as e:
        if not done:
            log_gen(request, kind, req, (time.time() - t0) * 1000,
                    ok=False, error=e)
        raise


# ------------------------------------------------------------ 查询/统计 ----

def _read_file(path):
    try:
        with open(path, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if ln:
                    try:
                        yield json.loads(ln)
                    except Exception:
                        pass
    except OSError:
        return


def _files(kind, days=None):
    """kind 的全部日志文件按日期升序；days 限制最近 N 天。"""
    fs = sorted(glob.glob(os.path.join(DIR, f"{kind}-*.jsonl")))
    if days:
        fs = fs[-days:]
    return fs


def recent(kind, n=200, days=7):
    """最近 n 条（新的在前）。"""
    out = []
    for path in reversed(_files(kind, days)):
        rows = list(_read_file(path))
        out[:0] = rows            # 整天插入到前面，保持时间升序再统一反转
        if len(out) >= n:
            break
    return list(reversed(out[-n:]))


def overview(days=7):
    """汇总：每日请求数/独立IP/生成次数/失败数，Top IP，今日明细。"""
    per_day, ips, top_ip = {}, {}, {}
    gen_ok = gen_fail = 0
    for path in _files("access", days):
        day = os.path.basename(path)[7:15]
        d = per_day.setdefault(day, {"req": 0, "err": 0})
        for r in _read_file(path):
            d["req"] += 1
            if r.get("st", 0) >= 400:
                d["err"] += 1
            ip = r.get("ip", "-")
            ips[day] = ips.get(day, set()) | {ip}
            top_ip[ip] = top_ip.get(ip, 0) + 1
    for path in _files("gen", days):
        day = os.path.basename(path)[4:12]
        d = per_day.setdefault(day, {"req": 0, "err": 0})
        d["gen"] = d.get("gen", 0)
        for r in _read_file(path):
            d["gen"] = d.get("gen", 0) + 1
            if r.get("ok"):
                gen_ok += 1
            else:
                gen_fail += 1
    today = datetime.now().strftime("%Y%m%d")
    days_out = [{"day": k, "req": v.get("req", 0), "err": v.get("err", 0),
                 "gen": v.get("gen", 0), "ips": len(ips.get(k, set()))}
                for k, v in sorted(per_day.items())]
    return {
        "today": {"day": today, **(per_day.get(today) or
                                   {"req": 0, "err": 0, "gen": 0}),
                  "ips": len(ips.get(today, set()))},
        "gen_ok": gen_ok, "gen_fail": gen_fail,
        "days": days_out,
        "top_ips": sorted(({"ip": k, "n": v} for k, v in top_ip.items()),
                          key=lambda x: -x["n"])[:10],
    }
