"""Chinese Streamlit client. Business files and agents are owned by FastAPI."""
from __future__ import annotations

import os
from collections import Counter
from html import escape
from pathlib import PurePosixPath
from uuid import uuid4

import streamlit as st
import streamlit.components.v1 as components

from src.client import ApiClient, ApiError
from src.workbench_theme import brand, header, icon, source_card, stylesheet, task_state, welcome
from src.telemetry import ROLES

st.set_page_config(page_title='DS Studio · 数据工作台', page_icon=':material/analytics:', layout='wide')

API_URL = os.getenv('DS_API_URL', 'http://127.0.0.1:8000')
ACTIVE = {'queued', 'running', 'cancelling', 'cancel_requested', 'pending'}
LABELS = {'queued': '排队中', 'pending': '等待处理', 'running': '执行中', 'completed': '已完成', 'succeeded': '已完成', 'failed': '执行失败', 'cancelled': '已取消', 'cancelling': '正在取消', 'cancel_requested': '正在取消', 'interrupted': '执行中断', 'waiting_user': '等待补充信息'}
PROFILE_PENDING = {'pending', 'queued', 'running'}


@st.cache_resource
def api() -> ApiClient:
    return ApiClient(API_URL)


def show_error(exc: ApiError) -> None:
    st.error(str(exc))
    if exc.request_id:
        st.caption(f'请求编号：{exc.request_id}')


def name_of(item: dict) -> str:
    return item.get('original_name') or item.get('name') or item.get('filename') or item.get('file_id', '未命名文件')


def session_name(item: dict) -> str:
    title = item.get('title') or '新会话'
    if title in {'新会话', '新对话'}:
        first_question = next((message.get('content', '') for message in item.get('messages', [])
                               if message.get('role') == 'user'), '')
        title = first_question or title
    return ' '.join(title.split())


def session_labels(sessions: list[dict]) -> dict[str, str]:
    titles = [session_name(item)[:28] for item in sessions]
    counts = Counter(titles)
    labels, used = {}, set()
    for index, (item, title) in enumerate(zip(sessions, titles), 1):
        label = f'{index} · {title}' if counts[title] > 1 else title
        while label in used:
            label += f' ({index})'
        labels[item['session_id']] = label
        used.add(label)
    return labels


def history_labels(task_ids: list[str], titles: dict[str, str]) -> dict[str, str]:
    """Retries have identical questions; each option must serialize uniquely."""
    captions = [titles.get(tid, f'任务 {tid[:8]}')[:35] for tid in task_ids]
    counts = Counter(captions)
    labels, used = {}, set()
    for index, (tid, caption) in enumerate(zip(task_ids, captions), 1):
        label = f'{index} · {caption}' if counts[caption] > 1 else caption
        while label in used:
            label += f' ({index})'
        labels[tid] = label
        used.add(label)
    return labels


def select_session(session_id: str) -> None:
    stop_task_subscription()
    st.session_state.session_id = session_id
    st.session_state.active_task = None
    st.session_state.reply_to_task = None
    st.session_state.pop('selected_files', None)
    st.session_state.pop('submit_key', None)
    clear_database_selection()
    for key in ('report_outputs', 'reply_formats_for', 'pending_reply_formats', 'last_task', 'submit_payload',
                'prompt', 'clear_prompt_next', 'history_focus_task', 'report_preview_id', 'report_preview_error',
                'conversation_report_preview', 'conversation_report_error'):
        st.session_state.pop(key, None)
    st.session_state.workspace_tabs = '对话'


def clear_deleted_session_cache() -> None:
    """Discard task events, downloads and drafts after backend deletion succeeds."""
    stop_task_subscription()
    specific = {'session_id', 'active_task', 'reply_to_task', 'last_task', 'selected_files',
                'submit_key', 'submit_payload', 'prompt', 'report_outputs', 'reply_formats_for', 'pending_reply_formats',
        'clear_prompt_next', 'history_focus_task', 'report_preview_id', 'report_preview_error',
        'conversation_report_preview', 'conversation_report_error'}
    clear_database_selection()
    prefixes = ('events_', 'cursor_', 'download_', 'load_', 'save_', 'delete_confirm_', 'history_picker_', 'session_picker_')
    for key in list(st.session_state):
        if key in specific or key.startswith(prefixes):
            st.session_state.pop(key, None)


def clear_database_selection() -> None:
    """Database widgets and cached previews belong to one conversation only."""
    names = {'selected_database_sources', 'selected_database_tables', 'database_selection_touched',
             'reply_database_for', 'pending_reply_database', 'database_catalogs_current'}
    for key in list(st.session_state):
        if key in names or key.startswith(('database_table_picker_', 'database_preview_picker_', 'db_catalog_', 'db_preview_', 'db_scope_')):
            st.session_state.pop(key, None)


def database_selection_changed() -> None:
    st.session_state.database_selection_touched = True


def render_database_picker(session_id: str) -> None:
    """Only HTTP APIs are used here; no credentials or SQL reach Streamlit."""
    st.markdown('<div class="ds-side-label">PostgreSQL 数据库</div>', unsafe_allow_html=True)
    st.session_state.database_catalogs_current = {}
    try:
        sources = {item['source_id']: item for item in api().database_sources() if item.get('configured')}
    except ApiError as exc:
        show_error(exc)
        return
    current = st.session_state.get('selected_database_sources', [])
    if any(source not in sources for source in current):
        st.session_state.selected_database_sources = [source for source in current if source in sources]
        st.session_state.database_selection_touched = True
    selected = st.multiselect('数据库来源', list(sources), key='selected_database_sources',
        format_func=lambda source: sources[source]['name'], placeholder='选择数据库来源', on_change=database_selection_changed)
    selections = {}
    for source in selected:
        try:
            description = api().database_catalog(source)
        except ApiError as exc:
            show_error(exc)
            # Keep the previous scope on a transient failure. Submission will
            # validate the actual role again; file-only users can clear source.
            previous = st.session_state.get('selected_database_tables', {})
            if source in previous:
                selections[source] = list(previous[source])
            continue
        scope = description['scope_fingerprint']
        st.session_state.database_catalogs_current[source] = description
        for key in list(st.session_state):
            if key.startswith((f'db_catalog_{source}_', f'db_preview_{source}_')) and not key.startswith((f'db_catalog_{source}_{scope}', f'db_preview_{source}_{scope}')):
                st.session_state.pop(key, None)
        st.session_state[f'db_catalog_{source}_{scope}'] = description
        table_map = {item['qualified_name']: item for item in description['tables']}
        picker = f'database_table_picker_{source}'
        if picker not in st.session_state:
            old = st.session_state.get('selected_database_tables', {})
            st.session_state[picker] = list(old.get(source, table_map))
        st.session_state[picker] = [name for name in st.session_state[picker] if name in table_map]
        chosen = st.multiselect('授权表 · ' + sources[source]['name'], list(table_map), key=picker,
                                on_change=database_selection_changed, placeholder='清空表示不读取任何表')
        selections[source] = list(chosen)
        st.caption(description.get('scope_description', '只读查询'))
        if not chosen:
            st.info('尚未授权业务表，Agent 不会查询此来源的任何表。')
        with st.expander('字段、关联与数据预览 · ' + sources[source]['name']):
            if not chosen:
                st.caption('先选择授权表。')
                continue
            preview_picker = f'database_preview_picker_{source}'
            if st.session_state.get(preview_picker) not in chosen:
                st.session_state.pop(preview_picker, None)
            table = st.selectbox('查看表', chosen, key=preview_picker)
            metadata = table_map[table]
            st.caption(metadata.get('description', ''))
            st.dataframe(metadata['columns'], hide_index=True, width='stretch')
            for key in ('primary_key', 'foreign_keys'):
                if metadata.get(key):
                    st.caption(('主键：' if key == 'primary_key' else '关联：') + '；'.join(metadata[key]))
            preview_key = f'db_preview_{source}_{scope}_{table}'
            if st.button('读取 20 行预览', key=f'db_preview_button_{source}'):
                try:
                    st.session_state[preview_key] = api().database_preview(session_id, source, table)
                except ApiError as exc:
                    show_error(exc)
            preview = st.session_state.get(preview_key)
            if preview:
                st.caption('仅为前 20 行样本，分析将在数据库中读取完整授权数据。')
                st.dataframe(preview.get('preview', []), hide_index=True, width='stretch')
    st.session_state.selected_database_tables = selections


