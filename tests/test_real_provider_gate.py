from scripts.validate_real_provider import REQUIRED, check_result, preflight


def test_preflight_does_not_expose_secret_and_rejects_placeholder():
    environment = {name: 'some-value' for name in REQUIRED}
    environment.update(CAREGUIDE_LLM_API_KEY='YOUR_LLM_API_KEY',
                       CAREGUIDE_LLM_BASE_URL='https://provider.example/v1',
                       CAREGUIDE_EMBEDDING_BASE_URL='https://embedding.example/v1')
    result = preflight(environment)
    assert not result['ready']
    assert result['missing_variables'] == ['CAREGUIDE_LLM_API_KEY']
    assert 'some-value' not in str(result)


def test_http_success_and_correct_route_cannot_mask_llm_fallback():
    reply = {'intent': 'appointment', 'workflow_status': 'completed', 'evidence': [{}],
             'execution_mode': 'provider_tool_loop', 'medical_answering_enabled': False}
    trace = {'task_understanding': {'intent_result': {'source_scores': {
                'llm': {'failed': True}, 'embedding': {'failed': False}, 'embedding_mode': 'remote'}}},
             'execution': {'execution_waves': [{'agents': [{'error': ''}]}],
                           'tool_calls': [{'success': True}]}, 'side_effects': []}
    assert check_result(reply, trace, 'appointment', 'completed') == ['llm_branch_failed_or_missing']


def test_endpoint_cannot_contain_inline_credentials():
    environment = {name: 'configured' for name in REQUIRED}
    environment.update(CAREGUIDE_LLM_BASE_URL='https://user:secret@provider.example/v1',
                       CAREGUIDE_EMBEDDING_BASE_URL='https://embedding.example/v1')
    assert preflight(environment)['invalid_endpoint_variables'] == ['CAREGUIDE_LLM_BASE_URL']


def test_mimo_bounded_calls_restore_non_thinking_mode_without_changing_other_models():
    import asyncio
    from types import SimpleNamespace
    from careguide.llm_utils import create_chat_completion
    calls = []
    async def create(**kwargs):
        calls.append(kwargs)
        return {'choices': [{'message': {'content': '{}'}}]}
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    async def run():
        for model in ('mimo-v2.5', 'other-provider-model'):
            await create_chat_completion(client, model=model, messages=[{'role': 'user', 'content': 'JSON'}])
    asyncio.run(run())
    assert calls[0]['extra_body'] == {'thinking': {'type': 'disabled'}}
    assert 'extra_body' not in calls[1]


def test_deepseek_tool_calls_disable_thinking_for_named_tool_choice_compatibility():
    import asyncio
    from types import SimpleNamespace
    from careguide.llm_utils import create_chat_completion

    calls = []

    async def create(**kwargs):
        calls.append(kwargs)
        return {'choices': [{'message': {'tool_calls': []}}]}

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    tool = {
        'name': 'lookup_drug_label',
        'description': '查询药品说明书',
        'input_schema': {
            'type': 'object',
            'properties': {'drug_name': {'type': 'string'}},
            'required': ['drug_name'],
        },
    }

    asyncio.run(create_chat_completion(
        client,
        model='deepseek-flash',
        messages=[{'role': 'user', 'content': '查询药品说明书'}],
        tools=[tool],
        tool_choice={'type': 'function', 'function': {'name': 'lookup_drug_label'}},
    ))

    assert calls[0]['extra_body'] == {'thinking': {'type': 'disabled'}}


def test_deepseek_json_mode_disables_thinking_so_reasoning_cannot_truncate_schema():
    """Schema-bound DeepSeek replies (json_object) must not share the output
    budget with reasoning tokens; plain chat keeps thinking enabled."""
    import asyncio
    from types import SimpleNamespace
    from careguide.llm_utils import create_chat_completion

    calls = []

    async def create(**kwargs):
        calls.append(kwargs)
        return {'choices': [{'message': {'content': '{"kind": "direct"}'}}]}

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    async def run():
        await create_chat_completion(client, model='deepseek-v4-pro-0813-new',
            messages=[{'role': 'user', 'content': '规划任务'}],
            response_format={'type': 'json_object'})
        await create_chat_completion(client, model='deepseek-flash',
            messages=[{'role': 'user', 'content': '普通对话'}])

    asyncio.run(run())
    assert calls[0]['extra_body'] == {'thinking': {'type': 'disabled'}}
    assert 'extra_body' not in calls[1]
