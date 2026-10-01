"""外部数据源接入层：高德 Web 服务 + 12306 余票查询。

所有调用都有超时与异常兜底，任何一步失败都会自动降级为内置估算模型，
保证应用在无 Key、无外网的情况下依然完整可用。

数据来源标记约定：返回 dict 中的 "src" 字段：
  - "amap"   高德开放平台实时数据（需要 AMAP_KEY）
  - "12306"  12306 官方余票接口实时数据（无需 Key）
  - "model"  本地估算模型
"""
import json
import os
import re
import threading
from datetime import datetime, timedelta

import requests

AMAP_KEY = os.environ.get("AMAP_KEY", "").strip()
AMAP_BASE = "https://restapi.amap.com"
RAIL_BASE = "https://kyfw.12306.cn"

_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) TravelAgent/1.0"}
_TIMEOUT = 6

# ---------------------------------------------------------------- amap ----

def amap_available() -> bool:
    return bool(AMAP_KEY)


def _amap_get(path, **params):
    params["key"] = AMAP_KEY
    r = requests.get(AMAP_BASE + path, params=params, timeout=_TIMEOUT)
    r.raise_for_status()
    d = r.json()
    # 高德 v3 接口 status=="1" 表示成功
    if str(d.get("status")) != "1":
        raise RuntimeError(f"amap error: {d.get('info')}")
    return d


def ip_locate(ip: str = ""):
    """高德 IP 定位。返回 {city, adcode, lng, lat} 或 None。"""
    if not amap_available():
        return None
    try:
        d = _amap_get("/v3/ip", ip=ip or "")
        rect = d.get("rectangle") or ""
        if not rect or ";" not in rect:
            return None
        (lng1, lat1), (lng2, lat2) = (p.split(",") for p in rect.split(";"))
        return {
            "city": str(d.get("city") or "").replace("市", "") or None,
            "province": str(d.get("province") or ""),
            "adcode": d.get("adcode"),
            "lng": (float(lng1) + float(lng2)) / 2,
            "lat": (float(lat1) + float(lat2)) / 2,
            "src": "amap",
        }
    except Exception:
        return None


def geocode(address: str):
    """高德地理编码：地名 -> 经纬度/城市。"""
    if not amap_available():
        return None
    try:
        d = _amap_get("/v3/geocode/geo", address=address)
        geo = (d.get("geocodes") or [None])[0]
        if not geo:
            return None
        lng, lat = geo["location"].split(",")
        return {
            "city": (geo.get("city") or "").replace("市", "") if isinstance(geo.get("city"), str) else None,
            "lng": float(lng), "lat": float(lat),
            "formatted": geo.get("formatted_address"), "src": "amap",
        }
    except Exception:
        return None


def driving_route(o_lng, o_lat, d_lng, d_lat):
    """驾车路径规划：返回 {km, hours, tolls}。"""
    if not amap_available():
        return None
    try:
        d = _amap_get("/v3/direction/driving",
                      origin=f"{o_lng},{o_lat}", destination=f"{d_lng},{d_lat}",
                      strategy="0")
        path = ((d.get("route") or {}).get("paths") or [None])[0]
        if not path:
            return None
        return {
            "km": round(float(path["distance"]) / 1000, 1),
            "hours": round(float(path["duration"]) / 3600, 1),
            "tolls": round(float(path.get("tolls") or 0)),
            "src": "amap",
        }
    except Exception:
        return None


def weather_live(city_name: str):
    """高德天气实况。city_name 可传城市名。"""
    if not amap_available():
        return None
    try:
        d = _amap_get("/v3/weather/weatherInfo", city=city_name, extensions="base")
        live = (d.get("lives") or [None])[0]
        if not live:
            return None
        return {
            "weather": live.get("weather"), "temp": live.get("temperature"),
            "wind": f"{live.get('winddirection')}风{live.get('windpower')}级",
            "humidity": live.get("humidity"), "src": "amap",
        }
    except Exception:
        return None


def district_cities(province: str):
    """高德行政区域查询：某省下辖城市列表 [{name, lat, lng}]。
    直辖市/特区返回自身；无 key 或失败返回 None 由调用方降级。"""
    if not amap_available():
        return None
    prov = province.strip().rstrip("省市自治区壮族回族维吾尔") or province.strip()
    try:
        d = _amap_get("/v3/config/district", keywords=prov, subdistrict=1)
        top = (d.get("districts") or [{}])[0]
        subs = top.get("districts") or []
        if prov in _DIRECT or not subs:
            c = (top.get("center") or "").split(",")
            return [{"name": prov,
                     "lng": float(c[0]) if len(c) == 2 else None,
                     "lat": float(c[1]) if len(c) == 2 else None}]
        out = []
        for it in subs:
            c = (it.get("center") or "").split(",")
            out.append({"name": (it.get("name") or "").rstrip("市"),
                        "lng": float(c[0]) if len(c) == 2 else None,
                        "lat": float(c[1]) if len(c) == 2 else None})
        return [x for x in out if x["name"]]
    except Exception:
        return None


