"""Strict passive SVG validation and scoped embedding for generated reports."""
from __future__ import annotations

import base64
import hashlib
import re
from pathlib import Path

from lxml import etree
from markupsafe import Markup

SVG_NS = 'http://www.w3.org/2000/svg'
XLINK_NS = 'http://www.w3.org/1999/xlink'
CHART_RENDERING = 'inline-svg-interactive-v2'
TAGS = {'svg', 'g', 'path', 'defs', 'style', 'title', 'desc', 'use', 'clipPath',
        'rect', 'circle', 'ellipse', 'line', 'polyline', 'polygon', 'text', 'tspan',
        'linearGradient', 'radialGradient', 'stop', 'pattern'}
ATTRS = {'id', 'version', 'viewBox', 'width', 'height', 'x', 'y', 'x1', 'x2', 'y1', 'y2',
         'cx', 'cy', 'r', 'rx', 'ry', 'd', 'points', 'transform', 'style', 'class',
         'fill', 'fill-opacity', 'fill-rule', 'stroke', 'stroke-width', 'stroke-opacity',
         'stroke-linecap', 'stroke-linejoin', 'stroke-dasharray', 'stroke-dashoffset',
         'opacity', 'clip-path', 'clip-rule', 'clipPathUnits', 'preserveAspectRatio',
         'font-family', 'font-size', 'font-weight', 'font-style', 'text-anchor',
         'offset', 'stop-color', 'stop-opacity', 'gradientUnits', 'gradientTransform',
         'patternUnits', 'patternContentUnits', 'patternTransform', 'href', 'type',
         'data-series', 'data-label', 'data-points', 'data-ds-chart', 'data-chart-title', 'role', 'aria-label'}


def _style(value):
    if re.search(r'@|expression|javascript|[<>\\]|/\*', value, re.I):
        raise ValueError('Active SVG CSS is forbidden')
    # Only local paint/clipping references are allowed, never data or URLs.
    for target in re.findall(r'url\s*\((.*?)\)', value, re.I):
        if not re.fullmatch(r'#[A-Za-z_][\w.:-]*', target.strip()):
            raise ValueError('External SVG reference is forbidden')
    properties = {'fill', 'fill-opacity', 'fill-rule', 'stroke', 'stroke-width', 'stroke-opacity',
                  'stroke-linecap', 'stroke-linejoin', 'stroke-dasharray', 'stroke-dashoffset',
                  'opacity', 'clip-path', 'clip-rule', 'font', 'font-family', 'font-size',
                  'font-weight', 'font-style', 'text-anchor', 'stop-color', 'stop-opacity',
                  'paint-order', 'stroke-miterlimit', 'vector-effect'}
    if any(prop.lower() not in properties for prop in re.findall(r'([a-zA-Z-]+)\s*:', value)):
        raise ValueError('Unsupported SVG style property')
    return value


