"""Agent 决策图：LangGraph StateGraph 驱动的两段式工作流。

  推荐图  resolve → scan → rank ──(有key)→ verdict ──→ END
                                 └──(无key)──────────→ END
  方案图  resolve → fetch_intel ──(有key)→ judge_llm ──┐
                                 └──(无key)→ judge_rule ┼→ compose ──(有key)→ polish → END
                                                              └──────(无key)──────────→ END

节点职责：工具节点只做数据搬运；LLM 节点做语义决策（复核排序 / 情报取舍 / 文案润色），
每次失败原地降级为规则结果并把降级事实写进 trace，前端可观测。
"""
import json
import operator
import time
import uuid
from datetime import datetime
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph

import llm
import tools


class S(TypedDict, total=False):
    req: dict                       # {lat,lng,city,budget,days,transport,prefs[,dest]}
    client_ip: str | None
    origin: dict
    scanned: list
    ranked: list
    verdict: dict                   # LLM 复核结论 {pick, why}
    intel: dict
    judged: dict
    judge_engine: str               # llm | rule | rule-fallback
    result: dict                    # compose_plans 输出
    message: str                    # 无可行目的地时的提示
    trace: Annotated[list, operator.add]


def _t(icon, title, detail=""):
    return {"id": uuid.uuid4().hex[:8], "icon": icon, "title": title,
            "detail": detail, "t": round(time.time(), 3)}


def _mode_label(m):
    return {"auto": "不限", "train": "高铁/火车", "flight": "飞机",
            "drive": "自驾"}.get(m, m)


# ------------------------------------------------------------- 公共节点 ----

def n_resolve(s: S):
    req = s["req"]
    origin = tools.resolve_origin(None, lat=req.get("lat"), lng=req.get("lng"),
                                  city=req.get("city"), client_ip=s.get("client_ip"))
    return {"origin": origin,
            "trace": [_t("pin", "解析出发位置",
                         f"{origin['note']} → {origin['name']}")]}


# ------------------------------------------------------------- 推荐图 ------

def n_scan(s: S):
    req = s["req"]
    scanned = tools.scan_destinations(None, s["origin"], req["budget"], req["days"],
                                      req.get("transport", "auto"),
                                      req.get("prefs") or [])
    feas = [x for x in scanned if x["feasible"]]
    detail = (f"共评估 {len(scanned)} 个目的地，{len(feas)} 个满足"
              f"「{_mode_label(req.get('transport','auto'))} + ¥{req['budget']}"
              f" + {req['days']}天」硬约束")
    out = {"scanned": scanned, "trace": [_t("scan", "扫描候选目的地", detail)]}
    if not feas:
        out["message"] = "当前条件下没有可行的目的地，建议提高预算或放宽出行方式"
    return out


def n_rank(s: S):
    req = s["req"]
    ranked = tools.rank_destinations(None, s["scanned"], req["budget"], req["days"],
                                     req.get("prefs") or [])
    return {"ranked": ranked,
            "trace": [_t("rank", "多因子打分排序",
                         "、".join(f"{r['city']}({r['score']})" for r in ranked[:5]))]}


def n_verdict(s: S):
    """LLM 决策节点①：复核规则排序，给出首推与逐城点评。"""
    req, ranked = s["req"], s["ranked"]
    payload = {
        "出发地": s["origin"]["name"], "预算": req["budget"], "天数": req["days"],
        "出行方式": _mode_label(req.get("transport", "auto")),
        "偏好": req.get("prefs") or [], "当前月份": datetime.now().month,
        "候选": [{"城市": r["city"], "规则得分": r["score"], "距离km": round(r["km"]),
                  "标签": r["tags"], "预估总花费": r["rough_total"],
                  "交通": r["transport_est"]["desc"]} for r in ranked],
    }
    d = llm.chat_json(
        "你是资深旅行规划师。下面是规则引擎按预算/天数/季节/偏好打分排序的目的地候选。"
        "请复核：结合时令、偏好契合度与性价比，选出你的首推（可不同于得分第一名），"
        "并对每个候选给一句点评。",
        json.dumps(payload, ensure_ascii=False)
        + '\n输出格式：{"pick":"首选城市名(必须来自候选)","why":"≤40字推荐理由",'
          '"comments":{"城市名":"≤25字点评"}}')
    valid = {r["city"] for r in ranked}
    if not d:
        return {"trace": [_t("brain", "LLM 复核排序", "模型未响应，采用规则排序")]}
    pick = d.get("pick") if d.get("pick") in valid else None
    comments = d.get("comments") if isinstance(d.get("comments"), dict) else {}
    for r in ranked:
        c = comments.get(r["city"])
        if isinstance(c, str) and c.strip():
            r["ai_comment"] = c.strip()[:40]
        if pick and r["city"] == pick:
            r["llm_pick"] = True
    verdict = {"pick": pick, "why": str(d.get("why") or "")[:60]}
    return {"ranked": ranked, "verdict": verdict,
            "trace": [_t("brain", "LLM 复核排序",
                         (f"首推「{pick}」：{verdict['why']}" if pick
                          else "维持规则排序，无额外首推"))]}


