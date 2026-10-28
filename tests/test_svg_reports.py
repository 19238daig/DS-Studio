"""Real vector output, offline interaction boundary and Docker export coverage."""
import json
import subprocess

import pytest
from bs4 import BeautifulSoup
from lxml import etree
from openpyxl import load_workbook

from src.chart_svg import chart_csp, inline_svg, interaction_script, parse_svg
from src.config import Settings
from src.data import DataService
from src.execution import DockerExecutor
from src.reporting import ReportService
from src.schemas import TaskSpec
from src.storage import Store, StoreError


@pytest.fixture
def context(tmp_path):
    store = Store(Settings(workspace=tmp_path, _env_file=None))
    sid = store.new_session()['session_id']
    (store.root / 'KB' / 'values.csv').write_text('name,x,y\na,1,3\nb,2,7\nc,3,9\n', encoding='utf-8')
    fid = store.refresh_kb()[0]['file_id']
    task = TaskSpec(session_id=sid, request='SVG report', file_ids=[fid])
    store.create_task(task)
    return store, task, fid


@pytest.mark.parametrize('kind', ['bar', 'line', 'hist', 'scatter'])
def test_all_builtin_charts_vector_and_excel_preview(context, kind):
    store, task, fid = context
    a = DataService(store).analyze(task, fid, 'rows', chart=kind)
    out = ReportService(store).generate(task, [a.analysis_id])
    assert out.status == 'completed'
    paths = {store.resolve_artifact(aid).suffix: store.resolve_artifact(aid) for aid in out.report_ids}
    soup = BeautifulSoup(paths['.html'].read_text(encoding='utf-8'), 'html.parser')
    svg = soup.select_one('svg[data-ds-chart]')
    assert svg and svg.select('[data-label]')
    assert not svg.find(['image', 'script', 'foreignObject']) and not soup.find('img')
    assert soup.select_one('script#ds-chart-interactions').get_text() == interaction_script()
    assert soup.find('meta', attrs={'http-equiv': 'Content-Security-Policy'})['content'] == chart_csp()
    assert 'unsafe-inline' not in chart_csp().split('script-src')[1].split(';')[0]
    book = load_workbook(paths['.xlsx'])
    assert len(book['图表']._images) == 1
    book.close()
    manifest = json.loads(store.resolve_artifact(out.validation_summary['manifest_id']).read_text(encoding='utf-8'))
    assert manifest['chart_rendering'] == 'inline-svg-interactive-v2'
    assert any(x['kind'] == 'chart_svg' for x in manifest['source_artifacts'])


@pytest.mark.parametrize('payload', [
    '<script>alert(1)</script>', '<foreignObject/>', '<image href="data:image/png;base64,AA"/>',
    '<g onclick="alert(1)"/>', '<use href="https://example.test/a.svg#x"/>',
    '<path style="fill:url(https://example.test)"/>', '<style>@import "https://example.test";</style>',
    '<path style="position:fixed"/>', '<style>*{fill: u\\72l(https://example.test)}</style>',
    '<use href="#missing"/>', '<g id="same"/><g id="same"/>',
])
def test_active_raster_external_svg_rejected(payload):
    with pytest.raises(ValueError):
        inline_svg(('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20">' + payload + '</svg>').encode(), 'safe')


def test_svg_entities_rejected_and_ids_scoped():
    with pytest.raises(ValueError):
        parse_svg(b'<!DOCTYPE svg [<!ENTITY x SYSTEM "file:///etc/passwd">]><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1 1">&x;</svg>')
    raw = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20"><defs><path id="p" d="M0 0L1 1"/></defs><use href="#p"/></svg>'
    first, second = str(inline_svg(raw, 'one')), str(inline_svg(raw, 'two'))
    assert 'chart_one_p' in first and 'chart_two_p' in second
    assert 'href="#chart_one_p"' in first


