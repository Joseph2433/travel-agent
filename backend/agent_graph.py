"""Agent 决策图：LangGraph 驱动的两段式工作流。

【有 LLM key】模型是大脑 —— ReAct 循环自主调工具拿数据、推理、输出结构化决策；
              代码只做校验（名字必须来自知识库）与算术（预算/票价永不由模型生成）。
  推荐图  resolve → agent_rank(ReAct: scan/profile → 排序+点评+首推) → END
  方案图  resolve → agent_plan(ReAct: profile/intel/车次/预算 → 3-5套行程) → assemble → END
【无 LLM key】固定流水线兜底：
  推荐图  resolve → scan → rank → END
  方案图  resolve → fetch_intel → judge_rule → compose → END

模型每次推理/工具调用/观察都会变成 trace 步，前端可回放完整思考过程。
"""
import json
import operator
import time
import uuid
from datetime import datetime
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph

import apis
import geo
import llm
import schemas
import tools


class S(TypedDict, total=False):
    req: dict                       # {lat,lng,city,budget,days,transport,prefs[,dest]}
    client_ip: str | None
    origin: dict
    scanned: list
    ranked: list
    more: list                      # 精选 6 个之外的其余可达候选（查看更多）
    total_feasible: int
    verdict: dict
    intel: dict
    judged: dict
    guide: dict                     # 目的地详细攻略（多源交叉验证组装）
    judge_engine: str               # llm-agent | rule | rule-fallback
    agent_out: object               # LLM 结构化输出（PlansOut/RecOut 实例）
    result: dict                    # {plans, transport, km, resolved_mode}
    tool_data: dict                 # ReAct 过程中工具返回的原始数据（按工具名）
    message: str
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


# ------------------------------------------------- ReAct（LLM 是大脑） -----

_rec_agent_g = None
_plan_agent_g = None


def _chat_model():
    from langchain_openai import ChatOpenAI
    return ChatOpenAI(model=llm.LLM_MODEL, api_key=llm.LLM_API_KEY,
                    base_url=llm.LLM_BASE_URL.rstrip("/"),
                    timeout=llm._TIMEOUT, max_tokens=llm._MAX_TOKENS,
                    temperature=0.3)


def _rec_agent():
    global _rec_agent_g
    if _rec_agent_g is None:
        from langgraph.prebuilt import create_react_agent
        _rec_agent_g = create_react_agent(
            _chat_model(), tools=__import__("agent_tools").REC_TOOLS,
            prompt=REC_PROMPT)
    return _rec_agent_g


def _plan_agent():
    global _plan_agent_g
    if _plan_agent_g is None:
        from langgraph.prebuilt import create_react_agent
        _plan_agent_g = create_react_agent(
            _chat_model(), tools=__import__("agent_tools").PLAN_TOOLS,
            prompt=PLAN_PROMPT)
    return _plan_agent_g


REC_PROMPT = """你是「旅图」旅行规划 Agent 的目的地决策大脑。

工作方式：先用工具拿真实数据，推理决策，最后必须调用 submit_result 提交结论。
1) 调 scan_destinations 获取可达候选：若用户消息中"圈定省份"不是"不限"，
   必须把该列表传给 scan_destinations 的 provinces 参数，且最终只推荐其中的城市。
   in_kb=true 是知识库精选城市；in_kb=false 是其他城市（包括小众目的地）——
   两者都可推荐，别只挑热门
2) 对感兴趣的城市：知识库城调 get_city_profile 看景点/美食/佳季；
   小众城市可改调 get_city_intel 看它的实时POI密度/天气/攻略热度再判断；
   还可调 travel_trends 看微博/抖音/小红书热搜，正在热议的目的地/玩法可优先
3) 综合维度：预算契合、天数匹配（每天约2-3个景点）、当前月份 vs 佳季、偏好命中、
   交通效率、热榜热度

最后用 submit_result 提交，payload 结构：
{"ranking":[{"city":"城市名","score":0-100整数,"comment":"≤25字点评"},...3-6条],
 "pick":"首推城市名(必须在ranking中)","why":"≤40字理由"}

硬约束：只推荐工具返回中 fits_budget=true 或略超预算(over_ratio≤1.3)的城市。"""

