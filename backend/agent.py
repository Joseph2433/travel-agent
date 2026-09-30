"""旅行规划 Agent：以 计划→工具调用→观察→判断 的循环执行多阶段任务，
每一步产出可观测的 trace，供前端展示"思考过程"。"""
import time
import uuid

import tools


def _mode_label(m):
    return {"auto": "不限", "train": "高铁/火车", "flight": "飞机", "drive": "自驾"}.get(m, m)


class TravelAgent:
    def __init__(self):
        self.trace = []

    def _step(self, icon, title, detail=""):
        self.trace.append({"id": uuid.uuid4().hex[:8], "icon": icon,
                           "title": title, "detail": detail,
                           "t": round(time.time(), 3)})

    # ---------------- 阶段一：目的地推荐 ----------------
    def run_recommend(self, req, client_ip=None):
        """req: {lat,lng,city,budget,days,transport,prefs}"""
        self.trace = []
        budget, days = req["budget"], req["days"]
        transport, prefs = req.get("transport", "auto"), req.get("prefs") or []

        self._step("pin", "解析出发位置", "调用定位工具…")
        origin = tools.resolve_origin(self, lat=req.get("lat"), lng=req.get("lng"),
                                      city=req.get("city"), client_ip=client_ip)
        self.trace[-1]["detail"] = f"{origin['note']} → {origin['name']}"

        self._step("scan", "扫描候选目的地", "")
        scanned = tools.scan_destinations(self, origin, budget, days, transport, prefs)
        feas = [s for s in scanned if s["feasible"]]
        self.trace[-1]["detail"] = (
            f"共评估 {len(scanned)} 个目的地，{len(feas)} 个满足"
            f"「{_mode_label(transport)} + ¥{budget} + {days}天」硬约束")
        if not feas:
            return {"trace": self.trace, "origin": origin, "destinations": [],
                    "message": "当前条件下没有可行的目的地，建议提高预算或放宽出行方式"}

        self._step("rank", "多因子打分排序", "")
        ranked = tools.rank_destinations(self, scanned, budget, days, prefs)
        self.trace[-1]["detail"] = "、".join(
            f"{r['city']}({r['score']})" for r in ranked[:5])
        return {"trace": self.trace, "origin": origin, "destinations": ranked}

    # ---------------- 阶段二：目的地搜索 + 方案生成 ----------------
    def run_plan(self, req, client_ip=None):
        """req: 同上 + dest"""
        self.trace = []
        budget, days, dest = req["budget"], req["days"], req["dest"]
        transport, prefs = req.get("transport", "auto"), req.get("prefs") or []

        self._step("pin", "解析出发位置", "")
        origin = tools.resolve_origin(self, lat=req.get("lat"), lng=req.get("lng"),
                                      city=req.get("city"), client_ip=client_ip)
        self.trace[-1]["detail"] = f"{origin['note']} → {origin['name']}"

        self._step("search", f"搜索「{dest}」当地特色与攻略", "")
        intel = tools.fetch_intel(self, dest)
        self.trace[-1]["detail"] = "数据来源：" + " + ".join(intel["sources"])

        self._step("judge", "情报判断与取舍", "")
        judged = tools.judge_intel(self, intel, days, prefs)
        kept_n = len(judged["kept"])
        drop_n = len(judged["dropped"])
        self.trace[-1]["detail"] = (
            f"保留 {kept_n} 个核心体验，剔除 {drop_n} 项（"
            + "；".join(d["reason"] for d in judged["dropped"][:2] or [{"reason": "无"}]) + "）")

        rail_hint = "（正在查询12306真实车次…）" if transport in ("auto", "train") else ""
        self._step("rail", "查询去程/回程交通", rail_hint)
        result = tools.compose_plans(self, origin, dest, budget, days,
                                     transport, prefs, judged)
        ti = result["transport"]
        self.trace[-1]["detail"] = (
            f"去程 {ti['outbound']}｜回程 {ti['back']}"
            f"（{'12306实时' if ti['src']=='12306' else '估算'}）")

        self._step("plan", f"生成 {len(result['plans'])} 套出游方案", "")
        self.trace[-1]["detail"] = "、".join(p["name"] for p in result["plans"])

        return {
            "trace": self.trace, "origin": origin, "dest": dest,
            "km": result["km"], "resolved_mode": result["resolved_mode"],
            "intel": {
                "sources": intel["sources"], "weather": intel.get("weather"),
                "web": intel.get("web"), "notes": judged["notes"],
                "dropped": judged["dropped"], "tips": judged["tips"],
            },
            "plans": result["plans"],
        }
