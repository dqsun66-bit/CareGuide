"""Read-only AgentArts demo gateway for the Taizhou main campus."""

import json
import os
import re
import secrets
from urllib.parse import urlencode

import httpx
from fastapi import Depends, FastAPI, HTTPException
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, ConfigDict, Field

from .amap_models import RouteRequest
from .agentarts_judge import JudgeOperation, judge_operation, open_session


app = FastAPI(title="CareGuide 挂号资料查询", docs_url=None, redoc_url=None, openapi_url=None)
key_header = APIKeyHeader(name="X-CareGuide-Key", auto_error=False)


def authenticate(key: str | None = Depends(key_header)):
    expected = os.environ.get("CAREGUIDE_AGENTARTS_KEY", "")
    if not expected or not key or not secrets.compare_digest(expected.encode(), key.encode()):
        raise HTTPException(401, "插件密钥无效")


class RegistrationQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    question: str = Field(min_length=1, max_length=200)


_CAMPUS = re.compile(r"总院区?|本部|(?:海陵|迎春|东|西|南|北)院(?:区)?|院区|分院")
_NEGATED_DESTINATION = re.compile(
    r"(?:不(?:想|打算|准备|是要)?去|不在|不是|不考虑|排除|别去|并非)"
    r"\s*(?:(?:南京医科大学附属泰州人民医院|泰州市人民医院)的?)?\s*$"
)


def registration_query(question: str) -> str:
    # Keep excluded destinations out of the ranking query after scope validation.
    for campus in reversed(list(_CAMPUS.finditer(question))):
        prefix = _NEGATED_DESTINATION.search(question[:campus.start()])
        if prefix:
            question = question[:prefix.start()] + question[campus.end():]
    return "泰州市人民医院总院区 挂号 " + question


def campus_notice(question: str) -> str:
    positive_main = False
    excluded_main = False
    positive_other = False
    for match in _CAMPUS.finditer(question):
        negated = bool(_NEGATED_DESTINATION.search(question[:match.start()]))
        if match.group() in {"总院区", "总院", "本部"}:
            positive_main |= not negated
            excluded_main |= negated
        elif not negated:
            positive_other = True
    if positive_other or excluded_main:
        return "当前工具仅覆盖泰州市人民医院总院区。如需查询其他院区，请通过该院区官方渠道确认。"
    if not positive_main:
        return "请先确认您要去的是泰州市人民医院总院区吗？"
    return ""


@app.post("/registration", dependencies=[Depends(authenticate)])
async def registration(request: RegistrationQuestion):
    notice = campus_notice(request.question)
    if notice:
        return {"hospital": "", "evidence": [], "message": notice}
    payload = {
        "agent": "appointment",
        "tool": "lookup_hospital_guide",
        "arguments": {
            "hospital_id": "js_taizhou_001",
            "campus": "js_taizhou_001_main",
            "section": "all",
            "query": registration_query(request.question),
            "top_k": 3,
        },
    }
    try:
        async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
            result = await client.post("http://127.0.0.1:8010/tools/query", json=payload)
            result.raise_for_status()
    except httpx.HTTPError as exc:
        raise HTTPException(502, "挂号资料服务暂时不可用") from exc
    evidence = [
        {"title": item["title"], "content": item["content"], "url": item["source"]["url"]}
        for item in result.json()["evidence"]
        if item["source"]["tier"] == "hospital_official"
    ]
    for item in evidence:
        if item["url"] == "https://www.jstzhospital.com/gwrk/xwzx/yyxw/content_1772":
            item["content"] += (
                "\n微信小程序入口（项目实测的分享口令）："
                "#小程序://泰州人民医院互联网医院/1wGRDDgpEQNsJGg"
                "\n复制上一行从 # 开始的完整口令，发送到微信文件传输助手，"
                "点开发出的消息进入医院小程序首页，再选择“当日挂号”或“预约挂号”。"
            )
    return {"hospital": "南京医科大学附属泰州人民医院总院区", "evidence": evidence}


class CampusSearch(BaseModel):
    query: str = Field(min_length=1, max_length=100)
    city: str = Field(default="泰州", min_length=1, max_length=50)


class ParkingSearch(BaseModel):
    hospital_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,100}$")
    radius_m: int = Field(default=1000, ge=1, le=50000)


