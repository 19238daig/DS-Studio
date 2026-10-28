from __future__ import annotations

from typing import Any
from unittest.mock import Mock
import pytest
from pydantic import PrivateAttr
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from src.agents import TaskRuntime, build_agents, run_task
from src.config import Settings
from src.data import DataService
from src.memory import MemoryService
from src.schemas import TaskSpec
from src.storage import Store


class ScriptedModel(BaseChatModel):
    model_name: str = 'offline-model'
    responses: list[Any]
    _index: int = PrivateAttr(default=0)
    _seen: list = PrivateAttr(default_factory=list)

    @property
    def _llm_type(self):
        return 'offline-script'

    def _get_ls_params(self, **kwargs):
        return {'ls_provider': 'openai', 'ls_model_name': self.model_name}

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self._seen.append(messages)
        if self._index >= len(self.responses):
            raise AssertionError('Unexpected extra model call')
        message = self.responses[self._index]
        self._index += 1
        if callable(message):
            message = message(messages)
        return ChatResult(generations=[ChatGeneration(message=message)])


def call(name, args, ident='call1'):
    return AIMessage(content='', tool_calls=[{'name': name, 'args': args, 'id': ident}])


@pytest.fixture
def context(tmp_path):
    store = Store(Settings(workspace=tmp_path, api_key='offline-secret', _env_file=None))
    session = store.new_session()
    path = tmp_path / 'KB' / 'sales.csv'
    path.write_text('amount,category\n10,A\n20,A\n30,B\n', encoding='utf-8')
    file = store.register_file(path, path.name, None, source='kb')
    task = TaskSpec(session_id=session['session_id'], request='统计amount均值', file_ids=[file['file_id']])
    store.create_task(task)
    return store, task, file


def models(main, analyst=None, report=None):
    result = {'main': ScriptedModel(model_name='offline-main', responses=main),
              'analyst': ScriptedModel(model_name='offline-analyst', responses=analyst or [AIMessage(content='done')]),
              'report_designer': ScriptedModel(model_name='offline-report', responses=report or [AIMessage(content='done')])}
    return result, lambda settings, role: result[role]


def test_real_deepagents_persists_explicit_routes_and_all_role_usage(context):
    from src.telemetry import summarize_task
    store, task, _ = context
    task.request, task.file_ids = '介绍功能', []
    main = [call('task', {'subagent_type': 'analyst', 'description': 'Describe analysis capabilities.'}),
            call('task', {'subagent_type': 'report_designer', 'description': 'Describe report capabilities.'}, 'call2'),
            call('finish_task', {'intent': 'general', 'answer': '支持分析与报告。'}, 'call3'), AIMessage(content='done')]
    child_a, child_r = AIMessage(content='analysis'), AIMessage(content='report')
    for message in [*main, child_a, child_r]:
        message.usage_metadata = {'input_tokens': 10, 'output_tokens': 5, 'total_tokens': 15}
    _, factory = models(main, [child_a], [child_r])
    event = lambda kind, message='', **details: store.add_event(task.task_id, kind, message, **details)
    result = run_task(task, store, event, lambda: False, model_factory=factory)
    assert result['status'] == 'completed'
    stats = summarize_task(store.get_task(task.task_id), store.list_events(task.task_id))
    assert [route['target'] for route in stats['routes']] == ['analyst', 'report_designer']
    assert stats['total_tokens'] == 90 and stats['usage_status'] == 'complete'
    assert {item['role']: item['total_tokens'] for item in stats['by_agent']} == {'main': 60, 'analyst': 15, 'report_designer': 15}


def test_model_usage_returned_during_cancellation_is_still_persisted(context):
    from src.jobs import execute_task
    from src.telemetry import summarize_task
    store, task, _ = context
    store.update_task(task.task_id, status='running')
    def finish_after_cancel(messages):
        store.update_task(task.task_id, cancel_requested=True)
        return AIMessage(content='private model text', usage_metadata={'input_tokens': 7, 'output_tokens': 3, 'total_tokens': 10})
    _, factory = models([finish_after_cancel])
    def runner(spec, child_store, event, cancelled):
        return run_task(spec, child_store, event, cancelled, model_factory=factory)
    execute_task(task.task_id, store.settings, runner)
    events = store.list_events(task.task_id)
    assert store.get_task(task.task_id)['status'] == 'cancelled'
    assert summarize_task(store.get_task(task.task_id), events)['total_tokens'] == 10
    assert 'private model text' not in str(events)


