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
                      transport: str = "auto", prefs: list = None,
                      provinces: list = None):
    """扫描全国可达目的地候选（地级市池约350城，小众城市也在内）。
    返回每城：省份/距离km/推荐交通mode/单程耗时h/单程费用cost/预估总花费
    rough_total/是否预算内fits_budget/是否知识库精选in_kb/标签tags。
    provinces: 用户圈定的省份范围如["浙江","云南"]，为空则全部可达省份。
    prefs: 偏好标签如["美食","人文"]。只返回最契合的前50条可行候选。"""
    origin = _city(origin_city)
    if not origin:                                  # 非知识库城市 → 高德地理编码兜底
        g = apis.geocode(origin_city)
        if g and g.get("lat"):
            origin = {"name": origin_city.rstrip("市"), "province": g.get("city") or "",
                      "lat": g["lat"], "lng": g["lng"]}
        else:
            return _j({"error": f"出发城市「{origin_city}」无法定位"})
    out = T.scan_destinations(None, origin, budget, days,
                              transport or "auto", prefs or [],
                              provinces=provinces)
    cities = {c["name"]: c for c in T.load_cities()}
    feas = [s for s in out if s.get("feasible")]
    feas.sort(key=lambda s: (not s["fits_budget"], s["over_ratio"], s["km"]))
    rows = [{"city": s["city"], "province": s["province"], "km": round(s["km"]),
             "mode": s["transport_est"]["mode"],
             "hours": s["transport_est"].get("hours"),
             "cost": s["transport_est"]["cost"],
             "rough_total": s["rough_total"], "fits_budget": s["fits_budget"],
             "in_kb": s["in_kb"],
             "tags": (cities.get(s["city"]) or {}).get("tags") or []}
            for s in feas[:50]]
    return _j({"total_feasible": len(feas), "returned": len(rows),
               "candidates": rows})


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
    去哪儿在售景点榜(真实挂牌票价/5A级标/评分/销量热度)、热门美食POI、
    网络攻略摘要（含小红书被索引的笔记）。POI/景点名称可作为行程候选，
    去哪儿 ticket 字段是真实票价，编排预算时优先用它而不是估算。"""
    name = city.rstrip("市")
    out = {"city": name, "weather": apis.weather_live(name),
           "forecast": apis.weather_forecast(name)}   # 近4天逐日预报[{date,day,night,hi,lo}]
    ps = _poi_rows(apis.poi_search(name, "景点", "110000", 8))
    if ps:
        out["pois_scenic"] = ps
    qs = apis.qunar_scenic(name, 10)
    if qs:
        out["scenic_qunar"] = [{"name": s["name"], "star": s["star"],
                                "score": s["score"], "ticket": s["ticket"],
                                "intro": s["intro"], "sales": s["sales"]}
                               for s in qs["scenic"]]
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
def xhs_find_notes(keyword: str, sort_by: str = "最多点赞", limit: int = 8):
    """按任意关键词搜小红书笔记（攻略深读子代理专用）：
    返回标题/作者/点赞/收藏/feed_id/xsec_token/链接。
    换角度搜索比换同义词有效：'X 旅游攻略' / 'X 3日游 路线' / 'X 美食 必吃' /
    'X 避雷 劝退' / 'X 亲子游' / 'X citywalk' 等；
    sort_by: 综合|最新|最多点赞|最多收藏（结果太旧时换"最新"）。"""
    if not apis.xhs_enabled():
        return _j({"error": "小红书数据源未配置"})
    res = apis.xhs_search_notes(keyword, sort_by=sort_by, limit=limit)
    if not res or res.get("error"):
        return _j({"error": f"搜索失败：{(res or {}).get('error') or '服务不可达'}",
                   "keyword": keyword})
    return _j({"keyword": keyword, "notes": res["notes"],
               "tip": "挑最相关/最高赞的 1-2 篇用 xhs_read_note 读正文与热评"})


@tool
def xhs_read_note(feed_id: str, xsec_token: str = "", title: str = ""):
    """读取指定小红书笔记的正文摘录与热门评论（深挖店名/路线细节/避雷用）。
    feed_id/xsec_token 取自 xhs_find_notes 返回；title 填笔记标题便于对照。"""
    d = apis.xhs_feed_detail(feed_id, xsec_token, desc_len=900)
    if not d:
        return _j({"error": "笔记详情读取失败", "feed_id": feed_id})
    return _j({"feed_id": feed_id, "title": d.get("title") or title,
               "desc": d.get("desc"), "likes": d.get("likes"),
               "hot_comments": d.get("hot_comments") or []})


@tool
def travel_trends(boards: list = None, count: int = 10):
    """拉取各平台实时热搜榜（微博/知乎/抖音/小红书/头条），免登录免费。
    用于发现正在爆的目的地/网红玩法/文旅热点（如"XX麻辣烫""XX草原"），
    让推荐与行程紧跟潮流。返回 {board: [{rank,title,hot,url}]}。"""
    out = {}
    for b in (boards or list(apis.HOT_BOARDS)):
        rows = apis.hot_board(b, count)
        if rows:
            out[b] = rows
    if not out:
        return _j({"error": "热榜接口暂不可用"})
    return _j({"boards": out,
               "tip": "筛出与目的地/玩法/节庆相关的条目；命中候选城市时"
                      "可在推荐理由中标注'近期热议'"})


@tool
def query_trains(from_city: str, to_city: str,
                 dep_date: str = "", ret_date: str = ""):
    """查询12306往返真实车次余票。dep_date/ret_date 传 YYYY-MM-DD
    （去程=出发日，回程=出发日+行程天数-1）；留空默认预售期内第7天。
    超出12306预售期（约15天）查不到就改用 estimate_transport 并在结论中说明。
    返回去程 outbound 与回程 return 各自车次列表：车次/出发站到达站/
    发时到时/历时h/余票/二等座一等座票价。"""
    def leg(a, b, dt):
        d = apis.query_trains(a, b, dt or None)
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
    out = {"outbound": leg(from_city, to_city, dep_date),
           "return": leg(to_city, from_city, ret_date)}
    if not out["outbound"] and not out["return"]:
        return _j({"error": "查无车次或接口不可用"})
    return _j(out)


@tool
def estimate_transport(from_city: str, to_city: str, mode: str = "auto"):
    """估算两城市间交通：直线km、推荐方式、单程耗时h、单程费用、可行性。
    mode: auto|train|flight|drive。"""
    a, b = T.get_city(from_city), T.get_city(to_city)
    if not a or not b:
        return _j({"error": "城市无法定位（不在知识库且地理编码失败）"})
    km = geo.haversine_km(a["lat"], a["lng"], b["lat"], b["lng"])
    est = geo.estimate_transport(km, mode or "auto", b)
    return _j({"km": km, **est})


@tool
def calc_budget(city: str, days: int, transport_one_way: float,
                hotel_factor: float = 1.0, food_factor: float = 1.0,
                tickets: float = 0):
    """计算行程预算拆解（单人）：往返交通×2 + 住宿(晚数=天数-1) + 餐饮 + 门票 + 市内。
    hotel_factor/food_factor 调节档位（0.8穷游 1.0标准 1.3+舒适），tickets 为门票合计
    ——优先把 get_city_intel 返回的 scenic_qunar 真实票价相加传入，不要估算。"""
    c = T.get_city(city)
    if not c:
        return _j({"error": f"城市「{city}」无法定位"})
    return _j(geo.trip_budget(c, days, transport_one_way,
                              hotel_factor=hotel_factor, food_factor=food_factor,
                              attraction_ticket_sum=tickets))


@tool
def submit_result(payload: dict):
    """【终止工具】调研完成后必须调用它提交最终结论，payload 为完整 JSON 对象，
    字段结构以系统指令中的输出格式为准。调用后任务即结束。"""
    return "已收到，任务结束"


REC_TOOLS = [scan_destinations, get_city_profile, get_city_intel,
             travel_trends, submit_result]
PLAN_TOOLS = [get_city_profile, get_city_intel, search_pois, search_xhs_notes,
              travel_trends, query_trains, estimate_transport, calc_budget,
              submit_result]
# 攻略深读子代理：只挖小红书 + 核实POI，不碰编排/预算工具
DIVE_TOOLS = [xhs_find_notes, xhs_read_note, search_pois, submit_result]
