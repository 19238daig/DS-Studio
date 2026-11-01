"""Chat conclusions never turn a narrow question into a result dump."""
from unittest.mock import Mock

import pytest
from langchain_core.messages import AIMessage

from src.agents import TaskRuntime, _data_answer, _report_requested, analyst_tools, main_tools, run_task
from src.answers import bound_template, render_metrics
from src.config import Settings
from src.data import DataService
from src.schemas import AnalysisResult, ReportMetricReference, TaskSpec
from src.storage import Store, StoreError
from test_agents import call, models
from test_database_core import db_setup, isolated_postgres


@pytest.fixture
def chat(tmp_path):
    store = Store(Settings(workspace=tmp_path, _env_file=None))
    sid = store.new_session()['session_id']
    ids = []
    for name, values in [('red.csv', [3.1, 3.3, 3.5]), ('white.csv', [2.8, 3.0, 3.2])]:
        path = tmp_path/'KB'/name
        path.write_text('pH,alcohol\n' + '\n'.join(f'{value},12' for value in values) + '\n', encoding='utf-8')
        ids.append(store.register_file(path, name, None, source='kb')['file_id'])
    task = TaskSpec(session_id=sid, request='pH平均值是多少？', file_ids=ids)
    store.create_task(task)
    return store, task, ids


def tools(runtime, factory):
    return {tool.name: tool for tool in factory(runtime)}


def test_plain_mean_tool_calculates_no_incidental_aggregates_or_columns(chat):
    store, task, ids = chat
    runtime = TaskRuntime(task, store, Mock(), lambda: False)
    result = tools(runtime, analyst_tools)['analyze_dataset'].invoke({
        'file_id': ids[0], 'operation': 'aggregate',
        'aggregations': {'pH': ['mean', 'count', 'median', 'std'], 'alcohol': ['mean', 'std']}})
    assert result['metrics']['pH_mean'] == 3.3
    assert set(result['metrics']) == {'pH_mean', 'source_rows', 'result_rows'}
    assert result['metric_definitions']['pH_mean']['aggregations'] == {'pH': 'mean'}


def test_mean_question_cannot_run_full_descriptive_statistics(chat):
    store, task, ids = chat
    runtime = TaskRuntime(task, store, Mock(), lambda: False)
    result = tools(runtime, analyst_tools)['analyze_dataset'].invoke({'file_id': ids[0], 'operation': 'describe', 'columns': ['pH']})
    assert result['code'] == 'unrequested_descriptive_statistics'
    assert not store._index('analyses')


def test_legacy_full_analysis_gives_only_requested_mean_without_reading_table(chat, monkeypatch):
    store, task, ids = chat
    analysis = DataService(store).analyze(task, ids[0], 'aggregate', aggregations={'pH': ['mean', 'count', 'median']})
    runtime = TaskRuntime(task, store, Mock(), lambda: False)
    runtime.record_analysis(analysis)
    monkeypatch.setattr(runtime.data, 'read_result_table', Mock(side_effect=AssertionError('No chat preview')))
    answer = _data_answer(runtime, [analysis.analysis_id])
    assert answer == 'pH平均值：**3.3**。'
    assert all(word not in answer for word in ('数量', '中位数', 'source_rows', '口径', 'csv', '|'))


def test_finish_filters_incidental_explicit_refs_instead_of_dumping_them(chat):
    store, task, ids = chat
    analysis = DataService(store).analyze(task, ids[0], 'aggregate', aggregations={'pH': ['mean', 'count', 'median']})
    task.analysis_ids = [analysis.analysis_id]
    runtime = TaskRuntime(task, store, Mock(), lambda: False)
    result = tools(runtime, main_tools)['finish_task'].invoke({'intent': 'analysis', 'answer': '999', 'metric_refs': [
        {'analysis_id': analysis.analysis_id, 'metric_key': key} for key in analysis.metrics]})
    assert result['status'] == 'recorded'
    assert [ref.metric_key for ref in runtime.answer_metric_refs] == ['pH_mean']
    assert '999' not in _data_answer(runtime, [analysis.analysis_id])