def test_real_deepagents_delegate_and_compute_without_hardcoded_answers(context):
    store, task, file = context
    store.append_message(task.session_id, 'user', 'PARENT_CONTEXT_ONLY')
    scripted, factory = models([
        call('task', {'subagent_type': 'analyst', 'description': 'Compute amount mean.'}),
        call('finish_task', {'intent': 'analysis', 'answer': '分析已完成。'}), AIMessage(content='done')],
        [call('analyze_dataset', {'file_id': file['file_id'], 'operation': 'aggregate', 'aggregations': {'amount': 'mean'}}), AIMessage(content='Use registered result.')])
    events = Mock()
    result = run_task(task, store, events, lambda: False, model_factory=factory)
    assert result['status'] == 'completed'
    analysis = store.get_analysis(result['analysis_ids'][0], task.session_id)
    assert analysis.metrics['amount_mean'] == 20
    assert analysis.metrics['source_rows'] == 3
    assert '20' in result['final_response'] and '```json' not in result['final_response']
    assert not any('PARENT_CONTEXT_ONLY' in str(message.content) for batch in scripted['analyst']._seen for message in batch)
    runtime = TaskRuntime(task, store, Mock(), lambda: False)
    agents = build_agents(runtime, factory)
    assert 'task' in agents['main'].get_graph().nodes['tools'].data.tools_by_name
    for role in ('analyst', 'report_designer'):
        names = agents[role].get_graph().nodes['tools'].data.tools_by_name
        assert 'task' not in names and 'execute' not in names


def test_shared_budget_stops_main_and_specialist(context):
    store, task, file = context
    store.settings.max_tool_calls = 1
    _, factory = models([call('task', {'subagent_type': 'analyst', 'description': 'Compute.'})],
                        [call('profile_dataset', {'file_id': file['file_id']})])
    result = run_task(task, store, Mock(), lambda: False, model_factory=factory)
    assert result['status'] == 'failed'
    assert result['tool_calls'] == 1
    assert not result['analysis_ids']


def test_unknown_column_requests_clarification(context):
    store, task, file = context
    _, factory = models([call('task', {'subagent_type': 'analyst', 'description': 'Compute.'}), AIMessage(content='Need column.')],
        [call('analyze_dataset', {'file_id': file['file_id'], 'operation': 'aggregate', 'aggregations': {'missing': 'mean'}}),
         call('ask_user', {'question': '数据没有missing字段，请确认列名。'}), AIMessage(content='Need input.')])
    result = run_task(task, store, Mock(), lambda: False, model_factory=factory)
    assert result['status'] == 'waiting_user'
    assert '列名' in result['final_response']
    assert not result['analysis_ids']


def test_uncomputed_numeric_answer_cannot_finish_data_query(context):
    store, task, _ = context
    task.request = 'amount中位数是多少？'
    _, factory = models([call('finish_task', {'intent': 'general', 'answer': '中位数为999。'}), AIMessage(content='999')])
    result = run_task(task, store, Mock(), lambda: False, model_factory=factory)
    assert result['status'] == 'failed'
    assert '999' not in result['final_response']


def test_explicit_report_preference_without_analysis_persists(context):
    store, task, _ = context
    task.request = '以后报告用中文，先摘要后图表，请记住'
    task.file_ids = []
    _, factory = models([call('task', {'subagent_type': 'report_designer', 'description': task.request}),
                         call('finish_task', {'intent': 'preference', 'answer': '已记住报告偏好。'}), AIMessage(content='done')],
        report=[call('memory_propose', {'content': 'untrusted rewritten text', 'kind': 'preference'}), AIMessage(content='已提交偏好。')])
    result = run_task(task, store, Mock(), lambda: False, model_factory=factory)
    assert result['status'] == 'completed' and not result['analysis_ids']
    entries = MemoryService(store).search('report_designer', '中文')
    assert len(entries) == 1 and entries[0]['content'] == task.request
    assert not MemoryService(store).search('analyst', '中文')


