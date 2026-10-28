"""Offline evidence for fewer calls, real role execution, stable edits and streaming."""
from pathlib import Path
import hashlib
import json
from unittest.mock import Mock
import pytest

from langchain_core.messages import AIMessage
from src.agents import TaskRuntime, build_agents, main_tools, report_tools, run_task
from src.agent_optimization import deterministic_summary, tool_chars
from src.data import DataService
from test_agents import context, models, call
from test_general_revision_runtime import report_context


def test_scalar_one_coordinator_call_analyst_executor_and_no_extra_metrics(context):
    store, task, file = context
    seen, factory = models([call('query_metric',dict(file_id=file['file_id'],column='amount',aggregation='mean'))])
    events = lambda kind,message='',**data: store.add_event(task.task_id,kind,message,**data)
    result = run_task(task,store,events,lambda:False,model_factory=factory)
    assert result['status']=='completed',result
    assert result['metric_answer']['value']==20
    assert seen['main']._index==1 and seen['analyst']._index==0
    analysis=store.get_analysis(result['analysis_ids'][0],task.session_id)
    assert set(analysis.metrics)-{'source_rows','result_rows','output_rows'}=={'amount_mean'}
    log=store.list_events(task.task_id)
    assert any(e['type']=='agent_assigned' and e['target']=='analyst' for e in log)
    assert any(e['type']=='tool' and e['role']=='analyst' and e['tool']=='answer_metric' for e in log)
    assert 'source_rows' not in result['final_response']


def test_single_metric_does_not_grant_unselected_file(context):
    store,task,file=context
    task.file_ids=[]
    runtime=TaskRuntime(task,store,Mock(),lambda:False)
    query=next(t for t in main_tools(runtime) if t.name=='query_metric')
    result=query.invoke(dict(file_id=file['file_id'],column='amount',aggregation='mean'))
    assert result['status']=='error' and result['code']=='file_not_selected'
    assert not runtime.analyses


def test_native_lazy_graphs_and_one_summarizer(context, monkeypatch):
    import src.agents as implementation
    native_create=implementation.create_deep_agent
    stacks=[]
    def capture(**kwargs):
        stacks.append(kwargs['middleware'])
        return native_create(**kwargs)
    monkeypatch.setattr(implementation,'create_deep_agent',capture)
    store,task,_=context
    constructed=[]
    scripted,factory=models([AIMessage(content='hello')])
    def counted(settings,role):
        constructed.append(role)
        return factory(settings,role)
    runtime=TaskRuntime(task,store,Mock(),lambda:False)
    agents=build_agents(runtime,counted)
    assert constructed==[]
    graph=agents['main']
    assert constructed==['main']
    assert sum(m.name=='SummarizationMiddleware' for m in stacks[0])==1
    # Deep Agents merges by exact middleware name, replacing its native default.
    from deepagents.graph import _apply_custom_middleware
    active=_apply_custom_middleware(stacks[0],stacks[0])
    assert sum(m.name=='SummarizationMiddleware' for m in active)==1
    assert agents['main'] is graph
    build_agents(TaskRuntime(task,store,Mock(),lambda:False),counted)['main']
    assert constructed==['main','main']  # no TaskRuntime closure reused


def test_typed_report_supplement_no_paraphrase_or_main_handoff_calls(context):
    store,task,file=context
    initial=DataService(store).analyze(task,file['file_id'],'aggregate',aggregations={'amount':'mean'})
    task.analysis_ids=[initial.analysis_id]
    task.request='生成含amount最大值的HTML报告'
    task.expected_outputs=['html']
    newest=lambda: next(a for a in store._index('analyses') if a!=initial.analysis_id)
    scripted,factory=models([call('perform_task',dict(kind='report',request=task.request,analysis_ids=[initial.analysis_id]))],
        analyst=[call('analyze_dataset',dict(file_id=file['file_id'],operation='aggregate',aggregations={'amount':'max'})),AIMessage(content='analysis_ready')],
        report=[call('request_analysis',dict(request='只补amount最大值')),
                lambda _:call('create_report',{'options':{'analysis_ids':[newest()],'formats':['html']}})])
    result=run_task(task,store,Mock(),lambda:False,model_factory=factory)
    assert result['status']=='completed' and result['report_ids'],result
    assert scripted['main']._index==1
    assert scripted['report_designer']._index==2
    assert scripted['analyst']._index==2