PLAN_PROMPT = """你是「旅图」旅行规划 Agent 的行程编排大脑。

工作方式：严格按"攻略先行 → 多源验证 → 编排填充"的顺序调研，最后必须调用
submit_result 提交结论。

1) 攻略先行：优先调 search_xhs_notes 搜「目的地 旅游攻略」「目的地 N日游路线」
   拿真实游客笔记作为攻略骨架（必去景点/必吃美食/避雷点/路线节奏）；需要深挖
   店名或避雷细节时加 with_detail=true 拿正文摘录。小红书未配置时跳过，改看
   get_city_intel 的网页攻略摘要。
2) 骨架验证：get_city_profile 看知识库景点/美食/贴士/消费档；get_city_intel 拿
   实时天气、高德POI、去哪儿真实票价(scenic_qunar.ticket 是真实挂牌价)。
   笔记里出现的店名/景点，先用 search_pois 核实真实存在再编进方案；
   需要特定类别（博物馆/夜市/古镇/亲子等）时用 search_pois 主动补充。
3) 交通：出行方式为 train/auto 时调 query_trains 拿真实车次票价；
   其他方式调 estimate_transport。
4) 编排 3-5 套差异化方案（主题如：经典全景/寻味美食/深度慢游/精华快闪/舒适度假，
   可按目的地特点自由命名）：
   - 每套 days 数量 = 用户天数；每天排 景点/美食/休闲 槽位（上午/下午/晚上/全天/午餐/晚餐）
   - 景点名可来自知识库 attractions 或 get_city_intel/search_pois 返回的高德POI名称；
     美食名可来自知识库 foods 或高德美食POI；都可混用，优先选评分高的真实POI
   - 不要排往返交通项（系统会自动插入首尾两天）；抵达日少排点、返程日只排上午
   - hotel_factor(0.8穷游/1.0标准/1.3+舒适) 和 food_factor 用来区分方案档位
   - 可以调 calc_budget 自检每套方案总价是否贴合预算，
     门票优先用 scenic_qunar 的真实票价求和

最后用 submit_result 提交，payload 结构：
{"plans":[{"name":"≤8字方案名","pace":"节奏","desc":"≤25字定位","hotel_factor":1.0,
           "food_factor":1.0,"days":[{"day":1,"title":"当日标题","items":[
           {"slot":"上午","type":"景点","name":"知识库中的精确名称"}]}],
           "guide":"本套方案的详细攻略正文(###小节/-列表/**重点**，300-600字)：
                    路线怎么玩、门票怎么约、美食去哪吃、避雷提醒——
                    只写刚才工具调研核实过的信息，不编造店名和数字"}],
 "notes":["出行提示3-5条，注明源自攻略还是实况"],
 "dropped":[{"name":"剔除景点名","reason":"理由"}]}

硬约束：真实景点/美食名只能从工具数据中选，不要编造。"""


def _msg_trace(msgs):
    """把 ReAct 消息流翻译成前端 trace 步：思考 / 调工具 / 观察。"""
    steps = []
    for m in msgs:
        mtype = getattr(m, "type", "")
        if mtype == "ai":
            rc = (getattr(m, "additional_kwargs", None) or {}).get("reasoning_content")
            if isinstance(rc, str) and rc.strip():
                steps.append(_t("think", "模型推理",
                                " ".join(rc.split())[:110] + "…"))
            for tc in getattr(m, "tool_calls", None) or []:
                args = json.dumps(tc.get("args") or {}, ensure_ascii=False)
                steps.append(_t("tool", f"调用工具 {tc['name']}", args[:80]))
        elif mtype == "tool":
            body = m.content if isinstance(m.content, str) else str(m.content)
            steps.append(_t("observe", f"观察 {getattr(m,'name','tool')} 返回",
                            " ".join(body.split())[:80]))
    return steps


def _collect_tool_data(msgs):
    """从消息流里回收工具返回数据：{工具名: [各次调用的返回值]}。"""
    data = {}
    for m in msgs:
        if getattr(m, "type", "") == "tool" and getattr(m, "name", None):
            try:
                v = json.loads(m.content)
            except Exception:
                v = m.content
            data.setdefault(m.name, []).append(v)
    return data


def _last_td(tool_data, name):
    lst = tool_data.get(name) or []
    return lst[-1] if lst else None


def _writer():
    """外层图以 stream_mode='custom' 运行时可用；非流式调用返回 None。"""
    try:
        from langgraph.config import get_stream_writer
        return get_stream_writer()
    except Exception:
        return None


def _run_react(agent, user_msg):
    """流式执行 ReAct 子图：每出现一条新消息（思考/工具调用/观察）立刻经
    custom writer 推给 SSE 通道。返回 (messages, 异常或None, 已推送的steps)。"""
    w = _writer()
    msgs, steps = [], []
    try:
        for chunk in agent.stream({"messages": [("user", user_msg)]},
                                  config={"recursion_limit": 30},
                                  stream_mode="updates"):
            for _node, upd in chunk.items():
                if not isinstance(upd, dict):
                    continue
                for m in upd.get("messages") or []:
                    msgs.append(m)
                    for st in _msg_trace([m]):
                        steps.append(st)
                        if w:
                            w({"type": "step", **st})
        return msgs, None, steps
    except Exception as e:
        return msgs, e, steps