def _scan_has_feasible(s: S):
    return "rank" if any(x["feasible"] for x in s["scanned"]) else END


def _llm_or_end(s: S):
    return "verdict" if llm.llm_available() else END


def build_recommend():
    g = StateGraph(S)
    g.add_node("resolve", n_resolve)
    g.add_node("scan", n_scan)
    g.add_node("rank", n_rank)
    g.add_node("verdict", n_verdict)
    g.add_edge(START, "resolve")
    g.add_edge("resolve", "scan")
    g.add_conditional_edges("scan", _scan_has_feasible, {"rank": "rank", END: END})
    g.add_conditional_edges("rank", _llm_or_end, {"verdict": "verdict", END: END})
    g.add_edge("verdict", END)
    return g.compile()


# ------------------------------------------------------------- 方案图 ------

def n_fetch_intel(s: S):
    intel = tools.fetch_intel(None, s["req"]["dest"])
    return {"intel": intel,
            "trace": [_t("search", f"搜索「{s['req']['dest']}」当地特色与攻略",
                         "数据来源：" + " + ".join(intel["sources"]))]}


def n_judge_rule(s: S):
    req = s["req"]
    judged = tools.judge_intel(None, s["intel"], req["days"], req.get("prefs") or [])
    return {"judged": judged, "judge_engine": "rule",
            "trace": [_t("judge", "情报判断与取舍（规则引擎）",
                         _judge_detail(judged))]}


def n_judge_llm(s: S):
    """LLM 决策节点②：读攻略摘要+天气+POI，决定景点取舍。失败降级规则。"""
    req, intel = s["req"], s["intel"]
    c = intel["city"]
    payload = {
        "城市": c["name"], "旅行天数": req["days"], "当前月份": datetime.now().month,
        "用户偏好": req.get("prefs") or [],
        "实时天气": intel.get("weather"),
        "攻略摘要": [{"标题": w["title"], "摘要": (w.get("snippet") or "")[:120]}
                     for w in (intel.get("web") or [])[:3]],
        "高德热门景点": [p["name"] for p in (intel.get("pois_scenic") or [])[:6]],
        "候选景点": [{"名称": a["n"], "简介": a["desc"], "建议时长h": a["hours"],
                      "门票": a["ticket"], "推荐度": a["must"]}
                     for a in c["attractions"]],
    }
    d = llm.chat_json(
        f"你是{c['name']}的资深当地向导。根据实时情报和用户条件，从候选景点中决定"
        "本次行程保留哪些、剔除哪些。取舍原则：天数有限必做取舍；不合时令/性价比低"
        "的剔除；恶劣天气倾向室内项目；优先契合用户偏好。",
        json.dumps(payload, ensure_ascii=False)
        + '\n输出格式：{"keep":["要保留的景点名"],"drop":[{"name":"剔除景点名",'
          '"reason":"≤20字理由"}],"notes":["≤30字的当地提示，最多4条"]}')
    judged = tools.apply_llm_judge(intel, req["days"], req.get("prefs") or [], d) if d else None
    if judged is None:                      # LLM 失败或输出不合法 → 规则兜底
        judged = tools.judge_intel(None, intel, req["days"], req.get("prefs") or [])
        return {"judged": judged, "judge_engine": "rule-fallback",
                "trace": [_t("judge", "情报判断与取舍（LLM失败→规则兜底）",
                             _judge_detail(judged))]}
    return {"judged": judged, "judge_engine": "llm",
            "trace": [_t("judge", "情报判断与取舍（LLM 决策）",
                         _judge_detail(judged))]}


