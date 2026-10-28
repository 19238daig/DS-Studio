import threading
import time

from src.config import Settings
from src.jobs import Worker
from src.schemas import MessageRequest
from src.services import Services
from src.storage import Store


def explicit_test_runner(task, store, event, cancel_check):
    event('test', 'explicit deterministic test stub')
    return {'final_response': 'test stub completed'}


def test_failed_agent_final_response_is_kept_as_actionable_error(tmp_path):
    store = Store(Settings(workspace=tmp_path, _env_file=None))
    sid = store.new_session()['session_id']
    task = Services(store).submit(sid, MessageRequest(content='report'))
    def runner(*args):
        return {'status': 'failed', 'final_response': 'Report renderer not ready; analysis preserved.'}
    Worker(store).run_once(runner, subprocess_mode=False)
    record = store.get_task(task['task_id'])
    assert record['status'] == 'failed' and 'Report renderer not ready' in record['error']


def slow_test_runner(task, store, event, cancel_check):
    time.sleep(10)
    return {'final_response': 'should not appear'}


def test_queue_single_slot_and_order(tmp_path):
    store = Store(Settings(workspace=tmp_path, _env_file=None))
    sid = store.new_session()['session_id']
    service = Services(store)
    first = service.submit(sid, MessageRequest(content='one'))
    second = service.submit(sid, MessageRequest(content='two'))
    worker = Worker(store)
    assert worker.run_once(explicit_test_runner, subprocess_mode=False)
    assert store.get_task(first['task_id'])['status'] == 'succeeded'
    assert store.get_task(second['task_id'])['status'] == 'queued'
    assert worker.run_once(explicit_test_runner, subprocess_mode=False)
    assert not worker.run_once(explicit_test_runner, subprocess_mode=False)
    assert len([m for m in store.get_session(sid)['messages'] if m['role'] == 'assistant']) == 2


def test_spawn_execution_is_supported(tmp_path):
    store = Store(Settings(workspace=tmp_path, _env_file=None))
    sid = store.new_session()['session_id']
    task = Services(store).submit(sid, MessageRequest(content='test'))
    Worker(store).run_once(explicit_test_runner)
    assert store.get_task(task['task_id'])['status'] == 'succeeded'


def test_running_cancellation_terminates_child(tmp_path, monkeypatch):
    # No Docker calls are needed for this runner: it never starts a sandbox.
    monkeypatch.setattr('src.jobs.stop_task_containers', lambda _: None)
    store = Store(Settings(workspace=tmp_path, _env_file=None))
    sid = store.new_session()['session_id']
    task = Services(store).submit(sid, MessageRequest(content='test'))
    worker = Worker(store)
    thread = threading.Thread(target=worker.run_once, args=(slow_test_runner,))
    thread.start()
    deadline = time.time() + 8
    while store.get_task(task['task_id'])['status'] != 'running' and time.time() < deadline:
        time.sleep(0.05)
    Services(store).cancel(task['task_id'])
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert store.get_task(task['task_id'])['status'] == 'cancelled'
    assert not store.get_task(task['task_id'])['final_response']


def test_recovery_only_on_worker_not_api(tmp_path, monkeypatch):
    monkeypatch.setattr('src.jobs.stop_task_containers', lambda _: None)
    store = Store(Settings(workspace=tmp_path, _env_file=None))
    sid = store.new_session()['session_id']
    task = Services(store).submit(sid, MessageRequest(content='test'))
    worker = Worker(store)
    worker.claim()
    from src.api import create_app
    create_app(store.settings)
    assert store.get_task(task['task_id'])['status'] == 'running'
    worker.recover()
    assert store.get_task(task['task_id'])['status'] == 'interrupted'


def test_profile_runs_in_spawned_worker_and_survives_reopen(tmp_path):
    store = Store(Settings(workspace=tmp_path, _env_file=None))
    sid = store.new_session()['session_id']
    path = store.root / 'input' / sid / 'data.csv'
    path.parent.mkdir(parents=True)
    path.write_text('name,value\na,2\nb,4\n')
    record = store.register_file(path, 'data.csv', sid)
    queued = Services(store).queue_profiles([record['file_id']], sid)[0]
    Worker(store).run_once()
    saved = Store(store.settings).get_file(record['file_id'], sid)
    assert saved['profile_status'] == 'ready'
    assert saved['profile']['row_count'] == 2
    assert store.get_task(queued['profile_task_id'])['status'] == 'succeeded'


def test_first_kb_analysis_profiles_before_agent_dispatch(tmp_path, monkeypatch):
    import sys
    from types import SimpleNamespace
    store = Store(Settings(workspace=tmp_path, _env_file=None))
    sid = store.new_session()['session_id']
    (store.root / 'KB' / 'data.csv').write_text('x\n1\n2\n')
    file = store.refresh_kb()[0]

    def test_agent(task, agent_store, event, cancel_check):
        assert agent_store.get_file(file['file_id'], sid)['profile_status'] == 'ready'
        return {'final_response': 'explicit test agent stub'}

    monkeypatch.setitem(sys.modules, 'src.agents', SimpleNamespace(run_task=test_agent))
    task = Services(store).submit(sid, MessageRequest(content='test', file_ids=[file['file_id']]))
    Worker(store).run_once(subprocess_mode=False)
    assert store.get_task(task['task_id'])['status'] == 'succeeded'


def test_interrupted_profile_does_not_remain_queued(tmp_path, monkeypatch):
    monkeypatch.setattr('src.jobs.stop_task_containers', lambda _: None)
    store = Store(Settings(workspace=tmp_path, _env_file=None))
    sid = store.new_session()['session_id']
    path = store.root / 'input' / sid / 'data.csv'
    path.parent.mkdir(parents=True)
    path.write_text('x\n1\n')
    record = store.register_file(path, 'data.csv', sid)
    Services(store).queue_profiles([record['file_id']], sid)
    worker = Worker(store)
    worker.claim()
    worker.recover()
    assert store.get_file(record['file_id'], sid)['profile_status'] == 'interrupted'
