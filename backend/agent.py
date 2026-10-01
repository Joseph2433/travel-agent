"""旅行规划 Agent 门面：对外保持原接口，内部交给 LangGraph 决策图执行。

状态图与节点定义见 agent_graph.py；trace 由图中各节点累计产出，
供前端展示"思考过程"；有无 LLM key 决定走模型决策节点还是规则节点。
"""
import agent_graph


class TravelAgent:
    """每个请求 new 一个实例，语义上代表一次独立的规划任务。"""

    def run_recommend(self, req, client_ip=None):
        """阶段一：目的地推荐。req: {lat,lng,city,budget,days,transport,prefs}"""
        s = agent_graph.recommend_graph().invoke(
            {"req": req, "client_ip": client_ip, "trace": []})
        out = {"trace": s.get("trace", []), "origin": s["origin"],
               "destinations": s.get("ranked", []),
               "verdict": s.get("verdict")}
        if s.get("message"):
            out["message"] = s["message"]
        return out

    def run_plan(self, req, client_ip=None):
        """阶段二：情报搜索 + 判断 + 方案生成。req 同上 + dest"""
        s = agent_graph.plan_graph().invoke(
            {"req": req, "client_ip": client_ip, "trace": []})
        r, intel, judged = s["result"], s["intel"], s["judged"]
        return {
            "trace": s.get("trace", []), "origin": s["origin"], "dest": req["dest"],
            "km": r["km"], "resolved_mode": r["resolved_mode"],
            "judge_engine": s.get("judge_engine", "rule"),
            "intel": {
                "sources": intel["sources"], "weather": intel.get("weather"),
                "web": intel.get("web"), "notes": judged["notes"],
                "dropped": judged["dropped"], "tips": judged["tips"],
            },
            "plans": r["plans"],
        }
