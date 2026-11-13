import hashlib
import json
from dataclasses import dataclass
from datetime import date
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from careguide.api import app
from careguide.corpus import Corpus, DEFAULT_CORPUS
from careguide.domain import AgentType
from careguide.hospital_corpus.retrieval import (
    HospitalRetrievalReport,
    HospitalRetrievalResult,
)
from careguide.retrieval.knowledge_base import MedicalKnowledgeBase
from careguide.tools import MedicalToolRegistry

TODAY = date(2026, 9, 17)
PRODUCT = {'drug_name': '精氨酸布洛芬片', 'dosage_form': '片剂', 'specification': '0.2g'}


@pytest.fixture
def registry():
    return MedicalToolRegistry(MedicalKnowledgeBase(Corpus()))


def query(registry, agent, tool, args, today=TODAY):
    return registry.query(agent, tool, args, today=today)


@dataclass
class FakeHospitalKnowledge:
    unknown: bool = False
    multiple_campuses: bool = False

    def __post_init__(self):
        self.corpus = SimpleNamespace(
            manifest=SimpleNamespace(version="jiangsu-hospitals-v2"))
        self.calls = []

    def resolve_hospital(self, identity):
        if self.unknown:
            return None
        campuses = [SimpleNamespace(
            campus_id="js_taizhou_001_main", official_name="总院区")]
        if self.multiple_campuses:
            campuses.append(SimpleNamespace(
                campus_id="js_taizhou_001_east", official_name="东院区"))
        return SimpleNamespace(
            hospital_id="js_taizhou_001",
            official_name="南京医科大学附属泰州人民医院",
            campuses=tuple(campuses),
        )

    def resolve_campus(self, hospital, identity=None):
        if identity is None:
            return hospital.campuses[0] if len(hospital.campuses) == 1 else None
        return next((campus for campus in hospital.campuses if identity in {
            campus.campus_id, campus.official_name}), None)

    def search_with_report(self, query, hospital, **kwargs):
        self.calls.append((query, hospital, kwargs))
        evidence = ({
            "id": "js_taizhou_001_appointment_0003",
            "content": "官方挂号资料",
            "scope": {
                "hospital_id": "js_taizhou_001",
                "campus_id": "js_taizhou_001_main",
                "category": "appointment",
            },
            "source": {
                "url": "https://wjw.taizhou.gov.cn/official",
                "review_status": "verified_official",
            },
            "retrieval": {"mode": "hybrid_rrf_rerank", "rerank_score": 0.91},
        },)
        return HospitalRetrievalResult(evidence, HospitalRetrievalReport(
            "hybrid", 1, 1, 1, True, 0, 1))


@pytest.mark.parametrize('spec,frequency,page', [
    ('200mg', '一日3次', 'PDF页33'), ('0.4克', '一日2次', 'PDF页35'),
])
def test_exact_label_specification_prevents_dose_mixups(registry, spec, frequency, page):
    result = query(registry, AgentType.MEDICATION, 'lookup_drug_label',
                   {**PRODUCT, 'specification': spec, 'section': 'dosage'})
    assert result['status'] == 'found'
    assert len(result['evidence']) == 1
    evidence = result['evidence'][0]
    assert frequency in evidence['content']
    assert page in evidence['citation']['locator']
    assert evidence['scope']['label_type'] == 'regulatory_template'
    assert not evidence['citation']['clinical_reviewed']
    assert result['side_effects'] == []


@pytest.mark.parametrize('args,status', [
    ({'drug_name': '精氨酸布洛芬片'}, 'needs_clarification'),
    ({**PRODUCT, 'dosage_form': '胶囊'}, 'not_covered'),
    ({**PRODUCT, 'specification': '0.3g'}, 'not_covered'),
    ({**PRODUCT, 'drug_name': '布洛芬'}, 'not_covered'),
])
def test_missing_or_different_product_never_substitutes_a_label(registry, args, status):
    result = query(registry, AgentType.MEDICATION, 'lookup_drug_label', args)
    assert result['status'] == status
    assert result['evidence'] == []


def test_nasal_label_is_independent_of_tablet(registry):
    result = query(registry, AgentType.MEDICATION, 'lookup_drug_label', {
        'drug_name': '富马酸酮替芬鼻喷雾剂', 'dosage_form': '鼻喷雾剂',
        'specification': '15毫升：16.7毫克', 'section': 'dosage'})
    assert result['status'] == 'found'
    assert '鼻腔喷雾' in result['evidence'][0]['content']
    assert '口服' not in result['evidence'][0]['content']


@pytest.mark.parametrize('other', ['肝素', '甲氨蝶呤'])
def test_interactions_return_only_explicit_sourced_entries(registry, other):
    result = query(registry, AgentType.MEDICATION, 'lookup_drug_interaction',
                   {**PRODUCT, 'other_drug': other})
    assert result['status'] == 'found'
    assert result['safe_to_combine'] is None
    assert all(e['scope']['other_drug'] == other for e in result['evidence'])


