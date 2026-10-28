"""Short-lived, task-local validation scopes, never a permission TTL cache.

Only a source catalog is memoized. Persisted selections, snapshot/index
identity and every file checksum are checked on every public authorization.
An outer model/tool boundary must use ``fresh=True``; nested helpers can share
that one boundary. A new scope cannot inherit a previous boundary's catalog.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from functools import wraps
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from typing import Any, Callable


@dataclass
class ValidationScope:
    root: Path
    catalog_values: dict[tuple, dict] = field(default_factory=dict)
    counters: dict[str, int] = field(default_factory=dict)
    lock: RLock = field(default_factory=RLock)

    def count(self, name: str) -> None:
        with self.lock:
            self.counters[name] = self.counters.get(name, 0) + 1


_CURRENT: ContextVar[ValidationScope | None] = ContextVar('ds_validation_scope', default=None)


def current_scope(store) -> ValidationScope | None:
    scope = _CURRENT.get()
    if (scope is None or scope.root != store.root.resolve() or
            not getattr(store.settings, 'auth_dedup_enabled', True)):
        return None
    return scope


@contextmanager
def validation_scope(store, task=None, *, fresh: bool = False):
    """Share catalog work within a boundary; ``fresh`` starts a new boundary.

    ``task`` is deliberately not an authorization grant. Every caller still
    validates its own persisted task/source/table selection normally.
    ContextVar isolates threads/tasks; a lock protects intentional nested use.
    """
    existing = current_scope(store)
    if existing is not None and not fresh:
        yield existing
        return
    scope = ValidationScope(store.root.resolve())
    token = _CURRENT.set(scope)
    try:
        from src.performance import stage
        with stage(store, 'validation_boundary', counters=scope.counters):
            yield scope
    finally:
        _CURRENT.reset(token)


def catalog_once(store, key: tuple, factory: Callable[[], dict]) -> dict:
    scope = current_scope(store)
    if scope is None:
        return factory()
    with scope.lock:
        scope.count('catalog_requests')
        if key not in scope.catalog_values:
            value = factory()
            # Do not retain a half-built result or allow a picker to narrow the
            # cached descriptor in-place for another authorization check.
            scope.catalog_values[key] = deepcopy(value)
            scope.count('catalog_loads')
        else:
            scope.count('catalog_hits')
        return deepcopy(scope.catalog_values[key])


def validation_count(store, name: str) -> None:
    scope = current_scope(store)
    if scope is not None:
        scope.count(name)


def validated_method(function):
    """Group nested source checks in an existing public service operation."""
    @wraps(function)
    def wrapped(self, *args, **kwargs):
        with validation_scope(self.store):
            return function(self, *args, **kwargs)
    return wrapped
