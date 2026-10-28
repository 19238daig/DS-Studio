"""Real spawned-process isolation without provider calls or Docker."""
import time
from types import SimpleNamespace
from unittest.mock import Mock

from src.config import Settings
from src.jobs import Worker
from src.schemas import MessageRequest
from src.services import Services
from src.storage import Store


def gated_runner(task, store, event, cancel_check):
    directory = store.root / 'state/test_gates'
    directory.mkdir(exist_ok=True)
    (directory / (task.task_id + '.started')).write_text(task.session_id)
    deadline = time.monotonic() + 15
    while not (directory / (task.task_id + '.release')).exists():
        if cancel_check():
            raise InterruptedError('test cancel')
        if time.monotonic() >= deadline:
            raise TimeoutError('test gate never released')
        time.sleep(.03)
    return {'final_response': task.request}


def tick_until(worker, condition, timeout=12):
    deadline = time.monotonic() + timeout
    while not condition() and time.monotonic() < deadline:
        worker._cycle(gated_runner)
        time.sleep(.03)
    assert condition(), 'isolated worker condition timed out'


def test_two_sessions_overlap_but_same_session_and_cancel_remain_isolated(tmp_path, monkeypatch):
    monkeypatch.setattr('src.jobs.stop_task_containers', lambda _: None)
    store = Store(Settings(workspace=tmp_path, worker_concurrency=2, _env_file=None))
    first_sid = store.new_session()['session_id']
    other_sid = store.new_session()['session_id']
    service = Services(store)
    first = service.submit(first_sid, MessageRequest(content='first'))['task_id']
    revision = service.submit(first_sid, MessageRequest(content='second in same session'))['task_id']
    other = service.submit(other_sid, MessageRequest(content='other session'))['task_id']
    worker = Worker(store)
    monkeypatch.setattr(worker, '_capacity', lambda: 2)
    directory = store.root / 'state/test_gates'
    with worker.lock:
        try:
            worker._cycle(gated_runner)
            assert set(worker._active) == {first, other}
            assert store.get_task(revision)['status'] == 'queued'
            tick_until(worker, lambda: all((directory / (tid + '.started')).exists() for tid in (first, other)))
            heartbeat = store.read_json('state/worker.json')
            assert set(heartbeat['task_ids']) == {first, other}
            assert store.get_task(first)['child_pid'] != store.get_task(other)['child_pid']
            service.cancel(first)
            tick_until(worker, lambda: store.get_task(first)['status'] == 'cancelled' and revision in worker._active)
            # Cancellation of one task cannot kill a different task process.
            assert store.get_task(other)['status'] == 'running'
            assert worker._active[other]['child'].is_alive()
            assert first not in worker._active
            tick_until(worker, lambda: (directory / (revision + '.started')).exists())
            for tid in (revision, other):
                (directory / (tid + '.release')).touch()
            tick_until(worker, lambda: not worker._active)
            assert [store.get_task(tid)['status'] for tid in (first, revision, other)] == ['cancelled', 'succeeded', 'succeeded']
            assert [m['content'] for m in store.get_session(first_sid)['messages'] if m['role'] == 'assistant'] == ['second in same session']
            assert [m['content'] for m in store.get_session(other_sid)['messages'] if m['role'] == 'assistant'] == ['other session']
        finally:
            worker.stopping = True
            tick_until(worker, lambda: not worker._active)


def test_capacity_defaults_to_one_and_low_memory_falls_back(tmp_path, monkeypatch):
    import psutil
    settings = Settings(workspace=tmp_path, _env_file=None)
    worker = Worker(Store(settings))
    assert settings.worker_concurrency == 1 and worker._capacity() == 1
    settings.worker_concurrency = 2
    monkeypatch.setattr(psutil, 'virtual_memory', lambda: SimpleNamespace(available=128 * 1024**2))
    assert worker._capacity() == 1
    monkeypatch.setattr(psutil, 'virtual_memory', lambda: SimpleNamespace(available=8 * 1024**3))
    assert worker._capacity() == 2
    object.__delattr__(settings, 'worker_concurrency')
    assert worker._capacity() == 1


def test_session_stays_reserved_until_terminal_child_has_exited(tmp_path):
    store = Store(Settings(workspace=tmp_path, worker_concurrency=2, _env_file=None))
    sid = store.new_session()['session_id']
    service = Services(store)
    first = service.submit(sid, MessageRequest(content='first'))['task_id']
    second = service.submit(sid, MessageRequest(content='second'))['task_id']
    worker = Worker(store)
    store.update_task(first, status='succeeded')
    worker._active[first] = {'session_id': sid}
    assert worker.claim() is None
    del worker._active[first]
    assert worker.claim()['task_id'] == second


def test_deleted_completed_session_does_not_crash_coordinator_or_reappear(tmp_path, monkeypatch):
    monkeypatch.setattr('src.jobs.stop_task_containers', lambda _: None)
    store = Store(Settings(workspace=tmp_path, _env_file=None))
    sid = store.new_session()['session_id']
    tid = Services(store).submit(sid, MessageRequest(content='done'))['task_id']
    store.update_task(tid, status='succeeded')
    worker = Worker(store)
    child = Mock()
    child.is_alive.return_value = False
    worker._active[tid] = {'session_id': sid, 'child': child, 'started': time.monotonic()}
    store.delete_session(sid)
    assert worker._poll_active() and not worker._active
    child.join.assert_called_once()
    assert not store.safe_path(f'state/tasks/{tid}.json').exists()