def test_chart_companion_hash_checked_and_legacy_never_fake_vector(context):
    store, task, fid = context
    a = DataService(store).analyze(task, fid, 'rows', chart='bar')
    svg = next(x for x in store.list_artifacts(task.task_id) if x.kind == 'chart_svg')
    path = store.resolve_artifact(svg.artifact_id)
    path.write_text('tampered')
    with pytest.raises(StoreError):
        ReportService(store).generate(task, [a.analysis_id])
    index = store._index('artifacts')
    index.pop(svg.artifact_id)
    store._save_index('artifacts', index)
    out = ReportService(store).generate(task, [a.analysis_id])
    assert out.status == 'need_analysis' and out.validation_summary['reason_code'] == 'missing_vector_chart'
    assert not list((store.root / 'report').rglob('report.html'))
    # Old PNG-only analyses can still produce an Excel or a chart-free report.
    assert ReportService(store).generate(task, [a.analysis_id], formats=['xlsx']).status == 'completed'
    assert ReportService(store).generate(task, [a.analysis_id], layout={'include_charts': False}).status == 'completed'


def test_real_docker_png_and_svg_saves_both_become_interactive_vectors(context):
    if subprocess.run(['docker', 'image', 'inspect', 'ds-analysis:local'], capture_output=True).returncode:
        pytest.skip('Docker image unavailable')
    store, task, fid = context
    code = '''import json
import matplotlib.pyplot as plt
fig, ax = plt.subplots()
ax.boxplot([[1, 2, 4], [3, 5, 6]], tick_labels=['A','B'])
fig.savefig('/output/box.png')
fig2, ax2 = plt.subplots()
ax2.plot([1, 2, 3], [3, 7, 9], label='Trend')
fig2.savefig('/output/line.svg')
fig3, ax3 = plt.subplots()
ax3.hist([[1,2,4], [3,5,6]], bins=4, label=['Red Wine', 'White Wine'])
fig3.savefig('/output/hist.svg')
json.dump({'metrics':{'mean':7/3}, 'metric_definitions':{'mean':'Mean of [1,2,4]'},
'tables':[], 'charts':['box.png','line.svg','hist.svg'], 'findings':[], 'limitations':[]}, open('/output/result.json','w'))
'''
    a = DockerExecutor(store).run_python(task, code)
    result = ReportService(store).generate(task, [a.analysis_id])
    assert result.status == 'completed'
    path = next(store.resolve_artifact(x) for x in result.report_ids if store.resolve_artifact(x).suffix == '.html')
    soup = BeautifulSoup(path.read_text(encoding='utf-8'), 'html.parser')
    assert len(soup.select('svg[data-ds-chart]')) == 3 and not soup.find('image')
    hist = soup.select('svg[data-ds-chart]')[2]
    assert {n['data-series'] for n in hist.select('[data-series]')} == {'0:Red Wine', '0:White Wine'}
    ids = [x['id'] for x in soup.select('svg [id]')]
    assert len(ids) == len(set(ids))


def test_rasterized_svg_is_not_accepted_as_vector(context):
    import matplotlib.pyplot as plt
    from matplotlib.figure import Figure
    from src.chart_export import export_figure
    store, _, _ = context
    fig, ax = plt.subplots()
    ax.imshow([[1, 2], [3, 4]])
    try:
        with pytest.raises(ValueError, match='vector'):
            export_figure(fig, store.root / 'bad.svg', Figure.savefig)
        assert not (store.root / 'bad.svg').exists()
    finally:
        plt.close(fig)


