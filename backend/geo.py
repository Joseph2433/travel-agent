"""地理与交通估算模型：直线距离、各出行方式的时间/费用估算、可行性判断。"""
import math

TRANSPORT_MODES = {
    "auto":   {"label": "不限(智能推荐)"},
    "train":  {"label": "高铁/火车"},
    "flight": {"label": "飞机"},
    "drive":  {"label": "自驾"},
}


def haversine_km(lat1, lng1, lat2, lng2) -> float:
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return round(2 * R * math.asin(math.sqrt(a)), 1)


def _rail_km(km):   # 铁路里程约比直线多 25%
    return km * 1.25


def _road_km(km):   # 公路里程约比直线多 30%
    return km * 1.3


def estimate_transport(km: float, mode: str, city: dict = None):
    """按出行方式估算单程 {feasible, hours, cost, desc}。
    mode: auto/train/flight/drive。auto 时按距离推荐最优方式。"""
    if mode == "auto":
        if km <= 350:
            mode = "train" if (city or {}).get("hsRail", True) else "drive"
        elif km <= 1200:
            mode = "train" if (city or {}).get("hsRail", True) else "flight"
        else:
            mode = "flight" if (city or {}).get("airport", True) else "train"

    if mode == "train":
        rail = _rail_km(km)
        hs = (city or {}).get("hsRail", True)
        if rail > 2800:
            return {"mode": "train", "feasible": False, "reason": "距离过远，铁路耗时过长，建议飞机"}
        if hs:
            hours = rail / 240 + 0.8          # 高铁均速约240含进出站
            cost = rail * 0.46                 # 二等座约0.46元/公里
            desc = "高铁二等座"
        else:
            hours = rail / 90 + 1.0
            cost = rail * 0.22                 # 普速硬卧均值
            desc = "普速列车"
        return {"mode": "train", "feasible": True, "hours": round(hours, 1),
                "cost": round(cost), "desc": desc}

    if mode == "flight":
        if not (city or {}).get("airport", True):
            return {"mode": "flight", "feasible": False, "reason": "目的地无机场，需中转"}
        if km < 500:
            return {"mode": "flight", "feasible": False, "reason": "距离太近，高铁更划算"}
        hours = km * 1.12 / 800 + 2.5          # 巡航+机场往返安检
        cost = 380 + km * 1.12 * 0.62          # 经济舱全价均值的折扣估计
        return {"mode": "flight", "feasible": True, "hours": round(hours, 1),
                "cost": round(cost), "desc": "经济舱(估)"}

    if mode == "drive":
        road = _road_km(km)
        if road > 1600:
            return {"mode": "drive", "feasible": False, "reason": "超过舒适自驾里程(1200km)"}
        hours = road / 85 + 0.5                # 高速均速含休息
        cost = road * 0.55 + road * 0.45       # 油费+高速费
        days_needed = math.ceil(road / 600)    # 每天舒适驾驶上限
        return {"mode": "drive", "feasible": True, "hours": round(hours, 1),
                "cost": round(cost), "desc": f"自驾约{round(road)}km",
                "driveDays": days_needed, "roadKm": round(road)}

    return {"mode": mode, "feasible": False, "reason": "未知出行方式"}


def trip_budget(dest: dict, days: int, transport_cost_one_way: float,
                hotel_factor: float = 1.0, food_factor: float = 1.0,
                attraction_ticket_sum: float = 0):
    """组装一份行程的总预算（单人）。"""
    nights = max(days - 1, 0)
    hotel = dest["hotelPerNight"] * nights * hotel_factor
    food = dest["foodPerDay"] * days * food_factor
    local = dest["localPerDay"] * days
    transport = transport_cost_one_way * 2
    total = round(transport + hotel + food + local + attraction_ticket_sum)
    return {
        "transport": round(transport), "hotel": round(hotel),
        "food": round(food), "local": round(local),
        "tickets": round(attraction_ticket_sum), "total": total,
        "per_day": round(total / max(days, 1)),
    }
