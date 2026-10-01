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


class DropItem(BaseModel):
    name: str
    reason: str = Field(default="", description="剔除理由，≤20字")


class PlansOut(BaseModel):
    plans: list[PPlan] = Field(description="3-5 套差异化方案")
    notes: list[str] = Field(default=[], description="出行提示 3-5 条")
    dropped: list[DropItem] = Field(default=[], description="你剔除的景点")
