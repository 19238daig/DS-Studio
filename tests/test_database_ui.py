"""UI database controls preserve explicit scopes without contacting PostgreSQL."""
from unittest.mock import Mock

import pytest

from src.client import ApiClient, ApiError
from test_ui import file_ui


@pytest.fixture
def database_ui(file_ui, monkeypatch):
    at, item = file_ui
    at.session_state.selected_files = []
    monkeypatch.setattr(ApiClient, 'health', lambda self: {'worker': {'ready': True}, 'database': {'configured': True}})
    monkeypatch.setattr(ApiClient, 'database_sources', lambda self: [
        {'source_id': 'olist', 'name': 'Olist 电商分析', 'configured': True},
        {'source_id': 'sellers_restricted', 'name': 'Olist 卖家（受限城市）', 'configured': True}])
    description = {'scope_fingerprint': 'scope-one', 'scope_description': '只读测试来源', 'tables': [
        {'name': 'orders', 'qualified_name': 'olist.orders', 'columns': [{'name': 'order_id', 'data_type': 'text'}], 'primary_key': ['order_id'], 'foreign_keys': []},
        {'name': 'sellers', 'qualified_name': 'olist.sellers', 'columns': [{'name': 'seller_id', 'data_type': 'text'}], 'primary_key': ['seller_id'], 'foreign_keys': []}]}
    monkeypatch.setattr(ApiClient, 'database_catalog', lambda self, source: description)
    preview = Mock(return_value={'preview': [{'order_id': 'visible'}], 'row_count': 1, 'query_id': 'q1'})
    monkeypatch.setattr(ApiClient, 'database_preview', preview)
    submit = Mock(side_effect=ApiError('temporary failure', 503))
    monkeypatch.setattr(ApiClient, 'submit', submit)
    return at, description, preview, submit


def test_database_only_scope_and_empty_table_choice_reach_submit(database_ui):
    at, _, _, submit = database_ui
    at.run()
    at.multiselect(key='selected_database_sources').select('olist').run()
    assert not at.exception
    assert at.session_state.selected_database_tables == {'olist': ['olist.orders', 'olist.sellers']}
    assert any('0 份文件 · 1 个数据库来源' in block.value for block in at.markdown)
    assert not any('还没有选择数据' in block.value for block in at.markdown)
    at.multiselect(key='database_table_picker_olist').set_value([]).run()
    assert at.session_state.selected_database_tables == {'olist': []}
    assert any('尚未授权业务表' in message.value for message in at.info)
    at.text_area(key='prompt').set_value('不要查询任何表')
    next(button for button in at.button if button.label == '发送').click().run()
    assert submit.call_args.kwargs['database_tables'] == {'olist': []}
    assert submit.call_args.kwargs['database_source_ids'] == ['olist']


def test_idempotency_key_changes_with_scope_and_explicit_clear(database_ui):
    at, _, _, submit = database_ui
    at.run()
    at.multiselect(key='selected_database_sources').select('olist').run()
    at.text_area(key='prompt').set_value('查询数量')
    next(button for button in at.button if button.label == '发送').click().run()
    key = submit.call_args.args[4]
    next(button for button in at.button if button.label == '发送').click().run()
    assert submit.call_args.args[4] == key
    at.multiselect(key='database_table_picker_olist').set_value(['olist.orders']).run()
    next(button for button in at.button if button.label == '发送').click().run()
    assert submit.call_args.args[4] != key
    assert submit.call_args.kwargs['database_tables'] == {'olist': ['olist.orders']}
    at.multiselect(key='selected_database_sources').set_value([]).run()
    next(button for button in at.button if button.label == '发送').click().run()
    assert submit.call_args.kwargs['database_source_ids'] == []
    assert submit.call_args.kwargs['database_tables'] == {}


def test_preview_cache_invalidates_on_actual_scope_change(database_ui):
    at, catalog, preview, _ = database_ui
    at.run()
    at.multiselect(key='selected_database_sources').select('olist').run()
    at.button(key='db_preview_button_olist').click().run()
    preview.assert_called_once_with('s1', 'olist', 'olist.orders')
    assert any('order_id' in frame.value and 'visible' in frame.value.to_string() for frame in at.dataframe)
    catalog['scope_fingerprint'] = 'scope-two'
    at.run()
    assert not any('visible' in frame.value.to_string() for frame in at.dataframe)
    assert not any(key.startswith('db_preview_olist_scope-one') for key in at.session_state)