def _judge_detail(judged):
    kept_n, drop_n = len(judged["kept"]), len(judged["dropped"])
    why = "；".join(d["reason"] for d in judged["dropped"][:2]) or "无"
    return f"保留 {kept_n} 个核心体验，剔除 {drop_n} 项（{why}）"


def n_compose(s: S):
    req = s["req"]
    rail_hint = ("（正在查询12306真实车次…）"
                 if req.get("transport") in (None, "auto", "train") else "")
    result = tools.compose_plans(None, s["origin"], req["dest"], req["budget"],
                                 req["days"], req.get("transport", "auto"),
                                 req.get("prefs") or [], s["judged"])
    ti = result["transport"]
    return {"result": result, "trace": [
        _t("rail", "查询去程/回程交通",
           f"去程 {ti['outbound']}｜回程 {ti['back']}"
           f"（{'12306实时' if ti['src'] == '12306' else '估算'}）{rail_hint}"),
        _t("plan", f"生成 {len(result['plans'])} 套出游方案",
           "、".join(p["name"] for p in result["plans"]))]}


def n_polish(s: S):
    """LLM 节点③：给每套方案写推荐语（ai_note），不改结构数据。"""
    req, result = s["req"], s["result"]
    plans = result["plans"]
    payload = {
        "城市": req["dest"], "天数": req["days"], "用户偏好": req.get("prefs") or [],
        "方案": [{"id": p["id"], "名称": p["name"], "定位": p["desc"],
                  "节奏": p["pace"], "总价": p["budget"]["total"],
                  "行程": ["D%d %s" % (d["day"], "、".join(
                      i["name"] for i in d["items"] if i["type"] == "景点"))
                      for d in p["days"]]} for p in plans],
    }
    d = llm.chat_json(
        "你是旅行文案编辑。给每套方案写一条推荐语：≤45字，点明适合什么人/亮点在哪，"
        "口语化，不夸大，不用emoji。",
        json.dumps(payload, ensure_ascii=False)
        + '\n输出格式：{"notes":{"方案id":"推荐语"}}', temperature=0.6)
    notes = d.get("notes") if isinstance(d, dict) else None
    n = 0
    if isinstance(notes, dict):
        for p in plans:
            v = notes.get(p["id"])
            if isinstance(v, str) and v.strip():
                p["ai_note"] = v.strip()[:60]
                n += 1
    return {"result": result,
            "trace": [_t("pen", "AI 文案润色",
                         f"为 {n} 套方案生成推荐语" if n else "模型未响应，跳过")]}


def _judge_route(s: S):
    return "judge_llm" if llm.llm_available() else "judge_rule"


def _polish_or_end(s: S):
    return "polish" if llm.llm_available() else END


def build_plan():
    g = StateGraph(S)
    g.add_node("resolve", n_resolve)
    g.add_node("fetch_intel", n_fetch_intel)
    g.add_node("judge_llm", n_judge_llm)
    g.add_node("judge_rule", n_judge_rule)
    g.add_node("compose", n_compose)
    g.add_node("polish", n_polish)
    g.add_edge(START, "resolve")
    g.add_edge("resolve", "fetch_intel")
    g.add_conditional_edges("fetch_intel", _judge_route,
                            {"judge_llm": "judge_llm", "judge_rule": "judge_rule"})
    g.add_edge("judge_llm", "compose")
    g.add_edge("judge_rule", "compose")
    g.add_conditional_edges("compose", _polish_or_end, {"polish": "polish", END: END})
    g.add_edge("polish", END)
    return g.compile()


# ------------------------------------------------------------- 编译缓存 ----

_rec_graph = None
_plan_graph = None


def recommend_graph():
    global _rec_graph
    if _rec_graph is None:
        _rec_graph = build_recommend()
    return _rec_graph


def plan_graph():
    global _plan_graph
    if _plan_graph is None:
        _plan_graph = build_plan()
    return _plan_graph
