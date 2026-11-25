import asyncio
import json
import os

from fastapi.testclient import TestClient
import pytest

from careguide import runtime as factory
from careguide.api import app
from careguide.corpus import DEFAULT_CORPUS
from careguide.orchestration.contracts import TaskStatus
from careguide.orchestration.decisions import RequestDecision, TaskAnswer
from careguide.orchestration.planner import PlanningFailure
from careguide.orchestration.store import VersionConflict
from careguide.orchestration.unified import UnifiedRuntime
from careguide.orchestration.understanding_planner import UnderstandingPlanner
from scripts.reproduce_orchestration import build
from tests.test_chat_runtime import ScriptedClient, tool_reply
from tests.test_model_planner import reply


def unified(tmp_path):
    old, _ = build(tmp_path, [])
    return UnifiedRuntime(old.engine, old.store.path)


def test_hospital_guide_query_routes_deterministically_without_model(tmp_path):
    """语料已收录的医院地址/资料查询不经过模型规划，直接确定性执行。"""
    instance = unified(tmp_path)
    async def run():
        result = await instance.process('北京大学第一医院的地址是什么', 'u', 'c')
        assert result['budget']['used']['model'] == 0
        assert len(result['workflow']['tasks']) == 1
        tasks = result['workflow']['tasks']
        assert tasks[0]['action'] == 'lookup_hospital_guide'
        assert 'lookup_hospital_guide' in result['tools_used']
        assert result['errors'] == []
    asyncio.run(run())


def test_navigation_request_returns_out_of_scope_without_model(tmp_path):
    """导航类请求确定性返回 out_of_scope，不走规划器、不调模型。"""
    instance = unified(tmp_path)
    async def run():
        result = await instance.process('导航去泰州医院', 'u', 'c')
        assert result['workflow_status'] == 'out_of_scope'
        assert '当前支持症状咨询' in result['response']
        assert result['errors'] == []
    asyncio.run(run())


def test_symptom_followup_converges_to_guidance_without_model(tmp_path):
    """症状问诊澄清后用户补充症状 → 确定性就医引导，不再调用模型。"""
    instance = unified(tmp_path)
    clarify = {'kind': 'clarify',
               'question': '请补充你的主要症状和持续时间，例如：流鼻涕是清鼻涕还是黄脓鼻涕？'
                           '是否伴有发热、咳嗽、打喷嚏、鼻塞、头痛？症状持续多久了？'}
    client = ScriptedClient([reply(clarify)])
    instance.engine.client = client
    instance.planner = UnderstandingPlanner(client)
    async def run():
        first = await instance.process('流鼻涕是什么原因 怎么处理', 'u', 'c')
        assert first['workflow_status'] == 'waiting_user'
        calls_before = len(client.calls)
        second = await instance.process('黄色的 又热', 'u', 'c')
        assert second['workflow_status'] == 'completed'
        assert len(client.calls) == calls_before
        assert '不替代诊断' in second['response']
        assert '就诊' in second['response']
        assert second['errors'] == []
    asyncio.run(run())


def test_symptom_followup_ignores_new_questions(tmp_path):
    """补充消息里带新问句（怎么处理）时不收敛，走原规划流程。"""
    instance = unified(tmp_path)
    clarify = {'kind': 'clarify', 'question': '请补充你的主要症状和持续时间。'}
    client = ScriptedClient([reply(clarify), reply(clarify)])
    instance.engine.client = client
    instance.planner = UnderstandingPlanner(client)
    async def run():
        await instance.process('流鼻涕是什么原因', 'u', 'c')
        calls_before = len(client.calls)
        second = await instance.process('流鼻涕怎么处理', 'u', 'c')
        assert second['workflow_status'] == 'waiting_user'
        assert len(client.calls) > calls_before
    asyncio.run(run())


def test_symptom_followup_requires_symptom_clarify_topic(tmp_path):
    """上一轮澄清的不是症状（如缺药品名）时，症状词回复不收敛。"""
    instance = unified(tmp_path)
    clarify = {'kind': 'clarify', 'question': '请提供需要查询的药品名称。'}
    client = ScriptedClient([reply(clarify), reply(clarify)])
    instance.engine.client = client
    instance.planner = UnderstandingPlanner(client)
    async def run():
        await instance.process('鼻子不舒服', 'u', 'c')
        calls_before = len(client.calls)
        second = await instance.process('黄色的 又热', 'u', 'c')
        assert second['workflow_status'] == 'waiting_user'
        assert len(client.calls) > calls_before
    asyncio.run(run())