def test_offline_browser_click_series_zoom_pan_and_reset(context):
    playwright = pytest.importorskip('playwright.sync_api')
    from pathlib import Path
    edge = Path('C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe')
    if not edge.exists():
        pytest.skip('Edge browser unavailable')
    store, task, fid = context
    analyses = [DataService(store).analyze(task, fid, 'rows', chart=kind) for kind in ('bar', 'line', 'scatter')]
    result = ReportService(store).generate(task, [a.analysis_id for a in analyses], formats=['html'])
    path = store.resolve_artifact(result.report_ids[0])
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(executable_path=str(edge), headless=True)
        page = browser.new_page(viewport={'width':1280, 'height':1000}, offline=True)
        errors, requests = [], []
        page.on('pageerror', lambda err: errors.append(str(err)))
        page.on('request', lambda req: requests.append(req.url))
        try:
            page.goto(path.as_uri())
            page.wait_for_selector('[data-interactive-ready="true"]')
            assert page.locator('[data-interactive-ready="true"]').count() == 3
            card = page.locator('[data-chart-card]').first
            svg = card.locator('svg')
            original = svg.get_attribute('viewBox')
            card.locator('g[data-label] path').first.click()
            assert 'y=' in card.locator('[data-chart-detail]').inner_text()
            toggle = card.locator('[data-series-toggle]').first
            toggle.click()
            assert toggle.get_attribute('aria-pressed') == 'false'
            assert card.locator('.ds-series-hidden').count() > 0
            toggle.click()
            assert card.locator('.ds-series-hidden').count() == 0
            card.locator('[data-zoom-in]').click()
            assert svg.get_attribute('viewBox') != original
            box = svg.bounding_box()
            page.mouse.move(box['x'] + box['width']/2, box['y'] + box['height']/2)
            page.mouse.down()
            page.mouse.move(box['x'] + box['width']/2 + 45, box['y'] + box['height']/2 + 20, steps=5)
            page.mouse.up()
            assert float(svg.get_attribute('viewBox').split()[0]) != float(original.split()[0])
            card.locator('[data-chart-reset]').click()
            assert svg.get_attribute('viewBox') == original
            scatter = page.locator('[data-chart-card]').nth(2)
            scatter.locator('use[data-label]').first.click(force=True)
            assert 'x=1' in scatter.locator('[data-chart-detail]').inner_text()
            assert not errors and not [url for url in requests if url.startswith(('http:', 'https:'))]
        finally:
            browser.close()


def test_polished_hover_pin_series_focus_modal_keyboard_and_export(context, tmp_path):
    playwright = pytest.importorskip('playwright.sync_api')
    from pathlib import Path
    from src.chart_export import apply_report_theme, export_figure
    from src.schemas import AnalysisResult
    import matplotlib.pyplot as plt
    from matplotlib.figure import Figure
    edge = Path('C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe')
    if not edge.exists():
        pytest.skip('Edge browser unavailable')
    store, task, fid = context
    path = store.safe_path(f'script_output/{task.session_id}/{task.task_id}/theme.png')
    path.parent.mkdir(parents=True, exist_ok=True)
    fig, ax = plt.subplots(figsize=(8, 4))
    ax.plot([1,2,3], [3,7,9], label='Primary')
    ax.plot([1,2,3], [1,2,3], label='Reference')
    ax.set(xlabel='x', ylabel='y', title='Two series comparison')
    ax.legend()
    apply_report_theme(fig)
    fig.tight_layout()
    try:
        fig.savefig(path)
        export_figure(fig, path.with_suffix('.svg'), Figure.savefig, bbox_inches='tight')
    finally:
        plt.close(fig)
    chart = store.register_artifact(path, task.task_id, task.session_id, 'chart', [fid])
    store.register_artifact(path.with_suffix('.svg'), task.task_id, task.session_id, 'chart_svg', [chart.artifact_id])
    analysis = store.save_analysis(AnalysisResult(task_id=task.task_id, session_id=task.session_id,
        input_versions={fid:store.get_file(fid, task.session_id)['hash']}, chart_ids=[chart.artifact_id],
        metrics={'rows':3}, metric_definitions={'rows':'Rows in complete fixture'}))
    result = ReportService(store).generate(task, [analysis.analysis_id], formats=['html'])
    html = store.resolve_artifact(result.report_ids[0])
    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(executable_path=str(edge), headless=True)
        page = browser.new_page(viewport={'width':1280,'height':1000}, offline=True, accept_downloads=True)
        errors = []
        page.on('pageerror', lambda e: errors.append(str(e)))
        try:
            page.goto(html.as_uri())
            card = page.locator('[data-interactive-ready="true"]')
            card.wait_for()
            svg = card.locator('svg[data-ds-chart]')
            svg.scroll_into_view_if_needed()
            # Tight-bbox coordinates must still identify the actual middle point.
            point = svg.evaluate('''svg => {
              const g=svg.querySelector('[data-points]'), p=JSON.parse(g.dataset.points)[1];
              const pt=svg.createSVGPoint();pt.x=p[0];pt.y=p[1];
              const screen=pt.matrixTransform(svg.getScreenCTM());return [screen.x,screen.y];
            }''')
            page.mouse.move(*point)
            playwright.expect(card.locator('[data-chart-tooltip]')).to_be_visible()
            assert 'y=7' in card.locator('[data-chart-tooltip]').inner_text()
            page.mouse.click(*point)
            assert 'y=7' in card.locator('[data-chart-detail]').inner_text()
            assert card.locator('[data-selection-status]').inner_text() == '已固定'
            toggles = card.locator('[data-series-toggle]')
            toggles.nth(1).dblclick()
            assert toggles.nth(0).get_attribute('aria-pressed') == 'false'
            assert toggles.nth(1).get_attribute('aria-pressed') == 'true'
            with page.expect_download() as download:
                card.locator('[data-chart-export]').click()
            exported = tmp_path / download.value.suggested_filename
            download.value.save_as(exported)
            root = parse_svg(exported.read_bytes())
            names = {n.get('data-series') for n in root.iter() if n.get('data-series')}
            assert names == {'0:Reference'}
            assert not root.xpath('//*[@data-interaction-layer]')
            card.locator('[data-chart-show-all]').click()
            assert card.locator('.ds-series-hidden').count() == 0
            initial = svg.get_attribute('viewBox')
            svg.focus(); page.keyboard.press('+')
            assert svg.get_attribute('viewBox') != initial
            page.keyboard.press('0')
            assert svg.get_attribute('viewBox') == initial
            card.locator('[data-chart-expand]').click()
            playwright.expect(page.locator('dialog.chart-modal')).to_be_visible()
            assert card.locator('[data-chart-expand]').get_attribute('aria-expanded') == 'true'
            page.keyboard.press('Escape')
            playwright.expect(page.locator('dialog.chart-modal')).to_have_count(0)
            assert page.locator('.chart-grid > [data-chart-card]').count() == 1
            svg.scroll_into_view_if_needed()
            box = svg.bounding_box()
            page.mouse.move(box['x'] + box['width']/2, box['y'] + box['height']/2)
            page.mouse.wheel(0, 80)
            assert svg.get_attribute('viewBox') == initial
            svg.scroll_into_view_if_needed()
            box = svg.bounding_box()
            page.mouse.move(box['x'] + box['width']/2, box['y'] + box['height']/2)
            page.keyboard.down('Control'); page.mouse.wheel(0, -80); page.keyboard.up('Control')
            playwright.expect(svg).not_to_have_attribute('viewBox', initial)
            card.locator('[data-chart-reset]').click()
            page.set_viewport_size({'width':390,'height':844})
            assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth + 1')
            assert not errors
        finally:
            browser.close()