def test_two_means_compare_only_same_measure_and_no_unrequested_stats(chat):
    store, task, ids = chat
    task.request = '红白的平均pH分别是多少，哪一种更高？'
    runtime = TaskRuntime(task, store, Mock(), lambda: False)
    analyses = [DataService(store).analyze(task, fid, 'aggregate', aggregations={'pH': ['mean', 'median', 'count']}) for fid in ids]
    for analysis in analyses:
        runtime.record_analysis(analysis)
    refs = [ReportMetricReference(analysis_id=analysis.analysis_id, metric_key='pH_mean', label=label)
            for analysis, label in zip(analyses, ['红葡萄酒pH均值', '白葡萄酒pH均值'])]
    runtime.answer_metric_refs = refs
    answer = _data_answer(runtime, [a.analysis_id for a in analyses])
    assert answer == '红葡萄酒pH均值（**3.3**）高于白葡萄酒pH均值（**3.0**）。'
    assert all(term not in answer for term in ('中位数', 'count', '标准差', '|', '口径', '预览'))


def test_explicit_detail_request_still_returns_requested_rows(chat):
    store, task, ids = chat
    task.request = '列出pH和alcohol的数据行，不需要额外统计'
    analysis = DataService(store).analyze(task, ids[0], 'rows', columns=['pH', 'alcohol'])
    runtime = TaskRuntime(task, store, Mock(), lambda: False)
    runtime.record_analysis(analysis)
    answer = _data_answer(runtime, [analysis.analysis_id])
    assert '| pH | alcohol |' in answer and '| 3.1 | 12 |' in answer
    assert 'source_rows' not in answer and '口径' not in answer


def test_negated_raw_data_does_not_open_preview(chat, monkeypatch):
    store, task, ids = chat
    task.request = 'pH均值，不要原始数据和计算过程'
    analysis = DataService(store).analyze(task, ids[0], 'aggregate', aggregations={'pH': 'mean'})
    runtime = TaskRuntime(task, store, Mock(), lambda: False)
    runtime.record_analysis(analysis)
    monkeypatch.setattr(runtime.data, 'read_result_table', Mock(side_effect=AssertionError('No raw data')))
    assert _data_answer(runtime, [analysis.analysis_id]) == 'pH平均值：**3.3**。'


@pytest.mark.parametrize('question', ['数据有多少行？', '数据行数是多少？', 'How many rows?', 'Show the number of rows.'])
def test_asked_row_count_is_business_answer_not_hidden_metadata(chat, question):
    store, task, ids = chat
    task.request = question
    analysis = DataService(store).analyze(task, ids[0], 'aggregate', aggregations={'pH': 'mean'})
    runtime = TaskRuntime(task, store, Mock(), lambda: False)
    runtime.record_analysis(analysis)
    assert _data_answer(runtime, [analysis.analysis_id]) == '数据行数：**3**。'


def test_unbound_answer_prose_falls_back_to_bound_metrics_without_looping(chat):
    """Prose carrying unbound numbers is dropped; verified metrics deliver."""
    store, task, ids = chat
    analysis = DataService(store).analyze(task, ids[0], 'aggregate', aggregations={'pH': 'mean'})
    task.analysis_ids = [analysis.analysis_id]
    token = '{{metric:' + analysis.analysis_id + ':pH_mean}}'
    scripted, factory = models([
        call('finish_task', {'intent': 'analysis', 'answer': '均值为' + token + '，另有999条记录。'})])
    result = run_task(task, store, Mock(), lambda: False, model_factory=factory)
    assert result['status'] == 'completed', result
    assert '3.3' in result['final_response'] and '999' not in result['final_response']
    assert len(store._index('analyses')) == 1
    assert scripted['analyst']._index == 0 and scripted['main']._index == 1


