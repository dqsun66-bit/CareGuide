"""Deterministic routing and composition for care requests that also need maps."""

from __future__ import annotations

import re

from careguide.medical_triage import is_headache_triage_request


_CARE_ACTION = re.compile(
    r'挂号|预约|看病|就诊|看医生|挂什么科|看什么科|开药'
)
_MEDICAL_EXAM = re.compile(
    r'(?:做|安排|需要|想做|要做)[^，。！？\n]{0,6}(?:检查|检验)|'
    r'检查项目|检查报告|检验项目|检验报告'
)
_CURRENT_SYMPTOM = re.compile(
    r'(?:我|本人|患者|老人|孩子|家人)[^，。！？\n]{0,36}'
    r'(?:疼|痛|晕|不舒服|发烧|发热|咳嗽|呕吐|恶心|胸闷|气短|出血)'
)
_URGENT_STATUSES = frozenset({'urgent_handoff', 'escalated', 'emergency'})
_EMERGENCY_SIGNAL = re.compile(
    r'叫不醒|没有反应|失去意识|不省人事|意识不清|意识模糊|'
    r'说话困难|言语不清|正在抽搐|持续抽搐|全身抽搐|'
    r'大量出血|出血[^，。！？\n]{0,12}止不住|喘不上气|严重呼吸困难|'
    r'胸痛[^，。！？\n]{0,12}(?:呼吸困难|喘不上气|气短)|'
    r'(?:呼吸困难|喘不上气|气短)[^，。！？\n]{0,12}胸痛'
)
_EDUCATIONAL_SIGNAL = re.compile(r'假如|假设|科普|新闻|是什么意思|有哪些表现')
_NEGATED_SIGNAL = re.compile(r'(?:没有|无|未出现|否认)$')
_CLAUSE_BOUNDARY = re.compile(r'[，,。.!！？?；;\r\n]')
_NON_MEDICAL_EXAM_OBJECT = re.compile(r'停车|车位|地图|位置|地址|路线|导航')


def registration_clause(message: str) -> str:
    """Isolate a registration guide only when all other clauses are travel details."""
    text = message.strip(' “”。！？?!')
    clauses = [part.strip() for part in _CLAUSE_BOUNDARY.split(text) if part.strip()]
    travel = r'(?:(?:家人|朋友|我自己|我)?(?:开车|驾车)(?:送我|去)?)|(?:(?:哪里|在哪儿?|哪儿|怎么)停车(?:方便|比较方便)?)'
    care = [part for part in clauses if not re.fullmatch(travel, part)]
    if len(care) == len(clauses):
        return message
    guide = re.fullmatch(
        r'(?:我)?(?:今天|明天|后天)?(?:坐轮椅|乘轮椅)?(?:想去|要去|去|到)?'
        r'(?P<destination>(?:泰州市人民医院|南京医科大学附属泰州人民医院)总院区)'
        r'(?P<question>怎么挂号|如何挂号|挂号流程)', ''.join(care))
    return guide['destination'] + guide['question'] if guide else message


def _clause_containing(message: str, start: int, end: int) -> str:
    clause_start = 0
    for boundary in _CLAUSE_BOUNDARY.finditer(message, 0, start):
        clause_start = boundary.end()
    clause_end_match = _CLAUSE_BOUNDARY.search(message, end)
    clause_end = clause_end_match.start() if clause_end_match else len(message)
    return message[clause_start:clause_end]


def immediate_emergency_signal(message: str) -> bool:
    """High-recall zero-model safety gate used before map fast paths."""
    if not isinstance(message, str):
        return False
    for match in _EMERGENCY_SIGNAL.finditer(message):
        clause = _clause_containing(message, match.start(), match.end())
        if _EDUCATIONAL_SIGNAL.search(clause):
            continue
        prefix = message[max(0, match.start() - 4):match.start()]
        if _NEGATED_SIGNAL.search(prefix):
            continue
        return True
    return False


def _medical_exam_request(message: str) -> bool:
    for match in _MEDICAL_EXAM.finditer(message):
        clause = _clause_containing(message, match.start(), match.end())
        if not _NON_MEDICAL_EXAM_OBJECT.search(clause):
            return True
    return False


def needs_care_orchestration(message: str, history: list[dict]) -> bool:
    """Return whether a map request also contains an explicit care task."""
    if not isinstance(message, str):
        return False
    return bool(
        immediate_emergency_signal(message)
        or is_headache_triage_request(message, history)
        or _CARE_ACTION.search(message)
        or _medical_exam_request(message)
        or _CURRENT_SYMPTOM.search(message)
    )


