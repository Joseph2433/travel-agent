"""Agent 工具集：定位解析、目的地扫描排序、情报搜索、情报判断、行程编排。"""
import json
import os
from datetime import datetime

import apis
import geo

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
_cities_cache = None


def load_cities():
    global _cities_cache
    if _cities_cache is None:
        with open(os.path.join(DATA_DIR, "cities.json"), encoding="utf-8") as f:
            _cities_cache = json.load(f)
    return _cities_cache


# ----------------------------------------------------------- 工具实现 ----

def resolve_origin(ctx, lat=None, lng=None, city=None, client_ip=None):
    """定位解析：指定城市 > 浏览器坐标 > 高德IP定位 > 默认上海。
    城市不在知识库时：用前端传来的坐标，或高德地理编码兜底。"""
    cities = load_cities()
    if city:
        for c in cities:
            if c["name"] == city.rstrip("市"):
                return {"name": c["name"], "province": c["province"],
                        "lat": c["lat"], "lng": c["lng"], "src": "manual", "note": "用户手动指定"}
        if lat is not None and lng is not None:      # 高德行政区选中的非知识库城市
            return {"name": city.rstrip("市"), "province": "",
                    "lat": float(lat), "lng": float(lng), "src": "manual",
                    "note": "用户手动指定（高德行政区）"}
        g = apis.geocode(city)
        if g:
            return {"name": g.get("city") or city, "province": "",
                    "lat": g["lat"], "lng": g["lng"], "src": "amap",
                    "note": f"高德地理编码：{g.get('formatted','')}"}
    if lat is not None and lng is not None:
        near = min(cities, key=lambda c: geo.haversine_km(lat, lng, c["lat"], c["lng"]))
        return {"name": near["name"], "province": near["province"],
                "lat": float(lat), "lng": float(lng), "src": "gps",
                "note": f"GPS坐标定位，归属最近枢纽「{near['name']}」"}
    ip_loc = apis.ip_locate(client_ip or "")
    if ip_loc and ip_loc.get("lat"):
        return {"name": ip_loc.get("city") or "未知", "province": ip_loc.get("province", ""),
                "lat": ip_loc["lat"], "lng": ip_loc["lng"], "src": "amap-ip",
                "note": "高德IP定位（精度到城市级）"}
    sh = next(c for c in cities if c["name"] == "上海")
    return {"name": sh["name"], "province": sh["province"], "lat": sh["lat"], "lng": sh["lng"],
            "src": "default", "note": "定位不可用，默认以上海为出发地"}


def scan_destinations(ctx, origin, budget, days, transport, prefs):
    """扫描全部候选目的地：距离、交通可行性、粗略预算。"""
    out = []
    for c in load_cities():
        if c["name"] == origin["name"]:
            continue
        km = geo.haversine_km(origin["lat"], origin["lng"], c["lat"], c["lng"])
        est = geo.estimate_transport(km, transport, c)
        if not est.get("feasible"):
            out.append({"city": c["name"], "feasible": False, "reason": est.get("reason"), "km": km})
            continue
        tickets = sum(a["ticket"] for a in c["attractions"] if a["must"] >= 4)
        rough = geo.trip_budget(c, days, est["cost"], attraction_ticket_sum=tickets * 0.6)
        out.append({
            "city": c["name"], "feasible": True, "km": km,
            "transport_est": est, "rough_total": rough["total"],
            "fits_budget": rough["total"] <= budget,
            "over_ratio": round(rough["total"] / max(budget, 1), 2),
        })
    return out


def rank_destinations(ctx, scanned, budget, days, prefs):
    """多因子打分：预算契合 + 天数匹配 + 季节适宜 + 偏好命中 + 距离效率。"""
    month = datetime.now().month
    cities = {c["name"]: c for c in load_cities()}
    scored = []
    for s in scanned:
        if not s["feasible"]:
            continue
        c = cities[s["city"]]
        score, reasons = 0, []
        # 预算（35）
        if s["fits_budget"]:
            score += 35; reasons.append(f"预算内（约¥{s['rough_total']}）")
        elif s["over_ratio"] <= 1.3:
            score += 15; reasons.append(f"略超预算（约¥{s['rough_total']}，可降住宿档位）")
        else:
            reasons.append(f"超预算较多（约¥{s['rough_total']}）")
        # 天数（20）：景点量与天数匹配
        need = max(2, min(6, round(len(c["attractions"]) / 2)))
        if days >= need:
            score += 20; reasons.append(f"{days}天玩得从容")
        elif days == need - 1:
            score += 12; reasons.append("行程会稍紧凑")
        else:
            score += 4; reasons.append("时间偏紧只能打卡核心")
        # 季节（15）
        if month in c["bestMonths"]:
            score += 15; reasons.append("正值佳季")
        else:
            score += 5
        # 偏好（20）
        hit = set(prefs) & set(c["tags"])
        if hit:
            score += min(20, 8 * len(hit)); reasons.append("契合偏好：" + "、".join(hit))
        # 距离效率（10）：交通耗时越短越好
        h = s["transport_est"].get("hours", 99)
        score += max(0, 10 - h)
        scored.append({
            "city": c["name"], "province": c["province"], "score": round(score),
            "km": s["km"], "lat": c["lat"], "lng": c["lng"],
            "tags": c["tags"], "reasons": reasons,
            "transport_est": s["transport_est"], "rough_total": s["rough_total"],
            "hotelPerNight": c["hotelPerNight"],
        })
    scored.sort(key=lambda x: -x["score"])
    return scored[:6]


