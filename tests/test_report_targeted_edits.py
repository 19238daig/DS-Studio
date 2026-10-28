import json
from pathlib import Path

import pytest
from bs4 import BeautifulSoup

from src.chart_export import apply_report_theme, configure_chart_fonts, export_figure
from src.config import Settings
from src.data import DataService
from src.reporting import ReportService
from src.schemas import TaskSpec
from src.storage import Store, StoreError, file_hash


@pytest.fixture
def ranked_report(tmp_path):
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    store = Store(Settings(workspace=tmp_path, _env_file=None))
    sid = store.new_session()['session_id']
    (tmp_path / 'KB' / 'states.csv').write_text('state,revenue,orders\n' + ''.join(
        f'S{i:02},{100000 - i * 125.25},{100 - i}\n' for i in range(20)), encoding='utf-8')
    fid = store.refresh_kb()[0]['file_id']
    original = TaskSpec(session_id=sid, request='各州订单和营收报告', file_ids=[fid])
    store.create_task(original)
    data = DataService(store)
    analysis = data.analyze(original, fid, 'rows')
    configure_chart_fonts()
    for name, count in [('top15', 15), ('unchanged', 20)]:
        fig = Figure(figsize=(8, 6)); FigureCanvasAgg(fig)
        ax = fig.subplots()
        indices = list(reversed(range(count)))
        ax.barh([f'S{i:02}' for i in indices], [100000 - i * 125.25 for i in indices], color='#dd8452')
        ax.set_xlabel('营收'); ax.set_ylabel('州'); ax.set_title('Top 15 州营收排名' if name == 'top15' else '原样保留图')
        apply_report_theme(fig); fig.tight_layout()
        path = store.safe_path(f'script_output/{sid}/{original.task_id}/{name}.svg')
        export_figure(fig, path, Figure.savefig)
        png = path.with_suffix('.png'); fig.savefig(png)
        art = store.register_artifact(path, original.task_id, sid, 'chart', [fid])
        store.register_artifact(png, original.task_id, sid, 'chart_preview', [art.artifact_id])
        analysis.chart_ids.append(art.artifact_id)
    store.save_analysis(analysis)
    service = ReportService(store)
    base = service.generate(original, [analysis.analysis_id], '地区报告',
        {'summary': '营收集中在主要州。', 'sections': [
            {'heading': '地区分布', 'text': '原本的地区分布说明。'},
            {'heading': '经营建议', 'text': '保留这一段建议。'}]}, ['html', 'xlsx'])
    store.update_task(original.task_id, status='succeeded', result=base.model_dump())
    revision = TaskSpec(session_id=sid, request='报告Top15改Top10')
    store.create_task(revision)
    return store, service, analysis, base, revision


def _manifest(store, result):
    return json.loads(store.resolve_artifact(result.validation_summary['manifest_id']).read_text(encoding='utf-8'))


def _chart_marks(store, result, index=0):
    path = next(store.resolve_artifact(aid) for aid in result.report_ids if store.get_artifact(aid).name.endswith('.html'))
    soup = BeautifulSoup(path.read_text(encoding='utf-8'), 'html.parser')
    return soup.select('figure.chart-card')[index].select('[data-label]')


def test_top15_to_top10_changes_actual_data_only_and_preserves_original(ranked_report):
    store, service, analysis, base, task = ranked_report
    old_hashes = {a.artifact_id: file_hash(store.resolve_artifact(a.artifact_id)) for a in store.list_artifacts(analysis.task_id)}
    before = _manifest(store, base)
    changed = service.generate(task, [analysis.analysis_id], base_report_id=base.report_ids[0],
        chart_changes=[{'chart_id': analysis.chart_ids[0], 'top_n': 10}])
    after = _manifest(store, changed)
    marks = _chart_marks(store, changed)
    assert len(marks) == 10
    assert all('S1' not in mark['data-label'].split(':')[0] for mark in marks)
    assert {m['data-label'].split(':')[0] for m in marks} == {f'S{i:02}' for i in range(10)}
    assert all(before['presentation'][k] == after['presentation'][k] for k in before['presentation'])
    assert before['analyses'] == after['analyses']
    assert len(after['chart_overrides']) == 1 and after['chart_overrides'][0]['spec']['top_n'] == 10
    assert _chart_marks(store, changed, 1) == _chart_marks(store, base, 1)
    assert changed.validation_summary['chart_changes'][0]['business_queries_rerun'] is False
    assert old_hashes == {aid: file_hash(store.resolve_artifact(aid)) for aid in old_hashes}
    assert not after['presentation']['layout']['include_tables']