def test_template_cannot_include_incidental_metadata(chat):
    store, task, ids = chat
    analysis = DataService(store).analyze(task, ids[0], 'aggregate', aggregations={'pH': 'mean'})
    task.analysis_ids = [analysis.analysis_id]
    runtime = TaskRuntime(task, store, Mock(), lambda: False)
    result = tools(runtime, main_tools)['finish_task'].invoke({'intent': 'analysis', 'answer':
        '均值{{metric:' + analysis.analysis_id + ':pH_mean}}，行数{{metric:' + analysis.analysis_id + ':source_rows}}。'})
    assert result['code'] == 'unrelated_answer_metric'
    assert runtime.answer_template is None


def _year_analysis(store, task):
    """A run_python-style grouped analysis whose keys/definitions name the years."""
    grouped = AnalysisResult(task_id=task.task_id, session_id=task.session_id,
        metrics={'mean_va_2021': 24.95, 'mean_va_2022': 10.47, 'n_2021': 11, 'n_2022': 11},
        metric_definitions={'mean_va_2021': {'definition': '2021年增加值累计增幅（%）月度均值'},
                            'mean_va_2022': {'definition': '2022年增加值累计增幅（%）月度均值'},
                            'n_2021': {'definition': '2021年月度数据行数'},
                            'n_2022': {'definition': '2022年月度数据行数'}})
    return store.save_analysis(grouped)


def test_bound_answer_allows_year_identifiers_registered_by_analysis(chat):
    store, task, ids = chat
    task.request = '分析不同年份的差异，说明关键发现'
    grouped = _year_analysis(store, task)
    task.analysis_ids = [grouped.analysis_id]
    runtime = TaskRuntime(task, store, Mock(), lambda: False)
    token = '{{metric:' + grouped.analysis_id + ':mean_va_2021}}'
    template, refs = bound_template(runtime, '2021年最高，2022年回落，均值为' + token + '。')
    assert template == '2021年最高，2022年回落，均值为24.95。'
    assert [ref.metric_key for ref in refs] == ['mean_va_2021']


def test_bound_answer_still_rejects_unbound_statistics_between_identifiers(chat):
    store, task, ids = chat
    task.request = '分析不同年份的差异，说明关键发现'
    grouped = _year_analysis(store, task)
    task.analysis_ids = [grouped.analysis_id]
    runtime = TaskRuntime(task, store, Mock(), lambda: False)
    token = '{{metric:' + grouped.analysis_id + ':mean_va_2021}}'
    with pytest.raises(StoreError) as error:
        bound_template(runtime, '2021年比2022年高出14.48个百分点，均值为' + token + '。')
    assert error.value.code == 'unbound_answer'
    assert '14.48' in str(error.value)


def test_bound_answer_rejects_identifiers_absent_from_data(chat):
    store, task, ids = chat
    task.request = '分析不同年份的差异，说明关键发现'
    grouped = _year_analysis(store, task)
    task.analysis_ids = [grouped.analysis_id]
    runtime = TaskRuntime(task, store, Mock(), lambda: False)
    token = '{{metric:' + grouped.analysis_id + ':mean_va_2021}}'
    with pytest.raises(StoreError) as error:
        bound_template(runtime, '2019年起无明显差异，均值为' + token + '。')
    assert error.value.code == 'unbound_answer'


def test_finish_task_accepts_year_identifiers_in_conclusion(chat):
    store, task, ids = chat
    task.request = '分析不同年份的差异，说明关键发现'
    grouped = _year_analysis(store, task)
    task.analysis_ids = [grouped.analysis_id]
    runtime = TaskRuntime(task, store, Mock(), lambda: False)
    result = tools(runtime, main_tools)['finish_task'].invoke({'intent': 'analysis', 'answer':
        '2021年整体高于2022年，增加值均值为{{metric:' + grouped.analysis_id + ':mean_va_2021}}。'})
    assert result['status'] == 'recorded'
    assert runtime.answer_template == '2021年整体高于2022年，增加值均值为24.95。'


