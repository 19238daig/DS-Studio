"""Failures found by the real three-agent acceptance, including missing tool turns."""
import hashlib
from pathlib import Path
from unittest.mock import Mock

import pytest
from langchain_core.messages import AIMessage

from test_agents import context, models, call
from src.agents import run_task
from src.data import DataService
from src.memory import MemoryService
from src.reporting import ReportService
from src.schemas import TaskSpec
from src.storage import StoreError


@pytest.mark.parametrize('declared', [False, True])
def test_negated_analysis_capability_is_general_even_with_selected_file(context, declared):
    store, task, _ = context
    task.request = '你能做哪些数据分析和报告工作？本次只介绍功能，不分析数据，也不生成报告。'
    messages = ([call('finish_task', {'intent':'general','answer':'支持分析和报告。'})] if declared else []) + [AIMessage(content='支持分析和报告。')]
    scripted, factory = models(messages)
    event = Mock()
    result = run_task(task, store, event, lambda:False, model_factory=factory)
    assert result['status'] == 'completed', result
    assert not result['analysis_ids'] and not result['report_ids']
    assert scripted['main']._index == len(messages)
    assert not any(c.args[0] == 'delivery_recovery' for c in event.call_args_list)


def test_negated_analysis_preference_is_activated(context):
    store, task, _ = context
    task.request = '请记住我的报告偏好：以后默认中文。本次只保存偏好，不分析任何数据，也不要生成报告。'
    _, factory = models([
        call('task', {'subagent_type':'report_designer','description':task.request}),
        call('finish_task', {'intent':'preference','answer':'已保存。'}), AIMessage(content='已保存。')],
        report=[call('memory_propose', {'content':task.request,'kind':'preference'}), AIMessage(content='已登记。')])
    result = run_task(task, store, Mock(), lambda:False, model_factory=factory)
    assert result['status'] == 'completed', result
    assert not result['analysis_ids'] and not result['report_ids']
    assert MemoryService(store).search('report_designer', '中文')[0]['status'] == 'active'


def test_known_cached_mean_binds_automatically_without_correction_or_reanalysis(context):
    store, task, file = context
    analysis = DataService(store).analyze(task, file['file_id'], 'aggregate', aggregations={'amount':'mean'})
    task.analysis_ids = [analysis.analysis_id]
    scripted, factory = models([call('finish_task', {'intent':'analysis','answer':'平均为999。'})])
    event = Mock()
    result = run_task(task, store, event, lambda:False, model_factory=factory)
    assert result['status'] == 'completed', result
    assert '20' in result['final_response'] and '999' not in result['final_response']
    assert result['analysis_ids'] == [analysis.analysis_id] and result['tool_calls'] == 1
    assert scripted['analyst']._index == 0
    assert scripted['main']._index == 1 and len(store._index('analyses')) == 1
    assert sum(c.args[0] == 'delivery_recovery' for c in event.call_args_list) == 0


def test_sidebar_report_formats_cannot_override_current_no_report_query(context):
    store, task, file = context
    analysis = DataService(store).analyze(task, file['file_id'], 'aggregate', aggregations={'amount':'mean'})
    task.analysis_ids, task.expected_outputs = [analysis.analysis_id], ['html','xlsx']
    task.request = 'amount平均值是多少？只回答这一项，不做报告。'
    scripted, factory = models([call('finish_task', {'intent':'analysis','answer':'已查询。'}), AIMessage(content='20'),
        call('finish_task', {'intent':'analysis','answer':'已查询。','metric_refs':[
            {'analysis_id':analysis.analysis_id,'metric_key':'amount_mean'}]})])
    result = run_task(task, store, Mock(), lambda:False, model_factory=factory)
    assert result['status'] == 'completed' and not result['report_ids'], result
    assert result['analysis_ids'] == [analysis.analysis_id]
    assert scripted['report_designer']._index == scripted['analyst']._index == 0


def test_empty_report_turn_recovers_through_real_report_tools(context):
    store, task, file = context
    analysis = DataService(store).analyze(task, file['file_id'], 'aggregate', aggregations={'amount':'mean'})
    task.analysis_ids, task.expected_outputs = [analysis.analysis_id], ['html']
    task.request = '复用刚才的分析结果生成报告，不重新分析。'
    scripted, factory = models([AIMessage(content='报告已生成。'),
        call('task', {'subagent_type':'report_designer','description':task.request})],
        report=[call('render_report', {'analysis_ids':[analysis.analysis_id],'formats':['html']})])
    result = run_task(task, store, Mock(), lambda:False, model_factory=factory)
    assert result['status'] == 'completed' and len(result['report_ids']) == 1, result
    assert scripted['analyst']._index == 0
    assert len(store._index('analyses')) == 1


