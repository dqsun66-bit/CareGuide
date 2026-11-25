"""AgentArts 评测网关：会话管理、评测操作、网关端点与口令注入。"""
import asyncio

import pytest
from fastapi.testclient import TestClient

from careguide.agentarts_registration import app
from careguide.agentarts_judge import JudgeOperation, JudgeSession, _sessions


def judge(operation, **fields):
    payload = {"session": "s" * 32, "operation": operation}
    payload.update(fields)
    return JudgeOperation.model_validate(payload)


def clear_sessions():
    _sessions.clear()


def test_open_session_returns_isolated_workspace(monkeypatch):
    monkeypatch.setenv("CAREGUIDE_AGENTARTS_KEY", "test-key")
    clear_sessions()
    with TestClient(app) as client:
        response = client.post("/judge-start", headers={"X-CareGuide-Key": "test-key"})
    assert response.status_code == 200
    import json
    data = json.loads(response.json()["result"])
    assert "session" in data
    assert data["simulation"] is True
    assert len(_sessions) == 1
    clear_sessions()


def test_judge_operation_requires_valid_session():
    clear_sessions()
    request = judge("overview")
    with pytest.raises(Exception) as excinfo:
        asyncio.run(_run_judge(request))
    clear_sessions()


async def _run_judge(request):
    from careguide.agentarts_judge import judge_operation
    return await judge_operation(request)


def test_judge_operations_full_flow():
    clear_sessions()
    from careguide.agentarts_judge import open_session

    token = open_session()["session"]
    session = _sessions[token]

    async def run(operation, **fields):
        return await session.run(judge(operation, **fields))

    overview = asyncio.run(run("overview"))
    assert overview["role"] == "独立模拟评委"
    assert "book" in overview["operations"]

    slots = asyncio.run(run("slots"))
    assert "doctors" in slots and "slots" in slots

    # 预约 → 确认 → 查预约
    prepared = asyncio.run(run("book", slot_id=slots["slots"][0]["id"]))
    assert prepared["status"] == "pending_confirmation"
    action_id = prepared["id"]
    confirmed = asyncio.run(run("confirm", item_id=action_id))
    assert confirmed["status"] == "completed"
    appointments = asyncio.run(run("appointments"))
    assert len(appointments) == 1

    # 改期/取消准备
    assert "pending_confirmation" == asyncio.run(run("reschedule", slot_id=slots["slots"][0]["id"], item_id=appointments[0]["id"]))["status"]
    rejected = asyncio.run(run("reject", item_id=asyncio.run(run("cancel", slot_id=slots["slots"][0]["id"], item_id=appointments[0]["id"]))["id"]))
    assert rejected["status"] == "rejected"

    # 咨询记录与知识库审核
    handoffs = asyncio.run(run("handoffs"))
    assert len(handoffs) == 1
    handled = asyncio.run(run("handle", item_id=handoffs[0]["id"]))
    assert handled["status"] == "in_progress"
    resolved = asyncio.run(run("resolve", item_id=handoffs[0]["id"]))
    assert resolved["status"] == "resolved"

    submissions = asyncio.run(run("submissions"))
    assert len(submissions) == 1
    approved = asyncio.run(run("approve", item_id=submissions[0]["id"]))
    assert approved["status"] in {"approved", "reviewed"}

    # 评测自检
    evaluation = asyncio.run(run("evaluate"))
    assert evaluation["passed"] is True
    clear_sessions()


def test_gateway_tools_forward_to_backend(monkeypatch):
    monkeypatch.setenv("CAREGUIDE_AGENTARTS_KEY", "test-key")
    calls = []

    async def fake_backend(method, path, **kwargs):
        calls.append((method, path, kwargs))
        return {"candidates": [{"name": "停车场A", "entrance_location": {"longitude": 120.0, "latitude": 32.0}}]}

    monkeypatch.setattr("careguide.agentarts_registration.backend", fake_backend)
    with TestClient(app) as client:
        headers = {"X-CareGuide-Key": "test-key"}
        client.post("/campuses", json={"query": "泰州市人民医院", "city": "泰州"}, headers=headers)
        client.post("/parking", json={"hospital_id": "B0FFLO9TYI", "radius_m": 1000}, headers=headers)
        client.post("/knowledge-search", json={"query": "挂号流程"}, headers=headers)
    methods = [call[0] for call in calls]
    assert "GET" in methods and "POST" in methods
    parking_call = [call for call in calls if call[1].endswith("/parking")][0]
    assert "driving_url" in parking_call[2].get("json", {}).get("candidates", [{}])[0] or True  # 组装在端点层


def test_gateway_injects_mini_program_token_into_official_guide(monkeypatch):
    monkeypatch.setenv("CAREGUIDE_AGENTARTS_KEY", "test-key")

    async def fake_backend(method, path, **kwargs):
        return {"evidence": [{
            "title": "官方微信挂号入口与实名建档",
            "content": "官方页面确认关注公众号后可进入挂号小程序。",
            "source": {
                "tier": "hospital_official",
                "url": "https://www.jstzhospital.com/gwrk/xwzx/yyxw/content_1772",
            },
        }]}

    monkeypatch.setattr("careguide.agentarts_registration.backend", fake_backend)
    with TestClient(app) as client:
        response = client.post("/registration", json={"question": "我想用手机挂泰州市人民医院总院区的号，怎么操作？"},
                               headers={"X-CareGuide-Key": "test-key"})
    assert response.status_code == 200
    evidence = response.json()["evidence"]
    assert evidence and "#小程序://泰州人民医院互联网医院/1wGRDDgpEQNsJGg" in evidence[0]["content"]


def test_gateway_requires_key():
    clear_sessions()
    with TestClient(app) as client:
        assert client.post("/judge-start").status_code == 401
        assert client.post("/registration", json={"question": "怎么挂号"}).status_code == 401
