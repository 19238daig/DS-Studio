"""UI contract checks use mock HTTP-facing methods; no agents or model calls."""
from pathlib import Path
from unittest.mock import Mock

import pytest
pytest.importorskip('streamlit', reason='Legacy UI tests require the optional legacy-ui extra')
from streamlit.testing.v1 import AppTest

from src.client import ApiClient, ApiError

APP = Path(__file__).resolve().parents[1] / 'app.py'


@pytest.fixture
def file_ui(monkeypatch):
    item = {'file_id': 'file1', 'name': 'example.csv', 'source': 'upload', 'size': 100,
            'profile_status': 'pending', 'profile_task_id': 'profile1', 'profiles': []}
    monkeypatch.setattr(ApiClient, 'health', lambda self: {'status': 'ok', 'worker': {'ready': True}})
    monkeypatch.setattr(ApiClient, 'sessions', lambda self: [{'session_id': 's1', 'title': '概览测试'}])
    monkeypatch.setattr(ApiClient, 'kb_files', lambda self: [])
    monkeypatch.setattr(ApiClient, 'files', lambda self, session_id: [item])
    monkeypatch.setattr(ApiClient, 'session', lambda self, session_id: {'session_id': 's1', 'messages': [], 'tasks': []})
    monkeypatch.setattr(ApiClient, 'memories', lambda self, owner: [])
    at = AppTest.from_file(str(APP), default_timeout=10)
    at.session_state.session_id = 's1'
    at.session_state.selected_files = ['file1']
    return at, item


@pytest.mark.parametrize('available', [True, False])
def test_streamed_verified_answer_keeps_cancel_responsive_and_poll_fallback(file_ui, monkeypatch, available):
    at, _ = file_ui
    answer = '已验证：平均值为20。'
    record = {'task_id': 'live', 'status': 'running', 'spec': {'request': '求平均值'}}
    subscription = Mock()
    subscription.task_id = 'live'
    subscription.snapshot.return_value = {'answer': answer if available else '',
        'progress': '正在核验结果。', 'done': False, 'available': available}
    subscriptions = Mock(return_value=subscription)
    monkeypatch.setattr(ApiClient, 'subscribe_task', subscriptions)
    monkeypatch.setattr(ApiClient, 'health', lambda self: {'worker': {'ready': True}, 'streaming': {'enabled': True}})
    def session(self, sid):
        messages = [{'role': 'user', 'content': '求平均值', 'task_id': 'live'}]
        if record['status'] == 'succeeded':
            messages.append({'role': 'assistant', 'content': answer, 'task_id': 'live'})
        return {'session_id': sid, 'tasks': ['live'], 'messages': messages}
    monkeypatch.setattr(ApiClient, 'session', session)
    monkeypatch.setattr(ApiClient, 'task', lambda self, tid: dict(record))
    monkeypatch.setattr(ApiClient, 'events', lambda self, tid, after=0: {'items': [], 'cursor': 0})
    monkeypatch.setattr(ApiClient, 'artifacts', lambda self, tid: [])
    cancel = Mock(return_value={'task_id': 'live'})
    monkeypatch.setattr(ApiClient, 'cancel', cancel)
    at.session_state.active_task = 'live'
    at.run()
    assert not at.exception and not at.error
    assert not at.button(key='cancel_live').disabled
    if available:
        assert sum(text.value == answer for text in at.markdown) == 1
    at.button(key='cancel_live').click().run()
    assert not at.exception and not at.error
    cancel.assert_called_once_with('live')
    assert subscriptions.call_count == 1
    # Canonical polling finishes the task even if the stream was unsupported.
    record.update(status='succeeded', final_response=answer)
    at.run()
    assert not at.exception and not at.error
    assert sum(text.value == answer for text in at.markdown) == 1
    subscription.close.assert_called()