def test_missing_interaction_is_not_a_safety_assurance(registry):
    result = query(registry, AgentType.MEDICATION, 'lookup_drug_interaction',
                   {**PRODUCT, 'other_drug': '阿托伐他汀'})
    assert result['status'] == 'not_covered'
    assert result['safe_to_combine'] is None
    assert '不表示没有相互作用' in result['message']
    assert result['evidence'] == []


@pytest.mark.parametrize('args,status', [
    ({}, 'needs_clarification'),
    ({'hospital_id': 'pkufh'}, 'needs_clarification'),
    ({'hospital_id': 'pkufh', 'campus': '德胜院区'}, 'not_covered'),
    ({'hospital_id': 'unknown', 'campus': '本院'}, 'not_covered'),
])
def test_hospital_and_campus_must_be_confirmed(registry, args, status):
    result = query(registry, AgentType.APPOINTMENT, 'lookup_hospital_guide', args)
    assert result['status'] == status
    assert result['evidence'] == []


def test_lexical_search_cannot_return_other_hospital_rules(registry):
    result = query(registry, AgentType.APPOINTMENT, 'lookup_hospital_guide', {
        'hospital_id': 'pkufh', 'campus': '大兴院区', 'query': '挂号流程缴费'})
    assert result['status'] == 'found'
    assert result['evidence'][0]['record_id'] == 'pkufh-daxing-process'
    assert all(e['scope']['hospital_id'] == 'pkufh' and e['scope']['campus'] == '大兴院区'
               for e in result['evidence'])


def test_hospital_guide_delegates_to_v2_and_clamps_top_k():
    hospital_knowledge = FakeHospitalKnowledge()
    registry = MedicalToolRegistry(
        MedicalKnowledgeBase(Corpus()),
        hospital_knowledge=hospital_knowledge,
    )

    response = query(registry, AgentType.APPOINTMENT, 'lookup_hospital_guide', {
        'hospital_id': '南京医科大学附属泰州人民医院',
        'query': '怎么挂号',
        'section': 'channels',
        'top_k': 10,
    })

    assert response['status'] == 'found'
    assert response['hospital_corpus_version'] == 'jiangsu-hospitals-v2'
    assert response['retrieval_mode'] == 'hybrid'
    assert response['retrieval']['reranked'] is True
    assert len(response['evidence']) <= 5
    assert all(item['source']['review_status'] == 'verified_official'
               for item in response['evidence'])
    assert hospital_knowledge.calls[0][2]['top_k'] == 5
    assert hospital_knowledge.calls[0][2]['category'] == {
        'appointment', 'contact'}


def test_hospital_v2_unknown_hospital_is_not_covered():
    registry = MedicalToolRegistry(
        MedicalKnowledgeBase(Corpus()),
        hospital_knowledge=FakeHospitalKnowledge(unknown=True),
    )

    response = query(registry, AgentType.APPOINTMENT, 'lookup_hospital_guide', {
        'hospital_id': '不存在医院', 'query': '怎么挂号'})

    assert response['status'] == 'not_covered'
    assert response['evidence'] == []


def test_hospital_v2_ambiguous_campus_needs_clarification():
    registry = MedicalToolRegistry(
        MedicalKnowledgeBase(Corpus()),
        hospital_knowledge=FakeHospitalKnowledge(multiple_campuses=True),
    )

    response = query(registry, AgentType.APPOINTMENT, 'lookup_hospital_guide', {
        'hospital_id': '南京医科大学附属泰州人民医院',
        'query': '怎么挂号',
    })

    assert response['status'] == 'needs_clarification'
    assert response['missing_fields'] == ['campus']
    assert len(response['available_campuses']) == 2


def test_registry_without_hospital_v2_keeps_legacy_behavior(registry):
    response = query(registry, AgentType.APPOINTMENT, 'lookup_hospital_guide', {
        'hospital_id': 'pkufh',
        'campus': '大兴院区',
        'query': '挂号流程缴费',
    })

    assert response['status'] == 'found'
    assert response['evidence'][0]['record_id'] == 'pkufh-daxing-process'
    assert 'hospital_corpus_version' not in response


def test_department_is_service_scope_not_a_diagnosis(registry):
    result = query(registry, AgentType.TRIAGE, 'lookup_department', {
        'hospital_id': 'pkuih', 'campus': '本院', 'department': '神经内科'})
    assert result['status'] == 'found'
    assert '不是' in result['evidence'][0]['content']
    assert not result['personalized_medical_advice']


def test_general_care_search_has_no_unrequested_hospital_scope(registry):
    result = query(registry, AgentType.TRIAGE, 'search_care_guidance', {'query': '反复头痛如何记录就医信息'})
    assert result['status'] == 'found'
    assert result['evidence'][0]['record_id'] == 'headache-diary'
    assert all(e['kind'] == 'care_guidance' for e in result['evidence'])
    assert all(e['citation']['jurisdiction'] == 'GB' for e in result['evidence'])
    assert result['retrieval']['mode'] == 'bm25'
    assert result['retrieval']['final_count'] == len(result['evidence'])
    assert result['retrieval']['final_count'] <= 5