def test_symptom_followup_served_once(tmp_path):
    """收敛过一次后再次补充只给简短确认（防重复模板、防问诊循环）。"""
    instance = unified(tmp_path)
    clarify = {'kind': 'clarify', 'question': '请补充你的主要症状。'}
    client = ScriptedClient([reply(clarify)])
    instance.engine.client = client
    instance.planner = UnderstandingPlanner(client)
    async def run():
        await instance.process('流鼻涕什么原因', 'u', 'c')
        second = await instance.process('黄色的 又热', 'u', 'c')
        assert '不替代诊断' in second['response']
        calls_after_second = len(client.calls)
        third = await instance.process('是的', 'u', 'c')
        assert third['workflow_status'] == 'completed'
        assert '收到' in third['response']
        assert '不替代诊断' not in third['response']
        assert len(client.calls) == calls_after_second
        # 具体症状补充放行给模型（不再重复模板）
        fourth = await instance.process('还有点咳嗽', 'u', 'c')
        assert len(client.calls) > calls_after_second
    asyncio.run(run())


def test_symptom_followup_after_full_answer_uses_history_context(tmp_path):
    """一轮完整症状回答（completed）后用户继续补充症状，同样收敛。"""
    from careguide.orchestration.store import utc_now
    instance = unified(tmp_path)
    async def run():
        st = instance.store.create_pending(
            'u', 'c', '流鼻涕是什么原因 怎么处理',
            source_at=utc_now().isoformat(), source_message_id='m1')
        instance.ledger(st, create=True)
        def close(value):
            value.status = 'completed'
            value.events.append({'event': 'direct_answer'})
        instance.store.mutate(st.id, 'u', 'c', close,
                              plan_version=st.version, run_id=st.active_run)
        history = [{'role': 'user', 'content': '流鼻涕是什么原因 怎么处理'},
                   {'role': 'assistant', 'content': '流鼻涕常见原因包括：感冒、过敏性鼻炎……'}]
        second = await instance.process('黄色的 又热', 'u', 'c', history=history)
        assert '不替代诊断' in second['response']
    asyncio.run(run())


def test_symptom_confirmation_word_converges(tmp_path):
    """确认词（是的）回应症状澄清时也收敛，不再追问。"""
    instance = unified(tmp_path)
    clarify = {'kind': 'clarify',
               'question': '请确认：鼻涕是黄色脓涕，是否伴有发热？症状持续多久了？'}
    client = ScriptedClient([reply(clarify)])
    instance.engine.client = client
    instance.planner = UnderstandingPlanner(client)
    async def run():
        await instance.process('流鼻涕什么原因', 'u', 'c')
        calls_before = len(client.calls)
        second = await instance.process('是的', 'u', 'c')
        assert '不替代诊断' in second['response']
        assert len(client.calls) == calls_before
    asyncio.run(run())


def test_price_query_with_drug_context_uses_guidance_not_old_task(tmp_path):
    """药品价格查询不绑定旧说明书任务，给确定性价格指引。"""
    instance = unified(tmp_path)
    async def run():
        await instance.process('查询精氨酸布洛芬片说明书', 'u', 'c')
        await instance.process('0.2g', 'u', 'c')
        price = await instance.process('布洛芬价格', 'u', 'c')
        assert price['workflow_status'] == 'completed'
        assert '不提供实时价格' in price['response']
        assert '你之前查询的' in price['response']
        assert '说明书' in price['response']
    asyncio.run(run())


def test_price_query_without_context_still_answers_deterministically(tmp_path):
    """新会话里药品价格查询同样给确定性指引，不调模型。"""
    instance = unified(tmp_path)
    async def run():
        result = await instance.process('布洛芬多少钱', 'u', 'c')
        assert result['workflow_status'] == 'completed'
        assert '不提供实时价格' in result['response']
    asyncio.run(run())


def test_price_query_for_non_medicine_is_not_intercepted(tmp_path):
    """非药品价格（挂号多少钱）不拦截，走原流程。"""
    instance = unified(tmp_path)
    async def run():
        result = await instance.process('挂号多少钱', 'u', 'c')
        assert '不提供实时价格' not in result['response']
    asyncio.run(run())