_WINTER_MARK = ("冰雪大世界", "雪博会", "冰雪嘉年华")


def fetch_intel(ctx, city_name):
    """目的地情报搜索：高德POI + 天气 + 网页攻略 + 本地知识库，多源融合。"""
    c = next(x for x in load_cities() if x["name"] == city_name)
    intel = {"city": c, "sources": ["本地知识库"], "pois_scenic": None,
             "pois_food": None, "weather": None, "web": None}
    w = apis.weather_live(c["name"])
    if w:
        intel["weather"] = w; intel["sources"].append("高德天气")
    scenic = apis.poi_search(c["name"], "景点", "110000", 8)
    if scenic and scenic["pois"]:
        intel["pois_scenic"] = scenic["pois"]; intel["sources"].append("高德POI")
    food = apis.poi_search(c["name"], "特色美食", "050000", 8)
    if food and food["pois"]:
        intel["pois_food"] = food["pois"]
    web = apis.web_search_snippets(f"{c['name']}旅游攻略 必去景点 美食")
    if web:
        intel["web"] = web; intel["sources"].append("网页搜索")
    return intel


def judge_intel(ctx, intel, days, prefs):
    """对搜到的情报做判断：剔除不合时令/低价值项，按天数取舍，输出核心体验清单。"""
    c, month = intel["city"], datetime.now().month
    kept, dropped, notes = [], [], []
    for a in c["attractions"]:
        if any(m in a["n"] for m in _WINTER_MARK) and month not in (12, 1, 2):
            dropped.append({"name": a["n"], "reason": "冬季限定，当前未开放"})
            continue
        if a["hours"] >= 8 and days <= 2:
            dropped.append({"name": a["n"], "reason": "需一整天，短行程放不下"})
            continue
        kept.append(a)
    # 高权重优先，容量=每天约2.5个景点
    kept.sort(key=lambda a: -a["must"])
    cap = max(3, round(days * 2.5))
    overflow = kept[cap:]
    kept = kept[:cap]
    for a in overflow:
        dropped.append({"name": a["n"], "reason": "天数内排不下，忍痛割爱"})
    notes += _base_notes(intel)
    return {"kept": kept, "dropped": dropped, "notes": notes,
            "foods": c["foods"][:5], "tips": c["tips"]}


def _base_notes(intel):
    """客观提示：天气/POI热度/季节——两种判断引擎共用。"""
    c, month, notes = intel["city"], datetime.now().month, []
    if intel.get("weather"):
        w = intel["weather"]
        notes.append(f"当前实况：{w['weather']} {w['temp']}°C")
        if any(k in (w["weather"] or "") for k in ("雨", "雪")):
            notes.append("有降水，行程中增加室内项目权重")
    if intel.get("pois_scenic"):
        real = [p["name"] for p in intel["pois_scenic"][:4]]
        notes.append("高德实时热门景点参考：" + "、".join(real))
    if month in c["bestMonths"]:
        notes.append(f"{month}月正值{c['name']}佳季")
    else:
        notes.append(f"{month}月非{c['name']}最佳季节（佳季：{'/'.join(map(str,c['bestMonths']))}月）")
    return notes


