"""Prevent malformed analysis programs from consuming a whole report task."""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from langchain_core.messages import AIMessage
from langchain_core.messages import SystemMessage

from test_agents import context, models, call
from src.agents import TaskRuntime, analyst_tools, BoundaryMiddleware, run_task
from src.data import DataService
from src.execution import validate_analysis_script
from src.storage import StoreError


@pytest.mark.parametrize('code,reason',[
    ('print("summary")','missing_result_contract'),
    ('open("/output/result.json","w").write("unfinished','invalid_python')])
def test_bad_python_rejected_before_container_start(context, monkeypatch, code, reason):
    store, task, _ = context
    runtime = TaskRuntime(task,store,Mock(),lambda:False)
    execute = Mock(side_effect=AssertionError('Invalid script must not execute'))
    monkeypatch.setattr('src.agents.DockerExecutor.run_python',execute)
    result = next(t for t in analyst_tools(runtime) if t.name=='run_python').invoke({'code':code})
    assert result['code'] == reason and not execute.called


def test_static_contract_check_does_not_execute_any_user_code(tmp_path):
    destination = tmp_path/'must_not_exist'
    validate_analysis_script(f'open({str(destination)!r},"w").write("danger")\nresult="/output/result.json"')
    assert not destination.exists()


def test_three_failures_stop_all_further_model_and_tool_calls(context, monkeypatch):
    store,task,_ = context
    task.request, task.expected_outputs = '生成关系报告',['html']
    scripted,factory=models([call('task',{'subagent_type':'analyst','description':'分析关联并登记结果。'})],
        analyst=[call('run_python',{'code':'print("no result")'},f'bad{i}') for i in range(3)])
    docker = Mock(side_effect=AssertionError('No Docker for incomplete contract'))
    monkeypatch.setattr('src.agents.DockerExecutor.run_python',docker)
    event=Mock()
    result=run_task(task,store,event,lambda:False,model_factory=factory)
    assert result['status']=='failed' and result['tool_calls']==4
    assert 'result.json' in result['final_response'] and '工具调用限额' not in result['final_response']
    assert scripted['main']._index==1 and scripted['analyst']._index==3
    assert not docker.called and not result['report_ids']


def test_report_budget_handoff_preserves_last_slots_for_registered_delivery(context):
    store,task,file=context
    task.request,task.expected_outputs='生成分析报告',['html']
    store.settings.max_tool_calls=8
    ids=lambda:list(store._index('analyses'))
    scripted,factory=models([
        call('task',{'subagent_type':'analyst','description':'统计均值，交回报告。'}),
        lambda _:call('task',{'subagent_type':'report_designer','description':'使用已验证分析 '+ids()[0]})],
        analyst=[call('analyze_dataset',{'file_id':file['file_id'],'operation':'aggregate','aggregations':{'amount':'mean'}})],
        report=[lambda _:call('render_report',{'analysis_ids':ids(),'formats':['html']})])
    event=Mock()
    result=run_task(task,store,event,lambda:False,model_factory=factory)
    assert result['status']=='completed' and len(result['report_ids'])==1
    assert result['tool_calls']==4 and scripted['analyst']._index==1
    assert any(c.args[0]=='analysis_handoff' for c in event.call_args_list)


def test_expanded_default_budget_allows_more_than_old_32_calls(context):
    store,task,_=context
    task.request,task.file_ids='介绍功能',[]
    assert store.settings.max_tool_calls==256 and store.settings.max_agent_steps==1024
    assert store.settings.task_timeout_seconds==3600
    _,factory=models([*[call('file_catalog',{},f'lookup{i}') for i in range(35)],
        call('finish_task',{'intent':'general','answer':'支持分析和报告。'}),AIMessage(content='done')])
    result=run_task(task,store,Mock(),lambda:False,model_factory=factory)
    assert result['status']=='completed' and result['tool_calls']==36
