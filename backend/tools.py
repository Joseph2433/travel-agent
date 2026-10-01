"""Agent 工具集：定位解析、目的地扫描排序、情报搜索、情报判断、行程编排。"""
import json
import os
import threading
import time
from datetime import datetime

import apis
import geo
from datetime import timedelta

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
    """定位解析：指定城市 > 浏览器坐标（高德逆地理，失败则吸附最近枢纽）
    > 高德IP定位 > 默认上海。城市不在知识库时：用前端传来的坐标，
    或高德地理编码兜底。"""
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
        rg = apis.regeo(lat, lng)
        if rg:
            return {"name": rg["city"], "province": rg["province"],
                    "lat": float(lat), "lng": float(lng), "src": "gps",
                    "note": f"GPS定位 · {rg['district'] or rg['city']}（高德逆地理）"}
        near = min(cities, key=lambda c: geo.haversine_km(lat, lng, c["lat"], c["lng"]))
        return {"name": near["name"], "province": near["province"],
                "lat": float(lat), "lng": float(lng), "src": "gps",
                "note": f"GPS坐标定位，归属最近枢纽「{near['name']}」"}
    ip_loc = apis.ip_locate(client_ip or "")
    if ip_loc and ip_loc.get("lat"):
        return {"name": ip_loc.get("city") or "未知", "province": ip_loc.get("province", ""),
                "lat": ip_loc["lat"], "lng": ip_loc["lng"], "src": "amap-ip",
                "note": "高德IP定位（精度到城市级）"}
    sh = (next((c for c in cities if c["name"] == "上海"), None)
          or (cities[0] if cities else None)
          or {"name": "上海", "province": "上海", "lat": 31.2304, "lng": 121.4737})
    return {"name": sh["name"], "province": sh["province"], "lat": sh["lat"], "lng": sh["lng"],
            "src": "default", "note": f"定位不可用，默认以{sh['name']}为出发地"}


def pool_profile(name, province="", lat=None, lng=None):
    """非知识库城市的合成画像：距离/预算估算用中性消费档，
    景点/美食信息交由实时 POI 工具补齐。"""
    return {"name": name, "province": province,
            "lat": lat if lat is not None else 35.0,
            "lng": lng if lng is not None else 110.0,
            "hotelPerNight": 280, "foodPerDay": 110, "localPerDay": 40,
            "attractions": [], "foods": [], "tags": [], "bestMonths": [],
            "hsRail": True, "airport": True, "tips": []}


def get_city(name):
    """城市画像统一入口：知识库 → 全国地级市池（坐标+省份）→ 高德地理编码。
    找不到返回 None；任何地方都不应再直接遍历 load_cities() 假定在库。"""
    name = (name or "").strip().rstrip("市")
    if not name:
        return None
    c = next((x for x in load_cities() if x["name"] == name), None)
    if c:
        return c
    row = next((r for r in (apis.all_prefecture_cities() or [])
                if r.get("name") == name), None)
    if row:
        return pool_profile(name, row.get("province", ""),
                            row.get("lat"), row.get("lng"))
    g = apis.geocode(name)
    if g and g.get("lat") is not None:
        return pool_profile(name, g.get("city") or "", g["lat"], g["lng"])
    return None


def scan_destinations(ctx, origin, budget, days, transport, prefs,
                      provinces=None):
    """扫描目的地候选。默认池是全国地级市种子（357城）：知识库城市用真实
    画像估算，其余城市用合成档估算，小众目的地因此也能进入候选。
    provinces: 省份名列表（如 ["浙江","云南"]），限定推荐范围。"""
    kb = {c["name"]: c for c in load_cities()}
    pool = apis.all_prefecture_cities() or [
        {"name": c["name"], "province": c["province"],
         "lat": c["lat"], "lng": c["lng"]} for c in load_cities()]
    provs = {p.rstrip("省") for p in (provinces or [])}
    out, seen = [], set()
    for row in pool:
        name = row["name"]
        if name in seen or name == origin["name"]:
            continue
        seen.add(name)
        if provs and row["province"].rstrip("省") not in provs:
            continue
        if row.get("lat") is None or row.get("lng") is None:
            continue
        c = kb.get(name) or pool_profile(name, row["province"],
                                         row["lat"], row["lng"])
        km = geo.haversine_km(origin["lat"], origin["lng"], c["lat"], c["lng"])
        if km < 30:                                 # 同一都市圈，不算出游
            continue
        est = geo.estimate_transport(km, transport, c)
        if not est.get("feasible"):
            out.append({"city": name, "feasible": False,
                        "reason": est.get("reason"), "km": km})
            continue
        tickets = sum(a["ticket"] for a in c["attractions"] if a["must"] >= 4)
        rough = geo.trip_budget(c, days, est["cost"],
                                attraction_ticket_sum=tickets * 0.6)
        out.append({
            "city": name, "province": row["province"],
            "lat": c["lat"], "lng": c["lng"],
            "feasible": True, "km": km, "in_kb": name in kb,
            "transport_est": est, "rough_total": rough["total"],
            "fits_budget": rough["total"] <= budget,
            "over_ratio": round(rough["total"] / max(budget, 1), 2),
        })
    return out


