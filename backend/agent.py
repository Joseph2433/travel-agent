"""旅行规划 Agent 门面：对外保持接口，内部交给 LangGraph 决策图执行。

两条通路：
  run_*       一次性 invoke，拿最终响应（兼容旧接口）
  stream_*    SSE 生成器：图执行中逐步 yield 事件 {"type":"step|done|error"}
              ——ReAct 循环里每次思考/工具调用/观察都会实时推给前端
"""
import json

import agent_graph


def _shape_recommend(s):
    out = {"trace": s.get("trace", []), "origin": s["origin"],
           "destinations": s.get("ranked", []),
           "more": s.get("more") or [],
           "total_feasible": s.get("total_feasible"),
           "date": s["req"].get("date"),
           "local": bool(s["req"].get("local")),
           "verdict": s.get("verdict")}
    if s.get("message"):
        out["message"] = s["message"]
    return out


def _shape_plan(s, req):
    r, intel, judged = s["result"], s["intel"], s["judged"]
    return {
        "trace": s.get("trace", []), "origin": s["origin"], "dest": req["dest"],
        "date": req.get("date"), "local": bool(req.get("local")),
        "km": r["km"], "resolved_mode": r["resolved_mode"],
        "judge_engine": s.get("judge_engine", "rule"),
        "intel": {
            "sources": intel["sources"], "weather": intel.get("weather"),
            "web": intel.get("web"), "notes": judged["notes"],
            "dropped": judged["dropped"], "tips": judged["tips"],
        },
        "plans": r["plans"],
        "guide": s.get("guide"),
    }


class TravelAgent:
    """每个请求 new 一个实例，语义上代表一次独立的规划任务。"""

    def run_recommend(self, req, client_ip=None):
        s = agent_graph.recommend_graph().invoke(
            {"req": req, "client_ip": client_ip, "trace": []})
        return _shape_recommend(s)

    def run_plan(self, req, client_ip=None):
        s = agent_graph.plan_graph().invoke(
            {"req": req, "client_ip": client_ip, "trace": []})
        return _shape_plan(s, req)


# ------------------------------------------------------------ SSE 流式 ----

def _sse(obj):
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


def _stream_graph(graph, init, shape):
    """通用流式执行：custom 事件=节点内实时步骤；updates=节点完成输出。
    用 step.id 去重（节点内已推过的步骤不再从 trace 返回值重复推）。"""
    seen = set()
    merged = dict(init)
    try:
        for mode, chunk in graph.stream(init, stream_mode=["updates", "custom"]):
            if mode == "custom":
                if isinstance(chunk, dict) and chunk.get("type") == "step":
                    seen.add(chunk.get("id"))
                    yield _sse(chunk)
                continue
            for _node, upd in chunk.items():
                if not isinstance(upd, dict):
                    continue
                for st in upd.get("trace") or []:
                    if st.get("id") in seen:
                        continue
                    seen.add(st.get("id"))
                    yield _sse({"type": "step", **st})
                for k, v in upd.items():
                    if k == "trace":
                        merged["trace"] = merged.get("trace", []) + list(v)
                    else:
                        merged[k] = v
        yield _sse({"type": "done", "data": shape(merged)})
    except Exception as e:
        yield _sse({"type": "error", "message": str(e)[:200]})


def stream_recommend(req, client_ip=None):
    init = {"req": req, "client_ip": client_ip, "trace": []}
    yield from _stream_graph(agent_graph.recommend_graph(), init,
                             lambda s: _shape_recommend(s))


def stream_plan(req, client_ip=None):
    init = {"req": req, "client_ip": client_ip, "trace": []}
    yield from _stream_graph(agent_graph.plan_graph(), init,
                             lambda s: _shape_plan(s, req))