def test_next_revision_inherits_top10_then_can_expand_to_top18(ranked_report):
    store, service, analysis, base, task = ranked_report
    changed = service.generate(task, [analysis.analysis_id], base_report_id=base.report_ids[0],
        chart_changes=[{'chart_id': analysis.chart_ids[0], 'top_n': 10}])
    store.update_task(task.task_id, status='succeeded', result=changed.model_dump())
    second = TaskSpec(session_id=task.session_id, request='只改报告标题'); store.create_task(second)
    title_only = service.generate(second, [analysis.analysis_id], title='管理层报告', base_report_id=changed.report_ids[0])
    assert len(_chart_marks(store, title_only)) == 10
    assert _manifest(store, title_only)['chart_overrides'] == _manifest(store, changed)['chart_overrides']
    store.update_task(second.task_id, status='succeeded', result=title_only.model_dump())
    third = TaskSpec(session_id=task.session_id, request='改为Top18'); store.create_task(third)
    expanded = service.generate(third, [analysis.analysis_id], base_report_id=title_only.report_ids[0],
        chart_changes=[{'chart_id': analysis.chart_ids[0], 'top_n': 18}])
    assert len(_chart_marks(store, expanded)) == 18
    assert _manifest(store, expanded)['presentation']['title'] == '管理层报告'
    assert _manifest(store, expanded)['chart_overrides'][0]['spec']['title'] == 'Top 18 州营收排名'


def test_target_chapter_text_and_removal_preserve_other_chapters(ranked_report):
    store, service, analysis, base, task = ranked_report
    edited = service.generate(task, [analysis.analysis_id], base_report_id=base.report_ids[0],
        section_edits=[{'section_heading': '地区分布', 'text': '仅改这里的说明。'}])
    presentation = _manifest(store, edited)['presentation']
    assert presentation['sections'][0]['text'] == '仅改这里的说明。'
    assert presentation['sections'][1] == _manifest(store, base)['presentation']['sections'][1]
    assert not edited.validation_summary['chart_changes']
    removed = service.generate(task, [analysis.analysis_id], base_report_id=base.report_ids[0],
        section_edits=[{'section_heading': '地区分布', 'remove': True}])
    assert [s['heading'] for s in _manifest(store, removed)['presentation']['sections']] == ['经营建议']


@pytest.mark.parametrize('edit,code', [
    ({'chart_id': 'not-in-report', 'top_n': 10}, 'artifact_not_authorized'),
    ({'top_n': 10, 'title': 'Top 10 销售增长99%'}, 'unbound_chart_title'),
    ({'top_n': 10, 'title': 'Top 15 州营收'}, 'invalid_chart_edit'),
    ({'top_n': 10, 'value_column': 'orders'}, 'chart_edit_source_required'),
    ({'top_n': 10, 'category_column': 'orders'}, 'chart_edit_source_required'),
])
def test_invalid_target_and_false_claims_are_not_published(ranked_report, edit, code):
    store, service, analysis, base, task = ranked_report
    args = {'chart_id': analysis.chart_ids[0], **edit}
    if code == 'chart_edit_source_required':
        result = service.generate(task, [analysis.analysis_id], base_report_id=base.report_ids[0], chart_changes=[args])
        assert result.status == 'need_analysis' and result.validation_summary['reason_code'] == code
    else:
        with pytest.raises(StoreError) as exc:
            service.generate(task, [analysis.analysis_id], base_report_id=base.report_ids[0], chart_changes=[args])
        assert exc.value.code == code
    assert not [a for a in store.list_artifacts(task.task_id) if a.kind in {'report', 'report_chart'}]


def test_noop_and_tampered_derived_chart_fail_closed(ranked_report):
    store, service, analysis, base, task = ranked_report
    with pytest.raises(StoreError) as exc:
        service.generate(task, [analysis.analysis_id], base_report_id=base.report_ids[0],
            chart_changes=[{'chart_id': analysis.chart_ids[0], 'top_n': 15}])
    assert exc.value.code == 'report_unchanged'
    edited = service.generate(task, [analysis.analysis_id], base_report_id=base.report_ids[0],
        chart_changes=[{'chart_id': analysis.chart_ids[0], 'top_n': 10}])
    store.update_task(task.task_id, status='succeeded', result=edited.model_dump())
    ref = _manifest(store, edited)['chart_overrides'][0]
    store.resolve_artifact(ref['artifact_id']).write_text('<svg/>', encoding='utf-8')
    second = TaskSpec(session_id=task.session_id, request='再改标题'); store.create_task(second)
    with pytest.raises(StoreError):
        service.read_report(second, edited.report_ids[0])


def test_unbound_number_in_single_chapter_still_requires_metric(ranked_report):
    _, service, analysis, base, task = ranked_report
    result = service.generate(task, [analysis.analysis_id], base_report_id=base.report_ids[0],
        section_edits=[{'section_heading': '地区分布', 'text': '销售额为99999。'}])
    assert result.status == 'need_analysis' and result.validation_summary['reason_code'] == 'unbound_prose'