def profile_list(item: dict) -> list[dict]:
    """Accept both the multi-table API contract and its single-profile fallback."""
    profiles = item.get('profiles')
    if isinstance(profiles, list) and profiles:
        return [p for p in profiles if isinstance(p, dict)]
    profile = item.get('profile') or item.get('dataset_profile')
    return [profile] if isinstance(profile, dict) else []


def render_file_profile(item: dict) -> None:
    with st.expander(name_of(item) + ' · 数据概览', expanded=True):
        status = item.get('profile_status', 'ready' if profile_list(item) else 'registered')
        if status in PROFILE_PENDING:
            st.info('数据概览正在' + ('解析中。' if status == 'running' else '等待解析。') + '结果就绪后自动更新。')
            if item.get('profile_task_id'):
                st.caption('解析任务：' + item['profile_task_id'])
            return
        if status in {'failed', 'cancelled', 'interrupted'}:
            message = {'failed': '数据概览解析失败', 'cancelled': '数据概览解析已取消', 'interrupted': '数据概览解析已中断'}[status]
            st.warning(message + '。此文件尚无可用的完整概览。')
            error = item.get('profile_error')
            if error:
                st.text(error.get('message', str(error)) if isinstance(error, dict) else str(error))
            if item.get('profile_task_id'):
                st.caption('可在历史任务中查看或重新运行解析任务：' + item['profile_task_id'])
            return
        profiles = profile_list(item)
        if not profiles:
            st.info('文件已登记，尚未提供数据概览。')
        for profile in profiles:
            columns = profile.get('columns', [])
            if not columns:
                st.info('文字材料：没有可计算表格，可用于有来源的材料整理与报告。')
                for warning in profile.get('quality_warnings', []):
                    st.warning(str(warning))
                text = profile.get('text') or profile.get('source_text')
                if text:
                    st.markdown('**正文摘录**')
                    st.text(str(text))
                continue
            location = profile.get('sheet_or_table') or '默认数据表'
            st.markdown('**工作表 / 表格**')
            st.text(str(location))
            left, right = st.columns(2)
            left.metric('完整数据行数', str(profile.get('row_count', '未提供')))
            right.metric('列数', str(len(columns)))
            for warning in profile.get('quality_warnings', []):
                st.warning(str(warning))
            if columns:
                st.dataframe([{'列名': column, '数据类型': profile.get('dtypes', {}).get(column, '未提供')} for column in columns], hide_index=True, width='stretch')
            preview = profile.get('preview') or []
            if preview:
                st.caption(f'以下仅展示 {len(preview)} 行预览样本；完整行数见上方，分析应读取全量已授权数据。')
                st.dataframe(preview, hide_index=True, width='stretch')
            else:
                st.caption('没有可展示的表格预览。')
            # Compatibility for source-grounded HTML text; never interpret it as markup.
            text = profile.get('text') or profile.get('source_text')
            if text:
                st.markdown('**正文摘录**')
                st.text(str(text))
        text = item.get('text') or item.get('source_text')
        if text:
            st.markdown('**正文摘录**')
            st.text(str(text))


@st.fragment(run_every='2s')
def poll_profiles(session_id: str, selected_ids: list[str], fallback_files: dict) -> None:
    try:
        current = {item['file_id']: item for item in api().files(session_id)}
        selected = [current.get(fid, fallback_files[fid]) for fid in selected_ids if fid in fallback_files]
        for item in selected:
            render_file_profile(item)
        if not any(item.get('profile_status') in PROFILE_PENDING for item in selected):
            st.rerun()
    except ApiError as exc:
        show_error(exc)


def human_size(size: int) -> str:
    if size < 1024:
        return f'{size} B'
    return f'{size / 1024 / 1024:.1f} MB' if size >= 1024 * 1024 else f'{max(1, round(size / 1024))} KB'


def render_artifact_card(item: dict) -> None:
    artifact_id = item['artifact_id']
    filename = item.get('name') or PurePosixPath(item.get('relative_path', artifact_id)).name
    mime = item.get('mime_type', 'application/octet-stream')
    suffix = PurePosixPath(filename).suffix.lower()
    is_report = item.get('kind') in {'report', 'report_html', 'html_report'} or item.get('relative_path', '').replace('\\', '/').startswith('report/')
    label = {'html': 'HTML 网页报告', 'xlsx': 'Excel 工作簿', 'png': '分析图表', 'svg': 'SVG 矢量图表', 'csv': '数据结果表',
             'py': '分析脚本', 'json': '来源与校验记录'}.get(suffix.lstrip('.'), filename)
    with st.container(key=f'artifact_{artifact_id}'):
        title_col, action_col = st.columns([3, 1], vertical_alignment='center')
        with title_col:
            color = ' blue' if suffix in {'.html', '.json', '.py'} else ''
            st.markdown(f'<span class="ds-pill{color}">{escape(suffix.lstrip(".").upper() or "FILE")}</span>'
                        f'<div class="ds-artifact-title" style="margin-top:10px">{escape(label)}</div>'
                        f'<div class="ds-artifact-meta">{escape(filename)} · {human_size(item.get("size", 0))}</div>',
                        unsafe_allow_html=True)
        with action_col:
            if st.button('预览 / 下载', key=f'load_{artifact_id}', width='stretch',
                         disabled=not item.get('downloadable', True)):
                try:
                    st.session_state[f'download_{artifact_id}'] = api().download(artifact_id)
                except ApiError as exc:
                    show_error(exc)
        data = st.session_state.get(f'download_{artifact_id}')
        if data is not None:
            if mime == 'image/svg+xml':
                # Streamlit accepts SVG as XML text; raw bytes go through PIL,
                # which only supports raster images.
                st.image(data.decode('utf-8'))
            elif mime.startswith('image/'):
                st.image(data)
            elif mime == 'text/html' and is_report and not (
                    st.session_state.get('workspace_tabs', '对话') == '对话' and
                    st.session_state.get('conversation_report_preview')):
                # Only registered generated reports, inside a sandboxed iframe.
                components.html(data.decode('utf-8', errors='replace'), height=620, scrolling=True)
            elif mime.startswith('text/') and mime != 'text/html' and len(data) <= 200_000:
                st.code(data.decode('utf-8', errors='replace'), language='python' if suffix == '.py' else None)
            st.download_button('下载文件', data=data, file_name=item.get('download_name') or filename, mime=mime, key=f'save_{artifact_id}',
                               icon=':material/download:')
        if not item.get('downloadable', True):
            st.caption('任务结束后即可预览与下载。')


