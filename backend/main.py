"""FastAPI 入口：API 路由 + 前端静态文件托管。"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

import envload                     # noqa: F401  必须先于 apis/llm 加载 .env
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from agent import TravelAgent
import agent
import apis
import auth
import llm
import tools

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
    transport: str = "auto"
    prefs: list[str] = []
    provinces: list[str] = []          # 用户圈定的出行范围（省份名），空=全部可达


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
                "logged_in": (xhs or {}).get("logged_in")},
        "cities": len(tools.load_cities()),
        "modes": list(__import__("geo").TRANSPORT_MODES.items()),
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
        lst = [{"name": c["name"], "lat": c["lat"], "lng": c["lng"]}
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
    ip = request.client.host if request.client else None
    origin = tools.resolve_origin(agent, lat=req.lat, lng=req.lng,
                                  city=req.city, client_ip=ip)
    return origin


@app.post("/api/agent/scope")
def scope(req: RecommendReq, request: Request):
    """可达范围预览：按出行方式+天数+预算扫描全国地级市池，
    返回各省份的可行城市数/最快单程耗时/最低单程价，供前端圈范围。"""
    ip = request.client.host if request.client else None
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
    return {"origin": {"name": origin["name"], "lat": origin["lat"],
                       "lng": origin["lng"]},
            "total": sum(p["n"] for p in provs), "provinces": provs}


@app.post("/api/agent/destinations")
def destinations(req: RecommendReq, request: Request):
    agent = TravelAgent()
    ip = request.client.host if request.client else None
    return agent.run_recommend(req.model_dump(), client_ip=ip)


@app.post("/api/agent/plans")
def plans(req: PlanReq, request: Request):
    agent = TravelAgent()
    ip = request.client.host if request.client else None
    return agent.run_plan(req.model_dump(), client_ip=ip)


# ---- SSE 流式端点：逐步推送 Agent 思考/工具调用，done 事件携带完整结果 ----

@app.post("/api/agent/destinations/stream")
def destinations_stream(req: RecommendReq, request: Request):
    ip = request.client.host if request.client else None
    return StreamingResponse(
        agent.stream_recommend(req.model_dump(), client_ip=ip),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/agent/plans/stream")
def plans_stream(req: PlanReq, request: Request):
    ip = request.client.host if request.client else None
    return StreamingResponse(
        agent.stream_plan(req.model_dump(), client_ip=ip),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.exception_handler(Exception)
def err(request: Request, e: Exception):
    return JSONResponse({"error": str(e)}, status_code=500)


app.mount("/", StaticFiles(directory=FRONTEND, html=True), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=False)
