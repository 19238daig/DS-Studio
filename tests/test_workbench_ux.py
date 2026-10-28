"""Presentation states are checked through the real Streamlit app, without models."""
from unittest.mock import Mock

import pytest

from src.client import ApiClient
from test_ui import file_ui


def task_ui(at, monkeypatch, record, artifacts=None, versions=None):
    monkeypatch.setattr(ApiClient, 'task', lambda self, tid: record)
    monkeypatch.setattr(ApiClient, 'events', lambda self, tid, after=0: {'items': [], 'cursor': 0})
    monkeypatch.setattr(ApiClient, 'artifacts', lambda self, tid: artifacts or [])
    session = {'session_id': 's1', 'tasks': ['t1'], 'messages': [
        {'role': 'user', 'content': '分析任务', 'task_id': 't1'},
        {'role': 'assistant', 'content': '任务的真实结论。', 'task_id': 't1'}]}
    if versions is not None:
        session['report_versions'] = versions
    monkeypatch.setattr(ApiClient, 'session', lambda self, sid: session)


def test_task_sources_do_not_follow_next_question_selection(file_ui, monkeypatch):
    at, _ = file_ui
    at.session_state.selected_files = []
    task_ui(at, monkeypatch, {'task_id': 't1', 'status': 'succeeded',
        'spec': {'file_ids': ['file1']}, 'result': {'analysis_ids': ['a1']}})
    at.run()
    assert not at.exception and not at.error
    blocks = [m.value for m in at.markdown]
    assert any('该任务的数据来源' in text and '1 个来源' in text for text in blocks)
    assert any('example.csv' in text for text in blocks)
    assert any('下次提问的数据' in text and '0 个来源' in text for text in blocks)
    assert any('此任务未生成报告文件' in c.value for c in at.caption)
    assert not any('报告已交付' in text for text in blocks)
    assert not next(e for e in at.expander if e.label == '耗时与 Token').proto.expanded


def test_task_database_scope_includes_explicit_zero_tables(file_ui, monkeypatch):
    at, _ = file_ui
    task_ui(at, monkeypatch, {'task_id': 't1', 'status': 'succeeded',
        'spec': {'database_source_ids': ['olist'], 'database_tables': {'olist': []}},
        'result': {}})
    at.run()
    assert not at.exception
    assert any('0 张表' in m.value and 'PostgreSQL 只读' in m.value for m in at.markdown)
    assert not any('全部获准' in m.value for m in at.markdown)


def test_legacy_report_is_discovered_without_rewriting_task(file_ui, monkeypatch):
    at, _ = file_ui
    original = {'task_id': 't1', 'status': 'succeeded', 'spec': {'file_ids': ['missing-file']}}
    task_ui(at, monkeypatch, original, [{'artifact_id': 'legacy', 'name': 'report.html',
        'kind': 'report', 'mime_type': 'text/html', 'downloadable': True}])
    at.run()
    assert not at.exception and not at.error
    assert at.button(key='open_report_t1_legacy')
    assert any('报告已交付' in m.value for m in at.markdown)
    assert any('当前文件信息不可用' in m.value for m in at.markdown)
    missing_card = next(m.value for m in at.markdown if '当前文件信息不可用' in m.value)
    assert '0 B' not in missing_card
    assert 'result' not in original and '_ui_report_ids' not in original


@pytest.mark.parametrize('entry', ['open_report_t1_r1', 'view_result_t1'])
def test_delivered_report_has_direct_preview_navigation_without_generation(file_ui, monkeypatch, entry):
    import streamlit.components.v1 as components
    at, _ = file_ui
    artifact = {'artifact_id': 'r1', 'name': 'report.html', 'kind': 'report',
        'mime_type': 'text/html', 'downloadable': True, 'relative_path': 'report/s1/report.html'}
    task_ui(at, monkeypatch, {'task_id': 't1', 'status': 'succeeded', 'spec': {'file_ids': ['file1']},
        'result': {'report_ids': ['r1']}}, [artifact], [
        {'number': 1, 'title': '原报告', 'request': '生成报告', 'created_at': '2026-10-27T00:00:00Z',
         'version': 'v1', 'artifacts': [artifact]}])
    download = Mock(return_value=b'<html><body>verified report</body></html>')
    submit = Mock()
    embed = Mock()
    monkeypatch.setattr(ApiClient, 'download', download)
    monkeypatch.setattr(ApiClient, 'submit', submit)
    monkeypatch.setattr(components, 'html', embed)
    at.run()
    at.button(key=entry).click().run()
    assert not at.exception and not at.error
    assert at.session_state.workspace_tabs == '文件与报告'
    assert at.session_state.report_preview_id == 'r1'
    assert any('报告已交付' in m.value for m in at.markdown)
    download.assert_called_once_with('r1')
    submit.assert_not_called()
    embed.assert_called_once()


@pytest.mark.parametrize('status', ['failed', 'cancelled', 'waiting_user'])
def test_non_success_never_claims_report_delivered(file_ui, monkeypatch, status):
    at, _ = file_ui
    task_ui(at, monkeypatch, {'task_id': 't1', 'status': status, 'spec': {'expected_outputs': []},
        'result': {'report_ids': ['not_published']}})
    at.run()
    assert not at.exception
    assert not any('报告已交付' in m.value for m in at.markdown)
    assert not any(b.key == 'open_report_t1_not_published' for b in at.button)