def _extract_structured(msgs, cls):
    """从消息流提取 submit_result 工具调用的 payload，校验为 pydantic 模型；
    模型没调用时兜底解析最后一条 AI 文本中的 JSON。"""
    for m in reversed(msgs):
        if getattr(m, "type", "") != "ai":
            continue
        for tc in getattr(m, "tool_calls", None) or []:
            if tc.get("name") == "submit_result" and isinstance(
                    tc.get("args"), dict):
                try:
                    return cls(**tc["args"].get("payload", tc["args"]))
                except Exception:
                    return None
    for m in reversed(msgs):                        # 兜底：最后一条AI文本里的JSON
        if getattr(m, "type", "") == "ai" and getattr(m, "content", None):
            txt = m.content if isinstance(m.content, str) else ""
            i, j = txt.find("{"), txt.rfind("}")
            if i >= 0 and j > i:
                try:
                    return cls(**json.loads(txt[i:j + 1]))
                except Exception:
                    return None
    return None


# --------------------------------------------- 推荐图：LLM 自主决策 --------

def n_agent_rank(s: S):
    """LLM Agent：自主调工具调研候选，输出排序+点评+首推。"""
    req, origin = s["req"], s["origin"]
    user_msg = json.dumps({
        "出发地": origin["name"], "预算": req["budget"], "天数": req["days"],
        "出行方式": req.get("transport", "auto"), "偏好": req.get("prefs") or [],
        "圈定省份": req.get("provinces") or "不限（全国可达范围）",
        "当前月份": datetime.now().month,
    }, ensure_ascii=False)

    msgs, err, steps = _run_react(_rec_agent(), user_msg)
    out = _extract_structured(msgs, schemas.RecOut)
    if err:
        steps.append(_t("brain", "LLM 调研", f"调用失败：{str(err)[:60]}，转规则引擎"))
        return _rule_rank_pipeline(s, steps)

    ranking = getattr(out, "ranking", None) if out else None
    if not ranking:
        steps.append(_t("brain", "LLM 调研", "输出不合法，转规则引擎"))
        return _rule_rank_pipeline(s, steps)

    # 合并：模型给排序/分数/点评/首推，代码回填真实距离/费用/交通
    scanned = {x["city"]: x for x in
               tools.scan_destinations(None, origin, req["budget"], req["days"],
                                       req.get("transport", "auto"),
                                       req.get("prefs") or [],
                                       provinces=req.get("provinces") or None)
               if x["feasible"]}
    cities = {c["name"]: c for c in tools.load_cities()}
    dests = []
    for item in ranking[:6]:
        name = item.city.rstrip("市")
        sc = scanned.get(name)
        if not sc:
            continue
        c = cities.get(name) or tools.pool_profile(
            name, sc.get("province", ""), sc.get("lat"), sc.get("lng"))
        in_kb = name in cities
        reasons = _rule_reasons(sc, c, req)
        if not in_kb:
            reasons.append("小众目的地 · 行程由实时POI编排")
        dests.append({
            "city": name, "province": c["province"] or sc.get("province", ""),
            "score": max(0, min(100, int(item.score))),
            "km": sc["km"], "lat": c["lat"], "lng": c["lng"],
            "tags": c["tags"], "in_kb": in_kb,
            "transport_est": sc["transport_est"],
            "rough_total": sc["rough_total"], "hotelPerNight": c["hotelPerNight"],
            "reasons": reasons,
            "ai_comment": (item.comment or "")[:40],
            "llm_pick": name == (getattr(out, "pick", "") or "").rstrip("市"),
        })
    if len(dests) < 3:
        steps.append(_t("brain", "LLM 调研", "有效推荐不足3个，转规则引擎"))
        return _rule_rank_pipeline(s, steps)

    verdict = {"pick": (out.pick or "").rstrip("市"),
               "why": str(out.why or "")[:60]}
    steps.append(_t("brain", "LLM 决策完成",
                    f"调研 {len([m for m in msgs if getattr(m,'type','')=='tool'])} 次工具，"
                    f"首推「{verdict['pick'] or dests[0]['city']}」"))
    picked = {d["city"] for d in dests}
    extra = [r for r in tools.rank_destinations(
                 None, list(scanned.values()), req["budget"], req["days"],
                 req.get("prefs") or [])
             if r["city"] not in picked]
    return {"ranked": dests, "more": extra[:24],
            "total_feasible": len(scanned),
            "verdict": verdict, "trace": steps}