def test_question_log_and_task_metrics_show_agent_time_and_provider_tokens(file_ui, monkeypatch):
    from src.telemetry import summarize_task
    at, _ = file_ui
    task = {'task_id': 't1', 'status': 'succeeded', 'spec': {'request': '求平均值'},
            'created_at': '2026-10-23T10:00:00Z', 'started_at': '2026-10-23T10:00:01Z',
            'finished_at': '2026-10-23T10:00:12Z'}
    events = [{'type': 'agent_assigned', 'role': 'main', 'target': 'analyst', 'created_at': task['started_at']},
              {'type': 'model_usage', 'role': 'analyst', 'seq': 2, 'usage': {'input_tokens': 100, 'output_tokens': 20, 'total_tokens': 120}}]
    task['telemetry'] = summarize_task(task, events)
    monkeypatch.setattr(ApiClient, 'session', lambda self, sid: {'session_id': sid, 'tasks': ['t1'],
        'messages': [{'role': 'user', 'content': '求平均值', 'task_id': 't1'}],
        'question_logs': [{'task_id': 't1', 'question': '求平均值', 'status': 'succeeded',
                           'created_at': task['created_at'], 'telemetry': task['telemetry']}]})
    monkeypatch.setattr(ApiClient, 'task', lambda self, tid: task)
    monkeypatch.setattr(ApiClient, 'events', lambda self, tid, after=0: {'items': events if after == 0 else [], 'cursor': 2})
    monkeypatch.setattr(ApiClient, 'artifacts', lambda self, tid: [])
    at.run()
    assert not at.exception
    assert {m.label: m.value for m in at.metric}['Token 总量'] == '120'
    assert {m.label: m.value for m in at.metric}['响应时间'] == '12.00 秒'
    assert any('主 Agent → 数据分析 Agent' in text.value for text in at.text)
    log = next(frame.value for frame in at.dataframe if '用户问题' in frame.value.columns)
    assert log.iloc[0]['用户问题'] == '求平均值' and log.iloc[0]['总 Token'] == 120
    assert any('Token 120' in text.value for text in at.caption)


def test_pending_profile_is_clear_and_transitions_to_complete(file_ui):
    at, item = file_ui
    at.run()
    assert not at.exception
    assert any('等待解析' in info.value for info in at.info)
    assert not at.metric
    item.update(profile_status='ready', profiles=[{
        'sheet_or_table': 'Sheet 1', 'columns': ['amount'], 'dtypes': {'amount': 'int64'},
        'row_count': 1234, 'quality_warnings': [], 'preview': [{'amount': 10}],
    }])
    at.run()
    assert not at.exception
    assert any(m.label == '完整数据行数' and m.value == '1234' for m in at.metric)
    assert any('仅展示 1 行预览样本' in caption.value for caption in at.caption)
    assert not any('等待解析' in info.value for info in at.info)


def test_multiple_tables_show_full_counts_schema_preview_and_warnings(file_ui):
    at, item = file_ui
    item.update(profile_status='ready', profiles=[
        {'sheet_or_table': 'Sales', 'columns': ['amount', 'code'],
         'dtypes': {'amount': 'float64', 'code': 'str'}, 'row_count': 300,
         'quality_warnings': ['amount 有 2 个缺失值'], 'preview': [{'amount': 12.5, 'code': '001'}]},
        {'sheet_or_table': 'Returns', 'columns': ['amount'], 'dtypes': {'amount': 'int64'},
         'row_count': 8, 'quality_warnings': [], 'preview': [{'amount': 1}]},
    ])
    at.run()
    assert not at.exception
    assert {metric.value for metric in at.metric if metric.label == '完整数据行数'} == {'300', '8'}
    assert {metric.value for metric in at.metric if metric.label == '列数'} == {'2', '1'}
    assert {'Sales', 'Returns'} <= {text.value for text in at.text}
    assert any('缺失值' in warning.value for warning in at.warning)
    # Two schemas, two sample previews, and the registered-file list.
    assert len(at.dataframe) == 5
    assert at.dataframe[1].value.iloc[0]['code'] == '001'


