"""Task-scoped, bounded stream journal, separate from the audit event journal.

Only application-verified text and fixed lifecycle messages enter this channel.
The model's token/tool/reasoning stream is deliberately not a public protocol.
An offset index makes cursor reads incremental; rotation keeps disk use bounded.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import struct
import threading
import weakref
from collections import deque
from typing import Any

from filelock import FileLock

from src.schemas import new_id, utcnow
from src.storage import StoreError, TERMINAL, identifier

KINDS = {'status', 'progress', 'verified_answer_segment', 'artifact_ready', 'final'}
ROLES = {'main', 'analyst', 'report_designer'}
INDEX = struct.Struct('>QQ')  # sequence and byte offset, never a user path
MAX_RECORD_BYTES = 512 * 1024
MAX_JOURNAL_BYTES = 4 * 1024 * 1024
MAX_RECORDS = 4096
RETAIN_RECORDS = 128
READ_LIMIT = 128
_local_locks = weakref.WeakValueDictionary()
_registry_lock = threading.Lock()


def _redact(store, value: Any, limit: int = 65536) -> str:
    text = str(value or '')
    for name in ('api_key', 'db_password', 'db_admin_password', 'db_restricted_password'):
        configured = getattr(store.settings, name, None)
        secret = configured.get_secret_value() if hasattr(configured, 'get_secret_value') else ''
        if secret:
            text = text.replace(secret, '[REDACTED]')
    return re.sub(r'sk-[A-Za-z0-9_-]{8,}', '[REDACTED]', text)[:limit]


def _atomic(path: Path, value: dict) -> None:
    temp = path.with_name(path.name + '.' + new_id() + '.tmp')
    try:
        temp.write_text(json.dumps(value, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


class StreamJournal:
    def __init__(self, store, task_id: str):
        self.store = store
        self.task_id = identifier(task_id)
        task = store.get_task(self.task_id)
        self.session_id = identifier(task['session_id'])
        self.directory = store.safe_path(f'state/streams/{self.session_id}/{self.task_id}')
        self.meta = self.directory / 'head.json'
        self.lock = FileLock(str(self.directory.parent / (self.task_id + '.lock')), timeout=10)
        with _registry_lock:
            self.local_lock = _local_locks.setdefault(str(self.directory), threading.RLock())

    def _generation(self, create=False):
        if not self.meta.exists():
            if not create:
                return None
            self.directory.mkdir(parents=True, exist_ok=True)
            name = new_id()
            (self.directory / (name + '.jsonl')).touch()
            (self.directory / (name + '.idx')).touch()
            _atomic(self.meta, {'generation': name})
        name = identifier(json.loads(self.meta.read_text(encoding='utf-8'))['generation'])
        return self.directory / (name + '.jsonl'), self.directory / (name + '.idx')

    @staticmethod
    def _last(log, index):
        if not index.exists() or index.stat().st_size < INDEX.size:
            return 0, 0, 0
        with index.open('rb') as handle:
            count = index.stat().st_size // INDEX.size
            handle.seek((count - 1) * INDEX.size)
            seq, offset = INDEX.unpack(handle.read(INDEX.size))
        with log.open('rb') as handle:
            handle.seek(offset)
            line = handle.readline(MAX_RECORD_BYTES + 1)
            event = json.loads(line)
            if not isinstance(event, dict) or event.get('seq') != seq or not line.endswith(b'\n'):
                raise StoreError('任务流记录不完整。', 409, 'stream_corrupt')
            end = handle.tell()
        return seq, end, count

    def _read(self, log, index, after, limit):
        size = index.stat().st_size // INDEX.size
        if not size:
            return [], False
        with index.open('rb') as idx, log.open('rb') as data:
            first_seq, _ = INDEX.unpack(idx.read(INDEX.size))
            # Sequences are consecutive within a generation. A rotated journal
            # signals a snapshot rather than pretending missing text was read.
            gap = after < first_seq - 1
            row = max(0, after - first_seq + 1)
            if row >= size:
                return [], gap
            idx.seek(row * INDEX.size)
            items = []
            for _ in range(min(limit, size - row)):
                seq, offset = INDEX.unpack(idx.read(INDEX.size))
                data.seek(offset)
                line = data.readline(MAX_RECORD_BYTES + 1)
                if len(line) > MAX_RECORD_BYTES or not line.endswith(b'\n'):
                    raise StoreError('任务流记录不完整。', 409, 'stream_corrupt')
                event = json.loads(line)
                if (not isinstance(event, dict) or event.get('seq') != seq or
                        event.get('task_id') != self.task_id or event.get('session_id') != self.session_id or
                        event.get('kind') not in KINDS or not isinstance(event.get('data'), dict)):
                    raise StoreError('任务流游标不一致。', 409, 'stream_corrupt')
                items.append(event)
            return items, gap

    def read(self, after=0, limit=READ_LIMIT):
        if not isinstance(after, int) or after < 0 or limit < 1 or limit > READ_LIMIT:
            raise StoreError('无效的任务流游标。', 422, 'invalid_stream_cursor')
        # Always acquire Store before the stream lock. delete_session uses the
        # same Store lock, so a stale journal cannot resurrect deleted content.
        with self.store.lock, self.local_lock:
            self._assert_owner()
            if not self.directory.exists():
                return {'items': [], 'cursor': after, 'gap': False, 'first_seq': None, 'last_seq': 0, 'last_kind': None}
            with self.lock:
                return self._read_page(after, limit)

    def _assert_owner(self):
        task = self.store.get_task(self.task_id)
        # Presence under Store.lock is sufficient here: session IDs are
        # immutable. Do not parse a potentially long chat history every 150 ms.
        if not self.store.safe_path(f'state/sessions/{self.session_id}.json').is_file():
            raise StoreError('Session not found', 404, 'not_found')
        if task['session_id'] != self.session_id:
            raise StoreError('任务流作用域不一致。', 404, 'not_found')

    def _read_page(self, after, limit):
        paths = self._generation()
        if paths is None:
            return {'items': [], 'cursor': after, 'gap': False, 'first_seq': None, 'last_seq': 0, 'last_kind': None}
        log, index = paths
        items, gap = self._read(log, index, after, limit)
        with index.open('rb') as handle:
            head = handle.read(INDEX.size)
        first_seq = INDEX.unpack(head)[0] if head else None
        last_seq, _, _ = self._last(log, index)
        last, _ = self._read(log, index, max(0, last_seq - 1), 1)
        return {'items': items, 'cursor': items[-1]['seq'] if items else after,
                'gap': gap, 'first_seq': first_seq, 'last_seq': last_seq,
                'last_kind': last[0]['kind'] if last else None}

    def append(self, event):
        with self.store.lock, self.local_lock:
            self._assert_owner()
            self.directory.parent.mkdir(parents=True, exist_ok=True)
            with self.lock:
                return self._append_locked(event)

    def _append_locked(self, event):
        # Read only the last indexed record; unlike Store.add_event this
        # does not scan the journal. Only trusted application events use
        # this lock; raw model tokens never publish here.
        log, index = self._generation(create=True)
        seq, end, count = self._last(log, index)
        if count:
            last, _ = self._read(log, index, seq - 1, 1)
            if last and last[0]['kind'] == 'final':
                return last[0]
        event = {**event, 'seq': seq + 1, 'task_id': self.task_id,
                 'session_id': self.session_id, 'created_at': utcnow()}
        raw = (json.dumps(event, ensure_ascii=False, separators=(',', ':')) + '\n').encode('utf-8')
        if len(raw) > MAX_RECORD_BYTES:
            raise StoreError('任务流记录过大。', 422, 'stream_record_too_large')
        if end + len(raw) > MAX_JOURNAL_BYTES or count >= MAX_RECORDS:
            kept, _ = self._read(log, index, max(0, seq - RETAIN_RECORDS), RETAIN_RECORDS)
            old_log, old_index = log, index
            name = new_id()
            log, index = self.directory / (name + '.jsonl'), self.directory / (name + '.idx')
            with log.open('wb') as data, index.open('wb') as idx:
                for item in kept:
                    idx.write(INDEX.pack(item['seq'], data.tell()))
                    data.write((json.dumps(item, ensure_ascii=False, separators=(',', ':')) + '\n').encode('utf-8'))
            _atomic(self.meta, {'generation': name})
            old_log.unlink(missing_ok=True); old_index.unlink(missing_ok=True)
            _, end, count = self._last(log, index)
        with log.open('r+b') as data, index.open('r+b') as idx:
            # Discard unindexed tails after an interrupted append.
            data.truncate(end); idx.truncate(count * INDEX.size)
            data.seek(end); data.write(raw); data.flush()
            idx.seek(count * INDEX.size); idx.write(INDEX.pack(event['seq'], end)); idx.flush()
            if event['kind'] == 'final':
                os.fsync(data.fileno()); os.fsync(idx.fileno())
        if event['kind'] in {'verified_answer_segment', 'final'}:
            checkpoint = self.directory / 'verified.json'
            old = json.loads(checkpoint.read_text(encoding='utf-8')) if checkpoint.exists() else {}
            answer = (event['data'].get('answer', '') if event['kind'] == 'final' else
                      old.get('answer', '') + event['data']['text'])
            _atomic(checkpoint, {'seq': event['seq'], 'answer': answer[:65536]})
        return event

    def checkpoint(self):
        path = self.directory / 'verified.json'
        with self.store.lock, self.local_lock:
            self._assert_owner()
            return json.loads(path.read_text(encoding='utf-8')) if path.exists() else {'seq': 0, 'answer': ''}


def publish(store, task_id: str, kind: str, *, role: str | None = None, **data):
    """Publish a trusted application event, not raw model deltas.

    verified_answer_segment requires verified=True and an already validated
    conclusion. final/artifact events are always derived from persisted state.
    """
    if not getattr(store.settings, 'sse_stream_enabled', False):
        return None
    if kind not in KINDS or (role is not None and role not in ROLES):
        raise StoreError('无效的任务流事件。', 422, 'invalid_stream_event')
    task = store.get_task(task_id)
    if kind == 'final':
        return finish(store, task_id)
    if kind == 'artifact_ready':
        if task['status'] != 'succeeded':
            raise StoreError('报告尚未完成校验。', 409, 'incomplete_artifact')
        reports = (task.get('result') or {}).get('report_ids', [])
        payload = {'report_ids': list(reports)}
    elif kind == 'verified_answer_segment':
        if data.get('verified') is not True:
            raise StoreError('仅允许已验证结论进入任务流。', 422, 'unverified_stream_content')
        if set(data) - {'text', 'verified'}:
            raise StoreError('任务流不接受工具参数或原始模型内容。', 422, 'invalid_stream_event')
        payload = {'text': _redact(store, data.get('text')), 'verified': True}
        if not payload['text']:
            return None
        journal = StreamJournal(store, task_id)
        last = None
        # This is segmentation of already verified text, never simulated token
        # typing or a per-token fsync/audit event. Final replaces these segments.
        for offset in range(0, len(payload['text']), 8192):
            last = journal.append({'kind': kind, 'role': role,
                                   'data': {'text': payload['text'][offset:offset + 8192], 'verified': True}})
        return last
    else:
        unknown = set(data) - {'text', 'message', 'stage', 'status'}
        if unknown:
            raise StoreError('任务流不接受工具参数或原始模型内容。', 422, 'invalid_stream_event')
        payload = {'text': _redact(store, data.get('text', data.get('message', '')), 500)}
        for key in ('stage', 'status'):
            if key in data:
                payload[key] = _redact(store, data[key], 100)
    return StreamJournal(store, task_id).append({'kind': kind, 'role': role, 'data': payload})


def final_payload(store, task):
    from src.telemetry import summarize_task
    status = task['status']
    answer = task.get('final_response') or (task.get('result') or {}).get('final_response') or ''
    return {'status': status,
            'answer': _redact(store, answer) if status in {'succeeded', 'waiting_user'} else '',
            'error': _redact(store, task.get('error', '')) if status in {'failed', 'interrupted'} else '',
            'report_ids': list((task.get('result') or {}).get('report_ids', [])) if status == 'succeeded' else [],
            'telemetry': summarize_task(task, store.list_events(task['task_id']))}


def finish(store, task_id):
    if not getattr(store.settings, 'sse_stream_enabled', False):
        return None
    task = store.get_task(task_id)
    if task['status'] not in TERMINAL:
        raise StoreError('任务尚未结束。', 409, 'task_active')
    return StreamJournal(store, task_id).append({'kind': 'final', 'role': None, 'data': final_payload(store, task)})


def lifecycle_event(store, task_id, event_type, message='', **details):
    """Small public lifecycle projection; raw audit details stay in audit logs."""
    if event_type == 'tool':
        # Per-tool movement keeps the chat live line visibly progressing during
        # long computations; tool arguments/results stay in the audit journal.
        role = details.get('role')
        return publish(store, task_id, 'progress', role=role if role in ROLES else None,
                       stage='tool', text=message or '正在执行分析步骤。')
    messages = {
        'running': '已开始处理。', 'profiling': '正在准备所选数据。',
        'profile_ready': '所选数据已就绪。', 'agent_assigned': '已分配处理任务。',
        'model_started': '正在理解请求与整理结果。', 'sql_started': '正在查询数据。',
        'sql_completed': '查询结果已完成验证。', 'analysis_ready': '分析结果已就绪。',
        'report_validation': '正在校验报告。', 'report_revision_prepared': '已锁定报告版本与修改范围。',
    }
    if event_type not in messages:
        return None
    role = details.get('target') if event_type == 'agent_assigned' else details.get('role')
    return publish(store, task_id, 'progress', role=role if role in ROLES else None,
                   stage=event_type, text=messages[event_type])


def best_effort_lifecycle(store, task_id, event_type, message='', **details):
    try:
        return lifecycle_event(store, task_id, event_type, message, **details)
    except (OSError, ValueError, StoreError, TimeoutError):
        # The audit/task state remains authoritative. A broken subscriber or
        # stream disk must not turn a completed analysis into a failed task.
        return None


def best_effort_publish(store, task_id, kind, *, role=None, **data):
    """A derived live view must not invalidate a canonical validated result."""
    try:
        return publish(store, task_id, kind, role=role, **data)
    except (OSError, ValueError, StoreError, TimeoutError):
        return None


def best_effort_finish(store, task_id):
    try:
        return finish(store, task_id)
    except (OSError, ValueError, StoreError, TimeoutError):
        return None
