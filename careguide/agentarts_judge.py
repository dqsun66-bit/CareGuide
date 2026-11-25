"""Per-session synthetic competition workspace, separate from operational stores."""
import json
import secrets
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, Field

from .action_store import ActionStore
from .appointment_service import AppointmentService
from .business_store import BusinessStore
from .hospital_workflow import InMemoryHospitalBackend
from .corpus import DEFAULT_CORPUS
from .knowledge_workflow import KnowledgeBundle, KnowledgeWorkflow
from .parking_demo import DEMO_HOSPITAL_ID, replan_demo


class JudgeOperation(BaseModel):
    session: str = Field(min_length=20, max_length=100)
    operation: Literal["overview", "slots", "book", "reschedule", "cancel", "confirm",
                       "reject", "appointments", "handoffs", "handle", "resolve",
                       "submissions", "approve", "decline", "publish", "evaluate"]
    item_id: str = Field(default="", max_length=100)
    slot_id: str = Field(default="", max_length=100)


class JudgeSession:
    def __init__(self):
        self.files = TemporaryDirectory(prefix="careguide-judge-")
        db = Path(self.files.name) / "demo.db"
        self.hospital = InMemoryHospitalBackend()
        self.booking = AppointmentService(self.hospital, ActionStore(db))
        self.store = BusinessStore(db)
        self.knowledge = KnowledgeWorkflow(self.store)
        self.last_used = monotonic()
        self.store.create_handoff("demo", "demo", "fixture", "合成场景：家属咨询轮椅到院流程。",
                                  [], [], "routine")
        # Reuse a source-validated public fixture with a demo-only record ID.
        records = json.loads((DEFAULT_CORPUS / "records.json").read_text(encoding="utf-8"))
        sources = json.loads((DEFAULT_CORPUS / "sources.json").read_text(encoding="utf-8"))
        record = next(item for item in records if item["kind"] == "hospital_guide")
        source = next(item for item in sources if item["id"] == record["source_id"])
        bundle = KnowledgeBundle.model_validate({"records": [{**record, "id": "judge-demo-guide"}],
                                                 "sources": [source]})
        self.knowledge.submit(bundle, "模拟资料员")

    async def run(self, request):
        op, item = request.operation, request.item_id
        if op == "overview":
            return {"role": "独立模拟评委", "operations": list(JudgeOperation.model_fields["operation"].annotation.__args__)}
        if op == "slots":
            return {"doctors": await self.hospital.search_doctors(),
                    "slots": await self.hospital.list_available_slots()}
        if op == "appointments":
            return await self.hospital.list_appointments("demo")
        if op in {"book", "reschedule", "cancel"}:
            action = {"book": "appointment_create", "reschedule": "appointment_reschedule",
                      "cancel": "appointment_cancel"}[op]
            return self.booking.prepare(action, "demo", "demo",
                                        {"slot_id": request.slot_id, "appointment_id": item})
        if op == "confirm":
            return await self.booking.confirm(item, "demo", "demo")
        if op == "reject":
            return self.booking.reject(item, "demo", "demo")
        if op == "handoffs":
            return self.store.list_handoffs("demo")
        if op in {"handle", "resolve"}:
            return self.store.transition_handoff(item, "demo", "in_progress" if op == "handle" else "resolved")
        if op == "submissions":
            return self.knowledge.list()
        if op in {"approve", "decline"}:
            return self.knowledge.review(item, "模拟评委", op == "approve", "演示审核")
        if op == "publish":
            return self.knowledge.publish(item, None)
        if op == "evaluate":
            result = replan_demo(DEMO_HOSPITAL_ID, 1000, False, "SIM-A", 0)
            checks = {"满位前模拟停车场A可选": result["before"]["selected_lot_id"] == "SIM-A",
                      "满位后选择其他停车场": result["after"]["selected_lot_id"] not in (None, "SIM-A")}
            return {"scope": "停车重规划合成回归用例", "checks": checks, "passed": all(checks.values())}
        raise ValueError("不支持的操作")


_sessions = {}


def open_session():
    for token, session in list(_sessions.items()):
        if monotonic() - session.last_used > 3600:
            del _sessions[token]
    if len(_sessions) >= 64:
        raise HTTPException(429, "演示会话已满，请稍后再试")
    token = secrets.token_urlsafe(32)
    _sessions[token] = JudgeSession()
    return {"session": token, "simulation": True, "expires_after_idle_minutes": 60}


async def judge_operation(request):
    session = _sessions.get(request.session)
    if session is None or monotonic() - session.last_used > 3600:
        raise HTTPException(404, "演示会话已过期，请重新开始")
    session.last_used = monotonic()
    try:
        return {"simulation": True, "data": await session.run(request)}
    except (ValueError, LookupError) as exc:
        raise HTTPException(409, str(exc)) from exc