def test_empty_translation_turn_recovers_without_overwriting_original(context):
    store, task, file = context
    analysis = DataService(store).analyze(task, file['file_id'], 'aggregate', aggregations={'amount':'mean'})
    original = ReportService(store).generate(task, [analysis.analysis_id], title='销售分析报告', formats=['html'],
        layout={'include_quality_and_limitations':False})
    rid = original.report_ids[0]
    path = store.resolve_artifact(rid)
    old_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    store.update_task(task.task_id,status='succeeded',result={'analysis_ids':[analysis.analysis_id],'report_ids':[rid]})
    edit = TaskSpec(session_id=task.session_id, request='把最新报告的标题翻译成英文 Sales Analysis Report，只改标题，不重新分析。')
    store.create_task(edit)
    scripted, factory = models([AIMessage(content='已翻译。'),
        call('task', {'subagent_type':'report_designer','description':edit.request})],
        report=[call('read_report', {'report_id':rid}), call('revise_report', {'report_id':rid,
            'text_replacements':[{'field':'title','original':'销售分析报告','replacement':'Sales Analysis Report'}]})])
    result = run_task(edit, store, Mock(), lambda:False, model_factory=factory)
    assert result['status'] == 'completed', result
    assert hashlib.sha256(path.read_bytes()).hexdigest() == old_hash
    store.update_task(edit.task_id, status='succeeded', result=result)
    new = ReportService(store).read_report(edit, result['report_ids'][0])
    assert new['presentation']['title'] == 'Sales Analysis Report'
    assert not new['presentation']['layout']['include_quality_and_limitations']
    assert scripted['analyst']._index == 0 and len(store._index('analyses')) == 1


def test_model_keeps_omitting_tools_fails_instead_of_waiting_for_user(context):
    store, task, _ = context
    event = Mock()
    scripted, factory = models([AIMessage(content='均值999'), AIMessage(content='还是999')])
    result = run_task(task, store, event, lambda:False, model_factory=factory)
    assert result['status'] == 'failed' and '999' not in result['final_response']
    assert scripted['main']._index == 2
    assert sum(c.args[0] == 'delivery_recovery' for c in event.call_args_list) == 1


def test_text_only_preference_confirmation_must_recover_and_save(context):
    store, task, _ = context
    task.request = '请记住报告偏好：默认中文，只保存偏好，不分析数据，不生成报告。'
    _, factory = models([AIMessage(content='已记住。'),
        call('task', {'subagent_type':'report_designer','description':task.request}),
        call('finish_task', {'intent':'preference','answer':'已保存。'}), AIMessage(content='已保存。')],
        report=[call('memory_propose', {'content':task.request,'kind':'preference'}), AIMessage(content='已登记。')])
    result = run_task(task, store, Mock(), lambda:False, model_factory=factory)
    assert result['status'] == 'completed', result
    assert MemoryService(store).search('report_designer','中文')


def test_real_long_result_name_mobile_has_local_table_scroll_only(context):
    playwright = pytest.importorskip('playwright.sync_api')
    edge = Path('C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe')
    if not edge.exists():
        pytest.skip('Edge unavailable')
    store, task, file = context
    analysis = DataService(store).analyze(task, file['file_id'], 'rows', chart='bar')
    index = store._index('artifacts')
    index[analysis.table_ids[0]]['name'] = 'result_' + 'long_unbroken_filename_' * 14 + '.csv'
    store._save_index('artifacts', index)
    report = ReportService(store).generate(task,[analysis.analysis_id],formats=['html'],layout={'include_tables':True},
        narrative={'sections':[{'heading':'LongUnbrokenSectionHeading'*15,'text':'来源已验证。',
            'metric_refs':[{'analysis_id':analysis.analysis_id,'metric_key':'source_rows','label':'完整来源行数'}]}]})
    assert report.status == 'completed'
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(executable_path=str(edge),headless=True)
        try:
            page = browser.new_page(viewport={'width':390,'height':844},offline=True)
            page.goto(store.resolve_artifact(report.report_ids[0]).as_uri())
            assert page.locator('h3').filter(has_text='long_unbroken_filename').count() == 1
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            assert page.locator('.scroll').count() >= 2
        finally:
            browser.close()


def test_old_report_style_can_be_upgraded_once_without_charts_or_content_changes(context, monkeypatch):
    import src.reporting as reporting
    store, task, file = context
    analysis = DataService(store).analyze(task,file['file_id'],'aggregate',aggregations={'amount':'mean'})
    service = ReportService(store)
    monkeypatch.setattr(reporting,'REPORT_STYLE_REVISION','old-style')
    original = service.generate(task,[analysis.analysis_id],formats=['html'],layout={'include_charts':False})
    store.update_task(task.task_id,status='succeeded',result=original.model_dump())
    path = store.resolve_artifact(original.report_ids[0])
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    monkeypatch.setattr(reporting,'REPORT_STYLE_REVISION','responsive-20261024')
    edit = TaskSpec(session_id=task.session_id,request='修复报告手机排版',analysis_ids=[analysis.analysis_id])
    store.create_task(edit)
    assert service.read_report(edit,original.report_ids[0])['needs_visual_upgrade']
    upgraded = service.generate(edit,[analysis.analysis_id],base_report_id=original.report_ids[0])
    assert upgraded.validation_summary['visual_upgrade']
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
    store.update_task(edit.task_id,status='succeeded',result=upgraded.model_dump())
    assert not service.read_report(edit,upgraded.report_ids[0])['needs_visual_upgrade']
    with pytest.raises(StoreError) as error:
        service.generate(edit,[analysis.analysis_id],base_report_id=upgraded.report_ids[0])
    assert error.value.code == 'report_unchanged'
