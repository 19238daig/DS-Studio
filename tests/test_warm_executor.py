"""Warm executor: resident child serves claimed tasks via the handoff file."""
import time

import pytest

from src.config import Settings
from src.jobs import Worker
from src.schemas import TaskSpec
from src.storage import Store


@pytest.fixture
def warm_store(tmp_path):
    return Store(Settings(workspace=tmp_path, _env_file=None, api_key='sk-warm-test',
                          warm_executor=True))


def test_warm_executor_serves_claimed_tasks_and_records_completion(warm_store):
    sid = warm_store.new_session()['session_id']
    path = warm_store.root / 'KB' / 'warm.csv'
    path.write_text('v\n1\n2\n', encoding='utf-8')
    fid = warm_store.refresh_kb()[0]['file_id']
    spec = TaskSpec(session_id=sid, request='profile', intent='profile', file_ids=[fid])
    task = warm_store.create_task(spec, kind='profile')

    worker = Worker(warm_store)
    # run_forever minus its signal handlers (signal.signal needs the main thread).
    worker.lock.acquire()
    try:
        worker.recover()
        deadline = time.time() + 120
        while time.time() < deadline:
            worker._cycle()
            if (warm_store.get_task(task['task_id'])['status'] == 'succeeded'
                    and not worker._active):
                break
            time.sleep(0.2)
        assert warm_store.get_task(task['task_id'])['status'] == 'succeeded'
        assert worker.executor is not None
        executor = warm_store.read_json('state/worker.executor.json')
        assert executor['executor_id'] == worker.executor['executor_id']
        assert worker.executor['completed'] >= 1
        # The resident clears the handoff right after the terminal write; poll
        # briefly for that clearance (the parent saw the record first).
        deadline_clear = time.time() + 10
        handoff = {'task_id': 'pending'}
        while time.time() < deadline_clear:
            handoff = warm_store.read_json('state/worker.handoff.json') or {}
            if not handoff.get('task_id'):
                break
            time.sleep(0.2)
        assert not handoff.get('task_id'), 'handoff must be cleared after completion'
    finally:
        worker.stopping = True
        worker._stop_executor()
        worker.lock.release()
    assert not worker.executor, 'shutdown must stop the resident executor'


def test_warm_recover_recycles_a_stale_resident(warm_store):
    worker = Worker(warm_store)
    # A stale registration from a dead previous worker.
    warm_store.write_json('state/worker.executor.json',
                          dict(executor_id='stale', pid=999999, updated_at='2026-01-01T00:00:00+00:00'))
    warm_store.write_json('state/worker.handoff.json', dict(task_id='ghost', executor_id='stale'))
    worker.recover()
    assert not warm_store.safe_path('state/worker.executor.json').exists()
    assert not warm_store.safe_path('state/worker.handoff.json').exists()
