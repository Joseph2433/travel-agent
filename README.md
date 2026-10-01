# 旅图 · TravelAgent

一个 Agent 驱动的智能旅行规划应用：根据**当前定位、预算、出行天数、出行方式**
先圈出可达范围（全国 372 个地级市，可再按省份筛选），再由 Agent 推荐目的地、
搜索当地情报、判断取舍，最终生成 **3-5 套完整出游方案**（交通往返 +
每日行程 + 吃喝玩乐 + 预算拆解）。小众城市同样可推荐/可规划。

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
   │                     / get_city_intel / search_pois(任意类别POI实时搜索)
   │                     / travel_trends(各平台实时热搜榜，免登录)
   │                     / search_xhs_notes(小红书攻略笔记，可选源)
   │                     / query_trains(往返) / estimate_transport
   │                     / calc_budget / submit_result(终止+结构化提交)
   ├─► schemas.py     ── pydantic 输出契约（模型只给名字/排序/理由）
   ├─► tools.py       ── 规则引擎实现（无key兜底）+ LLM结论合并校验
   │                     + 热榜命中加分（微博/抖音/小红书）+ 真实票价映射
   ├─► apis.py        ── 高德 Web服务(IP/地理编码/行政区/POI/天气/驾车)  需 AMAP_KEY
   │                  ── 12306 queryG/queryTicketPrice  真实往返车次+余票+票价
   │                  ── 去哪儿门票频道  在售景点真实票价/5A级标/销量热度（免key）
   │                  ── uapis.cn 聚合热榜  微博/知乎/抖音/小红书/头条（免key免注册）
   │                  ── 博查AI搜索(需BOCHA_API_KEY)→Bing/DuckDuckGo  攻略网页摘要
   │                  ── 小红书笔记搜索  需本地 xiaohongshu-mcp（可选，XHS_API_BASE）
   ├─► geo.py         ── 距离/交通/预算估算模型（无网兜底）
   ├─► data/cities.json             # 25 城种子知识库：画像/消费档兜底，
   │                                # 景点美食可由高德POI实时数据取代
   └─► data/prefecture_cities.json  # 全国 372 地级市坐标种子（高德行政区生成，
                                     # 推荐候选池；文件缺失且有key时自动重建）