def _rule_reasons(sc, c, req):
    """给 LLM 选中的城市补上数字理由条（预算/天数/季节/偏好）。"""
    reasons, month = [], datetime.now().month
    if sc["rough_total"] <= req["budget"]:
        reasons.append(f"预算内（约¥{sc['rough_total']}）")
    else:
        reasons.append(f"略超预算（约¥{sc['rough_total']}）")
    if month in c["bestMonths"]:
        reasons.append("正值佳季")
    hit = set(req.get("prefs") or []) & set(c["tags"])
    if hit:
        reasons.append("契合偏好：" + "、".join(hit))
    return reasons


def _rule_rank_pipeline(s: S, prior_steps):
    """规则兜底：scan+rank 内联执行。"""
    req = s["req"]
    scanned = tools.scan_destinations(None, s["origin"], req["budget"], req["days"],
                                      req.get("transport", "auto"),
                                      req.get("prefs") or [],
                                      provinces=req.get("provinces") or None)
    feas = [x for x in scanned if x["feasible"]]
    steps = prior_steps + [
        _t("scan", "扫描候选目的地",
           f"共评估 {len(scanned)} 个目的地，{len(feas)} 个满足硬约束")]
    out = {"scanned": scanned, "trace": steps}
    if not feas:
        out["message"] = "当前条件下没有可行的目的地，建议提高预算或放宽出行方式"
        return out
    ranked = tools.rank_destinations(None, scanned, req["budget"], req["days"],
                                     req.get("prefs") or [])
    out["ranked"], out["more"] = ranked[:6], ranked[6:30]
    out["total_feasible"] = len(feas)
    out["trace"] = steps + [_t("rank", "多因子打分排序",
                              "、".join(f"{r['city']}({r['score']})" for r in ranked[:5]))]
    return out


# 规则流水线节点（无 key 时使用）---------------------------------------------

def n_scan(s: S):
    req = s["req"]
    scanned = tools.scan_destinations(None, s["origin"], req["budget"], req["days"],
                                      req.get("transport", "auto"),
                                      req.get("prefs") or [],
                                      provinces=req.get("provinces") or None)
    feas = [x for x in scanned if x["feasible"]]
    out = {"scanned": scanned,
           "trace": [_t("scan", "扫描候选目的地",
                        f"共评估 {len(scanned)} 个目的地，{len(feas)} 个满足"
                        f"「{_mode_label(req.get('transport','auto'))} + ¥{req['budget']}"
                        f" + {req['days']}天」硬约束")]}
    if not feas:
        out["message"] = "当前条件下没有可行的目的地，建议提高预算或放宽出行方式"
    return out


def n_rank(s: S):
    req = s["req"]
    ranked = tools.rank_destinations(None, s["scanned"], req["budget"], req["days"],
                                     req.get("prefs") or [])
    return {"ranked": ranked[:6], "more": ranked[6:30],
            "total_feasible": sum(1 for x in s["scanned"] if x["feasible"]),
            "trace": [_t("rank", "多因子打分排序",
                         "、".join(f"{r['city']}({r['score']})" for r in ranked[:5]))]}


def _route_rec(s: S):
    return "agent_rank" if llm.llm_available() else "scan"


def _scan_has_feasible(s: S):
    return "rank" if any(x["feasible"] for x in s["scanned"]) else END


def build_recommend():
    g = StateGraph(S)
    g.add_node("resolve", n_resolve)
    g.add_node("agent_rank", n_agent_rank)
    g.add_node("scan", n_scan)
    g.add_node("rank", n_rank)
    g.add_edge(START, "resolve")
    g.add_conditional_edges("resolve", _route_rec,
                            {"agent_rank": "agent_rank", "scan": "scan"})
    g.add_edge("agent_rank", END)
    g.add_conditional_edges("scan", _scan_has_feasible, {"rank": "rank", END: END})
    g.add_edge("rank", END)
    return g.compile()


# --------------------------------------------- 方案图：LLM 自主编排 --------

