import json
from unittest.mock import Mock

import pytest
from langchain_core.messages import AIMessage

from src.agents import TaskRuntime, _report_requested, analyst_tools, main_tools, report_tools, run_task
from src.config import Settings
from src.data import DataService
from src.reporting import ReportService
from src.schemas import MessageRequest, TaskSpec
from src.services import Services
from src.storage import Store, file_hash
from test_agents import call, models


def tools_for(runtime, builder):
    return {tool.name: tool for tool in builder(runtime)}


@pytest.fixture
def report_context(tmp_path):
    store = Store(Settings(workspace=tmp_path, api_key='offline', _env_file=None))
    sid = store.new_session()['session_id']
    path = tmp_path / 'KB' / 'revenue.csv'
    path.write_text('month,amount\nJan,10\nFeb,20\nMar,30\n', encoding='utf-8')
    fid = store.refresh_kb()[0]['file_id']
    original = TaskSpec(session_id=sid, request='Generate revenue report', file_ids=[fid])
    store.create_task(original)
    data = DataService(store)
    chart = data.analyze(original, fid, 'aggregate', group_by=['month'], aggregations={'amount': 'sum'}, chart='bar')
    mean = data.analyze(original, fid, 'aggregate', aggregations={'amount': 'mean'})
    base = ReportService(store).generate(original, [chart.analysis_id, mean.analysis_id], 'Revenue report',
        {'summary': 'Mean={{metric:' + mean.analysis_id + ':amount_mean}}.',
         'sections': [{'heading': 'Observations', 'text': 'Keep this original paragraph.'},
                      {'heading': 'Recommendations', 'text': 'This recommendation is deliberately long.'}]}, ['html'])
    store.update_task(original.task_id, status='succeeded', result=base.model_dump())
    task = TaskSpec(session_id=sid, request='第二张图换成折线图，其余不变', file_ids=[fid])
    store.create_task(task)
    return store, task, fid, chart, mean, base


@pytest.mark.parametrize('user_request', ['这一段简短些', '第二张图换成折线图，其余不变', '把摘要译成英文',
                                     '新增一节经营建议', '这份报告只统计已交付订单，其余不变'])
def test_followup_revision_is_not_limited_to_top_words(user_request):
    task = TaskSpec(session_id='test', request=user_request)
    assert _report_requested(task, None, has_prior_report=True)


@pytest.mark.parametrize('user_request', ['只统计已交付订单有多少笔？', '上一份报告标题是什么？',
                                     '解释报告中的结论', '如何修改报告里的图表？',
                                     '不要修改报告，只告诉我订单数量', '请记住以后默认图表改成折线图'])
def test_readonly_and_new_questions_do_not_force_revision(user_request):
    assert not _report_requested(TaskSpec(session_id='test', request=user_request), None, has_prior_report=True)


def test_prepare_persists_exact_base_targets_and_rejects_scope_switch(report_context):
    store, task, _, _, _, base = report_context
    runtime = TaskRuntime(task, store, Mock(), lambda: False)
    tools = tools_for(runtime, report_tools)
    prepared = tools['prepare_report_revision'].invoke({'targets': ['section:Recommendations']})
    assert prepared['revision_plan']['base_report_id'] == base.report_ids[0]
    assert store.get_task(task.task_id)['spec']['constraints']['report_revision']['targets'] == ['section:Recommendations']
    alternate_task = TaskSpec(session_id=task.session_id, request='Generate alternate report')
    store.create_task(alternate_task)
    alternate = ReportService(store).generate(alternate_task, prepared['analysis_ids'], 'Alternate report', formats=['html'])
    store.update_task(alternate_task.task_id, status='succeeded', result=alternate.model_dump())
    switched = tools['prepare_report_revision'].invoke({'report_id': alternate.report_ids[0]})
    assert switched['code'] == 'revision_base_changed'
    assert runtime.revision_state['base_report_id'] == base.report_ids[0]
    assert runtime.revision_state['targets'] == ['section:Recommendations']
    patch = {'sections': [{'operation': 'update', 'target_heading': 'Recommendations',
                          'section': {'text': 'Act cautiously.'}}]}
    result = tools['revise_report'].invoke({'revision_patch': patch})
    assert result['status'] == 'completed', result
    manifest = json.loads(store.resolve_artifact(result['validation_summary']['manifest_id']).read_text(encoding='utf-8'))
    assert manifest['presentation']['sections'][0]['text'] == 'Keep this original paragraph.'
    assert manifest['presentation']['sections'][1]['text'] == 'Act cautiously.'
    assert manifest['revision_of']['report_id'] == base.report_ids[0]
    assert result['validation_summary']['revision_integrity']['changed_targets'] == ['section:Recommendations']


