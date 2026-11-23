"""Read-only AgentArts demo gateway for the Taizhou main campus."""

import os
import re
import secrets

import httpx
from fastapi import Depends, FastAPI, HTTPException
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, ConfigDict, Field


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
    return {"hospital": "南京医科大学附属泰州人民医院总院区", "evidence": evidence}