def test_switch_session_clears_database_widgets_and_metadata(database_ui, monkeypatch):
    at, _, _, _ = database_ui
    monkeypatch.setattr(ApiClient, 'sessions', lambda self: [
        {'session_id': 's1', 'title': '一'}, {'session_id': 's2', 'title': '二'}])
    monkeypatch.setattr(ApiClient, 'session', lambda self, sid: {'session_id': sid, 'messages': [], 'tasks': []})
    at.run()
    at.multiselect(key='selected_database_sources').select('olist').run()
    at.button(key='db_preview_button_olist').click().run()
    at.radio(key='session_picker_s1').set_value('s2').run()
    assert not at.exception
    assert at.session_state.session_id == 's2'
    assert at.session_state.selected_database_sources == []
    assert at.session_state.database_catalogs_current == {}
    assert not any(key.startswith(('db_catalog_olist_', 'db_preview_olist_')) for key in at.session_state)


def test_catalog_unavailable_does_not_break_file_question(database_ui, monkeypatch):
    at, _, _, submit = database_ui
    monkeypatch.setattr(ApiClient, 'database_catalog', Mock(side_effect=ApiError('database offline', 503)))
    at.run()
    # Selecting the offline source shows a recoverable error and can be cleared.
    at.multiselect(key='selected_database_sources').select('olist').run()
    assert not at.exception
    assert any('database offline' in error.value for error in at.error)
    at.multiselect(key='selected_database_sources').set_value([]).run()
    at.multiselect(key='selected_files').select('file1').run()
    at.text_area(key='prompt').set_value('分析文件')
    next(button for button in at.button if button.label == '发送').click().run()
    assert submit.call_args.args[2] == ['file1'] and submit.call_args.kwargs['database_source_ids'] == []


def test_untouched_database_picker_omits_selection_for_main_routing(database_ui):
    at, _, _, submit = database_ui
    at.run()
    at.text_area(key='prompt').set_value('使用Olist数据库统计订单')
    next(button for button in at.button if button.label == '发送').click().run()
    assert 'database_source_ids' not in submit.call_args.kwargs


def test_clarification_restores_database_scope_once_and_user_can_clear(database_ui, monkeypatch):
    at, _, _, submit = database_ui
    monkeypatch.setattr(ApiClient, 'session', lambda self, sid: {'session_id': sid, 'messages': [], 'tasks': ['waiting']})
    task = {'task_id': 'waiting', 'status': 'waiting_user', 'spec': {'request': '比较订单', 'expected_outputs': [],
            'database_source_ids': ['olist'], 'database_tables': {'olist': ['olist.orders']}}}
    monkeypatch.setattr(ApiClient, 'task', lambda self, tid: task)
    events = [{'type': 'sql_completed', 'seq': 1, 'source_id': 'olist', 'query_id': 'query-one',
               'sql': 'SELECT count(*) AS total FROM olist.orders', 'row_count': 1, 'duration_seconds': 0.12}]
    monkeypatch.setattr(ApiClient, 'events', lambda self, tid, after=0: {'items': events if after == 0 else [], 'cursor': 1})
    monkeypatch.setattr(ApiClient, 'artifacts', lambda self, tid: [])
    at.run()
    assert not at.exception
    assert at.session_state.selected_database_sources == ['olist']
    assert at.session_state.selected_database_tables == {'olist': ['olist.orders']}
    assert any(code.value == events[0]['sql'] for code in at.code)
    assert any('结果 1 行' in caption.value for caption in at.caption)
    at.multiselect(key='database_table_picker_olist').set_value([]).run()
    assert at.session_state.selected_database_tables == {'olist': []}
    at.text_area(key='prompt').set_value('本轮不要读表')
    next(button for button in at.button if button.label == '发送').click().run()
    assert submit.call_args.kwargs['reply_to_task_id'] == 'waiting'
    assert submit.call_args.kwargs['database_tables'] == {'olist': []}
