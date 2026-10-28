"""Matplotlib vector export; also copied into the network-isolated runner.

No application imports: the exact same exporter runs on the host and in Docker.
PNG is retained solely as an Excel thumbnail. SVG never embeds raster images.
"""
from __future__ import annotations

import json
import math
import re
from functools import lru_cache
from pathlib import Path
import xml.etree.ElementTree as ET

SVG = 'http://www.w3.org/2000/svg'

CJK_FAMILIES = ('Noto Sans CJK SC', 'Noto Sans SC', 'Microsoft YaHei',
                'Noto Sans CJK JP', 'SimHei', 'Microsoft JhengHei', 'Arial Unicode MS')


def configure_chart_fonts():
    """Use an installed CJK face before layout, on both Windows and Linux."""
    import matplotlib
    from matplotlib import font_manager
    available = {face.name for face in font_manager.fontManager.ttflist}
    families = [name for name in CJK_FAMILIES if name in available]
    if families:
        matplotlib.rcParams['font.family'] = [families[0], 'DejaVu Sans']
    return families


@lru_cache(maxsize=128)
def _glyphs(path):
    from matplotlib.ft2font import FT2Font
    # A Last Resort font intentionally claims every codepoint but paints the
    # same boxed script symbol. It is not evidence of real glyph coverage.
    face = FT2Font(path)
    if 'lastresort' in (face.family_name + Path(path).stem).lower().replace(' ', '').replace('-', ''):
        return frozenset()
    return frozenset(face.get_charmap())


def ensure_figure_fonts(figure):
    """Repair unsupported Text faces and fail before publishing tofu glyphs.

    Public FontProperties/findfont APIs preserve size, rotation and emphasis:
    https://matplotlib.org/stable/api/font_manager_api.html
    """
    from matplotlib import font_manager
    from matplotlib.text import Text
    available = {face.name for face in font_manager.fontManager.ttflist}
    candidates = [name for name in CJK_FAMILIES if name in available]
    for ax in figure.axes:
        # Materialize lazy tick labels before checking every text artist.
        ax.get_xticklabels(); ax.get_yticklabels()
        if hasattr(ax, 'get_zticklabels'):
            ax.get_zticklabels()
    for text in figure.findobj(match=Text):
        value = text.get_text()
        if not value or not text.get_visible():
            continue
        required = {ord(char) for char in value if not char.isspace() and ord(char) >= 32}
        properties = text.get_fontproperties().copy()
        path = font_manager.findfont(properties)
        if required.issubset(_glyphs(path)):
            continue
        # Respect explicitly chosen Latin fonts when they work. Replace only
        # a face lacking actual characters (including an explicit DejaVu face).
        for family in candidates:
            alternative = properties.copy()
            alternative.set_file(None)
            alternative.set_family([family])
            path = font_manager.findfont(alternative, fallback_to_default=False)
            if required.issubset(_glyphs(path)):
                text.set_fontproperties(alternative)
                break
        else:
            missing = sorted(required - _glyphs(font_manager.findfont(properties)))
            codes = ', '.join(f'U+{code:04X}' for code in missing[:8])
            raise ValueError('图表字体不包含实际字形（' + codes + '）；请安装或重建包含 Noto CJK 的分析镜像，未导出占位方框。')