def test_report_not_ready_never_fabricates_success(context, monkeypatch):
    import src.agents as agents_module
    original_import = agents_module.importlib.import_module
    def import_without_reporting(name, *args, **kwargs):
        if name == 'src.reporting':
            raise ModuleNotFoundError('reporting deliberately unavailable in test')
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(agents_module.importlib, 'import_module', import_without_reporting)
    store, task, file = context
    analysis = DataService(store).analyze(task, file['file_id'], 'aggregate', aggregations={'amount': 'mean'})
    task.analysis_ids = [analysis.analysis_id]
    task.request = '生成报告'
    task.expected_outputs = ['html']
    _, factory = models([call('task', {'subagent_type': 'report_designer', 'description': 'Generate report.'}), AIMessage(content='报告已生成。')],
        report=[call('render_report', {'analysis_ids': [analysis.analysis_id]}), AIMessage(content='Not ready.')])
    result = run_task(task, store, Mock(), lambda: False, model_factory=factory)
    assert result['status'] == 'failed'
    assert not result['report_ids'] and '尚未就绪' in result['final_response']


def test_cancel_boundary_and_stale_analysis_scope(context):
    store, task, file = context
    with pytest.raises(InterruptedError):
        run_task(task, store, Mock(), lambda: True)
    analysis = DataService(store).analyze(task, file['file_id'], 'aggregate', aggregations={'amount': 'mean'})
    store.update_task(task.task_id, status='succeeded', result={'analysis_ids': [analysis.analysis_id]})
    path = store.root / 'KB' / 'other.csv'
    path.write_text('x\n1\n', encoding='utf-8')
    other = store.register_file(path, path.name, None, source='kb')
    next_task = TaskSpec(session_id=task.session_id, request='新数据', file_ids=[other['file_id']])
    store.create_task(next_task)
    runtime = TaskRuntime(next_task, store, Mock(), lambda: False)
    assert analysis.analysis_id not in runtime.allowed_analysis_ids


@pytest.mark.parametrize('ending', [
    '不生成报告', '不需要生成报告', '不要帮我生成报告', '不生成HTML报告',
    '不要生成任何报告', '无需报告', '暂时不出报告', "don't generate a report",
    'do not create any report', 'without generating a report',
])
def test_negated_report_does_not_block_verified_analysis(context, ending):
    store, task, file = context
    task.request = '统计amount均值，' + ending
    _, factory = models([
        call('task', {'subagent_type': 'analyst', 'description': 'Compute amount mean.'}),
        call('finish_task', {'intent': 'analysis', 'answer': '已完成均值计算。'}), AIMessage(content='done')],
        [call('analyze_dataset', {'file_id': file['file_id'], 'operation': 'aggregate', 'aggregations': {'amount': 'mean'}}), AIMessage(content='done')])
    result = run_task(task, store, Mock(), lambda: False, model_factory=factory)
    assert result['status'] == 'completed', result
    assert len(result['analysis_ids']) == 1 and not result['report_ids']


@pytest.mark.parametrize('user_text', ['介绍一下如何生成报告', '生成报告需要什么数据',
                                    'How do you generate a report?', '以后生成报告用中文，请记住'])
def test_report_capability_or_preference_is_not_a_deliverable(context, user_text):
    store, task, _ = context
    task.request = user_text
    # A previously selected dataset does not turn a capability question into
    # a calculation job. Intent is checked independently of sidebar selection.
    intent = 'preference' if '记住' in user_text else 'general'
    main = [call('finish_task', {'intent': intent, 'answer': '支持基于已验证分析结果生成HTML和Excel报告。'}), AIMessage(content='done')]
    if intent == 'preference':
        main = [call('task', {'subagent_type':'report_designer','description':user_text}), *main]
    _, factory = models(main, report=[call('memory_propose', {'content':user_text,'kind':'preference'}), AIMessage(content='已保存。')])
    result = run_task(task, store, Mock(), lambda: False, model_factory=factory)
    assert result['status'] == 'completed', result
    assert not result['analysis_ids'] and not result['report_ids']


@pytest.mark.parametrize('user_text', ['生成报告', '请生成一份HTML报告', '统计数据并生成报告', 'Generate a report'])
def test_explicit_report_request_cannot_complete_without_artifact(context, user_text):
    store, task, _ = context
    task.request = user_text
    task.file_ids = []
    _, factory = models([call('finish_task', {'intent': 'general', 'answer': '报告已生成。'}), AIMessage(content='done')])
    result = run_task(task, store, Mock(), lambda: False, model_factory=factory)
    assert result['status'] == 'failed', result
    assert not result['report_ids']


