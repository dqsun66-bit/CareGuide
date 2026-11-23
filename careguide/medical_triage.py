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
_MOBILITY_TERM = re.compile(
    r'不能走路|无法走路|走不了路|不能行走|无法行走|站不住|不能站立|'
    r'无法站立|站不起来|走不了(?:路)?|完全不能动|'
    r'(?:双腿|下肢|腿部?|腿)[^，,。.!！？?；;\n]{0,8}'
    r'(?:无力|没力气|麻木|瘫痪)'
)
_CLAUSE_SPLIT = re.compile(r'[，,。.!！？?；;\r\n]+')
_NEGATION_PREFIX = re.compile(r'(?:没有|没|无|否认|不是|并非|未)[^，,。.!！？?；;\n]*$')
_CURRENT_MOBILITY_CONTEXT = re.compile(r'我|本人|患者|老人|孩子|家人|现在|今天|刚刚|突然')
_HYPOTHETICAL = re.compile(r'担心|假如|如果|以后|将来|万一')
_RESOLVED_MOBILITY = re.compile(
    r'(?:走路|行走|站立|行动|腿部?)(?:能力)?(?:已经|现已|早已)?(?:恢复|好了)|'
    r'现在能走|已经能走|现已能走'
)
_PAST_CONTEXT = re.compile(r'以前|曾经|过去|当时')
_ACUTE_CONTEXT = re.compile(r'突然|刚刚|骤然|今天突然')
_TEMPORAL_BOUNDARY = re.compile(r'(?=现在|今天|刚刚|突然|以前|曾经|过去|当时)')
_CHRONIC_MOBILITY = re.compile(r'长期|一直|多年|从小|轮椅用户')
_ACCESSIBILITY_INFO = re.compile(r'无障碍|入口|停车|路线|导航|地址')
_ACTIVE_CARE_REQUEST = re.compile(r'检查|看病|就医|挂号|急诊|加重|恶化|新出现')
_NON_LIVE_INFORMATION = re.compile(r'想了解|科普|新闻|案例|别人说|听说|查资料')
_CURRENT_SELF_FACT = re.compile(r'(?:我|本人)(?:现在|今天|刚刚|目前|正在)')
_EXPLICIT_CURRENT_MOBILITY = re.compile(
    r'(?:现在|今天|刚刚|目前|正在)[^，,。.!！？?；;\n]{0,16}'
    r'(?:不能走路|无法走路|走不了|不能行走|无法行走|站不住|站不起来|'
    r'不能站立|无法站立|完全不能动|双腿瘫痪|下肢无力|双腿没力气)'
)
_SUBJECT = re.compile(r'我|本人|家人|父亲|母亲|爸爸|妈妈|老人|孩子|患者|他|她')
_CONTRAST = re.compile(r'但有|仍有|不过|然而|可是|但|却|而')
_VAGUE_FOLLOWUP = re.compile(r'^(?:那|那么)?(?:我)?(?:现在)?(?:该|要)?怎么办[？?]?$')
_MOBILITY_DANGER_TERMS = (
    '大小便失禁', '排尿困难', '会阴麻木', '双腿瘫痪', '完全不能动',
    '摔倒', '车祸', '外伤', '不能承重', '无法承重',
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


def is_mobility_triage_request(message: str, history: list[dict]) -> bool:
    """Identify a current loss of walking or standing ability before planning."""
    current = str(message).strip()
    if _active_mobility_clause(current) is not None:
        return True
    if _VAGUE_FOLLOWUP.fullmatch(current):
        return _active_mobility_clause(_last_user_text(history)) is not None
    return False


def _last_user_text(history: Iterable[dict]) -> str:
    for turn in reversed(list(history)):
        if turn.get('role') == 'user':
            return str(turn.get('content', '')).strip()
    return ''


def _term_is_negated(clause: str, start: int) -> bool:
    return bool(_NEGATION_PREFIX.search(clause[:start]))


def _event_records(text: str) -> list[tuple[str, str | None]]:
    records: list[tuple[str, str | None]] = []
    carried_subject: str | None = None
    for clause in _CLAUSE_SPLIT.split(text):
        for contrast_part in _CONTRAST.split(clause):
            for part in _TEMPORAL_BOUNDARY.split(contrast_part):
                part = part.strip()
                if not part:
                    continue
                explicit_subject = _SUBJECT.search(part)
                if explicit_subject:
                    carried_subject = explicit_subject.group()
                records.append((part, carried_subject))
    return records


def _event_segments(text: str) -> list[str]:
    return [segment for segment, _ in _event_records(text)]


def _active_mobility_event(text: str) -> tuple[str, str | None] | None:
    if (_NON_LIVE_INFORMATION.search(text)
            and not (_CURRENT_SELF_FACT.search(text)
                     or _EXPLICIT_CURRENT_MOBILITY.search(text))):
        return None
    candidates: list[tuple[int, str, str | None]] = []
    for clause, subject in _event_records(text):
        if (_HYPOTHETICAL.search(clause) or _RESOLVED_MOBILITY.search(clause)
                or _PAST_CONTEXT.search(clause)):
            continue
        match = _MOBILITY_TERM.search(clause)
        if match is None or _term_is_negated(clause, match.start()):
            continue
        if _CURRENT_MOBILITY_CONTEXT.search(clause):
            score = 3 if _ACUTE_CONTEXT.search(clause) else 2 if re.search(
                r'现在|今天|刚刚', clause) else 1
            candidates.append((score, clause, subject))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    _, selected, subject = candidates[0]
    if (_CHRONIC_MOBILITY.search(text) and _ACCESSIBILITY_INFO.search(text)
            and not _ACTIVE_CARE_REQUEST.search(text)
            and not _ACUTE_CONTEXT.search(selected)):
        return None
    return selected, subject


def _active_mobility_clause(text: str) -> str | None:
    """Return the current mobility event clause, excluding informational cases."""
    event = _active_mobility_event(text)
    return event[0] if event else None


def _mobility_source(message: str, history: list[dict]) -> tuple[str | None, str]:
    current = str(message).strip()
    event = _active_mobility_event(current)
    if event is not None:
        clause, active_subject = event
        current_segments = [segment for segment, subject in _event_records(current)
                            if not (_PAST_CONTEXT.search(segment)
                                    or _HYPOTHETICAL.search(segment))
                            and subject == active_subject]
        return clause, '，'.join(current_segments)
    if _VAGUE_FOLLOWUP.fullmatch(current):
        prior = _last_user_text(history)
        event = _active_mobility_event(prior)
        if event is None:
            return None, prior
        clause, active_subject = event
        current_segments = [segment for segment, subject in _event_records(prior)
                            if not (_PAST_CONTEXT.search(segment)
                                    or _HYPOTHETICAL.search(segment))
                            and subject == active_subject]
        return clause, '，'.join(current_segments)
    return None, current


def _mobility_term_states(text: str) -> tuple[set[str], set[str]]:
    positive: set[str] = set()
    negated: set[str] = set()
    segments = re.split(r'[，,。.!！？?；;\r\n]+|但有|仍有|不过|然而|可是|但|却|而', text)
    for segment in segments:
        negation_starts = [match.start() for match in re.finditer(
            r'没有|没|无|否认|不是|并非|未', segment)]
        for term in _MOBILITY_DANGER_TERMS:
            offset = 0
            while True:
                index = segment.find(term, offset)
                if index < 0:
                    break
                (negated if any(start < index for start in negation_starts)
                 else positive).add(term)
                offset = index + len(term)
    return positive, negated


def evaluate_mobility_triage(
        message: str, history: list[dict]) -> TriageDecision:
    """Safety navigation for walking/standing loss; never diagnoses a cause."""
    active_clause, source = _mobility_source(message, history)
    positive, negated = _mobility_term_states(source)
    acute = bool(active_clause and re.search(r'突然|刚刚|骤然|今天突然', active_clause))
    spinal_red_flag = bool(positive.intersection({
        '大小便失禁', '排尿困难', '会阴麻木', '双腿瘫痪', '完全不能动'}))
    trauma = bool(positive.intersection({'摔倒', '车祸', '外伤'}))
    cannot_bear_weight = bool(positive.intersection({'不能承重', '无法承重'}))
    red_flags = []
    if acute:
        red_flags.append('acute_mobility_loss')
    if spinal_red_flag:
        red_flags.append('possible_spinal_emergency')
    if trauma and cannot_bear_weight:
        red_flags.append('trauma_with_inability_to_bear_weight')
    if red_flags:
        return TriageDecision(
            status='emergency',
            symptoms=('mobility_impairment',),
            negated=tuple(sorted(negated)),
            red_flags=tuple(red_flags),
            questions=(),
            department='急诊',
            run_rag=False,
        )
    return TriageDecision(
        status='needs_clarification',
        symptoms=('mobility_impairment',),
        negated=tuple(sorted(negated)),
        red_flags=(),
        questions=(
            '这是突然发生还是长期行动不便？如果是突然不能行走，请立即前往急诊；无法安全转运时联系120。',
            '是否有摔倒或其他外伤、剧烈疼痛、明显肿胀、腿部无力或麻木？',
            '是哪条腿不适？现在能否站立或承重，是否伴有大小便异常？',
        ),
        department=None,
        run_rag=False,
    )


def evaluate_medical_triage(
        message: str, history: list[dict]) -> TriageDecision | None:
    """Return a deterministic safety decision for supported live symptoms."""
    if is_headache_triage_request(message, history):
        return evaluate_headache_triage(message, history)
    if is_mobility_triage_request(message, history):
        return evaluate_mobility_triage(message, history)
    return None


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