def render_artifacts(task_id: str, *, include_reports: bool = True) -> None:
    try:
        items = api().artifacts(task_id)
    except ApiError as exc:
        show_error(exc)
        return
    if not items:
        st.info('还没有生成文件。分析完成后，图表、数据表和报告会显示在这里。')
        return
    reports = [item for item in items if is_report_artifact(item)]
    other = [item for item in items if item not in reports]
    if include_reports:
        for item in reports:
            render_artifact_card(item)
    if other:
        with st.expander(f'图表、数据与来源文件 · {len(other)} 项', expanded=False):
            for item in other:
                render_artifact_card(item)


def render_report_versions(versions: list[dict]) -> None:
    st.caption('在对话中说明要改哪一处，即可基于已有报告另存新版本。原报告和所有修改版均可分别预览和下载。')
    if not versions:
        st.info('当前会话还没有报告。生成后，原报告及后续修改版本会集中显示在这里。')
        return
    st.markdown(f'**报告版本 · {len(versions)} 个**')
    for index, version in enumerate(versions):
        kind = '修订版' if version.get('revision_of') else '原始报告'
        focused = any(item['artifact_id'] == st.session_state.get('report_preview_id') for item in version['artifacts'])
        with st.expander(f"版本 {version['number']} · {kind} · {version['title']}", expanded=focused or index < 2):
            st.caption(f"保存时间（UTC）：{version['created_at']} · 版本编号：{version['version']}")
            if version.get('base_number'):
                st.caption(f"基于版本 {version['base_number']} 修改，原版本完整保留。")
            elif version.get('revision_of'):
                st.caption('基于已有报告修改，原报告文件保留。')
            st.text('生成要求：' + version['request'])
            for item in version['artifacts']:
                suffix = PurePosixPath(item['name']).suffix
                version_kind = 'revised' if version.get('revision_of') else 'original'
                render_artifact_card({**item, 'download_name': f"report_v{version['number']}_{version_kind}{suffix}"})


def duration_text(seconds) -> str:
    return '未记录' if seconds is None else f'{seconds:,.2f} 秒'


def token_text(value) -> str:
    return '未获取' if value is None else f'{value:,}'


def agent_text(telemetry: dict) -> str:
    return '、'.join(ROLES[role] for role in telemetry.get('agents', []) if role in ROLES) or '等待主 Agent 接收'


def usage_note(telemetry: dict) -> str:
    return {'partial': '部分统计：存在未返回用量的调用', 'unavailable': '尚未获取模型用量',
            'recorded': '历史已记录用量', 'complete': '模型已返回用量'}.get(telemetry.get('usage_status'), '尚未获取模型用量')


def stop_task_subscription() -> None:
    subscription = st.session_state.pop('_task_subscription', None)
    if subscription is not None:
        subscription.close()


def task_subscription(task_id: str):
    """One UI-session subscription. Unsupported old APIs retain normal polling."""
    if not st.session_state.get('_streaming_enabled', False):
        return None
    current = st.session_state.get('_task_subscription')
    if current is not None and current.task_id != task_id:
        stop_task_subscription()
        current = None
    if current is None:
        current = api().subscribe_task(task_id)
        st.session_state._task_subscription = current
    return current


@st.fragment(run_every='250ms')
def stream_answer(task_id: str) -> None:
    subscription = task_subscription(task_id)
    state = subscription.snapshot() if subscription is not None else {}
    if state.get('answer'):
        # These are complete verified segments, never fake character-by-character
        # typing or raw tool JSON. st.markdown escapes untrusted HTML by default.
        st.markdown(state['answer'])
    else:
        st.caption(state.get('progress') or '正在整理答复…')
    if state.get('done'):
        stop_task_subscription()
        st.rerun()


def render_question_metrics(telemetry: dict) -> None:
    # Keep native metric semantics, full precision and labels. The scoped local
    # stylesheet wraps long values instead of Streamlit's default ellipsis.
    with st.container(key='question_metrics'):
        timing, tokens = st.columns(2)
        elapsed = duration_text(telemetry.get('elapsed_seconds'))
        total = token_text(telemetry.get('total_tokens'))
        timing.metric('响应时间' if telemetry.get('finished') else '已等待', elapsed, help='完整耗时：' + elapsed)
        tokens.metric('Token 总量', total, help='模型返回的完整 Token 数：' + total)
    st.caption('参与处理：' + agent_text(telemetry))
    st.caption(f"输入 {token_text(telemetry.get('input_tokens'))} · 输出 {token_text(telemetry.get('output_tokens'))} · {usage_note(telemetry)}")
    st.caption(f"响应 {duration_text(telemetry.get('elapsed_seconds'))} · Token {token_text(telemetry.get('total_tokens'))}")


def open_report_preview(task_id: str, artifact_id: str | None = None) -> None:
    """Navigate to existing report bytes; never rerun analysis or generation."""
    st.session_state.workspace_tabs = '文件与报告'
    st.session_state.history_focus_task = task_id
    try:
        if artifact_id is None:
            record = api().task(task_id)
            if record.get('status') == 'succeeded':
                known = 'report_ids' in (record.get('result') or {})
                artifact_id = next((item['artifact_id'] for item in api().artifacts(task_id)
                                    if (item['artifact_id'] in report_ids(record) if known else is_report_artifact(item)) and
                                    PurePosixPath(item.get('name', '')).suffix.lower() == '.html' and
                                    item.get('downloadable', True)), None)
        if artifact_id:
            if f'download_{artifact_id}' not in st.session_state:
                st.session_state[f'download_{artifact_id}'] = api().download(artifact_id)
            st.session_state.report_preview_id = artifact_id
    except ApiError as exc:
        st.session_state.report_preview_error = str(exc)


def report_ids(task: dict) -> list[str]:
    return list(dict.fromkeys((task.get('result') or {}).get('report_ids') or task.get('_ui_report_ids') or []))


def is_report_artifact(item: dict) -> bool:
    """Recognize registered report formats, never uploaded HTML or manifests."""
    return (PurePosixPath(item.get('name', '')).suffix.lower() in {'.html', '.xlsx'} and
            (item.get('kind') in {'report', 'report_html', 'html_report', 'report_xlsx'} or
             item.get('relative_path', '').replace('\\', '/').startswith('report/')))


def conversation_reports(session: dict, task_ids: list[str], chosen_record: dict | None) -> dict[str, list[dict]]:
    """Use delivered session metadata; never fetch every historical task or report body."""
    grouped: dict[str, list[dict]] = {}
    seen = set()
    for version in session.get('report_versions', []):
        tid = version.get('task_id')
        if tid not in task_ids:
            continue
        items = []
        for item in version.get('artifacts', []):
            aid = item.get('artifact_id')
            if (not aid or aid in seen or not is_report_artifact(item) or
                    item.get('task_id', tid) != tid or
                    item.get('session_id', session['session_id']) != session['session_id']):
                continue
            seen.add(aid)
            items.append(item)
        if items:
            grouped.setdefault(tid, []).append({**version, 'artifacts': items})
    # Older API/task records may lack the version catalog. Reuse the selected
    # task's already-fetched artifacts, rather than querying every message.
    if chosen_record and chosen_record.get('status') == 'succeeded':
        tid = chosen_record.get('task_id')
        if tid in task_ids and tid not in grouped:
            items = [item for item in chosen_record.get('_ui_report_items', [])
                     if item.get('artifact_id') in report_ids(chosen_record) and is_report_artifact(item)]
            if items:
                grouped[tid] = [{'task_id': tid, 'title': '', 'artifacts': items}]
    return grouped