def test_ordinary_report_schema_avoids_two_large_parameter_trees(context):
    store,task,_=context
    runtime=TaskRuntime(task,store,Mock(),lambda:False)
    tools=report_tools(runtime)
    old=[t for t in tools if t.name in {'render_report','revise_report'}]
    new=[t for t in tools if t.name in {'create_report','apply_report_edit'}]
    assert tool_chars(new)<tool_chars(old)*0.3


def test_summary_checkpoint_has_exact_original_request_and_pinned_revision(context):
    store,task,_=context
    task.request='仅修改指定图表，金额口径按订单去重，保留全部其他章节'
    runtime=TaskRuntime(task,store,Mock(),lambda:False)
    runtime.revision_state={'base_report_id':'a'*32,'base_report_hash':'b'*64,'targets':['chart:exact-id'],
        'base_analysis_ids':[],'supplemental_analysis_ids':[]}
    # No model factory or summarizer model invoked by checkpoint construction.
    checkpoint=json.loads(deterministic_summary(runtime,'report_designer',[]))
    assert checkpoint['request']==task.request
    assert checkpoint['current_state']['report_revision_plan']['base_report_hash']=='b'*64


def test_optimizer_pinned_upstream_and_adapter_hashes():
    root=Path(__file__).resolve().parents[1]
    manifest=json.loads((root/'skills/token-optimization-manifest.json').read_text(encoding='utf-8'))
    assert manifest['upstream']['commit']=='6f1504cfc0e39fed36f6d7405907231d6474c01d'
    for item in manifest['files']:
        assert hashlib.sha256((root/item['path']).read_bytes()).hexdigest()==item['sha256']
    for role in ('main','analyst','report_designer'):
        assert (root/f'skills/{role}/token-optimization/SKILL.md').stat().st_size<2000


def test_incremental_prose_edit_one_designer_call_preserves_old_report(report_context):
    from src.storage import file_hash
    store,task,_,chart,mean,base=report_context
    task.request='把Recommendations这一节缩短些，其他保持不变'
    prior_hash=file_hash(store.resolve_artifact(base.report_ids[0]))
    scripted,factory=models([call('perform_task',{'kind':'report','request':task.request})],
        report=[call('apply_report_edit',{'options':{'section_edits':[{'section_heading':'Recommendations','text':'Act cautiously.'}]}})])
    result=run_task(task,store,Mock(),lambda:False,model_factory=factory)
    assert result['status']=='completed',result
    assert scripted['main']._index==1 and scripted['report_designer']._index==1 and scripted['analyst']._index==0
    assert file_hash(store.resolve_artifact(base.report_ids[0]))==prior_hash
    assert result['report_ids'][0]!=base.report_ids[0]
    revision=result['report_revision']
    assert revision['revision_integrity']['changed_targets']==['section:Recommendations']


def test_successful_same_parameter_analysis_deduplicates_but_new_args_execute(context):
    store,task,file=context
    task.request='统计amount均值和最大值'
    events=lambda kind,message='',**data:store.add_event(task.task_id,kind,message,**data)
    same={'file_id':file['file_id'],'operation':'aggregate','aggregations':{'amount':'mean'}}
    scripted,factory=models([call('perform_task',{'kind':'analysis','request':task.request}),
                            call('finish_task',{'intent':'analysis','answer':''})],
        analyst=[call('analyze_dataset',same,'a'),call('analyze_dataset',same,'b'),
                 call('analyze_dataset',{**same,'aggregations':{'amount':'max'}},'c'),AIMessage(content='done')])
    result=run_task(task,store,events,lambda:False,model_factory=factory)
    assert result['status']=='completed',result
    assert len(result['analysis_ids'])==2
    assert any(e['type']=='tool_result_reused' for e in store.list_events(task.task_id))