_trend_cache = {"ts": 0.0, "names": set()}
# 高频词误伤名单：这些地级市名同时是常用词，命中热榜不算"热议"
_TREND_DENY = {"长治", "朝阳", "灯塔", "前进", "向阳", "东风", "共和",
               "文昌", "同心", "平安", "太和", "清流", "大田", "双峰",
               "永新", "惠民", "富民", "和平", "新华", "新兴", "解放"}


def trending_cities(ttl=600):
    """热榜标题中出现的城市名集合：微博/抖音/小红书三榜并发抓取，
    10 分钟缓存，失败降级为空集。供排序加"近期热议"分。"""
    if time.time() - _trend_cache["ts"] < ttl:
        return _trend_cache["names"]
    titles, lock = [], threading.Lock()

    def one(b):
        rows = apis.hot_board(b, 20) or []
        with lock:
            titles.extend(x["title"] or "" for x in rows)

    ths = [threading.Thread(target=one, args=(b,), daemon=True)
           for b in ("weibo", "douyin", "xiaohongshu")]
    for t in ths:
        t.start()
    for t in ths:
        t.join(8)
    pools = ([c["name"] for c in load_cities()]
             + [r["name"] for r in (apis.all_prefecture_cities() or [])])
    names = {n for n in pools
             if len(n) >= 2 and n not in _TREND_DENY
             and any(n in t for t in titles)}
    _trend_cache.update({"ts": time.time(), "names": names})
    return names


# 出行日期因素：固定假 + 农历假年表（近似调休区间，仅用于提示/打分微调）
_HOLI_FIXED = [((1, 1), (1, 1), "元旦"), ((5, 1), (5, 5), "劳动节"),
               ((10, 1), (10, 7), "国庆")]
_HOLI_LUNAR = {
    2025: [((1, 28), (2, 4), "春节"), ((4, 4), (4, 6), "清明"),
           ((5, 31), (6, 2), "端午"), ((10, 6), (10, 6), "中秋")],
    2026: [((2, 16), (2, 22), "春节"), ((4, 4), (4, 6), "清明"),
           ((6, 19), (6, 21), "端午"), ((9, 25), (9, 27), "中秋")],
    2027: [((2, 5), (2, 11), "春节"), ((4, 4), (4, 6), "清明"),
           ((6, 9), (6, 11), "端午"), ((9, 15), (9, 17), "中秋")],
}