def preview_conversation_report(session_id: str, task_id: str, artifact_id: str) -> None:
    """Load one existing attachment on demand and keep the user in the conversation."""
    try:
        if f'download_{artifact_id}' not in st.session_state:
            st.session_state[f'download_{artifact_id}'] = api().download(artifact_id)
        st.session_state.conversation_report_preview = (session_id, task_id, artifact_id)
        st.session_state.pop('conversation_report_error', None)
    except ApiError as exc:
        st.session_state.conversation_report_error = (artifact_id, str(exc))


def close_conversation_report() -> None:
    st.session_state.pop('conversation_report_preview', None)


def render_conversation_reports(session_id: str, task_id: str, versions: list[dict]) -> None:
    """A durable attachment under its own assistant response, including old versions."""
    for version in versions:
        number = f" · 版本 {version['number']}" if version.get('number') else ''
        kind = '修订版' if version.get('revision_of') else '原始报告'
        for item in version['artifacts']:
            aid = item['artifact_id']
            suffix = PurePosixPath(item.get('name', '')).suffix.lower()
            with st.container(key=f'message_report_{task_id}_{aid}'):
                st.markdown(f'<div class="ds-report-attachment-heading">报告附件{number} · {kind}</div>', unsafe_allow_html=True)
                title = version.get('title') or ('HTML 网页报告' if suffix == '.html' else 'Excel 工作簿')
                st.markdown(f'<div class="ds-report-attachment-title">{escape(title)}</div>'
                            f'<div class="ds-artifact-meta">{escape(item.get("name", "报告"))} · {human_size(item.get("size", 0))}</div>',
                            unsafe_allow_html=True)
                if version.get('base_number'):
                    st.caption(f"基于版本 {version['base_number']} 修改，原报告保留。")
                if not item.get('downloadable', True):
                    st.caption('报告文件暂不可用，请在文件与报告页检查。')
                    continue
                actions = st.columns([1, 1, 1] if suffix == '.html' else [1, 1], gap='small')
                offset = 0
                if suffix == '.html':
                    with actions[0]:
                        st.button('在对话中预览', key=f'chat_preview_{task_id}_{aid}',
                                  on_click=preview_conversation_report, args=(session_id, task_id, aid),
                                  icon=':material/visibility:', width='stretch')
                    offset = 1
                with actions[offset]:
                    st.button('打开报告详情', key=f'chat_open_{task_id}_{aid}',
                              on_click=open_report_preview, args=(task_id, aid),
                              icon=':material/open_in_new:', width='stretch')
                with actions[offset + 1]:
                    st.link_button('下载 ' + ('HTML' if suffix == '.html' else 'Excel'),
                                   f'{api().base_url}/api/v1/artifacts/{aid}/download',
                                   icon=':material/download:', width='stretch')
                error = st.session_state.get('conversation_report_error')
                if error and error[0] == aid:
                    st.warning(error[1])
                if (st.session_state.get('workspace_tabs', '对话') == '对话' and
                        st.session_state.get('conversation_report_preview') == (session_id, task_id, aid)):
                    data = st.session_state.get(f'download_{aid}')
                    if data is not None:
                        st.caption('当前对话中的报告预览；可打开报告详情查看完整内容。')
                        components.html(data.decode('utf-8', errors='replace'), height=460, scrolling=True)
                        st.button('收起预览', key=f'chat_close_{task_id}_{aid}', on_click=close_conversation_report)


def render_report_actions(task_id: str, task: dict) -> None:
    if task.get('status') != 'succeeded' or not report_ids(task):
        return
    try:
        all_items = task['_ui_report_items'] if '_ui_report_items' in task else api().artifacts(task_id)
        task['_ui_report_items'] = all_items
        items = [item for item in all_items if item['artifact_id'] in report_ids(task)]
    except ApiError as exc:
        show_error(exc)
        return
    for item in items:
        suffix = PurePosixPath(item.get('name', '')).suffix.lower()
        if not item.get('downloadable', True):
            st.caption('报告正在准备下载，请稍后查看。')
            continue
        if suffix == '.html':
            st.button('预览 HTML 报告', key=f'open_report_{task_id}_{item["artifact_id"]}',
                      on_click=open_report_preview, args=(task_id, item['artifact_id']),
                      icon=':material/description:', width='stretch')
        st.link_button('下载 ' + ('HTML 报告' if suffix == '.html' else 'Excel 报告' if suffix == '.xlsx' else item.get('name', '报告')),
                       f'{api().base_url}/api/v1/artifacts/{item["artifact_id"]}/download',
                       icon=':material/download:', width='stretch')
    if not items:
        st.warning('任务记录中的报告文件当前不可用，可在“文件与报告”查看保留的版本。')


