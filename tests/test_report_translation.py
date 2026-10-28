"""Translations replace visible report text while raw evidence stays immutable."""
import json
from unittest.mock import Mock

import pytest
from bs4 import BeautifulSoup
from openpyxl import load_workbook

from src.agents import run_task
from src.config import Settings
from src.data import DataService
from src.reporting import ReportService
from src.schemas import TaskSpec
from src.storage import Store, StoreError, file_hash
from test_agents import call, models


@pytest.fixture
def translation(tmp_path):
    store = Store(Settings(workspace=tmp_path, _env_file=None))
    sid = store.new_session()['session_id']
    path = tmp_path / 'KB' / 'values.csv'
    path.write_text('pH\n3.1\n3.3\n', encoding='utf-8')
    fid = store.refresh_kb()[0]['file_id']
    task = TaskSpec(session_id=sid, request='生成报告', file_ids=[fid])
    store.create_task(task)
    analysis = DataService(store).analyze(task, fid, 'aggregate', aggregations={'pH': 'mean'})
    analysis.findings = ['Mean pH: 3.2.']
    analysis.limitations = ['Only the pH column is analyzed.', 'Welch t-test assumes independent observations.']
    store.save_analysis(analysis)
    service = ReportService(store)
    original = service.generate(task, [analysis.analysis_id], title='原报告', narrative={
        'summary': '原摘要。', 'sections': [{'heading': 'Original chapter', 'text': 'Original prose.'}]})
    store.update_task(task.task_id, status='succeeded', result=original.model_dump())
    revision = TaskSpec(session_id=sid, request='翻译为中文', analysis_ids=[analysis.analysis_id])
    store.create_task(revision)
    return store, service, original, revision, analysis


def manifest(store, result):
    return json.loads(store.resolve_artifact(result.validation_summary['manifest_id']).read_text(encoding='utf-8'))


def test_translations_replace_in_both_formats_and_survive_later_edits(translation):
    store, service, original, task, analysis = translation
    base = service.read_report(task, original.report_ids[0])
    old_hashes = {rid: file_hash(store.resolve_artifact(rid)) for rid in original.report_ids}
    quality = base['presentation']['quality']
    changes = [
        dict(field='findings', original='Mean pH: 3.2.', replacement='pH均值：3.2。'),
        dict(field='limitations', original=analysis.limitations[0], replacement='仅分析pH列。'),
        dict(field='limitations', original=analysis.limitations[1], replacement='Welch t检验假设观测相互独立。'),
        dict(field='section_heading', original='Original chapter', replacement='原有章节'),
        dict(field='section_text', original='Original prose.', replacement='原有正文。'),
    ]
    # Keep every original numeric lexeme in source-bound quality translations.
    for row in quality:
        changes.append(dict(field='quality', original=row['text'], replacement='质量说明：' + row['text'].replace('Missing cells by column', '各列缺失单元格').replace('Duplicate full rows', '整行重复数量').replace('all records parsed', '全部记录已解析')))
    result = service.generate(task, [analysis.analysis_id], base_report_id=base['report_id'], text_replacements=changes)
    html = store.resolve_artifact(result.report_ids[0]).read_text(encoding='utf-8')
    soup = BeautifulSoup(html, 'html.parser')
    assert 'Mean pH: 3.2.' not in soup.get_text() and 'pH均值：3.2。' in soup.get_text()
    assert all(text not in soup.get_text() for text in analysis.limitations)
    assert '仅分析pH列。' in soup.get_text()
    assert 'Original chapter' not in soup.get_text() and '原有章节' in soup.get_text()
    assert 'Original prose.' not in soup.get_text() and '原有正文。' in soup.get_text()
    book = load_workbook(store.resolve_artifact(result.report_ids[1]), read_only=True)
    try:
        text = '\n'.join(str(cell) for sheet in book for row in sheet.values for cell in row)
        assert 'Only the pH column' not in text and '仅分析pH列。' in text
        assert 'Mean pH: 3.2.' not in text and 'pH均值：3.2。' in text
    finally:
        book.close()
    data = manifest(store, result)
    assert data['analyses'][0]['limitations'] == analysis.limitations
    assert data['analyses'][0]['findings'] == analysis.findings
    assert data['source_quality'] == quality
    assert data['text_replacements'] == changes
    assert data['metric_bindings'] == manifest(store, original)['metric_bindings']
    assert old_hashes == {rid: file_hash(store.resolve_artifact(rid)) for rid in original.report_ids}
    store.update_task(task.task_id, status='succeeded', result=result.model_dump())
    next_task = TaskSpec(session_id=task.session_id, request='仅修改标题')
    store.create_task(next_task)
    second = service.generate(next_task, [analysis.analysis_id], title='修改后的标题', base_report_id=result.report_ids[0])
    assert manifest(store, second)['presentation']['limitations'] == ['仅分析pH列。', 'Welch t检验假设观测相互独立。']
    assert manifest(store, second)['presentation']['quality'] == data['presentation']['quality']
    assert manifest(store, second)['presentation']['findings'] == ['pH均值：3.2。']
    assert manifest(store, second)['presentation']['sections'] == data['presentation']['sections']


