from unittest.mock import Mock

import pytest
from langchain_core.messages import AIMessage

from src.agents import TaskRuntime, analyst_tools, run_task
from src.config import Settings
from src.data import DataService
from src.schemas import TaskSpec
from src.storage import Store
from test_agents import call, models


@pytest.fixture
def metric_context(tmp_path):
    store = Store(Settings(workspace=tmp_path, _env_file=None))
    sid = store.new_session()['session_id']
    path = tmp_path/'KB'/'values.csv'
    path.write_text('pH,group\n3.0,A\n3.2,A\n3.4,B\n', encoding='utf-8')
    fid = store.refresh_kb()[0]['file_id']
    task = TaskSpec(session_id=sid, request='平均pH值是多少', file_ids=[fid])
    store.create_task(task)
    return store, task, fid


def test_scalar_deepagent_computes_only_mean_and_ends_without_extra_turns(metric_context):
    store, task, fid = metric_context
    scripted, factory = models([call('task', {'subagent_type':'analyst', 'description':'Only mean pH.'})],
                              analyst=[call('answer_metric', {'file_id':fid, 'column':'pH', 'aggregation':'mean'})])
    result = run_task(task, store, Mock(), lambda: False, model_factory=factory)
    assert result['status'] == 'completed' and result['tool_calls'] == 2
    assert len(result['final_response']) < 100 and '3.2000' in result['final_response']
    assert all(word not in result['final_response'] for word in ('pH_count', 'median', 'result_rows', '预览'))
    analysis = store.get_analysis(result['analysis_ids'][0], task.session_id)
    assert set(analysis.metrics) == {'source_rows', 'result_rows', 'pH_mean'}
    assert analysis.metric_definitions['pH_mean']['aggregations'] == {'pH':'mean'}
    assert not analysis.chart_ids
    assert scripted['main']._index == scripted['analyst']._index == 1


def test_matching_historical_metric_is_reused_without_loading_or_new_analysis(metric_context, monkeypatch):
    store, original, fid = metric_context
    prior = DataService(store).analyze(original, fid, 'aggregate', aggregations={'pH':['count','mean','median']})
    store.update_task(original.task_id, status='succeeded', result={'analysis_ids':[prior.analysis_id]})
    task = TaskSpec(session_id=original.session_id, request='平均pH值', file_ids=[fid])
    store.create_task(task)
    runtime = TaskRuntime(task, store, Mock(), lambda: False)
    monkeypatch.setattr(runtime.data, 'analyze', Mock(side_effect=AssertionError('No recomputation')))
    tool = next(tool for tool in analyst_tools(runtime) if tool.name=='answer_metric')
    result = tool.invoke({'file_id':fid, 'column':'pH', 'aggregation':'mean'})
    assert result['reused'] and result['analysis_id']==prior.analysis_id
    assert not list((store.root/'script_output'/task.session_id/task.task_id).glob('analysis_*.json'))


def test_changed_filter_does_not_reuse_global_mean(metric_context):
    store, original, fid = metric_context
    prior = DataService(store).analyze(original, fid, 'aggregate', aggregations={'pH':'mean'})
    store.update_task(original.task_id, status='succeeded', result={'analysis_ids':[prior.analysis_id]})
    task = TaskSpec(session_id=original.session_id, request='A组均值', file_ids=[fid])
    store.create_task(task)
    runtime = TaskRuntime(task, store, Mock(), lambda: False)
    tool = next(tool for tool in analyst_tools(runtime) if tool.name=='answer_metric')
    result = tool.invoke({'file_id':fid,'column':'pH','aggregation':'mean','filters':[{'column':'group','op':'eq','value':'A'}]})
    assert not result['reused'] and result['value']==3.1


def test_single_answer_tool_cannot_short_circuit_a_report(metric_context):
    store, task, fid = metric_context
    task.request='生成pH报告'
    runtime = TaskRuntime(task, store, Mock(), lambda: False)
    tool = next(tool for tool in analyst_tools(runtime) if tool.name=='answer_metric')
    result = tool.invoke({'file_id':fid,'column':'pH','aggregation':'mean'})
    assert result['status']=='error' and runtime.direct_metric_answer is None and not runtime.analyses


def test_parallel_scalar_tools_for_multiple_metrics_do_not_drop_second_metric(metric_context):
    store, task, fid = metric_context
    task.request='pH均值和最大值'
    batch=AIMessage(content='',tool_calls=[
        {'name':'answer_metric','args':{'file_id':fid,'column':'pH','aggregation':'mean'},'id':'mean'},
        {'name':'answer_metric','args':{'file_id':fid,'column':'pH','aggregation':'max'},'id':'max'}])
    _, factory=models([call('task',{'subagent_type':'analyst','description':'Mean and max pH.'}),
                       call('finish_task', {'intent':'analysis','answer':'已计算请求的均值和最大值。'}),AIMessage(content='done')],
                      analyst=[batch,AIMessage(content='Use both registered analyses.')])
    result=run_task(task,store,Mock(),lambda:False,model_factory=factory)
    assert result['status']=='completed' and len(result['analysis_ids'])==2 and 'metric_answer' not in result
    values={key:value for aid in result['analysis_ids'] for key,value in store.get_analysis(aid,task.session_id).metrics.items()}
    assert values['pH_mean']==3.2 and values['pH_max']==3.4


def test_finish_task_only_displays_selected_verified_metrics(metric_context):
    store, original, fid=metric_context
    a=DataService(store).analyze(original,fid,'aggregate',aggregations={'pH':['count','mean','median']})
    store.update_task(original.task_id,status='succeeded',result={'analysis_ids':[a.analysis_id]})
    task=TaskSpec(session_id=original.session_id,request='只告诉我均值',file_ids=[fid])
    store.create_task(task)
    scripted,factory=models([call('finish_task',{'intent':'analysis','answer':'忽略未绑定数字999。',
        'metric_refs':[{'analysis_id':a.analysis_id,'metric_key':'pH_mean','label':'pH平均值'}]})])
    result=run_task(task,store,Mock(),lambda:False,model_factory=factory)
    assert result['status']=='completed' and result['final_response']=='pH平均值：**3.2**。'
    assert scripted['main']._index==1


def test_finish_reference_cannot_smuggle_unbound_numbers_through_label(metric_context):
    from src.agents import main_tools
    store,task,fid=metric_context
    a=DataService(store).analyze(task,fid,'aggregate',aggregations={'pH':'mean'})
    task.analysis_ids=[a.analysis_id]
    runtime=TaskRuntime(task,store,Mock(),lambda:False)
    finish=next(tool for tool in main_tools(runtime) if tool.name=='finish_task')
    result=finish.invoke({'intent':'analysis','answer':'done',
        'metric_refs':[{'analysis_id':a.analysis_id,'metric_key':'pH_mean','label':'均值为999'}]})
    assert result['status']=='error' and result['code']=='invalid_answer_label'
    assert not runtime.answer_metric_refs