def n_agent_plan(s: S):
    """LLM Agent：自主调研目的地（画像/情报/车次/预算），输出 3-5 套行程。"""
    req, origin = s["req"], s["origin"]
    user_msg = json.dumps({
        "出发地": origin["name"], "目的地": req["dest"], "预算": req["budget"],
        "天数": req["days"], "出行方式": req.get("transport", "auto"),
        "偏好": req.get("prefs") or [], "当前月份": datetime.now().month,
    }, ensure_ascii=False)

    msgs, err, steps = _run_react(_plan_agent(), user_msg)
    tool_data = _collect_tool_data(msgs)
    out = _extract_structured(msgs, schemas.PlansOut)
    if err or not out or not getattr(out, "plans", None):
        why = f"调用失败：{str(err)[:60]}" if err else "输出不合法"
        steps.append(_t("judge", "LLM 编排", f"{why}，转规则引擎"))
        # 原地降级到规则流水线（仅知识库城市有兜底素材）
        kb = next((x for x in tools.load_cities()
                   if x["name"] == req["dest"].rstrip("市")), None)
        if kb is None:
            return {"intel": {"sources": [], "weather": None, "web": None,
                              "city": {"name": req["dest"], "tips": []}},
                    "judged": {"notes": ["该目的地不在知识库，且模型编排失败"],
                               "dropped": [], "tips": [], "foods": [], "kept": []},
                    "result": {"plans": [], "transport": {}, "km": 0,
                               "resolved_mode": req.get("transport", "auto")},
                    "judge_engine": "rule-fallback", "tool_data": tool_data,
                    "trace": steps}
        intel = tools.fetch_intel(None, req["dest"])
        judged = tools.judge_intel(None, intel, req["days"], req.get("prefs") or [])
        result = tools.compose_plans(None, origin, req["dest"], req["budget"],
                                     req["days"], req.get("transport", "auto"),
                                     req.get("prefs") or [], judged)
        return {"intel": intel, "judged": judged, "result": result,
                "guide": tools.build_guide(kb, intel, judged,
                                           tool_data=tool_data,
                                           transport=result.get("transport")),
                "judge_engine": "rule-fallback", "tool_data": tool_data,
                "trace": steps + _compose_trace(result, req)}

    return {"agent_out": out, "tool_data": tool_data, "trace": steps}


def n_assemble(s: S):
    """组装校验：模型只给了名字/结构，这里回填真实票价/时长/简介/预算。
    agent_plan 若已原地降级（无 agent_out），结果已齐，本节点直通。"""
    out = s.get("agent_out")
    if out is None:
        return {}
    req, origin = s["req"], s["origin"]
    tool_data = s.get("tool_data") or {}
    dest_name = req["dest"].rstrip("市")
    kb = next((x for x in tools.load_cities() if x["name"] == dest_name), None)
    c = kb or _pseudo_city(dest_name)

    transport, km, mode = _resolve_transport(origin, c, req, tool_data)
    att_map = {a["n"]: a for a in c["attractions"]}
    food_map = {f["n"]: f for f in c["foods"]}
    poi_map = _poi_map(tool_data)                   # 高德实时POI也可作行程项

    plans = []
    for i, p in enumerate(out.plans):
        days_out, used_tickets = [], 0.0
        for d in p.days[:req["days"]]:
            items = []
            if d.day == 1:
                items.append({"slot": "上午", "type": "交通",
                              "name": f"{origin['name']} → {c['name']}",
                              "note": transport["outbound"], "cost": transport["cost"]})
            for it in d.items:
                mapped = _map_item(it, att_map, food_map, poi_map)
                if mapped:
                    items.append(mapped)
                    if mapped.get("_ticket"):
                        used_tickets += mapped.pop("_ticket")
            if d.day == req["days"]:
                items.append({"slot": "下午", "type": "交通",
                              "name": f"{c['name']} → {origin['name']} 返程",
                              "note": transport["back"], "cost": transport["cost"]})
            title = (d.title or "").strip() or tools._day_title(
                d.day, req["days"], [a for a in c["attractions"]
                                     if any(i["name"] == a["n"] for i in items)])
            days_out.append({"day": d.day, "title": title[:14], "items": items})
        if not days_out:
            continue
        b = geo.trip_budget(c, req["days"], transport["cost"],
                            hotel_factor=getattr(p, "hotel_factor", 1.0) or 1.0,
                            food_factor=getattr(p, "food_factor", 1.0) or 1.0,
                            attraction_ticket_sum=used_tickets)
        over = b["total"] > req["budget"]
        plans.append({
            "id": f"agent{i}", "name": p.name[:12], "desc": (p.desc or "")[:30],
            "pace": (p.pace or "适中")[:6], "days": days_out,
            "guide": (getattr(p, "guide", "") or "")[:2500],
            "transport": transport, "budget": b, "fits_budget": not over,
            "over_hint": f"约超¥{b['total'] - req['budget']}，建议降低住宿或餐饮档位" if over else "",
        })

    if len(plans) < 3 and kb:                    # 知识库城市 → 规则兜底
        intel = tools.fetch_intel(None, req["dest"])
        judged = tools.judge_intel(None, intel, req["days"], req.get("prefs") or [])
        result = tools.compose_plans(None, origin, req["dest"], req["budget"],
                                     req["days"], req.get("transport", "auto"),
                                     req.get("prefs") or [], judged)
        return {"intel": intel, "judged": judged, "result": result,
                "guide": tools.build_guide(kb, intel, judged,
                                           tool_data=tool_data,
                                           transport=result.get("transport")),
                "judge_engine": "rule-fallback",
                "trace": [_t("judge", "方案校验", "LLM 方案不足3套，转规则编排")
                          ] + _compose_trace(result, req)}

    # 组装 intel（供前端情报条）：数据来自 ReAct 工具调用的真实返回
    intel = _intel_from_tools(c, tool_data)
    if not intel.get("scenic_qunar"):          # 模型没查去哪儿 → 代码补拉兜底
        qs = apis.qunar_scenic(c["name"], 8)
        if qs:
            intel["scenic_qunar"] = qs["scenic"]
            intel["sources"].append("去哪儿票价")
    dropped = [{"name": d.name, "reason": (d.reason or "")[:30]}
               for d in (getattr(out, "dropped", None) or [])]
    notes = [str(n)[:60] for n in (getattr(out, "notes", None) or [])][:5]
    notes += tools._base_notes(intel)
    judged = {"notes": notes, "dropped": dropped,
              "tips": c["tips"], "foods": c["foods"][:5], "kept": []}
    result = {"plans": plans[:5], "transport": transport, "km": km,
              "resolved_mode": mode}
    for pl in result["plans"]:
        if not pl.get("guide"):              # 模型没写攻略 → 确定性兜底组装
            pl["guide"] = tools.build_plan_guide(
                pl, dict(judged,
                         qunar_scenic=intel.get("scenic_qunar") or []))
    guide = tools.build_guide(c, intel, judged, tool_data=tool_data,
                              transport=transport)
    return {"intel": intel, "judged": judged, "result": result,
            "guide": guide, "judge_engine": "llm-agent",
            "trace": [_t("judge", "LLM 编排完成",
                         f"{len(plans)} 套方案通过校验，"
                         f"剔除 {len(dropped)} 项")
                      ] + [_t("rail", "查询去程/回程交通",
                              f"去程 {transport['outbound']}｜回程 {transport['back']}"
                              f"（{'12306实时' if transport['src']=='12306' else '估算'}）")]}


