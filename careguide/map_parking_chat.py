"""Deterministic chat handling for real hospital parking map queries."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re
import time
import uuid

from careguide.amap_models import HospitalPoi
from careguide.trace_store import TRACE_VERSION, validate_trace_completeness


EXPLICIT_PARKING = re.compile(r'停车场|停车|车停')
DRIVE_TO = re.compile(r'开车去')
HOSPITAL = re.compile(r'医院|院区|就医地点')
LOCATION_MARKER = re.compile(r'医院|院区|就医地点')
DIRECTION_TO = re.compile(r'开车去|前往|去|到')
TRAVEL_SUFFIXES = frozenset(
    {'怎么走', '怎么去', '路线', '导航', '入口', '地址', '在哪', '在哪里'}
)
SIMULATION_ID = re.compile(r'SIM-[ABC]', re.I)
SIMULATION_NEGATIONS = ('不要再', '不再', '不要', '别再', '别', '无需', '拒绝')
CLAUSE_BOUNDARY = re.compile(r'[，,。.!！？?；;\r\n]')
QUERY_NOISE = re.compile(
    r'不要模拟结果|停车场|车停哪里|怎么停|哪里停|停哪里|车停|停车|'
    r'附近|周边|查询|查找|搜索|帮我|一下|请'
)
LEADING_TRAVEL_NOISE = re.compile(
    r'^(?:我(?:想|要)?|想)?(?:开车去|前往|去|到|在)'
)
LEADING_POLITENESS = re.compile(r'^(?:麻烦|能不能)')
CITY_AFTER_TRAVEL = re.compile(
    r'(?:前往|位于|去|到|在)([\u4e00-\u9fff]{2,20}?)市'
)
KNOWN_CITIES = ('泰州', '南京', '上海', '北京', '广州', '深圳', '杭州', '苏州')


def _simulation_is_negated(prefix: str) -> bool:
    context = prefix[-6:]
    return any(context.endswith(term) for term in SIMULATION_NEGATIONS)


def _clause_containing(message: str, offset: int) -> str:
    start = 0
    for boundary in CLAUSE_BOUNDARY.finditer(message):
        if boundary.start() >= offset:
            return message[start:boundary.start()]
        start = boundary.end()
    return message[start:]


def _is_explicit_simulation(message: str) -> bool:
    if SIMULATION_ID.search(message):
        return True
    for match in re.finditer('模拟', message):
        if _simulation_is_negated(message[:match.start()]):
            continue
        following = message[match.start():match.end() + 20]
        clause = _clause_containing(message, match.start())
        if following.startswith('模拟重规划') or EXPLICIT_PARKING.search(clause):
            return True
    return False


def _strip_negated_simulation_clause(message: str) -> str:
    separator = re.search(r'[，,。；;]', message)
    if separator is None:
        return message
    clause = message[:separator.start()]
    simulation_at = clause.find('模拟')
    if simulation_at >= 0 and _simulation_is_negated(clause[:simulation_at]):
        return message[separator.end():]
    return message


def _hospital_query(message: str) -> str:
    message = _strip_negated_simulation_clause(message)
    value = QUERY_NOISE.sub('', message)
    value = value.lstrip(' \t\r\n，,。.!！？?、的')
    value = LEADING_POLITENESS.sub('', value)
    value = LEADING_TRAVEL_NOISE.sub('', value)
    return value.strip(' \t\r\n，,。.!！？?、的')


def _normalize_city_qualifier(value: str) -> str:
    for suffix in ('特别行政区', '自治区', '省'):
        if suffix in value:
            value = value.rsplit(suffix, 1)[-1]
    return value


def _directional_segment(message: str) -> str | None:
    direction = DIRECTION_TO.search(message)
    if direction is None:
        return None
    boundary = CLAUSE_BOUNDARY.search(message, direction.end())
    end = boundary.start() if boundary else len(message)
    return message[direction.end():end].strip()


def _location_prefix(segment: str) -> str | None:
    markers = list(LOCATION_MARKER.finditer(segment))
    return segment[:markers[-1].end()].strip() if markers else None


def _drive_only_is_travel_request(message: str) -> bool:
    direction = DIRECTION_TO.search(message)
    segment = _directional_segment(message)
    if direction is None or segment is None:
        return False
    target = _location_prefix(segment)
    if target is None:
        return False
    target_end = direction.end() + segment.find(target) + len(target)
    suffix = message[target_end:].strip(' \t\r\n，,。.!！？?；;、')
    return not suffix or suffix in TRAVEL_SUFFIXES


def _clause_has_only_negated_simulation(clause: str) -> bool:
    matches = list(re.finditer('模拟', clause))
    return bool(matches) and all(
        _simulation_is_negated(clause[:match.start()]) for match in matches
    )


def _local_parking_clause(message: str) -> str:
    candidates = []
    for clause in CLAUSE_BOUNDARY.split(message):
        if (not HOSPITAL.search(clause) or not EXPLICIT_PARKING.search(clause)
                or _clause_has_only_negated_simulation(clause)):
            continue
        hospital_positions = [match.start() for match in HOSPITAL.finditer(clause)]
        parking_positions = [match.start() for match in EXPLICIT_PARKING.finditer(clause)]
        distance = min(
            abs(hospital - parking)
            for hospital in hospital_positions
            for parking in parking_positions
        )
        candidates.append((distance, clause.strip()))
    return min(candidates, default=(0, message), key=lambda item: item[0])[1]


def _clean_location_source(value: str) -> str:
    value = re.sub(r'(?:附近|周边)?(?:停车场|停车|车停).*$', '', value)
    for suffix in sorted(TRAVEL_SUFFIXES, key=len, reverse=True):
        if value.endswith(suffix):
            value = value[:-len(suffix)]
            break
    return value.strip(' \t\r\n，,。.!！？?；;、')


def _nearest_known_city(source: str) -> str | None:
    markers = list(LOCATION_MARKER.finditer(source))
    limit = markers[-1].start() if markers else len(source)
    candidates = [
        (source.rfind(city, 0, limit), city)
        for city in KNOWN_CITIES
        if source.rfind(city, 0, limit) >= 0
    ]
    return max(candidates, default=(-1, None), key=lambda item: item[0])[1]


def _city_hint(source: str, cleaned_query: str, full_message: str) -> str:
    known_city = _nearest_known_city(source)
    if known_city:
        return known_city
    match = CITY_AFTER_TRAVEL.search(source)
    if match:
        return _normalize_city_qualifier(match.group(1))
    match = re.search(r'^([\u4e00-\u9fff]{2,20}?)市', cleaned_query)
    if match:
        return _normalize_city_qualifier(match.group(1))
    known_city = _nearest_known_city(full_message)
    if known_city:
        return known_city
    match = CITY_AFTER_TRAVEL.search(full_message)
    return _normalize_city_qualifier(match.group(1)) if match else ''


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
        explicit_parking = EXPLICIT_PARKING.search(message)
        drive_to = DRIVE_TO.search(message)
        if not explicit_parking and not drive_to:
            return False
        if drive_to and not explicit_parking and not _drive_only_is_travel_request(message):
            return False
        return bool(
            HOSPITAL.search(message)
            and not _is_explicit_simulation(message)
        )

    async def handle(self, message, user_id, conv_id, *, map_event=None):
        if map_event is not None:
            return await self._select(message, user_id, conv_id, map_event)
        if not self.matches(message):
            return None

        started = time.monotonic()
        segment = _directional_segment(message)
        target = _location_prefix(segment) if segment else None
        query_source = target or _local_parking_clause(message)
        query_source = _strip_negated_simulation_clause(query_source)
        query_source = _clean_location_source(query_source)
        query = _hospital_query(query_source)
        city = _city_hint(query_source, query, message)
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
        if map_event.get('kind') != 'map_hospital_select':
            raise ValueError('Unsupported map event kind')

        started = time.monotonic()
        stored = self.store.select_map_hospital(
            map_event['query_id'],
            user_id,
            conv_id,
            map_event['candidate_version'],
            map_event['hospital_id'],
            now=self._now(),
        )
        stored_hospital = HospitalPoi.model_validate(stored)
        refreshed_hospital = await self.amap.get_hospital(
            stored_hospital.hospital_id
        )
        hospital = refreshed_hospital or stored_hospital
        candidates = await self.amap.search_parking(hospital, radius_m=1000)
        serialized_candidates = [
            candidate.model_dump(mode='json') for candidate in candidates
        ]
        observed_at = self._now().isoformat()

        if candidates:
            response = (
                f'已确认院区“{hospital.name}”，找到 {len(candidates)} 个停车地点。'
                '本项目无法核实实时余位和无障碍通行，请以现场信息为准。'
            )
        else:
            response = (
                f'已确认院区“{hospital.name}”，1 公里内未找到停车地点。'
                '建议到地图页扩大范围；本项目无法核实实时余位和无障碍通行。'
            )

        return self._result(
            message,
            user_id,
            conv_id,
            started,
            'completed',
            response,
            map_parking={
                'hospital': hospital.model_dump(mode='json'),
                'candidates': serialized_candidates,
                'data_tier': 'map_poi',
                'source_provider': 'amap',
                'observed_at': observed_at,
            },
            tool_name='amap_search_parking',
            tool_input={
                'hospital_id': stored_hospital.hospital_id,
                'radius_m': 1000,
            },
            tool_output={
                'count': len(candidates),
                'parking_ids': [candidate.parking_id for candidate in candidates],
            },
        )

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
        map_parking=None,
        tool_name='amap_search_hospitals',
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
        if map_parking is not None:
            result['map_parking'] = map_parking
        return result