def test_profile_failure_never_displays_stale_success(file_ui):
    at, item = file_ui
    item.update(profile_status='failed', profile_error='CSV 行 4 列数不一致',
                profiles=[{'row_count': 999, 'columns': ['old'], 'preview': [{'old': 1}]}])
    at.run()
    assert not at.exception
    assert any('解析失败' in warning.value for warning in at.warning)
    assert any('列数不一致' in text.value for text in at.text)
    assert not at.metric


def test_html_text_is_plain_and_empty_table_is_explicit(file_ui, monkeypatch):
    at, item = file_ui
    import streamlit.components.v1 as components
    embed = Mock(side_effect=AssertionError('Uploaded HTML must never be embedded'))
    monkeypatch.setattr(components, 'html', embed)
    item.update(name='notes.html', profile_status='ready',
                text='<script>alert(1)</script>说明材料',
                profiles=[{'sheet_or_table': 'HTML', 'row_count': 0, 'columns': [], 'dtypes': {},
                           'quality_warnings': ['No tables; text length=20'], 'preview': []}])
    at.run()
    assert not at.exception
    assert any('No tables' in warning.value for warning in at.warning)
    assert any(text.value == item['text'] for text in at.text)
    assert any('没有可计算表格' in info.value for info in at.info)
    assert not at.metric
    assert not any(text.value == '默认数据表' for text in at.text)
    embed.assert_not_called()


def test_single_profile_compatibility_and_missing_profile(file_ui):
    at, item = file_ui
    item.update(profile_status='ready', profile={'row_count': 10, 'columns': ['x'], 'dtypes': {},
                                                'quality_warnings': [], 'preview': []})
    at.run()
    assert not at.exception
    assert any(metric.value == '10' for metric in at.metric)
    item.update(profile_status='registered', profile=None)
    at.run()
    assert not at.exception
    assert any('尚未提供数据概览' in info.value for info in at.info)


def test_offline_backend_has_useful_connection_message(monkeypatch):
    def offline(self):
        raise ApiError('无法连接本地后端，请运行 scripts/start.ps1。')
    monkeypatch.setattr(ApiClient, 'health', offline)
    at = AppTest.from_file(str(APP), default_timeout=10).run()
    assert not at.exception
    assert any('scripts/start.ps1' in error.value for error in at.error)


def test_report_library_shows_original_and_revision_with_independent_previews(file_ui, monkeypatch):
    import streamlit.components.v1 as components
    at, _ = file_ui
    original = {'artifact_id': 'original', 'kind': 'report', 'name': 'report.html', 'mime_type': 'text/html', 'size': 100}
    revised = {**original, 'artifact_id': 'revised'}
    def version(number, title, artifact, base=None):
        return dict(number=number, version=f'v{number}', title=title, request=title,
                    created_at='2026-10-23T09:00:00Z', artifacts=[artifact],
                    revision_of={'report_id': 'original'} if base else None, base_number=base)
    monkeypatch.setattr(ApiClient, 'session', lambda self, sid: {'session_id': sid, 'messages': [], 'tasks': ['t2'],
        'report_versions': [version(2, '修改版', revised, 1), version(1, '原版', original)]})
    monkeypatch.setattr(ApiClient, 'task', lambda self, tid: {'task_id': tid, 'status': 'succeeded', 'spec': {}})
    monkeypatch.setattr(ApiClient, 'events', lambda self, tid, after=0: {'items': [], 'cursor': 0})
    # Selected-task reports must not be rendered twice with duplicate widget IDs.
    monkeypatch.setattr(ApiClient, 'artifacts', lambda self, tid: [revised])
    download = Mock(side_effect=lambda aid: f'<h1>{aid}</h1>'.encode())
    monkeypatch.setattr(ApiClient, 'download', lambda self, aid: download(aid))
    embed = Mock()
    monkeypatch.setattr(components, 'html', embed)
    at.run()
    assert not at.exception
    assert any('版本 2 · 修订版 · 修改版' == e.label for e in at.expander)
    assert any('版本 1 · 原始报告 · 原版' == e.label for e in at.expander)
    at.button(key='load_original').click().run()
    assert not at.exception and at.session_state['download_original'] == b'<h1>original</h1>'
    at.button(key='load_revised').click().run()
    assert not at.exception
    assert at.session_state['download_original'] == b'<h1>original</h1>'
    assert at.session_state['download_revised'] == b'<h1>revised</h1>'
    assert {c.args[0] for c in embed.call_args_list} == {'<h1>original</h1>', '<h1>revised</h1>'}
    assert {b.key for b in at.get('download_button')} == {'save_original', 'save_revised'}