def test_report_content_read_does_not_pin_or_force_output(report_context):
    store, task, _, _, _, base = report_context
    task.request = '上一份报告标题是什么？'
    store.update_task(task.task_id, spec=task.model_dump())
    runtime = TaskRuntime(task, store, Mock(), lambda: False)
    result = tools_for(runtime, report_tools)['read_report'].invoke({})
    assert result['report_id'] == base.report_ids[0]
    assert runtime.revision_state is None and not runtime.followup_report_edit
    assert 'report_revision' not in store.get_task(task.task_id)['spec']['constraints']


def test_pinned_revision_does_not_allow_qa_early_stop_or_template_replacement(report_context):
    store, task, fid, _, mean, base = report_context
    runtime = TaskRuntime(task, store, Mock(), lambda: False)
    rtools = tools_for(runtime, report_tools)
    rtools['prepare_report_revision'].invoke({'targets': ['summary']})
    error = tools_for(runtime, analyst_tools)['answer_metric'].invoke({'file_id': fid, 'column': 'amount', 'aggregation': 'mean'})
    assert error['code'] == 'invalid_metric_query' and runtime.terminal_result() is None
    runtime.report_revisions = 2
    rejected = rtools['revise_report'].invoke({'narrative': {'summary': 'Unbound 999.'},
                                             'layout_changes': {'include_sources': False}})
    assert rejected['code'] == 'revision_content_invalid'
    assert not runtime.reports and runtime.revision_state['base_report_id'] == base.report_ids[0]
    assert runtime.request_revision('unbound_prose', 'repair')['next_step'].startswith('仅修正计划中目标')


def test_real_expert_handoff_replaces_only_target_line_chart(report_context):
    store, task, fid, chart, mean, base = report_context
    original_hash = file_hash(store.resolve_artifact(base.report_ids[0]))
    target = 'chart:' + chart.chart_ids[0]

    def apply_supplement(messages):
        added = [store.get_analysis(aid, task.session_id) for aid in store._index('analyses')
                 if store.get_analysis(aid, task.session_id).task_id == task.task_id]
        assert len(added) == 1
        assert any('revision_plan' in str(message.content) for message in messages)
        new = added[0]
        return call('revise_report', {'supplemental_analysis_ids': [new.analysis_id], 'revision_patch': {
            'components': [{'kind': 'chart', 'operation': 'replace', 'target_id': chart.chart_ids[0],
                            'artifact_id': new.chart_ids[0], 'analysis_id': new.analysis_id}]}})

    scripted, factory = models([
        call('task', {'subagent_type': 'report_designer', 'description': 'Change only target chart to line.'}),
        call('task', {'subagent_type': 'analyst', 'description': 'Only create replacement line chart; keep pinned revision.'}),
        call('task', {'subagent_type': 'report_designer', 'description': 'Apply target replacement to the same pinned report.'})],
        analyst=[call('analyze_dataset', {'file_id': fid, 'operation': 'aggregate', 'group_by': ['month'],
                                       'aggregations': {'amount': 'sum'}, 'chart': 'line'}), AIMessage(content='Target chart registered.')],
        report=[call('read_report', {}), call('request_analysis', {'request': 'Only redraw the target revenue chart as a line.',
                'targets': [target]}), AIMessage(content='Please supplement only the pinned target.'), apply_supplement])
    events = []
    result = run_task(task, store, lambda kind, message='', **kw: events.append(dict(type=kind, **kw)),
                      lambda: False, model_factory=factory)
    assert result['status'] == 'completed', result
    assert scripted['analyst']._index == 2
    manifest = json.loads(store.resolve_artifact(result['report_revision']['manifest_id']).read_text(encoding='utf-8'))
    assert manifest['revision_of']['report_id'] == base.report_ids[0]
    assert manifest['component_state']['charts'][0]['component_id'] == chart.chart_ids[0]
    assert manifest['component_state']['charts'][0]['artifact_id'] != chart.chart_ids[0]
    assert manifest['presentation']['sections'][0]['text'] == 'Keep this original paragraph.'
    assert manifest['presentation']['summary'] == 'Mean=20.0.'
    assert set(result['analysis_ids']) == {item['analysis_id'] for item in manifest['analyses']}
    assert {chart.analysis_id, mean.analysis_id}.issubset(result['analysis_ids'])
    assert file_hash(store.resolve_artifact(base.report_ids[0])) == original_hash
    persisted = store.get_task(task.task_id)['spec']['constraints']['report_revision']
    assert persisted['base_report_id'] == base.report_ids[0] and persisted['targets'] == [target]
    assert persisted['supplemental_analysis_ids'] == result['report_revision']['supplemental_analysis_ids']
    store.update_task(task.task_id, status='succeeded', result=result)
    followup = TaskSpec(session_id=task.session_id, request='这一段简短些', file_ids=[fid])
    store.create_task(followup)
    next_runtime = TaskRuntime(followup, store, Mock(), lambda: False)
    next_tools = tools_for(next_runtime, report_tools)
    next_base = next_tools['prepare_report_revision'].invoke({'targets': ['section:Observations']})
    assert next_base['report_id'] == result['report_ids'][0]
    assert set(next_base['analysis_ids']) == set(result['analysis_ids'])
    shortened = next_tools['revise_report'].invoke({'revision_patch': {'sections': [
        {'operation': 'update', 'target_heading': 'Observations', 'section': {'text': 'Keep this.'}}]}})
    assert shortened['status'] == 'completed', shortened
    second_manifest = json.loads(store.resolve_artifact(shortened['validation_summary']['manifest_id']).read_text(encoding='utf-8'))
    assert second_manifest['component_state'] == manifest['component_state']
    assert second_manifest['presentation']['summary'] == manifest['presentation']['summary']
    assert {item['analysis_id'] for item in second_manifest['analyses']} == set(result['analysis_ids'])


