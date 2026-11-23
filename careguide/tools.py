"""Typed, agent-scoped, read-only medical source queries.

All results are evidence or clarification, never diagnosis or a transaction.
The registry advertises only handlers that actually exist.
"""
from __future__ import annotations

import re
from dataclasses import asdict
from datetime import date, datetime, timezone
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from careguide.corpus import Corpus, DocumentKind, Record
from careguide.domain import AgentType, require_tool
from careguide.retrieval.knowledge_base import MedicalKnowledgeBase

Text = Annotated[str, Field(min_length=1, max_length=200)]
Query = Annotated[str, Field(min_length=1, max_length=1000)]


class Arguments(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True, strict=True)


class CareGuidanceArgs(Arguments):
    query: Query
    hospital_id: Text | None = None
    campus: Text | None = None
    top_k: int = Field(default=5, ge=1, le=10)


class DepartmentArgs(Arguments):
    hospital_id: Text | None = None
    campus: Text | None = None
    department: Text | None = None


class DrugLabelArgs(Arguments):
    drug_name: Text
    dosage_form: Text | None = None
    specification: Text | None = None
    section: Literal['dosage', 'contraindications', 'precautions', 'all'] = 'all'


class DrugInteractionArgs(Arguments):
    drug_name: Text
    other_drug: Text
    dosage_form: Text | None = None
    specification: Text | None = None


class HospitalGuideArgs(Arguments):
    hospital_id: Text | None = None
    campus: Text | None = None
    query: Query | None = None
    section: Literal['channels', 'process', 'materials', 'cancellation', 'followup', 'all'] = 'all'
    top_k: int = Field(default=5, ge=1, le=10)


ARGUMENT_TYPES = {
    'search_care_guidance': CareGuidanceArgs,
    'lookup_department': DepartmentArgs,
    'lookup_drug_label': DrugLabelArgs,
    'lookup_drug_interaction': DrugInteractionArgs,
    'lookup_hospital_guide': HospitalGuideArgs,
}
DESCRIPTIONS = {
    'search_care_guidance': '检索有来源的一般就医信息；指定医院时可附该院科室介绍。结果不能排除急症。',
    'lookup_department': '查询指定医院和院区的科室服务范围，不做临床分诊决定。',
    'lookup_drug_label': '按确切药名、剂型、规格查询官方说明书范本，返回引用，不决定个人剂量。',
    'lookup_drug_interaction': '查询确切产品的已收录说明书相互作用条目；未命中表示无法核实。',
    'lookup_hospital_guide': '查询指定医院和院区的挂号流程资料，不查实时号源、不提交预约。',
}
HOSPITAL_SECTION_CATEGORIES = {
    'channels': {'appointment', 'contact'},
    'process': {'appointment', 'visit_preparation'},
    'materials': {'visit_preparation', 'appointment'},
    'cancellation': {'appointment'},
    'followup': {'report', 'contact'},
    'all': None,
}
HOSPITAL_SECTION_QUERIES = {
    'channels': '如何挂号预约以及联系医院',
    'process': '挂号和就诊流程',
    'materials': '就诊需要携带什么材料',
    'cancellation': '如何取消预约或退号',
    'followup': '复诊和报告查询联系方式',
    'all': '医院就诊信息',
}
HOSPITAL_QUERY_CATEGORY_TERMS = {
    'parking': ('停车', '车位', '停车场'),
    'accessibility': ('无障碍', '轮椅', '行动不便'),
    'campus': ('院区', '地址', '在哪里'),
    'appointment': ('挂号', '预约', '退号'),
    'visit_preparation': ('材料', '证件', '携带', '带什么'),
    'contact': ('电话', '联系', '咨询'),
    'report': ('报告', '检查结果'),
}


def normalize_specification(value: str | None) -> str | None:
    """Normalize only unit spelling and exact numeric equivalents, not products."""
    if value is None:
        return None
    value = value.casefold().replace('毫升', 'ml').replace('毫克', 'mg').replace('克', 'g')
    value = re.sub(r'\s+', '', value).replace('：', ':')
    match = re.fullmatch(r'(\d+(?:\.\d+)?)(mg|g)', value)
    if match:
        from decimal import Decimal
        amount = Decimal(match[1]) / (1000 if match[2] == 'mg' else 1)
        return f'{amount.normalize():f}g'
    return value


def result(status: str, message: str, *, evidence: list[dict] | None = None, **fields):
    return {'status': status, 'message': message, 'evidence': evidence or [],
            'side_effects': [], 'personalized_medical_advice': False, **fields}