def date_meta(date_str, days=1):
    """出发日期画像：星期/是否撞假期或周末/距今天数/12306预售期/预报可及性。
    无效或过去日期返回 None（调用方按未指定处理）。"""
    if not date_str:
        return None
    try:
        dep = datetime.strptime(str(date_str)[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None
    today = datetime.now().date()
    away = (dep - today).days
    if away < 0:
        return None
    days = max(1, int(days or 1))
    ret = dep + timedelta(days=days - 1)
    trip = [dep + timedelta(days=i) for i in range(days)]

    def _mdhit(d, a, b):          # (月,日) 落区间，兼容跨年段
        md = (d.month, d.day)
        return a <= md <= b if a <= b else (md >= a or md <= b)

    holiday = None
    for a, b, n in _HOLI_FIXED + _HOLI_LUNAR.get(dep.year, []):
        if any(_mdhit(d, a, b) for d in trip):
            holiday = n
            break
    weekend = any(d.weekday() >= 5 for d in trip) or dep.weekday() == 4
    return {"date": dep.isoformat(), "ret": ret.isoformat(),
            "weekday": dep.weekday(), "away": away,
            "holiday": holiday, "weekend": weekend,
            "rail_sale": away <= 13,          # 12306预售期约15天(浮动，留余量)
            "fc_range": away <= 3,            # 高德逐日预报仅覆盖今天起4天
            "label": f"{dep.month}月{dep.day}日"
                     f"·周{'一二三四五六日'[dep.weekday()]}"}


def trip_forecast(intel, meta):
    """从 intel.weather_fore 中筛出行程窗口（出发日~返程日）内的逐日预报。"""
    if not meta:
        return None
    fore = [c for c in (intel.get("weather_fore") or [])
            if meta["date"] <= (c.get("date") or "") <= meta["ret"]]
    return fore or None


def date_notes(meta, fore=None):
    """出发日期衍生的出行提示：假期高峰 / 周末 / 预售期 / 逐日天气。"""
    if not meta:
        return []
    tags = []
    if meta["holiday"]:
        tags.append(meta["holiday"] + "假期")
    elif meta["weekend"]:
        tags.append("周末档")
    head = f"出发日 {meta['label']}" + ("·" + "·".join(tags) if tags else "")
    if meta["holiday"] or meta["weekend"]:
        head += "，客流高峰车票住宿建议尽早订"
    notes = [head]
    if not meta["rail_sale"]:
        notes.append("距出发超12306预售期(约15天)，车次票价为估算，开售后再核验")
    if fore:
        seg = "、".join(f"{(c.get('date') or '')[5:].replace('-', '/')}"
                        f"{c.get('day')}{c.get('lo')}~{c.get('hi')}°C"
                        for c in fore)
        notes.append("行程天气：" + seg)
    elif not meta["fc_range"]:
        notes.append("出发日较远暂无逐日预报，出发前3天可再看天气")
    return notes


def rank_destinations(ctx, scanned, budget, days, prefs, meta=None):
    """多因子打分：预算契合 + 天数匹配 + 季节适宜 + 偏好命中 + 距离效率 + 热榜
    + 出发日因素（节假日/周末高峰 → 短途圈加分、长线标注票紧）。"""
    month = datetime.now().month
    cities = {c["name"]: c for c in load_cities()}
    trending = trending_cities()
    scored = []
    for s in scanned:
        if not s["feasible"]:
            continue
        c = cities.get(s["city"]) or pool_profile(
            s["city"], s.get("province", ""), s.get("lat"), s.get("lng"))
        in_kb = s["city"] in cities
        score, reasons = 0, []
        # 预算（35）
        if s["fits_budget"]:
            score += 35; reasons.append(f"预算内（约¥{s['rough_total']}）")
        elif s["over_ratio"] <= 1.3:
            score += 15; reasons.append(f"略超预算（约¥{s['rough_total']}，可降住宿档位）")
        else:
            reasons.append(f"超预算较多（约¥{s['rough_total']}）")
        # 天数（20）：景点量与天数匹配；非知识库城市按中性值3天
        need = (max(2, min(6, round(len(c["attractions"]) / 2)))
                if c["attractions"] else 3)
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
        # 偏好（20）；非知识库城市无标签，给个"小众"标记分兜底
        hit = set(prefs) & set(c["tags"])
        if hit:
            score += min(20, 8 * len(hit)); reasons.append("契合偏好：" + "、".join(hit))
        elif not in_kb:
            score += 6; reasons.append("小众目的地 · 行程由实时POI编排")
        # 距离效率（10）：交通耗时越短越好
        h = s["transport_est"].get("hours", 99)
        score += max(0, 10 - h)
        # 热榜（10）：目的地正挂在微博/抖音/小红书热搜上
        if s["city"] in trending:
            score += 10; reasons.append("近期全网热议")
        # 出发日因素：假期/周末高峰 → 短途圈更稳，长线标注票紧
        if meta and (meta.get("holiday") or meta.get("weekend")):
            tag = meta.get("holiday") or "周末"
            if h <= 4:
                score += 4; reasons.append(f"{tag}短途圈·票源相对稳")
            elif h >= 7:
                reasons.append(f"{tag}长线·车票紧俏需早订")
        scored.append({
            "city": c["name"], "province": c["province"], "score": round(score),
            "km": s["km"], "lat": c["lat"], "lng": c["lng"],
            "tags": c["tags"], "in_kb": in_kb, "reasons": reasons,
            "transport_est": s["transport_est"], "rough_total": s["rough_total"],
            "hotelPerNight": c["hotelPerNight"],
        })
    scored.sort(key=lambda x: -x["score"])
    return scored


_WINTER_MARK = ("冰雪大世界", "雪博会", "冰雪嘉年华")


def fetch_intel(ctx, city_name, date=None):
    """目的地情报搜索：高德POI + 天气(+指定日期的逐日预报) + 网页攻略 + 本地知识库。"""
    c = get_city(city_name) or pool_profile(city_name)
    in_kb = any(x["name"] == c["name"] for x in load_cities())
    intel = {"city": c,
             "sources": ["本地知识库"] if in_kb else [],
             "pois_scenic": None, "pois_food": None, "weather": None,
             "web": None, "scenic_qunar": None}
    w = apis.weather_live(c["name"])
    if w:
        intel["weather"] = w; intel["sources"].append("高德天气")
    if date:                                   # 给了出发日 → 拉逐日预报（近4天）
        fc = apis.weather_forecast(c["name"])
        if fc:
            intel["weather_fore"] = fc
    scenic = apis.poi_search(c["name"], "景点", "110000", 8)
    if scenic and scenic["pois"]:
        intel["pois_scenic"] = scenic["pois"]; intel["sources"].append("高德POI")
    food = apis.poi_search(c["name"], "特色美食", "050000", 8)
    if food and food["pois"]:
        intel["pois_food"] = food["pois"]
    qs = apis.qunar_scenic(c["name"], 8)
    if qs and qs["scenic"]:
        intel["scenic_qunar"] = qs["scenic"]; intel["sources"].append("去哪儿票价")
    web = apis.web_search_snippets(f"{c['name']}旅游攻略 必去景点 美食")
    if web:
        intel["web"] = web; intel["sources"].append("网页搜索")
    if apis.xhs_enabled():
        res = apis.xhs_search_notes(f"{c['name']} 旅游攻略", limit=4)
        if res and res.get("notes"):
            intel["xhs"] = res["notes"]; intel["sources"].append("小红书")
    return intel


def _poi_attractions(intel, cap):
    """非知识库城市：把实时POI/去哪儿在售景点合成 judge 用的景点素材
    （与知识库 attraction 同构：n/desc/ticket/hours/must）。"""
    out, seen = [], set()
    for s in intel.get("scenic_qunar") or []:
        n = (s.get("name") or "").strip()
        if n and n not in seen:
            seen.add(n)
            out.append({"n": n,
                        "desc": (s.get("intro") or "").strip()[:40]
                                or f"去哪儿在售景区·评分{s.get('score') or '—'}",
                        "ticket": s.get("ticket") or 0, "hours": 3,
                        "must": min(5.0, float(s.get("score") or 3.5))})
    for p in intel.get("pois_scenic") or []:
        n = (p.get("name") or "").strip()
        if n and n not in seen:
            seen.add(n)
            try:
                must = min(5.0, float(p.get("rating") or 3))
            except (TypeError, ValueError):
                must = 3.0
            out.append({"n": n,
                        "desc": f"高德热门POI·评分{p.get('rating') or '—'}",
                        "ticket": 0, "hours": 3, "must": must})
    return out[:cap]


def _poi_foods(intel):
    """非知识库城市：高德美食POI → 与知识库 food 同构的 {n,d,p}。"""
    out = []
    for p in (intel.get("pois_food") or [])[:5]:
        if not p.get("name"):
            continue
        try:
            cost = int(float(p.get("cost") or 60))
        except (TypeError, ValueError):
            cost = 60
        out.append({"n": p["name"],
                    "d": f"高德热门POI·评分{p.get('rating') or '—'}",
                    "p": cost})
    return out


# ------------------------------------------------- 风格 / 时间轴 / 美食备选 ----

# 游玩风格：特种兵=早出晚归高密度；休闲随意=晚起留白；适中=常规
_STYLE_CFG = {
    "特种兵":   {"start": 7.5, "meal": 0.75, "gap": 0.25, "shift": -0.5, "take": +1},
    "休闲随意": {"start": 10.0, "meal": 1.5, "gap": 1.0, "shift": +0.5, "take": -1},
}
_STYLE_DEF = {"start": 9.0, "meal": 1.0, "gap": 0.5, "shift": 0.0, "take": 0}

# slot 标签 → 最早开始时刻（小时），再按风格 shift 微调
_SLOT_EARLIEST = {"午餐": 11.5, "午后": 13.0, "下午": 13.5,
                  "下午茶/夜宵": 15.0, "晚餐": 17.5, "晚上": 19.0}
_DUR_DEF = {"景点": 3.0, "休闲": 1.5, "交通": 2.0}
# 槽位 → 日内规范顺序（返程交通恒排当天末尾，去程恒排最前）
_SLOT_ORDER = {"上午": 0, "全天": 0, "午餐": 3, "午后": 4, "下午": 5,
               "下午茶/夜宵": 6, "晚餐": 7, "晚上": 8}


def _item_rank(d, it):
    if it.get("type") == "交通":
        return 9 if "返程" in (it.get("name") or "") else -1
    return _SLOT_ORDER.get(it.get("slot") or "", 5)


def _style_cfg(style):
    return {**_STYLE_DEF, **_STYLE_CFG.get(style or "", {})}


def _hm(h):
    m = round(max(0, min(h, 24)) * 60)
    return f"{m // 60:02d}:{m % 60:02d}"


def assign_times(days, style="适中"):
    """给每个行程项打大致时间范围（it["time"]="HH:MM–HH:MM"）：
    当天项目先按槽位规范顺序重排（去程最前、返程最后、午餐在午后前），
    再从「出门时刻」顺推；slot 标签约束最早开始点；
    风格决定起床早晚/吃饭快慢/项目间隔。"""
    cfg = _style_cfg(style)
    for d in days or []:
        items = d.get("items") or []
        items.sort(key=lambda it: _item_rank(d, it))
        cur = cfg["start"]
        for it in items:
            ear = _SLOT_EARLIEST.get(it.get("slot") or "")
            if ear is not None:
                cur = max(cur, ear + cfg["shift"])
            dur = 0.0
            if it.get("type") == "美食":
                dur = cfg["meal"]
            else:
                try:
                    dur = float(it.get("hours") or 0)
                except (TypeError, ValueError):
                    dur = 0.0
                if dur <= 0:
                    dur = _DUR_DEF.get(it.get("type"), 1.5)
            it["time"] = (f"{_hm(cur)}–{_hm(cur + dur)}"
                          if cur < 23.9 else "深夜后")
            cur += dur + cfg["gap"]
    return days


def food_options(pool, exclude="", limit=3):
    """给一餐配不同价位备选：pool 兼容 KB foods({n,d,p}) 与 POI({name,rating,cost})，
    按价格升序取 低/中/高 档各一个。返回 [{name,cost,tier,note}]。"""
    cand, seen = [], {exclude}
    for f in pool or []:
        n = (f.get("n") or f.get("name") or "").strip()
        if not n or n in seen:
            continue
        seen.add(n)
        try:
            cost = round(float(f.get("p") if f.get("p") is not None
                               else f.get("cost") or 0)) or None
        except (TypeError, ValueError):
            cost = None
        cand.append({"name": n[:16], "cost": cost,
                     "note": (f.get("d") or
                              (f"评分{f['rating']}" if f.get("rating") else "")
                              or "")[:40]})
    cand.sort(key=lambda x: (x["cost"] is None, x["cost"] or 0))
    sel = ([cand[0], cand[len(cand) // 2], cand[-1]] if len(cand) > limit
           else cand)
    for i, o in enumerate(sel):
        o["tier"] = (["平价", "中档", "品质"][i] if len(sel) == 3
                     else ("平价" if i == 0 else "升级"))
    return sel


def judge_intel(ctx, intel, days, prefs, date=None):
    """对搜到的情报做判断：剔除不合时令/低价值项，按天数取舍，输出核心体验清单。
    date: 出发日 YYYY-MM-DD → 生成假期/预售/逐日天气提示。"""
    c, month = intel["city"], datetime.now().month
    kept, dropped, notes = [], [], []
    cap = max(3, round(days * 2.5))
    atts = c["attractions"] or _poi_attractions(intel, cap)
    for a in atts:
        if any(m in a["n"] for m in _WINTER_MARK) and month not in (12, 1, 2):
            dropped.append({"name": a["n"], "reason": "冬季限定，当前未开放"})
            continue
        if a["hours"] >= 8 and days <= 2:
            dropped.append({"name": a["n"], "reason": "需一整天，短行程放不下"})
            continue
        kept.append(a)
    # 高权重优先，容量=每天约2.5个景点
    kept.sort(key=lambda a: -a["must"])
    overflow = kept[cap:]
    kept = kept[:cap]
    for a in overflow:
        dropped.append({"name": a["n"], "reason": "天数内排不下，忍痛割爱"})
    meta = date_meta(date, days)
    notes = date_notes(meta, trip_forecast(intel, meta))
    notes += _base_notes(intel)
    return {"kept": kept, "dropped": dropped, "notes": notes,
            "foods": c["foods"][:5] or _poi_foods(intel),
            "tips": c["tips"],
            "qunar_scenic": intel.get("scenic_qunar") or []}


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
    if intel.get("scenic_qunar"):
        top = sorted(intel["scenic_qunar"],
                     key=lambda s: -(s.get("sales") or 0))[:3]
        notes.append("去哪儿在售景点（真实票价）：" + "、".join(
            f"{s['name']}¥{s['ticket']:g}" for s in top))
    if c["bestMonths"]:
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
            "foods": c["foods"][:5] or _poi_foods(intel),
            "tips": c["tips"],
            "qunar_scenic": intel.get("scenic_qunar") or []}


def _real_rail(origin_name, dest_name, dep_date=None, ret_date=None):
    """查12306真实车次，选去程(最早出发)/回程(最晚出发)各一班。
    dep_date/ret_date: YYYY-MM-DD，None 用接口默认（预售期第7天）。"""
    data = apis.query_trains(origin_name, dest_name, dep_date)
    if not data or not data["trains"]:
        return None
    hs = [t for t in data["trains"] if t["kind"] == "高铁"]
    trains = hs or data["trains"]
    outbound = min(trains, key=lambda t: t.get("dep") or "99:99")  # 最早出发
    ret = outbound
    back = apis.query_trains(dest_name, origin_name, ret_date)   # 回程反方向查
    if back and back["trains"]:
        hb = [t for t in back["trains"] if t["kind"] == "高铁"] or back["trains"]
        ret = max(hb, key=lambda t: t.get("dep") or "")            # 最晚返程
    price = apis.train_price(outbound) or {}
    return {"date": data["date"], "ret_date": (back or {}).get("date"),
            "all": trains,
            "outbound": outbound, "return": ret,
            "price_2nd": price.get("二等座"), "price_1st": price.get("一等座"),
            "src": "12306"}


def compose_plans(ctx, origin, dest_name, budget, days, transport, prefs,
                  judged, date=None, style="适中"):
    """编排 3-5 套差异化行程方案。date: 出发日 YYYY-MM-DD（可选）；
    style: 特种兵|适中|休闲随意——影响每日密度与时间轴。"""
    c = get_city(dest_name) or pool_profile(dest_name)
    km = geo.haversine_km(origin["lat"], origin["lng"], c["lat"], c["lng"])

    trans = geo.estimate_transport(km, transport, c)
    resolved_mode = trans["mode"]
    meta = date_meta(date, days)
    dep_d = meta["date"] if meta else None      # 去程=出发日
    ret_d = meta["ret"] if meta else None       # 回程=行程最后一天
    rail = _real_rail(origin["name"], c["name"], dep_d, ret_d) \
        if resolved_mode == "train" else None

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
            "ret_date": rail.get("ret_date"),
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
                           transport_info, budget, prefs,
                           qunar=judged.get("qunar_scenic"), style=style)
        plan["guide"] = build_plan_guide(plan, judged)
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


_CORE_FILLER = ("风景名胜", "国家", "旅游", "度假", "风景", "名胜",
                "景区", "公园", "区")


def _core_name(x):
    """景点名归一化："秦始皇帝陵博物院(兵马俑)"→"秦始皇帝陵博物院"，
    "西溪国家湿地公园"→"西溪湿地"。便于和知识库短名做包含匹配。"""
    n = (x or "").split("（")[0].split("(")[0].strip()
    for w in _CORE_FILLER:
        if n != w:                       # 防止"西湖区"被剥成"西湖"之外的空串
            n = n.replace(w, "")
    return n.strip() or (x or "").split("（")[0].split("(")[0].strip()


def _qunar_price(name, scenic):
    """知识库景点名 → 去哪儿在售景点的真实票价；匹配不到返回 None。"""
    n = _core_name(name)
    best = None
    for s in scenic or []:
        q, core = s["name"], _core_name(s["name"])
        hit = (n and n in q) or (len(core) >= 3 and core in n) or n == core
        if hit and (best is None
                    or (s.get("sales") or 0) > (best.get("sales") or 0)):
            best = s
    return best["ticket"] if best else None


def _build_plan(theme, city, origin, attractions, foods, days,
                transport, budget, prefs, qunar=None, style="适中"):
    """把景点/美食排进 days 天的日程槽位。style 影响每日密度与时间轴。"""
    cfg = _style_cfg(style)
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
                          "note": transport["outbound"], "cost": transport["cost"],
                          "hours": transport.get("hours")})
            pool = normal
        elif d == days:
            items.append({"slot": "下午", "type": "交通",
                          "name": f"{city['name']} → {origin['name']} 返程",
                          "note": transport["back"], "cost": transport["cost"],
                          "hours": transport.get("hours")})
            pool = normal
        else:
            pool = normal

        # 挑选当日景点；特种兵多加一个、休闲随意减一个
        take = max(1, (density if 1 < d < days else 1) + cfg["take"])
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
            real = _qunar_price(a["n"], qunar)
            items.append({"slot": slot, "type": "景点", "name": a["n"],
                          "note": a["desc"],
                          "cost": real if real is not None else a["ticket"],
                          "hours": a["hours"]})
        # 餐饮槽位：午/晚；每餐附不同价位备选
        f1 = foods[(d - 1) % len(foods)] if foods else None
        f2 = foods[(d) % len(foods)] if foods and len(foods) > 1 else None
        if d < days and f1:
            items.append({"slot": "午餐", "type": "美食", "name": f1["n"],
                          "note": f1["d"], "cost": f1["p"],
                          "options": food_options(foods, f1["n"])})
        if f2:
            lab = "晚餐" if d < days else "午餐"
            items.append({"slot": lab, "type": "美食", "name": f2["n"],
                          "note": f2["d"], "cost": f2["p"],
                          "options": food_options(foods, f2["n"])})
        if theme.get("resort") and 1 < d < days:
            items.append({"slot": "午后", "type": "休闲", "name": "酒店休憩/咖啡时光",
                          "note": "留白时间，度假节奏", "cost": 40})
        if theme["id"] == "foodie" and 1 < d < days and foods:
            extra = foods[(d + 1) % len(foods)]
            items.append({"slot": "下午茶/夜宵", "type": "美食", "name": extra["n"],
                          "note": extra["d"], "cost": extra["p"],
                          "options": food_options(foods, extra["n"])})
        itinerary.append({"day": d, "title": _day_title(d, days, picked),
                          "items": items})

    assign_times(itinerary, style)

    tickets = sum((rp if (rp := _qunar_price(a["n"], qunar)) is not None
                   else a["ticket"]) for a in used)
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


