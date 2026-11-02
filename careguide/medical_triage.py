"""Deterministic symptom safety triage that runs before retrieval.

This module provides navigation and emergency escalation signals.  It does not
diagnose a condition and deliberately asks for missing facts before RAG runs.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable


@dataclass(frozen=True)
class TriageDecision:
    status: str
    symptoms: tuple[str, ...]
    negated: tuple[str, ...]
    red_flags: tuple[str, ...]
    questions: tuple[str, ...]
    department: str | None
    run_rag: bool


_NEGATION = re.compile(
    r"(?:没有|没出现|无|未出现)([^。；！？\n]*?)(?=但|不过|然而|[。；！？\n]|$)"
)
_HEADACHE_TERMS = ("头痛", "头疼", "头有点疼")
_DANGER_TERMS = (
    "肢体无力", "肢体麻木", "面歪", "口角歪斜", "说话困难", "言语不清",
    "意识不清", "意识模糊", "抽搐", "高热", "颈部僵硬", "颈硬", "畏光",
    "不褪色皮疹", "头部外伤", "撞到头", "视力丧失", "看不见", "受伤",
)


def is_headache_triage_request(message: str, history: list[dict]) -> bool:
    """Return whether this turn asks for live symptom safety/navigation.

    General educational queries such as "头痛日记怎么记录" stay in ordinary
    retrieval.  First-person/current symptoms and explicit urgency or department
    questions enter the deterministic safety gate.
    """
    current = str(message).strip()
    prior = _recent_user_text(history)
    explicit = bool(re.search(
        r"判断.*(?:紧急|急症)|是否.*(?:紧急|急症)|需要紧急就医|"
        r"挂什么科|看什么科|看哪个科",
        current,
    ))
    current_symptom = bool(re.search(
        r"(?:我|本人|患者|现在|今天|刚刚|突然)[^。！？\n]{0,24}"
        r"(?:头痛|头疼|头有点疼)|(?:头痛|头疼|头有点疼)[^。！？\n]{0,16}"
        r"(?:了|不舒服|[0-9一二三四五六七八九十]+分)",
        current,
    ))
    contextual = explicit and any(term in prior for term in _HEADACHE_TERMS)
    return explicit or current_symptom or contextual


def _recent_user_text(history: Iterable[dict]) -> str:
    return "\n".join(
        str(turn.get("content", ""))
        for turn in list(history)[-6:]
        if turn.get("role") == "user"
    )


def _negated_terms(text: str) -> set[str]:
    spans = [match.group(1) for match in _NEGATION.finditer(text)]
    return {term for term in _DANGER_TERMS if any(term in span for span in spans)}


def evaluate_headache_triage(message: str, history: list[dict]) -> TriageDecision:
    text = f"{_recent_user_text(history)}\n{message}".strip()
    if not any(term in text for term in _HEADACHE_TERMS):
        return TriageDecision(
            status="needs_clarification",
            symptoms=(),
            negated=(),
            red_flags=(),
            questions=("请描述主要症状、开始时间和严重程度。",),
            department=None,
            run_rag=False,
        )

    negated = _negated_terms(text)

    def present(term: str) -> bool:
        return term in text and term not in negated

    red_flags: list[str] = []
    sudden = bool(re.search(r"突然|几秒|雷击|霹雳", text))
    severe = bool(re.search(r"剧烈|严重|最痛|一生中最", text))
    if sudden and severe:
        red_flags.append("thunderclap")
    if any(present(term) for term in (
        "肢体无力", "肢体麻木", "面歪", "口角歪斜", "说话困难", "言语不清",
        "意识不清", "意识模糊", "抽搐",
    )):
        red_flags.append("focal_neurologic_deficit")
    if present("高热") and any(present(term) for term in (
        "颈部僵硬", "颈硬", "畏光", "不褪色皮疹",
    )):
        red_flags.append("meningism")
    if any(present(term) for term in ("头部外伤", "撞到头", "受伤")):
        red_flags.append("head_injury")
    if any(present(term) for term in ("视力丧失", "看不见")):
        red_flags.append("vision_loss")

    if red_flags:
        return TriageDecision(
            status="emergency",
            symptoms=("headache",),
            negated=tuple(sorted(negated)),
            red_flags=tuple(red_flags),
            questions=(),
            department="急诊",
            run_rag=False,
        )

    missing = tuple(
        question
        for pattern, question in (
            (r"今天|昨天|小时|天|周|突然|逐渐|刚刚|上午|下午|晚上",
             "头痛从什么时候开始？是突然出现还是逐渐出现？"),
            (r"[0-9一二三四五六七八九十]+分|轻微|较轻|中度|严重|剧烈",
             "疼痛程度0到10分是多少？"),
            (r"发热|呕吐|视力|肢体|说话|外伤|受伤|没有|没出现|无|未出现",
             "是否伴随发热、呕吐、视力变化、肢体无力或近期外伤？"),
        )
        if not re.search(pattern, text)
    )
    if missing:
        return TriageDecision(
            status="needs_clarification",
            symptoms=("headache",),
            negated=tuple(sorted(negated)),
            red_flags=(),
            questions=missing,
            department=None,
            run_rag=False,
        )

    return TriageDecision(
        status="non_emergency_information",
        symptoms=("headache",),
        negated=tuple(sorted(negated)),
        red_flags=(),
        questions=(),
        department="神经内科",
        run_rag=True,
    )
