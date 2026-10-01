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
        return _j({"error": f"知识库没有「{city}」的画像，请改用 get_city_intel "
                            f"和 search_pois 获取实时POI数据来编排"})
    return _j({k: c[k] for k in
               ("name", "province", "tags", "bestMonths", "hotelPerNight",
                "foodPerDay", "attractions", "foods", "tips")})


def _poi_rows(ps):
    return [{"name": p["name"], "rating": p.get("rating"),
             "cost": p.get("cost"), "addr": (p.get("addr") or "")[:40]}
            for p in (ps or {}).get("pois") or []]


@tool
def get_city_intel(city: str):
    """获取某城市实时情报：天气实况、高德热门景点POI(评分/参考价/地址)、
    热门美食POI、网络攻略摘要（含小红书被索引的笔记）。POI 名称可作为行程的
    景点/美食候选。"""
    name = city.rstrip("市")
    out = {"city": name, "weather": apis.weather_live(name)}
    ps = _poi_rows(apis.poi_search(name, "景点", "110000", 8))
    if ps:
        out["pois_scenic"] = ps
    pf = _poi_rows(apis.poi_search(name, "特色美食", "050000", 8))
    if pf:
        out["pois_food"] = pf
    web = []
    for i in apis.web_search_snippets(f"{name}旅游攻略 必去景点 美食", 3) or []:
        web.append({"title": i["title"], "snippet": (i.get("snippet") or "")[:150],
                    "url": i.get("url") or "", "src": "web"})
    for i in apis.web_search_snippets(f"site:xiaohongshu.com {name} 旅游", 3) or []:
        if "xiaohongshu.com" not in (i.get("url") or ""):
            continue                                # 搜索引擎不保证按站点过滤，手动核
        web.append({"title": i["title"], "snippet": (i.get("snippet") or "")[:150],
                    "url": i["url"], "src": "xhs-web"})
    if web:
        out["web"] = web
    return _j(out)


@tool
def search_pois(city: str, keywords: str, types: str = "", count: int = 8):
    """按关键词在某城市搜索高德POI，返回名称/评分/参考价/地址。
    用于补充特定类别：如 keywords="博物馆"/"夜市"/"古镇"/"亲子乐园"，
    types 可给高德分类码(110000风景 050000美食 060100购物)，默认不限类别。"""
    ps = apis.poi_search(city.rstrip("市"), keywords, types, count)
    rows = _poi_rows(ps)
    if not rows:
        return _j({"error": "未找到相关POI", "pois": []})
    return _j({"pois": rows})


@tool
def search_xhs_notes(city: str, topic: str = "旅游攻略",
                     sort_by: str = "最多点赞", with_detail: bool = False):
    """搜索小红书真实攻略笔记作编排参考：返回标题/作者/点赞/收藏/笔记链接；
    with_detail=True 时附前2篇的正文摘录与热门评论（更慢但更具体，适合深挖
    美食店名、避雷提示、路线细节）。topic 可自定义如 '美食'/'避雷'/'两日游路线'；
    sort_by: 综合|最新|最多点赞|最多收藏。需本地运行 xiaohongshu-mcp 服务。"""
    if not apis.xhs_enabled():
        return _j({"error": "小红书数据源未配置（本地部署 xiaohongshu-mcp 并设 "
                            "XHS_API_BASE 后可用）。请改用 get_city_intel 的网络"
                            "攻略摘要与 search_pois 获取参考信息"})
    st = apis.xhs_login_status()
    if st and st.get("logged_in") is False:
        return _j({"error": "xiaohongshu-mcp 在线但未登录小红书，需先运行其登录工具扫码"})
    kw = f"{city.rstrip('市')} {topic}".strip()
    res = apis.xhs_search_notes(kw, sort_by=sort_by, limit=6)
    if not res or res.get("error"):
        return _j({"error": f"小红书搜索失败：{(res or {}).get('error') or '服务不可达'}"})
    notes = res["notes"]
    if not notes:
        return _j({"keyword": kw, "notes": [], "tip": "未搜到相关笔记，换个 topic 试试"})
    if with_detail:
        for n in notes[:2]:
            d = apis.xhs_feed_detail(n.get("feed_id"), n.get("xsec_token"))
            if d:
                n["excerpt"], n["hot_comments"] = d["desc"], d["hot_comments"]
    return _j({"keyword": kw, "notes": notes,
               "tip": "笔记里的店名/景点可先用 search_pois 核实真实存在后再编入行程"})


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
PLAN_TOOLS = [get_city_profile, get_city_intel, search_pois, search_xhs_notes,
              query_trains, estimate_transport, calc_budget, submit_result]
