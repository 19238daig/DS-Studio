"""Trusted, source-bound edits of individual report charts.

Only application-owned Matplotlib operations run here. A model can select a
registered table/column, not supply Python, paths, SVG, or numerical values.
The original analysis, SQL snapshot, and other charts remain immutable.
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
import re
from collections import Counter
from decimal import Decimal, InvalidOperation
from pathlib import Path

from src.chart_svg import parse_svg, SVG_NS
from src.schemas import ReportChartEdit
from src.storage import StoreError

TOP = re.compile(r'(?<![A-Za-z])top\s*\d+(?!\d)', re.I)
NUMBERS = re.compile(r'[+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?')
MAX_CHART_PIXELS = 40_000_000
MAX_CHART_EDGE = 10_000
PREVIEW_DPI = 150


def validate_geometry(spec):
    """Bound host raster allocation before constructing any Matplotlib canvas.

    An SVG viewBox is independent of its PNG companion. Apply the report's
    existing 40 MP preview limit to both legacy and inherited edit specs.
    """
    size = spec.get('figsize')
    if (not isinstance(size, (list, tuple)) or len(size) != 2 or
            any(isinstance(value, bool) or not isinstance(value, (int, float)) or
                not math.isfinite(value) or value <= 0 for value in size)):
        raise StoreError('图表尺寸必须为有限的正数。', 422, 'invalid_chart_geometry')
    width, height = (value * PREVIEW_DPI for value in size)
    if (not math.isfinite(width) or not math.isfinite(height) or
            max(width, height) > MAX_CHART_EDGE or width * height > MAX_CHART_PIXELS):
        raise StoreError('图表局部重绘超过预览尺寸上限，请先提供合理尺寸的目标图。',
                         422, 'chart_edit_resource_limit')


def _decimal(value):
    try:
        number = Decimal(value)
        return number if number.is_finite() else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def table_rows(store, artifact_id):
    """Read the complete registered result, never a UI preview or model rows."""
    path = store.resolve_artifact(artifact_id)
    if path.suffix.lower() != '.csv' or path.stat().st_size > 25 * 1024 * 1024:
        raise StoreError('图表局部修改需要已登记的完整 CSV 结果表。', 422, 'chart_edit_source_required')
    with path.open(encoding='utf-8-sig', newline='') as handle:
        reader = csv.reader(handle)
        columns = next(reader, [])
        if not columns or len(set(columns)) != len(columns):
            raise StoreError('图表结果表列名无效。', 422, 'invalid_chart_edit')
        rows = []
        for row in reader:
            if len(row) != len(columns):
                raise StoreError('图表结果表形状无效。', 422, 'invalid_chart_edit')
            rows.append(dict(zip(columns, row)))
            if len(rows) > 200000:
                raise StoreError('图表结果表超过局部绘图行数上限。', 422, 'chart_edit_source_required')
    return columns, rows


def _bar_profile(raw):
    """Recognize exporter-tagged, one-axis single-series rectangular bars.

    Legacy export did not retain categorical tick text. Values must therefore
    match a unique complete result table and its sole categorical key before
    an old chart can be edited. Unsupported plots fail closed.
    """
    root = parse_svg(raw)
    marks = [node for node in root.iter() if node.get('id', '').startswith('dsmark_')]
    if not marks or len(marks) > 500 or len({n.get('data-series') for n in marks}) != 1:
        return None
    rectangles, colors, labels = [], [], []
    for mark in marks:
        paths = [n for n in mark if n.tag == f'{{{SVG_NS}}}path']
        if len(paths) != 1:
            return None
        path = paths[0]
        if not re.fullmatch(r'\s*M\s+[-+\d.eE\s]+L\s+[-+\d.eE\s]+L\s+[-+\d.eE\s]+L\s+[-+\d.eE\s]+z\s*', path.get('d', '')):
            return None
        coords = [float(x) for x in NUMBERS.findall(path.get('d', ''))]
        if len(coords) != 8 or not all(math.isfinite(x) for x in coords):
            return None
        rectangles.append(coords)
        match = re.search(r'(?:^|;)\s*fill:\s*(#[0-9A-Fa-f]{6})(?:;|$)', path.get('style', ''))
        if not match:
            return None
        colors.append(match[1].lower())
        labels.append(mark.get('data-label', ''))
    horizontal = len({round(c[0], 5) for c in rectangles}) == 1 and len({round(c[1], 5) for c in rectangles}) > 1
    vertical = len({round(c[1], 5) for c in rectangles}) == 1 and len({round(c[0], 5) for c in rectangles}) > 1
    if not (horizontal or vertical) or len(set(colors)) != 1:
        return None
    values, value_label, category_label = [], '', ''
    for label in labels:
        pairs = re.findall(r'([^;=]+)=([^;]+)', label.partition(': ')[2])
        if len(pairs) != 2:
            return None
        category, value = (pairs[1], pairs[0]) if horizontal else (pairs[0], pairs[1])
        if _decimal(value[1].strip()) is None:
            return None
        values.append(value[1].strip())
        value_label, category_label = value[0].strip(), category[0].strip()
    width, height = [float(x) for x in root.get('viewBox').split()[2:]]
    profile = dict(values=values, title=root.get('data-chart-title', ''), color=colors[0],
                chart_type='barh' if horizontal else 'bar', value_label=value_label,
                category_label=category_label, figsize=[width / 72, height / 72])
    validate_geometry(profile)
    return profile


def source_tables(analyses):
    return list(dict.fromkeys(tid for a in analyses for tid in a.table_ids))


def selected_rows(store, spec):
    columns, rows = table_rows(store, spec['table_id'])
    category, value = spec['category_column'], spec['value_column']
    if category not in columns or value not in columns or category == value:
        raise StoreError('图表列不属于原完整结果表。', 422, 'invalid_chart_edit')
    if any(not row[category].strip() or _decimal(row[value]) is None for row in rows):
        raise StoreError('类别或数值存在缺失，不能在局部修改时静默丢弃。', 422, 'chart_edit_source_required')
    if len({row[category] for row in rows}) != len(rows):
        raise StoreError('结果表类别不唯一，局部绘图不能擅自聚合。', 422, 'chart_edit_source_required')
    count = spec['top_n']
    if count > len(rows):
        raise StoreError(f'原完整结果只有{len(rows)}个类别，无法展示Top{count}；需要补充所需范围的数据。', 422, 'chart_edit_source_required')
    # Stable tie order uses original CSV order; no new business aggregation.
    ranked = sorted(rows, key=lambda row: _decimal(row[value]), reverse=True)
    return [[row[category], row[value]] for row in ranked[:count]]


def rows_hash(rows):
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def infer_spec(store, original_chart, analyses, edit):
    path = store.resolve_artifact(original_chart)
    if path.suffix.lower() == '.png':
        original = store.get_artifact(original_chart)
        pairs = [a for a in store.list_artifacts(original.task_id) if a.kind == 'chart_svg'
                 and a.source_ids == [original_chart] and Path(a.relative_path) == Path(original.relative_path).with_suffix('.svg')]
        if len(pairs) != 1:
            raise StoreError('历史PNG缺少同次矢量图，需只补目标图的SVG。', 422, 'chart_edit_source_required')
        path = store.resolve_artifact(pairs[0].artifact_id)
    profile = _bar_profile(path.read_bytes())
    if profile is None:
        raise StoreError('该历史图尚无可可靠恢复的单系列条形图结构。请只补充此图的结构化数据或重新绘制此图；其他报告内容应保留。', 422, 'chart_edit_source_required')
    candidates = []
    tables = source_tables(analyses)
    if edit.table_id is not None and edit.table_id not in tables:
        raise StoreError('局部绘图只能使用原报告分析中的结果表。', 422, 'artifact_not_authorized')
    for tid in tables:
        if edit.table_id is not None and tid != edit.table_id:
            continue
        columns, rows = table_rows(store, tid)
        if len(rows) < len(profile['values']):
            continue
        categorical = [c for c in columns if rows and all(row[c].strip() and _decimal(row[c]) is None for row in rows)
                       and len({row[c] for row in rows}) == len(rows)]
        # With no categorical metadata in a legacy SVG, only an unambiguous
        # key is accepted. A model cannot choose another identifier silently.
        if len(categorical) != 1 or (edit.category_column is not None and edit.category_column != categorical[0]):
            continue
        for value in columns:
            if edit.value_column is not None and value != edit.value_column:
                continue
            if not rows or any(_decimal(row[value]) is None for row in rows):
                continue
            top = sorted(rows, key=lambda row: _decimal(row[value]), reverse=True)[:len(profile['values'])]
            expected = Counter(format(float(row[value]), '.12g') for row in top)
            actual = Counter(format(float(v), '.12g') for v in profile['values'])
            if actual == expected:
                candidates.append(dict(table_id=tid, category_column=categorical[0], value_column=value,
                    top_n=len(profile['values']), title=profile['title'], color=profile['color'],
                    chart_type=profile['chart_type'], figsize=profile['figsize'],
                    value_label=profile['value_label'], category_label=profile['category_label']))
    # Identical copies of the same table are safe but use a stable original id.
    unique = {}
    for spec in candidates:
        fingerprint = (spec['category_column'], spec['value_column'], rows_hash(selected_rows(store, spec)))
        unique.setdefault(fingerprint, spec)
    if len(unique) != 1:
        raise StoreError('无法唯一匹配原图与完整结果表，请指定原图对应的table_id/category_column/value_column；不能猜测数据或只改标题。', 422, 'chart_edit_source_required')
    return next(iter(unique.values()))


def plan_edit(store, original_chart, analyses, edit, inherited=None):
    if original_chart not in {cid for a in analyses for cid in a.chart_ids}:
        raise StoreError('目标图表不属于原报告。', 422, 'artifact_not_authorized')
    current = dict(inherited['spec']) if inherited else infer_spec(store, original_chart, analyses, edit)
    validate_geometry(current)
    spec = dict(current)
    for key in ('table_id', 'category_column', 'value_column'):
        value = getattr(edit, key)
        if value is not None and value != current[key]:
            raise StoreError('局部图表修改不能更换原数据来源或指标；需要分析时只补目标部分。', 422, 'report_source_mismatch')
    if edit.top_n is not None:
        spec['top_n'] = edit.top_n
        if edit.title is None:
            if TOP.search(spec['title']):
                spec['title'] = TOP.sub(f'Top {edit.top_n}', spec['title'])
            else:
                spec['title'] += f'（Top {edit.top_n}）'
    for key in ('title', 'color', 'chart_type'):
        value = getattr(edit, key)
        if value is not None:
            spec[key] = value
    if edit.title is not None:
        # TopN is a display selection, not a free pass for numerical claims.
        from src.reporting import NUMBER
        old_title, new_title = (TOP.sub('', value) for value in (current['title'], spec['title']))
        # NUMBER is a presence guard (it matches individual Arabic digits),
        # so compare complete numeric lexemes before applying its CN guard.
        if (Counter(NUMBERS.findall(old_title)) != Counter(NUMBERS.findall(new_title)) or
                Counter(NUMBER.findall(NUMBERS.sub('', old_title))) !=
                Counter(NUMBER.findall(NUMBERS.sub('', new_title)))):
            raise StoreError('图表标题不能新增或改变未登记的统计数值；仅Top数量由top_n控制。', 422, 'unbound_chart_title')
        declared = re.findall(r'(?<![A-Za-z])top\s*(\d+)(?!\d)', spec['title'], re.I)
        if declared and any(int(n) != spec['top_n'] for n in declared):
            raise StoreError('图表标题中的Top数量必须与top_n一致。', 422, 'invalid_chart_edit')
    if spec == current:
        return None
    selected_rows(store, spec)
    return spec


def render_chart(store, task, original_chart, aid, spec, directory):
    """Use only fixed Matplotlib APIs on verified CSV values, never model code."""
    validate_geometry(spec)
    import matplotlib
    from matplotlib.figure import Figure
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.ticker import StrMethodFormatter
    from src.chart_export import apply_report_theme, configure_chart_fonts, export_figure
    rows = selected_rows(store, spec)
    directory.mkdir(parents=True, exist_ok=False)
    svg, png = directory / 'chart.svg', directory / 'chart.png'
    with matplotlib.rc_context():
        configure_chart_fonts()
        figure = Figure(figsize=spec['figsize'], dpi=120)
        FigureCanvasAgg(figure)
        axes = figure.subplots()
        plot_rows = list(reversed(rows)) if spec['chart_type'] == 'barh' else rows
        labels, values = [r[0] for r in plot_rows], [float(r[1]) for r in plot_rows]
        if spec['chart_type'] == 'barh':
            bars = axes.barh(labels, values, color=spec['color'])
            axes.set_xlabel(spec['value_label'])
            axes.set_ylabel(spec['category_label'])
            for bar, (label, lexical) in zip(bars, plot_rows):
                axes.annotate(format(float(lexical), ',.0f'), (bar.get_width(), bar.get_y() + bar.get_height() / 2),
                              xytext=(5, 0), textcoords='offset points', va='center', fontsize=9)
            axes.xaxis.set_major_formatter(StrMethodFormatter('{x:,.0f}'))
            axes.grid(axis='x', alpha=.3)
        else:
            bars = axes.bar(labels, values, color=spec['color'])
            axes.set_xlabel(spec['category_label'])
            axes.set_ylabel(spec['value_label'])
            axes.tick_params(axis='x', labelrotation=45)
            axes.yaxis.set_major_formatter(StrMethodFormatter('{x:,.0f}'))
            axes.grid(axis='y', alpha=.3)
        # Give every mark its exact category and source lexeme for inspection.
        axes.set_title(spec['title'])
        apply_report_theme(figure)
        figure.tight_layout()
        export_figure(figure, svg, Figure.savefig)
        figure.savefig(png, format='png', dpi=PREVIEW_DPI)
    root = parse_svg(svg.read_bytes())
    marks = [n for n in root.iter() if n.get('id', '').startswith('dsmark_')]
    if len(marks) != len(rows):
        raise StoreError('局部绘图标记数与目标Top数量不一致。', 422, 'report_validation_failed')
    from lxml import etree
    for mark, (category, lexical) in zip(marks, plot_rows):
        text = f'{category}: {spec["value_label"]}={lexical}'
        mark.set('data-label', text)
        title = mark.find(f'{{{SVG_NS}}}title')
        if title is not None:
            title.text = text
    svg.write_bytes(etree.tostring(root, encoding='utf-8', xml_declaration=True))
    source = store.get_artifact(spec['table_id'])
    chart = store.register_artifact(svg, task.task_id, task.session_id, 'report_chart', [original_chart, source.artifact_id])
    try:
        preview = store.register_artifact(png, task.task_id, task.session_id, 'chart_preview', [chart.artifact_id])
    except BaseException:
        discard_artifacts(store, [chart.artifact_id])
        raise
    return dict(chart_id=original_chart, artifact_id=chart.artifact_id, artifact_hash=chart.hash,
        preview_artifact_id=preview.artifact_id, preview_hash=preview.hash,
        analysis_id=aid, source_table_id=source.artifact_id, source_table_hash=source.hash,
        original_chart_hash=store.get_artifact(original_chart).hash,
        spec=spec, selected_rows_hash=rows_hash(rows), derivation_version=1)


def validate_override(store, task, analyses, override):
    """Reauthorize both original evidence and every inherited derived artifact."""
    required = {'chart_id', 'artifact_id', 'artifact_hash', 'preview_artifact_id', 'preview_hash',
                'analysis_id', 'source_table_id', 'source_table_hash', 'original_chart_hash', 'spec',
                'selected_rows_hash', 'derivation_version'}
    if not isinstance(override, dict) or set(override) != required or override['derivation_version'] != 1:
        raise StoreError('局部图表来源记录无效。', 422, 'invalid_report_base')
    owner = next((a for a in analyses if a.analysis_id == override['analysis_id'] and override['chart_id'] in a.chart_ids), None)
    if owner is None or override['source_table_id'] not in owner.table_ids:
        raise StoreError('局部图表超出原报告分析来源。', 422, 'invalid_report_base')
    if override['spec'].get('table_id') != override['source_table_id']:
        raise StoreError('局部图表结果表来源不匹配。', 422, 'invalid_report_base')
    validate_geometry(override['spec'])
    for key, hash_key in [('chart_id', 'original_chart_hash'), ('source_table_id', 'source_table_hash'),
                          ('artifact_id', 'artifact_hash'), ('preview_artifact_id', 'preview_hash')]:
        artifact = store.get_artifact(override[key])
        if artifact.session_id != task.session_id or artifact.hash != override[hash_key]:
            raise StoreError('局部图表来源指纹不匹配。', 409, 'stale_report_analysis')
        store.resolve_artifact(artifact.artifact_id)
    chart = store.get_artifact(override['artifact_id'])
    preview = store.get_artifact(override['preview_artifact_id'])
    origin = store.get_task(chart.task_id)
    if (chart.kind != 'report_chart' or chart.source_ids != [override['chart_id'], override['source_table_id']]
            or preview.kind != 'chart_preview' or preview.task_id != chart.task_id
            or preview.source_ids != [chart.artifact_id] or origin['session_id'] != task.session_id
            or (chart.task_id != task.task_id and origin['status'] != 'succeeded')):
        raise StoreError('局部图表产物来源链不匹配。', 422, 'invalid_report_base')
    rows = selected_rows(store, override['spec'])
    root = parse_svg(store.resolve_artifact(chart.artifact_id).read_bytes())
    marks = [n for n in root.iter() if n.get('id', '').startswith('dsmark_')]
    expected_rows = list(reversed(rows)) if override['spec']['chart_type'] == 'barh' else rows
    expected = [f'{label}: {override["spec"]["value_label"]}={value}' for label, value in expected_rows]
    if (rows_hash(rows) != override['selected_rows_hash'] or [n.get('data-label') for n in marks] != expected
            or root.get('data-chart-title') != override['spec']['title']):
        raise StoreError('局部图表与原完整结果数据不一致。', 422, 'report_validation_failed')
    return override


def discard_artifacts(store, ids):
    with store.lock:
        index = store._index('artifacts')
        for aid in ids:
            index.pop(aid, None)
        store._save_index('artifacts', index)