class MedicalToolRegistry:
    def __init__(self, knowledge: MedicalKnowledgeBase, *, hospital_knowledge=None):
        self.knowledge = knowledge
        self.hospital_knowledge = hospital_knowledge
        self.corpus: Corpus = knowledge.corpus

    def definitions(self, agent: AgentType) -> list[dict]:
        definitions = []
        for name, model in ARGUMENT_TYPES.items():
            try:
                require_tool(agent, name)
            except PermissionError:
                continue
            definitions.append({'type': 'function', 'function': {
                'name': name, 'description': DESCRIPTIONS[name],
                'parameters': model.model_json_schema()}})
        return definitions

    def query(self, agent: AgentType, tool: str, arguments: dict, *, today: date | None = None) -> dict:
        # Scope enforcement precedes argument validation and handler execution.
        require_tool(agent, tool)
        if tool not in ARGUMENT_TYPES:
            raise PermissionError('Unregistered medical tool')
        args = ARGUMENT_TYPES[tool].model_validate(arguments)
        today = today or datetime.now(timezone.utc).date()
        response = getattr(self, tool)(args, today)
        return {**response, 'agent': agent.value, 'tool': tool,
                'corpus_fingerprint': self.corpus.fingerprint}

    def _read(self, records: list[Record], today: date) -> dict:
        if not records:
            return result('not_covered', '当前已核验资料未覆盖此问题，无法核实。')
        fresh = [record for record in records if self.corpus.fresh(record, today)]
        if not fresh:
            return result('source_unavailable', '资料已超过复核期限或尚未生效，无法提供内容；请核对官方来源。')
        return result('found', '以下为已核对来源的资料摘要，请按适用范围查看原文。',
                      evidence=[self.corpus.evidence(record) for record in fresh],
                      partial_source_coverage=len(fresh) < len(records))

    def _hospital_scope(self, records, hospital_id, campus):
        if not hospital_id:
            return [], result('needs_clarification', '请指定医院。', missing_fields=['hospital_id'])
        records = [r for r in records if r.hospital_id == hospital_id]
        if not records:
            return [], result('not_covered', '当前资料未覆盖该医院，无法核实。')
        if not campus:
            return [], result('needs_clarification', '请确认院区或选择资料标注的适用范围。',
                              missing_fields=['campus'], available_campuses=sorted({r.campus for r in records}))
        records = [r for r in records if r.campus == campus]
        if not records:
            return [], result('not_covered', '当前资料未覆盖该院区，不能套用其他院区规定。')
        return records, None

    def _product_scope(self, args):
        records = [r for r in self.corpus.records if r.drug_name == args.drug_name]
        if not records:
            return [], result('not_covered', '当前资料未覆盖这个确切药品名称，不能用相似药名替代。')
        missing = [key for key in ['dosage_form', 'specification'] if getattr(args, key) is None]
        if missing:
            return [], result('needs_clarification', '请补充剂型和规格，以免混用说明书。',
                              missing_fields=missing,
                              available_products=[{'drug_name': name, 'dosage_form': form, 'specification': spec}
                                                  for name, form, spec in sorted({
                                                      (r.drug_name,r.dosage_form,r.specification) for r in records})])
        specification = normalize_specification(args.specification)
        records = [r for r in records if r.dosage_form == args.dosage_form
                   and normalize_specification(r.specification) == specification]
        if not records:
            return [], result('not_covered', '当前资料未覆盖该药品的剂型和规格，不能套用其他产品信息。')
        return records, None

    def search_care_guidance(self, args: CareGuidanceArgs, today: date) -> dict:
        records = [r for r in self.corpus.records if r.kind == DocumentKind.CARE_GUIDANCE]
        if args.campus and not args.hospital_id:
            return result('needs_clarification', '指定院区时也需要指定医院。', missing_fields=['hospital_id'])
        if args.hospital_id:
            departments, problem = self._hospital_scope(
                [r for r in self.corpus.records if r.kind == DocumentKind.DEPARTMENT],
                args.hospital_id, args.campus)
            if problem:
                return problem
            records += departments
        return self._search(args.query, records, today, args.top_k)

    def _search(self, query, records, today, top_k):
        if records and not any(self.corpus.fresh(r, today) for r in records):
            return self._read(records, today)
        search = self.knowledge.search_with_report(
            query, records, today=today, top_k=top_k,
            risk='general_guidance')
        hits = list(search.evidence)
        retrieval = asdict(search.report)
        if not hits:
            return result('not_covered', '未检索到适用资料，无法核实；不代表症状安全或没有风险。',
                          retrieval=retrieval, retrieval_mode=search.report.mode)
        return result('found', '检索结果是相关资料片段，不保证覆盖问题全部信息，也不是个人诊疗结论。',
                      evidence=hits, retrieval=retrieval,
                      retrieval_mode=search.report.mode)

    def lookup_department(self, args: DepartmentArgs, today: date) -> dict:
        records, problem = self._hospital_scope(
            [r for r in self.corpus.records if r.kind == DocumentKind.DEPARTMENT],
            args.hospital_id, args.campus)
        if problem:
            return problem
        if not args.department:
            return result('needs_clarification', '请指定需要查询的科室。', missing_fields=['department'],
                          available_departments=sorted({r.department for r in records}))
        return self._read([r for r in records if r.department == args.department], today)

    def lookup_drug_label(self, args: DrugLabelArgs, today: date) -> dict:
        records, problem = self._product_scope(args)
        if problem:
            return problem
        return self._read([r for r in records if r.kind == DocumentKind.DRUG_LABEL
                           and (args.section == 'all' or r.section == args.section)], today)

    def lookup_drug_interaction(self, args: DrugInteractionArgs, today: date) -> dict:
        records, problem = self._product_scope(args)
        if problem:
            return problem
        records = [r for r in records if r.kind == DocumentKind.DRUG_INTERACTION
                   and r.other_drug == args.other_drug]
        response = self._read(records, today)
        if response['status'] == 'not_covered':
            response['message'] = '当前说明书条目未覆盖此组合，无法核实；不表示没有相互作用或可以合用。请咨询医师或药师。'
        return {**response, 'coverage': 'limited_label_entries', 'safe_to_combine': None}

    def lookup_hospital_guide(self, args: HospitalGuideArgs, today: date) -> dict:
        if self.hospital_knowledge is not None:
            return self._lookup_hospital_v2(args)
        records, problem = self._hospital_scope(
            [r for r in self.corpus.records if r.kind == DocumentKind.HOSPITAL_GUIDE],
            args.hospital_id, args.campus)
        if problem:
            return problem
        records = [r for r in records if args.section == 'all' or r.section == args.section]
        if args.query:
            return self._search(args.query, records, today, args.top_k)
        return self._read(records, today)

    def _lookup_hospital_v2(self, args: HospitalGuideArgs) -> dict:
        version = self.hospital_knowledge.corpus.manifest.version
        common = {'hospital_corpus_version': version}
        if not args.hospital_id:
            return result(
                'needs_clarification', '请指定医院。',
                missing_fields=['hospital_id'], **common)
        hospital = self.hospital_knowledge.resolve_hospital(args.hospital_id)
        if hospital is None:
            return result(
                'not_covered', '当前官方医院资料未覆盖该医院，无法核实。',
                **common)
        campus = self.hospital_knowledge.resolve_campus(hospital, args.campus)
        if campus is None:
            if args.campus:
                return result(
                    'not_covered', '当前官方医院资料未覆盖该院区，不能套用其他院区规定。',
                    **common)
            return result(
                'needs_clarification', '请确认院区或选择资料标注的适用范围。',
                missing_fields=['campus'],
                available_campuses=[{
                    'campus_id': item.campus_id,
                    'campus': item.official_name,
                } for item in hospital.campuses],
                **common,
            )
        query = args.query or HOSPITAL_SECTION_QUERIES[args.section]
        inferred_categories = {
            category for category, terms in HOSPITAL_QUERY_CATEGORY_TERMS.items()
            if any(term in query for term in terms)
        }
        section_categories = HOSPITAL_SECTION_CATEGORIES[args.section]
        # Colloquial registration questions can split 挂号 around a hospital or doctor.
        if re.search(r'挂[^，。！？,!?\n]{1,48}?(?:的号|专家号|普通号|门诊号)', query):
            inferred_categories.add('appointment')
        categories = (
            inferred_categories if section_categories is None
            else section_categories | inferred_categories
        ) or None
        search = self.hospital_knowledge.search_with_report(
            query,
            hospital.hospital_id,
            campus=campus.campus_id,
            category=categories,
            top_k=min(args.top_k, 5),
        )
        retrieval = asdict(search.report)
        evidence = list(search.evidence)
        returned_categories = {
            item['scope']['category'] for item in evidence
        }
        missing_categories = sorted((categories or set()) - returned_categories)
        if not evidence:
            return result(
                'not_covered', '当前官方医院资料未覆盖此问题，无法核实。',
                retrieval=retrieval,
                retrieval_mode=search.report.mode,
                requested_categories=sorted(categories or ()),
                missing_categories=missing_categories,
                **common,
            )
        return result(
            'found', '以下为已核验的医院官方资料，请按适用院区查看原文。',
            evidence=evidence,
            retrieval=retrieval,
            retrieval_mode=search.report.mode,
            requested_categories=sorted(categories or ()),
            missing_categories=missing_categories,
            **common,
        )