def test_chart_color_edit_and_failed_publish_cleanup(ranked_report, monkeypatch):
    store, service, analysis, base, task = ranked_report
    edited = service.generate(task, [analysis.analysis_id], base_report_id=base.report_ids[0],
        chart_changes=[{'chart_id': analysis.chart_ids[0], 'color': '#008080'}])
    assert _manifest(store, edited)['chart_overrides'][0]['spec']['color'] == '#008080'
    before = {a.artifact_id for a in store.list_artifacts(task.task_id)}
    def fail(*args, **kwargs):
        raise RuntimeError('intentional report validation failure')
    monkeypatch.setattr(service, '_validate', fail)
    with pytest.raises(RuntimeError):
        service.generate(task, [analysis.analysis_id], base_report_id=base.report_ids[0],
            chart_changes=[{'chart_id': analysis.chart_ids[0], 'top_n': 9}])
    assert {a.artifact_id for a in store.list_artifacts(task.task_id)} == before


def test_color_only_agent_reply_does_not_claim_data_or_title_changed(ranked_report):
    from unittest.mock import Mock
    from src.agents import run_task
    from test_agents import call, models

    store, _, analysis, base, task = ranked_report
    task.request = '仅把报告中的指定图表改为蓝色，其他内容保持不变。'
    scripted, factory = models([
        call('task', {'subagent_type': 'report_designer', 'description': task.request})],
        report=[call('revise_report', {'report_id': base.report_ids[0], 'chart_changes': [
            {'chart_id': analysis.chart_ids[0], 'color': '#2563EB'}]})])
    result = run_task(task, store, Mock(), lambda: False, model_factory=factory)
    assert result['status'] == 'completed', result
    assert '已更新指定图表。' in result['final_response']
    assert '数据范围、标题' not in result['final_response']
    assert '图中类别已同步更新' not in result['final_response']
    assert scripted['analyst']._index == 0
    manifest = json.loads(store.resolve_artifact(result['report_revision']['manifest_id']).read_text(encoding='utf-8'))
    assert manifest['chart_overrides'][0]['spec']['color'] == '#2563EB'
    assert manifest['chart_overrides'][0]['spec']['top_n'] == 15


@pytest.mark.parametrize('size,code', [
    ([float('nan'), 8], 'invalid_chart_geometry'),
    ([float('inf'), 8], 'invalid_chart_geometry'),
    ([0, 8], 'invalid_chart_geometry'),
    ([-1, 8], 'invalid_chart_geometry'),
    ([True, 8], 'invalid_chart_geometry'),
    (['10', 8], 'invalid_chart_geometry'),
    ([100000, 8], 'chart_edit_resource_limit'),
    ([50, 50], 'chart_edit_resource_limit'),
])
def test_edit_geometry_rejected_before_any_canvas_allocation(size, code):
    from src.report_edits import validate_geometry
    with pytest.raises(StoreError) as exc:
        validate_geometry({'figsize': size})
    assert exc.value.code == code


def test_legacy_viewbox_and_inherited_edit_cannot_bypass_preview_limits(ranked_report):
    from lxml import etree
    from src.report_edits import _bar_profile, plan_edit, validate_override
    from src.schemas import ReportChartEdit

    store, service, analysis, base, task = ranked_report
    cid = analysis.chart_ids[0]
    root = etree.fromstring(store.resolve_artifact(cid).read_bytes())
    root.set('viewBox', '0 0 10000000 576')
    # This exercises recognition only, without attempting the 25-billion-pixel raster.
    with pytest.raises(StoreError) as exc:
        _bar_profile(etree.tostring(root))
    assert exc.value.code == 'chart_edit_resource_limit'

    edited = service.generate(task, [analysis.analysis_id], base_report_id=base.report_ids[0],
        chart_changes=[{'chart_id': cid, 'top_n': 10}])
    override = _manifest(store, edited)['chart_overrides'][0]
    override['spec']['figsize'] = [100000, 8]
    with pytest.raises(StoreError) as exc:
        validate_override(store, task, [analysis], override)
    assert exc.value.code == 'chart_edit_resource_limit'
    with pytest.raises(StoreError) as exc:
        plan_edit(store, cid, [analysis], ReportChartEdit(chart_id=cid, color='#2563EB'), override)
    assert exc.value.code == 'chart_edit_resource_limit'


def test_chart_title_cannot_permute_numeric_literal_or_add_chinese_claim(ranked_report):
    from src.report_edits import plan_edit
    from src.schemas import ReportChartEdit

    store, _, analysis, _, _ = ranked_report
    cid = analysis.chart_ids[0]
    spec = plan_edit(store, cid, [analysis], ReportChartEdit(chart_id=cid, top_n=10))
    spec['title'] = 'Top 10 2026年州营收'
    for replacement in ['Top 10 2062年州营收', 'Top 10 2026年州营收增长百分之九十九']:
        with pytest.raises(StoreError) as exc:
            plan_edit(store, cid, [analysis], ReportChartEdit(chart_id=cid, title=replacement), {'spec': spec})
        assert exc.value.code == 'unbound_chart_title'