def parse_svg(raw: bytes):
    if len(raw) > 20 * 1024 * 1024 or b'<!ENTITY' in raw.upper():
        raise ValueError('SVG is too large or contains entities')
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False)
    try:
        root = etree.fromstring(raw, parser)
    except etree.XMLSyntaxError as exc:
        raise ValueError('Malformed SVG XML') from exc
    if root.tag != f'{{{SVG_NS}}}svg' or not root.get('viewBox'):
        raise ValueError('Expected an SVG root with viewBox')
    dimensions = root.get('viewBox').split()
    import math
    if len(dimensions) != 4 or not all(math.isfinite(float(n)) for n in dimensions) or any(float(n) <= 0 for n in dimensions[2:]):
        raise ValueError('Invalid SVG viewBox')
    # Matplotlib emits a metadata/RDF block. Discard metadata, never render it.
    for child in list(root):
        if isinstance(child.tag, str) and etree.QName(child).localname == 'metadata':
            root.remove(child)
    ids = set()
    for node in root.iter():
        if not isinstance(node.tag, str):
            raise ValueError('SVG comments and processing instructions are forbidden')
        qname = etree.QName(node)
        if qname.namespace != SVG_NS or qname.localname not in TAGS:
            raise ValueError('Unsupported or active SVG element: ' + qname.localname)
        if node.get('id'):
            value = node.get('id')
            if not re.fullmatch(r'[A-Za-z_][\w.:-]*', value) or value in ids:
                raise ValueError('Invalid or duplicate SVG ID')
            ids.add(value)
        for key, value in node.attrib.items():
            attr = etree.QName(key)
            if attr.localname not in ATTRS or attr.namespace not in (None, XLINK_NS):
                raise ValueError('Unsupported SVG attribute: ' + key)
            if attr.localname == 'href' and not re.fullmatch(r'#[A-Za-z_][\w.:-]*', value):
                raise ValueError('Only local SVG fragment links are permitted')
            if attr.localname in {'style', 'fill', 'stroke', 'clip-path'}:
                _style(value)
        if qname.localname == 'style':
            _style(node.text or '')
    # Reject unresolved references, including references to stripped metadata.
    for node in root.iter():
        for key, value in node.attrib.items():
            refs = [value[1:]] if etree.QName(key).localname == 'href' else re.findall(r'url\(\s*#([^\s)]+)\s*\)', value)
            if any(ref not in ids for ref in refs):
                raise ValueError('Unresolved SVG reference')
    return root


def inline_svg(raw: bytes, artifact_id: str):
    root = parse_svg(raw)
    prefix = 'chart_' + artifact_id + '_'
    ids = {node.get('id'): prefix + node.get('id') for node in root.iter() if node.get('id')}
    # Move stylesheet declarations onto their matching elements. This prevents
    # a chart's CSS from changing other charts or the surrounding report.
    for style in list(root.iter(f'{{{SVG_NS}}}style')):
        css = style.text or ''
        for selectors, declarations in re.findall(r'([^{}]+)\{([^{}]*)\}', css):
            for selector in selectors.split(','):
                selector = selector.strip()
                if selector == '*':
                    matches = list(root.iter())
                elif re.fullmatch(r'[a-zA-Z]+', selector):
                    matches = list(root.iter(f'{{{SVG_NS}}}{selector}'))
                else:
                    raise ValueError('Only simple SVG CSS selectors are supported')
                for target in matches:
                    target.set('style', declarations.strip() + ';' + target.get('style', ''))
        style.getparent().remove(style)
    for node in root.iter():
        for key, value in list(node.attrib.items()):
            if key == 'id':
                node.set(key, ids[value])
            elif etree.QName(key).localname == 'href':
                node.set(key, '#' + ids[value[1:]])
            elif re.search(r'url\s*\(', value, re.I):
                node.set(key, re.sub(r'url\s*\(\s*#([^\s)]+)\s*\)', lambda m: 'url(#' + ids[m[1]] + ')', value, flags=re.I))
    root.set('data-ds-chart', artifact_id)
    root.set('role', 'img')
    root.set('aria-label', '交互式 SVG 图表')
    # Only this validated application-owned boundary produces trusted markup.
    return Markup(etree.tostring(root, encoding='unicode'))


def interaction_script():
    return (Path(__file__).resolve().parents[1] / 'templates' / 'report_interactions_v2.js').read_text(encoding='utf-8')


def chart_csp():
    # Keep the fixed v1 hash so already delivered reports remain interactive
    # when served under the updated API response policy.
    legacy = (Path(__file__).resolve().parents[1] / 'templates' / 'report_interactions.js').read_text(encoding='utf-8')
    hashes = ["'sha256-" + base64.b64encode(hashlib.sha256(code.encode('utf-8')).digest()).decode('ascii') + "'"
              for code in (interaction_script(), legacy)]
    return ("default-src 'none'; script-src " + ' '.join(hashes) + "; style-src 'unsafe-inline'; "
            "img-src data:; font-src 'none'; connect-src 'none'; base-uri 'none'; form-action 'none'")
