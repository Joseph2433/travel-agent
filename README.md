# 旅图 · TravelAgent

一个 Agent 驱动的智能旅行规划应用：根据**当前定位、预算、出行天数、出行方式**推荐目的地，再由 Agent 搜索目的地情报、判断取舍，最终生成 **3-5 套完整出游方案**（交通往返 + 每日行程 + 吃喝玩乐 + 预算拆解）。

## 架构

```
浏览器(单页应用)
   │  fetch JSON
   ▼
FastAPI 后端  backend/main.py
   │
   ▼
TravelAgent  backend/agent.py        # 门面：一次请求 = 一次 LangGraph 图执行
   │
   ▼  agent_graph.py                  # StateGraph 编排 + trace 采集
   │    有key: resolve → agent_*(ReAct循环: think→tool→observe→…) → assemble → END
   │    无key: resolve → scan/fetch_intel → rank/judge_rule → compose → END
   │
   ├─► llm.py         ── OpenAI兼容协议(Kimi/DeepSeek/OpenAI)  需 LLM_API_KEY
   ├─► agent_tools.py ── 暴露给模型的 @tool：scan_destinations / get_city_profile
   │                     / get_city_intel / query_trains(往返) / estimate_transport
   │                     / calc_budget / submit_result(终止+结构化提交)
   ├─► schemas.py     ── pydantic 输出契约（模型只给名字/排序/理由）
   ├─► tools.py       ── 规则引擎实现（无key兜底）+ LLM结论合并校验
   ├─► apis.py        ── 高德 Web服务(IP/地理编码/POI/天气/驾车)  需 AMAP_KEY
   │                  ── 12306 queryG/queryTicketPrice  真实往返车次+余票+票价
   │                  ── Bing/DuckDuckGo  攻略网页摘要
   ├─► geo.py         ── 距离/交通/预算估算模型（无网兜底）
   └─► data/cities.json             # 25 目的地知识库（景点/美食/贴士/消费档）
```

**真 Agent 模式**（配 `LLM_API_KEY` 后）：模型自主决定调哪些工具、调几次——
目的地推荐时它自己扫描候选、挑感兴趣的城市深入看画像、打分排序并给首推；
行程规划时它自己查攻略摘要/天气/POI/12306往返车次，编排 3-5 套差异化方案
（每天槽位、餐厅景点、档位系数都由它决定），推理与工具调用全程进 trace 回放。
代码的职责只剩：**校验**（景点名必须存在于知识库，防幻觉）和**算数**
（票价/预算永不由模型生成）。模型失联/输出不合法时原地降级规则引擎。

## 运行

```bash
pip install -r requirements.txt
python run.py        # → http://127.0.0.1:8000
```

可选配置（不配也能跑，自动降级）。推荐写到 `.env`：

```bash
cp .env.example .env   # 然后填入你的 key；.env 已在 .gitignore 中
```

| 变量 | 作用 | 缺省行为 |
|---|---|---|
| `AMAP_KEY` | 高德 Web 服务 key（IP定位/POI/天气/驾车） | 估算模型 |
| `LLM_API_KEY` | 大模型 key（OpenAI 兼容协议，需支持 function calling） | 规则引擎决策 |
| `LLM_BASE_URL` | 默认 `https://api.moonshot.cn/v1` | — |
| `LLM_MODEL` | 默认 `kimi-k2-0905-preview` | — |

也可以直接 export/set 环境变量（优先级高于 .env）。
注意：配置在**进程启动时**读取，改完要重启 `python run.py`。

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | /api/status | 数据源/LLM 可用性 |
| GET | /api/cities | 城市列表 |
| POST | /api/locate | GPS 坐标 → 最近出发城市 |
| POST | /api/agent/destinations | 阶段一：推荐目的地（含 trace + AI复核） |
| POST | /api/agent/plans | 阶段二：搜索+判断+生成 3-5 套方案 |

## 前端

原生 HTML/CSS/JS 单页（`frontend/`）：极光渐变 Hero、玻璃拟态表单、
Agent 思考时间线、Leaflet 地图、目的地评分卡（AI首推徽标+点评）、
方案详情抽屉（真实车次表 + 逐日行程时间轴 + 预算条形图）。
