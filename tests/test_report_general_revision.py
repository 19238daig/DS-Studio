import json

import pytest
from bs4 import BeautifulSoup

from src.chart_export import apply_report_theme, export_figure
from src.data import DataService
from src.schemas import TaskSpec
from src.storage import StoreError, file_hash
from test_report_targeted_edits import ranked_report, _manifest


def supplement(store, original, analysis):
    """A real, separately delivered filtered analysis with non-bar SVGs."""
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    fid = next(iter(analysis.input_versions))
    task = TaskSpec(session_id=original.session_id, request='只补充指定图的散点和均值', file_ids=[fid])
    store.create_task(task)
    result = DataService(store).analyze(task, fid, 'aggregate', aggregations={'revenue': 'mean'},
                                      filters=[{'column': 'orders', 'op': 'gt', 'value': 90}])
    for name in ('requested-scatter', 'unrelated-new-line'):
        figure = Figure(figsize=(6, 4)); FigureCanvasAgg(figure); axes = figure.subplots()
        if name == 'requested-scatter':
            axes.scatter([1, 2, 3], [8, 4, 7]); axes.set_title('目标散点图')
        else:
            axes.plot([1, 2, 3], [7, 3, 9]); axes.set_title('不应自动新增的图')
        apply_report_theme(figure)
        path = store.safe_path(f'script_output/{task.session_id}/{task.task_id}/{name}.svg')
        export_figure(figure, path, Figure.savefig)
        figure.savefig(path.with_suffix('.png'))
        artifact = store.register_artifact(path, task.task_id, task.session_id, 'chart', [fid])
        store.register_artifact(path.with_suffix('.png'), task.task_id, task.session_id, 'chart_preview', [artifact.artifact_id])
        result.chart_ids.append(artifact.artifact_id)
    store.save_analysis(result)
    store.update_task(task.task_id, status='succeeded', result={'analysis_ids': [result.analysis_id], 'report_ids': []})
    return result


def html(store, result):
    return BeautifulSoup(next(store.resolve_artifact(aid) for aid in result.report_ids
        if store.get_artifact(aid).name.endswith('.html')).read_text(encoding='utf-8'), 'html.parser')


def test_general_chart_replacement_uses_supplement_and_keeps_non_targets(ranked_report):
    store, service, old, base, task = ranked_report
    new = supplement(store, task, old)
    old_bytes = {aid: file_hash(store.resolve_artifact(aid)) for aid in base.report_ids + old.chart_ids}
    revised = service.generate(task, [old.analysis_id, new.analysis_id], base_report_id=base.report_ids[0],
        revision_patch={'components': [{'kind': 'chart', 'operation': 'replace', 'target_id': old.chart_ids[0],
            'artifact_id': new.chart_ids[0], 'analysis_id': new.analysis_id}]})
    before, after = _manifest(store, base), _manifest(store, revised)
    assert after['presentation'] == before['presentation']
    assert [a['analysis_id'] for a in after['analyses']] == [old.analysis_id, new.analysis_id]
    assert len(html(store, revised).select('figure')) == 2
    assert html(store, revised).select('figure')[1] == html(store, base).select('figure')[1]
    assert new.chart_ids[1] not in html(store, revised).get_text()
    assert after['component_state']['charts'][0]['component_id'] == old.chart_ids[0]
    assert after['component_state']['charts'][0]['artifact_id'] == new.chart_ids[0]
    assert all(file_hash(store.resolve_artifact(aid)) == value for aid, value in old_bytes.items())
    store.update_task(task.task_id, status='succeeded', result=revised.model_dump())
    next_task = TaskSpec(session_id=task.session_id, request='只改结论'); store.create_task(next_task)
    second = service.generate(next_task, revised.analysis_ids, base_report_id=revised.report_ids[0],
        revision_patch={'summary': '这一轮只修改结论。'})
    assert _manifest(store, second)['component_state'] == after['component_state']
    assert html(store, second).select('figure') == html(store, revised).select('figure')
    assert second.validation_summary['revision_integrity']['changed_targets'] == ['summary']


