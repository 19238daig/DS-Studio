"""Token-per-task regressions: fewer calls without weaker report/source checks."""
from unittest.mock import Mock
import json

import pytest

from src.agents import TaskRuntime, analyst_tools, main_tools, report_tools, run_task
from src.agent_optimization import standard_result_report
from src.data import DataService
from src.storage import file_hash
from test_agents import context, models, call
from test_database_core import db_setup, isolated_postgres


@pytest.mark.parametrize('question', ['生成报告', '请帮我生成HTML报告', '基于刚才的分析结果生成中文HTML和Excel报告', 'generate a report from the previous results'])
def test_plain_report_match_is_whole_request(question):
    assert standard_result_report(question)


@pytest.mark.parametrize('question', ['生成报告并新增地区分析', '生成英文报告', '生成含最大值的HTML报告', '生成报告，图表改为折线图', '把报告的结论删除', 'generate a report with new customer cohorts'])
def test_new_requirements_or_edits_cannot_use_formatting_shortcut(question):
    assert not standard_result_report(question)


def test_report_existing_results_one_main_no_specialist_models(context):
    store, task, file = context
    initial = DataService(store).analyze(task,file['file_id'],'aggregate',aggregations={'amount':'mean'})
    old_csv_hash = file_hash(store.resolve_artifact(initial.table_ids[0]))
    task.request='生成报告'
    task.expected_outputs=['html','xlsx']
    task.analysis_ids=[initial.analysis_id]
    scripted,factory=models([call('perform_task',{'kind':'report','request':task.request,'analysis_ids':[initial.analysis_id]})])
    result=run_task(task,store,lambda k,m='',**d:store.add_event(task.task_id,k,m,**d),lambda:False,model_factory=factory)
    assert result['status']=='completed',result
    assert result['analysis_ids']==[initial.analysis_id] and len(result['report_ids'])==2
    assert scripted['main']._index==1 and scripted['analyst']._index==0 and scripted['report_designer']._index==0
    assert file_hash(store.resolve_artifact(initial.table_ids[0]))==old_csv_hash
    html=next(store.resolve_artifact(aid).read_text(encoding='utf-8') for aid in result['report_ids'] if store.get_artifact(aid).name.endswith('.html'))
    assert '20' in html and '指标汇总</h2>' not in html and '结果明细</h2>' not in html
    assert any(e['type']=='workflow_fast_path' for e in store.list_events(task.task_id))


def test_report_executor_does_not_accept_tampered_registered_results(context):
    store,task,file=context
    initial=DataService(store).analyze(task,file['file_id'],'aggregate',aggregations={'amount':'mean'})
    task.request='生成报告';task.analysis_ids=[initial.analysis_id]
    runtime=TaskRuntime(task,store,Mock(),lambda:False)
    store.resolve_artifact(initial.table_ids[0]).write_text('amount_mean\n999\n',encoding='utf-8')
    perform=next(t for t in main_tools(runtime) if t.name=='perform_task')
    result=perform.invoke({'kind':'report','request':task.request,'analysis_ids':[initial.analysis_id]})
    assert result['status']=='error' and not runtime.reports


def test_empty_targets_on_new_report_is_not_a_missing_base_error(context):
    store,task,_=context
    task.request='生成满意度图表报告';task.expected_outputs=['html']
    runtime=TaskRuntime(task,store,Mock(),lambda:False)
    request=next(t for t in report_tools(runtime) if t.name=='request_analysis')
    result=request.invoke({'request':'补用户明确要求的评分图表','targets':[]})
    assert result['status']=='need_analysis' and not runtime.revision_state


def test_catalog_projection_cannot_widen_task_table_selection(context):
    store,task,_=context
    task.database_source_ids=['selected'];task.database_tables={'selected':['olist.orders']}
    runtime=TaskRuntime(task,store,Mock(),lambda:False)
    original={'tables':[{'name':'orders','qualified_name':'olist.orders','columns':[{'name':'order_id'}]},
                        {'name':'private','qualified_name':'olist.private','columns':[{'name':'secret'}]}]}
    runtime.database.catalog=Mock(return_value=original)
    catalog=next(t for t in analyst_tools(runtime) if t.name=='database_catalog')
    allowed=catalog.invoke({'source_id':'selected','table_names':['olist.orders']})
    assert [t['qualified_name'] for t in allowed['tables']]==['olist.orders']
    assert 'olist.private' not in allowed['visible_table_names']
    denied=catalog.invoke({'source_id':'selected','table_names':['olist.private']})
    assert denied['status']=='error' and denied['code']=='database_table_not_allowed'
    assert len(original['tables'])==2


def test_report_help_returns_one_schema_not_entire_manual(context):
    store,task,_=context
    runtime=TaskRuntime(task,store,Mock(),lambda:False)
    help_tool=next(t for t in report_tools(runtime) if t.name=='report_options')
    result=help_tool.invoke({'section':'layout'})
    assert result['section']=='layout' and 'include_charts' in result['schema']['properties']
    assert 'components' not in result['schema']['properties']
    assert len(json.dumps(result))<3000