def apply_report_theme(figure):
    """A light, readable theme; never changes values, limits, or custom palettes."""
    from matplotlib.colors import to_rgba
    from matplotlib.collections import PathCollection
    ensure_figure_fonts(figure)
    defaults = ['#1f77b4', '#ff7f0e', '#2ca02c', '#d62728', '#9467bd',
                '#8c564b', '#e377c2', '#7f7f7f', '#bcbd22', '#17becf']
    palette = ['#178c83', '#df8769', '#659684', '#c9686a', '#9179ac',
               '#aa8b75', '#c789ae', '#8795a1', '#b1a365', '#62a5b7']

    def color(value):
        rgba = to_rgba(value)
        for original, replacement in zip(defaults, palette):
            if all(abs(a - b) < .003 for a, b in zip(rgba[:3], to_rgba(original)[:3])):
                return (*to_rgba(replacement)[:3], rgba[3])
        return value

    figure.set_facecolor('white')
    for ax in figure.axes:
        ax.set_facecolor('#fbfdfe')
        if ax.name == 'rectilinear' and ax.axison:
            ax.set_axisbelow(True)
            ax.yaxis.grid(True, color='#e5edf2', linewidth=.7)
            for side in ('top', 'right'):
                ax.spines[side].set_visible(False)
            for side in ('left', 'bottom'):
                ax.spines[side].set_color('#d5e1e8'); ax.spines[side].set_linewidth(.7)
            ax.tick_params(colors='#718899', labelsize=9, length=0, pad=7)
        for label in (ax.xaxis.label, ax.yaxis.label):
            label.set_color('#587286'); label.set_fontsize(10)
        ax.title.set_color('#294457'); ax.title.set_fontsize(12); ax.title.set_fontweight('semibold')
        for patch in ax.patches:
            patch.set_facecolor(color(patch.get_facecolor()))
        for line in ax.lines:
            line.set_color(color(line.get_color()))
            if line.get_linestyle() not in ('None', 'none', ''):
                line.set_linewidth(max(line.get_linewidth(), 1.8))
                if not line.get_label().startswith('_') and len(line.get_xdata()) <= 24 and line.get_marker() in ('None', 'none', ''):
                    line.set_marker('o'); line.set_markersize(4)
        for collection in ax.collections:
            if isinstance(collection, PathCollection) and collection.get_array() is None:
                faces = collection.get_facecolors()
                if len(faces):
                    collection.set_facecolor([color(value) for value in faces])
        legend = ax.get_legend()
        if legend:
            legend.get_frame().set_edgecolor('#e1e9ee')
            legend.get_frame().set_facecolor('white')
            for label in legend.get_texts():
                label.set_color('#587286'); label.set_fontsize(9)
            for handle in legend.legend_handles:
                if hasattr(handle, 'get_facecolor') and hasattr(handle, 'set_facecolor'):
                    face = handle.get_facecolor()
                    if hasattr(face, 'ndim') and face.ndim == 2:
                        handle.set_facecolor([color(c) for c in face])
                    else:
                        handle.set_facecolor(color(face))
                if hasattr(handle, 'get_color') and hasattr(handle, 'set_color'):
                    handle.set_color(color(handle.get_color()))