```

**真 Agent 模式**（配 `LLM_API_KEY` 后）：模型自主决定调哪些工具、调几次——
目的地推荐时它在**全国 372 城候选池**（可按用户圈定省份过滤）里扫描，
对感兴趣的小众城市还能调 `get_city_intel` 查实况再推荐；
行程规划时它先搜小红书真实攻略笔记搭骨架，再查攻略摘要/天气/POI/12306往返车次、
去哪儿真实票价交叉验证填充（`search_pois` 可实时搜任意类别景点/美食，带评分/参考价/
地址），编排 3-5 套差异化方案（每天槽位、餐厅景点、档位系数都由它决定），
并在方案页底部输出一份多源组装的**详细攻略**（笔记灵感/景点门票/风味/交通/避雷/参考来源），
推理与工具调用全程进 trace 回放（独立页面可展开）。
代码的职责只剩：**校验**（景点名必须是知识库条目或高德真实POI，防幻觉）
和**算数**（票价/预算永不由模型生成）。模型失联/输出不合法时原地降级规则引擎。

目的地不限于知识库 25 城：前端「指定目的地」可填任意城市（如景德镇/大理），
非知识库城市走「地理编码定位 + 高德POI实时编排」，预算用估算系数兜底。

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
| `XHS_API_BASE` | 小红书笔记源地址，二选一：本地 [xiaohongshu-mcp](https://github.com/xpzouying/xiaohongshu-mcp) `http://localhost:18060`（REST 层）；或 x-mcp 插件云端 `https://mcp.aredink.com/mcp`（以 `/mcp` 结尾自动走 MCP Streamable HTTP 协议）。启用 `search_xhs_notes` 工具让模型查真实攻略笔记 | 跳过小红书源 |
| `XHS_API_TOKEN` | 本地服务设了 `AUTH_TOKEN` 时填；x-mcp 插件版填 aredink 账号的 API Token（同时以 `X-API-Key` 与 `Authorization: Bearer` 发送） | — |
| `BOCHA_API_KEY` | [博查AI搜索](https://open.bochaai.com/) key：攻略摘要主源（中文质量好、索引含小红书），有免费额度 | 回退 Bing/DDG 抓取 |

小红书没有面向普通开发者的官方笔记搜索 API，目前可用两条社区方案（均为第三方
逆向/聚合，仅适合个人学习用途，账号有风控风险，建议用小号）：

- **本地 xiaohongshu-mcp**：下载 release 二进制 → `xiaohongshu-login` 扫码登录
  自己的小红书账号 → 启动 `xiaohongshu-mcp`（无头浏览器，默认 :18060），
  `XHS_API_BASE=http://localhost:18060`。注意其内置无头浏览器的 `leakless.exe`
  易被 Windows Defender/火绒误报，需把安装目录与 `%TEMP%\leakless-*` 加信任区。
- **x-mcp 浏览器插件版**（推荐，零部署、不碰杀软）：Chrome 商店装
  「小红书MCP助手」→ [aredink.com](https://mcp.aredink.com) 注册 → 创建连接拿
  API Token 填入插件 → `XHS_API_BASE=https://mcp.aredink.com/mcp` +
  `XHS_API_TOKEN=<token>`。它复用你浏览器里已登录的小红书会话，浏览器开着才在线。

不配置也完全可用：`get_city_intel` 已自动附带 `site:xiaohongshu.com` 的网页
搜索结果，`travel_trends` 工具还会直接拉小红书热搜榜（免登录，uapis 聚合）。

也可以直接 export/set 环境变量（优先级高于 .env）。
注意：配置在**进程启动时**读取，改完要重启 `python run.py`。

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | /api/status | 数据源/LLM 可用性 |
| GET | /api/cities | 城市列表 |
| GET | /api/geo/provinces | 34 省级列表（出发地第一级） |
| GET | /api/geo/cities?province=xx | 该省地级市（高德行政区接口） |
| POST | /api/locate | GPS 坐标 → 最近出发城市 |
| POST | /api/agent/scope | 可达范围预览：各省份可行城市数/最快耗时 |
| POST | /api/agent/destinations | 阶段一：推荐目的地（含 trace + AI复核，可圈省份） |
| POST | /api/agent/plans | 阶段二：搜索+判断+生成 3-5 套方案（可指定任意目的地） |
| POST | /api/agent/destinations/stream | 同上，SSE 流式：逐步推 trace 事件 |
| POST | /api/agent/plans/stream | 同上，SSE 流式 |

## 前端

原生 HTML/CSS/JS（`frontend/`）：整页路由（条件页 → 目的地页 → 方案页 → 详情页，
支持浏览器前进后退）、极光渐变 Hero、玻璃拟态表单、**SSE 实时 Agent 思考时间线**
（结果页可展开回放）、Leaflet 地图、目的地评分卡 + 「查看更多」候选列表、
方案详情页（真实车次表 + 逐日行程时间轴 + **每套方案的详细攻略正文** + 预算条形图）
+ 页底数据来源与参考清单。

## 部署（前端 GitHub Pages + 后端独立主机）

GitHub Pages 只能跑静态文件，FastAPI 后端（SSE 流式 + 长耗时 Agent）需要单独的主机。

**① 后端 → Render（免费档，推荐）**
1. [render.com](https://render.com) → New → **Blueprint** → 选本仓库（`render.yaml` 已写好）
2. 按提示填环境变量：`AMAP_KEY` / `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL`
   （可选 `BOCHA_API_KEY`；小红书云端模式填 `XHS_API_BASE=https://mcp.aredink.com/mcp`
   + `XHS_API_TOKEN`）；`CORS_ORIGINS` 填 `https://<你的用户名>.github.io`
3. 部署完记下后端地址，如 `https://travel-agent-xxxx.onrender.com`
   - 也可选 Railway / Fly.io：根目录已附 `Procfile`，环境变量同上
   - 免费档休眠冷启动约 30-60s；Agent 单次请求 1-3 分钟属正常

**② 前端 → GitHub Pages**
1. 仓库 Settings → Pages → Source 选 **GitHub Actions**（一次性）
2. push 到 `main` 即触发 `.github/workflows/pages.yml`，自动发布 `frontend/`
3. 站点地址：`https://<用户名>.github.io/<仓库名>/`

**③ 把前端指向后端**
- `frontend/config.js` 已内置线上后端地址（`window.API_BASE`），只对 Pages
  生效——本地 `python run.py` 访问 localhost 时永远走同源后端，两套配置互不干扰；
- 也可以不改文件：访问 `https://<页地址>/?api=https://后端地址` 一次即写入浏览器
  localStorage，`?api=local` 可清除覆盖还原默认。

**两套配置并存**

| | 本地开发 | GitHub Pages 站点 |
|---|---|---|
| 入口 | `python run.py` → `127.0.0.1:8000` | `https://<用户名>.github.io/travel-agent/` |
| 前端请求后端 | 同源（localhost 忽略 config.js） | `config.js` 里的 Render 地址 |
| 密钥配置 | `.env`（不进仓库） | Render 控制台环境变量 |
| 小红书源 | `XHS_API_BASE=http://localhost:18060`（本机 mcp） | `https://mcp.aredink.com/mcp` 云端插件，或隧道回本机，或留空自动降级 |