def test_pure_html_material_runs_both_specialists_and_real_report(context):
    store, task, _ = context
    path = store.root / 'KB' / 'brief.html'
    path.write_text('<h1>项目说明</h1><p>2026年收入为120万元。</p><script>hiddenScript()</script>', encoding='utf-8')
    source = store.register_file(path, path.name, None, source='kb')
    task.file_ids = [source['file_id']]
    task.expected_outputs = ['html']
    task.request = '把选中的HTML正文整理成HTML报告'
    ids = lambda: list(store._index('analyses'))
    _, factory = models([
        call('task', {'subagent_type': 'analyst', 'description': 'Prepare authorized HTML material.'}),
        lambda _: call('task', {'subagent_type': 'report_designer', 'description': 'Render HTML using analysis ' + ids()[0]}),
        call('finish_task', {'intent': 'report', 'answer': '已整理原文并生成报告。'}), AIMessage(content='done')],
        analyst=[call('prepare_material', {'file_id': source['file_id']}), AIMessage(content='已登记材料摘录。')],
        report=[lambda _: call('read_analysis', {'analysis_id': ids()[0]}),
                lambda _: call('render_report', {'analysis_ids': ids(), 'title': '材料整理报告', 'formats': ['html'],
                    'narrative': {'summary': '以下整理来自已选原文，未对原文主张开展独立统计验证。'}}), AIMessage(content='已生成正式报告。')])
    result = run_task(task, store, Mock(), lambda: False, model_factory=factory)
    assert result['status'] == 'completed', result
    assert len(result['analysis_ids']) == 1 and len(result['report_ids']) == 1
    analysis = store.get_analysis(result['analysis_ids'][0], task.session_id)
    material = analysis.kb_refs[0]
    assert material['source_type'] == 'material' and material['file_id'] == source['file_id']
    assert material['source_hash'] == analysis.input_versions[source['file_id']]
    assert '2026年收入为120万元。' in material['text'] and 'hiddenScript' not in material['text']
    assert analysis.metrics['source_characters'] == len(material['text'])
    assert analysis.metrics['excerpt_characters'] == len(material['text'])
    assert not material['truncated'] and not analysis.table_ids
    assert all(d['operation'] == 'prepare_material' for d in analysis.metric_definitions.values())
    html = store.resolve_artifact(result['report_ids'][0]).read_text(encoding='utf-8')
    assert '2026年收入为120万元。' in html and 'hiddenScript' not in html
    assert '材料' in result['final_response'] and '不代表业务统计' in result['final_response']


@pytest.mark.parametrize('suffix', ['.html', '.md', '.txt'])
def test_material_excerpt_is_bounded_and_sources_are_verified(context, suffix):
    from src.agents import analyst_tools
    store, task, _ = context
    path = store.root / 'KB' / ('long' + suffix)
    path.write_text('材料正文' * 1800, encoding='utf-8')
    source = store.register_file(path, path.name, None, source='kb')
    task.file_ids = [source['file_id']]
    runtime = TaskRuntime(task, store, Mock(), lambda: False)
    prepare = next(t for t in analyst_tools(runtime) if t.name == 'prepare_material')
    result = prepare.invoke({'file_id': source['file_id']})
    assert result.get('status') == 'completed', result
    ref = result['kb_refs'][0]
    assert len(ref['text']) == 6000 and ref['truncated']
    assert result['metrics'] == {'source_characters': 7200, 'excerpt_characters': 6000}
    path.write_text('modified', encoding='utf-8')
    with pytest.raises(Exception, match='checksum|version|changed'):
        store.get_analysis(result['analysis_id'], task.session_id)


def test_prepare_material_rejects_dataset_and_unselected_source(context):
    from src.agents import analyst_tools
    store, task, file = context
    runtime = TaskRuntime(task, store, Mock(), lambda: False)
    prepare = next(t for t in analyst_tools(runtime) if t.name == 'prepare_material')
    assert prepare.invoke({'file_id': file['file_id']})['code'] == 'invalid_material'
    task.file_ids = []
    assert prepare.invoke({'file_id': file['file_id']})['code'] == 'file_not_selected'
    assert not runtime.analyses