def apply_llm_judge(intel, days, prefs, llm_out):
    """把 LLM 的取舍结论与硬约束合并，产出与 judge_intel 同构的结果。

    LLM 决定优先级与剔除理由；代码负责：名单合法性校验（只允许真实景点名）、
    天数容量裁剪、客观 notes 补齐。llm_out 不合法时返回 None 交由调用方降级。
    """
    if not isinstance(llm_out, dict) or not isinstance(llm_out.get("keep"), list):
        return None
    c = intel["city"]
    names = {a["n"]: a for a in c["attractions"]}
    keep_seq = [n for n in llm_out["keep"] if n in names]
    if not keep_seq:
        return None
    drop_reasons = {}
    for x in llm_out.get("drop") or []:
        if isinstance(x, dict) and x.get("name") in names:
            drop_reasons[x["name"]] = str(x.get("reason") or "模型建议剔除")[:30]

    cap = max(3, round(days * 2.5))
    # LLM 点名保留的优先；其未提及也未剔除的按推荐度补位至容量满
    ordered = keep_seq + [a["n"] for a in
                          sorted(c["attractions"], key=lambda a: -a["must"])
                          if a["n"] not in keep_seq and a["n"] not in drop_reasons]
    kept_names = ordered[:cap]
    kept = sorted((names[n] for n in kept_names), key=lambda a: -a["must"])
    dropped = ([{"name": n, "reason": drop_reasons[n]}
                for n in drop_reasons if n not in kept_names]
               + [{"name": n, "reason": "天数内排不下，忍痛割爱"}
                  for n in ordered[cap:] if n not in drop_reasons])
    notes = [str(x).strip()[:60] for x in (llm_out.get("notes") or [])
             if isinstance(x, str) and x.strip()][:4]
    notes += _base_notes(intel)
    return {"kept": kept, "dropped": dropped, "notes": notes,
            "foods": c["foods"][:5], "tips": c["tips"]}


def _real_rail(origin_name, dest_name):
    """查12306真实车次，选去程(最早出发)/回程(最晚出发)各一班。"""
    data = apis.query_trains(origin_name, dest_name)
    if not data or not data["trains"]:
        return None
    hs = [t for t in data["trains"] if t["kind"] == "高铁"]
    trains = hs or data["trains"]
    outbound = min(trains, key=lambda t: t.get("dep") or "99:99")  # 最早出发
    ret = outbound
    back = apis.query_trains(dest_name, origin_name)               # 回程反方向查
    if back and back["trains"]:
        hb = [t for t in back["trains"] if t["kind"] == "高铁"] or back["trains"]
        ret = max(hb, key=lambda t: t.get("dep") or "")            # 最晚返程
    price = apis.train_price(outbound) or {}
    return {"date": data["date"], "all": trains,
            "outbound": outbound, "return": ret,
            "price_2nd": price.get("二等座"), "price_1st": price.get("一等座"),
            "src": "12306"}


def compose_plans(ctx, origin, dest_name, budget, days, transport, prefs, judged):
    """编排 3-5 套差异化行程方案。"""
    c = next(x for x in load_cities() if x["name"] == dest_name)
    km = geo.haversine_km(origin["lat"], origin["lng"], c["lat"], c["lng"])

    trans = geo.estimate_transport(km, transport, c)
    resolved_mode = trans["mode"]
    rail = _real_rail(origin["name"], c["name"]) if resolved_mode == "train" else None

    if rail and resolved_mode == "train":
        one_way = rail["price_2nd"] or trans["cost"]
        transport_info = {
            "mode": "train", "src": "12306", "hours": rail["outbound"]["hours"],
            "cost": round(one_way),
            "outbound": f"{rail['outbound']['code']} {origin['name']}{rail['outbound']['dep']}→{c['name']}{rail['outbound']['arr']}",
            "back": f"{rail['return']['code']} {c['name']}{rail['return']['dep']}→{origin['name']}{rail['return']['arr']}",
            "trains": [{"code": t["code"], "dep": t["dep"], "arr": t["arr"],
                        "hours": t["hours"], "from": t["from"], "to": t["to"],
                        "seats": t["seats"]} for t in rail["all"][:6]],
            "query_date": rail["date"],
        }
    else:
        transport_info = {
            "mode": resolved_mode, "src": "model", "hours": trans.get("hours"),
            "cost": trans["cost"],
            "outbound": f"{trans['desc']} 约{trans.get('hours')}h",
            "back": f"{trans['desc']} 约{trans.get('hours')}h返程",
            "trains": [],
        }
        if resolved_mode == "drive" and trans.get("driveDays", 1) > 1:
            transport_info["back"] += f"（约{round(trans.get('roadKm',0))}km）"

    kept = list(judged["kept"])
    foods = judged["foods"]

    themes = _plan_themes(days, c)
    plans = []
    for t in themes[:5]:
        plan = _build_plan(t, c, origin, kept, foods, days,
                           transport_info, budget, prefs)
        plans.append(plan)
    return {"plans": plans, "transport": transport_info, "km": km,
            "resolved_mode": resolved_mode}


# ----------------------------------------------------------- 行程编排 ----

