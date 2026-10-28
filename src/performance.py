"""Local elapsed-time spans without prompts, credentials or data payloads."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import re
import time
from uuid import uuid4


_PARENT: ContextVar[dict | None] = ContextVar('ds_performance_parent', default=None)
_TEXT = {'tool_name', 'operation', 'source_id', 'artifact_id', 'analysis_id', 'query_id',
         'boundary', 'cache_status', 'mode', 'format', 'component', 'phase', 'model'}
_NUMBER = {'row_count', 'column_count', 'artifact_count', 'analysis_count', 'query_count',
           'file_count', 'bytes', 'input_chars', 'output_chars', 'schema_chars',
           'catalog_requests', 'catalog_loads', 'catalog_hits', 'snapshot_checks',
           'artifact_hashes', 'input_hashes'}
_NAME = re.compile(r'^[A-Za-z][A-Za-z0-9_.:-]{0,95}$')


def _fields(values):
    safe = {}
    for key, value in values.items():
        if key in _TEXT and isinstance(value, str) and _NAME.fullmatch(value):
            safe[key] = value
        elif key in _NUMBER and isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
            safe[key] = value
    return safe


@contextmanager
def span(store, task_id: str, name: str, *, role: str | None = None, counters: dict | None = None, **safe_fields):
    """Emit one completion event, also on cancellation/failure.

    Only structural allowlisted fields are emitted. Exception text and return
    values are never captured. Normal journal failures remain visible; this
    helper does not turn logging errors into silent performance successes.
    """
    if not getattr(store.settings, 'perf_trace_enabled', True):
        yield None
        return
    if not _NAME.fullmatch(name):
        raise ValueError('Invalid performance span name')
    parent = _PARENT.get()
    identifier = uuid4().hex
    token = _PARENT.set(dict(span_id=identifier, task_id=task_id, root=store.root.resolve()))
    started = time.perf_counter()
    status = 'ok'
    try:
        yield identifier
    except BaseException:
        status = 'error'
        raise
    finally:
        _PARENT.reset(token)
        details = dict(span_id=identifier, name=name, duration_seconds=round(time.perf_counter()-started, 6),
                       status=status, **_fields({**safe_fields, **(counters or {})}))
        if parent and parent['task_id'] == task_id:
            details['parent_span_id'] = parent['span_id']
        if role in {'main', 'analyst', 'report_designer'}:
            details['role'] = role
        try:
            store.add_event(task_id, 'span_completed', '运行阶段计时', **details)
        except Exception:
            if status == 'ok':
                raise
            # A failed tracing sink must not replace the original SQL,
            # permission, cancellation or renderer failure during unwinding.


@contextmanager
def stage(store, name: str, *, role: str | None = None, **safe_fields):
    """Measure an internal stage only when a task span already exists."""
    parent = _PARENT.get()
    if parent is None or parent['root'] != store.root.resolve():
        yield None
        return
    with span(store, parent['task_id'], name, role=role, **safe_fields) as identifier:
        yield identifier


def traced_method(name: str, *, role: str | None = None):
    """Trace methods with the existing ``(self, task, ...)`` contract."""
    def decorate(function):
        @wraps(function)
        def wrapped(self, task, *args, **kwargs):
            with span(self.store, task.task_id, name, role=role):
                return function(self, task, *args, **kwargs)
        return wrapped
    return decorate
