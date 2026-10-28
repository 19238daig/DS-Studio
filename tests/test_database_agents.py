"""Real PostgreSQL-to-Agent-to-sandbox flow and source isolation regressions."""
import csv
import json
import subprocess

import pytest
from langchain_core.messages import AIMessage

from src.agents import TaskRuntime, analyst_tools, main_tools, report_tools, run_task
from src.execution import DockerExecutor
from src.schemas import MessageRequest, TaskSpec
from src.services import Services
from src.storage import StoreError
from test_agents import call, models
from test_database_core import db_setup, isolated_postgres


def task_runtime(db_setup, *, source='olist', tables=None, request='统计订单数量', constraints=None):
    store, _, sid = db_setup
    task = TaskSpec(session_id=sid, request=request, database_source_ids=[source] if source else [],
                    database_tables={source: tables} if tables is not None else {}, constraints=constraints or {})
    store.create_task(task)
    event = lambda kind, message='', **details: store.add_event(task.task_id, kind, message, **details)
    return TaskRuntime(task, store, event, lambda: False)


def tools_for(runtime, factory):
    return {item.name: item for item in factory(runtime)}


def test_sql_only_analyst_and_scalar_values_are_source_bound(db_setup):
    runtime = task_runtime(db_setup)
    assert not {'run_sql', 'run_sql_batch'} & tools_for(runtime, main_tools).keys()
    assert not {'run_sql', 'run_sql_batch'} & tools_for(runtime, report_tools).keys()
    result = tools_for(runtime, analyst_tools)['run_sql'].invoke({
        'source_id': 'olist', 'sql': 'SELECT count(*) AS order_count FROM olist.orders'})
    analysis = runtime.store.get_analysis(result['items'][0]['analysis_id'], runtime.task.session_id)
    assert analysis.metrics == {'order_count': 2}
    assert not analysis.input_versions and len(analysis.db_refs) == 1
    assert runtime.authorize_analysis(analysis.analysis_id).db_refs == analysis.db_refs
    assert [event['type'] for event in runtime.store.list_events(runtime.task.task_id) if event['type'].startswith('sql_')] == ['sql_started', 'sql_query_completed', 'sql_completed']
    assert result['queries'][0]['complete'] is True


def test_true_deepagents_routes_db_question_and_binds_only_requested_metric(db_setup):
    store, _, sid = db_setup
    task = TaskSpec(session_id=sid, request='统计订单数量', database_source_ids=['olist'])
    store.create_task(task)
    def finish(messages):
        aid = next(iter(store._index('analyses')))
        return call('finish_task', {'intent': 'analysis', 'answer': '已查询。', 'metric_refs': [
            {'analysis_id': aid, 'metric_key': 'order_count', 'label': '订单数量'}]}, 'finish')
    _, factory = models([
        call('task', {'subagent_type': 'analyst', 'description': '使用olist查询订单数量并返回analysis_id'}),
        finish, AIMessage(content='done')], [
        call('run_sql', {'source_id': 'olist', 'sql': 'SELECT count(*) AS order_count FROM olist.orders'}, 'sql'),
        AIMessage(content='completed')])
    event = lambda kind, message='', **details: store.add_event(task.task_id, kind, message, **details)
    result = run_task(task, store, event, lambda: False, model_factory=factory)
    assert result['status'] == 'completed', result
    assert result['final_response'] == '订单数量：**2**。'
    assert len(result['analysis_ids']) == 1
    events = store.list_events(task.task_id)
    assert any(item['type'] == 'agent_assigned' and item['target'] == 'analyst' for item in events)
    assert any(item['type'] == 'sql_completed' and item['row_count'] == 1 for item in events)


def test_database_catalog_and_main_selection_cannot_expand_explicit_scope(db_setup):
    runtime = task_runtime(db_setup, source='sellers_restricted', tables=['sellers'])
    catalog = tools_for(runtime, analyst_tools)['database_catalog'].invoke({'source_id': 'sellers_restricted'})
    assert [item['qualified_name'] for item in catalog['tables']] == ['olist.sellers']
    denied = tools_for(runtime, main_tools)['select_database_sources'].invoke({'source_ids': ['olist']})
    assert denied['code'] == 'database_source_not_selected'
    assert runtime.store.get_task(runtime.task.task_id)['spec']['database_source_ids'] == ['sellers_restricted']
    empty = task_runtime(db_setup, source=None, constraints={'database_sources_explicit': True})
    denied = tools_for(empty, main_tools)['select_database_sources'].invoke({'source_ids': ['olist']})
    assert denied['code'] == 'database_source_not_selected'