def _resolve_transport(origin, c, req, tool_data):
    """优先用模型查到的 12306 往返真实车次；模型没查则代码补查，最终兜底估算。"""
    km = geo.haversine_km(origin["lat"], origin["lng"], c["lat"], c["lng"])
    est = geo.estimate_transport(km, req.get("transport", "auto"), c)
    mode = est["mode"]
    if mode != "train":
        return ({"mode": mode, "src": "model", "hours": est.get("hours"),
                 "cost": est["cost"],
                 "outbound": f"{est['desc']} 约{est.get('hours')}h",
                 "back": f"{est['desc']} 约{est.get('hours')}h返程",
                 "trains": []}, km, mode)

    td = _last_td(tool_data, "query_trains")
    fo = td.get("outbound") if isinstance(td, dict) else None
    ro = td.get("return") if isinstance(td, dict) else None
    if not (fo and fo.get("trains")):                # 模型没查去程 → 代码补查
        d = apis.query_trains(origin["name"], c["name"])
        if d:
            fo = {"date": d["date"], "trains": [
                {"code": t["code"], "kind": t["kind"], "from": t["from"],
                 "to": t["to"], "dep": t["dep"], "arr": t["arr"],
                 "hours": t["hours"], "seats": t["seats"],
                 "二等座": (apis.train_price(t) or {}).get("二等座")}
                for t in d["trains"][:5]]}
    if not (ro and ro.get("trains")):                # 回程同理补查
        d = apis.query_trains(c["name"], origin["name"])
        if d:
            ro = {"date": d["date"], "trains": [
                {"code": t["code"], "kind": t["kind"], "from": t["from"],
                 "to": t["to"], "dep": t["dep"], "arr": t["arr"],
                 "hours": t["hours"], "seats": t["seats"],
                 "二等座": (apis.train_price(t) or {}).get("二等座")}
                for t in d["trains"][:3]]}
    if not (fo and fo.get("trains")):                # 两个方向都查不到 → 估算
        return ({"mode": mode, "src": "model", "hours": est.get("hours"),
                 "cost": est["cost"],
                 "outbound": f"{est['desc']} 约{est.get('hours')}h",
                 "back": f"{est['desc']} 约{est.get('hours')}h返程",
                 "trains": []}, km, mode)

    ob = min(fo["trains"], key=lambda t: t.get("dep") or "99:99")   # 最早去程
    bk = (max(ro["trains"], key=lambda t: t.get("dep") or "")
          if ro and ro.get("trains") else None)                     # 最晚回程
    cost = ob.get("二等座") or (bk or {}).get("二等座") or est["cost"]
    return ({"mode": "train", "src": "12306", "hours": ob["hours"],
             "cost": round(cost),
             "outbound": f"{ob['code']} {ob['from']}{ob['dep']}→{ob['to']}{ob['arr']}",
             "back": (f"{bk['code']} {bk['from']}{bk['dep']}→{bk['to']}{bk['arr']}"
                      if bk else "返程车次请到12306查询"),
             "trains": fo["trains"][:6], "query_date": fo.get("date", "")},
            km, mode)