def test_same_invalid_call_stops_without_spending_full_budget(context):
    store,task,file=context
    args={'file_id':file['file_id'],'operation':'aggregate','aggregations':{'absent':'mean'}}
    scripted,factory=models([call('perform_task',{'kind':'analysis','request':task.request})],
        analyst=[call('analyze_dataset',args,str(i)) for i in range(8)])
    result=run_task(task,store,Mock(),lambda:False,model_factory=factory)
    assert result['status']=='failed'
    assert '无进展循环' in result['final_response']
    assert result['tool_calls']<store.settings.max_tool_calls and not result['analysis_ids']


def test_sql_metric_compiler_keeps_selection_and_parameter_boundaries(context, monkeypatch):
    import src.agents as implementation
    store,task,_=context
    task.database_source_ids=['selected']
    task.database_tables={'selected':['orders']}
    runtime=TaskRuntime(task,store,Mock(),lambda:False)
    runtime.database.catalog=Mock(return_value={'tables':[
        {'name':'orders','qualified_name':'olist.orders','columns':[{'name':'order_status'}]},
        {'name':'secret','qualified_name':'olist.secret','columns':[]}]})
    captured=[]
    def executor(runtime,name,args):
        captured.append((name,args))
        return {'status':'error','code':'offline_sql_intercept'}
    monkeypatch.setattr(implementation,'_analyst_executor',executor)
    query=next(t for t in main_tools(runtime) if t.name=='query_database_metric')
    literal="delivered' OR 1=1 --"
    result=query.invoke({'source_id':'selected','table':'orders','aggregation':'count',
        'filters':[{'column':'order_status','op':'eq','value':literal}]})
    assert result['code']=='offline_sql_intercept'
    name,args=captured[0]
    assert name=='run_sql' and args['params']==[literal]
    assert args['sql']=='SELECT COUNT(*) AS requested_value FROM "olist"."orders" WHERE "order_status" = $1'
    denied=query.invoke({'source_id':'selected','table':'secret','aggregation':'count'})
    assert denied['code']=='database_table_not_allowed' and len(captured)==1
    denied=query.invoke({'source_id':'not-selected','table':'orders','aggregation':'count'})
    assert denied['code']=='database_source_not_selected' and len(captured)==1


def test_native_long_context_offload_uses_checkpoint_not_extra_llm(context):
    from langchain_core.messages import HumanMessage
    store,task,_=context
    events=lambda kind,message='',**data:store.add_event(task.task_id,kind,message,**data)
    scripted,factory=models([AIMessage(content='done')])
    runtime=TaskRuntime(task,store,events,lambda:False)
    graph=build_agents(runtime,factory)['main']
    messages=[HumanMessage(content='old trace observation '+('data '*2400)) for _ in range(16)]
    graph.invoke({'messages':messages})
    assert scripted['main']._index==1
    compacted=[e for e in store.list_events(task.task_id) if e['type']=='context_compacted']
    assert len(compacted)==1 and compacted[0]['added_model_calls']==0
    seen=str(scripted['main']._seen[0])
    assert task.request in seen and 'full conversation history has been saved to' in seen


def test_multimetric_workflow_keeps_all_requested_operations_until_explicit_ready(context):
    store,task,file=context
    task.request='计算amount均值和最大值，只给结论'
    def ready(_):
        analyses=[store.get_analysis(aid,task.session_id) for aid in store._index('analyses')]
        refs=[{'analysis_id':a.analysis_id,'metric_key':key,'label':key}
              for a in analyses for key in ('amount_mean','amount_max') if key in a.metrics]
        assert len(refs)==2
        return call('finish_analysis',{'metric_refs':refs})
    scripted,factory=models([call('perform_task',{'kind':'analysis','request':task.request})],
        analyst=[call('analyze_dataset',{'file_id':file['file_id'],'operation':'aggregate','aggregations':{'amount':'mean'}}),
                 call('analyze_dataset',{'file_id':file['file_id'],'operation':'aggregate','aggregations':{'amount':'max'}}),ready])
    result=run_task(task,store,Mock(),lambda:False,model_factory=factory)
    assert result['status']=='completed' and len(result['analysis_ids'])==2,result
    assert '20' in result['final_response'] and '30' in result['final_response']
    assert scripted['main']._index==1 and scripted['analyst']._index==3