def render_task(task_id: str, *, live: bool, show_response: bool = True, show_files: bool = True,
                files: dict | None = None) -> dict | None:
    try:
        task = dict(api().task(task_id))
        status = task.get('status', 'queued')
        # Older tasks may predate result.report_ids. Project their registered
        # artifacts into this render only; do not rewrite any persisted result.
        if status == 'succeeded' and 'report_ids' not in (task.get('result') or {}):
            try:
                task['_ui_report_items'] = api().artifacts(task_id)
                task['_ui_report_ids'] = [item['artifact_id'] for item in task['_ui_report_items'] if is_report_artifact(item)]
            except ApiError:
                task['_ui_report_lookup_failed'] = True
        if status != 'waiting_user':
            # The displayed task controls clarification context. Leaving a
            # waiting task must not silently attach a new question to it;
            # report format choices remain the user's visible selection.
            st.session_state.pop('reply_to_task', None)
            st.session_state.pop('reply_formats_for', None)
            st.session_state.pop('pending_reply_formats', None)
            st.session_state.pop('reply_database_for', None)
            st.session_state.pop('pending_reply_database', None)
        event_key = f'events_{task_id}'
        cursor_key = f'cursor_{task_id}'
        page = api().events(task_id, after=st.session_state.get(cursor_key, 0))
        events = st.session_state.get(event_key, []) + page.get('items', [])
        st.session_state[event_key] = events[-100:]
        st.session_state[cursor_key] = page.get('cursor', st.session_state.get(cursor_key, 0))
        telemetry = task.get('telemetry', {})
        seen = set(telemetry.get('agents', [])) or {event['role'] for event in events if event.get('role') in ROLES}
        active_role = next((event.get('target') if event.get('type') == 'agent_assigned' else event.get('role')
                            for event in reversed(events)
                            if (event.get('target') if event.get('type') == 'agent_assigned' else event.get('role')) in ROLES),
                           next((role for role in ('report_designer', 'analyst', 'main') if role in seen), 'main'))
        state = 'active' if status in ACTIVE else 'failed' if status in {'failed', 'interrupted'} else 'complete' if status in {'succeeded', 'completed'} else 'neutral'
        st.markdown(task_state(LABELS.get(status, status), state, active_role, seen), unsafe_allow_html=True)
        hint = {'main': '主助手正在整理需求与分析步骤。', 'analyst': '分析助手正在读取数据、计算指标与生成图表。',
                'report_designer': '报告助手正在整理结果并校验报告。'}
        if status in ACTIVE:
            st.markdown(f'<div class="ds-task-hint">{hint[active_role]}</div>', unsafe_allow_html=True)
        elif status == 'succeeded':
            st.markdown('<div class="ds-result-label">' + ('报告已交付' if report_ids(task) else '分析结论已返回' if (task.get('result') or {}).get('analysis_ids') else '答复已完成') + '</div>', unsafe_allow_html=True)
            st.caption('原报告与每次修订均独立保存。' if report_ids(task) else
                       '报告状态暂不可用，请在文件与报告页查看。' if task.get('_ui_report_lookup_failed') else
                       '在对话中查看结论；此任务未生成报告文件。')
            render_report_actions(task_id, task)
        if status in ACTIVE:
            if not live:
                st.session_state.active_task = task_id
                st.rerun()
            if st.button('取消任务', key=f'cancel_{task_id}', width='stretch'):
                api().cancel(task_id)
                st.info('取消请求已提交，正在等待 Worker 终止执行。')
        elif status in {'failed', 'interrupted'}:
            error = task.get('error')
            if error:
                st.error(error.get('message', '任务未完成。') if isinstance(error, dict) else str(error))
            if st.button('重新运行', key=f'retry_{task_id}'):
                new_task = api().retry(task_id)
                st.session_state.active_task = new_task['task_id']
                st.rerun()
        spec = task.get('spec') or {}
        task_files = [(files or {}).get(fid, {'file_id': fid, 'name': '来源文件 ' + fid[:8], 'source': 'unavailable'}) for fid in spec.get('file_ids', [])]
        render_sources(task_files, database_sources=spec.get('database_source_ids', []),
                       database_tables=spec.get('database_tables', {}), title='该任务的数据来源',
                       empty='该任务未记录文件或数据库来源。')
        with st.expander('耗时与 Token', expanded=False):
            render_question_metrics(telemetry)
        with st.expander('运行记录', expanded=False):
            st.caption(f'任务编号：{task_id}')
            st.write('用户问题：' + task.get('spec', {}).get('request', ''))
            st.caption(f"排队 {duration_text(telemetry.get('queue_seconds'))} · 执行 {duration_text(telemetry.get('execution_seconds'))}")
            for route in telemetry.get('routes', []):
                st.text(f"{route.get('created_at', '')} · 主 Agent → {ROLES.get(route['target'], route['target'])}")
            if not telemetry.get('routes') and seen - {'main'}:
                st.caption('旧任务未记录明确分派事件；上方参与 Agent 根据已有调用记录还原。')
            if telemetry.get('by_agent'):
                st.dataframe([{'Agent': ROLES[item['role']],
                    '输入 Token': item['input_tokens'] if item['usage_samples'] else None,
                    '输出 Token': item['output_tokens'] if item['usage_samples'] else None,
                    '总 Token': item['total_tokens'] if item['usage_samples'] else None}
                    for item in telemetry['by_agent']], hide_index=True, width='stretch')
            for event in events[-30:]:
                message = event.get('message') or event.get('summary') or event.get('type', '')
                if message:
                    for role, label in ROLES.items():
                        message = message.replace(role, label)
                    st.text(f"{event.get('created_at', '')} · {message}")
                if event.get('type', '').startswith(('sql_', 'database_')):
                    if event.get('source_id'):
                        st.caption('数据库来源：' + event['source_id'])
                    if event.get('sql'):
                        st.code(event['sql'], language='sql')
                    if event.get('query_id'):
                        st.caption('查询：' + event['query_id'])
                    elif event.get('query_ids'):
                        st.caption('查询：' + '、'.join(event['query_ids']))
                    if event.get('row_count') is not None:
                        st.caption(f"结果 {event['row_count']} 行 · SQL 耗时 {duration_text(event.get('duration_seconds'))}")
                    elif event.get('duration_seconds') is not None:
                        st.caption('SQL 耗时：' + duration_text(event['duration_seconds']))
                    result_ids = ([event['result_artifact_id']] if event.get('result_artifact_id') else event.get('result_artifact_ids', []))
                    if status in {'succeeded', 'waiting_user'}:
                        for artifact_id in result_ids:
                            st.link_button('下载查询结果', f'{api().base_url}/api/v1/artifacts/{artifact_id}/download')
            if not events:
                st.caption('等待任务进度。')
        if status == 'waiting_user':
            st.session_state.reply_to_task = task_id
            if st.session_state.get('reply_formats_for') != task_id:
                # The sidebar widget already exists in this run. Apply the
                # parent defaults before widget creation on the next app run.
                st.session_state.pending_reply_formats = {'session_id': st.session_state.session_id,
                    'task_id': task_id, 'outputs': task.get('spec', {}).get('expected_outputs', [])}
                st.rerun()
            if st.session_state.get('database_feature_enabled') and st.session_state.get('reply_database_for') != task_id:
                st.session_state.pending_reply_database = {'session_id': st.session_state.session_id, 'task_id': task_id,
                    'source_ids': task.get('spec', {}).get('database_source_ids', []),
                    'tables': task.get('spec', {}).get('database_tables', {})}
                st.rerun()
            st.info('请在下方补充信息，下一条消息将关联此任务。')
        response = task.get('final_response') or (task.get('result') or {}).get('final_response')
        if response and show_response:
            st.markdown(response)
        if show_files:
            render_artifacts(task_id)
        if live and status not in ACTIVE:
            stop_task_subscription()
            st.session_state.active_task = None
            st.session_state.last_task = task_id
            st.session_state.history_focus_task = task_id
            st.rerun()
        return task
    except ApiError as exc:
        show_error(exc)


@st.fragment(run_every='2s')
def poll_task(files: dict | None = None) -> None:
    task_id = st.session_state.get('active_task')
    if task_id:
        render_task(task_id, live=True, show_response=False, show_files=False, files=files)



def render_sources(selected: list[dict], *, database_sources: list[str] | None = None,
                   database_tables: dict | None = None, title: str = '下次提问的数据',
                   empty: str = '尚未选择数据，可在左侧添加文件或选择数据库。') -> None:
    if database_sources is None:
        database_sources = st.session_state.get('selected_database_sources', [])
    if database_tables is None:
        database_tables = st.session_state.get('selected_database_tables', {})
    st.markdown(f'<div class="ds-context-title">{escape(title)}<span class="ds-count">{len(selected) + len(database_sources)} 个来源</span></div>',
                unsafe_allow_html=True)
    if not selected and not database_sources:
        st.markdown(f'<div class="ds-empty-source">{escape(empty)}</div>', unsafe_allow_html=True)
    for item in selected:
        if item.get('source') == 'unavailable':
            st.markdown(source_card(name_of(item), 'FILE', '来源记录保留，当前文件信息不可用'), unsafe_allow_html=True)
            continue
        profiles = profile_list(item)
        tabular = [profile for profile in profiles if profile.get('columns')]
        status = item.get('profile_status', '')
        if status in PROFILE_PENDING:
            detail = '正在准备数据概览'
        elif status in {'failed', 'cancelled', 'interrupted'}:
            detail = '数据概览暂不可用'
        elif len(tabular) == 1:
            rows = tabular[0].get('row_count')
            detail = (f'{rows:,} 行' if isinstance(rows, int) else '行数待确认') + f' · {len(tabular[0]["columns"])} 列'
        elif tabular:
            detail = f'{len(tabular)} 个工作表 / 表格'
        else:
            detail = '文字材料' if profiles else human_size(item.get('size', 0))
        detail += ' · ' + ('知识库' if item.get('source') == 'kb' else '本次上传')
        st.markdown(source_card(name_of(item), PurePosixPath(name_of(item)).suffix, detail), unsafe_allow_html=True)
    for source in database_sources:
        names = database_tables.get(source)
        count = len(names) if names is not None else '全部获准'
        st.markdown(source_card({'olist': 'Olist 电商分析', 'sellers_restricted': 'Olist 卖家（受限城市）'}.get(source, source),
                               'SQL', f'{count} 张表 · PostgreSQL 只读'), unsafe_allow_html=True)


