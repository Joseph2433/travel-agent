"""暴露给 LLM 的工具集（LangChain @tool）。docstring 就是给模型看的说明书。

设计原则：工具只提供真实/估算数据，不替模型做决策；
所有数字（距离/票价/预算）都由代码算出，模型负责理解与取舍。
"""
import json

from langchain_core.tools import tool

import apis
import geo
import tools as T


def _j(x):
    return json.dumps(x, ensure_ascii=False, default=str)


def _city(name):
    return next((c for c in T.load_cities() if c["name"] == name.rstrip("市")), None)


@tool
def scan_destinations(origin_city: str, budget: int, days: int,
                      transport: str = "auto", prefs: list = None):
    """扫描知识库中全部候选目的地。返回每个城市的：距离km、推荐交通方式、
    单程耗时h、单程费用、预估总花费、是否在预算内、标签、佳季。
    transport: auto(智能)|train|flight|drive；prefs: 偏好标签如["美食","人文"]。"""
    origin = _city(origin_city)
    if not origin:                                  # 非知识库城市 → 高德地理编码兜底
        g = apis.geocode(origin_city)
        if g and g.get("lat"):
            origin = {"name": origin_city.rstrip("市"), "province": g.get("city") or "",
                      "lat": g["lat"], "lng": g["lng"]}
        else:
            return _j({"error": f"出发城市「{origin_city}」无法定位"})
    out = T.scan_destinations(None, origin, budget, days,
                              transport or "auto", prefs or [])
    cities = {c["name"]: c for c in T.load_cities()}
    for s in out:
        if s.get("feasible"):
            c = cities[s["city"]]
            s["tags"] = c["tags"]
            s["bestMonths"] = c["bestMonths"]
    return _j(out)


@tool
def get_city_profile(city: str):
    """查看某城市画像：标签、佳季、酒店/餐饮消费档、全部景点(名称/简介/
    建议时长h/门票/推荐度0-5)、特色美食、实用贴士。"""
    c = _city(city)
    if not c:
        return _j({"error": f"知识库没有「{city}」"})
    return _j({k: c[k] for k in
               ("name", "province", "tags", "bestMonths", "hotelPerNight",
                "foodPerDay", "attractions", "foods", "tips")})


@tool
def get_city_intel(city: str):
    """获取某城市实时情报：天气实况、高德热门景点/美食POI、网络攻略摘要。"""
    name = city.rstrip("市")
    out = {"city": name, "weather": apis.weather_live(name)}
    ps = apis.poi_search(name, "景点", "110000", 6)
    if ps and ps["pois"]:
        out["pois_scenic"] = [p["name"] for p in ps["pois"]]
    pf = apis.poi_search(name, "特色美食", "050000", 6)
    if pf and pf["pois"]:
        out["pois_food"] = [p["name"] for p in pf["pois"]]
    w = apis.web_search_snippets(f"{name}旅游攻略 必去景点 美食", 3)
    if w:
        out["web"] = [{"title": x["title"],
                       "snippet": (x.get("snippet") or "")[:150]} for x in w]
    return _j(out)


@tool
def query_trains(from_city: str, to_city: str):
    """查询12306往返真实车次（预售期第7天）。返回去程 outbound 与回程 return
    两个方向各自的车次列表：车次/出发站到达站/发时到时/历时h/余票/二等座一等座票价。"""
    def leg(a, b):
        d = apis.query_trains(a, b)
        if not d:
            return None
        rows = [{"code": t["code"], "kind": t["kind"], "from": t["from"],
                 "to": t["to"], "dep": t["dep"], "arr": t["arr"],
                 "hours": t["hours"], "seats": t["seats"]}
                for t in d["trains"][:5]]
        for row in rows[:2]:                        # 只补前2班票价，控制耗时
            t = next(x for x in d["trains"] if x["code"] == row["code"])
            p = apis.train_price(t) or {}
            row["二等座"], row["一等座"] = p.get("二等座"), p.get("一等座")
        return {"date": d["date"], "trains": rows}
    out = {"outbound": leg(from_city, to_city), "return": leg(to_city, from_city)}
    if not out["outbound"] and not out["return"]:
        return _j({"error": "查无车次或接口不可用"})
    return _j(out)


@tool
def estimate_transport(from_city: str, to_city: str, mode: str = "auto"):
    """估算两城市间交通：直线km、推荐方式、单程耗时h、单程费用、可行性。
    mode: auto|train|flight|drive。"""
    a, b = _city(from_city), _city(to_city)
    if not a or not b:
        return _j({"error": "城市不在知识库"})
    km = geo.haversine_km(a["lat"], a["lng"], b["lat"], b["lng"])
    est = geo.estimate_transport(km, mode or "auto", b)
    return _j({"km": km, **est})


@tool
def calc_budget(city: str, days: int, transport_one_way: float,
                hotel_factor: float = 1.0, food_factor: float = 1.0,
                tickets: float = 0):
    """计算行程预算拆解（单人）：往返交通×2 + 住宿(晚数=天数-1) + 餐饮 + 门票 + 市内。
    hotel_factor/food_factor 调节档位（0.8穷游 1.0标准 1.3+舒适），tickets 为门票合计。"""
    c = _city(city)
    if not c:
        return _j({"error": f"未知城市「{city}」"})
    return _j(geo.trip_budget(c, days, transport_one_way,
                              hotel_factor=hotel_factor, food_factor=food_factor,
                              attraction_ticket_sum=tickets))


@tool
def submit_result(payload: dict):
    """【终止工具】调研完成后必须调用它提交最终结论，payload 为完整 JSON 对象，
    字段结构以系统指令中的输出格式为准。调用后任务即结束。"""
    return "已收到，任务结束"


REC_TOOLS = [scan_destinations, get_city_profile, submit_result]
PLAN_TOOLS = [get_city_profile, get_city_intel, query_trains,
              estimate_transport, calc_budget, submit_result]