def test_explicit_revised_limitations_replace_instead_of_merge_and_preserve_other_prose(translation):
    store, service, original, task, analysis = translation
    result = service.generate(task, [analysis.analysis_id], base_report_id=original.report_ids[0],
        narrative={'limitations': ['仅分析pH列。', 'Welch t检验假设观测相互独立。']})
    data = manifest(store, result)
    assert data['presentation']['limitations'] == ['仅分析pH列。', 'Welch t检验假设观测相互独立。']
    assert data['presentation']['summary'] == manifest(store, original)['presentation']['summary']
    assert data['presentation']['sections'] == manifest(store, original)['presentation']['sections']
    assert data['analyses'][0]['limitations'] == analysis.limitations


@pytest.mark.parametrize('original,replacement,code', [
    ('Mean pH: 3.2.', 'pH均值：3.3。', 'translation_number_changed'),
    ('Mean pH: 3.2.', 'pH均值。', 'translation_number_changed'),
    ('Mean pH: 3.2.', 'pH均值：3.2，样本量100。', 'translation_number_changed'),
    ('Unknown text', '任意译文', 'text_not_found'),
])
def test_invalid_translation_is_atomic_and_keeps_original(translation, original, replacement, code):
    store, service, source, task, analysis = translation
    old = file_hash(store.resolve_artifact(source.report_ids[0]))
    with pytest.raises(StoreError) as error:
        service.generate(task, [analysis.analysis_id], base_report_id=source.report_ids[0], text_replacements=[
            dict(field='limitations', original=analysis.limitations[0], replacement='仅分析pH列。'),
            dict(field='findings', original=original, replacement=replacement)])
    assert error.value.code == code
    assert not store.list_artifacts(task.task_id)
    assert file_hash(store.resolve_artifact(source.report_ids[0])) == old


def test_existing_translated_duplicate_is_collapsed_instead_of_appended(translation):
    store, service, original, task, analysis = translation
    bilingual = service.generate(task, [analysis.analysis_id], base_report_id=original.report_ids[0],
        narrative={'limitations': [analysis.limitations[0], '仅分析pH列。']})
    store.update_task(task.task_id, status='succeeded', result=bilingual.model_dump())
    next_task = TaskSpec(session_id=task.session_id, request='替换英文')
    store.create_task(next_task)
    result = service.generate(next_task, [analysis.analysis_id], base_report_id=bilingual.report_ids[0], text_replacements=[
        dict(field='limitations', original=analysis.limitations[0], replacement='仅分析pH列。')])
    assert manifest(store, result)['presentation']['limitations'] == ['仅分析pH列。']


def test_deep_agent_translation_routes_to_report_tool_without_analysis(translation):
    store, _, source, task, analysis = translation
    scripted, factory = models([call('task', {'subagent_type': 'report_designer', 'description': 'Translate limitations in place.'})],
        report=[call('read_report', {}), call('revise_report', {'text_replacements': [
            dict(field='limitations', original=analysis.limitations[0], replacement='仅分析pH列。')]} , 'translate')])
    result = run_task(task, store, Mock(), lambda: False, model_factory=factory)
    assert result['status'] == 'completed' and result['report_ids']
    assert '已用译文替换指定原文' in result['final_response']
    assert result['report_revision']['text_replacements_applied'] == 1
    assert store.get_analysis(analysis.analysis_id, task.session_id).limitations == analysis.limitations
    assert not list((store.root / 'script_output' / task.session_id / task.task_id).glob('analysis_*.json'))
    assert any(task.request in str(message.content) for batch in scripted['report_designer']._seen for message in batch
               if message.type == 'human' and str(message.content).startswith('APP_STATE:'))


@pytest.mark.parametrize('conflict', [False, True])
def test_repeated_mapping_is_idempotent_but_conflicting_translation_is_rejected(translation, conflict):
    store, service, original, task, analysis = translation
    item = dict(field='limitations', original=analysis.limitations[0], replacement='仅分析pH列。')
    changes = [item, {**item, 'replacement': '只分析pH列。'} if conflict else dict(item)]
    if conflict:
        with pytest.raises(StoreError) as error:
            service.generate(task, [analysis.analysis_id], base_report_id=original.report_ids[0], text_replacements=changes)
        assert error.value.code == 'invalid_text_replacement'
        assert not store.list_artifacts(task.task_id)
    else:
        result = service.generate(task, [analysis.analysis_id], base_report_id=original.report_ids[0], text_replacements=changes)
        assert result.validation_summary['text_replacements_applied'] == 1
        assert manifest(store, result)['presentation']['limitations'].count('仅分析pH列。') == 1