def render_starters() -> None:
    st.markdown('<div class="ds-section-label">从一个明确的问题开始</div>', unsafe_allow_html=True)
    starters = [
        ('table', '', '了解数据', '查看字段与数据质量', '探索数据',
         '请检查我选中的数据，说明完整行数、字段含义、缺失值和需要注意的数据质量问题。'),
        ('chart', 'blue', '比较与洞察', '回答具体指标与差异', '比较与分析',
         '比较我选中数据中的主要分组或时间趋势，给出直接结论；指标或统计口径不明确时先向我确认。'),
        ('report', 'amber', '生成报告', '复用结果，支持继续修改', '整理成报告',
         '基于上一次分析生成中文 HTML 和 Excel 报告，列出数据来源与局限。'),
    ]
    with st.container(key='suggestion_grid'):
        starter_columns = st.columns(3, gap='small')
    for index, (column, starter) in enumerate(zip(starter_columns, starters)):
        symbol, color, title, copy, action, prompt = starter
        with column, st.container(key=f'starter_{index}'):
            st.markdown(f'<div class="ds-card-symbol {color}">{icon(symbol)}</div>'
                        f'<div class="ds-card-title">{title}</div><div class="ds-card-copy">{copy}</div>',
                        unsafe_allow_html=True)
            if st.button(action, key=f'example_{index}', width='stretch'):
                st.session_state.prompt = prompt


def render_composer(selected: list[dict]) -> tuple[str | None, list[str]]:
    active = bool(st.session_state.get('active_task'))
    with st.container(key='composer'):
        context_col, format_col = st.columns([1, 1.25], vertical_alignment='center')
        with context_col:
            database_count = len(st.session_state.get('selected_database_sources', []))
            context = f'下次提问：<b>{len(selected)} 份文件 · {database_count} 个数据库来源</b>' if selected or database_count else '下次提问：未选择数据'
            st.markdown(f'<div class="ds-composer-context">{context}</div>', unsafe_allow_html=True)
        with format_col:
            outputs = st.multiselect('需要的报告格式', ['html', 'xlsx'],
                format_func=lambda value: {'html': 'HTML 报告', 'xlsx': 'Excel 报告'}[value],
                key='report_outputs', label_visibility='collapsed', placeholder='报告格式（可选）')
        with st.form('message_form', clear_on_submit=False, border=False):
            st.text_area('你的问题', key='prompt',
                placeholder='输入数据问题，或说明已有报告需要修改的内容…', height=80, disabled=active)
            hint_col, send_col = st.columns([4, 1], vertical_alignment='center')
            with hint_col:
                st.caption('正在处理，可在右侧取消任务。' if active else '需要报告时选择格式；也可以直接提问。')
            with send_col:
                submitted = st.form_submit_button('发送', disabled=active, type='primary',
                                                   icon=':material/arrow_upward:', width='stretch')
    return st.session_state.get('prompt', '').strip() if submitted else None, outputs


def submit_question(session_id: str, content: str, outputs: list[str]) -> None:
    if not content:
        st.warning('请输入问题。')
        return
    db_sources = st.session_state.get('selected_database_sources', [])
    database_kwargs = {}
    if db_sources or st.session_state.get('database_selection_touched'):
        database_kwargs = {'database_source_ids': list(db_sources),
                           'database_tables': {source: list(names) for source, names in st.session_state.get('selected_database_tables', {}).items() if source in db_sources}}
    submission = (content, tuple(st.session_state.get('selected_files', [])), tuple(outputs), st.session_state.get('reply_to_task'),
                  tuple(database_kwargs.get('database_source_ids', [])),
                  tuple(sorted((source, tuple(names)) for source, names in database_kwargs.get('database_tables', {}).items())), bool(database_kwargs))
    if st.session_state.get('submit_payload') != submission:
        st.session_state.submit_key = uuid4().hex
        st.session_state.submit_payload = submission
    key = st.session_state.setdefault('submit_key', uuid4().hex)
    try:
        result = api().submit(session_id, content, st.session_state.get('selected_files', []), outputs, key,
                              reply_to_task_id=st.session_state.get('reply_to_task'), **database_kwargs)
        st.session_state.active_task = result['task_id']
        st.session_state.clear_prompt_next = True
        st.session_state.pop('submit_key', None)
        st.session_state.pop('reply_to_task', None)
        st.rerun()
    except ApiError as exc:
        show_error(exc)