def test_report_need_analysis_returns_to_main_for_real_supplement(context):
    store, task, file = context
    initial = DataService(store).analyze(task, file['file_id'], 'aggregate', aggregations={'amount': 'mean'})
    task.analysis_ids = [initial.analysis_id]
    task.expected_outputs = ['html']
    task.request = '生成包含amount最大值的HTML报告'
    newest = lambda: [aid for aid in store._index('analyses') if aid != initial.analysis_id]
    _, factory = models([
        call('task', {'subagent_type': 'report_designer', 'description': 'Generate report with amount_max metric.'}),
        call('task', {'subagent_type': 'analyst', 'description': 'Supplement amount maximum from authorized data.'}),
        lambda _: call('task', {'subagent_type': 'report_designer', 'description': 'Use supplemented analysis ' + newest()[0]}),
        call('finish_task', {'intent': 'report', 'answer': '已补充所需指标并生成报告。'}), AIMessage(content='done')],
        analyst=[call('analyze_dataset', {'file_id': file['file_id'], 'operation': 'aggregate', 'aggregations': {'amount': 'max'}}), AIMessage(content='已登记最大值。')],
        report=[call('render_report', {'analysis_ids': [initial.analysis_id], 'formats': ['html'],
                    'narrative': {'sections': [{'heading': '最大值', 'metric_refs': [{'analysis_id': initial.analysis_id, 'metric_key': 'amount_max', 'label': '最大值'}]}]}}), AIMessage(content='need_analysis: 请补充amount最大值。'),
                lambda _: call('render_report', {'analysis_ids': newest(), 'formats': ['html'],
                    'narrative': {'sections': [{'heading': '最大值', 'metric_refs': [{'analysis_id': newest()[0], 'metric_key': 'amount_max', 'label': '最大值'}]}]}}), AIMessage(content='报告已生成。')])
    events = Mock()
    result = run_task(task, store, events, lambda: False, model_factory=factory)
    assert result['status'] == 'completed', result
    assert store.get_analysis(result['analysis_ids'][0], task.session_id).metrics['amount_max'] == 30
    assert result['report_ids']
    tools = [call.kwargs.get('tool') for call in events.call_args_list if call.args[0] == 'tool']
    assert tools.count('task') == 3 and tools.count('analyze_dataset') == 1 and tools.count('render_report') == 2


def test_report_supplement_requests_share_two_round_limit(context):
    from src.agents import report_tools
    store, task, _ = context
    runtime = TaskRuntime(task, store, Mock(), lambda: False)
    tools = {t.name: t for t in report_tools(runtime)}
    assert tools['render_report'].invoke({'analysis_ids': []})['round'] == 1
    assert tools['request_analysis'].invoke({'request': '缺少均值'})['round'] == 2
    assert tools['render_report'].invoke({'analysis_ids': []})['status'] == 'waiting_user'
    assert runtime.clarification and not runtime.reports


def test_report_format_must_match_user_selection(context):
    from src.agents import report_tools
    store, task, file = context
    analysis = DataService(store).analyze(task, file['file_id'], 'aggregate', aggregations={'amount': 'mean'})
    task.analysis_ids = [analysis.analysis_id]
    task.expected_outputs = ['xlsx']
    runtime = TaskRuntime(task, store, Mock(), lambda: False)
    render = next(t for t in report_tools(runtime) if t.name == 'render_report')
    result = render.invoke({'analysis_ids': task.analysis_ids, 'formats': ['html']})
    assert result['code'] == 'report_format_mismatch' and not runtime.reports
    result = render.invoke({'analysis_ids': task.analysis_ids})
    assert result['status'] == 'completed', result
    assert len(result['report_ids']) == 1
    assert store.resolve_artifact(result['report_ids'][0]).suffix == '.xlsx'


def test_tool_errors_are_role_scoped_redacted_and_recoverable(context):
    store, task, file = context
    events = Mock()
    _, factory = models([
        call('task', {'subagent_type': 'analyst', 'description': 'Compute amount mean.'}),
        call('finish_task', {'intent': 'analysis', 'answer': '已按正确列完成计算。'}), AIMessage(content='done')],
        analyst=[call('analyze_dataset', {'file_id': file['file_id'], 'operation': 'aggregate', 'aggregations': {'offline-secret': 'mean'}}),
                 call('analyze_dataset', {'file_id': file['file_id'], 'operation': 'aggregate', 'aggregations': {'amount': 'mean'}}), AIMessage(content='done')])
    result = run_task(task, store, events, lambda: False, model_factory=factory)
    assert result['status'] == 'completed', result
    errors = [item for item in events.call_args_list if item.args[0] == 'tool_error']
    assert len(errors) == 1
    assert errors[0].kwargs == {'role': 'analyst', 'tool': 'analyze_dataset', 'code': 'unknown_column'}
    assert 'offline-secret' not in str(errors[0]) and '[REDACTED]' in errors[0].args[1]
    assert 'args' not in errors[0].kwargs and 'reasoning' not in str(errors[0])


