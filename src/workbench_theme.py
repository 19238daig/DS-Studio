"""Presentation only: application-owned styles and escaped HTML fragments."""
from html import escape

# The local stylesheet is also used in offline browser verification.
from pathlib import Path

def stylesheet() -> str:
    return '<style>' + Path(__file__).with_name('ui_theme.css').read_text(encoding='utf-8') + '</style>'


STYLES = stylesheet()


def icon(kind: str) -> str:
    """Decorative, fixed local vector marks beside visible labels."""
    paths = {
        'chart': '<path d="M4 18V6m0 12h16M8 14l4-5 4 2 4-6"/>',
        'table': '<rect x="4" y="4" width="16" height="16" rx="2"/><path d="M4 9h16M9 9v11"/>',
        'report': '<path d="M7 3h7l4 4v14H7zM14 3v5h4M10 12h5m-5 4h5"/>',
    }
    return ('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" '
            'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">'
            + paths.get(kind, paths['chart']) + '</svg>')


def brand() -> str:
    return ('<div class="ds-brand"><div class="ds-brand-mark">' + icon('chart') + '</div><div>'
            '<div class="ds-brand-name">DS Studio</div>'
            '<div class="ds-brand-sub">数据分析工作台</div></div></div>')


def header(title: str, ready: bool) -> str:
    return ('<div class="ds-topbar"><div class="ds-breadcrumb">工作空间<span>/</span>'
            f'<strong>{escape(title)}</strong></div><div class="ds-connection">'
            f'<span class="ds-live-dot{"" if ready else " offline"}"></span>'
            f'{"分析服务已就绪" if ready else "分析服务等待连接"}</div></div>'
            '<div class="ds-page-head"><h1>数据对话</h1>'
            '<p>查询指标、分析数据，或继续修改已有报告。</p></div>')


def welcome() -> str:
    return ('<div class="ds-welcome">' + icon('chart') + '<h2>开始一次数据分析</h2>'
            '<p>在左侧选择文件或数据库，然后输入你要了解的问题。'
            '已有报告可以在同一会话中继续修改。</p></div>')


def source_card(name: str, suffix: str, detail: str) -> str:
    kind = escape(suffix.upper().lstrip('.') or 'FILE')
    return ('<div class="ds-source"><div class="ds-file-icon">'
            f'{kind}</div><div><div class="ds-source-name">{escape(name)}</div>'
            f'<div class="ds-source-meta">{escape(detail)}</div></div></div>')


def task_state(label: str, state: str, active_role: str | None, seen: set[str]) -> str:
    stages = [('main', '理解需求'), ('analyst', '分析数据'), ('report_designer', '生成报告')]
    pills = []
    for role, name in stages:
        if role not in seen and role != active_role:
            continue
        style = ('current' if state == 'active' and role == active_role else
                 'failed' if state == 'failed' and role == active_role else
                 'done' if role in seen and state == 'complete' else '')
        pills.append(f'<span class="{style}">{name}</span>')
    return (f'<div class="ds-task-state {escape(state)}"><span class="ds-live-dot"></span>'
            f'{escape(label)}</div><div class="ds-pipeline">{"".join(pills)}</div>')
