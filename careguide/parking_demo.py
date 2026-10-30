"""Deterministic parking replanning over explicit fictional demo data."""

from copy import deepcopy
from datetime import datetime, timezone
import re
import time
import uuid

from careguide.trace_store import TRACE_VERSION, validate_trace_completeness


DEMO_HOSPITAL_ID = 'tzph-taihu-demo'
NOTICE = ('仅用于程序开发的虚构测试数据；所有停车场、距离和状态均非真实信息，'
          '不可用于实际就医或导航。')
DEMO_LOTS = (
    {'id': 'SIM-A', 'name': '模拟停车场A', 'walking_distance_m': 80,
     'wheelchair_accessible': True, 'available_spaces': 3},
    {'id': 'SIM-B', 'name': '模拟停车场B', 'walking_distance_m': 150,
     'wheelchair_accessible': True, 'available_spaces': 2},
    {'id': 'SIM-C', 'name': '模拟停车场C', 'walking_distance_m': 350,
     'wheelchair_accessible': None, 'available_spaces': 10},
)


def _evaluate(lots: list[dict], max_distance: int, needs_access: bool) -> list[dict]:
    candidates = []
    for lot in lots:
        reasons = []
        spaces = lot['available_spaces']
        distance = lot['walking_distance_m']
        if spaces is None:
            reasons.append('空位状态未知')
        elif spaces <= 0:
            reasons.append('无空位')
        if distance is None:
            reasons.append('步行距离未知')
        elif distance > max_distance:
            reasons.append('步行距离超限')
        if needs_access and lot['wheelchair_accessible'] is not True:
            reasons.append('无障碍通行未确认' if lot['wheelchair_accessible'] is None
                           else '不支持轮椅通行')
        candidates.append({
            'id': lot['id'], 'name': lot['name'], 'eligible': not reasons,
            'reasons': reasons, 'walking_distance_m': distance,
            'available_spaces': spaces,
        })
    return candidates


def _plan(lots: list[dict], max_distance: int, needs_access: bool) -> dict:
    candidates = _evaluate(lots, max_distance, needs_access)
    eligible = sorted(
        (item for item in candidates if item['eligible']),
        key=lambda item: (item['walking_distance_m'], -item['available_spaces'], item['id']),
    )
    chosen = eligible[0] if eligible else None
    return {
        'selected_lot_id': chosen['id'] if chosen else None,
        'alternatives': [item['id'] for item in eligible[1:]],
        'candidates': candidates,
        'message': (
            f"模拟条件下首选{chosen['name']}，步行约{chosen['walking_distance_m']}米；"
            '出发前需重新确认车位与可通行路线。'
            if chosen else
            '没有满足已知条件的停车方案；请联系院方核实车位、落客点及无障碍路线。'
        ),
    }


def replan_demo(hospital_id: str, max_distance: int, needs_access: bool,
                event_lot_id: str, available_spaces: int) -> dict:
    if hospital_id != DEMO_HOSPITAL_ID:
        raise ValueError('当前仅支持明确标识的模拟医院数据')
    lots = deepcopy(DEMO_LOTS)
    before = _plan(lots, max_distance, needs_access)
    lot = next((item for item in lots if item['id'] == event_lot_id), None)
    if lot is None:
        raise LookupError('模拟事件中的停车场不存在')
    lot['available_spaces'] = available_spaces
    after = _plan(lots, max_distance, needs_access)
    previous, current = before['selected_lot_id'], after['selected_lot_id']
    if previous == current:
        change_reason = '首选方案未变化；事件未改变最优可行方案。'
    elif current is None:
        change_reason = '事件后没有满足全部已知约束的停车场。'
    else:
        prior_after_event = next(item for item in after['candidates'] if item['id'] == previous)
        change_reason = (f'原首选 {previous} 不再可行；改选 {current}。'
                         if not prior_after_event['eligible'] else
                         f'事件后 {current} 的步行距离更短；由 {previous} 改选 {current}。')
    return {
        'simulation': True, 'hospital_id': hospital_id, 'notice': NOTICE,
        'before': before,
        'event': {'parking_lot_id': event_lot_id, 'available_spaces': available_spaces},
        'after': after, 'change_reason': change_reason,
    }