def test_tool_schema_validation_error_has_safe_diagnostic(context):
    store, task, file = context
    events = Mock()
    _, factory = models([
        call('task', {'subagent_type': 'analyst', 'description': 'Compute amount mean.'}),
        call('finish_task', {'intent': 'analysis', 'answer': '已完成计算。'}), AIMessage(content='done')],
        analyst=[call('analyze_dataset', {'file_id': file['file_id'], 'operation': 'aggregate', 'aggregations': {'amount': 'unsupported'}}),
                 call('analyze_dataset', {'file_id': file['file_id'], 'operation': 'aggregate', 'aggregations': {'amount': 'mean'}}), AIMessage(content='done')])
    result = run_task(task, store, events, lambda: False, model_factory=factory)
    assert result['status'] == 'completed', result
    errors = [item for item in events.call_args_list if item.args[0] == 'tool_error']
    assert errors and errors[0].kwargs['code'] == 'tool_validation_error'
    assert 'unsupported' not in errors[0].args[1]


def test_analysis_progress_is_shared_without_copying_private_history(context):
    store, task, file = context
    scripted, factory = models([
        call('task', {'subagent_type': 'analyst', 'description': 'Compute mean.'}),
        call('finish_task', {'intent': 'analysis', 'answer': '已完成计算。'}), AIMessage(content='done')],
        analyst=[call('analyze_dataset', {'file_id': file['file_id'], 'operation': 'aggregate', 'aggregations': {'amount': 'mean'}}), AIMessage(content='done')])
    result = run_task(task, store, Mock(), lambda: False, model_factory=factory)
    assert result['status'] == 'completed'
    seen = str(scripted['main']._seen[-1])
    assert result['analysis_ids'][0] in seen and 'remaining_tool_calls' in seen
    assert 'amount_mean' in seen


def test_aggregation_schema_has_typed_functions_and_examples(context):
    from src.agents import analyst_tools
    store, task, _ = context
    runtime = TaskRuntime(task, store, Mock(), lambda: False)
    tool = next(t for t in analyst_tools(runtime) if t.name == 'analyze_dataset')
    schema = tool.args_schema.model_json_schema()
    value = schema['properties']['aggregations']['anyOf'][0]['additionalProperties']
    assert 'mean' in str(value) and 'count' in str(value)
    assert 'source_rows' in tool.description and 'NOT input columns' in tool.description
    assert '"chart":"bar"' in tool.description and 'NEVER pass a filename' in tool.description


@pytest.mark.parametrize('payload', [
    {'summary': ['错误的段落列表']}, {'summary': {'text': '错误对象'}},
    {'sections': [{'heading': ['错误标题']}]}, {'sections': [{'text': {'raw': '错误对象'}}]},
    {'sections': [{'metric_refs': [{'analysis_id': 'a', 'metric_key': 'mean', 'label': 12}]}]},
    {'sections': [{'table_ids': 'not-a-list'}]}, {'limitations': [{'text': 'not-text'}]},
])
def test_report_narrative_contract_rejects_actual_shape_errors(payload):
    from pydantic import ValidationError
    from src.schemas import ReportNarrative
    with pytest.raises(ValidationError):
        ReportNarrative.model_validate(payload)


def test_invalid_narrative_schema_repairs_report_without_reanalysis(context):
    store, task, file = context
    analysis = DataService(store).analyze(task, file['file_id'], 'aggregate', aggregations={'amount': 'mean'})
    task.analysis_ids = [analysis.analysis_id]
    task.expected_outputs = ['html']
    task.request = '使用已有结果生成HTML报告'
    events = Mock()
    _, factory = models([
        call('task', {'subagent_type': 'report_designer', 'description': 'Generate from existing analysis.'}),
        call('finish_task', {'intent': 'report', 'answer': '报告已生成。'}), AIMessage(content='done')],
        report=[call('render_report', {'analysis_ids': task.analysis_ids, 'narrative': {'summary': ['invalid shape']}}),
                call('render_report', {'analysis_ids': task.analysis_ids, 'narrative': None}), AIMessage(content='done')])
    result = run_task(task, store, events, lambda: False, model_factory=factory)
    assert result['status'] == 'completed', result
    assert result['analysis_ids'] == task.analysis_ids and result['report_ids']
    assert '默认报告模板' in result['final_response']
    validators = [item for item in events.call_args_list if item.args[0] == 'report_validation']
    assert any(item.kwargs.get('reason_code') == 'invalid_narrative' for item in validators)
    assert not any(item.kwargs.get('tool') == 'analyze_dataset' for item in events.call_args_list)