def main() -> None:
    st.markdown(stylesheet(), unsafe_allow_html=True)
    if st.session_state.pop('clear_prompt_next', False):
        st.session_state.prompt = ''
    try:
        health = api().health()
        st.session_state._streaming_enabled = bool((health.get('streaming') or {}).get('enabled', False))
        sessions = api().sessions()
        kb = api().kb_files()
    except ApiError as exc:
        with st.sidebar:
            st.markdown(brand(), unsafe_allow_html=True)
        st.markdown(header('连接工作空间', False), unsafe_allow_html=True)
        st.markdown(welcome(), unsafe_allow_html=True)
        show_error(exc)
        st.info('在项目目录打开 PowerShell，运行：.\\scripts\\start.ps1。启动后点击重新连接。')
        if st.button('重新连接', type='primary', icon=':material/refresh:'):
            st.rerun()
        return

    worker = health.get('worker') or {}
    database_enabled = bool((health.get('database') or {}).get('configured'))
    st.session_state.database_feature_enabled = database_enabled
    if not database_enabled:
        clear_database_selection()
    ready = bool(worker.get('ready', worker.get('alive', False)))
    if not sessions:
        with st.sidebar:
            st.markdown(brand(), unsafe_allow_html=True)
            if st.button('新建对话', key='new_session', icon=':material/add:', type='primary', width='stretch'):
                try:
                    select_session(api().create_session()['session_id'])
                    st.rerun()
                except ApiError as exc:
                    show_error(exc)
        st.markdown(header('新的数据探索', ready), unsafe_allow_html=True)
        st.markdown(welcome(), unsafe_allow_html=True)
        st.info('点击左侧“新建对话”，连接数据并开始提问。')
        return
    pending_database = st.session_state.pop('pending_reply_database', None)
    if pending_database and pending_database['session_id'] == st.session_state.get('session_id'):
        clear_database_selection()
        st.session_state.selected_database_sources = list(pending_database['source_ids'])
        st.session_state.selected_database_tables = {source: list(names) for source, names in pending_database['tables'].items()}
        st.session_state.database_selection_touched = True
        st.session_state.reply_database_for = pending_database['task_id']
    with st.sidebar:
        st.markdown(brand(), unsafe_allow_html=True)
        if st.button('新建对话', key='new_session', icon=':material/add:', type='primary', width='stretch'):
            try:
                select_session(api().create_session()['session_id'])
                st.rerun()
            except ApiError as exc:
                show_error(exc)
        by_id = {item['session_id']: item for item in sessions}
        labels = session_labels(sessions)
        if st.session_state.get('session_id') not in by_id:
            select_session(next(iter(by_id)))
        st.markdown('<div class="ds-side-label">最近会话</div>', unsafe_allow_html=True)
        with st.container(key='session_list'):
            selected = st.radio('会话', list(by_id), index=list(by_id).index(st.session_state.session_id),
                format_func=lambda sid: labels[sid],
                key=f'session_picker_{st.session_state.session_id}', label_visibility='collapsed')
        if selected != st.session_state.session_id:
            select_session(selected)
            st.rerun()
        session_id = st.session_state.session_id
        try:
            uploaded = api().files(session_id)
        except ApiError as exc:
            show_error(exc)
            uploaded = []
        files = {item['file_id']: item for item in kb + uploaded if item.get('file_id')}
        st.markdown('<div class="ds-side-label">分析数据</div>', unsafe_allow_html=True)
        st.multiselect('选择 KB 或上传文件', list(files),
            format_func=lambda fid: ('KB · ' if files[fid].get('source') == 'kb' else '') + name_of(files[fid]),
            key='selected_files', label_visibility='collapsed', placeholder='选择文件或知识库数据')
        if database_enabled:
            render_database_picker(session_id)
        with st.expander('上传与知识库', icon=':material/folder_open:'):
            uploads = st.file_uploader('添加 CSV / Excel / HTML', type=['csv', 'xlsx', 'xls', 'html', 'htm'], accept_multiple_files=True)
            st.caption('单文件最大 1 GB（1024 MB），每批文件总量最多 1 GB。')
            if st.button('上传文件', disabled=not uploads, width='stretch', icon=':material/upload:'):
                try:
                    for item in uploads:
                        item.seek(0)
                    api().upload(session_id, [(item.name, item, item.type or 'application/octet-stream') for item in uploads])
                    st.success('上传已接收。请在文件列表中选择本次使用的数据。')
                    st.rerun()
                except ApiError as exc:
                    show_error(exc)
            if st.button('刷新知识库', width='stretch', icon=':material/refresh:'):
                try:
                    result = api().refresh_kb()
                    if result.get('task_id'):
                        st.session_state.active_task = result['task_id']
                    st.rerun()
                except ApiError as exc:
                    show_error(exc)
        with st.expander('会话设置', icon=':material/settings:'):
            st.caption('删除当前会话及其上传原件、中间产物、报告与任务记录。KB 和两个 Agent 的独立记忆保留。此操作不可撤销。')
            confirmed = st.checkbox('确认删除当前会话及 input / script_output / report 中的相关文件', key=f'delete_confirm_{session_id}')
            if st.button('删除当前会话及产物', key='delete_session', disabled=not confirmed, width='stretch'):
                try:
                    api().delete_session(session_id)
                    clear_deleted_session_cache()
                    remaining = api().sessions()
                    replacement = remaining[0]['session_id'] if remaining else api().create_session()['session_id']
                    select_session(replacement)
                    st.rerun()
                except ApiError as exc:
                    show_error(exc)
        dot = '' if ready else ' offline'
        st.markdown(f'<div class="ds-side-footer"><span class="ds-live-dot{dot}"></span>'
                    f'{"分析服务已就绪" if ready else "分析服务等待连接"} · 本地工作空间</div>', unsafe_allow_html=True)
        if not ready:
            st.warning('Worker 尚未就绪，任务将等待处理。')

    pending_formats = st.session_state.pop('pending_reply_formats', None)
    if pending_formats and pending_formats['session_id'] == session_id:
        st.session_state.report_outputs = list(pending_formats['outputs'])
        st.session_state.reply_formats_for = pending_formats['task_id']
    selected_ids = st.session_state.get('selected_files', [])
    selected_files = [files[fid] for fid in selected_ids if fid in files]
    try:
        session = api().session(session_id)
    except ApiError as exc:
        show_error(exc)
        return
    st.markdown(header(session_name(session), ready), unsafe_allow_html=True)
    tab_labels = ['对话', '数据与知识库', '文件与报告', '提问日志', 'Agent 记忆']
    if st.session_state.get('workspace_tabs') not in tab_labels:
        st.session_state.workspace_tabs = '对话'
    view = 'ds-conversation-view' if st.session_state.workspace_tabs == '对话' else 'ds-document-view'
    st.markdown(f'<span class="ds-view-marker {view}" aria-hidden="true"></span>', unsafe_allow_html=True)
    conversation, data_tab, report_tab, log_tab, memory_tab = st.tabs(tab_labels, key='workspace_tabs', on_change='rerun')
    question_logs = session.get('question_logs', [])
    log_by_task = {item['task_id']: item for item in question_logs}
    tasks = session.get('task_ids') or session.get('tasks') or []
    task_ids = [task if isinstance(task, str) else task.get('task_id') for task in tasks]
    task_ids = [tid for tid in task_ids if tid]
    messages = [message for message in session.get('messages', []) if message.get('role') in {'user', 'assistant'}]
    task_titles = {message.get('task_id'): message.get('content', '') for message in messages if message.get('role') == 'user'}
    task_labels = history_labels(task_ids, task_titles)
    chosen_task = st.session_state.get('active_task')
    chosen_record = None
    with conversation, st.container(key='conversation_workspace'):
        chat_column, context_column = st.columns([3.1, 1.2], gap='large')
        with context_column, st.container(key='context_panel'):
            if st.session_state.get('active_task'):
                st.markdown('<div class="ds-context-title">当前任务</div>', unsafe_allow_html=True)
                poll_task(files)
            elif task_ids:
                st.markdown('<div class="ds-context-title">任务状态</div>', unsafe_allow_html=True)
                history_key = f'history_picker_{session_id}'
                focus = st.session_state.pop('history_focus_task', None)
                if focus in task_ids:
                    st.session_state[history_key] = focus
                if st.session_state.get(history_key) not in task_ids:
                    st.session_state.pop(history_key, None)
                chosen_task = st.selectbox('查看历史任务', task_ids[::-1], key=history_key,
                    format_func=lambda tid: task_labels[tid], label_visibility='collapsed')
                chosen_record = render_task(chosen_task, live=False, show_response=False, show_files=False, files=files)
            else:
                st.session_state.reply_to_task = None
                st.markdown('<div class="ds-context-title">当前任务</div>'
                            '<div class="ds-empty-source">尚未开始任务。发送问题后，这里显示任务状态、结果与数据来源。</div>', unsafe_allow_html=True)
            with st.expander('下次提问的数据', expanded=not task_ids):
                render_sources(selected_files)
        with chat_column, st.container(key='dialogue'):
            st.markdown('<div class="ds-dialogue-heading">当前对话<span>对话与结果自动保存</span></div>', unsafe_allow_html=True)
            response = (chosen_record or {}).get('final_response') or ((chosen_record or {}).get('result') or {}).get('final_response')
            if response and not any(message.get('role') == 'assistant' and
                                    message.get('task_id') == chosen_task for message in messages):
                messages = [*messages, {'role': 'assistant', 'content': response, 'task_id': chosen_task}]
            attached_reports = conversation_reports(session, task_ids, chosen_record)
            # Old sessions may have a delivered report but no saved assistant
            # response. Add a presentation-only response beside its question.
            assistant_tasks = {message.get('task_id') for message in messages if message['role'] == 'assistant'}
            expanded_messages = []
            for index, message in enumerate(messages):
                expanded_messages.append(message)
                tid = message.get('task_id')
                if tid in attached_reports and tid not in assistant_tasks and not any(
                        later.get('task_id') == tid for later in messages[index + 1:]):
                    expanded_messages.append({'role': 'assistant', 'content': '报告已生成。', 'task_id': tid})
            last_assistant = {message.get('task_id'): index for index, message in enumerate(expanded_messages)
                              if message['role'] == 'assistant'}
            with st.container(height=420, border=False, key='chat_history',
                              autoscroll=not bool(st.session_state.get('conversation_report_preview'))):
                if not messages:
                    st.markdown(welcome(), unsafe_allow_html=True)
                    render_starters()
                else:
                    for index, message in enumerate(expanded_messages):
                        is_user = message['role'] == 'user'
                        avatar = ':material/person:' if is_user else ':material/analytics:'
                        with st.chat_message(message['role'], avatar=avatar):
                            st.markdown(f'<div class="ds-message-label {"user" if is_user else "assistant"}">{"你的问题" if is_user else "DS 助手"}</div>', unsafe_allow_html=True)
                            st.markdown(message.get('content', ''))
                            tid = message.get('task_id')
                            if not is_user and last_assistant.get(tid) == index:
                                render_conversation_reports(session_id, tid, attached_reports.get(tid, []))
                    if st.session_state.get('active_task'):
                        with st.chat_message('assistant', avatar=':material/analytics:'):
                            stream_answer(st.session_state.active_task)
                if chosen_record and chosen_record.get('status') == 'succeeded' and report_ids(chosen_record):
                    with st.container(key='conversation_result'):
                        st.caption('本次任务已生成报告，原版和修订版均保留。')
                        st.button('查看本次报告', key=f'view_result_{chosen_task}', on_click=open_report_preview,
                                  args=(chosen_task,), icon=':material/description:')
            content, outputs = render_composer(selected_files)
            if content is not None:
                submit_question(session_id, content, outputs)

    with data_tab:
        st.markdown('<div class="ds-subtitle">数据与知识库</div>'
                    '<div class="ds-lead">检查所选数据的字段、完整行数与质量，再开始分析。</div>', unsafe_allow_html=True)
        for source, description in st.session_state.get('database_catalogs_current', {}).items():
            with st.expander('数据库结构 · ' + source, expanded=not selected_files):
                st.caption(description.get('scope_description', 'PostgreSQL 只读'))
                chosen = st.session_state.get('selected_database_tables', {}).get(source, [])
                for table in description['tables']:
                    if table['qualified_name'] in chosen:
                        st.markdown('**' + table['qualified_name'] + '**')
                        st.caption(table.get('description', ''))
                        st.dataframe(table['columns'], hide_index=True, width='stretch')
        if not selected_files and not st.session_state.get('selected_database_sources'):
            st.info('在左侧选择文件，即可查看数据概览。')
        elif any(item.get('profile_status') in PROFILE_PENDING for item in selected_files):
            poll_profiles(session_id, selected_ids, files)
        else:
            for item in selected_files:
                render_file_profile(item)
        all_files = list({item['file_id']: item for item in kb + uploaded}.values())
        with st.expander(f'全部可用文件 · {len(all_files)} 份', expanded=not selected_files):
            if all_files:
                st.dataframe([{'名称': name_of(item), '来源': '知识库' if item.get('source') == 'kb' else '本次上传',
                    '大小': human_size(item.get('size', 0)), '解析状态': item.get('profile_status', '已登记')}
                    for item in all_files], hide_index=True, width='stretch')
            else:
                st.caption('还没有文件。在左侧上传数据，或刷新知识库。')

    with report_tab:
        st.markdown('<div class="ds-subtitle">文件与报告</div>'
                    '<div class="ds-lead">按版本查看报告，预览或下载已生成的文件。</div>', unsafe_allow_html=True)
        preview_error = st.session_state.pop('report_preview_error', None)
        if preview_error:
            st.error(preview_error)
        if 'report_versions' in session:
            render_report_versions(session['report_versions'])
            if chosen_task:
                st.markdown('**当前任务的中间文件**')
                render_artifacts(chosen_task, include_reports=False)
        elif chosen_task:
            render_artifacts(chosen_task)
        else:
            st.markdown('<div class="ds-empty-source">这里将收好每一次分析的成果。<br>在对话中提出问题，或选择需要的报告格式。</div>', unsafe_allow_html=True)

    with log_tab:
        st.markdown('<div class="ds-subtitle">提问日志</div>', unsafe_allow_html=True)
        st.caption('响应时间从提交到任务结束，包含排队、分析与报告生成；Token 为本次任务所有 Agent 模型调用的累计用量，包含重复发送的上下文。')
        st.caption('思考 Token 已包含在输出中，缓存 Token 已包含在输入中，不重复相加。未返回用量的调用不估算；执行中的数字随任务面板更新，结束后自动刷新本表。')
        if question_logs:
            st.dataframe([{'提问时间（UTC）': item['created_at'], '用户问题': item['question'],
                '状态': LABELS.get(item['status'], item['status']), '参与 Agent': agent_text(item['telemetry']),
                '响应时间': duration_text(item['telemetry'].get('elapsed_seconds')),
                '输入 Token': item['telemetry'].get('input_tokens'), '输出 Token': item['telemetry'].get('output_tokens'),
                '总 Token': item['telemetry'].get('total_tokens'), '统计状态': usage_note(item['telemetry']),
                '任务编号': item['task_id']} for item in reversed(question_logs)], hide_index=True, width='stretch')
            st.caption('在“对话”右侧选择历史任务，并展开“运行记录”，可查看分派时间线和各 Agent 用量。每次重试单独记录。')
        else:
            st.info('提交第一个问题后，这里会保存分派、耗时和 Token 记录。')

    with memory_tab:
        st.markdown('<div class="ds-subtitle">Agent 记忆</div>'
                    '<div class="ds-lead">管理两个分析助手已保存的偏好与经验。</div>', unsafe_allow_html=True)
        owner = st.selectbox('记忆所属 Agent', ['analyst', 'report_designer'],
            format_func=lambda value: {'analyst': '数据分析助手', 'report_designer': '报告生成助手'}[value])
        st.caption('两个 Agent 的记忆分别保存。删除后，后续任务不再检索该条记忆。')
        try:
            memories = api().memories(owner)
            if not memories:
                st.info('还没有保存的记忆。你可以在对话里说明希望助手记住的分析或报告偏好。')
            for entry in memories:
                with st.container(border=True):
                    st.write(entry.get('content', ''))
                    st.caption(f"范围：{entry.get('scope', 'project')} · 状态：{entry.get('status', '')}")
                    if st.button('删除此条记忆', key=f"memory_{entry['memory_id']}", icon=':material/delete:'):
                        api().delete_memory(owner, entry['memory_id'])
                        st.rerun()
        except ApiError as exc:
            show_error(exc)


if __name__ == '__main__':
    main()