# ----------------------------------------------------------- 详细攻略 ----

def build_guide(c, intel, judged, tool_data=None, transport=None):
    """组装「目的地详细攻略」：小红书笔记骨架 → 去哪儿票价 → 美食 → 贴士 → 来源。
    全部内容来自工具/接口真实返回，代码只做取舍与去重，不生成文本。
    item 结构统一为 {title, meta, url, excerpt}（后端字段，前端负责排版）。"""
    sections = []

    # ① 攻略灵感：小红书真实笔记（LLM路径从 tool_data 收，规则路径从 intel 直收）
    notes = []
    for td in (tool_data or {}).get("search_xhs_notes") or []:
        if isinstance(td, dict):
            notes.extend(td.get("notes") or [])
    notes.extend(intel.get("xhs") or [])
    seen, xhs_items = set(), []
    for n in notes:
        t = (n.get("title") or "").strip()
        if not t or t in seen:
            continue
        seen.add(t)
        xhs_items.append({"title": t[:42],
                          "meta": f"赞{n.get('likes', '0')}"
                                  + (f" · 藏{n['collects']}" if n.get("collects") else "")
                                  + f" · @{n.get('author', '')}",
                          "url": n.get("url") or "",
                          "excerpt": (n.get("excerpt") or "")[:120]})
    if xhs_items:
        sections.append({"icon": "📕", "title": "攻略灵感 · 小红书真实笔记",
                         "items": xhs_items[:6]})

    # ② 景点与门票：去哪儿在售真实票价
    scenic = intel.get("scenic_qunar") or []
    if scenic:
        items = []
        for s in scenic[:8]:
            meta = " · ".join(x for x in [
                (s["star"] + "景区") if s.get("star") else "",
                f"评分{s['score']}" if s.get("score") else "",
                "免费" if not s.get("ticket") else f"¥{s['ticket']:g}",
                f"月销{s['sales']}" if s.get("sales") else ""] if x)
            items.append({"title": s["name"], "meta": meta,
                          "excerpt": s.get("intro") or ""})
        sections.append({"icon": "🎫", "title": "景点与门票 · 去哪儿实时在售",
                         "items": items})

    # ③ 本地风味：知识库特色美食 + 高德美食POI
    food_items = [{"title": f["n"], "meta": f"约¥{f['p']}", "excerpt": f["d"]}
                  for f in (judged.get("foods") or [])]
    for p in (intel.get("pois_food") or [])[:4]:
        food_items.append({"title": p["name"],
                           "meta": f"评分{p.get('rating') or '—'}"
                                   + (f" · 人均¥{p['cost']}" if p.get("cost") else ""),
                           "excerpt": (p.get("addr") or "")[:30]})
    if food_items:
        sections.append({"icon": "🍜", "title": "本地风味 · 特色与热门店",
                         "items": food_items[:8]})

    # ④ 大交通：12306 真实车次或估算
    if transport and transport.get("outbound"):
        items = [{"title": transport["outbound"], "meta": "去程"},
                 {"title": transport["back"], "meta": "回程"}]
        for tr in (transport.get("trains") or [])[:4]:
            seat = (tr.get("seats") or {}).get("二等座")
            items.append({"title": f"{tr['code']}　{tr['from']}→{tr['to']}",
                          "meta": f"{tr['dep']}–{tr['arr']} · {tr['hours']}h"
                                  + (f" · 二等座{seat}" if seat else "")})
        sections.append({"icon": "🚄",
                         "title": "大交通 · " + ("12306 实时余票"
                                 if transport.get("src") == "12306" else "估算参考"),
                         "items": items})

    # ⑤ 贴士与避雷：知识库贴士 + 引擎提示 + 笔记热评摘录
    tips = []
    for x in (judged.get("tips") or []) + (judged.get("notes") or []):
        if x and x not in tips:
            tips.append(x)
    for n in notes:
        for cm in (n.get("hot_comments") or [])[:1]:
            txt = f"小红书热评：{cm}"
            if txt not in tips:
                tips.append(txt)
    if tips:
        sections.append({"icon": "⚠️", "title": "贴士与避雷",
                         "items": [{"title": t} for t in tips[:8]]})

    # ⑥ 参考来源：攻略网页 + 笔记链接
    links, seen_u = [], set()
    for w in (intel.get("web") or []):
        u = w.get("url") or ""
        if u and u not in seen_u:
            seen_u.add(u)
            links.append({"title": w.get("title") or u, "url": u,
                          "meta": {"web": "网页", "xhs-web": "小红书(被索引)",
                                   "xiaohongshu": "小红书"}.get(w.get("src"),
                                                              "网页")})
    for n in xhs_items:
        if n["url"] and n["url"] not in seen_u:
            seen_u.add(n["url"])
            links.append({"title": n["title"], "url": n["url"],
                          "meta": "小红书"})
    if links:
        sections.append({"icon": "🔗", "title": "参考来源", "items": links[:10]})

    return {"dest": c["name"], "sections": sections}


