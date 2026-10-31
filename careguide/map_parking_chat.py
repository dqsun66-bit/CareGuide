"""Deterministic chat handling for real hospital parking map queries."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re
import time
import uuid

from careguide.amap_models import HospitalPoi
from careguide.trace_store import TRACE_VERSION, validate_trace_completeness


PARKING = re.compile(r'停车场|停车|车停|开车去')
HOSPITAL = re.compile(r'医院|院区|就医地点')
SIMULATION = re.compile(r'SIM-[ABC]|模拟重规划|模拟停车', re.I)
QUERY_NOISE = re.compile(
    r'不要模拟结果|停车场|车停哪里|怎么停|哪里停|停哪里|车停|停车|'
    r'附近|周边|查询|查找|搜索|帮我|一下|请'
)
LEADING_TRAVEL_NOISE = re.compile(
    r'^(?:我(?:想|要)?|想)?(?:开车去|前往|去|到|在)'
)
KNOWN_CITIES = ('泰州', '南京', '上海', '北京', '广州', '深圳', '杭州', '苏州')


def _hospital_query(message: str) -> str:
    value = QUERY_NOISE.sub('', message)
    value = value.lstrip(' \t\r\n，,。.!！？?、的')
    value = LEADING_TRAVEL_NOISE.sub('', value)
    return value.strip(' \t\r\n，,。.!！？?、的')


def _city_hint(message: str) -> str:
    for city in KNOWN_CITIES:
        if city in message:
            return city
    match = re.search(r'(?:^|[\s，,。！？?、])([\u4e00-\u9fff]{2,8})市', message)
    return match.group(1) if match else '泰州'


def _serialize_hospital(hospital: HospitalPoi) -> dict:
    return hospital.model_dump(mode='json')


class MapParkingChatService:
    def __init__(self, amap, store, traces, *, now=None, ttl_minutes=10):
        self.amap = amap
        self.store = store
        self.traces = traces
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.ttl_minutes = ttl_minutes

    @staticmethod
    def matches(message: str) -> bool:
        if not isinstance(message, str):
            return False
        return bool(
            PARKING.search(message)
            and HOSPITAL.search(message)
            and not SIMULATION.search(message)
        )

    async def handle(self, message, user_id, conv_id, *, map_event=None):
        if map_event is not None:
            return await self._select(message, user_id, conv_id, map_event)
        if not self.matches(message):
            return None

        started = time.monotonic()
        query = _hospital_query(message)
        city = _city_hint(query)
        hospitals = await self.amap.search_hospitals(query, city=city)
        tool_input = {'query': query, 'city': city}
        tool_output = {
            'count': len(hospitals),
            'hospital_ids': [hospital.hospital_id for hospital in hospitals],
        }
        if not hospitals:
            return self._result(
                message,
                user_id,
                conv_id,
                started,
                'completed',
                f'没有找到“{query}”对应的真实院区，请补充城市和医院正式名称。',
                tool_input=tool_input,
                tool_output=tool_output,
            )

        serialized = [_serialize_hospital(hospital) for hospital in hospitals]
        now = self._now()
        saved = self.store.create_map_parking_query(
            user_id,
            conv_id,
            query,
            city,
            serialized,
            expires_at=now + timedelta(minutes=self.ttl_minutes),
            now=now,
        )
        return self._result(
            message,
            user_id,
            conv_id,
            started,
            'waiting_user',
            f'找到 {len(serialized)} 个高德院区候选，请先确认要前往的院区。',
            map_selection={
                'query_id': saved['query_id'],
                'candidate_version': saved['candidate_version'],
                'expires_at': saved['expires_at'],
                'hospitals': serialized,
            },
            tool_input=tool_input,
            tool_output=tool_output,
        )

    async def _select(self, message, user_id, conv_id, map_event):
        raise NotImplementedError('Hospital candidate selection is implemented in Task 3')

    def _now(self) -> datetime:
        value = self.now() if callable(self.now) else self.now
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError('now must be timezone-aware')
        return value.astimezone(timezone.utc)

    def _result(
        self,
        message,
        user_id,
        conv_id,
        started,
        status,
        response,
        *,
        map_selection=None,
        tool_input,
        tool_output,
    ):
        request_id = uuid.uuid4().hex
        finished = self._now()
        finished_at = finished.isoformat()
        latency_ms = (time.monotonic() - started) * 1000
        started_at = datetime.fromtimestamp(
            finished.timestamp() - latency_ms / 1000,
            tz=timezone.utc,
        ).isoformat()
        tool_name = 'amap_search_hospitals'
        routing_reason = '医院停车请求使用真实地图 POI 确定性流程'
        tool_call = {
            'tool_name': tool_name,
            'agent_type': 'map_parking',
            'input': tool_input,
            'success': True,
            'output': tool_output,
            'error': None,
            'latency_ms': latency_ms,
        }
        trace = {
            'trace_version': TRACE_VERSION,
            'request_id': request_id,
            'conversation_id': conv_id,
            'user_id': user_id,
            'timestamp': finished_at,
            'input': message,
            'started_at': started_at,
            'finished_at': finished_at,
            'total_latency_ms': latency_ms,
            'status': status,
            'task_understanding': {
                'task_type': 'map_parking',
                'entities': {'data_tier': 'map_poi'},
                'missing_fields': (
                    {'map_parking': ['hospital_id']} if map_selection else {}
                ),
                'workflow_status_after_understanding': status,
            },
            'routing': {
                'primary_agent': 'map_parking',
                'supporting_agents': [],
                'routing_reason': routing_reason,
                'routing_confidence': 1.0,
            },
            'execution': {
                'execution_waves': [{
                    'wave': 1,
                    'agents': [{
                        'agent_type': 'map_parking',
                        'role': 'map_parking_lookup',
                        'started_at': started_at,
                        'finished_at': finished_at,
                        'latency_ms': latency_ms,
                        'success': True,
                        'tools_used': [tool_name],
                        'error': None,
                    }],
                }],
                'tool_calls': [tool_call],
                'budget': {'model_calls': 0, 'tool_calls': 1},
                'synthesis': {'mode': 'deterministic_map_lookup'},
            },
            'evidence': [],
            'side_effects': [],
            'final': {
                'workflow_status': status,
                'final_response': response,
                'agent_types': ['map_parking'],
                'primary_agent': 'map_parking',
                'supporting_agents': [],
                'total_tool_calls': 1,
                'total_latency_ms': latency_ms,
            },
        }
        completeness = validate_trace_completeness(trace)
        if not completeness['complete']:
            raise RuntimeError(
                f'Incomplete map parking trace: {completeness["missing_fields"]}'
            )
        self.traces.append(trace)

        result = {
            'request_id': request_id,
            'conv_id': conv_id,
            'conversation_id': conv_id,
            'response': response,
            'workflow_status': status,
            'workflow': None,
            'errors': [],
            'evidence': [],
            'knowledge_used': False,
            'medical_answering_enabled': False,
            'grounded': True,
            'simulation': False,
            'action_requests': [],
            'appointment': [],
            'tools_used': [tool_name],
            'agent_types': ['map_parking'],
            'agent_type': 'map_parking',
            'primary_agent': 'map_parking',
            'supporting_agents': [],
            'intent': 'map_parking',
            'intents': ['map_parking'],
            'intent_group': 'map_parking',
            'routing_reason': routing_reason,
            'routing_confidence': 1.0,
            'intent_confidence': 1.0,
            'execution_mode': 'deterministic_tool',
        }
        if map_selection is not None:
            result['map_selection'] = map_selection
        return result