class DemoReplan(BaseModel):
    parking_lot_id: str = Field(min_length=1, max_length=100)
    available_spaces: int = Field(ge=0)
    max_walking_distance_m: int = Field(default=1000, ge=0, le=5000)
    requires_wheelchair_access: bool = False


class KnowledgeSearch(BaseModel):
    query: str = Field(min_length=1, max_length=500)


class ToolResult(BaseModel):
    result: str = Field(description="本次查询的结构化 JSON 文本，保留来源、模拟标记及未知项")


async def backend(method, path, **kwargs):
    try:
        async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
            response = await client.request(method, "http://127.0.0.1:8010" + path, **kwargs)
            response.raise_for_status()
            return response.json()
    except httpx.HTTPStatusError as exc:
        raise HTTPException(exc.response.status_code, "本地服务未能处理此查询") from exc
    except httpx.RequestError as exc:
        raise HTTPException(502, "本地服务暂时不可用") from exc


def tool_result(data):
    return ToolResult(result=json.dumps(data, ensure_ascii=False, separators=(",", ":")))


@app.post("/campuses", dependencies=[Depends(authenticate)], operation_id="findCampuses")
async def campuses(request: CampusSearch) -> ToolResult:
    """查询真实院区，按名称和地址向用户确认目标院区。"""
    return tool_result(await backend("GET", "/map/hospitals/search",
                                     params={"q": request.query, "city": request.city}))


@app.post("/parking", dependencies=[Depends(authenticate)], operation_id="findParking")
async def parking(request: ParkingSearch) -> ToolResult:
    """使用上一步已确认的真实院区 ID 查询周边停车场及驾车入口。未知余位或无障碍信息应保留。"""
    data = await backend("GET", f"/map/hospitals/{request.hospital_id}/parking",
                         params={"radius_m": request.radius_m})
    for item in data["candidates"]:
        point = item.get("entrance_location") or item["location"]
        destination = f"{point['longitude']},{point['latitude']},{item['name']}"
        item["driving_url"] = "https://uri.amap.com/navigation?" + urlencode(
            {"to": destination, "mode": "car", "src": "CareGuide"})
    return tool_result(data)


@app.post("/route", dependencies=[Depends(authenticate)], operation_id="planMapRoute")
async def route(request: RouteRequest) -> ToolResult:
    """使用上一步返回的坐标规划路线。停车后步行时，起点为停车场、终点为院区。"""
    data = await backend("POST", "/map/routes", json=request.model_dump())
    data["route"].pop("polyline", None)
    for step in data["route"].get("steps", []):
        step.pop("polyline", None)
    return tool_result(data)


@app.post("/demo-replan", dependencies=[Depends(authenticate)], operation_id="simulateParkingReplan")
async def demo_replan(request: DemoReplan) -> ToolResult:
    """在合成停车场演示车位变化后的行程重规划。回答须注明模拟。"""
    return tool_result(await backend("POST", "/demo/parking/replan", json={
        "hospital_id": "tzph-taihu-demo",
        "event": {"parking_lot_id": request.parking_lot_id, "available_spaces": request.available_spaces},
        "requires_wheelchair_access": request.requires_wheelchair_access,
        "max_walking_distance_m": request.max_walking_distance_m,
    }))


@app.post("/knowledge-search", dependencies=[Depends(authenticate)], operation_id="searchCareGuideKnowledge")
async def knowledge_search(request: KnowledgeSearch) -> ToolResult:
    """检索 CareGuide 知识库，使用检索结果及其来源回答。"""
    return tool_result(await backend("POST", "/search", params={"query": request.query, "top_k": 3}))

@app.post("/judge-start", dependencies=[Depends(authenticate)], operation_id="startJudgeDemo")
async def judge_start() -> ToolResult:
    """创建独立的合成评委会话。首次体验预约、咨询、审核或评测时调用，保留 session。"""
    return tool_result(open_session())


@app.post("/judge-operation", dependencies=[Depends(authenticate)], operation_id="runJudgeDemo")
async def judge_run(request: JudgeOperation) -> ToolResult:
    """使用当前会话的合成数据。查询后按真实返回的 ID 操作。confirm 须等待用户明确确认。"""
    return tool_result(await judge_operation(request))
