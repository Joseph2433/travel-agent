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
TravelAgent  backend/agent.py        # 门面：一次请求 = 一次图执行
   │
   ▼  agent_graph.py                  # LangGraph StateGraph 决策图，全程 trace
   │    推荐图: resolve → scan → rank ─(有key)→ verdict(LLM复核) → END
   │    方案图: resolve → fetch_intel ─(有key)→ judge_llm → compose
   │                              └─(无key)→ judge_rule ┘       │
   │                                          └─(有key)→ polish(LLM文案) → END
   │ tools.py                       # 定位/扫描/排序/搜索/判断/编排 + LLM结论合并
   ├─► llm.py   ── OpenAI兼容协议(Kimi/DeepSeek/OpenAI)  需 LLM_API_KEY
   ├─► apis.py  ── 高德 Web服务(IP定位/地理编码/POI/天气/驾车路径)  需 AMAP_KEY
   │            ── 12306 queryG/queryTicketPrice  真实车次+余票+票价  无需Key
   │            ── Bing/DuckDuckGo  攻略网页摘要
   ├─► geo.py   ── 距离/交通/预算估算模型（无网兜底）
   └─► data/cities.json             # 25 目的地知识库（景点/美食/贴士/消费档）
```

**LLM 决策点**（有 `LLM_API_KEY` 时启用，失败原地降级规则并在 trace 中标注）：

1. `verdict` 复核排序 —— 模型对规则打分结果给出首推与逐城点评
2. `judge_llm` 情报取舍 —— 读攻略摘要/天气/POI，决定景点去留与提示
3. `polish` 方案文案 —— 为每套方案写推荐语（`ai_note`）

所有外部依赖均**带超时和降级**：无 Key、无外网时应用依然完整可用（标注"估算"/"规则"）。

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
| `LLM_API_KEY` | 大模型 key（OpenAI 兼容协议） | 规则引擎决策 |
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
