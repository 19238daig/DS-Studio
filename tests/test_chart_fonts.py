"""Real CJK glyphs in the same offline SVG/PNG export path used by reports."""
import subprocess
import warnings
import xml.etree.ElementTree as ET

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib import font_manager
from matplotlib.figure import Figure
import pytest

from src.chart_export import _glyphs, ensure_figure_fonts, export_figure
from src.chart_svg import inline_svg
from src.config import Settings
from src.data import DataService
from src.execution import DockerExecutor
from src.schemas import TaskSpec
from src.storage import Store


TITLE = '各 Loyalty Tier: GMV 与人均消费'


def test_explicit_latin_face_is_repaired_for_title_legend_ticks_and_annotation(tmp_path):
    with matplotlib.rc_context({'font.family': 'DejaVu Sans'}):
        fig, ax = plt.subplots()
        try:
            ax.bar(['金卡', '银卡'], [200, -80], label='人均消费')
            ax.set_title(TITLE, fontfamily='DejaVu Sans', fontweight='bold')
            ax.set_xlabel('会员等级'); ax.set_ylabel('销售额 − 变化')
            ax.annotate('较高', (0, 200)); ax.legend()
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter('always')
                ensure_figure_fonts(fig)
                fig.tight_layout()
                fig.savefig(tmp_path / 'host.png')
                export_figure(fig, tmp_path / 'host.svg', Figure.savefig)
            assert not any('Glyph' in str(item.message) and 'missing' in str(item.message) for item in caught)
            title_font = font_manager.findfont(ax.title.get_fontproperties())
            assert {ord(c) for c in '各与人均消费'}.issubset(_glyphs(title_font))
            assert ax.title.get_fontweight() == 'bold'
            raw = (tmp_path / 'host.svg').read_bytes()
            root = ET.fromstring(raw)
            assert root.get('data-chart-title') == TITLE
            assert not root.findall('.//{http://www.w3.org/2000/svg}image')
            assert b'LastResort' not in raw
            assert 'data-series' in str(inline_svg(raw, 'cjk_host'))
        finally:
            plt.close(fig)


def test_last_resort_is_not_real_character_coverage():
    face = next(face for face in font_manager.fontManager.ttflist if face.name == 'Last Resort High-Efficiency')
    assert not _glyphs(face.fname)


def test_unsupported_text_fails_instead_of_exporting_placeholder():
    fig, ax = plt.subplots()
    try:
        ax.set_title('unsupported ' + chr(0x10FFFF))
        with pytest.raises(ValueError, match='U\\+10FFFF'):
            ensure_figure_fonts(fig)
    finally:
        plt.close(fig)


def test_host_builtin_chinese_columns_export_actual_vector_glyphs(tmp_path):
    store = Store(Settings(workspace=tmp_path, _env_file=None))
    sid = store.new_session()['session_id']
    (tmp_path / 'KB' / 'sales.csv').write_text('地区,销售额\n华东,200\n华南,80\n', encoding='utf-8')
    fid = store.refresh_kb()[0]['file_id']
    task = TaskSpec(session_id=sid, request='各地区销售额', file_ids=[fid])
    store.create_task(task)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter('always')
        result = DataService(store).analyze(task, fid, 'aggregate', group_by=['地区'], aggregations={'销售额': 'sum'}, chart='bar')
    assert not any('Glyph' in str(item.message) and 'missing' in str(item.message) for item in caught)
    svg = next(a for a in store.list_artifacts(task.task_id) if a.kind == 'chart_svg')
    raw = store.resolve_artifact(svg.artifact_id).read_bytes()
    assert b'LastResort' not in raw and result.chart_ids
    assert '销售额' in ET.fromstring(raw).get('data-chart-title')


def test_real_docker_cjk_svg_png_and_no_missing_glyphs(tmp_path):
    if subprocess.run(['docker', 'image', 'inspect', 'ds-analysis:local'], capture_output=True).returncode:
        pytest.skip('Build ds-analysis:local to run the real font test')
    store = Store(Settings(workspace=tmp_path, _env_file=None))
    sid = store.new_session()['session_id']
    (tmp_path / 'KB' / 'sales.csv').write_text('tier,revenue\ngold,200\nsilver,80\n', encoding='utf-8')
    fid = store.refresh_kb()[0]['file_id']
    task = TaskSpec(session_id=sid, request='中文图表字形验收', file_ids=[fid])
    store.create_task(task)
    code = '''import json
import warnings
import matplotlib.pyplot as plt
from matplotlib import font_manager
warnings.filterwarnings('error', message=r'Glyph .* missing from font')
# Even an explicit Latin face in model-generated code must not create tofu.
plt.rcParams['font.family'] = 'DejaVu Sans'
fig, ax = plt.subplots(figsize=(10, 5))
ax.bar(['金卡', '银卡'], [200, -80], label='GMV')
ax.plot(['金卡', '银卡'], [120, 60], label='人均消费')
ax.set_title('各 Loyalty Tier: GMV 与人均消费', fontweight='bold')
ax.set_xlabel('会员等级'); ax.set_ylabel('销售额（含负值 −80）')
ax.annotate('消费较高', (0, 200)); ax.legend()
fig.savefig('/output/chinese.svg')
fig2, ax2 = plt.subplots(figsize=(11, 5))
ax2.bar(['华东', '华南'], [200, 80], label='商品金额')
ax2.set_title('各 Region GMV（基于 customer_info.region）', fontweight='bold')
ax2.set_xlabel('客户地区'); ax2.set_ylabel('销售金额'); ax2.legend()
fig2.savefig('/output/region.png')
json.dump({'metrics':{'gold_revenue':200,'silver_revenue':80},
'metric_definitions':{'gold_revenue':'complete fixture gold revenue','silver_revenue':'complete fixture silver revenue'},
'charts':['chinese.svg','region.png'],'tables':[], 'findings':[], 'limitations':[]},open('/output/result.json','w'))
print('CJK title, axis, ticks, legend, annotation and Unicode minus exported')
'''
    analysis = DockerExecutor(store).run_python(task, code)
    artifacts = store.list_artifacts(task.task_id)
    assert len(analysis.chart_ids) == 2
    for chart_id in analysis.chart_ids:
        chart = store.get_artifact(chart_id)
        svg = chart if chart.name.endswith('.svg') else next(a for a in artifacts if a.kind == 'chart_svg' and a.source_ids == [chart_id])
        raw = store.resolve_artifact(svg.artifact_id).read_bytes()
        assert b'LastResort' not in raw
        parsed = ET.fromstring(raw)
        assert not parsed.findall('.//{http://www.w3.org/2000/svg}image')
        assert 'data-series' in str(inline_svg(raw, chart_id))
        assert not parsed.findall('.//{http://www.w3.org/2000/svg}text')
    logs = '\n'.join(store.resolve_artifact(a.artifact_id).read_text(encoding='utf-8', errors='replace') for a in artifacts if a.kind == 'log')
    assert 'Glyph' not in logs and 'missing from font' not in logs