def test_only_explicit_new_table_becomes_visible(ranked_report):
    store, service, old, base, task = ranked_report
    new = supplement(store, task, old)
    revised = service.generate(task, [old.analysis_id, new.analysis_id], base_report_id=base.report_ids[0],
        revision_patch={'components': [{'kind': 'table', 'operation': 'add', 'artifact_id': new.table_ids[0],
            'analysis_id': new.analysis_id}]})
    manifest = _manifest(store, revised)
    assert [s['visible'] for s in manifest['component_state']['tables']] == [False, True]
    assert len(manifest['tables']) == 1 and manifest['tables'][0]['artifact_id'] == new.table_ids[0]
    assert old.table_ids[0] not in [t['artifact_id'] for t in manifest['tables']]
    store.update_task(task.task_id, status='succeeded', result=revised.model_dump())
    next_task = TaskSpec(session_id=task.session_id, request='只改报告标题'); store.create_task(next_task)
    second = service.generate(next_task, revised.analysis_ids, title='新标题', base_report_id=revised.report_ids[0], revision_patch={})
    assert _manifest(store, second)['component_state'] == manifest['component_state']
    assert len(_manifest(store, second)['tables']) == 1


def test_new_section_move_delete_and_explicit_empty_fields(ranked_report):
    store, service, old, base, task = ranked_report
    original = _manifest(store, base)['presentation']['sections']
    revised = service.generate(task, [old.analysis_id], base_report_id=base.report_ids[0], revision_patch={
        'summary': '', 'findings': [], 'limitations': [], 'sections': [{'operation': 'add', 'position': 1,
            'section': {'heading': '补充说明', 'text': '这是新增加的说明。'}},
            {'operation': 'move', 'target_heading': '经营建议', 'position': 0}]})
    presentation = _manifest(store, revised)['presentation']
    assert presentation['summary'] == '' and presentation['findings'] == []
    assert [s['heading'] for s in presentation['sections']] == ['经营建议', '地区分布', '补充说明']
    assert presentation['sections'][0] == original[1] and presentation['sections'][1] == original[0]
    assert html(store, revised).select_one('.report-conclusion').get_text() == ''
    assert '结果限于所选来源' not in html(store, revised).get_text()


def test_new_metric_in_only_one_chapter_is_bound_and_other_text_unchanged(ranked_report):
    store, service, old, base, task = ranked_report
    new = supplement(store, task, old)
    revised = service.generate(task, [old.analysis_id, new.analysis_id], base_report_id=base.report_ids[0], revision_patch={
        'sections': [{'operation': 'update', 'target_heading': '地区分布', 'section': {
            'text': '新口径的营收平均值为{{metric:' + new.analysis_id + ':revenue_mean}}。'}}]})
    before, after = _manifest(store, base)['presentation'], _manifest(store, revised)['presentation']
    assert str(new.metrics['revenue_mean']) in after['sections'][0]['text']
    assert before['summary'] == after['summary'] and before['sections'][1] == after['sections'][1]
    assert html(store, revised).select('figure') == html(store, base).select('figure')


def test_remove_conclusion_is_inherited_and_does_not_return(ranked_report):
    store, service, old, base, task = ranked_report
    first = service.generate(task, [old.analysis_id], base_report_id=base.report_ids[0],
        layout={'include_conclusion': False}, revision_patch={})
    assert not html(store, first).select('[data-section="conclusion"]')
    store.update_task(task.task_id, status='succeeded', result=first.model_dump())
    follow = TaskSpec(session_id=task.session_id, request='再改报告标题'); store.create_task(follow)
    second = service.generate(follow, [old.analysis_id], title='报告新标题', base_report_id=first.report_ids[0], revision_patch={})
    assert not html(store, second).select('[data-section="conclusion"]')


def test_generic_patch_rejects_unused_supplement_duplicate_charts_and_noop(ranked_report):
    store, service, old, base, task = ranked_report
    new = supplement(store, task, old)
    with pytest.raises(StoreError, match='explicitly bind'):
        service.generate(task, [old.analysis_id, new.analysis_id], base_report_id=base.report_ids[0], revision_patch={'summary': '新的说明。'})
    with pytest.raises(StoreError, match='Duplicate'):
        service.generate(task, [old.analysis_id], base_report_id=base.report_ids[0], revision_patch={'components': [
            {'kind': 'chart', 'target_id': old.chart_ids[0], 'artifact_id': old.chart_ids[1], 'analysis_id': old.analysis_id}]})
    with pytest.raises(StoreError) as error:
        service.generate(task, [old.analysis_id], base_report_id=base.report_ids[0], revision_patch={})
    assert error.value.code == 'report_unchanged'


def pin(store, task, base, targets):
    artifact = store.get_artifact(base.report_ids[0])
    task.constraints['report_revision'] = {'base_report_id': artifact.artifact_id,
        'base_report_hash': artifact.hash, 'targets': targets}
    store.update_task(task.task_id, spec=task.model_dump())