def export_figure(figure, svg_path, savefig, **kwargs):
    """Export with public artist IDs and data labels, without changing data."""
    import matplotlib
    from matplotlib.collections import PathCollection
    ensure_figure_fonts(figure)
    records = {}
    saved = []

    def tag(artist, series, label, points=None):
        key = f'dsmark_{len(records)}'
        saved.append((artist, artist.get_gid()))
        artist.set_gid(key)
        records[key] = (str(series), str(label), points, ai)

    try:
        bounds = {}
        for ai, ax in enumerate(figure.axes):
            bounds[ai] = ax.bbox.bounds
            xlabel, ylabel = ax.get_xlabel() or 'x', ax.get_ylabel() or 'y'
            # Tick text preserves categorical labels rather than internal codes.
            ticks = dict(zip(ax.get_xticks(), [t.get_text() for t in ax.get_xticklabels()]))
            for ci, container in enumerate(ax.containers):
                patches = getattr(container, 'patches', [])
                name = container.get_label()
                if not name or name.startswith('_'):
                    name = next((p.get_label() for p in patches if p.get_label() and not p.get_label().startswith('_')), None)
                name = name or (f'{ylabel} / series {ci + 1}' if len(ax.containers) > 1 else ylabel)
                for patch in patches:
                    x = patch.get_x() + patch.get_width() / 2
                    if getattr(container, 'orientation', None) == 'horizontal':
                        value = f'{xlabel}={patch.get_width():.12g}; {ylabel}={patch.get_y() + patch.get_height() / 2:.8g}'
                    else:
                        value = f'{xlabel}={ticks.get(x) or format(x, ".8g")}; {ylabel}={patch.get_height():.12g}'
                    tag(patch, f'{ai}:{name}', f'{name}: {value}')
            for li, line in enumerate(ax.lines):
                name = line.get_label()
                name = name if name and not name.startswith('_') else f'{ylabel} / line {li + 1}'
                xs, ys = line.get_data(orig=False)
                values = [f'{xlabel}={ticks.get(float(x)) or format(float(x), ".8g")}, {ylabel}={float(y):.12g}'
                          for x, y in zip(xs, ys)]
                # Vertices stay in display coordinates for nearest-point clicks.
                points = [[float(x), float(y), text] for (x, y), text in
                          zip(ax.transData.transform(list(zip(xs, ys))), values)
                          if math.isfinite(float(x)) and math.isfinite(float(y))] if len(xs) else []
                tag(line, f'{ai}:{name}', name, points)
            for ci, collection in enumerate(ax.collections):
                if not isinstance(collection, PathCollection):
                    tag(collection, f'{ai}:{ylabel} / layer {ci + 1}', f'{xlabel} / {ylabel}')
                    continue
                name = collection.get_label()
                name = name if name and not name.startswith('_') else ylabel
                labels = [f'{name}: {xlabel}={float(x):.12g}; {ylabel}={float(y):.12g}'
                          for x, y in collection.get_offsets()]
                tag(collection, f'{ai}:{name}', name, labels)
            # Pie wedges and other shapes that are not bar-container members.
            already = {id(a) for a, _ in saved}
            for patch in ax.patches:
                if id(patch) not in already:
                    label = patch.get_label() or ylabel
                    if hasattr(patch, 'theta1') and hasattr(patch, 'theta2'):
                        label += f'; sector={(patch.theta2 - patch.theta1) / 360 * 100:.8g}%'
                    tag(patch, f'{ai}:{patch.get_label() or ylabel}', label)
            for artist in ax.get_children():
                if hasattr(artist, 'set_rasterized'):
                    artist.set_rasterized(False)
        options = {k: v for k, v in kwargs.items() if k not in ('format', 'backend', 'pil_kwargs', 'metadata')}
        with matplotlib.rc_context({'svg.fonttype': 'path'}):
            savefig(figure, svg_path, format='svg', **options)
        tree = ET.parse(svg_path)
        root = tree.getroot()
        headings = [ax.get_title() for ax in figure.axes if ax.get_title()]
        fallback = next((f'{ax.get_ylabel()} / {ax.get_xlabel()}'.strip(' /') for ax in figure.axes
                         if ax.get_xlabel() or ax.get_ylabel()), '数据图表')
        root.set('data-chart-title', ' · '.join(headings)[:200] or fallback[:200])
        if root.findall(f'.//{{{SVG}}}image'):
            Path(svg_path).unlink(missing_ok=True)
            raise ValueError('SVG charts must be vector graphics; use pcolormesh/contour instead of imshow or raster images.')
        scale = 72 / figure.dpi
        height = figure.get_figheight() * 72
        for node in root.iter():
            record = records.get(node.get('id'))
            if record is None:
                continue
            series, label, points, axis = record
            node.set('data-series', series)
            node.set('data-label', label)
            title = ET.SubElement(node, f'{{{SVG}}}title')
            title.text = label
            if points and isinstance(points[0], list):
                # The actual SVG clipping rectangle accounts for tight bbox
                # shifts, so nearest-point inspection also works after cropping.
                reference = next((n.get('clip-path') for n in node.iter() if n.get('clip-path')), None)
                clip_id = re.fullmatch(r'url\(#([^)]*)\)', reference or '')
                clip = next((n for n in root.iter(f'{{{SVG}}}clipPath') if clip_id and n.get('id') == clip_id[1]), None)
                rect = clip.find(f'{{{SVG}}}rect') if clip is not None else None
                if rect is not None and bounds[axis][2] and bounds[axis][3]:
                    bx, by, bw, bh = bounds[axis]
                    rx, ry, rw, rh = [float(rect.get(k)) for k in ('x', 'y', 'width', 'height')]
                    mapped = [[rx + (x-bx)/bw*rw, ry + (1-(y-by)/bh)*rh, t] for x, y, t in points]
                    node.set('data-points', json.dumps(mapped, allow_nan=False))
                elif kwargs.get('bbox_inches') != 'tight':
                    node.set('data-points', json.dumps([[x * scale, height - y * scale, t] for x, y, t in points], allow_nan=False))
                else:
                    node.set('data-label', label + ': ' + '; '.join(p[2] for p in points[:8]))
            elif points:
                uses = [u for u in node.iter(f'{{{SVG}}}use') if u.get('x') is not None and u.get('y') is not None]
                for use, text in zip(uses, points):
                    use.set('data-label', text)
                    ET.SubElement(use, f'{{{SVG}}}title').text = text
        ET.register_namespace('', SVG)
        ET.register_namespace('xlink', 'http://www.w3.org/1999/xlink')
        tree.write(svg_path, encoding='utf-8', xml_declaration=True)
    finally:
        for artist, gid in saved:
            artist.set_gid(gid)