def test_unknown_topic_has_no_fabricated_answer(registry):
    result = query(registry, AgentType.TRIAGE, 'search_care_guidance', {'query': '量子磁疗仪抗癌'})
    assert result['status'] == 'not_covered'
    assert result['evidence'] == []


@pytest.mark.parametrize('today', [date(2026, 9, 16), date(2026, 10, 18)])
def test_not_yet_verified_or_expired_source_content_is_blocked(registry, today):
    result = query(registry, AgentType.APPOINTMENT, 'lookup_hospital_guide',
                   {'hospital_id': 'pkuih', 'campus': '本院', 'section': 'materials'}, today=today)
    assert result['status'] == 'source_unavailable'
    assert result['evidence'] == []


def test_queries_do_not_change_files_or_leak_mutable_evidence(registry):
    before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in DEFAULT_CORPUS.glob('*.json')}
    first = query(registry, AgentType.MEDICATION, 'lookup_drug_label', PRODUCT)
    first['evidence'][0]['content'] = 'tampered'
    first['evidence'][0]['citation']['url'] = 'https://evil.example/'
    second = query(registry, AgentType.MEDICATION, 'lookup_drug_label', PRODUCT)
    assert all(e['content'] != 'tampered' for e in second['evidence'])
    assert all('evil' not in e['citation']['url'] for e in second['evidence'])
    after = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in DEFAULT_CORPUS.glob('*.json')}
    assert before == after


def test_restart_rebuilds_same_lexical_index_from_verified_corpus(registry):
    args = {'hospital_id': 'pkuih', 'campus': '本院', 'query': '挂号需要带什么材料'}
    expected = query(registry, AgentType.APPOINTMENT, 'lookup_hospital_guide', args)
    restarted = MedicalToolRegistry(MedicalKnowledgeBase(Corpus()))
    assert query(restarted, AgentType.APPOINTMENT, 'lookup_hospital_guide', args) == expected


def test_http_rejects_cross_scope_writes_unknown_arguments_and_wrong_types():
    client = TestClient(app)
    assert client.post('/tools/query', json={'agent': 'triage', 'tool': 'lookup_drug_label',
                                           'arguments': PRODUCT}).status_code == 403
    assert client.post('/tools/query', json={'agent': 'appointment', 'tool': 'submit_appointment',
                                           'arguments': {}}).status_code == 403
    for extra in [{'dosage_for_patient': '2 tablets'}, {'corpus_path': '/forbidden/external/corpus'},
                  {'drug_name': 123}]:
        response = client.post('/tools/query', json={'agent':'medication', 'tool':'lookup_drug_label',
                                                    'arguments': {**PRODUCT, **extra}})
        assert response.status_code == 422


def test_http_returns_working_definitions_and_source_citations():
    client = TestClient(app)
    payload = client.get('/tools', params={'agent': 'medication'}).json()
    definitions = payload['tools']
    assert {d['function']['name'] for d in definitions} == {'lookup_drug_label', 'lookup_drug_interaction'}
    assert payload['read_only']
    assert all(set(d) == {'type','function'} and d['function']['parameters']['additionalProperties'] is False
               for d in definitions)
    response = client.post('/tools/query', json={'agent': 'medication', 'tool': 'lookup_drug_label',
                                               'arguments': PRODUCT})
    assert response.status_code == 200
    assert response.json()['evidence'][0]['citation']['url'].startswith('https://www.nmpa.gov.cn/')
    assert not client.get('/capabilities').json()['medical_answering_enabled']


def test_manifest_detects_content_tampering(tmp_path):
    for file in DEFAULT_CORPUS.glob('*.json'):
        (tmp_path/file.name).write_bytes(file.read_bytes())
    (tmp_path/'records.json').write_text('[]', encoding='utf-8')
    with pytest.raises(ValueError, match='integrity'):
        Corpus(tmp_path)


def test_non_official_source_is_rejected_even_if_file_hash_is_updated(tmp_path):
    for file in DEFAULT_CORPUS.glob('*.json'):
        (tmp_path/file.name).write_bytes(file.read_bytes())
    path = tmp_path/'sources.json'
    sources = json.loads(path.read_text(encoding='utf-8'))
    sources[0]['url'] = 'https://www.nmpa.gov.cn.evil.example/label.pdf'
    path.write_text(json.dumps(sources, ensure_ascii=False), encoding='utf-8')
    manifest_path = tmp_path/'manifest.json'
    manifest = json.loads(manifest_path.read_text())
    manifest['files']['sources.json'] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match='official HTTPS authority'):
        Corpus(tmp_path)
