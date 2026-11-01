"""File-backed records with cross-process locks and atomic replacement."""
from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from filelock import FileLock

from src.config import Settings, get_settings
from src.schemas import AnalysisResult, Artifact, TaskSpec, new_id, utcnow

TERMINAL = {'succeeded', 'failed', 'cancelled', 'interrupted', 'waiting_user'}
FILE_TYPES = {'.csv', '.xlsx', '.xls', '.html', '.htm'}
KB_TYPES = FILE_TYPES | {'.md', '.txt'}
DEFAULT_SESSION_TITLE = '新会话'


def auto_title(content: str) -> str:
    """Derive a sidebar title from the first question; '' keeps the default."""
    text = re.sub(r'\s+', ' ', (content or '').strip())
    if not text:
        return ''
    return text[:24] + ('…' if len(text) > 24 else '')


class StoreError(Exception):
    def __init__(self, message: str, status: int = 400, code: str = 'invalid_request'):
        super().__init__(message)
        self.status, self.code = status, code


def identifier(value: str) -> str:
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,128}', value):
        raise StoreError('Invalid record identifier', 422, 'invalid_identifier')
    return value


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


class Store:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self.settings.ensure_directories()
        self.root = self.settings.root
        self.lock = FileLock(str(self.root / 'state/store.lock'), timeout=30)
        # task_id -> (journal size the cache is valid for, last seq); avoids an
        # O(n) re-read of the whole journal on every add_event.
        self._event_seq_cache: dict[str, tuple[int, int]] = {}

    def safe_path(self, relative: str | Path, under: str | Path | None = None) -> Path:
        candidate = Path(relative)
        if candidate.is_absolute() or '..' in candidate.parts:
            raise StoreError('Path outside permitted directory', 422, 'unsafe_path')
        path = (self.root / candidate).resolve()
        base = (self.root / under).resolve() if under else self.root
        if not path.is_relative_to(self.root) or not path.is_relative_to(base):
            raise StoreError('Path outside permitted directory', 422, 'unsafe_path')
        return path

    def read_json(self, relative: str, default: Any = None) -> Any:
        path = self.safe_path(relative)
        with self.lock:
            return json.loads(path.read_text(encoding='utf-8')) if path.exists() else default

    def write_json(self, relative: str, data: Any) -> None:
        path = self.safe_path(relative)
        with self.lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_name(path.name + '.' + new_id() + '.tmp')
            try:
                with temp.open('w', encoding='utf-8') as stream:
                    json.dump(data, stream, ensure_ascii=False, allow_nan=False, indent=2)
                    stream.flush()
                    os.fsync(stream.fileno())
                # On Windows a short-lived open handle can deny replacement.
                # Keep the complete fsynced temporary file and the store lock;
                # retry only access errors, never fall back to in-place writes.
                # Four delays total 375 ms, after which the error is surfaced.
                delays = (0.025, 0.05, 0.1, 0.2)
                for attempt in range(len(delays) + 1):
                    try:
                        os.replace(temp, path)
                        break
                    except PermissionError:
                        if attempt == len(delays):
                            raise
                        time.sleep(delays[attempt])
            finally:
                temp.unlink(missing_ok=True)

    def _index(self, name: str) -> dict:
        return self.read_json(f'state/indexes/{name}.json', {})

    def _save_index(self, name: str, data: dict) -> None:
        self.write_json(f'state/indexes/{name}.json', data)

    def new_session(self, title: str = '新会话') -> dict:
        session = dict(session_id=new_id(), title=title, created_at=utcnow(), messages=[], tasks=[])
        self.write_json(f"state/sessions/{session['session_id']}.json", session)
        return session

    def list_sessions(self) -> list[dict]:
        with self.lock:
            sessions = [self.read_json(str(p.relative_to(self.root))) for p in (self.root / 'state/sessions').glob('*.json')]
            return sorted(sessions, key=lambda x: x['created_at'], reverse=True)

    def get_session(self, session_id: str) -> dict:
        session = self.read_json(f'state/sessions/{identifier(session_id)}.json')
        if session is None:
            raise StoreError('Session not found', 404, 'not_found')
        return session

    def append_message(self, session_id: str, role: str, content: str, **extra) -> dict:
        with self.lock:
            session = self.get_session(session_id)
            message = dict(message_id=new_id(), role=role, content=content, created_at=utcnow(), **extra)
            session['messages'].append(message)
            self.write_json(f'state/sessions/{session_id}.json', session)
            return message

    def list_files(self, session_id: str, include_kb: bool = True) -> list[dict]:
        self.get_session(session_id)
        return [f for f in self._index('files').values() if f.get('active', True) and (f.get('session_id') == session_id or (include_kb and f['source'] == 'kb'))]

    def get_file(self, file_id: str, session_id: str | None = None) -> dict:
        item = self._index('files').get(identifier(file_id))
        if not item or not item.get('active', True):
            raise StoreError('File not found or no longer available', 404, 'not_found')
        if item['source'] != 'kb' and item['session_id'] != session_id:
            raise StoreError('File does not belong to this session', 404, 'not_found')
        return item

    def resolve_file(self, file_id: str, session_id: str) -> Path:
        from src.validation import validation_count
        validation_count(self,'input_hashes')
        item = self.get_file(file_id, session_id)
        path = self.safe_path(item['relative_path'], 'KB' if item['source'] == 'kb' else Path('input') / identifier(session_id))
        if not path.is_file() or file_hash(path) != item['hash']:
            raise StoreError('Input changed; refresh files and submit a new task', 409, 'input_changed')
        return path

    def register_file(self, path: Path, name: str, session_id: str | None, source: str = 'upload', file_id: str | None = None) -> dict:
        path = path.resolve()
        relative = path.relative_to(self.root).as_posix()
        self.safe_path(relative, 'KB' if source == 'kb' else Path('input') / identifier(session_id or ''))
        item = dict(file_id=file_id or new_id(), name=name, source=source, session_id=session_id, relative_path=relative,
                    hash=file_hash(path), size=path.stat().st_size, active=True, profile_status='pending', created_at=utcnow())
        with self.lock:
            files = self._index('files')
            files[item['file_id']] = item
            self._save_index('files', files)
        return item

    def update_file_metadata(self, file_id: str, session_id: str | None = None, **changes) -> dict:
        """Update derived profile fields without modifying source or authorization."""
        allowed = {'profile_status', 'profile', 'profiles', 'profile_error', 'profile_task_id'}
        if set(changes) - allowed:
            raise StoreError('Only derived profile metadata may be updated', 422, 'immutable_file_field')
        if 'profile_status' in changes and changes['profile_status'] not in {'pending', 'queued', 'running', 'ready', 'failed', 'cancelled', 'interrupted'}:
            raise StoreError('Invalid profile state', 422, 'invalid_profile_state')
        with self.lock:
            item = self.get_file(file_id, session_id)
            item.update(changes)
            files = self._index('files')
            files[file_id] = item
            self._save_index('files', files)
            return item

    def finalize_profile_task(self, task_id: str, status: str, reason: str | None = None) -> None:
        """Make cancellation/crash outcomes visible in file lists as well as tasks."""
        with self.lock:
            files = self._index('files')
            changed = False
            for item in files.values():
                if item.get('profile_task_id') == task_id and item.get('profile_status') in {'pending', 'queued', 'running'}:
                    item.update(profile_status=status, profile_error=reason)
                    changed = True
            if changed:
                self._save_index('files', files)

    def refresh_kb(self) -> list[dict]:
        with self.lock:
            files = self._index('files')
            existing = {v['relative_path']: v for v in files.values() if v['source'] == 'kb'}
            found = set()
            for path in sorted((self.root / 'KB').rglob('*')):
                if not path.is_file() or path.suffix.lower() not in KB_TYPES or not path.resolve().is_relative_to((self.root / 'KB').resolve()):
                    continue
                relative = path.relative_to(self.root).as_posix()
                found.add(relative)
                old = existing.get(relative)
                digest = file_hash(path)
                if old and old['hash'] == digest:
                    old['active'] = True
                    files[old['file_id']] = old
                else:
                    if old:
                        files[old['file_id']]['active'] = False
                    new = dict(file_id=new_id(), name=path.name, source='kb', session_id=None, relative_path=relative,
                               hash=digest, size=path.stat().st_size, active=True, profile_status='pending', created_at=utcnow())
                    files[new['file_id']] = new
            for old in existing.values():
                if old['relative_path'] not in found:
                    files[old['file_id']]['active'] = False
            self._save_index('files', files)
            return [f for f in files.values() if f['source'] == 'kb' and f['active']]

    def create_task(self, spec: TaskSpec, **metadata) -> dict:
        with self.lock:
            if self.read_json(f'state/tasks/{identifier(spec.task_id)}.json'):
                raise StoreError('Task already exists', 409, 'conflict')
            session = self.get_session(spec.session_id)
            task = dict(task_id=spec.task_id, session_id=spec.session_id, spec=spec.model_dump(), status='queued', stage='queued',
                        created_at=utcnow(), updated_at=utcnow(), cancel_requested=False, result=None, final_response=None, **metadata)
            self.write_json(f'state/tasks/{spec.task_id}.json', task)
            session['tasks'].append(spec.task_id)
            self.write_json(f'state/sessions/{spec.session_id}.json', session)
            self.add_event(spec.task_id, 'queued', '任务已加入队列')
            return task

    def get_task(self, task_id: str) -> dict:
        task = self.read_json(f'state/tasks/{identifier(task_id)}.json')
        if task is None:
            raise StoreError('Task not found', 404, 'not_found')
        return task

    def list_tasks(self) -> list[dict]:
        with self.lock:
            return sorted([self.read_json(p.relative_to(self.root).as_posix()) for p in (self.root / 'state/tasks').glob('*.json')], key=lambda t: (t['created_at'], t['task_id']))

    def estimate_seconds(self, kind: str = 'message', *, report: bool | None = None,
                         sample: int = 10, within_days: float = 7) -> int | None:
        """Median duration of recent succeeded tasks of a kind, as a rough ETA.

        report=True/False restricts the sample to report-delivering / plain
        tasks (report time dominates mixed medians); tasks older than
        within_days are ignored so earlier runtime eras cannot poison it.
        """
        cutoff = (datetime.now(timezone.utc) - timedelta(days=within_days)).isoformat()

        def duration(task: dict) -> float | None:
            end, start = task.get('finished_at'), task.get('started_at') or task.get('created_at')
            if not end or not start:
                return None
            try:
                seconds = (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()
            except ValueError:
                return None
            return seconds if seconds > 0 else None

        durations = []
        for task in reversed(self.list_tasks()):
            if task.get('kind') != kind or task.get('status') != 'succeeded':
                continue
            if (task.get('finished_at') or '') < cutoff:
                break  # newest-first order: everything after the cutoff is older
            is_report = bool((task.get('result') or {}).get('report_ids'))
            if report is not None and is_report is not report:
                continue
            value = duration(task)
            if value is not None:
                durations.append(value)
                if len(durations) >= sample:
                    break
        if not durations:
            return None
        durations.sort()
        middle = len(durations) // 2
        median = durations[middle] if len(durations) % 2 else (durations[middle - 1] + durations[middle]) / 2
        return max(1, round(median))

    def update_task(self, task_id: str, **changes) -> dict:
        with self.lock:
            task = self.get_task(task_id)
            # A late child completion must never undo a cancellation or interruption.
            if task['status'] in TERMINAL and changes.get('status', task['status']) != task['status']:
                return task
            if task['cancel_requested'] and changes.get('status') in {'succeeded', 'waiting_user'}:
                changes = dict(status='cancelled', stage='cancelled', result=None, final_response=None)
            task.update(changes, updated_at=utcnow())
            self.write_json(f'state/tasks/{task_id}.json', task)
            return task

    def _parseable_event_count(self, path: Path) -> int:
        """Number of parseable journal entries — the seq basis for appending."""
        if not path.exists():
            return 0
        count = 0
        for line in path.read_text(encoding='utf-8').splitlines():
            try:
                json.loads(line)
                count += 1
            except json.JSONDecodeError:
                continue  # A killed writer may leave an incomplete final line.
        return count

    def add_event(self, task_id: str, event_type: str = 'progress', message: str = '', **details) -> dict:
        """Append one journal entry without re-reading the whole file.

        The per-task seq cache is validated against the file size, so an
        append from another process (coordinator cancel, worker resume) is
        always re-derived rather than trusted.
        """
        with self.lock:
            self.get_task(task_id)
            path = self.safe_path(f'state/tasks/{identifier(task_id)}.events.jsonl')
            size = path.stat().st_size if path.exists() else 0
            cached_size, seq = self._event_seq_cache.get(task_id, (None, 0))
            if cached_size != size:
                seq = self._parseable_event_count(path)
            event = dict(seq=seq + 1, type=event_type, message=message, created_at=utcnow(), **details)
            payload = (json.dumps(event, ensure_ascii=False, allow_nan=False) + '\n').encode('utf-8')
            with path.open('ab') as stream:
                if size and size > 0:
                    with path.open('rb') as reader:
                        reader.seek(size - 1)
                        if reader.read(1) != b'\n':
                            # A killed writer left a torn line; terminate it so
                            # this event lands on its own parseable line.
                            stream.write(b'\n')
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            self._event_seq_cache[task_id] = (size + len(payload), seq + 1)
            return event

    def list_events(self, task_id: str, after: int = 0) -> list[dict]:
        path = self.safe_path(f'state/tasks/{identifier(task_id)}.events.jsonl')
        with self.lock:
            events = []
            if path.exists():
                for line in path.read_text(encoding='utf-8').splitlines():
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError:
                        continue  # A killed writer may leave an incomplete final line.
                    if item['seq'] > after:
                        events.append(item)
            return events

    def register_artifact(self, path: Path, task_id: str, session_id: str, kind: str, source_ids: list[str] | None = None) -> Artifact:
        self.get_task(task_id)
        path = Path(path).resolve()
        if not path.is_relative_to(self.root):
            raise StoreError('Artifact outside workspace', 422, 'unsafe_path')
        relative = path.relative_to(self.root).as_posix()
        if not any(path.is_relative_to(self.safe_path(Path(base) / identifier(session_id) / identifier(task_id))) for base in ('script_output', 'report')):
            raise StoreError('Artifact must be in the current task output directory', 422, 'unsafe_path')
        if self.get_task(task_id)['session_id'] != session_id:
            raise StoreError('Task session mismatch', 422)
        artifact = Artifact(task_id=task_id, session_id=session_id, kind=kind, relative_path=relative,
                            mime_type=mimetypes.guess_type(path.name)[0] or 'application/octet-stream', hash=file_hash(path),
                            size=path.stat().st_size, source_ids=source_ids or [], name=path.name)
        with self.lock:
            artifacts = self._index('artifacts')
            artifacts[artifact.artifact_id] = artifact.model_dump()
            self._save_index('artifacts', artifacts)
        return artifact

    def get_artifact(self, artifact_id: str) -> Artifact:
        item = self._index('artifacts').get(identifier(artifact_id))
        if not item:
            raise StoreError('Artifact not found', 404, 'not_found')
        return Artifact.model_validate(item)

    def resolve_artifact(self, artifact_id: str) -> Path:
        from src.validation import validation_count
        validation_count(self,'artifact_hashes')
        item = self.get_artifact(artifact_id)
        path = self.safe_path(item.relative_path)
        if not any(path.is_relative_to(self.safe_path(Path(base) / identifier(item.session_id) / identifier(item.task_id))) for base in ('script_output', 'report')):
            raise StoreError('Artifact outside task directory', 422, 'unsafe_path')
        if not path.is_file() or file_hash(path) != item.hash:
            raise StoreError('Artifact missing or checksum mismatch', 409, 'artifact_changed')
        return path

    def list_artifacts(self, task_id: str) -> list[Artifact]:
        return [Artifact.model_validate(x) for x in self._index('artifacts').values() if x['task_id'] == task_id]

    def save_analysis(self, analysis: AnalysisResult) -> AnalysisResult:
        self.get_session(analysis.session_id)
        if self.get_task(analysis.task_id)['session_id'] != analysis.session_id:
            raise StoreError('Task session mismatch', 422)
        self._validate_analysis_database_refs(analysis)
        relative = f'script_output/{identifier(analysis.session_id)}/{identifier(analysis.task_id)}/analysis_{identifier(analysis.analysis_id)}.json'
        with self.lock:
            self.write_json(relative, analysis.model_dump())
            items = self._index('analyses')
            items[analysis.analysis_id] = dict(path=relative, session_id=analysis.session_id)
            self._save_index('analyses', items)
        return analysis

    def get_analysis(self, analysis_id: str, session_id: str) -> AnalysisResult:
        item = self._index('analyses').get(identifier(analysis_id))
        if not item or item['session_id'] != session_id:
            raise StoreError('Analysis not found in this session', 404, 'not_found')
        analysis = AnalysisResult.model_validate(self.read_json(item['path']))
        for file_id, version in analysis.input_versions.items():
            record = self.get_file(file_id, session_id)
            if record['hash'] != version:
                raise StoreError('Analysis input version has changed', 409, 'stale_analysis')
            self.resolve_file(file_id, session_id)
        self._validate_analysis_database_refs(analysis)
        return analysis

    def _validate_analysis_database_refs(self, analysis: AnalysisResult) -> None:
        if not analysis.db_refs:
            return
        # Local import avoids a storage/database import cycle. Resolve only the
        # server-owned originating task; model references cannot grant access.
        from src.database import DatabaseService
        from src.schemas import TaskSpec
        task = TaskSpec.model_validate(self.get_task(analysis.task_id)['spec'])
        if task.session_id != analysis.session_id:
            raise StoreError('Analysis database task session mismatch', 409, 'invalid_database_snapshot')
        service = DatabaseService(self)
        keys = {'query_id', 'source_id', 'scope_fingerprint', 'metadata_artifact_id', 'result_artifact_id', 'sql_artifact_id'}
        from src.validation import validation_scope
        with validation_scope(self,task):
            for ref in analysis.db_refs:
                if set(ref) != keys or not all(isinstance(v, str) and v for v in ref.values()):
                    raise StoreError('Invalid database snapshot reference', 422, 'invalid_database_snapshot')
                service.authorize_snapshot(task, ref)

    def _purge_session_data(self, session_id: str, tasks: list[dict], keep_files: bool) -> None:
        """Delete task-derived session data. Caller holds the store lock.

        keep_files preserves uploaded files (input/ and the files index) so a
        conversation can be cleared without losing its data sources; full
        deletion removes them together with the session record.
        """
        sid = identifier(session_id)
        bases = ('script_output', 'report') if keep_files else ('input', 'script_output', 'report')
        for base in bases:
            path = self.safe_path(Path(base) / sid, base)
            if path.exists():
                shutil.rmtree(path)
        # Stream journals are session-owned derived output, never KB or
        # memory. Match the same no-active-task deletion boundary.
        streams = self.safe_path(Path('state/streams') / sid, 'state/streams')
        if streams.exists():
            shutil.rmtree(streams)
        # The per-session KB snapshot cache written by backends.snapshot_root
        # is derived data too; leave no stale catalog behind.
        agent_kb = self.safe_path(Path('state/indexes/agent_kb') / sid, 'state/indexes')
        if agent_kb.exists():
            shutil.rmtree(agent_kb)
        indexes = ('artifacts', 'analyses', 'database_queries') if keep_files else \
            ('files', 'artifacts', 'analyses', 'database_queries')
        for index in indexes:
            self._save_index(index, {k: v for k, v in self._index(index).items() if v.get('session_id') != session_id})
        for task in tasks:
            for suffix in ('.json', '.events.jsonl'):
                self.safe_path(f"state/tasks/{task['task_id']}{suffix}").unlink(missing_ok=True)

    def delete_session(self, session_id: str) -> None:
        with self.lock:
            self.get_session(session_id)
            tasks = [t for t in self.list_tasks() if t['session_id'] == session_id]
            if any(t['status'] not in TERMINAL for t in tasks):
                raise StoreError('Cancel or finish active tasks before deleting session', 409, 'task_active')
            self._purge_session_data(session_id, tasks, keep_files=False)
            self.safe_path(f'state/sessions/{session_id}.json').unlink()

    def clear_session(self, session_id: str) -> dict:
        """Drop conversation history and task results; keep uploaded files and sources."""
        with self.lock:
            session = self.get_session(session_id)
            tasks = [t for t in self.list_tasks() if t['session_id'] == session_id]
            if any(t['status'] not in TERMINAL for t in tasks):
                raise StoreError('Cancel or finish active tasks before clearing the conversation', 409, 'task_active')
            self._purge_session_data(session_id, tasks, keep_files=True)
            session['messages'], session['tasks'] = [], []
            self.write_json(f'state/sessions/{session_id}.json', session)
            return session

    def rename_session(self, session_id: str, title: str) -> dict:
        title = (title or '').strip()
        if not title or len(title) > 200:
            raise StoreError('会话标题不能为空且不超过200字。', 422, 'invalid_session_title')
        with self.lock:
            session = self.get_session(session_id)
            session['title'] = title
            self.write_json(f'state/sessions/{session_id}.json', session)
            return session

    def backfill_session_titles(self) -> None:
        """Name default-titled sessions after their first user message.

        Covers sessions created before auto-titling existed; manually renamed
        sessions and message-less sessions are left alone. Idempotent.
        """
        with self.lock:
            for session in self.list_sessions():
                if session.get('title') != DEFAULT_SESSION_TITLE or not session.get('messages'):
                    continue
                first = next((m.get('content') or '' for m in session['messages'] if m.get('role') == 'user'), '')
                title = auto_title(first)
                if title:
                    self.rename_session(session['session_id'], title)