def test_declared_revision_scope_rejects_changes_to_other_chapters(ranked_report):
    store, service, old, base, task = ranked_report
    pin(store, task, base, ['section:地区分布'])
    with pytest.raises(StoreError) as error:
        service.generate(task, [old.analysis_id], base_report_id=base.report_ids[0], revision_patch={
            'sections': [{'operation': 'update', 'target_heading': '经营建议', 'section': {'text': '擅自修改其他章节。'}}]})
    assert error.value.code == 'revision_scope_exceeded'
    assert not [a for a in store.list_artifacts(task.task_id) if a.kind == 'report']
    good = service.generate(task, [old.analysis_id], base_report_id=base.report_ids[0], revision_patch={
        'sections': [{'operation': 'update', 'target_heading': '地区分布', 'section': {'text': '只修改目标章节。'}}]})
    assert good.status == 'completed'


def test_declared_layout_scope_can_hide_conclusion(ranked_report):
    store, service, old, base, task = ranked_report
    pin(store, task, base, ['layout:include_conclusion'])
    result = service.generate(task, [old.analysis_id], base_report_id=base.report_ids[0],
        layout={'include_conclusion': False}, revision_patch={})
    assert result.validation_summary['revision_integrity']['changed_targets'] == ['layout:include_conclusion']


def test_completed_analysis_recovered_from_failed_task_stays_readable(ranked_report):
    store, service, old, base, task = ranked_report
    store.update_task(old.task_id, status='failed')
    task.analysis_ids = [old.analysis_id]; store.update_task(task.task_id, spec=task.model_dump())
    recovered = service.generate(task, [old.analysis_id], formats=['html'])
    store.update_task(task.task_id, status='succeeded', result=recovered.model_dump())
    follow = TaskSpec(session_id=task.session_id, request='更换报告标题'); store.create_task(follow)
    read = service.read_report(follow, recovered.report_ids[0])
    assert read['analysis_ids'] == [old.analysis_id]


def quality_base(ranked_report, monkeypatch):
    store, service, analysis, _, task = ranked_report
    fid = next(iter(analysis.input_versions))
    notes = [{'file_id': fid, 'table': 'data', 'text': '缺失值按原口径处理。'},
             {'file_id': fid, 'table': 'data', 'text': '数据为已登记快照。'}]
    monkeypatch.setattr(service, '_source_quality', lambda task, analyses: [dict(note) for note in notes])
    base = service.generate(task, [analysis.analysis_id], formats=['html'])
    store.update_task(task.task_id, status='succeeded', result=base.model_dump())
    follow = TaskSpec(session_id=task.session_id, request='只删除指定质量说明'); store.create_task(follow)
    pin(store, follow, base, ['quality'])
    return store, service, analysis, base, follow


def test_delete_single_quality_note_preserves_source_facts_and_other_notes(ranked_report, monkeypatch):
    store, service, analysis, base, task = quality_base(ranked_report, monkeypatch)
    before = _manifest(store, base)
    revised = service.generate(task, [analysis.analysis_id], base_report_id=base.report_ids[0],
        revision_patch={'quality_edits': [{'index': 0, 'remove': True}]})
    after = _manifest(store, revised)
    assert after['presentation']['quality'] == before['presentation']['quality'][1:]
    assert after['source_quality'] == before['source_quality']
    assert after['analyses'] == before['analyses']
    assert after['presentation']['summary'] == before['presentation']['summary']
    assert revised.validation_summary['revision_integrity']['changed_targets'] == ['quality']


def test_edit_single_quality_note_keeps_provenance_and_requires_bound_numbers(ranked_report, monkeypatch):
    store, service, analysis, base, task = quality_base(ranked_report, monkeypatch)
    revised = service.generate(task, [analysis.analysis_id], base_report_id=base.report_ids[0], revision_patch={
        'quality_edits': [{'index': 1, 'text': '参与分析的记录数为{{metric:' + analysis.analysis_id + ':source_rows}}。'}]})
    before, after = _manifest(store, base), _manifest(store, revised)
    assert after['presentation']['quality'][0] == before['presentation']['quality'][0]
    note = after['presentation']['quality'][1]
    assert note['file_id'] == before['presentation']['quality'][1]['file_id'] and note['table'] == 'data'
    assert '20' in note['text']
    assert after['source_quality'] == before['source_quality']
    rejected = service.generate(task, [analysis.analysis_id], base_report_id=base.report_ids[0],
        revision_patch={'quality_edits': [{'index': 1, 'text': '记录数为999。'}]})
    assert rejected.status == 'need_analysis' and rejected.validation_summary['reason_code'] == 'unbound_prose'