def test_main_file_scalar_batch_returns_all_requested_values(context):
    store,task,file=context
    task.request='计算amount均值和最大值，只给结论'
    calls=[{'name':'query_metric','id':op,'args':{'file_id':file['file_id'],'column':'amount','aggregation':op}}
           for op in ('mean','max')]
    scripted,factory=models([AIMessage(content='',tool_calls=calls)])
    result=run_task(task,store,Mock(),lambda:False,model_factory=factory)
    assert result['status']=='completed' and len(result['analysis_ids'])==2,result
    assert '20' in result['final_response'] and '30' in result['final_response']
    assert 'metric_answer' not in result and scripted['main']._index==1


def _fake_database_scalar(context,monkeypatch,*,table='orders',column='amount'):
    import src.agents as implementation
    from src.schemas import AnalysisResult
    store,task,file=context
    task.database_source_ids=['selected']
    task.database_tables={'selected':[table]}
    monkeypatch.setattr(implementation.DatabaseService,'catalog',lambda self,source:{'tables':[
        {'name':table,'qualified_name':'olist.'+table,'columns':[{'name':column}]}]})
    native=implementation._analyst_executor
    sql_calls=[]
    def executor(runtime,name,args):
        if name!='run_sql': return native(runtime,name,args)
        # Compiler/batch fixture only. Database snapshot/RLS/source contracts
        # are independently exercised by the DB integration test suite.
        sql_calls.append(args)
        value=3 if 'COUNT(' in args['sql'] else 60
        analysis=runtime.store.save_analysis(AnalysisResult(task_id=task.task_id,session_id=task.session_id,
            input_versions={file['file_id']:file['hash']},metrics={'requested_value':value},
            metric_definitions={'requested_value':{'operation':'aggregate'}}))
        return {'items':[runtime.record_analysis(analysis)]}
    monkeypatch.setattr(implementation,'_analyst_executor',executor)
    return sql_calls


def test_main_database_scalar_batch_keeps_both_verified_refs(context,monkeypatch):
    store,task,_=context
    task.request='查询orders行数与amount总和'
    sql=_fake_database_scalar(context,monkeypatch)
    calls=[{'name':'query_database_metric','id':'count','args':{'source_id':'selected','table':'orders','aggregation':'count'}},
           {'name':'query_database_metric','id':'sum','args':{'source_id':'selected','table':'orders','aggregation':'sum','column':'amount'}}]
    scripted,factory=models([AIMessage(content='',tool_calls=calls)])
    result=run_task(task,store,Mock(),lambda:False,model_factory=factory)
    assert result['status']=='completed' and len(result['analysis_ids'])==2,result
    assert '**3**' in result['final_response'] and '**60**' in result['final_response']
    assert len(sql)==2 and scripted['main']._index==1


def test_main_mixed_file_database_scalar_batch_preserves_both_sources(context,monkeypatch):
    store,task,file=context
    task.request='文件amount均值和数据库orders行数'
    sql=_fake_database_scalar(context,monkeypatch)
    calls=[{'name':'query_metric','id':'file','args':{'file_id':file['file_id'],'column':'amount','aggregation':'mean'}},
           {'name':'query_database_metric','id':'db','args':{'source_id':'selected','table':'orders','aggregation':'count'}}]
    scripted,factory=models([AIMessage(content='',tool_calls=calls)])
    result=run_task(task,store,Mock(),lambda:False,model_factory=factory)
    assert result['status']=='completed' and len(result['analysis_ids'])==2,result
    assert '**20.0**' in result['final_response'] and '**3**' in result['final_response']
    assert len(sql)==1 and scripted['main']._index==1


