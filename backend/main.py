"""FastAPI 入口：API 路由 + 前端静态文件托管。"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from agent import TravelAgent
import apis
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
    return {
        "amap": apis.amap_available(),
        "cities": len(tools.load_cities()),
        "modes": list(__import__("geo").TRANSPORT_MODES.items()),
    }


@app.get("/api/cities")
def cities():
    return [{"name": c["name"], "province": c["province"],
             "lat": c["lat"], "lng": c["lng"], "tags": c["tags"]}
            for c in tools.load_cities()]


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


@app.exception_handler(Exception)
def err(request: Request, e: Exception):
    return JSONResponse({"error": str(e)}, status_code=500)


app.mount("/", StaticFiles(directory=FRONTEND, html=True), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=False)