@pytest.mark.parametrize('suffix', ['md', 'txt'])
def test_text_material_never_claims_zero_data_rows(file_ui, suffix):
    at, item = file_ui
    item.update(name='notes.' + suffix, profile_status='ready', profiles=[{
        'sheet_or_table': None, 'row_count': 0, 'columns': [], 'dtypes': {},
        'quality_warnings': ['Text material; no computable tables'], 'preview': []}])
    at.run()
    assert not at.exception
    assert any('文字材料' in info.value for info in at.info)
    assert not at.metric
    assert not any(text.value == '默认数据表' for text in at.text)


def test_delete_session_requires_confirmation_and_clears_all_task_caches(file_ui, monkeypatch):
    at, _ = file_ui
    sessions = [{'session_id': 's1', 'title': '删除测试'}, {'session_id': 's2', 'title': '保留测试'}]
    deleted, submitted = Mock(), Mock(side_effect=AssertionError('Deletion must never start a model task'))
    monkeypatch.setattr(ApiClient, 'sessions', lambda self: list(sessions))
    def delete(self, sid):
        deleted(sid)
        sessions[:] = [item for item in sessions if item['session_id'] != sid]
    monkeypatch.setattr(ApiClient, 'delete_session', delete)
    monkeypatch.setattr(ApiClient, 'submit', submitted)
    at.session_state['download_old'] = b'old report'
    at.session_state['events_old'] = ['old event']
    at.session_state['cursor_old'] = 10
    at.session_state['submit_key'] = 'old key'
    at.session_state['submit_payload'] = ('old draft',)
    at.session_state['reply_to_task'] = 'old-task'
    at.run()
    assert not at.exception
    assert at.button(key='delete_session').disabled
    deleted.assert_not_called()
    at.checkbox(key='delete_confirm_s1').check().run()
    assert not at.button(key='delete_session').disabled
    assert any('独立记忆保留' in c.value for c in at.caption)
    at.button(key='delete_session').click().run()
    assert not at.exception
    deleted.assert_called_once_with('s1')
    assert at.session_state.session_id == 's2'
    for key in ['download_old', 'events_old', 'cursor_old', 'submit_key', 'submit_payload']:
        assert key not in at.session_state
    assert at.session_state.reply_to_task is None
    submitted.assert_not_called()


def test_delete_last_session_opens_empty_session_without_model_calls(file_ui, monkeypatch):
    at, _ = file_ui
    sessions = [{'session_id': 's1', 'title': '唯一会话'}]
    monkeypatch.setattr(ApiClient, 'sessions', lambda self: list(sessions))
    monkeypatch.setattr(ApiClient, 'delete_session', lambda self, sid: sessions.clear())
    create = Mock(return_value={'session_id': 's2'})
    def create_session(self):
        result = create()
        sessions.append({'session_id': result['session_id'], 'title': '空会话'})
        return result
    monkeypatch.setattr(ApiClient, 'create_session', create_session)
    submit = Mock(side_effect=AssertionError('No model task'))
    monkeypatch.setattr(ApiClient, 'submit', submit)
    at.run().checkbox(key='delete_confirm_s1').check().run()
    at.button(key='delete_session').click().run()
    assert not at.exception
    assert at.session_state.session_id == 's2'
    create.assert_called_once()
    submit.assert_not_called()