def test_retry_and_clarification_retain_pinned_base(report_context):
    store, task, _, _, _, base = report_context
    runtime = TaskRuntime(task, store, Mock(), lambda: False)
    runtime.prepare_revision(targets=['section:Recommendations'])
    runtime.revision_state.update(patch={'summary': 'transient'}, supplemental_analysis_ids=['transient'])
    runtime._persist_revision()
    store.update_task(task.task_id, status='failed')
    retry = Services(store).retry(task.task_id)
    plan = retry['spec']['constraints']['report_revision']
    assert plan['base_report_id'] == base.report_ids[0] and plan['targets'] == ['section:Recommendations']
    assert not set(plan) & {'patch', 'supplemental_analysis_ids'}
    clarifying = TaskSpec(session_id=task.session_id, request='这一段简短些', file_ids=task.file_ids)
    store.create_task(clarifying)
    TaskRuntime(clarifying, store, Mock(), lambda: False).prepare_revision(targets=['section:Recommendations'])
    store.update_task(clarifying.task_id, status='waiting_user')
    reply = Services(store).submit(task.session_id, MessageRequest(content='只改这一节', reply_to_task_id=clarifying.task_id))
    assert reply['spec']['constraints']['report_revision']['base_report_id'] == base.report_ids[0]


def test_report_revision_remains_available_after_many_ordinary_turns(report_context):
    store, original_task, _, _, _, base = report_context
    for index in range(35):
        ordinary = TaskSpec(session_id=original_task.session_id, request=f'Ordinary question {index}')
        store.create_task(ordinary)
        store.update_task(ordinary.task_id, status='succeeded', result={'analysis_ids': [], 'report_ids': []})
    task = TaskSpec(session_id=original_task.session_id, request='\u8fd9\u4e00\u6bb5\u7b80\u77ed\u4e9b')
    store.create_task(task)
    runtime = TaskRuntime(task, store, Mock(), lambda: False)
    assert runtime.prior_reports[0]['report_id'] == base.report_ids[0]
    assert runtime.followup_report_edit
    prepared = tools_for(runtime, report_tools)['prepare_report_revision'].invoke({'targets': ['section:Recommendations']})
    assert prepared['revision_plan']['base_report_id'] == base.report_ids[0]