def _pseudo_city(name):
    """非知识库目的地：地理编码定位 + 中性消费档，景点/美食完全交给 POI。"""
    g = apis.geocode(name) or {}
    return {"name": name, "province": g.get("city") or "",
            "lat": g.get("lat") or 35.0, "lng": g.get("lng") or 110.0,
            "hotelPerNight": 300, "foodPerDay": 120, "localPerDay": 40,
            "attractions": [], "foods": [], "tags": [], "bestMonths": [],
            "hsRail": True, "airport": True,
            "tips": ["该目的地不在本地知识库，行程基于高德POI与网络攻略实时编排"]}


def _poi_map(tool_data):
    """汇总 ReAct 中所有 POI 工具返回：{名称: {name,rating,cost,addr}}。"""
    m = {}
    for td in (tool_data.get("get_city_intel") or []):
        if isinstance(td, dict):
            for p in (td.get("pois_scenic") or []) + (td.get("pois_food") or []):
                if isinstance(p, dict) and p.get("name"):
                    m[p["name"]] = p
            for s in td.get("scenic_qunar") or []:
                if isinstance(s, dict) and s.get("name"):
                    m.setdefault(s["name"], {"name": s["name"],
                                             "cost": s.get("ticket"),
                                             "rating": s.get("score"),
                                             "addr": "", "_src": "去哪儿"})
    for td in (tool_data.get("search_pois") or []):
        if isinstance(td, dict):
            for p in td.get("pois") or []:
                if isinstance(p, dict) and p.get("name"):
                    m[p["name"]] = p
    return m


def _poi_cost(p, default):
    try:
        return max(0, round(float(p.get("cost") or default)))
    except (TypeError, ValueError):
        return default


def _map_item(it, att_map, food_map, poi_map=None):
    """把模型给的名字映射回真实对象：知识库 → 高德POI → 丢弃/通配。
    支持子串模糊匹配；查无实据的景点一律丢弃（防幻觉）。"""
    name, typ = (it.name or "").strip(), (it.type or "景点")
    poi_map = poi_map or {}
    if typ == "景点":
        a = att_map.get(name) or next(
            (v for k, v in att_map.items() if k in name or name in k), None)
        if a:
            return {"slot": it.slot, "type": "景点", "name": a["n"],
                    "note": a["desc"], "cost": a["ticket"],
                    "hours": a["hours"], "_ticket": a["ticket"]}
        p = poi_map.get(name) or next(
            (v for k, v in poi_map.items() if k in name or name in k), None)
        if p:
            cost = _poi_cost(p, 45)
            return {"slot": it.slot, "type": "景点", "name": p["name"][:16],
                    "note": f"{p.get('_src') or '高德POI'} · "
                            f"评分{p.get('rating') or '—'} · "
                            f"{(p.get('addr') or '')[:24]}",
                    "cost": cost, "hours": 3, "_ticket": cost}
        return None
    if typ == "美食":
        f = food_map.get(name) or next(
            (v for k, v in food_map.items() if k in name or name in k), None)
        if f:
            return {"slot": it.slot, "type": "美食", "name": f["n"],
                    "note": f["d"], "cost": f["p"]}
        p = poi_map.get(name) or next(
            (v for k, v in poi_map.items() if k in name or name in k), None)
        if p:
            return {"slot": it.slot, "type": "美食", "name": p["name"][:16],
                    "note": f"高德POI · 评分{p.get('rating') or '—'}",
                    "cost": _poi_cost(p, 60)}
        return {"slot": it.slot, "type": "美食", "name": name[:16],
                "note": "当地特色", "cost": 60}
    return {"slot": it.slot, "type": "休闲", "name": name[:16],
            "note": "", "cost": 40}