def test_scalar_mixed_with_complex_work_delegates_original_goal_once(context):
    store,task,file=context
    task.request='计算amount均值和最大值，只给结论'
    calls=[{'name':'query_metric','id':'scalar','args':{'file_id':file['file_id'],'column':'amount','aggregation':'mean'}},
           {'name':'perform_task','id':'complex','args':{'kind':'analysis','request':'maximum only'}}]
    def finish(_):
        aid=next(iter(store._index('analyses')))
        return call('finish_analysis',{'metric_refs':[{'analysis_id':aid,'metric_key':'amount_'+op} for op in ('mean','max')]})
    scripted,factory=models([AIMessage(content='',tool_calls=calls)],analyst=[
        call('analyze_dataset',{'file_id':file['file_id'],'operation':'aggregate','aggregations':{'amount':['mean','max']}}),finish])
    result=run_task(task,store,Mock(),lambda:False,model_factory=factory)
    assert result['status']=='completed',result
    assert '20' in result['final_response'] and '30' in result['final_response']
    assert len(result['analysis_ids'])==1 and scripted['analyst']._index==2
    assert task.request in str(scripted['analyst']._seen[0])


def test_scalar_batch_failure_does_not_deliver_successful_subset(context):
    store,task,file=context
    task.request='计算amount均值和absent最大值'
    calls=[{'name':'query_metric','id':'good','args':{'file_id':file['file_id'],'column':'amount','aggregation':'mean'}},
           {'name':'query_metric','id':'bad','args':{'file_id':file['file_id'],'column':'absent','aggregation':'max'}}]
    scripted,factory=models([AIMessage(content='',tool_calls=calls),AIMessage(content='done')],
        analyst=[AIMessage(content='no more facts')])
    result=run_task(task,store,Mock(),lambda:False,model_factory=factory)
    assert result['status']=='failed' and len(result['analysis_ids'])==1,result
    assert not result.get('metric_answer')


def test_scalar_batch_repair_reuses_success_and_completes_entire_original_goal(context):
    store,task,file=context
    task.request='计算amount均值和最大值'
    calls=[{'name':'query_metric','id':'good','args':{'file_id':file['file_id'],'column':'amount','aggregation':'mean'}},
           {'name':'query_metric','id':'typo','args':{'file_id':file['file_id'],'column':'amont','aggregation':'max'}}]
    def finish(_):
        analyses=[store.get_analysis(aid,task.session_id) for aid in store._index('analyses')]
        refs=[{'analysis_id':a.analysis_id,'metric_key':key} for a in analyses
              for key in ('amount_mean','amount_max') if key in a.metrics]
        return call('finish_analysis',{'metric_refs':refs})
    scripted,factory=models([AIMessage(content='',tool_calls=calls)],analyst=[
        call('analyze_dataset',{'file_id':file['file_id'],'operation':'aggregate','aggregations':{'amount':'max'}}),finish])
    result=run_task(task,store,Mock(),lambda:False,model_factory=factory)
    assert result['status']=='completed' and len(result['analysis_ids'])==2,result
    assert '20' in result['final_response'] and '30' in result['final_response']
    assert scripted['main']._index==1 and scripted['analyst']._index==2
    assert task.request in str(scripted['analyst']._seen[0])


@pytest.mark.parametrize('filename',['sales2024.csv','二〇二四销售.csv','销售一百万元.csv'])
def test_batch_numeric_filename_is_trusted_identity_not_freeform_number(context,filename):
    store,task,_=context
    path=store.root/'KB'/filename
    path.write_text('amount\n10\n20\n30\n',encoding='utf-8')
    file=store.register_file(path,path.name,None,source='kb')
    task.file_ids=[file['file_id']]
    task.request='计算amount均值和最大值'
    store.update_task(task.task_id,spec=task.model_dump())
    calls=[{'name':'query_metric','id':op,'args':{'file_id':file['file_id'],'column':'amount','aggregation':op}}
           for op in ('mean','max')]
    scripted,factory=models([AIMessage(content='',tool_calls=calls)])
    result=run_task(task,store,Mock(),lambda:False,model_factory=factory)
    assert result['status']=='completed',result
    assert '20' in result['final_response'] and '30' in result['final_response']
    assert filename[:-4] in result['final_response']
    assert scripted['main']._index==1 and scripted['analyst']._index==0