def parking_chat_response(message: str, user_id: str, conv_id: str, traces) -> dict | None:
    """Handle the narrow, explicit Taizhou mobility demo without model inference."""
    hospital = re.search(r'泰州市?(?:人民)?医院', message)
    distance_match = re.search(r'最多(?:步行|走)?\s*(\d{1,4})\s*米', message)
    needs_access = '轮椅' in message
    if not hospital or not (distance_match or needs_access):
        return None
    max_distance = int(distance_match.group(1)) if distance_match else None
    started = time.monotonic()
    started_at = datetime.now(timezone.utc).isoformat()
    request_id = uuid.uuid4().hex
    access_text = '需要已确认的轮椅通行路线' if needs_access else '未提出轮椅通行要求'
    if max_distance is None:
        plan, tool_input, tool_name, status = None, None, None, 'waiting_user'
        response = (
            f'我先记录到：明天前往泰州的医院，{access_text}。为了避免给出错误路线，请确认两点：\n\n'
            '1. 你指的是泰州市人民医院太湖路总院区吗？\n'
            '2. 从落客点到门诊入口，你最多可以移动多远？是否必须全程无障碍？\n\n'
            '目前没有院方授权的实时停车数据和已核实无障碍路线；在这些信息确认前，我不会生成停车场或路线。'
        )
    else:
        plan = replan_demo(DEMO_HOSPITAL_ID, max_distance, needs_access, 'SIM-A', 0)
        tool_input = {
            'hospital_id': DEMO_HOSPITAL_ID,
            'max_walking_distance_m': max_distance,
            'requires_wheelchair_access': needs_access,
            'event': {'parking_lot_id': 'SIM-A', 'available_spaces': 0},
        }
        tool_name, status = 'replan_parking_demo', 'completed'
        response = (
            f'已识别出行约束：{access_text}，最多步行 {max_distance} 米。\n\n'
            f'模拟规划中，变化前首选 {plan["before"]["selected_lot_id"]}；'
            f'假设该模拟停车场满位后，改选 {plan["after"]["selected_lot_id"]}。'
            f'{plan["change_reason"]}\n\n'
            '这里的 SIM-A、SIM-B、距离和车位均为虚构测试数据，不可用于实际就医或导航。'
            '目前还没有泰州市人民医院授权的实时停车接口及已核实无障碍路线；实际出发前请向院方确认院区、落客点和无障碍通道。\n\n'
            '症状信息不由停车规划工具判断病因或紧急程度；如果症状明显加重或出现其他严重不适，应及时寻求医护帮助。'
        )
    finished_at = datetime.now(timezone.utc).isoformat()
    latency_ms = (time.monotonic()-started)*1000
    tool_calls = ([{
        'tool_name': tool_name, 'agent_type': 'appointment', 'input': tool_input,
        'success': True, 'output': plan, 'error': None, 'latency_ms': latency_ms,
    }] if tool_name else [])
    entities = {'hospital_id': DEMO_HOSPITAL_ID,
                'requires_wheelchair_access': needs_access}
    if max_distance is not None:
        entities['max_walking_distance_m'] = max_distance
    trace = {
        'trace_version': TRACE_VERSION, 'request_id': request_id,
        'conversation_id': conv_id, 'user_id': user_id, 'timestamp': finished_at,
        'input': message, 'started_at': started_at, 'finished_at': finished_at,
        'total_latency_ms': latency_ms, 'status': status,
        'task_understanding': {
            'task_type': 'parking_replanning_demo',
            'entities': entities,
            'missing_fields': ({'parking': ['campus_confirmation', 'max_walking_distance_m']}
                               if max_distance is None else {}),
            'workflow_status_after_understanding': status,
        },
        'routing': {
            'primary_agent': 'appointment', 'supporting_agents': [],
            'routing_reason': ('行动约束不完整，先确认院区和移动距离'
                               if max_distance is None else
                               '明确医院和行动约束触发受控模拟停车重规划'),
            'routing_confidence': 1.0,
        },
        'execution': {
            'execution_waves': [{'wave': 1, 'agents': [{
                'agent_type': 'appointment', 'role': 'mobility_planner',
                'started_at': started_at, 'finished_at': finished_at,
                'latency_ms': latency_ms, 'success': True,
                'tools_used': [tool_name] if tool_name else [], 'error': None,
            }]}],
            'tool_calls': tool_calls,
            'budget': {'model_calls': 0, 'tool_calls': len(tool_calls)},
            'synthesis': {'mode': ('constraint_clarification' if max_distance is None
                                   else 'deterministic_simulation')},
        },
        'evidence': [], 'side_effects': [],
        'final': {
            'workflow_status': status, 'final_response': response,
            'agent_types': ['appointment'], 'primary_agent': 'appointment',
            'supporting_agents': [], 'total_tool_calls': len(tool_calls),
            'total_latency_ms': latency_ms,
        },
    }
    if not validate_trace_completeness(trace)['complete']:
        raise RuntimeError('Parking demo trace contract incomplete')
    traces.append(trace)
    result = {
        'request_id': request_id, 'conv_id': conv_id, 'conversation_id': conv_id,
        'response': response, 'workflow_status': status, 'workflow': None,
        'errors': [], 'evidence': [], 'knowledge_used': False,
        'medical_answering_enabled': False, 'grounded': True, 'simulation': True,
        'action_requests': [], 'appointment': [],
        'tools_used': [tool_name] if tool_name else [],
        'agent_types': ['appointment'], 'agent_type': 'appointment',
        'primary_agent': 'appointment', 'supporting_agents': [],
        'intent': 'appointment', 'intents': ['appointment'], 'intent_group': 'appointment',
        'routing_reason': ('行动约束不完整，先确认院区和移动距离'
                           if max_distance is None else
                           '明确医院和行动约束触发受控模拟停车重规划'),
        'routing_confidence': 1.0, 'intent_confidence': 1.0,
        'execution_mode': ('constraint_clarification' if max_distance is None
                           else 'deterministic_tool'),
    }
    if plan is not None:
        result['parking_plan'] = plan
    return result