def test_active_session_deletion_keeps_session_and_shows_backend_rejection(file_ui, monkeypatch):
    at, _ = file_ui
    def reject(self, sid):
        raise ApiError('请先取消或完成进行中的任务', 409, 'request-test')
    monkeypatch.setattr(ApiClient, 'delete_session', reject)
    at.session_state['download_old'] = b'keep on failed delete'
    at.run().checkbox(key='delete_confirm_s1').check().run()
    at.button(key='delete_session').click().run()
    assert not at.exception
    assert at.session_state.session_id == 's1'
    assert at.session_state['download_old'] == b'keep on failed delete'
    assert any('进行中的任务' in e.value for e in at.error)


@pytest.fixture
def waiting_ui(file_ui, monkeypatch):
    at, item = file_ui
    item.update(profile_status='ready', profiles=[{'columns': ['value'], 'row_count': 1, 'preview': []}])
    monkeypatch.setattr(ApiClient, 'session', lambda self, sid: {'session_id': sid, 'messages': [],
        'tasks': ['waiting'] if sid == 's1' else []})
    monkeypatch.setattr(ApiClient, 'task', lambda self, tid: {'task_id': tid,
        'status': 'waiting_user' if tid == 'waiting' else 'queued',
        'spec': {'expected_outputs': ['html', 'xlsx']}, 'final_response': '请说明报告受众。'})
    monkeypatch.setattr(ApiClient, 'events', lambda self, tid, after=0: {'items': [], 'cursor': 0})
    monkeypatch.setattr(ApiClient, 'artifacts', lambda self, tid: [])
    submitted = Mock(return_value={'task_id': 'followup'})
    monkeypatch.setattr(ApiClient, 'submit', submitted)
    return at, submitted


@pytest.mark.parametrize('clear_formats', [False, True])
def test_clarification_defaults_inherit_formats_but_user_can_clear(waiting_ui, clear_formats):
    at, submitted = waiting_ui
    at.run()
    assert not at.exception
    assert at.multiselect(key='report_outputs').value == ['html', 'xlsx']
    if clear_formats:
        at.multiselect(key='report_outputs').set_value([]).run()
        assert at.multiselect(key='report_outputs').value == []
    at.text_area(key='prompt').set_value('面向管理层')
    next(button for button in at.button if button.label == '发送').click().run()
    assert not at.exception
    assert submitted.call_count == 1
    assert submitted.call_args.args[3] == ([] if clear_formats else ['html', 'xlsx'])
    assert submitted.call_args.kwargs['reply_to_task_id'] == 'waiting'


def test_switch_session_resets_report_format_defaults(waiting_ui, monkeypatch):
    at, submitted = waiting_ui
    monkeypatch.setattr(ApiClient, 'sessions', lambda self: [
        {'session_id': 's1', 'title': '待补充'}, {'session_id': 's2', 'title': '其他会话'}])
    at.run()
    assert at.multiselect(key='report_outputs').value == ['html', 'xlsx']
    next(select for select in at.radio if select.label == '会话').set_value('s2').run()
    assert not at.exception
    assert at.session_state.session_id == 's2'
    assert at.multiselect(key='report_outputs').value == []
    assert at.session_state.reply_to_task is None
    submitted.assert_not_called()


@pytest.mark.parametrize('chosen_formats', [[], ['xlsx']])
def test_leaving_waiting_history_clears_reply_without_overwriting_formats(waiting_ui, monkeypatch, chosen_formats):
    at, submitted = waiting_ui
    monkeypatch.setattr(ApiClient, 'session', lambda self, sid: {'session_id': sid, 'messages': [],
        'tasks': ['completed', 'waiting']})
    monkeypatch.setattr(ApiClient, 'task', lambda self, tid: {'task_id': tid,
        'status': {'waiting': 'waiting_user', 'completed': 'succeeded'}.get(tid, 'queued'),
        'spec': {'expected_outputs': ['html', 'xlsx']}, 'final_response': 'Task response'})
    at.run()
    assert at.session_state.reply_to_task == 'waiting'
    at.multiselect(key='report_outputs').set_value(chosen_formats).run()
    next(select for select in at.selectbox if select.value == 'waiting').set_value('completed').run()
    assert not at.exception
    assert 'reply_to_task' not in at.session_state
    assert 'reply_formats_for' not in at.session_state
    assert at.multiselect(key='report_outputs').value == chosen_formats
    assert not any('下一条消息将关联此任务' in info.value for info in at.info)
    at.text_area(key='prompt').set_value('一个独立的新问题')
    next(button for button in at.button if button.label == '发送').click().run()
    assert not at.exception
    assert submitted.call_count == 1
    assert submitted.call_args.kwargs['reply_to_task_id'] is None
    assert submitted.call_args.args[3] == chosen_formats