def test_planner_normalizes_clarify_intent_shaped_as_new_with_empty_tasks(tmp_path):
    """模型用 new+空任务+question 表达澄清意图时，归一化为合法 clarify。"""
    from careguide.orchestration.understanding_planner import UnderstandingPlanner, build_context
    odd = {'kind': 'new', 'plan': {'goal': '泰州市人民医院怎么挂号，需要带什么？', 'tasks': []},
           'question': '请补充需要查询的院区，以便查询对应院区的挂号流程和就诊材料。'}
    client = ScriptedClient([reply(odd)])
    planner = UnderstandingPlanner(client)
    context = build_context('泰州市人民医院怎么挂号，需要带什么？', 'm1', '2026-11-18T10:00:00+00:00')
    decision, _ = asyncio.run(planner._decide(context))
    assert decision.kind == 'clarify'
    assert '院区' in decision.question


def test_planner_repair_feedback_explains_empty_task_plan(tmp_path):
    """空任务计划的重试反馈必须告诉模型 plan.tasks 不能为空。"""
    from careguide.orchestration.understanding_planner import UnderstandingPlanner, build_context
    invalid = {'kind': 'new', 'plan': {'goal': '泰州市人民医院的地址是什么', 'tasks': []}}
    client = ScriptedClient([reply(invalid), reply(invalid)])
    planner = UnderstandingPlanner(client)
    context = build_context('泰州市人民医院的地址是什么', 'm1', '2026-11-18T10:00:00+00:00')
    with pytest.raises(PlanningFailure) as excinfo:
        asyncio.run(planner._decide(context))
    assert excinfo.value.failure['code'] == 'model_format_error'
    assert len(client.calls) == 2
    second = json.loads(client.calls[1]['messages'][-1]['content'])
    assert second['validation_feedback']['fields'][0]['loc'] == ['plan', 'tasks']
    assert '至少一个任务' in second['validation_feedback']['hint']


def test_local_composition_is_durable_and_brief_isolated(tmp_path):
    instance = unified(tmp_path)
    observed = []
    original = instance.engine._run_agent
    async def capture(agent, message, history, context):
        observed.append((message, history, context))
        return await original(agent, message, history, context)
    instance.engine._run_agent = capture
    async def run():
        result = await instance.process('查消化内科医生，同时查询精氨酸布洛芬片0.2g说明书', 'u', 'c')
        assert result['workflow_status'] == 'completed'
        assert result['budget']['used']['model'] == 0
        assert result['budget']['used']['tool'] == 2
        assert len(result['workflow']['tasks']) == 2
        assert result['synthesis']['status'] == 'not_needed'
        assert len(observed) == 1
        message, history, context = observed[0]
        brief = json.loads(message)
        assert '消化内科' not in message
        assert history == []
        assert brief['known_facts']['arguments']['drug_name'] == '精氨酸布洛芬片'
        assert context.task_type == 'medication_query'
        assert context.task_brief['context_policy'] == 'brief'
        assert all(task['worker_result'] for task in result['workflow']['tasks'])
        trace = instance.traces.get(result['request_id'])
        assert trace['task_understanding']['intent_signals']['plan_source'] == 'compose'
        sources = {s['id'] for s in instance.store.get(result['workflow']['id'], 'u', 'c').source_messages}
        goals = trace['task_understanding']['intent_signals']['local_understanding']['goals']
        assert all(source in sources for goal in goals for source in goal['sources'].values())
    asyncio.run(run())


def test_template_selection_confirmation_and_resume_do_not_need_planner(tmp_path):
    instance = unified(tmp_path)
    async def run():
        first = await instance.process('查消化内科医生的号源并预约', 'u', 'c')
        assert first['workflow_status'] == 'waiting_user'
        identity = first['workflow']['id']
        assert first['budget']['used']['model'] == 0
        def event(task, field, value):
            return dict(kind='answer', workflow_id=identity, plan_version=1,
                        task_id=task, task_revision=1, values={field: value})
        await instance.process('PRA-GASTRO-01', 'u', 'c', workflow_event=event('slots', 'practitioner_id', 'PRA-GASTRO-01'))
        prepared = await instance.process('SLOT-GASTRO-001', 'u', 'c', workflow_event=event('booking', 'slot_id', 'SLOT-GASTRO-001'))
        assert prepared['workflow_status'] == 'pending_confirmation'
        assert prepared['budget']['used']['model'] == 0
        action = prepared['action_requests'][0]
        confirmed = await instance.confirm_action(action['id'], 'u', 'c')
        assert confirmed['status'] == 'completed'
        assert (await instance.confirm_action(action['id'], 'u', 'c'))['status'] == 'completed'
        resumed = await instance.process('继续', 'u', 'c', workflow_event=dict(
            kind='run', workflow_id=identity, plan_version=1))
        assert resumed['workflow_status'] == 'completed'
        assert len(instance.appointment_service.backend.appointments) == 1
    asyncio.run(run())