def test_prose_revision_cap_falls_back_to_safe_title_and_keeps_formats(context):
    from src.agents import report_tools
    store, task, file = context
    analysis = DataService(store).analyze(task, file['file_id'], 'aggregate', aggregations={'amount': 'mean'})
    task.analysis_ids = [analysis.analysis_id]
    task.expected_outputs = ['xlsx']
    events = Mock()
    runtime = TaskRuntime(task, store, events, lambda: False)
    render = next(tool for tool in report_tools(runtime) if tool.name == 'render_report')
    arguments = {'analysis_ids': task.analysis_ids, 'title': '报告2026', 'narrative': {'summary': '均值为999。'}}
    first = render.invoke(arguments)
    second = render.invoke(arguments)
    assert first['status'] == second['status'] == 'needs_revision'
    assert runtime.report_rounds == 0 and runtime.report_revisions == 2
    fallback = render.invoke(arguments)
    assert fallback['status'] == 'completed', fallback
    assert fallback['validation_summary']['default_template_fallback']
    assert fallback['limitations'] and len(fallback['report_ids']) == 1
    artifact = store.get_artifact(fallback['report_ids'][0])
    assert artifact.name.endswith('.xlsx')
    import json
    from openpyxl import load_workbook
    manifest = json.loads(store.resolve_artifact(fallback['validation_summary']['manifest_id']).read_text(encoding='utf-8'))
    assert any('默认报告模板' in text for text in manifest['limitations'])
    book = load_workbook(store.resolve_artifact(fallback['report_ids'][0]), read_only=True)
    try:
        assert any('默认报告模板' in str(cell.value) for sheet in book for row in sheet for cell in row)
    finally:
        book.close()
    assert not any(item.kwargs.get('status') == 'need_analysis' for item in events.call_args_list)


@pytest.mark.parametrize('revisions', [0, 2])
def test_missing_metric_stays_analysis_request_not_prose_revision(context, revisions):
    from src.agents import report_tools
    store, task, file = context
    analysis = DataService(store).analyze(task, file['file_id'], 'aggregate', aggregations={'amount': 'mean'})
    task.analysis_ids = [analysis.analysis_id]
    runtime = TaskRuntime(task, store, Mock(), lambda: False)
    runtime.report_revisions = revisions
    render = next(tool for tool in report_tools(runtime) if tool.name == 'render_report')
    result = render.invoke({'analysis_ids': task.analysis_ids, 'narrative': {'summary': '{{metric:' + analysis.analysis_id + ':amount_max}}'}})
    assert result['status'] == 'need_analysis'
    assert runtime.report_rounds == 1 and runtime.report_revisions == revisions
    assert not runtime.reports


def test_explicit_twenty_tool_budget_remains_strict(context):
    from src.agents import BudgetExceeded
    store, task, _ = context
    store.settings.max_tool_calls = 20
    runtime = TaskRuntime(task, store, Mock(), lambda: False)
    for _ in range(20):
        runtime.consume('main', 'file_catalog')
    with pytest.raises(BudgetExceeded):
        runtime.consume('analyst', 'analyze_dataset')
    assert runtime.calls == 20


def test_report_delivered_on_last_budget_slot_completes_without_more_model_calls(context):
    store, task, file = context
    analysis = DataService(store).analyze(task, file['file_id'], 'aggregate', aggregations={'amount': 'mean'})
    task.analysis_ids = [analysis.analysis_id]
    task.request, task.expected_outputs = '使用已有结果生成报告', ['html', 'xlsx']
    store.settings.max_tool_calls = 2
    scripted, factory = models([
        call('task', {'subagent_type': 'report_designer', 'description': 'Render registered analyses.'})],
        report=[call('render_report', {'analysis_ids': task.analysis_ids, 'narrative': None})])
    result = run_task(task, store, Mock(), lambda: False, model_factory=factory)
    assert result['status'] == 'completed' and result['tool_calls'] == 2, result
    assert len(result['report_ids']) == 2
    for aid in result['report_ids']:
        assert store.resolve_artifact(aid).is_file()
    assert scripted['main']._index == scripted['report_designer']._index == 1
    assert scripted['analyst']._index == 0