def test_completed_answer_is_not_repeated_inside_task_panel(file_ui, monkeypatch):
    at, _ = file_ui
    answer = '已验证的分析结果只应显示一次。'
    monkeypatch.setattr(ApiClient, 'session', lambda self, sid: {
        'session_id': sid, 'tasks': ['completed'], 'messages': [
            {'role': 'user', 'content': '检查这份数据', 'task_id': 'completed'},
            {'role': 'assistant', 'content': answer, 'task_id': 'completed'}]})
    monkeypatch.setattr(ApiClient, 'task', lambda self, tid: {
        'task_id': tid, 'status': 'succeeded', 'final_response': answer})
    monkeypatch.setattr(ApiClient, 'events', lambda self, tid, after=0: {'items': [], 'cursor': 0})
    monkeypatch.setattr(ApiClient, 'artifacts', lambda self, tid: [])
    at.run()
    assert not at.exception
    assert len(at.chat_message) == 2
    assert sum(markdown.value == answer for markdown in at.markdown) == 1


def test_retry_with_same_question_keeps_successful_task_when_previewing_report(file_ui, monkeypatch):
    import streamlit.components.v1 as components
    at, _ = file_ui
    embed = Mock()
    monkeypatch.setattr(components, 'html', embed)
    monkeypatch.setattr(ApiClient, 'session', lambda self, sid: {'session_id': sid,
        'tasks': ['failed', 'completed'], 'messages': [
            {'role': 'user', 'content': '生成对比报告', 'task_id': 'failed'},
            {'role': 'user', 'content': '生成对比报告', 'task_id': 'completed'},
            {'role': 'assistant', 'content': '报告已生成', 'task_id': 'completed'}]})
    monkeypatch.setattr(ApiClient, 'task', lambda self, tid: {'task_id': tid,
        'status': 'failed' if tid == 'failed' else 'succeeded', 'error': 'old failure' if tid == 'failed' else None})
    monkeypatch.setattr(ApiClient, 'events', lambda self, tid, after=0: {'items': [], 'cursor': 0})
    monkeypatch.setattr(ApiClient, 'artifacts', lambda self, tid: [] if tid == 'failed' else [
        {'artifact_id': 'report1', 'name': 'report.html', 'relative_path': 'report/s1/report.html',
         'mime_type': 'text/html', 'kind': 'report', 'downloadable': True}])
    download = Mock(return_value=b'<html>verified report</html>')
    monkeypatch.setattr(ApiClient, 'download', lambda self, aid: download(aid))
    at.session_state.history_focus_task = 'completed'
    at.run()
    assert not at.exception and not at.error
    at.button(key='load_report1').click().run()
    assert not at.exception and not at.error
    picker = next(select for select in at.selectbox if select.label == '查看历史任务')
    assert picker.value == 'completed' and len(set(picker.options)) == 2
    download.assert_called_once_with('report1')
    embed.assert_called_once()
    assert len(at.get('download_button')) == 1