def test_missing_spec_new_query_then_reopen_and_old_answer(tmp_path):
    instance = unified(tmp_path)
    async def run():
        first = await instance.process('查询精氨酸布洛芬片说明书', 'u', 'c')
        assert first['workflow_status'] == 'waiting_user'
        identity = first['workflow']['id']
        before = instance.store.get(identity, 'u', 'c').model_dump(mode='json')
        new = await instance.process('查心血管内科医生', 'u', 'c')
        assert new['workflow_status'] == 'completed'
        assert new['workflow']['id'] != identity
        assert instance.store.get(identity, 'u', 'c').model_dump(mode='json') == before
        reopened = UnifiedRuntime(instance.engine, instance.store.path)
        done = await reopened.process('0.2g', 'u', 'c', workflow_event=dict(kind='answer',
            workflow_id=identity, plan_version=1, task_id='goal1', task_revision=1,
            values={'specification': '0.2g'}, event_id='restore'))
        assert done['workflow_status'] == 'completed'
        assert done['budget']['used']['tool'] == 1
    asyncio.run(run())


def test_input_and_change_package_rolls_back_completely_on_invalid_addition(tmp_path):
    instance = unified(tmp_path)
    first = asyncio.run(instance.process('查询精氨酸布洛芬片说明书', 'u', 'c'))
    identity = first['workflow']['id']
    state = instance.store.get(identity, 'u', 'c')
    before = state.model_dump(mode='json')
    decision = RequestDecision.model_validate(dict(kind='change', workflow_id=identity, base_version=1,
        answers=[dict(task_id='goal1', task_revision=1, values={'specification': '0.2g'})],
        change=dict(base_version=1, reason='test', add=[dict(id='bad', fragment='查医生',
            action='unavailable_action', agent='appointment', completion='items')])))
    with pytest.raises(PermissionError):
        instance.store.apply_decision(identity, 'u', 'c', decision, user_text='0.2g，另外查医生')
    assert instance.store.get(identity, 'u', 'c').model_dump(mode='json') == before


def test_dynamic_mixed_answer_and_addition_with_real_worker_and_safe_synthesis_fallback(tmp_path):
    instance = unified(tmp_path)
    async def run():
        first = await instance.process('查询精氨酸布洛芬片说明书', 'u', 'c')
        identity = first['workflow']['id']
        decision = dict(kind='change', workflow_id=identity, base_version=1,
            answers=[dict(task_id='goal1', task_revision=1, values={'specification': '0.2g'})],
            change=dict(base_version=1, reason='用户补规格并新增查询', add=[dict(id='doctor',
                fragment='查心血管内科医生', action='search_doctors', agent='appointment',
                arguments={'department': '心血管内科'}, completion='items')]))
        args = {'drug_name': '精氨酸布洛芬片', 'dosage_form': '片剂', 'specification': '0.2g', 'section': 'all'}
        client = ScriptedClient([reply(decision), tool_reply('lookup_drug_label', args),
                                 {'content': 'done'}, reply({'sections': [dict(
                                     task_id='goal1', evidence_id='invented', excerpt='预约成功')]})])
        instance.engine.client = client
        instance.planner = UnderstandingPlanner(client)
        done = await instance.process('规格0.2g，另外查心血管内科医生', 'u', 'c')
        assert done['workflow_status'] == 'completed'
        assert done['workflow']['version'] == 2
        assert len(done['workflow']['tasks']) == 2
        assert done['budget']['used']['replan'] == 1
        assert done['synthesis']['status'] == 'deterministic_fallback'
        assert done['evidence']
        assert '预约成功' not in done['response']
        assert len(client.calls) == 4
        planning_input = json.loads(client.calls[0]['messages'][-1]['content'])
        assert planning_input['existing_workflow']['tasks']['goal1']['missing_fields'] == ['specification']
        assert planning_input['workers']['medication']['mission']
        trace = instance.traces.get(done['request_id'])
        assert trace['final']['final_response'] == done['response']
        assert trace['execution']['budget'] == done['budget']
    asyncio.run(run())


