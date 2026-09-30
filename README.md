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
TravelAgent  backend/agent.py      # 计划→工具→观察→判断 循环，全程 trace
   │ tools.py                       # 6 个工具：定位/扫描/排序/搜索/判断/编排
   ├─► apis.py  ── 高德 Web服务(IP定位/地理编码/POI/天气/驾车路径)  需 AMAP_KEY
   │            ── 12306 queryG/queryTicketPrice  真实车次+余票+票价  无需Key
   │            ── Bing/DuckDuckGo  攻略网页摘要
   ├─► geo.py   ── 距离/交通/预算估算模型（无网兜底）
   └─► data/cities.json             # 24+ 目的地知识库（景点/美食/贴士/消费档）
```

所有外部数据源均**带超时和降级**：无 Key、无外网时应用依然完整可用（标注"估算"）。

## 运行

```bash
pip install fastapi uvicorn requests
python run.py        # → http://127.0.0.1:8000
```

可选增强（申请高德 Web 服务 Key，免费）：

```bash
set AMAP_KEY=你的key        # Windows
export AMAP_KEY=你的key     # bash
```

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | /api/status | 数据源可用性 |
| GET | /api/cities | 城市列表 |
| POST | /api/locate | GPS 坐标 → 最近出发城市 |
| POST | /api/agent/destinations | 阶段一：推荐目的地（含 trace） |
| POST | /api/agent/plans | 阶段二：搜索+判断+生成 3-5 套方案 |

## 前端

原生 HTML/CSS/JS 单页（`frontend/`）：极光渐变 Hero、玻璃拟态表单、
Agent 思考时间线、Leaflet 地图、目的地评分卡、方案详情抽屉（真实车次表 +
逐日行程时间轴 + 预算条形图）。
