"""Orchestrator entry: local understanding/composition, then one dynamic decision."""
from __future__ import annotations

import re
import asyncio
import time
import uuid
from datetime import timedelta

from careguide.domain import IntentCategory
from careguide.errors import ErrorCode, Failure
from careguide.intent_recognizer import IntentRecognizer, IntentResult
from careguide.qic_intent_recognizer import QICIntentRecognizer
from careguide.orchestration.budget import BudgetExhausted, use_budget
from careguide.orchestration.contracts import PlanChange, TaskStatus
from careguide.orchestration.decisions import RequestDecision, TaskAnswer
from careguide.orchestration.planner import PlanningFailure
from careguide.orchestration.runtime import OrchestrationRuntime, preference_snapshot
from careguide.orchestration.store import VersionConflict, WorkflowExpired, WorkflowState, utc_now
from careguide.orchestration.understanding import LocalUnderstanding, compose_plan
from careguide.orchestration.understanding_planner import UnderstandingPlanner, build_context
from careguide.streaming import emit
from careguide.medical_triage import (evaluate_headache_triage,
                                      is_headache_triage_request)


_SYMPTOM_CLARIFY_MARKERS = re.compile(
    r'症状|鼻涕|流涕|咳|发热|发烧|热|烧|疼|痛|鼻塞|痰|体温|多久|持续|清涕|黄涕|喷嚏')
_SYMPTOM_FOLLOWUP_MARKERS = re.compile(
    r'鼻涕|流涕|黄涕|清涕|浓涕|鼻塞|咳|痰|发热|发烧|热|烧|疼|痛|嗓子|喉咙|咽|喷嚏|'
    r'面部|胀|反复|持续|加重|缓解|黄|绿')
_SYMPTOM_FOLLOWUP_EXCLUDE = re.compile(
    r'查询|查一下|查查|怎么|什么原因|原因|怎么办|如何处理|怎么处理|推荐|什么药|吃什么|'
    r'挂号|预约|地址|电话|医生|医院|导航')

_SYMPTOM_GUIDANCE_FEVER = (
    '收到，我会用一般就医引导来回应（不替代诊断）。\n'
    '目前信息不足以判断具体原因。黄脓涕/浓涕伴发热时建议重点观察：\n'
    '· 发热持续超过3天仍不退，或体温达到39℃及以上；\n'
    '· 头痛或面部胀痛明显加重；\n'
    '· 症状超过10天仍无好转，或好转后再次加重。\n'
    '出现上述任一情况，建议及时就诊；就诊时请说明鼻涕性状变化与发热过程。\n'
    '以上为一般信息，具体请以就诊医院意见为准。')

_SYMPTOM_GUIDANCE_GENERIC = (
    '收到，我会用一般就医引导来回应（不替代诊断）。\n'
    '目前信息不足以判断具体原因。建议重点观察：\n'
    '· 症状超过10天仍无好转；\n'
    '· 出现高热（39℃及以上）、明显头痛或面部胀痛；\n'
    '· 症状反复发作、影响休息或正常生活。\n'
    '出现上述任一情况，建议及时就诊；就诊前可记录症状起始时间、分泌物性状和体温变化。\n'
    '以上为一般信息，具体请以就诊医院意见为准。')

_SYMPTOM_ACK = (
    '收到，已记录你的补充。请继续观察：出现持续高热（39℃及以上）、面部或头部胀痛加重、'
    '症状超过10天仍无好转时，建议及时就诊。以上为一般信息，具体请以就诊医院意见为准。'
)


