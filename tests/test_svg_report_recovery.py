"""Regression for SVG supplements reading authorized result snapshots."""
import json
from unittest.mock import Mock

from langchain_core.messages import AIMessage

from src.agents import TaskRuntime, analyst_tools, report_tools, run_task
from src.data import DataService
from src.storage import file_hash
from test_agents import context, models, call
from test_database_core import db_setup, isolated_postgres
from test_database_agents import task_runtime
from test_general_revision_runtime import report_context


def test_analyst_can_preview_only_authorized_result_table(context):
    store, task, file = context
    result = DataService(store).analyze(task, file['file_id'], 'aggregate', aggregations={'amount':'mean'})
    runtime = TaskRuntime(task, store, Mock(), lambda:False)
    preview = next(t for t in analyst_tools(runtime) if t.name == 'read_result_table')
    assert preview.invoke({'artifact_id':result.table_ids[0]})['status'] == 'error'
    runtime.record_analysis(result)
    actual = preview.invoke({'artifact_id':result.table_ids[0]})
    assert actual['total_rows'] == 1 and actual['columns'] == ['amount_mean']
    store.resolve_artifact(result.table_ids[0]).write_text('amount_mean\n999\n',encoding='utf-8')
    assert preview.invoke({'artifact_id':result.table_ids[0]})['status'] == 'error'


def test_python_inputs_explains_docker_paths_and_includes_bounded_preview(db_setup):
    runtime = task_runtime(db_setup)
    tools = {t.name:t for t in analyst_tools(runtime)}
    result = tools['run_sql'].invoke({'source_id':'olist','sql':'SELECT COUNT(*) AS orders FROM olist.orders'})
    assert 'items' in result
    manifest = tools['python_inputs'].invoke({})
    assert 'ONLY in run_python Docker' in manifest['path_scope']
    item = manifest['inputs'][0]
    assert item['path'].startswith('/previous_output/')
    assert item['preview'] == [{'orders':2}] and item['preview_truncated'] is False


def test_read_file_schema_errors_are_actionable_without_echoing_values(context):
    store, task, file = context
    events = Mock()
    scripted, factory = models([
        call('task',{'subagent_type':'analyst','description':'Compute the mean amount.'}),
        call('finish_task',{'intent':'analysis'}), AIMessage(content='done')], analyst=[
        call('read_file',{'offset':'private-invalid-value','limit':80}),
        call('analyze_dataset',{'file_id':file['file_id'],'operation':'aggregate','aggregations':{'amount':'mean'}}),
        AIMessage(content='complete')])
    result = run_task(task,store,events,lambda:False,model_factory=factory)
    assert result['status'] == 'completed',result
    errors = [c for c in events.call_args_list if c.args[0]=='tool_error' and c.kwargs.get('tool')=='read_file']
    assert errors and errors[0].kwargs['validation_issues']
    assert any(e['field']=='file_path' for e in errors[0].kwargs['validation_issues'])
    assert 'private-invalid-value' not in str(errors)
    feedback = str(scripted['analyst']._seen[1])
    assert 'read_result_table' in feedback and 'validation_issues' in feedback


def test_csv_table_cannot_be_added_as_chart_and_base_is_preserved(report_context):
    store,task,_,chart,_,base = report_context
    old_hash = file_hash(store.resolve_artifact(base.report_ids[0]))
    runtime = TaskRuntime(task,store,Mock(),lambda:False)
    runtime.prepare_revision(targets=['charts:add'])
    edit = next(t for t in report_tools(runtime) if t.name=='apply_report_edit')
    response = edit.invoke({'options':{'revision_patch':{'components':[{'kind':'chart','operation':'add',
        'artifact_id':chart.table_ids[0],'analysis_id':chart.analysis_id}]}}})
    assert response['status']=='error' and response['code']=='chart_not_generated'
    assert 'request_analysis' in response['message'] and not runtime.reports
    assert file_hash(store.resolve_artifact(base.report_ids[0])) == old_hash


def test_repeated_schema_failure_names_tool_and_fields_in_terminal_message(context):
    store,task,_ = context
    _,factory = models([call('task',{'subagent_type':'analyst','description':'Compute mean amount'})],
        analyst=[call('read_file',{'offset':'private-invalid-value','limit':80},f'bad{i}') for i in range(4)])
    result=run_task(task,store,Mock(),lambda:False,model_factory=factory)
    assert result['status']=='failed'
    assert 'read_file' in result['final_response'] and 'file_path' in result['final_response']
    assert 'private-invalid-value' not in result['final_response']