def test_default_factory_wires_existing_classifier_and_does_not_call_legacy_recognizer(monkeypatch, tmp_path):
    for key in tuple(os.environ):
        if key.startswith('CAREGUIDE_'):
            monkeypatch.delenv(key)
    monkeypatch.setenv('CAREGUIDE_BUSINESS_DB', str(tmp_path/'factory.sqlite3'))
    monkeypatch.setenv('CAREGUIDE_CORPUS_DIR', str(DEFAULT_CORPUS))
    monkeypatch.setenv('CAREGUIDE_HOSPITAL_BACKEND', 'memory')
    from careguide.orchestration.understanding import LocalClassifier
    calls = []
    def predict(self, text):
        calls.append((self.path, text))
        return {'label': '其他', 'scores': {'其他': 1}, 'margin': 1, 'truncated': False}
    monkeypatch.setattr(LocalClassifier, 'predict', predict)
    factory.get_chat_runtime.cache_clear()
    factory.get_registry.cache_clear()
    try:
        instance = factory.get_chat_runtime()[0]
        assert isinstance(instance, UnifiedRuntime)
        async def forbidden(*args, **kwargs):
            raise AssertionError('Legacy recognition must not run')
        instance.engine.recognizer.recognize = forbidden
        with TestClient(app) as http:
            result = http.post('/chat', json={'message': '有没有其他建议', 'user_id': 'u', 'conv_id': 'c'})
            assert result.status_code == 200
            assert result.json()['workflow_status'] == 'waiting_user'
            assert len(calls) == 1
            assert 'careguide-intent-roberta-v1' in str(calls[0][0])
            trace = instance.traces.get(result.json()['request_id'])
            local = trace['task_understanding']['intent_signals']['local_understanding']
            assert local['candidates'][-1]['source'] == 'classifier'
            foreign = http.get('/workflows/'+result.json()['workflow']['id'], params={'user_id': 'other', 'conv_id': 'c'})
            assert foreign.status_code == 404
    finally:
        factory.get_chat_runtime.cache_clear()
        factory.get_registry.cache_clear()


def test_model_discovered_new_request_keeps_charged_budget_and_old_wait(tmp_path):
    instance = unified(tmp_path)
    async def run():
        old = await instance.process('查询精氨酸布洛芬片说明书', 'u', 'c')
        old_state = instance.store.get(old['workflow']['id'], 'u', 'c')
        before = old_state.model_dump(mode='json')
        text = '帮我找心内科的医生'
        decision = dict(kind='new', plan=dict(goal=text, tasks=[dict(id='doctor', fragment=text,
            action='search_doctors', agent='appointment', arguments={'department': '心血管内科'}, completion='items')]))
        client = ScriptedClient([reply(decision)])
        instance.engine.client = client
        instance.planner = UnderstandingPlanner(client)
        result = await instance.process(text, 'u', 'c')
        assert result['workflow_status'] == 'completed'
        assert result['workflow']['id'] != old_state.id
        # Planning and the attempted final-answer stream share the old ledger.
        assert result['budget']['used']['model'] == 2
        assert instance.ledger(old_state).summary()['used']['model'] == 2
        assert instance.store.get(old_state.id, 'u', 'c').model_dump(mode='json') == before
        assert len(client.calls) == 2
    asyncio.run(run())


def test_structured_cancel_works_after_model_exhaustion_without_understanding(tmp_path):
    instance = unified(tmp_path)
    async def run():
        first = await instance.process('查询精氨酸布洛芬片说明书', 'u', 'c')
        state = instance.store.get(first['workflow']['id'], 'u', 'c')
        ledger = instance.ledger(state)
        for _ in range(ledger.limits.model):
            async with ledger.call('model'):
                pass
        async def forbidden(*args, **kwargs):
            raise AssertionError('Structured event must bypass local understanding')
        instance.understanding.understand = forbidden
        result = await instance.process('取消', 'u', 'c', workflow_event=dict(kind='cancel',
            workflow_id=state.id, plan_version=1, task_id='goal1', task_revision=1))
        assert result['workflow']['tasks'][0]['status'] == 'cancelled'
        assert result['budget']['used']['model'] == ledger.limits.model
        assert result['budget']['used']['tool'] == 0
    asyncio.run(run())