def test_batch_same_metric_from_numeric_filenames_remains_distinguishable(context):
    store,task,_=context
    files=[]
    for name,value in [('销售2023.csv',10),('销售2024.csv',30)]:
        path=store.root/'KB'/name
        path.write_text('amount\n'+str(value)+'\n',encoding='utf-8')
        files.append(store.register_file(path,path.name,None,source='kb'))
    task.file_ids=[f['file_id'] for f in files]
    task.request='计算两份文件各自amount均值'
    store.update_task(task.task_id,spec=task.model_dump())
    calls=[{'name':'query_metric','id':str(i),'args':{'file_id':f['file_id'],'column':'amount','aggregation':'mean'}}
           for i,f in enumerate(files)]
    scripted,factory=models([AIMessage(content='',tool_calls=calls)])
    result=run_task(task,store,Mock(),lambda:False,model_factory=factory)
    assert result['status']=='completed',result
    assert '销售2023' in result['final_response'] and '销售2024' in result['final_response']
    assert '**10.0**' in result['final_response'] and '**30.0**' in result['final_response']
    assert scripted['main']._index==1 and scripted['analyst']._index==0


def test_sql_numeric_table_and_column_names_use_only_catalog_labels(context,monkeypatch):
    from src.answers import validate_refs
    from src.schemas import ReportMetricReference
    from src.storage import StoreError
    store,task,_=context
    task.request='查询orders2024行数与amount2024总和'
    _fake_database_scalar(context,monkeypatch,table='orders2024',column='amount2024')
    calls=[{'name':'query_database_metric','id':'count','args':{'source_id':'selected','table':'orders2024','aggregation':'count'}},
           {'name':'query_database_metric','id':'sum','args':{'source_id':'selected','table':'orders2024','column':'amount2024','aggregation':'sum'}}]
    scripted,factory=models([AIMessage(content='',tool_calls=calls)])
    result=run_task(task,store,Mock(),lambda:False,model_factory=factory)
    assert result['status']=='completed',result
    assert 'orders2024' in result['final_response'] and 'amount2024' in result['final_response']
    assert '**3**' in result['final_response'] and '**60**' in result['final_response']
    runtime=TaskRuntime(task,store,Mock(),lambda:False)
    aid=result['analysis_ids'][0]
    runtime.authorize_analysis(aid)
    with pytest.raises(StoreError) as error:
        validate_refs(runtime,[ReportMetricReference(analysis_id=aid,metric_key='requested_value',label='虚构收益2024')])
    assert error.value.code=='invalid_answer_label'


def test_revision_derives_only_referenced_supplements_and_pins_summary_before_analysis(report_context):
    from src.storage import file_hash
    store,task,fid,chart,mean,base=report_context
    task.request='将成交额图改为折线图，同步更新结论中对应的成交额合计；其余图表和章节保持原样'
    runtime=TaskRuntime(task,store,Mock(),lambda:False)
    original_hash=file_hash(store.resolve_artifact(base.report_ids[0]))
    tools={t.name:t for t in report_tools(runtime)}
    target='chart:'+chart.chart_ids[0]
    prepared=tools['prepare_report_revision'].invoke({'targets':[target,'findings']})
    assert set(prepared['revision_plan']['targets'])=={target,'summary'}
    runtime.request_supplement('仅补目标数据和图表')
    data=DataService(store)
    scalar=data.analyze(task,fid,'aggregate',aggregations={'amount':'sum'})
    intermediate=data.analyze(task,fid,'aggregate',group_by=['month'],aggregations={'amount':'sum'})
    line=data.analyze(task,fid,'aggregate',group_by=['month'],aggregations={'amount':'sum'},chart='line')
    for analysis in (scalar,intermediate,line):runtime.record_analysis(analysis)
    result=tools['apply_report_edit'].invoke({'options':{
        'supplemental_analysis_ids':[scalar.analysis_id,intermediate.analysis_id,line.analysis_id],
        'revision_patch':{'summary':'成交额合计为{{metric:'+scalar.analysis_id+':amount_sum}}。',
            'components':[{'kind':'chart','operation':'replace','target_id':chart.chart_ids[0],
                'artifact_id':line.chart_ids[0],'analysis_id':line.analysis_id}]}}})
    assert result['status']=='completed',result
    assert set(result['analysis_ids'])=={chart.analysis_id,mean.analysis_id,scalar.analysis_id,line.analysis_id}
    assert intermediate.analysis_id not in result['analysis_ids']
    assert file_hash(store.resolve_artifact(base.report_ids[0]))==original_hash
    manifest=json.loads(store.resolve_artifact(result['validation_summary']['manifest_id']).read_text(encoding='utf-8'))
    assert manifest['presentation']['sections']==runtime.report_capsule['presentation']['sections']
    assert manifest['presentation']['findings']==runtime.report_capsule['presentation']['findings']
    assert set(result['validation_summary']['revision_integrity']['changed_targets'])=={'charts:'+chart.chart_ids[0],'summary'}
    rejected=tools['prepare_report_revision'].invoke({'targets':[target,'summary','findings']})
    assert rejected['code']=='revision_plan_changed'  # no post-pin expansion