def _intel_from_tools(c, tool_data):
    """把 ReAct 工具返回还原成前端情报条需要的数据源标注。"""
    intel = {"city": c, "sources": ["本地知识库"], "weather": None,
             "web": None, "pois_scenic": None}
    td = _last_td(tool_data, "get_city_intel")
    if isinstance(td, dict):
        if td.get("weather"):
            intel["weather"] = td["weather"]; intel["sources"].append("高德天气")
        if td.get("pois_scenic"):
            intel["pois_scenic"] = td["pois_scenic"]
            intel["sources"].append("高德POI")
        if td.get("pois_food"):
            intel["pois_food"] = td["pois_food"]
        if td.get("scenic_qunar"):
            intel["scenic_qunar"] = td["scenic_qunar"]
            intel["sources"].append("去哪儿票价")
        if td.get("web"):
            intel["web"] = list(td["web"]); intel["sources"].append("网页搜索")
    xhs_td = _last_td(tool_data, "search_xhs_notes")
    if isinstance(xhs_td, dict) and xhs_td.get("notes"):
        intel["sources"].append("小红书")
        for n in xhs_td["notes"][:3]:           # 并进攻略参考列表，前端自动可点
            (intel["web"] or intel.setdefault("web", [])).append({
                "title": "小红书 · " + n["title"], "url": n.get("url") or "",
                "snippet": f"赞{n.get('likes','0')} · @{n.get('author','')}"
                           + (f"｜{n['excerpt'][:60]}…" if n.get("excerpt") else "")})
    if _last_td(tool_data, "query_trains"):
        intel["sources"].append("12306实时车次")
    return intel


def _compose_trace(result, req):
    ti = result["transport"]
    return [_t("rail", "查询去程/回程交通",
               f"去程 {ti['outbound']}｜回程 {ti['back']}"
               f"（{'12306实时' if ti['src']=='12306' else '估算'}）"),
            _t("plan", f"生成 {len(result['plans'])} 套出游方案",
               "、".join(p["name"] for p in result["plans"]))]


# 规则流水线节点（无 key 时使用）---------------------------------------------

def n_fetch_intel(s: S):
    intel = tools.fetch_intel(None, s["req"]["dest"])
    return {"intel": intel,
            "trace": [_t("search", f"搜索「{s['req']['dest']}」当地特色与攻略",
                         "数据来源：" + " + ".join(intel["sources"]))]}


def n_judge_rule(s: S):
    req = s["req"]
    judged = tools.judge_intel(None, s["intel"], req["days"], req.get("prefs") or [])
    kept_n, drop_n = len(judged["kept"]), len(judged["dropped"])
    why = "；".join(d["reason"] for d in judged["dropped"][:2]) or "无"
    return {"judged": judged, "judge_engine": "rule",
            "trace": [_t("judge", "情报判断与取舍（规则引擎）",
                         f"保留 {kept_n} 个核心体验，剔除 {drop_n} 项（{why}）")]}


def n_compose(s: S):
    req = s["req"]
    result = tools.compose_plans(None, s["origin"], req["dest"], req["budget"],
                                 req["days"], req.get("transport", "auto"),
                                 req.get("prefs") or [], s["judged"])
    c = next((x for x in tools.load_cities()
              if x["name"] == req["dest"].rstrip("市")), None) \
        or _pseudo_city(req["dest"])
    guide = tools.build_guide(c, s["intel"], s["judged"],
                              transport=result.get("transport"))
    return {"result": result, "guide": guide,
            "trace": _compose_trace(result, req)}


def _route_plan(s: S):
    return "agent_plan" if llm.llm_available() else "fetch_intel"


def build_plan():
    g = StateGraph(S)
    g.add_node("resolve", n_resolve)
    g.add_node("agent_plan", n_agent_plan)
    g.add_node("assemble", n_assemble)
    g.add_node("fetch_intel", n_fetch_intel)
    g.add_node("judge_rule", n_judge_rule)
    g.add_node("compose", n_compose)
    g.add_edge(START, "resolve")
    g.add_conditional_edges("resolve", _route_plan,
                            {"agent_plan": "agent_plan",
                             "fetch_intel": "fetch_intel"})
    g.add_edge("agent_plan", "assemble")
    g.add_edge("assemble", END)
    g.add_edge("fetch_intel", "judge_rule")
    g.add_edge("judge_rule", "compose")
    g.add_edge("compose", END)
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