def _cost_txt(cost):
    return f"¥{cost:g}" if isinstance(cost, (int, float)) and cost else ""


def build_plan_guide(plan, judged=None):
    """为单套方案生成「详细攻略」正文（markdown-lite：###小节 / -列表 / **重点**）。
    模型没写 guide 时的确定性兜底：只复述工具数据里已有的信息——
    行程项本身、去哪儿真实票价、知识库贴士与提示，不新造内容。"""
    judged = judged or {}
    scenic = judged.get("qunar_scenic") or []
    days = plan.get("days") or []
    lines = []

    if days:
        lines.append("### 路线速览")
        for d in days:
            spots = [i["name"] for i in d.get("items", [])
                     if i.get("type") in ("景点", "美食", "休闲")]
            seg = " → ".join(spots[:5]) or "机动安排"
            lines.append(f"- **D{d.get('day')} {d.get('title', '')}**：{seg}")

    fee, seen = [], set()
    for d in days:
        for i in d.get("items", []):
            name = i.get("name") or ""
            if i.get("type") != "景点" or name in seen:
                continue
            seen.add(name)
            real = _qunar_price(name, scenic)
            if real is not None:
                fee.append(f"- {name}：去哪儿在售 **¥{real:g}**")
            elif i.get("cost"):
                fee.append(f"- {name}：参考门票 {_cost_txt(i['cost'])}")
            else:
                fee.append(f"- {name}：免费开放")
    if fee:
        lines.append("### 门票与预约")
        lines.extend(fee[:8])
        lines.append("- 旺季/节假日建议提前 1-3 天在官方公众号或 OTA 预约购票")

    eats = [i for d in days for i in d.get("items", [])
            if i.get("type") == "美食"]
    if eats:
        lines.append("### 吃什么")
        for i in eats[:6]:
            cost = _cost_txt(i.get("cost"))
            lines.append(f"- **{i['name']}**："
                         + (f"{cost} · " if cost else "")
                         + (i.get("note") or "当地特色")[:40])

    tips = []
    for x in (judged.get("tips") or []) + (judged.get("notes") or []):
        if x and x not in tips:
            tips.append(x)
    if tips:
        lines.append("### 行前贴士")
        lines.extend(f"- {t}" for t in tips[:6])

    return "\n".join(lines)