def urgent_care_result(result: dict) -> bool:
    """Keep emergency handling ahead of travel convenience work."""
    safety = result.get('safety') if isinstance(result.get('safety'), dict) else {}
    return bool(
        result.get('escalated')
        or result.get('workflow_status') in _URGENT_STATUSES
        or safety.get('disposition') in _URGENT_STATUSES
    )


def compose_care_and_map(care: dict, map_result: dict) -> dict:
    """Attach a real map result to the care result without losing either task."""
    result = dict(care)
    care_text = str(care.get('response') or '').strip()
    map_text = str(map_result.get('response') or '').strip()
    result['response'] = '\n\n'.join(part for part in (
        care_text,
        f'出行与停车：\n{map_text}' if map_text else '',
    ) if part)

    for field in ('map_selection', 'map_parking'):
        if field in map_result:
            result[field] = map_result[field]

    def merged_values(field: str) -> list:
        values = []
        for source in (care, map_result):
            for value in source.get(field, []) or []:
                if value not in values:
                    values.append(value)
        return values

    result['intents'] = merged_values('intents')
    result['agent_types'] = merged_values('agent_types')
    result['tools_used'] = merged_values('tools_used')
    supporting = list(care.get('supporting_agents') or [])
    if 'map_parking' not in supporting:
        supporting.append('map_parking')
    result['supporting_agents'] = supporting
    result['primary_agent'] = care.get('primary_agent') or care.get('agent_type') or 'map_parking'
    result['agent_type'] = care.get('agent_type') or result['primary_agent']
    result['intent'] = care.get('intent') or (result['intents'][0] if result['intents'] else 'map_parking')
    result['intent_group'] = 'compound_care_map'
    result['routing_reason'] = (
        '复合就医请求保留医疗安全处理，并并行附加真实地图停车候选'
    )
    result['map_request_id'] = map_result.get('request_id')

    care_status = care.get('workflow_status')
    result['workflow_status'] = (
        map_result.get('workflow_status')
        if care_status in {None, 'completed'}
        else care_status
    )
    return result


_MOBILITY_TRAVEL_SIGNAL = re.compile(
    r'腿脚|走路|行走|站立|下肢|轮椅|行动不便|行动不?方便|走不了|走路不方便|腿不方便')


def mobility_travel_notice(care: dict, message: str) -> dict:
    """行动能力受限 + 出行计划：把通用 out_of_scope 文案替换为就医通行提示。

    复合请求里 care 部分可能被路由到 out_of_scope（"当前支持症状咨询……"），
    与停车结果拼接后自相矛盾。检测到行动能力信号时改为确定性的通行提示，
    保留安全价值并让后续停车信息自然衔接。
    """
    if not _MOBILITY_TRAVEL_SIGNAL.search(message):
        return care
    text = str(care.get('response') or '')
    if '当前支持症状咨询' not in text:
        return care
    result = dict(care)
    result['response'] = (
        '收到，你提到腿脚不适并计划前往医院就诊，以下是就医通行提示（一般信息，不替代诊断）：\n'
        '· 家人陪同开车可直接停到靠近门诊入口的停车场，下车后留意院区坡道与无障碍电梯；\n'
        '· 腿脚明显无力、麻木或疼痛加重时，可在入口寻求导医协助或使用轮椅通道；\n'
        '· 若出现突然不能行走、大小便失禁等情况，请立即就近急诊。\n'
        '具体就诊科室由分诊台按症状判断更稳妥，以下同时为你查找院区停车信息。'
    )
    result['workflow_status'] = 'completed'
    result['intent_group'] = 'compound_care_map'
    return result


def compose_care_with_map_failure(care: dict, code: str) -> dict:
    """Preserve medical guidance when the optional map provider is unavailable."""
    result = dict(care)
    care_text = str(care.get('response') or '').strip()
    notice = (
        '出行与停车：\n停车信息暂不可用，请稍后重试或通过医院官方渠道确认院区、'
        '落客点及无障碍入口。医疗安全提示仍然有效。'
    )
    result['response'] = '\n\n'.join(part for part in (care_text, notice) if part)
    result['intent_group'] = 'compound_care_map'
    result['map_degraded'] = {'code': code, 'provider': 'amap'}
    supporting = list(care.get('supporting_agents') or [])
    if 'map_parking' not in supporting:
        supporting.append('map_parking')
    result['supporting_agents'] = supporting
    result['routing_reason'] = '医疗安全处理已完成；地图工具不可用时保留医疗结果并安全降级'
    return result