def test_specialist_clarification_stops_all_agents_at_last_budget_slot(context):
    store, task, _ = context
    task.file_ids = []
    store.settings.max_tool_calls = 2
    scripted, factory = models([
        call('task', {'subagent_type': 'analyst', 'description': 'Check data selection.'})],
        analyst=[call('ask_user', {'question': '请选择本次分析文件。'})])
    result = run_task(task, store, Mock(), lambda: False, model_factory=factory)
    assert result['status'] == 'waiting_user' and result['tool_calls'] == 2, result
    assert result['final_response'] == '请选择本次分析文件。'
    assert scripted['main']._index == scripted['analyst']._index == 1


def test_pending_tool_after_clarification_is_not_executed_or_counted(context):
    from types import SimpleNamespace
    from src.agents import BoundaryMiddleware
    store, task, _ = context
    runtime = TaskRuntime(task, store, Mock(), lambda: False)
    runtime.clarification = '请选择文件。'
    middleware = BoundaryMiddleware(runtime, 'analyst')
    handler = Mock(side_effect=AssertionError('Must not run after clarification'))
    response = middleware.wrap_tool_call(SimpleNamespace(tool_call={'name': 'run_python', 'id': 'late', 'args': {'code': 'bad'}}), handler)
    assert response.tool_call_id == 'late' and runtime.calls == 0
    handler.assert_not_called()


def test_report_batch_read_preserves_authorization_and_registered_sources(context):
    from src.agents import report_tools
    store, task, file = context
    analysis = DataService(store).analyze(task, file['file_id'], 'aggregate', aggregations={'amount': 'mean'})
    task.analysis_ids = [analysis.analysis_id]
    runtime = TaskRuntime(task, store, Mock(), lambda: False)
    batch = next(tool for tool in report_tools(runtime) if tool.name == 'read_analyses')
    result = batch.invoke({'analysis_ids': [analysis.analysis_id, analysis.analysis_id]})
    assert len(result['items']) == 1 and result['items'][0]['metrics']['amount_mean'] == 20
    assert batch.invoke({'analysis_ids': [analysis.analysis_id, 'unauthorized']})['status'] == 'error'


@pytest.mark.parametrize('removed_field,removed_heading', [
    ('include_metrics_summary','指标汇总'), ('include_quality_and_limitations','数据质量与局限')])
def test_report_revision_routes_to_designer_and_removal_survives_prose_fallback(context, removed_field, removed_heading):
    from src.reporting import ReportService
    from bs4 import BeautifulSoup
    from src.storage import file_hash
    store, original, file = context
    analysis = DataService(store).analyze(original, file['file_id'], 'aggregate', aggregations={'amount': 'mean'})
    base = ReportService(store).generate(original, [analysis.analysis_id], formats=['html'],
                                         layout={'include_metrics_summary': True})
    store.update_task(original.task_id, status='succeeded', result=base.model_dump())
    old_path = store.resolve_artifact(base.report_ids[0])
    old_hash = file_hash(old_path)
    task = TaskSpec(session_id=original.session_id, request='删除上述报告的指标汇总，重新生成报告')
    store.create_task(task)
    changes = {removed_field: False}
    if removed_field != 'include_metrics_summary':
        changes['include_metrics_summary'] = True  # Explicit opt-in while testing the other switch.
    bad_shape = {'layout_changes': changes, 'narrative': {'summary': ['wrong']}}
    scripted, factory = models([
        call('task', {'subagent_type': 'report_designer', 'description': 'Revise latest report; remove metrics summary.'})],
        report=[call('revise_report', bad_shape),
                call('revise_report', {'layout_changes': changes, 'narrative': {'summary': '均值为999。'}}, 'repair'),
                call('revise_report', bad_shape, 'fallback')])
    result = run_task(task, store, Mock(), lambda: False, model_factory=factory)
    assert result['status'] == 'completed' and len(result['report_ids']) == 1, result
    assert result['report_revision']['layout_changes'] == {removed_field: False}
    assert f'已移除“{removed_heading}”' in result['final_response']
    soup = BeautifulSoup(store.resolve_artifact(result['report_ids'][0]).read_text(encoding='utf-8'), 'html.parser')
    assert not soup.find('h2', string=removed_heading)
    assert bool(soup.select('.metric-value')) == (removed_field != 'include_metrics_summary')
    assert scripted['analyst']._index == 0 and not list((store.root/'script_output'/task.session_id/task.task_id).glob('analysis_*.json'))
    assert old_hash == file_hash(old_path)