def test_semantic_error_guard_stops_reworded_patch_without_new_facts(report_context):
    store,task,fid,chart,_,base=report_context
    extra=DataService(store).analyze(task,fid,'aggregate',aggregations={'amount':'sum'})
    task.analysis_ids=[extra.analysis_id]
    patch={'components':[{'kind':'chart','operation':'replace','target_id':chart.chart_ids[0],
                          'artifact_id':chart.chart_ids[0],'analysis_id':chart.analysis_id}]}
    reports=[call('revise_report',{'supplemental_analysis_ids':[extra.analysis_id],
                'revision_patch':patch,'narrative':{'summary':text}},str(i))
             for i,text in enumerate(('Alpha prose','Beta prose','Gamma prose','Delta prose','Fifth prose'))]
    scripted,factory=models([call('task',{'subagent_type':'report_designer','description':task.request})],report=reports)
    result=run_task(task,store,Mock(),lambda:False,model_factory=factory)
    assert result['status']=='failed',result
    assert '同一原因' in result['final_response'] and 'report_source_mismatch' in result['final_response']
    assert scripted['report_designer']._index==4 and result['tool_calls']<store.settings.max_tool_calls
    assert not result['report_ids'] and store.resolve_artifact(base.report_ids[0]).is_file()


def test_summary_plan_keeps_an_explicit_findings_dependency(report_context):
    store,task,_,chart,_,_=report_context
    task.request='修改图表，并同步更新结论和发现'
    runtime=TaskRuntime(task,store,Mock(),lambda:False)
    base=runtime.prepare_revision(targets=['chart:'+chart.chart_ids[0],'findings'])
    assert set(base['revision_plan']['targets'])=={'chart:'+chart.chart_ids[0],'findings','summary'}


def test_distinct_validation_errors_under_same_code_can_be_corrected(report_context,monkeypatch):
    from src.reporting import ReportService
    from src.storage import StoreError
    store,task,_,_,mean,_=report_context
    task.request='生成一份新的HTML报告'
    task.analysis_ids=[mean.analysis_id]
    native=ReportService.generate
    attempts=[]
    def generate(self,*args,**kwargs):
        attempts.append(1)
        if len(attempts)<=4:
            raise StoreError(('Invalid layout switch','Invalid chart selector','Missing chapter heading','Invalid chart color')[len(attempts)-1],
                             422,'invalid_report_edit')
        return native(self,*args,**kwargs)
    monkeypatch.setattr(ReportService,'generate',generate)
    scripted,factory=models([call('perform_task',{'kind':'report','request':task.request})],
        report=[call('create_report',{'options':{'analysis_ids':[mean.analysis_id],'formats':['html'],'title':title}},str(i))
                for i,title in enumerate(('Alpha','Beta','Gamma','Delta','Epsilon'))])
    result=run_task(task,store,Mock(),lambda:False,model_factory=factory)
    assert result['status']=='completed' and len(attempts)==5,result