_DIRECT = {"北京", "天津", "上海", "重庆", "香港", "澳门", "台湾"}

PROVINCES = ["北京", "天津", "河北", "山西", "内蒙古", "辽宁", "吉林", "黑龙江",
             "上海", "江苏", "浙江", "安徽", "福建", "江西", "山东", "河南",
             "湖北", "湖南", "广东", "广西", "海南", "重庆", "四川", "贵州",
             "云南", "西藏", "陕西", "甘肃", "青海", "宁夏", "新疆",
             "香港", "澳门", "台湾"]


def poi_search(city: str, keywords: str, poi_type: str = "", count: int = 8):
    """高德 POI 搜索（v5）。返回 [{name, type, addr, rating, cost}]。"""
    if not amap_available():
        return None
    try:
        params = {"keywords": keywords, "region": city, "page_size": count}
        if poi_type:
            params["types"] = poi_type
        r = requests.get(AMAP_BASE + "/v5/place/text", params=params,
                         headers={**_UA, "key": AMAP_KEY}, timeout=_TIMEOUT)
        r.raise_for_status()
        d = r.json()
        pois = []
        for p in d.get("pois") or []:
            biz = p.get("biz_ext") or {}
            rating = biz.get("rating") if isinstance(biz, dict) else None
            cost = biz.get("cost") if isinstance(biz, dict) else None
            pois.append({
                "name": p.get("name"), "type": p.get("type"),
                "addr": p.get("address") if isinstance(p.get("address"), str) else "",
                "rating": rating, "cost": cost,
            })
        return {"pois": pois, "src": "amap"}
    except Exception:
        return None

# --------------------------------------------------------------- 12306 ----

_station_lock = threading.Lock()
_city_stations: dict = {}          # city -> [(station_name, code)]
_station_city: dict = {}           # code -> city
_rail_cookie = None


def _load_station_map():
    """拉取 12306 车站码表，构建 城市->车站 映射（懒加载，带缓存）。"""
    global _city_stations, _station_city
    with _station_lock:
        if _city_stations:
            return
        r = requests.get(RAIL_BASE + "/otn/resources/js/framework/station_name.js",
                         headers=_UA, timeout=_TIMEOUT)
        r.raise_for_status()
        text = r.text
        # 格式: @abbr|站名|代码|拼音|abbr|idx|区号|城市|||
        for rec in text.split("@")[1:]:
            f = rec.split("|")
            if len(f) < 8:
                continue
            name, code, city = f[1], f[2], f[7] or f[1]
            _city_stations.setdefault(city.rstrip("市"), []).append((name, code))
            _station_city[code] = city


def _rail_session():
    """12306 需要先在 init 页种 cookie。"""
    global _rail_cookie
    with _station_lock:
        if _rail_cookie is not None:
            return _rail_cookie
        s = requests.Session()
        s.headers.update(_UA)
        s.get(RAIL_BASE + "/otn/leftTicket/init", timeout=_TIMEOUT)
        _rail_cookie = s
        return s


def _pick_stations(city: str):
    """选出该城市查询用车站：优先与城市同名的主站，其次常见大站。"""
    city = city.rstrip("市")
    sts = _city_stations.get(city, [])
    if not sts:
        return []
    exact = [s for s in sts if s[0] == city]
    prefer = [s for s in sts if s[0] in (f"{city}南", f"{city}西", f"{city}北", f"{city}东", f"{city}虹桥")]
    out = exact + [s for s in prefer if s not in exact] + [s for s in sts if s not in exact + prefer]
    return out[:3]


_SEAT_FIELDS = {  # row index -> 席位名
    32: "商务座", 31: "一等座", 30: "二等座", 33: "动卧",
    23: "软卧", 28: "硬卧", 29: "硬座", 26: "无座",
}