def test_repeated_general_declaration_ends_the_coordinator_loop(chat):
    """A main model stuck re-declaring finish_task must stop at the 2nd call."""
    store, task, ids = chat
    task.request = '你是谁？简单介绍一下你能做什么。'
    scripted, factory = models([
        call('finish_task', {'intent': 'general', 'answer': '说明一。'}),
        call('finish_task', {'intent': 'general', 'answer': '说明二。'}),
        AIMessage(content='不应到达这里。'),
    ])
    result = run_task(task, store, Mock(), lambda: False, model_factory=factory)
    assert result['status'] == 'completed', result
    assert result['final_response'] == '说明二。'
    assert scripted['main']._index == 2  # the canned terminal replaces the 3rd model call


def test_finish_analysis_unbound_answer_falls_back_to_automatic_binding(chat):
    store, task, ids = chat
    analysis = DataService(store).analyze(task, ids[0], 'aggregate', aggregations={'pH': 'mean'})
    runtime = TaskRuntime(task, store, Mock(), lambda: False)
    runtime.record_analysis(analysis)
    result = tools(runtime, analyst_tools)['finish_analysis'].invoke(
        {'answer': 'pH均值为3.3，另外999条记录。'})
    assert result['status'] == 'analysis_ready', result
    assert [ref.metric_key for ref in runtime.answer_metric_refs] == ['pH_mean']


def test_task_stage_tracks_the_active_phase(chat):
    store, task, ids = chat
    analysis = DataService(store).analyze(task, ids[0], 'aggregate', aggregations={'pH': 'mean'})
    task.analysis_ids = [analysis.analysis_id]
    seen = {}

    def delegate_probe(_):
        seen['main'] = store.get_task(task.task_id)['stage']
        return call('task', {'subagent_type': 'analyst', 'description': '查询pH均值'})

    def analyst_probe(_):
        seen['analyst'] = store.get_task(task.task_id)['stage']
        return AIMessage(content='已登记')

    scripted, factory = models([
        delegate_probe,
        AIMessage(content='已完成。'),
        call('finish_task', {'intent': 'analysis', 'metric_refs': [
            {'analysis_id': analysis.analysis_id, 'metric_key': 'pH_mean'}]}),
    ], analyst=[analyst_probe, AIMessage(content='已登记')])
    result = run_task(task, store, Mock(), lambda: False, model_factory=factory)
    assert result['status'] == 'completed', result
    assert seen == {'main': 'understanding', 'analyst': 'analysis'}


def test_two_different_means_are_not_compared_as_the_same_measure(chat):
    store, task, ids = chat
    task.request = '比较pH与alcohol的均值'
    analysis = DataService(store).analyze(task, ids[0], 'aggregate', aggregations={'pH': 'mean', 'alcohol': 'mean'})
    task.analysis_ids = [analysis.analysis_id]
    runtime = TaskRuntime(task, store, Mock(), lambda: False)
    refs = [ReportMetricReference(analysis_id=analysis.analysis_id, metric_key=key) for key in ['pH_mean', 'alcohol_mean']]
    answer = render_metrics(runtime, refs)
    assert 'pH平均值' in answer and 'alcohol平均值' in answer
    assert '高于' not in answer and '低于' not in answer


def test_sql_final_answer_uses_business_ref_no_query_row_metadata(db_setup):
    store, _, sid = db_setup
    task = TaskSpec(session_id=sid, request='订单数量是多少？', database_source_ids=['olist'])
    store.create_task(task)
    runtime = TaskRuntime(task, store, Mock(), lambda: False)
    result = tools(runtime, analyst_tools)['run_sql'].invoke({'source_id': 'olist',
        'sql': 'SELECT count(*) AS order_count, avg(price) AS avg_price FROM olist.orders'})
    aid = result['items'][0]['analysis_id']
    tools(runtime, main_tools)['finish_task'].invoke({'intent': 'analysis', 'answer': '已查询', 'metric_refs': [
        {'analysis_id': aid, 'metric_key': 'order_count', 'label': '订单数量'},
        {'analysis_id': aid, 'metric_key': 'avg_price', 'label': '平均金额'}]})
    assert _data_answer(runtime, [aid]) == '订单数量：**2**。'
    assert len(store._index('database_queries')) == 1


