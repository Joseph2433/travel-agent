"""FastAPI 入口：API 路由 + 前端静态文件托管。"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

import envload                     # noqa: F401  必须先于 apis/llm 加载 .env
import time
from fastapi import Body, FastAPI, Request, WebSocket
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from agent import TravelAgent
import agent
import apis
import auth
import llm
import monitor
import tools
import xhs_relay

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FRONTEND = os.path.join(BASE, "frontend")

app = FastAPI(title="旅图 · TravelAgent")


@app.middleware("http")
async def _auth_gate(request: Request, call_next):
    """启用账号体系后（auth.enabled()：存在用户）保护所有 /api/*，
    仅放行登录接口；预检与非 API 路径（静态页）始终通过。"""
    p = request.url.path
    if (request.method == "OPTIONS" or not p.startswith("/api/")
            or p == "/api/auth/login" or not auth.enabled()):
        return await call_next(request)
    tok = request.headers.get("authorization", "")
    tok = tok[7:].strip() if tok.lower().startswith("bearer ") else ""
    user = auth.resolve(tok)
    if not user:
        return JSONResponse({"error": "未登录或登录已过期"}, status_code=401)
    request.state.user = user
    return await call_next(request)


# 拆分部署（前端 GitHub Pages + 后端独立主机）时允许跨域；
# CORS_ORIGINS 逗号分隔，未配置则放开（无凭据请求，安全）。
# 注意：CORS 必须最后注册（最外层），否则 401 响应缺 CORS 头、预检被拦
from fastapi.middleware.cors import CORSMiddleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in
                   os.getenv("CORS_ORIGINS", "*").split(",")],
    allow_methods=["*"], allow_headers=["*"])


# 监控中间件：注册在 CORS 之后 → 最外层，能看到所有请求与登录态。
# 记录 /api/* 全部请求 + 首页 GET（页面访问）；其余静态资源不记（纯噪音）。
@app.middleware("http")
async def _monitor(request: Request, call_next):
    p = request.url.path
    watch = p.startswith("/api/") or (
        request.method == "GET" and p in ("/", "/index.html"))
    if not watch or request.method == "OPTIONS":
        return await call_next(request)
    t0 = time.time()
    try:
        resp = await call_next(request)
        status = resp.status_code
        return resp
    except Exception:
        status = 500
        raise
    finally:
        monitor.log_access(request, status, (time.time() - t0) * 1000)


# ---- 自建 x-mcp 中继：浏览器插件经 /xhs/ws 连进来，后端经 /xhs/mcp 透传调用 ----

@app.websocket("/xhs/ws")
async def xhs_ws(websocket: WebSocket):
    await xhs_relay.handle_ws(websocket)


@app.post("/xhs/mcp")
def xhs_mcp(request: Request, body: dict = Body(default={})):
    """MCP Streamable HTTP 端点（同步！call_tool 阻塞等插件回包，
    async 会卡死事件循环）。XHS_API_BASE 指向本端点即完成透传。"""
    if xhs_relay.RELAY_TOKEN:
        tok = request.headers.get("x-api-key") or ""
        auth_h = request.headers.get("authorization", "")
        auth_h = auth_h[7:].strip() if auth_h.lower().startswith("bearer ") else ""
        if xhs_relay.RELAY_TOKEN not in (tok, auth_h):
            return JSONResponse({"error": "invalid relay token"}, status_code=401)
    res = xhs_relay.mcp_dispatch(body)
    if res is None:                                  # notifications/*
        return Response(status_code=202)
    return res


# ---- 账号：登录/会话/管理员开号（无注册入口） ----

class LoginReq(BaseModel):
    user: str
    pw: str


@app.post("/api/auth/login")
def auth_login(req: LoginReq):
    if not auth.enabled():
        return {"auth": False}
    tok = auth.login(req.user, req.pw)
    if not tok:
        return JSONResponse({"error": "用户名或密码错误"}, status_code=401)
    u = auth.resolve(tok)
    return {"auth": True, "token": tok,
            "user": u["name"], "role": u["role"]}


@app.get("/api/auth/me")
def auth_me(request: Request):
    if not auth.enabled():
        return {"auth": False}
    u = getattr(request.state, "user", None)     # 中间件已拦无效 token
    return {"auth": True, "user": u["name"], "role": u["role"]}


@app.post("/api/auth/logout")
def auth_logout(request: Request):
    tok = request.headers.get("authorization", "")
    auth.logout(tok[7:].strip() if tok.lower().startswith("bearer ") else tok)
    return {"ok": True}


def _require_admin(request):
    u = getattr(request.state, "user", None)
    if not u or u["role"] != "admin":
        return None
    return u


@app.get("/api/auth/users")
def auth_users(request: Request):
    if not _require_admin(request):
        return JSONResponse({"error": "需要管理员权限"}, status_code=403)
    return {"users": auth.list_users()}


class NewUserReq(BaseModel):
    user: str
    pw: str
    role: str = "user"


@app.post("/api/auth/users")
def auth_add_user(req: NewUserReq, request: Request):
    if not _require_admin(request):
        return JSONResponse({"error": "需要管理员权限"}, status_code=403)
    name, e = auth.add_user(req.user, req.pw, req.role)
    if e:
        return JSONResponse({"error": e}, status_code=400)
    return {"ok": True, "user": name}


@app.delete("/api/auth/users/{name}")
def auth_del_user(name: str, request: Request):
    u = _require_admin(request)
    if not u:
        return JSONResponse({"error": "需要管理员权限"}, status_code=403)
    if name == u["name"]:
        return JSONResponse({"error": "不能删除当前登录的管理员"},
                            status_code=400)
    e = auth.remove_user(name)
    if e:
        return JSONResponse({"error": e}, status_code=400)
    return {"ok": True}


class RecommendReq(BaseModel):
    lat: float | None = None
    lng: float | None = None
    city: str | None = None
    budget: int = 3000
    days: int = 3
    date: str | None = None            # 出发日期 YYYY-MM-DD（可选）
    transport: str = "auto"
    style: str = "适中"               # 游玩风格：特种兵|适中|休闲随意
    prefs: list[str] = []
    provinces: list[str] = []          # 用户圈定的出行范围（省份名），空=全部可达
    local: bool = False                # 本地游：本城+周边≤160km，1-2天近游


class PlanReq(RecommendReq):
    dest: str


@app.get("/api/status")
def status():
    xhs = apis.xhs_login_status() if apis.xhs_enabled() else None
    return {
        "amap": apis.amap_available(),
        "llm": llm.llm_available(),
        "llm_model": llm.model_name() if llm.llm_available() else None,
        "xhs": {"enabled": apis.xhs_enabled(),
                "logged_in": (xhs or {}).get("logged_in"),
                "relay_online": xhs_relay.online()},
        "cities": len(tools.load_cities()),
        "modes": list(__import__("geo").TRANSPORT_MODES.items()),
        "local_modes": list(__import__("geo").LOCAL_MODES.items()),
    }


@app.get("/api/cities")
def cities():
    return [{"name": c["name"], "province": c["province"],
             "lat": c["lat"], "lng": c["lng"], "tags": c["tags"]}
            for c in tools.load_cities()]


@app.get("/api/geo/provinces")
def provinces():
    return {"provinces": apis.PROVINCES}


@app.get("/api/geo/cities")
def geo_cities(province: str):
    """某省的城市列表：优先高德行政区接口，无key时降级知识库同省城市。"""
    lst = apis.district_cities(province)
    if not lst:
        prov = province.strip().rstrip("省市自治区壮族回族维吾尔") or province
        lst = [{"name": r["name"], "lat": r.get("lat"), "lng": r.get("lng")}
               for r in (apis.all_prefecture_cities() or [])
               if (r.get("province") or "").startswith(prov[:2])] or \
              [{"name": c["name"], "lat": c["lat"], "lng": c["lng"]}
               for c in tools.load_cities()
               if c["province"].startswith(prov[:2])]
        if not lst:
            lst = [{"name": prov}]
    return {"cities": lst}


class LocateReq(BaseModel):
    lat: float | None = None
    lng: float | None = None
    city: str | None = None


@app.post("/api/locate")
def locate(req: LocateReq, request: Request):
    agent = TravelAgent()
    ip = monitor.client_ip(request)
    origin = tools.resolve_origin(agent, lat=req.lat, lng=req.lng,
                                  city=req.city, client_ip=ip)
    return origin


@app.post("/api/agent/scope")
def scope(req: RecommendReq, request: Request):
    """可达范围预览：按出行方式+天数+预算扫描全国地级市池，
    返回各省份的可行城市数/最快单程耗时/最低单程价，供前端圈范围。"""
    t0 = time.time()
    ip = monitor.client_ip(request)
    try:
        origin = tools.resolve_origin(None, lat=req.lat, lng=req.lng,
                                      city=req.city, client_ip=ip)
        scanned = tools.scan_destinations(None, origin, req.budget, req.days,
                                          req.transport or "auto",
                                          req.prefs or [])
        by_prov = {}
        for s in scanned:
            if not s.get("feasible"):
                continue
            p = by_prov.setdefault(s["province"],
                                   {"name": s["province"], "n": 0,
                                    "min_hours": 99.0, "min_cost": 99999,
                                    "n_fit": 0, "kb": []})
            p["n"] += 1
            h = s["transport_est"].get("hours") or 99.0
            p["min_hours"] = round(min(p["min_hours"], h), 1)
            p["min_cost"] = min(p["min_cost"], s["transport_est"]["cost"])
            if s["fits_budget"]:
                p["n_fit"] += 1
            if s.get("in_kb"):
                p["kb"].append(s["city"])
        provs = sorted(by_prov.values(), key=lambda x: x["min_hours"])
        out = {"origin": {"name": origin["name"], "lat": origin["lat"],
                          "lng": origin["lng"]},
               "total": sum(p["n"] for p in provs), "provinces": provs}
        monitor.log_gen(request, "scope", req.model_dump(),
                        (time.time() - t0) * 1000, ok=True, data=out)
        return out
    except Exception as e:
        monitor.log_gen(request, "scope", req.model_dump(),
                        (time.time() - t0) * 1000, ok=False, error=e)
        raise


@app.post("/api/agent/destinations")
def destinations(req: RecommendReq, request: Request):
    agent = TravelAgent()
    ip = monitor.client_ip(request)
    t0 = time.time()
    try:
        out = agent.run_recommend(req.model_dump(), client_ip=ip)
        monitor.log_gen(request, "recommend", req.model_dump(),
                        (time.time() - t0) * 1000, ok=True, data=out)
        return out
    except Exception as e:
        monitor.log_gen(request, "recommend", req.model_dump(),
                        (time.time() - t0) * 1000, ok=False, error=e)
        raise


@app.post("/api/agent/plans")
def plans(req: PlanReq, request: Request):
    agent = TravelAgent()
    ip = monitor.client_ip(request)
    t0 = time.time()
    try:
        out = agent.run_plan(req.model_dump(), client_ip=ip)
        monitor.log_gen(request, "plan", req.model_dump(),
                        (time.time() - t0) * 1000, ok=True, data=out)
        return out
    except Exception as e:
        monitor.log_gen(request, "plan", req.model_dump(),
                        (time.time() - t0) * 1000, ok=False, error=e)
        raise


# ---- SSE 流式端点：逐步推送 Agent 思考/工具调用，done 事件携带完整结果 ----

@app.post("/api/agent/destinations/stream")
def destinations_stream(req: RecommendReq, request: Request):
    ip = monitor.client_ip(request)
    body = req.model_dump()
    return StreamingResponse(
        monitor.wrap_stream(request, "recommend", body,
                            agent.stream_recommend(body, client_ip=ip)),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/agent/plans/stream")
def plans_stream(req: PlanReq, request: Request):
    ip = monitor.client_ip(request)
    body = req.model_dump()
    return StreamingResponse(
        monitor.wrap_stream(request, "plan", body,
                            agent.stream_plan(body, client_ip=ip)),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---- 监控查询（含访客 IP/UA，属于敏感信息） ----
# 启用账号体系 → 仅管理员；未启用（本地开发）→ 仅本机环回地址可访问，
# 避免公网部署且没配账号时任何人都能拉到全部访客 IP。

def _require_monitor(request):
    if auth.enabled():
        return _require_admin(request)
    return monitor.client_ip(request) in ("127.0.0.1", "::1")


@app.get("/api/monitor/overview")
def monitor_overview(request: Request):
    if not _require_monitor(request):
        return JSONResponse({"error": "需要管理员权限"}, status_code=403)
    return monitor.overview()


@app.get("/api/monitor/logs")
def monitor_logs(request: Request, kind: str = "gen", n: int = 200):
    if not _require_monitor(request):
        return JSONResponse({"error": "需要管理员权限"}, status_code=403)
    if kind not in ("gen", "access"):
        kind = "gen"
    return {"logs": monitor.recent(kind, min(max(n, 1), 1000))}


@app.exception_handler(Exception)
def err(request: Request, e: Exception):
    return JSONResponse({"error": str(e)}, status_code=500)


app.mount("/", StaticFiles(directory=FRONTEND, html=True), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=False)