def test_visual_upgrade_keeps_original_and_only_upgrades_once(context, monkeypatch):
    import src.reporting as reporting
    from src.storage import file_hash
    store, task, fid = context
    analysis = DataService(store).analyze(task, fid, 'rows', chart='line')
    service = ReportService(store)
    monkeypatch.setattr(reporting, 'CHART_RENDERING', 'inline-svg-interactive-v1')
    original = service.generate(task, [analysis.analysis_id], formats=['html'])
    store.update_task(task.task_id, status='succeeded', result=original.model_dump())
    digest = file_hash(store.resolve_artifact(original.report_ids[0]))
    monkeypatch.setattr(reporting, 'CHART_RENDERING', 'inline-svg-interactive-v2')
    revision = TaskSpec(session_id=task.session_id, request='Improve chart interaction', analysis_ids=[analysis.analysis_id])
    store.create_task(revision)
    assert service.read_report(revision, original.report_ids[0])['needs_visual_upgrade']
    upgraded = service.generate(revision, [analysis.analysis_id], base_report_id=original.report_ids[0])
    assert upgraded.validation_summary['visual_upgrade']
    assert digest == file_hash(store.resolve_artifact(original.report_ids[0]))
    assert upgraded.analysis_ids == original.analysis_ids
    store.update_task(revision.task_id, status='succeeded', result=upgraded.model_dump())
    following = TaskSpec(session_id=task.session_id, request='Same settings', analysis_ids=[analysis.analysis_id])
    store.create_task(following)
    assert not service.read_report(following, upgraded.report_ids[0])['needs_visual_upgrade']
    with pytest.raises(StoreError) as error:
        service.generate(following, [analysis.analysis_id], base_report_id=upgraded.report_ids[0])
    assert error.value.code == 'report_unchanged'