def test_main_database_selection_is_persisted_before_query_and_empty_tables_remain_empty(db_setup):
    runtime = task_runtime(db_setup, source=None)
    result = tools_for(runtime, main_tools)['select_database_sources'].invoke({
        'source_ids': ['olist'], 'tables': {'olist': ['orders']}})
    assert result['selected_tables'] == {'olist': ['olist.orders']}
    assert runtime.store.get_task(runtime.task.task_id)['spec']['database_tables'] == runtime.task.database_tables
    assert tools_for(runtime, analyst_tools)['run_sql'].invoke({'source_id': 'olist',
        'sql': 'SELECT count(*) AS orders FROM olist.orders'})['items'][0]['metrics'] == {'orders': 2}
    tools_for(runtime, main_tools)['select_database_sources'].invoke({'source_ids': ['olist'], 'tables': {'olist': []}})
    assert not runtime.database_catalog('olist')['tables']
    denied = tools_for(runtime, analyst_tools)['run_sql'].invoke({'source_id': 'olist',
        'sql': 'SELECT count(*) FROM olist.orders'})
    assert denied['code'] == 'sql_object_not_allowed'


def test_first_main_choice_then_clear_cannot_switch_to_full_profile(db_setup):
    runtime = task_runtime(db_setup, source=None)
    select = tools_for(runtime, main_tools)['select_database_sources']
    first = select.invoke({'source_ids': ['sellers_restricted']})
    assert first['selected_source_ids'] == ['sellers_restricted']
    assert select.invoke({'source_ids': []})['selected_source_ids'] == []
    denied = select.invoke({'source_ids': ['olist']})
    assert denied['code'] == 'database_source_not_selected'
    assert runtime.task.database_source_ids == []
    saved = runtime.store.get_task(runtime.task.task_id)['spec']
    assert saved['database_source_ids'] == [] and saved['constraints']['database_sources_explicit'] is True


def test_clearing_source_cannot_reset_table_whitelist(db_setup):
    runtime = task_runtime(db_setup, source=None)
    select = tools_for(runtime, main_tools)['select_database_sources']
    assert select.invoke({'source_ids': ['olist'], 'tables': {'olist': ['orders']}})['selected_tables'] == {
        'olist': ['olist.orders']}
    assert select.invoke({'source_ids': []})['selected_source_ids'] == []
    # Dropping the map by clearing sources must not allow re-adding the source
    # with a missing map (which ordinarily means every visible business table).
    denied = select.invoke({'source_ids': ['olist']})
    assert denied['code'] == 'database_source_not_selected'
    assert runtime.task.database_tables == {}


@pytest.mark.parametrize('followup', ['clarification', 'retry'])
def test_cleared_tool_authorization_remains_frozen_in_inherited_tasks(db_setup, followup):
    runtime = task_runtime(db_setup, source=None)
    select = tools_for(runtime, main_tools)['select_database_sources']
    select.invoke({'source_ids': ['sellers_restricted'], 'tables': {'sellers_restricted': ['sellers']}})
    select.invoke({'source_ids': []})
    services = Services(runtime.store)
    if followup == 'clarification':
        runtime.store.update_task(runtime.task.task_id, status='waiting_user')
        child = services.submit(runtime.task.session_id, MessageRequest(content='继续处理',
            reply_to_task_id=runtime.task.task_id))
    else:
        runtime.store.update_task(runtime.task.task_id, status='failed')
        child = services.retry(runtime.task.task_id)
    spec = TaskSpec.model_validate(child['spec'])
    assert spec.database_source_ids == [] and spec.constraints['database_sources_explicit'] is True
    event = lambda kind, message='', **details: runtime.store.add_event(spec.task_id, kind, message, **details)
    inherited = TaskRuntime(spec, runtime.store, event, lambda: False)
    denied = tools_for(inherited, main_tools)['select_database_sources'].invoke({'source_ids': ['olist']})
    assert denied['code'] == 'database_source_not_selected'


def test_history_never_reuses_full_profile_under_restricted_or_empty_selection(db_setup):
    original = task_runtime(db_setup)
    result = tools_for(original, analyst_tools)['run_sql'].invoke({'source_id': 'olist',
        'sql': 'SELECT count(*) AS orders FROM olist.orders'})
    aid = result['items'][0]['analysis_id']
    original.store.update_task(original.task.task_id, status='succeeded', result={'analysis_ids': [aid], 'report_ids': []})
    restricted = task_runtime(db_setup, source='sellers_restricted')
    assert aid not in restricted.allowed_analysis_ids
    empty = task_runtime(db_setup, source=None, constraints={'database_sources_explicit': True})
    assert aid not in empty.allowed_analysis_ids
    with pytest.raises(StoreError):
        restricted.authorize_analysis(aid)