class UnifiedRuntime(OrchestrationRuntime):
    def __init__(self, engine, path, *, understanding=None, planner=None, **kwargs):
        super().__init__(engine, path,
                         planner=planner or UnderstandingPlanner(engine.client, engine.model), **kwargs)
        self.understanding = understanding or LocalUnderstanding(engine.registry.corpus)
        from careguide.orchestration.artifacts import ResultArtifacts
        self.result_artifacts = ResultArtifacts(self.store.path)
        self.worker.brief_mode = True
        self.worker.artifacts = self.result_artifacts

    async def _answer_general(self, message, user_id, conv_id, history, started, *, budget=None):
        from careguide.orchestration.general_answering import generate_general_answer
        # Request-only state for trace compatibility. Never persist it as the
        # latest workflow: a general question must not hide a waiting booking.
        now = utc_now()
        state = WorkflowState(id='direct-'+uuid.uuid4().hex, actor=user_id, conversation=conv_id,
            goal=message, tasks={}, status='completed', created_at=now.isoformat(),
            expires_at=(now+timedelta(minutes=30)).isoformat(), events=[{'event': 'direct_answer'}])
        budget = budget or self.ledger(state, create=True)
        with use_budget(budget, deadline=started+budget.limits.request_seconds):
            answer, diagnostic = await generate_general_answer(self.engine.client, self.engine.model,
                                                               message, history, budget)
        failures = [diagnostic['failure']] if diagnostic.get('failure') else []
        result = self._reply(state, budget, message, started, answer=answer,
                             synthesis=diagnostic, failures=failures)
        result.update(workflow=None, knowledge_used=False, grounded=False,
                      answer_kind='general_information', answer_generation=diagnostic)
        if answer is not None:
            result['answer'] = {'body': answer, 'sources': []}
        return result

    def _save_context(self, state, context, metadata):
        def save(value):
            known = {source['id'] for source in value.source_messages}
            value.source_messages.extend(s for s in context['user_sources'] if s['id'] not in known)
            value.events.append({'event': 'request_understood', **metadata,
                                 'message_id': context['current_message']['id']})
        return self.store.mutate(state.id, state.actor, state.conversation, save,
                                 plan_version=state.version, run_id=state.active_run)

    def _last_clarify_question(self, state):
        for event in reversed(state.events):
            if event.get('event') == 'request_understood' and event.get('decision') == 'clarify':
                return event.get('clarify_question', '')
        return ''

    def _symptom_followup_guidance(self, state, message, user_id, conv_id, history, started):
        """症状对话中用户补充症状/确认时，确定性收敛为一般就医引导。

        覆盖两种入口：症状 clarify 后补充症状；以及一轮完整症状回答后继续
        补充（会话上下文谈症状）。确认词（是的/对/嗯）同样收敛。已收敛过的
        会话再次补充只给简短确认，不再重复模板，也不再进入问诊循环。
        不诊断、不处方，只给就医边界与观察要点；返回 None 表示不适用。
        """
        if not state or state.status == 'expired' or state.tasks:
            return None
        served = any(event.get('event') == 'symptom_guidance_served' for event in state.events)
        text = re.sub(r'[，,。？！?!、\s]', '', message)
        is_confirmation = bool(re.fullmatch(r'是的|对|对的|嗯|嗯嗯|没错|是', text))
        is_supplement = bool(not is_confirmation and len(text) <= 40
                             and not _SYMPTOM_FOLLOWUP_EXCLUDE.search(text)
                             and _SYMPTOM_FOLLOWUP_MARKERS.search(text))
        if not (is_confirmation or is_supplement):
            return None
        # 症状上下文：上轮是症状 clarify，或最近一条用户消息在谈症状。
        question = self._last_clarify_question(state)
        previous = next((turn.get('content', '') for turn in reversed(history or ())
                         if turn.get('role') == 'user'), '')
        if not (_SYMPTOM_CLARIFY_MARKERS.search(question)
                or _SYMPTOM_CLARIFY_MARKERS.search(previous)):
            return None
        budget = self.ledger(state)
        if served:
            # 已收敛过：确认词给简短确认（防模型问询循环）；
            # 具体症状补充放行给原流程（模型可直接给出更完整的回答）。
            if is_confirmation:
                return self._symptom_reply(state, budget, message, started, _SYMPTOM_ACK, served=True)
            return None
        guidance = (_SYMPTOM_GUIDANCE_FEVER
                    if re.search(r'热|烧', text) else _SYMPTOM_GUIDANCE_GENERIC)
        return self._symptom_reply(state, budget, message, started, guidance, served=False)

    def _symptom_reply(self, state, budget, message, started, text, *, served):
        """组装确定性症状引导回复；首次收敛打防重复标记，状态保持 waiting_user。"""
        if not served:
            def mark(value):
                value.events.append({'event': 'symptom_guidance_served', 'at': utc_now().isoformat()})
            state = self.store.mutate(state.id, state.actor, state.conversation, mark,
                                      plan_version=state.version, run_id=state.active_run)
        result = self._reply(state, budget, message, started, answer=text,
                             synthesis={'mode': 'deterministic_symptom_guidance'})
        result['workflow_status'] = 'completed'
        result.update(workflow=None, knowledge_used=False, grounded=False,
                      answer_kind='symptom_guidance',
                      answer_generation={'mode': 'deterministic'})
        return result

    async def _respond(self, state, budget, message, started, **kwargs):
        from careguide.orchestration.answering import generate_answer
        deadline = started+budget.limits.request_seconds
        with use_budget(budget, deadline=deadline):
            emit('stage', stage='answering', message='正在根据查询结果生成回答')
            answer, diagnostic = await generate_answer(self.engine.client, self.engine.model, state, budget, message)
        latest = self.store.get(state.id, state.actor, state.conversation)
        if (latest.version, latest.revision) != (state.version, state.revision):
            from careguide.streaming import emit_answer
            if answer is not None:
                await emit_answer('answer_reset', answer_id=diagnostic['answer_id'], reason='workflow_changed')
            answer, state = None, latest
            diagnostic = {**diagnostic, 'status': 'stale_result_fallback'}
            kwargs['failures'] = ()
        body = diagnostic.pop('body', None) if answer is not None else None
        if diagnostic.get('status') == 'deterministic_fallback' and diagnostic.get('failure'):
            failure = diagnostic['failure']
            kwargs['note'] = kwargs.get('note', '') + (
                '\n\n回答生成未完成：'+failure['message']+'\n'+failure['next_step']+
                '\n已完成的查询结果仍按系统记录展示。')
        result = self._reply(state, budget, message, started, answer=answer, synthesis=diagnostic, **kwargs)
        if body is not None:
            result['answer'] = {'body': body, 'sources': diagnostic['sources']}
        result['answer_generation'] = diagnostic
        return result

    async def process(self, message, user_id, conv_id, history=None, *, workflow_event=None):
        started, at, message_id = time.monotonic(), utc_now().isoformat(), uuid.uuid4().hex
        history = history or []
        state = (self.store.get(workflow_event['workflow_id'], user_id, conv_id)
                 if workflow_event else self.store.latest(user_id, conv_id))
        triage = (evaluate_headache_triage(message, history)
                  if is_headache_triage_request(message, history) else None)
        if triage is not None and not triage.run_rag:
            if triage.status == 'emergency' and state:
                self._preempt(state)
            return await self.engine.process(message, user_id, conv_id, history)
        urgent = IntentRecognizer._pattern(message).get('urgent')
        boundary = QICIntentRecognizer._boundary(message)
        if re.search(r'导航', message):
            # 本服务不提供实时导航/路况能力；不消耗模型规划次数。
            return await self.engine.process(message, user_id, conv_id, history, understood=IntentResult(
                None, 0, [], 'out_of_scope', boundary='out_of_scope'))
        if urgent or boundary == 'urgent_handoff':
            if state:
                self._preempt(state)
            return await self.engine.process(message, user_id, conv_id, history, understood=IntentResult(
                IntentCategory.EMERGENCY, .95, [IntentCategory.EMERGENCY], 'urgent_handoff',
                urgency='CRITICAL', boundary='urgent_handoff'))
        if boundary:
            return await self.engine.process(message, user_id, conv_id, history,
                understood=IntentResult(None, 0, [], boundary, boundary=boundary))
        if re.fullmatch(r'(?:请)?转人工(?:协助|咨询|客服)?|(?:查询)?CG-[A-F0-9]{12}', message.strip(), re.I):
            # Preserve the existing local consultation-record workflow. This
            # records a request; it does not contact a human or external system.
            return await self.engine.process(message, user_id, conv_id, history,
                understood=IntentResult(None, 0, [], 'needs_clarification'))
        if not workflow_event:
            from careguide.orchestration.general_answering import local_general_request
            if local_general_request(message, corpus=self.registry.corpus):
                return await self._answer_general(message, user_id, conv_id, history, started)
        if state and workflow_event and state.version != workflow_event['plan_version']:
            raise VersionConflict('Old plan version')
        if state:
            state = self.store.expire_waits(state.id, user_id, conv_id)
            if state.status == 'expired':
                if workflow_event:
                    raise WorkflowExpired('Workflow expired')
                state = None
        decision, events, metadata, understood = None, [], {}, None
        if workflow_event and workflow_event['kind'] != 'change':
            kind = workflow_event['kind']
            budget = self.ledger(state)
            if kind == 'answer':
                state = self.store.answer(state.id, user_id, conv_id, workflow_event['task_id'],
                    workflow_event['values'], plan_version=workflow_event['plan_version'],
                    task_revision=workflow_event['task_revision'], event_id=workflow_event.get('event_id'))
            elif kind == 'cancel':
                task = state.tasks.get(workflow_event['task_id'])
                if task is None or task.revision != workflow_event['task_revision']:
                    raise VersionConflict('Old task revision')
                state = self.store.apply_change(state.id, user_id, conv_id, PlanChange(
                    base_version=state.version, reason='用户明确取消', cancel=[task.spec.id]),
                    user_text='取消任务 '+task.spec.id)
                state = self.store.end_run(state.id, user_id, conv_id, None, status='cancelled' if all(
                    t.status == TaskStatus.CANCELLED for t in state.tasks.values()) else 'ready')
                return self._reply(state, budget, message, started)
            context = build_context(message, message_id, at, history, state)
            signals = {'request_context': context, 'decision_source': 'structured'}
            return await self._execute_workflow(state, budget, message, user_id, conv_id, history,
                                                 started, at, signals, 'execute')
        # Literal answers to a single persisted question are unambiguous. No
        # classifier or planning call is needed for these narrow forms.
        if state and not workflow_event:
            waiting = [t for t in state.tasks.values() if t.status == TaskStatus.WAITING_INPUT]
            if len(waiting) == 1 and len(waiting[0].missing_fields) == 1:
                task, value = waiting[0], message.strip()
                key = task.missing_fields[0]
                choices = [str(item.get(task.spec.inputs[key].item_field))
                           for option in task.options for item in option.get('values', [])
                           if key in task.spec.inputs]
                if value in choices or key == 'specification' and re.fullmatch(
                        r'[\d.]+\s*(?:mg|g|ml|μg|ug|毫克|克|毫升)(?::[\d.]+\s*(?:mg|g))?', value, re.I):
                    return await self.process(message, user_id, conv_id, history, workflow_event={
                        'kind': 'answer', 'workflow_id': state.id, 'plan_version': state.version,
                        'task_id': task.spec.id, 'task_revision': task.revision,
                        'values': {key: value}, 'event_id': message_id})
        if state and not workflow_event:
            guidance = self._symptom_followup_guidance(
                state, message, user_id, conv_id, history, started)
            if guidance is not None:
                return guidance
        understood = await self.understanding.understand(message, message_id=message_id, at=at,
                                                        history=history, classify=False)
        proposal, metadata = compose_plan(message, understood)
        if workflow_event:
            proposal = None  # explicit changes must target their existing plan
        if proposal and (state is None or message != state.goal):
            state = None  # independent new query; preserve old workflow/actions
        fresh = state is None
        if fresh:
            state = self.store.create_pending(user_id, conv_id, message, source_at=at,
                                              source_message_id=message_id)
            budget = self.ledger(state, create=True)
        else:
            budget = self.ledger(state)
        with use_budget(budget, deadline=started+budget.limits.request_seconds):
            try:
                async with asyncio.timeout(max(.001, started+budget.limits.request_seconds-time.monotonic())):
                    await self.understanding.enrich(message, understood, budget=budget, scope=(user_id, conv_id),
                                                   allow_remote=self.engine.client is not None)
            except TimeoutError:
                understood.diagnostics.append('understanding_request_deadline')
        metadata['local_understanding'] = understood.model_dump(mode='json')
        context = build_context(message, message_id, at, history, state, understood)
        if fresh and proposal:
            decision = RequestDecision(kind='new', plan=proposal)
        elif not workflow_event and message == state.goal and state.tasks:
            decision = RequestDecision(kind='run', workflow_id=state.id, base_version=state.version)
        else:
            if self.engine.client is None:
                decision = RequestDecision(kind='clarify', question=(
                    '请明确本次查询对象、条件及对应任务；当前未配置模型，无法可靠规划这个请求。'))
            else:
                try:
                    emit('stage', stage='planning', message='正在生成任务计划')
                    with use_budget(budget, deadline=started+budget.limits.request_seconds):
                        decision, events = await self.planner.decide(context, budget=budget)
                except PlanningFailure as exc:
                    return self._reply(state, budget, message, started, failures=[exc.failure],
                                       signals={'plan_source': 'llm', 'planning_events': exc.events})
                metadata = {**metadata, 'plan_source': 'llm', 'recognition_source': understood.recognition_source}
        if decision.kind == 'direct':
            from careguide.orchestration.general_answering import direct_allowed
            if not direct_allowed(message, corpus=self.registry.corpus):
                return self._reply(state, budget, message, started, failures=[Failure(
                    ErrorCode.INVALID_PARAMETERS, 'answer_route_validation').payload()])
            return await self._answer_general(message, user_id, conv_id, history, started, budget=budget)
        if decision.kind == 'boundary':
            if decision.boundary == 'urgent_handoff':
                self._preempt(state)
            return await self.engine.process(message, user_id, conv_id, history, understood=IntentResult(
                IntentCategory.EMERGENCY if decision.boundary == 'urgent_handoff' else None, 0, [],
                decision.boundary, boundary=decision.boundary))
        if decision.kind == 'clarify':
            state = self._save_context(state, context, {**metadata, 'decision': 'clarify',
                                                        'clarify_question': decision.question})
            if not state.tasks:
                state.status = 'waiting_user'
                state = self.store.save(state, expected_revision=state.revision)
            return self._reply(state, budget, message, started,
                               note='\n\n'+decision.question, signals=metadata)
        if decision.kind == 'new':
            if state.tasks:
                # A model decision about an active workflow is charged to its
                # existing ledger, even if it discovers an independent request.
                prior_budget = state.budget_id or state.id
                state = self.store.create_pending(user_id, conv_id, message, source_at=at,
                                                  source_message_id=message_id)
                state.budget_id = prior_budget
                state = self.store.save(state, expected_revision=state.revision)
            state = self._save_context(state, context, metadata)
            state.preference_snapshot = preference_snapshot(history)
            state = self.store.save(state, expected_revision=state.revision)
            state = self.store.initialize(state, decision.plan, events)
        elif decision.kind in {'answer', 'change', 'cancel'}:
            if decision.kind == 'change':
                try:
                    async with budget.call('replan'):
                        pass
                except BudgetExhausted:
                    return self._reply(state, budget, message, started, failures=[Failure(
                        ErrorCode.BUDGET_EXHAUSTED, 'user_replanning').payload()])
            if decision.change and decision.change.add and budget.summary()['closing']:
                return self._reply(state, budget, message, started, failures=[Failure(
                    ErrorCode.BUDGET_EXHAUSTED, 'closing_new_tasks').payload()])
            state = self.store.apply_decision(state.id, user_id, conv_id, decision, user_text=message,
                sources=context['user_sources'], source_at=at,
                authorized_entities=self.worker.entities._explicit_entities(message, []))
            if decision.kind == 'cancel':
                return self._reply(state, budget, message, started, signals=metadata)
        context = build_context(message, message_id, at, history, state, understood)
        signals = {**metadata, 'request_context': context}
        return await self._execute_workflow(state, budget, message, user_id, conv_id, history,
                                             started, at, signals, 'execute')