def _plan_themes(days, c):
    themes = [
        {"id": "classic", "name": "经典全景线", "pace": "适中", "hf": 1.0, "ff": 1.0,
         "density": 2, "desc": "必去景点全覆盖，首次到访首选"},
        {"id": "foodie", "name": "寻味美食线", "pace": "轻松", "hf": 1.0, "ff": 1.3,
         "density": 1, "desc": "景点减量，把时间留给街头巷尾"},
    ]
    if days >= 3:
        themes.append({"id": "deep", "name": "深度慢游线", "pace": "悠闲", "hf": 1.15,
                       "ff": 1.1, "density": 1, "deep": True,
                       "desc": "减少打卡，加入周边延伸与本地生活"})
    themes.append({"id": "express", "name": "精华快闪线", "pace": "紧凑", "hf": 0.85,
                   "ff": 0.9, "density": 3, "desc": "一天顶一天半用，穷游高效"})
    if days >= 4 or "亲子" in c["tags"] or "度假" in c["tags"]:
        themes.append({"id": "comfort", "name": "舒适度假线", "pace": "悠闲", "hf": 1.4,
                       "ff": 1.2, "density": 1, "resort": True,
                       "desc": "升级住宿，午间留白，睡到自然醒"})
    return themes


def _build_plan(theme, city, origin, attractions, foods, days,
                transport, budget, prefs):
    """把景点/美食排进 days 天的日程槽位。"""
    density = theme["density"]
    atts = list(attractions)
    used = []
    itinerary = []

    big = [a for a in atts if a["hours"] >= 7]   # 需整天的景点
    normal = [a for a in atts if a["hours"] < 7]

    for d in range(1, days + 1):
        items = []
        if d == 1:
            items.append({"slot": "上午", "type": "交通",
                          "name": f"{origin['name']} → {city['name']}",
                          "note": transport["outbound"], "cost": transport["cost"]})
            pool = normal
        elif d == days:
            items.append({"slot": "下午", "type": "交通",
                          "name": f"{city['name']} → {origin['name']} 返程",
                          "note": transport["back"], "cost": transport["cost"]})
            pool = normal
        else:
            pool = normal

        # 挑选当日景点
        take = density if 1 < d < days else 1
        picked = []
        # 中间日优先安排"需一整天"的大景点（每个行程最多2个大景点日）
        big_cap = 1 if days <= 3 else 2
        if theme["id"] == "deep":
            big_cap = max(1, big_cap - 0)
        if 1 < d < days and not theme.get("resort"):
            big_left = [a for a in big if a not in used]
            big_used_so_far = sum(1 for a in big if a in used)
            if big_left and big_used_so_far < big_cap:
                picked.append(big_left[0]); used.append(big_left[0])
        for a in list(pool):
            if len(picked) >= take:
                break
            if a in used:
                continue
            picked.append(a); used.append(a)
            pool.remove(a)

        slot_order = ["上午", "下午", "晚上"]
        base = 1 if d == 1 else 0          # 抵达日景点从下午排起
        for i, a in enumerate(picked):
            slot = "全天" if a["hours"] >= 7 else slot_order[min(i + base, 2)]
            items.append({"slot": slot, "type": "景点", "name": a["n"],
                          "note": a["desc"], "cost": a["ticket"],
                          "hours": a["hours"]})
        # 餐饮槽位：午/晚
        f1 = foods[(d - 1) % len(foods)] if foods else None
        f2 = foods[(d) % len(foods)] if foods and len(foods) > 1 else None
        if d < days and f1:
            items.append({"slot": "午餐", "type": "美食", "name": f1["n"],
                          "note": f1["d"], "cost": f1["p"]})
        if f2:
            lab = "晚餐" if d < days else "午餐"
            items.append({"slot": lab, "type": "美食", "name": f2["n"],
                          "note": f2["d"], "cost": f2["p"]})
        if theme.get("resort") and 1 < d < days:
            items.append({"slot": "午后", "type": "休闲", "name": "酒店休憩/咖啡时光",
                          "note": "留白时间，度假节奏", "cost": 40})
        if theme["id"] == "foodie" and 1 < d < days and foods:
            extra = foods[(d + 1) % len(foods)]
            items.append({"slot": "下午茶/夜宵", "type": "美食", "name": extra["n"],
                          "note": extra["d"], "cost": extra["p"]})
        itinerary.append({"day": d, "title": _day_title(d, days, picked),
                          "items": items})

    tickets = sum(a["ticket"] for a in used)
    b = geo.trip_budget(city, days, transport["cost"],
                        hotel_factor=theme["hf"], food_factor=theme["ff"],
                        attraction_ticket_sum=tickets)
    over = b["total"] > budget
    return {
        "id": theme["id"], "name": theme["name"], "desc": theme["desc"],
        "pace": theme["pace"], "days": itinerary,
        "transport": transport, "budget": b, "fits_budget": not over,
        "over_hint": f"约超¥{b['total'] - budget}，建议降低住宿或餐饮档位" if over else "",
    }


def _day_title(d, days, picked):
    if d == 1:
        return "抵达 · 初印象"
    if d == days:
        return "收尾 · 返程"
    if picked:
        return picked[0]["n"].split("-")[0].split("(")[0][:8] + " 一线"
    return "自由探索"