def test_batch_same_snapshot_and_failed_sql_can_be_corrected(db_setup):
    runtime = task_runtime(db_setup)
    tool = tools_for(runtime, analyst_tools)
    failed = tool['run_sql'].invoke({'source_id': 'olist', 'sql': 'SELECT missing_column FROM olist.orders'})
    assert failed['status'] == 'error' and not runtime.store._index('analyses')
    result = tool['run_sql_batch'].invoke({'source_id': 'olist', 'queries': [
        {'sql': 'SELECT count(*) AS orders FROM olist.orders'},
        {'sql': 'SELECT sum(price) AS total_price FROM olist.orders'}]})
    refs = [analysis['db_refs'][0] for analysis in result['items']]
    snapshots = [runtime.database.authorize_snapshot(runtime.task, ref) for ref in refs]
    assert len({snapshot['snapshot_id'] for snapshot in snapshots}) == 1
    assert result['items'][1]['metrics']['total_price'] == '30.000000'
    assert runtime.last_error is None
    assert any(event['type'] == 'sql_failed' for event in runtime.store.list_events(runtime.task.task_id))


def test_sql_analysis_reports_and_revision_restore_only_implicit_source(db_setup):
    runtime = task_runtime(db_setup, request='生成订单数量报告')
    result = tools_for(runtime, analyst_tools)['run_sql'].invoke({'source_id': 'olist',
        'sql': 'SELECT count(*) AS order_count FROM olist.orders'})
    aid = result['items'][0]['analysis_id']
    generated = tools_for(runtime, report_tools)['render_report'].invoke({'analysis_ids': [aid], 'title': '订单数量报告'})
    assert generated['status'] == 'completed', generated
    original = {rid: runtime.store.resolve_artifact(rid).read_bytes() for rid in generated['report_ids']}
    runtime.store.update_task(runtime.task.task_id, status='succeeded', result={
        'analysis_ids': [aid], 'report_ids': generated['report_ids']})
    revision = task_runtime(db_setup, source=None, request='修改报告标题')
    assert revision.task.database_source_ids == ['olist']
    assert aid in revision.allowed_analysis_ids
    base = tools_for(revision, report_tools)['read_report'].invoke({})
    assert base['status'] if 'status' in base else base['report_id']
    explicit = task_runtime(db_setup, source=None, request='修改报告标题',
        constraints={'database_sources_explicit': True})
    assert not explicit.task.database_source_ids and aid not in explicit.allowed_analysis_ids
    assert tools_for(explicit, report_tools)['read_report'].invoke({})['code'] == 'database_source_not_selected'
    assert original == {rid: runtime.store.resolve_artifact(rid).read_bytes() for rid in original}


def test_full_sql_snapshot_is_readonly_docker_input_and_svg_inherits_db_provenance(db_setup):
    available = subprocess.run(['docker', 'image', 'inspect', 'ds-analysis:local'], capture_output=True)
    if available.returncode:
        pytest.skip('Build ds-analysis:local for real snapshot Docker integration')
    runtime = task_runtime(db_setup)
    result = tools_for(runtime, analyst_tools)['run_sql'].invoke({'source_id': 'olist', 'sql':
        'SELECT row_number() OVER () AS row_number, p.price FROM olist.products p '
        'CROSS JOIN olist.orders o CROSS JOIN olist.orders o2 '
        'CROSS JOIN olist.orders o3 CROSS JOIN olist.orders o4'})
    query = result['queries'][0]
    assert query['row_count'] == 32 and len(query['preview']) == 20
    refs = runtime.database_inputs()
    executor = DockerExecutor(runtime.store)
    inputs = executor.input_manifest(runtime.task, db_refs=refs)
    assert len(inputs) == 1 and 'file_id' not in inputs[0]
    path = inputs[0]['path']
    code = f'''
import json, os
import pandas as pd
import matplotlib.pyplot as plt
assert not any(key.startswith('DB_') or key in ('API_KEY','OPENAI_API_KEY') for key in os.environ)
try:
    open({path!r}, 'a').write('tamper')
    raise AssertionError('snapshot writable')
except OSError:
    pass
df = pd.read_csv({path!r}, dtype=str)
assert len(df) == 32
df.to_csv('/output/full.csv', index=False)
json.dump({{'metrics':{{'full_rows':len(df)}}, 'metric_definitions':{{'full_rows':'Count of complete authorized SQL snapshot rows'}},
 'tables':['full.csv'], 'charts':['rows.svg'], 'findings':['The complete snapshot was used.']}}, open('/output/result.json','w'))
fig, ax = plt.subplots()
ax.bar(['Full snapshot'], [len(df)])
ax.set_ylabel('Rows')
fig.savefig('/output/rows.svg')
'''
    derived = executor.run_python(runtime.task, code, db_refs=refs)
    assert derived.metrics == {'full_rows': 32} and not derived.input_versions
    assert derived.db_refs == refs and derived.chart_ids
    assert runtime.store.resolve_artifact(derived.chart_ids[0]).suffix == '.svg'
    assert any(item.kind == 'chart_preview' for item in runtime.store.list_artifacts(runtime.task.task_id))
    assert runtime.authorize_analysis(derived.analysis_id).db_refs == refs
    with runtime.store.resolve_artifact(derived.table_ids[0]).open(encoding='utf-8-sig') as stream:
        assert len(list(csv.DictReader(stream))) == 32