def query_trains(from_city: str, to_city: str, date: str = None, limit: int = 8):
    """查询真实车次余票。date: YYYY-MM-DD，默认取预售期内第 7 天。
    返回 {trains:[{code,dep,arr,hours,seats,kind}], date, src} 或 None。"""
    try:
        _load_station_map()
        sess = _rail_session()
        if date is None:
            date = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d")
        fr = _pick_stations(from_city)
        to = _pick_stations(to_city)
        if not fr or not to:
            return None
        trains = []
        used_date = date
        for f_name, f_code in fr[:2]:
            for t_name, t_code in to[:2]:
                try:
                    r = sess.get(
                        RAIL_BASE + "/otn/leftTicket/queryG",
                        params={
                            "leftTicketDTO.train_date": used_date,
                            "leftTicketDTO.from_station": f_code,
                            "leftTicketDTO.to_station": t_code,
                            "purpose_codes": "ADULT",
                        }, timeout=_TIMEOUT)
                    d = r.json()
                    result = (d.get("data") or {}).get("result") or []
                    if not result and d.get("c_url"):  # 接口端点轮换
                        ep = d["c_url"]
                        r = sess.get(RAIL_BASE + "/otn/" + ep, params={
                            "leftTicketDTO.train_date": used_date,
                            "leftTicketDTO.from_station": f_code,
                            "leftTicketDTO.to_station": t_code,
                            "purpose_codes": "ADULT",
                        }, timeout=_TIMEOUT)
                        d = r.json()
                        result = (d.get("data") or {}).get("result") or []
                    for row in result:
                        f = row.split("|")
                        if len(f) < 34 or f[11] not in ("Y", "IS_TIME_NOT_BUY"):
                            continue
                        code = f[3]
                        kind = ("高铁" if code[0] in "GDC" else "普速")
                        seats = {n: f[i] or "无" for i, n in _SEAT_FIELDS.items() if f[i] not in ("", "--")}
                        trains.append({
                            "code": code, "kind": kind,
                            "from": f_name, "to": t_name,
                            "dep": f[8], "arr": f[9],
                            "hours": _dur_hours(f[10]),
                            "seats": seats, "train_no": f[2],
                            "from_no": f[16], "to_no": f[17],
                            "seat_types": f[35] if len(f) > 35 else "",
                            "date": used_date,
                        })
                except Exception:
                    continue
            if len(trains) >= 4:
                break
        if not trains:
            return None
        trains.sort(key=lambda t: (t["hours"] or 99))
        return {"trains": trains[:limit], "date": used_date, "src": "12306"}
    except Exception:
        return None


def train_price(train: dict):
    """查某趟车的票价（元）。返回 {席位:价格} 或 None。"""
    try:
        sess = _rail_session()
        r = sess.get(RAIL_BASE + "/otn/leftTicket/queryTicketPrice", params={
            "train_no": train["train_no"],
            "from_station_no": train["from_no"],
            "to_station_no": train["to_no"],
            "seat_types": train.get("seat_types") or "",
            "train_date": train["date"],
        }, timeout=_TIMEOUT)
        data = (r.json().get("data") or {})
        seat_map = {"A9": "商务座", "P": "特等座", "M": "一等座", "O": "二等座",
                    "A6": "高级软卧", "A4": "软卧", "F": "动卧", "A3": "硬卧",
                    "A2": "软座", "A1": "硬座", "WZ": "无座"}
        prices = {}
        for k, v in data.items():
            if k in seat_map and isinstance(v, str):
                m = re.search(r"([\d.]+)", v)
                if m:
                    prices[seat_map[k]] = float(m.group(1))
        return prices or None
    except Exception:
        return None


def _dur_hours(s: str):
    try:
        h, m = s.split(":")
        return round(int(h) + int(m) / 60, 2)
    except Exception:
        return None


# ------------------------------------------------------------- web 搜索 ----

def _clean_html(s):
    import html as _html
    return _html.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()


def web_search_snippets(query: str, count: int = 3):
    """轻量网页搜索：Bing 主源，DuckDuckGo 兜底。返回 [{title,snippet,url}] 或 None。"""
    ua = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    try:
        r = requests.get("https://cn.bing.com/search", params={"q": query},
                         headers=ua, timeout=6)
        r.raise_for_status()
        out = []
        for b in re.findall(r'<li class="b_algo".*?</li>', r.text, re.S):
            m = re.search(r'<h2[^>]*>.*?<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', b, re.S)
            if not m:
                continue
            p = (re.search(r'<p[^>]*class="[^"]*b_lineclamp[^"]*"[^>]*>(.*?)</p>', b, re.S)
                 or re.search(r'<p[^>]*>(.*?)</p>', b, re.S))
            out.append({"title": _clean_html(m.group(2)), "url": m.group(1),
                        "snippet": _clean_html(p.group(1)) if p else ""})
            if len(out) >= count:
                break
        if out:
            return out
    except Exception:
        pass
    try:
        r = requests.get("https://html.duckduckgo.com/html/",
                         params={"q": query}, headers=ua, timeout=5)
        titles = re.findall(r'class="result__a"[^>]*>(.*?)</a>', r.text)
        snippets = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', r.text)
        urls = re.findall(r'class="result__a"[^>]*href="([^"]+)"', r.text)
        return [{"title": _clean_html(t),
                 "snippet": _clean_html(snippets[i]) if i < len(snippets) else "",
                 "url": urls[i] if i < len(urls) else ""}
                for i, t in enumerate(titles[:count])] or None
    except Exception:
        return None