def test_sql_unknown_alias_uses_explicit_business_label_and_exact_decimal(db_setup):
    store, _, sid = db_setup
    task = TaskSpec(session_id=sid, request='订单金额合计？', database_source_ids=['olist'])
    store.create_task(task)
    runtime = TaskRuntime(task, store, Mock(), lambda: False)
    result = tools(runtime, analyst_tools)['run_sql'].invoke({'source_id': 'olist',
        'sql': 'SELECT sum(price) AS business_answer FROM olist.orders'})
    aid = result['items'][0]['analysis_id']
    tools(runtime, main_tools)['finish_task'].invoke({'intent': 'analysis', 'answer': '已查询', 'metric_refs': [
        {'analysis_id': aid, 'metric_key': 'business_answer', 'label': '订单金额合计'}]})
    assert _data_answer(runtime, [aid]) == '订单金额合计：**30.000000**。'


def test_explicit_top_one_returns_one_requested_result_not_all_sql_rows(db_setup):
    store, _, sid = db_setup
    task = TaskSpec(session_id=sid, request='列出Top1订单金额', database_source_ids=['olist'])
    store.create_task(task)
    runtime = TaskRuntime(task, store, Mock(), lambda: False)
    result = tools(runtime, analyst_tools)['run_sql'].invoke({'source_id': 'olist',
        'sql': 'SELECT price FROM olist.orders ORDER BY price DESC'})
    aid = result['items'][0]['analysis_id']
    answer = _data_answer(runtime, [aid])
    assert '| 20.0 |' in answer and '| 10.0 |' not in answer
    assert 'result_rows' not in answer and '口径' not in answer


def test_unknown_sql_aliases_get_one_binding_recovery_not_extra_queries(db_setup):
    store, _, sid = db_setup
    task = TaskSpec(session_id=sid, request='订单金额合计？', database_source_ids=['olist'])
    store.create_task(task)
    def finish(_):
        aid = next(iter(store._index('analyses')))
        return call('finish_task', {'intent': 'analysis', 'answer': '已查询', 'metric_refs': [
            {'analysis_id': aid, 'metric_key': 'business_answer', 'label': '订单金额合计'}]}, 'binding')
    scripted, factory = models([
        call('task', {'subagent_type': 'analyst', 'description': '查询订单金额合计'}),
        AIMessage(content='已完成。'), finish], analyst=[
        call('run_sql', {'source_id': 'olist', 'sql':
            'SELECT sum(price) AS business_answer, count(*) AS available_records FROM olist.orders'}),
        AIMessage(content='计算已登记')])
    events = Mock()
    result = run_task(task, store, events, lambda: False, model_factory=factory)
    assert result['status'] == 'completed', result
    assert result['final_response'] == '订单金额合计：**30.000000**。'
    assert len(store._index('database_queries')) == 1
    assert scripted['main']._index == 3 and scripted['analyst']._index == 2
    assert any(call.args[0] == 'delivery_recovery' for call in events.call_args_list)


@pytest.mark.parametrize('user_text', ['Top15改Top10', '把标题换成客户购买情况', '删除图表模块'])
def test_followup_short_edit_needs_report_context(user_text):
    task = TaskSpec(session_id='s', request=user_text)
    assert not _report_requested(task, None)
    assert _report_requested(task, None, has_prior_report=True)
    task.request = '如何' + user_text
    assert not _report_requested(task, None, has_prior_report=True)
    task.request = '不要' + user_text
    assert not _report_requested(task, None, has_prior_report=True)
