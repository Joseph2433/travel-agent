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
import llm
import tools

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FRONTEND = os.path.join(BASE, "frontend")

app = FastAPI(title="旅图 · TravelAgent")


class RecommendReq(BaseModel):
    lat: float | None = None
    lng: float | None = None
    city: str | None = None
    budget: int = 3000
    days: int = 3
    transport: str = "auto"
    prefs: list[str] = []


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