@pytest.mark.parametrize("coordinator_subset", [False, True])
def test_plain_report_restores_whole_prior_result_bundle_when_main_omits_ids(context, coordinator_subset):
    from src.schemas import TaskSpec
    store,original,file=context
    mean=DataService(store).analyze(original,file['file_id'],'aggregate',aggregations={'amount':'mean'})
    maximum=DataService(store).analyze(original,file['file_id'],'aggregate',aggregations={'amount':'max'})
    store.update_task(original.task_id,status='succeeded',result={'analysis_ids':[mean.analysis_id,maximum.analysis_id],'report_ids':[]})
    task=TaskSpec(session_id=original.session_id,request='生成报告',file_ids=[file['file_id']],expected_outputs=['html'])
    store.create_task(task)
    scripted,factory=models([call('perform_task',{'kind':'report','request':task.request,**({'analysis_ids':[mean.analysis_id]} if coordinator_subset else {})})])
    result=run_task(task,store,Mock(),lambda:False,model_factory=factory)
    assert result['status']=='completed',result
    assert set(result['analysis_ids'])=={mean.analysis_id,maximum.analysis_id}
    assert scripted['analyst']._index==0 and scripted['report_designer']._index==0


def test_report_memory_keeps_specialist_model(context):
    from src.schemas import MemoryEntry
    store,task,file=context
    initial=DataService(store).analyze(task,file['file_id'],'aggregate',aggregations={'amount':'mean'})
    task.request='生成报告';task.analysis_ids=[initial.analysis_id];task.expected_outputs=['html']
    entry=MemoryEntry(owner='report_designer',content='Write report prose in English.',source_task_id=task.task_id,status='active',kind='preference')
    store.write_json('memory/report_designer/entries.json',[entry.model_dump()])
    scripted,factory=models([call('perform_task',{'kind':'report','request':task.request,'analysis_ids':[initial.analysis_id]})],
                            report=[call('create_report',{'options':{'analysis_ids':[initial.analysis_id],'formats':['html']}})])
    result=run_task(task,store,Mock(),lambda:False,model_factory=factory)
    assert result['status']=='completed',result
    assert scripted['report_designer']._index==1
    assert 'Write report prose in English' in str(scripted['report_designer']._seen[0])


def test_sql_answer_executes_and_binds_all_metrics_in_one_call(db_setup):
    from test_database_agents import task_runtime
    runtime=task_runtime(db_setup, request='????????????')
    query=next(t for t in analyst_tools(runtime) if t.name=='answer_sql')
    result=query.invoke({'source_id':'olist','queries':[{'sql':'SELECT COUNT(*) AS orders, MAX(order_id) AS maximum FROM olist.orders'}],
                         'metrics':[{'query_index':0,'metric_key':'orders'},{'query_index':0,'metric_key':'maximum'}]})
    assert result['status']=='analysis_ready',result
    assert runtime.analysis_ready and runtime.finished_intent=='analysis'
    assert {ref.metric_key for ref in runtime.answer_metric_refs}=={'orders','maximum'}
    assert len(runtime.store._index('database_queries'))==1


def test_sql_answer_missing_metric_cannot_mark_partial_result_complete(db_setup):
    from test_database_agents import task_runtime
    runtime=task_runtime(db_setup)
    query=next(t for t in analyst_tools(runtime) if t.name=='answer_sql')
    result=query.invoke({'source_id':'olist','queries':[{'sql':'SELECT COUNT(*) AS orders FROM olist.orders'}],
                         'metrics':[{'query_index':0,'metric_key':'orders'},{'query_index':0,'metric_key':'missing_max'}]})
    assert result['status']=='error' and not runtime.analysis_ready and runtime.finished_intent is None


def test_numeric_finish_omits_prose_and_still_validates_refs(context):
    store,task,file=context
    initial=DataService(store).analyze(task,file['file_id'],'aggregate',aggregations={'amount':['mean','max']})
    task.request='mean and maximum amount'
    runtime=TaskRuntime(task,store,Mock(),lambda:False)
    runtime.record_analysis(initial)
    finish=next(t for t in main_tools(runtime) if t.name=='finish_task')
    refs=[{'analysis_id':initial.analysis_id,'metric_key':key} for key in ('amount_mean','amount_max')]
    result=finish.invoke({'intent':'analysis','metric_refs':refs})
    assert result['status']=='recorded',result
    assert {r.metric_key for r in runtime.answer_metric_refs}=={'amount_mean','amount_max'}
    assert not runtime.finished_answer
    bad=finish.invoke({'intent':'analysis','metric_refs':[{'analysis_id':initial.analysis_id,'metric_key':'missing'}]})
    assert bad['status']=='error' and bad['code']=='missing_metric'


def test_atomic_sql_cannot_omit_a_query_from_answer(db_setup):
    from test_database_agents import task_runtime
    runtime=task_runtime(db_setup)
    query=next(t for t in analyst_tools(runtime) if t.name=='answer_sql')
    result=query.invoke({'source_id':'olist','queries':[{'sql':'SELECT COUNT(*) AS orders FROM olist.orders'},
                         {'sql':'SELECT COUNT(*) AS orders FROM olist.orders'}],
                         'metrics':[{'query_index':0,'metric_key':'orders'}]})
    assert result['status']=='error' and result['code']=='incomplete_metric_batch'
    assert not runtime.store._index('database_queries') and not runtime.analysis_ready


def test_atomic_sql_cannot_turn_grouped_preview_into_numeric_answer(db_setup):
    from test_database_agents import task_runtime
    runtime=task_runtime(db_setup)
    query=next(t for t in analyst_tools(runtime) if t.name=='answer_sql')
    result=query.invoke({'source_id':'olist','queries':[{'sql':'SELECT order_id FROM olist.orders'}],
                         'metrics':[{'query_index':0,'metric_key':'result_rows'}]})
    assert result['status']=='error' and result['code']=='non_scalar_answer'
    assert not runtime.analysis_ready and runtime.finished_intent is None