def test_svg_artifact_preview_uses_xml_instead_of_raster_bytes(file_ui, monkeypatch):
    import streamlit as st
    at, _ = file_ui
    monkeypatch.setattr(ApiClient, 'session', lambda self, sid: {'session_id': sid,
        'tasks': ['completed'], 'messages': []})
    monkeypatch.setattr(ApiClient, 'task', lambda self, tid: {'task_id': tid, 'status': 'succeeded'})
    monkeypatch.setattr(ApiClient, 'events', lambda self, tid, after=0: {'items': [], 'cursor': 0})
    monkeypatch.setattr(ApiClient, 'artifacts', lambda self, tid: [
        {'artifact_id': 'vector', 'name': 'plot.svg', 'relative_path': 'script_output/s1/t1/plot.svg',
         'mime_type': 'image/svg+xml', 'kind': 'chart_svg', 'downloadable': True}])
    svg = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20"><path d="M0 0L20 20"/></svg>'
    monkeypatch.setattr(ApiClient, 'download', lambda self, aid: svg.encode())
    preview = Mock()
    monkeypatch.setattr(st, 'image', preview)
    at.run().button(key='load_vector').click().run()
    assert not at.exception and not at.error
    preview.assert_called_once_with(svg)


def test_starter_only_prefills_draft_without_submitting(file_ui, monkeypatch):
    at, _ = file_ui
    submit = Mock(side_effect=AssertionError('Starter cards must not submit tasks'))
    monkeypatch.setattr(ApiClient, 'submit', submit)
    at.run().button(key='example_0').click().run()
    assert not at.exception
    assert '完整行数' in at.text_area(key='prompt').value
    submit.assert_not_called()


def test_failed_submission_keeps_draft_and_idempotency_key(file_ui, monkeypatch):
    at, _ = file_ui
    submit = Mock(side_effect=ApiError('暂时无法提交', 503))
    monkeypatch.setattr(ApiClient, 'submit', submit)
    at.run()
    at.text_area(key='prompt').set_value('请检查我的数据')
    next(button for button in at.button if button.label == '发送').click().run()
    assert not at.exception
    assert at.text_area(key='prompt').value == '请检查我的数据'
    original_key = submit.call_args.args[4]
    next(button for button in at.button if button.label == '发送').click().run()
    assert not at.exception
    assert submit.call_args.args[4] == original_key
    assert submit.call_count == 2


def test_empty_workspace_can_create_first_session_without_model_calls(monkeypatch):
    sessions = []
    monkeypatch.setattr(ApiClient, 'health', lambda self: {'worker': {'ready': True}})
    monkeypatch.setattr(ApiClient, 'sessions', lambda self: list(sessions))
    monkeypatch.setattr(ApiClient, 'kb_files', lambda self: [])
    monkeypatch.setattr(ApiClient, 'files', lambda self, sid: [])
    monkeypatch.setattr(ApiClient, 'session', lambda self, sid: {'messages': [], 'tasks': []})
    monkeypatch.setattr(ApiClient, 'memories', lambda self, owner: [])
    submit = Mock(side_effect=AssertionError('Creating a session must not start a model'))
    monkeypatch.setattr(ApiClient, 'submit', submit)
    def create(self):
        result = {'session_id': 'first', 'title': '新会话'}
        sessions.append(result)
        return result
    monkeypatch.setattr(ApiClient, 'create_session', create)
    at = AppTest.from_file(str(APP), default_timeout=10).run()
    assert not at.exception
    assert any('新建对话' in info.value for info in at.info)
    at.button(key='new_session').click().run()
    assert not at.exception
    assert at.session_state.session_id == 'first'
    assert at.text_area(key='prompt').value == ''
    submit.assert_not_called()


def test_duplicate_session_titles_do_not_switch_selection_on_other_click(file_ui, monkeypatch):
    at, _ = file_ui
    monkeypatch.setattr(ApiClient, 'sessions', lambda self: [
        {'session_id': 's1', 'title': '同名会话'}, {'session_id': 's2', 'title': '同名会话'}])
    at.session_state.session_id = 's2'
    at.run()
    assert not at.exception
    selector = next(widget for widget in at.radio if widget.label == '会话')
    assert len(set(selector.options)) == 2
    at.button(key='example_0').click().run()
    assert not at.exception
    assert at.session_state.session_id == 's2'