def test_unified_emergency_preempts_wait_before_understanding_or_tools(tmp_path):
    instance = unified(tmp_path)
    async def run():
        first = await instance.process('查询精氨酸布洛芬片说明书', 'u', 'c')
        async def forbidden(*args, **kwargs):
            raise AssertionError('Emergency must preempt ordinary understanding')
        instance.understanding.understand = forbidden
        result = await instance.process('突然胸痛呼吸困难', 'u', 'c')
        assert result['workflow_status'] == 'urgent_handoff'
        assert result['tools_used'] == []
        state = instance.store.get(first['workflow']['id'], 'u', 'c')
        assert state.tasks['goal1'].status == TaskStatus.CANCELLED
        assert instance.ledger(state).summary()['used']['tool'] == 0
    asyncio.run(run())


def test_mode_fallback_and_direct_confirmation_preserve_aliased_budget(tmp_path):
    from careguide.orchestration.runtime import OrchestrationRuntime
    instance = unified(tmp_path)
    async def run():
        anchor = await instance.process('查询精氨酸布洛芬片说明书', 'u', 'c')
        first = await instance.process('查消化内科医生的号源并预约', 'u', 'c')
        identity = first['workflow']['id']
        state = instance.store.get(identity, 'u', 'c')
        state.budget_id = anchor['workflow']['id']
        instance.store.save(state, expected_revision=state.revision)
        for task, field, value in [('slots', 'practitioner_id', 'PRA-GASTRO-01'),
                                   ('booking', 'slot_id', 'SLOT-GASTRO-001')]:
            prepared = await instance.process(value, 'u', 'c', workflow_event=dict(kind='answer',
                workflow_id=identity, plan_version=1, task_id=task, task_revision=1, values={field: value}))
        action = prepared['action_requests'][0]
        # Exercise AppointmentService's own fallback, outside UnifiedRuntime's
        # budget context, as in direct mode.
        confirmed = await instance.appointment_service.confirm(action['id'], 'u', 'c')
        assert confirmed['status'] == 'completed'
        legacy = OrchestrationRuntime(instance.engine, instance.store.path)
        assert legacy.ledger(state).summary()['used']['tool'] == 3
        assert instance.ledger(state).summary()['used']['tool'] == 3
        assert (await instance.appointment_service.confirm(action['id'], 'u', 'c'))['status'] == 'completed'
        assert len(instance.appointment_service.backend.appointments) == 1
    asyncio.run(run())


def test_planner_prompt_reuses_confirmed_campus_and_gives_department_guidance():
    """挂号请求必须沿用会话已确认院区；症状+挂号给科室方向引导而非直接指定。"""
    from careguide.orchestration.understanding_planner import SYSTEM
    assert '必须沿用该院区' in SYSTEM
    assert '不直接指定具体科室' in SYSTEM
    assert '分诊台' in SYSTEM


def test_registration_guidance_uses_symptom_history_and_confirmed_campus(tmp_path):
    """短挂号请求 + 历史症状与已选院区 → 确定性科室方向引导。"""
    instance = unified(tmp_path)
    history = [
        {'role': 'user', 'content': '我腿脚不舒服，想去医院看看，家人开车送我'},
        {'role': 'assistant', 'content': '就医通行提示…'},
        {'role': 'user', 'content': '选择院区：南京医科大学附属泰州人民医院'},
        {'role': 'assistant', 'content': '已确认院区“南京医科大学附属泰州人民医院”，找到 10 个停车地点。'},
    ]
    async def run():
        result = await instance.process('安排挂号', 'u', 'c', history=history)
        assert result['workflow_status'] == 'completed'
        assert '南京医科大学附属泰州人民医院' in result['response']
        assert '骨科' in result['response']
        assert '分诊台' in result['response']
        assert '挂号' in result['response']
    asyncio.run(run())


def test_registration_guidance_works_without_campus(tmp_path):
    """有症状历史但无院区时，仍给科室方向引导并请求医院信息。"""
    instance = unified(tmp_path)
    history = [{'role': 'user', 'content': '我腿脚不舒服'}]
    async def run():
        result = await instance.process('挂号', 'u', 'c', history=history)
        assert result['workflow_status'] == 'completed'
        assert '骨科' in result['response']
        assert '告诉我具体医院和院区' in result['response']
    asyncio.run(run())


def test_registration_guidance_ignores_requests_without_symptom_context(tmp_path):
    """无症状上下文的挂号请求不拦截，走原流程。"""
    instance = unified(tmp_path)
    async def run():
        result = await instance.process('安排挂号', 'u', 'c', history=[])
        assert '骨科' not in (result.get('response') or '')
    asyncio.run(run())
