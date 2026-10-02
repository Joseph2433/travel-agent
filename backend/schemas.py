"""LLM 结构化输出契约：create_react_agent 的 response_format（ToolStrategy）。

模型只负责"决定"——名字/顺序/节奏/理由；所有数字由代码回填，防幻觉。
"""
from pydantic import BaseModel, Field


# 阶段一：目的地推荐
class RecItem(BaseModel):
    city: str = Field(description="城市名，必须来自工具返回")
    score: int = Field(ge=0, le=100, description="你的综合评分 0-100")
    comment: str = Field(default="", description="一句点评，≤25字")


class RecOut(BaseModel):
    ranking: list[RecItem] = Field(description="3-6 个推荐，按你心中的顺序")
    pick: str = Field(description="你的首推城市，必须出现在 ranking 中")
    why: str = Field(default="", description="首推理由，≤40字")


# 阶段二：行程方案
class PItem(BaseModel):
    slot: str = Field(description="时段：上午/下午/晚上/全天/午餐/晚餐/午后…")
    type: str = Field(description="景点 | 美食 | 休闲")
    name: str = Field(description="景点/美食必须精确等于知识库名称")


class PDay(BaseModel):
    day: int
    title: str = Field(default="", description="当日标题，≤10字，如「五大道漫步」")
    items: list[PItem]


class PPlan(BaseModel):
    name: str = Field(description="方案名，≤8字，如「经典全景线」")
    pace: str = Field(default="适中", description="节奏：紧凑/适中/轻松/悠闲")
    desc: str = Field(default="", description="方案定位一句话，≤25字")
    hotel_factor: float = Field(default=1.0, description="住宿档位 0.8~1.5")
    food_factor: float = Field(default=1.0, description="餐饮档位 0.8~1.5")
    days: list[PDay]
    guide: str = Field(default="", description=(
        "该方案的详细攻略正文（markdown 精简语法：###小节、-列表、**重点**），"
        "300-600字：行程怎么玩、门票怎么约、美食去哪吃、避雷提醒。"
        "内容必须基于你刚才工具调研到的真实信息（笔记要点/真实票价/天气），"
        "不要编造没核实过的店名和数字"))


class DropItem(BaseModel):
    name: str
    reason: str = Field(default="", description="剔除理由，≤20字")


class PlansOut(BaseModel):
    plans: list[PPlan] = Field(description="3-5 套差异化方案")
    notes: list[str] = Field(default=[], description="出行提示 3-5 条")
    dropped: list[DropItem] = Field(default=[], description="你剔除的景点")


# 嵌套深读：小红书攻略挖掘子代理的结构化产出
class GNote(BaseModel):
    title: str = Field(description="笔记标题")
    url: str = Field(default="", description="笔记链接")
    likes: str = Field(default="0", description="点赞数")
    author: str = Field(default="", description="作者昵称")
    points: list[str] = Field(default=[], description="这篇笔记里你采信的要点 1-3 条")


class GuideDigest(BaseModel):
    summary: str = Field(default="", description=(
        "≤120字综合经验谈：这座城实际怎么玩最顺、什么节奏、什么气质"))
    routes: list[str] = Field(default=[], description=(
        "笔记里反复出现的真实路线安排（按半天/天粒度），每条≤40字，≤4条"))
    must_go: list[str] = Field(default=[], description=(
        "高频被点名的必去地，可带具体位置/时段，≤6条"))
    eats: list[str] = Field(default=[], description=(
        "具体到店名/品类的美食，带一句为什么，≤6条"))
    pitfalls: list[str] = Field(default=[], description="避雷/差评/排队坑，≤5条")
    booking: list[str] = Field(default=[], description="预约/购票/排队/交通技巧，≤4条")
    notes: list[GNote] = Field(default=[], description="采信笔记清单，≤5篇")
